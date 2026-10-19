# -*- coding: utf-8 -*-
"""★ 第九/十轮独立复验（fresh eyes）—— 框选多选（250ms 手势）+ 组操作 + 五退出路径 + 防回归。

独立实现：自建夹具 + 真 QApplication(offscreen) + QSignalSpy；不引用作者脚本。
逐条 [PASS]/[FAIL]，末尾 `FIX10_IVEIRFY: OK / FAIL`。
只读：不改源码、不改作者测试/脚本。

跑法：<Cue>\\venv\\Scripts\\python.exe -u tools/_fix10_indep_verify.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

CUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CUE, "src"))

from PySide6.QtCore import QPoint, Qt                                     # noqa: E402
from PySide6.QtGui import QKeyEvent                                      # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest                             # noqa: E402
from PySide6.QtWidgets import QApplication                               # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,           # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                                               # noqa: E402
from ercue.ui.timeline_widget import CueTimeline, BOX_PAN_MS             # noqa: E402

APP = None
MP4 = None
RESULTS = []


def _chk(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok))
    print("   [%s] %s %s" % ("PASS" if ok else "FAIL", tag, detail))
    return ok


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


# ==========================================================================
# widget 夹具（可控 2 层）
# ==========================================================================
def _clip(iid, at, end, src_dur=600.0):
    return CueItem(item_id=iid, clip_id="c1", at_sec=at, end_sec=end,
                   src_in_sec=0.0, src_out_sec=(end - at), speed=1.0).normalized()


def _widget(layers_spec, view_span=40.0):
    """layers_spec: list[(z, [CueItem...])] —— z 越大越靠上行。"""
    tl = CueTimeline()
    tl.resize(1100, 320)
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=600.0, bpm=0.0))
    te.identity.ensure_meta_id()
    for z, items in layers_spec:
        ly = CompLayer(lyr_id="Lz%d" % z, name="L%d" % z, z=z)
        ly.items.extend(items)
        te.composition.layers.append(ly)
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=600.0))
    sheet.tracks.append(te)
    tl.set_track(te)
    tl.set_sheet(sheet)
    tl.set_media_paths({"c1": os.path.abspath(__file__)})
    tl.set_view(0.0, view_span)
    tl.show()
    for _ in range(4):
        APP.processEvents()
    return tl


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


def _press_blank(tl, x, y):
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(x), int(y)))
    APP.processEvents()


def _set_elapsed(tl, ms):
    """把本次空白按下的 '已过毫秒' 精确设为 ms（规避 sleep 抖动，确定性测阈值）。"""
    if tl._blank is not None:
        tl._blank["t0"] = time.monotonic() - (ms / 1000.0)


def _drag_layers_for_box_hit():
    """L10(row0): A 4~10, C 20~30 ; L0(row1): B 5~12, D 14~22。"""
    return [(10, [_clip("A", 4.0, 10.0), _clip("C", 20.0, 30.0)]),
            (0, [_clip("B", 5.0, 12.0), _clip("D", 14.0, 22.0)])]


# ==========================================================================
# ① 手势判定
# ==========================================================================
def t1_immediate_move_is_box():
    print("--- ① 立即移动 ⇒ 框选（view0 不变、橡皮筋出现） ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0), _clip("B", 12.0, 18.0)])])
    y = _row_y(tl)
    p0 = (tl._x(19.0), y)               # 空白（B 止于 18）
    v0 = tl.view0
    _press_blank(tl, *p0)
    _chk("①.1 空白按下先记 _blank（未立即平移）", tl._blank is not None and tl._pan is None)
    _chk("①.2 按下时未清选中（延迟判定）", tl.sel_multi == set())
    _set_elapsed(tl, 30.0)              # 立即移动
    QTest.mouseMove(tl, QPoint(int(p0[0] - 25), y))
    APP.processEvents()
    _chk("①.3 立即移动 ⇒ mode=box、_box 出现", tl._blank and tl._blank["mode"] == "box"
         and tl._box is not None)
    _chk("①.4 框选光标=Cross", tl.cursor().shape() == Qt.CrossCursor)
    QTest.mouseMove(tl, QPoint(int(tl._x(3.0)), y))
    APP.processEvents()
    tl.grab()                            # 真画一次橡皮筋（不崩）
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(3.0)), y))
    APP.processEvents()
    _chk("①.5 松手 view0 不变（框选≠平移）", abs(tl.view0 - v0) < 1e-9, "v0=%.3f now=%.3f" % (v0, tl.view0))
    _chk("①.6 命中 A、B 两个 ⇒ multi>=2", tl.multi_selection_count() >= 2,
         "ids=%s" % (sorted(tl.sel_multi),))


def t2_longpress_then_move_is_pan():
    print("--- ① 长按(≥250ms)再移动 ⇒ 平移（view0 变、不进多选） ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0), _clip("B", 12.0, 18.0)])])
    y = _row_y(tl)
    p0 = (tl._x(19.0), y)
    v0 = tl.view0
    _press_blank(tl, *p0)
    _set_elapsed(tl, 320.0)             # 长按 >250ms
    QTest.mouseMove(tl, QPoint(int(p0[0] - 60), y))
    APP.processEvents()
    _chk("①.7 长按后移动 ⇒ 进 _pan", tl._pan is not None)
    _chk("①.8 长按后移动 ⇒ 不进多选(_box None)", tl._box is None)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(p0[0] - 60), y))
    APP.processEvents()
    _chk("①.9 平移生效 view0 改变", abs(tl.view0 - v0) > 1e-6, "v0=%.3f now=%.3f" % (v0, tl.view0))
    _chk("①.10 平移不产生多选", tl.sel_multi == set())


def t3_threshold_boundary():
    print("--- ① 阈值边界：200ms⇒框选 / 300ms⇒平移 / 249⇒框 / 251⇒平移 ---")
    for ms, want in ((200.0, "box"), (300.0, "pan"), (249.0, "box"), (251.0, "pan")):
        tl = _widget([(0, [_clip("A", 4.0, 10.0)])])
        y = _row_y(tl)
        p0 = (tl._x(20.0), y)
        _press_blank(tl, *p0)
        _set_elapsed(tl, ms)
        QTest.mouseMove(tl, QPoint(int(p0[0] - 40), y))
        APP.processEvents()
        mode = (tl._blank or {}).get("mode") if tl._pan is None else "pan"
        _chk("①.%s %sms ⇒ %s" % (int(ms // 100) + 10, int(ms), want), mode == want,
             "mode=%r" % (mode,))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(p0[0] - 40), y))
        APP.processEvents()


def t4_real_sleep_longpress_is_pan():
    print("--- ① 真实计时：按住真睡 0.35s 再动 ⇒ 平移 ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0)])])
    y = _row_y(tl)
    p0 = (tl._x(20.0), y)
    v0 = tl.view0
    _press_blank(tl, *p0)
    time.sleep(0.35)
    QTest.mouseMove(tl, QPoint(int(p0[0] - 60), y))
    APP.processEvents()
    _chk("①.20 真睡 0.35s ⇒ 平移(_pan 非空)", tl._pan is not None)
    _chk("①.21 真睡 0.35s ⇒ view0 改变", abs(tl.view0 - v0) > 1e-6)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(p0[0] - 60), y))
    APP.processEvents()


def t5_blank_click_no_drag_cancels():
    print("--- ① 点空白不拖 ⇒ 取消选择（原行为保留，含清多选） ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0), _clip("B", 12.0, 18.0)])])
    y = _row_y(tl)
    tl.sel_multi = {("Lz0", "A"), ("Lz0", "B")}
    tl.sel = ("Lz0", "A")
    p0 = (tl._x(19.0), y)
    sp = QSignalSpy(tl.picked)
    _press_blank(tl, *p0)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(p0[0]), y))
    APP.processEvents()
    _chk("①.30 点空白 ⇒ 清多选", tl.sel_multi == set())
    _chk("①.31 点空白 ⇒ sel 清空", tl.sel == ("", ""))
    _chk("①.32 点空白 ⇒ 发 picked('','')", ("", "") in [tuple(sp.at(i)) for i in range(sp.count())])


def t6_middle_drag_pans_immediately():
    print("--- ① 中键拖 ⇒ 立即平移（不判阈值、不框选） ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0)])])
    y = _row_y(tl)
    v0 = tl.view0
    p0 = QPoint(int(tl._x(20.0)), y)
    QTest.mousePress(tl, Qt.MiddleButton, Qt.NoModifier, p0)
    APP.processEvents()
    _chk("①.40 中键按下 ⇒ 立即 _pan", tl._pan is not None)
    _chk("①.41 中键按下 ⇒ 不进 box/_blank", tl._box is None and tl._blank is None)
    QTest.mouseMove(tl, QPoint(p0.x() - 80, y))
    QTest.mouseRelease(tl, Qt.MiddleButton, Qt.NoModifier, QPoint(p0.x() - 80, y))
    APP.processEvents()
    _chk("①.42 中键拖 ⇒ view0 改变", abs(tl.view0 - v0) > 1e-6)


def t7_ruler_drag_seeks_not_box():
    print("--- ① 标尺拖 ⇒ seek（不框选） ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0)])])
    sp = QSignalSpy(tl.seek_requested)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(8.0)), 3))
    QTest.mouseMove(tl, QPoint(int(tl._x(15.0)), 3))
    QTest.mouseMove(tl, QPoint(int(tl._x(20.0)), 3))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(20.0)), 3))
    APP.processEvents()
    _chk("①.50 标尺 ⇒ 发 seek（≥1 次）", sp.count() >= 1, "count=%d" % sp.count())
    _chk("①.51 标尺 ⇒ 不框选(_box None)", tl._box is None)
    _chk("①.52 标尺 ⇒ 不进多选", tl.sel_multi == set())


# ==========================================================================
# ② 框选命中（跨层 / 部分相交 / 完全不碰）
# ==========================================================================
def t8_box_hit_crosslayer_partial_none():
    print("--- ② 框选命中：跨层 + 部分相交 + 完全不碰 ---")
    tl = _widget(_drag_layers_for_box_hit())
    y0 = _row_y(tl, 0)
    y1 = _row_y(tl, 1)
    # 从 (x=16s,row0) 拖到 (x=3s,row1) —— 框覆盖 x[3,16] 且跨两行
    _press_blank(tl, tl._x(16.0), y0)
    _set_elapsed(tl, 20.0)
    QTest.mouseMove(tl, QPoint(int(tl._x(16.0) - 25), y0))
    APP.processEvents()
    QTest.mouseMove(tl, QPoint(int(tl._x(3.0)), y1))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(3.0)), y1))
    APP.processEvents()
    ids = set(tl.sel_multi)
    _chk("②.1 跨层：row0 的 A 与 row1 的 B 都选中", ("Lz10", "A") in ids and ("Lz0", "B") in ids, str(sorted(ids)))
    _chk("②.2 部分相交：D(14~22) 只被压到 14~16 ⇒ 仍选中", ("Lz0", "D") in ids)
    _chk("②.3 完全没碰到：C(20~30) 不在框内 ⇒ 不选", ("Lz10", "C") not in ids)
    _chk("②.4 命中数恰为 3", len(ids) == 3, "n=%d %s" % (len(ids), sorted(ids)))


# ==========================================================================
# ⑦ 第七轮防回归：单击不能拖两端；双击才能 trim
# ==========================================================================
def t9_round7_singleclick_no_trim():
    print("--- ⑦ 第七轮：单击边缘 ⇒ _drag（非 trim）；双击 ⇒ trim ---")
    tl = _widget([(0, [_clip("A", 4.0, 14.0)])])
    y = _row_y(tl)
    xr = int(tl._x(14.0)) - 2               # 右边缘
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xr, y))
    APP.processEvents()
    _chk("⑦.1 非剪辑态单击右边缘 ⇒ _drag（非 _trim）", tl._drag is not None and tl._trim is None)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xr, y))
    APP.processEvents()
    # 双击进剪辑态后再按边缘 ⇒ trim
    xmid = int((tl._x(4.0) + tl._x(14.0)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    _chk("⑦.2 双击进剪辑态", tl.edit_mode_item() is not None)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(14.0)) - 2, y))
    APP.processEvents()
    _chk("⑦.3 剪辑态按右把手 ⇒ _trim", tl._trim is not None)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(14.0)) - 2, y))
    APP.processEvents()


# ==========================================================================
# ③ 多选态操作（widget 级信号）
# ==========================================================================
def _box_select_two(tl):
    """框选 A(4~10) 与 B(12~18)（单层场景）。返回是否成功。"""
    y = _row_y(tl)
    _press_blank(tl, tl._x(19.0), y)
    _set_elapsed(tl, 20.0)
    QTest.mouseMove(tl, QPoint(int(tl._x(19.0) - 25), y))
    APP.processEvents()
    QTest.mouseMove(tl, QPoint(int(tl._x(3.0)), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(3.0)), y))
    APP.processEvents()
    return tl.multi_selection_count() == 2


def t10_group_move_signal():
    print("--- ③ 整组移动（信号）：同位移、相对间距不变 ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0), _clip("B", 12.0, 18.0)])])
    assert _box_select_two(tl)
    y = _row_y(tl)
    sp = QSignalSpy(tl.group_moved)
    # 拖 A（成员）向右 +5s
    xa = int((tl._x(4.0) + tl._x(10.0)) / 2)
    pxps = tl._w() / tl.view_span
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xa, y))
    QTest.mouseMove(tl, QPoint(int(xa + pxps * 5), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xa + pxps * 5), y))
    APP.processEvents()
    _chk("③.1 发 group_moved 恰 1 次", sp.count() == 1, "count=%d" % sp.count())
    if sp.count():
        entries = list(sp.at(0)[0])
        d = {iid: at for (_l, iid, at) in entries}
        _chk("③.2 两个成员都在 entries", set(d) == {"A", "B"}, str(sorted(d)))
        dA = d["A"] - 4.0
        dB = d["B"] - 12.0
        _chk("③.3 两成员同位移(≈+5)", abs(dA - 5.0) < 0.25 and abs(dB - 5.0) < 0.25,
             "dA=%.2f dB=%.2f" % (dA, dB))
        _chk("③.4 相对间距不变(8s)", abs((d["B"] - d["A"]) - 8.0) < 1e-6,
             "gap=%.3f" % (d["B"] - d["A"]))
    _chk("③.5 拖完仍保持多选", tl.multi_selection_count() == 2)


def t11_group_duplicate_signal():
    print("--- ③ 组再制（CDR，信号）：各成员都产一份、相对位置保持 ---")
    tl = _widget([(0, [_clip("A", 4.0, 10.0), _clip("B", 12.0, 18.0)])])
    assert _box_select_two(tl)
    y = _row_y(tl)
    spd = QSignalSpy(tl.group_duplicate_requested)
    spm = QSignalSpy(tl.moved)
    xa = int((tl._x(4.0) + tl._x(10.0)) / 2)
    pxps = tl._w() / tl.view_span
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xa, y))
    QTest.mouseMove(tl, QPoint(int(xa + pxps * 6), y))
    APP.processEvents()
    from PySide6.QtGui import QContextMenuEvent
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                          QPoint(int(xa + pxps * 6), y), QPoint(5, 5)))
    APP.processEvents()
    _chk("③.6 拖动中右键 ⇒ 进复制模式", tl.duplicate_mode() is True)
    QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(int(xa + pxps * 6), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xa + pxps * 6), y))
    APP.processEvents()
    _chk("③.7 松左键 ⇒ 恰 1 次 group_duplicate_requested", spd.count() == 1, "count=%d" % spd.count())
    _chk("③.8 组再制不发 moved（原件组不动）", spm.count() == 0)
    _chk("③.9 提交后退出复制模式", tl.duplicate_mode() is False)
    if spd.count():
        entries = list(spd.at(0)[0])
        d = {iid: at for (_l, iid, at) in entries}
        _chk("③.10 两个成员都有副本条目", set(d) == {"A", "B"}, str(sorted(d)))
        _chk("③.11 副本相对间距保持(8s)", abs((d["B"] - d["A"]) - 8.0) < 1e-6,
             "gap=%.3f" % (d["B"] - d["A"]))


# ==========================================================================
# ④ 退出路径（widget 级：sel_multi 清空）
# ==========================================================================
def _fresh_2clip():
    tl = _widget([(0, [_clip("A", 4.0, 10.0), _clip("B", 12.0, 18.0)])])
    return tl


def t12_exit_paths_widget():
    print("--- ④ 退出路径（widget）：5 种都清多选 ---")
    y = int(theme.RULER_H + theme.ROW_H / 2)
    # 1) 点单个片段
    tl = _fresh_2clip()
    assert _box_select_two(tl)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int((tl._x(4.0) + tl._x(10.0)) / 2), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int((tl._x(4.0) + tl._x(10.0)) / 2), y))
    APP.processEvents()
    _chk("④.1 点单个片段 ⇒ 清多选", tl.sel_multi == set())
    # 2) 点空白
    tl = _fresh_2clip()
    assert _box_select_two(tl)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(19.0)), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(19.0)), y))
    APP.processEvents()
    _chk("④.2 点空白 ⇒ 清多选", tl.sel_multi == set())
    # 3) 双击片段
    tl = _fresh_2clip()
    assert _box_select_two(tl)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int((tl._x(4.0) + tl._x(10.0)) / 2), y))
    APP.processEvents()
    _chk("④.3 双击片段 ⇒ 清多选", tl.sel_multi == set())
    # 4) Esc
    tl = _fresh_2clip()
    assert _box_select_two(tl)
    tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    APP.processEvents()
    _chk("④.4 Esc ⇒ 清多选", tl.sel_multi == set())
    # 5a) 换码表
    tl = _fresh_2clip()
    assert _box_select_two(tl)
    tl.set_sheet(CueSheet(title="other"))
    APP.processEvents()
    _chk("④.5 换码表 ⇒ 清多选", tl.sel_multi == set())
    # 5b) 换曲目
    tl = _fresh_2clip()
    assert _box_select_two(tl)
    te2 = TrackEntry(track_key="k2", identity=Identity(title="T2", artist="", duration_sec=100.0, bpm=0.0))
    te2.identity.ensure_meta_id()
    te2.composition.layers.append(CompLayer(lyr_id="LX", name="LX", z=0))
    tl.set_track(te2)
    APP.processEvents()
    _chk("④.6 换曲目 ⇒ 清多选", tl.sel_multi == set())


# ==========================================================================
# window 级：属性面板灰化 + 组操作落模型 + 一次 undo
# ==========================================================================
def _win():
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
    return w


def _win_scene(w):
    w.new_sheet("FIX10", 1920, 1080)
    ids = w.add_media_paths([MP4])
    cid = ids[0]
    w.add_track("曲", "a", 300.0, 120.0)
    ly = w._layers()[0]
    e1 = w.add_cue(ly.lyr_id, cid, 4.0); e1.end_sec = 10.0
    e2 = w.add_cue(ly.lyr_id, cid, 12.0); e2.end_sec = 18.0
    e3 = w.add_cue(ly.lyr_id, cid, 20.0); e3.end_sec = 26.0
    w._rebuild_runtime()
    tl = w.timeline
    tl.set_view(0.0, 40.0)
    for _ in range(6):
        APP.processEvents()
    return w, ly, e1, e2, e3, tl


def _win_box_two(tl, ly):
    y = _row_y(tl)
    lyid = ly.lyr_id
    _press_blank(tl, tl._x(19.0), y)
    _set_elapsed(tl, 20.0)
    QTest.mouseMove(tl, QPoint(int(tl._x(19.0) - 25), y))
    APP.processEvents()
    QTest.mouseMove(tl, QPoint(int(tl._x(3.0)), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(3.0)), y))
    APP.processEvents()
    return tl.multi_selection_count() == 2


def t13_props_gray():
    print("--- ③ 属性面板：多选灰化 + 文案；恢复单选后 enabled ---")
    w = _win()
    try:
        w, ly, e1, e2, e3, tl = _win_scene(w)
        ok = _win_box_two(tl, ly)
        _chk("③.20 框选到 2 个", ok, "multi=%d" % tl.multi_selection_count())
        _chk("③.21 多选 ⇒ props_panel 禁用", w.props_panel.isEnabled() is False)
        tabtxt = w.tabs.tabText(w.tabs.indexOf(w.props_panel)) if w.tabs.indexOf(w.props_panel) >= 0 else ""
        lbl = ""
        _tip = ""
        try:
            lbl = w.props_panel.lbl_sel.text()
            _tip = w.props_panel.lbl_sel.toolTip()
        except Exception:                        # noqa: BLE001
            lbl = ""
        _hint = tabtxt + " | " + lbl + " | " + _tip
        _chk("③.22 多选 ⇒ 有『可见原因』(标签页/面板行/tooltip 含 多选|不可编辑)",
             ("多选" in _hint or "不可编辑" in _hint), repr(_hint))
        _chk("③.23 多选 ⇒ 面板提示『已选中 2 个片段』",
             ("已选中" in lbl and "2" in lbl) or ("多选" in lbl), repr(lbl))
        # 恢复单选
        tl.set_selected(ly.lyr_id, e1.item_id)
        APP.processEvents()
        _chk("③.24 恢复单选 ⇒ props_panel 启用", w.props_panel.isEnabled() is True)
    finally:
        try:
            w.shutdown()
        except Exception:                        # noqa: BLE001
            pass


def t14_group_move_model_undo():
    print("--- ③ 整组移动落模型 + 一次 undo 全退回 ---")
    w = _win()
    try:
        w, ly, e1, e2, e3, tl = _win_scene(w)
        rid = ly.lyr_id
        id1, id2, id3 = e1.item_id, e2.item_id, e3.item_id
        _win_box_two(tl, ly)
        a0, b0, c0 = float(e1.at_sec), float(e2.at_sec), float(e3.at_sec)
        y = _row_y(tl)
        xa = int((tl._x(e1.at_sec) + tl._x(e1.end_sec)) / 2)
        pxps = tl._w() / tl.view_span
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xa, y))
        QTest.mouseMove(tl, QPoint(int(xa + pxps * 5), y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xa + pxps * 5), y))
        APP.processEvents()
        a1, b1, c1 = float(e1.at_sec), float(e2.at_sec), float(e3.at_sec)
        _chk("③.30 组内 A 位移≈+5", abs((a1 - a0) - 5.0) < 0.3, "dA=%.2f" % (a1 - a0))
        _chk("③.31 组内 B 同位移", abs((b1 - b0) - (a1 - a0)) < 1e-6, "dB=%.2f" % (b1 - b0))
        _chk("③.32 相对间距不变", abs((b1 - a1) - (b0 - a0)) < 1e-6)
        _chk("③.33 组外 C 未动", abs(c1 - c0) < 1e-9, "dC=%.3f" % (c1 - c0))
        w.undo()
        APP.processEvents()
        # undo 会 load_snapshot **重建对象** ⇒ 必须按 id 重新取（旧的 e1/e2/e3 已成孤儿）
        _lA, A2 = w._find_item(rid, id1)
        _lB, B2 = w._find_item(rid, id2)
        _lC, C2 = w._find_item(rid, id3)
        _chk("③.34 一次 undo ⇒ A 回原位", A2 is not None and abs(float(A2.at_sec) - a0) < 1e-6,
             "%.2f (期望 %.2f)" % (float(A2.at_sec) if A2 else -1, a0))
        _chk("③.35 一次 undo ⇒ B 回原位", B2 is not None and abs(float(B2.at_sec) - b0) < 1e-6,
             "%.2f (期望 %.2f)" % (float(B2.at_sec) if B2 else -1, b0))
        _chk("③.36 一次 undo ⇒ C 仍在原位", C2 is not None and abs(float(C2.at_sec) - c0) < 1e-6)
    finally:
        try:
            w.shutdown()
        except Exception:                        # noqa: BLE001
            pass


def t15_group_duplicate_model_undo():
    print("--- ③ 组再制落模型（各出一份、原件不动、一次 undo） ---")
    from PySide6.QtGui import QContextMenuEvent
    w = _win()
    try:
        w, ly, e1, e2, e3, tl = _win_scene(w)
        _win_box_two(tl, ly)
        n0 = len(ly.items)
        a0, b0 = float(e1.at_sec), float(e2.at_sec)
        y = _row_y(tl)
        xa = int((tl._x(e1.at_sec) + tl._x(e1.end_sec)) / 2)
        pxps = tl._w() / tl.view_span
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xa, y))
        QTest.mouseMove(tl, QPoint(int(xa + pxps * 6), y))
        APP.processEvents()
        tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                              QPoint(int(xa + pxps * 6), y), QPoint(5, 5)))
        APP.processEvents()
        QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(int(xa + pxps * 6), y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xa + pxps * 6), y))
        APP.processEvents()
        n1 = len(ly.items)
        _chk("③.40 组再制 ⇒ 片段数 +2", n1 == n0 + 2, "%d→%d" % (n0, n1))
        _chk("③.41 原件 A 未动", abs(float(e1.at_sec) - a0) < 1e-9, "%.3f" % e1.at_sec)
        _chk("③.42 原件 B 未动", abs(float(e2.at_sec) - b0) < 1e-9, "%.3f" % e2.at_sec)
        w.undo()
        APP.processEvents()
        ly_after = w._layers()[0]
        _chk("③.43 一次 undo ⇒ 片段数回到 %d" % n0, len(ly_after.items) == n0,
             "%d" % len(ly_after.items))
    finally:
        try:
            w.shutdown()
        except Exception:                        # noqa: BLE001
            pass


def t16_exit_paths_window_props():
    print("--- ④ 退出路径（window）：面板恢复 enabled ---")
    # 换曲目：需 2 条
    w = _win()
    try:
        w, ly, e1, e2, e3, tl = _win_scene(w)
        # Esc
        _win_box_two(tl, ly)
        _chk("④.10 多选 baseline 面板禁用", w.props_panel.isEnabled() is False)
        tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        APP.processEvents()
        _chk("④.11 Esc ⇒ 面板启用", w.props_panel.isEnabled() is True)
        # 点单个片段
        _win_box_two(tl, ly)
        y = _row_y(tl)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier,
                         QPoint(int((tl._x(e1.at_sec) + tl._x(e1.end_sec)) / 2), y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier,
                           QPoint(int((tl._x(e1.at_sec) + tl._x(e1.end_sec)) / 2), y))
        APP.processEvents()
        _chk("④.12 点单片段 ⇒ 面板启用", w.props_panel.isEnabled() is True)
        # 点空白
        _win_box_two(tl, ly)
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(19.0)), y))
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(19.0)), y))
        APP.processEvents()
        _chk("④.13 点空白 ⇒ 面板启用", w.props_panel.isEnabled() is True)
        # 双击
        _win_box_two(tl, ly)
        QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier,
                          QPoint(int((tl._x(e1.at_sec) + tl._x(e1.end_sec)) / 2), y))
        APP.processEvents()
        _chk("④.14 双击片段 ⇒ 面板启用", w.props_panel.isEnabled() is True)
    finally:
        try:
            w.shutdown()
        except Exception:                        # noqa: BLE001
            pass
    # 换曲目 / 换码表
    w = _win()
    try:
        w, ly, e1, e2, e3, tl = _win_scene(w)
        w.add_track("曲2", "b", 200.0, 100.0)          # 第 2 条
        w._refresh_all()
        APP.processEvents()
        w._on_track_picked(0)                           # ★ 回到有片段的第 1 条（add_track 已切到新轨）
        APP.processEvents()
        tl = w.timeline
        tl.set_view(0.0, 40.0)
        for _ in range(4):
            APP.processEvents()
        ly = w._layers()[0]
        _win_box_two(tl, ly)
        _chk("④.15 多选 baseline 面板禁用", w.props_panel.isEnabled() is False)
        w._on_track_picked(1)                           # 换曲目
        APP.processEvents()
        _chk("④.16 换曲目 ⇒ 面板启用", w.props_panel.isEnabled() is True)
        _chk("④.17 换曲目 ⇒ 清多选", tl.sel_multi == set())
    finally:
        try:
            w.shutdown()
        except Exception:                        # noqa: BLE001
            pass


# ==========================================================================
def main():
    global APP, MP4
    APP = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="ercue_fix10_")
    MP4 = os.path.join(tmp, "clip.mp4")
    _mk_mp4(MP4)
    print("=" * 76)
    print("第十轮独立复验 —— 框选多选(250ms) / 组操作 / 五退出路径 / 第七轮防回归")
    print("=" * 76)
    fns = (t1_immediate_move_is_box, t2_longpress_then_move_is_pan, t3_threshold_boundary,
           t4_real_sleep_longpress_is_pan, t5_blank_click_no_drag_cancels,
           t6_middle_drag_pans_immediately, t7_ruler_drag_seeks_not_box,
           t8_box_hit_crosslayer_partial_none, t9_round7_singleclick_no_trim,
           t10_group_move_signal, t11_group_duplicate_signal, t12_exit_paths_widget,
           t13_props_gray, t14_group_move_model_undo, t15_group_duplicate_model_undo,
           t16_exit_paths_window_props)
    for fn in fns:
        try:
            fn()
        except Exception as e:                          # noqa: BLE001
            RESULTS.append((fn.__name__ + " (exception)", False))
            print("   [FAIL] %s 抛异常: %s\n%s" % (fn.__name__, e, traceback.format_exc()))
    n_ok = sum(1 for _t, ok in RESULTS if ok)
    n = len(RESULTS)
    print("=" * 76)
    print("判据 %d/%d 通过" % (n_ok, n))
    bad = [t for t, ok in RESULTS if not ok]
    if bad:
        print("未通过：")
        for b in bad:
            print("  -", b)
    print("FIX10_IVEIRFY:", "OK" if n_ok == n else "FAIL")
    print("=" * 76)
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
