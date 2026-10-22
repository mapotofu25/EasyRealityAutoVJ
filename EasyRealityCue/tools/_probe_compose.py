# -*- coding: utf-8 -*-
"""实测 6：预览合成器性能 —— QPainter 多图层合成 vs numpy 逐像素。

模拟编辑器预览：画布 1920x1080（合成原始尺寸），3~4 层 1920x1080 源图，
每层带 透明(opacity) + 裁切(crop 源矩形) + 缩放/位移 + 不同混合模式 + 过渡(alpha)。
目标：编辑器预览只需 8~15 fps ⇒ 单帧预算 66~125 ms。
"""
import os, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import Qt, QRectF

W, H = 1920, 1080
app = None
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])


def make_layer(w=W, h=H, alpha=True, seed=0):
    rng = np.random.default_rng(seed)
    buf = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    buf = np.ascontiguousarray(buf)
    fmt = QImage.Format_ARGB32 if alpha else QImage.Format_RGB32
    img = QImage(buf.data, w, h, 4 * w, fmt).copy()   # .copy() 摆脱 numpy 生命周期
    return img


canvas = QImage(W, H, QImage.Format_ARGB32)
layers = [make_layer(seed=i) for i in range(4)]

BLENDS = ["SourceOver", "Plus", "Screen", "Multiply", "Lighten", "Darken"]


def compose_qpainter(nlayers, mode="SourceOver", crop=True, xform=True, n=10):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        canvas.fill(Qt.black)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        for i in range(nlayers):
            img = layers[i]
            p.setCompositionMode(getattr(QPainter, "CompositionMode_" + mode))
            p.setOpacity(0.85)
            # crop：只取源图中间 90%
            src = QRectF(img.width() * 0.05, 0, img.width() * 0.9, img.height()) if crop else QRectF()
            tw, th = W * 0.7, H * 0.7
            tgt = QRectF((W - tw) / 2 + i * 12, (H - th) / 2 + i * 9, tw, th) if xform else QRectF(0, 0, W, H)
            p.drawImage(tgt, img, src)
        p.end()
        ts.append((time.perf_counter() - t0) * 1000)
    return np.mean(ts), np.percentile(ts, 90)


print("=" * 78)
print(f"QPainter 合成 1920x1080，每层 opacity=0.85 + crop + 缩放位移")
print("=" * 78)
for mode in BLENDS:
    m, p90 = compose_qpainter(3, mode)
    print(f"  3 层 {mode:12s} : {m:7.1f} ms/帧   p90 {p90:7.1f}  → {1000/m:5.1f} fps")
m, p90 = compose_qpainter(4, "SourceOver")
print(f"  4 层 SourceOver   : {m:7.1f} ms/帧  → {1000/m:5.1f} fps")
m, p90 = compose_qpainter(3, "SourceOver", crop=False, xform=False)
print(f"  3 层 SourceOver 直接铺满(无crop/无缩放): {m:7.1f} ms/帧 → {1000/m:5.1f} fps")

print()
print("=" * 78)
print("小尺寸预览画布（编辑器预览窗口通常 960x540 或更小）")
print("=" * 78)


def compose_small(w, h, nlayers=3, n=10):
    cv = QImage(w, h, QImage.Format_ARGB32)
    imgs = [make_layer(w, h, seed=i) for i in range(nlayers)]
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        cv.fill(Qt.black)
        p = QPainter(cv)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        for i in range(nlayers):
            p.setCompositionMode(QPainter.CompositionMode_Screen)
            p.setOpacity(0.85)
            p.drawImage(QRectF(0, 0, w, h), imgs[i], QRectF(0, 0, w, h))
        p.end()
        ts.append((time.perf_counter() - t0) * 1000)
    return np.mean(ts)


for (w, h) in ((960, 540), (640, 360), (480, 270)):
    m = compose_small(w, h, 3)
    print(f"  {w}x{h} 3 层 Screen : {m:6.2f} ms/帧 → {1000/m:6.1f} fps")

print()
print("=" * 78)
print("numpy 逐像素混合（subtract）与整数运算开销，1920x1080 ARGB32")
print("=" * 78)
dst = make_layer()
src = make_layer(seed=9)


def np_view(img):
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    return np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)[:, :w * 4].reshape(h, w, 4)


dv, sv = np_view(dst), np_view(src)
# 安全的饱和减：astype(int16) 累加再 clip
t0 = time.perf_counter()
a = dv.astype(np.int16) - sv.astype(np.int16)
a = np.clip(a, 0, 255).astype(np.uint8)
t_safe = (time.perf_counter() - t0) * 1000
print(f"  astype(int16)+clip 饱和减 : {t_safe:7.1f} ms/帧")

# 原地无分配写法（先求反再取 max：255-src 后 np.maximum）
t0 = time.perf_counter()
tmp = 255 - sv
np.maximum(dv, tmp, out=dv)   # 近似 subtract（对 alpha 一并处理，仅测速度）
t_fast = (time.perf_counter() - t0) * 1000
print(f"  原地 255-src + maximum   : {t_fast:7.1f} ms/帧（零拷贝）")

t0 = time.perf_counter()
_ = dv.copy()
print(f"  单次 (h,w,4) 整幅拷贝     : {(time.perf_counter()-t0)*1000:7.1f} ms（作为内存带宽基线）")
