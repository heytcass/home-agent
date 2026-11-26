"""The Matrix side of the front desk: one unencrypted room.

Tom talks there. Each member posts as its own account. The alerts account
posts alerts with a custom content field. This module turns raw room events
into three kinds of callback and posts messages, and does nothing else.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from nio import AsyncClient, Event, SyncResponse

from .config import Config
from .ledger import Ledger, RuleBroken

log = logging.getLogger("staff.room")

DEVICE_ID = "FRONTDESK"


@dataclass
class Incoming:
    event_id: str
    sender: str
    text: str
    reply_to: str | None
    ts: float
    mentions: tuple[str, ...] = ()  # members Tom addressed by name, in config order


@dataclass
class Reaction:
    event_id: str
    sender: str
    target: str
    key: str


@dataclass
class Alert:
    event_id: str
    text: str
    host: str
    source: str
    key: str
    level: str
    urgent: bool

    @property
    def alert_key(self) -> str | None:
        """None unless all three parts are present, so half-labeled alerts never merge."""
        if not (self.host and self.source and self.key):
            return None
        return f"{self.host}/{self.source}/{self.key}"


_PILL = re.compile(r'<a href="https://matrix\.to/#/((?:@|%40)[^"?]+)[^"]*">([^<]*)</a>')


def _mentions(cfg: Config, content: dict, body: str) -> tuple[tuple[str, ...], str]:
    """Which members a message addresses, and its text with each name set apart.
    A client writes a mention into the plain text as the bare display name, and some
    leave no space after it ("fleetgo ahead"), so the name is split from the next word."""
    listed = (content.get("m.mentions") or {}).get("user_ids") if isinstance(content.get("m.mentions"), dict) else None
    ids = {u for u in listed if isinstance(u, str)} if isinstance(listed, list) else set()
    rich = content.get("formatted_body")
    pills = _PILL.findall(rich) if isinstance(rich, str) else []
    ids |= {unquote(user) for user, _ in pills}
    names = tuple(name for name, m in cfg.members.items() if m.user_id in ids)
    if not names:
        return (), body
    by_id = {m.user_id: name for name, m in cfg.members.items()}
    labels = {label for user, label in pills if unquote(user) in by_id and label.strip()}
    labels |= {m.user_id.split(":")[0].lstrip("@") for m in cfg.members.values() if m.user_id in ids}
    for label in sorted(labels, key=len, reverse=True):
        body = re.sub(rf"(?<!\w)(@?{re.escape(label)})(?=[^\W_])", r"\1 ", body, count=1, flags=re.I)
    return names, body


# -- classification (pure) ---------------------------------------------------


def classify(cfg: Config, source: dict) -> Incoming | Reaction | Alert | None:
    """Turn one raw room event into something to dispatch, or None to ignore it."""
    sender = source.get("sender", "")
    event_id = source.get("event_id", "")
    content = source.get("content")
    if not isinstance(content, dict) or not event_id:
        return None
    if any(m.user_id == sender for m in cfg.members.values()):
        return None  # loop protection
    rel = content.get("m.relates_to")
    rel = rel if isinstance(rel, dict) else {}
    etype = source.get("type")

    if etype == "m.reaction":
        if sender != cfg.tom or rel.get("rel_type") != "m.annotation":
            return None
        if not rel.get("event_id") or not rel.get("key"):
            return None
        return Reaction(event_id, sender, rel["event_id"], rel["key"])

    if etype != "m.room.message" or rel.get("rel_type") == "m.replace":
        return None  # edits are ignored for now
    body = content.get("body")
    if not isinstance(body, str):
        return None

    if sender == cfg.alerts_user:
        a = content.get(cfg.alert_field)
        if not isinstance(a, dict):
            return None
        return Alert(
            event_id=event_id,
            text=body,
            host=str(a.get("host", "")),
            source=str(a.get("source", "")),
            key=str(a.get("key", "")),
            level=str(a.get("level", "")),
            urgent=a.get("urgent") is True,  # "false", 1, or anything else that is not a real true is not urgent
        )

    if sender == cfg.tom and content.get("msgtype") == "m.text":
        reply_to = (rel.get("m.in_reply_to") or {}).get("event_id")
        if reply_to:
            body = _strip_reply_fallback(body)
        mentions, body = _mentions(cfg, content, body)
        return Incoming(event_id, sender, body, reply_to, source.get("origin_server_ts", 0) / 1000, mentions)
    return None


def _strip_reply_fallback(body: str) -> str:
    """Drop the quoted "> ..." lines some clients put before a reply."""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    if i and i < len(lines) and lines[i] == "":
        i += 1
    return "\n".join(lines[i:]) if i < len(lines) else body


# -- rule 6: no server paths ---------------------------------------------------

_FENCED = re.compile(r"^```.*?^```", re.S | re.M)
_PATH = re.compile(r"(?<![\w.:/~-])/(?:var|home|srv|mnt|nix/store|etc|opt|root)/\S+")


def check_no_paths(text: str) -> None:
    """Raise RuleBroken if prose contains a server file path. Code blocks may."""
    m = _PATH.search(_FENCED.sub("", text))
    if m:
        raise RuleBroken(f"brief rule 6: put the content in the message, not the path {m.group(0)!r}")


# -- markdown to HTML ----------------------------------------------------------

_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")


def _inline(text: str) -> str:
    codes: list[str] = []
    text = text.replace("\0", "")  # NUL marks the stashed code below; no message may bring its own

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\0{len(codes) - 1}\0"

    text = re.sub(r"`([^`\n]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(
        r"\[([^\]]+)\]\(((?:https?://|mailto:)[^\s)]+)\)", r'<a href="\2">\1</a>', text
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", text)
    return re.sub(r"\0(\d+)\0", lambda m: codes[int(m.group(1))], text)


def _blocks(text: str) -> str:
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in text.split("\n"):
        b = _BULLET.match(line)
        if b:
            if para:
                flush()
            bullets.append(b.group(1))
        elif line.strip():
            if bullets:
                flush()
            para.append(line.rstrip())
        else:
            flush()
    flush()
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """Bold, italics, inline code, fenced code, links, bullets, line breaks."""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"^```[^\n]*\n(.*?)^```[ \t]*$", text, re.S | re.M):
        out.append(_blocks(text[pos : m.start()]))
        out.append(f"<pre><code>{html.escape(m.group(1))}</code></pre>")
        pos = m.end()
    out.append(_blocks(text[pos:]))
    return "".join(out)


# -- the room ------------------------------------------------------------------


class Room:
    def __init__(self, cfg: Config, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger
        self.token_path = Path(cfg.db_path + ".sync")
        self._clients: dict[str, AsyncClient] = {}

    def _client(self, member: str) -> AsyncClient:
        """A logged-in client for one member, made on first use. Tokens only."""
        if member not in self._clients:
            m = self.cfg.members[member]
            c = AsyncClient(self.cfg.homeserver, m.user_id)
            c.restore_login(m.user_id, DEVICE_ID, m.token())
            self._clients[member] = c
        return self._clients[member]

    # -- listening -------------------------------------------------------------

    async def run(self, on_message, on_reaction, on_alert) -> None:
        """Sync forever as the chief's account. Never raises on network trouble."""
        chief = self._client("chief")
        flt = {"room": {"rooms": [self.cfg.room_id]}}
        since = self._load_token()
        backoff = 1.0
        while True:
            try:
                resp = await chief.sync(timeout=30000, since=since, sync_filter=flt)
                if not isinstance(resp, SyncResponse):
                    raise RuntimeError(f"sync failed: {resp}")
                backoff = 1.0
                if since is not None:  # first start: skip what is already there
                    await self._dispatch(resp, on_message, on_reaction, on_alert)
                since = resp.next_batch
                self._save_token(since)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync failed; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _dispatch(self, resp: SyncResponse, on_message, on_reaction, on_alert) -> None:
        room = resp.rooms.join.get(self.cfg.room_id)
        if room is None:
            return
        for ev in room.timeline.events:
            if not isinstance(ev, Event):
                continue
            got = classify(self.cfg, ev.source)
            handler = {Incoming: on_message, Reaction: on_reaction, Alert: on_alert}.get(type(got))
            if handler is None:
                continue
            try:
                await handler(got)
            except Exception:
                log.exception("handler failed for %s", ev.event_id)

    def _load_token(self) -> str | None:
        try:
            return self.token_path.read_text().strip() or None
        except FileNotFoundError:
            return None

    def _save_token(self, token: str) -> None:
        tmp = self.token_path.with_suffix(".sync.tmp")
        tmp.write_text(token)
        tmp.replace(self.token_path)

    # -- speaking ----------------------------------------------------------------

    async def say(
        self, member: str, item_id: int, kind: str, text: str, reply_to: str | None = None
    ) -> str:
        unprompted = self.ledger.check_send(item_id, kind)  # RuleBroken: send nothing
        check_no_paths(text)
        content: dict = {
            "msgtype": "m.text",
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": markdown_to_html(text),
        }
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        resp = await self._client(member).room_send(self.cfg.room_id, "m.room.message", content)
        event_id = getattr(resp, "event_id", None)
        if not event_id:
            raise RuntimeError(f"could not send as {member}: {resp}")
        self.ledger.record_message(event_id, item_id, self.cfg.members[member].user_id, kind, unprompted)
        return event_id

    async def react(self, member: str, event_id: str, emoji: str) -> None:
        content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": emoji}}
        await self._client(member).room_send(self.cfg.room_id, "m.reaction", content)

    async def typing(self, member: str, on: bool) -> None:
        await self._client(member).room_typing(self.cfg.room_id, on, 30000)

    async def close(self) -> None:
        for c in self._clients.values():
            await c.close()
        self._clients.clear()
