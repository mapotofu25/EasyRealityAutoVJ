# -*- coding: utf-8 -*-
"""分析 layer_health.log：找出"真卡住"的形态。

两种形态分开统计（按层、按帧计数是否推进）：
  A. 真·停帧：同一个播放器、cur 不变，但 frames 连续多秒不涨（age 在涨）—— 用户看到的"放一会儿就卡"
  B. 无帧：frames 恒为 0（重建后新播放器起不来）
输出：每个事件的起止时间、持续秒数、播放器 id、cur、期间 gpu= 取值。
"""
import io
import os
import re
import sys

P = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "layer_health.log")
lines = io.open(P, encoding="utf-8", errors="replace").read().splitlines()
print("日志:", P, "共", len(lines), "行")

SEG = re.compile(r"(L\d+)\s+(.+?)\s+vis=(\d)\s+cur=(-?\d+)/(\d+)\s+p=(\S+)(.*?)frames=(\d+)")
AGE = re.compile(r"age=([\d.]+|-)")


def parse(ln):
    ts = ln[1:9]
    gpu = "?"
    m = re.search(r"gpu=(\S+)", ln)
    if m:
        gpu = m.group(1)
    out = []
    for seg in ln.split("|")[1:]:
        m = SEG.search(seg)
        if not m:
            continue
        ag = AGE.search(seg)
        out.append({
            "lay": m.group(1), "name": m.group(2).strip(), "vis": m.group(3),
            "cur": int(m.group(4)), "n": int(m.group(5)), "p": m.group(6),
            "frames": int(m.group(8)), "age": ag.group(1) if ag else "-",
            "gpu": gpu, "ts": ts,
        })
    return out


samples = []
for ln in lines:
    samples.extend(parse(ln))

by_layer = {}
for s in samples:
    by_layer.setdefault(s["lay"], []).append(s)

print("层:", {k: len(v) for k, v in by_layer.items()})
print()

for lay, rows in sorted(by_layer.items()):
    rows.sort(key=lambda r: r["ts"])
    # 事件检测
    events = []
    cur_ev = None
    for r in rows:
        if r["p"] in ("img", "none") or r["cur"] < 0:
            if cur_ev:
                events.append(cur_ev)
                cur_ev = None
            continue
        if cur_ev is None:
            cur_ev = {"start": r["ts"], "end": r["ts"], "secs": 1, "p": r["p"],
                      "cur": r["cur"], "kind": "?", "frames0": r["frames"] == 0,
                      "last_f": r["frames"], "gpu": r["gpu"]}
        else:
            same_cur = (r["cur"] == cur_ev["cur"])
            same_p = (r["p"] == cur_ev["p"])
            stall = same_cur and same_p and (r["frames"] <= cur_ev["last_f"])
            if stall:
                cur_ev["secs"] += 1
                cur_ev["end"] = r["ts"]
                cur_ev["kind"] = "B-无帧" if r["frames"] == 0 else "A-停帧"
            else:
                events.append(cur_ev)
                cur_ev = {"start": r["ts"], "end": r["ts"], "secs": 1, "p": r["p"],
                          "cur": r["cur"], "kind": "?", "last_f": r["frames"],
                          "gpu": r["gpu"]}
            cur_ev["last_f"] = min(cur_ev["last_f"], r["frames"]) if stall else r["frames"]
            if stall:
                cur_ev["last_f"] = r["frames"]
    if cur_ev:
        events.append(cur_ev)
    long_ev = [e for e in events if e["secs"] >= 2 and e["kind"] != "?"]
    print("=== %s：停帧事件（≥2 秒）共 %d 个 ===" % (lay, len(long_ev)))
    for e in sorted(long_ev, key=lambda x: -x["secs"])[:12]:
        print("   %s → %s  连续 %2d 秒  %s  cur=%d  p=%s  gpu=%s"
              % (e["start"], e["end"], e["secs"], e["kind"], e["cur"], e["p"], e["gpu"]))
    if not long_ev:
        print("   （无 ≥2 秒的停帧）")
    print()
