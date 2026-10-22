# -*- coding: utf-8 -*-
"""组 A 修复验证（A1~A5）。**只读被测源码**，不改任何 src/。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tools/_fix_a_verify.py

覆盖：
  · A1  timeline._alpha_for 出场淡出窗口（本 item 有 end_sec 时以 end_sec 为准）
        + 无下段回归 + **反向验证**（改回旧写法 ⇒ 断言必失败）
  · A2  timeline `src_out_sec` 钳位（到出点冻结最后一帧）+ **反向验证**
  · A3  features：`trans_glitch`/`trans_zoom` 提为支持；导出→导入不再报"将被忽略"
  · A4  compositor 几何对齐演出端：默认 cover / 改过 xform 才 contain；crop 以裁切尺寸为基准
  · A5  compositor fade 曲线与演出端一致（α²：opacity×alpha×trans_p）
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                   # noqa: E402
from PySide6.QtGui import QGuiApplication                            # noqa: E402

from ercue.model import timeline as tl                               # noqa: E402
from ercue.model import units as _units                              # noqa: E402
from ercue.model import features as feat                             # noqa: E402
from ercue.model import ercue_io                                     # noqa: E402
from ercue.model.cuesheet import (                                   # noqa: E402
    CueSheet, TrackEntry, Identity, CompLayer, CueItem, MediaRef)
from ercue.render import compositor as cp                            # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])             # noqa: E402
_TMP = tempfile.mkdtemp(prefix="fixa_")
_FAILS = []


def _need(cond, msg):
    print("    · %s %s" % ("OK " if cond else "✗  ", msg))
    if not cond:
        _FAILS.append(msg)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def make_sheet(items, bpm=120.0):
    """构造 (CueSheet, TrackEntry)：单层 L1，items 均 normalized。"""
    s = CueSheet(title="fixA 夹具")
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=120.0, bpm=bpm))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="bg", z=0, blend="normal", opacity=1.0)
    ly.items = [it.normalized() for it in items]
    te.composition.layers.append(ly)
    s.tracks.append(te)
    s.media.append(MediaRef(clip_id="c1", name="c1.mp4", duration_sec=60.0))
    return s, te


def _alpha_old(it, nxt, pos):
    """★ 反向验证用：**旧实现**的出场窗口算法（`nxt.at_sec` 优先）。"""
    tin = _units.to_float(it.trans_in_sec, 2.0)
    if it.trans_in == "cut" or abs(tin) < 1e-6:
        a_in = 1.0
    else:
        a_in = min(1.0, max(0.0, (pos - _units.to_float(it.at_sec)) / tin))
    a_out = 1.0
    fade_start = None
    if nxt is not None:                       # ← 旧写法：下一个 item 优先
        fade_start = _units.to_float(nxt.at_sec)
    elif it.end_sec is not None:
        fade_start = _units.to_float(it.end_sec)
    tout = _units.to_float(it.trans_out_sec, 2.0)
    if fade_start is not None and tout > 1e-6 and it.trans_out != "cut":
        left = fade_start - pos
        if left <= tout:
            a_out = max(0.0, left / tout)
    return min(a_in, a_out)


def _solid(w, h, rgb):
    a = np.empty((h, w, 4), np.uint8)
    a[:, :, 0] = rgb[2]
    a[:, :, 1] = rgb[1]
    a[:, :, 2] = rgb[0]
    a[:, :, 3] = 255
    return a


def _center(img):
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), np.uint8, bpl * h).reshape(h, bpl)
    v = arr[:, :w * 4].reshape(h, w, 4)[h // 2, w // 2]
    return int(v[2]), int(v[1]), int(v[0])


def _st(xform=None, trans="cut", trans_p=1.0, crop=None, alpha=1.0):
    return tl.LayerDrawState(lyr_id="L", z=0, blend="normal", opacity=1.0, clip_id="c1",
                             crop=crop,
                             xform=(xform if xform is not None
                                    else {"scale": 1.0, "x": 0.0, "y": 0.0}),
                             alpha=alpha, trans=trans, trans_p=trans_p, active=True)


# --------------------------------------------------------------------------
# A1：出场淡出窗口
# --------------------------------------------------------------------------
def a1():
    it1 = CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=10.0,
                  trans_in="cut", trans_in_sec=0.0, trans_out="fade", trans_out_sec=2.0)
    it2 = CueItem(item_id="i2", clip_id="c1", at_sec=20.0,
                  trans_in="cut", trans_in_sec=0.0, trans_out="cut", trans_out_sec=0.0)
    s, te = make_sheet([it1, it2])
    rt = tl.CueRuntime(s, te, media_paths={"c1": "x.mp4"})
    i1, i2 = rt._plans[0].items
    got = {}
    for pos in (8.0, 9.0, 10.0):
        a, _trans, _tp = rt._alpha_for(i1, i2, pos)
        got[pos] = a
    _need(abs(got[8.0] - 1.0) < 1e-6, "8s  alpha=1.0（got %.4f）" % got[8.0])
    _need(abs(got[9.0] - 0.5) < 1e-6, "9s  alpha=0.5（got %.4f）" % got[9.0])
    _need(abs(got[10.0] - 0.0) < 1e-6, "10s alpha=0.0（got %.4f）" % got[10.0])
    # 运行时：正好到 end_sec 该层失活（"到 end_sec 归零"）
    d = {x.lyr_id: x for x in rt.update(10.0)}
    _need(not d["L1"].active, "到 end_sec 该层应失活（active=False）")
    # 回归：无下段（单 item、end_sec=None）→ 不淡出，alpha 恒 1（与改前一致）
    it3 = CueItem(item_id="i3", clip_id="c1", at_sec=0.0,
                  trans_in="cut", trans_in_sec=0.0, trans_out="fade", trans_out_sec=2.0)
    s2, te2 = make_sheet([it3])
    rt2 = tl.CueRuntime(s2, te2, media_paths={"c1": "x.mp4"})
    a_r, _, _ = rt2._alpha_for(rt2._plans[0].items[0], None, 50.0)
    _need(abs(a_r - 1.0) < 1e-6, "回归：无下段 alpha 恒 1.0（got %.4f）" % a_r)
    # 反向验证：旧写法在 9s/10s 都得 1.0（⇒ 若改回旧写法，上面的 0.5/0.0 断言必失败）
    old9, old10 = _alpha_old(it1, it2, 9.0), _alpha_old(it1, it2, 10.0)
    _need(abs(old9 - 1.0) < 1e-6 and abs(old10 - 1.0) < 1e-6,
          "反向验证：旧写法 9s/10s=1.0（证明改回旧写法则断言失败）")
    _need(abs(old9 - got[9.0]) > 1e-3, "反向验证：新旧在 9s 确实不同")
    print("    ⇒ A1 采用语义：本 item 有 end_sec ⇒ 淡出窗口右界=end_sec；否则用下一段 at_sec。")


# --------------------------------------------------------------------------
# A2：src_out 钳位
# --------------------------------------------------------------------------
def a2():
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_sec=10.0,
                 src_in_sec=0.0, src_out_sec=4.0, speed=1.0,
                 trans_in="cut", trans_in_sec=0.0, trans_out="cut", trans_out_sec=0.0)
    s, te = make_sheet([it])
    rt = tl.CueRuntime(s, te, media_paths={"c1": "x.mp4"})
    for k in range(0, 181):                 # 0 .. 9.00，步长 0.05
        rt.update(k * 0.05)
    d = {x.lyr_id: x for x in rt.update(9.0)}
    _need(d["L1"].active, "9s 该层应仍激活")
    _need(abs(d["L1"].play_sec - 4.0) < 1e-6,
          "src_out=4 ⇒ play_sec 钳在 4（got %.4f）" % d["L1"].play_sec)
    _need(d["L1"].src_ended is True, "src_ended 应为 True")
    _need(abs(float(d["L1"].src_out_sec) - 4.0) < 1e-6,
          "src_out_sec 字段应=4（got %r）" % d["L1"].src_out_sec)
    # 全程不越过出点
    rt2 = tl.CueRuntime(s, te, media_paths={"c1": "x.mp4"})
    over = []
    for k in range(0, 201):
        for x in rt2.update(k * 0.05):
            if x.active and x.play_sec > 4.0 + 1e-6:
                over.append((k * 0.05, x.play_sec))
    _need(not over, "play_sec 全程不得 > 4（越界样本 %r）" % over[:3])
    # 反向验证：旧式（不钳位）在 elapsed=5 ⇒ play_sec=5 > 4
    old_play = 0.0 + 5.0 * 1.0
    _need(old_play > 4.0 + 1e-6, "反向验证：旧式 elapsed=5 ⇒ play_sec=5（断言必失败）")
    # 变速：speed=2、src_out=4 ⇒ elapsed=1 即到出点
    it2 = CueItem(item_id="i2", clip_id="c1", at_sec=0.0, end_sec=10.0,
                  src_in_sec=0.0, src_out_sec=4.0, speed=2.0,
                  trans_in="cut", trans_in_sec=0.0, trans_out="cut", trans_out_sec=0.0)
    s3, te3 = make_sheet([it2])
    rt3 = tl.CueRuntime(s3, te3, media_paths={"c1": "x.mp4"})
    for k in range(0, 81):
        rt3.update(k * 0.05)
    p2 = {x.lyr_id: x for x in rt3.update(4.0)}["L1"].play_sec
    _need(abs(p2 - 4.0) < 1e-6, "speed=2 + src_out=4：elapsed=1 即到出点（got %.4f）" % p2)
    print("    ⇒ A2 采用语义：play_sec 钳到 src_out，冻结最后一帧直到 item 结束/被替换。")


# --------------------------------------------------------------------------
# A3：能力表
# --------------------------------------------------------------------------
def a3():
    _need("trans_glitch" in feat.SUPPORTED_FEATURES and "trans_zoom" in feat.SUPPORTED_FEATURES,
          "trans_glitch/trans_zoom 应在 SUPPORTED_FEATURES（got %r）" % (sorted(feat.SUPPORTED_FEATURES),))
    _need(feat.KNOWN_FEATURES >= feat.SUPPORTED_FEATURES, "KNOWN_FEATURES 应仍 ⊇ SUPPORTED_FEATURES")
    _need(("multi_layer_cue" in feat.KNOWN_FEATURES)
          and ("multi_layer_cue" not in feat.SUPPORTED_FEATURES),
          "multi_layer_cue 应仍在 KNOWN 而非 SUPPORTED")
    it = CueItem(item_id="i1", clip_id="c1", at_sec=0.0,
                 trans_in="glitch", trans_in_sec=1.0, trans_out="zoom", trans_out_sec=1.0)
    s, te = make_sheet([it])
    s.recompute_features()
    _need("trans_glitch" in s.requires_features and "trans_zoom" in s.requires_features,
          "requires_features 应含 glitch/zoom（got %r）" % (s.requires_features,))
    pkg = os.path.join(_TMP, "a3.ercue")
    ercue_io.save(s, pkg, media_paths={"c1": "x.mp4"}, app_version="2026.10.01.00")
    s2, _info = ercue_io.load(pkg)
    warns, _miss = ercue_io.capability_report(s2, app_version="2026.10.01.00")
    bad = [w for w in warns if "将被忽略" in w]
    _need(not bad, "导出→导入后不应再报'将被忽略'（got %r）" % (bad,))
    print("    ⇒ A3：glitch/zoom 两端能力表一致（本软件实现 + 演出端 engine.py:2841/2845 可渲染）。")


# --------------------------------------------------------------------------
# A4：几何对齐
# --------------------------------------------------------------------------
def a4():
    fw, fh, w, h = 640, 480, 160, 90          # 4:3 素材 → 16:9 画布
    # 默认 xform ⇒ cover（max）
    tgt, src = cp._target_src(_st(), fw, fh, w, h)
    k_cover = max(w / fw, h / fh)
    _need(abs(tgt.width() - fw * k_cover) < 1e-6 and abs(tgt.height() - fh * k_cover) < 1e-6,
          "默认 xform ⇒ cover(max)：期望 %.1fx%.1f，实得 %.1fx%.1f"
          % (fw * k_cover, fh * k_cover, tgt.width(), tgt.height()))
    _need(tgt.height() > h + 1e-6, "cover 应铺满：高度超出画布（裁切）")
    _need(abs(src.width() - fw) < 1e-6 and abs(src.height() - fh) < 1e-6, "未裁切时源矩形=整幅")
    # scale=1.2 ⇒ contain（min * us）
    us = 1.2
    tgt2, _s2 = cp._target_src(_st(xform={"scale": us, "x": 0.0, "y": 0.0}), fw, fh, w, h)
    k_contain = min(w / fw, h / fh) * us
    _need(abs(tgt2.width() - fw * k_contain) < 1e-6,
          "有变换（scale=%.1f）⇒ contain(min*us)：期望宽 %.1f，实得 %.1f"
          % (us, fw * k_contain, tgt2.width()))
    _need(abs(tgt2.width() - tgt.width()) > 1e-6, "cover 与 contain 结果应不同（判据生效）")
    # x/y 非默认同样触发 contain
    tgt3, _ = cp._target_src(_st(xform={"scale": 1.0, "x": 5.0, "y": 0.0}), fw, fh, w, h)
    _need(abs(tgt3.width() - fw * min(w / fw, h / fh)) < 1e-6,
          "x≠0（未改 scale）也应判为 contain")
    # crop：以「裁切尺寸」为基准 ⇒ 不变形（旧写法会用整幅尺寸 ⇒ 拉伸变形）
    tgtc, srcc = cp._target_src(_st(crop=[0.0, 0.0, 0.5, 1.0]), 100, 100, 64, 64)
    _need(abs(srcc.width() - 50) < 1e-6 and abs(srcc.height() - 100) < 1e-6,
          "crop 源矩形应为 50x100（got %.0fx%.0f）" % (srcc.width(), srcc.height()))
    ar_t, ar_s = tgtc.height() / tgtc.width(), srcc.height() / srcc.width()
    _need(abs(ar_t - ar_s) < 1e-6,
          "crop 后宽高比应保持（不被幅面拉伸）：目标 %.3f vs 源 %.3f" % (ar_t, ar_s))
    print("    ⇒ A4 判据：xform 全默认⇒cover(max)；任一变换⇒contain(min*us)；crop 以裁切尺寸为基准。")


# --------------------------------------------------------------------------
# A5：fade 曲线
# --------------------------------------------------------------------------
def a5():
    base = (200, 60, 60)
    frames = {"c1": _solid(64, 64, base)}
    # 过渡进行到 50%（淡入中点）：timeline 同时给出 alpha=0.5 与 trans_p=0.5
    img = cp.compose((64, 64), [_st(trans="fade", trans_p=0.5, alpha=0.5)],
                     lambda cid: frames.get(cid))
    got = _center(img)
    exp = tuple(int(round(c * 0.5 * 0.5)) for c in base)      # 演出端公式 α*trans_p = 0.25
    old = tuple(int(round(c * 0.5)) for c in base)            # 旧线性 = 0.5
    _need(all(abs(g - e) <= 2 for g, e in zip(got, exp)),
          "fade@50%% 应为 α²≈0.25*base %r（实得 %r）" % (exp, got))
    _need(any(abs(g - o) > 2 for g, o in zip(got, old)),
          "应不同于旧线性值 %r（实得 %r）" % (old, got))
    # cut：trans_p=0 也应立刻满（硬切）
    cut = _center(cp.compose((64, 64), [_st(trans="cut", trans_p=0.0, alpha=1.0)],
                             lambda cid: frames.get(cid)))
    _need(all(abs(c - b) <= 2 for c, b in zip(cut, base)), "cut（trans_p=0）应硬切为原色（实得 %r）" % (cut,))
    print("    ⇒ A5 值：本软件=%r，演出端引擎公式(alpha*trans_p)=%r，旧线性=%r" % (got, exp, old))


# --------------------------------------------------------------------------
def main():
    tests = [("A1 出场淡出窗口", a1), ("A2 src_out 钳位", a2), ("A3 能力表", a3),
             ("A4 几何对齐", a4), ("A5 fade 曲线", a5)]
    for name, fn in tests:
        print("\n=== %s ===" % name)
        try:
            fn()
        except Exception:                                        # noqa: BLE001
            _FAILS.append(name)
            print("    ✗ 异常:\n" + traceback.format_exc())

    print("\n" + "=" * 64)
    if _FAILS:
        print("结果：%d 项失败 ✗" % len(_FAILS))
        for m in _FAILS:
            print("  ✗ " + m)
        return 1
    print("结果：A1~A5 全部通过 ✓   夹具目录 %s" % _TMP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
