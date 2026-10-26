# -*- coding: utf-8 -*-
r"""ERLIB 详情页对话框 —— 复用共享 `waveform_mark` 控件编辑手工标注。

⚠ 与 `AutoVJ/src/erlib/detail.py` **逐字一致**（双实现纪律，见架构 §6.3）。
  本文件是**薄壳**：只负责「读库 → 注入控件 → 编辑 → 写回 `songs.manual`」，
  波形绘制/点选/拖拽/吸附全在共享模块 `waveform_mark.WaveformMarkWidget` 里。

## 数据流（对齐架构 §4 时序 ②）
  - 打开：`LibReader` 读 `songs(grid)` + `waveform(sid)` + `manual_of(sid)`；
  - 注入：`beatgrid.effective_grid(grid, manual)` 给控件做吸附/拍网；
  - 保存：归一化 `grid_manual`（bpm_manual/anchors/segments）→ `LibWriter.update_manual`。
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from erlib.format import LibReader, LibWriter                       # noqa: E402


class ErlibDetailDialog(QDialog):
    """单曲详情编辑对话框。"""

    def __init__(self, work, sid, root="", parent=None):
        super().__init__(parent)
        self.work = work
        self.sid = int(sid)
        self.root = root or ""
        self._song = {}
        self._grid = {}
        self._auto_bpm = 0.0
        self._build()
        self._load()

    # ---------------- UI ----------------
    def _build(self):
        self.setWindowTitle("歌曲详情")
        self.resize(1080, 640)
        v = QVBoxLayout(self)

        # 顶栏：曲名/艺人/时长/曲风 + 自动 BPM
        top = QHBoxLayout()
        self.lbl_title = QLabel("—")
        self.lbl_title.setStyleSheet("font-weight:bold;font-size:15px;")
        top.addWidget(self.lbl_title)
        top.addStretch(1)
        self.lbl_auto = QLabel("自动 BPM：—")
        top.addWidget(self.lbl_auto)
        v.addLayout(top)

        # 手动 BPM
        form = QFormLayout()
        self.spin_bpm = QDoubleSpinBox()
        self.spin_bpm.setRange(30.0, 300.0)
        self.spin_bpm.setDecimals(1)
        self.spin_bpm.setSingleStep(0.5)
        self.spin_bpm.valueChanged.connect(self._mark_dirty)
        self.chk_manual = QCheckBox("手动覆盖 BPM")
        self.chk_manual.toggled.connect(self._mark_dirty)
        row = QHBoxLayout()
        row.addWidget(self.spin_bpm)
        row.addWidget(self.chk_manual)
        form.addRow("手动 BPM", row)
        v.addLayout(form)

        # 模式工具栏
        bar = QHBoxLayout()
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for key, label in (("select", "选择/拖动"), ("anchor", "标八拍"),
                           ("segment", "标分段"), ("bpm", "标 BPM")):
            b = QPushButton(label)
            b.setCheckable(True)
            self._mode_group.addButton(b)
            bar.addWidget(b)
            if key == "select":
                b.setChecked(True)
            b.clicked.connect(lambda _=False, k=key: self.wave.set_mode(k))
        bar.addStretch(1)
        self.chk_snap = QCheckBox("吸附到拍")
        self.chk_snap.setChecked(True)
        self.chk_snap.toggled.connect(self.wave.set_snap)
        bar.addWidget(self.chk_snap)
        v.addLayout(bar)

        # 主体：左波形 / 右列表
        body = QHBoxLayout()
        from waveform_mark import WaveformMarkWidget
        self.wave = WaveformMarkWidget()
        self.wave.anchorsChanged.connect(self._on_anchors_changed)
        self.wave.segmentsChanged.connect(self._on_segments_changed)
        self.wave.segmentSelected.connect(self._on_segment_selected)
        self.wave.bpmPicked.connect(self._on_bpm_picked)
        body.addWidget(self.wave, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("八拍起点（秒）"))
        self.lst_anchors = QListWidget()
        self.lst_anchors.setMaximumWidth(220)
        right.addWidget(self.lst_anchors, 1)
        self.btn_del_anchor = QPushButton("删除选中八拍")
        self.btn_del_anchor.clicked.connect(self._del_anchor)
        right.addWidget(self.btn_del_anchor)

        right.addWidget(QLabel("BPM 分段"))
        self.lst_segments = QListWidget()
        self.lst_segments.setMaximumWidth(220)
        right.addWidget(self.lst_segments, 1)
        self.btn_del_seg = QPushButton("删除选中分段")
        self.btn_del_seg.clicked.connect(self._del_segment)
        right.addWidget(self.btn_del_seg)
        body.addLayout(right)
        v.addLayout(body, 1)

        # 底部按钮
        bot = QHBoxLayout()
        bot.addStretch(1)
        self.btn_revert = QPushButton("回退自动")
        self.btn_revert.clicked.connect(self._revert)
        bot.addWidget(self.btn_revert)
        self.btn_save = QPushButton("保存")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._save)
        bot.addWidget(self.btn_save)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        bot.addWidget(self.btn_close)
        v.addLayout(bot)

        self._dirty = False
        self._update_title()

    # ---------------- 数据 ----------------
    def _load(self):
        try:
            with LibReader(self.work) as r:
                for s in r.songs():
                    if int(s.get("song_id") or 0) == self.sid:
                        self._song = s
                        break
                self._manual = r.manual_of(self.sid)
                self._wf = r.waveform(self.sid)
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "歌曲详情", "读取工作库失败：%s" % e)
            return

        s = self._song
        g = s.get("grid") or {}
        if not isinstance(g, dict):
            g = {}
        try:
            import beatgrid as _bg
            self._grid = _bg.effective_grid(g, self._manual)
        except Exception:                                        # noqa: BLE001
            self._grid = dict(g)
        self._auto_bpm = float(self._grid.get("bpm") or 0.0)

        title = s.get("title") or s.get("file_name") or ("song %d" % self.sid)
        artist = s.get("artist") or ""
        dur = s.get("duration") or self._grid.get("dur") or 0.0
        try:
            dtext = "%d:%02d" % (int(dur) // 60, int(dur) % 60)
        except (TypeError, ValueError):
            dtext = "—"
        genres = s.get("genres") or []
        if isinstance(genres, str):
            genres = [genres]
        self.lbl_title.setText("%s · %s" % (title, artist) if artist else title)
        src = self._grid.get("src") or "—"
        self.lbl_auto.setText("自动 BPM：%s（%s）· %s"
                              % ((" %.1f" % self._auto_bpm) if self._auto_bpm else "—",
                                 src, dtext) +
                              (" · %s" % " / ".join(str(x) for x in genres[:3])
                               if genres else ""))

        # 手动值
        m = self._manual if isinstance(self._manual, dict) else {}
        bpm_m = m.get("bpm_manual") or 0.0
        try:
            bpm_m = float(bpm_m)
        except (TypeError, ValueError):
            bpm_m = 0.0
        self.chk_manual.setChecked(bpm_m > 0)
        self.spin_bpm.setValue(bpm_m if bpm_m > 0 else (self._auto_bpm or 120.0))

        self.wave.set_waveform(self._wf)
        self.wave.set_grid(self._grid)
        self.wave.set_manual(m)
        self._dirty = False
        self._refresh_lists()
        self._update_title()

    def _effective_manual(self):
        """把当前控件状态归一化成 grid_manual dict。"""
        bpm_m = self.spin_bpm.value() if self.chk_manual.isChecked() else 0.0
        anchors = self.wave.anchors()
        segments = self.wave.segments()
        m = {"bpm_manual": round(float(bpm_m), 3),
             "anchors": [round(a, 4) for a in anchors],
             "segments": segments}
        # 全空 → 视为无手工标注（回退自动）
        if m["bpm_manual"] <= 0 and not m["anchors"] and not m["segments"]:
            return None
        return m

    def _update_title(self):
        suf = " *" if self._dirty else ""
        self.setWindowTitle("歌曲详情%s" % suf)

    def _mark_dirty(self, *_):
        self._dirty = True
        self._update_title()

    # ---------------- 列表 ----------------
    def _refresh_lists(self):
        self.lst_anchors.clear()
        for a in self.wave.anchors():
            self.lst_anchors.addItem("%.2f s" % a)
        self.lst_segments.clear()
        for s in self.wave.segments():
            self.lst_segments.addItem("%.1f s · %.1f BPM"
                                      % (float(s.get("t0") or 0.0),
                                         float(s.get("bpm") or 0.0)))

    def _on_anchors_changed(self):
        self._mark_dirty()
        self._refresh_lists()

    def _on_segments_changed(self):
        self._mark_dirty()
        self._refresh_lists()

    def _del_anchor(self):
        r = self.lst_anchors.currentRow()
        if r < 0:
            return
        anchors = self.wave.anchors()
        if 0 <= r < len(anchors):
            anchors.pop(r)
            self.wave.set_anchors(anchors)
            self._mark_dirty()
            self._refresh_lists()

    def _del_segment(self):
        r = self.lst_segments.currentRow()
        if r < 0:
            return
        segs = self.wave.segments()
        if 0 <= r < len(segs):
            segs.pop(r)
            # 删掉首段后，把下一段顶到 0（保持首尾相接覆盖全曲）
            if segs:
                segs[0]["t0"] = 0.0
            self.wave.set_segments(segs)
            self._mark_dirty()
            self._refresh_lists()

    # ---------------- 交互 ----------------
    def _on_segment_selected(self, t0, t1):
        bpm, ok = QInputDialog.getDouble(
            self, "分段 BPM", "该段（%.1fs ~ %.1fs）的 BPM：" % (t0, t1),
            self._auto_bpm or 128.0, 30.0, 300.0, 1)
        if not ok:
            return
        self.wave.add_segment(t0, t1, bpm, default_bpm=self._auto_bpm)
        self._mark_dirty()
        self._refresh_lists()

    def _on_bpm_picked(self, sec):
        # 「标 BPM」点选：以该点所在网格 BPM 回填（与直接手输等价）。
        bpm = self._auto_bpm
        if bpm > 0:
            self.chk_manual.setChecked(True)
            self.spin_bpm.setValue(bpm)
            self._mark_dirty()

    # ---------------- 保存 / 回退 ----------------
    def _save(self):
        manual = self._effective_manual()
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
            try:
                n = w.update_manual(self.sid, manual)
            finally:
                w.close()
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "歌曲详情", "保存失败：%s" % e)
            return
        if n:
            self._manual = manual
            self._dirty = False
            self._update_title()
            QMessageBox.information(self, "歌曲详情", "已保存手工标注。")
        else:
            QMessageBox.warning(self, "歌曲详情", "保存失败（找不到这首歌）。")

    def _revert(self):
        manual = None
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
            try:
                w.update_manual(self.sid, manual)
            finally:
                w.close()
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "歌曲详情", "回退失败：%s" % e)
            return
        self._load()

    def closeEvent(self, ev):
        if self._dirty:
            ret = QMessageBox.question(
                self, "歌曲详情", "有未保存的修改，仍要关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        super().closeEvent(ev)
