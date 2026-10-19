# -*- coding: utf-8 -*-
"""查 C 盘占用（快版）：只量指定目录，带全局时间预算，绝不走全盘。

只读，不删任何东西。
"""
import os
import shutil
import sys
import tempfile
import time

DEADLINE = time.time() + 45          # 整个脚本最多跑 45 秒
LOG = []


def du(path, cap_files=40000):
    total = n = 0
    cut = False
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                n += 1
            except Exception:                                       # noqa: BLE001
                pass
            if n >= cap_files or time.time() > DEADLINE:
                cut = True
                break
        if cut:
            break
    return total, n, cut


def show(label, path, cap=40000):
    if not os.path.exists(path):
        print("  %-44s (不存在)" % label)
        return 0
    b, n, cut = du(path, cap)
    print("  %-44s %9.1f MB %7d 文件%s"
          % (label, b / 1048576, n, "  (超时/截断)" if cut else ""))
    sys.stdout.flush()
    return b


def main():
    C = os.environ.get("SystemDrive", "C:") + "\\"
    try:
        u = shutil.disk_usage(C)
        print("=" * 76)
        print("%s 总 %.1f GB / 已用 %.1f GB / **可用 %.1f GB**（占 %.1f%%）"
              % (C, u.total / 1073741824, u.used / 1073741824,
                 u.free / 1073741824, u.used * 100.0 / u.total))
        print("=" * 76)
    except Exception as e:                                          # noqa: BLE001
        print("取盘容量失败：", e)

    LA = os.environ.get("LOCALAPPDATA", "")
    TMP = tempfile.gettempdir()
    HOME = os.path.expanduser("~")

    print("\n【A. 本项目数据目录】")
    mine = 0
    mine += show("AutoVJ 合计", os.path.join(LA, "AutoVJ"))
    for sub in ("thumbs", "cuesheets"):
        show("  └ " + sub, os.path.join(LA, "AutoVJ", sub))
    for f in ("fingerprints.db", "config.json", ".ui_stall.log", "ui_stall.log"):
        p = os.path.join(LA, "AutoVJ", f)
        if os.path.isfile(p):
            print("  └ %-41s %9.1f MB" % (f, os.path.getsize(p) / 1048576))

    print("\n【B. 系统临时目录里我的前缀】\n  TEMP =", TMP)
    try:
        entries = os.listdir(TMP)
    except Exception:                                               # noqa: BLE001
        entries = []
    PRE = ("autovj_", "ercue", "hlreal", "hl_rr", "demo_src", "demochk",
           "easyreality", "pip-")
    tb = 0
    for e in sorted(entries):
        low = e.lower()
        if not any(low.startswith(p) for p in PRE):
            continue
        p = os.path.join(TMP, e)
        b, n, _c = du(p, 20000)
        if b > 0 or os.path.isdir(p):
            print("  %-44s %9.1f MB %7d 文件" % (e[:44], b / 1048576, n))
            tb += b
    print("  我的前缀合计 %.1f MB" % (tb / 1048576))
    mine += tb

    print("\n【C. 我截的图 / 读过的图】")
    tc = 0
    for sub in ("blobs", "clipboard-images"):
        tc += show("  .workbuddy\\" + sub, os.path.join(HOME, ".workbuddy", sub))
    mine += tc

    print("\n【D. 我装过的开发工具残留】")
    td = 0
    for p in (os.path.join(LA, "pip", "cache"), os.path.join(LA, "Temp", "pip-"),
              os.path.join(HOME, ".cache", "pip")):
        td += show("  " + p, p)
    mine += td

    print("\n" + "=" * 76)
    print("属于我 / 本项目、可清理的合计 ≈ **%.1f MB**"
          % ((mine) / 1048576))
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
