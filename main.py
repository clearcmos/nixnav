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
import subprocess
from pathlib import Path
from typing import Optional, List

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


def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


class Config:
    def __init__(self):
        self.data = {
            "bookmarks": [
                {"name": "home", "path": str(Path.home())},
            ],
            "last_bookmark": 0,
            "last_mode": "edit",
            "exclude_patterns": ["*.pyc", "__pycache__", ".git", "node_modules", "*.log"],
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
    """Fast file scanner using fd (Rust-based find)."""
    results_ready = Signal(list)
    finished = Signal()

    # Binary/non-editable file extensions to exclude in edit mode
    BINARY_EXTENSIONS = [
        "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.ico", "*.webp", "*.svg",
        "*.mp3", "*.mp4", "*.wav", "*.avi", "*.mkv", "*.mov", "*.flac", "*.ogg",
        "*.pdf", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx",
        "*.zip", "*.tar", "*.gz", "*.bz2", "*.xz", "*.7z", "*.rar",
        "*.exe", "*.dll", "*.so", "*.dylib", "*.a", "*.o", "*.obj",
        "*.bin", "*.dat", "*.db", "*.sqlite", "*.sqlite3",
        "*.ttf", "*.otf", "*.woff", "*.woff2", "*.eot",
        "*.class", "*.jar", "*.war", "*.pyc", "*.pyo", "*.whl",
        "*.min.js", "*.min.css",  # Minified files aren't pleasant to edit
    ]

    def __init__(self, root_path: str, mode: str, query: str, exclude_patterns: list, max_results: int, ext_filter: str = None):
        super().__init__()
        self.root_path = root_path
        self.mode = mode
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
        results = []
        try:
            # Build fd command
            cmd = ["fd", "--color=never", "--absolute-path"]

            # Type filter
            if self.mode == "gotodir":
                cmd.extend(["--type", "d"])
            else:  # edit or gotofile - search files
                cmd.extend(["--type", "f"])

            # Exclude patterns
            for pattern in self.exclude_patterns:
                cmd.extend(["--exclude", pattern])

            # For edit mode, also exclude binary files
            if self.mode == "edit":
                for pattern in self.BINARY_EXTENSIONS:
                    cmd.extend(["--exclude", pattern])

            # Extension filter (e.g., "*.md" -> only .md files)
            if self.ext_filter:
                cmd.extend(["--extension", self.ext_filter])

            # Max results
            cmd.extend(["--max-results", str(self.max_results)])

            # Query pattern (empty = match all)
            if self.query:
                cmd.append(self.query)
            else:
                cmd.append(".")  # Match everything

            # Search path
            cmd.append(self.root_path)

            # Run fd
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            stdout, _ = self._process.communicate(timeout=10)

            if self._cancelled:
                return

            # Parse results
            is_dir = (self.mode == "gotodir")
            for line in stdout.strip().split('\n'):
                if not line:
                    continue
                path = line.strip()
                try:
                    mtime = Path(path).stat().st_mtime
                    results.append((path, is_dir, mtime))
                except:
                    results.append((path, is_dir, 0))

        except subprocess.TimeoutExpired:
            if self._process:
                self._process.kill()
        except Exception as e:
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

        # Top bar: mode + bookmark
        top = QHBoxLayout()
        top.setSpacing(8)

        # Mode buttons
        self.mode_buttons = []
        modes = [
            ("Edit", "edit", "Edit file in Kate"),
            ("File", "gotofile", "Go to file's folder in Dolphin"),
            ("Dir", "gotodir", "Go to folder in Dolphin"),
        ]
        for label, mode, tooltip in modes:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("mode", mode)
            btn.setMinimumWidth(50)
            btn.setToolTip(tooltip)
            btn.clicked.connect(lambda _, m=mode: self.set_mode(m))
            btn.setStyleSheet("""
                QPushButton { background: #333; color: #888; border: none; padding: 4px 8px; border-radius: 3px; font-weight: bold; }
                QPushButton:checked { background: #5c9ae6; color: #fff; }
                QPushButton:hover:!checked { background: #444; }
            """)
            self.mode_buttons.append(btn)
            top.addWidget(btn)

        top.addSpacing(8)

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
        help_lbl = QLabel("Enter: Open | Tab: Mode | Esc: Close")
        help_lbl.setStyleSheet("color: #444; font-size: 10px;")
        help_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(help_lbl)

        # Window styling
        self.setStyleSheet("NixNavWindow { background: #141414; }")

    def setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key_Tab), self).activated.connect(self._cycle_mode)
        QShortcut(QKeySequence("Ctrl+O"), self).activated.connect(self._open_folder)

    def showEvent(self, event):
        super().showEvent(event)
        self._load_bookmarks()
        self._set_mode_from_config()
        self.search.setFocus()
        self._refresh()

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

    def _set_mode_from_config(self):
        mode = self.config.data.get("last_mode", "edit")
        # Handle old config values
        if mode in ("files", "grep"):
            mode = "edit"
        elif mode == "folders":
            mode = "gotodir"
        self.set_mode(mode)

    def set_mode(self, mode: str):
        for btn in self.mode_buttons:
            btn.setChecked(btn.property("mode") == mode)
        self.config.data["last_mode"] = mode
        self.config.save()
        placeholders = {
            "edit": "> Search files to edit...",
            "gotofile": "> Search files...",
            "gotodir": "> Search folders...",
        }
        self.search.setPlaceholderText(placeholders.get(mode, "> Search..."))
        self._refresh()

    def _cycle_mode(self):
        modes = ["edit", "gotofile", "gotodir"]
        current = self.config.data.get("last_mode", "edit")
        try:
            idx = modes.index(current)
            self.set_mode(modes[(idx + 1) % len(modes)])
        except:
            self.set_mode("edit")

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

    def _get_mode(self) -> str:
        return self.config.data.get("last_mode", "edit")

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
        mode = self._get_mode()

        self.status.setText("...")
        self._start_scan(root, mode, query, ext_filter)

    def _start_scan(self, root: str, mode: str, query: str, ext_filter: str = None):
        self._scanner_thread = QThread()
        self._scanner = FileScanner(root, mode, query,
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
        self.status.setText(str(len(results)))
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
                lines = [(" " if i.is_dir() else " ") + i.name for i in items[:80]]
                self.preview.setPlainText("\n".join(lines) if lines else "(empty)")
            except Exception as e:
                self.preview.setPlainText(f"Error: {e}")
        else:
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
            mode = self._get_mode()
            try:
                if mode == "edit":
                    # Open file in Kate
                    subprocess.Popen(["kate", path], start_new_session=True)
                elif mode == "gotofile":
                    # Open containing folder in Dolphin, with file selected
                    subprocess.Popen(["dolphin", "--select", path], start_new_session=True)
                elif mode == "gotodir":
                    # Open folder in Dolphin
                    subprocess.Popen(["dolphin", path], start_new_session=True)
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

        edit_action = QAction("Edit File", self.tray_menu)
        edit_action.triggered.connect(lambda: self._show_mode("edit"))
        self.tray_menu.addAction(edit_action)

        gotofile_action = QAction("Go to File", self.tray_menu)
        gotofile_action.triggered.connect(lambda: self._show_mode("gotofile"))
        self.tray_menu.addAction(gotofile_action)

        gotodir_action = QAction("Go to Folder", self.tray_menu)
        gotodir_action.triggered.connect(lambda: self._show_mode("gotodir"))
        self.tray_menu.addAction(gotodir_action)

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

    def _show_mode(self, mode: str):
        self.config.data["last_mode"] = mode
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
