# -*- coding: utf-8 -*-
"""造一个 3 帧测试 GIF（给 T02 验证用）。

⚠ 本脚本需要 **PIL** —— 用 AutoVJ 的 venv 跑（ERCUE venv 无 PIL）：
  P:/ERAVJ/AutoVJ/venv/Scripts/python.exe tools/_make_test_gif.py <输出路径>

产物：3 帧（红/绿/蓝），每帧 100ms，loop=0（无限循环）⇒ 一轮 0.3s。
"""
import sys

from PIL import Image


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "test3f.gif"
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", (64, 64), c) for c in cols]
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=100, loop=0)
    print("已造 GIF: %s  帧色(RGB)=%s  每帧100ms ⇒ 一轮 0.3s" % (out, cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
