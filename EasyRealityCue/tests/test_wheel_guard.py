# -*- coding: utf-8 -*-
"""滚轮护栏：滚轮不许改「组合框 / 数字框 / 滑条」的值（2026-11-03 03:56）。

用户原话：「把那个鼠标悬停在那个选项上，滚轮滚动可以调整选项功能去掉呢，这个经常误操作啊。」

本文件要证明三件事：
  A. **反向基线**：不装护栏时，控件有焦点 + 滚轮 **真的会改值**（否则测不出问题）
  B. 装了护栏后：同样条件下**值不变**，而且**页面照常滚**（不能"吞掉"变成滚不动）
  C. 别的东西没被误伤：时间轴的 Ctrl+滚轮缩放、普通滚轮平移仍然工作

跑法：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe tests\\test_wheel_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="cuewheel_")
os.environ["LOCALAPPDATA"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtCore import QPoint, QPointF, Qt                     # noqa: E402
from PySide6.QtGui import QWheelEvent                              # noqa: E402
from PySide6.QtWidgets import (                                    # noqa: E402
    QApplication, QComboBox, QDoubleSpinBox, QScrollArea, QSlider, QVBoxLayout,
    QWidget,
)

from ercue.ui import wheel_guard                                   # noqa: E402

APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def pump(ms=120):
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        APP.processEvents()
        time.sleep(0.005)


def wheel(widget, dy=120, ctrl=False):
    """给控件发一次滚轮（正数 = 向上滚）。"""
    mods = Qt.ControlModifier if ctrl else Qt.NoModifier
    ev = QWheelEvent(QPointF(10.0, 10.0), QPointF(10.0, 10.0),
                     QPoint(0, 0), QPoint(0, int(dy)),
                     Qt.NoButton, mods, Qt.NoScrollPhase, False)
    APP.sendEvent(widget, ev)
    APP.processEvents()


def make_strip(inner_widget, scroll=True):
    """造一个「滚动区 + 里面一个控件」的小场景（和属性面板同样的结构）。"""
    host = QScrollArea()
    host.setWidgetResizable(True)
    box = QWidget()
    # ⚠ 必须**显式给最小高度**：`widgetResizable=True` + 一堆空 QWidget 的 minimumSize 都是 0
    #   ⇒ 内容被压成 viewport 大小，滚动条量程 = 0（我第一次就踩了，测出"页面不滚"的假象）。
    box.setMinimumHeight(2000)
    lay = QVBoxLayout(box)
    lay.addWidget(inner_widget)
    for _ in range(30):
        lay.addWidget(QWidget())
    host.setWidget(box)
    host.resize(240, 180)
    host.show()
    pump(120)
    return host


def main():
    print("=" * 78)
    print("【A】反向基线：**不装护栏**时，滚轮真的会改值（证明这条测试能抓到问题）")
    print("=" * 78)
    sp = QDoubleSpinBox()
    sp.setRange(0.0, 100.0)
    sp.setSingleStep(1.0)
    sp.setValue(10.0)
    host = make_strip(sp)
    sp.setFocus()
    pump(60)
    before = sp.value()
    wheel(sp, +120)
    check(abs(sp.value() - before) > 1e-9,
          "(a1) 无护栏 + 有焦点 + 滚轮 ⇒ 值被改（这就是误操作）",
          "%.1f → %.1f" % (before, sp.value()))
    host.close()

    print()
    print("=" * 78)
    print("【B】装上护栏：值不变，但**页面照常滚**")
    print("=" * 78)
    wheel_guard.install()
    check(wheel_guard._FILTER is not None, "(b0) 护栏已安装")

    sp2 = QDoubleSpinBox()
    sp2.setRange(0.0, 100.0)
    sp2.setSingleStep(1.0)
    sp2.setValue(42.0)
    host2 = make_strip(sp2)
    sp2.setFocus()
    pump(60)
    sb = host2.verticalScrollBar()
    v0, s0 = sp2.value(), sb.value()
    # ⚠ 必须**向下滚**（-120）：滚动条初始就在顶部，+120 是往顶滚 ⇒ value 不变，
    #   会误判成"页面没滚"（我第一次就这么踩的）。
    wheel(sp2, -120)
    wheel(sp2, -120)
    check(abs(sp2.value() - v0) < 1e-9, "(b1) 数字框：滚轮**不再改值**",
          "%.1f → %.1f" % (v0, sp2.value()))
    check(sb.value() != s0, "(b2) 但页面**照常滚动**（不是「吞掉变卡死」）",
          "%d → %d" % (s0, sb.value()))

    cb = QComboBox()
    for t in ("甲", "乙", "丙", "丁"):
        cb.addItem(t)
    cb.setCurrentIndex(1)
    host3 = make_strip(cb)
    cb.setFocus()
    pump(60)
    i0 = cb.currentIndex()
    wheel(cb, +120)
    wheel(cb, -120)
    check(cb.currentIndex() == i0, "(b3) 下拉框：滚轮**不再切换选项**",
          "%d → %d" % (i0, cb.currentIndex()))

    sl = QSlider(Qt.Horizontal)
    sl.setRange(0, 100)
    sl.setValue(50)
    host4 = make_strip(sl)
    sl.setFocus()
    pump(60)
    p0 = sl.value()
    wheel(sl, +120)
    check(sl.value() == p0, "(b4) 滑条：滚轮**不再改值**", "%d → %d" % (p0, sl.value()))

    host2.close()
    host3.close()
    host4.close()

    print()
    print("=" * 78)
    print("【C】没误伤：普通控件与时间轴照旧")
    print("=" * 78)
    # 普通 QWidget（不是那几类）⇒ 护栏**不插手**，事件原样交回 Qt。
    #   ⚠ 这里不能断言"页面滚了"：`sendEvent` 是**直接派发**，控件不处理也不会自动冒泡到
    #   父滚动区（真实鼠标事件才会）。那种断言测的是 Qt 的传播机制，不是护栏。
    #   所以直接断言护栏的返回值 —— 它对普通控件必须放行（return False）。
    plain = QWidget()
    host5 = make_strip(plain)
    pump(60)
    ev = QWheelEvent(QPointF(10.0, 10.0), QPointF(10.0, 10.0),
                     QPoint(0, 0), QPoint(0, -120),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    check(wheel_guard._FILTER.eventFilter(plain, ev) is False,
          "(c1) 普通控件不拦（交回 Qt 原生处理）")
    # 反过来：受保护的类型必须被拦（返回 True）
    spx = QDoubleSpinBox()
    ev2 = QWheelEvent(QPointF(1.0, 1.0), QPointF(1.0, 1.0),
                      QPoint(0, 0), QPoint(0, -120),
                      Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    check(wheel_guard._FILTER.eventFilter(spx, ev2) is True,
          "(c1b) 受保护类型（数字框）必须被拦")
    host5.close()

    # 时间轴：Ctrl+滚轮仍能缩放（护栏不拦 CueTimeline）
    from ercue.ui.timeline_widget import CueTimeline
    tl = CueTimeline()
    tl.resize(1000, 260)
    tl.show()
    pump(150)
    tl.set_view(0.0, 64.0)
    span0 = tl.view_span
    wheel(tl, -120, ctrl=True)
    check(tl.view_span != span0, "(c2) 时间轴 Ctrl+滚轮**仍能缩放**（护栏没拦它）",
          "%.1f → %.1f" % (span0, tl.view_span))
    v_before = tl.view_span
    wheel(tl, -120, ctrl=False)
    check(tl.view_span == v_before, "(c3) 时间轴普通滚轮不改跨度（那是平移）", tl.view_span)
    tl.close()

    print()
    print("=" * 78)
    print("【D】真窗口：曲目 BPM / 属性面板各控件 / 标签页，滚轮都不动它")
    print("=" * 78)
    from PySide6.QtWidgets import QTabWidget
    from ercue.ui.window import MainWindow
    w = MainWindow(restore_workspace=False, autosave=False)
    w._interactive = False                     # 默认 True，右键/对话框会卡死
    w.resize(1400, 900)
    w.move(-20000, -20000)
    w.show()
    pump(400)
    w.new_sheet("滚轮护栏")
    pump(200)

    # ① 曲目面板的 BPM（用户点名的那个）
    bpm = getattr(w.track_panel, "sp_bpm", None)
    if bpm is not None:
        bpm.setFocus()
        pump(60)
        b0 = bpm.value()
        wheel(bpm, +120)
        wheel(bpm, -120)
        check(abs(bpm.value() - b0) < 1e-9, "(d1) 曲目 BPM：滚轮不改值",
              "%.2f → %.2f" % (b0, bpm.value()))
    else:
        check(False, "(d1) 找不到 track_panel.sp_bpm")

    # ② 属性面板：数字框 / 下拉框 / 滑条
    pp = w.props_panel
    for name, wdg in (("起点", pp.sp_at), ("变速滑条", pp.sld_speed),
                      ("混合下拉", pp.cb_blend), ("旋转", pp.sp_rot)):
        wdg.setFocus()
        pump(40)
        if hasattr(wdg, "value"):
            v0 = wdg.value()
            wheel(wdg, +120)
            wheel(wdg, -120)
            check(abs(wdg.value() - v0) < 1e-9, "(d2) 属性·%s：滚轮不改值" % name,
                  "%s → %s" % (v0, wdg.value()))
        else:
            i0 = wdg.currentIndex()
            wheel(wdg, +120)
            wheel(wdg, -120)
            check(wdg.currentIndex() == i0, "(d2) 属性·%s：滚轮不切选项" % name,
                  "%d → %d" % (i0, wdg.currentIndex()))

    # ③ ①②的标签页：滚轮不切页（QTabBar）
    tw = w.tabs._secs[0].widget()
    if isinstance(tw, QTabWidget):
        bar = tw.tabBar()
        bar.setFocus()
        pump(40)
        tw.setCurrentIndex(0)
        pump(40)
        wheel(bar, -120)
        wheel(bar, -120)
        check(tw.currentIndex() == 0, "(d3) 标签页：滚轮不切页", tw.currentIndex())
    try:
        w.shutdown()
    except Exception:                                      # noqa: BLE001
        pass

    print()
    print("=" * 78)
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
