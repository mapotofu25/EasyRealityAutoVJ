# -*- coding: utf-8 -*-
"""五次取样：按「当前出厂/现场分辨率 1280x720 @30fps」与「目标 1920x1080 @60fps」双口径量化。"""
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

from PySide6.QtGui import QImage, QPainter, QGuiApplication  # noqa: E402
from PySide6.QtCore import Qt, QRectF  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication([])
import postprocess as PP  # noqa: E402
import colorfx  # noqa: E402


def med(fn, n=20, warm=3):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def row(label, t720, t1080):
    print("%-40s %7.2f ms (720p@30 = %4.1f%%核)   %7.2f ms (1080p@60 = %5.2f 核)"
          % (label, t720, t720 * 30 / 10.0 / 100.0 * 100 / 100 * 100, t1080, t1080 * 60 / 1000),
          flush=True)


print("== 同一操作在两种分辨率下的耗时 ==\n")
print("%-40s %-34s %s" % ("项目", "1280x720 @30fps", "1920x1080 @60fps"))
print("-" * 100)
for (W, H) in ((1280, 720),):
    pass

res = {}
for (W, H, tag) in ((1280, 720, "720"), (1920, 1080, "1080")):
    rng = np.random.RandomState(3)
    raw = (rng.rand(H, W, 4) * 255).astype(np.uint8)
    canvas = QImage(raw.data, W, H, 4 * W, QImage.Format_ARGB32).copy()
    img = QImage(raw.data, W, H, 4 * W, QImage.Format_ARGB32).copy()
    bgr = np.ascontiguousarray(raw[:, :, :3])
    f3 = bgr
    lut = np.arange(256, dtype=np.uint8)[None, :, None].repeat(3, 2)

    cr = colorfx.ColorRenderer()
    cr.s = 0.6
    cr.color = (168.0, 85.0, 247.0)
    cr._lut_key = None

    def compose(n):
        def f():
            p = QPainter(canvas)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            for i in range(n):
                p.setOpacity(0.95)
                p.drawImage(QRectF(0, 0, W, H), img)
            p.end()
        return f

    def deepcopy_frame():
        f = QImage(W, H, QImage.Format_RGB32)
        np.copyto(np.frombuffer(f.bits(), np.uint8, f.sizeInBytes()),
                  np.frombuffer(canvas.constBits(), np.uint8, canvas.sizeInBytes()))

    def view_bgr():
        PP._np_view_bgr(canvas)

    def view_write():
        b = PP._np_view_bgr(canvas)
        PP.write_back_bgr(canvas, b)

    cfg2 = {"enabled": True, "effects": {"bloom": {"on": True, "level": 100},
                                         "chromatic": {"on": True, "level": 100}}}
    cfg3 = {"enabled": True, "effects": {"bloom": {"on": True, "level": 100},
                                         "chromatic": {"on": True, "level": 100},
                                         "exposure": {"on": True, "level": 100}}}
    res[tag] = {
        "compose1": med(compose(1)),
        "compose3": med(compose(3)),
        "compose6": med(compose(6)),
        "deepcopy": med(deepcopy_frame),
        "view_bgr": med(view_bgr),
        "view_write": med(view_write),
        "lut3": med(lambda: cv2.LUT(f3, lut)),
        "colorfx_apply": med(lambda: cr.apply(canvas, "lut"), n=15),
        "pp_2fx": med(lambda: PP.apply_postfx(canvas, cfg2, seed=1), n=15),
        "pp_3fx": med(lambda: PP.apply_postfx(canvas, cfg3, seed=1), n=15),
        "bloom": med(lambda: PP.fx_bloom(f3, 0.6)),
        "softfocus": med(lambda: PP.fx_softfocus(f3, 0.6)),
        "grain": med(lambda: PP.fx_grain(f3, 0.6, 7)),
        "chromatic": med(lambda: PP.fx_chromatic(f3, 0.6)),
        "canvas_copy": med(lambda: canvas.copy()),
        "resize_1080": med(lambda: cv2.resize(f3, (1920, 1080)), n=10),
        "qimage_copy": med(lambda: QImage(raw.data, W, H, 4 * W, QImage.Format_ARGB32).copy()),
    }

KEYS = ["compose1", "compose3", "compose6", "deepcopy", "view_bgr", "view_write", "lut3",
        "colorfx_apply", "pp_2fx", "pp_3fx", "bloom", "softfocus", "grain", "chromatic",
        "canvas_copy", "qimage_copy"]
NAME = {"compose1": "QPainter 合成 1 层", "compose3": "QPainter 合成 3 层",
        "compose6": "QPainter 合成 6 层", "deepcopy": "深拷贝帧 np.copyto",
        "view_bgr": "_np_view_bgr(3ch 跨步拷贝)", "view_write": "视图往返(取+写回)",
        "lut3": "cv2.LUT 3ch(已连续)", "colorfx_apply": "colorfx.apply 完整",
        "pp_2fx": "apply_postfx(bloom+chromatic)", "pp_3fx": "apply_postfx(3 效果)",
        "bloom": "fx_bloom", "softfocus": "fx_softfocus", "grain": "fx_grain",
        "chromatic": "fx_chromatic", "canvas_copy": "canvas.copy()",
        "qimage_copy": "QImage(arr).copy()"}

print("\n%-38s %10s %10s %10s %10s" % ("项目", "720p ms", "720p@30核", "1080p ms", "1080p@60核"))
print("-" * 88)
for k in KEYS:
    a = res["720"][k]
    b = res["1080"][k]
    print("%-38s %10.2f %10.3f %10.2f %10.2f"
          % (NAME[k], a, a * 30 / 1000, b, b * 60 / 1000))
