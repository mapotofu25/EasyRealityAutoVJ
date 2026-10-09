# -*- coding: utf-8 -*-
"""erlib —— 曲库分析器（**DJ 端独立小软件**）。

## 用途
在自己的曲库上跑一遍分析，导出成 `.eravjlib` 包（**不含任何音频文件**，
只有曲风/封面/指纹/节拍网格），拿到演出机的 ERAVJ 里导入。
之后 DJ 放歌时，演出机能在几秒内**认出正在放的是哪首歌**，
自动套用曲风、封面和**离线节拍网格**（画面切在八拍乐句头上）。

## 为什么不塞进 ERAVJ 主程序
1. DJ 机器不必装整个 ERAVJ（主程序打包 460 MB，这个小很多）；
2. 扫描很吃 CPU/磁盘（指纹约 2.4 秒/首），独立进程不干扰别的事；
3. 崩溃隔离 —— 扫描端出问题不会影响演出机。

## 用法
双击运行 → 选曲库文件夹 → 开始扫描（可挂着跑）→ 勾选要导出的歌 → 导出。

⚠ 2026-10-10：本文件曾丢失（未被 git 跟踪），从 `__pycache__/erlib_main.cpython-313.pyc`
   的字节码逐指令还原重建，已 `git add` 防再丢。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "src"), _HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from erlib.app import main

if __name__ == "__main__":
    sys.exit(main())
