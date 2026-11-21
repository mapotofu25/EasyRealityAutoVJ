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
from PySide6.QtWidgets import (QButtonGroup, QRadioButton,
                               QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

import sections as S

TIER_LABEL = {"low": "低 (low)", "mid": "中 (mid)", "high": "高 (high)"}
# ★ 2026-11-13：「档位覆盖」列。空字符串 = 自动（由能量算），其余 = 锁定为该档。
TIER_CHOICES = ("", "low", "mid", "high")
TIER_CHOICE_LABEL = {"": "自动", "low": "低 (low)", "mid": "中 (mid)",
                     "high": "高 (high)"}
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
        # ★★ 保留**未吸附**的原始边界：切换"八拍/小节"时必须从它重算。
        #   ⚠ 八拍线是小节线的子集（8 拍 = 2 个小节），一旦把边界吸到八拍，
        #     再切成小节就是原地不动 —— 实测 25.2 → 25.95（八拍）→ 25.95（小节），
        #     用户会以为选项没生效。只有从原始值重算才能双向切换。
        self._orig = [dict(x) for x in (sections or []) if isinstance(x, dict)]
        self._rows = S.normalize(sections or [], self._dur, self._grid or None)
        if not self._rows:
            self._rows = S.make_default(self._dur, grid=self._grid or None)
            # ★ 2026-11-18：没有已存段落时表格显示的是**默认 4 段**；`_orig`
            #   必须同步成这 4 段，否则第一次切“八拍/小节”就把整张表清空。
            self._orig = [dict(x) for x in self._rows]
        self._result = []

        root = QVBoxLayout(self)

        head = QLabel(
            "每个段落写明「目标能量」和「档位」。演出时指纹认到歌、位置一对齐，"
            "到点就按这张表**立刻**切素材池 —— 不再等实测能量爬上来。\n"
            "边界会按下方选择的粒度吸附：八拍（乐句头）或小节（4 拍）——"
            "与波形图上那套网格是同一条。")
        head.setWordWrap(True)
        head.setStyleSheet("color:#9fb3c8;")
        root.addWidget(head)

        self.tbl = QTableWidget(0, 6, self)
        # ★ 2026-10-30 用户要求：「能量 改成该段平均能量」「依据输入的能量数值自动确定挡位」
        #   ⇒ 表头直接写清楚：能量是**该段平均**，档位是**自动**算的（不再手动选）。
        self.tbl.setHorizontalHeaderLabels(
            ["起点 (秒)", "终点 (秒)", "档位（自动）", "该段平均能量", "名称",
             "档位覆盖"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.tbl.setColumnWidth(0, 110)
        self.tbl.setColumnWidth(1, 110)
        self.tbl.setColumnWidth(2, 120)
        self.tbl.setColumnWidth(3, 110)
        self.tbl.setColumnWidth(5, 130)
        root.addWidget(self.tbl, 1)

        self.lbl_grid = QLabel()
        self.lbl_grid.setStyleSheet("color:#7f93a8;")
        root.addWidget(self.lbl_grid)

        # ★ 2026-10-29 用户要求：「吸附到八拍」和「吸附到小节」**二选一**。
        #   用 QRadioButton 而不是两个按钮：按钮是"点一次执行一次"，
        #   而这里要的是一个**持续生效的粒度设置** —— 之后每次改边界、加段、
        #   恢复模板都按它吸附。QButtonGroup 保证互斥（同组只能选一个）。
        snap_row = QHBoxLayout()
        snap_row.addWidget(QLabel("边界吸附："))
        self.rb_phrase = QRadioButton("八拍（乐句）")
        self.rb_bar = QRadioButton("小节（4 拍）")
        self.rb_phrase.setChecked(True)
        self.snap_group = QButtonGroup(self)
        self.snap_group.addButton(self.rb_phrase, 8)
        self.snap_group.addButton(self.rb_bar, 4)
        snap_row.addWidget(self.rb_phrase)
        snap_row.addWidget(self.rb_bar)
        snap_row.addStretch(1)
        root.addLayout(snap_row)

        bar = QHBoxLayout()
        self.btn_add = QPushButton("＋ 在播放头加段")
        self.btn_del = QPushButton("删除选中段")
        self.btn_tpl = QPushButton("恢复默认四段模板")
        for b in (self.btn_add, self.btn_del, self.btn_tpl):
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
        self.snap_group.buttonClicked.connect(self._on_snap_mode)
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
        _has_grid = bool(self._grid.get("beat_len"))
        self.rb_phrase.setEnabled(_has_grid)
        self.rb_bar.setEnabled(_has_grid)

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
        # ★★ 2026-10-30 用户要求「依据输入的能量数值自动确定挡位」「去掉手动改挡位」
        #   ⇒ 档位列改成**只读显示**，值由能量列算出（同 `sections.tier_from_energy`）。
        #   为什么：档位本来就是能量的函数，两处各写一份会打架（用户把能量改成 0.9
        #   而档位还挂着手选的 low ⇒ 演出时按 low 换池，与看到的数字对不上）。
        _e0 = float(r.get("energy") or 0.0)
        _auto = S.tier_from_energy(_e0)
        _cur = str(r.get("tier") or "").strip().lower()
        _locked = (_cur in S.TIERS) and (_cur != _auto)
        it_tier = QTableWidgetItem(TIER_LABEL.get(_cur if _locked else _auto, ""))
        it_tier.setFlags(it_tier.flags() & ~Qt.ItemFlag.ItemIsEditable)
        it_tier.setForeground(Qt.GlobalColor.gray)
        self.tbl.setItem(i, 2, it_tier)
        sp = QDoubleSpinBox()
        sp.setRange(0.0, 1.0)
        sp.setSingleStep(0.05)
        sp.setDecimals(2)
        sp.setValue(float(r.get("energy") or 0.0))
        sp.valueChanged.connect(self._on_cell_changed)
        self.tbl.setCellWidget(i, 3, sp)
        self.tbl.setItem(i, 4, QTableWidgetItem(str(r.get("name") or "")))
        # ★ 2026-11-13：档位覆盖（默认“自动”= 仍由能量算）。只在用户明确改过时才锁档位。
        cb = QComboBox()
        for _t in TIER_CHOICES:
            cb.addItem(TIER_CHOICE_LABEL[_t], _t)
        cb.setCurrentIndex(TIER_CHOICES.index(_cur) if _locked else 0)
        cb.currentIndexChanged.connect(self._on_cell_changed)
        self.tbl.setCellWidget(i, 5, cb)

    def _read_table(self):
        """从表格收集成 raw（不做校验，交给 `sections.normalize`）。"""
        out = []
        for i in range(self.tbl.rowCount()):
            it0 = self.tbl.item(i, 0)
            try:
                t0 = float(it0.text()) if it0 is not None else 0.0
            except (TypeError, ValueError):
                t0 = 0.0
            sp = self.tbl.cellWidget(i, 3)
            energy = sp.value() if isinstance(sp, QDoubleSpinBox) else 0.5
            # ★ 2026-10-30：默认档位由能量算；2026-11-13 起允许「档位覆盖」手动锁档。
            cb = self.tbl.cellWidget(i, 5)
            ovr = str(cb.currentData() or "") if isinstance(cb, QComboBox) else ""
            tier = ovr if ovr in S.TIERS else S.tier_from_energy(float(energy))
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
        old = [str(x.get("tier") or "") for x in (self._rows or [])]
        rows = S.normalize(self._read_table(), self._dur,
                           self._grid or None, beats=self.snap_beats())
        new = [str(x.get("tier") or "") for x in rows]
        # ★ 2026-11-18：normalize 可能因 t0 太近丢行/重排；继续按旧行号写名字
        #   会把名字写到错误行、表格还留下幽灵行。行数/顺序变了就整体重铺。
        if len(rows) != self.tbl.rowCount():
            self._rows = rows
            self._rebuild()
            return self._rows
        if old and new != old:
            # ★ 2026-11-13：档位被手动改过 ⇒ 段名跟着档位重编
            #   （与自动扫描同一套命名：高=drop n / 中=中段 n / 低=过渡 n）。
            S._renumber_names(rows)
            for i, r in enumerate(rows):
                it = self.tbl.item(i, 4)
                if it is not None and it.text() != r["name"]:
                    it.setText(r["name"])
        self._rows = rows
        return self._rows

    def _on_cell_changed(self, *_a):
        self._refresh_tier_col()      # 能量改了 ⇒ 档位列跟着变（档位不再手动选）
        self._commit()

    def _refresh_tier_col(self):
        """按能量列重算"档位"列（只读显示，见 `_append_row` 的说明）。"""
        for i in range(self.tbl.rowCount()):
            sp = self.tbl.cellWidget(i, 3)
            try:
                e = float(sp.value()) if isinstance(sp, QDoubleSpinBox) else 0.5
            except (TypeError, ValueError):
                e = 0.5
            it = self.tbl.item(i, 2)
            if it is None:
                it = QTableWidgetItem()
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it.setForeground(Qt.GlobalColor.gray)
                self.tbl.setItem(i, 2, it)
            cb = self.tbl.cellWidget(i, 5)
            ovr = str(cb.currentData() or "") if isinstance(cb, QComboBox) else ""
            want = TIER_LABEL.get(ovr if ovr in S.TIERS else S.tier_from_energy(e), "")
            if it.text() != want:
                it.setText(want)

    def _update_grid_hint(self):
        try:
            bl = float(self._grid.get("beat_len") or 0.0)
            a = float(self._grid.get("anchor") or 0.0)
        except (TypeError, ValueError):
            bl, a = 0.0, 0.0
        if bl > 1e-6:
            nb = self.snap_beats()
            step = bl * nb
            self.lbl_grid.setText(
                "%s：一发 %.2f 秒 = %d 拍 × %.4f 秒，锚点 %.2f 秒；"
                "波形上的网格线就在这些位置上。曲长 %.1f 秒。"
                % ("八拍" if nb == 8 else "小节", step, nb, bl, a, self._dur))
        else:
            self.lbl_grid.setText(
                "这首歌还没有网格（BPM 未知）⇒ 无法吸附，边界按填的秒数原样使用。"
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
        self._rows = S.normalize(rows, self._dur, self._grid or None,
                                 beats=self.snap_beats())
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
        self._rows = S.normalize(rows, self._dur, self._grid or None,
                                 beats=self.snap_beats())
        self._rebuild()

    def snap_beats(self):
        """当前吸附粒度（4=小节 / 8=八拍）。"""
        return int(self.snap_group.checkedId()) if self.snap_group.checkedId() in (4, 8) else 8

    def _on_snap_mode(self, _btn=None):
        """切换吸附粒度 ⇒ **立即**从**原始边界**按新粒度重排（不用再点按钮）。

        ⚠ 用 `self._orig`（打开对话框时的未吸附值）而不是表格当前值：
          表格里存的已经是吸附后的坐标，拿它再吸附只会往"更粗"走、回不来。
          代价是"手改了表格再切粒度"会丢掉手改 —— 但这两个动作本来就是二选一的
          语义（要么按网格对齐，要么手填），不会同时用。
        """
        if float(self._grid.get("beat_len") or 0.0) <= 1e-6:
            return
        self._rows = S.normalize([dict(x) for x in self._orig], self._dur,
                                 self._grid, beats=self.snap_beats())
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
        self._orig = [dict(x) for x in tpl]
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
