# -*- coding: utf-8 -*-
"""H2 变体对照（临时实验）：OLD / NEW / 未锁定临时hint 仅当 prov 由强窗建立（GATE）。"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
spec = importlib.util.spec_from_file_location("g4", os.path.join(ROOT, "tools", "_gridfix4_accept.py"))
g4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g4)
E, FP = g4.E, g4.FP

PROV_CREATE = ("            if pp is None or pp[0] != sid:\n"
               "                self._grid_prov = (sid, off, ref_t)\n")
PROV_CREATE_GATE = PROV_CREATE + "                self._grid_prov_strong = bool(strong)\n"

GATE_PUB = ("            if getattr(self, \"_grid_prov_strong\", False):\n"
            "                _publish_grid_hint(self, anchor + pos * bl - 4.5 * r,"
            " hint_window_frames(bl))\n"
            "            else:\n"
            "                _publish_grid_hint(self, -1.0)\n"
            "            return pos\n")


def build_gate():
    body = g4._extract_method(g4._engine_src(), "_beat_pos_grid")
    assert body.count(PROV_CREATE) == 1
    body = body.replace(PROV_CREATE, PROV_CREATE_GATE, 1)
    assert body.count(g4._UNLOCKED_OLD) == 1
    body = body.replace(g4._UNLOCKED_OLD, GATE_PUB, 1)
    ns = {}
    exec("import math\n" + body, ns)
    ns["time"] = E.time
    ns["_publish_grid_hint"] = E._publish_grid_hint
    ns["hint_window_frames"] = FP.hint_window_frames
    return ns["_beat_pos_grid"]


def stats(data, starts, pol, label):
    tot = 0.0
    mx = 0.0
    nbad = 0
    worst = []
    for d in data:
        for st in starts:
            e, _ = g4.replay(pol, d["wins"], d["exp"], d["bl"], d["anchor"], st)
            bd = g4.bad_dur(e)
            tot += bd
            mx = max(mx, bd)
            if bd > 5.0:
                nbad += 1
            worst.append((bd, d["song"], st))
    worst.sort(reverse=True)
    print("%-20s 总误锁=%5.0fs 最长=%3.0fs #(>5s)=%2d | %s" % (
        label, tot, mx, nbad,
        "; ".join("%.0fs:%s@%d" % (b, s.split(" - ")[-1][:14], st) for b, s, st in worst[:4])))


def main():
    data = g4.load()
    starts = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    stats(data, starts, g4.POLICY_OLD, "OLD 临时hint")
    stats(data, starts, g4.POLICY_NEW, "NEW 无hint(现役)")
    stats(data, starts, build_gate(), "GATE(强窗prov)")


if __name__ == "__main__":
    main()
