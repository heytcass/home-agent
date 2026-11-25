"""The shared item list.

Every piece of work is one item. Every room message belongs to one item, so
a reply or a reaction is never ambiguous. The rules in BRIEF.md that can be
enforced in code are enforced here, outside any agent's judgment:

- an item only moves along the allowed lifecycle;
- only Tom can approve;
- an item cannot be closed as fixed unless it was verified;
- an alert that matches an open item is attached to it, never re-announced;
- at most one unprompted item a day;
- spend is recorded and capped.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path

# state -> states it may move to
LIFECYCLE: dict[str, set[str]] = {
    "noticed": {"investigated", "dropped", "closed"},  # closed: answered, nothing to change
    "investigated": {"pitched", "proposed", "done", "waiting", "dropped", "shelved", "closed"},
    "waiting": {"investigated", "shelved", "dropped"},  # a question is out to Tom
    "pitched": {"approved", "dropped", "shelved", "investigated"},
    "proposed": {"approved", "dropped", "shelved", "investigated"},
    "approved": {"done", "investigated"},
    "done": {"verified", "investigated"},
    "verified": {"closed"},
    "shelved": {"investigated", "dropped"},
    "closed": set(),
    "dropped": set(),
}
OPEN_STATES = set(LIFECYCLE) - {"closed", "dropped", "shelved"}
AWAITING_TOM = {"pitched", "proposed", "waiting"}

# The first message of any kind on an item Tom did not start counts as the day's
# unprompted item. These kinds never count: they are the front desk's own.
EXEMPT_KINDS = {"reminder", "system", "intake"}

# A problem Tom dropped, or that was shelved, stays quiet this long if it comes back.
QUIET_AFTER_NO = 7 * 86400
APPROVAL_LASTS = 24 * 3600  # a yes that was not used in a day has to be asked for again
DESK_KEY = "frontdesk/desk/notices"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  owner TEXT,
  state TEXT NOT NULL DEFAULT 'noticed',
  origin TEXT NOT NULL,            -- tom | alert | staff
  alert_key TEXT,                  -- host/source/key of the alert that opened it
  embedding BLOB,
  urgent INTEGER NOT NULL DEFAULT 0,
  session_id TEXT,                 -- Agent SDK session of the owner's work
  reminded_ts REAL,
  created_ts REAL NOT NULL,
  updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE TABLE IF NOT EXISTS messages (
  event_id TEXT PRIMARY KEY,       -- Matrix event id
  item_id INTEGER NOT NULL REFERENCES items(id),
  sender TEXT NOT NULL,
  kind TEXT NOT NULL,              -- intake|claim|chat|question|pitch|proposal|reminder|result|rule
  unprompted INTEGER NOT NULL DEFAULT 0,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY,
  item_id INTEGER,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  what TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  member TEXT NOT NULL,
  item_id INTEGER,
  model TEXT NOT NULL,
  usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scopes (
  item_id INTEGER PRIMARY KEY REFERENCES items(id),
  kind TEXT NOT NULL,              -- change | restart | reboot
  rev TEXT NOT NULL DEFAULT '',    -- change: the commit the door prepared
  unit TEXT NOT NULL DEFAULT '',   -- restart: the unit
  hosts TEXT NOT NULL,             -- JSON list, in roll order
  files TEXT NOT NULL DEFAULT '[]',       -- JSON, as the door reported them
  done_hosts TEXT NOT NULL DEFAULT '[]',  -- JSON list of hosts where the scoped action succeeded
  event_id TEXT,                   -- the proposal message Tom's yes must be on
  approved_ts REAL,                -- when Tom said yes on that message; NULL: no yes, or taken back
  set_ts REAL NOT NULL
);
"""


class RuleBroken(Exception):
    """An agent asked for something the brief forbids."""


@dataclass
class Item:
    id: int
    title: str
    body: str
    owner: str | None
    state: str
    origin: str
    alert_key: str | None
    urgent: bool
    session_id: str | None
    reminded_ts: float | None
    created_ts: float
    updated_ts: float

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


@dataclass
class Scope:
    """What a yes on an item covers: one commit, or one restart or reboot, on these hosts."""

    kind: str
    rev: str
    unit: str
    hosts: list[str]
    files: list
    done_hosts: list[str]
    event_id: str | None
    set_ts: float
    approved_ts: float | None = None  # when Tom said yes on event_id, if he did and it still stands

    @property
    def spent(self) -> bool:
        return set(self.hosts) <= set(self.done_hosts)


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _day_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _month_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


class Ledger:
    def __init__(self, path: str | Path, tom: str, clock=time.time):
        """`tom` is Tom's Matrix user id; he is the only one who can approve."""
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.tom = tom
        self.now = clock

    # -- items ---------------------------------------------------------------

    def _item(self, row: sqlite3.Row | None) -> Item | None:
        if row is None:
            return None
        d = {k: row[k] for k in Item.__dataclass_fields__}
        d["urgent"] = bool(d["urgent"])
        return Item(**d)

    def get(self, item_id: int) -> Item:
        item = self._item(self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
        if item is None:
            raise KeyError(item_id)
        return item

    def open_items(self, owner: str | None = None) -> list[Item]:
        marks = ",".join("?" * len(OPEN_STATES))
        sql = f"SELECT * FROM items WHERE state IN ({marks})"
        args: list = sorted(OPEN_STATES)
        if owner:
            sql += " AND owner=?"
            args.append(owner)
        return [self._item(r) for r in self.db.execute(sql + " ORDER BY id", args)]

    def find_repeat(
        self, alert_key: str | None, embedding: list[float] | None, threshold: float = 0.86, urgent: bool = False
    ) -> Item | None:
        """The item this is a repeat of, if any.

        An exact alert key wins. Otherwise the closest item by embedding, if
        it is close enough. With no key and no embedding (Silverstone is
        down) nothing matches, and the worst case is one duplicate item.

        Open items match. So do items Tom dropped or that were shelved in the
        last week, unless this is urgent: his "no" holds, the problem is not
        announced again. A closed item (fixed and checked) never matches, so
        a problem that comes back after a fix is a new item.
        """
        marks = ",".join("?" * len(OPEN_STATES))
        where = f"(state IN ({marks})"
        args: list = sorted(OPEN_STATES)
        if not urgent:
            where += " OR (state IN ('dropped','shelved') AND updated_ts>=?)"
            args.append(self.now() - QUIET_AFTER_NO)
        where += ")"
        if alert_key:
            row = self.db.execute(
                f"SELECT * FROM items WHERE alert_key=? AND {where} ORDER BY id DESC LIMIT 1", [alert_key, *args]
            ).fetchone()
            if row:
                return self._item(row)
        if embedding is None:
            return None
        best, best_score = None, threshold
        rows = self.db.execute(f"SELECT * FROM items WHERE embedding IS NOT NULL AND {where}", args)
        for row in rows:
            score = cosine(embedding, unpack(row["embedding"]))
            if score >= best_score:
                best, best_score = self._item(row), score
        return best

    def open(
        self,
        title: str,
        origin: str,
        actor: str,
        body: str = "",
        alert_key: str | None = None,
        embedding: list[float] | None = None,
        urgent: bool = False,
    ) -> tuple[Item, bool]:
        """Open an item, or return the item it repeats. -> (item, is_new)"""
        if origin not in ("tom", "alert", "staff"):
            raise ValueError(origin)
        if origin != "tom":
            repeat = self.find_repeat(alert_key, embedding, urgent=urgent)
            if repeat:
                self.note(repeat.id, actor, "repeat", title)
                return repeat, False
        now = self.now()
        cur = self.db.execute(
            "INSERT INTO items(title, body, origin, alert_key, embedding, urgent, created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (title, body, origin, alert_key, pack(embedding) if embedding else None, int(urgent), now, now),
        )
        self.db.commit()
        self.note(cur.lastrowid, actor, "opened", title)
        return self.get(cur.lastrowid), True

    def assign(self, item_id: int, owner: str, actor: str) -> None:
        self._set(item_id, owner=owner)
        self.note(item_id, actor, "assigned", owner)

    def set_session(self, item_id: int, session_id: str) -> None:
        self._set(item_id, session_id=session_id)

    def move(self, item_id: int, to: str, actor: str, detail: str = "") -> Item:
        """Move an item along the lifecycle. Raises RuleBroken on a bad move."""
        item = self.get(item_id)
        toms_call = actor == self.tom and to in ("closed", "dropped") and item.state not in ("closed", "dropped")
        if to not in LIFECYCLE.get(item.state, set()) and not toms_call:
            raise RuleBroken(f"item {item_id} is '{item.state}' and cannot become '{to}'")
        if to == "approved" and actor != self.tom:
            raise RuleBroken("only Tom can approve an item")
        scope = self.scope(item_id) if to in ("proposed", "approved") else None
        if scope and (to == "proposed" or not scope.spent):
            # What is proposed is fixed by propose_change or propose_action, and the
            # yes is Tom's reaction on that message (approve_scope). No move stands in for either.
            raise RuleBroken("this item has a proposal; propose again with the propose tool, "
                             "and only Tom's yes on that message approves it")
        if to == "closed" and item.state in ("noticed", "investigated") and not toms_call:
            # Small talk and plain questions end with an answer. Work never does:
            # that still goes done -> verified -> closed.
            if self.last_sender(item_id) in (None, self.tom, "alerts"):
                raise RuleBroken("answer in the room first; an item closes here only after your answer")
            if self.scope(item_id) is not None:
                raise RuleBroken("this item has a proposal, so it cannot be closed as answered")
            if not detail.strip():
                raise RuleBroken("say in one line what you answered")
        if to == "verified" and not detail.strip():
            raise RuleBroken("say what was checked and what the check showed")
        if to == "dropped" and item.origin == "tom" and actor != self.tom:
            raise RuleBroken("only Tom can drop something Tom asked for")
        if to in AWAITING_TOM:
            self._set(item_id, state=to, reminded_ts=None)  # a new ask gets its own reminder
        else:
            self._set(item_id, state=to)
        self.note(item_id, actor, f"-> {to}", detail)
        return self.get(item_id)

    def _set(self, item_id: int, **fields) -> None:
        fields["updated_ts"] = self.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))
        self.db.commit()

    def note(self, item_id: int | None, actor: str, what: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO log(item_id, ts, actor, what, detail) VALUES (?,?,?,?,?)",
            (item_id, self.now(), actor, what, detail),
        )
        self.db.commit()

    def history(self, item_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM log WHERE item_id=? ORDER BY id", (item_id,)).fetchall()

    # -- scopes --------------------------------------------------------------

    def set_scope(self, item_id: int, kind: str, hosts: list[str], actor: str, rev: str = "", unit: str = "",
                  files: list | None = None, event_id: str | None = None) -> None:
        """Fix what a yes on this item covers, replacing any earlier scope, and
        put the item in 'proposed'. A new proposal always needs a new yes."""
        if kind not in ("change", "restart", "reboot"):
            raise ValueError(kind)
        item = self.get(item_id)
        self.db.execute(
            "INSERT OR REPLACE INTO scopes(item_id, kind, rev, unit, hosts, files, done_hosts, event_id, set_ts)"
            " VALUES (?,?,?,?,?,?,'[]',?,?)",  # approved_ts is left empty: no yes yet
            (item_id, kind, rev, unit, json.dumps(hosts), json.dumps(files or []), event_id, self.now()),
        )
        self._set(item_id, state="proposed", reminded_ts=None)
        self.note(item_id, actor, "-> proposed",
                  "a new proposal replaced the approved one" if item.state == "approved" else "")

    def scope(self, item_id: int) -> Scope | None:
        row = self.db.execute("SELECT * FROM scopes WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            return None
        return Scope(row["kind"], row["rev"], row["unit"], json.loads(row["hosts"]), json.loads(row["files"]),
                     json.loads(row["done_hosts"]), row["event_id"], row["set_ts"], row["approved_ts"])

    def mark_done(self, item_id: int, host: str) -> None:
        """The scoped action succeeded on this host."""
        scope = self.scope(item_id)
        if scope is None or host in scope.done_hosts:
            return
        self.db.execute("UPDATE scopes SET done_hosts=? WHERE item_id=?",
                        (json.dumps(scope.done_hosts + [host]), item_id))
        self.db.commit()

    def approve_scope(self, item_id: int, actor: str) -> None:
        """Tom said yes on the proposal message itself. The caller checks which
        message he answered; this only refuses anyone who is not Tom."""
        if actor != self.tom:
            raise RuleBroken("only Tom can approve an item")
        if self.scope(item_id) is None:
            raise RuleBroken(f"item {item_id} has no proposal to approve")
        self.db.execute("UPDATE scopes SET approved_ts=? WHERE item_id=?", (self.now(), item_id))
        self._set(item_id, state="approved")
        self.note(item_id, actor, "-> approved")

    def clear_approval(self, item_id: int) -> None:
        """Tom's yes no longer stands: he said no, steered, or took it back."""
        self.db.execute("UPDATE scopes SET approved_ts=NULL WHERE item_id=?", (item_id,))
        self.db.commit()

    def yes_stands(self, scope: Scope | None) -> bool:
        """Did Tom say yes to exactly this proposal, less than a day ago? Checked
        when the yes is used, so a lapse does not wait for anyone to notice it."""
        return (scope is not None and scope.approved_ts is not None
                and 0 <= self.now() - scope.approved_ts < APPROVAL_LASTS)

    # -- messages ------------------------------------------------------------

    def may_send_unprompted(self) -> bool:
        """Rule 9: one unprompted item a day at most."""
        n = self.db.execute(
            "SELECT COUNT(DISTINCT item_id) FROM messages WHERE unprompted=1 AND ts>=?",
            (_day_start(self.now()),),
        ).fetchone()[0]
        return n == 0

    def is_unprompted(self, item: Item, kind: str) -> bool:
        """The first message on an item the staff started on its own.

        Tom's request is a prompt, and so is an alert: an item either of them
        opened may always be talked about. The kind does not matter: a member
        picks the kind, so a rule that only counted pitches would be a rule
        the member could step around.
        """
        if item.origin != "staff" or item.urgent or kind in EXEMPT_KINDS:
            return False
        already = self.db.execute(
            "SELECT 1 FROM messages WHERE item_id=? AND unprompted=1", (item.id,)
        ).fetchone()
        return already is None  # later messages on the same item are follow-through

    def check_send(self, item_id: int, kind: str) -> bool:
        """Call before posting. Returns whether the message counts as unprompted.

        Raises RuleBroken if today's unprompted item was already sent.
        """
        item = self.get(item_id)
        unprompted = self.is_unprompted(item, kind)
        if unprompted and not self.may_send_unprompted():
            raise RuleBroken("today's one unprompted item was already sent; hold this until tomorrow")
        return unprompted

    def record_message(self, event_id: str, item_id: int, sender: str, kind: str, unprompted: bool = False) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO messages(event_id, item_id, sender, kind, unprompted, ts) VALUES (?,?,?,?,?,?)",
            (event_id, item_id, sender, kind, int(unprompted), self.now()),
        )
        self.db.commit()

    def item_for_event(self, event_id: str) -> Item | None:
        """The item a room message belongs to: how a reply or reaction finds its target."""
        row = self.db.execute("SELECT item_id FROM messages WHERE event_id=?", (event_id,)).fetchone()
        return self.get(row[0]) if row else None

    def awaiting_tom(self) -> list[Item]:
        """Items with a question, pitch, or proposal out to Tom, newest first."""
        return sorted(
            (i for i in self.open_items() if i.state in AWAITING_TOM),
            key=lambda i: i.updated_ts,
            reverse=True,
        )

    def last_ask(self, item_id: int) -> str | None:
        """Event id of the newest message on this item that asked Tom something."""
        row = self.db.execute(
            "SELECT event_id FROM messages WHERE item_id=? AND kind IN ('question','pitch','proposal')"
            " ORDER BY ts DESC, rowid DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row[0] if row else None

    def last_staff_message(self) -> str | None:
        """Event id of the newest thing a member said in the room, on any item.
        The front desk's own reminders and notices do not count."""
        marks = ",".join("?" * len(EXEMPT_KINDS))
        row = self.db.execute(
            f"SELECT event_id FROM messages WHERE sender NOT IN (?, 'alerts') AND kind NOT IN ({marks})"
            " ORDER BY ts DESC, rowid DESC LIMIT 1",
            (self.tom, *sorted(EXEMPT_KINDS)),
        ).fetchone()
        return row[0] if row else None

    def last_sender(self, item_id: int) -> str | None:
        """Who wrote the newest room message on this item."""
        row = self.db.execute(
            "SELECT sender FROM messages WHERE item_id=? ORDER BY ts DESC, rowid DESC LIMIT 1", (item_id,)
        ).fetchone()
        return row[0] if row else None

    def desk_item(self) -> Item:
        """The item the front desk's own notices hang on. It is kept closed, so
        nobody is woken for it and a thumbs-up on a notice approves nothing."""
        row = self.db.execute("SELECT * FROM items WHERE alert_key=? ORDER BY id LIMIT 1", (DESK_KEY,)).fetchone()
        if row is None:
            now = self.now()
            self.db.execute(
                "INSERT INTO items(title, origin, alert_key, state, created_ts, updated_ts)"
                " VALUES ('Front desk notices','staff',?,'closed',?,?)",
                (DESK_KEY, now, now),
            )
            self.db.commit()
            return self.desk_item()
        return self._item(row)

    def mark_reminded(self, item_id: int) -> None:
        self._set(item_id, reminded_ts=self.now())

    # -- money ---------------------------------------------------------------

    def record_spend(self, member: str, model: str, usd: float, item_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO spend(ts, member, item_id, model, usd) VALUES (?,?,?,?,?)",
            (self.now(), member, item_id, model, usd),
        )
        self.db.commit()

    def spent(self, since: float) -> float:
        return self.db.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE ts>=?", (since,)).fetchone()[0]

    def spent_today(self) -> float:
        return self.spent(_day_start(self.now()))

    def spent_this_month(self) -> float:
        return self.spent(_month_start(self.now()))

    def budget_left(self, daily_cap: float, monthly_cap: float) -> float:
        """Dollars a member may still spend right now. Zero or less means stop."""
        return min(daily_cap - self.spent_today(), monthly_cap - self.spent_this_month())
