# -*- coding: utf-8 -*-
r"""★ 最终验收：启动**打好的新 exe**，观察还有没有 arp 控制台窗口闪。

这是"用户视角"的验收 —— 直接用 PyInstaller `--windowed` 的产物（它天然无控制台，
正是会触发闪烁的那种环境），所以**能真实复现**用户报的"弹窗在闪"。

判据：
  - 运行 N 秒；
  - 期间**任何父进程 = 该 exe 的 ConsoleWindowClass 窗口**都算失败；
  - 目标 exe 必须存活（否则谈不上验证）。

用法：python tools/_final_exe_nowin_test.py [exe路径] [秒数]
默认 exe：dist_new/EasyRealityAutoVJ/EasyRealityAutoVJ.exe
"""
import ctypes
import ctypes.wintypes as W
import os
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

u = ctypes.windll.user32
k = ctypes.windll.kernel32
u.SetProcessDPIAware()
u.IsWindowVisible.argtypes = [W.HWND]
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, W.HWND, W.LPARAM)
u.GetWindowThreadProcessId.restype = W.DWORD
u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]

TH32CS_SNAPPROCESS = 0x00000002


class PE32(ctypes.Structure):
    _fields_ = [
        ("dwSize", W.DWORD), ("cntUsage", W.DWORD),
        ("th32ProcessID", W.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
        ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", W.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


def ptab():
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    out = {}
    try:
        e = PE32(); e.dwSize = ctypes.sizeof(PE32)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile)
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def consoles():
    got = []

    def cb(h, _lp):
        try:
            if not u.IsWindowVisible(h):
                return True
            b = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, b, 256)
            if b.value == "ConsoleWindowClass":
                t = ctypes.create_unicode_buffer(512)
                u.GetWindowTextW(h, t, 512)
                pid = W.DWORD(0)
                u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                got.append((int(h), t.value, int(pid.value)))
        except Exception:
            pass
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return got


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "dist_new", "EasyRealityAutoVJ", "EasyRealityAutoVJ.exe")
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    if not os.path.isfile(exe):
        print("找不到 exe：%s" % exe)
        sys.exit(2)
    print("验收对象：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1e6))

    # 隔离 TEMP/LOCALAPPDATA，绕开单实例锁、不动用户数据
    tmp = tempfile.mkdtemp(prefix="autovj_final_")
    env = dict(os.environ)
    env["LOCALAPPDATA"] = tmp
    env["TEMP"] = tmp
    env["TMP"] = tmp

    print("启动（无隔离窗口，纯 exe）：等 %ds" % int(secs))
    p = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = p.pid
    print("exe pid =", pid)

    base = set(h for h, _t, _pp in consoles())
    found = []
    t0 = time.perf_counter()
    alive = True
    while time.perf_counter() - t0 < secs:
        if p.poll() is not None:
            alive = False
            print("!! exe 提前退出，返回码", p.returncode)
            break
        tab = ptab()
        for h, title, cpid in consoles():
            if h in base:
                continue
            base.add(h)
            par = tab.get(cpid, (0, "?"))[0]
            found.append((round(time.perf_counter() - t0, 2), title, cpid,
                          par, tab.get(par, (0, "?"))[1]))
        time.sleep(0.02)

    try:
        p.terminate(); p.wait(timeout=8)
    except Exception:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
        except Exception:
            pass
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print("")
    print("存活 = %s，采样 %.0fs，新出现控制台 %d 个"
          % (alive, secs, len(found)))
    mine = [f for f in found if f[3] == pid]
    for t, title, cpid, par, pex in found[:10]:
        tag = "  <== 来自本 exe" if par == pid else ""
        print("   +%.2fs pid=%-6d %-14s 父=%s(%d)%s"
              % (t, cpid, pex, pex, par, tag))
    print("")
    if mine:
        print("结论：❌ 新 exe 仍在弹控制台窗口（弹窗问题未修复）")
        sys.exit(1)
    if not alive:
        print("结论：⚠ exe 没存活满 %ds，无法判定（看是不是别的原因）" % int(secs))
        sys.exit(2)
    print("结论：✅ 新 exe 运行 %ds，未弹任何控制台窗口 ⇒ 弹窗问题已修复"
          % int(secs))
    sys.exit(0)


if __name__ == "__main__":
    main()
