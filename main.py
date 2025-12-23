#!/usr/bin/env python3
"""
NixNav - A GUI file navigator for NixOS/KDE Wayland
GUI version of fcd, fcat, fgrep, fnano commands

Minimal, fzf-inspired interface.
"""

import sys
import os
import json
import re
import socket
import subprocess
import zipfile
import tarfile
from pathlib import Path
from typing import Optional, List, Tuple

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QListWidget, QListWidgetItem, QLabel,
    QTextEdit, QSystemTrayIcon, QMenu, QSplitter, QPushButton,
    QComboBox, QInputDialog, QMessageBox
)
from PySide6.QtCore import Qt, QTimer, Signal, QThread, QObject
from PySide6.QtGui import QIcon, QAction, QFont, QKeySequence, QShortcut


# Config paths
CONFIG_DIR = Path.home() / ".config" / "nixnav"
CONFIG_FILE = CONFIG_DIR / "config.json"
DAEMON_SOCKET = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}") + "/nixnav-daemon.sock"


def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def find_daemon_binary() -> Optional[str]:
    """Find the nixnav-daemon binary."""
    # Check common locations
    candidates = [
        Path(__file__).parent / "daemon" / "target" / "release" / "nixnav-daemon",
        Path.home() / ".nix-profile" / "bin" / "nixnav-daemon",
        Path("/run/current-system/sw/bin/nixnav-daemon"),
    ]
    # Also check PATH
    for p in os.environ.get("PATH", "").split(":"):
        candidates.append(Path(p) / "nixnav-daemon")

    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def start_daemon() -> bool:
    """Start the daemon if not running. Returns True if daemon is available."""
    # Check if already running
    if os.path.exists(DAEMON_SOCKET):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(DAEMON_SOCKET)
            sock.sendall(b"PING\n")
            response = sock.recv(1024)
            sock.close()
            if b"pong" in response:
                return True
        except:
            pass
        # Stale socket
        try:
            os.unlink(DAEMON_SOCKET)
        except:
            pass

    # Find and start daemon
    daemon_bin = find_daemon_binary()
    if not daemon_bin:
        return False

    try:
        # Start daemon in background
        subprocess.Popen(
            [daemon_bin],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        # Wait for socket to appear
        for _ in range(50):  # 5 seconds max
            if os.path.exists(DAEMON_SOCKET):
                return True
            import time
            time.sleep(0.1)
    except:
        pass
    return False


class DaemonClient:
    """Client for communicating with nixnav-daemon."""

    def __init__(self):
        self._socket: Optional[socket.socket] = None
        self._daemon_started = False

    def connect(self) -> bool:
        """Connect to the daemon, starting it if necessary."""
        if self._socket:
            return True

        # Try to start daemon if not already attempted
        if not self._daemon_started:
            self._daemon_started = True
            start_daemon()

        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(5.0)
            self._socket.connect(DAEMON_SOCKET)
            return True
        except (socket.error, FileNotFoundError):
            self._socket = None
            return False

    def disconnect(self):
        """Disconnect from the daemon."""
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            self._socket = None

    def is_connected(self) -> bool:
        """Check if connected to daemon."""
        return self._socket is not None

    def ping(self) -> bool:
        """Check if daemon is responsive."""
        if not self.connect():
            return False
        try:
            self._socket.sendall(b"PING\n")
            response = self._socket.recv(4096).decode().strip()
            return "pong" in response
        except:
            self.disconnect()
            return False

    def search(self, bookmark_path: str, query: str, extension: Optional[str] = None) -> Tuple[List[dict], int, int]:
        """
        Search for files via the daemon.

        Returns: (results, total_indexed, search_time_ms)
        """
        if not self.connect():
            return [], 0, 0

        try:
            request = {
                "bookmark_path": bookmark_path,
                "mode": "all",  # Search all files and directories
                "query": query,
                "extension": extension,
            }
            cmd = f"SEARCH {json.dumps(request)}\n"
            self._socket.sendall(cmd.encode())

            # Read response
            response = b""
            while True:
                chunk = self._socket.recv(65536)
                if not chunk:
                    break
                response += chunk
                if b"\n" in response:
                    break

            data = json.loads(response.decode().strip())
            if "error" in data:
                return [], 0, 0

            results = data.get("results", [])
            total = data.get("total_indexed", 0)
            time_ms = data.get("search_time_ms", 0)

            return results, total, time_ms

        except Exception as e:
            self.disconnect()
            return [], 0, 0

    def add_bookmark(self, name: str, path: str) -> bool:
        """Add a bookmark to the daemon's index. Non-blocking - returns immediately."""
        if not self.connect():
            return False

        try:
            # Detect if network mount
            is_network = self._is_network_mount(path)
            bookmark = {"name": name, "path": path, "is_network": is_network}
            cmd = f"ADD_BOOKMARK {json.dumps(bookmark)}\n"

            # Use a longer timeout for scanning
            old_timeout = self._socket.gettimeout()
            self._socket.settimeout(300)  # 5 minutes for large directories

            self._socket.sendall(cmd.encode())

            response = b""
            while b"\n" not in response:
                chunk = self._socket.recv(65536)
                if not chunk:
                    break
                response += chunk

            self._socket.settimeout(old_timeout)
            data = json.loads(response.decode().strip())
            return data.get("status") == "ok"
        except Exception:
            self.disconnect()
            return False

    def rescan(self, path: str) -> int:
        """Trigger a rescan of a path. Returns number of files indexed."""
        if not self.connect():
            return 0

        try:
            cmd = f"RESCAN {path}\n"
            self._socket.sendall(cmd.encode())

            response = self._socket.recv(4096).decode().strip()
            data = json.loads(response)
            return data.get("indexed", 0)
        except:
            self.disconnect()
            return 0

    def get_stats(self) -> dict:
        """Get daemon statistics."""
        if not self.connect():
            return {"connected": False, "files": 0, "trigrams": 0, "bookmarks": 0}

        try:
            self._socket.sendall(b"STATS\n")
            response = b""
            while b"\n" not in response:
                chunk = self._socket.recv(4096)
                if not chunk:
                    break
                response += chunk
            data = json.loads(response.decode().strip())
            data["connected"] = True
            return data
        except:
            self.disconnect()
            return {"connected": False, "files": 0, "trigrams": 0, "bookmarks": 0}

    def _is_network_mount(self, path: str) -> bool:
        """Check if a path is on a network mount."""
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mount_point, fs_type = parts[1], parts[2]
                        if path.startswith(mount_point):
                            return fs_type in ("nfs", "nfs4", "cifs", "smb", "smbfs", "fuse.sshfs")
        except:
            pass
        return False


# Global daemon client instance
_daemon_client = DaemonClient()


# ============================================================================
# File Type Detection and Smart Previews
# ============================================================================

# Binary file extensions (no text preview)
BINARY_EXTENSIONS = {
    # Images
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "webp", "svg", "tiff", "tif", "raw", "psd", "xcf",
    # Compiled/executables
    "exe", "dll", "so", "dylib", "a", "o", "obj", "bin", "dat",
    # Fonts
    "ttf", "otf", "woff", "woff2", "eot",
    # Java/Python bytecode
    "class", "jar", "war", "pyc", "pyo", "whl",
    # Database
    "db", "sqlite", "sqlite3",
    # Documents (handled separately)
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
}

# Audio extensions (show ID3/codec info)
AUDIO_EXTENSIONS = {"mp3", "flac", "ogg", "m4a", "aac", "wav", "wma", "opus", "aiff"}

# Video extensions (show media info)
VIDEO_EXTENSIONS = {"mp4", "mkv", "avi", "mov", "wmv", "webm", "m4v", "flv", "ts", "mts"}

# Archive extensions (show contents)
ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "bz2", "xz", "7z", "rar", "zst", "tgz", "tbz2", "txz"}


def get_file_category(path: str) -> str:
    """Determine the category of a file based on extension."""
    ext = Path(path).suffix.lower().lstrip(".")

    # Handle compound extensions like .tar.gz
    name = Path(path).name.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "archive"
    if name.endswith(".tar.bz2") or name.endswith(".tbz2"):
        return "archive"
    if name.endswith(".tar.xz") or name.endswith(".txz"):
        return "archive"

    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in BINARY_EXTENSIONS:
        return "binary"
    return "text"


def preview_binary(path: str) -> str:
    """Generate preview for binary files."""
    try:
        p = Path(path)
        stat = p.stat()
        size = stat.st_size
        ext = p.suffix.lower().lstrip(".")

        # Format size
        if size < 1024:
            size_str = f"{size} bytes"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"

        lines = [
            "━━━ Binary File ━━━",
            "",
            f"  Name: {p.name}",
            f"  Size: {size_str}",
            f"  Type: {ext.upper() if ext else 'Unknown'}",
            "",
            "  (No text preview available for binary files)",
        ]

        # Add type-specific info
        if ext in {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "tif"}:
            lines[0] = "━━━ Image File ━━━"
            # Try to get image dimensions using file command
            try:
                result = subprocess.run(
                    ["file", path], capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    info = result.stdout.strip()
                    # Extract dimensions if present
                    import re
                    dims = re.search(r'(\d+)\s*x\s*(\d+)', info)
                    if dims:
                        lines.insert(4, f"  Dimensions: {dims.group(1)} × {dims.group(2)}")
            except:
                pass
        elif ext in {"pdf"}:
            lines[0] = "━━━ PDF Document ━━━"
        elif ext in {"doc", "docx", "odt"}:
            lines[0] = "━━━ Word Document ━━━"
        elif ext in {"xls", "xlsx", "ods"}:
            lines[0] = "━━━ Spreadsheet ━━━"
        elif ext in {"ppt", "pptx", "odp"}:
            lines[0] = "━━━ Presentation ━━━"

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading file info: {e}"


def preview_audio(path: str) -> str:
    """Generate preview for audio files with ID3 tags and codec info."""
    try:
        p = Path(path)
        stat = p.stat()
        size = stat.st_size
        ext = p.suffix.lower().lstrip(".")

        # Format size
        if size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f} MB"

        lines = [
            "━━━ Audio File ━━━",
            "",
            f"  File: {p.name}",
            f"  Size: {size_str}",
            "",
        ]

        # Try to get audio info using ffprobe
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import json as json_mod
                data = json_mod.loads(result.stdout)

                # Format info
                fmt = data.get("format", {})
                if fmt:
                    duration = float(fmt.get("duration", 0))
                    if duration > 0:
                        mins = int(duration // 60)
                        secs = int(duration % 60)
                        lines.append(f"  Duration: {mins}:{secs:02d}")

                    bitrate = int(fmt.get("bit_rate", 0))
                    if bitrate > 0:
                        lines.append(f"  Bitrate: {bitrate // 1000} kbps")

                # Audio stream info
                for stream in data.get("streams", []):
                    if stream.get("codec_type") == "audio":
                        codec = stream.get("codec_name", "unknown")
                        sample_rate = stream.get("sample_rate", "")
                        channels = stream.get("channels", 0)
                        ch_str = "Stereo" if channels == 2 else "Mono" if channels == 1 else f"{channels}ch"
                        lines.append(f"  Codec: {codec.upper()}")
                        if sample_rate:
                            lines.append(f"  Sample Rate: {int(sample_rate) // 1000} kHz")
                        lines.append(f"  Channels: {ch_str}")
                        break

                # Tags (ID3 or other metadata)
                tags = fmt.get("tags", {})
                if tags:
                    lines.append("")
                    lines.append("  ─── Tags ───")
                    tag_order = ["title", "artist", "album", "track", "genre", "date", "year"]
                    for tag in tag_order:
                        for key, val in tags.items():
                            if key.lower() == tag and val:
                                lines.append(f"  {key.title()}: {val}")
                                break
        except FileNotFoundError:
            lines.append("  (Install ffmpeg for detailed audio info)")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading audio file: {e}"


def preview_video(path: str) -> str:
    """Generate preview for video files with media info."""
    try:
        p = Path(path)
        stat = p.stat()
        size = stat.st_size

        # Format size
        if size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"

        lines = [
            "━━━ Video File ━━━",
            "",
            f"  File: {p.name}",
            f"  Size: {size_str}",
            "",
        ]

        # Try to get video info using ffprobe
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import json as json_mod
                data = json_mod.loads(result.stdout)

                # Format info
                fmt = data.get("format", {})
                if fmt:
                    duration = float(fmt.get("duration", 0))
                    if duration > 0:
                        hours = int(duration // 3600)
                        mins = int((duration % 3600) // 60)
                        secs = int(duration % 60)
                        if hours > 0:
                            lines.append(f"  Duration: {hours}:{mins:02d}:{secs:02d}")
                        else:
                            lines.append(f"  Duration: {mins}:{secs:02d}")

                    bitrate = int(fmt.get("bit_rate", 0))
                    if bitrate > 0:
                        lines.append(f"  Bitrate: {bitrate // 1000} kbps")

                # Video stream
                lines.append("")
                lines.append("  ─── Video ───")
                for stream in data.get("streams", []):
                    if stream.get("codec_type") == "video":
                        codec = stream.get("codec_name", "unknown")
                        width = stream.get("width", 0)
                        height = stream.get("height", 0)
                        fps_str = stream.get("r_frame_rate", "0/1")
                        try:
                            num, den = map(int, fps_str.split("/"))
                            fps = num / den if den else 0
                        except:
                            fps = 0

                        lines.append(f"  Codec: {codec.upper()}")
                        if width and height:
                            lines.append(f"  Resolution: {width}×{height}")
                        if fps > 0:
                            lines.append(f"  Frame Rate: {fps:.2f} fps")
                        break

                # Audio streams
                audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
                if audio_streams:
                    lines.append("")
                    lines.append("  ─── Audio ───")
                    for i, stream in enumerate(audio_streams):
                        codec = stream.get("codec_name", "unknown")
                        channels = stream.get("channels", 0)
                        lang = stream.get("tags", {}).get("language", "")
                        ch_str = "Stereo" if channels == 2 else "Mono" if channels == 1 else f"{channels}ch"
                        track = f"  Track {i+1}: {codec.upper()} ({ch_str})"
                        if lang:
                            track += f" [{lang}]"
                        lines.append(track)

                # Subtitle streams
                sub_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "subtitle"]
                if sub_streams:
                    lines.append("")
                    lines.append("  ─── Subtitles ───")
                    for stream in sub_streams:
                        codec = stream.get("codec_name", "")
                        lang = stream.get("tags", {}).get("language", "unknown")
                        title = stream.get("tags", {}).get("title", "")
                        sub = f"  {lang.upper()}"
                        if title:
                            sub += f": {title}"
                        if codec:
                            sub += f" ({codec})"
                        lines.append(sub)

        except FileNotFoundError:
            lines.append("  (Install ffmpeg for detailed video info)")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading video file: {e}"


def preview_archive(path: str) -> str:
    """Generate preview for archive files with contents listing."""
    try:
        p = Path(path)
        stat = p.stat()
        size = stat.st_size
        ext = p.suffix.lower().lstrip(".")
        name = p.name.lower()

        # Format size
        if size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"

        lines = [
            "━━━ Archive File ━━━",
            "",
            f"  File: {p.name}",
            f"  Size: {size_str}",
            "",
            "  ─── Contents ───",
        ]

        contents = []
        max_entries = 50
        total_files = 0

        # Handle different archive types
        if ext == "zip" or name.endswith(".zip"):
            try:
                with zipfile.ZipFile(path, 'r') as zf:
                    for info in zf.infolist():
                        total_files += 1
                        if len(contents) < max_entries:
                            size_kb = info.file_size / 1024
                            if size_kb >= 1024:
                                sz = f"{size_kb / 1024:.1f}M"
                            elif size_kb >= 1:
                                sz = f"{size_kb:.0f}K"
                            else:
                                sz = f"{info.file_size}B"
                            is_dir = info.filename.endswith("/")
                            prefix = "📁 " if is_dir else "   "
                            contents.append(f"  {prefix}{info.filename}" + (f" ({sz})" if not is_dir else ""))
            except zipfile.BadZipFile:
                lines.append("  (Invalid or corrupted ZIP file)")
                return "\n".join(lines)

        elif name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
            try:
                mode = "r:*"  # Auto-detect compression
                with tarfile.open(path, mode) as tf:
                    for member in tf:
                        total_files += 1
                        if len(contents) < max_entries:
                            size_kb = member.size / 1024
                            if size_kb >= 1024:
                                sz = f"{size_kb / 1024:.1f}M"
                            elif size_kb >= 1:
                                sz = f"{size_kb:.0f}K"
                            else:
                                sz = f"{member.size}B"
                            is_dir = member.isdir()
                            prefix = "📁 " if is_dir else "   "
                            contents.append(f"  {prefix}{member.name}" + (f" ({sz})" if not is_dir else ""))
            except Exception as e:
                lines.append(f"  (Error reading tar archive: {e})")
                return "\n".join(lines)

        elif ext in {"gz", "bz2", "xz", "zst"}:
            # Single-file compression
            lines[0] = "━━━ Compressed File ━━━"
            lines.append(f"  Compression: {ext.upper()}")
            # Get uncompressed name
            uncomp_name = p.stem
            lines.append(f"  Contains: {uncomp_name}")
            return "\n".join(lines)

        elif ext == "7z":
            # Try 7z command
            try:
                result = subprocess.run(
                    ["7z", "l", path], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    in_list = False
                    for line in result.stdout.split("\n"):
                        if "---" in line:
                            in_list = not in_list
                            continue
                        if in_list and line.strip():
                            total_files += 1
                            if len(contents) < max_entries:
                                # 7z list format varies, just show filenames
                                parts = line.split()
                                if len(parts) >= 6:
                                    fname = " ".join(parts[5:])
                                    contents.append(f"     {fname}")
            except FileNotFoundError:
                lines.append("  (Install p7zip for 7z support)")
                return "\n".join(lines)
            except Exception:
                pass

        elif ext == "rar":
            # Try unrar command
            try:
                result = subprocess.run(
                    ["unrar", "l", path], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    in_list = False
                    for line in result.stdout.split("\n"):
                        if "---" in line:
                            in_list = not in_list
                            continue
                        if in_list and line.strip():
                            total_files += 1
                            if len(contents) < max_entries:
                                contents.append(f"     {line.strip()}")
            except FileNotFoundError:
                lines.append("  (Install unrar for RAR support)")
                return "\n".join(lines)
            except Exception:
                pass

        # Add contents to lines
        if contents:
            lines.extend(contents)
            if total_files > max_entries:
                lines.append(f"  ... and {total_files - max_entries} more entries")
            lines.append("")
            lines.append(f"  Total: {total_files} entries")
        elif total_files == 0:
            lines.append("  (Empty archive)")

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading archive: {e}"


class Config:
    def __init__(self):
        self.data = {
            "bookmarks": [
                {"name": "home", "path": str(Path.home())},
            ],
            "last_bookmark": 0,
            "last_mode": "edit",
            "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules", "*.log", ".Trash*", "Trash"],
            "max_results": 500,
        }
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    saved = json.load(f)
                    self.data.update(saved)
            except:
                pass

    def save(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.data, f, indent=2)
        except:
            pass

    def get_bookmarks(self):
        return self.data.get("bookmarks", [])

    def add_bookmark(self, name: str, path: str):
        self.data["bookmarks"].append({"name": name, "path": path})
        self.save()

    def rename_bookmark(self, index: int, new_name: str):
        if 0 <= index < len(self.data["bookmarks"]):
            self.data["bookmarks"][index]["name"] = new_name
            self.save()

    def delete_bookmark(self, index: int):
        if 0 <= index < len(self.data["bookmarks"]):
            del self.data["bookmarks"][index]
            # Adjust last_bookmark if needed
            if self.data.get("last_bookmark", 0) >= len(self.data["bookmarks"]):
                self.data["last_bookmark"] = max(0, len(self.data["bookmarks"]) - 1)
            self.save()


class FileScanner(QObject):
    """
    Fast file scanner using nixnav-daemon (trigram index) with fallback to fd.

    The daemon provides instant search across millions of files.
    Falls back to fd if daemon is not running.
    """
    results_ready = Signal(list)
    finished = Signal()

    def __init__(self, root_path: str, query: str, exclude_patterns: list, max_results: int, ext_filter: str = None):
        super().__init__()
        self.root_path = root_path
        self.query = query
        self.exclude_patterns = exclude_patterns
        self.max_results = max_results
        self.ext_filter = ext_filter
        self._cancelled = False
        self._process = None

    def cancel(self):
        self._cancelled = True
        if self._process:
            try:
                self._process.kill()
            except:
                pass

    def run(self):
        # Try daemon first (instant search)
        if self._try_daemon_search():
            return

        # Fallback to fd
        self._fd_search()

    def _try_daemon_search(self) -> bool:
        """Try searching via daemon. Returns True if successful."""
        global _daemon_client

        try:
            results, total, time_ms = _daemon_client.search(
                bookmark_path=self.root_path,
                query=self.query,
                extension=self.ext_filter,
            )

            if self._cancelled:
                return True

            if results or _daemon_client.is_connected():
                self._daemon_search_time = time_ms
                self._daemon_total_indexed = total
                converted = [
                    (r["path"], r.get("is_dir", False), r.get("mtime", 0))
                    for r in results
                ]
                self.results_ready.emit(converted)
                self.finished.emit()
                return True

        except Exception:
            pass

        return False

    def _fd_search(self):
        """Fallback search using fd command."""
        results = []
        try:
            cmd = ["fd", "--color=never", "--absolute-path"]

            # Search both files and directories
            for pattern in self.exclude_patterns:
                cmd.extend(["--exclude", pattern])

            if self.ext_filter:
                cmd.extend(["--extension", self.ext_filter])

            cmd.extend(["--max-results", str(self.max_results)])

            if self.query:
                cmd.append(self.query)
            else:
                cmd.append(".")

            cmd.append(self.root_path)

            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            stdout, _ = self._process.communicate(timeout=30)

            if self._cancelled:
                return

            for line in stdout.strip().split('\n'):
                if not line:
                    continue
                path = line.strip()
                try:
                    p = Path(path)
                    is_dir = p.is_dir()
                    mtime = p.stat().st_mtime
                    results.append((path, is_dir, mtime))
                except:
                    results.append((path, False, 0))

        except subprocess.TimeoutExpired:
            if self._process:
                self._process.kill()
        except Exception:
            pass

        if not self._cancelled:
            self.results_ready.emit(results)
        self.finished.emit()


class NixNavWindow(QWidget):
    closed = Signal()

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self._scanner_thread: Optional[QThread] = None
        self._scanner = None
        self._results_data = []  # Store (path, is_dir, matches) for each item

        self.setWindowTitle("NixNav")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)

        # Restore window size from config or use defaults
        geo = self.config.data.get("window_geometry")
        if geo and isinstance(geo, str):
            from PySide6.QtCore import QByteArray
            # restoreGeometry handles size; position won't work on Wayland
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1000, 650)

        self.setup_ui()
        self.setup_shortcuts()

    def closeEvent(self, event):
        """Clean up threads and save position on close."""
        self._cancel_scan()
        # Save window geometry (as base64 bytes for Qt's native format)
        self.config.data["window_geometry"] = self.saveGeometry().toBase64().data().decode()
        self.config.save()
        super().closeEvent(event)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top bar: bookmark selector
        top = QHBoxLayout()
        top.setSpacing(8)

        # Bookmark dropdown with context menu for rename/delete
        self.bookmark_combo = QComboBox()
        self.bookmark_combo.setStyleSheet("""
            QComboBox { background: #252525; color: #ccc; border: 1px solid #444; padding: 4px 8px; border-radius: 3px; min-width: 140px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #252525; color: #ccc; selection-background-color: #5c9ae6; }
        """)
        self.bookmark_combo.currentIndexChanged.connect(self._on_bookmark_changed)
        self.bookmark_combo.setContextMenuPolicy(Qt.CustomContextMenu)
        self.bookmark_combo.customContextMenuRequested.connect(self._show_bookmark_context_menu)
        top.addWidget(self.bookmark_combo)

        # Add bookmark button
        add_btn = QPushButton("+")
        add_btn.setFixedWidth(28)
        add_btn.setToolTip("Add bookmark")
        add_btn.setStyleSheet("QPushButton { background: #333; color: #888; border: none; padding: 4px; border-radius: 3px; } QPushButton:hover { background: #444; }")
        add_btn.clicked.connect(self._add_bookmark)
        top.addWidget(add_btn)

        top.addStretch()

        # Status
        self.status = QLabel("0")
        self.status.setStyleSheet("color: #666; font-size: 11px;")
        top.addWidget(self.status)

        layout.addLayout(top)

        # Search input
        self.search = QLineEdit()
        self.search.setPlaceholderText("> Search...")
        self.search.setStyleSheet("""
            QLineEdit { background: #1e1e1e; color: #fff; border: 1px solid #444; border-radius: 3px; padding: 8px 12px; font-size: 14px; font-family: monospace; }
            QLineEdit:focus { border-color: #5c9ae6; }
        """)
        self.search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search)

        # Content: list + preview
        self.splitter = QSplitter(Qt.Horizontal)

        # Results list - compact
        self.list = QListWidget()
        self.list.setStyleSheet("""
            QListWidget { background: #1a1a1a; color: #ccc; border: none; font-family: monospace; font-size: 12px; }
            QListWidget::item { padding: 3px 6px; border-bottom: 1px solid #252525; }
            QListWidget::item:selected { background: #2a4a6a; color: #fff; }
            QListWidget::item:hover:!selected { background: #252525; }
        """)
        self.list.currentRowChanged.connect(self._on_selection_changed)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.splitter.addWidget(self.list)

        # Preview
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet("""
            QTextEdit { background: #1e1e1e; color: #bbb; border: none; font-size: 11px; font-family: monospace; padding: 8px; }
        """)
        self.splitter.addWidget(self.preview)

        self.splitter.setSizes([500, 500])
        layout.addWidget(self.splitter, 1)

        # Help line
        help_lbl = QLabel("Enter: Open | Ctrl+O: Open folder | Ctrl+R: Refresh index | Esc: Close")
        help_lbl.setStyleSheet("color: #444; font-size: 10px;")
        help_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(help_lbl)

        # Window styling
        self.setStyleSheet("NixNavWindow { background: #141414; }")

    def setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(self._open_folder)
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self._rescan_bookmark)

    def showEvent(self, event):
        super().showEvent(event)
        self._load_bookmarks()
        self.search.setFocus()
        # Sync bookmarks to daemon (in background)
        self._sync_bookmarks_to_daemon()
        self._refresh()

    def _sync_bookmarks_to_daemon(self):
        """Ensure all bookmarks are indexed by the daemon (runs in background thread)."""
        import threading

        bookmarks = self.config.get_bookmarks().copy()

        def sync():
            # Use separate connection for background sync
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(300)
                sock.connect(DAEMON_SOCKET)

                for bm in bookmarks:
                    is_network = bm["path"].startswith("/mnt/")
                    bookmark = {"name": bm["name"], "path": bm["path"], "is_network": is_network}
                    cmd = f"ADD_BOOKMARK {json.dumps(bookmark)}\n"
                    sock.sendall(cmd.encode())
                    # Wait for response
                    response = b""
                    while b"\n" not in response:
                        chunk = sock.recv(65536)
                        if not chunk:
                            break
                        response += chunk

                sock.close()
            except Exception:
                pass

        thread = threading.Thread(target=sync, daemon=True)
        thread.start()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel_scan()
            self.close()
            self.closed.emit()
        elif event.key() == Qt.Key_Down:
            row = self.list.currentRow()
            if row < self.list.count() - 1:
                self.list.setCurrentRow(row + 1)
        elif event.key() == Qt.Key_Up:
            row = self.list.currentRow()
            if row > 0:
                self.list.setCurrentRow(row - 1)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._open_selected()
        elif event.text() and event.text().isprintable():
            # Forward printable keys to search field (type anywhere to search)
            if not self.search.hasFocus():
                self.search.setFocus()
                self.search.setText(self.search.text() + event.text())
                self.search.setCursorPosition(len(self.search.text()))
        else:
            super().keyPressEvent(event)

    def _load_bookmarks(self):
        self.bookmark_combo.blockSignals(True)
        self.bookmark_combo.clear()
        for bm in self.config.get_bookmarks():
            self.bookmark_combo.addItem(bm["name"], bm["path"])
        idx = self.config.data.get("last_bookmark", 0)
        if 0 <= idx < self.bookmark_combo.count():
            self.bookmark_combo.setCurrentIndex(idx)
        self.bookmark_combo.blockSignals(False)

    def _on_bookmark_changed(self, idx):
        self.config.data["last_bookmark"] = idx
        self.config.save()
        self._refresh()

    def _add_bookmark(self):
        path, ok = QInputDialog.getText(self, "Add Bookmark", "Directory path:", text=self._get_path())
        if ok and path:
            p = Path(path).expanduser()
            if p.is_dir():
                name, ok = QInputDialog.getText(self, "Bookmark Name", "Name:", text=p.name)
                if ok and name:
                    self.config.add_bookmark(name, str(p))
                    self._load_bookmarks()
                    self.bookmark_combo.setCurrentIndex(self.bookmark_combo.count() - 1)
                    # Tell daemon to index this path
                    _daemon_client.add_bookmark(name, str(p))
            else:
                QMessageBox.warning(self, "Error", f"'{path}' is not a directory")

    def _show_bookmark_context_menu(self, pos):
        idx = self.bookmark_combo.currentIndex()
        if idx < 0:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #252525; color: #ccc; border: 1px solid #444; }
            QMenu::item { padding: 6px 20px; }
            QMenu::item:selected { background: #5c9ae6; }
        """)

        rename_action = QAction("Rename", self)
        rename_action.triggered.connect(lambda: self._rename_bookmark(idx))
        menu.addAction(rename_action)

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self._delete_bookmark(idx))
        menu.addAction(delete_action)

        menu.exec(self.bookmark_combo.mapToGlobal(pos))

    def _rename_bookmark(self, idx: int):
        bookmarks = self.config.get_bookmarks()
        if 0 <= idx < len(bookmarks):
            current_name = bookmarks[idx]["name"]
            new_name, ok = QInputDialog.getText(self, "Rename Bookmark", "New name:", text=current_name)
            if ok and new_name and new_name != current_name:
                self.config.rename_bookmark(idx, new_name)
                self._load_bookmarks()

    def _delete_bookmark(self, idx: int):
        bookmarks = self.config.get_bookmarks()
        if len(bookmarks) <= 1:
            QMessageBox.warning(self, "Cannot Delete", "You must have at least one bookmark.")
            return
        if 0 <= idx < len(bookmarks):
            name = bookmarks[idx]["name"]
            reply = QMessageBox.question(self, "Delete Bookmark", f"Delete bookmark '{name}'?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.config.delete_bookmark(idx)
                self._load_bookmarks()
                self._refresh()

    def _get_path(self) -> str:
        idx = self.bookmark_combo.currentIndex()
        return self.bookmark_combo.itemData(idx) if idx >= 0 else str(Path.home())

    def _parse_query(self, text: str):
        """Parse query for bookmark prefix and extension filter.

        Examples:
            "home: foo" -> bookmark="home", query="foo", ext=None
            "data: *.md bar" -> bookmark="data", query="bar", ext="md"
            "*.py test" -> bookmark=None, query="test", ext="py"
            "simple query" -> bookmark=None, query="simple query", ext=None
        """
        text = text.strip()
        bookmark_name = None
        ext_filter = None
        query = text

        # Check for bookmark prefix (e.g., "home: query")
        if ": " in text:
            prefix, rest = text.split(": ", 1)
            # Check if prefix matches a bookmark name (case-insensitive)
            for i, bm in enumerate(self.config.get_bookmarks()):
                if bm["name"].lower() == prefix.lower():
                    bookmark_name = prefix
                    query = rest.strip()
                    break

        # Check for extension filter (e.g., "*.md" or "*.py")
        ext_match = re.search(r'\*\.(\w+)', query)
        if ext_match:
            ext_filter = ext_match.group(1)
            query = re.sub(r'\*\.\w+\s*', '', query).strip()

        return bookmark_name, query, ext_filter

    def _on_search_changed(self, text: str):
        # Check for bookmark prefix and switch if found
        bookmark_name, _, _ = self._parse_query(text)
        if bookmark_name:
            for i in range(self.bookmark_combo.count()):
                if self.bookmark_combo.itemText(i).lower() == bookmark_name.lower():
                    if self.bookmark_combo.currentIndex() != i:
                        self.bookmark_combo.blockSignals(True)
                        self.bookmark_combo.setCurrentIndex(i)
                        self.bookmark_combo.blockSignals(False)
                    break

        if hasattr(self, '_timer'):
            self._timer.stop()
        else:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._refresh)
        self._timer.start(100)

    def _cancel_scan(self):
        if self._scanner:
            self._scanner.cancel()
        if self._scanner_thread:
            if self._scanner_thread.isRunning():
                self._scanner_thread.quit()
                self._scanner_thread.wait(2000)
            self._scanner_thread.deleteLater()
            self._scanner_thread = None
        if self._scanner:
            self._scanner.deleteLater()
            self._scanner = None

    def _refresh(self):
        self._cancel_scan()
        # Don't clear list/preview here - wait until results arrive to avoid flash

        raw_query = self.search.text().strip()
        _, query, ext_filter = self._parse_query(raw_query)
        root = self._get_path()

        self.status.setText("...")
        self._start_scan(root, query, ext_filter)

    def _start_scan(self, root: str, query: str, ext_filter: str = None):
        self._scanner_thread = QThread()
        self._scanner = FileScanner(root, query,
                                    self.config.data.get("exclude_patterns", []),
                                    self.config.data.get("max_results", 500),
                                    ext_filter)
        self._scanner.moveToThread(self._scanner_thread)
        self._scanner_thread.started.connect(self._scanner.run)
        self._scanner.results_ready.connect(self._on_file_results)
        self._scanner.finished.connect(self._scanner_thread.quit)
        self._scanner_thread.start()

    def _on_file_results(self, results: list):
        results.sort(key=lambda x: x[2], reverse=True)  # Sort by mtime
        root = self._get_path()

        # Batch updates to prevent visual flash
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        self._results_data.clear()

        for path, is_dir, mtime in results:
            # Show relative path if under root
            try:
                rel = str(Path(path).relative_to(root))
            except:
                rel = path
            item = QListWidgetItem(rel)
            self.list.addItem(item)
            self._results_data.append((path, is_dir, None))

        self.list.setUpdatesEnabled(True)

        # Show result count and search time if available
        status_text = str(len(results))
        if self._scanner and hasattr(self._scanner, '_daemon_search_time'):
            time_ms = self._scanner._daemon_search_time
            total = getattr(self._scanner, '_daemon_total_indexed', 0)
            if total > 0:
                status_text = f"{len(results)} ({time_ms}ms, {total:,} indexed)"
        self.status.setText(status_text)

        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _on_selection_changed(self, row: int):
        if 0 <= row < len(self._results_data):
            path, is_dir, matches = self._results_data[row]
            self._show_preview(path, is_dir, matches)

    def _show_preview(self, path: str, is_dir: bool, matches: list):
        p = Path(path)
        if is_dir:
            try:
                items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                lines = ["📁 " + i.name if i.is_dir() else "   " + i.name for i in items[:80]]
                self.preview.setPlainText("\n".join(lines) if lines else "(empty)")
            except Exception as e:
                self.preview.setPlainText(f"Error: {e}")
        else:
            # Use smart preview based on file type
            category = get_file_category(path)

            if category == "audio":
                self.preview.setPlainText(preview_audio(path))
            elif category == "video":
                self.preview.setPlainText(preview_video(path))
            elif category == "archive":
                self.preview.setPlainText(preview_archive(path))
            elif category == "binary":
                self.preview.setPlainText(preview_binary(path))
            else:
                # Text file - show contents
                try:
                    with open(path, 'r', errors='replace') as f:
                        content = f.read(50000)
                        if len(content) >= 50000:
                            content += "\n\n... (truncated)"
                        self.preview.setPlainText(content)
                except Exception as e:
                    self.preview.setPlainText(f"Error: {e}")

    def _on_double_click(self, item):
        self._open_selected()

    def _open_selected(self):
        row = self.list.currentRow()
        if 0 <= row < len(self._results_data):
            path, is_dir, _ = self._results_data[row]
            try:
                if is_dir:
                    # Open folder in Dolphin
                    subprocess.Popen(["dolphin", path], start_new_session=True)
                else:
                    # Open file with default application
                    subprocess.Popen(["xdg-open", path], start_new_session=True)
                self.close()
                self.closed.emit()
            except Exception as e:
                self.status.setText(f"Error: {e}")

    def _open_folder(self):
        row = self.list.currentRow()
        if 0 <= row < len(self._results_data):
            path, _, _ = self._results_data[row]
            try:
                subprocess.Popen(["dolphin", "--select", path], start_new_session=True)
                self.close()
                self.closed.emit()
            except Exception as e:
                self.status.setText(f"Error: {e}")

    def _rescan_bookmark(self):
        """Rescan current bookmark's directory with real-time progress."""
        import threading

        path = self._get_path()
        self.status.setText("Rescanning...")
        self.status.setStyleSheet("color: #e6a855; font-size: 11px;")  # Orange during scan

        def do_rescan():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(600)  # 10 min timeout for large directories
                sock.connect(DAEMON_SOCKET)
                sock.sendall(f"RESCAN {path}\n".encode())

                response = b""
                while b"\n" not in response:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    response += chunk

                sock.close()

                data = json.loads(response.decode().strip())
                indexed = data.get("indexed", 0)

                # Update UI from main thread
                QTimer.singleShot(0, lambda: self._on_rescan_complete(indexed))

            except Exception as e:
                QTimer.singleShot(0, lambda: self._on_rescan_error(str(e)))

        thread = threading.Thread(target=do_rescan, daemon=True)
        thread.start()

    def _on_rescan_complete(self, indexed: int):
        """Called when rescan completes."""
        self.status.setStyleSheet("color: #66bb6a; font-size: 11px;")  # Green on success
        self.status.setText(f"Rescanned: {indexed:,} files")
        # Reset status color after 2 seconds and refresh results
        QTimer.singleShot(2000, lambda: self.status.setStyleSheet("color: #666; font-size: 11px;"))
        QTimer.singleShot(100, self._refresh)

    def _on_rescan_error(self, error: str):
        """Called when rescan fails."""
        self.status.setStyleSheet("color: #ef5350; font-size: 11px;")  # Red on error
        self.status.setText(f"Rescan failed: {error}")
        QTimer.singleShot(3000, lambda: self.status.setStyleSheet("color: #666; font-size: 11px;"))


def get_socket_path() -> str:
    """Get consistent socket path in XDG_RUNTIME_DIR."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return os.path.join(runtime_dir, "nixnav.sock")


def send_toggle_to_existing() -> bool:
    """Try to send toggle signal to existing instance via Unix socket."""
    import socket
    sock_path = get_socket_path()

    if not os.path.exists(sock_path):
        return False

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(sock_path)
        sock.send(b"toggle")
        sock.close()
        return True
    except:
        # Socket exists but can't connect - stale, remove it
        try:
            os.unlink(sock_path)
        except:
            pass
        return False


class NixNavApp(QObject):
    def __init__(self):
        super().__init__()
        self.app = QApplication(sys.argv)
        self.app.setApplicationName("NixNav")
        self.app.setQuitOnLastWindowClosed(False)

        # Setup IPC server for single instance
        self._setup_ipc_server()

        ensure_dirs()
        self.config = Config()

        self.window = NixNavWindow(self.config)
        self.window.closed.connect(self._on_closed)

        self.setup_tray()

    def _setup_ipc_server(self):
        """Setup Unix socket server for IPC."""
        import socket as sock_module
        self.sock_path = get_socket_path()
        self.ipc_socket = None

        # Remove stale socket
        try:
            os.unlink(self.sock_path)
        except:
            pass

        try:
            self.ipc_socket = sock_module.socket(sock_module.AF_UNIX, sock_module.SOCK_STREAM)
            self.ipc_socket.setblocking(False)
            self.ipc_socket.bind(self.sock_path)
            self.ipc_socket.listen(1)

            # Use QTimer to poll for connections
            self.ipc_timer = QTimer()
            self.ipc_timer.timeout.connect(self._check_ipc)
            self.ipc_timer.start(100)  # Check every 100ms
        except Exception as e:
            print(f"IPC setup failed: {e}")
            self.ipc_socket = None

    def _check_ipc(self):
        """Check for incoming IPC messages."""
        if not self.ipc_socket:
            return
        try:
            conn, _ = self.ipc_socket.accept()
            data = conn.recv(64).decode()
            conn.close()
            if data == "toggle":
                self.toggle_window()
        except BlockingIOError:
            pass  # No connection waiting
        except:
            pass

    def setup_tray(self):
        self.tray = QSystemTrayIcon()
        icon = QIcon.fromTheme("folder-open", QIcon.fromTheme("system-file-manager"))
        self.tray.setIcon(icon)
        self.tray.setToolTip("NixNav")

        self.tray_menu = QMenu()
        open_action = QAction("Open NixNav", self.tray_menu)
        open_action.triggered.connect(self.show_window)
        self.tray_menu.addAction(open_action)
        self.tray_menu.addSeparator()

        quit_action = QAction("Quit", self.tray_menu)
        quit_action.triggered.connect(self.quit)
        self.tray_menu.addAction(quit_action)

        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show_window()

    def show_window(self):
        # Restore window size (position is handled by centering on Wayland)
        geo = self.config.data.get("window_geometry")
        if geo and isinstance(geo, str):
            from PySide6.QtCore import QByteArray
            self.window.restoreGeometry(QByteArray.fromBase64(geo.encode()))

        # Center on screen (works reliably on Wayland, predictable UX)
        screen = self.app.primaryScreen()
        if screen:
            screen_geo = screen.availableGeometry()
            x = screen_geo.x() + (screen_geo.width() - self.window.width()) // 2
            y = screen_geo.y() + (screen_geo.height() - self.window.height()) // 2
            self.window.move(x, y)

        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.search.setFocus()

    def toggle_window(self):
        """Toggle window visibility."""
        if self.window.isVisible():
            self.window.close()
        else:
            self.show_window()

    def _on_closed(self):
        pass

    def quit(self):
        # Stop IPC
        if hasattr(self, 'ipc_timer'):
            self.ipc_timer.stop()
        if hasattr(self, 'ipc_socket') and self.ipc_socket:
            try:
                self.ipc_socket.close()
            except:
                pass
        if hasattr(self, 'sock_path'):
            try:
                os.unlink(self.sock_path)
            except:
                pass

        self.window._cancel_scan()
        self.window.close()
        self.tray.hide()
        self.config.save()
        self.app.quit()

    def run(self):
        return self.app.exec()


def main():
    # Check for --toggle flag BEFORE starting Qt
    if "--toggle" in sys.argv:
        if send_toggle_to_existing():
            sys.exit(0)
        # No existing instance - start new one and show it

    app = NixNavApp()
    app.show_window()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
