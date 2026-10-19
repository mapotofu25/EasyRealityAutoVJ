# -*- coding: utf-8 -*-
"""区块面板：① 码表包 ② 曲目 ③ 素材（+ 可复用的拖拽素材列表）。

本模块只负责"长什么样"与"用户点了啥"，**不直接改数据模型** —— 一切以信号发给
主窗口（`window.py`）去落模型。这样数据流是单向的，最不容易自激。

═══ 纪律（架构方案 §4-B）═══
  B1 任何"重填列表/下拉框"前 `blockSignals(True)`，填完恢复并还原选中项 —— 防信号风暴。
  B2 数据→控件回填走 `_loading` / `_tloading` 守卫，**守卫置位必须在动控件之前**，
     且**≥2 处独立复位**（`try/finally` + 结尾再置一次）—— 防面板自激。
  B3 禁止裸 except 静默吞异常，至少 `log.exception`。
  B4 `@staticmethod` 里绝不用 `self`。
  B5 `ClipList.startDrag()` 里**自己造 QDrag**（PySide6 里视图重写的 `mimeData()`
     不保证被调用），同时保留 `mimeData()` 双保险。
  B6 列表项 `UserRole` 存 **clip_id**，路径另存 `UserRole+1`（绝不把路径当 id）。
"""

from __future__ import annotations

import logging
import os
import threading

from PySide6.QtCore import QMimeData, Qt, QThread, Signal
from PySide6.QtGui import QColor, QDrag, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
)

from . import theme
from .dialogs import red_dot_icon
from .timeline_widget import CLIP_MIME, mmss

log = logging.getLogger("ercue.ui.panels")

_HOW = "color:%s;font-size:11px;" % theme.c("fg_dim")

# BPM 输入框的基础 tooltip（`set_bpm_hint()` 会把"这个 BPM 从哪来的"追加在后面）
# ★★ 2026-10-18 事实纠正：**留 0 不会让画面对不上** ——
#   编排端导出 `at_beats = at_sec × bpm/60`，演出端选片段 `(pos_beats − at_beats) × beat_len`，
#   同一个系数互相抵消 ⇒ 比较回**秒差**；位置来自原曲播放秒数，不靠拍钟累加。
#   BPM 只影响「按小节查看/对齐」，所以措辞必须是**可选**，不能吓唬用户。
_BPM_TIP = (
    "BPM 只影响「按小节查看 / 对齐」，<b>不影响演出</b>：\n"
    "留 0 也能正常导出和演出（两端换算一致，片段位置不会错）。\n"
    "想按拍编排就填一下，或点旁边的「自动识别」让软件自己听（后台算，不卡界面）；\n"
    "也可以从演出端曲库列表的 BPM 列抄。")


# ==========================================================================
# 可拖拽的素材列表（B5/B6）
# ==========================================================================
class ClipList(QListWidget):
    """素材列表：拖出去时把 **clip_id** 放进私有 mime（时间轴靠它认素材）。

    ⚠ 规格书 §6.2：PySide6 里 QListWidget 真正参与拖拽的是它**内部模型**的 mimeData，
    视图上重写的 `mimeData()` 不保证被调用 ⇒ 时间轴 `hasFormat(CLIP_MIME)` 为假、
    整条拖放被拒。`startDrag()` 是视图自己的方法，**一定被调用**，在这里自己造 QDrag
    最可靠；同时保留 `mimeData()` 做双保险。
    """

    def startDrag(self, actions):
        it = self.currentItem()
        if it is None:
            return
        cid = str(it.data(Qt.UserRole) or "")
        if not cid:
            return
        md = QMimeData()
        md.setData(CLIP_MIME, cid.encode("utf-8"))
        md.setText(cid)
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)

    def mimeData(self, items):
        md = QMimeData()
        cid = str(items[0].data(Qt.UserRole) or "") if items else ""
        md.setData(CLIP_MIME, cid.encode("utf-8"))
        md.setText(cid)
        return md


# ==========================================================================
# 缩略图后台线程（A1/A2：缩略图绝不在 GUI 线程做）
# ==========================================================================
class ThumbWorker(QThread):
    """后台批量生成缩略图。生成好一个就发一次 `done(clip_id, png_bytes)`。

    ⚠ `media.thumbs.make_thumb` 会开容器 + 采样解码 + 压 PNG（单素材 20~120ms），
    放主线程生成几十个就"未响应"。这里全部丢本线程。
    """

    done = Signal(str, bytes)

    def __init__(self, cache_dir, parent=None):
        super().__init__(parent)
        self.cache_dir = cache_dir
        self._lock = threading.Lock()
        self._queue = []
        self._stop = False

    def request(self, clip_id, path):
        with self._lock:
            self._queue.append((str(clip_id), str(path)))

    def stop(self):
        self._stop = True
        with self._lock:
            self._queue.clear()

    def run(self):                                          # noqa: D401
        from ercue.media.thumbs import make_thumb, resolve_thumb, thumb_path
        while not self._stop:
            with self._lock:
                job = self._queue.pop(0) if self._queue else None
            if job is None:
                self.msleep(40)
                continue
            cid, path = job
            try:
                cached = resolve_thumb(path, self.cache_dir)
                if cached:
                    with open(cached, "rb") as f:
                        self.done.emit(cid, f.read())
                    continue
                png = make_thumb(path)
                if png:
                    try:
                        os.makedirs(self.cache_dir, exist_ok=True)
                        with open(thumb_path(self.cache_dir, path), "wb") as f:
                            f.write(png)
                    except OSError:
                        log.exception("写缩略图缓存失败: %s", path)
                    self.done.emit(cid, png)
            except Exception:                               # noqa: BLE001
                log.exception("生成缩略图失败: %s", path)


# ==========================================================================
# ① 码表包
# ==========================================================================
class PkgPanel(QWidget):
    """码表包列表 + 新建 / 导入 / 导出 / 删除 / 指定素材目录。

    ★ T02（③ 导出合并）：**去掉"另存为"** —— 导出即唯一的落盘入口（顶栏「导出当前码表」
      与「① 码表包 → 导出…」同一个动作）；"另存"改为：导出对话框里改个文件名即可。
    """

    newSheet = Signal()
    importSheet = Signal()
    exportSheet = Signal()
    deleteSheet = Signal()
    pickMediaDir = Signal()
    pickSheet = Signal(str)              # 列表点选某张码表 → key

    def __init__(self, parent=None):
        super().__init__(parent)
        lv = QVBoxLayout(self)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(5)
        how = QLabel(
            "顺序：①「新建 / 导入」一张码表 → ② 到「③ 素材」把视频加进这张码表 → "
            "③ 到「② 曲目」绑歌 → ④ 把素材拖到时间轴某一层上排片段 → ⑤「导出…」。\n"
            "每张码表是一个独立的 .ercue 包；切换列表里的一张 = 换一篇编排（互不影响）。")
        how.setWordWrap(True)
        how.setStyleSheet(_HOW)
        lv.addWidget(how)

        lv.addWidget(QLabel("码表包"))
        self.lst = QListWidget()
        self.lst.currentItemChanged.connect(self._on_pick)
        lv.addWidget(self.lst, 1)

        row = QHBoxLayout()
        for text, sig in (("新建…", self.newSheet), ("导入…", self.importSheet),
                          ("导出…", self.exportSheet)):
            b = QPushButton(text)
            b.clicked.connect(sig.emit)
            row.addWidget(b)
        lv.addLayout(row)
        b_del = QPushButton("删除这张码表包")
        b_del.setToolTip("从列表里移除（<b>不会删磁盘上的 .ercue 文件</b>）")
        b_del.clicked.connect(self.deleteSheet.emit)
        lv.addWidget(b_del)
        b_dir = QPushButton("指定素材目录…")
        b_dir.setToolTip("素材换过目录时：记住这个目录，重新按文件名挂载丢失素材")
        b_dir.clicked.connect(self.pickMediaDir.emit)
        lv.addWidget(b_dir)

    def _on_pick(self, *_):
        it = self.lst.currentItem()
        if it is not None:
            self.pickSheet.emit(str(it.data(Qt.UserRole) or ""))

    def refresh(self, rows, current_key):
        """`rows` = `[(key, label, unsaved)]`；**B1：blockSignals + 还原选中项**。"""
        self.lst.blockSignals(True)
        try:
            self.lst.clear()
            for key, label, _unsaved in rows:
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, key)
                self.lst.addItem(it)
            for i in range(self.lst.count()):               # findData 还原选中项
                if self.lst.item(i).data(Qt.UserRole) == current_key:
                    self.lst.setCurrentRow(i)
                    break
        finally:
            self.lst.blockSignals(False)


# ==========================================================================
# ② 曲目
# ==========================================================================
class TrackPanel(QWidget):
    """曲目列表 + 曲目信息（曲名 / 艺术家 / 时长 / BPM / 指纹 id）。"""

    addTrack = Signal()
    importAudio = Signal()
    delTrack = Signal()
    trackPicked = Signal(int)            # 选第几首（立刻重建时间轴/运行时）
    edited = Signal()                    # 任一字段改完 → 主窗口落模型
    rescanBpm = Signal()                 # ★ 「自动识别」按钮：**明确要求重算 BPM**（允许覆盖）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tloading = False
        lv = QVBoxLayout(self)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(5)
        how = QLabel(
            "一张码表按<b>曲目</b>组织（每首歌一份编排）。\n"
            "「导入音频…」用 mutagen 读标签与时长；<b>BPM 可自动识别</b>（也可手填）——\n"
            "它只影响按小节对齐，不填也能正常导出和演出。\n"
            "选中哪一首，下面编辑的就是哪一首的编排。")
        how.setTextFormat(Qt.TextFormat.RichText)   # ★ 原写 `**曲目**`（Markdown）⇒ QLabel 只认 HTML，会显示字面星号
        how.setWordWrap(True)
        how.setStyleSheet(_HOW)
        self.lbl_how = how                  # 留引用：截图/自检要能读到这段说明的实际文本
        lv.addWidget(how)

        row = QHBoxLayout()
        b_add = QPushButton("＋ 曲目")
        b_add.clicked.connect(self.addTrack.emit)
        row.addWidget(b_add)
        b_imp = QPushButton("导入音频…")
        b_imp.clicked.connect(self.importAudio.emit)
        row.addWidget(b_imp)
        b_del = QPushButton("删除曲目")
        b_del.clicked.connect(self.delTrack.emit)
        row.addWidget(b_del)
        row.addStretch(1)
        lv.addLayout(row)

        self.lst = QListWidget()
        self.lst.setMaximumHeight(140)
        self.lst.currentRowChanged.connect(self._on_pick)
        lv.addWidget(self.lst)

        box = QGroupBox("这首曲目的信息（也是「认歌」用的身份）")
        g = QGridLayout(box)
        g.setVerticalSpacing(4)
        r = 0
        self.le_title = QLineEdit()
        self.le_title.editingFinished.connect(self._on_edit)
        g.addWidget(QLabel("曲名"), r, 0)
        g.addWidget(self.le_title, r, 1)
        r += 1
        self.le_artist = QLineEdit()
        self.le_artist.editingFinished.connect(self._on_edit)
        g.addWidget(QLabel("艺术家"), r, 0)
        g.addWidget(self.le_artist, r, 1)
        r += 1
        self.sp_dur = QDoubleSpinBox()
        self.sp_dur.setRange(0, 100000)
        self.sp_dur.setDecimals(1)
        self.sp_dur.setSuffix(" 秒")
        self.sp_dur.valueChanged.connect(self._on_edit)
        g.addWidget(QLabel("时长"), r, 0)
        g.addWidget(self.sp_dur, r, 1)
        r += 1
        self.sp_bpm = QDoubleSpinBox()
        self.sp_bpm.setRange(0, 300)
        self.sp_bpm.setDecimals(2)
        # ★★ 2026-10-17：原来写「用实时推算」——**那是假的**，演出端没有"实时推算 BPM"这回事。
        # ★★ 2026-10-18 再次纠正（上一轮我把后果说重了）：留 0 走 `units` 的退化约定
        #   「1 拍 = 1 秒」，但**画面并不会因此对不上** —— 编排端 `at_beats = at_sec × bpm/60`
        #   与演出端 `(pos_beats − at_beats) × beat_len` 里的同一个系数互相抵消，比较回秒差；
        #   真正会坏的只有「算位置用的 beat_len ≠ 选片段用的 beat_len」（演出端自己的 bug）。
        #   ⇒ 文案只说"没法按小节查看/对齐"，**不能**再说"画面不会对拍"（用户为此白折腾过）。
        self.sp_bpm.setSpecialValueText("未设 BPM（可选）")
        self.sp_bpm.setToolTip(_BPM_TIP)
        self.sp_bpm.valueChanged.connect(self._on_edit)
        g.addWidget(QLabel("BPM"), r, 0)
        bpm_row = QHBoxLayout()
        bpm_row.setContentsMargins(0, 0, 0, 0)
        bpm_row.setSpacing(4)
        bpm_row.addWidget(self.sp_bpm, 1)
        # ★ 新增：「自动识别」—— 手动点 = **明确要求重算**，允许覆盖已有值（见 window._rescan_bpm）。
        self.btn_bpm = QPushButton("自动识别")
        self.btn_bpm.setToolTip("听一遍这首歌，自动算出 BPM（后台进行，不卡界面）。\n"
                                "手填过 BPM 也可以点：会重算并覆盖，日志里会记下原值 → 新值。")
        self.btn_bpm.clicked.connect(self.rescanBpm.emit)
        bpm_row.addWidget(self.btn_bpm, 0)
        g.addLayout(bpm_row, r, 1)
        r += 1
        self.le_fp = QLineEdit()
        self.le_fp.setReadOnly(True)
        g.addWidget(QLabel("指纹 id"), r, 0)
        g.addWidget(self.le_fp, r, 1)
        r += 1
        # ★ 文件 md5（认歌最高优先级：同一文件是确定判据）
        self.le_md5 = QLineEdit()
        self.le_md5.setReadOnly(True)
        g.addWidget(QLabel("文件 md5"), r, 0)
        g.addWidget(self.le_md5, r, 1)
        r += 1
        # ★ 身份扫描状态（进度 / 失败**可见** —— 绝不静默）
        self.lbl_id = QLabel("")
        self.lbl_id.setWordWrap(True)
        g.addWidget(QLabel("身份"), r, 0)
        g.addWidget(self.lbl_id, r, 1)
        lv.addWidget(box)
        lv.addStretch(1)

    def _on_pick(self, row):
        if self._tloading:
            return
        if row is not None and row >= 0:
            self.trackPicked.emit(int(row))

    def _on_edit(self, *_):
        if self._tloading:                                   # B2：回填期间不写回
            return
        self.edited.emit()

    def refresh_tracks(self, labels, current_index):
        """B1：blockSignals 重填,填完还原选中行。"""
        self.lst.blockSignals(True)
        try:
            self.lst.clear()
            for nm in labels:
                self.lst.addItem(nm)
            if self.lst.count():
                i = max(0, min(int(current_index or 0), self.lst.count() - 1))
                self.lst.setCurrentRow(i)
        finally:
            self.lst.blockSignals(False)

    def fill_identity(self, identity, fp_display="（无）", md5_display="（无）", id_status=""):
        """数据 → 控件（B2：守卫置位**在动控件之前**，且 2 处独立复位）。"""
        self._tloading = True                                # ① 置位在动控件之前
        try:
            if identity is None:
                self.le_title.setText("")
                self.le_artist.setText("")
                self.le_fp.setText(fp_display)
                self.le_md5.setText(md5_display)
                self.sp_dur.setValue(0.0)
                self.sp_bpm.setValue(0.0)
            else:
                self.le_title.setText(identity.title or "")
                self.le_artist.setText(identity.artist or "")
                self.le_fp.setText(identity.fp_id or fp_display)
                md5 = str(getattr(identity, "file_md5", "") or "")
                self.le_md5.setText((md5[:16] + "…") if len(md5) > 16 else (md5 or md5_display))
                self.le_md5.setToolTip(md5 or "（还没算；导入音频后会自动后台计算）")
                self.sp_dur.setValue(float(identity.duration_sec or 0.0))
                self.sp_bpm.setValue(float(identity.bpm or 0.0))
            self.lbl_id.setText(id_status or "")
        finally:
            self._tloading = False                           # ② 复位之一
        self._tloading = False                               # ③ 独立复位之二（防御）

    def set_bpm_hint(self, line: str = "") -> None:
        """把 BPM 的**来源说明**（以及备选候选）写进 BPM 输入框的 tooltip。

        `line` 为空 ⇒ 恢复基础文案。示例：
          · `BPM 150.00（来自 VirtualDJ）`
          · `BPM 145.00（音频分析，建议核对）\\n备选：160.00 / 80.75`
        """
        text = str(line or "").strip()
        self.sp_bpm.setToolTip(_BPM_TIP + (("\n\n" + text) if text else ""))
        self.sp_bpm.setStatusTip(text)

    def set_bpm_detecting(self, on: bool) -> None:
        """「自动识别」按钮的忙碌态：禁用 + 文案变「识别中…」；完成/失败后恢复。

        只动控件外观，**不动模型**（B2：这里不涉及回填，只改按钮本身）。
        """
        busy = bool(on)
        self.btn_bpm.setEnabled(not busy)
        self.btn_bpm.setText("识别中…" if busy else "自动识别")

    def values(self) -> dict:
        return {"title": self.le_title.text().strip(),
                "artist": self.le_artist.text().strip(),
                "duration_sec": float(self.sp_dur.value()),
                "bpm": float(self.sp_bpm.value())}


# ==========================================================================
# ③ 素材
# ==========================================================================
class MediaPanel(QWidget):
    """素材列表（缩略图 + 文件名）+ 加 / 去 / 定位 + 指定素材目录。"""

    addMedia = Signal()
    pickMediaDir = Signal()
    addToTimeline = Signal(str)          # 双击 → 在播放头给当前层加片段（clip_id）
    revealRequested = Signal(str)        # 右键「定位」（clip_id）
    removeRequested = Signal(str)        # 右键「从这张码表里去掉」（clip_id）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        self._thumbs_ready = {}          # clip_id -> QIcon
        lv = QVBoxLayout(self)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(5)
        how = QLabel(
            "这里放的是**这张码表用到的素材**（时间轴上能挑的就是它们）。\n"
            "只记路径、**不拷贝文件**；素材换过位置就用「指定素材目录…」按同名文件重挂。\n"
            "**双击 = 在播放头位置给当前层加一个片段**；也可拖到时间轴某一层。")
        how.setWordWrap(True)
        how.setStyleSheet(_HOW)
        lv.addWidget(how)

        b_add = QPushButton("＋ 素材…")
        b_add.setToolTip("把视频 / 图片加进当前码表（多选）")
        b_add.clicked.connect(self.addMedia.emit)
        lv.addWidget(b_add)

        self.lst = ClipList()
        self.lst.setIconSize(QPixmap(96, 54).size())
        self.lst.setToolTip("双击 = 在播放头位置给当前层加片段；可拖到时间轴；右键 = 定位 / 去掉")
        self.lst.itemDoubleClicked.connect(self._on_double)
        self.lst.setDragEnabled(True)
        self.lst.setDragDropMode(QAbstractItemView.DragOnly)
        self.lst.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lst.customContextMenuRequested.connect(self._on_menu)
        lv.addWidget(self.lst, 1)

        b_dir = QPushButton("指定素材目录…")
        b_dir.clicked.connect(self.pickMediaDir.emit)
        lv.addWidget(b_dir)
        self.lbl_stat = QLabel("")
        self.lbl_stat.setStyleSheet(_HOW)
        lv.addWidget(self.lbl_stat)

    # ------------------------------------------------------------------
    def _ensure_worker(self, cache_dir):
        if self._worker is None:
            self._worker = ThumbWorker(cache_dir, self)
            self._worker.done.connect(self._on_thumb)
            self._worker.start()

    def _on_thumb(self, clip_id, png):
        img = QImage()
        if not img.loadFromData(png):
            return
        icon = QIcon(QPixmap.fromImage(img))
        self._thumbs_ready[str(clip_id)] = icon
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if str(it.data(Qt.UserRole) or "") == str(clip_id):
                it.setIcon(icon)
                break

    def _on_double(self, item):
        cid = str(item.data(Qt.UserRole) or "") if item is not None else ""
        if cid:
            self.addToTimeline.emit(cid)

    def _on_menu(self, pos):
        it = self.lst.itemAt(pos)
        if it is None:
            return
        cid = str(it.data(Qt.UserRole) or "")
        m = QMenu(self)
        m.addAction("在文件管理器里定位").triggered.connect(
            lambda _c=False, x=cid: self.revealRequested.emit(x))
        m.addAction("从这张码表里去掉").triggered.connect(
            lambda _c=False, x=cid: self.removeRequested.emit(x))
        m.exec(self.lst.mapToGlobal(pos))

    # ------------------------------------------------------------------
    def fill(self, sheet, media_paths, cache_dir=None):
        """重填素材列表（B1：blockSignals）。缺失素材**标红**并加「（缺失）」。"""
        self.lst.blockSignals(True)
        try:
            self.lst.clear()
            if sheet is None:
                self.lbl_stat.setText("")
                return
            missing = 0
            for m in sheet.media:
                path = media_paths.get(m.clip_id) or ""
                ok = bool(path) and os.path.isfile(path)
                if not ok:
                    missing += 1
                text = m.name or m.clip_id
                if not ok:
                    text = "%s（丢失）" % text
                it = QListWidgetItem(text)
                it.setData(Qt.UserRole, m.clip_id)           # ★ B6：UserRole = clip_id
                it.setData(Qt.UserRole + 1, path)            # 路径另存
                if not ok:
                    # ★ 规格书 §15：前面加红色圆点 + 名称变灰 + 后缀「（丢失）」
                    it.setIcon(red_dot_icon())
                    it.setForeground(QColor(theme.c("fg_dim")))
                else:
                    icon = self._thumbs_ready.get(m.clip_id)
                    if icon is not None:
                        it.setIcon(icon)
                self.lst.addItem(it)
            self.lbl_stat.setText("共 %d 个素材，缺失 %d 个" % (len(sheet.media), missing))
        finally:
            self.lst.blockSignals(False)

        # 后台补缩略图（A1/A2：绝不在 GUI 线程做）
        if cache_dir:
            self._ensure_worker(cache_dir)
            for m in sheet.media:
                path = media_paths.get(m.clip_id) or ""
                if path and os.path.isfile(path) and m.clip_id not in self._thumbs_ready:
                    self._worker.request(m.clip_id, path)

    def stop_worker(self):
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(1500)
            self._worker = None
