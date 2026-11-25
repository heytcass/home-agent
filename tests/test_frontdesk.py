import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from staff.config import Config, Member
from staff.frontdesk import FrontDesk
from staff import policy
from staff.ledger import Ledger
from staff.room import Alert, check_no_paths

TOM = "@tom:example.org"


class Clock:
    t = 1_800_000_000.0  # 2027-01-15 03:00 local-ish; tests set the hour they need

    def __call__(self):
        return self.t


class FakeRoom:
    def __init__(self, ledger):
        self.ledger, self.said, self.reactions, self.n = ledger, [], [], 0

    async def say(self, member, item_id, kind, text, reply_to=None):
        unprompted = self.ledger.check_send(item_id, kind)
        self.n += 1
        eid = f"$out{self.n}"
        self.ledger.record_message(eid, item_id, member, kind, unprompted)
        self.said.append((member, item_id, kind, text, reply_to))
        return eid

    async def react(self, member, event_id, emoji):
        self.reactions.append((member, event_id, emoji))

    async def typing(self, member, on):
        pass


@dataclass
class Result:
    ok: bool = True
    handoff: str | None = None
    error: str = ""


class FakeRunner:
    def __init__(self):
        self.runs, self.result = [], Result()

    async def run(self, member, item_id, prompt):
        self.runs.append((member, item_id, prompt))
        return self.result


class FakeLocal:
    owner = "fleet"

    async def embed(self, text):
        return None

    async def route(self, text, members, min_confidence=0.6):
        return self.owner


@pytest.fixture
def desk(tmp_path):
    cfg = Config(
        homeserver="", room_id="!r", tom=TOM, alerts_user="@alerts:example.org",
        db_path=str(tmp_path / "t.db"), memory_dir="", members_dir="",
        members={n: Member(n, f"@{n}:example.org", "", "m") for n in ("chief", "fleet")},
    )
    clock = Clock()
    ledger = Ledger(cfg.db_path, tom=TOM, clock=clock)

    async def triage(text):
        return None, "fleet"

    d = FrontDesk(cfg, ledger, FakeRoom(ledger), FakeRunner(), FakeLocal(), triage=triage, clock=clock)
    d.clock = clock
    return d


async def drain(desk):
    """Run queued work the way a worker would, until nothing is left."""
    while not desk.queue.empty():
        member, item_id, prompt = desk.queue.get_nowait()
        await desk._work(member, item_id, prompt)
        desk.busy.discard(item_id)
        more = desk.pending.pop(item_id, None)
        if more:
            desk.wake(item_id, "\n\n".join(more))


def msg(text, event_id="$in1", reply_to=None):
    return SimpleNamespace(event_id=event_id, sender=TOM, text=text, reply_to=reply_to, ts=0)


def alert(text="pool tank at 91%", key="tank", event_id="$a1", urgent=False):
    return SimpleNamespace(event_id=event_id, text=text, host="silverstone", source="zfs", key=key,
                           level="warn", urgent=urgent, alert_key=f"silverstone/zfs/{key}")


async def test_request_is_claimed_and_worked(desk):
    await desk.on_message(msg("why is suzuka slow?"))
    assert desk.room.reactions == [("chief", "$in1", "👀")]
    await drain(desk)
    member, item_id, prompt = desk.runner.runs[0]
    assert member == "fleet" and "why is suzuka slow?" in prompt
    assert desk.ledger.get(item_id).origin == "tom"


async def test_alert_is_said_once(desk):
    await desk.on_alert(alert(event_id="$a1"))
    await desk.on_alert(alert("pool tank at 92%", event_id="$a2"))
    await drain(desk)
    assert len(desk.runner.runs) == 1
    assert len(desk.ledger.open_items()) == 1


async def test_thumbs_up_approves_and_starts_the_work(desk):
    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, "proposed", "fleet")
    ask = await desk.room.say("fleet", item.id, "proposal", "Delete old snapshots? 👍 / 👎")
    await desk.on_reaction(SimpleNamespace(event_id="$r1", sender=TOM, target=ask, key="👍🏽"))
    assert desk.ledger.get(item.id).state == "approved"
    await drain(desk)
    assert "Tom said yes" in desk.runner.runs[-1][2]


async def test_thumbs_down_drops_without_a_run(desk):
    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, "pitched", "fleet")
    ask = await desk.room.say("fleet", item.id, "pitch", "Idea")
    runs = len(desk.runner.runs)
    await desk.on_reaction(SimpleNamespace(event_id="$r1", sender=TOM, target=ask, key="👎"))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "dropped" and len(desk.runner.runs) == runs


async def test_reply_steers_the_right_item(desk):
    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, "proposed", "fleet")
    ask = await desk.room.say("fleet", item.id, "proposal", "Delete old snapshots?")
    await desk.on_message(msg("keep the monthly ones", event_id="$in2", reply_to=ask))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "investigated"
    assert desk.runner.runs[-1][1] == item.id and "keep the monthly ones" in desk.runner.runs[-1][2]
    assert len(desk.ledger.open_items()) == 1  # no new item was made


async def test_one_reminder_then_shelved(desk):
    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, "proposed", "fleet")
    await desk.room.say("fleet", item.id, "proposal", "Delete old snapshots?")
    import time
    # move to noon the next day so quiet hours do not hold the reminder
    t = time.localtime(desk.clock.t + 86400)
    desk.clock.t = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
    await desk.tend()
    await desk.tend()
    reminders = [s for s in desk.room.said if s[2] == "reminder"]
    assert len(reminders) == 1 and reminders[0][4] == "$out1"
    desk.clock.t += 49 * 3600
    await desk.tend()
    assert desk.ledger.get(item.id).state == "shelved"


async def test_cap_is_said_once(desk):
    desk.runner.result = Result(ok=False, error="budget")
    desk.ledger.record_spend("fleet", "m", 99.0)
    await desk.on_message(msg("one"))
    await desk.on_message(msg("two", event_id="$in2"))
    await drain(desk)
    assert len([s for s in desk.room.said if "cap" in s[3]]) == 1


async def test_handoff_wakes_the_new_owner(desk):
    await desk.on_message(msg("something"))
    item = desk.ledger.open_items()[0]

    async def run(member, item_id, prompt):
        desk.runner.runs.append((member, item_id, prompt))
        if member == "fleet":
            desk.ledger.assign(item_id, "chief", "fleet")
            return Result(handoff="chief")
        return Result()

    desk.runner.run = run
    await drain(desk)
    assert [r[0] for r in desk.runner.runs] == ["fleet", "chief"]
    assert item.id == desk.runner.runs[1][1]


# -- regression tests for the review fixes -------------------------------------


def at(desk, hour, days=0):
    """Set the clock to a local hour, `days` after the current day."""
    t = time.localtime(desk.clock.t + days * 86400)
    desk.clock.t = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, 0, 0, 0, 0, -1))


async def proposed(desk, key="tank", state="proposed"):
    """An alert item whose ask is out to Tom. -> (item, ask event id)"""
    await desk.on_alert(alert(key=key, event_id=f"$a-{key}"))
    await drain(desk)
    item = next(i for i in desk.ledger.open_items() if i.alert_key == f"silverstone/zfs/{key}")
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, state, "fleet")
    ask = f"$ask-{key}"  # recorded directly: two asks in a day would trip the one-unprompted-item rule
    desk.ledger.record_message(ask, item.id, "fleet", "proposal")
    return item, ask


async def approve(desk):
    item, ask = await proposed(desk)
    await desk.on_reaction(SimpleNamespace(event_id="$r1", sender=TOM, target=ask, key="👍"))
    await drain(desk)
    return item, ask


@pytest.mark.parametrize("how", ["thumbs_down", "reply_no"])
async def test_taking_back_a_yes(desk, how):  # finding 4
    item, ask = await approve(desk)
    assert desk.ledger.get(item.id).state == "approved"
    runs = len(desk.runner.runs)
    if how == "thumbs_down":
        await desk.on_reaction(SimpleNamespace(event_id="$r2", sender=TOM, target=ask, key="👎"))
    else:
        await desk.on_message(msg("no", event_id="$in2", reply_to=ask))
    state = desk.ledger.get(item.id).state
    assert state == "investigated"
    await drain(desk)
    assert len(desk.runner.runs) == runs + 1 and "took back his yes" in desk.runner.runs[-1][2]
    assert not policy.check_bash("sudo systemctl restart x").allowed(state)


async def test_second_thumbs_up_does_nothing(desk):  # finding 4
    item, ask = await approve(desk)
    runs = len(desk.runner.runs)
    await desk.on_reaction(SimpleNamespace(event_id="$r2", sender=TOM, target=ask, key="👍"))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "approved" and len(desk.runner.runs) == runs


async def test_handoff_ping_pong_stops(desk):  # finding 5
    await desk.on_message(msg("something"))
    item = desk.ledger.open_items()[0]

    async def run(member, item_id, prompt):
        desk.runner.runs.append((member, item_id, prompt))
        other = "chief" if member == "fleet" else "fleet"
        desk.ledger.assign(item_id, other, member)
        return Result(handoff=other)

    desk.runner.run = run
    await drain(desk)
    assert len(desk.runner.runs) <= 4
    assert "handoff held" in [r["what"] for r in desk.ledger.history(item.id)]


async def answered(desk, hour=12, moved=True):
    """Tom asks; fleet answers in the room and leaves the item 'investigated'
    (or, with moved=False, forgets to move it at all)."""
    at(desk, hour)

    async def run(member, item_id, prompt):
        desk.runner.runs.append((member, item_id, prompt))
        desk.answer = await desk.room.say(member, item_id, "chat", "answer")
        if moved:
            desk.ledger.move(item_id, "investigated", member)
        return Result()

    desk.runner.run = run
    await desk.on_message(msg("why is suzuka slow?"))
    await drain(desk)
    return desk.ledger.open_items()[0]


@pytest.mark.parametrize("moved", [True, False])
async def test_answered_item_is_not_woken_then_shelved(desk, moved):  # finding 6a
    item = await answered(desk, moved=moved)
    kept = item.state
    base = desk.clock.t
    for hours in (1, 5, 24, 30, 48, 70):
        desk.clock.t = base + hours * 3600
        await desk.tend()
        await drain(desk)
    assert len(desk.runner.runs) == 1 and desk.ledger.get(item.id).state == kept
    desk.clock.t = base + 73 * 3600
    await desk.tend()
    await drain(desk)
    assert len(desk.runner.runs) == 1 and desk.ledger.get(item.id).state == "shelved"


@pytest.mark.parametrize("hour,urgent,woken", [(12, False, True), (3, False, False), (3, True, True)])
async def test_noticed_item_woken_by_day_not_night(desk, hour, urgent, woken):  # finding 6b
    at(desk, hour)
    await desk.on_alert(alert(urgent=urgent))
    await drain(desk)
    desk.clock.t += 10 * 60
    await desk.tend()
    await drain(desk)
    assert len(desk.runner.runs) == 1  # not stale yet
    desk.clock.t += 21 * 60
    await desk.tend()
    await drain(desk)
    assert len(desk.runner.runs) == (2 if woken else 1)


@pytest.mark.parametrize("how", ["thumbs_up", "thanks"])
async def test_thanks_closes_an_answered_item(desk, how):  # finding 6c
    item = await answered(desk)
    if how == "thumbs_up":
        await desk.on_reaction(SimpleNamespace(event_id="$r1", sender=TOM, target=desk.answer, key="👍"))
    else:
        await desk.on_message(msg("thanks", event_id="$in2", reply_to=desk.answer))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "closed" and len(desk.runner.runs) == 1


async def test_proposed_without_a_recorded_ask_gets_no_reminder(desk):  # finding 7
    at(desk, 12)
    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, "proposed", "fleet")  # nothing was ever said
    at(desk, 12, days=1)
    await desk.tend()
    assert desk.ledger.get(item.id).state == "investigated" and desk.room.said == []


async def test_repeat_of_a_dropped_alert_is_quiet(desk):  # finding 8
    item, _ = await proposed(desk)
    desk.ledger.move(item.id, "dropped", TOM)
    runs = len(desk.runner.runs)
    await desk.on_alert(alert("pool tank at 92%", event_id="$a2"))
    await drain(desk)
    n = desk.ledger.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert len(desk.runner.runs) == runs and desk.ledger.open_items() == [] and n == 1


async def test_alert_after_a_closed_item_is_new(desk):  # finding 8
    item, _ = await proposed(desk)
    desk.ledger.move(item.id, "approved", TOM)
    desk.ledger.move(item.id, "done", "fleet")
    desk.ledger.move(item.id, "verified", "fleet", "zpool list shows 71%")
    desk.ledger.move(item.id, "closed", "fleet")
    runs = len(desk.runner.runs)
    await desk.on_alert(alert("pool tank at 92%", event_id="$a2"))
    await drain(desk)
    assert len(desk.runner.runs) == runs + 1 and len(desk.ledger.open_items()) == 1


@pytest.mark.parametrize("end", ["dropped", "closed"])
async def test_reply_on_a_finished_item_opens_a_new_one(desk, end):  # finding 9
    item, ask = await proposed(desk)
    if end == "dropped":
        desk.ledger.move(item.id, "dropped", TOM)
    else:
        desk.ledger.move(item.id, "approved", TOM)
        desk.ledger.move(item.id, "done", "fleet")
        desk.ledger.move(item.id, "verified", "fleet", "checked")
        desk.ledger.move(item.id, "closed", "fleet")
    runs = len(desk.runner.runs)
    await desk.on_message(msg("it came back, please redo it", event_id="$in2", reply_to=ask))
    await drain(desk)
    new = desk.ledger.open_items()
    assert len(new) == 1 and new[0].id != item.id and new[0].origin == "tom"
    assert f"#{item.id}" in new[0].body and "it came back, please redo it" in new[0].body
    assert len(desk.runner.runs) == runs + 1 and desk.runner.runs[-1][1] == new[0].id
    assert desk.ledger.get(item.id).state == end


async def test_reminder_state_resets_on_a_new_ask(desk):  # finding 10
    item, ask = await proposed(desk)
    at(desk, 12, days=1)
    await desk.tend()
    assert desk.ledger.get(item.id).reminded_ts is not None
    await desk.on_message(msg("keep the monthly ones", event_id="$in2", reply_to=ask))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "investigated"
    at(desk, 12, days=10)
    desk.ledger.move(item.id, "proposed", "fleet")
    await desk.room.say("fleet", item.id, "proposal", "Keep the monthly ones, delete the rest?")
    assert desk.ledger.get(item.id).reminded_ts is None
    desk.clock.t += 10 * 60
    await desk.tend()
    assert desk.ledger.get(item.id).state == "proposed"


async def test_one_failing_reminder_does_not_stop_tend(desk):  # finding 11
    first, _ = await proposed(desk, key="one")
    second, _ = await proposed(desk, key="two")
    say = desk.room.say

    async def flaky(member, item_id, kind, text, reply_to=None):
        if item_id == first.id and kind == "reminder":
            raise RuntimeError("homeserver down")
        return await say(member, item_id, kind, text, reply_to)

    desk.room.say = flaky
    at(desk, 12, days=1)
    await desk.tend()
    assert desk.ledger.get(second.id).reminded_ts is not None
    assert [s[1] for s in desk.room.said if s[2] == "reminder"] == [second.id]


async def test_reminder_names_no_title_or_path(desk):  # finding 11
    await desk.on_alert(alert("disk full on /var/lib/postgresql", key="disk"))
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    desk.ledger.move(item.id, "proposed", "fleet")
    await desk.room.say("fleet", item.id, "proposal", "Prune the WAL?")
    at(desk, 12, days=1)
    await desk.tend()
    texts = [s[3] for s in desk.room.said if s[2] == "reminder"]
    assert len(texts) == 1 and "postgresql" not in texts[0]
    check_no_paths(texts[0])


@pytest.mark.parametrize("host,source,key", [("", "zfs", "tank"), ("silverstone", "", "tank"), ("silverstone", "zfs", "")])
async def test_half_labeled_alerts_never_merge(desk, host, source, key):  # finding 12
    def make(text, eid):
        return Alert(event_id=eid, text=text, host=host, source=source, key=key, level="warn", urgent=False)

    assert make("a", "$a1").alert_key is None
    await desk.on_alert(make("pool tank at 91%", "$a1"))
    await desk.on_alert(make("the router lost its uplink", "$a2"))
    assert len(desk.ledger.open_items()) == 2


async def test_triage_makes_no_model_call_when_budget_is_used_up(desk):  # finding 13c
    async def boom(prompt):
        raise AssertionError("router called with no budget")

    desk._ask_router = boom
    await proposed(desk, state="proposed")
    desk.ledger.record_spend("fleet", "m", 99.0)
    assert await desk._triage_with_model("something new") == (None, "fleet")


async def test_bare_ok_with_two_waiting_approves_neither(desk):  # finding 14
    async def triage(text):
        raise AssertionError("triage must not pick what Tom approves")

    desk.triage = triage
    a, _ = await proposed(desk, key="one")
    b, _ = await proposed(desk, key="two")
    runs = len(desk.runner.runs)
    await desk.on_message(msg("ok", event_id="$in9"))
    await drain(desk)
    assert [desk.ledger.get(i.id).state for i in (a, b)] == ["proposed", "proposed"]
    assert len(desk.runner.runs) == runs
    asks = [s for s in desk.room.said if s[2] == "system"]
    assert len(asks) == 1 and "Which one" in asks[0][3]


async def test_bare_ok_with_one_waiting_approves_it(desk):  # finding 14
    async def triage(text):
        raise AssertionError("triage must not pick what Tom approves")

    desk.triage = triage
    item, _ = await proposed(desk)
    await desk.on_message(msg("ok", event_id="$in9"))
    assert desk.ledger.get(item.id).state == "approved"
    await drain(desk)
    assert "Tom said yes" in desk.runner.runs[-1][2]


async def test_cap_notice_hangs_on_the_closed_desk_item(desk):  # finding 15
    item, ask = await proposed(desk)
    desk.runner.result = Result(ok=False, error="budget")
    desk.ledger.record_spend("fleet", "m", 99.0)
    desk.wake(item.id, "go")
    await drain(desk)
    notice = next(s for s in desk.room.said if "cap" in s[3])
    desk_item = desk.ledger.desk_item()
    assert notice[1] == desk_item.id and desk_item.state == "closed"
    runs = len(desk.runner.runs)
    await desk.on_reaction(SimpleNamespace(event_id="$r1", sender=TOM, target=f"$out{desk.room.n}", key="👍"))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "proposed" and desk.ledger.get(desk_item.id).state == "closed"
    assert len(desk.runner.runs) == runs


# -- a yes covers one proposal ------------------------------------------------------


def thumb(target, key="👍", event_id="$r9"):
    return SimpleNamespace(event_id=event_id, sender=TOM, target=target, key=key)


async def rescoped(desk):
    """An item whose first proposal ($ask-tank) was replaced by a second ($new). -> item"""
    item, _ = await proposed(desk)
    desk.ledger.record_message("$new", item.id, "fleet", "proposal")
    desk.ledger.set_scope(item.id, "change", ["suzuka"], "fleet", rev="a" * 40, event_id="$new")
    return item


@pytest.mark.parametrize("how", ["thumbs_up", "reply_yes"])
async def test_a_yes_on_a_replaced_proposal_approves_nothing(desk, how):
    item = await rescoped(desk)
    runs = len(desk.runner.runs)
    if how == "thumbs_up":
        await desk.on_reaction(thumb("$ask-tank"))
    else:
        await desk.on_message(msg("yes", event_id="$in5", reply_to="$ask-tank"))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "proposed" and len(desk.runner.runs) == runs
    member, _, kind, text, reply_to = desk.room.said[-1]
    assert (member, kind, reply_to) == ("chief", "system", "$new") and "approves nothing" in text
    assert desk.ledger.scope(item.id).approved_ts is None

    await desk.on_reaction(thumb("$new", event_id="$r10"))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "approved" and len(desk.runner.runs) == runs + 1


async def test_a_yes_on_the_old_proposal_does_not_approve_after_the_scope_changed_under_a_yes(desk):
    item, ask = await approve(desk)
    desk.ledger.record_message("$new", item.id, "fleet", "proposal")
    desk.ledger.set_scope(item.id, "reboot", ["monza"], "fleet", event_id="$new")
    assert desk.ledger.get(item.id).state == "proposed"
    await desk.on_reaction(thumb(ask))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "proposed"


async def test_a_no_keeps_the_scope_and_a_later_yes_on_the_same_proposal_means_the_same(desk):
    item = await rescoped(desk)
    await desk.on_reaction(thumb("$new"))
    await desk.on_reaction(thumb("$new", key="👎", event_id="$r10"))  # took it back
    await drain(desk)
    assert desk.ledger.get(item.id).state == "investigated"
    scope = desk.ledger.scope(item.id)
    assert scope.event_id == "$new" and scope.rev == "a" * 40  # still there, but nothing runs on it
    runs = len(desk.runner.runs)
    desk.clock.t += 3600
    await desk.on_reaction(thumb("$new", event_id="$r11"))
    await drain(desk)
    assert desk.ledger.get(item.id).state == "approved"
    assert desk.ledger.scope(item.id).approved_ts == desk.clock.t  # a fresh yes, not the old one
    assert len(desk.runner.runs) == runs + 1 and "Tom said yes" in desk.runner.runs[-1][2]


async def test_a_yes_on_a_used_up_proposal_approves_nothing(desk):
    item = await rescoped(desk)
    await desk.on_reaction(thumb("$new"))
    desk.ledger.mark_done(item.id, "suzuka")
    await desk.on_reaction(thumb("$new", key="👎", event_id="$r10"))
    await desk.on_reaction(thumb("$new", event_id="$r11"))
    assert desk.ledger.get(item.id).state != "approved"


# -- the router ------------------------------------------------------------------------


@pytest.fixture
async def api(monkeypatch):
    """A fake Messages API. -> (requests seen, answer to change)"""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    seen = []
    answer = {"status": 200, "body": {
        "content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": '{"item": null, "owner": "chief"}'}],
        "usage": {"input_tokens": 1_000_000, "output_tokens": 200_000},
    }}

    async def messages(request):
        seen.append((dict(request.headers), await request.json()))
        return web.json_response(answer["body"], status=answer["status"])

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", str(server.make_url("")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield seen, answer
    await server.close()


async def test_router_is_one_plain_messages_call(desk, api):
    seen, answer = api
    item, _ = await proposed(desk)
    assert await desk._triage_with_model("the dishwasher is beeping") == (None, "chief")
    headers, body = seen[0]
    assert headers["x-api-key"] == "test-key" and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == desk.cfg.router_model and body["max_tokens"] <= 1024
    assert "the dishwasher is beeping" in body["messages"][0]["content"]
    assert desk.ledger.spent_today() == pytest.approx(0.10 + 0.2 * 0.50)  # haiku 5.5, from usage

    answer["body"] = {"content": [{"type": "text", "text": f'{{"item": {item.id}, "owner": "nobody"}}'}], "usage": {}}
    assert await desk._triage_with_model("yes do that") == (item.id, None)


@pytest.mark.parametrize("status,body", [(529, {"type": "error"}), (200, {"content": [{"type": "text", "text": "no idea"}]})])
async def test_router_failure_falls_back(desk, api, status, body):
    _, answer = api
    answer.update(status=status, body=body)
    await proposed(desk)
    assert await desk._triage_with_model("hello") == (None, None)


async def test_router_without_a_key_or_a_server_falls_back(desk, monkeypatch):
    await proposed(desk)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert await desk._triage_with_model("hello") == (None, None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    assert await desk._triage_with_model("hello") == (None, None)


def test_frontdesk_never_imports_the_sdk():
    import subprocess
    import sys

    code = "import sys, staff.frontdesk, staff.runner, staff.doorclient; sys.exit('claude_agent_sdk' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# -- review A: the yes is bound to the proposal ------------------------------------------


async def test_a_yes_on_a_chat_message_does_not_reboot_monza(desk):  # review A, as reproduced
    from staff.runner import Runner

    asked = []

    async def door(request):
        asked.append(request)
        return {"ok": True}

    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    runner = Runner(desk.cfg, desk.ledger, desk.room, door=door)
    tools = {t.name: t.handler for t in runner.tools("fleet", item.id, {}, ("propose_action", "act"))}
    out = await tools["propose_action"]({"verb": "reboot", "host": "monza", "unit": "",
                                         "text": "Kernel update. Reboot monza?"})
    assert not out.get("is_error"), out
    proposal = desk.ledger.scope(item.id).event_id

    await desk.on_message(msg("don't reboot", event_id="$in2", reply_to=proposal))
    await drain(desk)
    assert (await tools["move"]({"state": "proposed", "detail": "asked again"}))["is_error"]
    assert not (await tools["say"]({"kind": "chat", "text": "I'll only read the logs, OK?"})).get("is_error")
    await desk.on_reaction(thumb(f"$out{desk.room.n}"))
    await drain(desk)

    out = await tools["act"]({})
    assert out["is_error"] and "has not said yes" in out["content"][0]["text"]
    assert asked == [] and desk.ledger.scope(item.id).approved_ts is None
    assert desk.ledger.get(item.id).state != "approved"


async def test_a_yes_on_the_proposal_is_the_one_the_door_is_sent(desk):  # review A
    from staff.runner import Runner

    asked = []

    async def door(request):
        asked.append(request)
        return {"ok": True}

    await desk.on_alert(alert())
    await drain(desk)
    item = desk.ledger.open_items()[0]
    desk.ledger.move(item.id, "investigated", "fleet")
    runner = Runner(desk.cfg, desk.ledger, desk.room, door=door)
    tools = {t.name: t.handler for t in runner.tools("fleet", item.id, {}, ("propose_action", "act"))}
    await tools["propose_action"]({"verb": "reboot", "host": "monza", "unit": "", "text": "Reboot monza?"})
    proposal = desk.ledger.scope(item.id).event_id
    await tools["say"]({"kind": "chat", "text": "It takes about two minutes."})

    await desk.on_reaction(thumb(f"$out{desk.room.n}"))  # a yes on the chat line: a notice, and no approval
    member, _, kind, text, reply_to = desk.room.said[-1]
    assert (member, kind, reply_to) == ("chief", "system", proposal) and "approves nothing" in text
    assert (await tools["act"]({}))["is_error"] and asked == []

    desk.clock.t += 60
    await desk.on_reaction(thumb(proposal, event_id="$r10"))
    yes = desk.clock.t
    desk.clock.t += 60
    assert not (await tools["act"]({})).get("is_error")
    assert asked == [{"op": "reboot", "host": "monza", "item_id": item.id, "urgent": False, "approved_ts": yes}]


@pytest.mark.parametrize("words", ["wait, which host is that?", "no", "👎"])
async def test_anything_but_a_yes_on_the_proposal_ends_the_yes(desk, words):  # review A
    item = await rescoped(desk)
    await desk.on_reaction(thumb("$new"))
    assert desk.ledger.yes_stands(desk.ledger.scope(item.id))
    if words == "👎":
        await desk.on_reaction(thumb("$new", key=words, event_id="$r10"))
    else:
        await desk.on_message(msg(words, event_id="$in5", reply_to="$new"))
    scope = desk.ledger.scope(item.id)
    assert scope.approved_ts is None and not desk.ledger.yes_stands(scope)
    assert desk.ledger.get(item.id).state == "investigated"


async def test_a_bare_yes_counts_only_when_the_ask_was_said_last(desk):  # review A
    item = await rescoped(desk)
    desk.clock.t += 60
    desk.ledger.record_message("$later", item.id, "fleet", "chat")  # the member said something after asking
    await desk.on_message(msg("ok", event_id="$in5"))
    member, _, kind, text, reply_to = desk.room.said[-1]
    assert (member, kind, reply_to) == ("chief", "system", "$in5") and "React 👍" in text
    assert desk.ledger.get(item.id).state == "proposed" and desk.ledger.scope(item.id).approved_ts is None

    desk.ledger.record_message("$newer", item.id, "fleet", "proposal")
    desk.ledger.set_scope(item.id, "change", ["suzuka"], "fleet", rev="b" * 40, event_id="$newer")
    await desk.on_message(msg("ok", event_id="$in6"))  # the proposal is the last thing said: no thread hunting
    assert desk.ledger.get(item.id).state == "approved"
    assert desk.ledger.scope(item.id).approved_ts == desk.clock.t


async def asked(desk, kind="question"):
    """Tom asked for something and the chief answered with a question of the given kind."""
    await desk.on_message(msg("look into the kiosk updates"))
    await drain(desk)
    item = desk.ledger.open_items()[0]
    await desk.room.say("chief", item.id, kind, "I can't reach it. Should I drop this? Yes or no.")
    desk.ledger.asked(item.id, kind, "chief")
    return item


@pytest.mark.parametrize("how", ["bare", "reply", "thumbs_down"])
async def test_a_no_to_a_question_goes_back_to_the_member(desk, how):
    item = await asked(desk)
    assert desk.ledger.get(item.id).state == "waiting"
    ask, runs = desk.ledger.last_ask(item.id), len(desk.runner.runs)
    if how == "thumbs_down":
        await desk.on_reaction(SimpleNamespace(event_id="$r1", sender=TOM, target=ask, key="👎"))
    else:
        await desk.on_message(msg("no", event_id="$in2", reply_to=ask if how == "reply" else None))
    assert desk.ledger.get(item.id).state == "investigated"  # not dropped: he may have meant "keep it"
    await drain(desk)
    assert len(desk.ledger.open_items()) == 1 and len(desk.runner.runs) == runs + 1
    assert "answered no to the question you asked" in desk.runner.runs[-1][2]


@pytest.mark.parametrize("word", ["no", "yes"])
async def test_a_stray_yes_or_no_lands_on_what_the_staff_last_said(desk, word):
    async def triage(text):
        raise AssertionError("a bare yes or no is never a new request while a member spoke last")

    item = await asked(desk, kind="chat")  # the member asked without marking it: nothing is waiting
    desk.triage = triage
    assert desk.ledger.awaiting_tom() == []
    await desk.on_message(msg(word, event_id="$in2"))
    await drain(desk)
    after = desk.ledger.get(item.id)
    assert len(desk.ledger.open_items()) == 1 and after.state == item.state  # no new item, nothing approved
    assert desk.runner.runs[-1][1] == item.id and word in desk.runner.runs[-1][2]


async def test_a_stray_no_after_tom_spoke_last_is_a_new_message(desk):
    item = await asked(desk, kind="chat")
    await desk.on_message(msg("also check the disks", event_id="$in2"))
    await desk.on_message(msg("no", event_id="$in3"))
    assert len(desk.ledger.open_items()) == 3 and desk.ledger.get(item.id).state == item.state
