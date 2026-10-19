# -*- coding: utf-8 -*-
"""QA(严过关)：用**真实 Windows 平台(qwindows)** 启动精简后的 exe（临时副本），
确认它能起来，并查看它是否加载了 qtuiotouchplugin / Qt6Network（被跳过的 DLL）。
只杀“exe 路径在本临时副本目录下”的实例，绝不碰用户正在跑的实例。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time

DIST = open(os.path.join(tempfile.gettempdir(), "qa3_distcopy_path.txt")).read().strip()
EXE = os.path.join(DIST, "EasyRealityAutoVJ.exe")
iso = os.path.join(tempfile.gettempdir(), "qa3_iso3")
os.makedirs(os.path.join(iso, "AutoVJ"), exist_ok=True)
env = os.environ.copy()
env["LOCALAPPDATA"] = iso
env.pop("QT_QPA_PLATFORM", None)          # ★ 走真实 qwindows 平台

k32 = ctypes.windll.kernel32
k32.OpenProcess.restype = wt.HANDLE


def pid_path(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    buf = ctypes.create_unicode_buffer(2048)
    size = ctypes.c_ulong(2048)
    try:
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        k32.CloseHandle(h)
    return None


def list_instances():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EasyRealityAutoVJ.exe",
                          "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    pids = []
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("easyreality"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


print("DIST=%s" % DIST)
p = subprocess.Popen([EXE], cwd=DIST, env=env)
print("launched wrapper pid=%d" % p.pid)
time.sleep(12)

mine = [pid for pid in list_instances()
        if (pid_path(pid) or "").lower().startswith(DIST.lower())]
allp = list_instances()
print("所有 EasyRealityAutoVJ.exe 实例=%s" % allp)
print("属于本临时副本的实例=%s" % mine)

if not mine:
    print("!! 精简 exe 在真实平台上未能存活（未找到本副本实例）—— 可能无桌面会话或启动即退")
else:
    pid = mine[0]
    print("本副本 PID=%d path=%s" % (pid, pid_path(pid)))
    out = subprocess.run(["tasklist", "/M", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    mods = [m.strip().strip('"') for m in out.replace("\n", "").split(",") if m.strip()]
    interesting = [m for m in mods if any(k in m.lower() for k in
                   ("qtuiot", "qt6network", "qwindows", "qt6core", "qt6gui", "qt6widgets",
                    "qt6qml", "qt6svg", "qt6opengl", "qt6pdf"))]
    print("加载的相关 DLL：%s" % interesting)
    print("qtuiotouchplugin 被加载？ %s" % (any("qtuiot" in m.lower() for m in mods)))
    print("Qt6Network 被加载？ %s" % (any("qt6network" in m.lower() for m in mods)))
    print("qwindows 被加载？ %s" % (any("qwindows" in m.lower() for m in mods)))

# 只杀本副本实例
for pid in mine:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
time.sleep(1)
print("剩余实例=%s" % list_instances())
print("startup_error.log 存在？ %s" % os.path.exists(os.path.join(iso, "AutoVJ", "startup_error.log")))
