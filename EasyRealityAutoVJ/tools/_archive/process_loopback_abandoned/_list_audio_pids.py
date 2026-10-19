# -*- coding: utf-8 -*-
r"""枚举默认渲染设备上所有音频 session 的 PID（含进程名与状态）。

所有 COM 调用都用 raw vtable，规避 comtypes 自动 QI 的问题。
"""
import ctypes
import sys
from ctypes import POINTER, c_void_p, c_int, c_ulong

from comtypes import GUID, HRESULT, IUnknown, COMMETHOD
from comtypes.client import CreateObject
import comtypes


class IMMDevice(IUnknown):
    _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
    _methods_ = [COMMETHOD([], HRESULT, 'Activate',
                           (['in'], POINTER(GUID), 'iid'),
                           (['in'], c_ulong, 'cls'),
                           (['in'], c_void_p, 'p'),
                           (['out'], POINTER(POINTER(IUnknown)), 'pp'))]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'EnumAudioEndpoints',
                  (['in'], c_int, 'd'), (['in'], c_ulong, 'm'),
                  (['out'], POINTER(c_void_p), 'p')),
        COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint',
                  (['in'], c_int, 'd'), (['in'], c_int, 'r'),
                  (['out'], POINTER(POINTER(IMMDevice)), 'p'))]


IID_SESSION_MANAGER2 = GUID('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')

CFUN = ctypes.WINFUNCTYPE


def _ptr(obj):
    """把 comtypes 对象取成裸接口指针。"""
    if isinstance(obj, int):
        return obj
    try:
        return ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p)).contents.value or 0
    except Exception:
        return 0


def _vt(p):
    """取 vtable 数组：p 是接口指针，*(void***)p 才是 vtable 起始。"""
    vt = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    # 上面 gives POINTER(c_void_p) 指向 vtable 首元素，取 .contents 继续
    return ctypes.cast(vt, ctypes.POINTER(ctypes.c_void_p))


def pname(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return '?'
    b = ctypes.create_unicode_buffer(32768)
    s = c_ulong(32768)
    k32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(s))
    k32.CloseHandle(h)
    return b.value or '?'


def main():
    comtypes.CoInitialize()
    en = CreateObject(GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}'),
                      interface=IMMDeviceEnumerator)
    dev = en.GetDefaultAudioEndpoint(0, 0)
    # 走 comtypes 正常路径拿 IAudioSessionManager2（已证明可用）

    class _SM2(IUnknown):
        _iid_ = GUID('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')
        _methods_ = [
            COMMETHOD([], HRESULT, 'GetAudioSessionControl',
                      (['in'], c_void_p, 'g'), (['in'], c_ulong, 'f'),
                      (['out'], POINTER(c_void_p), 'p')),
            COMMETHOD([], HRESULT, 'GetSimpleAudioVolume',
                      (['in'], c_void_p, 'g'), (['in'], c_ulong, 'f'),
                      (['out'], POINTER(c_void_p), 'p')),
            COMMETHOD([], HRESULT, 'GetSessionEnumerator',
                      (['out'], POINTER(c_void_p), 'p')),
        ]

    sm = dev.Activate(_SM2._iid_, 0x17, None).QueryInterface(_SM2)
    out = sm.GetSessionEnumerator()
    sess_p = _ptr(out)

    svt2 = _vt(sess_p)
    # IAudioSessionEnumerator: QI,AddRef,Release,GetCount,GetSession
    cnt = c_int()
    CFUN(ctypes.c_long, c_void_p, POINTER(c_int))(svt2[3])(
        sess_p, ctypes.byref(cnt))
    print('session 总数:', cnt.value)

    for i in range(cnt.value):
        item = c_void_p()
        hr = CFUN(ctypes.c_long, c_void_p, c_int, POINTER(c_void_p))(svt2[4])(
            sess_p, i, ctypes.byref(item))
        if hr < 0 or not item.value:
            continue
        ip = item.value
        vt = _vt(ip)
        # IAudioSessionControl: QI,AddRef,Release,GetState(3)
        st = c_int()
        CFUN(ctypes.c_long, c_void_p, POINTER(c_int))(vt[3])(ip, ctypes.byref(st))
        # IAudioSessionControl 占槽 0..11（IUnknown 3 + 自身 9）
        # IAudioSessionControl2: 12=GetSessionIdentifier,
        #   13=GetSessionInstanceIdentifier, 14=GetProcessId
        pid = c_ulong()
        CFUN(ctypes.c_long, c_void_p, POINTER(c_ulong))(vt[14])(
            ip, ctypes.byref(pid))
        state = {0: 'Inactive', 1: 'Active', 2: 'Expired'}.get(st.value, '?')
        nm = pname(pid.value)
        mark = '   <== 正在出声' if st.value == 1 else ''
        print('  pid=%-7d state=%-8s %s%s' % (pid.value, state, nm, mark))


if __name__ == '__main__':
    sys.exit(main())
