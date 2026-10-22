# -*- coding: utf-8 -*-
"""`ercue.render` —— 预览合成层（**允许使用 Qt**）。

依赖方向：`render ──▶ model`（只读 `timeline.LayerDrawState` 数据类）。
本层**不碰解码**：帧由调用方经 `frame_lookup(clip_id, play_sec) -> numpy(BGRA) | None` 注入
（见 `render.compositor`；旧单参回调亦兼容）。
"""

from __future__ import annotations

from .compositor import BLEND_QT, Compositor, compose

__all__ = ["BLEND_QT", "Compositor", "compose"]
