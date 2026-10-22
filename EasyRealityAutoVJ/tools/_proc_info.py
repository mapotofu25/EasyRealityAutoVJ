# -*- coding: utf-8 -*-
"""快速查：正在运行的 EasyRealityAutoVJ 的 PID / 镜像路径 / 启动时刻 / 线程数。"""
import ctypes
import ctypes.wintypes as wt
import datetime
import subprocess
import sys

k32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
k32.OpenProcess.restype = wt.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                           ctypes.POINTER(wt.DWORD)]


def find():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EasyRealityAutoVJ.exe",
                          "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    res = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("easyreality"):
            res.append(int(parts[1]))
    return res


def info(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return
    buf = ctypes.create_unicode_buffer(1024)
    n = wt.DWORD(1024)
    k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    c = ctypes.c_ulonglong()
    e = ctypes.c_ulonglong()
    k = ctypes.c_ulonglong()
    u = ctypes.c_ulonglong()
    k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                        ctypes.byref(k), ctypes.byref(u))
    ft = c.value - 116444736000000000
    t = datetime.datetime.fromtimestamp(ft / 10000000.0)
    # 运行时长 & 累计 CPU
    import time as _t
    uptime = _t.time() - t.timestamp()
    cpu = (k.value + u.value) / 10000000.0
    print("PID %d" % pid)
    print("  exe   :", buf.value)
    print("  启动  :", t.strftime("%m-%d %H:%M:%S"),
          "（已运行 %.0f 秒）" % uptime)
    print("  累计CPU: %.1f 秒 ⇒ 平均 %.1f%%" % (cpu, cpu / max(1.0, uptime) * 100))
    k32.CloseHandle(h)


if __name__ == "__main__":
    ps = find()
    if not ps:
        print("没找到运行中的实例")
        sys.exit(1)
    for p in ps:
        info(p)
