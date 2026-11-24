"""Wake one staff member to work one item.

A member is a definition, never a process: members/<name>.md holds a small
frontmatter (optional model override, allowed built-in tools, door tools) and a body
that is its system prompt. A run is one Agent SDK query, made by a worker
process running as the member's own OS user (staff/worker.py). The front
desk sends the worker one job over the member's socket and then answers its
calls (ARCHITECTURE.md, "Wire 1"): the member acts on the world only through
the tools defined here and a few built-in tools, and every Bash, Edit, Write,
and read call passes a PreToolUse hook that applies policy.py with the
item's current state read fresh from the ledger. Nothing here imports the SDK.

Nothing a member holds can change a host. Deploys, restarts, and reboots are
tools here that check the item's scope and Tom's yes on that scope, then ask the door
(ARCHITECTURE.md, "Wire 3" and "What a yes covers").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import doorclient, policy, wire
from .config import Config
from .ledger import Ledger, RuleBroken

BUILTIN_TOOLS = ("Bash", "Read", "Grep", "Glob", "Edit", "Write")
DEFAULT_TOOLS = ("Bash", "Read", "Grep", "Glob")
SAY_KINDS = ("chat", "question", "pitch", "proposal", "result")
# Tools that reach the door. A member has only the ones its definition lists under `staff_tools`.
DOOR_TOOLS = ("propose_change", "propose_action", "deploy", "act", "flake_update", "finish",
              "finish_flake_update", "host_status")
MAY_PROPOSE = ("investigated", "proposed", "approved")
SLOW_TOOLS = ("deploy", "act", "flake_update")  # they wait on a host, so their time is not the run's
DESK_MARK = "written by the front desk"  # heads the block Tom approves; no member text may carry it
MAX_PROPOSAL = 1500  # characters of member text above the desk's block
MAX_SHOWN = 60  # changed lines the desk prints in a proposal
MAX_OPENED = 3  # items one run may open
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_REV = re.compile(r"^[0-9a-f]{40}$")
_UNIT = re.compile(r"^[A-Za-z0-9@_.:-]+\.(service|timer|socket)$")
MEMORY_FILES = ("preferences.md", "facts.md")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
INSTEAD = "Propose it on the item and wait for Tom's yes."

RULES = """\
## Rules for every member (from Tom's brief; enforced in part by code)

1. You were woken by open work, never by the clock. Work this item; do not invent reports.
2. An item is done when the world changed and you checked it. A message is never the finish line.
   Move to "verified" only with what you checked and what it showed.
3. A problem is said once. Never re-announce it. If you notice a separate problem, open_item it.
4. Gumption before asking: search, read, inspect, try. Ask with a best guess to confirm.
   A bare open question is allowed only after real effort, and it shows what you tried.
5. Pitch, then build. Investigation is free. Anything beyond a small fix is pitched first:
   what it is, how Tom would use it, what changes for him day to day, then yes or no.
6. Everything is readable in the room: content inline, never a file path on a server.
7. Tom answers with a thumbs-up, a thumbs-down, or a short reply. End every message that needs a
   decision with one clear yes/no question.
8. Ask "which ones?" when a request is ambiguous. Say so when a task has become pointless.
9. At most one unprompted item a day, and only when it is useful or novel to Tom. One is a
   ceiling, never a quota. The first message of any kind on an item you opened yourself counts; items
   Tom or an alert opened never do. If
   say() refuses a message for today, stop; the item is picked up again tomorrow.

Autonomy: you can read everything and change nothing yourself. Investigate freely: logs, state,
research. Every change to a host goes through the door, by tools, and only members whose work
needs them have those tools. Make the fix as commits in your work folder, run `git format-patch`,
and give the patch to propose_change() with the hosts and your message; the front desk adds the
changed files and lines itself. Wait for Tom's yes: only he can approve, by answering that
proposal message, and his yes covers exactly that commit on those hosts, for one day. A new
proposal replaces the old one and needs a new yes; so does anything he says other than yes.
Nothing reaches a host before his yes except host_status() and flake_update(). Then deploy()
with dry-activate first, switch suzuka first and monza last, verify each host, and finish() to
put the commit on main. A restart or a reboot is proposed with propose_action() and run once
with act(). Flake updates need no yes: flake_update(), suzuka first. Nothing changes between
midnight and 7 a.m. unless the item is urgent. A refusal from the door is final for now,
not something to route around: tell Tom what it said. Permissions, secrets, the thermostat, and
these rules are Tom's alone; you can change your behavior, never your permissions.

Tools: say() is the only way to talk to Tom; your final text is never posted. move() changes the
item's state. handoff() gives the item to the member who owns the area. remember() saves a lesson;
after a correction from Tom, save the rule and then say() the exact rule you saved so he can veto it.
"""


@dataclass
class RunResult:
    ok: bool
    session_id: str | None
    cost_usd: float
    turns: int
    summary: str  # the member's final text, for the log only, never posted
    handoff: str | None  # member name the item was handed to during the run
    error: str = ""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, str]  # argument name -> "str" | "int" | "float" | "bool"
    handler: Callable[[dict], Awaitable[dict[str, Any]]]


@dataclass
class Definition:
    name: str
    model: str | None
    tools: list[str]
    body: str
    staff_tools: tuple[str, ...] = ()  # door tools this member is given


def parse_definition(name: str, text: str) -> Definition:
    """Parse members/<name>.md: `---` frontmatter of `key: value` lines, then the body."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("\n---")
        if sep:
            body = rest.split("\n", 1)[1] if "\n" in rest else ""
            for line in head.splitlines():
                key, colon, value = line.partition(":")
                if colon:
                    meta[key.strip().lower()] = value.strip()
    tools = _listed(meta.get("tools", "")) or list(DEFAULT_TOOLS)
    unknown = [t for t in tools if t not in BUILTIN_TOOLS]
    if unknown:
        raise ValueError(f"member {name}: tools not allowed for members: {', '.join(unknown)}")
    staff_tools = tuple(_listed(meta.get("staff_tools", "")))
    unknown = [t for t in staff_tools if t not in DOOR_TOOLS]
    if unknown:
        raise ValueError(f"member {name}: no such staff tools: {', '.join(unknown)}")
    return Definition(name=name, model=meta.get("model") or None, tools=tools, body=body.strip(),
                      staff_tools=staff_tools)


def _listed(raw: str) -> list[str]:
    return [t.strip().strip("'\"") for t in raw.strip("[]").split(",") if t.strip()]


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _sane(value, top: float) -> float | None:
    """A worker's number, if it is a finite one from 0 to `top`."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) and 0 <= n <= top else None


def _result(msg: dict, budget: float, max_turns: int) -> dict[str, Any]:
    """A worker's `result` event with every field coerced to the type run() relies on.
    The worker is not trusted with the books: a cost that is not a number, is
    negative, or is far over the run's budget is recorded as the whole budget."""
    cost, turns = _sane(msg.get("total_cost_usd"), 2 * budget), _sane(msg.get("num_turns"), 2 * max_turns)
    return {
        "session_id": str(msg.get("session_id") or ""),
        "cost": budget if cost is None else cost,
        "turns": max_turns if turns is None else int(turns),
        "is_error": bool(msg.get("is_error")),
        "subtype": str(msg.get("subtype") or ""),
        "errors": [str(e) for e in msg.get("errors") or []],
        "text": str(msg.get("result") or ""),
    }


def _scope_line(scope) -> str:
    """One line saying exactly what the scope lets run, and where."""
    hosts = ", ".join(scope.hosts)
    if scope.kind == "change":
        return f"deploy commit {scope.rev[:12]} to {hosts}"
    if scope.kind == "restart":
        return f"restart {scope.unit} on {hosts}"
    return f"reboot {hosts}"


def _marked(text: str) -> bool:
    """Does member text carry the heading only the front desk writes?"""
    return DESK_MARK in " ".join(text.lower().split())


def _changed_lines(diff: str) -> list[str]:
    """The added and removed lines of a `git diff`, each file's under an `@@ path` line."""
    out: list[str] = []
    minus, old, new = "", 0, 0
    for line in diff.splitlines():
        if old > 0 or new > 0:  # inside a hunk; its header said how many lines it has
            if line.startswith("+"):
                new -= 1
            elif line.startswith("-"):
                old -= 1
            elif not line.startswith("\\"):
                old, new = old - 1, new - 1
            if line[:1] in ("+", "-"):
                out.append(line[:200])
        elif line.startswith("--- "):
            minus = line[4:]
        elif line.startswith("+++ "):
            name = minus if line[4:].startswith("/dev/null") else line[4:]
            out.append("@@ " + name.split("\t")[0][2:])  # drop a/ or b/
        elif m := _HUNK.match(line):
            old, new = (1 if n is None else int(n) for n in m.groups())
    return out


def _change_block(reply: dict, hosts: list[str]) -> str:
    """What Tom is shown under a proposed change. Every part of it is the door's
    account of the real commit against main; none of it is the member's."""
    lines = [f"What a yes covers ({DESK_MARK}): commit {reply['rev'][:12]}"]
    total = 0
    for f in reply["files"]:
        f = f if isinstance(f, dict) else {}
        path = " ".join(str(f.get("path")).replace("`", "").split())
        added, removed = (f.get(k) if isinstance(f.get(k), int) else "?" for k in ("added", "removed"))
        lines.append(f"- `{path}` +{added} -{removed}")  # "?": the door counts no lines in a binary file
        total += sum(n for n in (added, removed) if n != "?")
    lines.append(f"Hosts: {', '.join(hosts)}")
    if reply.get("touches_flake") is not False:
        lines.append("This changes flake.nix, which controls where code comes from.")
    shown = _changed_lines(str(reply.get("diff") or ""))
    if total > MAX_SHOWN or reply.get("diff_truncated"):
        lines.append(f"{total} changed lines, too many to show here. Ask for the diff if you want to read it "
                     "before you answer.")
    elif sum(not g.startswith("@@ ") for g in shown) != total:
        lines.append(f"The {total} changed lines could not be read out of the door's diff, so they are not "
                     "shown. Ask for the diff before you answer.")
    elif total:
        lines += ["```diff", *shown, "```"]
    return "\n".join(lines)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class Runner:
    def __init__(self, cfg: Config, ledger: Ledger, room, connect: Callable | None = None,
                 door: Callable[[dict], Awaitable[dict]] | None = None, embed: Callable | None = None):
        """`room` has: async say(member, item_id, kind, text, reply_to=None) -> event_id.

        `connect` is async (member name) -> (reader, writer) to that member's
        worker. It defaults to the member's Unix socket; tests pass their own.
        `door` is async (request) -> reply. It defaults to the door's socket.
        `embed` is async (text) -> embedding or None, the one alerts are matched with.
        """
        self.cfg = cfg
        self.ledger = ledger
        self.room = room
        self.connect = connect or self._connect
        self.door = door or (lambda request: doorclient.ask(cfg.door_socket, request, cfg.door_timeout_s))
        self.embed = embed
        self.sending = asyncio.Lock()  # one member message at a time, so the one-a-day check holds
        self.reserved: dict[int, float] = {}  # budget promised to runs in flight

    async def _connect(self, member: str):
        path = self.cfg.members[member].socket
        if not path:
            raise RuntimeError(f"no worker socket configured for {member}")
        return await asyncio.open_unix_connection(path, limit=wire.LIMIT)

    # -- definition and prompt -----------------------------------------------

    def definition(self, member: str) -> Definition:
        if not _NAME.match(member) or member not in self.cfg.members:
            raise ValueError(f"unknown member {member!r}")
        path = Path(self.cfg.members_dir) / f"{member}.md"
        return parse_definition(member, path.read_text())

    def memory_text(self) -> str:
        root = Path(self.cfg.memory_dir)
        files = [root / f for f in MEMORY_FILES] + sorted((root / "playbooks").glob("*.md"))
        parts = []
        for f in files:
            if f.is_file():
                text = f.read_text().strip()
                if text:
                    parts.append(f"### {f.relative_to(root)}\n{text}")
        return "\n\n".join(parts) or "(no lessons saved yet)"

    def item_text(self, item_id: int) -> str:
        item = self.ledger.get(item_id)
        lines = [
            f"Item #{item.id}: {item.title}",
            f"State: {item.state}. Owner: {item.owner or 'none'}. Started by: {item.origin}."
            + (" Urgent." if item.urgent else ""),
        ]
        if item.body:
            lines += ["", item.body]
        scope = self.ledger.scope(item_id)
        if scope:
            lines += ["", f"Proposed: {_scope_line(scope)}. Done on: {', '.join(scope.done_hosts) or 'no host yet'}."
                      f" Tom's yes on it: {'given' if self.ledger.yes_stands(scope) else 'not given, or no longer standing'}."]
        lines += ["", "Log:"]
        for row in self.ledger.history(item_id):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
            detail = f": {row['detail']}" if row["detail"] else ""
            lines.append(f"- {ts} {row['actor']} {row['what']}{detail}")
        return "\n".join(lines)

    def system_prompt(self, defn: Definition, item_id: int) -> str:
        return "\n\n".join(
            [
                defn.body,
                RULES,
                "## Lessons (Tom's preferences, facts, playbooks)\n\n" + self.memory_text(),
                "## The item you are working\n\n" + self.item_text(item_id),
            ]
        )

    def resume_id(self, member: str, item_id: int) -> str | None:
        """The item's session, only if this same member made it."""
        item = self.ledger.get(item_id)
        if not item.session_id:
            return None
        for row in reversed(self.ledger.history(item_id)):
            if row["what"] == "session" and row["detail"] == item.session_id:
                return item.session_id if row["actor"] == member else None
        return None

    # -- guards --------------------------------------------------------------

    def workdir(self, member: str) -> Path | None:
        wd = self.cfg.members[member].workdir if member in self.cfg.members else ""
        return Path(wd).resolve() if wd else None

    def protected(self) -> list[str]:
        """Paths no member may read or name: the item list and every Matrix token."""
        paths = [str(Path(self.cfg.db_path).resolve().parent)] if self.cfg.db_path else []
        return paths + [m.token_file for m in self.cfg.members.values() if m.token_file]

    def hooks(self, member: str, item_id: int) -> dict[str, Callable]:
        """PreToolUse callbacks by matcher. Returning {} leaves the call to the permission rules."""

        async def bash(input_data, tool_use_id, context):
            command = (input_data or {}).get("tool_input", {}).get("command", "")
            try:
                state = self.ledger.get(item_id).state
            except KeyError:
                state = None
            verdict = policy.check_bash(command, self.protected())
            if verdict.allowed(state):
                return {}
            if verdict.level == policy.NEEDS_YES:
                why = f"Refused: {verdict.reason}, and item #{item_id} is '{state}', not approved. {INSTEAD}"
            else:
                why = f"Refused: {verdict.reason}. Only Tom does this; tell him what you would run and why."
            self.ledger.note(item_id, member, "refused", command[:500])
            return _deny(why)

        async def edit(input_data, tool_use_id, context):
            raw = (input_data or {}).get("tool_input", {}).get("file_path", "")
            wd = self.workdir(member)
            if wd is None:
                return _deny("Refused: you have no working directory for edits.")
            target = (wd / raw).resolve() if raw else wd
            members = Path(self.cfg.members_dir).resolve()
            if not _inside(target, wd) or _inside(target, members):
                self.ledger.note(item_id, member, "refused", f"edit {raw}")
                return _deny(f"Refused: edits are allowed only inside {wd}, never in member definitions.")
            return {}

        async def read(input_data, tool_use_id, context):
            tool_input = (input_data or {}).get("tool_input", {})
            raw = tool_input.get("file_path") or tool_input.get("path") or ""
            wd = self.workdir(member) or Path.cwd()
            verdict = policy.check_path(str(wd / raw), self.protected())
            if verdict.level == policy.FREE:
                return {}
            self.ledger.note(item_id, member, "refused", f"read {raw}")
            return _deny(f"Refused: {verdict.reason}.")

        return {"Bash": bash, "Edit|Write": edit, "Read|Grep|Glob": read}

    # -- tools ---------------------------------------------------------------

    def tools(self, member: str, item_id: int, state: dict, staff_tools: tuple[str, ...] = ()) -> list[Tool]:
        """The member's tools; the worker offers them and sends each call here.
        `state` collects the handoff, if any. `staff_tools` names the door tools
        this member's definition gives it; no other door tool is offered."""
        ledger, cfg = self.ledger, self.cfg
        made: list[Tool] = []

        def tool(name: str, description: str, schema: dict):
            def add(handler):
                made.append(Tool(name, description, {k: t.__name__ for k, t in schema.items()}, handler))
                return handler

            return add

        async def send(kind: str, text: str) -> str | None:
            """Check, post, and record one message, with no other member message in between."""
            async with self.sending:
                unprompted = ledger.check_send(item_id, kind)
                event_id = await self.room.say(member, item_id, kind, text)
                sender = cfg.members[member].user_id if member in cfg.members else member
                if event_id:
                    ledger.record_message(event_id, item_id, sender, kind, unprompted)
                return event_id

        @tool("say", f"Post in the room as {member} on this item. kind: {'|'.join(SAY_KINDS)}.",
              {"kind": str, "text": str})
        async def say(args):
            kind, text = args.get("kind", ""), (args.get("text") or "").strip()
            if kind not in SAY_KINDS:
                return _err(f"kind must be one of {', '.join(SAY_KINDS)}")
            if not text:
                return _err("text is empty")
            if _marked(text):
                return _err(f'Not sent: only the front desk writes "{DESK_MARK}". Take it out and send again.')
            try:
                await send(kind, text)
            except RuleBroken as e:
                ledger.note(item_id, member, "held", str(e)[:200])
                return _err(f"Not sent: {e}")
            return _ok(f"sent ({kind})")

        @tool("move", "Move this item to another state, with what changed or what was checked.",
              {"state": str, "detail": str})
        async def move(args):
            try:
                item = ledger.move(item_id, args.get("state", ""), actor=member, detail=args.get("detail", ""))
            except RuleBroken as e:
                return _err(str(e))
            return _ok(f"item #{item_id} is now '{item.state}'")

        @tool("open_item", "Open a separate item for a different problem you noticed.",
              {"title": str, "body": str})
        async def open_item(args):
            title, body = str(args.get("title") or "").strip(), str(args.get("body") or "")
            if not title:
                return _err("title is empty")
            if state.get("opened", 0) >= MAX_OPENED:
                return _err(f"Refused: this run already opened {MAX_OPENED} items. Work this item; "
                            "anything else can be opened in a later run.")
            item = next((i for i in ledger.open_items() if i.title.strip().lower() == title.lower()), None)
            if item is not None:
                ledger.note(item.id, member, "repeat", title)
            else:  # the match an alert gets: closest open item by embedding, when there is one
                embedding = None
                if self.embed is not None:
                    with contextlib.suppress(Exception):
                        embedding = await self.embed(f"{title}\n{body}".strip())
                item, new = ledger.open(title, origin="staff", actor=member, body=body, embedding=embedding)
                if new:
                    state["opened"] = state.get("opened", 0) + 1
                    return _ok(f"opened item #{item.id}")
            return _ok(f"repeat of item #{item.id} ('{item.state}'); not opened again")

        @tool("handoff", "Give this item to the member who owns the area, with a note for them.",
              {"member": str, "note": str})
        async def handoff(args):
            other = args.get("member", "")
            if other not in cfg.members:
                return _err(f"no member {other!r}; members are {', '.join(sorted(cfg.members))}")
            if other == member:
                return _err("you already own this item")
            ledger.assign(item_id, other, actor=member)
            if args.get("note"):
                ledger.note(item_id, member, "handoff note", args["note"])
            state["handoff"] = other
            return _ok(f"item #{item_id} handed to {other}")

        @tool("remember", "Save a one-line lesson. file: preferences | facts | playbooks/<slug>.",
              {"file": str, "rule": str})
        async def remember(args):
            try:
                saved = await self.remember(member, args.get("file", ""), args.get("rule", ""))
            except ValueError as e:
                return _err(str(e))
            return _ok(f"saved: {saved}\nNow tell Tom the rule you saved so he can veto it.")

        @tool("list_items", "List open items, compactly.", {})
        async def list_items(args):
            rows = [f"#{i.id} [{i.state}] {i.owner or '-'}: {i.title}" for i in ledger.open_items()]
            return _ok("\n".join(rows) or "no open items")

        # -- the door ----------------------------------------------------------

        async def door(request: dict) -> dict:
            """Ask the door, and note the request and its outcome on the item."""
            try:
                reply = await self.door(request)
            except Exception as e:
                reply = {"ok": False, "error": f"The door could not be asked ({type(e).__name__})."}
            asked = " ".join(str(v)[:12] if k == "rev" else str(v) for k, v in request.items()
                             if k in ("op", "host", "action", "unit", "rev", "flake_update"))
            ledger.note(item_id, member, "door", f"{asked}: {'ok' if reply.get('ok') else reply.get('error')}"[:300])
            return reply

        def done(reply: dict, text: str) -> dict:
            if not reply.get("ok"):
                return _err(f"The door refused: {reply.get('error')} This is final for now; tell Tom what it said.")
            extra = reply.get("log") or ""
            return _ok(f"{text}\n\n{str(extra)[-4000:]}".strip())

        def pick_hosts(raw: str) -> list[str]:
            """The named hosts, in roll order. Raises ValueError with a sentence."""
            names = set(re.split(r"[,\s]+", (raw or "").strip())) - {""}
            unknown = sorted(names - set(cfg.hosts))
            if unknown or not names:
                raise ValueError(f"hosts must be one or more of {', '.join(cfg.hosts)}"
                                 + (f"; not {', '.join(unknown)}" if unknown else ""))
            return [h for h in cfg.hosts if h in names]

        def asked_with(scope) -> dict:
            """What every changing request carries. Urgent is the item's, never the
            member's; the time of the yes is the scope's, the one Tom gave on this proposal."""
            return {"item_id": item_id, "urgent": ledger.get(item_id).urgent, "approved_ts": scope.approved_ts}

        def may_run(kinds: tuple[str, ...], host: str | None = None):
            """-> (scope, None), or (None, a refusal the member can act on). Nothing
            passes without Tom's yes on this very proposal, less than a day old."""
            scope = ledger.scope(item_id)
            if scope is None or scope.kind not in kinds:
                what = "propose_change" if "change" in kinds else "propose_action"
                return None, _err(f"Refused: nothing of that kind is proposed on item #{item_id}. Use {what} first.")
            if host is not None and host not in scope.hosts:
                return None, _err(f"Refused: {host or 'that host'} is not in what was proposed "
                                  f"({_scope_line(scope)}). Propose again with the hosts you need.")
            if not ledger.yes_stands(scope):
                why = ("Tom's yes on this proposal is more than a day old" if scope.approved_ts
                       else "Tom has not said yes to this proposal, or he took it back")
                ledger.note(item_id, member, "refused", why)
                return None, _err(f"Refused: {why}. Only his yes on the proposal message itself counts. "
                                  "Wait for it, or propose again and say what changed.")
            return scope, None

        async def propose(text: str, block: str, **scope) -> dict:
            """Post the member's text with the front desk's own block under it, then fix the scope."""
            try:
                event_id = await send("proposal", f"{text}\n\n{block}")
            except RuleBroken as e:
                ledger.note(item_id, member, "held", str(e)[:200])
                return _err(f"Not sent, so nothing is proposed and the item is unchanged: {e}")
            ledger.set_scope(item_id, actor=member, event_id=event_id, **scope)
            return _ok(f"Proposed, and item #{item_id} is 'proposed'. Nothing runs until Tom says yes.\n\n{block}")

        def may_propose(text: str):
            if _marked(text):
                return _err(f'Refused: only the front desk writes "{DESK_MARK}". Take it out and call again.')
            if len(text) > MAX_PROPOSAL:
                return _err(f"Refused: the text is {len(text)} characters and the most is {MAX_PROPOSAL}. "
                            "Tom reads this on a phone; cut it down and call again.")
            if text.count("```") % 2:
                return _err("Refused: a code block in the text is not closed. Close it and call again.")
            if "?" not in text:
                return _err("Refused: the text must end with one yes/no question for Tom. Add it and call again.")
            state_now = ledger.get(item_id).state
            if state_now not in MAY_PROPOSE:
                return _err(f"Refused: item #{item_id} is '{state_now}'. Move it to 'investigated' with what "
                            "you found, then propose.")
            try:
                ledger.check_send(item_id, "proposal")
            except RuleBroken as e:
                ledger.note(item_id, member, "held", str(e)[:200])
                return _err(f"Not sent, so nothing is proposed and the item is unchanged: {e}")
            return None

        @tool("propose_change", "Propose a change to the flake. patch: `git format-patch --stdout` output from "
              "your work folder. hosts: names separated by commas. text: your message to Tom, ending with a "
              "yes/no question, at most 1500 characters. The front desk adds the commit, the changed files and "
              "lines, and the hosts itself.",
              {"patch": str, "hosts": str, "text": str})
        async def propose_change(args):
            text, patch = (args.get("text") or "").strip(), args.get("patch") or ""
            if not patch.strip():
                return _err("patch is empty")
            try:
                hosts = pick_hosts(args.get("hosts", ""))
            except ValueError as e:
                return _err(str(e))
            refusal = may_propose(text)
            if refusal:
                return refusal
            reply = await door({"op": "prepare", "item_id": item_id, "patch": patch})
            if not reply.get("ok"):
                return done(reply, "")
            rev, files = reply.get("rev"), reply.get("files")
            if not isinstance(rev, str) or not _REV.match(rev) or not isinstance(files, list):
                return _err("The door's answer had no commit or file list, so nothing was proposed.")
            block = _change_block(reply, hosts)
            return await propose(text, block, kind="change", rev=rev, hosts=hosts, files=files)

        @tool("propose_action", "Propose one restart or reboot. verb: restart | reboot. host: one host. "
              "unit: the unit to restart (empty for a reboot). text: your message to Tom, ending with a yes/no "
              "question. The front desk adds exactly what will run and where.",
              {"verb": str, "host": str, "unit": str, "text": str})
        async def propose_action(args):
            verb, unit, text = args.get("verb", ""), (args.get("unit") or "").strip(), (args.get("text") or "").strip()
            if verb not in ("restart", "reboot"):
                return _err("verb must be restart or reboot")
            if verb == "restart" and not _UNIT.match(unit):
                return _err("a restart needs unit: a plain .service, .timer, or .socket name")
            if verb == "reboot" and unit:
                return _err("a reboot takes no unit")
            try:
                hosts = pick_hosts(args.get("host", ""))
            except ValueError as e:
                return _err(str(e))
            if len(hosts) != 1:
                return _err("name one host; propose each host on its own")
            refusal = may_propose(text)
            if refusal:
                return refusal
            what = f"restart `{unit}` on {hosts[0]}" if verb == "restart" else f"reboot {hosts[0]}"
            block = f"What a yes covers ({DESK_MARK}): {what}, once."
            return await propose(text, block, kind=verb, unit=unit, hosts=hosts)

        @tool("deploy", "Deploy the proposed commit to one host, only after Tom's yes on the proposal. "
              "action: dry-activate | switch.", {"host": str, "action": str})
        async def deploy(args):
            host, action = args.get("host", ""), args.get("action", "")
            if action not in ("dry-activate", "switch"):
                return _err("action must be dry-activate or switch")
            scope, refusal = may_run(("change",), host)
            if refusal:
                return refusal
            if action == "switch":
                if host in scope.done_hosts:
                    return _err(f"Refused: {host} was already switched to this commit. A yes covers each host once.")
                # Canary order: scope.hosts is kept in roll order, so every
                # host before this one must already be switched and healthy.
                waiting = [h for h in scope.hosts[:scope.hosts.index(host)] if h not in scope.done_hosts]
                if waiting:
                    return _err(f"Refused: {host} comes after {', '.join(waiting)}. Switch in order; "
                                "a host that failed stops the roll.")
            reply = await door({"op": "deploy", "host": host, "action": action, "rev": scope.rev, **asked_with(scope)})
            if reply.get("ok") and action == "switch":
                ledger.mark_done(item_id, host)
            return done(reply, f"{action} of {scope.rev[:12]} on {host}: ok")

        @tool("act", "Run the approved restart or reboot, once.", {})
        async def act(args):
            scope, refusal = may_run(("restart", "reboot"))
            if refusal:
                return refusal
            if scope.spent:
                return _err(f"Refused: this already ran ({_scope_line(scope)}). A yes covers it once; "
                            "propose again if it is needed again.")
            host = scope.hosts[0]
            request = {"op": scope.kind, "host": host, **({"unit": scope.unit} if scope.kind == "restart" else {})}
            reply = await door({**request, **asked_with(scope)})
            if reply.get("ok"):
                ledger.mark_done(item_id, host)
            return done(reply, f"{_scope_line(scope)}: ok")

        @tool("flake_update", "Update the flake's inputs on one host; needs no yes. The door makes the lock "
              "file and keeps the order: suzuka first.", {"host": str})
        async def flake_update(args):
            host = args.get("host", "")
            if host not in cfg.hosts:
                return _err(f"host must be one of {', '.join(cfg.hosts)}")
            # An update nobody asked for is never urgent, whatever the item is.
            reply = await door({"op": "flake_update", "host": host, "item_id": item_id, "urgent": False})
            return done(reply, f"flake update on {host}: ok")

        @tool("finish", "Put the proposed commit on main, once every proposed host is switched to it. "
              "Tom's yes must still stand.", {})
        async def finish(args):
            scope, refusal = may_run(("change",))
            if refusal:
                return refusal
            left = [h for h in scope.hosts if h not in scope.done_hosts]
            if left:
                return _err(f"Refused: not switched yet on {', '.join(left)}. Finish comes after every proposed host.")
            reply = await door({"op": "finish", "item_id": item_id, "rev": scope.rev, "hosts": scope.hosts})
            return done(reply, f"commit {scope.rev[:12]} is on main")

        @tool("finish_flake_update", "Put the flake update on main, once every host is on it.", {})
        async def finish_flake_update(args):
            reply = await door({"op": "finish", "flake_update": True, "hosts": list(cfg.hosts)})
            return done(reply, "the flake update is on main")

        @tool("host_status", "The revision a host runs and whether a revert is pending.", {"host": str})
        async def host_status(args):
            host = args.get("host", "")
            if host not in cfg.hosts:
                return _err(f"host must be one of {', '.join(cfg.hosts)}")
            reply = await door({"op": "status", "host": host})
            if not reply.get("ok"):
                return done(reply, "")
            return _ok(json.dumps({k: v for k, v in reply.items() if k != "ok"}, indent=1)[:4000])

        return [t for t in made if t.name not in DOOR_TOOLS or t.name in staff_tools]

    async def remember(self, member: str, file: str, rule: str) -> str:
        """Append a dated lesson under memory_dir and commit it. Returns the saved line."""
        name = file.strip().removesuffix(".md")
        if name in ("preferences", "facts"):
            rel = f"{name}.md"
        elif name.startswith("playbooks/") and _SLUG.match(name.split("/", 1)[1]):
            rel = f"{name}.md"
        else:
            raise ValueError("file must be preferences, facts, or playbooks/<lowercase-slug>")
        rule = " ".join(rule.split())
        if not rule:
            raise ValueError("rule is empty")
        root = Path(self.cfg.memory_dir).resolve()
        path = (root / rel).resolve()
        if not _inside(path, root):
            raise ValueError("path escapes the memory directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"- {time.strftime('%Y-%m-%d', time.localtime(self.ledger.now()))} ({member}): {rule}"
        with path.open("a") as f:
            f.write(line + "\n")
        if (root / ".git").exists():
            await self._git(root, "add", rel)
            await self._git(root, "-c", f"user.name={member}", "-c", f"user.email={member}@staff.invalid",
                            "commit", "-q", "-m", f"{member}: {rule[:60]}", "--", rel)
        return line

    async def _git(self, root: Path, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(root), *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            raise ValueError(f"saved, but git failed: {err.decode(errors='replace').strip()[:200]}")

    # -- the run -------------------------------------------------------------

    def job(self, member: str, item_id: int, prompt: str, defn: Definition, budget: float,
            tools: list[Tool], hooks: dict[str, Callable]) -> dict[str, Any]:
        """Everything the worker needs for one SDK call. Its working directory and
        credentials are not here: those come from its unit."""
        return {
            "member": member,
            "item_id": item_id,
            "prompt": prompt,
            "system_prompt": self.system_prompt(defn, item_id),
            "model": defn.model or self.cfg.members[member].model,
            "builtin_tools": list(defn.tools),
            "tools": [{"name": t.name, "description": t.description, "schema": t.schema} for t in tools],
            "hook_matchers": list(hooks),
            "resume": self.resume_id(member, item_id),
            "max_turns": self.cfg.max_turns,
            "budget_usd": budget,
        }

    async def answer(self, msg: dict, tools: dict[str, Tool], hooks: dict[str, Callable]) -> dict[str, Any]:
        """The result for one `call` from the worker. Never raises; whatever the
        worker sent, a hook that cannot be checked is a deny and a tool that
        cannot run is an error result."""
        if "hook" in msg:
            try:
                return await hooks[msg["hook"]](msg.get("input"), None, {})
            except Exception as e:
                return _deny(f"Refused: the front desk could not check this call ({type(e).__name__}).")
        name, args = msg.get("tool"), msg.get("args")
        if not isinstance(name, str) or name not in tools:
            return _err(f"no tool {name!r}")
        if not isinstance(args, dict):
            return _err("args must be an object")
        try:
            return await tools[name].handler(args)
        except Exception as e:
            return _err(f"{type(e).__name__}: {e}")

    async def run(self, member: str, item_id: int, prompt: str) -> RunResult:
        left = self.ledger.budget_left(self.cfg.daily_cap_usd, self.cfg.monthly_cap_usd)
        left -= sum(self.reserved.values())  # two runs at once must not both spend the same remainder
        if left <= 0:
            msg = "budget cap reached; not starting"
            self.ledger.note(item_id, member, "run refused", msg)
            return RunResult(False, None, 0.0, 0, "", None, msg)

        state: dict = {"handoff": None}
        session_id: str | None = None
        result: dict | None = None
        last_text = ""
        model = self.cfg.members[member].model if member in self.cfg.members else "unknown"
        error = ""
        budget = min(self.cfg.per_run_cap_usd, left)
        key = id(state)
        self.reserved[key] = budget
        assumed = 0.0  # what to record if the run dies without a result
        writer = None
        try:
            defn = self.definition(member)
            model = defn.model or model
            tools = {t.name: t for t in self.tools(member, item_id, state, defn.staff_tools)}
            hooks = self.hooks(member, item_id)
            job = self.job(member, item_id, prompt, defn, budget, list(tools.values()), hooks)
            reader, writer = await self.connect(member)
            await wire.send(writer, {"job": job})
            loop, used = asyncio.get_running_loop(), 0.0
            while True:
                idle, since = self.cfg.run_idle_s, loop.time()
                limit = min(idle, self.cfg.run_timeout_s - used)
                try:
                    msg = await asyncio.wait_for(wire.read(reader), limit)
                except TimeoutError:
                    error = (f"the worker said nothing for {idle:.0f} seconds" if limit == idle
                             else f"the run passed {self.cfg.run_timeout_s:.0f} seconds") + "; it was stopped"
                    assumed = budget  # what it really cost is unknown
                    break
                if msg is None:
                    break
                event = msg.get("event")
                if "call" in msg:
                    began = loop.time()
                    reply = await self.answer(msg, tools, hooks)
                    if msg.get("tool") in SLOW_TOOLS:
                        since += loop.time() - began  # a deploy waits on a host for longer than a run may last
                    await wire.send(writer, {"reply": msg["call"], "result": reply})
                    used += loop.time() - since
                    continue
                used += loop.time() - since
                if event == "session":
                    sid = msg.get("session_id")
                    if isinstance(sid, str) and sid and sid != session_id:
                        session_id = sid
                        self._save_session(member, item_id, sid)
                elif event == "assistant":
                    assumed = budget  # the API was called; a lost result still cost money
                    if msg.get("text"):
                        last_text = str(msg["text"])
                elif event == "result":
                    result = _result(msg, budget, self.cfg.max_turns)
                    break
                elif event == "error":
                    error = str(msg.get("error") or "worker error")
                    break
        except asyncio.CancelledError:
            self._finish(member, model, item_id, result, "cancelled", assumed)
            raise  # cancellation belongs to the caller; spend is already recorded
        except Exception as e:  # never raise out of run()
            error = f"{type(e).__name__}: {e}"
        finally:
            self.reserved.pop(key, None)
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()  # the worker stops when its connection ends

        if result is not None and result["session_id"] and result["session_id"] != session_id:
            session_id = result["session_id"]
            self._save_session(member, item_id, session_id)
        if result is None and not error:
            error = "no result from the worker"
        elif result is not None and not error and result["is_error"]:
            error = result["subtype"] + (f": {'; '.join(result['errors'])}" if result["errors"] else "")
        cost = self._finish(member, model, item_id, result, error, assumed)
        return RunResult(
            ok=not error,
            session_id=session_id,
            cost_usd=cost,
            turns=result["turns"] if result else 0,
            summary=(result["text"] if result and result["text"] else last_text),
            handoff=state["handoff"],
            error=error,
        )

    def _save_session(self, member: str, item_id: int, sid: str) -> None:
        self.ledger.set_session(item_id, sid)
        self.ledger.note(item_id, member, "session", sid)

    def _finish(self, member: str, model: str, item_id: int, result, error: str, assumed: float = 0.0) -> float:
        """Record spend in every outcome. If the run died after calling the API and
        left no result, the cost is unknown, so the run's whole budget is recorded:
        the cap must err toward stopping early, never toward a surprise bill."""
        cost = result["cost"] if result is not None else assumed
        try:
            self.ledger.record_spend(member, model, cost, item_id)
            if error:
                self.ledger.note(item_id, member, "run failed", error[:1000])
        except Exception:
            pass
        return cost
