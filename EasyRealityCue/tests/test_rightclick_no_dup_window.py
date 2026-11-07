# -*- coding: utf-8 -*-
"""端到端：**时间轴右键不会复制片段**（用户 2026-11-03 03:04）。

用户原话：「在时间轴上对着素材右键两下就会复制一份出来，这个是怎么回事？不需要这个逻辑啊，
这个再制逻辑是长按鼠标左键再按右键，然后再松开左键才是再制啊。」

真因：`timeline_widget.contextMenuEvent` 里有一条旧语义
「已选中片段 + 未在拖动 + 右键 ⇒ 立即复制一份」
⇒ 第一下右键把片段选中（弹菜单），**第二下右键**（此时已选中）就多出一个片段。

本文件走**真窗口**（不是裸 widget）：右键两次 ⇒ 模型里的片段数**一份都不多**；
再验 CDR 手势（左键拖 + 右键 + 松左键）仍然正常产份。

跑法：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe tests\\test_rightclick_no_dup_window.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="cuert_")
os.environ["LOCALAPPDATA"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtCore import QPoint, Qt                                # noqa: E402
from PySide6.QtGui import QContextMenuEvent                         # noqa: E402
from PySide6.QtTest import QTest                                    # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

from ercue.model.cuesheet import CueItem, MediaRef                   # noqa: E402
from ercue.ui import theme                                          # noqa: E402
from ercue.ui.window import MainWindow                              # noqa: E402

_APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def pump(ms=200):
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        _APP.processEvents()
        time.sleep(0.005)


def row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


def main():
    w = MainWindow()
    # ⚠⚠ 必须关掉交互：`_interactive` 默认 **True**，右键会走到 `_build_item_menu().exec()`
    #   ⇒ 无头测试直接**卡死**（我第一次跑就挂在这里，12 秒栈转储才看出来）。
    w._interactive = False
    w.resize(1500, 950)
    w.move(-20000, -20000)
    w.show()
    pump(400)
    w.new_sheet("右键不复制")
    w.add_track(title="T")
    pump(200)

    s = w._sheet()
    ly = s.tracks[0].composition.layers[0]
    s.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=100.0))
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=12.0,
                            src_in_sec=0.0, src_out_sec=8.0, speed=1.0).normalized())
    ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=16.0, end_sec=24.0,
                            src_in_sec=0.0, src_out_sec=8.0, speed=1.0).normalized())
    w._refresh_all()
    tl = w.timeline
    tl.set_view(0.0, 30.0)
    pump(300)

    y = row_y(tl, 0)
    xmid = int((tl._x(4.0) + tl._x(12.0)) / 2)
    print("=" * 78)
    print("【A】对着片段右键（含连点两下）⇒ 一份都不多")
    print("=" * 78)
    n0 = len(ly.items)
    print("  起始片段数 = %d，点在第 %d 行 x=%d" % (n0, 0, xmid))
    for k in (1, 2, 3):
        tl.contextMenuEvent(QContextMenuEvent(
            QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(k, k)))
        pump(120)
        check(len(ly.items) == n0, "(a%d) 第 %d 次右键后片段数仍是 %d" % (k, k, n0),
              len(ly.items))
    check(len(ly.items) == n0, "(a4) 连点三次右键：一份都没多", len(ly.items))
    ids = [x.item_id for x in ly.items]
    check(sorted(ids) == ["i1", "i2"], "(a5) 也没有被「复制+改名」出来的新 id", ids)

    print()
    print("=" * 78)
    print("【B】CDR 手势仍然正常产份（左键拖 + 右键 + 松左键）")
    print("=" * 78)
    n1 = len(ly.items)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    pump(60)
    QTest.mouseMove(tl, QPoint(xmid + 100, y))
    pump(60)
    tl.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Mouse, QPoint(xmid + 100, y), QPoint(9, 9)))
    pump(60)
    check(tl.duplicate_mode() is True, "(b1) 拖动中按右键 ⇒ 进入复制模式")
    check(len(ly.items) == n1, "(b2) 此刻**还不产份**（等松左键）", len(ly.items))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid + 100, y))
    pump(250)
    check(len(ly.items) == n1 + 1, "(b3) 松开左键 ⇒ 多出一个副本", len(ly.items))
    check(tl.duplicate_mode() is False, "(b4) 提交后自动退出复制模式")

    print()
    print("=" * 78)
    print("【C】右键「多选成员」不能被收敛成单选（否则菜单「复制片段」只复制一个）")
    print("=" * 78)
    tl.sel = ("L1", "i1")
    tl.sel_multi.clear()
    tl.sel_multi.update({("L1", "i1"), ("L1", "i2")})
    x2 = int((tl._x(16.0) + tl._x(24.0)) / 2)
    tl.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Mouse, QPoint(x2, y), QPoint(20, 20)))
    pump(150)
    check(tl.multi_selection_count() == 2,
          "(c1) 右键多选里的片段 ⇒ 多选仍是 2 个", tl.multi_selection_count())
    check(len(ly.items) == n1 + 1, "(c2) 而且仍然没产份", len(ly.items))
    ids2 = sorted(x.item_id for x in ly.items)
    print("     当前片段：%s" % ids2)

    print()
    print("=" * 78)
    try:
        w.shutdown()
    except Exception:                                          # noqa: BLE001
        pass
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
