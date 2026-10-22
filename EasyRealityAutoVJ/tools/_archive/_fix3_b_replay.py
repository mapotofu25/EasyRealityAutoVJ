# -*- coding: utf-8 -*-
"""第三轮 ★B 端到端重放：把真实素材的逐窗识别结果（votes/off/sid）喂给**现役引擎的
`_beat_pos_grid`**，测「网格拍位」相对真实歌曲位置的误差，量化坏窗/重复段落对演出拍位的影响。

数据来自 tools/_fix3_votes_cover.py 落盘的 tools/_fix3_votes_seq.json（只读素材+临时指纹库）。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_fix3_b_replay.py
"""
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import engine as E  # noqa: E402

SEQ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fix3_votes_seq.json")
DT = 1.0 / 60.0


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
    for nm in ("_beat_pos_grid",):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    return s


def snap(sid, off, votes, ref_t):
    return {"bpm": 128.0, "beat_active": True, "beat_count": 0, "beat_phase": 0.0,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def replay(song):
    """把一首歌的逐窗喂进引擎。仅统计**网格实际生效**的帧（当前窗命中本歌且 votes>0 且
    off 合法）；命错歌 / 未识别时生产里 `_grid_state` 返回 None → 走音频模式，网格时钟
    不被查询，故这些帧不计入网格误差。返回 (max|err|, err 列表, 网格帧数)。"""
    wins = song["wins"]
    exp = song["exp"]
    anchor = wins[0]["anchor"]
    bl = wins[0]["bl"]
    s = make_self()
    errs = []
    ngrid = 0
    if not wins:
        return 0.0, errs, 0
    win_by_start = {w["t"]: w for w in wins}
    t = 0.0
    t_end = wins[-1]["t"]
    cur = None
    cur_ref = 1000.0     # ★ 识别参考时刻：只在**出新窗**时更新（真实链路 recognized_t
                         #   每 0.5s 才变；与它同步，别每帧都给新时间戳）
    while t <= t_end + DT:
        w = win_by_start.get(round(t, 3))
        if w is not None:
            cur = w
            cur_ref = 1000.0 + t          # 该窗的挂钟参考时刻
        FT.t = 1000.0 + t
        grid_on = (cur is not None and cur["sid"] == exp and cur["off"] is not None
                   and cur["votes"] > 0)
        if grid_on:
            gs = ({"beat_len": bl, "anchor": anchor}, bl, anchor,
                  float(cur["off"]), 1.0, exp)
            pos = s._beat_pos_grid(snap(exp, float(cur["off"]), int(cur["votes"]),
                                        cur_ref), gs)
            errs.append((t, pos - (t - anchor) / bl))
            ngrid += 1
        t += DT
    mx = max((abs(e) for _, e in errs), default=0.0)
    return mx, errs, ngrid


data = json.load(open(SEQ, encoding="utf-8"))
# 只重放「在 config 里有 song_id（exp>=0）」的歌：exp<0 无法核对真实位置，跳过。
data = [d for d in data if int(d.get("exp", -1)) >= 0]
print("载入 %d 首可核对的逐窗序列（config 内命中）" % len(data))
print("=" * 78)
print("★B 端到端：现役引擎 _beat_pos_grid 重放真实识别序列，测网格拍位误差（相对真实位置）")
print("=" * 78)
import statistics
per = []
allmax = 0.0
allbad = []
for song in data:
    try:
        mx, errs, ngrid = replay(song)
    except Exception as e:
        print("  %-40s 重放异常：%s" % (song["song"][:40], e))
        continue
    if not errs:
        continue
    bad = sum(1 for _, e in errs if abs(e) > 1.0) / max(1, len(errs))
    per.append((song["song"], mx, bad, ngrid))
    allmax = max(allmax, mx)
    allbad.append(bad)
per.sort(key=lambda r: -r[1])
print("各首网格拍位误差（仅网格生效帧，按 max|err| 排序）：")
for nm, mx, bad, ng in per[:20]:
    print("  %-42s max|err|=%.2f 拍；|err|>1 拍占比 %.1f%%（网格帧 %d）" % (nm[:42], mx, 100 * bad, ng))
print("  ...（其余 %d 首）" % max(0, len(per) - 8))
print()
print("总体：%d 首；max|err| = %.2f 拍；|err|>1 拍的时间占比 中位 %.1f%% / 均值 %.1f%%"
      % (len(per), allmax, 100 * statistics.median(allbad),
         100 * (sum(allbad) / max(1, len(allbad)))))
print()
print("⚠ 注意：本脚本按**既有设计**把「已算好的识别结果(全局众数 offset)」直接喂进引擎，")
print("   因此**绕过 fp.match**，无法体现「连续性提示(hint_delta)」的修复（fix①）。")
print("   带 hint 的端到端重放（模拟 识别→fp.match(hint)→引擎）见：")
print("     python tools/_fix3_d_hint.py      # 20 首，改动前后对照，实测 max|err| 由 738→<4")
