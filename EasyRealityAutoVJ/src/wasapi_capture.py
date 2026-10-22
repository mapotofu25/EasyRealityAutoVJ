# -*- coding: utf-8 -*-
"""WASAPI 原生采集层（带真实时间戳）—— 修 NDI 音频「随机咔哒」。

## 为什么必须自己写（2026-10-08 取证结论）

`soundcard` 库的 loopback 采集有三个致命问题，全部实测确认：

1. **丢弃了驱动给的真实时间戳。**
   `mediafoundation.py:699` 调 `GetBuffer(..., flags, _ffi.NULL, _ffi.NULL)`
   —— 第 5/6 个参数（`pu64DevicePosition` / **`pu64QPCPosition`**）**被故意传 NULL**。
   于是我们拿不到「这一包音频的真实时刻」，只能把它当"连续 48k 流"处理。

2. **攒块导致相位抖动。**
   `record(numframes=1024)` 会在用户态把驱动给的包重切成 1024 帧才返回。
   实测（`tools/_ndi_audio_probe4.py`）：
        numframes=1024 → 出块间隔均值 21.31ms、**P99 31.0ms**、速率比 0.9983
        numframes=480  → 出块间隔均值 10.00ms、**P99 11.7ms**、速率比 1.0001
   即按 1024 攒块会引入 ±10ms 抖动 + **0.17% 累计亏空**。

3. **静音时自己填零。**
   `_record_chunk()` 在 `GetNextPacketSize()==0` 持续超过 `deviceperiod*4` 时
   **返回一整段 zeros**，帧数还是「采样率 × 估算时长」——硬凑出来的块与真实音频
   边界不连续 ⇒ 咔哒。

而 WASAPI 驱动本身会通过这些标志位**明确告知**不连续：
    `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY`（缓冲被驱动丢弃/错位）
    `AUDCLNT_BUFFERFLAGS_SILENT`（本包静音）
`soundcard` 对前者的处理只有一句 `warnings.warn(...)`，而 `audio_engine` 顶部
又把它 `filterwarnings("ignore")` 掉了 —— 于是这个问题**在现场被完全屏蔽**，
拖了很久。

## 本模块做什么

**只做一件事：把驱动原始包（含真实 QPC 时间戳与标志位）按设备周期取出，原样交给上层。**
- 做法是**继承 `soundcard.mediafoundation._Recorder`**，只重写 `_capture_buffer()`
  与 `record()` —— 初始化 / Start / Stop / channelmap 全部复用，风险最低。
- 接口与 `soundcard` 的 recorder **完全一致**（`.record(numframes)` / with 用法）
  ⇒ `audio_engine._capture_loop` 一行都不用改。
- 额外暴露 `.last_qpc`（本包首帧的 QPC=100ns）、`.last_flags`、`.disc_count` /
  `.silent_count`，供 `ndi_out` 打**真实时间的 NDI timecode**并做现场取证。
- **绝不填零、绝不重切**：驱动给多少就返回多少。

## 线程/COM 约束（沿用项目铁律）

`soundcard` 导入时会在**当前线程** `CoInitializeEx(MTA)`。本模块**复用 soundcard
已建立的 COM 环境**，因此同样**只能在那条常驻后台线程上使用**，
绝不能在 Qt 主线程触碰（否则 IFileDialog 卡死）。
"""
import time

import numpy as np

# WASAPI 标志位
# https://learn.microsoft.com/en-us/windows/win32/api/audioclient/ne-audioclient-_audclnt_bufferflags
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4

_SILENT_EPS = 1e-7


def _make_recorder_cls():
    """延迟构建子类：必须先 import soundcard（它会在导入时 CoInitializeEx）。"""
    from soundcard import mediafoundation as mf

    class _TimedRecorder(mf._Recorder):
        """在 soundcard 的 _Recorder 上保留真实 QPC 时间戳与标志位。"""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.last_qpc = 0            # 本包首帧的 QPC（100ns）
            self.last_frames = 0         # 本包帧数
            self.last_flags = 0          # 本包标志位
            self.disc_count = 0          # DATA_DISCONTINUITY 次数（诊断）
            self.silent_count = 0        # SILENT 次数（诊断）
            self.ts_err_count = 0        # TIMESTAMP_ERROR 次数（诊断）
            self._is_first_pkt = True

        def _capture_buffer(self):
            """照抄父类，但把 device/QPC 两个时间戳参数填上**真实指针**。"""
            ffi = mf._ffi
            data = ffi.new("BYTE**")
            toread = ffi.new("UINT32*")
            flags = ffi.new("DWORD*")
            pdev = ffi.new("UINT64*")     # ← 父类传 NULL，我们填真实指针
            pqpc = ffi.new("UINT64*")     # ← 同上：真实 QPC（100ns）
            pp = self._ppCaptureClient
            hr = pp[0][0].lpVtbl.GetBuffer(pp[0], data, toread, flags, pdev, pqpc)
            mf._com.check_error(hr)
            return data[0], toread[0], flags[0], int(pqpc[0])

        def record(self, numframes=1024):
            """返回最多 numframes 帧的 (n, ch) float32；没数据时返回 None。

            ⚠ 与 soundcard 的**关键差别**：这里**绝不填零、绝不静音补齐**。
              驱动给多少就是多少；凑不满 numframes 也照样返回（上层自己攒）。
              只有这样时间轴才与真实时间一一对应。
            """
            want = int(numframes or 0)
            ffi = mf._ffi
            deadline = time.perf_counter() + 0.5      # 最多等 0.5s，绝不永久阻塞
            chunks = []
            got = 0
            first_qpc = 0
            first_flags = 0
            while got < want and time.perf_counter() < deadline:
                if self._capture_available_frames() <= 0:
                    # 驱动暂时没有帧：**短睡**再问（不填零）
                    time.sleep(0.001)
                    continue
                try:
                    data_ptr, nframes, flags, qpc = self._capture_buffer()
                except Exception:                                  # noqa: BLE001
                    break
                if self._is_first_pkt:
                    flags &= ~AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY
                    self._is_first_pkt = False
                if flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY:
                    self.disc_count += 1
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    self.silent_count += 1
                if flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR:
                    self.ts_err_count += 1
                    qpc = 0
                nch = len(set(self.channelmap))
                if nframes == 0 or data_ptr == ffi.NULL:
                    continue
                raw = bytes(ffi.buffer(data_ptr, nframes * 4 * nch))
                arr = np.frombuffer(raw, dtype=np.float32).reshape(-1, nch).copy()
                self._capture_release(nframes)
                if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                    arr[:] = 0.0
                if not chunks:
                    first_qpc = qpc
                    first_flags = flags
                chunks.append(arr)
                got += arr.shape[0]
                if want <= 0:
                    break                     # 有多少取多少：取一个包就走
            if not chunks:
                return None
            out = chunks[0] if len(chunks) == 1 else np.concatenate(chunks, axis=0)
            self.last_qpc = first_qpc
            self.last_frames = out.shape[0]
            self.last_flags = first_flags
            return out

        def stats(self):
            return {
                "disc": self.disc_count,
                "silent": self.silent_count,
                "ts_err": self.ts_err_count,
                "last_qpc": self.last_qpc,
            }

    return _TimedRecorder


_RECORDER_CLS = None


def _recorder_cls():
    global _RECORDER_CLS
    if _RECORDER_CLS is None:
        _RECORDER_CLS = _make_recorder_cls()
    return _RECORDER_CLS


class TimedDevice:
    """把 `soundcard` 的 mic 对象包一层，提供 `.name` / `.recorder()`。

    ⚠ 刻意**不**替换 `soundcard` 的设备枚举（枚举仍走 soundcard，见 audio_engine）。
      本类只接管「录」这一步 —— 改动面越小，现场越稳。
    """

    def __init__(self, mic):
        self._mic = mic
        self.name = mic.name
        self.isloopback = bool(getattr(mic, "isloopback", False))

    def recorder(self, samplerate, channels=None, blocksize=None):
        from soundcard import mediafoundation as mf
        cls = _recorder_cls()
        if channels is None:
            channels = self._mic.channels
        return cls(self._mic._audio_client(), samplerate, channels,
                   blocksize, self._mic.isloopback, False)

    def __getattr__(self, item):
        return getattr(self._mic, item)


def wrap(mic):
    """把 soundcard 的 mic 包成本模块的采集设备（接口兼容）。"""
    return TimedDevice(mic)


def self_test(mic=None, secs=6.0):
    """自测：验证零亏空、时间戳可用、不连续可观测。"""
    import soundcard as sc

    if mic is None:
        loop = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
        mic = None
        for m in loop:
            if "realtek" in m.name.lower():
                mic = m
                break
        mic = mic or loop[0]
    dev = wrap(mic)
    print("设备: %s" % dev.name)
    t0 = time.perf_counter()
    total = 0
    blocks = 0
    zeros = 0
    qpcs = []
    with dev.recorder(samplerate=48000, channels=2) as rec:
        while time.perf_counter() - t0 < secs:
            d = rec.record(numframes=960)
            if d is None:
                continue
            blocks += 1
            total += d.shape[0]
            x = d[:, 0]
            if float(np.sqrt(np.mean(x * x))) < _SILENT_EPS:
                zeros += 1
            if rec.last_qpc:
                qpcs.append(rec.last_qpc)
        st = rec.stats()
    dt = time.perf_counter() - t0
    print("时长 %.2fs 块 %d 帧 %d 速率比 %.4f 近零块 %d"
          % (dt, blocks, total, (total / dt) / 48000, zeros))
    print("不连续 %d 次  静音包 %d 次  时间戳错误 %d 次"
          % (st["disc"], st["silent"], st["ts_err"]))
    if len(qpcs) > 2:
        q = np.array(qpcs, dtype=np.float64)
        dq = np.diff(q) / 1e7            # 100ns → 秒
        print("QPC 增量 s: 均值 %.4f  P99 %.4f  max %.4f  非单调次数 %d"
              % (dq.mean(), np.percentile(dq, 99), dq.max(), int((dq <= 0).sum())))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test(secs=float(sys.argv[1]) if len(sys.argv) > 1 else 6.0))
