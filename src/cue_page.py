# -*- coding: utf-8 -*-
"""码表**管理页**（演出端第 4 个页签）+ 只读详情窗 + 「选一张表」选择器。

════════════════════════════════════════════════════════════════════════
为什么单独一个模块
════════════════════════════════════════════════════════════════════════
用户原话（2026-10）：
  「在演出台、素材库、曲库、曲风旁边有一个单独的码表管理选项。可以查看文件、删除、
    添加等等这些管理码表。然后在图层的新建里面有一个码表图层，可以选择码表。」

⇒ 第 4 个页签 = **列表式管理**（添加 / 删除 / 定位 / 刷新 + 双击详情 + 右键绑图层）。

三条硬约束（别改回去）
--------------------
1. **编排不在这里**：本页只做「文件级管理 + 绑到图层」，**不做时间轴编排**
   （编排在 EasyRealityCue）。历史上把编排形态搬进主程序出现过"码表把主程序拖垮"。
2. **删除 = 从列表移除，不删磁盘文件**（用户要求「删除这些管理码表」= 移出清单）。
   真要删文件请用「定位文件」自己去资源管理器删 —— 这里绝不替用户删包。
3. **导入/绑定的唯一实现**在 `cue_ui.load_and_bind()`：本页与图层头下拉都调它，
   不在本模块再写一套（两套迟早分叉）。

数据来源
--------
列表**不自己扫盘**，一律读 `engine.cue_index()`（`cfg["cue"]["sheets"]`，只存摘要）。
「片段总数」摘要里没有（红线：不改 `cue_index.py` / `cuesheet.py`）⇒ 用公开 API
`cuesheet.load()` 读一次后按 `(mtime, size)` 缓存在本模块，**只在包变了才重读**。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QMenu, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import cuesheet
from i18n import T, Tf
from panels import I18nDialog

#: abspath → ((mtime, size), 片段总数)；包变了（mtime/size 不同）才重读
_FRAG_CACHE: dict[str, tuple] = {}

CUE_PAGE_INDEX = 3          # 第 4 个页签（0=演出台 1=素材库 2=曲库·曲风 3=码表）


# ==========================================================================
# 小工具
# ==========================================================================
def fmt_size(n):
    """文件大小 → 自动单位（B / KB / MB / GB）。非法值返回 "—"。"""
    try:
        v = float(n)
    except Exception:                                            # noqa: BLE001
        return "—"
    if v < 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % int(v)
            return "%.1f %s" % (v, unit)
        v /= 1024.0
    return "—"


def fmt_mtime(ts):
    """mtime → "YYYY-MM-DD HH:MM"；非法值返回 "—"。"""
    try:
        t = float(ts)
    except Exception:                                            # noqa: BLE001
        return "—"
    if t <= 0:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    except Exception:                                            # noqa: BLE001
        return "—"


def _stat_key(path):
    """`(mtime, size)`；取不到返回 None（文件不存在/无权限）。"""
    try:
        st = os.stat(str(path))
        return (float(st.st_mtime), int(st.st_size))
    except OSError:
        return None


def sheet_frag_count(path):
    """这张码表一共有多少个片段（全曲所有层的 items 合计）。读不了返回 0。

    ⚠ 只读**一次**并缓存：键是 `(mtime, size)`，包没变就直接返回缓存值。
      码表可能有上千片段，全量 `cuesheet.load()` 几十~几百 ms ⇒ 绝不每次刷新都读。
    """
    p = os.path.abspath(str(path or ""))
    key = _stat_key(p)
    if key is None:
        return 0
    hit = _FRAG_CACHE.get(p)
    if hit is not None and hit[0] == key:
        return int(hit[1])
    n = 0
    try:
        sheet, _info = cuesheet.load(p)
        for t in (getattr(sheet, "tracks", None) or []):
            for ly in (getattr(t, "layers", None) or []):
                n += len(getattr(ly, "items", None) or [])
    except Exception:                                            # noqa: BLE001
        n = 0
    _FRAG_CACHE[p] = (key, n)
    return n


def bound_layer_names(owner, entry):
    """哪些图层绑着这张码表（返回图层名列表）。"""
    out = []
    engine = getattr(owner, "engine", None)
    lays = list(getattr(engine, "layers", None) or []) if engine is not None else []
    p = os.path.abspath(str((entry or {}).get("path") or ""))
    base = os.path.basename(p)
    title = str((entry or {}).get("title") or "")
    for ly in lays:
        if not getattr(ly, "is_cuesheet", False):
            continue
        cid = str(getattr(ly, "cuesheet_id", "") or "")
        if cid and (cid == base or cid == title or cid == p):
            out.append(str(getattr(ly, "name", "?") or "?"))
    return out


def bound_layer_names_all(owner):
    """所有码表图层的名字（状态行用）。"""
    out = []
    engine = getattr(owner, "engine", None)
    for ly in list(getattr(engine, "layers", None) or []):
        if getattr(ly, "is_cuesheet", False):
            out.append(str(getattr(ly, "name", "?") or "?"))
    return out


def sheet_entries(owner):
    """取索引里的全部码表摘要（**不扫盘**）。引擎/索引不可用时返回 []。"""
    engine = getattr(owner, "engine", None)
    idx = None
    try:
        fn = getattr(engine, "cue_index", None)
        if callable(fn):
            idx = fn()
    except Exception:                                            # noqa: BLE001
        idx = None
    entries = list(getattr(idx, "entries", None) or [])
    # 只保留带路径的（索引本身就有过滤，这里再兜一层防脏配置）
    return [e for e in entries if isinstance(e, dict) and e.get("path")]


def sheet_label(entry):
    """列表/下拉里显示的码表名：表名 → 文件名 → 路径。"""
    e = entry or {}
    t = str(e.get("title") or "").strip()
    if t:
        return t
    b = os.path.basename(str(e.get("path") or "")).strip()
    return b or str(e.get("path") or "?")


# ==========================================================================
# 「选一张表」——新建码表图层 / 右键绑到图层 共用
# ==========================================================================
def pick_sheet(owner, parent=None, prompt=""):
    """让用户从索引里选一张码表；取消 / 列表为空 ⇒ None。

    ⚠ 列表为空时必须给出**下一步怎么走**（用户提示「码表为空」最容易卡住）：
      指向第 4 个页签的「添加…」按钮，而不是只说"没有码表"。
    """
    entries = sheet_entries(owner)
    if not entries:
        QMessageBox.information(
            parent or (owner if isinstance(owner, QWidget) else None),
            T("码表"),
            T("还没有码表。\n\n请先到「码表」页签（第 4 个）点「添加…」导入 .ercue 码表包，"
              "再回来选择。"))
        return None
    dlg = QDialog(parent or (owner if isinstance(owner, QWidget) else None))
    dlg.setWindowTitle(T("选择码表"))
    dlg.setMinimumWidth(420)
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel(prompt or T("选择这张图层要应用的码表：")))
    cmb = QComboBox(dlg)
    for e in entries:
        cmb.addItem("%s ｜ %s ｜ %s"
                    % (sheet_label(e),
                       Tf("{} 首", int(e.get("n_tracks", 0) or 0)),
                       os.path.basename(str(e.get("path") or ""))), e)
    lay.addWidget(cmb)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
    bb.button(QDialogButtonBox.Ok).setText(T("确定"))
    bb.button(QDialogButtonBox.Cancel).setText(T("取消"))
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if dlg.exec() != QDialog.Accepted:
        return None
    return cmb.currentData()


# ==========================================================================
# 只读详情窗
# ==========================================================================
class CueSheetDetailDialog(I18nDialog):
    """码表详情（**只读**）：表名 / 画布 / 曲目清单（含 BPM）/ 每层片段数 / 内嵌素材。

    ⚠ **不做编排编辑**（red line）：这里只"看"，改编排请用 EasyRealityCue 打开再导出。
    """

    def __init__(self, owner, entry, parent=None):
        super().__init__(parent or (owner if isinstance(owner, QWidget) else None))
        self.owner = owner
        self.entry = dict(entry or {})
        self.path = os.path.abspath(str(self.entry.get("path") or ""))
        self.sheet = None
        self.err = ""
        self.setMinimumWidth(720)
        self.setMinimumHeight(560)
        self._build()
        self._load()

    # ---------------- 构建 ----------------
    def _build(self):
        self.setWindowTitle(Tf("码表详情 — {}", sheet_label(self.entry)))
        self.setProperty("_i18nDynamic", True)     # 标题含表名（用户数据）
        root = QVBoxLayout(self)

        self.lbl_head = QLabel("")
        self.lbl_head.setWordWrap(True)
        self.lbl_head.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.lbl_head)

        self.lbl_err = QLabel("")
        self.lbl_err.setWordWrap(True)
        self.lbl_err.setStyleSheet("color:#c62828;")
        root.addWidget(self.lbl_err)

        root.addWidget(QLabel(T("曲目（含 BPM 与每层片段数）")))
        self.tbl_tracks = QTableWidget(0, 6, self)
        self.tbl_tracks.setHorizontalHeaderLabels(
            [T("曲目"), T("艺术家"), T("BPM"), T("时长"), T("层 / 片段"), T("每层片段数")])
        self.tbl_tracks.verticalHeader().setVisible(False)
        self.tbl_tracks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_tracks.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_tracks.setAlternatingRowColors(True)
        hh = self.tbl_tracks.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        # ⚠ 关掉"拉伸最后一列"：它和下面的 resizeColumnsToContents 叠加会把末列拉成一大片空白，
        #   看上去像"少了一列"。列宽一律按内容算。
        hh.setStretchLastSection(False)
        root.addWidget(self.tbl_tracks, 1)

        root.addWidget(QLabel(T("内嵌素材清单")))
        self.tbl_media = QTableWidget(0, 4, self)
        self.tbl_media.setHorizontalHeaderLabels(
            [T("文件名"), T("片段 ID"), T("时长"), T("透明 / 内嵌")])
        self.tbl_media.verticalHeader().setVisible(False)
        self.tbl_media.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_media.setAlternatingRowColors(True)
        self.tbl_media.horizontalHeader().setStretchLastSection(False)
        root.addWidget(self.tbl_media, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close, self)
        bb.button(QDialogButtonBox.Close).setText(T("关闭"))
        bb.rejected.connect(self.reject)
        bb.clicked.connect(lambda _b: self.close())
        root.addWidget(bb)

    # ---------------- 填充 ----------------
    def _load(self):
        """全量读一次包（详情页允许慢一点：用户主动双击才会有这一步）。"""
        if not os.path.isfile(self.path):
            self.err = Tf("文件不存在：{}", self.path)
            self._fill_head(0, 0)
            return
        try:
            self.sheet, _info = cuesheet.load(self.path)
        except Exception as e:                                   # noqa: BLE001
            self.err = Tf("读取失败：{}", "%s: %s" % (type(e).__name__, e))
            self._fill_head(0, 0)
            return
        warns = []
        try:
            from version import APP_VERSION
            w, _miss = cuesheet.capability_report(self.sheet,
                                                 app_version=str(APP_VERSION))
            warns = list(w or [])
        except Exception:                                        # noqa: BLE001
            warns = []
        self.err = "\n".join(warns)
        self._fill_head(int(getattr(self.sheet, "res_w", 0) or 0),
                        int(getattr(self.sheet, "res_h", 0) or 0))
        self._fill_tracks()
        self._fill_media()

    def _fill_head(self, res_w, res_h):
        e = self.entry
        n_frag = sum(len(getattr(ly, "items", None) or [])
                     for t in (getattr(self.sheet, "tracks", None) or [])
                     for ly in (getattr(t, "layers", None) or []))
        lines = [
            Tf("表名：{}", sheet_label(e)),
            Tf("画布：{} × {}", res_w or "—", res_h or "—"),
            Tf("曲目：{} 首 ｜ 片段：{} 个 ｜ 素材：{} 个",
               int(e.get("n_tracks", 0) or 0), n_frag,
               len(getattr(self.sheet, "media", None) or []) if self.sheet else 0),
            Tf("文件：{}", self.path),
            Tf("大小：{} ｜ 修改时间：{}",
               fmt_size(e.get("size")), fmt_mtime(e.get("mtime"))),
            Tf("格式版本：{} ｜ 要求主机版本：{}",
               int(e.get("format_version", 0) or 0),
               str(e.get("min_app_version") or T("无"))),
        ]
        self.lbl_head.setText("\n".join(lines))
        self.lbl_err.setText(self.err)

    def _fill_tracks(self):
        tracks = list(getattr(self.sheet, "tracks", None) or []) if self.sheet else []
        self.tbl_tracks.setRowCount(len(tracks))
        for r, t in enumerate(tracks):
            idn = getattr(t, "identity", None)
            bpm = float(getattr(idn, "bpm", 0.0) or 0.0) if idn is not None else 0.0
            dur = float(getattr(idn, "duration_sec", 0.0) or 0.0) if idn is not None else 0.0
            lays = list(getattr(t, "layers", None) or [])
            per = ["%s:%d" % (str(getattr(ly, "name", "") or ("层%d" % (k + 1))),
                              len(getattr(ly, "items", None) or []))
                   for k, ly in enumerate(lays)]
            total = sum(len(getattr(ly, "items", None) or []) for ly in lays)
            cells = [
                str(getattr(idn, "title", "") or T("(无标题)")) if idn else T("(无标题)"),
                str(getattr(idn, "artist", "") or "") if idn else "",
                ("%g" % bpm) if bpm > 0 else T("无（退化 1 拍=1 秒）"),
                ("%d:%02d" % (int(dur) // 60, int(dur) % 60)) if dur > 0 else "—",
                Tf("{} 层 / {} 片段", len(lays), total),
                "、".join(per) or "—",
            ]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                if c == 2 and bpm <= 0:
                    it.setToolTip(T("这张码表的该曲目没有 BPM：只能按「1 拍 = 1 秒」播放，"
                                    "画面不会对拍。请在 EasyRealityCue 里补上 BPM 后重新导出。"))
                self.tbl_tracks.setItem(r, c, it)
        _fit_columns(self.tbl_tracks)

    def _fill_media(self):
        media = list(getattr(self.sheet, "media", None) or []) if self.sheet else []
        self.tbl_media.setRowCount(len(media))
        for r, m in enumerate(media):
            dur = float(getattr(m, "duration_sec", 0.0) or 0.0)
            flags = []
            if getattr(m, "has_alpha", False):
                flags.append(T("透明"))
            if getattr(m, "embedded", False):
                flags.append(T("内嵌"))
            cells = [
                str(getattr(m, "name", "") or "?"),
                str(getattr(m, "clip_id", "") or "")[:16],
                ("%.2f s" % dur) if dur > 0 else "—",
                " / ".join(flags) or "—",
            ]
            for c, txt in enumerate(cells):
                self.tbl_media.setItem(r, c, QTableWidgetItem(txt))
        _fit_columns(self.tbl_media)


# ==========================================================================
# 管理页（第 4 个页签）
# ==========================================================================
class CueSheetPanel(QWidget):
    """码表管理页：列表 + 添加/删除/定位/刷新 + 双击详情 + 右键绑图层。

    `owner` 是主窗（需要 `.cfg` 与 `.engine`）。
    """

    COLS = ("表名", "覆盖曲目", "片段", "大小", "修改时间", "已绑图层")

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.cfg = getattr(owner, "cfg", None)
        self.engine = getattr(owner, "engine", None)
        self._frag_busy = False
        self._build()
        self.refresh()

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.lbl_title = QLabel(T("🎞 码表"))
        self.lbl_title.setStyleSheet("font-weight:bold;font-size:15px;color:"
                                     + _text_color() + ";")
        head.addWidget(self.lbl_title)
        head.addStretch(1)
        self.btn_add = QPushButton(T("添加…"), self)
        self.btn_add.setMinimumHeight(28)
        self.btn_add.setToolTip(T("导入 .ercue 码表包（可多选）"))
        self.btn_add.clicked.connect(self.on_add)
        head.addWidget(self.btn_add)
        self.btn_del = QPushButton(T("删除"), self)
        self.btn_del.setMinimumHeight(28)
        self.btn_del.setToolTip(T("从列表移除（不删除磁盘文件）"))
        self.btn_del.clicked.connect(self.on_remove)
        head.addWidget(self.btn_del)
        self.btn_locate = QPushButton(T("定位文件"), self)
        self.btn_locate.setMinimumHeight(28)
        self.btn_locate.setToolTip(T("在资源管理器里选中这个码表文件"))
        self.btn_locate.clicked.connect(self.on_locate)
        head.addWidget(self.btn_locate)
        self.btn_refresh = QPushButton(T("刷新"), self)
        self.btn_refresh.setMinimumHeight(28)
        self.btn_refresh.setToolTip(T("重新读取码表包的表头（文件改过才重读）"))
        self.btn_refresh.clicked.connect(self.on_refresh)
        head.addWidget(self.btn_refresh)
        # ★ 这里保留通往「码表（演出端）」轻量对话框的入口：
        #   手动起拍（临时位置源）/ 最近文件 / 定位档位 都在那个对话框里，
        #   删掉入口就等于把 Step5/6 的手动起拍弄丢了（用户报过要用）。
        self.btn_ops = QPushButton(T("更多操作…"), self)
        self.btn_ops.setMinimumHeight(28)
        self.btn_ops.setToolTip(T("打开码表对话框：最近文件 / 手动绑歌 / 手动起拍 / 定位档位"))
        self.btn_ops.clicked.connect(self.on_open_dialog)
        head.addWidget(self.btn_ops)
        root.addLayout(head)

        self.lbl_desc = QLabel(T(
            "这里管理演出端可用的码表文件：查看 / 添加 / 移除 / 定位。"
            "双击一行查看详情（只读）。\n"
            "「删除」只把码表从列表里移除，不会删除磁盘上的 .ercue 文件。"
            "编排请在 EasyRealityCue 里做，演出端只负责执行。"))
        self.lbl_desc.setWordWrap(True)
        self.lbl_desc.setStyleSheet("color:" + _muted_color() + ";")
        root.addWidget(self.lbl_desc)

        self.tbl = QTableWidget(0, len(self.COLS), self)
        self.tbl.setHorizontalHeaderLabels([T(c) for c in self.COLS])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSortingEnabled(False)
        self.tbl.doubleClicked.connect(lambda _i: self.on_detail())
        self.tbl.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._on_context_menu)
        hh = self.tbl.horizontalHeader()
        # ⚠ 最小列宽 88：`ResizeToContents` 在表头文字（「覆盖曲目」「修改时间」）比数据宽时
        #   会把列压到只剩一个字 —— 显式给下限，表名那列再吃剩余宽度。
        hh.setMinimumSectionSize(88)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        root.addWidget(self.tbl, 1)

        self.lbl_stat = QLabel("")
        self.lbl_stat.setStyleSheet("color:" + _muted_color() + ";font-size:11px;")
        root.addWidget(self.lbl_stat)

    # ---------------- 数据 ----------------
    def refresh(self):
        """重建表格（读索引，不扫盘）。

        ⚠ 「片段」列**不在构造时算**：它要全量 `cuesheet.load()`，启动路径绝不能碰
          （一张 169MB 的包能卡住好几秒）。这里只填**已缓存**的值，其余显示 "—"；
          真正的计算由 `ensure_frag_counts()` 在页面**首次可见**时异步补上。
        """
        entries = sheet_entries(self.owner)
        self.tbl.setRowCount(len(entries))
        missing = 0
        for r, e in enumerate(entries):
            path = os.path.abspath(str(e.get("path") or ""))
            alive = os.path.isfile(path)
            if not alive:
                missing += 1
            name = sheet_label(e)
            if not alive:
                name = Tf("{}（文件已丢失）", name)
            n_tr = int(e.get("n_tracks", 0) or 0)
            cells = [
                name,
                Tf("{} 首", n_tr) if n_tr else "—",
                self._frag_text(path, alive),
                fmt_size(e.get("size")) if alive else "—",
                fmt_mtime(e.get("mtime")) if alive else "—",
                "、".join(bound_layer_names(self.owner, e)) or "",
            ]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setData(Qt.UserRole, path)     # 每格都带路径 ⇒ 右键/双击随时能取
                if c == 0:
                    it.setToolTip(path)
                self.tbl.setItem(r, c, it)
        self._sync_stat(len(entries), missing)
        if self.tbl.rowCount() and self.tbl.currentRow() < 0:
            self.tbl.selectRow(0)

    @staticmethod
    def _frag_text(path, alive):
        """「片段」列文本：只读缓存，未算过就是 "—"（见 refresh 的说明）。"""
        if not alive:
            return "—"
        key = _stat_key(path)
        hit = _FRAG_CACHE.get(path)
        if hit is not None and key is not None and hit[0] == key:
            return str(int(hit[1]))
        return "—"

    def ensure_frag_counts(self):
        """页面可见时补齐「片段」列（异步一次，只读没缓存过的包）。"""
        if self._frag_busy:
            return
        self._frag_busy = True
        QTimer.singleShot(0, self._fill_frags)

    def _fill_frags(self):
        """真正去读包算片段数；读完**只刷表格的数字**（不打断用户当前选择）。"""
        changed = False
        try:
            entries = sheet_entries(self.owner)
        except Exception:                                        # noqa: BLE001
            entries = []
        for e in entries:
            p = os.path.abspath(str(e.get("path") or ""))
            if not os.path.isfile(p):
                continue
            key = _stat_key(p)
            hit = _FRAG_CACHE.get(p)
            if hit is not None and key is not None and hit[0] == key:
                continue
            sheet_frag_count(p)
            changed = True
        self._frag_busy = False
        if not changed:
            return
        # 只更新片段列（重排整表会丢选中/滚动位置）
        for r in range(self.tbl.rowCount()):
            it0 = self.tbl.item(r, 0)
            it2 = self.tbl.item(r, 2)
            if it0 is None or it2 is None:
                continue
            path = str(it0.data(Qt.UserRole) or "")
            it2.setText(self._frag_text(os.path.abspath(path), os.path.isfile(path)))

    def _sync_stat(self, n, missing):
        parts = [Tf("共 {} 张码表", n)]
        if missing:
            parts.append(Tf("{} 张文件已丢失（请「删除」后重新「添加」，或用「定位文件」检查）",
                            missing))
        lay = bound_layer_names_all(self.owner)
        if lay:
            parts.append(Tf("已绑图层：{}", "、".join(lay)))
        self.lbl_stat.setText(" ｜ ".join(parts))

    def _sel_paths(self):
        """当前选中的码表路径（多行 ⇒ 多条，去重保序）。"""
        out = []
        for r in sorted({i.row() for i in self.tbl.selectedIndexes()}):
            it = self.tbl.item(r, 0)
            if it is None:
                continue
            p = str(it.data(Qt.UserRole) or "")
            if p and p not in out:
                out.append(p)
        return out

    def _entry_for(self, path):
        for e in sheet_entries(self.owner):
            if os.path.abspath(str(e.get("path") or "")) == os.path.abspath(path):
                return e
        return None

    # ---------------- 动作 ----------------
    def on_add(self):
        """添加…：多选导入 .ercue ⇒ 只进列表（绑定用右键「绑到图层…」）。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, T("导入码表"), "", T("码表包 (*.ercue);;所有文件 (*)"))
        if not paths:
            return
        idx = None
        try:
            idx = self.engine.cue_index() if self.engine is not None else None
        except Exception:                                        # noqa: BLE001
            idx = None
        ok, bad = [], []
        for p in paths:
            done = False
            if idx is not None:
                try:
                    done = idx.add(p) is not None
                except Exception:                                # noqa: BLE001
                    done = False
            if done:
                ok.append(p)
            else:
                bad.append(p)
        if idx is not None:
            try:
                idx.save_cfg(self.cfg)
            except Exception:                                    # noqa: BLE001
                pass
        self.refresh()
        if bad:
            QMessageBox.warning(
                self, T("导入码表"),
                "\n".join([Tf("已导入 {} 张码表。", len(ok)),
                           Tf("以下 {} 个文件不是有效的码表包：", len(bad))] + bad))
        elif ok:
            self._flash(Tf("已导入 {} 张码表。右键一行可以「绑到图层…」。", len(ok)))

    def on_remove(self):
        """删除：**只从列表移除**（二次确认，明确告知不删磁盘文件）。"""
        paths = self._sel_paths()
        if not paths:
            QMessageBox.information(self, T("删除码表"), T("请先选中要移除的码表。"))
            return
        names = [sheet_label(self._entry_for(p) or {"path": p}) for p in paths]
        box = QMessageBox(self)
        box.setWindowTitle(T("删除码表"))
        box.setIcon(QMessageBox.Question)
        box.setText(Tf("从列表移除以下 {} 张码表？", len(paths))
                    + "\n\n" + "\n".join("· " + n for n in names))
        box.setInformativeText(T("只会从列表里移除，不会删除磁盘上的 .ercue 文件。\n"
                                 "（之后仍可用「添加…」重新导入）"))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText(T("移除"))
        box.button(QMessageBox.No).setText(T("取消"))
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        idx = None
        try:
            idx = self.engine.cue_index() if self.engine is not None else None
        except Exception:                                        # noqa: BLE001
            idx = None
        if idx is None:
            QMessageBox.warning(self, T("删除码表"), T("码表索引不可用，无法移除。"))
            return
        for p in paths:
            try:
                idx.remove(p)
            except Exception:                                    # noqa: BLE001
                pass
        try:
            idx.save_cfg(self.cfg)
        except Exception:                                        # noqa: BLE001
            pass
        # 被移除的表如果正绑在图层上 ⇒ 卸下（否则图层会指向一张已不在列表里的表）
        removed = [os.path.basename(p) for p in paths]
        for lay in list(getattr(self.engine, "layers", None) or []):
            if not getattr(lay, "is_cuesheet", False):
                continue
            if str(getattr(lay, "cuesheet_id", "") or "") in removed:
                try:
                    import cue_ui
                    cue_ui.unbind_layer(self.owner, lay)
                except Exception:                                # noqa: BLE001
                    lay.is_cuesheet = False
                    lay.cuesheet_id = ""
        self._after_layers_changed()
        self.refresh()
        self._flash(Tf("已从列表移除 {} 张码表（磁盘文件仍在）。", len(paths)))

    def on_locate(self):
        """定位文件：在资源管理器里选中（文件不在了就打开其所在目录）。"""
        paths = self._sel_paths()
        if not paths:
            QMessageBox.information(self, T("定位文件"), T("请先选中一行。"))
            return
        p = paths[0]
        try:
            if os.path.isfile(p):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
            else:
                d = os.path.dirname(p)
                if os.path.isdir(d):
                    os.startfile(d)      # noqa: S606  Windows 默认程序打开
                else:
                    QMessageBox.warning(self, T("定位文件"),
                                        Tf("文件和目录都不存在了：{}", p))
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, T("定位文件"), "%s: %s" % (type(e).__name__, e))

    def on_refresh(self):
        """刷新：按 mtime/size 重读变了的包（引擎的 rescan 只读改过的）。"""
        try:
            fn = getattr(self.engine, "cue_index_refresh", None)
            if callable(fn):
                fn()
            else:
                idx = self.engine.cue_index(refresh=True)
                idx.save_cfg(self.cfg)
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, T("刷新码表"), "%s: %s" % (type(e).__name__, e))
        _FRAG_CACHE.clear()      # 包可能变了 ⇒ 片段数缓存整体失效（条目少，重建很便宜）
        self.refresh()

    def on_detail(self):
        """双击 ⇒ 只读详情。"""
        paths = self._sel_paths()
        if not paths:
            return
        e = self._entry_for(paths[0]) or {"path": paths[0]}
        CueSheetDetailDialog(self.owner, e, self).exec()

    def on_open_dialog(self):
        """「更多操作…」⇒ 打开**原有的轻量对话框**（手动起拍/最近/定位档位都在里面）。

        ⚠ 必须复用 `ui_main.open_cuesheet()`（它再调 `cue_ui.open_cuesheet_dialog`）：
          对话框里那套"手动绑歌 + 手动起拍"是 Step5/6 的能力，**不许重写一份**。
        """
        fn = getattr(self.owner, "open_cuesheet", None)
        if callable(fn):
            try:
                fn()
            except Exception as e:                               # noqa: BLE001
                QMessageBox.warning(self, T("码表"), "%s: %s" % (type(e).__name__, e))
            self.refresh()

    # ---------------- 右键菜单 ----------------
    def _on_context_menu(self, pos):
        r = self.tbl.rowAt(pos.y())
        if r < 0:
            return
        if r not in {i.row() for i in self.tbl.selectedIndexes()}:
            self.tbl.selectRow(r)
        m = QMenu(self)
        act_detail = m.addAction(T("查看详情…"))
        act_bind = m.addAction(T("绑到图层…"))
        m.addSeparator()
        act_del = m.addAction(T("删除（仅从列表移除）"))
        act_locate = m.addAction(T("定位文件"))
        chosen = m.exec(self.tbl.viewport().mapToGlobal(pos))
        if chosen is act_detail:
            self.on_detail()
        elif chosen is act_bind:
            self.on_bind()
        elif chosen is act_del:
            self.on_remove()
        elif chosen is act_locate:
            self.on_locate()

    def on_bind(self):
        """绑到图层…：选一个现有图层，把它变成码表图层并装上这张表。"""
        paths = self._sel_paths()
        if not paths:
            QMessageBox.information(self, T("绑到图层"), T("请先选中一张码表。"))
            return
        path = paths[0]
        e = self._entry_for(path) or {"path": path}
        lays = list(getattr(self.engine, "layers", None) or [])
        if not lays:
            QMessageBox.information(self, T("绑到图层"), T("当前没有图层。"))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(T("绑到图层"))
        dlg.setMinimumWidth(380)
        vl = QVBoxLayout(dlg)
        vl.addWidget(QLabel(Tf("把码表「{}」应用到哪个图层？", sheet_label(e))))
        cmb = QComboBox(dlg)
        for i, ly in enumerate(lays):
            mark = T("（码表层）") if getattr(ly, "is_cuesheet", False) else ""
            cmb.addItem("%d. %s%s" % (i + 1, getattr(ly, "name", "?"), mark), i)
        vl.addWidget(cmb)
        hint = QLabel(T("提示：被绑定的图层会变成「码表图层」，画面由码表接管；"
                        "普通素材不会被删除。"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color:" + _muted_color() + ";font-size:11px;")
        vl.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dlg)
        bb.button(QDialogButtonBox.Ok).setText(T("绑定"))
        bb.button(QDialogButtonBox.Cancel).setText(T("取消"))
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        vl.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        i = int(cmb.currentData() if cmb.currentData() is not None else 0)
        if not (0 <= i < len(lays)):
            return
        self.bind_to_layer(path, lays[i])

    def bind_to_layer(self, path, layer):
        """把 `path` 装到 `layer`（导入/校验/绑曲/报错全走 `cue_ui.load_and_bind`）。"""
        try:
            import cue_ui
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, T("绑到图层"),
                                Tf("加载码表模块失败：{}", str(e)))
            return False
        ok, msg = cue_ui.load_and_bind(self.owner, path, layer, parent=self)
        self._after_layers_changed()
        self.refresh()
        if msg:
            QMessageBox.warning(self, T("绑到图层"), msg)
        elif ok:
            self._flash(Tf("已把「{}」绑到图层「{}」。",
                           sheet_label(self._entry_for(path) or {"path": path}),
                           getattr(layer, "name", "?")))
        return ok

    # ---------------- 杂项 ----------------
    def _after_layers_changed(self):
        """绑/卸之后：图层面板徽标（下拉）与配置必须跟着变。"""
        try:
            self.owner.save_layers()
        except Exception:                                        # noqa: BLE001
            pass
        panel = getattr(self.owner, "layers_panel", None)
        for fn in (getattr(panel, "refresh_header", None),
                   getattr(panel, "refresh_playing", None)):
            if callable(fn):
                try:
                    fn()
                except Exception:                                # noqa: BLE001
                    pass

    def _flash(self, msg):
        self.lbl_stat.setText(str(msg))

    def retranslate(self):
        """语言切换：表头与标题是**表头文本**（i18n 树遍历不处理 QTableWidget）⇒ 显式重设。"""
        self.lbl_title.setText(T("🎞 码表"))
        self.btn_add.setText(T("添加…"))
        self.btn_del.setText(T("删除"))
        self.btn_locate.setText(T("定位文件"))
        self.btn_refresh.setText(T("刷新"))
        self.btn_ops.setText(T("更多操作…"))
        self.lbl_desc.setText(T(
            "这里管理演出端可用的码表文件：查看 / 添加 / 移除 / 定位。"
            "双击一行查看详情（只读）。\n"
            "「删除」只把码表从列表里移除，不会删除磁盘上的 .ercue 文件。"
            "编排请在 EasyRealityCue 里做，演出端只负责执行。"))
        self.tbl.setHorizontalHeaderLabels([T(c) for c in self.COLS])
        self.refresh()


# ==========================================================================
# 主题取色（容错：theme 取不到值时给中性色，绝不因样式抛异常）
# ==========================================================================
def _v(key, fallback):
    try:
        import theme
        return str(theme.V(key) or fallback)
    except Exception:                                            # noqa: BLE001
        return fallback


def _fit_columns(tbl, pad=14):
    """按内容定列宽 + 一点右边距。

    ⚠ 直接 `resizeColumnsToContents()` 在**未显示的对话框**上会按"尚未布局"的字体量宽，
      中文列会差几个像素被切掉（实测「夏夜星律」显示成「夏夜星律」缺一笔）。统一补 pad。
    """
    try:
        tbl.resizeColumnsToContents()
        for c in range(tbl.columnCount()):
            tbl.setColumnWidth(c, tbl.columnWidth(c) + pad)
    except Exception:                                            # noqa: BLE001
        pass


def _text_color():
    return _v("text", "#dddddd")


def _muted_color():
    return _v("muted", "#999999")
