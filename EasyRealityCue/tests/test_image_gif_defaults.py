# -*- coding: utf-8 -*-
"""T01 · 需求 4 图片默认 5 秒 + GIF 标记 + "GIF 不设 src_out"约束。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_image_gif_defaults.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.media import probe                                          # noqa: E402
from ercue.model.cuesheet import CueItem                               # noqa: E402


def test_image_default_constant():
    assert probe.IMAGE_DEFAULT_SEC == 5.0, probe.IMAGE_DEFAULT_SEC
    print("  ✓ IMAGE_DEFAULT_SEC == 5.0（唯一来源）")


def test_is_gif():
    for p, want in (("a.gif", True), ("A.GIF", True), ("x/y/z.GiF", True),
                    ("a.png", False), ("a.mp4", False), ("gif", False), ("", False)):
        assert probe.is_gif(p) is want, (p, probe.is_gif(p), want)
    # kind_of 仍把 GIF 当视频（循环由 frames 侧环绕，不是靠 kind）
    assert probe.kind_of("a.gif") == "video"
    assert probe.kind_of("a.png") == "image"
    print("  ✓ is_gif 判定正确；GIF 仍 kind=video")


def test_gif_src_out_constraint_checker():
    assert probe.gif_src_out_conflict("a.gif", None) is False, "GIF + src_out=None 应合规"
    assert probe.gif_src_out_conflict("a.gif", 0.0) is True, "设为 0 也算设了"
    assert probe.gif_src_out_conflict("a.gif", 12.5) is True
    assert probe.gif_src_out_conflict("a.mp4", 12.5) is False, "非 GIF 不受此约束"
    assert probe.gif_src_out_conflict("a.png", 5.0) is False
    print("  ✓ gif_src_out_conflict 检查器正确（约束可被机器检查）")


def test_model_never_fabricates_src_out():
    """GIF 循环依赖：不设 src_out 时，模型/规范化**绝不能**自己造出一个 src_out。"""
    it = CueItem(item_id="g", clip_id="c1", at_sec=0.0, end_sec=30.0, src_out_sec=None)
    it.normalized()
    assert it.src_out_sec is None, "normalized() 不应给 src_out 填值"
    # 往返(file形态)也不应产生 src_out
    from ercue.model import ercue_io
    d = ercue_io._item_to_file(it, 120.0)
    assert d["src_out_sec"] is None, d
    assert probe.gif_src_out_conflict("clip.gif", d["src_out_sec"]) is False
    print("  ✓ 模型不会凭空造 src_out（GIF 循环的前提成立）")


def test_image_placement_end():
    """图片摆放：`end = at + IMAGE_DEFAULT_SEC`（这是 UI 侧要写的公式，此处钉死常量语义）。"""
    at = 3.0
    end = at + probe.IMAGE_DEFAULT_SEC
    assert abs(end - 8.0) < 1e-9
    print("  ✓ 图片默认摆放时长 = %.1fs" % probe.IMAGE_DEFAULT_SEC)


def main() -> int:
    tests = [
        ("图片默认 5s 常量", test_image_default_constant),
        ("is_gif 判定", test_is_gif),
        ("GIF 约束检查器", test_gif_src_out_constraint_checker),
        ("模型不造 src_out", test_model_never_fabricates_src_out),
        ("图片摆放公式", test_image_placement_end),
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
