#!/usr/bin/env pythonw
"""Windows 11 quick launcher for shortcuts in the user's Start Menu root.

Press the global hotkey (Ctrl+Shift+Alt+K by default, configurable from the
tray menu) to open a full-screen launcher. Only shortcut files directly
inside %APPDATA%\\Microsoft\\Windows\\Start Menu are listed; subfolders such as
Programs are intentionally ignored.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import Counter
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QFileInfo,
    QKeyCombination,
    QProcess,
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
    QImage,
    QInputMethodEvent,
    QKeySequence,
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
    QDialog,
    QDialogButtonBox,
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
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "快速启动"
APP_ID = "StartMenuLauncher"
SERVER_NAME = "StartMenuLauncher.SingleInstance.v1"

HOTKEY_ID = 0xB001
HOTKEY_PROBE_ID = 0xB002
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
DEFAULT_HOTKEY_TEXT = "Ctrl+Shift+Alt+K"
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_RECONFIGURE_HOTKEY = 0x8000 + 1  # WM_APP + 1

USER_START_MENU = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
)
SHORTCUT_SUFFIXES = {".lnk", ".url", ".appref-ms"}

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_ID
SETTINGS_FILE = CONFIG_DIR / "settings.json"

SHADOW_MARGIN = 22
SEARCH_HEIGHT = 58
FOOTER_HEIGHT = 34
ITEM_HEIGHT = 104
ITEM_SPACING = 2
LIST_ICON_SIZE = 96

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

# Shell icon extraction: SHGetImageList is exported from shell32 by ordinal
# only, so it is resolved with GetProcAddress at runtime.
SHGFI_SYSICONINDEX = 0x000004000
SHIL_JUMBO = 4
ILD_TRANSPARENT = 0x00000001


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def guid_from_text(text: str) -> GUID:
    value = uuid.UUID(text)
    return GUID(
        value.fields[0],
        value.fields[1],
        value.fields[2],
        (ctypes.c_ubyte * 8)(*value.bytes[8:]),
    )


IID_IIMAGE_LIST = guid_from_text("46EB5926-582E-4017-9FDF-E8998DAA0950")


class ShellFileInfo(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


shell32.SHGetFileInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(ShellFileInfo),
    wintypes.UINT,
    wintypes.UINT,
]
shell32.SHGetFileInfoW.restype = ctypes.c_void_p
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL

_IMAGE_LIST_GET_ICON = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p, ctypes.c_int, wintypes.UINT,
    ctypes.POINTER(wintypes.HICON),
)
_IMAGE_LIST_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

_shgetimagelist = None
_shgetimagelist_resolved = False


def _resolve_shgetimagelist():
    """Look up SHGetImageList, which shell32 exports only by ordinal 727."""
    global _shgetimagelist, _shgetimagelist_resolved
    if _shgetimagelist_resolved:
        return _shgetimagelist
    _shgetimagelist_resolved = True
    try:
        kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
        kernel32.GetProcAddress.restype = ctypes.c_void_p
        address = kernel32.GetProcAddress(
            shell32._handle, ctypes.cast(727, ctypes.c_void_p)
        )
        if address:
            prototype = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_int,
                ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )
            _shgetimagelist = prototype(address)
    except Exception:
        _shgetimagelist = None
    return _shgetimagelist


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


# ---------------------------------------------------------------------------
# Hotkey specification and conversion
# ---------------------------------------------------------------------------

_MODIFIER_ORDER = (
    (MOD_CONTROL, "Ctrl"),
    (MOD_SHIFT, "Shift"),
    (MOD_ALT, "Alt"),
    (MOD_WIN, "Win"),
)

# Qt key -> Windows virtual-key code for keys whose values do not coincide.
_QT_KEY_TO_VK = {
    Qt.Key.Key_Left: 0x25,
    Qt.Key.Key_Up: 0x26,
    Qt.Key.Key_Right: 0x27,
    Qt.Key.Key_Down: 0x28,
    Qt.Key.Key_Home: 0x24,
    Qt.Key.Key_End: 0x23,
    Qt.Key.Key_PageUp: 0x21,
    Qt.Key.Key_PageDown: 0x22,
    Qt.Key.Key_Insert: 0x2D,
    Qt.Key.Key_Delete: 0x2E,
    Qt.Key.Key_Space: 0x20,
    Qt.Key.Key_Backspace: 0x08,
    Qt.Key.Key_Tab: 0x09,
    Qt.Key.Key_Return: 0x0D,
    Qt.Key.Key_Enter: 0x0D,
    Qt.Key.Key_Escape: 0x1B,
    Qt.Key.Key_Pause: 0x13,
    Qt.Key.Key_Print: 0x2C,
    Qt.Key.Key_ScrollLock: 0x91,
    Qt.Key.Key_CapsLock: 0x14,
    Qt.Key.Key_NumLock: 0x90,
    Qt.Key.Key_Semicolon: 0xBA,
    Qt.Key.Key_Equal: 0xBB,
    Qt.Key.Key_Comma: 0xBC,
    Qt.Key.Key_Minus: 0xBD,
    Qt.Key.Key_Period: 0xBE,
    Qt.Key.Key_Slash: 0xBF,
    Qt.Key.Key_QuoteLeft: 0xC0,
    Qt.Key.Key_BracketLeft: 0xDB,
    Qt.Key.Key_Backslash: 0xDC,
    Qt.Key.Key_BracketRight: 0xDD,
    Qt.Key.Key_Apostrophe: 0xDE,
}

_VK_NAMES = {
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x24: "Home",
    0x23: "End",
    0x21: "PageUp",
    0x22: "PageDown",
    0x2D: "Insert",
    0x2E: "Delete",
    0x20: "Space",
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x13: "Pause",
    0x2C: "PrintScreen",
    0x91: "ScrollLock",
    0x14: "CapsLock",
    0x90: "NumLock",
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}


@dataclass(frozen=True)
class HotkeySpec:
    """A global hotkey: Win32 modifier bits, a virtual-key code, and its text."""

    mods: int
    vk: int
    text: str

    @property
    def register_mods(self) -> int:
        return self.mods | MOD_NOREPEAT

    @property
    def label(self) -> str:
        parts = [name for bit, name in _MODIFIER_ORDER if self.mods & bit]
        parts.append(vk_name(self.vk))
        return " + ".join(parts)


# Key events hand out plain ints in some PyQt6 builds, so look keys up by value.
_QT_KEY_VALUE_TO_VK = {key.value: vk for key, vk in _QT_KEY_TO_VK.items()}


def qt_key_to_vk(key) -> int | None:
    value = getattr(key, "value", key)
    if not isinstance(value, int):
        return None
    vk = _QT_KEY_VALUE_TO_VK.get(value)
    if vk is not None:
        return vk
    if 0x41 <= value <= 0x5A or 0x30 <= value <= 0x39:  # A-Z, 0-9
        return value
    f1 = int(Qt.Key.Key_F1.value)
    if f1 <= value <= int(Qt.Key.Key_F24.value):
        return 0x70 + (value - f1)
    return None


def vk_name(vk: int) -> str:
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return _VK_NAMES.get(vk, f"0x{vk:02X}")


def mods_from_qt(qmods) -> int:
    mods = 0
    if qmods & Qt.KeyboardModifier.ControlModifier:
        mods |= MOD_CONTROL
    if qmods & Qt.KeyboardModifier.ShiftModifier:
        mods |= MOD_SHIFT
    if qmods & Qt.KeyboardModifier.AltModifier:
        mods |= MOD_ALT
    if qmods & Qt.KeyboardModifier.MetaModifier:
        mods |= MOD_WIN
    return mods


def hotkey_from_qt_key(key, qmods) -> HotkeySpec | None:
    """Build a spec from a key event's key and modifiers."""
    vk = qt_key_to_vk(key)
    if vk is None:
        return None
    qt_key = key if isinstance(key, Qt.Key) else Qt.Key(key)
    text = QKeySequence(QKeyCombination(qmods, qt_key)).toString(
        QKeySequence.SequenceFormat.PortableText
    )
    if not text:
        return None
    return HotkeySpec(mods_from_qt(qmods), vk, text)


def parse_hotkey(text: object) -> HotkeySpec | None:
    """Parse a stored hotkey string such as "Ctrl+Shift+Alt+K"."""
    if not isinstance(text, str) or not text.strip():
        return None
    normalized = re.sub(r"\bwin\b", "Meta", text.strip(), flags=re.IGNORECASE)
    normalized = re.sub(r"\bPageUp\b", "PgUp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bPageDown\b", "PgDown", normalized, flags=re.IGNORECASE)
    sequence = QKeySequence(normalized, QKeySequence.SequenceFormat.PortableText)
    if sequence.isEmpty():
        return None
    combination = sequence[0]
    vk = qt_key_to_vk(combination.key())
    if vk is None:
        return None
    canonical = QKeySequence(combination).toString(
        QKeySequence.SequenceFormat.PortableText
    )
    return HotkeySpec(mods_from_qt(combination.keyboardModifiers()), vk, canonical or text)


def default_hotkey() -> HotkeySpec:
    spec = parse_hotkey(DEFAULT_HOTKEY_TEXT)
    if spec is None:  # Defensive: QKeySequence always parses the default.
        return HotkeySpec(MOD_CONTROL | MOD_SHIFT | MOD_ALT, 0x4B, DEFAULT_HOTKEY_TEXT)
    return spec


def probe_hotkey(spec: HotkeySpec) -> tuple[bool, int]:
    """Check whether the combination can be registered right now."""
    if user32.RegisterHotKey(None, HOTKEY_PROBE_ID, spec.register_mods, spec.vk):
        user32.UnregisterHotKey(None, HOTKEY_PROBE_ID)
        return True, 0
    return False, ctypes.get_last_error()


def registration_error_text(spec: HotkeySpec, error: int) -> str:
    return (
        f"无法注册全局快捷键 {spec.label}（错误码 {error}）。\n"
        "该组合键可能已被其他程序占用。"
    )


class HotkeyThread(QThread):
    """Owns the global hotkey and its Win32 message loop."""

    activated = pyqtSignal()
    failed = pyqtSignal(str)
    reconfigured = pyqtSignal(bool, str)

    def __init__(self, spec: HotkeySpec, parent=None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._registered: HotkeySpec | None = None
        self._pending: HotkeySpec | None = None
        self._lock = threading.Lock()
        self._thread_id = 0
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if self._stop_event.is_set():
            return
        if self._register(self._spec):
            self._registered = self._spec
        else:
            self.failed.emit(registration_error_text(self._spec, ctypes.get_last_error()))
        # A request that arrived before the loop started is applied here.
        self._apply_pending()

        msg = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if (
                    msg.message == WM_HOTKEY
                    and msg.wParam == HOTKEY_ID
                    and self._registered is not None
                ):
                    self.activated.emit()
                elif msg.message == WM_RECONFIGURE_HOTKEY:
                    self._apply_pending()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._registered is not None:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = None

    def request(self, spec: HotkeySpec) -> None:
        """Ask the message loop to switch to another hotkey, without blocking."""
        with self._lock:
            self._pending = spec
        thread_id = self._thread_id
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_RECONFIGURE_HOTKEY, 0, 0)

    def _register(self, spec: HotkeySpec) -> bool:
        return bool(user32.RegisterHotKey(None, HOTKEY_ID, spec.register_mods, spec.vk))

    def _apply_pending(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None or pending == self._registered:
            return

        previous = self._registered
        if previous is not None:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = None

        if self._register(pending):
            self._spec = pending
            self._registered = pending
            self.reconfigured.emit(True, "")
            return

        error = ctypes.get_last_error()
        if previous is not None and self._register(previous):
            self._registered = previous
        self.reconfigured.emit(False, registration_error_text(pending, error))

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


def load_settings() -> dict:
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_settings(settings: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, SETTINGS_FILE)
    except OSError:
        pass


def load_order() -> list[str]:
    order = load_settings().get("order")
    if not isinstance(order, list):
        return []
    return [item for item in order if isinstance(item, str)]


def save_order(entries: list[ShortcutEntry]) -> None:
    settings = load_settings()
    settings["order"] = [str(entry.path) for entry in entries]
    save_settings(settings)


def load_hotkey() -> HotkeySpec:
    spec = parse_hotkey(load_settings().get("hotkey"))
    return spec if spec is not None else default_hotkey()


def save_hotkey(spec: HotkeySpec) -> None:
    settings = load_settings()
    settings["hotkey"] = spec.text
    save_settings(settings)


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def load_large_icon(path: Path, size: int = 256) -> QIcon | None:
    """Return the shortcut's high-resolution shell icon, or None.

    QFileIconProvider caps shortcuts at 48 px, which looks soft when rows show
    large icons. The shell's jumbo image list holds the real 256 px icons that
    Explorer uses on large-icon views.
    """
    shgetimagelist = _resolve_shgetimagelist()
    if shgetimagelist is None:
        return None

    try:
        info = ShellFileInfo()
        if not shell32.SHGetFileInfoW(
            os.fspath(path),
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
            SHGFI_SYSICONINDEX,
        ):
            return None

        image_list = ctypes.c_void_p()
        if shgetimagelist(
            SHIL_JUMBO, ctypes.byref(IID_IIMAGE_LIST), ctypes.byref(image_list)
        ) != 0 or not image_list:
            return None

        vtable = ctypes.cast(
            image_list, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        get_icon = _IMAGE_LIST_GET_ICON(vtable[10])
        release = _IMAGE_LIST_RELEASE(vtable[2])
        hicon = wintypes.HICON()
        try:
            if get_icon(
                image_list, info.iIcon, ILD_TRANSPARENT, ctypes.byref(hicon)
            ) != 0 or not hicon.value:
                return None
            image = QImage.fromHICON(hicon.value)
        finally:
            release(image_list)
            if hicon.value:
                user32.DestroyIcon(hicon)
    except Exception:
        return None

    if image.isNull():
        return None
    if image.width() != size or image.height() != size:
        image = image.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return QIcon(QPixmap.fromImage(image))


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
    painter.setBrush(QColor(120, 126, 136, 40))
    painter.drawRoundedRect(QRectF(8, 8, 48, 48), 13, 13)
    painter.setBrush(QColor(120, 126, 136, 78))
    painter.drawRoundedRect(QRectF(23, 23, 18, 18), 5, 5)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Hotkey settings dialog
# ---------------------------------------------------------------------------


_PURE_MODIFIER_KEYS = {
    Qt.Key.Key_Control,
    Qt.Key.Key_Shift,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Meta,
    Qt.Key.Key_CapsLock,
    Qt.Key.Key_NumLock,
    Qt.Key.Key_ScrollLock,
}
_alt_gr = getattr(Qt.Key, "Key_AltGr", None)
if _alt_gr is not None:
    _PURE_MODIFIER_KEYS.add(_alt_gr)
del _alt_gr


class HotkeyCapture(QLineEdit):
    """A read-only field that records the next key combination pressed in it."""

    spec_changed = pyqtSignal(object)
    message = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HotkeyCapture")
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._spec: HotkeySpec | None = None

    def selected_spec(self) -> HotkeySpec | None:
        return self._spec

    def set_spec(self, spec: HotkeySpec | None) -> None:
        self._spec = spec
        self.setText(spec.label if spec is not None else "")

    def keyPressEvent(self, event):  # noqa: N802 (Qt override)
        key = event.key()
        if key in _PURE_MODIFIER_KEYS:
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            # Let the dialog handle Esc so it cancels the whole dialog.
            super().keyPressEvent(event)
            return

        mods = mods_from_qt(event.modifiers())
        if not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN):
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                super().keyPressEvent(event)
                return
            self.message.emit("快捷键需要包含 Ctrl、Alt 或 Win 键")
            event.accept()
            return

        spec = hotkey_from_qt_key(key, event.modifiers())
        if spec is None:
            self.message.emit("这个按键不能用作全局快捷键")
            event.accept()
            return

        self.set_spec(spec)
        self.message.emit("")
        self.spec_changed.emit(spec)
        event.accept()


class HotkeyDialog(QDialog):
    def __init__(self, current: HotkeySpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec: HotkeySpec | None = current
        self.setWindowTitle("设置全局快捷键")
        self.setModal(True)
        self.setFixedWidth(440)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        title = QLabel("全局快捷键", self)
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        description = QLabel(
            "点击下方输入框，然后按下新的组合键。全局快捷键在任何程序中都能唤起启动器。",
            self,
        )
        description.setObjectName("DialogText")
        description.setWordWrap(True)
        layout.addWidget(description)

        self.capture = HotkeyCapture(self)
        self.capture.set_spec(current)
        self.capture.spec_changed.connect(self._on_spec_changed)
        self.capture.message.connect(self._show_message)
        layout.addWidget(self.capture)

        self.hint = QLabel("", self)
        self.hint.setObjectName("DialogHint")
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(18)
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        reset_button = QPushButton("恢复默认", self)
        reset_button.setAutoDefault(False)
        reset_button.clicked.connect(self._restore_default)
        buttons.addWidget(reset_button)
        buttons.addStretch(1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.button_box.button(QDialogButtonBox.StandardButton.Save).setDefault(True)
        buttons.addWidget(self.button_box)
        layout.addLayout(buttons)

        self.setStyleSheet(
            """
            QDialog {
                background-color: #FFFFFF;
            }
            QLabel#DialogTitle {
                color: #1A1A1A;
                font-size: 13pt;
                font-weight: 600;
            }
            QLabel#DialogText {
                color: #6A6A6A;
                font-size: 9.5pt;
            }
            QLabel#DialogHint {
                color: #B4453A;
                font-size: 9pt;
            }
            QLineEdit#HotkeyCapture {
                background-color: #F5F6F8;
                border: 1px solid #D8D8D8;
                border-radius: 10px;
                color: #1A1A1A;
                font-size: 12pt;
                font-weight: 600;
                selection-background-color: transparent;
            }
            QLineEdit#HotkeyCapture:focus {
                background-color: #FFFFFF;
                border: 1px solid #4F8EF7;
            }
            QPushButton {
                min-width: 72px;
                padding: 6px 16px;
                border: 1px solid #D0D0D0;
                border-radius: 6px;
                background-color: #F7F7F7;
                color: #202020;
            }
            QPushButton:hover {
                background-color: #EFEFEF;
            }
            QPushButton:default {
                background-color: #2F6FEB;
                border-color: #2F6FEB;
                color: #FFFFFF;
            }
            QPushButton:default:hover {
                background-color: #2861D0;
            }
            """
        )

    def selected_hotkey(self) -> HotkeySpec | None:
        return self._spec

    def _on_spec_changed(self, spec: HotkeySpec) -> None:
        self._spec = spec

    def _show_message(self, message: str) -> None:
        self.hint.setText(message)

    def _restore_default(self) -> None:
        spec = default_hotkey()
        self.capture.set_spec(spec)
        self._spec = spec
        self.hint.setText("")
        self.capture.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def accept(self) -> None:
        if self._spec is None:
            self.hint.setText("请先按下一个组合键")
            return
        super().accept()

    def showEvent(self, event):  # noqa: N802 (Qt override)
        super().showEvent(event)
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is not None:
            geometry = self.frameGeometry()
            geometry.moveCenter(screen.availableGeometry().center())
            self.move(geometry.topLeft())
        self.capture.setFocus(Qt.FocusReason.ShortcutFocusReason)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class LauncherWindow(QWidget):
    hotkey_changed = pyqtSignal(str)

    def __init__(self, app_icon: QIcon, hotkey_spec: HotkeySpec) -> None:
        super().__init__()
        self._app_icon = app_icon
        self._hotkey_spec = hotkey_spec
        self._hotkey_thread: HotkeyThread | None = None
        self._revert_spec: HotkeySpec | None = None
        self.entries: list[ShortcutEntry] = []
        self.visible_entries: list[ShortcutEntry] = []
        self._items_by_key: dict[str, QListWidgetItem] = {}
        self._icon_cache: dict[str, QIcon] = {}
        self._large_icon_cache: dict[str, QIcon] = {}
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
        self.app_list.setIconSize(QSize(LIST_ICON_SIZE, LIST_ICON_SIZE))
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
                border-radius: 14px;
                padding-left: 16px;
                font-family: "Segoe UI Variable Text", "Microsoft YaHei UI", "Segoe UI";
                font-size: 13pt;
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
            # Fill the list so the message sits in the middle of the window.
            empty.setSizeHint(
                QSize(
                    0,
                    max(
                        72,
                        self.height()
                        - SHADOW_MARGIN * 2
                        - SEARCH_HEIGHT
                        - 1
                        - FOOTER_HEIGHT,
                    ),
                )
            )
            empty.setForeground(QColor("#8A8A8A"))
            self.app_list.addItem(empty)
            self.app_list.setCurrentRow(-1)
            self.count_label.setText("0 个应用")

    def _load_icon_batch(self) -> None:
        pending = [entry for entry in self.entries if entry.icon is None]
        if not pending:
            self._icon_timer.stop()
            return

        for entry in pending[:6]:
            # Prefer the shell's 256 px icon: QFileIconProvider caps shortcuts
            # at 48 px, which is too small now that rows show large icons.
            icon = self._large_icon_cache.get(entry.key)
            if icon is None or icon.isNull():
                large = load_large_icon(entry.path)
                if large is not None and not large.isNull():
                    icon = large
                    self._large_icon_cache[entry.key] = large
                else:
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
        # Fill the whole usable screen; the panel keeps a small margin for its
        # drop shadow and rounded corners.
        self.setGeometry(screen.availableGeometry())

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
        self._place_window()
        self._apply_filter(select_first=True)
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

    # -- Hotkey -----------------------------------------------------------

    def current_hotkey(self) -> HotkeySpec:
        return self._hotkey_spec

    def attach_hotkey_thread(self, thread: HotkeyThread) -> None:
        self._hotkey_thread = thread
        thread.reconfigured.connect(self._on_hotkey_reconfigured)

    def open_hotkey_settings(self) -> None:
        dialog = HotkeyDialog(self._hotkey_spec, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        spec = dialog.selected_hotkey()
        if spec is None or spec == self._hotkey_spec:
            return

        available, error = probe_hotkey(spec)
        if not available:
            self.notify(
                "无法设置快捷键",
                f"快捷键 {spec.label} 当前不可用（错误码 {error}）。\n"
                "该组合键可能已被其他程序占用，请换一个。",
            )
            return

        self._revert_spec = self._hotkey_spec
        self._hotkey_spec = spec
        save_hotkey(spec)
        self.hotkey_changed.emit(spec.label)
        if self._hotkey_thread is not None:
            self._hotkey_thread.request(spec)

    def _on_hotkey_reconfigured(self, ok: bool, message: str) -> None:
        if ok:
            self._revert_spec = None
            return

        self.notify("无法设置快捷键", message)
        previous = self._revert_spec
        self._revert_spec = None
        if previous is None:
            return
        self._hotkey_spec = previous
        save_hotkey(previous)
        self.hotkey_changed.emit(previous.label)
        if self._hotkey_thread is not None:
            self._hotkey_thread.request(previous)

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


def restart_arguments() -> list[str]:
    """Arguments that relaunch this script (or the frozen executable)."""
    if getattr(sys, "frozen", False):
        return sys.argv[1:]
    return sys.argv


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

    hotkey_spec = load_hotkey()
    window = LauncherWindow(app_icon, hotkey_spec)

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
    open_action: QAction | None = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(app_icon)
        tray.setToolTip(f"{APP_NAME} · {hotkey_spec.label}")

        menu = QMenu()
        open_action = QAction(f"打开启动器    {hotkey_spec.label}", menu)
        refresh_action = QAction("刷新应用列表", menu)
        hotkey_action = QAction("设置全局快捷键…", menu)
        # (&R) / (&X) make R / X accelerators while the tray menu is open.
        restart_action = QAction("重启(&R)", menu)
        quit_action = QAction("退出(&X)", menu)
        menu.addAction(open_action)
        menu.addAction(refresh_action)
        menu.addSeparator()
        menu.addAction(hotkey_action)
        menu.addAction(restart_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        open_action.triggered.connect(window.show_launcher)
        refresh_action.triggered.connect(window.refresh_entries)
        hotkey_action.triggered.connect(window.open_hotkey_settings)

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

    hotkey = HotkeyThread(hotkey_spec)
    window.attach_hotkey_thread(hotkey)

    def on_hotkey_label_changed(label: str) -> None:
        if open_action is not None:
            open_action.setText(f"打开启动器    {label}")
        if tray is not None:
            tray.setToolTip(f"{APP_NAME} · {label}")

    window.hotkey_changed.connect(on_hotkey_label_changed)

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

    def restart_application() -> None:
        nonlocal hotkey
        # Release the hotkey and the single-instance name before the new process
        # starts, otherwise it would just ask the old instance to show.
        hotkey.stop()
        window.hide_launcher()
        server.close()
        QLocalServer.removeServer(SERVER_NAME)
        if QProcess.startDetached(sys.executable, restart_arguments()):
            if tray is not None:
                tray.hide()
            app.quit()
            return

        # Could not launch a new process: bring this instance back instead of
        # leaving the user without a launcher.
        server.listen(SERVER_NAME)
        hotkey = HotkeyThread(window.current_hotkey())
        window.attach_hotkey_thread(hotkey)
        hotkey.activated.connect(window.toggle_launcher)
        hotkey.failed.connect(on_hotkey_failed)
        hotkey.start()
        if tray is not None:
            tray.showMessage(
                "无法重启",
                "启动新实例失败，已恢复当前实例。",
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )

    if tray is not None:
        quit_action.triggered.connect(quit_application)
        restart_action.triggered.connect(restart_application)
    app.aboutToQuit.connect(lambda: hotkey.stop())

    exit_code = app.exec()

    hotkey.stop()
    server.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
