# -*- coding: utf-8 -*-
"""回归：**码表里的指纹（`identity.fp_id`）真的被演出端用上了**（零 Qt、无头可跑）。

════════════════════════════════════════════════════════════════════════
背景（本脚本要守住的四件事）
════════════════════════════════════════════════════════════════════════
1. `CueSheet.match_track()` 的 ⓪ `md5` / ① `fingerprint` 两级**只有在调用方传对了
   字段**时才会命中。以前 `engine._cue_track_query()` 读的是 `meta["md5"]`，
   而 `cfg["music_meta"]` 里从来没有这个键 ⇒ 两级**恒跳过**，全靠最容易误判的
   "标题+艺人 / 文件名"在撑。脚本第 ①/② 项就是这条链的端到端断言 + 反向复现。
2. **三个 md5 不是一回事**（混用不报错、只静默失效）：
     · `song_id`   = `int(md5(路径字符串)[:8],16)`（8 hex 整数）→ 指纹库主键
     · `file_md5`  = **文件字节**的 md5（32 hex）          → 码表 ⓪ 级
     · `fp_id`     = 指纹签名 `fp2-<16hex>`                → 码表 ① 级
   第 ④ 项专门断言：meta 只有 `song_id` 时，查询参数里 `file_md5` / `fp_id`
   **必须是空串**（不是 `song_id` 的值）。
3. `FingerprintDB.add_song(..., hashes=, want_sig=)`：扫描流程要同时拿"入库行"和
   "签名"，不能对整首歌做两遍全曲 STFT。第 ⑥ 项断言复用 `hashes` 时**不再算指纹**，
   且**旧调用方（不传 `want_sig`）拿到的仍是 `int`**。
4. `_cue_beat_len_for()` 的拍长来源：`_cue_track`（`match_track` 认出来的那条）
   优先，不再靠 basename 猜；卸表后必须复位（否则上一首的 bpm 污染下一首）。

夹具全部用 `tempfile.mkdtemp()` 自造，**绝不读写用户真实 config / 指纹库**。

运行：`venv\\Scripts\\python.exe tools/cue_fp_link_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cue_index                                                   # noqa: E402
import cuesheet                                                    # noqa: E402
import engine                                                      # noqa: E402
import fp                                                          # noqa: E402


class FakeCfg:
    """最小 Config（**项目铁律：`Config` 没有 `.get()`，只能用 `__getitem__`**）。"""

    def __init__(self, data=None):
        self.data = json.loads(json.dumps(data)) if data else {}
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        self.saves += 1


def make_sheet(title, tracks):
    """`tracks` = [(title, artist, dur, bpm, file_md5, fp_id, file_name)]"""
    s = cuesheet.CueSheet(title=title, res_w=1920, res_h=1080)
    for k, (tt, ar, du, bpm, md5, fpid, fname) in enumerate(tracks):
        te = cuesheet.TrackEntry(track_key="k%d" % k)
        te.identity = cuesheet.Identity(title=tt, artist=ar, duration_sec=du, bpm=bpm,
                                       file_md5=md5 or "", fp_id=fpid or "",
                                       file_name=fname or "")
        te.identity.ensure_meta_id()
        L = cuesheet.CompLayer(lyr_id="L%d" % k, name="层%d" % k, z=0)
        L.items.append(cuesheet.CueItem(
            item_id="i%d" % k, clip_id="c1",
            at_beats=0.0, src_in_sec=0.0, src_out_sec=8.0,
            trans_in="fade", trans_out="fade").normalized())
        te.layers.append(L)
        s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    return s


def new_engine(cfg):
    """最小引擎（`__new__` 跳过重初始化，只放本次要用的字段）。"""
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.cfg = cfg
    eng.layers = []
    eng._cue_index = None
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_rt = None
    eng._cue_media = {}
    return eng


def song_id_of(path):
    """与 `ui_main` 建库处、`engine._cue_song_id_of` **同一算法**（路径哈希）。"""
    return int(hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8], 16)


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="cuefp_")
    FP_ID = "fp2-0123456789abcdef"
    FILE_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
    MEDIA = os.path.join(tmp, "songA.flac")

    # 一张真码表（两份曲目：A 有 fp_id，B 只有 file_md5 —— 用来验"md5 优先"）
    sheet = make_sheet("指纹联调表", [
        ("Song A", "Artist A", 180.0, 128.0, "", FP_ID, "对方机器的文件名.flac"),
        ("Song B", "Artist B", 200.0, 126.0, FILE_MD5, "", "b.flac"),
    ])
    pkg = os.path.join(tmp, "fp_link.ercue")
    cuesheet.save(sheet, pkg, app_version="2026.10.17.12")

    cfg = FakeCfg({"music_meta": {}, "cue": {"sheets": []}})
    eng = new_engine(cfg)
    idx = eng.cue_index()
    idx.add(pkg)
    idx.save_cfg(cfg)
    check("索引里读到了这张码表", len(idx.entries) == 1, len(idx.entries))

    # ---------------- ① 正向：music_meta 有 fp_id ⇒ ① 级命中 ----------------
    print("\n① 正向：music_meta['fp_id'] 与码表一致 ⇒ 命中级别必须是 'fingerprint'")
    cfg["music_meta"][MEDIA] = {"title": "Song A", "artist": "Artist A",
                                "duration_sec": 180.0, "song_id": song_id_of(MEDIA),
                                "fp_id": FP_ID}
    hits = idx.match(fp_id=FP_ID, title="Song A", artist="Artist A",
                     duration_sec=180.0, file_name=os.path.basename(MEDIA))
    lvls = [h[1] for h in hits]
    print("      命中级别：%s" % lvls)
    check("命中且级别为 'fingerprint'", lvls == ["fingerprint"], lvls)

    # 端到端：走引擎真实的"拼查询参数 → 查表"路径（不是直接给 match 塞 fp_id）
    q = eng._cue_track_query(MEDIA)
    print("      _cue_track_query → fp_id=%r file_md5=%r"
          % (q.get("fp_id"), q.get("file_md5")))
    hits_e2e = eng.cue_sheets_for_track(**q)
    lvls_e2e = [h[1] for h in hits_e2e]
    check("引擎端到端（_cue_track_query → match）级别为 'fingerprint'",
          lvls_e2e == ["fingerprint"], lvls_e2e)
    # 同一首（标题+艺人也在）⇒ 至少能证明**不是靠标题蒙对的**：抹掉标题再来一次
    hits_bare = eng.cue_sheets_for_track(fp_id=FP_ID)
    check("只给 fp_id、不给标题艺人也能命中",
          [h[1] for h in hits_bare] == ["fingerprint"],
          [h[1] for h in hits_bare])

    # ---------------- ② 反向复现：把 fp_id 换成 song_id ⇒ 不命中 ----------------
    print("\n② 反向复现：fp_id 误写成 song_id（十进制）⇒ 必须**不命中**")
    sid = song_id_of(MEDIA)
    cfg["music_meta"][MEDIA] = {"title": "", "artist": "", "duration_sec": 0.0,
                                "song_id": sid, "fp_id": str(sid)}
    q_bad = eng._cue_track_query(MEDIA)
    print("      误写后的 fp_id=%r（真实签名 %r）" % (q_bad.get("fp_id"), FP_ID))
    hits_bad = idx.match(fp_id=q_bad.get("fp_id", ""))
    check("song_id 当 fp_id 用 ⇒ 0 条命中（证明这条链真的靠字段语义）",
          hits_bad == [], hits_bad)
    hits_bad2 = eng.cue_sheets_for_track(**q_bad)
    check("端到端同样 0 条命中", hits_bad2 == [], hits_bad2)

    # ---------------- ③ md5 优先于 fingerprint ----------------
    print("\n③ file_md5 命中 ⇒ 'md5'；与 fp_id 同时命中 ⇒ md5 优先（且命中不同的那条曲目）")
    tr_b, lvl_b = sheet.match_track(file_md5=FILE_MD5, fp_id=FP_ID,
                                    title="Song A", artist="Artist A")
    print("      同时给 md5+fp_id ⇒ level=%r, 命中曲目=%r"
          % (lvl_b, tr_b.identity.title if tr_b else None))
    check("级别为 'md5'", lvl_b == "md5", lvl_b)
    check("md5 优先命中 B（不是 fp_id 指向的 A）",
          tr_b is not None and tr_b.identity.title == "Song B",
          tr_b.identity.title if tr_b else None)
    hits_md5 = idx.match(file_md5=FILE_MD5)
    check("索引层 file_md5 命中 ⇒ 'md5'", [h[1] for h in hits_md5] == ["md5"],
          [h[1] for h in hits_md5])

    # ---------------- ④ meta 只有 song_id ⇒ 查询参数里两级都必须是空串 ----------------
    print("\n④ 只有 song_id 的 meta ⇒ q['file_md5'] 与 q['fp_id'] **都是空串**")
    p2 = os.path.join(tmp, "songC.flac")
    cfg["music_meta"][p2] = {"title": "Song C", "artist": "Artist C",
                             "duration_sec": 100.0, "song_id": song_id_of(p2)}
    q2 = eng._cue_track_query(p2)
    print("      q = %s" % {k: q2.get(k) for k in ("title", "artist", "file_md5", "fp_id")})
    check("file_md5 是空串（不是 song_id）", q2.get("file_md5") == "", q2.get("file_md5"))
    check("fp_id 是空串（不是 song_id）", q2.get("fp_id") == "", q2.get("fp_id"))
    check("song_id 的十进制字符串没有漏进任何一个 md5 字段",
          str(song_id_of(p2)) not in (q2.get("file_md5") or "",
                                      q2.get("fp_id") or ""),
          (q2.get("file_md5"), q2.get("fp_id")))
    # 完全没有 meta（未扫描过的歌）⇒ 只剩文件名级，也不许崩
    q3 = eng._cue_track_query(os.path.join(tmp, "nosuch.flac"))
    check("无 meta 时不抛且两级仍为空",
          q3.get("file_md5", "") == "" and q3.get("fp_id", "") == "", q3)

    # ---------------- ⑤ 签名函数本身 ----------------
    print("\n⑤ fp.signature_from_hashes：稳定 / 空→\"\" / 格式 ^fp2-[0-9a-f]{16}$")
    hs = [("0123456789abcdef0123", 0), ("fedcba9876543210fedc", 37),
          ("0123456789abcdef0123", 2048)]
    s1, s2 = fp.signature_from_hashes(hs), fp.signature_from_hashes(list(hs))
    print("      signature = %r" % s1)
    check("同一 hashes 两次结果一致", s1 == s2 and s1, (s1, s2))
    check("空列表 ⇒ 空串", fp.signature_from_hashes([]) == "",
          fp.signature_from_hashes([]))
    check("None ⇒ 空串", fp.signature_from_hashes(None) == "",
          fp.signature_from_hashes(None))
    import re as _re
    check("格式匹配 ^fp2-[0-9a-f]{16}$", bool(_re.match(r"^fp2-[0-9a-f]{16}$", s1)), s1)
    check("顺序敏感（换序 ⇒ 换签名）",
          fp.signature_from_hashes(list(reversed(hs))) != s1,
          fp.signature_from_hashes(list(reversed(hs))))
    # 码表里那个 fp_id 也必须是同格式
    check("码表曲目的 fp_id 同格式",
          bool(_re.match(r"^fp2-[0-9a-f]{16}$", sheet.tracks[0].identity.fp_id)),
          sheet.tracks[0].identity.fp_id)

    # ---------------- ⑥ add_song 复用 hashes（不二次 STFT）+ 旧返回值不变 ----------------
    print("\n⑥ FingerprintDB.add_song(hashes=, want_sig=True)：复用指纹、旧返回值仍是 int")
    dbp = os.path.join(tmp, "fp_test.db")
    db = fp.FingerprintDB(dbp)
    sid_a = 1234567
    hs_a = [("hash%04d" % i, i * 2048) for i in range(50)]
    # 旧调用姿势（不传 hashes / 不传 want_sig）：必须仍返回 int
    silent = np.zeros(fp.FFT_WINDOW + fp.HOP_SIZE * 4, dtype="float32")
    n0 = db.add_song(sid_a, silent, fp.SAMPLE_RATE, commit=True)
    check("不传 want_sig ⇒ 返回 int（旧调用方行为不变）", isinstance(n0, int), type(n0))

    # 关键：把 fingerprint_audio 换成"一调就炸"，证明传了 hashes 就不再算
    real_fp_audio = fp.fingerprint_audio
    fp.fingerprint_audio = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("传了 hashes 还去算 fingerprint_audio（二次全曲 STFT）"))
    try:
        n1 = db.add_song(sid_a, None, 44100, commit=True, hashes=hs_a)
        n2, sig2 = db.add_song(sid_a, None, 44100, commit=True,
                               hashes=hs_a, want_sig=True)
        ok_reuse = True
        err = ""
    except Exception as e:                                        # noqa: BLE001
        ok_reuse, err = False, "%s: %s" % (type(e).__name__, e)
        n1 = n2 = -1
        sig2 = ""
    finally:
        fp.fingerprint_audio = real_fp_audio
    check("传 hashes 时完全不再调 fingerprint_audio", ok_reuse, err)
    check("入库行数 = len(hashes)", n1 == len(hs_a) == n2, (n1, n2, len(hs_a)))
    check("want_sig=True ⇒ (int, 'fp2-…')", isinstance(n2, int) and sig2.startswith("fp2-"),
          (n2, sig2))
    check("add_song 返回的签名与 signature_from_hashes 一致",
          sig2 == fp.signature_from_hashes(hs_a), (sig2, fp.signature_from_hashes(hs_a)))
    rows = db.conn.execute("SELECT COUNT(*) FROM fingerprints WHERE song_id=?",
                           (sid_a,)).fetchone()[0]
    check("库里行数正确（重扫先删后插，不膨胀）", rows == len(hs_a), rows)
    db.close()

    # ---------------- ⑦ file_md5 是「文件字节」的 md5，与 song_id 无关 ----------------
    print("\n⑦ fp.file_md5 = 文件字节 md5（32 hex），与 song_id 完全不同")
    probe = os.path.join(tmp, "probe.bin")
    with open(probe, "wb") as fh:
        fh.write(b"EasyRealityAutoVJ")
    want = hashlib.md5(b"EasyRealityAutoVJ").hexdigest()
    got = fp.file_md5(probe)
    print("      file_md5=%s  song_id=%d" % (got, song_id_of(probe)))
    check("file_md5 与 hashlib 一致", got == want, (got, want))
    check("file_md5 是 32 hex", len(got) == 32 and all(c in "0123456789abcdef" for c in got), got)
    check("读不到文件 ⇒ 空串（不抛）", fp.file_md5(os.path.join(tmp, "nope.bin")) == "",
          fp.file_md5(os.path.join(tmp, "nope.bin")))
    check("file_md5 不可能等于 song_id 的十进制串", got != str(song_id_of(probe)),
          (got, str(song_id_of(probe))))

    # ---------------- ⑧ 拍长来源：_cue_track 优先，卸表后复位 ----------------
    print("\n⑧ _cue_beat_len_for：优先 _cue_track.identity.bpm（不靠 basename），卸表复位")
    local = os.path.join(tmp, "本机改名后的文件.flac")
    # 兜底来源：真实曲库**没有** `meta["bpm"]` 键，bpm 只在 `meta["grid"]["bpm"]` 里
    cfg["music_meta"][local] = {"grid": {"bpm": 90.0, "beat_len": 60.0 / 90.0}}
    # 装表（track 是 match_track 认出来的那条，identity.file_name 与本机文件名**不一致**）
    eng._cue_sheet = sheet
    eng._cue_track = sheet.tracks[0]                  # bpm=128
    bl = eng._cue_beat_len_for(local)
    print("      beat_len = %.6f（期望 60/128 = %.6f；兜底会给 60/90 = %.6f）"
          % (bl, 60.0 / 128.0, 60.0 / 90.0))
    check("用了码表 bpm 128 而不是 music_meta 的 90",
          abs(bl - 60.0 / 128.0) < 1e-9, bl)
    # 换歌那一拍：use_track=False ⇒ 跳过（还是上一首的）_cue_track，用本曲 grid bpm
    bl_sw = eng._cue_beat_len_for(local, use_track=False)
    check("use_track=False（换歌那一拍）跳过码表 bpm，用本曲 grid bpm（60/90）",
          abs(bl_sw - 60.0 / 90.0) < 1e-9, bl_sw)
    # 卸表 ⇒ 复位 ⇒ 回落 music_meta(grid.bpm)
    eng.clear_cue_sheet(silent=True)
    check("clear_cue_sheet 后 _cue_track 复位为 None",
          getattr(eng, "_cue_track", "missing") is None, getattr(eng, "_cue_track", "missing"))
    bl2 = eng._cue_beat_len_for(local)
    check("卸表后回落到 music_meta['grid']['bpm']（60/90）",
          abs(bl2 - 60.0 / 90.0) < 1e-9, bl2)
    # 顶层 bpm（手工/旧键）优先于 grid
    cfg["music_meta"][local] = {"bpm": 100.0, "grid": {"bpm": 90.0}}
    check("meta['bpm'] 优先于 meta['grid']['bpm']（60/100）",
          abs(eng._cue_beat_len_for(local) - 60.0 / 100.0) < 1e-9,
          eng._cue_beat_len_for(local))
    # 完全没信息 ⇒ 1.0（退化态，绝不抛）
    bl3 = eng._cue_beat_len_for(os.path.join(tmp, "unknown.flac"))
    check("无任何 bpm 信息 ⇒ 1.0", abs(bl3 - 1.0) < 1e-9, bl3)
    # 装表但 _cue_track 为空 ⇒ 老路径（basename 猜测）仍可用
    eng._cue_sheet = sheet
    eng._cue_track = None
    bl4 = eng._cue_beat_len_for(os.path.join(tmp, "b.flac"))
    check("_cue_track 为空时退回 basename 猜测（b.flac ⇒ 126bpm）",
          abs(bl4 - 60.0 / 126.0) < 1e-9, bl4)

    print("")
    if fails:
        print("FP_LINK_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("FP_LINK_OK  码表指纹 ① 级接线 / 反向复现 / md5 优先 / 三 md5 不混用 / "
          "add_song 复用 hashes / 拍长来源 全过")
    print("结论：OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
