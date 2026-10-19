# -*- coding: utf-8 -*-
"""QA 第三轮：闭环自证偏差的【可达性】分析 + 逐步注入实测。

问题：hint 会把匹配拉向「错误但票数够」的同歌候选（已在 _qa3_real.py 证明）。
但这要求时钟**先**被推到那个错误位置。本脚本回答：
  1. 真实数据里，是否存在「同歌错候选、票数≥门槛(120)、距真值>HINT_W」的窗？连续几个？
  2. 若时钟被推到该错位置，hint 会不会把它**永久锁死**在那里（teleport 到本曲主错偏移）？
  3. hint 完全错误(偏 100 拍)时，_pick 是否安全退回全局众数？
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
MINV = E.AutoVJEngine.GRID_MIN_VOTES       # 120
STRONG = E.AutoVJEngine.GRID_STRONG_VOTES  # 180

seq = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"),
                     encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


def make_self():
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._bar_off_user = None
    s.mode = "normal"
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


# ================================================ 1 可达性：强错候选窗与连续段
print("=" * 78)
print("1 可达性：真实数据里「同歌错候选 票≥%d 且 距真值>%d 帧」的窗" % (MINV, FP.HINT_W))
print("=" * 78)
run_rows = []
for d in seq:
    exp = d["exp"]
    flags = []
    for w in d["wins"]:
        dt = round(w["t"] * FPS)
        cands = {(c[0], c[1]): c[2] for c in w["cand"]}
        bad = [(s, dd, v) for (s, dd), v in cands.items()
               if s == exp and abs(dd - dt) > FP.HINT_W and v >= MINV]
        flags.append(max(bad, key=lambda x: x[2]) if bad else None)
    # 连续段
    i, n = 0, len(flags)
    while i < n:
        if flags[i] is None:
            i += 1
            continue
        j = i
        while j + 1 < n and flags[j + 1] is not None:
            j += 1
        run_rows.append((d["song"], d["wins"][i]["t"], j - i + 1,
                         round((flags[i][1] - flags[i][1]) * 1, 2),
                         statistics.median([f[2] for f in flags[i:j + 1]]),
                         (flags[i][1] - round(d["wins"][i]["t"] * FPS)) / FPS))
        i = j + 1
run_rows.sort(key=lambda r: -r[2])
print("总窗 %d；出现强错候选的连续段 %d 个" % (sum(len(d["wins"]) for d in seq), len(run_rows)))
print("最长连续段 top10（歌, 起始t, 连续窗数, 错候选票中位, 错候选偏移秒）:")
for r in run_rows[:10]:
    print("   %-30s t=%6.1fs 连=%2d 窗 票=%.0f  Δ=%+.2fs"
          % (r[0][:30], r[1], r[2], r[4], r[5]))
consec = [r for r in run_rows if r[2] >= 2]
print("连续段 ≥2 窗的数量 = %d（这些才可能让 >32 拍的重锚判据通过）" % len(consec))
for r in consec[:10]:
    print("   %-30s t=%6.1fs 连=%2d 窗 票=%.0f Δ=%+.2fs" % (r[0][:30], r[1], r[2], r[4], r[5]))


# ================================================ 2 每首主错偏移 + teleport 锁死测试
print("\n" + "=" * 78)
print("2 teleport 到本曲「主错偏移」后，hint 是否把时钟永久锁死")
print("=" * 78)


def dominant_decoy(d):
    exp = d["exp"]
    cnt = {}
    for w in d["wins"]:
        dt = round(w["t"] * FPS)
        for (s, dd), v in {(c[0], c[1]): c[2] for c in w["cand"]}.items():
            if s == exp and abs(dd - dt) > FP.HINT_W and v >= MINV:
                key = round((dd - dt) / FPS, 1)   # 秒
                cnt[key] = cnt.get(key, 0) + 1
    if not cnt:
        return None
    k = max(cnt, key=cnt.get)
    return k, cnt[k]


def replay(song, tele_sec_beats=None, t_tele=40.0):
    wins = song["wins"]
    exp = song["exp"]
    if not wins:
        return 0.0, []
    anchor, bl = wins[0]["anchor"], wins[0]["bl"]
    s = make_self()
    errs = []
    win_by_start = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    cur, cur_ref = None, 1000.0
    t = 0.0
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            hist = {(c[0], c[1]): c[2] for c in w["cand"]}
            hint_f = None
            if s.audio.grid_pred_orig >= 0.0:
                hint_f = s.audio.grid_pred_orig * FPS
            picked = FP.FingerprintDB._pick(hist, hint_f)
            if picked is None:
                cur = None
            else:
                (sid, delta), votes = picked
                cur = {"sid": int(sid), "off": delta * FP.HOP_SIZE / FP.SAMPLE_RATE,
                       "votes": int(votes)}
            cur_ref = 1000.0 + t
        if tele_sec_beats is not None and round(t, 3) == round(t_tele, 3):
            clk = s._grid_clock
            clk.pos0 += tele_sec_beats
            clk.last_pos = None
            if s._grid_prov is not None:
                pp = s._grid_prov
                s._grid_prov = (pp[0], pp[1] + tele_sec_beats * bl, pp[2])
            if s._grid_pending is not None:
                pp = s._grid_pending
                s._grid_pending = (pp[0], pp[1] + tele_sec_beats * bl, pp[2])
        FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (t + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
        t += DT
    return (max((abs(e) for _, e in errs), default=0.0)), errs


locked_songs = []
for d in seq:
    dd = dominant_decoy(d)
    if dd is None:
        continue
    decoy_sec, votes_n = dd
    decoy_beats = decoy_sec / 0.5      # bl=0.5
    _, errs = replay(d, tele_sec_beats=decoy_beats)
    post = [e for t, e in errs if t >= 60.0]
    fin = statistics.mean(abs(e) for e in post) if post else 0.0
    if fin > 50.0:
        locked_songs.append((d["song"], decoy_sec, decoy_beats, fin))
for nm, ds, db, fin in sorted(locked_songs, key=lambda r: -r[3]):
    print("   ★永久锁死：%-34s 主错偏移=%+.1fs(%+.1f 拍) 60s 后残余 |err|=%.1f 拍"
          % (nm[:34], ds, db, fin))
if not locked_songs:
    print("   未发现「锁死」，残余最大 = %.1f 拍"
          % max((statistics.mean(abs(e) for t, e in replay(d, tele_sec_beats=dominant_decoy(d)[0] / 0.5)[1]
                                 if t >= 60.0) for d in seq if dominant_decoy(d)), default=0.0))
print("   主错偏移曲目数 = %d" % sum(1 for d in seq if dominant_decoy(d)))


# ================================================ 3 hint 完全错误
print("\n" + "=" * 78)
print("3 hint 完全错误（偏 +100 拍 = +1077 帧）：_pick 是否退回全局众数")
print("=" * 78)
ok_fallback = 0
tot = 0
for d in seq[:6]:
    for w in d["wins"][:20]:
        hist = {(c[0], c[1]): c[2] for c in w["cand"]}
        if not hist:
            continue
        dt = round(w["t"] * FPS)
        g = max(hist.items(), key=lambda kv: kv[1])
        r = FP.FingerprintDB._pick(hist, dt + 100 * 10.77)
        tot += 1
        if r is not None and r[0] == g[0]:
            ok_fallback += 1
print("   随机抽样 %d 窗：给出偏 100 拍的 hint 时，_pick 结果 == 全局众数 的比例 = %.1f%%"
      % (tot, 100 * ok_fallback / max(1, tot)))
print("   （窗内无候选 ⇒ 退回 max(votes)，与无 hint 完全一致；这是安全兜底）")
