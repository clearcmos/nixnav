{
  description = "NixNav - GUI file navigator for NixOS/KDE Wayland with high-performance daemon";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    rust-overlay.url = "github:oxalica/rust-overlay";
  };

  outputs = { self, nixpkgs, flake-utils, rust-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        overlays = [ (import rust-overlay) ];
        pkgs = import nixpkgs {
          inherit system overlays;
        };

        python = pkgs.python312;

        pythonEnv = python.withPackages (ps: with ps; [
          pyside6
        ]);

        rust = pkgs.rust-bin.stable.latest.default.override {
          extensions = [ "rust-src" ];
        };

        # Build the Rust daemon
        nixnav-daemon = pkgs.rustPlatform.buildRustPackage {
          pname = "nixnav-daemon";
          version = "0.1.0";

          src = ./daemon;

          cargoLock = {
            lockFile = ./daemon/Cargo.lock;
          };

          nativeBuildInputs = [ pkgs.pkg-config ];
          buildInputs = [ pkgs.sqlite ];

          meta = with pkgs.lib; {
            description = "High-performance file indexing daemon for NixNav";
            license = licenses.mit;
            platforms = platforms.linux;
          };
        };

      in {
        packages = {
          daemon = nixnav-daemon;

          default = pkgs.stdenv.mkDerivation {
            pname = "nixnav";
            version = "0.1.0";

            src = ./.;

            nativeBuildInputs = [ pkgs.makeWrapper ];

            buildInputs = [ pythonEnv ];

            installPhase = ''
              mkdir -p $out/bin $out/share/nixnav

              cp -r *.py $out/share/nixnav/

              # Install the daemon
              cp ${nixnav-daemon}/bin/nixnav-daemon $out/bin/

              # Main GUI wrapper
              makeWrapper ${pythonEnv}/bin/python $out/bin/nixnav \
                --add-flags "$out/share/nixnav/main.py" \
                --unset QT_PLUGIN_PATH \
                --set QT_QPA_PLATFORM "wayland;xcb" \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.fd pkgs.ripgrep ]}

              # Toggle script
              cat > $out/bin/nixnav-toggle << 'EOF'
#!/usr/bin/env python3
import socket
import os
import sys

sock_path = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}") + "/nixnav.sock"

try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    sock.connect(sock_path)
    sock.send(b"toggle")
    sock.close()
except:
    # Start nixnav if not running
    os.execvp("nixnav", ["nixnav"])
EOF
              chmod +x $out/bin/nixnav-toggle
            '';

            meta = with pkgs.lib; {
              description = "GUI file navigator for NixOS - browse files, folders with instant search";
              license = licenses.mit;
              platforms = platforms.linux;
            };
          };
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonEnv
            rust
            pkgs.rust-analyzer
            pkgs.fd
            pkgs.ripgrep
            pkgs.pkg-config
            pkgs.sqlite
          ];

          shellHook = ''
            unset QT_PLUGIN_PATH
            export QT_QPA_PLATFORM="wayland;xcb"
            echo "NixNav development shell"
            echo ""
            echo "Commands:"
            echo "  python main.py          - Run the GUI"
            echo "  cd daemon && cargo run  - Run the daemon"
            echo "  cd daemon && cargo build --release  - Build optimized daemon"
          '';
        };

        apps.default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nixnav";
        };
      }
    );
}
