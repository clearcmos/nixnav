# NixNav

Fast, keyboard-centric file navigator for NixOS/KDE Wayland with instant search powered by a Rust daemon.

![NixNav Icon](nixnav.svg)

## Features

- **Instant search** - Trigram-indexed daemon provides sub-10ms search across 600k+ files
- **Smart previews** - Context-aware: text files, media info (ID3 tags, video codec), archive contents
- **Unified search** - Files and directories in one query
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

# Build the daemon
cd daemon && cargo build --release

# Run the app (daemon auto-starts)
python main.py

# Or with flake
nix run
```

## Usage

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Arrow Up/Down` | Navigate results |
| `Enter` | Open file or folder |
| `Ctrl+O` | Open containing folder |
| `Ctrl+R` | Refresh index (rescan) |
| `Esc` | Close/hide window |

### Global Toggle

Configure `nixnav-toggle` as a global shortcut (e.g., `Meta+F`) in KDE System Settings:
- **System Settings -> Keyboard -> Shortcuts -> Custom Shortcuts**
- Add command: `nixnav-toggle`

### Smart Previews

| File Type | Preview Shows |
|-----------|--------------|
| Text files | File contents |
| Directories | Folder listing |
| Images | Dimensions and file info |
| Audio (MP3, FLAC) | ID3 tags, duration, bitrate, codec |
| Video (MKV, MP4) | Resolution, duration, audio tracks, subtitles |
| Archives (ZIP, TAR) | Contents listing |

## Configuration

Config stored at `~/.config/nixnav/config.json`:

```json
{
  "bookmarks": [
    {"name": "home", "path": "/home/nicholas"}
  ],
  "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules", ".Trash*"],
  "max_results": 500
}
```

### Bookmark Management

- **Add**: Click the `+` button next to the dropdown
- **Rename/Delete**: Right-click on the bookmark dropdown

## Architecture

NixNav consists of two components:

1. **nixnav-daemon** (Rust) - High-performance indexing daemon
   - Trigram-based posting lists for instant substring search
   - SQLite persistence (~5s warm start for 600k files)
   - inotify for real-time updates + periodic integrity checker

2. **main.py** (Python/Qt) - GUI application
   - Auto-starts daemon on launch
   - Falls back to `fd` if daemon unavailable

## Data Locations

| Data | Location |
|------|----------|
| Config | `~/.config/nixnav/config.json` |
| Index | `~/.local/share/nixnav/index.db` |

## Requirements

- Python 3.12+
- PySide6 (Qt6)
- Rust (for daemon)
- ffmpeg (for media previews)
- Dolphin (file manager)

## License

MIT
