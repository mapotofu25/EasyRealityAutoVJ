# -*- coding: utf-8 -*-
"""码表**索引**：曲目 → 可用码表（`cfg["cue"]["sheets"]`）。

════════════════════════════════════════════════════════════════════════
为什么单独一个模块（而不是塞进 engine）
════════════════════════════════════════════════════════════════════════
1. **启动不能加载整张表**：一张码表可能上千个片段，`cuesheet.load()` 全量构造要几十~几百 ms；
   而"这首歌有哪些码表"只需要**表头**（tracks + identity）。⇒ `cuesheet.load(meta_only=True)`。
2. **不重造匹配逻辑**：认歌的五级链（md5 → 指纹 → meta → 名+艺人 → 文件名）在
   `cuesheet.CueSheet.match_track()` 里，本模块**只做"多张表里谁命中"的汇总**，
   命中判定一律调它 —— 否则两套逻辑迟早分叉（与 Cue 端也会分叉）。
3. 零 Qt、零 player、零 IO（`load_cfg` 纯内存）⇒ 可无头单测（`tools/cue_index_test.py`）。

存储口径：`cfg["cue"]["sheets"]` 里**只存摘要**（路径 / 表名 / 画布 / 曲目清单），
**不存整张表**；真正装表走 `engine.set_cue_sheet()`（那时才 `load()` 全量）。
"""
from __future__ import annotations

import os
import re
import time

import cuesheet

CUE_CFG_DEFAULTS = {
    "recent": [],
    "media_dir": "",
    "seek_profile": "auto",
    "sheets": [],        # ← 本模块维护：码表索引（只存摘要）
}
SHEET_CACHE_MAX = 32      # `_sheets`（meta-only 的 CueSheet 实例）上限，防长跑堆积

#: 认歌级别的可靠性排序（越小越可靠）——与 `CueSheet.match_track` 的返回字符串一致
LEVEL_ORDER = {"md5": 0, "fingerprint": 1, "meta": 2,
               "title+artist": 3, "title+artist?": 4, "file": 5}


def cue_cfg(cfg):
    """取（并在缺失时初始化）`cfg["cue"]`；旧配置直接可用，未知键无害。"""
    try:
        cur = cfg["cue"]
    except Exception:                                            # noqa: BLE001
        cur = None
    if not isinstance(cur, dict):
        cur = {}
        try:
            cfg["cue"] = cur
        except Exception:                                        # noqa: BLE001
            pass
    for k, v in CUE_CFG_DEFAULTS.items():
        if k not in cur:
            cur[k] = list(v) if isinstance(v, list) else v
    return cur


# ==========================================================================
def read_meta(path):
    """读**表头摘要**（不建 media/layers/items）。失败返回 None（绝不抛）。

    摘要字段刻意很小：路径、表名、画布、曲目清单（title/artist/时长/BPM/meta_id/fp_id/file_md5）、
    文件 mtime/size（用于"变了才重读"）。
    """
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return None
    try:
        sheet, _info = cuesheet.load(p, meta_only=True)
    except Exception:                                            # noqa: BLE001
        return None
    tracks = []
    for t in (getattr(sheet, "tracks", None) or []):
        idn = getattr(t, "identity", None)
        if idn is None:
            continue
        tracks.append({
            "title": str(getattr(idn, "title", "") or ""),
            "artist": str(getattr(idn, "artist", "") or ""),
            "duration_sec": float(getattr(idn, "duration_sec", 0.0) or 0.0),
            "bpm": float(getattr(idn, "bpm", 0.0) or 0.0),
            "meta_id": str(getattr(idn, "meta_id", "") or ""),
            "fp_id": str(getattr(idn, "fp_id", "") or ""),
            "file_md5": str(getattr(idn, "file_md5", "") or ""),
            "file_name": str(getattr(idn, "file_name", "") or ""),
        })
    try:
        st = os.stat(p)
        mtime, size = float(st.st_mtime), int(st.st_size)
    except OSError:
        mtime, size = 0.0, 0
    return {
        "path": os.path.abspath(p),
        "title": str(getattr(sheet, "title", "") or ""),
        "res": [int(getattr(sheet, "res_w", 1920) or 1920),
                int(getattr(sheet, "res_h", 1080) or 1080)],
        "n_tracks": len(tracks),
        "tracks": tracks,
        "format_version": int(getattr(sheet, "format_version", 0) or 0),
        "min_app_version": str(getattr(sheet, "min_app_version", "") or ""),
        "requires_features": list(getattr(sheet, "requires_features", None) or []),
        "mtime": mtime,
        "size": size,
        "indexed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==========================================================================
class CueIndex:
    """码表索引：**只存摘要**；`match()` 时按需把 meta-only 的表读进内存再判定。

    用法：
        idx = CueIndex(); idx.load_cfg(cfg)      # 启动：纯内存，零 IO（实测 <1ms）
        idx.rescan()                             # 导入/显式刷新：只重读"变了"的包
        idx.match(title="…", artist="…")          # → [(摘要, 命中级别)]，按可靠性排序
    """

    def __init__(self):
        self.entries = []          # [dict]（见 read_meta）
        self._sheets = {}          # path → meta-only CueSheet（惰性）
        self.last_rescan_ms = 0.0
        self.last_rescan_n = 0

    # ---------------- 配置 ----------------
    def load_cfg(self, cfg):
        """从 `cfg["cue"]["sheets"]` 读索引（**纯内存、零 IO**）。返回自身。"""
        self.entries = []
        self._sheets = {}
        try:
            raw = cue_cfg(cfg).get("sheets") or []
        except Exception:                                        # noqa: BLE001
            raw = []
        for e in raw:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            self.entries.append({
                "path": str(e.get("path") or ""),
                "title": str(e.get("title") or ""),
                "res": list(e.get("res") or [1920, 1080])[:2],
                "n_tracks": int(e.get("n_tracks", 0) or 0),
                "tracks": list(e.get("tracks") or []),
                "format_version": int(e.get("format_version", 0) or 0),
                "min_app_version": str(e.get("min_app_version") or ""),
                "requires_features": list(e.get("requires_features") or []),
                "mtime": float(e.get("mtime", 0.0) or 0.0),
                "size": int(e.get("size", 0) or 0),
                "indexed_at": str(e.get("indexed_at") or ""),
            })
        return self

    def save_cfg(self, cfg):
        """把索引写回 `cfg["cue"]["sheets"]` 并落盘（**只写摘要**）。"""
        try:
            cue_cfg(cfg)["sheets"] = list(self.entries)
            cfg.save()
        except Exception:                                        # noqa: BLE001
            pass
        return self.entries

    # ---------------- 扫描 / 增删 ----------------
    def rescan(self, paths=None):
        """（重新）读表头：`paths=None` ⇒ 扫索引里所有条目，**只重读 mtime/size 变了的**。

        返回 (耗时ms, 实际读取的包数)。
        """
        t0 = time.perf_counter()
        n_read = 0
        if paths is None:
            for e in list(self.entries):
                p = e["path"]
                try:
                    st = os.stat(p)
                    if (float(st.st_mtime) == float(e.get("mtime", -1))
                            and int(st.st_size) == int(e.get("size", -1))):
                        continue                      # 没变 ⇒ 不读（启动/刷新都快）
                except OSError:
                    continue                          # 文件没了：保留旧摘要（下次 add/remove 再处理）
                m = read_meta(p)
                if m is not None:
                    self._upsert(m)
                    n_read += 1
        else:
            for p in paths:
                m = read_meta(p)
                if m is not None:
                    self._upsert(m)
                    n_read += 1
                else:
                    self.remove(p)
        self.last_rescan_ms = (time.perf_counter() - t0) * 1000.0
        self.last_rescan_n = n_read
        return self.last_rescan_ms, n_read

    def _upsert(self, meta):
        p = meta["path"]
        self._sheets.pop(p, None)
        for i, e in enumerate(self.entries):
            if e["path"] == p:
                self.entries[i] = meta
                return
        self.entries.append(meta)

    def add(self, path):
        """加/更新一个包（读表头）。返回摘要；读不了 ⇒ None。"""
        m = read_meta(path)
        if m is None:
            return None
        self._upsert(m)
        return m

    def remove(self, path):
        p = os.path.abspath(str(path or ""))
        self._sheets.pop(p, None)
        n = len(self.entries)
        self.entries = [e for e in self.entries if e["path"] != p]
        return len(self.entries) != n

    # ---------------- 查询 ----------------
    def sheet_for(self, path):
        """取（并缓存）某包的 meta-only CueSheet；失败返回 None。"""
        p = os.path.abspath(str(path or ""))
        if p in self._sheets:
            return self._sheets[p]
        try:
            sheet, _ = cuesheet.load(p, meta_only=True)
        except Exception:                                        # noqa: BLE001
            return None
        if len(self._sheets) >= SHEET_CACHE_MAX:
            self._sheets.clear()                 # 简单起见整体清空（条目少，重建很便宜）
        self._sheets[p] = sheet
        return sheet

    def match(self, title="", artist="", duration_sec=0.0, fp_id="",
              file_name="", file_md5=""):
        """**这首歌能用哪些码表** → `[(摘要, 命中级别)]`，按可靠性（md5 最优先）排序。

        ★ 命中判定**完全复用** `CueSheet.match_track()`（五级链），本函数只做汇总与排序。
        """
        out = []
        for e in self.entries:
            sheet = self.sheet_for(e["path"])
            if sheet is None:
                continue
            try:
                tr, level = sheet.match_track(title=title, artist=artist,
                                              duration_sec=duration_sec, fp_id=fp_id,
                                              file_name=file_name, file_md5=file_md5)
            except Exception:                                    # noqa: BLE001
                continue
            if tr is not None and level:
                out.append((e, level))
        out.sort(key=lambda x: LEVEL_ORDER.get(x[1], 99))
        return out

    def covers(self):
        """索引里**一共覆盖多少首（去重后）曲目** —— 状态栏"覆盖 N 首"用它。"""
        keys = set()
        for e in self.entries:
            for t in (e.get("tracks") or []):
                k = t.get("meta_id") or ("%s|%s" % (t.get("title", ""), t.get("artist", "")))
                keys.add(k)
        return len(keys)

    def titles(self):
        return [e.get("title") or os.path.basename(e.get("path") or "")
                for e in self.entries]


# ==========================================================================
# 曲库里的「码表派生行」
# ==========================================================================
#: 派生行的伪路径前缀：`@cue:<码表绝对路径>#<track_key>`（与导入包的 `@imported:` 同风格）
CUE_ROW_PREFIX = "@cue:"


def _norm(s):
    """归一化：小写 → 去扩展名 → 去括号后缀 → 去 feat/remix… 后缀 → 只留 数字/字母/汉字。

    与 `cuesheet._norm_title_artist` **同一口径**（认歌第 ③/④ 级用的就是它）——
    两套归一化一旦分叉，"本机明明有这首歌"就会判成"没覆盖"。
    """
    s = str(s or "").lower().strip()
    s = re.sub(r"\.[a-z0-9]{2,5}$", "", s)                          # 去扩展名
    s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)                    # 去括号后缀
    s = re.sub(r"\b(feat|ft|remix|vip|edit|mix|version|master|remaster)\b.*$", "", s)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


def _as_md5(v):
    """只接受 **32 位十六进制**（= 文件字节的 md5）；其它一律返回 `""`。

    ⚠⚠ 这里刻意**拒绝整数与短串**。本机曲库里还有一个 `song_id`
      （`int(md5(路径字符串)[:8], 16)`，如 `463268676`），它是**路径**哈希、
      跟文件内容毫无关系。若让 `song_id` 混进这条比较，结果是"永远不相等"且
      **不报任何错** ⇒ 本机明明有这首歌，却每次都被当成码表独有（用户提醒：
      「注意 md5 的对比」）。本函数就是那道闸门。
    """
    t = str(v if v is not None else "").strip().lower()
    if len(t) != 32:
        return ""
    for ch in t:
        if ch not in "0123456789abcdef":
            return ""
    return t


def _dur(v):
    """任意值 → 正数秒（float）；无效/≤0 ⇒ None。"""
    try:
        f = float(v or 0.0)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _local_dur(rec):
    """本机 meta（`cfg["music_meta"][路径]`）的时长（秒）；没有 ⇒ None。

    ⚠ BPM 与时长都在 `meta[路径]["grid"]` 里（曲库扫描建库时写入），不在顶层。
    """
    g = rec.get("grid") if isinstance(rec, dict) else None
    if not isinstance(g, dict):
        return None
    return _dur(g.get("dur"))


def _meta_key(title, artist, dur):
    """归一化身份 key：`artist|title|时长取整到 2 秒`。

    与 `cuesheet.Identity.meta_key()` 同公式 —— 本机老曲库没有 `meta_id` 字段时，
    用它从 title/artist/时长现算一个来比对（否则第 ③ 级永远空着）。
    """
    d = 0
    try:
        d = int(round(float(dur or 0.0) / 2.0) * 2)
    except (TypeError, ValueError):
        d = 0
    return "%s|%s|%d" % (_norm(artist), _norm(title), d)


def _covered(track, rec):
    """码表曲目 `track` 是否**已被本机曲库覆盖**（`rec` = 本机 `music_meta` 的一项）。

    判定链（**严格按可靠性从高到低**，命中任一级即算覆盖）：
      ① `fp_id` 相等（`fp2-<16hex>` 指纹签名）
      ② `file_md5` 相等（32 hex 文件**内容** md5）
      ③ `meta_id` 相等（两侧缺失时用 title/artist/时长现算，同一套归一化）
      ④ title + artist 归一化相等 **且** 时长差 ≤2 秒

    ⚠ 每一级都**只在两侧字段真的存在时**才成立 ⇒ 老曲库没有 `fp_id`/`file_md5`
      会自动降级到 ③/④，不会因为缺字段就崩、也不会全判成"未覆盖"。
    ⚠⚠ `song_id`（整数，路径哈希）**全程不参与** —— 见 `_as_md5()`。

    ★ 实现上只是"只有一个本机曲目的 `_LocalIndex`" —— 判定逻辑**只有一份**
      （在 `_LocalIndex.covers()`），批量场景才不会与单条场景分叉。
    """
    idx = _LocalIndex()
    idx.add(rec)
    return idx.covers(track)


class _LocalIndex:
    """本机曲库的可比索引：`fp_id` / `file_md5` / `meta_id` 用集合、名+艺人用字典。

    为什么要它：`library_rows()` 要给**每条码表曲目**判"本机有没有"。
    朴素写法是「每条 track × 每首本机曲目」两两比较 —— 1000 条 track × 3000 首本机
    = 300 万次比较，而 `refresh()` 在切排序/切筛选时会被反复调用。
    这里把本机曲库压成集合/字典 ⇒ 每条 track 只做 O(1) 查表。
    """

    __slots__ = ("fp", "md5", "meta", "names")

    def __init__(self):
        self.fp = set()            # {fp_id}
        self.md5 = set()           # {32 hex 文件内容 md5}
        self.meta = set()          # {meta_id}（只收"有名或艺人"的，与判定口径一致）
        self.names = {}            # (归一化艺人, 归一化曲名) -> [时长秒 | None]

    def add(self, rec):
        """把一首本机曲目（`cfg["music_meta"][路径]`）并进索引。"""
        if not isinstance(rec, dict):
            return self
        fp = str(rec.get("fp_id") or "")
        if fp:
            self.fp.add(fp)
        m = _as_md5(rec.get("file_md5"))
        if m:
            self.md5.add(m)
        r_title = str(rec.get("title") or "")
        r_artist = str(rec.get("artist") or "")
        if r_title or r_artist:
            mid = str(rec.get("meta_id") or "") or _meta_key(
                r_title, r_artist, _local_dur(rec))
            if mid:
                self.meta.add(mid)
        key = (_norm(r_artist), _norm(r_title))
        self.names.setdefault(key, []).append(_local_dur(rec))
        return self

    def covers(self, track):
        """这条码表曲目是否已被本机曲库覆盖（四级链，见 `_covered()`）。"""
        if not isinstance(track, dict):
            return False
        # ① 指纹签名
        fp = str(track.get("fp_id") or "")
        if fp and fp in self.fp:
            return True
        # ② 文件内容 md5（`song_id` 进不来，见 `_as_md5()`）
        m = _as_md5(track.get("file_md5"))
        if m and m in self.md5:
            return True
        title = str(track.get("title") or "")
        artist = str(track.get("artist") or "")
        if not (title or artist):
            return False
        # ③ meta_id（码表侧缺失时用 title/artist/时长现算）
        mid = str(track.get("meta_id") or "") or _meta_key(
            title, artist, track.get("duration_sec"))
        if mid and mid in self.meta:
            return True
        # ④ 名 + 艺人 + 时长差 ≤2 秒
        d1 = _dur(track.get("duration_sec"))
        if d1 is not None:
            for d2 in self.names.get((_norm(artist), _norm(title)), ()):
                if d2 is not None and abs(d1 - d2) <= 2.0:
                    return True
        return False


def _track_key(track):
    """一条码表曲目的稳定 key：`fp_id` → `file_md5` → `meta_id` → `title|artist`。"""
    k = str(track.get("fp_id") or "")
    if k:
        return k
    k = _as_md5(track.get("file_md5"))
    if k:
        return k
    k = str(track.get("meta_id") or "")
    if k:
        return k
    return "%s|%s" % (_norm(track.get("title")), _norm(track.get("artist")))


def library_rows(cfg, index=None):
    """码表曲目 → 曲库**派生行**（只列"本机曲库没覆盖"的）。

    ★★ 这些行**绝不落盘**：既不写 `cfg["music_library"]`，也不写 `cfg["music_meta"]`。
       它们是每次 `refresh()` 现算的**派生视图**。所以需求「码表被删/移除，曲库里的
       对应条目一起消失」**天然成立**：派生行没有实体，表没了下一帧就再也算不出来。
       ⇒ **不要**给这里补"级联删除"代码。`music_library` 是用户本机曲库的**唯一真源**，
         一旦级联删错，删掉的是用户硬盘上的真歌（还没法从配置里找回），代价不可接受。

    ★ 只列"本机曲库没覆盖"的曲目（`_covered()` 四级链）——否则同一首歌会出现两次，
      而且码表一删，用户会以为歌没了。

    参数：
      · `cfg` — `Config` 包装对象（**没有 `.get()`**，只能 `cfg["music_meta"]`）；
      · `index` — `CueIndex`（`load_cfg(cfg)` 之后，**纯内存**），缺省视为"没有码表"。

    返回 `[dict]`，每项含：
      `key` / `sheet_path` / `sheet_name` / `title` / `artist` / `duration_sec` /
      `bpm` / `fp_id` / `file_md5` / `meta_id` / `file_name` / `track_key`
    """
    out = []
    try:
        entries = list(getattr(index, "entries", None) or [])
    except Exception:                                            # noqa: BLE001
        entries = []
    if not entries:
        return out                                 # 没有码表 ⇒ 没有派生行（空配置不抛）

    # 本机曲库（只在内存里取一次；Config 没有 .get()，必须 try 兜住）
    local = _LocalIndex()
    try:
        mm = cfg["music_meta"] or {}
    except Exception:                                            # noqa: BLE001
        mm = {}
    if isinstance(mm, dict):
        for _path, rec in mm.items():
            local.add(rec)

    for e in entries:
        if not isinstance(e, dict):
            continue
        pkg = str(e.get("path") or "")
        # 包文件不存在 ⇒ 这张表等于没了 ⇒ 它的派生行**全部**跳过
        # （"删了码表/换了盘/包被移走"三种情况都会落到这里，语义一致）
        try:
            if not pkg or not os.path.isfile(pkg):
                continue
        except Exception:                                        # noqa: BLE001
            continue
        sheet_abs = os.path.abspath(pkg)
        sheet_name = os.path.basename(sheet_abs)
        seen = {}
        for track in (e.get("tracks") or []):
            if not isinstance(track, dict):
                continue
            if local.covers(track):
                continue                                 # 本机已有 ⇒ 不重复列
            title = str(track.get("title") or "")
            artist = str(track.get("artist") or "")
            tkey = _track_key(track)
            n = seen.get(tkey, 0) + 1
            seen[tkey] = n
            # 同一张表里 track_key 撞车（老码表 fp_id/meta_id 全空 + 同名同艺人）：
            # 加序号去重 —— 否则两行同 key 会互相吃勾选状态。
            key = "%s%s#%s" % (CUE_ROW_PREFIX, sheet_abs, tkey)
            if n > 1:
                key = "%s#%d" % (key, n)
            out.append({
                "key": key,
                "sheet_path": sheet_abs,
                "sheet_name": sheet_name,
                "title": title,
                "artist": artist,
                "duration_sec": _dur(track.get("duration_sec")) or 0.0,
                "bpm": _dur(track.get("bpm")) or 0.0,
                "fp_id": str(track.get("fp_id") or ""),
                "file_md5": _as_md5(track.get("file_md5")),
                "meta_id": str(track.get("meta_id") or ""),
                "file_name": str(track.get("file_name") or ""),
                "track_key": tkey,
            })
    return out
