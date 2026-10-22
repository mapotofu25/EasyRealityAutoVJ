# -*- coding: utf-8 -*-
"""导出前检查弹窗 + 缺失素材重挂对话框。

★ 分工（任务书硬要求："检查逻辑和弹窗分离成两个函数"）：
  · 检查逻辑 → `model/precheck.check_sheet()`（纯函数、零 Qt、可单测）
  · 弹窗     → 本模块 `ask_export()` / `ask_missing_media()`（**只在有 QApplication
               且非测试模式时被主窗口调用**，无头测试里根本不进这里）

`report_text()` 是**纯字符串**构造（不碰 Qt），便于单测与日志复用。
"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton,
    QSpinBox, QVBoxLayout,
)

from . import theme

log = logging.getLogger("ercue.ui.dialogs")


# ==========================================================================
# 纯文本（不碰 Qt）
# ==========================================================================
def report_text(report) -> str:
    """把 `precheck.Report` 渲染成一段中文说明（供弹窗 / 日志共用）。"""
    if report is None:
        return "（没有检查结果）"
    lines = list(report.summary_lines())
    if not lines:
        return "检查通过：没有发现缺素材 / 超长的问题。"
    if report.blocking:
        lines.insert(0, "⚠ 发现会**阻止导出**的问题（缺素材）。")
    return "\n".join(lines)


# ==========================================================================
# 导出决策
# ==========================================================================
class ExportDecision:
    """导出弹窗的结果。`proceed=False` 表示用户取消。"""

    def __init__(self, proceed: bool, embed: bool = False):
        self.proceed = bool(proceed)
        self.embed = bool(embed)


# ==========================================================================
# 红色圆点（缺失素材标记）
# ==========================================================================
def red_dot_icon(size: int = 10) -> QIcon:
    """造一个**红色圆点** QIcon（缺失素材列表项前面那个点）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.c("missing")))
        p.drawEllipse(0, 0, size - 1, size - 1)
    finally:
        p.end()
    return QIcon(pm)


# ==========================================================================
# 导出前检查弹窗
# ==========================================================================
class ExportCheckDialog(QDialog):
    """展示导出前检查清单 + 「内嵌素材」勾选 + 「仍然导出 / 取消」。"""

    def __init__(self, report, embed_default=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出前检查")
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)

        head = QLabel("导出前检查发现以下问题：")
        head.setStyleSheet("font-weight:bold;")
        v.addWidget(head)

        txt = QPlainTextEdit(report_text(report))
        txt.setReadOnly(True)
        txt.setMinimumHeight(180)
        if report is not None and report.blocking:
            txt.setStyleSheet("color:%s;" % theme.c("missing"))
        v.addWidget(txt)

        self.chk_embed = QCheckBox("把素材一起打包进 .ercue（超过 1 GB 会提醒，别人换机也能打开）")
        self.chk_embed.setChecked(bool(embed_default))
        v.addWidget(self.chk_embed)

        hint = QLabel("缺素材不会写进包：别人拿到后对应片段会显示「文件丢失」。"
                      "可先「仍然导出」，回头用「指定素材目录…」重挂。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;font-size:11px;" % theme.c("fg_dim"))
        v.addWidget(hint)

        bb = QDialogButtonBox()
        self.btn_go = QPushButton("仍然导出")
        self.btn_cancel = QPushButton("取消")
        bb.addButton(self.btn_go, QDialogButtonBox.AcceptRole)
        bb.addButton(self.btn_cancel, QDialogButtonBox.RejectRole)
        self.btn_go.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        v.addWidget(bb)

        # ★ 缺素材时默认焦点在「取消」（规格书 §15 最后一条：阻止导出并提示）
        if report is not None and report.blocking:
            self.btn_cancel.setDefault(True)
            self.btn_cancel.setFocus()
        else:
            self.btn_go.setDefault(True)
            self.btn_go.setFocus()


def ask_export(parent, report, embed_default=False) -> ExportDecision:
    """弹导出前检查对话框。**没有 QApplication 时直接放行（不阻塞）**。"""
    if QApplication.instance() is None:
        return ExportDecision(True, embed_default)
    dlg = ExportCheckDialog(report, embed_default, parent)
    ok = dlg.exec() == QDialog.Accepted
    return ExportDecision(ok, dlg.chk_embed.isChecked())


# ==========================================================================
# 缺失素材重挂对话框（导入完成时弹一次）
# ==========================================================================
class MissingMediaDialog(QDialog):
    """列出缺失素材，提供「逐个指定新路径…」/「用占位色块先跑」。"""

    def __init__(self, missing_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("有素材没找到")
        self.setMinimumWidth(460)
        self.remap = {}                                # {name: 新路径}
        self._names = list(missing_names or [])
        v = QVBoxLayout(self)

        v.addWidget(QLabel("%d 个素材没找到：" % len(self._names)))
        lst = QListWidget()
        for n in self._names:
            lst.addItem(str(n))
        lst.setMaximumHeight(160)
        v.addWidget(lst)

        hint = QLabel("可以「逐个指定新路径…」把文件重新挂上；也可以「用占位色块先跑」——"
                      "缺失片段在预览里会被跳过（保持上一层），编排照常能编辑、能导出。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;font-size:11px;" % theme.c("fg_dim"))
        v.addWidget(hint)

        row = QDialogButtonBox()
        self.btn_pick = QPushButton("逐个指定新路径…")
        self.btn_placeholder = QPushButton("用占位色块先跑")
        row.addButton(self.btn_pick, QDialogButtonBox.ActionRole)
        row.addButton(self.btn_placeholder, QDialogButtonBox.AcceptRole)
        self.btn_pick.clicked.connect(self._pick_all)
        self.btn_placeholder.clicked.connect(self.accept)
        v.addWidget(row)

    def _pick_all(self):
        start = ""
        for n in self._names:
            p, _ = QFileDialog.getOpenFileName(self, "为「%s」指定新路径" % n, start,
                                               "视频 / 图片 (*.mp4 *.mov *.webm *.avi *.mkv "
                                               "*.png *.jpg *.jpeg *.bmp);;所有文件 (*)")
            if p:
                self.remap[str(n)] = p
                start = os.path.dirname(p)
        self.accept()


def ask_missing_media(parent, missing_names) -> dict:
    """弹缺失素材对话框。**没有 QApplication 时返回空映射（不阻塞）**。

    返回 `{素材名: 新路径}`（用户没指定就是空 dict）。
    """
    if QApplication.instance() is None or not missing_names:
        return {}
    dlg = MissingMediaDialog(missing_names, parent)
    dlg.exec()
    return dict(dlg.remap)


# ==========================================================================
# 新建码表（需求 1：码表名 + 画布比例）
# ==========================================================================
# 预设比例（label → (w, h)）；"自定义"由用户填宽高。**唯一来源**，UI 不再散落字面量。
ASPECT_PRESETS = [
    ("16:9（1920 × 1080）", 1920, 1080),
    ("9:16（1080 × 1920，竖屏）", 1080, 1920),
    ("4:3（1440 × 1080）", 1440, 1080),
    ("3:4（1080 × 1440，竖屏）", 1080, 1440),
    ("自定义…", 0, 0),
]


class NewSheetDialog(QDialog):
    """新建码表：码表名 + 画布比例（需求 1）。"""

    def __init__(self, default_title="新建码表", parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建码表")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("码表名"))
        self.le_title = QLineEdit(str(default_title or "新建码表"))
        v.addWidget(self.le_title)

        v.addWidget(QLabel("画布比例（决定预览/导出的画面比例）"))
        self.cb_aspect = QComboBox()
        for label, _w, _h in ASPECT_PRESETS:
            self.cb_aspect.addItem(label)
        v.addWidget(self.cb_aspect)

        self.row_wh = QHBoxLayout()
        self.sp_w = QSpinBox()
        self.sp_w.setRange(16, 16384)
        self.sp_w.setValue(1920)
        self.sp_h = QSpinBox()
        self.sp_h.setRange(16, 16384)
        self.sp_h.setValue(1080)
        self.row_wh.addWidget(QLabel("宽"))
        self.row_wh.addWidget(self.sp_w)
        self.row_wh.addWidget(QLabel("高"))
        self.row_wh.addWidget(self.sp_h)
        self.row_wh.addStretch(1)
        v.addLayout(self.row_wh)
        self.cb_aspect.currentIndexChanged.connect(self._on_aspect)
        self._on_aspect(0)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _on_aspect(self, idx):
        """选预设 ⇒ 填宽高并禁用输入；选"自定义" ⇒ 开放输入。"""
        custom = 0 <= idx < len(ASPECT_PRESETS) and ASPECT_PRESETS[idx][1] == 0
        if not custom and 0 <= idx < len(ASPECT_PRESETS):
            _lbl, w, h = ASPECT_PRESETS[idx]
            self.sp_w.setValue(int(w))
            self.sp_h.setValue(int(h))
        self.sp_w.setEnabled(custom)
        self.sp_h.setEnabled(custom)

    def values(self):
        return (self.le_title.text().strip() or "新建码表",
                int(self.sp_w.value()), int(self.sp_h.value()))


def ask_new_sheet(parent, default_title="新建码表"):
    """弹"新建码表"对话框，返回 `(title, res_w, res_h)`；取消 / 无 QApplication 返回 **None**。"""
    if QApplication.instance() is None:
        return None
    dlg = NewSheetDialog(default_title, parent)
    if dlg.exec() != QDialog.Accepted:
        return None
    return dlg.values()
