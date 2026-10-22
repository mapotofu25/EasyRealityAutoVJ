# -*- coding: utf-8 -*-
"""离线复现**整条认歌链**：录 30s 系统回环 → 0.5s 滑窗 → fp_db.match → ChargeBarEngine.tick
→ 打印每一拍的 votes / 候选条 / 是否确认。

目的：把"应用里认不出"拆成"匹配层"与"确认层"，且**不用改应用、不用重启**。
"""
import os, sys, shutil, tempfile, time
sys.path.insert(0, "src")
import numpy as np, soundcard as sc
import fp as fpvj
from charge_engine import ChargeBarEngine, FpResult

SR = 48000
WIN = 4.5
SEC = 30.0
spk = sc.default_speaker()
mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
print("录 %.0fs 系统回环…（请保持播放）" % SEC)
with mic.recorder(samplerate=SR, channels=2) as rec:
    data = rec.record(numframes=int(SR * SEC))
mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)   # 建库口径
print("RMS=%.4f" % float(np.sqrt(np.mean(mono ** 2))))

db_src = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
tmp = os.path.join(tempfile.mkdtemp(), "fp.db"); shutil.copyfile(db_src, tmp)
db = fpvj.FingerprintDB(tmp)
eng = ChargeBarEngine()

step = int(SR * 0.5)
w = int(SR * WIN)
t0 = time.time()
print("\n   t     votes  sid           off      cand_bar confirmed")
for k, end in enumerate(range(w, len(mono) + 1, step)):
    seg = mono[end - w:end]
    sid, votes, off = db.match(seg, SR)
    eng.tick(FpResult(matched=(votes > 0), song_id=sid, aligned_votes=votes,
                      offset_sec=off), t0 + k * 0.5)
    if k % 2 == 0 or (k > 0 and eng.current_song_id >= 0):
        print("  %5.1fs %5d  %-12s %7.2f  %5d   %s"
              % (k * 0.5, votes, sid, off, int(getattr(eng, "cand_bar", 0)),
                 eng.current_song_id))
    if eng.current_song_id >= 0 and k > 40:
        break
print("\n最终 current_song_id =", eng.current_song_id)
db.close()
