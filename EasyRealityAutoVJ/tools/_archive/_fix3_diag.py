# -*- coding: utf-8 -*-
"""临时诊断：逐窗看 hint 选择 / 全局众数 / 正确候选，定位仍偏的窗口。"""
import json
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP  # noqa: E402

seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq = [s for s in seq if s["exp"] >= 0]
NAME = sys.argv[1] if len(sys.argv) > 1 else "Raccoon"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 15
song = [s for s in seq if NAME in s["song"]][0]
exp = song["exp"]
bl = song["wins"][0]["bl"]
print("song=%s exp=%d bl=%.4f" % (song["song"][:30], exp, bl))
for w in song["wins"][:N]:
    hist = {(c[0], c[1]): c[2] for c in w["cand"]}
    gp = FP.FingerprintDB._pick(hist, None)
    td = w["t"] * FP.SAMPLE_RATE / FP.HOP_SIZE
    nearc = max(((s, dd, v) for (s, dd), v in hist.items()
                 if s == exp and abs(dd - td) <= 48), key=lambda x: x[2], default=None)
    gerr = ((gp[0][1] * FP.HOP_SIZE / FP.SAMPLE_RATE) - w["t"]) / bl if gp else None
    nerr = ((nearc[1] * FP.HOP_SIZE / FP.SAMPLE_RATE) - w["t"]) / bl if nearc else None
    print("t=%3d | global sid=%s votes=%s err=%s | near-correct votes=%s err=%s"
          % (w["t"], gp[0][0] if gp else None, gp[1] if gp else 0,
             ("%.1f" % gerr) if gerr is not None else "NA",
             nearc[2] if nearc else 0,
             ("%.2f" % nerr) if nerr is not None else "NA"))
