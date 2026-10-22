# -*- coding: utf-8 -*-
"""音频层：解码到内存（`AudioTrack`）+ 声卡走带（`AudioPlayer`）。

★ 本包**纯逻辑 + 声卡**，**不准 import PySide6**（架构方案 §1 原则 3 / §2 依赖方向）。
  它是 `ui/` 的下层，被预览走带驱动使用，但不依赖 Qt。
"""

from .player import AudioPlayer, AudioTrack

__all__ = ["AudioPlayer", "AudioTrack"]
