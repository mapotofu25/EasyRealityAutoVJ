# -*- coding: utf-8 -*-
"""lead 亲自复核：真数据端到端验证「码表靠指纹认歌」这条链。

三组验证（全部用**真实**曲库/真实音频/真实指纹库，只用 temp 副本，不动用户数据）：
  V1 实时指纹认歌：真歌切片 → fp.FingerprintDB.match() → song_id 正确 + offset 误差 ≤0.5s
     反向：谎报采样率 / 变速 8% ⇒ 认不出（否则说明是假阳性）
  V2 码表 ↔ 曲库 真值比对：真 fp_id + 真 file_md5 造最小 .ercue
     ⇒ md5 级 → fingerprint 级 → title+artist 级 逐级降级正确
     反向：fp_id 填成 song_id 十进制串 ⇒ 不再命中 fingerprint
  V3 engine._cue_track_query() 真数据（FakeCfg 用 __getitem__）

⚠ 只读用户配置与指纹库（指纹库先复制到 temp 再开，避免和正在运行的 exe 抢 WAL）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np
import soundfile as sf

import cuesheet as cs
import fp as fpvj

OK, FAIL = [], []
TMP = tempfile.mkdtemp(prefix="leadcuefp_")


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                           ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


def user_cfg():
    p = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f), p


def song_id_of(path):
    return int(hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8], 16)


def decode_mono(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return np.ascontiguousarray(np.mean(data, axis=1), dtype=np.float32), int(sr)


def main():
    cfg, cfg_path = user_cfg()
    lib = list(cfg.get("music_library") or [])
    meta = cfg.get("music_meta") or {}
    print("真实曲库 %d 首 / music_meta %d 条（只读 %s）" % (len(lib), len(meta), cfg_path))

    # ---- 挑 3 首：2 首 44.1k + 1 首 48k ----
    picked, got48 = [], False
    for p in lib:
        if not os.path.isfile(p):
            continue
        try:
            sr = sf.info(p).samplerate
        except Exception:
            continue
        if sr != 44100 and not got48:
            picked.append(p); got48 = True
        elif sr == 44100 and len([q for q in picked if sf.info(q).samplerate == 44100]) < 2:
            picked.append(p)
        if len(picked) >= 3 and got48:
            break
    print("选中：\n  " + "\n  ".join("%s  (%d Hz)" % (os.path.basename(q), sf.info(q).samplerate)
                                    for q in picked))

    # ---- 指纹库副本（只读用，avoid WAL 争用）----
    db_src = os.path.join(os.environ["LOCALAPPDATA"], "AutoVJ", "fingerprints.db")
    db_tmp = os.path.join(TMP, "fp_copy.db")
    t0 = time.perf_counter()
    shutil.copyfile(db_src, db_tmp)
    print("指纹库副本 %.1f MB，复制 %.2fs" % (os.path.getsize(db_tmp) / 1e6,
                                              time.perf_counter() - t0))
    db = fpvj.FingerprintDB(db_tmp)
    try:
        n_rows = db.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        n_songs = db.conn.execute("SELECT COUNT(DISTINCT song_id) FROM fingerprints").fetchone()[0]
        print("指纹库：%d 行 / %d 首" % (n_rows, n_songs))
    except Exception as e:                                        # noqa: BLE001
        print("查指纹库失败：%s" % e)

    # ================= V1 =================
    print("\n===== V1 实时指纹认歌（真歌切片 → 真库）=====")
    costs = []
    for p in picked:
        sid_want = song_id_of(p)
        mono, sr = decode_mono(p)
        dur = len(mono) / float(sr)
        for frac in (0.35, 0.62):
            at = round(dur * frac, 1)
            seg = mono[int(at * sr): int(at * sr) + int(4.5 * sr)]
            if len(seg) < int(3.0 * sr):
                continue
            t = time.perf_counter()
            sid, votes, off = db.match(seg, sr)
            dt = (time.perf_counter() - t) * 1000.0
            costs.append(dt)
            tag = "%s @%.1fs" % (os.path.basename(p)[:34], at)
            chk(sid == sid_want, "V1 song_id 命中  %s" % tag,
                "sid=%s want=%s votes=%s" % (sid, sid_want, votes))
            chk(abs(off - at) <= 0.5, "V1 offset 误差 ≤0.5s  %s" % tag,
                "off=%.3f want=%.3f Δ=%.3f" % (off, at, off - at))
    # 反向①：谎报采样率
    p0 = picked[0]
    mono0, sr0 = decode_mono(p0)
    at0 = round(len(mono0) / sr0 * 0.5, 1)
    seg0 = mono0[int(at0 * sr0): int(at0 * sr0) + int(4.5 * sr0)]
    sid_wrong, _v, _o = db.match(seg0, 22050)
    chk(sid_wrong != song_id_of(p0), "V1 反向：谎报 22050Hz ⇒ 认不出",
        "sid=%s（正确应为 %s 才叫假阳性）" % (sid_wrong, song_id_of(p0)))
    # 反向②：变速 8%
    idx = np.linspace(0, len(seg0) - 1, max(1, int(len(seg0) * 1.08)))
    fast = np.interp(idx, np.arange(len(seg0)), seg0).astype(np.float32)
    sid_fast, _v2, _o2 = db.match(fast, sr0)
    chk(sid_fast != song_id_of(p0), "V1 反向：加速 8% ⇒ 认不出", "sid=%s" % sid_fast)
    print("  → match() 耗时 p50≈%.1fms / max≈%.1fms（生产 0.5s 一次）"
          % (float(np.median(costs)), float(max(costs))))

    # ================= V2 =================
    print("\n===== V2 码表 ↔ 曲库 真值比对（md5 / 指纹 / 名+艺人）=====")
    recs = []
    for p in picked:
        mono, sr = decode_mono(p)
        fp_id = fpvj.signature_from_hashes(fpvj.fingerprint_audio(mono, sr))
        fmd5 = fpvj.file_md5(p)
        m = meta.get(p) or {}
        bpm = float((m.get("grid") or {}).get("bpm") or 0.0)
        recs.append(dict(path=p, fp_id=fp_id, file_md5=fmd5, bpm=bpm,
                         title=str(m.get("title") or ""),
                         artist=str(m.get("artist") or ""),
                         dur=float((m.get("grid") or {}).get("dur") or 0.0),
                         name=os.path.basename(p), sid=song_id_of(p)))
        print("  %-40s fp_id=%s  md5=%s…  bpm=%.2f" % (
            os.path.basename(p)[:40], fp_id, fmd5[:12], bpm))
        chk(bool(fp_id) and fp_id.startswith("fp2-") and len(fp_id) == 20,
            "V2 fp_id 形状合法（fp2-+16hex）  %s" % os.path.basename(p)[:30], fp_id)
        chk(len(fmd5) == 32 and fmd5 != str(recs[-1]["sid"]).rjust(32, "0"),
            "V2 file_md5 是 32hex 且不是 song_id 填充", fmd5[:16] + "…")

    sheet = cs.new_sheet("lead 自检")
    for r in recs:
        te = cs.new_track(title=r["title"] or r["name"], artist=r["artist"],
                          duration_sec=r["dur"], bpm=r["bpm"])
        te.identity.fp_id = r["fp_id"]
        te.identity.file_md5 = r["file_md5"]
        te.identity.file_name = r["name"]
        te.identity.meta_id = cs.Identity.meta_key(te.identity.title,
                                                   te.identity.artist, r["dur"])
        sheet.tracks.append(te)
    pkg = os.path.join(TMP, "lead_check.ercue")
    cs.save(sheet, pkg)
    loaded, _info = cs.load(pkg)
    chk(len(loaded.tracks) == len(recs), "V2 .ercue 往返：曲目数一致", len(loaded.tracks))

    r0 = recs[0]
    q = dict(title=r0["title"], artist=r0["artist"], duration_sec=r0["dur"],
             file_name=r0["name"])
    tr, lvl = loaded.match_track(fp_id=r0["fp_id"], file_md5=r0["file_md5"], **q)
    chk(lvl == "md5", "V2 ⓪ md5 级优先命中", lvl)
    tr, lvl = loaded.match_track(fp_id=r0["fp_id"], file_md5="", **q)
    chk(lvl == "fingerprint", "V2 ① 指纹级（md5 缺失时）命中", lvl)
    tr, lvl = loaded.match_track(fp_id="", file_md5="", **q)
    chk(lvl in ("meta", "title+artist", "title+artist?"), "V2 降级到名/艺人级", lvl)
    # 反向：fp_id 填 song_id 十进制串
    tr, lvl2 = loaded.match_track(fp_id=str(r0["sid"]), file_md5="", **q)
    chk(lvl2 != "fingerprint", "V2 反向：fp_id 写成 song_id 十进制串 ⇒ 不命中指纹级",
        "level=%s" % lvl2)

    # ================= V3 =================
    print("\n===== V3 engine._cue_track_query()（真数据 + FakeCfg）=====")

    class FakeCfg:
        """Config 是包装对象（**没有 .get()**）⇒ 用 __getitem__ 模拟。"""

        def __init__(self, d):
            self._d = d

        def __getitem__(self, k):
            return self._d[k]

    meta_v = dict(meta)
    m0 = dict(meta_v.get(r0["path"]) or {})
    m0["fp_id"] = r0["fp_id"]
    m0["file_md5"] = r0["file_md5"]
    meta_v[r0["path"]] = m0
    fcfg = FakeCfg({"music_meta": meta_v, "music_library": lib, "cue": {}})

    import engine as eng_mod
    fake_eng = type("E", (), {})()
    fake_eng.cfg = fcfg
    q2 = eng_mod.AutoVJEngine._cue_track_query(fake_eng, r0["path"])
    chk(q2.get("fp_id") == r0["fp_id"], "V3 fp_id 从 meta 正确取出", q2.get("fp_id"))
    chk(q2.get("file_md5") == r0["file_md5"], "V3 file_md5 从 meta 正确取出",
        (q2.get("file_md5") or "")[:16] + "…")
    chk(str(q2.get("file_md5")) != str(r0["sid"]), "V3 file_md5 没有被 song_id 顶替",
        "%s vs %s" % (q2.get("file_md5"), r0["sid"]))

    # meta 只有 song_id 时，两个字段必须都是空串（不是 song_id 的值）
    m1 = dict(m0)
    m1.pop("fp_id"); m1.pop("file_md5")
    meta_v[r0["path"]] = m1
    q3 = eng_mod.AutoVJEngine._cue_track_query(fake_eng, r0["path"])
    chk(q3.get("fp_id") == "" and q3.get("file_md5") == "",
        "V3 meta 只有 song_id ⇒ fp_id/file_md5 都是空串（反向复现）",
        "fp_id=%r file_md5=%r" % (q3.get("fp_id"), q3.get("file_md5")))

    # 端到端：CueIndex 命中级别
    import cue_index
    idx = cue_index.CueIndex()
    idx.add(pkg)
    hits = idx.match(title=r0["title"], artist=r0["artist"], duration_sec=r0["dur"],
                     fp_id=r0["fp_id"], file_name=r0["name"], file_md5=r0["file_md5"])
    chk(bool(hits) and hits[0][1] == "md5", "V3 CueIndex.match 端到端 == md5",
        hits[0][1] if hits else "无命中")

    db.close()
    print("\n" + "=" * 78)
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("RESULT：LEAD_CUEFP_FAIL")
        return 1
    print("RESULT：LEAD_CUEFP_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
