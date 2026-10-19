#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性：在**生产库**上执行「指纹列错位」修复（清脏数据 + 正确重写 30 首）。

用法：
    python tools/_fpfix_prod.py            # 执行修复
    python tools/_fpfix_prod.py --check    # 只查状态，不动数据
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config import app_base_dir                                 # noqa: E402
from erlib.transfer import import_into                          # noqa: E402

PKG = os.path.join(ROOT, "dist_erlib", "我的曲库.eravjlib")


def exe_base_dir():
    """**模拟 exe 环境**的数据目录。

    ⚠ `config.app_base_dir()` 在源码运行时会返回**项目根**，而用户的软件是
     打包 exe ⇒ 数据在 `%LOCALAPPDATA%\\AutoVJ`。这里必须显式指向后者，
      否则会去操作一个不存在（或无关）的项目内文件。
    """
    return os.path.join(os.environ.get("LOCALAPPDATA",
                                       os.path.expanduser("~")), "AutoVJ")


def uri(p):
    return "file:%s?mode=ro" % Path(p).as_posix()


def stat(db, tag):
    c = sqlite3.connect(uri(db), uri=True)
    total = c.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    bad = c.execute("SELECT COUNT(*) FROM fingerprints "
                    "WHERE typeof(song_id) <> 'integer'").fetchone()[0]
    c.close()
    print("  %-8s 总行=%d  错位脏行=%d" % (tag, total, bad))
    return total, bad


def sids_of(base):
    c = sqlite3.connect(uri(os.path.join(base, "imported_meta.db")), uri=True)
    try:
        return [r[0] for r in c.execute("SELECT song_id FROM songs")]
    finally:
        c.close()


def count_by_sid(db, sids):
    c = sqlite3.connect(uri(db), uri=True)
    try:
        return sum(c.execute("SELECT COUNT(*) FROM fingerprints "
                             "WHERE song_id=?", (s,)).fetchone()[0]
                   for s in sids)
    finally:
        c.close()


def main():
    check_only = "--check" in sys.argv
    base = exe_base_dir()
    db = os.path.join(base, "fingerprints.db")
    print("生产库：%s" % db)
    print("包    ：%s" % PKG)
    print()
    print("=== 现状 ===")
    stat(db, "修复前")
    sids = sids_of(base)
    n = count_by_sid(db, sids)
    print("  导入的 %d 首按 song_id 可查：%d 条（包内 868850）" % (len(sids), n))
    if check_only:
        print("\n（--check：只查不改）")
        return 0

    print()
    print("=== 执行修复导入 ===")
    t0 = time.perf_counter()
    st = import_into(PKG, base)
    print("完成：%s（用时 %.1f 秒）" % (st, time.perf_counter() - t0))

    print()
    print("=== 修复后 ===")
    stat(db, "现状")
    n = count_by_sid(db, sids)
    print("  导入的 %d 首按 song_id 可查：%d 条" % (len(sids), n))
    ok = (n == 868850)
    print()
    print("结论：%s" % ("✅ 修复完成" if ok else "★ 仍有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
