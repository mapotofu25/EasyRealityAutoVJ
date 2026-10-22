# -*- coding: utf-8 -*-
"""记录：复选框「勾选后胀大 4px」的 QSS 盒模型坑。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-10-01.md")

TXT = """

## 八、UI：复选框/单选框「勾选后胀大 4px ⇒ 间距看起来不一样」

用户截图反馈：输出设置里「锁定 16:9 / 窗口置顶 / Spout 输出 / NDI 输出」几个勾选框
大小与文字位置不一致。

**根因（QSS 盒模型）**：`QCheckBox::indicator` 里 `width/height` 指的是**内容盒**，
边框画在**外面**：
- 未勾选：`16px` 内容 + `2×1px` 边框 = **18×18**
- 勾选：`16px` 内容 + `2×3px` 边框 = **22×22**（3px 边框是 2026-09-27 要的
  「里层色块比外轮廓小一圈」的做法，设计本身没错，错在没把内容盒相应缩小）
⇒ 勾选后整块大 4px、**标签起点从 x=24 挪到 x=28** ⇒ 一列复选框看起来"间距不一样"。

**修法**：勾选态的内容盒写成 `width/height: 12px` ⇒ `12+2×3 = 18px`，
与未勾选**外轮廓完全等大**，里层 accent 色块仍比外轮廓小一圈（设计不变）。

**实测对照**（`QStyle.subElementRect(SE_CheckBoxIndicator/Contents)`）：
| | 指示器 | 标签起点 |
|---|---|---|
| 修之前 · 未勾选 | 18×18 | x=24 |
| 修之前 · **勾选** | **22×22** | **x=28** |
| 修之后 · 未勾选/勾选 | 都是 18×18 | 都是 x=24 |

**回归测试** `tools/_test_chkbox_geom.py`（10 项全绿，QCheckBox 与 QRadioButton 都覆盖）。
⚠ 写这类测试别数像素（我第一次就量到整个容器），直接用
`QStyle.subElementRect(SE_CheckBoxIndicator, opt, w)` 拿 Qt 算好的矩形最准。
"""

io.open(LOG, "a", encoding="utf-8").write(TXT)
print("已写入", LOG)
