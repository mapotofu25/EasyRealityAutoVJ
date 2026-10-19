# -*- coding: utf-8 -*-
"""ERA 与 ERC 的指纹算法**逐项对照**：常量 / 三个核心函数源码 / 同文件实算结果。"""
import os, re, sys, io, inspect
sys.path.insert(0, "src")
sys.path.insert(0, r"P:/ERAVJ/EasyRealityCue/src")
import numpy as np
import fp as ERA
from ercue.model import fp_core as ERC

CONSTS = ["SAMPLE_RATE", "FFT_WINDOW", "HOP_SIZE", "FAN_VALUE", "AMP_MIN", "HASH_LEN",
          "MAX_HASH_TIME_DELTA", "DT_QUANT", "FREQ_QUANT", "PEAK_NEIGHBORHOOD",
          "FP_SCHEMA_VERSION"]
print("=== ① 常量 ===")
bad = 0
for c in CONSTS:
    a = getattr(ERA, c, "<缺>"); b = getattr(ERC, c, "<缺>")
    same = (a == b)
    bad += 0 if same else 1
    print("  %-22s ERA=%-6s ERC=%-6s %s" % (c, a, b, "✅" if same else "❌"))

print("\n=== ② 核心函数源码（去掉注释/空行后逐字节比）===")
def norm(fn):
    src = inspect.getsource(fn)
    out = []
    for line in src.splitlines():
        s = line.split("#")[0].rstrip()
        if s.strip():
            out.append(s)
    return "\n".join(out)

for name in ("_spectrogram", "_find_peaks", "_hashes", "fingerprint_audio"):
    fa, fb = getattr(ERA, name, None), getattr(ERC, name, None)
    if fa is None or fb is None:
        print("  %-18s ❌ 有一端没有这个函数" % name); bad += 1; continue
    ta, tb = norm(fa), norm(fb)
    if ta == tb:
        print("  %-18s ✅ 完全一致（%d 行有效代码）" % (name, len(ta.splitlines())))
    else:
        bad += 1
        print("  %-18s ❌ 不同" % name)
        import difflib
        for d in list(difflib.unified_diff(ta.splitlines(), tb.splitlines(),
                                           "ERA", "ERC", lineterm=""))[:12]:
            print("     " + d)

print("\n=== ③ 同一文件实算（同一段音频，两边各算一遍哈希）===")
import soundfile as sf
for path in (r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac",
             r"Y:/EasyRealityAutoVJ/码表素材/Nimbus Spark 1999 - Supa7onyz.flac"):
    if not os.path.isfile(path):
        print("  缺文件:", path); continue
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
    a = ERA.fingerprint_audio(mono, sr)
    b = ERC.fingerprint_audio(mono, sr)
    eq = (len(a) == len(b)) and all(x == y for x, y in zip(a[:2000], b[:2000]))
    all_eq = (len(a) == len(b)) and all(x == y for x, y in zip(a, b))
    print("  %-42s %d Hz  条数 %d/%d  前2000条=%s 全量=%s"
          % (os.path.basename(path)[:42], sr, len(a), len(b),
             "✅" if eq else "❌", "✅" if all_eq else "❌"))
    if not all_eq:
        bad += 1
print("\n结论：%s" % ("✅ 两端指纹算法**完全一致**" if bad == 0 else "❌ 有 %d 处不一致" % bad))
