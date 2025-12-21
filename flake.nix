{
  description = "NixNav - GUI file navigator for NixOS/KDE Wayland";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        python = pkgs.python312;

        pythonEnv = python.withPackages (ps: with ps; [
          pyside6
        ]);

      in {
        packages.default = pkgs.stdenv.mkDerivation {
          pname = "nixnav";
          version = "0.1.0";

          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          buildInputs = [ pythonEnv ];

          installPhase = ''
            mkdir -p $out/bin $out/share/nixnav
            cp -r *.py $out/share/nixnav/

            makeWrapper ${pythonEnv}/bin/python $out/bin/nixnav \
              --add-flags "$out/share/nixnav/main.py" \
              --unset QT_PLUGIN_PATH \
              --set QT_QPA_PLATFORM "wayland;xcb" \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.fd pkgs.ripgrep ]}
          '';

          meta = with pkgs.lib; {
            description = "GUI file navigator for NixOS - browse files, folders, grep contents";
            license = licenses.mit;
            platforms = platforms.linux;
          };
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonEnv
            pkgs.fd
            pkgs.ripgrep
            # kate and dolphin already installed system-wide
          ];

          shellHook = ''
            # Clear QT_PLUGIN_PATH to avoid Qt version mismatch with system plugins
            unset QT_PLUGIN_PATH
            export QT_QPA_PLATFORM="wayland;xcb"
            echo "NixNav development shell"
            echo "Run: python main.py"
          '';
        };

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nixnav";
        };
      }
    );
}
