# -*- coding: utf-8 -*-
r"""ERAVJ 演出端 · 歌曲详情页对话框 —— 复用共享 `waveform_mark.WaveformMarkWidget`。

数据源是演出端本地曲库的 `cfg["music_meta"][path]`（**不是** erlib 的工作库）：
  - 读：`grid`（算法网格）+ `grid_manual`（用户手工标注）+ 波形（懒加载）；
  - 写：归一化 `grid_manual`（bpm_manual / anchors / segments）→ 存回
    `music_meta[path]["grid_manual"]` → `cfg.save()`。

## 波形
ERAVJ 的曲库扫描**不**把波形写进 `music_meta`（扫描线程不加重活）。本对话框打开时：
  1) 先查本地 `.wj` 缓存（`waveform.waveform_load(path)`，键=sha1(路径|mtime|size|v|cps)）；
  2) 没有则懒解码一次算包络（`waveform.waveform_from_signal`）并写回 `.wj` 缓存。
解码用 `erlib.scanner.decode`（soundfile，原始采样率、全曲），与 ERLIB 建库端**同一口径**
（架构 §6.4：同一段 PCM 产出逐字节相同的 amp 数组）。

## 与 erlib/detail.py 的关系
结构同源（顶栏/手动 BPM/模式工具栏/波形/左右列表/保存），但**存储后端不同**
（这里是 `cfg["music_meta"]`，erlib 是 `.eravjlib` 的 `songs.manual`）。
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QMessageBox, QPushButton,
                               QVBoxLayout)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _lazy_waveform(path):
    """取波形：先查本地 `.wj` 缓存，缺失则**懒解码算一次**并写回缓存。失败返回 None。"""
    try:
        import waveform as _wf
    except Exception:                                            # noqa: BLE001
        return None
    try:
        wf = _wf.waveform_load(path)
        if wf:
            return wf
    except Exception:                                            # noqa: BLE001
        pass
    try:
        from erlib.scanner import decode
        sig, sr, _dur = decode(path)
        wf = _wf.waveform_from_signal(sig, sr)
        if wf:
            try:
                _wf.waveform_save(path, wf)
            except Exception:                                    # noqa: BLE001
                pass
        return wf
    except Exception:                                            # noqa: BLE001
        return None


class MusicDetailDialog(QDialog):
    """演出端单曲详情编辑对话框（读写 `music_meta[path]["grid_manual"]`）。"""

    def __init__(self, cfg, path, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.path = str(path)
        self._meta = {}
        self._grid = {}
        self._manual = None
        self._wf = None
        self._auto_bpm = 0.0
        # ---- 试听播放（懒创建：只有点了 ▶ 才去探测声卡 / 解码）----
        self._player = None
        self._tick = None
        self._playing = False
        self._pending_play = False
        self._build()
        self._load()

    # ---------------- UI ----------------
    def _build(self):
        self.setWindowTitle("歌曲详情")
        self.resize(1080, 640)
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.lbl_title = QLabel("—")
        self.lbl_title.setStyleSheet("font-weight:bold;font-size:15px;")
        top.addWidget(self.lbl_title)
        top.addStretch(1)
        self.lbl_auto = QLabel("自动 BPM：—")
        top.addWidget(self.lbl_auto)
        v.addLayout(top)

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

        # ★ self.wave 必须**先创建**（工具栏 lambda / 吸附 checkbox 在 build 期就引用它）。
        from waveform_mark import WaveformMarkWidget
        self.wave = WaveformMarkWidget()
        self.wave.anchorsChanged.connect(self._on_anchors_changed)
        self.wave.segmentsChanged.connect(self._on_segments_changed)
        self.wave.segmentSelected.connect(self._on_segment_selected)
        self.wave.bpmPicked.connect(self._on_bpm_picked)
        self.wave.playheadMoved.connect(self._on_playhead_moved)

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
        # ★ 试听控制（用户要求）：播放起点 = 波形上的播放头竖线位置。
        self.btn_play = QPushButton("▶ 播放")
        self.btn_play.setToolTip("从播放头竖线所在位置开始播放")
        self.btn_play.clicked.connect(self._on_play)
        bar.addWidget(self.btn_play)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.clicked.connect(self._on_pause)
        bar.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.clicked.connect(self._on_stop)
        bar.addWidget(self.btn_stop)
        self.lbl_pos = QLabel("位置 0.00s")
        self.lbl_pos.setMinimumWidth(150)
        bar.addWidget(self.lbl_pos)
        self.chk_snap = QCheckBox("吸附到拍")
        self.chk_snap.setChecked(True)
        self.chk_snap.toggled.connect(self.wave.set_snap)
        bar.addWidget(self.chk_snap)
        v.addLayout(bar)

        body = QHBoxLayout()
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
        self._shutdown_audio()             # 重新读数据（含「回退自动」）先停掉试听
        try:
            self._meta = (self.cfg["music_meta"] or {}).get(self.path) or {}
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "歌曲详情", "读取曲目数据失败：%s" % e)
            return
        if not isinstance(self._meta, dict):
            self._meta = {}
        m = self._meta
        g = m.get("grid") if isinstance(m.get("grid"), dict) else {}
        manual = m.get("grid_manual") if isinstance(m.get("grid_manual"), dict) else None
        self._manual = manual
        try:
            import beatgrid as _bg
            self._grid = _bg.effective_grid(g, manual)
        except Exception:                                        # noqa: BLE001
            self._grid = dict(g)
        self._auto_bpm = float(self._grid.get("bpm") or 0.0)

        title = m.get("title") or os.path.basename(self.path)
        artist = m.get("artist") or ""
        dur = 0.0
        for _src in (m.get("duration"), self._grid.get("dur"),
                     (self._wf or {}).get("dur")):
            try:
                if _src:
                    dur = float(_src)
                    break
            except (TypeError, ValueError):
                continue
        try:
            dtext = "%d:%02d" % (int(dur) // 60, int(dur) % 60)
        except (TypeError, ValueError):
            dtext = "—"
        genres = m.get("genres") or []
        if isinstance(genres, str):
            genres = [genres]
        self.lbl_title.setText("%s · %s" % (title, artist) if artist else title)

        # 波形（懒加载；可能为空 → 控件显示"无波形"背景，仍可标秒）
        self._wf = _lazy_waveform(self.path)
        if self._wf:
            try:
                dur = float(self._wf.get("dur") or dur)
                dtext = "%d:%02d" % (int(dur) // 60, int(dur) % 60)
            except (TypeError, ValueError):
                pass
        src = self._grid.get("src") or "—"
        self.lbl_auto.setText("自动 BPM：%s（%s）· %s"
                              % ((" %.1f" % self._auto_bpm) if self._auto_bpm else "—",
                                 src, dtext) +
                              (" · %s" % " / ".join(str(x) for x in genres[:3])
                               if genres else "") +
                              ("" if self._wf else " · 无波形"))

        mm = manual or {}
        bpm_m = mm.get("bpm_manual") or 0.0
        try:
            bpm_m = float(bpm_m)
        except (TypeError, ValueError):
            bpm_m = 0.0
        self.chk_manual.setChecked(bpm_m > 0)
        self.spin_bpm.setValue(bpm_m if bpm_m > 0 else (self._auto_bpm or 120.0))

        self.wave.set_waveform(self._wf)
        self.wave.set_grid(self._grid)
        self.wave.set_manual(mm)
        self.wave.set_playhead(0.0)
        self._dirty = False
        self._refresh_lists()
        self._update_title()

    def _effective_manual(self):
        """把当前控件状态归一化成 grid_manual dict；全空 → None（回退自动）。"""
        bpm_m = self.spin_bpm.value() if self.chk_manual.isChecked() else 0.0
        anchors = self.wave.anchors()
        segments = self.wave.segments()
        m = {"bpm_manual": round(float(bpm_m), 3),
             "anchors": [round(a, 4) for a in anchors],
             "segments": segments}
        if m["bpm_manual"] <= 0 and not m["anchors"] and not m["segments"]:
            return None
        return m

    def _update_title(self):
        self.setWindowTitle("歌曲详情%s" % (" *" if self._dirty else ""))

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
        bpm = self._auto_bpm
        if bpm > 0:
            self.chk_manual.setChecked(True)
            self.spin_bpm.setValue(bpm)
            self._mark_dirty()

    # ---------------- 试听播放（起点 = 播放头竖线） ----------------
    def _ensure_player(self):
        """懒创建播放器 + 50ms 轮询定时器（没声卡也绝不抛异常）。"""
        if self._player is not None:
            return self._player
        try:
            from audio_playback import AudioPlayer
        except Exception as e:                                   # noqa: BLE001
            self.lbl_pos.setText("无法播放：音频模块不可用（%s）" % e)
            return None
        try:
            self._player = AudioPlayer()
        except Exception as e:                                   # noqa: BLE001
            self.lbl_pos.setText("无法播放：%s" % e)
            return None
        self._tick = QTimer(self)
        self._tick.setInterval(50)                               # 20fps：够跟手，开销可忽略
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        return self._player

    def _on_playhead_moved(self, sec):
        """拖动播放头 → 记位置；正在播放就立即 seek 过去。"""
        try:
            self.lbl_pos.setText("位置 %.2fs" % float(sec))
        except (TypeError, ValueError):
            pass
        if self._player is not None and self._player.loaded:
            self._player.seek(sec)

    def _on_play(self):
        """▶：从播放头竖线所在秒起播（先 seek 再播）。"""
        p = self._ensure_player()
        if p is None:
            return
        if p.is_playing():
            return
        if not p.available:
            self.lbl_pos.setText("无法播放：%s" % (p.error or "没有可用的音频输出设备"))
            return
        pos = float(self.wave.playhead_sec())
        # 未装载 / 装的是别的歌 → 后台解码，`_on_tick` 里就绪后自动起播
        if not (p.loaded and p.loaded_path == self.path):
            self._pending_play = True
            self.lbl_pos.setText("准备音频…（从 %.2fs 起播）" % pos)
            if not p.load(self.path):
                self._pending_play = False
                self.lbl_pos.setText("无法播放：%s" % (p.error or "音频文件不可用"))
            return
        self._pending_play = False
        if p.play(pos):
            self._playing = True
            self.btn_play.setText("▶ 播放中")
            self.lbl_pos.setText("播放中（从 %.2fs）" % pos)
        else:
            self.lbl_pos.setText("无法播放：%s" % (p.error or "未知原因"))

    def _on_pause(self):
        p = self._player
        if p is None:
            return
        if p.is_playing():
            p.pause()
        self._playing = False
        self._pending_play = False
        self.btn_play.setText("▶ 播放")
        pos = p.position_sec()
        self.wave.set_playhead(pos)          # 暂停后竖线留在原位
        self.lbl_pos.setText("已暂停 %.2fs" % pos)

    def _on_stop(self):
        p = self._player
        if p is not None:
            p.stop()
        self._playing = False
        self._pending_play = False
        self.btn_play.setText("▶ 播放")
        self.wave.set_playhead(0.0)
        self.lbl_pos.setText("位置 0.00s")

    def _on_tick(self):
        """播放中：竖线跟着音频走；就绪后自动补起播；播完复位按钮。"""
        p = self._player
        if p is None:
            return
        if self._pending_play and p.loaded and p.loaded_path == self.path:
            self._pending_play = False
            pos = float(self.wave.playhead_sec())
            if p.play(pos):
                self._playing = True
                self.btn_play.setText("▶ 播放中")
                self.lbl_pos.setText("播放中（从 %.2fs）" % pos)
            else:
                self.lbl_pos.setText("无法播放：%s" % (p.error or "未知原因"))
                return
        if self._playing and p.is_playing():
            pos = p.position_sec()
            self.wave.set_playhead(pos)
            self.lbl_pos.setText("位置 %.2fs / %.2fs" % (pos, p.duration_sec))
        elif self._playing and not p.is_playing():
            # 自然播完（或声卡中途出错）
            self._playing = False
            self.btn_play.setText("▶ 播放")
            pos = p.position_sec()
            self.wave.set_playhead(pos)
            self.lbl_pos.setText("播放结束 %.2fs%s"
                                 % (pos, ("（%s）" % p.error) if p.error else ""))

    def _shutdown_audio(self):
        """关详情页必调：停播 + 停定时器 + 释放声卡流（绝不留后台线程）。"""
        self._playing = False
        self._pending_play = False
        t = self._tick
        self._tick = None
        if t is not None:
            try:
                t.stop()
                t.deleteLater()
            except Exception:                                    # noqa: BLE001
                pass
        p = self._player
        self._player = None
        if p is not None:
            try:
                p.stop()
            except Exception:                                    # noqa: BLE001
                pass
            try:
                p.close()
            except Exception:                                    # noqa: BLE001
                pass

    # ---------------- 保存 / 回退 ----------------
    def _write_manual(self, manual):
        """把 grid_manual 写回 music_meta[path] 并落盘。成功返回 True。"""
        try:
            meta = self.cfg["music_meta"]
            m = meta.get(self.path)
            if not isinstance(m, dict):
                m = {}
                meta[self.path] = m
            if manual:
                m["grid_manual"] = manual
            else:
                m.pop("grid_manual", None)
            self.cfg.save()
            return True
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "歌曲详情", "保存失败：%s" % e)
            return False

    def _save(self):
        manual = self._effective_manual()
        if not self._write_manual(manual):
            return
        self._manual = manual
        if manual:
            self._meta["grid_manual"] = manual
        else:
            self._meta.pop("grid_manual", None)
        self._dirty = False
        self._update_title()
        QMessageBox.information(self, "歌曲详情", "已保存手工标注。")

    def _revert(self):
        if not self._write_manual(None):
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
        self._shutdown_audio()
        super().closeEvent(ev)

    def done(self, r):                     # noqa: D401 - Qt 钩子
        """任何关闭路径（X / 关闭按钮 / ESC）都要停播 + 释放声卡流。"""
        self._shutdown_audio()
        super().done(r)
