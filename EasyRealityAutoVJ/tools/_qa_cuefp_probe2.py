# -*- coding: utf-8 -*-
"""只读探针 2：真实指纹库 + 真实码表包 的内容（不改任何东西）。"""
import hashlib
import json
import os
import sqlite3
import sys

APP = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ")
DB = os.path.join(APP, "fingerprints.db")
CFG = os.path.join(APP, "config.json")
PKG = r"Y:\EasyRealityAutoVJ\测试.ercue"


def main():
    # ---- 指纹库 ----
    print("db:", DB, "exists=", os.path.isfile(DB),
          "size=%.1fMB" % (os.path.getsize(DB) / 1e6 if os.path.isfile(DB) else 0))
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    n = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    nsong = conn.execute("SELECT COUNT(DISTINCT song_id) FROM fingerprints").fetchone()[0]
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    print("rows=%d  distinct_song_id=%d  user_version=%d" % (n, nsong, uv))
    ids = [r[0] for r in conn.execute(
        "SELECT song_id, COUNT(*) c FROM fingerprints GROUP BY song_id ORDER BY c DESC LIMIT 8")]
    print("top song_ids:", ids)
    conn.close()

    # ---- 与 music_library 的 song_id 对照 ----
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    lib = cfg.get("music_library") or []
    print("library n=", len(lib))
    for p in lib[:4]:
        sid = int(hashlib.md5(str(p).encode("utf-8")).hexdigest()[:8], 16)
        print("   sid=%-12d %s" % (sid, p))
        print("       exists=", os.path.isfile(p))

    # ---- 码表包 ----
    print("\npkg:", PKG, "exists=", os.path.isfile(PKG))
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src"))
    import cuesheet
    sheet, info = cuesheet.load(PKG, meta_only=True)
    print("sheet title=%r tracks=%d res=%s fv=%d" %
          (sheet.title, len(sheet.tracks), sheet.aspect(), sheet.format_version))
    for t in sheet.tracks:
        i = t.identity
        print("  track_key=%r" % t.track_key)
        print("    title=%r artist=%r dur=%.3f bpm=%.4f" %
              (i.title, i.artist, i.duration_sec, i.bpm))
        print("    fp_id=%r file_md5=%r" % (i.fp_id, i.file_md5))
        print("    meta_id=%r file_name=%r key=%r" %
              (i.meta_id, i.file_name, i.key))
    print("warnings:", info.get("warnings"))
    print("cue cfg sheets:", json.dumps(
        (cfg.get("cue") or {}).get("sheets") or [], ensure_ascii=False)[:1200])


if __name__ == "__main__":
    sys.exit(main())
