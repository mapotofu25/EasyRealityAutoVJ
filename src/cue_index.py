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
