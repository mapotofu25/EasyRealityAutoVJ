# -*- coding: utf-8 -*-
"""端点 GUID -> 名称 映射 + AutoVJ 当前音源设置"""
import sys, os, json, winreg

buf = []
def w(s=""):
    buf.append(str(s))

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

STATE = {1: "在用", 2: "已禁用", 4: "未插入", 8: "不存在", 0x10000000: "?"}

def epmap(kind):
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s" % kind
    k = ok(winreg.HKEY_LOCAL_MACHINE, base)
    m = {}
    if not k:
        return m
    rows = []
    for g in subs(k):
        kk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g)
        d = dict((a, b) for a, b, t in vals(kk))
        st = d.get("DeviceState")
        nm = ""
        pk = ok(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\Properties")
        if pk:
            for a, b, t in vals(pk):
                if a == "{a45c254e-df1c-4efd-8020-67d146a850e0},2" and isinstance(b, str):
                    nm = b
        m["{%s}" % g] = nm
        m[g] = nm
        rows.append((st, nm, g))
    w("--- %s ---" % kind)
    for st, nm, g in sorted(rows, key=lambda r: (r[0] is None, r[0] or 0)):
        w("  [%s] %-38s %s" % (STATE.get(st, st), nm[:38], g))
    return m

w("=" * 74)
w("[A] 全部端点（含未插入/已禁用）")
w("=" * 74)
rm = epmap("Render")
cm = epmap("Capture")

w("")
w("=" * 74)
w("[B] 各 App 的默认端点解析（HKCU\\...\\Audio\\DefaultEndpoint）")
w("=" * 74)
k = ok(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Audio\DefaultEndpoint")
if k:
    allm = dict(rm); allm.update(cm)
    for sub in subs(k):
        kk = ok(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Audio\DefaultEndpoint\\" + sub)
        app = ""
        for a, b, t in vals(kk):
            if a == "" and isinstance(b, str):
                app = b
        w("· %s   (%s)" % (sub, os.path.basename(app) if app else "?"))
        for a, b, t in vals(kk):
            if a.startswith("0") and isinstance(b, str) and "SWD" in b:
                g = b.split("{", 2)[-1].split("}")[0]
                dev = allm.get("{%s}" % g, allm.get(g, "?"))
                w("     %s = %s   ->  %s" % (a, b.rsplit("#", 1)[-1], dev))
else:
    w("  无记录（= 所有 App 都用系统默认设备）")

w("")
w("=" * 74)
w("[C] AutoVJ 当前音源设置")
w("=" * 74)
p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
w("  配置文件：%s" % p)
if os.path.exists(p):
    try:
        cfg = json.load(open(p, encoding="utf-8"))
        for kk in ("audio", "output", "engine", "kv", "postfx", "color"):
            if kk in cfg:
                w("  --- %s ---" % kk)
                w("    " + json.dumps(cfg[kk], ensure_ascii=False)[:1200])
    except Exception as e:
        w("  读失败：%s" % e)
else:
    w("  不存在")

txt = "\n".join(buf)
open(r"P:\AutoVJ\_audiocheck4.txt", "w", encoding="utf-8").write(txt)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print(txt)
