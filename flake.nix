{
  description = "Home staff: a front desk and a small staff of agents for the homelab";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = p: builtins.elem (lib.getName p) [ "claude-code" ];
      }));

      pyDeps = pkgs: with pkgs.python3Packages; [ matrix-nio aiohttp claude-agent-sdk ];

      # One of the two scripts that run on every target host. The script is
      # checked (bash -n, shellcheck), installed, and started through a
      # wrapper that replaces PATH, so nothing is inherited from the SSH
      # session or from sudo. /run/wrappers/bin, the setuid wrapper directory
      # of NixOS, is on PATH only where `sudo` is needed.
      hostScript = pkgs: { name, src, runtimeInputs, withSudo ? false, env ? { }, postPatch ? "" }:
        pkgs.stdenvNoCC.mkDerivation {
          inherit name src;
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.makeWrapper pkgs.shellcheck ];
          buildInputs = [ pkgs.bash ];
          doCheck = true;
          checkPhase = ''
            bash -n "$src"
            shellcheck --shell=bash --severity=warning "$src"
          '';
          installPhase = ''
            install -Dm755 "$src" "$out/libexec/${name}"
            patchShebangs "$out/libexec/${name}"
            ${postPatch}
            makeWrapper "$out/libexec/${name}" "$out/bin/${name}" \
              --set PATH ${lib.escapeShellArg (lib.makeBinPath runtimeInputs + lib.optionalString withSudo ":/run/wrappers/bin")} \
              ${lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "--set ${k} ${lib.escapeShellArg v}") env)}
          '';
          meta.mainProgram = name;
        };

      # Both host scripts as a pair, so the path the first one calls is
      # always the second one. nix/host.nix calls this with the host's own
      # nix, nixos-rebuild, and systemd, so a switch uses the tools the
      # host itself would use.
      hostScripts =
        { pkgs
        , nix ? pkgs.nix
        , nixos-rebuild ? pkgs.nixos-rebuild
        , systemd ? pkgs.systemd
        }: rec {
          # Runs as root through the one sudo rule of nix/host.nix. The rule
          # names this package's bin/staff-deploy-root by its store path.
          staff-deploy-root = hostScript pkgs {
            name = "staff-deploy-root";
            src = ./nix/staff-deploy-root.sh;
            runtimeInputs = [ systemd nixos-rebuild nix ] ++ (with pkgs; [
              coreutils hostname gnutar gzip gnugrep gnused gawk findutils util-linux git
            ]);
          };

          # The forced command of the staff-deploy SSH key. It must reach the
          # root script by the one store path the sudo rule allows, never by
          # a PATH lookup. A script that reads STAFF_DEPLOY_ROOT gets it from
          # the wrapper; one that calls `sudo -n staff-deploy-root` gets the
          # name replaced by the store path in the installed copy. The build
          # fails if neither form is found.
          staff-deploy-host = hostScript pkgs {
            name = "staff-deploy-host";
            src = ./nix/staff-deploy-host.sh;
            withSudo = true;
            runtimeInputs = with pkgs; [ coreutils findutils gnutar gzip gnugrep gnused ];
            env.STAFF_DEPLOY_ROOT = "${staff-deploy-root}/bin/staff-deploy-root";
            postPatch = ''
              if ! grep -q STAFF_DEPLOY_ROOT "$out/libexec/staff-deploy-host"; then
                substituteInPlace "$out/libexec/staff-deploy-host" \
                  --replace-fail "sudo -n staff-deploy-root" "sudo -n ${staff-deploy-root}/bin/staff-deploy-root"
              fi
            '';
          };
        };
    in {
      lib = { inherit hostScripts; };

      packages = forAllSystems (pkgs:
        let py = pkgs.python3Packages; in {
          default = py.buildPythonApplication {
            pname = "home-staff";
            version = "0.1.0";
            pyproject = true;
            src = ./.;
            build-system = [ py.setuptools ];
            dependencies = pyDeps pkgs;
            nativeCheckInputs = [ py.pytestCheckHook py.pytest-asyncio ];
            # The Agent SDK in nixpkgs ships without the CLI, so put it on PATH.
            makeWrapperArgs = [ "--prefix" "PATH" ":" (lib.makeBinPath [ pkgs.claude-code pkgs.git pkgs.openssh ]) ];
            # Read-only member definitions, used as members_dir by the NixOS module.
            postInstall = ''
              mkdir -p $out/share/home-staff/members
              cp -r members/. $out/share/home-staff/members/
            '';
          };
        } // hostScripts { inherit pkgs; });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: pyDeps pkgs ++ [ ps.pytest ps.pytest-asyncio ]))
            pkgs.claude-code
            pkgs.shellcheck
            pkgs.git
          ];
        };
      });

      # The staff's own host: front desk, member workers, key relay, deploy door.
      nixosModules.default = import ./nix/module.nix self;
      # Every host the staff may read from and deploy to.
      nixosModules.host = import ./nix/host.nix self;
    };
}
