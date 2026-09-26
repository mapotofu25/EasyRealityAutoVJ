# -*- coding: utf-8 -*-
"""读取 VirtualDJ 分析库（database.xml）里的 BPM / 网格锚点 / 段落标记。

VirtualDJ 的网格只有两个参数，**网格线就是纯数学**：
    拍位 t = anchor + n * beatLen        (n 为整数，可正可负)

数据库字段（`<Song>` 下）：
    <Scan Bpm="0.468753" Phase="1.101406" AltBpm="0.580658" Key="C#" Version="801"/>
        ⚠ `Bpm` **不是 BPM**，而是「每拍的秒数」→ 真实 BPM = 60 / Bpm
        ⚠ `Phase` = 网格锚点（第一个拍的位置，秒），与 `<Poi Type="beatgrid">` 同值
        ⚠ `AltBpm` = 另一个候选拍长（通常是 1.5×Bpm，即 2/3 BPM 的半速值）
    <Poi Name="Break 1" Pos="118.759" Type="remix"/>   ← 自动段落标记
    <Poi Pos="91.122" Type="cue"/>                      ← 用户设的 cue

用法（命令行）：
    python tools/vdj_grid.py                       # 列出库里 BPM 分布摘要
    python tools/vdj_grid.py "Dust"                # 按文件名关键字查
    python tools/vdj_grid.py --db <database.xml> "outburst"
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

# 复用生产模块的自动探测（不硬编码本机路径）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
try:
    import beatgrid as _bg
    DEFAULT_DB = _bg.vdj_db_path()
except Exception:                                   # noqa: BLE001
    DEFAULT_DB = ""


def load_db(path=DEFAULT_DB):
    """返回 {规范化路径: {bpm, beat_len, anchor, key, dur, remix:[(name,pos)], cues:[pos]}}"""
    root = ET.parse(path).getroot()
    out = {}
    for s in root.iter("Song"):
        fp = s.get("FilePath")
        if not fp:
            continue
        scan = s.find("Scan")
        infos = s.find("Infos")
        e = {"path": fp, "bpm": 0.0, "beat_len": 0.0, "anchor": None,
             "key": "", "dur": 0.0, "remix": [], "cues": []}
        if infos is not None and infos.get("SongLength"):
            e["dur"] = float(infos.get("SongLength"))
        if scan is not None and scan.get("Bpm"):
            bl = float(scan.get("Bpm"))
            e["beat_len"] = bl
            e["bpm"] = 60.0 / bl if bl > 0 else 0.0
            ph = scan.get("Phase")
            if ph is not None:
                e["anchor"] = float(ph)
            e["key"] = scan.get("Key") or ""
        for poi in s.findall("Poi"):
            ty = poi.get("Type")
            try:
                pos = float(poi.get("Pos"))
            except (TypeError, ValueError):
                continue
            if ty == "remix":
                e["remix"].append((poi.get("Name") or "", pos))
            elif ty == "cue":
                e["cues"].append(pos)
        out[os.path.normcase(fp)] = e
    return out


def find(db, keyword):
    """按路径关键字（不区分大小写）筛选，按路径排序。"""
    k = keyword.lower()
    return sorted([e for e in db.values() if k in (e["path"] or "").lower()],
                  key=lambda e: e["path"])


def grid_times(entry, t0, t1):
    """按 VDJ 网格列出 [t0,t1] 内的拍位（秒）。anchor 缺失返回 []。"""
    if not entry.get("beat_len") or entry.get("anchor") is None:
        return []
    bl, a = entry["beat_len"], entry["anchor"]
    n0 = int((t0 - a) / bl) - 1
    n1 = int((t1 - a) / bl) + 2
    return [a + n * bl for n in range(n0, n1 + 1)]


def nearest_downbeat(entry, t):
    """返回距 `t` 最近的「小节头」（每 4 拍）时间。

    注意：VDJ 把锚点视作第 1 拍（downbeat），所以小节头 = anchor + 4n*beatLen。"""
    if not entry.get("beat_len") or entry.get("anchor") is None:
        return None
    bl, a = entry["beat_len"], entry["anchor"]
    bar = 4 * bl
    n = round((t - a) / bar)
    return a + n * bar


def main():
    args = [x for x in sys.argv[1:]]
    db_path = DEFAULT_DB
    if "--db" in args:
        i = args.index("--db")
        db_path = args[i + 1]
        del args[i:i + 2]
    db = load_db(db_path)
    print("库: %s（%d 首）" % (db_path, len(db)))
    if not args:
        import collections
        c = collections.Counter()
        unanalyzed = 0
        for e in db.values():
            if e["anchor"] is None:
                unanalyzed += 1
            else:
                c[round(e["bpm"])] += 1
        print("未分析（无锚点）: %d 首" % unanalyzed)
        print("BPM 分布 top15: %s" % c.most_common(15))
        return 0
    for kw in args:
        hits = find(db, kw)
        print("\n=== 「%s」→ %d 条 ===" % (kw, len(hits)))
        for e in hits:
            if e["anchor"] is None:
                print("  [未分析] %s" % e["path"])
                continue
            print("  %s" % e["path"])
            print("     BPM %7.2f  拍长 %.6fs  锚点 %.3fs  Key %-4s 时长 %.1fs"
                  % (e["bpm"], e["beat_len"], e["anchor"], e["key"], e["dur"]))
            if e["remix"]:
                print("     段落: " + ", ".join("%s@%.1f" % (n, p)
                                                for n, p in sorted(e["remix"], key=lambda x: x[1])[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
