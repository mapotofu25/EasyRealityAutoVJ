# -*- coding: utf-8 -*-
"""六次取样：软解回退成本、减少混合、码表逐帧、crop 绘制。"""
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


def ms(fn, n=30, warm=3):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


print("=== 1. DXV 软解回退（GPU 不可用时每帧成本） ===")
import av  # noqa: E402

p = r"Y:\EasyRealityAutoVJ\素材库\0 (6).mov"
W, H = 1920, 1080
if os.path.exists(p):
    c = av.open(p, metadata_errors="replace")
    st = c.streams.video[0]
    print("  codec=%s pix_fmt=%s %dx%d  线程=%s"
          % (st.codec_context.name, st.codec_context.pix_fmt,
             st.codec_context.width, st.codec_context.height,
             st.codec_context.thread_count))
    frames = []
    for i, fr in enumerate(c.decode(video=0)):
        frames.append(fr)
        if i >= 60:
            break
    c.close()
    i = {"i": 0}

    def conv():
        i["i"] = (i["i"] + 1) % len(frames)
        return frames[i["i"]].to_ndarray(format="bgra")

    print("  解码已完成 %d 帧缓存，仅测 to_ndarray(bgra) 转换" % len(frames))
    print("  to_ndarray(bgra) 1080p                    %7.2f ms/帧" % ms(conv, n=30))

    # 包含解码的完整一趟（重开容器逐帧解码）
    def full_pass():
        cc = av.open(p, metadata_errors="replace")
        s2 = cc.streams.video[0]
        cnt = 0
        for fr in cc.decode(video=0):
            a = fr.to_ndarray(format="bgra")
            cnt += 1
            if cnt >= 30:
                break
        cc.close()
        return cnt

    t = ms(full_pass, n=3, warm=1) / 30.0
    print("  软解 30 帧全程（含 demux+解码+转换）        %7.2f ms/帧" % t)

    # OpenCV 路径
    def cv_pass():
        cap = cv2.VideoCapture(p)
        cnt = 0
        while cnt < 30:
            ok, b = cap.read()
            if not ok:
                break
            b = cv2.cvtColor(b, cv2.COLOR_BGR2BGRA)
            cnt += 1
        cap.release()
        return cnt

    t2 = ms(cv_pass, n=3, warm=1) / 30.0
    print("  cv2.VideoCapture 30 帧（含 cvtColor）        %7.2f ms/帧" % t2)
else:
    print("  素材不存在")

print("\n=== 2. 「减少」混合：真实路径拆解 ===")
for (w, h) in ((1280, 720), (1920, 1080)):
    rng = np.random.RandomState(4)
    cv = (rng.rand(h, w, 4) * 255).astype(np.uint8)
    tmp = (rng.rand(h, w, 4) * 255).astype(np.uint8)
    scr = np.empty((h, w, 4), np.uint8)

    def sub_one():
        np.copyto(scr, tmp)
        scr[:, :, 3] = 0
        cv2.LUT(scr, np.tile(np.arange(256, dtype=np.uint8).reshape(1,256,1), (1,1,4)), dst=scr)
        cv2.subtract(cv, scr, dst=cv)

    print("  %dx%d 每层 %.2f ms  (3 层 %.2f ms)"
          % (w, h, ms(sub_one, n=20), ms(sub_one, n=20) * 3))

print("\n=== 3. 码表：逐帧 policy + 取帧 + crop 绘制 ===")
import cue_seek_policy as CSP  # noqa: E402

print("  cue_seek_policy 公共函数：%s"
      % [n for n in dir(CSP) if not n.startswith('_')][:12])
for (w, h) in ((1280, 720), (1920, 1080)):
    rng = np.random.RandomState(5)
    raw = (rng.rand(h, w, 4) * 255).astype(np.uint8)
    canvas = QImage(raw.data, w, h, 4 * w, QImage.Format_ARGB32).copy()
    img = QImage(raw.data, w, h, 4 * w, QImage.Format_ARGB32).copy()

    def draw_full():
        p2 = QPainter(canvas)
        p2.setRenderHint(QPainter.SmoothPixmapTransform)
        p2.drawImage(QRectF(0, 0, w, h), img)
        p2.end()

    def draw_crop():
        p2 = QPainter(canvas)
        p2.setRenderHint(QPainter.SmoothPixmapTransform)
        p2.drawImage(QRectF(0, 0, w, h), img, QRectF(w * 0.1, h * 0.1, w * 0.5, h * 0.5))
        p2.end()

    print("  %dx%d crop 绘制 %.2f ms / 整幅 %.2f ms"
          % (w, h, ms(draw_crop, n=20), ms(draw_full, n=20)))
