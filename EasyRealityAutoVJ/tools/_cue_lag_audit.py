# -*- coding: utf-8 -*-
"""从**用户真机日志**里算：指纹 offset 的真实推进速率、以及视频相对音频的滞后。

`off` 是识别窗口起点在**原曲时间轴**上的秒数；它的斜率 = 音乐真实播放倍率。
若斜率 ≠ 1.0 而音乐明明是 1.0× 播的 ⇒ 指纹库里的版本与正在放的版本节奏不同
⇒ 码表会按错误速率推进 ⇒ 用户看到"视频慢/跟不上"。
"""
from __future__ import annotations

import io
import os
import re
from collections import defaultdict

LOG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "cue_health.log")
ROW = re.compile(r"\[(\d\d):(\d\d):(\d\d)\].*?sid=(-?\d+) votes=(\d+) off=(-?[\d.]+) "
                 r"status=(\w+) pb=(\S+).*?dmax=(\S+) spd=(\S+).*?r=([\d.]+)"
                 r"(?:.*?dark=([\d.]+)/(\d+))?")


def load():
    rows = []
    for ln in io.open(LOG, encoding="utf-8", errors="replace"):
        m = ROW.search(ln)
        if not m:
            continue
        hh, mm, ss, sid, v, off, st, pb, dm, spd, r, dk, dn = m.groups()
        rows.append(dict(t=int(hh) * 3600 + int(mm) * 60 + int(ss), sid=int(sid),
                         votes=int(v), off=float(off), st=st,
                         pb=(None if pb == "None" else float(pb)),
                         dmax=float(dm), r=float(r)))
    return rows


def main():
    rows = load()
    print("取证行 %d 条" % len(rows))
    by = defaultdict(list)
    for x in rows:
        by[x["sid"]].append(x)

    for sid, rs in by.items():
        if sid < 0:
            continue
        # 切连续段：相邻两条 off 的增量若不合常理（野匹配/断档）就断开
        segs, cur = [], []
        for a, b in zip(rs, rs[1:]):
            dt = b["t"] - a["t"]
            do = b["off"] - a["off"]
            ok = (0 < dt <= 4) and (0.0 <= do / dt <= 2.0)
            if ok:
                cur.append((a["t"], a["off"]))
            else:
                if len(cur) > 5:
                    segs.append(cur)
                cur = [(b["t"], b["off"])]
        if len(cur) > 5:
            segs.append(cur)

        r_end = rs[-1]["r"]
        print("\n=== sid=%d  %d 行  %d 段  r 末值=%.3f ===" % (sid, len(rs), len(segs), r_end))
        for s in segs:
            t0, o0 = s[0]
            t1, o1 = s[-1]
            span = t1 - t0
            if span <= 0:
                continue
            slope = (o1 - o0) / span
            print("  %02d:%02d→%02d:%02d  %5.1fs  off %7.2f→%7.2f  斜率 %.4f"
                  % (t0 // 3600, (t0 % 3600) // 60, t1 // 3600, (t1 % 3600) // 60,
                     span, o0, o1, slope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
