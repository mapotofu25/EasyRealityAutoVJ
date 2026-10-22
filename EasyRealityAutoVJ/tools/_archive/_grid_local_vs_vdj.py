# -*- coding: utf-8 -*-
"""冷启动本地锚点 vs VirtualDJ 真值：八拍网格到底能不能不靠 VDJ。

对每首歌：
  · VDJ 真值  = 库里的 BPM + 锚点（Phase，小节头）
  · 本地冷启动 = analyze_signal(不给任何 hint)，走我们自己的 BPM 扫描 + phase_of + phrase_phase
然后比较：BPM 误差 / 亚拍残差 / 小节级偏差 / 八拍级偏差。

用法：venv/Scripts/python.exe -u tools/_grid_local_vs_vdj.py [目录]
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import numpy as np              # noqa: E402
import beatgrid as bg           # noqa: E402

DIR = sys.argv[1] if len(sys.argv) > 1 else "W:/临时结界相关/2of/tofu"


def wrap(x, m):
    """把 x 折到 [-m/2, m/2)"""
    return (x + m / 2.0) % m - m / 2.0


def main():
    vdj = bg.load_vdj()
    files = sorted(f for f in os.listdir(DIR)
                   if f.lower().endswith((".flac", ".mp3", ".wav", ".m4a", ".aiff")))
    print("VDJ 库 %d 首；测试目录 %d 首" % (len(vdj), len(files)))
    print("\n%-44s %7s %7s %8s %8s %7s %7s"
          % ("文件", "VDJ BPM", "自算", "亚拍残差", "小节偏差", "八拍", "秒"))
    print("-" * 100)

    n = bpm_ok = bar_ok = phr_ok = 0
    tot = 0.0
    rows = []
    for f in files:
        p = os.path.join(DIR, f)
        dur = bg._file_duration(p)
        e = bg.vdj_lookup(vdj, p, dur)
        if not e or not e.get("beat_len"):
            continue
        t0 = time.perf_counter()
        mono, sr = bg.load_mono(p, maxsec=180.0)
        a = bg.analyze_signal(mono, sr, anchor_hint=None, bpm_hint=None)  # ★ 冷启动
        el = time.perf_counter() - t0
        tot += el

        bl = e["beat_len"]
        off = (a["anchor"] - e["anchor"]) / bl          # 本地锚点 相对 VDJ 锚点（拍）
        sub = wrap(off, 1.0)                            # 亚拍残差：phase_of 准不准
        bar = wrap(off, 4.0)                            # 小节级：0 = 同一个小节头
        phr = wrap(off, 8.0)                            # 八拍级：0 = 同一个乐句头
        n += 1
        if abs(a["bpm"] - e["bpm"]) < 1.0:
            bpm_ok += 1
        if abs(bar) < 0.25:
            bar_ok += 1
        if abs(phr) < 0.25:
            phr_ok += 1
        rows.append((f, e["bpm"], a["bpm"], sub, bar, phr, el))
        print("%-44s %7.2f %7.2f %8.3f %8.3f %7.2f %7.2f"
              % (f.replace("Rolling Contact - ", "")[:44], e["bpm"], a["bpm"],
                 sub, bar, phr, el))

    print("-" * 100)
    print("样本 %d 首，总耗时 %.1fs（平均 %.2fs/首）" % (n, tot, tot / max(n, 1)))
    print("BPM 一致（±1）      : %d/%d" % (bpm_ok, n))
    print("小节级对齐（±0.25拍）: %d/%d" % (bar_ok, n))
    print("八拍级对齐（±0.25拍）: %d/%d" % (phr_ok, n))

    if rows:
        subs = np.array([abs(r[3]) for r in rows])
        bars = np.array([abs(r[4]) for r in rows])
        print("\n亚拍残差 |中位 %.3f| 最大 %.3f 拍" % (np.median(subs), subs.max()))
        print("小节偏差 |中位 %.3f| 最大 %.3f 拍  ← 这才是「本地能不能用」的关键"
              % (np.median(bars), bars.max()))
        print("\n⚠ 判据：小节偏差要接近 0 本地才可用；接近 1~2 拍 ⇒ 锚点本身就错，"
              "谈不上再判八拍那 1 bit。")
    print("OK")


if __name__ == "__main__":
    main()
