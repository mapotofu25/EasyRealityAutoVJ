# -*- coding: utf-8 -*-
"""直接读运行中 AutoVJ 进程里 AudioState 的关键字段（bool / float）。

原理：先用 py-spy 拿到 `st: <AudioState at 0x...>` 地址，再用 ReadProcessMemory
读该对象内存，按 CPython 对象布局找属性。⚠ 只读诊断。

由于 CPython 3.13 对象布局 + 类型指针判断较脆弱，这里用**穷举扫描 + 类型校验**：
在对象内存附近搜 "True/False" 布尔对象指针，配合已知 bool 对象地址比对，
定位 ndi_audio_on 这类 bool 槽位。实践上更稳的做法是只报候选。

用法：venv\\Scripts\\python tools\\_read_state.py <PID>
"""
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYSPY = os.path.join(ROOT, "venv", "Scripts", "py-spy.exe")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


def read(pid, addr, size):
    h = k32.OpenProcess(0x0410, False, pid)      # VM_READ | QUERY_INFORMATION
    if not h:
        return None, "OpenProcess 失败 err=%d（管理员权限？）" % ctypes.get_last_error()
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n))
    k32.CloseHandle(h)
    if not ok:
        return None, "ReadProcessMemory 失败 err=%d @%#x" % (ctypes.get_last_error(), addr)
    return buf.raw, ""


def main():
    pid = int(sys.argv[1])
    out = subprocess.run([PYSPY, "dump", "--pid", str(pid), "--locals"],
                         capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"st: <AudioState at (0x[0-9a-f]+)>", out)
    if not m:
        print("没找到 AudioState 地址（进程在跑吗 / 管理员权限？）")
        print(out[:500])
        return 3
    st = int(m.group(1), 16)
    print("AudioState @", hex(st))

    # AudioState 的 __dict__ 里存属性；先读对象头部拿到 dict 指针
    # CPython 3.13: object header = ob_refcnt(8) + ob_type(8)
    raw, err = read(pid, st, 64)
    if raw is None:
        print(err)
        return 3
    import struct
    refcnt, typ = struct.unpack("<QQ", raw[:16])
    print("refcnt=%d  type=%#x" % (refcnt, typ))

    # 对普通对象，__dict__ 指针在 header 之后（带 dict 的类型）。
    # 逐个 8 字节槽位尝试当成 dict 指针，读 dict 的 keys/values。
    print("\n扫描候选 __dict__ 指针：")
    for off in range(16, 64, 8):
        p = struct.unpack("<Q", raw[off:off + 8])[0]
        if not (0x1000 < p < 0x7FFFFFFFFFFF):
            continue
        d, e = read(pid, p, 48)
        if d is None:
            continue
        # dict 内存：ob_refcnt, ob_type, ma_used, ...
        dref, dtype, dused = struct.unpack("<QQq", d[:24])
        print("  off+%d -> %#x  refcnt=%d type=%#x used=%d" % (off, p, dref, dtype, dused))
    print("\n（若 used 明显是属性个数，就是它）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
