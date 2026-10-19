# -*- coding: utf-8 -*-
"""「减少」混合模式的单帧微基准：现行实现 vs 优化实现。

只测纯计算与内存分配，不起引擎、不碰 GPU、不读素材。
用法：venv/Scripts/python.exe -u tools/_subtract_bench.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")

import numpy as np                              # noqa: E402
from PySide6.QtWidgets import QApplication      # noqa: E402
from PySide6.QtGui import QImage                # noqa: E402
from PySide6.QtCore import Qt                   # noqa: E402

app = QApplication([])
import cv2                                      # noqa: E402

W, H = 1920, 1080
N_LAYERS = 3
OPACITY = 0.6
REPEAT = 20


def np_view(img):
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * img.height()).reshape(img.height(), bpl)
    return arr[:, :img.width() * 4].reshape(img.height(), img.width(), 4)


rng = np.random.default_rng(7)


def make_img():
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    v = np_view(img)
    v[:] = rng.integers(0, 256, size=v.shape, dtype=np.uint8)
    return img


canvas = make_img()
tmps = [make_img() for _ in range(N_LAYERS)]


# ---------------- 现行实现（engine._apply_subtract 原样） ----------------
def cur_subtract(canvas, jobs):
    for tmp, opacity in jobs:
        try:
            c = np_view(canvas).astype(np.int16)
            t = np_view(tmp).astype(np.int16)
            rgb = np.clip(c[:, :, :3] - (t[:, :, :3] * opacity), 0, 255)
            c[:, :, :3] = rgb
            np_view(canvas)[:, :, :3] = c[:, :, :3].astype(np.uint8)
        except Exception:
            pass


# ---------------- 优化实现（整型 + cv2 饱和减 + 缓存 LUT） ----------------
_LUT = {}


def sub_lut(op):
    key = round(float(op), 4)
    lut = _LUT.get(key)
    if lut is None:
        # 与 engine._sub_lut 同口径：ceil + float64 ⇒ 与旧实现逐位一致
        lut = np.clip(np.ceil(np.arange(256, dtype=np.float64) * key), 0, 255).astype(np.uint8)
        _LUT[key] = lut
    return lut


_SCRATCH = {}


def scratch3(h, w):
    a = _SCRATCH.get(("3", h, w))
    if a is None:
        a = np.empty((h, w, 3), np.uint8)
        _SCRATCH[("3", h, w)] = a
    return a


def scratch4(h, w):
    a = _SCRATCH.get(("4", h, w))
    if a is None:
        a = np.empty((h, w, 4), np.uint8)
        _SCRATCH[("4", h, w)] = a
    return a


def new_subtract(canvas, jobs):
    """B1：3 通道 + 拷回（cv2 dst 必须连续，而 canvas[:, :, :3] 不连续）"""
    h, w = canvas.height(), canvas.width()
    cnv = np_view(canvas)[:, :, :3]
    for tmp, opacity in jobs:
        tv = np_view(tmp)[:, :, :3]
        scr = scratch3(h, w)
        if opacity >= 0.999:
            cv2.subtract(cnv, tv, dst=scr)
        else:
            cv2.LUT(tv, sub_lut(opacity), dst=scr)
            cv2.subtract(cnv, scr, dst=scr)
        cnv[:] = scr


def new_subtract4(canvas, jobs):
    """B2：整幅 4 通道（canvas 连续可作 dst），把 operand 的 alpha 置 0 后一起算"""
    h, w = canvas.height(), canvas.width()
    cvv = np_view(canvas)
    for tmp, opacity in jobs:
        scr = scratch4(h, w)
        np.copyto(scr, np_view(tmp))
        scr[:, :, 3] = 0
        if opacity < 0.999:
            cv2.LUT(scr, sub_lut(opacity), dst=scr)   # alpha=0，缩放后仍为 0
        cv2.subtract(cvv, scr, dst=cvv)


# ---------------- 附带测：每帧新建 tmp QImage 的代价 ----------------
def bench_alloc():
    t0 = time.perf_counter()
    for _ in range(REPEAT):
        t = QImage(W, H, QImage.Format_ARGB32)
        t.fill(Qt.transparent)
    return (time.perf_counter() - t0) / REPEAT * 1000


def bench(fn, jobs, label):
    # 预热 2 帧
    for _ in range(2):
        fn(canvas, jobs)
    t0 = time.perf_counter()
    for _ in range(REPEAT):
        fn(canvas, jobs)
        jobs[0][0].fill(Qt.transparent)   # 防止 tmp 被减到 0 影响后续（无意义但保持一致）
    ms = (time.perf_counter() - t0) / REPEAT * 1000
    print(f"  {label:<34s} {ms:8.2f} ms/帧   核当量 {ms / 1000 * 60:5.2f}（@60fps）")
    return ms


print(f"分辨率 {W}x{H}，减少图层数 {N_LAYERS}，透明度 {OPACITY}，每项 {REPEAT} 帧取均值")
print(f"（60fps 单帧预算 = 16.67 ms）\n")

jobs1 = [(tmps[0], OPACITY)]
jobs3 = [(t, OPACITY) for t in tmps]

print("【A】现行实现（astype(int16) + float 相乘 + clip）")
a1 = bench(cur_subtract, jobs1, "1 个减少图层")
a3 = bench(cur_subtract, jobs3, "3 个减少图层")

print("\n【B1】优化：3 通道 cv2 饱和减 + 缓存 LUT + 复用 scratch + 拷回")
b1 = bench(new_subtract, jobs1, "1 个减少图层")
b3 = bench(new_subtract, jobs3, "3 个减少图层")

print("\n【B2】优化：整幅 4 通道原地 cv2 饱和减（alpha 置 0 保护）")
c1 = bench(new_subtract4, jobs1, "1 个减少图层")
c3 = bench(new_subtract4, jobs3, "3 个减少图层")

alloc = bench_alloc()
print(f"\n【C】每帧新建 tmp QImage + fill（现行做法，每个减少图层一次）")
print(f"  {'QImage(1920x1080,ARGB32)+fill':<34s} {alloc:8.2f} ms/次")

print("\n" + "=" * 62)


def speedup(base, new, tag):
    print(f"  提速（{tag}）1 层 {base[0] / new[0]:6.1f}×   3 层 {base[1] / new[1]:6.1f}×   "
          f"3 层省 {(base[1] + 3 * alloc) - new[1]:7.1f} ms/帧 = "
          f"{(base[1] + 3 * alloc - new[1]) / 1000 * 60:5.2f} 核@60fps")


speedup((a1, a3), (b1, b3), "B1")
speedup((a1, a3), (c1, c3), "B2")

# 正确性：与现行实现逐像素对比
# ⚠ 必须用**全新的** tmp：上面 bench() 会把 tmps 反复 fill 成全透明，
#    拿它做对照会得到"差 0"的假象（踩过一次）。
sub = make_img()
src = make_img()
ref = QImage(src)
cur_subtract(ref, [(sub, OPACITY)])
for fn, tag in ((new_subtract, "B1"), (new_subtract4, "B2")):
    got = QImage(src)
    fn(got, [(sub, OPACITY)])
    d = np.abs(np_view(ref)[:, :, :3].astype(np.int16) - np_view(got)[:, :, :3].astype(np.int16))
    alpha_same = bool((np_view(ref)[:, :, 3] == np_view(got)[:, :, 3]).all())
    print(f"  {tag} 与现行实现差异：最大 {d.max()} / 平均 {d.mean():.3f}；"
          f"alpha 位保持不变：{alpha_same}")
print("OK")
