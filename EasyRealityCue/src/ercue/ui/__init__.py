# -*- coding: utf-8 -*-
"""`ercue.ui` —— 唯一允许 import PySide6 的一层。

模块：
    theme.py            配色与尺寸常量
    timeline_widget.py  ★ 自绘时间轴 + 全部交互
    preview.py          预览画布（10fps，只 dirty/playing 时重合成）
    panels.py           区块面板（① 码表包 ② 曲目 ③ 素材）+ 可拖拽素材列表
    props_panel.py      片段属性面板（含旧实现从未做出的**入点/出点**）
    window.py           主窗口（顶栏 / 状态行 / 左标签页 / 预览+传输条 / 时间轴 / 日志）
"""

from __future__ import annotations

from .window import MainWindow, SheetSession

__all__ = ["MainWindow", "SheetSession"]
