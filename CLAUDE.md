# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NixNav is a GUI file navigator for NixOS/KDE Wayland built with Python and PySide6 (Qt6), backed by a high-performance Rust daemon for instant search across millions of files. It's the GUI equivalent of the `fcd`, `fcat` terminal commands defined in `/etc/nixos/modules/core/dev/fzf.nix`.

## Architecture

**Two-component system:**

1. **`nixnav-daemon`** (Rust) - Background indexing daemon
   - Trigram-based posting lists for instant substring search
   - SQLite persistence for index cache
   - inotify for real-time local filesystem updates
   - Periodic integrity checker for detecting bulk deletes
   - Unix socket API at `/run/user/1000/nixnav-daemon.sock`

2. **`main.py`** (Python/Qt) - GUI application
   - **DaemonClient**: Communicates with daemon via Unix socket
   - **FileScanner**: Background thread with daemon query (fallback to `fd`)
   - **NixNavWindow**: Main window with search, results list, and smart preview panel
   - **NixNavApp**: Application controller managing system tray, window, and IPC

## Development Commands

```bash
# Enter development shell
nix develop

# Build the daemon
cd daemon && cargo build --release

# Run the app (daemon auto-starts)
python main.py

# Or run directly with flake
nix run

# Build the package
nix build

# Test toggle (with app running)
./nixnav-toggle
```

## Key Features

- **Instant search**: Trigram index enables sub-10ms search across 600k+ files
- **Unified search**: Searches both files and directories in one query
- **Smart previews**: Context-aware preview panel based on file type
- **Bookmarks**: Configurable directories to search within
- **Auto-indexing**: Daemon auto-starts and indexes bookmarks on launch
- **Real-time updates**: inotify watches for file changes
- **Integrity checker**: Periodic verification catches bulk deletes
- **Wayland native**: Centers on screen each toggle
- **System tray**: Left-click opens overlay, right-click for menu
- **Keyboard-centric**: Arrow keys navigate, Enter opens, Esc closes

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Arrow Up/Down` | Navigate results |
| `Enter` | Open file (xdg-open) or folder (Dolphin) |
| `Ctrl+O` | Open containing folder in Dolphin |
| `Ctrl+R` | Rescan current bookmark (refresh index) |
| `Esc` | Close overlay |

## Smart Preview System

The preview panel shows context-appropriate content based on file type:

| File Type | Preview Shows |
|-----------|--------------|
| Directories | Folder contents listing |
| Text files | File contents (up to 50KB) |
| Binary files | File info (name, size, type, dimensions for images) |
| Audio (MP3, FLAC, etc.) | ID3 tags, duration, bitrate, codec, sample rate |
| Video (MKV, MP4, etc.) | Duration, resolution, frame rate, audio tracks, subtitles |
| Archives (ZIP, TAR, etc.) | Contents listing with file sizes |

Requires `ffprobe` (from ffmpeg) for audio/video metadata.

## Data Locations

| Data | Location |
|------|----------|
| GUI Config | `~/.config/nixnav/config.json` |
| Daemon Index | `~/.local/share/nixnav/index.db` |
| GUI Socket | `$XDG_RUNTIME_DIR/nixnav.sock` |
| Daemon Socket | `$XDG_RUNTIME_DIR/nixnav-daemon.sock` |

## Config Structure

```json
{
  "bookmarks": [
    {"name": "home", "path": "/home/nicholas"}
  ],
  "last_bookmark": 0,
  "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules", "*.log", ".Trash*", "Trash"],
  "max_results": 500,
  "window_geometry": "<base64 encoded Qt geometry>"
}
```

## Daemon Exclude Patterns

The daemon automatically excludes:
- `.git`, `node_modules`, `__pycache__`, `.cache`, `.npm`, `.cargo`
- `target`, `build`, `dist`, `.next`, `.nuxt`
- `.Trash*`, `Trash` (all trash folders)

## Bookmark Management

- **Add**: Click the + button next to the dropdown
- **Rename/Delete**: Right-click on the bookmark dropdown for context menu
- **Auto-sync**: Bookmarks are automatically indexed by daemon on launch

## IPC Protocol

### GUI Toggle Socket
```
$XDG_RUNTIME_DIR/nixnav.sock
Command: "toggle" -> Show/hide window
```

### Daemon Socket
```
$XDG_RUNTIME_DIR/nixnav-daemon.sock
Commands:
  PING -> {"status": "pong"}
  STATS -> {"files": N, "trigrams": N, "bookmarks": N}
  SEARCH {"bookmark_path": "...", "mode": "all", "query": "...", "extension": null}
  RESCAN /path -> {"status": "ok", "indexed": N}
  ADD_BOOKMARK {"name": "...", "path": "...", "is_network": false}
```

## Wayland Considerations

Wayland does not allow applications to set their own window positions (security feature). NixNav handles this by:
- Saving/restoring window **size** between sessions
- **Centering** the window on the primary screen each time it's shown
- This provides predictable, consistent UX similar to KRunner

## Known Behaviors

- **Max results**: Limited to 2000 items from daemon
- **Integrity check**: Runs every 60 seconds, checks 5000 files per cycle
- **Network mounts**: Rescanned every 5 minutes (no inotify support)
- **Daemon auto-start**: GUI starts daemon if not running
