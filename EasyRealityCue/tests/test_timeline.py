# -*- coding: utf-8 -*-
"""M2 · 时间轴解释器测试（**零 Qt**）。

跑法：
    P:\\AutoVJ\\venv\\Scripts\\python.exe  P:\\EasyRealityCue\\tests\\test_timeline.py

覆盖（对应验收标准）：
  · ★ 逐帧单调推进：过渡按预期发生（E4，规格书 §6.14 测试陷阱原文）
  · 回滞：边界 ±0.01s 抖动 50 次不来回触发（E1）
  · 大跳变：前跳 10 秒 ⇒ 直接切新片段且不做过渡（E2）；倒退同理
  · z 升序契约（D3）、缺素材只跳过该 item（E3）、`play_sec` 变速折算（D4）
  · `end_sec` 到期失活
  · **零 Qt**：AST 断言 `model/timeline.py` 无任何 Qt import，且不直接碰 `beat_len`
"""

from __future__ import annotations

import ast
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ercue.model import timeline as tl                                    # noqa: E402
from ercue.model.cuesheet import (CueSheet, TrackEntry, Identity,         # noqa: E402
                                  CompLayer, CueItem, MediaRef)

TIMELINE_PY = os.path.join(_SRC, "ercue", "model", "timeline.py")

_FORBIDDEN = ("PySide6", "PyQt5", "PyQt6", "shiboken")


# --------------------------------------------------------------------------
# 构造辅助
# --------------------------------------------------------------------------
def _sheet(track, media_ids=("c1", "c2")):
    s = CueSheet(title="test")
    s.tracks.append(track)
    for cid in media_ids:
        s.media.append(MediaRef(clip_id=cid, name=cid + ".mp4"))
    return s


def _track(bpm=0.0):
    te = TrackEntry(track_key="k", identity=Identity(
        title="T", artist="A", duration_sec=300.0, bpm=bpm))
    te.identity.ensure_meta_id()
    return te


def _layer(lyr_id, z, items, blend="normal", opacity=1.0):
    ly = CompLayer(lyr_id=lyr_id, name=lyr_id, z=z, blend=blend, opacity=opacity)
    for it in items:
        ly.items.append(it.normalized())
    return ly


def _item(item_id, clip_id, at_sec, **kw):
    return CueItem(item_id=item_id, clip_id=clip_id, at_sec=at_sec, **kw)


def _paths(*ids):
    return {i: "P:/x/%s.mp4" % i for i in ids}


# --------------------------------------------------------------------------
# 零 Qt（AST）
# --------------------------------------------------------------------------
def test_zero_qt_ast():
    """`model/timeline.py` 必须零 Qt import，且不直接出现 `beat_len` 乘除。"""
    with open(TIMELINE_PY, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=TIMELINE_PY)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _FORBIDDEN:
                    bad.append("L%d import %s" % (node.lineno, a.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in _FORBIDDEN:
                bad.append("L%d from %s" % (node.lineno, mod))
        elif isinstance(node, ast.Name) and node.id == "beat_len":
            bad.append("L%d 出现标识符 beat_len（应走 units）" % node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "beat_len":
            bad.append("L%d 直接调用 .beat_len（应走 units）" % node.lineno)
    assert not bad, "零 Qt / 单位纪律违约：\n" + "\n".join(bad)
    print("  ✓ 零 Qt：timeline.py 无 Qt import、无 beat_len 直接引用")


# --------------------------------------------------------------------------
# E4：逐帧单调推进（★ 规格书 §6.14 测试陷阱）
# --------------------------------------------------------------------------
def test_frame_by_frame_transition():
    """★ 必须**逐帧单调推进** —— 瞬移测试会（正确地）被判成"跳转"、测不到过渡。"""
    te = _track(bpm=120.0)                     # beat_len=0.5，仅用于记 bpm（坐标仍是秒）
    te.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 32.0, trans_in="fade", trans_in_sec=2.0,
              trans_out="fade", trans_out_sec=2.0),
        _item("i2", "c2", 64.0, trans_in="cut", trans_out="cut"),
    ]))
    rt = tl.CueRuntime(_sheet(te), te, _paths("c1", "c2"))

    def walk(a, b, step=1.0 / 30.0):
        """逐帧单调推进（≈30fps），返回最后一次 update 的 states。"""
        p, last = a, None
        while p <= b + 1e-9:
            last = rt.update(p)
            p += step
        return last

    # 0s：未到 i1
    assert not rt.update(0.0)[0].active
    # 走到 32s：淡入起点 alpha≈0
    d = {s.lyr_id: s for s in walk(0.0, 32.0)}
    assert d["L1"].active and abs(d["L1"].alpha) < 1e-6, d["L1"]
    assert d["L1"].item_id == "i1" and abs(d["L1"].play_sec) < 1e-6, d["L1"]
    # 33s：淡入中点 alpha≈0.5（逐帧推进下必然测得到）
    d = {s.lyr_id: s for s in walk(32.0, 33.0)}
    assert abs(d["L1"].alpha - 0.5) < 1e-6, d["L1"]
    assert d["L1"].trans == "fade", d["L1"]
    # 34s：淡入完成
    d = {s.lyr_id: s for s in walk(33.0, 34.0)}
    assert abs(d["L1"].alpha - 1.0) < 1e-6, d["L1"]
    # 63s：接近 i2，淡出中点 alpha≈0.5
    d = {s.lyr_id: s for s in walk(34.0, 63.0)}
    assert abs(d["L1"].alpha - 0.5) < 1e-6, d["L1"]
    assert d["L1"].trans == "fade", d["L1"]
    # 64s：切到 i2（cut ⇒ 立即满）
    d = {s.lyr_id: s for s in walk(63.0, 64.5)}
    assert d["L1"].item_id == "i2" and abs(d["L1"].alpha - 1.0) < 1e-6, d["L1"]
    assert not rt.jumped, "逐帧单调推进不应触发跳转"
    print("  ✓ E4 逐帧单调推进：淡入 0→0.5→1、淡出 0.5、cut 硬切，全过程未误判跳转")


# --------------------------------------------------------------------------
# E1：回滞
# --------------------------------------------------------------------------
def test_hysteresis_no_flip():
    te = _track(bpm=0.0)                       # 退化：回滞=0.05s，大跳变=2.0s
    te.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 0.0, trans_in="cut", trans_out="cut"),
        _item("i2", "c2", 10.0, trans_in="cut", trans_out="cut"),
    ]))
    rt = tl.CueRuntime(_sheet(te), te, _paths("c1", "c2"))
    assert abs(rt.hyst - 0.05) < 1e-9 and abs(rt.jump - 2.0) < 1e-9, (rt.hyst, rt.jump)

    # 单调推进到边界
    p = 0.0
    while p <= 10.02 + 1e-9:
        rt.update(p)
        p += 0.02
    # 在边界 ±0.01s 抖动 50 次：item 不许来回触发
    seq = []
    for k in range(50):
        pos = 10.0 + (-0.01 if k % 2 else 0.01)
        st = {s.lyr_id: s for s in rt.update(pos)}["L1"]
        assert not rt.jumped, "±0.01s 抖动（< 回滞 0.05s）不应判为跳转 @%.3f" % pos
        seq.append(st.item_id)
    assert len(set(seq)) == 1 and seq[0] == "i2", seq
    print("  ✓ E1 回滞：边界 ±0.01s 抖动 50 次，item 恒为 i2、未误判跳转")

    # 回滞是有界的：退到 9.90s（超出 0.05s 回滞窗）应回到 i1
    rt2 = tl.CueRuntime(_sheet(te), te, _paths("c1", "c2"))
    assert {s.lyr_id: s for s in rt2.update(9.90)}["L1"].item_id == "i1"
    print("  ✓ E1 回滞有界：9.90s（超窗）正确回落 i1")


# --------------------------------------------------------------------------
# E2：大跳变 ⇒ 直接重算、不做过渡
# --------------------------------------------------------------------------
def test_big_jump_forward_no_transition():
    te = _track(bpm=120.0)                     # 大跳变阈值 = 8 拍 × 0.5 = 4.0s
    te.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 0.0, trans_in="fade", trans_in_sec=2.0,
              trans_out="fade", trans_out_sec=2.0),
        _item("i2", "c2", 30.0, trans_in="fade", trans_in_sec=2.0,
              trans_out="fade", trans_out_sec=2.0),
    ]))
    rt = tl.CueRuntime(_sheet(te), te, _paths("c1", "c2"))
    assert abs(rt.jump - 4.0) < 1e-9, rt.jump

    p = 0.0
    while p <= 10.0 + 1e-9:
        rt.update(p)
        p += 0.05
    # 前跳 10 → 31 秒（21s > 4s）：立即切到 i2、alpha 直接到位（不停在 0.5 的淡入中点）
    st = {s.lyr_id: s for s in rt.update(31.0)}["L1"]
    assert rt.jumped, "前跳 21 秒应判为大跳变"
    assert st.item_id == "i2" and abs(st.alpha - 1.0) < 1e-6, st
    assert abs(st.trans_p - 1.0) < 1e-6, st
    print("  ✓ E2 前跳 21s：立即切到 i2、alpha=1.0（未停在淡入中点 0.5）")

    # 对照：不跳变时，逐帧推进到 31s 应停在淡入中点 alpha≈0.5
    rtc = tl.CueRuntime(_sheet(te), te, _paths("c1", "c2"))
    q = 30.0
    while q <= 31.0 + 1e-9:
        stc = {s.lyr_id: s for s in rtc.update(q)}["L1"]
        q += 1.0 / 30.0
    assert abs(stc.alpha - 0.5) < 1e-6, stc
    print("  ✓ 对照（逐帧推进到 31s）：alpha≈0.5，证明跳变分支确实起效")


def test_big_jump_backward():
    te = _track(bpm=0.0)                       # 退化：大跳变阈值 = 2.0s
    te.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 32.0, trans_in="cut", trans_out="cut"),
    ]))
    rt = tl.CueRuntime(_sheet(te), te, _paths("c1"))
    p = 0.0
    while p <= 40.0 + 1e-9:
        rt.update(p)
        p += 0.05
    assert rt.update(40.0)[0].active
    st = rt.update(5.0)[0]
    assert rt.jumped, "倒退 35 秒应判为大跳变"
    assert not st.active, st
    print("  ✓ E2 倒退 35s：判为跳变且该层失活")


# --------------------------------------------------------------------------
# D3 / E3 / D4 / end_sec
# --------------------------------------------------------------------------
def test_z_order_contract_d3():
    te = _track()
    te.composition.layers += [
        _layer("Ltop", 10, [_item("a", "c1", 0.0)]),
        _layer("Lbot", 0, [_item("b", "c1", 0.0)]),
        _layer("Lmid", 5, [_item("c", "c1", 0.0)]),
    ]
    states = tl.CueRuntime(_sheet(te), te, _paths("c1")).update(1.0)
    assert [s.lyr_id for s in states] == ["Lbot", "Lmid", "Ltop"], states
    assert [s.z for s in states] == [0, 5, 10], states
    print("  ✓ D3 z 升序契约：返回 [Lbot(0), Lmid(5), Ltop(10)]")


def test_missing_skips_only_that_item_e3():
    te = _track()
    te.composition.layers += [
        _layer("L1", 0, [_item("i1", "c_gone", 0.0)]),      # 缺素材
        _layer("L2", 1, [_item("i2", "c_ok", 0.0)]),        # 正常
    ]
    rt = tl.CueRuntime(_sheet(te, media_ids=("c_gone", "c_ok")), te,
                       media_paths={"c_ok": "P:/x/ok.mp4"})   # 只给 L2 的路径
    d = {s.lyr_id: s for s in rt.update(22.0)}
    assert len(d) == 2, "整条链不许断：两层的 state 都要返回"
    assert d["L1"].active and d["L1"].missing, d["L1"]
    assert d["L2"].active and not d["L2"].missing, d["L2"]
    print("  ✓ E3 缺素材：只标 L1.missing，L2 正常，链未断")


def test_play_sec_speed_d4():
    # ⚠ 防回归：`× speed` 与演出端一致（speed=倍速）。旧运行时 cuesheet_runtime.py:174 是 `/speed`
    #   （慢放），与它自己的播放器语义相反，是旧实现的缺陷 —— 若有人"改回除法对齐旧实现"，
    #   本项断言会立刻失败（speed=2 将得 2.0 而非 8.0）。
    te = _track()
    te.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 10.0, src_in_sec=2.0, speed=2.0, trans_in="cut", trans_out="cut"),
    ]))
    # 13.0s：已播 3.0s ⇒ play_sec = 2.0 + 3.0×2.0 = 8.0（× speed）
    st = tl.CueRuntime(_sheet(te), te, _paths("c1")).update(13.0)[0]
    assert abs(st.play_sec - 8.0) < 1e-9, st
    assert abs(st.speed - 2.0) < 1e-9 and abs(st.src_in_sec - 2.0) < 1e-9, st

    # speed=0.5 ⇒ play_sec = 2.0 + 3.0×0.5 = 3.5
    te2 = _track()
    te2.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 10.0, src_in_sec=2.0, speed=0.5, trans_in="cut", trans_out="cut"),
    ]))
    st = tl.CueRuntime(_sheet(te2), te2, _paths("c1")).update(13.0)[0]
    assert abs(st.play_sec - 3.5) < 1e-9, st
    print("  ✓ D4 变速折算：speed=2 ⇒ play_sec=8.0；speed=0.5 ⇒ play_sec=3.5")


def test_end_sec_deactivates():
    te = _track()
    te.composition.layers.append(_layer("L1", 0, [
        _item("i1", "c1", 0.0, end_sec=20.0, trans_in="cut", trans_out="cut"),
    ]))
    rt = tl.CueRuntime(_sheet(te), te, _paths("c1"))
    st = rt.update(19.0)[0]
    assert st.active and not st.ended, st
    st = rt.update(25.0)[0]
    assert (not st.active) and st.ended, st
    print("  ✓ end_sec 到期：19s 激活、25s 失活且 ended=True")


# --------------------------------------------------------------------------
def main() -> int:
    print("=" * 68)
    print("M2 · 时间轴解释器测试  (python %s)" % sys.version.split()[0])
    print("=" * 68)
    tests = [
        ("零 Qt（AST）", test_zero_qt_ast),
        ("E4 逐帧单调推进", test_frame_by_frame_transition),
        ("E1 回滞", test_hysteresis_no_flip),
        ("E2 前跳不做过渡", test_big_jump_forward_no_transition),
        ("E2 倒退跳变", test_big_jump_backward),
        ("D3 z 升序", test_z_order_contract_d3),
        ("E3 缺素材跳过", test_missing_skips_only_that_item_e3),
        ("D4 变速折算", test_play_sec_speed_d4),
        ("end_sec 失活", test_end_sec_deactivates),
    ]
    ok_all = True
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            ok_all = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
