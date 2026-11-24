# NixOS module for every host the staff may look at and deploy to (the
# staff's own host included). See "On each host" in ARCHITECTURE.md.
#
# Used from the flake as `nixosModules.host = import ./nix/host.nix self;`.
#
# It declares two users, two authorized keys, two directories, one file in
# /etc, and exactly one sudo rule. All of it goes away when the module is
# disabled or removed; what stays on disk is listed in nix/README.md.
self:
{ config, lib, pkgs, ... }:

let
  inherit (lib) mkOption mkEnableOption mkIf types;

  cfg = config.services.home-staff-host;

  # Built with this host's own nix, nixos-rebuild, and systemd, so a switch
  # started by the staff uses the same tools as one started by hand.
  packages = self.lib.hostScripts {
    inherit pkgs;
    nix = config.nix.package;
    nixos-rebuild = config.system.build.nixos-rebuild or pkgs.nixos-rebuild;
    systemd = config.systemd.package;
  };

  # The forced command of the deploy key. It is built against the root
  # script below and calls it by that store path, so the path in the sudo
  # rule and the path it calls are one.
  hostCommand = "${packages.staff-deploy-host}/bin/staff-deploy-host";
  rootCommand = "${packages.staff-deploy-root}/bin/staff-deploy-root";

  # The same rule for sudo and sudo-rs; both take `extraRules` in this form.
  sudoRule = {
    users = [ "staff-deploy" ];
    runAs = "root";
    commands = [{ command = rootCommand; options = [ "NOPASSWD" ]; }];
  };

  hourType = types.ints.between 0 23;
in
{
  options.services.home-staff-host = {
    enable = mkEnableOption "the home staff's read login and deploy entrance on this host";

    deployKey = mkOption {
      type = types.str;
      example = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleDeployKeyOnly staff-door";
      description = ''
        Public SSH key of the staff's deploy door. It can run one thing on
        this host: the forced command `staff-deploy-host`.
      '';
    };

    readKey = mkOption {
      type = types.str;
      example = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleReadKeyOnly staff-fleet";
      description = ''
        Public SSH key of the member that looks at this host. It logs in as
        `staff-read`: a user in `systemd-journal`, with no sudo.
      '';
    };

    nightStartHour = mkOption {
      type = hourType;
      default = 0;
      description = "First hour (local time) of this host's night window.";
    };

    nightEndHour = mkOption {
      type = hourType;
      default = 7;
      description = "Hour (local time) at which this host's night window ends.";
    };

    revertAfter = mkOption {
      type = types.ints.positive;
      default = 300;
      description = "Seconds after a switch at which this host goes back to its previous system unless the switch was confirmed. Must equal `services.home-staff.door.revertAfter` on the staff host.";
    };

    buildTimeout = mkOption {
      type = types.ints.positive;
      default = 3600;
      description = "Seconds a build started by the staff may take on this host before it is stopped. Must equal `services.home-staff.door.buildTimeout` on the staff host.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = config.services.openssh.enable;
        message = "services.home-staff-host needs services.openssh.enable: both of its users are reached over SSH.";
      }
      {
        assertion = config.security.sudo.enable || config.security.sudo-rs.enable;
        message = "services.home-staff-host needs security.sudo or security.sudo-rs: staff-deploy reaches root through one sudo rule.";
      }
      {
        assertion = lib.all (k: builtins.match "[^\n\"]+" k != null) [ cfg.deployKey cfg.readKey ];
        message = "services.home-staff-host: deployKey and readKey must each be one line holding one public key, without options.";
      }
      {
        assertion = cfg.nightStartHour != cfg.nightEndHour;
        message = "services.home-staff-host: nightStartHour and nightEndHour must differ.";
      }
    ];

    users.users.staff-read = {
      isSystemUser = true;
      group = "staff-read";
      extraGroups = [ "systemd-journal" ];
      # sshd runs every command through the login shell, so it must be real.
      shell = pkgs.bash;
      description = "Home staff read-only login";
      # `restrict`: no forwarding of any kind and no terminal; commands only.
      openssh.authorizedKeys.keys = [ "restrict ${cfg.readKey}" ];
    };
    users.groups.staff-read = { };

    users.users.staff-deploy = {
      isSystemUser = true;
      group = "staff-deploy";
      home = "/var/lib/staff-deploy";
      createHome = false; # the tmpfiles rules below make it, owned by root
      # Needed by sshd to start the forced command; nothing else can be run.
      shell = pkgs.bash;
      description = "Home staff deploy entrance";
      openssh.authorizedKeys.keys = [
        ''restrict,command="${hostCommand}" ${cfg.deployKey}''
      ];
    };
    users.groups.staff-deploy = { };

    systemd.tmpfiles.rules = [
      # Root keeps its own record of switches here (previous system, pending
      # revert); staff-deploy can pass through but not write.
      "d /var/lib/staff-deploy 0755 root root -"
      # Where staff-deploy-host unpacks the flake source it is sent.
      "d /var/lib/staff-deploy/src 0700 staff-deploy staff-deploy -"
    ];

    # Read by the deploy scripts as text, never sourced. No secrets.
    environment.etc."staff-deploy.conf".text = ''
      REVERT_AFTER_S=${toString cfg.revertAfter}
      NIGHT_START_HOUR=${toString cfg.nightStartHour}
      NIGHT_END_HOUR=${toString cfg.nightEndHour}
      BUILD_TIMEOUT_S=${toString cfg.buildTimeout}
    '';

    # The one sudo rule: staff-deploy may run this one store path as root
    # without a password, and nothing else. Whichever of sudo and sudo-rs is
    # enabled gets it.
    security.sudo.extraRules = mkIf config.security.sudo.enable [ sudoRule ];
    security.sudo-rs.extraRules = mkIf config.security.sudo-rs.enable [ sudoRule ];
  };
}
