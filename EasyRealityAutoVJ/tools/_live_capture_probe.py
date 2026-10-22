# -*- coding: utf-8 -*-
"""录一段**正在播放的系统声音**，直接拿真实指纹库比对 —— 复现演出现场那条路径。

为什么必须这样：离线用 FLAC 切片能匹配（200+ 票），现场却 0 票。
差异只可能在"进到匹配器里的那段音频"本身。
"""
import os, sys, shutil, tempfile, time
sys.path.insert(0, "src")
import numpy as np

try:
    import soundcard as sc
except Exception as e:
    print("soundcard 不可用:", e); sys.exit(2)

spk = sc.default_speaker()
print("默认扬声器:", spk.name)
mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
SR = 48000
SEC = 7.0
print("录 %.1fs 系统回环…" % SEC)
with mic.recorder(samplerate=SR, channels=2) as rec:
    data = rec.record(numframes=int(SR * SEC))
mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
rms = float(np.sqrt(np.mean(mono ** 2)))
print("录到 %d 点，RMS=%.4f  %s" % (len(mono), rms, "（有声音）" if rms > 1e-3 else "（几乎静音！）"))

import fp as fpvj
db_src = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
tmp = os.path.join(tempfile.mkdtemp(), "fp.db")
shutil.copyfile(db_src, tmp)
db = fpvj.FingerprintDB(tmp)

# 用最后 4.5s（与生产 FP_WIN_SEC 一致）
n = int(SR * 4.5)
seg = mono[-n:]
for tag, sr_arg in (("按 48000 传", SR), ("按 44100 传（错配复现）", 44100)):
    sid, votes, off = db.match(seg, sr_arg)
    print("  %-22s → sid=%-12s votes=%-4s off=%.2f" % (tag, sid, votes, off))
db.close()
