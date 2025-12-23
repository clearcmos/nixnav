# NixOS Integration

## Installation

### Option 1: Import Module

Create a module file (e.g., `nixnav.nix`):

```nix
# NixNav - GUI file navigator with Rust daemon
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

  # Build the Rust daemon
  nixnavDaemon = pkgs.rustPlatform.buildRustPackage {
    pname = "nixnav-daemon";
    version = "0.1.0";
    src = "${nixnavSrc}/daemon";
    cargoLock.lockFile = "${nixnavSrc}/daemon/Cargo.lock";
  };

  # Build the Python GUI
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
        --prefix PATH : ${pkgs.lib.makeBinPath [
          pkgs.fd
          pkgs.ripgrep
          pkgs.ffmpeg  # For media previews
          nixnavDaemon
        ]}
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
  environment.systemPackages = [ nixnav nixnavDaemon nixnavToggle ];

  # Autostart GUI as user service
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

  # Optional: Start daemon as separate service (GUI auto-starts it anyway)
  systemd.user.services.nixnav-daemon = {
    description = "NixNav indexing daemon";
    wantedBy = [ "default.target" ];
    serviceConfig = {
      ExecStart = "${nixnavDaemon}/bin/nixnav-daemon";
      Restart = "on-failure";
      RestartSec = 5;
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

### Option 2: Flake-based

If the repository has a flake.nix, you can use it directly:

```nix
{
  inputs.nixnav.url = "github:clearcmos/nixnav";

  outputs = { self, nixpkgs, nixnav, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        ({ pkgs, ... }: {
          environment.systemPackages = [
            nixnav.packages.${pkgs.system}.default
            nixnav.packages.${pkgs.system}.nixnav-daemon
          ];
        })
      ];
    };
  };
}
```

## Global Shortcut Setup

After installation, configure Meta+F (or your preferred key) in KDE:

1. **System Settings** -> **Keyboard** -> **Shortcuts**
2. **Custom Shortcuts** -> **Edit** -> **New** -> **Global Shortcut** -> **Command/URL**
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
- `fd` - Fast file finder (fallback search)
- `ripgrep` - Fast content search
- `ffmpeg` - Media file previews (ffprobe)
- `python312` with `pyside6` - Qt6 GUI framework
- `nixnav-daemon` - Rust indexing daemon

System should have:
- `dolphin` - File manager (opens folders)

## Data Locations

| Data | Location |
|------|----------|
| GUI Config | `~/.config/nixnav/config.json` |
| Daemon Index | `~/.local/share/nixnav/index.db` |
| GUI Socket | `$XDG_RUNTIME_DIR/nixnav.sock` |
| Daemon Socket | `$XDG_RUNTIME_DIR/nixnav-daemon.sock` |

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

### Daemon not indexing
```bash
# Check daemon status
systemctl --user status nixnav-daemon

# Check daemon logs
journalctl --user -u nixnav-daemon -n 50

# Manual daemon start
nixnav-daemon

# Check daemon stats
echo "STATS" | nc -U $XDG_RUNTIME_DIR/nixnav-daemon.sock
```

### Toggle doesn't work
```bash
# Check socket exists
ls -la $XDG_RUNTIME_DIR/nixnav.sock

# Test toggle manually
nixnav-toggle
echo $?  # Should be 0 if app is running
```

### Search is slow
```bash
# Check if daemon is connected (should show indexed count in status bar)
# If showing "0 indexed", daemon isn't running or not connected

# Force rescan
echo "RESCAN /home/username" | nc -U $XDG_RUNTIME_DIR/nixnav-daemon.sock
```

### Icon not showing in menu
```bash
# Refresh icon cache
gtk-update-icon-cache -f /run/current-system/sw/share/icons/hicolor

# Or logout/login to refresh KDE
```

### Clear index and rebuild
```bash
# Stop daemon
systemctl --user stop nixnav-daemon

# Remove index
rm ~/.local/share/nixnav/index.db

# Restart daemon (will rebuild index)
systemctl --user start nixnav-daemon
```
