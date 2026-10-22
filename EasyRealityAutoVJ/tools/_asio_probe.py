# -*- coding: utf-8 -*-
"""ASIO 可行性探针（只读，不改任何东西）。

目的：在动手写 ASIO 采集之前，先搞清楚本机有什么牌可打：
  1. 系统里到底注册了哪些 ASIO 驱动（注册表 HKLM\\SOFTWARE\\ASIO，reg.exe 在沙箱黑名单里，故用 winreg）
  2. 每个驱动的 DLL 是否存在、是 32 位还是 64 位（位数不对就白搭）
  3. 现成可用的低延迟采集库在不在（sounddevice / pyaudiowpatch / PortAudio 是否带 ASIO 支持）
  4. 官方的 ASIO SDK 头文件有没有现成的（asio.h / iasiodrv.h）

用法：venv\\Scripts\\python.exe -u tools\\_asio_probe.py
"""

import os
import struct
import sys

try:
    import winreg
except ImportError:  # 非 Windows
    print("非 Windows 环境，退出")
    sys.exit(0)


def _pe_machine(path):
    """读 PE 头判断位数，返回 'x64' / 'x86' / 'unknown'。"""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return "unknown"
            f.seek(0x3C)
            off = struct.unpack("<I", f.read(4))[0]
            f.seek(off)
            if f.read(4) != b"PE\0\0":
                return "unknown"
            m = struct.unpack("<H", f.read(2))[0]
        return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(m, f"0x{m:04X}")
    except Exception as e:
        return f"读取失败({e})"


def _val(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def scan_asio_registry():
    print("=" * 78)
    print("① 注册表里注册的 ASIO 驱动  HKLM\\SOFTWARE\\ASIO")
    print("=" * 78)
    found = []
    for root_flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        bits = "64 位视图" if root_flag == winreg.KEY_WOW64_64KEY else "32 位视图"
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ASIO",
                               0, winreg.KEY_READ | root_flag)
        except OSError:
            continue
        n = winreg.QueryInfoKey(k)[0]
        print(f"\n[{bits}] 共 {n} 个：")
        for i in range(n):
            try:
                nm = winreg.EnumKey(k, i)
            except OSError:
                break
            try:
                sub = winreg.OpenKey(k, nm)
            except OSError:
                continue
            desc = _val(sub, "Description") or _val(sub, "") or ""
            dll = _val(sub, "CLSID") or ""
            # CLSID 在 InprocServer32 里指向 DLL
            real = ""
            try:
                c = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                   rf"CLSID\{dll}\InprocServer32")
                real = _val(c, "") or ""
            except OSError:
                pass
            if not real:
                real = dll
            exists = os.path.isfile(real) if real else False
            arch = _pe_machine(real) if exists else "-"
            print(f"  · {nm}")
            print(f"      Description = {desc}")
            print(f"      DLL         = {real}")
            print(f"      存在={exists}  位数={arch}")
            found.append((nm, real, exists, arch, bits))
            winreg.CloseKey(sub)
        winreg.CloseKey(k)
    if not found:
        print("  ⚠ 一个 ASIO 驱动都没注册 ⇒ 本机现在没有可用的 ASIO 设备")
    return found


def scan_libs():
    print()
    print("=" * 78)
    print("② 现成的低延迟采集库")
    print("=" * 78)
    for mod, note in (("sounddevice", "PortAudio 封装（要 ASIO 必须用带 PA_USE_ASIO 的 PortAudio）"),
                      ("pyaudiowpatch", "WASAPI 独占/环回，非 ASIO"),
                      ("pyaudio", "PortAudio 封装（同上）"),
                      ("soundcard", "本项目现用；不支持 ASIO")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            print(f"  ✓ {mod:16s} 已安装 版本 {ver}   —— {note}")
        except Exception:
            print(f"  ✗ {mod:16s} 未安装                —— {note}")

    # sounddevice 若存在，问它 PortAudio 有没有编译进 ASIO
    try:
        import sounddevice as sd
        print(f"\n  PortAudio 版本：{sd.get_portaudio_version()[1]}")
        apis = {i["name"] for i in sd.query_hostapis()}
        print(f"  可用 host API：{sorted(apis)}")
        print(f"  → 含 ASIO？{'是 ✓' if 'ASIO' in apis else '否 ✗（PortAudio 没编 ASIO，或本机无驱动）'}")
    except Exception as e:
        print(f"\n  （无法询问 PortAudio：{e}）")

    # PortAudio DLL 里有没有 ASIO 符号
    try:
        import sounddevice as _sd
        pa = os.path.join(os.path.dirname(_sd.__file__), "_sounddevice_data",
                          "portaudio-binaries")
        if os.path.isdir(pa):
            for f in os.listdir(pa):
                print(f"  PortAudio DLL：{f}")
    except Exception:
        pass


def scan_sdk():
    print()
    print("=" * 78)
    print("③ 官方 ASIO SDK 头文件（写原生宿主才需要）")
    print("=" * 78)
    hits = []
    for base in (r"P:\AutoVJ", r"C:\Program Files", r"C:\Program Files (x86)",
                 os.path.expanduser("~/Downloads"), r"P:\\"):
        if not os.path.isdir(base):
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(base):
                if dirpath.count(os.sep) - base.count(os.sep) > 3:
                    dirnames[:] = []
                    continue
                low = [f.lower() for f in filenames]
                if "asio.h" in low or "iasiodrv.h" in low:
                    hits.append(dirpath)
        except Exception:
            pass
    if hits:
        for h in hits:
            print(f"  ✓ 找到 SDK 头文件：{h}")
    else:
        print("  ✗ 没找到 asio.h / iasiodrv.h")
        print("    ⇒ 走「原生 ASIO 宿主」路线需要先拿到 Steinberg 官方 ASIO SDK")
        print("      https://www.steinberg.net/developers/  （免费，但需接受其授权条款；")
        print("       其条款禁止把 SDK 源码本身再分发，编译产物可以）")


if __name__ == "__main__":
    print("ASIO 可行性探针（只读）")
    scan_asio_registry()
    scan_libs()
    scan_sdk()
    print()
    print("=" * 78)
    print("结论速查")
    print("=" * 78)
