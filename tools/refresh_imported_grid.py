"""用**当前**的扫描算法重算「导入包」里各首的网格/段落，写回 `imported_meta.db`。

为什么需要：导入歌的 BPM/段落是 **ERLIB 扫描那一刻**的结果，存在 `imported_meta.db`。
           改了扫描算法（2026-11-01：HOP 1024→512）之后，已导入的歌不会自己更新
           —— 用户看到的就是旧值（`(07) Distorted Dimension` 还写着 132.65）。

只重算**本机能找到文件**的（`src_path` 或 `{根}/{rel_path}`）；找不到的跳过并列出。
写前自动备份 `imported_meta.db`（名带毫秒，不覆盖）。

用法：
    venv\\Scripts\\python.exe -u tools/refresh_imported_grid.py --dry
    venv\\Scripts\\python.exe -u tools/refresh_imported_grid.py
    venv\\Scripts\\python.exe -u tools/refresh_imported_grid.py --only "Distorted Dimension"
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


def main():
    dry = "--dry" in sys.argv
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].lower()

    import config
    import beatgrid as bg
    import sections as sec

    # ⚠ 必须显式指向 **exe 的数据目录**：`config.app_base_dir()` 在源码运时返回项目根，
    #   那里的 `imported_meta.db` 是另一个（空的）库，不是用户真正在用的那个。
    base = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ")
    db = os.path.join(base, "imported_meta.db")
    print("导入库: %s（%.1f MB）" % (db, os.path.getsize(db) / 1e6))
    print("算法：HOP=%d  GRID_VER=%d  margin=%.2f  topn=%d"
          % (bg.HOP, bg.GRID_VER, bg.TEMPO_FIT_ALT_MARGIN, bg.TEMPO_FIT_ALT_TOPN))

    c = sqlite3.connect(db, timeout=30.0)
    cols = {r[1] for r in c.execute("PRAGMA table_info(songs)")}
    want = ["song_id", "file_name", "rel_path", "src_path", "grid", "sections",
            "sections_raw", "grid_ver"]
    sel = ", ".join(want) if all(w in cols for w in want) else \
        ", ".join(w if w in cols else "NULL" for w in want)
    rows = list(c.execute("SELECT %s FROM songs" % sel))
    print("库里 %d 首" % len(rows))

    def find_path(rel, src):
        for p in (src, os.path.join(base, rel or ""), os.path.join(base, os.path.basename(rel or ""))):
            if p and os.path.isfile(p):
                return p
        return ""

    # ★ 导入库**没有** `src_path` 列（导出时按"不带位置"设计）。这些歌的音频文件
    #   在本机的位置只能从**工作库**（ERLIB 扫出来的 `erlib_scan.eravjlib`）按
    #   `song_id` 取 —— 两边是同一批文件、同一个 id 算法。
    work = os.path.join(base, "erlib_scan.eravjlib")
    wmap, libmap = {}, {}
    if os.path.isfile(work):
        try:
            w = sqlite3.connect("file:%s?mode=ro" % work.replace("\\", "/"), uri=True)
            for sid, sp in w.execute("SELECT song_id, src_path FROM songs"):
                if sp:
                    wmap[int(sid)] = sp
            w.close()
            print("工作库提供 %d 条本机路径" % len(wmap))
        except Exception as ex:                                # noqa: BLE001
            print("!! 读工作库失败：%s" % ex)
    try:
        cfg = json.load(open(os.path.join(base, "config.json"), encoding="utf-8"))
        for p in (cfg.get("music_library") or []):
            if os.path.isfile(p):
                libmap[os.path.basename(p).lower()] = p
        print("本机曲库提供 %d 个按文件名兜底" % len(libmap))
    except Exception:                                          # noqa: BLE001
        pass

    todo, missing = [], []
    for sid, name, rel, src, grid, secs, sraw, ver in rows:
        if only and only not in (name or "").lower():
            continue
        p = find_path(rel, src) or wmap.get(int(sid)) or \
            libmap.get(os.path.basename(name or "").lower()) or ""
        if not p and os.path.isfile(wmap.get(int(sid), "")):
            p = wmap[int(sid)]
        if not p:
            missing.append((sid, name))
            continue
        todo.append((int(sid), name, p, json.loads(grid) if grid else {},
                     ver if ver is not None else
                     (json.loads(grid).get("grid_ver") if grid else None)))

    print("可重算 %d 首，缺文件 %d 首" % (len(todo), len(missing)))
    for _sid, name in missing[:8]:
        print("   缺文件:", (name or "")[:56])

    if dry:
        for sid, name, p, g, ver in todo:
            print("   %-46s 旧 %8s (ver %s)" % ((name or "")[:46], g.get("bpm"), ver))
        print("\n(dry-run，不写库)")
        c.close()
        return 0

    # ---- 备份 ----
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % int((time.time() % 1) * 1000)
    bak = "%s.bak-%s" % (db, stamp)
    i = 1
    while os.path.exists(bak):
        bak = "%s.bak-%s-%d" % (db, stamp, i)
        i += 1
    shutil.copy2(db, bak)
    print("\n已备份:", os.path.basename(bak))

    t0 = time.perf_counter()
    changed = kept = failed = 0
    for sid, name, p, old_g, old_ver in todo:
        try:
            mono, sr = bg.load_mono(p, maxsec=bg.GRID_MAXSEC)
            dur = len(mono) / float(sr)
            feats = {}
            g = bg.grid_from_signal(mono, sr, None, path=p, dur=dur,
                                    use_vdj=False, out_feats=feats)
            if not g:
                failed += 1
                print("   !! %-44s 算不出网格" % (name or "")[:44])
                continue
            sg = sgr = None
            f2 = feats.get("f")
            if f2 is not None and g:
                try:
                    sg = sec.auto_segments(
                        f2, dur=dur,
                        grid=sec.grid_from_meta({"grid": g, "grid_manual": {}}) or None)
                    sgr = sec.auto_segments(
                        f2, dur=dur, grid=None,
                        phrase_grid=sec.grid_from_meta(
                            {"grid": g, "grid_manual": {}}) or None)
                except Exception:                              # noqa: BLE001
                    pass
            old_bpm = float(old_g.get("bpm") or 0)
            if abs(float(g["bpm"]) - old_bpm) < 0.01 and old_ver == g.get("grid_ver"):
                kept += 1
                continue
            # ⚠ 导入库**没有** `grid_ver` 列（版本号只存在 `grid` JSON 里）——
            #   早先写成 `SET ..., grid_ver=?` 会让 19 首全部报 `no such column`。
            c.execute("UPDATE songs SET grid=?, sections=?, sections_raw=?"
                      " WHERE song_id=?",
                      (json.dumps(g, ensure_ascii=False),
                       json.dumps(sg, ensure_ascii=False) if sg else "",
                       json.dumps(sgr, ensure_ascii=False) if sgr else "",
                       sid))
            changed += 1
            print("   %-44s %8.2f → %8.2f  (%+.2f%%)"
                  % ((name or "")[:44], old_bpm, g["bpm"],
                     (g["bpm"] - old_bpm) / old_bpm * 100 if old_bpm else 0))
        except Exception as ex:                                # noqa: BLE001
            failed += 1
            print("   !! %-44s %s" % ((name or "")[:44], str(ex)[:60]))
    c.commit()
    c.close()
    print("\n改 %d 首 / 未变 %d 首 / 失败 %d 首，用时 %.0fs"
          % (changed, kept, failed, time.perf_counter() - t0))
    print("备份: %s" % bak)
    return 0


if __name__ == "__main__":
    sys.exit(main())
