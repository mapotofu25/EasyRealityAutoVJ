# -*- coding: utf-8 -*-
"""验证：加了 CREATE_NO_WINDOW 之后，反复调 `lan_hosts()` 不会再闪控制台窗口。

做法：后台每 0.3s 调一次 `lan_hosts()`（共 20 次），同时前台枚举顶层窗口
数「新出现的 ConsoleWindowClass」。修复前应能抓到 arp.exe 控制台；修复后应为 0。

用法：python tools/_lan_nowindow_test.py
"""
import ctypes
import ctypes.wintypes as W
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"P:\ERAVJ\AutoVJ\src")

import lan_audio as LA  # noqa: E402

u = ctypes.windll.user32
u.SetProcessDPIAware()
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
u.IsWindowVisible.argtypes = [W.HWND]

console_seen = []


def snapshot_consoles():
    got = []

    def cb(h, _lp):
        try:
            if not u.IsWindowVisible(h):
                return True
            cbuf = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cbuf, 256)
            if cbuf.value == "ConsoleWindowClass":
                tbuf = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(h, tbuf, 512)
                got.append(tbuf.value)
        except Exception:
            pass
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return got


stop = False
calls = [0]


def worker():
    while not stop:
        try:
            LA.lan_hosts()
            calls[0] += 1
        except Exception as e:
            print("lan_hosts 异常:", e)
        time.sleep(0.25)


# 先建立基线（已有控制台不算）
base = set(snapshot_consoles())
print("基线控制台窗口：", base or "(无)")

th = threading.Thread(target=worker, daemon=True)
th.start()
t0 = time.perf_counter()
while time.perf_counter() - t0 < 6.0:
    for title in snapshot_consoles():
        if title not in base:
            console_seen.append((round(time.perf_counter() - t0, 2), title))
            base.add(title)
    time.sleep(0.03)
stop = True
th.join(1.0)

print("lan_hosts() 调用次数：", calls[0])
print("期间新出现的控制台窗口：", len(console_seen))
for t, title in console_seen[:10]:
    print("   +%.2fs  %r" % (t, title))

print("")
if console_seen:
    print("结论：❌ 仍在弹控制台窗口（修复无效）")
    sys.exit(1)
else:
    print("结论：✅ 无任何控制台窗口闪现（修复有效）")
    sys.exit(0)
