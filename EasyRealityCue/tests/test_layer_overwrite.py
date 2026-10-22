# -*- coding: utf-8 -*-
"""第三轮 T01 · #15 同层「顶掉」（PR 覆盖）：`CompLayer.overwrite` / `plan_overwrite`。

规则：B 被 A 全覆盖 ⇒ 删 B；A 盖 B 左半 ⇒ 裁 B 头；A 盖 B 右半 ⇒ 裁 B 尾；
      A 落在 B 内部 ⇒ 删 B；**恰好相接 ⇒ 不重叠**。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_layer_overwrite.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model.cuesheet import CompLayer, CueItem, plan_overwrite    # noqa: E402


def _it(iid, at, end, clip="c1"):
    return CueItem(item_id=iid, clip_id=clip, at_sec=float(at),
                   end_sec=(None if end is None else float(end)))


def _ly(*items):
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.extend(items)
    return ly


def _find(ly, iid):
    return next((x for x in ly.items if x.item_id == iid), None)


# ---------------------------------------------------------------- 五种情形
def test_b_fully_covered_removed():
    ly = _ly(_it("A", 4, 11), _it("B", 5, 10))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["removed"] == ["B"], rep
    assert _find(ly, "B") is None and _find(ly, "A") is not None
    print("  ✓ B 被完全覆盖 ⇒ 删 B")


def test_left_half_trims_b_head():
    ly = _ly(_it("A", 3, 10), _it("B", 5, 15))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["trimmed"] == ["B"] and rep["removed"] == [], rep
    b = _find(ly, "B")
    assert abs(b.at_sec - 10.0) < 1e-9, ("B 头应被裁到 A.end", b.at_sec)
    assert abs(b.end_sec - 15.0) < 1e-9, "B 尾不变"
    print("  ✓ A 盖 B 左半 ⇒ 裁 B 头（B.at = A.end = 10）")


def test_right_half_trims_b_tail():
    ly = _ly(_it("A", 10, 20), _it("B", 5, 15))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["trimmed"] == ["B"] and rep["removed"] == [], rep
    b = _find(ly, "B")
    assert abs(b.end_sec - 10.0) < 1e-9, ("B 尾应被裁到 A.at", b.end_sec)
    assert abs(b.at_sec - 5.0) < 1e-9, "B 头不变"
    print("  ✓ A 盖 B 右半 ⇒ 裁 B 尾（B.end = A.at = 10）")


def test_a_inside_b_removes_b():
    ly = _ly(_it("A", 10, 20), _it("B", 0, 30))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["removed"] == ["B"], rep
    assert _find(ly, "B") is None, "A 落在 B 内部 ⇒ 删 B（不拆两段）"
    print("  ✓ A 落在 B 内部 ⇒ 删 B（不拆成两段）")


def test_touching_edges_do_not_overlap():
    ly = _ly(_it("A", 10, 15), _it("B", 5, 10))          # 恰好相接于 10
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["removed"] == [] and rep["trimmed"] == [], ("相接不该动 B", rep)
    b = _find(ly, "B")
    assert abs(b.at_sec - 5.0) < 1e-9 and abs(b.end_sec - 10.0) < 1e-9
    # 反向相接（A 在左、B 在右）
    ly2 = _ly(_it("A", 0, 5), _it("B", 5, 10))
    rep2 = ly2.overwrite(_find(ly2, "A"))
    assert rep2["removed"] == [] and rep2["trimmed"] == [], rep2
    print("  ✓ 边界恰好相接（缝隙 0）⇒ 视为不重叠，不动 B")


def test_disjoint_untouched():
    ly = _ly(_it("A", 0, 4), _it("B", 5, 10))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep == {"removed": [], "trimmed": [], "plan": []}, rep
    assert abs(_find(ly, "B").at_sec - 5.0) < 1e-9
    print("  ✓ 不相交 ⇒ 完全不动")


# ---------------------------------------------------------------- 作用范围
def test_only_target_items_affected():
    """**只影响目标片段**：本层其它远处片段、以及**其它层**一律不动。"""
    ly = _ly(_it("A", 4, 11), _it("B", 5, 10), _it("C", 100, 110))
    other = _ly(_it("D", 5, 10))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["removed"] == ["B"], rep
    assert abs(_find(ly, "C").at_sec - 100.0) < 1e-9, "远处片段不该被动"
    assert _find(other, "D") is not None and abs(_find(other, "D").at_sec - 5.0) < 1e-9, \
        "**别的层**绝不该被动"
    print("  ✓ 只影响目标片段（本层远处 + 别的层都不动）")


def test_plan_is_pure():
    """`plan_overwrite` **不改任何对象**（纯函数）。"""
    a, b = _it("A", 4, 11), _it("B", 5, 10)
    items = [a, b]
    rep = plan_overwrite(items, a)
    assert rep["removed"] == ["B"], rep
    assert len(items) == 2, "纯函数不该改动入参列表"
    assert abs(b.at_sec - 5.0) < 1e-9 and abs(b.end_sec - 10.0) < 1e-9, "不该改 B"
    print("  ✓ plan_overwrite 是纯函数（不动入参对象与列表）")


def test_overwrite_is_self_excluding():
    """A 自己不会被自己顶掉。"""
    ly = _ly(_it("A", 0, 10))
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["removed"] == [] and rep["trimmed"] == []
    assert _find(ly, "A") is not None
    print("  ✓ A 不会顶掉自己")


def test_open_ended_last_item_point_range():
    """开放结尾（`end=None` 且无后继）⇒ `end_eff = at + 极小`：A 压在它起点上 ⇒ **删它**。"""
    ly = _ly(_it("A", 0, 5), _it("B", 0, None))           # B 开放结尾
    rep = ly.overwrite(_find(ly, "A"))
    assert rep["removed"] == ["B"], ("A 盖住 B 的起点 ⇒ 删 B", rep)
    print("  ✓ 开放结尾片段：A 压其起点 ⇒ 删它（end_eff 退化为一点）")


def test_overwrite_returns_report_shape():
    ly = _ly(_it("A", 3, 20), _it("B", 5, 10), _it("C", 15, 25))
    rep = ly.overwrite(_find(ly, "A"))
    assert set(rep.keys()) == {"removed", "trimmed", "plan"}, rep
    assert "B" in rep["removed"], rep
    print("  ✓ 返回 {removed, trimmed, plan}（报告形状固定）")


def main() -> int:
    tests = [
        ("B 全覆盖 ⇒ 删", test_b_fully_covered_removed),
        ("左盖 ⇒ 裁头", test_left_half_trims_b_head),
        ("右盖 ⇒ 裁尾", test_right_half_trims_b_tail),
        ("内含 ⇒ 删 B", test_a_inside_b_removes_b),
        ("相接 ⇒ 不重叠", test_touching_edges_do_not_overlap),
        ("不相交 ⇒ 不动", test_disjoint_untouched),
        ("只影响目标片段", test_only_target_items_affected),
        ("plan 纯函数", test_plan_is_pure),
        ("不自顶", test_overwrite_is_self_excluding),
        ("开放结尾退化", test_open_ended_last_item_point_range),
        ("报告形状", test_overwrite_returns_report_shape),
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
