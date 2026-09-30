# -*- coding: utf-8 -*-
"""素材导入与管理：扫描、缩略图缓存、视频/GIF/图片播放器"""
import os
import time
import random
import threading
import _thread
import numpy as np

from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer
from PySide6.QtGui import QImage, QPixmap, QIcon
from PySide6.QtWidgets import (QListWidget, QListWidgetItem, QMenu, QStyledItemDelegate)

import cv2

# ⚠ OpenCV 默认按 **CPU 核数** 开内部线程池（本机 16 / 4 核机器 4）。
# 解码后的 resize / cvtColor / 导入缩略图 都会用它，于是它会把 CPU 铺满 ——
# 在 4 核机器上就是「VJ 软件把 CPU 吃光、跟 VDJ 抢核」的直接原因之一。
# 实测（tools/cpu_profile.py）：OpenCV 线程池独占约 1.6 个核；限制成 2 之后
# 那部分几乎消失，而单帧解码吞吐没有可测量的下降（解 DXV 的 DXT 解压主要在
# FFmpeg 自己的解码器里，不靠 OpenCV 的线程池）。
# 想改回去：环境变量 AUTO_VJ_CV_THREADS=0 表示恢复 OpenCV 默认。
try:
    _want = int(os.environ.get("AUTO_VJ_CV_THREADS", "2"))
    if _want > 0:
        cv2.setNumThreads(_want)
except Exception:
    pass

VIDEO_EXT = (".mp4", ".mov", ".webm", ".avi", ".mkv")
GIF_EXT = (".gif",)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

from config import app_base_dir
THUMB_DIR = os.path.join(app_base_dir(), "thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)


def kind_of(path: str) -> str:
    e = path.lower()
    if e.endswith(VIDEO_EXT):
        return "video"
    if e.endswith(GIF_EXT):
        return "gif"
    if e.endswith(IMAGE_EXT):
        return "image"
    return ""


def scan_folder(folder: str):
    """递归扫描文件夹，返回素材路径列表。子文件夹名作为标签。"""
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            p = os.path.join(root, f)
            if kind_of(p):
                out.append(p)
    return out


def tag_of(path: str) -> str:
    """父文件夹名作为标签（PRD：文件夹即标签）"""
    return os.path.basename(os.path.dirname(path))


def _ascii_safe_path(path):
    """OpenCV 在 Windows 读不了非 ASCII 路径：含中文等字符时复制到临时 ASCII 副本"""
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    try:
        import hashlib
        import tempfile
        import shutil
        ext = os.path.splitext(path)[1]
        h = hashlib.md5(path.encode("utf-8")).hexdigest()[:12]
        tmp = os.path.join(tempfile.gettempdir(), f"autovj_{h}{ext}")
        if not os.path.exists(tmp):
            shutil.copy2(path, tmp)
        return tmp
    except Exception:
        return path


class MediaItem:

    def __init__(self, path: str):
        self.path = path
        self.name = os.path.basename(path)
        self.kind = kind_of(path)
        self.tag = tag_of(path)
        self.duration = 0.0
        try:
            self.mtime = os.path.getmtime(path)
        except OSError:
            self.mtime = 0
        self.thumb = self._thumb_path()
        self._icons = {}
        self._pix = {}
        # 每素材独立的大小/位置（图片摆位用，默认铺满）
        self.img_scale = 1.0
        self.img_x = 0.0
        self.img_y = 0.0
        self.img_rot = 0.0
        # 智能打标：内容 tag + 多重角色（前景/背景）
        self.tags = []                       # [中文tag, ...] 自动（视觉扫描）
        self.manual_tags = []                # [中文tag, ...] 手动（永不覆盖）
        self.disabled_tags = []              # [中文tag, ...] 被用户取消的自动标签（重扫不复活）
        self.roles = {"fg": False, "bg": False}   # 多重角色：前景/背景
        self.excluded = False                # 排除自动打标/匹配（logo 等固定素材）
        self._has_alpha = None               # 视频 alpha 探测缓存（None=未探测，避免每次切换重复打开文件）
        self._codec = None                   # 视频编码名缓存（后台普查填；决定能否走 GPU 解码）
        self._is_dxv = None                  # 是否 DXV（同上）

    def all_tags(self):
        """生效标签 = (自动 + 手动) - 被取消的自动，去重（显示/筛选用）"""
        dis = set(self.disabled_tags or [])
        seen, out = set(), []
        for t in list(self.tags) + list(self.manual_tags):
            if t and t not in dis and t not in seen:
                seen.add(t); out.append(t)
        return out

    def _thumb_path(self, ext=None):
        import hashlib
        h = hashlib.md5(f"{self.path}|{int(self.mtime)}".encode("utf-8")).hexdigest()[:16]
        if ext is None:
            # 图片/GIF 用 PNG 以保留透明通道（预览区要画棋盘格）
            ext = ".png" if self.kind in ("image", "gif") else ".jpg"
        return os.path.join(THUMB_DIR, h + ext)

    def ensure_thumbnail(self):
        """取中间帧，避免黑帧；已缓存则直接返回。
        带 alpha 的视频用 PyAV 取帧存 PNG（保留透明，预览画棋盘格）；
        旧版生成的 JPG 缩略图丢 alpha，探测到 alpha 后会被自动替换。
        图片用 Qt 读取（支持中文路径），视频走 OpenCV（非 ASCII 路径先复制临时副本）。

        ⚠⚠ **主线程上只准读缓存，不准生成、也不准探测 alpha**（2026-09-26 现场卡死的根因）：
        本函数会被 `icon()` / `pixmap()` 调用，而图层行填充与素材墙都在**主线程**走这条路。
        里面的 `_av_probe_alpha` 要**顺序解最多 24 帧**，生成缩略图要开容器 + 取帧 + 写盘 ——
        164 个素材放在主线程上就是几十秒到几分钟 → 窗口"未响应"（标题栏变灰）。
        缺图 / 待探测的素材统一交给后台（`ThumbBackfillWorker` / `AlphaMigrationWorker`），
        主线程先返回现有缓存或空串，等后台完成后由 `refresh_grid` 刷新。
        """
        on_main = threading.current_thread() is threading.main_thread()
        if self.kind == "video":
            png = self._thumb_path(".png")
            if os.path.exists(png):          # PNG 优先：alpha 视频的正确缩略图
                self.thumb = png
                return self.thumb
            if os.path.exists(self.thumb):
                # JPG 已存在：非 alpha 视频命中缓存直接返回；仅首次探测一次 alpha
                # ⚠ 主线程**不做**这个探测（要解最多 24 帧，太慢）—— 先直接用 JPG，
                #   alpha 的判定交给后台的 AlphaMigrationWorker。
                if getattr(self, "_av_no_alpha", False) or on_main:
                    return self.thumb
                avmod, has = _av_probe_alpha(self.path)
                if not has:
                    self._av_no_alpha = True     # 实例缓存：普通视频别再探测/重生成
                    return self.thumb
                # 旧版 JPG 其实带 alpha：作废并生成 PNG
                old = self.thumb
                if self._make_av_thumb(avmod, png):
                    self.thumb = png
                    if os.path.exists(old):
                        try:
                            os.remove(old)
                        except OSError:
                            pass
                    return self.thumb
                return self.thumb if os.path.exists(self.thumb) else ""
        elif os.path.exists(self.thumb):
            return self.thumb
        try:
            if on_main:
                return ""          # ⚠ 主线程不生成缩略图（开容器/取帧/写盘都慢），交给后台补扫
            if self.kind == "image":
                img = QImage(self.path)
                if not img.isNull():
                    if img.width() > 320:
                        img = img.scaledToWidth(320, Qt.SmoothTransformation)
                    img.save(self.thumb, "PNG")     # PNG 保留透明通道
                return self.thumb if os.path.exists(self.thumb) else ""
            if self.kind == "gif":
                from PySide6.QtGui import QImageReader
                rd = QImageReader(self.path)
                rd.setAutoTransform(True)
                for i in range(3):          # 跳过前几帧，避免黑帧
                    img = rd.read()
                    if img.isNull():
                        break
                if not img.isNull():
                    if img.width() > 320:
                        img = img.scaledToWidth(320, Qt.SmoothTransformation)
                    img.save(self.thumb, "PNG")
                return self.thumb if os.path.exists(self.thumb) else ""
            src = _ascii_safe_path(self.path)
            if self.kind == "video":
                avmod, has = _av_probe_alpha(self.path)
                if has:
                    if self._make_av_thumb(avmod, png):
                        self.thumb = png
                        return self.thumb
                self._av_no_alpha = True
                cap = cv2.VideoCapture(src)
                self.duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / max(cap.get(cv2.CAP_PROP_FPS), 1)
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                frame = self._pick_video_frame(cap, n)
                cap.release()
                if frame is not None:
                    frame = cv2.resize(frame, (320, 180)) if frame.shape[1] > 320 else frame
                    cv2.imwrite(self.thumb, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        except Exception as e:
            print("thumb fail", self.path, e)
        return self.thumb if os.path.exists(self.thumb) else ""

    @staticmethod
    def _pick_video_frame(cap, n):
        """采样多帧，选内容最丰富的一帧（避开纯黑/纯白/过场黑帧）。
        评分 = 像素标准差（内容对比度），纯黑/纯白帧 std≈0 会被自然避开；
        全被过滤时回退选 std 最大的一帧。"""
        if n <= 0:
            return None
        if n <= 12:
            positions = list(range(n))
        else:
            step = n / 12.0
            positions = [int(step * (k + 0.5)) for k in range(12)]
        best, best_score, fallback, fb_score = None, -1.0, None, -1.0
        for idx in positions:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
            ok, f = cap.read()
            if not ok or f is None:
                continue
            mean = float(f.mean())
            std = float(f.std())
            if std > fb_score:
                fb_score, fallback = std, f
            if mean < 4 or mean > 251:          # 纯黑/纯白帧跳过
                continue
            score = std + min(mean, 255.0 - mean) * 0.4
            if score > best_score:
                best_score, best = score, f
        return best if best is not None else fallback

    def _make_av_thumb(self, avmod, png_path):
        """带 alpha 视频：采样多个时间点，选 alpha 内容最丰富的一帧存 PNG（保留透明）"""
        from fractions import Fraction
        c = avmod.open(self.path, metadata_errors="replace")
        try:
            st = c.streams.video[0]
            n = int(st.frames or 0)
            fps = max(float(st.average_rate or 30.0), 1.0)
            self.duration = n / fps
            step = max(1.0, n / 12.0)
            mids = [int(step * (k + 0.5)) for k in range(12)]
            best, best_score = None, -1.0
            for mid in mids:
                try:
                    pts = int(Fraction(mid, 1) / Fraction(st.average_rate) / Fraction(st.time_base))
                    c.seek(pts, stream=st, any_frame=True)
                except Exception:
                    continue
                for f in c.decode(video=0):
                    arr = f.to_ndarray(format="bgra")
                    alpha = arr[..., 3]
                    al = float((alpha > 16).mean())     # 非透明像素占比
                    if al > 0:
                        std = float(arr[..., :3].std())  # RGB 内容丰富度
                        score = al * 100.0 + std         # alpha 覆盖率优先 + 内容
                        if score > best_score:
                            best_score = score
                            best = np.ascontiguousarray(arr)
                    break                                # 每个采样点只取一帧
            if best is None:
                return False
            h, w = best.shape[:2]
            img = QImage(best.data, w, h, 4 * w, QImage.Format_ARGB32).copy()
            if img.width() > 320:
                img = img.scaledToWidth(320, Qt.SmoothTransformation)
            img.save(png_path, "PNG")
            return os.path.exists(png_path)
        except Exception as e:
            print("av thumb fail", self.path, e)
            return False
        finally:
            try:
                c.close()
            except Exception:
                pass

    def resolve_thumb(self):
        """返回**磁盘上确实存在**的缩略图路径（没有则 None），并顺手修正 self.thumb。

        ⚠ 不能只信 `self.thumb`：实例刚建时它是按默认扩展名猜的
        （图片/GIF → .png，视频 → .jpg），但**带 alpha 的视频缩略图是 PNG**
        （要保留透明通道）。于是 alpha 视频的真实图是 `xxx.png`，而 `self.thumb`
        指着 `xxx.jpg` → 「文件不存在」→ 格子里只有文字没有图。
        实测用户素材库：164 个里 49 个 alpha .mov 就是这种情况（用户报「部分素材的图不见了」）。
        """
        t = self.thumb
        if t and os.path.exists(t):
            return t
        for ext in (".png", ".jpg"):
            p = self._thumb_path(ext)
            if os.path.exists(p):
                self.thumb = p
                return p
        return None

    def icon(self, w=0, h=0) -> QIcon:
        """缩略图图标（带进程内缓存）：同一尺寸只读盘/缩放一次"""
        key = (int(w), int(h))
        ic = self._icons.get(key)
        if ic is not None:
            return ic
        if w and h:
            pm = self.pixmap(w, h)
            ic = QIcon(pm) if (pm is not None and not pm.isNull()) else QIcon()
        else:
            self.ensure_thumbnail()
            path = self.resolve_thumb()
            ic = QIcon(path) if path else QIcon()
        self._icons[key] = ic
        return ic

    def pixmap(self, w, h):
        """按尺寸缩放后的缩略图（带进程内缓存）；带 alpha 的素材合成到棋盘格底上"""
        key = (int(w), int(h))
        pm = self._pix.get(key)
        if pm is not None:
            return pm
        self.ensure_thumbnail()
        path = self.resolve_thumb()
        src = QPixmap(path) if path else QPixmap()
        if src.isNull():
            pm = QPixmap()
        else:
            scaled = src.scaled(int(w), int(h), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if scaled.hasAlphaChannel():
                pm = checkerboard(scaled.width(), scaled.height())
                from PySide6.QtGui import QPainter
                p = QPainter(pm)
                p.drawPixmap(0, 0, scaled)
                p.end()
            else:
                pm = scaled
        self._pix[key] = pm
        return pm

    def icon_cached(self, w=0, h=0) -> QIcon:
        """只用**已存在**的缩略图生成图标；没有就给空图标——绝不在 GUI 线程里解码。

        大批量导入时 `refresh_grid` 会给每个素材要图标，如果走 `icon()`→`ensure_thumbnail()`
        就会在主线程逐个解码（上千个 4K/DXV 素材 = 界面直接卡死）。缩略图统一由后台
        `ThumbWorker` / `ThumbBackfillWorker` 生成，生成完再 `drop_icon_cache()` +
        `refresh_grid(icons_only=True)` 补上。
        """
        key = ("cached", int(w), int(h))
        ic = self._icons.get(key)
        if ic is not None:
            return ic
        pm = QPixmap()
        try:
            path = self.resolve_thumb()      # 会同时认 .jpg 和 .png（alpha 视频是 PNG）
            if path:
                src = QPixmap(path)
                if not src.isNull():
                    pm = (src.scaled(int(w), int(h), Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation) if (w and h) else src)
                    # 透明素材（PNG / alpha 视频）在格子里也要看到棋盘格底，
                    # 与图层行的缩略图（pixmap()）保持一致 —— 否则透明区域直接露面板底色，
                    # 用户会以为「棋盘格底没了」。
                    if pm.hasAlphaChannel():
                        base = checkerboard(pm.width(), pm.height())
                        from PySide6.QtGui import QPainter
                        _p = QPainter(base)
                        _p.drawPixmap(0, 0, pm)
                        _p.end()
                        pm = base
        except Exception:
            pm = QPixmap()
        ic = QIcon(pm) if not pm.isNull() else QIcon()
        self._icons[key] = ic
        return ic

    def drop_icon_cache(self):
        """缩略图文件被清理后调用：丢弃内存缓存，下次访问重新生成"""
        self._icons.clear()
        self._pix.clear()


# ---------------- 后台导入 ----------------
class ImportWorker(QThread):
    """后台导入：扫描文件夹 →（复制/移动落盘）→ 建 MediaItem，分批回传给 GUI。

    以前整条链路都在 GUI 线程里跑（os.walk 全盘扫描 + 逐个建条目 + 主线程解码缩略图），
    导入上百 GB / 上千个素材时界面直接卡死、甚至被系统杀掉。
    现在扫描与建条目都在这个线程里，按批（200 个）emit 给界面；
    界面只做「往列表里插一批图标」这种轻活，缩略图照旧交给 ThumbWorker。
    """

    scanned = Signal(int)        # 已扫描到的素材数（增量）
    batch = Signal(list)         # 一批 MediaItem
    done = Signal(int, int)      # (新增数, 跳过数)
    failed = Signal(str)

    BATCH = 200

    def __init__(self, paths, strategy="link", lib_dir="", existing=None, copy_fn=None):
        super().__init__()
        self.paths = list(paths)
        self.strategy = strategy
        self.lib_dir = lib_dir
        self.existing = set(existing or ())   # 只读快照，避免跨线程读主线程 dict
        self.copy_fn = copy_fn
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            found = []
            if self.lib_dir:
                try:
                    os.makedirs(self.lib_dir, exist_ok=True)
                except Exception:
                    pass
            n = 0
            for p in self.paths:
                if self._stop:
                    return
                if os.path.isdir(p):
                    for root, _dirs, files in os.walk(p):
                        for f in files:
                            q = os.path.join(root, f)
                            if kind_of(q):
                                found.append(q)
                                n += 1
                                if n % self.BATCH == 0:
                                    self.scanned.emit(n)
                elif kind_of(p):
                    found.append(p)
                    n += 1
            self.scanned.emit(n)

            items, new_n, skip_n, seen = [], 0, 0, set()
            for src in found:
                if self._stop:
                    break
                dst = src
                if self.strategy in ("copy", "move") and self.copy_fn is not None:
                    dst = self.copy_fn(src, self.lib_dir, self.strategy == "move")
                    if not dst:
                        skip_n += 1
                        continue
                if dst in self.existing or dst in seen:
                    skip_n += 1
                    continue
                seen.add(dst)
                try:
                    it = MediaItem(dst)
                    if not it.kind:
                        skip_n += 1
                        continue
                    items.append(it)
                    new_n += 1
                except Exception:
                    skip_n += 1
                if len(items) >= self.BATCH:
                    self.batch.emit(items)
                    items = []
                    self.msleep(1)      # 给 GUI 线程一点喘息，避免队列堆积
            if items:
                self.batch.emit(items)
            self.done.emit(new_n, skip_n)
        except Exception as e:
            self.failed.emit(str(e))


# ---------------- 缩略图后台生成 ----------------
class ThumbWorker(QThread):
    progress = Signal(int, int)   # done, total
    done = Signal()

    def __init__(self, items):
        super().__init__()
        self.items = items

    def run(self):
        total = len(self.items)
        for i, it in enumerate(self.items):
            if self.isInterruptionRequested():
                return
            it.ensure_thumbnail()
            self.progress.emit(i + 1, total)
        self.done.emit()


class ThumbBackfillWorker(QThread):
    """给**缺缩略图**的素材后台补生成（启动时 / 清理缓存后 / 导入中断后）。

    为什么需要它：缩略图改成「后台预生成 + GUI 只用现成图标」之后（`icon_cached`），
    没有再「谁要看就现场解码」的兜底了 —— 于是凡是没有缩略图文件的素材
    （导入中途崩溃、清过缓存、手动拷进来的库）就**永远显示不出图**，
    界面上只剩一串标签文字（用户反馈「部分素材的图不见了」）。
    这里补上统一的后台补扫链路：先筛出缺图的，再逐个生成。
    """
    progress = Signal(int, int)   # done, total（total 是「缺图数」，不是素材总数）
    done = Signal(int)            # 实际补生成的数量

    def __init__(self, items):
        super().__init__()
        self.items = list(items)

    def run(self):
        todo = []
        for it in self.items:
            if self.isInterruptionRequested():
                return
            try:
                if it.resolve_thumb():      # 认 .jpg 也认 .png
                    continue
            except Exception:
                pass
            todo.append(it)
        total = len(todo)
        self.progress.emit(0, total)
        for i, it in enumerate(todo):
            if self.isInterruptionRequested():
                return
            try:
                it.ensure_thumbnail()
            except Exception:
                pass
            self.progress.emit(i + 1, total)
        self.done.emit(total)


def _start_decoder_thread(target, name=None):
    """启动解码线程，**不等它跑起来**（2026-09-26 卡顿修复）。

    为什么不用 `threading.Thread(...).start()`：
    它会执行 `self._started.wait()` —— 等新线程被调度并且**拿到 GIL** 才返回。
    而这条路径跑在**渲染线程**上（切素材那一刻 `_switch_layer_to`，以及预热 `_do_warm`），
    现场实测偶发**卡 3 秒以上** → 窗口被 Windows 判成"无响应"（CPU 却只有 13%，
    因为它是在等、不是在算）。看门狗抓到的栈：

        _switch_layer_to → _get_player → create_video_player → __init__
        → threading.py:982 in start          ← 停在这里

    而且用户实测：**关掉「GPU 解码」就不卡、一开就卡**（只有 GpuDxvPlayer 这条路径
    会在解码线程里建 GL 上下文）。

    `_thread.start_new_thread` 创建完 OS 线程立即返回（实测 0.014ms，对比 `start()` 的
    0.06~0.3ms 且不含等待），语义上等同原来的 `daemon=True`：不注册到 `threading._active`、
    不阻止解释器退出、退出时同样被强杀。解码线程本来就是 daemon，行为不变。
    """
    def _run():
        # ⚠ `_thread.start_new_thread` 起的线程**不在** `threading._active` 里，
        # 所以 `current_thread()` 会**新建一个 `_DummyThread`**，而它的 `_delete` 是空实现
        # ——**永不被回收**。后果：每建一个解码线程泄漏一个小对象，且 `threading.enumerate()`
        # 每次都要遍历这些僵尸（看门狗每次卡顿都要枚举一遍）。
        # 所以设完名字后自己把它摘掉（线程退出时在 finally 里 pop）。
        tid = None
        if name:
            try:
                tid = threading.get_ident()
                threading.current_thread().name = name
            except Exception:                                     # noqa: BLE001
                tid = None
        try:
            target()
        finally:
            if tid is not None:
                try:
                    threading._active.pop(tid, None)              # noqa: SLF001
                except Exception:                                 # noqa: BLE001
                    pass
    try:
        _thread.start_new_thread(_run, ())
    except Exception:                                                 # noqa: BLE001
        # 极端情况下（线程资源耗尽）退化成原来的写法，至少还能跑
        threading.Thread(target=_run, daemon=True).start()


# ---------------- 播放器 ----------------
# ============================================================================
# 解码器自愈 / 卡帧看门狗的统一约定（三个播放器共用）
# ----------------------------------------------------------------------------
# 用户报：「运行时素材突然卡在某一帧，直到下一个切进来才恢复」。
# 2026-10-01 复核发现**上一轮只修了 GpuDxvPlayer 一条路径**
# （tools/_fix_freeze.py 的锚点只匹配到它），VideoPlayer / AvAlphaPlayer
# 的 `_loop` 里 `_open()` 失败一次就 `return`、线程永久死亡 —— 这才是主凶。
# 详见 `_卡帧诊断.md`。
# ============================================================================
_REOPEN_BACKOFF_MAX = 2.0      # 重开退避上限（秒）
_REOPEN_BACKOFF_BASE = 0.15    # 首轮退避（原 0.25；缩短以减小可见冻帧）
_STALL_SECS = 1.5              # 活跃播放器超过这么久没有新帧 ⇒ 判定"卡帧"


class _DecoderHealth:
    """解码器健康度：产帧计数 + 卡帧判据。

    为什么要它（`_卡帧诊断.md` R5）：三个播放器**都没有任何"已死/卡帧"的可观测判据**，
    引擎只能一直显示最后一帧 —— 于是 R1~R4 的每一个小故障都会变成"永久冻帧"。
    """

    def _mark_frame(self):
        """每产出一帧调用一次（看门狗心跳）。

        ⚠ 全部用 `getattr` 兜底：本方法跑在**解码线程**上，一旦抛异常就等于把
          解码线程打死 —— 那正是我们要修的病。所以它绝不能因为"少了个字段"而炸
          （测试里有用 `__new__` 绕过 `__init__` 构造播放器的用法）。
        """
        self._frames = getattr(self, "_frames", 0) + 1
        self._last_t = time.perf_counter()

    def stalled(self, secs=_STALL_SECS):
        """活跃播放器是否卡帧。已放弃（_ended）时无条件 True。

        ⚠ 还没出过首帧时给 `secs*4` 的宽限：打开容器 + 解首帧实测要 70~690ms，
          正常慢启动不能被误判成卡死。
        """
        if getattr(self, "_ended", False):
            return True
        last = getattr(self, "_last_t", 0.0)
        if not last:
            born = getattr(self, "_born", None)
            if born is None:
                self._born = born = time.perf_counter()
            return (time.perf_counter() - born) > secs * 4
        return (time.perf_counter() - last) > secs

    def health(self):
        t = getattr(self, "_last_t", 0.0)
        return {"ended": getattr(self, "_ended", False),
                "frames": getattr(self, "_frames", 0),
                "age": (time.perf_counter() - t) if t else None,
                "reason": getattr(self, "gave_up_reason", "")}

    def _init_health(self):
        """在 __init__ 里调用。"""
        self._frames = 0                    # 产出帧总数（可观测判据）
        self._last_t = 0.0                  # 最后一次产帧时刻
        self._born = time.perf_counter()    # 对象创建时刻
        self._ended = False                 # 解码循环已彻底退出（★ 比 _dead 准确）
        self.gave_up_reason = ""


class VideoPlayer(_DecoderHealth):
    """OpenCV 视频循环播放线程，提供最新帧；非活跃时挂起省 CPU。

    **容器必须在解码线程里打开**：`cv2.VideoCapture(path)` 对 4K/DXV 素材要 60~250ms，
    放在构造函数里 = 在**渲染线程**同步等这么久（60fps 下就是 4~15 帧卡顿）。
    实测：切换素材时卡几帧，硬切/淡入都一样 —— 就是这一步造成的，与过渡方式无关。
    现在构造只起线程立即返回，`current()` 在首帧就绪前返回 None，
    引擎侧本来就有「取不到就用上一帧」的兜底，所以画面不会黑、渲染也不会被堵住。
    """

    def __init__(self, path):
        self.path = path
        self.cap = None        # 由解码线程打开
        self.fps = 30.0        # 打开容器后才拿到真实帧率
        self.speed = 1.0
        self._img = None
        self._active = False    # 默认不活跃：打开不立即解码首帧（预加载/缓存中的解码器不抢 CPU）；
                                # 只有 _tick_locked 把「当前/过渡中的 player」置 active 才真正解码。
        self._warm = 0          # >0 = 预热请求：不活跃时也解出首帧（见 warm_up）
        self._lock = False
        self._dead = False
        self._max_w = 0        # 解码缩放上限（长边超过则缩到画布 1.5 倍，省 CPU/内存）
        self._max_h = 0
        self._t = None                  # 不再持有 Thread 对象（见 _start_decoder_thread 的说明）
        self._init_health()             # 产帧计数 / 卡帧判据（见 _DecoderHealth）
        _start_decoder_thread(self._loop, name="autovj-dec")

    def set_max_size(self, w, h):
        """设置解码目标尺寸上限（画布分辨率 ×1.5 余量）：4K/5K 素材在解码线程里
        缩小后再转 QImage/拷贝，避免每帧对超大帧做 cvtColor+copy+QPainter 缩放。"""
        self._max_w = max(0, int(w))
        self._max_h = max(0, int(h))

    def set_speed(self, s: float):
        self.speed = float(max(0.1, min(3.0, s)))

    def set_active(self, a: bool):
        self._active = a

    def warm_up(self):
        """预热：提前在解码线程里「开容器 + 解出首帧」。

        实测（tools/switch_latency.py，6 个最大素材）：从构造到首帧可用要
        **70~690ms（中位 115ms ≈ 60fps 下 7 帧）**，这段时间引擎只能继续显示上一帧
        —— 用户看到的「切素材卡几帧」就是它（与硬切/淡入无关）。
        在切换前一秒左右预热，真正切到时 current() 立刻有画面，切换零延迟。
        预热只解一帧，之后回到不活跃挂起状态（不烧 CPU）。"""
        self._warm = 1

    def _open(self):
        """打开容器（只在解码线程里调用）。失败返回 None。"""
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            return None
        # 不读 CAP_PROP_FRAME_COUNT——它对某些容器(.mov/.mkv)会触发全文件扫描；
        # 本类循环播放用 POS_FRAMES 归零，用不到帧数。
        self.fps = max(float(cap.get(cv2.CAP_PROP_FPS)), 10.0)
        return cap

    def _loop(self):
        """解码主循环（**自愈**，2026-10-01 重写）。

        ⚠⚠ 老实现的致命问题（用户报「素材突然卡在某一帧」的主凶）：
          `self.cap = self._open(); if self.cap is None: return` ——
          **打开失败一次，解码线程就永久退出**；此时对象还活着、`current()` 一直返回
          最后一帧图像 ⇒ 引擎看起来"在播"，实际画面冻死，直到下一个素材切进来。
        ⇒ 现在：打开失败**退避重试**；读失败累计到阈值就**跳出重开容器**；
          只有「从来没出过帧 + 连续 `_REOPEN_MAX` 轮失败」才真放弃
          （出过帧的素材**永不放弃** —— 瞬时抖动不该让素材永久黑掉）。
        """
        tries = 0
        ever = False                       # ★ 是否产出过帧
        while not self._dead:
            try:
                self.cap = self._open()
            except Exception:                                  # noqa: BLE001
                self.cap = None
            if self.cap is None:
                tries += 1
                if (not ever) and tries >= _REOPEN_MAX:
                    self._ended = True
                    self.gave_up_reason = "连续 %d 次打开失败" % tries
                    _log_decoder_giveup(self.path, self.gave_up_reason)
                    return
                time.sleep(min(_REOPEN_BACKOFF_MAX, _REOPEN_BACKOFF_BASE * tries))
                continue
            if self._dead:                 # 打开期间就被关掉了
                self._release()
                return
            interval = 1.0 / max(self.fps, 1.0)
            fail = 0
            try:
                while not self._dead and self.cap is not None:
                    if not self._active:
                        if self._warm <= 0:
                            time.sleep(0.05)
                            continue
                        self._warm -= 1  # 预热：不活跃也只解这一帧，解完回到挂起
                    t0 = time.perf_counter()
                    ok, frame = self.cap.read()
                    if not ok or frame is None:
                        fail += 1
                        try:
                            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # 片尾回卷
                        except Exception:                      # noqa: BLE001
                            pass
                        ok, frame = self.cap.read()
                        if not ok or frame is None:
                            # ★ 连续读失败 ⇒ 容器坏了，跳出重开。
                            #   ⚠ 老实现这里是 `time.sleep(0.05); continue` **无限空转**，
                            #     画面永远停在最后一帧（正是用户看到的现象）。
                            if fail >= 20:
                                break
                            time.sleep(0.03)
                            continue
                    fail = 0
                    if frame is not None:
                        if self._max_w and self._max_h:
                            fh, fw = frame.shape[:2]
                            sc = min(self._max_w / fw, self._max_h / fh)
                            if sc < 1.0:
                                frame = cv2.resize(
                                    frame, (max(1, int(fw * sc)), max(1, int(fh * sc))),
                                    interpolation=cv2.INTER_AREA)
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        h, w, _ = rgb.shape
                        img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
                        self._img = img.copy()
                        self._mark_frame()             # ★ 看门狗心跳
                        ever = True
                    wait = float(interval / self.speed - (time.perf_counter() - t0))
                    if wait > 0:
                        time.sleep(wait)
            finally:
                self._release()
            if self._dead:
                break
            tries = 0 if ever else tries + 1   # ★ 出过帧 ⇒ 计数清零，不放弃
            if (not ever) and tries >= _REOPEN_MAX:
                self._ended = True
                self.gave_up_reason = "连续 %d 轮取不到帧" % tries
                _log_decoder_giveup(self.path, self.gave_up_reason)
                break
            time.sleep(min(_REOPEN_BACKOFF_MAX, _REOPEN_BACKOFF_BASE * tries))
        self._release()
    def _release(self):
        """解码器只在解码线程内释放，避免跨线程 release 崩溃"""
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def current(self) -> QImage:
        return self._img

    def close(self):
        """线程安全：只置退出标志，解码线程（daemon）自行退出并释放。

        ⚠ 绝不 join：实测 close() 阻塞调用线程 **10~106ms（中位 43ms ≈ 2.6 帧）**，
        而它在**渲染线程**里被调用（缓存驱逐 / 图层重建）—— 也是「切素材卡几帧」的来源。
        容器还没打开完就关闭时，解码线程打开后会立刻自行释放。"""
        self._dead = True


# 预热 PyAV：首次 `import av` 约 65ms，若等到切到 alpha 素材时才导入，这 65ms 会
# 卡在渲染线程上（切换卡顿）。放在模块导入期付掉 —— 模块是在启动建窗口前导入的。
try:
    import av as _av_warm  # noqa: F401
except Exception:
    pass


# ---------------- alpha 视频支持（PyAV） ----------------
# OpenCV 的 FFmpeg 后端不支持 rgba 输出（DXV/ProRes4444 等 alpha 编码会被压成 3 通道），
# 带 alpha 的视频改用 PyAV 解码；普通视频仍走 OpenCV。

_ALPHA_PIXFMT_MARKS = ("rgba", "bgra", "yuva", "argb", "abgr", "ya8", "pal8")
# deep 探测的采样范围：顺序解码最多 24 帧、每 4 帧查一次 alpha（命中即判定，不必解完）
_ALPHA_SCAN_FRAMES = 24
_ALPHA_SCAN_STEP = 4


def _av_probe_alpha(path, deep=True):
    """检测视频是否**真的会用到** alpha。返回 (av模块或None, has_alpha)。

    ⚠ 两层判断，缺一不可：

    1) `av.open` **必须带 `metadata_errors="replace"`**。素材库里 105/164 个 .mov 的元数据
       是 GBK，不带这个参数会抛 UnicodeDecodeError —— 探测直接失败、返回「无 alpha」，
       这些素材于是长期走 cv2（**透明通道一直是丢的**）。

    2) **只看 `pix_fmt` 不够**：DXV 的解码器一律输出 `rgba`，而 DXT1 素材的 alpha 通道
       全是 255 —— 只看像素格式会把整个 DXV 素材库都判成「带 alpha」，于是让 28 个
       完全不透明的素材白白走 PyAV + ARGB32 这条更贵的路（实测审计：30 个判为带 alpha 的
       素材里有 28 个其实不带）。所以 pix_fmt 说有 alpha 时，**再解码若干帧确认 alpha
       真的不是全 255**（deep=True）—— 只查第一帧不够，透明可能只出现在中段。

    保守原则：解不出帧就返回「无 alpha」，维持改动前的行为，绝不凭空改变画面。
    """
    try:
        import av
    except Exception:
        return None, False
    try:
        c = av.open(path, metadata_errors="replace")
        try:
            st = c.streams.video[0]
            pf = (st.pix_fmt or "").lower()
            if not any(m in pf for m in _ALPHA_PIXFMT_MARKS):
                return av, False
            if not deep:
                return av, True
            try:
                k = 0
                for fr in c.decode(video=0):
                    if k % _ALPHA_SCAN_STEP == 0:          # 查第 1、5、9 … 帧
                        arr = fr.to_ndarray(format="rgba")
                        if (arr.ndim == 3 and arr.shape[2] >= 4
                                and int(arr[:, :, 3].min()) < 255):
                            return av, True                # 发现真透明 → 立即收工
                    k += 1
                    if k >= _ALPHA_SCAN_FRAMES:
                        break
                return av, False
            except Exception:
                return av, False
        finally:
            c.close()
    except Exception:
        return av, False


class AvAlphaPlayer(_DecoderHealth):
    """带 alpha 视频的循环播放（PyAV 解码，输出 ARGB32 QImage），接口与 VideoPlayer 一致。

    与 VideoPlayer 同样：**容器在解码线程里打开**（`av.open` 对大素材也要几十到几百毫秒），
    构造立即返回，不做任何 I/O。
    """

    def __init__(self, path, avmod):
        self.path = path
        self._av = avmod
        self.c = None          # 由解码线程打开
        self.st = None
        self.fps = 30.0
        # 不读 st.frames（PyAV 统计帧数可能触发扫描，切换卡顿；循环播放用不到）
        self.speed = 1.0
        self._img = None
        self._active = False    # 默认不活跃：打开不立即解码首帧（预加载/缓存不抢 CPU）
        self._warm = 0          # >0 = 预热请求（不活跃时也解首帧，见 warm_up）
        self._dead = False
        self._max_w = 0
        self._max_h = 0
        self._t = None                  # 不再持有 Thread 对象（见 _start_decoder_thread 的说明）
        self._init_health()             # 产帧计数 / 卡帧判据（见 _DecoderHealth）
        _start_decoder_thread(self._loop, name="autovj-dec")

    def _open(self):
        """打开容器（只在解码线程里调用）。

        ⚠ `metadata_errors="replace"` 不能省：105/164 个 .mov 的元数据是 GBK，
        不带它 `av.open` 会抛 UnicodeDecodeError → 这个素材永远打不开（画面黑/停）。
        """
        c = self._av.open(self.path, metadata_errors="replace")
        st = c.streams.video[0]
        _cap_decode_threads(st)
        self.fps = max(float(st.average_rate or 30.0), 10.0)
        return c, st

    def set_max_size(self, w, h):
        self._max_w = max(0, int(w))
        self._max_h = max(0, int(h))

    def set_speed(self, s: float):
        self.speed = float(max(0.1, min(3.0, s)))

    def set_active(self, a: bool):
        self._active = a

    def warm_up(self):
        """预热：提前在解码线程里打开容器并解出首帧（切到时立即有画面）。
        实测 alpha 素材从构造到首帧 70~75ms ≈ 4~5 帧，预热后切换不再等。"""
        self._warm = 1

    def _iter_frames(self):
        while True:
            try:
                for frame in self.c.decode(video=0):
                    yield frame
            except Exception:
                pass
            # 循环：回到开头（seek 失败则重开容器）
            try:
                self.c.seek(0)
            except Exception:
                try:
                    self.c.close()
                except Exception:
                    pass
                # ⚠⚠ 这里原来**没有 try**：重开容器一旦抛异常（文件被删/盘拔了/句柄失效），
                #   异常会一路穿过生成器、穿过 `_loop` 的 `except StopIteration`，
                #   **直接打死解码线程** ⇒ 画面永久冻在最后一帧（正是用户报的现象）。
                #   现在失败就结束这个生成器，让 `_loop` 去退避重开。
                try:
                    self.c = self._av.open(self.path, metadata_errors="replace")
                    self.st = self.c.streams.video[0]
                    _cap_decode_threads(self.st)
                except Exception:                              # noqa: BLE001
                    return

    def _loop(self):
        """alpha 素材解码主循环（**自愈**，2026-10-01 重写；与 VideoPlayer 同构）。

        ⚠⚠ 老实现的三条永久死亡路径：
          ① `self.c, self.st = self._open()` 失败就 `return` ⇒ 线程永久退出；
          ② `_iter_frames` 里重开容器抛异常会**穿透**到 `_loop`（只捕获 StopIteration）；
          ③ 帧转换 `except Exception: pass` ⇒ 一直转换失败也永远不重开、不报错。
          三条的结果都是同一个：**对象活着、画面冻在最后一帧**。
        """
        tries = 0
        ever = False                       # ★ 是否产出过帧
        while not self._dead:
            try:
                self.c, self.st = self._open()
            except Exception:                                  # noqa: BLE001
                self.c = None
            if self.c is None:
                tries += 1
                if (not ever) and tries >= _REOPEN_MAX:
                    self._ended = True
                    self.gave_up_reason = "连续 %d 次打开失败" % tries
                    _log_decoder_giveup(self.path, self.gave_up_reason)
                    return
                time.sleep(min(_REOPEN_BACKOFF_MAX, _REOPEN_BACKOFF_BASE * tries))
                continue
            if self._dead:                 # 打开期间就被关掉了
                try:
                    self.c.close()
                except Exception:                              # noqa: BLE001
                    pass
                self.c = None
                return
            interval = 1.0 / max(self.fps, 1.0)
            frames = self._iter_frames()
            fail = 0
            while not self._dead:
                if not self._active:
                    if self._warm <= 0:
                        time.sleep(0.05)
                        continue
                    self._warm -= 1      # 预热：不活跃也只解这一帧
                t0 = time.perf_counter()
                try:
                    frame = next(frames)
                except StopIteration:
                    break                      # ★ 生成器收工 ⇒ 跳出重开容器
                except Exception:                                 # noqa: BLE001
                    fail += 1
                    if fail >= 20:
                        break
                    time.sleep(0.03)
                    continue
                ok = False
                try:
                    arr = frame.to_ndarray(format="bgra")   # BGRA，与 engine._np_view 一致
                    h, w = arr.shape[:2]
                    if self._max_w and self._max_h:
                        sc = min(self._max_w / w, self._max_h / h)
                        if sc < 1.0:
                            arr = cv2.resize(
                                arr, (max(1, int(w * sc)), max(1, int(h * sc))),
                                interpolation=cv2.INTER_AREA)
                            h, w = arr.shape[:2]
                    img = QImage(arr.data, w, h, 4 * w, QImage.Format_ARGB32)
                    self._img = img.copy()
                    ok = True
                except Exception:                              # noqa: BLE001
                    ok = False
                if ok:
                    fail = 0
                    self._mark_frame()             # ★ 看门狗心跳
                    ever = True
                else:
                    # ★ 转换一直失败也算"卡帧"：累计到阈值要能重开，
                    #   不能像老实现那样 `pass` 掉永远空转
                    fail += 1
                    if fail >= 20:
                        break
                wait = float(interval / self.speed - (time.perf_counter() - t0))
                if wait > 0:
                    time.sleep(wait)
            # 本轮结束：释放容器（解码器只在解码线程内释放，避免跨线程崩溃）
            try:
                self.c.close()
            except Exception:                                  # noqa: BLE001
                pass
            self.c = None
            if self._dead:
                break
            tries = 0 if ever else tries + 1   # ★ 出过帧 ⇒ 计数清零，不放弃
            if (not ever) and tries >= _REOPEN_MAX:
                self._ended = True
                self.gave_up_reason = "连续 %d 轮取不到帧" % tries
                _log_decoder_giveup(self.path, self.gave_up_reason)
                break
            time.sleep(min(_REOPEN_BACKOFF_MAX, _REOPEN_BACKOFF_BASE * tries))
    def current(self) -> QImage:
        return self._img

    def close(self):
        """只置退出标志，解码线程（daemon）自行释放；**不 join**（渲染线程里调用会卡 10~100ms）。"""
        self._dead = True


def _probe_alpha_async(item):
    """后台补探测 alpha（渲染线程不能等 av.open）。

    缩略图就是「alpha 的持久化探测结果」；只有缩略图还没生成（刚导入/补图还没跑完）才需要
    真探测。以前在这里同步 `av.open` —— 实测一个大素材要 70ms ≈ 4 帧，卡在切换那一下。
    现在先按「无 alpha」用起来（绝不卡切换），探测放后台；确为 alpha 时把标记改成 True
    并置 `_alpha_recheck`，引擎下次取该素材的解码器时会丢掉旧的 OpenCV 解码器重开（走 PyAV）。"""
    if getattr(item, "_alpha_probing", False):
        return
    item._alpha_probing = True

    def job():
        try:
            _, has = _av_probe_alpha(item.path)
            if has:
                item._has_alpha = True
                item._alpha_recheck = True
        except Exception:
            pass
        finally:
            item._alpha_probing = False

    _start_decoder_thread(job)


def _probe_codec_async(item):
    """后台探测视频编码名（决定能不能走 GPU 解码）。

    为什么需要：判断「是不是 DXV」只能 `av.open`，而它对 4K 素材要几十毫秒 ——
    绝不能在渲染线程（切素材那一下）做。这里后台填 `item._codec` / `item._is_dxv`，
    填好后下次切到这个素材就能走 GPU；没填好时先用原路径，不影响播放。

    ⚠ `av.open` 必须带 `metadata_errors="replace"`：素材库里 **105/164** 个 .mov 的元数据
    是 GBK，不带它会抛 UnicodeDecodeError（`AvAlphaPlayer._open` 正是如此，
    这些素材于是被一路当成「无 alpha」走了 cv2）。
    """
    if getattr(item, "_codec_probing", False):
        return
    item._codec_probing = True

    def job():
        try:
            import av
            c = av.open(item.path, metadata_errors="replace")
            try:
                item._codec = c.streams.video[0].codec_context.name
            finally:
                c.close()
            item._is_dxv = (item._codec == "dxv")
        except Exception:
            item._codec = ""          # 失败也落个值，别让它无限重试
            item._is_dxv = False
        finally:
            item._codec_probing = False

    _start_decoder_thread(job)


class CodecProbeWorker(QThread):
    """启动后把素材库的编码名普查一遍（后台、不卡界面）。

    GPU 解码只接管 DXV，而「是不是 DXV」必须先 `av.open` —— 普查一次填进 MediaItem，
    之后每次切素材都是零成本判断（164 个素材约 2~4 秒）。
    """
    done = Signal(int)

    def __init__(self, items):
        super().__init__()
        self.items = list(items)

    def run(self):
        n = 0
        for it in self.items:
            if self.isInterruptionRequested():
                return
            if getattr(it, "_codec", None) is not None:
                continue
            try:
                import av
                c = av.open(it.path, metadata_errors="replace")
                try:
                    it._codec = c.streams.video[0].codec_context.name
                finally:
                    c.close()
                it._is_dxv = (it._codec == "dxv")
                n += 1
            except Exception:
                it._codec = ""
                it._is_dxv = False
        self.done.emit(n)


def _png_to_jpg(png_path, jpg_path):
    """缩略图 PNG → JPG（给「其实整段不透明」的旧缩略图用）。

    直接读现有 PNG 转存，**不重新解码视频** —— 快，也不会引入新的取帧差异。
    """
    try:
        from PySide6.QtGui import QPainter
        img = QImage(png_path)
        if img.isNull():
            return False
        if img.hasAlphaChannel():
            flat = QImage(img.size(), QImage.Format_RGB32)
            flat.fill(Qt.black)
            p = QPainter(flat)
            p.drawImage(0, 0, img)
            p.end()
            img = flat
        return bool(img.save(jpg_path, "JPEG", 85))
    except Exception:
        return False


class AlphaMigrationWorker(QThread):
    """一次性校正全库的 alpha 判定与缩略图（修好 `av.open` GBK bug 的收尾）。

    背景（2026-09-25 定位，`tools/_alpha_audit.py` 审计）：
      · `av.open` 没带 `metadata_errors` → 105/164 个 .mov 的探测直接抛异常 → 一律被判
        「无 alpha」→ **真带 alpha 的素材透明通道一直是丢的**；
      · 旧判定又只看 pix_fmt，而 DXV 一律输出 rgba → **25 个整段不透明的素材被误判成
        「有 alpha」**，白白走更贵的 PyAV + ARGB32 路径。
    探测修好之后，磁盘上的旧缩略图仍带着旧结论，所以这里重判一遍，并让**缩略图扩展名
    与结论一致**（PNG ⇔ 真带 alpha）—— 这样之后每次启动还是零成本判定（看扩展名即可）。

    跑在后台线程（每个素材最多解 24 帧，全库约 1~3 分钟），绝不卡界面。
    """
    progress = Signal(int, int)      # done, total
    done = Signal(int, int)          # 改成带 alpha 的个数、改成不带 alpha 的个数

    def __init__(self, items):
        super().__init__()
        self.items = list(items)

    def run(self):
        n_alpha = n_plain = 0
        total = len(self.items)
        self.progress.emit(0, total)
        for i, it in enumerate(self.items):
            if self.isInterruptionRequested():
                return
            try:
                if it.kind == "video":
                    avmod, has = _av_probe_alpha(it.path)
                    png, jpg = it._thumb_path(".png"), it._thumb_path(".jpg")
                    has_png, has_jpg = os.path.exists(png), os.path.exists(jpg)
                    changed = False
                    if has and not has_png:
                        # 真带 alpha 却只有 JPG / 没有缩略图 → 重建 PNG 缩略图
                        if avmod is not None and it._make_av_thumb(avmod, png):
                            it.thumb = png
                            if has_jpg:
                                try:
                                    os.remove(jpg)
                                except OSError:
                                    pass
                            n_alpha += 1
                            changed = True
                    elif (not has) and has_png:
                        # 误判成 alpha：PNG 缩略图降成 JPG（不重新解码）
                        if _png_to_jpg(png, jpg):
                            try:
                                os.remove(png)
                            except OSError:
                                pass
                            it.thumb = jpg
                            n_plain += 1
                            changed = True
                        else:
                            it.thumb = png
                    it._has_alpha = bool(has)
                    it._av_no_alpha = (not has)
                    if changed:
                        try:
                            it.drop_icon_cache()
                        except Exception:
                            pass
                    if has:
                        it._alpha_recheck = True      # 让引擎丢掉按旧判定建的解码器
            except Exception:
                pass
            self.progress.emit(i + 1, total)
        self.done.emit(n_alpha, n_plain)


# ---------------------------------------------------------------- 解码线程上限
# FFmpeg 的 DXV 解码走「切片多线程」，而切片线程的开销在大画面上**大于收益**：
# 实测（2026-09-25，60 帧 × 2 轮，process_time 累加）
#   1080p DXT5：16 线程 12.05ms CPU/帧 → 4 线程 9.80ms
#   4K   DXT5：16 线程 38.67ms CPU/帧 → 4 线程 12.89ms（省 67%）
#   5280 超宽 ： 8 线程 29.17ms → 4 线程 30.21ms（差别不大，但 1 线程更差）
# 而每个解码线程有 33ms 的预算（30fps），根本不需要「2.7ms 的低延迟」。
# 环境变量 AUTO_VJ_DEC_THREADS 可覆盖（0 = 不限制，恢复旧行为）。
def _decode_thread_limit():
    try:
        v = int(os.environ.get("AUTO_VJ_DEC_THREADS", "0") or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    return min(4, os.cpu_count() or 4)


def _cap_decode_threads(stream):
    """给 PyAV 的流设解码线程上限。失败静默 —— 绝不能因此影响播放。"""
    n = _decode_thread_limit()
    if n <= 0:
        return
    try:
        stream.codec_context.thread_count = n
    except Exception:
        pass


# ---------------------------------------------------------------- GPU 解码（实验）
# DXV3 的帧内就是 DXT1/DXT5（= BC1/BC3）压缩块，而 GPU 原生支持 BC 纹理 —— 把压缩块
# 原样上传成压缩纹理，采样时由显卡硬件解压，CPU 不再参与解压（Resolume Arena 同款思路）。
# 实测 CPU ms/帧（含回读，2026-09-25 三轮回测，详见 GPU解码实现与实测.txt）：
#   DXT5-1080p    3.91  vs 软解 9.27（省 58%）
#   DXT5-超宽5280 8.07  vs 软解 30.21（省 73%）
#   DXT1-4K       9.64  vs 软解 13.54（省 29%）
#   DXT1-1080p    2.86  vs 软解 2.34（**反而略亏** —— BC1 对 FFmpeg 本来就便宜，
#                 而回读 glReadPixels 要花钱）⇒ 所以 DXT1 只在 ≥3.0 Mpx 才启用。
# ⚠ 这仍是「半程 GPU」：解压+缩放在 GPU 上完成，但整幅 RGBA 还要回读给 QPainter 合成。
_GPU_PREF = False
_GPU_DISABLED = ""        # 全局不可用原因（DLL 缺失 / GL 建不起来）；非空则不再尝试
_GPU_STATUS = None        # (可用, 说明) 缓存，供设置界面显示


def set_gpu_decode(on):
    """设置里「GPU 解码（实验）」的总闸；默认关。"""
    global _GPU_PREF
    _GPU_PREF = bool(on)


def gpu_decode_enabled():
    """环境变量 AUTO_VJ_GPU=1/0 优先（调试用），否则看设置开关。"""
    v = os.environ.get("AUTO_VJ_GPU")
    if v not in (None, ""):
        return v not in ("0", "false", "False", "no")
    return _GPU_PREF


def _gpu_dxt1_min_px():
    """DXT1 走 GPU 的最小像素数（低于此值回退软解）。

    ⚠ 这个门槛**曾经是 3.0 Mpx，是错的**（2026-09-26 重测后下调到 1.0 Mpx）。
    当初定 3.0 Mpx 的依据是「DXT1-1080p 实测多花 46%」——但那次测量有两个问题：
      ① 对照错了：拿 **cv2 软解（0.50 核）** 当基准，可 DXV 素材一旦不走 GPU，
         走的是 `GpuDxvPlayer._soft_frames`（**PyAV**），实测 **0.57~0.76 核**；
      ② 把冷启动（开容器 + 建 GL 上下文 + 编译着色器）算进了稳态。
    按**现场同一条路径**重测（预热 1.5 秒、交替 3 轮取中位数，`tools/_dxt1_recheck.py`）：
      DXT1-1080p（2.07 Mpx）：GPU **0.185~0.229** 核 vs 软解 **0.567~0.755** 核
      ⇒ **GPU 反而省 63~70%**。
    后果很实际：用户现场的层 2 / 层 3 正好都在播 DXT1-1080p，被这道门槛赶去软解，
    每个线程烧 0.95 核 → 整机 55% CPU、界面"无响应"。
    现在只保留一个很低的兜底（1.0 Mpx，即约 720p 以下才挡），因为回读成本与源分辨率无关，
    源太小的时候 GPU 的相对开销才会明显。
    """
    try:
        return max(0, int(os.environ.get("AUTO_VJ_GPU_DXT1_MIN_PX", "1000000")))
    except Exception:
        return 1000000


def gpu_status():
    """(是否可用, 说明)。给设置界面显示用。

    缓存只用于**避免反复建 GL 上下文**，但绝不能让界面撒谎（R4）：每次调用都实时核对
    `GLWorker.available()` —— 工作线程事后降级/挂死时立即改报不可用，并写明原因。
    """
    global _GPU_DISABLED, _GPU_STATUS
    if _GPU_STATUS is not None:
        if _GPU_STATUS[0]:
            try:
                import glctx
                wk = glctx.GLWorker.get()
                if not wk.available():
                    return (False, wk.last_message() or "GL 工作线程已停用，已转软解")
            except Exception:
                pass
        return _GPU_STATUS
    def _fail(msg):
        global _GPU_STATUS
        _GPU_STATUS = (False, msg)
        return _GPU_STATUS
    try:
        import dxvnative
        if not dxvnative.available():
            return _fail("dxvnative 不可用：%s" % (dxvnative.LAST_ERROR or "未知"))
    except Exception as e:                                            # noqa: BLE001
        return _fail("依赖导入失败：%s" % str(e)[:80])
    try:
        import glctx
        glctx.preinit()
        worker = glctx.GLWorker.get()
        if not worker.ensure():
            # ⚠ **不要**把瞬时状态写进全局 `_GPU_DISABLED`：那会自动堵住后面所有
            #   播放器的 GPU 路径（用户就是在设置里开关一次 GPU 之后再也回不到 GPU）。
            return _fail("GL 工作线程暂不可用：%s" % (worker.last_error or "未知"))
    except Exception as e:                                            # noqa: BLE001
        # 同上：初始化异常也只作显示，不做全局封杀（下次探测可能就好了）
        return _fail("GL 初始化异常：%s" % str(e)[:80])
    _GPU_STATUS = (True, "GPU 解码可用")
    return _GPU_STATUS


# ---- 解码生成器的"空转退避"（2026-10-01 现场：CPU 从 2 核悄悄涨到满核）----
#   ⚠ 三个取帧生成器都是 `while True: 解码一趟 → _rewind() → 再解码`。只要某趟
#     **一个帧都没产出**（decode 抛异常 / demux 全是坏包 / frame_blocks 全返回 None），
#     而 `_rewind()` 又成功（能 seek 回片头），这个 while 就**没有任何 sleep**
#     ⇒ 白占一个核 + 反复重开容器（磁盘狂读），且**永不自愈**。
#     现场实测：正在软解的线程 12 秒内从 2 个涨到 18 个、CPU 一路到 100%。
#   ⇒ 连续空转就退避，连续空转太多就判定"这个素材解不出来"、收工（释放容器）。
_SOFT_EMPTY_BACKOFF_AFTER = 3      # 连续空转几趟后开始 sleep
_SOFT_EMPTY_GIVEUP = 30            # 连续空转这么多趟 ⇒ 放弃这个素材
# 解码线程连续"打不开容器 / 拿不到帧"这么多次 ⇒ 才真的收工（期间退避重试，见 `_loop`）。
# 用户 2026-10-01 报「素材会突然卡住不动，直到下一个切进来才恢复」就是因为它原来**一次
# 失败就永久死亡**；现在会自愈重开，只有连续失败这么多次才放弃。
_REOPEN_MAX = 6


def _log_decoder_giveup(path, why):
    """解码器放弃时记一行（best-effort，绝不抛）。"""
    try:
        import stallwatch
        stallwatch.log_line("!! 解码器放弃：%s（%s）—— 已停止该路解码，避免空转烧 CPU"
                            % (os.path.basename(str(path or "")), why))
    except Exception:                                              # noqa: BLE001
        pass


class GpuDxvPlayer(_DecoderHealth):
    """DXV3 素材的 GPU 解码播放器（实验）。接口与 AvAlphaPlayer 完全一致，可直接替换。

    **内部全自动降级**：容器打开后若发现不是 DXV3 / 帧头不受支持（DXV2-LZF、YCG6）/
    分辨率不是 4 的倍数 / GL 上下文建不起来 → 就在**同一个解码线程**里切回 PyAV 软解
    （与 AvAlphaPlayer 同构）。任何失败都不影响播放 —— 演出软件绝不允许黑屏。
    诊断：`.gpu`（是否走了 GPU）、`.reason`（原因）。

    `out_alpha` 必须与「按现状这个素材会不会输出 alpha 通道」一致（即 MediaItem._has_alpha）：
    带 alpha 的给 `Format_ARGB32`，否则给 `Format_RGB32`（同样 BGRA 字节序、但
    `hasAlphaChannel()` 为 False）。这样引擎里那条「关闭保留素材 alpha 时把透明区填黑」
    的分支（`engine._draw_layer`）行为与改动前完全一致 —— 不会凭空多出每帧转换，
    也不会让本来就丢着 alpha 的素材突然变透明。
    """

    def __init__(self, path, avmod, out_alpha=True):
        self.path = path
        self._av = avmod
        self.out_alpha = bool(out_alpha)
        self._qfmt = QImage.Format_ARGB32 if self.out_alpha else QImage.Format_RGB32
        self.c = None              # 由解码线程打开
        self.st = None
        self.fps = 30.0
        self.speed = 1.0
        self._img = None
        self._active = False       # 默认不活跃（预加载/缓存中的解码器不抢 CPU）
        self._warm = 0             # >0 = 预热请求（不活跃时也解首帧）
        self._dead = False
        self._max_w = 0
        self._max_h = 0
        self._src_w = 0
        self._src_h = 0
        self._fmt = 0
        self.gpu = False           # 诊断：**当前**是否真的走 GPU
        self.gpu_want = False      # ★ 素材允许走 GPU（即使此刻工作线程没就绪）
                                   #   ⇒ 用 `_gpu_frames()` 进自愈路径，别用 `_soft_frames`
        self.reason = "未探测"      # 诊断：走 / 不走 GPU 的原因
        self._t = None                  # 不再持有 Thread 对象（见 _start_decoder_thread 的说明）
        self._init_health()             # 产帧计数 / 卡帧判据（见 _DecoderHealth）
        _start_decoder_thread(self._loop, name="autovj-dec")

    # ---------------- 与 VideoPlayer / AvAlphaPlayer 一致的接口 ----------------
    def set_max_size(self, w, h):
        self._max_w = max(0, int(w))
        self._max_h = max(0, int(h))

    def set_speed(self, s: float):
        self.speed = float(max(0.1, min(3.0, s)))

    def set_active(self, a: bool):
        self._active = a

    def warm_up(self):
        """预热：提前在解码线程里开容器 + 判路径 + 解出首帧（切到时立即有画面）。"""
        self._warm = 1

    def current(self) -> QImage:
        return self._img

    def close(self):
        """只置退出标志，解码线程（daemon）自行释放；**不 join**（渲染线程里调用会卡 10~100ms）。"""
        self._dead = True

    # ---------------- 打开容器 + 判定路径 ----------------
    def _open(self):
        c = self._av.open(self.path, metadata_errors="replace")
        st = c.streams.video[0]
        self.fps = max(float(st.average_rate or 30.0), 10.0)
        self._src_w, self._src_h = int(st.width), int(st.height)
        if not self._try_gpu(c, st):
            _cap_decode_threads(st)        # 软解路径才需要限制解码线程数
        return c, st

    def _try_gpu(self, c, st):
        """能不能走 GPU；能则设好 self._fmt 并返回 True（GL 上下文由全局工作线程持有）。"""
        global _GPU_DISABLED
        try:
            # ⚠ 不再因为全局 `_GPU_DISABLED` 就一票否决：它可能只是**上一次**的
            #   瞬时失败（GL 工作线程起得慢 / 正在冷却）。真正的硬失败（dxvnative
            #   缺失、依赖导入失败）下面各有独立判据，照样会退回软解。
            if st.codec_context.name != "dxv":
                self.reason = "非 DXV（%s）" % st.codec_context.name
                return False
            if self._src_w % 4 or self._src_h % 4:
                self.reason = "分辨率不是 4 的倍数（%dx%d）" % (self._src_w, self._src_h)
                return False
            try:
                import dxvnative
                import glctx
            except Exception as e:                                     # noqa: BLE001
                _GPU_DISABLED = "依赖导入失败：%s" % str(e)[:80]
                self.reason = _GPU_DISABLED
                return False
            if not dxvnative.available():
                _GPU_DISABLED = "dxvnative 不可用：%s" % (dxvnative.LAST_ERROR or "未知")
                self.reason = _GPU_DISABLED
                return False
            # 取第一包解析 DXV 帧头（这一包被消费掉，循环播放无影响）
            pk = None
            try:
                for p in c.demux(st):
                    if p.size:
                        pk = bytes(p)
                        break
            except Exception:
                pk = None
            hd = dxvnative.parse_header(pk) if pk else None
            if hd is None:
                self.reason = "帧头不受支持（DXV2-LZF / YCG6 等）"
                return False
            fmt = int(hd[0])
            if fmt not in (1, 5):
                self.reason = "BC 格式 %d 不受支持" % fmt
                return False
            px = self._src_w * self._src_h
            if fmt == 1 and px < _gpu_dxt1_min_px():
                self.reason = "DXT1 且尺寸偏小（%.1f Mpx，回读成本大于解压收益）" % (px / 1e6)
                return False
            glctx.preinit()
            # ★ 不再为每个播放器建 GL 上下文（那会带来运行期的 glfwCreateWindow/DestroyWindow
            #   → 撞上 NVIDIA 驱动偶发挂死）。改为向**进程唯一**的 GL 工作线程申请：
            #   它只建 1 个上下文且**永不销毁**，所有解码排队串行执行。
            worker = glctx.GLWorker.get()
            if not worker.ensure():
                err = worker.last_error or "未知"
                # ⚠ 「配额达上限」只是**这一个素材**的事（走软解就行），
                #   不能把整条 GPU 路径全局关掉 —— 否则一次瞬时超限会永久禁用 GPU 解码。
                # ⚠⚠ 这里**绝不能就这么 return False 完事**（2026-09-29 现场
                #   CPU 50%+ / 磁盘 300MB/s 的根因）：软解路径 `_soft_frames` 是
                #   **建播放器那一刻定一次、永不重探**的 —— 只要此刻 GL 工作线程
                #   还没就绪（或正在冷却期），这个播放器就**一辈子软解**，
                #   每个 DXV-1080p 烧 0.6~0.95 核 + 一路磁盘读。
                #   正确做法：标记 `gpu_want` 后照常进 `_gpu_frames()` —— 它开头就
                #   `yield from _soft_until_recover()`，**每 3 秒重探**，工作线程一
                #   恢复就自动切回 GPU（这条自愈路径本来就有，只是没人走进去）。
                #   「配额达上限」同理：那是这一个素材的事，不是全局判死刑。
                self.gpu_want = True
                self.reason = ("GL 工作线程暂不可用（%s）—— 先软解，"
                               "恢复后自动切回 GPU" % err)
                return False
            self._fmt = fmt
            self.gpu = True
            self.reason = "DXT%d %dx%d → GPU(单上下文工作线程)" % (
                5 if fmt == 5 else 1, self._src_w, self._src_h)
            return True
        except Exception as e:                                         # noqa: BLE001
            self.reason = "探测异常：%s" % str(e)[:80]
            return False

    def _dst_size(self):
        w, h = self._src_w, self._src_h
        if self._max_w and self._max_h:
            s = min(self._max_w / w, self._max_h / h)
            if s < 1.0:
                return max(1, int(w * s)), max(1, int(h * s))
        return w, h

    # ---------------- 两条取帧生成器（各自无限循环） ----------------
    def _rewind(self):
        """回到片头；seek 失败则重开容器。False = 无法继续（生成器收尾）。"""
        try:
            self.c.seek(0)
            return True
        except Exception:
            pass
        try:
            self.c.close()
        except Exception:
            pass
        try:
            self.c = self._av.open(self.path, metadata_errors="replace")
            self.st = self.c.streams.video[0]
            return True
        except Exception:
            return False

    # 降级到软解后，周期性重探 GPU 可用性的间隔（秒）：既不每帧查、也够快自动切回。
    # 2~5s 之间：太密会浪费（available() 只是状态读，但没必要每帧），太疏则长演出回切太慢。
    _GPU_REPROBE_SECS = 3.0

    def _gpu_frames(self):
        """GPU 路径：原始包 → LZ 解包 → BC 块直传 GPU 解压+缩放 → 回读 BGRA。

        解码走**进程唯一的 GL 工作线程**（`glctx.GLWorker`）：上下文不再随本播放器
        创建/销毁，所以 LRU 淘汰、图层切换都**不会再碰 `glfwDestroyWindow`**。
        工作线程失效（请求超时 / 驱动挂死 / 上下文失效）⇒ **就地切软解**保画面连续；
        软解期间**周期性重探**（每 `_GPU_REPROBE_SECS` 秒至多一次，绝非每帧）：worker 一旦
        自动恢复（R2 冷却探测成功）就**切回 GPU 路径**（`.gpu` 变回 True），全程不阻塞。
        """
        import dxvnative
        import glctx
        worker = glctx.GLWorker.get()
        empty = 0
        while True:
            if not worker.available():
                self.reason = worker.last_message() or "GL 工作线程不可用，已切软解"
                self.gpu = False
                # 软解 + 周期重探：用 `yield from` 委托迭代（同时取回生成器的返回值）——
                # 恢复则 recovered=True（回到上面的 GPU 路径），播放器关闭/素材结束则 False。
                recovered = yield from self._soft_until_recover(worker)
                if not recovered:
                    return
                self.gpu = True           # ★ 自动切回 GPU（长演出不换素材的图层也能恢复）
                self.reason = "DXT%s %dx%d → GPU(单上下文工作线程)" % (
                    "5" if self._fmt == 5 else "1", self._src_w, self._src_h)
                continue
            gone = False
            produced = 0
            try:
                for pkt in self.c.demux(self.st):
                    if not pkt.size:
                        continue
                    r = dxvnative.frame_blocks(bytes(pkt), self._src_w, self._src_h)
                    if r is None:
                        continue
                    fmt, blk = r
                    dw, dh = self._dst_size()
                    arr = worker.decode(blk, self._src_w, self._src_h, fmt, dw, dh,
                                        bgra=True)
                    if arr is None:
                        if not worker.available():
                            gone = True
                            break
                        continue
                    produced += 1
                    yield QImage(arr.data, dw, dh, 4 * dw, self._qfmt).copy()
            except Exception:                                          # noqa: BLE001
                pass
            if gone:
                continue                  # 回顶部：降级 → 软解 + 周期重探（worker 恢复即回 GPU）
            # ⚠ 空转退避：整趟 demux 一个帧都没解出来（全是坏包 / 不是真 DXV）时，
            #   立刻 `_rewind()` 会变成无 sleep 的死循环，而且每趟都白跑一次 native 解包。
            empty = 0 if produced else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if self._dead:             # 已被淘汰/关闭：立刻收工
                return
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "GPU 路径连续 %d 趟无帧" % empty)
                return
            if not self._rewind():
                return

    def _soft_until_recover(self, worker):
        """软解出帧 + **周期性重探** GPU：worker 恢复可用即 `return True`（切回 GPU），
        播放器关闭或容器无法继续则 `return False`。

        重探每 `_GPU_REPROBE_SECS` 秒至多一次，只做极轻的状态读（`worker.available()`）：
        **绝不阻塞、绝不每帧查**，所以软解出帧节奏完全不受影响。
        """
        next_probe = time.perf_counter() + self._GPU_REPROBE_SECS
        empty = 0
        while True:
            got = 0
            try:
                for frame in self.c.decode(video=0):
                    got += 1
                    arr = frame.to_ndarray(format="bgra")
                    h, w = arr.shape[:2]
                    if self._max_w and self._max_h:
                        s = min(self._max_w / w, self._max_h / h)
                        if s < 1.0:
                            arr = cv2.resize(arr, (max(1, int(w * s)), max(1, int(h * s))),
                                             interpolation=cv2.INTER_AREA)
                            h, w = arr.shape[:2]
                    yield QImage(arr.data, w, h, 4 * w, self._qfmt).copy()
                    now = time.perf_counter()
                    if now >= next_probe:
                        next_probe = now + self._GPU_REPROBE_SECS
                        if worker.available():
                            return True       # ★ 恢复：切回 GPU 路径
            except Exception:                                      # noqa: BLE001
                pass
            # ⚠ 空转退避：理由同 `_soft_frames`（解不出来的素材不能占着一个核空转）
            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if self._dead:             # 已被淘汰/关闭：立刻收工
                return False
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解(重探)连续 %d 趟无帧" % empty)
                return False
            if not self._rewind():
                return False
            if worker.available():            # 片尾也顺带探一次
                return True

    def _soft_frames(self):
        """软解路径：与 AvAlphaPlayer 完全一致的 PyAV 解码（降级用）。

        ⚠ 必须有"空转退避"：见 `_SOFT_EMPTY_BACKOFF_AFTER` 的说明 —— 否则一个解不出来
          的素材会**占满一个核空转**，而且现场就是这样把 CPU 一点点拖到 100% 的。
        """
        empty = 0
        while True:
            got = 0
            try:
                for frame in self.c.decode(video=0):
                    got += 1
                    arr = frame.to_ndarray(format="bgra")
                    h, w = arr.shape[:2]
                    if self._max_w and self._max_h:
                        s = min(self._max_w / w, self._max_h / h)
                        if s < 1.0:
                            arr = cv2.resize(arr, (max(1, int(w * s)), max(1, int(h * s))),
                                             interpolation=cv2.INTER_AREA)
                            h, w = arr.shape[:2]
                    yield QImage(arr.data, w, h, 4 * w, self._qfmt).copy()
            except Exception:                                      # noqa: BLE001
                pass
            empty = 0 if got else empty + 1
            if empty >= _SOFT_EMPTY_BACKOFF_AFTER:
                time.sleep(min(0.2, 0.02 * empty))
            if self._dead:             # 已被淘汰/关闭：立刻收工，别继续空转
                return
            if empty >= _SOFT_EMPTY_GIVEUP:
                _log_decoder_giveup(self.path, "软解连续 %d 趟无帧" % empty)
                return
            if not self._rewind():
                return

    def _loop(self):
        """解码线程主体。

        ⚠⚠ 必须能**自愈重开**（2026-10-01 用户报「素材会突然卡住不动，直到下一个切进来才恢复」）：
          原来 `_open()` 一抛异常就 `return`、取帧生成器一收工就 `break`
          ⇒ 这个播放器**永久死掉**，画面停在最后一帧（引擎拿的是 `_img` 的旧值），
          直到下一次切素材新建播放器才恢复正常 —— 与用户描述完全一致。
          现在：打开失败 / 取帧收工 ⇒ **退避后重开容器再试**（最多 `_REOPEN_MAX` 轮），
          只有连续失败那么多次才真的收工；期间一律 sleep 退避，**绝不忙等**。
        """
        tries = 0
        while not self._dead:
            # ---- ① 打开容器：失败就退避重试，而不是直接死掉 ----
            try:
                self.c, self.st = self._open()
            except Exception:                                      # noqa: BLE001
                tries += 1
                if tries >= _REOPEN_MAX:
                    self._ended = True
                    self.gave_up_reason = "连续 %d 次打开失败" % tries
                    _log_decoder_giveup(self.path, self.gave_up_reason)
                    break
                time.sleep(min(_REOPEN_BACKOFF_MAX, _REOPEN_BACKOFF_BASE * tries))
                continue
            if self._dead:                 # 打开期间就被关掉了
                break
            interval = 1.0 / max(self.fps, 1.0)
            gen_gpu = (self.gpu or self.gpu_want) and gpu_decode_enabled()
            frames = self._gpu_frames() if gen_gpu else self._soft_frames()
            produced = False
            fail = 0
            while not self._dead:
                # ★ 只有"该不该走 GPU"这一件事允许中途换生成器：设置里开关 GPU 解码
                #   立刻生效（以前只在建播放器那一刻定一次 ⇒ 改完开关得重开软件才行）。
                want = (self.gpu or self.gpu_want) and gpu_decode_enabled()
                if want != gen_gpu:
                    gen_gpu = want
                    frames = self._gpu_frames() if want else self._soft_frames()
                if not self._active:
                    if self._warm <= 0:
                        time.sleep(0.05)
                        continue
                    self._warm -= 1        # 预热：不活跃也只解这一帧
                t0 = time.perf_counter()
                try:
                    img = next(frames)
                except StopIteration:
                    break                  # 生成器收工 ⇒ 走下面"重开再试"
                except Exception:                                      # noqa: BLE001
                    # ⚠ 生成器一直抛异常时必须能累积到"重开"，
                    #   否则这里就是 50Hz 无限空转、画面永远冻着、也永远不重开
                    fail += 1
                    if fail >= 20:
                        break
                    time.sleep(0.02)
                    continue
                if img is not None:
                    self._img = img
                    produced = True
                    fail = 0
                    self._mark_frame()             # ★ 看门狗心跳
                wait = float(interval / self.speed - (time.perf_counter() - t0))
                if wait > 0:
                    time.sleep(wait)
            if self._dead:
                break
            # ---- ② 取帧收工：出过帧说明素材是好的，重置计数；否则累加 ----
            if produced:
                tries = 0
            else:
                tries += 1
            if tries >= _REOPEN_MAX:
                self._ended = True
                self.gave_up_reason = "连续 %d 轮取帧收工" % tries
                _log_decoder_giveup(self.path, self.gave_up_reason)
                break
            self._release()
            time.sleep(min(_REOPEN_BACKOFF_MAX, _REOPEN_BACKOFF_BASE * tries))
        self._release()

    def _release(self):
        """容器只在解码线程内释放（避免跨线程崩溃）。

        GL 上下文是**进程唯一**的（由 `glctx.GLWorker` 持有并永不销毁），
        所以这里**不再销毁任何 GL 上下文** —— 也就不会再触发挂钩死的 `glfwDestroyWindow`。
        """
        if self.c is not None:
            try:
                self.c.close()
            except Exception:
                pass
            self.c = None


def create_video_player(item):
    """视频播放器工厂：带 alpha 的视频用 PyAV 解码（保留透明），其余走 OpenCV。

    **这里绝不能打开容器做探测**：`_av_probe_alpha` 会 `av.open(path)`，对 4K/DXV 要几十
    到两百毫秒，而本函数是在渲染线程里调的（切素材那一下）→ 直接卡几帧。
    判断 alpha 的免费依据：**缩略图是 PNG 就说明该素材带 alpha** —— 缩略图只在探测到 alpha
    时才存 PNG（要保透明通道），而缩略图在导入时就生成好了，等于把探测结果持久化在了磁盘上。
    缩略图缺失时**也不等**（后台补探测，见 _probe_alpha_async）。
    """
    has = getattr(item, "_has_alpha", None)
    if has is None:
        try:
            if os.path.exists(item._thumb_path(".png")):
                has = True
            elif os.path.exists(item._thumb_path(".jpg")):
                has = False
            else:
                has = False
                _probe_alpha_async(item)             # 后台探测，绝不在渲染线程等
        except Exception:
            has = False
        item._has_alpha = bool(has)
    try:
        import av as avmod                              # 模块导入期已预热，这里几乎零成本
    except Exception:
        avmod = None
    # GPU 解码（实验）：**只接管 DXV 素材**（普查 144/164，见 tools/_dxv_inventory.py）。
    # 「是不是 DXV」由后台编码普查填进 item._is_dxv —— 没填好时不接管（走原路径），
    # 后台补完下一次切到它就能用，绝不在这里 av.open。
    # `out_alpha` 传「按现状这个素材会不会输出 alpha 通道」，保证画面与改动前一致。
    if gpu_decode_enabled() and avmod is not None:
        dxv = getattr(item, "_is_dxv", None)
        if dxv is None:
            _probe_codec_async(item)
        elif dxv:
            return GpuDxvPlayer(item.path, avmod, out_alpha=bool(has))
    if has and avmod is not None:
        return AvAlphaPlayer(item.path, avmod)          # 构造不再阻塞（容器在解码线程开）
    return VideoPlayer(item.path)


class GifPlayer:
    """GIF 循环播放（QImageReader 逐帧）"""

    def __init__(self, path):
        from PySide6.QtGui import QImageReader
        self.path = path
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        self.frames = []
        self.delays = []
        while reader.canRead():
            img = reader.read()
            if img.isNull():
                break
            self.frames.append(img)
            d = reader.nextImageDelay()
            self.delays.append(max(d, 30) / 1000.0)
            if len(self.frames) > 300:
                break
        self.total = sum(self.delays) if self.delays else 1.0
        self.speed = 1.0
        self._t0 = time.perf_counter()

    def set_speed(self, s: float):
        self.speed = max(0.1, min(3.0, s))

    def set_active(self, a: bool):
        pass  # GIF 按需取帧，无需挂起

    def warm_up(self):
        pass  # GIF/图片在构造时就已就绪，无需预热

    def current(self) -> QImage:
        if not self.frames:
            return QImage()
        el = (time.perf_counter() - self._t0) * self.speed
        pos = el % self.total
        acc = 0.0
        for img, d in zip(self.frames, self.delays):
            acc += d
            if pos < acc:
                return img
        return self.frames[0]

    def close(self):
        pass


class ImagePlayer:
    def __init__(self, path):
        self.img = QImage(path)

    def set_speed(self, s):
        pass

    def set_active(self, a):
        pass

    def warm_up(self):
        pass  # 图片构造时已解码

    def current(self) -> QImage:
        return self.img

    def close(self):
        pass


def make_player(item: MediaItem):
    if item.kind == "video":
        return create_video_player(item)
    if item.kind == "gif":
        return GifPlayer(item.path)
    return ImagePlayer(item.path)


# ---------------- 素材网格控件 ----------------
class MediaGrid(QListWidget):
    """素材网格：缩略图、标签、播放高亮；不显示文件名和时长"""
    add_to_layer = Signal(object, int)   # item, layer_index
    remove_from_layer = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setIconSize(QSize(160, 90))
        self.setGridSize(QSize(178, 138))
        self.setSpacing(8)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setWordWrap(False)

    def _menu(self, pos):
        menu = QMenu(self)
        menu.addSeparator()
        for i, act in enumerate(self.window()._layer_actions_for_menu()):
            menu.addAction(act)
        menu.addSeparator()
        rm = menu.addAction("从图层移除")
        act = menu.exec(self.mapToGlobal(pos))
        if act is None:
            return
        sel = [it.data(Qt.UserRole) for it in self.selectedItems()]
        if act == rm:
            for m in sel:
                self.remove_from_layer.emit(m)


# ---------------- 预览棋盘格（透明底） ----------------
_CHECKER_CACHE = {}


def checkerboard(w, h, cell=8):
    """生成棋盘格底图（表示透明区域，类似 Resolume Arena 的透明提示）"""
    key = (int(w), int(h), int(cell))
    pm = _CHECKER_CACHE.get(key)
    if pm is not None:
        return pm.copy()
    from PySide6.QtGui import QPainter, QColor
    pm = QPixmap(int(w), int(h))
    p = QPainter(pm)
    dark, light = QColor(58, 58, 58), QColor(86, 86, 86)
    for y in range(0, int(h), cell):
        for x in range(0, int(w), cell):
            p.fillRect(x, y, cell, cell,
                       light if ((x // cell + y // cell) % 2 == 0) else dark)
    p.end()
    _CHECKER_CACHE[key] = pm
    return pm.copy()


def invalidate_checker_cache():
    _CHECKER_CACHE.clear()


# ---------------- 缩略图缓存维护 ----------------
def thumb_cache_size():
    """缩略图缓存大小（MB）"""
    total = 0
    try:
        for f in os.listdir(THUMB_DIR):
            try:
                total += os.path.getsize(os.path.join(THUMB_DIR, f))
            except OSError:
                pass
    except OSError:
        pass
    return total / 1024.0 / 1024.0


def clear_thumb_cache(library=None):
    """清空缩略图文件缓存；library 传入时同步丢弃内存图标缓存"""
    n = 0
    try:
        for f in os.listdir(THUMB_DIR):
            if not f.lower().endswith((".jpg", ".png")):
                continue
            try:
                os.remove(os.path.join(THUMB_DIR, f))
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    if library:
        for m in library.values():
            try:
                m.drop_icon_cache()
            except Exception:
                pass
    return n
