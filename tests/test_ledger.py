import pytest

from staff import policy
from staff.ledger import APPROVAL_LASTS, Ledger, RuleBroken

TOM = "@tom:example.org"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(tmp_path, clock):
    return Ledger(tmp_path / "t.db", tom=TOM, clock=clock)


def test_happy_path_needs_tom_and_a_check(ledger):
    item, new = ledger.open("pool tank at 91%", "alert", "frontdesk", alert_key="silverstone/zfs/tank")
    assert new
    ledger.move(item.id, "investigated", "fleet")
    ledger.move(item.id, "proposed", "fleet")
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "approved", "fleet")  # a member cannot approve
    ledger.move(item.id, "approved", TOM)
    ledger.move(item.id, "done", "fleet")
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "closed", "fleet")  # done is not the finish line
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "verified", "fleet")  # must say what was checked
    ledger.move(item.id, "verified", "fleet", "zpool list shows 71%")
    assert ledger.move(item.id, "closed", "fleet").state == "closed"


def test_cannot_skip_approval(ledger):
    item, _ = ledger.open("x", "staff", "fleet")
    ledger.move(item.id, "investigated", "fleet")
    ledger.move(item.id, "pitched", "fleet")
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "done", "fleet")


def test_member_cannot_drop_toms_request(ledger):
    item, _ = ledger.open("rip the new disc", "tom", TOM)
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "dropped", "media")
    ledger.move(item.id, "dropped", TOM)


def test_same_alert_key_is_said_once(ledger):
    a, new_a = ledger.open("tank 91%", "alert", "frontdesk", alert_key="k")
    b, new_b = ledger.open("tank 92%", "alert", "frontdesk", alert_key="k")
    assert new_a and not new_b and a.id == b.id
    ledger.move(a.id, "dropped", "fleet")
    c, new_c = ledger.open("tank 93%", "alert", "frontdesk", alert_key="k")
    assert not new_c and c.id == a.id  # a "no" holds: the dropped problem is not announced again
    u, new_u = ledger.open("tank 99%", "alert", "frontdesk", alert_key="k", urgent=True)
    assert new_u  # unless it has become urgent


def test_fixed_problem_can_come_back(ledger, clock):
    a, _ = ledger.open("tank 91%", "alert", "frontdesk", alert_key="k")
    ledger.move(a.id, "closed", TOM)
    b, new = ledger.open("tank 91%", "alert", "frontdesk", alert_key="k")
    assert new and b.id != a.id
    ledger.move(b.id, "dropped", TOM)
    clock.t += 8 * 86400
    _, new = ledger.open("tank 91%", "alert", "frontdesk", alert_key="k")
    assert new  # a "no" holds for a week, not forever


def test_embedding_match_is_a_repeat(ledger):
    a, _ = ledger.open("eink render timing out", "alert", "frontdesk", embedding=[1.0, 0.0, 0.1])
    b, new = ledger.open("eink job timeout again", "staff", "house", embedding=[0.98, 0.05, 0.1])
    assert not new and b.id == a.id
    c, new = ledger.open("garage AP offline", "alert", "frontdesk", embedding=[0.0, 1.0, 0.0])
    assert new


def test_toms_requests_are_never_merged(ledger):
    ledger.open("look at the eink panel", "tom", TOM, embedding=[1.0, 0.0])
    _, new = ledger.open("look at the eink panel", "tom", TOM, embedding=[1.0, 0.0])
    assert new


def test_one_unprompted_item_a_day(ledger, clock):
    a, _ = ledger.open("a", "staff", "scout")
    b, _ = ledger.open("b", "staff", "scout")
    al, _ = ledger.open("pool tank at 91%", "alert", "frontdesk")
    assert ledger.check_send(a.id, "pitch") is True
    ledger.record_message("$1", a.id, "scout", "pitch", unprompted=True)
    assert ledger.check_send(a.id, "pitch") is False  # follow-through on the same item
    for kind in ("chat", "result", "proposal"):  # the kind is the member's choice, so every kind counts
        with pytest.raises(RuleBroken):
            ledger.check_send(b.id, kind)
    assert ledger.check_send(b.id, "reminder") is False
    for kind in ("chat", "result", "proposal"):  # an alert is a prompt: its item is never held
        assert ledger.check_send(al.id, kind) is False
    clock.t += 86400
    assert ledger.check_send(b.id, "proposal") is True


def test_replies_to_tom_and_urgent_do_not_count(ledger):
    a, _ = ledger.open("a", "staff", "scout")
    ledger.record_message("$1", a.id, "scout", "pitch", unprompted=True)
    t, _ = ledger.open("fix my thing", "tom", TOM)
    u, _ = ledger.open("monza down", "alert", "frontdesk", urgent=True)
    assert ledger.check_send(t.id, "proposal") is False
    assert ledger.check_send(u.id, "proposal") is False


def test_reaction_finds_its_item(ledger):
    a, _ = ledger.open("a", "staff", "fleet")
    ledger.record_message("$abc", a.id, "fleet", "proposal")
    assert ledger.item_for_event("$abc").id == a.id
    assert ledger.item_for_event("$nope") is None
    assert ledger.last_ask(a.id) == "$abc"


def test_budget(ledger, clock):
    ledger.record_spend("fleet", "claude-sonnet-5-5", 1.25)
    assert ledger.budget_left(3.0, 95.0) == pytest.approx(1.75)
    ledger.record_spend("fleet", "claude-opus-5-5", 2.0)
    assert ledger.budget_left(3.0, 95.0) < 0
    clock.t += 86400
    assert ledger.spent_today() == 0


def test_policy_levels():
    assert policy.check_bash("journalctl -u synapse -n 200").level == policy.FREE
    assert policy.check_bash("ssh spa systemctl status synapse").level == policy.FREE
    v = policy.check_bash("ssh spa sudo systemctl restart synapse")
    assert v.level == policy.NEEDS_YES and not v.allowed("proposed") and v.allowed("approved")
    assert policy.check_bash("sudo reboot").level == policy.NEEDS_YES
    never = policy.check_bash("sudo zfs destroy tank/media")
    assert never.level == policy.TOM_ONLY and not never.allowed("approved")
    assert policy.check_bash("echo x >> staff/policy.py").level == policy.TOM_ONLY
    assert policy.check_bash("cat /run/secrets/matrix").level == policy.TOM_ONLY


def test_nudge():
    h = 3600
    assert policy.nudge("proposed", 0, None, now=2 * h) is None
    assert policy.nudge("proposed", 0, None, now=21 * h) == "remind"
    assert policy.nudge("proposed", 0, 21 * h, now=30 * h) is None
    assert policy.nudge("proposed", 0, 21 * h, now=70 * h) == "shelve"
    assert policy.nudge("approved", 0, None, now=99 * h) is None


# -- regression tests for the review fixes -------------------------------------


@pytest.mark.parametrize("origin", ["staff", "alert"])
@pytest.mark.parametrize("kind", ["chat", "result", "pitch", "reminder", "system"])
def test_one_unprompted_item_a_day_counts_any_kind(ledger, origin, kind):  # finding 7
    first, _ = ledger.open("first", "staff", "scout")
    second, _ = ledger.open("second", origin, "scout", alert_key="h/s/k" if origin == "alert" else None)
    ledger.record_message("$1", first.id, "scout", "chat", ledger.check_send(first.id, "chat"))
    if kind in ("reminder", "system") or origin == "alert":  # an alert counts as a prompt
        assert ledger.check_send(second.id, kind) is False
    else:
        with pytest.raises(RuleBroken):
            ledger.check_send(second.id, kind)


def test_tom_items_and_urgent_items_are_never_held(ledger):  # finding 7
    first, _ = ledger.open("first", "staff", "scout")
    ledger.record_message("$1", first.id, "scout", "chat", ledger.check_send(first.id, "chat"))
    toms, _ = ledger.open("his", "tom", TOM)
    urgent, _ = ledger.open("fire", "alert", "frontdesk", alert_key="h/s/fire", urgent=True)
    for item in (toms, urgent):
        for kind in ("chat", "result", "pitch"):
            assert ledger.check_send(item.id, kind) is False


# -- what a yes covers -----------------------------------------------------------


def approved(ledger):
    item, _ = ledger.open("fix the proxy", "alert", "frontdesk", alert_key="monza/nginx/x")
    ledger.move(item.id, "investigated", "fleet")
    ledger.move(item.id, "proposed", "fleet")
    ledger.move(item.id, "approved", TOM)
    return item.id


def test_scope_is_set_read_and_marked_done(ledger, clock):
    iid = approved(ledger)
    assert ledger.scope(iid) is None
    files = [{"path": "hosts/monza.nix", "added": 2, "removed": 1}]
    ledger.set_scope(iid, "change", ["suzuka", "monza"], "fleet", rev="a" * 40, files=files, event_id="$p1")
    scope = ledger.scope(iid)
    assert (scope.kind, scope.rev, scope.hosts, scope.files) == ("change", "a" * 40, ["suzuka", "monza"], files)
    assert scope.event_id == "$p1" and scope.set_ts == clock.t and scope.done_hosts == [] and not scope.spent
    ledger.mark_done(iid, "suzuka")
    ledger.mark_done(iid, "suzuka")
    assert ledger.scope(iid).done_hosts == ["suzuka"] and not ledger.scope(iid).spent
    ledger.mark_done(iid, "monza")
    assert ledger.scope(iid).spent


def test_a_new_scope_replaces_the_old_one_and_needs_a_new_yes(ledger):
    iid = approved(ledger)
    ledger.set_scope(iid, "change", ["suzuka"], "fleet", rev="a" * 40)
    assert ledger.get(iid).state == "proposed"  # the yes was for something else
    ledger.approve_scope(iid, TOM)
    ledger.mark_done(iid, "suzuka")
    ledger.set_scope(iid, "restart", ["monza"], "fleet", unit="nginx.service")
    scope = ledger.scope(iid)
    assert (scope.kind, scope.rev, scope.unit, scope.hosts, scope.done_hosts) == ("restart", "", "nginx.service", ["monza"], [])
    assert ledger.get(iid).state == "proposed"
    assert ledger.history(iid)[-1]["what"] == "-> proposed"
    with pytest.raises(ValueError):
        ledger.set_scope(iid, "format", ["monza"], "fleet")


def test_setting_a_scope_proposes_the_item_and_no_move_can(ledger):
    item, _ = ledger.open("x", "staff", "fleet")
    ledger.move(item.id, "investigated", "fleet")
    ledger.set_scope(item.id, "reboot", ["spa"], "fleet", event_id="$p")
    assert ledger.get(item.id).state == "proposed"
    ledger.move(item.id, "investigated", "fleet")
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "proposed", "fleet")  # only a propose tool proposes an item with a scope
    ledger._set(item.id, state="proposed")
    with pytest.raises(RuleBroken):
        ledger.move(item.id, "approved", TOM)  # and only a yes on the proposal approves it
    assert ledger.scope(item.id).approved_ts is None


def test_the_yes_lives_on_the_scope_and_lasts_a_day(ledger, clock):
    item, _ = ledger.open("x", "staff", "fleet")
    ledger.move(item.id, "investigated", "fleet")
    with pytest.raises(RuleBroken):
        ledger.approve_scope(item.id, TOM)  # nothing proposed
    ledger.set_scope(item.id, "reboot", ["spa"], "fleet", event_id="$p")
    assert not ledger.yes_stands(ledger.scope(item.id))
    with pytest.raises(RuleBroken):
        ledger.approve_scope(item.id, "fleet")
    ledger.approve_scope(item.id, TOM)
    assert ledger.get(item.id).state == "approved" and ledger.scope(item.id).approved_ts == clock.t
    assert ledger.yes_stands(ledger.scope(item.id))
    clock.t += APPROVAL_LASTS + 1
    assert not ledger.yes_stands(ledger.scope(item.id))  # lapsed with nobody looking
    ledger.approve_scope(item.id, TOM)
    ledger.clear_approval(item.id)
    assert not ledger.yes_stands(ledger.scope(item.id))
    ledger.approve_scope(item.id, TOM)
    ledger.set_scope(item.id, "reboot", ["monza"], "fleet", event_id="$q")  # a new proposal: no yes yet
    assert ledger.scope(item.id).approved_ts is None and ledger.get(item.id).state == "proposed"


def test_last_staff_message_skips_tom_alerts_and_the_desks_own(ledger, clock):
    item, _ = ledger.open("x", "alert", "frontdesk")
    assert ledger.last_staff_message() is None
    ledger.record_message("$p", item.id, "@fleet:x", "proposal")
    for event_id, sender, kind in (("$t", TOM, "chat"), ("$a", "alerts", "intake"), ("$r", "@fleet:x", "reminder")):
        clock.t += 1
        ledger.record_message(event_id, item.id, sender, kind)
    assert ledger.last_staff_message() == "$p"
    ledger.record_message("$c", item.id, "@fleet:x", "chat")  # same second: the later row wins
    assert ledger.last_staff_message() == "$c"
