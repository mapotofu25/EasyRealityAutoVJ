# -*- coding: utf-8 -*-
"""编译 ASIO 原生宿主 DLL → `src/_asiohost.dll`

⚠ 只有**开发机**需要 zig（它自带 C/C++ 编译器，不用装 VS/Windows SDK）。
   发布包里只需要带上编译好的 `_asiohost.dll`（几十 KB），用户端不需要任何编译器。

## 关于 ASIO SDK（重要）
宿主代码 #include 了 Steinberg 官方 ASIO SDK 的头文件，并链接 SDK 里的
`asio.cpp` / `asiodrivers.cpp` / `asiolist.cpp`。

**SDK 源码不进 git**（其许可禁止再分发源码，但允许分发编译产物）：
本脚本首次运行会自动下载到 `_asio_sdk/`，该目录已在 `.gitignore` 里。

官方下载地址（2026-10-01 实测 200 OK）：
    https://download.steinberg.net/sdk_downloads/asiosdk2.3.zip

## 用法
    venv\\Scripts\\python.exe tools\\build_asio.py            # 自动找 zig
    venv\\Scripts\\python.exe tools\\build_asio.py D:\\zig\\zig.exe
"""

import glob
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "native", "asiohost.cpp")
OUT = os.path.join(ROOT, "src", "_asiohost.dll")

SDK_DIR = os.path.join(ROOT, "_asio_sdk")
SDK_ZIP = os.path.join(SDK_DIR, "asiosdk2.3.zip")
SDK_URL = "https://download.steinberg.net/sdk_downloads/asiosdk2.3.zip"
SDK_SUB = "ASIOSDK2.3"


def find_zig():
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        return sys.argv[1]
    cands = [
        os.path.join(ROOT, "_build_tools", "ziglang", "zig.exe"),
        os.path.join(ROOT, "_build_tools", "zig", "zig.exe"),
    ]
    cands += glob.glob(os.path.join(ROOT, "_build_tools", "**", "zig.exe"), recursive=True)
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which("zig")


def ensure_sdk():
    """确保 `_asio_sdk/ASIOSDK2.3/` 存在（缺就下载并解压）。"""
    root = os.path.join(SDK_DIR, SDK_SUB)
    need = (os.path.isfile(os.path.join(root, "common", "asio.h"))
            and os.path.isfile(os.path.join(root, "host", "pc", "asiolist.cpp")))
    if need:
        return root
    os.makedirs(SDK_DIR, exist_ok=True)
    if not os.path.isfile(SDK_ZIP):
        print("下载 ASIO SDK：%s" % SDK_URL)
        urllib.request.urlretrieve(SDK_URL, SDK_ZIP)
        print("  已下载 %.0f KB" % (os.path.getsize(SDK_ZIP) / 1024.0))
    print("解压 ASIO SDK → %s" % SDK_DIR)
    with zipfile.ZipFile(SDK_ZIP) as z:
        z.extractall(SDK_DIR)
    if not os.path.isdir(root):
        raise SystemExit("解压后仍找不到 %s" % root)
    return root


def check_imports(path):
    """列出 DLL 的导入表，并**警告**是否依赖 C++ 运行时 DLL。

    为什么必须查：我们刻意用 `-nostdinc++` + 自带 operator new/delete 来避免
    依赖 libstdc++/libcxx —— 一旦哪天编译参数改动让它们回来，发行包里就会缺 DLL、
    用户端一开 ASIO 就崩。这里直接在构建时就把它暴露出来。
    """
    try:
        data = open(path, "rb").read()
        import struct
        if data[:2] != b"MZ":
            print("!! 不是有效 PE 文件（前 2 字节 %r）" % data[:2])
            return False
        off = struct.unpack("<I", data[0x3c:0x40])[0]
        if data[off:off + 4] != b"PE\0\0":
            print("!! 没有 PE 签名")
            return False
        nsec = struct.unpack("<H", data[off + 6:off + 8])[0]
        opt = off + 24
        magic = struct.unpack("<H", data[opt:opt + 2])[0]
        dd = opt + (112 if magic == 0x20b else 96)
        imp_rva, imp_sz = struct.unpack("<II", data[dd + 8:dd + 16])
        if not imp_rva:
            print("   导入表：空")
            return True
        # RVA → 文件偏移
        sec = off + 24 + struct.unpack("<H", data[off + 20:off + 22])[0]
        secs = []
        for i in range(nsec):
            b = sec + i * 40
            va, vsz = struct.unpack("<II", data[b + 12:b + 20])
            praw, _ = struct.unpack("<II", data[b + 20:b + 28])
            secs.append((va, vsz, praw))

        def r2o(rva):
            for va, vsz, praw in secs:
                if va <= rva < va + max(vsz, 1):
                    return praw + (rva - va)
            return None

        names = []
        o = r2o(imp_rva)
        while o is not None:
            ent = data[o:o + 20]
            if len(ent) < 20 or ent == b"\0" * 20:
                break
            name_rva = struct.unpack("<I", ent[12:16])[0]
            if not name_rva:
                break
            no = r2o(name_rva)
            if no is None:
                break
            end = data.index(b"\0", no)
            names.append(data[no:end].decode("ascii", "replace"))
            o += 20
        print("   导入表：%s" % ", ".join(names))
        bad = [n for n in names if any(k in n.lower() for k in
               ("libstdc++", "libc++", "libcxx", "libgcc", "libwinpthread"))]
        if bad:
            print("!! 依赖了 C++ 运行时 DLL：%s（发行包会缺文件）" % bad)
            return False
        return True
    except Exception as e:                                     # noqa: BLE001
        print("   （导入表解析失败：%s）" % e)
        return True


def main():
    zig = find_zig()
    if not zig:
        print("找不到 zig。请先：")
        print("  pip install --no-deps --target %s ziglang" % os.path.join(ROOT, "_build_tools"))
        return 2
    sdk = ensure_sdk()

    inc = ["-I" + os.path.join(sdk, "common"),
           "-I" + os.path.join(sdk, "host"),
           "-I" + os.path.join(sdk, "host", "pc")]
    sdk_srcs = [os.path.join(sdk, "common", "asio.cpp"),
                os.path.join(sdk, "host", "asiodrivers.cpp"),
                os.path.join(sdk, "host", "pc", "asiolist.cpp")]
    for f in sdk_srcs:
        if not os.path.isfile(f):
            raise SystemExit("SDK 文件缺失：%s" % f)

    cmd = [zig, "c++", "-shared", "-O2", "-std=c++14",
           "-D_WIN32_WINNT=0x0601", "-DWINVER=0x0601",
           # ⚠ 官方 asiolist.cpp 用的是 **ANSI** 版 Win32 API（RegOpenKey / CharLowerBuff
           #   配 char*）。**绝不能**定义 UNICODE/_UNICODE，否则这些宏会展开成 W 版、
           #   与 char* 实参不匹配，直接编译不过（2026-10-01 实测踩到）。
           # ⚠ **绝不能**加 `-static`：zig 会因此不产出 DLL 而是产出 ar 静态库
           #   （文件头 `!<arch>`，Python 加载报 WinError 193）。
           # ⚠ 也**不能**指望 `-static-libstdc++`：zig 会去从源码重编 libcxx/libunwind，
           #   本机实测 "sub-compilation of libunwind failed" 直接失败。
           #   ⇒ 改用 `-nostdinc++ -fno-exceptions -fno-rtti`：SDK 宿主侧只用到
           #     `new` 和几个 C 函数，不需要真的 C++ 标准库；缺的 operator new/delete
           #     由 src/native/asiohost.cpp 里的**最小垫片**提供。
           #     这样产物**不依赖任何 C++ 运行时 DLL**，打包干净。
           "-nostdinc++", "-fno-exceptions", "-fno-rtti",
           "-o", OUT, SRC] + sdk_srcs + inc + \
          ["-lole32", "-loleaut32", "-luuid", "-ladvapi32", "-luser32", "-lkernel32"]

    # 项目名/tmp 路径别进生成的二进制，便于复现
    env = dict(os.environ)
    env.setdefault("ZIG_GLOBAL_CACHE_DIR", os.path.join(ROOT, "_build_tools", "ziggcache"))
    env.setdefault("ZIG_LOCAL_CACHE_DIR", os.path.join(ROOT, "_build_tools", "zigcache"))

    print("编译 %s" % os.path.basename(OUT))
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    out = (r.stdout or "") + (r.stderr or "")
    if out.strip():
        print(out.strip()[:6000])
    if r.returncode != 0 or not os.path.isfile(OUT):
        print("!! 编译失败（exit=%d，%.1f 秒）" % (r.returncode, dt))
        return 1
    print("完成：%s  %.0f KB  （%.1f 秒）" % (OUT, os.path.getsize(OUT) / 1024.0, dt))
    if not check_imports(OUT):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
