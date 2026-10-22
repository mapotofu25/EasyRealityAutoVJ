# -*- coding: utf-8 -*-
"""`ercue.model` —— 纯逻辑数据层（**零 Qt**）。

本包下任何文件都不允许 `import PySide6`（架构方案 §1 原则 3、§4 纪律）。
可脱离 Qt 直接跑自检：
    python -m ercue.model.units
    python -m ercue.model.features
    python -m ercue.model.cuesheet
    python -m ercue.model.ercue_io
"""

from __future__ import annotations
