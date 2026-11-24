import asyncio
import math
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from staff import wire, worker
from staff.config import Config, Member
from staff.ledger import Ledger, RuleBroken
from staff.runner import Runner, parse_definition

TOM = "@tom:example.org"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Room:
    def __init__(self, fail: Exception | None = None):
        self.said = []
        self.fail = fail

    async def say(self, member, item_id, kind, text, reply_to=None):
        if self.fail:
            raise self.fail
        self.said.append((member, item_id, kind, text))
        return f"$ev{len(self.said)}"


def result(cost=0.12, sid="sess-1", is_error=False, subtype="success", text="done"):
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=3,
        session_id=sid, total_cost_usd=cost, result=text,
    )


class FakeQuery:
    """Stands in for claude_agent_sdk.query; records every call's options."""

    def __init__(self, sid="sess-1", cost=0.12, raises=None, during=None):
        self.calls = []
        self.sid, self.cost, self.raises, self.during = sid, cost, raises, during

    def __call__(self, *, prompt, options):
        self.calls.append((prompt, options))
        return self._gen(options)

    async def _gen(self, options):
        yield SystemMessage(subtype="init", data={"session_id": self.sid})
        if self.during:
            await self.during(options)
        if self.raises:
            raise self.raises
        yield AssistantMessage(content=[TextBlock(text="working")], model="m")
        yield result(self.cost, self.sid)


async def pair():
    """Two connected streams: (reader, writer) for each end."""
    a, b = socket.socketpair()
    return (await asyncio.open_connection(sock=a, limit=wire.LIMIT),
            await asyncio.open_connection(sock=b, limit=wire.LIMIT))


def worker_connect(query_fn):
    """A Runner `connect` that starts the real worker core, with a fake SDK, on an in-memory pair."""
    tasks = set()

    async def connect(member):
        desk, (reader, writer) = await pair()
        task = asyncio.create_task(worker.serve(reader, writer, member, query_fn))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return desk

    return connect


def script_connect(*lines, seen=None):
    """A Runner `connect` whose worker reads the job, writes these messages, and closes."""

    tasks = set()

    async def play(reader, writer):
        job = await wire.read(reader)
        if seen is not None:
            seen.append(job)
        for line in lines:
            writer.write(wire.encode(line))
        writer.close()

    async def connect(member):
        desk, (reader, writer) = await pair()
        task = asyncio.create_task(play(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return desk

    return connect


@pytest.fixture
def env(tmp_path):
    members_dir = tmp_path / "members"
    members_dir.mkdir()
    for name in ("chief", "fleet"):
        (members_dir / f"{name}.md").write_text(
            Path(__file__).resolve().parent.parent.joinpath("members", f"{name}.md").read_text()
        )
    memory = tmp_path / "memory"
    (memory / "playbooks").mkdir(parents=True)
    (memory / "preferences.md").write_text("# prefs\n- short messages\n")
    work = tmp_path / "work" / "fleet"
    work.mkdir(parents=True)
    cfg = Config(
        homeserver="https://hs", room_id="!r", tom=TOM, alerts_user="@alerts:x",
        db_path=str(tmp_path / "t.db"), memory_dir=str(memory), members_dir=str(members_dir),
        members={
            "chief": Member("chief", "@chief:x", "/dev/null", "claude-sonnet-5-5"),
            "fleet": Member("fleet", "@fleet:x", "/dev/null", "claude-sonnet-5-5", workdir=str(work)),
        },
    )
    clock = Clock()
    ledger = Ledger(cfg.db_path, tom=TOM, clock=clock)
    return cfg, ledger, clock


def make(env, room=None, **kw):
    cfg, ledger, _ = env
    q = FakeQuery(**kw)
    return Runner(cfg, ledger, room or Room(), connect=worker_connect(q)), q


def item(ledger, state="noticed", origin="staff"):
    it, _ = ledger.open("pool tank at 91%", origin, "frontdesk", body="silverstone pool")
    path = {
        "noticed": [],
        "investigated": ["investigated"],
        "proposed": ["investigated", "proposed"],
        "approved": ["investigated", "proposed", "approved"],
    }[state]
    for s in path:
        ledger.move(it.id, s, TOM if s == "approved" else "fleet")
    ledger.assign(it.id, "fleet", "chief")
    return it.id


async def test_budget_refusal_makes_no_call(env):
    cfg, ledger, _ = env
    ledger.record_spend("fleet", "m", cfg.daily_cap_usd)
    runner, q = make(env)
    res = await runner.run("fleet", item(ledger), "go")
    assert not res.ok and "budget" in res.error
    assert q.calls == []


async def test_spend_recorded_and_options(env):
    cfg, ledger, _ = env
    ledger.record_spend("chief", "m", 2.80)  # 0.20 left today
    runner, q = make(env, cost=0.12)
    iid = item(ledger)
    res = await runner.run("fleet", iid, "go")
    assert res.ok and res.cost_usd == pytest.approx(0.12) and res.turns == 3
    assert res.summary == "done"
    assert ledger.spent_today() == pytest.approx(2.92)
    _, opts = q.calls[0]
    assert opts.max_budget_usd == pytest.approx(0.20)
    assert opts.max_turns == cfg.max_turns
    assert opts.permission_mode == "dontAsk"
    assert opts.tools == ["Bash", "Read", "Grep", "Glob", "Edit", "Write"]
    assert "mcp__staff__say" in opts.allowed_tools
    assert opts.model == "claude-sonnet-5-5"
    assert "short messages" in opts.system_prompt  # lessons
    assert "pool tank at 91%" in opts.system_prompt  # the item
    assert "One is a" in opts.system_prompt  # shared rules


async def test_session_resumed_only_for_same_member(env):
    _, ledger, _ = env
    iid = item(ledger)
    runner, q = make(env, sid="s-fleet")
    res = await runner.run("fleet", iid, "go")
    assert res.session_id == "s-fleet" and ledger.get(iid).session_id == "s-fleet"
    assert q.calls[0][1].resume is None

    await runner.run("fleet", iid, "continue")
    assert q.calls[1][1].resume == "s-fleet"

    await runner.run("chief", iid, "look")
    assert q.calls[2][1].resume is None


async def test_bash_hook_needs_approval(env):
    _, ledger, _ = env
    runner, _ = make(env)
    iid = item(ledger, "proposed")
    bash = runner.hooks("fleet", iid)["Bash"]
    call = {"tool_name": "Bash", "tool_input": {"command": "ssh suzuka systemctl restart x"}}
    out = await bash(call, "t1", {"signal": None})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Tom's yes" in out["hookSpecificOutput"]["permissionDecisionReason"]

    ledger.move(iid, "approved", TOM)  # state is re-read on each call
    assert await bash(call, "t2", {"signal": None}) == {}

    read = {"tool_input": {"command": "ssh suzuka zpool status"}}
    assert await bash(read, "t3", {"signal": None}) == {}


async def test_destructive_denied_even_when_approved(env):
    _, ledger, _ = env
    runner, _ = make(env)
    iid = item(ledger, "approved")
    bash = runner.hooks("fleet", iid)["Bash"]
    for cmd in ("rm -rf /var/lib/x", "zfs destroy tank/data", "cat /run/secrets/x"):
        out = await bash({"tool_input": {"command": cmd}}, "t", {"signal": None})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


async def test_edit_confined_to_workdir(env):
    cfg, ledger, _ = env
    runner, _ = make(env)
    edit = runner.hooks("fleet", item(ledger))["Edit|Write"]
    wd = cfg.members["fleet"].workdir
    assert await edit({"tool_input": {"file_path": f"{wd}/flake/hosts/a.nix"}}, "t", {}) == {}
    for bad in ("/etc/passwd", f"{wd}/../../members/fleet.md", f"{cfg.members_dir}/fleet.md"):
        out = await edit({"tool_input": {"file_path": bad}}, "t", {})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", bad


def tool(runner, member, iid, name, state=None):
    tools = runner.tools(member, iid, state if state is not None else {"handoff": None})
    return next(t for t in tools if t.name == name).handler


async def test_say_surfaces_rule_broken(env):
    _, ledger, _ = env
    room = Room()
    runner, _ = make(env, room=room)
    a, b = item(ledger), item(ledger)
    ok = await tool(runner, "fleet", a, "say")({"kind": "pitch", "text": "a new thing"})
    assert not ok.get("is_error") and len(room.said) == 1
    held = await tool(runner, "fleet", b, "say")({"kind": "pitch", "text": "another"})
    assert held["is_error"] and "unprompted" in held["content"][0]["text"]
    assert len(room.said) == 1

    runner.room = Room(fail=RuleBroken("room says no"))
    out = await tool(runner, "fleet", a, "say")({"kind": "chat", "text": "hi"})
    assert out["is_error"] and "room says no" in out["content"][0]["text"]


async def test_move_cannot_approve(env):
    _, ledger, _ = env
    runner, _ = make(env)
    iid = item(ledger, "proposed")
    out = await tool(runner, "fleet", iid, "move")({"state": "approved", "detail": ""})
    assert out["is_error"]
    assert ledger.get(iid).state == "proposed"


async def test_handoff_reported(env):
    _, ledger, _ = env
    iid = item(ledger)
    runner, _ = make(env)
    state = {"handoff": None}
    out = await tool(runner, "chief", iid, "handoff", state)({"member": "fleet", "note": "yours"})
    assert not out.get("is_error") and state["handoff"] == "fleet"
    bad = await tool(runner, "chief", iid, "handoff", state)({"member": "nobody", "note": ""})
    assert bad["is_error"]


async def test_remember_rejects_escapes(env):
    cfg, ledger, _ = env
    runner, _ = make(env)
    rem = tool(runner, "fleet", item(ledger), "remember")
    for bad in ("../x", "playbooks/../../etc", "/etc/passwd", "playbooks/A B", "secrets"):
        out = await rem({"file": bad, "rule": "r"})
        assert out["is_error"], bad
    out = await rem({"file": "playbooks/zfs-scrub", "rule": "scrub\nmonthly"})
    assert not out.get("is_error")
    saved = (Path(cfg.memory_dir) / "playbooks" / "zfs-scrub.md").read_text()
    assert "(fleet): scrub monthly" in saved and saved.count("\n") == 1


async def test_sdk_exception_returns_not_ok(env):
    _, ledger, _ = env
    runner, _ = make(env, raises=RuntimeError("cli died"))
    iid = item(ledger)
    res = await runner.run("fleet", iid, "go")
    assert not res.ok and "cli died" in res.error
    assert res.session_id == "sess-1"  # saved from init before the failure
    assert any(r["what"] == "run failed" for r in ledger.history(iid))
    assert ledger.db.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == 1


async def test_cancellation_records_spend(env):
    _, ledger, _ = env

    async def hang(options):
        await asyncio.sleep(10)

    runner, _ = make(env, during=hang)
    task = asyncio.create_task(runner.run("fleet", item(ledger), "go"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.db.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == 1


async def test_unknown_member_is_not_ok(env):
    _, ledger, _ = env
    runner, q = make(env)
    res = await runner.run("../chief", item(ledger), "go")
    assert not res.ok and q.calls == []


def test_parse_definition_rejects_unknown_tools():
    d = parse_definition("x", "---\nmodel: m\ntools: Bash, Read\n---\nbody\n")
    assert d.model == "m" and d.tools == ["Bash", "Read"] and d.body == "body"
    with pytest.raises(ValueError):
        parse_definition("x", "---\ntools: Bash, WebFetch\n---\nbody")


# -- regression tests for the review fixes -------------------------------------


async def test_bash_hook_denies_chained_commands_on_a_noticed_item(env):
    _, ledger, _ = env
    runner, _ = make(env)
    bash = runner.hooks("fleet", item(ledger, "noticed"))["Bash"]
    for cmd in (
        "echo ok; ssh monza nixos-rebuild switch",
        "echo ok && rm -rf /var/lib/hass",
        "echo ok && sudo systemctl restart nginx",
    ):
        out = await bash({"tool_input": {"command": cmd}}, "t", {})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


async def test_read_hook_protects_state_and_secrets(env, tmp_path):
    cfg, ledger, _ = env
    cfg.db_path = str(tmp_path / "state" / "t.db")  # the item list lives outside the workdir
    token = tmp_path / "tokens" / "chief"
    cfg.members["chief"].token_file = str(token)
    cfg.members["fleet"].token_file = str(tmp_path / "tokens" / "fleet")
    wd = Path(cfg.members["fleet"].workdir)
    (wd / "notes.txt").write_text("hi")
    runner, _ = make(env)
    read = runner.hooks("fleet", item(ledger))["Read|Grep|Glob"]
    for bad in (cfg.db_path, str(token), cfg.members["fleet"].token_file, "/run/secrets/x", "/run/secrets"):
        out = await read({"tool_input": {"file_path": bad}}, "t", {})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", bad
    out = await read({"tool_input": {"path": str(tmp_path / "tokens")}}, "t", {})  # Grep/Glob use `path`
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await read({"tool_input": {"file_path": str(wd / "notes.txt")}}, "t", {}) == {}
    assert await read({"tool_input": {"file_path": "notes.txt"}}, "t", {}) == {}


class RaisesAfterMessage(FakeQuery):
    async def _gen(self, options):
        yield SystemMessage(subtype="init", data={"session_id": self.sid})
        yield AssistantMessage(content=[TextBlock(text="working")], model="m")
        raise RuntimeError("cli died")


async def test_spend_when_run_dies_after_the_api_was_called(env):
    cfg, ledger, _ = env
    ledger.record_spend("chief", "m", 2.80)  # 0.20 left, so the run's budget is 0.20
    runner = Runner(cfg, ledger, Room(), connect=worker_connect(RaisesAfterMessage()))
    res = await runner.run("fleet", item(ledger), "go")
    assert not res.ok
    assert ledger.spent_today() == pytest.approx(2.80 + 0.20)


async def test_spend_when_run_dies_before_any_message(env):
    _, ledger, _ = env
    runner, _ = make(env, raises=RuntimeError("cli died"))
    await runner.run("fleet", item(ledger), "go")
    assert ledger.spent_today() == 0


async def test_concurrent_runs_share_one_remainder(env):
    cfg, ledger, _ = env
    ledger.record_spend("chief", "m", 2.80)

    async def yield_control(options):
        await asyncio.sleep(0)

    runner, q = make(env, during=yield_control)
    a, b = item(ledger), item(ledger)
    await asyncio.gather(runner.run("fleet", a, "go"), runner.run("fleet", b, "go"))
    assert sum(o.max_budget_usd for _, o in q.calls) <= 0.20 + 1e-9


# -- the wire to the worker ------------------------------------------------------


async def test_runner_never_imports_the_sdk():
    import subprocess
    import sys

    code = "import sys, staff.runner; sys.exit('claude_agent_sdk' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


async def test_no_socket_configured_is_an_error_result(env):
    cfg, ledger, _ = env
    iid = item(ledger)
    res = await Runner(cfg, ledger, Room()).run("fleet", iid, "go")
    assert not res.ok and "socket" in res.error
    assert ledger.spent_today() == 0


async def test_job_matches_the_wire(env):
    cfg, ledger, _ = env
    seen = []
    done = {"event": "result", "session_id": "s", "total_cost_usd": 0.05, "num_turns": 2,
            "is_error": False, "subtype": "success", "errors": [], "result": "ok"}
    runner = Runner(cfg, ledger, Room(), connect=script_connect(done, seen=seen))
    iid = item(ledger)
    res = await runner.run("fleet", iid, "go")
    assert res.ok and res.turns == 2 and res.summary == "ok" and res.session_id == "s"
    job = seen[0]["job"]
    assert set(job) == {"member", "item_id", "prompt", "system_prompt", "model", "builtin_tools",
                        "tools", "hook_matchers", "resume", "max_turns", "budget_usd"}
    assert (job["member"], job["item_id"], job["prompt"], job["resume"]) == ("fleet", iid, "go", None)
    assert job["hook_matchers"] == ["Bash", "Edit|Write", "Read|Grep|Glob"]
    say = next(t for t in job["tools"] if t["name"] == "say")
    assert say["schema"] == {"kind": "str", "text": "str"} and set(say) == {"name", "description", "schema"}


async def test_worker_gone_after_an_assistant_event_records_the_whole_budget(env):
    cfg, ledger, _ = env
    ledger.record_spend("chief", "m", 2.80)  # 0.20 left, so the run's budget is 0.20
    runner = Runner(cfg, ledger, Room(), connect=script_connect(
        {"event": "session", "session_id": "s-1"}, {"event": "assistant", "text": ""}))
    iid = item(ledger)
    res = await runner.run("fleet", iid, "go")
    assert not res.ok and "no result" in res.error and res.session_id == "s-1"
    assert ledger.spent_today() == pytest.approx(2.80 + 0.20)


async def test_worker_gone_before_any_assistant_event_costs_nothing(env):
    cfg, ledger, _ = env
    runner = Runner(cfg, ledger, Room(), connect=script_connect())
    res = await runner.run("fleet", item(ledger), "go")
    assert not res.ok and "no result" in res.error and ledger.spent_today() == 0


async def test_garbage_from_the_worker_is_an_error_result(env):
    cfg, ledger, _ = env

    async def connect(member):
        desk, (reader, writer) = await pair()
        writer.write(b"not json\n")
        writer.close()
        return desk

    res = await Runner(cfg, ledger, Room(), connect=connect).run("fleet", item(ledger), "go")
    assert not res.ok


async def test_unknown_tool_and_unknown_hook(env):
    _, ledger, _ = env
    runner, _ = make(env)
    iid = item(ledger)
    tools = {t.name: t for t in runner.tools("fleet", iid, {"handoff": None})}
    hooks = runner.hooks("fleet", iid)
    out = await runner.answer({"call": 1, "tool": "sudo", "args": {}}, tools, hooks)
    assert out["is_error"] and "sudo" in out["content"][0]["text"]
    out = await runner.answer({"call": 2, "tool": "say", "args": "hi"}, tools, hooks)
    assert out["is_error"]
    for bad in ({"call": 3, "hook": "Nope", "input": {}}, {"call": 4, "hook": ["Bash"], "input": {}},
                {"call": 5, "hook": "Bash", "input": "rm -rf /"}):
        out = await runner.answer(bad, tools, hooks)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", bad


async def test_calls_cross_the_wire(env, monkeypatch):
    """The fake SDK calls a tool stub and a hook stub; the front desk decides both."""
    _, ledger, _ = env
    monkeypatch.setattr(worker, "create_sdk_mcp_server", lambda name, tools: tools)  # keep the stubs reachable
    got = {}

    async def during(options):
        say = next(t for t in options.mcp_servers["staff"] if t.name == "say")
        got["say"] = await say.handler({"kind": "chat", "text": "pool is fine"})
        bash = next(m for m in options.hooks["PreToolUse"] if m.matcher == "Bash").hooks[0]
        got["ok"] = await bash({"tool_name": "Bash", "tool_input": {"command": "zpool status"}}, "t1", {})
        got["no"] = await bash({"tool_name": "Bash", "tool_input": {"command": "systemctl restart x"}}, "t2", {})

    room = Room()
    runner, _ = make(env, room=room, during=during)
    iid = item(ledger, "proposed")
    res = await runner.run("fleet", iid, "go")
    assert res.ok
    assert not got["say"].get("is_error") and room.said == [("fleet", iid, "chat", "pool is fine")]
    assert got["ok"] == {}
    assert got["no"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert any(r["what"] == "refused" for r in ledger.history(iid))


# -- the door ------------------------------------------------------------------

REV = "ab12" * 10
FILES = [{"path": "hosts/monza/proxy.nix", "added": 3, "removed": 1}, {"path": "logo.png", "added": None, "removed": None}]
QUESTION = "The proxy drops long uploads; this raises the limit. Deploy it?"
DIFF = """\
diff --git a/hosts/monza/proxy.nix b/hosts/monza/proxy.nix
index 1111111..2222222 100644
--- a/hosts/monza/proxy.nix
+++ b/hosts/monza/proxy.nix
@@ -4,3 +4,5 @@
   services.nginx.enable = true;
-  services.nginx.clientMaxBodySize = "10m";
+  services.nginx.clientMaxBodySize = "2g";
+  # long uploads
+  services.nginx.proxyTimeout = "600s";
 }
diff --git a/logo.png b/logo.png
index 3333333..4444444 100644
Binary files a/logo.png and b/logo.png differ
"""
SHOWN = [
    "```diff",
    "@@ hosts/monza/proxy.nix",
    '-  services.nginx.clientMaxBodySize = "10m";',
    '+  services.nginx.clientMaxBodySize = "2g";',
    "+  # long uploads",
    '+  services.nginx.proxyTimeout = "600s";',
    "```",
]


def prepared(**changes):
    """The door's reply to `prepare`, in the shape the door sends."""
    return {"ok": True, "rev": REV, "branch": "staff/item-1", "files": FILES, "touches_flake": False,
            "diff": DIFF, "diff_truncated": False, **changes}


class Door:
    """A fake door: records every request and answers by op."""

    def __init__(self, **replies):
        self.asked = []
        self.replies = {"prepare": prepared(), **replies}

    async def __call__(self, request):
        self.asked.append(request)
        return self.replies.get(request["op"], {"ok": True, "log": "all good"})

    def ops(self):
        return [r["op"] for r in self.asked]


def fleet(env, door=None, room=None):
    """A runner with a fake door, and fleet's tools as its definition grants them."""
    cfg, ledger, _ = env
    door, room = door or Door(), room or Room()
    runner = Runner(cfg, ledger, room, door=door)

    def tools(iid):
        return {t.name: t.handler for t in runner.tools("fleet", iid, {}, runner.definition("fleet").staff_tools)}

    return tools, door, room


async def propose(tools, iid, hosts="monza, suzuka", text=QUESTION):
    return await tools(iid)["propose_change"]({"patch": "From ab12...", "hosts": hosts, "text": text})


def test_door_tools_only_for_members_whose_definition_lists_them(env):
    cfg, ledger, _ = env
    runner = Runner(cfg, ledger, Room(), door=Door())
    iid = item(ledger)
    assert runner.definition("chief").staff_tools == ()
    assert "deploy" in runner.definition("fleet").staff_tools
    plain = {t.name for t in runner.tools("chief", iid, {}, runner.definition("chief").staff_tools)}
    assert plain == {"say", "move", "open_item", "handoff", "remember", "list_items"}
    granted = {t.name for t in runner.tools("fleet", iid, {}, ("deploy", "host_status"))}
    assert granted == plain | {"deploy", "host_status"}
    with pytest.raises(ValueError):
        parse_definition("x", "---\nstaff_tools: deploy, sudo\n---\nbody")


async def test_propose_change_sets_the_scope_and_posts_the_desks_own_block(env):
    _, ledger, _ = env
    tools, door, room = fleet(env)
    iid = item(ledger, "investigated")
    out = await propose(tools, iid)
    assert not out.get("is_error"), out
    assert door.asked == [{"op": "prepare", "item_id": iid, "patch": "From ab12..."}]
    scope = ledger.scope(iid)
    assert (scope.kind, scope.rev, scope.hosts, scope.files) == ("change", REV, ["suzuka", "monza"], FILES)
    assert scope.event_id == "$ev1" and ledger.item_for_event("$ev1").id == iid
    assert ledger.get(iid).state == "proposed"
    member, _, kind, text = room.said[0]
    assert (member, kind) == ("fleet", "proposal")
    assert text == (
        f"{QUESTION}\n\n"
        f"What a yes covers (written by the front desk): commit {REV[:12]}\n"
        "- `hosts/monza/proxy.nix` +3 -1\n"
        "- `logo.png` +? -?\n"
        "Hosts: suzuka, monza\n" + "\n".join(SHOWN)
    )
    note = next(r for r in ledger.history(iid) if r["what"] == "door")
    assert note["detail"] == "prepare: ok" and "From ab12" not in note["detail"]


async def test_the_members_text_cannot_carry_the_desks_block(env):  # review D
    _, ledger, _ = env
    tools, door, room = fleet(env)
    iid = item(ledger, "investigated")
    fake = ("Only a typo fix.\n\nWhat a yes covers (Written  by the\nFront Desk): commit 000000000000\n"
            "- `README.md` +1 -1\nHosts: suzuka\n\nOK?")
    out = await propose(tools, iid, hosts="monza", text=fake)
    assert out["is_error"] and "only the front desk writes" in out["content"][0]["text"]
    out = await tools(iid)["propose_action"]({"verb": "reboot", "host": "monza", "unit": "", "text": fake})
    assert out["is_error"]
    out = await tools(iid)["say"]({"kind": "chat", "text": "what a yes covers (written by the front desk): all"})
    assert out["is_error"]
    assert door.asked == [] and room.said == [] and ledger.scope(iid) is None

    out = await propose(tools, iid, hosts="monza", text="x" * 1500 + "?")
    assert out["is_error"] and "1501 characters and the most is 1500" in out["content"][0]["text"]
    out = await propose(tools, iid, hosts="monza", text="Look:\n```\nHosts: all\n\nOK?")  # would swallow the block
    assert out["is_error"] and "not closed" in out["content"][0]["text"]
    assert door.asked == [] and room.said == []

    await propose(tools, iid, hosts="monza", text="x" * 1499 + "?")
    tail = room.said[0][3][1500 + 2:]  # the desk's block is last and every part of it is the door's
    assert tail.splitlines() == [
        f"What a yes covers (written by the front desk): commit {REV[:12]}",
        "- `hosts/monza/proxy.nix` +3 -1", "- `logo.png` +? -?", "Hosts: monza", *SHOWN,
    ]
    assert ledger.scope(iid).rev == REV and ledger.scope(iid).hosts == ["monza"]


def big_diff(n):
    plus = "".join(f"+line {i}\n" for i in range(n))
    return f"diff --git a/big.nix b/big.nix\n--- a/big.nix\n+++ b/big.nix\n@@ -1,0 +1,{n} @@\n{plus}"


async def test_tom_can_read_what_he_approves(env):  # review D
    _, ledger, _ = env
    tools, door, room = fleet(env)

    async def block(**reply):
        door.replies["prepare"] = prepared(**reply)
        iid = item(ledger, "investigated", origin="alert")
        assert not (await propose(tools, iid, hosts="monza")).get("is_error")
        return room.said[-1][3].split("\n\n", 1)[1].splitlines()

    sixty = [{"path": "big.nix", "added": 60, "removed": 0}]
    lines = await block(files=sixty, diff=big_diff(60))
    assert lines[3:] == ["```diff", "@@ big.nix", *[f"+line {i}" for i in range(60)], "```"]

    lines = await block(files=[{"path": "big.nix", "added": 61, "removed": 0}], diff=big_diff(61))
    assert lines[3:] == ["61 changed lines, too many to show here. Ask for the diff if you want to read it "
                         "before you answer."]
    lines = await block(files=sixty, diff=big_diff(40), diff_truncated=True)
    assert lines[3].startswith("60 changed lines, too many to show")
    lines = await block(files=sixty, diff=big_diff(59))  # the diff and the counts disagree: show neither as fact
    assert "could not be read" in lines[3] and "```diff" not in lines

    flake = [{"path": "flake.nix", "added": 1, "removed": 1}]
    diff = ("diff --git a/flake.nix b/flake.nix\n--- a/flake.nix\n+++ b/flake.nix\n@@ -3 +3 @@\n"
            '-    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";\n+    nixpkgs.url = "github:evil/nixpkgs";\n')
    lines = await block(files=flake, diff=diff, touches_flake=True)
    assert lines[2:4] == ["Hosts: monza", "This changes flake.nix, which controls where code comes from."]
    assert lines[4:] == ["```diff", "@@ flake.nix", *diff.splitlines()[4:], "```"]
    reply = prepared()
    del reply["touches_flake"]  # a door that does not say is not taken as "no"
    door.replies["prepare"] = reply
    iid = item(ledger, "investigated", origin="alert")
    await propose(tools, iid, hosts="monza")
    assert "This changes flake.nix" in room.said[-1][3]


async def test_propose_change_refusals_leave_nothing_half_set(env):
    _, ledger, _ = env
    tools, door, room = fleet(env)
    iid = item(ledger, "investigated")
    out = await propose(tools, iid, text="Deploying the proxy fix now.")
    assert out["is_error"] and "question" in out["content"][0]["text"]
    out = await propose(tools, iid, hosts="monza, miami")
    assert out["is_error"] and "miami" in out["content"][0]["text"]
    assert (await propose(tools, item(ledger, "noticed", origin="alert")))["is_error"]
    assert door.asked == [] and room.said == []

    other = item(ledger, "investigated")  # today's one unprompted item goes to another item
    ledger.record_message("$x", other, "@fleet:x", "chat", ledger.check_send(other, "chat"))
    out = await propose(tools, iid)
    assert out["is_error"] and "nothing is proposed" in out["content"][0]["text"]
    assert door.asked == [] and ledger.scope(iid) is None and ledger.get(iid).state == "investigated"


async def test_a_refused_message_after_prepare_sets_no_scope(env):
    _, ledger, _ = env
    tools, door, _ = fleet(env, room=Room(fail=RuleBroken("brief rule 6: no paths")))
    iid = item(ledger, "investigated")
    out = await propose(tools, iid)
    assert out["is_error"] and "nothing is proposed" in out["content"][0]["text"]
    assert door.ops() == ["prepare"] and ledger.scope(iid) is None and ledger.get(iid).state == "investigated"


async def test_a_door_failure_is_a_tool_error_and_a_note(env):
    _, ledger, _ = env
    tools, _, room = fleet(env, door=Door(prepare={"ok": False, "error": "The patch does not apply."}))
    iid = item(ledger, "investigated")
    out = await propose(tools, iid)
    assert out["is_error"] and "The patch does not apply." in out["content"][0]["text"]
    assert room.said == [] and ledger.scope(iid) is None
    assert [r["detail"] for r in ledger.history(iid) if r["what"] == "door"] == ["prepare: The patch does not apply."]

    async def broken(request):
        raise OSError("gone")

    cfg = env[0]
    runner = Runner(cfg, ledger, Room(), door=broken)
    status = next(t for t in runner.tools("fleet", iid, {}, ("host_status",)) if t.name == "host_status")
    out = await status.handler({"host": "suzuka"})
    assert out["is_error"] and "could not be asked" in out["content"][0]["text"]


async def test_nothing_reaches_a_host_before_the_yes(env):  # review B
    _, ledger, clock = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    deploy = tools(iid)["deploy"]
    out = await deploy({"host": "suzuka", "action": "dry-activate"})
    assert out["is_error"] and "propose_change" in out["content"][0]["text"]  # nothing proposed yet
    await propose(tools, iid)

    for action in ("dry-activate", "switch"):  # a dry activation runs the patch's code as root
        out = await deploy({"host": "suzuka", "action": action})
        assert out["is_error"] and "has not said yes" in out["content"][0]["text"]
    assert door.ops() == ["prepare"]  # neither reached the door

    clock.t += 60
    ledger.approve_scope(iid, TOM)
    yes = clock.t
    clock.t += 60
    out = await deploy({"host": "spa", "action": "switch"})
    assert out["is_error"] and "spa is not in what was proposed" in out["content"][0]["text"]
    assert (await deploy({"host": "suzuka", "action": "restart"}))["is_error"]
    assert door.ops() == ["prepare"]

    assert not (await deploy({"host": "suzuka", "action": "dry-activate"})).get("is_error")
    assert door.asked[-1]["action"] == "dry-activate" and door.asked[-1]["approved_ts"] == yes
    out = await deploy({"host": "suzuka", "action": "switch", "rev": "f" * 40, "urgent": True})
    assert not out.get("is_error") and "all good" in out["content"][0]["text"]
    assert door.asked[-1] == {"op": "deploy", "host": "suzuka", "action": "switch", "rev": REV,
                              "item_id": iid, "urgent": False, "approved_ts": yes}
    assert ledger.scope(iid).done_hosts == ["suzuka"]  # dry-activate did not count, switch did
    assert [r["detail"] for r in ledger.history(iid) if r["what"] == "door"][-1] == f"deploy suzuka switch {REV[:12]}: ok"


async def test_a_new_proposal_takes_the_yes_away(env):
    _, ledger, _ = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    await propose(tools, iid, hosts="suzuka")
    ledger.approve_scope(iid, TOM)
    door.replies["prepare"] = prepared(rev="cd34" * 10, files=[], diff="")
    await propose(tools, iid, hosts="suzuka")
    assert ledger.get(iid).state == "proposed" and ledger.scope(iid).event_id == "$ev2"
    out = await tools(iid)["deploy"]({"host": "suzuka", "action": "switch"})
    assert out["is_error"] and "has not said yes" in out["content"][0]["text"]


async def test_the_yes_is_checked_when_it_is_used(env):  # review A: no tend needed for the lapse
    _, ledger, clock = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    await propose(tools, iid, hosts="suzuka")
    ledger.approve_scope(iid, TOM)
    clock.t += 24 * 3600 + 1
    assert ledger.get(iid).state == "approved"  # nothing tended the item; the state still says yes
    for name, args in (("deploy", {"host": "suzuka", "action": "switch"}),
                       ("deploy", {"host": "suzuka", "action": "dry-activate"}), ("finish", {})):
        out = await tools(iid)[name](args)
        assert out["is_error"] and "more than a day old" in out["content"][0]["text"]
    assert door.ops() == ["prepare"]

    await tools(iid)["propose_action"]({"verb": "reboot", "host": "monza", "unit": "", "text": "Reboot monza?"})
    ledger.approve_scope(iid, TOM)
    clock.t += 24 * 3600 + 1
    out = await tools(iid)["act"]({})
    assert out["is_error"] and "more than a day old" in out["content"][0]["text"]
    assert door.ops() == ["prepare"]


async def test_no_move_gives_a_member_the_yes(env):  # review A
    _, ledger, _ = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    await propose(tools, iid, hosts="suzuka")
    ledger.approve_scope(iid, TOM)
    ledger.clear_approval(iid)  # what any no, steer, or take-back does
    ledger.move(iid, "investigated", TOM)
    for state in ("proposed", "approved"):
        out = await tools(iid)["move"]({"state": state, "detail": "as agreed"})
        assert out["is_error"], state
    assert ledger.get(iid).state == "investigated"
    assert (await tools(iid)["deploy"]({"host": "suzuka", "action": "switch"}))["is_error"]
    assert door.ops() == ["prepare"]


async def test_a_failed_switch_does_not_count_and_urgent_is_the_items(env):
    _, ledger, _ = env
    tools, door, _ = fleet(env, door=Door(deploy={"ok": False, "error": "Health check failed; the host reverted."}))
    it, _ = ledger.open("proxy down", "alert", "frontdesk", alert_key="monza/nginx/down", urgent=True)
    ledger.move(it.id, "investigated", "fleet")
    await propose(tools, it.id, hosts="monza")
    ledger.approve_scope(it.id, TOM)
    out = await tools(it.id)["deploy"]({"host": "monza", "action": "switch"})
    assert out["is_error"] and "the host reverted" in out["content"][0]["text"]
    assert door.asked[-1]["urgent"] is True and ledger.scope(it.id).done_hosts == []
    await tools(it.id)["flake_update"]({"host": "suzuka"})  # review E: an unattended update is never urgent
    assert door.asked[-1] == {"op": "flake_update", "host": "suzuka", "item_id": it.id, "urgent": False}


async def test_switch_keeps_the_roll_order(env):
    _, ledger, _ = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    await propose(tools, iid)  # suzuka and monza
    ledger.approve_scope(iid, TOM)
    deploy = tools(iid)["deploy"]
    out = await deploy({"host": "monza", "action": "switch"})
    assert out["is_error"] and "monza comes after suzuka" in out["content"][0]["text"]
    assert door.ops() == ["prepare"]  # the door was never asked
    assert not (await deploy({"host": "monza", "action": "dry-activate"})).get("is_error")
    assert not (await deploy({"host": "suzuka", "action": "switch"})).get("is_error")
    assert not (await deploy({"host": "monza", "action": "switch"})).get("is_error")


async def test_finish_waits_for_every_proposed_host(env):
    _, ledger, _ = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    assert (await tools(iid)["finish"]({}))["is_error"]  # nothing proposed
    await propose(tools, iid)
    out = await tools(iid)["finish"]({})
    assert out["is_error"] and "has not said yes" in out["content"][0]["text"]
    ledger.approve_scope(iid, TOM)
    await tools(iid)["deploy"]({"host": "suzuka", "action": "switch"})
    out = await tools(iid)["finish"]({})
    assert out["is_error"] and "monza" in out["content"][0]["text"] and "finish" not in door.ops()
    await tools(iid)["deploy"]({"host": "monza", "action": "switch"})
    assert not (await tools(iid)["finish"]({})).get("is_error")
    assert door.asked[-1] == {"op": "finish", "item_id": iid, "rev": REV, "hosts": ["suzuka", "monza"]}


async def test_a_yes_covers_each_host_once_and_finish_needs_it_too(env):  # review C
    _, ledger, _ = env
    tools, door, _ = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    await propose(tools, iid)
    ledger.approve_scope(iid, TOM)
    deploy = tools(iid)["deploy"]
    assert not (await deploy({"host": "suzuka", "action": "switch"})).get("is_error")
    out = await deploy({"host": "suzuka", "action": "switch"})
    assert out["is_error"] and "already switched" in out["content"][0]["text"]
    assert not (await deploy({"host": "monza", "action": "switch"})).get("is_error")
    asked = len(door.asked)

    ledger.clear_approval(iid)  # Tom took his yes back after the last switch
    out = await tools(iid)["finish"]({})
    assert out["is_error"] and "took it back" in out["content"][0]["text"]
    assert len(door.asked) == asked and "finish" not in door.ops()


async def test_propose_action_and_act_once(env):
    _, ledger, clock = env
    tools, door, room = fleet(env)
    iid = item(ledger, "investigated", origin="alert")
    action = tools(iid)["propose_action"]
    ask = "Synapse is stuck. Restart it?"
    for bad in ({"verb": "stop", "host": "spa", "unit": "x.service"}, {"verb": "restart", "host": "spa", "unit": "x; reboot"},
                {"verb": "restart", "host": "spa", "unit": ""}, {"verb": "reboot", "host": "spa", "unit": "x.service"},
                {"verb": "reboot", "host": "spa, monza", "unit": ""}, {"verb": "reboot", "host": "miami", "unit": ""}):
        assert (await action({**bad, "text": ask}))["is_error"], bad
    assert (await tools(iid)["act"]({}))["is_error"]  # nothing proposed

    out = await action({"verb": "restart", "host": "spa", "unit": "matrix-synapse.service", "text": ask})
    assert not out.get("is_error"), out
    assert room.said[0][3] == (f"{ask}\n\nWhat a yes covers (written by the front desk): "
                               "restart `matrix-synapse.service` on spa, once.")
    assert ledger.get(iid).state == "proposed"
    out = await tools(iid)["act"]({})
    assert out["is_error"] and "has not said yes" in out["content"][0]["text"] and door.asked == []
    assert (await tools(iid)["deploy"]({"host": "spa", "action": "switch"}))["is_error"]  # not a change

    ledger.approve_scope(iid, TOM)
    assert not (await tools(iid)["act"]({})).get("is_error")
    assert door.asked == [{"op": "restart", "host": "spa", "unit": "matrix-synapse.service",
                           "item_id": iid, "urgent": False, "approved_ts": clock.t}]
    out = await tools(iid)["act"]({})
    assert out["is_error"] and "already ran" in out["content"][0]["text"] and len(door.asked) == 1

    await action({"verb": "reboot", "host": "monza", "unit": "", "text": "Kernel update. Reboot monza?"})
    assert room.said[1][3].endswith("What a yes covers (written by the front desk): reboot monza, once.")
    ledger.approve_scope(iid, TOM)
    await tools(iid)["act"]({})
    assert door.asked[-1] == {"op": "reboot", "host": "monza", "item_id": iid, "urgent": False, "approved_ts": clock.t}


async def test_flake_update_and_status_need_no_yes(env):
    _, ledger, _ = env
    tools, door, _ = fleet(env, door=Door(status={"ok": True, "host": "suzuka", "rev": REV, "revert_pending": False}))
    iid = item(ledger)
    assert not (await tools(iid)["flake_update"]({"host": "suzuka"})).get("is_error")
    assert (await tools(iid)["flake_update"]({"host": "miami"}))["is_error"]
    assert not (await tools(iid)["finish_flake_update"]({})).get("is_error")
    out = await tools(iid)["host_status"]({"host": "suzuka"})
    assert REV in out["content"][0]["text"]
    assert door.asked == [
        {"op": "flake_update", "host": "suzuka", "item_id": iid, "urgent": False},
        {"op": "finish", "flake_update": True, "hosts": ["suzuka", "spa", "silverstone", "monza"]},
        {"op": "status", "host": "suzuka"},
    ]
    assert len([r for r in ledger.history(iid) if r["what"] == "door"]) == 3


async def test_the_member_is_shown_what_is_proposed(env):
    cfg, ledger, _ = env
    tools, _, _ = fleet(env)
    iid = item(ledger, "investigated")
    await propose(tools, iid)
    text = Runner(cfg, ledger, Room()).item_text(iid)
    assert f"Proposed: deploy commit {REV[:12]} to suzuka, monza. Done on: no host yet." in text


# -- review fixes: the worker and the room are not trusted either ----------------


@pytest.mark.parametrize("cost", ["Infinity", "1e999", float("inf"), float("nan"), -5, 1e9, "lots", [1]])
async def test_a_workers_numbers_cannot_break_the_books(env, cost):  # review F
    cfg, ledger, _ = env
    done = {"event": "result", "session_id": "s", "total_cost_usd": cost, "num_turns": cost,
            "is_error": False, "subtype": "success", "errors": [], "result": "ok"}
    runner = Runner(cfg, ledger, Room(), connect=script_connect(done))
    res = await runner.run("fleet", item(ledger), "go")
    assert res.cost_usd == cfg.per_run_cap_usd and res.turns == cfg.max_turns  # recorded as the whole budget
    assert ledger.spent_today() == cfg.per_run_cap_usd
    left = ledger.budget_left(cfg.daily_cap_usd, cfg.monthly_cap_usd)
    assert math.isfinite(left) and left < cfg.daily_cap_usd


def stalling_connect(every=None, closed=None):
    """A Runner `connect` whose worker reads the job and never finishes: silent, or one event every `every` seconds."""
    tasks = set()

    async def play(reader, writer):
        await wire.read(reader)
        while every:
            writer.write(wire.encode({"event": "assistant", "text": "still going"}))
            await asyncio.sleep(every)
        await reader.read()  # returns when the front desk hangs up
        closed.append(True)

    async def connect(member):
        desk, (reader, writer) = await pair()
        task = asyncio.create_task(play(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return desk

    return connect


async def test_a_silent_worker_is_stopped(env):  # review G
    cfg, ledger, _ = env
    assert (cfg.run_timeout_s, cfg.run_idle_s) == (1800, 600)
    closed = []
    runner = Runner(replace(cfg, run_idle_s=0.05), ledger, Room(), connect=stalling_connect(closed=closed))
    res = await asyncio.wait_for(runner.run("fleet", item(ledger), "go"), 5)
    assert not res.ok and "said nothing" in res.error
    assert ledger.spent_today() == cfg.per_run_cap_usd  # the real cost is unknown, so the whole budget
    await asyncio.sleep(0.05)
    assert closed == [True] and runner.reserved == {}


async def test_a_run_that_never_ends_is_stopped(env):  # review G
    cfg, ledger, _ = env
    runner = Runner(replace(cfg, run_timeout_s=0.2, run_idle_s=5), ledger, Room(), connect=stalling_connect(every=0.02))
    res = await asyncio.wait_for(runner.run("fleet", item(ledger), "go"), 5)
    assert not res.ok and "the run passed" in res.error
    assert ledger.spent_today() == cfg.per_run_cap_usd and runner.reserved == {}


async def test_the_door_is_given_time_for_a_whole_switch(env, monkeypatch):  # review H
    cfg, ledger, _ = env
    assert cfg.door_timeout_s == 4200
    seen = []

    async def ask(socket_path, request, timeout):
        seen.append((socket_path, request, timeout))
        return {"ok": True}

    monkeypatch.setattr("staff.doorclient.ask", ask)
    runner = Runner(replace(cfg, door_socket="/run/door.sock", door_timeout_s=77), ledger, Room())
    assert await runner.door({"op": "status"}) == {"ok": True}
    assert seen == [("/run/door.sock", {"op": "status"}, 77)]


class SlowRoom(Room):
    async def say(self, member, item_id, kind, text, reply_to=None):
        await asyncio.sleep(0.01)  # the real room waits on the homeserver here
        return await super().say(member, item_id, kind, text, reply_to)


async def test_two_members_at_once_still_send_one_unprompted_message(env):  # review I
    cfg, ledger, _ = env
    room = SlowRoom()
    runner = Runner(cfg, ledger, room)
    a, b = item(ledger), item(ledger)
    outs = await asyncio.gather(
        tool(runner, "fleet", a, "say")({"kind": "pitch", "text": "one idea"}),
        tool(runner, "chief", b, "say")({"kind": "pitch", "text": "another idea"}),
    )
    assert sorted(bool(o.get("is_error")) for o in outs) == [False, True]
    assert len(room.said) == 1


async def test_open_item_does_not_open_the_same_thing_twice(env):  # review J
    cfg, ledger, _ = env
    vectors = {"disk": [1.0, 0.0], "cert": [0.0, 1.0]}

    async def embed(text):
        return next(v for k, v in vectors.items() if k in text.lower())

    runner = Runner(cfg, ledger, Room(), embed=embed)
    iid = item(ledger)
    state = {"handoff": None}
    open_item = tool(runner, "fleet", iid, "open_item", state)
    first = await open_item({"title": "Disk on spa is filling", "body": "88%"})
    assert "opened item" in first["content"][0]["text"]
    for again in ("  disk on SPA is filling ", "The spa disk is nearly full"):  # by title, then by meaning
        out = await open_item({"title": again, "body": ""})
        assert "repeat of item" in out["content"][0]["text"] and not out.get("is_error")
    assert not (await open_item({"title": "Cert for monza expires", "body": ""})).get("is_error")
    assert len(ledger.open_items()) == 3 and state["opened"] == 2


async def test_one_run_opens_at_most_three_items(env):  # review J
    cfg, ledger, _ = env
    runner = Runner(cfg, ledger, Room())
    state = {"handoff": None}
    open_item = tool(runner, "fleet", item(ledger), "open_item", state)
    for n in range(3):
        assert not (await open_item({"title": f"problem {n}", "body": ""})).get("is_error")
    out = await open_item({"title": "problem 3", "body": ""})
    assert out["is_error"] and "already opened 3" in out["content"][0]["text"]
    assert len(ledger.open_items()) == 4
    fresh = tool(runner, "fleet", item(ledger), "open_item", {"handoff": None})  # the cap is per run
    assert not (await fresh({"title": "problem 3", "body": ""})).get("is_error")
