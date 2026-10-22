# -*- coding: utf-8 -*-
"""决定性对照：**同一段现场声音**，只换"怎么变单声道"，看票数差多少。

背景：建库用的是 `np.mean(data, axis=1)`（左右平均），
      而现场采集喂给匹配器的是 `data[:, 0]`（**只取左声道**）。
      指纹靠频谱峰值，左声道 vs 左右平均会改变峰值取向 ⇒ 可能一票都没有。
"""
import os, sys, shutil, tempfile
sys.path.insert(0, "src")
import numpy as np, soundcard as sc
import fp as fpvj

spk = sc.default_speaker()
mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
SR = 48000
with mic.recorder(samplerate=SR, channels=2) as rec:
    data = rec.record(numframes=int(SR * 7.0))
print("录到", data.shape, "RMS=%.4f" % float(np.sqrt(np.mean(data ** 2))))

db_src = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
tmp = os.path.join(tempfile.mkdtemp(), "fp.db")
shutil.copyfile(db_src, tmp)
db = fpvj.FingerprintDB(tmp)

n = int(SR * 4.5)
variants = [
    ("左右平均 mean(axis=1)  ← 建库口径", np.ascontiguousarray(data[:, :].mean(axis=1), np.float32)),
    ("只取左声道 data[:,0]     ← 现场口径", np.ascontiguousarray(data[:, 0], np.float32)),
    ("只取右声道 data[:,1]", np.ascontiguousarray(data[:, 1], np.float32)),
]
for tag, mono in variants:
    seg = mono[-n:]
    sid, votes, off = db.match(seg, SR)
    print("  %-34s → sid=%-12s votes=%-4s off=%.2f" % (tag, sid, votes, off))
db.close()
