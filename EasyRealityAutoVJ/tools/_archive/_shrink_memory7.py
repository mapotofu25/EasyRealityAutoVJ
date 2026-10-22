# -*- coding: utf-8 -*-
"""最后一档：把 MEMORY.md 压到 15.0KB 以内。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")
t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))

PAIRS = [
    # ① 不上传清单压成一行
    ("- ❌ **不上传**：`venv/ dist*/ build*/ *.spec *.exe`、发行 zip、`assets/models`、`assets/*.png` 截图、\n"
     "  `更新内容*.txt`、**内部工程笔记**、`tools/` 一次性探针、`_zips_old/`、`config.json`、`.workbuddy/`、\n"
     "  `_ui_shot/`。全进 `.gitignore`；一律 `git rm --cached`（保留本地文件）。",
     "- ❌ **不上传**：`venv/ dist*/ build*/ *.spec *.exe`、发行 zip、`assets/models`、`assets/*.png`、\n"
     "  `更新内容*.txt`、内部工程笔记、`tools/` 一次性探针、`_zips_old/`、`config.json`、`.workbuddy/`。\n"
     "  全进 `.gitignore`；一律 `git rm --cached`（保留本地文件）。"),
    # ② 快捷键并进工作方式（省一个小节）
    ("## 快捷键\n默认全部**窗口内**（全局无修饰键会系统级吞键）。HotkeyDialog：改表格只改表格，点「应用」才生效；关闭时 `_dirty` 弹二级框（**用按钮对象判断**）。",
     "## 快捷键\n默认全部**窗口内**（全局无修饰键会吞键）。HotkeyDialog：点「应用」才生效；关闭时 `_dirty` 弹二级框（**用按钮对象判断**）。"),
    # ③ 能量与节奏里那条「写文档要核实」已被工作方式覆盖，压短
    ("- ⚠ **写 README / 使用说明必须逐条对照代码核实**（**界面文案权威清单 = `i18n_map.py`**），\n"
     "  不能凭旧文档或记忆推断（曾一次写错 5 处）。其余细项见 REFERENCE §五。",
     "- ⚠ **写 README / 使用说明必须逐条对照代码核实**（界面文案权威清单 = `i18n_map.py`）。其余见 REFERENCE §五。"),
    # ④ UI 小节压紧（三界面细节已在码表小节/设计文档里）
    ("- ★ **主区是三界面**（`QStackedWidget`）：① 🎬 演出台（图层+预览+**素材库**+设置+输出）\n"
     "  ② 🎵 曲库 · 曲风（音乐曲库 / 曲风映射入口 + 只读曲库概况）③ 🎞 码表编排\n"
     "  （`cue_editor.CueSheetEditor(main, embedded=True)`，**惰性创建**，切走就 `set_active(False)` 停定时器）。\n"
     "  页号存 `cfg[\"ui\"][\"page\"]`；入口 `MainWindow.switch_page(i)`；`open_cuesheet()` = `switch_page(2)`。\n"
     "  ⚠ **素材库必须留在演出台**（用户 2026-09-28：「不然我怎么手动添加素材」）——搬走会导致**跨页拖拽失效**\n"
     "  （Qt 要求两端同时可见）。「🎵 音乐曲库 / 曲风映射」按钮**已从顶部工具条撤掉**（收进第 2 页）。\n"
     "  ★ 素材库**双击缩略图 = 播放到当前图层**（`LibraryGrid.itemDoubleClicked`）保留，很好用。",
     "- ★ **主区是三界面**（`QStackedWidget`，共用同一引擎）：① 🎬 演出台（图层+预览+**素材库**+设置+输出）\n"
     "  ② 🎵 曲库 · 曲风 ③ 🎞 码表编排（`CueSheetEditor(embedded=True)`，**惰性创建**，切走 `set_active(False)`）。\n"
     "  页号存 `cfg[\"ui\"][\"page\"]`；入口 `MainWindow.switch_page(i)`。\n"
     "  ⚠ **素材库必须留在演出台**（搬走会导致**跨页拖拽失效**，Qt 要求两端同时可见）；「音乐曲库/曲风映射」\n"
     "  已从工具条撤到第 2 页。★ 素材库**双击缩略图 = 播放到当前图层**保留。"),
]

for a, b in PAIRS:
    assert t.count(a) == 1, (t.count(a), a[:44])
    t = t.replace(a, b)
io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
