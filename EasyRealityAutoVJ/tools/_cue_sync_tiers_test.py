# -*- coding: utf-8 -*-
"""**音画对齐机制（四级）**回归 —— 用户 2026-10-18 的设计要求逐条钉死。

设计（见 `src/cue_seek_policy.py` 顶部注释）：
    e = (play_sec + LEAD) − player.position()
    ① |e| ≤ 0.05        → 名义倍速，正常播放零干扰
    ② 0.05 < |e| ≤ 0.30 → 比例变速（±25% 封顶），画面连续
    ③ 0.30 < |e| < 0.60 → 倍速拉满 + 开始滞留计时
    ④ |e| ≥ 0.60 持续 2.0s → 强制 seek；|e| ≥ 1.20 → 立即 seek；跳后 1.2s 冷却

判据（全部用假时钟，纯逻辑）：
  A. LEAD 前馈：目标 = play_sec + 0.06；
  B. 死区：偏差 ≤0.05 ⇒ 倍速**恰好**等于名义值，且 20 秒内 0 次 seek；
  C. 比例段：0.15 偏差 ⇒ 倍速在 (1, 1+25%]，且**仍然 0 次 seek**（靠变速就够了）；
  D. 软阈值：0.7 偏差 ⇒ 前 2 秒不 seek（给追赶机会），满 2 秒后才 seek 一次；
  E. 冷却：刚跳完 1.2s 内即使偏差仍大也不再跳（防自激）；
  F. 硬阈值：1.3 偏差 ⇒ **立即** seek，不等滞留；
  G. 持帧（src_ended）/ off 档 ⇒ 既不调速也不 seek。

运行：`python tools/_cue_sync_tiers_test.py`
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cue_seek_policy as P                                       # noqa: E402

FAIL = []
FPS = 60.0


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def tick(self, d):
        self.t += d


class PS:
    def __init__(self, play_sec=10.0, speed=1.0, **kw):
        self.lyr_id = "L1"
        self.item_id = "i1"
        self.play_sec = play_sec
        self.speed = speed
        self.src_ended = False
        self.active = True
        self.missing = False
        for k, v in kw.items():
            setattr(self, k, v)


class Item:
    _codec = "h264"
    _is_dxv = False


class Player:
    """假播放器：位置按 speed 前进；seek 记录次数并瞬移。"""

    def __init__(self, pos, clock):
        self.pos = float(pos)
        self.speed = 1.0
        self.fps = FPS
        self._now = clock
        self.seeks = []
        self.speeds = []

    def position(self):
        return self.pos

    def seek(self, sec):
        self.seeks.append(float(sec))
        self.pos = float(sec)
        return True

    def set_speed(self, s):
        self.speed = float(s)
        self.speeds.append(float(s))

    def advance(self, dt):
        self.pos += dt * self.speed


def run(e0, seconds=4.0, speed=1.0, src_ended=False, profile="auto"):
    """让偏差保持在 e0（= target − position）跑一段时间，返回 (seek 次数, 速度序列)。"""
    clk = Clock()
    pol = P.CueSeekPolicy(cfg={"seek_profile": profile}, now=clk)
    it, ps = Item(), PS(play_sec=10.0, speed=speed, src_ended=src_ended)
    pl = Player(P.sync_target(ps) - e0, clk)
    st = pol.state_for(ps)
    dt = 1.0 / FPS
    warm = 6                       # 前 6 帧只做"入场落地"（换片段必然要落一次），不计入

    def _frame(e, do_count=False):
        pol.settle(ps, pl, st)
        hit = pol.should_seek(ps, it, pl, st)
        if hit:
            pl.seek(pol.seek_target(ps))
            pol.on_seek(ps, st, True, pl.position())
        pl.set_speed(pol.desired_speed_for(ps, it, pl.position()))
        clk.tick(dt)
        return hit

    # 阶段一：e=0 跑几帧，让入场 seek 落地、inflight 清干净
    for _ in range(warm):
        ps.play_sec += dt * speed
        pl.pos = P.sync_target(ps)
        _frame(0.0)
    base_seeks = len(pl.seeks)
    for i in range(int(seconds * FPS)):
        # 让 play_sec 正常推进，并按 e0 反推播放器应有的位置（保持恒定偏差）
        ps.play_sec += dt * speed
        pl.pos = P.sync_target(ps) - e0
        pol.settle(ps, pl, st)
        if pol.should_seek(ps, it, pl, st):
            pl.seek(pol.seek_target(ps))
            pol.on_seek(ps, st, True, pl.position())
        pl.set_speed(pol.desired_speed_for(ps, it, pl.position()))
        clk.tick(dt)
    n_seek = max(0, len(pl.seeks) - base_seeks)
    return n_seek, list(pl.speeds)[2 * warm:], pl


def main():
    print("A. 前馈补偿：目标 = play_sec + LEAD")
    ck("LEAD = %.2fs" % P.LEAD_SEC, abs(P.LEAD_SEC - 0.06) < 1e-9)
    ck("sync_target(10.0) = %.2fs" % (10.0 + P.LEAD_SEC),
       abs(P.sync_target(PS(play_sec=10.0)) - (10.0 + P.LEAD_SEC)) < 1e-9)
    ck("持帧时不加 LEAD（冻结就该停住）",
       abs(P.sync_target(PS(play_sec=10.0, src_ended=True)) - 10.0) < 1e-9)

    print("B. ① 死区内：倍速恰为名义值，20 秒 0 次 seek")
    n, sp, _ = run(0.03, seconds=20.0)
    ck("seek 次数 = 0", n == 0, n)
    ck("倍速恒为 1.000（正常播放零干扰）", all(abs(s - 1.0) < 1e-9 for s in sp),
       sorted(set(round(s, 3) for s in sp))[:5])

    print("C. ② 比例段（0.15 偏差）：只变速、不跳")
    n, sp, _ = run(0.15, seconds=6.0)
    ck("6 秒内 0 次 seek（靠变速就够了）", n == 0, n)
    ck("倍速 > 1 且在 ±%.0f%% 内" % (P.RATE_MAX * 100),
       max(sp) > 1.0 and max(sp) <= 1.0 + P.RATE_MAX + 1e-9,
       "max=%.4f" % max(sp))

    print("D. ③+④ 软阈值（0.70 偏差）：先给 2 秒追赶，满 2 秒才跳一次")
    clk = Clock()
    pol = P.CueSeekPolicy(cfg={}, now=clk)
    it, ps = Item(), PS(play_sec=10.0)
    pl = Player(P.sync_target(ps) - 0.70, clk)
    st = pol.state_for(ps)
    dt = 1.0 / FPS
    for _ in range(6):                     # 入场落地
        ps.play_sec += dt
        pl.pos = P.sync_target(ps)
        pol.settle(ps, pl, st)
        if pol.should_seek(ps, it, pl, st):
            pl.seek(pol.seek_target(ps))
            pol.on_seek(ps, st, True, pl.position())
        clk.tick(dt)
    first_seek_t = None
    for i in range(int(5.0 * FPS)):
        ps.play_sec += dt
        pl.pos = P.sync_target(ps) - 0.70
        pol.settle(ps, pl, st)
        if pol.should_seek(ps, it, pl, st):
            if first_seek_t is None:
                first_seek_t = i * dt
            pl.seek(pol.seek_target(ps))
            pol.on_seek(ps, st, True, pl.position())
        clk.tick(dt)
    ck("确实触发了 seek", first_seek_t is not None)
    ck("第一次 seek 不早于 %.1fs（滞留期给了追赶机会）" % P.SEEK_DWELL_SEC,
       first_seek_t is not None and first_seek_t >= P.SEEK_DWELL_SEC - 0.05,
       "实际 %.2fs" % (first_seek_t or -1))

    print("E. 冷却：刚跳完 1.2s 内不再跳")
    n_after = 0
    for i in range(int(1.0 * FPS)):
        ps.play_sec += dt
        pl.pos = P.sync_target(ps) - 0.70        # 偏差立刻又出现
        pol.settle(ps, pl, st)
        if pol.should_seek(ps, it, pl, st):
            n_after += 1
            pol.on_seek(ps, st, True, pl.position())
        clk.tick(dt)
    ck("冷却期内 0 次 seek", n_after == 0, n_after)

    print("F. ④-硬（1.30 偏差）：立即 seek，不等滞留")
    n, sp, _ = run(1.30, seconds=0.1)
    ck("0.1 秒内就 seek 了", n >= 1, n)

    print("G. 持帧 / off 档：既不调速也不 seek")
    n, sp, _ = run(2.0, seconds=3.0, src_ended=True)
    ck("src_ended：0 次 seek", n == 0, n)
    ck("src_ended：倍速恒为名义值", all(abs(s - 1.0) < 1e-9 for s in sp),
       sorted(set(round(s, 3) for s in sp))[:5])
    n, sp, _ = run(2.0, seconds=3.0, profile="off")
    ck("off 档：0 次 seek", n == 0, n)
    ck("off 档：倍速恒为名义值", all(abs(s - 1.0) < 1e-9 for s in sp),
       sorted(set(round(s, 3) for s in sp))[:5])

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 四级对齐按设计工作：正常不动、小偏差变速、大偏差滞留后才跳。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
