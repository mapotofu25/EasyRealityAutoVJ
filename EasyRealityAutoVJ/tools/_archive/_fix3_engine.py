# -*- coding: utf-8 -*-
"""第三轮 ★A/★B 引擎级实测（走真实 _beat_pos → _beat_pos_grid 门控）。

A：低票数长段，引擎对低票窗调用 GridClock.update_rate（只刷新速率）→ 恢复时不猛跳。
B：① 首个 offset 是坏值（低票）时**不再硬锚**（首次锁定走信任判据）→ 错误很快消失；
   ② 已锁定后单个坏的可信窗只让 err 超阈 1 次 → run=1 不硬重锚（靠「连续同向」挡掉）。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_engine.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import engine as E  # noqa: E402

BL = 60.0 / 128.0     # 128 BPM
ANCHOR = 1.0
SID = 777
DT = 1.0 / 60.0


class FakeTime:
    def __init__(self, t=9000.0):
        self.t = t

    def perf_counter(self):
        return self.t

    def time(self):
        return 1600000000.0 + self.t


FT = FakeTime()
E.time = FT


def make_self(bl=BL, anchor=ANCHOR, sid=SID):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._song_grid_lookup = {sid: {"beat_len": bl, "anchor": anchor, "bpm": 128.0, "src": "vdj"}}
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_state"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


def snap(off, ref_t, votes, r):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": SID, "recognized_offset": off, "recognized_rate": r,
            "recognized_t": ref_t, "recognized_votes": votes}


def run(duration, truth_fn, off_fn, r_fn, votes_fn):
    """每帧推进挂钟；每 0.5s 出一个识别窗（ref_t=窗末挂钟）。非窗帧沿用同一 ref_t
    （真实链路里 recognized_t 只在出新窗时变化）。返回 rec=[(t, pos_out, ref_pos_true, hard_flag)]。"""
    s = make_self()
    rec = []
    last_ref = -1e9
    cur_ref = FT.t
    t = 0.0
    while t <= duration + 1e-9:
        FT.t = 9000.0 + t
        is_win = (t - last_ref >= 0.5 - 1e-9)
        if is_win:
            cur_ref = FT.t
            last_ref = t
        before = s._grid_clock.last_pos
        p = s._beat_pos(snap(off_fn(t), cur_ref, votes_fn(t), r_fn(t)))
        hard = is_win and (s._grid_clock.last_pos is None)
        rec.append((t, p, (truth_fn(t) - ANCHOR) / BL, hard))
        t += DT
    return s, rec


def lags_of(rec):
    return [r[1] - r[2] for r in rec]


def maxstep(rec):
    return max((abs(rec[i][1] - rec[i - 1][1]) for i in range(1, len(rec))), default=0.0)


print("=" * 78)
print("★A（引擎级）低票数长段：低票窗走 update_rate（喂 RateEstimator 收敛后的 r）")
print("=" * 78)
for gap_s in (20, 40, 60):
    r_true = 1.05

    def truth_fn(t):
        return ANCHOR + r_true * t
    off_fn = truth_fn

    def r_fn(t):
        return 1.05 if t >= 3.0 else 1.00     # RateEstimator ~3s 收敛

    def votes_fn(t):
        return 300 if (t < 3.0 or t > 3.0 + gap_s) else 50

    s, rec = run(3.0 + gap_s + 5.0, truth_fn, off_fn, r_fn, votes_fn)
    lags = lags_of(rec)
    i_end = max(i for i, r in enumerate(rec) if r[0] <= 3.0 + gap_s)
    print("  gap=%2ds：自由跑末误差 %+0.3f 拍；恢复段最大单帧跳变 %.3f 拍；终值 %+0.3f"
          % (gap_s, lags[i_end], maxstep(rec), lags[-1]))
print("  （对照旧实现：20s→0.97 / 40s→1.83 / 60s→6.65 拍跳变）")

print()
print("=" * 78)
print("★B-① 首个 offset 是坏值（低票 votes=100，偏 +16 拍），随后低票但正确")
print("=" * 78)
BAD = 16.0
truth = lambda t: ANCHOR + t


def off_bad(t):
    if t < 0.25:
        return ANCHOR + t + BAD * BL        # 首个窗：坏
    return ANCHOR + t


def votes_low(t):
    return 100 if t < 10.0 else 300

s, rec = run(14.0, truth, off_bad, lambda t: 1.0, votes_low)
lags = lags_of(rec)
print("  首次坏值后：t+0.6s 误差 %+0.3f 拍；t+1s %+0.3f；低票段末(t=10s) %+0.3f；末 %+0.3f"
      % (lags[int(0.6 / DT)], lags[int(1.0 / DT)],
         lags[max(i for i, r in enumerate(rec) if r[0] <= 10.0)], lags[-1]))
print("  （旧实现：硬锚到坏 offset，低票段末仍错 +16 拍，直到高票窗才纠正）")

print()
print("=" * 78)
print("★B-② 已锁定后，单个坏的可信窗（votes=150, +16 拍）不应触发硬重锚")
print("=" * 78)
s, rec = run(12.0, truth,
             lambda t: ANCHOR + t + (BAD * BL if 5.0 <= t < 5.5 else 0.0),
             lambda t: 1.0, lambda t: 150)
lags = lags_of(rec)
mx = max(lags, key=abs)
print("  单弱坏窗(votes=150)后：最大误差 %+0.3f 拍 @t=%.2fs；末值 %+0.3f 拍"
      % (mx, rec[lags.index(mx)][0], lags[-1]))
print("  （强坏窗 votes>=180 才会单次重锚；弱窗须「连续同向」两窗确认）")

print()
print("=" * 78)
print("★B-③ 连续 2 个坏窗（同向）→ 判为真跳转（E 的意图，非误接受）")
print("=" * 78)
s, rec = run(12.0, truth,
             lambda t: ANCHOR + t + (BAD * BL if 5.0 <= t < 6.0 else 0.0),
             lambda t: 1.0, lambda t: 150)
lags = lags_of(rec)
mx = max(lags, key=abs)
print("  连续弱坏窗(votes=150)后：最大误差 %+0.3f 拍；t+8s %+0.3f；末 %+0.3f"
      "（跟随坏坐标，恢复后回正 —— 与真 Beat Jump 无法区分，属预期）"
      % (mx, lags[int(8.0 / DT)], lags[-1]))

print()
print("=" * 78)
print("★B-④ 真实票数分布：可信窗稀疏 + 其间大量**不可信坏窗**（重复段落歧义, 偏 +20 拍）")
print("=" * 78)
# 真实素材实测：votes p50=56 / p75=113 ⇒ 大量窗不可信，时钟长期锁不上。
# 旧实现此时直接返回当前 offset（不可信坏窗 ⇒ 拍位乱跳）；现改为「从上个可信窗自由跑」。
def votes_sparse(t):
    return 300 if (t < 0.25 or t >= 10.0) else 50      # 0.25~10s 全是不可信窗


def off_sparse(t):
    # 不可信段给「坏 offset」(偏 +20 拍)：模拟重复段落歧义
    return ANCHOR + t + (20.0 * BL if 0.25 <= t < 10.0 else 0.0)

s, rec = run(14.0, truth, off_sparse, lambda t: 1.0, votes_sparse)
lags = lags_of(rec)
i_bad = [i for i, r in enumerate(rec) if 0.5 <= r[0] < 9.5]
mbad = max(abs(lags[i]) for i in i_bad)
print("  不可信坏窗段(0.5~9.5s)：最大误差 %+0.3f 拍；抖动量程 %.3f 拍；末值 %+0.3f"
      % (lags[i_bad[len(i_bad) // 2]], mbad, lags[-1]))
print("  （旧实现：直接返回坏 offset ⇒ 该段拍位乱跳 ±20 拍；现自由跑 ⇒ 仍贴合真实拍）")
