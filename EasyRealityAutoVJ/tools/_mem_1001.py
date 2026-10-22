# -*- coding: utf-8 -*-
"""把 10-01 的 CPU 诊断写进项目记忆。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-10-01.md")

TXT = """# 2026-10-01 工作日志

## CPU 诊断：8~14 核 / 磁盘 500MB/s（软件正在运行，跑的是 093002）

**现场读数**（`tools/_perf_watch.py 66 3`）：CPU **800~1400%**、磁盘读 **340~553 MB/s**、
GPU 显存 1.35GB / 26~29W、内存 ~900MB。跑的是 `Y:\\EasyRealityAutoVJ\\EasyRealityAutoVJ.exe`
（exe 时间 09-30 18:42 = **093002**）。

**py-spy 定位**：`_soft_frames` 的 `self.c.decode(video=0)` 占 **60.3%** —— 是**软解**。
线程快照 19 个 `autovj-dec` 里 **12 个在睡眠**（解码器缓存已经不泄漏了，白天的修复有效），
只有 4 个在真解码 ⇒ **问题是"在解的那几路是软解"**，不是解码器越积越多。

**根因（扫了素材库全部 279 个文件）**：
- 能硬解 214 个（DXT5≤2K 93 / DXT1≤2K 81 / DXT1-4K 29 / DXT5-4K 11）
- **★ 硬解不了 40 个**：DXV 但帧头是 **DXV2-LZF / YCG6** 等我们不支持的变体
  （`dxvnative.parse_header` 返回 None ⇒ `_try_gpu` 在**材质级**就 return False ⇒
  永久走 `_soft_frames`）
- 非 DXV 19 个（mjpeg 14 / h264 4 / qtrle 1）走 OpenCV；图片 5；DXT1 太小 1
- **软解一路 1080p DXV ≈ 1.5~2 核 + ~150MB/s**。自动图层挑到这些素材时 3~4 路同解
  ⇒ 8~14 核 + 500MB/s，与读数完全吻合。

**结论**：这不是代码 bug，是**素材格式问题**——库里 ~14% 的 DXV 用的是我们解不了的
封装变体。治本 = 把这 40 个转成 DXV3/DXT5；治标 = 代码里给它们降级让路。

报告：`P:\\AutoVJ\\_CPU诊断_20261001.md`。
新增工具：`tools/_gpu_reason.py`（单个素材为何不走 GPU）、
`tools/_lib_codec_scan.py`（整库编解码分布 + 需转码清单）。

## ★ 又一个"沙箱/环境"级的坑（这轮踩的）

`py-spy record` 的输出**必须读全栈**再下结论：`_soft_frames`(1476) 与
`_soft_until_recover`(1444) 长得很像，行号（1480 vs 1454）才是判据 ——
前者是"这个素材永远硬解不了"，后者是"GL 工作线程暂时不可用，在等它恢复"。
**两者要采取的措施完全不同**（转码 vs 等自愈）。
"""

if os.path.isfile(LOG):
    io.open(LOG, "a", encoding="utf-8").write("\n\n" + TXT)
else:
    io.open(LOG, "w", encoding="utf-8", newline="\n").write(TXT)
print("已写入", LOG)
