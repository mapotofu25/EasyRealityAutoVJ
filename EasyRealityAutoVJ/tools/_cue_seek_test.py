# -*- coding: utf-8 -*-
"""真拖动（seek）决策单测 —— 用户 2026-10-18：

> "我音频播到五分钟，然后我手动突然跳到三分钟，视频半天都不跳的"

要同时满足两件相反的事：
  · **真拖动**（之后每次结果都指向同一处）⇒ 必须**跳过去**（2.5 秒内确认）
  · **野匹配**（每次结果跳到不同地方）⇒ **永远不跳**
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cue_position as CP                                       # noqa: E402

BL = 0.4225
FAIL = []


def ck(name, cond, detail=""):
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def main():
    print("真拖动 vs 野匹配（判据 = 新位置能否持续站住）")

    # ---- 1) 真拖动：300s 处 → 突然跳到 180s ----
    trk = CP.CuePositionTracker()
    trk.rate = 1.0
    trk.feed(7, 300.0, 60, 0.0, beat_len=BL)
    t = 0.0
    for _ in range(20):                      # 正常播到 304s
        t = round(t + 0.2, 6)
        trk.feed(7, 300.0 + t, 60, t, beat_len=BL)
    # 手动拖到 180s（之后一直按 180+t 走）
    jump_t = t
    after = []
    for _ in range(40):                      # 观察 8 秒
        t = round(t + 0.2, 6)
        st, pb, _ = trk.feed(7, 180.0 + (t - jump_t), 60, t, beat_len=BL)
        after.append((t - jump_t, None if pb is None else pb * BL))
    hit = next((dt for dt, p in after if p is not None and abs(p - (180.0 + dt)) <= 1.0), None)
    ck("真拖动 120 秒：在 %.1f 秒内跳过去（应 ≤%.1f）" % (hit if hit is not None else -1,
                                                          CP.CUE_SEEK_DEBOUNCE + 1.0),
       hit is not None and hit <= CP.CUE_SEEK_DEBOUNCE + 1.0, "hit=%r" % (hit,))

    # ---- 2) 野匹配：每次指到不同地方（100 / 250 交替）----
    trk = CP.CuePositionTracker()
    trk.rate = 1.0
    trk.feed(7, 300.0, 60, 0.0, beat_len=BL)
    t = 0.0
    for _ in range(20):
        t = round(t + 0.2, 6)
        trk.feed(7, 300.0 + t, 60, t, beat_len=BL)
    base_err = None
    for k in range(60):                      # 12 秒，交替野值
        t = round(t + 0.2, 6)
        off = 100.0 + t if k % 2 == 0 else 250.0 + t
        st, pb, _ = trk.feed(7, off, 60, t, beat_len=BL)
        if pb is not None:
            base_err = pb * BL - (300.0 + t)
    ck("野匹配交替 12 秒：位置**没有被带走**（偏差 <5 秒）",
       base_err is not None and abs(base_err) < 5.0, "偏差 %.2fs" % (base_err or 999))

    # ---- 3) 野匹配"粘住"（一直指同一处 1 秒）后消失，也不该跳 ----
    trk = CP.CuePositionTracker()
    trk.rate = 1.0
    trk.feed(7, 300.0, 60, 0.0, beat_len=BL)
    t = 0.0
    for _ in range(20):
        t = round(t + 0.2, 6)
        trk.feed(7, 300.0 + t, 60, t, beat_len=BL)
    for k in range(5):                       # 1 秒的野值（< 2.5s 判定窗）
        t = round(t + 0.2, 6)
        trk.feed(7, 100.0 + t, 60, t, beat_len=BL)
    for _ in range(10):                      # 回到正确
        t = round(t + 0.2, 6)
        st, pb, _ = trk.feed(7, 300.0 + t, 60, t, beat_len=BL)
    ck("野匹配只粘 1 秒（<%.1fs）⇒ 不跳" % CP.CUE_SEEK_DEBOUNCE,
       pb is not None and abs(pb * BL - (300.0 + t)) < 3.0,
       "偏差 %.2fs" % ((pb * BL - 300.0 - t) if pb else 999))

    print()
    if FAIL:
        print("结论：FAIL —— " + "、".join(FAIL))
        return 1
    print("结论：PASS —— 真拖动 2.5 秒内跟上，野匹配（无论多远/多久）都不跳。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
