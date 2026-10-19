# -*- coding: utf-8 -*-
"""Cue Sheet 数据模型。

七个数据类（架构方案 §2 / 规格书 §2.2）：
    CueSheet / TrackEntry / Composition / CompLayer / CueItem / MediaRef / Identity

★★ 单位纪律（架构方案 §3.2）：
    模型内部**唯一坐标 = 秒**。CueItem 的坐标字段：
        at_sec        起点（秒）
        end_sec       终点（秒，None = 到本层下一个 item / 曲终）
        src_in_sec / src_out_sec   素材入/出点（秒）
        trans_in_sec / trans_out_sec  过渡时长（秒）
    `.ercue` 文件里的 `at_beats` / `end_beats` / `trans_*_beats`（拍）**不是模型字段**，
    只在序列化边界（`ercue_io.sheet_to_file_dict / sheet_from_file_dict`）用 `units.py`
    换算一次。本模块的 `to_state_dict / from_state_dict` 是**秒**形态的**状态快照**
    （供撤销栈做 JSON 快照，架构方案 §3.5），**不含任何拍字段**。
    **本文件不出现任何 beat_len 乘除。**

本模块 **零 Qt**，只跑数据与纯逻辑，可离线自检：
    python -m ercue.model.cuesheet
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .units import to_float as _f
from . import features as _features

# ==========================================================================
# 枚举（规格书 §3.5；照旧代码 cuesheet.py:53-58）
# ==========================================================================
TRANSITIONS = ("fade", "cut", "slide", "zoom", "glitch")
BLEND_MODES = ("normal", "add", "subtract", "screen", "multiply", "lighten", "darken")
SNAP_MODES = ("off", "beat", "bar", "phrase")

FORMAT_VERSION = 1        # 当前支持的最高格式版本
MIN_FORMAT_VERSION = 1    # 能读的最低格式版本
# ★ 导出时写进 `min_app_version` 的**演出端最低版本**（空 = 不限制）。
#   ⚠ 这**不是**编排端（EasyRealityCue）版本 —— 千万别填自己的构建号：
#     演出端会拿**它自己的**构建号去比，填了就永远差一格 ⇒ 每次导入都误报
#     「这张码表要求 2026.10.17.08 以上版本（当前 2026.10.17.07）」（真实用户截图）。
#     真有"需要某个演出端版本才支持的能力"时才在这里写死一个值。
REQUIRED_RUNTIME_VERSION = ""


def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_list(v) -> list:
    """容器守卫：非 list 一律当空列表（脏数据不崩，导入必须容错）。"""
    return v if isinstance(v, list) else []


def _norm_title_artist(s) -> str:
    """认歌第 ③/④ 级用的归一化（**与 `Identity.meta_key` 的 `nz` 略有不同**：
    多去「扩展名」与「master/remaster」）。逐字照规格书 §4.1（`cuesheet.py:254-259`）。
    """
    s = (s or "").lower().strip()
    s = re.sub(r"\.[a-z0-9]{2,5}$", "", s)                                # 去扩展名
    s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)                          # 去括号后缀
    s = re.sub(r"\b(feat|ft|remix|vip|edit|mix|version|master|remaster)\b.*$", "", s)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


# ==========================================================================
# 数据类
# ==========================================================================
@dataclass
class MediaRef:
    """素材引用（**只存 basename，不含路径**）。"""
    clip_id: str = ""
    name: str = ""
    sha256: str = ""
    embedded: bool = False
    duration_sec: float = 0.0
    has_alpha: bool = False

    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"clip_id": self.clip_id, "name": self.name, "sha256": self.sha256,
                "embedded": bool(self.embedded), "duration_sec": _f(self.duration_sec),
                "has_alpha": bool(self.has_alpha)}

    @classmethod
    def from_state_dict(cls, d) -> "MediaRef":
        d = d if isinstance(d, dict) else {}
        return cls(clip_id=str(d.get("clip_id") or ""), name=str(d.get("name") or ""),
                   sha256=str(d.get("sha256") or ""), embedded=bool(d.get("embedded")),
                   duration_sec=_f(d.get("duration_sec")), has_alpha=bool(d.get("has_alpha")))


@dataclass
class Identity:
    """曲目身份（认歌用）。"""
    fp_id: str = ""            # 指纹签名（`model/fp_core`：fp2-<16hex>；同机/异机均可重算）
    file_md5: str = ""         # ★ 音频文件的 md5（**认歌最高优先级**：同一文件是确定判据）
    meta_id: str = ""          # 归一化 key = meta_key(title, artist, duration)
    title: str = ""
    artist: str = ""
    duration_sec: float = 0.0
    bpm: float = 0.0           # 码表时间轴坐标系；0 = 用实时推算（退化态 1 拍 = 1 秒）
    key: str = ""              # 调性（预留）
    file_name: str = ""        # 原始文件名（含扩展名）

    @staticmethod
    def meta_key(title, artist, duration_sec) -> str:
        """归一化 → 稳定 key。**逐字照规格书 §2.2（`cuesheet.py:172-182`），不得修改**
        —— 这是跨机器认歌的关键。

        规则：去括号后缀 → 去 feat/ft/remix/... 后缀 → 只留 数字/小写字母/汉字；
        时长**取整到 2 秒**（两端时长差 >2 秒必然命不中，故有第 ③ 级降级）。
        """
        def nz(s):
            s = (s or "").lower().strip()
            s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)                 # 去括号后缀
            s = re.sub(r"\b(feat|ft|remix|vip|edit|mix|version)\b.*$", "", s)
            s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)                # 只留 数字/小写字母/汉字
            return s
        return "%s|%s|%d" % (nz(artist), nz(title), int(round(_f(duration_sec) / 2.0) * 2))

    def ensure_meta_id(self) -> "Identity":
        if not self.meta_id:
            self.meta_id = Identity.meta_key(self.title, self.artist, self.duration_sec)
        return self

    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"fp_id": self.fp_id, "file_md5": self.file_md5, "meta_id": self.meta_id,
                "title": self.title,
                "artist": self.artist, "duration_sec": _f(self.duration_sec),
                "bpm": _f(self.bpm), "key": self.key, "file_name": self.file_name}

    @classmethod
    def from_state_dict(cls, d) -> "Identity":
        # ★ 兼容：`file_md5` 是**后加的可选字段**，旧码表没有 ⇒ 取不到就是 ""（按"没有 md5"降级）
        d = d if isinstance(d, dict) else {}
        return cls(fp_id=str(d.get("fp_id") or ""),
                   file_md5=str(d.get("file_md5") or ""),
                   meta_id=str(d.get("meta_id") or ""),
                   title=str(d.get("title") or ""), artist=str(d.get("artist") or ""),
                   duration_sec=_f(d.get("duration_sec")), bpm=_f(d.get("bpm")),
                   key=str(d.get("key") or ""), file_name=str(d.get("file_name") or ""))


@dataclass
class CueItem:
    """某一层在时间轴上的一个片段。

    ★ 坐标在**模型内部一律用秒**（`at_sec` 为权威值）。`end_sec=None` 表示开放结尾。
    `trans_*_sec` 为过渡时长（秒）。拍相关字段只存在于文件形态，见模块 docstring。
    """
    item_id: str = ""
    clip_id: str = ""                    # 指向 CueSheet.media
    at_sec: float = 0.0                  # ★ 权威坐标：相对曲首的秒数
    end_sec: Optional[float] = None      # None = 直到本层下一个 item / 曲终
    src_in_sec: float = 0.0              # ★ 素材入点（秒）
    src_out_sec: Optional[float] = None  # 出点；None = 到素材末尾
    speed: float = 1.0                   # ★ 变速（钳 0.1~3.0）
    crop: Optional[list] = None          # [x, y, w, h] 归一化 0~1；None = 不裁
    xform: dict = field(default_factory=lambda: {"scale": 1.0, "x": 0.0, "y": 0.0})
    # ★ 需求 9（名字 + 语义一起改）：混合/不透明度从**层**下沉到**素材（item）**级 ——
    #   每段素材各自的混合互不影响。`""` / `None` = **继承本层**（见 `CompLayer.effective_blend`）。
    #   ⚠ 可选字段 + 惰性继承 ⇒ 旧码表（只有层级 blend）导入后行为**逐值不变**（沿用层值作初值）。
    blend: str = ""                      # 本素材混合；"" = 继承本层 CompLayer.blend
    opacity: Optional[float] = None      # 本素材不透明度；None = 继承本层 CompLayer.opacity
    trans_in: str = "fade"
    trans_in_sec: float = 2.0            # 入场时长（秒）
    trans_out: str = "fade"
    trans_out_sec: float = 2.0           # 出场时长（秒）
    snap: str = "off"                    # ★ 吸附功能已移除；此字段仅为**旧文件格式兼容**保留
    note: str = ""

    def normalized(self) -> "CueItem":
        """规范化（每次导入/编辑后必须调用）。规则照规格书 §2.2：
        非法 trans_* 回落 fade；非法 snap 回落 off；speed 钳 0.1~3.0；
        at ≥ 0；end ≥ at；crop 长度 ≠ 4 清空。"""
        if self.trans_in not in TRANSITIONS:
            self.trans_in = "fade"
        if self.trans_out not in TRANSITIONS:
            self.trans_out = "fade"
        if self.snap not in SNAP_MODES:
            self.snap = "off"
        self.speed = min(3.0, max(0.1, _f(self.speed, 1.0)))
        self.at_sec = max(0.0, _f(self.at_sec))
        self.trans_in_sec = max(0.0, _f(self.trans_in_sec, 2.0))
        self.trans_out_sec = max(0.0, _f(self.trans_out_sec, 2.0))
        if self.end_sec is not None:
            self.end_sec = max(self.at_sec, _f(self.end_sec))
        if self.crop is not None and len(self.crop) != 4:
            self.crop = None
        # 需求 9：item 级混合 —— 只允许 BLEND_MODES 或 ""（空 = 继承本层）
        if self.blend and self.blend not in BLEND_MODES:
            self.blend = ""
        if self.opacity is not None:
            self.opacity = min(1.0, max(0.0, _f(self.opacity, 1.0)))
        return self

    def deep_clone(self, new_item_id: str = "") -> "CueItem":
        """★ **显式深拷贝**一个全新片段（供"复制片段"/"同素材多图层/多段"等派生路径**统一**使用）。

        为什么要它：浅引用（`copy.copy` / 直接复用同一个 `xform`、`crop` 对象）会亲手制造
        "改一个片段、另一个跟着变"的**共享**问题（需求 10 用户反馈的隐患）。这里把**可变字段**
        一律新建：`xform` 新 dict、`crop` 新 list；其余为不可变标量（逐一同值）。
        `new_item_id` 非空时替换 `item_id`（调用方负责给**全局唯一**的新 id）。
        """
        return CueItem(
            item_id=str(new_item_id or self.item_id), clip_id=self.clip_id,
            at_sec=self.at_sec, end_sec=self.end_sec,
            src_in_sec=self.src_in_sec, src_out_sec=self.src_out_sec,
            speed=self.speed,
            crop=(list(self.crop) if isinstance(self.crop, (list, tuple)) else None),
            xform=dict(self.xform),
            blend=self.blend, opacity=self.opacity,
            trans_in=self.trans_in, trans_in_sec=self.trans_in_sec,
            trans_out=self.trans_out, trans_out_sec=self.trans_out_sec,
            snap=self.snap, note=self.note)

    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"item_id": self.item_id, "clip_id": self.clip_id,
                "at_sec": _f(self.at_sec), "end_sec": (None if self.end_sec is None
                                                       else _f(self.end_sec)),
                "src_in_sec": _f(self.src_in_sec),
                "src_out_sec": (None if self.src_out_sec is None else _f(self.src_out_sec)),
                "speed": _f(self.speed, 1.0),
                "crop": (list(self.crop) if isinstance(self.crop, (list, tuple)) else None),
                "xform": dict(self.xform),
                "blend": self.blend,
                "opacity": (None if self.opacity is None else _f(self.opacity, 1.0)),
                "trans_in": self.trans_in, "trans_in_sec": _f(self.trans_in_sec, 2.0),
                "trans_out": self.trans_out, "trans_out_sec": _f(self.trans_out_sec, 2.0),
                "snap": self.snap, "note": self.note}

    @classmethod
    def from_state_dict(cls, d) -> "CueItem":
        d = d if isinstance(d, dict) else {}
        crop = d.get("crop")
        return cls(item_id=str(d.get("item_id") or ""), clip_id=str(d.get("clip_id") or ""),
                   at_sec=_f(d.get("at_sec")),
                   end_sec=(None if d.get("end_sec") is None else _f(d.get("end_sec"))),
                   src_in_sec=_f(d.get("src_in_sec")),
                   src_out_sec=(None if d.get("src_out_sec") is None else _f(d.get("src_out_sec"))),
                   speed=_f(d.get("speed"), 1.0),
                   crop=(list(crop) if isinstance(crop, list) else None),
                   xform=(dict(d["xform"]) if isinstance(d.get("xform"), dict)
                          else {"scale": 1.0, "x": 0.0, "y": 0.0}),
                   # ★ 需求 9 后加的可选字段：旧快照没有 ⇒ ""/None = 继承本层（行为不变）
                   blend=str(d.get("blend") or ""),
                   opacity=(None if d.get("opacity") is None else _f(d.get("opacity"), 1.0)),
                   trans_in=str(d.get("trans_in") or "fade"),
                   trans_in_sec=_f(d.get("trans_in_sec"), 2.0),
                   trans_out=str(d.get("trans_out") or "fade"),
                   trans_out_sec=_f(d.get("trans_out_sec"), 2.0),
                   snap=str(d.get("snap") or "off"), note=str(d.get("note") or ""))


# ==========================================================================
# ★ 变速一致性（A3/#20，纯逻辑）：item 的"唯一自洽关系"
#     end - at  ==  (src_out_eff - src_in) / speed      （src_out 未知则用 src_dur）
# ==========================================================================
TIMING_EPS = 1e-6      # 秒：字段"变了没变"的比较阈值（浮点容差）
OVERLAP_EPS = 1e-3     # 秒：区间"恰好相接"（缝隙 ≤ 此）视为**不重叠**


def item_source_used_sec(it, src_dur=0.0):
    """本片段用到的**源长度**（秒）：

      · `src_out_sec` 已知 ⇒ `src_out - src_in`；
      · 否则 `src_dur > 0`  ⇒ `src_dur - src_in`（= 用到素材末尾；GIF 即"一轮时长"）；
      · 否则               ⇒ `None`（源长度未知，**无法收敛**）。

    返回值钳到 `>= 0`。**绝不抛**。GIF 片段（不设 `src_out`）由此拿到"一轮"长度。
    """
    si = max(0.0, _f(getattr(it, "src_in_sec", 0.0)))
    so = getattr(it, "src_out_sec", None)
    if so is not None:
        return max(0.0, _f(so) - si)
    d = _f(src_dur)
    if d > 0.0:
        return max(0.0, d - si)
    return None


def _dur_of(end, at):
    """区间时长；`end is None` ⇒ `None`（开放结尾）。"""
    return None if end is None else max(0.0, _f(end) - _f(at))


def _same(a, b) -> bool:
    """两个可空浮点是否"没变"（`None` 与值视为不同）。"""
    if (a is None) != (b is None):
        return False
    if a is None:
        return True
    return abs(_f(a) - _f(b)) <= TIMING_EPS


def reconcile_item_timing(it, prev, src_dur=0.0, keep_src_out_unset=False) -> str:
    """让 `at / end / src_in / src_out / speed` **自洽**（"唯一权威关系"）：

        `end - at  ==  (src_out_eff - src_in) / speed`     （`src_out` 未知则用 `src_dur`）

    **按"用户到底改了哪个"决定派生物**（`prev` = 改动**前**的
    `{"at_sec","end_sec","src_in_sec","src_out_sec","speed"}`）：

      · 只改了 **speed**（源范围未变）      ⇒ **源范围权威**：`end = at + used / speed`
      · 改了 **src_in / src_out**（时长未变）⇒ **源范围权威**：`end = at + used / speed`
      · 只改了 **时长（`end - at`）**        ⇒ **时长权威**：`src_out = src_in + (end-at)*speed`
                                               （钳到 `src_dur` ⇒ 超出即"**末尾定格**"，`end` 保留）
      · 两者都变了 / 无法判定               ⇒ **源范围优先**（`end` 派生）
      · 源长度未知（无 `src_out` 且 `src_dur<=0`）⇒ **不动**（返回 `'none'`）

    ⚠ **"改了时长"按 `end - at` 判定，而不是按 `end`** —— 否则"整体平移片段"（`at`/`end` 同时
      平移、时长不变）会被误判成"用户改了时长"，从而**错误改写 `src_out`**。

    ⚠ `keep_src_out_unset=True`（**GIF 片段**）⇒ **绝不写 `src_out_sec`**（GIF 的循环依赖它保持
      `None`）；此时"时长权威"支**不收敛**并返回 `'none'`。

    返回做了什么：`'end_from_source'` / `'src_from_end'` / `'none'`。**不推进撤销栈**（由 window 管）。
    **绝不抛**。
    """
    if it is None:
        return "none"
    p = prev if isinstance(prev, dict) else {}

    # 1) 先做基本钳位（与 `CueItem.normalized()` 同口径；此处不调用它以免把别的字段一起改）
    at = max(0.0, _f(getattr(it, "at_sec", 0.0)))
    it.at_sec = at
    speed = min(3.0, max(0.1, _f(getattr(it, "speed", 1.0), 1.0)))
    it.speed = speed
    src_in = max(0.0, _f(getattr(it, "src_in_sec", 0.0)))
    it.src_in_sec = src_in
    so = getattr(it, "src_out_sec", None)
    src_out_now = None if so is None else max(0.0, _f(so))
    end = getattr(it, "end_sec", None)
    end = None if end is None else max(at, _f(end))

    # 2) 判断"变的是哪一类"（时长按 `end - at` 比，平移不变形）
    p_at = p.get("at_sec")
    p_end = p.get("end_sec")
    p_age = p.get("speed")
    p_si = p.get("src_in_sec")
    p_so = p.get("src_out_sec")
    has_prev = any(k in p for k in ("at_sec", "end_sec", "src_in_sec", "src_out_sec", "speed"))
    if not has_prev:                                   # 没有 prev ⇒ 无法判定 ⇒ 只做钳位
        it.end_sec = end
        return "none"

    speed_ch = p_age is not None and abs(speed - _f(p_age)) > TIMING_EPS
    src_ch = (not _same(p_si, src_in)) or (not _same(p_so, src_out_now))
    dur_now, dur_prev = _dur_of(end, at), _dur_of(p_end, p_at)
    dur_ch = not _same(dur_now, dur_prev)

    # 3) 决策
    if dur_ch and not src_ch and not speed_ch:
        if keep_src_out_unset:
            it.end_sec = end
            return "none"                              # GIF：时长权威支不写 src_out，保持现状
        d = max(0.0, _f(src_dur))
        new_out = src_in + max(0.0, (dur_now or 0.0)) * speed
        if d > 0.0:
            new_out = min(new_out, d)                  # ★ 钳到素材全长 ⇒ 超出部分即"定格"
        it.src_out_sec = new_out
        it.end_sec = end                               # ★ 时长权威：end 原样保留（定格语义）
        return "src_from_end"

    if src_ch or speed_ch:
        used = item_source_used_sec(it, src_dur)
        if used is None:
            it.end_sec = end
            return "none"                              # 源长度未知 ⇒ 不动
        it.end_sec = at + used / speed                 # ★ 源范围权威：end 派生
        return "end_from_source"

    it.end_sec = end
    return "none"


# ==========================================================================
# ★ #15 同层"顶掉"（PR 覆盖）：纯逻辑，不推进撤销栈
# ==========================================================================
def _end_eff(items_sorted, idx, view_right=None) -> float:
    """片段"有效终点"（秒）：`end_sec` → 本层**下一个片段起点** → `at + OVERLAP_EPS`。

    与 UI 的 `timeline_widget._item_end_px_sec` **同口径**（那边最后的兜底是"视口右端"；
    模型侧没有视口，故兜底为 `at + 极小` = "开放结尾只占一个点"，见 `plan_overwrite` 说明）。
    """
    it = items_sorted[idx]
    at = _f(it.at_sec)
    if it.end_sec is not None:
        return max(at, _f(it.end_sec))
    nxt = [_f(x.at_sec) for x in items_sorted if _f(x.at_sec) > at + TIMING_EPS]
    if nxt:
        return min(nxt)
    if view_right is not None:
        return max(at, _f(view_right))
    return at + OVERLAP_EPS


def plan_overwrite(items, new_item) -> dict:
    """**纯函数**（不改任何对象）：算出"把 `new_item` 覆盖到的同层其它片段"该怎么删/裁。

    规则（架构师裁定，PR 覆盖语义）——`A` = `new_item`，`B` = 同层其它片段：
      · **B 被 A 完全覆盖**（`A.at <= B.at` 且 `A.end >= B.end_eff`）⇒ **删 B**
      · **A 只盖住 B 左半**（`A.end` 落在 B 内部）            ⇒ **裁 B 头**：`B.at = A.end`
      · **A 只盖住 B 右半**（`A.at` 落在 B 内部）             ⇒ **裁 B 尾**：`B.end = A.at`
      · **A 落在 B 内部**（B 全包 A）                          ⇒ **删 B**（不拆两段，避免凭空多一段）
      · **恰好在边界相接**（缝隙 ≤ `OVERLAP_EPS`）             ⇒ **不重叠**（不动 B）
    ⚠ 间隙（不重叠）与"完全相等"都归于"相接"，均不处理。

    返回 `{"removed": [item_id…], "trimmed": [item_id…], "plan": [(item, at, end)…]}`；
    `plan` 里给出每个被裁片段该写成什么（`end=None` 表示保持开放结尾）。
    """
    out = {"removed": [], "trimmed": [], "plan": []}
    if new_item is None:
        return out
    cands = [x for x in _as_list(items) if x is not None and x is not new_item]
    cands.sort(key=lambda x: (_f(x.at_sec), str(getattr(x, "item_id", ""))))
    a_at = _f(new_item.at_sec)
    # A 的有效终点：优先自己的 end；否则"同层下一个片段起点"（含候选）→ 兜底 at+极???
    if new_item.end_sec is not None:
        a_end = max(a_at, _f(new_item.end_sec))
    else:
        nxt = [(_f(x.at_sec)) for x in cands if _f(x.at_sec) > a_at + TIMING_EPS]
        a_end = (min(nxt) if nxt else (a_at + OVERLAP_EPS))
    for idx, b in enumerate(cands):
        b_at, b_end = _f(b.at_sec), _end_eff(cands, idx)
        if b_end <= b_at + OVERLAP_EPS:                # 退化（点/零长，含"开放结尾无后继"）
            if a_at <= b_at + OVERLAP_EPS and a_end >= b_at - OVERLAP_EPS:
                out["removed"].append(str(getattr(b, "item_id", "")))
            continue
        if a_end <= b_at + OVERLAP_EPS or a_at >= b_end - OVERLAP_EPS:
            continue                                    # 不相交 / 恰好相接 ⇒ 不处理
        covers_left = a_at <= b_at + OVERLAP_EPS        # A 的左端在 B 起点或之前
        covers_right = a_end >= b_end - OVERLAP_EPS     # A 的右端在 B 终点或之后
        if covers_left and covers_right:
            out["removed"].append(str(getattr(b, "item_id", "")))       # B 完全被覆盖
        elif covers_left:                               # A 盖住 B 左半 ⇒ 裁 B 头
            out["trimmed"].append(str(getattr(b, "item_id", "")))
            out["plan"].append((b, a_end, (None if b.end_sec is None else b_end)))
        elif covers_right:                              # A 盖住 B 右半 ⇒ 裁 B 尾
            out["trimmed"].append(str(getattr(b, "item_id", "")))
            out["plan"].append((b, b_at, a_at))
        else:                                           # A 落在 B 内部 ⇒ 删 B
            out["removed"].append(str(getattr(b, "item_id", "")))
    return out


def apply_overwrite(items, new_item) -> dict:
    """在 `plan_overwrite` 的基础上**就地执行**（删/裁 `items` 列表里的元素），返回同一份报告。

    ⚠ 只改 `items` 传进来的那个列表（默认由 `CompLayer.overwrite` 传 `self.items`）。
    """
    rep = plan_overwrite(items, new_item)
    if not rep["removed"] and not rep["trimmed"]:
        return rep
    for b, new_at, new_end in rep["plan"]:
        b.at_sec = new_at
        b.end_sec = new_end
    if rep["removed"]:
        dead = set(rep["removed"])
        items[:] = [x for x in items if str(getattr(x, "item_id", "")) not in dead]
    return rep


@dataclass
class CompLayer:
    """码表内的一层（z 越大越"在上"）。"""
    lyr_id: str = ""
    name: str = ""
    z: int = 0
    blend: str = "normal"
    opacity: float = 1.0
    items: list = field(default_factory=list)    # [CueItem]

    def normalized(self) -> "CompLayer":
        if self.blend not in BLEND_MODES:
            self.blend = "normal"
        self.opacity = min(1.0, max(0.0, _f(self.opacity, 1.0)))
        for it in self.items:
            if isinstance(it, CueItem):
                it.normalized()
        return self

    # ---- ★ 需求 9：混合/不透明度**下沉到素材级**后的"有效值"解析（唯一真源）----
    def effective_blend(self, item) -> str:
        """该片段**此刻生效**的混合：本素材自设了就用它，否则**继承本层**。

        ★ 惰性继承（不物化）⇒ 旧码表（只有层级 `blend`、片段没有 `blend` 字段）导入后，
          "沿用层值作为该层所有片段的初值" = 本函数返回层值，**行为与改动前逐值一致**。
        ⚠ 下游（timeline/preview 组装 `LayerDrawState`）**必须**用本函数取 blend，不要直接读
          `it.blend`（那会丢掉继承）。
        """
        b = str(getattr(item, "blend", "") or "")
        if b in BLEND_MODES:
            return b
        return self.blend if self.blend in BLEND_MODES else "normal"

    def effective_opacity(self, item) -> float:
        """该片段**此刻生效**的不透明度：本素材自设了就用它，否则继承本层（见 `effective_blend`）。"""
        o = getattr(item, "opacity", None)
        if o is not None:
            return min(1.0, max(0.0, _f(o, 1.0)))
        return min(1.0, max(0.0, _f(self.opacity, 1.0)))

    def materialize_item_blend(self) -> "CompLayer":
        """★ **显式迁移**：把本层的 blend/opacity **物化**到"还没设过自己值"的片段上。

        ⚠ **默认不需要调用** —— `effective_blend()/effective_opacity()` 已经"沿用层值"（惰性继承），
          旧码表导入后行为逐值不变。只有在确实希望"**以后改层值不再影响这些片段**"时才调用
          （调用后该层值对这些片段失效，成为它们各自的初始值）。
        """
        for it in self.items:
            if not isinstance(it, CueItem):
                continue
            if not (it.blend or ""):
                it.blend = self.blend
            if it.opacity is None:
                it.opacity = self.opacity
        return self

    # ---- ★ #15 同层"顶掉"（PR 覆盖）----
    def overwrite(self, moved_item, items_check=None) -> dict:
        """把 `moved_item` **覆盖到的同层其它片段**按 PR 覆盖语义**删/裁**（就地生效）。

        返回 `{"removed":[item_id…], "trimmed":[item_id…], "plan":[…]}`；
        **不推进撤销栈**（由 window 在"移动/落层"时整次记一次 undo）。

        `items_check` = 参与判定并被执行的那个列表；`None` ⇒ 用本层的 `self.items`。
        ⚠ 调用时机由 window 决定：**只在"拖动 / 落层"时**调；`add_cue` / 粘贴 / 复制片段
          **不调**（保持叠加语义）；历史重叠数据**不回溯**（下次移动才收敛）。
        """
        return apply_overwrite(self.items if items_check is None else items_check, moved_item)

    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"lyr_id": self.lyr_id, "name": self.name, "z": _i(self.z),
                "blend": self.blend, "opacity": _f(self.opacity, 1.0),
                "items": [it.to_state_dict() for it in self.items if isinstance(it, CueItem)]}

    @classmethod
    def from_state_dict(cls, d) -> "CompLayer":
        d = d if isinstance(d, dict) else {}
        ly = cls(lyr_id=str(d.get("lyr_id") or ""), name=str(d.get("name") or ""),
                 z=_i(d.get("z")), blend=str(d.get("blend") or "normal"),
                 opacity=_f(d.get("opacity"), 1.0))
        for itd in _as_list(d.get("items")):
            if isinstance(itd, dict):
                ly.items.append(CueItem.from_state_dict(itd))
        return ly


@dataclass
class Composition:
    """一首曲目的编排（层容器）。文件里是 `composition.layers`。"""
    layers: list = field(default_factory=list)   # [CompLayer]

    def normalized(self) -> "Composition":
        for ly in self.layers:
            if isinstance(ly, CompLayer):
                ly.normalized()
        return self

    def sorted_layers(self) -> list:
        """按 z 升序（z 大的在上 ⇒ 绘制时后画）。**契约见架构方案 §4-D3。**"""
        return sorted([l for l in self.layers if isinstance(l, CompLayer)],
                      key=lambda l: l.z)

    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"layers": [ly.to_state_dict() for ly in self.layers if isinstance(ly, CompLayer)]}

    @classmethod
    def from_state_dict(cls, d) -> "Composition":
        d = d if isinstance(d, dict) else {}
        comp = cls()
        for lyd in _as_list(d.get("layers")):
            if isinstance(lyd, dict):
                comp.layers.append(CompLayer.from_state_dict(lyd))
        return comp


@dataclass
class TrackEntry:
    """一首歌的一份编排。"""
    track_key: str = ""
    identity: Identity = field(default_factory=Identity)
    composition: Composition = field(default_factory=Composition)

    @property
    def layers(self) -> list:
        """便捷别名：`track.layers` ≡ `track.composition.layers`。"""
        return self.composition.layers

    def normalized(self) -> "TrackEntry":
        self.composition.normalized()
        return self

    def sorted_layers(self) -> list:
        return self.composition.sorted_layers()

    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"track_key": self.track_key, "identity": self.identity.to_state_dict(),
                "composition": self.composition.to_state_dict()}

    @classmethod
    def from_state_dict(cls, d) -> "TrackEntry":
        d = d if isinstance(d, dict) else {}
        te = cls(track_key=str(d.get("track_key") or ""),
                 identity=Identity.from_state_dict(d.get("identity")),
                 composition=Composition.from_state_dict(d.get("composition")))
        te.identity.ensure_meta_id()
        return te


@dataclass
class CueSheet:
    """一张码表（= 一个 `.ercue` 包）。"""
    title: str = ""
    author: str = ""
    notes: str = ""
    format_version: int = FORMAT_VERSION
    min_app_version: str = ""
    requires_features: list = field(default_factory=list)
    generator: dict = field(default_factory=dict)
    created_at: str = ""
    media: list = field(default_factory=list)     # [MediaRef]
    tracks: list = field(default_factory=list)    # [TrackEntry]
    # ★ 需求 1：码表画布比例（像素）。**可选字段**，缺省 = 1920×1080（16:9）。
    #   这是"画布/输出规格"，与几何变换（xform/crop）**正交**（设计文档 §需求1）。
    res_w: int = 1920
    res_h: int = 1080

    def aspect(self) -> tuple:
        """画布宽高像素 `(res_w, res_h)`；非法（≤0 / 非数字）回落 `(1920, 1080)`。

        ⚠ 调用方（预览画布 / 导出）应据此算比例，**不要**再写死 `9/16`。
        """
        w, h = _i(self.res_w, 1920), _i(self.res_h, 1080)
        if w <= 0 or h <= 0:
            return (1920, 1080)
        return (w, h)

    # ---- 便捷查询 ----
    def media_by_id(self, clip_id) -> Optional[MediaRef]:
        for m in self.media:
            if isinstance(m, MediaRef) and m.clip_id == clip_id:
                return m
        return None

    def all_used_clip_ids(self) -> set:
        used = set()
        for t in self.tracks:
            for ly in t.layers:
                for it in ly.items:
                    if it.clip_id:
                        used.add(it.clip_id)
        return used

    def recompute_features(self) -> list:
        """委托 `features.recompute_features`（能力词表是那一个模块的唯一职责）。"""
        return _features.recompute_features(self)

    def track_by_identity(self, title="", artist="", duration_sec=0.0, fp_id="") -> Optional[TrackEntry]:
        """旧接口（只做 ① 指纹 → ② meta_key 两级）。**新代码应优先用 `match_track`。**"""
        if fp_id:
            for t in self.tracks:
                if t.identity.fp_id and t.identity.fp_id == fp_id:
                    return t
        key = Identity.meta_key(title, artist, duration_sec)
        for t in self.tracks:
            if t.identity.meta_id and t.identity.meta_id == key:
                return t
        return None

    def match_track(self, title="", artist="", duration_sec=0.0, fp_id="", file_name="",
                    file_md5=""):
        """认歌结果 → 码表曲目：返回 `(TrackEntry | None, 命中级别字符串)`。

        ★ **五级降级**（顺序 = 可靠性从高到低）：
          ⓪ `md5`          **音频文件的 md5**（★ 用户点名新增，**最高优先级**）——
                            同一文件是**确定**判据：改名、换目录、改标签都仍命中。
          ① `fingerprint`  指纹签名（`fp2-<hex>`；同机/异机重算一致，抗改名/换目录/换容器）
          ② `meta`         名 + 艺人 + 时长（`Identity.meta_key`）
          ③ `title+artist` 名 + 艺人（**忽略时长**；多命中时取 |时长差| 最小，
                           无时长取第一个并标 `title+artist?`）
          ④ `file`         文件名（去扩展名/括号后缀）
        全不中 → `(None, "")`。

        ★ **为什么 md5 排指纹前面**：md5 是"**就是这一个文件**"的确定性判据（零误命中）；
          指纹是概率判据（签名撞车概率极低但非零），meta 可能撞名（同名不同曲）。
          两者都命中时也应优先信 md5 ⇒ 顺序 `md5 → fingerprint → meta → title+artist → file`。
        ★ **兼容**：旧码表没有 `file_md5` / `fp_id` ⇒ 该级自动跳过、正常降级（`.get` 取不到即空串）。
        """
        if not self.tracks:
            return None, ""
        # ⓪ md5（★ 最高优先级）
        m = str(file_md5 or "").strip().lower()
        if m:
            for t in self.tracks:
                if str(t.identity.file_md5 or "").strip().lower() == m:
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

    def check_media(self, media_dir):
        """检查素材是否就位。返回 `(missing_names, present_map)`。

        `media_dir` 为 None / 不存在 → **按"全部缺失"处理**（用户只导入了 json 的情况，§3.4）。
        """
        import os
        present, missing = {}, []
        for m in self.media:
            p = os.path.join(media_dir, m.name) if media_dir else ""
            if p and os.path.isfile(p):
                present[m.clip_id] = p
            else:
                missing.append(m.name)
        return missing, present

    def normalized(self) -> "CueSheet":
        self.format_version = _i(self.format_version, FORMAT_VERSION)
        w, h = self.aspect()
        self.res_w, self.res_h = w, h                 # 非法比例回落 1920×1080
        for t in self.tracks:
            if isinstance(t, TrackEntry):
                t.normalized()
        return self

    # ---- 序列化（模型原生 / 秒形态；文件形态见 ercue_io）----
    def to_state_dict(self) -> dict:
        """内部状态快照（**秒**坐标）。文件形态见 ercue_io.sheet_to_file_dict()。"""
        return {"format_version": _i(self.format_version, FORMAT_VERSION),
                "generator": dict(self.generator or {}),
                "created_at": str(self.created_at or ""),
                "min_app_version": str(self.min_app_version or ""),
                "requires_features": list(self.requires_features or []),
                "author": str(self.author or ""),
                "title": str(self.title or ""), "notes": str(self.notes or ""),
                "res_w": _i(self.res_w, 1920), "res_h": _i(self.res_h, 1080),
                "media": [m.to_state_dict() for m in self.media if isinstance(m, MediaRef)],
                "tracks": [t.to_state_dict() for t in self.tracks if isinstance(t, TrackEntry)]}

    @classmethod
    def from_state_dict(cls, d) -> "CueSheet":
        """**容错解析：字段缺失/类型不对一律回落默认值，绝不抛异常**（导入必须不崩）。"""
        s = cls()
        if not isinstance(d, dict):
            return s
        s.format_version = _i(d.get("format_version"), FORMAT_VERSION)
        s.generator = d.get("generator") if isinstance(d.get("generator"), dict) else {}
        s.created_at = str(d.get("created_at") or "")
        s.min_app_version = str(d.get("min_app_version") or "")
        rf = d.get("requires_features")
        s.requires_features = [str(x) for x in rf] if isinstance(rf, list) else []
        s.author = str(d.get("author") or "")
        s.title = str(d.get("title") or "")
        s.notes = str(d.get("notes") or "")
        # ★ 需求 1 后加的可选字段：旧快照没有 ⇒ 回落 1920×1080（16:9）
        s.res_w = _i(d.get("res_w"), 1920)
        s.res_h = _i(d.get("res_h"), 1080)
        for md in _as_list(d.get("media")):
            if isinstance(md, dict) and md.get("clip_id"):
                s.media.append(MediaRef.from_state_dict(md))
        for td in _as_list(d.get("tracks")):
            if isinstance(td, dict):
                s.tracks.append(TrackEntry.from_state_dict(td))
        return s.normalized()


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    """数据模型自检：规范化边界 + meta_key + 五级认歌 + 原生往返。"""
    # 1) meta_key 逐字核对（规格书样例）
    assert Identity.meta_key("Cosmic String", "Rolling Contact", 372.4) == \
        "rollingcontact|cosmicstring|372"
    # 去括号后缀 / feat / 大小写 / 符号
    assert Identity.meta_key("Song (Radio Edit)", "DJ feat. X", 200.0) == "dj|song|200"
    # 时长取整到 2 秒：372.4 与 373.0 同 key
    assert Identity.meta_key("A", "B", 372.4) == Identity.meta_key("A", "B", 373.0)

    # 2) normalized 边界
    it = CueItem(at_sec=-5.0, speed=99.0, trans_in="bogus", trans_out="nope",
                 snap="weird", crop=[1, 2, 3], end_sec=-1.0).normalized()
    assert it.at_sec == 0.0 and it.speed == 3.0, it
    assert it.trans_in == "fade" and it.trans_out == "fade" and it.snap == "off", it
    assert it.crop is None, it.crop
    assert it.end_sec == 0.0, it.end_sec           # end ≥ at
    it2 = CueItem(speed=0.01, at_sec=10.0, end_sec=3.0, crop=[0, 0, 1, 1]).normalized()
    assert it2.speed == 0.1 and it2.end_sec == 10.0 and it2.crop == [0, 0, 1, 1]
    ly = CompLayer(blend="xxx", opacity=5.0).normalized()
    assert ly.blend == "normal" and ly.opacity == 1.0

    # 3) 五级认歌
    s = CueSheet()
    t1 = TrackEntry(track_key="k1", identity=Identity(
        fp_id="FP1", title="Cosmic String", artist="Rolling Contact",
        duration_sec=372.4, bpm=155.03, file_name="Rolling Contact - Cosmic String.flac"))
    t1.identity.ensure_meta_id()
    t2 = TrackEntry(track_key="k2", identity=Identity(
        title="Cosmic String", artist="Rolling Contact", duration_sec=300.0,
        file_name="Cosmic String (Live).flac"))
    t2.identity.ensure_meta_id()
    s.tracks = [t1, t2]
    # ⓪ md5（★ 最高优先级）：比大小写、且**压过**指纹/meta
    t1.identity.file_md5 = "D41D8CD98F00B204E9800998ECF8427E"
    assert s.match_track(title="Cosmic String", artist="Rolling Contact", duration_sec=372.4,
                         fp_id="FP1", file_md5="d41d8cd98f00b204e9800998ecf8427e")[1] == "md5"
    # ⓪ 旧码表没有 md5 ⇒ **跳过该级**、正常降级到指纹
    assert s.match_track(fp_id="FP1", file_md5="")[1] == "fingerprint"
    # ① 指纹
    assert s.match_track(fp_id="FP1")[1] == "fingerprint"
    # ② meta
    assert s.match_track("Cosmic String", "Rolling Contact", 372.4)[1] == "meta"
    # ③ title+artist 多命中 → 取 |时长差| 最小（用 250 秒避开两首的 meta key：372 与 300）
    got, lvl = s.match_track("Cosmic String", "Rolling Contact", 250.0)
    assert lvl == "title+artist" and got is t2, (lvl, got)
    # ③ 多命中且无时长 → 第一个 + 问号
    got, lvl = s.match_track("Cosmic String", "Rolling Contact", 0.0)
    assert lvl == "title+artist?" and got is t1, (lvl, got)
    # ④ 文件名
    s2 = CueSheet()
    t3 = TrackEntry(track_key="k3", identity=Identity(
        title="别的名字", artist="别人", duration_sec=1.0,
        file_name="Rolling Contact - Cosmic String.flac"))
    t3.identity.ensure_meta_id()
    s2.tracks = [t3]
    assert s2.match_track(file_name="Rolling Contact - Cosmic String.flac")[1] == "file"
    # 全不中
    assert s.match_track("Nope", "Nobody", 1.0, file_name="x.mp3") == (None, "")

    # 4) 原生（秒）往返
    s3 = CueSheet(title="自检", author="unit", media=[MediaRef(clip_id="c1", name="a.mp4")])
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A", duration_sec=100.0, bpm=128.0))
    te.identity.ensure_meta_id()
    lyx = CompLayer(lyr_id="L1", name="背景", z=0, blend="screen", opacity=0.9)
    lyx.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=12.384,
                             src_in_sec=2.0, speed=1.25, crop=[0.05, 0.0, 0.9, 1.0],
                             trans_in="glitch", trans_out_sec=0.5).normalized())
    te.composition.layers.append(lyx)
    s3.tracks.append(te)
    d = s3.to_state_dict()
    r = CueSheet.from_state_dict(d)
    assert r.title == "自检" and len(r.tracks) == 1 and r.tracks[0].layers[0].items[0].at_sec == 12.384
    assert r.to_state_dict() == d, "原生往返应完全一致"

    print("[cuesheet] 自检通过 ✓  meta_key=%s  五级认歌/规范化/原生往返 均 OK"
          % Identity.meta_key("Cosmic String", "Rolling Contact", 372.4))
    return True


if __name__ == "__main__":
    selftest()
