# -*- coding: utf-8 -*-
"""★ **导出前检查**（纯逻辑、零 Qt）—— 旧实现从未做过，是本项目该补的短板。

规格书 §9.7 / §7.4：「导出前检查：缺素材 / 超长 的清单」。
本模块把这份清单**纯函数化**（`check_sheet()`），与"弹窗"彻底分离：

    · `check_sheet()`            → 返回 `Report`（可离线单测，不碰 Qt）
    · `check_embed_size()`       → 内嵌素材体积是否超上限（可注入 `size_of` 假体积）
    · UI 层 `ui/dialogs.ask_export()` → 只负责把 `Report` 摆给用户看 + 收「仍然导出 / 取消」

两类问题（规格书 §15 最后一条 + 任务书）：
  · **缺素材**（重要提示）→ `report.missing`，**阻止导出**（默认焦点在"取消"），
    允许"仍然导出"（此时素材未内嵌，导出后别人打不开对应片段——与规格书一致）。
  · **超长**（警告，可继续导出）→ `report.overlong`：
       - `end`/`at` 落点超出**曲目总长**；
       - 片段时长超过**素材本身时长**（用了素材没有的部分）。

★ 变更说明：原先还有第三类「**未吸附**」——那是"吸附到拍/小节/乐句"功能的产物。
  用户明确要求**去掉吸附功能**，故本模块**一并移除**这一类检查（吸附已不存在，
  "未落网格"无从谈起）。`CueItem.snap` 字段仍保留，仅为**旧文件格式兼容**。

本模块**零 Qt**，可离线自检：`python -m ercue.model.precheck`
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from . import units

# 内嵌素材"提醒"阈值：1 GB。
# ⚠ 语义已变（2026-10-17 用户要求）：**不再是硬上限** —— 超过只弹窗提醒、由用户决定是否继续内嵌。
#   历史：原 500 MB 硬上限（规格书 §7.3 之 3）会引起"自动取消内嵌"，用户明确要求取消该限制。
EMBED_WARN_BYTES = 1024 * 1024 * 1024
# 兼容别名：旧名保留（值同步），勿在新代码使用
EMBED_LIMIT_BYTES = EMBED_WARN_BYTES
# 超长判定容差（秒）
OVERLONG_TOL_SEC = 1e-3


# ==========================================================================
# 数据结构
# ==========================================================================
@dataclass
class MissingClip:
    clip_id: str = ""
    name: str = ""


@dataclass
class OverlongItem:
    track_index: int = 0
    lyr_id: str = ""
    item_id: str = ""
    at_sec: float = 0.0
    end_sec: Optional[float] = None
    total_sec: float = 0.0                 # 曲目总长
    media_duration_sec: float = 0.0        # 素材本身时长（0 = 未知）
    reason: str = ""                       # "beyond_track"（超出曲目）/ "beyond_media"（超出素材）


@dataclass
class Report:
    """一次导出前检查的完整清单。"""

    missing: list = field(default_factory=list)       # [MissingClip]
    overlong: list = field(default_factory=list)      # [OverlongItem]
    notes: list = field(default_factory=list)         # 其它提示，如"这张码表还没有曲目"

    @property
    def blocking(self) -> bool:
        """是否有**阻止导出**的问题（缺素材）。缺素材可"强制导出"，默认拦截。"""
        return bool(self.missing)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.overlong or self.notes)

    def counts(self) -> dict:
        return {"missing": len(self.missing), "overlong": len(self.overlong),
                "notes": len(self.notes)}

    def summary_lines(self) -> list:
        """面向用户的中文清单（纯字符串，便于日志 / 单测 / 弹窗共用）。"""
        out = []
        if self.missing:
            names = "、".join([(m.name or m.clip_id) for m in self.missing[:8]])
            more = "" if len(self.missing) <= 8 else "…等 %d 个" % len(self.missing)
            out.append("【缺素材】%d 个素材没找到：%s%s"
                       % (len(self.missing), names, more))
        if self.overlong:
            out.append("【超长】%d 个片段的时长/落点超出范围：" % len(self.overlong))
            for it in self.overlong[:8]:
                why = ("超出曲目总长" if it.reason == "beyond_track" else "超出素材本身时长")
                out.append("    · %s（层 %s）%s" % (it.item_id, it.lyr_id, why))
        out.extend(self.notes)
        return out


# ==========================================================================
# 纯检查
# ==========================================================================
def _track_total_sec(track, fallback=0.0) -> float:
    """曲目总长（秒）：优先 `identity.duration_sec`（>1s 视为有效），否则取最晚片段终点。"""
    dur = 0.0
    idt = getattr(track, "identity", None)
    if idt is not None:
        dur = units.to_float(getattr(idt, "duration_sec", 0.0))
    if dur > 1.0:
        return dur
    last = 0.0
    for ly in getattr(track, "layers", []) or []:
        for it in getattr(ly, "items", []) or []:
            end = it.end_sec if it.end_sec is not None else it.at_sec
            last = max(last, units.to_float(end))
    return max(last, units.to_float(fallback))


def check_sheet(sheet, media_paths=None, track_totals=None) -> Report:
    """★ 导出前检查（**纯函数**，不碰 Qt、不弹窗）。

    `sheet`        : `CueSheet`（可传 None → 返回一条 note）
    `media_paths`  : `{clip_id: 磁盘路径}`；缺失即"素材没找到"
    `track_totals` : 可选，`[每首曲目的总长(秒)]`，由调用方注入（UI 用真实音频时长；
                     不传则用 `identity.duration_sec` 或最晚片段终点推算）
    """
    rep = Report()
    if sheet is None:
        rep.notes.append("没有可导出的码表。")
        return rep

    paths = media_paths if isinstance(media_paths, dict) else {}

    # ---- 1) 缺素材（重要提示）----
    for m in getattr(sheet, "media", []) or []:
        p = paths.get(m.clip_id) or ""
        if not (p and os.path.isfile(p)):
            rep.missing.append(MissingClip(clip_id=m.clip_id, name=m.name or m.clip_id))

    # ---- 2) 超长 ----
    tracks = list(getattr(sheet, "tracks", []) or [])
    if not tracks:
        rep.notes.append("这张码表还没有曲目。")
    for ti, tr in enumerate(tracks):
        if track_totals is not None and 0 <= ti < len(track_totals):
            total = units.to_float(track_totals[ti]) or _track_total_sec(tr)
        else:
            total = _track_total_sec(tr)
        for ly in getattr(tr, "layers", []) or []:
            for it in getattr(ly, "items", []) or []:
                at = units.to_float(it.at_sec)
                end = None if it.end_sec is None else units.to_float(it.end_sec)
                # 超出曲目总长（起点或终点）
                if total > 0.0 and ((end is not None and end > total + OVERLONG_TOL_SEC)
                                    or at > total + OVERLONG_TOL_SEC):
                    rep.overlong.append(OverlongItem(
                        track_index=ti, lyr_id=ly.lyr_id, item_id=it.item_id,
                        at_sec=at, end_sec=end, total_sec=total, reason="beyond_track"))
                else:
                    # 超出素材本身时长（只在该片段有明确终点、且素材时长已知时判）
                    m = sheet.media_by_id(it.clip_id) if hasattr(sheet, "media_by_id") else None
                    mdur = units.to_float(getattr(m, "duration_sec", 0.0)) if m else 0.0
                    if end is not None and mdur > 0.0:
                        avail = mdur - units.to_float(it.src_in_sec)
                        # ★ 2026-10-19 审计修复：片段用掉的源时长要乘 speed
                        #   （模型里已有正确口径 `item_source_used_sec()`，这里补同一逻辑）。
                        _spd = max(1e-6, units.to_float(getattr(it, "speed", 1.0) or 1.0))
                        if avail > 0.0 and (end - at) * _spd > avail + OVERLONG_TOL_SEC:
                            rep.overlong.append(OverlongItem(
                                track_index=ti, lyr_id=ly.lyr_id, item_id=it.item_id,
                                at_sec=at, end_sec=end, total_sec=total,
                                media_duration_sec=mdur, reason="beyond_media"))
    # ★★ 2026-10-19 审计修复：**悬空 clip_id**（片段指向一个不在 media[] 里的素材）。
    #   旧实现只遍历 `sheet.media` 判"文件在不在"，完全没看 items 引用了什么 ⇒
    #   素材被删掉后留下的片段照样导出，演出端整条 item 直接跳过（那段画面凭空消失），
    #   而导出前检查还报"干净"。
    try:
        _known = set()
        for _m in getattr(sheet, "media", []) or []:
            _cid = getattr(_m, "clip_id", "")
            if _cid:
                _known.add(_cid)
        _seen = set()
        for _tr in getattr(sheet, "tracks", []) or []:
            for _ly in getattr(_tr, "layers", []) or []:
                for _it in getattr(_ly, "items", []) or []:
                    _cid = str(getattr(_it, "clip_id", "") or "")
                    if _cid and _cid not in _known and _cid not in _seen:
                        _seen.add(_cid)
                        rep.missing.append(MissingClip(
                            clip_id=_cid, name="（片段引用的素材不在素材表里）"))
    except Exception:                                            # noqa: BLE001
        pass
    return rep


# ==========================================================================
# 内嵌素材体积保护
# ==========================================================================
def check_embed_size(sheet, media_paths=None, limit=EMBED_WARN_BYTES, size_of=None):
    """内嵌素材是否**超过提醒阈值**。返回 `(ok, total_bytes, present_names)`。

    ⚠ 语义（2026-10-17 起）：`ok=False` 仅表示"超过 1 GB 提醒阈值"，
    **不再表示"禁止内嵌"** —— 是否继续由调用方（UI 弹窗）与用户决定。
    `limit` 可传其它阈值；`size_of(path) -> int` 可注入（测试用假体积）。
    """
    if size_of is None:
        size_of = os.path.getsize
    total = 0
    names = []
    for m in getattr(sheet, "media", []) or []:
        p = (media_paths or {}).get(m.clip_id) or ""
        if not (p and os.path.isfile(p)):
            continue
        try:
            total += int(size_of(p))
        except OSError:
            continue
        names.append(m.name or m.clip_id)
    return (total <= int(limit), total, names)


def human_mb(n: int) -> str:
    return "%.1f MB" % (float(n) / (1024.0 * 1024.0))


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    import tempfile

    from .cuesheet import CompLayer, CueItem, CueSheet, MediaRef, TrackEntry, Identity

    sheet = CueSheet(title="预检自检")
    sheet.media.append(MediaRef(clip_id="c1", name="a.mp4", duration_sec=10.0))
    sheet.media.append(MediaRef(clip_id="c2", name="b.mp4", duration_sec=100.0))
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=60.0, bpm=120.0))
    te.identity.ensure_meta_id()
    ly = CompLayer(lyr_id="L1", name="L1", z=0)
    # i1：素材够长、落点也在曲目内 → 不报
    ly.items.append(CueItem(item_id="i1", clip_id="c2", at_sec=4.0, end_sec=8.0).normalized())
    # i2：超出曲目总长（end=80 > 60）
    ly.items.append(CueItem(item_id="i2", clip_id="c1", at_sec=40.0, end_sec=80.0).normalized())
    # i3：用时 40s > 素材 c1 的 10s → 超素材
    ly.items.append(CueItem(item_id="i3", clip_id="c1", at_sec=10.0, end_sec=50.0).normalized())
    te.composition.layers.append(ly)
    sheet.tracks.append(te)

    tmp = tempfile.mkdtemp(prefix="ercue_chk_")
    pa = os.path.join(tmp, "a.mp4")
    with open(pa, "wb") as f:
        f.write(b"x")
    # b.mp4 故意不落盘 → 缺素材
    paths = {"c1": pa}

    rep = check_sheet(sheet, paths)
    # 缺 1（b.mp4），超长 2（i2 超曲目；i3 超素材）
    assert len(rep.missing) == 1 and rep.missing[0].clip_id == "c2", rep.missing
    assert rep.blocking is True
    assert len(rep.overlong) == 2, [(o.item_id, o.reason) for o in rep.overlong]
    reasons = sorted(o.reason for o in rep.overlong)
    assert reasons == ["beyond_media", "beyond_track"], reasons
    assert "缺素材" in rep.summary_lines()[0]
    # 吸附功能已移除：Report 不再有 unsnapped 这一类
    assert not hasattr(rep, "unsnapped")
    assert set(rep.counts()) == {"missing", "overlong", "notes"}, rep.counts()

    # 内嵌体积提醒阈值（注入假体积）
    # 1.5 GB ⇒ 超阈值（over）
    ok, total, names = check_embed_size(sheet, paths, size_of=lambda p: 1536 * 1024 * 1024)
    assert ok is False and total == 1536 * 1024 * 1024 and names == ["a.mp4"], (ok, total, names)
    # 600 MB ⇒ 已在新阈值（1 GB）内：**不再阻止**（旧行为会拒绝）
    ok_mid, total_mid, _ = check_embed_size(sheet, paths, size_of=lambda p: 600 * 1024 * 1024)
    assert ok_mid is True and total_mid == 600 * 1024 * 1024, (ok_mid, total_mid)
    ok2, total2, _ = check_embed_size(sheet, paths, size_of=lambda p: 1024)
    assert ok2 is True and total2 == 1024

    # 空码表
    assert check_sheet(None).notes and not check_sheet(None).blocking

    print("[precheck] 自检通过 ✓  缺%d 超长%d  内嵌上限=%s（吸附检查已移除）"
          % (len(rep.missing), len(rep.overlong), human_mb(EMBED_LIMIT_BYTES)))
    return True


if __name__ == "__main__":
    selftest()
