# -*- coding: utf-8 -*-
"""素材探测后台 worker（时长 / alpha）。

`probe.probe()` 会起 ffprobe / 解码部分帧，几秒钟一个文件；放在 UI 线程里
点「添加素材」就会整窗卡住。这里用**队列式 QThread** 串行探测，结果回主线程
后由 `MainWindow` 写回对应的 `MediaRef`。

失败不弹窗（与旧同步实现一致）：探测失败只记日志，素材照常可编辑，
precheck 少一条超长提示而已。
"""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal

from ercue.media import probe

log = logging.getLogger("ercue.ui.media_probe")


class MediaProbeWorker(QThread):
    """按 `(session_key, clip_id, path)` 去重、可停的后台探测队列。"""

    done = Signal(str, str, str, object)   # session key, clip_id, path, result dict
    failed = Signal(str, str, str)         # session key, clip_id, reason

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ercue-media-probe")
        self._lock = threading.Lock()
        self._queue = []
        self._queued = set()
        self._ev = threading.Event()
        self._stop = threading.Event()

    def request(self, session_key, clip_id, path):
        key = str(session_key or "")
        cid = str(clip_id or "")
        p = str(path or "")
        if not cid or not p or self._stop.is_set():
            return False
        token = (key, cid, p)
        with self._lock:
            if token in self._queued:
                return False
            self._queued.add(token)
            self._queue.append(token)
        self._ev.set()
        if not self.isRunning():
            self.start(QThread.LowPriority)
        return True

    def stop(self):
        """清队 + 唤醒 + 有界等待；调用方再用 `orphan_thread` 兜底。"""
        self._stop.set()
        with self._lock:
            self._queue.clear()
            self._queued.clear()
        self._ev.set()
        try:
            self.requestInterruption()
        except Exception:                                    # noqa: BLE001
            pass
        if self.isRunning():
            try:
                self.wait(2000)
            except Exception:                                # noqa: BLE001
                pass

    def run(self):                                           # noqa: D102 - QThread 入口
        while not self._stop.is_set():
            with self._lock:
                item = self._queue.pop(0) if self._queue else None
            if item is None:
                self._ev.wait(0.2)
                self._ev.clear()
                continue
            key, cid, path = item
            try:
                r = probe.probe(path)
            except Exception as e:                           # noqa: BLE001
                log.exception("素材探测失败: %s", path)
                if not self._stop.is_set():
                    self.failed.emit(key, cid, "%s: %s" % (type(e).__name__, e))
            else:
                if r is not None and getattr(r, "ok", False):
                    data = {
                        "duration_sec": float(getattr(r, "duration_sec", 0.0) or 0.0),
                        "has_alpha": bool(getattr(r, "has_alpha", False)),
                    }
                    if not self._stop.is_set():
                        self.done.emit(key, cid, path, data)
                elif not self._stop.is_set():
                    self.failed.emit(key, cid, "probe 未成功")
            with self._lock:
                self._queued.discard(item)
            self.msleep(1)
