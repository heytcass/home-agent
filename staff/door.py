"""The door: the one service that can change a host. No model inside.

It holds the deploy SSH key and git write access. The front desk is the
only caller (ARCHITECTURE.md, "Wire 3"). Whoever controls this socket is
root on the fleet, so every request field is validated before use, nothing
from a request ever reaches a shell (argv lists only), and the rules below
(host allowlist, night window, one operation per host, revert unless
healthy) hold whatever the caller says.

All effects go through two seams so tests need no git remote or SSH:
`run(argv, stdin=None, cwd=None) -> (returncode, stdout bytes, stderr str)`
for local commands and `remote(host, verb_line, stdin=None) ->
(returncode, output str)` for the forced command on a host.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("staff.door")

ROLL_ORDER = ("suzuka", "spa", "silverstone", "monza")
NIGHT_STRICT = ("monza",)  # urgent is not enough here at night
# A dry activation runs the new system's activation script as root, so it is
# gated like a switch.
NIGHT_OPS = ("dry-activate", "switch", "flake_update", "restart", "reboot")
REV_RE = re.compile(r"[0-9a-f]{40}")
UNIT_RE = re.compile(r"[A-Za-z0-9@_.:-]+\.(service|timer|socket)")
# Never restarted for the staff: the deploy path, the staff itself, the way
# in, and anything that takes the host down. The host checks again, by alias too.
RESTART_DENIED = re.compile(r"(staff-deploy-|systemd-|sshd|ssh[.@]|home-staff-|dbus)"
                            r"|.*(reboot|poweroff|halt|kexec|shutdown)")
APPROVAL_SKEW_S = 60
MAX_PATCH = 1_000_000  # bytes
MAX_DIFF = 200_000  # bytes of diff in a prepare reply
SETTLE_S = 150  # after a failed switch, how long to watch the host go back
MAX_REQUEST = 4 * MAX_PATCH  # a patch grows when JSON-escaped
FLAKE_BRANCH = "staff/flake-update"
POLL_S = 10
STATUS_TIMEOUT_S = 30
CONFIRM_MARGIN_S = 30  # stop waiting this long before the host's revert fires
IN_FLIGHT = ("building", "activating")
GIT_ID = ["-c", "user.name=home-staff door", "-c", "user.email=door@home-staff.invalid"]


class Refused(Exception):
    """A request the door will not or could not carry out. The message goes to Tom."""

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


def _tail(text: str, n: int = 2000) -> str:
    return text.strip()[-n:]


def is_night(hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # the window wraps midnight


def night_refusal(op: str, host: str, urgent: bool, approved_ts: float | None,
                  now: datetime, start_hour: int, end_hour: int) -> str | None:
    """Why `op` on `host` must wait for morning, or None if it may go ahead.

    `now` is local time; `approved_ts` is the epoch time of Tom's yes.
    """
    if op not in NIGHT_OPS or not is_night(now.hour, start_hour, end_hour):
        return None
    window = f"{start_hour:02d}:00 to {end_hour:02d}:00"
    if urgent is not True:
        return f"It is night ({window}); {op} on {host} waits for morning unless the item is urgent."
    if host in NIGHT_STRICT:
        start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        if approved_ts is None or not start.timestamp() <= approved_ts <= now.timestamp():
            return (f"It is night ({window}); {op} on {host} needs a yes from Tom given during "
                    "this same night, even for an urgent item.")
    return None


def ssh_argv(key: str, address: str, verb_line: str) -> list[str]:
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "ConnectTimeout=15", f"staff-deploy@{address}", verb_line]


async def _exec(argv: list[str], stdin: bytes | None, cwd: str | None, timeout: float):
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except BaseException as e:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        if isinstance(e, asyncio.TimeoutError):
            return 124, b"", f"{argv[0]} timed out after {timeout:.0f} s"
        raise
    return proc.returncode, out, err.decode(errors="replace")


def _item(req: dict) -> int:
    item = req.get("item_id")
    if type(item) is not int or not 0 <= item < 2**31:
        raise Refused("item_id must be a whole number.")
    return item


def _rev(req: dict) -> str:
    rev = req.get("rev")
    if not isinstance(rev, str) or not REV_RE.fullmatch(rev):
        raise Refused("rev must be a full 40-character commit id.")
    return rev


def _urgent(req: dict) -> bool:
    urgent = req.get("urgent", False)
    if type(urgent) is not bool:
        raise Refused("urgent must be true or false.")
    return urgent


def _unit(req: dict) -> str:
    unit = req.get("unit")
    if (not isinstance(unit, str) or len(unit) > 200 or not UNIT_RE.fullmatch(unit)
            or unit.startswith("-")):
        raise Refused("unit must be a plain .service, .timer, or .socket name.")
    if RESTART_DENIED.match(unit):
        raise Refused(f"{unit} is not for the staff to restart: it belongs to the deploy path, "
                      "the staff, remote access, or the system itself. Tom has to do this one.")
    return unit


def _loggable(req: dict) -> str:
    """The request as one log line; the patch body is never logged."""
    out = {}
    for k, v in list(req.items())[:20]:
        if k == "patch":
            out[k] = f"<{len(v) if isinstance(v, str) else '?'} chars>"
        else:
            text = json.dumps(v, default=str)
            out[str(k)[:40]] = v if len(text) <= 200 else text[:200] + "..."
    return json.dumps(out)


def _failed(st: dict) -> set[str] | None:
    """The names of the failed units, or None if the status does not give them all."""
    names, count = st.get("failed_list"), st.get("failed_units", "")
    if names is None or not count.isdigit() or int(count) != len(names.split()):
        return None  # missing, or more failed units than the host lists
    return set(names.split())


def _healthy(st: dict, rev: str, failed_before: list) -> bool:
    """No unit may be failed now that was not failed before the switch."""
    failed = _failed(st)
    return (st.get("active_rev") == rev and st.get("switch") == "succeeded"
            and st.get("revert_pending") == "yes"
            and st.get("system_running") in ("running", "degraded")
            and failed is not None and failed <= set(failed_before))


class Door:
    def __init__(self, cfg: dict, run=None, remote=None, now=datetime.now, sleep=asyncio.sleep):
        self.cfg = cfg
        self.checkout = cfg["checkout"]
        self.addresses = {h: a for h, a in cfg["hosts"].items() if h in ROLL_ORDER}
        self.roll = [h for h in ROLL_ORDER if h in self.addresses]
        self.night = (int(cfg.get("night_start_hour", 0)), int(cfg.get("night_end_hour", 7)))
        self.revert_after_s = int(cfg.get("revert_after_s", 300))
        self.build_timeout_s = int(cfg.get("build_timeout_s", 3600))
        self.approval_max_age_s = int(cfg.get("approval_max_age_s", 86400))
        self.run = run or self._run
        self.remote = remote or self._remote
        self.now = now
        self.sleep = sleep
        self.locks = {h: asyncio.Lock() for h in self.roll}
        self.repo = asyncio.Lock()  # the checkout's work tree, one user at a time
        self.state_path = Path(cfg["state_path"])
        # hosts: host -> rev confirmed healthy there. pending: host -> a switch
        # that is waiting for health. flake_update: the rev being rolled out.
        self.state = {"hosts": {}, "pending": {}, "flake_update": None}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text()))
        self._tasks: set[asyncio.Task] = set()

    # --- seams (production) ---

    async def _run(self, argv, stdin=None, cwd=None):
        return await _exec(argv, stdin, cwd, 1800)

    async def _remote(self, host, verb_line, stdin=None):
        argv = ssh_argv(self.cfg["ssh_key"], self.addresses[host], verb_line)
        rc, out, err = await _exec(argv, stdin, None, 3600)
        return rc, out.decode(errors="replace") + err

    # --- state ---

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.state, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # --- checks shared by the operations ---

    def _host(self, req: dict) -> str:
        host = req.get("host")
        if host == "miami":
            raise Refused("miami is not served by the door in this version (it cannot build itself).")
        if not isinstance(host, str) or host not in self.addresses:
            raise Refused(f"Unknown host. The door serves: {', '.join(self.roll)}.")
        return host

    def _approval(self, req: dict) -> float:
        """When Tom said yes. The front desk checks this too; the door does not take its word."""
        ts = req.get("approved_ts")
        if type(ts) not in (int, float) or not math.isfinite(ts):
            raise Refused("approved_ts must be the time of Tom's yes, in epoch seconds.")
        age = self.now().timestamp() - ts
        if age < -APPROVAL_SKEW_S:
            raise Refused("approved_ts is in the future; that is not a yes from Tom.")
        if age > self.approval_max_age_s:
            raise Refused("Tom's yes (approved_ts) is too old; he has to be asked again.")
        return ts

    def _night_ok(self, op: str, host: str, urgent: bool, approved_ts: float | None) -> str:
        """Refuse at night per the rules; return the flag the host needs, if any."""
        now = self.now()
        why = night_refusal(op, host, urgent, approved_ts, now, *self.night)
        if why:
            raise Refused(why)
        strict_night = op in NIGHT_OPS and host in NIGHT_STRICT and is_night(now.hour, *self.night)
        return " --night-ok" if strict_night else ""

    def _take(self, host: str) -> asyncio.Lock:
        """The host's lock, for `async with`. A busy host is refused, not queued."""
        lock = self.locks[host]
        if lock.locked():
            raise Refused(f"{host} is busy with another operation; try again when it finishes.")
        return lock

    # --- git ---

    async def _git(self, *args: str, check: bool = True) -> str:
        rc, out, err = await self.run(["git", *GIT_ID, *args], cwd=self.checkout)
        if rc != 0 and check:
            raise Refused(f"git {args[0]} failed: {_tail(err or out.decode(errors='replace'), 500)}")
        return out.decode(errors="replace")

    async def _detach_at_main(self) -> None:
        """Fetch, then put the work tree on a clean, detached origin/main."""
        if not Path(self.checkout, ".git").exists():
            rc, _, err = await self.run(["git", "clone", "--quiet", self.cfg["repo_url"], self.checkout])
            if rc != 0:
                raise Refused(f"git clone failed: {_tail(err, 500)}")
        await self._git("fetch", "--quiet", "--prune", "origin")
        await self._git("am", "--abort", check=False)
        await self._git("checkout", "--quiet", "--force", "--detach", "origin/main")
        await self._git("clean", "-fdxq")

    async def _head(self) -> str:
        return (await self._git("rev-parse", "HEAD")).strip()

    async def _require_tip(self, branch: str, rev: str) -> None:
        tip = await self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if tip.strip() != rev:
            raise Refused(f"{rev[:12]} is not the tip of {branch}; it must be prepared again.")

    # --- hosts ---

    async def _status(self, host: str) -> dict:
        try:
            rc, out = await asyncio.wait_for(self.remote(host, "status"), STATUS_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise Refused(f"{host} did not answer a status request.") from None
        if rc != 0:
            raise Refused(f"{host} could not report its status: {_tail(out, 500)}")
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    async def _archive(self, rev: str) -> bytes:
        """The tree of `rev` as a tar archive: exactly what was reviewed."""
        # export-ignore and export-subst would make the archive differ from the
        # tree, so a tree that carries any attributes file is not sent at all.
        names = await self._git("ls-tree", "-r", "-z", "--name-only", rev)
        if any(n.rsplit("/", 1)[-1] == ".gitattributes" for n in names.split("\0")):
            raise Refused(f"{rev[:12]} contains a .gitattributes file; the door does not deploy such a tree.")
        rc, tar, err = await self.run(["git", "archive", "--format=tar", rev], cwd=self.checkout)
        if rc != 0:
            raise Refused(f"git archive failed: {_tail(err, 500)}")
        return tar

    async def _switch(self, host: str, rev: str, flag: str) -> dict:
        before = await self._status(host)
        if before.get("revert_pending") == "yes" or before.get("switch") in IN_FLIGHT:
            raise Refused(f"{host} is still in the middle of an earlier switch.")
        failed_before = _failed(before)
        if failed_before is None or not before.get("current_system"):
            raise Refused(f"{host} gave a status the door could not read, or has more failed "
                          "units than it can list.")
        tar = await self._archive(rev)
        rc, out = await self.remote(host, f"switch {rev}{flag}", stdin=tar)
        if rc != 0:
            raise Refused(f"{host} did not start the switch.", log=_tail(out))
        # The host builds first; its revert timer starts only when it activates.
        self.state["pending"][host] = {
            "rev": rev, "failed_before": sorted(failed_before),
            "system_before": before["current_system"], "deadline": None,
            "build_deadline": self.now().timestamp() + self.build_timeout_s + 2 * POLL_S}
        self._save()
        return await self._await_health(host)

    async def _settle(self, host: str, p: dict, last: dict) -> tuple[str, dict]:
        """After a failed switch: watch the host's own revert finish, then say
        where the host is. Returns the sentence and the last status read."""
        def settled(st: dict) -> bool:
            return bool(st) and st.get("revert_pending") != "yes" and st.get("switch") not in IN_FLIGHT

        give_up = self.now().timestamp() + CONFIRM_MARGIN_S + SETTLE_S
        while not settled(last) and self.now().timestamp() < give_up:
            await self.sleep(POLL_S)
            try:
                last = await self._status(host)
            except Refused:
                last = {}
        before = p.get("system_before")
        if before and last.get("current_system") == before and last.get("revert_pending") != "yes":
            return "The host is on its previous system.", last
        return ("The door could not see the host back on its previous system: it may still be "
                "running the new one and needs Tom."), last

    async def _await_health(self, host: str) -> dict:
        """Wait out the build, then poll until the activated system is healthy
        and confirm it. Otherwise change nothing: the host's own timer puts the
        previous system back, and its boot default never moved. The door only
        watches that happen and reports where the host ended up."""
        p = self.state["pending"][host]
        rev = p["rev"]
        failed_before = p["failed_before"] if isinstance(p.get("failed_before"), list) else []
        last: dict = {}
        while True:
            now = self.now().timestamp()
            if p["deadline"] is None and now >= p["build_deadline"]:
                why = f"the build did not finish within {self.build_timeout_s} s"
                break
            if p["deadline"] is not None and now >= p["deadline"]:
                why = f"it did not report healthy within {self.revert_after_s} s of activating"
                break
            try:
                last = await self._status(host)
            except Refused:
                last = {}  # the host may be restarting its network; keep trying
            if _healthy(last, rev, failed_before):
                rc, out = await self.remote(host, f"confirm {rev}")
                if rc == 0:
                    del self.state["pending"][host]
                    self.state["hosts"][host] = rev
                    self._save()
                    return {"rev": rev, "log": _tail(out), "status": last}
                why = f"it did not accept the confirm ({_tail(out, 300)})"
                break
            state = last.get("switch")
            if state in ("failed", "lost"):
                why = ("the host restarted before the confirm" if state == "lost"
                       else "the build or the activation failed on the host")
                break
            if last and state != "building":
                if p["deadline"] is None:  # activation began: the host's revert timer is running
                    p["deadline"] = now + max(self.revert_after_s - CONFIRM_MARGIN_S, 1)
                    self._save()
                elif last.get("revert_pending") != "yes":
                    why = "the host already went back to its previous system"
                    break
            await self.sleep(POLL_S)
        where, last = await self._settle(host, p, last)
        del self.state["pending"][host]
        self._save()
        raise Refused(f"Switch of {host} to {rev[:12]} failed: {why}. {where}", status=last)

    # --- operations ---

    async def prepare(self, req: dict) -> dict:
        item = _item(req)
        patch = req.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise Refused("patch must be the text of git format-patch.")
        data = patch.encode()
        if len(data) > MAX_PATCH:
            raise Refused(f"The patch is larger than {MAX_PATCH} bytes.")
        async with self.repo:
            await self._detach_at_main()
            base = await self._head()
            rc, _, err = await self.run(["git", *GIT_ID, "am", "--quiet"], stdin=data, cwd=self.checkout)
            if rc != 0:
                await self._git("am", "--abort", check=False)
                raise Refused(f"The patch does not apply to main: {_tail(err, 800)}")
            rev = await self._head()
            if rev == base:
                raise Refused("The patch changed nothing.")
            numstat = await self._git("diff", "--numstat", "-z", "--no-renames", base, rev)
            files = []
            for record in numstat.split("\0"):
                if record.strip():
                    added, removed, path = record.split("\t", 2)
                    files.append({"path": path,  # binary files have no line counts
                                  "added": int(added) if added.isdigit() else None,
                                  "removed": int(removed) if removed.isdigit() else None})
            names = {f["path"].rsplit("/", 1)[-1] for f in files}
            if "flake.lock" in names:
                raise Refused("The patch changes flake.lock. Only the door's own flake update may do that.")
            if ".gitattributes" in names:
                raise Refused("The patch changes a .gitattributes file; such a patch is not accepted.")
            # Everything from main to the new tip, however many commits the patch has.
            diff = (await self._git("diff", "--no-ext-diff", "--no-textconv", base, rev)).encode()
            await self._git("branch", "--force", f"staff/item-{item}", rev)
        return {"rev": rev, "branch": f"staff/item-{item}", "files": files,
                "touches_flake": "flake.nix" in names,
                "diff": diff[:MAX_DIFF].decode(errors="ignore"), "diff_truncated": len(diff) > MAX_DIFF}

    async def deploy(self, req: dict) -> dict:
        host = self._host(req)
        action = req.get("action")
        if action not in ("dry-activate", "switch"):
            raise Refused("action must be dry-activate or switch.")
        rev, item = _rev(req), _item(req)
        flag = self._night_ok(action, host, _urgent(req), self._approval(req))
        async with self._take(host):
            async with self.repo:
                await self._require_tip(f"staff/item-{item}", rev)
            if action == "switch":
                return await self._switch(host, rev, flag)
            tar = await self._archive(rev)
            rc, out = await self.remote(host, f"dry-activate {rev}{flag}", stdin=tar)
            if rc != 0:
                raise Refused(f"dry-activate failed on {host}.", log=_tail(out))
            return {"rev": rev, "log": _tail(out)}

    async def _new_flake_update(self) -> str | None:
        """Build the flake-update commit from origin/main. None if the lock did not change."""
        await self._detach_at_main()
        rc, out, err = await self.run(["nix", "flake", "update"], cwd=self.checkout)
        if rc != 0:
            raise Refused(f"nix flake update failed: {_tail(err, 800)}")
        porcelain = await self._git("status", "--porcelain", "--untracked-files=all")
        changed = [line[3:] for line in porcelain.splitlines() if line.strip()]
        if not changed:
            return None
        if changed != ["flake.lock"]:
            await self._git("reset", "--quiet", "--hard")
            await self._git("clean", "-fdxq")
            raise Refused("The flake update changed more than flake.lock: " + ", ".join(changed[:5]))
        await self._git("commit", "--quiet", "-m", "flake.lock: update", "--", "flake.lock")
        rev = await self._head()
        await self._git("branch", "--force", FLAKE_BRANCH, rev)
        self.state["flake_update"] = rev
        self._save()
        return rev

    async def flake_update(self, req: dict) -> dict:
        host = self._host(req)
        _item(req)
        # Unattended: no yes from Tom is needed, and `urgent` is not listened to,
        # so a flake update never runs in the night window.
        flag = self._night_ok("flake_update", host, False, None)
        async with self._take(host):
            if host == self.roll[0]:
                async with self.repo:
                    rev = await self._new_flake_update()
                if rev is None:
                    return {"changed": False}
            else:
                rev = self.state.get("flake_update")
                if not rev:
                    raise Refused(f"No flake update is under way; it starts on {self.roll[0]}.")
                for earlier in self.roll[:self.roll.index(host)]:
                    if self.state["hosts"].get(earlier) != rev:
                        raise Refused(f"{earlier} comes before {host} and is not confirmed "
                                      "on this flake update yet.")
                async with self.repo:
                    await self._require_tip(FLAKE_BRANCH, rev)
            return {"changed": True, **await self._switch(host, rev, flag)}

    async def restart(self, req: dict) -> dict:
        host, unit = self._host(req), _unit(req)
        _item(req)
        flag = self._night_ok("restart", host, _urgent(req), self._approval(req))
        async with self._take(host):
            rc, out = await self.remote(host, f"restart {unit}{flag}")
        if rc != 0:
            raise Refused(f"Restarting {unit} on {host} failed.", log=_tail(out))
        return {"log": _tail(out)}

    async def reboot(self, req: dict) -> dict:
        host = self._host(req)
        _item(req)
        flag = self._night_ok("reboot", host, _urgent(req), self._approval(req))
        async with self._take(host):
            rc, out = await self.remote(host, f"reboot{flag}")
        if rc != 0:
            raise Refused(f"{host} did not accept the reboot.", log=_tail(out))
        return {"log": _tail(out)}

    async def status(self, req: dict) -> dict:
        host = self._host(req)
        st = await self._status(host)
        return {"host": host, "rev": st.get("active_rev") or None,
                "revert_pending": st.get("revert_pending") == "yes",
                "confirmed_rev": self.state["hosts"].get(host), "status": st}

    async def finish(self, req: dict) -> dict:
        hosts = req.get("hosts")
        if not isinstance(hosts, list) or not hosts:
            raise Refused("hosts must list the hosts the change was deployed to.")
        hosts = [self._host({"host": h}) for h in hosts]
        flake = req.get("flake_update") is True
        if flake:
            rev, branch = self.state.get("flake_update"), FLAKE_BRANCH
            if not rev:
                raise Refused("No flake update is under way.")
        else:
            rev, branch = _rev(req), f"staff/item-{_item(req)}"
        missing = [h for h in hosts if self.state["hosts"].get(h) != rev]
        if missing:
            raise Refused(f"Not confirmed on {rev[:12]}: {', '.join(missing)}.")
        async with self.repo:
            await self._require_tip(branch, rev)
            # A stale staff/ branch on the remote may be replaced, but only the
            # one just fetched: the lease names the value it must still have
            # (empty: it must not exist). Refs outside staff/ are never forced.
            await self._git("fetch", "--quiet", "--prune", "origin")
            seen = (await self._git("rev-parse", "--verify", "--quiet",
                                    f"refs/remotes/origin/{branch}", check=False)).strip()
            await self._git("push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{seen}",
                            "origin", f"{rev}:refs/heads/{branch}")
            # Never forced: if main moved, this fails and Tom hears about it.
            rc, _, err = await self.run(["git", *GIT_ID, "push", "--quiet", "origin", f"{rev}:refs/heads/main"],
                                        cwd=self.checkout)
            if rc != 0:
                # Take the branch back so a later, rebased finish can push it.
                await self._git("push", "--quiet", "origin", "--delete", f"refs/heads/{branch}", check=False)
                raise Refused("main could not be fast-forwarded (it has probably moved); nothing was "
                              f"forced. The change must be prepared again. {_tail(err, 500)}")
            if flake:
                self.state["flake_update"] = None
                self._save()
        return {"rev": rev, "branch": branch}

    OPS = {"prepare": prepare, "deploy": deploy, "flake_update": flake_update, "restart": restart,
           "reboot": reboot, "status": status, "finish": finish}

    # --- the socket ---

    async def handle(self, raw: bytes) -> dict:
        """One request in, one reply out. Never raises."""
        op = None
        try:
            try:
                req = json.loads(raw)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                log.info("request <not a JSON object, %d bytes>", len(raw))
                raise Refused("The request was not a JSON object.")
            log.info("request %s", _loggable(req))
            op = req.get("op")
            if not isinstance(op, str) or op not in self.OPS:
                op = None
                raise Refused("Unknown op.")
            reply = {"ok": True, **await self.OPS[op](self, req)}
        except Refused as e:
            reply = {"ok": False, "error": str(e), **e.extra}
        except Exception:
            log.exception("internal error")
            reply = {"ok": False, "error": "The door hit an internal error; its log has the details."}
        log.info("outcome op=%s ok=%s %s", op, reply["ok"],
                 json.dumps(reply.get("error") or reply.get("rev") or ""))
        return reply

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), 30)
        except (ValueError, asyncio.TimeoutError, OSError):  # ValueError: over MAX_REQUEST
            raw = b""
        reply = await self.handle(raw)
        with contextlib.suppress(OSError):  # the caller may be gone; the work is done anyway
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
            writer.close()

    async def _resume(self, host: str) -> None:
        async with self.locks[host]:
            try:
                await self._await_health(host)
                log.info("outcome op=resume host=%s ok=True", host)
            except Refused as e:
                log.info("outcome op=resume host=%s ok=False %s", host, json.dumps(str(e)))

    def resume(self) -> None:
        """Pick up switches that were waiting for health when the door last
        stopped (a switch of suzuka can restart the door itself)."""
        for host in list(self.state["pending"]):
            if host in self.locks:
                task = asyncio.create_task(self._resume(host))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                del self.state["pending"][host]

    async def serve(self) -> None:
        path = self.cfg["socket"]
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        server = await asyncio.start_unix_server(self._client, path=path, limit=MAX_REQUEST)
        os.chmod(path, 0o660)
        self.resume()
        log.info("listening on %s", path)
        async with server:
            await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    cfg = json.loads(Path(os.environ["DOOR_CONFIG"]).read_text())
    asyncio.run(Door(cfg).serve())


if __name__ == "__main__":
    main()
