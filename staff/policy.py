"""What a member may do without asking, after a yes, or never.

This mirrors the autonomy table in BRIEF.md. It is a second line of defense:
the first is the operating system (each member's user can only reach what
the NixOS module grants it). The staff can change its behavior, never this.

Reading a shell command is guesswork: a member can always hide a command in
a script it wrote. So this is strict where it can be (anything that looks
like a state change needs a yes unless it is on a short list of read-only
forms). Deploys, restarts, and reboots do not rest on it at all: members have
no command for them, only the front desk's tools, which ask the door.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

FREE = "free"  # without asking
NEEDS_YES = "needs_yes"  # only while working an item Tom approved
TOM_ONLY = "tom_only"  # never

_RANK = {FREE: 0, NEEDS_YES: 1, TOM_ONLY: 2}

# Checked against the whole command. First match wins.
_BASH_RULES: list[tuple[str, str, str]] = [
    (TOM_ONLY, r"\bsudoers\b|\bvisudo\b|\bsops\b|\bage(-keygen)?\b|/run/secrets|\.claude/settings|members/|staff/policy\.py",
     "permissions, secrets, and the staff's own rules are Tom's"),
    (TOM_ONLY, r"(^|[;&|(]\s*)(env|printenv|set)\s*($|[|;&>)])|ANTHROPIC_API_KEY|/proc/\S+/environ|\bsqlite3\b",
     "the staff's own keys and item list are Tom's"),
    (TOM_ONLY, r"\bclimate\b|thermostat", "the thermostat is Tom's"),
    (TOM_ONLY, r"\bzfs\s+destroy|\bzpool\s+destroy|\bmkfs|\bdd\s+if=|\bwipefs\b|\bshred\b",
     "destructive commands are Tom's"),
    (NEEDS_YES, r"\bfleet-deploy|switch-to-configuration",
     "a deploy needs Tom's yes"),
    (NEEDS_YES, r"\breboot\b|\bshutdown\b|\bpoweroff\b|\bhalt\b|\bkexec\b|\bp?kill(all)?\b",
     "restarting or stopping a service or host needs Tom's yes"),
    (NEEDS_YES, r"\bgh\s+pr\s+merge", "merging to the main branch needs Tom's yes"),
]

_SYSTEMCTL_READS = {
    "status", "show", "cat", "list-units", "list-unit-files", "list-timers", "list-sockets",
    "list-dependencies", "list-jobs", "is-active", "is-enabled", "is-failed", "is-system-running",
    "get-default", "help",
}
_SYSTEMCTL_TAKES_VALUE = {"-H", "--host", "-M", "--machine", "-t", "--type", "-p", "--property", "-n", "--lines", "-o", "--output"}
_REBUILD_READS = {"build", "dry-build", "dry-activate", "build-vm", "list-generations", "repl"}
_SEGMENT = re.compile(r"&&|\|\||[;|\n]|\$\(|`")


@dataclass
class Verdict:
    level: str
    reason: str = ""

    def allowed(self, item_state: str | None) -> bool:
        if self.level == FREE:
            return True
        if self.level == NEEDS_YES:
            return item_state == "approved"
        return False


def _plain(command: str) -> str:
    """Drop quotes and backslashes so `reb\\oot` and `'re'boot` read as what the shell runs."""
    return re.sub(r"[\\'\"]", "", command)


def _after(tokens: list[str], name: str) -> list[str] | None:
    """The words after the command `name`, wherever it sits (after sudo, ssh host, a full path)."""
    for i, t in enumerate(tokens):
        if t == name or t.endswith("/" + name):
            return tokens[i + 1:]
    return None


def _segment(tokens: list[str]) -> Verdict:
    """Rules that need the words of one command, not a pattern."""
    args = _after(tokens, "systemctl")
    if args is not None:
        verb, skip = None, False
        for a in args:
            if skip:
                skip = False
            elif a in _SYSTEMCTL_TAKES_VALUE:
                skip = True
            elif not a.startswith("-"):
                verb = a
                break
        if verb not in _SYSTEMCTL_READS:
            return Verdict(NEEDS_YES, "changing a service needs Tom's yes")

    args = _after(tokens, "service")
    if args is not None and set(args) & {"restart", "reload", "stop", "start"}:
        return Verdict(NEEDS_YES, "changing a service needs Tom's yes")

    args = _after(tokens, "nixos-rebuild")
    if args is not None and not (set(args) & _REBUILD_READS and not set(args) & {"switch", "boot", "test"}):
        return Verdict(NEEDS_YES, "a deploy needs Tom's yes")

    args = _after(tokens, "rm")
    if args is not None:
        flags = [a for a in args if a.startswith("-")]
        if any(a == "--recursive" or (not a.startswith("--") and re.search("[rR]", a)) for a in flags):
            return Verdict(TOM_ONLY, "deleting a whole directory is Tom's")

    args = _after(tokens, "git")
    if args is not None and "push" in args:
        rest = args[args.index("push") + 1:]
        if any(a in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d") or a.startswith("--force") for a in rest):
            return Verdict(TOM_ONLY, "force pushes and deleting branches are Tom's")
        refs = [a for a in rest if not a.startswith("-")][1:]  # after the remote
        if any(r.startswith("+") or r.startswith(":") for r in refs):
            return Verdict(TOM_ONLY, "force pushes and deleting branches are Tom's")
        if not refs or "--all" in rest:
            return Verdict(NEEDS_YES, "a push that does not name its branch needs Tom's yes")
        for r in refs:
            dest = r.split(":")[-1].removeprefix("refs/heads/")
            if dest in ("main", "master", "HEAD"):
                return Verdict(NEEDS_YES, "merging to the main branch needs Tom's yes")
    return Verdict(FREE)


def check_bash(command: str, protected: list[str] | tuple[str, ...] = ()) -> Verdict:
    """The strictest verdict any part of the command earns.

    `protected` are paths (the item list, token files) no member command may name.
    """
    plain = _plain(command)
    worst = Verdict(FREE)
    for path in protected:
        if path and path in plain:
            return Verdict(TOM_ONLY, "the staff's own keys and item list are Tom's")
    for level, pattern, reason in _BASH_RULES:
        if re.search(pattern, plain):
            worst = Verdict(level, reason)
            break
    for part in _SEGMENT.split(plain):
        v = _segment(part.split())
        if _RANK[v.level] > _RANK[worst.level]:
            worst = v
    return worst


def check_path(path: str, protected: list[str] | tuple[str, ...]) -> Verdict:
    """May a member read this path? Secrets and the staff's own state are Tom's."""
    try:
        target = Path(path).resolve()
    except (OSError, ValueError):
        return Verdict(TOM_ONLY, "unreadable path")
    for root in ("/run/secrets", *protected):
        if not root:
            continue
        r = Path(root).resolve()
        if target == r or r in target.parents or target in r.parents:
            return Verdict(TOM_ONLY, "secrets and the staff's own state are Tom's")
    return Verdict(FREE)


def nudge(state: str, asked_ts: float, reminded_ts: float | None, now: float | None = None) -> str | None:
    """What to do about an item that is waiting on Tom: 'remind', 'shelve', or None.

    TOM: this is yours to shape. The brief says one reminder, then shelve.
    The numbers below are my defaults; change them or replace the logic (for
    example, remind the next time you write in the room rather than after a
    fixed wait).
    """
    now = now or time.time()
    remind_after = 20 * 3600  # roughly "the next day"
    shelve_after = 48 * 3600  # after the reminder
    if state not in ("pitched", "proposed", "waiting"):
        return None
    if reminded_ts is None:
        return "remind" if now - asked_ts >= remind_after else None
    return "shelve" if now - reminded_ts >= shelve_after else None
