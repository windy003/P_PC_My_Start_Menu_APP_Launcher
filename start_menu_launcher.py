#!/usr/bin/env pythonw
"""Windows 11 quick launcher for shortcuts in the user's Start Menu root.

Press Ctrl+Shift+Alt+K to open a Spotlight-style window. Only shortcut files
directly inside %APPDATA%\\Microsoft\\Windows\\Start Menu are listed; subfolders
such as Programs are intentionally ignored.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QFileInfo,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QInputMethodEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileIconProvider,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "快速启动"
APP_ID = "StartMenuLauncher"
SERVER_NAME = "StartMenuLauncher.SingleInstance.v1"

HOTKEY_LABEL = "Ctrl + Shift + Alt + K"
HOTKEY_ID = 0xB001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
HOTKEY_MODS = MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
HOTKEY_VK = 0x4B  # K
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

USER_START_MENU = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
)
SHORTCUT_SUFFIXES = {".lnk", ".url", ".appref-ms"}

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_ID
SETTINGS_FILE = CONFIG_DIR / "settings.json"

WINDOW_WIDTH = 600
SHADOW_MARGIN = 22
SEARCH_HEIGHT = 58
FOOTER_HEIGHT = 34
ITEM_HEIGHT = 42
ITEM_SPACING = 2
ROW_STEP = ITEM_HEIGHT + ITEM_SPACING * 2
MAX_ROWS = 9
LIST_PADDING = 12

if sys.platform != "win32":
    raise SystemExit("This application only runs on Windows.")

# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LPARAM
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def force_foreground(window: QWidget) -> None:
    """Make a global-hotkey window actually receive keyboard focus."""
    if QApplication.platformName() != "windows":
        return
    hwnd = wintypes.HWND(int(window.winId()))
    fg = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    try:
        if fg_thread and fg_thread != current_thread:
            attached = bool(user32.AttachThreadInput(fg_thread, current_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, current_thread, False)


class HotkeyThread(QThread):
    """Owns the global hotkey and its Win32 message loop."""

    activated = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._thread_id = 0
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if self._stop_event.is_set():
            return
        if not user32.RegisterHotKey(None, HOTKEY_ID, HOTKEY_MODS, HOTKEY_VK):
            error = ctypes.get_last_error()
            self.failed.emit(
                f"无法注册全局快捷键 {HOTKEY_LABEL}（错误码 {error}）。\n"
                "该组合键可能已被其他程序占用。"
            )
            return

        msg = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.activated.emit()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        self._stop_event.set()
        thread_id = self._thread_id
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        self.wait(1500)


# ---------------------------------------------------------------------------
# Shortcut discovery and persistence
# ---------------------------------------------------------------------------


@dataclass
class ShortcutEntry:
    path: Path
    key: str
    name: str
    display_name: str
    icon: QIcon | None = None


def norm_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def scan_start_menu() -> list[ShortcutEntry]:
    """Read only the files directly inside the user's Start Menu directory."""
    try:
        children = list(USER_START_MENU.iterdir())
    except OSError:
        return []

    entries: list[ShortcutEntry] = []
    for path in children:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() not in SHORTCUT_SUFFIXES:
            continue
        name = path.stem.strip()
        # Explorer appends a localized "shortcut" suffix when creating links.
        name = re.sub(r"\s*-\s*(?:快捷方式|Shortcut)\s*$", "", name, flags=re.IGNORECASE)
        name = name.strip()
        if not name:
            continue
        entries.append(
            ShortcutEntry(
                path=path,
                key=norm_key(path),
                name=name,
                display_name=name,
            )
        )

    # Two files can share a stem (for example name.lnk and name.url).
    counts = Counter(entry.name.casefold() for entry in entries)
    for entry in entries:
        if counts[entry.name.casefold()] > 1:
            extension = entry.path.suffix.lstrip(".").upper()
            entry.display_name = f"{entry.name}  ({extension})"

    entries.sort(key=lambda entry: entry.name.casefold())
    return entries


def load_order() -> list[str]:
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    order = payload.get("order")
    if not isinstance(order, list):
        return []
    return [item for item in order if isinstance(item, str)]


def save_order(entries: list[ShortcutEntry]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        payload = {"order": [str(entry.path) for entry in entries]}
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, SETTINGS_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(size * 0.045, size * 0.045, size * 0.91, size * 0.91)
        radius = size * 0.24
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor("#4F8EF7"))
        gradient.setColorAt(1.0, QColor("#7A5AF8"))
        painter.fillPath(path, QBrush(gradient))

        margin = size * 0.28
        gap = size * 0.075
        cell = (size - 2 * margin - gap) / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 245))
        for row in range(2):
            for column in range(2):
                x = margin + column * (cell + gap)
                y = margin + row * (cell + gap)
                painter.drawRoundedRect(QRectF(x, y, cell, cell), cell * 0.3, cell * 0.3)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def make_placeholder_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 22))
    painter.drawRoundedRect(QRectF(8, 8, 48, 48), 13, 13)
    painter.setBrush(QColor(255, 255, 255, 45))
    painter.drawRoundedRect(QRectF(23, 23, 18, 18), 5, 5)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class LauncherWindow(QWidget):
    def __init__(self, app_icon: QIcon) -> None:
        super().__init__()
        self._app_icon = app_icon
        self.entries: list[ShortcutEntry] = []
        self.visible_entries: list[ShortcutEntry] = []
        self._items_by_key: dict[str, QListWidgetItem] = {}
        self._icon_cache: dict[str, QIcon] = {}
        self._icon_provider = QFileIconProvider()
        self._placeholder_icon = make_placeholder_icon()
        self._suppress_deactivate_until = 0.0
        self._fade_animation: QPropertyAnimation | None = None
        self._composing = False
        self._composition_ended_at = 0.0
        self.tray_icon: QSystemTrayIcon | None = None

        self._icon_timer = QTimer(self)
        self._icon_timer.setInterval(0)
        self._icon_timer.timeout.connect(self._load_icon_batch)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(WINDOW_WIDTH)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._build_ui()
        self.refresh_entries()

    # -- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        root.setSpacing(0)

        panel = QFrame(self)
        panel.setObjectName("Panel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root.addWidget(panel)

        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 110))
        panel.setGraphicsEffect(shadow)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        search_row = QWidget(panel)
        search_row.setFixedHeight(SEARCH_HEIGHT)
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(18, 0, 18, 0)
        search_layout.setSpacing(12)

        logo = QLabel(search_row)
        logo.setFixedSize(22, 22)
        logo.setPixmap(self._app_icon.pixmap(22, 22))
        search_layout.addWidget(logo)

        self.search = QLineEdit(search_row)
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("搜索应用…")
        self.search.setFrame(False)
        self.search.setClearButtonEnabled(False)
        palette = self.search.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor("#1A1A1A"))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8A8A8A"))
        self.search.setPalette(palette)
        self.search.textChanged.connect(self._on_search_changed)
        self.search.installEventFilter(self)
        search_layout.addWidget(self.search, 1)
        panel_layout.addWidget(search_row)

        separator = QFrame(panel)
        separator.setObjectName("Separator")
        separator.setFixedHeight(1)
        separator.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        panel_layout.addWidget(separator)

        self.app_list = QListWidget(panel)
        self.app_list.setObjectName("AppList")
        self.app_list.setFrameShape(QFrame.Shape.NoFrame)
        self.app_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.app_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.app_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.app_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.app_list.setIconSize(QSize(26, 26))
        self.app_list.setSpacing(ITEM_SPACING)
        self.app_list.setUniformItemSizes(True)
        self.app_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.app_list.itemClicked.connect(self._on_item_clicked)
        panel_layout.addWidget(self.app_list, 1)

        footer = QWidget(panel)
        footer.setFixedHeight(FOOTER_HEIGHT)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 0, 20, 8)
        footer_layout.setSpacing(0)
        self.hint_label = QLabel("↑ ↓ 选择    Enter 打开    Esc 关闭", footer)
        self.hint_label.setObjectName("Hint")
        self.count_label = QLabel("", footer)
        self.count_label.setObjectName("Count")
        footer_layout.addWidget(self.hint_label)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.count_label)
        panel_layout.addWidget(footer)

        self.setStyleSheet(
            """
            QFrame#Panel {
                background-color: #FFFFFF;
                border: 1px solid #E2E2E2;
                border-radius: 16px;
            }
            QFrame#Separator {
                background-color: rgba(0, 0, 0, 18);
                border: none;
            }
            QLineEdit#Search {
                background: transparent;
                border: none;
                color: #1A1A1A;
                font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI";
                font-size: 13pt;
                selection-background-color: #CFE4FA;
                selection-color: #101010;
            }
            QListWidget#AppList {
                background: transparent;
                border: none;
                outline: 0;
                padding: 6px 8px;
            }
            QListWidget#AppList::item {
                color: #202020;
                border-radius: 9px;
                padding-left: 10px;
                font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI";
                font-size: 10pt;
            }
            QListWidget#AppList::item:selected {
                background-color: rgba(0, 120, 212, 28);
                border: 1px solid rgba(0, 120, 212, 110);
                color: #0F0F0F;
            }
            QListWidget#AppList::item:hover:!selected {
                background-color: rgba(0, 0, 0, 14);
            }
            QLabel#Hint, QLabel#Count {
                color: #8A8A8A;
                font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 6px 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(0, 0, 0, 55);
                min-height: 28px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(0, 0, 0, 90);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
                background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )

    # -- Data -------------------------------------------------------------

    def refresh_entries(self) -> None:
        """Rescan the Start Menu root, preserving icons and pinned order."""
        for entry in self.entries:
            if entry.icon is not None and not entry.icon.isNull():
                self._icon_cache[entry.key] = entry.icon

        entries = scan_start_menu()
        for entry in entries:
            entry.icon = self._icon_cache.get(entry.key)

        rank: dict[str, int] = {}
        for index, path in enumerate(load_order()):
            try:
                rank[norm_key(path)] = index
            except (OSError, ValueError):
                continue
        entries.sort(
            key=lambda entry: (
                rank.get(entry.key, len(rank) + 1_000_000),
                entry.name.casefold(),
            )
        )

        self.entries = entries
        self._apply_filter(select_first=True)
        if any(entry.icon is None for entry in self.entries):
            self._icon_timer.start(0)

    def _on_search_changed(self) -> None:
        self._apply_filter(select_first=True)

    def _apply_filter(self, select_first: bool = True) -> None:
        query = self.search.text().strip().casefold()
        tokens = [token for token in query.split() if token]
        visible: list[ShortcutEntry] = []
        for entry in self.entries:
            haystack = f"{entry.name} {entry.display_name}".casefold()
            if all(token in haystack for token in tokens):
                visible.append(entry)
        self.visible_entries = visible

        self.app_list.clear()
        self._items_by_key.clear()

        for entry in visible:
            icon = entry.icon if entry.icon is not None else self._placeholder_icon
            if icon.isNull():
                icon = self._placeholder_icon
            item = QListWidgetItem(icon, entry.display_name)
            item.setSizeHint(QSize(0, ITEM_HEIGHT))
            item.setData(Qt.ItemDataRole.UserRole, entry.key)
            item.setToolTip(str(entry.path))
            self.app_list.addItem(item)
            self._items_by_key[entry.key] = item

        if visible:
            if select_first:
                self.app_list.setCurrentRow(0)
            self.app_list.scrollToTop()
            self.count_label.setText(f"{len(visible)} 个应用")
        else:
            empty = QListWidgetItem("未找到匹配的快捷方式")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setSizeHint(QSize(0, 72))
            empty.setForeground(QColor("#8A8A8A"))
            self.app_list.addItem(empty)
            self.app_list.setCurrentRow(-1)
            self.count_label.setText("0 个应用")

        self._update_window_height()

    def _update_window_height(self) -> None:
        if self.visible_entries:
            rows = min(len(self.visible_entries), MAX_ROWS)
            list_height = rows * ROW_STEP + LIST_PADDING
        else:
            list_height = 92
        self.app_list.setFixedHeight(list_height)
        panel_height = SEARCH_HEIGHT + 1 + list_height + FOOTER_HEIGHT
        self.setFixedHeight(panel_height + SHADOW_MARGIN * 2)

    def _load_icon_batch(self) -> None:
        pending = [entry for entry in self.entries if entry.icon is None]
        if not pending:
            self._icon_timer.stop()
            return

        for entry in pending[:6]:
            try:
                icon = self._icon_provider.icon(QFileInfo(str(entry.path)))
            except Exception:
                icon = QIcon()
            if icon.isNull():
                # Mark as attempted so this entry is not retried on every tick.
                entry.icon = QIcon()
                continue
            entry.icon = icon
            self._icon_cache[entry.key] = icon
            item = self._items_by_key.get(entry.key)
            if item is not None:
                item.setIcon(icon)

        if len(pending) <= 6:
            self._icon_timer.stop()

    # -- Presentation -----------------------------------------------------

    def _place_window(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        width = self.width()
        height = self.height()
        x = area.left() + (area.width() - width) // 2
        y = area.top() + int((area.height() - height) * 0.26)
        y = max(area.top() + 16, min(y, area.bottom() - height - 16))
        self.move(x, y)

    def _animate_in(self) -> None:
        if self._fade_animation is not None:
            self._fade_animation.stop()
        animation = QPropertyAnimation(self, b"windowOpacity", self)
        animation.setDuration(110)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(self._clear_fade_animation)
        self._fade_animation = animation
        animation.start()

    def _clear_fade_animation(self) -> None:
        self._fade_animation = None

    def show_launcher(self) -> None:
        if not self.entries:
            self.refresh_entries()
        if self.search.text():
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self._apply_filter(select_first=True)
        self._place_window()
        self._composing = False
        self._composition_ended_at = 0.0
        self._suppress_deactivate_until = time.monotonic() + 0.35
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        force_foreground(self)
        self.activateWindow()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._animate_in()

    def hide_launcher(self) -> None:
        if self._fade_animation is not None:
            self._fade_animation.stop()
            self._fade_animation = None
        self.hide()
        self.setWindowOpacity(1.0)

    def toggle_launcher(self) -> None:
        if self.isVisible() and self.isActiveWindow():
            self.hide_launcher()
        else:
            self.show_launcher()

    def _move_selection(self, delta: int) -> None:
        count = len(self.visible_entries)
        if count == 0:
            return
        current = self.app_list.currentRow()
        if current < 0:
            current = 0
        target = max(0, min(count - 1, current + delta))
        self.app_list.setCurrentRow(target)
        item = self.app_list.item(target)
        if item is not None:
            self.app_list.scrollToItem(item)

    def _activate_current(self) -> None:
        row = self.app_list.currentRow()
        if 0 <= row < len(self.visible_entries):
            self._launch(self.visible_entries[row])

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        row = self.app_list.row(item)
        if 0 <= row < len(self.visible_entries):
            self._launch(self.visible_entries[row])

    def _launch(self, entry: ShortcutEntry) -> None:
        try:
            os.startfile(str(entry.path))
        except OSError as error:
            self.notify("无法启动", f"{entry.display_name}\n{error}")
            return

        if entry in self.entries:
            self.entries.remove(entry)
            self.entries.insert(0, entry)
            save_order(self.entries)

        self.hide_launcher()

    def notify(self, title: str, message: str) -> None:
        if self.tray_icon is not None and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                title,
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
        else:
            QMessageBox.warning(self, title, message)

    # -- Events -----------------------------------------------------------

    def event(self, event):  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            if time.monotonic() >= self._suppress_deactivate_until:
                self.hide_launcher()
        return super().event(event)

    def eventFilter(self, watched, event):  # noqa: N802 (Qt override)
        if watched is self.search:
            if event.type() == QEvent.Type.InputMethod:
                if isinstance(event, QInputMethodEvent):
                    if event.preeditString():
                        self._composing = True
                    else:
                        self._composing = False
                        self._composition_ended_at = time.monotonic()
                return False

            if event.type() == QEvent.Type.KeyPress:
                # Never steal keys that belong to an active IME composition.
                if self._composing:
                    return False
                key = event.key()
                if (
                    key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and time.monotonic() - self._composition_ended_at < 0.1
                ):
                    return False
                if key == Qt.Key.Key_Down:
                    self._move_selection(1)
                    return True
                if key == Qt.Key.Key_Up:
                    self._move_selection(-1)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._activate_current()
                    return True
                if key == Qt.Key.Key_Escape:
                    self.hide_launcher()
                    return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):  # noqa: N802 (Qt override)
        if event.key() == Qt.Key.Key_Escape:
            self.hide_launcher()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        event.ignore()
        self.hide_launcher()


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------


def notify_existing_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(200):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(200)
    socket.disconnectFromServer()
    return True


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("Local")
    app.setQuitOnLastWindowClosed(False)

    try:
        shell32.SetCurrentProcessExplicitAppUserModelID(f"Local.{APP_ID}")
    except Exception:
        pass

    font = QFont("Segoe UI Variable Text", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    app_icon = make_app_icon()
    app.setWindowIcon(app_icon)

    if notify_existing_instance():
        return 0

    window = LauncherWindow(app_icon)

    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)

    def handle_connection() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is None:
                continue

            def read_command(socket=connection) -> None:
                data = bytes(socket.readAll())
                if b"show" in data:
                    window.show_launcher()

            connection.readyRead.connect(read_command)
            connection.disconnected.connect(connection.deleteLater)

    server.newConnection.connect(handle_connection)

    tray: QSystemTrayIcon | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(app_icon)
        tray.setToolTip(f"{APP_NAME} · {HOTKEY_LABEL}")

        menu = QMenu()
        open_action = QAction(f"打开启动器    {HOTKEY_LABEL}", menu)
        refresh_action = QAction("刷新应用列表", menu)
        # (&X) makes X an accelerator while the tray menu is open.
        quit_action = QAction("退出(&X)", menu)
        menu.addAction(open_action)
        menu.addAction(refresh_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        open_action.triggered.connect(window.show_launcher)
        refresh_action.triggered.connect(window.refresh_entries)

        tray.setContextMenu(menu)

        def on_tray_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
            if reason in (
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick,
            ):
                window.show_launcher()

        tray.activated.connect(on_tray_activated)
        tray.show()
        window.tray_icon = tray

    hotkey = HotkeyThread()

    def on_hotkey_failed(message: str) -> None:
        if tray is not None and tray.isVisible():
            tray.showMessage(
                "快捷键不可用",
                message,
                QSystemTrayIcon.MessageIcon.Warning,
                6000,
            )
        else:
            print(message, file=sys.stderr)

    hotkey.activated.connect(window.toggle_launcher)
    hotkey.failed.connect(on_hotkey_failed)
    hotkey.start()

    def quit_application() -> None:
        hotkey.stop()
        window.hide_launcher()
        if tray is not None:
            tray.hide()
        app.quit()

    if tray is not None:
        quit_action.triggered.connect(quit_application)
    app.aboutToQuit.connect(hotkey.stop)

    exit_code = app.exec()

    hotkey.stop()
    server.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
