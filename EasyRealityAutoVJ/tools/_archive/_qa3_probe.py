# -*- coding: utf-8 -*-
"""QA 第三轮：直方图级探针 —— hint 误差分布 / 真候选是否落在 ±HINT_W 内 /
同歌错候选（自证偏差的必要条件）分布。

只读 tools/_fix3_hint_seq.json（工程师采集的真实逐窗直方图），不碰用户数据。
"""
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP  # noqa: E402

FPS = FP.SAMPLE_RATE / FP.HOP_SIZE   # 帧/秒
HOP = FP.HOP_SIZE / FP.SAMPLE_RATE

seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"),
                     encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]
print("可核对曲目 %d 首；HINT_W=%d 帧 = %.3f s" % (len(seq), FP.HINT_W, FP.HINT_W * HOP))


def true_delta(w):
    """真候选 delta（帧）：窗口起点原曲秒数 × FPS。允许 ±3 帧量化。"""
    return round(w["t"] * FPS)


def analyze():
    nwin = 0
    same_song_decoy = 0          # 同歌错候选（自证偏差的必要条件）
    top_is_decoy = 0
    decoy_dist_frame = []
    true_votes = []
    decoy_votes = []
    # 这里用「真候选」判据：同歌 + |delta-dt|<=3
    for d in seq:
        exp = d["exp"]
        for w in d["wins"]:
            dt = true_delta(w)
            cands = {(c[0], c[1]): c[2] for c in w["cand"]}
            if not cands:
                continue
            nwin += 1
            tv = [v for (s, dd), v in cands.items() if s == exp and abs(dd - dt) <= 3]
            tv = max(tv) if tv else 0
            true_votes.append(tv)
            # 同歌非真候选
            ss = [(s, dd, v) for (s, dd), v in cands.items()
                  if s == exp and abs(dd - dt) > 3]
            if ss:
                same_song_decoy += 1
                b = max(ss, key=lambda x: x[2])
                decoy_dist_frame.append(abs(b[1] - dt))
                decoy_votes.append(b[2])
            gmax = max(cands.values())
            gk = [k for k, v in cands.items() if v == gmax][0]
            is_true = (gk[0] == exp and abs(gk[1] - dt) <= 3)
            if not is_true:
                top_is_decoy += 1
    print("总窗 = %d" % nwin)
    print("真候选票中位 = %.0f" % statistics.median(true_votes))
    print("同歌错候选存在的窗 = %d (%.1f%%)" % (same_song_decoy, 100 * same_song_decoy / nwin))
    print("全局众数 != 真候选的窗 = %d (%.1f%%)" % (top_is_decoy, 100 * top_is_decoy / nwin))
    if decoy_dist_frame:
        ds = sorted(decoy_dist_frame)
        print("同歌错候选距真值(帧): min=%.0f p1=%.0f 中位=%.0f" %
              (ds[0], ds[max(0, len(ds) // 100)], statistics.median(ds)))
        print("   同歌错候选落在 ±HINT_W(%d) 内的比例 = %.2f%%" %
              (FP.HINT_W, 100 * sum(1 for x in ds if x <= FP.HINT_W) / len(ds)))
        print("   同歌错候选票中位 = %.0f  max = %.0f" %
              (statistics.median(decoy_votes), max(decoy_votes)))


analyze()
