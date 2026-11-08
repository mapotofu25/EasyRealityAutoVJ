# -*- coding: utf-8 -*-
"""Spout 输出：**渲染线程只拷贝+入队，native 提交在专用线程**。

为什么要这个文件（2026-10-29 现场「预览和输出都一卡一卡」）：

    `engine._spout_send()` 原来在 `_tick_locked`（渲染线程）里**同步**调
    `s.sendImage(buf, w, h, ...)`。实测（tools/_probe_spout.py）：

        720p   中位 1.01ms  p95 1.44ms  **max 14327ms**
        1080p  中位 2.04ms  p95 2.77ms  **max   217ms**

    也就是说 `sendImage` 会偶发阻塞到十几秒。它跑在渲染线程里 ⇒ 整个 tick 停住
    ⇒ **预览面板和输出窗口同时冻**，而 GUI 主线程没事（所以 `ui_stall.log` 干净、
    `layer_health.log` 里好几层 age 一起飙）。用户对照"OBS 的 Spout/NDI 输出不卡"
    也正好说明：问题不在 Spout 技术本身，而在**我们用同步调用的方式用它**。

    这与 NDI 那条路的结论完全一样（见 `ndi_out.send_video` 注释里的 14ms 实测），
    NDI 早在 2026-10-08 就改成了"入队 + 专用线程"，Spout 一直漏了。

设计要点：
  · **GL 上下文是线程绑定的**：`SpoutSender()` / `createOpenGL()` 必须和 `sendImage()`
    在同一线程 ⇒ 整个 sender 的生命周期都在发送线程里，渲染线程绝不碰 SDK。
  · 渲染线程只做一次 `bytearray(...)` 拷贝（实测 720p ≈ 1.7ms，max 2.8ms）。
    ⚠ 绝不能用 `bytes(...)`：`sendImage` 的形参要求**可写** buffer（同 NDI 的
      `BufferError: Object is not writable` 坑）。
  · 队列只留**最新一帧**（`maxsize=1`）：发送线程慢的时候丢旧帧，绝不排队积压。
  · 发送线程里的异常只记录、不抛出，由 `last_error()` 交给引擎走既有的
    退避重试（`_output_fail`），**绝不永久停**。
"""
import queue
import threading
import time
import traceback


class SpoutOut:
    """Spout 发送端。`send()` 可在渲染线程安全调用（只拷贝+入队）。"""

    def __init__(self, name="EasyRealityAutoVJ"):
        self.name = str(name or "EasyRealityAutoVJ")
        self._q = queue.Queue(maxsize=1)
        self._dead = False
        self._thr = None
        self._lock = threading.Lock()
        self._err = None            # 最近一次发送线程异常（None = 正常）
        self._sent = 0              # 成功提交帧数（诊断用）
        self._longest_ms = 0.0      # 单帧 sendImage 最长耗时（诊断用）
        self._size = (0, 0)
        # ★ 2026-11-08（输出偶发卡顿）：**复用发送缓冲**，不再每帧分配 3.7MB bytearray。
        #   每帧新建大 buffer 会持续推高 Python GC/分配压力；Spout 发送线程拿到 GIL 时
        #   偶发一次长时间停顿，渲染线程和执行输出一起卡住。缓冲在渲染线程/发送线程
        #   之间通过队列交接，任意时刻只属于一方，无需加长锁。
        self._free = []
        self._buf_max = 4

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        if self._thr is not None and self._thr.is_alive():
            return True
        self._thr = threading.Thread(target=self._loop, daemon=True,
                                     name="autovj-spout-send")
        self._thr.start()
        return True

    def close(self):
        self._dead = True
        try:
            while True:
                item = self._q.get_nowait()
                if item is not None:
                    with self._lock:
                        self._free.append(item[0])
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thr is not None:
            self._thr.join(timeout=1.0)
            self._thr = None
        with self._lock:
            self._free.clear()

    # ---------------------------------------------------------------- 渲染线程侧
    def send(self, bits, w, h, bpl=0):
        """渲染线程调用：**只拷贝 + 入队**，绝不接触 SDK。

        `bits`：可读 buffer（通常是 `QImage.bits()`，行序自上而下、BGRA）。
        `bpl`：源行字节数（`QImage.bytesPerLine()`）；0 或等于 w*4 时走整块拷贝。
        """
        w = int(w)
        h = int(h)
        if w <= 0 or h <= 0:
            return False
        need = w * 4
        total = need * h
        try:
            mv = memoryview(bits)
        except TypeError:
            return False
        # ⚠ `len(memoryview)` 是**第一维长度**，不是字节数：`QImage.bits()` 给 1 维
        #   （len==字节数），但 numpy 的 (h, w, 4) 视图 len==h ⇒ 用 nbytes 判长度，
        #   再 cast 成字节视图拷贝。用 len() 会让每帧都被长度校验挡掉、一帧都发不出去。
        if mv.nbytes < total:
            return False
        try:
            if mv.ndim != 1 or mv.format != "B" or mv.itemsize != 1:
                mv = mv.cast("B")
        except TypeError:
            pass
        if len(mv) < total:
            return False
        # ★ 复用缓冲（见 __init__ 说明）：优先从 free 池取，池空才分配。
        buf = None
        with self._lock:
            while self._free:
                b = self._free.pop()
                if len(b) == total:
                    buf = b
                    break
                if len(b) > total:
                    buf = b
                    break
        if buf is None:
            buf = bytearray(total)
        # 拷贝：`bits` 是画布的活动视图，渲染线程下一帧就会覆写（同 NDI 的坑）
        if bpl and bpl != need:
            for y in range(h):
                s = y * bpl
                buf[y * need:(y + 1) * need] = mv[s:s + need]
        else:
            buf[:total] = mv[:total]
        self._size = (w, h)
        self.start()
        # 只保留最新帧：满了就丢掉旧的（旧缓冲立即回收，绝不泄漏）
        try:
            self._q.put_nowait((buf, w, h))
        except queue.Full:
            try:
                old = self._q.get_nowait()
                if old is not None:
                    with self._lock:
                        self._free.append(old[0])
            except queue.Empty:
                pass
            try:
                self._q.put_nowait((buf, w, h))
            except queue.Full:
                with self._lock:
                    self._free.append(buf)
        return True

    def last_error(self):
        """返回最近一次发送线程异常（字符串）；None = 正常。取走即清。"""
        with self._lock:
            e = self._err
            self._err = None
        return e

    def stats(self):
        with self._lock:
            return {"sent": self._sent, "longest_ms": self._longest_ms,
                    "size": self._size}

    # ---------------------------------------------------------------- 发送线程
    def _loop(self):
        try:
            from SpoutGL import SpoutSender
            from SpoutGL.enums import GL_BGRA_EXT
        except Exception:                                          # noqa: BLE001
            with self._lock:
                self._err = "SpoutGL 不可导入：%s" % traceback.format_exc(limit=1)
            return
        s = None
        try:
            s = SpoutSender()
            s.setSenderName(self.name)
            # ★ createOpenGL 必须在本线程：GL 上下文线程绑定（实测专用线程可行）
            s.createOpenGL()
            while not self._dead:
                try:
                    item = self._q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                buf, w, h = item
                t0 = time.perf_counter()
                try:
                    s.sendImage(buf, w, h, GL_BGRA_EXT, False, 0)
                finally:
                    # 缓冲交还给渲染线程复用（限制上限，分辨率切换时旧尺寸自然淘汰）
                    with self._lock:
                        if len(self._free) < self._buf_max:
                            self._free.append(buf)
                dt = (time.perf_counter() - t0) * 1000
                with self._lock:
                    self._sent += 1
                    if dt > self._longest_ms:
                        self._longest_ms = dt
        except Exception:                                          # noqa: BLE001
            with self._lock:
                self._err = traceback.format_exc()
        finally:
            if s is not None:
                try:
                    s.releaseSender()
                except Exception:                                  # noqa: BLE001
                    pass
