# -*- coding: utf-8 -*-
"""**淡出过渡**回归（用户 2026-10-17："码表编排结束后看不见淡出的过渡效果"）。

根因（两端同源）：`_alpha_for()` 在淡出期把 `trans_p` 给成 **1-a_out**，而合成端算的是
`opacity × alpha × trans_p`，此时 `alpha` 本身就是 `a_out`
⇒ 有效 α = **a_out(1-a_out)**：
  · 淡出**刚开始**（a_out=1）→ 有效 α = **0** ⇒ 画面**瞬间消失**（这就是"看不见淡出"）；
  · 中段冒出个 0.25 的鬼影；末端又是 0。
正确：淡出的 `trans_p` 必须 = `a_out` ⇒ 有效 α = **a_out²**，与淡入的 `a_in²` 对称（契约②）。
注意：`slide/zoom/glitch` 的 `trans_p` 是**几何进度**（0→1 越走越远），必须仍是 `1-a_out`。

判据：
  A. 淡出曲线：有效 α == α²，且**起点必须是 1.0**（不是 0）；
  B. 淡入曲线不受影响：仍 == a_in²；
  C. slide/zoom 的 trans_p 仍是 1-a_out（几何不能被顺手改坏）；
  D. 编排端 `EasyRealityCue/timeline._alpha_for` 与演出端**逐值一致**（契约②不能单边改）。

运行：`python tools/_cue_fade_test.py`
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cuesheet as CS                                        # noqa: E402
import cuesheet_runtime as crt                               # noqa: E402

BPM = 120.0
BL = 0.5
ERC_TIMELINE = r"P:/ERAVJ/EasyRealityCue/src/ercue/model/timeline.py"
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


def build(trans_out="fade", trans_in="cut"):
    s = CS.new_sheet("t", res_w=640, res_h=360)
    te = CS.new_track("T", "A", 100.0, BPM)
    L = CS.CompLayer(lyr_id="L1", z=0)
    L.items.append(CS.CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_beats=20.0,
                              trans_in=trans_in, trans_in_beats=2.0,
                              trans_out=trans_out, trans_out_beats=2.0).normalized())
    te.layers.append(L)
    s.tracks.append(te)
    s.media.append(CS.MediaRef(clip_id="c1", name="a.mp4"))
    return crt.CueRuntime(s, te, media_paths={"c1": "a.mp4"})


def walk(rt, points):
    """逐帧推进（避免被判"大跳变"），返回每个采样点的 LayerPlayState。"""
    out = []
    q = 0.0
    for p in points:
        while q < p - 1e-9:
            q = min(p, q + 0.01)
            st = rt.update(q, beat_len=BL)[0]
        out.append(rt.update(p, beat_len=BL)[0])
    return out


def main():
    print("A. 淡出曲线：有效 α 必须 = α²，且起点 = 1.0")
    rt = build()
    pts = [17.0, 17.5, 18.0, 18.5, 19.0, 19.5]      # 淡出窗口 = 18~20 拍
    sts = walk(rt, pts)
    for p, st in zip(pts, sts):
        if p < 18.0:
            continue
        eff = st.alpha * st.trans_p
        ck("拍位 %.1f: alpha=%.3f ⇒ 有效 α=%.4f == α²(%.4f)"
           % (p, st.alpha, eff, st.alpha ** 2),
           abs(eff - st.alpha ** 2) < 1e-6, "实际 %.4f" % eff)
    first = sts[pts.index(18.0)]
    ck("淡出**起点**有效 α ≈ 1.0（旧实现在这里是 0 ⇒ 画面瞬间消失）",
       first.alpha * first.trans_p > 0.9,
       "实际 %.3f（alpha=%.3f trans_p=%.3f）"
       % (first.alpha * first.trans_p, first.alpha, first.trans_p))
    ck("淡出确实在递减",
       all(sts[i].alpha * sts[i].trans_p > sts[i + 1].alpha * sts[i + 1].trans_p
           for i in range(pts.index(18.0), len(pts) - 1)))

    print("B. 淡入曲线不受影响（仍 a_in²）")
    rt2 = build(trans_in="fade")
    pts2 = [0.0, 0.5, 1.0, 1.5, 2.0]
    sts2 = walk(rt2, pts2)
    for p, st in zip(pts2, sts2):
        eff = st.alpha * st.trans_p
        ck("拍位 %.1f: 有效 α=%.4f == α²(%.4f)" % (p, eff, st.alpha ** 2),
           abs(eff - st.alpha ** 2) < 1e-6, "实际 %.4f" % eff)

    print("C. slide / zoom 的 trans_p 仍是几何进度（1-a_out）")
    for kind in ("slide", "zoom"):
        rt3 = build(trans_out=kind)
        sts3 = walk(rt3, [19.0])                    # 淡出中段：a_out = 0.5
        st = sts3[-1]
        ck("%s: trans_p ≈ 1-a_out（几何进度，不是 a_out）" % kind,
           abs(st.trans_p - (1.0 - st.alpha)) < 0.08,
           "trans_p=%.3f alpha=%.3f" % (st.trans_p, st.alpha))
        ck("%s: trans 被正确标成 %s" % (kind, kind), st.trans == kind,
           "实际 %r" % (st.trans,))

    print("C2. 超过素材时长 ⇒ 冻结最后一帧（不许去追已回卷的位置）")
    s2 = CS.new_sheet("t", res_w=640, res_h=360)
    te2 = CS.new_track("T", "A", 100.0, BPM)
    L2 = CS.CompLayer(lyr_id="L1", z=0)
    # 片段 0~40 拍（20s），但素材只有 12s ⇒ 到 12s 必须冻结
    L2.items.append(CS.CueItem(item_id="i1", clip_id="c1", at_sec=0.0, end_beats=40.0,
                               trans_in="cut", trans_out="cut").normalized())
    te2.layers.append(L2); s2.tracks.append(te2)
    s2.media.append(CS.MediaRef(clip_id="c1", name="a.mp4", duration_sec=12.0))
    rt2 = crt.CueRuntime(s2, te2, media_paths={"c1": "a.mp4"})
    st_a = walk(rt2, [10.0])[0]          # 5s
    st_b = walk(rt2, [30.0])[0]          # 15s（已超 12s）
    ck("12s 内正常推进（play_sec≈5）", abs(st_a.play_sec - 5.0) <= 0.3,
       "%.2f" % st_a.play_sec)
    ck("超过素材时长 ⇒ play_sec 钳在 12.0 且 src_ended=True",
       abs(st_b.play_sec - 12.0) <= 0.05 and bool(st_b.src_ended),
       "play=%.2f ended=%s" % (st_b.play_sec, st_b.src_ended))

    print("D. 编排端与演出端逐值一致（契约②不能单边改）")
    if not os.path.isfile(ERC_TIMELINE):
        print("  （没有 EasyRealityCue 源码，跳过 D）")
    else:
        src = open(ERC_TIMELINE, encoding="utf-8").read()
        ck("编排端 fade 也用 a_out（不是 1-a_out）",
           'a_out if it.trans_out == "fade" else 1.0 - a_out' in src,
           "没找到同款写法")
        ck("编排端 slide/zoom 仍走 1-a_out",
           "else 1.0 - a_out" in src)

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 淡出是真正的 α² 收尾（起点 1.0 → 末端 0），两端口径一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
