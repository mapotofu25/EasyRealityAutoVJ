# -*- coding: utf-8 -*-
"""应用图标定位（★ 仅"图标"相关，不含任何业务逻辑）。

打包（PyInstaller onedir）后，随包资源被解到 **`sys._MEIPASS`**（= `_internal/`）；
源码运行时用项目根下的 `assets/`。两处都找不到就返回 None（调用方自行跳过）。
与 `dxvnative.py` / `asio_engine.py` 里"先看 `_MEIPASS` 再看源码目录"的既有约定一致。
"""
from __future__ import annotations

import os
import sys


def icon_path(name: str = "autovj") -> str | None:
    """返回 `<name>.ico` 的绝对路径；找不到返回 None。

    name: `autovj`（主程序） / `send`（音频发送端） / `erlib`（曲库分析）。
    """
    fn = name + ".ico"
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)          # 打包后：随包资源根（_internal）
    if meipass:
        roots.append(os.path.join(meipass, "assets"))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # src/ 的上一级 = 项目根
    roots.append(os.path.join(here, "assets"))
    roots.append(os.path.join(here, "brand", "ico"))  # 兜底：本地品牌目录
    for d in roots:
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return None
