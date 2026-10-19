# -*- coding: utf-8 -*-
"""码表全链一致性审计：ERC 导出 → 包格式 → ERA 导入 → 配置 → 运行时输入。

每一项都打印 PASS/FAIL + 实测值，用来回答"到底是哪一端的问题"。
"""
import os, sys, json, shutil, sqlite3, tempfile, zipfile, hashlib
sys.path.insert(0, "src")
import numpy as np
import fp as fpvj
import cue_fp_pack as C

PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
CFG = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
DB  = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
ok = fail = 0

def chk(cond, desc, detail=""):
    global ok, fail
    (ok.__class__, )  # noop
    if cond: ok += 1
    else: fail += 1
    print("  [%s] %-46s %s" % ("ok  " if cond else "FAIL", desc, detail))

cfg = json.load(open(CFG, encoding="utf-8"))
print("=== ① ERC 导出物 ===")
chk(os.path.isfile(PKG), "包存在", "%.0f MB" % (os.path.getsize(PKG)/1e6))
with zipfile.ZipFile(PKG) as z:
    names = z.namelist()
    d = json.loads(z.read("cuesheet.json").decode("utf-8"))
    members = [n for n in names if n.startswith("fp/")]
    thumbs = [n for n in names if n.startswith("thumbs/")]
chk(len(members) == len(d.get("tracks") or []), "fp/ 成员数 == 曲目数", "%d / %d" % (len(members), len(d.get("tracks") or [])))
chk(all(n == "fp/%s.bin" % (t.get("identity") or {}).get("fp_id") for n, t in zip(sorted(members), sorted(d["tracks"], key=lambda x: x["identity"]["fp_id"]))),
    "成员名 == fp/<identity.fp_id>.bin")
chk(len(thumbs) > 0, "thumbs/ 有缩略图", len(thumbs))
chk(d.get("min_app_version", "") == "", "min_app_version 未被误写成本端构建号", repr(d.get("min_app_version")))
bpm0 = [t["identity"]["title"] for t in d["tracks"] if not (t["identity"].get("bpm") or 0) > 0]
chk(True, "曲目 BPM", "未填：%s" % (bpm0 or "无"))

print("\n=== ② 包格式（与演出端 cue_fp_pack 同一契约）===")
for n in members:
    with zipfile.ZipFile(PKG) as z:
        blob = z.read(n)
    hs = C.unpack_hashes(blob)
    fp_id = os.path.basename(n)[:-4]
    chk(hs is not None and len(hs) > 1000, "解包 %s" % fp_id, "%d 条 / %d 字节" % (len(hs or []), len(blob)))
    chk(all(len(h) == 20 for h, _ in (hs or [])[:50]), "hash 长度 20")

print("\n=== ③ ERA 导入（指纹库 + 映射）===")
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
total = c.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
chk(total > 2_800_000, "指纹库总量", total)
fps = (cfg.get("cue") or {}).get("fp_songs") or {}
chk(len(fps) >= 1, "cfg cue.fp_songs 有映射", list(fps.keys()))
for fp_id, rec in fps.items():
    sid = C.fp_song_id(fp_id)
    n = c.execute("SELECT COUNT(*) FROM fingerprints WHERE song_id=?", (sid,)).fetchone()[0]
    chk(n > 1000, "合成 id %s 已灌库" % fp_id, "%d 行（映射 sid=%s）" % (n, rec.get("song_id")))
chk(all(r.get("song_id") == C.fp_song_id(k) for k, r in fps.items()), "映射 sid 与 fp_song_id 一致")

print("\n=== ④ 配置一致性 ===")
sheets = (cfg.get("cue") or {}).get("sheets") or []
chk(any(os.path.abspath(e.get("path","")) == os.path.abspath(PKG) for e in sheets), "包已在 cue.sheets 索引里", len(sheets))
meta = cfg.get("music_meta") or {}
n_fp = sum(1 for m in meta.values() if isinstance(m, dict) and m.get("fp_id"))
n_md5 = sum(1 for m in meta.values() if isinstance(m, dict) and m.get("file_md5"))
chk(n_fp == len(meta), "曲库全部有 fp_id", "%d / %d" % (n_fp, len(meta)))
chk(n_md5 == len(meta), "曲库全部有 file_md5", "%d / %d" % (n_md5, len(meta)))
dup = [p for p, m in meta.items() if isinstance(m, dict) and m.get("fp_id") in fps]
chk(not dup, "本机曲库与码表指纹无撞车（否则票数会被拆）", dup[:2])

print("\n=== ⑤ 运行时输入（真音频 vs 库）===")
flac = r"Y:/EasyRealityAutoVJ/码表素材/揽月 - Dreamira.flac"
if os.path.isfile(flac):
    import soundfile as sf
    data, sr = sf.read(flac, dtype="float32", always_2d=True)
    mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
    tmp = os.path.join(tempfile.mkdtemp(), "fp.db"); shutil.copyfile(DB, tmp)
    db = fpvj.FingerprintDB(tmp)
    sid_want = C.fp_song_id("fp2-617615efb8f43911")
    for at in (60.0, 120.0, 300.0):
        seg = mono[int(at*sr): int(at*sr)+int(4.5*sr)]
        sid, votes, off = db.match(seg, sr)
        chk(sid == sid_want and abs(off-at) <= 0.5, "@%.0fs" % at, "sid=%s 票=%d Δ=%+.3f" % (sid, votes, off-at))
    db.close()
print("\n通过 %d / 失败 %d" % (ok, fail))
