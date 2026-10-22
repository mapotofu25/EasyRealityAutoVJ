# -*- coding: utf-8 -*-
r"""BPM 可检测范围扫描：合成不同 BPM 的鼓循环，喂真实 _Analyzer，看哪些能建立 BPM。

背景：建锁门控是 `115 <= 众数 <= 185 and conf >= 0.45 and 票数 >= 20`。
若某区间的曲子永远建不了锁，界面就会**一直显示 BPM 0**（用户 2026-09-24 报的现象）。

用法：venv\Scripts\python.exe tools\bpm_range_check.py
输出：P:\AutoVJ\_bpm_range.txt
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402
from audio_engine import AudioState, _Analyzer, BLOCK  # noqa: E402

SR = 48000
DUR = 40.0          # 每个 BPM 合成 40 秒（真实运行里也要给够投票时间）
out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def drum_loop(bpm, dur=DUR, sr=SR):
    """合成一段有明确拍感的电子鼓循环：kick(每拍) + hihat(八分) + bass(每拍长音)"""
    n = int(dur * sr)
    x = np.zeros(n, dtype=np.float32)
    beat = 60.0 / bpm
    rng = np.random.default_rng(7)
    t_kick = np.arange(int(0.18 * sr)) / sr
    kick = (np.sin(2 * np.pi * (110 * np.exp(-t_kick * 22)) * t_kick)
            * np.exp(-t_kick * 18)).astype(np.float32)
    click = (rng.standard_normal(len(t_kick)) * np.exp(-t_kick * 300) * 0.25).astype(np.float32)
    kick = kick + click
    t_hat = np.arange(int(0.04 * sr)) / sr
    hat = (rng.standard_normal(len(t_hat)) * np.exp(-t_hat * 90)).astype(np.float32)
    t_bass = np.arange(int(beat * sr)) / sr
    bass = (np.sin(2 * np.pi * 55 * t_bass) * 0.35).astype(np.float32)
    k, h = 0, 0
    while True:
        tk = int(k * beat * sr)
        if tk >= n:
            break
        e = min(n, tk + len(kick))
        x[tk:e] += kick[:e - tk] * 0.9
        b = min(n, tk + len(bass))
        x[tk:b] += bass[:b - tk] * 0.5
        th = int((k + 0.5) * beat * sr)
        if th < n:
            e2 = min(n, th + len(hat))
            x[th:e2] += hat[:e2 - th] * 0.30
        k += 1
    peak = float(np.max(np.abs(x))) or 1.0
    return (x / peak * 0.7).astype(np.float32)


def run_bpm(bpm):
    x = drum_loop(bpm)
    an = _Analyzer(SR)
    st = AudioState()
    # 真实运行里是「实时」推进（墙钟），离线跑得快会让时间判断失真，
    # 但 BPM 投票链路只用块索引（flux_i % sr//BLOCK），与墙钟无关，所以离线可测。
    for i in range(0, len(x) - BLOCK, BLOCK):
        an.feed(x[i:i + BLOCK].reshape(-1, 1), st)   # feed 要二维 (n, ch)
    votes = list(an._bpm_votes)
    mode_s = conf_s = "-"
    if len(votes) >= 6:
        v = np.array(votes, dtype=np.float32)
        hist, edges = np.histogram(v, bins=np.arange(60.0, 201.0, 1.0))
        pk = int(np.argmax(hist))
        mode_s = "%.1f" % (edges[pk] + 0.5)
        conf_s = "%.3f" % (float(hist[pk]) / len(v))
    return an.bpm_smooth, an.bpm_display, an._bpm_lock, len(votes), mode_s, conf_s


def main():
    w("=" * 96)
    w("BPM 可检测范围扫描（合成鼓循环 → 真实 _Analyzer）")
    w("=" * 96)
    w("  bpm     显示值      锁定   拍钟值   投票  众数    conf   判定")
    BAD = []
    for bpm in (85, 95, 100, 105, 110, 115, 120, 128, 140, 150, 160, 174, 180, 190):
        tk, disp, lock, nv, mode_s, conf_s = run_bpm(bpm)
        if disp <= 0:
            BAD.append(bpm)
        w("  %-7d %-11s %-6s %-8s %-5d %-7s %-7s %s"
          % (bpm, ("%.1f" % disp) if disp > 0 else "0（不显示）",
             "是" if lock else "否",
             ("%.1f" % tk) if tk > 0 else "-",
             nv, mode_s, conf_s,
             "OK" if disp > 0 else "★ 永远显示 0"))
    w("")
    w("=" * 96)
    if BAD:
        w("❌ 这些 BPM 的曲子**显示不出来**：%s" % ", ".join(str(b) for b in BAD))
    else:
        w("✅ 全部区间的曲子都能显示 BPM（拍钟值仍按原门控，未锁定的显示值带「≈」）")
    open(r"P:\AutoVJ\_bpm_range.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
