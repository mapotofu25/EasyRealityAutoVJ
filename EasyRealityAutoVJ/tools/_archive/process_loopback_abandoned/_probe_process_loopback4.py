# -*- coding: utf-8 -*-
r"""进程级 loopback 诊断 v4 —— 每次循环重新取 CaptureClient（绕过一次性的 GetService 失效）。"""
import ctypes
import sys
import time
from ctypes import POINTER, byref, c_void_p, c_float

import comtypes
from comtypes import GUID

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from _probe_process_loopback2 import (   # noqa: E402
    AUDIOCLIENT_ACTIVATION_PARAMS, AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
    AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, Handler,
    IActivateAudioInterfaceAsyncOperation, IActivateAudioInterfaceCompletionHandler,
    IAudioCaptureClient, IAudioClient, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
    PROPVARIANT, VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, VT_BLOB, WAVEFORMATEX,
    WAVE_FORMAT_IEEE_FLOAT, fmt, mmdev,
)

pid = int(sys.argv[1]) if len(sys.argv) > 1 else 18288
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
print("目标 PID: %d  时长 %.1fs" % (pid, dur))

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
ctypes.windll.kernel32.WaitForSingleObject(h.event, 5000)
res_hr, act = ctypes.cast(op, POINTER(IActivateAudioInterfaceAsyncOperation)).GetActivateResult()
print("激活 hr =", fmt(res_hr))
client = act.QueryInterface(IAudioClient)

wfx = WAVEFORMATEX()
wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
wfx.nChannels = 2
wfx.nSamplesPerSec = 48000
wfx.wBitsPerSample = 32
wfx.nBlockAlign = 8
wfx.nAvgBytesPerSec = 384000
print("Initialize ->", fmt(client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                                             AUDCLNT_STREAMFLAGS_LOOPBACK,
                                             200000, 0, byref(wfx), None)))
print("Start ->", fmt(client.Start()))
print("StreamFlags 试加 EVENTCALLBACK:",
      fmt(client.Stop()) if False else "skip")

peak = 0.0
nfr = npkt = 0
nodata = 0
t0 = time.time()
k = 0
while time.time() - t0 < dur:
    k += 1
    try:
        cap = client.GetService(IAudioCaptureClient._iid_).QueryInterface(IAudioCaptureClient)
    except Exception as e:
        nodata += 1
        time.sleep(0.05)
        continue
    got = False
    for _ in range(50):
        n = cap.GetNextPacketSize()
        if n:
            got = True
            break
        time.sleep(0.002)
    if not got:
        nodata += 1
        if nodata <= 3:
            print("  [%d] 本轮无包（已等待 100ms）" % k)
        continue
    while True:
        n = cap.GetNextPacketSize()
        if n == 0:
            break
        data, frames, flags, devpos, qpc = cap.GetBuffer()
        if frames:
            buf = ctypes.cast(data, POINTER(c_float))
            lim = min(frames * 2, 200000)
            for i in range(lim):
                a = abs(buf[i])
                if a > peak:
                    peak = a
            nfr += frames
            npkt += 1
        cap.ReleaseBuffer(frames)
    if npkt <= 5 and npkt > 0:
        print("  [%d] pkt frames=%d flags=0x%x devpos=%d" % (k, frames, flags, devpos))

client.Stop()
print("\n结果：包 %d / 帧 %d / 循环次数 %d / 无数据轮次 %d / 峰值 %.6f" % (
    npkt, nfr, k, nodata, peak))
print("结论:", "✅ 采到真实电平" if peak > 1e-4 else "⚠ 全程静音/无包")
