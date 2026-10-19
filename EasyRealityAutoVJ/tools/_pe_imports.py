# -*- coding: utf-8 -*-
"""极简 PE 导入表解析器（零依赖，用于取证「运行时到底加载哪一份 NDI DLL」）。

用法：
    venv\\Scripts\\python -u tools\\_pe_imports.py <file.dll|file.pyd> [...]

输出：每个文件 PE 导入表里引用的 DLL 名字（大小写按原样）。
用来证明 cyndilib 的 .pyd 模块只 import `Processing.NDI.Lib.x64.dll`，
而不是 DirectShow / UWP 那两份。
"""
import struct
import sys


def _rva_to_off(rva, sections):
    """把 RVA 映射到文件偏移（未命中返回 None）。"""
    for va, vsize, raw, rsize in sections:
        if va <= rva < va + max(vsize, rsize):
            return raw + (rva - va)
    return None


def parse_imports(path):
    """返回该 PE 文件导入表里引用的 DLL 名列表。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        raise ValueError("not a PE (no MZ)")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise ValueError("not a PE (no PE sig)")
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    is_pe32p = magic == 0x20B
    dd_off = opt + (112 if is_pe32p else 96)   # 数据目录：PE32+ 112 / PE32 96
    import_rva = struct.unpack_from("<I", data, dd_off + 8)[0]   # 目录[1] = 导入表
    sec_off = opt + opt_size
    sections = []
    for i in range(num_sections):
        base = sec_off + i * 40
        vsize = struct.unpack_from("<I", data, base + 8)[0]
        va = struct.unpack_from("<I", data, base + 12)[0]
        rsize = struct.unpack_from("<I", data, base + 16)[0]
        raw = struct.unpack_from("<I", data, base + 20)[0]
        sections.append((va, vsize, raw, rsize))
    off = _rva_to_off(import_rva, sections)
    if off is None:
        return []
    names = []
    i = 0
    while True:
        desc = off + i * 20
        if desc + 20 > len(data):
            break
        # IMAGE_IMPORT_DESCRIPTOR: OriginalFirstThunk, TimeDateStamp, ForwarderChain, Name, FirstThunk
        oft, ts, fc, name_rva, ft = struct.unpack_from("<IIIII", data, desc)
        if name_rva == 0 and ts == 0 and fc == 0 and oft == 0 and ft == 0:
            break
        noff = _rva_to_off(name_rva, sections)
        if noff is None:
            break
        end = data.index(b"\x00", noff)
        names.append(data[noff:end].decode("latin-1"))
        i += 1
        if i > 5000:
            break
    return names


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    for p in argv:
        try:
            names = parse_imports(p)
        except Exception as e:  # noqa: BLE001
            print(f"!! {p}: {e}")
            continue
        ndi = [n for n in names if "ndi" in n.lower()]
        print(f"{p}")
        print(f"    导入表 DLL 数={len(names)}  NDI 相关={ndi if ndi else '（无）'}")
        print(f"    全部: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
