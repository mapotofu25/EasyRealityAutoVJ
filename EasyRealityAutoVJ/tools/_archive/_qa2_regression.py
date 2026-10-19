# -*- coding: utf-8 -*-
"""QA 第二轮：零回归独立复验（★ D）。
用 git show HEAD 的**改动前**实现，与当前实现搭对照：同一受控时钟 + 同一输入流，
逐值比较 _beat_pos / _bar_off / _bar_floor / _next_switch_due / switch_countdown。
控制 clock 用假 perf_counter，确保两套实现看到完全相同的时间。"""
import sys, os, math, types, subprocess

SRC = r"P:\AutoVJ\src"
sys.path.insert(0, SRC)
sys.path.insert(0, r"P:\AutoVJ")
import engine as E

# ---------------- 受控时钟 ----------------
class _Clock:
    t = 1000.0
def _fake_perf():
    return _Clock.t
# engine 里 `import time` 后用 time.perf_counter()；替换为受控对象
E.time = types.SimpleNamespace(perf_counter=_fake_perf, monotonic=_fake_perf)

MODE_META = E.MODE_META

# ---------------- 改动前(old)实现：从 git HEAD 源里抽取 ----------------
def _old_src():
    out = subprocess.check_output(
        ["git", "-C", r"P:\AutoVJ", "show", "HEAD:src/engine.py"]).decode("utf-8")
    return out

_OLD = _old_src()

def _extract_method(src, name):
    """从源码文本里抠出 `def name(self...` 到下一个同级 def 之前的整段。"""
    key = "    def %s(" % name
    i = src.index(key)
    j = src.find("\n    def ", i + 1)
    seg = src[i:j] if j != -1 else src[i:]
    seg = seg.rstrip() + "\n"
    # 去掉缩进，变成模块级函数
    func = "_old%s" % name          # name 已带前导下划线 -> "_old_beat_pos"
    lines = seg.splitlines()
    body = "\n".join(l[4:] if l.startswith("    ") else l for l in lines)
    ns = {}
    exec("import math, time\n" + body.replace("def %s(" % name, "def %s(" % func, 1), ns)
    ns["time"] = E.time          # ★ 注入受控时钟（否则抽出的函数用真实挂钟）
    return ns[func]

_old_beat_pos = _extract_method(_OLD, "_beat_pos")
_old_bar_off = _extract_method(_OLD, "_bar_off")
_old_bar_floor = _extract_method(_OLD, "_bar_floor")
_old_next_due = _extract_method(_OLD, "_next_switch_due")

# 验证抽取成功（源码里确实存在这些行）
assert "return ref[1] + (now - ref[0]) * (120.0 / 60.0)" in _OLD


class Dummy:
    pass


def make_new(cfg):
    d = Dummy()
    d.cfg = cfg
    d._song_grid_lookup = getattr(make_new, "_grids", {})
    d._grid_clock = E.GridClock()
    d._grid_active_sid = None
    d._grid_coord_delta = 0.0
    d._grid_last_pos = 0.0
    d._pos_ref = None
    d.switch_pos = 0.0
    d._next_due = None
    d._due_off = 0.0
    d._bar_off_user = None
    d.mode = "normal"
    d.ALIGN_MIN_OFFSET = 1.0
    C = E.AutoVJEngine
    d._beat_pos = types.MethodType(C._beat_pos, d)
    d._beat_pos_grid = types.MethodType(C._beat_pos_grid, d)
    d._grid_observe = types.MethodType(C._grid_observe, d)
    d._grid_state = types.MethodType(C._grid_state, d)
    d._align_width = types.MethodType(C._align_width, d)
    d._bar_off = types.MethodType(C._bar_off, d)
    d._bar_floor = types.MethodType(C._bar_floor, d)
    d._next_switch_due = types.MethodType(C._next_switch_due, d)
    d._current_interval = types.MethodType(C._current_interval, d)
    d.beats_to_next_switch = types.MethodType(C.beats_to_next_switch, d)
    d.switch_countdown = types.MethodType(C.switch_countdown, d)
    return d


def make_old(cfg):
    d = Dummy()
    d.cfg = cfg
    d._bar_off_user = None
    d._pos_ref = None
    d.switch_pos = 0.0
    d._next_due = None
    d._due_off = 0.0
    d.mode = "normal"
    d.ALIGN_MIN_OFFSET = 1.0
    d.ALIGN_MIN_OFFSET = 1.0
    d._beat_pos = types.MethodType(_old_beat_pos, d)
    d._bar_off = types.MethodType(_old_bar_off, d)
    d._bar_floor = types.MethodType(_old_bar_floor, d)
    d._next_switch_due = types.MethodType(_old_next_due, d)
    d._current_interval = types.MethodType(E.AutoVJEngine._current_interval, d)
    d.beats_to_next_switch = types.MethodType(E.AutoVJEngine.beats_to_next_switch, d)
    d.switch_countdown = types.MethodType(E.AutoVJEngine.switch_countdown, d)
    return d


def gen_stream(n=600, dt=1/60.0, bpm=128.0, downbeat_off=0.0, beat_active=True,
               has_grid=False, recognized=False, drop_at=None):
    """生成受控快照流（每帧推进挂钟 dt）。"""
    be = 60.0 / bpm
    if has_grid:
        grid = {"bpm": bpm, "beat_len": be, "anchor": 1.0, "conf": 0.9, "src": "vdj"}
        make_new._grids = {1234: grid}
    else:
        make_new._grids = {}
    out = []
    for i in range(n):
        t = i * dt
        count = int(t / be)
        phase = (t / be) - count
        snap = {
            "bpm": bpm, "beat_active": beat_active,
            "beat_count": count, "beat_phase": phase,
            "downbeat_off": downbeat_off,
            "energy": 0.5, "energy_trend": 0.0,
            "recognized_song_id": (1234 if recognized else -1),
            "recognized_offset": (1.0 + t if recognized else -1.0),
            "recognized_rate": 1.0,
            "recognized_t": _Clock.t,
            "recognized_votes": 300,
        }
        out.append((t, snap))
    return out


def run_case(name, cfg, stream, manual_press_at=None):
    new = make_new(cfg)
    old = make_old(cfg)
    diffs = []
    for i, (t, snap) in enumerate(stream):
        _Clock.t = 1000.0 + t
        snap = dict(snap)  # 各自独立
        if manual_press_at is not None and i == manual_press_at:
            # 模拟按下「下一素材」：两套各自按自己的规则设 _bar_off_user
            beat = math.floor(new._beat_pos(dict(snap)))
            new._bar_off_user = float(beat)
            old._bar_off_user = beat % 4
            new._next_due = None; old._next_due = None
            new._due_off = new._bar_off(dict(snap)); old._due_off = old._bar_off(dict(snap))
        n_bp = new._beat_pos(dict(snap)); o_bp = old._beat_pos(dict(snap))
        n_bo = new._bar_off(dict(snap)); o_bo = old._bar_off(dict(snap))
        n_bf = new._bar_floor(n_bp, dict(snap)); o_bf = old._bar_floor(o_bp, dict(snap))
        n_nd = new._next_switch_due(dict(snap)); o_nd = old._next_switch_due(dict(snap))
        n_sc = new.switch_countdown(dict(snap)); o_sc = old.switch_countdown(dict(snap))
        for label, a, b in (("beat_pos", n_bp, o_bp), ("bar_off", n_bo, o_bo),
                            ("bar_floor", n_bf, o_bf), ("next_due", n_nd, o_nd),
                            ("countdown", n_sc, o_sc)):
            if a is None or b is None:
                ok = (a is None and b is None)
                if not ok:
                    diffs.append((i, label, a, b))
            elif abs(a - b) > 1e-9:
                diffs.append((i, label, a, b))
    status = "一致" if not diffs else ("不一致(%d)" % len(diffs))
    print("[%s] %-46s %s" % ("OK " if not diffs else "XX ", name, status))
    for d in diffs[:4]:
        print("       frm=%d %s new=%r old=%r" % d)
    return not diffs


CFG_ON = lambda g: {"auto": {"beat_grid": g}, "mode": {"sensitivity": 1}}

print("=== 零回归对照（old vs new，逐值）===")
res = []
res.append(run_case("① 开关关 beat_grid=False + 有网格", CFG_ON(False),
                    gen_stream(has_grid=True, recognized=True)))
res.append(run_case("② 开关开 + 无网格未识别", CFG_ON(True), gen_stream()))
res.append(run_case("③ 开关开 + 有网格但未识别", CFG_ON(True),
                    gen_stream(has_grid=True, recognized=False)))
res.append(run_case("④ 非零 downbeat_off=1.7", CFG_ON(True),
                    gen_stream(downbeat_off=1.7)))
res.append(run_case("⑤ 无鼓点(beat_active=False) 回退时钟", CFG_ON(True),
                    gen_stream(beat_active=False)))
res.append(run_case("⑥ 手动按下 next_scene 后", CFG_ON(True),
                    gen_stream(downbeat_off=1.0), manual_press_at=30))
res.append(run_case("⑦ 关+有网格+非零downbeat", CFG_ON(False),
                    gen_stream(downbeat_off=2.3, has_grid=True, recognized=True)))
print("=== 合计：%d/%d 场景逐值一致 ===" % (sum(res), len(res)))
