import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from staff import door as door_mod
from staff.door import Door, night_refusal, ssh_argv

REV = "a" * 40
REV2 = "b" * 40
MAIN = "c" * 40
NOON = datetime(2026, 11, 23, 12, 0)
NIGHT = datetime(2026, 11, 23, 2, 0)
NIX = Path(__file__).parent.parent / "nix"
YES = datetime(2026, 11, 22, 21, 0).timestamp()  # Tom said yes the evening before
OLD_SYS, NEW_SYS = "/nix/store/old", "/nix/store/new"  # what the fake host reports
OLD, NEW = "0" * 32 + "-old-system", "1" * 32 + "-new-system"  # store names for the root script


class Fake:
    """Stands in for git, nix, and the hosts. Records every call."""

    def __init__(self):
        self.t = NOON
        self.runs: list[list[str]] = []
        self.remotes: list[tuple[str, str, bytes | None]] = []
        self.tips: dict[str, str] = {}  # refs/heads/... -> rev
        self.heads = [REV]  # answers to successive `git rev-parse HEAD`
        self.porcelain = ""
        self.numstat = ""
        self.fail: dict[str, str] = {}  # "am", "nix", "push-main" -> stderr
        self.remote_rc: dict[str, int] = {}
        # A small model of one host's deploy state.
        self.host = {"current_system": OLD_SYS, "rev": "", "active_rev": "", "switch": "none",
                     "revert_pending": "no", "system_running": "running"}
        self.failed: list[str] = []  # names of the failed units
        self.failed_after_switch: list[str] | None = None
        self.revert_at: float | None = None  # when the host's own timer fires
        self.revert_fails = False
        self.diff = "diff --git a/x b/x\n"
        self.tree = "flake.nix\0nix/module.nix\0"
        self.build_polls = 1  # status polls after a switch that still say building
        self.healthy_after = 2  # status polls after a switch until it has succeeded; 0 is never
        self.reachable = True
        self.polls = 0
        self.gate: asyncio.Event | None = None  # set to block inside sleep

    def now(self):
        return self.t

    async def sleep(self, s):
        if self.gate:
            await self.gate.wait()
        self.t = datetime.fromtimestamp(self.t.timestamp() + s)

    async def run(self, argv, stdin=None, cwd=None):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), argv
        assert stdin is None or isinstance(stdin, bytes)
        self.runs.append(argv)
        if argv[0] == "nix":
            return (1, b"", self.fail["nix"]) if "nix" in self.fail else (0, b"", "")
        assert argv[0] == "git"
        args = argv[1:]
        while args[0] == "-c":
            args = args[2:]
        sub = args[0]
        if sub == "rev-parse":
            if args[1] == "HEAD":
                rev = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
                return 0, rev.encode() + b"\n", ""
            tip = self.tips.get(args[-1])
            return (0, tip.encode() + b"\n", "") if tip else (1, b"", "")
        if sub == "am" and "--abort" not in args and "am" in self.fail:
            return 1, b"", self.fail["am"]
        if sub == "branch":
            self.tips[f"refs/heads/{args[-2]}"] = args[-1]
        if sub == "status":
            return 0, self.porcelain.encode(), ""
        if sub == "diff":
            return 0, (self.numstat if "--numstat" in args else self.diff).encode(), ""
        if sub == "ls-tree":
            return 0, self.tree.encode(), ""
        if sub == "archive":
            return 0, b"TAR:" + args[-1].encode(), ""
        if sub == "push" and args[-1].endswith(":refs/heads/main") and "push-main" in self.fail:
            return 1, b"", self.fail["push-main"]
        return 0, b"", ""

    async def remote(self, host, verb_line, stdin=None):
        assert isinstance(verb_line, str)
        self.remotes.append((host, verb_line, stdin))
        words = verb_line.split(" ")
        verb = words[0]
        rc = self.remote_rc.get(verb, 0)
        if verb == "status":
            if not self.reachable:
                return 255, "ssh: connect to host: timed out"
            h = self.host
            if h["switch"] in ("building", "activating"):
                self.polls += 1
                if h["switch"] == "building" and self.polls > self.build_polls:
                    h.update(switch="activating", revert_pending="yes", current_system=NEW_SYS)
                    self.revert_at = self.t.timestamp() + 300
                if self.healthy_after and self.polls >= max(self.healthy_after, self.build_polls + 1):
                    h.update(switch="succeeded", active_rev=h["rev"])
                    if self.failed_after_switch is not None:
                        self.failed = self.failed_after_switch
            if h["revert_pending"] == "yes" and self.revert_at and self.t.timestamp() >= self.revert_at:
                h.update(switch="failed", revert_pending="no", active_rev="")
                if not self.revert_fails:
                    h["current_system"] = OLD_SYS
            lines = {**h, "failed_units": len(self.failed), "failed_list": " ".join(sorted(self.failed))}
            return rc, "".join(f"{k}={v}\n" for k, v in lines.items())
        if rc == 0 and verb == "switch":
            self.polls, self.revert_at = 0, None
            self.host.update(rev=words[1], switch="building", revert_pending="no", active_rev="")
        if rc == 0 and verb == "confirm":
            self.host["revert_pending"] = "no"
        return rc, f"{verb} done"

    def verbs(self, host=None):
        return [v for h, v, _ in self.remotes if host in (None, h)]


def make(tmp_path, fake=None, **cfg):
    fake = fake or Fake()
    config = {"socket": str(tmp_path / "door.sock"), "repo_url": "ssh://git@example/flake.git",
              "checkout": str(tmp_path / "flake"), "state_path": str(tmp_path / "state.json"),
              "ssh_key": "/run/secrets/door-key",
              "hosts": {"suzuka": "10.0.0.1", "spa": "10.0.0.2", "silverstone": "10.0.0.3",
                        "monza": "10.0.0.4", "miami": "10.0.0.5"},
              "night_start_hour": 0, "night_end_hour": 7, "revert_after_s": 300, **cfg}
    return Door(config, run=fake.run, remote=fake.remote, now=fake.now, sleep=fake.sleep), fake


async def ask(door, **req):
    if req.get("op") in ("restart", "reboot"):
        req.setdefault("approved_ts", YES)
    return await door.handle(json.dumps(req).encode())


def deploy_req(**over):
    return {"op": "deploy", "host": "suzuka", "action": "switch", "rev": REV, "item_id": 7,
            "urgent": False, "approved_ts": YES, **over}


# --- validation: refused before anything runs ---

@pytest.mark.parametrize("req,needle", [
    ({"op": "launch"}, "Unknown op"),
    ({"op": ["deploy"]}, "Unknown op"),
    ({"host": "suzuka"}, "Unknown op"),
    (deploy_req(host="paris"), "Unknown host"),
    (deploy_req(host="miami"), "miami"),
    (deploy_req(host=["suzuka"]), "Unknown host"),
    (deploy_req(host="suzuka; reboot"), "Unknown host"),
    (deploy_req(rev="abc123"), "40-character"),
    (deploy_req(rev="A" * 40), "40-character"),
    (deploy_req(rev=REV + "\n"), "40-character"),
    (deploy_req(rev=f"{'a' * 39};"), "40-character"),
    (deploy_req(rev=None), "40-character"),
    (deploy_req(item_id="7"), "item_id"),
    (deploy_req(item_id=True), "item_id"),
    (deploy_req(item_id=7.5), "item_id"),
    (deploy_req(item_id=-1), "item_id"),
    (deploy_req(action="boot"), "action"),
    (deploy_req(action="switch --night-ok"), "action"),
    (deploy_req(urgent="yes"), "urgent"),
    (deploy_req(approved_ts="now"), "approved_ts"),
    (deploy_req(approved_ts=True), "approved_ts"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "nginx"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "nginx.service; reboot"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "a b.service"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "$(id).service"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "x.mount"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "--now.service"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7, "unit": "x.service\n"}, "unit"),
    ({"op": "restart", "host": "spa", "item_id": 7}, "unit"),
    ({"op": "restart", "host": "miami", "item_id": 7, "unit": "x.service"}, "miami"),
    ({"op": "reboot", "host": "miami", "item_id": 7}, "miami"),
    ({"op": "status", "host": "miami"}, "miami"),
    ({"op": "status", "host": "nowhere"}, "Unknown host"),
    ({"op": "flake_update", "host": "miami", "item_id": 7}, "miami"),
    ({"op": "prepare", "item_id": 7, "patch": "x" * (door_mod.MAX_PATCH + 1)}, "larger"),
    ({"op": "prepare", "item_id": 7, "patch": ""}, "patch"),
    ({"op": "prepare", "item_id": 7, "patch": ["x"]}, "patch"),
    ({"op": "prepare", "item_id": "7; id", "patch": "x"}, "item_id"),
    ({"op": "finish", "item_id": 7, "rev": REV}, "hosts"),
    ({"op": "finish", "item_id": 7, "rev": REV, "hosts": []}, "hosts"),
    ({"op": "finish", "item_id": 7, "rev": REV, "hosts": ["miami"]}, "miami"),
    ({"op": "finish", "item_id": 7, "rev": "main", "hosts": ["suzuka"]}, "40-character"),
])
async def test_validation_refusals(tmp_path, req, needle):
    door, fake = make(tmp_path)
    reply = await door.handle(json.dumps(req).encode())
    assert reply["ok"] is False and needle in reply["error"]
    assert fake.runs == [] and fake.remotes == []


@pytest.mark.parametrize("raw", [b"", b"{not json", b"[1, 2]", b'"deploy"', b"\xff\xfe", b"null"])
async def test_malformed_json(tmp_path, raw):
    door, fake = make(tmp_path)
    reply = await door.handle(raw)
    assert reply == {"ok": False, "error": "The request was not a JSON object."}
    assert fake.runs == [] and fake.remotes == []


async def test_unit_regex_accepts_real_names(tmp_path):
    door, fake = make(tmp_path)
    for unit in ["matrix-synapse.service", "getty@tty1.service", "nix-gc.timer", "nginx.socket"]:
        assert (await ask(door, op="restart", host="spa", item_id=7, unit=unit))["ok"]
    assert fake.verbs() == ["restart matrix-synapse.service", "restart getty@tty1.service",
                            "restart nix-gc.timer", "restart nginx.socket"]


# --- the night rule ---

def ts(*a):
    return datetime(*a).timestamp()


@pytest.mark.parametrize("op,host,urgent,approved,now,start,end,refused", [
    # daytime: nothing is refused
    ("switch", "suzuka", False, None, NOON, 0, 7, False),
    ("reboot", "monza", False, None, NOON, 0, 7, False),
    # night, not urgent: every changing op is refused
    ("switch", "suzuka", False, None, NIGHT, 0, 7, True),
    ("flake_update", "suzuka", False, None, NIGHT, 0, 7, True),
    ("restart", "spa", False, None, NIGHT, 0, 7, True),
    ("reboot", "silverstone", False, None, NIGHT, 0, 7, True),
    ("switch", "suzuka", False, ts(2026, 11, 23, 1, 59), NIGHT, 0, 7, True),  # a yes is not urgency
    # night, urgent: allowed except on monza
    ("switch", "suzuka", True, None, NIGHT, 0, 7, False),
    ("restart", "spa", True, None, NIGHT, 0, 7, False),
    # a dry run executes the new activation script: gated like a switch
    ("dry-activate", "suzuka", False, None, NIGHT, 0, 7, True),
    ("dry-activate", "suzuka", True, None, NIGHT, 0, 7, False),
    ("dry-activate", "monza", True, None, NIGHT, 0, 7, True),
    ("dry-activate", "monza", True, ts(2026, 11, 23, 1, 55), NIGHT, 0, 7, False),
    # reads are never refused
    ("status", "monza", False, None, NIGHT, 0, 7, False),
    ("prepare", "", False, None, NIGHT, 0, 7, False),
    # monza at night
    ("reboot", "monza", True, None, NIGHT, 0, 7, True),  # urgent, no yes
    ("reboot", "monza", True, ts(2026, 11, 22, 21, 0), NIGHT, 0, 7, True),  # yes from the evening
    ("reboot", "monza", True, ts(2026, 11, 22, 1, 30), NIGHT, 0, 7, True),  # yes from last night
    ("reboot", "monza", True, ts(2026, 11, 23, 3, 0), NIGHT, 0, 7, True),  # yes from the future
    ("reboot", "monza", True, ts(2026, 11, 23, 1, 55), NIGHT, 0, 7, False),  # fresh yes
    ("reboot", "monza", True, ts(2026, 11, 23, 0, 0), NIGHT, 0, 7, False),  # at the window's start
    ("reboot", "monza", False, ts(2026, 11, 23, 1, 55), NIGHT, 0, 7, True),  # fresh yes, not urgent
    ("switch", "monza", True, ts(2026, 11, 23, 1, 55), NIGHT, 0, 7, False),
    ("flake_update", "monza", True, None, NIGHT, 0, 7, True),
    # window edges
    ("switch", "suzuka", False, None, datetime(2026, 11, 23, 6, 59), 0, 7, True),
    ("switch", "suzuka", False, None, datetime(2026, 11, 23, 7, 0), 0, 7, False),
    ("switch", "suzuka", False, None, datetime(2026, 11, 23, 0, 0), 0, 7, True),
    ("switch", "suzuka", False, None, datetime(2026, 11, 22, 23, 59), 0, 7, False),
    # a window that wraps midnight, 22:00 to 06:00
    ("switch", "suzuka", False, None, datetime(2026, 11, 22, 23, 0), 22, 6, True),
    ("switch", "suzuka", False, None, datetime(2026, 11, 23, 5, 0), 22, 6, True),
    ("switch", "suzuka", False, None, datetime(2026, 11, 23, 6, 0), 22, 6, False),
    ("switch", "suzuka", False, None, datetime(2026, 11, 22, 21, 59), 22, 6, False),
    ("switch", "suzuka", True, None, datetime(2026, 11, 22, 23, 0), 22, 6, False),
    # monza in a wrapping window: the yes may be from before midnight, same night
    ("reboot", "monza", True, ts(2026, 11, 22, 22, 30), datetime(2026, 11, 23, 2, 0), 22, 6, False),
    ("reboot", "monza", True, ts(2026, 11, 22, 22, 30), datetime(2026, 11, 22, 23, 0), 22, 6, False),
    ("reboot", "monza", True, ts(2026, 11, 22, 21, 30), datetime(2026, 11, 23, 2, 0), 22, 6, True),
    ("reboot", "monza", True, ts(2026, 11, 22, 2, 0), datetime(2026, 11, 22, 23, 0), 22, 6, True),
    # start == end: no window at all
    ("switch", "suzuka", False, None, NIGHT, 3, 3, False),
])
def test_night_refusal(op, host, urgent, approved, now, start, end, refused):
    why = night_refusal(op, host, urgent, approved, now, start, end)
    assert (why is not None) == refused, why


async def test_night_blocks_before_any_effect(tmp_path):
    door, fake = make(tmp_path)
    fake.t = NIGHT
    fake.tips["refs/heads/staff/item-7"] = REV
    for req in [deploy_req(), deploy_req(action="dry-activate"),
                {"op": "flake_update", "host": "suzuka", "item_id": 7},
                {"op": "flake_update", "host": "suzuka", "item_id": 7, "urgent": True, "approved_ts": YES},
                {"op": "restart", "host": "spa", "item_id": 7, "unit": "x.service", "approved_ts": YES},
                {"op": "reboot", "host": "spa", "item_id": 7, "approved_ts": YES},
                {"op": "reboot", "host": "monza", "item_id": 7, "urgent": True, "approved_ts": YES},
                deploy_req(action="dry-activate", host="monza", urgent=True)]:
        reply = await door.handle(json.dumps(req).encode())
        assert not reply["ok"] and "night" in reply["error"]
    assert fake.runs == [] and fake.remotes == []
    # Urgent work goes ahead; on monza only with a yes from this same night.
    assert (await door.handle(json.dumps(deploy_req(action="dry-activate", urgent=True)).encode()))["ok"]
    reply = await door.handle(json.dumps(deploy_req(
        action="dry-activate", host="monza", urgent=True, approved_ts=ts(2026, 11, 23, 1, 50))).encode())
    assert reply["ok"] and fake.verbs("monza") == [f"dry-activate {REV} --night-ok"]


async def test_night_ok_flag_only_when_the_monza_rule_allowed_it(tmp_path):
    door, fake = make(tmp_path)
    fresh = ts(2026, 11, 23, 1, 50)
    fake.t = NIGHT
    assert (await ask(door, op="reboot", host="monza", item_id=7, urgent=True, approved_ts=fresh))["ok"]
    assert (await ask(door, op="restart", host="monza", item_id=7, urgent=True, approved_ts=fresh,
                      unit="x.service"))["ok"]
    assert (await ask(door, op="reboot", host="spa", item_id=7, urgent=True))["ok"]
    fake.t = NOON
    assert (await ask(door, op="reboot", host="monza", item_id=7))["ok"]
    assert fake.verbs() == ["reboot --night-ok", "restart x.service --night-ok", "reboot", "reboot"]


# --- prepare ---

async def test_prepare_applies_patch_and_reports_files(tmp_path, caplog):
    door, fake = make(tmp_path)
    fake.heads = [MAIN, REV]
    fake.numstat = "3\t1\tnix/module.nix\0-\t-\tlogo.png\0" + "10\t0\tdocs/a b.md\0"
    patch = "From abc\nSECRET-PATCH-BODY\n"
    with caplog.at_level(logging.INFO, logger="staff.door"):
        reply = await ask(door, op="prepare", item_id=7, patch=patch)
    assert reply == {"ok": True, "rev": REV, "branch": "staff/item-7", "files": [
        {"path": "nix/module.nix", "added": 3, "removed": 1},
        {"path": "logo.png", "added": None, "removed": None},
        {"path": "docs/a b.md", "added": 10, "removed": 0}],
        "touches_flake": False, "diff": "diff --git a/x b/x\n", "diff_truncated": False}
    diffs = [r for r in fake.runs if "diff" in r and "--numstat" not in r]
    assert diffs[0][-2:] == [MAIN, REV]  # the whole change from main, not just the last commit
    subs = [[a for a in r[5:]] for r in fake.runs if r[0] == "git" and r[1] == "-c"]
    assert ["fetch", "--quiet", "--prune", "origin"] in subs
    assert ["checkout", "--quiet", "--force", "--detach", "origin/main"] in subs
    assert ["branch", "--force", "staff/item-7", REV] in subs
    assert not any(s[0] == "push" for s in subs)  # nothing is pushed
    assert "SECRET-PATCH-BODY" not in caplog.text
    assert "request" in caplog.text and "outcome op=prepare ok=True" in caplog.text


async def test_prepare_failure_aborts_and_leaves_branch_alone(tmp_path):
    door, fake = make(tmp_path)
    fake.heads = [MAIN, REV]
    fake.tips["refs/heads/staff/item-7"] = REV2  # an earlier, approved prepare
    fake.fail["am"] = "error: patch failed: flake.nix:12"
    reply = await ask(door, op="prepare", item_id=7, patch="garbage")
    assert not reply["ok"] and "flake.nix:12" in reply["error"]
    subs = [r[5:] for r in fake.runs if r[0] == "git"]
    assert subs[-1] == ["am", "--abort"]
    assert fake.tips["refs/heads/staff/item-7"] == REV2


# --- deploy ---

async def test_deploy_refuses_rev_that_is_not_the_branch_tip(tmp_path):
    door, fake = make(tmp_path)
    for tip in (None, REV2):
        if tip:
            fake.tips["refs/heads/staff/item-7"] = tip
        for action in ("dry-activate", "switch"):
            reply = await door.handle(json.dumps(deploy_req(action=action)).encode())
            assert not reply["ok"] and "not the tip of staff/item-7" in reply["error"]
    assert fake.remotes == []
    # The tip of another item's branch does not count either.
    fake.tips = {"refs/heads/staff/item-8": REV}
    assert not (await door.handle(json.dumps(deploy_req()).encode()))["ok"]
    assert fake.remotes == []


async def test_dry_activate_sends_archive_of_that_rev(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    reply = await door.handle(json.dumps(deploy_req(action="dry-activate", host="spa")).encode())
    assert reply == {"ok": True, "rev": REV, "log": "dry-activate done"}
    assert fake.remotes == [("spa", f"dry-activate {REV}", b"TAR:" + REV.encode())]
    assert door.state["hosts"] == {}


async def test_switch_confirms_only_after_healthy_status(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.healthy_after = 3
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert reply["ok"] and reply["rev"] == REV
    assert fake.verbs() == ["status", f"switch {REV}", "status", "status", "status", f"confirm {REV}"]
    assert fake.remotes[1][2] == b"TAR:" + REV.encode()
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["hosts"] == {"suzuka": REV} and saved["pending"] == {}
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".state-")] == []


async def test_switch_times_out_without_confirming(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.healthy_after = 0  # activated, never healthy
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "did not report healthy within 300 s" in reply["error"]
    assert "is on its previous system" in reply["error"]  # the door watched the revert happen
    assert not any(v.startswith("confirm") for v in fake.verbs())
    assert reply["status"]["current_system"] == OLD_SYS
    assert door.state["hosts"] == {} and door.state["pending"] == {}


async def test_switch_not_confirmed_when_new_units_failed(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.failed = ["old.service"]  # already failed before: tolerated
    fake.failed_after_switch = ["new.service"]  # the same count, but another unit
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "did not report healthy" in reply["error"]
    assert not any(v.startswith("confirm") for v in fake.verbs())
    assert door.state["hosts"] == {}


async def test_switch_tolerates_units_that_had_failed_before(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.host["system_running"] = "degraded"
    fake.failed = ["old.service", "older.service"]
    fake.failed_after_switch = ["old.service"]
    assert (await door.handle(json.dumps(deploy_req()).encode()))["ok"]


async def test_switch_waits_out_a_build_longer_than_the_revert_window(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.build_polls = 100  # 1000 s of building, revert_after_s is 300
    fake.healthy_after = 103
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert reply["ok"] and fake.verbs()[-1] == f"confirm {REV}"
    assert (fake.t - NOON).total_seconds() > 1000
    assert door.state["hosts"] == {"suzuka": REV} and door.state["pending"] == {}


async def test_health_countdown_starts_when_activation_begins(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.build_polls = 100
    fake.healthy_after = 0
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "did not report healthy within 300 s" in reply["error"]
    waited = (fake.t - NOON).total_seconds()
    assert 1000 + 250 <= waited <= 1000 + 330


async def test_switch_gives_up_when_the_build_never_finishes(tmp_path):
    door, fake = make(tmp_path, build_timeout_s=600)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.build_polls = 10**9
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "build did not finish within 600 s" in reply["error"]
    assert 600 <= (fake.t - NOON).total_seconds() <= 800  # the build limit, then a look at where the host is
    assert not any(v.startswith("confirm") for v in fake.verbs())
    assert door.state["pending"] == {}


@pytest.mark.parametrize("state,words", [("failed", "build or the activation failed"),
                                         ("lost", "restarted before the confirm")])
async def test_switch_reports_failed_or_lost_early(tmp_path, state, words):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.healthy_after = 0

    real = fake.remote

    async def remote(host, verb_line, stdin=None):
        rc, out = await real(host, verb_line, stdin)
        if verb_line.startswith("switch"):
            fake.host["switch"] = state
        return rc, out

    door.remote = remote
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and words in reply["error"]
    assert fake.verbs() == ["status", f"switch {REV}", "status"]


async def test_switch_reports_a_host_that_reverted_by_itself(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.healthy_after = 0
    real = fake.remote

    async def remote(host, verb_line, stdin=None):
        if verb_line == "status" and fake.polls == 3:
            fake.host.update(switch="none", revert_pending="no")  # e.g. state wiped
        return await real(host, verb_line, stdin)

    door.remote = remote
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "already went back" in reply["error"]
    assert (fake.t - NOON).total_seconds() < 100


async def test_switch_refused_when_host_wont_start_or_is_mid_switch(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.remote_rc["switch"] = 2
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "did not start the switch" in reply["error"]
    assert door.state["pending"] == {} and fake.verbs() == ["status", f"switch {REV}"]
    fake.healthy_after = 0
    for busy in ({"revert_pending": "yes"}, {"switch": "building"}, {"switch": "activating"}):
        fake.remotes.clear()
        fake.host.update(switch="none", revert_pending="no")
        fake.host.update(busy)
        reply = await door.handle(json.dumps(deploy_req()).encode())
        assert not reply["ok"] and "earlier switch" in reply["error"]
        assert fake.verbs() == ["status"]


async def test_switch_keeps_polling_while_host_is_unreachable(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    real = fake.remote
    calls = 0

    async def remote(host, verb_line, stdin=None):
        nonlocal calls
        calls += 1
        if verb_line == "status" and calls in (3, 4):
            return 255, "connection refused"
        return await real(host, verb_line, stdin)

    door.remote = remote
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert reply["ok"] and door.state["hosts"] == {"suzuka": REV}


async def test_failed_confirm_is_a_failure(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.remote_rc["confirm"] = 2
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "did not accept the confirm" in reply["error"]
    assert door.state["hosts"] == {}


async def test_resume_confirms_a_switch_that_outlived_the_door(tmp_path):
    fake = Fake()
    fake.host.update(rev=REV, active_rev=REV, switch="succeeded", revert_pending="yes", current_system=NEW_SYS)
    (tmp_path / "state.json").write_text(json.dumps({"hosts": {}, "flake_update": None, "pending": {
        "suzuka": {"rev": REV, "deadline": NOON.timestamp() + 100, "build_deadline": 0, "failed_before": [],
                   "system_before": OLD_SYS}}}))
    door, _ = make(tmp_path, fake)
    door.resume()
    await asyncio.gather(*door._tasks)
    assert fake.verbs() == ["status", f"confirm {REV}"]
    assert door.state["hosts"] == {"suzuka": REV} and door.state["pending"] == {}


# --- flake_update ---

async def test_flake_update_refuses_when_other_files_changed(tmp_path):
    door, fake = make(tmp_path)
    fake.porcelain = " M flake.lock\n?? result\n M flake.nix\n"
    reply = await ask(door, op="flake_update", host="suzuka", item_id=7)
    assert not reply["ok"] and "more than flake.lock" in reply["error"] and "flake.nix" in reply["error"]
    assert fake.remotes == [] and door.state["flake_update"] is None
    assert not any("commit" in r for r in fake.runs)


async def test_flake_update_is_a_noop_when_lock_unchanged(tmp_path):
    door, fake = make(tmp_path)
    reply = await ask(door, op="flake_update", host="suzuka", item_id=7)
    assert reply == {"ok": True, "changed": False}
    assert ["nix", "flake", "update"] in fake.runs
    assert fake.remotes == [] and door.state["flake_update"] is None
    assert not any("commit" in r or "branch" in r for r in fake.runs)


async def test_flake_update_reports_nix_failure(tmp_path):
    door, fake = make(tmp_path)
    fake.fail["nix"] = "error: unable to download"
    reply = await ask(door, op="flake_update", host="suzuka", item_id=7)
    assert not reply["ok"] and "unable to download" in reply["error"]
    assert fake.remotes == []


async def test_flake_update_rolls_in_order(tmp_path):
    door, fake = make(tmp_path)
    fake.porcelain = " M flake.lock\n"

    # Later hosts cannot start a flake update.
    reply = await ask(door, op="flake_update", host="spa", item_id=7)
    assert not reply["ok"] and "starts on suzuka" in reply["error"]

    reply = await ask(door, op="flake_update", host="suzuka", item_id=7)
    assert reply["ok"] and reply["changed"] and reply["rev"] == REV
    assert door.state["flake_update"] == REV and door.state["hosts"] == {"suzuka": REV}
    assert fake.tips["refs/heads/staff/flake-update"] == REV
    commit = next(r for r in fake.runs if "commit" in r)
    assert commit[-2:] == ["--", "flake.lock"]

    # monza and silverstone must wait for spa.
    for host in ("silverstone", "monza"):
        reply = await ask(door, op="flake_update", host=host, item_id=7)
        assert not reply["ok"] and "spa comes before" in reply["error"]
    assert fake.verbs("silverstone") == [] and fake.verbs("monza") == []

    nix_runs = len([r for r in fake.runs if r[0] == "nix"])
    for host in ("spa", "silverstone", "monza"):
        fake.host.update(switch="none", revert_pending="no", active_rev="")
        reply = await ask(door, op="flake_update", host=host, item_id=7)
        assert reply["ok"] and reply["rev"] == REV, reply
        assert fake.verbs(host)[1] == f"switch {REV}"
    assert len([r for r in fake.runs if r[0] == "nix"]) == nix_runs  # same commit, no new update
    assert door.state["hosts"] == dict.fromkeys(["suzuka", "spa", "silverstone", "monza"], REV)


async def test_flake_update_later_host_waits_for_failed_earlier_host(tmp_path):
    door, fake = make(tmp_path)
    fake.porcelain = " M flake.lock\n"
    fake.healthy_after = 0  # suzuka never gets healthy
    reply = await ask(door, op="flake_update", host="suzuka", item_id=7)
    assert not reply["ok"]
    assert door.state["flake_update"] == REV and door.state["hosts"] == {}
    reply = await ask(door, op="flake_update", host="spa", item_id=7)
    assert not reply["ok"] and "suzuka comes before spa" in reply["error"]
    assert fake.verbs("spa") == []


async def test_flake_update_never_runs_at_night_and_needs_no_yes(tmp_path):
    door, fake = make(tmp_path)
    door.state["flake_update"] = REV
    door.state["hosts"] = dict.fromkeys(["suzuka", "spa", "silverstone"], REV)
    fake.tips["refs/heads/staff/flake-update"] = REV
    fake.t = NIGHT
    for host in ("spa", "monza"):
        for extra in ({}, {"urgent": True}, {"urgent": True, "approved_ts": ts(2026, 11, 23, 1, 0)}):
            reply = await ask(door, op="flake_update", host=host, item_id=7, **extra)
            assert not reply["ok"] and "night" in reply["error"]
    assert fake.remotes == [] and fake.runs == []
    fake.t = NOON
    reply = await ask(door, op="flake_update", host="monza", item_id=7, urgent="whatever", approved_ts="x")
    assert reply["ok"] and fake.verbs()[1] == f"switch {REV}"


# --- finish ---

def pushes(fake):
    return [r for r in fake.runs if r[0] == "git" and "push" in r]


def assert_never_forced(fake):
    """main is never forced; a staff/ branch only with a lease on that one ref."""
    for argv in pushes(fake):
        branch = argv[-1].split(":refs/heads/")[-1]
        forcing = [a for a in argv if a.startswith("+") or "force" in a or a in ("-f", "--mirror")]
        if branch.startswith("staff/") and "--delete" not in argv:
            assert forcing == [f"--force-with-lease=refs/heads/{branch}:"], argv
        else:
            assert forcing == [], argv


async def test_finish_refuses_unconfirmed_host(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    door.state["hosts"] = {"suzuka": REV, "spa": REV2}
    for hosts in (["suzuka", "spa"], ["monza"]):
        reply = await ask(door, op="finish", item_id=7, rev=REV, hosts=hosts)
        assert not reply["ok"] and "Not confirmed" in reply["error"]
    assert fake.runs == []


async def test_finish_pushes_branch_then_main_without_force(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    door.state["hosts"] = {"suzuka": REV, "spa": REV}
    reply = await ask(door, op="finish", item_id=7, rev=REV, hosts=["suzuka", "spa"])
    assert reply == {"ok": True, "rev": REV, "branch": "staff/item-7"}
    assert [p[-1] for p in pushes(fake)] == [f"{REV}:refs/heads/staff/item-7", f"{REV}:refs/heads/main"]
    assert_never_forced(fake)


async def test_finish_reports_non_fast_forward_and_never_forces(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    door.state["hosts"] = {"suzuka": REV}
    fake.fail["push-main"] = "! [rejected] main -> main (non-fast-forward)"
    reply = await ask(door, op="finish", item_id=7, rev=REV, hosts=["suzuka"])
    assert not reply["ok"] and "non-fast-forward" in reply["error"] and "nothing was forced" in reply["error"]
    assert_never_forced(fake)
    assert len([p for p in pushes(fake) if p[-1].endswith(":refs/heads/main")]) == 1  # no retry


async def test_finish_refuses_when_branch_moved(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV2
    door.state["hosts"] = {"suzuka": REV}
    reply = await ask(door, op="finish", item_id=7, rev=REV, hosts=["suzuka"])
    assert not reply["ok"] and "not the tip" in reply["error"] and pushes(fake) == []


async def test_finish_flake_update(tmp_path):
    door, fake = make(tmp_path)
    reply = await ask(door, op="finish", flake_update=True, hosts=["suzuka"])
    assert not reply["ok"] and "No flake update" in reply["error"]
    door.state["flake_update"] = REV
    door.state["hosts"] = {"suzuka": REV}
    fake.tips["refs/heads/staff/flake-update"] = REV
    reply = await ask(door, op="finish", flake_update=True, hosts=["suzuka", "spa"])
    assert not reply["ok"] and "spa" in reply["error"] and pushes(fake) == []
    reply = await ask(door, op="finish", flake_update=True, hosts=["suzuka"])
    assert reply["ok"] and door.state["flake_update"] is None
    assert pushes(fake)[-1][-1] == f"{REV}:refs/heads/main"
    assert_never_forced(fake)


# --- status, busy hosts, and the shell ---

async def test_status(tmp_path):
    door, fake = make(tmp_path)
    fake.host.update(active_rev=REV, revert_pending="yes")
    door.state["hosts"]["spa"] = REV2
    reply = await ask(door, op="status", host="spa")
    assert reply["ok"] and reply["rev"] == REV and reply["revert_pending"] is True
    assert reply["confirmed_rev"] == REV2 and reply["status"]["system_running"] == "running"
    fake.host.update(active_rev="", system_rev=REV2)  # deployed by hand: the host's own record answers
    assert (await ask(door, op="status", host="spa"))["rev"] == REV2
    fake.reachable = False
    reply = await ask(door, op="status", host="spa")
    assert not reply["ok"] and "status" in reply["error"]


async def test_busy_host_is_refused_not_queued(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.gate = asyncio.Event()
    first = asyncio.create_task(door.handle(json.dumps(deploy_req()).encode()))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not first.done()

    for req in [deploy_req(action="dry-activate"), {"op": "reboot", "host": "suzuka", "item_id": 7, "approved_ts": YES},
                {"op": "restart", "host": "suzuka", "item_id": 7, "unit": "x.service", "approved_ts": YES},
                {"op": "flake_update", "host": "suzuka", "item_id": 7}]:
        reply = await asyncio.wait_for(door.handle(json.dumps(req).encode()), 1)
        assert not reply["ok"] and "busy" in reply["error"]
    # Another host is not held up, and status still answers.
    assert (await ask(door, op="reboot", host="spa", item_id=7))["ok"]
    assert (await ask(door, op="status", host="suzuka"))["ok"]

    fake.gate.set()
    assert (await first)["ok"]
    assert (await ask(door, op="reboot", host="suzuka", item_id=7))["ok"]  # free again


async def test_no_request_field_reaches_a_shell(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.heads = [MAIN, REV]
    fake.porcelain = " M flake.lock\n"
    door.state["hosts"] = {"suzuka": REV}
    evil = "$(touch /tmp/pwned); `id` && rm -rf / #"
    await ask(door, op="prepare", item_id=7, patch=f"From {evil}\n")
    fake.heads = [REV]
    await door.handle(json.dumps(deploy_req(action="dry-activate")).encode())
    await door.handle(json.dumps(deploy_req()).encode())
    await ask(door, op="restart", host="spa", item_id=7, unit="nginx.service")
    await ask(door, op="reboot", host="spa", item_id=7)
    await ask(door, op="finish", item_id=7, rev=REV, hosts=["suzuka"])
    await ask(door, op="flake_update", host="suzuka", item_id=7)
    for field in ("host", "rev", "unit", "action", "item_id"):
        await door.handle(json.dumps({**deploy_req(), "op": "restart", "unit": "x.service",
                                      field: evil}).encode())
        await door.handle(json.dumps({**deploy_req(), field: evil}).encode())
    assert fake.runs and fake.remotes
    for argv in fake.runs:  # Fake.run already asserted these are lists of str
        assert argv[0] in ("git", "nix")
        assert not any(evil in a or ";" in a or "$(" in a for a in argv), argv
    verb = (r"status|reboot|restart (nginx|x)\.service|(dry-activate|switch|confirm) " + REV)
    for _, line, _ in fake.remotes:
        assert re.fullmatch(verb, line), line
    # The door has no way to start a shell at all.
    assert "create_subprocess_shell" not in Path(door_mod.__file__).read_text()
    assert "shell=True" not in Path(door_mod.__file__).read_text()


def test_ssh_argv():
    argv = ssh_argv("/key", "10.0.0.4", f"switch {REV} --night-ok")
    assert argv[:7] == ["ssh", "-i", "/key", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]
    assert argv[-2:] == ["staff-deploy@10.0.0.4", f"switch {REV} --night-ok"]


async def test_socket_round_trip(tmp_path):
    door, fake = make(tmp_path)
    server = asyncio.create_task(door.serve())
    sock = tmp_path / "door.sock"
    for _ in range(200):
        if sock.exists():
            break
        await asyncio.sleep(0.01)
    try:
        assert oct(sock.stat().st_mode & 0o777) == "0o660"
        for raw, ok in [(b'{"op": "status", "host": "suzuka"}\n', True), (b"nonsense\n", False),
                        (b'{"op": "status", "host": "miami"}', False)]:
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(raw)
            writer.write_eof()
            reply = json.loads(await reader.read())
            writer.close()
            assert reply["ok"] is ok
    finally:
        server.cancel()


# --- the same flow against real git ---

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main"]


def git(cwd, *args, stdin=None):
    return subprocess.run([*GIT, *args], cwd=cwd, input=stdin, check=True, capture_output=True).stdout


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_prepare_deploy_finish_with_real_git(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "--quiet", "--bare", str(origin))
    git(tmp_path, "init", "--quiet", str(work))
    (work / "flake.nix").write_text("{ }\n")
    (work / "notes.md").write_text("one\ntwo\n")
    git(work, "add", ".")
    git(work, "commit", "--quiet", "-m", "base")
    git(work, "push", "--quiet", str(origin), "HEAD:refs/heads/main")
    (work / "notes.md").write_text("one\nthree\nfour\n")
    (work / "new file.txt").write_text("hi\n")
    git(work, "add", ".")
    git(work, "commit", "--quiet", "-m", "change")
    patch = git(work, "format-patch", "--stdout", "HEAD~1").decode()

    fake = Fake()
    door, _ = make(tmp_path, fake, repo_url=str(origin))
    door.run = door._run  # real git; the hosts stay fake

    assert not (await ask(door, op="prepare", item_id=7, patch="not a patch\n"))["ok"]
    reply = await ask(door, op="prepare", item_id=7, patch=patch)
    assert reply["ok"], reply
    rev = reply["rev"]
    assert sorted(reply["files"], key=lambda f: f["path"]) == [
        {"path": "new file.txt", "added": 1, "removed": 0},
        {"path": "notes.md", "added": 2, "removed": 1}]
    assert git(origin, "for-each-ref", "--format=%(refname)").split() == [b"refs/heads/main"]

    # Only the tip of the item's branch deploys, and what is sent is that commit.
    base = git(origin, "rev-parse", "main").decode().strip()
    assert not (await door.handle(json.dumps(deploy_req(rev=base, action="dry-activate")).encode()))["ok"]
    reply = await door.handle(json.dumps(deploy_req(rev=rev, action="dry-activate")).encode())
    assert reply["ok"], reply
    with tarfile.open(fileobj=io.BytesIO(fake.remotes[-1][2])) as tar:
        assert sorted(tar.getnames()) == ["flake.nix", "new file.txt", "notes.md"]
        assert tar.extractfile("notes.md").read() == b"one\nthree\nfour\n"

    assert not (await ask(door, op="finish", item_id=7, rev=rev, hosts=["suzuka"]))["ok"]
    assert (await door.handle(json.dumps(deploy_req(rev=rev)).encode()))["ok"]

    # main moves under the door: finish must fail, not force.
    git(work, "reset", "--quiet", "--hard", "HEAD~1")
    (work / "other.txt").write_text("tom was here\n")
    git(work, "add", ".")
    git(work, "commit", "--quiet", "-m", "tom")
    git(work, "push", "--quiet", str(origin), "HEAD:refs/heads/main")
    moved = git(origin, "rev-parse", "main")
    reply = await ask(door, op="finish", item_id=7, rev=rev, hosts=["suzuka"])
    assert not reply["ok"] and "nothing was forced" in reply["error"]
    assert git(origin, "rev-parse", "main") == moved
    assert git(origin, "for-each-ref", "--format=%(refname)").split() == [b"refs/heads/main"]

    # Prepared again on the new main, deployed, and finished: a fast-forward.
    reply = await ask(door, op="prepare", item_id=7, patch=patch)
    rev2 = reply["rev"]
    assert reply["ok"] and rev2 != rev
    fake.host.update(switch="none", revert_pending="no")
    assert (await door.handle(json.dumps(deploy_req(rev=rev2)).encode()))["ok"]
    # A stale branch of that name on the remote does not block the finish.
    git(work, "push", "--quiet", str(origin), "HEAD:refs/heads/staff/item-7")
    reply = await ask(door, op="finish", item_id=7, rev=rev2, hosts=["suzuka"])
    assert reply["ok"], reply
    assert git(origin, "rev-parse", "main").decode().strip() == rev2
    assert git(origin, "rev-parse", "staff/item-7").decode().strip() == rev2


# --- the host scripts, with sudo and the system tools stubbed ---

needs_bash = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("tar")),
                                reason="bash or tar is not installed")


def stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!{shutil.which('bash')}\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def host_script(tmp_path):
    """The forced command with its state directory moved under tmp_path."""
    (tmp_path / "state" / "src").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub(bindir, "sudo", f'printf "%s\\n" "$@" >"{tmp_path}/sudo-args"')
    script = tmp_path / "host.sh"
    script.write_text((NIX / "staff-deploy-host.sh").read_text()
                      .replace("/var/lib/staff-deploy", str(tmp_path / "state")))

    def call(command, stdin=b""):
        (tmp_path / "sudo-args").unlink(missing_ok=True)
        env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
        if command is not None:
            env["SSH_ORIGINAL_COMMAND"] = command
        p = subprocess.run(["bash", str(script)], input=stdin, env=env, capture_output=True)
        args = tmp_path / "sudo-args"
        return p.returncode, (args.read_text().splitlines() if args.exists() else None)

    return call


def make_tar(members: dict[str, bytes | None], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.FIFOTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.LNKTYPE, target
            tar.addfile(info)
    return buf.getvalue()


@needs_bash
@pytest.mark.parametrize("command,args", [
    ("status", ["-n", "staff-deploy-root", "status"]),
    (f"confirm {REV}", ["-n", "staff-deploy-root", "confirm", REV]),
    ("restart matrix-synapse.service", ["-n", "staff-deploy-root", "restart", "matrix-synapse.service"]),
    ("restart getty@tty1.service --night-ok",
     ["-n", "staff-deploy-root", "restart", "getty@tty1.service", "--night-ok"]),
    ("reboot", ["-n", "staff-deploy-root", "reboot"]),
    ("reboot --night-ok", ["-n", "staff-deploy-root", "reboot", "--night-ok"]),
])
def test_host_script_accepts_the_verbs(host_script, command, args):
    assert host_script(command) == (0, args)


@needs_bash
@pytest.mark.parametrize("command", [
    None, "", "status ", " status", "status extra", "status; id", "status\nreboot", "status && reboot",
    "STATUS", "shell", "bash -c id", "$(reboot)", "`reboot`", "reboot now", "reboot --night-ok extra",
    "reboot  --night-ok", "status --night-ok", f"confirm {REV} --night-ok", "confirm", "confirm abc",
    f"confirm {'A' * 40}", f"confirm {REV}0", f"confirm {REV} {REV}", f"confirm  {REV}",
    f"confirm {REV}\n", "switch", f"switch {REV[:39]}", f"switch {REV} --force", f"dry-activate {REV} --force",
    f"dry-activate {REV} --night-ok --night-ok",
    "restart", "restart nginx", "restart nginx.mount", "restart -nginx.service", "restart --now.service",
    "restart ../x.service", "restart a.service b.service", "restart a.service;reboot",
    "restart 'a.service'", "restart a\\.service", "restart a.service\treboot", "revert-now",
    f"run-switch {REV}", "restart " + "a" * 300 + ".service", "restart é.service",
])
def test_host_script_refuses_everything_else(host_script, command):
    rc, args = host_script(command, stdin=make_tar({"flake.nix": b"{ }"}))
    assert rc != 0 and args is None


@needs_bash
def test_host_script_unpacks_source_for_switch(host_script, tmp_path):
    tar = make_tar({"flake.nix": b"{ }\n", "nix/module.nix": b"x\n"})
    src = tmp_path / "state" / "src" / REV
    src.mkdir()
    (src / "stale").write_text("left over from an earlier upload")
    assert host_script(f"switch {REV} --night-ok", tar) == (
        0, ["-n", "staff-deploy-root", "switch", REV, "--night-ok"])
    assert sorted(p.name for p in src.iterdir()) == ["flake.nix", "nix"]  # fresh directory
    assert (src / "nix" / "module.nix").read_text() == "x\n"
    assert host_script(f"dry-activate {REV2}", tar) == (0, ["-n", "staff-deploy-root", "dry-activate", REV2])
    assert (tmp_path / "state" / "src" / REV2 / "flake.nix").exists()
    assert not list((tmp_path / "state" / "src").glob(".incoming*"))


@needs_bash
@pytest.mark.parametrize("tar", [
    make_tar({"flake.nix": b"{ }", "../evil": b"x"}),
    make_tar({"flake.nix": b"{ }", "a/../../evil": b"x"}),
    make_tar({"flake.nix": b"{ }", "/evil": b"x"}),
    make_tar({"flake.nix": b"{ }", "pipe": None}),
    make_tar({"flake.nix": b"{ }"}, links={"hard": "flake.nix"}),
    make_tar({"README": b"no flake here"}),
    b"", b"this is not a tar archive",
])
def test_host_script_refuses_unsafe_archives(host_script, tmp_path, tar):
    rc, args = host_script(f"switch {REV}", tar)
    assert rc != 0 and args is None
    assert not (tmp_path / "evil").exists() and not (tmp_path / "state" / "evil").exists()
    assert not Path("/evil").exists()


@pytest.fixture
def root_script(tmp_path):
    """The root script with every path under tmp_path and the system tools stubbed."""
    for d in ("state/src", "logs", "bin", f"store/{OLD}/bin", f"store/{NEW}/bin"):
        (tmp_path / d).mkdir(parents=True)
    bindir, calls = tmp_path / "bin", tmp_path / "calls"
    for name, store in (("old-system", OLD), ("new-system", NEW)):
        # Like the real one: `test` changes the running system, `boot` does not.
        switch_to = tmp_path / "store" / store / "bin/switch-to-configuration"
        switch_to.write_text(f"""#!{shutil.which('bash')}
echo "switch-to-configuration {name} $*" >>{calls}
[[ ! -e "$STUBS/{name}-$1-fails" ]] || exit 1
[[ $1 != test ]] || ln -sfn {tmp_path}/store/{store} {tmp_path}/current
""")
        switch_to.chmod(0o755)
    (tmp_path / "current").symlink_to(tmp_path / "store" / OLD)
    (tmp_path / "profile").symlink_to(tmp_path / "store" / OLD)
    record = f'echo "$(basename "$0") $*" >>"{calls}"'
    stub(bindir, "systemctl", record + """
case "$1" in
  is-active) [[ -e "$STUBS/active-${@: -1}" ]] ;;
  is-system-running) echo running ;;
  list-units) cat "$STUBS/failed" 2>/dev/null || true ;;
  show)
    if [[ "$*" == *ActiveState* ]]; then cat "$STUBS/revert-state" 2>/dev/null || echo inactive
    else cat "$STUBS/names" 2>/dev/null || echo "${@: -1}"; fi ;;
esac""")
    stub(bindir, "systemd-run", record)
    stub(bindir, "timeout", record + '\nshift\nexec "$@"')
    stub(bindir, "nix-env", record + f'\n[[ ! -e "$STUBS/nix-env-fails" ]] || exit 1\nln -sfn "$4" {tmp_path}/profile')
    stub(bindir, "nixos-rebuild", record + f"""
echo "building"
[[ ! -e "$STUBS/build-fails" ]] || exit 1
[[ $1 != build ]] || ln -sfn "${{FAKE_RESULT:-{tmp_path}/store/{NEW}}}" result""")
    stub(bindir, "hostname", 'echo "${FAKE_HOST:-suzuka}"')
    stub(bindir, "date", 'if [[ "$1" == +%-H ]]; then echo "${FAKE_HOUR:-12}"; else echo 20261123T120000; fi')
    text = (NIX / "staff-deploy-root.sh").read_text()
    for old, new in [("/var/lib/staff-deploy", "state"), ("/var/log/fleet-deploy", "logs"),
                     ("/etc/staff-deploy.conf", "conf"), ("/run/current-system", "current"),
                     ("/run/staff-deploy", "run"), ("/nix/var/nix/profiles/system", "profile"),
                     ("/nix/store/", "store/")]:
        assert old in text
        text = text.replace(old, f"{tmp_path}/{new}")
    script = tmp_path / "root.sh"
    script.write_text(text)

    def call(*args, **env):
        calls.write_text("")
        p = subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                           env={"PATH": f"{bindir}:{os.environ['PATH']}", "STUBS": str(tmp_path), **env})
        return p.returncode, p.stdout, calls.read_text().splitlines()

    return call


def root_status(root_script):
    return dict(line.split("=", 1) for line in root_script("status")[1].splitlines())


def put_source(tmp_path, rev):
    src = tmp_path / "state" / "src" / rev
    src.mkdir(parents=True, exist_ok=True)
    (src / "flake.nix").write_text("{ }\n")
    return src


@needs_bash
@pytest.mark.parametrize("args", [
    [], ["shell"], ["status", "extra"], ["status", "--night-ok"], ["switch"], ["switch", "abc"],
    ["switch", REV, "extra"], ["switch", "A" * 40], ["switch", REV + "\n"], ["confirm", f"{REV};id"],
    ["confirm", REV, "--night-ok"], ["dry-activate", REV, "--force"], ["restart"], ["restart", "nginx"],
    ["restart", "-x.service"], ["restart", "a b.service"], ["restart", "x.service", "y.service"],
    ["restart", "$(id).service"], ["reboot", "now"], ["--night-ok"], ["revert-now", "x"],
])
def test_root_script_revalidates_arguments(root_script, args):
    rc, out, calls = root_script(*args)
    assert rc != 0 and calls == [] and out == ""


@needs_bash
def test_root_script_status_format(root_script, tmp_path):
    rc, out, _ = root_script("status")
    assert rc == 0
    assert out.splitlines() == [
        f"current_system={tmp_path}/store/{OLD}", "rev=", "active_rev=", "system_rev=", "switch=none",
        "revert_pending=no", "system_running=running", "failed_units=0", "log=", "failed_list="]
    # A host deployed by hand still says what it was built from; anything that is not a revision is dropped.
    version = tmp_path / "store" / OLD / "sw/bin/nixos-version"
    version.parent.mkdir(parents=True)
    for says, shown in ((REV, REV), ("dirty", "")):
        version.write_text(f"#!/bin/sh\necho {says}\n")
        version.chmod(0o755)
        assert root_script("status")[1].splitlines()[3] == f"system_rev={shown}"
    (tmp_path / "failed").write_text("b.service loaded failed failed B\na.service loaded failed failed A\n")
    lines = root_script("status")[1].splitlines()
    assert len(lines) == 10 and lines[7] == "failed_units=2" and lines[9] == "failed_list=a.service b.service"
    # The list is capped at 50 names; the count is not, so the door can tell.
    (tmp_path / "failed").write_text("".join(f"u{i:02}.service loaded failed failed\n" for i in range(60)))
    lines = root_script("status")[1].splitlines()
    assert lines[7] == "failed_units=60" and lines[9].split("=")[1].split() == [f"u{i:02}.service" for i in range(50)]


@needs_bash
def test_root_script_switch_builds_then_arms_then_activates_and_confirm_sets_boot(root_script, tmp_path):
    state, old, new = tmp_path / "state", f"{tmp_path}/store/{OLD}", f"{tmp_path}/store/{NEW}"
    assert root_script("switch", REV)[0] != 0  # no source uploaded
    put_source(tmp_path, REV)
    (tmp_path / "conf").write_text("# comment\nREVERT_AFTER_S=120\nBUILD_TIMEOUT_S=900\nNIGHT_START_HOUR=$(reboot)\n")

    rc, out, calls = root_script("switch", REV)
    assert rc == 0 and out.startswith(f"switch started rev={REV} revert_after_s=120 log={tmp_path}/logs/staff-")
    runs = [c for c in calls if c.startswith("systemd-run")]
    assert len(runs) == 1  # only the build unit: no revert is armed while nothing has changed
    assert "--unit staff-deploy-switch" in runs[0] and runs[0].endswith(f"root.sh run-switch {REV}")
    assert (state / "previous").read_text().strip() == old
    assert not (tmp_path / "run/pending").exists()

    # While the unit builds: status says so, a second switch, a confirm and a reboot are refused.
    (tmp_path / "active-staff-deploy-switch.service").touch()
    status = root_status(root_script)
    assert status["switch"] == "building" and status["revert_pending"] == "no" and status["rev"] == REV
    assert status["active_rev"] == "" and status["log"].endswith(f"-{REV}.log")
    assert root_script("switch", REV)[0] != 0
    assert root_script("confirm", REV)[0] != 0
    assert root_script("reboot")[0] != 0

    # The transient unit: build, then arm the revert, then activate without touching the boot default.
    rc, _, calls = root_script("run-switch", REV)
    assert rc == 0
    assert calls[0] == "timeout 900 nixos-rebuild build --flake " + f"{state}/src/{REV}#suzuka"
    assert calls[1] == f"nixos-rebuild build --flake {state}/src/{REV}#suzuka"
    assert calls[2].startswith("systemd-run ") and calls[2].endswith("root.sh revert-now")
    assert "--unit staff-deploy-revert --on-active=120" in calls[2]
    assert calls[3:] == ["switch-to-configuration new-system test"]
    assert os.readlink(tmp_path / "current") == new and os.readlink(tmp_path / "profile") == old
    assert (tmp_path / "run/pending").read_text().strip() == REV
    (tmp_path / "active-staff-deploy-switch.service").unlink()
    status = root_status(root_script)
    assert status["switch"] == "succeeded" and status["active_rev"] == REV and status["revert_pending"] == "yes"
    assert root_script("reboot")[0] != 0

    assert root_script("confirm", REV2)[0] != 0  # some other rev
    assert (tmp_path / "run/pending").exists()
    rc, out, calls = root_script("confirm", REV)
    assert rc == 0 and out.strip() == f"confirmed rev={REV}"
    assert [c for c in calls if not c.startswith("systemctl is-active")] == [
        "systemctl stop staff-deploy-revert.timer",  # first the timer, then a look at the revert
        "systemctl show --property=ActiveState --value staff-deploy-revert.service",
        f"nix-env --profile {tmp_path}/profile --set {new}",
        "switch-to-configuration new-system boot"]
    assert os.readlink(tmp_path / "profile") == new
    status = root_status(root_script)
    assert status["switch"] == "succeeded" and status["active_rev"] == REV and status["revert_pending"] == "no"
    assert root_script("confirm", REV)[0] != 0  # nothing left to confirm
    assert root_script("revert-now")[0] != 0  # and nothing to revert


@needs_bash
def test_root_script_build_timeout_defaults_to_an_hour(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[2][0].startswith("timeout 3600 nixos-rebuild build ")


@needs_bash
def test_root_script_failed_build_changes_nothing(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    (tmp_path / "build-fails").touch()
    rc, _, calls = root_script("run-switch", REV)
    assert rc != 0
    assert not any(c.startswith(("systemd-run", "switch-to-configuration", "nix-env")) for c in calls)
    status = root_status(root_script)
    assert status["switch"] == "failed" and status["revert_pending"] == "no" and status["active_rev"] == ""
    assert status["current_system"] == f"{tmp_path}/store/{OLD}"
    assert root_script("confirm", REV)[0] != 0
    (tmp_path / "build-fails").unlink()
    assert root_script("switch", REV)[0] == 0  # a later try is not blocked


@needs_bash
def test_root_script_failed_activation_reverts_at_once(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    (tmp_path / "new-system-test-fails").touch()
    rc, _, calls = root_script("run-switch", REV)
    assert rc != 0
    assert calls[-3:] == ["switch-to-configuration new-system test", "systemctl stop staff-deploy-revert.timer",
                          "switch-to-configuration old-system test"]
    status = root_status(root_script)
    assert status["switch"] == "failed" and status["revert_pending"] == "no"
    assert status["current_system"] == f"{tmp_path}/store/{OLD}"


@needs_bash
def test_root_script_timer_revert_only_reactivates_the_previous_system(root_script, tmp_path):
    old = f"{tmp_path}/store/{OLD}"
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    rc, _, calls = root_script("revert-now")
    assert rc == 0
    assert calls == ["systemctl stop staff-deploy-switch.service", "switch-to-configuration old-system test"]
    assert os.readlink(tmp_path / "current") == old and os.readlink(tmp_path / "profile") == old
    status = root_status(root_script)
    assert status["switch"] == "failed" and status["revert_pending"] == "no" and status["active_rev"] == ""
    assert root_script("confirm", REV)[0] != 0
    assert root_script("revert-now")[0] != 0


@needs_bash
def test_root_script_unconfirmed_reboot_is_reported_as_lost(root_script, tmp_path):
    old = tmp_path / "store" / OLD
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    assert os.readlink(tmp_path / "profile") == str(old)  # the boot default never moved
    # The reboot: /run is empty, the timer is gone, the old system is running.
    shutil.rmtree(tmp_path / "run")
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(old)
    status = root_status(root_script)
    assert status["switch"] == "lost" and status["revert_pending"] == "no" and status["active_rev"] == ""
    assert status["current_system"] == str(old)
    assert root_script("confirm", REV)[0] != 0
    assert root_script("switch", REV)[0] == 0  # nothing to undo, a new switch may start


@needs_bash
def test_root_script_reboot_during_build_or_activation_is_reported_as_failed(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_status(root_script)["switch"] == "failed"  # result says building, the unit is gone
    (tmp_path / "state/result").write_text("activating\n")
    status = root_status(root_script)
    assert status["switch"] == "failed" and status["revert_pending"] == "no"


@needs_bash
@pytest.mark.parametrize("broken", ["nix-env-fails", "new-system-boot-fails"])
def test_root_script_confirm_that_cannot_set_the_boot_default_reverts(root_script, tmp_path, broken):
    old = f"{tmp_path}/store/{OLD}"
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    (tmp_path / broken).touch()
    rc, out, calls = root_script("confirm", REV)
    assert rc != 0 and out == ""
    assert calls[-1] == "switch-to-configuration old-system test"
    (tmp_path / broken).unlink()
    if broken == "new-system-boot-fails":  # the profile had moved: it is put back first
        assert calls[-3:-1] == [f"nix-env --profile {tmp_path}/profile --set {old}",
                                "switch-to-configuration old-system boot"]
    assert os.readlink(tmp_path / "current") == old and os.readlink(tmp_path / "profile") == old
    status = root_status(root_script)
    assert status["switch"] == "failed" and status["revert_pending"] == "no"


@needs_bash
def test_root_script_confirm_refused_once_the_revert_has_started(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    (tmp_path / "active-staff-deploy-revert.service").touch()
    for state in ("activating", "active", "deactivating", "failed"):
        (tmp_path / "revert-state").write_text(state + "\n")
        rc, out, calls = root_script("confirm", REV)
        assert rc != 0 and out == "", state
        assert not any(c.startswith(("nix-env", "switch-to-configuration")) for c in calls)
        assert calls.index("systemctl stop staff-deploy-revert.timer") < calls.index(
            "systemctl show --property=ActiveState --value staff-deploy-revert.service")
    assert os.readlink(tmp_path / "profile") == f"{tmp_path}/store/{OLD}"
    assert root_script("switch", REV)[0] != 0


@needs_bash
def test_root_script_confirm_refused_when_the_revert_ran_during_the_stop(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    # The revert service ran to its end between the first checks and the look after the stop.
    stub(tmp_path / "bin", "systemctl", f"""echo "systemctl $*" >>"{tmp_path}/calls"
case "$1" in
  is-active) exit 1 ;;
  stop) rm -f "{tmp_path}/run/pending" ;;
  show) echo inactive ;;
esac""")
    rc, _, calls = root_script("confirm", REV)
    assert rc != 0 and not any(c.startswith(("nix-env", "switch-to-configuration")) for c in calls)
    assert os.readlink(tmp_path / "profile") == f"{tmp_path}/store/{OLD}"


@needs_bash
@pytest.mark.parametrize("how", ["timer", "failed activation", "failed confirm"])
def test_root_script_failed_revert_is_recorded_and_said(root_script, tmp_path, how):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    if how == "failed activation":
        # `test` of the new system changes the running system and then fails.
        new_switch = tmp_path / "store" / NEW / "bin/switch-to-configuration"
        new_switch.write_text(new_switch.read_text() + '[[ $1 != test ]] || exit 1\n')
    (tmp_path / "old-system-test-fails").touch()
    if how == "failed activation":
        assert root_script("run-switch", REV)[0] != 0
    else:
        assert root_script("run-switch", REV)[0] == 0
        if how == "timer":
            assert root_script("revert-now")[0] != 0
        else:
            (tmp_path / "nix-env-fails").touch()
            assert root_script("confirm", REV)[0] != 0
    status = root_status(root_script)
    assert status["switch"] == "failed" and status["revert_pending"] == "no" and status["active_rev"] == ""
    assert status["current_system"] == f"{tmp_path}/store/{NEW}"  # which is how the door can tell
    assert "REVERT FAILED: suzuka may still be running the new system" in Path(status["log"]).read_text()
    assert os.readlink(tmp_path / "profile") == f"{tmp_path}/store/{OLD}"


@needs_bash
def test_root_script_accepts_only_strict_store_paths(root_script, tmp_path):
    put_source(tmp_path, REV)
    store = tmp_path / "store"
    good = store / OLD
    bad = [store / "old-system", store / ("0" * 31 + "-x"), store / ("A" * 32 + "-x"),
           store / ("2" * 32 + "-a b"), store / ("3" * 32 + "-a\nb"), store / ("4" * 32 + "-..")]
    for path in bad:
        (path / "bin").mkdir(parents=True)
        shutil.copy(good / "bin/switch-to-configuration", path / "bin/switch-to-configuration")
    link = store / ("5" * 32 + "-link")  # a symlink in the store is not written as it resolves
    link.symlink_to(good)
    # 1. The running system must be one before a switch starts.
    for path in bad:
        (tmp_path / "current").unlink()
        (tmp_path / "current").symlink_to(path)
        rc, _, calls = root_script("switch", REV)
        assert rc != 0 and not any(c.startswith("systemd-run") for c in calls), path
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(good)
    # 2. The build result must be one before anything is armed or activated.
    for path in bad:
        assert root_script("switch", REV)[0] == 0
        rc, _, calls = root_script("run-switch", REV, FAKE_RESULT=str(path))
        assert rc != 0 and not any(c.startswith(("systemd-run", "switch-to-configuration")) for c in calls), path
    # 3. A tampered `previous` is never executed by a revert.
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    for text in [f"{good}/../{OLD}", f"{good}\n{good}", f"{good}/", str(link), f"{tmp_path}/evil", *map(str, bad)]:
        (tmp_path / "state/previous").write_text(text + "\n")
        rc, _, calls = root_script("revert-now")
        assert rc != 0 and not any(c.startswith("switch-to-configuration") for c in calls), text


@needs_bash
def test_root_script_confirm_refused_when_system_is_not_from_that_rev(root_script, tmp_path):
    put_source(tmp_path, REV)
    assert root_script("switch", REV)[0] == 0
    assert root_script("run-switch", REV)[0] == 0
    # Something else switched the system since.
    (tmp_path / "store/other-system").mkdir()
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(tmp_path / "store/other-system")
    rc, _, calls = root_script("confirm", REV)
    assert rc != 0 and "systemctl stop staff-deploy-revert.timer" not in calls


@needs_bash
@pytest.mark.parametrize("host,hour,conf,flag,allowed", [
    ("monza", 2, "", False, False),
    ("monza", 2, "", True, True),
    ("monza", 12, "", False, True),
    ("monza", 7, "", False, True),
    ("monza", 6, "", False, False),
    ("suzuka", 2, "", False, True),
    ("monza", 23, "NIGHT_START_HOUR=22\nNIGHT_END_HOUR=6\n", False, False),
    ("monza", 5, "NIGHT_START_HOUR=22\nNIGHT_END_HOUR=6\n", False, False),
    ("monza", 6, "NIGHT_START_HOUR=22\nNIGHT_END_HOUR=6\n", False, True),
    ("monza", 2, "NIGHT_START_HOUR=22\nNIGHT_END_HOUR=6", True, True),
    ("monza", 8, "NIGHT_START_HOUR=08\nNIGHT_END_HOUR=09\n", False, False),
])
def test_root_script_night_gate_on_monza(root_script, tmp_path, host, hour, conf, flag, allowed):
    (tmp_path / "conf").write_text(conf)
    put_source(tmp_path, REV)
    env = {"FAKE_HOST": host, "FAKE_HOUR": str(hour)}
    extra = ["--night-ok"] if flag else []
    for args in (["restart", "nginx.service"], ["reboot"], ["switch", REV], ["dry-activate", REV]):
        rc, _, calls = root_script(*args, *extra, **env)
        assert (rc == 0) == allowed, (args, calls)
        if not allowed:
            assert calls == []
    assert root_script("status", **env)[0] == 0  # reads are never gated


@needs_bash
def test_root_script_restart_and_reboot(root_script):
    rc, out, calls = root_script("restart", "matrix-synapse.service")
    assert rc == 0 and calls[:2] == ["systemctl show --property=Id --property=Names --value -- matrix-synapse.service",
                                     "systemctl restart -- matrix-synapse.service"]
    rc, out, calls = root_script("reboot")
    assert rc == 0 and len(calls) == 1 and calls[0].endswith("--on-active=5 -- systemctl reboot")


DENIED_UNITS = ["staff-deploy-revert.timer", "staff-deploy-switch.service", "systemd-logind.service",
                "systemd-journald.socket", "sshd.service", "sshd.socket", "sshd@x.service", "ssh.service",
                "ssh@x.service", "home-staff-door.service", "home-staff-front-desk.service", "dbus.service",
                "dbus-broker.service", "dbus.socket", "reboot.service", "systemd-reboot.service",
                "poweroff.timer", "my-halt.service", "kexec.service", "x-kexec-load.service", "shutdown.service"]


@needs_bash
@pytest.mark.parametrize("unit", DENIED_UNITS)
def test_root_script_never_restarts_a_denied_unit(root_script, unit):
    rc, out, calls = root_script("restart", unit)
    assert rc != 0 and out == "" and calls == []


@needs_bash
@pytest.mark.parametrize("names", ["innocent.service\nsshd.service", "systemd-reboot.service innocent.service",
                                   "innocent.service dbus.service"])
def test_root_script_never_restarts_an_alias_of_a_denied_unit(root_script, tmp_path, names):
    (tmp_path / "names").write_text(names + "\n")
    rc, out, calls = root_script("restart", "innocent.service")
    assert rc != 0 and out == "" and not any(c.startswith("systemctl restart") for c in calls)


@needs_bash
def test_root_script_dry_activate_takes_the_night_flag(root_script, tmp_path):
    put_source(tmp_path, REV)
    rc, _, calls = root_script("dry-activate", REV, "--night-ok", FAKE_HOST="monza", FAKE_HOUR="2")
    assert rc == 0 and calls == [f"nixos-rebuild dry-activate --flake {tmp_path}/state/src/{REV}#monza"]


@needs_bash
def test_host_script_passes_the_night_flag_of_a_dry_run(host_script, tmp_path):
    tar = make_tar({"flake.nix": b"{ }"})
    assert host_script(f"dry-activate {REV}", stdin=tar) == (0, ["-n", "staff-deploy-root", "dry-activate", REV])
    assert host_script(f"dry-activate {REV} --night-ok", stdin=tar) == (
        0, ["-n", "staff-deploy-root", "dry-activate", REV, "--night-ok"])


# --- review fixes: approval, prepare, restart, health, revert ---

APPROVED_OPS = [deploy_req(), deploy_req(action="dry-activate"),
                {"op": "restart", "host": "spa", "item_id": 7, "unit": "x.service"},
                {"op": "reboot", "host": "spa", "item_id": 7}]


@pytest.mark.parametrize("req", APPROVED_OPS, ids=["switch", "dry-activate", "restart", "reboot"])
async def test_no_change_without_a_recent_yes(tmp_path, req):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    now = NOON.timestamp()
    bad = [None, "1700000000", True, [now], float("nan"), float("inf"),
           now + 61, now - 86401, 0, -1]
    for approved in bad:
        raw = json.dumps({k: v for k, v in {**req, "approved_ts": approved}.items() if v is not None})
        reply = await door.handle(raw.encode())
        assert not reply["ok"] and "approved_ts" in reply["error"], approved
    assert fake.remotes == [] and fake.runs == []
    for approved in (now + 59, now - 86399, int(now)):  # a little clock skew is fine
        fake.t = NOON
        fake.host.update(switch="none", revert_pending="no")
        assert (await door.handle(json.dumps({**req, "approved_ts": approved}).encode()))["ok"], approved


async def test_approval_max_age_comes_from_the_config(tmp_path):
    door, fake = make(tmp_path, approval_max_age_s=600)
    reply = await ask(door, op="reboot", host="spa", item_id=7, approved_ts=NOON.timestamp() - 601)
    assert not reply["ok"] and "too old" in reply["error"] and fake.remotes == []
    assert (await ask(door, op="reboot", host="spa", item_id=7, approved_ts=NOON.timestamp() - 599))["ok"]


@pytest.mark.parametrize("path,needle", [("flake.lock", "flake.lock"), ("sub/flake.lock", "flake.lock"),
                                         (".gitattributes", ".gitattributes"),
                                         ("nix/.gitattributes", ".gitattributes")])
async def test_prepare_refuses_lock_and_attribute_changes(tmp_path, path, needle):
    door, fake = make(tmp_path)
    fake.heads = [MAIN, REV]
    fake.tips["refs/heads/staff/item-7"] = REV2  # an earlier, approved prepare
    fake.numstat = f"1\t0\tflake.nix\x001\t1\t{path}\x00"
    reply = await ask(door, op="prepare", item_id=7, patch="From abc\n")
    assert not reply["ok"] and needle in reply["error"]
    assert fake.tips["refs/heads/staff/item-7"] == REV2
    assert not any("branch" in r for r in fake.runs)


async def test_prepare_says_when_the_flake_is_touched_and_cuts_a_long_diff(tmp_path):
    door, fake = make(tmp_path)
    fake.heads = [MAIN, REV]
    fake.numstat = "1\t0\tnix/flake.nix.md\x002\t2\tflake.nix\x00"
    fake.diff = "é" * 150_000  # 300 kB
    reply = await ask(door, op="prepare", item_id=7, patch="From abc\n")
    assert reply["ok"] and reply["touches_flake"] is True and reply["diff_truncated"] is True
    assert len(reply["diff"].encode()) <= 200_000 and reply["diff"].startswith("ééé")
    fake.heads = [MAIN, REV]
    fake.numstat, fake.diff = "1\t0\tnix/flake.nix.md\x00", "short\n"
    reply = await ask(door, op="prepare", item_id=7, patch="From abc\n")
    assert reply["touches_flake"] is False and reply["diff"] == "short\n" and reply["diff_truncated"] is False


@pytest.mark.parametrize("action", ["switch", "dry-activate"])
async def test_no_tree_with_gitattributes_is_sent_to_a_host(tmp_path, action):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    for tree in (".gitattributes\0flake.nix\0", "flake.nix\0nix/.gitattributes\0"):
        fake.tree = tree
        reply = await door.handle(json.dumps(deploy_req(action=action)).encode())
        assert not reply["ok"] and ".gitattributes" in reply["error"]
    assert not any(v.startswith(("switch", "dry-activate")) for v in fake.verbs())
    assert not any("archive" in r for r in fake.runs)
    fake.tree = "flake.nix\0docs/gitattributes\0docs/x.gitattributes\0"
    assert (await door.handle(json.dumps(deploy_req(action=action)).encode()))["ok"]


@pytest.mark.parametrize("unit", DENIED_UNITS)
async def test_door_refuses_to_restart_a_denied_unit(tmp_path, unit):
    door, fake = make(tmp_path)
    reply = await ask(door, op="restart", host="spa", item_id=7, unit=unit)
    assert not reply["ok"] and "not for the staff to restart" in reply["error"] and fake.remotes == []


async def test_failed_revert_is_never_called_a_revert(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.healthy_after = 0
    fake.revert_fails = True  # the host's revert ran and its own activation failed
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and "needs Tom" in reply["error"] and "may still be" in reply["error"]
    assert "by itself" not in reply["error"] and "is on its previous system" not in reply["error"]
    assert reply["status"]["switch"] == "failed" and reply["status"]["current_system"] == NEW_SYS
    assert door.state["hosts"] == {}


async def test_switch_refused_when_the_baseline_cannot_be_read(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.host["current_system"] = ""
    reply = await door.handle(json.dumps(deploy_req()).encode())
    assert not reply["ok"] and fake.verbs() == ["status"]


async def test_finish_leases_the_stale_branch_it_just_fetched(tmp_path):
    door, fake = make(tmp_path)
    fake.tips["refs/heads/staff/item-7"] = REV
    fake.tips["refs/remotes/origin/staff/item-7"] = REV2  # left over from an earlier try
    door.state["hosts"] = {"suzuka": REV}
    assert (await ask(door, op="finish", item_id=7, rev=REV, hosts=["suzuka"]))["ok"]
    branch, main = pushes(fake)
    assert f"--force-with-lease=refs/heads/staff/item-7:{REV2}" in branch
    assert not any("force" in a or a.startswith("+") for a in main) and main[-1] == f"{REV}:refs/heads/main"
