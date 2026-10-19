# -*- coding: utf-8 -*-
"""单曲能量诊断（无界面）：把 energy 公式的每个分量摊开到时间轴上。

用的是**软件内部同一套分析器**（audio_engine._Analyzer），只是用假时钟离线喂数据
（与 tools/energy_probe.py 的「文件分析」页完全同一路径）。

用法：
    venv/Scripts/python.exe -u tools/_energy_probe_one.py <音频路径> [起点秒] [终点秒]

输出：CSV（P:/AutoVJ/_energy_one.csv）+ 控制台摘要 + 指定区间的逐拍明细。
"""
import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import numpy as np              # noqa: E402

PATH = sys.argv[1]
T0 = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
T1 = float(sys.argv[3]) if len(sys.argv) > 3 else -1.0
OUT = os.path.join(_ROOT, "_energy_one.csv")


def main():
    import soundfile as sf
    import audio_engine as AE
    from audio_engine import _Analyzer, AudioState, BLOCK

    real_time = AE.time

    class FakeClock:
        t = 1e6

        @classmethod
        def perf_counter(cls):
            return cls.t

        @classmethod
        def time(cls):
            return 1.7e9 + cls.t

    data, sr = sf.read(PATH, always_2d=True, dtype="float32")
    mono = data.mean(axis=1)
    dur = len(mono) / float(sr)
    total = len(mono) // BLOCK
    print("文件 %s\n时长 %.1f s  采样率 %d  mono %d 块" % (os.path.basename(PATH), dur, sr, total))

    an = _Analyzer(sr)
    st = AudioState()
    rows = []
    AE.time = FakeClock
    try:
        for i in range(total):
            FakeClock.t += BLOCK / sr
            an.feed(mono[i * BLOCK:(i + 1) * BLOCK].reshape(-1, 1), st)
            snap = st.snapshot()
            d = snap.get("dbg") or {}
            rows.append({
                "t": i * BLOCK / sr,
                "E": float(snap["energy"]),
                "loud": float(d.get("loud", 0.0)),
                "kick": float(d.get("kick_e", 0.0)),
                "bass": float(d.get("bass_f", 0.0)),
                "fluct": float(d.get("fluct_f", 0.0)),
                "voc": float(d.get("vocal_f", 1.0)),
                "rel": float(d.get("rel", 0.0)),
            })
    finally:
        AE.time = real_time

    # 分析器内部的"慢峰/基线"（只升不降的那些）——最容易造成"越响越不给分"的地方
    print("\n=== 分析器内部状态（曲终时）===")
    for k in ("kick_peak", "bass_peak", "fluct_peak", "wdb_base", "_peak_age",
              "_kick_peak_confirmed", "energy_scale", "bflux_ref"):
        if hasattr(an, k):
            v = getattr(an, k)
            print("   %-22s = %s" % (k, ("%.4f" % v) if isinstance(v, (int, float)) else v))

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("\n逐块 CSV 已写 %s（%d 行）" % (OUT, len(rows)))

    E = np.array([r["E"] for r in rows])
    print("\n=== 全曲能量分布 ===")
    print("   E: 最小 %.3f  5%% %.3f  中位 %.3f  95%% %.3f  最大 %.3f  均值 %.3f"
          % (E.min(), np.percentile(E, 5), np.median(E), np.percentile(E, 95), E.max(), E.mean()))
    t = np.array([r["t"] for r in rows])
    # 每 8 秒一行的概览
    print("\n=== 概览（每 8 秒）===")
    print("  %-9s %6s %6s %6s %6s %6s %6s %6s" % ("时间", "E", "响度", "kick", "bass", "起伏", "人声惩罚", "rel"))
    for s in range(0, int(dur), 8):
        m = (t >= s) & (t < s + 8)
        if not m.any():
            continue
        def av(k):
            return np.mean([r[k] for r, mm in zip(rows, m) if mm])
        print("  %-9s %6.3f %6.3f %6.3f %6.3f %6.3f %6.3f %6.2f"
              % ("%d:%02d" % (s // 60, s % 60), av("E"), av("loud"), av("kick"),
                 av("bass"), av("fluct"), av("voc"), av("rel")))

    if T1 > T0:
        print("\n=== 指定区间 %.2f~%.2f 秒（逐块 0.5 秒抽样）===" % (T0, T1))
        print("  %-8s %6s %6s %6s %6s %6s %6s" % ("t", "E", "响度", "kick", "bass", "起伏", "人声惩罚"))
        last = -9
        for r in rows:
            if T0 <= r["t"] <= T1 and r["t"] - last >= 0.5:
                last = r["t"]
                print("  %-8.2f %6.3f %6.3f %6.3f %6.3f %6.3f %6.3f"
                      % (r["t"], r["E"], r["loud"], r["kick"], r["bass"], r["fluct"], r["voc"]))
    print("\nOK")


if __name__ == "__main__":
    main()
