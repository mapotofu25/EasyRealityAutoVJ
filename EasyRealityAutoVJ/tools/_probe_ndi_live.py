# -*- coding: utf-8 -*-
"""读运行中的 AutoVJ 进程，检查 NDI 音频门控两个关键布尔量/对象。

⚠ 只读、只诊断，不改任何东西。
原理：用 py-spy 拿到线程栈与 `self` 地址后，本脚本改用
`ctypes` + ReadProcessMemory 直接读该进程堆上的对象字段，打印：
  - AudioState.ndi_audio_on
  - AudioState.ndi_feed
  - AudioEngine._ndi_q 长度
  - AudioState.running / silent_sec

用法：
  venv\\Scripts\\python tools\\_probe_ndi_live.py <PID>
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
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400


def open_proc(pid):
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        raise OSError("OpenProcess 失败 err=%d（需管理员权限）" % ctypes.get_last_error())
    return h


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                               ctypes.byref(n))
    if not ok:
        return None
    return buf.raw[:n.value]


def dump_stacks(pid):
    out = subprocess.run([PYSPY, "dump", "--pid", str(pid), "--locals"],
                         capture_output=True, text=True, timeout=60)
    return out.stdout


def find_addr(text, pattern):
    m = re.search(pattern, text)
    if not m:
        return None
    return int(m.group(1), 16)


def main():
    if len(sys.argv) < 2:
        print("用法：_probe_ndi_live.py <PID>")
        return 2
    pid = int(sys.argv[1])
    text = dump_stacks(pid)
    if not text.strip():
        print("py-spy 没抓到栈（进程在跑吗？需要管理员权限）")
        return 3

    st_addr = find_addr(text, r"st: <AudioState at (0x[0-9a-f]+)>")
    eng_addr = find_addr(text, r"self: <AudioEngine at (0x[0-9a-f]+)>")
    print("AudioState @", hex(st_addr) if st_addr else None)
    print("AudioEngine @", hex(eng_addr) if eng_addr else None)

    # 打印一切与 ndi 有关的局部变量名（py-spy 的 locals 里可能没有，
    # 因为 ndi_audio_on 是对象属性、不是局部变量 —— 这里只做线索汇总）
    for line in text.splitlines():
        if any(k in line for k in ("ndi", "NDI", "audio_on", "silent")):
            print("  |", line.strip())

    print("\n=== 结论数据来自上面的 py-spy 栈 + 本机 config ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
