# -*- coding: utf-8 -*-
"""量化对比：每首歌在"不给 hint"时，位置(offset)有多稳。

电荷条要求每拍 offset 递增 0.5s±0.4 才允许充电。重复段落多的歌，
全局众数会在几处之间来回跳 ⇒ 永远不满足 ⇒ 认不出。
"""
import os, sys, shutil, tempfile
sys.path.insert(0, "src")
import numpy as np, soundfile as sf
import fp as fpvj

DB = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
tmp = os.path.join(tempfile.mkdtemp(), "fp.db"); shutil.copyfile(DB, tmp)
db = fpvj.FingerprintDB(tmp)

FILES = [
    ("揽月 - Dreamira",     r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac"),
    ("Nimbus Spark 1999",  r"Y:/EasyRealityAutoVJ/码表素材/Nimbus Spark 1999 - Supa7onyz.flac"),
    ("Cosmic String(曲库)", r"W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac"),
    ("Black Warrior(曲库)", r"W:/临时结界相关/2of/tofu/Rolling Contact - Black Warrior (2025 Remaster).flac"),
]
WIN, STEP = 4.5, 0.5
print("%-22s %6s %8s %10s" % ("曲目", "窗数", "推进率", "位置跳变次数"))
for name, path in FILES:
    if not os.path.isfile(path):
        print("%-22s （文件不存在）" % name); continue
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
    offs = []
    start = int(len(mono) * 0.35)          # 从中段开始，避开前奏
    for i in range(12):
        a = start + int(i * STEP * sr)
        seg = mono[a:a + int(WIN * sr)]
        if len(seg) < int(WIN * sr): break
        sid, votes, off = db.match(seg, sr)
        if votes >= 4:
            offs.append(off)
    good = sum(1 for p, q in zip(offs, offs[1:]) if abs((q - p) - STEP) <= 0.4)
    print("%-22s %6d %7.0f%% %10d" % (name, len(offs), 100.0 * good / max(1, len(offs) - 1),
                                      len(offs) - 1 - good))
db.close()
