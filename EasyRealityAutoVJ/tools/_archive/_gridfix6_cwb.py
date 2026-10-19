# -*- coding: utf-8 -*-
"""③ 诊断：Can We Believe That (t=0) —— 锁定错到重复副本的逐窗过程。

复刻 tools/_gridfix5_accept.py 的 replay_now（闭环：hint 来自引擎上一帧发布），
逐窗打印：引擎选中候选（带 hint）vs 全局众数（无 hint）vs 真值，验证「hint 与全局众数
多窗持续背离 + 全局众数自身一致」是否**真的能**区分 hint（=当前锁定）对错。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix6_cwb.py [song_sub] [start_t]
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import engine as E  # noqa: E402
import fp as FP  # noqa: E402


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT
DT = 1.0 / 60.0
HOP = FP.HOP_SIZE
SR = FP.SAMPLE_RATE
SEQ = os.path.join(ROOT, "tools", "_fix3_hint_seq.json")
GRIDS = os.path.join(ROOT, "tools", "_gridfix4_grids.json")


def _base_self(cfg=None):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._due_w = None
    s._bar_off_user = None
    s.mode = "normal"
    s.cfg = cfg or {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s._song_grid_lookup = {}
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


def make_now(exp, bl, anchor):
    s = _base_self()
    s._song_grid_lookup = {exp: {"beat_len": bl, "anchor": anchor, "bpm": 120.0, "src": "vdj"}}
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    return s


def _snap_now(sid, off_end, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off_end, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def pick(s, w, use_hint):
    hint_f = hint_w = None
    if use_hint:
        p = s.audio.grid_pred_orig
        if p is not None and p >= 0.0:
            hint_f = p * SR / HOP
            pw = s.audio.grid_pred_w
            if pw is not None and float(pw) > 0.0:
                hint_w = float(pw)
    hist = {(c[0], c[1]): c[2] for c in w["cand"]}
    picked = FP.FingerprintDB._pick(hist, hint_f, hint_w)
    if picked is None:
        return None, hint_f
    (sid, delta), votes = picked
    return (int(sid), delta * HOP / SR, int(votes)), hint_f


def main():
    import json
    sub = sys.argv[1] if len(sys.argv) > 1 else "Can We Believe"
    start_t = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    seq = json.load(open(SEQ, encoding="utf-8"))
    grids = json.load(open(GRIDS, encoding="utf-8"))
    d = None
    for x in seq:
        if sub.lower() in x["song"].lower():
            d = x
            break
    if d is None:
        print("not found")
        return
    g = grids[d["song"]]
    bl = float(g["beat_len"])
    anchor = float(g["anchor"] or 0.0)
    exp = int(d["exp"])
    wins = [w for w in d["wins"] if w["t"] >= start_t - 1e-9]
    print("song=%s bl=%.4f anchor=%.3f" % (d["song"], bl, anchor))
    s = make_now(exp, bl, anchor)
    cur = None
    cur_ref = 1000.0 + (wins[0]["t"] if wins else 0.0)
    win_by_start = {w["t"]: w for w in d["wins"]}
    t = float(start_t)
    t_end = wins[-1]["t"]
    last_err = None
    lock_t = None
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            cur, hf = pick(s, w, True)
            gcur, _ = pick(s, w, False)      # 全局众数（无 hint）
            cur_ref = 1000.0 + t
            # 真值 = 窗口起点的真实歌曲位置 ≈ t（引擎口径：off_end = 真值+4.5）
            if cur is not None:
                off_end = cur[1] + 4.5
                # 全局众数的“位置”
                goff = (gcur[1] + 4.5) if gcur is not None else None
                gt = (goff - 4.5) if goff is not None else None
                print("  win t=%5.1f  引擎选中off=%.2f(v=%d)  全局off=%s  真值off=%.2f  "
                      "Δ(引擎-全局)=%s  引擎err=%+.2f拍"
                      % (t, cur[1], cur[2],
                         ("%.2f" % gcur[1]) if gcur is not None else "-", t,
                         ("%.2f" % (cur[1] - gcur[1])) if gcur is not None else "-",
                         (off_end - (t + 4.5)) / bl))
        FT.t = 1000.0 + t
        sid = cur[0] if cur is not None else -1
        off_end = (cur[1] + 4.5) if cur is not None else -1.0
        votes = cur[2] if cur is not None else 0
        s._beat_pos(_snap_now(sid, off_end, votes, cur_ref))
        if s._grid_active_sid is not None and lock_t is None:
            lock_t = t
            print("  >>> 锁定 t=%.2f  锁定拍位=%.3f  真值拍位=%.3f  锁定误差=%+.2f 拍"
                  % (t, s._grid_last_pos, (t + 4.5 - anchor) / bl,
                     s._grid_last_pos - (t + 4.5 - anchor) / bl))
        t += DT
    print("  锁定时刻 =", lock_t)


if __name__ == "__main__":
    main()
