{ pkgs ? import <nixpkgs> {} }:

let
  python = pkgs.python312;
  pythonEnv = python.withPackages (ps: with ps; [
    pyside6
  ]);
in pkgs.mkShell {
  buildInputs = [
    pythonEnv
    pkgs.fd        # Fast file finder (Rust)
    pkgs.ripgrep   # Fast grep (Rust)
  ];

  shellHook = ''
    unset QT_PLUGIN_PATH
    export QT_QPA_PLATFORM="wayland;xcb"
    echo "NixNav development shell"
    echo "Run: python main.py"
  '';
}
