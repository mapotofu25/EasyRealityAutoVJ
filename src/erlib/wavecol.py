# -*- coding: utf-8 -*-
r"""ERLIB 主表的两列"看得见"的东西：**专辑封面** + **整曲波形（带段落色块）**。

★ 2026-10-31（用户「波形图呢 / 专辑封面呢 / eravj 这些都要在 erlib 实现」）：
  ERLIB 原来只有文字列，看不到封面、也看不到波形，而这两样在 ERAVJ 的曲库里都有
  （波形列 + 段落半透明色带，见 `panels.WaveColumnDelegate`）。

## 为什么不直接 import panels 的那个 delegate
`panels.py` 是主程序的面板大杂烩（拖了一堆 engine/媒体库依赖），而 ERLIB 打包时
**刻意排除** `panels`/`engine`/`ui_main`（见 `tools/build_erlib_exe.py` 的 `_EXCLUDES`）
—— 引进来会把主程序半边都拖进包里。所以这里只**照抄绘制逻辑**
（镜像填充 + 色带 alpha 78 + 同一套配色），保持视觉一致、依赖为零。

## 数据来自哪里
`LibReader.waveform(sid)`（包里 `waveforms` 表，zlib 压缩的 `{v,cps,dur,amp}`）
+ `LibReader.songs()` 的 `sections`。**paint 里绝不解码音频**，只读内存里的
降采样包络（加载时一次算好）—— 否则滚动一次卡几十毫秒。
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

# 与 ERAVJ 曲库波形列**同一套配色**（low 蓝 / mid 黄 / high 红，alpha 78）
TIER_RGB = {"low": (74, 132, 214), "mid": (222, 190, 90), "high": (232, 96, 76)}

# ★ 2026-10-31（用户「封面太小了」）：40 → 64。行高同步到 76（见 app.COVER_ROW_H），
#   一屏仍能显示 8~10 行，但封面看得清是**哪张专辑**（40px 只够看个色块）。
COVER_PX = 64
MAX_AMP = 1400         # 波形降采样上限（再密也画不出来，白占内存）


def cover_pixmap(raw, px=COVER_PX):
    """`bytes` → 居中裁方 + 缩放的 QPixmap；失败返回 None。

    ★ 2026-11-06（用户要求"每一行高度可调"）：拆出 pixmap 版本 —— 列表里**存下这张
      基准图**（`COVER_PX`），换行高时只在内存里缩放它（不重读数据库、不解码），
      所以拖行高是瞬时的、且反复缩放不会越来越糊（每次都从同一张基准图算）。
    """
    if not raw:
        return None
    try:
        pm = QPixmap()
        if not pm.loadFromData(raw):
            return None
        if pm.isNull():
            return None
        w, h = pm.width(), pm.height()
        s = min(w, h)
        pm = pm.copy((w - s) // 2, (h - s) // 2, s, s)
        return pm.scaled(int(px), int(px), Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    except Exception:                                            # noqa: BLE001
        return None


def cover_icon(raw, px=COVER_PX):
    """`bytes` → QIcon（居中裁方 + 缩放）；失败返回 None。"""
    pm = cover_pixmap(raw, px)
    return QIcon(pm) if pm is not None else None


def pack_wave(wf, sections, dur):
    """把 `{amp:[...], cps, dur}` + 段落表压成绘制用的 `(amps, dur, bands)`。

    `amps` 已归一化到 0~1 并降采样到 `MAX_AMP` —— paint 里只做乘加，不做逐点遍历。
    """
    try:
        amp = (wf or {}).get("amp") or []
        n = len(amp)
        if n < 2:
            return None
        try:
            d = float((wf or {}).get("dur") or dur or 0.0)
        except (TypeError, ValueError):
            d = 0.0
        step = max(1, n // MAX_AMP)
        vals = [float(amp[i]) for i in range(0, n, step)]
        mx = max(vals) or 1.0
        if mx > 0:
            vals = [v / mx for v in vals]
        bands = []
        for s in (sections or []):
            try:
                bands.append((float(s.get("t0")), float(s.get("t1")),
                              str(s.get("tier") or "mid")))
            except (TypeError, ValueError, AttributeError):
                continue
        return (vals, d, bands)
    except Exception:                                            # noqa: BLE001
        return None


class WaveDelegate(QStyledItemDelegate):
    """「波形」列：镜像包络 + 段落半透明色块（与 ERAVJ 曲库同款观感）。"""

    def paint(self, p, opt, idx):                                # noqa: N802
        p.save()
        try:
            r = opt.rect
            if opt.state & QStyle.StateFlag.State_Selected:
                p.fillRect(r, QColor(58, 92, 140, 130))
            got = idx.data(Qt.ItemDataRole.UserRole)
            box = r.adjusted(3, 5, -3, -5)
            w, h = box.width(), box.height()
            if not got or w < 8 or h < 6:
                return
            amps, dur, bands = got
            n = len(amps)
            if n < 2:
                return
            mid = float(box.top()) + h / 2.0
            hh = max(1.0, h / 2.0 - 1.0)
            left = float(box.left())
            span = max(1, n - 1)
            pts = [QPointF(left + (i / span) * w, mid - amps[i] * hh) for i in range(n)]
            pts += [QPointF(left + (i / span) * w, mid + amps[i] * hh)
                    for i in range(n - 1, -1, -1)]
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(QColor(176, 214, 236, 235), 0.8))
            p.setBrush(QColor(198, 226, 242, 205))
            p.drawPolygon(QPolygonF(pts))
            p.setBrush(Qt.BrushStyle.NoBrush)
            # 段落色带**盖在**波形上（alpha 78：再高就把波峰糊掉）
            if dur > 0:
                for (t0, t1, tier) in bands:
                    x0 = left + (max(0.0, t0) / dur) * w
                    x1 = left + (min(dur, t1) / dur) * w
                    if x1 <= x0 + 0.5:
                        continue
                    c = TIER_RGB.get(tier, (150, 150, 150))
                    p.fillRect(QRectF(x0, float(box.top()), max(1.0, x1 - x0),
                                      float(h)), QColor(c[0], c[1], c[2], 78))
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            p.restore()
