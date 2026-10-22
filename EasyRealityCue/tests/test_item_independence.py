# -*- coding: utf-8 -*-
"""T01 · 需求 10 片段独立性：`deep_clone` + 同素材两片段互不影响。

跑法：<Cue>\\venv\\Scripts\\python.exe tests\\test_item_independence.py
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
from ercue.model.cuesheet import CompLayer, CueItem, CueSheet          # noqa: E402


def test_two_items_from_same_media_are_independent():
    """同素材建两段（模拟"拖同一素材到两层"）⇒ 可变字段必须是**各自独立**的对象。"""
    a = CueItem(item_id="i1", clip_id="c1", at_sec=0.0)
    b = CueItem(item_id="i2", clip_id="c1", at_sec=5.0)
    assert a.xform is not b.xform, "xform 不能共享同一个 dict"
    assert a.crop is None and b.crop is None, "crop 默认 None（不可变单例，共享它是安全的）"
    a.xform["scale"] = 2.0
    a.crop = [0.1, 0.1, 0.5, 0.5]
    assert a.crop is not b.crop, "给 A 赋 list 后不能与 B 共享"
    a.speed = 2.0
    a.src_in_sec = 3.0
    assert b.xform == {"scale": 1.0, "x": 0.0, "y": 0.0}, b.xform
    assert b.crop is None and b.speed == 1.0 and b.src_in_sec == 0.0, b
    print("  ✓ 同素材两片段独立（改 A 不动 B）")


def test_default_factory_not_shared():
    """dataclass 的 mutable default 必须 per-instance（否则所有片段共享一个 dict）。"""
    xs = [CueItem() for _ in range(3)]
    ids = {id(x.xform) for x in xs}
    assert len(ids) == 3, "xform 默认值被共享了（default_factory 失效）"
    xs[0].xform["x"] = 9.0
    assert xs[1].xform["x"] == 0.0 and xs[2].xform["x"] == 0.0
    print("  ✓ 默认 xform 每实例独立")


def test_deep_clone_independent():
    src = CueItem(item_id="orig", clip_id="c1", at_sec=1.0, end_sec=6.0,
                  src_in_sec=0.5, src_out_sec=4.5, speed=1.5, note="原",
                  crop=[0.1, 0.2, 0.3, 0.4], xform={"scale": 1.2, "x": 3.0, "y": -2.0},
                  blend="screen", opacity=0.8, trans_in="glitch")
    dup = src.deep_clone("copy")
    # 标量逐一同值
    assert dup.item_id == "copy" and src.item_id == "orig"
    for k in ("clip_id", "at_sec", "end_sec", "src_in_sec", "src_out_sec", "speed",
              "note", "blend", "opacity", "trans_in"):
        assert getattr(dup, k) == getattr(src, k), (k, getattr(dup, k), getattr(src, k))
    # 可变字段是**新对象**
    assert dup.xform == src.xform and dup.xform is not src.xform, "xform 必须深拷贝"
    assert dup.crop == src.crop and dup.crop is not src.crop, "crop 必须深拷贝"
    dup.xform["scale"] = 9.9
    dup.crop[0] = 0.99
    dup.speed = 3.0
    assert src.xform["scale"] == 1.2, "改副本的 xform 影响到了原片段"
    assert src.crop[0] == 0.1, "改副本的 crop 影响到了原片段"
    assert src.speed == 1.5, "改副本的 speed 影响到了原片段"
    # 反向也成立
    src.xform["x"] = 77.0
    assert dup.xform["x"] == 3.0
    print("  ✓ deep_clone 深拷贝（双向独立）")


def test_deep_clone_default_id_kept():
    it = CueItem(item_id="keep", clip_id="c1")
    assert it.deep_clone().item_id == "keep", "不传 new_item_id 时保留原 id"
    print("  ✓ deep_clone 默认保留 item_id")


def test_clone_in_sheet_roundtrip():
    """把副本放进码表 ⇒ 往返后两段仍各自独立（序列化也不是共享）。"""
    s = CueSheet(title="t")
    ly = CompLayer(lyr_id="L1", name="层1", z=0)
    a = CueItem(item_id="i1", clip_id="c1", at_sec=0.0, crop=[0, 0, 1, 1])
    ly.items.append(a)
    ly.items.append(a.deep_clone("i2"))
    s.tracks = []
    from ercue.model.cuesheet import Composition, TrackEntry
    te = TrackEntry(track_key="k", composition=Composition(layers=[ly]))
    s.tracks.append(te)
    back = ercue_io.sheet_from_file_dict(ercue_io.sheet_to_file_dict(s))
    it1, it2 = back.tracks[0].layers[0].items
    assert it1.crop == it2.crop == [0, 0, 1, 1]
    assert it1.crop is not it2.crop, "往返后两段的 crop 不能是同一对象"
    it2.crop[0] = 0.5
    assert it1.crop[0] == 0.0, "改一段的 crop 影响到了另一段"
    print("  ✓ 副本随码表往返后仍独立")


def main() -> int:
    tests = [
        ("同素材两片段独立", test_two_items_from_same_media_are_independent),
        ("默认值不共享", test_default_factory_not_shared),
        ("deep_clone 深拷贝", test_deep_clone_independent),
        ("deep_clone 默认 id", test_deep_clone_default_id_kept),
        ("副本随码表往返", test_clone_in_sheet_roundtrip),
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
