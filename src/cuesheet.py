# -*- coding: utf-8 -*-
"""码表（Cue Sheet）数据模型 + `.ercue` 包读写 + 版本/能力协商。

★ 定位：**演出端只读**。编排（编辑/预览/导出）由独立软件 EasyRealityCue 承担；
  本模块只负责"把 DJ 导出的 `.ercue` 读进内存，给运行时解释器用"。

核心约定（别改）：
  · **逐曲码表**：一首歌一份编排（`TrackEntry`），不做"整场一条长轴"。
  · **位置主坐标是"拍"**（`CueItem.at_beats`），`at_sec` 只是冗余备份。
    变速不改变拍/相位，只改变秒 —— 所以存拍天然免疫变速。
    ⇒ 演出端与位置源（指纹给的歌内秒 → 拍）**同一坐标系**，不再二次换算。
  · **`.ercue` = zip 容器**：`cuesheet.json`（必需）+ `media/`（可选内嵌素材）+ `thumbs/`（可选，忽略）。
  · **版本与能力分离**：
      `format_version`  文件结构版本，**只有结构变了才 +1**（可能一年一次）
      `min_app_version` 这张码表要求主程序 ≥ 某版本（由编辑器自动算）
      `generator`       谁、在什么系统上导出的（仅排查用）
    ⚠ **绝不能拿程序版本号当格式版本**（每次发版都会把旧码表误判成旧格式）。

★★ 与 Cue 端（`EasyRealityCue/src/ercue/model/`）的对齐纪律
  Cue 端模型内部是**秒**坐标（`ercue_io` 是唯一拍↔秒换算点）；演出端模型内部是**拍**坐标。
  两边**不是同一套模型**，但对同一个包必须得到**同一组数值**：
    · 文件字段名/类型/缺省语义逐项对齐（`res_w/res_h`、`identity.file_md5`、`item.blend/opacity`）；
    · 拍↔秒换算的**唯一公式**与退化规则逐字一致（见下方 `beat_len`）；
    · 认歌五级链 `md5 → fingerprint → meta → title+artist → file` 与 Cue 端 `match_track` 一致。
  任一偏差 ⇒ 现场与预览不一致（整条码表按比例错位）。改动前先跑 `tools/twoside_parity_check.py`。

本模块只做数据与 IO，**不碰 Qt、不碰引擎**（方便离线自测：`python -m cuesheet`）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass, field, asdict

# ==========================================================================
# 版本与能力
# ==========================================================================
FORMAT_VERSION = 1          # 当前支持的最高格式版本
MIN_FORMAT_VERSION = 1      # 能读的最低格式版本
EXT = ".ercue"              # 码表包扩展名
JSON_NAME = "cuesheet.json"
MEDIA_DIR = "media"
THUMB_DIR = "thumbs"

# 本版**支持**的能力（9 项，与 Cue 端 `model/features.SUPPORTED_FEATURES` 同一词表）。
# `requires_features` 里出现不在此集合的名字 ⇒ 提示"将被忽略"。
# ★ 与 Cue 端 A3 裁定对齐：`trans_glitch` / `trans_zoom` **已实现**（演出端引擎能渲染），
#   不再当"本版没有"报警 —— 否则等于软件实现了却给自己报警。
SUPPORTED_FEATURES = {
    "layer_multi",        # 码表内多层
    "clip_fade",          # 淡入淡出
    "clip_inout",         # 素材只取一段（入/出点）
    "clip_speed",         # 变速
    "clip_crop",          # 画面裁切
    "video_embed",        # 内嵌素材
    "beat_snap",          # 吸附到拍/小节/乐句（旧文件兼容，演出端不消费）
    "trans_glitch",       # 故障过渡（已实现）
    "trans_zoom",         # 缩放过渡（已实现）
}
# 已知但本版不实现的能力（只做提示；**只增不删**）
KNOWN_FEATURES = set(SUPPORTED_FEATURES) | {"multi_layer_cue", "remote_source"}

# 转场类型（复用引擎已有的 5 种）
TRANSITIONS = ("fade", "cut", "slide", "zoom", "glitch")
# 混合模式（复用引擎 Layer.blend 的 7 种）
BLEND_MODES = ("normal", "add", "subtract", "screen", "multiply", "lighten", "darken")
# 吸附档位（遗留字段，只为读旧文件）
SNAP_MODES = ("off", "beat", "bar", "phrase")

#: 默认画布比例（像素）。旧码表没有 `res_w/res_h` ⇒ 回落 16:9。
DEFAULT_RES_W = 1920
DEFAULT_RES_H = 1080

#: bpm 退化态下"1 拍 = 1 秒"（与 Cue 端 `units.DEGENERATE_BEAT_LEN` 同值）。
#: ⚠ 退化时**绝不猜 BPM**，也不抛异常。
DEGENERATE_BEAT_LEN = 1.0
_MIN_BPM = 1e-6


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_list(v):
    """容器守卫：非 list 一律当空列表（脏数据不崩，导入必须容错）。"""
    return v if isinstance(v, list) else []


# ==========================================================================
# 拍 ↔ 秒（★ 与 Cue 端 `units.py` 逐字同口径）
# ==========================================================================
def bpm_is_usable(bpm):
    """bpm 是否为可用有效值（> 0 且可转 float）。False 时一切换算走退化态。"""
    return _f(bpm) > _MIN_BPM


def beat_len(bpm):
    """一拍的秒数 = `60 / bpm`。退化态（bpm≤0/非数值）返回 `1.0`（1 拍 = 1 秒）。

    ⚠ 两端必须同一条：Cue 端 `units.beat_len` 的退化值是 `DEGENERATE_BEAT_LEN=1.0`。
      演出端若在这里"返回 None ⇒ 不推进播放"，两端对"拍↔秒"的理解就不同 ⇒ 整条码表错位。
    """
    b = _f(bpm)
    if b > _MIN_BPM:
        return 60.0 / b
    return DEGENERATE_BEAT_LEN


def beats_to_sec(beats, bpm):
    """拍 → 秒（退化态下原样返回）。文件换算的唯一入口之一。"""
    return _f(beats) * beat_len(bpm)


def sec_to_beats(sec, bpm):
    """秒 → 拍（退化态下原样返回）。指纹给的"歌内第几秒"→"第几拍"走这里。"""
    return _f(sec) / beat_len(bpm)


# ==========================================================================
# 数据模型
# ==========================================================================
@dataclass
class CueItem:
    """某一层在时间轴上的一个片段。"""
    item_id: str = ""
    clip_id: str = ""                 # 指向 sheet.media
    at_beats: float = 0.0             # ★ 主坐标：相对曲首的拍数（不减任何 anchor）
    at_sec: float = 0.0               # 冗余（读包时按 at_beats 重算，与 Cue 端一致）
    end_beats: float | None = None    # None = 直到本层下一个 item / 曲终
    src_in_sec: float = 0.0           # ★ 只取素材的一段：入点（秒）
    src_out_sec: float | None = None  # 出点；None = 到素材末尾；到点冻结最后一帧
    speed: float = 1.0                # ★ 变速倍率（× speed = 更快；钳 0.1~3.0）
    crop: list | None = None          # ★ 画面裁切 [x, y, w, h]（归一化 0~1）；None = 不裁
    xform: dict = field(default_factory=lambda: {"scale": 1.0, "x": 0.0, "y": 0.0})
    # ★ 素材（item）级混合/不透明度：`""` / `None` = **继承本层**（见 CompLayer.effective_*）。
    #   旧码表没有这两个键 ⇒ 继承 ⇒ 行为逐值不变。
    blend: str = ""
    opacity: float | None = None
    trans_in: str = "fade"
    trans_in_beats: float = 2.0
    trans_out: str = "fade"
    trans_out_beats: float = 2.0
    snap: str = "bar"                 # 遗留字段（吸附已下线），只为读旧文件
    note: str = ""

    def normalized(self):
        """规范化（导入后必调）。规则与 Cue 端 `CueItem.normalized` 同口径。"""
        if self.trans_in not in TRANSITIONS:
            self.trans_in = "fade"
        if self.trans_out not in TRANSITIONS:
            self.trans_out = "fade"
        if self.snap not in SNAP_MODES:
            self.snap = "off"
        self.speed = min(3.0, max(0.1, _f(self.speed, 1.0)))
        self.at_beats = max(0.0, _f(self.at_beats))
        self.at_sec = max(0.0, _f(self.at_sec))
        self.trans_in_beats = max(0.0, _f(self.trans_in_beats, 2.0))
        self.trans_out_beats = max(0.0, _f(self.trans_out_beats, 2.0))
        if self.end_beats is not None:
            self.end_beats = max(self.at_beats, _f(self.end_beats))
        if self.crop is not None and len(self.crop) != 4:
            self.crop = None
        # item 级混合：只允许 BLEND_MODES 或 ""（空 = 继承本层）
        self.blend = str(self.blend or "")
        if self.blend and self.blend not in BLEND_MODES:
            self.blend = ""
        if self.opacity is not None:
            self.opacity = min(1.0, max(0.0, _f(self.opacity, 1.0)))
        return self

    def xform_is_default(self):
        """`xform` 是否"全默认"（scale=1 / x=0 / y=0 / rot=0）。

        契约①：全默认 ⇒ 几何用 **cover**（铺满裁切）；有任一变换 ⇒ **contain**（完整显示）。
        """
        xf = self.xform if isinstance(self.xform, dict) else {}
        return (abs(_f(xf.get("scale"), 1.0) - 1.0) < 1e-9
                and abs(_f(xf.get("x"), 0.0)) < 1e-9
                and abs(_f(xf.get("y"), 0.0)) < 1e-9
                and abs(_f(xf.get("rot"), 0.0)) < 1e-9)

    def used_source_sec(self, src_dur=0.0):
        """本片段用到的**源长度**（秒）：`src_out` 已知 ⇒ `src_out-src_in`；
        否则 `src_dur>0` ⇒ `src_dur-src_in`；否则 `None`（源长度未知，无法收敛）。

        与 Cue 端 `cuesheet.item_source_used_sec` 同口径（两端对"该放多久"理解必须一致）。
        """
        si = max(0.0, _f(self.src_in_sec))
        if self.src_out_sec is not None:
            return max(0.0, _f(self.src_out_sec) - si)
        d = _f(src_dur)
        if d > 0.0:
            return max(0.0, d - si)
        return None


@dataclass
class CompLayer:
    """码表内的一层（z 越大越"在上"）。"""
    lyr_id: str = ""
    name: str = ""
    z: int = 0
    blend: str = "normal"
    opacity: float = 1.0
    items: list = field(default_factory=list)     # [CueItem]

    def normalized(self):
        if self.blend not in BLEND_MODES:
            self.blend = "normal"
        self.opacity = min(1.0, max(0.0, _f(self.opacity, 1.0)))
        for it in self.items:
            if isinstance(it, CueItem):
                it.normalized()
        return self

    # ---- ★ item 级 blend/opacity 的"有效值"解析（唯一真源，勿绕过）----
    def effective_blend(self, item):
        """该片段**此刻生效**的混合：本素材自设了就用它，否则继承本层。

        ⚠ 下游（运行时/合成）**必须**用本函数取 blend，不要直接读 `it.blend`
          （那会丢掉继承，旧码表变成"混合模式空字符串"）。
        """
        b = str(getattr(item, "blend", "") or "")
        if b in BLEND_MODES:
            return b
        return self.blend if self.blend in BLEND_MODES else "normal"

    def effective_opacity(self, item):
        """该片段**此刻生效**的不透明度：本素材自设了就用它，否则继承本层。"""
        o = getattr(item, "opacity", None)
        if o is not None:
            return min(1.0, max(0.0, _f(o, 1.0)))
        return min(1.0, max(0.0, _f(self.opacity, 1.0)))


@dataclass
class MediaRef:
    """素材引用（**只存 basename，不含路径**）。"""
    clip_id: str = ""
    name: str = ""
    sha256: str = ""
    embedded: bool = False
    duration_sec: float = 0.0
    has_alpha: bool = False


@dataclass
class Identity:
    """曲目身份（认歌用）。"""
    fp_id: str = ""            # 指纹签名（`fp2-<hex>`）
    file_md5: str = ""         # ★ 音频文件 md5（认歌最高优先级：同一文件是确定判据）
    meta_id: str = ""          # 归一化 key = meta_key(title, artist, duration)
    title: str = ""
    artist: str = ""
    duration_sec: float = 0.0
    bpm: float = 0.0           # 码表时间轴坐标系；0 = 退化（1 拍 = 1 秒）
    key: str = ""
    file_name: str = ""

    @staticmethod
    def meta_key(title, artist, duration_sec):
        """归一化 → 稳定 key（**逐字照 Cue 端，不得修改**）。

        规则：去括号后缀 → 去 feat/ft/remix/... 后缀 → 只留 数字/小写字母/汉字；
        时长**取整到 2 秒**（两端时长差 >2 秒必然命不中，故有第 ③ 级降级）。
        """
        def nz(s):
            s = (s or "").lower().strip()
            s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)      # 去括号后缀
            s = re.sub(r"\b(feat|ft|remix|vip|edit|mix|version)\b.*$", "", s)
            s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)
            return s
        return "%s|%s|%d" % (nz(artist), nz(title), int(round(_f(duration_sec) / 2.0) * 2))

    def ensure_meta_id(self):
        if not self.meta_id:
            self.meta_id = Identity.meta_key(self.title, self.artist, self.duration_sec)
        return self


def _norm_title_artist(s):
    """认歌第 ③/④ 级用的归一化（比 `meta_key` 的 `nz` 多去扩展名与 master/remaster）。

    逐字照 Cue 端 `cuesheet._norm_title_artist`（两端认歌必须同一套归一化）。
    """
    s = (s or "").lower().strip()
    s = re.sub(r"\.[a-z0-9]{2,5}$", "", s)                               # 去扩展名
    s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)                         # 去括号后缀
    s = re.sub(r"\b(feat|ft|remix|vip|edit|mix|version|master|remaster)\b.*$", "", s)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


@dataclass
class TrackEntry:
    """一首歌的一份编排。"""
    track_key: str = ""
    identity: Identity = field(default_factory=Identity)
    layers: list = field(default_factory=list)     # [CompLayer]

    def normalized(self):
        for ly in self.layers:
            if isinstance(ly, CompLayer):
                ly.normalized()
        return self

    def sorted_layers(self):
        """按 z 从小到大（z 大的在上 ⇒ 绘制时后画）。"""
        return sorted([l for l in self.layers if isinstance(l, CompLayer)],
                      key=lambda l: l.z)

    @property
    def bpm(self):
        """码表时间轴坐标系用的 bpm（缺失/无效 ⇒ 0 ⇒ 换算走退化态）。"""
        return _f(getattr(self.identity, "bpm", 0.0))


@dataclass
class CueSheet:
    """一张码表（= 一个 .ercue 包）。"""
    title: str = ""
    author: str = ""
    notes: str = ""
    format_version: int = FORMAT_VERSION
    min_app_version: str = ""
    requires_features: list = field(default_factory=list)
    generator: dict = field(default_factory=dict)
    created_at: str = ""
    media: list = field(default_factory=list)      # [MediaRef]
    tracks: list = field(default_factory=list)     # [TrackEntry]
    # ★ 码表画布比例（像素）。**可选字段**，缺省 = 1920×1080（16:9）。
    #   这是"画布/输出规格"，与几何变换（xform/crop）**正交**。
    res_w: int = DEFAULT_RES_W
    res_h: int = DEFAULT_RES_H

    # ---- 便捷查询 ----
    def aspect(self):
        """画布宽高像素 `(res_w, res_h)`；非法（≤0 / 非数字）回落 `(1920, 1080)`。"""
        w, h = _i(self.res_w, DEFAULT_RES_W), _i(self.res_h, DEFAULT_RES_H)
        if w <= 0 or h <= 0:
            return (DEFAULT_RES_W, DEFAULT_RES_H)
        return (w, h)

    def media_by_id(self, clip_id):
        for m in self.media:
            if isinstance(m, MediaRef) and m.clip_id == clip_id:
                return m
        return None

    def track_by_identity(self, title="", artist="", duration_sec=0.0, fp_id=""):
        """旧接口（① 指纹 → ② meta_key 两级）。**新代码用 `match_track`。**"""
        if fp_id:
            for t in self.tracks:
                if t.identity.fp_id and t.identity.fp_id == fp_id:
                    return t
        key = Identity.meta_key(title, artist, duration_sec)
        for t in self.tracks:
            if t.identity.meta_id and t.identity.meta_id == key:
                return t
        return None

    def match_track(self, title="", artist="", duration_sec=0.0, fp_id="",
                    file_name="", file_md5=""):
        """认歌结果 → 码表曲目：返回 `(TrackEntry | None, 命中级别字符串)`。

        ★ **五级降级**（顺序 = 可靠性从高到低，与 Cue 端 `match_track` 完全一致）：
          ⓪ `md5`          音频文件 md5（**同一文件是确定判据**：改名/换目录/改标签仍命中）
          ① `fingerprint`  指纹签名（跨机可重算，抗改名/换容器）
          ② `meta`         名 + 艺人 + 时长（`Identity.meta_key`，时长取整 2 秒）
          ③ `title+artist` 名 + 艺人（**忽略时长**，跨机器最实用；多命中取 |时长差| 最小，
                           无时长取第一个并标 `title+artist?`）
          ④ `file`         文件名（去扩展名/括号后缀）
        全不中 → `(None, "")`。
        ★ **兼容**：旧码表没有 `file_md5` / `fp_id` ⇒ 该级自动跳过、正常降级。
        """
        if not self.tracks:
            return None, ""
        # ⓪ md5（★ 最高优先级）
        m = str(file_md5 or "").strip().lower()
        if m:
            for t in self.tracks:
                if str(getattr(t.identity, "file_md5", "") or "").strip().lower() == m:
                    return t, "md5"
        # ① 指纹 id
        if fp_id:
            for t in self.tracks:
                if t.identity.fp_id and str(t.identity.fp_id) == str(fp_id):
                    return t, "fingerprint"
        # ② 名 + 艺人 + 时长
        if title or artist:
            key = Identity.meta_key(title, artist, duration_sec)
            for t in self.tracks:
                if t.identity.meta_id and t.identity.meta_id == key:
                    return t, "meta"
            # ③ 名 + 艺人（忽略时长）
            nt, na = _norm_title_artist(title), _norm_title_artist(artist)
            if nt or na:
                cands = [t for t in self.tracks
                         if _norm_title_artist(t.identity.title) == nt
                         and _norm_title_artist(t.identity.artist) == na]
                if len(cands) == 1:
                    return cands[0], "title+artist"
                if len(cands) > 1:
                    if duration_sec > 0:
                        return min(cands, key=lambda t: abs(
                            _f(t.identity.duration_sec) - duration_sec)), "title+artist"
                    return cands[0], "title+artist?"
        # ④ 文件名
        nf = _norm_title_artist(file_name)
        if nf:
            for t in self.tracks:
                if _norm_title_artist(t.identity.file_name) == nf:
                    return t, "file"
        return None, ""

    def all_used_clip_ids(self):
        used = set()
        for t in self.tracks:
            for ly in t.layers:
                for it in ly.items:
                    if it.clip_id:
                        used.add(it.clip_id)
        return used

    def recompute_features(self):
        """按内容自动推断 `requires_features`（规则与 Cue 端 `features.py` 同口径）。"""
        feats = set()
        for t in self.tracks:
            if len(t.layers) > 1:
                feats.add("layer_multi")
            for ly in t.layers:
                for it in ly.items:
                    if it.trans_in not in ("cut",) or it.trans_out not in ("cut",):
                        feats.add("clip_fade")
                    if abs(_f(it.src_in_sec)) > 1e-6 or it.src_out_sec is not None:
                        feats.add("clip_inout")
                    if abs(_f(it.speed, 1.0) - 1.0) > 1e-6:
                        feats.add("clip_speed")
                    if it.crop:
                        feats.add("clip_crop")
                    if it.snap and it.snap != "off":
                        feats.add("beat_snap")
                    if it.trans_in == "glitch" or it.trans_out == "glitch":
                        feats.add("trans_glitch")
                    if it.trans_in == "zoom" or it.trans_out == "zoom":
                        feats.add("trans_zoom")
        for m in self.media:
            if m.embedded:
                feats.add("video_embed")
        self.requires_features = sorted(feats | set(self.requires_features or []))
        return self.requires_features

    def normalized(self):
        self.format_version = _i(self.format_version, FORMAT_VERSION)
        w, h = self.aspect()
        self.res_w, self.res_h = w, h             # 非法比例回落 1920×1080
        for t in self.tracks:
            if isinstance(t, TrackEntry):
                t.normalized()
        return self


# ==========================================================================
# 序列化（文件形态 = 拍主坐标）
# ==========================================================================
def _item_to_dict(it, bpm=0.0):
    """文件 item（拍主坐标）。`at_sec` 按主坐标重算 ⇒ 与 Cue 端写出的冗余值同值。"""
    return {
        "item_id": it.item_id, "clip_id": it.clip_id,
        "at_beats": _f(it.at_beats),
        "at_sec": beats_to_sec(it.at_beats, bpm),
        "end_beats": (None if it.end_beats is None else _f(it.end_beats)),
        "src_in_sec": _f(it.src_in_sec),
        "src_out_sec": (None if it.src_out_sec is None else _f(it.src_out_sec)),
        "speed": _f(it.speed, 1.0),
        "crop": (list(it.crop) if isinstance(it.crop, (list, tuple)) else None),
        "xform": dict(it.xform),
        "trans_in": it.trans_in,
        "trans_in_beats": _f(it.trans_in_beats, 2.0),
        "trans_out": it.trans_out,
        "trans_out_beats": _f(it.trans_out_beats, 2.0),
        "snap": it.snap, "note": it.note,
        "blend": str(getattr(it, "blend", "") or ""),
        "opacity": (None if getattr(it, "opacity", None) is None
                    else _f(it.opacity, 1.0)),
    }


def to_dict(sheet):
    """模型 → `.ercue` 的 `cuesheet.json` 形态（字段集合/顺序与 Cue 端一致）。"""
    return {
        "format_version": int(sheet.format_version),
        "generator": dict(sheet.generator or {}),
        "created_at": sheet.created_at or _now_iso(),
        "min_app_version": sheet.min_app_version or "",
        "requires_features": list(sheet.requires_features or []),
        "author": {"name": sheet.author or ""},
        "title": sheet.title or "",
        "notes": sheet.notes or "",
        "res_w": _i(getattr(sheet, "res_w", DEFAULT_RES_W), DEFAULT_RES_W),
        "res_h": _i(getattr(sheet, "res_h", DEFAULT_RES_H), DEFAULT_RES_H),
        "media": [asdict(m) for m in sheet.media if isinstance(m, MediaRef)],
        "tracks": [{
            "track_key": t.track_key or "",
            "identity": asdict(t.identity),
            "composition": {"layers": [{
                "lyr_id": ly.lyr_id, "name": ly.name, "z": ly.z,
                "blend": ly.blend, "opacity": _f(ly.opacity, 1.0),
                "items": [_item_to_dict(it, t.bpm) for it in ly.items
                          if isinstance(it, CueItem)],
            } for ly in t.layers if isinstance(ly, CompLayer)]},
        } for t in sheet.tracks if isinstance(t, TrackEntry)],
    }


def from_dict(d, meta_only=False):
    """容错解析：字段缺失/类型不对一律回落默认值，**绝不抛异常**（导入必须不崩）。

    `meta_only=True`：**只建 tracks（含 identity），跳过 media / layers / items**。
      给"码表索引"用（启动/扫描只要"这张表覆盖哪几首歌"），实测能省掉绝大部分构造成本。

    ★ **信任 `at_beats`（主坐标），忽略文件里的 `at_sec`（冗余）**，与 Cue 端一致：
      `at_sec` 由 `beats_to_sec(at_beats, bpm)` 重算。
    """
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
    # ★ 可选字段：旧码表没有 ⇒ 回落 16:9（不升 FORMAT_VERSION）
    s.res_w = _i(d.get("res_w"), DEFAULT_RES_W)
    s.res_h = _i(d.get("res_h"), DEFAULT_RES_H)

    for m in ([] if meta_only else _as_list(d.get("media"))):
        if not isinstance(m, dict) or not m.get("clip_id"):
            continue
        s.media.append(MediaRef(
            clip_id=str(m.get("clip_id")), name=str(m.get("name") or ""),
            sha256=str(m.get("sha256") or ""), embedded=bool(m.get("embedded")),
            duration_sec=_f(m.get("duration_sec")), has_alpha=bool(m.get("has_alpha"))))

    for t in _as_list(d.get("tracks")):
        if not isinstance(t, dict):
            continue
        te = TrackEntry(track_key=str(t.get("track_key") or ""))
        ident = t.get("identity") or {}
        if isinstance(ident, dict):
            te.identity = Identity(
                fp_id=str(ident.get("fp_id") or ""),
                # ★ 后加的可选字段：旧码表没有 ⇒ ""（该级认歌自动跳过）
                file_md5=str(ident.get("file_md5") or ""),
                meta_id=str(ident.get("meta_id") or ""),
                title=str(ident.get("title") or ""), artist=str(ident.get("artist") or ""),
                duration_sec=_f(ident.get("duration_sec")), bpm=_f(ident.get("bpm")),
                key=str(ident.get("key") or ""), file_name=str(ident.get("file_name") or ""))
        te.identity.ensure_meta_id()
        bpm = te.bpm
        if meta_only:
            s.tracks.append(te)          # 只要 identity，不建 layers/items
            continue
        comp = t.get("composition") or {}
        layers = comp.get("layers") if isinstance(comp, dict) else None
        for lyd in _as_list(layers):
            if not isinstance(lyd, dict):
                continue
            ly = CompLayer(lyr_id=str(lyd.get("lyr_id") or ""), name=str(lyd.get("name") or ""),
                           z=_i(lyd.get("z")), blend=str(lyd.get("blend") or "normal"),
                           opacity=_f(lyd.get("opacity"), 1.0))
            for itd in _as_list(lyd.get("items")):
                if not isinstance(itd, dict):
                    continue
                at_beats = _f(itd.get("at_beats"))
                ly.items.append(CueItem(
                    item_id=str(itd.get("item_id") or ""), clip_id=str(itd.get("clip_id") or ""),
                    at_beats=at_beats,
                    # 冗余字段按主坐标重算（Cue 端同样"信任 at_beats、不信任 at_sec"）
                    at_sec=beats_to_sec(at_beats, bpm),
                    end_beats=(None if itd.get("end_beats") is None
                               else _f(itd.get("end_beats"))),
                    src_in_sec=_f(itd.get("src_in_sec")),
                    src_out_sec=(None if itd.get("src_out_sec") is None
                                 else _f(itd.get("src_out_sec"))),
                    speed=_f(itd.get("speed"), 1.0),
                    crop=(list(itd["crop"]) if isinstance(itd.get("crop"), list) else None),
                    xform=(dict(itd["xform"]) if isinstance(itd.get("xform"), dict)
                           else {"scale": 1.0, "x": 0.0, "y": 0.0}),
                    # ★ 后加的可选字段：旧码表没有 ⇒ ""/None = 继承本层
                    blend=str(itd.get("blend") or ""),
                    opacity=(None if itd.get("opacity") is None
                             else _f(itd.get("opacity"), 1.0)),
                    trans_in=str(itd.get("trans_in") or "fade"),
                    trans_in_beats=_f(itd.get("trans_in_beats"), 2.0),
                    trans_out=str(itd.get("trans_out") or "fade"),
                    trans_out_beats=_f(itd.get("trans_out_beats"), 2.0),
                    snap=str(itd.get("snap") or "off"), note=str(itd.get("note") or "")))
            te.layers.append(ly)
        s.tracks.append(te)
    return s.normalized()


# ==========================================================================
# 能力协商（导入时的提示；**一律不阻断导入**）
# ==========================================================================
def capability_report(sheet, app_version="", missing_media=None):
    """返回 `(warnings: list[str], missing: list[str])`。

    四条提示按顺序、文案与 Cue 端 `ercue_io.capability_report` 一致（用户可见文案）。
    ⚠ 这些是**面向用户的动态文案**，将来接 UI 时按项目约定处理 i18n（或直接中文常量）。
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
    miss = list(missing_media or [])
    if miss:
        warn.append("有 %d 个素材没找到。" % len(miss))
    return warn, miss


def check_media(sheet, media_dir):
    """检查素材是否就位。返回 `(missing_names, present_map)`。

    `media_dir` 为 None / 不存在时，按"全部缺失"处理（用户只导入了 json 的情况）。
    """
    present, missing = {}, []
    for m in sheet.media:
        p = os.path.join(media_dir, m.name) if media_dir else ""
        if p and os.path.isfile(p):
            present[m.clip_id] = p
        else:
            missing.append(m.name)
    return missing, present


# ==========================================================================
# 读 / 写
# ==========================================================================
def _read_json_text(path):
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


def load(path, media_dir=None, meta_only=False):
    """读 `.ercue`（也接受裸 `cuesheet.json`）。

    返回 (sheet, info)：
      info = {"warnings": [...], "missing_media": [...], "present_media": {clip_id: path},
              "embedded": bool}
    **读失败抛 ValueError（调用方负责提示用户）；解析容错在 from_dict 里。**

    `meta_only=True` ⇒ 只读表头（tracks + identity），**不做媒体检查**、不建 layers/items。
      给"码表索引"用：只要"这张表覆盖哪几首歌"，不需要素材与片段。
    """
    if not path or not os.path.isfile(path):
        raise ValueError("找不到文件：%s" % path)
    raw, embed_dir = _read_json_text(path)
    try:
        d = json.loads(raw)
    except Exception as e:                                       # noqa: BLE001
        raise ValueError("码表内容不是合法 JSON：%s" % e)
    sheet = from_dict(d, meta_only=meta_only)
    if meta_only:
        return sheet, {"warnings": [], "missing_media": [], "present_media": {},
                       "embedded": bool(embed_dir and os.path.isdir(embed_dir))}
    md = media_dir if media_dir is not None else (
        embed_dir if embed_dir and os.path.isdir(embed_dir) else None)
    missing, present = check_media(sheet, md)
    warn, miss = capability_report(sheet, missing_media=missing)
    return sheet, {"warnings": warn, "missing_media": miss, "present_media": present,
                   "embedded": bool(embed_dir and os.path.isdir(embed_dir))}


def extract_media(pkg_path, dest_dir):
    """把包内 `media/` 解到 `dest_dir`，返回 {basename: path}。

    ★ 幂等由**调用方**保证（目标目录已存在且非空 ⇒ 直接复用）——
      与 Cue 端 `_extract_embedded_media` 同一约定。
    """
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


def save(sheet, path, media_paths=None, embed=False, app_version="", generator=None):
    """写 `.ercue`（演出端主要用 `load`；保留 `save` 供排障/自检与将来"另存"）。

    `media_paths`: {clip_id: 磁盘路径}，用于内嵌与 sha256 计算。
    `embed=False`（默认）只写清单，不内嵌素材（包只有几 KB）。
    """
    sheet.recompute_features()
    t = to_dict(sheet)
    t["generator"] = dict(generator or {"tool": "Easy Reality AutoVJ", "version": app_version})
    if app_version:
        t["min_app_version"] = sheet.min_app_version or app_version
    if not t.get("created_at"):
        t["created_at"] = _now_iso()

    # 用磁盘上的真实素材补 sha256 / duration / embedded
    media_paths = media_paths or {}
    for m in t["media"]:
        p = media_paths.get(m["clip_id"])
        if p and os.path.isfile(p):
            if not m.get("sha256"):
                m["sha256"] = _sha256_file(p)
            if not m.get("name"):
                m["name"] = os.path.basename(p)
            m["embedded"] = bool(embed)
            m["_src"] = p                       # 仅本次写包用，不写进 json

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
    return path


# ==========================================================================
# 新建 / 自检
# ==========================================================================
def new_sheet(title="新建码表", author="", res_w=DEFAULT_RES_W, res_h=DEFAULT_RES_H):
    return CueSheet(title=title, author=author, created_at=_now_iso(),
                    res_w=_i(res_w, DEFAULT_RES_W), res_h=_i(res_h, DEFAULT_RES_H))


def new_track(title="", artist="", duration_sec=0.0, bpm=0.0):
    te = TrackEntry()
    te.identity = Identity(title=title, artist=artist, duration_sec=duration_sec, bpm=bpm)
    te.identity.meta_id = Identity.meta_key(title, artist, duration_sec)
    te.track_key = te.identity.meta_id[:12] or "t1"
    return te


def selftest():
    """无依赖自检：拍↔秒换算 + 往返读写 + item 级继承 + 五级认歌 + 容错。"""
    import tempfile

    # 1) 拍↔秒（含退化态，逐字对齐 Cue 端 units.py 的数值）
    assert abs(beat_len(120.0) - 0.5) < 1e-12
    assert abs(beats_to_sec(32.0, 155.03) - 12.384699735535058) < 1e-9
    for bad in (0, 0.0, -1.0, None, "xx", ""):
        assert not bpm_is_usable(bad), bad
        assert beat_len(bad) == DEGENERATE_BEAT_LEN
        assert sec_to_beats(3.0, bad) == 3.0

    tmp = tempfile.mkdtemp(prefix="ercue_")
    p = os.path.join(tmp, "t" + EXT)

    s = new_sheet("自检码表", "unit-test", res_w=1440, res_h=1080)
    te = new_track("Cosmic String", "Rolling Contact", 372.4, 155.03)
    te.identity.file_md5 = "D41D8CD98F00B204E9800998ECF8427E"
    te.identity.file_name = "Rolling Contact - Cosmic String.flac"
    ly = CompLayer(lyr_id="L1", name="背景", z=0, blend="screen", opacity=0.9)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_beats=32.0,
                            src_in_sec=2.0, src_out_sec=8.0, speed=1.25,
                            crop=[0.05, 0.0, 0.9, 1.0],
                            blend="add", opacity=0.5,
                            trans_in="glitch", trans_out="fade").normalized())
    ly.items.append(CueItem(item_id="i2", clip_id="c2", at_beats=96.0,
                            trans_in="cut", trans_out="cut").normalized())   # blend ""/opacity None
    te.layers.append(ly)
    s.tracks.append(te)
    s.media.append(MediaRef(clip_id="c1", name="a.mp4"))
    s.media.append(MediaRef(clip_id="c2", name="b.mp4"))

    feats = s.recompute_features()
    assert "clip_inout" in feats and "clip_speed" in feats and "clip_crop" in feats, feats
    assert "trans_glitch" in feats, feats

    save(s, p, app_version="2026.10.17.01")
    s2, info = load(p)
    assert s2.title == "自检码表", s2.title
    assert s2.aspect() == (1440, 1080), s2.aspect()
    assert len(s2.tracks) == 1 and len(s2.tracks[0].layers) == 1
    it = s2.tracks[0].layers[0].items[0]
    assert abs(it.at_beats - 32.0) < 1e-9 and abs(it.speed - 1.25) < 1e-9
    assert abs(it.at_sec - beats_to_sec(32.0, 155.03)) < 1e-9        # 冗余按主坐标重算
    assert it.crop == [0.05, 0.0, 0.9, 1.0] and it.trans_in == "glitch"
    assert abs(it.src_out_sec - 8.0) < 1e-9 and abs(it.trans_in_beats - 2.0) < 1e-9
    ly2 = s2.tracks[0].layers[0]
    assert ly2.blend == "screen" and abs(ly2.opacity - 0.9) < 1e-9
    # ★ item 级 blend/opacity：自设优先、缺省继承本层
    assert it.blend == "add" and abs(it.opacity - 0.5) < 1e-9
    assert ly2.effective_blend(it) == "add" and abs(ly2.effective_opacity(it) - 0.5) < 1e-9
    it2 = ly2.items[1]
    assert it2.blend == "" and it2.opacity is None
    assert ly2.effective_blend(it2) == "screen" and abs(ly2.effective_opacity(it2) - 0.9) < 1e-9
    # 缺素材提示
    assert len(info["missing_media"]) == 2, info
    assert any("素材没找到" in w for w in info["warnings"]), info["warnings"]

    # 2) 五级认歌（md5 压过指纹/meta；旧表缺 md5 ⇒ 正常降级）
    assert s2.match_track(fp_id="x", file_md5="d41d8cd98f00b204e9800998ecf8427e")[1] == "md5"
    assert s2.match_track(title="Cosmic String", artist="Rolling Contact",
                          duration_sec=372.4)[1] == "meta"
    assert s2.match_track(title="Cosmic String", artist="Rolling Contact",
                          duration_sec=100.0)[1] == "title+artist"
    assert s2.match_track(file_name="Rolling Contact - Cosmic String.flac")[1] == "file"
    assert s2.match_track("不是这首", "X", 10.0) == (None, "")

    # 3) 未来版本：应给提示但**仍然读出来**
    d = json.loads(json.dumps(to_dict(s)))
    d["format_version"] = FORMAT_VERSION + 5
    d["min_app_version"] = "2099.01.01.00"
    d["requires_features"] = ["clip_crop", "未来的能力"]
    s3 = from_dict(d)
    w, _ = capability_report(s3, app_version="2026.10.17.01")
    assert any("更新版本" in x for x in w) and any("要求" in x for x in w) \
        and any("没有的能力" in x for x in w), w
    assert s3.title == "自检码表" and len(s3.tracks) == 1

    # 4) 旧码表（无 res_w/res_h、无 file_md5、item 无 blend/opacity）⇒ 全部回落
    old = {"tracks": [{"identity": {"title": "T", "artist": "A", "duration_sec": 100.0,
                                    "bpm": 120.0},
                       "composition": {"layers": [{"lyr_id": "L1", "z": 0,
                                                   "items": [{"at_beats": 8.0}]}]}}]}
    s5 = from_dict(old)
    assert s5.aspect() == (1920, 1080), s5.aspect()
    assert s5.tracks[0].identity.file_md5 == ""
    it5 = s5.tracks[0].layers[0].items[0]
    assert it5.blend == "" and it5.opacity is None
    assert abs(it5.at_sec - 4.0) < 1e-9, it5.at_sec                  # 8 拍 @120bpm = 4s
    assert s5.tracks[0].layers[0].effective_blend(it5) == "normal"

    # 5) 脏数据不应崩
    s4 = from_dict({"tracks": [{"composition": {"layers": [{"items": [{"at_beats": "xx"}]}]}}]})
    assert isinstance(s4, CueSheet) and s4.tracks[0].layers[0].items[0].at_beats == 0.0
    s6 = from_dict({"tracks": "坏", "media": 3, "requires_features": "x"})
    assert isinstance(s6, CueSheet) and s6.tracks == [] and s6.media == []

    print("[cuesheet] 自检通过 ✓  包=%s（%.1f KB）  能力=%s  res=%dx%d"
          % (os.path.basename(p), os.path.getsize(p) / 1024.0, feats, s2.res_w, s2.res_h))
    return True


if __name__ == "__main__":
    selftest()
