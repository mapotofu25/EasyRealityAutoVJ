# -*- coding: utf-8 -*-
"""临时诊断：跑完整 hint 重放并逐窗打印（定位仍偏的窗口）。"""
import json
import sys
import os
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP  # noqa: E402
import engine as E  # noqa: E402

DT = 1.0 / 60.0


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT

seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq = [s for s in seq if s["exp"] >= 0]
NAME = sys.argv[1] if len(sys.argv) > 1 else "Raccoon"
T_MAX = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
song = [s for s in seq if NAME in s["song"]][0]
exp = song["exp"]
wins = song["wins"]
bl = wins[0]["bl"]
anchor = wins[0]["anchor"]


def make_self():
    s = types.SimpleNamespace()
    s._pos_ref = None; s.switch_pos = 0.0; s._next_due = None; s._due_off = 0.0
    s._bar_off_user = None; s.mode = "normal"
    s.cfg = {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock(); s._grid_active_sid = None
    s._grid_coord_delta = 0.0; s._grid_last_pos = 0.0; s._grid_last_out = None
    s._grid_pending = None; s._grid_seen = None
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0)
    s._beat_pos_grid = types.MethodType(E.AutoVJEngine._beat_pos_grid, s)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


s = make_self()
win_by_start = {w["t"]: w for w in wins}
cur = None
cur_ref = 1000.0
t = 0.0
last_print_win = None
while t <= wins[-1]["t"] + DT and t <= T_MAX:
    w = win_by_start.get(round(t, 3))
    if w is not None:
        hint_f = None
        if s.audio.grid_pred_orig is not None and s.audio.grid_pred_orig >= 0.0:
            hint_f = s.audio.grid_pred_orig * FP.SAMPLE_RATE / FP.HOP_SIZE
        hist = {(c[0], c[1]): c[2] for c in w["cand"]}
        picked = FP.FingerprintDB._pick(hist, hint_f)
        if picked is None:
            cur = None
        else:
            (sid, delta), votes = picked
            cur = {"sid": int(sid), "off": delta * FP.HOP_SIZE / FP.SAMPLE_RATE,
                   "votes": int(votes)}
        print("win t=%3d hint=%s -> sid=%s votes=%s off_err=%.2f (locked=%s pending=%s)"
              % (w["t"], ("%.1f" % hint_f) if hint_f is not None else "None",
                 cur["sid"] if cur else None, cur["votes"] if cur else 0,
                 ((cur["off"] - w["t"]) / bl) if cur else 0.0,
                 s._grid_clock.sid, s._grid_pending is not None))
        cur_ref = 1000.0 + t
    FT.t = 1000.0 + t
    grid_on = (cur is not None and cur["sid"] == exp and cur["votes"] > 0)
    if grid_on:
        off_end = float(cur["off"]) + 4.5
        gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
        snap = {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
                "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
                "recognized_song_id": exp, "recognized_offset": off_end,
                "recognized_rate": 1.0, "recognized_t": cur_ref,
                "recognized_votes": int(cur["votes"])}
        pos = s._beat_pos_grid(snap, gs)
    else:
        s.audio.grid_pred_orig = -1.0
    t += DT
