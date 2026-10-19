# -*- coding: utf-8 -*-
"""能力词表 + `requires_features` 自动推断。

★ 以 **规格书 §3.3（= 旧代码 `cuesheet.py:39-51/303-329`）为准**，不以设计文档为准。

★★ A3 修订（2026-10-01，团队裁定）：`trans_glitch` / `trans_zoom` **已从 `KNOWN_FEATURES`
  提升为 `SUPPORTED_FEATURES`**。原因：本软件 `render/compositor.py` 已真正实现这两种过渡，
  且**演出端引擎也能渲染**（备份 `engine.py:2841` zoom / `:2845` glitch）——旧代码把这两个
  能力判成"本版没有，将被忽略"是**自相矛盾**（软件实现了，却给自己报警）。
  两端能力表据此对齐。`multi_layer_cue` / `remote_source` 仍留在 `KNOWN_FEATURES`（确未实现）。

本模块**不 import 本包任何其它模块**（纯 duck-typing），以避免与 `cuesheet` 形成循环依赖。
"""

from __future__ import annotations

# 本版**支持**的能力（9 项）。`requires_features` 里出现不在此集合的名字 ⇒ 提示"将被忽略"。
# ★ A3：`trans_glitch` / `trans_zoom` 已并入（本软件实现了 + 演出端也能渲染）。
SUPPORTED_FEATURES = {
    "layer_multi",    # 码表内多层
    "clip_fade",      # 淡入淡出
    "clip_inout",     # 素材只取一段（入/出点）
    "clip_speed",     # 变速
    "clip_crop",      # 画面裁切
    "video_embed",    # 内嵌素材
    "beat_snap",      # 吸附到拍/小节/乐句
    "trans_glitch",   # ★ 已实现（compositor glitch；演出端 engine.py:2845）
    "trans_zoom",     # ★ 已实现（compositor zoom；演出端 engine.py:2841）
}

# 已知但本版不实现的能力（只做提示）。**只增不删**（旧实现的承诺）。
# ★ A3：glitch / zoom 已提为"支持"，此处只保留确未实现的两项。
KNOWN_FEATURES = set(SUPPORTED_FEATURES) | {
    "multi_layer_cue", "remote_source",
}


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _layers_of(track):
    """兼容两种结构：`track.composition.layers`（新）与 `track.layers`（旧）。"""
    comp = getattr(track, "composition", None)
    if comp is not None and hasattr(comp, "layers"):
        return getattr(comp, "layers", None) or []
    return getattr(track, "layers", None) or []


def recompute_features(sheet) -> list:
    """按内容自动推断 `sheet.requires_features` 并写回（规格书 §3.3 规则）。

    规则：
      · 层数 >1                     → layer_multi
      · 任一 item 的 trans_in/out ≠ "cut" → clip_fade
      · |src_in|>1e-6 或 src_out 非 None  → clip_inout
      · |speed-1|>1e-6              → clip_speed
      · crop 非空                    → clip_crop
      · snap ≠ off                   → beat_snap
      · trans_* == glitch            → trans_glitch
      · trans_* == zoom              → trans_zoom
      · 任一 media.embedded          → video_embed
      · 最终 `sorted(feats | 已有 requires_features)`（**不丢旧项**）
    """
    feats = set()
    for t in (getattr(sheet, "tracks", None) or []):
        layers = _layers_of(t)
        if len(layers) > 1:
            feats.add("layer_multi")
        for ly in layers:
            for it in (getattr(ly, "items", None) or []):
                tin = getattr(it, "trans_in", "")
                tout = getattr(it, "trans_out", "")
                if tin not in ("cut",) or tout not in ("cut",):
                    feats.add("clip_fade")
                if abs(_f(getattr(it, "src_in_sec", 0.0))) > 1e-6 \
                        or getattr(it, "src_out_sec", None) is not None:
                    feats.add("clip_inout")
                if abs(_f(getattr(it, "speed", 1.0), 1.0) - 1.0) > 1e-6:
                    feats.add("clip_speed")
                if getattr(it, "crop", None):
                    feats.add("clip_crop")
                if getattr(it, "snap", "") and getattr(it, "snap") != "off":
                    feats.add("beat_snap")
                if tin == "glitch" or tout == "glitch":
                    feats.add("trans_glitch")
                if tin == "zoom" or tout == "zoom":
                    feats.add("trans_zoom")
    for m in (getattr(sheet, "media", None) or []):
        if getattr(m, "embedded", False):
            feats.add("video_embed")

    merged = feats | set(getattr(sheet, "requires_features", None) or [])
    sheet.requires_features = sorted(merged)
    return sheet.requires_features


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    """能力词表与自动推断自检。"""
    # 词表大小与成员（★ A3：7 → 9，glitch/zoom 已提为"支持"）
    assert len(SUPPORTED_FEATURES) == 9, SUPPORTED_FEATURES
    assert SUPPORTED_FEATURES == {
        "layer_multi", "clip_fade", "clip_inout", "clip_speed",
        "clip_crop", "video_embed", "beat_snap",
        "trans_glitch", "trans_zoom"}, SUPPORTED_FEATURES
    assert "trans_glitch" in SUPPORTED_FEATURES and "trans_zoom" in SUPPORTED_FEATURES
    assert "multi_layer_cue" in KNOWN_FEATURES and "multi_layer_cue" not in SUPPORTED_FEATURES
    assert "remote_source" in KNOWN_FEATURES and "remote_source" not in SUPPORTED_FEATURES
    assert SUPPORTED_FEATURES <= KNOWN_FEATURES  # 只增不删

    # 轻量替身（不依赖 cuesheet，验证 duck-typing 推断）
    class _It:
        def __init__(self, **kw):
            self.trans_in = "cut"
            self.trans_out = "cut"
            self.src_in_sec = 0.0
            self.src_out_sec = None
            self.speed = 1.0
            self.crop = None
            self.snap = "off"
            for k, v in kw.items():
                setattr(self, k, v)

    class _Ly:
        def __init__(self, items):
            self.items = items

    class _Comp:
        def __init__(self, layers):
            self.layers = layers

    class _T:
        def __init__(self, layers):
            self.composition = _Comp(layers)

    class _M:
        def __init__(self, emb):
            self.embedded = emb

    class _S:
        def __init__(self, tracks, media, rf=None):
            self.tracks = tracks
            self.media = media
            self.requires_features = rf or []

    it1 = _It(trans_in="glitch", speed=1.25, crop=[0, 0, 1, 1], snap="bar", src_in_sec=2.0)
    it2 = _It(trans_in="cut", trans_out="cut")
    s = _S([_T([_Ly([it1, it2])])], [_M(False)])
    got = recompute_features(s)
    for want in ("clip_fade", "clip_inout", "clip_speed", "clip_crop", "beat_snap", "trans_glitch"):
        assert want in got, (want, got)
    assert "layer_multi" not in got, got

    # 多层 + 内嵌 + 保留已有项（不丢旧项）
    s2 = _S([_T([_Ly([it2]), _Ly([it2])])], [_M(True)], rf=["未来的能力", "clip_crop"])
    got2 = recompute_features(s2)
    assert "layer_multi" in got2 and "video_embed" in got2, got2
    assert "未来的能力" in got2 and "clip_crop" in got2, got2
    assert got2 == sorted(got2), got2

    print("[features] 自检通过 ✓  SUPPORTED=%d  KNOWN=%d  推断示例=%s"
          % (len(SUPPORTED_FEATURES), len(KNOWN_FEATURES), got))
    return True


if __name__ == "__main__":
    selftest()
