# -*- coding: utf-8 -*-
"""离线核对：变速后的音频还能不能被**真指纹库**认出来、报出的 offset 是不是 1.1×/0.9× 线性缩放。

这是真机测试的前置：认不出来的话，真机上看到的"没接管"就全是识别问题，不是对齐问题。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np

import fp as F

DB = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
TEMPO = os.path.join(ROOT, "tools", "_tempo")


def main():
    import soundfile as sf
    db = F.FingerprintDB(DB)
    print("FP_SCHEMA_VERSION=%d  db=%s" % (F.FP_SCHEMA_VERSION, DB))
    for rate in (1.10, 0.90):
        p = os.path.join(TEMPO, "lanyue_%.2f.wav" % rate)
        if not os.path.isfile(p):
            print("!! 缺 %s" % p)
            continue
        with sf.SoundFile(p) as f:
            sr = f.samplerate
            print("\n=== rate=%.2f  %s  sr=%d ===" % (rate, os.path.basename(p), sr))
            for at in (5.0, 40.0, 80.0, 120.0, 160.0):
                f.seek(int(at * sr))
                seg = f.read(int(6.0 * sr), dtype="float32", always_2d=True)
                if not len(seg):
                    continue
                mon = seg.mean(axis=1)
                sid, votes, off = db.match(mon, sr)
                # 变速率下，报出的 off ≈ 原始曲内的 180 + at×rate
                exp = 180.0 + at * rate
                print("  @%5.1fs → sid=%-4d votes=%-5d off=%8.2fs  期望≈%.1fs  Δ=%+.2fs"
                      % (at, sid, votes, off, exp, off - exp))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
