#!/usr/bin/env python3
"""
NixNav - A GUI file navigator for NixOS/KDE Wayland
GUI version of fcd, fcat, fgrep, fnano commands

Minimal, fzf-inspired interface.
"""

import sys
import os
import json
import subprocess
import fnmatch
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
                {"name": "NixOS Config", "path": "/etc/nixos"},
                {"name": "Home", "path": str(Path.home())},
            ],
            "last_bookmark": 0,
            "last_mode": "files",
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


class FileScanner(QObject):
    """Fast file scanner using fd (Rust-based find)."""
    results_ready = Signal(list)
    finished = Signal()

    def __init__(self, root_path: str, mode: str, query: str, exclude_patterns: list, max_results: int):
        super().__init__()
        self.root_path = root_path
        self.mode = mode
        self.query = query
        self.exclude_patterns = exclude_patterns
        self.max_results = max_results
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
            if self.mode == "folders":
                cmd.extend(["--type", "d"])
            else:  # files
                cmd.extend(["--type", "f"])

            # Exclude patterns
            for pattern in self.exclude_patterns:
                cmd.extend(["--exclude", pattern])

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
            is_dir = (self.mode == "folders")
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


class GrepScanner(QObject):
    """Fast content scanner using ripgrep."""
    results_ready = Signal(list)
    finished = Signal()

    def __init__(self, root_path: str, query: str, exclude_patterns: list, max_results: int):
        super().__init__()
        self.root_path = root_path
        self.query = query
        self.exclude_patterns = exclude_patterns
        self.max_results = max_results
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
            # Build rg command
            cmd = [
                "rg", "--color=never", "--line-number", "--no-heading",
                "--max-count=5",  # Max 5 matches per file
                "--max-filesize=1M",  # Skip large files
                "-i",  # Case insensitive
            ]

            # Exclude patterns
            for pattern in self.exclude_patterns:
                cmd.extend(["--glob", f"!{pattern}"])

            cmd.append(self.query)
            cmd.append(self.root_path)

            # Run rg
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            stdout, _ = self._process.communicate(timeout=10)

            if self._cancelled:
                return

            # Parse results: path:line_num:content
            file_matches = {}
            for line in stdout.strip().split('\n'):
                if not line or ':' not in line:
                    continue
                try:
                    # Format: /path/to/file:123:matching line content
                    parts = line.split(':', 2)
                    if len(parts) >= 3:
                        path, line_num, content = parts[0], int(parts[1]), parts[2]
                        if path not in file_matches:
                            file_matches[path] = []
                        if len(file_matches[path]) < 5:
                            file_matches[path].append((line_num, content.strip()[:120]))
                except:
                    pass

                if len(file_matches) >= self.max_results:
                    break

            results = [(path, matches) for path, matches in file_matches.items()]

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
        self._grep_scanner = None
        self._results_data = []  # Store (path, is_dir, matches) for each item

        self.setWindowTitle("NixNav")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.resize(1000, 650)
        self.setup_ui()
        self.setup_shortcuts()

    def closeEvent(self, event):
        """Clean up threads on close."""
        self._cancel_scan()
        super().closeEvent(event)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top bar: mode + bookmark
        top = QHBoxLayout()
        top.setSpacing(8)

        # Mode buttons - compact
        self.mode_buttons = []
        for label, mode in [("F", "files"), ("D", "folders"), ("S", "grep")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("mode", mode)
            btn.setFixedWidth(32)
            btn.setToolTip({"F": "Files", "D": "Directories", "S": "Search contents"}[label])
            btn.clicked.connect(lambda _, m=mode: self.set_mode(m))
            btn.setStyleSheet("""
                QPushButton { background: #333; color: #888; border: none; padding: 4px; border-radius: 3px; font-weight: bold; }
                QPushButton:checked { background: #5c9ae6; color: #fff; }
                QPushButton:hover:!checked { background: #444; }
            """)
            self.mode_buttons.append(btn)
            top.addWidget(btn)

        top.addSpacing(8)

        # Bookmark dropdown
        self.bookmark_combo = QComboBox()
        self.bookmark_combo.setStyleSheet("""
            QComboBox { background: #252525; color: #ccc; border: 1px solid #444; padding: 4px 8px; border-radius: 3px; min-width: 140px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #252525; color: #ccc; selection-background-color: #5c9ae6; }
        """)
        self.bookmark_combo.currentIndexChanged.connect(self._on_bookmark_changed)
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
        help_lbl = QLabel("Enter: Open | Ctrl+O: Open folder | Tab: Mode | Esc: Close")
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
        mode = self.config.data.get("last_mode", "files")
        self.set_mode(mode)

    def set_mode(self, mode: str):
        for btn in self.mode_buttons:
            btn.setChecked(btn.property("mode") == mode)
        self.config.data["last_mode"] = mode
        self.config.save()
        placeholders = {"files": "> Search files...", "folders": "> Search folders...", "grep": "> Search contents (min 2 chars)..."}
        self.search.setPlaceholderText(placeholders.get(mode, "> Search..."))
        self._refresh()

    def _cycle_mode(self):
        modes = ["files", "folders", "grep"]
        current = self.config.data.get("last_mode", "files")
        try:
            idx = modes.index(current)
            self.set_mode(modes[(idx + 1) % len(modes)])
        except:
            self.set_mode("files")

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

    def _get_path(self) -> str:
        idx = self.bookmark_combo.currentIndex()
        return self.bookmark_combo.itemData(idx) if idx >= 0 else str(Path.home())

    def _get_mode(self) -> str:
        return self.config.data.get("last_mode", "files")

    def _on_search_changed(self, text: str):
        if hasattr(self, '_timer'):
            self._timer.stop()
        else:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self._refresh)
        delay = 250 if self._get_mode() == "grep" else 100
        self._timer.start(delay)

    def _cancel_scan(self):
        if self._scanner:
            self._scanner.cancel()
            self._scanner = None
        if self._grep_scanner:
            self._grep_scanner.cancel()
            self._grep_scanner = None
        if self._scanner_thread:
            if self._scanner_thread.isRunning():
                self._scanner_thread.quit()
                self._scanner_thread.wait(2000)
            self._scanner_thread = None

    def _refresh(self):
        self._cancel_scan()
        self.list.clear()
        self._results_data.clear()
        self.preview.clear()

        query = self.search.text().strip()
        root = self._get_path()
        mode = self._get_mode()

        self.status.setText("...")

        if mode == "grep":
            if len(query) < 2:
                self.status.setText("type 2+ chars")
                return
            self._start_grep(root, query)
        else:
            self._start_scan(root, mode, query)

    def _start_scan(self, root: str, mode: str, query: str):
        self._scanner_thread = QThread()
        self._scanner = FileScanner(root, mode, query,
                                    self.config.data.get("exclude_patterns", []),
                                    self.config.data.get("max_results", 500))
        self._scanner.moveToThread(self._scanner_thread)
        self._scanner_thread.started.connect(self._scanner.run)
        self._scanner.results_ready.connect(self._on_file_results)
        self._scanner.finished.connect(self._scanner_thread.quit)
        self._scanner_thread.start()

    def _start_grep(self, root: str, query: str):
        self._scanner_thread = QThread()
        self._grep_scanner = GrepScanner(root, query,
                                         self.config.data.get("exclude_patterns", []),
                                         self.config.data.get("max_results", 100))
        self._grep_scanner.moveToThread(self._scanner_thread)
        self._scanner_thread.started.connect(self._grep_scanner.run)
        self._grep_scanner.results_ready.connect(self._on_grep_results)
        self._grep_scanner.finished.connect(self._scanner_thread.quit)
        self._scanner_thread.start()

    def _on_file_results(self, results: list):
        results.sort(key=lambda x: x[2], reverse=True)  # Sort by mtime
        root = self._get_path()
        for path, is_dir, mtime in results:
            # Show relative path if under root
            try:
                rel = str(Path(path).relative_to(root))
            except:
                rel = path
            prefix = "" if is_dir else ""
            item = QListWidgetItem(f"{prefix} {rel}")
            self.list.addItem(item)
            self._results_data.append((path, is_dir, None))

        self.status.setText(str(len(results)))
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _on_grep_results(self, results: list):
        root = self._get_path()
        for path, matches in results:
            try:
                rel = str(Path(path).relative_to(root))
            except:
                rel = path
            # Show first match
            first_match = matches[0] if matches else (0, "")
            line_num, line = first_match
            display = f" {rel}:{line_num}"
            item = QListWidgetItem(display)
            self.list.addItem(item)
            self._results_data.append((path, False, matches))

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
            try:
                if is_dir:
                    subprocess.Popen(["dolphin", path], start_new_session=True)
                else:
                    subprocess.Popen(["kate", path], start_new_session=True)
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

        files_action = QAction("Files", self.tray_menu)
        files_action.triggered.connect(lambda: self._show_mode("files"))
        self.tray_menu.addAction(files_action)

        folders_action = QAction("Folders", self.tray_menu)
        folders_action.triggered.connect(lambda: self._show_mode("folders"))
        self.tray_menu.addAction(folders_action)

        search_action = QAction("Search", self.tray_menu)
        search_action.triggered.connect(lambda: self._show_mode("grep"))
        self.tray_menu.addAction(search_action)

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
