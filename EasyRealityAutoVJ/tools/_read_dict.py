# -*- coding: utf-8 -*-
"""读运行进程 AudioState.__dict__，打印真实属性值（bool/int/float/str/None）。

用法：venv\\Scripts\\python tools\\_read_dict.py <PID> <dict_addr_hex>
"""
import ctypes
import struct
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def rd(pid, addr, size):
    h = k32.OpenProcess(0x0410, False, pid)
    if not h:
        return None
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n))
    k32.CloseHandle(h)
    return buf.raw[:n.value] if ok else None


def read_pystr(pid, addr):
    """读 CPython str 对象（compact ascii/unicode）。"""
    hdr = rd(pid, addr, 56)
    if not hdr:
        return None
    # ob_refcnt(8), ob_type(8), ob_size(8)  [length]
    # compact unicode: hash(8), state(8), len(8), then data
    try:
        length = struct.unpack("<q", hdr[16:24])[0]
    except Exception:
        return None
    if not (0 <= length <= 200):
        return None
    data = rd(pid, addr + 56, max(1, length * 4))
    if data is None:
        return None
    try:
        if length <= 200 and data[:2] == b"\x00\x00" or True:
            pass
    except Exception:
        return None
    # 先试 latin-ish：找可打印
    try:
        s = data[:length].decode("utf-8", "replace")
    except Exception:
        s = repr(data[:length])
    return s


def obj_repr(pid, addr):
    """给一个对象指针，尽力给出可读值。"""
    if addr == 0:
        return "NULL"
    hdr = rd(pid, addr, 32)
    if not hdr:
        return "<?>"
    refcnt, typ = struct.unpack("<QQ", hdr[:16])
    # 常见小整数缓存 -5..256
    if 0x7FFFF0000000 < typ or True:
        pass
    # 试 bool：CPython 的 True/False 是单例
    # 我们不知道地址，改从"指向的对象类型名"入手太难；这里只处理 str 与数值启发式。
    s = read_pystr(pid, addr)
    if s is not None and 1 <= len(s) <= 60 and all(31 < ord(c) < 127 or ord(c) > 127 for c in s):
        # 进一步确认像 str：读它的 type 名
        tn = read_pystr(pid, typ)
        if tn == "str":
            return repr(s)
    return "<obj %#x type=%#x>" % (addr, typ)


def main():
    pid = int(sys.argv[1])
    d = int(sys.argv[2], 16)
    raw = rd(pid, d, 0x40)
    if not raw:
        print("读不到 dict @", hex(d))
        return 3
    refcnt, typ, used = struct.unpack("<QQq", raw[:24])
    mask = struct.unpack("<Q", raw[24:32])[0]
    print("dict used=%d mask=%d @%#x" % (used, mask, d))
    if not (0 < used < 500):
        print("used 不像属性个数，可能不是 __dict__")
    # keys 指针在 offset 32, values 在 40 (3.13 布局近似)
    keys_ptr, vals_ptr = struct.unpack("<QQ", raw[32:48])
    print("keys=%#x vals=%#x" % (keys_ptr, vals_ptr))
    if not keys_ptr or not vals_ptr:
        return 0
    n = used + 4
    kb = rd(pid, keys_ptr, 8 * n)
    vb = rd(pid, vals_ptr, 8 * n)
    if not kb or not vb:
        print("读 keys/values 表失败")
        return 3
    kaddrs = struct.unpack("<%dQ" % n, kb)
    vaddrs = struct.unpack("<%dQ" % n, vb)
    cnt = 0
    for ka, va in zip(kaddrs, vaddrs):
        if ka == 0:
            continue
        key = read_pystr(pid, ka)
        if not isinstance(key, str):
            continue
        if key in ("ndi_audio_on", "ndi_feed", "running", "error",
                   "silent_sec", "device_desc", "asio_active"):
            print("  %-14s = %s" % (key, obj_repr(pid, va)))
            cnt += 1
    if cnt == 0:
        print("未命中目标键；列出前 20 个键名：")
        shown = 0
        for ka, va in zip(kaddrs, vaddrs):
            if ka == 0:
                continue
            key = read_pystr(pid, ka)
            if isinstance(key, str):
                print("   ", key)
                shown += 1
                if shown >= 20:
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main())
