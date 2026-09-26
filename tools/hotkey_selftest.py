# -*- coding: utf-8 -*-
r"""全局热键注册自测（修 2026-09-24 的「启动失败」）

崩溃现场：`hotkeys.py register()` 里 `combo & Qt.ControlModifier`，
PySide6 6.x 的 `QKeySequence[0]` 是 QKeyCombination 不是 int → TypeError → 软件打不开。
以前没暴露是因为**从未有过全局热键**（next_scene 默认改全局后第一次跑到）。

本脚本覆盖：拆分函数、GlobalHotkeys.register、HotkeyManager.apply（启动时的真实调用链）。
用法：venv\Scripts\python.exe tools\hotkey_selftest.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication([])

import config as cfgmod  # noqa: E402
import hotkeys as hkmod  # noqa: E402
from hotkeys import GlobalHotkeys, HotkeyManager, _split_combo  # noqa: E402

out, PASS, FAIL = [], [], []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    w("   [%s] %s%s" % ("OK " if cond else "FAIL", name, ("  " + extra) if extra else ""))


def main():
    w("=" * 70)
    w("全局热键注册自测")
    w("=" * 70)

    w("")
    w("[1] QKeySequence[0] 拆分（QKeyCombination，本版本枚举不能用 int()）")
    from hotkeys import MOD_CTRL, MOD_SHIFT  # noqa
    for seq, want_mods in (("Right", 0), ("Ctrl+Right", MOD_CTRL),
                           ("B", 0), ("Ctrl+Shift+R", MOD_CTRL | MOD_SHIFT)):
        combo = QKeySequence(seq)[0]
        try:
            mods, vk = _split_combo(combo)
            ok = True
        except Exception as e:  # noqa
            ok, mods, vk = False, -1, -1
            w("      %s → 异常 %s" % (seq, e))
        check("拆分 %s 不抛异常" % seq, ok, "mods=%s vk=0x%x" % (mods, vk) if ok else "")
        if ok:
            check("  %s 修饰键正确" % seq, mods == want_mods, "得到 %s 期望 %s" % (mods, want_mods))
            check("  %s 能算出 VK" % seq, vk > 0, "vk=0x%x" % vk)

    w("")
    w("[2] GlobalHotkeys.register（崩的就是这一步）")
    gh = GlobalHotkeys(None)
    results = {}
    for seq in ("Right", "Ctrl+Right", "B", "Ctrl+Shift+R", "F12"):
        try:
            results[seq] = gh.register(seq, lambda: None)
            check("register(%r) 不抛异常" % seq, True, "返回 %s" % results[seq])
        except Exception as e:  # noqa
            results[seq] = None
            check("register(%r) 不抛异常" % seq, False, str(e))
    gh.clear()
    check("clear() 后不残留注册", not gh.callbacks)

    w("")
    w("[3] HotkeyManager.apply（启动时真实调用链）")
    cfg = {"hotkeys": {k: dict(v) for k, v in cfgmod.DEFAULT_HOTKEYS.items()}}
    check("next_scene 默认是「仅窗口」（2026-09-24 用户改回；全局会吞掉 Right 键）",
          cfg["hotkeys"]["next_scene"]["global"] is False)
    # 把一条改成全局：**这条链必须继续被覆盖** —— 2026-09-24 的「启动失败」
    # 就是因为没有任何全局热键时这段代码从没被执行过，藏着 PySide6 6.x 的兼容 bug。
    cfg["hotkeys"]["next_scene"]["global"] = True
    win = QWidget()
    actions = {k: (lambda: None) for k in cfgmod.DEFAULT_HOTKEYS}
    try:
        mgr = HotkeyManager(win, actions, cfg)
        check("HotkeyManager 构造成功（原来这里崩 → 启动失败）", True)
        check("局部 QShortcut 都建好了", len(mgr.shortcuts) >= 15, "shortcuts=%d" % len(mgr.shortcuts))
        w("      全局热键注册失败项：%s" % (mgr.global_failed or "无"))
        check("global_failed 是列表", isinstance(mgr.global_failed, list))
        mgr.global_hk.clear()
    except Exception as e:  # noqa
        import traceback
        w(traceback.format_exc())
        check("HotkeyManager 构造成功（原来这里崩 → 启动失败）", False, str(e))

    w("")
    w("[4] 无修饰键的字母键 + 全局（黑场 B）也不崩")
    cfg2 = {"hotkeys": {"blackout": {"key": "B", "global": True, "enabled": True}}}
    try:
        mgr2 = HotkeyManager(QWidget(), {"blackout": lambda: None}, cfg2)
        check("B 键全局注册不崩", True, "失败项=%s" % (mgr2.global_failed or "无"))
        mgr2.global_hk.clear()
    except Exception as e:  # noqa
        check("B 键全局注册不崩", False, str(e))

    w("")
    w("=" * 70)
    w("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        w("   ✗ " + f)
    open(r"P:\AutoVJ\_hotkey_selftest.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
