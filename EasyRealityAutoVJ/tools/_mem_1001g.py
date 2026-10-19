# -*- coding: utf-8 -*-
"""记录：三页界面 + 曲库整页；以及"两份 config"这个重要发现。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-10-01.md")
MEMF = os.path.join(MEM, "MEMORY.md")

TXT = """

## 十二、UI 两项做完：三页界面 + 曲库整页（左栏排序/筛选）

### ① 三页界面（演出台 / 素材库 / 曲库·曲风）—— 用户要求"素材库复制一份、全屏操作"
`ui_main._build_pages` 页签从 2 项扩到 3 项，新增 `_build_library_page()`：
- **共享筛选状态**（类型/角色/标签/搜索词 = `kind_filter`/`role_filter`/`tag_selected`/`lib_kw`），
  两页控件镜像同一状态（`_sync_lib_controls`）⇒ 两页显示永远一致；
- **双网格**：`self.grid`（演出台，原样不动）+ `self.grid_full`（新页）；
- ⚠ **性能**：全屏那份**只在可见时填充**，不可见先标脏 `_lib_full_dirty`，切页时补
  ⇒ 在演出台打字搜索的延迟完全不变；
- ⚠ 跨页拖拽 Qt 不允许 ⇒ 「拖素材进图层」仍然只在演出台做（新页给：搜索/筛选/导入/
  双击播到当前图层/右键菜单）；
- 页号上界 1 → 2；`cfg["ui"]["page"]` 越界一律钳回。
- 回归测试 `tools/_test_pages3.py`（17 项全绿）。

### ② 曲库从**弹窗**改成**整页**（`panels.MusicLibraryPanel`）
- 左栏：**排序**（原顺序/曲名/艺人/BPM↑↓/时长↑↓）+ **筛选**（BPM 区间 / 时长区间 /
  只看文件缺失 / 重置）；
- ⚠ 成员名与旧弹窗**刻意对齐**（`refresh`/`progress`/`lbl`/`btn_scan`/`list`）⇒
  `ui_main` 里那 5 处「扫描进度 / 刷新」代码**一行没改**（主窗口把 `music_dialog` 指到面板）；
- 数据来源：`meta[path]["grid"]` 里的 **`bpm`** 与 **`dur`**（秒）——曲库扫描建库时就写好了
  （实测 66/66 首都有 dur，不用另起探测线程）；文件缺失直接 `os.path.isfile`；
- `open_music_library()` 改成 `switch_page(2)`；
- 回归测试 `tools/_test_music_page.py`（自造 6 首已知 BPM/时长/缺失的曲目，14 项全绿）。

### ★★ 重要发现：机器上有**两份 config**，别搞混
`config.py` 的 `CONFIG_FILE = app_base_dir() + "config.json"`，而 `app_base_dir()` 会**优先用程序自己的目录**（可写时）。于是：

| 配置 | 谁在用 | music_meta 形态 |
|---|---|---|
| `%LOCALAPPDATA%\\AutoVJ\\config.json` | **装到 Y: 的那份**（程序目录不可写 → 退回用户目录） | 有 **`grid`**（66/66）✓ |
| `P:\\AutoVJ\\config.json` | **我源码跑** | 只有 **`sections`**（旧格式，没有 grid） |

⇒ 我先前一直拿 `%LOCALAPPDATA%` 那份当"现场配置"，**看的是另一份**；
而源码跑看到的"全都没网格"是**旧格式配置**的假象（真机上装的那份有 grid，功能正常）。
⚠ **测试要用自造数据**，不能假设某个 config 一定有 `grid`（否则测的是空气）。
⚠ 我的源码测试会给 `P:\\AutoVJ\\config.json` 写盘（那是项目内的旧格式文件，不影响装的那份）。

### 教训
`CONFIG_FILE` 会跟着**运行方式**变（源码 vs 安装包）。看"现场数据"之前先 `print(config.CONFIG_FILE)`。
"""

io.open(LOG, "a", encoding="utf-8").write(TXT)
print("日志已写")

s = io.open(MEMF, encoding="utf-8").read()
add = """- ★ **机器上有两份 config，先看 `config.CONFIG_FILE` 再谈"现场数据"**：`app_base_dir()` 可写时
  用**程序自己目录**（源码跑 → `P:\\AutoVJ\\config.json`，旧格式只有 `sections`），
  安装包（程序目录不可写）退回 `%LOCALAPPDATA%\\AutoVJ\\config.json`（有 `grid`）。
  ⇒ 离线测试**必须自造数据**，不能假设某个 config 有 `grid`（否则测的是空气）。
"""
if "先看 `config.CONFIG_FILE` 再谈" not in s:
    i = s.find("## 输出")
    s = s[:i] + add + "\n" + s[i:]
    io.open(MEMF, "w", encoding="utf-8", newline="\n").write(s)
    print("MEMORY.md 已补（%d 字符 / %d 字节）" % (len(s), len(s.encode("utf-8"))))
