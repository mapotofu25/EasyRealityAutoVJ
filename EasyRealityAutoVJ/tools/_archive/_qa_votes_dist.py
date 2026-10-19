# -*- coding: utf-8 -*-
"""M2（票数门槛）依据实测：真实歌曲上 aligned_votes 的分布。

- 只读 W:\\临时结界相关\\2of\\tofu\\*.flac；指纹库建在**临时目录**，绝不碰 %LOCALAPPDATA%\\AutoVJ。
- 目的：给「votes 低于阈值就不信 offset」的门槛取一个有依据的数：
    · 正确命中（同歌 + offset 正确）的票数分布 → 下限；
    · 命中错歌 / 错位置（重复段落歧义）的票数 → 上限。
  门槛要落在「坏样本最大值」与「好样本最小值」之间，并留足余量。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_qa_votes_dist.py
"""
import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np            # noqa: E402
import soundfile as sf        # noqa: E402
import beatgrid as bg         # noqa: E402
from fp import FingerprintDB  # noqa: E402

DIR = r"W:/临时结界相关/2of/tofu"
SR = 44100
MAX_SONGS = int(os.environ.get("VOTES_MAX_SONGS", "10"))
STEP = 20.0        # 每隔 20s 取一个 4.5s 查询窗


def main():
    flacs = sorted(f for f in os.listdir(DIR) if f.lower().endswith(".flac"))[:MAX_SONGS]
    tmp = tempfile.mkdtemp(prefix="qa_votes_")
    dbp = os.path.join(tmp, "fp.db")
    print("临时指纹库：", dbp, " 歌曲数：", len(flacs))
    db = FingerprintDB(dbp)
    sids = {}
    for f in flacs:
        p = os.path.join(DIR, f)
        data, sr = sf.read(p, dtype="float32", always_2d=True)
        mono = np.mean(data, axis=1)
        sig = bg.resample(mono, sr, SR) if sr != SR else mono.astype(np.float32)
        sid = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
        sids[f] = (p, sig, sid)
        db.add_song(sid, sig, SR)
        print("  入库 %-46s sid=%d dur=%.0fs" % (f[:46], sid, len(sig) / SR))
    db.commit_batch()

    good = []      # (votes, off_err) 正确命中
    bad_song = []  # 命中错歌
    bad_pos = []   # 命中同歌但位置错 >0.3s（重复段落歧义）
    n_win = 0
    print("\n开始扫描查询窗（步长 %.0fs）..." % STEP)
    for f in flacs:
        p, sig, sid = sids[f]
        P = 5.0
        dur = len(sig) / SR
        while P + 4.5 <= dur - 1.0:
            i0 = int(P * SR)
            q = sig[i0:i0 + int(4.5 * SR)]
            msid, votes, off = db.match(q, SR)
            n_win += 1
            if msid != sid:
                bad_song.append((f, P, msid, votes, off))
            elif abs(off - P) > 0.3:
                bad_pos.append((f, P, votes, off))
            else:
                good.append((votes, off - P))
            P += STEP

    db.close()
    try:
        os.remove(dbp)
        os.rmdir(tmp)
    except Exception:
        pass

    gv = sorted(v for v, _ in good)
    def pct(a, q):
        if not a:
            return None
        i = max(0, min(len(a) - 1, int(round(q * (len(a) - 1)))))
        return a[i]

    print("\n" + "=" * 78)
    print("查询窗总数 = %d" % n_win)
    print("-" * 78)
    print("【正确命中】n=%d" % len(good))
    if gv:
        print("  票数  min=%d  p5=%d  p25=%d  中位=%d  p75=%d  max=%d"
              % (gv[0], pct(gv, 0.05), pct(gv, 0.25), pct(gv, 0.5),
                 pct(gv, 0.75), gv[-1]))
    print("【命中错歌】n=%d  → 票数：%s"
          % (len(bad_song), sorted(set(v for *_, v, _ in bad_song))[:20] or "无"))
    all_bad_votes = sorted([v for *_, v, _ in bad_song] + [v for _, _, v, _ in bad_pos])
    print("【命中同歌但位置错 >0.3s（重复段落歧义）】n=%d  → 票数（升序）：%s"
          % (len(bad_pos), all_bad_votes or "无"))
    print("-" * 78)
    worst_bad = max(all_bad_votes + [0])
    if gv:
        print("坏样本票数最大值 = %d ；好样本票数最小值 = %d" % (worst_bad, gv[0]))
    # 门槛扫描：接受 votes>=T 时的 TP（正确命中接受数）与 FP（歧义/错歌误接受数）
    print("\n门槛扫描（接受 votes>=T）：")
    print("  %-6s %-8s %-8s %-8s" % ("T", "TP接受", "FP误接受", "剩余TP%"))
    for T in (40, 60, 80, 100, 120, 150, 180, 200, 250):
        tp = sum(1 for v, _ in good if v >= T)
        fp = sum(1 for v in all_bad_votes if v >= T)
        print("  %-6d %-8d %-8d %-8.0f%%" % (T, tp, fp, 100.0 * tp / max(1, len(good))))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
