# -*- coding: utf-8 -*-
"""第三轮 T01 · 变速一致性（A3/#20）：`reconcile_item_timing` 的"唯一自洽关系"。

自洽式：`end - at  ==  (src_out_eff - src_in) / speed`

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_reconcile_timing.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model.cuesheet import (CueItem, item_source_used_sec,      # noqa: E402
                                  reconcile_item_timing)

DUR = 10.0


def _prev(it) -> dict:
    """改动**前**的快照（与 window 接线时抓的字段一致）。"""
    return {"at_sec": it.at_sec, "end_sec": it.end_sec, "src_in_sec": it.src_in_sec,
            "src_out_sec": it.src_out_sec, "speed": it.speed}


def _mk(at=0.0, end=DUR, si=0.0, so=None, speed=1.0, **kw):
    return CueItem(item_id="i", clip_id="c1", at_sec=at, end_sec=end, src_in_sec=si,
                   src_out_sec=so, speed=speed, **kw)


def _self_consistent(it, src_dur=DUR) -> bool:
    """自洽式是否成立（`src_out` 未知则用 `src_dur`）。"""
    used = item_source_used_sec(it, src_dur)
    if used is None or it.end_sec is None:
        return False
    return abs((float(it.end_sec) - float(it.at_sec)) - used / float(it.speed)) < 1e-6


# ---------------------------------------------------------------- 三种触发
def test_change_speed_recomputes_end():
    """只改 speed ⇒ **源范围权威**：end = at + used/speed。"""
    it = _mk(end=DUR)                      # at=0, end=10, src_out=None, src_dur=10, speed=1
    p = _prev(it)
    it.speed = 0.5
    lvl = reconcile_item_timing(it, p, src_dur=DUR)
    assert lvl == "end_from_source", lvl
    assert abs(it.end_sec - 20.0) < 1e-9, it.end_sec      # 0 + 10/0.5
    assert it.src_out_sec is None, "源范围权威支不该写 src_out"
    assert _self_consistent(it)
    # 再改一次：0.5 → 2.0（prev 的 speed = 0.5）
    p = _prev(it)
    it.speed = 2.0
    assert reconcile_item_timing(it, p, src_dur=DUR) == "end_from_source"
    assert abs(it.end_sec - 5.0) < 1e-9, it.end_sec       # 0 + 10/2
    print("  ✓ 改 speed ⇒ 重算 end（0.5×→20s / 2×→5s）")


def test_change_src_range_recomputes_end():
    """改 src_in / src_out（时长未变）⇒ **源范围权威**：end = at + used/speed。"""
    it = _mk(end=DUR, so=None)
    p = _prev(it)
    it.src_out_sec = 5.0
    lvl = reconcile_item_timing(it, p, src_dur=DUR)
    assert lvl == "end_from_source", lvl
    assert abs(it.end_sec - 5.0) < 1e-9, it.end_sec       # used=5 ⇒ end=5
    # 改 src_in：used = src_out - src_in
    p = _prev(it)
    it.src_in_sec = 1.0
    lvl = reconcile_item_timing(it, p, src_dur=DUR)
    assert lvl == "end_from_source" and abs(it.end_sec - 4.0) < 1e-9, (lvl, it.end_sec)
    print("  ✓ 改 src_out/src_in ⇒ 重算 end（5s / 4s）")


def test_change_end_recomputes_src_out_and_holds():
    """只改**时长**（end − at）⇒ **时长权威**：src_out = src_in + dur*speed（钳到 src_dur ⇒ **定格**）。"""
    it = _mk(end=DUR, so=None)
    p = _prev(it)
    it.end_sec = 20.0                                   # 拉到 20s（素材只有 10s）
    lvl = reconcile_item_timing(it, p, src_dur=DUR)
    assert lvl == "src_from_end", lvl
    assert abs(it.src_out_sec - DUR) < 1e-9, it.src_out_sec   # 钳到素材全长
    assert abs(it.end_sec - 20.0) < 1e-9, "★ 时长权威：end 必须保留（末尾定格）"
    # 而"未超素材"时正常换算：只改时长（10s → 6s，素材 10s 够）
    it2 = _mk(end=DUR, so=None)
    p2 = _prev(it2)
    it2.end_sec = 6.0
    assert reconcile_item_timing(it2, p2, src_dur=DUR) == "src_from_end"
    assert abs(it2.src_out_sec - 6.0) < 1e-9, it2.src_out_sec
    print("  ✓ 改时长 ⇒ 换算 src_out，超出钳到素材末端并**保留 end（定格）**")


def test_pure_move_does_not_touch_src_out():
    """★ 只**平移**（at/end 同时平移、时长不变）⇒ `none`，**绝不该改写 src_out**。"""
    it = _mk(at=0.0, end=5.0, si=0.0, so=5.0)
    p = _prev(it)
    it.at_sec, it.end_sec = 30.0, 35.0                  # 整体右移 30s（时长仍 5s）
    lvl = reconcile_item_timing(it, p, src_dur=DUR)
    assert lvl == "none", lvl
    assert abs(it.src_out_sec - 5.0) < 1e-9, ("平移不该动 src_out", it.src_out_sec)
    assert abs(it.end_sec - 35.0) < 1e-9 and abs(it.at_sec - 30.0) < 1e-9
    print("  ✓ 纯平移时长不变 ⇒ 'none'（不改写 src_out）")


# ---------------------------------------------------------------- 边界
def test_both_changed_source_wins():
    """两者都变 ⇒ **源范围优先**（end 派生）。"""
    it = _mk(end=DUR, so=4.0)
    p = _prev(it)
    it.end_sec = 99.0                                   # 改时长
    it.src_out_sec = 8.0                                # 同时改源
    lvl = reconcile_item_timing(it, p, src_dur=DUR)
    assert lvl == "end_from_source", lvl
    assert abs(it.end_sec - 8.0) < 1e-9, it.end_sec
    print("  ✓ 两者都变 ⇒ 源范围优先（end=8s）")


def test_unknown_source_does_nothing():
    """源长度未知（无 src_out 且 src_dur≤0，如**图片**）⇒ `none`，一切不动。"""
    it = _mk(at=2.0, end=7.0, so=None)
    p = _prev(it)
    it.speed = 0.5
    lvl = reconcile_item_timing(it, p, src_dur=0.0)
    assert lvl == "none", lvl
    assert abs(it.end_sec - 7.0) < 1e-9, ("图片/未知源不该被收敛", it.end_sec)
    print("  ✓ 源未知（图片）⇒ 'none'，时长不动")


def test_gif_never_writes_src_out():
    """★ GIF（`keep_src_out_unset=True`）⇒ **绝不写 src_out**（循环依赖它保持 None）。"""
    it = _mk(end=5.0, so=None)                          # GIF：一轮 10s，不设 src_out
    p = _prev(it)
    it.end_sec = 30.0                                   # 用户把时长拉长 ⇒ 想让它循环更久
    lvl = reconcile_item_timing(it, p, src_dur=DUR, keep_src_out_unset=True)
    assert lvl == "none", lvl
    assert it.src_out_sec is None, ("GIF 绝不该有 src_out", it.src_out_sec)
    assert abs(it.end_sec - 30.0) < 1e-9, "end 保留"
    # 而 speed 变化仍会重算 end（design：循环片段也符合直觉）
    p = _prev(it)
    it.speed = 2.0
    assert reconcile_item_timing(it, p, src_dur=DUR, keep_src_out_unset=True) == "end_from_source"
    assert abs(it.end_sec - 5.0) < 1e-9, it.end_sec     # 0 + 10/2
    assert it.src_out_sec is None
    print("  ✓ GIF：改时长只留 end（src_out 恒 None）；改 speed 仍重算 end")


def test_used_sec_helper():
    assert item_source_used_sec(_mk(so=6.0, si=1.0)) == 5.0
    assert item_source_used_sec(_mk(so=None, si=2.0), src_dur=10.0) == 8.0
    assert item_source_used_sec(_mk(so=None, si=2.0), src_dur=0.0) is None
    assert item_source_used_sec(_mk(so=1.0, si=5.0)) == 0.0     # 不产生负值
    print("  ✓ item_source_used_sec：已知/未知/退化 都正确")


def test_self_consistent_after_all_branches():
    """三条主支执行后，自洽式都必须成立（或明确处于"定格"）。"""
    # 支1：改 speed
    it = _mk(end=DUR, so=None)
    p = _prev(it); it.speed = 0.5
    reconcile_item_timing(it, p, src_dur=DUR)
    assert _self_consistent(it)
    # 支2：改 src_out
    it = _mk(end=DUR, so=None)
    p = _prev(it); it.src_out_sec = 7.0
    reconcile_item_timing(it, p, src_dur=DUR)
    assert _self_consistent(it)
    # 支3：改 end 且**未超素材** ⇒ 自洽
    it = _mk(end=DUR, so=None)
    p = _prev(it); it.end_sec = 8.0
    reconcile_item_timing(it, p, src_dur=DUR)
    assert _self_consistent(it), (it.end_sec, it.src_out_sec)
    print("  ✓ 三条主支后自洽式均成立")


def main() -> int:
    tests = [
        ("改 speed ⇒ 重算 end", test_change_speed_recomputes_end),
        ("改 src 范围 ⇒ 重算 end", test_change_src_range_recomputes_end),
        ("改时长 ⇒ 换算 src_out + 定格", test_change_end_recomputes_src_out_and_holds),
        ("纯平移不改 src_out", test_pure_move_does_not_touch_src_out),
        ("两者都变 ⇒ 源优先", test_both_changed_source_wins),
        ("源未知 ⇒ 不动", test_unknown_source_does_nothing),
        ("GIF 绝不写 src_out", test_gif_never_writes_src_out),
        ("used_sec 辅助函数", test_used_sec_helper),
        ("自洽式成立", test_self_consistent_after_all_branches),
    ]
    ok, passed = True, 0
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
            passed += 1
        except Exception as e:                              # noqa: BLE001
            ok = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (passed, len(tests) - passed))
    print("汇总：%s" % ("全部通过 ✓" if ok else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
