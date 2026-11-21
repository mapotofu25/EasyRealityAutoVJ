# -*- coding: utf-8 -*-
"""视频素材的悬浮胶片预览（方案 C）。

只处理 **视频**。图片 / GIF 不生成、不缓存、不进后台队列；悬停时最多显示静态缩略图。

缓存结构：
  %LOCALAPPDATA%\\AutoVJ\\previews\\<hash>\\meta.json
  %LOCALAPPDATA%\\AutoVJ\\previews\\<hash>\\f000.jpg|png ...
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil

import av
from PIL import Image
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

import media_manager as mm

PREVIEW_VERSION = 1
PREVIEW_FRAMES = 10
PREVIEW_MAX_W = 640
PREVIEW_MAX_H = 360
PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(mm.THUMB_DIR)), "previews")


def _key(path):
    return hashlib.sha1(str(path or "").encode("utf-8", "replace")).hexdigest()[:20]


def _dir(path):
    return os.path.join(PREVIEW_DIR, _key(path))


def _play(item):
    try:
        fn = getattr(item, "playback_path", None)
        return str(fn() if callable(fn) else (fn or item.path))
    except Exception:                                          # noqa: BLE001
        return str(getattr(item, "path", "") or "")


def _stamp(item, src):
    try:
        st = os.stat(src)
        return {"src": src, "mtime": int(st.st_mtime), "size": int(st.st_size)}
    except Exception:                                          # noqa: BLE001
        return {"src": src, "mtime": 0, "size": 0}


def _meta(item):
    if getattr(item, "kind", "") != "video":
        return None
    try:
        meta = json.loads(open(os.path.join(_dir(item.path), "meta.json"),
                               "r", encoding="utf-8").read())
    except Exception:                                          # noqa: BLE001
        return None
    if int(meta.get("version") or 0) != PREVIEW_VERSION:
        return None
    src = _play(item)
    stamp = _stamp(item, src)
    if (meta.get("src") != src or int(meta.get("mtime") or 0) != stamp["mtime"]
            or int(meta.get("size") or 0) != stamp["size"]):
        return None
    return meta


def preview_frame_paths(item):
    meta = _meta(item)
    if not meta:
        return []
    paths = []
    for p in meta.get("frames") or []:
        fp = os.path.join(_dir(item.path), p)
        if not os.path.isfile(fp):
            return []
        paths.append(fp)
    return paths


def preview_valid(item):
    return bool(preview_frame_paths(item))


def build_preview(item, force=False):
    """生成一个视频的胶片条；图片/GIF 直接返回 None。"""
    if getattr(item, "kind", "") != "video":
        return None
    src = _play(item)
    if not src or not os.path.isfile(src):
        return None
    if not force and preview_valid(item):
        return preview_frame_paths(item)

    alpha = bool(getattr(item, "_has_alpha", False))
    ext = ".png" if alpha else ".jpg"
    stamp = _stamp(item, src)
    d = _dir(item.path)
    tmp = d + ".tmp"
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        c = av.open(src, metadata_errors="replace")
        try:
            st = c.streams.video[0]
            try:
                dur = float(st.duration * st.time_base) if st.duration and st.time_base else 0.0
            except Exception:                                  # noqa: BLE001
                dur = 0.0
            if dur <= 0:
                try:
                    dur = float(c.duration) / 1e6
                except Exception:                              # noqa: BLE001
                    dur = float(getattr(item, "duration", 0.0) or 0.0)
            try:
                fps = float(st.average_rate or 0.0) or 30.0
            except Exception:                                  # noqa: BLE001
                fps = 30.0
            total = int(st.frames or 0) or max(1, int(dur * fps))
            n = max(6, min(PREVIEW_FRAMES, total or PREVIEW_FRAMES))
            targets = {int(total * (i + 0.5) / n) for i in range(n)}
            idx = 0
            saved = []
            for fr in c.decode(st):
                if idx in targets:
                    try:
                        img = fr.to_image()
                        img.thumbnail((PREVIEW_MAX_W, PREVIEW_MAX_H), Image.LANCZOS)
                        img = img.convert("RGBA" if alpha else "RGB")
                        name = "f%03d%s" % (len(saved), ext)
                        if alpha:
                            img.save(os.path.join(tmp, name), "PNG")
                        else:
                            img.save(os.path.join(tmp, name), "JPEG", quality=86)
                        saved.append(name)
                    except Exception:                          # noqa: BLE001
                        pass
                idx += 1
                if len(saved) >= n:
                    break
        finally:
            c.close()
        if not saved:
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        meta = {
            "version": PREVIEW_VERSION,
            "src": src,
            "mtime": stamp["mtime"],
            "size": stamp["size"],
            "alpha": alpha,
            "frames": saved,
            "count": len(saved),
        }
        with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)
        return [os.path.join(d, x) for x in saved]
    except Exception:                                          # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return None


def clear_previews():
    shutil.rmtree(PREVIEW_DIR, ignore_errors=True)


class PreviewBuildWorker(QThread):
    """低优先级视频胶片生成；暂停时不碰原片。"""
    progress = Signal(int, int, str)
    built = Signal(object, list)
    done = Signal(int)

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.items = [m for m in (items or []) if getattr(m, "kind", "") == "video"]
        self._paused = False
        self._stop = False

    def set_paused(self, paused):
        self._paused = bool(paused)

    def stop(self):
        self._stop = True

    def run(self):
        try:
            total = len(self.items)
            made = 0
            for i, it in enumerate(self.items):
                if self._stop:
                    break
                while self._paused and not self._stop:
                    self.msleep(120)
                if self._stop:
                    break
                if preview_valid(it):
                    self.progress.emit(i + 1, total, getattr(it, "name", ""))
                    continue
                try:
                    paths = build_preview(it, force=False)
                    if paths:
                        made += 1
                        self.built.emit(it, paths)
                except Exception:                              # noqa: BLE001
                    pass
                self.progress.emit(i + 1, total, getattr(it, "name", ""))
                self.msleep(20)
            self.done.emit(made)
        except Exception:                                      # noqa: BLE001
            self.done.emit(0)


class PreviewHoverCard(QWidget):
    """无焦点悬浮卡：视频播胶片，图片只显示静态缩略图。"""
    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("previewHover")
        self.setStyleSheet(
            "#previewHover{background:#141a20;border:1px solid #39516a;"
            "border-radius:8px;}"
            "QLabel{color:#dfe8f2;background:transparent;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        self.pic = QLabel(self)
        self.pic.setAlignment(Qt.AlignCenter)
        self._max_w = 192
        self._max_h = 108
        self.pic.setMinimumSize(self._max_w, self._max_h)
        self.pic.setMaximumSize(self._max_w, self._max_h)
        self.pic.setStyleSheet("background:#0d1218;border-radius:4px;")
        lay.addWidget(self.pic)
        self.info = QLabel(self)
        self.info.setWordWrap(True)
        self.info.setMaximumWidth(self._max_w)
        lay.addWidget(self.info)
        self.timer = QTimer(self)
        self.timer.setInterval(130)          # ~7.7 fps
        self.timer.timeout.connect(self._tick)
        self._frames = []
        self._idx = 0
        self._item = None
        self._dpr = 1.0
        self.hide()

    def _set_pix(self, pm):
        if pm is None or pm.isNull():
            return
        dpr = float(getattr(self, "_dpr", 1.0) or 1.0)
        tw = max(1, int(round(self._max_w * dpr)))
        th = max(1, int(round(self._max_h * dpr)))
        if pm.width() > tw or pm.height() > th:
            pm = pm.scaled(tw, th, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self.pic.setPixmap(pm)
        self.pic.setMinimumSize(pm.size())
        self.pic.setMaximumSize(pm.size())

    def _static_icon(self, item):
        try:
            ic = item.icon_cached(192, 108)
            pm = ic.pixmap(192, 108) if hasattr(ic, "pixmap") else ic
            return pm
        except Exception:                                      # noqa: BLE001
            return None

    def show_for(self, item, detail, global_pos, thumb_w=0, thumb_h=0):
        if item is None:
            self.hide_card()
            return
        try:
            scr = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
            self._dpr = float(scr.devicePixelRatio() or 1.0) if scr is not None else 1.0
        except Exception:
            self._dpr = 1.0
        if thumb_w and thumb_h:
            self._max_w = max(120, int(round(float(thumb_w) * 1.2)))
            self._max_h = max(68, int(round(float(thumb_h) * 1.2)))
        else:
            self._max_w, self._max_h = 192, 108
        self.pic.setMinimumSize(self._max_w, self._max_h)
        self.pic.setMaximumSize(self._max_w, self._max_h)
        self.info.setMaximumWidth(self._max_w)
        self._item = item
        self.info.setText(detail or "")
        self.timer.stop()
        self._frames = []
        self._idx = 0
        if getattr(item, "kind", "") == "video":
            paths = preview_frame_paths(item)
            if paths:
                self._frames = paths
                self._set_pix(QPixmap(paths[0]))
                self.timer.start()
            else:
                self._set_pix(self._static_icon(item))
        else:
            # 图片 / GIF：只显示现有缩略图，绝不做视频预览
            self._set_pix(self._static_icon(item))
        self.adjustSize()
        self.move(self._best_pos(global_pos))
        self.show()
        self.raise_()

    def _best_pos(self, pos):
        try:
            scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
            g = scr.availableGeometry()
            w, h = self.width(), self.height()
            x = pos.x() + 16
            y = pos.y() + 16
            if x + w > g.right():
                x = pos.x() - w - 12
            if y + h > g.bottom():
                y = g.bottom() - h - 8
            return pos.__class__(max(g.left(), x), max(g.top(), y))
        except Exception:                                      # noqa: BLE001
            return pos

    def _tick(self):
        if not self._frames:
            self.timer.stop()
            return
        self._idx = (self._idx + 1) % len(self._frames)
        self._set_pix(QPixmap(self._frames[self._idx]))

    def hide_card(self):
        self.timer.stop()
        self._frames = []
        self.hide()
