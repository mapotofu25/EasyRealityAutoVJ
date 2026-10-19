# -*- coding: utf-8 -*-
"""限时扫描 C 盘"谁在占空间"：只下探到指定层数，带全局超时，绝不卡死。

用法：venv/Scripts/python.exe -u tools/_disk_top.py [秒=60] [层数=2]
"""
import os
import sys
import time

DRIVE = os.environ.get("SystemDrive", "C:") + "\\"
BUDGET = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
DEPTH = int(sys.argv[2]) if len(sys.argv) > 2 else 2
T0 = time.time()
SKIP = {"$Recycle.Bin", "System Volume Information", "Windows", "WinSxS",
        "servicing", "Installer", "$WinREAgent", "Recovery"}


def size_of(path):
    t = 0
    try:
        for dp, dn, fn in os.walk(path):
            if time.time() - T0 > BUDGET:
                return t, True
            dn[:] = [d for d in dn if not os.path.islink(os.path.join(dp, d))]
            for f in fn:
                try:
                    t += os.path.getsize(os.path.join(dp, f))
                except Exception:                                   # noqa: BLE001
                    pass
    except Exception:                                               # noqa: BLE001
        pass
    return t, False


def walk_level(root, depth, out, prefix=""):
    if depth < 0 or time.time() - T0 > BUDGET:
        return
    try:
        entries = list(os.scandir(root))
    except Exception:                                               # noqa: BLE001
        return
    subs = []
    for e in entries:
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
            if e.name in SKIP and depth == DEPTH:
                continue
            subs.append(e)
        except Exception:                                           # noqa: BLE001
            pass
    for e in subs:
        if time.time() - T0 > BUDGET:
            print("  （超时，未扫完）")
            return
        s, cut = size_of(e.path)
        out.append((s, prefix + e.name))
        walk_level(e.path, depth - 1, out, prefix + e.name + "\\")


def main():
    print("扫描 %s（最多 %.0f 秒，下探 %d 层，跳过 Windows/WinSxS 等系统目录）"
          % (DRIVE, BUDGET, DEPTH))
    out = []
    try:
        for e in os.scandir(DRIVE):
            if not e.is_dir(follow_symlinks=False):
                continue
            if e.name in SKIP:
                continue
            s, _cut = size_of(e.path)
            out.append((s, e.name))
            if time.time() - T0 > BUDGET:
                break
    except Exception as ex:                                         # noqa: BLE001
        print("枚举失败：", ex)
    out.sort(reverse=True)
    print("\n%-46s %10s" % ("目录", "大小"))
    for s, n in out[:25]:
        print("%-46s %9.2f GB" % (n[:46], s / 1073741824))
    print("\n（用时 %.0f 秒；Windows / WinSxS / 回收站等已跳过，"
          "C:\\Windows 通常另有 20~40 GB）" % (time.time() - T0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
