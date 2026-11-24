"""The front desk's side of the door (ARCHITECTURE.md, "Wire 3").

One JSON line out, one JSON line back, then the connection ends. The door
decides; this only carries the question and the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from . import wire


def _failed(why: str) -> dict:
    return {"ok": False, "error": why}


async def ask(socket_path: str, request: dict, timeout: float = 1800) -> dict:
    """Send one request to the door and return its reply. Never raises: with no
    socket, no answer in time, or an answer that is not a reply, the result is
    `{"ok": False, "error": ...}`, the same shape as a refusal."""
    if not socket_path:
        return _failed("The door is not configured, so nothing can be deployed, restarted, or rebooted.")
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(socket_path, limit=wire.LIMIT)
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
        reply = json.loads(line)
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise ValueError("not a reply")
    except TimeoutError:
        return _failed(f"The door did not answer within {timeout:.0f} seconds; what it was doing may still finish.")
    except OSError as e:
        return _failed(f"The door could not be reached ({type(e).__name__}).")
    except ValueError:
        return _failed("The door's answer could not be read.")
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
    if not reply["ok"] and not isinstance(reply.get("error"), str):
        reply["error"] = "The door refused and gave no reason."
    return reply
