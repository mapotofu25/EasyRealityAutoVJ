# -*- coding: utf-8 -*-
"""★ 第六轮独立复验（fresh eyes）—— ① CDR 再制手感 + ③ 双击拖把手改长短。

独立实现：自建夹具 + 真 QApplication(offscreen) + QSignalSpy；**不引用作者脚本/测试 helper**。
逐条打印 [PASS]/[FAIL]，末尾 `FIX6_IVEIRFY: OK / FAIL`。
只读：不改源码、不改作者测试。

跑法：<Cue>\\venv\\Scripts\\python.exe -u tools/_fix6_indep_verify.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

CUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CUE, "src"))

from PySide6.QtCore import QPoint, Qt                                    # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QKeyEvent                   # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest                            # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,          # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                                              # noqa: E402
from ercue.ui.timeline_widget import CueTimeline                        # noqa: E402

APP = None
RESULTS = []


def _chk(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok))
    print("   [%s] %s %s" % ("PASS" if ok else "FAIL", tag, detail))
    return ok


def _spy_list(sp):
    return [tuple(sp.at(i)) for i in range(sp.count())]


# ==========================================================================
# 夹具（widget 级）
# ==========================================================================
def _widget_clip(at=4.0, end=14.0, view0=0.0, span=40.0):
    tl = CueTimeline()
    tl.resize(1100, 260)
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=300.0, bpm=0.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=at, end_sec=end,
                            src_in_sec=0.0,
                            src_out_sec=(end - at),
                            speed=1.0).normalized())
    te.composition.layers.append(ly)
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=600.0))
    sheet.tracks.append(te)
    tl.set_track(te)
    tl.set_sheet(sheet)
    tl.set_media_paths({"c1": os.path.abspath(__file__)})
    tl.set_view(view0, span)
    tl.show()
    for _ in range(4):
        APP.processEvents()
    return tl, ly


def _row_y(tl):
    return int(theme.RULER_H + tl.row_h / 2)


def _ctx(tl, x, y, gx=5, gy=5):
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                          QPoint(int(x), int(y)), QPoint(gx, gy)))
    APP.processEvents()


# ==========================================================================
# ① CDR 再制 —— 用户权威流程：按下→拖动→右键→松开右键→松开左键
# ==========================================================================
def t1_cdr_gesture_widget():
    print("--- ① CDR 流程（widget 级：信号/状态） ---")
    tl, ly = _widget_clip()
    it1 = ly.items[0]
    y = _row_y(tl)
    px = int(tl._x(9.0))                 # 按下：片段中部（远离两端，确保是拖动）
    mx = int(tl._x(17.0))                # 拖到 17s ⇒ dx = -5s ⇒ 副本落 ≈12s
    dup, moved = QSignalSpy(tl.duplicate_requested), QSignalSpy(tl.moved)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(px, y))
    QTest.mouseMove(tl, QPoint(mx, y))
    APP.processEvents()
    _chk("①.1 拖动已建立 _drag", tl._drag is not None)
    _chk("①.2 拖动期 _preview_dx≠0（跟手）", abs(tl._preview_dx) > 1.0)
    _ctx(tl, mx, y)                      # 按右键 ⇒ 进复制模式
    _chk("①.3 右键后进入复制模式", tl.duplicate_mode() is True)
    _chk("①.4 复制模式光标=DragCopy", tl.cursor().shape() == Qt.DragCopyCursor)
    _chk("①.5 右键**未产份**", dup.count() == 0, "dup=%d" % dup.count())
    _chk("①.6 右键**未弹菜单**（_drag 仍在）", tl._drag is not None)
    QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(mx, y))   # 松开右键（无动作）
    APP.processEvents()
    _chk("①.7 松开右键后仍复制模式", tl.duplicate_mode() is True)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mx, y))    # 松开左键 ⇒ 提交
    APP.processEvents()
    got = _spy_list(dup)
    _chk("①.8 松左键 ⇒ 恰 1 次 duplicate_requested", len(got) == 1, str(got))
    if got:
        exp = tl._sec(mx) + (tl._sec(px) - tl._sec(px))  # 见下：期望 = 拖动目标
        exp_target = tl._sec(tl._x(17.0)) + (4.0 - 9.0)   # = 12.0
        _chk("①.9 副本落点≈拖动目标(12.0)", abs(got[0][2] - exp_target) < 0.2,
             "got=%.3f exp=%.3f" % (got[0][2], exp_target))
    _chk("①.10 **不发 moved**（原件不动）", moved.count() == 0, "moved=%d" % moved.count())
    _chk("①.11 提交后自动退出复制模式", tl.duplicate_mode() is False)
    _chk("①.12 光标回箭头", tl.cursor().shape() == Qt.ArrowCursor)
    _chk("①.13 原件模型未被动（widget 不改模型）", abs(it1.at_sec - 4.0) < 1e-9)


def t2_cdr_cancel_esc():
    print("--- ① 取消路径 A：复制模式中按 Esc ---")
    tl, ly = _widget_clip()
    it1 = ly.items[0]
    y = _row_y(tl)
    px, mx = int(tl._x(9.0)), int(tl._x(17.0))
    dup, moved = QSignalSpy(tl.duplicate_requested), QSignalSpy(tl.moved)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(px, y))
    QTest.mouseMove(tl, QPoint(mx, y))
    _ctx(tl, mx, y)
    _chk("②A.1 已进入复制模式", tl.duplicate_mode() is True)
    tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    APP.processEvents()
    _chk("②A.2 Esc 后退出复制模式", tl.duplicate_mode() is False)
    _chk("②A.3 Esc 清掉在途手势（_drag=None）", tl._drag is None)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mx, y))
    APP.processEvents()
    _chk("②A.4 Esc 后松左键 ⇒ 不发副本", dup.count() == 0, "dup=%d" % dup.count())
    _chk("②A.5 Esc 后松左键 ⇒ 不发 moved", moved.count() == 0, "moved=%d" % moved.count())
    _chk("②A.6 原件留在原位(4.0)", abs(it1.at_sec - 4.0) < 1e-9)


def t3_cdr_cancel_rightclick():
    print("--- ① 取消路径 B：复制模式中再按一次右键 ---")
    tl, ly = _widget_clip()
    y = _row_y(tl)
    px, mx = int(tl._x(9.0)), int(tl._x(17.0))
    dup, moved = QSignalSpy(tl.duplicate_requested), QSignalSpy(tl.moved)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(px, y))
    QTest.mouseMove(tl, QPoint(mx, y))
    _ctx(tl, mx, y)
    _chk("②B.1 已进入复制模式", tl.duplicate_mode() is True)
    _ctx(tl, mx, y)                        # 再按右键 ⇒ 取消
    _chk("②B.2 再次右键 ⇒ 取消复制模式", tl.duplicate_mode() is False)
    _chk("②B.3 取消失败项：未产份", dup.count() == 0, "dup=%d" % dup.count())
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mx, y))
    APP.processEvents()
    _chk("②B.4 取消后松左键 ⇒ 不发 moved", moved.count() == 0, "moved=%d" % moved.count())


def t4_old_semantics_not_degraded():
    print("--- ① 旧语义不退化：普通右键（已选中、未拖动）⇒ 立即复制一份且不残留模式 ---")
    tl, ly = _widget_clip()
    it1 = ly.items[0]
    y = _row_y(tl)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    tl.set_selected("L1", "i1")
    dup, menu = QSignalSpy(tl.duplicate_requested), QSignalSpy(tl.context_menu)
    _ctx(tl, xmid, y)
    _chk("③.1 已选中+未拖动 ⇒ 立即发 duplicate_requested", dup.count() == 1, "dup=%d" % dup.count())
    _chk("③.2 不弹菜单", menu.count() == 0, "menu=%d" % menu.count())
    _chk("③.3 **不残留复制模式**（一次性）", tl.duplicate_mode() is False)
    # 未选中 ⇒ 菜单（不发 dup）
    tl.set_selected("", "")
    _ctx(tl, xmid, y)
    _chk("③.4 未选中 ⇒ 发 context_menu", menu.count() == 1 and dup.count() == 1,
         "menu=%d dup=%d" % (menu.count(), dup.count()))


def t5_control_no_rightclick_moves_original():
    print("--- ① 对照/反向：不做右键拦截 ⇒ 原件被 moved 搬走 ---")
    tl, ly = _widget_clip()
    it1 = ly.items[0]
    y = _row_y(tl)
    px, mx = int(tl._x(9.0)), int(tl._x(17.0))
    dup, moved = QSignalSpy(tl.duplicate_requested), QSignalSpy(tl.moved)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(px, y))
    QTest.mouseMove(tl, QPoint(mx, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mx, y))   # 无右键 ⇒ 普通移动
    APP.processEvents()
    got = _spy_list(moved)
    _chk("④.1 无右键拦截 ⇒ 发 moved（原件会被搬走）", len(got) == 1, str(got))
    _chk("④.2 且不发 duplicate_requested", dup.count() == 0, "dup=%d" % dup.count())
    if got:
        _chk("④.3 moved 落点≈12.0（=12.0 处被搬走）", abs(got[0][2] - 12.0) < 0.2,
             "got=%.3f" % got[0][2])


# ==========================================================================
# ③ 双击后拖把手改长短（长片段 + 窄视口）
# ==========================================================================
def t6_trim_longclip_edge():
    print("--- ③ 复现场景：长片段(4~120) 在窄视口(0~40) 拖视口边缘把手 ---")
    tl, ly = _widget_clip(at=4.0, end=120.0, view0=0.0, span=40.0)
    it1 = ly.items[0]
    y = _row_y(tl)
    W = tl.width()
    hx_r = W - 3                          # 视口右缘（把手画在钳位后的 W 处）
    # 命中判定：应判到右把手（旧实现会判空 ⇒ 变"移动整块"）
    _ly, _it, _row, edge_r = tl._hit(QPoint(hx_r, y))
    _chk("③.1 _hit 在视口右缘判到右把手", _it is it1 and edge_r == "r", "edge=%r" % edge_r)
    # 双击进剪辑态
    xmid = int((tl._x(4.0) + W) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    _chk("③.2 双击进剪辑态", tl.edit_mode_item() is it1)
    trimmed, moved = QSignalSpy(tl.trimmed), QSignalSpy(tl.moved)
    # 拖右把手向左（把 end 从 120 改小）
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(hx_r, y))
    _chk("③.3 按把手 ⇒ 进 trim（非拖动整块）", tl._trim is not None and tl._drag is None)
    QTest.mouseMove(tl, QPoint(W - 200, y))
    APP.processEvents()
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(W - 200, y))
    APP.processEvents()
    tg = _spy_list(trimmed)
    _chk("③.4 松手发 trimmed（end 变）", len(tg) == 1, str(tg))
    _chk("③.5 **不是**把整块搬走（moved=0）", moved.count() == 0, "moved=%d" % moved.count())
    if tg:
        _chk("③.6 trimmed 的 end < 120", tg[0][3] is not None and tg[0][3] < 120.0,
             "end=%s" % (tg[0][3],))
    # 左把手（本例左端 4.0 在视口内）
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(4.0)) + 2, y))
    _chk("③.7 按左把手 ⇒ 进 trim", tl._trim is not None)
    QTest.mouseMove(tl, QPoint(int(tl._x(4.0)) + 120, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(4.0)) + 120, y))
    APP.processEvents()
    tg2 = _spy_list(trimmed)
    _chk("③.8 拖左把手 ⇒ 再发 trimmed（at 变）", len(tg2) == 2 and tg2[1][2] > 4.0, str(tg2))


def t7_resize_path_longclip():
    print("--- ③ 普通 resize 路径（非双击态）也用钳位命中 ---")
    tl, ly = _widget_clip(at=4.0, end=120.0, view0=0.0, span=40.0)
    it1 = ly.items[0]
    y = _row_y(tl)
    W = tl.width()
    resized, moved = QSignalSpy(tl.resized), QSignalSpy(tl.moved)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(W - 3, y))
    _chk("③R.1 视口右缘按下 ⇒ 进 _resize（非拖动整块）", tl._resize is not None and tl._drag is None)
    QTest.mouseMove(tl, QPoint(W - 180, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(W - 180, y))
    APP.processEvents()
    rs = _spy_list(resized)
    _chk("③R.2 松手发 resized", len(rs) == 1, str(rs))
    _chk("③R.3 不发 moved", moved.count() == 0, "moved=%d" % moved.count())


# ==========================================================================
# ③ 常规路径（window 级）：双击→拖右把手⇒模型 end 变→undo 还原
# ==========================================================================
def _mk_mp4(path):
    import av
    import numpy as np
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=10)
    st.width, st.height = 320, 180
    st.pix_fmt = "yuv420p"
    for i in range(60):
        img = np.zeros((180, 320, 3), np.uint8)
        img[:, :, 2] = 180
        img[:, (i * 12) % 320:((i * 12) % 320) + 24, 1] = 255
        for pk in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def t8_window_trim_and_undo():
    print("--- ③ 常规路径（window 级）：双击拖右把手 ⇒ 模型 end 变 ⇒ undo 还原 ---")
    from ercue.ui.window import MainWindow
    try:
        w = MainWindow(restore_workspace=False, autosave=False)
    except TypeError:
        w = MainWindow()
    w.thumbs_enabled = False
    w._interactive = False
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1280, 860)
    w.show()
    for _ in range(6):
        APP.processEvents()
    try:
        w.new_sheet("FIX6", 1920, 1080)
        ids = w.add_media_paths([MP4])
        cid = ids[0]
        w.add_track("曲", "a", 300.0, 120.0)
        ly = w._layers()[0]
        it1 = w.add_cue(ly.lyr_id, cid, 4.0)
        it1.end_sec = 14.0
        w._rebuild_runtime()
        tl = w.timeline
        tl.resize(1100, 260)
        tl.set_view(0.0, 40.0)
        for _ in range(6):
            APP.processEvents()
        y = int(theme.RULER_H + tl.row_h / 2)
        xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
        APP.processEvents()
        _chk("③W.1 双击进剪辑态", tl.edit_mode_item() is it1)
        end0 = float(it1.end_sec)
        hr = QPoint(int(tl._x(it1.end_sec)) - 2, y)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, hr)
        QTest.mouseMove(tl, QPoint(int(tl._x(it1.end_sec)) - 120, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(it1.end_sec)) - 120, y))
        APP.processEvents()
        end1 = float(it1.end_sec)
        _chk("③W.2 模型 end 真的变小", end1 < end0 - 0.2, "end %.2f→%.2f" % (end0, end1))
        w.undo()
        APP.processEvents()
        ly2, it2 = w._find_item(ly.lyr_id, it1.item_id)
        _chk("③W.3 undo 还原 end", abs(float(it2.end_sec) - end0) < 1e-6,
             "end=%.2f (期望 %.2f)" % (float(it2.end_sec), end0))
    finally:
        try:
            w.shutdown()
        except Exception:                              # noqa: BLE001
            pass


def t9_window_cdr_counts():
    print("--- ① CDR（window 级）：片段 1→2 + 原件 4.0 + 副本≈12.0 ---")
    from ercue.ui.window import MainWindow
    try:
        w = MainWindow(restore_workspace=False, autosave=False)
    except TypeError:
        w = MainWindow()
    w.thumbs_enabled = False
    w._interactive = False
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1280, 860)
    w.show()
    for _ in range(6):
        APP.processEvents()
    try:
        w.new_sheet("FIX6D", 1920, 1080)
        ids = w.add_media_paths([MP4])
        cid = ids[0]
        w.add_track("曲", "a", 300.0, 120.0)
        ly = w._layers()[0]
        it1 = w.add_cue(ly.lyr_id, cid, 4.0)
        it1.end_sec = 14.0
        w._rebuild_runtime()
        tl = w.timeline
        tl.resize(1100, 260)
        tl.set_view(0.0, 40.0)
        for _ in range(6):
            APP.processEvents()
        y = int(theme.RULER_H + tl.row_h / 2)
        n0 = len(ly.items)
        px, mx = int(tl._x(9.0)), int(tl._x(17.0))
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(px, y))
        QTest.mouseMove(tl, QPoint(mx, y))
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                              QPoint(mx, y), QPoint(5, 5)))
        APP.processEvents()
        QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(mx, y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(mx, y))
        APP.processEvents()
        n1 = len(ly.items)
        _chk("①W.1 片段数 1→2", n0 == 1 and n1 == 2, "%d→%d" % (n0, n1))
        _chk("①W.2 原件仍在 4.0", abs(float(it1.at_sec) - 4.0) < 1e-9, "%.3f" % it1.at_sec)
        dup = [x for x in ly.items if x.item_id != it1.item_id]
        _chk("①W.3 副本落点≈12.0", dup and abs(float(dup[0].at_sec) - 12.0) < 0.3,
             "%.3f" % (dup[0].at_sec if dup else -1))
        _chk("①W.4 副本时长与原一致(10s)", dup and abs(
            float(dup[0].end_sec) - float(dup[0].at_sec) - 10.0) < 1e-6)
        _chk("①W.5 收尾自动退出复制模式", tl.duplicate_mode() is False)
    finally:
        try:
            w.shutdown()
        except Exception:                              # noqa: BLE001
            pass


# ==========================================================================
def main():
    global APP, MP4
    APP = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="ercue_fix6_")
    MP4 = os.path.join(tmp, "clip.mp4")
    _mk_mp4(MP4)
    print("=" * 74)
    print("第六轮独立复验 —— ① CDR 再制 / ③ 双击拖把手（长片段+窄视口）")
    print("=" * 74)
    for fn in (t1_cdr_gesture_widget, t2_cdr_cancel_esc, t3_cdr_cancel_rightclick,
               t4_old_semantics_not_degraded, t5_control_no_rightclick_moves_original,
               t6_trim_longclip_edge, t7_resize_path_longclip, t8_window_trim_and_undo,
               t9_window_cdr_counts):
        try:
            fn()
        except Exception as e:                          # noqa: BLE001
            RESULTS.append((fn.__name__ + " (exception)", False))
            print("   [FAIL] %s 抛异常: %s\n%s" % (fn.__name__, e, traceback.format_exc()))
    n_ok = sum(1 for _t, ok in RESULTS if ok)
    n = len(RESULTS)
    print("=" * 74)
    print("判据 %d/%d 通过" % (n_ok, n))
    bad = [t for t, ok in RESULTS if not ok]
    if bad:
        print("未通过：", "; ".join(bad))
    print("FIX6_IVEIRFY:", "OK" if n_ok == n else "FAIL")
    print("=" * 74)
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
