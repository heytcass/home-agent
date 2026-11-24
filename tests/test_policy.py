import pytest

from staff import policy
from staff.policy import FREE, NEEDS_YES, TOM_ONLY


@pytest.mark.parametrize("cmd,level", [
    # no command is free because of what it is chained to
    ("echo ok && rm -rf /var/lib/hass", TOM_ONLY),
    ("echo ok && sudo systemctl restart nginx", NEEDS_YES),
    # finding 3: needs a yes
    ("systemctl --no-block restart x", NEEDS_YES),
    ("systemctl -H monza restart x", NEEDS_YES),
    ("systemctl try-restart x", NEEDS_YES),
    ("systemctl isolate rescue.target", NEEDS_YES),
    ("systemctl daemon-reload", NEEDS_YES),
    ("service nginx restart", NEEDS_YES),
    ("nixos-rebuild --flake .#monza switch", NEEDS_YES),
    ("nixos-rebuild --target-host monza switch", NEEDS_YES),
    ("/run/current-system/bin/switch-to-configuration switch", NEEDS_YES),
    ("git push -u origin main", NEEDS_YES),
    ("git push origin HEAD:main", NEEDS_YES),
    ("git push", NEEDS_YES),
    ("halt", NEEDS_YES),
    ("kill -HUP 1", NEEDS_YES),
    ("reb\\oot", NEEDS_YES),
    ("nixos-rebuild build && nixos-rebuild switch", NEEDS_YES),
    # finding 3: Tom only
    ("git push origin +main", TOM_ONLY),
    ("git push --force origin x", TOM_ONLY),
    ("rm -fr x", TOM_ONLY),
    ("rm -Rf x", TOM_ONLY),
    ("rm -r -f x", TOM_ONLY),
    # finding 3: free
    ("journalctl -u synapse.service -n 200", FREE),
    ("systemctl -H monza is-active x", FREE),
    ("ssh suzuka systemctl status nginx 2>&1 | tail", FREE),
    ("git push origin fix/eink", FREE),
    ("git -C repo push -u origin fix/x", FREE),
    ("nixos-rebuild build --flake .#monza", FREE),
    ("rm old.txt", FREE),
])
def test_bash_levels(cmd, level):
    assert policy.check_bash(cmd).level == level, cmd


@pytest.mark.parametrize("cmd", ["sqlite3 /var/lib/x/t.db .dump", "env", "printenv", "env | grep KEY"])
def test_env_and_sqlite_are_tom_only(cmd):
    assert policy.check_bash(cmd).level == TOM_ONLY, cmd


def test_env_as_a_wrapper_is_not_tom_only():
    assert policy.check_bash("/usr/bin/env python x.py").level != TOM_ONLY


def test_protected_paths_are_named_by_no_command():
    prot = ["/run/tokens/chief", "/var/lib/home-staff"]
    assert policy.check_bash("cat /run/tokens/chief", protected=prot).level == TOM_ONLY
    assert policy.check_bash("ls /var/lib/home-staff", protected=prot).level == TOM_ONLY
    assert policy.check_bash("ls /var/lib/other", protected=prot).level == FREE
