# -*- coding: utf-8 -*-
"""隔离测：_pick 本身对不对？还是 match() 没把 hint 传下去？"""
import os, sys, shutil, tempfile, inspect
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import numpy as np, soundfile as sf
import fp as fpvj

# ① 纯 _pick（手造投票表）
votes = {(1, 604): 58, (1, 3028): 35, (1, 3063): 33, (1, 3027): 17}
print("_pick(votes, hint=3027.6, w=96) =", fpvj.FingerprintDB._pick(votes, 3027.6, 96.0))
print("_pick(votes, hint=None)          =", fpvj.FingerprintDB._pick(votes, None, None))

# ② 真数据：match() 带 hint
TMP = tempfile.mkdtemp(prefix="pickiso_")
db_tmp = os.path.join(TMP, "fp.db")
shutil.copyfile(os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db"), db_tmp)
db = fpvj.FingerprintDB(db_tmp)
P = "W:/CloudMusic/Sociable State/東方太嗨 -Touhou Toohigh Trance-/Sociable State - 1!2!3!☆J.A.C.R.P.D.mp3"
data, sr = sf.read(P, dtype="float32", always_2d=True)
mono = np.ascontiguousarray(np.mean(data, axis=1), dtype=np.float32)
AT = 140.6
seg = mono[int(AT*sr): int(AT*sr)+int(4.5*sr)]
want = AT * fpvj.SAMPLE_RATE / fpvj.HOP_SIZE
print("\nmatch(hint_delta=%r, hint_w=96)      =" % want,
      db.match(seg, sr, hint_delta=want, hint_w=96))
print("match(hint_delta=%r, hint_w=22)      =" % want,
      db.match(seg, sr, hint_delta=want, hint_w=22))
print("match(hint_delta=%r) [w=None→HINT_W] =" % want,
      db.match(seg, sr, hint_delta=want))
print("match(无 hint)                       =", db.match(seg, sr))
print("\nmatch 签名 :", inspect.signature(fpvj.FingerprintDB.match))
print("HINT_W =", fpvj.HINT_W, " HOP_SIZE =", fpvj.HOP_SIZE, " SAMPLE_RATE =", fpvj.SAMPLE_RATE)
db.close()
