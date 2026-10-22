# -*- coding: utf-8 -*-
"""T01 · 需求 1 码表比例字段：往返 + 旧码表回落 + `aspect()`。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_model_ratio.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model import ercue_io                                  # noqa: E402
from ercue.model.cuesheet import CueSheet                         # noqa: E402


def test_default_is_16_9():
    s = CueSheet()
    assert s.res_w == 1920 and s.res_h == 1080, (s.res_w, s.res_h)
    assert s.aspect() == (1920, 1080)
    assert ercue_io.new_sheet("x").aspect() == (1920, 1080)
    print("  ✓ 默认 1920×1080（16:9）")


def test_aspect_guards():
    assert CueSheet(res_w=0, res_h=1080).aspect() == (1920, 1080)
    assert CueSheet(res_w=-5, res_h=1080).aspect() == (1920, 1080)
    assert CueSheet(res_w=1080, res_h="x").aspect() == (1080, 1080)   # 'x'→_i 回落 1080
    assert CueSheet(res_w=1080, res_h=1920).aspect() == (1080, 1920)  # 9:16 竖屏合法
    print("  ✓ aspect() 非法回落 16:9；竖屏合法")


def test_roundtrip_state_dict():
    s = CueSheet(title="竖屏", res_w=1080, res_h=1920)
    d = s.to_state_dict()
    assert d["res_w"] == 1080 and d["res_h"] == 1920, d
    back = CueSheet.from_state_dict(d)
    assert (back.res_w, back.res_h) == (1080, 1920), (back.res_w, back.res_h)
    print("  ✓ 状态快照往返保留比例")


def test_roundtrip_file_dict():
    s = CueSheet(title="宽屏", res_w=2560, res_h=1080)
    fd = ercue_io.sheet_to_file_dict(s)
    assert fd["res_w"] == 2560 and fd["res_h"] == 1080, fd
    back = ercue_io.sheet_from_file_dict(fd)
    assert back.aspect() == (2560, 1080), back.aspect()
    print("  ✓ 文件形态往返保留比例")


def test_old_sheet_without_field_falls_back():
    s = CueSheet(title="旧档", res_w=1024, res_h=768)
    fd = ercue_io.sheet_to_file_dict(s)
    fd.pop("res_w", None)
    fd.pop("res_h", None)                       # 模拟"旧码表：没有这两个字段"
    back = ercue_io.sheet_from_file_dict(fd)     # 不报错
    assert back.aspect() == (1920, 1080), back.aspect()
    print("  ✓ 旧码表（无字段）导出/导入回落 16:9，不报错")


def test_invalid_values_fall_back():
    fd = ercue_io.sheet_to_file_dict(CueSheet())
    fd["res_w"], fd["res_h"] = 0, "bogus"
    back = ercue_io.sheet_from_file_dict(fd)
    assert back.aspect() == (1920, 1080), back.aspect()
    # 只给了 res_w：res_h 回落 1080
    fd2 = ercue_io.sheet_to_file_dict(CueSheet())
    fd2.pop("res_h", None)
    fd2["res_w"] = 1080
    assert ercue_io.sheet_from_file_dict(fd2).aspect() == (1080, 1080)
    print("  ✓ 非法/半缺字段各自回落，不抛")


def test_no_format_version_bump():
    from ercue.model.cuesheet import FORMAT_VERSION
    assert CueSheet().format_version == FORMAT_VERSION
    fd = ercue_io.sheet_to_file_dict(CueSheet(res_w=1080, res_h=1920))
    assert fd["format_version"] == FORMAT_VERSION, "新增可选字段**不得**升版本号"
    print("  ✓ 未升 FORMAT_VERSION（仍 v%d）" % FORMAT_VERSION)


def main() -> int:
    tests = [
        ("默认 16:9", test_default_is_16_9),
        ("aspect 兜底", test_aspect_guards),
        ("状态快照往返", test_roundtrip_state_dict),
        ("文件形态往返", test_roundtrip_file_dict),
        ("旧码表回落（无字段）", test_old_sheet_without_field_falls_back),
        ("非法/半缺字段兜底", test_invalid_values_fall_back),
        ("不升版本号", test_no_format_version_bump),
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
