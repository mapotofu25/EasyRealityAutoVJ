# -*- coding: utf-8 -*-
"""二次取样：把 colorfx/postprocess 的"视图往返"拆开量，确认是否真有 20ms。"""
import os
import statistics
import sys
import time

import numpy as np
import cv2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.chdir(ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QGuiApplication  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication([])
import postprocess as PP  # noqa: E402
import colorfx  # noqa: E402


def bench(name, fn, n=30, warm=3, freq=None):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(ts)
    line = "%-46s %8.3f ms  p90 %8.3f" % (name, med, sorted(ts)[int(n * 0.9) - 1])
    if freq:
        line += "  %5.2f 核@%dfps" % (med * freq / 1000, freq)
    print(line, flush=True)


W, H = 1920, 1080
rng = np.random.RandomState(1)
raw = (rng.rand(H, W, 4) * 255).astype(np.uint8)
canvas = QImage(raw.data, W, H, 4 * W, QImage.Format_ARGB32).copy()
print("canvas: %dx%d bpl=%d  %s" % (canvas.width(), canvas.height(),
                                    canvas.bytesPerLine(), canvas.format()))

print("\n--- A. 视图往返拆解 ---")
bench("img.bits() 只取指针", lambda: canvas.bits(), n=50, freq=60)
bench("PP._np_view_bgr(canvas)", lambda: PP._np_view_bgr(canvas), n=30, freq=60)
bgr = PP._np_view_bgr(canvas)
bench("PP.write_back_bgr(canvas, bgr)", lambda: PP.write_back_bgr(canvas, bgr), n=30, freq=60)
bench("cv2.LUT(bgr 连续 3ch)", lambda: cv2.LUT(bgr, np.arange(256, dtype=np.uint8)), n=30, freq=60)

print("\n--- B. colorfx.apply 逐步 ---")
cr = colorfx.ColorRenderer()
cr.s = 0.6
cr.color = (168.0, 85.0, 247.0)
cr._lut_key = None
bench("cr.apply(完整)", lambda: cr.apply(canvas, "lut"), n=20, freq=60)
lut = cr._lut
bench("  ├ 仅 _np_view_bgr", lambda: PP._np_view_bgr(canvas), n=30)

def _lut_only():
    b = PP._np_view_bgr(canvas)
    cv2.LUT(b, lut)
bench("  ├ 视图 + LUT", _lut_only, n=30)

def _lut_write():
    b = PP._np_view_bgr(canvas)
    b = cv2.LUT(b, lut)
    PP.write_back_bgr(canvas, b)
bench("  └ 视图 + LUT + 写回", _lut_write, n=30)

print("\n--- C. apply_postfx 真链（自动模式常见 2~3 个） ---")
cfg2 = {"enabled": True, "effects": {"bloom": {"on": True, "level": 100},
                                     "chromatic": {"on": True, "level": 100}}}
cfg3 = {"enabled": True, "effects": {"bloom": {"on": True, "level": 100},
                                     "chromatic": {"on": True, "level": 100},
                                     "exposure": {"on": True, "level": 100}}}
bench("apply_postfx(bloom+chromatic)", lambda: PP.apply_postfx(canvas, cfg2, seed=1), n=20, freq=60)
bench("apply_postfx(bloom+chromatic+exposure)", lambda: PP.apply_postfx(canvas, cfg3, seed=1),
      n=20, freq=60)
bench("空链（只做视图往返）", lambda: PP.apply_postfx(canvas, {"enabled": True, "effects": {}},
                                                    seed=1), n=30, freq=60)

print("\n--- D. 连续 BGR 的 cvtColor（真实 cv2 解码后是连续的） ---")
bgr_cont = np.ascontiguousarray(bgr)
bench("cvtColor BGR->BGRA 连续", lambda: cv2.cvtColor(bgr_cont, cv2.COLOR_BGR2BGRA),
      n=30, freq=30)
bench("cvtColor 非连续[:, :, :3]", lambda: cv2.cvtColor(raw[:, :, :3], cv2.COLOR_BGR2BGRA),
      n=30, freq=30)
bench("ascontiguousarray 跨步 3ch", lambda: np.ascontiguousarray(raw[:, :, :3]), n=30)

print("\n--- E. QImage.bits() 是否触发 detach（有第二份引用时） ---")
canvas2 = canvas.copy()
bench("bits() 单引用", lambda: canvas.bits(), n=50)
bench("bits() 双引用(共享)", lambda: canvas2.bits(), n=50)
bench("constBits() 双引用", lambda: canvas2.constBits(), n=50)
print("  canvas2 与 canvas 共享数据? cacheKey 相等 =", canvas.cacheKey() == canvas2.cacheKey())
