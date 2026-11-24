# Architecture: who holds what, and how a change reaches a host

Approved by Tom on 2026-11-23. BRIEF.md says what the staff must do; this
file says how the pieces are separated. Where they disagree, BRIEF.md wins.

## The principle

Members can read everything and change nothing. Every change to the world
goes through one door, and the door's caller (the front desk) checks Tom's
yes itself. A member never holds a credential that can change anything.

## Processes on Suzuka

| Unit | User | Holds | Has a model inside |
|---|---|---|---|
| `home-staff-frontdesk` | `home-staff` | item list, Matrix tokens, memory checkout | only the tool-less router call |
| `home-staff-member-<name>@` (socket-started, one per run) | `home-staff-<name>` | its own work folder; fleet also holds a read-only SSH key | yes |
| `home-staff-relay` | `home-staff-relay` | the Anthropic key, its own spend count | no |
| `home-staff-door` | `home-staff-door` | deploy SSH key, git write access to the flake repo | no |

Paths:

- `/var/lib/home-staff/desk/` (front desk only): `items.db`, `memory/`
- `/var/lib/home-staff/members/<name>/` (that member only): `work/`, `claude/`
- `/var/lib/home-staff/relay/` (relay only): `spend.json`
- `/var/lib/home-staff/door/` (door only): `flake/` (clean clone), `state.json`
- `/run/home-staff/members/<name>.sock` (connect: front desk only)
- `/run/home-staff-door/door.sock` (the door's own directory; connect: front desk only, through the door's group)
- `/run/home-staff/relay-tokens/<name>` (read: that member only; `desk` for the front desk)

## What is public and what stays in the house

This repo is public and holds code only. The hosts take it as a flake
input; removing the input and the imports removes every declared piece.

The item list, the lessons (`desk/memory`, its own local git repo, created
on first start), and the inventory never leave the house. Lessons are
backed up by the hosts' existing snapshots, not pushed anywhere. No member
has write access to this repo, so the staff's own code changes only when
Tom pushes.

## Wire 1: front desk to member worker

The front desk connects to `/run/home-staff/members/<name>.sock`. systemd
starts `staff-worker` as the member's user with the connection as stdin and
stdout. Both sides speak JSON, one object per line, UTF-8.

Front desk sends first, exactly one job:

```json
{"job": {"member": "fleet", "item_id": 7, "prompt": "...", "system_prompt": "...",
         "model": "claude-sonnet-5-5", "builtin_tools": ["Bash", "Read"],
         "tools": [{"name": "say", "description": "...", "schema": {"kind": "str", "text": "str"}}],
         "hook_matchers": ["Bash", "Edit|Write", "Read|Grep|Glob"],
         "resume": null, "max_turns": 40, "budget_usd": 0.75}}
```

Schema values are the type names `str`, `int`, `float`, `bool`.

Worker to front desk:

- `{"call": 3, "tool": "say", "args": {...}}` a member tool call. Reply:
  `{"reply": 3, "result": {"content": [...], "is_error": false}}`.
- `{"call": 4, "hook": "Bash", "input": {...}}` a PreToolUse hook; `input` is
  the SDK's hook input. Reply: `{"reply": 4, "result": {}}` or the deny object.
- `{"event": "session", "session_id": "..."}`
- `{"event": "assistant", "text": "..."}` (text may be empty; the event still
  matters: it means the API was called)
- `{"event": "result", "session_id": "...", "total_cost_usd": 0.12, "num_turns": 5,
   "is_error": false, "subtype": "success", "errors": [], "result": "..."}`
- `{"event": "error", "error": "..."}` then the worker exits.

The worker owns nothing but the SDK call: every tool and every hook decision
is made by the front desk. The worker's working directory, `HOME`,
`CLAUDE_CONFIG_DIR`, `ANTHROPIC_BASE_URL`, and `ANTHROPIC_API_KEY` (its relay
token, never the real key) come from its unit, not from the job. A worker
that gets a job for a member other than `STAFF_MEMBER` in its environment
refuses it.

## Wire 2: the relay

`staff-relay` listens on `127.0.0.1:8377` and forwards to
`https://api.anthropic.com`. The caller's `x-api-key` (or `Authorization:
Bearer`) is a relay token that names a member; the relay swaps in the real
key. Only `POST /v1/messages` and `POST /v1/messages/count_tokens` are
forwarded; anything else is 404 and logged.

The relay counts spend itself from the `usage` in each response, at list
prices (an unknown model is charged at the highest price it knows), into
`spend.json`. When the day's or month's cap is reached it answers 429 with
an Anthropic-shaped error body and forwards nothing. This is the hard stop;
the item list's own spend records are for reporting and for not starting
runs.

Config: a JSON file named by `RELAY_CONFIG` with `listen_host`, `port`,
`upstream`, `key_file` (a file with the real key, or an env file line
`ANTHROPIC_API_KEY=...`), `tokens_dir` (one file per caller, file name is
the caller, content is the token), `state_path`, `daily_cap_usd`,
`monthly_cap_usd`.

## Wire 3: the door

`staff-door` listens on `/run/home-staff-door/door.sock`. One JSON request per
connection, one JSON reply, then close. Every reply has `ok` (bool) and, on
failure, `error` (a sentence a member can relay to Tom).

| Request | What the door does |
|---|---|
| `{"op": "prepare", "item_id": 7, "patch": "<git format-patch text>"}` | Fetch origin, make branch `staff/item-7` from `origin/main`, apply the patch with `git am`. A patch that changes a `flake.lock` or a `.gitattributes` is refused. Reply `rev`, `files` (a list of `{"path", "added", "removed"}`, counts null for a binary file), `touches_flake`, and `diff` (the commit against main, cut at 200 KB, with `diff_truncated`). Nothing is pushed. |
| `{"op": "deploy", "host": "suzuka", "action": "dry-activate"\|"switch", "rev": "<40 hex>", "item_id": 7, "urgent": false, "approved_ts": 1700000000}` | Refuse unless `rev` is the tip of `staff/item-7`. Send that commit's source to the host and run the action. For `switch`: wait for the host's health check, then confirm; if health fails or the host never answers, the host reverts by itself. Reply `log` (tail). |
| `{"op": "flake_update", "host": "suzuka", "item_id": 7, "urgent": false}` | For the first host (always suzuka): fetch origin, branch `staff/flake-update` from `origin/main`, run `nix flake update`, commit. Refuse if anything but `flake.lock` changed. Then switch that host. For later hosts: same commit, only in roll order, only if every earlier host is on it and healthy. |
| `{"op": "restart", "host": "spa", "unit": "matrix-synapse.service", "item_id": 7, "urgent": false, "approved_ts": ...}` | Restart one unit on the host. |
| `{"op": "reboot", "host": "monza", "item_id": 7, "urgent": false, "approved_ts": ...}` | Reboot the host. |
| `{"op": "finish", "item_id": 7, "rev": "...", "hosts": [...]}` or `{"op": "finish", "flake_update": true, "hosts": [...]}` | Refuse unless every host in the request's `hosts` list is confirmed on `rev`. Push the branch, fast-forward `main` to it, push `main`. |
| `{"op": "status", "host": "suzuka"}` | Deployed revision and whether a revert is pending. |

Rules the door enforces on its own, whatever the front desk says:

- Hosts are an allowlist: `suzuka spa silverstone monza`, in that roll order.
  `miami` is refused in this version (it cannot build itself).
- `deploy` (both actions), `restart`, and `reboot` need an `approved_ts`
  that is at most 24 hours old (`approval_max_age_s`). A dry-activate is
  gated like a switch because it runs the new system's activation script
  as root. `flake_update` needs no approval.
- Night window, default 00:00 to 07:00 local: `dry-activate`, `switch`,
  `restart`, and `reboot` are refused unless `urgent` is true, and
  `flake_update` is refused always. On `monza`
  they are refused at night even when urgent, unless `approved_ts` falls
  inside the same night window (Tom is awake and said yes just now).
- One operation per host at a time.
- Unit names match `^[A-Za-z0-9@_.:-]+\.(service|timer|socket)$`. Refused
  by name or alias: `staff-deploy-*`, `home-staff-*`, `systemd-*`, `sshd*`,
  `dbus*`, and anything that names a reboot, poweroff, halt, kexec, or
  shutdown.
- A tree that contains a `.gitattributes` is never sent to a host.

Config: a JSON file named by `DOOR_CONFIG` with `socket`, `repo_url`,
`checkout`, `state_path`, `ssh_key`, `hosts` (name to address),
`night_start_hour`, `night_end_hour`, `revert_after_s` (default 300),
`build_timeout_s` (default 3600), `approval_max_age_s` (default 86400).

## On each host

Two users, added by this repo's `nixosModules.host`:

- `staff-read`: the fleet member's read-only login. In `systemd-journal`. No
  sudo.
- `staff-deploy`: accepts only the door's key, with a forced command
  (`staff-deploy-host`). One sudo rule lets it run `staff-deploy-root`, a
  root-owned script from the Nix store, without a password.

Forced-command verbs (from `SSH_ORIGINAL_COMMAND`, strictly parsed):
`status`, `dry-activate <rev>`, `switch <rev>`, `confirm <rev>`,
`restart <unit>`, `reboot`. `dry-activate` and `switch` read a tar archive of
the flake source on stdin.

`switch` runs in a transient systemd unit, so it survives the SSH session
and a restart of the staff itself. In order:

1. Record the current system, then build the new one (`nixos-rebuild build`,
   limited to `BUILD_TIMEOUT_S`). A failed build changes nothing.
2. Arm the revert: a transient timer that re-activates the recorded system
   after `REVERT_AFTER_S`.
3. Activate with the new system's `switch-to-configuration test`. The
   running system changes; the system profile and the boot default do not.

`confirm` stops the timer, then sets the system profile and the boot
default to the new system. Until then a reboot lands on the old system with
nothing to undo. Logs go to `/var/log/fleet-deploy/`.

`status` prints nine `key=value` lines: `current_system`, `rev`,
`active_rev`, `switch` (`none`, `building`, `activating`, `succeeded`,
`failed`, or `lost`, the last meaning the host rebooted before a confirm),
`revert_pending`, `system_running`, `failed_units`, `log`, `failed_list`.

The door confirms only when `active_rev` is the requested commit, `switch`
is `succeeded`, the revert is still pending, the system is `running` or
`degraded`, and every unit failed now was already failed before the
switch. It waits out the build (up to `build_timeout_s`), then gives the
host `revert_after_s` minus a 30 second margin to become healthy.

Host settings live in `/etc/staff-deploy.conf`: `REVERT_AFTER_S` (default
300), `BUILD_TIMEOUT_S` (default 3600), `NIGHT_START_HOUR`,
`NIGHT_END_HOUR`. The first two must equal the door's values.

## What a yes covers

A proposal fixes a scope on the item: the commit (or the unit and verb) and
the hosts. Under the member's text the front desk writes its own block from
the door's `prepare` reply: the commit, the files, the diff itself (up to 60
changed lines), and a warning when `flake.nix` changes. Member text may not
contain the block's heading and is capped at 1500 characters, so the block
is always the last thing in the message.

The yes is stored on the scope, not on the item. It is set only when Tom's
thumbs-up or yes-reply targets that proposal message. A bare "ok" counts
only when that proposal is the newest staff message in the room. The yes is
cleared by a new proposal, a no, a steer, or any reply on the proposal that
is not a yes, and it stops counting after 24 hours. A member cannot move an
item with a scope into `proposed` or `approved` by itself.

Every tool that reaches the door with a change (`deploy` with either action,
`act`, `finish`) checks that yes when it is used. Under one yes each host is
switched once, in roll order, and a restart or reboot runs once. Before the
yes only `host_status` and `flake_update` reach a host.

Flake updates need no yes: the lock file is produced by the door, never by a
member.
