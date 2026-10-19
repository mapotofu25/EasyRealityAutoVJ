# -*- coding: utf-8 -*-
"""单曲「八拍相位」诊断：看 0 / 4 哪个才是乐句头（用音频特征找证据）。

用法：venv/Scripts/python.exe -u tools/_phrase_probe_one.py <音频路径> [anchor] [beat_len]

输出：
  ① 8 个位置的组合得分（beatgrid.phrase_scores）
  ② 每个位置的原始特征均值（低频/高频/谱通量/低音换音）
  ③ 32 拍层的分位（看乐句长度到底是 8 拍还是 32 拍）
  ④ 8 拍周期的自相关强度（有没有真正的 8 拍周期性）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import numpy as np          # noqa: E402
import beatgrid as bg       # noqa: E402

PATH = sys.argv[1]
ANCHOR = float(sys.argv[2]) if len(sys.argv) > 2 else None
BL = float(sys.argv[3]) if len(sys.argv) > 3 else None
MAXSEC = 240.0

mono, sr = bg.load_mono(PATH, maxsec=MAXSEC)
dur = len(mono) / float(sr)
f = bg.features(mono, sr)

if BL is None:
    a = bg.analyze_signal(mono, sr)
    BL = 60.0 / a["bpm"]
    if ANCHOR is None:
        ANCHOR = a["anchor"]
    print("（未给参数，用 analyze_signal 自算）bpm=%.3f phrase=%d anchor=%.4f"
          % (a["bpm"], a["phrase"], a["anchor"]))
print("文件: %s" % os.path.basename(PATH))
print("分析 %.1f 秒 | beat_len=%.6f (BPM %.3f) | anchor=%.4f" % (dur, BL, 60.0 / BL, ANCHOR))

n = int((dur - ANCHOR) / BL)
beats = ANCHOR + np.arange(n) * BL
bf = bg.per_beat_feats(f, beats)
ks = np.arange(n)                       # k=0 就是 anchor 那一拍

sc = np.asarray(bg.phrase_scores(bf, ks % 8))
print("\n① 组合得分（phrase_scores，%d 拍参与）：" % n)
for p in range(8):
    bar = "  ← 候选乐句头" if p in (0, 4) else ""
    print("   pos %d : %6.4f   %s%s" % (p, sc[p], "#" * int(sc[p] * 60), bar))
print("   s0=%.4f  s4=%.4f  →  当前判定 phrase=%d（s0>=s4 取 0）"
      % (sc[0], sc[4], 0 if sc[0] >= sc[4] else 4))

print("\n② 每个位置的原始特征均值（未归一化）：")
def bypos(arr, name):
    a = np.asarray(arr, np.float64)
    row = [a[(ks % 8) == p].mean() for p in range(8)]
    m = np.mean(row)
    print("   %-8s" % name + "".join(" %8.4f" % v for v in row) + "   均值 %.4f" % m)
    return row

low = bypos(bf["Elow"], "Elow")
high = bypos(bf["Ehigh"], "Ehigh")
flux = bypos(bf["flux"], "flux")
clo = bypos(bf["c_low"], "c_low")

print("\n③ 32 拍层分位（同一首里第 0/8/16/24 拍的对比，看层次结构）：")
for off in range(0, 32, 8):
    idx = (ks % 32 == off)
    if idx.sum() == 0:
        continue
    print("   第 %2d 拍位: Elow %.4f  Ehigh %.4f  flux %.4f  (n=%d)"
          % (off, np.asarray(bf["Elow"])[idx].mean(), np.asarray(bf["Ehigh"])[idx].mean(),
             np.asarray(bf["flux"])[idx].mean(), idx.sum()))

print("\n④ 4 拍层分位（小节内 4 拍的位置画像，验证锚点是否真是小节头）：")
for p in range(4):
    idx = (ks % 4 == p)
    print("   第 %d 拍: Elow %.4f  Ehigh %.4f  flux %.4f  (n=%d)"
          % (p, np.asarray(bf["Elow"])[idx].mean(), np.asarray(bf["Ehigh"])[idx].mean(),
             np.asarray(bf["flux"])[idx].mean(), idx.sum()))

print("\n⑤ 8 拍周期性强度（把 8 拍位置画像做 FFT，看有没有真正的 8 拍周期）：")
prof = np.array([np.asarray(bf["Elow"])[(ks % 8) == p].mean() for p in range(8)])
prof = prof - prof.mean()
print("   Elow 位置画像:", np.round(prof, 4))
print("   第 4 拍(半周期) 分量幅度：%.4f   第 0 拍(直流已去) 残差 RMS：%.4f"
      % (abs(np.dot(prof, np.cos(2 * np.pi * np.arange(8) * 4 / 8))) / 8, float(np.std(prof))))
print("\n提示：若 ④ 里第 0 拍明显最重 ⇒ 锚点是小节头（对）；")
print("      若 ③ 里只有第 0 拍重、第 8/16/24 拍差不多 ⇒ 乐句其实是 32 拍，8 拍层本身无意义。")
