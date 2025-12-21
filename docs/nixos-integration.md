# NixOS Integration

## Installation

### Option 1: Import Module

Create a module file (e.g., `nixnav.nix`):

```nix
# NixNav - GUI file navigator
{ config, pkgs, lib, ... }:

let
  nixnavSrc = pkgs.fetchFromGitHub {
    owner = "clearcmos";
    repo = "nixnav";
    rev = "main";  # Or pin to specific commit
    sha256 = "sha256-XXXX";  # Get with: nix-prefetch-github clearcmos nixnav
  };

  python = pkgs.python312;
  pythonEnv = python.withPackages (ps: with ps; [ pyside6 ]);

  nixnav = pkgs.stdenv.mkDerivation {
    pname = "nixnav";
    version = "0.1.0";
    src = nixnavSrc;
    nativeBuildInputs = [ pkgs.makeWrapper ];
    buildInputs = [ pythonEnv ];

    installPhase = ''
      mkdir -p $out/bin $out/share/nixnav
      mkdir -p $out/share/icons/hicolor/scalable/apps
      mkdir -p $out/share/applications

      cp -r *.py $out/share/nixnav/
      cp nixnav.svg $out/share/icons/hicolor/scalable/apps/
      cp nixnav.desktop $out/share/applications/

      makeWrapper ${pythonEnv}/bin/python $out/bin/nixnav \
        --add-flags "$out/share/nixnav/main.py" \
        --unset QT_PLUGIN_PATH \
        --set QT_QPA_PLATFORM "wayland;xcb" \
        --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.fd pkgs.ripgrep ]}
    '';
  };

  nixnavToggle = pkgs.writeScriptBin "nixnav-toggle" ''
    #!${pkgs.python3}/bin/python3
    import socket, os, sys
    sock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "nixnav.sock")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(sock_path)
        s.send(b"toggle")
        s.close()
    except:
        sys.exit(1)
  '';
in
{
  environment.systemPackages = [ nixnav nixnavToggle ];

  # Autostart as user service
  systemd.user.services.nixnav = {
    description = "NixNav file navigator";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    serviceConfig = {
      ExecStart = "${nixnav}/bin/nixnav";
      Restart = "on-failure";
      RestartSec = 3;
    };
  };
}
```

Import in your host configuration:

```nix
imports = [
  ./nixnav.nix
];
```

### Option 2: Overlay

Add to your flake's overlays:

```nix
overlays = [
  (final: prev: {
    nixnav = prev.callPackage ./pkgs/nixnav.nix {};
  })
];
```

## Global Shortcut Setup

After installation, configure Meta+F (or your preferred key) in KDE:

1. **System Settings** → **Keyboard** → **Shortcuts**
2. **Custom Shortcuts** → **Edit** → **New** → **Global Shortcut** → **Command/URL**
3. Set trigger to `Meta+F`
4. Set command to `nixnav-toggle`

## Updating SHA256

When the repo is updated:

```bash
# Get new hash
nix-prefetch-github clearcmos nixnav

# Or force fetch with fake hash
nix-build -E 'with import <nixpkgs> {}; fetchFromGitHub {
  owner = "clearcmos";
  repo = "nixnav";
  rev = "main";
  sha256 = lib.fakeSha256;
}' 2>&1 | grep "got:"
```

## Dependencies

The module automatically provides:
- `fd` - Fast file finder
- `ripgrep` - Fast content search
- `python312` with `pyside6` - Qt6 GUI framework

System should have:
- `kate` - File editor (opens files)
- `dolphin` - File manager (opens folders)

## Troubleshooting

### App doesn't start
```bash
# Check service status
systemctl --user status nixnav

# View logs
journalctl --user -u nixnav -n 50

# Manual start for debugging
nixnav
```

### Toggle doesn't work
```bash
# Check socket exists
ls -la $XDG_RUNTIME_DIR/nixnav.sock

# Test toggle manually
nixnav-toggle
echo $?  # Should be 0 if app is running
```

### Icon not showing in menu
```bash
# Refresh icon cache
gtk-update-icon-cache -f /run/current-system/sw/share/icons/hicolor

# Or logout/login to refresh KDE
```
