# -*- coding: utf-8 -*-
"""音频链路体检（Easy Reality AutoVJ 排障用）

目的：找出「电脑在偷偷增强低频」的来源。逐段检查 Windows 音频链路：
  1) 播放端点（扬声器）的增强开关 Disable_SysFx / FxProperties
  2) 采集端点（立体声混音 / 线路输入 / 麦克风）的增强开关  <-- 软件的实时能量走这里
  3) 端点物理配置（5.1 会有独立的低音通道与低音管理）
  4) 空间音效（Windows Sonic / Dolby Atmos for Headphones / DTS 都会动低频）
  5) 可疑音效软件（Realtek/Dolby/Nahimic/Waves/MaxxAudio/EqualizerAPO/Voicemeeter/Boom...）
  6) 相关服务、驱动、启动项、进程
用法：python tools/audio_chain_probe.py [输出文件]
"""
import io
import os
import sys
import winreg

OUT = sys.argv[1] if len(sys.argv) > 1 else r"P:\AutoVJ\_audiocheck.txt"
_buf = []


def w(s=""):
    _buf.append(str(s))


def dump(s=""):
    _buf.append(str(s))


# ---- 已知 PKEY 名称，便于人肉辨认 -------------------------------------------
PKEY = {
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},0": "FormFactor(形态)",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},1": "ControlPanelPageProvider",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},2": "Association",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},3": "PhysicalSpeakers(物理声道掩码)",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},4": "EndpointGUID",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5": "★Disable_SysFx(0=允许系统增强 1=禁用)",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},6": "FullRangeSpeakers(全频段扬声器掩码)",
    "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},7": "Supports_EventDriven_Mode",
    "{a45c254e-df1c-4efd-8020-67d146a850e0},2": "DeviceDesc",
    "{a45c254e-df1c-4efd-8020-67d146a850e0},14": "FriendlyName",
    "{b3f8fa53-0004-438e-9003-51a46e139bfc},6": "DeviceInterface_FriendlyName",
    "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0": "AudioEngine_DeviceFormat",
    "{e4870e26-3cc5-4cd2-ba46-ca0a9a70ed04},3": "AudioEngine_OEMFormat",
    "{e4870e26-3cc5-4cd2-ba46-ca0a9a70ed04},2": "AudioEngine_?",
    "{9855c4cd-d55f-4c1e-b3d5-b7ba5ea84a5a},0": "?",
    "{2b5b1a37-0a2f-4b1e-b1a6-0e0a0f8f0a4a},0": "?",
}

KEYWORDS = [
    "realtek", "dolby", "nahimic", "waves", "maxxaudio", "maxx", "dts",
    "audio", "sound", "sonic", "equalizer", "apo", "voicemeeter", "boom",
    "soundblaster", "creative", "asus", "msi", "steelseries", "razer",
    "sennheiser", "logi", "corsair", "audioengine", "srs", "thx", "bongiovi",
    "subwoofer", "bass", "音效", "声卡", "均衡",
]


def is_audio_name(s):
    low = str(s).lower()
    return any(k in low for k in KEYWORDS)


def open_key(hive, path, wow=winreg.KEY_WOW64_64KEY):
    try:
        return winreg.OpenKey(hive, path, 0, winreg.KEY_READ | wow)
    except OSError:
        return None


def subkeys(key):
    out = []
    i = 0
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
        except OSError:
            break
        i += 1
    return out


def values(key):
    out = []
    i = 0
    while True:
        try:
            n, v, t = winreg.EnumValue(key, i)
        except OSError:
            break
        out.append((n, v, t))
        i += 1
    return out


def guid_str(raw):
    return str(raw).strip("{}") if raw else ""


def format_wfx(blob):
    """把 WAVEFORMATEX 二进制解成 采样率/位深/声道"""
    try:
        if not blob or len(blob) < 16:
            return "?"
        import struct
        tag, ch, rate = struct.unpack_from("<HHI", blob, 0)
        bits = struct.unpack_from("<H", blob, 14)[0]
        return "%dHz %dbit %dch%s" % (rate, bits, ch, "" if tag != 0xFFFE else " (extensible)")
    except Exception as e:  # noqa
        return "? (%s)" % e


# ---- 1. 端点 -----------------------------------------------------------------
def dump_endpoints(root_kind, label):
    w("=" * 78)
    w("[%s] 端点清单  (HKLM\\...\\MMDevices\\Audio\\%s)" % (label, root_kind))
    w("=" * 78)
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s" % root_kind
    k = open_key(winreg.HKEY_LOCAL_MACHINE, base)
    if not k:
        w("  读不到（权限？）")
        return
    for g in subkeys(k):
        ep = open_key(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g)
        if not ep:
            continue
        state = None
        try:
            state = winreg.QueryValueEx(ep, "")[0]
        except OSError:
            pass
        props = propmap = {}
        pk = open_key(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\Properties")
        nice = []
        if pk:
            for n, v, t in values(pk):
                if isinstance(v, str) and v.strip():
                    nice.append("%s=%s" % (PKEY.get(n, n), v))
                if n in ("{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},3",
                         "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},6",
                         "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5"):
                    nice.append("%s=%s" % (PKEY.get(n, n), v))
                if n in ("{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0",
                         "{e4870e26-3cc5-4cd2-ba46-ca0a9a70ed04},3"):
                    nice.append("%s=%s" % (PKEY.get(n, n), format_wfx(v)))
        fk = open_key(winreg.HKEY_LOCAL_MACHINE, base + "\\" + g + r"\FxProperties")
        fx = []
        if fk:
            for n, v, t in values(fk):
                fx.append("%s = %r" % (PKEY.get(n, n), v))
        w("")
        w("· %s   [state=%s]" % (guid_str(g), state))
        for line in nice:
            w("    " + line)
        w("    -- FxProperties(%d 项) --" % len(fx))
        for line in fx:
            w("      " + line)


# ---- 2. 音效软件 / 驱动 ------------------------------------------------------
def dump_installed():
    w("")
    w("=" * 78)
    w("[安装记录] 可疑音效软件")
    w("=" * 78)
    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]
    seen = set()
    hits = 0
    for hive, p, wow in paths:
        k = open_key(hive, p, wow) if wow else open_key(hive, p, 0)
        if not k:
            continue
        for sk in subkeys(k):
            kk = open_key(hive, p + "\\" + sk, wow) if wow else open_key(hive, p + "\\" + sk, 0)
            if not kk:
                continue
            d = {}
            for n, v, t in values(kk):
                d[n] = v
            name = str(d.get("DisplayName", "") or "")
            if not name or name in seen:
                continue
            if is_audio_name(name) or is_audio_name(d.get("Publisher", "")) \
                    or is_audio_name(d.get("InstallLocation", "")):
                seen.add(name)
                hits += 1
                w("  %-46s | %s | %s" % (name[:46], str(d.get("DisplayVersion", ""))[:14],
                                         str(d.get("Publisher", ""))[:26]))
    if not hits:
        w("  （无匹配）")


def dump_services():
    w("")
    w("=" * 78)
    w("[服务] 音频相关（正在运行的可疑项）")
    w("=" * 78)
    base = r"SYSTEM\CurrentControlSet\Services"
    k = open_key(winreg.HKEY_LOCAL_MACHINE, base)
    if not k:
        w("  读不到")
        return
    hits = 0
    for sk in subkeys(k):
        if not is_audio_name(sk):
            continue
        kk = open_key(winreg.HKEY_LOCAL_MACHINE, base + "\\" + sk)
        if not kk:
            continue
        d = dict((n, v) for n, v, t in values(kk))
        start = d.get("Start", -1)
        state = {0: "boot", 1: "system", 2: "自动", 3: "手动", 4: "禁用"}.get(start, start)
        img = str(d.get("ImagePath", ""))
        if start == 4:
            continue
        hits += 1
        w("  %-34s [%s] %s" % (sk[:34], state, img[:70]))
    if not hits:
        w("  （无匹配）")


def dump_startup_and_procs():
    w("")
    w("=" * 78)
    w("[启动项] 可疑音效常驻")
    w("=" * 78)
    runs = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ]
    hits = 0
    for hive, p in runs:
        k = open_key(hive, p)
        if not k:
            continue
        for n, v, t in values(k):
            if is_audio_name(n) or is_audio_name(v):
                hits += 1
                w("  %-26s -> %s" % (n[:26], str(v)[:80]))
    if not hits:
        w("  （无匹配）")

    w("")
    w("=" * 78)
    w("[进程] 正在运行的可疑音效进程")
    w("=" * 78)
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             timeout=30).stdout.decode("gbk", "replace")
        hits = 0
        for line in out.splitlines():
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) < 2:
                continue
            nm = cells[0]
            nm2 = nm[0] if nm and nm[0] == '"' else nm
            nm2 = nm2.strip('"')
            if is_audio_name(nm2):
                hits += 1
                w("  %-40s pid=%s mem=%s" % (nm2[:40], cells[1], cells[-1]))
        if not hits:
            w("  （无匹配）")
    except Exception as e:  # noqa
        w("  tasklist 失败：%s" % e)


def dump_misc():
    w("")
    w("=" * 78)
    w("[其它] 空间音效 / 全局音频策略 / EqualizerAPO")
    w("=" * 78)
    checks = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Audio"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Audio"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Multimedia\Audio\SpatialSound"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Dolby"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Dolby"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Waves"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Nahimic"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Realtek"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Realtek"),
    ]
    for hive, p in checks:
        k = open_key(hive, p)
        if not k:
            w("  %s  -> 无" % p)
            continue
        w("  %s" % p)
        for n, v, t in values(k):
            s = repr(v)
            w("      %s = %s" % (n, s[:110]))
        for sk in subkeys(k)[:12]:
            w("      [子键] %s" % sk)
    for cfg in [r"C:\Program Files\EqualizerAPO\config\config.txt",
                r"C:\Program Files (x86)\EqualizerAPO\config\config.txt",
                r"C:\Program Files\EqualizerAPO\config\config\config.txt"]:
        if os.path.exists(cfg):
            w("  ★ 检测到 EqualizerAPO 配置：%s" % cfg)
            try:
                txt = open(cfg, "r", encoding="utf-8", errors="replace").read()
                for line in txt.splitlines():
                    if line.strip() and not line.strip().startswith("#"):
                        w("      | " + line.strip()[:100])
            except Exception as e:  # noqa
                w("      读取失败 %s" % e)


def main():
    w("音频链路体检  %s" % os.environ.get("COMPUTERNAME", ""))
    w("主机名/用户：%s / %s" % (os.environ.get("COMPUTERNAME", "?"), os.environ.get("USERNAME", "?")))
    dump_endpoints("Render", "播放")
    dump_endpoints("Capture", "录制")
    dump_installed()
    dump_services()
    dump_startup_and_procs()
    dump_misc()
    w("")
    w("体检结束。★ 关注：Disable_SysFx=0 的端点、PhysicalSpeakers、FxProperties 里的效果项。")
    text = "\n".join(_buf)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(text)
    print("\n[written] %s" % OUT)


if __name__ == "__main__":
    main()
