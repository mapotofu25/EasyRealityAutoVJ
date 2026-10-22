# -*- coding: utf-8 -*-
"""★ T04 · 时间轴交互测试（需求 3 拖拽剪辑 / 需求 11 素材音轨 / 需求 12 右键信号）。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe  <Cue>\\tests\\test_timeline_trim.py

覆盖：
  A. `compute_trim()` **纯函数**（需求 3 核心裁定）：
     · trim 开头（收缩 / 还原到素材头 / 触 0 钳位 / `speed` 锁定 / 开放结尾）
     · trim 末尾（正常 / 源不够长钳位 / `speed` 折算 / `S` 未知 ⇒ 不动源出点）
  B. **控件交互**（真实 `CueTimeline`，offscreen）：
     · 双击进剪辑态 / 双击空白退出 / Esc 退出
     · 剪辑态拖左把手 ⇒ `trimmed`（at/end/src_in/src_out 契约）；**一次拖动仅 1 次 emit**
     · 剪辑态拖右把手 ⇒ `trimmed`；拖动期间**模型不被改**（只本地预览）
     · 右键片段 ⇒ `context_menu` 信号（带片段 + 全局坐标）；空白不弹
  C. **需求 11 素材音轨绘制**：`set_show_clip_audio` / `set_clip_waves` 状态；
     同一时刻 OFF vs ON 截图像素差 ⇒ 差异**只出现在片段块内**（画出来了）。
"""

from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
_SHOTS = os.path.join(_ROOT, "_shots")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                  # noqa: E402

from PySide6.QtCore import QEvent, QPoint, Qt                      # noqa: E402
from PySide6.QtGui import QContextMenuEvent                        # noqa: E402
from PySide6.QtTest import QTest                                    # noqa: E402
from PySide6.QtWidgets import QApplication                         # noqa: E402

from ercue.audio.waveform import WaveformData                      # noqa: E402
from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,    # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.ui import theme                                         # noqa: E402
from ercue.ui.timeline_widget import CueTimeline, compute_trim     # noqa: E402

APP = None


# ==========================================================================
# A. compute_trim() 纯函数（需求 3 公式）
# ==========================================================================
def test_trim_start_cut():
    """trim 开头=收缩：`at` 右移 Δ ⇒ `src_in` 加 Δ·speed，`end`/`src_out` 不动。"""
    at, end, si, so = compute_trim("l", 10.0, 20.0, 5.0, 15.0, 1.0, 12.0, 100.0)
    assert abs(at - 12.0) < 1e-9, at
    assert abs(si - 7.0) < 1e-9, si                 # 5 + (12-10)*1
    assert abs(end - 20.0) < 1e-9 and abs(so - 15.0) < 1e-9, (end, so)
    print("  ✓ trim 开头（收缩）：at 10→12、src_in 5→7、end/src_out 不变")


def test_trim_start_restore_head():
    """trim 开头=外拉：`src_in` 减小直到触 0（还原已掐掉的开头），再往左被钳住。"""
    # 拖到 at'=5：Δ=-5 ⇒ src_in' = 5-5 = 0（正好到素材头）
    at, _e, si, _so = compute_trim("l", 10.0, 20.0, 5.0, 15.0, 1.0, 5.0, 100.0)
    assert abs(at - 5.0) < 1e-9 and abs(si) < 1e-9, (at, si)
    # 再往左拖到 at'=0（Δ=-10 ⇒ 原始 src_in' = -5 < 0）⇒ 钳住：src_in'=0、at'=at-src_in/speed=5
    at2, _e2, si2, _so2 = compute_trim("l", 10.0, 20.0, 5.0, 15.0, 1.0, 0.0, 100.0)
    assert abs(si2) < 1e-9, si2
    assert abs(at2 - 5.0) < 1e-9, at2
    print("  ✓ trim 开头（外拉触头）：src_in 钳到 0、at 停在 5（不能再往左）")


def test_trim_start_speed_locked():
    """trim 开头受 `speed` 锁定：`src_in' = src_in + Δ·speed`。"""
    at, _e, si, _so = compute_trim("l", 10.0, 30.0, 2.0, 15.0, 2.0, 13.0, 100.0)
    assert abs(at - 13.0) < 1e-9, at
    assert abs(si - 8.0) < 1e-9, si                 # 2 + 3*2
    print("  ✓ trim 开头（speed=2）：Δ=3s ⇒ src_in 2→8（×speed）")


def test_trim_start_zero_at_clamp():
    """trim 开头：① `src_in=0`（已到素材头）⇒ 往左拖**无效**（at 不动）；② `at` 恒 ≥ 0。"""
    # src_in=0、at=3：拖到最左 ⇒ raw=0+(0-3)<0 ⇒ 钳住，at 保持 3（不能再往左）
    at, _e, si, _so = compute_trim("l", 3.0, 20.0, 0.0, 15.0, 1.0, 0.0, 100.0)
    assert abs(at - 3.0) < 1e-9, at
    assert abs(si) < 1e-9, si
    # src_in<at 时拖到最左：src_in 归 0、at 停在 at-src_in/speed（且 ≥ 0）
    at2, _e2, si2, _so2 = compute_trim("l", 3.0, 20.0, 1.0, 15.0, 1.0, -9.0, 100.0)
    assert abs(si2) < 1e-9 and abs(at2 - 2.0) < 1e-9, (at2, si2)
    assert at2 >= 0.0
    print("  ✓ trim 开头（已到素材头 / at≥0）：src_in=0 时往左无效；src_in 归 0 时 at 停在 2")


def test_trim_start_open_end():
    """trim 开头：`end=None`（开放结尾）保持 None，`end` 不被造出来。"""
    at, end, si, so = compute_trim("l", 4.0, None, 0.0, None, 1.0, 6.0, 100.0)
    assert end is None and so is None, (end, so)
    assert abs(at - 6.0) < 1e-9 and abs(si - 2.0) < 1e-9, (at, si)
    print("  ✓ trim 开头（开放结尾）：end/src_out 保持 None")


def test_trim_end_normal():
    """trim 末尾=正常：`src_out' = src_in + (end'-at)·speed`。"""
    at, end, si, so = compute_trim("r", 10.0, 20.0, 2.0, 12.0, 1.0, 25.0, 100.0)
    assert abs(at - 10.0) < 1e-9, at
    assert abs(end - 25.0) < 1e-9, end
    assert abs(si - 2.0) < 1e-9 and abs(so - 17.0) < 1e-9, (si, so)   # 2 + 15
    print("  ✓ trim 末尾（正常）：end 20→25、src_out 2+15=17")


def test_trim_end_speed():
    """trim 末尾受 `speed` 锁定（慢放 ⇒ 源只走一半）。"""
    _at, end, _si, so = compute_trim("r", 0.0, 10.0, 0.0, 5.0, 0.5, 10.0, 100.0)
    assert abs(end - 10.0) < 1e-9, end
    assert abs(so - 5.0) < 1e-9, so                 # 0 + 10*0.5
    print("  ✓ trim 末尾（speed=0.5）：10s 时间轴 ⇒ src_out=5")


def test_trim_end_clamp_src():
    """trim 末尾：源不够长 ⇒ 钳到素材尾（`end'` 随之，不产生冻结尾帧）。"""
    _at, end, _si, so = compute_trim("r", 10.0, 20.0, 2.0, 12.0, 1.0, 100.0, 20.0)
    assert abs(so - 20.0) < 1e-9, so                # 钳到 S=20
    assert abs(end - 28.0) < 1e-9, end              # at + (S - src_in)/speed = 10 + 18
    print("  ✓ trim 末尾（源不够长）：src_out 钳到 S=20、end 钳到 28")


def test_trim_end_unknown_dur():
    """trim 末尾：`S` 未知 ⇒ 保持 `end'`、`src_out'` 置 None（不动源出点）。"""
    _at, end, _si, so = compute_trim("r", 10.0, 20.0, 2.0, 12.0, 1.0, 30.0, 0.0)
    assert abs(end - 30.0) < 1e-9, end
    assert so is None, so
    print("  ✓ trim 末尾（S 未知）：end 30、src_out None")


def test_trim_illegal_inputs():
    """非法入参（None/NaN 字符串/0 速）不崩、可回落。"""
    at, end, si, so = compute_trim("l", "x", None, None, None, 0.0, "y", "")
    assert isinstance(at, float) and isinstance(si, float), (at, si)
    assert end is None and so is None, (end, so)
    print("  ✓ 非法入参：speed=0 → 回落 1.0；at/src_in 恒为 float；不崩")


# ==========================================================================
# B/C. 控件交互（真实 CueTimeline）
# ==========================================================================
def _build_timeline():
    """搭一个最小时间轴：1 层 1 片段（at=4/end=14、src_in=2/src_out=12、speed=1）。"""
    tl = CueTimeline()
    tl.resize(1000, 260)
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=300.0, bpm=0.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=4.0, end_sec=14.0,
                            src_in_sec=2.0, src_out_sec=12.0, speed=1.0).normalized())
    te.composition.layers.append(ly)
    sheet = CueSheet(title="t")
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=100.0))
    sheet.tracks.append(te)
    tl.set_track(te)
    tl.set_sheet(sheet)
    # 让片段不判「丢失」（clip-audio 绘制只在正常分支走）
    tl.set_media_paths({"c1": os.path.abspath(__file__)})
    tl.set_view(0.0, 30.0)
    tl.show()
    for _ in range(4):
        APP.processEvents()
    return tl, te, ly, ly.items[0]


def _row_y(tl, row=0):
    return int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)


def test_widget_double_click_edit_mode():
    """双击片段 ⇒ 进入剪辑态；双击空白 / Esc ⇒ 退出。"""
    tl, _te, _ly, it1 = _build_timeline()
    y = _row_y(tl, 0)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    assert tl.edit_mode_item() is it1, "双击后应进入剪辑态"
    assert tl.sel == ("L1", "i1"), tl.sel

    # Esc 退出
    from PySide6.QtGui import QKeyEvent
    tl.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    APP.processEvents()
    assert tl.edit_mode_item() is None, "Esc 应退出剪辑态"

    # 再双击进，再双击空白退出
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    assert tl.edit_mode_item() is it1
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(28.0)), y))
    APP.processEvents()
    assert tl.edit_mode_item() is None, "双击空白应退出剪辑态"
    print("  ✓ 双击进剪辑态 / Esc 退出 / 双击空白退出")


def test_widget_trim_left_handle():
    """剪辑态拖左把手 ⇒ `trimmed`（at/end/src_in/src_out）；一次拖动仅 1 次 emit。"""
    tl, _te, _ly, it1 = _build_timeline()
    y = _row_y(tl, 0)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()

    got = []
    tl.trimmed.connect(lambda *a: got.append(a))

    x_left = int(tl._x(it1.at_sec)) + 2          # 命中左把手
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_left, y))
    # 中间挪几下（模拟真实拖动）：必须**只在松手时** emit 一次
    QTest.mouseMove(tl, QPoint(int(tl._x(5.0)), y))
    QTest.mouseMove(tl, QPoint(int(tl._x(5.5)), y))
    QTest.mouseMove(tl, QPoint(int(tl._x(6.0)), y))
    # 拖动期间：模型**不被改**（只本地预览）
    assert abs(it1.at_sec - 4.0) < 1e-9 and abs(it1.src_in_sec - 2.0) < 1e-9, \
        "拖动期间不该改模型"
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(6.0)), y))
    APP.processEvents()

    assert len(got) == 1, "一次拖动应只 emit 一次 trimmed，实际 %d" % len(got)
    lyr, iid, at, end, si, so = got[0]
    assert lyr == "L1" and iid == "i1", got[0]
    assert abs(at - 6.0) < 0.35, at
    assert abs(end - 14.0) < 1e-6, end
    assert abs(si - 4.0) < 0.35, si               # 2 + (6-4)*1
    assert abs(so - 12.0) < 1e-6, so
    print("  ✓ 拖左把手 ⇒ trimmed(%s,%s,at=%.2f,end=%s,src_in=%.2f,src_out=%s)（仅 1 次）"
          % (lyr, iid, at, end, si, so))


def test_widget_trim_right_handle():
    """剪辑态拖右把手 ⇒ `trimmed`（末尾 trim，src_out 随 speed 折算）。"""
    tl, _te, _ly, it1 = _build_timeline()
    y = _row_y(tl, 0)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()

    got = []
    tl.trimmed.connect(lambda *a: got.append(a))
    x_right = int(tl._x(it1.end_sec)) - 2
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(x_right, y))
    QTest.mouseMove(tl, QPoint(int(tl._x(20.0)), y))
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(20.0)), y))
    APP.processEvents()

    assert len(got) == 1, len(got)
    _lyr, _iid, at, end, si, so = got[0]
    assert abs(at - 4.0) < 1e-6, at
    assert abs(end - 20.0) < 0.35, end
    assert abs(si - 2.0) < 1e-6, si
    assert abs(so - 18.0) < 0.35, so              # 2 + (20-4)*1
    print("  ✓ 拖右把手 ⇒ trimmed(at=%.2f,end=%.2f,src_in=%.2f,src_out=%.2f)"
          % (at, end, si, so))


def test_widget_context_menu_signal():
    """右键片段 ⇒ `context_menu(lyr,item,globalPos)`；★ 24 条 #13：**空白也发**（item=""，供粘贴）。"""
    tl, _te, _ly, it1 = _build_timeline()
    y = _row_y(tl, 0)
    got = []
    tl.context_menu.connect(lambda *a: got.append(a))

    # 片段**未选中** ⇒ 右键 = 发 context_menu（并选中它）
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(xmid, y), QPoint(321, 123))
    tl.contextMenuEvent(ev)
    APP.processEvents()
    assert len(got) == 1, len(got)
    lyr, iid, gpos = got[0]
    assert lyr == "L1" and iid == "i1", got[0]
    assert gpos.x() == 321 and gpos.y() == 123, gpos
    assert tl.sel == ("L1", "i1"), tl.sel

    # ★ 24 条 #13：空白处右键 ⇒ **也发** context_menu("", "", gpos)（供 window 弹"粘贴"）
    ev2 = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(int(tl._x(28.0)), y),
                            QPoint(10, 10))
    tl.contextMenuEvent(ev2)
    APP.processEvents()
    assert len(got) == 2, "空白处右键应发 context_menu（#13）"
    assert got[1][0] == "" and got[1][1] == "", got[1]
    print("  ✓ 右键：片段未选中 ⇒ context_menu(%s,%s)；空白 ⇒ context_menu('','')（#13）"
          % (lyr, iid))


def _fake_wave(columns=300, dur=100.0):
    """造一条与真实 `WaveformData` 同构的假波形（列/幅度/RGB 三频段）。"""
    i = np.arange(columns, dtype=np.float32)
    amp = (0.2 + 0.8 * (i % 2)).astype(np.float32)        # 强弱交替 ⇒ 明显梳状
    ones = np.ones(columns, dtype=np.float32)
    return WaveformData(columns=int(columns), amp=amp, r=ones, g=ones, b=ones,
                        duration_sec=float(dur))


def _np_rgb(img):
    from PySide6.QtGui import QImage
    if hasattr(img, "toImage"):
        img = img.toImage()
    im = img.convertToFormat(QImage.Format_ARGB32)
    w, h = im.width(), im.height()
    bpl = im.bytesPerLine()
    a = np.frombuffer(bytes(im.constBits()), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return a[:, :w * 4].reshape(h, w, 4)[:, :, :3].astype(np.int16)


def _green_spike_x(img, tl):
    """第 0 行内"绿色波形"像素最多的列 x（= 尖峰列）；无则 -1（BugFix Bug2 用）。"""
    a = _np_rgb(img)
    y0, y1 = theme.RULER_H, theme.RULER_H + int(tl.row_h)
    band = a[y0:y1, :, :]
    g, rr, bb = band[:, :, 1], band[:, :, 0], band[:, :, 2]
    green = (g > 60) & (g > rr + 30) & (g > bb + 30)
    counts = green.sum(axis=0)
    if counts.max() <= 0:
        return -1
    return int(np.argmax(counts))


def _spike_wave(columns=300, dur=60.0, spike_col=30):
    i = np.zeros(columns, dtype=np.float32)
    i[:] = 0.02
    i[spike_col] = 1.0
    return WaveformData(columns=columns, amp=i, r=np.zeros(columns, np.float32),
                        g=np.ones(columns, np.float32), b=np.zeros(columns, np.float32),
                        duration_sec=dur)


def test_widget_trim_press_does_not_blank():
    """★ BugFix Bug1 回归：剪辑态**按下把手（未移动）**时时间轴不得变空 / paintEvent 不得抛异常。

    旧 bug：`_trim["preview"]` 初始为 None，`_paint_clip` 用 `.get("preview", 默认)[:2]` 取到 None ⇒
    `TypeError` 逃出 `paintEvent` ⇒ **整条时间轴不再重绘**（松手才恢复）。
    """
    tl, _te, _ly, it1 = _build_timeline()
    y = _row_y(tl, 0)
    xmid = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mouseDClick(tl, Qt.LeftButton, Qt.NoModifier, QPoint(xmid, y))
    APP.processEvents()
    normal = _np_rgb(tl.grab())
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier,
                     QPoint(int(tl._x(it1.at_sec)) + 2, y))     # 只按下、不移动
    APP.processEvents()
    during = _np_rgb(tl.grab())                                 # 旧 bug：这里会抛异常 ⇒ 画面变空
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier,
                       QPoint(int(tl._x(it1.at_sec)) + 2, y))
    APP.processEvents()
    assert normal.shape == during.shape, (normal.shape, during.shape)
    diff = int(np.abs(normal - during).max())
    assert diff == 0, "按下把手（未移动）画面不该变化（旧 bug 会整条变空），diff=%d" % diff
    print("  ✓ Bug1 回归：剪辑态按下把手（未移动）画面不变（diff=0），paintEvent 未抛异常")


def test_widget_clip_audio_follows_drag():
    """★ BugFix Bug2 回归：拖动片段时其素材音轨波形必须**跟着块一起走**。"""
    tl, _te, _ly, it1 = _build_timeline()
    tl.set_clip_waves({"c1": _spike_wave()})
    tl.set_show_clip_audio(True)
    APP.processEvents()
    y = _row_y(tl, 0)
    x0 = _green_spike_x(tl.grab(), tl)
    cx = int((tl._x(it1.at_sec) + tl._x(it1.end_sec)) / 2)
    QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx, y))
    QTest.mouseMove(tl, QPoint(cx + 150, y))
    APP.processEvents()
    dx = float(tl._preview_dx)
    x1 = _green_spike_x(tl.grab(), tl)
    QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(cx + 150, y))
    APP.processEvents()
    assert x0 >= 0 and x1 >= 0, (x0, x1)
    assert abs((x1 - x0) - dx) <= max(6.0, abs(dx) * 0.25), \
        "波形未跟随块移动：尖峰 %d→%d（块移动 %.1fpx）" % (x0, x1, dx)
    print("  ✓ Bug2 回归：波形跟随拖动 —— 尖峰 %d→%d，块移动 %.1fpx" % (x0, x1, dx))


def test_widget_clip_audio_paint():
    """需求 11：素材音轨开关 OFF vs ON —— 像素差**只出现在片段块内**（画出来了）。"""
    tl, _te, _ly, it1 = _build_timeline()
    assert tl.clip_audio_status() == (False, 0), tl.clip_audio_status()
    tl.set_show_clip_audio(True)
    assert tl.clip_audio_status() == (True, 0)
    tl.set_clip_waves({"c1": _fake_wave()})
    assert tl.clip_audio_status() == (True, 1), tl.clip_audio_status()

    tl.set_show_clip_audio(False)
    for _ in range(4):
        APP.processEvents()
    off = tl.grab()
    tl.set_show_clip_audio(True)
    for _ in range(4):
        APP.processEvents()
    on = tl.grab()

    os.makedirs(_SHOTS, exist_ok=True)
    off.save(os.path.join(_SHOTS, "t04_clipaudio_off.png"))
    on.save(os.path.join(_SHOTS, "t04_clipaudio_on.png"))

    a_off, a_on = _np_rgb(off), _np_rgb(on)
    assert a_off.shape == a_on.shape, (a_off.shape, a_on.shape)
    d = np.abs(a_on - a_off).max(axis=2)
    mask = d > 8
    n = int(mask.sum())
    assert n > 0, "开关打开后片段块内应出现波形像素（diff=0）"
    ys, xs = np.where(mask)
    bx0, bx1 = int(xs.min()), int(xs.max())
    by0, by1 = int(ys.min()), int(ys.max())
    # 差异应落在片段块所在的第一行内（不是底部音频轨）
    row0_top = theme.RULER_H
    row0_bot = theme.RULER_H + int(tl.row_h) + 2
    assert by0 >= row0_top - 2 and by1 <= row0_bot, \
        "波形差异应落在片段块行内（y=%d..%d，期望 %d..%d）" % (by0, by1, row0_top, row0_bot)
    rect_l = int(min(tl._x(it1.at_sec), tl._x(it1.end_sec)))
    rect_r = int(max(tl._x(it1.at_sec), tl._x(it1.end_sec)))
    assert bx0 >= rect_l - 4 and bx1 <= rect_r + 4, \
        "波形差异应落在片段块横向范围内（x=%d..%d，片段 %d..%d）" % (bx0, bx1, rect_l, rect_r)
    print("  ✓ 素材音轨：OFF/ON 像素差 %d px，bbox=(%d,%d,%d,%d) **落在片段块内**" %
          (n, bx0, by0, bx1, by1))

    # 关掉开关 ⇒ 不再画（回到 OFF 画面）
    tl.set_show_clip_audio(False)
    for _ in range(4):
        APP.processEvents()
    off2 = _np_rgb(tl.grab())
    assert int(np.abs(off2 - a_off).max()) == 0, "关掉开关后应与 OFF 完全一致"
    print("  ✓ 关掉开关 ⇒ 与 OFF 画面逐像素一致")


def test_widget_clip_audio_missing_data():
    """需求 11：拿不到数据 ⇒ 不画、不崩（空 waves / 非 dict 输入）。"""
    tl, _te, _ly, _it = _build_timeline()
    tl.set_show_clip_audio(True)
    tl.set_clip_waves(None)                       # 非 dict ⇒ 清空
    assert tl.clip_audio_status() == (True, 0)
    tl.grab()                                     # 不崩
    tl.set_clip_waves({"c2": _fake_wave()})       # 有数据但没有对应片段 ⇒ 不画
    tl.grab()
    print("  ✓ 素材音轨：无数据 / 无对应片段 ⇒ 不画不崩")


# ==========================================================================
def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    print("=" * 70)
    print("T04 · 时间轴交互测试（需求 3/11/12）  (python %s)" % sys.version.split()[0])
    print("=" * 70)
    tests = [
        ("A trim 开头（收缩）", test_trim_start_cut),
        ("A trim 开头（外拉触头）", test_trim_start_restore_head),
        ("A trim 开头（speed 锁定）", test_trim_start_speed_locked),
        ("A trim 开头（触素材头/at≥0）", test_trim_start_zero_at_clamp),
        ("A trim 开头（开放结尾）", test_trim_start_open_end),
        ("A trim 末尾（正常）", test_trim_end_normal),
        ("A trim 末尾（speed）", test_trim_end_speed),
        ("A trim 末尾（源钳位）", test_trim_end_clamp_src),
        ("A trim 末尾（S 未知）", test_trim_end_unknown_dur),
        ("A trim 非法入参", test_trim_illegal_inputs),
        ("B 双击进/退剪辑态", test_widget_double_click_edit_mode),
        ("B 拖左把手 ⇒ trimmed", test_widget_trim_left_handle),
        ("B 拖右把手 ⇒ trimmed", test_widget_trim_right_handle),
        ("B 右键 ⇒ context_menu", test_widget_context_menu_signal),
        ("BugFix① 按下把手不变空", test_widget_trim_press_does_not_blank),
        ("BugFix② 波形跟随拖动", test_widget_clip_audio_follows_drag),
        ("C 素材音轨绘制（OFF/ON 差）", test_widget_clip_audio_paint),
        ("C 素材音轨（无数据不画）", test_widget_clip_audio_missing_data),
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
