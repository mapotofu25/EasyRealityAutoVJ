# -*- coding: utf-8 -*-
"""码表 → 曲库**派生行**自检（`cue_index.library_rows()`，零 Qt、可无头跑）。

覆盖 7 条判据（团队给定）：
  1. 本机没覆盖 ⇒ 出派生行；`key` 以 `@cue:` 开头；`sheet_name` = 表文件名
  2. 本机 `fp_id` 相同 ⇒ **不出**派生行
  3. 本机只有 `meta_id`（没 `fp_id` / `file_md5`）且相同 ⇒ **不出**（降级链生效）
  4. **反向复现**：把本机 `song_id`（整数，**路径**哈希）当 md5 用 ⇒ 必须判"未覆盖"
     （这条在"误用 song_id"的实现上会**失败**）
  5. 码表包文件被删 ⇒ 该表的派生行全部消失（"跟随码表"的根）
  6. 同一张表里两条 track 的 `fp_id` 都为空、title/artist 相同 ⇒ `key` 不重复（有 `#2`）
  7. 空 `cfg["cue"]["sheets"]` ⇒ 返回 `[]`，不抛

另加两条正向断言（防止把"覆盖"判得太宽 / 太窄）：
  · 本机 `file_md5` 相同 ⇒ 不出派生行（32 hex 文件内容 md5 这一级真的生效）
  · 本机只有 title+artist+时长（差 ≤2s）⇒ 不出派生行（第 ④ 级降级生效）

运行：`venv\\Scripts\\python.exe tools/cue_lib_rows_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cuesheet                                                     # noqa: E402
import cue_index                                                    # noqa: E402


class FakeCfg:
    """最小 `Config`：只支持 `cfg[key]` / `cfg[key]=v` / `save()`（同项目铁律：没有 .get()）。"""

    def __init__(self, data=None):
        self.data = json.loads(json.dumps(data)) if data else {}
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        self.saves += 1


def make_sheet(tracks):
    """`tracks` = [(title, artist, dur, bpm, fp_id, file_md5)]"""
    s = cuesheet.CueSheet(title="测试码表", res_w=1920, res_h=1080)
    for k, (tt, ar, du, bpm, fp, md5) in enumerate(tracks):
        te = cuesheet.TrackEntry(track_key="k%d" % k)
        te.identity = cuesheet.Identity(
            title=tt, artist=ar, duration_sec=du, bpm=bpm,
            fp_id=fp or "", file_md5=md5 or "")
        te.identity.ensure_meta_id()
        te.layers.append(cuesheet.CompLayer(lyr_id="L%d" % k, name="层%d" % k, z=0))
        s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    return s


MD5_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"      # 32 hex（文件内容 md5 的形状）
FP_A = "fp2-0123456789abcdef"                    # fp_id 的形状（fp2- + 16 hex）
FP_B = "fp2-fedcba9876543210"
SONG_ID = 463268676                              # int(md5(路径)[:8], 16) —— **路径**哈希

TRACKS = [
    # ① 本机完全没覆盖
    ("Nimbus Spark", "Supa7onyz", 165.5, 155.03, FP_A, MD5_A),
    # ② 应被本机 fp_id 覆盖
    ("Covered Fp", "AA", 180.0, 128.0, FP_B, ""),
    # ③ 应被本机 meta_id 覆盖（本机没 fp_id / file_md5）
    ("Covered Meta", "BB", 200.0, 130.0, "", ""),
    # 正向：应被本机 file_md5 覆盖
    ("Covered Md5", "CC", 170.0, 132.0, "", "00112233445566778899aabbccddeeff"),
    # ④ 反向陷阱：md5 位置被写成了 song_id 的**十进制串**
    ("SongId Trap", "DD", 150.0, 140.0, "", str(SONG_ID)),
    # ⑥ 两条同名同艺人、fp_id 全空 ⇒ track_key 撞车，必须靠 #2 去重
    ("Dup Title", "EE", 120.0, 124.0, "", ""),
    ("Dup Title", "EE", 120.0, 124.0, "", ""),
    # 正向：只有 title+artist+时长（第 ④ 级）
    ("Name Only", "FF", 240.0, 122.0, "", ""),
]


def meta_of(title):
    """从 TRACKS 里取一首（造本机 music_meta 用）。"""
    for tt, ar, du, _bpm, fp, md5 in TRACKS:
        if tt == title:
            return {"title": tt, "artist": ar, "dur": du, "fp_id": fp, "file_md5": md5}
    raise KeyError(title)


def local_rec(title, **over):
    """造一条本机 `cfg["music_meta"][路径]`（形状照真实曲库：BPM/时长在 grid 里）。"""
    m = meta_of(title)
    rec = {
        "song_id": SONG_ID,                       # ★ 老曲库只有这个，没有 fp_id/file_md5
        "grid": {"bpm": m["bpm"] if "bpm" in m else 120.0, "dur": m["dur"]},
        "artist": m["artist"],
        "title": m["title"],
        "genres": ["House"],
        "source": "vdj",
    }
    rec.update(over)
    return rec


def rows_of(cfg, idx):
    return cue_index.library_rows(cfg, idx)


def titles(rows):
    return [r["title"] for r in rows]


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="cuelib_")
    pkg = os.path.join(tmp, "演出码表.ercue")
    cuesheet.save(make_sheet(TRACKS), pkg, app_version="2026.10.17.12")

    idx = cue_index.CueIndex()
    idx.add(pkg)
    check("索引读到了这张表", len(idx.entries) == 1, idx.entries)

    def cfg_with(local_paths=None, sheets=True):
        """造配置：`local_paths` = [(路径, meta)]；`sheets` 决定是否带码表索引。"""
        data = {"music_library": [p for p, _m in (local_paths or [])],
                "music_meta": {p: m for p, m in (local_paths or [])}}
        data["cue"] = {"sheets": list(idx.entries) if sheets else []}
        return FakeCfg(data)

    # ---------------- ⑦ 空码表 ⇒ []（且不抛） ----------------
    print("⑦ 没有码表 ⇒ 返回空列表")
    cfg = cfg_with([], sheets=False)
    r7 = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    check("空 sheets ⇒ []", r7 == [], r7)

    # ---------------- ① 本机没覆盖 ⇒ 出派生行 ----------------
    print("\n① 本机曲库为空 ⇒ 码表曲目全部成为派生行")
    cfg = cfg_with([])
    rows = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    tt = titles(rows)
    check("8 条 track 全部出现（含重复的同名两条）", len(rows) == len(TRACKS), tt)
    r1 = [r for r in rows if r["title"] == "Nimbus Spark"]
    check("第一条派生行的 key 以 '@cue:' 开头",
          bool(r1) and r1[0]["key"].startswith("@cue:"), r1 and r1[0]["key"])
    check("派生行 key 里带码表**绝对路径**",
          bool(r1) and os.path.abspath(pkg) in r1[0]["key"], r1 and r1[0]["key"])
    check("sheet_name = 表文件名", bool(r1) and r1[0]["sheet_name"]
          == os.path.basename(pkg), r1 and r1[0]["sheet_name"])
    check("派生行带指纹（fp_id）", bool(r1) and r1[0]["fp_id"] == FP_A,
          r1 and r1[0]["fp_id"])
    check("派生行带 file_md5（32 hex）", bool(r1) and r1[0]["file_md5"] == MD5_A,
          r1 and r1[0]["file_md5"])
    # ★ 派生行绝不落盘：library_rows 不许改配置
    check("调用前后 cfg['music_library'] 不变（派生行不落盘）",
          cfg["music_library"] == [], cfg["music_library"])
    check("调用前后 cfg['music_meta'] 不变（派生行不落盘）",
          cfg["music_meta"] == {}, cfg["music_meta"])

    # ---------------- ② 本机 fp_id 相同 ⇒ 不出 ----------------
    print("\n② 本机 fp_id 相同 ⇒ 不重复出现")
    cfg = cfg_with([("P:/music/covered_fp.mp3",
                     local_rec("Covered Fp"))])
    rows = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    check("Covered Fp 被本机覆盖（fp_id 级）", "Covered Fp" not in titles(rows),
          titles(rows))
    check("其它曲目照常出现", "Nimbus Spark" in titles(rows), titles(rows))

    # ---------------- ③ 本机只有 meta_id ⇒ 不出（降级链） ----------------
    print("\n③ 本机只有 meta_id（无 fp_id / file_md5）⇒ 仍判为已覆盖")
    mid = cuesheet.Identity.meta_key("Covered Meta", "BB", 200.0)
    cfg = cfg_with([("P:/music/covered_meta.mp3",
                     {"song_id": SONG_ID, "meta_id": mid,
                      "grid": {"bpm": 130.0, "dur": 200.0},
                      "artist": "BB", "title": "Covered Meta"})])
    rows = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    check("Covered Meta 被本机覆盖（meta_id 级）", "Covered Meta" not in titles(rows),
          titles(rows))

    # ---------------- 正向：file_md5 级 / title+artist 级 ----------------
    print("\n正向：file_md5 与 title+artist+时长 两级降级都真的生效")
    cfg = cfg_with([
        ("P:/music/covered_md5.mp3", {
            "song_id": SONG_ID,
            "file_md5": "00112233445566778899aabbccddeeff",   # 32 hex 文件内容 md5
            "grid": {"bpm": 132.0, "dur": 170.0},
            "artist": "CC", "title": "Covered Md5"}),
        ("P:/music/name_only.mp3", {
            "song_id": SONG_ID,
            "grid": {"bpm": 122.0, "dur": 241.0},             # 差 1 秒 ≤2s
            "artist": "FF", "title": "Name Only (Original Mix)"}),
    ])
    rows = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    check("Covered Md5 被本机覆盖（file_md5 级）", "Covered Md5" not in titles(rows),
          titles(rows))
    check("Name Only 被本机覆盖（title+artist+时长 ≤2s）",
          "Name Only" not in titles(rows), titles(rows))

    # ---------------- ④ 反向复现：song_id ≠ file_md5 ----------------
    print("\n④ 反向复现：拿 song_id 当 md5 用 ⇒ 必须仍判「未覆盖」")
    trap = [t for t in TRACKS if t[0] == "SongId Trap"][0]
    trap_track = {"title": trap[0], "artist": trap[1], "duration_sec": trap[2],
                  "bpm": trap[3], "fp_id": trap[4], "file_md5": trap[5],
                  "meta_id": cuesheet.Identity.meta_key(trap[0], trap[1], trap[2])}
    trap_rec = {"song_id": SONG_ID, "grid": {"bpm": 140.0, "dur": 150.0},
                "artist": "Totally Other", "title": "Nothing Alike"}
    check("_as_md5(整数 song_id) == ''（song_id 进不了 md5 通道）",
          cue_index._as_md5(SONG_ID) == "", cue_index._as_md5(SONG_ID))
    check("track.file_md5 写成 song_id 十进制串 ⇒ 仍未被覆盖",
          cue_index._covered(trap_track, trap_rec) is False)
    cfg = cfg_with([("P:/music/trap.mp3", trap_rec)])
    rows = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    check("派生行里仍出现 SongId Trap", "SongId Trap" in titles(rows), titles(rows))

    # ---------------- ⑥ 同表内 track_key 撞车 ⇒ 加序号 ----------------
    print("\n⑥ 同表内 fp_id 全空 + 同名同艺人 ⇒ key 不重复")
    dup = [r for r in rows if r["title"] == "Dup Title"]
    check("两条同名曲目都在", len(dup) == 2, dup)
    keys = [r["key"] for r in dup]
    check("两条 key 不同（有 #2 去重）", len(set(keys)) == 2, keys)
    check("第二条 key 以 '#2' 结尾",
          len(keys) == 2 and keys[1].endswith("#2"), keys)

    # ---------------- ⑤ 包被删 ⇒ 派生行全部消失 ----------------
    print("\n⑤ 码表包文件被删 ⇒ 该表派生行全部消失（跟随码表）")
    os.remove(pkg)
    rows = rows_of(cfg, cue_index.CueIndex().load_cfg(cfg))
    check("包没了 ⇒ 派生行全部消失", rows == [], titles(rows))
    check("（索引摘要本身还在 —— 正是靠 isfile 判活，而不是靠索引条目）",
          len(idx.entries) == 1, idx.entries)

    # ---------------- 结论 ----------------
    print("\n" + "=" * 78)
    if fails:
        print("FAIL：%d 项未通过 → %s" % (len(fails), fails))
        print("=" * 78)
        return 1
    print("OK：码表 → 曲库派生行 7 条判据 + 2 条正向断言全部通过")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
