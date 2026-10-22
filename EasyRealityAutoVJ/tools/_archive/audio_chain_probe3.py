# -*- coding: utf-8 -*-
"""定位「低频增强」真凶：
1) 端点 FxProperties 里的 SFX/MFX/EFX CLSID -> 查实现 DLL（Dolby? Nahimic? Realtek?）
2) Dolby / Nahimic / Realtek 参数键全量转储
3) 当前默认播放/录音端点
"""
import sys, winreg

OUT = r"P:\AutoVJ\_audiocheck3.txt"
buf = []


def w(s=""):
    buf.append(str(s))


def ok(h, p, wow=winreg.KEY_WOW64_64KEY):
    try:
        return winreg.OpenKey(h, p, 0, winreg.KEY_READ | wow)
    except OSError:
        return None


def vals(k):
    r, i = [], 0
    while True:
        try:
            n, v, t = winreg.EnumValue(k, i); r.append((n, v, t)); i += 1
        except OSError:
            return r


def subs(k):
    r, i = [], 0
    while True:
        try:
            r.append(winreg.EnumKey(k, i)); i += 1
        except OSError:
            return r


def tree(h, p, depth=2, prefix="  ", wow=winreg.KEY_WOW64_64KEY):
    k = ok(h, p, wow)
    if not k:
        w("%s%s -> 无" % (prefix, p)); return
    w("%s%s" % (prefix, p))
    for n, v, t in vals(k):
        s = repr(v)
        w("%s    %s = %s" % (prefix, n, s[:150]))
    if depth <= 0:
        return
    for sk in subs(k):
        tree(h, p + "\\" + sk, depth - 1, prefix + "    ", wow)


# ---- 1. APO CLSID 归属 -------------------------------------------------------
FXGUID = [
    "{DA2C9ECE-7418-4906-B4FA-0A00B3EB88AA}",  # 该端点的 SFX
    "{A296D363-EE83-4af9-9BE7-729C1296150A}",  # MFX
    "{A29EB043-6CE2-4ee2-B38C-F58719E0D88F}",  # EFX
    "{5860E1C5-F95C-4a7a-8EC8-8AEF24F379A1}",  # 通用
    "{62dc1a93-ae24-464c-a43e-452f824c4250}",
    "{637c490d-eee3-4c0a-973f-371958802da2}",
    "{DFF21CE1-F70F-11D0-B917-00A0C9223196}",
    "{C18E2F7E-933D-4965-B7D1-1EEF228D2AF3}",  # d3993a3f,5/6 = 采样率转换?
]

w("=" * 74)
w("[1] 端点效果 APO 是谁实现的（CLSID -> DLL）")
w("=" * 74)
for g in FXGUID:
    hit = False
    for hive, base in [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\CLSID"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\WOW6432Node\CLSID")]:
        k = ok(hive, base + "\\" + g)
        if not k:
            continue
        hit = True
        n = ""
        try:
            n = winreg.QueryValueEx(k, "")[0]
        except OSError:
            pass
        dll = ""
        ik = ok(hive, base + "\\" + g + r"\InprocServer32")
        if ik:
            try:
                dll = winreg.QueryValueEx(ik, "")[0]
            except OSError:
                pass
        w("  %s  ->  %s" % (g, n))
        w("        DLL: %s" % dll)
    if not hit:
        w("  %s  ->  （CLSID 未注册）" % g)

# ---- 2. 默认端点 -------------------------------------------------------------
w("")
w("=" * 74)
w("[2] 当前默认端点")
w("=" * 74)
tree(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Sound Mapper", 1)
tree(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Audio\DefaultEndpoint", 2)
tree(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Audio\DeviceCpl", 2)

# ---- 3. 音效厂商参数 ---------------------------------------------------------
w("")
w("=" * 74)
w("[3] 音效厂商参数（Dolby / Nahimic / Realtek）")
w("=" * 74)
for p in [r"SOFTWARE\Dolby\DAX", r"SOFTWARE\Dolby\PCEE4", r"SOFTWARE\Dolby\Wrapper",
          r"SOFTWARE\Dolby\DolbyDigitalPlus",
          r"SOFTWARE\Nahimic", r"SOFTWARE\Realtek\Audio", r"SOFTWARE\Realtek\RealtekEffects",
          r"SOFTWARE\Realtek\RealtekOptions", r"SOFTWARE\Realtek\SpkProtection",
          r"SOFTWARE\WOW6432Node\Realtek\Audio"]:
    tree(winreg.HKEY_LOCAL_MACHINE, p, 3)
    w("")

txt = "\n".join(buf)
open(OUT, "w", encoding="utf-8").write(txt)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print(txt[:200])
print("[written]", OUT, len(txt), "chars")
