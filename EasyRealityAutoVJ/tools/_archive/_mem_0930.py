# -*- coding: utf-8 -*-
"""更新项目记忆：① 今日日志追加 ② MEMORY.md 里作废的"码表"章节改写。"""
import io
import os
import re
import sys

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-09-30.md")
M = os.path.join(MEM, "MEMORY.md")

LOGTXT = """# 2026-09-30 工作日志

## 一、码表（.ercue）整套功能摘除

用户判断：「所有的问题都是码表加上后导致」⇒ 备份到 P 盘后从程序里整体删除。

**备份位置**：`P:\\AutoVJ_码表备份_20260930\\`
- `src\\`（37 个 .py，**摘除前的完整源码**）、`tools\\`（31 个码表相关脚本）、
  `docs\\`（_码表功能设计.md + 两份使用说明的原始版本）、`pre_strip_bak\\`（我改之前
  单独留的三份 engine/ui_main/panels 快照）、`README.md`（为什么删 / 删了什么 / 怎么恢复）。
- ⚠ 刻意放在**项目目录之外**：不会被 build/make_zip 扫到，也不会被 git 收进去。

**删掉的文件**：`src/cuesheet.py`、`cuesheet_runtime.py`、`cue_editor.py`（2400+ 行）、
`cue_preview.py`、`position_source.py`、`_码表功能设计.md`。

**挖掉的代码**（文件还在）：
- `engine.py` 2899 → 2394 行：`_CueSubLayer` 类、`CUE_FP_POS_*`、
  `Layer.is_cuesheet/cuesheet_id/cs_states/cs_active`、`to_dict`/`rebuild_from_cfg` 的字段、
  引擎 11 个码表实例属性、整块码表方法（`set_cue_sheet` … `_draw_cuesheet_layer`，约 360 行）、
  `_tick_locked` 里的 `_cuesheet_tick` 调用、`_get_player` 的容器缓存分支、
  `_apply_color`/`_apply_postfx` 的 `_cuesheet_on` 旁路、`_compose` 的容器拦截分支、
  **以及只服务于码表的 `crop` 参数/裁切实现（连带 5 个调用点）**。
- `ui_main.py` 4853 → 4741 行：第 3 个界面、`cue_sheets` 注册表、`cue_layer_menu/use`、
  `open_cuesheet`、「⋯ 更多」里的入口、`_ensure_cue_editor`；`switch_page` 上界 2→1；
  `layer_add` 去掉 `cue_hint` 与第 5 个返回值。
- `panels.py` 2843 → 2719 行：`NewLayerDialog` 的码表单选项（`result_data` 5→4 元组）、
  码表徽标三个方法、图层头青绿配色分支、徽标控件与刷新。
- `i18n_map.py` 1254 → 914 行：**一次删掉 340 行码表词条**；`config.py` 删掉 `cfg["cue"]` 段。
- 两份《使用说明》删掉第十七节与所有码表措辞（残留 0 行）。

**验证**：`tools/_after_strip_smoke.py` 18 项全绿（含"老配置 page=2 必须钳到 1"、
"result_data 是 4 元组"、引擎跑 1.5s 无异常）；`tools/i18n_coverage.py` 报"全部覆盖"；
全库 grep 只剩 3 处刻意保留的历史注释 + `beatgrid.py` 里 VirtualDJ 的 cue **点**（同名不同义）。

## 二、全库四路审计（引擎/界面/音频/构建）→ 已修高危

用四个只读子代理分别审，合计 90+ 条。已落地的修复：

**★ 4 处「同名方法定义两遍」**（后一份静默覆盖前一份 —— 这轮最值钱的一类 bug）
用 `tools/_scan_dupdef.py`（AST）扫出来的：
1. `engine.manual_transition` 被第二份顶掉 ⇒ **GUI 回调变成无锁**（注释里写的修复根本没生效）；
2. `panels.ClipRow.mousePressEvent/mouseMoveEvent` 各两份 ⇒ 第一份是死代码，第二份才是活的；
3. `output_window.resizeEvent` 两份 ⇒ 第二份把「移动/缩放窗口时暂停重绘」顶掉了。
**新写的扫重复定义脚本以后每次改完都跑一遍。**

**★ 13 个被 GUI 直接调用的引擎方法缺 `@_locked`**（`beat_grid_view`、`phrase_countdown`、
`switch_countdown`、`flip_phrase_now`、`next_clip_for`、`kv_layer`、`manual_mode`、
`set_canvas_size/render_fps/blackout/freeze/pause_auto`、`lock_clip`）。用
`tools/_scan_locks.py` 扫 GUI 侧调用点找出来的，已全部补锁。
唯一**故意不加锁**的是 `latest_frame()`（GUI 每帧都调；加锁等于每帧等锁 ⇒ 幻灯片），
已在代码里写明"别顺手修成带锁的"。

**★ `postprocess.fx_trails(None, 0.0, reset=True)` 会 AttributeError**（`None.copy()`），
被上层 `except Exception: pass` 吞掉 ⇒ **拖影缓冲永远清不掉**，下次开拖影闪一下旧残影。
已改成 `bgr is None ⇒ 清缓冲`。

**音频**：识别窗口 4.5s 硬编码在两处（切窗 + 位置补偿）⇒ 提成 `FP_WIN_SEC`；
环形缓冲按 48k 写死 ⇒ 96k 设备时识别窗被截短，改成按实际采样率给足 12 秒；
`MatchTick.evidence_votes` 在 `__slots__` 里声明但没初始化 ⇒ 补上。

**顺带**：删掉死模块 `src/section_analyze.py`（无任何导入方）。

## 三、这次学到的可复用方法
- **看现场**比看代码快：`QGuiApplication.primaryScreen().grabWindow(0)` 截屏 +
  ctypes `SetForegroundWindow` 抢前台 + 裁剪放大关键区域，就能"自己看"用户的界面
  （窗口被别的窗口盖住时，**每次拍照前都要重新抢一次前台**）。
- **`py-spy` 直接读"正在运行"进程**（含 PyInstaller 冻结的 exe）的 Python 栈，
  按叶子函数聚合 raw 折叠栈 → 5 分钟定位热点。
- **AST 扫重复定义**、**扫"GUI 调用但引擎没加锁"**：两个脚本几分钟跑完，命中率极高。
- 离屏测试的两个坑：① 主题相关断言必须用 **app 级** `setStyleSheet`（控件级行为不同，
  会误判）；② `widget.grab()` 受布局尺寸影响，断言前先确认目标在可视区内，否则**假阴性**。
"""

if os.path.isfile(LOG):
    io.open(LOG, "a", encoding="utf-8").write("\n\n" + LOGTXT)
else:
    io.open(LOG, "w", encoding="utf-8", newline="\n").write(LOGTXT)
print("日志已写入", LOG)

# ---------------------------------------------------------------- MEMORY.md
s = io.open(M, encoding="utf-8").read()
NEW = """## 码表（.ercue）—— 已于 2026-09-30 **整体移除**
- 用户判断「所有的问题都是码表加上后导致」⇒ 连 UI / 图层 / 运行时 / 位置源一起删干净。
- **备份在 `P:\\AutoVJ_码表备份_20260930\\`**（摘除前的完整 src + 工具 + 文档 + 恢复说明）。
  要恢复就照那份 README 走，别去翻 git 历史。
- 残留约定：**没有** `cfg["cue"]`、没有 `Layer.is_cuesheet`、没有第 3 个界面；
  `switch_page` 上界是 **1**（老配置里的 `page=2` 会被钳回 1）。
- ⚠ **指纹认歌保留**：它现在驱动**离线节拍网格时钟**（`_grid_observe` 用
  `recognized_offset` 校正网格相位），不是码表的附属品，别当残留删掉。
  同理 `beatgrid.py` 里的 `"cue"` 是 **VirtualDJ 的 cue 点**，与码表无关。
"""
# 用"## 码表"到下一个 "## " 之间的整段替换掉
i = s.find("## 码表")
if i < 0:
    print("⚠ 没在 MEMORY.md 里找到 ## 码表 段落")
else:
    j = s.find("\n## ", i + 5)
    if j < 0:
        j = len(s)
    s = s[:i] + NEW + s[j:]
    io.open(M, "w", encoding="utf-8", newline="\n").write(s)
    print("MEMORY.md 码表章节已改写（%d 字符）" % len(s))
