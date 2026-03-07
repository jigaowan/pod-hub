{
  description = "Pod Hub development shell";

  inputs.nixpkgs.url = "https://flakehub.com/f/NixOS/nixpkgs/0.1";

  outputs = { nixpkgs, ... }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forEachSupportedSystem = f:
        nixpkgs.lib.genAttrs supportedSystems (system:
          f {
            pkgs = import nixpkgs { inherit system; };
          });
    in
    {
      devShells = forEachSupportedSystem ({ pkgs }:
        let
          python = pkgs.python312;
        in
        {
          default = pkgs.mkShellNoCC {
            venvDir = ".venv";

            packages = [
              python
              python.pkgs.venvShellHook
              python.pkgs.pip
              python.pkgs.setuptools
              python.pkgs.wheel
              pkgs.ffmpeg
              pkgs.sqlite
              pkgs.yt-dlp
            ];

            PYTHONUNBUFFERED = "1";
            PIP_DISABLE_PIP_VERSION_CHECK = "1";
            YTDLP_BIN = "${pkgs.yt-dlp}/bin/yt-dlp";

            postShellHook = ''
              venvVersionWarn() {
                local venvVersion
                venvVersion="$("$venvDir/bin/python" -c 'import platform; print(platform.python_version())')"

                [[ "$venvVersion" == "${python.version}" ]] && return

                cat <<EOF
Warning: Python version mismatch: [$venvVersion (venv)] != [${python.version}]
         Delete '$venvDir' and reload to rebuild for version ${python.version}
EOF
              }

              venvVersionWarn

              # Keep the venv isolated from Nix-provided Python site-packages.
              unset PYTHONPATH

              "$venvDir/bin/python" -c "import fastapi" >/dev/null 2>&1 || echo "Run: pip install -e '.[dev]'"
            '';
          };
        });
    };
}
