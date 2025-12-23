# Architecture

## Overview

NixNav is a two-component system designed for instant file search across large filesystems:

1. **nixnav-daemon** (Rust) - High-performance indexing daemon
2. **main.py** (Python/Qt) - GUI application

The architecture prioritizes:
- **Instant response** - Trigram index enables sub-10ms search
- **Minimal latency** - Unix sockets for IPC, SQLite for persistence
- **Single instance** - System tray + socket toggle prevents multiple windows
- **Real-time updates** - inotify + integrity checker catches all changes

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              nixnav-daemon (Rust)                            │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                 │
│  │ TrigramIndex   │  │    SQLite      │  │   inotify      │                 │
│  │                │  │   Database     │  │   Watcher      │                 │
│  │ trigrams: Map  │  │                │  │                │                 │
│  │ files: Map     │  │ ~/.local/share │  │ Real-time      │                 │
│  │ path_to_id     │  │ /nixnav/       │  │ file changes   │                 │
│  └────────────────┘  │ index.db       │  └────────────────┘                 │
│         │            └────────────────┘          │                          │
│         │                    │                   │                          │
│         ▼                    ▼                   ▼                          │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │                    Unix Socket Server                        │           │
│  │              /run/user/1000/nixnav-daemon.sock              │           │
│  └─────────────────────────────────────────────────────────────┘           │
│                              │                                              │
│  ┌────────────────┐          │                                              │
│  │ IntegrityCheck │          │  Periodic check for deleted files           │
│  │ (every 60s)    │──────────┘  (catches bulk deletes missed by inotify)   │
│  └────────────────┘                                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                               │
                               │ SEARCH / RESCAN / STATS commands
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              main.py (Python/Qt)                             │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────────────┐ │
│  │  DaemonClient  │  │ SocketListener │  │        NixNavWindow            │ │
│  │                │  │   (QThread)    │  │  ┌──────────┐ ┌─────────────┐  │ │
│  │ Connects to    │  │                │  │  │ Search   │ │  Results    │  │ │
│  │ daemon socket  │  │ Toggle socket  │  │  │ Bar      │ │  List       │  │ │
│  │                │  │ /run/user/     │  │  └──────────┘ └─────────────┘  │ │
│  │                │  │ 1000/nixnav    │  │  ┌──────────────────────────┐  │ │
│  │                │  │ .sock          │  │  │    Smart Preview Panel   │  │ │
│  └────────────────┘  └────────────────┘  │  │  (text/media/archive)    │  │ │
│                                          │  └──────────────────────────┘  │ │
│  ┌────────────────┐                      └────────────────────────────────┘ │
│  │ QSystemTray    │                                                         │
│  │ Left=toggle    │                                                         │
│  │ Right=menu     │                                                         │
│  └────────────────┘                                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Daemon Architecture

### Trigram Index

The daemon uses trigram posting lists for instant substring search:

```
Query: "test"
Trigrams: ["tes", "est"]

Posting lists:
  "tes" -> {file_id_1, file_id_5, file_id_99, ...}
  "est" -> {file_id_1, file_id_12, file_id_99, ...}

Intersection: {file_id_1, file_id_99}  <- candidates
Final filter: verify actual substring match
```

This approach (used by plocate, Google Code Search) enables O(1) candidate lookup regardless of index size.

### Threading Model

```
Main Thread
    │
    ├── IPC Server (blocking accept loop)
    │       └── spawn thread per client
    │
    ├── inotify Watcher (notify crate)
    │       └── handle_fs_event() on file changes
    │
    ├── Integrity Checker (every 60s)
    │       └── verify 5000 files still exist
    │
    ├── Network Scanner (every 5min)
    │       └── rescan network mounts (no inotify)
    │
    └── Database Thread (channel-based)
            └── serialize all SQLite writes
```

### Database Schema

```sql
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    is_dir INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL
);

CREATE TABLE bookmarks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT UNIQUE NOT NULL,
    is_network INTEGER NOT NULL,
    last_scan INTEGER
);
```

## IPC Protocol

### GUI Toggle Socket
```
Path: $XDG_RUNTIME_DIR/nixnav.sock
Protocol: Raw bytes
Command: "toggle" -> Show/hide window
```

### Daemon Socket
```
Path: $XDG_RUNTIME_DIR/nixnav-daemon.sock
Protocol: Newline-delimited JSON

Commands:
  PING
    -> {"status": "pong"}

  STATS
    -> {"files": 594028, "trigrams": 45000, "bookmarks": 3}

  SEARCH {"bookmark_path": "/home/user", "mode": "all", "query": "test", "extension": null}
    -> {"results": [...], "total_indexed": 594028, "search_time_ms": 7}

  RESCAN /path/to/rescan
    -> {"status": "ok", "indexed": 12345}

  ADD_BOOKMARK {"name": "data", "path": "/mnt/data", "is_network": true}
    -> {"status": "ok", "indexed": 50000}
```

## Smart Preview System

The GUI detects file type by extension and shows appropriate preview:

```python
AUDIO_EXTENSIONS = {"mp3", "flac", "ogg", "m4a", ...}
VIDEO_EXTENSIONS = {"mp4", "mkv", "avi", "mov", ...}
ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "7z", ...}
BINARY_EXTENSIONS = {"png", "jpg", "pdf", "exe", ...}

def get_file_category(path):
    if ext in AUDIO_EXTENSIONS: return "audio"
    if ext in VIDEO_EXTENSIONS: return "video"
    if ext in ARCHIVE_EXTENSIONS: return "archive"
    if ext in BINARY_EXTENSIONS: return "binary"
    return "text"
```

Preview handlers:
- **text**: Read first 50KB of file
- **binary**: Show file info (name, size, type, dimensions for images)
- **audio**: Run `ffprobe` to extract ID3 tags, duration, codec
- **video**: Run `ffprobe` to extract resolution, duration, audio tracks, subtitles
- **archive**: Use Python `zipfile`/`tarfile` or shell commands for listing

## Startup Sequence

1. User launches `main.py` or clicks system tray
2. `DaemonClient.connect()` checks for running daemon
3. If no daemon: `start_daemon()` spawns `nixnav-daemon`
4. Daemon loads index from SQLite (~5s for 600k files)
5. Daemon starts inotify watchers
6. GUI syncs bookmarks to daemon (ADD_BOOKMARK for each)
7. User searches -> SEARCH command -> instant results

## Shutdown Sequence

1. User closes window or clicks "Quit" in tray
2. `NixNavApp.quit()` called
3. Cancel any running scanners
4. Stop socket listener thread
5. Remove GUI socket file
6. `QApplication.quit()`
7. Daemon continues running (for next launch)

## Exclude Patterns

Both daemon and GUI exclude common non-searchable directories:

```
.git, node_modules, __pycache__, .cache, .npm, .cargo
target, build, dist, .next, .nuxt
.Trash*, Trash (all trash folders)
```
