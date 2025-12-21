# NixNav

Fast, keyboard-centric file navigator for NixOS/KDE Wayland. GUI equivalent of fzf-powered `fcd`, `fcat`, `fgrep` terminal commands.

![NixNav Icon](nixnav.svg)

## Features

- **Lightning fast** - Uses `fd` and `ripgrep` (Rust) for sub-second searches across large codebases
- **Three modes** - Files, Folders, Search (grep) - switchable with Tab
- **Live preview** - Right panel shows file contents as you navigate
- **System tray** - Runs in background, toggle with global hotkey
- **Keyboard-centric** - Arrow keys, Enter to open, Esc to close
- **Configurable bookmarks** - Quick access to frequently searched directories

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
| `Enter` | Open file (Kate) or folder (Dolphin) |
| `Ctrl+O` | Open containing folder |
| `Tab` | Cycle modes: Files → Folders → Search |
| `Esc` | Close/hide window |

### Global Toggle

Configure `nixnav-toggle` as a global shortcut (e.g., `Meta+F`) in KDE System Settings:
- **System Settings → Keyboard → Shortcuts → Custom Shortcuts**
- Add command: `nixnav-toggle`

### Modes

| Mode | What it searches | Opens with |
|------|------------------|------------|
| Files | File names | Kate |
| Folders | Directory names | Dolphin |
| Search | File contents (grep) | Kate (at match line) |

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
- ripgrep (content search)
- Kate (file editor)
- Dolphin (file manager)

## License

MIT
