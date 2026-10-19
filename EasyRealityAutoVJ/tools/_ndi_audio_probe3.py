# -*- coding: utf-8 -*-
"""NDI 音频咔哒根因取证 #3：直击 soundcard 的**静音填零 + 帧数估算**。

已知（读 mediafoundation.py:_record_chunk 得到）：
  WASAPI loopback 在「驱动不提供帧」持续 > default_block_length*4（典型 40ms）时，
  soundcard 会**返回一整段 numpy.zeros**，帧数还是「采样率 × 估算时长」——
  这样硬凑出来的块：① 长度不等于 BLOCK；② 边界与真实音频不连续 ⇒ 咔哒。

本探针直接复现这条路径，统计：
  - 出块长度 != 1024 的次数（零填充/估算的指纹）
  - 全零块次数
  - 相邻块「真实→零」跳变的幅度
"""
import sys
import time

import numpy as np

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
BLOCK = 1024


def main():
    import soundcard as sc

    loop = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    mic = None
    for m in loop:
        if "realtek" in m.name.lower():
            mic = m
            break
    mic = mic or loop[0]
    print("设备: %s" % mic.name)
    sr = 48000

    n_calls = 0
    n_toolong = 0        # 返回 > BLOCK
    n_toshort = 0        # 返回 < BLOCK
    n_zeroblk = 0        # 整块（近似）全零
    n_seam = 0           # 从真实音频 → 零 的边界
    zeros_frames = 0     # 零填充的总帧数
    total_frames = 0
    prev_rms = None
    prev_t = None
    max_len = 0
    len_hist = {}
    t0 = time.perf_counter()

    with mic.recorder(samplerate=sr, channels=2) as rec:
        while time.perf_counter() - t0 < SECS:
            d = rec.record(numframes=BLOCK)
            if d is None:
                continue
            n_calls += 1
            d = np.asarray(d, dtype=np.float32)
            n = d.shape[0]
            total_frames += n
            max_len = max(max_len, n)
            len_hist[n] = len_hist.get(n, 0) + 1
            if n > BLOCK:
                n_toolong += 1
            elif n < BLOCK:
                n_toshort += 1
            x = d[:, 0] if d.ndim == 2 else d
            r = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
            if r < 1e-7:
                n_zeroblk += 1
                zeros_frames += n
            if prev_rms is not None and prev_rms > 1e-4 and r < 1e-7:
                n_seam += 1
            prev_rms = r

    dt = time.perf_counter() - t0
    print("-" * 56)
    print("时长 %.2fs  调用 %d 次  总帧 %d  速率比 %.4f" % (dt, n_calls, total_frames, (total_frames / dt) / sr))
    print("块长度分布（前 8 种）:")
    for k in sorted(len_hist, key=lambda z: -len_hist[z])[:8]:
        print("    len=%-6d 出现 %d 次" % (k, len_hist[k]))
    print("最长块 %d 帧（BLOCK=%d）" % (max_len, BLOCK))
    print("超长块(>%d) %d   短块(<%d) %d   近零块 %d" % (BLOCK, n_toolong, BLOCK, n_toshort, n_zeroblk))
    print("零填充帧数 %d（占比 %.2f%%）" % (zeros_frames, zeros_frames / max(1, total_frames) * 100))
    print("「有声→零」边界次数 %d  ← 每出现一次就是一声潜在咔哒" % n_seam)
    print("-" * 56)
    if n_toolong or n_toshort:
        print("★ 存在长度 != %d 的块 —— soundcard 在走『静音估算帧数』路径（咔哒确认）" % BLOCK)
    if n_seam:
        print("★ 出现 %d 次「有声→零」突降 —— 这就是听到的咔哒" % n_seam)
    return 0


if __name__ == "__main__":
    sys.exit(main())
