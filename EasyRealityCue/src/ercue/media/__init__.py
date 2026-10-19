# -*- coding: utf-8 -*-
"""素材层（M1）—— 探测 / 缩略图 / 取帧缓存。

★★ 硬约束（架构方案 §1 原则 3）：**本包绝不 import PySide6 / 任何 Qt**。
本层只产出 **numpy 数组 / bytes / 普通 dataclass**，由 `ui/` 层自行转成
`QImage` / `QPixmap`。自检方式（AST 扫 import 节点，断言 0 命中 Qt）见
`tests/test_media.py::test_zero_qt`。

模块：
    probe.py    素材探测：时长 / fps / 分辨率 / 是否真带 alpha（两层判定）
    thumbs.py   缩略图：视频多帧采样选优 → PNG 字节（alpha 素材保留 alpha）
    frames.py   ★ CueFrameSource：按路径复用解码器 + 失败退避 + 随机 seek + 取帧缓存

设计依据：
    docs/架构方案.md §4-A（A1 复用 / A2 退避 / A3 只 dirty 重合成 / A4 释放）
    docs/技术选型.md §A2（解码与 seek）/ §A3（缩略图）
    docs/规格书_旧实现考古.md §6.3（每帧重建解码器）/ §6.4（失败素材硬盘狂读）/ §6.5
"""

from __future__ import annotations

from .probe import ProbeResult, kind_of
from .thumbs import make_thumb, cache_key, resolve_thumb
from .frames import CueFrameSource

__all__ = [
    "ProbeResult", "kind_of",
    "make_thumb", "cache_key", "resolve_thumb",
    "CueFrameSource",
]

# 备注：本包**不**在顶层 re-export `probe()` 函数。
# 因为包名下的 `probe` 若绑定成函数，会**遮蔽**同名的子模块 `ercue.media.probe`，
# 导致 `from ercue.media import probe as probe_mod` 拿到的是函数而不是模块。
# 约定：探测函数请 `from ercue.media.probe import probe` 或 `ercue.media.probe.probe(path)`。

