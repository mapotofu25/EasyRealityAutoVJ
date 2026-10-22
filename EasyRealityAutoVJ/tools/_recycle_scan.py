# -*- coding: utf-8 -*-
"""看各盘回收站里到底有什么（大头 + 时间），判断是不是我的删除被送进去了。

⚠ 路径里的 `$RECYCLE.BIN` 不能写进 `python -c "..."` —— bash 会把 `$RECYCLE` 当变量展开，
   于是路径变成空的，脚本会报"不存在"（我踩了两次）。
"""
import os
import sys
import time

RB = "$RECYCLE.BIN"
DRIVES = [d + ":\\" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ"
          if os.path.isdir(d + ":\\")]


def scan(root, label):
    if not os.path.isdir(root):
        print("  %s：不存在" % label)
        return 0
    rows = []
    tot = 0
    n = 0
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                s = os.path.getsize(p)
            except Exception:                                       # noqa: BLE001
                continue
            tot += s
            n += 1
            if s > 20 * 1024 * 1024:
                rows.append((s, os.path.getmtime(p), p))
    print("  %s：%d 个文件，合计 %.2f GB（其中 >20MB 的 %d 个）"
          % (label, n, tot / 1073741824, len(rows)))
    rows.sort(reverse=True)
    for s, m, p in rows[:10]:
        print("     %8.2f GB  %s  %s" % (
            s / 1073741824, time.strftime("%m-%d %H:%M", time.localtime(m)),
            os.path.basename(p)[:46]))
    return tot


def main():
    print("今天 =", time.strftime("%Y-%m-%d %H:%M"))
    grand = 0
    for d in DRIVES:
        p = os.path.join(d, RB)
        if os.path.isdir(p):
            grand += scan(p, "%s 回收站" % d)
    print("\n各盘回收站合计 %.2f GB" % (grand / 1073741824))
    return 0


if __name__ == "__main__":
    sys.exit(main())
