# -*- coding: utf-8 -*-
"""QA 第三轮：自证偏差「可复现场景」复刻。

复刻三个真实场景：
  S1  Demon's Mischief：把时钟 teleport 到本曲主错偏移后，看 err 轨迹（是否锁死）。
  S2  Can We Believe That：从强错候选连续段（t=2s）**中途开播**，首窗无 hint → 全局众数是否选错并锁死。
  S3  给错误 hint 时，_pick 返回的候选是否同歌、票数是否越过门槛（=是否会被引擎采纳）。
"""
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import fp as FP                 # noqa: E402
import engine as E              # noqa: E402

FPS = FP.SAMPLE_RATE / FP.HOP_SIZE
DT = 1.0 / 60.0
MINV, STRONG = E.AutoVJEngine.GRID_MIN_VOTES, E.AutoVJEngine.GRID_STRONG_VOTES
seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]
BY = {d["song"]: d for d in seq}


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


def replay(song, start_t=0.0, tele=(None, None), trace=False):
    wins = [w for w in song["wins"] if w["t"] >= start_t - 1e-9]
    exp = song["exp"]
    anchor, bl = song["wins"][0]["anchor"], song["wins"][0]["bl"]
    s = make_self()
    errs = []
    by = {w["t"]: w for w in song["wins"]}
    t_end = song["wins"][-1]["t"]
    cur, cur_ref = None, 1000.0
    t = start_t
    while t <= t_end + DT:
        w = by.get(round(t, 3))
        if w is not None:
            hist = {(c[0], c[1]): c[2] for c in w["cand"]}
            hint_f = s.audio.grid_pred_orig * FPS if s.audio.grid_pred_orig >= 0.0 else None
            picked = FP.FingerprintDB._pick(hist, hint_f)
            if picked is None:
                cur = None
            else:
                (sid, delta), votes = picked
                cur = {"sid": int(sid), "off": delta * FP.HOP_SIZE / FP.SAMPLE_RATE,
                       "votes": int(votes)}
            cur_ref = 1000.0 + t
        if tele[0] == round(t, 3):
            s._grid_clock.pos0 += tele[1]
            s._grid_clock.last_pos = None
            if s._grid_prov is not None:
                pp = s._grid_prov
                s._grid_prov = (pp[0], pp[1] + tele[1] * bl, pp[2])
        FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
        t += DT
    if trace:
        return errs
    return max((abs(e) for _, e in errs), default=0.0), errs


# ---------------- S1 Demon's Mischief 锁死轨迹 ----------------
print("=" * 78)
print("S1 Demon's Mischief：teleport 到主错偏移 +160s(=+320 拍)，err 轨迹")
print("=" * 78)
d = BY["Rolling Contact - Demon's Mischief.flac"]
for tele_beats in (160.0 / 0.5,):
    tr = replay(d, tele=(40.0, tele_beats), trace=True)
    print("  注入后每 5s 的 err：")
    last = -100
    for t, e in tr:
        if t >= 40 and int(t) % 5 == 0 and int(t) != last:
            last = int(t)
            print("     t=%6.1fs err=%+9.2f 拍" % (t, e))
tr = replay(d, trace=True)
mxt = max(abs(e) for _, e in tr)
print("  [对照] 不注入：max|err|=%.2f 拍" % mxt)

# ---------------- S2 Can We Believe That 中途开播 ----------------
print("\n" + "=" * 78)
print("S2 Can We Believe That：从 t=2s 中途开播（首窗即撞上 ~200 票强错候选）")
print("=" * 78)
d = BY["Rolling Contact - Can We Believe That.flac"]
# 首窗全局众数到底是什么
w2 = [w for w in d["wins"] if round(w["t"]) == 2][0]
hist2 = {(c[0], c[1]): c[2] for c in w2["cand"]}
g2 = max(hist2.items(), key=lambda kv: kv[1])
print("  t=2s 窗：全局众数 sid=%d delta=%.2fs votes=%d（真值 delta≈2.00s）"
      % (g2[0][0], g2[0][1] / FPS, g2[1]))
for st in (0.0, 2.0, 3.0):
    pick = FP.FingerprintDB._pick(hist2, None if st != 2 else None)
    mx, errs = replay(d, start_t=st)
    print("  从 t=%4.1fs 开播：max|err|=%9.2f 拍  末段(≥100s)中位 |err|=%.2f"
          % (st, mx, sorted(abs(e) for t, e in errs if t >= 100)[len([1 for t, e in errs if t >= 100]) // 2]
             if [1 for t, e in errs if t >= 100] else 0))

# ---------------- S3 错误 hint 返回候选的属性 ----------------
print("\n" + "=" * 78)
print("S3 给「偏 100 拍」的错误 hint 时，_pick 返回候选的属性（是否同歌/过门槛）")
print("=" * 78)
tot = same = over_min = over_strong = 0
for d in seq:
    exp = d["exp"]
    for w in d["wins"][::5]:
        hist = {(c[0], c[1]): c[2] for c in w["cand"]}
        if not hist:
            continue
        dt = round(w["t"] * FPS)
        g = max(hist.items(), key=lambda kv: kv[1])
        r = FP.FingerprintDB._pick(hist, dt + 100 * 10.77)
        tot += 1
        if r is not None and r[0][0] == exp:
            same += 1
            if r[1] >= MINV:
                over_min += 1
            if r[1] >= STRONG:
                over_strong += 1
print("  抽样 %d 窗：返回**同歌**候选 %d (%.1f%%)；其中票≥%d(可信) %d (%.1f%%)，票≥%d(强) %d (%.1f%%)"
      % (tot, same, 100 * same / tot, MINV, over_min, 100 * over_min / tot,
         STRONG, over_strong, 100 * over_strong / tot))
