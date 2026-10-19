# -*- coding: utf-8 -*-
r"""进程级 loopback 诊断 v3 —— 先打印状态，再记录设备位置与标志位。

怀疑点：v2 用的 2,000,000 (200ms) 缓冲对 loopback 来说太大，或需要显式授权。
这里做三件事：
  1) 打印 GetBufferSize / GetCurrentPadding / 缓冲时长 → 确认缓冲是否正常
  2) 打印每包的 flags（AUDCLNT_BUFFERFLAGS_SILENT=0x2 表示"系统主动告诉我们这是静音"）
  3) 打印 device position 是否在推进 → 区分"驱动没给数据"和"给的是静音"
"""
import ctypes
import sys
import time
from ctypes import POINTER, byref, c_void_p, c_uint, c_int, c_ulong, c_float, c_ubyte

import comtypes
from comtypes import GUID, HRESULT, IUnknown, COMMETHOD, COMObject

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from _probe_process_loopback2 import (   # noqa: E402
    AUDIOCLIENT_ACTIVATION_PARAMS, AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
    AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, BLOB, Handler,
    IActivateAudioInterfaceAsyncOperation, IActivateAudioInterfaceCompletionHandler,
    IAudioCaptureClient, IAudioClient, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
    PROPVARIANT, VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, VT_BLOB, WAVEFORMATEX,
    WAVE_FORMAT_IEEE_FLOAT, fmt, mmdev,
)


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 18288
    print("目标 PID:", pid)
    try:
        comtypes.CoInitializeEx(0)
    except OSError:
        pass

    h = Handler()
    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.u.ProcessLoopbackParams.TargetProcessId = pid
    params.u.ProcessLoopbackParams.ProcessLoopbackMode = \
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.u.blob.cbSize = ctypes.sizeof(params)
    pv.u.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

    iid = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    op = c_void_p()
    hptr = ctypes.cast(h._com_pointers_[IActivateAudioInterfaceCompletionHandler._iid_], c_void_p)
    hr = mmdev.ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
                                           byref(iid), byref(pv), hptr, byref(op))
    print("ActivateAudioInterfaceAsync ->", fmt(hr))
    rc = ctypes.windll.kernel32.WaitForSingleObject(h.event, 5000)
    print("回调:", "已触发" if rc == 0 else "超时")
    operation = ctypes.cast(op, POINTER(IActivateAudioInterfaceAsyncOperation))
    res_hr, act = operation.GetActivateResult()
    print("GetActivateResult hr =", fmt(res_hr))
    client = act.QueryInterface(IAudioClient)

    # ---- 该虚拟设备支持的格式
    try:
        pmix = client.GetMixFormat()
        mf = pmix.contents
        print("GetMixFormat: tag=%d ch=%d rate=%d bits=%d" % (
            mf.wFormatTag, mf.nChannels, mf.nSamplesPerSec, mf.wBitsPerSample))
    except Exception as e:
        print("GetMixFormat 失败:", e)

    wfx = WAVEFORMATEX()
    wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
    wfx.nChannels = 2
    wfx.nSamplesPerSec = 48000
    wfx.wBitsPerSample = 32
    wfx.nBlockAlign = 8
    wfx.nAvgBytesPerSec = 384000
    # ★ 关键改动：缓冲改小到 200ms→20ms 级别，且 periodicity 也用 0
    hr = client.Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                           200000, 0, byref(wfx), None)
    print("Initialize(20ms buffer) ->", fmt(hr))

    bs, lat, pad = None, None, None
    try:
        bs = client.GetBufferSize()
        print("GetBufferSize =", bs)
    except Exception as e:
        print("GetBufferSize 失败:", e)
    try:
        lat = client.GetStreamLatency()
        print("GetStreamLatency =", lat)
    except Exception as e:
        print("GetStreamLatency 失败:", e)
    try:
        pad = client.GetCurrentPadding()
        print("GetCurrentPadding =", pad)
    except Exception as e:
        print("GetCurrentPadding 失败:", e)

    cap = client.GetService(IAudioCaptureClient._iid_)
    cap = cap.QueryInterface(IAudioCaptureClient)
    print("Start ->", fmt(client.Start()))

    print("\n采集 4 秒（打印每包 flags / 位置）…")
    FLAG_SILENT = 0x2
    FLAG_DATA_DISCONTINUITY = 0x1
    nfr = npkt = nsilent = 0
    peak = 0.0
    last_pos = None
    move = 0
    t0 = time.time()
    while time.time() - t0 < 4.0:
        n = cap.GetNextPacketSize()
        if n == 0:
            time.sleep(0.003)
            continue
        data, frames, flags, devpos, qpc = cap.GetBuffer()
        tag = []
        if flags & FLAG_DATA_DISCONTINUITY:
            tag.append("DISC")
        if flags & FLAG_SILENT:
            tag.append("SILENT")
            nsilent += 1
        if frames:
            buf = ctypes.cast(data, POINTER(c_float))
            lim = min(frames * 2, 200000)
            for i in range(lim):
                a = abs(buf[i])
                if a > peak:
                    peak = a
            nfr += frames
            npkt += 1
        if last_pos is not None and devpos != last_pos:
            move += 1
        if npkt <= 6:
            print("  pkt#%d frames=%d flags=0x%x %s devpos=%d" % (
                npkt, frames, flags, "/".join(tag), devpos))
        last_pos = devpos
        cap.ReleaseBuffer(frames)
    client.Stop()

    print("\n结果：包 %d / 帧 %d / 静音包 %d / 位置推进次数 %d / 峰值 %.6f" % (
        npkt, nfr, nsilent, move, peak))
    if npkt == 0 and nsilent == 0:
        print("→ 驱动完全没投递任何包：目标进程在当前会话里没有音频流。")
    elif peak < 1e-6:
        print("→ 有包但全静音。")
    else:
        print("→ ✅ 采到真实电平 %.5f" % peak)


if __name__ == "__main__":
    main()
