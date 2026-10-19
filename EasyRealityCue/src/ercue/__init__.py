# -*- coding: utf-8 -*-
"""EasyRealityCue —— DJ 码表（Cue Sheet）编排编辑器。

纯 Authoring 工具：只做编排，不做实时演出播放。
本项目为「从零重写」：功能需求与 `.ercue` 格式见 `docs/规格书_旧实现考古.md`，
模块划分与重写纪律见 `docs/架构方案.md`。

依赖方向（单向，禁止反向）：
    ui ──▶ render / media / audio / model
                     └────────┬────────┘
                            model（最底层，谁也不依赖，且**绝不 import PySide6**）
"""

__version__ = "0.0.1"
