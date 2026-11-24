"""The member worker: one Agent SDK run, as the member's own OS user.

systemd starts `staff-worker` with the front desk's connection as stdin and
stdout (ARCHITECTURE.md, "Wire 1"). The worker owns nothing but the SDK call:
every member tool is a stub that asks the front desk, and so is every
PreToolUse hook. A hook that cannot get an answer denies.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, SdkMcpTool, create_sdk_mcp_server

from . import wire

SERVER = "staff"
TYPES = {"str": str, "int": int, "float": float, "bool": bool}
HOOK_WAIT_S = 45  # under the SDK's 60 s hook timeout, so a late answer is a deny here


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


class Link:
    """Calls to the front desk, matched to their replies by number."""

    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.pending: dict[int, asyncio.Future] = {}
        self.count = 0
        self.lost = False

    async def send(self, obj: dict[str, Any]) -> None:
        await wire.send(self.writer, obj)

    async def call(self, body: dict[str, Any]) -> Any:
        if self.lost:
            raise ConnectionError("the front desk connection is gone")
        self.count += 1
        n = self.count
        fut = self.pending[n] = asyncio.get_running_loop().create_future()
        try:
            await self.send({"call": n, **body})
            return await fut
        finally:
            self.pending.pop(n, None)

    async def pump(self, reader: asyncio.StreamReader) -> None:
        """Hand replies to their callers until the connection ends, then fail every waiter."""
        try:
            while (msg := await wire.read(reader)) is not None:
                fut = self.pending.get(msg.get("reply"))
                if fut and not fut.done():
                    fut.set_result(msg.get("result"))
        finally:
            self.lost = True
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("the front desk connection is gone"))


def tool_stub(link: Link, spec: dict[str, Any]) -> SdkMcpTool:
    name = spec["name"]

    async def handler(args):
        try:
            result = await link.call({"tool": name, "args": args})
        except Exception as e:
            return _err(f"{name} did not run: {e}")
        return result if isinstance(result, dict) else _err(f"{name}: bad reply from the front desk")

    schema = {key: TYPES[kind] for key, kind in spec.get("schema", {}).items()}
    return SdkMcpTool(name=name, description=spec.get("description", ""), input_schema=schema, handler=handler)


def hook_stub(link: Link, matcher: str) -> Callable:
    async def hook(input_data, tool_use_id, context):
        try:
            result = await asyncio.wait_for(link.call({"hook": matcher, "input": input_data}), HOOK_WAIT_S)
        except Exception as e:  # no answer is never a yes
            return _deny(f"Refused: the front desk did not answer ({type(e).__name__}).")
        return result if isinstance(result, dict) else _deny("Refused: bad reply from the front desk.")

    return hook


def options(job: dict[str, Any], link: Link) -> ClaudeAgentOptions:
    tools = [tool_stub(link, spec) for spec in job.get("tools", [])]
    builtins = list(job.get("builtin_tools", []))
    return ClaudeAgentOptions(
        system_prompt=job["system_prompt"],
        model=job.get("model"),
        tools=builtins,  # the only built-in tools that exist in the session
        allowed_tools=builtins + [f"mcp__{SERVER}__{t.name}" for t in tools],
        permission_mode="dontAsk",  # never prompt; anything not allowed is denied
        setting_sources=[],  # no user/project settings can widen permissions
        strict_mcp_config=True,
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=tools)},
        hooks={
            "PreToolUse": [
                # on_failure="block": a hook that raises or times out blocks the call
                HookMatcher(matcher=m, hooks=[hook_stub(link, m)], on_failure="block")
                for m in job.get("hook_matchers", [])
            ]
        },
        resume=job.get("resume"),
        max_turns=job["max_turns"],
        max_budget_usd=job["budget_usd"],
    )  # cwd is left alone: the unit sets the working directory


async def _run(job: dict[str, Any], link: Link, query_fn: Callable) -> None:
    """One SDK query; its messages become events. Any failure becomes an error event."""
    try:
        async with contextlib.aclosing(query_fn(prompt=job["prompt"], options=options(job, link))) as stream:
            async for msg in stream:
                kind = type(msg).__name__
                if kind == "SystemMessage" and getattr(msg, "subtype", "") == "init":
                    sid = (getattr(msg, "data", None) or {}).get("session_id")
                    if sid:
                        await link.send({"event": "session", "session_id": sid})
                elif kind == "AssistantMessage":
                    texts = [b.text for b in msg.content if type(b).__name__ == "TextBlock"]
                    await link.send({"event": "assistant", "text": "\n".join(texts)})
                elif kind == "ResultMessage":
                    await link.send({
                        "event": "result", "session_id": msg.session_id,
                        "total_cost_usd": msg.total_cost_usd, "num_turns": msg.num_turns,
                        "is_error": msg.is_error, "subtype": msg.subtype,
                        "errors": list(getattr(msg, "errors", None) or []), "result": msg.result,
                    })
    except Exception as e:
        with contextlib.suppress(Exception):
            await link.send({"event": "error", "error": f"{type(e).__name__}: {e}"})


async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, member: str,
                query_fn: Callable | None = None) -> None:
    """Read the one job, run it, and close. `member` is who this worker runs as."""
    link = Link(writer)
    tasks: list[asyncio.Task] = []
    try:
        try:
            job = (await wire.read(reader) or {}).get("job")
        except ValueError:
            job = None
        if not isinstance(job, dict):
            await link.send({"event": "error", "error": "expected one job"})
        elif not member or job.get("member") != member:
            await link.send({"event": "error",
                             "error": f"job is for {job.get('member')!r}; this worker is {member!r}"})
        else:
            if query_fn is None:
                from claude_agent_sdk import query as query_fn
            tasks = [asyncio.create_task(link.pump(reader)), asyncio.create_task(_run(job, link, query_fn))]
            # Whichever ends first ends the other: a finished run stops the reader,
            # and a lost connection stops the run (nobody is left to answer or pay attention).
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except ConnectionError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _main(conn: socket.socket, query_fn: Callable | None) -> None:
    reader, writer = await asyncio.open_connection(sock=conn, limit=wire.LIMIT)
    await serve(reader, writer, os.environ.get("STAFF_MEMBER", ""), query_fn)


def main(query_fn: Callable | None = None) -> None:
    # The connection is stdin and stdout. Keep a private copy and point fd 1 at
    # stderr, so a stray print (here, in a library, in a child) cannot reach the wire.
    conn = socket.socket(fileno=os.dup(0))
    if os.path.sameopenfile(0, 2):  # stderr is the connection too (systemd's default with a socket)
        os.dup2(os.open(os.devnull, os.O_WRONLY), 2)
    os.dup2(2, 1)
    asyncio.run(_main(conn, query_fn))


if __name__ == "__main__":
    main()
