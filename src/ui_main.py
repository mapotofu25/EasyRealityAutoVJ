# -*- coding: utf-8 -*-
"""主窗口 UI（Arena 风格布局）

布局：
  顶部工具条：返回/开始/黑场/冻结/暂停自动/下一素材/音源(含迷你电平)/语言/快捷键
  ┌──────────────┬──────────────────────────────┬──────────────┐
  │ 图层头(左列)   │ 素材格(一行一个图层)            │ 设置面板       │
  ├──────────────┴────────┬─────────────────────┤ (折叠分区)     │
  │ 输出预览+HUD+下一素材预看 │ 素材库(网格+右键+拖拽)  │              │
  └───────────────────────┴─────────────────────┴──────────────┘
  首页：三步（选音源→导素材→开始）
"""
import os
import shutil
import time
import math
import ctypes

from PySide6.QtCore import Qt, QTimer, QSize, QThread, Signal, QPoint, QObject, QEvent
from PySide6.QtGui import QImage, QPixmap, QAction, QKeySequence, QIcon, QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QPushButton, QSlider, QCheckBox, QSpinBox, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QSplitter, QStackedWidget,
    QGroupBox, QStatusBar, QDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QApplication, QProgressBar, QAbstractItemView, QInputDialog,
    QToolBox, QScrollArea, QMenu, QFrame, QSizePolicy, QLineEdit, QButtonGroup,
    QRadioButton,
)

import i18n
from i18n import tr, set_lang, T, Tf
import theme
from config import Config, CONFIG_FILE, exe_dir
from tags_def import DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER, DYNAMIC_TAGS
from audio_engine import AudioEngine
from media_manager import MediaItem, ThumbWorker, ThumbBackfillWorker, scan_folder
from engine import AutoVJEngine, Layer, MODE_LABELS, BLEND_MODES, BLEND_LABELS
from output_window import OutputWindow
from hotkeys import HotkeyManager
from panels import (LayerStackPanel, PreviewPanel, AudioSourceDialog, MiniLevel,
                    LibraryGrid, CollapsibleSection, CLIP_MIME, LIB_THUMB_SIZES,
                    NewLayerDialog, I18nDialog)

MEDIA_FILTER = "媒体素材 (*.mp4 *.mov *.webm *.avi *.mkv *.gif *.png *.jpg *.jpeg *.bmp *.webp);;All Files (*)"
MUSIC_EXTS = {".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".aac", ".ogg", ".opus", ".wma"}
MUSIC_FILTER = "音频文件 (*.flac *.mp3 *.m4a *.wav *.aiff *.aif *.aac *.ogg *.opus *.wma);;All Files (*)"


def scan_music_folder(folder):
    """递归扫描文件夹，返回音频文件路径列表（音乐曲库用）"""
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in MUSIC_EXTS:
                out.append(os.path.join(root, f))
    return out


def cpu_percent():
    """轻量 CPU 占用率（GetSystemTimes）"""
    import ctypes.wintypes as wt

    class FT(wt.FILETIME):
        pass

    kern_idle = FT()
    kern_user = FT()
    kern_sys = FT()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(kern_idle), ctypes.byref(kern_user), ctypes.byref(kern_sys)):
        return 0.0
    now = (kern_idle.dwLowDateTime | kern_idle.dwHighDateTime << 32,
           (kern_user.dwLowDateTime | kern_user.dwHighDateTime << 32) +
           (kern_sys.dwLowDateTime | kern_sys.dwHighDateTime << 32))
    if not hasattr(cpu_percent, "_prev"):
        cpu_percent._prev = now
        return 0.0
    pi, pt = cpu_percent._prev
    di = now[0] - pi
    dt = now[1] - pt
    cpu_percent._prev = now
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - di / dt) * 100))


class SettingsPanel(QWidget):
    """右侧设置：折叠分区（行为模式 / 效果 / 逐拍交替 / 输出）"""

    def __init__(self, main):
        super().__init__()
        self.main = main
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(6)
        box = QVBoxLayout()
        box.setSpacing(6)
        outer.addLayout(box)
        self._sections = {}

        # ---- Kv 主视觉图层（置顶分区；仅当存在 Kv 图层时显示）----
        pk = QWidget()
        gk = QGridLayout(pk)
        gk.setVerticalSpacing(4)
        gk.addWidget(QLabel("静音判定阈值"), 0, 0)
        self.kv_db = QSlider(Qt.Horizontal)
        self.kv_db.setRange(-600, -200)      # ×10 → -60.0 ~ -20.0 dB
        self.kv_db.setValue(-500)
        self.kv_db.setToolTip("电平低于该值视为静音；持续超过「静音触发延迟」后待机层切入")
        self.kv_db_lbl = QLabel("-50.0 dB")
        self.kv_db_lbl.setMinimumWidth(58)
        self.kv_db_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gk.addWidget(self.kv_db, 0, 1)
        gk.addWidget(self.kv_db_lbl, 0, 2)
        self.btn_noise_floor = QPushButton("自动检测底噪")
        self.btn_noise_floor.setToolTip("监听当前环境 3 秒，把阈值设为比底噪高 3dB（防场地设备噪音导致待机层乱切）")
        gk.addWidget(self.btn_noise_floor, 1, 1, 1, 2)
        gk.addWidget(QLabel("静音触发延迟"), 2, 0)
        self.kv_silent = QSlider(Qt.Horizontal)
        self.kv_silent.setRange(0, 100)      # ×10 → 0.0 ~ 10.0 s
        self.kv_silent.setValue(20)
        self.kv_silent.setToolTip("声音消失后必须持续静音这么多秒，待机层才切入（防半拍停顿导致突兀闪现）")
        self.kv_silent_lbl = QLabel("2.0 s")
        self.kv_silent_lbl.setMinimumWidth(58)
        self.kv_silent_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gk.addWidget(self.kv_silent, 2, 1)
        gk.addWidget(self.kv_silent_lbl, 2, 2)
        gk.addWidget(QLabel("声音恢复切出延迟"), 3, 0)
        self.kv_resume = QSlider(Qt.Horizontal)
        self.kv_resume.setRange(1, 50)       # ×10 → 0.1 ~ 5.0 s
        self.kv_resume.setValue(25)
        self.kv_resume.setToolTip("检测到声音恢复后，持续有声这么多秒才把待机层切出")
        self.kv_resume_lbl = QLabel("2.5 s")
        self.kv_resume_lbl.setMinimumWidth(58)
        self.kv_resume_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gk.addWidget(self.kv_resume, 3, 1)
        gk.addWidget(self.kv_resume_lbl, 3, 2)
        sep_in = QLabel("切入（没声音时把画面带进来）")
        sep_in.setStyleSheet("color:#8ab;font-weight:bold;")
        gk.addWidget(sep_in, 4, 0, 1, 3)
        gk.addWidget(QLabel("切入方式"), 5, 0)
        self.kv_in_trans = QComboBox()
        self.kv_in_trans.addItems(["淡入（Fade）", "硬切（Cut）", "故障（Glitch）"])
        gk.addWidget(self.kv_in_trans, 5, 1, 1, 2)
        gk.addWidget(QLabel("切入时长"), 6, 0)
        self.kv_in_dur = QSlider(Qt.Horizontal)
        self.kv_in_dur.setRange(1, 50)       # ×10 → 0.1 ~ 5.0 s
        self.kv_in_dur.setValue(15)
        self.kv_in_dur_lbl = QLabel("1.5 s")
        self.kv_in_dur_lbl.setMinimumWidth(58)
        self.kv_in_dur_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gk.addWidget(self.kv_in_dur, 6, 1)
        gk.addWidget(self.kv_in_dur_lbl, 6, 2)
        sep_out = QLabel("切出（有声音时把画面带出去）")
        sep_out.setStyleSheet("color:#8ab;font-weight:bold;")
        gk.addWidget(sep_out, 7, 0, 1, 3)
        gk.addWidget(QLabel("切出方式"), 8, 0)
        self.kv_out_trans = QComboBox()
        self.kv_out_trans.addItems(["淡出（Fade）", "硬切（Cut）", "缩放（Zoom）"])
        gk.addWidget(self.kv_out_trans, 8, 1, 1, 2)
        gk.addWidget(QLabel("切出时长"), 9, 0)
        self.kv_out_dur = QSlider(Qt.Horizontal)
        self.kv_out_dur.setRange(1, 30)      # ×10 → 0.1 ~ 3.0 s
        self.kv_out_dur.setValue(15)
        self.kv_out_dur_lbl = QLabel("1.5 s")
        self.kv_out_dur_lbl.setMinimumWidth(58)
        self.kv_out_dur_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gk.addWidget(self.kv_out_dur, 9, 1)
        gk.addWidget(self.kv_out_dur_lbl, 9, 2)
        gk.addWidget(QLabel("素材切换速度"), 10, 0)
        self.kv_switch = QSlider(Qt.Horizontal)
        self.kv_switch.setRange(0, 600)      # ×10 → 0.0 ~ 60.0 s
        self.kv_switch.setValue(0)
        self.kv_switch.setToolTip("Kv 显示期间每隔多少秒换下一个素材；0 = 不轮换（保持当前素材）")
        self.kv_switch_lbl = QLabel("0.0 s")
        self.kv_switch_lbl.setMinimumWidth(58)
        self.kv_switch_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gk.addWidget(self.kv_switch, 10, 1)
        gk.addWidget(self.kv_switch_lbl, 10, 2)
        hint_kv = QLabel("待机层：没声音时把画面带进来、有声音时带出去。完全独立，"
                         "不参与自动切换，也不受其它任何设置影响。")
        hint_kv.setWordWrap(True)
        hint_kv.setStyleSheet("color:#999;font-size:11px;")
        gk.addWidget(hint_kv, 11, 0, 1, 3)
        gk.setRowStretch(12, 1)
        self._add_section(box, "Kv 主视觉图层", pk)
        self._kv_noise_timer = None

        # ---- 行为模式 ----
        p1 = QWidget()
        gm = QGridLayout(p1)
        gm.addWidget(QLabel("素材切换节奏"), 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["自动（推荐）", "常规切", "快切", "逐拍交替"])
        gm.addWidget(self.mode_combo, 0, 1)
        self.lbl_cur_mode = QLabel("当前节奏: -")
        self.lbl_cur_mode.setStyleSheet("color:#7ab;")
        gm.addWidget(self.lbl_cur_mode, 1, 0, 1, 2)
        gm.addWidget(QLabel("灵敏度"), 2, 0)
        self.sens = QComboBox()
        self.sens.addItems(["低", "中", "高"])
        gm.addWidget(self.sens, 2, 1)
        gm.setRowStretch(3, 1)
        self._add_section(box, "视觉行为模式", p1)

        # ---- 效果 ----
        p2 = QWidget()
        ga = QGridLayout(p2)
        ga.addWidget(QLabel("画面振幅强度"), 0, 0)
        self.intensity = QComboBox()
        self.intensity.addItems([tr("intensity_low"), tr("intensity_mid"),
                                 tr("intensity_high"), tr("intensity_xhigh")])
        ga.addWidget(self.intensity, 0, 1)
        ga.addWidget(QLabel("过渡方式"), 1, 0)
        self.transition = QComboBox()
        self.transition.addItems(["跟随模式", "淡入淡出", "硬切", "滑动", "缩放", "故障"])
        self.transition.setToolTip("跟随模式：慢切/常规=淡入淡出，逐拍=硬切")
        ga.addWidget(self.transition, 1, 1)
        self.energy_map = QCheckBox("能量映射")
        self.bpm_sync = QCheckBox("BPM Sync 视频速度")
        ga.addWidget(self.energy_map, 2, 0, 1, 2)
        ga.addWidget(self.bpm_sync, 3, 0, 1, 2)
        ga.addWidget(QLabel("渲染帧率"), 4, 0)
        self.render_fps = QComboBox()
        self.render_fps.addItems(["60", "30", "20"])
        self.render_fps.setToolTip("引擎每秒合成多少次画面；素材多为 25 到 30fps，选 60 时约有一半是把同一帧重复合成 —— 机器吃紧（同时开 VDJ、直播、录制）就选 30，能省约一半渲染 CPU，画面仍跟得上素材")
        ga.addWidget(self.render_fps, 4, 1)
        self.chk_gpu = QCheckBox("GPU 解码（实验）")
        self.chk_gpu.setToolTip("让显卡硬件解压 DXV 素材（帧内 DXT 压缩块直传显存），降低解码 CPU 占用：DXT5 素材约省一半以上、超宽素材约省七成；解压失败或显卡不支持时自动回退软解，不影响播放。默认关闭，建议先试播一轮再上台")
        ga.addWidget(self.chk_gpu, 5, 0, 1, 2)
        ga.setRowStretch(6, 1)
        self._add_section(box, "效果", p2)

        # ---- 颜色渲染（一键调色：让同一批素材看起来不一样）----
        pc = QWidget()
        gc = QGridLayout(pc)
        gc.setVerticalSpacing(4)
        self.chk_color_on = QCheckBox("启用颜色渲染")
        self.chk_color_on.setToolTip("给画面整体调色，降低对素材数量的依赖；会让同一批素材反复看也不腻")
        gc.addWidget(self.chk_color_on, 0, 0, 1, 3)
        self.chk_color_auto = QCheckBox("自动模式：跟随音乐能量自动变色")
        self.chk_color_auto.setToolTip("关掉后只有点色卡才染色")
        gc.addWidget(self.chk_color_auto, 1, 0, 1, 3)
        gc.addWidget(QLabel("色卡（点一下临时锁定该色）"), 2, 0, 1, 3)
        self.color_palette_box = QWidget()
        gpal = QGridLayout(self.color_palette_box)
        gpal.setContentsMargins(0, 0, 0, 0)
        gpal.setSpacing(3)
        self.color_btns = {}
        from colorfx import PALETTES as _PAL
        for i, (pname, prgb, _kind) in enumerate(_PAL):
            b = QPushButton(T(pname))
            b.setProperty("_i18nDynamic", True)   # 文本=色名、tooltip 含色名，动态重建
            b.setCheckable(True)
            b.setFixedHeight(24)
            r, g, bch = prgb
            if pname == "原色":
                b.setStyleSheet("QPushButton{border:1px solid #666;background:#2b2b2b;color:#ddd;"
                                "border-radius:4px;padding:0;}"
                                "QPushButton:checked{border:1px solid #fff;background:#3a3a3a;}")
            else:
                txt = "#111" if (r * 0.299 + g * 0.587 + bch * 0.114) > 150 else "#fff"
                b.setStyleSheet(
                    f"QPushButton{{border:1px solid #555;background:rgb({r},{g},{bch});"
                    f"color:{txt};border-radius:4px;padding:0;font-size:11px;}}"
                    f"QPushButton:checked{{border:2px solid #fff;}}")
            b.setToolTip(Tf("锁定为「{}」（10 秒后自动回到自动变色）", T(pname)))
            b.clicked.connect(lambda _=False, n=pname: self.main.set_color_palette(n))
            gpal.addWidget(b, i // 4, i % 4)
            self.color_btns[pname] = b
        gc.addWidget(self.color_palette_box, 3, 0, 1, 3)
        self.btn_color_bypass = QPushButton(T("Bypass：原片 / 调色"))
        self.btn_color_bypass.setProperty("_i18nDynamic", True)   # 文本随旁路状态变化
        self.btn_color_bypass.setCheckable(True)
        self.btn_color_bypass.setFixedHeight(30)
        self.btn_color_bypass.setToolTip(T("一键在「调色版」和「原片」之间闪切（热键也可）"))
        gc.addWidget(self.btn_color_bypass, 4, 0, 1, 3)
        gc.addWidget(QLabel("染色强度"), 5, 0)
        self.color_strength = QSlider(Qt.Horizontal)
        self.color_strength.setRange(0, 100)
        self.color_strength.setValue(60)
        self.color_strength.setToolTip("整体浓淡：防止颜色过重掩盖素材细节")
        self.color_strength_lbl = QLabel("60%")
        self.color_strength_lbl.setMinimumWidth(44)
        self.color_strength_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gc.addWidget(self.color_strength, 5, 1)
        gc.addWidget(self.color_strength_lbl, 5, 2)
        self.btn_color_adv = QPushButton("高级设置 ▸")
        self.btn_color_adv.setCheckable(True)
        self.btn_color_adv.setFixedHeight(22)
        gc.addWidget(self.btn_color_adv, 6, 0, 1, 3)
        self.color_adv = QWidget()
        gca = QGridLayout(self.color_adv)
        gca.setContentsMargins(0, 0, 0, 0)
        gca.setVerticalSpacing(4)
        gca.addWidget(QLabel("调色方式"), 0, 0)
        self.color_mode = QComboBox()
        self.color_mode.addItems(["LUT 调色（保留素材色彩）", "双色调（最像换了一个素材）"])
        self.color_mode.setToolTip("LUT：整体调色，细节保留最好（1.7ms/帧）\n"
                                   "双色调：灰度重映射成双色渐变，视觉差异最大，但浅色素材会变海报感")
        gca.addWidget(self.color_mode, 0, 1, 1, 2)
        gca.addWidget(QLabel("色相轮换时间"), 1, 0)
        self.color_rotate = QSlider(Qt.Horizontal)
        self.color_rotate.setRange(1, 60)
        self.color_rotate.setValue(45)
        self.color_rotate.setToolTip("高能量染色状态下，每隔多少秒平滑轮换一种颜色（1~60 秒）\n"
                                     "设得很小（1~3 秒）就是快速变色，注意别和频闪素材叠在一起")
        self.color_rotate_lbl = QLabel("45 s")
        self.color_rotate_lbl.setMinimumWidth(44)
        self.color_rotate_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        gca.addWidget(self.color_rotate, 1, 1)
        gca.addWidget(self.color_rotate_lbl, 1, 2)
        self.chk_color_burst = QCheckBox("能量突变强制刷新颜色")
        gca.addWidget(self.chk_color_burst, 2, 0, 1, 3)
        gca.addWidget(QLabel("频闪素材"), 3, 0)
        self.color_flicker = QComboBox()
        self.color_flicker.addItems(["休眠（防闪+变色叠加）", "不休眠（照常染色）", "随机（每次换素材掷一次）"])
        self.color_flicker.setToolTip(
            "遇到带「频闪」标签的素材时，颜色渲染怎么办：\n"
            "休眠：颜色停掉，避免画面在闪、颜色同时在变\n"
            "不休眠：照常染色（想要颜色一致就选这个）\n"
            "随机：每次换到新素材随机决定休眠或染色，整段素材保持不变\n"
            "注：手动点色卡锁定的颜色永远优先，不受这一项影响")
        gca.addWidget(self.color_flicker, 3, 1, 1, 2)
        self.btn_color_auto_back = QPushButton("恢复自动变色")
        self.btn_color_auto_back.setToolTip("手动锁定色卡后，点这里立刻回到自动模式")
        gca.addWidget(self.btn_color_auto_back, 4, 0, 1, 3)
        self.color_adv.setVisible(False)
        gc.addWidget(self.color_adv, 7, 0, 1, 3)
        self.lbl_color_state = QLabel("")
        self.lbl_color_state.setWordWrap(True)
        self.lbl_color_state.setStyleSheet("color:#7ab;font-size:11px;")
        gc.addWidget(self.lbl_color_state, 8, 0, 1, 3)
        gc.setRowStretch(9, 1)
        self._add_section(box, "颜色渲染", pc)

        # ---- 后处理 ----
        p4 = QWidget()
        pf = QGridLayout(p4)
        pf.setVerticalSpacing(3)
        self.chk_postfx_on = QCheckBox("启用后处理")
        pf.addWidget(self.chk_postfx_on, 0, 0)
        pf.addWidget(QLabel("模式"), 0, 1)
        self.postfx_mode = QComboBox()
        self.postfx_mode.addItems(["自动（推荐）", "手动"])
        self.postfx_mode.setToolTip("自动：根据曲风和能量自动挑选 2~3 个合适的效果并随高潮增强\n手动：自己逐个开效果、调强度")
        pf.addWidget(self.postfx_mode, 0, 2)

        # --- 自动模式页 ---
        self.postfx_auto_page = QWidget()
        pa = QGridLayout(self.postfx_auto_page)
        pa.setContentsMargins(0, 0, 0, 0)
        pa.addWidget(QLabel("全局强度"), 0, 0)
        self.postfx_global = QSlider(Qt.Horizontal)
        self.postfx_global.setRange(0, 100)
        self.postfx_global.setToolTip("整体效果强度：自动挑出的效果都按这个强度为上限")
        pa.addWidget(self.postfx_global, 0, 1)
        self.postfx_auto = QCheckBox("自动联动（随曲风/音乐触发）")
        self.postfx_auto.setToolTip("关掉则效果只随能量变化，不跟曲风/事件")
        pa.addWidget(self.postfx_auto, 1, 0, 1, 2)
        pa.addWidget(QLabel("效果保持"), 2, 0)
        self.postfx_hold = QComboBox()
        self.postfx_hold.addItems(["实时（跟随能量）", "8 拍", "16 拍", "32 拍", "64 拍"])
        self.postfx_hold.setToolTip(
            "自动模式下，选好的一组效果保持多少拍之后再重新挑下一组。\n"
            "「实时」= 跟着能量即时变化（能量在门槛附近抖动时，效果会忽有忽无）；\n"
            "设成 8~64 拍则每组效果稳定停留一段时间，变化更有节奏感。")
        pa.addWidget(self.postfx_hold, 2, 1)
        hint = QLabel("自动模式按当前曲风挑效果，强度跟着音乐能量连续变化。\n"
                      "效果强度 = 能量 × 全局强度的 60%"
                      "（全局强度只决定「多强」，不影响多久换一次效果）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#999;")
        pa.addWidget(hint, 3, 0, 1, 2)
        pf.addWidget(self.postfx_auto_page, 1, 0, 1, 3)

        # --- 手动模式页 ---
        self.postfx_manual_page = QWidget()
        pm = QGridLayout(self.postfx_manual_page)
        pm.setContentsMargins(0, 0, 0, 0)
        pm.setVerticalSpacing(2)
        self.postfx_checks = {}
        self.postfx_sliders = {}
        self.postfx_gears = {}
        POSTFX = [
            ("chromatic", "色差", "红/蓝通道左右分离，边缘泛紫边（复古镜头感）"),
            ("bloom", "辉光", "亮部向外扩散柔光，暗场更梦幻"),
            ("glitch", "故障位移", "随机横向撕裂 + 色块跳位（数字故障感）"),
            ("exposure", "曝光脉冲", "整幅画面的亮暗随音乐跳动"),
            ("saturation", "饱和度脉冲", "颜色的浓淡随音乐跳动（高能量更艳）"),
            ("softfocus", "柔焦", "整体柔化：模糊 + 轻微发光（缓拍段更柔）"),
            ("trails", "拖影", "上一帧的残影叠上来，运动留下拖尾"),
            ("grain", "颗粒", "叠加噪点，胶片颗粒质感"),
            ("pixelate", "像素化", "马赛克方块（像素风）"),
            ("spectrum", "径向频谱", "画面中心叠加 24 段环形频谱：低频橙红→高频青，随音乐跳动"),
            ("deform", "音频反应变形", "画面切成 24 条横带，每条按对应频段强度左右错位（低频错得最多，撕裂/扭动感）"),
        ]
        for i, (key, label, desc) in enumerate(POSTFX):
            cb = QCheckBox(label)
            cb.setToolTip(desc)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setToolTip(desc + "\n\n强度：0 无效果，100 最强")
            gear = QPushButton("⚙")
            gear.setFixedSize(26, 20)
            gear.setToolTip("触发方式（常驻/曲风/事件）、音频驱动等详细设置")
            pm.addWidget(cb, i, 0)
            pm.addWidget(sl, i, 1)
            pm.addWidget(gear, i, 2)
            self.postfx_checks[key] = cb
            self.postfx_sliders[key] = sl
            self.postfx_gears[key] = gear
        hint_m = QLabel("手动模式独立生效：这里的强度就是它自己的数值，"
                        "不受上面「全局强度」影响（全局强度只作用于自动模式）。")
        hint_m.setWordWrap(True)
        hint_m.setStyleSheet("color:#999;font-size:11px;")
        pm.addWidget(hint_m, len(POSTFX), 0, 1, 3)
        pf.addWidget(self.postfx_manual_page, 2, 0, 1, 3)
        pf.setRowStretch(3, 1)
        self._add_section(box, "后处理", p4)

        # ---- 逐拍交替参数 ----
        p3 = QWidget()
        gb = QGridLayout(p3)
        gb.addWidget(QLabel(tr("pick_mode")), 0, 0)
        self.beat_pick = QComboBox()
        self.beat_pick.addItems([tr("order_seq"), tr("order_rand")])
        gb.addWidget(self.beat_pick, 0, 1)
        gb.addWidget(QLabel("交替间隔"), 1, 0)
        self.beat_interval = QComboBox()
        self.beat_interval.addItems(["1/4 拍", "1/2 拍", "1 拍", "2 拍", "4 拍", "8 拍", "16 拍"])
        self.beat_interval.setToolTip("一对素材里 A↔B 每隔多久交替一次（默认每 1 拍交替）")
        self.beat_interval.setCurrentIndex(2)
        gb.addWidget(self.beat_interval, 1, 1)
        gb.addWidget(QLabel("每对小节数(换素材周期)"), 2, 0)
        self.beat_bars = QSpinBox()
        self.beat_bars.setRange(1, 16)
        gb.addWidget(self.beat_bars, 2, 1)
        gb.addWidget(QLabel(tr("end_action")), 3, 0)
        self.beat_end = QComboBox()
        self.beat_end.addItems([tr("loop"), tr("reverse")])
        gb.addWidget(self.beat_end, 3, 1)
        gb.setRowStretch(4, 1)
        self._add_section(box, "逐拍交替参数", p3)

        # ---- 实验性（默认折叠）----
        p5 = QWidget()
        ef = QGridLayout(p5)
        ef.setVerticalSpacing(3)
        ef.addWidget(QLabel("能量校正"), 0, 0)
        self.energy_scale = QSlider(Qt.Horizontal)
        self.energy_scale.setRange(50, 200)   # 伽马 0.5 ~ 2.0
        self.energy_scale.setValue(100)
        self.energy_scale.setToolTip("伽马校正：>1 压低人声段能量、鼓点段基本不动。本机系统低频增强导致人声段虚高时往右拉")
        self.energy_scale_lbl = QLabel("×1.0")
        self.energy_scale_lbl.setMinimumWidth(48)
        self.energy_scale_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ef.addWidget(self.energy_scale, 0, 1)
        ef.addWidget(self.energy_scale_lbl, 0, 2)
        ef.addWidget(QLabel("实验性：现场系统低频增强导致能量虚高时用。默认 1.0 不校正。"), 1, 0, 1, 3)
        ef.setRowStretch(2, 1)
        self._add_section(box, "实验性", p5, default=False)

        # 信号
        self.kv_db.valueChanged.connect(self._on_kv_db)
        self.kv_silent.valueChanged.connect(self._on_kv_silent)
        self.kv_resume.valueChanged.connect(self._on_kv_resume)
        self.kv_in_dur.valueChanged.connect(self._on_kv_in_dur)
        self.kv_out_dur.valueChanged.connect(self._on_kv_out_dur)
        self.kv_switch.valueChanged.connect(self._on_kv_switch)
        self.kv_in_trans.currentIndexChanged.connect(
            lambda i: main.kv_cfg("in_trans", ["fade", "cut", "glitch"][i]))
        self.kv_out_trans.currentIndexChanged.connect(
            lambda i: main.kv_cfg("out_trans", ["fade", "cut", "zoom"][i]))
        self.btn_noise_floor.clicked.connect(self._on_auto_noise_floor)
        self.mode_combo.currentIndexChanged.connect(main.set_mode_selection)
        self.sens.currentIndexChanged.connect(lambda i: main.engine_cfg("mode", "sensitivity", i))
        self.intensity.currentIndexChanged.connect(main.set_intensity)
        self.render_fps.currentIndexChanged.connect(main.set_render_fps)
        self.chk_gpu.stateChanged.connect(main.set_gpu_decode)
        self.transition.currentIndexChanged.connect(main.set_transition)
        self.energy_map.stateChanged.connect(lambda s: main.engine_cfg("auto", "energy_map", bool(s)))
        self.bpm_sync.stateChanged.connect(lambda s: main.engine_cfg("auto", "bpm_speed_sync", bool(s)))
        self.energy_scale.valueChanged.connect(self._on_energy_scale)
        # 注意：构造时绝不主动 emit（否则默认值 100 会把配置里存好的 energy_scale 覆盖成 1.0）；
        # 初始化由 _load_settings_to_ui 的 setValue 触发 valueChanged 完成置换。
        self.beat_pick.currentIndexChanged.connect(lambda i: main.beat_cfg("pick_mode", "seq" if i == 0 else "rand"))
        self.beat_interval.currentIndexChanged.connect(
            lambda i: main.beat_cfg("interval_beats", [0.25, 0.5, 1, 2, 4, 8, 16][i]))
        self.beat_bars.valueChanged.connect(lambda v: main.beat_cfg("bars_per_pair", v))
        self.beat_end.currentIndexChanged.connect(lambda i: main.beat_cfg("end_action", ["loop", "reverse"][i]))
        self.postfx_auto.stateChanged.connect(lambda s: main.set_postfx_auto(bool(s)))
        self.chk_postfx_on.stateChanged.connect(lambda s: main.set_postfx_master(bool(s)))
        self.postfx_mode.currentIndexChanged.connect(main.set_postfx_mode)
        self.postfx_global.valueChanged.connect(lambda v: main.set_postfx_global(int(v)))
        for key in self.postfx_checks:
            self.postfx_checks[key].stateChanged.connect(
                lambda s, k=key: main.set_postfx(k, "on", bool(s)))
            self.postfx_sliders[key].valueChanged.connect(
                lambda v, k=key: main.set_postfx(k, "level", int(v)))
            self.postfx_gears[key].clicked.connect(lambda _c=False, k=key: main.open_postfx_detail(k))
        self.postfx_hold.currentIndexChanged.connect(main.set_postfx_hold)
        # 颜色渲染
        self.chk_color_on.stateChanged.connect(lambda s: main.color_cfg("on", bool(s)))
        self.chk_color_auto.stateChanged.connect(lambda s: main.color_cfg("auto", bool(s)))
        self.color_mode.currentIndexChanged.connect(
            lambda i: main.color_cfg("mode", "duotone" if i == 1 else "lut"))
        self.color_strength.valueChanged.connect(self._on_color_strength)
        self.color_rotate.valueChanged.connect(self._on_color_rotate)
        self.chk_color_burst.stateChanged.connect(lambda s: main.color_cfg("burst", bool(s)))
        self.color_flicker.currentIndexChanged.connect(
            lambda i: main.color_cfg("flicker_mode", ["on", "off", "random"][i]))
        self.btn_color_bypass.toggled.connect(self._on_color_bypass)
        self.btn_color_adv.toggled.connect(self._on_color_adv)
        self.btn_color_auto_back.clicked.connect(self.release_color_lock)
        box.addStretch(1)
        self.sync_beat_section()

    def _on_energy_scale(self, v):
        g = v / 100.0
        self.energy_scale_lbl.setText("×%.1f" % g)
        self.main.set_energy_scale(g)

    # ---- 颜色渲染回调 ----
    def _on_color_strength(self, v):
        self.color_strength_lbl.setText("%d%%" % v)
        self.main.color_cfg("strength", int(v))

    def _on_color_rotate(self, v):
        self.color_rotate_lbl.setText("%d s" % v)
        self.main.color_cfg("rotate_sec", int(v))

    def _on_color_bypass(self, on):
        self.btn_color_bypass.setText(T("已旁路（显示原片）") if on else T("Bypass：原片 / 调色"))
        self.main.color_cfg("bypass", bool(on))

    def _on_color_adv(self, on):
        self.color_adv.setVisible(bool(on))
        self.btn_color_adv.setText("高级设置 ▾" if on else "高级设置 ▸")

    def sync_color_palette(self, name):
        """色卡高亮：无锁定时全部不高亮；有锁定则高亮对应色卡"""
        for n, b in self.color_btns.items():
            b.setChecked(bool(name) and n == name)
        self.btn_color_auto_back.setEnabled(bool(name))

    def retranslate_palette(self):
        """语言切换：重建色卡按钮文本与提示（文本与 tooltip 都含色名，属动态文本）"""
        for pname, b in getattr(self, "color_btns", {}).items():
            b.setText(T(pname))
            b.setToolTip(Tf("锁定为「{}」（10 秒后自动回到自动变色）", T(pname)))

    def release_color_lock(self):
        self.main.color_cfg("manual_color", "")
        self.main.color_cfg("manual_t", 0.0)
        self.sync_color_palette("")

    # ---- Kv 主视觉图层设置回调 ----
    def _on_kv_db(self, v):
        self.kv_db_lbl.setText("%.1f dB" % (v / 10.0))
        self.main.kv_cfg("db_threshold", v / 10.0)

    def _on_kv_silent(self, v):
        self.kv_silent_lbl.setText("%.1f s" % (v / 10.0))
        self.main.kv_cfg("silent_delay", v / 10.0)

    def _on_kv_resume(self, v):
        self.kv_resume_lbl.setText("%.1f s" % (v / 10.0))
        self.main.kv_cfg("resume_delay", v / 10.0)

    def _on_kv_in_dur(self, v):
        self.kv_in_dur_lbl.setText("%.1f s" % (v / 10.0))
        self.main.kv_cfg("in_dur", v / 10.0)

    def _on_kv_out_dur(self, v):
        self.kv_out_dur_lbl.setText("%.1f s" % (v / 10.0))
        self.main.kv_cfg("out_dur", v / 10.0)

    def _on_kv_switch(self, v):
        self.kv_switch_lbl.setText("%.1f s" % (v / 10.0))
        self.main.kv_cfg("switch_sec", v / 10.0)

    def _on_auto_noise_floor(self):
        """监听当前环境 3 秒，把静音阈值设为比底噪高 3dB（防场地设备噪音导致乱切）。"""
        self._nf_samples = []
        self._nf_left = 30                    # 30 × 100ms = 3s
        if self._kv_noise_timer is None:
            self._kv_noise_timer = QTimer(self)
            self._kv_noise_timer.setInterval(100)
            self._kv_noise_timer.timeout.connect(self._nf_tick)
        self.btn_noise_floor.setEnabled(False)
        self.btn_noise_floor.setText("检测中…")
        self._kv_noise_timer.start()

    def _nf_tick(self):
        try:
            snap = self.main.audio.state.snapshot()
            self._nf_samples.append(float(snap.get("lv_db", -80.0)))
        except Exception:
            pass
        self._nf_left -= 1
        if self._nf_left > 0:
            return
        self._kv_noise_timer.stop()
        self.btn_noise_floor.setEnabled(True)
        self.btn_noise_floor.setText("自动检测底噪")
        if self._nf_samples:
            import statistics
            base = statistics.median(self._nf_samples)
            thr = max(-60.0, min(-20.0, base + 3.0))
            self.kv_db.setValue(int(round(thr * 10)))   # valueChanged 会写回配置

    def sync_kv_section(self):
        """Kv 主视觉图层设置区仅在存在 Kv 图层时显示。"""
        sec = self._sections.get("Kv 主视觉图层")
        if sec is not None:
            sec.setVisible(self.main.engine.kv_layer() is not None)

    def sync_beat_section(self):
        """逐拍交替参数仅在手动选择「逐拍交替」模式时显示（删掉慢切后索引为 3）"""
        sec = self._sections.get("逐拍交替参数")
        if sec is not None:
            sec.setVisible(self.mode_combo.currentIndex() == 3)

    def _add_section(self, box, title, page, default=True):
        """独立折叠分区：可同时展开多个，状态记忆在配置里"""
        states = self.main.cfg["ui"].get("sections", {}) or {}
        expanded = bool(states.get(title, default))
        sec = CollapsibleSection(title, expanded=expanded)
        sec.set_content(page)
        sec.on_toggle = self._on_section_toggle
        box.addWidget(sec)
        self._sections[title] = sec

    def _on_section_toggle(self, title, opened):
        states = dict(self.main.cfg["ui"].get("sections", {}) or {})
        states[title] = bool(opened)
        self.main.cfg["ui"]["sections"] = states
        self.main.cfg.save()

    def retranslate(self):
        pass


class OutputPanel(QWidget):
    """右下角独立输出设置区"""

    def __init__(self, main):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        g = QGroupBox("输出设置")
        go = QGridLayout(g)
        go.addWidget(QLabel(tr("resolution")), 0, 0)
        self.resolution = QComboBox()
        self.resolution.setEditable(True)          # 允许直接手输分辨率，如 1600 x 900
        self.resolution.setInsertPolicy(QComboBox.NoInsert)
        self.resolution.addItems(["1280 x 720 (720p)", "1920 x 1080 (1080p)",
                                  "1080 x 1920 (竖屏)", "1080 x 1080 (方形)"])
        self.resolution.setToolTip("可直接输入，例如 1600 900（宽 高，空格分隔）")
        go.addWidget(self.resolution, 0, 1)
        self.btn_show_out = QPushButton(tr("show_output"))
        self.btn_show_out.clicked.connect(main.show_output)
        go.addWidget(self.btn_show_out, 1, 0, 1, 2)
        self.chk_aspect = QCheckBox(tr("aspect_lock"))
        self.chk_border = QCheckBox(tr("borderless"))
        self.chk_top = QCheckBox(tr("always_top"))
        go.addWidget(self.chk_aspect, 2, 0)
        go.addWidget(self.chk_border, 2, 1)
        go.addWidget(self.chk_top, 3, 0)
        self.btn_reset_win = QPushButton("重置窗口大小")
        self.btn_reset_win.setToolTip("把输出窗口按当前分辨率设置重置（退出全屏/按比例缩放到屏幕内）")
        self.btn_reset_win.clicked.connect(main.reset_window_size)
        go.addWidget(self.btn_reset_win, 3, 1)
        go.addWidget(QLabel("输出显示器"), 4, 0)
        self.screen_combo = QComboBox()
        self._screens = QGuiApplication.screens()
        items = ["窗口模式（可拖动）"]
        for i, sc in enumerate(self._screens):
            tag = "（主）" if sc == QGuiApplication.primaryScreen() else ""
            items.append(f"显示器 {i + 1} 全屏{tag}  {sc.geometry().width()}x{sc.geometry().height()}")
        self.screen_combo.addItems(items)
        self.screen_combo.setToolTip("选择后输出窗口会全屏到对应显示器（适合双屏演出）")
        go.addWidget(self.screen_combo, 4, 1)
        # ---- Spout / NDI 输出 ----
        self.chk_spout = QCheckBox("Spout 输出")
        self.chk_spout.setToolTip("把画面实时共享给本机其他程序（OBS 需装 Spout 插件后选本 Sender）")
        go.addWidget(self.chk_spout, 5, 0)
        self.spout_name = QLineEdit()
        self.spout_name.setPlaceholderText("Sender 名称（OBS 里显示的名字）")
        go.addWidget(self.spout_name, 5, 1)
        self.chk_ndi = QCheckBox("NDI 输出（音画同步）")
        self.chk_ndi.setToolTip("通过网络发送画面+声音（运行时已随软件自带，无需安装；接收端需装 NDI Runtime）")
        go.addWidget(self.chk_ndi, 6, 0)
        self.ndi_name = QLineEdit()
        self.ndi_name.setPlaceholderText("NDI 源名称")
        go.addWidget(self.ndi_name, 6, 1)
        outer.addWidget(g)
        self.resolution.currentIndexChanged.connect(main.set_resolution)
        self.resolution.editTextChanged.connect(main.set_resolution_text)
        self.chk_aspect.stateChanged.connect(main.set_aspect_lock)
        self.chk_border.stateChanged.connect(main.set_borderless)
        self.chk_top.stateChanged.connect(main.set_always_top)
        self.screen_combo.currentIndexChanged.connect(main.set_output_screen)
        self.chk_spout.stateChanged.connect(main.set_spout_enabled)
        self.spout_name.editingFinished.connect(main.set_spout_name)
        self.chk_ndi.stateChanged.connect(main.set_ndi_enabled)
        self.ndi_name.editingFinished.connect(main.set_ndi_name)
        self.set_gate(False)   # 输出设置默认锁定，点「开始」后才可操作

    def set_gate(self, running):
        """输出设置门控：显示窗口/重置大小/输出显示器需在「开始自动VJ」后才能生效"""
        tip = "" if running else T("先点「▶ 开始」后再使用输出设置")
        for w in (self.btn_show_out, self.btn_reset_win, self.screen_combo):
            w.setEnabled(running)
            w.setToolTip(tip)

    def retranslate(self):
        """语言切换：重建含显示器名/分辨率的下拉项（这些项是动态拼出来的）"""
        cur = self.screen_combo.currentIndex()
        self.screen_combo.blockSignals(True)
        self.screen_combo.clear()
        items = [T("窗口模式（可拖动）")]
        for i, sc in enumerate(self._screens):
            tag = T("（主）") if sc == QGuiApplication.primaryScreen() else ""
            items.append(Tf("显示器 {} 全屏{}  {}x{}", i + 1, tag,
                            sc.geometry().width(), sc.geometry().height()))
        self.screen_combo.addItems(items)
        self.screen_combo.setCurrentIndex(max(0, min(cur, len(items) - 1)))
        self.screen_combo.blockSignals(False)


class HotkeyDialog(I18nDialog):
    """快捷键设置面板

    交互约定（2026-09-24 用户要求）：
    - 表格里的改动**只改表格**，点「应用」才写进配置并生效（key / 范围 / 启用 三项都是）；
    - 关闭时若还有没应用的改动 → 弹二级确认框问要不要保存；点过「应用」就不再弹。
    - 旧版只把「快捷键」列的文字改动即时保存，而「范围 / 启用」两列根本没接信号
      （改了不生效）—— 现在统一由「应用」提交，这三列才真正起作用。
    """

    def __init__(self, main):
        super().__init__(main)
        self.setWindowTitle(tr("hotkeys"))
        self.resize(560, 480)
        self.main = main
        self._dirty = False          # 有未应用的改动
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("settings"), tr("hotkeys"), tr("hotkey_scope"), tr("enabled")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        b_reset = QPushButton(tr("reset_defaults"))
        b_reset.clicked.connect(self._reset)
        b_apply = QPushButton(T("应用"))
        b_apply.setToolTip(T("把表格里的快捷键/范围/启用写进配置并立即生效"))
        b_apply.clicked.connect(self._apply_clicked)
        self.btn_apply = b_apply
        b_close = QPushButton(T("关闭"))
        # 走 close()（→ closeEvent）：与点窗口 X 同一条路径，保证"未应用就提示保存"都能触发
        b_close.clicked.connect(self.close)
        row.addWidget(b_reset)
        row.addStretch(1)
        row.addWidget(b_apply)
        row.addWidget(b_close)
        lay.addLayout(row)
        self._fill()
        self.table.itemChanged.connect(self._changed)

    def showEvent(self, ev):
        # 表头与「设置」列标签是表格项文本，不在通用控件遍历的覆盖范围内，
        # 每次打开时按当前语言重建（表格是短命窗口，成本可忽略）。
        self.table.setHorizontalHeaderLabels([tr("settings"), tr("hotkeys"),
                                              tr("hotkey_scope"), tr("enabled")])
        self._fill()
        super().showEvent(ev)

    def _fill(self):
        from config import DEFAULT_HOTKEYS
        cfg = self.main.cfg["hotkeys"]
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for key, label in (("toggle_run", "开始/停止"), ("blackout", "黑场"), ("freeze", "冻结"),
                           ("pause_auto", "暂停自动"), ("next_scene", "下一素材"),
                           ("toggle_order_mode", "顺序/随机（逐拍交替）"),
                           ("toggle_beat_mode", "逐拍交替开关"), ("intensity_up", "强度+"),
                           ("intensity_down", "强度-"), ("manual_transition", "手动切换"),
                           ("lock_clip", "锁定素材"), ("output_fullscreen", "输出全屏"),
                           ("toggle_ui", "显示/隐藏界面"),
                           ("color_bypass", "颜色 Bypass（原片/调色）"),
                           ("panic_reset", "紧急恢复")):
            it = cfg.get(key) or DEFAULT_HOTKEYS.get(key, {})
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(T(label)))
            self.table.setItem(r, 1, QTableWidgetItem(it.get("key", "")))
            sc = QComboBox()
            sc.addItems([T("全局"), T("仅窗口")])
            sc.setCurrentIndex(0 if it.get("global") else 1)
            # 范围改动也要能被"应用"/脏检测看到
            sc.currentIndexChanged.connect(self._touch)
            self.table.setCellWidget(r, 2, sc)
            cb = QCheckBox()
            cb.setChecked(bool(it.get("enabled", True)))
            cb.toggled.connect(self._touch)
            self.table.setCellWidget(r, 3, cb)
            self.table.item(r, 0).setData(Qt.UserRole, key)
        self.table.blockSignals(False)

    # ---------- 改动跟踪 / 应用 / 关闭确认 ----------
    def _touch(self, *a):
        """标记有未应用的改动（不写配置、不生效）"""
        self._dirty = True

    def _changed(self, item):
        if item.column() != 1:
            return
        self._dirty = True

    def _collect(self):
        """把表格现状读成 {name: {key, global, enabled}}"""
        out = {}
        for r in range(self.table.rowCount()):
            key = self.table.item(r, 0).data(Qt.UserRole)
            if not key:
                continue
            item = self.table.item(r, 1)
            sc = self.table.cellWidget(r, 2)
            cb = self.table.cellWidget(r, 3)
            out[key] = {
                "key": (item.text().strip() if item else ""),
                "global": bool(sc.currentIndex() == 0) if sc else False,
                "enabled": bool(cb.isChecked()) if cb else True,
            }
        return out

    def _apply(self):
        """写进配置并让快捷键立即生效"""
        table = self._collect()
        hk = self.main.cfg["hotkeys"]
        for name, spec in table.items():
            cur = hk.get(name)
            if not isinstance(cur, dict):
                cur = {}
                hk[name] = cur
            cur.update(spec)
        self.main.cfg.save()
        try:
            self.main.hotkeys.apply()
        except Exception:
            pass
        self._dirty = False
        # 全局热键可能被别的程序占用（无修饰键的字母/方向键尤其容易被抢）→ 明确告知，
        # 否则用户会以为「按了没反应」。用表格里的中文标签报出来。
        fails = list(getattr(self.main.hotkeys, "global_failed", []) or [])
        if fails:
            labels = {self.table.item(r, 0).data(Qt.UserRole): self.table.item(r, 0).text()
                      for r in range(self.table.rowCount()) if self.table.item(r, 0)}
            names = "、".join(labels.get(f, f) for f in fails)
            QMessageBox.warning(self, tr("hotkeys"),
                                Tf("这些全局快捷键没能注册（可能被其它程序占用）：\n{0}\n\n"
                                   "可以换成带 Ctrl/Alt 的组合键，或把范围改回「仅窗口」。", names))

    def _apply_clicked(self):
        self._apply()
        # 给个"已生效"的反馈（按钮禁用 0.6s 由 Qt 事件循环自然恢复）
        self.btn_apply.setEnabled(False)
        QTimer.singleShot(600, self._reenable_apply)

    def _reenable_apply(self):
        try:
            self.btn_apply.setEnabled(True)
        except RuntimeError:
            pass        # 面板已关闭（C++ 对象已销毁），忽略

    def _confirm_save(self):
        """二级确认：保存 / 不保存（用按钮对象判断，不看文本 —— 切英文后文本会变）"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(tr("hotkeys"))
        box.setText(T("快捷键有未应用的修改，要保存吗？"))
        b_save = box.addButton(T("保存"), QMessageBox.AcceptRole)
        b_drop = box.addButton(T("不保存"), QMessageBox.RejectRole)
        box.setDefaultButton(b_save)
        box.exec()
        return box.clickedButton() is b_save

    def closeEvent(self, ev):
        if self._dirty:
            try:
                if self._confirm_save():
                    self._apply()
            except Exception:
                pass
        self._dirty = False
        super().closeEvent(ev)

    def _reset(self):
        """恢复默认：只把默认值填回表格（不立刻生效），点「应用」或关闭时确认后才写入。
        这样与其它改动一致的「先改表、后应用」语义，避免一半即时生效一半不生效。"""
        from config import DEFAULT_HOTKEYS
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            key = self.table.item(r, 0).data(Qt.UserRole)
            d = DEFAULT_HOTKEYS.get(key)
            if not d:
                continue
            self.table.item(r, 1).setText(d.get("key", ""))
            sc = self.table.cellWidget(r, 2)
            if sc:
                sc.setCurrentIndex(0 if d.get("global") else 1)
            cb = self.table.cellWidget(r, 3)
            if cb:
                cb.setChecked(bool(d.get("enabled", True)))
        self.table.blockSignals(False)
        self._dirty = True


class AboutDialog(I18nDialog):
    """关于：版本、本版亮点、作者与反馈、开源项目与第三方组件（含链接）、免责声明。

    内容用富文本拼装（含可点链接），所以不做静态词条翻译，
    而是每次打开时按当前语言重建整块内容（_rebuild）。
    """

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setMinimumWidth(520)
        self.setMinimumHeight(560)
        v = QVBoxLayout(self)
        v.setSpacing(10)

        self.lbl_body = QLabel()
        self.lbl_body.setWordWrap(True)
        self.lbl_body.setTextFormat(Qt.RichText)
        self.lbl_body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.lbl_body.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_body.setOpenExternalLinks(True)   # 群链接等可点击直接打开浏览器
        self.lbl_body.setProperty("_i18nDynamic", True)   # 内容由 _rebuild 生成
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.lbl_body)
        v.addWidget(scroll, 1)

        row = QHBoxLayout()
        self.btn_hk = QPushButton(tr("hotkeys"))
        self.btn_hk.clicked.connect(self._open_hotkeys)
        self.btn_manual = QPushButton(tr("user_manual"))
        self.btn_manual.clicked.connect(self._open_manual)
        self.btn_data = QPushButton(tr("open_data_dir"))
        self.btn_data.clicked.connect(self._open_data_dir)
        for b in (self.btn_hk, self.btn_manual, self.btn_data):
            row.addWidget(b)
        row.addStretch(1)
        self.btn_close = QPushButton(tr("close"))
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_close)
        v.addLayout(row)

    # ---- 内容重建 ----
    def showEvent(self, ev):
        self._rebuild()
        super().showEvent(ev)

    def _rebuild(self):
        import version
        from i18n import lang as _lang
        en = (_lang() == "en")

        def pick(zh, en_s):
            return en_s if en else zh

        self.setWindowTitle(pick("关于", "About"))
        self.btn_hk.setText(tr("hotkeys"))
        self.btn_manual.setText(tr("user_manual"))
        self.btn_data.setText(tr("open_data_dir"))
        self.btn_close.setText(tr("close"))

        hl = "".join(
            f"<li><b>{pick(zh, e)}</b> — {pick(dz, de)}</li>"
            for zh, e, dz, de in version.HIGHLIGHTS)
        # 外部项目条目：名称 — 用途 + 可点击的项目地址（lbl_body 已开 openExternalLinks）
        def _entries(items):
            out = []
            for it in items:
                n, dz, de, url, lic = it[:5]
                out.append(
                    f"<li style='margin-bottom:3px;'><b>{n}</b> — {pick(dz, de)}<br/>"
                    f"<span style='color:#888;font-size:12px;'>"
                    f"<a href='{url}' style='color:#5aa9e6;'>{url.replace('https://', '')}</a>"
                    f" · {lic}</span></li>")
            return "".join(out)

        proj = _entries(version.PROJECTS)
        model_items = _entries(getattr(version, "MODELS", []))
        cred = _entries(getattr(version, "CREDITS", []))
        # 中英标点差异：中文用全角，英文用半角（否则英文界面里全是「：（）」很怪）
        _sp = pick("：", ": ")
        _op, _cp = pick("（", "("), pick("）", ")")
        author = version.AUTHOR or pick("（待填）", "(to be filled)")
        contact = version.CONTACT or pick("（待填）", "(to be filled)")
        # 测试群：有链接就渲染成可点文字（lbl_body 已开 openExternalLinks，
        # 点一下直接拉起浏览器/QQ 加群）
        group_line = ""
        _gno = getattr(version, "GROUP_NO", "")
        if _gno:
            _gname = getattr(version, "GROUP_NAME", "") or ""
            _gurl = getattr(version, "GROUP_URL", "") or ""
            _lnk = (f'　<a href="{_gurl}" style="color:#5aa9e6;">'
                    f'{pick("点击加入", "Join")}</a>') if _gurl else ""
            group_line = (f'<br/>{pick("测试群", "Test group")}{_sp}{_gname}'
                          f'{_op}{pick("群号", "Group no.")} {_gno}{_cp}{_lnk}')

        # 版本号 = 日期 + 当日序号（打包时由 tools/build_version.py 写进 _build_ver.py）
        _ver = getattr(version, "APP_VERSION", "dev")
        if _ver == "dev":
            _ver = pick("开发版（源码运行）", "Dev build (from source)")
        _vline = f"{pick('版本', 'Version')} {_ver}"
        _vseq = int(getattr(version, "APP_SEQ", 0) or 0)
        if _vseq:
            _vline += " · " + pick(f"当日第 {_vseq} 次生成", f"build #{_vseq} of the day")

        html = f"""
        <div style="font-family:system-ui,'Microsoft YaHei';">
          <h2 style="margin:0 0 2px 0;">{tr('app_name')}</h2>
          <div style="color:#888;">
            {_vline}
          </div>
          <p style="margin:8px 0 4px 0;">
            {pick('把音乐变成画面的实时自动 VJ 引擎',
                  'A real-time auto-VJ engine that turns music into visuals')}
          </p>

          <h3 style="margin:14px 0 4px 0;">{pick('本版亮点', 'Highlights')}</h3>
          <ul style="margin:0 0 0 18px;padding:0;">{hl}</ul>

          <h3 style="margin:14px 0 4px 0;">{pick('作者与反馈', 'Author & Feedback')}</h3>
          <div style="margin-left:2px;">
            {pick('作者', 'Author')}{_sp}{author}<br/>
            {pick('反馈', 'Feedback')}{_sp}{contact}{group_line}
          </div>

          <h3 style="margin:14px 0 4px 0;">{pick('开源项目与第三方组件', 'Open-source projects & third-party components')}</h3>
          <ul style="margin:0 0 0 18px;padding:0;">{proj}</ul>

          <h3 style="margin:14px 0 4px 0;">{pick('AI 模型', 'AI models')}</h3>
          <ul style="margin:0 0 0 18px;padding:0;">{model_items}</ul>

          <h3 style="margin:14px 0 4px 0;">{pick('来源与致谢', 'Credits & origins')}</h3>
          <ul style="margin:0 0 0 18px;padding:0;">{cred}</ul>

          <p style="margin:14px 0 0 0;color:#c66;">
            ⚠ {pick('本软件含快速闪烁画面，光敏性癫痫者请勿使用。',
                     'This software displays rapidly flashing visuals. '
                     'Not for people with photosensitive epilepsy.')}<br/>
            &nbsp;&nbsp;&nbsp;{pick('测试版软件，演出前请务必充分测试。',
                                    'Beta software — always test thoroughly before a show.')}
          </p>
        </div>
        """
        self.lbl_body.setText(html)

    # ---- 按钮动作 ----
    def _open_hotkeys(self):
        HotkeyDialog(self.main).exec()

    def _open_manual(self):
        from config import exe_dir
        en = (i18n.lang() == "en")
        zh_name, en_name = "使用说明.txt", "使用说明_EN.txt"
        order = [en_name, zh_name] if en else [zh_name, en_name]
        cands = []
        for nm in order:
            cands.append(os.path.join(exe_dir(), nm))
            cands.append(os.path.join(os.getcwd(), nm))
        for p in cands:
            if os.path.exists(p):
                try:
                    os.startfile(p)      # noqa: S606  Windows 默认程序打开
                except Exception:
                    QMessageBox.information(self, tr("app_name"), p)
                return
        QMessageBox.information(
            self, tr("app_name"),
            T("未找到「使用说明.txt」，请确认它和程序放在同一目录。"))

    def _open_data_dir(self):
        from config import CONFIG_FILE
        d = os.path.dirname(CONFIG_FILE)
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)              # noqa: S606
        except Exception:
            QMessageBox.information(self, tr("app_name"), d)


class TagScanThread(QThread):
    """后台视觉打标：遍历素材库，逐条打内容 tag，progress 增量更新 UI"""
    progress = Signal(int, int, str)   # done, total, path

    def __init__(self, main, paths=None, force=False):
        super().__init__(main)
        self.main = main
        self.paths = paths
        self.force = force
        self._cancel = False

    def run(self):
        from tagger import get_tagger
        try:
            tg = get_tagger()
        except Exception:
            return
        items = [m for m in self.main.library.values()
                 if m.kind in ("image", "video", "gif") and not getattr(m, "excluded", False)]
        if self.paths:
            want = set(self.paths)
            items = [m for m in items if m.path in want]
        elif not self.force:
            # 增量/断点续扫：已打标的跳过，下次接着扫
            items = [m for m in items if not m.tags]
        total = len(items)
        from tagger import auto_roles
        for i, m in enumerate(items):
            if self._cancel:
                break
            try:
                kind = "image" if m.kind in ("image", "gif") else "video"
                r = tg.analyze_media(m.path, kind)
                if r:
                    tags = [z for z, _ in r["tags"][:6]]
                    tags += list(r.get("colors") or [])
                    if r.get("bright"):
                        tags.append(r["bright"])
                    if r.get("motion"):
                        tags.append(r["motion"])
                    if r.get("flicker"):
                        tags.append(DYNAMIC_FLICKER)
                    m.tags = tags
                    self.main.cfg["clip_tags"][m.path] = tags
                    # 扫描自动分配角色：全量重扫(force)强制重算；增量只对未设角色的分配
                    if self.force or not any(m.roles.values()):
                        m.roles = auto_roles(
                            tags, kind, bool(r.get("alpha")),
                            coverage=r.get("coverage"),
                            dynamic=bool(r.get("dynamic")),
                            scored=r.get("tags"))
                        self.main.cfg["clip_roles"][m.path] = dict(m.roles)
            except Exception:
                pass
            self.progress.emit(i + 1, total, m.path)
            if (i + 1) % 20 == 0:
                try:
                    self.main.cfg.save()
                except Exception:
                    pass
        try:
            self.main.cfg.save()
        except Exception:
            pass


class DynTagScanThread(QThread):
    """动态标签快速重扫：只跑帧差/频闪检测（不跑 CLIP），给存量素材补三档动态标签，
    并做角色单向修正（铺满场景被误判前景 → 背景）。

    升级「低动态/中动态/高动态」三档 + coverage v2（分块广度）后，启动时按
    cfg["dyn_tag_ver"] < 1 触发一次。不跑 CLIP，不碰内容标签和手动标签。"""
    progress = Signal(int, int)

    def __init__(self, main):
        super().__init__(main)
        self.main = main

    def run(self):
        import cv2
        from tagger import Tagger, SUBJECT_TAGS
        from media_manager import _ascii_safe_path
        items = [m for m in self.main.library.values()
                 if m.kind in ("image", "video", "gif") and not getattr(m, "excluded", False)]
        total = len(items)
        for i, m in enumerate(items):
            try:
                kind = "image" if m.kind in ("image", "gif") else "video"
                motion, flicker, diff = Tagger.analyze_dynamic(m.path, kind)
                if motion:  # 打不开时 motion=""，保持原标签不动
                    # 替换旧动态词（含旧的「高动态」可能来自二值逻辑），再写入三档词；
                    # 被用户取消的动态词（disabled_tags）不复活
                    dis = set(m.disabled_tags or [])
                    tags = [t for t in m.tags if t not in DYNAMIC_TAGS]
                    if motion not in dis:
                        tags.append(motion)
                    if flicker and DYNAMIC_FLICKER not in dis:
                        tags.append(DYNAMIC_FLICKER)
                    m.tags = tags
                    self.main.cfg["clip_tags"][m.path] = tags
                # ---- 角色单向修正：铺满全屏(cov≥0.45)但被旧逻辑判成前景、且画面无明确
                # 主体词（人物/舞者等）→ 改为背景。绝不反向（真前景是黑底，新 cov 必低）。
                if (motion and m.roles.get("fg") and not m.roles.get("bg")
                        and not (set(m.tags) & SUBJECT_TAGS)):
                    cov = None
                    if kind == "image":
                        img = cv2.imread(_ascii_safe_path(m.path))
                        if img is not None:
                            cov = Tagger.coverage_score(img)
                    else:
                        cap = cv2.VideoCapture(_ascii_safe_path(m.path))
                        if cap.isOpened():
                            tot = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100
                            cap.set(cv2.CAP_PROP_POS_FRAMES, int(tot * 0.7))
                            ok, fr = cap.read()
                            cap.release()
                            if ok:
                                cov = Tagger.coverage_score(fr)
                    if cov is not None and cov >= 0.45:
                        m.roles = {"fg": False, "bg": True}
                        self.main.cfg["clip_roles"][m.path] = dict(m.roles)
            except Exception:
                pass
            self.progress.emit(i + 1, total)
            # 只在「开始 VJ」运行中才限速：演出时让出 CPU 给渲染/音频；
            # 空闲（未运行）时全速跑，尽快扫完。
            if self.main.engine.running:
                time.sleep(0.1)
            if (i + 1) % 20 == 0:
                try:
                    self.main.cfg.save()
                except Exception:
                    pass
        self.main.cfg["dyn_tag_ver"] = 5
        try:
            self.main.cfg.save()
        except Exception:
            pass


class ScanOverlayWidget(QWidget):
    """素材库扫描悬浮提示卡：无边框置顶小卡片（主窗口右下角），扫描完成自动关闭。

    后台重扫是静默的，之前 160 个素材重扫几分钟期间用户完全无感知，误以为软件卡死。
    这个悬浮卡让重扫「可见」：显示进度 N/M，完成即消失。"""

    def __init__(self, main, title):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setObjectName("scanOverlay")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(6)
        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet(
            "color:#dfe8f2;font-size:12px;font-weight:bold;background:transparent;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.bar.setStyleSheet(
            "QProgressBar{background:#1c2733;border:1px solid #2c3a4a;border-radius:4px;}"
            "QProgressBar::chunk{background:#3f9fff;border-radius:4px;}")
        v.addWidget(self.lbl_title)
        v.addWidget(self.bar)
        self.setStyleSheet(
            "#scanOverlay{background:rgba(18,26,36,235);border:1px solid #2c3a4a;"
            "border-radius:8px;}")
        self.setFixedWidth(280)
        self.adjustSize()
        geo = main.geometry()
        self.move(geo.right() - self.width() - 28, geo.bottom() - self.height() - 56)
        self.show()

    def update_progress(self, done, total):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.lbl_title.setText(f"正在扫描素材库… {done}/{total}")

    def finish(self):
        self.close()


class _ModalDialogTopFilter(QObject):
    """模态对话框打开时自动取消输出窗口置顶（避免对话框被盖住），关闭后恢复原置顶状态"""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self._depth = 0
        self._was_top = False

    def eventFilter(self, obj, ev):
        from PySide6.QtWidgets import QDialog
        if isinstance(obj, QDialog) and obj.isModal():
            if ev.type() == QEvent.Show:
                if self._depth == 0:
                    self._was_top = bool(self.main.cfg["output"].get("always_top", False))
                    if self._was_top:
                        self.main.set_always_top(False)
                self._depth += 1
            elif ev.type() == QEvent.Hide:
                self._depth = max(0, self._depth - 1)
                if self._depth == 0 and self._was_top:
                    self.main.set_always_top(True)
                    self._was_top = False
        return False


class MusicScanThread(QThread):
    """后台音乐分析：逐首建指纹 + 查曲风，progress 增量更新"""

    progress = Signal(int, int, str)   # done, total, path

    def __init__(self, main, paths, force=False):
        super().__init__(main)
        self.main = main
        self.paths = paths
        self.force = force

    @staticmethod
    def _local_ai_genre(gc, sig, sr):
        """整曲本地 AI：取 25%/50%/75% 三段各 6s 分类，投票合并（单曲级差异化）"""
        from collections import Counter
        seg = int(sr * 6)
        if len(sig) < seg:
            pts = [sig]
        else:
            pts = [sig[int(sr * f):int(sr * f) + seg] for f in (0.25, 0.5, 0.75)]
        votes = []
        for s in pts:
            try:
                votes.extend(gc.classify(s, sr)["tags"])
            except Exception:
                pass
        return [t for t, _ in Counter(votes).most_common(4)]

    # ---------------------------------------------------------------- 并行扫描
    @staticmethod
    def _lower_this_thread():
        """把**当前线程**的优先级降到「低于正常」（Windows）。

        这是「扫描改成并行、但不影响演出」的关键：演出/渲染线程是**正常**优先级，
        Windows 调度器会优先满足它们 —— 扫描即使满负荷，也只是"吃剩下的空闲"。
        非 Windows 或调用失败都不影响功能（只是少了这层保护）。
        """
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p   # 伪句柄 -1，必须包成 c_void_p
            k32.SetThreadPriority(ctypes.c_void_p(k32.GetCurrentThread()), -1)  # BELOW_NORMAL
        except Exception:
            pass

    def _worker_count(self):
        """扫描并发数：**演出中 2**、**空闲 min(6, 核数//3)**（16 核 → 5）。

        可用 `AUTO_VJ_SCAN_THREADS` 环境变量覆盖（实测/调试用）。
        """
        env = os.environ.get("AUTO_VJ_SCAN_THREADS")
        if env:
            try:
                return max(1, min(16, int(env)))
            except Exception:
                pass
        try:
            n = os.cpu_count() or 4
        except Exception:
            n = 4
        try:
            busy = bool(self.main.engine.running)
        except Exception:
            busy = False
        return 2 if busy else max(2, min(6, n // 3))

    def run(self):
        import hashlib
        import os as _os
        import threading as _th
        import time as _tm
        from concurrent.futures import ThreadPoolExecutor
        import numpy as np
        import soundfile as sf
        import genre_lookup as gl
        from fp import FingerprintDB
        from music_meta import resolve_music_genre
        from genre_keywords import simplify_genres
        from config import app_base_dir

        db = FingerprintDB(_os.path.join(app_base_dir(), "fingerprints.db"))
        try:
            db.begin_scan()       # 批量写入模式（synchronous=OFF + 攒批 commit，收尾 end_scan 恢复）
        except Exception:
            pass
        meta = self.main.cfg["music_meta"]
        items = list(self.paths) if self.paths else list(self.main.cfg["music_library"])
        todo = [p for p in items if self.force or p not in meta]
        total = len(todo)
        gc = None
        try:
            from audio_genre import get_genre_classifier
            gc = get_genre_classifier()
        except Exception:
            gc = None

        n_workers = self._worker_count()
        db_lock = _th.Lock()          # SQLite 同一连接不能多线程并发写，保护写库即可
        self._lower_this_thread()

        def _log(msg):
            try:
                lp = _os.path.join(_os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                                   "scan_time.log")
                _os.makedirs(_os.path.dirname(lp), exist_ok=True)
                with open(lp, "a", encoding="utf-8") as fh:
                    fh.write("[%s] %s\n" % (_tm.strftime("%Y-%m-%d %H:%M:%S"), msg))
            except Exception:
                pass

        # ★ 节拍网格：**建库时离线算一次**（BPM / 锚点 / 八拍乐句相位），演出中只查表。
        #   数据源优先 VirtualDJ 库（用户装过就白捡 BPM+锚点，且能省掉单首最贵的 BPM 扫描）。
        try:
            import beatgrid as _bg
            vdj_idx = _bg.load_vdj()
            _log("节拍网格：VDJ 库 %d 首（已分析 %d 首）"
                 % (len(vdj_idx), getattr(vdj_idx, "analyzed", 0)))
        except Exception as _e:                                         # noqa: BLE001
            _bg, vdj_idx = None, None
            _log("节拍网格：VDJ 库不可用（%s），将全部自算" % _e)

        def _one(p):
            """单首：解码 → 指纹入库 → 曲风（本地）。返回 (path, r, 三段耗时)。

            本函数在 worker 线程里跑，**任何异常都自己吞掉**（返回 r=None），
            免得一个坏文件把整轮扫描打断。
            """
            try:
                t0 = _tm.perf_counter()
                data, sr = sf.read(p, dtype="float32", always_2d=True)
                sig = np.mean(data, axis=1)
                t1 = _tm.perf_counter()
                song_id = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
                with db_lock:
                    # ★ 批量写入：攒 6 首落一次盘 —— INSERT 本身躲不掉，但能省掉"每首一次 fsync"
                    #   （HDD 上单次 fsync 要等盘片 10~50ms，这钱白花）。
                    db.add_song(song_id, sig, sr, commit=False)
                    if db.pending() >= 6:
                        db.commit_batch()
                t2 = _tm.perf_counter()
                r = resolve_music_genre(p)      # 并行阶段联网已关 → 只走本地
                t3 = _tm.perf_counter()
                # track 级精准命中 → 在线结果已足够，跳过本地 AI 推理（大幅提速）
                if gc is not None and r.get("level") != "track":
                    ai_zh = simplify_genres(self._local_ai_genre(gc, sig, sr))
                    r["ai_zh"] = ai_zh
                    on = list(r.get("genres") or [])
                    if ai_zh:
                        seen, merged = set(), []
                        if r.get("level") == "track":
                            order = on[:3] + ai_zh[:2]     # 在线为主，AI 补充
                        else:
                            order = ai_zh[:3] + on[:2]     # 本地 AI 为主，在线为辅
                        for z in order:
                            if z and z not in seen:
                                seen.add(z); merged.append(z)
                        r["genres"] = merged
                        if not on and ai_zh:
                            r["source"] = "localai"
                r["song_id"] = song_id
                # 段落分析已移除（用户反馈：不准，去掉曲库的段落分析与预览里的段落显示）
                # ★ 节拍网格（BPM / 锚点 / 八拍乐句相位）—— 离线一次入库，演出中只查表。
                #   复用刚解码的 sig：扫描已经为指纹读过一次盘，这里不再读第二遍。
                if _bg is not None:
                    try:
                        g = _bg.grid_from_signal(sig, sr, vdj_idx, path=p)
                        if g:
                            r["grid"] = g
                    except Exception:
                        pass
                return p, r, (t1 - t0), (t2 - t1), (t3 - t2)
            except Exception:
                return p, None, 0.0, 0.0, 0.0

        # ---- 阶段 1：**并行**做本地部分（解码 + 指纹 + 关键词/ID3/本地 AI）----
        # 联网在这一阶段**关闭**：多线程一起打 Discogs 只会更快撞它的限流（未认证 25 次/分钟）。
        gl.set_online_enabled(False)
        _log("开始扫描：%d 首，%d 线程并行（本地阶段）%s"
             % (total, n_workers, "；演出中→限 2 线程" if n_workers <= 2 else ""))
        done, ok_n = 0, 0
        acc = [0.0, 0.0, 0.0]
        t_all = _tm.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                for p, r, dt_dec, dt_fp, dt_meta in ex.map(_one, todo):
                    if r is not None:
                        ok_n += 1
                        try:
                            self.main.cfg["music_meta"][p] = r
                        except Exception:
                            pass
                    done += 1
                    self.progress.emit(done, total, p)
                    acc[0] += dt_dec; acc[1] += dt_fp; acc[2] += dt_meta
                    if done % 10 == 0:
                        try:
                            self.main.cfg.save()
                        except Exception:
                            pass
                        tot10 = sum(acc)
                        _log("阶段1 %d/%d：平均 解码 %.2fs + 指纹 %.2fs + 曲风(本地) %.2fs "
                             "= %.2fs/首（%.1f 首/秒，%d 线程）"
                             % (done, total, acc[0] / 10.0, acc[1] / 10.0, acc[2] / 10.0,
                                tot10 / 10.0, 10.0 / max(1e-6, tot10), n_workers))
                        acc[0] = acc[1] = acc[2] = 0.0
        except Exception as e:                                          # noqa: BLE001
            _log("阶段1 异常终止：%s" % e)
        try:
            self.main.cfg.save()
        except Exception:
            pass
        try:
            db.commit_batch()     # 落盘最后一批挂起的写入
        except Exception:
            pass
        el = _tm.perf_counter() - t_all
        _log("阶段1（并行本地）完成：成功 %d/%d 首，用时 %.1f 秒（%.2f 秒/首）"
             % (ok_n, done, el, el / max(1, done)))

        # ---- 阶段 2：**串行**补在线曲风（尊重限流；网络差就提前收手）----
        if done and not gl.online_blocked():
            # ★ 必须先清缓存：阶段 1 联网是关的，那些"查不到"会被缓存成 None，
            #   不清掉的话阶段 2 每首都命中失败缓存、根本不会发请求（实测 200 首只花 0.2 秒）。
            try:
                import music_meta as _mm
                _mm.clear_online_cache()
            except Exception:
                pass
            gl.set_online_enabled(True)
            fixed, t_net = 0, _tm.perf_counter()
            n_try, slow = 0, 0
            for p in todo:
                if gl.online_blocked():
                    break
                r0 = self.main.cfg["music_meta"].get(p)
                if not isinstance(r0, dict) or r0.get("level") == "track":
                    continue                  # 已有 track 级在线结果，无需再查
                _tq = _tm.perf_counter()
                try:
                    r2 = resolve_music_genre(p)
                    if r2 and (r2.get("level") or r2.get("source") in ("discogs", "itunes")):
                        self.main.cfg["music_meta"][p] = r2
                        fixed += 1
                        if fixed % 5 == 0:
                            try:
                                self.main.cfg.save()
                            except Exception:
                                pass
                except Exception:
                    pass
                _dq = _tm.perf_counter() - _tq
                n_try += 1
                # 连着 5 首单首都 >2.5 秒 ⇒ 判定网络不佳，别再耗（否则 1452 首要几小时）
                slow = slow + 1 if _dq > 2.5 else 0
                if slow >= 5:
                    _log("阶段2：连续 5 首查询都超 2.5 秒，判定网络不佳 → 提前结束（已试 %d 首）" % n_try)
                    break
            try:
                self.main.cfg.save()
            except Exception:
                pass
            _log("阶段2（串行在线）完成：补充 %d 首，用时 %.1f 秒%s"
                 % (fixed, _tm.perf_counter() - t_net,
                    "（已被限流，提前结束）" if gl.online_blocked() else ""))
        gl.set_online_enabled(True)
        try:
            db.end_scan()         # 落盘 + 把 synchronous 恢复回原值
        except Exception:
            pass
        db.close()


def _probe_spout_once():
    """一次性探测 Spout 能不能用（返回 (ok, 错误信息)）。

    为什么要单独探：`engine._spout_send` 是懒加载 + 失败静默禁用，用户勾上之后
    如果建 sender 失败，界面上完全看不出来（勾还在，但一帧都没发）——
    2026-09-24 用户问「Spout 能用吗」，就是因为缺这个反馈。
    """
    try:
        from SpoutGL import SpoutSender
        from SpoutGL.enums import GL_BGRA_EXT
        import numpy as _np
        sp = SpoutSender()
        sp.setSenderName("EasyRealityAutoVJ_Probe")
        sp.createOpenGL()
        buf = _np.zeros(8 * 8 * 4, dtype=_np.uint8)
        sp.sendImage(buf, 8, 8, GL_BGRA_EXT, True, 0)
        sp.releaseSender()
        return True, ""
    except Exception as e:  # noqa
        return False, str(e)[:200]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Easy Reality AutoVJ")
        self.resize(1500, 900)
        self.cfg = Config()
        self._migrate_library_paths()
        set_lang(self.cfg["lang"])

        self.audio = AudioEngine()
        self.audio.energy_scale = float(self.cfg["auto"].get("energy_scale", 1.0))  # 实验性：能量伽马校正，config 里改，不占 UI
        self._song_meta_by_id = {}     # song_id -> {path,title,artist,zh}（现场识别查表用）
        self._fp_db = None
        self.library = {}
        for p in self.cfg["library"]:
            try:
                self.library[p] = MediaItem(p)
            except Exception:
                pass
        # 恢复每个素材独立的大小/位置
        for p, adj in (self.cfg["clip_adj"] or {}).items():
            m = self.library.get(p)
            if m is not None:
                m.img_scale = float(adj.get("scale", 1.0) or 1.0)
                m.img_x = float(adj.get("x", 0.0) or 0.0)
                m.img_y = float(adj.get("y", 0.0) or 0.0)
                m.img_rot = float(adj.get("rot", 0.0) or 0.0)
        # 恢复每个素材的 tag 与多重角色
        for p, tags in (self.cfg["clip_tags"] or {}).items():
            m = self.library.get(p)
            if m is not None and isinstance(tags, list):
                m.tags = list(tags)
        for p, tags in (self.cfg["clip_tags_manual"] or {}).items():
            m = self.library.get(p)
            if m is not None and isinstance(tags, list):
                m.manual_tags = list(tags)
        for p, tags in (self.cfg["clip_tags_disabled"] or {}).items():
            m = self.library.get(p)
            if m is not None and isinstance(tags, list):
                m.disabled_tags = list(tags)
        for p, roles in (self.cfg["clip_roles"] or {}).items():
            m = self.library.get(p)
            if m is not None and isinstance(roles, dict):
                fg, bg = bool(roles.get("fg")), bool(roles.get("bg"))
                if fg and bg:
                    fg = False          # 历史双角色归一：背景优先（铺满场景倾向背景）
                if not fg and not bg and roles.get("mg"):
                    bg = True           # 旧「中景」迁移为背景
                m.roles = {"fg": fg, "bg": bg}
        for p, _v in (self.cfg["clip_excluded"] or {}).items():
            m = self.library.get(p)
            if m is not None:
                m.excluded = True

        self._cur_layer = 0
        # 加载用户自定义的曲风→画面标签映射（运行时覆盖默认）
        from match_engine import set_custom_visual_map
        set_custom_visual_map(self.cfg["genre_visual_map"] or {})
        self.engine = AutoVJEngine(self.cfg, self.audio.state, self.library)
        self.engine.rebuild_from_cfg()
        # 引擎主循环移入独立线程：合成/后处理不再与 GUI 抢线程（卡顿根因修复）。
        # frame_ready 跨线程自动变 queued 投递，GUI 只做贴图。
        from PySide6.QtCore import QThread, QMetaObject
        self.engine_thread = QThread(self)
        self.engine_thread.setObjectName("engineThread")
        self.engine.moveToThread(self.engine_thread)
        self.engine_thread.start()
        # ⚠⚠ 主循环的启动**必须确保在引擎线程里执行**：QTimer 只能在它所属线程里启动，
        # 跨线程调 `timer.start()` 会**静默失败**（只在 stderr 留一条 Qt 警告）——
        # 结果是主循环完全不跑：不合成、不切素材、不解码，画面全黑而 CPU 近乎为零。
        # 实测复现（tools/eco_diag.py）：原先用 `engine_thread.started.connect(self.engine.start)`，
        # 在 PySide6 里槽可能被按 QueuedConnection 投递回**主线程**执行 → `start()` 里
        # `self.timer.start(...)` 跨线程调用 → `isActive()` 永远是 False。
        # 改用 `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`：它明确以
        # **接收者对象的线程**（引擎线程）为准，不依赖信号的连接推断。
        QMetaObject.invokeMethod(self.engine, "start", Qt.QueuedConnection)
        self.audio.state.ndi_feed = self._ndi_audio_feed

        self.out_win = OutputWindow(self.cfg)
        # 输出窗口与预览统一走一个 slot（帧在引擎侧，这里只收序号后各自去取）
        self._drawn_seq = -1
        # 自动图层素材池被重算 → 刷新对应图层行的素材格
        self.engine.auto_layers_changed.connect(self._on_auto_layers_changed)
        # 模态对话框打开时，临时解除输出窗口置顶，避免对话框被置顶输出窗口挡住导致卡死
        self._dialog_filter = _ModalDialogTopFilter(self)
        QApplication.instance().installEventFilter(self._dialog_filter)

        # 这一段初始化会连着触发几十次配置落盘（每次 dump+fsync+备份 170KB），
        # 实测占主窗口构造时间的四成以上。用批量模式合并成一次写。
        with self.cfg.batch():
            self._build_ui()
            self._build_hotkeys()
            self._load_settings_to_ui()
            self._init_recognizer()

        # 「开始自动VJ」未运行时慢速呼吸闪烁（一闪一闪；周期 2.2s，亮暗主题两套色）
        self._breath_t0 = time.perf_counter()
        self._breath_timer = QTimer(self)
        self._breath_timer.setInterval(80)      # ~12fps 足够平滑且省 CPU
        self._breath_timer.timeout.connect(self._breath_tick)
        self._breath_timer.start()              # 初始未运行，开始按钮即闪烁

        # 动态标签升级：v4=三档动态+coverage v2；v5=顺序读全片修复 DXV seek 失效导致的动态误判
        # 启动时后台补扫一次（只跑帧差，不跑 CLIP）。悬浮提示卡让重扫进度可见，不再误以为卡死。
        if int(self.cfg["dyn_tag_ver"] or 0) < 5:
            self._dyn_scan = DynTagScanThread(self)
            self._dyn_scan.finished.connect(self._on_dyn_scan_done)
            self._scan_overlay = ScanOverlayWidget(self, "正在扫描素材库… 0/0")
            self._dyn_scan.progress.connect(self._scan_overlay.update_progress)
            self._dyn_scan.finished.connect(self._scan_overlay.finish)
            self._dyn_scan.start()

        # 素材缺失提醒：换了目录没拷「素材库」文件夹时，画面会全黑，明确告诉用户原因
        missing = sum(1 for p in self.library if not os.path.exists(p))
        if self.library and missing == len(self.library):
            QMessageBox.warning(
                self, "素材文件缺失",
                f"素材库里的 {len(self.library)} 个素材文件全部找不到。\n\n"
                "换目录运行时请把旧目录里的「素材库」文件夹拷到本软件目录旁，\n"
                "或右键素材库 → 设置素材库目录，指向已有素材的位置。\n"
                "修复后重启软件即可恢复。")
        elif missing:
            self.statusBar().showMessage(
                f"⚠ 有 {missing} 个素材文件缺失（红框「文件丢失」），"
                "请检查「素材库」文件夹是否在软件目录旁", 10000)

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(500)

        self.thumb_worker = None
        self.thumb_backfill = None

        # ---- 主线程心跳 + 卡顿看门狗 ----
        # 卡死（"未响应"）时把**所有线程的调用栈**写进 %LOCALAPPDATA%\AutoVJ\ui_stall.log，
        # 下次不用靠猜。详见 src/stallwatch.py。
        self._hb = {"t": time.perf_counter()}
        self._hb_timer = QTimer(self)
        self._hb_timer.setInterval(500)
        self._hb_timer.timeout.connect(self._heartbeat)
        self._hb_timer.start()
        self._watchdog = None
        try:
            import stallwatch
            self._watchdog = stallwatch.StallWatchdog(self._hb)
            self._watchdog.start()
        except Exception:
            pass

        # 启动后补生成缺失的缩略图（导入中崩溃 / 清过缓存 / 手工拷进来的库都会缺图）。
        # 延迟 1.5s 再跑，先让界面画出来；后台线程生成，不卡界面。
        QTimer.singleShot(1500, self._start_thumb_backfill)
        # 再晚一点普查素材编码（GPU 解码要先知道「是不是 DXV」）；错开是为了别和
        # 缩略图补扫一起抢 IO。没开 GPU 解码时这个函数直接返回，不做无用扫描。
        QTimer.singleShot(3000, self._start_codec_probe)
        # 一次性校正 alpha 判定（修好 av.open 的 GBK bug）—— 等缩略图补扫跑完再动，
        # 因为它要重建一部分缩略图文件，和补扫同时写会打架。
        QTimer.singleShot(6000, self._start_alpha_migration)
        self._hl_tick = 0
        # 自动按已保存配置开始采集（主界面可直接演出）
        if self.cfg["audio"].get("source_type"):
            self.start_audio(silent=True)

    # ================= UI 构建 =================
    def _style_gpu_button(self):
        """工具条上的「GPU 解码」开关：主题感知样式 + 选中态高亮（亮着 = 已开启）"""
        v = theme.V
        self.btn_gpu.setStyleSheet(
            f"QPushButton{{padding:2px 10px;border:1px solid {v('border')};"
            f"background:{v('btn')};color:{v('text')};}}"
            f"QPushButton:checked{{background:{v('sel')};color:#9fdcff;border-color:#2f6f9f;}}")

    def _style_kind_buttons(self):
        """主题感知的筛选按钮样式（注册进 theme，切换亮暗时重设）"""
        v = theme.V
        for b in self.kind_group.buttons():
            b.setStyleSheet(
                f"QPushButton{{padding:2px 10px;border:1px solid {v('border')};"
                f"background:{v('btn')};color:{v('text')};}}"
                f"QPushButton:checked{{background:{v('sel')};color:#9fdcff;border-color:#2f6f9f;}}")

    def _style_grid(self):
        self.grid.setStyleSheet(
            f"QListWidget{{background:{theme.V('panel2')};"
            f"border:1px solid {theme.V('border')};}}")

    def _build_ui(self):
        self.main_view = QWidget()
        self.setCentralWidget(self.main_view)

        mv = QVBoxLayout(self.main_view)
        mv.setContentsMargins(4, 4, 4, 4)
        mv.setSpacing(4)

        # ---- 顶部工具条 ----
        tb = QHBoxLayout()
        self.btn_run = QPushButton("▶ " + tr("start"))
        self.btn_run.setProperty("_i18nDynamic", True)   # 文本随运行状态变化
        self.btn_run.setMinimumHeight(32)
        self.btn_run.clicked.connect(self.toggle_run)
        tb.addWidget(self.btn_run)

        for text, cb in ((tr("blackout"), self.toggle_blackout), (tr("freeze"), self.toggle_freeze),
                         (tr("pause_auto"), self.toggle_pause_auto)):
            b = QPushButton(text)
            b.setMinimumHeight(32)
            b.clicked.connect(cb)
            tb.addWidget(b)
        self.btn_next = QPushButton(tr("next_scene") + " ▶▶")
        self.btn_next.setMinimumHeight(32)
        self.btn_next.clicked.connect(self.engine.next_scene)
        tb.addWidget(self.btn_next)

        # GPU 解码（实验）开关放在工具条上：演出中随时能关（关掉立刻回软解路径，
        # 下一次切换的素材就换回来，不打断当前画面）。与设置面板那个勾选框双向同步。
        gpu_sep = QFrame()
        gpu_sep.setFrameShape(QFrame.VLine)
        tb.addWidget(gpu_sep)
        self.btn_gpu = QPushButton("GPU 解码")
        self.btn_gpu.setCheckable(True)
        self.btn_gpu.setMinimumHeight(32)
        self.btn_gpu.setToolTip("开启后用显卡硬件解压 DXV 素材，降低解码 CPU 占用；显卡不支持或素材解不了会自动回退软解，不会黑屏。只作用于普通图层，待机层不受影响。已在播的素材要等下次切换才换路径")
        self.btn_gpu.toggled.connect(self.set_gpu_decode)
        tb.addWidget(self.btn_gpu)
        self._style_gpu_button()
        theme.register(self, self._style_gpu_button)

        # 音源（按钮 + 迷你电平）
        self._audio_sep = QFrame()
        self._audio_sep.setFrameShape(QFrame.VLine)
        tb.addWidget(self._audio_sep)
        self.btn_audio = QPushButton("🎛 音源")
        self.btn_audio.setMinimumHeight(32)
        self.btn_audio.clicked.connect(self.open_audio_dialog)
        tb.addWidget(self.btn_audio)
        self.mini_level = MiniLevel()
        tb.addWidget(self.mini_level)

        tb.addStretch(1)
        # 主题亮暗切换：palette + 全局 QSS 即时生效，注册过的 widget 级样式同步刷新
        self.btn_theme = QPushButton(T("☀ 亮色") if theme.current() == "dark" else T("🌙 暗色"))
        self.btn_theme.setProperty("_i18nDynamic", True)   # 随主题/语言变化，动态重建
        self.btn_theme.setMinimumHeight(32)
        self.btn_theme.setToolTip("切换亮色/暗色主题")
        self.btn_theme.clicked.connect(self.toggle_theme)
        tb.addWidget(self.btn_theme)
        tb.addWidget(QLabel(tr("language")))
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["中文", "English"])
        self.lang_combo.setCurrentIndex(0 if i18n.lang() == "zh" else 1)
        self.lang_combo.currentIndexChanged.connect(self.switch_lang)
        tb.addWidget(self.lang_combo)
        self.btn_hotkey = QPushButton(tr("hotkeys"))
        self.btn_hotkey.clicked.connect(self.open_hotkeys)
        tb.addWidget(self.btn_hotkey)
        self.btn_about = QPushButton(tr("about"))
        self.btn_about.setToolTip(T("版本信息、第三方组件、反馈渠道"))
        self.btn_about.clicked.connect(self.open_about)
        tb.addWidget(self.btn_about)
        self.btn_music = QPushButton("🎵 音乐曲库")
        self.btn_music.setMinimumHeight(32)
        self.btn_music.setToolTip("导入音乐、扫描分析曲风（演出前准备）")
        self.btn_music.clicked.connect(self.open_music_library)
        tb.addWidget(self.btn_music)
        self.btn_gvmap = QPushButton("曲风映射")
        self.btn_gvmap.setMinimumHeight(32)
        self.btn_gvmap.setToolTip("编辑曲风→画面标签的绑定关系")
        self.btn_gvmap.clicked.connect(self.open_genre_visual_editor)
        tb.addWidget(self.btn_gvmap)
        mv.addLayout(tb)

        # ---- 主区 ----
        main_split = QSplitter(Qt.Horizontal)
        left_split = QSplitter(Qt.Vertical)

        self.layers_panel = LayerStackPanel(self)          # 红 + 黄
        left_split.addWidget(self.layers_panel)

        bottom = QSplitter(Qt.Horizontal)
        self.preview_panel = PreviewPanel(self)            # 绿
        bottom.addWidget(self.preview_panel)
        bottom.addWidget(self._build_library())            # 蓝
        bottom.setSizes([560, 520])
        left_split.addWidget(bottom)
        left_split.setSizes([430, 380])
        main_split.addWidget(left_split)

        right = QWidget()
        right.setMinimumWidth(300)
        right.setMaximumWidth(430)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        self.settings = SettingsPanel(self)                # 白：行为模式/效果/逐拍
        right_scroll.setWidget(self.settings)
        rv.addWidget(right_scroll, 1)
        self.output_panel = OutputPanel(self)              # 右下角：输出设置
        rv.addWidget(self.output_panel, 0)
        main_split.addWidget(right)
        main_split.setSizes([1150, 360])
        mv.addWidget(main_split, 1)

        # ---- 状态栏 ----
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel(tr("ready"))
        self.lbl_bpm = QLabel("BPM --")
        self.lbl_energy = QLabel(tr("energy") + " --")
        self.lbl_cpu = QLabel("CPU --")
        self.lbl_warn = QLabel("")
        self.lbl_warn.setStyleSheet("color:#e66;")
        for w in (self.lbl_status, self.lbl_warn, self.lbl_bpm, self.lbl_energy, self.lbl_cpu):
            sb.addPermanentWidget(w)

        self.engine.frame_ready.connect(self._on_engine_frame)
        self.refresh_grid()
        self.layers_panel.rebuild()
        self._fill_tag_combo()
        self.preview_panel.chk_hud.setChecked(bool(self.cfg["ui"].get("hud", True)))
        self.preview_panel.chk_next.setChecked(bool(self.cfg["ui"].get("next_preview", True)))
        self.preview_panel._sync_overlays()
        self.preview_panel.chk_hud.toggled.connect(
            lambda s: self.engine_cfg("ui", "hud", bool(s)))
        self.preview_panel.chk_next.toggled.connect(
            lambda s: self.engine_cfg("ui", "next_preview", bool(s)))

    def _build_library(self):
        """蓝色区：素材库（标签 + 导入 + 网格）"""
        self.lbl_lib_count = QLabel("0")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)
        self.kind_filter = 0   # 0 全部 / 1 图片 / 2 视频
        self.role_filter = 0   # 0 全部 / 1 前景 / 2 背景
        bar = QHBoxLayout()
        t = QLabel("素材库")
        t.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        bar.addWidget(t)
        bar.addWidget(QLabel(tr("tag")))
        self.tag_selected = set()
        self.btn_tagfilter = QPushButton("标签: 全部")
        self.btn_tagfilter.setToolTip("按标签筛选素材（可多选）")
        self.btn_tagfilter.clicked.connect(self.open_tag_filter)
        bar.addWidget(self.btn_tagfilter, 1)
        bar.addWidget(QLabel("角色"))
        self.role_combo = QComboBox()
        self.role_combo.addItems(["全部", "前景", "背景"])
        self.role_combo.currentIndexChanged.connect(self.set_role_filter)
        bar.addWidget(self.role_combo, 0)
        bar.addWidget(QLabel("数量:"))
        bar.addWidget(self.lbl_lib_count)
        lay.addLayout(bar)

        # 图片/视频筛选 + 搜索 + 扫描打标
        fbar = QHBoxLayout()
        self.kind_group = QButtonGroup(self)
        self.kind_filter = 0   # 0 全部 / 1 图片 / 2 视频
        for i, name in enumerate(("全部", "图片", "视频")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setFixedHeight(22)
            b.clicked.connect(lambda _c=False, ii=i: self.set_kind_filter(ii))
            self.kind_group.addButton(b, i)
            fbar.addWidget(b)
        self._style_kind_buttons()
        theme.register(self, self._style_kind_buttons)   # 主题切换时重设 widget 级样式
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索文件名 / 标签")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(lambda _t: self.refresh_grid())
        fbar.addWidget(self.search_box, 1)
        self.btn_scan = QPushButton("扫描打标")
        self.btn_scan.setToolTip("用本地视觉模型为素材自动打内容标签（后台、可续扫）")
        self.btn_scan.clicked.connect(lambda: self.scan_tags())
        fbar.addWidget(self.btn_scan)
        lay.addLayout(fbar)

        row = QHBoxLayout()
        b1 = QPushButton(tr("import_folder"))
        b1.clicked.connect(self.import_folder)
        b2 = QPushButton(tr("import_files"))
        b2.clicked.connect(self.import_files)
        row.addWidget(b1)
        row.addWidget(b2)
        lay.addLayout(row)
        self.grid = LibraryGrid(self)
        # 背景比缩略图纯黑底亮一档，黑底素材缩略图才有轮廓（不能写 ::item，否则背景高亮失效）
        self._style_grid()
        theme.register(self, self._style_grid)
        self.grid.remove_from_layer.connect(self.remove_from_all_layers)
        self.grid.files_dropped.connect(self._import_paths)
        idx = int(self.cfg["ui"].get("thumb_size", 1))
        self.grid.apply_thumb_size(idx)
        self.thumb_bar = QProgressBar()
        self.thumb_bar.hide()
        lay.addWidget(self.thumb_bar)
        lay.addWidget(self.grid, 1)
        return w

    # ================= 快捷键 =================
    def _build_hotkeys(self):
        actions = {
            "toggle_run": self.toggle_run,
            "blackout": self.toggle_blackout,
            "freeze": self.toggle_freeze,
            "pause_auto": self.toggle_pause_auto,
            "next_scene": self.engine.next_scene,
            "toggle_order_mode": self.toggle_order_mode,
            "toggle_beat_mode": self.toggle_beat_mode,
            "intensity_up": lambda: self._nudge_intensity(1),
            "intensity_down": lambda: self._nudge_intensity(-1),
            "manual_transition": self.engine.manual_transition,
            "lock_clip": self.toggle_lock,
            "output_fullscreen": self.out_win.toggle_fullscreen,
            "toggle_ui": self.toggle_ui,
            "color_bypass": self.toggle_color_bypass,
            "panic_reset": self.panic_reset,
        }
        self.hotkeys = HotkeyManager(self, actions, self.cfg)

    # ================= 音源 =================
    def start_audio(self, silent=False):
        a = self.cfg["audio"]
        t = a.get("source_type", "system")
        dev = a.get("device_name", "") or ""
        # 占位文本不能当设备名（语言切换后占位符可能是半角括号/英文）
        if dev.startswith(("（", "(")) or dev.startswith(("正在", "Scanning")):
            dev = ""
        if t == "linein":
            t = "mic"
        self.audio.start(t, dev, mono=bool(self.cfg["audio"].get("mono", True)),
                         sr=int(self.cfg["audio"].get("sample_rate", 0) or 0))
        self.cfg["audio"].update({"source_type": t, "device_name": dev})
        self.cfg.save()

    def apply_audio_settings(self, source_type, device, sr, mono):
        """音源对话框应用：保存配置并重启采集

        ⚠ 原来的 `gain` 参数已去掉（2026-09-25）：那个滑块没接进分析链路，
        而且能量算法对整体增益免疫（实测 0.5~4x 完全无差别），留着只会误导。
        """
        self.cfg["audio"].update({"source_type": source_type, "device_name": device,
                                  "sample_rate": int(sr), "mono": bool(mono)})
        self.cfg.save()
        self.audio.start(source_type, device, mono=mono, sr=int(sr))

    def open_audio_dialog(self):
        AudioSourceDialog(self).exec()

    # ================= 运行控制 =================
    def _breath_tick(self):
        """未运行时「开始自动VJ」按钮的慢速呼吸闪烁：绿色在暗↔亮之间平滑往复（周期 2.2s）。
        亮暗主题各有一套起止色，切换主题时下一个 tick 自动跟随。运行中自动停表恢复普通绿。"""
        if self.engine.running:
            self._breath_timer.stop()
            self.btn_run.setStyleSheet("background:#2e7d32;color:#fff;")
            return
        import math
        ph = (time.perf_counter() - self._breath_t0) % 2.2 / 2.2
        wv = 0.5 - 0.5 * math.cos(ph * 2.0 * math.pi)     # 0..1 平滑呼吸波
        if theme.current() == "light":
            c0, c1 = (46, 96, 62), (110, 200, 125)        # 亮色主题：深绿 ↔ 亮绿
        else:
            c0, c1 = (20, 62, 32), (56, 176, 92)          # 暗色主题：更深 ↔ 更亮的绿
        r, g, b = (int(a + (bb - a) * wv) for a, bb in zip(c0, c1))
        self.btn_run.setStyleSheet(f"background:rgb({r},{g},{b});color:#fff;border-radius:3px;")

    def toggle_run(self):
        if not self.audio.state.running:
            self.start_audio()
        running = self.engine.toggle_run()
        self.btn_run.setText("■ " + tr("stop") if running else "▶ " + tr("start"))
        self.btn_run.setStyleSheet("background:#2e7d32;color:#fff;" if running else "")
        # 「开始自动VJ」按钮：未运行时慢速呼吸闪烁（一闪一闪，周期 2.2s；亮暗主题两套色），
        # 提示用户点击开始；运行中恢复普通绿底。
        if running:
            self._breath_timer.stop()
            self.btn_run.setStyleSheet("background:#2e7d32;color:#fff;")
        else:
            self._breath_t0 = time.perf_counter()
            self._breath_timer.start()
        # 停止只停"自动切换"，输出/预览始终实时（画面照常播放）
        self.lbl_status.setText(tr("running") if running
                                else tr("stopped") + T("（自动切换已停，画面实时）"))
        # 输出设置门控：显示窗口/重置大小/输出显示器仅在「开始」后可操作
        try:
            self.output_panel.set_gate(running)
        except Exception:
            pass
        if running:
            # 每次开始 VJ 都按设置应用（全屏→该屏；窗口→按分辨率重置大小），
            # 解决：之前仅在窗口隐藏时才调，导致窗口已可见时开始VJ不重置的bug
            self.apply_output_screen()
            self.out_win.raise_()
        else:
            if self.out_win.isVisible():
                self.out_win.hide()         # 停止：自动关闭输出窗口（主界面预览仍在实时显示）

    def toggle_blackout(self):
        self.engine.set_blackout(not self.engine.blackout)

    def toggle_freeze(self):
        self.engine.set_freeze(not self.engine.freeze)

    def toggle_pause_auto(self):
        self.engine.set_pause_auto(not self.engine.pause_auto)

    def toggle_order_mode(self):
        """顺序/随机切换（作用于逐拍交替的成对挑素材；常规/快切模式不使用该项）"""
        cur = self.cfg["beat"]["pick_mode"]
        self.cfg["beat"]["pick_mode"] = "rand" if cur == "seq" else "seq"
        self.cfg.save()
        self.settings.beat_pick.setCurrentIndex(0 if cur == "seq" else 1)
        if self.engine.mode != "beat":
            # 避免"按了没反应"的困惑：明确告诉用户这条只在逐拍交替生效
            self.lbl_status.setText(
                tr("order_mode_beat_only") + "　" + self.lbl_status.text())

    def toggle_beat_mode(self):
        """Ctrl+T：在「逐拍交替」与「自动」之间切换。

        注意：模式下拉只有 4 项（自动/常规切/快切/逐拍交替 → 索引 0~3），
        逐拍交替是索引 3。这里曾写成 5（下拉 5 项时代的遗留），
        setCurrentIndex 越界会被 Qt 静默忽略，导致按了热键但下拉框不动。
        """
        if self.engine.mode == "beat":
            self.engine_cfg("mode", "auto", True)
            self.engine_cfg("mode", "manual", "auto")
            self.settings.mode_combo.setCurrentIndex(0)
        else:
            self.engine_cfg("mode", "auto", False)
            self.engine_cfg("mode", "manual", "beat")
            self.engine.mode = "beat"
            self.settings.mode_combo.setCurrentIndex(3)

    def _nudge_intensity(self, d):
        # 4 档（低/中/高/极高）；setCurrentIndex 会触发 currentIndexChanged → 写回配置
        v = max(0, min(3, self.cfg["auto"]["intensity"] + d))
        self.settings.intensity.setCurrentIndex(v)

    def toggle_lock(self):
        self.engine.lock_clip()

    def toggle_ui(self):
        self.setVisible(not self.isVisible())

    def panic_reset(self):
        self.cfg.reset()
        self.engine.rebuild_from_cfg()
        self.layers_panel.rebuild()
        self.hotkeys.apply()
        self.lbl_warn.setText("")
        QMessageBox.information(self, tr("app_name"), T("已恢复默认"))

    # ================= 设置同步 =================
    def engine_cfg(self, sec, key, val):
        self.cfg[sec][key] = val
        self.cfg.save()

    def set_energy_scale(self, scale):
        """能量伽马校正（实验性）：实时作用于采集分析器 + 持久化"""
        self.cfg["auto"]["energy_scale"] = scale
        self.cfg.save()
        try:
            self.audio.energy_scale = scale
        except Exception:
            pass

    def beat_cfg(self, key, val):
        self.engine_cfg("beat", key, val)

    def kv_cfg(self, key, val):
        """Kv 主视觉图层设置：写配置并保存（引擎每帧读 cfg["kv"]）。"""
        self.engine_cfg("kv", key, val)

    def color_cfg(self, key, val):
        """颜色渲染设置：写配置并保存（引擎每帧读 cfg["color"]）。"""
        self.engine_cfg("color", key, val)

    def set_color_palette(self, name):
        """点色卡 = 手动锁定该颜色 10 秒（10 秒后自动回到自动变色）"""
        import time
        self.cfg["color"]["manual_color"] = name
        self.cfg["color"]["manual_t"] = time.perf_counter()
        self.cfg.save()
        self.settings.sync_color_palette(name)

    def toggle_color_bypass(self):
        """一键旁路（原片 / 调色 闪切），供热键与按钮共用"""
        v = not bool(self.cfg["color"].get("bypass", False))
        self.cfg["color"]["bypass"] = v
        self.cfg.save()
        self.settings.btn_color_bypass.setChecked(v)

    def set_postfx(self, key, field, val):
        """后处理特效：更新开关/强度/触发模式/参数并保存"""
        try:
            self.cfg["postfx"]["effects"][key][field] = val
        except (KeyError, TypeError):
            return
        self.cfg.save()

    def set_postfx_auto(self, on):
        """后处理自动联动总开关"""
        try:
            self.cfg["postfx"]["auto"] = bool(on)
        except (KeyError, TypeError):
            return
        self.cfg.save()

    def set_postfx_master(self, on):
        self.cfg["postfx"]["enabled"] = bool(on)
        self.cfg.save()

    def set_postfx_mode(self, i):
        self.cfg["postfx"]["mode"] = "manual" if i == 1 else "auto"
        self.cfg.save()
        st = self.settings
        st.postfx_auto_page.setVisible(i == 0)
        st.postfx_manual_page.setVisible(i == 1)

    def set_postfx_global(self, v):
        self.cfg["postfx"]["global_level"] = int(v)
        self.cfg.save()

    def set_postfx_hold(self, i):
        """自动模式「效果保持拍数」：0 = 实时跟随能量，其余为保持的拍数。

        只影响「多久重新挑一组效果」，与强度无关 ——
        全局强度决定的是效果有多强，不是多久换一次。
        """
        self.cfg["postfx"]["auto_hold_beats"] = [0, 8, 16, 32, 64][max(0, min(4, int(i)))]
        self.cfg.save()

    def open_postfx_detail(self, key):
        """单个特效的详细设置对话框：触发模式 / 曲风绑定 / 衰减 / 音频驱动"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QComboBox, \
            QLineEdit, QCheckBox, QDialogButtonBox
        e = self.cfg["postfx"]["effects"].get(key) or {}
        name = self.settings.postfx_checks.get(key)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"效果设置 - {name.text() if name else key}")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("触发模式"))
        trig = QComboBox()
        trig.addItems(["常驻", "曲风", "事件"])
        trig.setCurrentIndex(["constant", "genre", "event"].index(e.get("trigger", "constant")))
        lay.addWidget(trig)
        ge = QLineEdit(e.get("genres", "") or "")
        ge.setPlaceholderText("曲风名，逗号分隔（如 Hardcore, Trance）")
        ge.setVisible(trig.currentIndex() == 1)
        lay.addWidget(ge)
        lay.addWidget(QLabel("事件衰减时长"))
        dc = QComboBox()
        dc.addItems(["0.5 秒", "1 秒", "1.5 秒", "2 秒", "3 秒", "4 秒"])
        try:
            dc.setCurrentIndex([0.5, 1.0, 1.5, 2.0, 3.0, 4.0].index(float(e.get("decay", 1.5) or 1.5)))
        except ValueError:
            dc.setCurrentIndex(2)
        dc.setVisible(trig.currentIndex() == 2)
        lay.addWidget(dc)
        lay.addWidget(QLabel("音频驱动（强度随频段能量连续缩放）"))
        dr = QComboBox()
        dr.addItems(["无", "低频", "中频", "高频", "总能量"])
        try:
            dr.setCurrentIndex(["off", "bass", "mid", "high", "energy"].index(e.get("drive", "off")))
        except ValueError:
            dr.setCurrentIndex(0)
        lay.addWidget(dr)

        def on_trig(i):
            ge.setVisible(i == 1)
            dc.setVisible(i == 2)
        trig.currentIndexChanged.connect(on_trig)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            self.set_postfx(key, "trigger", ["constant", "genre", "event"][trig.currentIndex()])
            self.set_postfx(key, "genres", ge.text().strip())
            self.set_postfx(key, "decay", [0.5, 1.0, 1.5, 2.0, 3.0, 4.0][dc.currentIndex()])
            self.set_postfx(key, "drive", ["off", "bass", "mid", "high", "energy"][dr.currentIndex()])

    def set_solo(self, idx, on):
        """Solo 互斥：开启某图层的独奏时，自动关闭其它图层的 Solo"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        for i, lay in enumerate(self.engine.layers):
            lay.solo = bool(on) if i == idx else False
        self.layers_panel.refresh_header()
        self.save_layers()

    def save_layers(self):
        """图层属性变化后持久化"""
        self.engine.push_layers_to_cfg()
        self.cfg.save()

    def set_intensity(self, i):
        self.engine_cfg("auto", "intensity", i)

    def set_render_fps(self, i):
        """渲染帧率下拉（0 = 60fps / 1 = 30 / 2 = 20）—— 立即生效，不用重启。

        为什么要给这个开关：素材大多是 25~30fps，按 60fps 合成时有一半左右是把同一帧
        重复合成一遍。4 核老机器上同时开着 VDJ/直播时，这半个核的浪费很值钱。
        """
        fps = [60, 30, 20][max(0, min(2, int(i)))]
        try:
            self.cfg.set("perf", "render_fps", fps)   # Config 的对外写接口（内部 setdefault）
            self.cfg.save()
        except Exception:
            pass
        try:
            self.engine.set_render_fps(fps)
        except Exception:
            pass

    def set_gpu_decode(self, s):
        """GPU 解码（实验）开关 —— 立即生效，但只作用于**新开的播放器**：
        已在播的素材要等下一次切换才会换到 GPU 路径（不打断正在进行的演出）。
        只作用于普通图层，Kv 待机层不受影响（铁律）。
        工具条按钮与设置面板勾选框**双向同步**，从哪边切都行。
        """
        import media_manager as mm
        on = bool(s)
        # 同步另一个入口（blockSignals 避免来回触发）
        for ctl in (getattr(self, "btn_gpu", None),
                    getattr(getattr(self, "settings", None), "chk_gpu", None)):
            if ctl is not None and ctl.isChecked() != on:
                ctl.blockSignals(True)
                ctl.setChecked(on)
                ctl.blockSignals(False)
        try:
            self.cfg.set("perf", "gpu_decode", on)
            self.cfg.save()
        except Exception:
            pass
        mm.set_gpu_decode(on)
        if on:
            try:
                import glctx
                glctx.preinit()      # glfw 的 init 必须在主线程（这里就是 UI 线程）
            except Exception:
                pass
            self._start_codec_probe()
            ok, msg = mm.gpu_status()
            if not ok:
                QMessageBox.information(
                    self, T("GPU 解码"),
                    Tf("这台机器暂时用不了 GPU 解码，已自动回退软解：\n{}", msg))

    def set_mode_selection(self, i):
        if i == 0:
            self.engine_cfg("mode", "auto", True)
            self.engine_cfg("mode", "manual", "auto")
        else:
            self.engine_cfg("mode", "auto", False)
            self.engine_cfg("mode", "manual", ["normal", "fast", "beat"][i - 1])
        self.engine.mode = self.engine.manual_mode() or self.engine.mode
        self.settings.sync_beat_section()

    def set_transition(self, i):
        self.engine_cfg("mode", "transition", ["", "fade", "cut", "slide", "zoom", "glitch"][i])

    def set_resolution(self, i):
        try:
            w, h = [(1280, 720), (1920, 1080), (1080, 1920), (1080, 1080)][i]
        except (IndexError, ValueError):
            return
        self.set_custom_resolution(w, h)

    def set_resolution_text(self, text):
        """手动输入分辨率（宽 x 高）"""
        import re
        if getattr(self, "_loading_ui", False):
            return
        m = re.search(r"(\d{2,5})\s*[xX\u00d7*\s]\s*(\d{2,5})", str(text or ""))
        if not m:
            return
        try:
            w, h = int(m.group(1)), int(m.group(2))
        except ValueError:
            return
        if (w, h) == (self.cfg["output"]["width"], self.cfg["output"]["height"]):
            return
        self.set_custom_resolution(w, h)

    def set_custom_resolution(self, w, h):
        w = max(160, min(7680, int(w)))
        h = max(120, min(4320, int(h)))
        self.cfg["output"]["width"], self.cfg["output"]["height"] = w, h
        self.cfg.save()
        # 线程安全：画布尺寸经 engine.set_canvas_size（锁内重建），不再 GUI 直接赋值
        self.engine.set_canvas_size(w, h)
        # 窗口模式下按新分辨率重置输出窗口大小
        if int(self.cfg["output"].get("screen", -1)) < 0 and self.out_win.isVisible():
            self.out_win.apply_window_size()

    def set_aspect_lock(self, s):
        self.out_win.aspect_lock = bool(s)
        self.cfg["output"]["aspect_lock"] = bool(s)
        self.cfg.save()

    def set_borderless(self, s):
        self.out_win.set_borderless(bool(s))
        self.cfg["output"]["borderless"] = bool(s)
        self.cfg.save()

    def set_always_top(self, s):
        s = bool(s)
        self.out_win.set_always_top(s)
        self.cfg["output"]["always_top"] = s
        self.cfg.save()
        # 同步复选框（阻止信号避免递归）
        try:
            cb = self.output_panel.chk_top
            cb.blockSignals(True)
            cb.setChecked(s)
            cb.blockSignals(False)
        except Exception:
            pass

    def set_output_screen(self, i):
        """输出显示器：0=窗口模式，1..n=全屏到对应显示器"""
        self.cfg["output"]["screen"] = i - 1
        self.cfg.save()
        # 运行中立即生效（否则只存配置，要等下次开始才应用 →「更改失效」）
        if self.engine.running:
            self.apply_output_screen()

    def set_spout_enabled(self, s):
        s = bool(s)
        if s:
            # 勾选时立即探测一次（与 NDI 一致）：Spout 失败以前是完全静默的 ——
            # 界面上勾着、其实一帧都没发出去，用户只会以为「勾了没用」。
            ok, err = _probe_spout_once()
            if not ok:
                QMessageBox.warning(
                    self, "Spout 不可用",
                    "无法启用 Spout 输出。\n\n%s\n\n"
                    "常见原因：显卡驱动不支持共享纹理（GL/DX 互操作）。\n"
                    "（NDI 输出不依赖这个，可改用 NDI）" % err)
                self.cfg["output"]["spout_enabled"] = False
                try:
                    self.output_panel.chk_spout.blockSignals(True)
                    self.output_panel.chk_spout.setChecked(False)
                    self.output_panel.chk_spout.blockSignals(False)
                except Exception:
                    pass
                self.cfg.save()
                return
        else:
            # 取消勾选要把 Spout 源**释放掉**：以前只停发不释放，源会一直挂在系统里
            # （OBS / Resolume 的源列表里仍然看得见，只是没有新帧）。
            sp = getattr(self.engine, "_spout", None)
            if sp is not None:
                try:
                    sp[0].releaseSender()
                except Exception:
                    pass
                self.engine._spout = None
        self.cfg["output"]["spout_enabled"] = bool(s)
        self.cfg.save()
        self.engine._spout_fail = False

    def set_spout_name(self):
        self.cfg["output"]["spout_name"] = self.output_panel.spout_name.text().strip() or "EasyRealityAutoVJ"
        self.cfg.save()
        self.engine._spout = None   # 名字变了重建 sender

    def set_ndi_enabled(self, s):
        s = bool(s)
        if s:
            # 勾选时立即探测 NDI 运行时。**运行时是随 cyndilib 一起打包在软件里的**
            # （实测本机从未安装过 NDI，但 get_ndi_version() 能报出 6.1.1.0 ——
            #  读的就是自带的那份 Processing.NDI.Lib.x64.dll），所以用户**不需要安装任何东西**；
            # 加载失败一般是被杀毒隔离或文件缺失。
            #
            # ⚠⚠ 这里只能调 probe_runtime()，**不能调 ndi._ensure()** —— 后者需要真实分辨率
            # （发送端帧尺寸只能在 open() 之前定死），不给尺寸时它按设计返回 False，
            # 会把用户的勾选误判成失败并自动取消掉。2026-09-25 的回归 bug 就出在这里：
            # 引擎侧的 NDI 已经修好了，但用户一勾选就被这行弹窗挡回去，等于完全打不开。
            from ndi_out import get_ndi, probe_runtime
            ok, detail = probe_runtime()
            if not ok:
                QMessageBox.warning(
                    self, T("NDI 不可用"),
                    T("NDI 运行时加载失败，无法启用 NDI 输出。") + "\n\n" +
                    T("运行时是随软件一起提供的，不需要单独安装。"
                      "常见原因是杀毒软件隔离了软件目录里的 Processing.NDI.Lib.x64.dll，"
                      "请把它加入白名单；或者重新解压一次安装包再试。") +
                    "\n\n" + T("错误详情：") + "\n" + detail)
                self.cfg["output"]["ndi_enabled"] = False
                try:
                    self.output_panel.chk_ndi.blockSignals(True)
                    self.output_panel.chk_ndi.setChecked(False)
                    self.output_panel.chk_ndi.blockSignals(False)
                except Exception:
                    pass
                self.cfg.save()
                return
            # 运行时没问题 —— 发送端等第一帧来了再按真实分辨率建立（engine._ndi_send）
            name = self.cfg["output"].get("ndi_name") or "EasyRealityAutoVJ"
            ndi = get_ndi(name)
            self.cfg["output"]["ndi_enabled"] = True
            self.cfg.save()
            self.engine._ndi = ndi
            self.engine._ndi_fail = False
            print("[NDI] 已启用输出（运行时 %s），发送端将在第一帧按实际分辨率建立" % detail)
        else:
            self.cfg["output"]["ndi_enabled"] = False
            self.cfg.save()
            if self.engine._ndi is not None:
                self.engine._ndi.close()
                self.engine._ndi = None
        # 同步给采集循环：未启用 NDI 时它就不再为每块音频做 asarray/reshape/copy
        try:
            self.audio.state.ndi_audio_on = bool(self.cfg["output"].get("ndi_enabled"))
        except Exception:
            pass

    def set_ndi_name(self):
        self.cfg["output"]["ndi_name"] = self.output_panel.ndi_name.text().strip() or "EasyRealityAutoVJ"
        self.cfg.save()
        self.engine._ndi = None

    def _ndi_audio_feed(self, pcm, sr):
        ndi = self.engine._ndi
        if ndi is not None and self.cfg["output"].get("ndi_enabled"):
            ndi.feed_audio(pcm, sr)

    def apply_output_screen(self):
        idx = int(self.cfg["output"].get("screen", -1))
        screens = QGuiApplication.screens()
        if 0 <= idx < len(screens):
            self.out_win.show()
            self.out_win.show_fullscreen_on(screens[idx])
        else:
            self.out_win.show_windowed()

    def reset_window_size(self):
        """手动重置输出窗口大小（输出设置里的按钮）"""
        self.out_win.reset_window_size()

    def _load_settings_to_ui(self):
        a = self.cfg["auto"]
        # 钳到有效档位（0~3）：脏配置越界时 setCurrentIndex 会被 Qt 忽略 → 界面与配置对不上
        self.settings.intensity.setCurrentIndex(max(0, min(3, int(a.get("intensity", 1) or 0))))
        tr_ov = a.get("transition") if isinstance(a.get("transition"), str) else ""
        tr_ov = (self.cfg["mode"].get("transition") or tr_ov or "")
        try:
            self.settings.transition.setCurrentIndex(["", "fade", "cut", "slide", "zoom", "glitch"].index(tr_ov))
        except ValueError:
            self.settings.transition.setCurrentIndex(0)
        self.settings.energy_map.setChecked(a.get("energy_map", True))
        self.settings.bpm_sync.setChecked(a.get("bpm_speed_sync", True))
        # 渲染帧率：60 / 30 / 20（脏值一律回落到 60）
        try:
            _rf = int((self.cfg["perf"] or {}).get("render_fps", 60) or 60)
        except Exception:
            _rf = 60
        self.settings.render_fps.setCurrentIndex({60: 0, 30: 1, 20: 2}.get(_rf, 0))
        # GPU 解码（实验）：配置 → 复选框 + 模块总闸（复选框 setChecked 会触发 stateChanged
        # → set_gpu_decode 再写一遍配置，值相同无副作用）
        try:
            _gd = bool((self.cfg["perf"] or {}).get("gpu_decode", False))
        except Exception:
            _gd = False
        self.settings.chk_gpu.setChecked(_gd)
        # 工具条上的同一个开关也要跟上（两个入口共享状态；上面 setChecked 若值没变
        # 不会发信号，所以这里显式同步一次）
        try:
            self.btn_gpu.blockSignals(True)
            self.btn_gpu.setChecked(_gd)
            self.btn_gpu.blockSignals(False)
        except Exception:
            pass
        try:
            import media_manager as mm
            mm.set_gpu_decode(_gd)
            if _gd:
                import glctx
                glctx.preinit()      # 主线程先 init glfw（解码线程只建窗口）
        except Exception:
            pass
        self.settings.energy_scale.setValue(int(round(a.get("energy_scale", 1.0) * 100)))
        m = self.cfg["mode"]
        manual = m.get("manual", "auto")
        if m.get("auto", True) or manual in (None, "auto"):
            self.settings.mode_combo.setCurrentIndex(0)
        else:
            try:
                self.settings.mode_combo.setCurrentIndex(
                    ["normal", "fast", "beat"].index(manual) + 1)
            except ValueError:
                self.settings.mode_combo.setCurrentIndex(0)
        self.settings.sens.setCurrentIndex(int(m.get("sensitivity", 1)))
        b = self.cfg["beat"]
        self.settings.beat_pick.setCurrentIndex(0 if b.get("pick_mode", "seq") == "seq" else 1)
        try:
            self.settings.beat_interval.setCurrentIndex(
                [0.25, 0.5, 1, 2, 4, 8, 16].index(float(b.get("interval_beats", 1) or 1)))
        except ValueError:
            self.settings.beat_interval.setCurrentIndex(2)
        self.settings.beat_bars.setValue(b.get("bars_per_pair", 2))
        try:
            self.settings.beat_end.setCurrentIndex(["loop", "reverse"].index(b.get("end_action", "loop")))
        except ValueError:
            self.settings.beat_end.setCurrentIndex(0)
        # Kv 主视觉图层设置回填（此时 signal 已建，setValue 会触发写回，值一致无害）
        k = self.cfg["kv"]
        self.settings.kv_db.setValue(int(round(float(k.get("db_threshold", -50.0)) * 10)))
        self.settings.kv_silent.setValue(int(round(float(k.get("silent_delay", 2.0)) * 10)))
        self.settings.kv_resume.setValue(int(round(float(k.get("resume_delay", 2.5)) * 10)))
        self.settings.kv_in_dur.setValue(int(round(float(k.get("in_dur", 1.5)) * 10)))
        self.settings.kv_out_dur.setValue(int(round(float(k.get("out_dur", 1.5)) * 10)))
        self.settings.kv_switch.setValue(int(round(float(k.get("switch_sec", 0.0)) * 10)))
        try:
            self.settings.kv_in_trans.setCurrentIndex(
                ["fade", "cut", "glitch"].index(k.get("in_trans", "fade")))
        except ValueError:
            self.settings.kv_in_trans.setCurrentIndex(0)
        try:
            self.settings.kv_out_trans.setCurrentIndex(
                ["fade", "cut", "zoom"].index(k.get("out_trans", "fade")))
        except ValueError:
            self.settings.kv_out_trans.setCurrentIndex(0)
        # 颜色渲染回填
        # 注意：Config 是 dict 风格、没有 .get()，只能 cfg["color"]（内层才是普通 dict）
        col = self.cfg["color"]
        self.settings.chk_color_on.setChecked(bool(col.get("on", True)))
        self.settings.chk_color_auto.setChecked(bool(col.get("auto", True)))
        self.settings.color_mode.setCurrentIndex(1 if col.get("mode") == "duotone" else 0)
        self.settings.color_strength.setValue(int(col.get("strength", 60)))
        self.settings.color_rotate.setValue(int(col.get("rotate_sec", 45)))
        self.settings.chk_color_burst.setChecked(bool(col.get("burst", True)))
        fmode = col.get("flicker_mode")
        if fmode is None:                    # 老配置：从旧的 skip_flicker 推断
            fmode = "on" if col.get("skip_flicker", True) else "off"
        try:
            self.settings.color_flicker.setCurrentIndex(["on", "off", "random"].index(fmode))
        except ValueError:
            self.settings.color_flicker.setCurrentIndex(0)
        self.settings.btn_color_bypass.setChecked(bool(col.get("bypass", False)))
        # 手动锁定若已过期就不再高亮（避免看起来像锁着其实已回自动）
        import time as _t
        lock = str(col.get("manual_color", "") or "")
        if lock and (_t.perf_counter() - float(col.get("manual_t", 0.0) or 0.0)) >= float(col.get("manual_hold", 10.0)):
            lock = ""
        self.settings.sync_color_palette(lock)
        # 后处理特效回填
        p = self.cfg["postfx"]
        self.settings.chk_postfx_on.setChecked(bool(p.get("enabled", True)))
        mi = 1 if p.get("mode", "auto") == "manual" else 0
        self.settings.postfx_mode.setCurrentIndex(mi)
        self.settings.postfx_auto_page.setVisible(mi == 0)
        self.settings.postfx_manual_page.setVisible(mi == 1)
        self.settings.postfx_global.setValue(int(p.get("global_level", 60)))
        self.settings.postfx_auto.setChecked(bool(p.get("auto", True)))
        try:
            self.settings.postfx_hold.setCurrentIndex(
                [0, 8, 16, 32, 64].index(int(p.get("auto_hold_beats", 0) or 0)))
        except ValueError:
            self.settings.postfx_hold.setCurrentIndex(0)
        for key, e in (p.get("effects") or {}).items():
            cb = self.settings.postfx_checks.get(key)
            sl = self.settings.postfx_sliders.get(key)
            if cb is not None:
                cb.setChecked(bool(e.get("on", False)))
            if sl is not None:
                sl.setValue(int(e.get("level", 0)))
        o = self.cfg["output"]
        w, h = o.get("width", 1280), o.get("height", 720)
        try:
            self.output_panel.resolution.setCurrentIndex(
                [(1280, 720), (1920, 1080), (1080, 1920), (1080, 1080)].index((w, h)))
        except ValueError:
            self.output_panel.resolution.setCurrentIndex(0)
        self.output_panel.chk_aspect.setChecked(o.get("aspect_lock", True))
        self.output_panel.chk_border.setChecked(o.get("borderless", False))
        self.output_panel.chk_top.setChecked(o.get("always_top", False))
        try:
            self.output_panel.screen_combo.setCurrentIndex(int(o.get("screen", -1)) + 1)
        except Exception:
            self.output_panel.screen_combo.setCurrentIndex(0)
        self.output_panel.chk_spout.setChecked(o.get("spout_enabled", False))
        self.output_panel.spout_name.setText(o.get("spout_name", "EasyRealityAutoVJ"))
        self.output_panel.chk_ndi.setChecked(o.get("ndi_enabled", False))
        self.output_panel.ndi_name.setText(o.get("ndi_name", "EasyRealityAutoVJ"))
        # Kv 主视觉图层设置区显隐（按当前是否已存在 Kv 图层）
        self.settings.sync_kv_section()

    # ================= 素材 =================
    def import_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("import_folder"))
        if not d:
            return
        # 只把目录交给后台导入线程去扫描（以前在 GUI 线程 os.walk 整个目录，大目录会卡死）
        self._import_paths([d])

    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, tr("import_files"), "", MEDIA_FILTER)
        self._import_paths(files)

    def _init_recognizer(self):
        """曲库就绪后启用现场识别：建指纹库 + song_id→meta 映射（无库则跳过）"""
        from config import app_base_dir
        db_path = os.path.join(app_base_dir(), "fingerprints.db")
        if not os.path.exists(db_path):
            return
        meta = self.cfg["music_meta"] or {}
        for p, m in meta.items():
            sid = m.get("song_id")
            if sid:
                self._song_meta_by_id[sid] = {
                    "path": p,
                    "title": m.get("title") or os.path.basename(p),
                    "artist": m.get("artist") or "",
                    "genres": m.get("genres") or [],
                }
        if not self._song_meta_by_id:
            return
        # 注入 song_id → 曲风映射给 engine（自动匹配优先用指纹识别歌的精确曲风）
        self.engine.set_song_genres(self._song_meta_by_id)
        try:
            from fp import FingerprintDB
            if self._fp_db is not None:      # 重建：关闭旧连接，扫描后拿最新数据
                try:
                    self._fp_db.close()
                except Exception:
                    pass
            self._fp_db = FingerprintDB(db_path)
            self.audio.set_recognizer(self._fp_db)
        except Exception:
            pass

    def open_music_library(self):
        from panels import MusicLibraryDialog
        self.music_dialog = MusicLibraryDialog(self)
        self.music_dialog.exec()

    def open_genre_visual_editor(self):
        """曲风→画面标签 映射编辑器（保存后自动图层按新映射重匹配）"""
        from panels import GenreVisualEditDialog
        GenreVisualEditDialog(self).exec()

    def import_music_dir(self):
        d = QFileDialog.getExistingDirectory(self, "导入音乐文件夹")
        if not d:
            return
        self._add_music(scan_music_folder(d))

    def import_music_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "导入音乐文件", "", MUSIC_FILTER)
        self._add_music(files)

    def _add_music(self, paths):
        lib = self.cfg["music_library"]
        added = [p for p in paths if p not in lib]
        if not added:
            return
        lib.extend(added)
        self.cfg["music_library"] = lib
        self.cfg.save()
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.refresh()

    def scan_music(self, force=False):
        if getattr(self, "_music_scan", None) is not None and self._music_scan.isRunning():
            QMessageBox.information(self, "扫描分析", "已有扫描在进行中，请稍候。")
            return
        if force:
            ret = QMessageBox.question(
                self, "全量重扫",
                "全量重扫将忽略已有结果，重新分析曲库里的全部歌曲（含指纹与曲风）。\n可能耗时较长，确定继续吗？",
                QMessageBox.Yes | QMessageBox.Cancel)
            if ret != QMessageBox.Yes:
                return
        self._music_scan = MusicScanThread(self, None, force)
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.progress.show()
            dlg.progress.setRange(0, 1)
            dlg.progress.setValue(0)
            self._music_scan.progress.connect(
                lambda d, t, _p: (dlg.progress.setRange(0, max(1, t)),
                                  dlg.progress.setValue(d)))
            self._music_scan.finished.connect(
                lambda: (dlg.progress.hide(), dlg.refresh(), self._init_recognizer()))
        self._music_scan.start()

    def edit_music_genre(self, path):
        from panels import GenreEditDialog
        m = self.cfg["music_meta"].get(path) or {}
        GenreEditDialog(self, path, m.get("genres") or []).exec()

    def rescan_music(self, path):
        """重新分析扫描单首曲目（重建指纹 + 重查曲风 + 重算段落）"""
        if getattr(self, "_music_scan", None) is not None and self._music_scan.isRunning():
            QMessageBox.information(self, "重新分析", "已有扫描在进行中，请稍候。")
            return
        self._music_scan = MusicScanThread(self, [path], True)
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.progress.show()
            dlg.progress.setRange(0, 1)
            self._music_scan.progress.connect(
                lambda d, t, _p: (dlg.progress.setRange(0, max(1, t)), dlg.progress.setValue(d)))
            self._music_scan.finished.connect(
                lambda: (dlg.progress.hide(), dlg.refresh(), self._init_recognizer()))
        self._music_scan.start()

    def show_music_info(self, path):
        """曲目信息：文件/歌名/艺人/曲风/来源/时长/采样率/指纹/路径"""
        m = self.cfg["music_meta"].get(path) or {}
        dur = "未知"
        sr_txt = ""
        try:
            import soundfile as sf
            info = sf.info(path)
            dur = f"{info.duration:.0f} 秒"
            sr_txt = f"{info.samplerate} Hz · {info.channels} 声道"
        except Exception:
            pass
        genres = "、".join(m.get("genres") or []) or "未分析"
        text = (
            f"文件：{os.path.basename(path)}\n"
            f"歌名：{m.get('title') or '未知'}\n"
            f"艺人：{m.get('artist') or '未知'}\n"
            f"曲风：{genres}\n"
            f"曲风来源：{m.get('source') or '未分析'}\n"
            f"时长：{dur}\n"
            f"采样率：{sr_txt or '未知'}\n"
            f"指纹ID：{m.get('song_id') or '-'}\n"
            f"\n完整路径：\n{path}"
        )
        QMessageBox.information(self, "曲目信息", text)

    def show_clear_music_analysis(self):
        """清除全部已分析指纹与曲风（带二次确认，可选保留手动设置的曲风）"""
        if getattr(self, "_music_scan", None) is not None and self._music_scan.isRunning():
            QMessageBox.information(self, "清除分析数据", "扫描正在进行中，请稍后再清除。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("清除分析数据")
        box.setText("将删除全部已建立的指纹与自动识别的曲风。\n音乐文件本身和曲库列表不会被删除。")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("清除")
        cb = QCheckBox("保留手动设置过的曲风")
        box.setCheckBox(cb)
        if box.exec() != QMessageBox.Yes:
            return
        self.clear_music_analysis(cb.isChecked())
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.refresh()

    def clear_music_analysis(self, keep_manual):
        meta = self.cfg["music_meta"] or {}
        if keep_manual:
            self.cfg["music_meta"] = {p: m for p, m in meta.items()
                                      if (m or {}).get("source") == "manual"}
        else:
            self.cfg["music_meta"] = {}
        self.cfg.save()
        # 指纹库：先关闭本进程连接（Windows 下文件被占用无法删除），再删库文件
        if self._fp_db is not None:
            try:
                self._fp_db.close()
            except Exception:
                pass
        self._song_meta_by_id = {}
        self._fp_db = None
        try:
            self.audio.set_recognizer(None)
        except Exception:
            pass
        from config import app_base_dir
        dbp = os.path.join(app_base_dir(), "fingerprints.db")
        for f in (dbp, dbp + "-wal", dbp + "-shm"):
            try:
                os.remove(f)
            except OSError:
                pass

    def remove_music(self, path):
        if path in self.cfg["music_library"]:
            self.cfg["music_library"].remove(path)
        self.cfg["music_meta"].pop(path, None)
        self.cfg.save()
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.refresh()

    def _migrate_library_paths(self):
        """便携式路径迁移：软件换了目录/盘符后，config 里的旧素材绝对路径会失效
        （表现为素材库清空、必须重新导入）。按「素材库」目录锚点把旧路径重映射到
        当前素材库目录，素材/标签/角色/摆位/排除等全部跟着迁移。"""
        lib_root = os.path.abspath(self.library_dir())
        anchor = os.path.basename(lib_root)          # 默认「素材库」

        def resolve(p):
            if not p or (os.path.isabs(p) and os.path.exists(p)):
                return p
            norm = p.replace("/", "\\")
            if not os.path.isabs(p):                 # 相对路径 → 相对 exe 目录
                cand = os.path.join(exe_dir(), norm)
                if os.path.exists(cand):
                    return os.path.normpath(cand)
            parts = norm.split("\\")
            for i, seg in enumerate(parts):          # 旧「素材库」之后的子路径 → 新库根
                if seg == anchor and i + 1 < len(parts):
                    cand = os.path.join(lib_root, *parts[i + 1:])
                    if os.path.exists(cand):
                        return os.path.normpath(cand)
            return p

        mapping = {}

        def mp(p):
            q = resolve(p)
            if q != p:
                mapping[p] = q
            return q

        self.cfg["library"] = [mp(p) for p in (self.cfg["library"] or [])]
        for ld in (self.cfg["layers"] or []):
            for k in ("clips", "auto_clips"):
                if ld.get(k):
                    ld[k] = [mp(x) for x in ld[k]]
        for key in ("clip_adj", "clip_tags", "clip_tags_manual", "clip_tags_disabled",
                    "clip_roles", "clip_excluded", "music_meta"):
            d = self.cfg[key]
            if d:
                self.cfg[key] = {mp(p): v for p, v in d.items()}
        if self.cfg["music_library"]:
            self.cfg["music_library"] = [mp(p) for p in self.cfg["music_library"]]
        if mapping:
            self.cfg.save()

    def library_dir(self):
        """素材库目录：cfg 指定，或默认 exe 同级 / 素材库。
        若保存的值是乱码或目录已不存在，回退默认（自愈损坏的 library_dir）。"""
        d = self.cfg["output"].get("library_dir") or ""
        if d and os.path.isdir(d):
            return d
        return os.path.join(exe_dir(), "素材库")

    def set_library_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择素材库目录", self.library_dir())
        if not d:
            return
        self.cfg["output"]["library_dir"] = d
        self.cfg.save()
        QMessageBox.information(self, "素材库目录",
                                f"已设置：\n{d}\n\n后续「复制/移动」导入的素材会放到这里。")

    def _get_import_strategy(self):
        remembered = self.cfg["output"].get("import_strategy") or ""
        if remembered in ("copy", "move", "link"):
            return remembered
        return self._ask_import_strategy()

    def _ask_import_strategy(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("导入素材")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("如何导入这些素材？"))
        rb_copy = QRadioButton("复制到素材库（原文件保留）")
        rb_move = QRadioButton("移动到素材库（复制后删除原文件）")
        rb_link = QRadioButton("仅引用（不复制，保留原路径）")
        rb_copy.setChecked(True)
        for rb in (rb_copy, rb_move, rb_link):
            lay.addWidget(rb)
        chk_remember = QCheckBox("记住我的选择（以后不再询问）")
        lay.addWidget(chk_remember)
        btns = QHBoxLayout()
        ok = QPushButton("确定")
        cancel = QPushButton("取消")
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)
        if dlg.exec() != QDialog.Accepted:
            return None
        strat = "copy" if rb_copy.isChecked() else ("move" if rb_move.isChecked() else "link")
        if chk_remember.isChecked():
            self.cfg["output"]["import_strategy"] = strat
            self.cfg.save()
        return strat

    def _ask_duplicate_import(self, dupes):
        """导入时发现素材库已有同名/同路径素材 → 询问处理方式。
        返回 "replace" / "skip" / "add"；关掉对话框视同"不添加"。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("发现重复素材")
        lay = QVBoxLayout(dlg)
        names = "\n".join("· " + os.path.basename(p) for p in dupes[:8])
        if len(dupes) > 8:
            names += f"\n· …等共 {len(dupes)} 个"
        lay.addWidget(QLabel(f"以下 {len(dupes)} 个素材已在素材库中：\n\n{names}"))
        rb_replace = QRadioButton("替换旧素材（保留原标签/角色，用新文件替换）")
        rb_skip = QRadioButton("不添加（保持素材库不变）")
        rb_add = QRadioButton("仍然加入（复制模式下会生成 \"名字 (1)\" 的独立新条目）")
        rb_replace.setChecked(True)
        for rb in (rb_replace, rb_skip, rb_add):
            lay.addWidget(rb)
        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.clicked.connect(dlg.accept)
        btns.addStretch(1)
        btns.addWidget(ok)
        lay.addLayout(btns)
        dlg.exec()
        if rb_skip.isChecked():
            return "skip"
        if rb_add.isChecked():
            return "add"
        return "replace"

    def _purge_for_replace(self, old_path):
        """替换素材：移除旧条目（图层引用一并清理）；旧文件在素材库目录内则删除，
        这样新文件落盘时复用原文件名，按路径记忆的标签/角色/摆位自动延续。"""
        it = self.library.pop(old_path, None)
        if it is not None:
            self.remove_from_all_layers(it)
            # 自动图层兜底清理（与 remove_media 一致）
            for i, lay in enumerate(self.engine.layers):
                if not getattr(lay, "auto_mode", False):
                    continue
                if any(getattr(c, "path", None) == old_path for c in lay.clips):
                    lay.clips = [c for c in lay.clips if getattr(c, "path", None) != old_path]
                    lay.auto_clips = list(lay.clips)
                    if lay.cur >= len(lay.clips):
                        lay.cur = -1
                        lay.prev_idx = -1
                    self.layers_panel.sync_layer(i)
        lib = os.path.abspath(self.library_dir())
        ap = os.path.abspath(old_path)
        if ap.startswith(lib + os.sep) and os.path.exists(ap):
            try:
                os.remove(ap)
            except OSError:
                pass

    def _copy_into_lib(self, src, lib, delete_after=False):
        name = os.path.basename(src)
        dst = os.path.join(lib, name)
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(lib, f"{base} ({i}){ext}")
            i += 1
        try:
            shutil.copy2(src, dst)
            if delete_after:
                os.remove(src)
            return dst
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法复制 {name}：{e}")
            return None

    def _import_paths(self, paths):
        """导入入口（按钮 / 拖入 / 右键菜单都走这里）。

        **全程后台**：扫描文件夹、复制/移动落盘、建 MediaItem 都在 ImportWorker 里做，
        界面只按批插入图标（缩略图仍交给 ThumbWorker）。
        修复：导入上百 GB / 上千素材时界面卡死（旧实现三件事都在 GUI 线程里干）。
        """
        if not paths:
            return
        w0 = getattr(self, "_import_worker", None)
        if w0 is not None and w0.isRunning():
            QMessageBox.information(self, tr("import_files"),
                                    T("正在导入中，请稍候…"))
            return
        strat = self._get_import_strategy()
        if strat is None:
            return
        # 重复检测：只针对「直接选中的文件」；文件夹展开后的重复在批处理里静默跳过
        dupes = [p for p in paths if os.path.isfile(p) and p in self.library]
        if dupes:
            act = self._ask_duplicate_import(dupes)
            if act == "skip":
                paths = [p for p in paths if p not in self.library]
                if not paths:
                    return
            elif act == "replace":
                for p in dupes:
                    self._purge_for_replace(p)

        from media_manager import ImportWorker
        self._import_new = []
        self._import_skip = 0
        self.thumb_bar.show()
        self.thumb_bar.setRange(0, 0)          # 扫描阶段：不确定进度
        self.lbl_status.setText(T("正在扫描素材…"))
        w = ImportWorker(paths, strat, self.library_dir(),
                         set(self.library.keys()), self._copy_into_lib_silent)
        w.scanned.connect(self._on_import_scanned)
        w.batch.connect(self._on_import_batch)
        w.done.connect(self._on_import_done)
        w.failed.connect(self._on_import_failed)
        self._import_worker = w
        w.start()

    def _copy_into_lib_silent(self, src, lib, delete_after=False):
        """复制/移动落盘（后台线程用）：失败只返回 None，不弹窗（弹窗必须在 GUI 线程）"""
        name = os.path.basename(src)
        dst = os.path.join(lib, name)
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(lib, f"{base} ({i}){ext}")
            i += 1
            if i > 9999:
                return None
        try:
            shutil.copy2(src, dst)
            if delete_after:
                os.remove(src)
            return dst
        except Exception:
            return None

    def _on_import_scanned(self, n):
        self.thumb_bar.setRange(0, max(1, n))
        self.thumb_bar.setValue(0)
        self.lbl_status.setText(Tf("已发现 {} 个素材，正在导入…", n))

    def _on_import_batch(self, items):
        """一批素材到手：入素材库 + 增量插入列表（用缓存图标，不解码）"""
        if not items:
            return
        fresh = []
        for it in items:
            if it.path in self.library:
                continue
            self.library[it.path] = it
            fresh.append(it)
        self._import_new.extend(fresh)
        if fresh:
            self._append_grid_items(fresh)
        self.thumb_bar.setValue(min(self.thumb_bar.maximum(),
                                    self.thumb_bar.value() + len(items)))

    def _append_grid_items(self, items):
        """只插入这批素材（比整表重建快得多）。有筛选/搜索时不插，等导入结束统一重建。"""
        if self.tag_selected or (self.search_box.text() or "").strip() or \
                self.kind_filter or self.role_filter:
            return
        sz = self.grid.iconSize()
        for it in items:
            li = QListWidgetItem(it.icon_cached(sz.width(), sz.height()), self._item_text(it))
            li.setData(Qt.UserRole, it)
            li.setToolTip(self._item_tooltip(it))
            self._tint_role(li, it)
            self.grid.addItem(li)
        self.lbl_lib_count.setText(f"{self.grid.count()}/{len(self.library)}")

    def _on_import_done(self, new_n, skip_n):
        new_items = list(getattr(self, "_import_new", []) or [])
        self.cfg["library"] = list(self.library.keys())
        self.cfg.save()
        self.refresh_grid()            # 统一重建（顺带应用筛选/排序、修正计数）
        self._fill_tag_combo()
        if new_items:
            self.thumb_bar.show()
            self.thumb_bar.setRange(0, len(new_items))
            self.thumb_bar.setValue(0)
            self.thumb_worker = ThumbWorker(new_items)
            self.thumb_worker.progress.connect(
                lambda d, t: self.thumb_bar.setValue(int(d / max(t, 1) * 100)))
            self.thumb_worker.done.connect(self._thumbs_done)
            self.thumb_worker.start()
        else:
            self.thumb_bar.hide()
        self.lbl_status.setText(
            Tf("导入完成：新增 {} 个，跳过 {} 个（重复或无法识别）", new_n, skip_n)
            + T("　记得点「扫描打标」给新素材打标签"))

    def _on_import_failed(self, msg):
        self.thumb_bar.hide()
        self.lbl_status.setText(Tf("导入出错：{}", msg))
        QMessageBox.warning(self, tr("import_files"), Tf("导入出错：{}", msg))

    def _thumbs_done(self):
        self.thumb_bar.hide()
        # 后台缩略图生成后丢弃图标缓存，避免初次同步生成失败导致的空图标被缓存住
        tw = getattr(self, "thumb_worker", None)
        if tw is not None:
            for m in getattr(tw, "items", []) or []:
                try:
                    m.drop_icon_cache()
                except Exception:
                    pass
        self.refresh_grid(icons_only=True)

    # ---- 缺缩略图的补扫（启动 / 清缓存后 / 导入中断后）----
    def _heartbeat(self):
        """主线程心跳：卡顿看门狗靠它判断主线程有没有卡住（见 src/stallwatch.py）。"""
        self._hb["t"] = time.perf_counter()

    def _start_thumb_backfill(self, items=None):
        """后台补齐缺失的缩略图。items 为 None 时扫整个素材库。

        缩略图现在是「后台预生成 + GUI 只读现成图标」，所以缺图的素材必须靠这条
        后台链路补回来，否则它们的格子永远只有文字、没有图。
        """
        w = getattr(self, "thumb_backfill", None)
        if w is not None and w.isRunning():
            return                      # 已有补扫在跑，别叠加
        pool = list(items) if items is not None else list(self.library.values())
        if not pool:
            return
        self.thumb_backfill = ThumbBackfillWorker(pool)
        self.thumb_backfill.progress.connect(self._on_backfill_progress)
        self.thumb_backfill.done.connect(self._on_backfill_done)
        self.thumb_backfill.start()

    # ---- 素材编码普查（GPU 解码的前提）----
    def _start_codec_probe(self):
        """后台把整个素材库的编码名普查一遍，填进 MediaItem（`_codec` / `_is_dxv`）。

        GPU 解码只接管 DXV，而「是不是 DXV」必须先 `av.open`（几十毫秒）——
        这个判断绝不能落在渲染线程（切素材那一下）。普查一次之后，每次切素材都是零成本。
        """
        import media_manager as mm
        if not mm.gpu_decode_enabled():
            return
        w = getattr(self, "codec_probe", None)
        if w is not None and w.isRunning():
            return
        pool = list(self.library.values())
        if not pool:
            return
        self.codec_probe = mm.CodecProbeWorker(pool)
        self.codec_probe.start()

    # ---- 一次性校正 alpha 判定（修好 av.open GBK bug 的收尾）----
    def _start_alpha_migration(self, tries=0):
        """全库重判一次「素材到底用没用到 alpha」，并把缩略图扩展名改成与结论一致。

        为什么需要：以前 `av.open` 没带 `metadata_errors`，105/164 个 .mov 的探测直接
        抛异常 → 真带 alpha 的素材被判成「无 alpha」，**透明通道一直是丢的**；同时旧判定
        只看 pix_fmt（DXV 一律 rgba），又有 25 个整段不透明的素材被误判成「有 alpha」，
        白白走更贵的 PyAV 路径。探测修好后，磁盘上的旧缩略图还带着旧结论，所以要重判。
        跑一次即可（配置里留标记），之后仍是零成本判定。
        """
        import media_manager as mm
        try:
            if self.cfg.data.get("_alpha_probe2_migrated"):
                return
        except Exception:
            return
        w = getattr(self, "thumb_backfill", None)
        if w is not None and w.isRunning() and tries < 30:
            QTimer.singleShot(3000, lambda: self._start_alpha_migration(tries + 1))
            return
        # **正在演出就先别动**：重判要连续解码一百多个素材（约 34 秒、吃一个核），
        # 绝不能跟现场抢 CPU。每 10 秒看一次，等停下来再做；配置标记只在真正跑完后才写，
        # 所以一直没跑成也没关系 —— 下次启动会继续尝试。
        if getattr(self.engine, "running", False) and tries < 60:
            QTimer.singleShot(10000, lambda: self._start_alpha_migration(tries + 1))
            return
        pool = [it for it in self.library.values() if it.kind == "video"]
        if not pool:
            self._finish_alpha_migration(0, 0)
            return
        self.alpha_mig = mm.AlphaMigrationWorker(pool)
        self.alpha_mig.progress.connect(self._on_backfill_progress)   # 复用同一条进度条
        self.alpha_mig.done.connect(self._finish_alpha_migration)
        self.alpha_mig.start()

    def _finish_alpha_migration(self, n_alpha=0, n_plain=0):
        try:
            self.cfg.data["_alpha_probe2_migrated"] = True
            self.cfg.save()
        except Exception:
            pass
        try:
            self.thumb_bar.hide()
        except Exception:
            pass
        if n_alpha or n_plain:
            print("alpha 校正完成：%d 个改为带 alpha，%d 个改为不带 alpha" % (n_alpha, n_plain))
        self.refresh_grid(icons_only=True)

    def _on_backfill_progress(self, done, total):
        if total <= 0:
            return
        self.thumb_bar.show()
        self.thumb_bar.setRange(0, total)
        self.thumb_bar.setValue(done)

    def _on_backfill_done(self, n):
        self.thumb_bar.hide()
        w = getattr(self, "thumb_backfill", None)
        if w is not None:
            for m in getattr(w, "items", []) or []:
                try:
                    m.drop_icon_cache()
                except Exception:
                    pass
        if n:
            self.refresh_grid(icons_only=True)
            self.statusBar().showMessage(Tf("已补齐 {} 个素材的缩略图", n), 6000)

    def _fill_tag_combo(self):
        """标签集合变化后刷新筛选按钮文案（兼容旧调用点）"""
        self.btn_tagfilter.setText(
            f"标签: 已选 {len(self.tag_selected)}" if self.tag_selected else "标签: 全部")

    def open_tag_filter(self):
        from panels import TagFilterDialog
        dlg = TagFilterDialog(self, self.tag_selected)
        if dlg.exec() == QDialog.Accepted:
            self.tag_selected = dlg.selected
            self._fill_tag_combo()
            self.refresh_grid()

    def refresh_grid(self, icons_only=False):
        sz = self.grid.iconSize()
        if not icons_only:
            self.grid.clear()
            sel = self.tag_selected
            kw = (self.search_box.text() or "").strip().lower()
            for it in self.library.values():
                has_tags = bool(it.all_tags())
                has_roles = any(it.roles.values())
                # 标签筛选：只过滤"有标签但不匹配"的；未打标/排除的（无标签）始终显示
                if sel and has_tags and not (set(it.all_tags()) & sel):
                    continue
                if self.kind_filter == 1 and it.kind not in ("image", "gif"):
                    continue
                if self.kind_filter == 2 and it.kind != "video":
                    continue
                # 角色筛选：只过滤"有角色但不匹配"的；未打标/排除的（无角色）始终显示
                if self.role_filter == 1 and has_roles and not it.roles.get("fg"):
                    continue
                if self.role_filter == 2 and has_roles and not it.roles.get("bg"):
                    continue
                if kw and kw not in it.name.lower() and not any(kw in t.lower() for t in it.all_tags()):
                    continue
                li = QListWidgetItem(it.icon_cached(sz.width(), sz.height()), self._item_text(it))
                li.setData(Qt.UserRole, it)
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
                self.grid.addItem(li)
        else:
            for i in range(self.grid.count()):
                li = self.grid.item(i)
                it = li.data(Qt.UserRole)
                li.setIcon(it.icon_cached(sz.width(), sz.height()))
                li.setText(self._item_text(it))
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
        self.lbl_lib_count.setText(f"{self.grid.count()}/{len(self.library)}")

    def _exists_cached(self, path):
        """文件是否存在（带 30 秒缓存）：素材上万时每次刷新都 stat 一遍会拖慢界面"""
        try:
            c = self._exists_cache.get(path)
        except AttributeError:
            self._exists_cache = {}
            c = None
        now = time.perf_counter()
        if c is not None and now - c[0] < 30.0:
            return c[1]
        ok = os.path.exists(path)
        self._exists_cache[path] = (now, ok)
        if len(self._exists_cache) > 200000:
            self._exists_cache.clear()
        return ok

    def _item_text(self, it):
        if not self._exists_cached(it.path):
            return "文件丢失"
        if getattr(it, "excluded", False):
            return "⊘ " + it.name[:16]
        tags = it.all_tags()
        if tags:
            return "·".join(tags[:3])
        return it.name[:18]

    def _item_tooltip(self, it):
        roles = []
        if it.roles.get("fg"): roles.append("前景")
        if it.roles.get("bg"): roles.append("背景")
        r = "/".join(roles) or "未设置"
        excl = "\n已排除自动打标/匹配（logo 等固定素材）" if getattr(it, "excluded", False) else ""
        miss = "\n⚠ 素材文件已丢失（磁盘上找不到）" if not self._exists_cached(it.path) else ""
        return f"{it.name}\n标签：{'、'.join(it.all_tags()) or '未扫描（点扫描打标）'}\n角色：{r}{excl}{miss}"

    def _tint_role(self, li, it):
        from PySide6.QtGui import QColor
        if not os.path.exists(it.path):
            # 素材文件丢失：红色半透明遮盖 + 白字提示
            li.setBackground(QColor(190, 45, 45, 150))
            li.setForeground(QColor(255, 255, 255))
            return
        if getattr(it, "excluded", False):
            # 排除素材：整体变暗一眼区分。不设前景色（一旦设置，选中浅底上灰字会看不清）
            li.setBackground(QColor(40, 40, 40, 160))
            return
        li.setForeground(QColor(255, 255, 255))   # 素材名白色
        c = None
        if it.roles.get("fg"): c = QColor(30, 80, 120, 90)
        elif it.roles.get("bg"): c = QColor(120, 90, 30, 90)
        li.setBackground(c if c else QColor(0, 0, 0, 0))

    def set_kind_filter(self, i):
        self.kind_filter = i
        self.refresh_grid()

    def set_role_filter(self, i):
        self.role_filter = i
        self.refresh_grid()

    def edit_tags(self, item):
        """分类打勾式标签编辑器（手动 tag，永不覆盖自动 tag）"""
        from panels import TagEditDialog
        TagEditDialog(self, item).exec()

    def toggle_role(self, key, items):
        for m in items:
            # 角色单选：前景就是前景、背景就是背景，二选一（不再同时勾选两个角色）
            m.roles = {"fg": (key == "fg"), "bg": (key == "bg")}
            self.cfg["clip_roles"][m.path] = dict(m.roles)
        self.cfg.save()
        self.refresh_grid()

    def toggle_exclude(self, items):
        """排除/恢复素材：排除后不参与自动打标与匹配（logo 等固定素材用），并清空自动标签与角色"""
        exclude = not all(bool(getattr(m, "excluded", False)) for m in items)
        for m in items:
            m.excluded = exclude
            if exclude:
                m.tags = []
                m.roles = {"fg": False, "bg": False}
                self.cfg["clip_tags"].pop(m.path, None)
                self.cfg["clip_roles"].pop(m.path, None)
                self.cfg["clip_excluded"][m.path] = True
            else:
                self.cfg["clip_excluded"].pop(m.path, None)
        self.cfg.save()
        self.refresh_grid()

    def scan_tags(self, paths=None, force=False):
        if getattr(self, "_tag_scan", None) is not None and self._tag_scan.isRunning():
            QMessageBox.information(self, "扫描打标", "已有扫描在进行中，请稍候。")
            return
        self._tag_scan = TagScanThread(self, paths, force=force)
        self._tag_scan.progress.connect(self._on_scan_progress)
        self._tag_scan.finished.connect(self._on_scan_done)
        self.thumb_bar.setVisible(True)
        self.thumb_bar.setRange(0, 1)
        self.thumb_bar.setValue(0)
        self._tag_scan.start()

    def _on_scan_progress(self, done, total, path):
        self.thumb_bar.setRange(0, max(1, total))
        self.thumb_bar.setValue(done)
        for i in range(self.grid.count()):
            li = self.grid.item(i)
            m = li.data(Qt.UserRole)
            if m and m.path == path:
                li.setText(self._item_text(m))
                li.setToolTip(self._item_tooltip(m))
                self._tint_role(li, m)
                break

    def _on_scan_done(self):
        self.thumb_bar.hide()
        self._fill_tag_combo()  # 新内容标签进下拉
        self.refresh_grid()

    def _on_dyn_scan_done(self):
        self.refresh_grid()

    def show_cache_info(self):
        from media_manager import thumb_cache_size, THUMB_DIR
        QMessageBox.information(
            self, "缩略图缓存",
            f"缓存目录：\n{THUMB_DIR}\n\n占用：{thumb_cache_size():.2f} MB\n素材数：{len(self.library)}\n\n"
            "缩略图首次生成后写入磁盘，之后启动/重建都直接读缓存。")

    def clear_thumb_cache(self):
        from media_manager import clear_thumb_cache as _clear
        n = _clear(self.library)
        self.refresh_grid(icons_only=True)
        # 清完立刻后台重生成，否则整个库都会变成「只有文字没有图」
        self._start_thumb_backfill()
        QMessageBox.information(self, "缩略图缓存",
                                Tf("已清理 {} 个缓存文件，正在后台重新生成缩略图。", n))

    def remove_media(self, item):
        """从素材库移除（同时从所有图层移除）"""
        self.remove_from_all_layers(item)
        self.library.pop(item.path, None)
        self.cfg["library"] = list(self.library.keys())
        self.cfg.save()
        # 自动图层兜底：曲风重算只在曲风变化时触发，清掉 clips 里已删素材的残留
        for i, lay in enumerate(self.engine.layers):
            if not getattr(lay, "auto_mode", False):
                continue
            if any(getattr(c, "path", None) == item.path for c in lay.clips):
                lay.clips = [c for c in lay.clips if getattr(c, "path", None) != item.path]
                lay.auto_clips = list(lay.clips)
                if lay.cur >= len(lay.clips):
                    lay.cur = -1
                    lay.prev_idx = -1
                self.layers_panel.sync_layer(i)
        self.refresh_grid()
        self._fill_tag_combo()

    def confirm_remove_media(self, items):
        """从素材库移除（二次确认；可选同时删除本地文件，仅限「素材库」文件夹内）"""
        if not items:
            return
        box = QMessageBox(self)
        box.setWindowTitle("从素材库移除")
        if len(items) == 1:
            box.setText(f"确定从素材库移除「{items[0].name[:40]}」吗？\n（图层中的该素材会一并移除，标签与摆位也会清除）")
        else:
            box.setText(f"确定从素材库移除 {len(items)} 个素材吗？\n（图层中的这些素材会一并移除，标签与摆位也会清除）")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText("移除")
        cb = QCheckBox("同时删除本地文件（仅限「素材库」文件夹内的文件）")
        box.setCheckBox(cb)
        if box.exec() != QMessageBox.Yes:
            return
        lib_root = os.path.abspath(self.library_dir())
        for m in items:
            p = m.path
            self.remove_media(m)
            if cb.isChecked():
                ap = os.path.abspath(p)
                if ap.startswith(lib_root + os.sep) and os.path.exists(ap):
                    try:
                        os.remove(ap)
                    except OSError as e:
                        QMessageBox.warning(self, "删除失败", f"无法删除文件：\n{ap}\n{e}")

    def open_import_settings(self):
        """打开导入方式设置（即使勾选了「记住我的选择」也可随时修改）"""
        strat = self._ask_import_strategy()
        if strat is None:
            return
        self.cfg["output"]["import_strategy"] = strat
        self.cfg.save()
        QMessageBox.information(
            self, "导入素材设置",
            {"copy": "已设置为：复制到素材库（原文件保留）",
             "move": "已设置为：移动到素材库（复制后删除原文件）",
             "link": "已设置为：仅引用（不复制，保留原路径）"}.get(strat, strat))

    # ================= 图层操作 =================
    def add_to_layer(self, layer_idx, items):
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        if getattr(lay, "auto_mode", False):
            return  # 自动图层素材由曲风匹配自动填充，不接受手动拖入
        for m in items:
            if m and m not in lay.clips:
                lay.clips.append(m)
        if lay.cur < 0 and lay.clips and lay.fixed < 0:
            self.engine._switch_layer_to(lay, 0, instant=True)
        self.save_layers()
        self.layers_panel.sync_layer(layer_idx)

    def play_to_layer(self, layer_idx, items):
        """加入图层并立即切到该素材"""
        self.add_to_layer(layer_idx, items)
        if not (0 <= layer_idx < len(self.engine.layers)) or not items:
            return
        lay = self.engine.layers[layer_idx]
        try:
            idx = lay.clips.index(items[0])
        except ValueError:
            return
        self.engine._switch_layer_to(lay, idx)
        self.layers_panel.refresh_playing()

    def _drop_from_layer(self, layer_idx, item):
        """把素材从指定图层移除（同一素材在别的图层里不受影响）"""
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        if item not in lay.clips:
            return
        was_cur = (0 <= lay.cur < len(lay.clips) and lay.clips[lay.cur] is item)
        lay.clips.remove(item)
        if lay.cur >= len(lay.clips):
            lay.cur = -1
            lay.prev_idx = -1
        # 仅当该素材不再被本层任何播放器使用，才释放解码器
        still_used = (lay.player is not None and lay.player.path == item.path)
        if not still_used and not was_cur:
            pp = lay.cache.pop(item.path, None)
            if pp is not None and pp is not lay.player and pp is not lay.prev_player:
                pp.close()
        self.layers_panel.sync_layer(layer_idx)

    def remove_from_layer(self, layer_idx, item):
        """从指定图层移除（素材格右键 / 素材库菜单带图层时使用）"""
        self._drop_from_layer(layer_idx, item)
        self.save_layers()

    def remove_from_all_layers(self, item):
        """从所有图层移除（素材库右键"从所有图层移除"用）。
        单图层异常不中断其余图层（否则批量删除时会留下残留）"""
        for i in range(len(self.engine.layers)):
            try:
                self._drop_from_layer(i, item)
            except Exception:
                pass
        self.save_layers()

    def remove_item_everywhere(self, item):
        self.remove_from_all_layers(item)

    def clips_dropped_to_layer(self, layer_idx, paths):
        """素材库/资源管理器拖入图层行"""
        items = []
        for p in paths:
            m = self.library.get(p)
            if m is None:
                try:
                    m = MediaItem(p)
                    if not m.kind:
                        continue
                    self.library[p] = m
                except Exception:
                    continue
            items.append(m)
        if not items:
            return
        self.cfg["library"] = list(self.library.keys())
        self.add_to_layer(layer_idx, items)
        self.refresh_grid()
        self._fill_tag_combo()

    def clips_cross_moved(self, from_idx, to_idx, paths):
        """图层间互拖：从 from 移除，加入 to"""
        if not (0 <= from_idx < len(self.engine.layers)) or not (0 <= to_idx < len(self.engine.layers)):
            return
        src = self.engine.layers[from_idx]
        dst = self.engine.layers[to_idx]
        if getattr(src, "auto_mode", False) or getattr(dst, "auto_mode", False):
            return  # 自动图层素材自动管理，不支持手动互拖
        items = []
        for p in paths:
            for m in list(src.clips):
                if m.path == p:
                    src.clips.remove(m)
                    items.append(m)
                    break
        self.layers_panel.sync_layer(from_idx)
        if items:
            self.add_to_layer(to_idx, items)
        else:
            self.save_layers()

    def clips_reorder_request(self, layer_idx, paths, drop_row):
        """行内拖动排序：把 paths 中的素材移动到 drop_row 位置"""
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        moving = [m for m in lay.clips if m.path in paths]
        if not moving:
            return
        before = [m for i, m in enumerate(lay.clips) if i < drop_row and m not in moving]
        rest = [m for m in lay.clips if m not in moving]
        ins = min(len(before), len(rest))
        lay.clips = rest[:ins] + moving + rest[ins:]
        cur_item = lay.clips[lay.cur] if 0 <= lay.cur < len(lay.clips) else None
        if cur_item is not None:
            lay.cur = lay.clips.index(cur_item)
        self.save_layers()
        self.layers_panel.sync_layer(layer_idx)

    def clips_reordered(self, layer_idx, order):
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        if len(order) != len(lay.clips):
            return
        cur_item = lay.clips[lay.cur] if 0 <= lay.cur < len(lay.clips) else None
        lay.clips = list(order)
        if cur_item in lay.clips:
            lay.cur = lay.clips.index(cur_item)
        self.save_layers()

    def on_layer_pick(self, idx):
        self._cur_layer = idx

    def layer_menu(self, idx, gpos):
        """图层右键菜单"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[idx]
        m = QMenu(self)
        a_ren = m.addAction("重命名…")
        a_fix = m.addAction("固定播放本层当前素材")
        a_fix.setCheckable(True)
        a_fix.setChecked(lay.fixed >= 0)
        a_sil = m.addAction("无声音时隐藏本层")
        a_sil.setCheckable(True)
        a_sil.setChecked(bool(lay.hide_silent))
        a_pau = m.addAction("静音时暂停播放")
        a_pau.setCheckable(True)
        a_pau.setChecked(bool(getattr(lay, "pause_silent", False)))
        a_solo = m.addAction("Solo（只显示本层）")
        a_solo.setCheckable(True)
        a_solo.setChecked(bool(getattr(lay, "solo", False)))
        m.addSeparator()
        a_auto = m.addAction("自动匹配模式（按曲风挑素材）")
        a_auto.setCheckable(True)
        a_auto.setChecked(bool(getattr(lay, "auto_mode", False)))
        role_menu = m.addMenu("图层角色")
        r_fg = role_menu.addAction("前景（特效/覆盖上层）")
        r_bg = role_menu.addAction("背景（铺满打底）")
        r_fg.setCheckable(True)
        r_bg.setCheckable(True)
        r_fg.setChecked(lay.role == "fg")
        r_bg.setChecked(lay.role == "bg")
        m.addSeparator()
        sp = m.addMenu("播放速度")
        sp_acts = []
        for name, v in (("0.5x", 0.5), ("1x", 1.0), ("1.5x", 1.5), ("2x", 2.0)):
            a = sp.addAction(name)
            a.setCheckable(True)
            a.setChecked(abs(float(getattr(lay, "speed", 1.0) or 1.0) - v) < 1e-6)
            sp_acts.append((a, v))
        a_lock = m.addAction("锁定画面（不随脉冲/漂移）")
        a_lock.setCheckable(True)
        a_lock.setChecked(bool(getattr(lay, "lock_motion", False)))
        a_alpha = m.addAction("保留素材自带透明通道（alpha）")
        a_alpha.setCheckable(True)
        a_alpha.setChecked(bool(getattr(lay, "use_alpha", True)))
        m.addSeparator()
        a_up = m.addAction("上移（更靠上）")
        a_dn = m.addAction("下移（更靠下）")
        a_copy = m.addAction("复制素材池到新图层")
        a_rst = m.addAction("重置本层设置")
        m.addSeparator()
        a_del = m.addAction("删除图层")
        a_del.setEnabled(len(self.engine.layers) > 1)
        # Kv 主视觉图层：不参与自动匹配/角色，也不允许复制（全局唯一）
        if getattr(lay, "is_kv", False):
            a_auto.setEnabled(False)
            role_menu.setEnabled(False)
            a_copy.setEnabled(False)

        i18n.retranslate(m)     # 临时菜单：弹出前刷语言
        act = m.exec(gpos)
        if act is None:
            return
        if act == a_lock:
            lay.lock_motion = not bool(getattr(lay, "lock_motion", False))
            self.layers_panel.refresh_header(idx)
            self.save_layers()
        elif act == a_alpha:
            lay.use_alpha = not bool(getattr(lay, "use_alpha", True))
            self.layers_panel.refresh_header(idx)
            self.save_layers()
        elif act == a_ren:
            self._cur_layer = idx
            self.layer_rename()
        elif act == a_fix:
            if lay.fixed >= 0:
                lay.fixed = -1
            elif lay.cur >= 0:
                lay.fixed = lay.cur
            self.save_layers()
        elif act == a_sil:
            lay.hide_silent = not lay.hide_silent
            self.save_layers()
        elif act == a_pau:
            lay.pause_silent = not lay.pause_silent
            self.save_layers()
        elif act == a_solo:
            self.set_solo(idx, not bool(lay.solo))
        elif act == a_auto:
            self._set_layer_type(idx, not bool(getattr(lay, "auto_mode", False)))
        elif act == r_fg:
            self._set_layer_role(idx, "fg")
        elif act == r_bg:
            self._set_layer_role(idx, "bg")
        elif act in [a for a, _v in sp_acts]:
            for a, v in sp_acts:
                if a is act:
                    lay.speed = v
            self.layers_panel.refresh_header(idx)
            self.save_layers()
        elif act == a_up:
            self.refresh_layer_row(idx, idx - 1)
        elif act == a_dn:
            self.refresh_layer_row(idx, idx + 1)
        elif act == a_copy:
            self._copy_layer_pool(idx)
        elif act == a_rst:
            lay.blend, lay.opacity, lay.speed = "normal", 1.0, 1.0
            lay.hide_silent, lay.pause_silent, lay.solo, lay.fixed = False, False, False, -1
            lay.img_scale, lay.img_x, lay.img_y = 1.0, 0.0, 0.0
            lay.use_alpha = True
            lay.lock_motion = False
            lay.visible = True
            self.layers_panel.refresh_header(idx)
            self.save_layers()
        elif act == a_del:
            self._cur_layer = idx
            self.layer_del()

    def _set_layer_type(self, idx, auto):
        """切换图层类型（手动/自动匹配）。类型切换会重建图层头（角色按钮显隐）。"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[idx]
        if getattr(lay, "is_kv", False):
            return    # Kv 主视觉图层固定为待机层，不切换类型
        lay.auto_mode = bool(auto)
        if auto:
            lay.role = lay.role or "bg"
            # 立即匹配一次，让自动图层马上有画面
            self.engine._last_genres = None
            self.engine._auto_refresh_t = 0.0
            self.engine._refresh_auto_layers(self.audio.state.snapshot())
        self._sync_layers()

    def _set_layer_role(self, idx, role):
        """切换图层角色（前景/背景），自动图层立即重新匹配"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[idx]
        if getattr(lay, "is_kv", False):
            return    # Kv 主视觉图层无前景/背景角色
        lay.role = role
        if getattr(lay, "auto_mode", False):
            self.engine._last_genres = None
            self.engine._auto_refresh_t = 0.0
            self.engine._refresh_auto_layers(self.audio.state.snapshot())
        self.layers_panel.refresh_header(idx)
        self.save_layers()

    def toggle_layer_role(self, idx):
        """图层头角色按钮点击：前景 ↔ 背景"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[idx]
        self._set_layer_role(idx, "bg" if lay.role == "fg" else "fg")

    def refresh_layer_row(self, i, j):
        ls = self.engine.layers
        if not (0 <= i < len(ls)) or not (0 <= j < len(ls)):
            return
        ls[i], ls[j] = ls[j], ls[i]
        self._cur_layer = j
        self._sync_layers()

    def _copy_layer_pool(self, idx):
        lay = self.engine.layers[idx]
        new = Layer(lay.name + " 副本")
        new.clips = list(lay.clips)
        new.opacity, new.blend = lay.opacity, lay.blend
        new.speed, new.hide_silent = lay.speed, lay.hide_silent
        new.pause_silent = lay.pause_silent
        new.fixed = lay.fixed
        self.engine.layers.insert(idx, new)
        self._sync_layers()

    def clip_menu(self, layer_idx, media, gpos):
        """素材格右键菜单"""
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        is_auto = getattr(lay, "auto_mode", False)
        m = QMenu(self)
        a_play = m.addAction("播放此素材")
        a_play.setEnabled(media is not None and not is_auto)   # 自动图层素材由曲风管理
        a_size = m.addAction("素材大小与位置…")
        a_size.setEnabled(media is not None)
        a_fix = m.addAction("固定为本层素材")
        a_fix.setCheckable(True)
        a_fix.setEnabled(not is_auto)
        a_fix.setChecked(media is not None and lay.fixed >= 0
                        and 0 <= lay.fixed < len(lay.clips) and lay.clips[lay.fixed] is media)
        a_rm = m.addAction("从本层移除")
        a_rm.setEnabled(media is not None and not is_auto)
        mv = m.addMenu("移到其他图层")
        mv_acts = []
        for i, l2 in enumerate(self.engine.layers):
            if i == layer_idx:
                continue
            a = mv.addAction(l2.name)
            mv_acts.append((a, i))
        m.addSeparator()
        a_lay = m.addAction("图层设置…")
        # 菜单是临时构造的短命对象，弹出前刷一次语言（词表机制）
        i18n.retranslate(m)
        act = m.exec(gpos)
        if act is None:
            return
        if act == a_size and media is not None:
            from panels import ImageAdjustDialog
            ImageAdjustDialog(self, media).exec()
        elif act == a_play and media is not None:
            try:
                self.engine._switch_layer_to(lay, lay.clips.index(media))
                self.layers_panel.refresh_playing()
            except ValueError:
                pass
        elif act == a_fix and media is not None:
            try:
                i = lay.clips.index(media)
                lay.fixed = -1 if lay.fixed == i else i
                self.save_layers()
            except ValueError:
                pass
        elif act == a_rm and media is not None:
            self.remove_from_layer(layer_idx, media)
        elif act == a_lay:
            self.layer_menu(layer_idx, gpos)
        else:
            for a, i in mv_acts:
                if a is act and media is not None:
                    if media in lay.clips:
                        lay.clips.remove(media)
                    self.add_to_layer(i, [media])
                    self.layers_panel.sync_layer(layer_idx)
                    break

    def _next_layer_name(self):
        """生成不重复的默认图层名：图层 N（N = 已用编号最大值 + 1）"""
        import re
        used = set()
        for l in self.engine.layers:
            m = re.match(r"^图层\s*(\d+)$", l.name or "")
            if m:
                used.add(int(m.group(1)))
        n = 1
        while n in used:
            n += 1
        return Tf("图层 {}", n)

    def layer_add(self):
        default = self._next_layer_name()
        dlg = NewLayerDialog(self, default)
        if dlg.exec() != QDialog.Accepted:
            return
        name, auto, role, is_kv = dlg.result_data()
        # Kv 主视觉图层：全局唯一，已存在则提醒并中止
        if is_kv and self.engine.kv_layer() is not None:
            QMessageBox.information(
                self, "Kv 主视觉图层已存在",
                "Kv 主视觉图层全局只能有一个。\n请先删除现有的 Kv 图层，再添加新的。")
            return
        lay = Layer((name or ("Kv 主视觉" if is_kv else default)).strip())
        lay.auto_mode = auto
        lay.is_kv = is_kv
        lay.role = role
        if role == "fg" and not is_kv:
            lay.blend = "add"   # 前景（特效/覆盖）默认用"添加"混合，叠加发光才正常
        if is_kv:
            # Kv 图层默认置顶（列表 index 0 = 最上层），不参与自动/逐拍切换
            self.engine.layers.insert(0, lay)
            self.engine._kv_last_t = time.perf_counter()
        else:
            self.engine.layers.append(lay)   # 新建在已有图层下面（最底层）
        if auto:
            # 立即匹配一次（绕过 3s 间隔），让新建自动图层马上有画面
            self.engine._last_genres = None
            self.engine._auto_refresh_t = 0.0
            self.engine._refresh_auto_layers(self.audio.state.snapshot())
        self._cur_layer = 0 if is_kv else (len(self.engine.layers) - 1)
        self._sync_layers()

    def layer_del(self):
        if len(self.engine.layers) <= 1 or not (0 <= self._cur_layer < len(self.engine.layers)):
            return
        lay = self.engine.layers.pop(self._cur_layer)
        lay.close_all()
        self._cur_layer = 0
        self._sync_layers()

    def layer_move(self, d):
        self.refresh_layer_row(self._cur_layer, self._cur_layer + d)

    def layer_rename(self):
        if not (0 <= self._cur_layer < len(self.engine.layers)):
            return
        lay = self.engine.layers[self._cur_layer]
        name, ok = QInputDialog.getText(self, "重命名图层", tr("layer_name"), text=lay.name)
        if ok and name.strip():
            lay.name = name.strip()
            self.layers_panel.refresh_header(self._cur_layer)
            self.save_layers()

    def quick_mode(self):
        while len(self.engine.layers) > 1:
            lay = self.engine.layers.pop()
            lay.close_all()
        self._cur_layer = 0
        self._sync_layers()

    def _sync_layers(self):
        self.engine.push_layers_to_cfg()
        self.cfg.save()
        self.layers_panel.rebuild()
        self._cur_layer = min(self._cur_layer, max(0, len(self.engine.layers) - 1))
        # Kv 设置区随 Kv 图层增删显隐
        try:
            self.settings.sync_kv_section()
        except Exception:
            pass

    def _on_auto_layers_changed(self, idxs):
        """自动图层素材池被重算：刷新对应行素材格 + 播放高亮"""
        for i in (idxs or []):
            if 0 <= i < len(self.layers_panel.rows):
                self.layers_panel.sync_layer(i)
        self.layers_panel.refresh_playing()

    # ================= 输出/语言 =================
    def show_output(self):
        self.out_win.show()
        self.out_win.raise_()
        self.apply_output_screen()

    def open_hotkeys(self):
        HotkeyDialog(self).exec()

    def open_about(self):
        AboutDialog(self).exec()

    def switch_lang(self, i):
        """语言即时切换：不重启，直接重建全部界面文本。"""
        set_lang("zh" if i == 0 else "en")
        self.cfg["lang"] = i18n.lang()
        self.cfg.save()
        self.retranslate_ui()

    def retranslate_ui(self):
        """把界面所有文本刷成当前语言（含面板、输出窗口、折叠分区、动态状态行）。

        机制：i18n.retranslate 遍历控件树，按登记的「中文原文」查词表替换；
        带数值/随状态变化的动态文本（运行按钮、主题按钮、状态行、显示器下拉、
        图层头）无法靠静态词表还原，由 _sync_dynamic_texts 单独重建。
        """
        try:
            i18n.retranslate(self)
        except Exception:
            pass
        try:
            i18n.retranslate(self.out_win)
        except Exception:
            pass
        # 折叠分区标题带 ▼/▶ 展开符号（动态文本），需显式重建
        for sec in getattr(self.settings, "_sections", {}).values():
            try:
                sec.retranslate()
            except Exception:
                pass
        # 色卡按钮（文本/tooltip 含色名，动态）
        try:
            self.settings.retranslate_palette()
        except Exception:
            pass
        for panel in (getattr(self, "output_panel", None),):
            try:
                if panel is not None and hasattr(panel, "retranslate"):
                    panel.retranslate()
            except Exception:
                pass
        self._sync_dynamic_texts()

    def _sync_color_bypass_btn(self):
        v = bool(self.cfg["color"].get("bypass", False))
        self.settings.btn_color_bypass.setText(
            T("已旁路（显示原片）") if v else T("Bypass：原片 / 调色"))
        self.settings.btn_color_bypass.setToolTip(
            T("一键在「调色版」和「原片」之间闪切（热键也可）"))

    def _sync_dynamic_texts(self):
        """语言切换后需要整体重建的动态文本（含运行状态/数值，词表无法覆盖）"""
        running = bool(getattr(self.engine, "running", False))
        self.btn_run.setText(("■ " + tr("stop")) if running else ("▶ " + tr("start")))
        self._sync_theme_btn()
        self._sync_color_bypass_btn()
        self.lbl_status.setText(tr("running") if running
                                else tr("stopped") + T("（自动切换已停，画面实时）"))
        try:
            self.layers_panel.refresh_header()      # 图层名 tooltip / 不透明度 / 角色按钮
        except Exception:
            pass
        try:
            self._update_status()                   # 状态行 / HUD / 能量与颜色提示
        except Exception:
            pass

    def toggle_theme(self):
        name = theme.toggle(QApplication.instance())
        self.cfg["theme"] = name
        self.cfg.save()
        self._sync_theme_btn()

    def _sync_theme_btn(self):
        """主题按钮文本（随主题与语言变化，动态文本，不能靠 i18n 静态记录）"""
        self.btn_theme.setText(T("☀ 亮色") if theme.current() == "dark" else T("🌙 暗色"))
        self.btn_theme.setToolTip(T("切换亮色/暗色主题"))

    # ================= 预览/状态 =================
    def moveEvent(self, e):
        # 拖动窗口期间跳过 60fps 预览重绘（SmoothTransformation 缩放很重，
        # 与移动消息叠加会拖到卡顿）；0.25s 无移动自动恢复
        self._drag_pause_until = time.perf_counter() + 0.25
        super().moveEvent(e)

    def resizeEvent(self, e):
        self._drag_pause_until = time.perf_counter() + 0.25
        super().resizeEvent(e)

    def _on_engine_frame(self, seq=-1):
        """引擎帧到达：**只收到一个序号**，帧去引擎侧取最新的。

        为什么这么改（2026-09-26 内存雪崩）：旧写法每帧把一个 8MB 的 QImage 投进主线程队列，
        主线程一卡就积压（实测 30 秒 +3.2GB，涨到 15GB 把整机拖垮）。
        现在队列里只有 int；重复序号（队列积压时会有很多）直接跳过，不重复绘制。
        """
        if seq == self._drawn_seq:
            return                       # 同一帧已经画过（积压时的去重）
        self._drawn_seq = seq
        img = self.engine.latest_frame()
        if img is None:
            return
        ow = getattr(self, "out_win", None)
        if ow is not None:
            try:
                ow.set_frame(img)
            except Exception:
                pass
        self._update_preview(img)

    def _update_preview(self, img):
        if time.perf_counter() < getattr(self, "_drag_pause_until", 0.0):
            return   # 拖动/缩放窗口中：只存帧不重绘，松手后自然恢复
        self.preview_panel.set_frame(img)
        self._hl_tick += 1
        if self._hl_tick % 6 == 0:      # ~10fps 刷新高亮，省 CPU
            self.layers_panel.refresh_playing()

    def _bpm_text(self, snap, prefix="BPM "):
        """BPM 显示文案。用 bpm_display（未锁定也可有值）而不是 bpm（只有锁定值）。

        `bpm` 只在「众数 ∈ [115,185] 且足够集中」时才 >0，所以慢歌 / 极快曲 /
        被估成半速的曲子会**一直显示 0**（用户 2026-09-24 报的现象）。
        `bpm_display` 由票数≥10 且集中度≥0.40 就发布，未锁定时前面加「≈」区分。
        """
        v = snap.get("bpm_display") or snap.get("bpm") or 0.0
        if not v:
            return prefix + "--"
        if snap.get("bpm_locked"):
            return "%s%.0f" % (prefix, v)
        return "%s≈%.0f" % (prefix, v)

    def _update_status(self):
        snap = self.audio.state.snapshot()
        self.lbl_bpm.setText(self._bpm_text(snap))
        self.lbl_energy.setText(f"{tr('energy')} {snap['energy']:.2f}")
        self.lbl_cpu.setText(f"CPU {cpu_percent():.0f}%")
        self.mini_level.set_level(snap.get("level", 0.0))
        self.btn_audio.setToolTip(T("音源设置\n当前：") + (snap.get("device_desc") or T("未采集")))

        m = self.engine.mode
        tag = T("手动") if self.engine.manual_mode() else T("自动")
        self.settings.lbl_cur_mode.setText(
            Tf("当前节奏: {}（{}）", T(MODE_LABELS.get(m, m)), tag))

        # 颜色渲染状态提示（能量档 + 当前颜色 + 轮换倒计时 / 休眠原因）
        cfr = getattr(self.engine, "color_fx", None)
        if cfr is not None:
            ci = cfr.info
            # 阈值直接取 colorfx 的常量，避免两处各写一套（0.4 以下不渲染）
            from colorfx import TIER_LOW as _CL, TIER_HIGH as _CH
            _e = ci.get("E", 0)
            tier = T("低") if _e < _CL else (T("中") if _e < _CH else T("高"))
            txt = ci.get("state") or ""
            if ci.get("name"):
                txt = Tf("自动换色中（{}，剩余 {} 秒后轮换）",
                         T(ci["name"]), f"{ci['remain']:.0f}")
            else:
                txt = T(txt)
            self.settings.lbl_color_state.setText(Tf("当前能量：{}　{}", tier, txt))

        # HUD：第一行 模式+倒计时+BPM+能量；第二行 歌名+曲风（避免单行过长显示不全）
        # 段落显示已移除（用户反馈：段落判断不准，预览里不再显示）
        hud = f"{T(MODE_LABELS.get(m, m))}"
        remain_n = self.engine.switch_countdown(snap)
        if remain_n is not None:
            # 倒计时从「间隔-1」开始数（刚切完显示 15 / 快切 7），数到 0 就是下一个切换点；
            # 实际间隔仍是完整 16 / 8 拍并踩小节线（计算在 engine.switch_countdown 里）。
            hud += Tf("  |  还有 {} 拍切换", f"{remain_n}")
        hud += "  |  " + self._bpm_text(snap) + f"  {T('能量')} {snap['energy']:.2f}"
        sid_now = snap.get("recognized_song_id", -1)
        song_meta = self._song_meta_by_id.get(sid_now)
        song_line = ""
        if song_meta:
            title = song_meta.get("title") or ""
            genres = song_meta.get("genres") or []
            song_line = f"🎵 {title}"
            if genres:
                song_line += f"  {'·'.join(genres[:3])}"
        else:
            genre = getattr(self.audio.state, "genre_tags", None) or []
            if genre:
                song_line = f"🎵 {'·'.join(genre[:3])}"
        if song_line:
            hud += "\n" + song_line
        self.preview_panel.update_hud(hud)

        # 每图层的"下一个素材"缩略图（多图层多个；无素材/隐藏的图层不显示）
        self.preview_panel.update_layer_thumbs(self.engine.layers)

        if snap["error"]:
            self.lbl_warn.setText(snap["error"])
        elif self.engine.running and snap["silent_sec"] > 5:
            self.lbl_warn.setText(tr("no_sound_hint"))
        else:
            self.lbl_warn.setText("")

    def closeEvent(self, ev):
        self.save_layers()
        # 缩略图补扫线程要主动中断，否则退出时它还在解码大素材、拖慢关闭
        for attr in ("thumb_worker", "thumb_backfill"):
            w = getattr(self, attr, None)
            if w is not None and w.isRunning():
                w.requestInterruption()
                w.wait(1500)
        # 先停引擎线程，再关音频，避免退出时 worker 仍在合成
        from PySide6.QtCore import QMetaObject, Qt as _Qt, QThread as _QThread
        QMetaObject.invokeMethod(self.engine, "stop", _Qt.QueuedConnection)
        if hasattr(self, "engine_thread"):
            self.engine_thread.quit()
            self.engine_thread.wait(2000)
        self.engine.close()
        self.audio.shutdown()
