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

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox,
                               QFormLayout, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QMenu, QMessageBox, QPushButton,
                               QVBoxLayout)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import sections as S                                        # noqa: E402
from section_ui import EnergySectionDialog                   # noqa: E402  能量段落编辑框


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


class _ResegmentWorker(QThread):
    """后台解码 + 重算能量段落，避免详情页在 UI 线程卡死。

    只做纯计算：`soundfile` 读全曲 → 重采样 → 特征 → `sections.auto_segments`。
    结果通过信号回主线程，由对话框负责落盘和刷新控件；worker 不碰 `cfg` / Qt 控件。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, path, bpm, anchor, parent=None):
        super().__init__(parent)
        self._path = str(path)
        self._bpm = float(bpm)
        self._anchor = float(anchor)

    def run(self):                                            # noqa: D102 - QThread 入口
        try:
            import numpy as np
            import beatgrid as _bg
            import soundfile as sf

            if self.isInterruptionRequested():
                return
            data, sr = sf.read(self._path, dtype="float32", always_2d=True)
            if self.isInterruptionRequested():
                return
            sig = np.mean(data, axis=1)
            del data
            m2, s2 = _bg.resample(sig, sr, _bg.SR_TARGET)
            del sig
            if self.isInterruptionRequested():
                return
            f = _bg.features(m2, s2)
            dur = len(m2) / float(s2)
            grid = {"anchor": self._anchor,
                    "beat_len": round(60.0 / self._bpm, 6),
                    "bpm": self._bpm}
            got = S.auto_segments(f, dur=dur, grid=grid)
            # ★ 与 ERLIB `erlib/detail.py::_resegment_by_bpm` 保持一致：
            #   这里**不**把 `take_folded_anchor()` 写回局部 grid，否则同一首歌
            #   在「按 BPM 重新分段」后 raw 的八拍相位会与 ERLIB 不同。
            raw = S.auto_segments(f, dur=dur, grid=None, phrase_grid=grid)
            del m2, f
        except Exception as e:                                # noqa: BLE001
            if not self.isInterruptionRequested():
                self.failed.emit("%s: %s" % (type(e).__name__, e))
            return
        if not self.isInterruptionRequested():
            self.done.emit({"bpm": self._bpm, "anchor": self._anchor,
                            "got": got, "raw": raw})


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
    # ★ 2026-10-30 用户「大砍刀」：曲风识别删除 ⇒ 信息块不再显示「曲风 / 曲风来源」。
    return (
        "文件：%s\n"
        "歌名：%s\n"
        "艺人：%s\n"
        "时长：%s\n"
        "采样率：%s\n"
        "指纹ID：%s\n"
        "\n完整路径：\n%s"
        % (_os.path.basename(path), m.get("title") or "未知", m.get("artist") or "未知",
           dur, sr_txt or "未知", m.get("song_id") or "-", path)
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
        self._resec_worker = None
        self._resec_close_pending = False
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
        # ★ 2026-10-30 用户：「把这个吸附先去掉吧 我没搞明白有什么用」+「（重新分段）
        #   放到原来吸附的位置」。⇒ 原来「吸附：拍 / 小节」的位置现在放
        #   「按 BPM 重新分段」按钮。
        #   为什么吸附可以删：它只影响**手动**拖段落/锚点的落点，自动分段和播放光标
        #   都不受它影响（用户一直以为跟光标有关）；而波形控件的默认吸附粒度本来就是
        #   1 拍（`waveform_mark._snap_beats=1`），删掉 UI 后手动编辑的行为与原来
        #   "拍"选中时**完全一致**。
        self.btn_resec = QPushButton("按 BPM 重新分段")
        self.btn_resec.setToolTip(
            "用上面 BPM 框里的值重算**能量段落**（前奏/中段/drop/尾奏）。\n"
            "只重算这一首，不重扫曲库；会同时把这个 BPM 记为手动值。\n"
            "自动 BPM 差半拍、或半速/倍速判错导致分段碎掉时点它。")
        self.btn_resec.clicked.connect(lambda: self._resegment_by_bpm(ask=True))
        bar.addWidget(self.btn_resec)
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
        self._grid_auto = dict(g)
        manual = m.get("grid_manual") if isinstance(m.get("grid_manual"), dict) else None
        self._manual = manual
        try:
            import beatgrid as _bg
            self._grid = _bg.effective_grid(g, manual, m)
        except Exception:                                        # noqa: BLE001
            self._grid = dict(g)
        # ★ 2026-11-18：这里必须是**自动网格的 BPM**（g），不是 effective_grid
        #   的 BPM；否则手动分段会把“自动 BPM”标签显示成手动首段 BPM。
        self._auto_bpm = float(g.get("bpm") or 0.0)

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
                              ("" if self._wf else " · 无波形"))

        mm = manual or {}
        bpm_m = mm.get("bpm_manual") or 0.0
        try:
            bpm_m = float(bpm_m)
        except (TypeError, ValueError):
            bpm_m = 0.0
        # ★ 无勾选框了：手动值 > 0 就显示手动值，否则显示自动值。
        _spin_silent(self.spin_bpm, bpm_m if bpm_m > 0 else (self._auto_bpm or 120.0))
        self._bpm_user_edited = False
        self._bpm_auto_shown = float(self._auto_bpm or 0.0)

        self.wave.set_waveform(self._wf)
        self.wave.set_grid(self._grid)
        self.wave.set_manual(mm)
        # ★ 2026-10-28：自动分析出的多 BPM 段要显示出来（用户手动分段优先）。
        #   只做「显示」，不写进 grid_manual —— 见 `_effective_manual` 里的同源判断。
        if not mm.get("segments") and (self._grid or {}).get("segments"):
            self.wave.set_segments(self._grid["segments"])
        # ★ 2026-10-28：能量段落色带（演出时就按这张表立即切池，见 src/sections.py）
        try:
            self.wave.set_energy_sections(self._energy_rows())
        except Exception:                                     # noqa: BLE001
            pass
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
        _had_m = 0.0
        try:
            _had_m = float((self._manual or {}).get("bpm_manual") or 0.0)
        except (TypeError, ValueError):
            _had_m = 0.0
        if not getattr(self, "_bpm_user_edited", False) and _had_m <= 0.0:
            # 没改过 BPM、也没存过手动值 ⇒ 框里那个 120 只是占位，不能落盘
            bpm_m = 0.0
        else:
            bpm_m = bpm_now if (bpm_now > 0 and (auto <= 0 or abs(bpm_now - auto) > 0.05)) else 0.0
        anchors = self.wave.anchors()
        segments = self.wave.segments()
        # ★★ 2026-10-28：自动分段「只显示、不落成手动」。
        #   波形控件只有一份 `_segments`，自动结果也画在那里；若原样写进 grid_manual，
        #   `effective_grid` 就会走 manual 分支（src="manual"），之后重扫也不更新。
        #   ★ 2026-11-18：比较对象必须是**自动网格**（meta["grid"]），不能是
        #   effective_grid —— 后者已经把 manual.segments 复制进来，手动分段永远
        #   等于自己 ⇒ 一按保存就被清空。
        auto_segs = (getattr(self, "_grid_auto", None) or {}).get("segments") or []
        if auto_segs and self._segments_equal(segments, auto_segs):
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
                # ★ 2026-11-18：anchor 也是用户可能改的（八拍线 ±1/±4 拍）；
                #   只比 t0/bpm 会把“只挪相位”的手动状态误判成自动而清掉。
                ax = float(x.get("anchor") or 0.0)
                ay = float(y.get("anchor") or 0.0)
                if abs(ax - ay) > max(0.005, tol * max(1.0, abs(ax), abs(ay))):
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
            self._grid = _bg.effective_grid((self._meta or {}).get("grid") or {}, m,
                                            self._meta)
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
        # ★ 2026-11-18：切分/修改 BPM 造出的新段如果不带 anchor，会变成 0
        #   ⇒ 8 拍线整体跳。默认继承当前生效网格的 anchor（后面可再手动调）。
        _base_anchor = 0.0
        try:
            _base_anchor = float((self._grid or {}).get("anchor") or 0.0)
        except (TypeError, ValueError):
            _base_anchor = 0.0
        for x in segs:
            if x.get("anchor") is None:
                x["anchor"] = round(_base_anchor, 4)
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

    # ★★★ 2026-11-13（ERLIB 同步）：八拍线 ±1/±4 拍微调、尾巴 ±8 拍、
    #   整段重排 8 拍、改段名 —— 与 ERLIB 详情页同一套操作。
    def _nudge_grid(self, beats):
        """整首八拍线平移 `beats` 拍（写手动 anchors；保存后一起跟随）。"""
        try:
            bl = float(self._beat_len_now() or 0.0)
        except Exception:                                        # noqa: BLE001
            bl = 0.0
        if bl <= 0.0:
            QMessageBox.information(self, "八拍线微调", "还没有可用的 BPM / 拍长。")
            return False
        d = float(beats) * bl
        segs = self.wave.segments()
        if segs:
            for s in segs:
                # ★ 2026-11-18：每段用**自己的**拍长（多 BPM 歌不能拿首段 BPM 一刀切）
                try:
                    _bl = float(s.get("beat_len") or 0.0)
                except (TypeError, ValueError):
                    _bl = 0.0
                if _bl <= 0.0:
                    try:
                        _b = float(s.get("bpm") or 0.0)
                    except (TypeError, ValueError):
                        _b = 0.0
                    _bl = (60.0 / _b) if _b > 0 else bl
                try:
                    s["anchor"] = round(float(s.get("anchor") or 0.0)
                                        + float(beats) * _bl, 4)
                except (TypeError, ValueError):
                    pass
            self.wave.set_segments(segs)
        else:
            a = float((self._grid or {}).get("anchor") or 0.0)
            span = 8.0 * bl
            _ans = [float(x) for x in (self.wave.anchors() or [])]
            if _ans:
                self.wave.set_anchors([round((x + d) % span, 4) for x in _ans])
            else:
                self.wave.set_anchors([round((a + d) % span, 4)])
        self._refresh_grid_preview()
        self._mark_dirty()
        return True

    def _save_sections_now(self, out, raw=None):
        """把能量段落直接写回 cfg（`sections` / `sections_raw` / 手动标记）。

        `raw` 只给“边界真的变了”的动作（尾巴平移/重排）用；改名这类动作必须
        保留原有的未吸附边界，否则下次打开会被再吸附一次（越改越粗）。
        """
        try:
            mm = self.cfg["music_meta"]
            rec = mm.get(self.path)
            if not isinstance(rec, dict):
                rec = {}
                mm[self.path] = rec
            rec["sections"] = [dict(x) for x in out]
            rec["sections_raw"] = [dict(x) for x in (raw if raw is not None else out)]
            rec["sections_auto"] = False
            self.cfg.save()
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.warning(self, "能量段落", "保存失败：%s" % e)
            return False
        self._meta = rec
        try:
            self.wave.set_energy_sections([dict(x) for x in out])
        except Exception:                                        # noqa: BLE001
            pass
        self._dirty = False
        self._update_title()
        return True

    def _shift_tail(self, sec, beats):
        """把 `sec` 之后的能量段落边界整体平移 `beats` 拍（写回 cfg）。"""
        try:
            bl = float(self._beat_len_now() or 0.0)
        except Exception:                                        # noqa: BLE001
            bl = 0.0
        if bl <= 0.0:
            QMessageBox.information(self, "尾巴平移", "还没有可用的 BPM / 拍长。")
            return False
        rows = [dict(x) for x in self._energy_rows()]
        if len(rows) < 2:
            QMessageBox.information(self, "尾巴平移",
                                    "这首歌还没有能量段落，先「按 BPM 重新分段」。")
            return False
        d = float(beats) * bl
        eps = 1e-6
        out = [dict(x) for x in rows]
        for x in out[:-1]:
            try:
                t = float(x.get("t1") or 0.0)
            except (TypeError, ValueError):
                continue
            if t > sec + eps:
                x["t1"] = t + d
        for x in out[1:]:
            try:
                t = float(x.get("t0") or 0.0)
            except (TypeError, ValueError):
                continue
            if t > sec + eps:
                x["t0"] = t + d
        out[0]["t0"] = 0.0
        if len(out) >= 2:
            try:
                tail_len = float(out[-1]["t1"]) - float(out[-1]["t0"])
            except (TypeError, ValueError):
                tail_len = 0.0
            if tail_len <= max(0.05, 1.0 * bl):
                out[-2]["t1"] = out[-1]["t1"]
                out.pop()
        for i in range(1, len(out)):
            a0 = float(out[i - 1]["t1"])
            t0 = float(out[i]["t0"])
            if t0 < a0 - 1e-3:
                out[i]["t0"] = a0
            if float(out[i]["t1"]) <= float(out[i]["t0"]):
                out[i]["t1"] = float(out[i]["t0"]) + 1e-3
        return self._save_sections_now(out)

    def _rename_section(self, sec):
        """改当前能量段的名字（段名会随 `sections` 落盘，演出端直接用）。"""
        rows = [dict(x) for x in self._energy_rows()]
        if not rows:
            QMessageBox.information(self, "编辑本段名称", "这首歌还没有能量段落。")
            return False
        idx = 0
        for i, x in enumerate(rows):
            try:
                if float(x.get("t0") or 0.0) <= sec:
                    idx = i
            except (TypeError, ValueError):
                pass
        old = str(rows[idx].get("name") or "")
        name, ok = QInputDialog.getText(self, "编辑本段名称", "段名：", text=old)
        if not ok:
            return False
        name = str(name or "").strip()
        if not name:
            return False
        rows[idx]["name"] = name
        if not self._save_sections_now(
                rows, raw=(self._meta or {}).get("sections_raw")):
            return False
        return True

    def _requantize_tail_8(self, sec):
        """从 `sec` 起把能量段边界重排到最近的 8 拍线（段数不变）。"""
        rows = [dict(x) for x in self._energy_rows()]
        if len(rows) < 2:
            QMessageBox.information(self, "整段重排", "这首歌还没有能量段落。")
            return False
        g = self._grid_for_snap() or {}
        try:
            a = float(g.get("anchor") or 0.0)
            bl = float(g.get("beat_len") or 0.0)
        except (TypeError, ValueError):
            a, bl = 0.0, 0.0
        if bl <= 0.0:
            QMessageBox.information(self, "整段重排", "还没有可用的 BPM / 拍长。")
            return False
        step = 8.0 * bl
        eps = 1e-6
        try:
            _dur = float(self._dur_now() or 0.0)
        except (TypeError, ValueError):
            _dur = 0.0
        prev = 0.0
        for i in range(1, len(rows)):
            try:
                t = float(rows[i].get("t0") or 0.0)
            except (TypeError, ValueError):
                t = prev
            if t <= sec + eps:
                prev = float(rows[i].get("t1") or t)
                continue
            try:
                _next = (float(rows[i + 1].get("t0") or 0.0)
                         if i + 1 < len(rows) else _dur)
            except (TypeError, ValueError):
                _next = _dur
            k = round((t - a) / step)
            nt = a + k * step
            while nt <= prev + 0.5 * bl:
                nt += step
            # 不能越过下一段起点 / 曲尾；越界就保留原边界，避免 0 长度/重叠
            if (_next > 0 and nt >= _next - 0.05) or (_dur > 0 and nt >= _dur):
                nt = t
            rows[i]["t0"] = round(nt, 3)
            rows[i - 1]["t1"] = rows[i]["t0"]
            prev = rows[i]["t0"]
        try:
            _last = float(rows[-1].get("t1") or 0.0)
        except (TypeError, ValueError):
            _last = 0.0
        rows[-1]["t1"] = _dur if _dur > 0 else max(_last, rows[-1]["t0"] + 0.05)
        if not self._save_sections_now(rows):
            return False
        return True

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
        # 能量段落放在最前：它决定**演出时**什么时候换素材池，与 BPM 分段是不同的东西
        acts["esec"] = menu.addAction("编辑能量段落（演出到点直接切池）…")
        acts["rename"] = menu.addAction("编辑本段名称…")
        acts["rename"].setEnabled(bool(self._energy_rows()))
        menu.addSeparator()
        acts["cut"] = menu.addAction("在 %.2fs 处切分（输入该段 BPM）…" % sec)
        acts["edit"] = menu.addAction("修改本段 BPM（当前 %.1f）…" % cur_bpm)
        acts["del"] = menu.addAction("删除本段（%.2fs 起，%.1f BPM）"
                                     % (float(cur.get("t0") or 0.0), cur_bpm))
        menu.addSeparator()
        acts["half"] = menu.addAction("整段 ÷2 BPM（半速歧义）")
        acts["dbl"] = menu.addAction("整段 ×2 BPM（倍速歧义）")
        menu.addSeparator()
        # ★ 2026-11-13（ERLIB 同步）：八拍线手动微调（写 grid_manual.anchors）
        acts["nudge_p1"] = menu.addAction("八拍线延后 1 拍（+1）")
        acts["nudge_m1"] = menu.addAction("八拍线提前 1 拍（−1）")
        acts["nudge_p4"] = menu.addAction("八拍线延后 4 拍（+4）")
        acts["nudge_m4"] = menu.addAction("八拍线提前 4 拍（−4）")
        acts["reset_phase"] = menu.addAction("八拍线恢复自动相位")
        acts["reset_phase"].setEnabled(bool(self.wave.anchors())
                                       or bool(self.wave.segments())
                                       or bool((self._meta or {}).get("grid_flip")))
        acts["tail_p8"] = menu.addAction("从 %.1fs 起整条尾巴后移 8 拍" % float(sec))
        acts["tail_m8"] = menu.addAction("从 %.1fs 起整条尾巴前移 8 拍" % float(sec))
        _n_esec = len(self._energy_rows())
        acts["tail_p8"].setEnabled(_n_esec >= 2)
        acts["tail_m8"].setEnabled(_n_esec >= 2)
        acts["requant8"] = menu.addAction("从 %.1fs 起整段重排成 8 拍" % float(sec))
        acts["requant8"].setEnabled(_n_esec >= 2)
        menu.addSeparator()
        acts["clear"] = menu.addAction("清除全部分段（回到自动网格）")
        # 首段不能删（网格必须从 0 开始）；单段也没有「删除本段」的意义
        acts["del"].setEnabled(len(segs) >= 2 and idx > 0)
        acts["edit"].setEnabled(bool(segs) or cur_bpm > 0)
        acts["clear"].setEnabled(len(segs) >= 2 or bool(self.wave.segments()))
        return menu, acts, idx, segs, cur_bpm

    def _run_seg_action(self, key, idx, segs, cur_bpm, sec):
        """执行右键菜单里的某一项（`key` 见 `_build_seg_menu` 的 acts）。"""
        if key == "esec":
            return self._edit_energy_sections(sec)
        if key == "rename":
            return self._rename_section(sec)
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
        if key in ("nudge_p1", "nudge_m1", "nudge_p4", "nudge_m4"):
            return self._nudge_grid({"nudge_p1": 1, "nudge_m1": -1,
                                     "nudge_p4": 4, "nudge_m4": -4}[key])
        if key == "reset_phase":
            self.wave.set_anchors([])
            # ★ 2026-11-18：手动分段的每段 anchor / grid_flip 也要一起清，
            #   否则“恢复自动相位”看起来没反应。
            _segs = [dict(x) for x in (self.wave.segments() or [])]
            if _segs:
                try:
                    _a = float((self._grid_auto or {}).get("anchor") or 0.0)
                except (TypeError, ValueError):
                    _a = 0.0
                for _s in _segs:
                    _s["anchor"] = round(_a, 4)
                self.wave.set_segments(_segs)
            try:
                _mm = self.cfg["music_meta"]
                _rec = _mm.get(self.path)
                if isinstance(_rec, dict) and _rec.get("grid_flip"):
                    _rec.pop("grid_flip", None)
                    self.cfg.save()
            except Exception:                                    # noqa: BLE001
                pass
            self._refresh_grid_preview()
            self._mark_dirty()
            return True
        if key in ("tail_p8", "tail_m8"):
            return self._shift_tail(sec, 8 if key == "tail_p8" else -8)
        if key == "requant8":
            return self._requantize_tail_8(sec)
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
        self._bpm_user_edited = True
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

    # ---------------- 能量段落 ----------------
    def _energy_rows(self):
        """当前曲目的段落表（normalize 过）。取不到返回 []。

        ★★ 2026-10-29：优先读 `sections_raw`（**未吸附**的原始边界）。
          为什么：八拍线是小节线的**子集**，一旦把边界吸到八拍，再切成"小节"
          就是原地不动（实测 25.95 → 25.95，用户会以为选项没生效）。
          只有保留原始值，八拍 ⇄ 小节才能双向切换。
        """
        try:
            meta = (self.cfg["music_meta"] or {}).get(self.path) or {}
        except Exception:                                     # noqa: BLE001
            return []
        # ★★ 2026-10-30 用户第二次反馈（截图「怎么还是三个八拍五个八拍」）：
        #   **自动生成**的段落（`sections_auto` 不是 False）直接用库里存好的
        #   `sections` —— 它是扫描时**经过乐句量化**的（边界落在 2 个八拍网格上：
        #   0/16.00/76.01/136.01/151.01/196.01/271.02）。
        #   为什么不能再过一遍吸附：`sections_raw` 是"未吸附的原始边界"
        #   （13.61/75.79/136.72…），按八拍重吸附又回到 12.25 —— 还是 3 个八拍，
        #   用户看到的正是这个。而且界面上「吸附：拍/小节」是**手动编辑**时的工具，
        #   不该影响自动结果的显示（截图里选的是"拍"，一按粒度判断就会走 raw）。
        #   raw 只在**手动编辑过**（`sections_auto is False`）时才用 —— 那时需要更细的
        #   原始值才能实现"八拍 ⇄ 小节"双向切换。
        if meta.get("sections_auto", True) is not False:
            sec = meta.get("sections") or []
            if sec:
                return S.normalize(sec, self._dur_now(), None)
        raw = meta.get("sections_raw") or meta.get("sections") or []
        if not raw:
            return []
        return S.normalize(raw, self._dur_now(), self._grid_for_snap() or None)

    def _dur_now(self):
        try:
            d = float((self._wf or {}).get("dur") or 0.0)
        except (TypeError, ValueError):
            d = 0.0
        if d > 0:
            return d
        try:
            return float((self._grid or {}).get("dur") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _grid_for_snap(self):
        """给段落吸附用的 `{anchor, beat_len}`；没 BPM 时返回 {}（不吸附）。"""
        g = self._grid or {}
        try:
            bl = float(g.get("beat_len") or 0.0)
        except (TypeError, ValueError):
            bl = 0.0
        if bl <= 1e-6:
            try:
                bpm = float(g.get("bpm") or 0.0)
            except (TypeError, ValueError):
                bpm = 0.0
            bl = 60.0 / bpm if bpm > 0 else 0.0
        if bl <= 1e-6:
            return {}
        try:
            a = float(g.get("anchor") or 0.0)
        except (TypeError, ValueError):
            a = 0.0
        return {"anchor": a, "beat_len": bl}

    def _resec_busy(self):
        w = getattr(self, "_resec_worker", None)
        try:
            return w is not None and w.isRunning()
        except Exception:                                        # noqa: BLE001
            return False

    def _set_resec_busy(self, busy):
        """重算期间锁住会改数据/重复触发的控件；关闭键保持可用。"""
        for name in ("btn_resec", "spin_bpm", "btn_save", "btn_revert"):
            w = getattr(self, name, None)
            if w is not None:
                try:
                    w.setEnabled(not busy)
                except Exception:                                # noqa: BLE001
                    pass
        try:
            self.btn_resec.setText("正在重新分段…" if busy else "按 BPM 重新分段")
        except Exception:                                        # noqa: BLE001
            pass

    def _resegment_by_bpm(self, ask=False):
        """用**当前 BPM 框里的值**重算这一首的能量段落，并落盘。

        ★ 2026-10-30 用户：「增加一个依据当前 bpm 重新分段的选项」。
          为什么需要：自动 BPM 差半拍、或者半速/倍速判错时，能量分段是按
          "2 个八拍 = 16 拍"切块的 ⇒ 整块错位，一首歌会被切成 0.88/0.94/0.16
          这种碎段（现场 `Bunsen Burner` 就是这样）。手改 BPM 之后必须能
          **立刻**按新 BPM 重切这一首，而不是等一次全库扫描（那要几分钟）。

        口径与生产扫描**逐字一致**（`ui_main._one`）：全曲 `sf.read` → 重采样 →
        `beatgrid.features` → `sections.auto_segments(grid=生效网格)`，再算一份
        `sections_raw`（`grid=None, phrase_grid=生效网格`）。

        ★ 2026-11-18：解码/特征/分段全部放到 `_ResegmentWorker`（QThread），
           UI 线程只负责开始和收结果。大文件不再卡住详情页，也不会出现
           `QApplication.processEvents()` 重入导致的“关到一半又弹窗”。
        """
        if self._resec_busy():
            return False
        try:
            bpm = float(self.spin_bpm.value() or 0.0)
        except (TypeError, ValueError):
            bpm = 0.0
        if bpm <= 0:
            QMessageBox.information(self, "重新分段", "BPM 无效（必须 > 0）。")
            return False
        if ask:
            ret = QMessageBox.question(
                self, "按 BPM 重新分段",
                "按当前 BPM %.1f 重新计算这首歌的能量段落（前奏/中段/drop/尾奏）？\n\n"
                "· 只重算这一首，不重扫曲库\n"
                "· 这个 BPM 会一并记为手动值\n"
                "· 手动编辑过的段落会被覆盖" % bpm,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if ret != QMessageBox.StandardButton.Yes:
                return False
        try:
            anchor = float((self._grid or {}).get("anchor") or 0.0)
        except (TypeError, ValueError):
            anchor = 0.0

        self._shutdown_audio()                 # 重算要独占读文件（试听可能正开着）
        self._resec_close_pending = False
        w = _ResegmentWorker(self.path, bpm, anchor, self)
        self._resec_worker = w
        w.done.connect(self._on_reseg_done)
        w.failed.connect(self._on_reseg_failed)
        w.finished.connect(self._on_reseg_finished)
        self._set_resec_busy(True)
        self.lbl_pos.setText("正在按 %.1f BPM 重新分段…" % bpm)
        w.start(QThread.LowPriority)           # 低优先级：别和播放/预览抢 CPU
        return True

    def _on_reseg_done(self, payload):
        if self._resec_close_pending:
            return                              # 用户已关窗：丢弃迟到结果
        try:
            bpm = float((payload or {}).get("bpm") or 0.0)
            got = list((payload or {}).get("got") or [])
            raw = list((payload or {}).get("raw") or [])
            anchor = float((payload or {}).get("anchor") or 0.0)
        except Exception:                                        # noqa: BLE001
            bpm, got, raw, anchor = 0.0, [], [], 0.0
        if bpm <= 0 or not got:
            self.lbl_pos.setText("重新分段失败：算不出段落")
            QMessageBox.information(self, "重新分段", "这首算不出段落（特征不足）。")
            return
        self._apply_resegment(bpm, got, raw, anchor)

    def _on_reseg_failed(self, msg):
        if self._resec_close_pending:
            return
        self.lbl_pos.setText("重新分段失败")
        QMessageBox.warning(self, "重新分段", "重算失败：%s" % msg)

    def _on_reseg_finished(self):
        w = getattr(self, "_resec_worker", None)
        self._resec_worker = None
        if w is not None:
            try:
                w.deleteLater()
            except Exception:                                    # noqa: BLE001
                pass
        self._set_resec_busy(False)
        if self._resec_close_pending:
            self._resec_close_pending = False
            QTimer.singleShot(0, self.close)

    def _apply_resegment(self, bpm, got, raw, anchor=0.0):
        """把 worker 结果落盘并刷新界面（只在 UI 线程调用）。"""
        grid = {"anchor": float(anchor or 0.0),
                "beat_len": round(60.0 / float(bpm), 6),
                "bpm": float(bpm)}
        # ---- 落盘：sections + 手动 BPM（新 list ⇒ 引擎按引用失效自动重算）----
        try:
            mm = self.cfg["music_meta"]
            rec = mm.get(self.path)
            if not isinstance(rec, dict):
                rec = {}
                mm[self.path] = rec
            gm = rec.get("grid_manual")
            gm = dict(gm) if isinstance(gm, dict) else {}
            # ★★★ 2026-11-07（用户：「自动扫描的，标签怎么写手动」）：
            #   点「按 BPM 重新分段」时，BPM 框里如果还是**自动值**，原来也无条件写
            #   `grid_manual.bpm_manual` ⇒ 这首歌从此被标成"手动"，用户一头雾水。
            #   ⇒ 与自动 BPM 相同（±0.05）就不写，并清掉历史遗留的同值记录。
            _auto_bpm = float((rec.get("grid") or {}).get("bpm") or 0.0)
            if _auto_bpm > 0 and abs(float(bpm) - _auto_bpm) < 0.05:
                gm.pop("bpm_manual", None)
            else:
                gm["bpm_manual"] = float(bpm)
            # ★ 2026-11-18：这个动作明确断言“整首就按这个 BPM” ⇒ 旧的
            #   `grid_manual.segments`（多 BPM）必须清掉，否则 effective_grid
            #   仍优先用手动分段，BPM 字段看着变了、播放网格没变。
            gm.pop("segments", None)
            rec["grid_manual"] = gm
            rec["sections"] = [dict(x) for x in got]
            rec["sections_raw"] = [dict(x) for x in raw]
            rec["sections_auto"] = True
            self.cfg.save()
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.warning(self, "重新分段", "写盘失败：%s" % e)
            self.lbl_pos.setText("重新分段失败：写盘失败")
            return False

        # ---- 刷新界面（网格 + 色带 + 列表）----
        self._meta = rec
        self._manual = gm
        try:
            import beatgrid as _bg2
            self._grid = _bg2.effective_grid(rec.get("grid") or {}, gm, rec)
        except Exception:                                         # noqa: BLE001
            self._grid = dict(grid)
        for fn in (lambda: self.wave.set_grid(self._grid),
                   lambda: self.wave.set_energy_sections(self._energy_rows()),
                   self._refresh_lists):
            try:
                fn()
            except Exception:                                     # noqa: BLE001
                pass
        self._dirty = False                    # 已经落盘了，别再弹"有未保存的修改"
        self._update_title()
        n_hi = sum(1 for x in got if x.get("tier") == "high")
        self.lbl_pos.setText("%.1f BPM · %d 段（drop %d）" % (bpm, len(got), n_hi))
        return True

    def _edit_energy_sections(self, sec):
        """打开编辑框；确定后写回 `music_meta[path]["sections"]` 并落盘。

        ⚠ 保存时**替换成新 list**（不是原地 append）：引擎那边按 `raw is cached_raw`
          判缓存，引用一变就自动重算，不需要额外的"通知引擎"调用（少一个易漏点）。
        """
        try:
            meta = (self.cfg["music_meta"] or {}).get(self.path) or {}
            # ★ 2026-11-18：优先用**未吸附**的 sections_raw；拿 sections 再切
            #   “小节”是原地不动（8 拍是小节子集），用户会以为粒度没生效。
            raw = meta.get("sections_raw") or meta.get("sections") or []
        except Exception:                                     # noqa: BLE001
            raw = []
        dlg = EnergySectionDialog(self, sections=raw, dur=self._dur_now(),
                                  grid=self._grid_for_snap(),
                                  cur_sec=float(sec or 0.0))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        got = dlg.sections()
        try:
            mm = self.cfg["music_meta"]
            rec = mm.get(self.path)
            if not isinstance(rec, dict):
                rec = {}
                mm[self.path] = rec
            if got:
                rec["sections"] = [dict(x) for x in got]      # ★ 新 list ⇒ 引擎缓存按引用失效
                # ★ 2026-10-29：同时记一份**未吸附**的原始边界，供下次编辑时切换粒度
                #   （八拍是小节的子集，只留吸附值就再也细化不回去）。
                rec["sections_raw"] = S.normalize(
                    [dict(x) for x in got], self._dur_now(), None)
                # ★ 手动保存 = 用户认过的段落 ⇒ 标记为非自动，
                #   以后「扫描曲库 / 批量自动分段」一律不覆盖它（见 _auto_sections_apply）。
                rec["sections_auto"] = False
            else:
                rec.pop("sections", None)
                rec.pop("sections_raw", None)
                rec.pop("sections_auto", None)
            self.cfg.save()
        except Exception as e:                                # noqa: BLE001
            QMessageBox.warning(self, "能量段落", "保存失败：%s" % e)
            return False
        try:
            self.wave.set_energy_sections(got)
        except Exception:                                     # noqa: BLE001
            pass
        self._mark_dirty()
        return True

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
        if self._resec_busy():
            self._resec_close_pending = True
            try:
                self._resec_worker.requestInterruption()
            except Exception:                                    # noqa: BLE001
                pass
            self.lbl_pos.setText("正在取消重新分段…")
            ev.ignore()
            return
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
        if self._resec_busy():
            self._resec_close_pending = True
            try:
                self._resec_worker.requestInterruption()
            except Exception:                                    # noqa: BLE001
                pass
            self.lbl_pos.setText("正在取消重新分段…")
            return
        self._shutdown_audio()
        super().done(r)
