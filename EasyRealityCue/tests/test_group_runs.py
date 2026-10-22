# -*- coding: utf-8 -*-
"""第三轮 T01 · #17「相邻同设置视为一个素材」（run）：`group_runs` / `run_flags` + E17 端到端。

判据：相邻同设置 ⇒ 一段（只段首淡入、段尾淡出、**段内 cut**）；设置不同 / 有缝隙 ⇒ 断段；
      **单片段行为与改动前完全一致**（回归底线）。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_group_runs.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model.cuesheet import (Composition, CompLayer, CueItem,       # noqa: E402
                                  CueSheet, Identity, MediaRef, TrackEntry)
from ercue.model.timeline import (ADJ_EPS, CueRuntime, group_runs,       # noqa: E402
                                  run_flags, same_render_settings)


def _it(iid, at, end, **kw):
    end = None if end is None else float(end)
    return CueItem(item_id=iid, clip_id=kw.pop("clip_id", "c1"), at_sec=float(at),
                   end_sec=end, **kw)


def _chain(*ranges):
    """按 [at,end] 列表造一串**设置相同**的片段。"""
    return [_it("i%d" % i, a, b) for i, (a, b) in enumerate(ranges)]


# ---------------------------------------------------------------- 归组
def test_adjacent_same_settings_group():
    its = _chain((0, 5), (5, 10), (10, 15))
    assert group_runs(its) == [(0, 2)], group_runs(its)
    f = run_flags(its)
    assert f["i0"] == (True, False), f
    assert f["i1"] == (False, False), ("段内", f)
    assert f["i2"] == (False, True), f
    print("  ✓ 相邻同设置 ⇒ 一段；段首/段内/段尾 标记正确")


def test_settings_differ_splits():
    its = _chain((0, 5), (5, 10), (10, 15))
    its[1].crop = [0, 0, 1, 1]                       # 中间那片改了 crop
    assert group_runs(its) == [(0, 0), (1, 1), (2, 2)], group_runs(its)
    # 速度不同 / 混合不同 同样断段
    for attr, val in (("speed", 2.0), ("blend", "screen"), ("opacity", 0.5),
                      ("trans_in", "cut"), ("src_in_sec", 1.0)):
        a, b = _it("a", 0, 5), _it("b", 5, 10)
        assert group_runs([a, b]) == [(0, 1)], attr
        setattr(b, attr, val)
        assert group_runs([a, b]) == [(0, 0), (1, 1)], ("改了 %s 应断段" % attr)
    print("  ✓ 中间一片设置不同（crop/speed/blend/opacity/trans/src_in）⇒ 断段")


def test_gap_splits():
    its = _chain((0, 5), (5.5, 10))                  # 0.5s 缝隙
    assert group_runs(its) == [(0, 0), (1, 1)], group_runs(its)
    # 缝隙在容差内 ⇒ 仍算相邻
    its2 = _chain((0, 5), (5 + ADJ_EPS / 2, 10))
    assert group_runs(its2) == [(0, 1)], group_runs(its2)
    print("  ✓ 有缝隙 ⇒ 不合；缝隙 ≤ ADJ_EPS ⇒ 仍合")


def test_open_ended_prev_is_adjacent():
    """`prev.end_sec is None` ⇒ **天然相邻**（end_eff 就是 next.at）。"""
    its = [_it("i0", 0, None), _it("i1", 5, 10)]
    assert group_runs(its) == [(0, 1)], group_runs(its)
    print("  ✓ 开放结尾（end=None）⇒ 与下一片天然相邻")


def test_single_item_unchanged():
    f = run_flags([_it("only", 0, 5)])
    assert f["only"] == (True, True), f
    assert group_runs([_it("only", 0, 5)]) == [(0, 0)]
    assert group_runs([]) == []
    print("  ✓ 单片段 ⇒ [(0,0)] 且 flags=(True,True)（行为不变的依据）")


def test_unsorted_input_is_sorted():
    its = [_it("i2", 10, 15), _it("i0", 0, 5), _it("i1", 5, 10)]
    assert group_runs(its) == [(0, 2)], group_runs(its)
    f = run_flags(its)
    assert f["i0"] == (True, False) and f["i2"] == (False, True), f
    print("  ✓ 乱序输入按 at 排序后归组（下标是排序后的）")


def test_same_render_settings_helper():
    a, b = _it("a", 0, 5), _it("b", 9, 20)
    assert same_render_settings(a, b), "位置/身份不同不影响'设置相同'"
    b.note = "x"
    assert same_render_settings(a, b), "note 不算设置"
    b.xform = {"scale": 2.0, "x": 0.0, "y": 0.0}
    assert not same_render_settings(a, b), "xform 算设置"
    c = _it("c", 0, 5)
    c.crop = [0, 0, 1, 1]
    d = _it("d", 0, 5)
    d.crop = [0, 0, 1, 1]
    assert same_render_settings(c, d), "list 字段按值比较"
    print("  ✓ same_render_settings：位置/note 不算；xform/crop 算")


# ---------------------------------------------------------------- E17 端到端
def _rt(items):
    s = CueSheet(title="t")
    te = TrackEntry(track_key="k", identity=Identity(title="P", duration_sec=60.0, bpm=120.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    ly.items.extend(items)
    te.composition.layers.append(ly)
    s.media.append(MediaRef(clip_id="c1", name="a.mp4", duration_sec=60.0))
    s.tracks.append(te)
    return CueRuntime(s, te, media_paths={"c1": "a.mp4"})


def _chain_fade(*ranges):
    out = []
    for i, (a, b) in enumerate(ranges):
        out.append(CueItem(item_id="i%d" % i, clip_id="c1", at_sec=float(a),
                           end_sec=float(b), trans_in="fade", trans_in_sec=1.0,
                           trans_out="fade", trans_out_sec=1.0))
    return out


def _alpha_at(items, pos):
    """**每次都用全新运行时**读 alpha —— `update()` 是有状态的（`_last_pos` 触发"跳变重算"），
    同一实例上先后探两个位置会互相影响，故不能复用实例。"""
    st = _rt(items).update(pos)
    return float(st[0].alpha)


def test_e17_interior_suppresses_fade():
    its = _chain_fade((0, 5), (5, 10), (10, 15))
    # 段首（i0 起点后 0.1s）⇒ 正常淡入 ⇒ α≈0.1
    assert abs(_alpha_at(its, 0.1) - 0.1) < 0.02, _alpha_at(its, 0.1)
    # 段内（i1 起点后 0.1s）⇒ **抑制淡入** ⇒ α=1.0
    assert abs(_alpha_at(its, 5.1) - 1.0) < 1e-6, ("段内应无淡入", _alpha_at(its, 5.1))
    # 段内（i1 末尾前 0.1s）⇒ **抑制淡出** ⇒ α=1.0
    assert abs(_alpha_at(its, 9.9) - 1.0) < 1e-6, ("段内应无淡出", _alpha_at(its, 9.9))
    # 段尾（i2 末尾前 0.1s）⇒ 正常淡出 ⇒ α≈0.1
    assert abs(_alpha_at(its, 14.9) - 0.1) < 0.02, _alpha_at(its, 14.9)
    print("  ✓ E17：段首淡入 / 段内 cut / 段尾淡出")


def test_e17_single_item_behaviour_unchanged():
    """★ 回归底线：**单片段**（run 长度 1）行为与改动前一致（正常淡入淡出）。"""
    its = _chain_fade((0, 5))
    assert abs(_alpha_at(its, 0.1) - 0.1) < 0.02, ("单片段应正常淡入", _alpha_at(its, 0.1))
    assert abs(_alpha_at(its, 4.9) - 0.1) < 0.02, ("单片段应正常淡出", _alpha_at(its, 4.9))
    print("  ✓ 单片段：正常淡入淡出（与改动前一致）")


def test_e17_gap_keeps_per_clip_fades():
    its = _chain_fade((0, 5), (6, 11))               # 1s 缝隙 ⇒ 各自独立
    assert abs(_alpha_at(its, 6.1) - 0.1) < 0.02, ("有缝隙 ⇒ 各自淡入", _alpha_at(its, 6.1))
    assert abs(_alpha_at(its, 4.9) - 0.1) < 0.02, ("有缝隙 ⇒ 各自淡出", _alpha_at(its, 4.9))
    print("  ✓ 有缝隙 ⇒ 各自独立淡入淡出（不变）")


def test_e17_does_not_change_play_sec():
    """E17 **只改过渡判定**，绝不改取帧位置 `play_sec`。

    ⚠ `play_sec` 是**相对素材**的秒数（`src_in + (pos-at)*speed`），不是时间轴位置。
    """
    rt = _rt(_chain_fade((0, 5), (5, 10)))
    st = rt.update(7.0)[0]
    assert abs(float(st.play_sec) - 2.0) < 1e-6, ("play_sec = 7-5 = 2（相对素材）", st.play_sec)
    assert float(st.alpha) == 1.0, ("段内 ⇒ 无过渡", st.alpha)
    assert st.blend == "normal", st.blend
    print("  ✓ E17 不动 play_sec / blend（只改 alpha 过渡）")


def main() -> int:
    tests = [
        ("相邻同设置归组", test_adjacent_same_settings_group),
        ("设置不同断段", test_settings_differ_splits),
        ("缝隙断段", test_gap_splits),
        ("开放结尾天然相邻", test_open_ended_prev_is_adjacent),
        ("单片段不变", test_single_item_unchanged),
        ("乱序输入排序", test_unsorted_input_is_sorted),
        ("same_render_settings", test_same_render_settings_helper),
        ("E17 段内 cut", test_e17_interior_suppresses_fade),
        ("E17 单片段回归", test_e17_single_item_behaviour_unchanged),
        ("E17 有缝隙不变", test_e17_gap_keeps_per_clip_fades),
        ("E17 不动 play_sec", test_e17_does_not_change_play_sec),
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
