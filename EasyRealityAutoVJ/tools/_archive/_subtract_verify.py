# -*- coding: utf-8 -*-
"""验证 engine._apply_subtract 新实现：与旧算法逐像素一致 + alpha 不动 + 不连续视图守卫。

用法：venv/Scripts/python.exe -u tools/_subtract_verify.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"P:/AutoVJ/src")

import numpy as np                              # noqa: E402
from PySide6.QtWidgets import QApplication      # noqa: E402
from PySide6.QtGui import QImage                # noqa: E402
from PySide6.QtCore import Qt                   # noqa: E402

app = QApplication([])
import engine as E                              # noqa: E402

W, H = 1920, 1080
rng = np.random.default_rng(11)


def view(img):
    return E.AutoVJEngine._np_view(img)


def make(alpha_mode="rand"):
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    v = view(img)
    v[:] = rng.integers(0, 256, size=v.shape, dtype=np.uint8)
    if alpha_mode == "opaque":
        v[:, :, 3] = 255
    return img


class Dummy:
    """_apply_subtract 只用到 self._np_view（staticmethod）"""
    _np_view = staticmethod(lambda img: E.AutoVJEngine._np_view(img))


def old_apply(canvas, jobs):
    for tmp, opacity in jobs:
        c = view(canvas).astype(np.int16)
        t = view(tmp).astype(np.int16)
        rgb = np.clip(c[:, :, :3] - (t[:, :, :3] * opacity), 0, 255)
        c[:, :, :3] = rgb
        view(canvas)[:, :, :3] = c[:, :, :3].astype(np.uint8)


print("=== 1) 连续性检查（新实现的前提）===")
probe = make()
v = view(probe)
print(f"  bytesPerLine={probe.bytesPerLine()}  w*4={W * 4}  "
      f"整幅连续={v.flags['C_CONTIGUOUS']}  三通道切片连续={v[:, :, :3].flags['C_CONTIGUOUS']}")

print("\n=== 2) 与旧算法逐像素对比 ===")
ok = True
for n in (1, 2, 3):
    for op in (1.0, 0.6, 0.25, 0.0):
        base = make()
        a = QImage(base)
        b = QImage(base)
        jobs = [(make(), op) for _ in range(n)]
        old_apply(a, jobs)
        E.AutoVJEngine._apply_subtract(Dummy(), b, jobs)
        ra, rb = view(a), view(b)
        d = np.abs(ra[:, :, :3].astype(np.int16) - rb[:, :, :3].astype(np.int16))
        alpha_ok = bool((ra[:, :, 3] == rb[:, :, 3]).all())
        flag = "" if (d.max() == 0 and alpha_ok) else "   ← 不一致!"
        if d.max() != 0 or not alpha_ok:
            ok = False
        print(f"  {n} 层 / 透明 {op:<4} : RGB 最大差 {d.max():>3}  平均 {d.mean():.4f}  "
              f"alpha 不变 {alpha_ok}{flag}")

print("\n=== 3) alpha 全不透明时也正确 ===")
base = make("opaque")
a, b = QImage(base), QImage(base)
jobs = [(make("opaque"), 0.5), (make("opaque"), 0.8)]
old_apply(a, jobs)
E.AutoVJEngine._apply_subtract(Dummy(), b, jobs)
ra, rb = view(a), view(b)
d = np.abs(ra[:, :, :3].astype(np.int16) - rb[:, :, :3].astype(np.int16))
print(f"  RGB 最大差 {d.max()}   alpha 保持 255：{bool((rb[:, :, 3] == 255).all())}")
ok = ok and d.max() == 0

print("\n=== 4) 非连续 / 尺寸不符时安全退出（不抛异常、不改画布）===")
weird = QImage(W + 1, H, QImage.Format_ARGB32)   # 宽为奇数但仍连续
weird.fill(Qt.transparent)
before = view(weird).copy()
E.AutoVJEngine._apply_subtract(Dummy(), weird, [(make(), 0.6)])
print(f"  无异常 ✓ 画布被改动：{not np.array_equal(before, view(weird))}")

print("\n=== 5) Layer 是否已有缓存字段 ===")
print(f"  Layer._sub_tmp 默认值 = {E.Layer('x')._sub_tmp}")

print("\n" + "=" * 58)
print("结果：" + ("✅ 全部一致，可交付" if ok else "❌ 存在不一致，先别打包"))
print("OK" if ok else "FAIL")
