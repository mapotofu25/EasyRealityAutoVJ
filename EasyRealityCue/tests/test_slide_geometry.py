# -*- coding: utf-8 -*-
"""预览端 slide 几何（用户 2026-11-02 报障的编排端一半）。

报障现象（演出端 ERAVJ）：缩小过的图"滑动消失"时从**屏幕最右边**滑，而不是从原位。
根因：位移量用了**画布宽 `w`**，且入场/出场共用一个正方向。
已在 `AutoVJ/src/engine.py::_draw_layer` 改成"位移量 = 素材自身显示宽度 tw + 出场反向"，
本文件守住编排端预览（`render/compositor._target_src`）与它一致 ⇒ 契约②。

跑法：
    set QT_QPA_PLATFORM=offscreen
    venv\\Scripts\\python.exe -u tests\\test_slide_geometry.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtGui import QGuiApplication                        # noqa: E402

from ercue.model import timeline as tl                          # noqa: E402
from ercue.render import compositor as cp                        # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])

W, H = 1920, 1080
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def st(trans="slide", tp=0.0, is_out=False, scale=0.28):
    s = tl.LayerDrawState(lyr_id="L", z=0)
    s.trans = trans
    s.trans_p = tp
    s.trans_is_out = is_out
    s.xform = {"scale": scale, "x": 0.0, "y": 0.0}
    s.crop = None
    return s


def geom(state, fw=200, fh=200):
    tgt, src = cp._target_src(state, fw, fh, W, H)
    return tgt, src


def main():
    print("=" * 74)
    print("【A】缩小素材（scale=0.28）：slide 位移量必须是**素材自身宽度**")
    print("=" * 74)
    t1, _ = geom(st(tp=1.0))
    own = t1.width()
    check(abs(own - 302) < 6, "(a1) 素材显示宽度 ≈302px（不是 1920）", round(own, 1))

    t0, _ = geom(st(tp=0.0, is_out=False))
    check(abs((t0.x() - t1.x()) - own) < 6,
          "(a2) 入场 tp=0 ⇒ 只偏移**自身宽度**", round(t0.x() - t1.x(), 1))
    check(t0.x() < W, "(a3) 入场起点仍在屏幕内（不跑到 1920 外）", round(t0.x(), 1))

    print()
    print("=" * 74)
    print("【B】出场必须**反向**（原来入出场共用 +dx，出场也演成从右边滑进来）")
    print("=" * 74)
    o0, _ = geom(st(tp=0.0, is_out=True))
    check(abs((o0.x() - t1.x()) + own) < 6,
          "(b1) 出场 tp=0 ⇒ 向**左**偏移自身宽度", round(o0.x() - t1.x(), 1))
    check(o0.x() < t1.x(), "(b2) 出场终点在原位左侧", round(o0.x(), 1))
    o1, _ = geom(st(tp=1.0, is_out=True))
    check(abs(o1.x() - t1.x()) < 1, "(b3) 出场 tp=1 从原位开始（不跳动）", round(o1.x(), 1))

    print()
    print("=" * 74)
    print("【C】默认 xform（cover）：位移量恰等于 w ⇒ 与旧行为逐像素一致")
    print("=" * 74)
    d0, _ = geom(st(tp=0.0, is_out=False, scale=1.0), fw=1920, fh=1080)
    dh, _ = geom(st(tp=0.5, is_out=False, scale=1.0), fw=1920, fh=1080)
    dt1, _ = geom(st(tp=1.0, is_out=False, scale=1.0), fw=1920, fh=1080)
    check(abs(d0.x() - dt1.x() - W) < 2, "(c1) 入场 tp=0 偏移 = w", round(d0.x() - dt1.x(), 1))
    check(abs(dh.x() - dt1.x() - W / 2) < 2, "(c2) tp=0.5 偏移 = w/2",
          round(dh.x() - dt1.x(), 1))

    print()
    print("=" * 74)
    print("【D】运行时状态：出场标记必须由 timeline 打出来")
    print("=" * 74)

    class _It:
        at_sec = 10.0
        end_sec = 50.0
        trans_in = "slide"
        trans_out = "slide"
        trans_in_sec = 4.0
        trans_out_sec = 4.0
        speed = 1.0
        src_in_sec = 0.0
        src_out_sec = None
        crop = None
        xform = {"scale": 0.28, "x": 0.0, "y": 0.0}
        note = ""
        item_id = "i"
        clip_id = "c"

    rt = tl.CueRuntime.__new__(tl.CueRuntime)
    rt._run_flags = {}          # 只测 _alpha_for，用空的进出场分组表
    it = _It()
    a, tr, tp, out = rt._alpha_for(it, None, 5.0)
    check(out is False and tr == "slide", "(d1) 入场 is_out=False", (out, tr))
    a, tr, tp, out = rt._alpha_for(it, None, 49.0)
    check(out is True and tr == "slide", "(d2) 出场 is_out=True", (out, tr))
    check(abs(tp - 0.75) < 0.01, "(d3) 出场 trans_p 仍是几何进度 1-a_out=0.75", round(tp, 3))

    print()
    print("=" * 74)
    if _fail:
        print("汇总：%d 项失败 ✗" % _fail)
        return 1
    print("汇总：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
