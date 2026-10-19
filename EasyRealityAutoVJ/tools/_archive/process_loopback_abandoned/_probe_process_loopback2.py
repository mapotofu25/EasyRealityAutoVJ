# -*- coding: utf-8 -*-
r"""进程级 loopback（ProcessLoopback）可行性探针 v2 —— 用 comtypes 正确构造 COM 对象。

v1 用 ctypes 手搓 vtable 导致 access violation（COM 要求 vtable 指针在对象内存起始处，
且 QueryInterface 必须返回真正的接口指针）。这里改用 comtypes，它是成熟实现。

用法：venv\Scripts\python.exe -u tools\_probe_process_loopback2.py <PID>
"""
import ctypes
import ctypes.wintypes as w
import sys
import time

from ctypes import POINTER, byref, c_void_p, c_uint, c_int, c_ulong, c_float, c_ubyte

import comtypes
from comtypes import (
    GUID, HRESULT, IUnknown, COMMETHOD, CoInitializeEx, CoUninitialize,
    COMObject,
)
from comtypes.automation import VARIANT

COINIT_MULTITHREADED = 0x0

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

VT_BLOB = 65
WAVE_FORMAT_IEEE_FLOAT = 3

ole32 = ctypes.WinDLL("ole32")
mmdev = ctypes.WinDLL("Mmdevapi")


# ---------------------------------------------------------------- 结构定义
class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", c_uint), ("nChannels", c_uint),
                ("nSamplesPerSec", c_ulong), ("nAvgBytesPerSec", c_ulong),
                ("nBlockAlign", c_uint), ("wBitsPerSample", c_uint),
                ("cbSize", c_uint)]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", c_ulong), ("ProcessLoopbackMode", c_int)]


class _U(ctypes.Union):
    _fields_ = [("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", c_int), ("u", _U)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class _PU(ctypes.Union):
    _fields_ = [("blob", BLOB), ("lVal", ctypes.c_long),
                ("uhVal", ctypes.c_ulonglong), ("puuid", c_void_p),
                ("pUnk", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
                ("u", _PU)]


# ---------------------------------------------------------------- 接口声明
class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], c_void_p, "activateOperation")),
    ]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = []


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize",
                  (["in"], c_int, "ShareMode"),
                  (["in"], c_ulong, "StreamFlags"),
                  (["in"], ctypes.c_longlong, "hnsBufferDuration"),
                  (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], c_void_p, "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint), "p")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "p")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint), "p")),
        COMMETHOD([], HRESULT, "IsFormatSupported",
                  (["in"], c_int, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(ctypes.c_longlong), "a"),
                  (["out"], POINTER(ctypes.c_longlong), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], c_void_p, "h")),
        COMMETHOD([], HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                  (["out"], POINTER(c_uint), "pNumFramesToRead"),
                  (["out"], POINTER(c_ulong), "pdwFlags"),
                  (["out"], POINTER(ctypes.c_ulonglong), "pu64DevicePosition"),
                  (["out"], POINTER(ctypes.c_ulonglong), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint), "p")),
    ]


class Handler(COMObject):
    """同时实现 IActivateAudioInterfaceCompletionHandler + IAgileObject。

    ⚠ 关键坑：只实现前者会得到 E_ILLEGAL_METHOD_CALL，且系统不给任何提示。
    """
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)

    def ActivateCompleted(self, activateOperation):
        ctypes.windll.kernel32.SetEvent(self.event)
        return 0


def fmt(hr):
    u = hr & 0xFFFFFFFF
    if u == 0:
        return "S_OK"
    return "0x%08X" % u


mmdev.ActivateAudioInterfaceAsync.restype = HRESULT
mmdev.ActivateAudioInterfaceAsync.argtypes = [
    ctypes.c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT),
    c_void_p, POINTER(c_void_p)]


def main():
    if len(sys.argv) < 2:
        print("用法: _probe_process_loopback2.py <PID>")
        return 2
    pid = int(sys.argv[1])
    print("=" * 62)
    print("Windows build:", sys.getwindowsversion().build)
    print("目标 PID:", pid)
    print("=" * 62)

    import comtypes
    try:
        comtypes.CoInitializeEx(COINIT_MULTITHREADED)
    except OSError:
        # 0x80010106 RPC_E_CHANGED_MODE：线程已处于其它 apartment，可继续
        pass
    try:
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

        iid = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")  # IAudioClient
        op = c_void_p()
        # comtypes: COMObject._com_pointers_[iid] 就是该接口的 C 兼容指针
        hptr = ctypes.cast(
            h._com_pointers_[IActivateAudioInterfaceCompletionHandler._iid_],
            c_void_p)
        print("handler 接口指针: %s" % hex(hptr.value or 0))
        hr = mmdev.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(iid), byref(pv),
            hptr, byref(op))
        print("ActivateAudioInterfaceAsync -> %s" % fmt(hr))
        if hr < 0:
            print("\n结论：❌ 激活调用即失败 —— 本机不支持。")
            return 1

        rc = ctypes.windll.kernel32.WaitForSingleObject(h.event, 5000)
        print("等待回调:", "已触发" if rc == 0 else "超时(%d)" % rc)
        if rc != 0:
            print("\n结论：⚠ 回调超时。")
            return 1

        operation = ctypes.cast(op, POINTER(IActivateAudioInterfaceAsyncOperation))
        res_hr, act = operation.GetActivateResult()
        print("GetActivateResult -> 内部 hr=%s" % fmt(res_hr))
        if res_hr < 0:
            print("\n结论：❌ 激活失败 hr=%s" % fmt(res_hr))
            return 1

        print("\n✅ IAudioClient 激活成功 —— 本机支持进程级 loopback！")
        client = act.QueryInterface(IAudioClient)

        # 试几种格式
        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
        wfx.nChannels = 2
        wfx.nSamplesPerSec = 48000
        wfx.wBitsPerSample = 32
        wfx.nBlockAlign = 8
        wfx.nAvgBytesPerSec = 384000
        wfx.cbSize = 0
        hr = client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                               AUDCLNT_STREAMFLAGS_LOOPBACK,
                               2000000, 0, byref(wfx), None)
        print("Initialize(float32/48k/2ch) -> %s" % fmt(hr))
        if hr < 0:
            print("  ⚠ 该格式不被接受；正式实现改用 GetMixFormat 协商。")
            return 1

        cap = client.GetService(IAudioCaptureClient._iid_)
        cap = cap.QueryInterface(IAudioCaptureClient)
        print("GetService -> OK")

        hr = client.Start()
        print("Start -> %s" % fmt(hr))
        if hr < 0:
            return 1

        print("\n采集 3 秒…")
        peak = 0.0
        npkt = 0
        nfr = 0
        t_end = time.time() + 3.0
        while time.time() < t_end:
            n = cap.GetNextPacketSize()
            if n == 0:
                time.sleep(0.005)
                continue
            data, frames, flags, devpos, qpc = cap.GetBuffer()
            if frames:
                buf = ctypes.cast(data, POINTER(c_float))
                lim = min(frames * 2, 40000)
                for i in range(lim):
                    a = abs(buf[i])
                    if a > peak:
                        peak = a
                nfr += frames
                npkt += 1
            cap.ReleaseBuffer(frames)
        client.Stop()

        print("  packet 数: %d   总帧数: %d   峰值: %.5f" % (npkt, nfr, peak))
        print()
        if nfr > 0:
            print("结论：✅✅ 真正采到数据（%d 帧）。进程级采集可行。" % nfr)
            if peak < 1e-6:
                print("   ⚠ 3 秒内几乎静音 —— 目标进程没出声。")
        else:
            print("结论：⚠ 激活成功但没拿到 packet（目标进程无音频流）。")
        return 0
    finally:
        try:
            CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
