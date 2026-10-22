# -*- coding: utf-8 -*-
"""★ T03（24 条）· 时间轴控件新交互测试（无头，offscreen）。

跑法：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe  <Cue>\\tests\\test_timeline_snap.py

覆盖：
  #14 自动吸附：纯函数 `snap_at()`（左/右端、阈值、无吸附、非法入参）+ 控件拖动吸附（只动被拖的那个）
  #6  标尺点击 ⇒ `seek_requested`（且**不进入平移**）
  #9  剪辑态 hover 把手 ⇒ `SizeHorCursor`
  #10 点空白 ⇒ `picked("","")`（取消选择）
  #13 空白右键 ⇒ `context_menu("","",gpos)`
  #18 再制状态机（★ 第六轮 CDR 式）：左键按→拖动→右键（进复制模式、不产份）→松右键→松左键
      （提交：原件不动 + 副本落松手处，自动退出）；旧语义（未拖动的普通右键⇒立即复制，一次性）；
      复制模式再按右键 / Esc ⇒ 取消；未选中 ⇒ 菜单
  ★ 第九轮 框选多选：空白左键"立即移动"⇒框选（多选）/ "长按≥250ms后移动"⇒平移；
      多选拖动=整组一起移动（相对位置不变）；单击成员（不拖）⇒收敛单选
"""

from __future__ import annotations

import os
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtCore import QPoint, Qt                              # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QKeyEvent            # noqa: E402
from PySide6.QtTest import QTest                                   # noqa: E402
from PySide6.QtWidgets import QApplication                         # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,    # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                                         # noqa: E402
from ercue.ui.timeline_widget import (CueTimeline, SNAP_TOL_PX, snap_at,
                                      BOX_PAN_MS)  # noqa: E402

APP = None


# ==========================================================================
# 构造
# ==========================================================================
def _build_two():
    """两层？不：**一层两个片段**（c1: 4~14、c2: 20~25）—— 供吸附有"别人"可吸。"""
    tl = CueTimeline()
    tl.resize(1000, 260)
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=300.0, bpm=0.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=14.0,
                            src_in_sec=2.0, src_out_sec=12.0, speed=1.0).normalized())
    ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=20.0, end_sec=25.0,
                            src_in_sec=0.0, src_out_sec=5.0, speed=1.0).normalized())
    te.composition.layers.append(ly)
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=100.0))
    sheet.tracks.append(te)
    tl.set_track(te)
    tl.set_sheet(sheet)
    tl.set_media_paths({"c1": os.path.abspath(__file__)})
    tl.set_view(0.0, 30.0)
    tl.show()
    for _ in range(4):
        APP.processEvents()
    return tl, ly, ly.items[0], ly.items[1]


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


def _build_long_clip(end=120.0, view_span=40.0):
    """1 层 1 片段（4~`end`），视口只显示 0~`view_span` ⇒ 右端落在**视口外**（③ 根因场景）。"""
    tl = CueTimeline()
    tl.resize(1000, 260)
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=300.0, bpm=0.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=end,
                            src_in_sec=0.0, src_out_sec=None, speed=1.0).normalized())
    te.composition.layers.append(ly)
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=10.0))
    sheet.tracks.append(te)
    tl.set_track(te)
    tl.set_sheet(sheet)
    tl.set_media_paths({"c1": os.path.abspath(__file__)})
    tl.set_view(0.0, view_span)
    tl.show()
    for _ in range(4):
        APP.processEvents()
    return tl, ly, ly.items[0]


# ==========================================================================
# #14 snap_at() 纯函数
# ==========================================================================
def test_snap_at_pure():
    # 左端吸附：9.9 → 10
    at, t = snap_at(9.9, 2.0, [10.0, 20.0], 10.0)
    assert abs(at - 10.0) < 1e-9 and abs(t - 10.0) < 1e-9, (at, t)
    # 右端吸附：at=7.5,dur=3 ⇒ 右端 10.5 → 吸 10 ⇒ at'=7
    at, t = snap_at(7.5, 3.0, [10.0], 10.0)
    assert abs(at - 7.0) < 1e-9 and abs(t - 10.0) < 1e-9, (at, t)
    # 无吸附：太远
    at, t = snap_at(50.0, 2.0, [10.0, 20.0], 10.0)
    assert abs(at - 50.0) < 1e-9 and t is None, (at, t)
    # 阈值：tol_px=8、px_per_sec=10 ⇒ tol_sec=0.8；0.7 内吸、0.9 外不吸
    at, t = snap_at(9.3, 2.0, [10.0], 10.0)
    assert abs(at - 10.0) < 1e-9, at
    at, t = snap_at(9.05, 2.0, [10.0], 10.0)
    assert t is None, (at, t)
    # 非法入参：px_per_sec=0 ⇒ 不吸
    at, t = snap_at(9.9, 2.0, [10.0], 0.0)
    assert t is None, (at, t)
    # 空 targets
    at, t = snap_at(9.9, 2.0, [], 10.0)
    assert t is None, (at, t)
    assert SNAP_TOL_PX == 8.0
    print("  ✓ snap_at 纯函数：左/右端吸附、阈值 8px、无吸附、非法入参")


# ==========================================================================
# #14 控件：拖动吸附（只动被拖的那个）
# ==========================================================================
def test_widget_snap_drag():
    tl, _ly, it1, _it2 = _build_two()
    assert tl.snap_enabled() is False               # 默认关
    tl.set_snap(True)
    assert tl.snap_enabled() is True

    y = _row_y(tl)
    xc = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    # 让"被拖片段左端"落到 ≈19.9（离邻居 i2 的 20 只差 0.1s ⇒ 阈值内应吸附到 20）
    move_x = int(tl._x(19.9) + xc - tl._x(it1.at_sec))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xc, y))
    QTest.mouseMove(tl, QPoint(move_x, y))
    APP.processEvents()
    tgt = tl._drag.get("target")
    guide = tl._snap_guide
    assert tgt is not None and abs(tgt - 20.0) < 1e-6, ("应吸附到 20", tgt)
    assert guide is not None and abs(guide - 20.0) < 1e-6, guide
    # ★ 只动被拖的那个：i2 的 at/end 一字未改
    assert abs(_it2.at_sec - 20.0) < 1e-9 and abs(_it2.end_sec - 25.0) < 1e-9
    # 松手 ⇒ moved 发出 20.0
    got = []
    tl.moved.connect(lambda *a: got.append(a))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(move_x, y))
    APP.processEvents()
    assert len(got) == 1 and abs(got[0][2] - 20.0) < 1e-6, got
    assert tl._snap_guide is None                   # 松手清导引线
    print("  ✓ #14 拖动吸附：左端 19.9 → 吸到 20.0（moved 落 20.0），邻居未被动")

    # 关掉吸附 ⇒ 不再吸附（落到 ≈19.9）
    tl.set_snap(False)
    got2 = []
    tl.moved.connect(lambda *a: got2.append(a))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xc, y))
    QTest.mouseMove(tl, QPoint(move_x, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(move_x, y))
    APP.processEvents()
    assert tl._snap_guide is None
    assert got2 and abs(got2[-1][2] - 20.0) > 0.05, ("关吸附后不该吸到 20", got2)
    print("  ✓ #14 关吸附：不吸（落到 %.2f）" % got2[-1][2])


def test_widget_trim_handle_visible_when_edge_offscreen():
    """★ 第六轮 ③ 根因回归：片段右端**在视口外**时，画在**视口右缘的把手必须可抓**
    （应触发 `trimmed`，**而不是**移动整块）。

    旧 bug：`_hit` 用**未钳位**的真实边缘判定"边缘"。当片段右端在视口外时，把手画在视口右缘、
    却因真实边缘（视口外）而命不中 ⇒ 抓把手落到 `edge=""` ⇒ 变成移动整块 ⇒
    用户观感就是"拖左右两边改长短没效果"。
    """
    tl, _ly, it1 = _build_long_clip(end=120.0, view_span=40.0)
    y = _row_y(tl)
    W = tl.width()
    assert tl._x(120.0) > W, "前提：片段右端应该在视口外"
    # 双击（可见范围）进剪辑态
    xmid = int((tl._x(4.0) + tl._x(30.0)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    assert tl.edit_mode_item() is it1
    tris, moved = [], []
    tl.trimmed.connect(lambda *a: tris.append(a))
    tl.moved.connect(lambda *a: moved.append(a))
    at0 = float(it1.at_sec)
    x_handle = W - 3                                  # 用户看到的把手 = 视口右缘
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_handle, y))
    QTest.mouseMove(tl, QPoint(x_handle - 100, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_handle - 100, y))
    APP.processEvents()
    assert not moved, ("抓把手不得变成移动整块", moved)
    assert len(tris) == 1, ("应发出 trimmed", tris)
    _l, _i, at, end, _si, _so = tris[0]
    assert abs(at - at0) < 1e-6, ("trim 末尾时起点不该变", at)
    assert abs(end - 120.0) > 1e-6, ("end 应变化（改长短）", end)
    assert abs(float(it1.at_sec) - at0) < 1e-6, ("原件 at 不得被移动", it1.at_sec)
    print("  ✓ ③ 视口外右端：抓视口缘把手 ⇒ trim 生效（moved=0、at 不变），end 120.0→%.2f"
          % end)


# ==========================================================================
# 第九轮 · 框选多选（空白处左键：立即移动=框选 / 长按后移动=平移）
# ==========================================================================
def _box_two(tl, x_from=3.0, x_to=27.0, row=0):
    """在空白处"立即移动"框选 `_build_two()` 的两个片段（at 4~14 / 20~25）。"""
    y0 = int(theme.RULER_H + tl.row_h * row + 4)
    y1 = int(theme.RULER_H + tl.row_h * row + tl.row_h - 4)
    x0 = int(tl._x(x_from))
    x1 = int(tl._x(x_to))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y0))
    QTest.mouseMove(tl, QPoint((x0 + x1) // 2, (y0 + y1) // 2))
    QTest.mouseMove(tl, QPoint(x1, y1))
    APP.processEvents()
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x1, y1))
    APP.processEvents()


def test_widget_box_select_multi():
    """★ 第九轮：空白处"立即移动"⇒ **框选**（橡皮筋），松手进多选态（两个片段全选中）。"""
    tl, _ly, it1, it2 = _build_two()
    counts = []
    tl.selection_count_changed.connect(lambda n: counts.append(n))
    _box_two(tl)
    assert tl._box is None, "松手后橡皮筋应清除"
    assert tl.multi_selection_count() == 2, tl.multi_selection()
    assert set(tl.multi_selection()) == {("L1", it1.item_id), ("L1", it2.item_id)}
    assert counts and counts[-1] == 2, ("应发 selection_count_changed(2)", counts)
    assert tl.view0 == 0.0, "框选不得平移视图"
    print("  ✓ 框选：立即移动 ⇒ 多选 2（i1+i2），count 信号=2，视图不动")


def test_widget_long_press_pans():
    """★ 第九轮：空白处"长按 ≥250ms 后再移动"⇒ **平移**（不进多选）。"""
    tl, _ly, _it1, _it2 = _build_two()
    v0 = tl.view0
    y = int(theme.RULER_H + 4)
    x0 = int(tl._x(28.0))                         # 右侧空白
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y))
    time.sleep(0.30)                              # ★ 长按 > 250ms
    QTest.mouseMove(tl, QPoint(x0 - 120, y))
    APP.processEvents()
    assert tl._pan is not None, "长按后移动应进入平移"
    assert tl._box is None, "长按后移动不得进入框选"
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0 - 120, y))
    APP.processEvents()
    assert tl.view0 > v0 + 0.5, ("应平移视图", tl.view0)
    assert tl.multi_selection_count() == 0
    print("  ✓ 长按：静止 300ms 后移动 ⇒ 平移（view0 %.2f→%.2f），非多选" % (v0, tl.view0))


def test_widget_group_move_keeps_relative():
    """★ 第九轮：多选后拖动任一成员 ⇒ **整组一起移动**（相对位置不变，发 group_moved、不发 moved）。"""
    tl, ly, it1, it2 = _build_two()
    _box_two(tl)
    assert tl.multi_selection_count() == 2
    moved, group = [], []
    tl.moved.connect(lambda *a: moved.append(a))
    tl.group_moved.connect(lambda e: group.append(e))
    a1, a2 = float(it1.at_sec), float(it2.at_sec)
    y = _row_y(tl)
    xp = int((tl._x(a1) + tl._x(float(it1.end_sec))) / 2)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
    QTest.mouseMove(tl, QPoint(xp + 100, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp + 100, y))
    APP.processEvents()
    assert not moved, ("整组移动不得发单选 moved", moved)
    assert len(group) == 1 and len(group[0]) == 2, group
    ent = {iid: na for (_l, iid, na) in group[0]}
    d1 = ent[it1.item_id] - a1
    d2 = ent[it2.item_id] - a2
    assert d1 > 1.0 and abs(d1 - d2) < 0.05, ("相对位置应不变", d1, d2)
    print("  ✓ 整组移动：group_moved 2 项，delta一致(%.2f/%.2f)，无 moved" % (d1, d2))


def test_widget_box_250ms_threshold():
    """★ 第九轮：250ms 阈值边界 —— <250ms 立即移动=框选；≥250ms 后移动=平移（含常量自证）。"""
    assert abs(BOX_PAN_MS - 250.0) < 1e-9, BOX_PAN_MS
    # 150ms（明显 < 250ms）⇒ 框选
    tl, _ly, _i1, _i2 = _build_two()
    y0 = int(theme.RULER_H + 4)
    y1 = int(theme.RULER_H + tl.row_h - 4)
    x0 = int(tl._x(0.5))
    x1 = int(tl._x(27.0))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x0, y0))
    time.sleep(0.15)
    QTest.mouseMove(tl, QPoint(x1, y1))
    APP.processEvents()
    assert tl._box is not None and tl._pan is None, "150ms 应判为框选"
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x1, y1))
    APP.processEvents()
    assert tl.multi_selection_count() == 2
    b_ok = True
    # 350ms（明显 > 250ms）⇒ 平移
    tl2, _l2, _a, _b = _build_two()
    v0 = tl2.view0
    y = int(theme.RULER_H + 4)
    xs = int(tl2._x(28.0))
    QTest.mousePress(tl2, Qt.LeftButton, Qt.NoModifier, QPoint(xs, y))
    time.sleep(0.35)
    QTest.mouseMove(tl2, QPoint(xs - 120, y))
    APP.processEvents()
    assert tl2._pan is not None and tl2._box is None, "350ms 应判为平移"
    QTest.mouseRelease(tl2, Qt.LeftButton, Qt.NoModifier, QPoint(xs - 120, y))
    APP.processEvents()
    assert tl2.view0 > v0 + 0.5 and tl2.multi_selection_count() == 0
    print("  ✓ 250ms 阈值：150ms⇒框选(多选2)、350ms⇒平移(view0 %.2f→%.2f)；常量=%.0fms"
          % (v0, tl2.view0, BOX_PAN_MS))


def test_widget_track_sheet_switch_clears_multi():
    """★ 第九轮②：换码表 / 换曲目 ⇒ 清多选（回到单选/空、面板可恢复）。"""
    tl, ly, _i1, _i2 = _build_two()
    _box_two(tl)
    assert tl.multi_selection_count() == 2
    # 换曲目（不同 TrackEntry 对象）
    te2 = TrackEntry(track_key="k2", identity=Identity(
        title="T2", artist="A", duration_sec=300.0, bpm=0.0))
    te2.identity.ensure_meta_id()
    tl.set_track(te2)
    APP.processEvents()
    assert len(tl.multi_selection()) == 0, ("换曲目应清多选", tl.multi_selection())
    # 换码表（不同 CueSheet 对象）—— 直接造多选态，验证 set_sheet 清空
    tl.sel_multi = {("L1", "i1"), ("L1", "i2")}
    tl.set_sheet(CueSheet(title="other"))
    APP.processEvents()
    assert len(tl.multi_selection()) == 0, ("换码表应清多选", tl.multi_selection())
    print("  ✓ 换曲目 / 换码表 ⇒ 多选被清空")


def test_widget_click_member_collapses_multi():
    """★ 第九轮：多选态下"单击某成员（不拖动）"⇒ **收敛为单选**该成员。"""
    tl, _ly, it1, _it2 = _build_two()
    _box_two(tl)
    assert tl.multi_selection_count() == 2
    y = _row_y(tl)
    xp = int((tl._x(float(it1.at_sec)) + tl._x(float(it1.end_sec))) / 2)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xp, y))
    APP.processEvents()
    assert len(tl.multi_selection()) == 0, ("多选应被清空", tl.multi_selection())
    assert tl.multi_selection_count() == 1, "应回到单选（计数=1）"
    assert tl.sel == ("L1", it1.item_id), tl.sel
    print("  ✓ 单击多选成员（不拖）⇒ 收敛为单选 %s" % (tl.sel,))


# ==========================================================================
# #6 标尺点击 ⇒ seek_requested
# ==========================================================================
def test_widget_seek_requested():
    """#6 + 第五轮⑥：标尺**点击**定位、**拖动**连续定位（松手定稿）；都不进平移。"""
    tl, _ly, _it1, _it2 = _build_two()
    got = []
    tl.seek_requested.connect(lambda s: got.append(s))
    view0_before = tl.view0
    # 点击（按下 + 松开）
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(12.0)), 5))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(12.0)), 5))
    APP.processEvents()
    assert got and abs(got[-1] - 12.0) < 0.35, got
    assert tl._pan is None, "标尺操作不该进入平移"
    # ★ 拖动（按下 → 移动 → 松手）：连续 emit + 松手定稿
    n0 = len(got)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(5.0)), 5))
    QTest.mouseMove(tl, QPoint(int(tl._x(9.0)), 5))
    QTest.mouseMove(tl, QPoint(int(tl._x(15.0)), 5))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(15.0)), 5))
    APP.processEvents()
    assert len(got) > n0, ("标尺拖动应连续发 seek", got)
    assert abs(got[-1] - 15.0) < 0.5, ("松手定稿到 15", got[-1])
    assert tl._pan is None and abs(tl.view0 - view0_before) < 1e-9, "标尺拖动不得平移视图"
    print("  ✓ #6/⑥ 标尺点击⇒%.2f；拖动⇒连续 + 定稿 %.2f（均不进平移）" % (got[0], got[-1]))


def test_widget_cdr_duplicate_flow():
    """★ 第六轮 · **CDR 式再制**（用户实测权威流程）：

        左键按在片段 → 拖动 → 按右键（进**复制模式**、**不产份**）→ 松右键（无动作）→ 松左键（**提交**）。

    判据（逐条）：
      · 原件 at **不动**（始终 4.0）        · 副本落"**松开左键的位置**"（≈12.0）
      · 只发 1 次 `duplicate_requested`      · **全程无 `moved`**（原件没被搬）
      · 提交后**自动退出**复制模式（`duplicate_mode()==False`、光标回箭头）
    """
    tl, ly, it1, _it2 = _build_two()
    y = _row_y(tl)
    assert tl.snap_enabled() is False               # 吸附默认关 ⇒ 落点即松手处
    dups, moved = [], []
    tl.duplicate_requested.connect(lambda *a: dups.append(a))
    tl.moved.connect(lambda *a: moved.append(a))
    at0 = float(it1.at_sec)                          # 4.0
    xmid = int((tl._x(at0) + tl._x(float(it1.end_sec))) / 2)
    grab = xmid - tl._x(at0)                         # 抓点相对片段起点的像素偏移

    # 1) 左键按在片段上（选中）
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    assert tl.sel == ("L1", "i1"), tl.sel
    assert tl.duplicate_mode() is False, "按下左键不应进复制模式"
    # 2) 拖动到 8.0（普通拖动预览；此刻**尚未**进入复制模式）
    x8 = int(tl._x(8.0) + grab)
    QTest.mouseMove(tl, QPoint(x8, y))
    APP.processEvents()
    assert tl.duplicate_mode() is False, "未按右键，不应进复制模式"
    assert len(dups) == 0, "单纯拖动不应产份"
    # 3) 按右键 ⇒ 进入复制模式（★ 此刻**不产份**）
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(x8, y), QPoint(1, 1)))
    APP.processEvents()
    assert tl.duplicate_mode() is True, "拖动中按右键应进入复制模式"
    assert len(dups) == 0, "进复制模式那一刻**不得**产份（旧实现此处在右键瞬间就 emit）"
    assert tl.cursor().shape() == Qt.DragCopyCursor, tl.cursor().shape()
    # 4) 继续拖到 12.0
    x12 = int(tl._x(12.0) + grab)
    QTest.mouseMove(tl, QPoint(x12, y))
    APP.processEvents()
    # 拖动期间模型原件**一字未改**
    assert abs(float(it1.at_sec) - at0) < 1e-9 and abs(float(it1.end_sec) - 14.0) < 1e-9, \
        ("拖动期间原件不得动", it1.at_sec, it1.end_sec)
    # 5) 松右键（本身无动作）
    QTest.mouseRelease(tl, Qt.RightButton, Qt.NoModifier, QPoint(x12, y))
    APP.processEvents()
    assert tl.duplicate_mode() is True, "松右键**不该**退出复制模式"
    assert len(dups) == 0, "松右键不得产份"
    # 6) 松左键 ⇒ 提交
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x12, y))
    APP.processEvents()
    # ——— 判据 ———
    assert not moved, ("全程不得发 moved（原件没被搬）", moved)
    assert len(dups) == 1, ("提交时应恰好产 1 份", dups)
    lyr, iid, a = dups[0]
    assert lyr == "L1" and iid == "i1", dups[0]
    assert abs(a - 12.0) < 0.35, ("副本应落在松左手处 ≈12.0", a)
    assert abs(float(it1.at_sec) - at0) < 1e-9, ("原件必须留在按下位置 4.0", it1.at_sec)
    assert tl.duplicate_mode() is False, "提交后应**自动**退出复制模式"
    assert tl.cursor().shape() == Qt.ArrowCursor, tl.cursor().shape()
    print("  ✓ CDR 再制：原件 %.1f（不动）/ 副本落 %.2f / 只发 1 份 / 无 moved / 自动退出"
          % (float(it1.at_sec), a))
    _ = (ly,)


# ==========================================================================
# #9 剪辑态 hover 把手 ⇒ SizeHorCursor
# ==========================================================================
def test_widget_cursor_sizehor():
    tl, _ly, it1, _it2 = _build_two()
    y = _row_y(tl)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    assert tl.edit_mode_item() is it1
    # hover 左把手
    QTest.mouseMove(tl, QPoint(int(tl._x(it1.at_sec)) + 2, y))
    APP.processEvents()
    assert tl.cursor().shape() == Qt.SizeHorCursor, tl.cursor().shape()
    # hover 片段中部 ⇒ 回箭头
    QTest.mouseMove(tl, QPoint(xmid, y))
    APP.processEvents()
    assert tl.cursor().shape() == Qt.ArrowCursor, tl.cursor().shape()
    print("  ✓ #9 剪辑态 hover 把手 ⇒ SizeHorCursor；中部 ⇒ ArrowCursor")


# ==========================================================================
# #10 点空白 ⇒ picked("","") 取消选择
# ==========================================================================
def test_widget_blank_click_clears_selection():
    tl, _ly, it1, _it2 = _build_two()
    y = _row_y(tl)
    tl.set_selected("L1", "i1")
    got = []
    tl.picked.connect(lambda *a: got.append(a))
    # 点片段外的空白（x 落到 i1 与 i2 之间）
    blank_x = int((tl._x(14.0) + tl._x(20.0)) / 2)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(blank_x, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(blank_x, y))
    APP.processEvents()
    assert tl.sel == ("", ""), tl.sel
    assert ("", "") in got, got
    print("  ✓ #10 点空白 ⇒ picked('','')（选中已清空）")
    _ = it1


# ==========================================================================
# #13 空白右键 ⇒ context_menu("","",gpos)
# ==========================================================================
def test_widget_blank_rightclick():
    tl, _ly, _it1, _it2 = _build_two()
    y = _row_y(tl)
    got = []
    tl.context_menu.connect(lambda *a: got.append(a))
    blank_x = int((tl._x(14.0) + tl._x(20.0)) / 2)
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                          QPoint(blank_x, y), QPoint(7, 9)))
    APP.processEvents()
    assert len(got) == 1 and got[0][0] == "" and got[0][1] == "", got
    assert got[0][2].x() == 7 and got[0][2].y() == 9
    # sec_at_global 换算
    s = tl.sec_at_global(tl.mapToGlobal(QPoint(blank_x, y)))
    assert abs(s - tl._sec(blank_x)) < 1e-6, (s, tl._sec(blank_x))
    print("  ✓ #13 空白右键 ⇒ context_menu('','',gpos)；sec_at_global 换算一致")


# ==========================================================================
# #18 再制状态机
# ==========================================================================
def test_widget_duplicate_legacy_and_cancel():
    """★ 第六轮 #18：**旧语义**（未拖动的普通右键 ⇒ 立即复制一份，一次性）+ **复制模式中途取消**。

      · 未选中 + 右键 ⇒ 菜单（不产份、不进复制模式）
      · 已选中 + 未拖动 + 右键 ⇒ **立即复制一份**，且**不残留**复制模式（满足"再制后自动取消"）
      · 复制模式（拖动中按过右键）⇒ 再按右键 / Esc ⇒ **取消**（不产份、原件回原位）
      · 普通态左键拖 ⇒ 平移（moved），不产份
    """
    tl, _ly, it1, _it2 = _build_two()
    y = _row_y(tl)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    dups, menus = [], []
    tl.duplicate_requested.connect(lambda *a: dups.append(a))
    tl.context_menu.connect(lambda *a: menus.append(a))

    # 未选中 ⇒ 右键 = 菜单（不发 duplicate、不进复制模式）
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(1, 1)))
    APP.processEvents()
    assert len(menus) == 1 and len(dups) == 0 and tl.duplicate_mode() is False, (menus, dups)
    assert tl.sel == ("L1", "i1")
    print("  ✓ #18 未选中 ⇒ 右键弹菜单（不发 duplicate、不进复制模式）")

    # 已选中且**未拖动** ⇒ 右键 = 立即复制一份（一次性，**不进**复制模式）
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(2, 2)))
    APP.processEvents()
    assert len(dups) == 1 and len(menus) == 1, (dups, menus)
    assert dups[0][0] == "L1" and dups[0][1] == "i1"
    assert tl.duplicate_mode() is False, "旧语义一次性再制后必须**自动取消**（不残留复制模式）"
    assert tl.cursor().shape() == Qt.ArrowCursor
    # 再来一次（仍选中）⇒ 再复制一份，仍不残留
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(3, 3)))
    APP.processEvents()
    assert len(dups) == 2 and tl.duplicate_mode() is False, (dups, tl.duplicate_mode())
    print("  ✓ #18 右键已选中（未拖）⇒ 立即复制 1 份 + **自动取消**（不残留复制模式）")

    # —— 复制模式取消①：再按一次右键 ——
    moved = []
    tl.moved.connect(lambda *a: moved.append(a))
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    QTest.mouseMove(tl, QPoint(xmid + 120, y))
    APP.processEvents()
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                          QPoint(xmid + 120, y), QPoint(4, 4)))
    APP.processEvents()
    assert tl.duplicate_mode() is True
    d0 = len(dups)
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                          QPoint(xmid + 120, y), QPoint(5, 5)))
    APP.processEvents()
    assert tl.duplicate_mode() is False, "复制模式下再按右键应取消复制"
    assert len(dups) == d0, "取消复制不得产份"
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid + 120, y))
    APP.processEvents()
    assert len(dups) == d0 and not moved, ("取消后松手不得产份/moved", dups, moved)
    assert abs(float(it1.at_sec) - 4.0) < 1e-9, ("原件回原位", it1.at_sec)
    print("  ✓ #18 复制模式再按右键 ⇒ 取消（不产份、原件回原位）")

    # —— 复制模式取消②：Esc ——
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    QTest.mouseMove(tl, QPoint(xmid + 120, y))
    APP.processEvents()
    tl.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse,
                                          QPoint(xmid + 120, y), QPoint(6, 6)))
    APP.processEvents()
    assert tl.duplicate_mode() is True
    tl.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    APP.processEvents()
    assert tl.duplicate_mode() is False, "Esc 应取消复制"
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid + 120, y))
    APP.processEvents()
    assert len(dups) == d0 and not moved, "Esc 取消后松手不得产份/moved"
    print("  ✓ #18 复制模式 Esc ⇒ 取消（不产份）")

    # 普通态左键拖 ⇒ 平移（moved），不产份
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    QTest.mouseMove(tl, QPoint(xmid + 100, y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid + 100, y))
    APP.processEvents()
    assert len(moved) == 1 and len(dups) == d0, (moved, dups)
    print("  ✓ #18 普通态左键拖 ⇒ 平移（moved），不产份")


# ==========================================================================
def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    print("=" * 70)
    print("T03 · 时间轴新交互测试（#6/#9/#10/#13/#14/#18）")
    print("=" * 70)
    tests = [
        ("#14 snap_at 纯函数", test_snap_at_pure),
        ("#14 拖动吸附", test_widget_snap_drag),
        ("#6 标尺 seek", test_widget_seek_requested),
        ("① CDR 再制流程", test_widget_cdr_duplicate_flow),
        ("③ 视口外边缘把手可抓", test_widget_trim_handle_visible_when_edge_offscreen),
        ("⑨ 框选=多选", test_widget_box_select_multi),
        ("⑨ 250ms 阈值边界", test_widget_box_250ms_threshold),
        ("⑨ 换码表/曲目清多选", test_widget_track_sheet_switch_clears_multi),
        ("⑨ 长按=平移", test_widget_long_press_pans),
        ("⑨ 整组一起移动", test_widget_group_move_keeps_relative),
        ("⑨ 单击成员收敛单选", test_widget_click_member_collapses_multi),
        ("#9 光标 SizeHor", test_widget_cursor_sizehor),
        ("#10 点空白取消选择", test_widget_blank_click_clears_selection),
        ("#13 空白右键菜单", test_widget_blank_rightclick),
        ("#18 旧语义 + 取消", test_widget_duplicate_legacy_and_cancel),
    ]
    ok_all = True
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            ok_all = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 70)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 70)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
