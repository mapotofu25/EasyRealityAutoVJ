# -*- coding: utf-8 -*-
"""第三轮修复实测：GridClock 级（★E 真跳转；★E 量化抖动鲁棒性；★A 速率更新）。

严格复刻 engine._beat_pos_grid 的**新**门控：
  trusted(votes>=120) → clock.update(...)；否则 clock.update_rate(...)。
本脚本用 votes=300（恒可信），只验证 GridClock 对真跳转 / 抖动的响应。

用法：P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_gridclock.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from beatgrid import GridClock  # noqa: E402

BL = 60.0 / 128.0     # 128 BPM -> 0.46875 s/拍
ANCHOR = 1.0
DT = 1.0 / 60.0


def hard_of(c, ref_t):
    """该次 update 是否发生硬重锚：硬重锚会 last_pos=None，之后 pos() 会重新填。"""
    before = c.last_pos
    c_pos = c.pos(ref_t)
    return before is None, c_pos


def simulate(duration, off_fn, r_fn=None, votes_fn=None, sid=7, bl=BL, anchor=ANCHOR,
             frame=DT, ref_dt=0.5, noise_fn=None):
    """返回 rec = [(t, clock_pos, ref_pos, r, hard_flag)]。"""
    c = GridClock()
    r_fn = r_fn or (lambda t: 1.0)
    votes_fn = votes_fn or (lambda t: 300)
    noise_fn = noise_fn or (lambda: 0.0)
    rec = []
    last_ref = -1e9
    t = 0.0
    hard_flags = []
    while t <= duration + 1e-9:
        hard = False
        if t - last_ref >= ref_dt - 1e-9:
            off = off_fn(t) + noise_fn()
            r = r_fn(t)
            v = votes_fn(t)
            trusted = (v is None) or (int(v) >= 120)
            if trusted or c.sid != sid:
                c.update(sid, bl, anchor, off, r, t)
                hard = (c.last_pos is None)
            else:
                c.update_rate(bl, r)
            last_ref = t
            if hard:
                hard_flags.append(t)
        p = c.pos(t)
        rec.append((t, p, (off_fn(t) - anchor) / bl, c.r, hard))
        t += frame
    return c, rec, hard_flags


def maxlag(rec):
    lags = [r[1] - r[2] for r in rec]
    mx = max(lags, key=abs)
    return mx, rec[lags.index(mx)][0], lags[-1]


print("=" * 78)
print("★E-1 单次跳转：−1/−2/−4 拍 Beat Jump（先稳定 3s，跳转后正常播 10s）")
print("=" * 78)
for back in (1.0, 2.0, 3.9, 4.0, 4.1):
    def off_fn(t, back=back):
        T = 3.0
        if t < T:
            return ANCHOR + t
        return ANCHOR + t - back * BL
    c, rec, hf = simulate(15.0, off_fn)
    lags = [r[1] - r[2] for r in rec]
    # 跳转后（t>=3.0）误差回到 <1 拍的时刻
    conv = None
    for i, r in enumerate(rec):
        if r[0] >= 3.0 and abs(lags[i]) >= 1.0:
            conv = r[0]
    conv_t = None
    if conv is not None:
        # 找 conv 之后第一次 <1 拍
        for i, r in enumerate(rec):
            if r[0] > conv and abs(lags[i]) < 1.0:
                conv_t = r[0] - 3.0
                break
    print("  回跳 %4.1f 拍：跳转瞬间滞后 %+6.3f；T+1s %+6.3f；T+3s %+6.3f；"
          "T+10s %+6.3f；收敛<1拍用时=%s；硬重锚时刻=%s"
          % (back, lags[int(3.05 / DT)], lags[int(4.0 / DT)], lags[int(6.0 / DT)],
             lags[-1], ("%.2fs" % conv_t) if conv_t is not None else "N/A",
             ["%.2f" % x for x in hf[:4]]))

print()
print("=" * 78)
print("★E-2 前向跳转：+1/+2 拍（应同样跟上）")
print("=" * 78)
for fwd in (1.0, 2.0, 4.0):
    def off_fn(t, fwd=fwd):
        T = 3.0
        return ANCHOR + t + (fwd * BL if t >= T else 0.0)
    c, rec, hf = simulate(15.0, off_fn)
    lags = [r[1] - r[2] for r in rec]
    print("  前向 +%4.1f 拍：T+0.05s %+6.3f；T+1s %+6.3f；T+3s %+6.3f；末 %+6.3f；硬重锚=%s"
          % (fwd, lags[int(3.05 / DT)], lags[int(4.0 / DT)], lags[int(6.0 / DT)],
             lags[-1], ["%.2f" % x for x in hf[:4]]))

print()
print("=" * 78)
print("★E-3 4 拍 Loop 连打 20s（每 loopb*BL 秒回跳一次）")
print("=" * 78)
for loopb in (4.0, 3.9, 4.1):
    period = loopb * BL

    def off_fn(t, period=period):
        cyc = int(t / period + 1e-9)
        return ANCHOR + (t - cyc * period)
    c, rec, hf = simulate(20.0, off_fn)
    lags = [r[1] - r[2] for r in rec]
    mx = max(lags, key=abs)
    # 每个识别窗更新后的残余（tick 时刻，紧邻 tick 的帧）
    tick_res = []
    t = 0.0
    while t <= 20.0:
        i = min(int(round(t / DT)), len(rec) - 1)
        tick_res.append(abs(lags[i]))
        t += 0.5
    print("  周期=%.3fs(=%0.1f拍)：逐帧最大滞后 %+0.3f 拍；识别窗残余 max=%.3f 拍；"
          "末值 %+0.3f 拍；硬重锚次数=%d"
          % (period, period / BL, mx, max(tick_res), lags[-1], len(hf)))

print()
print("=" * 78)
print("★E-4 抖动鲁棒性：±50ms 与 ±量化(±186ms≈±0.4拍) 各 30 组随机种子 × 60s")
print("=" * 78)
for label, amp in (("±50ms", 0.050), ("±186ms量化", 0.186)):
    worst_err = 0.0
    total_hard = 0
    worst_hard = 0
    for seed in range(30):
        rng = random.Random(seed)
        def noise_fn(rng=rng, amp=amp):
            return rng.uniform(-amp, amp)
        def off_fn(t):
            return ANCHOR + t
        c, rec, hf = simulate(60.0, off_fn, noise_fn=noise_fn)
        hf = [x for x in hf if x > 1e-9]      # 去掉 t=0 的首次锁定
        lags = [r[1] - r[2] for r in rec]
        e = max(abs(x) for x in lags)
        worst_err = max(worst_err, e)
        total_hard += len(hf)
        worst_hard = max(worst_hard, len(hf))
    print("  %-12s：30 组最大 |err|=%.3f 拍；硬重锚总计=%d 次（单组最多 %d 次）"
          % (label, worst_err, total_hard, worst_hard))

print()
print("=" * 78)
print("★A 低票数长段：只更新速率（update_rate），恢复时跳变")
print("=" * 78)
T0 = 3.0


def run_A(gap_s, r_true=1.05, warm=3.0, r_initial=1.00):
    """前 warm 秒可信且 RateEstimator 尚未收敛（时钟看到 r_initial）；
    中段 gap 低票数（引擎走 update_rate，喂**已收敛的** r_true）；末段恢复可信。
    返回 (自由跑末误差, 恢复瞬间最大单帧跳变, 跳变时刻, 终值)。"""
    def off_fn(t):
        return ANCHOR + r_true * t
    def r_fn(t):
        return r_true if t >= warm else r_initial
    def votes_fn(t):
        return 300 if (t < warm or t > warm + gap_s) else 50
    c, rec, hf = simulate(warm + gap_s + 5.0, off_fn, r_fn, votes_fn)
    lags = [r[1] - r[2] for r in rec]
    i_end = max(i for i, r in enumerate(rec) if r[0] <= warm + gap_s)
    maxstep, maxat = 0.0, 0
    for i in range(1, len(rec)):
        d = abs(rec[i][1] - rec[i - 1][1])
        if d > maxstep:
            maxstep, maxat = d, rec[i][0]
    return lags[i_end], maxstep, maxat, lags[-1]


for gap_s, r_true, r_seen in ((20, 1.05, 1.00), (40, 1.05, 1.00), (30, 1.05, 1.00),
                              (60, 1.05, 1.00)):
    e_free, mstep, mat, fin = run_A(gap_s, r_true, warm=3.0, r_initial=r_seen)
    print("  gap=%2ds r_true=%.2f 恢复前时钟r=%.2f：自由跑末误差 %+0.3f 拍；"
          "恢复瞬间最大单帧跳变 %.3f 拍 @t=%.2fs；终值 %+0.3f"
          % (gap_s, r_true, r_seen, e_free, mstep, mat, fin))
print("  （对照旧实现：audit 报 20s→0.97 / 40s→1.83 / 30s→3.45 / 60s→6.65 拍跳变）")
