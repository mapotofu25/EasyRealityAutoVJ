# -*- coding: utf-8 -*-
r"""项目一键备份：把「不可再生的最小集合」拷到 P:\AutoVJ_备份\AutoVJ_<时间戳>\

为什么不整目录复制：`venv`(925MB) / `dist`(1.3GB) / `build*` / 各种测试 zip 都能重新生成，
而且会拖慢拷贝。真正**丢了就没了**的只有：
  · `src/`    —— 全部代码（28 个文件）
  · `tools/`  —— 构建、诊断、自检脚本
  · `assets/` —— ONNX 模型（Chinese-CLIP 345MB + Discogs 18MB，重新下载很麻烦）
  · 根目录的文档与配置样例（使用说明 / 更新内容 / 能量算法说明 / requirements / .bat / build_seq.json）

用法：
  venv\Scripts\python.exe tools\backup_project.py            → 备份到 P:\AutoVJ_备份\
  venv\Scripts\python.exe tools\backup_project.py D:\mybak   → 指定备份根目录
"""
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = ["src", "tools", "assets"]
FILE_PATTERNS = (
    r"^使用说明.*\.txt$", r"^更新内容.*\.txt$", r"^.*_说明\.txt$",
    r"^能量算法说明\.txt$", r"^.*方案.*\.txt$", r"^README\.md$", r"^requirements\.txt$",
    r"^.*\.bat$", r"^build_seq\.json$",
)
SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache"}
SKIP_FILE_SUFFIX = (".pyc", ".pyo")


def copy_tree(src, dst):
    """拷贝目录（跳过 __pycache__/.pyc）"""
    n = 0
    for r, ds, fs in os.walk(src):
        ds[:] = [d for d in ds if d not in SKIP_DIR_NAMES]
        rel = os.path.relpath(r, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for f in fs:
            if f.endswith(SKIP_FILE_SUFFIX):
                continue
            try:
                shutil.copy2(os.path.join(r, f), os.path.join(out, f))
                n += 1
            except Exception as e:      # noqa
                print("   拷贝失败 %s: %s" % (f, e))
    return n


def dir_size(p):
    tot = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            try:
                tot += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return tot


def main():
    bak_root = sys.argv[1] if len(sys.argv) > 1 else r"P:\AutoVJ_备份"
    stamp = time.strftime("%Y%m%d_%H%M")
    dst = os.path.join(bak_root, "AutoVJ_" + stamp)
    if os.path.exists(dst):
        dst += "_" + time.strftime("%S")
    os.makedirs(dst, exist_ok=True)

    # 版本号（源码未打包时读 _build_ver，读不到就算开发版）
    ver = "开发版"
    try:
        sys.path.insert(0, os.path.join(ROOT, "src"))
        import _build_ver  # noqa
        ver = getattr(_build_ver, "BUILD_VER", ver)
    except Exception:
        pass

    print("=" * 70)
    print("备份项目 → %s" % dst)
    print("=" * 70)
    total = 0
    rows = []
    for d in DIRS:
        s = os.path.join(ROOT, d)
        if not os.path.isdir(s):
            print("  跳过（不存在）: %s" % d)
            continue
        t = time.time()
        n = copy_tree(s, os.path.join(dst, d))
        sz = dir_size(os.path.join(dst, d))
        total += sz
        rows.append((d, n, sz))
        print("  %-10s %4d 个文件  %8.1f MB  (%.1fs)" % (d, n, sz / 1e6, time.time() - t))

    # 根目录的文档 / 脚本 / 配置样例
    nd = 0
    for f in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p):
            continue
        if any(re.match(pat, f) for pat in FILE_PATTERNS):
            shutil.copy2(p, os.path.join(dst, f))
            total += os.path.getsize(p)
            nd += 1
    print("  根目录文档    %4d 个文件" % nd)

    # 备份说明
    readme = [
        "Easy Reality AutoVJ —— 项目源码备份",
        "=" * 60,
        "备份时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "软件版本：%s" % ver,
        "来源目录：%s" % ROOT,
        "",
        "本备份包含（丢了自己重做的成本最高）：",
    ]
    for d, n, sz in rows:
        readme.append("  · %-8s %4d 个文件  %8.1f MB" % (d, n, sz / 1e6))
    readme += [
        "  · 根目录的 使用说明 / 更新内容 / 能量算法说明 / requirements.txt / *.bat / build_seq.json",
        "",
        "本备份**不包含**（都能重新生成，不必占空间）：",
        "  · venv\\        虚拟环境（重建：python -m venv venv + pip install -r requirements.txt）",
        "  · dist\\ build*\\ PyInstaller 产物（重建：venv\\Scripts\\python tools\\build_exe.py）",
        "  · *.zip        发群的测试包（重建：venv\\Scripts\\python tools\\make_zip.py）",
        "  · 用户数据      %LOCALAPPDATA%\\AutoVJ（配置/缩略图/指纹库，在用户机器上）",
        "",
        "恢复方法：把本目录里的 src\\ tools\\ assets\\ 和文档拷回项目根目录即可。",
        "",
        "后续要跑起来还需要：",
        "  1) python -m venv venv",
        "  2) venv\\Scripts\\pip install -r requirements.txt",
        "  3) venv\\Scripts\\python tools\\build_version.py set <序号>   # 恢复版本计数（如需要）",
    ]
    with open(os.path.join(dst, "备份说明.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(readme) + "\n")

    print("")
    print("合计 %.1f MB，备份完成：%s" % (total / 1e6, dst))
    print("（含 备份说明.txt，写了恢复步骤）")


if __name__ == "__main__":
    main()
