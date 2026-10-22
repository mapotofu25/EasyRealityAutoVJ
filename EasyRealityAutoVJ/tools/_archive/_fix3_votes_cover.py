# -*- coding: utf-8 -*-
"""第三轮 ★B 实测：真实素材上，票数门槛 120 vs 180 的「重同步覆盖率」与「误接受率」。

- 复用生产指纹库（复制到临时目录，绝不改动用户数据）；素材只读。
- 逐首取前 N 秒、hop=1s 的 4.5s 窗，跑 fp.match，得到 (sid, votes, off)。
- 覆盖率 = votes>=T 的窗占比（= 可用于相位校正/重同步的窗）。
- 误接受（bad accept，保守上界）：
    · 错歌：sid != 期望 sid（config 里的 path→song_id）
    · 位置错：命中的 off 与该窗真实位置（≈窗起点秒）相差 > 2 拍（按该窗 votes 命中的 bpm 估）
  统计 votes>=T 里的这两个比例（T=120 / 180）。
- 「连续 2 窗一致」会把孤立的坏窗再过滤一层，这里先给不设一致性时的上界。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_votes_cover.py
"""
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import numpy as np          # noqa: E402
import soundfile as sf      # noqa: E402
from fp import FingerprintDB  # noqa: E402

LA = os.environ.get("LOCALAPPDATA", "")
SRC_DB = os.path.join(LA, "AutoVJ", "fingerprints.db")
CFG = os.path.join(LA, "AutoVJ", "config.json")
TOFU = r"W:\临时结界相关\2of\tofu"
MAXSEC = int(os.environ.get("FIX3_MAXSEC", "150"))
HOP = 1.0
WIN = 4.5

tmp = tempfile.mkdtemp(prefix="fix3_fp_")
dst = os.path.join(tmp, "fp.db")
for suf in ("", "-wal", "-shm"):
    if os.path.exists(SRC_DB + suf):
        shutil.copyfile(SRC_DB + suf, dst + suf)
db = FingerprintDB(dst)
cfg = json.load(open(CFG, encoding="utf-8"))
base2sid = {}
for pp, vv in cfg.get("music_meta", {}).items():
    if isinstance(vv, dict) and vv.get("song_id") is not None:
        base2sid[os.path.basename(pp)] = vv["song_id"]

files = sorted(glob.glob(os.path.join(TOFU, "*.flac")))
print("素材 %d 首；前 %ds，hop=%.0fs，窗=%.1fs" % (len(files), MAXSEC, HOP, WIN))

# 每首的网格（beat_len / anchor），用于把「位置错」换算成拍、判断是否足以触发重锚
base2grid = {}
for pp, vv in cfg.get("music_meta", {}).items():
    if isinstance(vv, dict) and isinstance(vv.get("grid"), dict):
        base2grid[os.path.basename(pp)] = vv["grid"]

rows = []      # (votes, sid_ok, pos_ok, dbeat, song)
seqdump = []   # 供引擎端到端重放：每首一串 {t, sid, exp, votes, off, bl, anchor}
for si, song in enumerate(files):
    exp = base2sid.get(os.path.basename(song), -1)
    g = base2grid.get(os.path.basename(song)) or {}
    bl = float(g.get("beat_len") or 0.5)
    anchor = float(g.get("anchor") or 0.0)
    frames = int(MAXSEC * 44100)
    data, sr = sf.read(song, dtype="float32", always_2d=True, frames=frames)
    mono = np.mean(data, axis=1).astype(np.float32)
    if sr != 44100:
        x = np.linspace(0, len(mono) - 1, int(len(mono) * 44100 / sr))
        mono = np.interp(x, np.arange(len(mono)), mono).astype(np.float32)
        sr = 44100
    wl = int(WIN * sr)
    t = 0
    n = 0
    wins = []
    while t + wl <= len(mono):
        sid, votes, off = db.match(mono[t:t + wl], sr)
        pos_ok = None
        dbeat = 0.0
        if votes > 0 and sid >= 0:
            d = abs(off - (t / sr))
            pos_ok = d <= 1.2
            dbeat = d / bl          # 偏差换算成拍
        rows.append((votes, (sid == exp), pos_ok, dbeat, os.path.basename(song)))
        wins.append({"t": round(t / sr, 3), "sid": int(sid), "exp": int(exp),
                     "votes": int(votes), "off": (None if off is None else round(float(off), 3)),
                     "bl": bl, "anchor": anchor})
        n += 1
        t += int(HOP * sr)
    seqdump.append({"song": os.path.basename(song), "exp": int(exp), "wins": wins})
    print("  %-44s 窗=%3d" % (os.path.basename(song)[:44], n), flush=True)

db.close()
shutil.rmtree(tmp, ignore_errors=True)

v = np.array([r[0] for r in rows])
print()
print("=== 汇总：%d 首，%d 个 4.5s 窗 ===" % (len(files), len(v)))
json.dump([{"v": int(r[0]), "sok": bool(r[1]),
            "pok": (None if r[2] is None else bool(r[2])),
            "db": float(r[3]), "song": r[4]} for r in rows],
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_fix3_votes_rows.json"), "w"), ensure_ascii=False)
json.dump(seqdump, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "_fix3_votes_seq.json"), "w"), ensure_ascii=False)
print("（原始逐窗结果已写 tools/_fix3_votes_rows.json 与 _fix3_votes_seq.json，便于复核/重放）")
for T in (180, 150, 120):
    sel = v >= T
    cov = sel.mean()
    wrong_song = sum(1 for votes, sok, _ in [(r[0], r[1], r[2]) for r in rows]
                     if votes >= T and not sok)
    fa_song = wrong_song / max(1, int(sel.sum()))
    # 位置错（同歌，偏差 > 1.2s）
    pos_bad = sum(1 for r in rows if r[0] >= T and r[1] and r[2] is False)
    fa_pos = pos_bad / max(1, int(sel.sum()))
    # 足以单次触发硬重锚（|Δ拍| >= 3.5，engine seek_beats）的「强窗」数
    hard = sum(1 for r in rows if r[0] >= T and r[1] and r[3] >= 3.5)
    print("  T=%3d：覆盖率 %5.1f%%（窗数 %d）；错歌接受 %d（%.3f%%）；"
          "位置错>1.2s %d（%.3f%%）；其中偏差≥3.5拍可触发重锚 %d"
          % (T, 100 * cov, int(sel.sum()), wrong_song, 100 * fa_song,
             pos_bad, 100 * fa_pos, hard))
print("  votes 分位: p10=%d p25=%d p50=%d p75=%d p90=%d max=%d"
      % (np.percentile(v, 10), np.percentile(v, 25), np.percentile(v, 50),
         np.percentile(v, 75), np.percentile(v, 90), v.max()))
for T in (180, 120):
    below = (v < T).astype(int)
    best = cur = 0
    for b in below:
        cur = cur + 1 if b else 0
        best = max(best, cur)
    print("  最长连续 <%d 的时长 ≈ %d 秒（hop=1s）" % (T, best))
