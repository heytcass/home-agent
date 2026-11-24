# NixOS module for the staff's own host. See ARCHITECTURE.md for the design.
#
# Used from the flake as `nixosModules.default = import ./nix/module.nix self;`.
#
# It declares four kinds of unit, each under its own system user:
#
#   home-staff-frontdesk            home-staff          item list, Matrix tokens, memory
#   home-staff-member-<name>@       home-staff-<name>   one run of one member, socket-started
#   home-staff-relay                home-staff-relay    the Anthropic key and the spend count
#   home-staff-door                 home-staff-door     deploy SSH key, flake checkout
#
# plus a root oneshot (home-staff-relay-tokens) that hands every caller of
# the relay its own random token under /run.
#
# Everything the units write lives under `stateDir` and /run. Everything
# else (users, groups, units, sockets, /etc/home-staff, the ssh_config
# block) goes away when the module is disabled or removed.
self:
{ config, lib, pkgs, ... }:

let
  inherit (lib) mkOption mkEnableOption mkIf mkAfter types literalExpression;

  cfg = config.services.home-staff;
  toml = pkgs.formats.toml { };

  deskUser = "home-staff";
  relayUser = "home-staff-relay";
  doorUser = "home-staff-door";
  memberUser = name: "home-staff-${name}";

  memberNames = lib.attrNames cfg.members;
  reservedNames = [ "desk" "relay" "door" ];

  # Paths, exactly as the "Paths" list in ARCHITECTURE.md.
  deskDir = "${cfg.stateDir}/desk";
  membersDir = "${cfg.stateDir}/members";
  memberDir = name: "${membersDir}/${name}";
  relayDir = "${cfg.stateDir}/relay";
  doorDir = "${cfg.stateDir}/door";

  runDir = "/run/home-staff";
  memberSocket = name: "${runDir}/members/${name}.sock";
  tokensDir = "${runDir}/relay-tokens"; # one file per caller, readable by that caller only
  relayTokensDir = "${runDir}/relay/tokens"; # the same set, readable by the relay only

  # The door binds its own socket, so it lives in the door's own runtime
  # directory and not in /run/home-staff, which belongs to root. See the
  # comment on the door unit below.
  doorRunDir = "/run/home-staff-door";
  doorSocket = "${doorRunDir}/door.sock";

  relayUrl = "http://127.0.0.1:${toString cfg.relay.port}";

  # systemd's StateDirectory= takes paths relative to /var/lib.
  rel = p: lib.removePrefix "/var/lib/" p;

  # LoadCredential= puts the file here; the name is fixed by the unit name.
  credential = unit: name: "/run/credentials/${unit}.service/${name}";

  # ---- generated configuration -------------------------------------------

  # The same list, in the same order, as ROLL_ORDER in staff/door.py. The
  # door ignores any other name, so the front desk is told only these.
  rollOrder = [ "suzuka" "spa" "silverstone" "monza" ];
  hostNames = lib.filter (h: cfg.door.hosts ? ${h}) rollOrder;

  # Keys must match staff/config.py's Config and Member dataclasses exactly.
  settings = lib.recursiveUpdate {
    homeserver = cfg.homeserver;
    room_id = cfg.roomId;
    tom = cfg.tom;
    alerts_user = cfg.alertsUser;
    db_path = "${deskDir}/items.db";
    memory_dir = "${deskDir}/memory";
    members_dir = "${cfg.package}/share/home-staff/members";
    ollama_url = cfg.ollamaUrl;
    decision_model = cfg.decisionModel;
    daily_cap_usd = cfg.dailyCapUsd;
    monthly_cap_usd = cfg.monthlyCapUsd;
    door_socket = doorSocket;
    hosts = hostNames;
    alert_field = cfg.alertField;
    members = lib.mapAttrs (name: m: {
      user_id = m.userId;
      token_file = m.tokenFile;
      model = m.model;
      workdir = "${memberDir name}/work";
      socket = memberSocket name;
    }) cfg.members;
  } cfg.extraSettings;

  configFile = toml.generate "home-staff-config.toml" settings;

  # Keys exactly as "Wire 2: the relay" in ARCHITECTURE.md.
  relayConfig = pkgs.writeText "home-staff-relay.json" (builtins.toJSON {
    listen_host = "127.0.0.1"; # loopback only; not an option on purpose
    port = cfg.relay.port;
    upstream = cfg.relay.upstream;
    key_file = credential "home-staff-relay" "anthropic-key";
    tokens_dir = relayTokensDir;
    state_path = "${relayDir}/spend.json";
    daily_cap_usd = cfg.dailyCapUsd;
    monthly_cap_usd = cfg.monthlyCapUsd;
  });

  # Keys exactly as "Wire 3: the door" in ARCHITECTURE.md. The roll order is
  # not here: staff/door.py has it built in, and takes from `hosts` only the
  # names it knows.
  doorConfig = pkgs.writeText "home-staff-door.json" (builtins.toJSON {
    socket = doorSocket;
    repo_url = cfg.door.repoUrl;
    checkout = "${doorDir}/flake";
    state_path = "${doorDir}/state.json";
    ssh_key = credential "home-staff-door" "deploy-key";
    hosts = lib.mapAttrs (_: h: h.address) cfg.door.hosts;
    night_start_hour = cfg.door.nightStartHour;
    night_end_hour = cfg.door.nightEndHour;
    revert_after_s = cfg.door.revertAfter;
    build_timeout_s = cfg.door.buildTimeout;
  });

  # Host keys the door and the members check against. Public keys only.
  # door.py runs ssh with BatchMode, which refuses a host it does not know.
  knownHosts = pkgs.writeText "home-staff-known-hosts" (lib.concatStrings (
    lib.mapAttrsToList (_: h: "${h.address} ${h.hostKey}\n") cfg.door.hosts
    ++ map (l: "${l}\n") cfg.door.repoHostKeys
  ));

  # ANTHROPIC_API_KEY is the caller's relay token. The token is made at run
  # time, so it cannot be in the unit; this exports it and starts the program.
  withRelayToken = caller: exe: pkgs.writeShellScript "home-staff-${caller}-start" ''
    set -eu
    ANTHROPIC_API_KEY="$(${pkgs.coreutils}/bin/cat ${tokensDir}/${caller})"
    export ANTHROPIC_API_KEY
    exec ${exe}
  '';

  # Environment of the members, which run the Claude Code CLI. The CLI comes
  # from the Nix store and talks only to the relay.
  claudeEnv = {
    ANTHROPIC_BASE_URL = relayUrl;
    SHELL = "${pkgs.bash}/bin/bash";
    DISABLE_AUTOUPDATER = "1";
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  };

  # ---- hardening -----------------------------------------------------------

  # Applied to every unit of the staff. A unit that cannot hold a directive
  # overrides it next to its definition, with the reason.
  hardening = {
    UMask = "0077";

    # Filesystem. State directories come from StateDirectory= and stay
    # writable; nothing else is.
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectProc = "invisible";

    # Privileges
    NoNewPrivileges = true;
    CapabilityBoundingSet = "";
    AmbientCapabilities = "";
    RestrictSUIDSGID = true;
    RemoveIPC = true;
    KeyringMode = "private";

    # Kernel and system
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    LockPersonality = true;
    RestrictRealtime = true;
    RestrictNamespaces = true;

    # Network: outbound HTTPS and SSH need IPv4/IPv6; AF_NETLINK for
    # interface enumeration (getifaddrs) by Node and ssh; AF_UNIX for
    # local sockets (journal, the wires between the units, the Nix daemon).
    RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];

    # System calls
    SystemCallArchitectures = "native";
    SystemCallFilter = [ "@system-service" "~@privileged" ];
    SystemCallErrorNumber = "EPERM";

    # Deliberately NOT set, see nix/README.md:
    #   MemoryDenyWriteExecute (breaks the V8 JIT in Node),
    #   ProcSubset=pid (hides /proc/cpuinfo and /proc/stat from Node),
    #   PrivateNetwork / IPAddressAllow (needs open outbound HTTPS and SSH;
    #     the two units that need no network at all do set PrivateNetwork),
    #   PrivateUsers (would hide real uids from ssh and git).
  };

  # Second layer under the file modes: what a unit has no business seeing is
  # not in its mount namespace at all. InaccessiblePaths rather than an empty
  # tmpfs plus BindPaths, because the list is short and known, and because a
  # mistake here leaves the 0700 modes in force instead of leaving a unit
  # without its own state directory. `-` skips a path that does not exist yet.
  hide = paths: map (p: "-${p}") paths;

  othersOfMember = name:
    [ deskDir relayDir doorDir "${runDir}/relay" "${runDir}/members" doorRunDir ]
    ++ map memberDir (lib.remove name memberNames);

  # ---- option types --------------------------------------------------------

  memberType = types.submodule {
    options = {
      userId = mkOption {
        type = types.str;
        example = "@fleet:example.org";
        description = "Full Matrix user id of this member.";
      };
      tokenFile = mkOption {
        type = types.str;
        example = "/run/secrets/home-staff/fleet-token";
        description = ''
          Path to a file holding this member's Matrix access token, readable
          by the `home-staff` user, the front desk (for example a sops-nix
          secret with `owner = "home-staff"`). The member's own worker never
          reads it. A string, so the secret is never copied into the Nix store.
        '';
      };
      model = mkOption {
        type = types.str;
        example = "claude-opus-5-5";
        description = "Claude model this member runs on.";
      };
      sshKeyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "/run/secrets/home-staff/fleet-read-key";
        description = ''
          Private SSH key this member uses to look at other hosts, readable
          by the member's user `home-staff-<name>` and by nobody else. Its
          public half is `services.home-staff-host.readKey` on the hosts.
          When set, every ssh this user starts logs in as `staff-read` with
          this key (a `Match localuser` block in /etc/ssh/ssh_config).
        '';
      };
      extraGroups = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "systemd-journal" ];
        description = "Extra groups for this member's user, for read access on the staff's own host.";
      };
    };
  };

  hourType = types.ints.between 0 23;
in
{
  options.services.home-staff = {
    enable = mkEnableOption "the home staff (front desk, member workers, key relay, deploy door)";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = literalExpression "home-staff.packages.\${system}.default";
      description = ''
        The home-staff package (bin/frontdesk, bin/staff-worker,
        bin/staff-relay, bin/staff-door, share/home-staff/members).
      '';
    };

    homeserver = mkOption {
      type = types.str;
      example = "https://matrix.example.org";
      description = "Matrix homeserver base URL.";
    };

    roomId = mkOption {
      type = types.str;
      example = "!abcdef:example.org";
      description = "The one Matrix room the staff lives in.";
    };

    tom = mkOption {
      type = types.str;
      example = "@owner:example.org";
      description = "Matrix user id of the owner, the only person the staff takes orders from.";
    };

    alertsUser = mkOption {
      type = types.str;
      example = "@alerts:example.org";
      description = "Matrix user id that existing fleet alerts are posted as.";
    };

    ollamaUrl = mkOption {
      type = types.str;
      default = "";
      example = "http://ollama.example.org:11434";
      description = "Ollama base URL for embeddings (and the optional local router). Empty disables it.";
    };

    decisionModel = mkOption {
      type = types.str;
      default = "";
      description = "Local Ollama model for the router. Empty disables the local router.";
    };

    dailyCapUsd = mkOption {
      type = types.float;
      default = 3.0;
      description = ''
        Daily API spend cap in US dollars. The relay enforces it (the hard
        stop); the front desk uses the same number to not start runs.
      '';
    };

    monthlyCapUsd = mkOption {
      type = types.float;
      default = 95.0;
      description = "Monthly API spend cap in US dollars, enforced like the daily cap.";
    };

    anthropicKeyFile = mkOption {
      type = types.str;
      example = "/run/secrets/home-staff/anthropic-key";
      description = ''
        Path to a file holding the Anthropic API key: either the bare key or
        one line `ANTHROPIC_API_KEY=...`. Only the relay gets it, through
        LoadCredential, so the file can stay owned by root with mode 0400.
        A string, so the secret is never copied into the Nix store.
      '';
    };

    members = mkOption {
      type = types.attrsOf memberType;
      default = { };
      description = ''
        Staff members by name (chief, fleet, ...). Each gets a Matrix
        account, a model, its own system user `home-staff-<name>`, and its
        own socket-started worker. `desk`, `relay`, and `door` are taken.
      '';
    };

    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/home-staff";
      description = ''
        Root of all persistent state. Owned by root and only traversable;
        below it `desk/`, `members/<name>/`, `relay/`, and `door/` each
        belong to one user. Must be under /var/lib.
      '';
    };

    alertField = mkOption {
      type = types.str;
      default = "alert";
      description = "The key in an alert event's content that holds the alert's labels.";
    };

    extraSettings = mkOption {
      type = toml.type;
      default = { };
      example = literalExpression ''{ router_model = "claude-haiku-5-5"; max_turns = 40; }'';
      description = ''
        Extra keys merged into the front desk's config.toml, for fields of
        staff/config.py that have no option here. Never put secrets in it.
      '';
    };

    relay = {
      port = mkOption {
        type = types.port;
        default = 8377;
        description = "Loopback TCP port of the key relay. It listens on 127.0.0.1 only.";
      };
      upstream = mkOption {
        type = types.str;
        default = "https://api.anthropic.com";
        description = "Where the relay forwards to.";
      };
    };

    door = {
      repoUrl = mkOption {
        type = types.str;
        example = "git@git.example.org:owner/homelab.git";
        description = "Git URL of the homelab flake. The door clones it, pushes `staff/*` branches, and fast-forwards `main`.";
      };
      hosts = mkOption {
        type = types.attrsOf (types.submodule {
          options = {
            address = mkOption {
              type = types.str;
              example = "192.0.2.10";
              description = "Host name or IP address the door reaches this host at over SSH (port 22).";
            };
            hostKey = mkOption {
              type = types.str;
              example = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleHostKeyOnly";
              description = "Public SSH host key of this host (the content of its /etc/ssh/ssh_host_ed25519_key.pub, without the comment).";
            };
          };
        });
        example = literalExpression ''
          {
            alpha = { address = "192.0.2.10"; hostKey = "ssh-ed25519 AAAA..."; };
            beta = { address = "beta.example.org"; hostKey = "ssh-ed25519 AAAA..."; };
          }
        '';
        description = ''
          The hosts the door may deploy to. Nothing outside this set is
          reachable through the door, and the door refuses a host whose key
          is not the one given here. The roll order is built into
          staff/door.py (`ROLL_ORDER`); a name it does not know is ignored.
        '';
      };
      repoHostKeys = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "git.example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleGitServerKey" ];
        description = ''
          known_hosts lines (host name, key type, key) for the git server in
          `repoUrl`. Needed when `repoUrl` is an SSH URL: the door does not
          accept a host key it was not given.
        '';
      };
      deployKeyFile = mkOption {
        type = types.str;
        example = "/run/secrets/home-staff/deploy-key";
        description = ''
          Private SSH key of the door: accepted by `staff-deploy` on every
          host (`services.home-staff-host.deployKey` is its public half) and
          by the git server for `repoUrl`. Only the door gets it, through
          LoadCredential, so the file can stay owned by root with mode 0400.
        '';
      };
      nightStartHour = mkOption {
        type = hourType;
        default = 0;
        description = "First hour (local time) of the night window in which the door refuses changes that are not urgent.";
      };
      nightEndHour = mkOption {
        type = hourType;
        default = 7;
        description = "Hour (local time) at which the night window ends.";
      };
      revertAfter = mkOption {
        type = types.ints.positive;
        default = 300;
        description = "Seconds the door waits for a host to come back healthy after a switch before it counts on the host's own revert. Must equal `services.home-staff-host.revertAfter` on every host.";
      };
      buildTimeout = mkOption {
        type = types.ints.positive;
        default = 3600;
        description = "Seconds the door gives a build on a host. Must equal `services.home-staff-host.buildTimeout` on every host.";
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/var/lib/" cfg.stateDir && !(lib.hasSuffix "/" cfg.stateDir);
        message = "services.home-staff.stateDir must be a path under /var/lib/ without a trailing slash.";
      }
      {
        assertion = cfg.members != { };
        message = "services.home-staff.members must define at least one member.";
      }
      {
        assertion = lib.all (n: !(lib.elem n reservedNames)) memberNames;
        message = "services.home-staff.members: the names `desk`, `relay`, and `door` are taken by the staff's own users and tokens.";
      }
      {
        # "home-staff-" plus the name must be a valid user name (31 characters).
        assertion = lib.all (n: builtins.match "[a-z][a-z0-9-]{0,19}" n != null) memberNames;
        message = "services.home-staff.members: names must match [a-z][a-z0-9-]{0,19} (they become user, unit, and file names).";
      }
      {
        assertion = cfg.door.hosts != { };
        message = "services.home-staff.door.hosts must name at least one host.";
      }
      {
        assertion = hostNames != [ ];
        message = "services.home-staff.door.hosts: none of the names is in the door's roll order (${lib.concatStringsSep ", " rollOrder}); the door would serve no host.";
      }
      {
        assertion = lib.all (l: builtins.match "[^\n]+" l != null)
          (cfg.door.repoHostKeys ++ lib.concatMap (h: [ h.address h.hostKey ]) (lib.attrValues cfg.door.hosts));
        message = "services.home-staff.door: every address, hostKey, and repoHostKeys entry must be one non-empty line.";
      }
      {
        assertion = cfg.door.nightStartHour != cfg.door.nightEndHour;
        message = "services.home-staff.door: nightStartHour and nightEndHour must differ.";
      }
    ];

    # ---- users -------------------------------------------------------------

    users.users = {
      ${deskUser} = {
        isSystemUser = true;
        group = deskUser;
        home = deskDir;
        createHome = false; # StateDirectory= creates it with the right owner
        description = "Home staff front desk";
      };
      ${relayUser} = {
        isSystemUser = true;
        group = relayUser;
        home = relayDir;
        createHome = false;
        description = "Home staff key relay";
      };
      ${doorUser} = {
        isSystemUser = true;
        group = doorUser;
        home = doorDir;
        createHome = false;
        description = "Home staff deploy door";
      };
    } // lib.mapAttrs' (name: m: lib.nameValuePair (memberUser name) {
      isSystemUser = true;
      group = memberUser name;
      extraGroups = m.extraGroups;
      home = memberDir name;
      createHome = false;
      description = "Home staff member ${name}";
    }) cfg.members;

    users.groups = lib.genAttrs
      ([ deskUser relayUser doorUser ] ++ map memberUser memberNames)
      (_: { });

    # ---- directories -------------------------------------------------------

    # The parents belong to root and can be passed through but not listed
    # (0711), so each user reaches its own folder and learns nothing about
    # its siblings. The folders themselves are 0700 and come from
    # StateDirectory= on the unit that owns them.
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0711 root root -"
      "d ${membersDir} 0711 root root -"
      "d ${runDir} 0711 root root -"
      # Only the front desk may even reach the member sockets.
      "d ${runDir}/members 0710 root ${deskUser} -"
    ];

    environment.etc."home-staff/config.toml".source = configFile;

    # A member with a read key logs in everywhere as `staff-read` with that
    # key and nothing else. It and the door accept only the host keys
    # declared in `door.hosts` and `door.repoHostKeys`. mkAfter and the closing `Match all` keep the
    # block from swallowing options other modules add to ssh_config.
    programs.ssh.extraConfig =
      let withKey = lib.filterAttrs (_: m: m.sshKeyFile != null) cfg.members; in
      mkAfter (lib.concatStrings (lib.mapAttrsToList (name: m: ''
        Match localuser ${memberUser name}
          User staff-read
          IdentityFile ${m.sshKeyFile}
          IdentitiesOnly yes
          BatchMode yes
          StrictHostKeyChecking yes
          UserKnownHostsFile ${knownHosts}
      '') withKey) + ''
        Match localuser ${doorUser}
          StrictHostKeyChecking yes
          UserKnownHostsFile ${knownHosts}
        Match all
      '');

    # ---- sockets -----------------------------------------------------------

    systemd.sockets = lib.mapAttrs' (name: _: lib.nameValuePair "home-staff-member-${name}" {
      description = "Home staff member ${name} (socket for the front desk)";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        ListenStream = memberSocket name;
        Accept = true; # one worker process per connection, that is, per run
        SocketUser = deskUser;
        SocketGroup = deskUser;
        SocketMode = "0600";
        DirectoryMode = "0711";
      };
    }) cfg.members;

    # ---- services ----------------------------------------------------------

    systemd.services = {

      # Root, once per boot: one random token per caller of the relay. A
      # caller can read only its own; the relay gets a private copy of all.
      # Tokens live on /run (tmpfs), never in the Nix store. Existing tokens
      # are kept on a rerun, so a rebuild that adds a member does not cut off
      # the running ones.
      home-staff-relay-tokens = {
        description = "Home staff: relay tokens for the front desk and each member";
        wantedBy = [ "multi-user.target" ];
        before = [ "home-staff-relay.service" "home-staff-frontdesk.service" ];
        path = [ pkgs.coreutils ];
        script = ''
          set -eu
          umask 077
          install -d -m 0711 -o root -g root ${runDir} ${tokensDir}
          install -d -m 0700 -o ${relayUser} -g ${relayUser} ${runDir}/relay ${relayTokensDir}

          issue() {
            caller=$1 owner=$2
            if [ ! -s "${tokensDir}/$caller" ]; then
              new=$(mktemp ${tokensDir}/.new.XXXXXX)
              printf 'staff-%s-%s' "$caller" "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" > "$new"
              mv -f "$new" "${tokensDir}/$caller"
            fi
            chown "$owner:$owner" "${tokensDir}/$caller"
            chmod 0400 "${tokensDir}/$caller"
            install -m 0400 -o ${relayUser} -g ${relayUser} "${tokensDir}/$caller" "${relayTokensDir}/$caller"
          }

          issue desk ${deskUser}
          ${lib.concatMapStringsSep "\n" (n: "issue ${n} ${memberUser n}") memberNames}

          # Tokens of callers that are no longer declared.
          for f in ${tokensDir}/* ${relayTokensDir}/*; do
            [ -e "$f" ] || continue
            case "$(basename "$f")" in
              ${lib.concatStringsSep "|" ([ "desk" ] ++ memberNames)}) ;;
              *) rm -f -- "$f" ;;
            esac
          done
        '';
        serviceConfig = hardening // {
          Type = "oneshot";
          RemainAfterExit = true;
          ReadWritePaths = [ "/run" ];
          PrivateNetwork = true;
          RestrictAddressFamilies = [ "AF_UNIX" ];
          # Loosened: this unit is root and gives files to other users.
          CapabilityBoundingSet = [ "CAP_CHOWN" "CAP_FOWNER" "CAP_DAC_OVERRIDE" ];
          SystemCallFilter = [ "@system-service" ]; # `~@privileged` would forbid chown
        };
      };

      # The only holder of the Anthropic key. No model, no tools, no state
      # but its own spend count.
      home-staff-relay = {
        description = "Home staff key relay";
        wantedBy = [ "multi-user.target" ];
        requires = [ "home-staff-relay-tokens.service" ];
        after = [ "home-staff-relay-tokens.service" "network-online.target" ];
        wants = [ "network-online.target" ];
        environment = {
          RELAY_CONFIG = relayConfig;
          HOME = relayDir;
        };
        serviceConfig = hardening // {
          ExecStart = "${cfg.package}/bin/staff-relay";
          User = relayUser;
          Group = relayUser;
          Restart = "on-failure";
          RestartSec = "5s";
          # Read by root before the user is dropped, so the key file needs no
          # owner but root.
          LoadCredential = [ "anthropic-key:${cfg.anthropicKeyFile}" ];
          StateDirectory = [ (rel relayDir) ];
          StateDirectoryMode = "0700";
          WorkingDirectory = relayDir;
          InaccessiblePaths = hide (
            [ deskDir doorDir "${runDir}/members" doorRunDir ] ++ map memberDir memberNames
          );
        };
      };

      # NOT socket-activated: staff.door binds the path in its config itself
      # and sets it to 0660. The socket sits in the door's runtime directory
      # (owner and group home-staff-door, 0750). The front desk is the only
      # other process in that group (SupplementaryGroups on its unit, not a
      # membership in /etc/group), so it is the only other one that can
      # connect. The group opens nothing else: the door's state is 0700.
      home-staff-door = {
        description = "Home staff deploy door";
        wantedBy = [ "multi-user.target" ];
        after = [ "network-online.target" ];
        wants = [ "network-online.target" ];
        path = [ pkgs.git config.nix.package pkgs.openssh pkgs.gnutar pkgs.gzip pkgs.coreutils pkgs.bash ];
        environment = {
          DOOR_CONFIG = doorConfig;
          HOME = doorDir;
          # `nix flake update` runs here, through the Nix daemon.
          NIX_CONFIG = "experimental-features = nix-command flakes";
          # git reaches `repoUrl` with the door's key and no other.
          GIT_SSH_COMMAND = "ssh -i ${credential "home-staff-door" "deploy-key"} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${knownHosts}";
          GIT_AUTHOR_NAME = "home-staff door";
          GIT_AUTHOR_EMAIL = "home-staff-door@localhost";
          GIT_COMMITTER_NAME = "home-staff door";
          GIT_COMMITTER_EMAIL = "home-staff-door@localhost";
        };
        serviceConfig = hardening // {
          ExecStart = "${cfg.package}/bin/staff-door";
          # Started means listening: wait for the socket file (the first
          # start may clone the flake before it binds, so give up quietly).
          ExecStartPost = pkgs.writeShellScript "home-staff-door-wait" ''
            for _ in $(${pkgs.coreutils}/bin/seq 1 150); do
              [ -S ${doorSocket} ] && exit 0
              ${pkgs.coreutils}/bin/sleep 0.2
            done
            echo "home-staff-door: ${doorSocket} not there after 30 s, not waiting longer" >&2
          '';
          User = doorUser;
          Group = doorUser;
          Restart = "on-failure";
          RestartSec = "10s";
          LoadCredential = [ "deploy-key:${cfg.door.deployKeyFile}" ];
          StateDirectory = [ (rel doorDir) ];
          StateDirectoryMode = "0700";
          RuntimeDirectory = [ "home-staff-door" ];
          RuntimeDirectoryMode = "0750";
          WorkingDirectory = doorDir;
          InaccessiblePaths = hide (
            [ deskDir relayDir "${runDir}/relay" "${runDir}/members" ] ++ map memberDir memberNames
          );
        };
      };

      home-staff-frontdesk = {
        description = "Home staff front desk";
        wantedBy = [ "multi-user.target" ];
        requires = [ "home-staff-relay-tokens.service" ];
        wants = [ "network-online.target" "home-staff-relay.service" "home-staff-door.service" ]
          ++ map (n: "home-staff-member-${n}.socket") memberNames;
        after = [ "network-online.target" "home-staff-relay-tokens.service" "home-staff-relay.service" "home-staff-door.service" ]
          ++ map (n: "home-staff-member-${n}.socket") memberNames;
        restartTriggers = [ configFile ];

        # The front desk does not run the Claude CLI. Its one model call is
        # plain HTTPS to the relay. git is for the memory folder, a local
        # repository it creates itself and never pushes.
        path = [ pkgs.git pkgs.coreutils ];

        environment = {
          STAFF_CONFIG = "/etc/home-staff/config.toml";
          HOME = deskDir;
          ANTHROPIC_BASE_URL = relayUrl;
        };

        serviceConfig = hardening // {
          # No EnvironmentFile: the real key never enters this unit. Its
          # ANTHROPIC_API_KEY is the `desk` relay token.
          ExecStart = withRelayToken "desk" "${cfg.package}/bin/frontdesk";
          User = deskUser;
          Group = deskUser;
          # The one thing this group opens is the door's socket.
          SupplementaryGroups = [ doorUser ];
          Restart = "on-failure";
          RestartSec = "10s";
          StateDirectory = map rel [ deskDir "${deskDir}/memory" ];
          StateDirectoryMode = "0700";
          WorkingDirectory = deskDir;
          InaccessiblePaths = hide (
            [ relayDir doorDir "${runDir}/relay" ] ++ map memberDir memberNames
          );
        };
      };

    } // lib.mapAttrs' (name: _: lib.nameValuePair "home-staff-member-${name}@" {
      # One instance per connection on the member's socket; the connection is
      # the worker's stdin and stdout (Wire 1). stderr goes to the journal.
      description = "Home staff member ${name}, one run (%i)";
      requires = [ "home-staff-relay-tokens.service" ];
      wants = [ "home-staff-relay.service" ];
      after = [ "home-staff-relay-tokens.service" "home-staff-relay.service" ];

      # The package wrapper adds claude-code, git and openssh. The rest is
      # what the Bash tool needs to look at things; nothing here can change
      # anything the member's user could not change anyway.
      path = with pkgs; [ bash coreutils git openssh gnugrep gnused findutils curl jq ];

      environment = claudeEnv // {
        STAFF_MEMBER = name;
        HOME = memberDir name;
        CLAUDE_CONFIG_DIR = "${memberDir name}/claude";
      };

      serviceConfig = hardening // {
        ExecStart = withRelayToken name "${cfg.package}/bin/staff-worker";
        StandardInput = "socket";
        StandardOutput = "socket";
        StandardError = "journal";
        User = memberUser name;
        Group = memberUser name;
        StateDirectory = map rel [ (memberDir name) "${memberDir name}/work" "${memberDir name}/claude" ];
        StateDirectoryMode = "0700";
        WorkingDirectory = "${memberDir name}/work";
        # A finished or failed run leaves no unit behind.
        CollectMode = "inactive-or-failed";
        InaccessiblePaths = hide (othersOfMember name);
      };
    }) cfg.members;
  };
}
