# -*- coding: utf-8 -*-
"""能量段落编辑对话框（用户 2026-10-28 要求）。

为什么单独一个文件：`music_detail.py` 已经很长了，而且这个对话框将来 ERCUE 端
可能要复用同一套交互（但**现在只做 ERAVJ**，见用户 2026-10-28 的边界要求）。

交互设计（与"手填一张表"相比，三个按钮解决绝大多数场景）：
  · **＋ 在播放头加段**：先把播放头拖到"进 drop 的那一刻"，点一下就切一刀 ——
    比让用户自己读数填秒快得多；
  · **吸附到八拍**：把所有边界拉到最近的八拍头（与波形图那套 anchor + 8n×beat_len 同源）；
  · **恢复默认四段模板**：intro/build/drop/outro 骨架，改两个数字就能用。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

import sections as S

TIER_LABEL = {"low": "低 (low)", "mid": "中 (mid)", "high": "高 (high)"}
TIER_ORDER = ("low", "mid", "high")


class EnergySectionDialog(QDialog):
    """编辑一首歌的能量段落表。

    `sections`：已有数据（未必要 normalize，构造时会先整理一遍）；
    `dur`     ：曲长（秒），末段 `t1` 用它；
    `grid`    ：`{"anchor": 秒, "beat_len": 秒}`，给了才能"吸附到八拍"；
    `cur_sec` ：打开时的播放头位置，"在播放头加段"用它。
    """

    def __init__(self, parent=None, sections=None, dur=0.0,
                 grid=None, cur_sec=0.0):
        super().__init__(parent)
        self.setWindowTitle("能量段落 — 让段落切换踩在八拍上")
        self.resize(760, 420)
        self._dur = float(dur or 0.0)
        self._grid = dict(grid or {})
        self._cur = float(cur_sec or 0.0)
        self._rows = S.normalize(sections or [], self._dur, self._grid or None)
        if not self._rows:
            self._rows = S.make_default(self._dur, grid=self._grid or None)
        self._result = []

        root = QVBoxLayout(self)

        head = QLabel(
            "每个段落写明「目标能量」和「档位」。演出时指纹认到歌、位置一对齐，"
            "到点就按这张表**立刻**切素材池 —— 不再等实测能量爬上来。\n"
            "边界会自动吸附到八拍头（与波形图的八拍线同一条网格）。")
        head.setWordWrap(True)
        head.setStyleSheet("color:#9fb3c8;")
        root.addWidget(head)

        self.tbl = QTableWidget(0, 5, self)
        self.tbl.setHorizontalHeaderLabels(
            ["起点 (秒)", "终点 (秒)", "档位", "能量 0~1", "名称"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tbl.setColumnWidth(0, 110)
        self.tbl.setColumnWidth(1, 110)
        self.tbl.setColumnWidth(2, 120)
        self.tbl.setColumnWidth(3, 110)
        root.addWidget(self.tbl, 1)

        self.lbl_grid = QLabel()
        self.lbl_grid.setStyleSheet("color:#7f93a8;")
        root.addWidget(self.lbl_grid)

        bar = QHBoxLayout()
        self.btn_add = QPushButton("＋ 在播放头加段")
        self.btn_del = QPushButton("删除选中段")
        self.btn_snap = QPushButton("吸附到八拍")
        self.btn_tpl = QPushButton("恢复默认四段模板")
        for b in (self.btn_add, self.btn_del, self.btn_snap, self.btn_tpl):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        root.addWidget(bb)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_del.clicked.connect(self._on_del)
        self.btn_snap.clicked.connect(self._on_snap)
        self.btn_tpl.clicked.connect(self._on_template)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)

        self._rebuild()

    # ---------------- 表格 <-> 数据 ----------------
    def _rebuild(self):
        """把 `self._rows` 铺到表格里。"""
        self.tbl.setRowCount(0)
        for r in self._rows:
            self._append_row(r)
        self._update_grid_hint()
        self.btn_del.setEnabled(len(self._rows) >= 2)
        self.btn_snap.setEnabled(bool(self._grid.get("beat_len")))

    def _append_row(self, r):
        i = self.tbl.rowCount()
        self.tbl.insertRow(i)
        it0 = QTableWidgetItem("%.2f" % float(r.get("t0") or 0.0))
        it0.setData(Qt.ItemDataRole.UserRole, float(r.get("t0") or 0.0))
        self.tbl.setItem(i, 0, it0)
        it1 = QTableWidgetItem("%.2f" % float(r.get("t1") or 0.0))
        it1.setFlags(it1.flags() & ~Qt.ItemFlag.ItemIsEditable)   # 终点由下一段起点决定
        it1.setForeground(Qt.GlobalColor.gray)
        self.tbl.setItem(i, 1, it1)
        cb = QComboBox()
        for t in TIER_ORDER:
            cb.addItem(TIER_LABEL[t], t)
        tier = str(r.get("tier") or "mid")
        cb.setCurrentIndex(TIER_ORDER.index(tier) if tier in TIER_ORDER else 1)
        cb.currentIndexChanged.connect(self._on_cell_changed)
        self.tbl.setCellWidget(i, 2, cb)
        sp = QDoubleSpinBox()
        sp.setRange(0.0, 1.0)
        sp.setSingleStep(0.05)
        sp.setDecimals(2)
        sp.setValue(float(r.get("energy") or 0.0))
        sp.valueChanged.connect(self._on_cell_changed)
        self.tbl.setCellWidget(i, 3, sp)
        self.tbl.setItem(i, 4, QTableWidgetItem(str(r.get("name") or "")))

    def _read_table(self):
        """从表格收集成 raw（不做校验，交给 `sections.normalize`）。"""
        out = []
        for i in range(self.tbl.rowCount()):
            it0 = self.tbl.item(i, 0)
            try:
                t0 = float(it0.text()) if it0 is not None else 0.0
            except (TypeError, ValueError):
                t0 = 0.0
            cb = self.tbl.cellWidget(i, 2)
            sp = self.tbl.cellWidget(i, 3)
            tier = cb.currentData() if isinstance(cb, QComboBox) else "mid"
            energy = sp.value() if isinstance(sp, QDoubleSpinBox) else 0.5
            it4 = self.tbl.item(i, 4)
            rec = {"t0": t0, "energy": float(energy),
                   "tier": str(tier or "mid"),
                   "name": (it4.text() if it4 is not None else "")}
            tms = None
            if i < len(self._rows):
                tms = self._rows[i].get("trans_ms")
            if tms is not None:
                rec["trans_ms"] = tms
            out.append(rec)
        return out

    def _commit(self):
        """读表格 → normalize → 存回 `self._rows`（表格不自动重铺，避免打字被打断）。"""
        self._rows = S.normalize(self._read_table(), self._dur,
                                 self._grid or None)
        return self._rows

    def _on_cell_changed(self, *_a):
        self._commit()

    def _update_grid_hint(self):
        try:
            bl = float(self._grid.get("beat_len") or 0.0)
            a = float(self._grid.get("anchor") or 0.0)
        except (TypeError, ValueError):
            bl, a = 0.0, 0.0
        if bl > 1e-6:
            step = bl * S.PHRASE_BEATS
            self.lbl_grid.setText(
                "八拍网格：一发 %0.2f 秒 = 8 拍 × %.4f 秒，锚点 %.2f 秒；"
                "波形上的八拍线就在这些位置上。曲长 %.1f 秒。"
                % (step, bl, a, self._dur))
        else:
            self.lbl_grid.setText(
                "这首歌还没有网格（BPM 未知）⇒ 无法吸附到八拍，边界按填的秒数原样使用。"
                "曲长 %.1f 秒。" % self._dur)

    # ---------------- 按钮 ----------------
    def _on_add(self):
        rows = self._commit()
        t = float(self._cur)
        if not (0.0 < t < (self._dur or 1e9)):
            QMessageBox.information(self, "加段",
                                    "播放头位置不可用（%.2f 秒）。先把播放头拖到要切的地方。" % t)
            return
        rows.append({"t0": t, "energy": 0.6, "tier": "mid", "name": ""})
        self._rows = S.normalize(rows, self._dur, self._grid or None)
        self._rebuild()

    def _on_del(self):
        i = self.tbl.currentRow()
        rows = self._commit()
        if len(rows) < 2 or not (0 <= i < len(rows)):
            return
        if i == 0 and len(rows) >= 2:
            # 删首段 = 把第二段的起点拉到 0（网格必须从 0 开始）
            rows[1]["t0"] = 0.0
        rows.pop(i if i > 0 else 0)
        self._rows = S.normalize(rows, self._dur, self._grid or None)
        self._rebuild()

    def _on_snap(self):
        rows = self._commit()
        bl = float(self._grid.get("beat_len") or 0.0)
        if bl <= 1e-6:
            QMessageBox.information(self, "吸附到八拍",
                                    "这首歌没有网格（BPM 未知），无法吸附。")
            return
        self._rows = S.normalize(rows, self._dur, self._grid)
        self._rebuild()

    def _on_template(self):
        if self._rows:
            r = QMessageBox.question(
                self, "恢复默认模板",
                "用「intro / build / drop / outro」四段骨架覆盖当前段落表？（能量和边界都可再改）")
            if r != QMessageBox.StandardButton.Yes:
                return
        tpl = S.make_default(self._dur, grid=self._grid or None)
        if not tpl:
            QMessageBox.information(self, "恢复默认模板", "曲长未知，无法生成模板。")
            return
        self._rows = tpl
        self._rebuild()

    def _on_ok(self):
        rows = self._commit()
        if rows and len(rows) == 1 and self._dur > 0 and rows[0]["t1"] < self._dur * 0.5:
            QMessageBox.information(self, "能量段落",
                                    "只填了一段且只覆盖到 %.1f 秒，后面 %.0f 秒没有段落 ——"
                                    "那段会回到「按实测能量」的旧行为。确定要这样吗？"
                                    % (rows[0]["t1"], self._dur - rows[0]["t1"]))
        self._result = rows
        self.accept()

    def sections(self):
        """确定后的段落表（`normalize` 过）。取消时为空列表。"""
        return [dict(r) for r in (self._result or [])]
