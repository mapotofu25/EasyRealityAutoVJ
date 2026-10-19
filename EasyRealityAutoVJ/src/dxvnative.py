"""DXV3 帧解包：把 FFmpeg 套在 BC 块外面的那层 LZ 中间压缩解掉。

底层是 `src/native/dxvlz.c` 用 zig 编译出来的 `_dxvlz.dll`（见 tools/build_dxvlz.py）。
**DLL 不存在 / 解包失败一律返回 None** —— 调用方必须回退到 FFmpeg 软解，
绝不能让"装了没有显卡驱动的用户"直接黑屏。

用法：
    import dxvnative
    if dxvnative.available():
        info = dxvnative.parse_header(payload)      # (fmt, raw, off, plen) 或 None
        blocks = dxvnative.unpack(payload[off:off+plen], fmt, raw, dxvnative.block_bytes(w, h, fmt))
"""

import ctypes
import os
import sys

_DLL = None
_TRIED = False
LAST_ERROR = ""


def dll_path():
    """`_dxvlz.dll` 的位置。

    打包（PyInstaller onedir）后二进制放在 `_internal/`，即 `sys._MEIPASS`；
    源码运行时它就在本模块旁边。两处都试，谁先存在用谁 —— DLL 找不到会一路
    回退软解，所以这里必须够稳。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (getattr(sys, "_MEIPASS", None), here):
        if not base:
            continue
        p = os.path.join(base, "_dxvlz.dll")
        if os.path.exists(p):
            return p
    return os.path.join(here, "_dxvlz.dll")


def _load():
    global _DLL, _TRIED, LAST_ERROR
    if _TRIED:
        return _DLL
    _TRIED = True
    p = dll_path()
    if not os.path.exists(p):
        LAST_ERROR = "找不到 %s（请先跑 tools/build_dxvlz.py 编译）" % p
        return None
    try:
        d = ctypes.CDLL(p)
        ip = ctypes.POINTER(ctypes.c_int)
        d.dxv_header.argtypes = [ctypes.c_char_p, ctypes.c_int, ip, ip, ip, ip]
        d.dxv_header.restype = ctypes.c_int
        d.dxv_unpack.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int]
        d.dxv_unpack.restype = ctypes.c_int
        _DLL = d
    except Exception as e:                                     # noqa: BLE001
        LAST_ERROR = "加载 DLL 失败：%s" % e
        _DLL = None
    return _DLL


def available():
    return _load() is not None


def parse_header(buf):
    """解析 DXV 帧头 → (fmt, raw, off, plen)；不支持/非法返回 None。

    fmt: 1=DXT1(BC1) / 5=DXT5(BC3)。YCG6/YG10 与旧式帧头都会返回 None（走软解）。
    """
    d = _load()
    if d is None or not buf:
        return None
    a, b, c, e = (ctypes.c_int() for _ in range(4))
    rc = d.dxv_header(bytes(buf), len(buf), ctypes.byref(a), ctypes.byref(b),
                      ctypes.byref(c), ctypes.byref(e))
    if rc != 0:
        return None
    return (a.value, b.value, c.value, e.value)


def block_bytes(w, h, fmt):
    """BC 块数据长度：每 4x4 块 BC1=8 字节 / BC3=16 字节"""
    return (int(w) // 4) * (int(h) // 4) * (8 if int(fmt) == 1 else 16)


def unpack(payload, fmt, raw, out_len):
    """还原 BC 块流；失败返回 None（调用方回退软解）"""
    d = _load()
    if d is None or not payload or out_len <= 0:
        return None
    blob = bytes(payload)
    dst = ctypes.create_string_buffer(int(out_len))
    rc = d.dxv_unpack(blob, len(blob), dst, int(out_len), int(fmt), int(raw))
    if rc != 0:
        return None
    return dst.raw


def frame_blocks(payload, w, h):
    """一步到位：帧负载 → BC 块。返回 (fmt, blocks) 或 None"""
    info = parse_header(payload)
    if info is None:
        return None
    fmt, raw, off, plen = info
    n = block_bytes(w, h, fmt)
    blk = unpack(payload[off:off + plen], fmt, raw, n)
    if blk is None:
        return None
    return (fmt, blk)
