# -*- coding: utf-8 -*-
"""主窗口 UI（Arena 风格布局）

★ 2026-09-28 起主区拆成**两界面**（`QStackedWidget`，共用同一个引擎）：
  ① 演出台     —— 图层 + 预览 + 设置 + 输出（现场用）
  ② 素材·音乐曲库 —— 素材库独占整幅宽 + 音乐曲库入口（演出前准备）

布局：
  顶部工具条：返回/开始/黑场/冻结/暂停自动/下一素材/音源(含迷你电平)/语言/快捷键
  ┌──────────────┬──────────────────────────────┬──────────────┐
  │ 图层头(左列)   │ 素材格(一行一个图层)            │ 设置面板       │
  ├──────────────┴────────┬─────────────────────┤ (折叠分区)     │
  │ 输出预览+HUD+下一素材预看 │ 素材库(网格+右键+拖拽)  │              │
  └───────────────────────┴─────────────────────┴──────────────┘
  首页：三步（选音源→导素材→开始）
"""
import hashlib
import os
import shutil
import time
import math
import ctypes

from PySide6.QtCore import (Qt, QTimer, QSize, QThread, Signal, QPoint, QObject,
                            QEvent, QAbstractNativeEventFilter)
from PySide6.QtGui import (QImage, QPixmap, QAction, QKeySequence, QIcon,
                          QGuiApplication, QCursor)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout,
    QComboBox, QPushButton, QSlider, QCheckBox, QSpinBox, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QSplitter, QStackedWidget,
    QGroupBox, QStatusBar, QDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QApplication, QProgressBar, QAbstractItemView, QInputDialog,
    QToolBox, QScrollArea, QMenu, QFrame, QSizePolicy, QLineEdit, QButtonGroup,
    QRadioButton, QTabWidget, QAbstractSpinBox, QToolButton, QDoubleSpinBox,
)

import i18n
from i18n import tr, set_lang, T, Tf
import theme
from config import (Config, CONFIG_FILE, exe_dir,
                    DEFAULT_SPOUT_NAME, DEFAULT_NDI_NAME)
import dups as D            # 曲库查重（纯函数，见 dups.py）
from dup_ui import DuplicateDialog
from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER,
                      DYNAMIC_TAGS, COVERAGE_FULL, COVERAGE_PART, COVERAGE_TAGS,
                      COVERAGE_FULL_THRESHOLD)
from audio_engine import AudioEngine
from media_manager import MediaItem, ThumbWorker, ThumbBackfillWorker, scan_folder
from engine import (AutoVJEngine, Layer, MODE_LABELS, BLEND_MODES, BLEND_LABELS,
                    SWITCH_MANUAL)
from output_window import OutputWindow
from hotkeys import HotkeyManager
from panels import (LayerStackPanel, PreviewPanel, AudioSourceDialog, MiniLevel,
                    LibraryGrid, CollapsibleSection, CLIP_MIME, LIB_THUMB_SIZES,
                    NewLayerDialog, I18nDialog, MusicLibraryPanel)

MEDIA_FILTER = "媒体素材 (*.mp4 *.mov *.webm *.avi *.mkv *.gif *.png *.jpg *.jpeg *.bmp *.webp);;All Files (*)"
MUSIC_EXTS = {".flac", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".aac", ".ogg", ".opus", ".wma"}
MUSIC_FILTER = "音频文件 (*.flac *.mp3 *.m4a *.wav *.aiff *.aif *.aac *.ogg *.opus *.wma);;All Files (*)"

# 素材库两个筛选下拉的悬浮说明（「已排除 / 未扫描」是扫描状态，不是动态档，容易误会）
_FILTER_TIP = (
    "「已排除」= 右键设过「排除自动匹配」的素材；\n"
    "「未扫描」= 还没有任何标签（没跑过「扫描素材」）的素材。")

# 「清除分析数据」的确认文案 —— **提成模块级常量**，因为它是 i18n 的 key：
#   中文原文即 key，与 `i18n_map.ZH2EN` 必须**逐字一致**，否则英文界面直接显示中文。
#   以前这条文案内联在 `ask_clear_analysis` 里，改文案时忘了同步 i18n（实测发生过）。
#   `tools/_test_clear_keep_external.py` 的 (f) 就是拿这个常量去查 ZH2EN。
CLEAR_ANALYSIS_TIP = (
    "将清除**本机曲库的自动分析结果**：指纹、BPM、节拍网格、自动段落。\n"
    "以下内容一律保留：\n"
    "  · 音乐文件与曲库列表\n"
    "  · 手工标注（手改的 BPM / 拍锚点 / 段落）\n"
    "  · **码表**：码表本身与它内嵌曲目的指纹（照常认歌）\n"
    "  · **导入的曲库分析包**：节拍网格、封面、指纹"
)

# ★★★ 2026-11-06：「后处理 · 手动」每行右边那个齿轮按钮的样式（**必须自带**）。
#   全局 QSS 的 `QPushButton{padding:4px 10px; min-height:26px}` 会把 30×20 的按钮
#   内容区挤成 10×36 ⇒ 齿轮被压成一条竖缝（用户：「logo 图标没法正常显示」）。
#   ⚠ 抽成模块常量是为了让 `tools/_test_pp_gear_btn.py` 用**同一份**样式去验证，
#     而不是测试里再抄一遍（抄一遍的测试等于没测）。
#   ⚠ min/max-height 是**内容高**：20 的话连边框一共 22，会把表格行顶高 2px；
#     设 18 ⇒ 成品正好 30×20，和同一行的复选框齐平。
PP_GEAR_STYLE = ("QPushButton{padding:0;min-height:18px;max-height:18px;"
                 "font-size:13px;border-radius:3px;}")

# 后处理效果的配置键 → 界面中文名（配置里存的是英文 key，状态行要显示中文）
POSTFX_CN = {
    "chromatic": "色差", "bloom": "辉光", "glitch": "故障位移", "exposure": "曝光脉冲",
    "saturation": "饱和度脉冲", "softfocus": "柔焦", "trails": "拖影", "grain": "颗粒",
    "pixelate": "像素化", "spectrum": "径向频谱", "deform": "音频反应变形",
}


def _local_song_id(path):
    """本机曲目在指纹库里的 `song_id`：`int(md5(路径)[:8], 16)`。

    与建库处（`MusicScanThread` 的 `song_id = int(hashlib.md5(p.encode()).hexdigest()[:8], 16)`）
    **逐字一致** —— 口径必须只有一份，否则"该保留的"会算成别的 id（那就是漏删的根源）。
    取不到 / 路径为空 ⇒ 返回 `None`（调用方跳过，绝不抛）。
    """
    try:
        s = str(path or "")
        if not s:
            return None
        return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)
    except (TypeError, ValueError, AttributeError):
        return None


def postfx_status_text(pi):
    """把引擎的 `_postfx_info` 变成状态行文案（纯函数，便于自测）。

    设计目的：用户报"后处理看不出效果"时，界面必须能直接回答
    ——是关着？被码表/Kv 接管？还是强度只有 3%？
    """
    pi = pi or {}
    st_txt = pi.get("state") or ""
    fx = pi.get("fx") or {}
    if st_txt:
        return Tf("后处理：{}", T(st_txt))
    if not fx:
        return T("后处理：本帧无生效效果")
    names = sorted(fx.items(), key=lambda kv: -kv[1])
    body = " · ".join("%s %d%%" % (T(POSTFX_CN.get(k, k)), round(v * 100))
                      for k, v in names if v > 0.004)
    if not body:
        body = T("强度为 0（能量过低或全局强度为 0）")
    mode_txt = T("自动") if pi.get("mode") == "auto" else T("手动")
    return Tf("后处理（{}）：{}", mode_txt, body)


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
        # ★ 2026-10-25：加回「手动」——选它之后**完全不做自动切换**，
        #   只有你按「手动切换素材 / 下一素材 / 播放此素材」才换画面。
        #   Kv 待机层、码表、自愈看门狗都不受影响（它们本来就不走"自动换素材"）。
        self.mode_combo.addItems(["自动（推荐）", "常规切", "快切", "逐拍交替",
                                  "手动（不自动切换）"])
        # ★★★ 2026-11-06（用户：「演出行为这边的自动的悬浮的说明要改」）：
        #   旧文案还写着"自动：按能量/瞬态自己挑常规切/快切"——那已经是**没有段落表时**的
        #   兜底逻辑了。现在有段落表就跟着段落切，说明必须讲清"到底谁在决定什么时候换"。
        self.mode_combo.setToolTip(
            "自动（推荐）——让软件自己决定什么时候换素材：\n"
            "· 这首歌在曲库里标过「能量段落」⇒ 跟着段落边界切：进 drop 那一拍就换，\n"
            "  并提前换好符合该段动态（低/中/高）的素材池，不再等 8/16 拍排期；\n"
            "· 没有段落表 ⇒ 按能量在「常规切（16 拍）/ 快切（8 拍）」之间自动选；\n"
            "· 一个段落再长，最多 64 拍也会换一次，不会长时间不动；\n"
            "· 段落边界只切一次（同一次边界不会连着切两个素材）。\n\n"
            "常规切 / 快切 / 逐拍交替：固定用这一种节奏（有段落表时，段落边界仍会先切一次）。\n"
            "手动（不自动切换）：永远不自动换素材，只在素材右键「手动切换素材」、"
            "「播放此素材」或按「下一素材」快捷键时换。Kv 待机层与码表不受影响。")
        gm.addWidget(self.mode_combo, 0, 1)
        self.lbl_cur_mode = QLabel("当前节奏: -")
        self.lbl_cur_mode.setStyleSheet("color:#7ab;")
        self.lbl_cur_mode.setWordWrap(True)
        gm.addWidget(self.lbl_cur_mode, 1, 0, 1, 2)
        gm.addWidget(QLabel("灵敏度"), 2, 0)
        self.sens = QComboBox()
        self.sens.addItems(["低", "中", "高"])
        gm.addWidget(self.sens, 2, 1)
        gm.setRowStretch(6, 1)
        # 页面已建好，注册在下面统一做（见「设置面板重组」）

        # ---- 效果 ----
        p2 = QWidget()
        ga = QGridLayout(p2)
        # ★ 2026-09-27：「渲染帧率 / GPU 解码」原来混在「效果」里，其实是**性能**项 —— 单独成页。
        p_perf = QWidget()
        gp = QGridLayout(p_perf)
        ga.addWidget(QLabel("画面振幅强度"), 0, 0)
        self.intensity = QComboBox()
        self.intensity.addItems([tr("intensity_off"), tr("intensity_low"),
                                 tr("intensity_mid"), tr("intensity_high"),
                                 tr("intensity_xhigh")])
        self.intensity.setToolTip(T("关闭：画面不随低频能量脉冲缩放（其余效果不受影响）"))
        ga.addWidget(self.intensity, 0, 1)
        ga.addWidget(QLabel("过渡方式"), 1, 0)
        self.transition = QComboBox()
        self.transition.addItems(["跟随模式", "淡入淡出", "硬切", "滑动", "缩放", "故障"])
        self.transition.setToolTip(T("跟随模式：常规=淡入淡出，逐拍=硬切"))
        ga.addWidget(self.transition, 1, 1)
        self.energy_map = QCheckBox("能量映射")
        self.bpm_sync = QCheckBox("BPM Sync 视频速度")
        ga.addWidget(self.energy_map, 2, 0, 1, 2)
        ga.addWidget(self.bpm_sync, 3, 0, 1, 2)
        self.chk_beat_grid = QCheckBox("节拍网格（八拍乐句对齐）")
        self.chk_beat_grid.setToolTip("认出曲库里的歌时，用离线算好的八拍网格驱动拍位：素材切换踩在八拍乐句头，变速播放也能对齐。关掉则完全回到原来的实时拍钟。")
        gm.addWidget(self.chk_beat_grid, 3, 0, 1, 2)
        # ★ 2026-10-30 用户：「去掉这个选项」（截图指向「八拍相位翻转」）——
        #   手动翻转是给"离线乐句相位判反"打的补丁，用户不想再靠它；
        #   相位问题改为**在日志里量出来**（见 engine `_auto_switch_tick` 的「拍位切换」行：
        #   栅格=8 且残差≈0 ⇒ 对齐正常；栅格=4 ⇒ 根本没进网格模式）。
        #   ⚠ 已存的 `grid_flip` 仍然被引擎读取（老配置不会突然变调），只是不能再手动改。
        gp.addWidget(QLabel("渲染帧率"), 0, 0)
        self.render_fps = QComboBox()
        self.render_fps.addItems(["60", "30", "20"])
        self.render_fps.setToolTip("引擎每秒合成多少次画面；素材多为 25 到 30fps，选 60 时约有一半是把同一帧重复合成 —— 机器吃紧（同时开 VDJ、直播、录制）就选 30，能省约一半渲染 CPU，画面仍跟得上素材")
        gp.addWidget(self.render_fps, 0, 1)
        self.chk_gpu = QCheckBox("GPU 解码（DXV 硬件解压）")
        self.chk_gpu.setToolTip("让显卡硬件解压 DXV 素材（帧内 DXT 压缩块直传显存），降低解码 CPU 占用：DXT5 素材约省一半以上、超宽素材约省七成；解压失败或显卡不支持时自动回退软解，不影响播放")
        gp.addWidget(self.chk_gpu, 1, 0, 1, 2)
        # ★ 2026-11-09 用户要求：去掉「安全视频解码」开关（普通视频固定走 OpenCV，省 CPU）。
        #   原来这一行是 chk_safe_decode（2, 0），删掉后 chk_fps_follow 上移到第 2 行。
        self.chk_fps_follow = QCheckBox("跟随素材帧率（省 CPU，推荐）")
        self.chk_fps_follow.setToolTip("素材 25~30fps 时不再按 60fps 重复合成同一帧；有 60fps 素材时自动回到 60。手动选 60 但此项开启时，仍会按素材帧率封顶。")
        gp.addWidget(self.chk_fps_follow, 2, 0, 1, 2)
        ga.setRowStretch(4, 1)
        gp.setRowStretch(2, 1)
        # 同上：注册统一在下面做

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
        # 同上

        # ---- 后处理 ----
        p4 = QWidget()
        pf = QGridLayout(p4)
        pf.setVerticalSpacing(3)
        self.chk_postfx_on = QCheckBox("启用后处理")
        pf.addWidget(self.chk_postfx_on, 0, 0)
        # ★ 2026-11-06（用户：「把模式那两个字挪到手动和自动开关的下拉选项旁边，
        #   不要放居中了」）：原来是 `QLabel("模式")` 单独占第 1 列、下拉占第 2 列，
        #   三列均分宽度 ⇒ 「模式」两个字落在列中间，看起来和下拉框离得很远。
        #   改成一个横向子布局紧跟在下拉框左边，并跨到最后一列。
        self.postfx_mode = QComboBox()
        self.postfx_mode.addItems(["自动（推荐）", "手动"])
        self.postfx_mode.setToolTip("自动：根据能量自动挑选 2~3 个合适的效果并随高潮增强\n手动：自己逐个开效果、调强度")
        _mode_row = QHBoxLayout()
        _mode_row.setContentsMargins(0, 0, 0, 0)
        _mode_row.setSpacing(6)
        _mode_row.addWidget(QLabel("模式"))
        _mode_row.addWidget(self.postfx_mode)
        _mode_row.addStretch(1)
        pf.addLayout(_mode_row, 0, 1, 1, 2)

        # --- 自动模式页 ---
        self.postfx_auto_page = QWidget()
        pa = QGridLayout(self.postfx_auto_page)
        pa.setContentsMargins(0, 0, 0, 0)
        pa.addWidget(QLabel("全局强度"), 0, 0)
        self.postfx_global = QSlider(Qt.Horizontal)
        self.postfx_global.setRange(0, 100)
        self.postfx_global.setToolTip("整体效果强度：自动挑出的效果都按这个强度为上限")
        pa.addWidget(self.postfx_global, 0, 1)
        self.postfx_auto = QCheckBox("自动联动（随音乐触发）")
        self.postfx_auto.setToolTip("关掉则效果只随能量变化，不跟事件")
        pa.addWidget(self.postfx_auto, 1, 0, 1, 2)
        pa.addWidget(QLabel("效果保持"), 2, 0)
        self.postfx_hold = QComboBox()
        self.postfx_hold.addItems(["实时（跟随能量）", "8 拍", "16 拍", "32 拍", "64 拍"])
        self.postfx_hold.setToolTip(
            "自动模式下，选好的一组效果保持多少拍之后再重新挑下一组。\n"
            "「实时」= 跟着能量即时变化（能量在门槛附近抖动时，效果会忽有忽无）；\n"
            "设成 8~64 拍则每组效果稳定停留一段时间，变化更有节奏感。")
        pa.addWidget(self.postfx_hold, 2, 1)
        hint = QLabel("自动模式按能量挑效果；强度 = 全局强度（上限六成）× 能量。\n"
                      "能量 0.15 以下休眠、0.70 以上满额 —— 缓拍几乎无效果，高潮最明显。")
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
            # ★★★ 2026-11-06（用户：「后处理手动里面的 logo 图标没法正常显示」）：
            #   原来是 `QPushButton("⚙")` + `setFixedSize(26,20)`。但 `theme.py` 的全局 QSS
            #   写着 `QPushButton{padding:4px 10px; min-height:26px}` —— 26 宽减掉左右各 10px
            #   只剩 **6px** 内容区（实测 contentsRect 26×36，比控件 20 还高）⇒ 齿轮被压成
            #   一条竖缝，看起来就是"图标坏了"。
            #   ⇒ 这个按钮**必须自带 padding:0 + min/max-height**（全局 min-height 会盖过
            #     setFixedSize，见铁律「QSS min-height 盖过 setFixedSize」）。
            #   只覆盖排版属性，颜色/边框仍旧走全局主题（切主题不会留在旧配色）。
            gear = QPushButton("⚙")
            gear.setObjectName("btnPpGear")
            gear.setFixedSize(30, 20)
            gear.setStyleSheet(PP_GEAR_STYLE)
            gear.setToolTip("触发方式（常驻/事件）、音频驱动等详细设置")
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
        # 实时强度回显：直接回答"到底有没有生效、现在是多强"
        self.lbl_postfx_state = QLabel("")
        self.lbl_postfx_state.setWordWrap(True)
        self.lbl_postfx_state.setStyleSheet("color:#7ab;font-size:11px;")
        pf.addWidget(self.lbl_postfx_state, 3, 0, 1, 3)
        pf.setRowStretch(4, 1)
        # 同上

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
        # 同上

        # ---- 能量校正：**整个标签页已删除**（2026-10-18 用户要求）----
        #   界面不再提供入口；底层 energy_scale（伽马）照旧保留在
        #   audio_engine.energy_scale，默认 1.0 = 不校正，需要时改 config.json 的
        #   auto.energy_scale 即可（启动时由 MainWindow.__init__ 读入）。
        # ================= 设置面板重组（2026-09-27）=================

        # 7 个折叠分区 → **4 个**，按"用途"归组、顺序按使用流程：
        #   · 「渲染帧率 / GPU 解码」原本混在「效果」里 —— 它们是**性能**项，独立成「性能」；
        #   · 「节拍网格 / 相位翻转」原本是效果的一部分 —— 它决定**切换怎么跟音乐走**，归「演出行为」；
        #   · 「逐拍交替参数」是行为模式的子模式 ⇒ 并入「演出行为」；
        #   · 「颜色渲染 / 后处理 / 能量校正(原「实验性」)」都是画面处理 ⇒ 并入「画面效果」；
        #   · ⚠「输出」**刻意不并进来**（用户明确要求：输出很重要）—— 它保持右下角**独立常显**面板，
        #     不塞进折叠区（否则要多滚动 + 展开一次，反而更难找）。
        # 控件对象与信号连线一律没动，只是换了所在的布局 ⇒ 行为与原来一致。
        def _wrap(*pages):
            """把多个页面拼成一个折叠分区的内容。"""
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(6)
            for pg in pages:
                v.addWidget(pg)
            v.addStretch(1)
            return w
        # ⚠ 用户指出（2026-09-27）：这几项（素材选取 / 交替间隔 / 每对小节数 / 末尾行为）
        #   **只在「逐拍交替」模式下才生效** —— 常显会让人以为当前正起作用。
        #   原来它们是**独立折叠区**、由 `sync_beat_section()` 按模式隐藏；重组并进「演出行为」后，
        #   这里改成**可整段隐藏的子页**（连同上方的细分隔线），仍由 `sync_beat_section()` 控制。
        self._beat_box = QWidget()
        _bv = QVBoxLayout(self._beat_box)
        _bv.setContentsMargins(0, 0, 0, 0)
        _bv.setSpacing(4)
        _ln = QFrame()
        _ln.setFrameShape(QFrame.HLine)
        _ln.setFrameShadow(QFrame.Plain)
        _bv.addWidget(_ln)
        _bv.addWidget(p3)
        self._add_section(box, "演出行为", _wrap(p1, self._beat_box))
        # ★ 2026-09-27 用户反馈：四组画面相关设置**堆在同一个折叠区里反而更乱**（一屏 20+ 项），
        #   改成**子标签页**：基础（振幅/过渡/能量映射/BPM Sync）/ 颜色渲染 / 后处理。
        #   （原第 4 页「能量校正」已于 2026-10-18 整页删除。）
        #   ⚠ 页面对象（p2/pc/p4）**原样复用**，只是换了个容器 ⇒ 控件对象与信号连线一条都没动。
        self._fx_tabs = QTabWidget()
        self._fx_tabs.setDocumentMode(True)      # 扁平化，不再是「一坨嵌入式面板」
        # ★ 用户反馈（2026-09-27）：标签上方多出一条横线 ⇒
        #   ① `setDrawBase(False)` 去掉标签栏那条底线/基线；
        #   ② 去掉 pane 边框（documentMode 下本来就不该有外框，否则在标签上下各画一道）。
        #   只作用于这一个 QTabWidget，不影响素材库那几个标签页。
        try:
            self._fx_tabs.tabBar().setDrawBase(False)
        except Exception:
            pass
        self._fx_tabs.setStyleSheet("QTabWidget::pane{border:none;}")
        self._fx_tabs.addTab(p2, "基础")
        self._fx_tabs.addTab(pc, "颜色渲染")
        self._fx_tabs.addTab(p4, "后处理")
        self._add_section(box, "画面效果", self._fx_tabs)
        self._add_section(box, "性能", p_perf)

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
        self.chk_fps_follow.stateChanged.connect(
            lambda s: main.engine.set_fps_follow_source(bool(s)))
        self.chk_gpu.stateChanged.connect(main.set_gpu_decode)
        self.transition.currentIndexChanged.connect(main.set_transition)
        self.energy_map.stateChanged.connect(lambda s: main.engine_cfg("auto", "energy_map", bool(s)))
        self.bpm_sync.stateChanged.connect(lambda s: main.engine_cfg("auto", "bpm_speed_sync", bool(s)))
        self.chk_beat_grid.stateChanged.connect(lambda s: main.engine_cfg("auto", "beat_grid", bool(s)))
        # 能量校正（gamma）已无 UI 入口，只由 config.json 的 auto.energy_scale 驱动。
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

    # 能量校正（gamma）：原「实验性」滑条已从界面移除（2026-10-18）；
    # 底层仍由 MainWindow.set_energy_scale / config 的 auto.energy_scale 生效。

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
        self.btn_color_adv.setText(T("高级设置 ▾") if on else T("高级设置 ▸"))

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
        self.btn_noise_floor.setText(T("检测中…"))
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
        self.btn_noise_floor.setText( T("自动检测底噪"))
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
        """逐拍交替参数仅在手动选择「逐拍交替」模式时显示（删掉慢切后索引为 3）。

        ★ 2026-09-27：设置分区重组后它**不再是独立折叠区**，而是「演出行为」区里的一段
          （用户指出：这几项只在逐拍模式下有用，常显会让人以为当前生效）。
          所以这里改为隐藏/显示**那一段页面**（连同上方的分隔线），语义与原来完全一致；
          兼容旧结构：万一还有同名折叠区，也一并按模式显隐。
        """
        show = self.mode_combo.currentIndex() == 3
        sec = self._sections.get("逐拍交替参数")
        if sec is not None:
            sec.setVisible(show)
        box = getattr(self, "_beat_box", None)
        if box is not None:
            box.setVisible(show)

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

    # ★ 2026-10-30：`_flip_grid_phrase`（八拍相位翻转）随按钮一起去掉。
    #   手动翻转治的是"离线乐句相位判反"，但用户不想再靠它；现在靠日志量（见
    #   engine `_auto_switch_tick` 的「拍位切换」行）。引擎侧 `flip_phrase_now`
    #   保留（老配置里的 `grid_flip` 仍然生效），只是 GUI 不再提供入口。

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
        self.lbl_resolution = QLabel(tr("resolution"))
        go.addWidget(self.lbl_resolution, 0, 0)
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
        self.chk_aspect.setToolTip(
            "勾选：按画面比例缩放，不够的地方留黑边（不拉伸、不变形）；不勾选：拉伸铺满输出尺寸（填满，但比例不对时会变形）。")
        self.chk_border = QCheckBox(tr("borderless"))
        self.chk_top = QCheckBox(tr("always_top"))
        go.addWidget(self.chk_aspect, 2, 0)
        go.addWidget(self.chk_border, 2, 1)
        go.addWidget(self.chk_top, 3, 0)
        self.btn_reset_win = QPushButton("重置窗口大小")
        # ⚠ 文案必须与 output_window.apply_window_size 的实际行为一致：它是**严格按输出
        #   分辨率**重置窗口大小（不再做"缩到屏幕 80%"那种改动）。
        self.btn_reset_win.setToolTip(T("退出全屏并按输出分辨率重置窗口大小"))
        self.btn_reset_win.clicked.connect(main.reset_window_size)
        go.addWidget(self.btn_reset_win, 3, 1)
        go.addWidget(QLabel("输出显示器"), 4, 0)
        self.lbl_output_screen = go.itemAtPosition(4, 0).widget()
        self.screen_combo = QComboBox()
        self._screens = QGuiApplication.screens()
        # ★ S2 #14（用户第 14 条）：第 0 项 = **关闭**（默认，不弹输出窗口）。
        #   索引与配置值的换算统一为 `cfg = index - 2`
        #   （0→-2 关闭 / 1→-1 窗口模式 / 2→0 显示器1 / …）。
        items = [T("关闭（不弹出输出窗口）"), T("窗口模式（可拖动）")]
        for i, sc in enumerate(self._screens):
            tag = "（主）" if sc == QGuiApplication.primaryScreen() else ""
            items.append(f"显示器 {i + 1} 全屏{tag}  {sc.geometry().width()}x{sc.geometry().height()}")
        self.screen_combo.addItems(items)
        self.screen_combo.setToolTip(
            "关闭 = 不弹出输出窗口（默认；开始 VJ 时仍会自动显示，可在这里关掉）\n"
            "窗口模式 = 可拖动的独立窗口；显示器 N = 全屏到该显示器（适合双屏演出）")
        go.addWidget(self.screen_combo, 4, 1)
        # ---- Spout / NDI 输出 ----
        # ★★ 2026-10-30（用户：「两个输出名字改成固定的 分别加本地和局域网的括号说明」）：
        #   默认名从同一个 `EasyRealityAutoVJ` 拆成「(本地)」「(局域网)」—— 本质差异就一条：
        #   Spout 只能被**同一台电脑**上的程序收，NDI 走**局域网**。
        # ★★ 2026-10-30（用户：「两个输出名字改成固定的 分别加本地和局域网的括号说明」
        #   紧接着纠正：「不是 是加到前面的选项上」）：括号说明加在**复选框标签**上。
        self.chk_spout = QCheckBox(T("Spout 输出（本地）"))
        self.chk_spout.setToolTip(T("**本地**输出：把画面实时共享给同一台电脑上的程序"
                                    "（OBS 装 Spout 插件后选本 Sender；局域网里别的机器看不到）"))
        go.addWidget(self.chk_spout, 5, 0)
        # ★★ 2026-10-30（用户：「看不见了 不要输入框 把名字写死」）：
        #   输入框被右边的「本机 xxxx」挤成 `oVJ`（截图可见），而且这两个名字本来
        #   就不该让用户改 ⇒ 换回**只读文字**（可选中复制）。
        self.lbl_spout_name = QLabel(DEFAULT_SPOUT_NAME)
        self.lbl_spout_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_spout_name.setToolTip(
            T("固定名字。OBS 用 Spout2 Capture 插件时选它。"))
        go.addWidget(self.lbl_spout_name, 5, 1)
        self.chk_ndi = QCheckBox(T("NDI 输出（局域网）"))
        self.chk_ndi.setToolTip(T("**局域网**输出：通过网线/WiFi 把画面+声音发给别的电脑、手机"
                                  "（本机也能收）。运行时已随软件自带，接收端需装 NDI Runtime"))
        # ★★ 2026-10-30（用户：「写死了就能写完整名字了吧」）：名字写死之后就不用担心
        #   用户改坏 ⇒ **这里直接显示完整接收名**（`本机名 (源名)`），
        #   OBS 里看到的是什么，界面就显示什么。
        #   ⚠ 但真正交给 NDI 的 `cfg["output"]["ndi_name"]` 仍然是**裸源名**：
        #     机器名由 NDI 协议层自动加，写进源名会变成 `HOST (HOST (名))`（已实测）。
        self.lbl_ndi_name = QLabel("")
        self.lbl_ndi_name.setWordWrap(True)
        # ⚠ 垂直 policy 必须是 `Maximum`：`QGridLayout` 会把多余高度平均分给各行，
        #   默认 `Preferred` 允许增长 ⇒ 名字那一格被拉到 500+ px（文本飘在中间，
        #   实测 760px 高的面板里 label 高 526px）。`Maximum` 表示"sizeHint 就是上限"，
        #   行变高时它只按自身文字高度占位。
        self.lbl_ndi_name.setSizePolicy(QSizePolicy.Policy.Preferred,
                                       QSizePolicy.Policy.Maximum)
        self.lbl_ndi_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_ndi_name.setToolTip(
            T("OBS 的 NDI 源列表里就是这个名字。\n"
              "前面的本机名由 NDI 自动加，源名本身不含它（否则会重复两遍）。"))
        # ★★ 2026-10-30（用户：「和 ndi 开关一起 一行放得下就放 放不下就另起一行
        #   因为全屏了的话那肯定是放得下的嘛」）：
        #   ⇒ 复选框与完整名放进**同一个 QGridLayout**（一个格子一个），
        #     再由 `_flow_ndi()` 在窗口尺寸变化时二选一：
        #       · 放得下 → 复选框 (0,0) + 名字 (0,1) 并排一行；
        #       · 放不下 → 名字整体挪到 (1,0) 跨两列，**另起一行**（不是硬挤成两行）。
        #   为什么不用 QHBoxLayout：那样名字只能"折行"，窄窗口下会变成贴在右边
        #   折成两三行的碎块（实测 360px 宽时 x=148、高 32px），既不像并排也不像另起一行。
        self._ndi_grid = QGridLayout()
        self._ndi_grid.setContentsMargins(0, 0, 0, 0)
        self._ndi_grid.setHorizontalSpacing(8)
        self._ndi_grid.setVerticalSpacing(2)
        self._ndi_grid.addWidget(self.chk_ndi, 0, 0)
        self._ndi_grid.addWidget(self.lbl_ndi_name, 0, 1)
        self._ndi_grid.setColumnStretch(1, 1)
        self._ndi_stacked = False
        self._ndi_flow_busy = False
        go.addLayout(self._ndi_grid, 6, 0, 1, 2)
        # ---- Spout/NDI Alpha ----
        # ★ 2026-11-08（用户要求）：输出设置里加 Alpha 开关，只作用于 Spout/NDI；
        #   显示器窗口仍按黑底显示（物理显示器/HDMI 没有 alpha 通道）。
        self.chk_alpha = QCheckBox(T("Spout/NDI 输出 Alpha（透明通道）"))
        self.chk_alpha.setToolTip(T(
            "开启后，Spout 和 NDI 发送带透明通道（直通 Alpha）的画面。\n"
            "• 只影响 Spout / NDI；显示器窗口仍按不透明黑底显示（物理显示器没有 Alpha）。\n"
            "• 需要素材本身带 Alpha，且背景图层没有把整幅画面盖住；否则 Alpha 仍会是 255。\n"
            "• NDI 接收端必须支持 Alpha；OBS 用 Spout2 源时需在源属性里启用 Alpha/透明度。\n"
            "• 开启会增加少量合成开销：720p 约 +2~3ms/帧，1080p 约 +1ms/帧。\n"
            "• 关闭时保持原来的不透明输出，零额外开销。"))
        go.addWidget(self.chk_alpha, 7, 0, 1, 2)
        # ★ 2026-10-30（用户：「把下面多余的那行文字去掉」）：完整名已经直接显示在
        #   NDI 那一行上了，再补一行小字是重复 ⇒ 只保留 label 的 tooltip 做解释。
        # ★ 2026-10-18 用户要求：**「NDI 传输音频」这一行从界面删除**（复选框 + 提示）。
        #   NDI 音频转发后端（audio_engine 的 ndi_audio_on / ndi_out.send_audio）**原样保留**，
        #   只是不再有 UI 入口；配置文件里 ndi_audio 仍按原逻辑生效。
        outer.addWidget(g)
        self.resolution.currentIndexChanged.connect(main.set_resolution)
        self.resolution.editTextChanged.connect(main.set_resolution_text)
        self.chk_aspect.stateChanged.connect(main.set_aspect_lock)
        self.chk_border.stateChanged.connect(main.set_borderless)
        self.chk_top.stateChanged.connect(main.set_always_top)
        self.screen_combo.currentIndexChanged.connect(main.set_output_screen)
        self.chk_spout.stateChanged.connect(main.set_spout_enabled)
        self.chk_ndi.stateChanged.connect(main.set_ndi_enabled)
        self.chk_alpha.stateChanged.connect(main.set_output_alpha)
        self._update_ndi_full()   # NDI 完整接收名（机器名 + 源名）
        self.set_gate(False)   # 输出设置默认锁定，点「开始」后才可操作

    def set_mode(self, mode):
        """★ S2 #3/#4/#14：按**输出模式**决定"输出设置"里显示哪些项。

        · 单路（多屏整合单路）：只留 4 项 —— 输出显示器 / Spout 输出 / NDI 输出 /
          Spout·NDI Alpha（用户第 4 条：分辨率、显示输出窗口、锁定比例、无边框、
          窗口置顶、重置窗口大小 这一块**不要**）。
        · 多路：保留 分辨率 + 上述 4 项；但 **不显示** 显示输出窗口 / 锁定比例 /
          无边框 / 窗口置顶 / 重置窗口大小（用户第 14 条）。
        """
        single = str(mode) == "single"
        # (控件, 单路是否显示, 多路是否显示)
        rules = (
            (getattr(self, "lbl_resolution", None), False, True),
            (getattr(self, "resolution", None), False, True),
            (getattr(self, "btn_show_out", None), False, False),
            (getattr(self, "chk_aspect", None), False, False),
            (getattr(self, "chk_border", None), False, False),
            (getattr(self, "chk_top", None), False, False),
            (getattr(self, "btn_reset_win", None), False, False),
        )
        for w, in_single, in_multi in rules:
            if w is None:
                continue
            try:
                w.setVisible(in_single if single else in_multi)
            except Exception:                                     # noqa: BLE001
                pass
        # 下面 4 项两种模式都显示（单路时就是"独立一块"的全部内容）
        for w in (getattr(self, "lbl_output_screen", None),
                  getattr(self, "screen_combo", None),
                  getattr(self, "chk_spout", None),
                  getattr(self, "chk_ndi", None),
                  getattr(self, "chk_alpha", None)):
            if w is not None:
                try:
                    w.setVisible(True)
                except Exception:                                 # noqa: BLE001
                    pass

    def _update_ndi_full(self):
        """刷新 NDI **完整接收名**提示：`<本机名> (<源名>)`。

        机器名取 `%COMPUTERNAME%` —— NDI 用的就是它（实测 `tools/_probe_ndi_name.py`：
        源名 `EasyRealityAutoVJ` ⇒ 接收端 `6N4CZTM151UPP8Y (EasyRealityAutoVJ)`）。
        机器名取不到就只显示源名，绝不显示成 `(名)` 这种残缺串。

        ⚠ 源名现在是**写死的常量**（用户 2026-10-30：「不要输入框 把名字写死」），
          所以这里不再读输入框；名字以后要改就改 `config.DEFAULT_NDI_NAME`。

        ⚠⚠ 配套删除：`MainWindow.set_spout_name` / `MainWindow.set_ndi_name` 已**故意删除**
          （输入框没了，已无任何调用方）。`tools/_scan_removed_defs.py` 会报
          「ui_main.py 丢失 2 个定义」—— 那是预期的，不是批量替换误删。
        """
        try:
            nm = DEFAULT_NDI_NAME
            comp = str(os.environ.get("COMPUTERNAME") or "").strip()
            full = ("%s (%s)" % (comp, nm)) if comp else nm
            try:
                self.lbl_ndi_name.setText(full)      # 完整接收名（OBS 里就是它）
            except Exception:                                    # noqa: BLE001
                pass
            # 名字长度变了 ⇒ 并排/另起一行要重新判一次（换了电脑机器名长度会变）
            self._flow_ndi()
        except Exception:                                        # noqa: BLE001
            pass

    def _flow_ndi(self):
        """让 NDI 开关与完整名**自适应同一行或两行**（用户 2026-10-30 要求）。

        判据只看一件事：名字单行需要的宽度（`fontMetrics`）是否放得进"复选框右侧
        剩余的那点宽度"。放得下就并排；放不下就让名字整体挪到下一行（跨两列），
        此时它几乎总能一行显示完 —— 比硬挤成两三行的碎块好看，也更符合"另起一行"。

        滞回：从并排→堆叠的阈值是 `avail`，从堆叠→并排要 `avail-20`。
        没有滞回的话，窗口宽度正好卡在临界点时拖动窗口会疯狂来回跳。
        """
        if getattr(self, "_ndi_flow_busy", False):
            return
        grid = getattr(self, "_ndi_grid", None)
        if grid is None:
            return
        try:
            txt = self.lbl_ndi_name.text() or ""
            need = self.lbl_ndi_name.fontMetrics().horizontalAdvance(txt)
            chk = max(0, self.chk_ndi.sizeHint().width())
            # 面板宽 - 复选框 - 面板左右留白（实测 10 + 18）- 间距 8 - 安全余量 6
            # ⚠ 那 6px 不是多余的：`horizontalAdvance` 与实际绘制可能差 1~2px（不同
            #   字体/DPI 缩放），贴着边界时末尾字符会被裁掉。实测 380px 宽时正好卡在
            #   "需要 212 / 可用 214"，看着就是字顶到右边缘。
            avail = self.width() - chk - 42
            if getattr(self, "_ndi_stacked", False):
                want = need > (avail - 20)
            else:
                want = need > avail
            if want == getattr(self, "_ndi_stacked", False):
                return
            self._ndi_flow_busy = True
            try:
                if want:
                    # 另起一行：跨两列（第 1 行），名字自己独占一行
                    grid.addWidget(self.lbl_ndi_name, 1, 0, 1, 2)
                    room = self.width() - 28
                else:
                    grid.addWidget(self.lbl_ndi_name, 0, 1)
                    room = avail
                self._ndi_stacked = want
                # ⚠ 行高自己钉死：`QGridLayout` 对 hasHeightForWidth 的 item 会退回
                #   用 `sizeHint().height()`（实测 32px = 两行），于是并排时那一格
                #   凭空多出一行高度、文本飘在中间。按"当前实际可用宽度"算才对。
                hh = self.lbl_ndi_name.heightForWidth(max(40, int(room)))
                if hh > 0:
                    self.lbl_ndi_name.setFixedHeight(hh)
            finally:
                self._ndi_flow_busy = False
        except Exception:                                        # noqa: BLE001
            pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._flow_ndi()

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
        items = [T("关闭（不弹出输出窗口）"), T("窗口模式（可拖动）")]
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
        """写进配置并让快捷键立即生效。

        ⚠ 2026-10-10 审计修复：把「重复键校验」提到**写盘之前** —— 本次要写入的快捷键里
          若存在「同一个键配给多个启用动作」，先弹「仍然保存？」确认框（默认按钮 = 否），
          用户可选**取消**（不写配置、不 `apply()`、不清脏标记）。旧流程是「先 save 再
          由 hotkeys.apply() 告知」，用户无法反悔。
        """
        table = self._collect()

        # ---- 写盘前：检测本次 table 内的重复键 ----
        # 只比**本次要写入的 table**（cfg 里的旧值马上要被这批值覆盖，比它没有意义）。
        # 逐 key 归组，命中「同一 key ≥2 个 enabled 动作」即为冲突。
        _key_users = {}
        for _name, _spec in table.items():
            if not _spec.get("enabled", True):
                continue
            _k = (_spec.get("key") or "").strip()
            if not _k:
                continue
            _key_users.setdefault(_k, []).append(_name)
        _dups = [k for k, ns in _key_users.items() if len(ns) > 1]
        if _dups:
            # 文案复用 D 组已加进 i18n 的冲突词条（标题 T("快捷键冲突") + 正文 Tf(...)）。
            _ret = QMessageBox.question(
                self, T( T("快捷键冲突")),
                Tf("检测到 {} 组重复快捷键（{}）；只有先配置的那个会生效，"
                   "请在快捷键设置里改成不同的键。", len(_dups), "、".join(_dups)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)   # 默认按钮 = 否
            if _ret != QMessageBox.Yes:
                return          # ★ 取消：不写配置、不调 hotkeys.apply()、不改 self._dirty

        hk = self.main.cfg["hotkeys"]
        for name, spec in table.items():
            cur = hk.get(name)
            if not isinstance(cur, dict):
                cur = {}
                hk[name] = cur
            cur.update(spec)
        self.main.cfg.save()
        # ★ 修复 8：写盘前已弹过「可取消」的冲突窗（见上方校验），这里**不要再弹第二次**
        #   告知窗。D 组的 `hotkeys.apply()` 在 `interactive=True` 时会再弹一次冲突告知
        #   （`self.interactive` 是**实例标志**，HotkeyDialog 路径调 apply() 必然命中）
        #   ⇒ 会变成"双弹窗"。这里在调用前后**临时**切掉标志，用完必须还原（try/finally）：
        #   保留 `hotkeys.py` 给 `panic_reset` 等其它路径的告知能力（不改 hotkeys.py）。
        #   ⚠ 原有「异常不阻断后续流程」的语义保持（内层 `except Exception: pass`）。
        _hk = self.main.hotkeys
        _inter_bak = getattr(_hk, "interactive", True)
        _hk.interactive = False
        try:
            try:
                _hk.apply()
            except Exception:
                pass
        finally:
            _hk.interactive = _inter_bak
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

        # 项目地址（可点击；lbl_body 已开 openExternalLinks）
        proj_url = getattr(version, "PROJECT_URL", "") or ""
        proj_line = ""
        if proj_url:
            proj_line = (f'<p style="margin:2px 0 4px 0;">{pick("项目地址", "Project")}{_sp}'
                         f'<a href="{proj_url}" style="color:#5aa9e6;">'
                         f'{proj_url.replace("https://", "")}</a></p>')

        html = f"""
        <div style="font-family:system-ui,'Microsoft YaHei';">
          <h2 style="margin:0 0 2px 0;">{tr('app_name')}</h2>
          <div style="color:#888;">
            {_vline}
          </div>
          <p style="margin:8px 0 4px 0;">
            {pick('把音乐变成画面的实时 VJ 引擎：识别正在放的歌，按码表自动切换/驱动素材，'
                  '跟拍、跟情绪，DJ 只管放歌。',
                  'A real-time VJ engine that turns music into visuals: it recognizes the '
                  'track, then switches and drives clips from a cue sheet — on beat, on mood. '
                  'The DJ just plays.')}
          </p>
          {proj_line}

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


def merge_scan_tags(existing_tags, manual_tags, disabled_tags, *,
                    motion, flicker, coverage):
    """**纯函数**：把「快速重扫」检测出的动态/频闪/铺满度结果合并进自动标签。

    从 `DynTagScanThread.run()` 里抽出来，便于单元测试（原实现与 cfg/线程/self 耦合，
    只能靠起整个 GUI 才能验证）。**无任何副作用**：

    - 绝不读取/写入 `cfg`、文件或 `self`；只按入参计算并返回**新的**列表。
    - 绝不返回、绝不修改 `manual_tags` / `disabled_tags`（这两个是"用户意志"，重扫碰不得）。
    - `existing_tags` 不被就地修改（内部先 `list(existing_tags)` 复制）。

    Args:
        existing_tags: 当前自动标签列表（`MediaItem.tags` 的快照）。
        manual_tags:   用户手动标签（仅用于"不复活"语义的说明，本函数**不读也不用**它，
                       保留参数是为了让调用点语义自明、并便于将来断言）。
        disabled_tags: 被用户取消的自动标签 —— 命中者**不复活**。
        motion:        三档动态词之一；`""`（打不开）⇒ **原样返回**，整个列表不动。
        flicker:       是否频闪（True 时补 `DYNAMIC_FLICKER`）。
        coverage:      铺满度 0~1；None ⇒ 保留原铺满度词不动。

    Returns:
        合并后的新标签列表。

    语义（与旧内联实现逐条等价）：
      1. `motion` 为空 → `return list(existing_tags)`（完全不变，含动态/铺满度/亮度词）。
      2. 先剔除旧动态词，按新 `motion`/`flicker` 补词（命中 `disabled_tags` 的跳过）。
      3. `coverage is not None` → 剔除旧铺满度词，`>= COVERAGE_FULL_THRESHOLD` 补「铺满」
         否则补「局部」（命中 `disabled_tags` 的跳过）；`coverage is None` → 原样保留。
      ⚠ 2026-10-30 用户「大砍刀」：亮度分档（暗基调/中亮度/亮基调）整条删除，
         `brightness` 参数与对应分支一并移除。
    """
    # (1) 打不开：motion=""，保持原标签完全不动（含已有的铺满度/亮度词）。
    if not motion:
        return list(existing_tags)

    dis = set(disabled_tags or [])
    tags = [t for t in existing_tags if t not in DYNAMIC_TAGS]

    # (2) 动态三档 + 频闪（被用户取消的不复活）
    if motion not in dis:
        tags.append(motion)
    if flicker and DYNAMIC_FLICKER not in dis:
        tags.append(DYNAMIC_FLICKER)

    # (3) 铺满度：只有真算出 coverage 才动标签；算不出则保留原铺满度词不动。
    if coverage is not None:
        tags = [t for t in tags if t not in COVERAGE_TAGS]
        word = COVERAGE_FULL if coverage >= COVERAGE_FULL_THRESHOLD else COVERAGE_PART
        if word not in dis:
            tags.append(word)

    return tags


def pick_unscanned(library):
    """挑出**从未扫描过**的素材路径（= `_populate_grid` 在「全部」档会隐藏的那批）。

    ★★ 2026-10-31（用户「素材库怎么不显示图片」「导入图片也没反应」）：
      `_populate_grid` 里 `scanned = bool(it.all_tags())`，未扫描的素材在**任何工作档**
      都不显示（用户 2026-10-30 要求「选择时就排除这些」）。而导入流程过去从不触发
      扫描 ⇒ 新素材永远看不见。这个函数就是"该补扫谁"的唯一判据（纯函数，便于测试）。

    条件（与 `DynTagScanThread.run()` 的增量挑选保持一致）：
      · kind ∈ **video / gif**（图片不算：静态图本来就不打标 —— 用户 2026-10-31
        「图片不要打标」，见 `tagger.analyze_dynamic` 的 image 分支）
      · 未被标记「排除」（刻意不参与自动匹配的 logo 素材）
      · `tags` 为空（= 从未扫出过结果）
    """
    out = []
    for m in (library or {}).values():
        try:
            if m.kind not in ("video", "gif"):
                continue
            if getattr(m, "excluded", False):
                continue
            if m.tags:
                continue
            out.append(m.path)
        except Exception:                                          # noqa: BLE001
            continue
    return out


def passes_lib_filter(it, dyn_idx):
    """素材库「动态」下拉的筛选判据（**纯函数**，便于测试）。

    `dyn_idx`：0 全部 / 1 低 / 2 中 / 3 高 / 4 频闪 / 5 已排除 / 6 未扫描

    语义（三次用户反馈后定稿）：
      · 0「全部」 = **库里所有素材**（含已排除、含未扫描）—— 用户 2026-10-31 连续问
        「其他图片去哪了」「我那些 dj 的 logo 去哪了」，查下来这 24 项（6 个 LOGO 循环
        视频 + 8 张图）是他自己标过「已排除」的，但平时找不回来。
        ⇒ 素材库是**管理界面**，必须能看到全部；「已排除」的会被 `_tint_role`
          整体变暗 + tooltip 注明"已排除自动匹配"，一眼能分辨。
        ⚠ 换池（`match_engine`）仍然排除它们 —— "选择时排除"落在那里，不是这里。
      · 1~4      = 只显示匹配该动态档的（没扫过的没有标签，自然不会出现在这里）
      · 5「已排除」/ 6「未扫描」= 专门用来批量处理的两类
      · **图片（image）永远算"已就绪"**：静态图不打标（用户 2026-10-31「图片不要打标」），
        若仍按标签判，它会被当成"未扫描"。
    """
    excluded = bool(getattr(it, "excluded", False))
    scanned = bool(it.all_tags()) or getattr(it, "kind", "") == "image"
    if dyn_idx == 5:
        return excluded
    if dyn_idx == 6:
        return (not scanned) and (not excluded)
    if dyn_idx == 0:
        return True
    if excluded or not scanned:
        return False
    want = {1: DYNAMIC_LOW, 2: DYNAMIC_MID,
            3: DYNAMIC_HIGH, 4: DYNAMIC_FLICKER}.get(int(dyn_idx))
    return (want in it.all_tags()) if want else True


class DynTagScanThread(QThread):
    """素材**动态/铺满度**扫描（唯一的重扫线程）。

    ★★ 2026-10-30 用户「大砍刀」：CLIP 视觉打标（`TagScanThread`）、亮度分档、
      曲风识别全部删除，这个线程从"补动态标签的辅助线程"升级成**唯一的扫描入口**：
        · 动态三档 + 频闪  —— `tagger.analyze_dynamic`（顺序读全片帧差）
        · 铺满 / 局部      —— `tagger.coverage_score`（取一帧算边缘+分块）
        · 角色（前景/背景）—— `tagger.auto_roles`（alpha / 铺满度 / 高动态）
      绝不碰 `manual_tags` / `disabled_tags`（那是用户意志）。

    参数：
      paths: 只扫这些路径（右键"重扫这个素材"）；None = 全库
      force: True = 连已有标签/角色一起重算（全量重扫）；False = 增量（没标签的才扫）
    """
    progress = Signal(int, int)

    def __init__(self, main, paths=None, force=False):
        super().__init__(main)
        self.main = main
        self.paths = paths
        self.force = force

    def run(self):
        import cv2
        from tagger import Tagger, auto_roles
        from media_manager import _ascii_safe_path
        items = [m for m in self.main.library.values()
                 if m.kind in ("image", "video", "gif") and not getattr(m, "excluded", False)]
        if self.paths:
            want = set(self.paths)
            items = [m for m in items if m.path in want]
        elif not self.force:
            items = [m for m in items if not m.tags]
        total = len(items)
        for i, m in enumerate(items):
            # 支持中途中止（关闭窗口时由 closeEvent 发 requestInterruption）：
            # 每个素材一次检查点，退出后不再对新素材解码。
            if self.isInterruptionRequested():
                break
            try:
                kind = "image" if m.kind in ("image", "gif") else "video"
                motion, flicker, _diff = Tagger.analyze_dynamic(m.path, kind)
                # ★ 2026-10-30 用户「给素材打分 0-1」：同一份帧差顺便算连续分
                #   （零额外解码成本），换池时按"离目标强度多近"挑（match_clips）。
                _sc = Tagger.dynamic_score(_diff, flicker)
                # ---- 铺满度：开图/取一帧（70% 位置）算一次。
                #   增量模式下"已有铺满度词"的素材可以跳过，省一次开图；
                #   全量重扫（force）与单素材重扫一律重算。
                need_cov = self.force or not (set(m.tags) & set(COVERAGE_TAGS))
                cov = None
                has_alpha = bool(getattr(m, "_has_alpha", False))
                if need_cov:
                    if kind == "image":
                        # ★ 图片用 IMREAD_UNCHANGED：既是铺满度的输入，也顺手判 alpha
                        #   （PNG 带透明 = 天然前景叠加层；`_has_alpha` 只对视频探测，
                        #   图片如果不在这里判，透明 PNG 会被当背景）。
                        img = cv2.imread(_ascii_safe_path(m.path), cv2.IMREAD_UNCHANGED)
                        if img is not None:
                            if img.ndim == 3 and img.shape[2] == 4:
                                has_alpha = True
                                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
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
                # ---- 合并动态/频闪/铺满度（纯函数，UI 与此处互不耦合）。
                #   ⚠ 只写自动标签（m.tags）；绝不写 manual_tags / disabled_tags。
                tags = merge_scan_tags(m.tags, m.manual_tags, m.disabled_tags,
                                       motion=motion, flicker=flicker, coverage=cov)
                if tags != m.tags:
                    m.tags = tags
                    self.main.cfg["clip_tags"][m.path] = tags
                if _sc is not None:
                    m.score = float(_sc)
                    self.main.cfg["clip_score"][m.path] = float(_sc)
                # ---- 角色：全量重扫一律重算；增量只补"两个角色都没勾"的。
                #   ★ 砍掉 CLIP 内容标签后，`auto_roles` 只看 alpha / 铺满度 / 高动态。
                if self.force or not any(m.roles.values()):
                    m.roles = auto_roles(kind, has_alpha, coverage=cov,
                                         dynamic=motion in (DYNAMIC_MID, DYNAMIC_HIGH))
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
        self.main.cfg["dyn_tag_ver"] = 7
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
        self.lbl_title.setText(T("正在扫描素材库… {}/{}").format(done, total))

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


class _SmoothWindowDragFilter(QAbstractNativeEventFilter):
    """用**普通鼠标消息**移动窗口，替代 Windows 标题栏的模态移动循环。

    ★★ 2026-11-08（用户：刚开始拖动任何窗口的一秒仍掉帧）：
      实测系统标题栏拖动会进入 `DefWindowProc` 的模态移动循环，`processEvents()`
      单次可阻塞 **1063ms**；这期间 Qt 的帧/定时器事件不派发 ⇒ 预览和输出一起掉帧。
      这里拦截 `WM_NCLBUTTONDOWN(HTCAPTION)`，自己 `SetCapture` + 跟随鼠标
      `SetWindowPos`；事件循环保持运行，帧继续派发。

    范围：本进程所有顶层窗口的**标题栏拖动**（主窗口/输出窗口/对话框）。
    * 最大化窗口不拦截（保留系统还原/拖动行为）；
    * 双击标题栏不拦截（保留系统最大化）；
    * 边缘缩放仍走系统原生（本轮只修“拖动”）。
    """

    WM_NCLBUTTONDOWN = 0x00A1
    WM_NCLBUTTONUP = 0x00A2
    WM_NCLBUTTONDBLCLK = 0x00A3
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONUP = 0x0202
    WM_CAPTURECHANGED = 0x0215
    HTCAPTION = 2
    SWP_FLAGS = 0x0001 | 0x0004 | 0x0010   # NOSIZE | NOZORDER | NOACTIVATE

    def __init__(self):
        super().__init__()
        self._drag = None   # (hwnd, cursor_x, cursor_y, win_left, win_top)

    def _release(self):
        if self._drag is not None:
            try:
                import ctypes
                ctypes.windll.user32.ReleaseCapture()
            except Exception:                                  # noqa: BLE001
                pass
            self._drag = None

    def nativeEventFilter(self, eventType, message):
        if eventType != b"windows_generic_MSG":
            return False, 0
        try:
            import ctypes
            import ctypes.wintypes as wt
            user32 = ctypes.windll.user32
            msg = wt.MSG.from_address(int(message))
            # 只处理本进程窗口；原生过滤器也会收到其它进程的消息（极少）
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(int(msg.hWnd), ctypes.byref(pid))
            if pid.value != ctypes.windll.kernel32.GetCurrentProcessId():
                return False, 0

            if msg.message == self.WM_NCLBUTTONDBLCLK and int(msg.wParam) == self.HTCAPTION:
                # 双击标题栏：交回系统做最大化；若上一拍已经进入自绘拖动，先释放
                self._release()
                return False, 0

            if self._drag is None:
                if (msg.message == self.WM_NCLBUTTONDOWN
                        and int(msg.wParam) == self.HTCAPTION):
                    hwnd = int(msg.hWnd)
                    # 最大化/最小化窗口交给系统；普通窗口才接管
                    if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
                        return False, 0
                    # 只接顶层窗口（防止误接子控件）
                    user32.GetAncestor.restype = ctypes.c_void_p
                    root = user32.GetAncestor(hwnd, 2)   # GA_ROOT
                    if root and int(root) != hwnd:
                        return False, 0
                    rect = wt.RECT()
                    point = wt.POINT()
                    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        return False, 0
                    user32.GetCursorPos(ctypes.byref(point))
                    self._drag = (hwnd, point.x, point.y, rect.left, rect.top)
                    user32.SetForegroundWindow(hwnd)
                    user32.SetCapture(hwnd)
                    return True, 0
                return False, 0

            hwnd, cx, cy, wx, wy = self._drag
            if msg.message == self.WM_MOUSEMOVE:
                if user32.GetAsyncKeyState(0x01) & 0x8000:
                    point = wt.POINT()
                    user32.GetCursorPos(ctypes.byref(point))
                    user32.SetWindowPos(hwnd, 0,
                                        wx + point.x - cx, wy + point.y - cy,
                                        0, 0, self.SWP_FLAGS)
                    return True, 0
                self._release()
                return True, 0
            if msg.message in (self.WM_LBUTTONUP, self.WM_NCLBUTTONUP,
                               self.WM_CAPTURECHANGED):
                self._release()
                return True, 0
        except Exception:                                        # noqa: BLE001
            self._release()
        return False, 0


class _TaskbarCloseFilter(QAbstractNativeEventFilter):
    """任务栏「关闭窗口」在有模态对话框时也要能退出。

    ★★ 2026-11-02（用户：「开了二级窗口不关掉，主窗口就不能从任务栏关掉」）：
      `QDialog.exec()` 是**应用级模态** ⇒ Qt 禁用主窗口，Windows 发来的 `WM_CLOSE`
      被丢掉，任务栏关闭「没反应」。Qt 层的 eventFilter 拿不到这些消息（主窗口被禁用），
      只有原生事件层能接。
    做法：有模态对话框 ⇒ 先 `reject()` 再下一拍 `close()` 主窗口（走正常 closeEvent，
      线程停止/配置落盘都照旧）；没有模态 ⇒ 放行。
    ⚠ 只认主窗口自己的 hwnd；任何异常都放行（绝不因为过滤器让窗口关不掉）。
    """

    WM_CLOSE = 0x0010

    def __init__(self, win):
        super().__init__()
        self._win = win

    def nativeEventFilter(self, eventType, message):
        try:
            if eventType != b"windows_generic_MSG":
                return False, 0
            import ctypes.wintypes as _wt
            msg = _wt.MSG.from_address(int(message))
            if msg.message != self.WM_CLOSE:
                return False, 0
            try:
                mine = int(self._win.winId())
            except Exception:                                    # noqa: BLE001
                return False, 0
            if int(msg.hWnd) != mine:
                return False, 0
            from PySide6.QtWidgets import QApplication
            md = QApplication.activeModalWidget()
            if md is None:
                return False, 0
            try:
                md.reject()
            except Exception:                                    # noqa: BLE001
                pass
            QTimer.singleShot(0, self._win.close)
            return True, 0
        except Exception:                                        # noqa: BLE001
            return False, 0


class ErlibImportThread(QThread):
    """导入 erlib 分析包（`.eravjlib`）—— **后台跑**，大包几万条指纹别卡界面。

    ⚠ 导入是"并进本机"（幂等：同一首重导会覆盖），不碰本机自己扫的曲库。
    ⚠ 指纹直接写进主库 `fingerprints.db` ⇒ **识别链路一行都不用改**；
      但必须由 `erlib.transfer._import_fingerprints` 负责对齐 `user_version`，
      否则主程序下次打开会把整张表当成"旧版本"清空（已加测试守这一条）。
    """
    progress = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, pkg, parent=None, skip_sids=None):
        super().__init__(parent)
        self.pkg = pkg
        # ★★ 2026-10-31（用户「导入重复的歌也没提醒」）：本机已有的那些 song_id。
        #   由 `erlib.dupdetect` 在**界面线程**判出来（判据要读 cfg，且要弹窗问用户），
        #   这里只负责"照着跳过"，不重复做判定（判据写两遍必然漂移）。
        self.skip_sids = set(int(x) for x in (skip_sids or ()))

    def run(self):
        try:
            from config import app_base_dir
            from erlib.transfer import import_into

            def _p(a, b, s):
                self.progress.emit(a, b, s)
            st = import_into(self.pkg, app_base_dir(), on_progress=_p,
                             skip_sids=self.skip_sids)
            self.finished_ok.emit(st)
        except Exception as e:                                   # noqa: BLE001
            self.failed.emit("%s: %s" % (type(e).__name__, e))


class MusicScanThread(QThread):
    """后台音乐分析：逐首建指纹 + 算节拍网格 + 能量段落，progress 增量更新"""

    progress = Signal(int, int, str)   # done, total, path

    def __init__(self, main, paths, force=False):
        super().__init__(main)
        self.main = main
        self.paths = paths
        self.force = force

    @staticmethod
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
        """扫描并发数：**演出中 2**、**空闲 min(8, max(3, 核数//2))**（16 逻辑核 → 8）。

        可用 `AUTO_VJ_SCAN_THREADS` 环境变量覆盖（实测/调试用）。

        ★★ 2026-10-30（用户「扫描没有曲风为什么还是这么慢」）：原来 `min(6, 核数//3)`
          （16 核 → 5）。实测同一批 10 首（`tools/_bench_scan.py`）：
            5 线程 3.48 秒/首 → 8 线程 **2.97 秒/首**（-15%）→ 12 线程 3.06（回退）。
          为什么不是越多越好：每首要解码 300 秒音频、算两次 STFT、跑一次 ONNX，
          全是**内存带宽**密集型，线程超过物理核（这台 8 物理 / 16 逻辑）后互相抢带宽
          反而变慢。`核数//2` 正好落在物理核数上。
        ⚠ 演出中仍然是 2（现场稳定性优先，绝不能因为扫描抢 CPU 让画面/音频抖）。
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
        return 2 if busy else max(3, min(8, n // 2))

    def run(self):
        import hashlib
        import os as _os
        import threading as _th
        import time as _tm
        from concurrent.futures import ThreadPoolExecutor
        import numpy as np
        import soundfile as sf
        from fp import FingerprintDB
        import fp as fpvj          # 指纹算法本体（签名/文件 md5 也在里面）
        from config import app_base_dir

        db = FingerprintDB(_os.path.join(app_base_dir(), "fingerprints.db"))
        try:
            db.begin_scan()       # 批量写入模式（synchronous=OFF + 攒批 commit，收尾 end_scan 恢复）
        except Exception:
            pass
        meta = self.main.cfg["music_meta"]
        items = list(self.paths) if self.paths else list(self.main.cfg["music_library"])

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

        # ★ 增量扫描也要能补网格（阻塞项 F-a，旧实现 `todo=[p for p in items if force or
        #   p not in meta]` 让**存量歌永远不重算** ⇒ 老曲库永远拿不到网格）。现在把待办拆成两类：
        #     · todo（= todo_full）：需要**完整分析**（解码 + 指纹 + 曲风）
        #     · todo_grid：已有元数据、只缺网格 / grid_ver 过期 ⇒ **只算网格**，
        #       跳过指纹 / 联网 / 本地 AI（用户点一次「扫描分析」即可把全库缺失网格补齐，
        #       ~0.5s/首量级，不重跑指纹/联网）。
        _GRID_VER = getattr(_bg, "GRID_VER", 1) if _bg is not None else 1

        def _need_grid(p):
            m = meta.get(p)
            if not isinstance(m, dict):
                return False
            g = m.get("grid")
            return not (isinstance(g, dict) and g.get("beat_len")
                        and g.get("grid_ver") == _GRID_VER)

        todo = [p for p in items if self.force or p not in meta]
        todo_grid = [p for p in items
                     if not (self.force or p not in meta) and _need_grid(p)]
        # ★ 第三类待办：**已有元数据、但缺认歌字段**（存量曲库）。
        #   为什么必须有：引擎侧拼"认歌查询参数"的那一步（`engine._cue_track_query`）里
        #   ① 级（指纹认歌）读 `meta["fp_id"]`、⓪ 级（同一文件确定判据）读 `meta["file_md5"]`，
        #   而本轮之前扫过的歌**一个都没有**这两个字段 ⇒ 码表认歌的 ⓪/① 两级**永不命中**，
        #   只剩最容易误判的"标题+艺人 / 文件名"在撑。用户不重扫就永远拿不到。
        #   这类只做「解码 → 指纹 → 签名 → 写 fp_id」与「读文件字节算 md5 → 写 file_md5」，
        #   **不重跑**联网曲风 / 本地 AI / 封面 / 节拍网格（已有就保留）。
        #   ⚠ 判据在 `fp.meta_needs_fp()`（零 Qt，可无头单测）：缺任一个都算待办。
        #   ⚠⚠ **不要改成"启动即自动补"**：这个补算是几十分钟级别的 CPU/磁盘活
        #      （65 首 ≈ 30s，上千首 ≈ 十几分钟），必须由用户在「扫描分析」里**显式触发**。
        #      启动时抢 CPU/IO 会直接影响演出渲染与音频回调（本项目铁律：现场稳定性优先）。
        def _need_fp(p):
            return fpvj.meta_needs_fp(meta.get(p))

        todo_fp = [p for p in items
                   if not (self.force or p not in meta) and _need_fp(p)]
        total = len(todo) + len(todo_grid) + len(todo_fp)
        if todo_grid:
            _log("网格增量补算：%d 首缺网格（已有元数据）只补网格，不重跑指纹/联网" % len(todo_grid))
        if todo_fp:
            _log("认歌字段增量补算：%d 首缺 fp_id / file_md5（已有元数据）只补这两项，"
                 "不重跑曲风/封面/网格" % len(todo_fp))

        # 老曲库已算好的 `fp_id` / `file_md5` 快照（**主线程先取好**，worker 里不再并发读
        # `cfg["music_meta"]`）。⚠ `force`（全量重扫）时**一律不信旧值**：用户重扫常常
        # 正是因为文件被重新编码/替换过，沿用旧 md5/签名会让 ⓪/① 级拿旧判据去认新文件。
        old_fp, old_md5 = {}, {}
        if not self.force:
            for _p in items:
                try:
                    _fp_v, _md5_v = fpvj.meta_fp_fields(meta.get(_p))
                except Exception:                                    # noqa: BLE001
                    _fp_v, _md5_v = "", ""
                if _fp_v:
                    old_fp[_p] = _fp_v
                if _md5_v:
                    old_md5[_p] = _md5_v
        # ★ 手工标注（grid_manual）与八拍相位翻转（grid_flip）是**用户产物**：重扫只重建
        #   grid、不动它们（架构 §3.5）。**force 重扫也要保留**（和 fp_id/md5 不同——那是
        #   算法判据，force 时该重算；手工标注是用户显式点的，绝不能丢）。主线程快照。
        old_user = {}
        for _p in items:
            _m = meta.get(_p)
            if isinstance(_m, dict) and (_m.get("grid_manual") or _m.get("grid_flip")):
                old_user[_p] = {"grid_manual": _m.get("grid_manual"),
                                "grid_flip": _m.get("grid_flip")}

        def _grid_one(p):
            """只算网格（已有元数据、缺网格）：解码一次 → grid_from_signal → 返回 grid。

            ★ 不碰指纹 / 联网 / 本地 AI（只补缺失的部分）。读取按 200s 封顶并传入**真实时长**，
              既省内存/时间，又保证 VDJ 按时长匹配用对 dur（网格来源 src 判定靠它）。"""
            try:
                import soundfile as sf
                info = sf.info(p)
                dur = float(info.duration)
                n = min(int(info.frames), int(200.0 * info.samplerate))
                data, sr = sf.read(p, dtype="float32", always_2d=True, frames=n)
                sig = np.mean(data, axis=1)
                if _bg is None:
                    return p, None
                # ★ 2026-10-30 用户要求「去掉 vdj 源，完全依赖本地扫描确定 bpm」
                return p, _bg.grid_from_signal(sig, sr, vdj_idx, path=p, dur=dur,
                                              use_vdj=False)
            except Exception:
                return p, None

        def _one(p):
            """单首：解码 → 指纹入库（+ 指纹签名） → 曲风（本地）。返回 (path, r, 三段耗时)。

            本函数在 worker 线程里跑，**任何异常都自己吞掉**（返回 r=None），
            免得一个坏文件把整轮扫描打断。
            """
            try:
                t0 = _tm.perf_counter()
                data, sr = sf.read(p, dtype="float32", always_2d=True)
                # ★ 2026-11-06：与 ERLIB 扫描统一 —— 网格算法必须拿到**真实时长**
                # ⚠ 与 `erlib.scanner.decode()` 逐字一致：dur 取自 sf.info().duration，
                #   不用 len(data)/sr（mp3 的编码器延迟会让两者差几毫秒 → anchor 偏移）
                dur_full = float(sf.info(p).duration)
                sig = np.mean(data, axis=1)
                t1 = _tm.perf_counter()
                song_id = int(hashlib.md5(p.encode("utf-8")).hexdigest()[:8], 16)
                # ★ 指纹签名 `fp2-<16hex>`（= 码表 `identity.fp_id`，认歌 **① 级**用它）。
                #   ⚠ 全曲 STFT 是纯 CPU 活，**必须放在 db_lock 外面**：放里面会让 6 个
                #     worker 全部串行等锁，扫描退化成单线程（65 首要等几分钟）。
                #   ⚠ 签名算不出来**不能丢整首**：指纹入库是主功能、签名是附加 ⇒
                #     失败就 `fp_sig=""`，元数据照写（下次扫描会再补，见阶段 1d）。
                #   ⚠ 已有 `fp_id` 的老歌（force 重扫）直接复用，不重算 sha1。
                hashes = None
                try:
                    hashes = fpvj.fingerprint_audio(sig, sr)
                except Exception:                                    # noqa: BLE001
                    hashes = None          # add_song 会自己算一次；仍失败则整首丢（同改动前）
                fp_sig = str(old_fp.get(p) or "")
                if hashes is not None and not fp_sig:
                    try:
                        fp_sig = fpvj.signature_from_hashes(hashes)
                    except Exception:                                # noqa: BLE001
                        fp_sig = ""
                with db_lock:
                    # ★ 批量写入：攒 6 首落一次盘 —— INSERT 本身躲不掉，但能省掉"每首一次 fsync"
                    #   （HDD 上单次 fsync 要等盘片 10~50ms，这钱白花）。
                    # ★ 传 `hashes=` 复用刚算好的指纹，避免第二次全曲 STFT。
                    db.add_song(song_id, sig, sr, commit=False, hashes=hashes)
                    if db.pending() >= 6:
                        db.commit_batch()
                t2 = _tm.perf_counter()
                # ★ 2026-10-30 用户「大砍刀」：曲风识别（本地 AI + discogs 在线查询）整块删除。
                #   扫描现在只做：指纹入库 / 文件 md5 / 节拍网格 / 能量分段。
                r = {"song_id": song_id}
                # ★ 指纹签名（码表认歌 ① 级）。空串 = 这次没算出来（下次扫描会重试），
                #   **绝不**退化成写 `song_id`（那是「路径」哈希，与文件内容无关 ⇒ 静默永不命中）。
                r["fp_id"] = fp_sig
                # ★ 文件字节 md5（码表认歌 ⓪ 级）：「同一个文件是确定判据」，与 ① 级**互补**
                #   —— ① 抗改名/换容器（同一段音频重编码仍命中），⓪ 抗"指纹算不出来 /
                #   指纹算法版本变了"。成本实测 0.079s/首（534 MB/s，38~51MB 的 flac）。
                #   ⚠ 老歌已有就直接搬（`old_md5` 快照），重复扫描不重复读盘。
                fmd5 = str(old_md5.get(p) or "")
                if not fmd5:
                    try:
                        fmd5 = fpvj.file_md5(p)
                    except Exception:                                # noqa: BLE001
                        fmd5 = ""
                r["file_md5"] = fmd5
                # ★ 节拍网格（BPM / 锚点 / 八拍乐句相位）—— 离线一次入库，演出中只查表。
                #   复用刚解码的 sig：扫描已经为指纹读过一次盘，这里不再读第二遍。
                _feats = {}      # 网格算好的 STFT（下面能量分段直接复用，见 beatgrid 注释）
                if _bg is not None:
                    try:
                        g = _bg.grid_from_signal(sig, sr, None, path=p, dur=dur_full,
                                                 use_vdj=False, out_feats=_feats)
                        if g:
                            r["grid"] = g
                    except Exception:
                        pass
                # ★★★ 2026-10-29 自动能量分段（用户要求「扫描曲库时自动识别 drop 段落」）
                #   之前这里是"段落分析已移除"（旧版是手标模板，不准），现在换成
                #   基于能量落差的自动分段 —— 实测 INTERNET YAMERO 四个边界全中
                #   （误差 ≤0.1 秒，见 sections.auto_segments 的注释）。
                #   存两套：`sections`（按八拍对齐，引擎用）+ `sections_raw`（原始边界，
                #   编辑时切换"八拍/小节"用 —— 八拍是小节的子集，只留吸附值就细化不回去）。
                if _bg is not None:
                    try:
                        import sections as _sec
                        # ⚠ 不要在这里用 `np`：`import numpy as np` 是 1721 行
                        #   （另一个方法内）的**局部导入**，这里拿不到 ⇒ NameError
                        #   会被下面的 `except Exception` 吞掉、静默不生效。
                        #   `sig` 本身就是 `np.mean(data, axis=1)` 的连续数组，直接传。
                        # ★ 2026-10-30（扫描提速）：网格刚刚在**同一个重采样信号**上
                        #   算过 features ⇒ 直接复用，别再 resample + features 一遍
                        #   （297 秒的歌省 0.45 秒/首）。拿不到才自己算。
                        _f2 = _feats.get("f")
                        _d2 = float(_feats.get("dur") or 0.0)
                        _m22 = None
                        if _f2 is None or _d2 <= 0.0:
                            _m22, _s22 = _bg.resample(sig, sr, _bg.SR_TARGET)
                            _f2 = _bg.features(_m22, _s22)
                            _d2 = len(_m22) / float(_s22)
                        # ★★ 2026-10-30：用**生效网格**（`grid_manual.bpm_manual` 优先），
                        #   否则用户手改 BPM 之后分段仍按旧的自动 BPM 算 —— Extra Pain
                        #   手改成 200 但 `grid` 还是 99.95 ⇒ "2 个八拍"被当成 9.6 秒
                        #   （真实 4.8）⇒ 整首只切出 2 段、边界偏一倍。
                        _oldm = meta.get(p) or {}
                        _seg_g = _sec.grid_from_meta({
                            "grid": r.get("grid") or _oldm.get("grid") or {},
                            "grid_manual": _oldm.get("grid_manual") or {},
                        }) or None
                        _sg = _sec.auto_segments(_f2, dur=_d2, grid=_seg_g)
                        _af = _sec.take_folded_anchor()
                        if _af is not None and isinstance(r.get("grid"), dict):
                            r["grid"]["anchor"] = _af
                        # ★ 2026-10-30：`raw` 必须**同样过乐句判据**（只是不吸附位置），
                        #   否则段数和 `sections` 对不上 —— 详情页读 raw，用户看到的
                        #   就是另一套分段（"怎么还是三个八拍五个八拍"）。
                        _sgr = _sec.auto_segments(_f2, dur=_d2, grid=None,
                                                  phrase_grid=_seg_g)
                        if _sg:
                            r["sections"] = _sg
                            r["sections_raw"] = _sgr
                            r["sections_auto"] = True
                        del _f2
                        if _m22 is not None:
                            del _m22
                    except Exception:
                        pass
                # ★ 2026-10-30（用户「时长后面再加个波形图」）：扫描时顺手把**波形包络**
                #   写进 `.wj` 缓存 —— 这时 `sig` 已经在内存里，只多花 ~30ms/首；
                #   不然曲库一打开要 90 首挨个解码（~45 秒）才画得出波形。
                try:
                    import waveform as _wfm
                    if not _wfm.waveform_load(p):
                        _wf1 = _wfm.waveform_from_signal(sig, sr)
                        if _wf1:
                            _wfm.waveform_save(p, _wf1)
                except Exception:
                    pass
                # ★ 歌曲封面（列表左侧那张图）：**在建库时离线取一次**入库。
                #   演出中列表只读缓存路径，绝不为了封面去开容器/解码（实测 12~18ms/首）。
                #   ⚠ 单独 try：取不到封面（无内嵌也无同目录图）不能影响曲风/网格入库。
                try:
                    import music_cover as _mcover
                    _cv = _mcover.extract(p)
                    if _cv:
                        r["cover"] = _cv
                except Exception:
                    pass
                # ★ 保留用户手工产物（重扫只重建 grid，不动 grid_manual / grid_flip）。
                _ou = old_user.get(p)
                if _ou:
                    if _ou.get("grid_manual"):
                        r["grid_manual"] = _ou["grid_manual"]
                    if _ou.get("grid_flip"):
                        r["grid_flip"] = _ou["grid_flip"]
                    # ★ 手改过的段落一律保留（`sections_auto is False` 就是手动保存过的）
                    if _ou.get("sections_auto") is False and _ou.get("sections"):
                        r["sections"] = _ou["sections"]
                        r["sections_raw"] = _ou.get("sections_raw")
                        r["sections_auto"] = False
                # ★ 2026-10-30：t3（曲风解析）删除后，第三段改成「元数据（md5/网格/分段）」
                return p, r, (t1 - t0), (t2 - t1), (_tm.perf_counter() - t2)
            except Exception:
                return p, None, 0.0, 0.0, 0.0

        # ---- 阶段 1：**并行**做本地部分（解码 + 指纹 + 节拍网格 + 能量段落）----
        # ★ 2026-10-30 用户「大砍刀」：曲风查询（本地 AI + discogs）整条删除，扫描不再联网。
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
                        _log("阶段1 %d/%d：平均 解码 %.2fs + 指纹 %.2fs + 元数据 %.2fs "
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

        # ---- 阶段 1b：**只补网格**（存量歌缺网格 / grid_ver 过期）----
        # 已有元数据的歌：跳过指纹 / 联网 / 本地 AI，只解码一次算网格并入原记录（保留其它字段）。
        if todo_grid:
            tg0 = _tm.perf_counter()
            okg = 0
            try:
                with ThreadPoolExecutor(max_workers=n_workers) as ex:
                    for p, g in ex.map(_grid_one, todo_grid):
                        if g:
                            rec = dict(meta.get(p) or {})
                            rec["grid"] = g
                            meta[p] = rec
                            okg += 1
                        done += 1
                        self.progress.emit(done, total, p)
                        if done % 20 == 0:
                            try:
                                self.main.cfg.save()
                            except Exception:
                                pass
            except Exception as e:                                       # noqa: BLE001
                _log("阶段1b（补网格）异常终止：%s" % e)
            try:
                self.main.cfg.save()
            except Exception:
                pass
            _log("阶段1b（补网格）完成：%d 首只补网格，成功 %d 首，用时 %.1f 秒"
                 % (len(todo_grid), okg, _tm.perf_counter() - tg0))

        # ---- 阶段 1c：**补封面**（存量歌还没有封面路径的）----
        # 为什么要单独一遍：本轮之前扫过的歌，meta 里没有 "cover" 键；
        # 用户点一次「扫描分析」就能把全库封面补齐（~15ms/首，几百首也就几秒）。
        try:
            import music_cover as _mcover
            todo_cover = [p for p in items
                          if isinstance(meta.get(p), dict) and not meta[p].get("cover")]
        except Exception:                                            # noqa: BLE001
            _mcover, todo_cover = None, []
        if todo_cover and _mcover is not None:
            tc0 = _tm.perf_counter()
            okc = 0

            def _cover_one(p):
                try:
                    if not _os.path.isfile(p):
                        return p, None
                    return p, _mcover.extract(p)
                except Exception:                                    # noqa: BLE001
                    return p, None

            try:
                with ThreadPoolExecutor(max_workers=n_workers) as ex:
                    for p, cv in ex.map(_cover_one, todo_cover):
                        if cv:
                            rec = dict(meta.get(p) or {})
                            rec["cover"] = cv
                            meta[p] = rec
                            okc += 1
            except Exception as e:                                   # noqa: BLE001
                _log("阶段1c（补封面）异常终止：%s" % e)
            try:
                self.main.cfg.save()
            except Exception:
                pass
            _log("阶段1c（补封面）完成：%d 首待补，成功 %d 首，用时 %.1f 秒"
                 % (len(todo_cover), okc, _tm.perf_counter() - tc0))

        # ---- 阶段 1d：**补认歌字段** `fp_id` / `file_md5`（存量曲库）----
        # 只做「解码 → 指纹 → 签名 → 写 `fp_id`」与「读文件字节算 md5 → 写 `file_md5`」
        # （顺带把缺的指纹行补进库），**不重跑**联网曲风 / 本地 AI / 封面 / 节拍网格。
        # ⚠ 缺哪个补哪个：有 fp_id 只缺 file_md5 时**不重跑 STFT**（省 1.35s/首）。
        if todo_fp:
            tf0 = _tm.perf_counter()
            okf = 0

            def _fp_one(p):
                """补齐一首歌的认歌字段；返回 `(path, fp_id, file_md5)`。

                只算**缺的那个**（老快照里有的直接搬）：
                  · 缺 `file_md5` ⇒ `fpvj.file_md5(p)`（只读字节，不解码，~0.08s/首）；
                  · 缺 `fp_id`   ⇒ 解码 → 指纹 → 签名（~1.6s/首）并确保指纹行在库里。
                ⚠ 解码/声道混合必须与本文件 `_one()` **逐字一致**（整曲读、`np.mean(axis=1)`）：
                  签名是「指纹哈希序列」的 sha1，而哈希来自 PCM ⇒ 截断/重采样/单声道取法
                  只要差一点，算出来就是**另一串**，跨端比对会**静默永不命中**。
                ⚠ 任何异常自己吞掉，**已成功的那个字段照常返回**（坏文件不能打断整轮扫描，
                  也不能因为 md5 成功而把已算出的签名丢掉）。
                """
                fp_sig = str(old_fp.get(p) or "")
                fmd5 = str(old_md5.get(p) or "")
                try:
                    if not _os.path.isfile(p):
                        return p, fp_sig, fmd5
                    if not fmd5:
                        fmd5 = fpvj.file_md5(p)
                    if not fp_sig:
                        data, sr = sf.read(p, dtype="float32", always_2d=True)
                        sig = np.mean(data, axis=1)
                        hs = fpvj.fingerprint_audio(sig, sr)
                        fp_sig = fpvj.signature_from_hashes(hs)
                        if fp_sig:
                            song_id = int(hashlib.md5(
                                p.encode("utf-8")).hexdigest()[:8], 16)
                            with db_lock:
                                # 老曲库几乎**都已有指纹行**（本阶段多数只缺签名）⇒ 先查一次，
                                # 有就跳过写库（`idx_song` 索引让这条 SELECT 是常数级），
                                # 免得几千首全量重写几千万行。
                                has_rows = False
                                try:
                                    has_rows = db.conn.execute(
                                        "SELECT 1 FROM fingerprints WHERE song_id=? LIMIT 1",
                                        (song_id,)).fetchone() is not None
                                except Exception:                # noqa: BLE001
                                    has_rows = False
                                if not has_rows:
                                    db.add_song(song_id, sig, sr, commit=False, hashes=hs)
                                    if db.pending() >= 6:
                                        db.commit_batch()
                    return p, fp_sig, fmd5
                except Exception:                                    # noqa: BLE001
                    return p, fp_sig, fmd5

            try:
                with ThreadPoolExecutor(max_workers=n_workers) as ex:
                    for p, fs, md5v in ex.map(_fp_one, todo_fp):
                        got = False
                        if fs or md5v:
                            rec = dict(meta.get(p) or {})
                            if fs and not str(rec.get("fp_id") or ""):
                                rec["fp_id"] = fs
                                got = True
                            if md5v and not str(rec.get("file_md5") or ""):
                                rec["file_md5"] = md5v
                                got = True
                            if got:
                                meta[p] = rec
                                okf += 1
                        done += 1
                        self.progress.emit(done, total, p)
                        if done % 20 == 0:
                            try:
                                self.main.cfg.save()
                            except Exception:                        # noqa: BLE001
                                pass
            except Exception as e:                                   # noqa: BLE001
                _log("阶段1d（补认歌字段）异常终止：%s" % e)
            try:
                db.commit_batch()
            except Exception:                                        # noqa: BLE001
                pass
            try:
                self.main.cfg.save()
            except Exception:                                        # noqa: BLE001
                pass
            _log("阶段1d（补认歌字段）完成：%d 首待补，成功 %d 首，用时 %.1f 秒"
                 % (len(todo_fp), okf, _tm.perf_counter() - tf0))

        # ★ 2026-10-30 用户「大砍刀」：阶段 2「串行补在线曲风」整块删除
        #   （discogs / iTunes 联网查询 + 限流逻辑）。扫描不再联网。
        try:
            db.end_scan()         # 落盘 + 把 synchronous 恢复回原值
        except Exception:
            pass
        # ★ 曲库扫描结束 ⇒ **对账一次**：把"码表包里灌进来的合成指纹"里、本机曲库
        #   **现在也有了**的那几首清掉（`cfg["cue"]["fp_songs"]` ↔ `music_meta` 的 fp_id 撞车）。
        #   为什么必须在这里做：用户的真实顺序常常是「先导码表包，后来才把歌加进曲库」，
        #   那时同一段音频在指纹库里会有两份指纹（合成 id + 本机路径 id）⇒ 投票被拆成一半
        #   ⇒ 症状是**"有时候认得出、有时候认不出"**，现场最难查的一类故障。
        #   ⚠ 零重活：只比两个 dict（fp_songs 的键 / music_meta 的 fp_id 集合），
        #     不解包、不解码；没有撞车时它 0 删除、0 写盘。
        try:
            import cue_fp_pack as _cfp
            _rec = _cfp.reconcile_dupes(self.main.cfg, db)
            if _rec.get("dropped"):
                _log("码表指纹对账：清掉 %d 首与本机曲库撞车的合成指纹（%d 行）"
                     % (len(_rec.get("dropped") or []), int(_rec.get("rows", 0) or 0)))
        except Exception as _e:                                     # noqa: BLE001
            _log("码表指纹对账失败（不影响扫描结果）：%s" % _e)
        db.close()


class DupCheckThread(QThread):
    """后台跑曲库查重（铁律：主线程不做耗时活）。

    实测库里 98 首时：只读 meta 比对是瞬时的；只有「时长几乎一样」的少数几对
    才会去查指纹库（`mode=ro`，不占写锁），整轮通常 < 100ms。
    """

    found = Signal(list)

    def __init__(self, main):
        super().__init__(main)
        self.main = main

    def run(self):
        pairs = []
        try:
            cfg = self.main.cfg
            meta = cfg["music_meta"] or {}
            lib = [p for p in (cfg["music_library"] or []) if p]
            items = [(p, meta.get(p) or {}) for p in lib
                     if isinstance(meta.get(p), dict)]
            db = None
            try:
                import cue_fp_pack as _cfp
                db = _cfp.default_db_path()
            except Exception:                                      # noqa: BLE001
                db = None
            pairs = D.scan_pairs(items, db_path=db)
        except Exception:                                          # noqa: BLE001
            pairs = []
        try:
            self.found.emit(pairs)
        except Exception:                                          # noqa: BLE001
            pass


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


def _hud_grid_seg(us):
    """HUD 里「当前段 / 当前八角锚」那一段文本（T05②）。

    入参是引擎无锁快照 `ui_snapshot()`（字段 `grid_seg`，见 `engine.grid_segment_info`）。
    · 有分段 → `  |  段2 155BPM · 锚120.03`
    · 只有多锚 → `  |  多锚 140BPM · 锚28.80`
    · 无分段/多锚（老曲目）、未锁定、或字段缺失 → 返回**空串**
      ⇒ `_update_status` 拼上去等于没拼，现有 HUD 逐字不变（不影响现场既有读数）。

    为什么由 GUI 拼而不在引擎里拼：引擎不依赖 i18n（`engine.py` 不 import i18n），
    文案模板放这边才能跟着中英文切换走。
    """
    sg = (us or {}).get("grid_seg") if isinstance(us, dict) else None
    if not sg:
        return ""
    try:
        bpm = "%.0f" % float(sg.get("bpm") or 0.0)
        anc = "%.2f" % float(sg.get("anchor") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return ""
    if sg.get("kind") == "seg":
        return "  |  " + Tf("段{} {}BPM · 锚{}", str(int(sg.get("n") or 1)), bpm, anc)
    return "  |  " + Tf("多锚 {}BPM · 锚{}", bpm, anc)


# ============ 演出台 BPM 显示的「变速倍率」参数（见 `MainWindow._rate_text`） ============
# `lib_bpm` 是**原曲** BPM；DJ 变速（keylock / SOLA）后现场听到的是 `lib_bpm × r`。
# 这三个常量只服务于**显示**（HUD + 底部状态栏），不要拿去算网格/码表/素材速度。
#
# ★★★ 2026-11-03 第三轮（看了竞品 KVJ 的做法后用户定稿）：**两个数分开显示**。
#   上一版把 `lib_bpm × r` 乘成 167.5 写进 BPM 位，被推翻 —— BPM 位永远是原曲 BPM
#   （和 KVJ 时间轴上的「BPM 段」一致），变速倍率另挂一个后缀：
#       `BPM 140.0 ×1.196`    （1.0× 时退化成 `BPM 140.0`，没有后缀）
#   `_bpm_text` 只负责“原曲值 + 短时保持”，倍率全交给 `_rate_text`。
#
# 死区：`recognized_rate` 每 500ms 会微微变（±0.001 量级）。1.0× 正常播放时
#   三位小数会把噪声原样抖出来（×1.001 → ×0.998 → ×1.000），现场极显眼；
#   KVJ 的口径也是「差不到 1% 不改速度、只修位置」，所以 |r-1| 小于此值一律不显示。
#   ⚠ 死区不能太大：DJ 推子 ±5% 是常见操作，1% 以内本来就落在 `r` 的噪声里。
_BPM_RATE_DEADBAND = 0.01
# 倍率可用区间（显示层的**二次兜底**）。`audio_engine` 在发布 `recognized_rate` 之前
# 已经钳过 [0.78, 1.25]（见 `state.recognized_rate` 的注释，上界必须 ≥1.22），
# 这里再挡一次是为了防止读到别的中间态 / 旧快照。越界当「没测到」⇒ 不显示倍率，
# 宁少不多（显示一个假倍率比不显示更糟）。
_BPM_RATE_MIN = 0.78
_BPM_RATE_MAX = 1.25
# 库内 BPM 的保持时长（秒）。真机实测（`energy_log` CSV，1.20× 相位 19:11–19:13）：
#   `lib_bpm` 163 行里 93 行 = 140、**70 行 = 0**（约 43% 时间为 0），而且是**穿插**跳
#   （19:12:27 掉 0 → 19:12:28 回来 → 19:12:37 又掉），不是连续成段。
# 不保持的话同一首歌唱的同一个 BPM 会在 `140.0`（库内原曲值）/ `168`（兜底锁定）/ `--`（兜底无值）
# 三种**形态**之间反复闪（位数都不一样），现场盯着看极明显 —— 数值都对，抖的是显示形态。
#
# ★ 注意：保持的是「**原曲 BPM 这个属性**」，跟有没有变速无关。第三轮改成
#   `BPM 140.0 ×1.196` 之后，掉 0 时保持住 140.0、倍率后缀照常跟着 `recognized_rate` 走
#   （`_rate_text` 自己不做保持，见其 docstring）。
#
# ★★ 为什么是 4.5 而不是 3.0、也不是随便一个大数（2026-11-03 第三轮，QA 用真机 CSV 算出来的）：
#   · **掉落是按整数秒成段的**（真机实测段长 1 / 2 / 3 / 4 秒），而 UI 刷新是 **500ms 一次**
#     ⇒ 窗口如果正好落在某个整数秒上（3.0），"3 秒的掉 0 段"就有**一半概率**提前丢保持
#     （取决于这一 tick 落在段的哪半秒）。窗口必须**跨过**整数秒边界才稳。
#   · 稳态实测**最长连续掉 0 = 4 秒**，3.0 根本盖不住。
#   · 按 500ms tick 推演 90 秒现场：3.0 秒时形态翻转 **8 次**（140.0 ↔ 168），
#     4.5 秒时降到 **2 次** —— 剩下的 2 次就是"开始播 / 停播"各一次，本来就该变。
#   ⇒ 取 4.5 = 实测最长段(4.0) + 1 个 tick(0.5) 的余量，既不盖不住也不拖太久。
#   ⚠ 不能取太大：真停播（QA 验证过 22s / 27s 两段）必须回落，不能被保持住；
#     换歌另由 `recognized_song_id` 立即失效兜住（见 `_bpm_text`），不依赖这个窗口。
# ⚠ 计时必须用**墙钟** `time.time()`，不能用 `perf_counter()`：后者是进程内单调时钟，
#   语义上不是"多少秒没更新"，本项目已为此踩过坑（见 `color.manual_t` 的注释）。
_BPM_LIB_HOLD_SEC = 4.5


class _FloatingLibraryWindow(QWidget):
    """素材库浮动窗口：可拖动、可缩放，关闭=隐藏（不销毁）。"""

    def __init__(self, main):
        super().__init__(None, Qt.Tool | Qt.Window)
        self.main = main
        self.setWindowTitle("素材库")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMinimumSize(420, 260)

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()
        try:
            self.main._save_library_float(False)
        except Exception:
            pass

    def showEvent(self, ev):
        super().showEvent(ev)
        try:
            self.main._save_library_float(True)
        except Exception:
            pass

    def hideEvent(self, ev):
        super().hideEvent(ev)
        try:
            self.main._save_library_float(False)
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # ★ 2026-10-19：**主窗口从来没设过图标** —— 标题栏/任务栏一直用 exe 的默认图标，
        #   换了 assets/autovj.ico 也看不出来。这里显式设一次（找不到就跳过，绝不影响启动）。
        try:
            from PySide6.QtGui import QIcon
            import appicon
            _ic = appicon.icon_path("autovj")
            if _ic:
                self.setWindowIcon(QIcon(_ic))
        except Exception:                                        # noqa: BLE001
            pass
        self.setWindowTitle("Easy Reality AutoVJ")
        self.resize(1500, 900)
        self.cfg = Config()
        self._migrate_library_paths()
        set_lang(self.cfg["lang"])

        self.audio = AudioEngine()
        self.audio.energy_scale = float(self.cfg["auto"].get("energy_scale", 1.0))  # 实验性：能量伽马校正，config 里改，不占 UI
        self._song_meta_by_id = {}     # song_id -> {path,title,artist,zh}（现场识别查表 / HUD 歌名）
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
        # ★★ 2026-10-30 用户「大砍刀」：老配置里的素材标签是 CLIP 打的内容词
        #   （霓虹/激光/烟雾…）+ 亮度三档，这些词**已经没有任何地方会产出**了。
        #   不清掉的话，素材格上还会显示「霓虹·激光·烟雾」，用户会以为没砍干净。
        #   这里做一次**加载期迁移**：只留「动态三档 + 频闪 + 铺满/局部」，
        #   并把清洗结果写回 cfg（顺带删掉指向已不存在素材的条目）。
        _KEEP_TAGS = set(DYNAMIC_TAGS) | set(COVERAGE_TAGS)
        _ct = self.cfg["clip_tags"] or {}
        _cleaned = {}
        _dropped = 0
        for p, tags in _ct.items():
            m = self.library.get(p)
            if m is None and not isinstance(tags, list):
                continue
            keep = [t for t in (tags or []) if t in _KEEP_TAGS] if isinstance(tags, list) else []
            _dropped += (len(tags) if isinstance(tags, list) else 0) - len(keep)
            if keep:
                _cleaned[p] = keep
            if m is not None:
                m.tags = keep
        if _dropped:
            self.cfg["clip_tags"] = _cleaned
            try:
                self.cfg.save()
            except Exception:                                  # noqa: BLE001
                pass
            print("[素材] 迁移旧标签：清掉 %d 条已废弃的内容/亮度词" % _dropped)
        # ★★ 2026-10-31（用户：「图片不要打标」）：历史上给**静态图片**打过的
        #   动态/铺满标签全部作废 —— 截图里那两张黑图就顶着「低动态·局部」。
        #   静态图没有"动态"可言，留着只会让它在动态筛选/换池里乱入。
        #   ⚠ 只动自动标签（`clip_tags`），手动标签（`clip_tags_manual`）不碰。
        _img_cleared = 0
        for _p, _m in self.library.items():
            if _m.kind != "image":
                continue
            if _m.tags:
                _m.tags = []
                self.cfg["clip_tags"].pop(_p, None)
                _img_cleared += 1
            if _p in (self.cfg["clip_score"] or {}):
                self.cfg["clip_score"].pop(_p, None)
        if _img_cleared:
            try:
                self.cfg.save()
            except Exception:                                  # noqa: BLE001
                pass
            print("[素材] 图片不打标：清掉 %d 张图的旧动态标签" % _img_cleared)
        # ★ 2026-10-30（用户「给素材打分 0-1」）：动态分（老库没有这个键 ⇒ 全是 None，
        #   等一次「扫描素材」补齐；不影响任何现有行为）
        # ⚠ `Config` **没有 `.get()`**（项目铁律）—— `load()` 是 `deep_merge`，
        #   缺键也会有默认值，所以直接下标；写成 `.get()` 会在启动时 AttributeError，
        #   症状是「有进程没窗口 / 启动失败弹窗」（2026-10-30 实机验证抓到过一次）。
        for p, sc in (self.cfg["clip_score"] or {}).items():
            m = self.library.get(p)
            if m is not None:
                try:
                    m.score = float(sc)
                except (TypeError, ValueError):
                    pass
        # 手动标签（UI 已删）不再加载 —— 只读不写，避免影响老配置
        for p, tags in (self.cfg["clip_tags_disabled"] or {}).items():
            m = self.library.get(p)
            if m is not None and isinstance(tags, list):
                m.disabled_tags = [t for t in tags if t in _KEEP_TAGS]
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
        # ★ 2026-10-30 用户「大砍刀」：曲风→画面标签映射整条删除
        #   （`match_engine.set_custom_visual_map` / cfg["genre_visual_map"] 都不再使用）。
        self.engine = AutoVJEngine(self.cfg, self.audio.state, self.library)
        # ★ 2026-10-17：把**音频引擎本体**也给引擎（第二个参数只是它的 state）。
        #   码表自动跟拍要读 `_recognizer_engine.current_song_id`（电荷条确认后稳定的 id），
        #   `state.recognized_song_id` 会被逐拍覆写 ⇒ 只靠 state 会采到 -1 而永不接管。
        try:
            self.engine.set_audio_engine(self.audio)
        except Exception as _e:                                  # noqa: BLE001
            print("[码表] 注入音频引擎失败：%s" % _e)
        self.engine.rebuild_from_cfg()
        # ★★ 2026-11-02（用户「看一下今天00.00之后的日志 能量大部分时候貌似还是很低」）：
        #   `cue_health.log` 是**环形裁剪**的（超 120KB 只留最后一段）—— 昨晚 23:50 启动、
        #   01:14 退出，日志只剩 01:00:56 之后，**00:00–01:00 的能量曲线直接没了**，
        #   于是"那段到底低不低"根本无法回答。
        #   ⇒ 每次运行另落一个 `energy_YYYYMMDD.csv`（1 秒一行，不裁剪），
        #     字段能把"能量低"拆开：`energy` 低但 `level` 正常 = 算法问题；
        #     `level` 也是 0 = 电平被清零；`blk_delta=0` = **采集根本没喂数据**。
        #   ⚠ 纯旁路：daemon 线程，任何异常只记在自己身上，绝不影响演出。
        try:
            from energy_log import EnergyCsvLogger
            from config import app_base_dir as _abd
            self._energy_log = EnergyCsvLogger(
                self.audio.state, _abd(),
                tier_fn=lambda: getattr(self.engine, "_last_energy_tier", "-"),
                blk_fn=lambda: getattr(getattr(self.audio, "_analyzer", None), "blk", -1),
                interval=1.0)
            self._energy_log.start()
        except Exception as _e:                                  # noqa: BLE001
            self._energy_log = None
            print("[能量日志] 启动失败（不影响演出）：%s" % _e)
        # 引擎主循环移入独立线程：合成/后处理不再与 GUI 抢线程（卡顿根因修复）。
        # frame_ready 跨线程自动变 queued 投递，GUI 只做贴图。
        from PySide6.QtCore import QThread, QMetaObject
        self.engine_thread = QThread(self)
        self.engine_thread.setObjectName("engineThread")
        self.engine.moveToThread(self.engine_thread)
        try:
            # ★ 2026-11-08：**不要**给渲染线程提系统优先级。
            #   曾试过 QThread.Priority.HighPriority，但本机实测 tick 抖动的主因是
            #   显示器绘制被 0ms 定时器饿死（见 `_on_engine_frame`），不是解码线程抢 CPU。
            #   提高引擎线程优先级反而会让 GUI 重绘更容易被挤，现场更卡。
            self.engine_thread.start()
        except Exception:
            pass
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
        try:
            self.out_win.visibility_changed.connect(self._sync_frame_consumers)
        except Exception:
            pass
        # 输出窗口与预览统一走一个 slot（帧在引擎侧，这里只收序号后各自去取）
        self._drawn_seq = -1
        # ★ 2026-11-08 二修：帧回调改为**直接绘制**（见 `_on_engine_frame`）。
        #   旧注释保留在这里作历史：以前用 0ms 单发合并，反而被重绘事件饿死。
        self._pending_seq = -1
        self._draw_scheduled = False
        self._last_draw_t = 0.0
        self._out_gap_log_t = 0.0
        self._out_gap_max = 0.0
        self._out_gap_n = 0
        # 输出窗口是否完全盖住主窗（全屏同屏时预览/状态刷新纯属浪费）
        self._cover_main = False
        self._cover_check_t = 0.0
        # 预览面板中心是否被输出窗盖住（比"整窗盖住"更宽松，二屏/窗口化也适用）
        self._preview_cover = False
        self._preview_cover_t = 0.0
        # ★ 2026-11-03：库内 BPM 的**短时保持**（见 `_bpm_text`）。
        #   `lib_bpm` 真机会在 140 / 0 之间穿插跳（19:13 实测 163 行里 43% 为 0），
        #   不保持的话 HUD 会在 `167.4` / `168` / `--` 三种形态间闪。
        #   元组是 `(值, 墙钟秒, recognized_song_id)`；`(0.0, 0.0, -1)` = 还没有值。
        self._bpm_lib_hold = (0.0, 0.0, -1)
        # 自动图层素材池被重算 → 刷新对应图层行的素材格
        self.engine.auto_layers_changed.connect(self._on_auto_layers_changed)
        # 模态对话框打开时，临时解除输出窗口置顶，避免对话框被置顶输出窗口挡住导致卡死
        self._dialog_filter = _ModalDialogTopFilter(self)
        QApplication.instance().installEventFilter(self._dialog_filter)
        # ★ 2026-11-02：任务栏「关闭窗口」在模态对话框打开时也要能退出（见类注释）
        try:
            self._taskbar_close = _TaskbarCloseFilter(self)
            from PySide6.QtCore import QAbstractEventDispatcher
            _disp = QAbstractEventDispatcher.instance()
            if _disp is not None:
                _disp.installNativeEventFilter(self._taskbar_close)
        except Exception:                                        # noqa: BLE001
            self._taskbar_close = None
        # ★ 2026-11-08：标题栏拖动改为普通鼠标消息（避免 Windows 模态移动循环卡住帧派发）
        try:
            from PySide6.QtCore import QAbstractEventDispatcher
            self._smooth_drag = _SmoothWindowDragFilter()
            _disp = QAbstractEventDispatcher.instance()
            if _disp is not None:
                _disp.installNativeEventFilter(self._smooth_drag)
        except Exception:                                        # noqa: BLE001
            self._smooth_drag = None
        # 全局禁用「滚轮切换」：见 MainWindow.eventFilter（下拉框/滑条/数值框吞掉 Wheel）
        QApplication.instance().installEventFilter(self)

        # 这一段初始化会连着触发几十次配置落盘（每次 dump+fsync+备份 170KB），
        # 实测占主窗口构造时间的四成以上。用批量模式合并成一次写。
        with self.cfg.batch():
            self._build_ui()
            # 多屏输出开关：上次关闭前进过多屏模式，启动时直接恢复
            try:
                if bool((self.cfg["output"] or {}).get("multi_enabled", False)):
                    self.toggle_multi_output(True, startup=True)
            except Exception:
                pass
            self._enforce_kv_top()      # ★ 读档顺序可能把 KV 挤下去 ⇒ 启动先纠正一次
            self._build_hotkeys()
            self._load_settings_to_ui()
            self._init_recognizer()
        # ★ 启动后**延迟**把上次绑定的码表重新装载回图层（不能在构造里做：load 要走 zip + 解包）。
        #   ⚠ `is_cuesheet` / `cuesheet_id` 随图层持久化，但**表本身不持久化** ⇒ 不恢复的话，
        #     重启后图层仍是"码表图层"却没有表（`_cue_sheet is None`）：既不出画，也没有任何
        #     提示（`cue_health.log` 里 `sheet=0`）。用户会以为"码表丢了"。
        try:
            from PySide6.QtCore import QTimer as _QT
            _QT.singleShot(300, self._restore_cue_sheets)
        except Exception:                                        # noqa: BLE001
            pass

        # 「开始自动VJ」未运行时慢速呼吸闪烁（一闪一闪；周期 2.2s，亮暗主题两套色）
        self._breath_t0 = time.perf_counter()
        self._breath_timer = QTimer(self)
        self._breath_timer.setInterval(80)      # ~12fps 足够平滑且省 CPU
        self._breath_timer.timeout.connect(self._breath_tick)
        self._breath_timer.start()              # 初始未运行，开始按钮即闪烁

        # 动态标签升级：v4=三档动态+coverage v2；v5=顺序读全片修复 DXV seek 失效导致的动态误判；
        # v6=亮度分档（暗基调/中亮度/亮基调，全片中位数口径）+ 废止旧的「明亮/昏暗/中等」。
        # 启动时后台补扫一次（只跑帧差，不跑 CLIP）。悬浮提示卡让重扫进度可见，不再误以为卡死。
        if int(self.cfg["dyn_tag_ver"] or 0) < 6:
            self._dyn_scan = DynTagScanThread(self)
            self._dyn_scan.finished.connect(self._on_dyn_scan_done)
            self._scan_overlay = ScanOverlayWidget(self, "正在扫描素材库… 0/0")
            self._dyn_scan.progress.connect(self._scan_overlay.update_progress)
            self._dyn_scan.finished.connect(self._scan_overlay.finish)
            self._dyn_scan.start()
        else:
            # ★★ 2026-10-31（用户「素材库怎么不显示图片」「导入图片也没反应」）：
            #   版本已升级 ⇒ 不再全量重扫；但**从未扫过**的素材（新导入 / 早期漏掉）
            #   在「全部」档里是看不见的（用户要求「选择时就排除未扫描的」）⇒
            #   启动 2.5 秒后增量补一次，只扫没标签的（演出中会自动跳过）。
            try:
                from PySide6.QtCore import QTimer as _QT2
                _QT2.singleShot(2500, self._auto_scan_unscanned)
            except Exception:                                    # noqa: BLE001
                pass

        # 素材缺失提醒：换了目录没拷「素材库」文件夹时，画面会全黑，明确告诉用户原因
        missing = sum(1 for p in self.library if not os.path.exists(p))
        if self.library and missing == len(self.library):
            QMessageBox.warning(
                self, T("素材文件缺失"),
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
        # ★ 2026-11-08 二修：心跳从 500ms 提到 100ms，供 150ms 级掉帧取证使用。
        #   只是写一个 float，开销可忽略；3 秒级看门狗的逻辑完全不变。
        self._hb_timer.setInterval(100)
        self._hb_timer.timeout.connect(self._heartbeat)
        self._hb_timer.start()
        self._watchdog = None
        self._lag_watch = None
        try:
            import stallwatch
            self._watchdog = stallwatch.StallWatchdog(self._hb)
            self._watchdog.start()
            # ★ 2026-11-08：用户报「码表演出还是掉帧」。加一个轻量取证：
            #   主线程停顿 >150ms 时记一行「毫秒 + 引擎阶段 + 主线程栈」，
            #   下次日志能直接指出是重绘、锁、还是引擎阶段。
            self._lag_watch = stallwatch.UiLagWatchdog(
                self._hb,
                phase_fn=lambda: getattr(self.engine, "_phase", "?"),
                running_fn=lambda: bool(getattr(self.engine, "running", False)))
            self._lag_watch.start()
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

    def _layer_panels(self):
        """所有活着的图层栈（"N 份图层配置"的唯一列表来源）。

        `self.layers_panel` = **主屏那一份**（启动即存在，几何/布局代码仍按它做锚点，
        语义相当于"active/primary"）；多屏模式下其它屏各自的栈在 `_screen_panels`。
        任何要"对所有块生效"的动作都必须走 `_layers_call()` / `refresh_all_layer_blocks()`，
        不要直接调某一块的内部方法。
        """
        out = [self.layers_panel]
        for p in list(getattr(self, "_screen_panels", {}).values()):
            if p is not None and p is not self.layers_panel:
                out.append(p)
        return [p for p in out if p is not None]

    def refresh_all_layer_blocks(self):
        """**唯一**的重建入口：所有图层块按当前过滤集重建（加/删/换序图层后调它）。

        ⚠ 各块**不要**自己连 `main` 的业务信号去 rebuild —— 一律由这里统一广播，
          否则一次结构变化会触发 N 次重建（闪 + 重复分配）。
        """
        return self._layers_call("rebuild")

    def _layers_call(self, name, *args, **kwargs):
        """对**所有**图层栈调用同一个方法（一处改动 → 所有块同步）。

        ⚠ N 份面板共享同一份 `engine.layers`，所以这里只做"刷新类"广播调用，**不**
          重复连接信号（每个面板内部按行连一次 `dropped/reordered/cross_moved/menu_requested`
          → 同一个 `main` 方法）⇒ 一次点击只写一次数据，不会触发 N 次。
        """
        res = []
        for p in self._layer_panels():
            fn = getattr(p, name, None)
            if not callable(fn):
                continue
            try:
                res.append(fn(*args, **kwargs))
            except Exception:                                     # noqa: BLE001
                continue
        return res

    def _style_multi_out_button(self):
        """工具条上的「多屏输出」开关：**与 `btn_gpu` 完全同一套配色**（用户第 7 条）。

        亮着 = 多屏输出已开启（蓝底 + 蓝描边 + 浅蓝字），关掉 = 普通按钮。
        ⚠ 不要自创配色：用户要求"和左边 GPU 解码一样的蓝色描边+蓝底"。
        """
        v = theme.V
        try:
            self.btn_multi_out.setStyleSheet(
                f"QPushButton{{padding:2px 10px;border:1px solid {v('border')};"
                f"background:{v('btn')};color:{v('text')};}}"
                f"QPushButton:checked{{background:{v('sel')};color:#9fdcff;"
                f"border-color:#2f6f9f;font-weight:bold;}}")
        except Exception:                                         # noqa: BLE001
            pass

    def _style_kind_buttons(self):
        """主题感知的筛选按钮样式（注册进 theme，切换亮暗时重设；**两页都覆盖**）"""
        v = theme.V
        btns = list(self.kind_group.buttons())
        g2 = getattr(self, "kind_group2", None)
        if g2 is not None:
            btns += list(g2.buttons())
        for b in btns:
            b.setStyleSheet(
                f"QPushButton{{padding:2px 10px;border:1px solid {v('border')};"
                f"background:{v('btn')};color:{v('text')};}}"
                f"QPushButton:checked{{background:{v('sel')};color:#9fdcff;border-color:#2f6f9f;}}")

    def _style_grid(self):
        """演出台那份素材库网格的样式（全屏那份见 `_style_lib_full`）。"""
        self.grid.setStyleSheet(
            f"QListWidget{{background:{theme.V('panel2')};"
            f"border:1px solid {theme.V('border')};}}")

    # ================= 全局事件过滤 =================
    def eventFilter(self, obj, ev):
        """全局禁用「滚轮切换」（2026-10-18 用户要求：滚轮误触太烦）。

        下拉框（QComboBox）/ 滑条（QSlider）/ 数值框（QAbstractSpinBox）上的滚轮
        **一律吞掉**，避免"想滚动页面，结果改了分辨率/强度/音量"。

        ⚠ 为什么 `return True` 而不是 `ev.ignore()`：ignore 会把事件冒泡给父级 ⇒
          外面那层 QScrollArea 接着滚，等于"滚一下页面跟着跳"，比误改数值还烦。
        ⚠ 只拦这三类；QScrollArea / QListWidget / QTableWidget 的滚动完全不受影响
          （QScrollBar 是 QAbstractSlider，不是 QSlider，故意不拦）。
        ⚠ 禁用状态的控件不拦：让鼠标"路过"时父级滚动区照常滚。
        """
        if ev.type() == QEvent.Wheel:
            try:
                if isinstance(obj, (QComboBox, QSlider, QAbstractSpinBox)) and obj.isEnabled():
                    return True
            except Exception:                                  # noqa: BLE001
                pass
        # ★ S5 #8（用户第 8 条）：「选中此屏」按钮删掉后，选中靠**点块体/标题**触发
        _sid = getattr(obj, "_multi_screen_id", None)
        if _sid and ev.type() == QEvent.MouseButtonPress:
            st = getattr(self, "output_studio", None)
            if st is not None:
                try:
                    st.select_screen(_sid)
                except Exception:                              # noqa: BLE001
                    pass
            return True
        return super().eventFilter(obj, ev)

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

        # ★ 2026-11-08（用户要求）：多屏输出总开关。关闭时演出台保持原样；
        #   打开后整个演出台切到多屏输出布局（预览工作区 + 图层 + 素材库）。
        self.btn_multi_out = QPushButton("多屏输出")
        self.btn_multi_out.setCheckable(True)
        self.btn_multi_out.setMinimumHeight(32)
        self.btn_multi_out.setToolTip("打开多屏输出：上方棋盘格预览，下方保留演出台图层和素材库")
        self.btn_multi_out.toggled.connect(self.toggle_multi_output)
        tb.addWidget(self.btn_multi_out)
        # ★ 2026-11-09（用户第 7 条）：开启态要和左边「GPU 解码」一个样（蓝底蓝描边），
        #   别再靠文字后面的 "✓" 表示状态。
        self._style_multi_out_button()
        theme.register(self, self._style_multi_out_button)

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
        # ---- 右端：工具入口 ----
        # ★ 2026-09-27 收纳（用户选）：主题 / 语言 / 快捷键 / 关于 →「⋯ 更多」。
        # ★ 2026-09-28 再收纳（用户要求）：「🎵 音乐曲库 / 曲风映射」也挪去第 2 个界面
        #   「🎵 曲库 · 曲风」—— 它们属于"演出前准备"，不该占着现场工具条。
        #   现在工具条 = [开始|黑场|冻结|暂停自动|下一素材] │ [音源 电平] │ [GPU 解码] │ [⋯ 更多]

        # 主题按钮保留为**隐藏控件**：`_sync_theme_btn()` 仍要靠它同步文案，
        # ⋯ 菜单里的「主题」动作转发到它 ⇒ 行为与原来逐字一致（不是重写一遍逻辑）。
        self.btn_theme = QPushButton(T("☀ 亮色") if theme.current() == "dark" else T("🌙 暗色"), self)
        self.btn_theme.setProperty("_i18nDynamic", True)   # 随主题/语言变化，动态重建
        self.btn_theme.setToolTip("切换亮色/暗色主题")
        self.btn_theme.clicked.connect(self.toggle_theme)
        self.btn_theme.hide()

        # ★ 用户反馈（2026-09-27）：① 光一个「⋯」不知道是什么 ⇒ 写成「⋯ 更多」；
        #   ② **不能**跟「音乐曲库 / 曲风映射」并排 —— 那两个是**曲库工具**，
        #   这里是**主题 / 语言 / 快捷键 / 关于**这类设置项，放一起会被误当成同一组。
        #   所以中间加一条竖分隔线，让它单独成组、落在最右。
        more_sep = QFrame()
        more_sep.setFrameShape(QFrame.VLine)
        tb.addWidget(more_sep)
        self.btn_more = QPushButton("⋯ 更多")
        self.btn_more.setMinimumHeight(32)
        self.btn_more.setToolTip("更多：主题 / 语言 / 快捷键 / 导出日志 / 关于")
        self._more_menu = QMenu(self)
        self.act_theme = self._more_menu.addAction("")
        self.act_theme.triggered.connect(lambda: self.btn_theme.click())
        self._lang_menu = self._more_menu.addMenu(tr("language"))
        self.act_lang_zh = self._lang_menu.addAction("中文")
        self.act_lang_zh.setCheckable(True)
        self.act_lang_zh.triggered.connect(lambda: self.switch_lang(0))
        self.act_lang_en = self._lang_menu.addAction("English")
        self.act_lang_en.setCheckable(True)
        self.act_lang_en.triggered.connect(lambda: self.switch_lang(1))
        self._more_menu.addSeparator()
        self.act_hotkey = self._more_menu.addAction(tr("hotkeys"))
        self.act_hotkey.triggered.connect(self.open_hotkeys)
        # ★ 2026-10-18 用户要求：加「导出全部日志…」—— 现场出问题时不折腾用户找目录，
        #   一键把日志 + config 打成一个 zip，直接发给作者。
        self.act_export_logs = self._more_menu.addAction(T("导出全部日志…"))
        self.act_export_logs.setToolTip(T("把日志与配置文件打包成 zip，方便反馈问题"))
        self.act_export_logs.triggered.connect(self.export_logs)
        self.act_about = self._more_menu.addAction(tr("about"))
        self.act_about.setToolTip(T("版本信息、第三方组件、反馈渠道"))
        self.act_about.triggered.connect(self.open_about)
        # ★ 码表入口**只从第 4 个页签进**（用户明确：2026-10「更多里面的删了」）。
        #   原来这里有一个「码表（演出端）…」弹窗项，已删除 —— 不要再加回来。
        self.btn_more.setMenu(self._more_menu)
        tb.addWidget(self.btn_more)
        self._sync_more_menu()
        mv.addLayout(tb)

        # ---- 主区（两界面容器，见 _build_pages）----
        main_split = QSplitter(Qt.Horizontal)
        left_split = QSplitter(Qt.Vertical)

        self.layers_panel = LayerStackPanel(self)          # 红 + 黄
        left_split.addWidget(self.layers_panel)
        # ★ 2026-11-09（用户第 10 条）：多屏模式下**每块屏一个图层栈**。
        #   `layers_panel` 永远是"主屏那一份"（启动即存在，其它代码都靠它）；
        #   其余屏幕的栈按需创建，存在 `_screen_panels[sid]`，退出多屏时销毁。
        self._screen_panels = {}
        # 行高在所有屏的栈之间保持同步（在 B 块里拖行高，A 块也要变；块高跟着重算）
        try:
            self.layers_panel.row_height_changed.connect(self._on_panel_row_height)
        except Exception:                                         # noqa: BLE001
            pass

        # 演出台 = 图层 + 预览 + 素材库。
        # ★ 2026-09-28 用户要求把素材库**留在主界面**：「不然我怎么手动添加素材」——
        #   而且素材库与图层同页，才能把缩略图**直接拖进图层**（跨页拖拽在 Qt 里做不到）。
        bottom = QSplitter(Qt.Horizontal)
        self.preview_panel = PreviewPanel(self)            # 绿
        bottom.addWidget(self.preview_panel)
        self.library_panel = self._build_library()         # 蓝
        bottom.addWidget(self.library_panel)
        bottom.setSizes([560, 520])
        left_split.addWidget(bottom)
        left_split.setSizes([430, 380])
        # 保存演出台原始布局控件：多屏输出开关关闭时原样恢复
        self._stage_left_split = left_split
        self._stage_bottom = bottom
        main_split.addWidget(left_split)

        right = QWidget()
        right.setMinimumWidth(300)
        right.setMaximumWidth(430)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        # ★ 2026-11-09（用户第 11 条）：多屏模式下这块右侧设置要按"该屏边框色"描一圈，
        #   所以外面套一个 QFrame（QFrame 直接支持 stylesheet 的 border/圆角）。
        right_frame = QFrame()
        right_frame.setObjectName("stageRightFrame")
        # ★ S8 #13：右侧列收紧（304/434 → 288/410），压低"预览与设置之间的缝"
        right_frame.setMinimumWidth(288)
        right_frame.setMaximumWidth(410)
        _rf = QVBoxLayout(right_frame)
        _rf.setContentsMargins(2, 2, 2, 2)
        _rf.setSpacing(0)
        # 多屏模式的作用域提示（复制屏幕时显示"请选择原屏幕 xxx"）
        self.lbl_multi_scope = QLabel("")
        self.lbl_multi_scope.setWordWrap(True)
        self.lbl_multi_scope.setStyleSheet("color:#e0b050;font-size:11px;padding:2px 4px;")
        self.lbl_multi_scope.hide()
        rv.addWidget(self.lbl_multi_scope)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        self.settings = SettingsPanel(self)                # 白：行为模式/效果/逐拍
        right_scroll.setWidget(self.settings)
        rv.addWidget(right_scroll, 1)
        self.output_panel = OutputPanel(self)              # 右下角：输出设置
        # ★ S3 #5（用户第 5 条）：「屏幕设置」一栏 —— 选中切分屏幕时出现，
        #   和「画面效果 / 性能 / 输出设置」同级（都在右侧同一列），放该屏的 UV/源屏/分辨率。
        #   ⚠ 它和输出设置都挂在 `right_frame` 上（**不是** `right` 内部）：
        #     复制屏/切分屏要把上面那组设置整块置灰，但这块必须仍然可编辑。
        self.screen_setup_box = self._build_screen_setup_box()
        _rf.addWidget(right)
        _rf.addWidget(self.screen_setup_box, 0)
        _rf.addWidget(self.output_panel, 0)
        main_split.addWidget(right_frame)
        main_split.setSizes([1150, 360])
        # 记住这两块：多屏模式下要把它们搬进多屏页（同一个对象，不复制）
        self._stage_right = right
        self._stage_right_frame = right_frame
        self._stage_main_split = main_split
        self._build_pages(main_split)
        mv.addWidget(self.pages, 1)

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

        # ★ 播放高亮（图层里正在用的素材打金黄底）改由**独立定时器**刷新：
        #   以前它挂在 _update_preview 里（每 6 帧一次），而那条路只在**预览帧到达**时跑 ——
        #   换页 / 关预览 / 引擎不产帧时高亮就整个不动了（用户反馈过）。
        self._hl_timer = QTimer(self)
        self._hl_timer.timeout.connect(lambda: self._layers_call("refresh_playing"))
        self._hl_timer.start(1000)
        self.engine.frame_ready.connect(self._on_engine_frame)
        self.refresh_grid()
        self.refresh_all_layer_blocks()
        self._fill_tag_combo()
        self.preview_panel.chk_hud.setChecked(bool(self.cfg["ui"].get("hud", True)))
        self.preview_panel.chk_next.setChecked(bool(self.cfg["ui"].get("next_preview", True)))
        self.preview_panel._sync_overlays()
        self.preview_panel.chk_hud.toggled.connect(
            lambda s: self.engine_cfg("ui", "hud", bool(s)))
        self.preview_panel.chk_next.toggled.connect(
            lambda s: self.engine_cfg("ui", "next_preview", bool(s)))

    # ================= 两界面（演出台 / 素材·曲库·曲风）=================
    def _style_page_buttons(self):
        """两界面页签样式（主题感知；注册进 theme，切亮暗时重设）。"""
        v = theme.V
        for b in getattr(self, "_page_btns", []):
            b.setStyleSheet(
                f"QPushButton{{padding:4px 14px;border:1px solid {v('border')};"
                f"background:{v('btn')};color:{v('text')};}}"
                f"QPushButton:checked{{background:{v('sel')};color:#9fdcff;"
                f"border-color:#2f6f9f;font-weight:bold;}}")

    def _build_pages(self, show_split):
        """★ 主区拆成**两界面**（`QStackedWidget`），共用同一个引擎。

        为什么拆（用户反馈「看起来乱乱的」）：演出控制与素材整理是**两个完全不同的
        场合**（现场 / 演出前准备），混在一屏里互相挤。拆开后每页都能占满宽度，
        页签常显、一键来回切。
        """
        self.pages = QStackedWidget()

        # ---- 页签条（常显）----
        bar = QHBoxLayout()
        bar.setSpacing(0)
        self._page_group = QButtonGroup(self)
        self._page_btns = []
        for i, (txt, tip) in enumerate((
                ("🎬 演出台", "现场：预览 / 图层 / 效果 / 输出"),
                ("🗂 素材库", "全屏管理素材：搜索 / 筛选 / 导入 / 扫描素材（演出台那份原样不动）"),
                ("🎵 音乐曲库", "演出前准备：导入音乐 + 扫描分析（指纹 / 节拍网格 / 能量段落）"),
                ("🎞 码表", "演出前准备：管理 .ercue 码表（查看 / 添加 / 删除 / 定位）"))):
            b = QPushButton(T(txt))
            b.setCheckable(True)
            b.setMinimumHeight(28)
            b.setToolTip(T(tip))
            b.clicked.connect(lambda _c=False, ii=i: self.switch_page(ii))
            self._page_group.addButton(b, i)
            self._page_btns.append(b)
            bar.addWidget(b)
        # ★ 2026-11-09（用户第 1 条）：多屏控制条的**落脚点**。
        #   页签行是常显的 ⇒ 这里平时是空容器（`hide()` + 零尺寸，看不见也点不到），
        #   打开多屏输出时把 `OutputStudio.top_bar` 这一个控件搬进来（stretch 之前）。
        self._top_bar_host = QWidget()
        _host = QHBoxLayout(self._top_bar_host)
        _host.setContentsMargins(10, 0, 0, 0)
        _host.setSpacing(6)
        self._top_bar_host.hide()
        bar.addWidget(self._top_bar_host, 0)
        bar.addStretch(1)
        self._style_page_buttons()
        theme.register(self, self._style_page_buttons)
        self.main_view.layout().addLayout(bar)

        # ---- 第 1 页：演出台（图层 + 预览 + 设置 + 输出，原样）----
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(0, 0, 0, 0)
        # 演出台内部再用一个 QStackedWidget：正常布局 / 多屏输出布局
        self.perf_stack = QStackedWidget()
        self.perf_stack.addWidget(show_split)
        l1.addWidget(self.perf_stack)
        self.pages.addWidget(p1)

        # ---- 第 2 页：素材库（**全屏副本**；演出台那份原样不动）----
        self.pages.addWidget(self._build_library_page())

        # ---- 第 3 页：曲库 · 曲风（演出前准备；素材库在演出台）----
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(12, 12, 12, 12)
        l2.setSpacing(8)
        # ★ 标题行（2026-10-30 用户「大砍刀」：右上角的「曲风映射…」按钮已删除）
        head = QHBoxLayout()
        t2 = QLabel(T("🎵 音乐曲库"))
        t2.setStyleSheet("font-weight:bold;font-size:15px;color:" + theme.V("text") + ";")
        head.addWidget(t2)
        head.addStretch(1)
        l2.addLayout(head)
        self.lbl_p2_desc = QLabel(T(
            "演出前的准备工作都在这里：导入音乐、扫描分析（指纹 + 节拍网格 + 能量段落）。\n"
            "素材库在「🎬 演出台」上，演出中随时能取素材、直接拖进图层。"))
        self.lbl_p2_desc.setWordWrap(True)
        self.lbl_p2_desc.setStyleSheet("color:" + theme.V("muted") + ";")
        l2.addWidget(self.lbl_p2_desc)
        # ★ 用户要求：曲库管理**从弹窗改成整页**，左边加排序/筛选（见 MusicLibraryPanel）。
        #   ⚠ 面板的成员名与旧弹窗刻意对齐（refresh/progress/lbl/btn_scan/list）⇒
        #     主窗口里那些「扫描进度 / 刷新列表」的代码直接复用，并把 music_dialog 指过来。
        self.music_panel = MusicLibraryPanel(self)
        self.music_dialog = self.music_panel
        # ⚠ stretch=1 且**后面不能再有 addStretch**（否则面板只拿一半高度）
        l2.addWidget(self.music_panel, 1)
        self.lbl_p2_stat = QLabel("")
        self.lbl_p2_stat.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")
        l2.addWidget(self.lbl_p2_stat)
        self.pages.addWidget(p2)
        self._sync_p2_stat()

        # ---- 第 4 页：码表管理（2026-10 用户要求：与演出台/素材库/曲库并列的独立页签）----
        self.pages.addWidget(self._build_cue_page())

        try:
            idx = int(self.cfg["ui"].get("page", 0) or 0)
        except Exception:                                         # noqa: BLE001
            idx = 0
        if idx == 4:
            idx = 0            # v24 的输出页已合并成演出台开关，旧页码回演出台
        # ⚠ 上界是 3（四页：演出台 / 素材库 / 曲库·曲风 / 码表）。老配置里的值一律钳回合法范围，
        #   否则 setCurrentIndex 会切到不存在的页，界面一片空白。
        self.switch_page(idx if 0 <= idx <= 3 else 0, save=False)

    # ================= 多屏输出总开关（2026-11-08 用户要求） =================
    def _build_multi_stage(self):
        """惰性构建“多屏输出”演出台布局。

        正常演出台 = 图层 + (预览+素材库) + 右侧设置，原样不动。
        多屏输出   = 【左】输出工作区 + 下方每屏一块图层配置块
                     【右】和单屏**同一份**设置分组（图层属性/画面效果/性能 + 输出设置）
                           —— 用户第 8 条：多屏下不再显示"名称/类型/源屏幕/分辨率/边框
                           颜色/使用的图层"那张屏幕参数表。
        图层栈是**同一个对象**，只是在两个布局之间 reparent，所以编辑行为完全一致。
        """
        if getattr(self, "_multi_page", None) is not None:
            return
        import output_studio as _os
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.output_studio = _os.OutputStudio(self, compact=True)
        # 多屏模式：右侧设置的作用域跟着"选中哪块屏"走（用户第 8/11 条），
        # 下方图层块也跟着选中屏走（用户第 10 条）
        try:
            self.output_studio.selection_changed.connect(self._on_studio_selection)
        except Exception:                                         # noqa: BLE001
            pass
        # ★ 用户第 5 条：多屏模式下**不再内嵌素材库**，
        #   素材库改成独立窗口（同一个 `library_panel` 实例 reparent 过去）。
        try:
            self.output_studio.open_library.connect(self.toggle_library_float)
        except Exception:                                         # noqa: BLE001
            pass
        # ★ 用户第 10 条：下方不再是"一份共享图层栈"，改成**按屏幕数量生成 N 份
        #   图层配置块**（含主屏），每块用该屏边框色描边。
        # ★★ 2026-11-09（team-lead 定案）：改成**纵向堆叠**。
        #   横排不成立：每块要「图层头（默认 456）+ 素材格」，横排 3 块在 1760 窗口里
        #   每块只剩 ~580 ⇒ 块内还要横滚，变成三层滚动条套娃，现场没法用。
        #   纵向：每块**占满容器宽度**、高度按该块图层栈的自然高度，整区纵向滚动。
        holder = QWidget()
        # ★ S5 #10（用户第 10 条）：**左列 = 主屏（独占）**，**右列 = 各独立屏纵向排列**；
        #   复制屏/切分屏跟着**源屏**所在的列（源屏是主屏 → 左列，源屏是独立屏 → 右列）。
        self._multi_blocks_lay = QHBoxLayout(holder)
        self._multi_blocks_lay.setContentsMargins(2, 2, 2, 2)
        self._multi_blocks_lay.setSpacing(6)
        self._multi_col_left = QVBoxLayout()
        self._multi_col_left.setContentsMargins(0, 0, 0, 0)
        self._multi_col_left.setSpacing(6)
        self._multi_col_left.addStretch(1)        # 块少时贴顶
        self._multi_col_right = QVBoxLayout()
        self._multi_col_right.setContentsMargins(0, 0, 0, 0)
        self._multi_col_right.setSpacing(6)
        self._multi_col_right.addStretch(1)
        # 两个列各包一层容器控件：QHBoxLayout 里等拉伸 ⇒ 左右各占一半
        self._multi_col_left_w = QWidget()
        self._multi_col_left_w.setLayout(self._multi_col_left)
        self._multi_col_left_w.setSizePolicy(QSizePolicy.Policy.Expanding,
                                             QSizePolicy.Policy.Preferred)
        self._multi_col_right_w = QWidget()
        self._multi_col_right_w.setLayout(self._multi_col_right)
        self._multi_col_right_w.setSizePolicy(QSizePolicy.Policy.Expanding,
                                              QSizePolicy.Policy.Preferred)
        # ⚠ 两列必须**等宽**（用户对照图是左右各一半）：用 Ignored 宽度策略让 QHBoxLayout
        #   只按 stretch 分配，而不是按各自内容的 sizeHint（否则图层多的那一列会更宽）。
        for _cw in (self._multi_col_left_w, self._multi_col_right_w):
            _cw.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            _cw.setMinimumWidth(0)
        self._multi_blocks_lay.addWidget(self._multi_col_left_w, 1)
        self._multi_blocks_lay.addWidget(self._multi_col_right_w, 1)
        self._multi_blocks_scroll = QScrollArea()
        self._multi_blocks_scroll.setWidgetResizable(True)
        # 块是满宽的 ⇒ 正常窗口宽度下不该出现横向滚动条（只有在窗口被拖得极窄时才需要）
        self._multi_blocks_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._multi_blocks_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._multi_blocks_scroll.setMinimumHeight(220)
        self._multi_blocks_scroll.setWidget(holder)
        self._multi_blocks = []          # [(sid, frame, body_layout)]
        self._multi_block_sig = ()
        # ★ 预览工作区 ↔ 图层块区：**可拖的纵向 QSplitter**（和原来 `_multi_bottom` 一样
        #   的语义，块区最小高 220 不变）。默认给块区 320px，3 块时上下拖一拖就能全看到；
        #   总高度不够时块区内部纵向滚动。
        self._multi_bottom_split = QSplitter(Qt.Vertical)
        self._multi_bottom_split.setObjectName("multiStageSplit")
        self._multi_bottom_split.setChildrenCollapsible(False)
        self._multi_bottom_split.addWidget(self.output_studio)
        self._multi_bottom_split.addWidget(self._multi_blocks_scroll)
        self._multi_bottom_split.setSizes([520, 320])
        self._multi_bottom_split.setStretchFactor(0, 3)
        self._multi_bottom_split.setStretchFactor(1, 2)
        # ★ 用户手拖过分隔条 ⇒ 之后不再自动改高度（屏幕数变化时才强制重新分配）
        self._blocks_split_user_set = False
        try:
            self._multi_bottom_split.splitterMoved.connect(self._on_blocks_split_moved)
        except Exception:                                         # noqa: BLE001
            pass
        # 切配置 ⇒ 手拖标记复位（尺寸保持用户拖的那个，不立刻改）
        try:
            self.output_studio.cfg_combo.currentIndexChanged.connect(
                self._on_blocks_split_config_switch)
        except Exception:                                         # noqa: BLE001
            pass
        lay.addWidget(self._multi_bottom_split, 1)
        # 右侧：单屏那份设置分组的落脚点（`_move_panels_to_multi` 把控件搬进来）
        self._multi_right_lay = QVBoxLayout()
        self._multi_right_lay.setContentsMargins(0, 0, 0, 0)
        self._multi_right_lay.setSpacing(0)
        lay.addLayout(self._multi_right_lay, 0)
        self._multi_page = page
        self.perf_stack.addWidget(page)
        # ★ S1 #1：多屏页刚建好时，按当前运行状态决定开关能不能点
        try:
            self._sync_multi_controls()
        except Exception:                                         # noqa: BLE001
            pass

    def _on_studio_selection(self, sid=""):
        """选中屏幕变了：右侧设置换作用域 + 下方图层块重排（用户第 8/10/11 条）。"""
        try:
            self._rebuild_multi_blocks()
        except Exception as e:                                    # noqa: BLE001
            # ★ 这里以前是裸 pass：块区自动分配挂掉时完全静默（P0 BUG-1 就是这么漏的）
            self._log_ui_error("_on_studio_selection._rebuild_multi_blocks", e)
        try:
            self._sync_multi_right(sid)
        except Exception as e:                                    # noqa: BLE001
            self._log_ui_error("_on_studio_selection._sync_multi_right", e)

    # ---------- 用户第 10 条：每屏一块图层配置块 ----------
    def _panel_for_screen(self, s, cfg):
        """取/建这块屏幕**自己的**图层栈（主屏复用 `self.layers_panel`）。

        ★ 用户第 10 条原意：「有几个独立屏幕，下面就会有几个单独的图层配置」⇒
          每块屏一个**真实**的 `LayerStackPanel`，用 `layer_ids` 过滤出该屏使用的图层。
          它们绑的是同一份 `engine.layers`：在任意一块里改可见性/透明度/混合/拖素材，
          改的都是同一个图层对象，所以各块天然一致（见 `_layers_call` 的广播刷新）。
        """
        import output_model as _om
        sid = str(s.get("id") or "")
        all_ids = [str(l.lid) for l in self.engine.layers]
        used = [str(x) for x in _om.effective_layers(cfg, s, all_ids)]
        # 用满全部图层时不必过滤（None 更快、也不会因 lid 类型差异漏行）
        filt = None if len(used) >= len(all_ids) else used
        main = _om.main_screen(cfg)
        if main is not None and sid == str(main.get("id")):
            p = self.layers_panel
            p.set_layer_ids(filt)          # 内部会按需 rebuild
            self._ensure_panel_rows(p)     # 兜底：引擎图层数变了但没人重建过
            return p
        p = self._screen_panels.get(sid)
        if p is None:
            p = LayerStackPanel(self, layer_ids=filt)
            self._screen_panels[sid] = p
            p.rebuild()                     # ★ 新栈必须建一次行（否则是空面板）
            try:
                p.row_height_changed.connect(self._on_panel_row_height)
            except Exception:                                     # noqa: BLE001
                pass
            # 行高/列宽跟主屏那份对齐（`__init__` 已从 cfg 读过一次，这里兜住"本次会话改过"
            # 但还没落盘的极端情况）
            try:
                p.apply_row_height(self.layers_panel.cell_h, save=False)
            except Exception:                                     # noqa: BLE001
                pass
            try:
                p.set_header_width(self.layers_panel.header_w)
            except Exception:                                     # noqa: BLE001
                pass
        else:
            p.set_layer_ids(filt)
            self._ensure_panel_rows(p)
        return p

    @staticmethod
    def _ensure_panel_rows(p):
        """兜底：面板的行数应与"该显示的图层数"一致，不一致就重建。

        为什么需要：`set_layer_ids` 只在**过滤集合变化**时重建，而引擎图层增删由
        `refresh_all_layer_blocks()` 广播 —— 万一有代码路径直接改 `engine.layers`
        没走广播，这里能自愈，不至于出现"块里少一行/多一行"。
        """
        try:
            if len(p.rows) != len(p._visible_layers()):
                p.rebuild()
        except Exception:                                         # noqa: BLE001
            pass

    @staticmethod
    def _fit_block_height(blk, panel):
        """纵向堆叠：每块高度 = 该块图层栈的**自然高度**（随行高/行数变化）。

        算法：顶部工具条（标题 + 行高按钮 + ＋图层，约 34px）+ 上下边距 + 每行
        `行高 + 2`（行间 spacing）⇒ 夹在 190~320。
        ⚠ 用**固定高度**（不是 stretch）：这样三块各占自己需要的高度、整区纵向滚动，
          不会像横排那样每块都被压扁。
        """
        try:
            rows = len(getattr(panel, "rows", []) or []) if panel is not None else 0
            cell = int(getattr(panel, "cell_h", 58) or 58) if panel is not None else 58
        except Exception:                                          # noqa: BLE001
            rows, cell = 0, 58
        h = 36 + 8 + rows * (cell + 2)
        h = max(190, min(320, h))
        try:
            blk.setFixedHeight(int(h))
        except Exception:                                          # noqa: BLE001
            pass

    def _block_column(self, screen, cfg):
        """S5 #10：这块屏该放左列还是右列。

        左列 = 主屏那一列；右列 = 独立屏那一列。复制屏/切分屏跟着**源屏**走
        （源屏是主屏 ⇒ 左列；源屏是独立屏 ⇒ 右列；源屏本身也是 copy/split 时递归）。
        """
        import output_model as _om
        left = getattr(self, "_multi_col_left", None)
        right = getattr(self, "_multi_col_right", None)
        if left is None or right is None:
            return left or right
        s = screen or {}
        main = _om.main_screen(cfg)
        main_id = str((main or {}).get("id") or "")
        seen = set()
        cur = s
        for _ in range(8):
            t = str(cur.get("type") or "")
            cid = str(cur.get("id") or "")
            if cid in seen:
                break
            seen.add(cid)
            if t == "main":
                return left
            if t == "independent":
                return right
            src = _om.source_screen(cfg, cur)
            if not src or str(src.get("id")) == cid:
                # 没有源屏（脏数据）⇒ 主屏的跟随者归左列，其余归右列
                return left if cid == main_id else right
            cur = src
        return right

    def _screen_block_info(self, cfg, screen):
        """块头那一行：`名字 · 类型 · 分辨率`（S5 #9：挪到图层栈标题行上）。"""
        try:
            import output_model as _om
            w, h = _om.effective_size(cfg, screen)
            nm = str(screen.get("name") or screen.get("id") or "")
            t = _om.TYPE_LABELS.get(screen.get("type"), "")
            return "%s · %s · %dx%d" % (nm, t, w, h)
        except Exception:                                          # noqa: BLE001
            return str((screen or {}).get("name") or "")

    def _make_screen_block(self, s, cfg, all_ids, panel):
        """一块 = 外框(该屏边框色) + 该屏自己的图层栈（S5 #8/#9：不再有独立块头行）。

        ★ #8：块头原来的「图层…」「选中此屏」都删了 —— 「使用的图层」挪进「⋯」打开的
          屏幕设置对话框；选中改为**点块体/标题**触发。
        ★ #9：`名字·类型·分辨率` 这一行挪到图层栈标题那一行（替换掉原来那句
          "图层（越靠上显示越靠上）" 的提示）。
        """
        import output_model as _om
        sid = str(s.get("id") or "")
        is_copy = str(s.get("type")) == "copy"
        blk = QFrame()
        blk.setObjectName("multiScreenBlock")
        blk.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        blk._screen_id = sid
        blk._multi_screen_id = sid          # eventFilter：点块体 ⇒ 选中该屏
        v = QVBoxLayout(blk)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(2)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        if panel is not None:
            # ① 标题行改成"屏幕信息"（并删掉"越靠上显示越靠上"的提示）
            try:
                panel.title.setText(self._screen_block_info(cfg, s))
                panel.title.setToolTip(T("点这里选中这块屏幕"))
                panel.title._multi_screen_id = sid
                panel.title.installEventFilter(self)
                panel.title.setCursor(Qt.PointingHandCursor)
            except Exception:                                     # noqa: BLE001
                pass
            # ② 「⋯」放进图层栈自己的工具条（靠右），复制屏置灰
            btn_edit = QPushButton("⋯")
            btn_edit.setFixedWidth(26)
            btn_edit.setToolTip(T("屏幕设置：名称 / 分辨率 / 边框颜色 / 使用的图层"))
            btn_edit.setEnabled(not is_copy)
            if is_copy:
                btn_edit.setToolTip(T("复制屏幕跟随主屏，不能单独改；请选择原屏幕后再修改"))
            btn_edit.clicked.connect(
                lambda _c=False, x=sid: self.output_studio.edit_screen(x))
            try:
                bar = panel.layout().itemAt(0).layout()
                old_btn = getattr(panel, "_btn_edit", None)
                if old_btn is not None:
                    bar.removeWidget(old_btn)
                    old_btn.deleteLater()
                bar.addWidget(btn_edit)
                panel._btn_edit = btn_edit
            except Exception:                                     # noqa: BLE001
                pass
            body.addWidget(panel, 1)
        else:
            ph = QLabel(T("该屏没有可显示的图层（在「⋯ → 使用的图层」里勾选）"))
            ph.setWordWrap(True)
            ph.setStyleSheet("color:#888;font-size:11px;")
            body.addWidget(ph)
        blk._placeholder = None
        v.addLayout(body, 1)
        return blk, body

    def _toggle_screen_layer(self, screen, lid, on):
        """独立屏的"使用的图层"勾选（用户第 10 条：块内就能改，不是只读列表）。"""
        try:
            import output_model as _om
            cfg = self.output_studio.active_config()
            all_ids = [l.lid for l in self.engine.layers]
            cur = set(_om.effective_layers(cfg, screen, all_ids))
            if on:
                cur.add(lid)
            else:
                cur.discard(lid)
            screen["layers"] = [x for x in all_ids if x in cur]
            self.output_studio.apply()
        except Exception:                                         # noqa: BLE001
            pass

    # ---------- 块区高度：默认按块数自动分配 ----------
    def _on_blocks_split_moved(self, *_a):
        """用户手拖过分隔条 ⇒ 记住"别再自动改"（屏幕数变化时才会强制重排）。"""
        self._blocks_split_user_set = True

    def _on_blocks_split_config_switch(self, *_a):
        """切输出配置：复位"用户手拖过"的标记，但**不**动当前尺寸（用户拖的就保留）。"""
        self._blocks_split_user_set = False

    def _auto_size_blocks_split(self, force=False):
        """按块数自动分配「图层块区 : 预览工作区」的高度。

        ★ 用户诉求：「有几个屏下面就有几份配置」—— 打开就得看得见，不能默认只露一块半。
        规则（team-lead 定案 + 实测校准，页面高 902 / 分隔条 4px 时）：
          need = Σ块高 + 间距×(n-1) + 块区边框 + 上下边距
          · need ≤ 预算（= 页面 − 预览下限 35%）⇒ 块区 = need，**全部块完整可见、不用滚**
          · need 略超预算且块数少 ⇒ **轻微压缩块高**（≥170）让它放得下（1~2px 级，看不出来）
          · 再不行 ⇒ 块区 = 页面 62% 封顶，剩下的靠块区纵向滚动
        ⚠ 用户手拖过（`_blocks_split_user_set`）就不自动改，除非 `force=True`
          （进入多屏、屏幕数变化）。
        """
        sp = getattr(self, "_multi_bottom_split", None)
        if sp is None or not getattr(self, "_multi_blocks", None):
            return
        if getattr(self, "_blocks_split_user_set", False) and not force:
            return
        total = int(sp.height() or 0)
        if total <= 40:
            total = int(getattr(self, "_multi_page", sp).height() or 0)
        if total <= 40:
            total = 800                       # 还没布局（首次构建）时的兜底
        page_h = int(getattr(self, "_multi_page", sp).height() or total) or total
        # QSplitter 会按「总高 − 分隔条厚度」归一化 sizes ⇒ 必须先用 avail 算
        try:
            avail = max(160, total - int(sp.handleWidth()))
        except Exception:                                         # noqa: BLE001
            avail = max(160, total - 6)
        sa = self._multi_blocks_scroll
        try:
            extra = max(0, int(sa.height()) - int(sa.viewport().height())) or 2
        except Exception:                                         # noqa: BLE001
            extra = 2
        gap = 6
        # 块现在是**两列** ⇒ need = max(左列总高, 右列总高) + 间距 + 边框 + 边距
        def _col_need(lst):
            if not lst:
                return 0
            hs = [max(1, int(b.height())) for b in lst]
            return sum(hs) + gap * (len(hs) - 1)
        left_lst = [b[1] for b in self._multi_blocks
                    if getattr(b[1], "_multi_col", "right") == "left"]
        right_lst = [b[1] for b in self._multi_blocks
                     if getattr(b[1], "_multi_col", "right") == "right"]
        gap_total = extra + 4
        need = max(_col_need(left_lst), _col_need(right_lst)) + gap_total
        # 预览至少留 35% 页面高（team-lead 验收口径）
        budget = avail - int(page_h * 0.35)
        cap_hard = int(avail * 0.62)
        if need > budget:
            # ★ P0 BUG-1 修复（原代码是单列时代的：`per = ...// max(1, n)` 里 n 从未定义，
            #   且 `need = sum(hh)` 是单列求和口径）。改成**两列各自压缩**：
            #   每列的可用高 = budget − 边框/边距 − 列内间距，按列内块数均分。
            def _compress(lst):
                cnt = len(lst)
                if not cnt:
                    return
                per = (budget - gap_total - gap * (cnt - 1)) // cnt
                if per < 170:                 # 压太狠就不压，交给纵向滚动
                    return
                for b in lst:
                    h0 = max(1, int(b.height()))
                    if h0 > per:
                        try:
                            b.setFixedHeight(int(per))
                        except Exception:                     # noqa: BLE001
                            pass
            _compress(left_lst)
            _compress(right_lst)
            need = max(_col_need(left_lst), _col_need(right_lst)) + gap_total
        area = need if need <= budget else cap_hard
        # 块区最小高：1 块时就是它自己的自然高度（不硬撑 220），多块才用 220 兜底
        try:
            sa.setMinimumHeight(min(220, max(120, area)))
        except Exception:                                         # noqa: BLE001
            pass
        area = max(min(need, 220), min(area, max(160, budget)))
        try:
            sp.setSizes([max(1, avail - area), area])
        except Exception:                                         # noqa: BLE001
            pass

    def _on_panel_row_height(self, h):
        """某块（用户手动）改了行高 ⇒ 广播给其它块 + 重算每块的固定高度。

        ⚠ 用 `_rh_guard` 防环：同步时用 `save=False`，那条路径不会再 emit。
        """
        if getattr(self, "_rh_guard", False):
            return
        self._rh_guard = True
        try:
            h = int(h)
            for p in self._layer_panels():
                try:
                    if int(getattr(p, "cell_h", h)) != h:
                        p.apply_row_height(h, save=False)
                except Exception:                                 # noqa: BLE001
                    continue
            for _sid, blk, _body in getattr(self, "_multi_blocks", []):
                try:
                    ps = blk.findChildren(LayerStackPanel)
                    self._fit_block_height(blk, ps[0] if ps else None)
                except Exception:                                 # noqa: BLE001
                    continue
            # 行高变了 ⇒ 块高变了 ⇒ 块区高度重算（用户没手拖过时）
            self._auto_size_blocks_split()
        finally:
            self._rh_guard = False

    def _rebuild_multi_blocks(self, force=False):
        """按当前配置的屏幕列表生成/刷新 N 块（屏幕数量/过滤集变了才重建控件）。"""
        st = getattr(self, "output_studio", None)
        lay = getattr(self, "_multi_blocks_lay", None)
        if st is None or lay is None:
            return
        cfg = st.active_config()
        screens = list((cfg or {}).get("screens") or [])
        sig = tuple(str(s.get("id")) for s in screens)
        # 过滤集也进签名：某屏"使用的图层"勾选变了 ⇒ 必须重建它那块
        filt_sig = tuple(
            (str(s.get("id")), tuple(sorted(str(x) for x in (s.get("layers") or [])))
             if s.get("layers") is not None else None)
            for s in screens)
        sig_all = (sig, filt_sig)
        # 屏幕**数量**变了 ⇒ 必须重新自动分配块区高度（哪怕用户之前手动拖过）
        _prev = self._multi_block_sig
        _prev_n = len(_prev[0]) if (isinstance(_prev, tuple) and len(_prev) == 2) else 0
        count_changed = (_prev_n != len(sig))
        if not force and sig_all == self._multi_block_sig and self._multi_blocks:
            # 只是选中屏变了 / 数据刷新：样式刷一下，别重建控件（避免闪烁）
            self._refresh_block_styles(cfg)
            return
        self._multi_block_sig = sig_all
        # 不是"数量变了"（切配置 / 改某屏过滤集）⇒ 保留用户手拖的块区高度，
        # 但把"别自动改"的标记复位（下次数量变化仍会重新自动分配）。
        if not count_changed:
            self._blocks_split_user_set = False
        # 已经删掉的屏幕：它的图层栈要销毁（别让 N 个栈无限堆积）
        for sid in list(self._screen_panels.keys()):
            if sid not in sig:
                p = self._screen_panels.pop(sid, None)
                if p is not None:
                    try:
                        p.setParent(None)
                        p.deleteLater()
                    except Exception:                             # noqa: BLE001
                        pass
        try:
            self.layers_panel.setParent(None)      # 先摘出来，别被下面的删除带走
        except Exception:                                         # noqa: BLE001
            pass
        self._multi_blocks = []
        for col in (getattr(self, "_multi_col_left", None),
                    getattr(self, "_multi_col_right", None)):
            if col is None:
                continue
            while col.count():
                it = col.takeAt(0)      # ⚠ 连末尾那个 stretch 也一起摘掉，下面统一补回
                w = it.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        all_ids = [l.lid for l in self.engine.layers]
        for s in screens:
            panel = self._panel_for_screen(s, cfg)
            blk, body = self._make_screen_block(s, cfg, all_ids, panel)
            self._multi_blocks.append((str(s.get("id")), blk, body))
            self._fit_block_height(blk, panel)      # ★ 高度=该块自然高度
            col = self._block_column(s, cfg)
            blk._multi_col = "left" if col is getattr(self, "_multi_col_left", None) else "right"
            col.addWidget(blk, 0)                   # 0 = 不拉伸（高度由上面钉死）
        for col in (getattr(self, "_multi_col_left", None),
                    getattr(self, "_multi_col_right", None)):
            if col is not None:
                col.addStretch(1)                   # 块少时贴顶
        self._refresh_block_styles(cfg)
        # 新建的栈马上把行/高亮画出来（否则要等下一次广播）
        self._layers_call("refresh_playing")
        # ★ 块区高度按块数重新分配（数量变了强制，否则遵守用户手拖）
        self._auto_size_blocks_split(force=count_changed)

    def _refresh_block_styles(self, cfg):
        """每块描自己的边框色；选中的那块加粗 + 名字前打勾。"""
        import output_model as _om
        st = getattr(self, "output_studio", None)
        sid = str(getattr(st, "selected_id", "") or "") if st is not None else ""
        for bid, blk, _body in getattr(self, "_multi_blocks", []):
            s = _om.screen_by_id(cfg, bid) or {}
            col = _om.effective_color(cfg, s)
            sel = (bid == sid)
            try:
                blk.setStyleSheet(
                    "QFrame#multiScreenBlock{border:%dpx solid %s;border-radius:5px;"
                    "background:%s;}" % (3 if sel else 1, col, theme.V("panel")))
            except Exception:                                     # noqa: BLE001
                pass
            try:
                info = self._screen_block_info(cfg, s)
                ps = blk.findChildren(LayerStackPanel)
                if ps:
                    ps[0].title.setText(("✔ " if sel else "") + info)
            except Exception:                                     # noqa: BLE001
                pass

    def _restore_stage_panels(self):
        """把图层栈/素材库/右侧设置放回正常演出台的原始位置。"""
        # ★ 用户第 5 条：素材库可能正躺在浮动窗口里 ⇒ 先从那个布局里摘出来，
        #   否则它会被"加到第二个布局"而残留在浮动窗口（关窗后素材库就没了）。
        self._detach_library_from_float()
        # ★ 用户第 10 条：多屏模式下每块屏各有一个图层栈 ⇒ 退出时把**额外**的栈销毁，
        #   并把主屏那份的过滤清掉（单屏演出台必须显示**全部**图层）。
        for sid, p in list(getattr(self, "_screen_panels", {}).items()):
            try:
                p.setParent(None)
                p.deleteLater()
            except Exception:                                     # noqa: BLE001
                pass
        self._screen_panels = {}
        self._multi_block_sig = ()
        self._blocks_split_user_set = False     # 退出多屏：手拖标记复位
        try:
            self.layers_panel.set_layer_ids(None)
        except Exception:                                         # noqa: BLE001
            pass
        try:
            # 图层栈可能正躺在某块图层配置块的布局里 ⇒ 先从所有块里摘掉再搬走
            for _bid, _blk, body in getattr(self, "_multi_blocks", []):
                try:
                    body.removeWidget(self.layers_panel)
                except Exception:                                 # noqa: BLE001
                    pass
            self.layers_panel.setParent(None)
        except Exception:
            pass
        try:
            self._stage_left_split.insertWidget(0, self.layers_panel)
            self._stage_left_split.setSizes([430, 380])
        except Exception:
            pass
        try:
            self.library_panel.setParent(None)
            self._stage_bottom.addWidget(self.library_panel)
            self._stage_bottom.setSizes([560, 520])
        except Exception:
            pass
        # ★ 用户第 8 条：右侧设置搬回单屏那一列（同一个对象），并清掉描边/置灰
        try:
            rf = self._stage_right_frame
            rf.setParent(None)
            self._stage_main_split.insertWidget(1, rf)
            self._stage_main_split.setSizes([1150, 360])
            rf.setStyleSheet("")
            self._stage_right.setEnabled(True)
            self.lbl_multi_scope.hide()
        except Exception:                                         # noqa: BLE001
            pass
        # 多屏专属的屏幕参数面板重新打开（它是 OutputStudio 内部的）
        try:
            self.output_studio.settings.show()
        except Exception as e:                                    # noqa: BLE001
            self._log_ui_error("_restore_stage_panels.settings.show", e)

    def _detach_library_from_float(self):
        """把素材库面板从浮动窗口的布局里摘掉（不销毁、不隐藏）。"""
        win = getattr(self, "_lib_float", None)
        if win is None:
            return
        try:
            lay = win.layout()
            if lay is not None:
                lay.removeWidget(self.library_panel)
        except Exception:                                         # noqa: BLE001
            pass
        try:
            self.library_panel.setMinimumSize(0, 0)
            self.library_panel.setParent(None)
        except Exception:                                         # noqa: BLE001
            pass
        try:
            win.hide()
        except Exception:                                         # noqa: BLE001
            pass

    def _move_panels_to_multi(self):
        """切到多屏布局：图层栈 + 右侧设置分组搬进多屏页（素材库走独立窗口）。

        ⚠ 素材库**留在原处**（普通演出台那个 QSplitter 里，那块页面此刻是隐藏的），
          点「打开素材库」时才 reparent 到浮动窗口 ⇒ 这里绝不能把它 setParent(None)，
          否则它会变成一个没有父控件的顶层窗口，关窗后就"丢了"。
        """
        try:
            self.layers_panel.setParent(None)
            self._rebuild_multi_blocks(force=True)
        except Exception as e:                                    # noqa: BLE001
            self._log_ui_error("_move_panels_to_multi", e)
        # ★ 用户第 8 条：右侧改成"和单屏一样的设置分组" ⇒ 把单屏那列搬过来，
        #   同时藏掉多屏自己的屏幕参数表（名称/类型/源屏幕/分辨率/边框颜色/使用的图层）。
        try:
            self.output_studio.settings.hide()
        except Exception:                                         # noqa: BLE001
            pass
        try:
            rf = self._stage_right_frame
            rf.setParent(None)
            self._multi_right_lay.addWidget(rf)
        except Exception:                                         # noqa: BLE001
            pass
        try:
            self._sync_multi_right(getattr(self.output_studio, "selected_id", ""))
        except Exception:                                         # noqa: BLE001
            pass

    def _build_screen_setup_box(self):
        """★ S3 #5：选中「切分屏幕」时右侧出现的「屏幕设置」栏（UV / 源屏 / 分辨率）。"""
        g = QGroupBox(T("屏幕设置"))
        f = QGridLayout(g)
        f.setContentsMargins(8, 6, 8, 6)
        f.setVerticalSpacing(4)
        self.lbl_ss_source = QLabel(T("源屏幕"))
        f.addWidget(self.lbl_ss_source, 0, 0)
        self.ss_source = QComboBox()
        self.ss_source.setMinimumWidth(150)
        self.ss_source.currentIndexChanged.connect(self._on_screen_setup_changed)
        f.addWidget(self.ss_source, 0, 1)
        f.addWidget(QLabel(T("分辨率")), 1, 0)
        _row = QHBoxLayout()
        _row.setContentsMargins(0, 0, 0, 0)
        self.ss_w = QSpinBox(); self.ss_w.setRange(16, 16384); self.ss_w.setMinimumWidth(70)
        self.ss_h = QSpinBox(); self.ss_h.setRange(16, 16384); self.ss_h.setMinimumWidth(70)
        self.ss_w.valueChanged.connect(self._on_screen_setup_changed)
        self.ss_h.valueChanged.connect(self._on_screen_setup_changed)
        _row.addWidget(self.ss_w)
        _row.addWidget(QLabel("×"))
        _row.addWidget(self.ss_h)
        _row.addStretch(1)
        f.addLayout(_row, 1, 1)
        self.ss_uv = []
        for i, lab in enumerate(("左 U0", "上 V0", "右 U1", "下 V1")):
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1.0)
            sp.setDecimals(4)
            sp.setSingleStep(0.001)
            sp.setMinimumWidth(80)
            sp.valueChanged.connect(self._on_screen_setup_changed)
            self.ss_uv.append(sp)
            f.addWidget(QLabel(T(lab)), 2 + i // 2, (i % 2) * 2)
            f.addWidget(sp, 2 + i // 2, (i % 2) * 2 + 1)
        self.lbl_ss_hint = QLabel(T("切分屏幕取源屏的一块：改 UV 或直接在预览里拖裁切框。"))
        self.lbl_ss_hint.setWordWrap(True)
        self.lbl_ss_hint.setStyleSheet("color:" + theme.V("muted") + ";font-size:11px;")
        f.addWidget(self.lbl_ss_hint, 4, 0, 1, 4)
        self._ss_loading = False
        g.setVisible(False)
        return g

    def _fill_screen_setup(self, cfg, screen):
        """把选中切分屏的数据回填到「屏幕设置」栏（不触发写回）。"""
        if not hasattr(self, "ss_source"):
            return
        import output_model as _om
        self._ss_loading = True
        try:
            self.ss_source.blockSignals(True)
            self.ss_source.clear()
            for sc in (cfg or {}).get("screens") or []:
                if sc.get("type") in ("main", "independent"):
                    self.ss_source.addItem(sc.get("name") or sc.get("id"), sc.get("id"))
            idx = self.ss_source.findData((screen or {}).get("source_id"))
            self.ss_source.setCurrentIndex(max(0, idx))
            self.ss_source.blockSignals(False)
            w, h = _om.effective_size(cfg, screen)
            self.ss_w.blockSignals(True); self.ss_h.blockSignals(True)
            self.ss_w.setValue(int(w)); self.ss_h.setValue(int(h))
            self.ss_w.blockSignals(False); self.ss_h.blockSignals(False)
            uv = (screen or {}).get("uv") or [0, 0, 1, 1]
            for i, sp in enumerate(self.ss_uv):
                sp.blockSignals(True)
                sp.setValue(float(uv[i]))
                sp.blockSignals(False)
        finally:
            self._ss_loading = False

    def _on_screen_setup_changed(self, *_a):
        """「屏幕设置」栏被改动 ⇒ 写回当前切分屏并重建输出（S3 #5）。"""
        if getattr(self, "_ss_loading", False):
            return
        st = getattr(self, "output_studio", None)
        if st is None:
            return
        try:
            import output_model as _om
            cfg = st.active_config()
            s = _om.screen_by_id(cfg, getattr(st, "selected_id", ""))
            if not s or s.get("type") != "split":
                return
            s["source_id"] = self.ss_source.currentData() or s.get("source_id")
            s["width"] = max(16, int(self.ss_w.value()))
            s["height"] = max(16, int(self.ss_h.value()))
            u0 = float(self.ss_uv[0].value())
            v0 = float(self.ss_uv[1].value())
            u1 = max(u0 + 0.002, float(self.ss_uv[2].value()))
            v1 = max(v0 + 0.002, float(self.ss_uv[3].value()))
            s["uv"] = [u0, v0, u1, v1]
            st.apply()
        except Exception:                                         # noqa: BLE001
            pass

    def _sync_multi_right(self, *_a):
        """用户第 8/11 条：多屏模式右侧设置的作用域 = 当前选中的屏幕。

        · 主屏 / 独立屏 ⇒ 可编辑，外框描**该屏的边框色**
        · 复制屏       ⇒ 整组置灰，并提示"选择原屏幕 xxx 后再修改"
        · 没选中屏幕   ⇒ 不描色、整组置灰
        """
        rc = getattr(self, "_stage_right", None)
        rf = getattr(self, "_stage_right_frame", None)
        lbl = getattr(self, "lbl_multi_scope", None)
        st = getattr(self, "output_studio", None)
        if rc is None or rf is None or lbl is None or st is None:
            return
        try:
            import output_model as _om
        except Exception:                                         # noqa: BLE001
            return
        cfg = st.active_config()
        sid = str(getattr(st, "selected_id", "") or "")
        s = _om.screen_by_id(cfg, sid) if (cfg and sid) else None
        if s is None:
            rf.setStyleSheet("")
            rc.setEnabled(False)
            lbl.setText("未选中屏幕：设置不可修改（先在预览里点一块屏）")
            lbl.show()
            box0 = getattr(self, "screen_setup_box", None)
            if box0 is not None:
                box0.setVisible(False)
            return
        t = s.get("type")
        col = _om.effective_color(cfg, s)
        rf.setStyleSheet(
            "QFrame#stageRightFrame{border:2px solid %s;border-radius:5px;}" % col)
        lbl.show()
        box = getattr(self, "screen_setup_box", None)
        if t == "copy":
            src = _om.source_screen(cfg, s) or _om.main_screen(cfg) or {}
            rc.setEnabled(False)
            lbl.setText("「%s」是复制屏幕：请选择原屏幕「%s」后再修改"
                        % (s.get("name") or "复制屏幕", src.get("name") or "主屏幕"))
            if box is not None:
                box.setVisible(False)
        elif t == "split":
            src = _om.source_screen(cfg, s) or _om.main_screen(cfg) or {}
            rc.setEnabled(False)
            lbl.setText("「%s」是切分屏幕：下面「屏幕设置」里填源屏 / UV / 分辨率"
                        % (s.get("name") or "切分屏幕"))
            if box is not None:
                # 切分屏的"这块屏设置"就在这个栏里 ⇒ 上面那组演出设置置灰，它自己要能改
                self._fill_screen_setup(cfg, s)
                box.setVisible(True)
        else:
            rc.setEnabled(True)
            lbl.setText("以下设置作用于当前屏幕「%s」" % (s.get("name") or "主屏幕"))
            if box is not None:
                box.setVisible(False)

    def _host_top_bar(self, on):
        """把多屏控制条搬进页签行 / 搬回输出页（用户第 1 条）。

        页签行是**常显**的 ⇒ 普通演出台模式下这个容器必须是空的（不能冒出多屏控件）。
        """
        studio = getattr(self, "output_studio", None)
        host = getattr(self, "_top_bar_host", None)
        if studio is None or host is None:
            return
        try:
            if on:
                studio.attach_top_bar(host.layout())
                host.show()
            else:
                studio.detach_top_bar()
                host.hide()
        except Exception:                                         # noqa: BLE001
            pass

    def toggle_multi_output(self, on, startup=False):
        """打开/关闭多屏输出：整个演出台切换布局，输出引擎也切换单路/多路。"""
        on = bool(on)
        try:
            if on:
                # 首次从普通演出台进入：把旧输出设置同步到主屏幕，避免主屏输出开关/分辨率被旧值覆盖
                try:
                    import output_model as _om
                    _cfg = _om.active_config(self.cfg.data)
                    _main = _om.main_screen(_cfg) if _cfg else None
                    if _main is not None:
                        _o = self.cfg.data.setdefault("output", {})
                        _main["spout"] = bool(_o.get("spout_enabled", False))
                        _main["ndi"] = bool(_o.get("ndi_enabled", False))
                        _main["width"] = max(16, int(_o.get("width", _main.get("width", 1280)) or 1280))
                        _main["height"] = max(16, int(_o.get("height", _main.get("height", 720)) or 720))
                        _om.sync_legacy_output(self.cfg.data)
                except Exception:
                    pass
                self._build_multi_stage()
                self._move_panels_to_multi()
                self.perf_stack.setCurrentWidget(self._multi_page)
                self._host_top_bar(True)      # ★ 控制条搬进页签行（用户第 1 条）
                # ★ 进入多屏：块区高度按块数自动分配（此时才拿得到真实页面高度，
                #   所以延到事件循环下一轮再做）
                from PySide6.QtCore import QTimer as _QT
                _QT.singleShot(0, lambda: self._auto_size_blocks_split(force=True))
                try:
                    self.output_studio.reload()
                    self.output_studio.sync_engine()
                except Exception:
                    pass
                self.cfg.data.setdefault("output", {})["multi_enabled"] = True
                try:
                    self.engine.set_frame_consumers(True)
                except Exception:                                 # noqa: BLE001
                    pass
                self.lbl_status.setText(T("多屏输出已开启"))
            else:
                self._host_top_bar(False)      # ★ 控制条搬回输出页（普通演出台不显示多屏控件）
                self._restore_stage_panels()
                try:
                    self.perf_stack.setCurrentIndex(0)
                except Exception:
                    pass
                self.cfg.data.setdefault("output", {})["multi_enabled"] = False
                # 关闭多屏 → 回到原来的单画布/单路输出逻辑
                try:
                    self.engine.set_output_config(None)
                except Exception:                                 # noqa: BLE001
                    pass
                try:
                    import output_model as _om
                    _om.sync_legacy_output(self.cfg.data)
                except Exception:                                 # noqa: BLE001
                    pass
                try:
                    self._sync_frame_consumers()
                except Exception:                                 # noqa: BLE001
                    pass
                self.lbl_status.setText(T("已切换到普通演出台"))
            try:
                self.cfg.save()
            except Exception:                                     # noqa: BLE001
                pass
            # 同步按钮勾选状态（startup/程序化切换时避免递归）
            try:
                if self.btn_multi_out.isChecked() != on:
                    self.btn_multi_out.blockSignals(True)
                    self.btn_multi_out.setChecked(on)
                    self.btn_multi_out.blockSignals(False)
                self.btn_multi_out.setText("多屏输出")
                self.btn_multi_out.setToolTip(
                    "关闭：普通演出台（原图层/预览/素材库）\n"
                    "打开：多屏输出工作区 + 同一套图层和素材库")
            except Exception:
                pass
        except Exception as e:                                    # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                QMessageBox.warning(self, "多屏输出", "切换失败：%s" % e)
            except Exception:
                pass

    def _build_cue_page(self):
        """第 4 页：码表管理（列表 + 添加/删除/定位/刷新 + 双击详情 + 右键绑图层）。

        ⚠ 面板在 `src/cue_page.py`：主窗已经 5 千行，不再往这里堆；
          失败必须可见（不许静默把界面变成白板）。
        """
        try:
            import cue_page
            self.cue_panel = cue_page.CueSheetPanel(self)
        except Exception as e:                                    # noqa: BLE001
            w = QWidget()
            lv = QVBoxLayout(w)
            lv.setContentsMargins(12, 12, 12, 12)
            lv.addWidget(QLabel(Tf("码表页加载失败：{}", "%s: %s"
                                   % (type(e).__name__, e))))
            lv.addStretch(1)
            self.cue_panel = w
        return self.cue_panel

    def _build_library_page(self):
        """第 2 页：**全屏素材库**（演出台那份原样不动，这里是复制出来的一份）。

        ★ 用户要求：页签做成「演出台 / 素材库 / 曲库·曲风」，演出台的素材库不要动，
          在素材库页**再复制一个出来**，相当于有个能全屏操作的地方。

        ⚠ 两页**共用同一套筛选状态**（类型/角色/标签/搜索词）：任何一个控件改了都会
          同步到另一页（`_sync_lib_controls`），否则两页显示不一致会更迷惑。
        ⚠ 跨页拖拽在 Qt 里要求两端同时可见 ⇒ **拖素材进图层仍然只能在演出台做**；
          这一页给的是：搜索 / 筛选 / 导入 / 双击播放到当前图层 / 右键菜单。
        """
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        t = QLabel(T("🗂 素材库"))
        t.setStyleSheet("font-weight:bold;font-size:15px;color:" + theme.V("text") + ";")
        lay.addWidget(t)
        self.lbl_lib2_desc = QLabel(T(
            "这里是全屏版素材库，和「🎬 演出台」上那份是同一批素材（那份保持原样）。\n"
            "搜索 / 筛选 / 导入 / 右键扫描素材都在这里做；双击缩略图 = 播放到当前图层。\n"
            "⚠ 「拖进图层」只能在演出台做（跨页面拖拽系统不允许）。"))
        self.lbl_lib2_desc.setWordWrap(True)
        self.lbl_lib2_desc.setStyleSheet("color:" + theme.V("muted") + ";")
        lay.addWidget(self.lbl_lib2_desc)

        # ---- 筛选条（与演出台同一套状态）----
        fbar = QHBoxLayout()
        self.kind_group2 = QButtonGroup(self)
        for i, name in enumerate(("全部", "图片", "视频")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setFixedHeight(24)
            b.clicked.connect(lambda _c=False, ii=i: self.set_kind_filter(ii))
            self.kind_group2.addButton(b, i)
            fbar.addWidget(b)
        # ★ 2026-10-30 用户「大砍刀」：标签筛选按钮删除后，**动态筛选必须留着**
        #   （用户第 1 时间就问「素材的动态选择过滤怎么没了」—— 动态扫描留着就是为了筛）。
        #   这里给一个专用下拉：全部 / 低动态 / 中动态 / 高动态 / 频闪。
        fbar.addWidget(QLabel("动态"))
        self.dyn_combo2 = QComboBox()
        self.dyn_combo2.addItems(["全部", DYNAMIC_LOW, DYNAMIC_MID,
                                 DYNAMIC_HIGH, DYNAMIC_FLICKER, "已排除", "未扫描"])
        self.dyn_combo2.setToolTip(_FILTER_TIP)
        self.dyn_combo2.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.dyn_combo2.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.dyn_combo2.currentIndexChanged.connect(self.set_dyn_filter)
        fbar.addWidget(self.dyn_combo2, 0)
        fbar.addWidget(QLabel("角色"))
        self.role_combo2 = QComboBox()
        self.role_combo2.addItems(["全部", "前景", "背景"])
        self.role_combo2.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.role_combo2.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.role_combo2.currentIndexChanged.connect(self.set_role_filter)
        fbar.addWidget(self.role_combo2, 0)
        fbar.addWidget(QLabel("数量:"))
        self.lbl_lib_count2 = QLabel("0")
        fbar.addWidget(self.lbl_lib_count2)
        fbar.addStretch(1)      # 同上：紧凑左对齐，别把下拉拉到满屏宽
        lay.addLayout(fbar)

        # ---- 搜索 + 导入 + 打标 ----
        sbar = QHBoxLayout()
        self.search_box2 = QLineEdit()
        self.search_box2.setPlaceholderText("搜索文件名 / 标签")
        self.search_box2.setClearButtonEnabled(True)
        # ★ 2026-10-19：改走**防抖**（每敲一键重建整表，千级素材会顿）
        self.search_box2.textChanged.connect(
            lambda t, s=None: self._on_search_debounced(t, self.search_box2))
        sbar.addWidget(self.search_box2, 1)
        b1 = QPushButton(tr("import_folder"))
        b1.clicked.connect(self.import_folder)
        b2 = QPushButton(tr("import_files"))
        b2.clicked.connect(self.import_files)
        b3 = QPushButton("扫描素材")
        b3.setToolTip("用本地视觉模型为素材自动打内容标签（后台、可续扫）")
        b3.clicked.connect(lambda: self.scan_tags())
        for b in (b1, b2, b3):
            sbar.addWidget(b)
        lay.addLayout(sbar)

        # ---- 大网格 ----
        self.grid_full = LibraryGrid(self)
        self.grid_full.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid_full.remove_from_layer.connect(self.remove_from_all_layers)
        self.grid_full.files_dropped.connect(self._import_paths)
        self.grid_full.apply_thumb_size(int(self.cfg["ui"].get("thumb_size", 1)))
        lay.addWidget(self.grid_full, 1)
        self._lib_full_dirty = True
        self._style_lib_full()
        theme.register(self, self._style_lib_full)
        return w

    def _style_lib_full(self):
        """全屏素材库网格的样式（主题感知）。"""
        try:
            self.grid_full.setStyleSheet(
                f"QListWidget{{background:{theme.V('panel2')};"
                f"border:1px solid {theme.V('border')};}}")
        except Exception:                                         # noqa: BLE001
            pass

    def _on_search_debounced(self, text, src):
        """搜索防抖：180ms 内连续输入只真正刷新一次。

        旧行为是 `textChanged` 直连 `_on_search_changed` —— 每敲一个键就重建整张表
        （千级素材时每次几十毫秒），连着敲五个字就卡五下。
        失败时**降级直达**（宁可慢，也不能把搜索搞坏）。
        """
        try:
            self._search_pending = (text, src)
            t = getattr(self, "_search_timer", None)
            if t is None:
                from PySide6.QtCore import QTimer
                t = QTimer(self)
                t.setSingleShot(True)
                t.setInterval(180)
                t.timeout.connect(self._apply_search_pending)
                self._search_timer = t
            t.start()
        except Exception:                                        # noqa: BLE001
            self._on_search_changed(text, src)

    def _apply_search_pending(self):
        """防抖到期：把最后一次输入真正应用下去。"""
        p = getattr(self, "_search_pending", None)
        if not p:
            return
        self._search_pending = None
        self._on_search_changed(p[0], p[1])

    def _on_search_changed(self, text=None, src=None):
        """任一页的搜索框改动 ⇒ 写进共享状态、同步另一页、刷新两份网格。

        ⚠ 别用 `hasFocus()` 判断"是谁改的"：两个框都没焦点时（程序化 setText、失焦、
          离屏测试）就永远不会更新 `lib_kw` ⇒ 搜索失效（实测踩到）。这里由信号**显式
          把来源控件传进来**（`textChanged` 连接处带 `src=`）。
        """
        if src is not None:
            self.lib_kw = text or ""
        else:
            for ed in (getattr(self, "search_box", None),
                       getattr(self, "search_box2", None)):
                if ed is not None and ed.text() != getattr(self, "lib_kw", ""):
                    self.lib_kw = ed.text() or ""
                    break
        self.refresh_grid()

    def _sync_lib_controls(self):
        """把共享的筛选状态镜像到**两页**的控件上（避免两页显示不一致）。"""
        try:
            kf = int(self.kind_filter)
            rf = int(self.role_filter)
            for grp in (getattr(self, "kind_group", None), getattr(self, "kind_group2", None)):
                if grp is None:
                    continue
                for i, b in enumerate(grp.buttons()):
                    b.blockSignals(True)
                    b.setChecked(i == kf)
                    b.blockSignals(False)
            for cb in (getattr(self, "role_combo", None), getattr(self, "role_combo2", None)):
                if cb is None:
                    continue
                cb.blockSignals(True)
                cb.setCurrentIndex(max(0, min(cb.count() - 1, rf)))
                cb.blockSignals(False)
            # 动态筛选：两页共用同一个 `dyn_filter`（0~4 对应 全部/低/中/高/频闪）
            df = int(getattr(self, "dyn_filter", 0) or 0)
            for cb in (getattr(self, "dyn_combo", None), getattr(self, "dyn_combo2", None)):
                if cb is None:
                    continue
                cb.blockSignals(True)
                cb.setCurrentIndex(max(0, min(cb.count() - 1, df)))
                cb.blockSignals(False)
            kw = getattr(self, "lib_kw", "")
            for ed in (getattr(self, "search_box", None), getattr(self, "search_box2", None)):
                if ed is None or ed.text() == kw:
                    continue
                ed.blockSignals(True)
                ed.setText(kw)
                ed.blockSignals(False)
        except Exception:                                         # noqa: BLE001
            pass

    def _sync_p2_stat(self):
        """第 2 页的曲库概况（只读一行）：已导入多少首、其中多少首有节拍网格。"""
        try:
            # ⚠ Config 没有 .get()（只有 __getitem__）—— 记过的坑
            lib = list(self.cfg["music_library"] or [])
            meta = self.cfg["music_meta"] or {}
            with_grid = sum(1 for p in lib
                            if isinstance(meta.get(p), dict) and meta[p].get("grid"))
            self.lbl_p2_stat.setText(Tf(
                "曲库：已导入 {} 首；其中 {} 首已有节拍网格（八拍乐句对齐要用它）",
                len(lib), with_grid))
        except Exception:                                         # noqa: BLE001
            pass

    def switch_page(self, i, save=True):
        """切到页面之一（页签条走这里；码表页也支持外部跳转）。"""
        try:
            i = max(0, min(3, int(i)))
        except Exception:                                         # noqa: BLE001
            i = 0
        self.pages.setCurrentIndex(i)
        # ⚠ 全屏素材库那份是"不可见就不填充、先标脏"的（见 refresh_grid）⇒ 切过来时补上
        if i == 1 and getattr(self, "_lib_full_dirty", False):
            try:
                self._populate_grid(self.grid_full, getattr(self, "lbl_lib_count2", None))
                self._lib_full_dirty = False
            except Exception:                                     # noqa: BLE001
                pass
        if getattr(self, "grid_full", None) is not None:
            self._sync_lib_controls()
        # ★ 码表页：切过来才去算「片段」列（要全量读包，启动路径绝不能碰）
        if i == 3:
            panel = getattr(self, "cue_panel", None)
            for fn_name in ("refresh", "ensure_frag_counts"):
                fn = getattr(panel, fn_name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:                             # noqa: BLE001
                        pass
        # ★ 离开输出页（不再有独立输出页）时按预览/输出窗口实际可见性恢复帧消费者
        try:
            self._sync_frame_consumers()
        except Exception:                                         # noqa: BLE001
            pass
        for k, b in enumerate(getattr(self, "_page_btns", [])):
            b.setChecked(k == i)
        if save:
            try:
                if int(self.cfg["ui"].get("page", -1) or -1) != i:
                    self.cfg["ui"]["page"] = i
                    self.cfg.save()
            except Exception:                                     # noqa: BLE001
                pass

    # ================= 浮动素材库 + 收藏夹（2026-11-08） =================
    def _setup_library_float(self):
        """把演出台底部的素材库搬进一个可拖动浮动窗口，并在顶部加收藏夹栏。"""
        if getattr(self, "_lib_float", None) is not None:
            return
        win = _FloatingLibraryWindow(self)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        bar.addWidget(QLabel("收藏夹"))
        self.fav_combo = QComboBox()
        self.fav_combo.setMinimumWidth(130)
        self.fav_combo.currentIndexChanged.connect(self._on_favorite_changed)
        bar.addWidget(self.fav_combo, 1)
        for text, cb, tip in (
                ("＋新建", self.new_favorite, "新建收藏夹"),
                ("重命名", self.rename_favorite, "重命名当前收藏夹"),
                ("删除", self.delete_favorite, "删除当前收藏夹（不删素材）"),
                ("加入选中", self.add_to_favorite, "把素材库中选中的素材加入当前收藏夹"),
                ("移除", self.remove_from_favorite, "把选中素材从当前收藏夹移除")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(cb)
            bar.addWidget(b)
        self.chk_fav_only = QCheckBox("只看收藏夹")
        self.chk_fav_only.setToolTip("开启后素材库只显示当前收藏夹里的素材；按演出切换收藏夹即可换一套素材")
        self.chk_fav_only.toggled.connect(self._apply_favorite_filter)
        bar.addWidget(self.chk_fav_only)
        lay.addLayout(bar)
        # 把演出台底部那份素材库面板搬进来（同一个对象，筛选状态/拖拽都保留）
        try:
            self.library_panel.setParent(None)
        except Exception:
            pass
        # ★ S7 #12（用户第 12 条）：reparent 之后素材库内容区会塌（控件挤成一行、网格几乎空白）。
        #   根因是它离开 QSplitter 后没有任何尺寸约束：
        #   ① 进度条（thumb_bar）还留着 640×480 的旧尺寸，横在上面把行压扁；
        #   ② 面板 min size 被 `_detach_library_from_float` 清成 0×0，布局可以压到很扁。
        #   这里把它恢复成"和演出台一致"：进度条固定矮、面板给最小尺寸、网格保证最小高度。
        self.library_panel.setMinimumSize(380, 240)
        try:
            tb = getattr(self.library_panel, "thumb_bar", None)
            if tb is None:
                tb = getattr(self, "thumb_bar", None)
            if tb is not None:
                tb.setMaximumHeight(14)
                tb.setMinimumHeight(0)
                tb.hide()
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self.grid.setMinimumHeight(240)
        except Exception:                                          # noqa: BLE001
            pass
        lay.addWidget(self.library_panel, 1)
        self._lib_float = win
        geo = (self.cfg["ui"] or {}).get("library_float")
        try:
            if isinstance(geo, (list, tuple)) and len(geo) == 4:
                win.setGeometry(int(geo[0]), int(geo[1]), max(420, int(geo[2])),
                                max(260, int(geo[3])))
            else:
                win.resize(620, 460)
        except Exception:
            win.resize(620, 460)
        self._refresh_favorites_ui()
        try:
            self.btn_library_float.setCheckable(True)
        except Exception:
            pass
        vis = bool((self.cfg["ui"] or {}).get("library_float_visible", False))
        if vis:
            win.show()
        # reparent 后必须显式让布局重算一次（否则控件保持旧几何 ⇒「挤成一行」）
        try:
            self.library_panel.updateGeometry()
            if self.library_panel.layout() is not None:
                self.library_panel.layout().activate()
            win.layout().activate()
            win.resize(max(win.width(), 700), max(win.height(), 520))
        except Exception:                                          # noqa: BLE001
            pass
        self._sync_library_float_btn()

    def _sync_library_float_btn(self):
        try:
            win = getattr(self, "_lib_float", None)
            vis = bool(win is not None and win.isVisible())
            self.btn_library_float.blockSignals(True)
            self.btn_library_float.setChecked(vis)
            self.btn_library_float.blockSignals(False)
            self.btn_library_float.setText("📚 素材库" if not vis else "📚 隐藏素材库")
        except Exception:
            pass

    def toggle_library_float(self):
        try:
            if getattr(self, "_lib_float", None) is None:
                self._setup_library_float()
            win = self._lib_float
            if win.isVisible():
                win.hide()
            else:
                win.show()
                win.raise_()
                win.activateWindow()
                # ★ S7 #12：每次重新打开都重算一次布局（防止沿用旧几何挤成一行）
                try:
                    self.library_panel.updateGeometry()
                    if self.library_panel.layout() is not None:
                        self.library_panel.layout().activate()
                    win.layout().activate()
                except Exception:                                  # noqa: BLE001
                    pass
            self._sync_library_float_btn()
        except Exception:
            pass

    def _save_library_float(self, visible=None):
        try:
            win = getattr(self, "_lib_float", None)
            if win is None:
                return
            ui = self.cfg.data.setdefault("ui", {})
            g = win.geometry()
            ui["library_float"] = [int(g.x()), int(g.y()), int(g.width()), int(g.height())]
            ui["library_float_visible"] = bool(win.isVisible() if visible is None else visible)
            self.cfg.save()
            self._sync_library_float_btn()
        except Exception:
            pass

    def _favorites(self):
        ui = self.cfg.data.setdefault("ui", {})
        favs = ui.setdefault("favorites", [])
        if not isinstance(favs, list):
            favs = ui["favorites"] = []
        return favs

    def _active_favorite(self):
        favs = self._favorites()
        aid = str(((self.cfg.data.get("ui") or {}).get("active_favorite") or ""))
        for f in favs:
            if f.get("id") == aid:
                return f
        return None

    def _refresh_favorites_ui(self):
        try:
            favs = self._favorites()
            aid = str(((self.cfg.data.get("ui") or {}).get("active_favorite") or ""))
            self.fav_combo.blockSignals(True)
            self.fav_combo.clear()
            if not favs:
                self.fav_combo.addItem("（还没有收藏夹）", "")
            for f in favs:
                self.fav_combo.addItem(f.get("name") or "收藏夹", f.get("id"))
            idx = self.fav_combo.findData(aid)
            self.fav_combo.setCurrentIndex(max(0, idx))
            self.fav_combo.blockSignals(False)
            self.chk_fav_only.blockSignals(True)
            self.chk_fav_only.setChecked(bool((self.cfg.data.get("ui") or {}).get("favorite_filter", False)))
            self.chk_fav_only.blockSignals(False)
            self._apply_favorite_filter()
        except Exception:
            pass

    def new_favorite(self):
        name, ok = QInputDialog.getText(self, "新建收藏夹", "名称：", text="演出")
        if not ok or not name.strip():
            return
        favs = self._favorites()
        fid = "fav_%d" % (int(time.time() * 1000) & 0x7fffffff)
        favs.append({"id": fid, "name": name.strip(), "clips": []})
        self.cfg.data.setdefault("ui", {})["active_favorite"] = fid
        self.cfg.save()
        self._refresh_favorites_ui()

    def rename_favorite(self):
        f = self._active_favorite()
        if not f:
            return
        name, ok = QInputDialog.getText(self, "重命名收藏夹", "名称：", text=f.get("name") or "")
        if ok and name.strip():
            f["name"] = name.strip()
            self.cfg.save()
            self._refresh_favorites_ui()

    def delete_favorite(self):
        f = self._active_favorite()
        if not f:
            return
        if QMessageBox.question(self, "删除收藏夹",
                                "确定删除「%s」？只删收藏夹，不删素材。" % f.get("name")) != QMessageBox.Yes:
            return
        ui = self.cfg.data.setdefault("ui", {})
        ui["favorites"] = [x for x in self._favorites() if x.get("id") != f.get("id")]
        ui["active_favorite"] = ""
        self.cfg.save()
        self._refresh_favorites_ui()

    def _selected_clip_paths(self):
        paths = []
        for grid in (getattr(self, "grid", None), getattr(self, "grid_full", None)):
            if grid is None:
                continue
            for li in grid.selectedItems():
                it = li.data(Qt.UserRole)
                p = getattr(it, "path", "")
                if p and p not in paths:
                    paths.append(p)
        if not paths:
            # 没有多选时，取当前素材格
            grid = getattr(self, "grid", None)
            li = grid.currentItem() if grid is not None else None
            if li is not None:
                it = li.data(Qt.UserRole)
                p = getattr(it, "path", "")
                if p:
                    paths.append(p)
        return paths

    def add_to_favorite(self):
        f = self._active_favorite()
        if not f:
            self.new_favorite()
            f = self._active_favorite()
        if not f:
            return
        paths = self._selected_clip_paths()
        if not paths:
            QMessageBox.information(self, "加入收藏夹", "先在素材库里选中素材，再点「加入选中」。")
            return
        clips = set(f.get("clips") or [])
        clips.update(paths)
        f["clips"] = list(clips)
        self.cfg.save()
        self._apply_favorite_filter()
        self.statusBar().showMessage("已加入收藏夹「%s」：%d 个素材" % (f.get("name"), len(paths)), 4000)

    def remove_from_favorite(self):
        f = self._active_favorite()
        if not f:
            return
        paths = set(self._selected_clip_paths())
        if not paths:
            return
        f["clips"] = [p for p in (f.get("clips") or []) if p not in paths]
        self.cfg.save()
        self._apply_favorite_filter()

    def _on_favorite_changed(self, idx):
        try:
            fid = self.fav_combo.itemData(idx) or ""
            self.cfg.data.setdefault("ui", {})["active_favorite"] = fid
            self.cfg.save()
            self._apply_favorite_filter()
        except Exception:
            pass

    def _apply_favorite_filter(self, *_):
        try:
            f = self._active_favorite()
            only = bool(self.chk_fav_only.isChecked()) if hasattr(self, "chk_fav_only") else False
            self.cfg.data.setdefault("ui", {})["favorite_filter"] = bool(only)
            self.lib_favorite_paths = set(f.get("clips") or []) if (only and f) else None
            self.cfg.save()
            self.refresh_grid()
        except Exception:
            pass

    def _build_library(self):
        """蓝色区：素材库（标签 + 导入 + 网格）"""
        self.lbl_lib_count = QLabel("0")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)
        self.kind_filter = 0   # 0 全部 / 1 图片 / 2 视频
        self.role_filter = 0   # 0 全部 / 1 前景 / 2 背景
        # 动态筛选：0 全部 / 1 低动态 / 2 中动态 / 3 高动态 / 4 频闪
        # （2026-10-30「大砍刀」后只剩动态/频闪/铺满度三组标签，动态筛选是最常用的一维）
        self.dyn_filter = 0
        self.lib_kw = ""       # 搜索词（两页共用，见 _build_library_page）
        bar = QHBoxLayout()
        t = QLabel("素材库")
        t.setStyleSheet("color:" + theme.V("muted") + ";font-weight:bold;")
        bar.addWidget(t)
        # ★ 2026-10-30：标签筛选按钮删除（同上），但**动态筛选保留**
        self.tag_selected = set()      # 保留字段：老配置/其它调用点仍会读它
        bar.addWidget(QLabel("动态"))
        self.dyn_combo = QComboBox()
        self.dyn_combo.addItems(["全部", DYNAMIC_LOW, DYNAMIC_MID,
                                DYNAMIC_HIGH, DYNAMIC_FLICKER, "已排除", "未扫描"])
        self.dyn_combo.setToolTip(_FILTER_TIP)
        self.dyn_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.dyn_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.dyn_combo.currentIndexChanged.connect(self.set_dyn_filter)
        bar.addWidget(self.dyn_combo, 0)
        bar.addWidget(QLabel("角色"))
        self.role_combo = QComboBox()
        self.role_combo.addItems(["全部", "前景", "背景"])
        self.role_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.role_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.role_combo.currentIndexChanged.connect(self.set_role_filter)
        bar.addWidget(self.role_combo, 0)
        bar.addWidget(QLabel("数量:"))
        bar.addWidget(self.lbl_lib_count)
        # ★ 2026-10-30 用户「分的太开了」：QComboBox 默认 Expanding 会把整行撑满，
        #   两个下拉一放就变成"标签和下拉之间隔着半个屏幕"。下拉固定宽度 + 末尾 stretch
        #   ⇒ 一排控件紧凑左对齐，右边留白。
        bar.addStretch(1)
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
        # ⚠ 两页共用一个搜索词（`lib_kw`）；同步逻辑见 _on_search_changed/_sync_lib_controls
        self.search_box.textChanged.connect(
            lambda t, s=None: self._on_search_debounced(t, self.search_box))
        fbar.addWidget(self.search_box, 1)
        self.btn_scan = QPushButton("扫描素材")
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
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
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
    def _heal_fp_ids_bg(self, db):
        """把「旧路径 id」的指纹改挂到当前 id（仅在有确凿证据时）。"""
        try:
            import hashlib as _hl
            import soundfile as _sf
            import numpy as _np
            allow = self._allowed_song_ids()
            try:
                rows = db.conn.execute(
                    "SELECT song_id, COUNT(*) FROM fingerprints GROUP BY song_id "
                    "HAVING COUNT(*)>1000").fetchall()
            except Exception:                                    # noqa: BLE001
                return
            have = {int(s) for (s, _n) in rows}
            orphans = sorted(have - allow)
            if not orphans or len(orphans) > 8:
                return
            try:
                lib = list(self.cfg["music_library"] or [])
            except Exception:                                    # noqa: BLE001
                return
            fixed = 0
            for path in lib:
                try:
                    sid = int(_hl.md5(str(path).encode("utf-8")).hexdigest()[:8], 16)
                except Exception:                                # noqa: BLE001
                    continue
                if sid in have or not os.path.isfile(path):
                    continue
                try:
                    info = _sf.info(path)
                    d, sr = _sf.read(path, dtype="float32", always_2d=True,
                                     start=int(30 * info.samplerate),
                                     frames=int(6 * info.samplerate))
                    d = d.mean(axis=1)
                    if sr != 48000:
                        n = int(len(d) * 48000 / sr)
                        d = _np.interp(_np.linspace(0, len(d) - 1, n),
                                       _np.arange(len(d)), d).astype(_np.float32)
                except Exception:                                # noqa: BLE001
                    continue
                for old in list(orphans):
                    try:
                        db.set_allowed({old})
                        _s, votes, off = db.match(d, 48000)
                    except Exception:                            # noqa: BLE001
                        continue
                    if votes >= 100 and abs(float(off) - 30.0) < 2.0:
                        try:
                            db.conn.execute(
                                "UPDATE fingerprints SET song_id=? WHERE song_id=?",
                                (sid, old))
                            db.conn.commit()
                            fixed += 1
                            orphans.remove(old)
                            print("[识别] 指纹 id 自愈：%s ｜ %d → %d（票数 %d）"
                                  % (os.path.basename(str(path))[:40], old, sid, votes))
                        except Exception as e:                   # noqa: BLE001
                            print("[识别] 指纹 id 自愈写入失败：%s" % e)
                        break
            if fixed:
                print("[识别] 指纹 id 自愈完成：%d 首" % fixed)
        except Exception as e:                                   # noqa: BLE001
            print("[识别] 指纹 id 自愈异常：%s" % e)
        finally:
            try:
                db.set_allowed(self._allowed_song_ids())
            except Exception:                                    # noqa: BLE001
                pass

    def _lib_bpm_of(self, sid):
        """`song_id → 库内 BPM`（供识别线程做 BPM 一致性校验）。

        ⚠ 本方法在**识别线程**里被逐窗调用 ⇒ 必须无锁、无 IO、只读：
          · 只读 `engine._song_grid_lookup`（`set_song_grids` 整体替换引用，GIL 下原子）；
          · 拿不到（没网格 / sid 不在库里）返回 0.0 ⇒ 调用方跳过校验，不误杀。
        """
        try:
            g = (getattr(self.engine, "_song_grid_lookup", None) or {}).get(int(sid)) or {}
            b = float(g.get("bpm") or 0.0)
            if b > 0.0:
                return b
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 2026-10-27：**码表包内嵌曲**（本机没有音频文件 ⇒ 没有 grid）走 `fp_songs.bpm`。
        #   没有这一条，C 组那种「包内曲目被误匹配」的场景 BPM 校验永远拿不到库内 BPM
        #   （实测 `brel=none`，判据等于关着）。
        #   缓存策略：按 `fp_songs` 条数失效（导新包条数必变）⇒ 不用每窗都重建字典。
        try:
            fs = (self.cfg["cue"] or {}).get("fp_songs") or {}
            n = len(fs)
            cache = getattr(self, "_fp_song_bpm_cache", None)
            if cache is None or cache[0] != n:
                m = {}
                for _k, _v in fs.items():
                    try:
                        _sid = int(_v.get("song_id") or 0)
                        _b = float(_v.get("bpm") or 0.0)
                        if _sid and _b > 0.0:
                            m[_sid] = _b
                    except (TypeError, ValueError):
                        continue
                cache = (n, m)
                self._fp_song_bpm_cache = cache
            return float(cache[1].get(int(sid), 0.0) or 0.0)
        except Exception:                                        # noqa: BLE001
            return 0.0

    def _allowed_song_ids(self):
        """当前**真实存在**的曲目 id：曲库（music_meta 记的 song_id）+ 码表内嵌曲目。

        ⚠ `self.cfg` 是 `Config` 对象，**没有 .get()**，只能下标读取。
        指纹库会残留历史条目（歌已从曲库移除、指纹还在）。不设白名单时，
        放一首不在曲库的歌会被匹配到这些过期 id ⇒ 界面报出一首"不存在"的歌。
        """
        import hashlib as _hl
        ids = set()
        try:
            _meta = self.cfg["music_meta"] or {}
        except Exception:                                        # noqa: BLE001
            _meta = {}
        for _v in (_meta.values() if isinstance(_meta, dict) else []):
            try:
                ids.add(int(_v.get("song_id")))
            except Exception:                                    # noqa: BLE001
                pass
        try:
            _lib = self.cfg["music_library"] or []
        except Exception:                                        # noqa: BLE001
            _lib = []
        for _p in _lib:
            if isinstance(_p, str) and _p:
                try:
                    ids.add(int(_hl.md5(_p.encode("utf-8")).hexdigest()[:8], 16))
                except Exception:                                # noqa: BLE001
                    pass
        try:
            _fp = (self.cfg["cue"] or {}).get("fp_songs") or {}
        except Exception:                                        # noqa: BLE001
            _fp = {}
        for _v in (_fp.values() if isinstance(_fp, dict) else []):
            try:
                ids.add(int(_v.get("song_id")))
            except Exception:                                    # noqa: BLE001
                pass
        # ★★ 导入的曲库（ERLIB 曲库分析包）**也是有效曲目**：
        #   它们的指纹在指纹库里，但 id 不在 music_meta 里。漏掉它们 =
        #   导入的 50 首歌永远认不出（用户 2026-10-22 指出）。
        try:
            import imported_lib as _il
            from config import app_base_dir as _abd
            _cn = _il.open_meta(_abd())
            try:
                for (_sid,) in _cn.execute("SELECT song_id FROM songs"):
                    try:
                        ids.add(int(_sid))
                    except Exception:                            # noqa: BLE001
                        pass
            finally:
                _cn.close()
        except Exception:                                        # noqa: BLE001
            pass
        return ids

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
                         sr=int(self.cfg["audio"].get("sample_rate", 0) or 0),
                         ch0=int(self.cfg["audio"].get("asio_ch0", 0) or 0),
                         ch1=int(self.cfg["audio"].get("asio_ch1", 1) or 1))
        self.cfg["audio"].update({"source_type": t, "device_name": dev})
        self.cfg.save()

    def apply_audio_settings(self, source_type, device, sr, mono, ch0=0, ch1=1):
        """音源对话框应用：保存配置并重启采集

        ⚠ 原来的 `gain` 参数已去掉（2026-09-25）：那个滑块没接进分析链路，
        而且能量算法对整体增益免疫（实测 0.5~4x 完全无差别），留着只会误导。
        """
        upd = {"source_type": source_type, "device_name": device,
               "sample_rate": int(sr), "mono": bool(mono)}
        if source_type == "asio":
            upd["asio_ch0"], upd["asio_ch1"] = int(ch0), int(ch1)
        self.cfg["audio"].update(upd)
        self.cfg.save()
        self.audio.start(source_type, device, mono=mono, sr=int(sr), ch0=ch0, ch1=ch1)

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
        # ★★ 2026-10-07：NDI 音频跟「开始自动 VJ」绑定（用户要求）。
        #   未开始时一个样本都不推（连静音都不推）。这里同步给采集侧 +
        #   转发线程侧（两处都读同一个 `audio.state.ndi_running`）。
        #   ⚠ 必须在 `toggle_run()` **之后**同步：`engine.toggle_run` 是幂等翻转，
        #     用它的返回值（= 翻转后的真实状态）而不是 `not running`。
        try:
            self.audio.state.ndi_running = bool(running)
        except Exception:                                      # noqa: BLE001
            pass
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
        # ★ S1 #1：运行中不许切多屏/切模式（现场切错会黑屏）
        try:
            self._sync_multi_controls()
        except Exception:                                         # noqa: BLE001
            pass
        if running:
            # 每次开始 VJ 都按设置应用（全屏→该屏；窗口→按分辨率重置大小），
            # 解决：之前仅在窗口隐藏时才调，导致窗口已可见时开始VJ不重置的bug
            self.apply_output_screen()
            self.out_win.raise_()
        else:
            if self.out_win.isVisible():
                self.out_win.hide()         # 停止：自动关闭输出窗口（主界面预览仍在实时显示）
            try:
                self.engine.set_frame_consumers(
                    bool(self.preview_panel.preview_on()) or self.out_win.isVisible())
            except Exception:
                pass

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
        # 5 档（关闭/低/中/高/极高）；配置里 -1=关闭、0~3=低~极高；
        # 下拉索引 = 配置值 + 1。setCurrentIndex 会触发 currentIndexChanged → 写回配置
        v = max(-1, min(3, int(self.cfg["auto"]["intensity"]) + int(d)))
        self.settings.intensity.setCurrentIndex(v + 1)

    def toggle_lock(self):
        self.engine.lock_clip()

    def toggle_ui(self):
        """Ctrl+H：隐藏/恢复界面。

        ★ 2026-10-18 审计修复：旧实现 `setVisible(False)` 之后**无法恢复** ——
          没有托盘图标、任务栏按钮一起消失，而热键是 WindowShortcut（窗口不可见就收不到），
          默认还不是全局 ⇒ 一旦按下就只能去任务管理器结束进程。
          改成"最小化"：任务栏还在，点一下/Alt+Tab 就能回来，语义对现场也是安全的。
        """
        if self.isVisible() and not self.isMinimized():
            self.showMinimized()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def panic_reset(self):
        """紧急恢复：把配置与**运行时**一起复位到默认（Ctrl+Shift+R）。

        ⚠ 2026-10-10 审计：旧实现只 `cfg.reset()`（清空配置里的 library/layers/clip_tags…），
          但内存里的 `self.library` / `engine.layers` 原样保留 ⇒「界面显示」与「配置」不一致：
          之后任意一次 save 会把内存里的旧曲库又写回配置（"恢复默认"形同虚设），或反之
          重启后素材库/图层列表整体消失。这里选「**两者都复位**」，以"界面显示与配置一致"为准。
        """
        self.cfg.reset()
        # 运行时一并复位。⚠ `engine.library` 与 `self.library` 是**同一个 dict 对象**
        #   （见 engine.__init__ `self.library = media_library`），必须**原地 clear**，
        #   不能 `self.library = {}` 重新赋值（那样引擎还指着旧 dict ⇒ 又对不上）。
        try:
            self.library.clear()
        except Exception:                                        # noqa: BLE001
            self.library = {}
        self._cur_layer = 0
        self.engine.rebuild_from_cfg()
        self.refresh_grid()            # 素材库网格清空，与 cfg["library"] == [] 一致
        self._fill_tag_combo()
        self.refresh_all_layer_blocks()
        self.hotkeys.apply()
        self.lbl_warn.setText("")
        QMessageBox.information(
            self, tr("app_name"),
            T("已恢复默认")
            + "（设置、图层与素材库列表均已复位；素材文件仍保留在磁盘上）")

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
        """点色卡 = 手动锁定该颜色 10 秒（10 秒后自动回到自动变色）

        ⚠ `manual_t` 必须存**墙钟** `time.time()`：以前存的是 `perf_counter()`
          （进程内相对时钟）却落盘，重启后新进程的 perf_counter 更小 ⇒ 差值为负
          ⇒ 永远"还没到期" ⇒ 一开机就被上次的颜色锁死（用户报"颜色一直不变"）。
        """
        import time
        self.cfg["color"]["manual_color"] = name
        self.cfg["color"]["manual_t"] = time.time()
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
        """单个特效的详细设置对话框：触发模式 / 衰减 / 音频驱动

        ★ 2026-10-30 用户「大砍刀」：曲风识别删除 ⇒ "曲风"触发方式与曲风输入框一并删。
          老配置里 `trigger="genre"` 的会落到引擎的 `else: m = 1.0` ⇒ 退化为常驻。
        """
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QComboBox, \
            QCheckBox, QDialogButtonBox
        e = self.cfg["postfx"]["effects"].get(key) or {}
        name = self.settings.postfx_checks.get(key)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"效果设置 - {name.text() if name else key}")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("触发模式"))
        trig = QComboBox()
        trig.addItems(["常驻", "事件"])
        _trig = e.get("trigger", "constant")
        trig.setCurrentIndex(1 if _trig == "event" else 0)    # 老的 "genre" 归到"常驻"
        lay.addWidget(trig)
        lay.addWidget(QLabel("事件衰减时长"))
        dc = QComboBox()
        dc.addItems(["0.5 秒", "1 秒", "1.5 秒", "2 秒", "3 秒", "4 秒"])
        try:
            dc.setCurrentIndex([0.5, 1.0, 1.5, 2.0, 3.0, 4.0].index(float(e.get("decay", 1.5) or 1.5)))
        except ValueError:
            dc.setCurrentIndex(2)
        dc.setVisible(trig.currentIndex() == 1)
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
            dc.setVisible(i == 1)     # 0=常驻 / 1=事件（衰减时长只在事件模式有意义）
        trig.currentIndexChanged.connect(on_trig)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            self.set_postfx(key, "trigger", ["constant", "event"][trig.currentIndex()])
            self.set_postfx(key, "decay", [0.5, 1.0, 1.5, 2.0, 3.0, 4.0][dc.currentIndex()])
            self.set_postfx(key, "drive", ["off", "bass", "mid", "high", "energy"][dr.currentIndex()])

    def set_solo(self, idx, on):
        """Solo 互斥：开启某图层的独奏时，自动关闭其它图层的 Solo"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        for i, lay in enumerate(self.engine.layers):
            lay.solo = bool(on) if i == idx else False
        self._layers_call("refresh_header")
        self.save_layers()

    def save_layers(self):
        """图层属性变化后持久化"""
        self.engine.push_layers_to_cfg()
        self.cfg.save()
        # ★ 用户第 10 条：多屏模式下有 N 个图层栈（每屏一个）⇒ 任何一处改了图层属性
        #   （可见性/透明度/混合/名称/Solo…）都要**广播**刷新，不能只刷当前那块。
        #   这里是最集中的收口（`_set` / `_set_opacity` / `_set_blend` / 改名都走它）。
        try:
            self._layers_call("refresh_header")
            self._layers_call("refresh_playing")
        except Exception:                                         # noqa: BLE001
            pass
        try:
            os_obj = getattr(self, "output_studio", None)
            if os_obj is not None and os_obj.isVisible():
                os_obj.refresh_all()
        except Exception:
            pass

    def set_intensity(self, i):
        # 下拉索引 0=关闭 / 1~4=低~极高；配置存 -1 / 0~3
        try:
            v = max(-1, min(3, int(i) - 1))
        except (TypeError, ValueError):
            v = 1
        self.engine_cfg("auto", "intensity", v)

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
                    self, T( T("GPU 解码")),
                    Tf("这台机器暂时用不了 GPU 解码，已自动回退软解：\n{}", msg))

    def set_mode_selection(self, i):
        if i == 0:
            self.engine_cfg("mode", "auto", True)
            self.engine_cfg("mode", "manual", "auto")
        elif i == 4:
            # 手动切换：不走自动打分，三个节奏档也不生效。
            # `mode.auto=False` 与三档一致（都表示"不自动挑节奏"）；
            # `manual` 存 SWITCH_MANUAL，引擎侧由 manual_switch_mode() 统一判定。
            self.engine_cfg("mode", "auto", False)
            self.engine_cfg("mode", "manual", SWITCH_MANUAL)
        else:
            self.engine_cfg("mode", "auto", False)
            self.engine_cfg("mode", "manual", ["normal", "fast", "beat"][i - 1])
        self.engine.mode = self.engine.manual_mode() or self.engine.mode
        self.settings.sync_beat_section()

    def set_transition(self, i):
        self.engine_cfg("mode", "transition", ["", "fade", "cut", "slide", "zoom", "glitch"][i])

    def set_resolution(self, i):
        # ★ 2026-10-18 审计：可编辑下拉在"用户正在打字"时 currentIndex 会变成 -1，
        #   而 Python 的 [-1] 会取到**最后一项** ⇒ 画布被改成 1080x1080（方形）。
        if i is None or int(i) < 0 or int(i) > 3:
            return
        try:
            w, h = [(1280, 720), (1920, 1080), (1080, 1920), (1080, 1080)][int(i)]
        except (IndexError, ValueError):
            return
        self.set_custom_resolution(w, h)

    def set_resolution_text(self, text):
        """手动输入分辨率（宽 x 高）"""
        import re
        # ⚠ 这里原来有个 `if getattr(self, "_loading_ui", False): return` 守卫，但全库
        #   没有任何一处把它置 True ⇒ 恒不生效的死守卫，删掉。程序化回填时若要抑制，
        #   正确做法是 `blockSignals(True/False)`（见 _load_settings_to_ui 里其它控件的写法）。
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
        # ★ 多屏输出：分辨率属于“主屏幕”，同步到活动配置后再让引擎重建画布。
        try:
            import output_model as _om
            cfg = _om.active_config(self.cfg.data)
            main = _om.main_screen(cfg) if cfg else None
            if main is not None:
                main["width"], main["height"] = w, h
                _om.sync_legacy_output(self.cfg.data)
                self.cfg.save()
                multi_on = bool((self.cfg.data.get("output") or {}).get("multi_enabled", False))
                if multi_on:
                    self.engine.set_output_config(cfg)
                    try:
                        self.output_studio.refresh_all()
                    except Exception:
                        pass
                    if int(self.cfg["output"].get("screen", -1)) < 0 and self.out_win.isVisible():
                        self.out_win.apply_window_size()
                    return
        except Exception:
            pass
        self.cfg["output"]["width"], self.cfg["output"]["height"] = w, h
        self.cfg.save()
        # 线程安全：画布尺寸经 engine.set_canvas_size（锁内重建），不再 GUI 直接赋值
        self.engine.set_canvas_size(w, h)
        # 窗口模式下按新分辨率重置输出窗口大小
        if int(self.cfg["output"].get("screen", -1)) < 0 and self.out_win.isVisible():
            self.out_win.apply_window_size()

    def set_aspect_lock(self, s):
        on = bool(s)
        try:
            self.out_win.set_aspect_lock(on)
        except Exception:
            self.out_win.aspect_lock = on
        self.cfg["output"]["aspect_lock"] = on
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
        """输出显示器（S2 #14）：下拉索引 0=关闭(-2) / 1=窗口模式(-1) / 2..=显示器 0.."""
        self.cfg["output"]["screen"] = int(i) - 2
        self.cfg.save()
        # 运行中立即生效（否则只存配置，要等下次开始才应用 →「更改失效」）
        if self.engine.running:
            self.apply_output_screen()

    def set_spout_enabled(self, s):
        s = bool(s)
        # ★ 多屏输出：这个旧开关只代表“主屏幕”的 Spout，写入活动配置后由引擎路由表统一重建。
        try:
            import output_model as _om
            cfg = _om.active_config(self.cfg.data)
            main = _om.main_screen(cfg) if cfg else None
            if main is not None and bool((self.cfg.data.get("output") or {}).get("multi_enabled", False)):
                if s:
                    ok, err = _probe_spout_once()
                    if not ok:
                        QMessageBox.warning(
                            self, T("Spout 不可用"),
                            "无法启用 Spout 输出。\n\n%s\n\n"
                            "常见原因：显卡驱动不支持共享纹理（GL/DX 互操作）。\n"
                            "（NDI 输出不依赖这个，可改用 NDI）" % err)
                        s = False
                main["spout"] = s
                _om.sync_legacy_output(self.cfg.data)
                self.cfg.save()
                self.engine.set_output_config(cfg)
                try:
                    self.output_panel.chk_spout.blockSignals(True)
                    self.output_panel.chk_spout.setChecked(s)
                    self.output_panel.chk_spout.blockSignals(False)
                    self.output_studio.refresh_all()
                except Exception:
                    pass
                return
        except Exception:
            pass
        if s:
            # 勾选时立即探测一次（与 NDI 一致）：Spout 失败以前是完全静默的 ——
            # 界面上勾着、其实一帧都没发出去，用户只会以为「勾了没用」。
            ok, err = _probe_spout_once()
            if not ok:
                QMessageBox.warning(
                    self, T("Spout 不可用"),
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

        self.engine._spout = None   # 名字变了重建 sender

    def set_ndi_enabled(self, s):
        s = bool(s)
        # ★ 多屏输出：旧开关只代表主屏幕 NDI；运行时探测保留，发送端由引擎路由表创建。
        try:
            import output_model as _om
            cfg = _om.active_config(self.cfg.data)
            main = _om.main_screen(cfg) if cfg else None
            if main is not None and bool((self.cfg.data.get("output") or {}).get("multi_enabled", False)):
                if s:
                    from ndi_out import probe_runtime
                    ok, detail = probe_runtime()
                    if not ok:
                        QMessageBox.warning(
                            self, T(T("NDI 不可用")),
                            T("NDI 运行时加载失败，无法启用 NDI 输出。") + "\n\n" +
                            T("运行时是随软件一起提供的，不需要单独安装。"
                              "常见原因是杀毒软件隔离了软件目录里的 Processing.NDI.Lib.x64.dll，"
                              "请把它加入白名单；或者重新解压一次安装包再试。") +
                            "\n\n" + T("错误详情：") + "\n" + detail)
                        s = False
                main["ndi"] = s
                _om.sync_legacy_output(self.cfg.data)
                self.cfg.save()
                self.engine.set_output_config(cfg)
                try:
                    self.output_panel.chk_ndi.blockSignals(True)
                    self.output_panel.chk_ndi.setChecked(s)
                    self.output_panel.chk_ndi.blockSignals(False)
                    self.audio.state.ndi_audio_on = bool(
                        s and self.cfg["output"].get("ndi_audio", False))
                    self.output_studio.refresh_all()
                except Exception:
                    pass
                return
        except Exception:
            pass
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
                    self, T( T("NDI 不可用")),
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
        # NDI 音频：**必须 NDI 输出开着 + 配置里 ndi_audio 为真**才发。
        # 给采集循环同步一个布尔量：False 时它连每块的 asarray/reshape/copy 都省掉。
        try:
            self.audio.state.ndi_audio_on = bool(
                s and self.cfg["output"].get("ndi_audio", False))
        except Exception:
            pass


    def set_output_alpha(self, s):
        """Spout/NDI 的 Alpha 输出开关。

        ★ 只切画布格式：开启后引擎用 ARGB32（透明底/直通 alpha），Spout/NDI
          原样发送 BGRA+A；显示器输出仍由输出窗口铺黑底，物理显示器没有 alpha。
        ★ 运行中切换会重建一次画布（可能闪一帧），但不需要重启 Spout/NDI。
        """
        on = bool(s)
        self.cfg["output"]["alpha_enabled"] = on
        try:
            self.engine.set_alpha_output(on)
        except Exception:
            pass
        self.cfg.save()

    def _ndi_audio_feed(self, pcm, sr):
        """把采集到的 PCM 投递给 NDI 发送端（运行在**音频转发线程**里）。

        ⚠ 本方法每秒被调用约 47 次，**绝不能每块都打日志**，因此门控原因用一个
          实例级的一次性去重标志 `self._ndi_audio_gate_logged`：
          只在「门控原因发生变化时」打一次，正常发送时复位。

        ⚠ 历史 BUG（现场"有画面没声音、日志空白"）：原来这里是**静默 return**，
          `ndi is None` 或 `cfg["output"]["ndi_enabled"]` 为假时一个日志都不打，
          导致用户完全无法判断音频到底卡在哪一环。现改为分情况打一次性诊断日志。
        """
        # lazy 初始化一次性去重标志（不依赖 __init__，避免遗漏初始化分支）
        if not hasattr(self, "_ndi_audio_gate_logged"):
            self._ndi_audio_gate_logged = None
        try:
            ndi = self.engine._ndi
        except Exception:                                      # noqa: BLE001
            # engine 尚未就绪：按"发送端未建立"处理，且**不能**让异常冒泡
            ndi = None
        enabled = False
        try:
            enabled = bool(self.cfg["output"].get("ndi_enabled"))
        except Exception:                                      # noqa: BLE001
            enabled = False
        # ★★ 2026-10-07 新增门控：NDI 音频只在「开始自动 VJ」后才推流。
        #   用户要求：「ndi音频输出没有和开始自动vj绑定，没开始时也有输出」。
        #   未开始时**一个样本都不发**（不推静音、不推数据）—— 静音也是一条流，
        #   接收端会以为"有声音只是很轻"，反而更难判断。彻底不发最干净：
        #   接收端连不上音频就不显示音频电平表，语义明确。
        #   ⚠ 用 `getattr` 兜底：早期版本没有 `running` 属性时按"未运行"处理，
        #     但那种情况只可能是引擎还没建好（此时也不该发音频）。
        try:
            vj_running = bool(getattr(self.engine, "running", False))
        except Exception:                                      # noqa: BLE001
            vj_running = False
        # 正常路径（三个条件全真才发）
        if ndi is not None and enabled and vj_running:
            # ⚠ 这里**不再**调 `set_audio_rate()`：帧的采样率在 open() 时就被冻结，
            #   现在帧固定 48k、非 48k 的数据由 `feed_audio()` 自己重采样
            #   （原来每块告诉它一次采样率既无效又误导，见 _NDI音频诊断.md §1.3）。
            # ★ 首次喂入时把"开始真正发送"写进取证日志（现场可据此确认音频链路已通）
            if not getattr(self, "_ndi_audio_start_logged", False):
                self._ndi_audio_start_logged = True
                try:
                    from ndi_out import _health_write
                    _health_write("音频链路已通：开始把采集 PCM 投给 NDI（sr=%s）" % (sr,))
                except Exception:                              # noqa: BLE001
                    pass
            ndi.feed_audio(pcm, sr)
            # 恢复正常发送 → 复位去重标志，这样下次再被门控会重新报一次
            self._ndi_audio_gate_logged = None
            return
        # —— 门控分支：只在原因变化时打一次一次性日志（每秒 ~47 次调用，不能刷屏）——
        if ndi is None:
            reason = "sender_missing"
        elif not enabled:
            reason = "output_disabled"
        else:
            reason = "auto_not_running"
        if self._ndi_audio_gate_logged != reason:
            self._ndi_audio_gate_logged = reason
            _txt = ("[NDI] 音频门控：发送端尚未建立（engine._ndi is None），音频暂不发送"
                    if reason == "sender_missing" else
                    "[NDI] 音频门控：NDI 输出未启用，音频暂不发送"
                    if reason == "output_disabled" else
                    "[NDI] 音频门控：尚未「开始自动VJ」，音频暂不发送（开始后自动恢复）")
            # ⚠⚠ 必须**同时落盘**：打包后是 --windowed，没有控制台，只 print 等于没记
            #   （现场"有画面没声音、日志空白"能拖这么多轮，根因就是诊断全丢了）。
            #   复用 ndi_out 的落盘通道，保证 NDI 两侧取证写进同一个 output_health.log。
            try:
                print(_txt)
            except Exception:                                  # noqa: BLE001
                pass
            try:
                from ndi_out import _health_write
                _health_write(_txt.replace("[NDI] ", ""))
            except Exception:                                  # noqa: BLE001
                pass

    def _sync_frame_consumers(self, *_):
        try:
            multi_visible = False
            try:
                mp = getattr(self, "_multi_page", None)
                multi_visible = bool(
                    mp is not None
                    and getattr(self, "perf_stack", None) is not None
                    and self.perf_stack.currentWidget() is mp
                    and self.pages.currentIndex() == 0)
            except Exception:
                multi_visible = False
            on = bool(self.preview_panel.preview_on()) or bool(
                self.out_win is not None and self.out_win.isVisible()) or multi_visible
            self.engine.set_frame_consumers(on)
        except Exception:
            pass

    def apply_output_screen(self):
        idx = int(self.cfg["output"].get("screen", -2))
        screens = QGuiApplication.screens()
        if idx <= -2:
            # ★ S2 #14：「关闭」⇒ 不弹输出窗口（默认行为）。引擎照常合成，
            #   Spout/NDI 发送不受影响（它们不走显示器窗口）。
            try:
                self.out_win.hide()
            except Exception:                                     # noqa: BLE001
                pass
            try:
                self.engine.set_frame_consumers(
                    bool(self.preview_panel.preview_on()))
            except Exception:                                     # noqa: BLE001
                pass
            return
        if 0 <= idx < len(screens):
            self.out_win.show()
            self.out_win.show_fullscreen_on(screens[idx])
        else:
            self.out_win.show_windowed()
        try:
            self.engine.set_frame_consumers(
                bool(self.preview_panel.preview_on()) or self.out_win.isVisible())
        except Exception:
            pass

    def reset_window_size(self):
        """手动重置输出窗口大小（输出设置里的按钮）"""
        self.out_win.reset_window_size()

    def _load_settings_to_ui(self):
        a = self.cfg["auto"]
        # 配置 -1=关闭、0~3=低~极高；下拉索引 = 配置值 + 1（0=关闭）。
        # 钳到有效档位，脏配置越界时 setCurrentIndex 会被 Qt 忽略 → 界面与配置对不上
        try:
            _iv = int(a.get("intensity", 1))
        except (TypeError, ValueError):
            _iv = 1
        self.settings.intensity.setCurrentIndex(max(0, min(4, _iv + 1)))
        tr_ov = a.get("transition") if isinstance(a.get("transition"), str) else ""
        tr_ov = (self.cfg["mode"].get("transition") or tr_ov or "")
        try:
            self.settings.transition.setCurrentIndex(["", "fade", "cut", "slide", "zoom", "glitch"].index(tr_ov))
        except ValueError:
            self.settings.transition.setCurrentIndex(0)
        self.settings.energy_map.setChecked(a.get("energy_map", True))
        self.settings.bpm_sync.setChecked(a.get("bpm_speed_sync", True))
        # 节拍网格（八拍乐句对齐）：默认开。setChecked 会触发 stateChanged → engine_cfg 再写一遍
        # 配置（值相同无副作用）。
        self.settings.chk_beat_grid.setChecked(a.get("beat_grid", True))
        # 渲染帧率：60 / 30 / 20（脏值一律回落到 60）
        try:
            _rf = int((self.cfg["perf"] or {}).get("render_fps", 60) or 60)
        except Exception:
            _rf = 60
        self.settings.render_fps.setCurrentIndex({60: 0, 30: 1, 20: 2}.get(_rf, 0))
        try:
            _ff = bool((self.cfg["perf"] or {}).get("fps_follow_source", True))
            self.settings.chk_fps_follow.blockSignals(True)
            self.settings.chk_fps_follow.setChecked(_ff)
            self.settings.chk_fps_follow.blockSignals(False)
        except Exception:
            pass
        # GPU 解码（实验）：配置 → 复选框 + 模块总闸（复选框 setChecked 会触发 stateChanged
        # → set_gpu_decode 再写一遍配置，值相同无副作用）
        try:
            _gd = bool((self.cfg["perf"] or {}).get("gpu_decode", False))
        except Exception:
            _gd = False
        # ★★ 2026-10-28（现场：开了 GPU 解码就只剩启动画面、主窗口永不来）：
        #   `chk_gpu` 连的是 `main.set_gpu_decode`（会同步跑 `mm.gpu_status()` 探测）。
        #   启动回填时控件默认 False、config 可能是 True ⇒ 值变了 ⇒ 触发信号 ⇒
        #   在 `__init__` 里同步阻塞（GLWorker 不就绪时直接冻死）。
        #   ⇒ 回填状态必须 blockSignals；真正的 `mm.set_gpu_decode(_gd)` 下面单独调，
        #     探测留给用户手动点开关时再做（那时界面已经起来了）。
        try:
            self.settings.chk_gpu.blockSignals(True)
            self.settings.chk_gpu.setChecked(_gd)
        finally:
            self.settings.chk_gpu.blockSignals(False)
        # 工具条上的同一个开关也要跟上（两个入口共享状态）
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
        # 能量校正滑条已无 UI（2026-10-18）；配置值在 __init__ 里直接写进 audio.energy_scale。
        m = self.cfg["mode"]
        manual = m.get("manual", "auto")
        if str(manual) == SWITCH_MANUAL:
            self.settings.mode_combo.setCurrentIndex(4)
        elif m.get("auto", True) or manual in (None, "auto"):
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
        # ⚠ 与 colorfx.tick 同口径：墙钟 + 「0 <= 已过时间 < hold」。
        #   旧配置里存的是 perf_counter，算出来是负数或天文数字 ⇒ 判为已过期（自动迁移）。
        import time as _t
        lock = str(col.get("manual_color", "") or "")
        _el = _t.time() - float(col.get("manual_t", 0.0) or 0.0)
        if lock and not (0.0 <= _el < float(col.get("manual_hold", 10.0) or 10.0)):
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
            self.output_panel.screen_combo.setCurrentIndex(int(o.get("screen", -2)) + 2)
        except Exception:
            self.output_panel.screen_combo.setCurrentIndex(0)
        # ★ S2 #3/#4/#14：启动时也按当前配置的模式决定「输出设置」显示哪些项
        try:
            import output_model as _om0
            _c0 = _om0.active_config(self.cfg.data)
            self.output_panel.set_mode(str((_c0 or {}).get("mode", "multi")))
        except Exception:                                        # noqa: BLE001
            pass
        self.output_panel.chk_spout.setChecked(o.get("spout_enabled", False))
        # Alpha 开关：只回填，不触发 set_output_alpha（引擎已按配置初始化画布格式）
        _wa = self.output_panel.chk_alpha
        _wa.blockSignals(True)
        _wa.setChecked(bool(o.get("alpha_enabled", False)))
        _wa.blockSignals(False)
        # ★ 2026-10-30（用户：「不要输入框 把名字写死」）：界面已无输入框，
        #   这里把配置里的旧值**统一成常量**，保证 engine 实际用的名字与界面显示一致
        #   （否则旧配置里残留的自定义名会让 OBS 里看到的和界面对不上）。
        o["spout_name"] = DEFAULT_SPOUT_NAME
        # ⚠⚠ NDI：用配置回填 UI **必须屏蔽信号**（2026-10-01 修，现场"没声音"的直接原因）
        #   原来直接 `setChecked(ndi_enabled)` 会触发 `set_ndi_enabled()` 槽，而那个槽里会做
        #   运行时探测 ⇒ 启动时弹「NDI 不可用」/把已存值改写。这里只回填、不触发槽。
        _w = self.output_panel.chk_ndi
        _w.blockSignals(True)
        _w.setChecked(bool(o.get("ndi_enabled", False)))
        _w.blockSignals(False)
        o["ndi_name"] = DEFAULT_NDI_NAME
        self.output_panel._update_ndi_full()
        # 信号被屏蔽了，槽不会再跑 ⇒ 这里显式同步一次采集侧的布尔量（不要在这里做运行时探测：
        # 启动时弹「NDI 不可用」会很烦；真正的探测交给 set_ndi_enabled）
        try:
            self.audio.state.ndi_audio_on = bool(
                o.get("ndi_enabled") and o.get("ndi_audio"))
        except Exception:                                          # noqa: BLE001
            pass
        # ⚠ 同样因为信号被屏蔽，原来"靠 setChecked 触发 set_ndi_enabled ⇒ 启动即启用 NDI"
        #   这条路径没了，必须显式补上，否则重启后 NDI 输出不会自动恢复。
        #   这里**不弹窗**（启动弹窗很烦）：运行时不可用就只记日志 + 把勾去掉，
        #   用户手动再勾时会由 set_ndi_enabled 给出完整说明。
        try:
            eng = getattr(self, "engine", None)
            if o.get("ndi_enabled") and eng is not None:
                from ndi_out import get_ndi, probe_runtime
                _ok, _detail = probe_runtime()
                if _ok:
                    eng._ndi = get_ndi(o.get("ndi_name") or "EasyRealityAutoVJ")
                    eng._ndi_fail = False
                    print("[NDI] 启动恢复输出（运行时 %s）；发送端等第一帧按实际分辨率建立"
                          % _detail)
                else:
                    print("[NDI] 启动时运行时不可用，NDI 输出未启用：%s" % _detail)
                    self.cfg["output"]["ndi_enabled"] = False
                    # ⚠ 同步把 ndi_audio 配置也落回 False：否则 config 里仍是 True，
                    #   下次启动会照旧喂音频（NDI 输出都没开，音频投递必然被门控拦下）。
                    self.cfg["output"]["ndi_audio"] = False
                    self.output_panel.chk_ndi.blockSignals(True)
                    self.output_panel.chk_ndi.setChecked(False)
                    self.output_panel.chk_ndi.blockSignals(False)
                    try:
                        self.audio.state.ndi_audio_on = False
                    except Exception:                          # noqa: BLE001
                        pass
                    print("[NDI] NDI 运行时不可用 → 已同步关闭 NDI 音频")
        except Exception as _e:                                    # noqa: BLE001
            print("[NDI] 启动恢复 NDI 失败：%s" % _e)
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

    def _inject_song_grids(self):
        """构建并注入 song_id → 离线节拍网格（八拍乐句）映射给引擎。

        只保留**VDJ 真值来源 + 字段完整 + conf 足够高**的网格 —— 演出稳定性优先：
        宁可退回现有实时拍钟，也不用不可信的网格去驱动切换。
        网格结构见 beatgrid：统一约定 **八拍头 = anchor + 8n × beat_len**。

        ★ 来源门槛 `src ∈ {"vdj", "bt"}`（**不能省，也不能用 conf 替代**）：
          · `vdj` —— VirtualDJ 库的锚点，最准，且 0 成本（有就用）。
          · `bt`  —— **Beat This! 本地小节检测**（2026-09-28 新增）。给**没有 VDJ 数据**的
                     曲目补位：实测 21/21 小节头判对（偏置 ≤0.25 拍），单首约 1.9 秒。
          · `local` —— **我们自己那套（`phase_of` + 组合特征）实测小节头 0/21 对**
                     （偏 1~2 个整拍），**一律不用**。
          ⚠ 为什么不能用 conf 代替来源判定：`local` 网格的 conf 也会顶到上限
            （conf 反映的是「八拍相位对比度」，不是「锚点对不对」）——
            历史上正是这个原因让 15 首错的本地网格差点全被放进网格模式。
        """
        meta = self.cfg["music_meta"] or {}
        out = {}
        # song_id → 曲目路径：手动「八拍相位翻转」要按路径持久化到 cfg["music_meta"][path]
        self._grid_path_by_sid = {}
        try:
            import beatgrid as _bg
        except Exception:                                        # noqa: BLE001
            _bg = None
        for p, m in meta.items():
            if not isinstance(m, dict):
                continue
            sid = m.get("song_id")
            g = m.get("grid")
            if not sid or not isinstance(g, dict):
                continue
            manual = m.get("grid_manual") if isinstance(m.get("grid_manual"), dict) else None
            # ★ 合并用户手工标注（架构 §6.1：grid 算法产物 + grid_manual 用户产物 → effective_grid）
            g_eff = _bg.effective_grid(g, manual) if _bg is not None else dict(g)
            # 用户显式标注过（BPM/八拍/分段）⇒ 用户产物可信，跳过 src/conf 门槛（仍校验 beat_len）；
            # 无手工标注 ⇒ 沿用原 src/conf 门槛（零回归）。
            user_marked = bool(manual and (
                manual.get("segments") or manual.get("anchors")
                or (float(manual.get("bpm_manual") or 0.0) > 0)))
            try:
                bl = float(g_eff.get("beat_len") or 0.0)
                anchor = float(g_eff.get("anchor"))
                conf = float(g.get("conf") or 0.0)
            except Exception:
                continue
            # 阈值理由：
            #   - conf ≥ 0.6：VDJ 正常路径 conf≥0.80（可信）；本地自算需有八拍相位对比度证据
            #     （0.55 为无对比度地基线）；VDJ 分析失败回退项 conf=0.5 予以剔除。
            #   - beat_len ∈ (0.2, 1.5)：对应 BPM 40~300，排除异常值。
            if not (0.2 < bl < 1.5):
                continue
            if not user_marked:
                if g.get("src") not in ("vdj", "bt"):
                    continue
                if conf < 0.6:
                    continue
            item = {"bpm": g_eff.get("bpm"), "beat_len": bl, "anchor": anchor,
                    "conf": conf, "src": g_eff.get("src") or g.get("src"),
                    "dur": g.get("dur"),
                    # 手动八拍相位翻转（见 engine.flip_phrase_now）：存的是开关标记，
                    # 注入时换算成 4 拍偏移。落盘键故意放在 grid **外面**（同级的
                    # `grid_flip`），否则重扫重建 grid 时会被覆盖掉。
                    "flip": 4.0 if m.get("grid_flip") else 0.0}
            if g_eff.get("segments"):
                item["segments"] = g_eff["segments"]
            if g_eff.get("anchors"):
                item["anchors"] = g_eff["anchors"]
            out[sid] = item
            self._grid_path_by_sid[sid] = p
        # ★★ 2026-10-08（erlib 第二步）：合并「导入的曲库分析包」里的离线网格。
        #   导入的歌在本机**没有文件**，所以不可能出现在 `cfg["music_meta"]` 里；
        #   它们单独存在 `imported_lib`（按 song_id 索引）。
        #   ⚠ 本机曲库**优先**：同一个 song_id 两边都有时以本机为准
        #     （本机扫的是真实文件，DJ 那边可能扫的是另一个版本）。
        try:
            from config import app_base_dir
            import imported_lib as _IL
            _imp = _IL.lookup_grids(app_base_dir())
            for _sid, _g in _imp.items():
                if _sid in out:
                    continue
                _manual = _g.get("manual") if isinstance(_g.get("manual"), dict) else None
                _gel = _bg.effective_grid(_g, _manual) if _bg is not None else dict(_g)
                _item = {"bpm": _gel.get("bpm"), "beat_len": _gel.get("beat_len"),
                         "anchor": _gel.get("anchor"), "conf": _g.get("conf"),
                         "src": _gel.get("src") or _g.get("src"),
                         "dur": _g.get("dur"), "flip": _g.get("flip") or 0.0}
                if _gel.get("segments"):
                    _item["segments"] = _gel["segments"]
                if _gel.get("anchors"):
                    _item["anchors"] = _gel["anchors"]
                out[_sid] = _item
            if _imp:
                print("[曲库] 合并导入包：+%d 条离线网格" % len(_imp))
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self.engine.set_song_grids(out)
        except Exception:
            pass
        # ★★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：把**导入包里的能量段落**
        #   也注入引擎。原来只有本机曲库有段落（`cfg["music_meta"][path]`），导入的歌
        #   文件不在本机、没有 path ⇒ 没有段落色带、也不按段落切素材（"不一样"）。
        #   ⚠ 本机优先：同一 sid 两边都有时用本机的（本机扫的是真实文件）。
        try:
            from config import app_base_dir as _abd
            import imported_lib as _IL2
            _secm = {}
            for _sid, _t in _IL2.lookup_sections(_abd()).items():
                if _sid in self._grid_path_by_sid:
                    continue
                _secm[int(_sid)] = _t
            self.engine.set_song_sections(_secm)
            if _secm:
                print("[曲库] 合并导入包：+%d 条段落表" % len(_secm))
        except Exception:                                        # noqa: BLE001
            pass
        return len(out)

    def _restore_cue_sheets(self):
        """启动 300ms 后：把 `cfg["cue"]["sheets"]` 里登记过、且图层仍记着 `cuesheet_id` 的表装载回去。

        设计取舍：
          · **只装表、不选曲目**（`track=None`）——曲目由认歌决定，那时会自动选；
          · 表不在索引里 / 文件被移走 ⇒ 跳过并记一行，**绝不弹窗**（启动时打扰用户最讨厌）；
          · 全程 try/except：一张表坏了不能影响别的图层，更不能影响启动。
        """
        try:
            ents = []
            try:
                ents = list((self.cfg["cue"] or {}).get("sheets") or [])
            except Exception:                                    # noqa: BLE001
                ents = []
            by_name = {}
            for e in ents:
                if isinstance(e, dict) and e.get("path"):
                    by_name[os.path.basename(str(e["path"]))] = str(e["path"])
            if not by_name:
                return
            try:
                media_dir = str((self.cfg["cue"] or {}).get("media_dir") or "")
            except Exception:                                    # noqa: BLE001
                media_dir = ""
            import cuesheet as _cs
            n_ok = 0
            for lay in (getattr(self.engine, "layers", None) or []):
                if not getattr(lay, "is_cuesheet", False):
                    continue
                cid = str(getattr(lay, "cuesheet_id", "") or "").strip()
                p = by_name.get(cid)
                if not p or not os.path.isfile(p):
                    if cid:
                        print("[码表] 启动恢复跳过：%s（包不在索引/文件不存在）" % cid)
                    continue
                try:
                    sheet, info = _cs.load(p, media_dir=media_dir or None)
                    self.engine.set_cue_sheet(
                        sheet, media_paths=info.get("present_media") or {},
                        track=None, layer=lay, sheet_id=cid)
                    n_ok += 1
                except Exception as e:                            # noqa: BLE001
                    print("[码表] 启动恢复失败：%s（%s: %s）" % (cid, type(e).__name__, e))
            if n_ok:
                print("[码表] 启动恢复 %d 张（等认歌后自动选曲目）" % n_ok)
                # ★★ 2026-11-06：装表是在启动 300ms 后做的，而 `layers_panel.rebuild()` 早就
                #   跑过了 ⇒ 码表图层的素材行**还是空的**（用户看到"码表行没有缩略图"）。
                #   装完必须重填那一行（`sync_layer` 会按 `cue_media_paths()` 建格子）。
                try:
                    for _i, _lay in enumerate(self.engine.layers):
                        if getattr(_lay, "is_cuesheet", False):
                            self._layers_call("refresh_header", _i)
                            self._layers_call("sync_layer", _i)
                except Exception:                                # noqa: BLE001
                    pass
        except Exception:                                        # noqa: BLE001
            pass

    def _merge_imported_song_meta(self):
        """把**导入包**的歌并入 `_song_meta_by_id`（HUD 歌名用）。返回并入条数。

        ★★ 2026-10-31（用户「怎么输出预览不显示我在放那首歌」）：
          导入包的歌没有本机文件、不在 `cfg["music_meta"]` 里，只有
          `imported_meta.db` 里那一条（按 song_id）。识别能命中（指纹在库里），
          但 HUD 查 `_song_meta_by_id` 查不到 ⇒ 输出预览里没有 🎵 歌名。

        ⚠ 只补 title/artist（曲风已下线，别再复活）；
        ⚠ 已存在的 sid **不覆盖**（本机曲库 / 码表包的信息更全，优先保留）。
        """
        from config import app_base_dir
        import imported_lib as _IL
        added = 0
        for sid, m in (_IL.song_meta_by_sid(app_base_dir()) or {}).items():
            if sid in self._song_meta_by_id:
                continue
            self._song_meta_by_id[sid] = m
            added += 1
        return added

    def _merge_cue_song_meta(self):
        """把 `cfg["cue"]["fp_songs"]` 的曲目并入 `_song_meta_by_id`（HUD 歌名 / 曲风查表）。

        为什么必须有它（2026-10-17 用户报「识别出来了 HUD 怎么不显示歌名」）：
          码表包带来的歌**不在本机曲库**（`cfg["music_meta"]`）里 —— 它在指纹库里用的是
          **合成 id** `int(md5("fp:"+fp_id)[:8],16)`（`cue_fp_pack.fp_song_id()`）。
          而 `_song_meta_by_id` 原来只从 `music_meta` + `imported_lib` 构建
          ⇒ 歌认出来了（`recognized_song_id` = 合成 id），查表却是空的
          ⇒ HUD 退化到"只显示曲风"（码表歌连曲风都没有 ⇒ 整行空白）。
          ⚠ 这是**同一件事的两套 id 空间**没打通，不是 HUD 绘制问题。

        幂等：本机曲库已有同 id 的**不覆盖**（本机信息优先，与 `imported_lib` 的合并策略一致）。
        返回新增条数（0 ⇒ 无变化、不写配置）。
        """
        n = 0
        try:
            songs = (self.cfg["cue"] or {}).get("fp_songs") or {}
        except Exception:                                        # noqa: BLE001
            songs = {}
        for _fp, rec in list(songs.items()):
            rec = rec or {}
            try:
                sid = int(rec.get("song_id"))
            except (TypeError, ValueError):
                continue
            if sid in self._song_meta_by_id:
                continue
            self._song_meta_by_id[sid] = {
                "path": "",
                "title": str(rec.get("title") or "") or str(_fp),
                "artist": str(rec.get("artist") or ""),
                "genres": [],
                "cue": True,        # 来源标记：HUD 据此显示艺人而不是曲风（码表歌没有曲风）
            }
            n += 1
        if n:
            print("[码表] 歌名映射 +%d 首（来自码表包）" % n)
        return n

    def _init_recognizer(self):
        """曲库就绪后启用现场识别：建指纹库 + song_id→meta 映射（无库则跳过）"""
        from config import app_base_dir
        # 离线节拍网格：与指纹库无关，先注入（曲库即算好，演出中只查表）
        try:
            self._inject_song_grids()
        except Exception:
            pass
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
        # ★ 2026-10-30 用户「大砍刀」：原「合并导入包的曲风」整块删除
        #   （`imported_lib.lookup_genres` 已随曲风链路一并移除）。
        #   `_song_meta_by_id` 仍由本机曲库 + 码表包提供，HUD 歌名照常显示。
        # ★★ 2026-10-31 修正（用户「怎么输出预览不显示我在放那首歌」）：
        #   上面那句话对**导入包的歌**不成立 —— 它们不在 `cfg["music_meta"]` 里
        #   （本机没文件，按 song_id 存在 `imported_meta.db`）⇒ 认出来了、HUD 却没有
        #   「🎵 歌名」那一行（截图：在放 `(10) [天音] Fly Anima.flac`，只显示 BPM/能量）。
        #   曲风不恢复（那刀是对的），只补 **title/artist**。
        try:
            self._merge_imported_song_meta()
        except Exception as _e:                                  # noqa: BLE001
            print("[识别] 合并导入包歌名失败：%s" % _e)
        # ★ 2026-10-17：码表包带来的歌（合成 id）也并入 —— 否则认出来但 HUD 查不到歌名
        try:
            self._merge_cue_song_meta()
        except Exception as _e:                                  # noqa: BLE001
            print("[码表] 合并歌名映射失败：%s" % _e)
        if not self._song_meta_by_id:
            return
        try:
            from fp import FingerprintDB
            if self._fp_db is not None:      # 重建：关闭旧连接，扫描后拿最新数据
                try:
                    self._fp_db.close()
                except Exception:
                    pass
            self._fp_db = FingerprintDB(db_path)
            # ★★ 只让"当前曲库里真实存在"的歌参与匹配（防假阳性）
            try:
                _n = self._fp_db.set_allowed(self._allowed_song_ids())
                print("[识别] 有效曲目 %d 个（其余历史指纹不参与匹配）" % _n)
            except Exception as _e:                              # noqa: BLE001
                print("[识别] 白名单设置失败：%s" % _e)
            self.audio.set_recognizer(self._fp_db)
            try:
                self.audio.set_allowed_provider(self._allowed_song_ids)
            except Exception:                                    # noqa: BLE001
                pass
            # ★★ 2026-10-27（C 组误识别 C：B 站 200BPM 快歌误锁到 142BPM 的歌）：注入
            #   「song_id → 库内 BPM」提供者，识别线程用它跟实时 bpm_display 做一致性校验。
            #   取值口径与注入给引擎的网格**同一份**（`_song_grid_lookup` 装的就是
            #   `effective_grid` 的产物，已含 `grid_manual.bpm_manual` 的手工覆盖）⇒
            #   用户改过 BPM 的曲子，校验用的也是改后的值，不会自相矛盾。
            # ★ 2026-10-27：老数据迁移 —— `fp_songs` 以前漏存包内曲目的 `bpm`，
            #   这里按 `sheet` 分组解一次 `.ercue` 的 `cuesheet.json` 补齐（幂等、只补缺的），
            #   补到就落盘。放在注入 provider **之前**，保证第一次识别就能用上。
            try:
                import cue_fp_pack as _cfp
                _nfix = _cfp.migrate_fp_song_bpm(self.cfg)
                if _nfix:
                    self.cfg.save()
                    print("[识别] 包内曲目 BPM 补齐 %d 条（供 BPM 一致性校验）" % _nfix)
            except Exception as _e:                              # noqa: BLE001
                print("[识别] 包内曲目 BPM 迁移失败（不影响识别）：%s" % _e)
            try:
                self.audio.set_song_bpm_provider(self._lib_bpm_of)
            except Exception:                                    # noqa: BLE001
                pass
            # ★★ 2026-10-22：**指纹 id 自愈**（后台线程，绝不卡启动）。
            #   背景：历史扫描时若路径写法变过（`<盘符>://` vs `<盘符>:/`），同一条曲目在库里
            #   会存成**旧的 song_id**；白名单只认"按当前路径算出的 id" ⇒ 该曲永远
            #   命中哈希却 0 票（局域网实测：hit=208/217、raw=0）。
            #   这里用库内文件实测一次，证据足够（票数≥100 且 offset≈取样点）才改挂。
            try:
                import threading as _th
                _th.Thread(target=self._heal_fp_ids_bg, args=(self._fp_db,),
                           name="fp-id-heal", daemon=True).start()
            except Exception:                                    # noqa: BLE001
                pass
            # ★ 启动时**一次性对账**（把历史遗留的"撞车"清掉）：以前"先导码表、后加曲库"
            #   会在库里留下两份指纹（合成 id + 路径 id）⇒ 票数被拆一半 ⇒ 时认时不认。
            #   ⚠ 启动路径**必须零重活**：它只比 `cfg["cue"]["fp_songs"]` 的键与
            #     `music_meta` 的 fp_id 集合，撞车才动库；**不解包、不解码音频**。
            #     没有撞车时 0 删除、0 写盘（所以不会"每次启动都改一次配置"）。
            try:
                import cue_fp_pack as _cfp
                _rec = _cfp.reconcile_dupes(self.cfg, self._fp_db)
                if _rec.get("dropped"):
                    print("[码表] 启动对账：清掉 %d 首与本机曲库撞车的合成指纹（%d 行）"
                          % (len(_rec.get("dropped") or []), int(_rec.get("rows", 0) or 0)))
            except Exception:                                    # noqa: BLE001
                pass
        except Exception as e:                                   # noqa: BLE001
            # ⚠ 2026-10-10 审计：以前这里是 `pass` —— 指纹库打不开（损坏/被占用/路径异常）时
            #   现场识别**静默失效**（认不出歌 → 拿不到精确曲风与离线八拍网格），
            #   而且零线索、无从排查。现在补：① 控制台 ② engine_error.log（本文件已有先例）
            #   ③ 复用 audio.state.error 给出用户可见提示（lbl_warn 会显示）。
            print("[识别] 指纹库初始化失败：%s" % e)
            try:
                import traceback
                from config import app_base_dir as _abd
                with open(os.path.join(_abd(), "engine_error.log"),
                          "a", encoding="utf-8") as _f:
                    _f.write("[%s] 指纹库初始化失败：%s\n%s\n"
                             % (time.strftime("%Y-%m-%d %H:%M:%S"), e,
                                traceback.format_exc()))
            except Exception:                                    # noqa: BLE001
                pass
            # 用户可见提示：复用**已有**的 `audio.state.error` 字段（不新造字段）。
            # 它在 `_update_status` 里经 `snap["error"]` 显示到 `lbl_warn`。
            try:
                self.audio.state.error = "指纹库不可用，现场识别已关闭"
            except Exception:                                    # noqa: BLE001
                pass
            # 与旧实现行为一致：识别器未绑定 ⇒ 现场识别不启用。
            self._fp_db = None

    def open_music_library(self):
        """打开音乐曲库 —— 它现在是**第 3 页**（曲库·曲风）里的整页面板，不再是弹窗。"""
        try:
            self.switch_page(2)
            if getattr(self, "music_panel", None) is not None:
                self.music_panel.refresh()
        except Exception:                                          # noqa: BLE001
            pass

    def import_erlib_package(self):
        """导入 erlib 导出的曲库分析包（`.eravjlib`）——
        别的机器（DJ 机）扫好的**曲风 / 封面 / 指纹 / 离线节拍网格**。

        ⚠ 包**不含音频**：演出机不需要有这些歌的文件 —— 靠**指纹**认出来即可。
        ⚠ 导入是"并进本机"（幂等覆盖），**不碰**本机自己扫的曲库。

        ★★ 2026-10-31（用户原话：「导入了怎么 识别不到 …… 而且 导入重复的歌也没提醒」）：
          **导入前**先与本机曲库比对，命中就弹窗让用户选 —— 这是本次修复的核心。
          为什么必须在"开始导入"**之前**问：一旦写进 `imported_meta.db` / `fingerprints.db`
          再回滚，就得删用户数据（本次明确不删既有数据），所以只能提前拦。
        """
        p, _ = QFileDialog.getOpenFileName(
            self, "选择曲库分析包（.eravjlib）", "",
            "erlib 分析包 (*.eravjlib);;所有文件 (*)")
        if not p:
            return
        th = getattr(self, "_erlib_imp", None)
        if th is not None and th.isRunning():
            return
        # ★ 2026-10-31：导入前检测重复（只读包 + 只读 cfg，几毫秒）。
        skip_sids = self._ask_import_duplicates(p)
        if skip_sids is None:
            print("[曲库] 导入已取消（用户在重复提示里点了取消）")
            return
        try:
            from config import app_base_dir
            import imported_lib as _IL
            n_old = _IL.count(app_base_dir())
        except Exception:                                        # noqa: BLE001
            n_old = 0
        print("[曲库] 开始导入曲库分析包：%s（本机原有导入 %d 首，跳过重复 %d 首）"
              % (p, n_old, len(skip_sids)))
        self._erlib_imp = ErlibImportThread(p, self, skip_sids=skip_sids)
        self._erlib_imp.progress.connect(
            lambda a, b, s: print("[曲库] 导入中：%s %d/%d" % (s, a, b or 0)))
        self._erlib_imp.finished_ok.connect(
            lambda st: self._on_erlib_imported(st, n_old))
        self._erlib_imp.failed.connect(self._on_erlib_failed)
        self._erlib_imp.start()

    def _ask_import_duplicates(self, pkg_path):
        """导入前弹窗：「跳过这些（推荐）/ 仍然全部导入 / 取消」。

        返回**要跳过的 song_id 集合**（`set()` = 全部导入）；
        用户点「取消」⇒ 返回 `None`（调用方据此中止导入）。

        ⚠ 检测本身失败（包损坏/读不了）时**不挡死**，但**必须让用户看见**：
          ★★★ 2026-11-01 18:15（用户「怎么还是正常导入了呢」）：这里原来只是
          `print(); return set()` —— GUI 打包版**没有控制台**，print 等于扔进黑洞，
          于是「判重因为 `Config` 没有 `.get()` 抛异常」整整一天无人知晓（见
          `erlib.dupdetect.cfg_get` 的注释）。现在改成弹框问一句，
          用户至少知道"这次没去重"，而不是以为程序判断过"没有重复"。
        """
        try:
            from erlib.dupdetect import scan_package, dup_prompt_text
            # ★★ 2026-11-01（QA P1/P2/P3）：把**已导入**的曲目也当"本机已有"——
            #   反复重导入同一个包时，对照那一份只在 `imported_meta.db` 里，
            #   不并进来的话判据③的时长护栏会瞎（剪辑版误判），同 sid 也命不中。
            #   拿不到基准目录就传 None（scan_package 会退回指纹库所在目录）。
            _base = ""
            try:
                from config import app_base_dir
                _base = app_base_dir()
            except Exception:                                    # noqa: BLE001
                _base = ""
            res = scan_package(pkg_path, self.cfg, base_dir=(_base or None))
        except Exception as e:                                   # noqa: BLE001
            print("[曲库] 导入前重复检测失败（不影响导入）：%s" % e)
            _b = QMessageBox(self)
            _b.setIcon(QMessageBox.Icon.Warning)
            _b.setWindowTitle(T("导入曲库分析包"))
            _b.setText(Tf("导入前的重复检测没跑起来：{}", e))
            _b.setInformativeText(T(
                "这次会**不去重**直接导入（本机已有的同名/同一首不会被跳过）。\n"
                "如果是「导入了重复的歌也没提醒」，请把这行报错发给我。"))
            _b_go = _b.addButton("继续导入（不去重）", QMessageBox.ButtonRole.AcceptRole)
            _b.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            _b.setDefaultButton(_b_go)
            _b.exec()
            return set() if _b.clickedButton() is _b_go else None
            return set()
        dups = list(res.get("dups") or ())
        if not dups:
            return set()
        print("[曲库] 导入前发现重复 %d 首：%s"
              % (len(dups),
                 ", ".join("%s(%s%s)" % (
                     d.get("file_name"), d.get("how"),
                     # ★ 2026-11-01（用户「没做指纹对比吗」）：判据③要把重合率打出来 ——
                     #   用户反馈"判重到底凭啥"时，日志是唯一能自证的地方。
                     (" %.0f%%" % (float(d.get("overlap") or 0.0) * 100.0))
                     if d.get("how") == "fingerprint" else "")
                     for d in dups[:8])))
        # 弹窗：三个按钮，默认「跳过这些（推荐）」。
        # ⚠ 用自定义按钮而不是 question()：三个选项里"跳过"必须**显式**是 AcceptRole，
        #   否则回车会落到「全部导入」上（现场一按回车就把重复塞进库）。
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(T("导入曲库分析包"))
        box.setText(T(dup_prompt_text(dups, res.get("total") or 0)))
        btn_skip = box.addButton("跳过这些（推荐）", QMessageBox.ButtonRole.AcceptRole)
        btn_all = box.addButton("仍然全部导入", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_skip)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_skip:
            return set(res.get("dup_sids") or ())
        if clicked is btn_all:
            print("[曲库] 用户选择「仍然全部导入」（明知重复，按原样导入）")
            return set()
        return None

    def _on_erlib_imported(self, st, n_old):
        """导入完成：**立刻刷新查表 + 曲库列表**（否则要重启才生效）+ 给用户结果。"""
        try:
            self._inject_song_grids()      # 离线网格（内部会合并导入的）
            self._init_recognizer()        # song_id → 曲风映射（同上）
        except Exception as e:                                   # noqa: BLE001
            print("[曲库] 导入后刷新查表失败：%s" % e)
        try:
            # 曲库列表立刻显示导入的歌（它们以 "@imported:<sid>" 伪路径列出，
            # 带 📦 前缀、蓝色显示；见 panels.MusicLibraryPanel.refresh）。
            if getattr(self, "music_panel", None) is not None:
                self.music_panel.refresh()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            from config import app_base_dir
            import imported_lib as _IL
            n_now = _IL.count(app_base_dir())
        except Exception:                                        # noqa: BLE001
            n_now = 0
        # ★ 2026-10-31（用户「导入重复的歌也没提醒」）：跳过数必须**写在结果里**，
        #   否则用户只会看到"导入完成 19 首"却不知道为什么少了 2 首（以为丢了数据）。
        # ⚠ 逐行拼再 join：直接把 `%` 接在"多行字符串 + 变量"后面时，
        #   `%` 的优先级比 `+` 高，只会格式化最后一段（踩过）。
        n_skip = int(st.get("skipped") or 0)
        _lines = ["导入完成：",
                  "· 本包：%d 首 / 指纹 %d 条 / 封面 %d 张"
                  % (st.get("songs", 0), st.get("fingerprints", 0),
                     st.get("covers", 0))]
        if n_skip:
            _lines.append("· 跳过重复：%d 首（本机已有，未写入 / 未写指纹）" % n_skip)
        # ★★ 2026-11-01（用户 17:20「节拍网格（包 6 / 本机 7）导入失败」）：
        #   网格版本不一致**不再拦死导入**，但必须让用户知道这批歌的 BPM 可能偏，
        #   否则他会以为"导入成功 = 分析数据都对"（200BPM 扫成 132.7 就是这么发现的）。
        #   ⚠ 只提示、不阻塞：歌能放能认，重导或手改 BPM 都能救。
        #   ⚠⚠ 2026-11-01 QA：**绝不能把版本号写死**在文案里 —— 第一版写的是"（v6）"，
        #     等 GRID_VER 升到 8，导 v7 的包照样提示"（v6）"，反而骗人。
        #     版本从 `stats["grid_versions"]`（= `LibReader.grid_stale` 的 (包版本, 本机版本)）
        #     取；取不到就退化成「来自旧算法」，**绝不出现 "vNone"**。
        n_grid_stale = int(st.get("grid_stale") or 0)
        if n_grid_stale:
            _gv = st.get("grid_versions")
            if (isinstance(_gv, (tuple, list)) and len(_gv) == 2
                    and _gv[0] is not None and _gv[1] is not None):
                _ver_txt = "（包 v%s / 本机 v%s）" % (_gv[0], _gv[1])
            else:
                _ver_txt = ""
            _lines.append(
                "· 节拍网格：%d 首来自旧算法%s，BPM 可能偏差 → "
                "建议用新版 ERLIB 重新导出，或在详情页手改 BPM"
                % (n_grid_stale, _ver_txt))
        _lines.append("· 本机累计导入：%d 首" % n_now)
        _lines.append("")
        _lines.append("之后 DJ 放这些歌时，本机会自动认出并套用曲风、封面与离线八拍网格。")
        QMessageBox.information(self, T("导入曲库分析包"), "\n".join(_lines))

    def _on_erlib_failed(self, msg):
        print("[曲库] 导入失败：%s" % msg)
        # ★ 2026-11-01：文案跟着行为改 —— 网格版本（grid_ver）不一致**已不再拦导入**
        #   （只警告，见 `erlib.format.LibReader.check_versions` / `transfer.import_into`），
        #   所以这里再写"指纹/网格"会误导用户去纠结网格。能走到这个提示的只剩
        #   格式版本 / 指纹版本不一致（以及包损坏等），正文只提指纹。
        QMessageBox.warning(self, T("导入曲库分析包"),
                            "导入失败：\n%s\n\n"
                            "常见原因：包的指纹算法版本与本机不一致"
                            "（用新版 erlib 重新导出即可）。" % msg)

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

    def _scan_busy(self):
        """是否已有扫描在进行中 —— **含"正在弹确认框"的窗口期**。

        ⚠⚠ 2026-10-29 现场 bug（用户报"重扫非常慢"、CPU 60%）：**两个扫描同时在跑**。
          `scan_time.log` 铁证 —— 两组进度交错独立递增：

              23:19:08  40/98（2 线程）      23:19:22  10/98（5 线程）
              23:19:37  50/98（2 线程）      23:19:47  20/98（5 线程）
              23:20:11  60/98（2 线程）      23:20:18  30/98（5 线程）

          而且指纹从 5.7s/首 恶化到 **40.67s/首**（两批 worker 抢 16 核）。
          根因正是铁律 11「重入守卫必须在动 UI 之前置位」：旧代码先
          `QMessageBox.question(...)`（**模态，但期间照常派发事件**）再赋值
          `self._music_scan`，于是第二次点击进来时检查看到的是"还没有扫描"，
          两道检查一起放行 ⇒ 两个 `MusicScanThread` 各自起一个线程池。

        现在的判据：`_scan_pending`（点击→确认框→启动 这一整段都算忙）+ 线程在跑。
        """
        if getattr(self, "_scan_pending", False):
            return True
        sc = getattr(self, "_music_scan", None)
        return bool(sc is not None and sc.isRunning())

    def _on_music_scan_done(self, *_a):
        """扫描线程结束（正常或异常）⇒ 放开重入守卫。"""
        self._scan_pending = False

    def scan_music(self, force=False):
        if self._scan_busy():
            QMessageBox.information(self, T("扫描分析"), "已有扫描在进行中，请稍候。")
            return
        # ★ 守卫**必须在弹窗之前**置位（见 `_scan_busy` 的现场复盘）
        self._scan_pending = True
        try:
            if force:
                ret = QMessageBox.question(
                    self, T("全量重扫"),
                    "全量重扫将忽略已有结果，重新分析曲库里的全部歌曲（含指纹、节拍网格与段落）。\n"
                    "可能耗时较长，确定继续吗？",
                    QMessageBox.Yes | QMessageBox.Cancel)
                if ret != QMessageBox.Yes:
                    self._scan_pending = False
                    return
        except Exception:                                          # noqa: BLE001
            self._scan_pending = False
            raise
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
        # ★ 2026-10-29 用户要求：「扫到相同音乐（名字或指纹相似）时给个弹窗，
        #   对比选择是留着还是删掉一个」。
        #   放在扫描**结束之后**统一查一遍，而不是扫到哪首就弹哪首 ——
        #   扫描是并行的，逐首弹会连着炸出好几个窗口，而且那时别的歌还没入库、
        #   时长/指纹都还取不到。扫完再查一次既完整又不打扰。
        self._music_scan.finished.connect(
            lambda: self.find_duplicate_music(auto=True))
        # ★ 守卫复位挂在 finished 上：线程正常/异常结束都会 emit（见 `_scan_busy`）
        self._music_scan.finished.connect(self._on_music_scan_done)
        try:
            self._music_scan.start()
        except Exception:                                          # noqa: BLE001
            self._scan_pending = False
            raise

    # ------------------------------------------------------------------ 查重
    def find_duplicate_music(self, auto=False):
        """查重：找出曲库里指向同一段音频的多份，弹窗让用户选留哪一份。

        :param auto: True = 扫描结束后自动跑（没找到就不打扰用户）

        ⚠ 「移除」只删曲库条目（见 `remove_music_many` 的说明），
          **不动磁盘文件** —— 误删音频不可逆，不该藏在一次点击里。
        """
        t = getattr(self, "_dup_check", None)
        if t is not None and t.isRunning():
            return
        self._dup_auto = bool(auto)
        self._dup_check = DupCheckThread(self)
        self._dup_check.found.connect(self._show_duplicate_dialogs)
        self._dup_check.start()

    def _show_duplicate_dialogs(self, pairs):
        """把查重结果逐对弹出来让用户选（已移除的跳过）。"""
        auto = bool(getattr(self, "_dup_auto", False))
        pairs = list(pairs or [])
        if not pairs:
            if not auto:
                QMessageBox.information(self, T("查重"), T("没有发现重复曲目。"))
            return
        meta = self.cfg["music_meta"] or {}
        lib = self.cfg["music_library"] or []
        removed = 0
        for pr in pairs:
            a, b = pr.get("a"), pr.get("b")
            # 前一对可能已经把其中一份移除了 ⇒ 后面的对失效，跳过
            if a not in lib or b not in lib:
                continue
            dlg = DuplicateDialog(self, pr, meta.get(a) or {}, meta.get(b) or {})
            dlg.exec()
            c = dlg.choice()
            if c == "a":
                self.remove_music(b)
                removed += 1
            elif c == "b":
                self.remove_music(a)
                removed += 1
        if removed:
            QMessageBox.information(
                self, T("查重"),
                T("已从曲库移除 {} 份重复条目（磁盘上的音频文件没有动）。").format(removed))

    def rescan_music(self, path):
        """重新分析扫描单首曲目（重建指纹 + 重算节拍网格 + 能量段落）。

        段落分析已移除（见 `_music_scan` 的说明），别再写「重算段落」。"""
        if self._scan_busy():
            QMessageBox.information(self, T("重新分析"), "已有扫描在进行中，请稍候。")
            return
        self._scan_pending = True      # 同 `scan_music`：守卫先置位，再动任何东西
        try:
            self._music_scan = MusicScanThread(self, [path], True)
        except Exception:                                          # noqa: BLE001
            self._scan_pending = False
            raise
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.progress.show()
            dlg.progress.setRange(0, 1)
            self._music_scan.progress.connect(
                lambda d, t, _p: (dlg.progress.setRange(0, max(1, t)), dlg.progress.setValue(d)))
            self._music_scan.finished.connect(
                lambda: (dlg.progress.hide(), dlg.refresh(), self._init_recognizer()))
        self._music_scan.finished.connect(self._on_music_scan_done)
        try:
            self._music_scan.start()
        except Exception:                                          # noqa: BLE001
            self._scan_pending = False
            raise

    def show_music_info(self, path):
        """曲目信息（码表行 / 导入行仍走这个轻量弹窗）。

        ★ 2026-10-28：**本机曲目**改走「曲目信息与详情」合并页（见 panels 曲库右键），
          这里保留给"本机没有文件、打不开详情页"的行（码表派生行 / 导入包里的歌）。
          文本统一取 `music_detail.info_text()`，不再各写一份。
        """
        try:
            from music_detail import info_text
            text = info_text(self.cfg, path)
        except Exception:                                        # noqa: BLE001
            text = path
        QMessageBox.information(self, T("曲目信息"), text)

    def show_clear_music_analysis(self):
        """清除全部已分析结果（指纹 / 节拍网格 / 自动段落），保留用户产物。"""
        if getattr(self, "_music_scan", None) is not None and self._music_scan.isRunning():
            QMessageBox.information(self, T("清除分析数据"), "扫描正在进行中，请稍后再清除。")
            return
        box = QMessageBox(self)
        box.setWindowTitle( T("清除分析数据"))
        box.setText( T(CLEAR_ANALYSIS_TIP))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText( T("清除"))
        # ★ 2026-10-30 用户「大砍刀」：曲风没了 ⇒「保留手动设置过的曲风」复选框删除。
        if box.exec() != QMessageBox.Yes:
            return
        self.clear_music_analysis()
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.refresh()

    def _analysis_keep_paths(self):
        """清除分析数据时必须**整条保留** `music_meta` 的本机路径（码表 / 导入包覆盖的曲目）。

        ★★★ 2026-10-30 04:42 第四次报障（「怎么码表还是被清了」）—— 上一次只修了一半：
          上一版保住了**指纹**（`_analysis_keep_fp_ids`），却照旧把 `music_meta` 精简成
          "只留 grid_manual / grid_flip"。而码表曲目的 **BPM / 节拍网格 / 自动段落**
          就存在 `music_meta[path]` 里 ⇒ 清完这 3 首在曲库里变"无网格、未分析"、
          码表对齐拿不到 BPM、分段表消失 ⇒ 用户看到的就是"码表又被清了"。
          实测（清除后）：`music_meta` 91 → 3 条，3 首码表曲目**全都没有记录**，
          而指纹其实一行没少（主库 4 首 = 码表 3 首 + 包内合成指纹 1 个）。

        来源与 `_analysis_keep_fp_ids` **同一份判据**，只是返回路径而不是 song_id：
          a) `cfg["cue"]["fp_same"]` 的**值**（`fp_id → 本机路径`，导入码表时用指纹
             内容重合率算好并落盘，不依赖 `music_meta` 现状 —— 最可靠）；
          b) `cue_index.covered_paths(require_file=False)` 的 key（四级判据实时反查；
             关掉"包文件必须存在"是因为外接盘没插不等于码表被删）。

        ⚠ 必须在动 `music_meta` **之前**调用：b 要用里面的 fp_id / file_md5 / 名 / 时长。
        """
        out = set()
        try:
            _cue = self.cfg["cue"] or {}
        except Exception:                                        # noqa: BLE001
            _cue = {}
        try:
            _same = _cue.get("fp_same") or {}
            for _p in (_same.values() if isinstance(_same, dict) else []):
                if isinstance(_p, str) and _p:
                    out.add(_p)
        except Exception:                                        # noqa: BLE001
            pass
        try:
            import cue_index as _ci
            _cov = _ci.covered_paths(self.cfg, _ci.CueIndex().load_cfg(self.cfg),
                                     require_file=False) or {}
            for _p in _cov:
                if isinstance(_p, str) and _p:
                    out.add(_p)
        except Exception:                                        # noqa: BLE001
            pass
        return out

    def _analysis_keep_fp_ids(self, keep_paths=None):
        """清除分析数据时**必须保留**的指纹 `song_id`：码表内嵌曲 + 导入的曲库分析包。

        这两类都**不是**本机算法扫描的产物，而是用户导入的作品
        （用户 2026-10-30 两次强调「清除分析数据 不要清码表和曲库分析包的东西」）：
          · 码表：`cfg["cue"]["fp_songs"][fp_id]["song_id"]`（合成 id，`fp_song_id()`）
          · 导入包：`imported_meta.db` 里 `songs.song_id`
        本机曲库（`song_id = int(md5(路径)[:8], 16)`）**故意不在这里** ⇒ 会被清掉
        —— 那正是"清掉重新分析一遍"的本意。

        ⚠ 都是只读操作，异常只可能让"保护名单变小"（多清一点）；所以每一段都单独
          兜底，绝不因为读不到导入包就连码表也不保护。
        """
        keep = set()
        try:
            _fs = (self.cfg["cue"] or {}).get("fp_songs") or {}
        except Exception:                                        # noqa: BLE001
            _fs = {}
        for _rec in (_fs.values() if isinstance(_fs, dict) else []):
            try:
                keep.add(int(_rec.get("song_id")))
            except (AttributeError, TypeError, ValueError):
                continue
        # ③ 码表 / 导入包覆盖的**本机曲目**：它们的指纹是以本机 song_id 存的
        #   （不是 `fp_songs` 那个合成 id）⇒ 路径换算成 id 后并入白名单。
        #   判据只有一份，见 `_analysis_keep_paths()`。
        for _p in (keep_paths if keep_paths is not None
                   else self._analysis_keep_paths()):
            _sid = _local_song_id(_p)
            if _sid is not None:
                keep.add(_sid)
        try:
            import imported_lib as _il
            from config import app_base_dir as _abd
            _p = _il.meta_db_path(_abd())
            if os.path.exists(_p):
                import sqlite3 as _sq
                _conn = _sq.connect("file:%s?mode=ro" % _p, uri=True)
                try:
                    for _r in _conn.execute("SELECT song_id FROM songs"):
                        try:
                            keep.add(int(_r[0]))
                        except (TypeError, ValueError):
                            continue
                finally:
                    _conn.close()
        except Exception:                                        # noqa: BLE001
            pass
        return keep

    def clear_music_analysis(self):
        """清除**算法分析结果**（指纹 / 节拍网格 / 自动段落），**保留用户产物**。

        ⚠⚠ 2026-10-30 现场事故（用户：「清除分析数据 怎么把码表也清了」）：
          旧实现直接 `self.cfg["music_meta"] = {}` —— 把 `music_meta[path]` 里的
          **用户手工标注也一起清空了**（实测 91 条全没，其中 1 条是手改的 BPM/锚点/段落）。
          这个字典混着两类东西：
            · 算法产物：`grid`（BPM/网格）、自动生成的 `sections`/`sections_raw`、
              `cover`、`fp_id`、`file_md5`、`duration` …
            · **用户产物**：`grid_manual`（手改 BPM / 拍锚点 / 段落）、
              `grid_flip`（八拍相位翻转）、`sections_auto is False`（手存过的段落）
          用户点"清除分析数据"只是想清掉算法算的，手工作品必须留着 ——
          与扫描时的口径一致（`MusicScanThread` 的 `old_user` 快照同样是"force 重扫也保留"）。
        """
        # 计时：现场「点清除后卡死」（2026-10-30 02:06）就是这段慢到 3 分钟。
        # 现在 `purge_except` 改成重建表（实测 0.16 秒 / 146 万行），但**留着这行日志**：
        # 库规模是用户环境变量，下次变慢要能一眼看到是哪一步。
        _t_all = time.perf_counter()
        # ★★★ 白名单必须在**动 `music_meta` 之前**算：四级反查要用里面的
        #   `fp_id` / `file_md5` / 名 / 时长，先精简就一条都查不到 ——
        #   2026-10-30 04:27 第三次报障「还是把码表的清掉了」就是这个顺序错。
        keep_paths = self._analysis_keep_paths()
        keep = self._analysis_keep_fp_ids(keep_paths)
        meta = self.cfg["music_meta"] or {}
        new_meta = {}
        for p, m in meta.items():
            if not isinstance(m, dict):
                continue
            # ★★★ 2026-10-30 04:42 第四次报障的修法：码表 / 导入包**覆盖的曲目**，
            #   `music_meta` 整条保留 —— 它们的 BPM / 网格 / 段落是码表对齐要用的，
            #   只保指纹不保 meta，用户看到的就是"码表又被清了"（详见 `_analysis_keep_paths`）。
            if p in keep_paths:
                new_meta[p] = dict(m)
                continue
            k = {}
            for kk in ("grid_manual", "grid_flip"):
                if kk in m:
                    k[kk] = m[kk]
            if m.get("sections_auto") is False and m.get("sections"):
                k["sections"] = m["sections"]
                k["sections_raw"] = m.get("sections_raw")
                k["sections_auto"] = False
            if k:
                new_meta[p] = k
        self.cfg["music_meta"] = new_meta
        self.cfg.save()

        # ★★ 2026-10-30 修（用户两次强调：「清除分析数据 不要清码表和曲库分析包的东西」）：
        #   旧实现 `os.remove(fingerprints.db)` 把这个库里**另外两家**的指纹一起删了：
        #     · 码表 `.ercue` 的内嵌曲（合成 song_id，`cue_fp_pack` 灌进来的）
        #     · 导入的曲库分析包 `.eravjlib`（id 记在 `imported_meta.db`）
        #   实测代价：导入包 50 首**本机没有音频文件、只能靠指纹识别** ⇒ 全部失联；
        #   而且 `_init_recognizer()` 在库文件不存在时直接 return ⇒ 连识别器都不建。
        #   现在保留库文件，只删本机曲库那些行（白名单已在函数开头算好，见 `keep`）。
        n_songs = n_rows = 0
        if self._fp_db is not None:
            try:
                import fp as _fpmod
                n_songs, n_rows = _fpmod.purge_except(self._fp_db.conn, keep)
            except Exception as _e:                              # noqa: BLE001
                print("[曲库] 清除本机指纹失败：%s: %s" % (type(_e).__name__, _e))
            try:
                self._fp_db.close()
            except Exception:                                    # noqa: BLE001
                pass
        self._song_meta_by_id = {}
        self._fp_db = None
        try:
            self.audio.set_recognizer(None)
        except Exception:                                        # noqa: BLE001
            pass
        print("[曲库] 清除分析数据：删本机指纹 %d 首 / %d 行；保留码表+导入包 %d 首"
              % (n_songs, n_rows, len(keep)))
        # 立刻重建识别器：保留的码表 / 导入包指纹要**当场还能用**，不必等重启。
        # （库文件还在 ⇒ `_init_recognizer` 会正常建库并接上白名单；它只在文件不存在时才 return。）
        try:
            self._init_recognizer()
        except Exception as _e:                                  # noqa: BLE001
            print("[曲库] 清除后重建识别器失败：%s: %s" % (type(_e).__name__, _e))
        _dt = time.perf_counter() - _t_all
        # 阈值 5 秒：重建表要"搬保留的行 + 重建两个索引"，259 万行的真实库实测 3.98 秒
        # （146 万行时 1.9 秒）⇒ 按库规模正常增长，不该报"偏慢"。超过 5 秒才值得看。
        print("[曲库] 清除分析数据完成：%.2f 秒%s"
              % (_dt, "  ⚠ 偏慢，检查库规模/磁盘" if _dt > 5.0 else ""))

    def remove_music(self, path):
        """从曲库移除单首（曲库面板的右键走批量版 `remove_music_many`）。"""
        return self.remove_music_many([path])

    def remove_music_many(self, paths):
        """批量从曲库移除：**一次 save + 一次 refresh**（N 首别刷 N 次界面）。

        ⚠ 只删曲库**条目**（`music_library` + `music_meta`）——
          **音频文件与指纹不动**（指纹是扫描产物；要连指纹一起清是
          "清除分析数据"那条路，见 `show_clear_music_analysis`）。
        """
        paths = [p for p in (paths or []) if p]
        if not paths:
            return 0
        lib = self.cfg["music_library"]
        n = 0
        for p in paths:
            try:
                if p in lib:
                    lib.remove(p)
                    n += 1
            except Exception:                                    # noqa: BLE001
                pass
            self.cfg["music_meta"].pop(p, None)
        try:
            self.cfg.save()
        except Exception:                                        # noqa: BLE001
            pass
        dlg = getattr(self, "music_dialog", None)
        if dlg:
            dlg.refresh()
        return n

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
        QMessageBox.information(self, T("素材库目录"),
                                f"已设置：\n{d}\n\n后续「复制/移动」导入的素材会放到这里。")

    def _get_import_strategy(self):
        remembered = self.cfg["output"].get("import_strategy") or ""
        if remembered in ("copy", "move", "link"):
            return remembered
        return self._ask_import_strategy()

    def _ask_import_strategy(self):
        dlg = QDialog(self)
        dlg.setWindowTitle( T("导入素材"))
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
        dlg.setWindowTitle( T("发现重复素材"))
        lay = QVBoxLayout(dlg)
        names = "\n".join("· " + os.path.basename(p) for p in dupes[:8])
        if len(dupes) > 8:
            names += f"\n· …等共 {len(dupes)} 个"
        lay.addWidget(QLabel(f"以下 {len(dupes)} 个素材已在素材库中：\n\n{names}"))
        rb_replace = QRadioButton("替换旧素材（保留原标签/角色，用新文件替换）")
        rb_skip = QRadioButton("不添加（保持素材库不变）")
        rb_add = QRadioButton("仍然加入（复制模式下会生成 \"名字 (1)\" 的独立新条目）")
        # ★★ P0 审计修复：默认必须是「不添加」——原来默认「替换」，而 Esc/X 关闭
        #   既没接返回值、又落到最后一行 `return "replace"`，会去删素材库里的源文件。
        rb_skip.setChecked(True)
        for rb in (rb_replace, rb_skip, rb_add):
            lay.addWidget(rb)
        btns = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton("确定")
        ok.clicked.connect(dlg.accept)
        ok.setDefault(True)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        if dlg.exec() != QDialog.Accepted:      # Esc / X / 取消 ⇒ 不添加
            return "skip"
        if rb_add.isChecked():
            return "add"
        if rb_replace.isChecked():
            return "replace"
        return "skip"

    def _purge_for_replace(self, old_path, delete_file=False):
        """替换素材：移除旧条目（图层引用一并清理）。

        ⚠ 2026-10-18 审计修复：**默认不删文件**。旧实现在这里 `os.remove`，
          而调用方紧接着让 ImportWorker 去 copy 同一个路径 —— 源文件已被删，
          ⇒ 条目消失 + 文件丢失 + 导入失败（数据丢失级 P0）。
          真要删文件必须显式传 `delete_file=True`，目前没有任何调用方这么做。"""
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
                    self._layers_call("sync_layer", i)
        if not delete_file:                     # ★ 默认不删（见 docstring 说明）
            return
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
            QMessageBox.warning(self, T("导入失败"), f"无法复制 {name}：{e}")
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
                # ★ P0 修复：dupes 都是"路径已在库里"的文件 ⇒ 它自己就是库里的那份，
                #   "替换"在这里没有意义（不可能用一个文件替换它自己）。正确做法是：
                #   保留原条目、把它从本次导入里剔除，绝不删文件、也绝不重拷。
                _lib = os.path.abspath(self.library_dir())
                _keep = []
                for p in paths:
                    if p in self.library and os.path.abspath(p).startswith(_lib + os.sep):
                        continue                     # 库内的同一个文件 ⇒ 跳过（原样保留）
                    _keep.append(p)
                paths = _keep
                if not paths:
                    return

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
        if ((self.search_box.text() or "").strip() or self.kind_filter
                or self.role_filter or getattr(self, "dyn_filter", 0)):
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
            Tf("导入完成：新增 {} 个，跳过 {} 个（重复或无法识别）", new_n, skip_n))
        # ★★ 2026-10-31（用户：「素材库怎么不显示图片」「导入图片也没反应」）：
        #   根因不是导入失败 —— `_populate_grid` 的「全部」档会**跳过未扫描素材**
        #   （用户 2026-10-30 明确要求「选择时就排除这些」），而新导入的素材
        #   从来没有任何地方触发扫描 ⇒ 它们在默认视图里**根本看不见**，
        #   用户只能得出"导入没反应"的结论（实测库里 10 张图 + 6 个 LOGO 视频
        #   全卡在这个状态，而 368 个视频有标签所以照常显示 ⇒ "只有图片不显示"）。
        #   ⇒ 导入完成后**立刻自动扫这批新素材**，几秒内它们带上标签就出现在「全部」。
        _fresh_scan = [m.path for m in new_items if not m.tags]
        if _fresh_scan:
            self.lbl_status.setText(
                Tf("导入完成：新增 {} 个 —— 正在自动扫描新素材（扫完自动出现在素材库）",
                   new_n))
            # quiet：缩略图 worker 正在用同一条进度条，别抢
            self.scan_tags(_fresh_scan, quiet=True)

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
        """标签集合变化后刷新筛选按钮文案（**两页都要**，兼容旧调用点）"""
        self._sync_lib_controls()

    def refresh_grid(self, icons_only=False):
        """刷新素材库网格（**两页共用**：演出台那份 + 全屏素材库那份）。"""
        self._populate_grid(self.grid, self.lbl_lib_count, icons_only)
        g2 = getattr(self, "grid_full", None)
        if g2 is None:
            return
        # ⚠ 全屏那份**只在可见时填充**（库大时填充很贵，每次搜索都填两遍会明显变卡）：
        #   不可见就先标脏，切到那一页时再补（见 switch_page）。
        if self.pages.currentIndex() == 1:
            self._populate_grid(g2, getattr(self, "lbl_lib_count2", None), icons_only)
            self._lib_full_dirty = False
        else:
            self._lib_full_dirty = True
        self._sync_lib_controls()

    def _populate_grid(self, grid, count_lbl, icons_only=False):
        """把筛选后的素材填进 `grid`（演出台那份与全屏那份共用这段逻辑）。"""
        sz = grid.iconSize()
        if not icons_only:
            grid.clear()
            kw = (getattr(self, "lib_kw", "") or "").strip().lower()
            # 动态维度：0 全部 / 1 低 / 2 中 / 3 高 / 4 频闪 / 5 已排除 / 6 未扫描
            # （判据在 `passes_lib_filter`，纯函数、可测）
            _di = int(getattr(self, "dyn_filter", 0) or 0)
            # 角色维度：0 全部 / 1 前景 / 2 背景 / 3 已排除 / 4 未扫描
            _ri = int(self.role_filter or 0)
            for it in self.library.values():
                has_roles = any(it.roles.values())
                # 动态/未扫描判据统一走纯函数（见 `passes_lib_filter` 的说明：
                # 「全部」= 库里所有素材；图片不打标但算"已就绪"）
                if not passes_lib_filter(it, _di):
                    continue
                # --- 角色维度（只有 全部/前景/背景；「已排除/未扫描」按用户要求只放在动态栏）---
                if _ri == 1 and has_roles and not it.roles.get("fg"):
                    continue
                if _ri == 2 and has_roles and not it.roles.get("bg"):
                    continue
                # ★ 标签筛选（多选对话框）已删；搜索框仍可按标签文字过滤（见下面 kw 分支）
                if self.kind_filter == 1 and it.kind not in ("image", "gif"):
                    continue
                if self.kind_filter == 2 and it.kind != "video":
                    continue
                if kw and kw not in it.name.lower() and not any(kw in t.lower() for t in it.all_tags()):
                    continue
                # ★ 收藏夹过滤（2026-11-08）：只在“只看收藏夹”开启时生效
                _fav = getattr(self, "lib_favorite_paths", None)
                if _fav is not None and it.path not in _fav:
                    continue
                li = QListWidgetItem(it.icon_cached(sz.width(), sz.height()), self._item_text(it))
                li.setData(Qt.UserRole, it)
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
                grid.addItem(li)
        else:
            for i in range(grid.count()):
                li = grid.item(i)
                it = li.data(Qt.UserRole)
                li.setIcon(it.icon_cached(sz.width(), sz.height()))
                li.setText(self._item_text(it))
                li.setToolTip(self._item_tooltip(it))
                self._tint_role(li, it)
        if count_lbl is not None:
            count_lbl.setText(f"{grid.count()}/{len(self.library)}")

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
        _keep = set(DYNAMIC_TAGS) | set(COVERAGE_TAGS)
        tags = [t for t in it.all_tags() if t in _keep]
        if tags:
            return "·".join(tags[:3])
        return it.name[:18]

    def _item_tooltip(self, it):
        roles = []
        if it.roles.get("fg"): roles.append("前景")
        if it.roles.get("bg"): roles.append("背景")
        r = "/".join(roles) or "未设置"
        excl = "\n已排除自动匹配（logo 等固定素材）" if getattr(it, "excluded", False) else ""
        miss = "\n⚠ 素材文件已丢失（磁盘上找不到）" if not self._exists_cached(it.path) else ""
        _sc = getattr(it, "score", None)
        if isinstance(_sc, (int, float)):
            _sct = "动态分：%.2f（%s）" % (_sc, "越高越炸" if _sc >= 0.667
                                           else ("中间" if _sc >= 0.333 else "越静"))
        else:
            # 老素材还没打过分 ⇒ 按三档给代理值（换池就用这个值排序），并说明来源
            try:
                from match_engine import score_of
                _sct = "动态分：%.2f（按档位估算；跑一次「扫描素材」可得精确值）" \
                    % score_of(it)
            except Exception:                                  # noqa: BLE001
                _sct = "动态分：未扫描"
        return (f"{it.name}\n标签：{'、'.join(it.all_tags()) or '未扫描（点扫描素材）'}"
                f"\n{_sct}\n角色：{r}{excl}{miss}")

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
        self._sync_lib_controls()
        self.refresh_grid()

    def set_dyn_filter(self, i):
        """按动态档筛素材（0 全部 / 1 低 / 2 中 / 3 高 / 4 频闪）。

        ⚠ 选了某一档就**只显示这一档**（未扫描的素材不属于任何一档，会被排除）——
          这与旧的「标签筛选对未打标素材放行」不同：筛动态的目的就是找能用的，
          把未扫描的混进来会让"筛完还有一堆不认识的东西"。
        """
        self.dyn_filter = int(i or 0)
        self._sync_lib_controls()
        self.refresh_grid()

    def set_role_filter(self, i):
        self.role_filter = i
        self._sync_lib_controls()
        self.refresh_grid()

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

    def reveal_in_explorer(self, path):
        """在资源管理器里**打开并选中**该素材文件（素材右键「打开文件所在位置」）。

        ★ 2026-10-30 用户要求加这个入口。用 `explorer /select,<路径>` 而不是只开目录：
          素材库动辄几百个文件，只开目录还要自己翻。
        ⚠ `subprocess` 起 Windows 程序必须走 `_no_window_kwargs()`（项目铁律 12）——
          否则会闪一下黑色控制台窗口。
        """
        try:
            p = str(path or "")
            if not p or not os.path.isfile(p):
                QMessageBox.information(self, T("打开文件所在位置"),
                                        T("文件不存在（可能已被移动或删除）。"))
                return
            import subprocess
            kw = {}
            try:
                from lan_audio import _no_window_kwargs
                kw = _no_window_kwargs()
            except Exception:                                      # noqa: BLE001
                pass
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(p)], **kw)
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(p)], **kw)
        except Exception as e:                                     # noqa: BLE001
            QMessageBox.warning(self, T("打开文件所在位置"), str(e)[:200])

    def scan_tags(self, paths=None, force=False, quiet=False):
        """扫描素材的**动态三档 / 频闪 / 铺满度**（并据此自动分配前景/背景）。

        ★ 2026-10-30 用户「大砍刀」：CLIP 视觉打标（`TagScanThread`）已删除，
          这里改成唯一的 `DynTagScanThread`；亮度分档与曲风识别同样不再产出。

        `quiet=True`：不占用底部进度条（导入/启动时的**自动**补扫用 ——
        那时缩略图 worker 正在用同一个进度条，两边抢会一直跳）。
        """
        if getattr(self, "_tag_scan", None) is not None and self._tag_scan.isRunning():
            QMessageBox.information(self, T("扫描素材"), "已有扫描在进行中，请稍候。")
            return
        self._tag_scan = DynTagScanThread(self, paths, force=force)
        self._tag_scan.progress.connect(self._on_scan_progress)
        self._tag_scan.finished.connect(self._on_scan_done)
        self._scan_quiet = bool(quiet)
        if not quiet:
            self.thumb_bar.setVisible(True)
            self.thumb_bar.setRange(0, 1)
            self.thumb_bar.setValue(0)
        self._tag_scan.start()

    def _on_scan_progress(self, done, total):
        if getattr(self, "_scan_quiet", False):
            return
        self.thumb_bar.setRange(0, max(1, total))
        self.thumb_bar.setValue(done)

    def _on_scan_done(self):
        if not getattr(self, "_scan_quiet", False):
            self.thumb_bar.hide()
        self._scan_quiet = False
        self.refresh_grid()

    def _auto_scan_unscanned(self):
        """启动后**增量补扫**"未扫描"素材（只扫没标签的）。

        ★★ 2026-10-31（用户「素材库怎么不显示图片」）：`_populate_grid` 的「全部」档
          会跳过未扫描素材（用户要求「选择时就排除这些」）⇒ 没扫过 = 永远看不见。
          库里有 10 张图 + 6 个 LOGO 视频一直卡在这个状态（从来没人触发扫描）。
          ⇒ 启动 2.5 秒后补一次，扫完它们自动出现在「全部」。

        ⚠ 演出中不扫：会抢 CPU 影响画面（`ui_stall.log` 里已有停帧记录）。
        """
        try:
            if bool(getattr(getattr(self, "engine", None), "running", False)):
                return
            if getattr(self, "_tag_scan", None) is not None and self._tag_scan.isRunning():
                return
            todo = pick_unscanned(self.library)
            if not todo:
                return
            print("[素材] 补扫未扫描素材 %d 个（它们此前在「全部」里看不见）" % len(todo))
            self.scan_tags(todo, quiet=True)
        except Exception:                                          # noqa: BLE001
            pass

    def _on_dyn_scan_done(self):
        self.refresh_grid()

    def show_cache_info(self):
        from media_manager import thumb_cache_size, THUMB_DIR
        QMessageBox.information(
            self, T("缩略图缓存"),
            f"缓存目录：\n{THUMB_DIR}\n\n占用：{thumb_cache_size():.2f} MB\n素材数：{len(self.library)}\n\n"
            "缩略图首次生成后写入磁盘，之后启动/重建都直接读缓存。")

    def clear_thumb_cache(self):
        from media_manager import clear_thumb_cache as _clear
        n = _clear(self.library)
        self.refresh_grid(icons_only=True)
        # 清完立刻后台重生成，否则整个库都会变成「只有文字没有图」
        self._start_thumb_backfill()
        QMessageBox.information(self, T("缩略图缓存"),
                                Tf("已清理 {} 个缓存文件，正在后台重新生成缩略图。", n))

    def remove_media(self, item):
        """从素材库移除（同时从所有图层移除）"""
        self.remove_from_all_layers(item)
        self.library.pop(item.path, None)
        self.cfg["library"] = list(self.library.keys())
        self.cfg.save()
        # 自动图层兜底：清掉 clips 里已删除素材的残留（池子空了下次 tick 会重建）
        for i, lay in enumerate(self.engine.layers):
            if not getattr(lay, "auto_mode", False):
                continue
            if any(getattr(c, "path", None) == item.path for c in lay.clips):
                lay.clips = [c for c in lay.clips if getattr(c, "path", None) != item.path]
                lay.auto_clips = list(lay.clips)
                if lay.cur >= len(lay.clips):
                    lay.cur = -1
                    lay.prev_idx = -1
                self._layers_call("sync_layer", i)
        self.refresh_grid()
        self._fill_tag_combo()

    def confirm_remove_media(self, items):
        """从素材库移除（二次确认；可选同时删除本地文件，仅限「素材库」文件夹内）"""
        if not items:
            return
        box = QMessageBox(self)
        box.setWindowTitle( T("从素材库移除"))
        if len(items) == 1:
            box.setText(f"确定从素材库移除「{items[0].name[:40]}」吗？\n（图层中的该素材会一并移除，标签与摆位也会清除）")
        else:
            box.setText(f"确定从素材库移除 {len(items)} 个素材吗？\n（图层中的这些素材会一并移除，标签与摆位也会清除）")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.button(QMessageBox.Yes).setText( T("移除"))
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
                        QMessageBox.warning(self, T("删除失败"), f"无法删除文件：\n{ap}\n{e}")

    def open_import_settings(self):
        """打开导入方式设置（即使勾选了「记住我的选择」也可随时修改）"""
        strat = self._ask_import_strategy()
        if strat is None:
            return
        self.cfg["output"]["import_strategy"] = strat
        self.cfg.save()
        QMessageBox.information(
            self, T("导入素材设置"),
            {"copy": "已设置为：复制到素材库（原文件保留）",
             "move": "已设置为：移动到素材库（复制后删除原文件）",
             "link": "已设置为：仅引用（不复制，保留原路径）"}.get(strat, strat))

    # ================= 图层操作 =================
    def add_to_layer(self, layer_idx, items):
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        if getattr(lay, "auto_mode", False):
            return  # 自动图层素材由「角色 + 动态档」自动填充，不接受手动拖入
        for m in items:
            if m and m not in lay.clips:
                lay.clips.append(m)
        if lay.cur < 0 and lay.clips and lay.fixed < 0:
            self.engine._switch_layer_to(lay, 0, instant=True)
        self.save_layers()
        self._layers_call("sync_layer", layer_idx)

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
        self._layers_call("refresh_playing")

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
        self._layers_call("sync_layer", layer_idx)

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
        self._layers_call("sync_layer", from_idx)
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
        self._layers_call("sync_layer", layer_idx)

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

    def _cue_layer_menu(self, idx, lay, gpos):
        """码表图层的右键菜单 —— 只列**对它真正生效**的项。

        为什么单独一套（用户 2026-10-17：「码表图层的右键菜单应该和其他图层不一样」）：
          码表图层的画面 100% 来自 `.ercue` —— 片段何时进出、转场、混合、不透明度、
          crop、变速**全部在编排端定好**。ERA 这边只剩「容器级」的几个开关有意义。

        生效（列出）：
          · 重命名
          · 无声音时隐藏本层 —— `_compose` 对码表层同口径处理
          · Solo —— 同上
          · 上移 / 下移 —— 决定它与其它 ERA 图层的合成顺序
          · 卸下码表 —— 只清运行时（解码器全关），图层保留
          · 删除图层

        不生效或有害（**故意不列**）：
          · 固定播放本层当前素材 —— 码表没有"当前素材"，一帧里可能同时有多个子层
          · 自动匹配模式 / 图层角色 —— 会重建素材池，**直接把码表顶掉**
          · 播放速度 —— 每段素材的 `speed` 在码表里；这里的 `lay.speed` 没人读
          · 保留透明通道 —— 子层 alpha 由码表素材自身决定（`_CueSubLayer` 恒 True）
          · 锁定画面 —— 码表本来就不吃能量脉冲/漂移（`_draw_layer` 的 pulse 恒 1.0）
          · 复制素材池到新图层 / 重置本层设置 —— 没有素材池，语义不成立
        """
        m = QMenu(self)
        m.setToolTipsVisible(True)
        a_ren = m.addAction("重命名…")
        a_sil = m.addAction("无声音时隐藏本层")
        a_sil.setCheckable(True)
        a_sil.setChecked(bool(lay.hide_silent))
        a_solo = m.addAction("Solo")
        a_solo.setToolTip(T("只显示本层（码表以外的图层全部隐藏）"))
        a_solo.setCheckable(True)
        a_solo.setChecked(bool(getattr(lay, "solo", False)))
        m.addSeparator()
        a_up = m.addAction("上移")
        a_up.setToolTip(T("更靠上（合成顺序）"))
        a_dn = m.addAction("下移")
        a_dn.setToolTip(T("更靠下（合成顺序）"))
        m.addSeparator()
        a_unmount = m.addAction("卸下码表（变回普通图层）")
        a_unmount.setToolTip(T("清掉本次装载的码表，并把这个图层变回普通图层（会保存）；"
                              "下次启动不会再自动装回来"))
        m.addSeparator()
        a_del = m.addAction(theme.danger_icon(), "删除图层")   # ★ 红点标记危险项
        a_del.setEnabled(len(self.engine.layers) > 1)

        i18n.retranslate(m)     # 临时菜单：弹出前刷语言
        act = m.exec(gpos)
        if act is None:
            return
        if act == a_ren:
            self._cur_layer = idx
            self.layer_rename()
        elif act == a_sil:
            lay.hide_silent = not lay.hide_silent
            self._layers_call("refresh_header", idx)
            self.save_layers()
        elif act == a_solo:
            self.set_solo(idx, not bool(lay.solo))
        elif act == a_up:
            self.refresh_layer_row(idx, idx - 1)
        elif act == a_dn:
            self.refresh_layer_row(idx, idx + 1)
        elif act == a_unmount:
            # ★ 走**同一条**卸下路径（`cue_ui.unbind_layer`）：它负责清运行时 +
            #   `is_cuesheet=False` + 落盘。这里若自己去碰引擎的卸载方法，就成了"第二条
            #   卸下路径"，两处行为迟早分叉（GUI 线程直接关解码器也不合铁律 13）。
            try:
                from cue_ui import unbind_layer
                unbind_layer(self, lay)
            except Exception as e:                               # noqa: BLE001
                print("[码表] 卸下失败：%s: %s" % (type(e).__name__, e))
            self._layers_call("refresh_header", idx)
        elif act == a_del:
            self._cur_layer = idx
            self.layer_del()

    def layer_menu(self, idx, gpos, _no_exec=False):
        """图层右键菜单。`_no_exec=True` 只构建不弹出（离线取证用）。"""
        """图层右键菜单（**码表图层走另一套**，见 `_cue_layer_menu`）。"""
        if not (0 <= idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[idx]
        # ★★★ 2026-10-17（用户："码表图层的右键菜单应该和其他图层不一样"）：
        #   码表图层是**容器**，画面完全由码表决定（片段/转场/混合/不透明度都在 `.ercue` 里）。
        #   普通图层那套「固定素材 / 自动匹配 / 角色 / 播放速度 / 保留 alpha / 复制素材池」
        #   对它**要么无效、要么有害**（例如「自动匹配模式」会重建素材池 ⇒ 直接把码表顶掉）。
        #   ⇒ 走独立菜单，只留真正对码表生效的项。
        if getattr(lay, "is_cuesheet", False):
            self._cue_layer_menu(idx, lay, gpos)
            return
        m = QMenu(self)
        m.setToolTipsVisible(True)      # ★ 长条目拆短后，括号里的说明挪进悬停提示
        a_ren = m.addAction("重命名…")
        # ★ 2026-10-25：文案**固定**为「停止切换本层素材」，不再随状态改名。
        #   用户明确要求：前面已经打了勾，勾本身就表示"已停住"，文案跟着变反而让人以为
        #   是另一个功能（截图里"✓ 恢复切换本层素材"就是这么来的）。勾 = 已停止切换。
        _stopped = lay.fixed >= 0
        a_fix = m.addAction("停止切换本层素材")
        a_fix.setCheckable(True)
        a_fix.setChecked(_stopped)
        a_fix.setToolTip(T("本层停在这一张素材上，不再自动换；也挡不住你手动切"))
        # ★ 2026-10-25：「手动切换素材」**只放在这里**。
        #   素材右键菜单里曾也放了一份，和「播放此素材」重复（都是把本层切到某一格，
        #   一个指定、一个顺移），用户直接指出"是不是重复了" ⇒ 素材菜单里删掉。
        #   图层菜单没有"选哪一格"的能力，这一项才是唯一能单层前进一格的操作。
        a_step = m.addAction("手动切换素材")
        a_step.setToolTip(T("立刻切到本层的下一个素材（不影响其它图层与节奏）"))
        a_step.setEnabled(len(getattr(lay, "clips", []) or []) >= 2
                          and not bool(getattr(lay, "auto_mode", False))
                          and not bool(getattr(lay, "is_kv", False)))
        a_sil = m.addAction("无声音时隐藏本层")
        a_sil.setCheckable(True)
        a_sil.setChecked(bool(lay.hide_silent))
        a_pau = m.addAction("静音时暂停播放")
        a_pau.setCheckable(True)
        a_pau.setChecked(bool(getattr(lay, "pause_silent", False)))
        a_solo = m.addAction("Solo")
        a_solo.setToolTip(T("只显示本层"))
        a_solo.setCheckable(True)
        a_solo.setChecked(bool(getattr(lay, "solo", False)))
        m.addSeparator()
        a_auto = m.addAction("自动匹配模式")
        a_auto.setToolTip(T("按动态档给本层自动挑素材"))
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
        a_lock = m.addAction("锁定画面")
        a_lock.setToolTip(T("画面不随能量脉冲/漂移"))
        a_lock.setCheckable(True)
        a_lock.setChecked(bool(getattr(lay, "lock_motion", False)))
        a_alpha = m.addAction("保留透明通道")
        a_alpha.setToolTip(T("保留素材自带的 alpha 通道（透明区域）"))
        a_alpha.setCheckable(True)
        a_alpha.setChecked(bool(getattr(lay, "use_alpha", True)))
        m.addSeparator()
        a_up = m.addAction("上移")
        a_up.setToolTip(T("更靠上（合成顺序）"))
        a_dn = m.addAction("下移")
        a_dn.setToolTip(T("更靠下（合成顺序）"))
        a_copy = m.addAction("复制素材池到新图层")
        a_rst = m.addAction("重置本层设置")
        m.addSeparator()
        a_del = m.addAction(theme.danger_icon(), "删除图层")   # ★ 红点标记危险项
        a_del.setEnabled(len(self.engine.layers) > 1)
        # Kv 主视觉图层：不参与自动匹配/角色，也不允许复制（全局唯一）
        if getattr(lay, "is_kv", False):
            a_auto.setEnabled(False)
            role_menu.setEnabled(False)
            a_copy.setEnabled(False)

        i18n.retranslate(m)     # 临时菜单：弹出前刷语言
        if _no_exec:
            return m
        act = m.exec(gpos)
        if act is None:
            return
        if act == a_lock:
            lay.lock_motion = not bool(getattr(lay, "lock_motion", False))
            self._layers_call("refresh_header", idx)
            self.save_layers()
        elif act == a_alpha:
            lay.use_alpha = not bool(getattr(lay, "use_alpha", True))
            self._layers_call("refresh_header", idx)
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
        elif act == a_step:
            try:
                self.engine.manual_switch_layer(idx)
                self._layers_call("refresh_playing")
            except Exception:                                    # noqa: BLE001
                pass
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
            self._layers_call("refresh_header", idx)
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
            self._layers_call("refresh_header", idx)
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
            self.engine._refresh_auto_layers(self.audio.state.snapshot(), force=True)
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
            self.engine._refresh_auto_layers(self.audio.state.snapshot(), force=True)
        self._layers_call("refresh_header", idx)
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
        # ★ 交换可能把某个图层顶到 KV 上面 ⇒ 立刻纠正（表现为"KV 不能被越过"）
        self._enforce_kv_top()
        self._sync_layers()

    @staticmethod
    def _layer_insert_index(is_kv, is_cue, n):
        """新建图层的插入位置（None = 追加到末尾）。

        · Kv 主视觉层   ⇒ 0（最上层）
        · 码表图层      ⇒ 0（随后的 `_enforce_kv_top()` 把 KV 顶回 0，它就落在 **1**，
                          也就是"除 KV 以外的最上层"）
        · 其它图层      ⇒ None（追加到最底，保持原有习惯）

        ★ 单独抽出来是为了能无头断言（`tools/cue_layer_order_test.py`），
          否则这条规则只能靠"点开界面看一眼"。
        """
        if is_kv or is_cue:
            return 0
        return None

    def _enforce_kv_top(self):
        """KV 图层**永远**在 index 0（最上层），其余图层相对顺序不变。

        为什么需要（用户 2026-10-17「kv 图层永远强制置顶」）：
          只在新建时 `insert(0)` 是不够的 —— 复制、上移、删除、读档都能把它挤下去。
          而 KV 是"待机层"（无声音切入 / 有声音切出），一旦被别的图层盖住就等于失效，
          而且**从界面上看不出来**（图层还在，只是被压住了）。
        ⚠ 用"按对象身份重定位 `_cur_layer`"的方式，避免搬运后选中项跳到别的层。
        """
        try:
            ls = getattr(self.engine, "layers", None)
            if not ls:
                return
            kv = -1
            for i, l in enumerate(ls):
                if getattr(l, "is_kv", False):
                    kv = i
                    break
            if kv <= 0:              # 没有 KV（-1）或已在顶层（0）⇒ 什么都不做
                return
            cur = int(getattr(self, "_cur_layer", 0) or 0)
            cur_obj = ls[cur] if 0 <= cur < len(ls) else None
            ls.insert(0, ls.pop(kv))
            if cur_obj is not None:
                try:
                    self._cur_layer = ls.index(cur_obj)
                    return
                except ValueError:
                    pass
            self._cur_layer = 0
        except Exception:                                        # noqa: BLE001
            pass

    def _copy_layer_pool(self, idx):
        lay = self.engine.layers[idx]
        new = Layer(lay.name + " 副本")
        new.clips = list(lay.clips)
        new.opacity, new.blend = lay.opacity, lay.blend
        new.speed, new.hide_silent = lay.speed, lay.hide_silent
        new.pause_silent = lay.pause_silent
        new.fixed = lay.fixed
        self.engine.layers.insert(idx, new)
        self._enforce_kv_top()
        self._sync_layers()

    def clip_menu(self, layer_idx, media, gpos, _no_exec=False):
        """素材格右键菜单。

        `_no_exec=True`：只**构建**菜单并返回，不弹出（供离线 UI 取证脚本截图，
        避免在无窗口环境里 `QMenu.exec()` 永久阻塞）。日常路径永远是 False。
        """
        if not (0 <= layer_idx < len(self.engine.layers)):
            return
        lay = self.engine.layers[layer_idx]
        is_auto = getattr(lay, "auto_mode", False)
        m = QMenu(self)
        a_play = m.addAction("播放此素材")
        a_play.setEnabled(media is not None and not is_auto)   # 自动图层素材由自动匹配管理
        # ★ 2026-10-25（用户指出重复后删掉）：这里**不再放「手动切换素材」**。
        #   素材菜单的每一项都是"对**这一格**做事"，而「播放此素材」已经能把这层切到
        #   任意一格 —— 再挂一个"顺移下一个"就是同义重复，菜单里并排两项反而让人犹豫。
        #   「手动切换素材」只留在**图层右键菜单**：那里没有"选哪一格"的能力，
        #   它才是唯一能单层前进一格的操作（不重复，也不缺能力）。
        a_size = m.addAction("素材大小与位置…")
        a_size.setEnabled(media is not None)
        # ★ 2026-10-25（用户要求）：菜单文案改成「停止切换本层素材」——
        #   它做的事就是"这一层停在这张图上"，不是"设为默认素材"，旧文案容易被理解反。
        #   文案**固定不变**：已停住时前面就是个勾（勾 = 已停止切换），
        #   别再改成"恢复切换"——那会让人以为点了是另一个功能。
        _fixed_here = (media is not None and lay.fixed >= 0
                       and 0 <= lay.fixed < len(lay.clips) and lay.clips[lay.fixed] is media)
        a_fix = m.addAction("停止切换本层素材")
        a_fix.setCheckable(True)
        a_fix.setEnabled(not is_auto)
        a_fix.setChecked(_fixed_here)
        m.addSeparator()
        a_rm = m.addAction(theme.danger_icon(), "从本层移除")
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
        if _no_exec:
            return m
        act = m.exec(gpos)
        if act is None:
            return
        if act == a_size and media is not None:
            from panels import ImageAdjustDialog
            ImageAdjustDialog(self, media).exec()
        elif act == a_play and media is not None:
            try:
                self.engine._switch_layer_to(lay, lay.clips.index(media))
                self._layers_call("refresh_playing")
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
                    self._layers_call("sync_layer", layer_idx)
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
        name, auto, role, is_kv, is_cue = dlg.result_data()
        # Kv 主视觉图层：全局唯一，已存在则提醒并中止
        if is_kv and self.engine.kv_layer() is not None:
            QMessageBox.information(
                self, T("Kv 主视觉图层已存在"),
                "Kv 主视觉图层全局只能有一个。\n请先删除现有的 Kv 图层，再添加新的。")
            return
        # ★ 码表图层：**先选表再建层**（用户要求：新建时就要能选"应用哪一张码表"）。
        #   没有码表 ⇒ 直接提示去第 4 页签导入，不建一个空壳层（避免用户看到"码表图层但不接管"）。
        sheet_entry = None
        if is_cue:
            try:
                import cue_page
                sheet_entry = cue_page.pick_sheet(
                    self, self, T("新建「码表图层」—— 选择这张图层要应用的码表："))
            except Exception as e:                                # noqa: BLE001
                QMessageBox.warning(self, T( T("码表")),
                                    Tf("打开码表列表失败：{}", "%s: %s"
                                       % (type(e).__name__, e)))
                return
            if sheet_entry is None:
                return
        lay = Layer((name or ("Kv 主视觉" if is_kv
                              else ("码表图层" if is_cue else default))).strip())
        lay.auto_mode = auto
        lay.is_kv = is_kv
        lay.role = role
        # 码表图层是"容器层"：画面由码表接管 ⇒ 先标好类型与绑定的包名
        # （`Layer.to_dict` 已持久化这两个字段，见 engine.py）
        if is_cue:
            lay.is_cuesheet = True
            lay.cuesheet_id = os.path.basename(str(sheet_entry.get("path") or ""))
        # 「自动图层应该跟着音乐走」：新建**自动**图层默认勾选「无声音时隐藏本层」。
        # 仅限自动图层；手动图层保持现有默认（不勾选）；Kv 待机层本就有独立的静音逻辑
        # （无声音切入），不能叠加 hide_silent，故排除。
        # ⚠ 取舍说明：用户原话「添加两个自动图层时自动勾选」也可能指"只有当已存在 ≥1 个
        #   自动图层时再添加才勾选"。这里按**更符合常理**的理解实现——只要新建的是自动图层
        #   就默认勾选（自动图层本就应随音乐静音而隐藏）。若要改成"第二条自动图层才勾选"，
        #   只需在此处加 `and any(getattr(l, "auto_mode", False) for l in self.engine.layers)`。
        if auto and not is_kv:
            lay.hide_silent = True
        if role == "fg" and not is_kv:
            lay.blend = "add"   # 前景（特效/覆盖）默认用"添加"混合，叠加发光才正常
        _at = self._layer_insert_index(is_kv, is_cue, len(self.engine.layers))
        if _at is None:
            self.engine.layers.append(lay)   # 其它图层：新建在已有图层下面（最底层）
        else:
            self.engine.layers.insert(_at, lay)
        if is_kv:
            self.engine._kv_last_t = time.perf_counter()
        # ★ KV 永远强制置顶（新建/复制/上移/删除/读档都会走这里兜底）
        self._enforce_kv_top()
        if auto:
            # 立即匹配一次（绕过 3s 间隔），让新建自动图层马上有画面
            self.engine._refresh_auto_layers(self.audio.state.snapshot(), force=True)
        try:
            self._cur_layer = self.engine.layers.index(lay)
        except ValueError:
            self._cur_layer = 0
        self._sync_layers()
        # 码表图层：装表（导入/校验/绑曲/素材目录/报错全走 cue_ui 的**唯一实现**）
        if is_cue and sheet_entry is not None:
            try:
                import cue_ui
                _lv = []
                _ok, msg = cue_ui.load_and_bind(
                    self, str(sheet_entry.get("path") or ""), lay, parent=self,
                    out_level=_lv)
            except Exception as e:                                # noqa: BLE001
                _ok, msg = False, "装载码表失败：%s: %s" % (type(e).__name__, e)
                _lv = []
            self._layers_call("refresh_header", self._cur_layer)
            try:
                self.save_layers()
            except Exception:                                     # noqa: BLE001
                pass
            page = getattr(self, "cue_panel", None)
            fn = getattr(page, "refresh", None)
            if callable(fn):
                try:
                    fn()
                except Exception:                                 # noqa: BLE001
                    pass
            if msg:
                # ★ 2026-11-02：成功 + 补充说明用信息图标，只有真问题才用警告
                if _lv and _lv[0] == "info":
                    QMessageBox.information(self, T(T("码表")), msg)
                else:
                    QMessageBox.warning(self, T(T("码表")), msg)

    def layer_del(self):
        if len(self.engine.layers) <= 1 or not (0 <= self._cur_layer < len(self.engine.layers)):
            return
        lay = self.engine.layers.pop(self._cur_layer)
        lay.close_all()
        self._cur_layer = 0
        self._enforce_kv_top()
        self._sync_layers()

    def layer_move(self, d):
        self.refresh_layer_row(self._cur_layer, self._cur_layer + d)

    def layer_rename(self):
        if not (0 <= self._cur_layer < len(self.engine.layers)):
            return
        lay = self.engine.layers[self._cur_layer]
        name, ok = QInputDialog.getText(self, T("重命名图层"), tr("layer_name"), text=lay.name)
        if ok and name.strip():
            lay.name = name.strip()
            self._layers_call("refresh_header", self._cur_layer)
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
        self.refresh_all_layer_blocks()
        self._cur_layer = min(self._cur_layer, max(0, len(self.engine.layers) - 1))
        # Kv 设置区随 Kv 图层增删显隐
        try:
            self.settings.sync_kv_section()
        except Exception:
            pass

    @staticmethod
    def _log_ui_error(where, exc=None):
        """把 UI 层被**吞掉**的异常写一行到 `ui_stall.log`（带函数名 + 堆栈）。

        ★ P0 BUG-1 的教训：`_auto_size_blocks_split` 里一个 `NameError` 被外层
        `except: pass` 吞了整整一轮，"块区高度不再自动分配"这种症状完全静默。
        从此这几个调用点至少留一条日志，别再无声无息。
        """
        try:
            import traceback
            base = None
            try:
                base = config.app_base_dir()
            except Exception:                                     # noqa: BLE001
                base = tempfile.gettempdir()
            path = os.path.join(str(base), "ui_stall.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s: %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                           where, exc))
                if exc is not None:
                    f.write(traceback.format_exc() + "\n")
        except Exception:                                         # noqa: BLE001
            pass

    def _sync_multi_controls(self):
        """★ S1 #1（用户第 1 条）：VJ 运行中**禁用**「多屏输出」开关与「模式」下拉。

        为什么：运行中切多屏/切单路会重建整条输出计划（发送端/画布全重建），
        现场一按就可能黑一下或掉帧。停止 VJ 后恢复可用。
        判据（QA 会验）：运行中两个控件都是灰的、点了没反应，tooltip 为「停止 VJ 后才能切换」。
        """
        try:
            running = bool(self.engine.running)
        except Exception:                                         # noqa: BLE001
            running = False
        try:
            self.btn_multi_out.setEnabled(not running)
            self.btn_multi_out.setToolTip(
                T("停止 VJ 后才能切换") if running else
                T("关闭：普通演出台（原图层/预览/素材库）\n"
                  "打开：多屏输出工作区 + 同一套图层和素材库"))
        except Exception:                                         # noqa: BLE001
            pass
        st = getattr(self, "output_studio", None)
        if st is not None:
            try:
                st.mode_combo.setEnabled(not running)
                st.mode_combo.setToolTip(
                    T("停止 VJ 后才能切换") if running else
                    T("多路：每块屏幕独立 NDI/Spout 输出\n"
                      "单路：多屏拼成一张大画布，只发一路（在「输出画布…」里排布）"))
            except Exception:                                     # noqa: BLE001
                pass

    def _on_auto_layers_changed(self, idxs):
        """自动图层素材池被重算：刷新对应行素材格 + 播放高亮"""
        for i in (idxs or []):
            self._layers_call("sync_layer", i)
        self._layers_call("refresh_playing")

    # ================= 输出/语言 =================
    def show_output(self):
        self.out_win.show()
        self.out_win.raise_()
        self.apply_output_screen()
        try:
            self.engine.set_frame_consumers(
                bool(self.preview_panel.preview_on()) or self.out_win.isVisible())
        except Exception:
            pass

    def open_hotkeys(self):
        HotkeyDialog(self).exec()

    def open_about(self):
        AboutDialog(self).exec()

    def export_logs(self):
        """「⋯ 更多 → 导出全部日志…」：把日志 + config 打成 zip（2026-10-18 用户要求）。

        打包内容：
          · `%LOCALAPPDATA%\\AutoVJ\\*.log`（存在的全打。源码运行时是项目根）
          · config.json（现场设置是排障关键：音源 / 输出 / 快捷键都在里面）
          · environment.txt（BUILD_VER / 导出时间 / 系统 / Python / 数据目录）
        失败**必须弹窗**（不要静默 —— 用户会以为导出了、拿着空文件来回传）。
        """
        import platform as _platform
        import sys as _sys
        import zipfile

        base = os.path.dirname(CONFIG_FILE)
        stamp = time.strftime("%Y%m%d_%H%M")
        # 默认目录 = 桌面。用 QStandardPaths 而不是 ~/Desktop：桌面被 OneDrive 重定向时
        # 后者会指向一个空目录（用户找不到文件，还以为没导出）。
        from PySide6.QtCore import QStandardPaths
        desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        if not desktop or not os.path.isdir(desktop):
            desktop = os.path.expanduser("~")
        path, _sel = QFileDialog.getSaveFileName(
            self, T("导出全部日志"),
            os.path.join(desktop, "AutoVJ日志_%s.zip" % stamp),
            T("Zip 压缩包 (*.zip)"))
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"

        files = []
        try:
            if os.path.isdir(base):
                for fn in sorted(os.listdir(base)):
                    fp = os.path.join(base, fn)
                    if fn.lower().endswith(".log") and os.path.isfile(fp):
                        files.append(fp)
            if os.path.exists(CONFIG_FILE):
                files.append(CONFIG_FILE)
            import version as _v
            env = "\n".join([
                "BUILD_VER: %s" % getattr(_v, "APP_VERSION", "dev"),
                "BUILD_DATE: %s" % getattr(_v, "APP_BUILD", ""),
                "EXPORTED_AT: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                "OS: %s" % _platform.platform(),
                "PYTHON: %s" % _platform.python_version(),
                "FROZEN: %s" % bool(getattr(_sys, "frozen", False)),
                "DATA_DIR: %s" % base,
                "LOGS: %d" % len(files),
            ]) + "\n"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for fp in files:
                    try:
                        z.write(fp, os.path.basename(fp))
                    except Exception as _e:                    # noqa: BLE001
                        # 单个日志被占用/权限不足不整包失败，但要把原因写进 zip
                        z.writestr("SKIPPED_%s.txt" % os.path.basename(fp), str(_e))
                z.writestr("environment.txt", env)
        except Exception as e:                                 # noqa: BLE001
            QMessageBox.warning(self, T( T("导出全部日志")),
                                T("导出失败：") + "%s" % e)
            return
        QMessageBox.information(
            self, T( T("导出全部日志")),
            (T("已导出 %d 个日志/配置文件到：") % len(files)) + "\n\n" + path)

    def open_cuesheet(self):
        """码表（演出端）：轻量对话框（导入 / 最近 / 状态 / 绑图层 / 手动起拍 / 卸下）。

        UI 逻辑全在 `src/cue_ui.py`：这里只做"打开"，避免主窗继续膨胀。
        失败必须可见（不许静默）—— 对话框打不开也要弹一句。

        ⚠ 入口：第 4 页签「码表」→「更多操作…」。**「⋯ 更多」里的旧菜单项已删除**
          （用户 2026-10 明确要求），但对话框本身**保留**：手动起拍/最近文件都在里面。
        """
        try:
            import cue_ui
            cue_ui.open_cuesheet_dialog(self)
        except Exception as e:                                    # noqa: BLE001
            QMessageBox.warning(self, T("码表"), "打开码表对话框失败：%s: %s"
                                % (type(e).__name__, e))

    def open_cuesheet_page(self):
        """跳到第 4 个页签「码表」（供其它入口/脚本调用）。"""
        self.switch_page(3)

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
        for panel in (getattr(self, "output_panel", None),
                      getattr(self, "cue_panel", None)):
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
            self._sync_multi_controls()      # ★ S1 #1：启动/切语言时也按运行状态上锁
        except Exception:                                         # noqa: BLE001
            pass
        try:
            self._layers_call("refresh_header")      # 图层名 tooltip / 不透明度 / 角色按钮
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
        self._sync_more_menu()

    def _sync_more_menu(self):
        """「⋯」收纳菜单的文案与勾选态（菜单项不参与控件树 retranslate，必须显式同步）。

        由 `_sync_theme_btn()` 带出来 ⇒ 切主题、切语言、retranslate_ui 三条路径都会走到。
        """
        try:
            self.act_theme.setText(T("☀ 亮色") if theme.current() == "dark" else T("🌙 暗色"))
            self._lang_menu.setTitle(tr("language"))
            self.act_lang_zh.setChecked(i18n.lang() == "zh")
            self.act_lang_en.setChecked(i18n.lang() != "zh")
            self.act_hotkey.setText(tr("hotkeys"))
            self.act_export_logs.setText(T("导出全部日志…"))
            self.act_about.setText(tr("about"))
            self.btn_more.setToolTip(T("更多：主题 / 语言 / 快捷键 / 导出日志 / 关于"))
        except Exception:
            pass

    # ================= 预览/状态 =================
    def moveEvent(self, e):
        # ★ 2026-11-08：拖动窗口不再暂停预览。旧实现每次 moveEvent 都把
        # `_drag_pause_until` 推到 0.25s 后，连续拖动 = 预览一直不刷新（看起来卡死）。
        # 预览 0.25 分辨率下每帧只有约 1ms，正常跟着画即可。
        super().moveEvent(e)

    def resizeEvent(self, e):
        # 缩放窗口同样不再暂停预览；重活（布局/缩略图）由各自的定时器节流。
        super().resizeEvent(e)

    def _on_engine_frame(self, seq=-1):
        """引擎帧到达：**只收到一个序号**，帧去引擎侧取最新的。

        为什么这么改（2026-09-26 内存雪崩）：旧写法每帧把一个 8MB 的 QImage 投进主线程队列，
        主线程一卡就积压（实测 30 秒 +3.2GB，涨到 15GB 把整机拖垮）。
        现在队列里只有 int；重复序号（队列积压时会有很多）直接跳过，不重复绘制。

        ★★ 2026-11-08 二修（用户："显示器输出码表演出还是掉帧"）：
        旧实现把绘制丢给 `QTimer.singleShot(0, ...)`，原意是合并同一轮的多条通知。
        实测 1920×1080 输出窗口时，**零毫秒定时器会被连续的重绘事件饿死**：
        30fps 的引擎只画出 20fps，`[OUT]` 间隙 p95 112ms、max 269ms。
        改为在信号回调里**直接画**：信号本身已经排队到 GUI 线程，天然串行；
        积压时第一个回调画最新帧，后面的回调按 `seq <= _drawn_seq` 直接跳过，
        合并效果不变，但不再多经一层定时器。实测同一场景：30.0 draws/s、
        间隙 p95 48ms、max 84ms（无 >100ms）。
        """
        try:
            seq = int(seq)
        except Exception:
            seq = -1
        if seq <= self._drawn_seq:
            return
        # 防御：万一将来从非 GUI 线程发来，仍然编组回 GUI 线程，绝不跨线程画控件。
        try:
            if QThread.currentThread() is not self.thread():
                if not self._draw_scheduled:
                    self._draw_scheduled = True
                    QTimer.singleShot(0, self._draw_latest_frame)
                return
        except Exception:
            pass
        self._pending_seq = seq
        self._draw_latest_frame()

    def _covered_by_output(self):
        """输出窗口是否**完全盖住主窗**（全屏同屏 / 无边窗口包住主窗）。

        ★ 2026-11-08 二修：主窗被盖住时，预览缩放和 500ms 状态刷新全是白烧
          GUI 线程（预览 4~9ms/帧、状态 7ms/次），而用户根本看不见。
          缓存 0.4s，窗口拖动/切换全屏后最多 0.4s 自动跟上。
        """
        now = time.perf_counter()
        if now - float(getattr(self, "_cover_check_t", 0.0) or 0.0) < 0.4:
            return bool(getattr(self, "_cover_main", False))
        self._cover_check_t = now
        cover = False
        try:
            ow = getattr(self, "out_win", None)
            if ow is not None and ow.isVisible() and self.isVisible() and not self.isMinimized():
                same_screen = False
                try:
                    s1, s2 = ow.screen(), self.screen()
                    same_screen = bool(s1 is not None and s2 is not None
                                       and s1.geometry() == s2.geometry())
                except Exception:
                    same_screen = False
                # 几何上盖住还不够：主窗可能被 Alt+Tab 提到输出窗上面（输出未置顶时）。
                # 取主窗中心点，看实际最上层窗口是不是输出窗 —— 只做一次，缓存 0.4s。
                on_top = False
                try:
                    pt = self.mapToGlobal(self.rect().center())
                    wgt = QApplication.widgetAt(pt)
                    on_top = (wgt is not None and wgt.window() is ow)
                except Exception:
                    on_top = False
                if on_top:
                    if ow.isFullScreen():
                        cover = same_screen
                    else:
                        cover = ow.frameGeometry().contains(self.frameGeometry())
        except Exception:
            cover = False
        self._cover_main = cover
        return cover

    def _preview_covered(self):
        """预览面板中心点是否被输出窗盖住（输出窗窗口化/二屏也能正确判断）。

        用 `QApplication.widgetAt` 做真实 Z 序点测试：比整窗几何包含宽松，
        也不会把"主窗被 Alt+Tab 提到输出窗上面"误判成被盖住。缓存 0.4s。
        """
        now = time.perf_counter()
        if now - float(getattr(self, "_preview_cover_t", 0.0) or 0.0) < 0.4:
            return bool(getattr(self, "_preview_cover", False))
        self._preview_cover_t = now
        got = False
        try:
            ow = getattr(self, "out_win", None)
            pv = getattr(getattr(self, "preview_panel", None), "view", None)
            if (ow is not None and pv is not None and ow.isVisible()
                    and pv.isVisible() and pv.width() > 1 and pv.height() > 1):
                pt = pv.mapToGlobal(pv.rect().center())
                wgt = QApplication.widgetAt(pt)
                got = bool(wgt is not None and wgt.window() is ow)
        except Exception:
            got = False
        self._preview_cover = got
        return got

    def _draw_latest_frame(self):
        self._draw_scheduled = False
        seq = self._pending_seq
        if seq == self._drawn_seq:
            return
        now = time.perf_counter()
        gap = (now - self._last_draw_t) if self._last_draw_t else 0.0
        backlog = seq - self._drawn_seq if self._drawn_seq >= 0 else 0
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
        self._update_preview(img, allow=not self._preview_covered())
        try:
            os_obj = getattr(self, "output_studio", None)
            if os_obj is not None:
                os_obj.on_frame()
        except Exception:
            pass
        self._last_draw_t = time.perf_counter()

        # 真实的「显示帧间隔」取证：短于 120ms 的抖动不记；每 2 秒最多记一行，
        # 行内带这一窗口内的最差值 / 次数 / backlog / 视图后端 / 引擎 tick 间隔。
        try:
            if gap > 0.12 and bool(getattr(self.engine, "running", False)):
                self._out_gap_max = max(self._out_gap_max, gap)
                self._out_gap_n += 1
                if now - self._out_gap_log_t >= 2.0:
                    snap = {}
                    try:
                        snap = self.engine.ui_snapshot() or {}
                    except Exception:
                        pass
                    view = "?"
                    w = h = 0
                    if ow is not None:
                        try:
                            view = ow.debug_backend()
                            w, h = ow.width(), ow.height()
                        except Exception:
                            pass
                    import stallwatch
                    stallwatch.log_line(
                        "[OUT] 显示帧间隔 %.0fms（2s 最差 %.0fms，>120ms 共 %d 次）"
                        " backlog=%d 视图=%s 窗口=%dx%d tick=%.0fms tick_gap=%.0fms"
                        % (gap * 1000.0, self._out_gap_max * 1000.0, self._out_gap_n,
                           max(0, backlog), view, w, h,
                           float(snap.get("tick_ms", -1) or -1),
                           float(snap.get("tick_gap_ms", -1) or -1)))
                    self._out_gap_log_t = now
                    self._out_gap_max = 0.0
                    self._out_gap_n = 0
        except Exception:
            pass

    def _update_preview(self, img, allow=True):
        self.preview_panel.set_frame(img, allow=allow)
        # ⚠ 播放高亮**不在这里刷**：已交给独立的 1 秒定时器（`self._hl_timer`）。
        #   这条旧通路是"每 6 帧 ≈10fps 重建整张高亮表"，而预览帧在换页/隐藏时根本不来
        #   ⇒ 既白烧 CPU（图层多时每轮遍历所有格子）又不可靠。别再搬回来。

    def _bpm_text(self, snap, prefix="BPM "):
        """BPM 显示文案。**永远只显示原曲 BPM**，变速倍率另由 `_rate_text` 拼在后面。

        用 bpm_display（未锁定也可有值）而不是 bpm（只有锁定值）。
        `bpm` 只在「众数 ∈ [115,185] 且足够集中」时才 >0，所以慢歌 / 极快曲 /
        被估成半速的曲子会**一直显示 0**（用户 2026-09-24 报的现象）。
        `bpm_display` 由票数≥10 且集中度≥0.40 就发布，未锁定时前面加「≈」区分。

        ★★ 2026-10-28（用户报「本地分析 185 / 实时 182~183」）：
          认出歌以后优先显示 **`lib_bpm`（库内 BPM，离线整曲细扫）**——
          实时自相关在 fps≈86 时 1 帧就有 3.6 BPM 粒度，收敛值会低 1~2 BPM
          （实测 185.00 vs 181.5，集中度 4.02 vs 1.35、命中率 52% vs 31%）。
          库内值更准且与网格一致，所以不再挂「≈」。

        ★★★ 2026-11-03 第一轮（用户报「显示 140 不对呀，你肯定要按照真实来显示啊」）：
          当时把 `lib_bpm × r` 乘出来写进 BPM 位（1.20× ⇒ 167.5）。
          **此方案已被推翻**，见下一段。

        ★★★ 2026-11-03 第三轮（用户：「记得看 KVJ 是怎么搞的」）：
          竞品 `kumoSleeping/KVJ`（同作者的 QML + Rust 播放器）把**两个数分开**：
            · 跟随条：`"×" + rate.toFixed(3)`        ⇒ `×1.196`
            · 「对速」助手：`percent(rate)`          ⇒ `+19.6%`
            · BPM 只出现在时间轴的「BPM 段」，**永远是原曲值**
            · 文档原话：「最近 3 秒的观测拟合成一条直线，斜率就是速度；
              差不到 1% 不改速度、只修位置」
          用户选 A 方案 ⇒ 本函数回到**直接返回 `lib`（原曲值）**，
          倍率后缀由 `_rate_text` 负责（两处调用点各自拼一次）。
          这样做的好处：变速时 BPM 位不再跳小数，
          「原曲多快」和「推了多少」两个信息都还在，且各自语义干净。

        ⚠ 只有**演出台的 HUD 和底部状态栏**走这里；码表页 / 曲目页 / 素材库里的
          BPM 是**编辑语义**，必须保持原曲值（现在天然一致了，别再去乘 r）。

        健壮性（本函数挂在 500ms 的 QTimer 上，抛一次异常界面就停更）：
          `snap` 非 dict / 缺键 / 值为 None / 非数 一律安全回落，
          最差情况退化成「只显示兜底值」，绝不让异常冒出去。

        ★★★ 2026-11-03 第二轮（真机 energy CSV 取证）：**库内 BPM 短时保持**。
          1.20× 相位 19:11–19:13 逐秒核对：`lib_bpm` 163 行里 93 行 = 140、**70 行 = 0**
          （43% 时间为 0），跳变是**穿插**的（19:12:27 掉 → 19:12:28 回来 → 19:12:37 又掉）。
          后果：同一首歌同一个 BPM，显示会在 `140.0`（lib）/ `168`（兜底锁定）/
          `--`（兜底无值）三种**形态**之间反复闪 —— 位数和小数点在跳，
          现场盯着看很明显（用户抓的正是"闪"）。
          做法：`lib > 0` 时把 `(值, 墙钟, song_id)` 记在实例上；`lib <= 0` 且
          「距上次 ≤ `_BPM_LIB_HOLD_SEC` 秒」且「还是同一首歌」⇒ 沿用上次的 lib。
          超时 / 换歌 ⇒ 立刻放弃保持，逐字走下面的原兜底分支。
          ⚠ 保持的是「原曲 BPM」这个属性 —— 与是否变速、倍率多少**无关**。
          ⚠ 计时用墙钟 `time.time()`；时钟回拨（`ts > now`）或 `ts <= 0` 一律**不保持**。
          ⚠ 保持值只在 `MainWindow` 实例上、只在 GUI 线程读写 ⇒ 不加锁、不开线程。
        """
        if not isinstance(snap, dict):
            snap = {}
        try:
            lib = float(snap.get("lib_bpm") or 0.0)
        except (TypeError, ValueError):
            lib = 0.0
        # ---------------- 库内 BPM 短时保持（详见 docstring 第二轮） ----------------
        # `recognized_song_id` 用于「换歌立即失效」：绝不许把上一首的 BPM 挂在新歌上。
        sid_now = snap.get("recognized_song_id", -1)
        try:
            _hold = getattr(self, "_bpm_lib_hold", None)
        except Exception:                                        # noqa: BLE001
            _hold = None
        now = time.time()
        if lib > 0:
            # 真值到了：直接用，并记下「值 + 墙钟 + 歌 id」
            try:
                self._bpm_lib_hold = (lib, now, sid_now)
            except Exception:                                    # noqa: BLE001
                pass
        else:
            # 掉 0：`_BPM_LIB_HOLD_SEC`（4.5s）内沿用上次的值。四个条件缺一不可：
            #   h_lib > 0   —— 没有有效值就没得保持
            #   0 < h_ts    —— ts 为 0/负数说明没有记录（也挡住时钟回拨到 0 的情况）
            #   h_ts <= now —— 时钟回拨（记录时间在未来）⇒ 不信任，宁可不保持
            #   h_sid == sid_now —— 换歌立即失效
            try:
                h_lib, h_ts, h_sid = _hold
                if (float(h_lib) > 0 and 0.0 < float(h_ts) <= now
                        and (now - float(h_ts)) <= _BPM_LIB_HOLD_SEC
                        and h_sid == sid_now):
                    lib = float(h_lib)
            except (TypeError, ValueError):
                pass
        if lib > 0:
            # ★ 第三轮：**原曲值，不乘倍率**。库内值本身准 ⇒ 不挂「≈」。
            #   倍率后缀由调用点用 `self._rate_text(snap)` 拼上（两处，各自一次）。
            return "%s%.1f" % (prefix, lib)
        v = snap.get("bpm_display") or snap.get("bpm") or 0.0
        if not v:
            return prefix + "--"
        if snap.get("bpm_locked"):
            return "%s%.0f" % (prefix, v)
        return "%s≈%.0f" % (prefix, v)

    def _rate_text(self, snap, prefix=" ×"):
        """变速倍率后缀，形如 ` ×1.196`。没变速 / 没测到 ⇒ 返回空串（不占地方）。

        KVJ 口径（3 位小数，与它跟随条的 `"×" + rate.toFixed(3)` 对齐）。
        调用点形如 `self._bpm_text(snap) + self._rate_text(snap)`，所以这里**自带前导空格**，
        空串时整段消失，不会在 BPM 后面留一个孤零零的空格。

        规则（缺一不可）：
          · `recognized_rate` 缺失 / None / 非数（含 bool、字符串）/ NaN ⇒ `""`
          · 越界（不在 `[_BPM_RATE_MIN, _BPM_RATE_MAX]`）⇒ `""`（当没测到，不显示假值）
          · `|r - 1.0| < _BPM_RATE_DEADBAND`（1%）⇒ `""`（和 KVJ「差不到 1% 不改速度」对齐）
          · 否则 `" ×%.3f" % r`，例如 ` ×1.196` / ` ×0.796`

        ⚠ **不做保持**：倍率是测出来的**瞬时量**，掉了就消失 —— 显示一个过期的假倍率
          比不显示更糟（DJ 已经推回 1.0× 了，HUD 还挂着 ×1.196 会直接误导手上动作）。
          BPM 位之所以要保持，是因为「原曲 BPM」是**歌曲属性**，没测到时它仍然为真；
          两者语义不同，别把 `_bpm_lib_hold` 那套搬过来。

        ⚠ 本函数与 `_bpm_text` 同挂 500ms QTimer ⇒ 只读快照字典，
          **不做 IO、不循环、不加锁、不开线程**，异常一律吞掉返回空串（抛一次界面就停更）。
        """
        if not isinstance(snap, dict):
            return ""
        try:
            r = snap.get("recognized_rate", 1.0)
            # ⚠ 只接受**真正的数值**：快照里 `recognized_rate` 一直是 float，
            #   出现字符串说明上游另有问题 —— 显示层不猜，一律当「没测到」。
            #   （`bool` 也排除：`True` 当 1.0 只是碰巧对，语义上不是倍率。）
            if isinstance(r, bool) or not isinstance(r, (int, float)):
                return ""
            r = float(r)
            if r != r:                                  # NaN（非数）
                return ""
            if not (_BPM_RATE_MIN <= r <= _BPM_RATE_MAX):
                return ""                               # 越界 / 中间态 / inf
            if abs(r - 1.0) < _BPM_RATE_DEADBAND:
                return ""                               # 1% 以内：不动
            return "%s%.3f" % (prefix, r)
        except (TypeError, ValueError):
            return ""

    def _update_status(self):
        # ★ 2026-11-08 二修：主窗被全屏输出完全盖住时，这里的标签/HUD 更新
        #   一个像素都看不见，却每 500ms 在 GUI 线程花约 7ms（含布局）——
        #   正好会拖慢输出窗口的重绘。盖住时直接跳过；露出后下一个 500ms 周期自动补上。
        if self._covered_by_output():
            return
        snap = self.audio.state.snapshot()
        self.lbl_bpm.setText(self._bpm_text(snap) + self._rate_text(snap))
        self.lbl_energy.setText(f"{tr('energy')} {snap['energy']:.2f}")
        self.lbl_cpu.setText(f"CPU {cpu_percent():.0f}%")
        self.mini_level.set_level(snap.get("level", 0.0))
        self.btn_audio.setToolTip(T("音源设置\n当前：") + (snap.get("device_desc") or T("未采集")))

        m = self.engine.mode
        # ★★ 这里**必须走无锁快照**（`modes_snapshot`），不能调 `manual_mode()`：
        #   本函数挂在 500ms 的 QTimer 上，而 `manual_mode()` 是 `@_locked` ⇒
        #   每 0.5 秒主线程去抢引擎渲染锁，一帧慢就卡 2 秒 ⇒ 界面"卡成幻灯片"
        #   （2026-10-08 用户报「软件非常卡」的直接原因之一）。
        #   `modes_snapshot()` 只读 cfg/mode/mode_scores，GIL 下原子，零等待。
        try:
            snap_mode = self.engine.modes_snapshot()
            is_manual = bool(snap_mode.get("manual"))
        except Exception:                                        # noqa: BLE001
            is_manual = False
        # ★★★ 2026-11-06（用户：「这个当前节奏的显示也要改」）：
        #   原来只写「常规切（自动）」——看不出**到底谁在决定什么时候换**：有段落表时其实是
        #   段落边界在驱动，写"常规切"会让人以为还在按 16 拍排期。
        #   现在按**实际驱动**写，并带上还剩几拍：
        #     当前节奏：自动 · 跟随段落「drop 3」 · 还有 12 拍
        #     当前节奏：自动 · 常规切 16 拍 · 还有 5 拍
        #     当前节奏：不自动切换（手动）
        #   ⚠ 数据一律走 `ui_snapshot()`（无锁快照，铁律 13），绝不去抢引擎锁。
        try:
            _ui = self.engine.ui_snapshot() or {}
        except Exception:                                        # noqa: BLE001
            _ui = {}
        _kind = _ui.get("switch_kind")
        _cd = _ui.get("switch_cd")
        _secn = str(_ui.get("sec_name") or "")
        if m == SWITCH_MANUAL:
            self.settings.lbl_cur_mode.setText(T("当前节奏：不自动切换（手动）"))
        else:
            _parts = []
            if not is_manual:
                _parts.append(T("自动"))
            if _kind == "section" and _secn:
                _parts.append(Tf("跟随段落「{}」", _secn))
            else:
                _parts.append(T(MODE_LABELS.get(m, m)))
            # 没点「开始」时拍钟不走，倒计时会恒显示 0 拍（像是在催人）⇒ 改说"未开始"
            if not bool(getattr(self.engine, "running", False)):
                _parts.append(T("未开始"))
            elif _cd is not None:
                _parts.append(Tf("还有 {} 拍", int(_cd)))
            self.settings.lbl_cur_mode.setText(
                Tf("当前节奏：{}", " · ".join(_parts)))

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

        # 后处理状态提示：显示"当前模式下实际生效的效果与强度"
        # 只读引擎每帧写好的普通 dict（未加锁，GIL 下原子），不抢渲染锁。
        try:
            self.settings.lbl_postfx_state.setText(
                postfx_status_text(getattr(self.engine, "_postfx_info", None)))
        except Exception:                                        # noqa: BLE001
            pass

        # ★★ 本函数所有"引擎取值"**一律走无锁只读快照**，绝不调 `@_locked` 方法：
        #   `_update_status` 挂在 500ms 的 QTimer 上，而 `@_locked` 会 `acquire(timeout=2.0)`
        #   去抢引擎渲染锁（`_tick` 整帧持有）⇒ 主线程每 0.5 秒被卡一次，一帧慢就等 2 秒，
        #   表现就是"软件非常卡"（2026-10-08 事故）。
        #   本函数涉及的引擎取值共 4 个，全部必须走快照，别改回带锁版本：
        #     ① `modes_snapshot()`（上面已用，替代 `manual_mode()`）
        #     ②③④ `ui_snapshot()`（下面取一次，含 switch_cd / phrase_cd / grid_view）
        #   ⚠ 第 4 类 `next_clip_for`（每图层的"下一个素材"）由
        #     `preview_panel.update_layer_thumbs` 内部改读 `ui_snapshot()["next_clips"]`，
        #     同样不得再调 `@_locked` 的 `next_clip_for`。
        #   ⚠ 别再改回 `manual_mode()` / `switch_countdown()` / `phrase_countdown()` /
        #     `beat_grid_view()` —— 它们都是 `@_locked`，会重新引入卡顿。
        _us = {}
        try:
            _us = self.engine.ui_snapshot() or {}
        except Exception:                                        # noqa: BLE001
            _us = {}

        # HUD：第一行 模式+倒计时+BPM+能量；第二行 歌名（避免单行过长显示不全）
        # 段落显示已移除（用户反馈：段落判断不准，预览里不再显示）
        hud = f"{T(MODE_LABELS.get(m, m))}"
        remain_n = _us.get("switch_cd")
        if remain_n is not None:
            # 倒计时从「间隔-1」开始数（刚切完显示 15 / 快切 7），数到 0 就是下一个切换点；
            # 实际间隔仍是完整 16 / 8 拍并踩小节线（计算在 engine.switch_countdown 里）。
            # ★ 2026-11-06（用户：「有段落的时候 HUD 那个距离下一次切换素材还有多少的文案要改」）：
            #   段落接管时目标点是**段落边界**，说「还有 N 拍切换」会让人以为是按拍切的；
            #   改成「距下一段 N 拍」。`switch_kind` 由引擎在排程时写死，切完/换歌会重算。
            if _us.get("switch_kind") == "section":
                hud += Tf("  |  距下一段 {} 拍", f"{remain_n}")
            else:
                hud += Tf("  |  还有 {} 拍切换", f"{remain_n}")
        # 网格模式：附带「距下一个八拍头还有几拍（几秒）」——VJ 现场据此判断八拍对齐
        # 跟音乐合不合。仅在全局开关「节拍网格」打开且认出的歌有网格时才有值
        # （phrase_countdown 内部走同一个 _grid_state 门控，关闭/无网格/未识别返回 None）。
        # 复用本 HUD 的刷新节奏，不新增定时器、不新增每帧重活。
        pc = _us.get("phrase_cd")
        if pc is not None:
            pb, ps = pc
            hud += Tf("  |  距八拍头 {} 拍（{} 秒）", "%.1f" % pb, "%.1f" % ps)
        # T05②：有分段 / 多锚时追加「当前段」——现场一眼看出此刻用的是哪一段
        # （BPM 与锚点都是引擎**当前实际生效**的那一组，不是按进度猜的）。
        # 无分段/多锚（老曲目）时 `_hud_grid_seg` 返回空串 ⇒ HUD 逐字不变。
        hud += _hud_grid_seg(_us)
        hud += ("  |  " + self._bpm_text(snap) + self._rate_text(snap)
                + f"  {T('能量')} {snap['energy']:.2f}")
        sid_now = snap.get("recognized_song_id", -1)
        song_meta = self._song_meta_by_id.get(sid_now)
        song_line = ""
        if song_meta:
            title = song_meta.get("title") or ""
            genres = song_meta.get("genres") or []
            song_line = f"🎵 {title}"
            if genres:
                song_line += f"  {'·'.join(genres[:3])}"
            elif song_meta.get("artist"):
                # 码表包带来的歌没有艺人信息 ⇒ 退而显示文件名（总比空白强）
                song_line += f"  {song_meta['artist']}"
        else:
            genre = getattr(self.audio.state, "genre_tags", None) or []
            if genre:
                song_line = f"🎵 {'·'.join(genre[:3])}"
        if song_line:
            hud += "\n" + song_line
        # ★ 2026-10-18（用户："多层编排还是没在 era 里看见"）：
        #   码表是**容器层**，一帧里可能同时画好几层，肉眼很难判断"是只有一层，还是多层都在"。
        #   这里把实况直接写在 HUD 上：`🎬 码表 3层 · 2在播`。
        #   `cuesheet_status()` 是**无锁只读快照**（白名单内），500ms 一次开销可忽略。
        try:
            _cs = self.engine.cuesheet_status() or {}
            if _cs.get("loaded") and _cs.get("active_layers"):
                hud += "\n🎬 %s %d层 · %d在播" % (
                    T("码表"), int(_cs.get("sub_states", 0) or 0),
                    int(_cs.get("sub_active", 0) or 0))
        except Exception:                                        # noqa: BLE001
            pass
        self.preview_panel.update_hud(hud)
        # 「节拍网格线」浮层（贴在 HUD 下方）：与切换对齐共用同一相位，
        # 所以看到的那条乐句头线就是切换实际踩的线。无拍位时自动隐藏。
        bgv = _us.get("grid_view")
        self.preview_panel.update_grid(bgv[0] if bgv else None,
                                      bgv[1] if bgv else None)

        # 每图层的"下一个素材"缩略图（多图层多个；无素材/隐藏的图层不显示）
        self.preview_panel.update_layer_thumbs(self.engine.layers)

        if snap["error"]:
            self.lbl_warn.setText(snap["error"])
        elif snap.get("lan_failed"):
            # 局域网音源失败：**不自动回退**系统声音，必须显式告诉用户当前没声
            self.lbl_warn.setText(snap["lan_failed"])
        elif snap.get("lan_info"):
            # ★ 局域网音源已连上：常显对端 IP（用户要求"能看见源的 IP"）。
            #   关掉设置对话框也能一眼确认连的是哪台机器 / 延迟是否正常。
            self.lbl_warn.setText(snap["lan_info"])
        elif self.engine.running and snap["silent_sec"] > 5:
            self.lbl_warn.setText(tr("no_sound_hint"))
        else:
            self.lbl_warn.setText("")

    def closeEvent(self, ev):
        # ★★ 2026-11-02（用户：「输出窗口打开了，关闭主窗口时输出窗口不会一起关闭」）：
        #   输出窗口是**独立顶层窗口** —— `OutputWindow(self.cfg)` 创建时没传 parent，
        #   主窗口关闭不会带走它，退出后任务栏还留着一个「Easy Reality Output」黑窗口。
        #   这里显式关掉（它自己的 closeEvent 会保存几何，正常走）。
        try:
            _ow = getattr(self, "out_win", None)
            if _ow is not None:
                _ow.close()
                _ow.deleteLater()
                self.out_win = None
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 浮动素材库也是独立顶层窗口：先保存几何再隐藏/销毁
        try:
            _lf = getattr(self, "_lib_float", None)
            if _lf is not None:
                self._save_library_float(False)
                _lf.hide()
                _lf.deleteLater()
                self._lib_float = None
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 2026-11-02：先停能量日志线程（1Hz 写盘，退出时别让它跟 cfg.save() 撞）。
        try:
            _el = getattr(self, "_energy_log", None)
            if _el is not None:
                _el.stop()
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 2026-10-19：合并写盘窗口里可能还有未落盘的改动 —— 退出前强制写一次。
        try:
            self.cfg.flush()
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 2026-10-10 审计修复：**先把所有后台线程停下来，最后再落盘**。
        #   原来只停了 thumb_worker/thumb_backfill，其余后台扫描线程（dyn/tag/music/
        #   import/erlib/codec/alpha）未停未 join —— 退出时它们仍可能与主线程并发
        #   调 cfg.save()（退出瞬间最容易撞上）。
        # 1) 先给所有后台线程发停止信号（并发进行、不阻塞）：有 cancel() 的先 cancel()，
        #    有 stop() 的再调 stop()，最后统一 requestInterruption()。
        _bg_threads = ("_dyn_scan", "_tag_scan", "_music_scan", "_erlib_imp",
                       "_import_worker", "codec_probe", "alpha_mig",
                       "thumb_worker", "thumb_backfill")
        for attr in _bg_threads:
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if hasattr(w, "cancel"):
                    w.cancel()
            except Exception:                                    # noqa: BLE001
                pass
            try:
                if hasattr(w, "stop"):
                    w.stop()
            except Exception:                                    # noqa: BLE001
                pass
            try:
                w.requestInterruption()
            except Exception:                                    # noqa: BLE001
                pass
        # 2) 再逐个带超时等它们退出（**绝不无限等**；任一线程不可停也不影响关闭）
        for attr in _bg_threads:
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if w.isRunning():
                    w.wait(1500)
            except Exception:                                    # noqa: BLE001
                pass
        # 3) 心跳看门狗（普通守护线程）：停掉，免得退出时它还在抓栈写日志
        try:
            wd = getattr(self, "_watchdog", None)
            if wd is not None and hasattr(wd, "stop"):
                wd.stop()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            lw = getattr(self, "_lag_watch", None)
            if lw is not None and hasattr(lw, "stop"):
                lw.stop()
        except Exception:                                        # noqa: BLE001
            pass
        # 4) 线程都停了，再落盘（此时后台线程已不会再并发写 config）
        self.save_layers()
        # 5) 先停引擎线程，再关音频，避免退出时 worker 仍在合成
        from PySide6.QtCore import QMetaObject, Qt as _Qt, QThread as _QThread
        QMetaObject.invokeMethod(self.engine, "stop", _Qt.QueuedConnection)
        if hasattr(self, "engine_thread"):
            self.engine_thread.quit()
            self.engine_thread.wait(2000)
        self.engine.close()
        self.audio.shutdown()
