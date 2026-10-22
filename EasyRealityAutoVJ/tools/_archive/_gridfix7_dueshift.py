# -*- coding: utf-8 -*-
"""临时：核验第六轮交接帧 —— (a) 拍位跳变量(pos-prev_pos) 恒为 0；(b) due 只许往后(≥0)。

复用 _gridfix6_handoff 的 drive 逻辑，但额外记录「锁定帧」的 pos 单步增量。
"""
import importlib.util
import math
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_spec = importlib.util.spec_from_file_location(
    "g6", os.path.join(ROOT, "tools", "_gridfix6_handoff.py"))
G6 = importlib.util.module_from_spec(_spec)
sys.modules["g6"] = G6
_spec.loader.exec_module(G6)

DT = G6.DT
BL = G6.BL


def drive_probe(anchor, t_r, next_due_fn, T=G6.T_END):
    """返回 (lock_t, step_at_lock, due_shift_at_lock)。"""
    s = G6.make_engine(anchor, True, next_due_fn)
    t = 0.0
    lock_t = None
    prev_pos = None
    prev_ingrid = False
    prev_due = None
    step_at_lock = None
    due_shift = None
    while t <= T + 1e-9:
        G6.FT.t = 1000.0 + t
        snap = G6.snap_at(t, t_r)
        pos = s._beat_pos(dict(snap))
        ingrid = s._grid_active_sid is not None
        due = s._next_switch_due(dict(snap))
        if ingrid and not prev_ingrid:
            lock_t = t
            if prev_pos is not None:
                step_at_lock = pos - prev_pos
            if prev_due is not None:
                due_shift = (due - prev_due) - (pos - prev_pos)
        if due is not None:
            prev_due = due
        prev_pos = pos
        prev_ingrid = ingrid
        t += DT
    return lock_t, step_at_lock, due_shift


trs = [round(0.5 * k, 3) for k in range(24)]
anchors = [round(0.4 * k, 3) for k in range(10)]
mx_step = 0.0
n_step_nonzero = 0
due_min = 1e9
due_max = -1e9
n_locked = 0
for anchor in anchors:
    for t_r in trs:
        lt, step, ds = drive_probe(anchor, t_r, G6.NEXT_DUE_NEW)
        if lt is None:
            continue
        n_locked += 1
        if step is not None:
            mx_step = max(mx_step, abs(step))
            if abs(step) > 1e-12:
                n_step_nonzero += 1
        if ds is not None:
            due_min = min(due_min, ds)
            due_max = max(due_max, ds)

print("锁定组合 = %d/240" % n_locked)
print("(a) 交接帧拍位跳变量 |pos-prev_pos|：max=%.3e 拍  非零例数=%d  => %s"
      % (mx_step, n_step_nonzero, "PASS(恒为0)" if n_step_nonzero == 0 else "FAIL"))
print("(b) 交接帧 due_shift：min=%+.3f 拍  max=%+.3f 拍  => %s"
      % (due_min, due_max, "PASS(只许往后)" if due_min >= -1e-9 else "FAIL(有提前)"))
