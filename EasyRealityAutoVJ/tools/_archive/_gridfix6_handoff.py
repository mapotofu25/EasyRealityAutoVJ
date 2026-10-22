# -*- coding: utf-8 -*-
"""第六轮验收 ①：锁定交接「提前/额外切换」扫描。

问题（team-lead 明确）：240 个（锁定时刻 × anchor）组合里，部分组合在**锁定帧**出现
「网格模式有、纯 4 拍模式无」的**提前/额外切换**（锁定→下次切换最短 0.000s，太早）。
根因：4→8 栅格宽度切换把已排好的 `_next_due` 重贴到更粗栅格，可能重贴到当前 pos **之前**。

本脚本：
  · 以 OLD（快照 tools/_engine_fix6_snapshot.py 的 `_next_switch_due`）与
    NEW（现役 src/engine.py 的 `_next_switch_due`）两版驱动**同一** 240 组合；
  · 每组同时跑「网格模式」（会锁定，w 4→8）与「纯 4 拍对照」（beat_grid=False，永不锁定）；
  · 统计「锁定帧的提前/额外切换」= 网格模式在锁定后极短窗口内发生、而纯 4 拍在同时刻
    没有的切换；并检查「该切不切」（网格首切比纯 4 拍推迟 > 一个乐句 = 8 拍）。

音乐模型：bl=0.4s（150BPM），真实歌曲时间 t；识别每 0.5s 出新窗。
  · 锁定时刻由「识别起始 t_r」决定：第 1 个可信窗暂存、第 2 个（t_r+0.5）锁定。
  · t_r ∈ {0.0,0.5,…,11.5}（24）× anchor ∈ {0.0,0.4,…,3.6}（10）= 240 组合。

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix6_handoff.py
"""
import math
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import engine as E  # noqa: E402

DT = 1.0 / 60.0
BL = 0.4            # 150 BPM
SID = 7
T_END = 30.0
SNAPSHOT = os.path.join(ROOT, "tools", "_engine_fix6_snapshot.py")


class FT:
    t = 0.0

    @staticmethod
    def perf_counter():
        return FT.t


E.time = FT


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _extract_method(src, name):
    key = "    def %s(" % name
    i = src.index(key)
    j = src.find("\n    def ", i + 1)
    seg = src[i:j] if j != -1 else src[i:]
    lines = seg.rstrip().splitlines()
    return "\n".join(l[4:] if l.startswith("    ") else l for l in lines) + "\n"


def build_next_due(src):
    """从源码字符串抽 `_next_switch_due`，编译成独立函数。签名 (self, snap)。"""
    body = _extract_method(src, "_next_switch_due")
    ns = {}
    exec("import math\n" + body, ns)
    return ns["_next_switch_due"]


NEXT_DUE_OLD = build_next_due(_read(SNAPSHOT))
NEXT_DUE_NEW = build_next_due(_read(os.path.join(ROOT, "src", "engine.py")))


def make_engine(anchor, beat_grid, next_due_fn):
    s = types.SimpleNamespace()
    s._pos_ref = None
    s.switch_pos = 0.0
    s._next_due = None
    s._due_off = 0.0
    s._due_w = None
    s._bar_off_user = None
    s.mode = "normal"
    s.locked = False
    s.layers = []
    s.cfg = {"auto": {"beat_grid": beat_grid}, "mode": {"sensitivity": 1}}
    s._song_grid_lookup = {SID: {"beat_len": BL, "anchor": anchor, "bpm": 150.0, "src": "vdj"}}
    s._grid_clock = E.GridClock()
    s._grid_active_sid = None
    s._grid_coord_delta = 0.0
    s._grid_last_pos = 0.0
    s._grid_last_out = None
    s._grid_pending = None
    s._grid_seen = None
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state",
               "_align_width", "_bar_off", "_bar_floor", "_current_interval",
               "_auto_switch_tick", "switch_countdown", "beats_to_next_switch"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s._next_switch_due = types.MethodType(next_due_fn, s)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    s.ALIGN_MIN_OFFSET = E.AutoVJEngine.ALIGN_MIN_OFFSET
    return s


def snap_at(t, t_r):
    bc = int(t / BL)
    ph = (t / BL) - bc
    bnd = math.floor(t / 0.5 + 1e-9) * 0.5
    if bnd >= t_r - 1e-9:
        sid, off, ref_t, votes = SID, bnd, 1000.0 + bnd, 300
    else:
        sid, off, ref_t, votes = -1, -1.0, 1000.0 + bnd, 0
    return {"bpm": 150.0, "beat_active": True, "beat_count": bc, "beat_phase": ph,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def drive(anchor, t_r, beat_grid, next_due_fn, T=T_END):
    """返回 (lock_t, switches[(t, pos)], due_shift_at_lock)。"""
    s = make_engine(anchor, beat_grid, next_due_fn)
    t = 0.0
    lock_t = None
    switches = []
    prev_sw = 0.0
    prev_ingrid = False
    prev_due = None
    due_shift = None
    prev_pos = None
    while t <= T + 1e-9:
        FT.t = 1000.0 + t
        snap = snap_at(t, t_r)
        pos = s._beat_pos(dict(snap))
        ingrid = s._grid_active_sid is not None
        due = s._next_switch_due(dict(snap))
        if (ingrid and not prev_ingrid) and prev_due is not None and pos is not None:
            due_shift = (due - prev_due) - (pos - prev_pos)
        if due is not None:
            prev_due = due
        prev_pos = pos
        s._auto_switch_tick(dict(snap))
        if s.switch_pos != prev_sw:
            switches.append((t, s.switch_pos))
            prev_sw = s.switch_pos
        if ingrid and not prev_ingrid:
            lock_t = t
        prev_ingrid = ingrid
        t += DT
    return lock_t, switches, due_shift


# ------------------------------------------------------------------ 判定
# 「锁定帧的提前/额外切换」定义（team-lead 口径）：
#   网格模式在 [t_lock, t_lock + LOCK_WIN] 内发生了一次切换，而**纯 4 拍**在同一窗口内
#   没有任何切换。LOCK_WIN 取 1 个识别窗（0.5s）—— 覆盖「锁定帧立刻切」以及「锁定后
#   极短时间就切」两种表现。
LOCK_WIN = 0.5
PHRASE = 8.0 * BL      # 一个乐句（8 拍）的秒数


def first_after(switches, t0):
    for (t, sp) in switches:
        if t >= t0 - 1e-9:
            return t
    return None


def count_in(switches, t0, t1):
    return [ (t, sp) for (t, sp) in switches if t0 - 1e-9 <= t <= t1 + 1e-9 ]


def scan(next_due_fn, tag):
    trs = [round(0.5 * k, 3) for k in range(24)]
    anchors = [round(0.4 * k, 3) for k in range(10)]
    n_total = 0
    n_premature = 0
    n_immediate = 0
    n_extra_total = 0
    n_delayed = 0
    max_delay_beats = 0.0
    rows = []
    for anchor in anchors:
        for t_r in trs:
            n_total += 1
            lt_g, sw_g, ds = drive(anchor, t_r, True, next_due_fn)
            lt_p, sw_p, _ = drive(anchor, t_r, False, next_due_fn)
            if lt_g is None:
                rows.append((anchor, t_r, None, None, None, "未锁定"))
                continue
            g_win = count_in(sw_g, lt_g, lt_g + LOCK_WIN)
            p_win = count_in(sw_p, lt_g, lt_g + LOCK_WIN)
            prem = (len(g_win) > 0 and len(p_win) == 0)
            # 「立刻切」= 锁定帧本身（±1 帧）
            imm = any((t - lt_g) <= DT + 1e-9 for (t, _sp) in g_win)
            # 「该切不切」：网格首切比纯 4 拍首切**推迟 > 一个乐句**
            tg = first_after(sw_g, lt_g)
            tp = first_after(sw_p, lt_g)
            delay_beats = None
            delayed = False
            if tg is not None and tp is not None:
                delay_beats = (tg - tp) / BL
                max_delay_beats = max(max_delay_beats, delay_beats)
                if delay_beats > 8.0 + 1e-9:
                    delayed = True
            elif tg is None and tp is not None:
                delayed = True
            # 额外切换：网格在锁定后 LOCK_WIN 内比纯 4 拍多的切换数
            extra = max(0, len(g_win) - len(p_win))
            n_premature += 1 if prem else 0
            n_immediate += 1 if imm else 0
            n_extra_total += extra
            n_delayed += 1 if delayed else 0
            rows.append((anchor, t_r, lt_g, (tg - lt_g) if tg else None,
                         delay_beats, "提前" if prem else ("推迟" if delayed else "")))
    print("=" * 84)
    print("【%s】240 组合扫描" % tag)
    print("=" * 84)
    print("  组合数              = %d" % n_total)
    print("  锁定帧提前/额外切换  = %d  (网格在 [lock, lock+%.1fs] 有切、纯 4 拍无)"
          % (n_premature, LOCK_WIN))
    print("    其中「锁定帧即切」  = %d  (lock→首切 = 0.000s)" % n_immediate)
    print("  额外切换总数        = %d" % n_extra_total)
    print("  该切不切(推迟>乐句)  = %d  (最大推迟 %+.2f 拍 = %+.2f 个乐句)"
          % (n_delayed, max_delay_beats, max_delay_beats / 8.0))
    bad = [r for r in rows if r[5] == "提前"]
    if bad:
        print("  —— 提前/额外样例（anchor / t_r / lock_t / lock→首切 s）——")
        for r in bad[:40]:
            print("     anchor=%4.1fs t_r=%4.1f lock_t=%6.2f lock→首切=%s s"
                  % (r[0], r[1], r[2], ("%.3f" % r[3]) if r[3] is not None else "-"))
    return {"premature": n_premature, "immediate": n_immediate, "extra": n_extra_total,
            "delayed": n_delayed, "max_delay_beats": max_delay_beats, "rows": rows}


def main():
    print("音乐模型：bl=%.2fs（150BPM）；240 = 24 个锁定时刻(t_r∈0..11.5s) × 10 个 anchor(0..3.6s)")
    ro = scan(NEXT_DUE_OLD, "OLD（快照：现役改动前）")
    rn = scan(NEXT_DUE_NEW, "NEW（现役 src/engine.py）")

    print("\n" + "=" * 84)
    print("【修前/修后 切换时刻对照】逐组合：OLD 首次切换 vs NEW 首次切换（锁定后，拍）")
    print("=" * 84)
    print("  %-8s %-6s | %-28s | %-28s" % ("anchor", "t_r", "OLD 网格/纯4拍 首切(拍)",
                                          "NEW 网格/纯4拍 首切(拍)"))
    shown = 0
    for a, b in zip(ro["rows"], rn["rows"]):
        anchor, t_r = a[0], a[1]
        lt = a[2]
        if lt is None:
            continue
        # 重新算首切拍位
        _, swg_o, _ = drive(anchor, t_r, True, NEXT_DUE_OLD)
        _, swp_o, _ = drive(anchor, t_r, False, NEXT_DUE_OLD)
        _, swg_n, _ = drive(anchor, t_r, True, NEXT_DUE_NEW)
        _, swp_n, _ = drive(anchor, t_r, False, NEXT_DUE_NEW)
        f = lambda sw: first_after(sw, lt)
        go = f(swg_o); po = f(swp_o); gn = f(swg_n); pn = f(swp_n)
        mark = "  ←" if a[5] == "提前" else ""
        if mark or shown < 6:
            print("  %-8.1f %-6.1f | grid=%-8s pure=%-8s | grid=%-8s pure=%-8s%s"
                  % (anchor, t_r,
                     ("%.2f" % go) if go else "-", ("%.2f" % po) if po else "-",
                     ("%.2f" % gn) if gn else "-", ("%.2f" % pn) if pn else "-", mark))
            shown += 1
    print("\n结论：OLD 提前/额外切换 = %d 例；NEW = %d 例（验收要求 NEW = 0）"
          % (ro["premature"], rn["premature"]))
    print("      NEW 该切不切 = %d 例；最大推迟 = %.2f 拍（< 8 拍一个乐句）"
          % (rn["delayed"], rn["max_delay_beats"]))


if __name__ == "__main__":
    main()
