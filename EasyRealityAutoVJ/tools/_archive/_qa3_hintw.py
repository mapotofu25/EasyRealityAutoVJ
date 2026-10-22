# -*- coding: utf-8 -*-
"""QA 第三轮：HINT_W 扫描（8/16/24/32/48/64/96）—— 端到端 max|err| 与无回归权衡曲线。

复用工程师的真实逐窗直方图，直接改 fp.HINT_W 后重跑端到端重放（与 _qa3_loop.py 同路径）。
"""
import json
import os
import statistics
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP                 # noqa: E402
import engine as E              # noqa: E402

FPS = FP.SAMPLE_RATE / FP.HOP_SIZE
DT = 1.0 / 60.0
seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


def make_self():
    s = types.SimpleNamespace()
    for k, v in dict(_pos_ref=None, switch_pos=0.0, _next_due=None, _due_off=0.0,
                     _bar_off_user=None, mode="normal").items():
        setattr(s, k, v)
    s.cfg = {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s._grid_prov = None
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0)
    setattr(s, "_beat_pos_grid",
            types.MethodType(getattr(E.AutoVJEngine, "_beat_pos_grid"), s))
    for a in ("GRID_MIN_VOTES", "GRID_STRONG_VOTES", "GRID_CONSIST_LO",
              "GRID_CONSIST_HI", "GRID_PROV_TOL_BEATS"):
        setattr(s, a, getattr(E.AutoVJEngine, a))
    return s


def snap(sid, off, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def replay(song, use_hint=True):
    wins, exp = song["wins"], song["exp"]
    if not wins:
        return 0.0, []
    anchor, bl = wins[0]["anchor"], wins[0]["bl"]
    s = make_self()
    errs = []
    by = {w["t"]: w for w in wins}
    cur, cur_ref = None, 1000.0
    t, t_end = 0.0, wins[-1]["t"]
    while t <= t_end + DT:
        w = by.get(round(t, 3))
        if w is not None:
            hist = {(c[0], c[1]): c[2] for c in w["cand"]}
            hint_f = None
            if use_hint and s.audio.grid_pred_orig >= 0.0:
                hint_f = s.audio.grid_pred_orig * FPS
            picked = FP.FingerprintDB._pick(hist, hint_f)
            if picked is None:
                cur = None
            else:
                (sid, delta), votes = picked
                cur = {"sid": int(sid), "off": delta * FP.HOP_SIZE / FP.SAMPLE_RATE,
                       "votes": int(votes)}
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
        t += DT
    return max((abs(e) for _, e in errs), default=0.0), errs


print("=" * 78)
print("HINT_W 扫描（20 首、前 150s、真实直方图）")
print("=" * 78)
print("  W(帧)  W(拍)  全局max|err|  中位曲目max|err|  |err|<1 中位  最差曲目")
for W in (8, 16, 24, 32, 48, 64, 96):
    FP.HINT_W = W
    per, allbad = [], []
    for song in seq:
        mx, errs = replay(song)
        if not errs:
            continue
        bad = sum(1 for _, e in errs if abs(e) > 1.0) / len(errs)
        per.append((song["song"], mx))
        allbad.append(1.0 - bad)
    per.sort(key=lambda r: -r[1])
    print("  %5d  %5.2f  %11.2f  %13.2f  %11.1f%%  %s"
          % (W, W / 10.77, per[0][1], statistics.median(m for _, m in per),
             100 * statistics.median(allbad), per[0][0][:28]))
FP.HINT_W = 32
print("\n  说明：W=32 时无回归（hint=None 路径）与 W 无关 —— 见 _qa3_fpzero 逐值对照。")
