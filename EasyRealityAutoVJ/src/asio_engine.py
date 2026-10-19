# -*- coding: utf-8 -*-
"""ASIO 低延迟输入 —— Python 侧封装（ctypes 调 `_asiohost.dll`）。

## 为什么
项目原来只有 WASAPI（`soundcard`），共享模式往返延迟 20~60 ms。
现场要的是「线路输入 → 能量/拍子分析」尽量跟上人耳，所以加了 ASIO
（典型 1~10 ms）。原生宿主见 `src/native/asiohost.cpp`，用
`tools/build_asio.py` 编译。

## 实测结论（2026-10-01，本机 Realtek ASIO）
| 项 | 值 |
|---|---|
| 采样率 | 48000 Hz（**必须显式设一次**，见下） |
| 缓冲 | 528 帧 ≈ 11.0 ms |
| 驱动报的输入延迟 | 528 帧 ≈ 11.0 ms |
| 样本类型 | `ASIOSTInt32LSB24`（我们用顶 24 位对齐的 32 位字，按 2^31 归一正确） |
| 收数 | 1.2 秒 58608 帧 / 111 次回调 ✓ |

### ⚠⚠ 踩到的关键坑（已修，别再踩回去）
**不调 `ASIOSetSampleRate()` 时，Realtek ASIO 的 `ASIOInit` / `ASIOCreateBuffers` /
`ASIOStart` 全部返回成功，但回调一次都不触发**（收不到任何样本）。
必须**显式设一次采样率**（哪怕就是驱动当前报的 48000）。原生侧已固化这个行为。
另外**必须输入+输出缓冲一起建**（回调时钟跟着输出走），只建输入同样没有回调。

## 接口约定
对外只暴露三样，够用且不容易用错：
  · `available()` → (能否用, 原因)         —— 给界面判断要不要显示 ASIO 选项
  · `list_drivers()` → [驱动名]            —— 注册表里注册的 ASIO 驱动
  · `AsioDevice` + `AsioCapture`           —— 采集；`AsioDevice` 刻意做成
    **soundcard 的 recorder 形状**，好让 `audio_engine._capture_loop` 原样复用。
"""

import ctypes
import os
import sys
import threading
import time

import numpy as np

# 一次最多拉多少帧（与原生环形缓冲是 2 的幂，取 4096 够细）
READ_CHUNK = 4096

_dll = None
_dll_lock = threading.Lock()
_dll_err = ""


def _dll_path():
    """找 `_asiohost.dll`：优先程序目录（打包后就在 exe 旁边），再找源码目录。"""
    cands = []
    try:
        base = getattr(sys, "_MEIPASS", None)
        if base:
            cands.append(os.path.join(base, "_asiohost.dll"))
    except Exception:                                          # noqa: BLE001
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "_asiohost.dll"))
    cands.append(os.path.join(here, "native", "_asiohost.dll"))
    try:
        from config import app_base_dir
        cands.append(os.path.join(app_base_dir(), "_asiohost.dll"))
    except Exception:                                          # noqa: BLE001
        pass
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def _load():
    """惰性加载 DLL（失败只记一次原因，不抛）。"""
    global _dll, _dll_err
    if _dll is not None:
        return _dll
    with _dll_lock:
        if _dll is not None:
            return _dll
        p = _dll_path()
        if not p:
            _dll_err = "找不到 _asiohost.dll（需要先跑 tools/build_asio.py，打包时随包带上）"
            _dll = False
            return _dll
        try:
            try:
                os.add_dll_directory(os.path.dirname(p))
            except Exception:                                  # noqa: BLE001
                pass
            d = ctypes.CDLL(p)
            d.avj_asio_count.restype = ctypes.c_int
            d.avj_asio_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            d.avj_asio_name.restype = ctypes.c_int
            d.avj_asio_last_error.restype = ctypes.c_char_p
            d.avj_asio_is_open.restype = ctypes.c_int
            d.avj_asio_cur_rate.restype = ctypes.c_int
            d.avj_asio_buffer_frames.restype = ctypes.c_int
            d.avj_asio_blocks.restype = ctypes.c_int
            d.avj_asio_overflows.restype = ctypes.c_int
            d.avj_asio_pending.restype = ctypes.c_int
            d.avj_asio_open.argtypes = [
                ctypes.c_char_p, ctypes.c_double, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.c_char_p, ctypes.c_int]
            d.avj_asio_open.restype = ctypes.c_int
            d.avj_asio_read.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_int]
            d.avj_asio_read.restype = ctypes.c_int
            d.avj_asio_close.restype = ctypes.c_int
            d.avj_asio_latency_frames.argtypes = [ctypes.POINTER(ctypes.c_int),
                                                  ctypes.POINTER(ctypes.c_int)]
            d.avj_asio_channel_info.argtypes = [
                ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
            d.avj_asio_channel_info.restype = ctypes.c_int
            d.avj_asio_debug.argtypes = [ctypes.c_char_p, ctypes.c_int]
            d.avj_asio_debug.restype = ctypes.c_int
            _dll = d
        except Exception as e:                                 # noqa: BLE001
            _dll_err = "加载 _asiohost.dll 失败：%s" % e
            _dll = False
    return _dll


def available():
    """(能否用, 原因)。给界面判断 ASIO 选项要不要灰掉。"""
    d = _load()
    if not d:
        return False, _dll_err
    try:
        n = int(d.avj_asio_count())
    except Exception as e:                                     # noqa: BLE001
        return False, "枚举 ASIO 驱动失败：%s" % e
    if n <= 0:
        return False, "本机没有注册任何 ASIO 驱动（装了声卡驱动才会有）"
    return True, ""


def list_drivers():
    """注册表里注册的 ASIO 驱动名列表（失败返回 []）。"""
    d = _load()
    if not d:
        return []
    out = []
    try:
        for i in range(int(d.avj_asio_count())):
            buf = ctypes.create_string_buffer(256)
            if d.avj_asio_name(i, buf, 256) == 0:
                out.append(buf.value.decode("mbcs", "replace"))
    except Exception:                                          # noqa: BLE001
        pass
    return out


def list_channels(driver):
    """列出某驱动可用的**输入通道**。

    返回 (通道列表, 错误说明)。通道列表每项 `{"index": i, "name": str, "type": int}`；
    失败（驱动被独占 / 装不上）返回 ([], 原因) —— 这时界面就让用户按**序号**选。

    ★ 为什么要给用户看通道名（用户问「ASIO 能采集指定输出吗」）：
      ASIO **抓不到别的程序播出来的声音**（没有 loopback 概念），它能做的是选**硬件输入**。
      但有些驱动的输入里**自带 Loopback 通道**（名字里带 Loopback 之类），
      把名字列出来，用户才能选中那两路 —— 这是"抓输出"在 ASIO 侧唯一可行的形式。
    """
    d = _load()
    if not d:
        return [], _dll_err
    drv = driver or ""
    if drv.startswith("ASIO: "):
        drv = drv[6:].strip()
    if not drv:
        names = list_drivers()
        if not names:
            return [], "本机没有注册任何 ASIO 驱动"
        drv = names[0]
    nin = ctypes.c_int(0)
    nout = ctypes.c_int(0)
    buf = ctypes.create_string_buffer(16384)
    rc = d.avj_asio_channel_info(drv.encode("mbcs"), ctypes.byref(nin),
                                 ctypes.byref(nout), buf, 16384)
    if rc != 0:
        return [], (d.avj_asio_last_error() or b"").decode("utf-8", "replace") \
            or ("错误码 %d" % rc)
    out = []
    # ⚠ 通道名是驱动给的 ANSI 原文 ⇒ 按 mbcs 解码（错误信息才是 UTF-8，两者不混）
    for line in buf.value.decode("mbcs", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            out.append({"index": int(parts[0]), "name": parts[1], "type": int(parts[2])})
        except Exception:                                          # noqa: BLE001
            continue
    return out, ""


def debug_info():
    """一行诊断字符串（排查"打开了但没数据"）。"""
    d = _load()
    if not d:
        return _dll_err
    try:
        b = ctypes.create_string_buffer(512)
        d.avj_asio_debug(b, 512)
        return b.value.decode("mbcs", "replace")
    except Exception as e:                                     # noqa: BLE001
        return "取诊断失败：%s" % e


class AsioCapture:
    """一次 ASIO 采集会话。**必须 close()**（建议用 with）。

    ## ⚠⚠ 为什么 open/close 要跑到一条**自建的 STA 线程**上
    2026-10-01 实测踩到：ASIO 驱动是 in-proc COM 组件，而且把**自己的 CLSID 当接口 IID**
    （`CoCreateInstance(clsid,...,clsid,...)`）。这种自引用 IID 无法跨套间 marshaling
    ⇒ **从 MTA 线程实例化必然失败**。而本项目的音频工作线程因为 `soundcard`
    已经做过 `CoInitializeEx(MTA)` —— 在那里直接 open 会报"装载驱动失败（被独占）"，
    极难查（同样的代码在主线程上跑就成功，因为 Qt 主线程是 STA）。
    ⇒ 所以：`open()` / `close()` 一律投递到**自建的 STA 线程**执行；
      `read()` 只读原生环形缓冲、不碰 COM，留在调用线程（零额外开销）。
    """

    def __init__(self, driver=None, sr=0.0, ch0=0, ch1=1, buf_frames=0):
        self.driver = driver or ""
        self.want_sr = float(sr or 0.0)
        self.ch0, self.ch1 = int(ch0), int(ch1)
        self.want_buf = int(buf_frames or 0)
        self.sample_rate = 0
        self.buffer_frames = 0
        self._buf = (ctypes.c_float * (READ_CHUNK * 2))()
        self._open = False
        self._com_q = None
        self._com_t = None

    # ---------------- 自建 STA 线程 ----------------
    def _com_loop(self):
        try:
            # COINIT_APARTMENTTHREADED = 0x2
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        except Exception:                                      # noqa: BLE001
            pass
        while True:
            item = self._com_q.get()
            if item is None:
                break
            fn, ev, box = item
            try:
                box.append((True, fn()))
            except Exception as e:                             # noqa: BLE001
                box.append((False, e))
            finally:
                ev.set()

    def _on_com(self, fn, timeout=25.0):
        """把 fn 投到 STA 线程执行并等结果（超时抛 TimeoutError，绝不永久卡住）。"""
        if self._com_t is None:
            import queue as _q
            self._com_q = _q.Queue()
            self._com_t = threading.Thread(target=self._com_loop, daemon=True,
                                           name="autovj-asio-com")
            self._com_t.start()
        ev = threading.Event()
        box = []
        self._com_q.put((fn, ev, box))
        if not ev.wait(timeout):
            raise TimeoutError("ASIO COM 线程无响应（%.0fs）" % timeout)
        ok, val = box[0]
        if not ok:
            raise val
        return val

    def _stop_com(self):
        try:
            if self._com_q is not None:
                self._com_q.put(None)
            if self._com_t is not None:
                self._com_t.join(2.0)
        except Exception:                                      # noqa: BLE001
            pass
        self._com_t = None

    # ---- 打开 ----
    def _do_open(self):
        d = _load()
        if not d:
            return False, _dll_err
        drv = self.driver
        if not drv:
            names = list_drivers()
            if not names:
                return False, "本机没有注册任何 ASIO 驱动"
            drv = names[0]
            self.driver = drv
        out_sr = ctypes.c_int(0)
        out_buf = ctypes.c_int(0)
        err = ctypes.create_string_buffer(512)
        rc = d.avj_asio_open(drv.encode("mbcs"), self.want_sr, self.ch0, self.ch1,
                             self.want_buf, ctypes.byref(out_sr), ctypes.byref(out_buf),
                             err, 512)
        if rc != 0:
            # ⚠ 错误文本是 C++ 源码里的 UTF-8 字面量，不是 mbcs
            msg = err.value.decode("utf-8", "replace") or ("错误码 %d" % rc)
            return False, msg
        self.sample_rate = int(out_sr.value) or 48000
        self.buffer_frames = int(out_buf.value)
        self._open = True
        return True, ""

    def open(self):
        """返回 (ok, err)。ok=False 时 err 是可读原因。"""
        try:
            return self._on_com(self._do_open)
        except Exception as e:                                 # noqa: BLE001
            return False, "%s: %s" % (type(e).__name__, e)

    # ---- 读 ----
    def read(self, max_frames=READ_CHUNK):
        """拉取最多 max_frames 帧，返回 (n, 2) float32 数组（可能为空）。"""
        if not self._open:
            return None
        d = _dll
        n = int(d.avj_asio_read(self._buf, int(max_frames)))
        if n <= 0:
            return None
        # ⚠ 必须 copy：ctypes 缓冲是复用的，直接把视图交出去会被下一次读覆盖
        return np.frombuffer(self._buf, dtype=np.float32, count=n).reshape(-1, 2).copy()

    # ---- 状态 ----
    def latency_frames(self):
        if not self._open:
            return 0
        a = ctypes.c_int(0)
        b = ctypes.c_int(0)
        try:
            _dll.avj_asio_latency_frames(ctypes.byref(a), ctypes.byref(b))
        except Exception:                                      # noqa: BLE001
            return 0
        return int(a.value)

    def latency_ms(self):
        sr = self.sample_rate or 48000
        return self.latency_frames() * 1000.0 / sr

    def blocks(self):
        return int(_dll.avj_asio_blocks()) if self._open else 0

    def overflows(self):
        return int(_dll.avj_asio_overflows()) if self._open else 0

    def pending(self):
        return int(_dll.avj_asio_pending()) if self._open else 0

    def debug(self):
        return debug_info()

    def _do_close(self):
        if self._open:
            try:
                _dll.avj_asio_close()
            except Exception:                                  # noqa: BLE001
                pass
            self._open = False
        return True

    def close(self):
        """关闭会话。⚠ 必须在**初始化它的那条 STA 线程**上收尾（CoUninitialize 的要求），
        所以走 `_on_com`；随后把那线程收掉。"""
        try:
            if self._com_t is not None:
                self._on_com(self._do_close)
        except Exception:                                      # noqa: BLE001
            # COM 线程已死/超时：退而求其次，直接关（可能留下未释放的驱动句柄，
            # 但总比把线程挂在这里强）
            try:
                _dll.avj_asio_close()
            except Exception:                                  # noqa: BLE001
                pass
            self._open = False
        self._stop_com()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _AsioRecorder:
    """把 `AsioCapture` 伪装成 `soundcard` 的 recorder。

    ⚠ 为什么要伪装：`audio_engine._capture_loop()` 里那一整套下游逻辑
      （能量分析 / 拍钟 / 指纹 / 曲风环 / NDI 转发）已经写好且经过现场验证，
      为了接 ASIO 再抄一遍等于把风险复制一份。
      这里只提供 `.record(numframes=...)`，与 soundcard 的签名一致 ⇒ 主体零改动。
    """

    def __init__(self, cap):
        self.cap = cap

    def record(self, numframes=1024):
        """凑够 numframes 帧（或超时）就返回 (n, 2) float32；没数据返回 None。"""
        want = int(numframes)
        chunks = []
        got = 0
        deadline = time.perf_counter() + 0.5      # 最多等 0.5 秒，绝不永久阻塞
        while got < want and time.perf_counter() < deadline:
            d = self.cap.read(min(READ_CHUNK, want - got))
            if d is None or len(d) == 0:
                time.sleep(0.002)
                continue
            chunks.append(d)
            got += len(d)
        if not chunks:
            return None
        if len(chunks) == 1:
            return chunks[0]
        try:
            return np.concatenate(chunks, axis=0)
        except Exception:                                      # noqa: BLE001
            return chunks[0]


class AsioDevice:
    """设备对象（模仿 soundcard 的 device）：`.name` + `.recorder(...)`。

    ⚠ recorder 的 `channels` 参数被忽略：ASIO 侧固定双声道（第 ch0/ch1 路输入）。
    """

    def __init__(self, cap):
        self._cap = cap
        self.name = "ASIO: %s" % (cap.driver or "?")

    def recorder(self, samplerate=None, channels=None, blocksize=None):
        """返回上下文管理器（soundcard 的 recorder 也是 with 用法）。"""
        cap = self._cap

        class _Ctx:
            def __enter__(self):
                return _AsioRecorder(cap)

            def __exit__(self, *a):
                # ⚠ 不在这里 close：会话由 AudioCapture 统一管（要能跨代关闭）
                return False

        return _Ctx()
