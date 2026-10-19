# -*- coding: utf-8 -*-
"""`.ercue` 包读写 + 版本/能力协商。

★★ 单位边界（架构方案 §3.2 + 2026-10-18 「按指纹对齐」口径）——
   **本文件是整个项目唯一的拍↔秒换算发生地**：
    模型内部坐标 = 秒（`CueItem.at_sec` / `end_sec` / `trans_*_sec`）—— 这是**权威坐标**；
    文件 `.ercue` 里同时写 `at_sec`（秒，权威）与 `at_beats`（拍，DJ 视角 + 冗余派生），
    `end_beats` / `trans_*_beats` 同样在拍。
    `sheet_to_file_dict` / `sheet_from_file_dict` 调用 `units.py` 换算**一次**。

   ★ 写入：`at_beats = sec_to_beats(at_sec, bpm)`（DJ 视角仍是按拍）。
   ★ 读取：**以 `at_sec` 为权威**（有该键就用它，没有才回落 `at_beats`）。
     为什么：演出的位置来自指纹算出的**原曲秒数**，触发判据是
         `at_beats <= pos_beats` ⟺ `at_sec <= offset_sec`
     （`pos_beats = offset_sec / beat_len`、`at_beats = at_sec / beat_len`，
      同一个 `beat_len` 在比较里结构性抵消）
     ⇒ 以秒为权威后 **bpm 完全不影响"什么时候触发"** —— 填错 / 留空 / 手改 `.ercue`
       里的 bpm / 一首歌多段变速，都不会让画面错位。
     反之若"信任 at_beats"，一旦 bpm 与写文件时的值不一致（改过 bpm、第三方生成、
     手改），整张表的秒坐标会被整体缩放 —— 那才是真会错位的情形。
   ⚠ 判"有没有"必须看**键**而不是看值：`at_sec == 0.0` 是合法坐标（片段在第 0 秒）。

包结构（规格书 §2.1）：
    my_track.ercue (zip, ZIP_DEFLATED, compresslevel=6)
    ├── cuesheet.json     编排主数据（必需）
    ├── media/            可选：内嵌素材（embed=True 时才有）
    ├── fp/               可选：曲目指纹哈希序列 `fp/<fp_id>.bin`（★ 新增，向后兼容）
    │                     目的：`identity.fp_id` 只有 16 hex 的摘要，演出端**认不出音频**；
    │                     把完整哈希序列带进包，演出端导入后即可"歌不在本机也能认出"。
    │                     格式见 `model/fp_pack.py`（与演出端 `AutoVJ/src/cue_fp_pack.py`
    │                     是同一格式的两份实现）；老演出端会忽略这个未知成员 ⇒ 不改
    │                     `FORMAT_VERSION`。
    └── thumbs/           可选：缩略图 `<clip_id>.png`（长边 ≤256、保留 alpha；
                          只给用到的素材做；演出端缺图就按"没有缩略图"处理，绝不报错）

本模块 **零 Qt**，可离线自检：`python -m ercue.model.ercue_io`
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from typing import Optional

from . import fp_pack, units
from .units import to_float as _f
from .cuesheet import (
    CueSheet, TrackEntry, Composition, CompLayer, CueItem, MediaRef, Identity,
    FORMAT_VERSION, MIN_FORMAT_VERSION, REQUIRED_RUNTIME_VERSION, _as_list, _i,
)
from .features import SUPPORTED_FEATURES

# ==========================================================================
# 常量（规格书 §2.1 / §3.1）
# ==========================================================================
EXT = ".ercue"
JSON_NAME = "cuesheet.json"
MEDIA_DIR = "media"
THUMB_DIR = "thumbs"
FP_DIR = fp_pack.FP_DIR          # "fp"（★ 与演出端同一命名规则，见 model/fp_pack.py）
# FORMAT_VERSION / MIN_FORMAT_VERSION 定义在 cuesheet（数据版本属于数据模型），此处再导出。

__all__ = [
    "EXT", "JSON_NAME", "MEDIA_DIR", "THUMB_DIR", "FP_DIR", "FORMAT_VERSION", "MIN_FORMAT_VERSION",
    "load", "save", "extract_media", "capability_report", "check_media",
    "fp_member_name", "read_fp_pack", "list_fp_members",
    "sheet_to_file_dict", "sheet_from_file_dict", "new_sheet", "new_track", "selftest",
    "compat_selftest", "state_vs_file_selftest",
]


def _now_iso() -> str:
    """文档记录时间戳（`created_at`）：**这是人类可读的元数据，必须用墙钟**。

    纪律「一切时间戳用 time.monotonic()」针对的是**耗时/节流/看门狗**这类
    相对时间测量（绝不用墙钟）；文档记录的绝对时间不属于此列。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha256_file(path, chunk=1 << 20) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ==========================================================================
# 文件形态 ↔ 模型形态（★ 唯一换算点）
# ==========================================================================
def _item_to_file(it: CueItem, bpm) -> dict:
    """模型 item（秒） → 文件 item（拍主坐标）。字段顺序照规格书 §2.2。"""
    return {
        "item_id": it.item_id, "clip_id": it.clip_id,
        "at_beats": units.sec_to_beats(it.at_sec, bpm),
        "at_sec": _f(it.at_sec),
        "end_beats": (None if it.end_sec is None else units.sec_to_beats(it.end_sec, bpm)),
        "src_in_sec": _f(it.src_in_sec),
        "src_out_sec": (None if it.src_out_sec is None else _f(it.src_out_sec)),
        "speed": _f(it.speed, 1.0),
        "crop": (list(it.crop) if isinstance(it.crop, (list, tuple)) else None),
        "xform": dict(it.xform),
        "trans_in": it.trans_in,
        "trans_in_beats": units.sec_to_beats(it.trans_in_sec, bpm),
        "trans_out": it.trans_out,
        "trans_out_beats": units.sec_to_beats(it.trans_out_sec, bpm),
        "snap": it.snap, "note": it.note,
        # ★ 需求 9（后加的可选字段）：**素材级**混合 / 不透明度。
        #   `""` / `null` = 继承本层（缺字段的旧码表 ⇒ 继承 ⇒ 行为逐值不变，见 CompLayer.effective_blend）。
        "blend": str(getattr(it, "blend", "") or ""),
        "opacity": (None if getattr(it, "opacity", None) is None
                    else _f(it.opacity, 1.0)),
    }


def _item_from_file(d, bpm) -> CueItem:
    """文件 item → 模型 item（秒）。**时间以 `at_sec`（秒）为权威**，缺键才回落 `at_beats`。

    ★ 为什么以秒为权威：演出位置来自指纹算出的**原曲秒数**，触发判据
      `at_beats <= pos_beats` ⟺ `at_sec <= offset_sec`（`beat_len` 在比较里抵消）
      ⇒ bpm 不再参与"什么时候触发"。若反过来信任 `at_beats`，一旦 bpm 与写文件时不一致
        （改过 bpm / 第三方生成 / 手改），整张表的秒坐标会被整体缩放 ⇒ 真会错位。
    ★ `trans_in_sec` / `trans_out_sec` 仍由 `trans_*_beats` 换算（文件里没有秒孪生字段；
      淡入淡出按拍定义，两端同系数换算，自洽）。
    ⚠ 判"有没有"看**键**不看值：`at_sec == 0.0` 是合法坐标（片段在第 0 秒）。
    """
    d = d if isinstance(d, dict) else {}
    crop = d.get("crop")
    if "at_sec" in d:
        at_sec = max(0.0, _f(d.get("at_sec")))
    else:
        at_sec = units.beats_to_sec(d.get("at_beats"), bpm)      # 老包/手写包：逐值不变
    if "end_sec" in d:
        _es = d.get("end_sec")
        end_sec = None if _es is None else max(at_sec, _f(_es))
    else:
        end_sec = (None if d.get("end_beats") is None
                   else units.beats_to_sec(d.get("end_beats"), bpm))
    return CueItem(
        item_id=str(d.get("item_id") or ""), clip_id=str(d.get("clip_id") or ""),
        at_sec=at_sec,
        end_sec=end_sec,
        src_in_sec=_f(d.get("src_in_sec")),
        src_out_sec=(None if d.get("src_out_sec") is None else _f(d.get("src_out_sec"))),
        speed=_f(d.get("speed"), 1.0),
        crop=(list(crop) if isinstance(crop, list) else None),
        xform=(dict(d["xform"]) if isinstance(d.get("xform"), dict)
               else {"scale": 1.0, "x": 0.0, "y": 0.0}),
        trans_in=str(d.get("trans_in") or "fade"),
        trans_in_sec=units.beats_to_sec(d.get("trans_in_beats"), bpm),
        trans_out=str(d.get("trans_out") or "fade"),
        trans_out_sec=units.beats_to_sec(d.get("trans_out_beats"), bpm),
        snap=str(d.get("snap") or "off"), note=str(d.get("note") or ""),
        # ★ 需求 9：素材级混合/不透明度。**旧码表没有这两个键 ⇒ ""/None = 继承本层**
        #   （这即"沿用层值作为该层所有片段的初值"，无需改写旧档；见 CompLayer.effective_blend）。
        blend=str(d.get("blend") or ""),
        opacity=(None if d.get("opacity") is None else _f(d.get("opacity"), 1.0)))


def _layer_to_file(ly: CompLayer, bpm) -> dict:
    return {"lyr_id": ly.lyr_id, "name": ly.name, "z": _i(ly.z),
            "blend": ly.blend, "opacity": _f(ly.opacity, 1.0),
            "items": [_item_to_file(it, bpm) for it in _as_list(ly.items)
                      if isinstance(it, CueItem)]}


def _layer_from_file(d, bpm) -> CompLayer:
    d = d if isinstance(d, dict) else {}
    ly = CompLayer(lyr_id=str(d.get("lyr_id") or ""), name=str(d.get("name") or ""),
                   z=_i(d.get("z")), blend=str(d.get("blend") or "normal"),
                   opacity=_f(d.get("opacity"), 1.0))
    for itd in _as_list(d.get("items")):
        if isinstance(itd, dict):
            ly.items.append(_item_from_file(itd, bpm))
    return ly


def _track_to_file(t: TrackEntry) -> dict:
    bpm = t.identity.bpm
    return {"track_key": t.track_key or "", "identity": t.identity.to_state_dict(),
            "composition": {"layers": [_layer_to_file(ly, bpm) for ly in _as_list(t.layers)
                                       if isinstance(ly, CompLayer)]}}


def _track_from_file(d) -> TrackEntry:
    d = d if isinstance(d, dict) else {}
    ident = Identity.from_state_dict(d.get("identity"))
    ident.ensure_meta_id()
    comp = Composition()
    cdict = d.get("composition")
    layers = cdict.get("layers") if isinstance(cdict, dict) else None
    for lyd in _as_list(layers):
        if isinstance(lyd, dict):
            comp.layers.append(_layer_from_file(lyd, ident.bpm))
    return TrackEntry(track_key=str(d.get("track_key") or ""), identity=ident, composition=comp)


def sheet_to_file_dict(sheet: CueSheet) -> dict:
    """模型 → `.ercue` 的 `cuesheet.json` 形态（规格书 §2.2）。"""
    return {
        "format_version": int(sheet.format_version),
        "generator": dict(sheet.generator or {}),
        "created_at": sheet.created_at or _now_iso(),
        "min_app_version": sheet.min_app_version or "",
        "requires_features": list(sheet.requires_features or []),
        "author": {"name": sheet.author or ""},
        "title": sheet.title or "",
        "notes": sheet.notes or "",
        # ★ 需求 1（后加的可选字段）：画布比例（像素）。旧码表没有 ⇒ 读回时回落 1920×1080。
        "res_w": _i(getattr(sheet, "res_w", 1920), 1920),
        "res_h": _i(getattr(sheet, "res_h", 1080), 1080),
        "media": [m.to_state_dict() for m in sheet.media if isinstance(m, MediaRef)],
        "tracks": [_track_to_file(t) for t in sheet.tracks if isinstance(t, TrackEntry)],
    }


def sheet_from_file_dict(d) -> CueSheet:
    """`.ercue` 的 `cuesheet.json` 形态 → 模型（**容错，绝不抛**）。"""
    s = CueSheet()
    if not isinstance(d, dict):
        return s
    s.format_version = _i(d.get("format_version"), FORMAT_VERSION)
    s.generator = d.get("generator") if isinstance(d.get("generator"), dict) else {}
    s.created_at = str(d.get("created_at") or "")
    s.min_app_version = str(d.get("min_app_version") or "")
    rf = d.get("requires_features")
    s.requires_features = [str(x) for x in rf] if isinstance(rf, list) else []
    au = d.get("author")
    s.author = str((au or {}).get("name") or "") if isinstance(au, dict) else str(au or "")
    s.title = str(d.get("title") or "")
    s.notes = str(d.get("notes") or "")
    # ★ 需求 1：画布比例（可选字段）。**旧码表没有 ⇒ 回落 16:9**（不升 FORMAT_VERSION）。
    s.res_w = _i(d.get("res_w"), 1920)
    s.res_h = _i(d.get("res_h"), 1080)
    for md in _as_list(d.get("media")):
        if isinstance(md, dict) and md.get("clip_id"):
            s.media.append(MediaRef.from_state_dict(md))
    for td in _as_list(d.get("tracks")):
        if isinstance(td, dict):
            s.tracks.append(_track_from_file(td))
    return s.normalized()


# ==========================================================================
# 能力协商（导入时的提示；**一律不阻断导入**）—— 文案原文照规格书 §3.2
# ==========================================================================
def check_media(sheet: CueSheet, media_dir) -> tuple:
    """检查素材是否就位。返回 `(missing_names, present_map)`。"""
    return sheet.check_media(media_dir)


def capability_report(sheet: CueSheet, app_version: str = "", media_dir=None) -> tuple:
    """返回 `(warnings: list[str], missing: list[str])`。

    **四条提示按顺序、文案原文**（规格书 §3.2，顺序也不能变）：
      1. format_version 更高
      2. min_app_version 更高
      3. 有本版不支持的能力
      4. 缺素材
    """
    warn = []
    if sheet.format_version > FORMAT_VERSION:
        warn.append("这张码表来自更新版本的工具（格式 v%d，本版支持到 v%d），"
                    "部分内容可能读不到；已尽力按 v%d 读取。"
                    % (sheet.format_version, FORMAT_VERSION, FORMAT_VERSION))
    if app_version and sheet.min_app_version and sheet.min_app_version > app_version:
        warn.append("这张码表要求 %s 以上版本（当前 %s），部分画面可能不对。"
                    % (sheet.min_app_version, app_version))
    unknown = [f for f in (sheet.requires_features or []) if f not in SUPPORTED_FEATURES]
    if unknown:
        warn.append("这张码表用到以下本版没有的能力，将被忽略：%s" % "、".join(sorted(unknown)))
    miss, _present = (check_media(sheet, media_dir) if media_dir is not None
                      else ([m.name for m in sheet.media], {}))
    if miss:
        warn.append("有 %d 个素材没找到。" % len(miss))
    return warn, miss


# ==========================================================================
# 读 / 写
# ==========================================================================
def _read_json_text(path) -> tuple:
    """从 `.ercue` 或裸 JSON 文件读出 `(json_text, embed_dir_or_None)`。"""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if JSON_NAME not in names:
                cands = [n for n in names if n.endswith(".json") and "/" not in n]
                if not cands:
                    raise ValueError("这个包里没有 %s" % JSON_NAME)
                raw = z.read(cands[0]).decode("utf-8", "replace")
            else:
                raw = z.read(JSON_NAME).decode("utf-8", "replace")
        return raw, path + ".media"
    with open(path, encoding="utf-8") as f:
        return f.read(), None


def load(path, media_dir=None) -> tuple:
    """读 `.ercue`（也接受裸 `cuesheet.json`）。

    返回 `(sheet, info)`：
      info = {"warnings", "missing_media", "present_media", "embedded"}
    **读失败抛 ValueError（调用方负责提示用户）；解析容错在 `sheet_from_file_dict` /
    模型各 `from_state_dict` 里。**
    """
    if not path or not os.path.isfile(path):
        raise ValueError("找不到文件：%s" % path)
    raw, embed_dir = _read_json_text(path)
    try:
        d = json.loads(raw)
    except Exception as e:                                    # noqa: BLE001
        raise ValueError("码表内容不是合法 JSON：%s" % e)
    sheet = sheet_from_file_dict(d)
    md = media_dir if media_dir is not None else (
        embed_dir if embed_dir and os.path.isdir(embed_dir) else None)
    missing, present = check_media(sheet, md)
    # load 阶段不知道 app_version（调用方持有），第 2 条告警由调用方再跑 capability_report。
    warn, miss = capability_report(sheet, app_version="", media_dir=md)
    return sheet, {"warnings": warn, "missing_media": miss, "present_media": present,
                   "embedded": bool(embed_dir and os.path.isdir(embed_dir))}


def extract_media(pkg_path, dest_dir) -> dict:
    """把包内 `media/` 解到 `dest_dir`，返回 `{basename: path}`。"""
    out = {}
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(pkg_path) as z:
        for n in z.namelist():
            if not n.startswith(MEDIA_DIR + "/") or n.endswith("/"):
                continue
            base = os.path.basename(n)
            if not base:
                continue
            dst = os.path.join(dest_dir, base)
            with z.open(n) as src, open(dst, "wb") as f:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            out[base] = dst
    return out


def save(sheet: CueSheet, path, media_paths=None, embed=False,
         app_version="", generator=None, thumbs=None, fp_packs=None) -> str:
    """写 `.ercue`。

    `media_paths`: `{clip_id: 磁盘路径}`，用于内嵌与 sha256 计算。
    `embed=False`（默认）只写清单，不内嵌素材（包只有几 KB）。
    `thumbs`: `{clip_id: PNG 字节}`（可选）⇒ 写进包内 `thumbs/<clip_id>.png`。
        ★ 两端契约（2026-10-18 与演出端约定）：只给**用到的**素材做、PNG、长边 ≤256、
          alpha 保留；演出端缺某个 `clip_id` 的 png 就按"没有缩略图"处理，**绝不报错**。
        ⚠ zip 内路径分隔符**必须写 `/`**（不能用 `os.sep`，Windows 会写成 `\\`）。
        ⚠ 这是**向后兼容的新增**：老演出端读到 `thumbs/` 会忽略 ⇒ 不改 `FORMAT_VERSION`。
    `fp_packs`: `{fp_id: 打包字节}`（可选）⇒ 写进包内 `fp/<fp_id>.bin`。
        ★ 用途（用户原话「理论来讲 era 导入就应该要识别到」）：`identity.fp_id` 只是
          **16 个字符的摘要**，演出端拿它认不出音频；把**完整哈希序列**带进包，
          演出端导入后灌进自己的指纹库 ⇒ **歌不在那台机器上也能认出来**。
        ★ 字节必须由 `model/fp_pack.pack_hashes()` 产出（与演出端同一格式）；
          本函数只负责"放进 zip"，不解释内容。
        ⚠ 空字节 / `fp_id` 为空 / `fp_id` 含路径分隔符 ⇒ **跳过这个成员**（不报错）。
        ⚠ 分隔符同样硬编码 `/`；老演出端会忽略未知成员 ⇒ **不改 `FORMAT_VERSION`**。
    """
    sheet.recompute_features()
    t = sheet_to_file_dict(sheet)
    t["generator"] = dict(generator or {"tool": "EasyRealityCue", "version": app_version})
    # ★★ 2026-10-18 修复：`min_app_version` 写的是**演出端最低版本**，以前错填了编排端
    #   自己的构建号 ⇒ 演出端拿它自己的构建号去比，永远差一格 ⇒ 每次导入都误报
    #   「这张码表要求 X 以上版本（当前 X-1）」（真实用户截图）。
    #   `generator.version` 才是编排端版本（那个保持不变 —— 它本来就该写自己）。
    t["min_app_version"] = sheet.min_app_version or REQUIRED_RUNTIME_VERSION
    if not t.get("created_at"):
        t["created_at"] = _now_iso()

    media_paths = media_paths or {}
    # ★ 2026-10-19 审计修复：先把 embedded 一律置 False，只有"文件确实在、且本次真的写进包"
    #   才置 True。旧写法只在文件存在时改它 ⇒ 文件丢失/旧包会把 model 里的 True 原样带出去，
    #   演出端据此以为素材已内嵌（找不到文件也不报警）。
    for _m in t["media"]:
        _m["embedded"] = False
    for m in t["media"]:
        p = media_paths.get(m["clip_id"])
        if p and os.path.isfile(p):
            if not m.get("sha256"):
                m["sha256"] = _sha256_file(p)
            if not m.get("name"):
                m["name"] = os.path.basename(p)
            m["embedded"] = bool(embed)
            m["_src"] = p                          # 仅本次写包用，不写进 json

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if embed:
            for m in t["media"]:
                p = m.pop("_src", None)
                if p and os.path.isfile(p):
                    z.write(p, "%s/%s" % (MEDIA_DIR, m["name"]))
        for m in t["media"]:
            m.pop("_src", None)
        z.writestr(JSON_NAME, json.dumps(t, ensure_ascii=False, indent=1))
        # ★ 缩略图（可选，向后兼容的新增）：`thumbs/<clip_id>.png`。
        #   ⚠ 分隔符硬编码 `/`（绝不用 os.sep —— Windows 会写成 `\`，演出端解不出来）。
        for cid in sorted(thumbs or {}):
            data = thumbs[cid]
            if not data:
                continue                          # 没图 = 不写；演出端按"没有缩略图"处理
            name = _thumb_name(cid)
            if not name:
                continue
            z.writestr("%s/%s" % (THUMB_DIR, name), data)
        # ★ 曲目指纹哈希序列（可选，向后兼容的新增）：`fp/<fp_id>.bin`。
        #   只写**用得到且有内容**的曲目；`fp_id` 为空 / 非法 / 内容为空一律跳过。
        for fid in sorted(fp_packs or {}):
            blob = fp_packs[fid]
            if not blob or not isinstance(blob, (bytes, bytearray, memoryview)):
                continue
            name = fp_pack.member_name(fid)
            if not name:
                continue
            z.writestr(name, bytes(blob))
    return path


def fp_member_name(fp_id) -> str:
    """`fp_id` → 包内成员名（`fp/<fp_id>.bin`）；不安全/为空 ⇒ `""`。

    直接转发 `model/fp_pack.member_name()` —— **命名规则只有一处实现**
    （两端都按它还原文件名，绝不能各写一套）。
    """
    return fp_pack.member_name(fp_id)


def list_fp_members(pkg_path) -> list:
    """列出包内所有指纹包成员名（`fp/xxx.bin`）；不是 zip / 读失败 ⇒ `[]`（绝不抛）。"""
    try:
        with zipfile.ZipFile(pkg_path) as z:
            return [n for n in z.namelist() if fp_pack.is_member(n)]
    except Exception:                                          # noqa: BLE001
        return []


def read_fp_pack(pkg_path, fp_id) -> Optional[bytes]:
    """从包里读某个 `fp_id` 的指纹包字节；没有/读失败 ⇒ `None`（绝不抛）。

    给"排查导出对不对"和测试用（**生产路径不需要**：演出端自己解 zip）。
    """
    name = fp_pack.member_name(fp_id)
    if not name:
        return None
    try:
        with zipfile.ZipFile(pkg_path) as z:
            return z.read(name)
    except Exception:                                          # noqa: BLE001
        return None


def _thumb_name(clip_id) -> str:
    """`clip_id` → 包内缩略图文件名（`<clip_id>.png` 的 `<clip_id>` 部分）。

    只做**必要的**清洗（去掉路径分隔符与 `..`），其余原样保留 —— 因为文件名必须与
    `cuesheet.json` 里的 `clip_id` 一致，演出端就是按它查缩略图的。
    洗不出安全名字时返回 ""（调用方跳过 ⇒ 该素材没有缩略图，**不报错**）。
    """
    s = str(clip_id or "").strip()
    if not s:
        return ""
    if s in (".", ".."):
        return ""
    bad = ("/", "\\", ":", "*", "?", '"', "<", ">", "|", "\x00")
    if any(b in s for b in bad):
        return ""
    return s + ".png"


# ==========================================================================
# 新建
# ==========================================================================
def new_sheet(title="新建码表", author="", res_w=1920, res_h=1080) -> CueSheet:
    """新建码表。`res_w/res_h` = 画布比例（像素，需求 1）；非法回落 16:9（`CueSheet.normalized`）。"""
    return CueSheet(title=title, author=author, created_at=_now_iso(),
                    res_w=_i(res_w, 1920), res_h=_i(res_h, 1080))


def new_track(title="", artist="", duration_sec=0.0, bpm=0.0) -> TrackEntry:
    te = TrackEntry()
    te.identity = Identity(title=title, artist=artist, duration_sec=duration_sec, bpm=bpm)
    te.identity.meta_id = Identity.meta_key(title, artist, duration_sec)
    te.track_key = te.identity.meta_id[:12] or "t1"
    return te


# ==========================================================================
# 双向格式兼容验证
# ==========================================================================
# 规格书 §2.3 真实示例 JSON（按旧实现 to_dict 精确还原）
SPEC_SAMPLE_JSON = {
    "format_version": 1,
    "generator": {"tool": "Easy Reality AutoVJ", "version": "2026.09.29.03"},
    "created_at": "2026-09-29T03:07:11+0800",
    "min_app_version": "2026.09.29.03",
    "requires_features": ["clip_crop", "clip_fade", "clip_inout", "clip_speed",
                          "layer_multi", "beat_snap", "trans_glitch"],
    "author": {"name": ""},
    "title": "自检码表",
    "notes": "",
    "media": [
        {"clip_id": "c1", "name": "a.mp4", "sha256": "", "embedded": False,
         "duration_sec": 12.5, "has_alpha": False},
        {"clip_id": "c2", "name": "b.mp4", "sha256": "", "embedded": False,
         "duration_sec": 0.0, "has_alpha": False},
    ],
    "tracks": [
        {
            "track_key": "rollingcontact|cosmicstring|372",
            "identity": {
                "fp_id": "", "meta_id": "rollingcontact|cosmicstring|372",
                "title": "Cosmic String", "artist": "Rolling Contact",
                "duration_sec": 372.4, "bpm": 155.03, "key": "",
                "file_name": "Rolling Contact - Cosmic String.flac",
            },
            "composition": {
                "layers": [
                    {
                        "lyr_id": "L1", "name": "背景", "z": 0, "blend": "screen", "opacity": 0.9,
                        "items": [
                            {
                                "item_id": "i1", "clip_id": "c1",
                                "at_beats": 32.0, "at_sec": 12.384,
                                "end_beats": None,
                                "src_in_sec": 2.0, "src_out_sec": None,
                                "speed": 1.25,
                                "crop": [0.05, 0.0, 0.9, 1.0],
                                "xform": {"scale": 1.0, "x": 0.0, "y": 0.0},
                                "trans_in": "glitch", "trans_in_beats": 2.0,
                                "trans_out": "fade", "trans_out_beats": 2.0,
                                "snap": "bar", "note": "副歌进来",
                            },
                            {
                                "item_id": "i2", "clip_id": "c2",
                                "at_beats": 96.0, "at_sec": 37.152, "end_beats": None,
                                "src_in_sec": 0.0, "src_out_sec": None, "speed": 1.0,
                                "crop": None, "xform": {"scale": 1.0, "x": 0.0, "y": 0.0},
                                "trans_in": "cut", "trans_in_beats": 2.0,
                                "trans_out": "cut", "trans_out_beats": 2.0,
                                "snap": "bar", "note": "",
                            },
                        ],
                    }
                ]
            },
        }
    ],
}

_OLD_SRC = r"P:\AutoVJ_码表备份_20260930\src"


def _load_old_impl():
    """尝试把旧实现 `cuesheet.py` 独立 import。失败返回 None（走 json 比对降级）。"""
    if not os.path.isfile(os.path.join(_OLD_SRC, "cuesheet.py")):
        return None
    import importlib.util
    import sys
    try:
        spec = importlib.util.spec_from_file_location("_old_cuesheet",
                                                      os.path.join(_OLD_SRC, "cuesheet.py"))
        mod = importlib.util.module_from_spec(spec)
        # dataclasses 处理 `cls.__module__` 时会查 sys.modules，必须先注册再 exec。
        sys.modules["_old_cuesheet"] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:                                          # noqa: BLE001
        import traceback
        traceback.print_exc()
        return None


def compat_selftest() -> bool:
    """★ 双向格式兼容验证（旧 → 新；新 → 旧）。"""
    import tempfile

    bpm = SPEC_SAMPLE_JSON["tracks"][0]["identity"]["bpm"]
    tmp = tempfile.mkdtemp(prefix="ercue_cmp_")

    # ---------- 旧 → 新（真实文件）：模拟旧实现导出的 .ercue zip，用 load() 读 ----------
    p_old = os.path.join(tmp, "old" + EXT)
    with zipfile.ZipFile(p_old, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(JSON_NAME, json.dumps(SPEC_SAMPLE_JSON, ensure_ascii=False, indent=1))
    sheet_file, info_file = load(p_old)
    assert sheet_file.title == "自检码表" and len(sheet_file.tracks) == 1
    # ★ 时间以秒为权威：示例包里的 at_sec=12.384（旧实现手写时按 3 位小数记录）
    #   ⇒ 读回就是 12.384，**不再**按 at_beats×beat_len 反算（那会是 12.384699…）。
    assert abs(sheet_file.tracks[0].layers[0].items[0].at_sec - 12.384) < 1e-9
    assert len(info_file["missing_media"]) == 2   # 包内无素材 → 全缺

    # ---------- 旧 → 新（字段逐一核对）----------
    sheet = sheet_from_file_dict(SPEC_SAMPLE_JSON)
    assert sheet.format_version == 1
    assert sheet.title == "自检码表"
    assert sheet.author == "" and sheet.notes == ""
    assert sheet.min_app_version == "2026.09.29.03"
    assert sorted(sheet.requires_features) == sorted(SPEC_SAMPLE_JSON["requires_features"])
    assert sheet.generator["tool"] == "Easy Reality AutoVJ"
    assert len(sheet.media) == 2
    assert sheet.media[0].clip_id == "c1" and sheet.media[0].name == "a.mp4"
    assert abs(sheet.media[0].duration_sec - 12.5) < 1e-9
    assert sheet.media[1].clip_id == "c2" and sheet.media[1].has_alpha is False
    assert len(sheet.tracks) == 1
    tr = sheet.tracks[0]
    assert tr.track_key == "rollingcontact|cosmicstring|372"
    idn = tr.identity
    assert idn.meta_id == "rollingcontact|cosmicstring|372"
    assert idn.title == "Cosmic String" and idn.artist == "Rolling Contact"
    assert abs(idn.duration_sec - 372.4) < 1e-9 and abs(idn.bpm - 155.03) < 1e-9
    assert idn.file_name == "Rolling Contact - Cosmic String.flac"
    assert len(tr.layers) == 1
    ly = tr.layers[0]
    assert (ly.lyr_id, ly.name, ly.z, ly.blend) == ("L1", "背景", 0, "screen")
    assert abs(ly.opacity - 0.9) < 1e-9
    assert len(ly.items) == 2
    i1 = ly.items[0]
    # ★ 秒坐标 = 文件里的 at_sec（**权威**）；at_beats 只是 DJ 视角的派生值。
    #   示例包的 at_sec 手写成 3 位小数（12.384），与 at_beats×beat_len（12.384699…）
    #   差 0.7 ms —— 这正是"以秒为权威"的意义：不再让 bpm 参与坐标反算。
    assert abs(i1.at_sec - 12.384) < 1e-9
    # 示例包的 at_sec 是手写 3 位小数、bpm 也已四舍五入 ⇒ 与拍坐标差 ~0.7ms 属正常
    assert abs(i1.at_sec - units.beats_to_sec(32.0, bpm)) < 1e-2
    assert i1.end_sec is None
    assert abs(i1.src_in_sec - 2.0) < 1e-9 and i1.src_out_sec is None
    assert abs(i1.speed - 1.25) < 1e-9 and i1.crop == [0.05, 0.0, 0.9, 1.0]
    assert i1.xform == {"scale": 1.0, "x": 0.0, "y": 0.0}
    assert i1.trans_in == "glitch" and abs(i1.trans_in_sec - units.beats_to_sec(2.0, bpm)) < 1e-9
    assert i1.trans_out == "fade" and i1.snap == "bar" and i1.note == "副歌进来"
    i2 = ly.items[1]
    assert abs(i2.at_sec - 37.152) < 1e-9 and i2.crop is None       # 同样以 at_sec 为权威
    assert abs(i2.at_sec - units.beats_to_sec(96.0, bpm)) < 1e-2    # 同上（差 ~2ms）
    assert i2.trans_in == "cut" and i2.trans_out == "cut"

    # ---------- 新 → 旧：写包，再用旧实现读回 ----------
    p = os.path.join(tmp, "t" + EXT)
    save(sheet, p, app_version="2026.10.01.01")

    old = _load_old_impl()
    route = "真跑旧代码"
    if old is not None:
        s_old, info_old = old.load(p)
        assert s_old.title == "自检码表", s_old.title
        assert len(s_old.tracks) == 1 and len(s_old.tracks[0].layers) == 1
        it_old = s_old.tracks[0].layers[0].items[0]
        # ★ 时间以秒为权威后：示例包 at_sec=12.384（手写 3 位小数）⇒ 再导出时
        #   at_beats 由秒重算 = 31.9983（原来手写的 32.0 差 0.0017 拍 ≈ 0.7ms）。
        #   旧实现读的仍是"拍"字段，所以这里给 1e-2 的容差，而不是逐字相等。
        assert abs(it_old.at_beats - 32.0) < 1e-2, it_old.at_beats
        assert abs(it_old.speed - 1.25) < 1e-9
        assert it_old.crop == [0.05, 0.0, 0.9, 1.0]
        assert it_old.trans_in == "glitch" and it_old.snap == "bar"
        assert it_old.end_beats is None and it_old.note == "副歌进来"
        assert s_old.tracks[0].layers[0].blend == "screen"
        assert abs(s_old.tracks[0].identity.bpm - 155.03) < 1e-9
        assert abs(it_old.trans_in_beats - 2.0) < 1e-9                  # 拍时长逐字一致
    else:
        route = "json 逐字段比对（旧实现无法独立 import）"
        with zipfile.ZipFile(p) as z:
            d2 = json.loads(z.read(JSON_NAME).decode("utf-8"))
        it2 = d2["tracks"][0]["composition"]["layers"][0]["items"][0]
        assert abs(it2["at_beats"] - units.sec_to_beats(12.384, bpm)) < 1e-9
        assert abs(it2["at_beats"] - 32.0) < 1e-2        # 与手写的 32.0 差 0.0017 拍
        assert abs(it2["trans_in_beats"] - 2.0) < 1e-9
        assert it2["crop"] == [0.05, 0.0, 0.9, 1.0] and it2["trans_in"] == "glitch"

    # ---------- 结构一致性（与原始样例逐字段比对，允许 at_sec 被主坐标重算）----------
    with zipfile.ZipFile(p) as z:
        d2 = json.loads(z.read(JSON_NAME).decode("utf-8"))
    _assert_file_struct_equal(d2, SPEC_SAMPLE_JSON, bpm)

    print("[ercue_io.compat] 双向兼容通过 ✓  走法=%s  包=%.1f KB"
          % (route, os.path.getsize(p) / 1024.0))
    return True


def _assert_file_struct_equal(got, want, bpm):
    """逐字段核对「无丢失、无错位」。**`at_sec`（秒）必须逐字保留，`at_beats` 允许按秒重算。**"""
    for k in ("format_version", "title", "notes", "min_app_version"):
        assert got[k] == want[k], (k, got[k], want[k])
    assert got["author"]["name"] == want["author"]["name"]
    assert len(got["media"]) == len(want["media"])
    for gm, wm in zip(got["media"], want["media"]):
        assert gm == wm, (gm, wm)
    assert len(got["tracks"]) == len(want["tracks"])
    for gt, wt in zip(got["tracks"], want["tracks"]):
        assert gt["track_key"] == wt["track_key"]
        # ★ `file_md5` 是**后加的可选字段**（旧样例没有它，旧码表也不会有）：
        #   比对时把它剥掉再逐字比其余字段（样例里若也有，则照样比），
        #   避免"新增一个可选字段就破坏规格样例一致性自检"。
        gi = dict(gt["identity"])
        wi = dict(wt["identity"])
        gm = gi.pop("file_md5", None)
        wm = wi.pop("file_md5", None)
        assert gi == wi, (gi, wi)
        if wm is not None:
            assert gm == wm, (gm, wm)
        g_layers = gt["composition"]["layers"]
        w_layers = wt["composition"]["layers"]
        assert len(g_layers) == len(w_layers)
        for gl, wl in zip(g_layers, w_layers):
            for k in ("lyr_id", "name", "z", "blend", "opacity"):
                assert gl[k] == wl[k], (k, gl[k], wl[k])
            assert len(gl["items"]) == len(wl["items"])
            for gi, wi in zip(gl["items"], wl["items"]):
                # ★ 时间以**秒**为权威：at_sec 必须逐字保留；at_beats 由秒重算
                #   （老样例里 at_beats/at_sec 不自洽时，重算结果会与手写的拍值差一点点，
                #    那正是"以秒为准"该有的行为）。
                want_sec = (float(wi["at_sec"]) if wi.get("at_sec") is not None
                            else units.beats_to_sec(wi["at_beats"], bpm))
                assert abs(gi["at_sec"] - want_sec) < 1e-9, (gi["at_sec"], want_sec)
                assert abs(gi["at_beats"] - units.sec_to_beats(want_sec, bpm)) < 1e-9, \
                    (gi["at_beats"], units.sec_to_beats(want_sec, bpm))
                for k in ("item_id", "clip_id", "end_beats", "src_in_sec", "src_out_sec",
                          "speed", "crop", "xform", "trans_in", "trans_in_beats",
                          "trans_out", "trans_out_beats", "snap", "note"):
                    assert gi[k] == wi[k], (k, gi[k], wi[k])


# ==========================================================================
# 自检
# ==========================================================================
def state_vs_file_selftest() -> bool:
    """★ 钉死「两个 dict 不是一回事」：**文件形态含拍、状态快照不含拍**。

    背景：旧实现头号血案就是「同一概念两套坐标表示」。此处把歧义变成断言 ——
    看见 `sheet_to_file_dict()` 就知道拿到的是 §2.2 文件形态（拍主坐标），
    看见 `sheet.to_state_dict()` 就知道拿到的是内部状态快照（秒坐标）。
    """
    sheet = sheet_from_file_dict(SPEC_SAMPLE_JSON)

    # 文件形态：必须有拍字段（§2.2 主坐标 + 冗余 + 拍时长）
    fd = sheet_to_file_dict(sheet)
    f_item = fd["tracks"][0]["composition"]["layers"][0]["items"][0]
    for k in ("at_beats", "at_sec", "end_beats", "trans_in_beats", "trans_out_beats", "src_in_sec"):
        assert k in f_item, ("文件形态缺字段", k, list(f_item.keys()))

    # 状态快照：**不得**出现任何拍字段，坐标一律秒
    sd = sheet.to_state_dict()
    s_item = sd["tracks"][0]["composition"]["layers"][0]["items"][0]
    for k in ("at_beats", "end_beats", "trans_in_beats", "trans_out_beats"):
        assert k not in s_item, ("状态快照不应含拍字段", k, list(s_item.keys()))
    for k in ("at_sec", "end_sec", "trans_in_sec", "trans_out_sec", "src_in_sec"):
        assert k in s_item, ("状态快照缺秒字段", k, list(s_item.keys()))

    # 整份状态快照里都不该出现 "beats"（秒形态 + 撤销栈可 JSON 序列化）
    blob = json.dumps(sd, ensure_ascii=False)
    assert "beats" not in blob, "状态快照里出现了拍字段"
    json.dumps(sd, ensure_ascii=False)   # 可序列化（撤销栈快照要求）

    print("[ercue_io.state] 状态快照(秒) ≠ 文件形态(拍) 已钉死 ✓  状态 item 键=%s"
          % ",".join(s_item.keys()))
    return True


def selftest() -> bool:
    """IO / 版本协商 / 容错 / 双向兼容 自检。"""
    import tempfile

    # 1) 版本协商四条文案（顺序与原文）
    future = sheet_from_file_dict(dict(SPEC_SAMPLE_JSON,
                                       format_version=FORMAT_VERSION + 5,
                                       min_app_version="2099.01.01.00",
                                       requires_features=["clip_crop", "未来的能力"]))
    w, _ = capability_report(future, app_version="2026.10.01.01", media_dir=None)
    assert len(w) == 4, w
    assert "更新版本的工具（格式 v6，本版支持到 v1）" in w[0], w[0]
    assert w[1] == "这张码表要求 2099.01.01.00 以上版本（当前 2026.10.01.01），部分画面可能不对。", w[1]
    assert w[2] == "这张码表用到以下本版没有的能力，将被忽略：未来的能力", w[2]
    assert w[3] == "有 2 个素材没找到。", w[3]

    # 2) zip 往返
    tmp = tempfile.mkdtemp(prefix="ercue_io_")
    p = os.path.join(tmp, "round" + EXT)
    s = new_sheet("往返码表", "unit-test")
    te = new_track("Cosmic String", "Rolling Contact", 372.4, 155.03)
    ly = CompLayer(lyr_id="L1", name="背景", z=0, blend="screen", opacity=0.9)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=12.384, src_in_sec=2.0,
                            speed=1.25, crop=[0.05, 0.0, 0.9, 1.0],
                            trans_in="glitch", trans_out="fade").normalized())
    ly.items.append(CueItem(item_id="i2", clip_id="c2", at_sec=37.0,
                            trans_in="cut", trans_out="cut").normalized())
    te.composition.layers.append(ly)
    s.tracks.append(te)
    s.media.append(MediaRef(clip_id="c1", name="a.mp4"))
    s.media.append(MediaRef(clip_id="c2", name="b.mp4"))
    save(s, p, app_version="2026.10.01.01")
    s2, info = load(p)
    assert s2.title == "往返码表" and len(s2.tracks[0].layers[0].items) == 2
    assert abs(s2.tracks[0].layers[0].items[0].at_sec - 12.384) < 1e-9
    assert len(info["missing_media"]) == 2 and any("素材没找到" in x for x in info["warnings"])

    # 3) 裸 JSON 可读
    pj = os.path.join(tmp, "bare.json")
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(SPEC_SAMPLE_JSON, f, ensure_ascii=False)
    sb, _ = load(pj)
    assert sb.title == "自检码表" and len(sb.tracks) == 1

    # 4) zip 里没有 cuesheet.json → 找第一个顶层 *.json
    pz = os.path.join(tmp, "other" + EXT)
    with zipfile.ZipFile(pz, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("weird_name.json", json.dumps(SPEC_SAMPLE_JSON, ensure_ascii=False))
        z.writestr("sub/inner.json", "{}")
    sz, _ = load(pz)
    assert sz.title == "自检码表"

    # 5) 非法 JSON → ValueError
    pbad = os.path.join(tmp, "bad" + EXT)
    with open(pbad, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json ")
    try:
        load(pbad)
        raise AssertionError("应抛 ValueError")
    except ValueError as e:
        assert "不是合法 JSON" in str(e), e

    # 6) 找不到文件 → ValueError
    try:
        load(os.path.join(tmp, "nope" + EXT))
        raise AssertionError("应抛 ValueError")
    except ValueError as e:
        assert "找不到文件" in str(e), e

    # 7) 脏数据不崩
    s4 = sheet_from_file_dict({"tracks": [{"composition": {"layers": [{"items": [{"at_beats": "xx"}]}]}}]})
    assert isinstance(s4, CueSheet)
    assert s4.tracks[0].layers[0].items[0].at_sec == 0.0
    s5 = sheet_from_file_dict({"tracks": "坏", "media": 3, "requires_features": "x"})
    assert isinstance(s5, CueSheet) and s5.tracks == [] and s5.media == []

    # 8) 双向兼容
    compat_selftest()

    # 9) 状态快照(秒) 与 文件形态(拍) 不混用
    state_vs_file_selftest()

    print("[ercue_io] 自检通过 ✓  包=%s（%.1f KB）"
          % (os.path.basename(p), os.path.getsize(p) / 1024.0))
    return True


if __name__ == "__main__":
    selftest()
