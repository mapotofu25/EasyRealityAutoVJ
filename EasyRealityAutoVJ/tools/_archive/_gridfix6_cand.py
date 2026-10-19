# -*- coding: utf-8 -*-
"""①候选修法对比：在「锁定交接」重贴 `_next_due` 时，三种规则的切换时刻 / 真实八拍头偏差 /
提前切换 / 该切不切，逐组合对照，用于选定修法（不写入 src，仅评测）。

候选：
  OLD   现状：`near = off + round((due-off)/w)*w`（4→8 时可能贴到 pos 之前 ⇒ 提前切）
  CAND1 宽栅格化：取 >= max(旧目标, pos) 的最近八拍头（绝不提前，最多推迟一个乐句）
  CAND2 team-lead 原式：`off + ceil((pos-off)/w)*w`（pos 之后最近八拍头）

用法： P:\\AutoVJ\\venv\\Scripts\\python.exe tools/_gridfix6_cand.py
"""
import math
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import engine as E  # noqa: E402

DT = 1.0 / 60.0
BL = 0.4
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


def build(src):
    body = _extract_method(src, "_next_switch_due")
    ns = {}
    exec("import math\n" + body, ns)
    return ns["_next_switch_due"]


# CAND1 / CAND2 以「字符串替换」在 OLD body 上改（保证其余逐字一致）
_OLD_TAIL = (
    "        elif off != getattr(self, \"_due_off\", off):\n"
    "            # 对齐相位变了：只有挪动幅度够明显才把已排好的切换点重新贴线，\n"
    "            # 否则保持原目标（细碎的相位更新不再动切换点）\n"
    "            near = off + round((self._next_due - off) / w) * w\n"
    "            if abs(near - self._next_due) >= self.ALIGN_MIN_OFFSET:\n"
    "                self._next_due = near\n"
    "            self._due_off = off\n")

_GREW_C1 = (
    "        elif getattr(self, \"_due_w\", None) is not None and w > self._due_w:\n"
    "            # 4→8 交接：取 >= max(旧目标, pos) 的最近八拍头（绝不提前）。\n"
    "            _p = getattr(self, \"_grid_last_out\", None)\n"
    "            if _p is None:\n"
    "                _p = self.switch_pos\n"
    "            base = self._next_due if self._next_due > _p else _p\n"
    "            self._next_due = off + math.ceil((base - off) / w) * w\n"
    "            while self._next_due <= _p + 1e-9:\n"
    "                self._next_due += w\n"
    "            self._due_off = off\n")

_GREW_C2 = (
    "        elif getattr(self, \"_due_w\", None) is not None and w > self._due_w:\n"
    "            # team-lead 原式：pos 之后最近八拍头。\n"
    "            _p = getattr(self, \"_grid_last_out\", None)\n"
    "            if _p is None:\n"
    "                _p = self.switch_pos\n"
    "            self._next_due = off + math.ceil((_p - off) / w) * w\n"
    "            if self._next_due <= _p + 1e-9:\n"
    "                self._next_due += w\n"
    "            self._due_off = off\n")

_GREW_C3 = (
    "        elif getattr(self, \"_due_w\", None) is not None and w > self._due_w:\n"
    "            # 4→8 交接：先取最近八拍头；若它会**明显**提前（>= ALIGN_MIN_OFFSET）\n"
    "            # 则改取 >= 原目标的最近八拍头（只许往后贴）；再保证在 pos 之后。\n"
    "            _p = getattr(self, \"_grid_last_out\", None)\n"
    "            if _p is None:\n"
    "                _p = self.switch_pos\n"
    "            _t0 = self._next_due\n"
    "            _near = off + round((_t0 - off) / w) * w\n"
    "            if (_t0 - _near) >= self.ALIGN_MIN_OFFSET:\n"
    "                _near = off + math.ceil((_t0 - off) / w) * w\n"
    "            if _near <= _p + 1e-9:\n"
    "                _near = off + math.ceil((_p - off) / w) * w\n"
    "                while _near <= _p + 1e-9:\n"
    "                    _near += w\n"
    "            self._next_due = _near\n"
    "            self._due_off = off\n")

_OLD_TAIL_END = (
    "        if self._next_due <= self.switch_pos:\n"
    "            self._next_due += w   # 对齐后必须前进，避免原地打转\n"
    "        return self._next_due\n")
_NEW_TAIL_END = (
    "        if self._next_due <= self.switch_pos:\n"
    "            self._next_due += w   # 对齐后必须前进，避免原地打转\n"
    "        self._due_w = w\n"
    "        return self._next_due\n")

_OLD_SRC = _read(SNAPSHOT)
CAND = {
    "OLD": _read(SNAPSHOT),
    "CAND1": _OLD_SRC.replace(_OLD_TAIL, _GREW_C1 + _OLD_TAIL),
    "CAND2": _OLD_SRC.replace(_OLD_TAIL, _GREW_C2 + _OLD_TAIL),
    "CAND3": _OLD_SRC.replace(_OLD_TAIL, _GREW_C3 + _OLD_TAIL),
}
FNS = {}
for k, src in CAND.items():
    if k == "OLD":
        FNS[k] = build(src)
    else:
        s2 = src.replace(_OLD_TAIL_END, _NEW_TAIL_END)
        assert s2 != src, "候选 %s tail 替换未命中" % k
        FNS[k] = build(s2)


def make_engine(anchor, beat_grid, nd):
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
    s.cfg = {"auto": {"beat_grid": beat_grid}, "mode": {"sensitivity": 1}}
    s._song_grid_lookup = {SID: {"beat_len": BL, "anchor": anchor, "bpm": 150.0, "src": "vdj"}}
    s._grid_clock = E.GridClock()
    s.audio = types.SimpleNamespace(grid_pred_orig=-1.0, grid_pred_w=None)
    for nm in ("_beat_pos", "_beat_pos_grid", "_grid_observe", "_grid_state",
               "_align_width", "_bar_off", "_bar_floor", "_current_interval",
               "_auto_switch_tick"):
        setattr(s, nm, types.MethodType(getattr(E.AutoVJEngine, nm), s))
    s._next_switch_due = types.MethodType(nd, s)
    s.GRID_MIN_VOTES = E.AutoVJEngine.GRID_MIN_VOTES
    s.GRID_STRONG_VOTES = E.AutoVJEngine.GRID_STRONG_VOTES
    s.GRID_CONSIST_LO = E.AutoVJEngine.GRID_CONSIST_LO
    s.GRID_CONSIST_HI = E.AutoVJEngine.GRID_CONSIST_HI
    s.ALIGN_MIN_OFFSET = E.AutoVJEngine.ALIGN_MIN_OFFSET
    return s


def snap_at(t, t_r):
    bc = int(t / BL)
    ph = t / BL - bc
    bnd = math.floor(t / 0.5 + 1e-9) * 0.5
    if bnd >= t_r - 1e-9:
        sid, off, ref_t, votes = SID, bnd, 1000.0 + bnd, 300
    else:
        sid, off, ref_t, votes = -1, -1.0, 1000.0 + bnd, 0
    return {"bpm": 150.0, "beat_active": True, "beat_count": bc, "beat_phase": ph,
            "downbeat_off": 0.0, "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": sid, "recognized_offset": off, "recognized_rate": 1.0,
            "recognized_t": ref_t, "recognized_votes": votes}


def drive(anchor, t_r, beat_grid, nd, T=T_END):
    s = make_engine(anchor, beat_grid, nd)
    t = 0.0
    lock_t = None
    switches = []
    prev_sw = 0.0
    prev_ingrid = False
    while t <= T + 1e-9:
        FT.t = 1000.0 + t
        snap = snap_at(t, t_r)
        s._beat_pos(dict(snap))
        s._next_switch_due(dict(snap))
        s._auto_switch_tick(dict(snap))
        ingrid = s._grid_active_sid is not None
        if s.switch_pos != prev_sw:
            # 切换落点真实歌曲秒 = 该帧的 song 时间；偏差 = 到最近真实八拍头
            switches.append((t, s.switch_pos, (((t - anchor) / BL) % 8.0 + 4.0) % 8.0 - 4.0))
            prev_sw = s.switch_pos
        if ingrid and not prev_ingrid:
            lock_t = t
        prev_ingrid = ingrid
        t += DT
    return lock_t, switches


LOCK_WIN = 0.5
IMM = 2 * DT


def evaluate(name):
    nd = FNS[name]
    trs = [round(0.5 * k, 3) for k in range(24)]
    anchors = [round(0.4 * k, 3) for k in range(10)]
    prem = imm = 0
    delays = []
    biases = []
    max_bias_lock = 0.0
    for anchor in anchors:
        for t_r in trs:
            ltg, swg = drive(anchor, t_r, True, nd)
            ltp, swp = drive(anchor, t_r, False, nd)
            if ltg is None:
                continue
            gwin = [s for s in swg if ltg - 1e-9 <= s[0] <= ltg + LOCK_WIN + 1e-9]
            pwin = [s for s in swp if ltg - 1e-9 <= s[0] <= ltg + LOCK_WIN + 1e-9]
            if gwin and not pwin:
                prem += 1
            if any((s[0] - ltg) <= IMM + 1e-9 for s in gwin):
                imm += 1
            # 延迟 / 偏差
            tg = next((s[0] for s in swg if s[0] >= ltg - 1e-9), None)
            tp = next((s[0] for s in swp if s[0] >= ltg - 1e-9), None)
            if tg and tp:
                delays.append((tg - tp) / BL)
            for (st, sp, bias) in swg:
                biases.append(abs(bias))
                if st <= ltg + LOCK_WIN:
                    max_bias_lock = max(max_bias_lock, abs(bias))
    return {"prem": prem, "imm": imm,
            "max_delay": max(delays) if delays else 0.0,
            "min_delay": min(delays) if delays else 0.0,
            "bias_med": sorted(biases)[len(biases) // 2] if biases else 0.0,
            "bias_p90": sorted(biases)[int(len(biases) * 0.9)] if biases else 0.0,
            "max_bias_lock": max_bias_lock}


print("=" * 92)
print("①候选修法对比（240 组合；LOCK_WIN=%.1fs；IMM=%.3fs）" % (LOCK_WIN, IMM))
print("=" * 92)
print("  %-7s %10s %10s %11s %11s %10s %10s %10s" %
      ("规则", "提前(0.5s)", "立刻(2帧)", "最大推迟拍", "最小推迟拍",
       "偏差中位", "偏差p90", "锁定窗内max偏"))
for k in ("OLD", "CAND1", "CAND2", "CAND3"):
    r = evaluate(k)
    print("  %-7s %10d %10d %11.2f %11.2f %10.2f %10.2f %10.2f" %
          (k, r["prem"], r["imm"], r["max_delay"], r["min_delay"],
           r["bias_med"], r["bias_p90"], r["max_bias_lock"]))

# 逐组合切换时刻对比（挑 OLD 出现提前的组合）
print("\n" + "=" * 92)
print("逐组合首次切换（锁定后第一个切换的 song 秒 / 到最近真实八拍头偏差 拍）")
print("=" * 92)
trs = [round(0.5 * k, 3) for k in range(24)]
anchors = [round(0.4 * k, 3) for k in range(10)]
print("  %-7s %-5s | %-21s | %-21s | %-21s | %-21s" %
      ("anchor", "t_r", "OLD", "CAND1", "CAND2", "CAND3"))
shown = 0
for anchor in anchors:
    for t_r in trs:
        row = {}
        for k in ("OLD", "CAND1", "CAND2", "CAND3"):
            ltg, swg = drive(anchor, t_r, True, FNS[k])
            ltp, swp = drive(anchor, t_r, False, FNS[k])
            tg = next((s for s in swg if s[0] >= ltg - 1e-9), None) if ltg else None
            tp = next((s for s in swp if s[0] >= ltg - 1e-9), None) if ltg else None
            row[k] = (ltg, tg, tp)
        ltg, tg_o, tp_o = row["OLD"]
        if ltg is None:
            continue
        gwin = [s for s in drive(anchor, t_r, True, FNS["OLD"])[1]
                if ltg - 1e-9 <= s[0] <= ltg + LOCK_WIN + 1e-9]
        pwin = [s for s in drive(anchor, t_r, False, FNS["OLD"])[1]
                if ltg - 1e-9 <= s[0] <= ltg + LOCK_WIN + 1e-9]
        flag = " ←OLD提前" if (gwin and not pwin) else ""
        if flag or shown < 5:
            def fmt(v):
                if v is None:
                    return "-"
                return "t=%.2f b=%+.2f" % (v[0], v[2])
            print("  %-7.1f %-5.1f | %-21s | %-21s | %-21s | %-21s%s" %
                  (anchor, t_r, fmt(row["OLD"][1]), fmt(row["CAND1"][1]),
                   fmt(row["CAND2"][1]), fmt(row["CAND3"][1]), flag))
            shown += 1
