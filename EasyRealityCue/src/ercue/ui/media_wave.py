# -*- coding: utf-8 -*-
"""★ 素材音轨波形的**后台 worker**（需求 11；Qt 侧，纯计算在 `model`/`media` 侧）。

**冻结接口**（设计文档 §3.3，T03 已按"有就调用"接好）：
    `request(clip_id, path)`  —— 入队（**队列式**，非 latest-wins：多个素材都要算）
    `done(clip_id, data|None)`—— 算完（`None` = 无音轨/算不出）
    `stop()`                  —— 停止（关窗时调）

**不许卡 UI 的三条**：
  1. 全程在**后台线程**（`QThread`），并显式起在 **LowPriority** —— 与预览/播放抢 CPU 时
     让 OS 优先跑 GUI 线程；
  2. **按需入队**（首次打开开关 / 换曲目时，由 window 侧触发），**不是**导入时全量扫；
  3. 每个素材算完 **让出 1ms**（`msleep(1)`）—— 避免连续大块计算把 GIL 长期占住导致掉帧。
     （重活在 `audio/waveform` 的 numpy 分块 FFT 里，那部分本身会释放 GIL。）
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal

from ercue.media import media_wave

log = logging.getLogger("ercue.ui.media_wave")


class MediaWaveWorker(QThread):
    """后台算素材音轨波形：**队列式**、去重、可停。"""

    done = Signal(str, object)          # clip_id, WaveformData | None
    failed = Signal(str, str)           # clip_id, 原因（无音轨 / 算不出）——**失败要可见，不许静默**
    progress = Signal(str, float)       # clip_id, 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._queue = []                # [(clip_id, path)] 待算
        self._queued = set()            # clip_id 去重（已在队/在算的不重复入队）
        self._ev = threading.Event()    # 有新任务时唤醒
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def request(self, clip_id, path):
        """入队一个素材（**去重**：同一 `clip_id` 未算完前不重复入队）。"""
        cid = str(clip_id or "")
        p = str(path or "")
        if not cid or not p or self._stop.is_set():
            return False
        with self._lock:
            if cid in self._queued:
                return False
            self._queued.add(cid)
            self._queue.append((cid, p))
        self._ev.set()
        if not self.isRunning():
            self.start(QThread.LowPriority)     # ★ 低优先级：别和预览/播放抢
        return True

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def stop(self):
        """停止：清队 + 唤醒 + 有界等待（关窗时调，**不阻塞退出过久**）。"""
        self._stop.set()
        with self._lock:
            self._queue.clear()
            self._queued.clear()
        self._ev.set()
        try:
            self.requestInterruption()
        except Exception:                              # noqa: BLE001
            pass
        if self.isRunning():
            try:
                self.wait(2000)
            except Exception:                          # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def run(self):                                     # noqa: D102  (QThread 入口)
        while not self._stop.is_set():
            with self._lock:
                item = self._queue.pop(0) if self._queue else None
            if item is None:
                self._ev.wait(0.2)                     # 空转即可唤醒，不必忙等
                self._ev.clear()
                continue
            cid, path = item
            data = None
            try:
                data = media_wave.analyze(
                    path, progress=lambda f, c=cid: self.progress.emit(c, float(f)))
            except Exception as e:                     # noqa: BLE001
                log.exception("素材波形分析失败: %s", path)
                self.failed.emit(cid, "%s: %s" % (type(e).__name__, e))
            if data is None and not self._stop.is_set():
                self.failed.emit(cid, media_wave.no_audio_reason(path) or "算不出波形（解码失败）")
            if not self._stop.is_set():
                try:
                    self.done.emit(cid, data)
                except Exception:                      # noqa: BLE001
                    log.exception("投递素材波形失败")
            with self._lock:
                self._queued.discard(cid)
            self.msleep(1)                             # ★ 让出：别把 GIL 长期占住
