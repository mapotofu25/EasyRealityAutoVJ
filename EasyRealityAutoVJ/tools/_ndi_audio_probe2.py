# -*- coding: utf-8 -*-
"""NDI 音频咔哒根因取证 #2：块间时间间隔的抖动 + record() 是否填零。

假设：
  A. 亏空是**稳定**的（每次都少一点点）→ 听感应是整体慢，不是咔哒
  B. 亏空是**间歇**的（大多正常、偶尔整块晚到）→ 每次追赶 = 一声咔哒  ★更可能

做法：连续 record()，记录**每次调用返回的时刻**与**返回的样本数**，
输出「块间隔」直方图统计 + 检测「返回全零块」（record 凑不满时填零的痕迹）。
"""
import sys
import time

import numpy as np

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
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

    gaps = []            # 两次 record 返回之间的时间间隔
    lens = []            # 每次返回的帧数
    zero_blocks = 0      # 全零块
    near_zero = 0        # RMS < 1e-6 的块
    short_blocks = 0     # 帧数 < BLOCK 的块
    t_prev = None
    t0 = time.perf_counter()
    total = 0

    with mic.recorder(samplerate=sr, channels=2) as rec:
        while time.perf_counter() - t0 < SECS:
            d = rec.record(numframes=BLOCK)
            t_now = time.perf_counter()
            if d is None:
                continue
            d = np.asarray(d, dtype=np.float32)
            n = d.shape[0] if d.ndim >= 1 else 0
            lens.append(n)
            total += n
            if n < BLOCK:
                short_blocks += 1
            x = d[:, 0] if d.ndim == 2 else d
            r = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
            if r == 0.0:
                zero_blocks += 1
            elif r < 1e-6:
                near_zero += 1
            if t_prev is not None:
                gaps.append(t_now - t_prev)
            t_prev = t_now

    dt = time.perf_counter() - t0
    g = np.array(gaps)
    print("-" * 56)
    print("时长 %.2fs  块数 %d  总样本 %d  速率比 %.4f" % (dt, len(lens), total, (total / dt) / sr))
    print("块间隔 ms : 均值 %.2f  中位 %.2f  min %.2f  max %.2f  标准差 %.2f"
          % (g.mean() * 1e3, np.median(g) * 1e3, g.min() * 1e3, g.max() * 1e3, g.std() * 1e3))
    # 理想 = 1024/48000 = 21.33ms
    ideal = BLOCK / sr * 1e3
    late = g[g * 1e3 > ideal * 1.5]
    print("理想块间隔 %.2f ms；超 1.5 倍的块数 %d（%.2f%%），最大超时 %.2f ms"
          % (ideal, late.size, late.size / max(1, g.size) * 100,
             (late.max() * 1e3 - ideal) if late.size else 0))
    # 分位
    for p in (50, 90, 99, 99.9):
        print("  间隔 P%-4s = %.2f ms" % (p, np.percentile(g, p) * 1e3))
    print("全零块 %d  近零块 %d  短块(<%d) %d" % (zero_blocks, near_zero, BLOCK, short_blocks))
    print("-" * 56)
    if late.size / max(1, g.size) > 0.02:
        print("★ 有 %.1f%% 的块显著晚到 → 间歇性亏空，每次追赶即一声咔哒" % (late.size / g.size * 100))
    else:
        print("★ 块到达间隔平稳 → 亏空是稳定分布，听感应是整体微慢而非咔哒")
    if zero_blocks or short_blocks:
        print("★ record() 出现填零/短块 → 采集层在制造静音间隙（咔哒的另一种来源）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
