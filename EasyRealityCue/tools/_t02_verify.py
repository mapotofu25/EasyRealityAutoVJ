# -*- coding: utf-8 -*-
"""T02 验证：GIF 环绕播放（造一个多帧 GIF → 跨越循环点两次 → 帧序列应循环）。

做法：
  1. 用 PIL 造 3 帧 GIF（红/绿/蓝，每帧 100ms ⇒ 一轮 0.3s，loop=0=无限循环）
  2. 用 `CueFrameSource.frame_exact()` 取帧：
       第一轮：0.05 / 0.15 / 0.25
       第二轮：0.35 / 0.45 / 0.55   ← 应与第一轮逐帧相同（环绕生效）
       对照组：设 src_out 的场景由模型层负责（本脚本只管取帧层）
  3. 输出：每帧的中心像素色 + 两轮是否逐帧一致

用法：venv/Scripts/python.exe tools/_t02_verify.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))


def make_gif(path):
    from PIL import Image
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", (64, 64), c) for c in cols]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return cols


def center_color(arr):
    h, w = arr.shape[0], arr.shape[1]
    px = arr[h // 2, w // 2]
    return tuple(int(v) for v in px[:3])          # BGRA


def main():
    gif = sys.argv[1] if len(sys.argv) > 1 else ""
    if not gif or not os.path.isfile(gif):
        print("用法: python tools/_t02_verify.py <某个多帧GIF路径>")
        print("（ERCUE venv 无 PIL，故造 GIF 用 AutoVJ venv，见 tools/_make_test_gif.py）")
        return 2
    print("测试 GIF: %s（外部提供）" % gif)

    from ercue.media.frames import CueFrameSource
    src = CueFrameSource()
    try:
        # 先确认 loop_dur 被读到
        arr = src.frame_exact(gif, 0.05)
        assert arr is not None, "取帧失败"
        # 通过内部 _src 看 loop_dur（诊断）
        key = (gif, None)
        s0 = src._src.get(key)
        print("loop_dur = %r" % (getattr(s0, "loop_dur", "MISSING"),))

        print("\n=== 取帧（BGR 中心像素）===")
        r1 = []
        for t in (0.05, 0.15, 0.25):
            a = src.frame_exact(gif, t)
            c = center_color(a) if a is not None else None
            r1.append(c)
            print("  第一轮 t=%.2f → %s" % (t, c))
        r2 = []
        for t in (0.35, 0.45, 0.55):
            a = src.frame_exact(gif, t)
            c = center_color(a) if a is not None else None
            r2.append(c)
            print("  第二轮 t=%.2f → %s" % (t, c))

        ok = (r1 == r2) and all(c is not None for c in r1)
        print("\n=== 结论 ===")
        print("  第一轮=%s" % r1)
        print("  第二轮=%s" % r2)
        print("  两轮逐帧一致: %s" % ("是 ✓（环绕生效）" if r1 == r2 else "否 ✗"))
        print("  VERDICT: %s" % ("GIF_LOOP_OK" if ok else "GIF_LOOP_FAIL"))
        return 0 if ok else 1
    finally:
        src.close()


if __name__ == "__main__":
    raise SystemExit(main())
