# -*- coding: utf-8 -*-
"""QA 第二轮 ★B：真实素材统计 4.5s 窗的 fp.match votes —— 有多少比例 >= 180 门槛。
用生产指纹库（复制到临时目录，只读源文件，不动用户数据）。"""
import sys, os, glob, shutil, tempfile, hashlib, time
sys.path.insert(0, r"P:\AutoVJ\src")
import numpy as np
import soundfile as sf
from fp import FingerprintDB

SRC_DB = r"C:\Users\Administrator\AppData\Local\AutoVJ\fingerprints.db"
TOFU = r"W:\临时结界相关\2of\tofu"
tmp = tempfile.mkdtemp(prefix="qa2_fp_")
dst = os.path.join(tmp, "fp.db")
print("复制指纹库 ->", dst)
for suf in ("", "-wal", "-shm"):
    s = SRC_DB + suf
    if os.path.exists(s):
        shutil.copyfile(s, dst + suf)
db = FingerprintDB(dst)
try:
    n = db.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    ns = db.conn.execute("SELECT COUNT(DISTINCT song_id) FROM fingerprints").fetchone()[0]
    print("指纹库: %d 行, %d 首歌" % (n, ns))
except Exception as e:
    print("读库失败:", e)

files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))
print("素材 %d 首；全量前 200s，hop=1s" % len(files))

# 用生产 config 的 path->song_id 映射来判定命中是否正确（路径分隔符与扫描时一致）
import json
cfg = json.load(open(r"C:\Users\Administrator\AppData\Local\AutoVJ\config.json", encoding="utf-8"))
base2sid = {}
for pp, vv in cfg.get("music_meta", {}).items():
    if isinstance(vv, dict) and vv.get("song_id") is not None:
        base2sid[os.path.basename(pp)] = vv["song_id"]

WIN = 4.5
HOP = 1.0
allv = []
per_song = []
t0 = time.time()
for song in files:
    sid_expected = base2sid.get(os.path.basename(song), -1)
    data, sr = sf.read(song, dtype="float32", always_2d=True, frames=int(200 * 44100))
    mono = np.mean(data, axis=1).astype(np.float32)
    if sr != 44100:
        x = np.linspace(0, len(mono) - 1, int(len(mono) * 44100 / sr))
        mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32); sr = 44100
    wl = int(WIN * sr)
    votes_list = []
    ok_cnt = 0; tot = 0
    t = 0
    while t + wl <= len(mono):
        seg = mono[t:t + wl]
        sid, votes, off = db.match(seg, sr)
        votes_list.append(votes)
        allv.append(votes)
        tot += 1
        if sid == sid_expected:
            ok_cnt += 1
        t += int(HOP * sr)
    v = np.array(votes_list)
    ge = int((v >= 180).sum())
    name, tt, gg, pc, mn, md, mx, ok = (os.path.basename(song)[:42], tot, ge,
                                       100.0 * ge / max(1, tot), int(v.min()),
                                       int(np.median(v)), int(v.max()), ok_cnt)
    per_song.append((name, tt, gg, pc, mn, md, mx, ok))
    print("  %-42s 窗=%3d  >=180: %3d (%5.1f%%)  min=%4d 中位=%4d max=%4d  命中本歌=%d/%d  [%.1fs]"
          % (name, tt, gg, pc, mn, md, mx, ok, tt, time.time() - t0))

a = np.array(allv)
print()
print("=== 汇总（%d 首，%d 个 4.5s 窗）===" % (len(files), len(a)))
print("votes>=180 比例 = %.1f%%" % (100.0 * (a >= 180).mean()))
print("votes 分位: p10=%d p25=%d p50=%d p75=%d p90=%d max=%d" % (
    np.percentile(a, 10), np.percentile(a, 25), np.percentile(a, 50),
    np.percentile(a, 75), np.percentile(a, 90), a.max()))
print("votes==0（完全没匹配）比例 = %.1f%%" % (100.0 * (a == 0).mean()))
# 最长连续低票数（<180）时长（按 1s hop）
below = (a < 180).astype(int)
best = cur = 0
for b in below:
    cur = cur + 1 if b else 0
    best = max(best, cur)
print("最长连续 <180 的时长 ≈ %d 秒（hop=1s）" % best)
db.close()
shutil.rmtree(tmp, ignore_errors=True)
