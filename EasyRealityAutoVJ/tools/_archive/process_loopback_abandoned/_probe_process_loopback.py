# -*- coding: utf-8 -*-
r"""进程级 loopback（ProcessLoopback）可行性探针 —— 一次性验证脚本。

目的（决定「窗口采集」音源能不能做）：
  1. 本机 Windows 10 build 19045 到底支不支持 AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
  2. 能否真的按 PID 激活并采到数据
  3. （关键）把目标进程的系统音量调 0 / 静音后，还能不能采到 —— 即"绕过系统调音"

用法：venv\Scripts\python.exe -u tools\_probe_process_loopback.py [PID]
      不给 PID 时自动找一个正在出声的进程（枚举音频 session）。
"""
import ctypes
import ctypes.wintypes as w
import sys
import os
import time
import struct

from ctypes import POINTER, byref, c_void_p, c_uint, c_int, c_ulong, c_float, c_ubyte

# ----------------------------------------------------------------------------
# COM / WASAPI 常量与结构（照 MS 官方 ApplicationLoopback 样例）
# ----------------------------------------------------------------------------
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = -2147417850

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000

AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1

PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

VT_BLOB = 65
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

WAVEFORMATEX_FIELDS = [
    ("wFormatTag", c_uint),
    ("nChannels", c_uint),
    ("nSamplesPerSec", c_ulong),
    ("nAvgBytesPerSec", c_ulong),
    ("nBlockAlign", c_uint),
    ("wBitsPerSample", c_uint),
    ("cbSize", c_uint),
]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = WAVEFORMATEX_FIELDS


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", c_ulong),
        ("ProcessLoopbackMode", c_int),
    ]


class _UNION(ctypes.Union):
    _fields_ = [("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", c_int),
        ("u", _UNION),
    ]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class _PV_UNION(ctypes.Union):
    _fields_ = [("blob", BLOB), ("lVal", ctypes.c_long), ("uhVal", ctypes.c_ulonglong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
                ("u", _PV_UNION)]


HRESULT = ctypes.c_long

# ----------------------------------------------------------------------------
# GUID 辅助
# ----------------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", c_ubyte * 8)]

    def __init__(self, s=None):
        super().__init__()
        if s:
            self.from_string(s)

    def from_string(self, s):
        s = s.strip().strip("{}")
        p = s.split("-")
        self.Data1 = int(p[0], 16)
        self.Data2 = int(p[1], 16)
        self.Data3 = int(p[2], 16)
        rest = p[3] + p[4]
        for i in range(8):
            self.Data4[i] = int(rest[i * 2:i * 2 + 2], 16)

    def __str__(self):
        d4 = "".join("%02X" % b for b in self.Data4)
        return "{%08X-%04X-%04X-%s-%s}" % (
            self.Data1, self.Data2, self.Data3, d4[:4], d4[4:])


IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"
IID_IActivateAudioInterfaceCompletionHandler = "{41D949AB-9862-444A-80F6-C261334DA5EB}"
IID_IAgileObject = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"
IID_IUnknown = "{00000000-0000-0000-C000-000000000046}"

ole32 = ctypes.WinDLL("ole32")
mmdev = ctypes.WinDLL("Mmdevapi")

ole32.CoInitializeEx.restype = HRESULT
ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
ole32.CoUninitialize.argtypes = []
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.PropVariantClear.argtypes = [POINTER(PROPVARIANT)]

mmdev.ActivateAudioInterfaceAsync.restype = HRESULT
mmdev.ActivateAudioInterfaceAsync.argtypes = [
    w.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT), c_void_p,
    POINTER(c_void_p)]


# ----------------------------------------------------------------------------
# 最小 COM 手搓（只用 IUnknown 三件套 + vtable 偏移调用）
# ----------------------------------------------------------------------------
def _vtbl_call(ptr, index, restype, argtypes, *args):
    """调用 COM 接口指针的 vtable 第 index 个方法。"""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


class CompletionHandler:
    """实现 IActivateAudioInterfaceCompletionHandler + IAgileObject。

    ⚠ 已知坑：不实现 IAgileObject 会返回 E_ILLEGAL_METHOD_CALL 且没有提示。
    这里用 QueryInterface 手工应答这两个 IID（以及 IUnknown）。
    """

    def __init__(self):
        self.event = None
        self.operation = None
        self.result_hr = -1
        self._vtbl_h = None
        self._obj = None

    # --- IUnknown ---
    def QueryInterface(self, this, riid, ppv):
        s = str(riid.contents).upper()
        want = {IID_IActivateAudioInterfaceCompletionHandler.upper(),
                IID_IAgileObject.upper(),
                IID_IUnknown.upper()}
        if s in want:
            ppv[0] = this
            return 0
        ppv[0] = None
        return -2147467262  # E_NOINTERFACE

    def AddRef(self, this):
        return 2

    def Release(self, this):
        return 1

    # --- IActivateAudioInterfaceCompletionHandler ---
    def ActivateCompleted(self, this, activateOperation):
        self.operation = activateOperation
        if self.event is not None:
            ctypes.windll.kernel32.SetEvent(self.event)
        return 0

    def build(self):
        UNK = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
        UNK1 = ctypes.WINFUNCTYPE(c_ulong, c_void_p)
        DONE = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)
        self._vtbl_h = (c_void_p * 4)(
            ctypes.cast(UNK(self.QueryInterface), c_void_p),
            ctypes.cast(UNK1(self.AddRef), c_void_p),
            ctypes.cast(UNK1(self.Release), c_void_p),
            ctypes.cast(DONE(self.ActivateCompleted), c_void_p),
        )
        holder = ctypes.cast(ctypes.pointer(self._vtbl_h), c_void_p)
        self._obj = holder
        return holder


def hr_ok(hr):
    return hr >= 0


def hr_str(hr):
    return "%08X" % (hr & 0xFFFFFFFF)


def fmt_hr(hr):
    u = hr & 0xFFFFFFFF
    if u == 0:
        return "S_OK"
    return "0x%08X" % u


def enumerate_audio_pids():
    """枚举默认渲染设备上所有音频 session 的 PID（音量合成器那一列）。"""
    from comtypes import CoCreateInstance, CoInitialize
    CoInitialize()
    CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")

    # 用 vtable 手搓太繁琐，这里用 comtypes 的动态调用
    import comtypes.client
    import comtypes
    from comtypes import IUnknown, GUID as CGUID, COMMETHOD, HRESULT as CHR
    from ctypes import HRESULT as c_hr

    enum = comtypes.client.CreateObject(
        CGUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        interface=comtypes.client.GetModule.__self__ if False else None)
    return enum


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print("=" * 62)
    v = sys.getwindowsversion()
    print("Windows build:", v.build)
    print("目标 PID:", target or "(未指定)")
    print("=" * 62)

    hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    print("CoInitializeEx:", fmt_hr(hr))
    if hr == RPC_E_CHANGED_MODE:
        print("  (已是其它模式，可继续)")

    try:
        handler = CompletionHandler()
        handler.build()
        ev = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
        handler.event = ev

        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.u.ProcessLoopbackParams.TargetProcessId = target
        params.u.ProcessLoopbackParams.ProcessLoopbackMode = \
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE

        pv = PROPVARIANT()
        pv.vt = VT_BLOB
        pv.u.blob.cbSize = ctypes.sizeof(params)
        pv.u.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

        iid = GUID(IID_IAudioClient)
        op = c_void_p()
        t0 = time.time()
        hr = mmdev.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(iid), byref(pv), handler._obj, byref(op))
        print("ActivateAudioInterfaceAsync -> %s" % fmt_hr(hr))

        if not hr_ok(hr):
            print("\n结论：❌ 本机不支持进程级 loopback（激活调用即失败）")
            print("    错误码", fmt_hr(hr), "—— 这是决定性的负面结果。")
            return

        # 等待完成回调
        rc = ctypes.windll.kernel32.WaitForSingleObject(ev, 5000)
        print("等待完成回调: %s" % ("已触发" if rc == 0 else "超时(rc=%d)" % rc))
        if rc != 0:
            print("\n结论：⚠ 激活异步回调未在 5s 内返回 —— 可能不支持或行为异常。")
            return

        # 取结果
        hr_res = HRESULT()
        act = c_void_p()
        # IActivateAudioInterfaceAsyncOperation::GetActivateResult
        hr2 = _vtbl_call(handler.operation, 3, HRESULT,
                         [POINTER(HRESULT), POINTER(c_void_p)],
                         byref(hr_res), byref(act))
        print("GetActivateResult -> %s (内部 hr=%s)" % (fmt_hr(hr2), fmt_hr(hr_res.value)))

        if not hr_ok(hr_res.value):
            print("\n结论：❌ 激活失败，内部 hr=%s" % fmt_hr(hr_res.value))
            if (hr_res.value & 0xFFFFFFFF) == 0x80070057:
                print("     E_INVALIDARG —— 常见于 PID 无效或系统不支持该模式。")
            return

        print("\n✅ IAudioClient 激活成功！本机 build %d 支持进程级 loopback。" % v.build)

        # 试着 Initialize + 读一帧（真正证明能采到数据）
        # IAudioClient vtable: 0=QI 1=AddRef 2=Release 3=Initialize 4=GetBufferSize
        #   5=GetStreamLatency 6=GetCurrentPadding 7=IsFormatSupported 8=GetMixFormat
        #   9=GetDevicePeriod 10=Start 11=Stop 12=Reset 13=SetEventHandle 14=GetService
        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
        wfx.nChannels = 2
        wfx.nSamplesPerSec = 48000
        wfx.wBitsPerSample = 32
        wfx.nBlockAlign = wfx.nChannels * wfx.wBitsPerSample // 8
        wfx.nAvgBytesPerSec = wfx.nSamplesPerSec * wfx.nBlockAlign
        wfx.cbSize = 0

        hr = _vtbl_call(act, 3, HRESULT,
                        [c_int, c_ulong, ctypes.c_longlong, ctypes.c_longlong,
                         POINTER(WAVEFORMATEX), c_void_p],
                        AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK,
                        2000000, 0, byref(wfx), None)
        print("IAudioClient::Initialize -> %s" % fmt_hr(hr))
        if not hr_ok(hr):
            if (hr & 0xFFFFFFFF) == 0x88890008:   # AUDCLNT_E_UNSUPPORTED_FORMAT
                print("  格式不支持（会改用 GetMixFormat 协商，正式实现里要处理）")
            print("\n结论：⚠ 激活成功但 Initialize 失败 —— 需要格式协商。")
            return

        # GetService(IAudioCaptureClient)
        iid_cap = GUID(IID_IAudioCaptureClient)
        cap = c_void_p()
        hr = _vtbl_call(act, 14, HRESULT, [POINTER(GUID), POINTER(c_void_p)],
                        byref(iid_cap), byref(cap))
        print("GetService(IAudioCaptureClient) -> %s" % fmt_hr(hr))
        if not hr_ok(hr):
            return

        hr = _vtbl_call(act, 10, HRESULT, [])   # Start
        print("Start -> %s" % fmt_hr(hr))
        if not hr_ok(hr):
            return

        print("\n采集 3 秒，统计电平…")
        # IAudioCaptureClient: 0=QI 1=AddRef 2=Release 3=GetBuffer 4=ReleaseBuffer
        #   5=GetNextPacketSize
        peak = 0.0
        npkt = 0
        nframes_total = 0
        deadline = time.time() + 3.0
        while time.time() < deadline:
            nxt = c_uint()
            hr = _vtbl_call(cap, 5, HRESULT, [POINTER(c_uint)], byref(nxt))
            if not hr_ok(hr):
                break
            if nxt.value == 0:
                time.sleep(0.005)
                continue
            pdata = c_void_p()
            nframes = c_uint()
            flags = c_ulong()
            hr = _vtbl_call(cap, 3, HRESULT,
                            [POINTER(c_void_p), POINTER(c_uint), POINTER(c_ulong),
                             POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong)],
                            byref(pdata), byref(nframes), byref(flags),
                            None, None)
            if not hr_ok(hr):
                break
            n = nframes.value
            if n and pdata.value:
                buf = ctypes.cast(pdata, POINTER(c_float))
                cnt = n * 2
                s = 0.0
                for i in range(0, min(cnt, 20000)):
                    val = abs(buf[i])
                    if val > s:
                        s = val
                if s > peak:
                    peak = s
                nframes_total += n
                npkt += 1
            _vtbl_call(cap, 4, HRESULT, [c_uint], n)  # ReleaseBuffer

        _vtbl_call(act, 11, HRESULT, [])  # Stop
        print("  收到 packet 数: %d, 总帧数: %d" % (npkt, nframes_total))
        print("  峰值电平: %.5f" % peak)
        print()
        if nframes_total > 0:
            print("结论：✅✅ 能真正采到音频数据（%d 帧）。方案成立。" % nframes_total)
            if peak < 1e-6:
                print("   ⚠ 但这 3 秒内电平接近 0 —— 目标进程可能没在出声。")
                print("     要验证'绕过系统调音'，请在目标程序播放音乐时重跑本脚本。")
        else:
            print("结论：⚠ 激活/Initialize 都成功，但 3 秒内没拿到 packet。")

    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


if __name__ == "__main__":
    main()
