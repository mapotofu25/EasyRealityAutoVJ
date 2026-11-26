# -*- coding: utf-8 -*-
"""扫描前的代理检测：只读元数据，生成转码清单/原因/预计时间。"""
import os
import time

from PySide6.QtCore import QThread, Signal

import av

FIT_W, FIT_H = 1920, 1080
PX_1080 = FIT_W * FIT_H


def _even(v):
    return max(2, int(round(v)) // 2 * 2)


def _target_size(w, h):
    """超宽（>=2.2:1）保持原分辨率；其它等比缩到 1080p 内。"""
    if w / float(h or 1) >= 2.2:
        return int(w), int(h)
    s = min(1.0, FIT_W / float(w or 1), FIT_H / float(h or 1))
    return _even(w * s), _even(h * s)


def _alpha(it, pf):
    has = getattr(it, "_has_alpha", None)
    if has is not None:
        return bool(has)
    try:
        if os.path.exists(it._thumb_path(".png")):
            return True
        if os.path.exists(it._thumb_path(".jpg")):
            return False
    except Exception:                                          # noqa: BLE001
        pass
    return ("yuva" in (pf or "")) or pf == "argb"


def _meta(it):
    cached = getattr(it, "_proxy_meta", None)
    if cached:
        return cached
    c = av.open(it.path, metadata_errors="replace")
    try:
        st = c.streams.video[0]
        dur = 0.0
        try:
            if st.duration and st.time_base:
                dur = float(st.duration * st.time_base)
        except Exception:                                      # noqa: BLE001
            dur = 0.0
        if dur <= 0:
            try:
                dur = float(c.duration) / 1e6
            except Exception:                                  # noqa: BLE001
                dur = 0.0
        if dur <= 0:
            dur = float(getattr(it, "duration", 0.0) or 0.0)
        fps = 0.0
        try:
            fps = float(st.average_rate or 0.0)
        except Exception:                                      # noqa: BLE001
            fps = 0.0
        m = {
            "codec": st.codec_context.name,
            "pf": st.codec_context.pix_fmt or "",
            "w": int(st.width or 0),
            "h": int(st.height or 0),
            "dur": max(0.0, dur),
            "fps": max(1.0, fps or 30.0),
        }
        it._proxy_meta = m
        return m
    finally:
        c.close()


def _classify(it):
    if getattr(it, "kind", "") != "video":
        return None, "非视频"
    # ★ 2026-11-25（用户）：勾了“排除打标/匹配”的视频也要参与代理检测；
    #   如果格式需要转码，照样列进转码列表（转完只扫描分析，不打标）。
    pp = getattr(it, "proxy_path", None)
    if pp and os.path.exists(pp):
        return None, "已有代理"
    try:
        m = _meta(it)
    except Exception as e:                                     # noqa: BLE001
        return None, "无法探测：%s" % type(e).__name__
    codec, pf = m["codec"], m["pf"]
    w, h, dur = m["w"], m["h"], m["dur"]
    if w <= 0 or h <= 0:
        return None, "分辨率未知"
    px = w * h
    alpha = _alpha(it, pf)
    target = None
    reason = ""
    category = ""

    if codec == "dxv":
        if "yuva" in pf:
            alpha = True
        if "yuv" in pf and "rgba" not in pf:
            target = "prores" if alpha else "dxv"
            category = "DXV-YUV 带 Alpha" if alpha else "DXV-YUV 格式"
            reason = ("DXV-YUV 带 alpha：只能 CPU 软解，转 ProRes 保透明"
                      if alpha else "DXV-YUV 只能 CPU 软解，转 DXV 走 GPU")
        elif px > PX_1080 * 1.02:
            target = "prores" if alpha else "dxv"
            category = "4K 带 Alpha" if alpha else "4K 降 1080p"
            reason = ("4K alpha：转 1080p ProRes 保透明"
                      if alpha else "4K：降 1080p，降低磁盘码率")
        elif w % 4 or h % 4:
            # ★ 2026-11-12：GpuDxvPlayer 要求宽高是 4 的倍数；2048×858 /
            #   1920×810 / 1920×1070 这类会被拒绝并退回 PyAV 软解。
            #   转成 pad 到 4 倍数的 DXV（尺寸基本不变），重新走 GPU。
            target = "prores" if alpha else "dxv"
            category = "尺寸不是 4 的倍数（带 Alpha）" if alpha else "尺寸不是 4 的倍数"
            reason = ("DXV 尺寸不是 4 的倍数，GPU 会退回 CPU；转 ProRes 保 alpha"
                      if alpha else
                      "DXV 尺寸不是 4 的倍数，GPU 会退回 CPU；pad 到 4 倍数走 GPU")
        else:
            return None, "1080p DXV 已走 GPU"
    elif codec == "mjpeg":
        if px > 1280 * 720 * 1.02:
            target = "dxv"
            category = "MJPEG 格式"
            reason = "MJPEG 只能 CPU 解码，转 DXV 走 GPU"
        else:
            return None, "720p MJPEG 解码便宜"
    elif codec == "qtrle":
        return None, "qtrle 解码快，保留 alpha"
    elif codec in ("h264", "hevc", "vp9", "av1"):
        return None, "H.264/HEVC 可不转（NVDEC）"
    else:
        return None, "格式 %s 不在转码规则内" % (codec or "?")

    # 超宽素材（>= 2.2:1）保持原分辨率：不做代理转码，只在扫描前提醒。
    if target and w / float(h or 1) >= 2.2:
        return None, "超宽素材：保持原分辨率，不转码"
    if target:
        tw, th = _target_size(w, h)
        if target == "dxv" and (tw % 4 or th % 4):
            tw = (int(tw) + 3) // 4 * 4
            th = (int(th) + 3) // 4 * 4
            reason += "（代理 %d×%d）" % (tw, th)
        if target == "dxv":
            speed = 0.70 if px > PX_1080 * 1.02 else 0.80
        else:
            speed = 0.31 if px > PX_1080 * 1.02 else 1.20
        eta = dur / max(0.05, speed) + 0.4
        out_mb = dur * (5.0 if target == "dxv" else 34.0)
        return {
            "item": it,
            "path": it.path,
            "codec": codec,
            "pf": pf,
            "w": w, "h": h,
            "tw": tw, "th": th,
            "dur": dur,
            "fps": m["fps"],
            "alpha": bool(alpha),
            "excluded": bool(getattr(it, "excluded", False)),
            "target": target,
            "category": category,
            "reason": reason,
            "eta": eta,
            "out_mb": out_mb,
        }, ""
    return None, ""


def build_plan(items, scope_label):
    jobs = []
    groups = {}
    skipped = {}
    ultra = 0
    ultra_items = []
    tag_secs = 0.0
    for it in items:
        try:
            m = _meta(it)
            px = max(1, m["w"] * m["h"])
            tag_secs += max(0.18, 0.34 * (px / float(PX_1080)))
        except Exception:                                      # noqa: BLE001
            tag_secs += 0.34
        job, why = _classify(it)
        if job is None:
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                if why.startswith("超宽素材"):
                    ultra += 1
                    try:
                        ultra_items.append({
                            "path": it.path,
                            "name": os.path.basename(it.path),
                            "w": int(m["w"]),
                            "h": int(m["h"]),
                            "dur": float(m["dur"]),
                            "reason": why,
                        })
                    except Exception:                          # noqa: BLE001
                        pass
            continue
        jobs.append(job)
        key = (job["target"], job["category"])
        g = groups.setdefault(key, {"n": 0, "dur": 0.0, "eta": 0.0,
                                    "out_mb": 0.0, "target": job["target"],
                                    "category": job["category"],
                                    "reason": job["reason"], "items": []})
        g["n"] += 1
        g["dur"] += job["dur"]
        g["eta"] += job["eta"]
        g["out_mb"] += job["out_mb"]
        g["items"].append({
            "name": os.path.basename(str(job["path"])),
            "path": job["path"],
            "w": int(job["w"]), "h": int(job["h"]),
            "tw": int(job["tw"]), "th": int(job["th"]),
            "dur": float(job["dur"]),
        })
    trans_secs = sum(j["eta"] for j in jobs)
    return {
        "scope": scope_label,
        "items": len(items),
        "jobs": jobs,
        "groups": list(groups.values()),
        "skipped": skipped,
        "ultrawide": ultra,
        "ultrawide_items": ultra_items,
        "tag_secs": tag_secs,
        "trans_secs": trans_secs,
        "out_mb": sum(j["out_mb"] for j in jobs),
    }


class ProxyScanWorker(QThread):
    """后台探测（只读 metadata，不解码画面）。"""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, items, scope_label):
        super().__init__()
        self.items = list(items)
        self.scope_label = scope_label

    def run(self):
        try:
            self.done.emit(build_plan(self.items, self.scope_label))
        except Exception as e:                                 # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, str(e)[:160]))


def fmt_secs(s):
    s = max(0.0, float(s or 0.0))
    if s < 60:
        return "%d 秒" % int(round(s))
    return "%.1f 分钟" % (s / 60.0)
