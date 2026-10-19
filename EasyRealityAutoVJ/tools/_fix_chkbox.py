# -*- coding: utf-8 -*-
"""修复选框/单选框「勾选后胀大 4px」导致的间距/对齐不一致。

原因（QSS 盒模型）：`width/height` 指的是**内容盒**，边框画在外面。
  · 未勾选：16px 内容 + 2×1px 边框 = **18×18**
  · 勾选  ：16px 内容 + 2×3px 边框 = **22×22**  ← 大了 4px，标签跟着右移
（3px 边框是 2026-09-27 用户要的「里层色块比外轮廓小一圈」的做法，设计要保留。）

修法：勾选态的内容盒改成 **12px** —— 12 + 2×3 = 18px，与未勾选**外轮廓完全等大**，
里层 accent 色块仍是 12px（比外轮廓小一圈），设计效果不变。
"""
import io
import os
import sys

P = r"P:\AutoVJ\src\theme.py"
OLD = """QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
             background: {v['accent']}; border: 3px solid {v['border']}; }}"""
NEW = """/* ⚠⚠ 这里必须写 `width/height: 12px`（而不是沿用 16px）——Qt 的 width/height 是
   **内容盒**，边框画在外面：未勾选 = 16+2×1 = 18px，勾选若仍写 16 就变成 16+2×3 = 22px
   ⇒ **勾选后整块胀大 4px、标签跟着右移**，一列复选框看起来"间距不一样"
   （用户 2026-10-01 截图反馈）。改成 12px 后 12+6 = 18px，与未勾选**外轮廓等大**，
   里层色块依旧比外轮廓小一圈（保留 2026-09-27 的设计意图）。 */
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
             width: 12px; height: 12px;
             background: {v['accent']}; border: 3px solid {v['border']}; }}"""


def main():
    s = io.open(P, encoding="utf-8").read()
    if s.count(OLD) != 1:
        print("✗ 锚点命中 %d 次" % s.count(OLD))
        return 1
    io.open(P, "w", encoding="utf-8", newline="\n").write(s.replace(OLD, NEW))
    print("✓ theme.py 已修（勾选态内容盒 16px → 12px，外轮廓恒 18×18）")
    import py_compile
    py_compile.compile(P, doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
