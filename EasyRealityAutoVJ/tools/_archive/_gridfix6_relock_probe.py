# -*- coding: utf-8 -*-
"""③ 可行性测量：检验「hint 选中候选 与 全局众数 连续 N 窗持续背离 + 全局众数自身连续一致」
这一判据**是否真的能区分「当前锁定是否正确」**（QA 已证单窗投票比不行，这里测多窗持续性）。

做法：对每个用例跑现役闭环（hint 来自引擎上一帧发布），**同时**用同一窗的直方图算「无 hint
的全局众数」（引擎拿不到，但这里可离线算）。逐窗记录：

  · cur  = 引擎（hint）选中候选的 offset（秒）      —— 等价于引擎的 recognized_offset-4.5
  · gcur = 全局众数的 offset（秒）
  · true = 该窗真实歌曲位置（秒）= 窗起点 t

判据信号（team-lead 版）：
  FIRE(t)  =  「最近 N 窗 |cur-gcur| > TOL 持续背离」
              且「最近 N 窗 gcur 自身速率一致（相邻差 ≈ 挂钟差，rate∈[0.6,1.45]）」

再看 FIRE 时刻 hint 是否**真的错了**（|cur - true| > 1 拍）：
  · 若 FIRE 时 hint 常常确实错 ⇒ 判据可用（值得实现重开锁）；
  · 若 FIRE 时 hint 大多是对的、或该错的用例根本 FIRE 不了 ⇒ 判据不可分 ⇒ 明确报告做不到。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix6_relock_probe.py [song_sub]
"""
import importlib.util
import json
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
N = 4          # 连续窗数
TOL_S = 1.0    # 「背离」阈值（秒）
TRUE_TOL_S = 1.0   # 「hint 错」阈值（秒）


def _base_self(cfg=None):
    s = types.SimpleNamespace()
    for a, v in (("_pos_ref", None), ("switch_pos", 0.0), ("_next_due", None),
                 ("_due_off", 0.0), ("_due_w", None), ("_bar_off_user", None),
                 ("_grid_active_sid", None), ("_grid_coord_delta", 0.0),
                 ("_grid_last_pos", 0.0), ("_grid_last_out", None),
                 ("_grid_pending", None), ("_grid_seen", None)):
        setattr(s, a, v)
    s.mode = "normal"
    s.locked = False
    s.layers = []
    s.cfg = cfg or {"auto": {"beat_grid": True}, "mode": {"sensitivity": 1}}
    s._grid_clock = E.GridClock()
    s._song_grid_lookup = {}
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


def _pick_off(w, hint_f=None, hint_w=None):
    hist = {(c[0], c[1]): c[2] for c in w["cand"]}
    picked = FP.FingerprintDB._pick(hist, hint_f, hint_w)
    if picked is None:
        return None
    (sid, delta), votes = picked
    return (int(sid), delta * HOP / SR, int(votes))


def replay_one(d, st):
    bl = d["bl"]; anchor = d["anchor"]; exp = d["exp"]
    s = _base_self()
    s._song_grid_lookup = {exp: {"beat_len": bl, "anchor": anchor, "bpm": 120.0, "src": "vdj"}}
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    win_by_start = {w["t"]: w for w in d["wins"]}
    wins = [w for w in d["wins"] if w["t"] >= st - 1e-9]
    if not wins:
        return []
    t = float(st)
    t_end = wins[-1]["t"]
    cur_ref = 1000.0 + t
    rows = []
    cur = None
    lock_t = None
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            hint_f = hint_w = None
            p = s.audio.grid_pred_orig
            if p is not None and p >= 0.0:
                hint_f = p * SR / HOP
                pw = s.audio.grid_pred_w
                if pw is not None and float(pw) > 0.0:
                    hint_w = float(pw)
            cur = _pick_off(w, hint_f, hint_w)
            gcur = _pick_off(w, None, None)
            cur_ref = 1000.0 + t
            locked = (s._grid_active_sid is not None)
            if locked:
                rows.append({"t": t,
                             "cur": (cur[1] if cur else None),
                             "gcur": (gcur[1] if gcur else None)})
        FT.t = 1000.0 + t
        sid = cur[0] if cur is not None else -1
        off_end = (cur[1] + 4.5) if cur is not None else -1.0
        votes = cur[2] if cur is not None else 0
        s._beat_pos({"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
                     "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
                     "recognized_song_id": sid, "recognized_offset": off_end,
                     "recognized_rate": 1.0, "recognized_t": cur_ref,
                     "recognized_votes": votes})
        if s._grid_active_sid is not None and lock_t is None:
            lock_t = t
        t += DT
    return rows


def signal_fires(rows):
    """返回每个满足判据的窗索引（用 rows 内相邻窗，窗间隔≈1s）。"""
    fire = []
    gcons = []      # 每窗：全局众数是否与前一窗速率一致
    for i in range(len(rows)):
        if i == 0:
            gcons.append(False)
            continue
        dt = rows[i]["t"] - rows[i - 1]["t"]
        g0 = rows[i - 1]["gcur"]; g1 = rows[i]["gcur"]
        ok = False
        if g0 is not None and g1 is not None and dt > 0.05:
            rate = (g1 - g0) / dt
            ok = 0.6 <= rate <= 1.45
        gcons.append(ok)
    for i in range(len(rows)):
        if i < N - 1:
            continue
        div = all(rows[j]["cur"] is not None and rows[j]["gcur"] is not None
                  and abs(rows[j]["cur"] - rows[j]["gcur"]) > TOL_S
                  for j in range(i - N + 1, i + 1))
        cst = all(gcons[j] for j in range(i - N + 1, i + 1))
        if div and cst:
            fire.append(i)
    return fire, gcons


def longest_wrong_run(rows, bl):
    """最长连续「hint 错」区间（秒）：|cur - 窗口真实位置| > 1 拍。"""
    tol = 1.0 * bl
    best = cur_run = 0
    runs = []
    start = None
    for r in rows:
        bad = (r["cur"] is not None and abs(r["cur"] - r["t"]) > tol)
        if bad:
            if start is None:
                start = r["t"]
            cur_run += 1
            best = max(best, cur_run)
        else:
            if start is not None:
                runs.append((start, r["t"]))
            cur_run = 0
            start = None
    if start is not None:
        runs.append((start, rows[-1]["t"] + 1.0))
    return best, runs


def main():
    seq = json.load(open(SEQ, encoding="utf-8"))
    grids = json.load(open(GRIDS, encoding="utf-8"))
    data = []
    for d in seq:
        if int(d.get("exp", -1)) < 0:
            continue
        g = grids.get(d["song"])
        if not g or not g.get("beat_len"):
            continue
        data.append({"song": d["song"], "exp": int(d["exp"]), "wins": d["wins"],
                     "bl": float(g["beat_len"]), "anchor": float(g["anchor"] or 0.0)})
    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    n_case_fire = 0
    n_fire_windows = 0
    n_fire_hint_wrong = 0
    bad_total = 0          # 最长误锁 > 5s 的用例
    bad_covered = 0        # 其中「误锁区间内」判据触发过的
    rows_bad = []
    longest_any = 0.0
    for d in data:
        for st in starts:
            rows = replay_one(d, st)
            if not rows:
                continue
            fire, _ = signal_fires(rows)
            fire_ts = [rows[i]["t"] for i in fire]
            if fire:
                n_case_fire += 1
            for i in fire:
                n_fire_windows += 1
                if abs(rows[i]["cur"] - rows[i]["t"]) > 1.0 * d["bl"]:
                    n_fire_hint_wrong += 1
            lw, runs = longest_wrong_run(rows, d["bl"])
            longest_any = max(longest_any, lw)
            if lw > 5.0:
                bad_total += 1
                cov = any(any(abs(ft - a) <= 1.0 or (a <= ft <= b + 0.5)
                              for (a, b) in runs) for ft in fire_ts)
                if cov:
                    bad_covered += 1
                rows_bad.append((d["song"], st, lw, len(fire), bool(cov)))
    print("=" * 92)
    print("③ 判据可分性测量（N=%d 窗，背离阈值 %.1fs，全局一致=相邻速率∈[0.6,1.45]）"
          % (N, TOL_S))
    print("=" * 92)
    print("  用例数（已锁定）                          = %d" % (len(data) * len(starts)))
    print("  触发判据（FIRE）的用例数                  = %d" % n_case_fire)
    print("  触发判据的窗数（累计）                    = %d" % n_fire_windows)
    print("    其中 hint 确实错（|cur-true|>1拍）       = %d" % n_fire_hint_wrong)
    print("  最长连续「hint 错」> 5s 的用例数（220 内） = %d" % bad_total)
    print("    其中判据在误锁区间内触发过的            = %d" % bad_covered)
    print("  全部用例里最长连续「hint 错」= %.1f s" % longest_any)
    if rows_bad:
        print("  —— 长误锁用例明细（song / 起点 / 最长错 s / FIRE窗数 / 误锁期内是否触发）——")
        for r in sorted(rows_bad, key=lambda x: -x[2])[:12]:
            print("     %-46s t=%-3d  worst=%5.1fs  fire=%-3d  covered=%s"
                  % (r[0][:46], r[1], r[2], r[3], r[4]))
    print("\n  ★ 结论：", end="")
    if bad_total == 0:
        print("220 例无「最长连续 hint 错 > 5s」用例。")
    elif bad_covered == 0:
        print("长误锁用例（%d 个）**一个都触发不了判据** ⇒ 判据覆盖不了目标失效模式。" % bad_total)
    else:
        print("判据覆盖 %d/%d (%.0f%%) 的长误锁用例；"
              "触发窗里 %d/%d hint 确实错（精度）。"
              % (bad_covered, bad_total, 100.0 * bad_covered / bad_total,
                 n_fire_hint_wrong, max(1, n_fire_windows)))


if __name__ == "__main__":
    main()
