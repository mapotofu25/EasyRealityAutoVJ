# -*- coding: utf-8 -*-
"""给**已导入**的歌补上能量段落（`imported_meta.db` 的 `songs.sections`）。

## 为什么需要
2026-10-31 用户：「eravj 的导入怎么没有波形」—— 顺着查出来：导入歌的
`songs.sections` 是**空**的（那份包是"段落功能上线之前"导出的 / 或导入库还是旧结构），
所以曲库波形列没有段落色带、演出时导入歌也不按段落切素材。

数据不用重算：**ERLIB 的工作库**（`erlib_scan.eravjlib`）里就是同一批歌的段落
（`song_id` 两边一致：都是 `md5(完整路径)[:8]` 的十六进制），直接从那边搬过来。

## 安全约束
· 只补 `sections` 为空的行（已有段落的一律不覆盖）；
· 按 `song_id` 精确匹配，匹配不到的跳过（绝不按文件名猜）；
· 默认 `--dry` 只报告；真写前**备份** `imported_meta.db`；
· 不碰指纹 / 封面 / 网格 / 手工标注。

用法：
  venv\\Scripts\\python.exe -u tools/backfill_imported_sections.py --dry
  venv\\Scripts\\python.exe -u tools/backfill_imported_sections.py
"""
import json
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")
IMP = os.path.join(BASE, "imported_meta.db")
WORK = os.path.join(BASE, "erlib_scan.eravjlib")
DRY = "--dry" in sys.argv


def main():
    if not os.path.isfile(IMP):
        print("!! 没有 imported_meta.db（还没导入过任何包）")
        return 2
    if not os.path.isfile(WORK):
        print("!! 没有 erlib 工作库（没扫过曲库，没东西可补）")
        return 2
    print("· 导入库：%s" % IMP)
    print("· 工作库：%s" % WORK)

    w = sqlite3.connect("file:%s?mode=ro" % WORK.replace("\\", "/"), uri=True)
    try:
        src = {}
        for sid, secs in w.execute("SELECT song_id, sections FROM songs"):
            if secs:
                try:
                    v = json.loads(secs)
                except Exception:                                # noqa: BLE001
                    continue
                if isinstance(v, list) and v:
                    src[int(sid)] = (secs, json.dumps(v))
    finally:
        w.close()
    print("· 工作库里有段落的歌：%d 首" % len(src))
    if not src:
        return 2

    c = sqlite3.connect(IMP, timeout=30.0)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(songs)")}
        for _col in ("sections", "sections_raw"):
            if _col not in cols:
                # 旧库：先补列（与 `imported_lib._migrate` 同一套 DDL）
                c.execute("ALTER TABLE songs ADD COLUMN %s TEXT DEFAULT ''" % _col)
                c.commit()
                print("· 旧库缺 %s 列，已补上" % _col)
        rows = list(c.execute(
            "SELECT song_id, COALESCE(sections,'') FROM songs"))
    except Exception as e:                                       # noqa: BLE001
        print("!! 读导入库失败：%s" % e)
        c.close()
        return 1

    todo = [(int(sid), src[int(sid)][0]) for sid, secs in rows
            if not secs and int(sid) in src]
    print("· 导入歌 %d 首，需补段落 %d 首（其余本来就有 / 工作库里也没有）"
          % (len(rows), len(todo)))

    if DRY:
        for sid, secs in todo[:8]:
            print("   [dry] sid=%d 段落 %.1f KB" % (sid, len(secs) / 1024.0))
        print("[dry] 共会补 %d 首；加 --dry 去掉即真写（真写前自动备份）" % len(todo))
        c.close()
        return 0

    if not todo:
        print("· 无需处理")
        c.close()
        return 0

    bak = IMP + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    c.close()
    shutil.copy2(IMP, bak)
    print("· 已备份 → %s（%.2f MB）" % (os.path.basename(bak),
                                       os.path.getsize(bak) / 1e6))

    c = sqlite3.connect(IMP, timeout=30.0)
    n = 0
    try:
        for sid, secs in todo:
            cur = c.execute(
                "UPDATE songs SET sections=?, sections_raw=? "
                "WHERE song_id=? AND COALESCE(sections,'')=''",
                (secs, secs, sid))
            n += max(0, int(cur.rowcount))
        c.commit()
    finally:
        c.close()
    print("· 已补 %d 首的段落" % n)

    chk = sqlite3.connect(IMP, timeout=30.0)
    try:
        got = chk.execute(
            "SELECT COUNT(*) FROM songs WHERE sections IS NOT NULL "
            "AND sections<>''").fetchone()[0]
        tot = chk.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
    finally:
        chk.close()
    print("· 复核：导入库 %d 首，有段落 %d 首" % (tot, got))
    return 0 if got >= n else 1


if __name__ == "__main__":
    sys.exit(main())
