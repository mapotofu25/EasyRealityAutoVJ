# -*- coding: utf-8 -*-
"""打测试压缩包（版本号与 exe 内显示严格同步）。

包名 = EasyRealityAutoVJ_测试版 + 月日 + 当日序号，例如 EasyRealityAutoVJ_测试版092307.zip
序号来自 `tools/build_version.py` 写在 `src/_build_ver.py` 里的那一份 ——
也就是**当前 exe 里显示的版本号**，所以「包名 == 软件内版本」，不会对不上。

用法：
    venv\\Scripts\\python tools\\make_zip.py            # 打包当前 dist
    venv\\Scripts\\python tools\\make_zip.py 2026.09.23.07   # 只校验版本是否一致

包内容：
    EasyRealityAutoVJ/            整个 dist 目录（exe + _internal）
    使用说明.txt / 使用说明_EN.txt   放在压缩包顶层，方便直接看到
"""
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import build_version          # noqa: E402

DIST = os.path.join(ROOT, os.environ.get("AUTOVJ_DIST", "dist"), "EasyRealityAutoVJ")
MANUALS = ["使用说明.txt", "使用说明_EN.txt"]
# 「更新内容.txt / _EN.txt」只放在项目根，**不打进压缩包**（用户 2026-09-23 要求）


def slim_cyndilib():
    """cyndilib 自带 3 平台 NDI 运行库 + 源码文件，只用得上 x64 → 省约 80MB。"""
    internal = os.path.join(DIST, "_internal", "cyndilib")
    removed = 0
    if not os.path.isdir(internal):
        return 0
    for root, dirs, files in os.walk(internal):
        for f in files:
            low = f.lower()
            if (low.endswith((".cpp", ".pyx", ".pxd", ".html", ".lib", ".so", ".a"))
                    or "directshow" in low or "uwp" in low):
                try:
                    os.remove(os.path.join(root, f))
                    removed += 1
                except Exception:
                    pass
    for root, dirs, files in os.walk(internal, topdown=False):
        if not os.listdir(root) and root != internal:
            try:
                os.rmdir(root)
            except Exception:
                pass
    return removed


def main():
    date_key, seq, ver = build_version.read_build_info()
    if seq <= 0:
        print("!! 还没打包过（src/_build_ver.py 里没有版本号），先跑 tools/build_exe.py")
        return 1
    zip_name = build_version.zip_name(date_key, seq)
    zip_path = os.path.join(ROOT, zip_name)

    if len(sys.argv) > 1 and sys.argv[1] != ver:
        print(f"!! 包内版本 {ver} 与参数 {sys.argv[1]} 不一致，已按 exe 实际版本命名")
    if not os.path.isdir(DIST):
        print("!! 找不到 dist/EasyRealityAutoVJ，先打包 exe")
        return 1
    exe = os.path.join(DIST, "EasyRealityAutoVJ.exe")
    if not os.path.exists(exe):
        print("!! dist 里没有 EasyRealityAutoVJ.exe")
        return 1

    # 提示「exe 是否比源码旧」（漏打包最常见的坑）
    stale = [f for f in os.listdir(os.path.join(ROOT, "src"))
             if f.endswith(".py") and os.path.getmtime(os.path.join(ROOT, "src", f)) > os.path.getmtime(exe)]
    if stale:
        print(f"!! 注意：以下源文件比 exe 新，包内可能不是最新代码 → {stale}")

    print(f"版本 {ver}（{date_key} 第 {seq} 次生成）")
    print(f"包名 {zip_name}")
    print("cyndilib 瘦身删除:", slim_cyndilib(), "个文件")

    for m in MANUALS:
        src = os.path.join(ROOT, m)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DIST, m))
    # 旧版只有中文说明，dist 里可能残留 → 不在本次清单里的说明文件一并进包（顶层）
    # 注意：只认「使用说明*」，其它 txt（更新内容等）一律不进包
    top_files = [f for f in os.listdir(DIST)
                 if os.path.isfile(os.path.join(DIST, f)) and f.lower().endswith(".txt")
                 and "使用说明" in f]

    n = 0
    t0 = time.time()
    # compresslevel=6：与之前几版包大小一致（约 470MB）；用 1 会大 15MB 左右
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(DIST):
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, os.path.dirname(DIST))   # 保留 EasyRealityAutoVJ/ 前缀
                z.write(p, rel)
                n += 1
        for f in top_files:
            z.write(os.path.join(DIST, f), f)
            n += 1
    print(f"完成：{zip_path}")
    print(f"      {n} 条目 / {os.path.getsize(zip_path) / 1048576:.1f} MB / 用时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
