# -*- coding: utf-8 -*-
"""把一个 `.eravjlib` 收拾成**单文件**（删掉 `-wal` / `-shm` 附属文件）。

## 为什么要这个
用户 2026-10-31：「怎么有三个文件」—— 导出目录里除了 `我的曲库测试.eravjlib`，
还有 `我的曲库测试.eravjlib-shm`、`我的曲库测试.eravjlib-wal`。

它们不是"三份曲库"，是 **SQLite 的 WAL 附属文件**。根因：`LibWriter` 建库用
`PRAGMA journal_mode=WAL`（写入快、工作库需要），而这个设置**写在库文件头里**；
之后任何程序（包括 AutoVJ 用 `mode=ro` 只读导入）一碰这个包，SQLite 就会把
`-shm`/`-wal` 重新创建出来，看起来就像"凭空多出两个文件"。

## 这个工具做什么
1. 以**读写**方式打开包 → `wal_checkpoint(TRUNCATE)` 把 WAL 全并回主文件；
2. `PRAGMA journal_mode=DELETE` 把库切回**回滚日志模式**（库里不再有 WAL 标记）；
3. 关掉连接后删掉残留的 `-wal`/`-shm`；
4. 复核：歌数/指纹数不变、`journal_mode` 已是 delete、目录里只剩一个文件。

⚠ 新版 erlib 导出的包**天生就是单文件**（`transfer.export_subset` 已加这两步），
  这个工具只是给**以前导出的旧包**做一次修复。

用法：
  venv\\Scripts\\python.exe -u tools/pkg_single_file.py "<曲库包路径>.eravjlib"
  venv\\Scripts\\python.exe -u tools/pkg_single_file.py "…" --dry     # 只看不改
"""
import os
import shutil
import sqlite3
import sys


def _counts(path):
    c = sqlite3.connect(path, timeout=30.0)
    try:
        have = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out = {"journal": str(c.execute("PRAGMA journal_mode").fetchone()[0])}
        for t in ("songs", "fingerprints", "covers", "waveforms"):
            # ⚠ 只数**存在**的表：老包/半成品包可能没有 covers（第一次就栽在
            #   `no such table: covers` 上，而且是在"已合并完"之后才崩，白跑一趟）
            out[t] = (c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                      if t in have else -1)
        return out
    finally:
        c.close()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    if not args:
        print("用法：pkg_single_file.py <包路径> [--dry]")
        return 2
    pkg = args[0]
    if not os.path.isfile(pkg):
        print("!! 找不到文件：%s" % pkg)
        return 2
    side = [pkg + s for s in ("-wal", "-shm") if os.path.isfile(pkg + s)]
    print("· 包：%s（%.1f MB）" % (pkg, os.path.getsize(pkg) / 1e6))
    print("· 附属文件：%s" % ([os.path.basename(x) for x in side] or "无"))
    before = _counts(pkg)
    print("· 处理前：%s" % before)
    if not side and before["journal"].lower() != "wal":
        print("· 已经是单文件，无需处理。")
        return 0
    if dry:
        print("· [dry] 会做：checkpoint → journal_mode=DELETE → 删除附属文件")
        return 0

    # 备份（包不大，留一份更稳；确认没问题后可自行删除）
    bak = pkg + ".bak"
    if not os.path.isfile(bak):
        try:
            shutil.copy2(pkg, bak)
            print("· 已备份 → %s" % os.path.basename(bak))
        except Exception as e:                                   # noqa: BLE001
            print("!! 备份失败，中止（不冒险）：%s" % e)
            return 1

    c = sqlite3.connect(pkg, timeout=30.0)
    try:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        print("· integrity_check：%s" % ok)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        jm = c.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        print("· journal_mode → %s" % jm)
        c.commit()
    finally:
        c.close()

    for p in side:
        try:
            # ⚠ 必须在**删除那一刻**再判断：`PRAGMA journal_mode=DELETE` 成功时
            #   SQLite 自己就把 -wal/-shm 删掉了，这时再 remove 会报
            #   「系统找不到指定的文件」，看着像失败（实际是好事）。
            if os.path.isfile(p):
                os.remove(p)
                print("· 已删除 %s" % os.path.basename(p))
            else:
                print("· %s 已被数据库自己清理（正常）" % os.path.basename(p))
        except Exception as e:                                   # noqa: BLE001
            print("!! 删除 %s 失败：%s" % (os.path.basename(p), e))

    after = _counts(pkg)
    print("· 处理后：%s" % after)
    left = [os.path.basename(pkg + s) for s in ("-wal", "-shm")
            if os.path.isfile(pkg + s)]
    same = (before["songs"] == after["songs"]
            and before["fingerprints"] == after["fingerprints"])
    print("· 目录里剩余附属文件：%s" % (left or "无（单文件 ✓）"))
    if not same or left or after["journal"].lower() == "wal":
        print("!! 结果不符合预期，请用 %s 回滚" % os.path.basename(bak))
        return 1
    print("· 完成：数据条数一致，包已变成单文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
