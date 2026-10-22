# -*- coding: utf-8 -*-
"""M2 · 预览合成器测试（Qt 光栅路线）。

跑法（无头）：
    set QT_QPA_PLATFORM=offscreen
    P:\\AutoVJ\\venv\\Scripts\\python.exe  P:\\EasyRealityCue\\tests\\test_compositor.py

覆盖（对应验收标准）：
  · 7 种混合的**真实像素值**（normal/add/screen/multiply/lighten/darken + subtract 逐像素）
  · **几何对齐演出端**（★ 2026-10-11 修订）：xform 全默认 ⇒ cover(铺满裁切)；
    任一变换 ⇒ contain(完整显示)；crop 以裁切尺寸为基准不变形
  · **D2** alpha 只乘一次：opacity=1.0, alpha=0.5, trans=cut ⇒ 输出 ≈ 原色 × 0.5
  · crop：裁切区域外的像素不被写上（源矩形重载）
  · 过渡：fade α²（对齐演出端 `engine.py:2835`：opacity×alpha×trans_p）；cut 是硬切
  · 性能：1080p 三层合成单帧 < 40 ms（实测基线 6~13 ms）
"""

from __future__ import annotations

import os
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                    # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage                     # noqa: E402

from ercue.model import timeline as tl                               # noqa: E402
from ercue.render import compositor as cp                            # noqa: E402

_APP = QGuiApplication.instance() or QGuiApplication([])

# 报告实测基准（技术选型 §B7）：dst=(40,180,90) src=(200,60,60)
DST = (40, 180, 90)
SRC = (200, 60, 60)
EXPECT = {
    "normal":   (200, 60, 60),
    "add":      (240, 240, 150),
    "screen":   (209, 198, 129),
    "multiply": (31, 42, 21),
    "lighten":  (200, 180, 90),
    "darken":   (40, 60, 60),
    # ⚠ 防回归：subtract 的正确式是 max(0, dst−src)。
    #   若误用 技术选型.md §B7 的测速近似式 np.maximum(dst,255−src)，本项会得 (55,195,195) → 断言失败。
    "subtract": (0, 120, 30),        # max(0, dst-src) 分量饱和减
}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def solid(w, h, rgb):
    """构造 BGRA 纯色帧 `(h, w, 4)`。"""
    a = np.empty((h, w, 4), np.uint8)
    a[:, :, 0] = rgb[2]      # B
    a[:, :, 1] = rgb[1]      # G
    a[:, :, 2] = rgb[0]      # R
    a[:, :, 3] = 255         # A
    return a


def np_view(img: QImage):
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    arr = np.frombuffer(img.bits(), np.uint8, bpl * h).reshape(h, bpl)
    return arr[:, :w * 4].reshape(h, w, 4)


def px(img: QImage, x, y):
    """读像素 → (R, G, B, A)。"""
    v = np_view(img)[y, x]
    return int(v[2]), int(v[1]), int(v[0]), int(v[3])


def state(z, clip_id, blend="normal", opacity=1.0, alpha=1.0, trans="cut",
          trans_p=1.0, crop=None, scale=1.0, missing=False, active=True):
    return tl.LayerDrawState(lyr_id="L%d" % z, z=z, blend=blend, opacity=opacity,
                             clip_id=clip_id, item_id="i%d" % z, missing=missing,
                             crop=crop, xform={"scale": scale, "x": 0.0, "y": 0.0},
                             alpha=alpha, trans=trans, trans_p=trans_p, active=active)


def near(got, want, tol=1):
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(got, want))


# --------------------------------------------------------------------------
# 1) 7 种混合的真实像素值
# --------------------------------------------------------------------------
def test_blend_modes_pixels():
    size = (64, 64)
    frames = {"dst": solid(64, 64, DST), "src": solid(64, 64, SRC)}
    lookup = lambda cid: frames.get(cid)
    print("  混合模式      Qt/实现                            中心像素(RGB)      期望")
    for mode in ("normal", "add", "screen", "multiply", "lighten", "darken", "subtract"):
        states = [state(0, "dst", "normal"), state(1, "src", mode)]
        canvas = cp.compose(size, states, lookup)
        got = px(canvas, 32, 32)[:3]
        ok = near(got, EXPECT[mode])
        print("    %-10s  %-34s %-18s %s %s"
              % (mode, ("CompositionMode_" + cp.BLEND_QT[mode].name.split("_")[-1]
                        if mode in cp.BLEND_QT else "numpy 饱和减(max(0,dst-src))"),
                 str(got), str(EXPECT[mode]), "✓" if ok else "✗"))
        assert ok, "%s 合成像素错：got=%s want=%s" % (mode, got, EXPECT[mode])

    # subtract + opacity<1（走 numpy LUT 路径）：max(0, dst - src*0.5)
    canvas = cp.compose(size, [state(0, "dst", "normal"),
                               state(1, "src", "subtract", opacity=0.5)], lookup)
    got = px(canvas, 32, 32)[:3]
    want = (0, 150, 60)
    print("    %-10s  %-34s %-18s %s %s"
          % ("subtract×0.5", "numpy 饱和减 + 透明度 LUT", str(got), str(want),
             "✓" if near(got, want) else "✗"))
    assert near(got, want), "subtract×0.5 错：got=%s want=%s" % (got, want)

    # 多个 subtract 层必须**逐层累计**（不能共用 scratch 只剩最后一层）
    frames["src2"] = solid(64, 64, (20, 30, 40))
    canvas = cp.compose(size, [state(0, "dst", "normal"),
                               state(1, "src", "subtract"),
                               state(2, "src2", "subtract")], lookup)
    got = px(canvas, 32, 32)[:3]
    want = (0, 90, 0)     # (40,180,90)-(200,60,60)=(0,120,30) 再 -(20,30,40)= (0,90,0)
    print("    %-10s  %-34s %-18s %s %s"
          % ("subtract×2", "两层 subtract 累计", str(got), str(want),
             "✓" if near(got, want) else "✗"))
    assert near(got, want), "两层 subtract 未累计：got=%s want=%s" % (got, want)
    print("  ✓ 7 种混合像素值（含 subtract 透明度/多层累计）全部符合各自数学定义")


# --------------------------------------------------------------------------
# 2) 几何对齐演出端：默认 cover（铺满裁切）/ 有变换 contain（完整显示）
# --------------------------------------------------------------------------
def test_geometry_matches_engine():
    """★ 2026-10-11 修订：适配判据改为**对齐演出端**（`engine.py:2774 / 2799-2802`）。

    旧断言"永远 contain"，是本次（团队裁定，用户确认方向）**有意推翻**的旧设计：
      · xform 全默认（scale/x/y/rot 均默认）⇒ **cover**：`max(W/iw,H/ih)`，等比铺满 + 裁切；
      · 任一变换 ⇒ **contain**：`min(W/iw,H/ih) * 用户 scale`，完整显示（+ 用户缩放）。
    （演出端 `max()` 是等比 cover，**不变形**；它不会把画面拉成双向拉伸。）
    """
    W, H = 320, 180                      # 16:9 画布
    fw, fh = 400, 300                    # 4:3 素材
    frames = {"c1": solid(fw, fh, (10, 200, 50))}

    # ---- (a) 默认 xform ⇒ cover（铺满 + 裁切）----
    canvas = cp.compose((W, H), [state(0, "c1", "normal")], lambda cid: frames.get(cid))
    view = np_view(canvas)
    lit = view[:, :, 0].astype(np.int16) > 8        # 非黑（B 通道 > 8）
    ys, xs = np.where(lit)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    print("    [cover] bbox %dx%d  宽高比=%.4f（期望 16/9=1.7778）" % (bw, bh, bw / float(bh)))
    # 2026-10-11：几何改为对齐演出端（engine.py:2774/2799-2802）——xform 全默认 ⇒ cover(max)
    assert bw == W and bh == H, "默认 xform ⇒ cover 应铺满整幅画布（got %dx%d）" % (bw, bh)
    assert abs(bw / float(bh) - 16.0 / 9.0) < 0.03, "cover 后铺满画布，宽高比应为 16:9"

    # ---- (b) 有变换（scale=0.8）⇒ contain（完整显示 + 居中）----
    canvas2 = cp.compose((W, H), [state(0, "c1", "normal", scale=0.8)],
                         lambda cid: frames.get(cid))
    view2 = np_view(canvas2)
    lit2 = view2[:, :, 0].astype(np.int16) > 8
    ys2, xs2 = np.where(lit2)
    a0, a1, b0, b1 = int(xs2.min()), int(xs2.max()), int(ys2.min()), int(ys2.max())
    bw2, bh2 = a1 - a0 + 1, b1 - b0 + 1
    print("    [contain] bbox %dx%d  宽高比=%.4f（期望 4/3=1.3333）" % (bw2, bh2, bw2 / float(bh2)))
    # 2026-10-11：任一变换（scale≠1）⇒ contain(min*us)：k=min(320/400,180/300)*0.8=0.48 ⇒ 192x144
    assert abs(bw2 - 192) <= 1 and abs(bh2 - 144) <= 1, \
        "scale=0.8 ⇒ contain 应得 192x144（got %dx%d）" % (bw2, bh2)
    assert abs(bw2 / float(bh2) - 4.0 / 3.0) < 0.03, "contain 宽高比应保持 4:3（未变形）"
    assert abs(a0 - (W - bw2) / 2.0) <= 1 and abs(b0 - (H - bh2) / 2.0) <= 1, \
        "contain 应居中（左/上留黑边）"
    print("  ✓ 几何对齐演出端：默认 xform=cover(铺满裁切)；有变换=contain(完整显示不变形)")


# --------------------------------------------------------------------------
# 3) D2 alpha 只乘一次
# --------------------------------------------------------------------------
def test_alpha_multiplied_once():
    frames = {"c1": solid(64, 64, (100, 150, 200))}
    canvas = cp.compose((64, 64), [state(0, "c1", "normal", opacity=1.0, alpha=0.5)],
                        lambda cid: frames.get(cid))
    got = px(canvas, 32, 32)[:3]
    want = (50, 75, 100)                 # 原色 × 0.5
    # 注：本用例 trans="cut"（默认），只验证**基础项** opacity×alpha 不被多乘（D2）。
    #     fade 过渡另按演出端公式再乘 trans_p（α²）——见 test_transitions_fade_and_cut。
    wrong = (25, 37, 50)                 # 原色 × 0.25（基础项被多乘一次的旧 bug）
    print("    opacity=1.0, alpha=0.5 ⇒ 输出=%s  期望=%s  错误值(基础项平方)=%s" % (got, want, wrong))
    assert near(got, want), "alpha 应只乘一次：got=%s" % (got,)
    assert not near(got, wrong), "疑似乘了两次（平方曲线）"
    print("  ✓ D2 alpha 只乘一次：输出 ≈ 原色 × 0.5，不是 × 0.25")


# --------------------------------------------------------------------------
# 4) crop：裁切区域外的像素不被写上
# --------------------------------------------------------------------------
def test_crop_outside_not_written():
    fw = fh = 64
    src = solid(fw, fh, (0, 0, 255))                 # 外面：蓝 (B=255)
    src[:32, :32] = solid(32, 32, (255, 0, 0))       # 左上象限：红
    frames = {"c1": src}
    # crop 取左上 1/4（源矩形重载），画布与素材同为 64x64 ⇒ src 区被放大铺满
    canvas = cp.compose((64, 64), [state(0, "c1", "normal", crop=[0.0, 0.0, 0.5, 0.5])],
                        lambda cid: frames.get(cid))
    view = np_view(canvas)
    blue = int(((view[:, :, 0] > 200) & (view[:, :, 2] < 60)).sum())   # 蓝像素计数
    center = px(canvas, 32, 32)[:3]
    print("    画布中心=%s（期望红 (255,0,0)）；残留蓝色像素数=%d（期望 ~0）" % (center, blue))
    assert near(center, (255, 0, 0)), "中心应为裁切内容（红）"
    assert blue <= 4, "裁切区域外的像素不应被写上（残留蓝=%d）" % blue
    print("  ✓ crop：裁切区外的源像素未被写入画布（中心为裁切内容）")


# --------------------------------------------------------------------------
# 5) 过渡：fade α²（对齐演出端）+ cut 硬切
# --------------------------------------------------------------------------
def test_transitions_fade_and_cut():
    """★ 2026-10-11 修订：fade 期望值由"线性 ×0.5"改为 **α²**。

    依据：演出端 `engine.py:2832-2835` 的 fade 分支 `paint_cover(..., alpha * prog)`
    （`alpha = base*opacity*ps.alpha` 已含淡入量，`prog = ps.trans_p`）⇒ 淡入中点
    `alpha=0.5, trans_p=0.5` 实际画在 `0.5*0.5 = 0.25`。本次 A5 让预览与演出端**同式**。
    """
    frames = {"c1": solid(64, 64, (200, 100, 50))}
    lookup = lambda cid: frames.get(cid)                              # noqa: E731
    base = (200, 100, 50)
    v0 = px(cp.compose((64, 64), [state(0, "c1", "normal", alpha=0.0,
                                   trans="fade", trans_p=0.0)], lookup), 32, 32)[:3]
    v5 = px(cp.compose((64, 64), [state(0, "c1", "normal", alpha=0.5,
                                   trans="fade", trans_p=0.5)], lookup), 32, 32)[:3]
    v1 = px(cp.compose((64, 64), [state(0, "c1", "normal", alpha=1.0,
                                   trans="fade", trans_p=1.0)], lookup), 32, 32)[:3]
    print("    fade  alpha×trans_p = 0.0 / (0.5×0.5) / 1.0 → %s / %s / %s" % (v0, v5, v1))
    assert near(v0, (0, 0, 0)), "fade p=0 应为黑"
    # 2026-10-11：对齐 engine.py:2835（opacity×alpha×trans_p）——中点 = 0.5×0.5 = 0.25×base
    assert near(v5, tuple(int(round(c * 0.25)) for c in base), tol=2), \
        "fade 中点应为 α²≈0.25×base：got=%s" % (v5,)
    assert near(v1, base), "fade p=1 应为原色"

    # cut：trans_p=0 也应立刻满不透明（硬切，无渐变）——回归不动
    cut = px(cp.compose((64, 64), [state(0, "c1", "normal", alpha=1.0,
                                        trans="cut", trans_p=0.0)], lookup), 32, 32)[:3]
    print("    cut   trans_p=0.0 → %s（硬切，应立即为原色）" % (cut,))
    assert near(cut, base), "cut 应硬切：got=%s" % (cut,)

    # slide / zoom / glitch 至少能出图且进度 1.0 时与原色一致（不崩、不黑整幅）
    # 2026-10-11：这三者**不**额外乘 trans_p（演出端只在 fade 分支乘 prog），故 p=1 应为原色
    for t in ("slide", "zoom", "glitch"):
        got = px(cp.compose((64, 64), [state(0, "c1", "normal", alpha=1.0, trans=t,
                                            trans_p=1.0)], lookup), 32, 32)[:3]
        assert near(got, base), "%s 在 p=1 应收敛到原色：got=%s" % (t, got)
    print("  ✓ 过渡：fade α²（对齐演出端）、cut 硬切、slide/zoom/glitch 在 p=1 收敛到原色")


# --------------------------------------------------------------------------
# 6) 性能：1080p 三层
# --------------------------------------------------------------------------
def test_perf_1080p_three_layers():
    W, H = 1920, 1080
    rng = np.random.default_rng(7)
    frames = {("c%d" % i): np.ascontiguousarray(
        rng.integers(0, 256, (H, W, 4), dtype=np.uint8)) for i in range(3)}
    for f in frames.values():
        f[:, :, 3] = 255
    lookup = lambda cid: frames.get(cid)                              # noqa: E731
    states = [
        state(0, "c0", "normal", opacity=0.85, crop=[0.05, 0.0, 0.9, 1.0]),
        state(1, "c1", "screen", opacity=0.85),
        state(2, "c2", "add", opacity=0.85, scale=0.7),
    ]
    cp.compose((W, H), states, lookup)                # 预热
    n = 10
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        cp.compose((W, H), states, lookup)
        ts.append((time.perf_counter() - t0) * 1000.0)
    mean = float(np.mean(ts))
    p90 = float(np.percentile(ts, 90))
    print("    1920x1080 三层（normal+screen+add，含 crop/scale）: %.2f ms/帧 (p90 %.2f) → %.1f fps"
          % (mean, p90, 1000.0 / mean))
    assert mean < 40.0, "1080p 三层合成超预算：%.2f ms/帧" % mean
    print("  ✓ 性能：%.2f ms/帧 < 40 ms 预算（基线 6~13 ms）" % mean)


# --------------------------------------------------------------------------
# 7) 集成：时间轴解释器 → 合成器（契约贯通）
# --------------------------------------------------------------------------
def test_timeline_to_compositor_integration():
    from ercue.model.cuesheet import (CueSheet, TrackEntry, Identity,
                                      CompLayer, CueItem, MediaRef)
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=100.0, bpm=120.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="bg", z=0, blend="normal", opacity=1.0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=1.0,
                            trans_in="fade", trans_in_sec=2.0,
                            trans_out="fade", trans_out_sec=2.0).normalized())
    te.composition.layers.append(ly)
    sheet = CueSheet(title="it")
    sheet.tracks.append(te)
    sheet.media.append(MediaRef(clip_id="c1", name="c1.mp4"))
    rt = tl.CueRuntime(sheet, te, {"c1": "P:/x/c1.mp4"})

    frames = {"c1": solid(64, 64, (200, 100, 50))}
    lookup = lambda cid: frames.get(cid)                              # noqa: E731

    p = 0.0
    while p <= 4.0 + 1e-9:                                 # 逐帧单调推进
        states = rt.update(p)
        canvas = cp.compose((64, 64), states, lookup)
        assert canvas is not None and not canvas.isNull()
        p += 1.0 / 30.0
    # 2.0s = 淡入中点 ⇒ runtime 给 alpha=0.5, trans_p=0.5；合成按演出端 fade 公式
    # ★ 2026-10-11：fade 改为 α²（对齐 engine.py:2835）⇒ 中点 = 0.5×0.5 = 0.25×base（旧断言是 0.5×base）
    rt2 = tl.CueRuntime(sheet, te, {"c1": "P:/x/c1.mp4"})
    mid = px(cp.compose((64, 64), rt2.update(2.0), lookup), 32, 32)[:3]
    assert near(mid, (50, 25, 13), tol=2), "集成淡入中点应 ≈ 原色×0.25（α²）：got=%s" % (mid,)
    print("  ✓ 集成：逐帧 update→compose 全程无异常；淡入中点输出=%s ≈ 原色×0.25（α²）" % (mid,))


# --------------------------------------------------------------------------
# 7) frame_lookup 签名契约（M4）：双参数 (clip_id, play_sec) + 旧单参回调兼容
# --------------------------------------------------------------------------
def test_frame_lookup_signature_and_override():
    """同一素材在两层的**同一帧**、以**不同 play_sec** 使用时，两层必须各取各的帧。

    旧契约 `frame_lookup(clip_id)` 只按 clip 取帧 ⇒ 后者覆盖前者（M3 记录的缺陷）。
    新契约把 `play_sec` 一起传下去。同时断言旧的**单参回调不崩**（向后兼容）。
    """
    # 一个"按 play_sec 变色的素材"：play_sec≈0 红、play_sec≈5 蓝
    def gen(play_sec):
        return solid(32, 32, (255, 0, 0) if float(play_sec) < 2.5 else (0, 0, 255))

    seen = []

    def lookup2(cid, play_sec):                       # 新契约：双参数
        seen.append((cid, round(float(play_sec), 3)))
        return gen(play_sec)

    # 下层 z=0：play_sec=0（红）；上层 z=1：play_sec=5（蓝）。上层不透明 ⇒ 中心应为蓝。
    s0 = state(0, "c1", "normal"); s0.play_sec = 0.0
    s1 = state(1, "c1", "normal"); s1.play_sec = 5.0
    canvas = cp.compose((32, 32), [s0, s1], lookup2)
    got = px(canvas, 16, 16)[:3]
    print("    同一 clip 两层不同 play_sec：中心=%s（期望蓝 (0,0,255)）" % (got,))
    assert near(got, (0, 0, 255)), "上层（play_sec=5）应盖住下层（play_sec=0）：got=%s" % (got,)
    assert ("c1", 0.0) in seen and ("c1", 5.0) in seen, \
        "两层都应各自解析自己的 play_sec：seen=%s" % (seen,)

    # 反向验证：把两层 play_sec 对调，中心应变红（证明 play_sec 真的被用上了）
    s0.play_sec = 5.0
    s1.play_sec = 0.0
    got2 = px(cp.compose((32, 32), [s0, s1], lookup2), 16, 16)[:3]
    print("    play_sec 对调后：中心=%s（期望红 (255,0,0)）" % (got2,))
    assert near(got2, (255, 0, 0)), "play_sec 对调后应取到红帧：got=%s" % (got2,)

    # ★ 向后兼容：只接受 1 个参数的旧回调也必须照常工作（不崩）
    frames = {"c1": solid(32, 32, (10, 200, 30))}
    old_style = lambda cid: frames.get(cid)                               # noqa: E731
    got3 = px(cp.compose((32, 32), [state(0, "c1", "normal")], old_style), 16, 16)[:3]
    print("    旧单参回调：中心=%s（期望 (10,200,30)）" % (got3,))
    assert near(got3, (10, 200, 30)), "旧单参回调应兼容：got=%s" % (got3,)
    assert cp._wants_two_args(old_style) is False
    assert cp._wants_two_args(lookup2) is True
    print("  ✓ frame_lookup：双参数各取各帧、play_sec 真的生效、旧单参回调兼容")


# --------------------------------------------------------------------------
def main() -> int:
    print("=" * 68)
    print("M2 · 预览合成器测试  (python %s, QT_QPA_PLATFORM=%s)"
          % (sys.version.split()[0], os.environ.get("QT_QPA_PLATFORM")))
    print("PySide6 Qt=%s" % __import__("PySide6.QtCore", fromlist=["qVersion"]).qVersion())
    print("=" * 68)
    tests = [
        ("7 种混合的真实像素值", test_blend_modes_pixels),
        ("几何对齐演出端（cover/contain）", test_geometry_matches_engine),
        ("D2 alpha 只乘一次", test_alpha_multiplied_once),
        ("crop 裁切", test_crop_outside_not_written),
        ("过渡 fade/cut/slide/zoom/glitch", test_transitions_fade_and_cut),
        ("性能 1080p 三层", test_perf_1080p_three_layers),
        ("集成 timeline→compositor", test_timeline_to_compositor_integration),
        ("frame_lookup 双参数契约 + 旧单参兼容", test_frame_lookup_signature_and_override),
    ]
    ok_all = True
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
        except Exception as e:                                      # noqa: BLE001
            ok_all = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
