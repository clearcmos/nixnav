# Architecture

## Overview

NixNav is a single-file PySide6 application designed for speed and simplicity. The architecture prioritizes:

1. **Instant response** - UI never blocks, all I/O in background threads
2. **Minimal latency** - Rust tools (fd, rg) for search, Unix sockets for IPC
3. **Single instance** - System tray + socket toggle prevents multiple windows

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         NixNavApp                                │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ QSystemTray  │  │SocketListener│  │     NixNavWindow       │ │
│  │              │  │   (QThread)  │  │  ┌─────┐ ┌──────────┐  │ │
│  │ Left-click   │  │              │  │  │Search│ │ Results  │  │ │
│  │ = toggle     │  │ Unix socket  │  │  │ Bar  │ │  List    │  │ │
│  │              │  │ /run/user/   │  │  └─────┘ └──────────┘  │ │
│  │ Right-click  │  │ 1000/nixnav  │  │  ┌──────────────────┐  │ │
│  │ = menu       │  │ .sock        │  │  │  Preview Panel   │  │ │
│  └──────────────┘  └──────────────┘  │  └──────────────────┘  │ │
└─────────────────────────────────────────────────────────────────┘
         │                   │                    │
         │                   │                    │
         ▼                   ▼                    ▼
   Show/Hide          "toggle" msg         FileScanner / GrepScanner
    Window            from client             (QThread workers)
                                                    │
                                                    ▼
                                            ┌──────────────┐
                                            │  fd / rg     │
                                            │  (subprocess)│
                                            └──────────────┘
```

## Threading Model

### Main Thread (Qt Event Loop)
- UI rendering and user input
- Signal/slot connections
- Window show/hide animations

### SocketListener Thread
- Blocks on `socket.accept()` waiting for toggle commands
- Emits `toggle_received` signal to main thread
- Cleans up socket file on shutdown

### FileScanner / GrepScanner Threads
- Spawned per-search, cancelled on new query
- Run fd/rg subprocess, parse stdout line by line
- Emit `result_found` signals incrementally (not batched)
- Check `_cancelled` flag between results for responsiveness

## IPC Protocol

### Socket Path
```
$XDG_RUNTIME_DIR/nixnav.sock
# e.g., /run/user/1000/nixnav.sock
```

### Messages
| Command | Response | Effect |
|---------|----------|--------|
| `toggle` | (none) | Show window if hidden, hide if visible |

### Client Implementation (nixnav-toggle)
```python
import socket, os
sock_path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "nixnav.sock")
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
s.send(b"toggle")
s.close()
```

## Search Implementation

### File/Folder Search (fd)
```bash
fd --type f --hidden --follow --exclude .git "$query" "$bookmark_path"
fd --type d --hidden --follow --exclude .git "$query" "$bookmark_path"
```

### Content Search (ripgrep)
```bash
rg --line-number --with-filename --max-count 3 "$query" "$bookmark_path"
```

Output parsed as: `filepath:line_number:matched_text`

## Configuration

### File Location
```
~/.config/nixnav/config.json
```

### Schema
```json
{
  "bookmarks": [
    {"name": "Display Name", "path": "/absolute/path"}
  ],
  "last_bookmark": 0,
  "last_mode": "files",
  "exclude_patterns": ["*.pyc", ".git", "node_modules"],
  "max_preview_lines": 200,
  "max_results": 500
}
```

## Shutdown Sequence

1. User closes window or clicks "Quit" in tray
2. `NixNavApp.quit()` called
3. Cancel any running scanners (`_cancel_scan()`)
4. Stop socket listener thread
5. Remove socket file
6. `QApplication.quit()`

Critical: Socket cleanup prevents stale socket files that would block next launch.
