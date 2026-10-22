# -*- coding: utf-8 -*-
"""NDI 音频咔哒：新旧采集路径 A/B 离线对比（不接 NDI，只看时间轴质量）。

对比：
  A. soundcard 原路径：record(numframes=1024)  ← 现状
  B. 新路径：wasapi_capture 按驱动原生包长 480 取

指标（这三个是"咔哒"的直接来源）：
  1. 出块间隔抖动（P99 / max）—— 抖动大 = 时间轴被拉
  2. 累计速率偏差（送出去的名义样本数 vs 真实经过时间）—— 偏差 = 接收端持续校准
  3. 「有声→近零」突降次数 —— 每次都可能是一声咔哒

判据：B 的 ①②③ 全面优于 A，则新路径确认有效。
"""
import sys
import time

import numpy as np

sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0


def pick_mic():
    import soundcard as sc
    loop = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    mic = None
    for m in loop:
        if "realtek" in m.name.lower():
            mic = m
            break
    return mic or loop[0]


def measure(rec, numframes, secs):
    """跑一段时间，返回统计。"""
    t0 = time.perf_counter()
    lens = []
    qpcs = []
    gaps = []
    total = 0
    seams = 0
    prev_rms = None
    t_prev = None
    while time.perf_counter() - t0 < secs:
        d = rec.record(numframes=numframes)
        t_now = time.perf_counter()
        if d is None:
            continue
        n = d.shape[0]
        lens.append(n)
        total += n
        x = d[:, 0] if d.ndim == 2 else d
        r = float(np.sqrt(np.mean(x * x)))
        if prev_rms is not None and prev_rms > 1e-4 and r < 1e-6:
            seams += 1
        prev_rms = r
        if t_prev is not None:
            gaps.append(t_now - t_prev)
        t_prev = t_now
        q = getattr(rec, "last_qpc", 0)
        if q:
            qpcs.append(q)
    dt = time.perf_counter() - t0
    g = np.array(gaps) * 1e3 if gaps else np.array([0.0])
    res = {
        "dt": dt,
        "blocks": len(lens),
        "total": total,
        "ratio": (total / dt) / 48000.0,
        "gap_mean": g.mean(),
        "gap_p99": float(np.percentile(g, 99)),
        "gap_max": g.max(),
        "seams": seams,
        "disc": getattr(rec, "disc_count", 0),
        "silent": getattr(rec, "silent_count", 0),
        "qpc_std": 0.0,
    }
    if len(qpcs) > 3:
        dq = np.diff(np.array(qpcs, dtype=np.float64)) / 1e7
        res["qpc_std"] = float(dq.std())
        res["qpc_max"] = float(dq.max())
    return res


def show(tag, r):
    print("  [%s]" % tag)
    print("    块数 %-5d 总帧 %-7d 速率比 %.4f" % (r["blocks"], r["total"], r["ratio"]))
    print("    出块间隔 ms : 均值 %.2f  P99 %.2f  max %.2f" % (r["gap_mean"], r["gap_p99"], r["gap_max"]))
    print("    有声→近零突降 : %d" % r["seams"])
    print("    驱动标志 : discontinuity=%d  silent=%d" % (r["disc"], r["silent"]))
    if r["qpc_std"]:
        print("    QPC 间隔 std %.6f s  max %.6f s" % (r["qpc_std"], r.get("qpc_max", 0)))


def main():
    mic = pick_mic()
    print("设备: %s" % mic.name)
    print("=" * 60)

    # A: soundcard 原路径
    print("A. soundcard record(numframes=1024)  [现状]")
    with mic.recorder(samplerate=48000, channels=2) as rec:
        ra = measure(rec, 1024, SECS)
    show("A", ra)

    print("-" * 60)
    # B: 新路径
    import wasapi_capture as wc
    dev = wc.wrap(mic)
    print("B. wasapi_capture record(numframes=480)  [新路径]")
    with dev.recorder(samplerate=48000, channels=2) as rec:
        rb = measure(rec, 480, SECS)
    show("B", rb)

    print("=" * 60)
    print("对比结论：")
    print("  速率偏差   A %+.3f%%   B %+.3f%%" % ((ra["ratio"] - 1) * 100, (rb["ratio"] - 1) * 100))
    print("  间隔 P99   A %.2f ms   B %.2f ms" % (ra["gap_p99"], rb["gap_p99"]))
    print("  间隔 max   A %.2f ms   B %.2f ms" % (ra["gap_max"], rb["gap_max"]))
    print("  突降次数   A %d        B %d" % (ra["seams"], rb["seams"]))
    if rb["gap_p99"] < ra["gap_p99"] * 0.6 and abs(rb["ratio"] - 1) < abs(ra["ratio"] - 1):
        print("★ B 路径显著更优：抖动更小、速率更准 ⇒ 确认可消除咔哒")
    else:
        print("⚠ B 优势不明显，需要进一步分析")
    return 0


if __name__ == "__main__":
    sys.exit(main())
