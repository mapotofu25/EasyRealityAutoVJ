# -*- coding: utf-8 -*-
"""时间轴「缩小」的内容上限（用户 2026-11-03）。

用户原话：
  「这个时间轴这个缩小不能缩到这个音频的显示内容比这个时间轴还小这个长度上。
    就是要有一个限制。」
  （截图里刻度只到 8.6 秒、而内容更短 ⇒ 内容在视野里只占一小段。他要的是
    **最多缩到"内容正好铺满整个时间轴"**，不能再往外缩。）

覆盖：
  【A】有音频时长（`identity.duration_sec = 100`）⇒ 连缩 20 次**收敛在 100**，不趋近 20000。
  【B】无音频、但有片段（end=20）⇒ 上限 = `timeline_total_sec()`（= 25s，与总长同口径）。
  【C】空表（无音频、无片段）⇒ **不受内容限制**：能缩到远超 100s（没被 10s 兜底锁死）。
  【D】Ctrl+滚轮走**同一条**上限（`zoom_by` / `_clamp_span`），不会出现"按钮缩不出去、滚轮能"。
  【E】`set_max_span()` 的边界：已有视图立刻收敛；`sec<=0` 回落硬上限（**绝不退化成 0**）；
       内容长度超过硬上限时仍钳在 `theme.VIEW_SPAN_MAX`。
  【F】初始视图：内容 5.5s ⇒ 首帧跨度正好 = 5.5s（"内容正好铺满"），且不破坏空表行为。

跑：venv\\Scripts\\python.exe -u tests\\test_timeline_zoom_limit.py
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="cuezoom_")
os.environ["LOCALAPPDATA"] = _TMP          # 隔离配置 / 自动保存，别碰用户真实工作区

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

from PySide6.QtCore import QPoint, QPointF, Qt                        # noqa: E402
from PySide6.QtGui import QWheelEvent                                 # noqa: E402
from PySide6.QtWidgets import QApplication                            # noqa: E402

from ercue import config as app_config                                # noqa: E402
from ercue.model.cuesheet import CueItem                              # noqa: E402
from ercue.ui import theme                                            # noqa: E402
from ercue.ui.timeline_widget import CueTimeline                      # noqa: E402
from ercue.ui.window import MainWindow                                # noqa: E402

_APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def _new_window():
    """建主窗口（干净的**空表**）。⚠ `_interactive` **必须**立刻置 False（否则右键/对话框会卡死）。

    ⚠⚠ 必须显式 `restore_workspace=False, autosave=False`：默认 `restore_workspace=True`
      时新窗口会把**上一条用例**自动保存的工作区恢复进来（实测：A 用例那首 100s 的曲目
      跑到了 C 用例里 ⇒ "空表不受限"永远测不出来）。锁定空表语义只能靠这两开关。
    """
    w = MainWindow(restore_workspace=False, autosave=False)
    w._interactive = False
    w.move(-20000, -20000)
    w.show()
    _APP.processEvents()
    return w


def _close(w):
    w.close()
    w.deleteLater()
    _APP.processEvents()


# ==========================================================================
# 【A】有音频时长 ⇒ 上限 = 音频时长
# ==========================================================================
def test_audio_duration_limit():
    print("=" * 76)
    print("【A】有音频时长 100s ⇒ 缩不出去（收敛在 100，不是 20000）")
    print("=" * 76)
    w = _new_window()
    try:
        w.add_track("有音频", duration_sec=100.0)
        _APP.processEvents()
        tl = w.timeline
        check(abs(w._content_sec() - 100.0) < 1e-6, "(a1) `_content_sec()` = 音频时长 100",
              w._content_sec())
        check(abs(tl.max_span - 100.0) < 1e-6, "(a2) 控件 `max_span` = 100", tl.max_span)

        tl.set_view(0.0, 10.0)                     # 先放大看细节
        for _ in range(20):
            w._zoom(1.3)                           # 连点 20 次「缩小」
        print("     连缩 20 次后 view_span = %.6f" % tl.view_span)
        check(tl.view_span <= 100.0 + 1e-6, "(a3) view_span ≤ 100（内容上限）", tl.view_span)
        check(abs(tl.view_span - 100.0) < 1e-6, "(a4) **正好收敛在 100**", tl.view_span)
        check(tl.view_span < theme.VIEW_SPAN_MAX / 10.0,
              "(a5) 完全没有趋近硬上限 20000", (tl.view_span, theme.VIEW_SPAN_MAX))
        # 再缩 10 次也纹丝不动（真·收敛，不是"恰好路过"）
        for _ in range(10):
            w._zoom(1.3)
        check(abs(tl.view_span - 100.0) < 1e-6, "(a6) 再缩 10 次仍是 100（钳住不动）", tl.view_span)
        # 反向仍能放大（上限只管缩，不挡放大）
        w._zoom(0.5)
        check(abs(tl.view_span - 50.0) < 1e-6, "(a7) 反向放大正常（100 → 50）", tl.view_span)
    finally:
        _close(w)


# ==========================================================================
# 【B】无音频、有片段 ⇒ 上限 = timeline_total_sec()
# ==========================================================================
def test_clip_end_limit():
    print()
    print("=" * 76)
    print("【B】无音频但有片段（end=20）⇒ 上限 = timeline_total_sec() = 25s")
    print("=" * 76)
    w = _new_window()
    try:
        w.add_track("无音频")                       # identity.duration_sec = 0
        _APP.processEvents()
        check(abs(w._content_sec()) < 1e-9, "(b1) 先确认：此刻无音频、无片段 ⇒ 内容长度 0",
              w._content_sec())
        ly = w._layers()[0]
        ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=0.0,
                                end_sec=20.0).normalized())
        w._sync_timeline_data()                     # 模型变了 ⇒ 上限必须跟着重算
        _APP.processEvents()
        total = w.timeline_total_sec()
        print("     timeline_total_sec = %.3f / _content_sec = %.3f / max_span = %.3f"
              % (total, w._content_sec(), w.timeline.max_span))
        check(abs(total - 25.0) < 1e-6, "(b2) 总长 = 最后片段终点 20 + 5 = 25", total)
        check(abs(w.timeline.max_span - total) < 1e-6, "(b3) 上限 = 总长（不是 10s 兜底）",
              w.timeline.max_span)
        w.timeline.set_view(0.0, 5.0)
        for _ in range(20):
            w._zoom(1.3)
        print("     连缩 20 次后 view_span = %.6f" % w.timeline.view_span)
        check(abs(w.timeline.view_span - total) < 1e-6, "(b4) 收敛在总长 25", w.timeline.view_span)
    finally:
        _close(w)


# ==========================================================================
# 【C】空表 ⇒ 不受限（关键：不能被 timeline_total_sec() 的 10s 兜底锁死）
# ==========================================================================
def test_empty_sheet_unlimited():
    print()
    print("=" * 76)
    print("【C】空表（无音频 / 无片段）⇒ 不受内容限制，能缩到远超 100s")
    print("=" * 76)
    w = _new_window()
    try:
        tl = w.timeline
        check(abs(w._content_sec()) < 1e-9, "(c1) 空表 `_content_sec()` = 0（表示「别限制」）",
              w._content_sec())
        check(abs(tl.max_span - theme.VIEW_SPAN_MAX) < 1e-6,
              "(c2) 空表 `max_span` = 硬上限 20000（没被 10s 兜底污染）", tl.max_span)
        start = tl.view_span
        for _ in range(20):
            w._zoom(1.3)
        print("     空表连缩 20 次：%.3f → %.3f 秒" % (start, tl.view_span))
        check(tl.view_span > 100.0, "(c3) 能缩过 100s（空表没锁死）", tl.view_span)
        check(tl.view_span > 1000.0, "(c4) 能缩过 1000s（真放得开）", tl.view_span)
    finally:
        _close(w)


# ==========================================================================
# 【D】Ctrl+滚轮走同一条上限
# ==========================================================================
def _wheel(tl, dy=-120, ctrl=True, x=None):
    """给控件发一个滚轮事件（`dy<0` = 向下滚 = 缩小/视野变大）。"""
    px = int(tl.width() * 0.5) if x is None else int(x)
    py = int(theme.RULER_H + 4)
    ev = QWheelEvent(QPointF(px, py), QPointF(tl.mapToGlobal(QPoint(px, py))),
                     QPoint(0, 0), QPoint(0, int(dy)),
                     Qt.NoButton, Qt.ControlModifier if ctrl else Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    tl.wheelEvent(ev)
    _APP.processEvents()


def test_ctrl_wheel_same_limit():
    print()
    print("=" * 76)
    print("【D】Ctrl+滚轮：上限与「缩小」按钮**同一个** `max_span`")
    print("=" * 76)
    w = _new_window()
    try:
        w.add_track("有音频", duration_sec=100.0)
        _APP.processEvents()
        tl = w.timeline
        tl.set_view(0.0, 10.0)
        for _ in range(30):
            _wheel(tl, dy=-120, ctrl=True)          # 一直往外滚
        print("     Ctrl+滚轮 30 次后 view_span = %.6f / max_span = %.1f"
              % (tl.view_span, tl.max_span))
        check(tl.view_span <= tl.max_span + 1e-6, "(d1) 滚轮也不会超过 max_span", tl.view_span)
        check(abs(tl.view_span - 100.0) < 1e-6, "(d2) 收敛在 100（与按钮一致）", tl.view_span)
        check(tl.view0 >= 0.0, "(d3) 以鼠标为锚重算 view0 后仍 ≥ 0", tl.view0)
        # 直接调 zoom_by 也走同一条路（工具条/外部都可用）
        tl.set_view(0.0, 10.0)
        for _ in range(20):
            tl.zoom_by(1.3)
        check(abs(tl.view_span - 100.0) < 1e-6, "(d4) `zoom_by(1.3)`×20 同样收敛在 100",
              tl.view_span)
    finally:
        _close(w)


# ==========================================================================
# 【E】set_max_span 的边界
# ==========================================================================
def test_set_max_span_edges():
    print()
    print("=" * 76)
    print("【E】`set_max_span()` 边界：立刻收敛 / sec<=0 回落硬上限 / 不比硬上限大")
    print("=" * 76)
    tl = CueTimeline()                              # 裸控件：默认不限制（与旧行为一致）
    tl.resize(1000, 260)
    tl.show()
    _APP.processEvents()
    check(abs(tl.max_span - theme.VIEW_SPAN_MAX) < 1e-6,
          "(e1) 裸控件 `max_span` 默认 = 硬上限（老测试 `set_view(0,30)` 不受影响）", tl.max_span)
    tl.set_view(0.0, 500.0)
    check(abs(tl.view_span - 500.0) < 1e-6, "(e2) 未设上限时 500 秒照样能设", tl.view_span)
    tl.set_max_span(100.0)
    print("     set_view(500) → set_max_span(100) ⇒ view_span = %.3f" % tl.view_span)
    check(abs(tl.view_span - 100.0) < 1e-6, "(e3) 已缩过头 ⇒ **立刻收敛**到 100", tl.view_span)
    tl.set_max_span(0.0)
    check(abs(tl.max_span - theme.VIEW_SPAN_MAX) < 1e-6,
          "(e4) `sec=0`（不知道内容多长）⇒ 回落硬上限，**不退化成 0**", tl.max_span)
    tl.set_max_span(-5.0)
    check(abs(tl.max_span - theme.VIEW_SPAN_MAX) < 1e-6, "(e5) 负数同理", tl.max_span)
    tl.set_max_span(1e12)
    check(abs(tl.max_span - theme.VIEW_SPAN_MAX) < 1e-6,
          "(e6) 天文数字内容 ⇒ 仍钳在硬上限 20000", tl.max_span)
    tl.set_max_span(0.5)                            # 比 VIEW_SPAN_MIN 还小
    check(abs(tl.max_span - theme.VIEW_SPAN_MIN) < 1e-6,
          "(e7) 内容比 MIN 还短 ⇒ 上限 = VIEW_SPAN_MIN 2s（跨度不能小于 MIN）", tl.max_span)
    tl.set_view(0.0, 30.0)
    check(abs(tl.view_span - theme.VIEW_SPAN_MIN) < 1e-6,
          "(e8) set_view 也吃这个上限（30 → 2）", tl.view_span)
    tl.close()
    tl.deleteLater()
    _APP.processEvents()


# ==========================================================================
# 【F】首帧："内容正好铺满"
# ==========================================================================
def test_initial_fit():
    print()
    print("=" * 76)
    print("【F】首帧：内容 5.5s ⇒ 跨度正好 5.5s（内容铺满整个时间轴）")
    print("=" * 76)
    w = _new_window()
    try:
        w.add_track("短视频", duration_sec=5.5)
        _APP.processEvents()
        tl = w.timeline
        print("     total=%.3f max_span=%.3f view_span=%.3f"
              % (w.timeline_total_sec(), tl.max_span, tl.view_span))
        check(abs(tl.view_span - 5.5) < 1e-6, "(f1) 首帧跨度 = 内容长度 5.5", tl.view_span)
        check(tl.view0 == 0.0, "(f2) 首帧仍从 0 开始", tl.view0)
    finally:
        _close(w)


# ==========================================================================
def main() -> int:
    print("=" * 76)
    print("时间轴缩小上限 = 内容长度（用户 2026-11-03）")
    print("=" * 76)
    app_config._INSTANCE = app_config.Config(
        os.path.join(tempfile.mkdtemp(prefix="cuezoom_cfg_"), "config.json"))
    print("theme.VIEW_SPAN_MIN/MAX = %s / %s" % (theme.VIEW_SPAN_MIN, theme.VIEW_SPAN_MAX))
    print()
    test_audio_duration_limit()
    test_clip_end_limit()
    test_empty_sheet_unlimited()
    test_ctrl_wheel_same_limit()
    test_set_max_span_edges()
    test_initial_fit()

    print()
    print("=" * 76)
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
