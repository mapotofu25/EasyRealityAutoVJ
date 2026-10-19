# -*- coding: utf-8 -*-
"""枚举所有音频会话，并在屏幕左上角打印会话对应的 PID —— 用于找出"正在出声的进程"。

背景：手工 vtable 取 IAudioSessionControl2::GetProcessId（槽位 14）一直崩。
改用 comtypes 定义接口来取 PID，这才是正路。
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ctypes
from ctypes import POINTER, byref, c_void_p, c_uint32, c_wchar_p, c_int
import comtypes
from comtypes import GUID, HRESULT, COMMETHOD, IUnknown

try:
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
except OSError:
    pass

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioSessionManager2 = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionControl2 = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")

EDATAFLOW_RENDER = 0
DEVICE_STATE_ACTIVE = 0x1


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], c_uint32, "dwClsCtx"),
                  (["in"], POINTER(c_void_p), "pActivationParams"),
                  (["out"], POINTER(c_void_p), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore"),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_uint32), "pdwState")),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = IID_IMMDeviceEnumerator
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints"),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], HRESULT, "GetDevice"),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback"),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback"),
    )


class IAudioSessionControl(IUnknown):
    _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD([], HRESULT, "SetDisplayName"),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD([], HRESULT, "SetIconPath"),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
        COMMETHOD([], HRESULT, "SetGroupingParam"),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification"),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification"),
    )


class IAudioSessionControl2(IAudioSessionControl):
    _iid_ = IID_IAudioSessionControl2
    _methods_ = (
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(c_uint32), "pRetVal")),
        COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
        COMMETHOD([], HRESULT, "SetDuckingPreference"),
    )


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
        COMMETHOD([], HRESULT, "GetSession",
                  (["in"], c_int, "SessionIndex"),
                  (["out"], POINTER(POINTER(IAudioSessionControl)), "Session")),
    )


class IAudioSessionManager2(IUnknown):
    _iid_ = IID_IAudioSessionManager2
    _methods_ = (
        COMMETHOD([], HRESULT, "GetAudioSessionControl"),
        COMMETHOD([], HRESULT, "GetSimpleAudioVolume"),
        COMMETHOD([], HRESULT, "GetSessionEnumerator",
                  (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum")),
        COMMETHOD([], HRESULT, "RegisterSessionNotification"),
        COMMETHOD([], HRESULT, "UnregisterSessionNotification"),
        COMMETHOD([], HRESULT, "RegisterDuckNotification"),
        COMMETHOD([], HRESULT, "UnregisterDuckNotification"),
    )


def main():
    comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator,
        clsctx=comtypes.CLSCTX_INPROC_SERVER)
    enum = comtypes.CoCreateInstance(
        CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator,
        clsctx=comtypes.CLSCTX_INPROC_SERVER)

    dev = enum.GetDefaultAudioEndpoint(EDATAFLOW_RENDER, 0)
    did = dev.GetId()
    print("默认渲染设备:", did)

    mgr = dev.Activate(IID_IAudioSessionManager2, comtypes.CLSCTX_ALL, None)
    semgr = comtypes.cast(mgr, POINTER(IAudioSessionManager2))
    senum = semgr.GetSessionEnumerator()
    n = senum.GetCount()
    print("会话数:", n)

    rows = []
    for i in range(n):
        ctrl = senum.GetSession(i)
        try:
            c2 = ctrl.QueryInterface(IAudioSessionControl2)
        except Exception as e:
            print("  [debug] QueryInterface(IAudioSessionControl2) 失败 @%d: %r" % (i, e))
            c2 = comtypes.cast(ctrl, POINTER(IAudioSessionControl2))
        try:
            pid = c2.GetProcessId()
        except Exception as e:
            pid = -1
            if i == 0:
                print("  [debug] GetProcessId 异常: %r" % (e,))
        try:
            ident = c2.GetSessionIdentifier() or ""
        except Exception:
            ident = ""
        try:
            disp = ctrl.GetDisplayName() or ""
        except Exception:
            disp = ""
        try:
            st = c2.GetState()
        except Exception:
            st = -1
        name = ""
        try:
            import ctypes.wintypes as wt
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                buf = ctypes.create_unicode_buffer(512)
                sz = ctypes.c_uint32(512)
                if ctypes.WinDLL("kernel32").QueryFullProcessImageNameW(
                        h, 0, buf, byref(sz)):
                    name = buf.value
                k32.CloseHandle(h)
        except Exception:
            pass
        rows.append((i, pid, st, name, disp, ident))

    print("\n%-4s %-8s %-6s %-12s %s" % ("idx", "pid", "state", "进程名", "会话标识"))
    for i, pid, st, name, disp, ident in rows:
        short = name.split("\\")[-1] if name else "?"
        print("%-4d %-8d %-6d %-12s %s | %s" % (i, pid, st, short, disp, ident[:60]))

    print("\nAudioSessionState: 0=Inactive 1=Active 2=Expired")
    print("状态=1 的会话就是「此刻正在出声」的进程。")


if __name__ == "__main__":
    main()
