# -*- coding: utf-8 -*-
"""快捷键：局部（窗口激活时）+ 全局（Windows RegisterHotKey，默认关闭）"""
from PySide6.QtCore import QObject, Signal, QAbstractNativeEventFilter, Qt
from PySide6.QtGui import QShortcut, QKeySequence

import ctypes
import ctypes.wintypes as wt

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8

# 键名 → Windows VK。用「QKeySequence.toString() 出来的名字」查表，
# 刻意不走 Qt 枚举：这个版本（PySide6 6.11 / Qt 6.11）把 Qt 枚举做成了纯 Python enum，
# `int(Qt.ControlModifier)` 会抛 TypeError，`QKeySequence[0]` 也不再是 int 而是 QKeyCombination。
VK_MAP = {
    "Space": 0x20, "Return": 0x0D, "Enter": 0x0D, "Esc": 0x1B, "Escape": 0x1B,
    "Tab": 0x09, "Backspace": 0x08, "Del": 0x2E, "Delete": 0x2E, "Ins": 0x2D, "Insert": 0x2D,
    "Home": 0x24, "End": 0x23, "PgUp": 0x21, "PgDown": 0x22, "PageUp": 0x21, "PageDown": 0x22,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
    "Plus": 0xBB, "Minus": 0xBD, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
    "Semicolon": 0xBA, "Quote": 0xDE, "Backquote": 0xC0,
    "BracketLeft": 0xDB, "BracketRight": 0xDD, "Backslash": 0xDC,
}
for _i in range(1, 25):                      # VK_F1 = 0x70
    VK_MAP["F%d" % _i] = 0x6F + _i

_MOD_WORDS = {"ctrl": MOD_CTRL, "control": MOD_CTRL, "alt": MOD_ALT,
              "shift": MOD_SHIFT, "meta": MOD_WIN, "win": MOD_WIN, "windows": MOD_WIN}


def _parse_key_string(txt):
    """'Ctrl+Shift+R' → (MOD_CTRL|MOD_SHIFT, VK_R)。纯字符串解析，不碰 Qt 枚举。

    ⚠ 历史坑（2026-09-24 导致「软件启动失败」）：旧实现用
    `combo & Qt.ControlModifier`，而 PySide6 6.x 的 `QKeySequence[0]` 是 QKeyCombination
    → `TypeError: unsupported operand type(s) for &: 'QKeyCombination' and 'KeyboardModifier'`。
    这段代码以前从没被执行过，是因为**从来没有任何全局热键**；
    把「下一素材」默认改成全局后，启动时第一次调用就崩（软件打不开）。
    """
    parts = [p.strip() for p in str(txt).split("+") if p.strip()]
    if not parts:
        return 0, 0
    mods = 0
    for p in parts[:-1]:
        mods |= _MOD_WORDS.get(p.lower(), 0)
    key = parts[-1]
    vk = VK_MAP.get(key) or VK_MAP.get(key.capitalize()) or VK_MAP.get(key.upper())
    if not vk and len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    return mods, int(vk or 0)


def _split_combo(combo):
    """（调试/自测用）QKeyCombination / QKeySequence 项 / 字符串 → (修饰键掩码, VK)。
    用 .value 取枚举整数（int(枚举) 在本版本会抛），再回退到字符串解析。"""
    if isinstance(combo, str):
        return _parse_key_string(combo)
    try:
        if hasattr(combo, "keyboardModifiers") and hasattr(combo, "key"):
            mods = getattr(combo.keyboardModifiers(), "value", None)
            key = getattr(combo.key(), "value", None)
            if isinstance(mods, int) and isinstance(key, int):
                mods_map = 0
                if mods & int(getattr(Qt.ControlModifier, "value", 0)):
                    mods_map |= MOD_CTRL
                if mods & int(getattr(Qt.AltModifier, "value", 0)):
                    mods_map |= MOD_ALT
                if mods & int(getattr(Qt.ShiftModifier, "value", 0)):
                    mods_map |= MOD_SHIFT
                if mods & int(getattr(Qt.MetaModifier, "value", 0)):
                    mods_map |= MOD_WIN
                vk = _parse_key_string(QKeySequence(combo).toString())[1]
                if vk:
                    return mods_map, vk
    except Exception:
        pass
    return _parse_key_string(QKeySequence(combo).toString())


def _vk_of(qkey) -> int:
    """（保留旧接口）键名/键码 → Windows VK"""
    if isinstance(qkey, str):
        return _parse_key_string(qkey)[1]
    return _parse_key_string(QKeySequence(qkey).toString())[1]


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            msg = wt.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self.manager.callbacks:
                self.manager.callbacks[msg.wParam]()
                return True, 0
        return False, 0


class GlobalHotkeys(QObject):
    """ctypes RegisterHotKey 实现的全局快捷键（默认关闭，需手动开启）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.callbacks = {}   # id -> callable
        self._next_id = 1
        self._filter = _NativeFilter(self)
        from PySide6.QtCore import QAbstractEventDispatcher
        QAbstractEventDispatcher.instance().installNativeEventFilter(self._filter)

    def register(self, key_seq: str, cb) -> bool:
        ks = QKeySequence(key_seq)
        if not ks.count():
            return False
        # 走字符串解析（ks.toString() 规范化成 "Ctrl+Right"），不碰 Qt 枚举 —— 见 _parse_key_string
        mods, vk = _parse_key_string(ks.toString())
        if not vk:
            return False
        hid = self._next_id
        self._next_id += 1
        ok = ctypes.windll.user32.RegisterHotKey(None, hid, mods, vk)
        if ok:
            self.callbacks[hid] = cb
        return bool(ok)

    def clear(self):
        for hid in list(self.callbacks):
            ctypes.windll.user32.UnregisterHotKey(None, hid)
        self.callbacks.clear()


class HotkeyManager(QObject):
    """统一管理：局部 QShortcut + 全局 RegisterHotKey，含冲突检测"""

    def __init__(self, window, actions, config, parent=None):
        super().__init__(parent)
        self.win = window
        self.actions = actions      # dict name -> callable
        self.cfg = config
        self.global_hk = GlobalHotkeys(self)
        self.shortcuts = []
        self.global_failed = []     # 本轮注册失败的全局热键名（多为被别的程序占用/无修饰键被系统拒）
        self.apply()

    def apply(self):
        # 清理旧绑定
        for s in self.shortcuts:
            try:
                s.setParent(None)
            except Exception:
                pass
        self.shortcuts = []
        self.global_hk.clear()
        self.global_failed = []

        used = set()
        for name, spec in self.cfg["hotkeys"].items():
            if not spec.get("enabled", True) or not spec.get("key"):
                continue
            key = spec["key"]
            if key in used:
                continue
            used.add(key)
            cb = self.actions.get(name)
            if not cb:
                continue
            sc = QShortcut(QKeySequence(key), self.win)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(cb)
            self.shortcuts.append(sc)
            if spec.get("global"):
                # 注册失败要能看见：全局热键会被其它程序抢占（尤其无修饰键的字母/方向键），
                # 静默失败会让人以为「按了没反应」—— 设置面板会据此弹提示。
                if not self.global_hk.register(key, cb):
                    self.global_failed.append(name)

    @staticmethod
    def find_conflict(cfg, name, key):
        for n, spec in cfg["hotkeys"].items():
            if n != name and spec.get("key") == key and spec.get("enabled", True):
                return n
        return None
