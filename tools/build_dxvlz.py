"""编译 DXV 原生解包 DLL → `src/_dxvlz.dll`

⚠ 只有**开发机**需要 zig（它自带 C 编译器，不用装 VS/Windows SDK）。
   发布包里只需要带上编译好的 `_dxvlz.dll`（几十 KB），用户端不需要任何编译器。

zig 的获取（一次性）：
    pip install --no-deps --target P:\\AutoVJ\\_build_tools ziglang
    ⚠ 该 wheel 解压后有 363MB / 19565 个文件，逐个写盘要十几分钟（杀软逐文件扫描）。
      可以改用 `tools\\_extract_zig.py` 只抽 Windows 目标需要的部分。

用法：
    venv\\Scripts\\python.exe tools\\build_dxvlz.py              # 自动找 zig
    venv\\Scripts\\python.exe tools\\build_dxvlz.py D:\\zig\\zig.exe
"""

import glob
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "native", "dxvlz.c")
OUT = os.path.join(ROOT, "src", "_dxvlz.dll")


def find_zig():
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        return sys.argv[1]
    cands = [
        os.path.join(ROOT, "_build_tools", "ziglang", "zig.exe"),
        os.path.join(ROOT, "_build_tools", "zig", "zig.exe"),
    ]
    cands += glob.glob(os.path.join(ROOT, "_build_tools", "**", "zig.exe"), recursive=True)
    cands += glob.glob(r"C:\**\zig.exe", recursive=False)
    for c in cands:
        if os.path.exists(c):
            return c
    w = shutil.which("zig")
    if w:
        return w
    return None


def main():
    zig = find_zig()
    if not zig:
        print("找不到 zig。请先执行（一次性，开发机）：")
        print("  python -m pip install --no-deps --target %s ziglang"
              % os.path.join(ROOT, "_build_tools"))
        print("  或 %s %s" % (os.path.join(ROOT, "tools", "_extract_zig.py"), ""))
        return 1
    print("zig   :", zig)
    print("源文件:", SRC)
    if not os.path.exists(SRC):
        print("★ 找不到 C 源文件")
        return 1
    tmp = os.path.join(ROOT, "_build_tools", "dxvlz.out")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    cmd = [zig, "cc", "-O2", "-shared", "-fno-sanitize=all",
           "-o", tmp, SRC]
    print("命令  :", " ".join(cmd))
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("★ 编译失败 rc=%d" % r.returncode)
        print((r.stderr or "")[-3000:])
        return 1
    sz = os.path.getsize(tmp)
    shutil.copy2(tmp, OUT)
    print("✅ 编译成功：%s  (%.1f KB, %.1fs)" % (OUT, sz / 1024, time.perf_counter() - t0))
    # 自检：能否加载并解析一个真实 DXV 帧
    sys.path.insert(0, os.path.join(ROOT, "src"))
    try:
        import dxvnative
        print("   自检 available() =", dxvnative.available())
    except Exception as e:                                     # noqa: BLE001
        print("   自检失败（加载 DLL）:", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
