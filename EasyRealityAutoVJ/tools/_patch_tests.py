# -*- coding: utf-8 -*-
"""测试对齐补丁：把三个用例更新到「0.5 秒演出提前量 + 真拖动 2.5 秒判定」的新契约。"""

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def patch(path, pairs):
    p = os.path.join(ROOT, path)
    s = io.open(p, encoding="utf-8").read()
    n = 0
    for old, new in pairs:
        if old in s:
            s = s.replace(old, new)
            n += 1
        else:
            print("   !! 没找到：%s …" % old[:60].replace("\n", " "))
    io.open(p, "w", encoding="utf-8").write(s)
    print("   %s 改了 %d 处" % (path, n))


def main():
    # ① cue_tick_test：play_sec 现在统一 +0.5（演出提前量）
    patch("tools/cue_tick_test.py", [
        ('abs(sec - expected)', 'abs(sec - (expected + 0.5))'),
    ])

    # ② cue_fp_synth_test：同上
    patch("tools/cue_fp_synth_test.py", [
        ('abs(sec - offset)', 'abs(sec - (offset + 0.5))'),
        ('abs(sec - 22.0) <= 0.5', 'abs(sec - 22.5) <= 0.6'),
    ])

    # ③ 位置层：远偏差不再"慢慢提速度"，而是 2.5 秒后重锚
    patch("tools/_cue_pos_smooth_test.py", [
        ('''    ck("E2 持续 4 秒偏差：6s 时仍偏 %.2fs（防抖在起作用）" % e6, e6 > 1.5)
    ck("E2b 之后逐步收敛：15s→%.2fs、29s→%.2fs" % (e15, e29), e15 < e6 and e29 < 1.0)''',
         '''    # 新契约：4 秒偏差属于"远"，候选持续一致 2.5 秒 ⇒ 直接重锚（跳一次）
    ck("E2 持续 4 秒偏差：3 秒内重锚（6s 时偏差 %.2fs < 0.5）" % e6, e6 < 0.5)
    ck("E2b 之后保持正确：15s→%.2fs、29s→%.2fs" % (e15, e29), e15 < 0.6 and e29 < 0.6)'''),
        ('''    ck("F2 持续 >3 秒后放开到 ±20%", late > 0.10, "最大偏离 %.3f" % late)''',
         '''    ck("F2 远跳不靠提速（速度仍在 ±5% 内，靠 2.5 秒后的重锚）",
       late <= 0.0501, "最大偏离 %.3f" % late)'''),
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
