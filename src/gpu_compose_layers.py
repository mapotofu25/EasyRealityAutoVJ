# -*- coding: utf-8 -*-
"""把 engine 的 Layer 列表收集成 GPU 场景描述（供 gpu_stage 合成）。

这里的职责是**复刻 CPU `_compose_to`/`_draw_layer` 的业务语义**：
图层显隐 / solo / 静音隐藏 / Kv / 码表子层 / 转场（fade/slide/zoom/glitch）/
大小位置旋转 / cover 与 contain / 透明度 / 混合模式。

gpu_stage 只负责“按描述画四边形 + LUT”。

任何一项无法在 GPU 上正确复刻时返回 `None`，引擎会整帧回退 CPU 合成，
绝不带着错误画面硬上 GPU。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

_SUPPORTED_BLENDS = {"normal", "sourceover", "add", "plus", "screen",
                     "multiply", "lighten", "darken", "subtract"}


def collect(engine, layers, snap, W, H, cue_view=None):
    """返回场景描述（从上到下）；不支持时返回 None。

    `cue_view`：码表画布在输出画布里的 contain 区域 `(w, h, ox, oy)`。
    """
    try:
        try:
            iv = int((engine._cp_cfg or engine.cfg["auto"]).get("intensity", 1))
        except Exception:                                      # noqa: BLE001
            iv = 1
        inten = 0.0 if iv < 0 else [0.6, 1.0, 1.4, 2.0][max(0, min(3, iv))]
        pulse = 1.0 + float(snap.get("bass", 0.0) or 0.0) * 0.08 * inten
        silent = bool(getattr(engine, "_silent_now_debounced", False))
        solo_on = any(getattr(l, "solo", False) for l in layers)
        now = time.perf_counter()

        cue_w, cue_h, cue_ox, cue_oy = cue_view or (W, H, 0, 0)
        cue_dx = cue_ox + cue_w / 2.0 - W / 2.0
        cue_dy = cue_oy + cue_h / 2.0 - H / 2.0

        def xf_of(lay, idx):
            try:
                if idx is not None and int(idx) >= 0 and callable(
                        getattr(lay, "clip_xform", None)):
                    return lay.clip_xform(int(idx))
            except Exception:                                  # noqa: BLE001
                pass
            return None

        def make(lay, img, item, xf, opacity, scale_mul=1.0, dx=0.0, dy=0.0,
                 ref_w=None, ref_h=None, pulse_override=None, slot=None):
            if img is None or img.isNull():
                return None
            if xf:
                us = float(xf.get("scale", 1.0) or 1.0)
                ux = float(xf.get("x", 0.0) or 0.0)
                uy = float(xf.get("y", 0.0) or 0.0)
                rot = float(xf.get("rot", 0.0) or 0.0)
            elif item is not None:
                us = float(getattr(item, "img_scale", 1.0) or 1.0)
                ux = float(getattr(item, "img_x", 0.0) or 0.0)
                uy = float(getattr(item, "img_y", 0.0) or 0.0)
                rot = float(getattr(item, "img_rot", 0.0) or 0.0)
            else:
                us, ux, uy, rot = 1.0, 0.0, 0.0, 0.0
            custom = (abs(us - 1.0) > 1e-3 or abs(ux) > 1e-3
                      or abs(uy) > 1e-3 or abs(rot) > 1e-3)
            RW = float(W if ref_w is None else ref_w)
            RH = float(H if ref_h is None else ref_h)
            iw, ih = img.width(), img.height()
            if iw <= 0 or ih <= 0:
                return None
            _pulse = pulse if pulse_override is None else float(pulse_override)
            if custom:
                s = min(RW / iw, RH / ih) * _pulse * us * scale_mul
            else:
                s = max(RW / iw, RH / ih) * _pulse * scale_mul
            tw, th = iw * s, ih * s
            # 与 CPU `paint_cover` 一致：四边形以画面中心对齐，再叠加
            # 用户位移。GPU 的 x/y 是**左上角**，不是中心点 —— 少了这个
            # 居中量，缩小的素材会贴在画布左上角。
            return {"img": img,
                    "opacity": max(0.0, min(1.0, float(opacity))),
                    "w": tw, "h": th,
                    "x": (RW - tw) / 2.0 + RW * ux / 100.0 + dx,
                    "y": (RH - th) / 2.0 + RH * uy / 100.0 + dy,
                    "rot": rot,
                    "blend": str(getattr(lay, "blend", "normal") or "normal"),
                    "slot": slot or ("L:%s" % (getattr(lay, "lid", "") or id(lay)))}

        def zoom_center(d, zm):
            """围绕当前四边形中心缩放（x/y 是左上角，不能只改 w/h）。"""
            if not d:
                return
            cx = float(d["x"]) + float(d["w"]) / 2.0
            cy = float(d["y"]) + float(d["h"]) / 2.0
            d["w"] = float(d["w"]) * zm
            d["h"] = float(d["h"]) * zm
            d["x"] = cx - d["w"] / 2.0
            d["y"] = cy - d["h"] / 2.0

        def flatten(img, lay):
            """`use_alpha=False`：透明区域填黑，作为实底画面（与 CPU 一致）。"""
            if img is None or img.isNull():
                return img
            if getattr(lay, "use_alpha", True) or not img.hasAlphaChannel():
                return img
            try:
                key = img.cacheKey()
                if (getattr(lay, "_alpha_key", None) != key
                        or getattr(lay, "_alpha_img", None) is None):
                    flat = QImage(img.width(), img.height(), QImage.Format_RGB32)
                    flat.fill(Qt.black)
                    fp = QPainter(flat)
                    fp.drawImage(0, 0, img)
                    fp.end()
                    lay._alpha_key, lay._alpha_img = key, flat
                return lay._alpha_img
            except Exception:                                  # noqa: BLE001
                return img

        def guard(lay, blend, op, img):
            """GPU 无法精确复刻的组合 ⇒ 整帧回退 CPU。"""
            if blend not in _SUPPORTED_BLENDS:
                return False
            # lighten/darken 用 GL_MAX/MIN，只有「不透明实底 + 满不透明度」与 Qt 等价
            if blend in ("lighten", "darken"):
                if op < 0.999 or (img is not None and img.hasAlphaChannel()):
                    return False
            # subtract 在 CPU 侧对半透明图层会二次叠不透明度；满不透明度才等价
            if blend == "subtract" and op < 0.999:
                return False
            return True

        out = []
        for lay in layers:
            if not getattr(lay, "visible", True):
                continue
            if solo_on and not getattr(lay, "solo", False):
                continue
            if getattr(lay, "hide_silent", False) and silent:
                continue

            # ---------------- 码表容器：子层直接变 GPU 描述 ----------------
            if getattr(lay, "is_cuesheet", False):
                if not getattr(lay, "cs_active", False):
                    continue
                base_op = max(0.0, min(1.0,
                                        float(getattr(lay, "opacity", 1.0) or 0.0)))
                # 描述列表是“从上到下”，而子层按 cs_states 顺序从下往上画；
                # 所以这里倒序收集，交给渲染器整体 reverse 后恢复正确叠放顺序。
                for ps in list(getattr(lay, "cs_states", None) or [])[::-1]:
                    if not getattr(ps, "active", False) or getattr(ps, "missing", False):
                        continue
                    if not getattr(ps, "clip_path", ""):
                        continue
                    op = (base_op * float(getattr(ps, "opacity", 1.0) or 0.0)
                          * float(getattr(ps, "alpha", 1.0) or 0.0))
                    if op <= 0.002:
                        continue
                    img = engine._cue_frame(lay, ps)
                    if img is None or img.isNull():
                        continue
                    item = engine._cue_item(lay, ps.clip_path, ps.xform)
                    xf = getattr(ps, "xform", None)
                    tr = getattr(ps, "trans", "fade")
                    tr = tr if tr in ("fade", "cut", "slide", "zoom", "glitch") else "fade"
                    try:
                        prog = 1.0 if tr == "cut" else max(
                            0.0, min(1.0, float(getattr(ps, "trans_p", 1.0))))
                    except (TypeError, ValueError):
                        prog = 1.0
                    crop = getattr(ps, "crop", None)
                    if crop:
                        try:
                            cx, cy, cw, ch = [int(round(float(v))) for v in crop]
                            if cw > 0 and ch > 0:
                                img = img.copy(cx, cy, cw, ch)
                        except Exception:                  # noqa: BLE001
                            pass
                    slot = "C:%s:%s" % (getattr(lay, "lid", "") or id(lay),
                                        ps.clip_path)
                    # 码表子层不做能量脉冲（CPU `_draw_cuesheet_layer` 传 1.0）
                    d = make(lay, img, item, xf, op, ref_w=cue_w, ref_h=cue_h,
                             pulse_override=1.0, slot=slot)
                    if d is None:
                        continue
                    if cue_dx or cue_dy:
                        d["x"] += cue_dx
                        d["y"] += cue_dy
                    b = getattr(ps, "blend", "normal")
                    b = b if b in _SUPPORTED_BLENDS else "normal"
                    if not guard(lay, b, op, img):
                        return None
                    d["blend"] = b
                    out_dir = bool(getattr(ps, "trans_is_out", False))
                    sgn = -1.0 if out_dir else 1.0
                    if tr == "fade" and prog < 1.0:
                        d["opacity"] = op * prog
                    elif tr == "slide" and prog < 1.0:
                        d["x"] += sgn * d["w"] * (1.0 - prog)
                    elif tr == "zoom" and prog < 1.0:
                        zoom_center(d, 0.7 + 0.3 * prog)
                    elif tr == "glitch" and prog < 1.0 and abs(d["rot"]) < 1e-3:
                        d["glitch"] = True
                        d["glitch_seed"] = int(now * 1000)
                    out.append(d)
                continue

            # ---------------- 普通图层 ----------------
            is_kv = bool(getattr(lay, "is_kv", False))
            if is_kv and float(getattr(lay, "kv_on", 0.0) or 0.0) <= 0.01:
                continue
            p = getattr(lay, "player", None)
            if p is None:
                continue
            img = p.current() or getattr(lay, "_last_frame", None)
            if img is None or img.isNull():
                continue
            lay._last_frame = img
            img = flatten(img, lay)
            idx = lay.cur if 0 <= lay.cur < len(getattr(lay, "clips", [])) else -1
            item = lay.clips[idx] if idx >= 0 else None
            xf = xf_of(lay, idx)
            op = float(getattr(lay, "opacity", 1.0) or 1.0)
            if is_kv:
                op *= max(0.0, min(1.0, float(getattr(lay, "kv_on", 1.0) or 1.0)))
            op = max(0.0, min(1.0, op))
            lpulse = 1.0 if (is_kv or getattr(lay, "lock_motion", False)) else pulse
            blend = str(getattr(lay, "blend", "normal") or "normal")
            if not guard(lay, blend, op, img):
                return None

            if getattr(lay, "trans_active", False):
                prev = getattr(lay, "prev_player", None)
                pimg = prev.current() if prev is not None else None
                if pimg is not None and not pimg.isNull():
                    pimg = flatten(pimg, lay)
                    try:
                        dur = (lay.trans_ms or 350) / 1000.0
                    except Exception:                          # noqa: BLE001
                        dur = 0.35
                    dur = max(0.016, float(dur))
                    prog = min(1.0, max(0.0, (now - float(
                        getattr(lay, "trans_start", now) or now)) / dur))
                    tr = "fade"
                    try:
                        ov = (engine.cfg["mode"].get("transition") or "").strip()
                        if not ov:
                            ov = engine.cfg["auto"].get("transition") or ""
                        if ov in ("fade", "cut", "slide", "zoom", "glitch"):
                            tr = ov
                        elif engine.mode == "beat":
                            tr = "beat_cut"
                    except Exception:                      # noqa: BLE001
                        pass
                    if prog >= 1.0 or tr == "beat_cut":
                        lay.trans_active = False
                        lay.prev_player = None
                        lay._trans_prev_item = None
                        pimg = None
                    if pimg is not None:
                        pidx = (lay.prev_idx
                                if 0 <= getattr(lay, "prev_idx", -1)
                                < len(lay.clips) else -1)
                        pitem = (lay.clips[pidx] if pidx >= 0
                                 else getattr(lay, "_trans_prev_item", None))
                        pxf = xf_of(lay, pidx) if pidx >= 0 else None
                        dprev = make(lay, pimg, pitem, pxf, op,
                                     pulse_override=lpulse,
                                     slot="P:%s" % (getattr(lay, "lid", "") or id(lay)))
                        dcur = make(lay, img, item, xf, op,
                                    pulse_override=lpulse,
                                    slot="L:%s" % (getattr(lay, "lid", "") or id(lay)))
                        sgn = 1.0
                        if dcur is not None:
                            if tr == "fade":
                                dcur["opacity"] = op * prog
                            elif tr == "slide":
                                d = dcur["w"]
                                dcur["x"] += sgn * d * (1.0 - prog)
                                if dprev is not None:
                                    dprev["x"] -= sgn * d * prog
                            elif tr == "zoom":
                                zoom_center(dcur, 0.7 + 0.3 * prog)
                                if dprev is not None:
                                    zoom_center(dprev, 1.0 + 0.4 * prog)
                                    dprev["opacity"] = op * (1.0 - prog)
                            elif tr == "glitch":
                                if abs(dcur["rot"]) < 1e-3:
                                    dcur["glitch"] = True
                                    dcur["glitch_seed"] = int(now * 1000)
                        if dprev is not None:
                            dprev["blend"] = blend
                        if dcur is not None:
                            dcur["blend"] = blend
                        # 列表从上到下：当前层在上一帧之上
                        if dcur is not None:
                            out.append(dcur)
                        if dprev is not None:
                            out.append(dprev)
                        continue

            d = make(lay, img, item, xf, op, pulse_override=lpulse)
            if d is not None:
                d["blend"] = blend
                out.append(d)
        return out
    except Exception:                                          # noqa: BLE001
        return None
