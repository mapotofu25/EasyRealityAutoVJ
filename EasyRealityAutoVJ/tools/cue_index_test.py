# -*- coding: utf-8 -*-
"""Step2 自检：码表**索引**（曲目 → 码表；只存摘要；复用 `match_track`）。

5 项判据（团队给定）：
  1. 导入多曲目码表 ⇒ 引擎能列出"这张表覆盖的 3 首歌"
  2. 同一首歌绑了两张表 ⇒ 两张都能查出来（**选哪张留给 Step3**，这里只要求能查）
  3. **启动不加载整张表**（只读表头）⇒ 启动耗时增量 **< 50ms**
  4. 持久化：重启（配置 JSON 往返）后索引还在
  5. 回归另跑（11 项）

另做三条"防偷懒"断言：
  · `cuesheet.load(meta_only=True)` 的 **layers/items 必须为空**（否则等于还是加载了整张表）
  · **启动路径一次 `cuesheet.load` 都不许调**（零 IO）
  · 命中判定必须来自 `CueSheet.match_track`（用 md5 级命中验证级别字符串是 `md5`）

运行：`python tools/cue_index_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cuesheet                                                     # noqa: E402
import cue_index                                                    # noqa: E402
import engine                                                       # noqa: E402


class FakeCfg:
    """最小 Config：`cfg["cue"]` / 赋值 / `save()` 真落盘（测持久化）。"""

    def __init__(self, path=None, data=None):
        self.path = path
        self.data = json.loads(json.dumps(data)) if data else {}
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        self.saves += 1
        if self.path:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)


def make_sheet(title, tracks, with_items=True):
    """`tracks` = [(title, artist, dur, bpm, md5)]"""
    s = cuesheet.CueSheet(title=title, res_w=1920, res_h=1080)
    for k, (tt, ar, du, bpm, md5) in enumerate(tracks):
        te = cuesheet.TrackEntry(track_key="k%d" % k)
        te.identity = cuesheet.Identity(title=tt, artist=ar, duration_sec=du, bpm=bpm,
                                        file_md5=md5 or "")
        te.identity.ensure_meta_id()
        L = cuesheet.CompLayer(lyr_id="L%d" % k, name="层%d" % k, z=0)
        if with_items:
            for j in range(3):        # 每层塞 3 个片段：让"全量加载 vs 表头"成本可比
                L.items.append(cuesheet.CueItem(
                    item_id="i%d_%d" % (k, j), clip_id="c1",
                    at_beats=float(j * 16), src_in_sec=0.0, src_out_sec=8.0,
                    trans_in="fade", trans_out="fade").normalized())
        te.layers.append(L)
        s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    return s


def new_engine(cfg):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.cfg = cfg
    eng.layers = []
    eng._cue_index = None
    return eng


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="cueidx_")

    # 表 A：3 首（其中 "Nimbus Spark" 与表 B 重复）
    A = make_sheet("表A-三首", [
        ("Nimbus Spark", "Supa7onyz", 165.5, 155.03, "aaa111"),
        ("Second Wind", "Aoi", 200.0, 128.0, "bbb222"),
        ("Third Rail", "K", 180.0, 140.0, ""),
    ])
    # 表 B：2 首（含同一首 "Nimbus Spark"）
    B = make_sheet("表B-两首", [
        ("Nimbus Spark", "Supa7onyz", 165.5, 155.03, "aaa111"),
        ("Other Song", "Z", 190.0, 132.0, "ccc333"),
    ])
    pkgA = os.path.join(tmp, "A.ercue")
    pkgB = os.path.join(tmp, "B.ercue")
    cuesheet.save(A, pkgA, app_version="2026.10.17.12")
    cuesheet.save(B, pkgB, app_version="2026.10.17.12")

    # ---------------- 防偷懒①：meta_only 真的不建 layers/items ----------------
    print("⓪ 前置断言：meta_only 只读表头")
    full, _ = cuesheet.load(pkgA)
    meta, info = cuesheet.load(pkgA, meta_only=True)
    n_full_items = sum(len(getattr(ly, "items", []) or []) for t in full.tracks
                       for ly in getattr(t, "layers", []))
    n_meta_items = sum(len(getattr(ly, "items", []) or []) for t in meta.tracks
                       for ly in getattr(t, "layers", []))
    print("      全量加载：%d 首歌 / %d 个片段；meta_only：%d 首歌 / %d 个片段"
          % (len(full.tracks), n_full_items, len(meta.tracks), n_meta_items))
    check("meta_only 仍能拿到 3 首歌", len(meta.tracks) == 3, len(meta.tracks))
    check("meta_only 的 items 为空（没建片段）", n_meta_items == 0, n_meta_items)
    check("meta_only 的 media 为空（没建素材引用）", len(meta.media) == 0, len(meta.media))
    check("meta_only 不做媒体检查（返回空 info）",
          info["missing_media"] == [] and info["present_media"] == {}, info)

    cfg_path = os.path.join(tmp, "cfg.json")
    cfg = FakeCfg(cfg_path)
    eng = new_engine(cfg)

    # ---------------- ① 多曲目表 ⇒ 列出覆盖的 3 首 ----------------
    print("\n① 多曲目码表：引擎能列出覆盖的 3 首歌")
    idx = eng.cue_index()
    eA = idx.add(pkgA)
    idx.save_cfg(cfg)
    check("索引里出现表A", eA is not None and os.path.abspath(pkgA) == eA["path"], eA and eA["path"])
    check("表A 覆盖 3 首（n_tracks=3）", eA["n_tracks"] == 3, eA["n_tracks"])
    check("摘要里带 3 首歌的标题/艺人/时长/BPM",
          [(t["title"], t["artist"]) for t in eA["tracks"]]
          == [("Nimbus Spark", "Supa7onyz"), ("Second Wind", "Aoi"), ("Third Rail", "K")],
          eA["tracks"])
    listed = [t["title"] for t in eA["tracks"]]
    print("      覆盖曲目：%s" % listed)
    check("索引层 covers() == 3（单表时）", idx.covers() == 3, idx.covers())

    # ---------------- ② 同一首歌绑两张表 ⇒ 都能查到 ----------------
    print("\n② 同一首歌绑了两张表 ⇒ 两张都查得到")
    idx.add(pkgB)
    idx.save_cfg(cfg)
    hits = eng.cue_sheets_for_track(title="Nimbus Spark", artist="Supa7onyz",
                                    duration_sec=165.5)
    titles = sorted(h[0]["title"] for h in hits)
    print("      命中：%s（级别 %s）" % (titles, [h[1] for h in hits]))
    check("两张表都命中", len(hits) == 2, len(hits))
    check("命中的是表A/表B", titles == ["表A-三首", "表B-两首"], titles)
    # md5 级（最高可靠性）：必须来自 match_track 的 "md5"
    h2 = eng.cue_sheets_for_track(title="", artist="", file_md5="aaa111")
    check("md5 命中且级别为 'md5'（证明走的是 match_track 五级链）",
          len(h2) == 2 and all(x[1] == "md5" for x in h2), [(x[0]["title"], x[1]) for x in h2])
    # 不存在的歌 ⇒ 0 条
    check("不存在的歌 ⇒ 0 条", eng.cue_sheets_for_track(title="NoSuchSong") == [],
          eng.cue_sheets_for_track(title="NoSuchSong"))
    check("索引覆盖率：2 张表 → 4 首去重", idx.covers() == 4, idx.covers())

    # ---------------- ③ 启动：不加载整张表 + < 50ms + 零 IO ----------------
    print("\n③ 启动：只读索引（零 IO）且 < 50ms")
    # 造 20 条摘要（模拟"用户装了很多表"）后测 load_cfg
    big = FakeCfg()
    cue_index.cue_cfg(big)["sheets"] = [dict(eA, path="P:/x/s%d.ercue" % i)
                                        for i in range(20)]
    calls = {"n": 0}
    real_load = cuesheet.load

    def _counting_load(*a, **k):
        calls["n"] += 1
        return real_load(*a, **k)

    cuesheet.load = _counting_load
    try:
        t0 = time.perf_counter()
        idx2 = cue_index.CueIndex().load_cfg(big)
        t_load = (time.perf_counter() - t0) * 1000.0
        n_at_load = calls["n"]
        # 触发一次查询（这时才会惰性读表头）
        idx2.match(title="Third Rail", artist="K")
        n_after_match = calls["n"]
    finally:
        cuesheet.load = real_load
    print("      load_cfg(20 条) = %.2f ms ；期间 load 调用 %d 次；首次 match 后累计 %d 次"
          % (t_load, n_at_load, n_after_match))
    check("启动 load_cfg < 50ms", t_load < 50.0, round(t_load, 2))
    check("启动期零 IO（load 调用 0 次）", n_at_load == 0, n_at_load)
    check("只有真正查询时才读表头（≥1 次）", n_after_match >= 1, n_after_match)

    # ---------------- ④ 持久化 ----------------
    print("\n④ 持久化：配置 JSON 往返（模拟重启）后索引还在")
    raw = json.loads(json.dumps(cfg.data))          # "重启"：只剩磁盘上的 JSON
    check("cfg 里确实只存了摘要（没有 layers/items 这种整表字段）",
          all(("tracks" in e) and ("layers" not in e) and ("items" not in e)
              for e in raw["cue"]["sheets"]), raw["cue"]["sheets"][:1])
    cfg2 = FakeCfg(cfg_path, raw)
    idx3 = cue_index.CueIndex().load_cfg(cfg2)
    check("重启后条目数一致", len(idx3.entries) == len(idx.entries),
          (len(idx3.entries), len(idx.entries)))
    check("重启后仍能查到两张表（同一首歌）",
          len(idx3.match(title="Nimbus Spark", artist="Supa7onyz")) == 2,
          idx3.match(title="Nimbus Spark", artist="Supa7onyz"))
    check("重启后标题/覆盖数一致",
          idx3.titles() == idx.titles() and idx3.covers() == idx.covers(),
          (idx3.titles(), idx3.covers()))

    # 删包 ⇒ 索引同步
    idx3.remove(pkgA)
    check("remove 之后只剩 1 张表", len(idx3.entries) == 1, len(idx3.entries))
    check("remove 后同一首歌只命中 1 张",
          len(idx3.match(title="Nimbus Spark", artist="Supa7onyz")) == 1,
          idx3.match(title="Nimbus Spark", artist="Supa7onyz"))

    print("")
    if fails:
        print("INDEX_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("INDEX_OK  多曲目索引 / 双表同曲 / 启动零 IO(<50ms) / 持久化 / 删包 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
