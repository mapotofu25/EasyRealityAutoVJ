# -*- coding: utf-8 -*-
"""精简 MEMORY.md：细讲挪进 REFERENCE.md，MEMORY 只留"必须一眼看到"的铁律。

原因：MEMORY.md 长到 4.7K 字符 / ~13KB 字节，已逼近上次**被注入端截断**的体积
（上次是 15129 字节触发截断）。项目约定本来就是"MEMORY = 铁律，REFERENCE = 长尾"。
"""
import io
import os
import sys

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"

MEM_OLD = """- ★★ **改热路径里的局部变量名 = 高危**（2026-09-30 炸过）：音频 FFT 段 `freqs` 改名后，
  同一函数体第 444 行仍用旧名 ⇒ `NameError` 每块都抛 ⇒ 能量/BPM 全废，而界面只留一行红字
  "分析块错误（已跳过）"。⇒ **grep 整个函数体 + 改完真跑热路径**（`tools/_audioerr.py`）。
- ★★ **"用户说没修好"先自证**：`tools/_hl_diag.py` 看真实状态（⚠ **必须真启动引擎**，
  否则 `lay.cur` 全是 -1，会得出"没修好"的错结论）；真窗口取证用 `tools/_hl_live.py`
  （抢前台→点开始→**最小化输出窗口**，它一开就盖住图层区→截图数金像素）。
- ★ **"自己看"比反复问快**：`grabWindow(0)` 截屏 + ctypes `SetForegroundWindow`（**每次拍照前
  都要重抢**）+ 裁剪放大；`py-spy record --format raw --pid <PID>` 直接读**正在运行**进程
  （含 PyInstaller 冻结的 exe）的栈，聚合叶子函数 5 分钟定位 CPU 热点。
"""

MEM_NEW = """- ★★ **改热路径里的局部变量名 = 高危**：必须 grep 整个函数体 + **改完真跑热路径**
  （`tools/_audioerr.py`）。2026-09-30 就这么把音频分析炸成"每块都抛、能量全废"。
- ★★ **"用户说没修好"先自证**：`tools/_hl_diag.py`（⚠ 必须真启动引擎）+ `tools/_hl_live.py`
  真窗口截图。别靠读代码下结论。
- ★ **"自己看"比反复问快**：截屏 + `SetForegroundWindow`（每次拍照前重抢）+ 裁剪放大；
  `py-spy` 读**正在运行**进程的栈定位 CPU 热点。详见 REFERENCE「工作方式补充」。
"""

CUE_OLD_START = "## 码表（.ercue）—— 已于 2026-09-30 **整体移除**"
CUE_NEW = """## 码表（.ercue）—— 已于 2026-09-30 **整体移除**
- 备份在 `P:\\AutoVJ_码表备份_20260930\\`（含恢复说明）。**没有** `cfg["cue"]`、没有
  `Layer.is_cuesheet`、没有第 3 个界面；`switch_page` 上界是 **1**。
- ⚠ **指纹认歌保留**（它驱动离线节拍网格时钟）；`beatgrid` 里的 `"cue"` 是 VirtualDJ 的
  **cue 点**，与码表无关。
"""

REF_ADD = """

## 六、工作方式补充（2026-09-30 从 MEMORY.md 挪来）

**★★ 改热路径里的局部变量名 = 高危操作**
把音频 FFT 段的 `freqs` 改名成 `_freqs`，**同一个函数体**下面第 444 行
`bass_mask = (freqs >= 20) & (freqs <= 200)` 还在用旧名 ⇒ `NameError` **每块都抛**
⇒ 采集线程把它捕获成 `st.error = "分析块错误（已跳过）: ..."`
⇒ **整机能量/BPM 全废**（现场只留一行红字、BPM 显示 `-`、能量 0.00，不崩不报栈）。
为什么没被测试抓到：当时只跑了"指纹恒等""频带平均"两个单测，**没跑 `_Analyzer.feed`**。
⇒ 规矩：**grep 整个函数体；改完必须真跑热路径**。已加 `tools/_audioerr.py`
（直接喂 1024 帧给 `_Analyzer.feed`，异常完整打出，比在整机里看"已跳过"有用得多）。

**★★ "用户说没修好"先自证，别猜**
- `tools/_hl_diag.py`：离屏起真 MainWindow + **真启动引擎**，逐层打印 `cur`/clips/命中。
  ⚠ **不启动引擎就全是 `cur=-1`**，会得出"没修好"的**错误结论**（第一次就这么误判了）。
- `tools/_hl_grab.py`：抓 `ClipRow` 图并数金黄像素（离屏，快）。
- `tools/_hl_live.py`：真窗口取证 —— 抢前台 → 点「演出台」→ 点「开始自动 VJ」→
  **最小化输出窗口**（「Easy Reality Output」一打开就盖住图层区，不关掉根本看不到高亮）
  → 截图并统计金像素/连通块。
- 结论：修完要能拿出一张"看得见"的图，否则不算修好。

**★ 播放高亮为什么"能亮却看不见"**
`refresh_playing` 改按素材**路径**比对之后确实在工作（`_hl_diag` 实测图层 2/3 各 2 格亮着
= 正在播的 + 过渡中的上一个）。但缩略图的 `iconSize = (CELL_W-8) × (CELL_H-10)` 几乎铺满格子，
**金黄底只在四周露出约 4px**，一屏深色素材里根本注意不到。
⇒ 新增 `_PlayBorderDelegate`（`src/panels.py`）：在缩略图**之上**再画 3px 金边，
用 `PLAY_ROLE = Qt.UserRole + 7` 传"在播"标记（不依赖背景刷子）。真窗口实测每格整块变金。

**★ 取证/定位工具清单（都留在 tools/ 根，别归档）**
`_scan_dupdef.py`（同名方法定义两遍）、`_scan_locks.py`（GUI 调用但引擎没加锁）、
`_audioerr.py`（音频热路径）、`_hl_diag/_hl_grab/_hl_live.py`（高亮取证）、
`_proc_info.py`（进程/镜像/累计 CPU）、`_raise_shot.py`/`_win_shot.py`（截屏）、
`_show_ranges.py`（按行号读代码）、`_verify_perf_fp.py`（指纹恒等回归）、
`_git_hygiene.py`（仓库体检）、`_archive_probes.py`（历史脚本归档）。
"""


def main():
    p = os.path.join(MEM, "MEMORY.md")
    s = io.open(p, encoding="utf-8").read()
    before = len(s)
    if MEM_OLD in s:
        s = s.replace(MEM_OLD, MEM_NEW)
        print("✓ 工作方式：长教训已压成 3 条铁律")
    else:
        print("· 工作方式锚点未命中，跳过")
    i = s.find(CUE_OLD_START)
    j = s.find("\n## 输出", i)
    if i >= 0 and j > i:
        s = s[:i] + CUE_NEW + s[j:]
        print("✓ 码表段已压缩")
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("MEMORY.md：%d → %d 字符" % (before, len(s)))

    r = os.path.join(MEM, "REFERENCE.md")
    t = io.open(r, encoding="utf-8").read()
    if "## 六、工作方式补充" not in t:
        io.open(r, "a", encoding="utf-8").write(REF_ADD)
        print("✓ REFERENCE.md 已追加「工作方式补充」（%d → %d 字符）"
              % (len(t), len(t) + len(REF_ADD)))
    else:
        print("· REFERENCE 已有该节，跳过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
