"""The front desk: the one always-running piece.

It listens to the room and to existing alerts, keeps the item list, and
wakes a member only when there is work. It makes no judgment calls about
the homelab; it only decides which item something belongs to and who is
woken. Everything that follows is the member's work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import aiohttp

from . import policy
from .config import Config
from .ledger import APPROVAL_LASTS, AWAITING_TOM, DESK_KEY, Ledger, RuleBroken

log = logging.getLogger("frontdesk")

YES = {"👍", "👍️", "✅", "✔️"}
NO = {"👎", "👎️", "❌"}
YES_WORDS = re.compile(r"^\s*(yes|yep|yeah|do it|go|go ahead|approved?|ship it|ok|okay)[.! ]*$", re.I)
NO_WORDS = re.compile(r"^\s*(no|nope|drop it|don'?t|skip it|not now)[.! ]*$", re.I)

THANKS = re.compile(r"^\s*(thanks|thank you|thx|ty|got it|perfect|great|nice|all good)[.! ]*$", re.I)

ANSWERED_KEPT = 72 * 3600  # an answered item Tom never came back to is shelved after this
MAX_HANDOFFS_PER_DAY = 2  # per item, so two members cannot pass it back and forth
STALE_AFTER = 30 * 60  # an item with work owed and nobody on it
MAX_WAKES_PER_DAY = 3  # per item, so a stuck item cannot burn the budget
QUIET = (21, 8)  # no reminders between 21:00 and 08:00
WORK_OWED = {"noticed", "investigated", "approved", "done", "verified"}
# Dollars per million tokens (input, output), to record what the router call cost.
ROUTER_PRICES = {"claude-haiku-5-5": (0.10, 0.50)}
UNKNOWN_PRICE = (5.00, 25.00)  # a model not listed is counted high: the cap must err toward stopping early


def strip_marks(emoji: str) -> str:
    """Drop skin-tone and variation marks so 👍🏽 reads as 👍."""
    return re.sub("[\U0001f3fb-\U0001f3ff️]", "", emoji)


class FrontDesk:
    def __init__(self, cfg: Config, ledger: Ledger, room, runner, local, triage=None, clock=time.time):
        self.cfg, self.ledger, self.room, self.runner, self.local = cfg, ledger, room, runner, local
        self.triage = triage or self._triage_with_model
        self.now = clock
        self.queue: asyncio.Queue[tuple[str, int, str]] = asyncio.Queue()
        self.busy: set[int] = set()  # items being worked right now
        self.pending: dict[int, list[str]] = {}  # things said while the item was busy

    # -- what comes in -------------------------------------------------------

    async def on_message(self, msg) -> None:
        """Tom wrote in the room. Claim it at once, then work out what it is about."""
        await self._quietly(self.room.react("chief", msg.event_id, "👀"))
        item = self.ledger.item_for_event(msg.reply_to) if msg.reply_to else None
        owner, body, look_up, target, stray = None, msg.text, item is None, msg.reply_to, False
        if item is not None and item.alert_key == DESK_KEY:
            item, look_up = None, True  # a reply to a front desk notice is just a message
        elif item is not None and not item.is_open and item.state != "shelved":
            # The item he replied on is finished. His words are a new request about it.
            body = f"Follow-up to item #{item.id} ({item.title}), which is {item.state}:\n\n{msg.text}"
            owner, item = item.owner, None
        if look_up and (YES_WORDS.match(msg.text) or NO_WORDS.match(msg.text)):
            # A bare yes or no that points at nothing. A model must never pick what it approves.
            waiting = self.ledger.awaiting_tom()
            if len(waiting) > 1:
                await self._quietly(self.room.say(
                    "chief", self.ledger.desk_item().id, "system",
                    "Which one is that for? Reply to its message, or react 👍 or 👎 on it.",
                    reply_to=msg.event_id,
                ))
                return
            item, look_up = (waiting[0], False) if waiting else (None, True)
            if item is None:
                # Nothing is marked as waiting, but a member spoke last in the room: he is
                # answering that. It reaches the member as a reply and approves nothing.
                item = self.ledger.last_spoken_item()
                look_up, stray = item is None, item is not None
            if item is not None and not stray and YES_WORDS.match(msg.text):
                # The one thing waiting on him, and no thread hunting: his yes is for its ask,
                # but only if that ask is the last thing a member said. Anything said after it
                # could be what he is answering, and a guess must never approve.
                scope = self.ledger.scope(item.id)
                target = scope.event_id if scope and not scope.spent else self.ledger.last_ask(item.id)
                if target is None or target != self.ledger.last_staff_message():
                    await self._notice("Something else was said after that was asked, so I can't tell what "
                                       "this yes is for. React 👍 on the message you mean.", msg.event_id)
                    return
        if look_up:
            item_id, owner = await self.triage(msg.text)
            item = self.ledger.get(item_id) if item_id else None
        if item is None:
            item, _ = self.ledger.open(msg.text[:120], "tom", msg.sender, body=body)
            self.ledger.assign(item.id, owner or "chief", "frontdesk")
            self.ledger.record_message(msg.event_id, item.id, msg.sender, "intake")
            self.wake(item.id, f"Tom wrote this in the room:\n\n{msg.text}")
            return
        # Answer first: whether a member spoke last is judged before Tom's own message is recorded.
        if stray and YES_WORDS.match(msg.text) and item.state != "approved":
            self.wake(item.id, f"Tom replied to what you last said on this item:\n\n{msg.text}\n\n"
                               "It answers your message. It approves no change.")
        else:
            await self._answer(item, msg.text, msg.sender, target)
        self.ledger.record_message(msg.event_id, item.id, msg.sender, "chat")

    async def on_reaction(self, r) -> None:
        item = self.ledger.item_for_event(r.target)
        if item is None:
            return
        key = strip_marks(r.key)
        if key in YES:
            await self._answer(item, "yes", r.sender, r.target)
        elif key in NO:
            await self._answer(item, "no", r.sender, r.target)

    async def _answer(self, item, text: str, sender: str, target: str | None = None) -> None:
        """Tom answered on an item: a yes, a no, or steering. `target` is the
        message he reacted or replied to, if any."""
        if not item.is_open and item.state != "shelved":
            return
        awaiting = item.state in AWAITING_TOM or item.state == "shelved"
        yes, no = YES_WORDS.match(text), NO_WORDS.match(text)
        scope = self.ledger.scope(item.id)
        if scope is not None and not scope.spent:
            # A proposal is out. A yes covers what that one message says and nothing else,
            # so it counts only on that message; whatever else Tom says ends a yes he gave.
            if yes and target == scope.event_id:
                if not self.ledger.yes_stands(scope):
                    self.ledger.approve_scope(item.id, sender)
                    self.wake(item.id, "Tom said yes. Do it, check that it worked, and report the result.")
                return
            if yes and item.state in ("pitched", "proposed"):
                await self._notice("A yes counts only on the proposal itself, so this approves nothing. "
                                   "This is the one to 👍.", scope.event_id)
                return
            if not yes and not THANKS.match(text):
                self.ledger.clear_approval(item.id)
                if item.state == "approved" and not no:
                    self.ledger.move(item.id, "investigated", sender, "Tom replied, so his yes no longer stands")
                    item = self.ledger.get(item.id)
        if item.state == "approved" and (yes or no):
            if no:  # Tom took his yes back; the door tools read the scope on every call
                self.ledger.move(item.id, "investigated", sender, "Tom took back his yes")
                self.wake(item.id, "Tom took back his yes. Stop: run nothing more that needs approval, "
                                   "and tell him where things stand.")
            return
        if not awaiting and (yes or THANKS.match(text)):
            # A thumbs-up on an answer means "that's all": close it and wake nobody.
            if item.state in ("noticed", "investigated", "done", "verified") and self._member_spoke_last(item):
                self.ledger.move(item.id, "closed", sender, "Tom closed it")
            return
        if awaiting and YES_WORDS.match(text):
            if item.state in ("pitched", "proposed"):
                self.ledger.move(item.id, "approved", sender)
                self.wake(item.id, "Tom said yes. Do it, check that it worked, and report the result.")
            else:
                self._back_to_work(item, sender)
                self.wake(item.id, "Tom confirmed your best guess. Carry on.")
        elif awaiting and NO_WORDS.match(text) and item.state == "waiting":
            # A no to a question is an answer, and only the member knows what was asked:
            # "should I drop this?" and "should I go on?" take opposite meanings from it.
            self._back_to_work(item, sender)
            self.wake(item.id, "Tom answered no to the question you asked. Work out what a no means for "
                               "that question. If it means stop, tell him in one line that you stopped, then "
                               "end the item with move(): dropped if you may, otherwise closed.")
        elif awaiting and NO_WORDS.match(text):
            self.ledger.move(item.id, "dropped", sender, "Tom said no")
        else:
            if awaiting:
                self._back_to_work(item, sender)
            self.wake(
                item.id,
                f"Tom replied on this item:\n\n{text}\n\nIf this is steering that should hold from now on, "
                "save it with the remember tool and tell him the rule you saved.",
            )

    async def _notice(self, text: str, reply_to: str | None) -> None:
        """A short word from the chief, on the desk's own item so a 👍 on it approves nothing."""
        await self._quietly(self.room.say("chief", self.ledger.desk_item().id, "system", text, reply_to=reply_to))

    def _back_to_work(self, item, actor: str) -> None:
        if item.state != "investigated":
            self.ledger.move(item.id, "investigated", actor)

    async def on_alert(self, alert) -> None:
        """An existing alert arrived. A repeat of an open item is attached, never re-announced."""
        embedding = await self.local.embed(alert.text)
        item, new = self.ledger.open(
            alert.text[:120], "alert", "frontdesk", body=alert.text,
            alert_key=alert.alert_key, embedding=embedding, urgent=alert.urgent,
        )
        self.ledger.record_message(alert.event_id, item.id, "alerts", "intake")
        if not new:
            return
        owner = await self.local.route(alert.text, self._owners()) or "chief"
        self.ledger.assign(item.id, owner, "frontdesk")
        self.wake(item.id, f"This alert just arrived:\n\n{alert.text}\n\nInvestigate before saying anything.")

    # -- triage --------------------------------------------------------------

    def _owners(self) -> dict[str, str]:
        return {name: OWNS.get(name, name) for name in self.cfg.members}

    async def _triage_with_model(self, text: str) -> tuple[int | None, str | None]:
        """Which open item is this about (or none), and who should own a new one."""
        waiting = self.ledger.awaiting_tom()
        if not waiting or self.ledger.budget_left(self.cfg.daily_cap_usd, self.cfg.monthly_cap_usd) <= 0:
            return None, await self.local.route(text, self._owners())
        listing = "\n".join(f"{i.id}: [{i.owner}] {i.title}" for i in waiting[:15])
        prompt = (
            "Tom wrote a message in the staff room. Decide whether it answers one of the items waiting "
            "on him or is a new request.\n\n"
            f"Items waiting on Tom:\n{listing}\n\nMembers: {json.dumps(self._owners())}\n\n"
            f"Tom's message:\n{text}\n\n"
            'Reply with JSON only: {"item": <id or null>, "owner": "<member for a new request>"}'
        )
        try:
            reply = await self._ask_router(prompt)
            data = json.loads(re.search(r"\{.*\}", reply, re.S).group(0))
            item_id = data.get("item")
            if item_id not in {i.id for i in waiting}:
                item_id = None
            owner = data.get("owner") if data.get("owner") in self.cfg.members else None
            return item_id, owner
        except Exception as e:  # the room must keep working if routing fails
            log.warning("triage failed: %s", e)
            return None, None

    async def _ask_router(self, prompt: str) -> str:
        """One small Messages API call. Returns the reply's text and records what it cost."""
        base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/") or "https://api.anthropic.com"
        headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}
        # Room for a short think before a one-line answer; the reply itself is about 20 tokens.
        body = {"model": self.cfg.router_model, "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}]}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            async with http.post(f"{base}/v1/messages", json=body, headers=headers) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"router call failed with status {resp.status}")
        usage = data.get("usage") or {}
        price_in, price_out = ROUTER_PRICES.get(self.cfg.router_model, UNKNOWN_PRICE)
        usd = (usage.get("input_tokens", 0) * price_in + usage.get("output_tokens", 0) * price_out) / 1e6
        self.ledger.record_spend("frontdesk", self.cfg.router_model, usd)
        return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")

    # -- waking members ------------------------------------------------------

    def wake(self, item_id: int, prompt: str) -> None:
        if item_id in self.busy:
            self.pending.setdefault(item_id, []).append(prompt)
            return
        self.busy.add(item_id)
        item = self.ledger.get(item_id)
        self.queue.put_nowait((item.owner or "chief", item_id, prompt))

    async def worker(self) -> None:
        while True:
            member, item_id, prompt = await self.queue.get()
            try:
                await self._work(member, item_id, prompt)
            except Exception:
                log.exception("work on item %s failed", item_id)
            finally:
                self.busy.discard(item_id)
                more = self.pending.pop(item_id, None)
                if more:
                    self.wake(item_id, "\n\n".join(more))

    async def _work(self, member: str, item_id: int, prompt: str) -> None:
        self.ledger.note(item_id, "frontdesk", "woken", member)
        await self._quietly(self.room.typing(member, True))
        try:
            result = await self.runner.run(member, item_id, prompt)
        finally:
            await self._quietly(self.room.typing(member, False))
        if not result.ok:
            self.ledger.note(item_id, "frontdesk", "run failed", result.error)
            if self.ledger.budget_left(self.cfg.daily_cap_usd, self.cfg.monthly_cap_usd) <= 0:
                await self._say_cap_hit(item_id)
            return
        if result.handoff and result.handoff != member:
            if self._noted(item_id, "assigned", 86400, by_staff=True) > MAX_HANDOFFS_PER_DAY:
                self.ledger.note(item_id, "frontdesk", "handoff held", "passed around too often today")
                return
            self.pending.setdefault(item_id, []).append(
                f"{member} handed this item to you. Read its log and take it from here."
            )

    async def _say_cap_hit(self, item_id: int) -> None:
        """The chief says once that the cap stopped work (brief: Budget)."""
        day = time.strftime("%Y-%m-%d", time.localtime(self.now()))
        seen = self.ledger.db.execute("SELECT 1 FROM log WHERE what='cap said' AND detail=?", (day,)).fetchone()
        if seen:
            return
        self.ledger.note(item_id, "frontdesk", "cap said", day)
        item_id = self.ledger.desk_item().id  # a thumbs-up on this notice must approve nothing
        text = (
            f"The spending cap is reached (${self.ledger.spent_today():.2f} today, "
            f"${self.ledger.spent_this_month():.2f} this month), so work is paused. "
            "Open items are kept and will pick up when the cap resets."
        )
        await self._quietly(self.room.say("chief", item_id, "system", text))

    # -- open work -----------------------------------------------------------

    async def tend(self) -> None:
        """Look at open items. Nothing here sends a message because a timer fired:
        an item gets one reminder if Tom has not answered, and an owner is woken
        if work is owed and nobody is on it."""
        now = self.now()
        hour = time.localtime(now).tm_hour
        quiet = hour >= QUIET[0] or hour < QUIET[1]
        for item in self.ledger.open_items():
            if item.id in self.busy:
                continue
            try:  # one bad item must not stop the rest from being looked at
                await self._tend_one(item, now, quiet)
            except Exception:
                log.exception("tending item %s failed", item.id)

    async def _tend_one(self, item, now: float, quiet: bool) -> None:
        action = policy.nudge(item.state, item.updated_ts, item.reminded_ts, now)
        if action == "remind":
            if not quiet:
                await self._remind(item)
        elif action == "shelve":
            self.ledger.move(item.id, "shelved", "frontdesk", "no answer after one reminder")
        elif quiet and not item.urgent:
            return  # nobody is woken at night for work that can wait
        elif item.state == "approved" and now - self._last(item.id, "-> approved") > APPROVAL_LASTS:
            self.ledger.clear_approval(item.id)
            self.ledger.move(item.id, "investigated", "frontdesk", "the yes was not used within a day")
            self.wake(item.id, "Tom's yes on this item is more than a day old and the work was not finished, "
                               "so it no longer counts. If it still needs doing, propose it again and say what stopped you.")
        elif item.state in WORK_OWED and now - item.updated_ts > STALE_AFTER:
            if item.state in ("noticed", "investigated") and self._member_spoke_last(item):
                # The owner answered and it is Tom's turn. Waking the owner again would only repeat it.
                # "noticed" counts too: a member that answered and forgot to move the item still answered.
                if now - item.updated_ts > ANSWERED_KEPT:
                    self.ledger.move(item.id, "shelved", "frontdesk", "answered, and nothing more came")
            elif self._noted(item.id, "held", 86400) and self._last(item.id, "held") >= self._day_start(now):
                return  # its message is held until tomorrow; another run today would only be refused again
            elif self._noted(item.id, "woken", 86400) < MAX_WAKES_PER_DAY:
                self.wake(item.id, f"This item is '{item.state}' and nobody is on it. Take the next step.")

    def _member_spoke_last(self, item) -> bool:
        return self.ledger.last_sender(item.id) not in (None, self.cfg.tom, "alerts")

    def _noted(self, item_id: int, what: str, within: float, by_staff: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM log WHERE item_id=? AND what=? AND ts>=?"
        if by_staff:
            sql += " AND actor!='frontdesk'"
        return self.ledger.db.execute(sql, (item_id, what, self.now() - within)).fetchone()[0]

    def _last(self, item_id: int, what: str) -> float:
        row = self.ledger.db.execute("SELECT MAX(ts) FROM log WHERE item_id=? AND what=?", (item_id, what)).fetchone()
        return row[0] or 0.0

    @staticmethod
    def _day_start(now: float) -> float:
        t = time.localtime(now)
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))

    async def _remind(self, item) -> None:
        ask = self.ledger.last_ask(item.id)
        if ask is None:  # the ask was refused or lost, so there is nothing to remind him of
            self.ledger.move(item.id, "investigated", "frontdesk", "the ask never reached Tom")
            return
        text = "Still waiting on you for this one. 👍, 👎, or reply to steer."
        try:
            await self.room.say(item.owner or "chief", item.id, "reminder", text, reply_to=ask)
            self.ledger.mark_reminded(item.id)
        except RuleBroken as e:
            log.info("reminder held: %s", e)

    async def tend_forever(self, every: float = 600) -> None:
        while True:
            try:
                await self.tend()
            except Exception:
                log.exception("tend failed")
            await asyncio.sleep(every)

    @staticmethod
    async def _quietly(coro) -> None:
        try:
            await coro
        except Exception as e:
            log.debug("ignored: %s", e)


OWNS = {
    "chief": "anything about the staff itself, how it works, or that fits no one else",
    "house": "Home Assistant, tablets, e-ink panels, voice, automations",
    "fleet": "NixOS hosts, the flake, deploys, backups, disks, services on the servers",
    "media": "disc ripper, the media library on the NAS, Music Assistant",
    "maintainer": "Tom's own code projects, their issues and pull requests",
    "scout": "new projects and new releases of things Tom runs",
    "network": "UniFi, Wi-Fi, the VPN relay, Meshtastic, radios",
}


async def amain() -> None:
    from .local import Local
    from .room import Room
    from .runner import Runner

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = Config.load()
    ledger = Ledger(cfg.db_path, tom=cfg.tom)
    room = Room(cfg, ledger)
    local = Local(cfg)
    desk = FrontDesk(cfg, ledger, room, Runner(cfg, ledger, room, embed=local.embed), local)
    try:
        await asyncio.gather(
            room.run(desk.on_message, desk.on_reaction, desk.on_alert),
            desk.worker(),
            desk.worker(),
            desk.tend_forever(),
        )
    finally:
        await local.close()
        await room.close()


def main() -> None:
    asyncio.run(amain())
