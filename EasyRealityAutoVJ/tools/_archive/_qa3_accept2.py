# -*- coding: utf-8 -*-
"""QA 第三轮：独立复验
  P1 修法②（>32 拍跳变需连续 2 窗确认）—— 直接 GridClock 单测，覆盖边界。
  P2 hint 与「真实远距离 seek」的交互（engine+hint 全链路，用真实直方图拼接模拟 seek）——
     这是工程师 #4（纯 GridClock，无 hint）没覆盖的：hint 会不会把 seek 后的真值挡在窗外。
  P3 Raccoon Memes 3.78 拍残余 —— 定位到时间轴哪一段。
"""
import json
import os
import random
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from beatgrid import GridClock   # noqa: E402
import fp as FP                   # noqa: E402
import engine as E                # noqa: E402

BL, ANCHOR, SID = 0.5, 0.0, 1
FPS = FP.SAMPLE_RATE / FP.HOP_SIZE
DT = 1.0 / 60.0


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


def line(name, ok, msg):
    print("  [%s] %-34s %s" % ("PASS" if ok else "FAIL", name, msg))
    return ok


# ================================================== P1 修法②
print("=" * 78)
print("P1 修法②：>jump_max(32) 拍跳变须连续 2 窗确认（直接 GridClock）")
print("=" * 78)
res = []


def clk_after(seq):
    """seq=[(off, strong)]，t 步进 0.5s。返回最终 err(拍) 与重锚次数。

    每步 update 后**先**检查 last_pos 是否被清空（=硬重锚）**再**调 pos() 把 last_pos 置回，
    否则 last_pos 恒为 None（update 不设它）会误计。
    """
    c = GridClock()
    re = 0
    t = 0.0
    for i, (off, strong) in enumerate(seq):
        c.update(SID, BL, ANCHOR, off, 1.0, t, strong=strong)
        if i > 0 and c.last_pos is None:
            re += 1
        c.pos(t)
        t += 0.5
    # 残余：最后参考真值 = seq[-1][0]/BL，时钟实际拍位
    ref_last = (seq[-1][0] - ANCHOR) / BL
    return c.pos(t - 0.5) - ref_last, re


def base_jump(n_base, d_beats, last_strong=True, last_weak_idx=None):
    """基线 n_base 窗（off=t 正常前进），末窗注入 +d_beats 拍跳变。"""
    seq = []
    t = 0.0
    for _ in range(n_base):
        seq.append((0.0 + t, True))       # off = t ⇒ ref_pos=t/BL 正常前进（r=1）
        t += 0.5
    seq.append((0.0 + t + d_beats * BL, last_strong))   # err = d_beats
    return seq


# 单窗 +40 拍（强）：不应重锚（有界微调）
e, re = clk_after(base_jump(6, 40.0))
res.append(line("单窗 +40 拍(强)", re == 0 and abs(e) > 30,
                "重锚 %d 次；残余 err=%.2f 拍（应≈+40，不跟随）" % (re, e)))
# 连续 2 窗 +40 拍：应重锚
seq = base_jump(6, 40.0) + [(0.0 + 3.5 + 40.0 * BL, True)]
e, re = clk_after(seq)
res.append(line("连续2窗 +40 拍(强)", re >= 1 and abs(e) < 1.0,
                "重锚 %d 次；残余 err=%.2f 拍（应≈0）" % (re, e)))
# 单窗 +32 拍（≤jump_max）：单帧立即跟随
e, re = clk_after(base_jump(6, 32.0))
res.append(line("单窗 +32 拍(强)", re >= 1 and abs(e) < 1.0,
                "重锚 %d 次；残余 err=%.2f 拍（应≈0）" % (re, e)))
# 单窗 +34 拍（>jump_max 一点）：不跟随
e, re = clk_after(base_jump(6, 34.0))
res.append(line("单窗 +34 拍(强)", re == 0 and abs(e) > 30,
                "重锚 %d 次；残余 err=%.2f 拍（应≈+34）" % (re, e)))
# 连续2窗 但方向相反：不重锚（后窗回到基线）
seq = base_jump(6, 40.0) + [(0.0 + 3.5, True)]
e, re = clk_after(seq)
res.append(line("2窗 +40 后回到基线", re == 0 or e == 0, "重锚 %d 次；残余 %.2f" % (re, e)))
# 弱窗 +20 拍：不单帧重锚
e, re = clk_after(base_jump(6, 20.0, last_strong=False))
res.append(line("单窗 +20 拍(弱)", re == 0, "重锚 %d 次（弱窗不单帧重锚）" % re))
# 孤立强错窗后恢复正常：不移动
e, re = clk_after(base_jump(6, 490.0) + [(0.0 + 3.5 + 490.0 * BL, True),
                                          (0.0 + 4.0, True), (0.0 + 4.5, True),
                                          (0.0 + 5.0, True), (0.0 + 5.5, True)])
res.append(line("孤立强错窗(+490)后恢复", abs(e) < 2.0,
                "残余 err=%.2f 拍（应≈0，孤立错窗不退化为偏移）" % e))

# ================================================== P2 远距离 seek 全链路（含 hint）
print("\n" + "=" * 78)
print("P2 真实远距离 seek 全链路（engine + hint，真实直方图拼接）")
print("=" * 78)
seq_all = json.load(open(os.path.join(ROOT, "tools", "_fix3_hint_seq.json"), encoding="utf-8"))
seq_all = [d for d in seq_all if int(d.get("exp", -1)) >= 0]
BY = {d["song"]: d for d in seq_all}


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


def seek_replay(song, t_seek, use_hint=True):
    """t<t_seek：播放位置 = t；t≥t_seek：seek 到位置 t+JUMP（前向 60s=120 拍）。
    窗内容取自**真实位置**的直方图（即位置 src_t 的窗），候选不做人为平移。"""
    JUMP = 60.0
    wins = song["wins"]
    exp = song["exp"]
    anchor, bl = wins[0]["anchor"], wins[0]["bl"]
    s = make_self()
    errs = []
    by = {w["t"]: w for w in wins}
    t, t_end = 0.0, 85.0
    cur, cur_ref = None, 1000.0
    while t <= t_end + DT:
        src_t = t if t < t_seek else t + JUMP
        true_pos = src_t
        w = by.get(round(src_t, 3))
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
        FT.t = 1000.0 + t
        if cur is not None and cur["sid"] == exp and cur["votes"] > 0:
            off_end = float(cur["off"]) + 4.5
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor, off_end, 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, off_end, int(cur["votes"]), cur_ref), gs)
            errs.append((t, pos - (true_pos + 4.5 - anchor) / bl))
        else:
            s.audio.grid_pred_orig = -1.0
        t += DT
    return errs


def conv_time(errs, t0, thresh=1.0):
    for t, e in errs:
        if t >= t0 - 1e-9 and abs(e) < thresh:
            return t
    return None


for hint_on in (True, False):
    # 用几首已知无早段重复的歌做 seek（避免 t<40 本身是重复段）
    for nm in ("Rolling Contact - Cosmic String.flac",
               "Rolling Contact - Dust.flac",
               "Rolling Contact - Leviathan.flac"):
        d = BY.get(nm)
        if not d:
            continue
        errs = seek_replay(d, 40.0, use_hint=hint_on)
        post = [(t, e) for t, e in errs if t >= 40.0]
        mx = max((abs(e) for _, e in post), default=0.0)
        ct = conv_time(post, 40.0)
        tag = "hint ON " if hint_on else "hint OFF"
        print("  %s %-32s seek后 max|err|=%8.2f 拍；跟上<%d拍 用时 %s"
              % (tag, nm[:32], mx, 1, ("%.1fs" % (ct - 40.0)) if ct is not None else "未跟上"))

# ================================================== P3 Raccoon Memes 残余
print("\n" + "=" * 78)
print("P3 Raccoon Memes 3.78 拍残余定位")
print("=" * 78)
d = BY["Rolling Contact - Raccoon Memes.flac"]


def trace(song):
    wins, exp = song["wins"], song["exp"]
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
            hint_f = s.audio.grid_pred_orig * FPS if s.audio.grid_pred_orig >= 0.0 else None
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
    return errs


errs = trace(d)
big = [(t, e) for t, e in errs if abs(e) > 1.0]
print("  |err|>1 拍的帧数 = %d；时刻范围 = %.1fs ~ %.1fs"
      % (len(big), big[0][0] if big else -1, big[-1][0] if big else -1))
print("  前 12 个 |err|>1 的采样：")
for t, e in big[:12]:
    print("     t=%6.1fs err=%+.2f 拍" % (t, e))
print("  max|err| = %.2f 拍 @ t=%.1fs" % (max((abs(e), t) for t, e in errs)[0] if errs else 0,
                                          max(errs, key=lambda x: abs(x[1]))[0] if errs else 0))
