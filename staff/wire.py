"""Wire 1: one JSON object per line, UTF-8, between the front desk and a member worker."""

from __future__ import annotations

import asyncio
import json
from typing import Any

LIMIT = 8 * 1024 * 1024  # longest line either side accepts; pass as the stream's `limit`


def encode(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode() + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("wire message is not a JSON object")
    return obj


async def read(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """The next message, or None when the other side closed. ValueError on a bad or oversized line."""
    try:
        line = await reader.readline()
    except ConnectionError:
        return None
    if len(line) > LIMIT:
        raise ValueError("wire line too long")
    if not line.endswith(b"\n"):
        return None  # closed, possibly mid-line; a cut-off message is not a message
    return decode(line)


async def send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(encode(obj))
    await writer.drain()
