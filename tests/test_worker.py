import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from staff import wire, worker

JOB = {
    "member": "fleet", "item_id": 7, "prompt": "go", "system_prompt": "you are fleet",
    "model": "claude-sonnet-5-5", "builtin_tools": ["Bash", "Read"],
    "tools": [{"name": "say", "description": "post", "schema": {"kind": "str", "text": "str"}}],
    "hook_matchers": ["Bash", "Edit|Write"], "resume": "s-old", "max_turns": 40, "budget_usd": 0.75,
}
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": "Refused: no."}}


def result(sid="sess-1"):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=3, session_id=sid, total_cost_usd=0.12, result="done")


async def start(query_fn, member="fleet", job=JOB):
    """Start the worker core on an in-memory pair, send the job, and return the front desk's end."""
    a, b = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=a, limit=wire.LIMIT)
    wr, ww = await asyncio.open_connection(sock=b, limit=wire.LIMIT)
    task = asyncio.create_task(worker.serve(wr, ww, member, query_fn))
    await wire.send(writer, {"job": job})
    return reader, writer, task


async def rest(reader):
    out = []
    while (msg := await wire.read(reader)) is not None:
        out.append(msg)
    return out


def stubs(monkeypatch):
    """Keep the tool stubs reachable from the fake SDK: options.mcp_servers['staff'] is their list."""
    monkeypatch.setattr(worker, "create_sdk_mcp_server", lambda name, tools: tools)


def bash_hook(options):
    return next(m for m in options.hooks["PreToolUse"] if m.matcher == "Bash").hooks[0]


async def test_job_for_another_member_is_refused():
    called = []

    def query(*, prompt, options):
        called.append(prompt)

    for member in ("chief", ""):
        reader, _, task = await start(query, member=member)
        events = await rest(reader)
        await task
        assert [e["event"] for e in events] == ["error"] and "fleet" in events[0]["error"]
    assert called == []


async def test_not_a_job_is_refused():
    a, b = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=a)
    task = asyncio.create_task(worker.serve(*await asyncio.open_connection(sock=b), "fleet", None))
    writer.write(b"hello\n")
    assert [e["event"] for e in await rest(reader)] == ["error"]
    await task


async def test_options_and_events():
    seen = []

    async def query(*, prompt, options):
        seen.append((prompt, options))
        yield SystemMessage(subtype="init", data={"session_id": "sess-1"})
        yield AssistantMessage(content=[TextBlock(text="working")], model="m")
        yield result()

    reader, _, task = await start(query)
    events = await rest(reader)
    await task
    assert events == [
        {"event": "session", "session_id": "sess-1"},
        {"event": "assistant", "text": "working"},
        {"event": "result", "session_id": "sess-1", "total_cost_usd": 0.12, "num_turns": 3,
         "is_error": False, "subtype": "success", "errors": [], "result": "done"},
    ]
    prompt, opts = seen[0]
    assert prompt == "go" and opts.system_prompt == "you are fleet" and opts.model == "claude-sonnet-5-5"
    assert opts.permission_mode == "dontAsk" and opts.setting_sources == [] and opts.strict_mcp_config
    assert opts.tools == ["Bash", "Read"]
    assert opts.allowed_tools == ["Bash", "Read", "mcp__staff__say"]
    assert (opts.resume, opts.max_turns, opts.max_budget_usd, opts.cwd) == ("s-old", 40, 0.75, None)
    assert [m.matcher for m in opts.hooks["PreToolUse"]] == ["Bash", "Edit|Write"]
    assert all(m.on_failure == "block" for m in opts.hooks["PreToolUse"])
    assert opts.mcp_servers["staff"]["type"] == "sdk"  # the real SDK accepted the stubs


async def test_tool_call_round_trip(monkeypatch):
    stubs(monkeypatch)
    got = []

    async def query(*, prompt, options):
        say = options.mcp_servers["staff"][0]
        assert say.name == "say" and say.input_schema == {"kind": str, "text": str}
        got.append(await say.handler({"kind": "chat", "text": "hi"}))
        yield result()

    reader, writer, task = await start(query)
    call = await wire.read(reader)
    assert call == {"call": 1, "tool": "say", "args": {"kind": "chat", "text": "hi"}}
    answer = {"content": [{"type": "text", "text": "sent (chat)"}]}
    await wire.send(writer, {"reply": 1, "result": answer})
    assert [e["event"] for e in await rest(reader)] == ["result"]
    await task
    assert got == [answer]


async def test_hook_round_trip_allow_and_deny():
    got = []

    async def query(*, prompt, options):
        hook = bash_hook(options)
        got.append(await hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t1", {"signal": None}))
        got.append(await hook({"tool_name": "Bash", "tool_input": {"command": "reboot"}}, "t2", {"signal": None}))
        yield result()

    reader, writer, task = await start(query)
    first = await wire.read(reader)
    assert first == {"call": 1, "hook": "Bash", "input": {"tool_name": "Bash", "tool_input": {"command": "ls"}}}
    await wire.send(writer, {"reply": 1, "result": {}})
    second = await wire.read(reader)
    assert second["call"] == 2 and second["input"]["tool_input"]["command"] == "reboot"
    await wire.send(writer, {"reply": 2, "result": DENY})
    await rest(reader)
    await task
    assert got == [{}, DENY]


async def test_hook_denies_when_the_connection_drops(monkeypatch):
    stubs(monkeypatch)
    got = []

    async def query(*, prompt, options):
        hook = bash_hook(options)
        got.append(await hook({"tool_input": {"command": "reboot"}}, "t1", {}))  # waiting when it drops
        got.append(await hook({"tool_input": {"command": "reboot"}}, "t2", {}))  # asked after it dropped
        got.append(await options.mcp_servers["staff"][0].handler({"kind": "chat", "text": "hi"}))
        await asyncio.sleep(10)  # the run is stopped; it does not carry on without the front desk
        got.append("still running")
        yield result()

    reader, writer, task = await start(query)
    assert (await wire.read(reader))["call"] == 1
    writer.close()
    await asyncio.wait_for(task, 2)
    assert len(got) == 3
    assert all(g["hookSpecificOutput"]["permissionDecision"] == "deny" for g in got[:2])
    assert got[2]["is_error"]


async def test_hook_denies_when_the_answer_is_late_or_wrong(monkeypatch):
    monkeypatch.setattr(worker, "HOOK_WAIT_S", 0.05)
    got = []

    async def query(*, prompt, options):
        hook = bash_hook(options)
        got.append(await hook({"tool_input": {"command": "reboot"}}, "t1", {}))  # never answered
        got.append(await hook({"tool_input": {"command": "reboot"}}, "t2", {}))  # answered with a non-object
        yield result()

    reader, writer, task = await start(query)
    assert (await wire.read(reader))["call"] == 1
    assert (await wire.read(reader))["call"] == 2
    await wire.send(writer, {"reply": 2, "result": "allow"})
    await rest(reader)
    await task
    assert [g["hookSpecificOutput"]["permissionDecision"] for g in got] == ["deny", "deny"]


async def test_sdk_failure_is_an_error_event():
    async def query(*, prompt, options):
        yield AssistantMessage(content=[], model="m")
        raise RuntimeError("cli died")

    reader, _, task = await start(query)
    events = await rest(reader)
    await task
    assert events == [{"event": "assistant", "text": ""}, {"event": "error", "error": "RuntimeError: cli died"}]


FAKE_MAIN = """
import os, sys
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from staff import worker

async def query(*, prompt, options):
    print("noise from a library", flush=True)
    os.write(1, b"raw noise on fd 1\\n")
    yield AssistantMessage(content=[TextBlock(text="hi")], model="m")
    yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                        num_turns=1, session_id="s", total_cost_usd=0.01, result="done")

worker.main(query)
"""


def run_main(job, member="fleet", stderr=subprocess.PIPE):
    """Run the real entry point the way systemd does: one socket as stdin and stdout."""
    root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "STAFF_MEMBER": member, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    ours, theirs = socket.socketpair()
    proc = subprocess.Popen([sys.executable, "-c", FAKE_MAIN], stdin=theirs, stdout=theirs,
                            stderr=stderr or theirs, env=env, cwd=root)
    theirs.close()
    ours.settimeout(60)
    ours.sendall(wire.encode({"job": job}))
    data = b""
    while chunk := ours.recv(65536):
        data += chunk
    ours.close()
    err = proc.communicate(timeout=60)[1]
    return data, err


def test_only_protocol_lines_reach_the_connection():
    data, err = run_main(JOB)
    events = [wire.decode(line) for line in data.splitlines()]
    assert [e["event"] for e in events] == ["assistant", "result"]
    assert b"noise from a library" in err and b"raw noise on fd 1" in err


def test_stderr_on_the_connection_is_silenced():
    data, _ = run_main(JOB, stderr=None)  # systemd's default: stderr inherits the socket
    assert [wire.decode(line)["event"] for line in data.splitlines()] == ["assistant", "result"]


def test_main_refuses_a_job_for_another_member():
    data, _ = run_main(JOB, member="chief")
    events = [wire.decode(line) for line in data.splitlines()]
    assert [e["event"] for e in events] == ["error"]
