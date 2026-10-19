# -*- coding: utf-8 -*-
"""造「延迟尺子」视频：每帧灰度 = 该秒内的帧序号 ⇒ **截一张图就能读出画面内容时间**。

原理：第 k 帧（30fps）灰度 = 20 + 215 × ((k % 30) / 29)
  · 每 30 帧（1 秒）锯齿复位一次 ⇒ 一次截图得到「秒以下的小数相位」
  · 配合挂钟（±2 秒足够）可唯一反解出**画面此刻显示的是曲内第几秒**
  · 于是：延迟 = 画面内容时间 − 音频真实位置，**逐帧可测**，不依赖任何事件检测

用法：python tools/_mk_ruler.py [输出.mp4] [时长秒]
"""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", "_tempo", "ruler.mp4")
W, H, FPS = 1280, 720, 30


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else OUT
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    os.makedirs(os.path.dirname(out), exist_ok=True)
    import cv2
    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    if not vw.isOpened():
        print("!! VideoWriter 打不开：%s" % out)
        return 1
    n = int(dur * FPS)
    for k in range(n):
        # ★ 2 秒一个周期（60 帧）：1 秒周期在 ±0.5s 处解不出方向（正好是折返点）
        g = 20 + int(round(215.0 * ((k % (2 * FPS)) / float(2 * FPS - 1))))
        frame = np.full((H, W, 3), g, dtype=np.uint8)
        vw.write(frame)
    vw.release()
    print("已生成 %s   %d 帧 / %.0fs / %dx%d@%d" % (out, n, dur, W, H, FPS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
