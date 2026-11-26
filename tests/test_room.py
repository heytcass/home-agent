import pytest

from staff.config import Config, Member
from staff.ledger import Ledger, RuleBroken
from staff.room import Alert, Incoming, Reaction, Room, check_no_paths, classify, markdown_to_html

TOM = "@tom:example.org"
ALERTS = "@alerts:example.org"
CHIEF = "@chief:example.org"
ROOM = "!room:example.org"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        homeserver="http://localhost:8008",
        room_id=ROOM,
        tom=TOM,
        alerts_user=ALERTS,
        alert_field="org.example.alert",
        db_path=str(tmp_path / "t.db"),
        memory_dir="",
        members_dir="",
        members={
            "chief": Member("chief", CHIEF, "", "m"),
            "fleet": Member("fleet", "@fleet:example.org", "", "m"),
        },
    )


def msg(sender, body="hi", eid="$1", **content):
    return {
        "type": "m.room.message",
        "event_id": eid,
        "sender": sender,
        "origin_server_ts": 1_800_000_000_000,
        "content": {"msgtype": "m.text", "body": body, **content},
    }


def test_tom_message(cfg):
    got = classify(cfg, msg(TOM, "check the pool"))
    assert got == Incoming("$1", TOM, "check the pool", None, 1_800_000_000.0)


def test_reply_and_fallback_stripped(cfg):
    ev = msg(TOM, "> <@chief:example.org> which ones?\n\nthe big ones", **{"m.relates_to": {"m.in_reply_to": {"event_id": "$0"}}})
    got = classify(cfg, ev)
    assert got.reply_to == "$0" and got.text == "the big ones"


def test_members_never_dispatched(cfg):
    assert classify(cfg, msg(CHIEF)) is None
    assert classify(cfg, msg("@fleet:example.org")) is None


def test_strangers_ignored(cfg):
    assert classify(cfg, msg("@eve:example.org")) is None
    react = {"type": "m.reaction", "event_id": "$r", "sender": "@eve:example.org",
             "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$1", "key": "x"}}}
    assert classify(cfg, react) is None


def test_edits_ignored(cfg):
    ev = msg(TOM, "* fixed", **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$1"}})
    assert classify(cfg, ev) is None


def test_tom_notice_ignored(cfg):
    assert classify(cfg, msg(TOM, msgtype="m.notice")) is None


def test_reaction(cfg):
    ev = {"type": "m.reaction", "event_id": "$r", "sender": TOM,
          "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$1", "key": "\U0001F44D"}}}
    assert classify(cfg, ev) == Reaction("$r", TOM, "$1", "\U0001F44D")


def test_alert(cfg):
    ev = msg(ALERTS, "tank at 91%", "$a", **{"org.example.alert": {
        "v": 1, "host": "silverstone", "source": "zfs", "key": "tank", "level": "warn", "urgent": True, "ts": 1}})
    got = classify(cfg, ev)
    assert isinstance(got, Alert) and got.urgent and got.alert_key == "silverstone/zfs/tank"


def test_alerts_account_without_field_ignored(cfg):
    assert classify(cfg, msg(ALERTS)) is None


def test_tom_cannot_fake_alert(cfg):
    ev = msg(TOM, "x", **{"org.example.alert": {"host": "h"}})
    assert isinstance(classify(cfg, ev), Incoming)


def test_path_refusal():
    for bad in ["see /var/log/syslog", "it is in /home/tom/x.txt", "(/nix/store/abc-foo/bin)", "at /srv/data", "/mnt/nas/a"]:
        with pytest.raises(RuleBroken):
            check_no_paths(bad)
    check_no_paths("```\ncat /var/log/syslog\n```\nsee that")
    check_no_paths("https://example.com/var/log and and/or")


def test_markdown():
    assert markdown_to_html("**a** and *b* and `c<d`") == "<p><strong>a</strong> and <em>b</em> and <code>c&lt;d</code></p>"
    assert markdown_to_html("one\ntwo") == "<p>one<br>two</p>"
    assert markdown_to_html("- x\n- y") == "<ul><li>x</li><li>y</li></ul>"
    assert markdown_to_html("[t](https://e.org/a?b=1)") == '<p><a href="https://e.org/a?b=1">t</a></p>'
    assert "<a" not in markdown_to_html("[t](javascript:alert(1))")
    assert markdown_to_html("<script>") == "<p>&lt;script&gt;</p>"
    assert markdown_to_html("hi\n```\n*a* <b>\n```") == "<p>hi</p><pre><code>*a* &lt;b&gt;\n</code></pre>"
    assert markdown_to_html("**`x`**") == "<p><strong><code>x</code></strong></p>"


class FakeClient:
    def __init__(self):
        self.sent = []

    async def room_send(self, room_id, type_, content):
        self.sent.append((room_id, type_, content))
        return type("R", (), {"event_id": "$sent"})()


@pytest.fixture
def room(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.db", tom=TOM)
    r = Room(cfg, ledger)
    r._clients["fleet"] = FakeClient()
    return r


async def test_say_records_and_sends(room):
    item, _ = room.ledger.open("x", "tom", "chief")
    eid = await room.say("fleet", item.id, "chat", "hello **there**", reply_to="$0")
    assert eid == "$sent"
    _, type_, content = room._clients["fleet"].sent[0]
    assert type_ == "m.room.message"
    assert content["body"] == "hello **there**"
    assert content["format"] == "org.matrix.custom.html"
    assert content["formatted_body"] == "<p>hello <strong>there</strong></p>"
    assert content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$0"}}
    assert room.ledger.item_for_event("$sent").id == item.id


async def test_say_refuses_when_rule_broken(room):
    first, _ = room.ledger.open("a", "staff", "chief")
    second, _ = room.ledger.open("b", "staff", "chief")
    await room.say("fleet", first.id, "pitch", "one")
    with pytest.raises(RuleBroken):
        await room.say("fleet", second.id, "pitch", "two")
    assert len(room._clients["fleet"].sent) == 1
    assert room.ledger.item_for_event("$sent").id == first.id


async def test_say_refuses_path(room):
    item, _ = room.ledger.open("x", "tom", "chief")
    with pytest.raises(RuleBroken):
        await room.say("fleet", item.id, "chat", "look at /var/log/foo")
    assert room._clients["fleet"].sent == []


@pytest.mark.parametrize("urgent", ["true", 1, "yes", [True], None])
def test_only_a_real_true_makes_an_alert_urgent(cfg, urgent):  # review E
    ev = msg(ALERTS, "tank at 91%", "$a", **{"org.example.alert": {
        "v": 1, "host": "silverstone", "source": "zfs", "key": "tank", "level": "warn", "urgent": urgent, "ts": 1}})
    got = classify(cfg, ev)
    assert isinstance(got, Alert) and got.urgent is False


def test_text_cannot_bring_its_own_code_marks():  # review K
    assert markdown_to_html("a \x000\x00 b") == markdown_to_html("a 0 b")
    html = markdown_to_html("\x005\x00 and `real` code")
    assert "<code>real</code>" in html and "\x00" not in html


@pytest.mark.parametrize("body", ["fleetgo ahead and pitch it.", "fleet go ahead and pitch it.", "Fleetgo ahead and pitch it."])
def test_a_mention_names_the_member_and_is_set_apart(cfg, body):
    pill = '<a href="https://matrix.to/#/@fleet:example.org">fleet</a>go ahead and pitch it.'
    got = classify(cfg, msg(TOM, body, formatted_body=pill, **{"m.mentions": {"user_ids": ["@fleet:example.org"]}}))
    assert got.mentions == ("fleet",) and got.text.lower() == "fleet go ahead and pitch it."


def test_a_pill_alone_counts_as_a_mention(cfg):
    pill = '<a href="https://matrix.to/#/%40fleet%3Aexample.org">Fleet Bot</a>check spa'
    got = classify(cfg, msg(TOM, "Fleet Botcheck spa", formatted_body=pill))
    assert got.mentions == ("fleet",) and got.text == "Fleet Bot check spa"


def test_no_mention_leaves_the_text_alone(cfg):
    got = classify(cfg, msg(TOM, "the fleeting chiefdom", **{"m.mentions": {"user_ids": ["@nobody:example.org"]}}))
    assert got.mentions == () and got.text == "the fleeting chiefdom"
