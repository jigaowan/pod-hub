let
  flake = builtins.getFlake ("git+file://" + toString ../.);
  lib = flake.inputs.nixpkgs.lib;
  evaluate = system: overrides: (lib.nixosSystem {
    inherit system;
    modules = [
      flake.nixosModules.default
      {
        system.stateVersion = "26.05";
        services.pod-hub = { enable = true; } // overrides;
      }
    ];
  }).config;
  options = [ "stateDir" "outputDir" "environmentFile" "ytdlpPython" ];
  system = "x86_64-linux";
  pkgs = flake.inputs.nixpkgs.legacyPackages.${system};
  failedPodHubAssertions = config: builtins.filter
    (result: !result.assertion && lib.hasPrefix "services.pod-hub." result.message)
    config.assertions;
  rejectsRelative = name:
    let config = evaluate system { ${name} = "relative/path"; };
    in lib.any (result: result.message == "services.pod-hub.${name} must be an absolute path.")
      (failedPodHubAssertions config);
  rejectsPathLiteral = name:
    let config = evaluate system { ${name} = /run/pod-hub-test-path; };
    in !(builtins.tryEval (builtins.seq config.services.pod-hub.${name} true)).success;
  defaults = evaluate system {};
  custom = evaluate "aarch64-linux" {
    stateDir = "/var/lib/pod-hub-custom";
    outputDir = "/srv/Podcast Archive";
    environmentFile = "/run/secrets/pod-hub.env";
    ytdlpPython = "/run/current-system/sw/bin/python3";
  };
  withNodeRuntime = evaluate system {
    jsRuntimePackage = pkgs.nodejs;
  };
  conflictingPaths = evaluate system {
    settings.paths.output_dir = "/srv/other";
  };
  service = custom.systemd.services.pod-hub.serviceConfig;
  tmpfiles = custom.systemd.tmpfiles.settings."10-pod-hub";
in
assert lib.all rejectsRelative options;
assert lib.all rejectsPathLiteral options;
assert failedPodHubAssertions defaults == [];
assert failedPodHubAssertions custom == [];
assert failedPodHubAssertions withNodeRuntime == [];
assert lib.any (result: lib.hasInfix "settings.paths" result.message) (failedPodHubAssertions conflictingPaths);
assert !(defaults.systemd.services.pod-hub.serviceConfig ? EnvironmentFile);
assert service.EnvironmentFile == "/run/secrets/pod-hub.env";
assert service.ReadWritePaths == [ "/var/lib/pod-hub-custom" "/srv/Podcast Archive" ];
assert tmpfiles."/var/lib/pod-hub-custom".d.mode == "0750";
assert tmpfiles."/var/lib/pod-hub-custom".d.user == "pod-hub";
assert tmpfiles."/var/lib/pod-hub-custom".d.group == "pod-hub";
assert tmpfiles."/srv/Podcast Archive".d.mode == "0755";
assert tmpfiles."/srv/Podcast Archive".d.user == "pod-hub";
assert tmpfiles."/srv/Podcast Archive".d.group == "pod-hub";
true
