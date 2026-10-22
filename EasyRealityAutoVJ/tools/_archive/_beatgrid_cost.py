# -*- coding: utf-8 -*-
"""临时实测：beatgrid 各原语的单次调用成本（帧数 / 耗时），判断能否用于实时路径。

只读分析，不改 src。跑：
  P:\\AutoVJ\\venv\\Scripts\\python.exe tools\\_beatgrid_cost.py
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import beatgrid as bg  # noqa: E402

SRT = bg.SR_TARGET  # 22050


def bench(fn, n=5, label=""):
    fn()  # warm
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    ts = np.array(ts)
    print("    %-28s 中位 %7.2f ms  (min %.2f / max %.2f)" %
          (label, np.median(ts) * 1000, ts.min() * 1000, ts.max() * 1000))
    return np.median(ts) * 1000


def main():
    rng = np.random.default_rng(0)
    for win_sec in (8.0, 30.0, 180.0):
        n = int(win_sec * SRT)
        # 合成一段有节拍感的信号（kick 每 0.5s + 噪声），只为量算力不为量准确度
        t = np.arange(n) / SRT
        sig = (0.3 * np.sin(2 * np.pi * 55 * t) * (np.sin(2 * np.pi * 2 * t) > 0)).astype(np.float32)
        sig += 0.05 * rng.standard_normal(n).astype(np.float32)
        nf_est = 1 + (n - bg.WIN) // bg.HOP
        print("\n== 窗口 %.0fs @ %dHz  →  约 %d 帧 (hop=%d, win=%d, dt=%.1fms) =="
              % (win_sec, SRT, nf_est, bg.HOP, bg.WIN, 1000.0 * bg.HOP / SRT))

        f = bg.features(sig, SRT)
        bench(lambda: bg.features(sig, SRT), 5, "features()")
        env = bg.onset_env(f, "mix")
        bench(lambda: bg.onset_env(f, "mix"), 5, "onset_env()")
        # 单周期相位（等价于实时每 0.5s 定拍位）
        bench(lambda: bg.phase_of(f, env, 0.5), 5, "phase_of(period=0.5)")
        # 整曲 BPM 扫描（最贵，离线才做）
        if win_sec <= 30.0:
            bench(lambda: bg.scan_bpm(f, env), 1, "scan_bpm() 全扫(粗+细)")
        else:
            print("    scan_bpm() 全扫                       跳过（离线步骤，非实时）")
        # 八拍相位
        bench(lambda: bg.phrase_phase(f, 0.234, 0.5), 3, "phrase_phase(anchor,period)")
        beats = np.arange(1.0, min(win_sec, len(env) * f["dt"]) - 0.1, 0.5)
        bench(lambda: bg.per_beat_feats(f, beats), 3, "per_beat_feats(%d 拍)" % len(beats))

    # ---- 增量/滑窗版的真实成本：只喂最近 4~8 秒 ----
    print("\n== 实时兜底：只喂最近 8s（每 0.5s 调一次）==")
    n = int(8.0 * SRT)
    t = np.arange(n) / SRT
    sig = (0.3 * np.sin(2 * np.pi * 55 * t) * (np.sin(2 * np.pi * 2 * t) > 0)).astype(np.float32)
    tot = 0.0
    for _ in range(20):
        t0 = time.perf_counter()
        f = bg.features(sig, SRT)
        env = bg.onset_env(f, "mix")
        ph, conc = bg.phase_of(f, env, 0.5)
        tot += time.perf_counter() - t0
    print("    features+onset_env+phase_of 合计 中位 %6.2f ms/次（20 次均值 %.2f）"
          % (1000.0 * tot / 20, 1000.0 * tot / 20))
    print("    → 每 0.5s 一次的占空比 %.2f%%（单核）"
          % (100.0 * (tot / 20) / 0.5))


if __name__ == "__main__":
    main()
