# -*- coding: utf-8 -*-
"""★ T03 独立验证（fresh-eyes 黑盒）—— 24 条 #6/#9/#10/#14/#18 + 既有成果共存。

定位：team-lead 裁定 T03 owner = software-engineer-7；**本脚本由独立验证者撰写**，
不依赖对方的 `tests/test_timeline_snap.py`，用**自建夹具 + 真 QApplication(offscreen)** 重新判据；
逐条打印 `[PASS]/[FAIL]`，末尾给 `IVEIRFY: T03_OK / T03_FAIL`。

⚠ 只读：绝不修改 `timeline_widget.py` 或对方任何测试文件。
⚠ 与对方脚本 `tools/_t24_t03_verify.py` **不同意名**（避免覆盖对方成果）。

跑法：<Cue>\\venv\\Scripts\\python.exe -u tools/_t24_t03_iverify.py
"""
from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

CUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CUE, "src"))

from PySide6.QtCore import QPoint, Qt                                   # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QKeyEvent                 # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest                           # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,         # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                                             # noqa: E402
from ercue.ui.timeline_widget import (CueTimeline, SNAP_TOL_PX,        # noqa: E402
                                      snap_at)

APP = None
RESULTS = []


def _check(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok))
    print("   [%s] %s %s" % ("PASS" if ok else "FAIL", tag, detail))
    return ok


def _build(n=2):
    """一层 n 个片段：i1 4~14、i2 20~30（同一素材 c1）。"""
    tl = CueTimeline()
    tl.resize(1100, 260)
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=300.0, bpm=0.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=14.0,
                            src_in_sec=2.0, src_out_sec=12.0, speed=1.0).normalized())
    if n >= 2:
        ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=20.0, end_sec=30.0,
                                src_in_sec=0.0, src_out_sec=10.0, speed=1.0).normalized())
    te.composition.layers.append(ly)
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=100.0))
    sheet.tracks.append(te)
    tl.set_track(te)
    tl.set_sheet(sheet)
    tl.set_media_paths({"c1": os.path.abspath(__file__)})
    tl.set_view(0.0, 40.0)
    tl.show()
    for _ in range(5):
        APP.processEvents()
    return tl, ly


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


# ==========================================================================
# #6 标尺点击 ⇒ seek_requested（不进入平移、不做拖动 scrub、不取消选择）
# ==========================================================================
def t_6_seek():
    print("--- #6 标尺点击 ⇒ seek_requested ---")
    tl, _ = _build()
    sp = QSignalSpy(tl.seek_requested)
    tl.set_selected("L1", "i1")                 # 有选中 ⇒ 验证"点标尺不取消选择"
    v0 = tl.view0
    x = int(tl._x(12.0))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x, theme.RULER_H // 2))
    _check("#6 点标尺不进入平移 (_pan is None)", tl._pan is None)
    QTest.mouseMove(tl, QPoint(int(tl._x(25.0)), theme.RULER_H // 2))   # 拖动 = 不 scrub
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(25.0)), theme.RULER_H // 2))
    APP.processEvents()
    _check("#6 只发 1 次（无拖动 scrub）", sp.count() == 1, "count=%d" % sp.count())
    if sp.count():
        _check("#6 秒 ≈ 点击处(12)", abs(float(sp.at(0)[0]) - 12.0) < 0.35,
               "got=%.2f" % float(sp.at(0)[0]))
    _check("#6 视图未平移", abs(tl.view0 - v0) < 1e-9)
    _check("#6 点标尺不取消选择", tl.sel == ("L1", "i1"), str(tl.sel))


# ==========================================================================
# #9 剪辑态 hover 把手 ⇒ SizeHorCursor
# ==========================================================================
def t_9_cursor():
    print("--- #9 剪辑态光标 ---")
    tl, ly = _build()
    it1 = ly.items[0]
    y = _row_y(tl)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    _check("#9 双击进剪辑态", tl.edit_mode_item() is it1)
    QTest.mouseMove(tl, QPoint(int(tl._x(it1.at_sec)) + 2, y))
    APP.processEvents()
    _check("#9 hover 左把手 ⇒ SizeHor", tl.cursor().shape() == Qt.SizeHorCursor,
           str(tl.cursor().shape()))
    QTest.mouseMove(tl, QPoint(int(tl._x(it1.end_sec)) - 2, y))
    APP.processEvents()
    _check("#9 hover 右把手 ⇒ SizeHor", tl.cursor().shape() == Qt.SizeHorCursor)
    QTest.mouseMove(tl, QPoint(xmid, y))
    APP.processEvents()
    _check("#9 片段中部 ⇒ Arrow", tl.cursor().shape() == Qt.ArrowCursor)


# ==========================================================================
# #10 点空白 ⇒ picked("","")（且标尺区**不**触发）
# ==========================================================================
def t_10_blank():
    print("--- #10 点空白取消选择 ---")
    tl, ly = _build()
    y = _row_y(tl)
    tl.set_selected("L1", "i1")
    sp = QSignalSpy(tl.picked)
    blank_x = int((tl._x(ly.items[0].end_sec) + tl._x(ly.items[1].at_sec)) / 2)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(blank_x, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(blank_x, y))
    APP.processEvents()
    _check("#10 sel 清空", tl.sel == ("", ""), str(tl.sel))
    _got1 = [tuple(sp.at(i)) for i in range(sp.count())]
    _check("#10 发 picked('','')", ("", "") in _got1, str(_got1))
    # 标尺区不触发 picked("","")
    tl.set_selected("L1", "i1")
    sp2 = QSignalSpy(tl.picked)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(10.0)), 3))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(10.0)), 3))
    APP.processEvents()
    _got2 = [tuple(sp2.at(i)) for i in range(sp2.count())]
    _check("#10 标尺区不发 picked('','')", ("", "") not in _got2, str(_got2))


# ==========================================================================
# #14 snap_at 纯函数 + 控件拖动吸附（只动被拖的）
# ==========================================================================
def t_14_snap():
    print("--- #14 自动吸附 ---")
    a, tt = snap_at(9.9, 2.0, [10.0, 20.0], 10.0)
    _check("#14 左端吸附 9.9→10", abs(a - 10.0) < 1e-9 and abs(tt - 10.0) < 1e-9)
    a, tt = snap_at(7.5, 3.0, [10.0], 10.0)
    _check("#14 右端吸附 ⇒ at=7", abs(a - 7.0) < 1e-9 and abs(tt - 10.0) < 1e-9)
    a, tt = snap_at(50.0, 2.0, [10.0, 20.0], 10.0)
    _check("#14 太远不吸", abs(a - 50.0) < 1e-9 and tt is None)
    a, tt = snap_at(9.05, 2.0, [10.0], 10.0)
    _check("#14 阈值外不吸(0.95>0.8)", tt is None)
    a, tt = snap_at(9.9, 2.0, [10.0], 0.0)
    _check("#14 px_per_sec=0 不吸", tt is None)
    a, tt = snap_at(9.9, 2.0, [], 10.0)
    _check("#14 空 targets 不吸", tt is None)
    _check("#14 SNAP_TOL_PX==8", SNAP_TOL_PX == 8.0)

    tl, ly = _build()
    it1, it2 = ly.items[0], ly.items[1]
    _check("#14 默认关", tl.snap_enabled() is False)
    tl.set_snap(True)
    y = _row_y(tl)
    xc = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    move_x = int(tl._x(19.9) + xc - tl._x(it1.at_sec))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xc, y))
    QTest.mouseMove(tl, QPoint(move_x, y))
    APP.processEvents()
    _check("#14 拖动吸到 20", tl._drag and abs(tl._drag.get("target") - 20.0) < 1e-6,
           str(tl._drag.get("target") if tl._drag else None))
    _check("#14 导引线=20", tl._snap_guide is not None and abs(tl._snap_guide - 20.0) < 1e-6)
    _check("#14 邻片段未被动", abs(it2.at_sec - 20.0) < 1e-9 and abs(it2.end_sec - 30.0) < 1e-9)
    sp = QSignalSpy(tl.moved)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(move_x, y))
    APP.processEvents()
    _check("#14 松手 moved(20)", sp.count() == 1 and abs(sp.at(0)[2] - 20.0) < 1e-6)


# ==========================================================================
# #18 再制状态机（按选中分流）
# ==========================================================================
def t_18_dup():
    print("--- #18 再制状态机 ---")
    tl, ly = _build()
    it1 = ly.items[0]
    y = _row_y(tl)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    dup, menu = QSignalSpy(tl.duplicate_requested), QSignalSpy(tl.context_menu)

    # 未选中 ⇒ 菜单（不发 duplicate）
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(1, 1)))
    APP.processEvents()
    _check("#18 未选中⇒菜单", menu.count() == 1 and dup.count() == 0 and not tl.duplicate_mode())
    _check("#18 右键后片段变选中", tl.sel == ("L1", "i1"))
    # 已选中 ⇒ 直接再制（不弹菜单）
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(2, 2)))
    APP.processEvents()
    _check("#18 已选中⇒再制", dup.count() == 1 and tl.duplicate_mode())
    _check("#18 再制不发菜单", menu.count() == 1, "menu=%d" % menu.count())
    _check("#18 光标 DragCopy", tl.cursor().shape() == Qt.DragCopyCursor)
    # 再制态左键拖 ⇒ 落点产份（原地不动）
    move_x = int(tl._x(10.0) + xmid - tl._x(it1.at_sec))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    QTest.mouseMove(tl, QPoint(move_x, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(move_x, y))
    APP.processEvents()
    _check("#18 拖放再产一份", dup.count() == 2 and tl.duplicate_mode())
    _check("#18 原片段留在原地(4.0)", abs(it1.at_sec - 4.0) < 1e-9)
    # 再制态右键 ⇒ 退出
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(3, 3)))
    APP.processEvents()
    _check("#18 再制态右键⇒退出", not tl.duplicate_mode() and menu.count() == 1 and dup.count() == 2)
    # Esc ⇒ 退出
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(4, 4)))
    APP.processEvents()
    tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    APP.processEvents()
    _check("#18 Esc⇒退出", not tl.duplicate_mode())
    # 空白右键 ⇒ 仅粘贴菜单（item=""）
    m2 = QSignalSpy(tl.context_menu)
    bx = int((tl._x(it1.end_sec) + tl._x(ly.items[1].at_sec)) / 2)
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(bx, y), QPoint(9, 9)))
    APP.processEvents()
    _check("#18 空白右键 item=''", m2.count() == 1 and m2.at(0)[0] == "" and m2.at(0)[1] == "")


# ==========================================================================
# 既有成果共存：trimmed / context_menu / set_clip_waves / A1 / A2
# ==========================================================================
def t_regress_coexist():
    print("--- 既有成果共存 ---")
    tl, ly = _build()
    it1 = ly.items[0]
    y = _row_y(tl)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    # context_menu 签名仍在
    _check("既有 context_menu 信号存在", hasattr(tl, "context_menu"))
    _check("既有 trimmed 信号存在", hasattr(tl, "trimmed"))
    # set_clip_waves / set_show_clip_audio API
    _check("既有 set_clip_waves", callable(getattr(tl, "set_clip_waves", None)))
    _check("既有 set_show_clip_audio", callable(getattr(tl, "set_show_clip_audio", None)))
    tl.set_clip_waves({}); tl.set_show_clip_audio(False)
    sh, n = tl.clip_audio_status()
    _check("既有 clip_audio_status", sh is False and n == 0, str((sh, n)))
    # A1：剪辑态按下右把手不移动 ⇒ 不崩、不发 trimmed（pv is None 兜底）
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    sp = QSignalSpy(tl.trimmed)
    hr = QPoint(int(tl._x(it1.end_sec)) - 2, y)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, hr)
    APP.processEvents()
    _check("A1 按下把手 preview 非 None", tl._trim and tl._trim.get("preview") is not None)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, hr)
    APP.processEvents()
    _check("A1 未移动 ⇒ 不发 trimmed", sp.count() == 0, "count=%d" % sp.count())
    # A1b：拖右把手 ⇒ 发 trimmed
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, hr)
    QTest.mouseMove(tl, QPoint(int(tl._x(it1.end_sec)) + 60, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(it1.end_sec)) + 60, y))
    APP.processEvents()
    _check("既有 trimmed 仍可用", sp.count() == 1, "count=%d" % sp.count())
    # A2：拖动时 _preview_dx 生效（块跟手）
    tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    QTest.mouseMove(tl, QPoint(xmid + 50, y))
    APP.processEvents()
    _check("A2 拖动 _preview_dx 生效", tl._drag is not None and abs(tl._preview_dx) > 1.0,
           "dx=%.1f" % tl._preview_dx)


def main():
    global APP
    APP = QApplication.instance() or QApplication([])
    print("=" * 74)
    print("T03 · 独立黑盒验证（#6/#9/#10/#14/#18 + 共存回归）")
    print("=" * 74)
    for fn in (t_6_seek, t_9_cursor, t_10_blank, t_14_snap, t_18_dup, t_regress_coexist):
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            RESULTS.append((fn.__name__ + " (exception)", False))
            print("   [FAIL] %s 抛异常: %s\n%s" % (fn.__name__, e, traceback.format_exc()))
    n_ok = sum(1 for _t, ok in RESULTS if ok)
    n = len(RESULTS)
    print("=" * 74)
    print("判据 %d/%d 通过" % (n_ok, n))
    bad = [t for t, ok in RESULTS if not ok]
    if bad:
        print("未通过：", "; ".join(bad))
    print("IVEIRFY:", "T03_OK" if n_ok == n else "T03_FAIL")
    print("=" * 74)
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
