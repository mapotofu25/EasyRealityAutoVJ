# -*- coding: utf-8 -*-
"""T01 · 需求 9 语义迁移：**层混合 → 素材级混合**（名字 + 语义一起改）。

核心保证：
  · **旧码表（只有层级 `blend`）导入后行为逐值不变** —— 片段没有自己的 blend ⇒ 继承层值
    （"沿用层值作为该层所有片段的初值"）。
  · 片段一旦自设 blend/opacity ⇒ **只影响自己**，同层别的片段不受影响。
  · 可选字段往返不丢、不升 `FORMAT_VERSION`。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_item_blend_migration.py
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model import ercue_io                                       # noqa: E402
from ercue.model.cuesheet import (Composition, CompLayer, CueItem,      # noqa: E402
                                  CueSheet, TrackEntry)


def _sheet_with_layer(blend="screen", opacity=0.7, n_items=2):
    ly = CompLayer(lyr_id="L1", name="层1", z=0, blend=blend, opacity=opacity)
    for i in range(n_items):
        ly.items.append(CueItem(item_id="i%d" % (i + 1), clip_id="c1", at_sec=float(i)))
    te = TrackEntry(track_key="k", composition=Composition(layers=[ly]))
    s = CueSheet(title="t")
    s.tracks = [te]
    return s, ly


def test_old_sheet_inherits_layer_blend():
    """★ 旧码表（层级 blend）导入 ⇒ 每个片段的**有效值** = 层值（行为不变）。"""
    s, ly = _sheet_with_layer("multiply", 0.6)
    # 模拟"旧码表"：导出的 item 去掉 blend/opacity 两个键
    fd = ercue_io.sheet_to_file_dict(s)
    for t in fd["tracks"]:
        for lay in t["composition"]["layers"]:
            for it in lay["items"]:
                it.pop("blend", None)
                it.pop("opacity", None)
    back = ercue_io.sheet_from_file_dict(fd)          # 不报错
    ly2 = back.tracks[0].layers[0]
    assert ly2.blend == "multiply" and abs(ly2.opacity - 0.6) < 1e-9, (ly2.blend, ly2.opacity)
    for it in ly2.items:
        assert it.blend == "" and it.opacity is None, "旧片段应为『没设过』状态（继承本层）"
        assert ly2.effective_blend(it) == "multiply", ly2.effective_blend(it)
        assert abs(ly2.effective_opacity(it) - 0.6) < 1e-9
    print("  ✓ 旧码表：片段有效混合/不透明度 == 层值（逐值不变，惰性继承）")


def test_item_override_only_affects_itself():
    """片段自设 blend ⇒ **同层别的片段不受影响**（这就是"每段素材各自互不影响"）。"""
    s, ly = _sheet_with_layer("normal", 1.0, n_items=3)
    a, b, c = ly.items
    a.blend = "add"
    a.opacity = 0.25
    assert ly.effective_blend(a) == "add" and abs(ly.effective_opacity(a) - 0.25) < 1e-9
    for other in (b, c):
        assert ly.effective_blend(other) == "normal", ly.effective_blend(other)
        assert abs(ly.effective_opacity(other) - 1.0) < 1e-9
    # 改层值：只影响"没自设"的；已自设的 a 不受影响
    ly.blend, ly.opacity = "darken", 0.9
    assert ly.effective_blend(a) == "add" and abs(ly.effective_opacity(a) - 0.25) < 1e-9
    assert ly.effective_blend(b) == "darken" and abs(ly.effective_opacity(b) - 0.9) < 1e-9
    print("  ✓ 片段覆盖只影响自己；层值仍是未覆盖片段的默认")


def test_roundtrip_keeps_item_blend():
    s, ly = _sheet_with_layer("normal", 1.0, n_items=2)
    ly.items[0].blend = "lighten"
    ly.items[0].opacity = 0.5
    fd = ercue_io.sheet_to_file_dict(s)
    its = fd["tracks"][0]["composition"]["layers"][0]["items"]
    assert its[0]["blend"] == "lighten" and abs(its[0]["opacity"] - 0.5) < 1e-9, its[0]
    assert its[1]["blend"] == "" and its[1]["opacity"] is None, its[1]
    back = ercue_io.sheet_from_file_dict(fd)
    ly2 = back.tracks[0].layers[0]
    assert ly2.items[0].blend == "lighten" and abs(ly2.items[0].opacity - 0.5) < 1e-9
    assert ly2.items[1].blend == "" and ly2.items[1].opacity is None
    assert ly2.effective_blend(ly2.items[1]) == "normal"
    print("  ✓ 往返保留片段级 blend/opacity（未设的仍为继承）")


def test_normalized_rejects_bad_blend():
    ly = CompLayer(lyr_id="L", blend="normal")
    it = CueItem(item_id="i", clip_id="c1", blend="bogus", opacity=9.0)
    ly.items.append(it)
    ly.normalized()
    assert it.blend == "", "非法 blend 应回落 ''（= 继承本层）"
    assert it.opacity == 1.0, it.opacity          # 钳到 1.0
    assert ly.effective_blend(it) == "normal"
    print("  ✓ 非法 item.blend 回落继承；opacity 钳位")


def test_materialize_migration_helper():
    """显式迁移（可选）：把层值物化到"还没设过"的片段 ⇒ 之后层值不再影响它们。"""
    s, ly = _sheet_with_layer("screen", 0.4, n_items=2)
    ly.items[1].blend = "add"                       # 有一个已自设
    ly.materialize_item_blend()
    assert ly.items[0].blend == "screen" and abs(ly.items[0].opacity - 0.4) < 1e-9
    assert ly.items[1].blend == "add", "已自设的不应被覆盖"
    ly.blend = "normal"
    assert ly.effective_blend(ly.items[0]) == "screen"     # 物化后不再跟层走
    assert ly.effective_blend(ly.items[1]) == "add"
    print("  ✓ materialize_item_blend 显式迁移可用（默认不调=惰性继承）")


def test_no_format_version_bump():
    from ercue.model.cuesheet import FORMAT_VERSION
    fd = ercue_io.sheet_to_file_dict(_sheet_with_layer()[0])
    assert fd["format_version"] == FORMAT_VERSION, "新增可选字段不得升版本号"
    print("  ✓ 未升 FORMAT_VERSION（仍 v%d）" % FORMAT_VERSION)


def test_timeline_emits_per_item_blend():
    """★ 端到端下沉：`CueRuntime.update()` 产出的 `LayerDrawState.blend` 必须**按片段**取
    （否则"每段素材各自混合"只是数据字段、渲染层没生效）。"""
    from ercue.model.cuesheet import Identity, MediaRef
    from ercue.model.timeline import CueRuntime
    s = CueSheet(title="t")
    te = TrackEntry(track_key="k", identity=Identity(title="P", duration_sec=60.0, bpm=120.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0, blend="darken", opacity=0.5)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=10.0,
                            trans_in="cut", trans_out="cut", blend="add", opacity=0.25))
    ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=10.0, end_sec=20.0,
                            trans_in="cut", trans_out="cut"))   # 未自设 ⇒ 继承层值
    te.composition.layers.append(ly)
    s.media.append(MediaRef(clip_id="c1", name="a.mp4", duration_sec=30.0))
    s.tracks.append(te)
    rt = CueRuntime(s, te, media_paths={"c1": "a.mp4"})
    st1 = [x for x in rt.update(5.0) if x.active][0]      # 命中 i1
    st2 = [x for x in rt.update(15.0) if x.active][0]     # 命中 i2
    assert st1.item_id == "i1" and st1.blend == "add", (st1.item_id, st1.blend)
    assert abs(st1.opacity - 0.25) < 1e-9, st1.opacity
    assert st2.item_id == "i2" and st2.blend == "darken", (st2.item_id, st2.blend)
    assert abs(st2.opacity - 0.5) < 1e-9, st2.opacity
    # 旧码表（片段都没自设）⇒ 两层都随层值（行为不变）
    ly.items[0].blend, ly.items[0].opacity = "", None
    st1b = [x for x in rt.update(5.0) if x.active][0]
    assert st1b.blend == "darken" and abs(st1b.opacity - 0.5) < 1e-9, (st1b.blend, st1b.opacity)
    print("  ✓ timeline 产出的 blend/opacity 按片段取；未自设则继承层值")


def main() -> int:
    tests = [
        ("旧码表继承层值", test_old_sheet_inherits_layer_blend),
        ("片段覆盖只影响自己", test_item_override_only_affects_itself),
        ("往返保留片段级 blend", test_roundtrip_keeps_item_blend),
        ("非法值兜底", test_normalized_rejects_bad_blend),
        ("显式迁移助手", test_materialize_migration_helper),
        ("不升版本号", test_no_format_version_bump),
        ("timeline 按片段取 blend", test_timeline_emits_per_item_blend),
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
