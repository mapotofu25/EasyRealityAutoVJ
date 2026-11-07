# -*- coding: utf-8 -*-
"""码表包「显示名」与「删除二次确认」（用户 2026-11-02）。

## 用户原话
  ① 「之前说的码表图层和码表管理里显示的名字不一样的问题」
  ② 「删除码表的时候得加二次弹窗」

## 背景
① 实测 `测试151.ercue` 的包内 `title` 是 `测试11`（导出写残）。
   演出端 AutoVJ 早统一按**文件名**显示（`cue_page.sheet_label`），编排端这里却显示 `title`
   ⇒ 同一张表两边两个名字。现在编排端也改成**文件名**，`title` 落 tooltip。
② 「删除这张码表包」原来只在**有未导出改动**时才弹（`_warn_unsaved` 干净就静默放行），
   而它会把表同时从列表 + 自动保存里移除。现在**无条件**先弹一次，默认按钮 = 取消。

## 断言
  (a) `_sheet_label`：有 `backing_path` ⇒ 用文件名（即使 title 不同）
  (b) `_sheet_label`：没有 `backing_path`（新建未导出）⇒ 用 title 兜底
  (c) `_pkg_tooltip`：title 与文件名不同时把 title 写进 tooltip
  (d) `_confirm_close_sheet` 在**非交互**下返回 True（无头测试/自动化不被卡）
  (e) `_confirm_close_sheet` 在交互下**一定**弹框（用 monkeypatch 统计调用次数，不真弹）
  (f) 点「取消」⇒ 返回 False（不动 sessions）

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_pkg_label_close.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def check(cond, tag, msg=""):
    print("  %s %s%s" % ("✓" if cond else "✗", tag, ("   " + msg) if msg else ""))
    if not cond:
        FAILS.append(tag)
    return cond


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from ercue.ui import window as W
    from ercue.model import ercue_io

    print("\n--- (a)(b)(c) 显示名 ---")
    win = W.MainWindow()
    try:
        # 造两个会话：一个"已导入到磁盘"（backing_path 有值、title 不同），一个"新建未导出"
        key1, key2 = "sheet1", "sheet2"
        s1 = W.SheetSession(key1, ercue_io.new_sheet("测试11", ""))
        s1.backing_path = r"Y:\EasyRealityAutoVJ\码表素材\测试151.ercue"
        s2 = W.SheetSession(key2, ercue_io.new_sheet("新建码表9", ""))
        win.sessions = {key1: s1, key2: s2}

        lab = win._sheet_label(s1)
        check(lab.startswith("测试151.ercue"), "(a) 已导入 ⇒ 显示文件名", lab)
        check("测试11" not in lab, "(a2) 不再显示包内 title", lab)

        lab2 = win._sheet_label(s2)
        check(lab2.startswith("新建码表9"), "(b) 未导出 ⇒ 用 title 兜底", lab2)

        tip = win._pkg_tooltip(s1)
        check("测试11" in tip and "测试151.ercue" in tip,
              "(c) tooltip 里 title + 路径都在", tip.replace("\n", " | "))

        print("\n--- (d) 非交互 ⇒ 不弹框 ---")
        win._interactive = False
        check(win._confirm_close_sheet(s1) is True, "(d) 无头模式直接放行")

        print("\n--- (e)(f) 交互 ⇒ 一定弹框，取消则不放行 ---")
        win._interactive = True
        calls = {"n": 0}

        class FakeBox:
            """替身：只记录"弹过几次"和用户点了哪个按钮，不真的弹窗。"""

            Question = DestructiveRole = RejectRole = 0
            clicked = "取消"

            def __init__(self, *a, **k):
                calls["n"] += 1
                self._buttons = []

            def setWindowTitle(self, *a):
                pass

            def setIcon(self, *a):
                pass

            def setText(self, *a):
                pass

            def setInformativeText(self, *a):
                pass

            def addButton(self, text, role=None):
                b = object()
                self._buttons.append((b, text))
                return b

            def setDefaultButton(self, *a):
                pass

            def setEscapeButton(self, *a):
                pass

            def exec(self):
                return 0

            def clickedButton(self):
                for b, text in self._buttons:
                    if text == FakeBox.clicked:
                        return b
                return None

        import PySide6.QtWidgets as QW
        real = QW.QMessageBox
        QW.QMessageBox = FakeBox
        try:
            ok_cancel = win._confirm_close_sheet(s1)
            check(calls["n"] == 1, "(e) 交互下弹了一次确认框", "calls=%d" % calls["n"])
            check(ok_cancel is False, "(f) 点「取消」⇒ 返回 False（不删除）")
            FakeBox.clicked = "移除"
            ok_del = win._confirm_close_sheet(s1)
            check(ok_del is True, "(f2) 点「移除」⇒ 返回 True")
        finally:
            QW.QMessageBox = real
    finally:
        try:
            win.shutdown()
        except Exception:                                    # noqa: BLE001
            pass

    print("\n" + "=" * 66)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    print("汇总：全部通过 ✓")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
