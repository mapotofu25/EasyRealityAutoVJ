# -*- coding: utf-8 -*-
"""【第二轮旧门控对照·非现役】QA 第二轮：GridClock 真实场景仿真（★A 低票数长段；★E）。
⚠ 本脚本复刻**第二轮旧 engine** 门控（trusted=votes>=180 或换歌才 update；低票窗**完全跳过**
   update，不做 update_rate）。第三轮现役引擎已改：门槛 120 + 低票窗走 update_rate +
   首次锁定「信任判据 + 连续 2 窗一致」+ 逐窗去重。现役实测见
   **tools/_fix3_gridclock.py** 与 **tools/_fix3_engine.py**。
严格复刻 engine._beat_pos_grid 的门控：trusted(votes>=180) 或 换歌 才 update；
每帧（1/60s）取 pos；识别每 0.5s 一个参考。"""
import sys
sys.path.insert(0, r"P:\AutoVJ\src")
import beatgrid as BG

BL = 60.0 / 128.0
ANCHOR = 1.0
DT = 1.0 / 60.0


def simulate(duration, off_fn, r_fn=None, votes_fn=None, sid=7, bl=BL, anchor=ANCHOR,
             frame=DT, ref_dt=0.5, start_r=1.0, label=""):
    """off_fn(t) -> 原曲时间轴位置秒；r_fn(t)->速率；votes_fn(t)->票数。
    返回 (clock, 记录 list[(t, clock_pos, ref_pos, lag)])。"""
    c = BG.GridClock()
    r_fn = r_fn or (lambda t: 1.0)
    votes_fn = votes_fn or (lambda t: 300)
    rec = []
    last_ref = -1e9
    t = 0.0
    while t <= duration + 1e-9:
        if t - last_ref >= ref_dt - 1e-9:
            off = off_fn(t); r = r_fn(t); v = votes_fn(t)
            trusted = (v is None) or (int(v) >= 180)
            if trusted or c.sid != sid:
                c.update(sid, bl, anchor, off, r, t)
            last_ref = t
        p = c.pos(t)
        rec.append((t, p, (off_fn(t) - anchor) / bl, c.r))
        t += frame
    return c, rec


def report(name, rec, head=6, tail=6):
    lags = [r[1] - r[2] for r in rec]
    mx = max(lags, key=abs)
    print("[%s] 最大滞后 %+0.3f 拍 @t=%.2fs；末值 %+0.3f 拍" %
          (name, mx, rec[lags.index(mx)][0], lags[-1]))
    return lags


print("############ ★A 低票数长段：自由跑后恢复 -> 会不会猛跳 ############")
for gap_s, r_true, r_est in ((20, 1.05, 1.03), (40, 1.05, 1.03), (30, 1.05, 1.00), (60, 1.05, 1.00)):
    # 前 3s trusted（r_est 收敛）；中段 votes=50 自由跑 gap_s；末尾恢复 trusted(r 仍 1.05 供 RateEstimator，但时钟冻结)
    def mk(gap_s, r_true, r_est):
        T0 = 3.0
        def off_fn(t):
            return ANCHOR + r_true * t
        def r_fn(t):
            return r_true if t > T0 + gap_s else r_est   # 恢复前时钟只看 r_est
        def votes_fn(t):
            return 300 if (t < T0 or t > T0 + gap_s) else 50
        return off_fn, r_fn, votes_fn
    off_fn, r_fn, votes_fn = mk(gap_s, r_true, r_est)
    c, rec = simulate(3.0 + gap_s + 5.0, off_fn, r_fn, votes_fn)
    lags = [r[1] - r[2] for r in rec]
    # 自由跑段末（恢复前）误差
    i_end = max(i for i, r in enumerate(rec) if r[0] <= 3.0 + gap_s)
    err_free = lags[i_end]
    # 恢复后最大跳变（相邻帧 pos 差）
    maxstep = 0.0; maxat = 0
    for i in range(1, len(rec)):
        d = abs(rec[i][1] - rec[i - 1][1])
        if d > maxstep: maxstep, maxat = d, rec[i][0]
    print("  gap=%2ds r_true=%.2f r_est=%.2f：自由跑末误差 %+0.3f 拍；恢复瞬间最大单帧跳变 %0.3f 拍 @t=%.2fs；终值 %+0.3f"
          % (gap_s, r_true, r_est, err_free, maxstep, maxat, lags[-1]))

print()
print("############ ★E-4 4 拍 Loop 连打（每 2s 回跳 4 拍，共 20s）############")
def loop_sim(loopb, period=2.0, duration=20.0, jitter=0.0):
    # off 随时间推进，每 period 回跳到 loop 起点
    def off_fn(t):
        cyc = int(t / period)
        tic = t - cyc * period
        # 起点：anchor（每周期音乐回到同一原曲位置）
        start = ANCHOR + 0.0
        return start + tic
    return off_fn
for loopb in (4.0, 4.1, 3.9):
    period = loopb * BL
    def off_fn(t, period=period):
        cyc = int(t / period + 1e-9)
        tic = t - cyc * period
        return ANCHOR + tic
    c, rec = simulate(20.0, off_fn, lambda t: 1.0, lambda t: 300)
    lags = [r[1] - r[2] for r in rec]
    mx = max(lags, key=abs)
    print("  周期=%.3fs(=%0.1f拍)：最大滞后 %+0.3f 拍；末值 %+0.3f 拍" % (period, period / BL, mx, lags[-1]))

print()
print("############ ★E-5 常见 BeatJump 回跳 1/2/4/8/16 拍（各一次，之后正常）############")
for back in (1.0, 2.0, 4.0, 8.0, 16.0):
    def off_fn(t, back=back):
        T = 3.0
        if t < T: return ANCHOR + t
        return ANCHOR + t - back * BL   # 一次性后退
    c, rec = simulate(40.0, off_fn)
    lags = [r[1] - r[2] for r in rec]
    # 回跳后残留滞后（t=3.0 刚过）
    i = int(3.05 / DT)
    print("  回跳 %4.1f 拍：T+0.05s 滞后 %+0.3f；T+10s %+0.3f；T+35s %+0.3f"
          % (back, lags[i], lags[int(13.0 / DT)], lags[-1]))

print()
print("############ ★E-6 持续搓盘 ±2s（正弦原曲位置摆动，振幅=2s≈4.27拍）############")
import math
def off_fn(t):
    return ANCHOR + t + 2.0 * math.sin(2 * math.pi * t / 4.0)
c, rec = simulate(20.0, off_fn, lambda t: 1.0, lambda t: 300)
lags = [r[1] - r[2] for r in rec]
mx = max(lags, key=abs)
print("  搓盘 ±2s：最大偏差 %+0.3f 拍，出现在摆动峰" % mx)

print()
print("############ ★A-2 低票数期间 r 估计是否仍更新（RateEstimator vs GridClock.r）############")
from audio_engine import RateEstimator
re = RateEstimator()
re.update(7, 100.0, 0.0)
for k in range(1, 40):
    re.update(7, 100.0 + 1.05 * 0.5 * k, 0.5 * k)   # 真实 r=1.05
print("  RateEstimator（低票数也会 update，因为 votes>0 才走这里）-> r=%.4f" % re.rate)
print("  说明：RateEstimator 照常收敛；但 GridClock.update 被 engine 门控跳过 -> 时钟 r 冻结在最后一次 trusted 值。")

print()
print("############ ★E-7 前向跳转（旧版会一直落后）############")
for fwd in (4.0, 8.0, 32.0):
    def off_fn(t, fwd=fwd):
        T = 3.0
        return ANCHOR + t + (fwd * BL if t >= T else 0.0)
    c, rec = simulate(10.0, off_fn)
    lags = [r[1] - r[2] for r in rec]
    i = int(3.05 / DT)
    print("  前向 +%4.1f 拍：T+0.05s 滞后 %+0.3f 拍；T+5s %+0.3f" % (fwd, lags[i], lags[-1]))
