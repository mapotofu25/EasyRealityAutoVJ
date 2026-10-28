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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QFrame, QHBoxLayout, QInputDialog, QLabel, QMenu,
                               QMessageBox, QPushButton, QVBoxLayout)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _spin_silent(spin, val):
    """静默设置 `QDoubleSpinBox`（不触发 valueChanged）。

    ⚠ 必须 `blockSignals`：否则 `_load` 回填 BPM 时会走 `_on_bpm_edited`，
      把「刚读进来的手动值」当成「用户刚改的」，立刻标脏并弹提示。
    """
    try:
        spin.blockSignals(True)
        spin.setValue(float(val))
    except (TypeError, ValueError):
        pass
    finally:
        try:
            spin.blockSignals(False)
        except Exception:                                        # noqa: BLE001
            pass


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


def info_text(cfg, path):
    """曲目信息文本（**唯一真源**）。

    原来「此曲目信息」弹窗在 `ui_main.show_music_info()` 里拼，详情页另有一套；
    2026-10-28 用户要求把两个菜单项合并 ⇒ 文本收口到这里，两边都调它。
    """
    import os as _os
    # ⚠ `Config` 没有 `.get()`（全项目一律 `cfg[k]`）；dict 也支持 `[]` ⇒ 统一走下标
    try:
        _mm = cfg["music_meta"] or {}
    except Exception:                                            # noqa: BLE001
        _mm = {}
    m = _mm.get(path) or {}
    dur = "未知"
    sr_txt = ""
    try:
        import soundfile as _sf
        info = _sf.info(path)
        dur = "%.0f 秒" % info.duration
        sr_txt = "%d Hz · %d 声道" % (info.samplerate, info.channels)
    except Exception:                                            # noqa: BLE001
        pass
    genres = "、".join(m.get("genres") or []) or "未分析"
    return (
        "文件：%s\n"
        "歌名：%s\n"
        "艺人：%s\n"
        "曲风：%s\n"
        "曲风来源：%s\n"
        "时长：%s\n"
        "采样率：%s\n"
        "指纹ID：%s\n"
        "\n完整路径：\n%s"
        % (_os.path.basename(path), m.get("title") or "未知", m.get("artist") or "未知",
           genres, m.get("source") or "未分析", dur, sr_txt or "未知",
           m.get("song_id") or "-", path)
    )


class MusicDetailDialog(QDialog):
    """演出端单曲详情编辑对话框（读写 `music_meta[path]["grid_manual"]`）。"""

    def __init__(self, cfg, path, parent=None, info_text=""):
        # ★ 2026-10-28（用户要求合并「此曲目信息」+「歌曲详情」）：
        #   `info_text` 非空时，在顶部显示只读的曲目信息（文件/歌名/艺人/曲风/时长/采样率/
        #   指纹 ID/完整路径）。内容由调用方用 `music_info_text()` 生成，**只有一份真源**，
        #   避免"信息框"和"详情页"各写一遍然后走样。
        super().__init__(parent)
        self.cfg = cfg
        self.path = str(path)
        self._info_text = str(info_text or "")
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

        # 曲目信息（只读、可选中复制）——原来独立的「此曲目信息」菜单项的内容并到这里
        if self._info_text:
            box = QFrame()
            box.setFrameShape(QFrame.StyledPanel)
            bl = QVBoxLayout(box)
            bl.setContentsMargins(8, 6, 8, 6)
            lab = QLabel(self._info_text)
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lab.setStyleSheet("color:palette(mid);")
            bl.addWidget(lab)
            v.addWidget(box)

        # ★ 2026-10-28（用户要求「去掉手动覆盖 BPM 这个选项 改了就是手动」）：
        #   不再有勾选框 —— 值一旦与自动值不同就视为手动（见 `_effective_manual`）。
        form = QFormLayout()
        self.spin_bpm = QDoubleSpinBox()
        self.spin_bpm.setRange(30.0, 300.0)
        self.spin_bpm.setDecimals(1)
        self.spin_bpm.setSingleStep(0.5)
        self.spin_bpm.setToolTip("改动这个值即为手动覆盖（与自动值相同则仍走自动）")
        self.spin_bpm.valueChanged.connect(self._on_bpm_edited)
        form.addRow("BPM", self.spin_bpm)
        v.addLayout(form)

        # ★ self.wave 必须**先创建**（工具栏 lambda / 吸附 checkbox 在 build 期就引用它）。
        from waveform_mark import WaveformMarkWidget
        self.wave = WaveformMarkWidget()
        self.wave.segmentsChanged.connect(self._on_segments_changed)
        self.wave.playheadMoved.connect(self._on_playhead_moved)
        self.wave.contextMenuRequested.connect(self._on_wave_context_menu)
        # 操作提示（用户点名要 ERCUE 的键位：Ctrl+滚轮缩放）
        self.wave.setToolTip("Ctrl+滚轮 缩放 · Shift+滚轮 / 普通滚轮 平移 · "
                             "拖动可移动播放头（播放从这里开始） · 右键编辑 BPM 分段")

        # ★ 2026-10-28（用户要求）：
        #   · 去掉「选择/拖动 · 标八拍 · 标分段 · 标 BPM」模式按钮 —— 固定为选择/拖动；
        #   · 播放 / 暂停 / 停止 三个按钮合成**一个切换键**。
        bar = QHBoxLayout()
        self.wave.set_mode("select")
        self.btn_play = QPushButton("▶ 播放")
        self.btn_play.setToolTip("从播放头竖线所在位置开始播放；播放中再点就是暂停")
        self.btn_play.clicked.connect(self._on_play_toggle)
        bar.addWidget(self.btn_play)
        self.lbl_pos = QLabel("位置 0.00s")
        self.lbl_pos.setMinimumWidth(150)
        bar.addWidget(self.lbl_pos)
        bar.addStretch(1)
        self.chk_snap = QCheckBox("吸附到拍")
        self.chk_snap.setChecked(True)
        self.chk_snap.toggled.connect(self.wave.set_snap)
        bar.addWidget(self.chk_snap)
        v.addLayout(bar)

        # ★ 2026-10-28：右侧「八拍起点 / BPM 分段」整块去掉 —— 波形占满宽度；
        #   多 BPM 分段的编辑改到**波形右键菜单**（见 `_on_wave_context_menu`）。
        body = QHBoxLayout()
        body.addWidget(self.wave, 1)
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
        # ★ 无勾选框了：手动值 > 0 就显示手动值，否则显示自动值。
        _spin_silent(self.spin_bpm, bpm_m if bpm_m > 0 else (self._auto_bpm or 120.0))
        self._bpm_auto_shown = float(self._auto_bpm or 0.0)

        self.wave.set_waveform(self._wf)
        self.wave.set_grid(self._grid)
        self.wave.set_manual(mm)
        # ★ 2026-10-28：自动分析出的多 BPM 段要显示出来（用户手动分段优先）。
        #   只做「显示」，不写进 grid_manual —— 见 `_effective_manual` 里的同源判断。
        if not mm.get("segments") and (self._grid or {}).get("segments"):
            self.wave.set_segments(self._grid["segments"])
        self.wave.set_playhead(0.0)
        self._dirty = False
        self._refresh_lists()
        self._update_title()

    def _effective_manual(self):
        """把当前控件状态归一化成 grid_manual dict；全空 → None（回退自动）。

        ★ 2026-10-28：没有「手动覆盖」勾选框了 —— **BPM 与自动值不同即为手动**。
          否则每次打开详情页都会把手动值写死一遍，之后重扫也不更新。
        """
        try:
            bpm_now = float(self.spin_bpm.value())
        except (TypeError, ValueError):
            bpm_now = 0.0
        auto = float(getattr(self, "_bpm_auto_shown", 0.0) or 0.0)
        bpm_m = bpm_now if (bpm_now > 0 and (auto <= 0 or abs(bpm_now - auto) > 0.05)) else 0.0
        anchors = self.wave.anchors()
        segments = self.wave.segments()
        # ★★ 2026-10-28：自动分段「只显示、不落成手动」。
        #   波形控件只有一份 `_segments`，自动结果也画在那里；若原样写进 grid_manual，
        #   `effective_grid` 就会走 manual 分支（src="manual"），之后重扫也不更新。
        #   判据：与自动结果等价 ⇒ 视为「用户没改过」⇒ 不写。
        auto = (self._grid or {}).get("segments") or []
        if auto and self._segments_equal(segments, auto):
            segments = []
        m = {"bpm_manual": round(float(bpm_m), 3),
             "anchors": [round(a, 4) for a in anchors],
             "segments": segments}
        if m["bpm_manual"] <= 0 and not m["anchors"] and not m["segments"]:
            return None
        return m

    @staticmethod
    def _segments_equal(a, b, tol=0.01):
        """两串分段是否等价（按 t0+bpm 比，容差 `tol` 秒 / `tol`*bpm）。"""
        try:
            if len(a) != len(b):
                return False
            for x, y in zip(a, b):
                if abs(float(x.get("t0") or 0.0) - float(y.get("t0") or 0.0)) > tol:
                    return False
                bx, by = float(x.get("bpm") or 0.0), float(y.get("bpm") or 0.0)
                if abs(bx - by) > max(0.05, tol * max(bx, by)):
                    return False
            return True
        except (TypeError, ValueError, AttributeError):
            return False

    def _update_title(self):
        self.setWindowTitle("歌曲详情%s" % (" *" if self._dirty else ""))

    def _mark_dirty(self, *_):
        self._dirty = True
        self._update_title()

    # ---------------- 列表（2026-10-28：右侧列表已删，保留空壳供旧调用点） ----------------
    def _refresh_lists(self):
        return

    def _beat_len_now(self):
        """当前有效拍长（秒）。优先 effective grid，其次手动 BPM。"""
        try:
            bl = float((self._grid or {}).get("beat_len") or 0.0)
            if bl > 0:
                return bl
        except Exception:                                        # noqa: BLE001
            pass
        try:
            b = float((self._grid or {}).get("bpm") or 0.0)
            return 60.0 / b if b > 0 else 0.0
        except Exception:                                        # noqa: BLE001
            return 0.0

    def _refresh_grid_preview(self):
        """把当前手动状态重新合成「有效网格」并推给波形（改完立刻能看到线移动）。"""
        try:
            import beatgrid as _bg
            m = self._effective_manual()
            self._grid = _bg.effective_grid((self._meta or {}).get("grid") or {}, m)
            self.wave.set_grid(self._grid)
        except Exception:                                        # noqa: BLE001
            pass

    # ★ 2026-10-28：`_nudge_beats` / `_scale_bpm` / `_on_anchors_changed` /
    #   `_del_anchor` / `_del_segment` 随右侧面板一起去掉
    #   （用户要求去掉手工八拍与八拍起点；±1 小节/±1 拍/÷2/×2 改为右键菜单里的
    #   「切分到此处」与「整段 ÷2 / ×2」）。
    def _on_segments_changed(self):
        self._mark_dirty()

    # ---------------- 交互：波形右键 → BPM 分段 ----------------
    def _segments_now(self):
        """当前生效的分段（手动优先，否则自动）；空列表 ⇒ 单段模式。"""
        segs = [dict(x) for x in (self.wave.segments() or []) if isinstance(x, dict)]
        if segs:
            return segs
        auto = (self._grid or {}).get("segments") or []
        return [dict(x) for x in auto if isinstance(x, dict)]

    def _seg_at(self, sec):
        """返回 (索引, 段) —— `sec` 落在哪一段（按 t0 升序）。"""
        segs = self._segments_now()
        idx = 0
        for i, s in enumerate(segs):
            try:
                if float(s.get("t0") or 0.0) <= sec:
                    idx = i
            except (TypeError, ValueError):
                pass
        if not segs:
            return 0, {"t0": 0.0, "bpm": float(self.spin_bpm.value() or 0.0)}
        return idx, segs[idx]

    def _apply_segments(self, segs):
        """写回分段并立刻刷新预览（首段强制从 0 开始、按 t0 升序）。"""
        segs = [dict(x) for x in segs if isinstance(x, dict)]
        segs.sort(key=lambda x: float(x.get("t0") or 0.0))
        if segs:
            segs[0]["t0"] = 0.0
        for x in segs:
            try:
                b = float(x.get("bpm") or 0.0)
            except (TypeError, ValueError):
                b = 0.0
            if b > 0:
                x["beat_len"] = round(60.0 / b, 6)
        self.wave.set_segments(segs)
        self._refresh_grid_preview()
        self._mark_dirty()

    def _build_seg_menu(self, sec):
        """构造右键菜单（**只构造不弹出** —— 便于单测；`_on_wave_context_menu` 负责 exec）。

        返回 `(menu, {"cut":act, "edit":act, "del":act, "half":act, "dbl":act,
        "clear":act}, idx, segs, cur_bpm)`。
        """
        segs = self._segments_now()
        idx, cur = self._seg_at(sec)
        try:
            cur_bpm = float(cur.get("bpm") or 0.0)
        except (TypeError, ValueError):
            cur_bpm = 0.0
        if cur_bpm <= 0:
            cur_bpm = float(self.spin_bpm.value() or 0.0)

        menu = QMenu(self)
        acts = {}
        acts["cut"] = menu.addAction("在 %.2fs 处切分（输入该段 BPM）…" % sec)
        acts["edit"] = menu.addAction("修改本段 BPM（当前 %.1f）…" % cur_bpm)
        acts["del"] = menu.addAction("删除本段（%.2fs 起，%.1f BPM）"
                                     % (float(cur.get("t0") or 0.0), cur_bpm))
        menu.addSeparator()
        acts["half"] = menu.addAction("整段 ÷2 BPM（半速歧义）")
        acts["dbl"] = menu.addAction("整段 ×2 BPM（倍速歧义）")
        menu.addSeparator()
        acts["clear"] = menu.addAction("清除全部分段（回到自动网格）")
        # 首段不能删（网格必须从 0 开始）；单段也没有「删除本段」的意义
        acts["del"].setEnabled(len(segs) >= 2 and idx > 0)
        acts["edit"].setEnabled(bool(segs) or cur_bpm > 0)
        acts["clear"].setEnabled(len(segs) >= 2 or bool(self.wave.segments()))
        return menu, acts, idx, segs, cur_bpm

    def _run_seg_action(self, key, idx, segs, cur_bpm, sec):
        """执行右键菜单里的某一项（`key` 见 `_build_seg_menu` 的 acts）。"""
        if key == "cut":
            if sec < 1.0:
                QMessageBox.information(self, "切分分段",
                                        "切分点太靠前（< 1 秒），网格必须从 0 开始。")
                return False
            bpm, ok = QInputDialog.getDouble(
                self, "切分分段",
                "从 %.2fs 起改用哪个 BPM？（该点之前那段保持不变）" % sec,
                cur_bpm or 128.0, 30.0, 300.0, 1)
            if not ok:
                return False
            # ★ 单段（`segs` 为空）时必须先补出「首段」，
            #   否则新段会被 `_apply_segments` 的首段归一化改成 t0=0 ⇒ 等于没切。
            base = [dict(x) for x in segs] or [{"t0": 0.0, "bpm": cur_bpm}]
            new = [x for x in base if float(x.get("t0") or 0.0) < sec - 1e-6]
            new.append({"t0": sec, "bpm": float(bpm)})
            self._apply_segments(new)
            return True
        if key == "edit":
            bpm, ok = QInputDialog.getDouble(
                self, "修改本段 BPM", "本段 BPM：", cur_bpm or 128.0, 30.0, 300.0, 1)
            if not ok:
                return False
            if segs:
                segs[idx]["bpm"] = float(bpm)
                self._apply_segments(segs)
            else:
                self._spin_set(bpm)
                self._refresh_grid_preview()
                self._mark_dirty()
            return True
        if key == "del":
            if len(segs) >= 2 and idx > 0:
                segs.pop(idx)
                self._apply_segments(segs)
                return True
            return False
        if key in ("half", "dbl"):
            f = 0.5 if key == "half" else 2.0
            if segs:
                for x in segs:
                    try:
                        b = float(x.get("bpm") or 0.0)
                    except (TypeError, ValueError):
                        b = 0.0
                    if b > 0:
                        x["bpm"] = max(30.0, min(300.0, b * f))
                self._apply_segments(segs)
            else:
                self._spin_set(max(30.0, min(300.0, cur_bpm * f)))
                self._refresh_grid_preview()
                self._mark_dirty()
            return True
        if key == "clear":
            self.wave.set_segments([])
            self._refresh_grid_preview()
            self._mark_dirty()
            return True
        return False

    def _on_wave_context_menu(self, sec, gpos):
        """波形右键：多 BPM 段落的全部编辑入口（用户 2026-10-28 指定）。"""
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            sec = 0.0
        menu, acts, idx, segs, cur_bpm = self._build_seg_menu(sec)
        act = menu.exec(gpos)
        if act is None:
            return
        for k, a in acts.items():
            if a is act:
                self._run_seg_action(k, idx, segs, cur_bpm, sec)
                return

    # ---------------- BPM 输入框（改了就是手动） ----------------
    def _spin_set(self, val):
        """静默设置 BPM（不触发 `_on_bpm_edited` 的脏标记链）。"""
        _spin_silent(self.spin_bpm, val)

    def _on_bpm_edited(self, _v=None):
        """BPM 一改就是手动：立刻重算网格预览（无勾选框）。"""
        self._refresh_grid_preview()
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

    def _on_play_toggle(self):
        """一个键管播放/暂停（用户 2026-10-28 要求三键合一）。"""
        p = self._player
        if self._playing and p is not None and p.is_playing():
            p.pause()
            self._playing = False
            self._pending_play = False
            self.btn_play.setText("▶ 播放")
            pos = p.position_sec()
            self.wave.set_playhead(pos)      # 暂停后竖线留在原位
            self.lbl_pos.setText("已暂停 %.2fs" % pos)
            return
        self._on_play()

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
