# -*- coding: utf-8 -*-
"""只报「当前有效（DeviceState=1）」端点的关键增强状态。"""
import sys, winreg, struct

OUT = r"P:\AutoVJ\_audiocheck2.txt"
buf = []

def w(s=""):
    buf.append(str(s))

PKEY = {
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},3": "★PhysicalSpeakers(声道掩码)",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5": "★Disable_SysFx(0=增强可用/1=禁用)",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},6": "FullRangeSpeakers",
    "{a45c254e-df1c-4efd-8020-67d146a850e0},2": "设备名",
    "{a45c254e-df1c-4efd-8020-67d146a850e0},14": "友好名",
    "{b3f8fa53-0004-438e-9003-51a46e139bfc},6": "接口名",
    "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0": "★DeviceFormat",
    "{e4870e26-3cc5-4cd2-ba46-ca0a9a70ed04},3": "★OEMFormat",
}

def ok(h, p):
    try:
        return winreg.OpenKey(h, p, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
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

def wfx(b):
    try:
        tag, ch, rate = struct.unpack_from("<HHI", b, 0)
        bits = struct.unpack_from("<H", b, 14)[0]
        return "%dHz %dbit %dch" % (rate, bits, ch)
    except Exception:
        return "?"

PHYS = {1: "单声道", 3: "立体声", 0x33: "四声道", 0x3F: "5.1", 0x63F: "7.1", 0x60F: "?"}

def report(kind, label):
    w("=" * 74)
    w("[%s端点] DeviceState=1（正在使用）" % label)
    w("=" * 74)
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s" % kind
    k = ok(winreg.HKEY_LOCAL_MACHINE, base)
    if not k:
        w("  读不到"); return
    n = 0
    for g in subs(k):
        kk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g)
        if not kk:
            continue
        d = dict((a, b) for a, b, t in vals(kk))
        if d.get("DeviceState") != 1:
            continue
        n += 1
        name, interf, phys, sysfx, fmt, oem = "?", "?", None, None, "?", "?"
        pk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\Properties")
        if pk:
            for a, b, t in vals(pk):
                if a == "{a45c254e-df1c-4efd-8020-67d146a850e0},2" and isinstance(b, str):
                    name = b
                if a == "{b3f8fa53-0004-438e-9003-51a46e139bfc},6" and isinstance(b, str):
                    interf = b
                if a == "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},3":
                    phys = b
                if a == "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5":
                    sysfx = b
                if a == "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0":
                    fmt = wfx(b)
                if a == "{e4870e26-3cc5-4cd2-ba46-ca0a9a70ed04},3":
                    oem = wfx(b)
        w("")
        w("● %s ── %s" % (name, interf))
        w("    Disable_SysFx = %s     物理声道 = %s %s" % (sysfx, phys, PHYS.get(phys, "")))
        w("    默认格式 = %s      驱动OEM格式 = %s" % (fmt, oem))
        fk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\FxProperties")
        if fk:
            items = vals(fk)
            w("    -- FxProperties（%d 项）--" % len(items))
            for a, b, t in items:
                if t == winreg.REG_BINARY and len(b) > 24:
                    w("      %-46s = <bin %dB>" % (PKEY.get(a, a), len(b)))
                else:
                    w("      %-46s = %r" % (PKEY.get(a, a), b))
        else:
            w("    （无 FxProperties）")
    w("")
    w("  >> 有效端点数：%d" % n)

report("Render", "播放")
report("Capture", "录制")
txt = "\n".join(buf)
open(OUT, "w", encoding="utf-8").write(txt)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print(txt)
