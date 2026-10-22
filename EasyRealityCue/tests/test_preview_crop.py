# -*- coding: utf-8 -*-
"""T04 · 预览画布：**#16 PS 式裁切把手** + **#10 点空白取消选择**。

覆盖：
  A. `crop_apply()` **纯函数**（#16 核心几何）：左/右/上/下/角把手；最小尺寸守卫 `CROP_MIN`；0..1 钳位。
  B. **进/出裁切模式**：选中片段**双击**进/退；`Esc` 退出；未选中时双击无效。
  C. **把手拖动 ⇒ crop**（真 `PreviewCanvas`，offscreen）：
     · 命中把手 → 拖动 → **松手才 emit `xform_committed`（含新 crop）**，拖动期间**模型不被改**；
     · **一次拖动仅 1 次 emit**；松手后 `_override` 清空（只本地预览）。
  D. **冻结映射 / 性能护栏**：拖动期间**绝不调 `set_runtime()`**（用 spy 计数）。
  E. **#10 点空白**：比例框外 ⇒ `selection_cleared`；框内非图像（contain 黑边）⇒ `selection_cleared`；
     点在图像上 ⇒ **不**取消（进入位移拖动）。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe tests\\test_preview_crop.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
import warnings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# PySide6 6.11 把便捷版 QMouseEvent 构造重载标了 DeprecationWarning —— 本测试只用它**合成事件**，
# 属无噪声需求之外的提示，这里显式忽略（不影响被测逻辑）。
warnings.filterwarnings("ignore", category=DeprecationWarning)

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import av                                                          # noqa: E402
import numpy as np                                                 # noqa: E402

from PySide6.QtCore import QEvent, QPointF, Qt                    # noqa: E402
from PySide6.QtGui import QMouseEvent                              # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from ercue.model.cuesheet import (CompLayer, CueItem, CueSheet,    # noqa: E402
                                  Identity, MediaRef, TrackEntry)
from ercue.model.timeline import CueRuntime                        # noqa: E402
from ercue.ui.preview import CROP_MIN, PreviewCanvas, crop_apply   # noqa: E402

APP = None
_TMP = None
_MP4 = None


# ==========================================================================
# 夹具：系统临时目录里造 1 个真 mp4（不写项目目录）
# ==========================================================================
def _make_mp4(path, frames=8, fps=8, w=320, h=180):
    c = av.open(path, "w")
    st = c.add_stream("mpeg4", rate=fps)
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    for i in range(frames):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, :, 2] = 180
        x = (i * 37) % max(1, w - 30)
        img[:, x:x + 30, 1] = 255
        fr = av.VideoFrame.from_ndarray(img, format="rgb24")
        for pk in st.encode(fr):
            c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()


def _runtime(mp4_path):
    sheet = CueSheet(title="t")
    te = TrackEntry(track_key="k",
                    identity=Identity(title="P", duration_sec=60.0, bpm=120.0))
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=10.0,
                 trans_in="cut", trans_out="cut")
    ly.items.append(it)
    te.composition.layers.append(ly)
    sheet.media.append(MediaRef(clip_id="c1", name="a.mp4", duration_sec=30.0))
    sheet.tracks.append(te)
    return CueRuntime(sheet, te, media_paths={"c1": mp4_path}), ly, it


def _view(mp4_path, w=800, h=300):
    rt, ly, it = _runtime(mp4_path)
    v = PreviewCanvas()
    v.resize(w, h)
    v.set_runtime(rt, 120.0)
    v.set_selection(ly.lyr_id, it.item_id)
    v.set_pos(1.0)
    return v, rt, ly, it


def _press(v, x, y, btn=Qt.LeftButton):
    v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y),
                                  btn, btn, Qt.NoModifier))


def _move(v, x, y):
    v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y),
                                 Qt.NoButton, Qt.LeftButton, Qt.NoModifier))


def _release(v, x, y):
    v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


def _dblclick(v, x, y):
    v.mouseDoubleClickEvent(QMouseEvent(QEvent.MouseButtonDblClick, QPointF(x, y),
                                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def _key_esc(v):
    from PySide6.QtGui import QKeyEvent
    v.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))


# ==========================================================================
# A. crop_apply 纯函数
# ==========================================================================
def test_crop_apply_pure():
    # 左把手右移 ⇒ x 增、w 减；right 边界不动
    c = crop_apply([0.2, 0.2, 0.6, 0.6], "l", 0.1, 0.0)
    assert abs(c[0] - 0.3) < 1e-9 and abs(c[2] - 0.5) < 1e-9, c
    # 右把手左移 ⇒ w 减；x 不动
    c = crop_apply([0.2, 0.2, 0.6, 0.6], "r", -0.2, 0.0)
    assert abs(c[0] - 0.2) < 1e-9 and abs(c[2] - 0.4) < 1e-9, c
    # 上手柄下移 ⇒ y 增、h 减
    c = crop_apply([0.0, 0.0, 1.0, 1.0], "t", 0.0, 0.25)
    assert abs(c[1] - 0.25) < 1e-9 and abs(c[3] - 0.75) < 1e-9, c
    # 角把手两轴联动
    c = crop_apply([0.1, 0.1, 0.8, 0.8], "br", -0.1, -0.2)
    assert abs(c[2] - 0.7) < 1e-9 and abs(c[3] - 0.6) < 1e-9, c
    # 最小尺寸守卫（左把右移过头 ⇒ 顶到 right-mn）
    c = crop_apply([0.0, 0.0, 1.0, 1.0], "l", 5.0, 0.0)
    assert abs(c[2] - CROP_MIN) < 1e-9, c
    assert abs(c[0] + c[2] - 1.0) < 1e-9, c            # 右边界不动
    # 0..1 钳位（右把左移过头 ⇒ 顶到 l+mn）
    c = crop_apply([0.3, 0.0, 0.5, 1.0], "r", -9.0, 0.0)
    assert abs(c[2] - CROP_MIN) < 1e-9 and abs(c[0] - 0.3) < 1e-9, c
    # 结果恒在 [0,1] 且边长为正
    for hnd in ("l", "r", "t", "b", "tl", "tr", "bl", "br"):
        c = crop_apply([0.2, 0.3, 0.5, 0.4], hnd, 0.4, -0.4)
        assert 0.0 <= c[0] <= 1.0 and 0.0 <= c[1] <= 1.0, (hnd, c)
        assert 0.0 < c[2] <= 1.0 and 0.0 < c[3] <= 1.0, (hnd, c)
        assert c[0] + c[2] <= 1.0 + 1e-9 and c[1] + c[3] <= 1.0 + 1e-9, (hnd, c)
    print("  ✓ crop_apply：8 把手方向正确；最小边长 %g；0..1 钳位；边长为正" % CROP_MIN)


# ==========================================================================
# B. 进/出裁切模式
# ==========================================================================
def test_enter_exit_crop_mode():
    v, _rt, _ly, it = _view(_MP4)
    assert v._crop_mode is False
    cx, cy = v.rect().center().x(), v.rect().center().y()
    _dblclick(v, cx, cy)                               # 双击（选中）⇒ 进
    assert v._crop_mode is True
    _dblclick(v, cx, cy)                               # 再双击 ⇒ 退
    assert v._crop_mode is False
    _dblclick(v, cx, cy)                               # 进
    assert v._crop_mode is True
    _key_esc(v)                                        # Esc ⇒ 退
    assert v._crop_mode is False
    # 未选中 ⇒ 双击无效（只读预览）
    v.set_selection("", "")
    _dblclick(v, cx, cy)
    assert v._crop_mode is False
    # 换选中 ⇒ 退出裁切
    v.set_selection("L1", "i1")
    _dblclick(v, cx, cy)
    assert v._crop_mode is True
    v.set_selection("", "")
    assert v._crop_mode is False
    print("  ✓ 裁切模式：双击进/退、Esc 退、未选中无效、换选中退")


# ==========================================================================
# C. 把手拖动 ⇒ crop（松手才 emit；拖动中不改模型；一次 1 emit）
# ==========================================================================
def test_handle_drag_commits_crop():
    v, _rt, ly, it = _view(_MP4)
    it.crop = [0.25, 0.25, 0.5, 0.5]
    v._crop_mode = True
    geo = v._crop_frame()
    assert geo is not None, "素材尺寸探测失败（应有真 mp4）"
    wgt, tgt = geo[0], geo[1]
    x0, y0 = it.crop[0], it.crop[1]

    events = []
    v.xform_committed.connect(lambda a, b, xf, crop: events.append((a, b, dict(xf), crop)))
    set_rt_calls = []
    orig_set_runtime = v.set_runtime
    v.set_runtime = lambda *a, **k: (set_rt_calls.append(1), orig_set_runtime(*a, **k))[1]

    # 拖右把手：向左 0.25 个显示宽度 ⇒ crop_w 减 0.25*0.5 = 0.125
    rx, ry = wgt.right(), (wgt.top() + wgt.bottom()) / 2.0
    _press(v, rx, ry)
    assert v._crop_drag is not None, "按下应命中右把手"
    _move(v, rx - 0.25 * tgt.width(), ry)
    # 拖动期间：只本地补丁，**模型未改**、**未 set_runtime**
    assert it.crop == [0.25, 0.25, 0.5, 0.5], ("拖动期间不得改模型", it.crop)
    assert v._override is not None and len(events) == 0, "拖动期间不得 emit"
    assert set_rt_calls == [], "拖动期间绝不 set_runtime（性能护栏）"
    _release(v, rx - 0.25 * tgt.width(), ry)
    assert len(events) == 1, ("一次拖动应恰好 1 次 emit", len(events))
    _a, _b, _xf, crop = events[0]
    assert crop is not None and len(crop) == 4, crop
    assert abs(crop[2] - 0.375) < 1e-6, ("右把手内收 0.125", crop)
    assert abs(crop[0] - x0) < 1e-9 and abs(crop[1] - y0) < 1e-9, crop
    assert it.crop == [0.25, 0.25, 0.5, 0.5], "松手后预览也不直接改模型（由 window 落）"
    assert v._override is None, "松手后本地补丁清空"
    print("  ✓ 把手拖动：松手 1 次 emit crop=%s；拖动中/松手后均不改模型；拖动中 0 次 set_runtime"
          % ([round(x, 4) for x in crop]))


# ==========================================================================
# D. 冻结映射自洽（拖动不用 live tgt 重算）
# ==========================================================================
def test_frozen_mapping_consistency():
    v, _rt, _ly, it = _view(_MP4)
    it.crop = [0.2, 0.2, 0.6, 0.6]
    v._crop_mode = True
    geo = v._crop_frame()
    wgt, tgt = geo[0], geo[1]
    c0 = list(it.crop)
    # 拖左把手右移 = 0.5 个显示宽度 ⇒ dxn = 0.5*crop0_w = 0.3 ⇒ l=0.5, w=0.3
    lx, ly_ = wgt.left(), (wgt.top() + wgt.bottom()) / 2.0
    _press(v, lx, ly_)
    _move(v, lx + 0.5 * tgt.width(), ly_)
    crop = v._override[3]
    assert abs(crop[0] - 0.5) < 1e-6 and abs(crop[2] - 0.3) < 1e-6, crop
    _release(v, lx + 0.5 * tgt.width(), ly_)
    assert abs(c0[0] - 0.2) < 1e-9, "冻结映射不应回写 crop0"
    print("  ✓ 冻结映射：显示宽度×0.5 ⇒ 归一化 Δ=0.3（用冻结 tgt/crop0，不重算）")


# ==========================================================================
# E. 点空白取消选择（#10）
# ==========================================================================
def test_click_blank_clears_selection():
    v, _rt, _ly, it = _view(_MP4)          # 800×300，比例框居中（左右有黑边）
    cleared = []
    v.selection_cleared.connect(lambda: cleared.append(1))
    box = v._box_rect()
    # (1) 比例框**外**（左侧黑边）⇒ 取消
    _press(v, 5, int(box.center().y()))
    assert len(cleared) == 1, "比例框外点击应发 selection_cleared"
    assert v._drag is None, "点空白不应进入位移拖动"
    cleared.clear()
    # (2) 点在图像上（cover ⇒ 铺满比例框）⇒ **不**取消，进入位移拖动
    _press(v, int(box.center().x()), int(box.center().y()))
    assert len(cleared) == 0 and v._drag is not None, "点图像应进位移拖动、不取消"
    _release(v, int(box.center().x()), int(box.center().y()))
    # (3) contain（小 scale）⇒ 框内四角是黑边 ⇒ 取消
    it.xform = {"scale": 0.3, "x": 0.0, "y": 0.0}
    cleared.clear()
    _press(v, int(box.left() + 4), int(box.top() + 4))
    assert len(cleared) == 1, "contain 黑边（框内非图像）点击应取消选择"
    print("  ✓ #10 点空白：框外/框内黑边 ⇒ selection_cleared；点图像 ⇒ 位移拖动不取消")


# ==========================================================================
def main() -> int:
    global APP, _TMP, _MP4
    APP = QApplication.instance() or QApplication([])
    _TMP = tempfile.mkdtemp(prefix="ercue_t04_")
    mp4 = os.path.join(_TMP, "a.mp4")
    _make_mp4(mp4)
    _MP4 = mp4
    tests = [
        ("crop_apply 纯函数", test_crop_apply_pure),
        ("进/出裁切模式", test_enter_exit_crop_mode),
        ("把手拖动 ⇒ crop", test_handle_drag_commits_crop),
        ("冻结映射自洽", test_frozen_mapping_consistency),
        ("点空白取消选择", test_click_blank_clears_selection),
    ]
    ok, passed = True, 0
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
            passed += 1
        except Exception as e:                                  # noqa: BLE001
            ok = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (passed, len(tests) - passed))
    print("汇总：%s" % ("全部通过 ✓" if ok else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
