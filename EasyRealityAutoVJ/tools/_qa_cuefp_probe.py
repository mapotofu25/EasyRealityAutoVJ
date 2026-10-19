# -*- coding: utf-8 -*-
"""只读探针：看真实配置里的 music_library / music_meta 结构（不改任何东西）。"""
import json
import os
import sys

CFG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")


def main():
    print("cfg:", CFG, "exists=", os.path.isfile(CFG))
    with open(CFG, encoding="utf-8") as f:
        d = json.load(f)
    print("top keys:", sorted(d.keys()))
    lib = d.get("music_library") or []
    print("music_library n=", len(lib))
    for p in lib[:5]:
        print("   ", p)
    mm = d.get("music_meta") or {}
    print("music_meta n=", len(mm))
    # 统计字段覆盖
    keys = {}
    has_fp = has_md5 = has_song_id = 0
    for p, rec in mm.items():
        if not isinstance(rec, dict):
            continue
        for k in rec:
            keys[k] = keys.get(k, 0) + 1
        if rec.get("fp_id"):
            has_fp += 1
        if rec.get("file_md5"):
            has_md5 += 1
        if rec.get("song_id"):
            has_song_id += 1
    print("music_meta field counts:", json.dumps(keys, ensure_ascii=False, sort_keys=True))
    print("has fp_id=", has_fp, " file_md5=", has_md5, " song_id=", has_song_id)
    # 抽一条完整记录
    for p, rec in mm.items():
        if isinstance(rec, dict) and rec.get("grid"):
            print("sample path:", p)
            print("sample rec keys:", sorted(rec.keys()))
            print("grid:", json.dumps(rec.get("grid"), ensure_ascii=False)[:400])
            print("fp_id:", repr(rec.get("fp_id")), "file_md5:", repr(rec.get("file_md5")),
                  "song_id:", repr(rec.get("song_id")))
            print("title/artist/duration:", rec.get("title"), "|", rec.get("artist"),
                  "|", rec.get("duration"))
            break
    # cue 段
    print("cue cfg:", json.dumps(d.get("cue") or {}, ensure_ascii=False)[:600])


if __name__ == "__main__":
    sys.exit(main())
