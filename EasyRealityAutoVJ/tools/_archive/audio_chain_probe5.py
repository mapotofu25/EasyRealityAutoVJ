# -*- coding: utf-8 -*-
"""audiodg.exe 里实际加载了哪些 APO（= 哪些音效真正在跑）+ 修正端点取名"""
import sys, subprocess, winreg

buf = []
def w(s=""):
    buf.append(str(s))

w("=" * 74)
w("[1] audiodg.exe 已加载的音频处理模块（实际在跑的效果）")
w("=" * 74)
try:
    out = subprocess.run(["tasklist", "/fi", "imagename eq audiodg.exe", "/m", "/fo", "csv", "/nh"],
                         capture_output=True, timeout=60)
    txt = out.stdout.decode("gbk", "replace")
    for line in txt.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) < 3:
            w("  " + line[:160]); continue
        w("  进程: %s pid=%s" % (cells[0].strip('"'), cells[1]))
        mods = cells[-1].split('", "')
        key = ["RltkAPO", "Rtk", "Dolby", "DAX", "Nahimic", "Maxx", "Waves", "APO",
               "SysFx", "WMALFX", "Audio", "Bass", "vtbl", "vbaudio", "cable"]
        for m in mods:
            ml = m.lower()
            if any(k.lower() in ml for k in key):
                w("      ★ %s" % m)
except Exception as e:
    w("  失败：%s" % e)

w("")
w("=" * 74)
w("[2] 在用播放/录制端点：GUID -> 设备名（含厂商接口名）")
w("=" * 74)

def ok(h, p, wow=winreg.KEY_WOW64_64KEY):
    try:
        return winreg.OpenKey(h, p, 0, winreg.KEY_READ | wow)
    except OSError:
        return None

def subs(k):
    r, i = [], 0
    while True:
        try:
            r.append(winreg.EnumKey(k, i)); i += 1
        except OSError:
            return r

def vals(k):
    r, i = [], 0
    while True:
        try:
            n, v, t = winreg.EnumValue(k, i); r.append((n, v, t)); i += 1
        except OSError:
            return r

for kind, label in (("Render", "播放"), ("Capture", "录制")):
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s" % kind
    k = ok(winreg.HKEY_LOCAL_MACHINE, base)
    if not k:
        continue
    w("--- %s ---" % label)
    for g in sorted(subs(k)):
        kk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g)
        d = dict((a, b) for a, b, t in vals(kk))
        if d.get("DeviceState") != 1:
            continue
        desc = interf = ""
        pk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\Properties")
        if pk:
            for a, b, t in vals(pk):
                if a == "{a45c254e-df1c-4efd-8020-67d146a850e0},2" and isinstance(b, str):
                    desc = b
                if a == "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" and isinstance(b, str):
                    interf = b
        w("  %s" % g)
        w("      设备名=%-22s 接口=%s" % (desc, interf))

w("")
w("=" * 74)
w("[3] AutoVJ 音源那台端点的 GUID（用来核对是否就是它）")
w("=" * 74)
base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
k = ok(winreg.HKEY_LOCAL_MACHINE, base)
for g in subs(k):
    kk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g)
    d = dict((a, b) for a, b, t in vals(kk))
    if d.get("DeviceState") != 1:
        continue
    pk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\Properties")
    interf = ""
    if pk:
        for a, b, t in vals(pk):
            if a == "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" and isinstance(b, str):
                interf = b
    fk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\FxProperties")
    if fk:
        has = False
        for a, b, t in vals(fk):
            if a == "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5":
                w("  %-26s %s  Disable_SysFx=%s" % (interf[:26], g, b))
                has = True
        if not has:
            w("  %-26s %s  (FxProperties 无 Disable_SysFx 项)" % (interf[:26], g))

txt = "\n".join(buf)
open(r"P:\AutoVJ\_audiocheck5.txt", "w", encoding="utf-8").write(txt)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print(txt)
