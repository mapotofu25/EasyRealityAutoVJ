# -*- coding: utf-8 -*-
r"""曲库扫描核心（**与 ERLIB 曲库分析逐字同口径**）。

为什么单独抽出来
----------------
同一个文件在 ER 主程序扫描、ERLIB 扫描、详情页「按 BPM 重新分段」三处都会算
「网格 → 段落/能量」。此前是**三份复制代码**，任何一处改了阈值/调用顺序，另外
两处就悄悄漂移 —— 用户实测同一首歌两个软件切出来的段落/能量不一致。

现在唯一实现放在这里：
  · `decode_audio(path)`     —— 解码（单声道 float32 + 真实时长）
  · `grid_for_signal(...)`   —— 节拍网格（含 2026-11-18 的八拍相位反推修正）
  · `analyze_signal(...)`    —— 网格 + 段落（structure/timbre/energy 三种模式）

⚠ 调用顺序（与 `ERLIB_曲库分析/src/erlib/scanner.py::scan_one` 完全一致）：
     grid_from_signal(use_vdj=False, out_feats=...)
     → anchor 相位反推（用不吸附网格的 raw 边界）
     → 分段模式（默认 structure；环境变量/erlib_settings.json 可切 timbre/energy）
     → take_folded_anchor() 写回 grid.anchor，phrase 同步 −4
     → raw（structure/timbre 模式直接复用；energy 模式再算一遍 phrase_grid）

改这里之前先看 `ERLIB_曲库分析` 的同名实现；两个软件的段落/能量必须逐值一致。
"""

from __future__ import annotations

import io
import json
import os

import numpy as np


# ==========================================================================
# 解码
# ==========================================================================
def decode_audio(path):
    """解码整首 → `(sig, sr, dur)`；与 `erlib.scanner.decode()` 逐字一致。

    `dur` 必须取 `sf.info().duration`（不是 `len(sig)/sr`）—— mp3 编码器延迟
    会让两者差几毫秒，网格 anchor 会跟着偏。
    """
    import soundfile as sf

    info = sf.info(path)
    dur = float(info.duration)
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    sig = np.mean(data, axis=1).astype(np.float32, copy=False)
    return sig, int(sr), dur


# ==========================================================================
# 分段模式（与 ERLIB 共用同一份设置）
# ==========================================================================
def seg_mode():
    """`structure`（默认）/ `timbre` / `energy`。

    优先级：环境变量 `ERLIB_SEG_MODE` > `%LOCALAPPDATA%\\AutoVJ\\erlib_settings.json`
    的 `seg_mode` > 默认 `structure`。**必须读同一份设置**，否则用户在 ERLIB 里
    切成音色分段后，主程序扫描仍是另一套边界。
    """
    m = (os.environ.get("ERLIB_SEG_MODE") or "").strip().lower()
    if not m:
        try:
            sp = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                              "AutoVJ", "erlib_settings.json")
            with io.open(sp, "r", encoding="utf-8") as f:
                m = str((json.load(f) or {}).get("seg_mode") or "").strip().lower()
        except Exception:                                        # noqa: BLE001
            m = ""
    return m or "structure"


# ==========================================================================
# 八拍相位反推修正（2026-11-18）
# ==========================================================================
def anchor_phase_fix(grid, feats):
    """用**不吸附网格**的分段边界反推八拍相位，必要时把 anchor 挪回对齐位置。

    现场问题（用户）：「八拍线整首偏 4 拍」。真实段落边界落在 ±4 拍外时，说明
    整首网格晚了半句。只在证据一致（边界相位离散度 ≤1 拍、偏移 ≥1.5 拍、
    边界数 ≥6）时生效 —— 与 ERLIB `scanner.scan_one` 的判据逐字一致。
    """
    if not isinstance(grid, dict) or not grid:
        return
    try:
        import sections as _sec
        f = feats.get("f")
        dur = float(feats.get("dur") or 0.0)
        bl = float((grid or {}).get("beat_len") or 0.0)
        an = float((grid or {}).get("anchor") or 0.0)
        if f is None or dur <= 0 or bl <= 0:
            return
        raw = _sec.auto_segments(f, dur=dur, grid=None) or []
        ts = [float(x["t0"]) for x in raw[1:]]
        if len(ts) < 6:
            return
        mods = np.array([((t - an) / bl) % 8.0 for t in ts])
        cand = np.arange(0.0, 8.0, 0.25)

        def _cd(x, c):
            return (x - c + 4.0) % 8.0 - 4.0

        best = min(cand, key=lambda c: float(np.mean(np.abs(_cd(mods, c)))))
        dev = float(np.mean(np.abs(_cd(mods, best))))
        if abs(best) >= 1.5 and dev <= 1.0:
            sh = -int(round(best))
            new = (an + sh * bl) % (8.0 * bl)
            grid["anchor"] = round(float(new), 4)
            grid["phase_fix"] = int(sh)
            base = float(grid.get("base_anchor") or 0.0)
            grid["phrase"] = int(round((new - base) / bl)) % 8
            for sg in (grid.get("segments") or []):
                if isinstance(sg, dict) and "anchor" in sg:
                    sg["anchor"] = round(float(sg["anchor"]) + sh * bl, 4)
    except Exception:                                            # noqa: BLE001
        pass


# ==========================================================================
# 网格
# ==========================================================================
def _grid_and_feats(sig, sr, dur, path=""):
    """整曲网格 + 复用给段落分段的 STFT 特征。失败返回 `({}, {})`。"""
    try:
        import beatgrid as _bg
        feats = {}
        g = _bg.grid_from_signal(sig, sr, None, path=path, dur=dur,
                                 use_vdj=False, out_feats=feats)
        if g:
            anchor_phase_fix(g, feats)
        return (g or {}), feats
    except Exception:                                            # noqa: BLE001
        return {}, {}


def grid_for_signal(sig, sr, dur, path=""):
    """整曲网格（ERLIB 口径：`use_vdj=False` + 相位反推）。失败返回 {}。"""
    return _grid_and_feats(sig, sr, dur, path=path)[0]


# ==========================================================================
# 段落 / 能量
# ==========================================================================
def segments_for_signal(sig, sr, dur, grid, feats, mode=None, grid_manual=None):
    """返回 `(sections, sections_raw)`；完全照 ERLIB 扫描器。

    `sig/sr` 只有 timbre 模式用得到（`timbre_seg.edges` 要原始波形）。
    `feats` = `beatgrid.grid_from_signal(out_feats=...)` 的产物（`f`/`dur`）。
    `grid_manual` = 用户手改的 BPM/anchor/分段；有就用**生效网格**做段落吸附，
    与旧版 `_one()` 行为一致（ERLIB 没有手动层，传 None 时逐值同源）。
    """
    import sections as S

    mode = (mode or seg_mode()).strip().lower()
    f = (feats or {}).get("f")
    d2 = float((feats or {}).get("dur") or 0.0)
    if f is None or d2 <= 0 or not grid:
        return None, None
    gm = S.grid_from_meta({"grid": grid,
                           "grid_manual": grid_manual or {}}) or None
    _g_for_struct = gm or grid

    sg = None
    from_struct = False
    if mode == "structure":
        try:
            import structure_seg as _sseg
            base = S.auto_segments(f, dur=d2, grid=gm)
            # ★ 2026-11-18：auto_segments 可能折了 anchor（返回的是折后结果）。
            #   必须**立刻**把折后 anchor 写回 grid/gm，再用它做结构边界/吸附；
            #   否则 sections 吸在旧 anchor、grid 写新 anchor ⇒ 差 4 拍。
            _af0 = S.take_folded_anchor()
            if _af0 is not None and isinstance(grid, dict):
                grid["anchor"] = _af0
                try:
                    grid["phrase"] = int((int(grid.get("phrase") or 0) - 4) % 8)
                except (TypeError, ValueError):
                    grid["phrase"] = 0
                gm = S.grid_from_meta(
                    {"grid": grid, "grid_manual": grid_manual or {}}) or gm
                _g_for_struct = gm or grid
            se = _sseg.structure_edges_hybrid(f, d2, _g_for_struct, base)
            if se and len(se) >= 3:
                sg2 = S.segments_from_edges(f, se, dur=d2, grid=gm, post=False)
                lim = max(len(base or []) + 3, int(len(base or []) * 1.4))
                if sg2 and len(sg2) <= lim:
                    sg = sg2
                    from_struct = True
        except Exception:                                        # noqa: BLE001
            sg = None
    elif mode == "timbre":
        try:
            import timbre_seg as _tseg
            te = _tseg.edges(sig, sr, grid, dur=dur)
            if te and len(te) >= 3:
                sg = S.segments_from_edges(f, te, dur=d2, grid=gm)
                from_struct = True
        except Exception:                                        # noqa: BLE001
            sg = None

    if sg is None:
        sg = S.auto_segments(f, dur=d2, grid=gm)

    # 折过锚点 ⇒ 写回网格；phrase 同步 −4（否则 (anchor, phrase) 自相矛盾）
    af = S.take_folded_anchor()
    if af is not None and isinstance(grid, dict):
        grid["anchor"] = af
        try:
            ph = int(grid.get("phrase") or 0)
            grid["phrase"] = int((ph - 4) % 8)
        except (TypeError, ValueError):
            grid["phrase"] = 0

    if from_struct:
        sgr = sg
    else:
        sgr = S.auto_segments(f, dur=d2, grid=None, phrase_grid=gm)
    return (sg or None), (sgr or None)


def analyze_signal(sig, sr, dur, path="", grid_manual=None):
    """一次算完网格 + 段落；返回可直接并进 `music_meta[path]` 的 dict。"""
    grid, feats = _grid_and_feats(sig, sr, dur, path=path)
    out = {}
    if not grid:
        return out
    out["grid"] = grid
    try:
        sg, sgr = segments_for_signal(sig, sr, dur, grid, feats,
                                      grid_manual=grid_manual)
        if sg:
            out["sections"] = sg
            out["sections_raw"] = sgr
            out["sections_auto"] = True
    except Exception:                                            # noqa: BLE001
        pass
    return out
