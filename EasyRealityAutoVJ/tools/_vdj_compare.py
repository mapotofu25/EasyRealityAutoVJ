# -*- coding: utf-8 -*-
"""交叉验证：VirtualDJ 的网格（BPM + 锚点） vs 我们自己的算法。

用途：判断我们的 BPM / 拍位相位是否与一个成熟的商业 DJ 软件一致，
并把「8 拍乐句」问题缩小到「在 VDJ 的小节头里二选一」。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import beat_analyze as ba          # noqa: E402
import vdj_grid as vg              # noqa: E402

DIR = "W:/临时结界相关/2of/tofu"
# (文件名, 用户标注BPM, 标注区间, VDJ 库里同曲副本的关键字)
CASES = [
    ("Rolling Contact - Outburst Oxygen (Sharp Remaster).flac", 128,
     [(0, 30, 64), (60, 91, 64)], "ELECTRO CUTE 7"),
    ("Rolling Contact - The Lost (135 Extended).flac", 135,
     [(58, 85, 64)], r"TRANSFINITE\Rolling"),
    ("Rolling Contact - Crescent Horizon (Rebuild).flac", 145,
     [(80, 106, 64)], "TRANSFINITE 2"),
    ("Rolling Contact - Dust.flac", 128,
     [(15, 29, 32), (75, 105, 64)], "ELECTRO CUTE 8"),
    ("Rolling Contact - Endless & Limitless.flac", 155,
     [(51, 75, 64)], "Look Back"),
]


def main():
    db = vg.load_db()
    print("VDJ 库: %d 首" % len(db))
    rows = []
    for fn, ub, segs, kw in CASES:
        path = os.path.join(DIR, fn)
        # --- VDJ 侧 ---
        cands = [e for e in vg.find(db, kw)
                 if os.path.basename(e["path"]).lower() == fn.lower()]
        if not cands:
            cands = vg.find(db, kw)
        v = cands[0] if cands else None
        # --- 我们这边 ---
        maxsec = max(t1 for _, t1, _ in segs) + 12.0
        r = ba.segment_report(path, seg=segs, user_bpm=ub, maxsec=maxsec,
                              verbose=False)
        seg0 = r["segs"][0]
        my_bpm = seg0["bpm_sel"]
        my_ph = seg0["phase"]
        per = 60.0 / my_bpm
        # 相位差（取最短方向）
        diff = 0.0
        if v and v["anchor"] is not None:
            d = (v["anchor"] - my_ph) % per
            if d > per / 2:
                d -= per
            diff = d
        rows.append((fn, ub, v, my_bpm, my_ph, per, diff, r["bpm_peaks"][:3], v))

    print()
    print("%-40s %6s %8s %8s %9s %8s" % ("曲目", "用户标", "VDJ BPM", "算法BPM", "相位差(拍)", "相位差(ms)"))
    print("-" * 92)
    for fn, ub, v, my_bpm, my_ph, per, diff, tops, vv in rows:
        short = fn.replace("Rolling Contact - ", "")[:38]
        vb = ("%.2f" % v["bpm"]) if v else "—"
        dd = ("%+.3f" % (diff / per)) if v else "—"
        dm = ("%+.0f" % (diff * 1000)) if v else "—"
        print("%-40s %6d %8s %8.2f %9s %8s" % (short, ub, vb, my_bpm, dd, dm))

    print()
    print("算法 BPM 候选 Top3（看 VDJ 值排第几）:")
    for fn, ub, v, my_bpm, my_ph, per, diff, tops, vv in rows:
        short = fn.replace("Rolling Contact - ", "")[:34]
        s = "  ".join("%.2f(%.2f)" % (b, sc) for b, sc in tops)
        rank = "—"
        if v:
            for i, (b, sc) in enumerate(tops):
                if abs(b - v["bpm"]) < 0.6:
                    rank = "第%d" % (i + 1)
                    break
        print("  %-36s VDJ=%.2f  %s   → VDJ 值排名 %s"
              % (short, v["bpm"] if v else 0, s, rank))

    print()
    print("VDJ 自动段落标记（Break = 拆句段，可用于交叉参考）:")
    for fn, ub, v, *_ in rows:
        short = fn.replace("Rolling Contact - ", "")[:34]
        if v and v["remix"]:
            print("  %-36s %s" % (short,
                  ", ".join("%s@%.0f" % (n, p) for n, p in sorted(v["remix"], key=lambda x: x[1]))))
        else:
            print("  %-36s （无）" % short)

    print()
    print("VDJ 锚点处对应的「小节头」序列（每 4 拍），与用户标注区间对照:")
    for fn, ub, segs, kw in CASES:
        v = rows[[r[0] for r in rows].index(fn)][2]
        if not v or v["anchor"] is None:
            continue
        short = fn.replace("Rolling Contact - ", "")[:24]
        for (t0, t1, n) in segs:
            bars = vg.grid_times(v, t0, t1)
            downs = [t for t in bars if ((round((t - v["anchor"]) / v["beat_len"])) % 4) == 0]
            first = [round(x, 3) for x in downs[:4]]
            print("  %-26s 区间 %6.1f~%-6.1f 小节头: %s" % (short, t0, t1, first))
    return 0


if __name__ == "__main__":
    sys.exit(main())
