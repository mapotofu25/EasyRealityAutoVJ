# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _gridfix5_hintw as H
import fp as FP
data = H.A.load()
starts = [0, 6, 12, 18, 24]
for bpm in (90, 128, 155, 200):
    bl = 60.0 / bpm
    row = []
    for w in (32,):
        tot = sum(H.replay(H.POLICY_FIXED, d["wins"], d["exp"], bl, d["anchor"], st, False, w)[0]
                  for d in data for st in starts)
        row.append(("fixed%df" % w, round(tot)))
    for w in (1.5, 2.0, 2.5, 3.0, 4.0):
        tot = sum(H.replay(H.A.POLICY_AFTER, d["wins"], d["exp"], bl, d["anchor"], st, True, w)[0]
                  for d in data for st in starts)
        row.append(("beats%.1f" % w, round(tot)))
    print("BPM=%3d (bl=%.4f):" % (bpm, bl), "  ".join("%s=%d" % r for r in row))
