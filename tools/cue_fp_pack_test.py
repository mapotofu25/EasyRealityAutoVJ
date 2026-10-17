# -*- coding: utf-8 -*-
"""码表包内**完整指纹哈希序列** → 演出端指纹库（`cue_fp_pack`）自检。

用户原话（这条链要解决的问题）：
    「这首歌在 erc 里做的码表包啊，理论来讲 era 导入就应该要识别到 解决一下」

判据（7 项，全部要过；exit 0 = 全过）：
  1. 造包（**手写 zip**：`cuesheet.json` + `fp/<fp_id>.bin`，blob 按两端契约逐字节手拼）
     ⇒ `ingest_sheet()` 后 `SELECT COUNT(*) FROM fingerprints WHERE song_id=?`
       等于写入条数
  2. **最关键**：用同一首歌的 4.5 秒切片（真音频，120s 处）→ `db.match()` ⇒
     `song_id == fp_song_id(fp_id)` 且 offset 误差 ≤ 0.5s
     —— 这一条证明"包里带的指纹真的能认出音频"
  3. `drop_sheet()` ⇒ 该 song_id 的行清零、`cfg["cue"]["fp_songs"]` 映射清空
  4. **去重守卫**：`cfg["music_meta"]` 里已有同 `fp_id` ⇒ `ingest_sheet` 跳过，库里 0 行
  5. `unpack_hashes` 坏数据（截断 / 错 magic / 错 version / count 不符 / 非 zlib /
     短垃圾 / 非 bytes）⇒ 全部 `None`，**绝不抛**；而**空字节流 ⇒ `[]`**
     （"空"= 没导指纹，"坏"= 包坏了，两者必须分得开 —— 与编排端同一语义）
  6. 老包（没有 `fp/` 成员）⇒ `ingest_sheet()` 返回 0 条，不报错
  7. `engine._cue_lookup_song_id` / `_cue_track_query_by_sid` / `_cue_accept_pos`
     三条（FakeCfg + 把引擎里那三个方法**原样**挂到普通类上，测的是正式代码）

⚠ 真实素材：`Y:\\EasyRealityAutoVJ\\码表素材\\揽月 - Dreamira.flac`（+ Supa7onyz 那首）。
  找不到时退回**合成音频**（正弦 + 噪声），报告里会明确标注用的是生成素材。

运行：`venv\\Scripts\\python.exe tools\\cue_fp_pack_test.py`
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import numpy as np                                                   # noqa: E402

import cue_fp_pack                                                   # noqa: E402
import fp as fpvj                                                    # noqa: E402

OK, FAIL = [], []
TMP = tempfile.mkdtemp(prefix="cuefppack_")

#: 真实素材（用户机器上的码表素材目录）
MAT_DIR = r"Y:\EasyRealityAutoVJ\码表素材"
MAT_A = os.path.join(MAT_DIR, "揽月 - Dreamira.flac")
MAT_B = os.path.join(MAT_DIR, "Nimbus Spark 1999 - Supa7onyz.flac")

#: 指纹对**整首**算（与编排端导出时完全一致：真实歌 49462 / 19607 条）。
#: 真实包就是这么造出来的，所以这里也整首算 —— 报告的"灌库耗时"才是现场那个量级。
#: （`_spectrogram` 会为整段建 [frames,4096] float32 矩阵，8 分钟整曲峰值约 400MB。）
FP_SEC = 3600.0
SLICE_AT = 120.0        # 关键断言 ② 的切片位置（秒）
SLICE_WIN = 4.5         # 生产用的识别窗长度（`audio_engine.FP_WIN_SEC`）


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                          ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


# ==========================================================================
# 造包：**完全手写**（不复用 cue_fp_pack 的任何打包代码 —— 那样测的就是自己）
# ==========================================================================
def pack_blob(hashes):
    """按两端契约**逐字节**手拼 `fp/<fp_id>.bin` 的内容（= zlib.compress(payload, 6)）。"""
    body = bytearray()
    n = 0
    for h, off in hashes:
        raw = bytes.fromhex(h)
        assert len(raw) == 10, "哈希必须是 20 hex = 10 字节"
        body += raw + struct.pack("<i", int(off))
        n += 1
    head = bytearray(12)
    head[0:4] = b"ERFP"
    head[4] = 1                                  # version
    head[5] = 20                                 # hash_hex_len
    struct.pack_into("<H", head, 6, 0)           # reserved
    struct.pack_into("<I", head, 8, n)           # count
    payload = bytes(head) + bytes(body)
    assert len(payload) - 12 == n * 14, "载荷长度校验式必须成立"
    return zlib.compress(payload, 6)


def make_pkg(path, tracks, blobs=None):
    """手写一个最小 `.ercue`（zip）。`tracks` = [dict(title,artist,dur,bpm,fp_id,md5,name)]"""
    d = {
        "format_version": 3,
        "generator": {"name": "cue_fp_pack_test"},
        "title": "指纹包自检",
        "notes": "",
        "res_w": 1920, "res_h": 1080,
        "media": [],
        "tracks": [{
            "track_key": "k%d" % i,
            "identity": {
                "fp_id": t["fp_id"], "file_md5": t.get("md5", ""),
                "title": t["title"], "artist": t.get("artist", ""),
                "duration_sec": float(t.get("dur", 0.0)),
                "bpm": float(t.get("bpm", 0.0)),
                "file_name": t.get("name", ""),
            },
            "composition": {"layers": []},
        } for i, t in enumerate(tracks)],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("cuesheet.json", json.dumps(d, ensure_ascii=False))
        for name, blob in (blobs or {}).items():
            z.writestr(name, blob)
    return path


# ==========================================================================
# 音频
# ==========================================================================
def read_mono(path, max_sec=FP_SEC):
    """读前 `max_sec` 秒 → (mono float32, sr, 整曲时长秒)；读不了 ⇒ (None, 0, 0)。"""
    import soundfile as sf
    try:
        with sf.SoundFile(path) as f:
            sr = int(f.samplerate)
            total = len(f) / float(sr) if sr else 0.0
            n = min(len(f), int(max_sec * sr))
            data = f.read(n, dtype="float32", always_2d=True)
    except Exception:                                            # noqa: BLE001
        return None, 0, 0.0
    if not len(data):
        return None, 0, 0.0
    return np.ascontiguousarray(np.mean(data, axis=1), dtype=np.float32), sr, total


def read_slice(path, at_sec, win=SLICE_WIN):
    """从 `at_sec` 处读 `win` 秒 → (mono float32, sr)；读不了 ⇒ (None, 0)。"""
    import soundfile as sf
    try:
        with sf.SoundFile(path) as f:
            sr = int(f.samplerate)
            f.seek(int(at_sec * sr))
            data = f.read(int(win * sr), dtype="float32", always_2d=True)
    except Exception:                                            # noqa: BLE001
        return None, 0
    if not len(data):
        return None, 0
    return np.ascontiguousarray(np.mean(data, axis=1), dtype=np.float32), sr


def synth_song(seed, sec=60.0, sr=44100):
    """**生成素材**（真实素材不在时用）：正弦 + 噪声 + 随机音符包络。

    必须"内容丰富"才出得了足够多的峰：纯正弦只有几根谱线，指纹对不上任何东西。
    """
    rnd = np.random.default_rng(seed)
    n = int(sec * sr)
    t = np.arange(n, dtype=np.float32) / sr
    sig = np.zeros(n, dtype=np.float32)
    for _ in range(60):
        f0 = float(rnd.uniform(110.0, 1200.0))
        a = float(rnd.uniform(0.05, 0.25))
        n0 = int(rnd.uniform(0, sec - 2.0) * sr)
        ln = int(rnd.uniform(0.3, 1.5) * sr)
        env = np.hanning(ln).astype(np.float32)
        seg = a * np.sin(2.0 * np.pi * f0 * t[:ln]) * env
        sig[n0:n0 + ln] += seg.astype(np.float32)
    sig += (rnd.standard_normal(n) * 0.01).astype(np.float32)
    return np.ascontiguousarray(sig, dtype=np.float32), sr


# ==========================================================================
# 最小 Config（**包装对象：没有 .get()**，用 __getitem__ 模拟）
# ==========================================================================
class FakeCfg:
    def __init__(self, data=None):
        self.data = json.loads(json.dumps(data or {}))
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        self.saves += 1


def rows_of(db, song_id):
    return int(db.conn.execute(
        "SELECT COUNT(*) FROM fingerprints WHERE song_id=?", (int(song_id),)).fetchone()[0])


def main():
    real = os.path.isfile(MAT_A)
    print("=" * 78)
    print("素材：%s" % ("真实 FLAC（%s）" % os.path.basename(MAT_A) if real
                      else "⚠ 真实素材不存在 ⇒ 使用**生成素材**（正弦+噪声）"))
    print("=" * 78)

    # ---------------- 准备两首歌的指纹 ----------------
    t0 = time.perf_counter()
    songs = []
    if real:
        for path in (MAT_A, MAT_B if os.path.isfile(MAT_B) else MAT_A):
            mono, sr, total = read_mono(path)
            if mono is None:
                chk(False, "读取素材失败：%s" % path)
                return 1
            hs = fpvj.fingerprint_audio(mono, sr)
            songs.append({
                "path": path, "sr": sr, "dur": total,
                "name": os.path.basename(path),
                "title": os.path.splitext(os.path.basename(path))[0],
                "artist": "素材",
                "hashes": hs,
                "fp_id": fpvj.signature_from_hashes(hs),
                "md5": fpvj.file_md5(path),
            })
    else:
        for i in (11, 22):
            mono, sr = synth_song(i)
            hs = fpvj.fingerprint_audio(mono, sr)
            songs.append({
                "path": "<生成素材#%d>" % i, "sr": sr, "dur": len(mono) / float(sr),
                "name": "synth%d.flac" % i, "title": "生成素材 %d" % i,
                "artist": "synth", "hashes": hs,
                "fp_id": fpvj.signature_from_hashes(hs), "md5": "",
            })
    print("算指纹耗时 %.1fs；两首：%s / %s" % (
        time.perf_counter() - t0,
        "%d 条 %s" % (len(songs[0]["hashes"]), songs[0]["fp_id"]),
        "%d 条 %s" % (len(songs[1]["hashes"]), songs[1]["fp_id"])))
    for s in songs:
        chk(len(s["hashes"]) > 1000 and s["fp_id"].startswith("fp2-"),
            "指纹序列形状合法（%s）" % s["name"],
            "%d 条 / fp_id=%s" % (len(s["hashes"]), s["fp_id"]))

    # ---------------- 造包（手写 zip + 手拼 blob）----------------
    pkg = os.path.join(TMP, "real.ercue")
    blobs = {}
    for s in songs:
        blobs[cue_fp_pack.blob_name(s["fp_id"])] = pack_blob(s["hashes"])
    make_pkg(pkg, [{"fp_id": s["fp_id"], "title": s["title"], "artist": s["artist"],
                    "dur": s["dur"], "bpm": 128.0, "md5": s["md5"], "name": s["name"]}
                   for s in songs], blobs)
    print("码表包：%s（%.1f KB，成员 %s）" % (
        pkg, os.path.getsize(pkg) / 1024.0, ", ".join(sorted(blobs))))

    # ================= ① ingest_sheet 写入条数 =================
    print("\n===== ① ingest_sheet：包里的指纹进库，行数 ===")
    db_path = os.path.join(TMP, "fp.db")
    db = fpvj.FingerprintDB(db_path)
    cfg = FakeCfg({"music_meta": {}, "music_library": [], "cue": {}})
    r = cue_fp_pack.ingest_sheet(cfg, db, pkg)
    print("      ingest_sheet → %r" % (r,))
    chk(int(r.get("tracks", 0)) == len(songs), "写入曲目数 == 2", r.get("tracks"))
    chk(int(r.get("hashes", 0)) == sum(len(s["hashes"]) for s in songs),
        "写入行数 == 两首哈希条数之和",
        "%s vs %s" % (r.get("hashes"), sum(len(s["hashes"]) for s in songs)))
    for s in songs:
        sid = cue_fp_pack.fp_song_id(s["fp_id"])
        chk(rows_of(db, sid) == len(s["hashes"]),
            "库里 song_id=%d 的行数 == 该曲哈希条数" % sid,
            "%d vs %d" % (rows_of(db, sid), len(s["hashes"])))
        rec = (cfg["cue"].get("fp_songs") or {}).get(s["fp_id"]) or {}
        chk(rec.get("song_id") == sid and rec.get("title") == s["title"]
            and rec.get("sheet") == os.path.abspath(pkg),
            "cfg[\"cue\"][\"fp_songs\"] 记下了映射（song_id/title/sheet）", rec)
    chk(cue_fp_pack.blob_name("a/b") == "" and cue_fp_pack.blob_name("") == "",
        "blob_name 拒绝危险 fp_id（防 zip-slip）")

    # ================= ② 真音频切片能认出来（最关键）=================
    print("\n===== ② 同一首歌 4.5s 切片 → db.match()（证明包里的指纹真能认音频）=====")
    s0 = songs[0]
    sid0 = cue_fp_pack.fp_song_id(s0["fp_id"])
    if real:
        seg, seg_sr = read_slice(s0["path"], SLICE_AT)
        at = SLICE_AT
    else:
        # 生成素材：从内存信号里切（同一坐标系：整段指纹的 offset 是 hop 帧）
        mono, seg_sr = synth_song(11)
        at = 20.0
        seg = np.ascontiguousarray(mono[int(at * seg_sr): int(at * seg_sr)
                                        + int(SLICE_WIN * seg_sr)], dtype=np.float32)
    chk(seg is not None and len(seg) > int(3.0 * seg_sr),
        "取到 4.5 秒切片（位置 %.1fs）" % at,
        "%d 采样点 @%dHz" % (len(seg) if seg is not None else 0, seg_sr))
    t1 = time.perf_counter()
    got_sid, votes, got_off = db.match(seg, seg_sr)
    cost = (time.perf_counter() - t1) * 1000.0
    print("      match() → song_id=%s votes=%s offset=%.3fs（耗时 %.1f ms）"
          % (got_sid, votes, got_off, cost))
    chk(got_sid == sid0, "认出的 song_id == fp_song_id(该 fp_id)", "%s vs %s" % (got_sid, sid0))
    chk(abs(got_off - at) <= 0.5, "offset 误差 ≤0.5s",
        "off=%.3f want=%.3f Δ=%.3f" % (got_off, at, got_off - at))
    chk(votes > 0, "对齐票数 > 0", votes)
    print("      ★ 关键断言实测：切片 %.1fs / song_id=%d / Δ=%.3fs / %d 票 / %.1f ms"
          % (at, got_sid, got_off - at, votes, cost))
    # 反向：删掉这首的指纹 ⇒ 认不出（证明命中不是"总有一个答案"）
    db.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid0,))
    db.conn.commit()
    bad_sid, bad_votes, _bo = db.match(seg, seg_sr)
    print("      反向①：只删掉这首 → sid=%s votes=%s（charge_engine.CONFIRM_VOTES=20，"
          "ADMIT_VOTES=4 ⇒ 这个量级是噪声，不会被确认）" % (bad_sid, bad_votes))
    chk(bad_sid != sid0, "反向①：删掉这首的指纹 ⇒ 认不到这首", "sid=%s" % bad_sid)
    chk(bad_votes < 20, "反向①：残余票数远低于确认票数（<20，属噪声）", bad_votes)
    # 反向②：整个库清空 ⇒ 一条都对不上（match 的"没有结果"形态必须是 (-1, 0, 0.0)）
    db.conn.execute("DELETE FROM fingerprints")
    db.conn.commit()
    empty_sid, empty_votes, empty_off = db.match(seg, seg_sr)
    chk((empty_sid, empty_votes, empty_off) == (-1, 0, 0.0),
        "反向②：库清空 ⇒ (-1, 0, 0.0)", (empty_sid, empty_votes, empty_off))
    # 再灌回来，供后面几项使用
    r2 = cue_fp_pack.ingest_sheet(cfg, db, pkg)
    chk(rows_of(db, sid0) == len(s0["hashes"]), "重复 ingest 幂等（先删后插，行数不变）",
        "%d / %s" % (rows_of(db, sid0), r2.get("hashes")))

    # ================= ③ drop_sheet 清库 + 清映射 =================
    print("\n===== ③ drop_sheet：行清零 + 映射清空 =====")
    t2 = time.perf_counter()
    removed = cue_fp_pack.drop_sheet(cfg, db, pkg)
    print("      drop_sheet → 删除 %d 行，耗时 %.1f ms"
          % (removed, (time.perf_counter() - t2) * 1000.0))
    chk(removed == sum(len(s["hashes"]) for s in songs), "返回删除行数 == 灌进去的行数",
        "%s vs %s" % (removed, sum(len(s["hashes"]) for s in songs)))
    chk(all(rows_of(db, cue_fp_pack.fp_song_id(s["fp_id"])) == 0 for s in songs),
        "两首的指纹行都清零")
    chk(not (cfg["cue"].get("fp_songs") or {}), "cfg[\"cue\"][\"fp_songs\"] 已清空",
        cfg["cue"].get("fp_songs"))
    chk(cue_fp_pack.drop_sheet(cfg, db, pkg) == 0, "重复 drop ⇒ 0 行（幂等）")

    # ================= ④ 去重守卫 =================
    print("\n===== ④ 去重守卫：本机曲库已有同 fp_id ⇒ 跳过 =====")
    cfg2 = FakeCfg({"music_meta": {"/local/same.flac": {"fp_id": s0["fp_id"]}},
                    "music_library": ["/local/same.flac"], "cue": {}})
    r4 = cue_fp_pack.ingest_sheet(cfg2, db, pkg)
    print("      ingest_sheet（本机已有第一首）→ %r" % (r4,))
    chk(rows_of(db, sid0) == 0, "本机已有的那首 ⇒ 库里 0 行（不灌第二份指纹）")
    sid1 = cue_fp_pack.fp_song_id(songs[1]["fp_id"])
    chk(rows_of(db, sid1) == len(songs[1]["hashes"]),
        "本机没有的那首照常灌进去", "%d vs %d" % (rows_of(db, sid1),
                                                 len(songs[1]["hashes"])))
    chk(s0["fp_id"] not in ((cfg2["cue"].get("fp_songs") or {})),
        "被跳过的曲目**不写**映射（否则引擎会去找一个库里没指纹的 id）")
    cue_fp_pack.drop_sheet(cfg2, db, pkg)

    # ================= ⑤ 坏数据 ⇒ None，绝不抛 =================
    print("\n===== ⑤ unpack_hashes 坏数据（None，不抛）+ 空字节流（[]）=====")
    good = pack_blob(s0["hashes"][:200])
    raw = bytearray(zlib.decompress(good))
    bad_magic = bytearray(raw)
    bad_magic[0:4] = b"XRPF"
    bad_ver = bytearray(raw)
    bad_ver[4] = 2
    bad_len = bytearray(raw)
    bad_len[5] = 40
    bad_count = bytearray(raw)
    struct.pack_into("<I", bad_count, 8, 201)
    bad_cases = {
        "截断 1 字节": good[:-1],
        "错 magic": zlib.compress(bytes(bad_magic), 6),
        "错 version(2)": zlib.compress(bytes(bad_ver), 6),
        "错 hash_hex_len(40)": zlib.compress(bytes(bad_len), 6),
        "count 不符": zlib.compress(bytes(bad_count), 6),
        "非 zlib 垃圾": b"\x00\x01\x02not-a-zlib-stream",
        "纯随机": bytes(range(64)),
        "短垃圾 b'junk'": b"junk",
        "None": None,
        "非 bytes（str）": "not-bytes",
    }
    for label, bad in bad_cases.items():
        try:
            got = cue_fp_pack.unpack_hashes(bad)
            chk(got is None, "坏数据 %s ⇒ None" % label,
                "实际 %r" % (None if got is None else len(got)))
        except Exception as e:                                   # noqa: BLE001
            chk(False, "坏数据 %s 抛异常了" % label, "%s: %s" % (type(e).__name__, e))
    # ★ 空字节流 = "没有指纹"，**不是** None（与编排端 `fp_pack.unpack_hashes` 同一语义）。
    #   "空"和"坏"要能区分：前者是"这首没导指纹"，后者是"包坏了、该提示重导"。
    chk(cue_fp_pack.unpack_hashes(b"") == [], "空字节流 ⇒ []（不是 None）",
        repr(cue_fp_pack.unpack_hashes(b"")))
    chk(cue_fp_pack.unpack_hashes(b"junk") is None, "坏数据 b'junk' ⇒ None（与空区分开）")
    chk(cue_fp_pack.unpack_hashes(bytearray(b"")) == [], "bytearray(b'') 也 ⇒ []")
    # 正向：手拼的 blob 能被解开，且逐条相等（含负数 offset）
    hs200 = [(h, int(o)) for h, o in s0["hashes"][:200]]
    hs200[0] = (hs200[0][0], -3)
    chk(cue_fp_pack.unpack_hashes(pack_blob(hs200)) == hs200,
        "正向往返：手拼 blob → unpack 逐条相等（含负 offset）")
    chk(cue_fp_pack.read_from_package(pkg, "fp2-nosuch") is None,
        "read_from_package：成员不存在 ⇒ None")
    # 成员存在但内容为空 ⇒ []（与"成员不存在"区分开）
    empty_pkg = os.path.join(TMP, "empty.ercue")
    make_pkg(empty_pkg, [{"fp_id": "fp2-empty", "title": "E", "artist": "",
                          "dur": 1.0, "bpm": 0.0, "md5": "", "name": "e.flac"}],
             {"fp/fp2-empty.bin": b""})
    chk(cue_fp_pack.read_from_package(empty_pkg, "fp2-empty") == [],
        "read_from_package：空成员 ⇒ []（不是 None）")
    chk(int(cue_fp_pack.ingest_sheet(cfg, db, empty_pkg).get("hashes", 0)) == 0,
        "空成员的曲目 ⇒ 灌 0 条（不写库、不写映射）")

    # ================= ⑥ 老包（没有 fp/ 成员）=================
    print("\n===== ⑥ 老包（没有 fp/ 成员）⇒ 0 条，不报错 =====")
    old = os.path.join(TMP, "old.ercue")
    make_pkg(old, [{"fp_id": s0["fp_id"], "title": s0["title"], "artist": "",
                    "dur": s0["dur"], "bpm": 128.0, "md5": "", "name": s0["name"]}])
    cfg6 = FakeCfg({"music_meta": {}, "music_library": [], "cue": {}})
    try:
        r6 = cue_fp_pack.ingest_sheet(cfg6, db, old)
        chk(int(r6.get("tracks", 0)) == 0 and int(r6.get("hashes", 0)) == 0,
            "老包 ⇒ tracks=0 / hashes=0", r6)
        chk(rows_of(db, sid0) == 0, "老包不往库里写任何行")
        chk(not (cfg6["cue"].get("fp_songs") or {}), "老包不写映射")
    except Exception as e:                                       # noqa: BLE001
        chk(False, "老包抛异常了", "%s: %s" % (type(e).__name__, e))
    # 不是 zip / 文件不存在 ⇒ 也返回 0
    broke = os.path.join(TMP, "broke.ercue")
    with open(broke, "wb") as f:
        f.write(b"not a zip at all")
    chk(int(cue_fp_pack.ingest_sheet(cfg6, db, broke).get("tracks", 0)) == 0,
        "坏包 ⇒ 0 条（不抛）")
    chk(int(cue_fp_pack.ingest_sheet(cfg6, db, os.path.join(TMP, "nope.ercue"))
            .get("tracks", 0)) == 0, "文件不存在 ⇒ 0 条（不抛）")

    # ================= ⑦ 引擎三条方法 =================
    print("\n===== ⑦ engine：合成 id 能被反查/绑定/护栏 =====")
    import cuesheet as cs
    import engine as eng_mod
    sid = cue_fp_pack.fp_song_id(s0["fp_id"])
    fp_songs = {s0["fp_id"]: {"song_id": sid, "title": s0["title"],
                              "artist": s0["artist"], "duration": s0["dur"],
                              "sheet": os.path.abspath(pkg)}}
    fcfg = FakeCfg({"music_meta": {}, "music_library": [], "cue": {"fp_songs": fp_songs}})
    E = type("FakeEngine", (), {
        "_cue_fp_songs": eng_mod.AutoVJEngine._cue_fp_songs,
        "_cue_lookup_song_id": eng_mod.AutoVJEngine._cue_lookup_song_id,
        "_cue_track_query_by_sid": eng_mod.AutoVJEngine._cue_track_query_by_sid,
        "_cue_bound_song_id": eng_mod.AutoVJEngine._cue_bound_song_id,
        "_cue_accept_pos": eng_mod.AutoVJEngine._cue_accept_pos,
        "_cue_bad_song_log": eng_mod.AutoVJEngine._cue_bad_song_log,
        "_cue_song_id_of": staticmethod(eng_mod.AutoVJEngine._cue_song_id_of),
        "_cue_health": lambda self, msg: None,
    })
    e = E()
    e.cfg = fcfg
    e._cue_bound_cache = None
    e._cue_bad_song_n = 0
    e._cue_bad_song_t = 0.0
    tr = cs.TrackEntry(identity=cs.Identity(title=s0["title"], artist=s0["artist"],
                                            duration_sec=s0["dur"], bpm=128.0,
                                            file_name="不存在于本机.flac"))
    tr.identity.fp_id = s0["fp_id"]
    e._cue_track = tr

    got = e._cue_lookup_song_id(tr)
    chk(got == sid, "_cue_lookup_song_id：fp_id 在 fp_songs 里 ⇒ 合成 song_id",
        "%s vs %s" % (got, sid))
    chk(e._cue_bound_song_id() == sid, "_cue_bound_song_id 跟着返回合成 id（护栏自动生效）")
    q = e._cue_track_query_by_sid(sid)
    print("      _cue_track_query_by_sid(%d) → %r" % (sid, q))
    chk(q.get("fp_id") == s0["fp_id"] and q.get("title") == s0["title"]
        and q.get("file_name") == "" and abs(float(q.get("duration_sec") or 0.0)
                                             - float(s0["dur"])) < 1e-6,
        "_cue_track_query_by_sid：反查得到同一套查询键（file_name 恒空）", q)
    chk(e._cue_track_query_by_sid(sid + 1) == {}, "非合成 id ⇒ {}")
    chk(e._cue_accept_pos(sid, 12.5) == 12.5, "护栏：认到的就是本表绑定那首 ⇒ 照跟")
    chk(e._cue_accept_pos(sid + 1, 12.5) is None,
        "护栏：认到**另一首**（合成 id 生效后）⇒ 本帧不接管")
    chk(e._cue_accept_pos(-1, 12.5) == 12.5, "护栏：没识别到（sid=-1）⇒ 照跟（旧行为不变）")
    # 老路径逐值不变：没有 fp_songs 时行为 == 改动前（返回 None ⇒ 不启用护栏）
    e2 = E()
    e2.cfg = FakeCfg({"music_meta": {}, "music_library": [],
                      "cue": {"fp_songs": {}}})
    e2._cue_bound_cache = None
    e2._cue_bad_song_n = 0
    e2._cue_bad_song_t = 0.0
    e2._cue_track = tr
    chk(e2._cue_lookup_song_id(tr) is None, "没有 fp_songs 且名/艺人-md5 都对不上 ⇒ None（旧行为）")
    chk(e2._cue_accept_pos(12345, 3.0) == 3.0, "绑定未知 ⇒ 不启用护栏（照跟，旧行为）")
    chk(e2._cue_track_query_by_sid(sid) == {}, "没有 fp_songs ⇒ 反查为空")

    # ================= 耗时汇总 =================
    print("\n" + "=" * 78)
    print("ingest_sheet 耗时（2 首 ≈ %d 条）：%.0f ms"
          % (sum(len(s["hashes"]) for s in songs), float(r.get("ms", 0.0))))
    db.close()
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("CUE_FP_PACK_FAIL")
        return 1
    print("CUE_FP_PACK_OK")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
