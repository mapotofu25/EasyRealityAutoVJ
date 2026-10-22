# -*- coding: utf-8 -*-
"""QA 第三轮：★ 闭环反馈（hint↔时钟）自证偏差专项实测。

数据：tools/_fix3_hint_seq.json（工程师采集的 20 首真实逐窗投票直方图，前 150s）。
代码：真实 fp.FingerprintDB._pick + 真实 engine.AutoVJEngine._beat_pos_grid + 真实 GridClock。

实验：
  A. 复跑工程师端到端（hint ON/OFF）—— 独立复现他的 max|err| 与占比。
  B. hint 误差分布 & 「真候选是否落在 ±HINT_W 内」& 命中率。
  C. 注入初始偏移（teleport 时钟 +Δ 帧，Δ 逐渐加大）—— 能否被 hint 锁死。
  D. 同歌重复段落错候选锁死测试（Cosmic String：错候选恒定 +115.22s）。
  E. hint 完全错误（偏 100 拍）—— 是否退回全局众数、是否安全。
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
HOP = FP.HOP_SIZE / FP.SAMPLE_RATE
DT = 1.0 / 60.0
W = 4.5

SEQ = os.path.join(ROOT, "tools", "_fix3_hint_seq.json")
seq = json.load(open(SEQ, encoding="utf-8"))
seq = [d for d in seq if int(d.get("exp", -1)) >= 0]


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t

    @staticmethod
    def monotonic():
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
    for nm in ("_beat_pos_grid",):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    for a in ("GRID_MIN_VOTES", "GRID_STRONG_VOTES", "GRID_CONSIST_LO",
              "GRID_CONSIST_HI", "GRID_PROV_TOL_BEATS"):
        setattr(s, a, getattr(E.AutoVJEngine, a))
    return s


def snap(sid, off, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def _hist(w):
    return {(c[0], c[1]): c[2] for c in w["cand"]}


def replay(song, use_hint=True, teleport=None, hint_force=None, log=None):
    """端到端重放。

    teleport: (t_sec, delta_beats) —— 在 t_sec 时把时钟整体平移 delta_beats 拍。
    hint_force: 校准函数 fn(hint_true_frames)->强制 hint 帧；None=正常。
    log: 可选，逐窗记录 dict。
    返回 (max|err|, errs, stats)。
    """
    wins = song["wins"]
    exp = song["exp"]
    if not wins:
        return 0.0, [], {}
    anchor, bl = wins[0]["anchor"], wins[0]["bl"]
    s = make_self()
    errs, hint_errs, misses = [], [], 0
    in_win_true, fallback = 0, 0
    nlock = 0
    win_by_start = {w["t"]: w for w in wins}
    t_end = wins[-1]["t"]
    cur = None
    cur_ref = 1000.0
    t = 0.0
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            hist = _hist(w)
            true_delta = round(w["t"] * FPS)
            hint_f = None
            if use_hint and s.audio.grid_pred_orig is not None and s.audio.grid_pred_orig >= 0.0:
                hint_f = s.audio.grid_pred_orig * FPS
                if hint_force is not None:
                    hint_f = hint_force(hint_f)
            picked = FP.FingerprintDB._pick(hist, hint_f)
            # 统计：真候选是否在 hint 窗内 / 是否退回全局
            if hint_f is not None:
                hint_errs.append(abs(hint_f - true_delta))
                lo, hi = hint_f - FP.HINT_W, hint_f + FP.HINT_W
                has_near = any((lo <= k[1] <= hi) for k in hist)
                if not has_near:
                    fallback += 1
                if lo <= true_delta <= hi:
                    in_win_true += 1
            if picked is None:
                cur = None
            else:
                (sid, delta), votes = picked
                cur = {"sid": int(sid), "off": delta * HOP, "votes": int(votes)}
            cur_ref = 1000.0 + t
        # ---- teleport 注入 ----
        if teleport is not None and round(t, 3) == round(teleport[0], 3):
            clk = s._grid_clock
            clk.pos0 += teleport[1]
            clk.last_pos = None
            if getattr(s, "_grid_prov", None) is not None:
                pp = s._grid_prov
                s._grid_prov = (pp[0], pp[1] + teleport[1] * bl, pp[2])
            if getattr(s, "_grid_pending", None) is not None:
                pp = s._grid_pending
                s._grid_pending = (pp[0], pp[1] + teleport[1] * bl, pp[2])
            s.audio.grid_pred_orig = anchor + (s._grid_clock.pos0) * bl - 4.5
        FT.t = 1000.0 + t
        grid_on = (cur is not None and cur["sid"] == exp and cur["votes"] > 0)
        if grid_on:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            err = pos - (t + 4.5 - anchor) / bl
            errs.append((t, err))
            if s._grid_clock.sid == exp:
                nlock += 1
            if log is not None and w is not None:
                log.append({"t": round(t, 3), "err": err,
                            "hint": s.audio.grid_pred_orig,
                            "hint_f": hint_f, "true_delta": true_delta})
        else:
            s.audio.grid_pred_orig = -1.0
        t += DT
    mx = max((abs(e) for _, e in errs), default=0.0)
    stats = {"hint_errs": hint_errs, "fallback": fallback,
             "in_win_true": in_win_true, "nwin": len(win_by_start), "nlock": nlock}
    return mx, errs, stats


def report_endtoend(use_hint, title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    per, allbad, hmax = [], [], 0.0
    for song in seq:
        try:
            mx, errs, _ = replay(song, use_hint=use_hint)
        except Exception as e:                                        # noqa: BLE001
            print("  %-40s 重放异常 %s" % (song["song"][:40], e))
            continue
        if not errs:
            continue
        bad = sum(1 for _, e in errs if abs(e) > 1.0) / max(1, len(errs))
        per.append((song["song"], mx, bad))
        allbad.append(1.0 - bad)
    per.sort(key=lambda r: -r[1])
    for nm, mx, bad in per[:6]:
        print("  %-42s max|err|=%8.2f 拍；|err|<1 %5.1f%%" % (nm[:42], mx, 100 * (1 - bad)))
    print("  ...")
    print("  总体 %d 首；|err|<1 中位 %.1f%% / 均值 %.1f%%；全局 max=%.2f 拍"
          % (len(per), 100 * statistics.median(allbad),
             100 * (sum(allbad) / max(1, len(allbad))), max(r[1] for r in per)))


# ================================================================ A 端到端复现
report_endtoend(False, "A1 对照（hint=None 旧行为）")
report_endtoend(True, "A2 ★C（hint=引擎预测）")

# ================================================================ B hint 误差分布
print("\n" + "=" * 78)
print("B hint 误差分布 / 真候选命中 / 退回全局众数的频率（20 首汇总）")
print("=" * 78)
allh, allfb, allin, alln = [], 0, 0, 0
for song in seq:
    try:
        _, _, st = replay(song, use_hint=True)
    except Exception:                                                 # noqa: BLE001
        continue
    allh += st["hint_errs"]
    allfb += st["fallback"]
    allin += st["in_win_true"]
    alln += st["nwin"]
if allh:
    hs = sorted(allh)
    print("  hint 误差(帧)：p50=%.1f p90=%.1f p99=%.1f max=%.1f（HINT_W=%d）"
          % (statistics.median(hs), hs[int(len(hs) * .9)], hs[int(len(hs) * .99)],
             hs[-1], FP.HINT_W))
    print("  hint 误差 > HINT_W(%d) 的窗：%d / %d = %.1f%%"
          % (FP.HINT_W, sum(1 for x in hs if x > FP.HINT_W), len(hs),
             100 * sum(1 for x in hs if x > FP.HINT_W) / len(hs)))
print("  hint 窗内无任何候选 ⇒ 退回全局众数的窗：%d / %d = %.1f%%"
      % (allfb, alln, 100 * allfb / max(1, alln)))
print("  真候选落在 hint 窗(±%d 帧)内的窗：%d / %d = %.1f%%"
      % (FP.HINT_W, allin, alln, 100 * allin / max(1, alln)))


# ================================================================ C 注入初始偏移
print("\n" + "=" * 78)
print("C 注入初始偏移：t=40s 把时钟整体平移 +Δ 拍，看能否回到真值（20 首均值）")
print("=" * 78)
DELTAS = [2, 4, 8, 16, 32, 64, 128, 230]
for dlt in DELTAS:
    finals, recs, maxes = [], [], []
    for song in seq:
        try:
            _, errs, _ = replay(song, use_hint=True, teleport=(40.0, dlt))
        except Exception:                                             # noqa: BLE001
            continue
        post = [(t, e) for t, e in errs if t >= 40.0]
        if not post:
            continue
        maxes.append(max(abs(e) for _, e in post))
        tail = [e for t, e in post if t >= 60.0]
        fin = statistics.mean(abs(e) for e in tail) if tail else 0.0
        finals.append(fin)
        recs.append(1 if fin < 1.0 else 0)
    print("  Δ=%4d 拍(=%5.0f 帧)：注入后 max|err|=%7.2f 拍；60s 后残余=%7.3f 拍；恢复(<1拍) %d/%d 首"
          % (dlt, dlt * (1.0 / bl) * FPS if False else dlt * 10.77, statistics.mean(maxes),
             statistics.mean(finals), sum(recs), len(recs)))
