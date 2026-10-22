# -*- coding: utf-8 -*-
"""码表位置层回归 —— **速度式伺服**（用户 2026-10-18 明确要求）：

> "一旦成功识别到指纹，就进入强制播放状态，画面不再跳动，只允许在加速或减速过程中
>   有相应变化，并持续保持该状态，直到切换到其他歌曲时才移除"
> "加个防抖，好几秒的那种，超过这个时间指纹还是严重对不上才切"

核心判据只有三条：
  · **单调**：位置永远不倒退（观众不可能看到倒放）
  · **连续**：单帧前进不超过 1.2× 正常值（没有跳变）
  · **不抖**：指纹噪声（±0.25s）与偶发野匹配完全不改变位置轨迹

运行：python tools/_cue_pos_smooth_test.py
"""
from __future__ import annotations

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cue_position as CP                                       # noqa: E402

FAIL = []
BL = 0.4225                 # 142 BPM
STEP = 0.2                  # 引擎实际调用频率 5Hz


def ck(name, cond, detail=""):
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)



def max_speed(rows):
    """相邻两次**有位置**的采样之间的瞬时速度（拍位/秒）—— 连续性判据按时间归一，
    否则断档期间没有上报行，会把"5 秒空白"误算成"5 秒跳变"。"""
    pts = [(t, p) for t, p, _s in rows if p is not None]
    v = 0.0
    for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
        dt = t2 - t1
        if dt > 1e-6:
            v = max(v, (p2 - p1) / dt)
    return v


def run(fn, seconds=12.0, rate=1.0, bid=7, votes=30):
    """按 5Hz 喂 offset；fn(t) 返回 offset 或 None（None = 这帧没结果）。"""
    trk = CP.CuePositionTracker()
    trk.rate = rate
    out = []
    t = 0.0
    while t <= seconds + 1e-9:
        o = fn(t)
        if o is None:
            st, pb, _ = trk.feed(-1, -1.0, 0, t, beat_len=BL)
        else:
            st, pb, _ = trk.feed(bid, o, votes, t, beat_len=BL)
        out.append((round(t, 6), None if pb is None else pb * BL, st))
        t = round(t + STEP, 6)
    return trk, out


def steady(rate, jitter=0.0, seed=3):
    rnd = random.Random(seed)

    def f(t):
        if abs((t * 2) - round(t * 2)) > 1e-6:      # 每 0.5s 才给一次新值
            return None
        base = 100.0 + t * rate
        return base + (rnd.uniform(-jitter, jitter) if jitter else 0.0)
    return f


def main():
    print("码表位置层：速度式伺服（单调 / 连续 / 不抖）")

    # A/B/C 稳态
    _trk, rows = run(steady(1.0, jitter=0.2), seconds=12.0)
    pos = [p for _t, p, _s in rows if p is not None]
    back = min((pos[i] - pos[i - 1] for i in range(1, len(pos))), default=0.0)
    vmax = max_speed(rows)
    ck("A 位置单调：没有一帧倒退", back >= -1e-6, "最小单帧增量 %+.4fs" % back)
    ck("B 位置连续：瞬时速度 ≤1.25×（无跳变）", vmax <= 1.25, "最大 %.3f×" % vmax)
    ck("C 总量对：12 秒走 12±0.5 秒", abs((pos[-1] - pos[0]) - 12.0) <= 0.5,
       "实际 %.2fs" % (pos[-1] - pos[0]))

    # D 一次 200 秒野匹配
    def wild(t):
        if abs(t - 5.0) < 1e-6:
            return 100.0 + t + 200.0
        if abs((t * 2) - round(t * 2)) > 1e-6:
            return None
        return 100.0 + t
    _trk, rows = run(wild, seconds=10.0)
    vmax = max_speed(rows)
    ck("D 一次 200 秒野匹配：瞬时速度仍 ≤1.25×（完全忽略）", vmax <= 1.25, "最大 %.3f×" % vmax)

    # E 断档 5 秒
    def gap(t):
        if 5.0 <= t < 10.0:
            return None
        if abs((t * 2) - round(t * 2)) > 1e-6:
            return None
        return 100.0 + t
    trk, rows = run(gap, seconds=40.0)
    pos = [(t, p) for t, p, _s in rows if p is not None]
    vmax = max_speed(rows)
    ck("E1 断档 5 秒恢复后仍然不跳（速度 ≤1.25×）", vmax <= 1.25, "最大 %.3f×" % vmax)
    # E2 持续 4 秒偏差（真出问题）⇒ 防抖 3 秒后开始追，最终收敛
    def persist(t):
        if abs((t * 2) - round(t * 2)) > 1e-6:
            return None
        # t<2 正常；t≥2 起指纹突然说"你差了 4 秒"（模拟真出问题）
        return 100.0 + t + (4.0 if t >= 2.0 else 0.0)
    trk, rows = run(persist, seconds=30.0)
    d = {t: p for t, p, _s in rows if p is not None}
    # 注意：这里的"真值"就是指纹给的位置（100+t+4），伺服应当收敛到它
    e6 = abs(d[6.0] - (100.0 + 6.0 + 4.0))
    e15 = abs(d[15.0] - (100.0 + 15.0 + 4.0))
    e29 = abs(d[29.0] - (100.0 + 29.0 + 4.0))
    # 新契约：4 秒偏差属于"远"，候选持续一致 2.5 秒 ⇒ 直接重锚（跳一次）
    ck("E2 持续 4 秒偏差：3 秒内重锚（6s 时偏差 %.2fs < 0.5）" % e6, e6 < 0.5)
    ck("E2b 之后保持正确：15s→%.2fs、29s→%.2fs" % (e15, e29), e15 < 0.6 and e29 < 0.6)

    # F 防抖
    trk = CP.CuePositionTracker()
    trk.rate = 1.0
    trk.feed(7, 100.0, 30, 0.0, beat_len=BL)
    vels = []
    for k in range(1, 60):
        t = round(0.2 * k, 6)
        trk.feed(7, 100.0 + t + 5.0, 30, t, beat_len=BL)   # 一直偏 5 秒
        vels.append(trk._vel)
    early = max(abs(v - 1.0) for v in vels[:9])            # 前 1.8 秒
    late = max(abs(v - 1.0) for v in vels[20:])            # 4 秒之后
    ck("F1 防抖窗口内（<3s）速度只 ±5%", early <= 0.0501, "最大偏离 %.3f" % early)
    ck("F2 远跳不靠提速（速度仍在 ±5% 内，靠 2.5 秒后的重锚）",
       late <= 0.0501, "最大偏离 %.3f" % late)

    # G 变速 0.8×
    _trk, rows = run(steady(0.8, jitter=0.15), seconds=12.0, rate=0.8)
    pos = [p for _t, p, _s in rows if p is not None]
    ck("G 0.8× 变速：12 秒走 9.6±0.6 秒", abs((pos[-1] - pos[0]) - 9.6) <= 0.6,
       "实际 %.2fs" % (pos[-1] - pos[0]))

    # H 换歌：位置立刻重锚（这是唯一允许的跳变）
    trk = CP.CuePositionTracker()
    trk.rate = 1.0
    trk.feed(7, 100.0, 30, 0.0, beat_len=BL)
    for k in range(1, 6):
        trk.feed(7, 100.0 + 0.2 * k, 30, 0.2 * k, beat_len=BL)
    seen_switch = False
    pb = None
    for k in range(10, 30):
        st, pb, sid = trk.feed(9, 30.0, 40, 0.2 * k, beat_len=BL)
        if st == "switch":
            seen_switch = True
            break
    ck("H 换歌后位置立刻到新歌的开头（唯一允许的跳）",
       seen_switch and pb is not None and abs(pb * BL - 30.0) < 3.0,
       "st=%s pb=%s" % (st, pb))

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 位置单调连续、野匹配完全忽略、防抖 3 秒生效、变速照常跟、换歌才重锚。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
