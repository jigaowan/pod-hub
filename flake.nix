{
  description = "Pod Hub V2, a YouTube-to-podcast service for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      appDependencies = ps: with ps; [
        fastapi starlette uvicorn jinja2 python-multipart pydantic httpx socksio pathvalidate
      ];
      testDependencies = ps: with ps; [ pytest pytest-asyncio ];
      toolPython = pkgs: package:
        let
          python = if (package.pythonModule or false) != false then package.pythonModule
                   else package.python or pkgs.python3;
        in python.withPackages (ps: [ (ps.toPythonModule package) ]);
    in
    {
      packages = forSystems (pkgs:
        let
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
          buildVersion =
            if self ? shortRev then "v${version}+${self.shortRev}"
            else if self ? dirtyShortRev then "v${version}+${self.dirtyShortRev}"
            else "v${version}";
        in {
        default = pkgs.python312Packages.buildPythonApplication {
          pname = "pod-hub";
          inherit version;
          pyproject = true;
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [ ./podhub ./tests ./pyproject.toml ./README.md ];
          };
          build-system = [ pkgs.python312Packages.hatchling ];
          dependencies = appDependencies pkgs.python312Packages;
          nativeCheckInputs = testDependencies pkgs.python312Packages ++ [ pkgs.ffmpeg ];
          nativeBuildInputs = [ pkgs.makeWrapper ];
          nativeInstallCheckInputs = [ pkgs.python312Packages.pytestCheckHook ];
          pythonImportsCheck = [ "podhub.cli" "podhub.main" ];
          makeWrapperArgs = [
            "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.ffmpeg pkgs.deno ])
            "--set-default" "PODHUB_DEFAULT_YTDLP_PYTHON" "${toolPython pkgs pkgs.yt-dlp}/bin/python3"
            "--set-default" "PODHUB_BUILD_VERSION" buildVersion
          ];
          meta.mainProgram = "pod-hub";
        };
      });

      devShells = forSystems (pkgs: {
        default = pkgs.mkShellNoCC {
          packages = [
            (pkgs.python312.withPackages (ps: appDependencies ps ++ testDependencies ps ++
              [ ps.yt-dlp ps.hatchling ps.pip ]))
            pkgs.ffmpeg pkgs.deno pkgs.sqlite
          ];
          PYTHONUNBUFFERED = "1";
        };
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.pod-hub;
          defaultAccount = "pod-hub";
          toml = pkgs.formats.toml {};
          effectiveYtdlp =
            if cfg.jsRuntimePackage != null && cfg.ytdlpPackage ? override
            then cfg.ytdlpPackage.override { jsRuntime = cfg.jsRuntimePackage; }
            else cfg.ytdlpPackage;
          effectiveYtdlpPython =
            if cfg.ytdlpPython != null then cfg.ytdlpPython
            else "${toolPython pkgs effectiveYtdlp}/bin/python3";
          configFile = toml.generate "pod-hub-config.toml" (lib.recursiveUpdate {
            paths = { state_dir = cfg.stateDir; output_dir = cfg.outputDir; };
            web = { listen_host = cfg.listenAddress; listen_port = cfg.port; base_url = cfg.baseUrl; };
            toolchain = {
              ytdlp_python = effectiveYtdlpPython;
              ffmpeg_path = "${cfg.ffmpegPackage}/bin/ffmpeg";
              ffprobe_path = "${cfg.ffmpegPackage}/bin/ffprobe";
              js_runtime = if cfg.jsRuntimePackage == null then "" else lib.getExe cfg.jsRuntimePackage;
            };
          } cfg.settings);
        in
        {
          options.services.pod-hub = {
            enable = lib.mkEnableOption "Pod Hub";
            package = lib.mkPackageOption self.packages.${pkgs.stdenv.hostPlatform.system} "default" {};
            user = lib.mkOption { type = lib.types.str; default = defaultAccount; description = "Service user."; };
            group = lib.mkOption { type = lib.types.str; default = defaultAccount; description = "Service group."; };
            listenAddress = lib.mkOption { type = lib.types.str; default = "127.0.0.1"; description = "HTTP listen address."; };
            port = lib.mkOption { type = lib.types.port; default = 8000; description = "HTTP listen port."; };
            baseUrl = lib.mkOption { type = lib.types.str; default = "http://127.0.0.1:8000"; description = "Public URL used in feeds."; };
            stateDir = lib.mkOption { type = lib.types.str; default = "/var/lib/pod-hub"; description = "Absolute path for the private database, backups and working state."; };
            outputDir = lib.mkOption { type = lib.types.str; default = "/var/lib/pod-hub/output"; description = "Absolute path for published podcast folders."; };
            ytdlpPackage = lib.mkPackageOption pkgs "yt-dlp" {};
            ytdlpPython = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Optional absolute path to a Python interpreter that can import yt_dlp (use when supplying yt-dlp from another nixpkgs with a different Python version).";
            };
            ffmpegPackage = lib.mkPackageOption pkgs "ffmpeg" {};
            jsRuntimePackage = lib.mkOption { type = lib.types.nullOr lib.types.package; default = pkgs.deno; description = "yt-dlp JavaScript runtime package."; };
            environmentFile = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; description = "Absolute path to an optional systemd environment file for secrets."; };
            settings = lib.mkOption { type = toml.type; default = {}; description = "Additional TOML settings. Use stateDir/outputDir instead of settings.paths, and keep secrets in environmentFile."; };
          };

          config = lib.mkIf cfg.enable {
            assertions = (lib.mapAttrsToList (name: value: {
              assertion = value == null || lib.hasPrefix "/" value;
              message = "services.pod-hub.${name} must be an absolute path.";
            }) { inherit (cfg) stateDir outputDir environmentFile ytdlpPython; }) ++ [
              {
                assertion = !(cfg.settings ? paths);
                message = "services.pod-hub.settings.paths must not be set; configure services.pod-hub.stateDir and services.pod-hub.outputDir instead.";
              }
            ];
            users.groups = lib.optionalAttrs (cfg.group == defaultAccount) { ${defaultAccount} = {}; };
            users.users = lib.optionalAttrs (cfg.user == defaultAccount) {
              ${defaultAccount} = { isSystemUser = true; group = cfg.group; };
            };
            systemd.tmpfiles.settings."10-pod-hub" = {
              ${cfg.stateDir}.d = { mode = "0750"; user = cfg.user; group = cfg.group; };
              ${cfg.outputDir}.d = { mode = "0755"; user = cfg.user; group = cfg.group; };
            };
            systemd.services.pod-hub = {
              description = "Pod Hub podcast service";
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];
              wantedBy = [ "multi-user.target" ];
              environment.PODHUB_CONFIG = configFile;
              serviceConfig = {
                ExecStart = "${lib.getExe cfg.package} serve";
                User = cfg.user;
                Group = cfg.group;
                Restart = "on-failure";
                RestartSec = 5;
                TimeoutStopSec = 30;
                NoNewPrivileges = true;
                PrivateTmp = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                ReadWritePaths = [ cfg.stateDir cfg.outputDir ];
              } // lib.optionalAttrs (cfg.environmentFile != null) {
                EnvironmentFile = cfg.environmentFile;
              };
            };
          };
        };
    };
}
