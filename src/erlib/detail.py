# -*- coding: utf-8 -*-
r"""ERLIB 歌曲详情页 —— **整份照搬 ERAVJ 的 `music_detail.MusicDetailDialog`**，
只把"存哪里"换成 erlib 的工作库（`.eravjlib`）。

★ 2026-10-31 用户：「直接按着 eravj 的做 erlib」。
  之前我自己攒了一个版本（右侧三个列表），和 ERAVJ 长得不一样、还少了波形下方的
  段落色带。现在**结构、控件、行为全部沿用 ERAVJ**，只改四处存储/数据来源：
      ① `__init__` 收的是 `(work, sid, root)`，不是 `(cfg, path)`；
      ② `_load` 从 `LibReader.songs()` 读 grid / manual / sections / waveform；
      ③ `_write_manual` 写 `LibWriter.update_manual`；
      ④ 能量段落（`_resegment_by_bpm` / `_edit_energy_sections`）写 `update_sections`。

⚠ 与 `src/music_detail.py` 是**双实现**：那边改 UI 时这里要跟着改（反之亦然），
  否则两个软件的详情页又会长得不一样（这正是这一轮返工的原因）。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox,
                               QFormLayout, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QMenu, QMessageBox, QPushButton,
                               QVBoxLayout)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import sections as S                                        # noqa: E402
from erlib.format import LibReader, LibWriter                # noqa: E402
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


def _find_audio(song, root, max_walk=20000):
    """按「本机绝对路径 → 曲库根+相对路径 → 曲库根+文件名」找这首歌的音频文件。

    ★★ 2026-10-31（用户「这里还是得要能播放的，只是说导出的文件不要带位置」）：
      **播放是"本机行为"，位置是"导出物里不能有的东西"** —— 这两件事必须分开：
        · 工作库的 `songs.src_path`（本机绝对路径）+ `meta.root`：只在本机用，
          `transfer.export_subset()` 导出时**整列丢弃 / 清空**；
        · 导出包里只留 `rel_path` / `file_name`（相对、无盘符）。
      之前一刀把 `rel_path` 改成纯文件名、又没留本机路径 ⇒ 详情页一开就
      「无法播放：找不到音频文件」（用户 2026-10-31 截图）。

    返回绝对路径；都找不到返回 ""（界面提示"本机没有这个音频文件"）。
    """
    song = song or {}
    rel = str(song.get("rel_path") or "")
    fn = str(song.get("file_name") or "")
    cands = []
    sp = str(song.get("src_path") or "")
    if sp:
        cands.append(sp)                       # ① 扫描时记下的本机绝对路径
    for r in (rel, fn):
        if r and root and not os.path.isabs(r):
            cands.append(os.path.join(root, r))    # ② 曲库根 + 相对路径 / 文件名
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            continue
    # ③ 兜底：文件被挪到 root 下的别的子目录了 ⇒ 按文件名在 root 里找一次
    #   ⚠ 带上限（`max_walk`）—— 几十万文件的目录不能让"点一下详情"卡死。
    if root and fn and os.path.isdir(root):
        n = 0
        try:
            for dp, _dns, fns in os.walk(root):
                for f in fns:
                    if f == fn:
                        return os.path.join(dp, f)
                    n += 1
                    if n >= max_walk:
                        raise StopIteration
        except StopIteration:
            pass
        except OSError:
            pass
    return ""


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


class ErlibDetailDialog(QDialog):
    """演出端单曲详情编辑对话框（读写 `music_meta[path]["grid_manual"]`）。"""

    def __init__(self, work, sid, root="", parent=None):
        """`work` = `.eravjlib` 路径，`sid` = 工作库里的 song_id，`root` = 曲库根目录。

        ※ UI 与行为**整份照搬 ERAVJ**（`music_detail.MusicDetailDialog`），
          只把数据源从 `cfg["music_meta"][path]` 换成 `.eravjlib` 的 `songs` 表。
        """
        super().__init__(parent)
        self.work = str(work)
        self.sid = int(sid)
        self.root = str(root or "")
        self.path = ""              # 音频绝对路径（_load 里按 rel_path + root 算出来）
        self._song = {}             # 工作库里这一行（songs 表）
        self._info_text = ""
        self._meta = {}             # 兼容 ERAVJ 代码里的 `_meta`（这里 = self._song）
        self._grid = {}
        self._manual = None
        self._wf = None
        self._sections = []
        self._sections_raw = []
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
        # ⚠ ERLIB 的文本在 `_load()` 里才生成（要先读工作库）⇒ 这里**无条件**建好控件，
        #   否则 `self._info_text` 为空时框根本不存在，后面 setText 全落空（实测白框都没有）。
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(8, 6, 8, 6)
        self.lbl_info = QLabel(self._info_text or "—")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_info.setStyleSheet("color:palette(mid);")
        bl.addWidget(self.lbl_info)
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
            song, manual, wf = self._read_song()
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "歌曲详情", "读取工作库失败：%s" % e)
            return
        if not song:
            QMessageBox.warning(self, "歌曲详情", "工作库里找不到这首歌（可能被删除）。")
            return
        self._song = song
        self._meta = song
        # 音频路径：本机绝对路径（src_path）优先，其次 曲库根 + 相对路径/文件名
        # ★ 2026-10-31：导出包不带位置，所以这里**只在本机能找到就用**；
        #   找不到也不报错，点 ▶ 时再提示（包在演出机上本来就没有音频文件）。
        self.path = _find_audio(song, self.root)
        self._audio_path = self.path

        self._manual = manual if isinstance(manual, dict) else None
        g = song.get("grid") if isinstance(song.get("grid"), dict) else {}
        try:
            import beatgrid as _bg
            self._grid = _bg.effective_grid(g, self._manual, song)
        except Exception:                                        # noqa: BLE001
            self._grid = dict(g)
        self._auto_bpm = float(self._grid.get("bpm") or 0.0)
        # 能量段落
        self._sections = song.get("sections") or []
        self._sections_raw = song.get("sections_raw") or []
        if isinstance(self._sections, str):
            self._sections = []

        title = song.get("title") or song.get("file_name") or ("song %d" % self.sid)
        artist = song.get("artist") or ""
        dur = 0.0
        for _src in (song.get("duration"), self._grid.get("dur"),
                     (wf or {}).get("dur")):
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
        # ★ 2026-10-31（用户「不要艺人和曲名」）：标题只显示文件名
        self.lbl_title.setText(str(song.get("file_name") or ("song %d" % self.sid)))

        # 波形：优先用**包里带的**（扫描时算好）；没有才懒解码一次
        self._wf = wf or _lazy_waveform(self.path)
        if self._wf:
            try:
                dur = float(self._wf.get("dur") or dur)
                dtext = "%d:%02d" % (int(dur) // 60, int(dur) % 60)
            except (TypeError, ValueError):
                pass
        # ★ 2026-10-31（用户「bpm 不要写来源」）：只显示数值 + 时长
        self.lbl_auto.setText("自动 BPM：%s · %s"
                              % ((" %.1f" % self._auto_bpm) if self._auto_bpm else "—",
                                 dtext) +
                              ("" if self._wf else " · 无波形"))

        # 曲目信息块（ERAVJ 那边由 music_info_text() 生成，这里按同一版式现造）
        try:
            import soundfile as _sf
            _sr = int(_sf.info(self.path).samplerate) if self.path else 0
        except Exception:                                        # noqa: BLE001
            _sr = 0
        # ★★ 2026-10-31（用户「erlib 不要带文件原位置，dj 和 vj 不是一台电脑」）：
        #   **不显示、也不携带**源文件路径 —— 在 DJ 机上扫的 `<曲库目录>/…` 到演出机上
        #   既没意义又容易让人以为"文件该在这个位置"。包里只留 `rel_path`（相对曲库根），
        #   没有根目录就显示「—」。
        self._info_text = (
            "文件：%s\n"
            "时长：%s    采样率：%s Hz\n"
            "指纹：%s 条    ID：%s"
            % (song.get("file_name") or "—", dtext,
               _sr or "—", song.get("fp_count") or 0, self.sid))
        try:
            self.lbl_info.setText(self._info_text)
        except Exception:                                        # noqa: BLE001
            pass

        mm = self._manual if isinstance(self._manual, dict) else {}
        bpm_m = mm.get("bpm_manual") or 0.0
        try:
            bpm_m = float(bpm_m)
        except (TypeError, ValueError):
            bpm_m = 0.0
        _spin_silent(self.spin_bpm, bpm_m if bpm_m > 0 else (self._auto_bpm or 120.0))
        self._bpm_auto_shown = float(self._auto_bpm or 0.0)

        self.wave.set_waveform(self._wf)
        self.wave.set_grid(self._grid)
        self.wave.set_manual(mm)
        if not mm.get("segments") and (self._grid or {}).get("segments"):
            self.wave.set_segments(self._grid["segments"])
        try:
            self.wave.set_energy_sections(self._energy_rows())
        except Exception:                                        # noqa: BLE001
            pass
        self.wave.set_playhead(0.0)
        self._dirty = False
        self._refresh_lists()
        self._update_title()

    # ---------------- 工作库读写（ERLIB 的存储层） ----------------
    def _read_song(self):
        """返回 `(song_dict, manual, waveform)`。"""
        with LibReader(self.work) as r:
            for s in r.songs():
                if int(s.get("song_id") or 0) == self.sid:
                    return s, r.manual_of(self.sid), r.waveform(self.sid)
        return {}, None, None
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

    # ★★★ 2026-11-13（用户「我主要要的是 8 拍和段落边界要对」）：
    #   自动相位判据在个别歌上定不了（Clockwork Vespers 用户说偏 4 拍，四种判据全指向
    #   当前相位）⇒ 手动微调入口：右键 ±1 / ±4 拍，写 `grid_manual.anchors`。
    def _nudge_grid(self, beats):
        """整首八拍线平移 `beats` 拍（写手动 anchors；保存后导出/切分一起跟随）。"""
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
            # 多 BPM 分段：每段自己的 anchor 一起平移（段长不变）
            for s in segs:
                try:
                    s["anchor"] = round(float(s.get("anchor") or 0.0) + d, 4)
                except (TypeError, ValueError):
                    pass
            self.wave.set_segments(segs)
        else:
            a = float((self._grid or {}).get("anchor") or 0.0)
            span = 8.0 * bl
            self.wave.set_anchors([round((a + d) % span, 4)])
            # 2026-11-18（用户要求）：不再记录相位校准表，重扫后完全由算法决定。
        self._refresh_grid_preview()
        self._mark_dirty()
        return True

    # ★★★ 2026-11-13（用户「段落边界差 8 拍」+ 偶数八拍规则）：
    #   单条边界挪 8 拍会把两侧段长变成奇数（违反 16 拍整数倍），
    #   所以只提供「从某点起整条尾巴平移 8 拍」—— Cothurnus 的「中段1 后移 8 拍」
    #   正是这类操作；平移后尾巴被挤没就并进前一段。
    #   注意：动的是**能量段落**（songs.sections），不是 BPM 分段。
    def _shift_tail(self, sec, beats):
        """把 `sec` 之后的能量段落边界整体平移 `beats` 拍（写回 songs.sections）。"""
        try:
            bl = float(self._beat_len_now() or 0.0)
        except Exception:                                        # noqa: BLE001
            bl = 0.0
        if bl <= 0.0:
            QMessageBox.information(self, "尾巴平移", "还没有可用的 BPM / 拍长。")
            return False
        secs = [dict(x) for x in (self._sections or [])]
        if len(secs) < 2:
            QMessageBox.information(self, "尾巴平移", "这首歌还没有能量段落，先「按 BPM 重新分段」。")
            return False
        d = float(beats) * bl
        eps = 1e-6
        out = [dict(x) for x in secs]
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
        # 尾巴被挤没：末段 <= 1 拍 ⇒ 并进前一段
        if len(out) >= 2:
            try:
                tail_len = float(out[-1]["t1"]) - float(out[-1]["t0"])
            except (TypeError, ValueError):
                tail_len = 0.0
            if tail_len <= max(0.05, 1.0 * bl):
                out[-2]["t1"] = out[-1]["t1"]
                out.pop()
        # 单调性护栏
        for i in range(1, len(out)):
            a0 = float(out[i - 1]["t1"])
            t0 = float(out[i]["t0"])
            if t0 < a0 - 1e-3:
                out[i]["t0"] = a0
            if float(out[i]["t1"]) <= float(out[i]["t0"]):
                out[i]["t1"] = float(out[i]["t0"]) + 1e-3
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
            try:
                n = w.update_sections(self.sid, out, out)
            finally:
                w.close()
            if not n:
                QMessageBox.warning(self, "尾巴平移", "保存失败（工作库里找不到这首歌）。")
                return False
        except Exception as e:                                   # noqa: BLE001
            QMessageBox.warning(self, "尾巴平移", "保存失败：%s" % e)
            return False
        self._sections = out
        self._sections_raw = out
        try:
            self.wave.set_energy_sections([dict(x) for x in out])
        except Exception:                                        # noqa: BLE001
            pass
        self._mark_dirty()
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
        menu.addSeparator()
        acts["cut"] = menu.addAction("在 %.2fs 处切分（输入该段 BPM）…" % sec)
        acts["edit"] = menu.addAction("修改本段 BPM（当前 %.1f）…" % cur_bpm)
        acts["del"] = menu.addAction("删除本段（%.2fs 起，%.1f BPM）"
                                     % (float(cur.get("t0") or 0.0), cur_bpm))
        menu.addSeparator()
        acts["half"] = menu.addAction("整段 ÷2 BPM（半速歧义）")
        acts["dbl"] = menu.addAction("整段 ×2 BPM（倍速歧义）")
        menu.addSeparator()
        # ★★★ 2026-11-13（用户「我主要要的是 8 拍和段落边界要对」）：
        #   自动相位判据在个别歌上定不了（如 Clockwork Vespers 用户说整体偏 4 拍，
        #   但重音/连拍/换和弦/kick 四种判据都指向当前相位）⇒ 给八拍线一个手动微调。
        #   写进 `grid_manual.anchors`（`effective_grid` 取 anchors[0]），拍网整体平移；
        #   ±4 拍时 beat 线位置不变、只有八拍线翻半句 —— 正是用户要的效果。
        acts["nudge_p1"] = menu.addAction("八拍线延后 1 拍（+1）")
        acts["nudge_m1"] = menu.addAction("八拍线提前 1 拍（−1）")
        acts["nudge_p4"] = menu.addAction("八拍线延后 4 拍（+4）")
        acts["nudge_m4"] = menu.addAction("八拍线提前 4 拍（−4）")
        acts["reset_phase"] = menu.addAction("八拍线恢复自动相位")
        acts["reset_phase"].setEnabled(bool(self.wave.anchors())
                                       or bool(self.wave.segments()))
        # ★★★ 2026-11-13（「段落边界差 8 拍」）：整条尾巴平移，
        #   单条边界 ±8 拍会破坏偶数段长，这是唯一合法的 8 拍修正。
        acts["tail_p8"] = menu.addAction("从 %.1fs 起整条尾巴后移 8 拍" % float(sec))
        acts["tail_m8"] = menu.addAction("从 %.1fs 起整条尾巴前移 8 拍" % float(sec))
        acts["tail_p8"].setEnabled(len(self._sections or []) >= 2)
        acts["tail_m8"].setEnabled(len(self._sections or []) >= 2)
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
            self._refresh_grid_preview()
            self._mark_dirty()
            return True
        if key in ("tail_p8", "tail_m8"):
            return self._shift_tail(sec, 8 if key == "tail_p8" else -8)
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
        # ★ 2026-10-31：本机找不到音频文件时**别去动声卡**，直接说明原因
        #   （导出包按需求不带位置，到了演出机上本来就没有对应文件）。
        if not self.path:
            self.lbl_pos.setText(
                "无法播放：本机找不到这个音频文件"
                "（导出包只带文件名、不带位置）")
            return
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
        """当前曲目的能量段落（`songs.sections` → normalize，带 tier/energy/name）。"""
        raw = self._sections or []
        if not raw:
            return []
        try:
            return [dict(x) for x in S.normalize(raw, self._dur_now(), self._grid or None)]
        except Exception:                                        # noqa: BLE001
            return [dict(x) for x in raw if isinstance(x, dict)]
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

        写盘：`sections` / `sections_raw` / `sections_auto=True`，并把当前 BPM
        记为 `grid_manual.bpm_manual`（用户就是在断言这个 BPM；不记的话下次
        「扫描分析」又会按自动 BPM 重切，白改）。
        """
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
        from PySide6.QtWidgets import QApplication
        import numpy as np
        try:
            anchor = float((self._grid or {}).get("anchor") or 0.0)
        except (TypeError, ValueError):
            anchor = 0.0
        grid = {"anchor": anchor, "beat_len": round(60.0 / bpm, 6), "bpm": bpm}

        self._shutdown_audio()                 # 重算要独占读文件（试听可能正开着）
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.lbl_pos.setText("正在按 %.1f BPM 重新分段…" % bpm)
            QApplication.processEvents()
            import beatgrid as _bg
            import soundfile as sf
            data, sr = sf.read(self.path, dtype="float32", always_2d=True)
            sig = np.mean(data, axis=1)
            del data
            m2, s2 = _bg.resample(sig, sr, _bg.SR_TARGET)
            del sig
            f = _bg.features(m2, s2)
            dur = len(m2) / float(s2)
            got = S.auto_segments(f, dur=dur, grid=grid)
            raw = S.auto_segments(f, dur=dur, grid=None, phrase_grid=grid)
            del m2, f
        except Exception as e:                                    # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "重新分段", "重算失败：%s" % e)
            self.lbl_pos.setText("重新分段失败")
            return False
        finally:
            try:
                QApplication.restoreOverrideCursor()
            except Exception:                                     # noqa: BLE001
                pass
        if not got:
            QMessageBox.information(self, "重新分段", "这首算不出段落（特征不足）。")
            return False

        # ---- 落盘：sections + 手动 BPM（写工作库；ERLIB 没有 cfg）----
        gm = dict(self._manual) if isinstance(self._manual, dict) else {}
        gm["bpm_manual"] = float(bpm)
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
            try:
                n1 = w.update_manual(self.sid, gm)
                n2 = w.update_sections(self.sid, [dict(x) for x in got],
                                       [dict(x) for x in raw])
            finally:
                w.close()
            if not (n1 or n2):
                QMessageBox.warning(self, "重新分段", "写盘失败（工作库里找不到这首歌）。")
                return False
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.warning(self, "重新分段", "写盘失败：%s" % e)
            return False

        # ---- 刷新界面（网格 + 色带）----
        self._manual = gm
        self._sections = [dict(x) for x in got]
        self._sections_raw = [dict(x) for x in raw]
        try:
            import beatgrid as _bg2
            self._grid = _bg2.effective_grid((self._song or {}).get("grid") or {}, gm,
                                             self._song)
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
            raw = list(self._sections or [])
        except Exception:                                     # noqa: BLE001
            raw = []
        dlg = EnergySectionDialog(self, sections=raw, dur=self._dur_now(),
                                  grid=self._grid_for_snap(),
                                  cur_sec=float(sec or 0.0))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        got = dlg.sections()
        raw = ([dict(x) for x in got]
               if got else [])
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
            try:
                n = w.update_sections(self.sid, raw, raw)
            finally:
                w.close()
            if not n:
                QMessageBox.warning(self, "能量段落", "保存失败（工作库里找不到这首歌）。")
                return False
        except Exception as e:                                # noqa: BLE001
            QMessageBox.warning(self, "能量段落", "保存失败：%s" % e)
            return False
        self._sections = raw
        self._sections_raw = raw
        try:
            self.wave.set_energy_sections(got)
        except Exception:                                     # noqa: BLE001
            pass
        self._mark_dirty()
        return True

    # ---------------- 保存 / 回退 ----------------
    def _write_manual(self, manual):
        """把 grid_manual 写回工作库 `songs.manual`。成功返回 True。"""
        try:
            w = LibWriter(self.work, root=self.root, resume=True, app_ver="erlib")
            try:
                n = w.update_manual(self.sid, manual)
            finally:
                w.close()
            if not n:
                QMessageBox.warning(self, "歌曲详情", "保存失败（工作库里找不到这首歌）。")
            return bool(n)
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
