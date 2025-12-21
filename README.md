# NixNav

Fast, keyboard-centric file navigator for NixOS/KDE Wayland. GUI equivalent of fzf-powered `fcd`, `fcat` terminal commands.

![NixNav Icon](nixnav.svg)

## Features

- **Lightning fast** - Uses `fd` (Rust) for sub-second searches across large codebases
- **Three modes** - Edit, File, Dir - switchable with Tab
- **Live preview** - Right panel shows file contents as you navigate
- **System tray** - Runs in background, toggle with global hotkey
- **Keyboard-centric** - Arrow keys, Enter to open, Esc to close
- **Configurable bookmarks** - Quick access to frequently searched directories
- **Wayland native** - Works on KDE Plasma Wayland, centers on screen each toggle

## Installation

### NixOS (Recommended)

Add to your NixOS configuration:

```nix
# In your host configuration
imports = [
  ./modules/desktop/nixnav.nix
];
```

See [docs/nixos-integration.md](docs/nixos-integration.md) for the full module.

### Development

```bash
# Enter development shell
nix develop

# Run the app
python main.py

# Or with flake
nix run
```

## Usage

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Arrow Up/Down` | Navigate results |
| `Enter` | Execute action (depends on mode) |
| `Ctrl+O` | Open containing folder |
| `Tab` | Cycle modes: Edit → File → Dir |
| `Esc` | Close/hide window |

### Global Toggle

Configure `nixnav-toggle` as a global shortcut (e.g., `Meta+F`) in KDE System Settings:
- **System Settings → Keyboard → Shortcuts → Custom Shortcuts**
- Add command: `nixnav-toggle`

### Modes

| Mode | Button | What it searches | Enter action |
|------|--------|------------------|--------------|
| Edit | `Edit` | Text files (excludes binaries) | Open file in Kate |
| File | `File` | All files | Open containing folder in Dolphin |
| Dir | `Dir` | Directories | Open folder in Dolphin |

## Configuration

Config stored at `~/.config/nixnav/config.json`:

```json
{
  "bookmarks": [
    {"name": "NixOS Config", "path": "/etc/nixos"},
    {"name": "Home", "path": "/home/nicholas"}
  ],
  "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules"],
  "max_results": 500
}
```

## Requirements

- Python 3.12+
- PySide6 (Qt6)
- fd (file finder)
- Kate (file editor)
- Dolphin (file manager)

## License

MIT
