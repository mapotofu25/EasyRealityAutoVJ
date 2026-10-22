# -*- coding: utf-8 -*-
"""实测：QImage→BGR 视图的四条路子，挑最快且逐像素等价的那条。

背景（两条审计线独立指向同一点）：
  `postprocess._np_view_bgr` 现在用 `np.ascontiguousarray(...[:, :, :3])`，
  在 1280x720 上实测 9.29ms，1080p 上 19.6ms —— 每帧白烧掉 1/3 ~ 1.3 个核。
"""
from __future__ import annotations

import os
import sys

import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

import cv2                                                       # noqa: E402
from PySide6.QtGui import QImage                                 # noqa: E402

N = 30


def bench(name, fn):
    fn()                                    # 预热
    t0 = cv2.getTickCount()
    for _ in range(N):
        fn()
    dt = (cv2.getTickCount() - t0) / cv2.getTickFrequency() / N * 1000.0
    print("  %-34s %6.2f ms" % (name, dt))
    return dt


def make_img(w, h):
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 255, (h, w, 4), dtype=np.uint8)
    arr[:, :, 3] = 200
    img = QImage(arr.data, w, h, w * 4, QImage.Format_ARGB32)
    return img.copy()          # copy ⇒ 自己持有数据，不依赖临时数组

def old_view(img):
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return np.ascontiguousarray(arr[:, :w * 4].reshape(h, w, 4)[:, :, :3])


def fast_view(img):
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    v4 = arr[:, :w * 4].reshape(h, w, 4)
    return cv2.cvtColor(v4, cv2.COLOR_BGRA2BGR)


def main():
    for (w, h) in ((1280, 720), (1920, 1080)):
        img = make_img(w, h)
        print("\n%d x %d" % (w, h))
        t_old = bench("旧：ascontiguousarray 跨步拷贝", lambda: old_view(img))
        t_new = bench("新：cv2.cvtColor(BGRA2BGR)", lambda: fast_view(img))
        print("  → 提速 %.1f×（省 %.2f ms/帧）" % (t_old / max(1e-9, t_new),
                                                  t_old - t_new))
        a, b = old_view(img), fast_view(img)
        same = (a.shape == b.shape) and np.array_equal(a, b)
        print("  逐像素等价：%s" % ("是 ✓" if same else "否 ✗（不允许上线）"))
        if not same:
            print("    最大差 %d" % int(np.abs(a.astype(int) - b.astype(int)).max()))

        # 写回的两条路子
        bgr = fast_view(img)
        def wb_old():
            bpl = img.bytesPerLine()
            dst = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
            dst[:, :w * 4].reshape(h, w, 4)[:, :, :3] = bgr
        def wb_new():
            bpl = img.bytesPerLine()
            v4 = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)[
                :, :w * 4].reshape(h, w, 4)
            alpha = v4[:, :, 3].copy()
            cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA, dst=v4)
            v4[:, :, 3] = alpha
        t1 = bench("旧：跨步赋值 dst[:,:,:3] = bgr", wb_old)
        t2 = bench("新：cvtColor 到 dst + 回填 alpha", wb_new)
        print("  → 提速 %.1f×（省 %.2f ms/帧）" % (t1 / max(1e-9, t2), t1 - t2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
