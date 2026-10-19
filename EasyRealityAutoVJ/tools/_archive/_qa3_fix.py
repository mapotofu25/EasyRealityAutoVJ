# -*- coding: utf-8 -*-
"""QA 第三轮：闭环自证偏差「最小修复」候选验证。

候选修复 H2：**未锁定阶段不发布临时 hint**（只在 clock 已锁定、坐标系被多窗证实时才发 hint）。
对照：
  A2   原样（未锁定也发临时 hint）        —— 工程师现状。
  H2   未锁定不发 hint（发布 -1）        —— 候选修复。
指标：① 从 t=0 的 20 首端到端 max|err|（不能退化）；② 各曲「中途开播」误锁时长（应显著改善）。
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
seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


def mk():
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


def snap(sid, off, votes, rt):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": rt, "recognized_votes": votes}


def replay(song, start_t, mode):
    """mode: 'A2'=现状；'H2'=未锁定不发临时 hint。"""
    wins, exp = song["wins"], song["exp"]
    anchor, bl = wins[0]["anchor"], wins[0]["bl"]
    s = mk()
    dur = mx = 0.0
    by = {w["t"]: w for w in wins}
    cur, cur_ref = None, 1000.0
    t, t_end = start_t, wins[-1]["t"]
    while t <= t_end + DT:
        w = by.get(round(t, 3))
        if w is not None:
            hist = {(c[0], c[1]): c[2] for c in w["cand"]}
            hf = s.audio.grid_pred_orig * FPS if s.audio.grid_pred_orig >= 0.0 else None
            pk = FP.FingerprintDB._pick(hist, hf)
            if pk is None:
                cur = None
            else:
                (sid, dl), v = pk
                cur = {"sid": int(sid), "off": dl * FP.HOP_SIZE / FP.SAMPLE_RATE,
                       "votes": int(v)}
            cur_ref = 1000.0 + t
        FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            oe = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, oe, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, oe, int(cur["votes"]), cur_ref), gs)
            if mode == "H2" and s._grid_clock.sid != exp:
                s.audio.grid_pred_orig = -1.0      # 未锁定：抑制临时 hint
            e = pos - (t + 4.5 - anchor) / bl
            if abs(e) > 1.0:
                dur += DT
            mx = max(mx, abs(e))
        else:
            s.audio.grid_pred_orig = -1.0
        t += DT
    return dur, mx


# ① 从 t=0
print("=" * 78)
print("① 从 t=0 的 20 首端到端（A2 现状 vs H2 候选修复）")
print("=" * 78)
a2_mx = h2_mx = 0.0
for d in seq:
    _, m1 = replay(d, 0.0, "A2")
    _, m2 = replay(d, 0.0, "H2")
    a2_mx = max(a2_mx, m1)
    h2_mx = max(h2_mx, m2)
print("  A2 全局 max|err| = %.2f 拍；H2 全局 max|err| = %.2f 拍" % (a2_mx, h2_mx))

# ② 中途开播
print("\n" + "=" * 78)
print("② 中途开播误锁：A2 时长/max vs H2 时长/max")
print("=" * 78)
rows = []
for d in seq:
    for st in (1., 2., 3., 5., 8., 10., 15., 20., 30.):
        da, ma = replay(d, st, "A2")
        dh, mh = replay(d, st, "H2")
        rows.append((da - dh, d["song"], st, da, ma, dh, mh))
rows.sort(key=lambda r: -r[0])
print("  H2 相对 A2 缩短最多的 10 例（>0 = H2 更好）：")
for dd, nm, st, da, ma, dh, mh in rows[:10]:
    print("    %+6.1fs  %-28s start=%4.0fs  A2 %6.1fs/max%5.0f  H2 %6.1fs/max%5.0f"
          % (dd, nm[:28], st, da, ma, dh, mh))
print("  平均缩短 = %+.1fs；H2 变差的例数 = %d/%d；最差变差 = %+.1fs"
      % (sum(r[0] for r in rows) / len(rows),
         sum(1 for r in rows if r[0] < -0.6), len(rows),
         min(r[0] for r in rows)))
_w = max(rows, key=lambda r: r[5])
print("  H2 后仍存在的最大误锁：%s start=%.0fs 时长=%.1fs max=%.0f 拍"
      % (_w[1][:30], _w[2], _w[5], _w[6]))
