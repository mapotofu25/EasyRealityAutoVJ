# -*- coding: utf-8 -*-
"""把 match() 内部的投票表 dump 出来：为什么带正确 hint 仍然选到 28.05s？"""
import os, sys, shutil, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import numpy as np, soundfile as sf
import fp as fpvj

TMP = tempfile.mkdtemp(prefix="vdump_")
db_tmp = os.path.join(TMP, "fp.db")
shutil.copyfile(os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db"), db_tmp)
db = fpvj.FingerprintDB(db_tmp)

P = "W:/CloudMusic/Sociable State/東方太嗨 -Touhou Toohigh Trance-/Sociable State - 1!2!3!☆J.A.C.R.P.D.mp3"
SID = int(__import__("hashlib").md5(P.encode("utf-8")).hexdigest()[:8], 16)
data, sr = sf.read(P, dtype="float32", always_2d=True)
mono = np.ascontiguousarray(np.mean(data, axis=1), dtype=np.float32)
AT = 140.6
seg = mono[int(AT*sr): int(AT*sr)+int(4.5*sr)]

hs = fpvj.fingerprint_audio(seg, sr)
qoff_map = {}
hashes = []
for h, qoff in hs:
    qoff_map.setdefault(h, []).append(qoff)
    hashes.append(h)
votes = {}
for i in range(0, len(hashes), 500):
    chunk = hashes[i:i+500]
    ph = ",".join("?" * len(chunk))
    for (h, song_id, dboff) in db.conn.execute(
            "SELECT hash, song_id, offset FROM fingerprints WHERE hash IN (%s)" % ph, chunk):
        for qoff in qoff_map.get(h, ()):
            key = (song_id, dboff - qoff)
            votes[key] = votes.get(key, 0) + 1

mine = {k: v for k, v in votes.items() if k[0] == SID}
print("查询窗 @%.1fs（%d 个哈希）→ 本歌候选组合 %d 个，总票 %d" % (AT, len(hashes), len(mine), sum(mine.values())))
top = sorted(mine.items(), key=lambda kv: -kv[1])[:8]
print("票数最高的 8 个 delta：")
for (sid, d), c in top:
    print("   delta=%7d  (%8.3f s)  votes=%d" % (d, d * fpvj.HOP_SIZE / fpvj.SAMPLE_RATE, c))
want = AT * fpvj.SAMPLE_RATE / fpvj.HOP_SIZE
print("\n真值 delta = %.1f（%.1fs）" % (want, AT))
for w in (22, 96, 500, 2000):
    lo, hi = want - w, want + w
    inside = [(d, c) for (s, d), c in mine.items() if lo <= d <= hi]
    print("  窗 ±%-5d 帧内候选 %-4d 个，最高票 %s" % (
        w, len(inside), max((c for _d, c in inside), default=0)))
    if inside:
        bd, bc = max(inside, key=lambda x: x[1])
        print("        → 若按此窗选：delta=%d (%.3fs) votes=%d" % (
            bd, bd*fpvj.HOP_SIZE/fpvj.SAMPLE_RATE, bc))
db.close()
