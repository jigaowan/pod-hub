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
  options = [ "stateDir" "outputDir" "environmentFile" ];
  system = "x86_64-linux";
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
    outputDir = "/srv/pod-hub-custom";
    environmentFile = "/run/secrets/pod-hub.env";
  };
  service = custom.systemd.services.pod-hub.serviceConfig;
in
assert lib.all rejectsRelative options;
assert lib.all rejectsPathLiteral options;
assert failedPodHubAssertions defaults == [];
assert failedPodHubAssertions custom == [];
assert !(defaults.systemd.services.pod-hub.serviceConfig ? EnvironmentFile);
assert service.EnvironmentFile == "/run/secrets/pod-hub.env";
assert service.ReadWritePaths == [ "/var/lib/pod-hub-custom" "/srv/pod-hub-custom" ];
assert lib.elem "d /var/lib/pod-hub-custom 0750 pod-hub pod-hub -" custom.systemd.tmpfiles.rules;
assert lib.elem "d /srv/pod-hub-custom 0755 pod-hub pod-hub -" custom.systemd.tmpfiles.rules;
true
