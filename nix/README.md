# Running the home staff on NixOS

This flake has two NixOS modules:

- `nixosModules.default` (`services.home-staff`) for the one host the staff
  runs on: the front desk, one socket-started worker per member, the key
  relay, and the deploy door.
- `nixosModules.host` (`services.home-staff-host`) for every host the staff
  may look at and deploy to, the staff's own host included: a read-only
  login and a deploy entrance.

The design is in [ARCHITECTURE.md](../ARCHITECTURE.md). All names and
addresses below are placeholders.

**Nothing here has run on a live system.** The modules evaluate, and the two
deploy scripts build and pass shellcheck. What that does not prove is listed
under [Not tested on a live system](#not-tested-on-a-live-system).

## 1. Add the flake input

```nix
# flake.nix of your homelab
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    home-staff = {
      url = "github:OWNER/home-staff";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-staff, ... }: {
    nixosConfigurations.alpha = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./hosts/alpha
        home-staff.nixosModules.default # the staff's own host only
        home-staff.nixosModules.host    # every host the staff may touch
      ];
    };
    nixosConfigurations.beta = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [ ./hosts/beta home-staff.nixosModules.host ];
    };
  };
}
```

Importing a module declares nothing until its `enable` is set.

## 2. The staff's own host

```nix
{
  services.home-staff = {
    enable = true;
    homeserver = "https://matrix.example.org";
    roomId = "!abcdefghijkl:example.org";
    tom = "@owner:example.org";
    alertsUser = "@alerts:example.org";
    anthropicKeyFile = "/run/secrets/home-staff/anthropic-key";

    members.chief = {
      userId = "@chief:example.org";
      model = "claude-haiku-5-5";
      tokenFile = "/run/secrets/home-staff/chief-token";
    };
    members.fleet = {
      userId = "@fleet:example.org";
      model = "claude-opus-5-5";
      tokenFile = "/run/secrets/home-staff/fleet-token";
      sshKeyFile = "/run/secrets/home-staff/fleet-read-key";
      extraGroups = [ "systemd-journal" ]; # read the staff host's own journal
    };

    door = {
      repoUrl = "git@git.example.org:owner/homelab.git";
      deployKeyFile = "/run/secrets/home-staff/deploy-key";
      hosts = {
        alpha = { address = "192.0.2.10"; hostKey = "ssh-ed25519 AAAA..."; };
        beta = { address = "beta.example.org"; hostKey = "ssh-ed25519 AAAA..."; };
      };
      repoHostKeys = [ "git.example.org ssh-ed25519 AAAA..." ];
    };
  };
}
```

Other options, with their defaults: `ollamaUrl` (`""`), `decisionModel`
(`""`), `dailyCapUsd` (`3.0`), `monthlyCapUsd` (`95.0`), `alertField`
(`"alert"`), `stateDir` (`/var/lib/home-staff`), `extraSettings` (`{ }`,
merged into the front desk's `config.toml`), `relay.port` (`8377`),
`relay.upstream` (`https://api.anthropic.com`), `door.nightStartHour` (`0`),
`door.nightEndHour` (`7`), `door.revertAfter` (`300`), `door.buildTimeout`
(`3600`), `package` (this flake's own).

Things to know:

- The names in `door.hosts` must be names in `ROLL_ORDER` in
  `staff/door.py`. The door ignores any other name.
- `door.hosts.<name>.address` is used on port 22 and must be written the same
  way everywhere, because the host key is tied to that exact spelling.
- `door.revertAfter` and `door.buildTimeout` must equal
  `services.home-staff-host.revertAfter` and `buildTimeout` on every host.
  The defaults agree (300 and 3600).
- The member names `desk`, `relay`, and `door` are taken.
- If your nixpkgs does not allow unfree packages, nothing needs to change:
  the package (and the Claude Code CLI inside it) is built with this flake's
  own nixpkgs settings.

### What it declares

| Unit | User | Holds |
| --- | --- | --- |
| `home-staff-frontdesk.service` | `home-staff` | item list, Matrix tokens, memory |
| `home-staff-member-<name>.socket` and `@.service` | `home-staff-<name>` | one run of one member |
| `home-staff-relay.service` | `home-staff-relay` | the Anthropic key, the spend count |
| `home-staff-door.service` | `home-staff-door` | the deploy key, the flake checkout |
| `home-staff-relay-tokens.service` | root, oneshot | makes the relay tokens under `/run` |

Also: `/etc/home-staff/config.toml`, a `Match localuser` block in
`/etc/ssh/ssh_config` for the door and for each member with `sshKeyFile`,
and these directories:

| Path | Owner, mode |
| --- | --- |
| `/var/lib/home-staff`, `/var/lib/home-staff/members` | root, 0711 |
| `/var/lib/home-staff/desk` (with `items.db`, `memory/`) | `home-staff`, 0700 |
| `/var/lib/home-staff/members/<name>` (with `work/`, `claude/`) | `home-staff-<name>`, 0700 |
| `/var/lib/home-staff/relay` | `home-staff-relay`, 0700 |
| `/var/lib/home-staff/door` | `home-staff-door`, 0700 |
| `/run/home-staff/members/<name>.sock` | `home-staff`, 0600, in a directory only `home-staff` can enter |
| `/run/home-staff/relay-tokens/<name>` | that caller, 0400 |
| `/run/home-staff/relay/tokens` | `home-staff-relay`, 0700 |
| `/run/home-staff-door/door.sock` | `home-staff-door`, 0660, directory 0750 |

### The door's socket

The door is **not** socket-activated. `staff-door` binds its own socket, so
the socket cannot be in `/run/home-staff` (root's) and is at
`/run/home-staff-door/door.sock` instead of the `/run/home-staff/door.sock`
that ARCHITECTURE.md names. Only the door's user and group can reach it. The
front desk's unit has `SupplementaryGroups=home-staff-door`, which is the
only way into that group; the group opens nothing but this socket.

### The relay tokens

`home-staff-relay-tokens` runs as root once per boot and writes one random
token per caller (`desk` and each member) on `/run`, a tmpfs. They are never
in the Nix store. Each caller's start script reads its own token into
`ANTHROPIC_API_KEY`; the real key is only in the relay. A rebuild keeps the
tokens that exist, adds new members, and removes the ones no longer declared.

### Hardening

Every unit has the same set: `ProtectSystem=strict`, `ProtectHome`,
`PrivateTmp`, `PrivateDevices`, `ProtectProc=invisible`, `NoNewPrivileges`,
an empty capability set, `RestrictSUIDSGID`, the `ProtectKernel*` group,
`ProtectControlGroups`, `ProtectClock`, `ProtectHostname`,
`LockPersonality`, `RestrictRealtime`, `RestrictNamespaces`,
`RestrictAddressFamilies` (Unix, IPv4, IPv6, netlink),
`SystemCallFilter=@system-service ~@privileged`, `UMask=0077`. On top of the
file modes, each unit has `InaccessiblePaths` for the state of the others.

Left off on purpose:

| Directive | Why |
| --- | --- |
| `MemoryDenyWriteExecute` | breaks the JIT of Node, which the Claude CLI runs on |
| `ProcSubset=pid` | hides `/proc/cpuinfo` and `/proc/stat` from Node |
| `PrivateNetwork`, `IPAddressAllow` | the units need open outbound HTTPS and SSH (the token oneshot needs no network and does set `PrivateNetwork`) |
| `PrivateUsers` | hides real user ids from ssh and git |

Loosened in one unit: `home-staff-relay-tokens` runs as root with
`CAP_CHOWN`, `CAP_FOWNER`, `CAP_DAC_OVERRIDE` and without `~@privileged`,
because its whole job is to give files to other users.

## 3. Every host the staff may touch

```nix
{
  services.openssh.enable = true;

  services.home-staff-host = {
    enable = true;
    deployKey = "ssh-ed25519 AAAA... staff-door";  # public half of door.deployKeyFile
    readKey = "ssh-ed25519 AAAA... staff-fleet";   # public half of members.fleet.sshKeyFile
  };
}
```

Other options, with their defaults: `nightStartHour` (`0`), `nightEndHour`
(`7`), `revertAfter` (`300`), `buildTimeout` (`3600`).

It declares:

- User `staff-read`: in `systemd-journal`, no sudo, `readKey` with
  `restrict`.
- User `staff-deploy`: `deployKey` with `restrict` and the forced command
  `staff-deploy-host`.
- `/var/lib/staff-deploy` (root, 0755) and `/var/lib/staff-deploy/src`
  (`staff-deploy`, 0700).
- `/etc/staff-deploy.conf` with `REVERT_AFTER_S`, `NIGHT_START_HOUR`,
  `NIGHT_END_HOUR`, `BUILD_TIMEOUT_S`.
- One sudo rule, for `security.sudo` or `security.sudo-rs`, whichever is on:
  `staff-deploy` may run the store path of `staff-deploy-root` as root
  without a password, with any arguments (the script checks them), and
  nothing else. `staff-deploy-host` is built to call that same store path.

If your sshd has `AllowUsers` or `AllowGroups`, add `staff-read` and
`staff-deploy` there yourself.

## 4. Secrets you must create

None of these are made for you, and none may be in the Nix store. With
sops-nix or agenix, set the owner as shown.

| Secret | Option | Owner |
| --- | --- | --- |
| Anthropic API key (bare, or one line `ANTHROPIC_API_KEY=...`) | `anthropicKeyFile` | root, 0400 |
| One Matrix access token per member | `members.<name>.tokenFile` | `home-staff`, 0400 |
| The door's private SSH key | `door.deployKeyFile` | root, 0400 |
| A member's private read key (optional) | `members.<name>.sshKeyFile` | `home-staff-<name>`, 0400 |

And by hand:

1. Make the two SSH key pairs (`ssh-keygen -t ed25519`, no passphrase). The
   public halves go into `deployKey` and `readKey` on every host.
2. Give the door's public key write access to the homelab repository
   (a deploy key on the git server).
3. Collect each host's public host key for `door.hosts.<name>.hostKey`, and
   the git server's for `door.repoHostKeys`.
4. Create the Matrix accounts, invite them to the room, and get their access
   tokens.

## 5. Coming from the older single-unit module

- `memoryDir` is gone; memory is always `<stateDir>/desk/memory`, a local
  git repository the front desk creates.
- The item list moved to `<stateDir>/desk/items.db`. Move an old
  `items.db` there by hand if you want to keep it.
- The front desk no longer takes the real API key; give it to the relay with
  `anthropicKeyFile`.

## Teardown

Remove the `services.home-staff*` settings, the two module imports, and the
flake input, then rebuild each host. That removes every user and group,
every unit and socket, the sudo rule, `/etc/home-staff`,
`/etc/staff-deploy.conf`, the authorized keys, and the ssh_config block.

What stays, for you to remove by hand:

- On the staff's host: `/var/lib/home-staff`.
- On every host: `/var/lib/staff-deploy` and `/var/log/fleet-deploy/staff-*`.
- The Matrix accounts of the members, and the room.
- The `staff/*` branches in the homelab repository, and the door's deploy
  key on the git server.

`/run/home-staff` on the staff's host is on a tmpfs and is gone after the
next reboot. Files left in `/var/lib` keep the numeric ids of users that no
longer exist.

## Not tested on a live system

Checked: `nix flake check --no-build`; a throwaway NixOS configuration with
both modules evaluates to a system derivation with `sudo` and with
`sudo-rs`; the assertions fire; both deploy scripts build and pass
shellcheck. Never done: a deploy, a boot, or a start of any unit.

Only a live host can show:

1. That each unit starts under this hardening, and that
   `StateDirectory=` makes the nested folders correctly below the 0711
   parents owned by root.
2. That the front desk reaches the door's socket through
   `SupplementaryGroups`, and that nobody else does.
3. That a member's worker gets its connection on stdin and stdout, and that
   a run leaves no unit behind.
4. That the Claude CLI accepts a relay token as `ANTHROPIC_API_KEY` and a
   plain-HTTP loopback `ANTHROPIC_BASE_URL`.
5. That ssh and git accept the door's key from the credentials directory.
6. That the `Match localuser` block and the generated known_hosts file work
   for the door and the members, with the addresses as you wrote them.
7. That the door's user may talk to the Nix daemon (`nix.settings.allowed-users`)
   and that `nix flake update` works inside the unit.
8. That sshd runs the forced command under `restrict`, and that sudo (or
   sudo-rs) accepts the one rule without a terminal.
9. A real switch, confirm, and timed revert through `staff-deploy-root`.
10. That relay tokens survive a rebuild and that a removed member's token
    stops working.
11. That after a teardown nothing but the paths listed above is left.
12. aarch64-linux: never evaluated.
