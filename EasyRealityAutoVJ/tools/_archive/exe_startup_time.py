# -*- coding: utf-8 -*-
r"""测 exe「双击 → 主窗口出现」的真实耗时（冷/热各一次）。

为什么需要它：用户报「打不开了，但任务管理器里有进程」—— 其实是启动慢，
窗口过一会儿才出来。这里量出到底慢多少，以及冷启动(首次/被杀毒扫描后)与热启动差多少。

用法：venv\Scripts\python.exe tools\exe_startup_time.py
输出：P:\AutoVJ\_exe_startup.txt
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

EXE = r"P:\AutoVJ\dist\EasyRealityAutoVJ\EasyRealityAutoVJ.exe"
LOG = r"P:\AutoVJ\_exe_startup.txt"
TITLE_KEY = "Easy Reality AutoVJ"

u32 = ctypes.windll.user32
u32.SetProcessDPIAware()

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def find_main_window(pids, timeout):
    """等窗口出现；返回 (耗时, hwnd, pid) 或 (None, None, None)"""
    t0 = time.perf_counter()
    hwnd_hit = []

    def cb(hwnd, lparam):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(hwnd, buf, n + 2)
        if TITLE_KEY in buf.value and u32.IsWindowVisible(hwnd):
            hwnd_hit.append((hwnd, pid.value, buf.value))
            return False
        return True

    while time.perf_counter() - t0 < timeout:
        if hwnd_hit:
            break
        u32.EnumWindows(EnumWindowsProc(cb), 0)
        if not hwnd_hit:
            time.sleep(0.05)
    if hwnd_hit:
        return time.perf_counter() - t0, hwnd_hit[0][0], hwnd_hit[0][1], hwnd_hit[0][2]
    return None, None, None, None


def kill_all():
    subprocess.run(["taskkill", "/IM", "EasyRealityAutoVJ.exe", "/F"], capture_output=True)
    time.sleep(1.5)


def one_run(label, timeout=120):
    p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE),
                         creationflags=0x00000008 | 0x00000200,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = set()
    # 收集该 exe 及可能的子进程（PyInstaller onefile 会有父/子）
    pids.add(p.pid)
    try:
        r = subprocess.run(["tasklist", "/fi", "imagename eq EasyRealityAutoVJ.exe", "/fo", "csv", "/nh"],
                           capture_output=True)
        for line in r.stdout.decode("gbk", "replace").splitlines():
            parts = line.split('","')
            if len(parts) > 1:
                try:
                    pids.add(int(parts[1].strip('"')))
                except Exception:
                    pass
    except Exception:
        pass
    t, hwnd, pid, title = find_main_window(pids, timeout)
    if t is None:
        w("  %-14s 超过 %ds 仍未出现窗口" % (label, timeout))
    else:
        w("  %-14s 启动到窗口可见：%6.2fs   (pid=%s 标题=%r)" % (label, t, pid, title))
    kill_all()
    return t


def main():
    w("=" * 74)
    w("exe 启动耗时（双击 → 主窗口可见）")
    w("=" * 74)
    if not os.path.exists(EXE):
        w("exe 不存在: %s" % EXE)
        open(LOG, "w", encoding="utf-8").write("\n".join(out))
        return
    r1 = one_run("第 1 次(冷)")
    time.sleep(1.0)
    r2 = one_run("第 2 次(热)")
    w("")
    w("=" * 74)
    if r1 is not None and r2 is not None:
        w("冷 %.2fs / 热 %.2fs —— 差 %.2fs" % (r1, r2, r1 - r2))
        w("冷启动更慢通常是：首次读几百 MB 文件被系统缓存/杀毒扫描。")
    if r1 is None or r2 is None:
        w("有一次没等到窗口：要么真卡住，要么超过 120s（用户报的『打不开』就是这种）")
    open(LOG, "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
