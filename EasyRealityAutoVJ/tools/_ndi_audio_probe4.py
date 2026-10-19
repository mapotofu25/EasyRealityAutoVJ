# -*- coding: utf-8 -*-
"""取证 #4：对比 record(numframes=1024) vs record(numframes=None) 两条路径。

假设：numframes=1024 会**主动触发** soundcard 的「凑满」逻辑（含静音填零、
长度重切），而 numframes=None「有多少取多少」能拿到驱动真实的块边界与长度，
从而暴露 DATA_DISCONTINUITY 的真实频率。

同时统计：驱动丢帧的间隔分布（= 咔哒的间隔分布），与用户说的"随机不规则"比对。
"""
import sys
import time
import warnings

import numpy as np

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0


def run(numframes, secs):
    import soundcard as sc

    loop = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    mic = None
    for m in loop:
        if "realtek" in m.name.lower():
            mic = m
            break
    mic = mic or loop[0]

    # 统计 soundcard 的 discontinuity / silent 警告
    counts = {"disc": 0, "silent": 0}
    orig_warn = warnings.warn

    def _hook(message, *a, **k):
        m = str(message)
        if "discontinuity" in m:
            counts["disc"] += 1
        if "silent" in m:
            counts["silent"] += 1
        return orig_warn(message, *a, **k)

    warnings.warn = _hook
    warnings.simplefilter("always")

    t0 = time.perf_counter()
    lens = []
    zero_runs = 0
    total = 0
    gaps = []          # 两次出块之间的间隔
    t_prev = None
    with mic.recorder(samplerate=48000, channels=2) as rec:
        while time.perf_counter() - t0 < secs:
            if numframes is None:
                d = rec.record(numframes=None)
            else:
                d = rec.record(numframes=numframes)
            t_now = time.perf_counter()
            if d is None or len(d) == 0:
                time.sleep(0.001)
                continue
            n = d.shape[0]
            lens.append(n)
            total += n
            x = d[:, 0] if d.ndim == 2 else d
            r = float(np.sqrt(np.mean(x * x)))
            if r < 1e-7:
                zero_runs += 1
            if t_prev is not None:
                gaps.append(t_now - t_prev)
            t_prev = t_now
    dt = time.perf_counter() - t0
    warnings.warn = orig_warn

    L = np.array(lens)
    print("  出块 %d 次  总帧 %d  速率比 %.4f  近零块 %d" % (len(L), total, (total / dt) / 48000, zero_runs))
    print("  块长度: min %d  max %d  均值 %.1f  唯一值数 %d" % (L.min(), L.max(), L.mean(), len(set(lens))))
    if gaps:
        g = np.array(gaps) * 1e3
        print("  出块间隔 ms: 均值 %.2f  P99 %.2f  max %.2f" % (g.mean(), np.percentile(g, 99), g.max()))
    print("  ★ discontinuity 警告 %d 次  silent 警告 %d 次" % (counts["disc"], counts["silent"]))
    return counts, L


def main():
    for nf in (1024, None, 480):
        print("--- record(numframes=%r) ---" % nf)
        try:
            run(nf, SECS)
        except Exception as e:                                 # noqa: BLE001
            print("  失败: %s: %s" % (type(e).__name__, e))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
