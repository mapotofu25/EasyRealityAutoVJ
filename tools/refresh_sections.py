"""用**当前**的分段算法重算已有曲库的 sections（不动 grid / 指纹 / 封面）。

为什么需要：分段算法改了（2026-11-01：边界贴 8 拍线时不再被 16 拍量化拉走），
           但 `sections` 是**扫描那一刻**存的派生数据，不重算不会变 ——
           用户详情页/引擎拿到的还是旧边界（The Core Mind 的 drop3 仍显示 127.39）。

三个数据源（可只跑其中一个）：
    --config     `%LOCALAPPDATA%\\AutoVJ\\config.json` 的 `music_meta[*].sections`
    --erlib      工作库 `erlib_scan.eravjlib` 的 `songs.sections`（ERLIB 详情页读它）
    --imported   导入库 `imported_meta.db` 的 `songs.sections`（AutoVJ 里的 📦 歌）

用法：
    venv\\Scripts\\python.exe -u tools/refresh_sections.py --dry --config
    venv\\Scripts\\python.exe -u tools/refresh_sections.py --all
"""
import os
import sys
import json
import time
import shutil
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

BASE = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")


def _recompute(path, grid, manual=None):
    """只需要 sections 时：解码一次 → 特征 → 两次 auto_segments。

    ⚠ 必须把 `manual`（用户手改的 `bpm_manual`/`anchors`）喂进去：`grid_from_meta`
      会优先用它。只传 `grid` 的话，用户手改过 BPM 的歌会按**旧自动 BPM** 重新分段
      —— 段落边界全错（项目里 `Extra Pain` 就是"手改 99.95→200 后整首只切 2 段"那个坑）。
    """
    import beatgrid as bg
    import sections as sec
    mono, sr = bg.load_mono(path, maxsec=bg.GRID_MAXSEC)
    dur = len(mono) / float(sr)
    f = bg.features(mono, sr)
    g = sec.grid_from_meta({"grid": grid, "grid_manual": manual or {}})
    sg = sec.auto_segments(f, dur=dur, grid=g) or []
    sgr = sec.auto_segments(f, dur=dur, grid=None, phrase_grid=g) or []
    return sg, sgr


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % int((time.time() % 1) * 1000)
    bak = "%s.bak-%s" % (path, stamp)
    i = 1
    while os.path.exists(bak):
        bak = "%s.bak-%s-%d" % (path, stamp, i)
        i += 1
    shutil.copy2(path, bak)
    return bak


def do_config(dry):
    import beatgrid as bg
    p = os.path.join(BASE, "config.json")
    cfg = json.load(open(p, encoding="utf-8"))
    mm = cfg.get("music_meta") or {}
    # ⚠⚠ `sections_auto is False` = **用户手改过段落**（`music_detail.py:908` 写的），
    #   重算会把手动结果冲掉 ⇒ 必须整条跳过。这是"自动产物可重算、人工数据不可覆盖"的分界。
    manual = [k for k, v in mm.items()
              if isinstance(v, dict) and v.get("sections_auto") is False]
    todo = [(k, v) for k, v in mm.items()
            if isinstance(v, dict) and (v.get("grid") or {}).get("beat_len")
            and os.path.isfile(k) and v.get("sections_auto") is not False]
    print("\n[config] %d 首有网格且文件在；跳过**手改过段落**的 %d 首"
          % (len(todo), len(manual)))
    for k in manual[:5]:
        print("   跳过（手改）:", os.path.basename(k)[:50])
    if dry:
        for k, _v in todo[:5]:
            print("   ", os.path.basename(k))
        print("   …（dry-run 不写）")
        return 0
    bak = _backup(p)
    print("   已备份:", os.path.basename(bak))
    t0 = time.perf_counter()
    changed = same = fail = 0
    for k, v in todo:
        try:
            old = v.get("sections") or []
            sg, sgr = _recompute(k, v.get("grid") or {})
            if not sg:
                fail += 1
                continue
            old_t = [(round(x.get("t0", 0), 2), round(x.get("t1", 0), 2)) for x in old]
            new_t = [(round(x["t0"], 2), round(x["t1"], 2)) for x in sg]
            if old_t == new_t:
                same += 1
                continue
            v["sections"] = sg
            v["sections_raw"] = sgr
            changed += 1
            print("   %-46s %d 段 → %d 段" % (os.path.basename(k)[:46], len(old), len(sg)))
        except Exception as ex:                                # noqa: BLE001
            fail += 1
            print("   !! %-44s %s" % (os.path.basename(k)[:44], str(ex)[:60]))
    json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    print("   改 %d 首 / 未变 %d / 失败 %d，%.0fs" % (changed, same, fail,
                                                     time.perf_counter() - t0))
    return changed


def do_sqlite(db_path, table, dry, tag):
    if not os.path.isfile(db_path):
        print("\n[%s] 库不存在，跳过" % tag)
        return 0
    c = sqlite3.connect(db_path, timeout=30.0)
    cols = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}
    if "sections" not in cols:
        print("\n[%s] 没有 sections 列，跳过" % tag)
        c.close()
        return 0
    has_raw = "sections_raw" in cols
    has_man = "manual" in cols
    sel = ("song_id, grid, sections" + (", sections_raw" if has_raw else "")
           + (", manual" if has_man else ""))
    rows = list(c.execute("SELECT %s FROM %s" % (sel, table)))
    print("\n[%s] %d 首（sections_raw 列: %s）" % (tag, len(rows), has_raw))
    # 需要本机路径：工作库直接有 src_path；导入库按 file_name 从 config/music_library 找
    paths = {}
    if "src_path" in cols:
        for sid, sp in c.execute("SELECT song_id, src_path FROM %s" % table):
            if sp and os.path.isfile(sp):
                paths[int(sid)] = sp
    if len(paths) < len(rows):
        # ⚠ 导入库**没有 `src_path` 列**（导出时按"不带位置"设计）⇒ 音频文件只能从
        #   **工作库** `erlib_scan.eravjlib` 按 `song_id` 借（两边是同一批文件、同一个 id 算法）。
        try:
            w = sqlite3.connect("file:%s?mode=ro"
                                % os.path.join(BASE, "erlib_scan.eravjlib").replace("\\", "/"),
                                uri=True)
            for sid, sp in w.execute("SELECT song_id, src_path FROM songs"):
                if sp and os.path.isfile(sp) and int(sid) not in paths:
                    paths[int(sid)] = sp
            w.close()
        except Exception:                                      # noqa: BLE001
            pass
        try:
            lib = json.load(open(os.path.join(BASE, "config.json"),
                                 encoding="utf-8")).get("music_library") or []
            byname = {os.path.basename(x).lower(): x for x in lib if os.path.isfile(x)}
        except Exception:                                      # noqa: BLE001
            byname = {}
        names = {}
        try:
            for sid, n in c.execute("SELECT song_id, file_name FROM %s" % table):
                names[int(sid)] = n
        except Exception:                                      # noqa: BLE001
            pass
        for sid in names:
            if sid not in paths and byname.get(os.path.basename(names[sid] or "").lower()):
                paths[sid] = byname[os.path.basename(names[sid]).lower()]
    print("   能定位到文件 %d 首" % len(paths))
    if dry:
        print("   （dry-run 不写）")
        c.close()
        return 0
    bak = _backup(db_path)
    print("   已备份:", os.path.basename(bak))
    t0 = time.perf_counter()
    changed = same = fail = skip = 0
    for row in rows:
        sid = int(row[0])
        grid = json.loads(row[1]) if row[1] else {}
        old = json.loads(row[2]) if row[2] else []
        man = {}
        if has_man and len(row) > 2:
            try:
                man = json.loads(row[-1]) if row[-1] else {}
            except Exception:                                  # noqa: BLE001
                man = {}
        p = paths.get(sid)
        if not p or not (grid.get("beat_len")):
            skip += 1
            continue
        try:
            sg, sgr = _recompute(p, grid)
            if not sg:
                fail += 1
                continue
            old_t = [(round(x.get("t0", 0), 2), round(x.get("t1", 0), 2)) for x in old]
            new_t = [(round(x["t0"], 2), round(x["t1"], 2)) for x in sg]
            if old_t == new_t:
                same += 1
                continue
            if has_raw:
                c.execute("UPDATE %s SET sections=?, sections_raw=? WHERE song_id=?"
                          % table, (json.dumps(sg, ensure_ascii=False),
                                    json.dumps(sgr, ensure_ascii=False), sid))
            else:
                c.execute("UPDATE %s SET sections=? WHERE song_id=?" % table,
                          (json.dumps(sg, ensure_ascii=False), sid))
            changed += 1
            print("   %-46s %d 段 → %d 段" % (os.path.basename(p)[:46], len(old), len(sg)))
        except Exception as ex:                                # noqa: BLE001
            fail += 1
            print("   !! %-44s %s" % (os.path.basename(p)[:44], str(ex)[:60]))
    c.commit()
    c.close()
    print("   改 %d / 未变 %d / 失败 %d / 定位不到 %d，%.0fs"
          % (changed, same, fail, skip, time.perf_counter() - t0))
    return changed


def main():
    dry = "--dry" in sys.argv
    allf = "--all" in sys.argv
    do_cfg = allf or "--config" in sys.argv
    do_er = allf or "--erlib" in sys.argv
    do_im = allf or "--imported" in sys.argv
    if not (do_cfg or do_er or do_im):
        print(__doc__)
        return 2
    print("算法：GRID_VER=%d  POS_FIRST=%s"
          % (__import__("beatgrid").GRID_VER,
             getattr(__import__("sections"), "PHRASE_ALT_POS_FIRST", "N/A")))
    n = 0
    if do_cfg:
        n += do_config(dry)
    if do_er:
        n += do_sqlite(os.path.join(BASE, "erlib_scan.eravjlib"), "songs", dry, "erlib")
    if do_im:
        n += do_sqlite(os.path.join(BASE, "imported_meta.db"), "songs", dry, "imported")
    print("\n合计改动 %d 处" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
