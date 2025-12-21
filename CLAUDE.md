# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

NixNav is a GUI file navigator for NixOS/KDE Wayland built with Python and PySide6 (Qt6). It's the GUI equivalent of the `fcd`, `fcat` terminal commands defined in `/etc/nixos/modules/core/dev/fzf.nix`.

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
- **FileScanner(QObject)**: Background thread using `fd` for file/folder scanning (filters binary files in edit mode)
- **NixNavWindow(QWidget)**: Main window with search, results list, and preview panel
- **NixNavApp**: Application controller managing system tray, window, and IPC socket

### Why fd?

Initial implementation used Python's `pathlib.rglob()` which was slow (~2-5s for large directories). Switched to subprocess calls to `fd` (Rust binary) for 50-100x speedup.

### IPC Toggle Mechanism

The app creates a Unix socket at `$XDG_RUNTIME_DIR/nixnav.sock` for instant window toggling:

1. **Server side** (`main.py`): Timer polls socket, emits Qt signal on "toggle" message
2. **Client side** (`nixnav-toggle`): Minimal Python script sends "toggle" to socket, exits immediately
3. **Why not QLocalSocket?** Direct Unix sockets avoid Qt overhead for sub-50ms response time

## Key Features

- **Three modes**: Edit, File, Dir - switchable via Tab or mode buttons
- **Edit mode**: Filters out binary files (images, audio, compiled, etc.) for clean text-file search
- **Bookmarks**: Configurable directories to search within
- **Live preview**: Right panel shows file contents as you navigate
- **Background scanning**: File search runs in separate thread to keep UI responsive
- **Wayland native**: Uses `QT_QPA_PLATFORM=wayland;xcb` - window centers on screen each toggle
- **No flash on search**: Uses `setUpdatesEnabled()` to batch UI updates atomically
- **Window size remembered**: Size persists between sessions (position centers on Wayland)
- **System tray**: Left-click opens overlay, right-click for menu
- **Keyboard-centric**: Arrow keys navigate, Enter opens, Tab cycles modes, Esc closes

## Keyboard Shortcuts

- `Arrow Up/Down` - Navigate results
- `Enter` - Execute mode action
- `Ctrl+O` - Open containing folder in Dolphin
- `Tab` - Cycle through modes (Edit → File → Dir)
- `Esc` - Close overlay

## Modes and Actions

| Mode | Button | Searches | Enter Action |
|------|--------|----------|--------------|
| Edit | `Edit` | Text files only | Open file in Kate |
| File | `File` | All files | Open containing folder in Dolphin |
| Dir | `Dir` | Directories | Open folder in Dolphin |

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
  "last_mode": "edit",
  "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules", "*.log"],
  "max_results": 500,
  "window_geometry": "<base64 encoded Qt geometry>"
}
```

## Binary File Filtering (Edit Mode)

Edit mode excludes these file types for a cleaner editing experience:
- Images: png, jpg, jpeg, gif, bmp, ico, webp, svg
- Audio/Video: mp3, mp4, wav, avi, mkv, mov, flac, ogg
- Documents: pdf, doc, docx, xls, xlsx, ppt, pptx
- Archives: zip, tar, gz, bz2, xz, 7z, rar
- Compiled: exe, dll, so, dylib, a, o, obj, class, jar, pyc
- Fonts: ttf, otf, woff, woff2, eot
- Data: bin, dat, db, sqlite, sqlite3
- Minified: min.js, min.css

## Wayland Considerations

Wayland does not allow applications to set their own window positions (security feature). NixNav handles this by:
- Saving/restoring window **size** between sessions
- **Centering** the window on the primary screen each time it's shown
- This provides predictable, consistent UX similar to KRunner

## Comparison to Terminal Commands

| Terminal | NixNav Mode | Action |
|----------|-------------|--------|
| `fcd` | Dir | Opens selected folder in Dolphin |
| `fcat` | Edit | Preview panel shows contents, Enter opens in Kate |

## Known Behaviors

- **Max results**: Limited to 500 items to keep performance snappy
- **Exclude patterns**: Skips .git, node_modules, __pycache__ etc by default
- **Thread cleanup**: Uses `deleteLater()` to prevent Qt object lifetime crashes
