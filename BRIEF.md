# Home staff: project brief

Status: approved by Tom as the starting point, 2026-11-23. Nothing is built yet.

## Goal

A small staff of AI agents that lives on the homelab, owns parts of it, and
gets things done. Tom talks to them in one Matrix room in plain language.
They fix problems he didn't know about, act on what he drops in the room,
and bring him things he didn't ask for when those are useful or novel.

## How it must behave

1. **Driven by open work, never by the clock.** Activity starts when Tom
   says something, an alert arrives, or an owner has an unfinished item.
   No daily report. No message exists because a timer fired.
2. **An item is done when the world changed and it was checked.** A message
   is never the finish line.
3. **A problem is said once.** It becomes one tracked item that is worked
   until fixed or dropped. It is never re-announced.
4. **Gumption before asking.** Search, read, inspect, and try first. Ask
   with a best guess to confirm. A bare open question is allowed only after
   real effort, and the message shows what was tried.
5. **Pitch, then build.** Investigation is free. Anything beyond a small
   fix is pitched first: what it is, how we'd use it, what changes for Tom
   day to day, yes or no. A yes starts the work and it runs through to done.
6. **Everything is readable in the room.** Content is inline. Never a file
   path on a server, never a folder for Tom to sort.
7. **No thread hunting.** Tom writes in the room; the staff works out what
   he means and who takes it. A thumbs-up or thumbs-down on a message is a
   complete answer.
8. **Clarify the task, then question it.** Ask "which ones?" at the start
   when a request is ambiguous. Notice when a task has become pointless
   (a dead project, a stale watch) and say so.
9. **One unprompted item a day, at most, and only when it earns it.** One
   is a ceiling, never a quota. An unprompted item is sent only if it is
   useful or novel to Tom; on a day with nothing that clears that bar, the
   staff sends nothing. An alert counts as a prompt: replies to Tom,
   approved work, and anything said about an item an alert opened don't
   count against the ceiling. Only items the staff starts on its own do.

## The staff

Organized by ownership. Each owner is responsible for their area all day
and has a standing mandate to make it better.

| Member | Owns |
|---|---|
| Chief of staff | The room. Hears Tom first, routes work, decides whether any unprompted item is worth sending that day, runs the weekly retro. |
| House | Home Assistant, tablets, e-ink panels, voice, automations. |
| Fleet | The NixOS hosts, the flake, deploys, backups, disks. |
| Media | Disc ripper, the library on the NAS, Silo, Music Assistant. |
| Maintainer | Tom's own projects: his Home Assistant add-on, its issues and PRs, upstreams it depends on. |
| Scout | New projects and releases, read against what Tom actually runs. |
| Network and radio | UniFi, the relay, Meshtastic, SDRs. |

Each member is its own Matrix account. They hand work to each other in the
room, where Tom can watch. Names are Tom's to choose.

What Tom wants most from initiative, in order: fixes for problems he didn't
know about; releases of things he runs (for example a new Home Assistant
version) already read and compared with his setup; new projects; playful
ideas.

## How work flows

Every piece of work is an item in one shared list that all members read and
update. Each room message belongs to one item, so a reply or reaction is
never ambiguous.

`noticed → investigated → pitched or fix proposed → approved → done → verified → closed`

If Tom doesn't answer: one reminder offering thumbs-up (do it), thumbs-down
(drop it), or a reply to steer. Steering is saved as a standing rule. Still
no answer: the item is shelved and the pattern goes to the weekly retro.

## Channels

- **Matrix room:** all conversation. Element X on the phone, Fractal on the
  laptop.
- **Home Assistant phone push:** urgent only (for example Home Assistant or
  a host down).
- **Meshtastic:** later, as a backup when the network is down.

Existing alerts (`room-say`, the Home Assistant watchdogs) are the inputs.
Plug into signals that exist before building new detection.

## Autonomy

| Without asking | After a yes | Tom only |
|---|---|---|
| Read logs and state anywhere. Research. Draft small fixes. Open branches and PRs. Flake updates that build and pass tests, deployed Suzuka first, Monza last. | Restart or reload a service. Deploy a config change. Edit Home Assistant. Build anything that was pitched. Reboot Monza. | Changing these rules, the sleep gate, or any member's permissions. The thermostat. |

**The sleep gate.** All Home Assistant calls from the staff pass through a
gate enforced in code, outside the agents' judgment. While the sleep or
quiet-hours sensors are on, it refuses anything that makes light, sound, or
motion: lights, speakers, fans, tablet screens, tablet reboots. Waking
someone is the one unforgivable failure. The gate snapshots before any live
edit.

Members can read everything and change nothing. Every deploy, restart, and
reboot goes through one door on Suzuka that holds the only deploy key and
has no model inside. A yes covers one exact change on named hosts; anything
else needs a new yes. Every switch reverts by itself unless the host is
confirmed healthy. Between midnight and 7 a.m. the door changes nothing
unless the item is urgent, so nothing breaks while Tom is asleep. The
Anthropic key sits behind a local relay that enforces the spend caps.
Details are in `ARCHITECTURE.md`.

## Self-improvement

- Lessons are plain files in git: Tom's standing preferences, playbooks for
  solved problems, facts about the setup.
- After a correction, the member replies with the rule it saved so Tom can
  veto it.
- Weekly retro: one short message with what was finished, what was rejected
  or ignored, and one to three proposed changes to how the staff works.
- Memory is pruned on a schedule so it stays small.
- The staff can change its behavior, never its permissions.

## Budget

Under $100 a month of API spend. A hard daily cap stops work when hit, and
the chief of staff says so. Cheaper models do routine work; the strongest
model is kept for diagnosis and judgment.

## How it is built

A thin layer of our own on the Claude Agent SDK, in Python. One small
service, the front desk, listens to the room and to existing alerts, keeps
the item list, and wakes a member only when there is work. A member is a
definition (instructions, tools, model), never an always-running process.
The item list is a SQLite file on Suzuka.

## Where it runs

Suzuka is the staff's home. Everything is declared in the NixOS flake: each
member is a locked-down service with its own user, permissions, and
secrets. Removing it leaves nothing behind.

## Local models

Embeddings (`bge-m3` on Silverstone's Ollama) are required from the first
milestone: they match a new alert against open items so a problem is said
once, and they search the staff's memory. Local models never do diagnosis
and never act on the house.

## Clean start

Before the first milestone, in one pass and only on Tom's go: wipe and
redeclare Synapse, and remove every leftover from earlier attempts listed
in section 12 of `inventory.md`.

## First milestone

Chief of staff plus the fleet owner, on Suzuka, in a fresh room. Fleet goes
first because NixOS rollback and the Suzuka canary make mistakes cheap, and
because real unknown problems are already waiting there.

It passes when, within one week:

1. Tom drops a request in the room from his phone, it is claimed within a
   minute, and it is carried through to done.
2. The staff brings Tom a fix for a problem he didn't know about, he
   approves it with a thumbs-up, and it is deployed and verified.
3. No message was a repeat, a bare problem, or a file path.

The house owner and the sleep gate are the second milestone.

## Open decisions

- Whether the Max plan can cover any unattended use, or all of it is API.
- Member names.

## Not now

Meshtastic and SDR integrations, local models as the main brain, anything
involving the laptop's own ClaudeOS timers.
