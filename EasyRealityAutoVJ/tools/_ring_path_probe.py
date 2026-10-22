# -*- coding: utf-8 -*-
"""用**真实环形缓冲**复现应用那条路径：录 12s → 逐块写进 _AudioRing → last(4.5s) → match。

目的：把"缓冲区里的音频"与"我直接录的音频"分开。
若两者票数一致 ⇒ 环形缓冲没问题，问题在应用"采到了别的音频"。
"""
import os, sys, shutil, tempfile
sys.path.insert(0, "src")
import numpy as np, soundcard as sc
import fp as fpvj
from audio_engine import _AudioRing

SR = 48000
spk = sc.default_speaker()
mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
with mic.recorder(samplerate=SR, channels=2) as rec:
    data = rec.record(numframes=int(SR * 12.0))
mono_all = np.ascontiguousarray(data[:, 0], dtype=np.float32)   # 现场口径：左声道
print("录到 %d 点  RMS=%.4f" % (len(mono_all), float(np.sqrt(np.mean(mono_all**2)))))

ring = _AudioRing(SR * 12)
for i in range(0, len(mono_all), 1440):          # 30ms 一块，与生产 _block 同量级
    ring.write(mono_all[i:i + 1440])
print("ring.filled=%d cap=%d" % (ring.filled, ring.cap))

db_src = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
tmp = os.path.join(tempfile.mkdtemp(), "fp.db"); shutil.copyfile(db_src, tmp)
db = fpvj.FingerprintDB(tmp)

for tag, seg in (("ring.last(4.5s)", ring.last(int(SR * 4.5))),
                 ("原始尾部 4.5s", mono_all[-int(SR * 4.5):])):
    sid, votes, off = db.match(np.ascontiguousarray(seg, np.float32), SR)
    print("  %-18s n=%d rms=%.3f → sid=%-12s votes=%-4s off=%.2f"
          % (tag, len(seg), float(np.sqrt(np.mean(seg**2))), sid, votes, off))
db.close()
