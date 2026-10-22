# -*- coding: utf-8 -*-
"""Easy Reality AutoVJ · 能量诊断工具

用**软件内部同一套分析器**（audio_engine._Analyzer）把能量公式的每个分量摊开，
用来回答「为什么这一段能量这么高」以及「现场设备的低频增强影响有多大」。

三个页签：
  1. 文件分析  —— 选一个音频文件，跑完整分析，看能量曲线 + 各分量；可调「假设参数」预览改公式后的效果
  2. 实时监听  —— 用软件自己的采集链路，实时显示每个分量（现场排障用）
  3. 低频增强自检 —— 播放音乐的同时录 12 秒回环，与文件本身的频谱对比，算出系统给低频加了多少 dB

用法：venv\\Scripts\\python tools\\energy_probe.py
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QPointF, QRectF      # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPen                      # noqa: E402
from PySide6.QtWidgets import (                                              # noqa: E402
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QProgressBar, QPushButton, QSlider, QTabWidget, QVBoxLayout,
    QWidget)

APP = "Easy Reality AutoVJ · 能量诊断工具"
COL = {"E": QColor(255, 90, 90), "loud": QColor(90, 160, 255),
       "kick": QColor(255, 190, 60), "bass": QColor(120, 220, 140),
       "fluct": QColor(200, 130, 255), "vary": QColor(255, 255, 255, 140)}


# ------------------------------------------------------------------ 离线分析
class FileAnalyzer(QThread):
    progress = Signal(int, int)
    done = Signal(list, float)          # rows, 时长
    failed = Signal(str)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        import numpy as np
        import soundfile as sf
        import audio_engine as AE
        from audio_engine import _Analyzer, AudioState, BLOCK

        real_time = AE.time

        class FakeClock:                     # 离线喂数据必须用假时钟（拍钟/计时都靠它）
            t = 1e6

            @classmethod
            def perf_counter(cls):
                return cls.t

            @classmethod
            def time(cls):
                return 1.7e9 + cls.t

        try:
            data, sr = sf.read(self.path, always_2d=True, dtype="float32")
        except Exception as e:
            self.failed.emit(f"读不出这个音频（只支持常见音频/视频里的音轨）：{e}")
            return
        mono = data.mean(axis=1)
        total = len(mono) // BLOCK
        if total <= 0:
            self.failed.emit("文件太短")
            return
        an = _Analyzer(sr)
        st = AudioState()
        rows = []
        AE.time = FakeClock
        try:
            for i in range(total):
                if self._stop:
                    return
                FakeClock.t += BLOCK / sr
                chunk = mono[i * BLOCK:(i + 1) * BLOCK].reshape(-1, 1)
                an.feed(chunk, st)
                snap = st.snapshot()
                d = snap.get("dbg") or {}
                rows.append({
                    "t": i * BLOCK / sr,
                    "E": float(snap["energy"]),
                    "loud": float(d.get("loud", 0.0)),      # 响度项（0~1）
                    "kick": float(d.get("kick_e", 0.0)),    # 低频冲击
                    "bass": float(d.get("bass_f", 0.0)),    # 低频能量
                    "fluct": float(d.get("fluct_f", 0.0)),  # 低频起伏
                    "voc": float(d.get("vocal_f", 1.0)),    # 人声惩罚
                    "rel": float(d.get("rel", 0.0)),
                    "onset": float(snap.get("onset_density", 0.0)),
                })
                if i % 200 == 0:
                    self.progress.emit(i, total)
        finally:
            AE.time = real_time                 # 恢复真实时钟（实时监听页要用）
        self.progress.emit(total, total)
        self.done.emit(rows, len(mono) / sr)


# ------------------------------------------------------------------ 曲线控件
class Curve(QWidget):
    """能量曲线 + 分量曲线；点击/拖动移动播放头"""
    moved = Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(240)
        self.rows = []
        self.keys = {"loud": True, "kick": True, "bass": False, "fluct": False}
        self.pos = 0.0
        self.note = ""

    def set_rows(self, rows):
        self.rows = rows
        self.pos = 0.0
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 28))
        W, H = self.width(), self.height()
        pad = 28
        if not self.rows:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignCenter, "选一个音频文件开始分析")
            p.end()
            return
        dur = self.rows[-1]["t"] or 1.0
        # 网格
        p.setPen(QPen(QColor(60, 60, 66), 1))
        for k in range(1, 4):
            y = pad + (H - 2 * pad) * k / 4
            p.drawLine(pad, int(y), W - 8, int(y))
        p.setPen(QColor(120, 120, 126))
        for k in range(0, 5):
            x = pad + (W - pad - 8) * k / 4
            p.drawLine(int(x), pad, int(x), H - pad)
            p.drawText(int(x) - 16, H - 8, "%d:%02d" % (int(dur * k / 4) // 60, int(dur * k / 4) % 60))
        p.drawText(4, pad + 4, "1.0")
        p.drawText(4, H - pad, "0.0")

        def draw(key, color, dashed=False):
            pen = QPen(color, 2)
            if dashed:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            lastx = lasty = None
            step = max(1, len(self.rows) // max(1, W - pad - 8))
            for i in range(0, len(self.rows), step):
                r = self.rows[i]
                x = pad + (W - pad - 8) * (r["t"] / dur)
                y = pad + (H - 2 * pad) * (1.0 - max(0.0, min(1.0, r[key])))
                if lastx is not None:
                    p.drawLine(QPointF(lastx, lasty), QPointF(x, y))
                lastx, lasty = x, y

        for k in ("loud", "kick", "bass", "fluct"):
            if self.keys.get(k):
                draw(k, COL[k])
        draw("E", COL["E"])
        if self.note:
            p.setPen(QColor(255, 255, 255, 170))
            draw(self.note, COL["vary"], dashed=True)
        # 播放头
        x = pad + (W - pad - 8) * (self.pos / dur)
        p.setPen(QPen(QColor(255, 255, 255), 1, Qt.DotLine))
        p.drawLine(int(x), pad, int(x), H - pad)
        p.end()

    def _set_from_x(self, x):
        if not self.rows:
            return
        W, pad = self.width(), 28
        frac = (x - pad) / max(1, (W - pad - 8))
        self.pos = max(0.0, min(1.0, frac)) * (self.rows[-1]["t"] or 1.0)
        self.moved.emit(self.pos)
        self.update()

    def mousePressEvent(self, e):
        self._set_from_x(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons():
            self._set_from_x(e.position().x())

    def value_at(self, t):
        """取 t 时刻所在的那一行"""
        if not self.rows:
            return None
        # 行是均匀的，二分即可
        lo, hi = 0, len(self.rows) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.rows[mid]["t"] < t:
                lo = mid + 1
            else:
                hi = mid
        return self.rows[lo]


# ------------------------------------------------------------------ 通用小组件
def bar_row(label, color):
    lb = QLabel(label)
    lb.setFixedWidth(76)
    v = QLabel("0.00")
    v.setFixedWidth(52)
    v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(0)
    bar.setTextVisible(False)
    bar.setFixedHeight(12)
    bar.setStyleSheet("QProgressBar{border:1px solid #555;background:#1b1b1f;}"
                      "QProgressBar::chunk{background:%s;}" % color)
    return lb, bar, v


# ------------------------------------------------------------------ 页签 1：文件分析
class FileTab(QWidget):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.worker = None
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.btn_open = QPushButton("选择音频文件…")
        self.btn_open.clicked.connect(self.pick)
        self.lbl_file = QLabel("未选择")
        self.lbl_file.setStyleSheet("color:#9a9a9a;")
        top.addWidget(self.btn_open)
        top.addWidget(self.lbl_file, 1)
        v.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        v.addWidget(self.bar)

        self.curve = Curve()
        self.curve.moved.connect(self.on_move)
        v.addWidget(self.curve, 1)

        ck = QHBoxLayout()
        for k, name in (("loud", "响度项"), ("kick", "低频冲击"), ("bass", "低频能量"), ("fluct", "低频起伏")):
            cb = QCheckBox(name)
            cb.setChecked(self.curve.keys[k])
            cb.toggled.connect(lambda on, kk=k: (self.curve.keys.__setitem__(kk, on), self.curve.update()))
            ck.addWidget(cb)
        ck.addStretch(1)
        self.lbl_at = QLabel("拖动曲线看某一点的数值")
        self.lbl_at.setStyleSheet("color:#cfcfcf;")
        ck.addWidget(self.lbl_at)
        v.addLayout(ck)

        g = QGroupBox("该点的分量（能量 E = 响度项×0.4 + 低频证据×0.6，再乘人声惩罚）")
        gg = QGridLayout(g)
        self.rowsb = {}
        for i, (key, name, color) in enumerate((
                ("E", "能量 E", "#ff5a5a"), ("loud", "响度项", "#5aa0ff"),
                ("kick", "低频冲击", "#ffbe3c"), ("bass", "低频能量", "#78dc8c"),
                ("fluct", "低频起伏", "#c882ff"), ("voc", "人声惩罚", "#8fd0ff"))):
            lb, bar, val = bar_row(name, color)
            gg.addWidget(lb, i // 3, (i % 3) * 3)
            gg.addWidget(bar, i // 3, (i % 3) * 3 + 1)
            gg.addWidget(val, i // 3, (i % 3) * 3 + 2)
            self.rowsb[key] = (bar, val)
        self.lbl_extra = QLabel("")
        self.lbl_extra.setStyleSheet("color:#9a9a9a;")
        gg.addWidget(self.lbl_extra, 2, 0, 1, 6)
        v.addWidget(g)

        hypo = QGroupBox("假设参数（预览：改公式后这条曲线会变成什么样，白色虚线）")
        hg = QGridLayout(hypo)
        self.s_w = QSlider(Qt.Horizontal)      # 响度项权重
        self.s_w.setRange(0, 60)
        self.s_w.setValue(40)
        self.s_cap = QSlider(Qt.Horizontal)     # 响度项上限
        self.s_cap.setRange(40, 100)
        self.s_cap.setValue(100)
        self.s_dead = QSlider(Qt.Horizontal)    # 低频冲击死区（越大越难算高）
        self.s_dead.setRange(0, 80)
        self.s_dead.setValue(48)
        self.lbl_h = QLabel("")
        self.lbl_h.setStyleSheet("color:#9a9a9a;")
        for r, (nm, sl) in enumerate((("响度项权重", self.s_w), ("响度项上限", self.s_cap),
                                      ("低频冲击门槛", self.s_dead))):
            hg.addWidget(QLabel(nm), r, 0)
            hg.addWidget(sl, r, 1)
            sl.valueChanged.connect(self.recompute)
        hg.addWidget(self.lbl_h, 3, 0, 1, 2)
        v.addWidget(hypo)

        bot = QHBoxLayout()
        self.btn_csv = QPushButton("导出 CSV")
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_csv.setEnabled(False)
        bot.addWidget(self.btn_csv)
        bot.addStretch(1)
        v.addLayout(bot)

    # ---- 文件 ----
    def pick(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择音频文件", "", "音频 (*.flac *.wav *.mp3 *.m4a *.aac *.ogg *.opus *.aiff *.aif);;所有文件 (*)")
        if not p:
            return
        self.lbl_file.setText(p)
        self.start(p)

    def start(self, path):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.lbl_at.setText("分析中…（长曲子要几十秒）")
        self.worker = FileAnalyzer(path)
        self.worker.progress.connect(lambda d, t: self.bar.setValue(int(d / max(t, 1) * 100)))
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def on_fail(self, msg):
        self.bar.setVisible(False)
        QMessageBox.warning(self, APP, msg)

    def on_done(self, rows, dur):
        self.rows = rows
        self.bar.setVisible(False)
        self.btn_csv.setEnabled(bool(rows))
        self.curve.set_rows(rows)
        self.lbl_at.setText(f"分析完成：{len(rows)} 个采样点，约 {dur:.0f} 秒。拖动曲线看任意时刻的分量。")
        self.recompute()
        self.on_move(0.0)

    # ---- 播放头 ----
    def on_move(self, t):
        r = self.curve.value_at(t)
        if not r:
            return
        for k, (bar, val) in self.rowsb.items():
            v = r[k]
            bar.setValue(int(max(0.0, min(1.0, v)) * 100))
            val.setText(("%.2f" % v) if k not in ("voc",) else ("×%.2f" % v))
        self.lbl_at.setText("%d:%04.1f  能量 %.3f" % (int(r["t"]) // 60, r["t"] % 60, r["E"]))
        self.lbl_extra.setText(
            "相对响度 %+.1f dB（相对 80 秒自适应基线）　低频冲击次数密度（近 4 秒）：%.2f 次/秒"
            % (r["rel"], r["onset"]))

    # ---- 假设参数 ----
    def recompute(self):
        """用记录下来的各分量重算一遍（纯算术，不重新分析音频），
        预览「改公式」后的曲线 —— 用来判断某个改动方向值不值得做。"""
        if not self.rows:
            return
        w = self.s_w.value() / 100.0        # 响度项权重（现状 0.40）
        cap = self.s_cap.value() / 100.0    # 响度项上限（现状 1.00）
        dead = self.s_dead.value() / 100.0  # 低频冲击门槛（现状 0.00，越大越难算高）
        for r in self.rows:
            loud = min(cap, r["loud"])
            kick = r["kick"]
            if dead > 0:
                kick = max(0.0, min(1.0, (kick - dead) / max(1e-6, 1.0 - dead)))
            ev = max(kick, r["bass"], r["fluct"])
            r["vary"] = max(0.0, min(1.0, w * loud + (1.0 - w) * ev))
        self.curve.note = "vary"
        self.curve.update()
        avg = sum(r["vary"] for r in self.rows) / len(self.rows)
        avg0 = sum(r["E"] for r in self.rows) / len(self.rows)
        self.lbl_h.setText("全曲平均：现在 %.3f → 假设后 %.3f（虚线）" % (avg0, avg))

    def export_csv(self):
        if not self.rows:
            return
        p, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "energy.csv", "CSV (*.csv)")
        if not p:
            return
        keys = ["t", "E", "loud", "kick", "bass", "fluct", "voc", "rel", "onset", "vary"]
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write(",".join(keys) + "\n")
            for r in self.rows:
                f.write(",".join("%.5f" % r.get(k, 0.0) for k in keys) + "\n")
        QMessageBox.information(self, APP, "已导出：\n" + p)


# ------------------------------------------------------------------ 页签 2：实时监听
class LiveTab(QWidget):
    def __init__(self):
        super().__init__()
        self.audio = None
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.poll)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("用软件自己的采集链路实时监听（设备取自软件设置里的音源）。"
                           "现场发现能量不对时看这里，能看出是哪一路顶上去的。"))
        top = QHBoxLayout()
        self.btn = QPushButton("开始监听")
        self.btn.clicked.connect(self.toggle)
        self.lbl_dev = QLabel("-")
        self.lbl_dev.setStyleSheet("color:#9a9a9a;")
        top.addWidget(self.btn)
        top.addWidget(self.lbl_dev, 1)
        v.addLayout(top)
        self.big = QLabel("E 0.000")
        f = QFont()
        f.setPointSize(26)
        f.setBold(True)
        self.big.setFont(f)
        v.addWidget(self.big)
        g = QGroupBox("实时分量")
        gg = QGridLayout(g)
        self.rowsb = {}
        for i, (k, name, color) in enumerate((
                ("loud", "响度项", "#5aa0ff"), ("kick", "低频冲击", "#ffbe3c"),
                ("bass", "低频能量", "#78dc8c"), ("fluct", "低频起伏", "#c882ff"),
                ("voc", "人声惩罚", "#8fd0ff"))):
            lb, bar, val = bar_row(name, color)
            gg.addWidget(lb, i // 3, (i % 3) * 3)
            gg.addWidget(bar, i // 3, (i % 3) * 3 + 1)
            gg.addWidget(val, i // 3, (i % 3) * 3 + 2)
            self.rowsb[k] = (bar, val)
        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color:#9a9a9a;")
        gg.addWidget(self.lbl_info, 2, 0, 1, 6)
        v.addWidget(g)
        v.addStretch(1)

    def toggle(self):
        if self.audio is None:
            import audio_engine
            self.audio = audio_engine.AudioEngine()
            cfg = self.load_audio_cfg()
            try:
                self.audio.start(cfg.get("source_type", "system"),
                                 cfg.get("device_name", ""),
                                 mono=bool(cfg.get("mono", True)),
                                 sr=int(cfg.get("sample_rate", 0) or 0))
            except Exception as e:
                QMessageBox.warning(self, APP, f"启动采集失败：{e}")
                self.audio = None
                return
            self.btn.setText("停止监听")
            self.timer.start()
        else:
            self.timer.stop()
            try:
                self.audio.shutdown()
            except Exception:
                pass
            self.audio = None
            self.btn.setText("开始监听")

    @staticmethod
    def load_audio_cfg():
        for p in (os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "AutoVJ", "config.json"),
                  os.path.join(_ROOT, "config.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    return (json.load(f).get("audio") or {})
            except Exception:
                continue
        return {}

    def poll(self):
        if self.audio is None:
            return
        snap = self.audio.state.snapshot()
        d = snap.get("dbg") or {}
        self.big.setText("E %.3f" % snap["energy"])
        vals = {"loud": d.get("loud", 0.0), "kick": d.get("kick_e", 0.0),
                "bass": d.get("bass_f", 0.0), "fluct": d.get("fluct_f", 0.0),
                "voc": d.get("vocal_f", 1.0)}
        for k, (bar, val) in self.rowsb.items():
            v = float(vals.get(k, 0.0))
            bar.setValue(int(max(0.0, min(1.0, v)) * 100))
            val.setText(("%.2f" % v) if k != "voc" else ("×%.2f" % v))
        self.lbl_dev.setText(snap.get("device_desc") or "-")
        b, m, h = snap.get("bass", 0), snap.get("mid", 0), snap.get("high", 0)
        tot = max(1e-9, b + m + h)
        self.lbl_info.setText(
            "低频占比 %.0f%%  中频 %.0f%%  高频 %.0f%% ｜ 相对响度 %+.1f dB ｜ 打击密度 %.2f 次/秒 ｜ BPM %.0f"
            % (b / tot * 100, m / tot * 100, h / tot * 100, d.get("rel", 0.0),
               snap.get("onset_density", 0.0), snap.get("bpm", 0)))

    def shutdown(self):
        self.timer.stop()
        if self.audio is not None:
            try:
                self.audio.shutdown()
            except Exception:
                pass
            self.audio = None


# ------------------------------------------------------------------ 页签 3：低频增强自检
class BoostTab(QWidget):
    """录 12 秒回环，与音频文件本身的平均频谱对比 → 系统给各频段加/减了多少 dB"""
    BANDS = [(20, 60), (60, 120), (120, 250), (250, 500), (500, 1000),
             (1000, 2000), (2000, 4000), (4000, 8000), (8000, 12000)]

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.addWidget(QLabel(
            "用法：① 在播放器/DJ 软件里播放一首歌（就用你平时放的方式）\n"
            "　　　② 点下面的按钮，工具录 12 秒系统回环\n"
            "　　　③ 再选这首歌的音频文件（不用对齐时间，比的是「整体频谱形状」）\n"
            "结论会直接告诉你：系统把哪个频段抬高/压低了，抬高多少 dB。"))
        row = QHBoxLayout()
        self.btn_rec = QPushButton("① 录 12 秒回环")
        self.btn_rec.clicked.connect(self.record)
        self.btn_file = QPushButton("② 选择对应音频文件")
        self.btn_file.clicked.connect(self.pick_file)
        self.btn_cmp = QPushButton("③ 对比")
        self.btn_cmp.clicked.connect(self.compare)
        self.btn_cmp.setEnabled(False)
        for b in (self.btn_rec, self.btn_file, self.btn_cmp):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#9a9a9a;")
        v.addWidget(self.lbl)
        self.txt = QLabel("")
        self.txt.setStyleSheet("font-family:Consolas,monospace;color:#ddd;")
        self.txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.txt)
        v.addStretch(1)
        self.rec = None
        self.file = ""

    def record(self):
        try:
            import soundcard as sc
            import numpy as np
        except Exception as e:
            QMessageBox.warning(self, APP, f"没有 soundcard：{e}")
            return
        cfg = LiveTab.load_audio_cfg()
        spk = None
        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(str(spk.name), include_loopback=True)
        except Exception as e:
            QMessageBox.warning(self, APP, f"找不到回环设备：{e}")
            return
        self.lbl.setText("录音中… 请让音乐一直响着（12 秒）")
        QApplication.processEvents()
        try:
            with mic.recorder(samplerate=48000, channels=2) as r:
                data = r.record(numframes=48000 * 12)
            self.rec = np.asarray(data).mean(axis=1)
            self.btn_cmp.setEnabled(bool(self.file))
            self.lbl.setText("录音完成：12 秒。")
        except Exception as e:
            self.lbl.setText(f"录音失败：{e}")

    def pick_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择对应的音频文件", "",
                                          "音频 (*.flac *.wav *.mp3 *.m4a *.aac *.ogg *.opus)")
        if p:
            self.file = p
            self.lbl.setText("已选择文件。" + ("录音也有，可以对比了。" if self.rec is not None else "再点①录音。"))
            self.btn_cmp.setEnabled(self.rec is not None)

    def _band_db(self, sig, sr):
        import numpy as np
        n = 8192
        acc = np.zeros(n // 2 + 1)
        cnt = 0
        hop = n // 2
        for i in range(0, max(1, len(sig) - n), hop):
            seg = sig[i:i + n] * np.hanning(n)
            acc += np.abs(np.fft.rfft(seg))
            cnt += 1
        spec = acc / max(cnt, 1)
        fr = np.fft.rfftfreq(n, 1.0 / sr)
        out = []
        for a, b in self.BANDS:
            m = (fr >= a) & (fr < b)
            out.append(float(np.sqrt((spec[m] ** 2).sum())) + 1e-12)
        return out

    def compare(self):
        import numpy as np
        import soundfile as sf
        try:
            data, sr = sf.read(self.file, always_2d=True, dtype="float32")
        except Exception as e:
            self.lbl.setText(f"读文件失败：{e}")
            return
        ref = self._band_db(data.mean(axis=1), sr)
        got = self._band_db(self.rec, 48000)
        ref = np.array(ref) / ref[5]          # 以 1~2kHz 为基准归一
        got = np.array(got) / got[5]
        diff = 20 * np.log10(got / ref)
        lines = ["频段            文件(相对1k)   系统回环(相对1k)   差值",
                 "-" * 58]
        for (a, b), d, r, g in zip(self.BANDS, diff, ref, got):
            lines.append("%6d-%-6d  %+7.1f dB   %+7.1f dB   %+6.1f dB"
                         % (a, b, 20 * np.log10(r), 20 * np.log10(g), d))
        low = float(np.mean(diff[:3]))
        mid = float(np.mean(diff[3:6]))
        high = float(np.mean(diff[6:]))
        lines.append("-" * 58)
        lines.append("低频(20~250Hz) 平均 %+.1f dB   中频(250~2k) %+.1f dB   高频(2k+) %+.1f dB"
                     % (low, mid, high))
        if low - mid > 2.0:
            lines.append("")
            lines.append("结论：**系统在给低频加料**（比中频高 %.1f dB）。"
                         "这类增强会让软件的能量判高——因为低频是能量的主要依据。" % (low - mid))
            lines.append("　可以关掉：Windows「声音」→ 播放设备属性 → 增强（Bass Boost/响度均衡），"
                         "或 Realtek / 主板音效面板里的低音增强、Dolby/DTS 后处理。")
            lines.append("　软件侧也可以补偿：把「实验性 → 能量校正」往右拉（但那只压中段，"
                         "压不动接近 1.0 的高值）。")
        elif mid - low > 2.0:
            lines.append("")
            lines.append("结论：低频反而被削了 %.1f dB（某些「清晰度/对话增强」会这样），"
                         "同样会让能量判低。" % (mid - low))
        else:
            lines.append("")
            lines.append("结论：低频与中频差 %.1f dB，基本是平的 —— 系统没有明显加料。" % (low - mid))
        self.txt.setText("\n".join(lines))


# ------------------------------------------------------------------ 主窗口
class Probe(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.resize(1000, 760)
        v = QVBoxLayout(self)
        tabs = QTabWidget()
        self.f = FileTab()
        self.l = LiveTab()
        self.b = BoostTab()
        tabs.addTab(self.f, "文件分析")
        tabs.addTab(self.l, "实时监听")
        tabs.addTab(self.b, "低频增强自检")
        v.addWidget(tabs)

    def closeEvent(self, e):
        self.l.shutdown()
        if self.f.worker is not None and self.f.worker.isRunning():
            self.f.worker.stop()
            self.f.worker.wait(2000)
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = Probe()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
