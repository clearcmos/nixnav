# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NixNav is a GUI file navigator for NixOS/KDE Wayland built with Python and PySide6 (Qt6). It's the GUI equivalent of the `fcd`, `fcat`, `fgrep`, `fnano` terminal commands defined in `/etc/nixos/modules/core/dev/fzf.nix`.

## Development Commands

```bash
# Enter development shell
nix develop

# Run the app
python main.py

# Or run directly with flake
nix run

# Build the package
nix build

# Test toggle (with app running)
./nixnav-toggle
```

## Architecture

**Single-file application** (`main.py`) with these main classes:

- **Config**: Manages bookmarks, exclude patterns, and settings in `~/.config/nixnav/config.json`
- **FileScanner(QObject)**: Background thread using `fd` for file/folder scanning
- **GrepScanner(QObject)**: Background thread using `ripgrep` for content search
- **NixNavWindow(QWidget)**: Main window with search, results list, and preview panel
- **NixNavApp**: Application controller managing system tray, window, and IPC socket
- **SocketListener(QThread)**: Unix socket server for receiving toggle commands

### Why fd/ripgrep?

Initial implementation used Python's `pathlib.rglob()` which was slow (~2-5s for large directories). Switched to subprocess calls to `fd` and `rg` (Rust binaries) for 50-100x speedup.

### IPC Toggle Mechanism

The app creates a Unix socket at `$XDG_RUNTIME_DIR/nixnav.sock` for instant window toggling:

1. **Server side** (`main.py`): `SocketListener` thread accepts connections, emits Qt signal on "toggle" message
2. **Client side** (`nixnav-toggle`): Minimal Python script sends "toggle" to socket, exits immediately
3. **Why not QLocalSocket?** Direct Unix sockets avoid Qt overhead for sub-50ms response time

## Key Features

- **Three modes**: Files, Folders, Search (grep) - switchable via Tab or mode buttons
- **Bookmarks**: Configurable directories to search within
- **Live preview**: Right panel shows file contents as you navigate
- **Background scanning**: File search runs in separate thread to keep UI responsive
- **Wayland native**: Uses `QT_QPA_PLATFORM=wayland;xcb` with XCB fallback
- **System tray**: Left-click opens overlay, right-click for menu
- **Keyboard-centric**: Arrow keys navigate, Enter opens, Tab cycles modes, Esc closes

## Keyboard Shortcuts

- `Arrow Up/Down` - Navigate results
- `Enter` - Open selected in Kate (file) or Dolphin (folder)
- `Ctrl+O` - Open containing folder in Dolphin
- `Tab` - Cycle through modes (Files → Folders → Search)
- `Esc` - Close overlay

## Actions

| Mode | Enter Action | What Opens |
|------|--------------|------------|
| Files | Open file | Kate |
| Folders | Open folder | Dolphin |
| Search | Open file with match | Kate |

## Data Locations

- Config: `~/.config/nixnav/config.json`

## Config Structure

```json
{
  "bookmarks": [
    {"name": "NixOS Config", "path": "/etc/nixos"},
    {"name": "Home", "path": "/home/nicholas"}
  ],
  "last_bookmark": 0,
  "last_mode": "files",
  "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules", "*.log"],
  "max_preview_lines": 200,
  "max_results": 500
}
```

## Comparison to Terminal Commands

| Terminal | NixNav Mode | Action |
|----------|-------------|--------|
| `fcd` | Folders | Opens selected folder in Dolphin |
| `fcat` | Files | Preview panel shows contents, Enter opens in Kate |
| `fgrep` | Search | Grep file contents, shows matches, Enter opens in Kate |
| `fnano` | Files | Enter opens file in Kate (not nano) |

## Known Behaviors

- **Grep minimum**: Search mode requires at least 2 characters before scanning
- **Max results**: Limited to 500 items (100 for grep) to keep performance snappy
- **Text files only**: Grep mode only searches recognized text file extensions
- **Exclude patterns**: Skips .git, node_modules, __pycache__ etc by default
