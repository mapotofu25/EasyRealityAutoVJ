# -*- coding: utf-8 -*-
"""主窗口：顶栏 / 状态行 / 左标签页 / 预览+传输条 / 时间轴 / 日志。

布局（仿 Adobe Premiere）：
    ┌ 顶栏：当前码表[下拉]｜码表▾(新建/导入/导出/另存/关闭)｜撤销/重做 ┐
    ├ 状态行：码表：X  曲目：Y  层数：N  位置：T                                  │
    ├──────────────────────┬───────────────────────────────────────────────────┤
    │ 左上：4 标签页        │ 右上：预览 + 传输条                                │
    ├──────────────────────┴───────────────────────────────────────────────────┤
    │ 下：时间轴（全宽）+ 工具条                                                │
    ├ 底部日志（矮，只读）                                                       │
    └───────────────────────────────────────────────────────────────────────────┘

★★ 数据流（单向，最不容易自激）
    用户操作 → 校验/入撤销栈 → 改 `SheetSession.sheet` → `_rebuild_runtime()` → `_refresh()`
    控件**绝不直接**改模型；控件只发信号，主窗口统一落模型。

★★ 多码表隔离（架构方案 §3.3）
    每张码表 = 一个 `SheetSession`（sheet + 素材目录 + dirty + 撤销栈 + 素材路径映射），
    主窗口只持 `sessions: dict[key, SheetSession]` 与 `current_key`。切换 = 换 key，
    **绝无"字段拼装"**（旧实现 §6.12「新建第二张后切回第一张两张都空了」的根治）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QMainWindow, QMenu,
    QPlainTextEdit, QPushButton, QSlider, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ercue import config as app_config
from ercue.audio.player import AudioPlayer, BLOCK_SEC as AUDIO_BLOCK_SEC
from ercue.media import probe, thumbs
from ercue.model import ercue_io, precheck, session_store, units
from ercue.model import fp_pack                       # ★ 指纹包格式（fp/<fp_id>.bin）
from ercue.model.cuesheet import (
    CompLayer, Composition, CueItem, CueSheet, Identity, MediaRef, TrackEntry,
    reconcile_item_timing,
)
from ercue.model.timeline import CueRuntime
from . import dialogs, theme
from . import identity_scan                            # ★ 哈希序列缓存 / 导出补算共用实现
from .identity_scan import IdentityScanWorker
from .media_wave import MediaWaveWorker
from .panels import MediaPanel, PkgPanel, TrackPanel
from .preview import PreviewCanvas
from .props_panel import PropsPanel
from .timeline_widget import CueTimeline, mmss

log = logging.getLogger("ercue.ui.window")

def _app_version() -> str:
    """构建版本号 —— 优先取打包时生成的 `_build_ver.BUILD_VER`（形如 `2026.10.01.06`）。

    ⚠ **绝不能硬编码**：这个值会写进 `.ercue` 的 `generator.version`，并且是
    `capability_report()` 判定"这张码表要求的最低程序版本"时的比较基准。
    写死成 `0.1.0` 之类的假版本，会让**所有版本兼容提示全部判错**。
    源码直接运行时没有那个生成文件，回退到开发态标记。
    """
    try:
        from .._build_ver import BUILD_VER
        return str(BUILD_VER)
    except Exception:                       # 源码运行 / 生成文件缺失
        return "0.0.0-dev"


APP_VERSION = (_app_version()) + "-RC1"
UNDO_CAP = 50
# 滑杆 seek 防抖：拖动停止这么久之后才真正给音频重定位一次（P1，避免每帧重启音频）
SEEK_DEBOUNCE_MS = 100
# ★ 导出包内缩略图（`thumbs/<clip_id>.png`，与演出端约定：PNG、长边 ≤256、保留 alpha）
THUMB_EXPORT_SIDE = 256          # 首选长边（单张一般 20~80KB）
THUMB_EXPORT_SIDE_SMALL = 128    # 总量超限时的降级长边
THUMB_EXPORT_MAX_BYTES = 8 * 1024 * 1024   # 缩略图总量上限（超过就降级，别撑爆包）
# ★ T02（#5）自动保存：编辑停顿 1.5s 落一次盘；另有 30s 安全网兜底（见 `_flush_autosave`）。
AUTOSAVE_DEBOUNCE_MS = 1500
AUTOSAVE_SAFETY_MS = 30000

# ==========================================================================
# ★「曲目未设 BPM」的**提示**（不是错误）—— 纯逻辑、零 Qt，可离线单测
#
# ★★ 2026-10-18 事实纠正（上一轮写错了，会误导用户）：
#   两端的位置换算**同一个系数会互相抵消** ——
#     编排端导出：`at_beats = at_sec × bpm/60`
#     演出端算位置：`pos_beats = offset_sec / beat_len`（`offset_sec` 来自原曲播放秒数）
#     演出端选片段：`(pos_beats − at_beats) × beat_len = offset_sec − at_sec`（**秒差**）
#   ⇒ **BPM 填错 / 留空 / 一首歌里有多段 BPM，都不会让画面对不上**。
#     真正会坏的只有「算位置用的 beat_len ≠ 选片段用的 beat_len」（那是演出端自己的 bug，
#     已在 `AutoVJ 11deb2c` 修掉），与码表里的 BPM 值是两件事。
#   ⇒ 所以 `bpm <= 0`（`units.py` 的退化约定 1 拍 = 1 秒）**只影响"按小节查看/对齐"**，
#     必须用**提示**口径，绝不能再说"画面不对拍"（用户为此白折腾过）。
# ==========================================================================
_BPM_BAD_PLACEHOLDER_TITLE = "（未命名）"


def tracks_without_bpm(sheet) -> list:
    """收集 `identity.bpm` 不可用的曲目（顺序 = `sheet.tracks` 顺序）。

    判据与 `model/units.py` 的退化态**严格一致**：`not units.bpm_is_usable(bpm)`
    （即 `bpm <= 0`，以及 1e-6 以下的极小正数 —— 它们同样走退化态 1 拍 = 1 秒）。

    ⚠ 返回值语义 = 「**没填 BPM 的曲目**」，与「会影响演出」无关：
      没填 BPM 照样能导出、能演出（两端换算一致），只是编辑器里没法按小节查看/对齐。

    Args:
        sheet: `CueSheet`（允许 None —— 空窗/未载入时安全返回空列表）。

    Returns:
        `[(曲名, 艺术家), ...]`；`曲名` 缺失时为 `"（未命名）"`，`艺术家` 缺失为 `""`。
        无曲目 / `sheet is None` ⇒ `[]`。
    """
    bad = []
    if sheet is None:
        return bad
    for track in (getattr(sheet, "tracks", None) or []):
        identity = getattr(track, "identity", None)
        bpm = units.to_float(getattr(identity, "bpm", 0.0))
        if units.bpm_is_usable(bpm):              # 有效 BPM ⇒ 不用提醒
            continue
        title = str(getattr(identity, "title", "") or "").strip() \
            or _BPM_BAD_PLACEHOLDER_TITLE
        artist = str(getattr(identity, "artist", "") or "").strip()
        bad.append((title, artist))
    return bad


def bpm_warn_text(bad) -> str:
    """把 `tracks_without_bpm()` 的结论拼成**提示原文**（纯字符串，便于单测逐字校验）。

    ★ 口径是**提示**不是警告：没填 BPM **不影响演出**（两端换算一致，位置取自原曲秒数），
      只在编辑器里没法按小节查看/对齐。措辞见 `tests/test_bpm_invariant.py` 的断言。

    格式（用户可读、逐首列出）::

        这张码表有 2 首曲目没填 BPM：
          · Nimbus Spark 1999 — Supa7onyz
          · 揽月 — Dreamira

        不影响演出：两端换算一致（都按「1 拍 = 1 秒」），片段位置不会错。
        只是在编辑器里没法按小节查看 / 对齐 —— 要按拍编排就填一下（可选）。

    Args:
        bad: `[(曲名, 艺术家), ...]`（来自 `tracks_without_bpm`）。

    Returns:
        完整提示文案；`bad` 为空 ⇒ 空字符串（调用方据此跳过）。
    """
    items = list(bad or [])
    if not items:
        return ""
    lines = ["这张码表有 %d 首曲目没填 BPM：" % len(items)]
    for title, artist in items:
        if artist:
            lines.append("  · %s — %s" % (title, artist))
        else:
            lines.append("  · %s" % title)
    lines.append("")
    lines.append("不影响演出：两端换算一致（都按「1 拍 = 1 秒」），片段位置不会错。")
    lines.append("只是在编辑器里没法按小节查看 / 对齐 —— 要按拍编排就填一下（可选）。")
    return "\n".join(lines)


# 指定素材目录 → 递归扫描的深度上限与跳过名单（绝不扫进系统目录 / 回收站 / 版本库）
SCAN_MAX_DEPTH = 4
_SCAN_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "node_modules",
    ".git", ".svn",
}


def _scan_media_index(root, max_depth=SCAN_MAX_DEPTH) -> dict:
    """递归扫描 `root` 下的素材文件 → `{basename: 第一个命中路径}`（限深、跳过系统目录）。

    只读目录名与文件名，**不开文件**；深度到 `max_depth` 停止；命中跳过名单的目录不深入。
    """
    index = {}
    if not root or not os.path.isdir(root):
        return index
    root = os.path.abspath(root)
    root_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip("\\/").count(os.sep) - root_depth
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames
                           if d.strip().lower() not in _SCAN_SKIP_DIRS
                           and not d.startswith(".")]
        for fn in filenames:
            index.setdefault(fn, os.path.join(dirpath, fn))
    return index


# ==========================================================================
# 一张码表 = 一个 session（架构方案 §3.3）
# ==========================================================================
class SheetSession:
    """一张码表的全部状态：模型 + 素材目录 + dirty + 撤销栈 + 素材路径映射。

    **不 import Qt**，纯数据容器；切换码表只是换 `current_key`，不拼装字段。
    """

    def __init__(self, key, sheet, media_dir=""):
        self.key = key
        self.sheet = sheet
        self.dir = media_dir
        self.dirty = False
        self.undo = []                 # [json 快照]，上限 UNDO_CAP
        self.redo = []
        self.media_paths = {}          # clip_id -> 磁盘路径
        self.track_audio = {}          # 曲目 index -> 音频文件路径（导入音频时记下）
        # ★ T07（需求 11）：素材音轨波形数据 `{clip_id: WaveformData}`（**session 级缓存**，
        #   切码表各自独立；由 `window._ensure_clip_waves` 后台按需填充，重复开关不重算）
        self.clip_waves = {}
        self.backing_path = ""         # 导入/另存后的 .ercue 路径（导出默认落点）
        self.ti = 0                    # ★ B3：快照里要一并保存的"当前曲目下标"

    def snapshot(self, ti=None) -> str:
        """内部状态快照（**秒**坐标）—— 撤销栈专用，绝不用文件形态（§3.5）。

        ★ B3：除 `sheet` 外，**还把"当前曲目下标 + 音频/素材映射"一起快照** ——
        否则撤销后当前曲目会被打回第 1 首，且撤销后音频映射 / 身份会不同步。
        """
        return json.dumps({
            "sheet": self.sheet.to_state_dict(),
            "ti": int(ti if ti is not None else getattr(self, "ti", 0) or 0),
            "track_audio": {str(k): str(v) for k, v in (self.track_audio or {}).items()},
            "media_paths": {str(k): str(v) for k, v in (self.media_paths or {}).items()},
        }, ensure_ascii=False)

    def load_snapshot(self, snap):
        """从快照恢复。**兼容旧格式**（只含裸 `sheet` 的旧快照照常能恢复）。"""
        try:
            d = json.loads(snap)
        except (TypeError, ValueError):
            log.warning("快照解析失败，忽略：%r", str(snap)[:64])
            return
        if isinstance(d, dict) and "sheet" in d:
            self.sheet = CueSheet.from_state_dict(d.get("sheet"))
            self.ti = max(0, int(d.get("ti") or 0))
            ta = d.get("track_audio")
            if isinstance(ta, dict):
                self.track_audio = {int(k): str(v) for k, v in ta.items()}
            mp = d.get("media_paths")
            if isinstance(mp, dict):
                self.media_paths = {str(k): str(v) for k, v in mp.items()}
        else:
            self.sheet = CueSheet.from_state_dict(d)   # 旧格式（裸 sheet 快照）


# ==========================================================================
# 辅助：id 生成（架构方案 §3.4）
# ==========================================================================
def _next_num(prefix, ids):
    mx = 0
    for s in ids:
        s = str(s or "")
        if s.startswith(prefix):
            tail = s[len(prefix):]
            if tail.isdigit():
                mx = max(mx, int(tail))
    return mx + 1


def _all_item_ids(sheet):
    out = []
    for t in sheet.tracks:
        for ly in t.layers:
            for it in ly.items:
                out.append(it.item_id)
    return out


def _num_eq(a, b, tol=1e-9) -> bool:
    """两个**可空浮点**是否"相等"：`None == None` → True；一空一非空 → False。**绝不抛**。

    ★ A3 幻影抑制用：比较"改动前 / 改动后"的 `speed` / `src_in` / `src_out` 是否未变。
    """
    if (a is None) != (b is None):
        return False
    if a is None:
        return True
    return abs(float(a) - float(b)) <= tol


# UI 精度护栏：`sp_len` / `sp_at` 是 **2 位小数**控件，`commit()` 会把 `end` 写成 `at + round(dur,2)`
# ⇒ 即便用户没碰"时长"，模型也会看到 **< 5ms** 的 `end` 漂移。此容差用来判定"这是回填噪声，不是用户意图"。
UI_LEN_EPS = 0.006


# ==========================================================================
# 主窗口
# ==========================================================================
class MainWindow(QMainWindow):
    def __init__(self, parent=None, *, restore_workspace=True, autosave=True):
        """`restore_workspace=False` ⇒ 不恢复自动保存（测试隔离）；`autosave=False` ⇒ 关自动保存。
        环境变量 `ERCUE_NO_AUTOSAVE=1` 也会全关（P0 测试隔离）。
        """
        super().__init__(parent)
        self.setWindowTitle("EasyRealityCue —— DJ 码表编排编辑器")
        self.resize(1240, 800)
        self.setStyleSheet(theme.stylesheet())

        self.sessions = {}                        # key -> SheetSession
        self.current_key = ""
        self._key_seq = 0
        self._pos = 0.0
        self._playing = False
        self._loop = False
        self._play_last = 0.0
        self._audio_backed = False                # 本次播放是否由音频驱动（否则挂钟）
        # ★ 音频位置**平滑锚点**（见 `_smooth_audio_pos`）：音频位置以 BLOCK_SEC=0.25s 为块阶梯，
        #   直接喂给 30Hz 的预览会"停 7~8 拍 → 跳 0.25s"（跳帧）。这里按墙钟在块之间插值。
        self._pos_anchor = None                   # (音频锚点秒, 锚定时的 monotonic)
        self._pos_smooth = 0.0                    # 平滑后的位置（单调不减）
        self._pos_anchor_moved = False            # ★ 音频是否**真的交过帧**（没交过就绝不外推）
        self.audio = AudioPlayer()                # ★ M4：音频走带
        # ★ 身份扫描（文件 md5 + 音频指纹签名）：后台线程 + 代号防过期回执（见 `_ensure_track_identity`）
        self._id_scan = None                      # 当前 IdentityScanWorker
        self._id_scan_key = ""                    # (曲目index, 路径) —— 同一目标不重复扫
        self._id_gen = 0                          # 代号：换歌/换码表后旧回执作废
        self._id_status = ""                      # 面板上显示的扫描状态（进度 / 失败，绝不静默）
        # ★ 手动「自动识别」语义：force=True ⇒ 回执**允许覆盖**已有 BPM（与自动扫描相反）
        self._bpm_force = False
        self._bpm_force_old = 0.0                 # 重算前的 BPM（日志"原 X → 新 Y"用）
        # ★ T07（需求 11）：素材音轨波形的后台 worker（**按需**起，见 `_ensure_clip_waves`）
        self._wave_worker = None
        self._pending_seek_sec = None             # 滑杆拖动待落地的音频 seek（防抖）
        self._sig = {"sheet": 0}                  # B1 哨兵：统计下拉框级联信号次数
        self.thumbs_enabled = True
        self._thumb_dir = os.path.join(tempfile.gettempdir(), "ercue_thumbs")
        self.cfg = app_config.instance()           # 配置文件（最近文件 / 上次目录 / 内嵌偏好）
        self._interactive = True                    # 测试置 False → 不弹任何对话框（无头不阻塞）
        self.last_import_warnings = []              # 最近一次导入的兼容提示（状态行 / 哨兵用）
        self._embed_pref = bool(self.cfg.get("embed_media"))

        # ★ T02（#5）自动保存：**独立目录**（`model/session_store`），**绝不写用户 .ercue**。
        #   开关：构造参数 autosave=False / config `autosave=False` / 环境变量 `ERCUE_NO_AUTOSAVE=1`
        #   任一为假即全关（后者保证测试互相不污染 —— P0）。
        self._autosave_enabled = bool(autosave) and bool(self.cfg.get("autosave", True)) \
            and not session_store.disabled()
        self._restore_workspace_wanted = bool(restore_workspace)
        self._autosave_pending = set()               # 待落盘的 session key
        self._autosave_timer = QTimer(self)          # 1.5s 防抖
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setTimerType(Qt.PreciseTimer)
        self._autosave_timer.setInterval(AUTOSAVE_DEBOUNCE_MS)
        self._autosave_timer.timeout.connect(self._flush_autosave)
        self._autosave_safety = QTimer(self)         # 30s 安全网（防某次 debounce 被丢掉）
        self._autosave_safety.setInterval(AUTOSAVE_SAFETY_MS)
        self._autosave_safety.timeout.connect(self._flush_autosave)

        self._build()
        self._wire()
        # ★ T02：恢复上次工作区；**失败/为空不阻断**（回落新建一张空码表）。
        restored = self._restore_workspace() if (self._restore_workspace_wanted
                                                 and self._autosave_enabled) else False
        if not restored:
            self.new_sheet("新建码表")
        if self._autosave_enabled:
            self._autosave_safety.start()

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(33)
        # ★ 帧率修复（本文件最关键的一行）：QTimer **默认是 `Qt.CoarseTimer`**，在 Windows 上
        #   受系统定时器精度（默认 15.625ms）量化 ⇒ `setInterval(33)` 实际被抬到 ~46.9ms，
        #   行走带只跑到 **21 Hz**（实测 21.0Hz，且与负载无关）——于是"播放头的有效推进"
        #   只有 21 次/秒，预览里视频**肉眼就是 21fps**，用户报的"帧率非常低"正是它。
        #   改 `Qt.PreciseTimer` 后实测 30.0Hz（33→30.0 / 30→33.3 / 16→62.3）。
        #   ⚠ 位置精度不受影响：本计时器只决定**采样频率**，位置本身仍按音频/挂钟真实时间算。
        self._play_timer.setTimerType(Qt.PreciseTimer)
        self._play_timer.timeout.connect(self._play_tick)

        # 滑杆 seek 防抖：拖动时不每帧重启音频，静默 100ms 后只起播一次（P1）
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(SEEK_DEBOUNCE_MS)
        self._seek_timer.timeout.connect(self._flush_audio_seek)

        # 后台解码就绪轮询：解码中把 ▶ 置灰显示"准备中"，就绪/失败后恢复（P2）
        self._audio_poll = QTimer(self)
        self._audio_poll.setInterval(200)
        self._audio_poll.timeout.connect(self._poll_audio_ready)

        self._refresh_all()

    # ==================================================================
    # 构建
    # ==================================================================
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setSpacing(4)

        # ---------- 顶栏 ----------
        top = QHBoxLayout()
        top.addWidget(QLabel("当前码表："))
        self.cb_sheet = QComboBox()
        self.cb_sheet.setMinimumWidth(220)
        top.addWidget(self.cb_sheet)
        self.btn_pkg = QPushButton("码表 ▾")
        self.btn_pkg.setMenu(self._pkg_menu())
        top.addWidget(self.btn_pkg)
        top.addSpacing(10)
        self.btn_undo = QPushButton("撤销")
        top.addWidget(self.btn_undo)
        self.btn_redo = QPushButton("重做")
        top.addWidget(self.btn_redo)
        top.addStretch(1)
        # ★ 需求 6：顶栏**右上角**独立「导出」入口（裁定=双入口，保留「码表▾ / ① 码表包」原入口）
        #   ★ T02（#8）：文案明确为「导出当前码表」+ **视觉强调**（强调色底 + 加粗）
        self.btn_export = QPushButton("导出当前码表")
        self.btn_export.setToolTip("把当前码表导出（另存）为 .ercue；"
                                   "与「码表▾ / ① 码表包」的导出是同一个动作")
        _ac = theme.c("accent")
        self.btn_export.setStyleSheet(
            "QPushButton{background:%s;color:#10231c;font-weight:bold;padding:5px 14px;"
            "border:1px solid %s;border-radius:4px;}"
            "QPushButton:hover{background:%s;}"
            "QPushButton:disabled{background:#3a3a3a;color:#8a8a8a;font-weight:normal;}"
            % (_ac, _ac, theme.c("accent2")))
        top.addWidget(self.btn_export)
        v.addLayout(top)

        # ---------- 状态行 ----------
        self.lbl_state = QLabel("")
        self.lbl_state.setStyleSheet("color:%s;font-size:11px;" % theme.c("fg_dim"))
        v.addWidget(self.lbl_state)

        # ---------- 主区 ----------
        vsplit = QSplitter(Qt.Vertical)
        up = QSplitter(Qt.Horizontal)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.pkg_panel = PkgPanel()
        self.track_panel = TrackPanel()
        self.media_panel = MediaPanel()
        self.props_panel = PropsPanel()
        self.tabs.addTab(self.pkg_panel, "① 码表包")
        self.tabs.addTab(self.track_panel, "② 曲目")
        self.tabs.addTab(self.media_panel, "③ 素材")
        self.tabs.addTab(self.props_panel, "④ 片段属性")
        up.addWidget(self.tabs)

        # 右上：节目监视器 + 传输条
        prevw = QWidget()
        pv = QVBoxLayout(prevw)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(4)
        ph = QHBoxLayout()
        # ★ T02（#11）：预览标签**显示当前码表名**（`码表预览 —— <名>`），随切换/改名更新
        self.lbl_preview = QLabel(self._preview_label())
        self.lbl_preview.setStyleSheet("font-weight:bold;")
        ph.addWidget(self.lbl_preview)
        ph.addStretch(1)
        pv.addLayout(ph)
        self.preview = PreviewCanvas()
        pv.addWidget(self.preview, 1)
        bot = QHBoxLayout()
        self.btn_rew = QPushButton("⏮")
        self.btn_rew.setFixedWidth(34)
        self.btn_rew.setToolTip("回到开头")
        bot.addWidget(self.btn_rew)
        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(42)
        bot.addWidget(self.btn_play)
        self.chk_loop = QCheckBox("循环")
        bot.addWidget(self.chk_loop)
        bot.addWidget(QLabel("位置"))
        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(0, 100)
        bot.addWidget(self.sld, 1)
        self.lbl_pos = QLabel("0:00（0.0 秒）")
        self.lbl_pos.setMinimumWidth(130)
        bot.addWidget(self.lbl_pos)
        self.lbl_total = QLabel("总长 0:00")
        self.lbl_total.setStyleSheet("color:%s;font-size:11px;" % theme.c("fg_dim"))
        bot.addWidget(self.lbl_total)
        pv.addLayout(bot)
        up.addWidget(prevw)
        up.setSizes([460, 760])
        vsplit.addWidget(up)

        # 下：时间轴（全宽）+ 工具条
        down = QWidget()
        dv = QVBoxLayout(down)
        dv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        # ★ T02（#7）：**已去掉「放大 / 缩小」两个按钮** —— 时间轴缩放改由
        #   `Ctrl+滚轮`（见 `timeline_widget` 的滚轮处理）承担，工具条只留"层"相关操作。
        #   ⚠ 同步改了 `tools/_t03_verify.py` / `brand/ui_audit/_walk_ui_audit.py` 里对这两个按钮的引用。
        self.btn_add_layer = QPushButton("＋ 层")
        bar.addWidget(self.btn_add_layer)
        self.btn_del_layer = QPushButton("删除层")
        bar.addWidget(self.btn_del_layer)
        bar.addStretch(1)
        hint = QLabel("横轴 = 时间（秒）；滚轮=上下 / Shift=平移 / Ctrl=缩放 / Alt=行高")
        hint.setStyleSheet("color:%s;font-size:11px;" % theme.c("fg_dim"))
        bar.addWidget(hint)
        dv.addLayout(bar)
        self.timeline = CueTimeline()
        dv.addWidget(self.timeline, 1)
        # ★ 需求 11：时间轴**下方**的「显示素材音轨」开关（默认关；绘制在 timeline 侧，T04/T07）
        clipbar = QHBoxLayout()
        self.chk_clip_audio = QCheckBox("显示素材音轨")
        self.chk_clip_audio.setToolTip("在片段块内绘制该素材自己的音轨波形（与歌曲音频轨互相独立）")
        clipbar.addWidget(self.chk_clip_audio)
        # ★ 第五轮①（24 条 #14）：时间轴工具条上的「自动吸附」开关（默认关；状态存 `cfg["snap"]`）。
        self.chk_snap = QCheckBox("自动吸附")
        self.chk_snap.setToolTip("拖动片段时自动吸附到其它片段两端 / 播放头 / 0"
                                 "（只影响正在拖的那一个；约 8px 内才吸）")
        self.chk_snap.setChecked(bool(self.cfg.get("snap", False)))   # 初值（连线前设，避免伪信号）
        clipbar.addWidget(self.chk_snap)
        # ★ 第十一轮（用户反馈"Alt+滚轮始终无效"）：给出**不依赖 Alt** 的显式入口
        #   —— 轨道行高 −/+（有些环境 Alt 键会被菜单栏/输入法/驱动吞掉，键盘手势不可靠）
        clipbar.addSpacing(10)
        clipbar.addWidget(QLabel("轨道行高"))
        self.btn_rowh_minus = QPushButton("−")
        self.btn_rowh_minus.setFixedWidth(26)
        self.btn_rowh_minus.setToolTip("减小轨道行高（Alt+滚轮 的等价入口）")
        self.btn_rowh_minus.clicked.connect(lambda: self._nudge_row_h(1.0 / 1.15))
        clipbar.addWidget(self.btn_rowh_minus)
        self.btn_rowh_plus = QPushButton("+")
        self.btn_rowh_plus.setFixedWidth(26)
        self.btn_rowh_plus.setToolTip("增大轨道行高（Alt+滚轮 的等价入口）")
        self.btn_rowh_plus.clicked.connect(lambda: self._nudge_row_h(1.15))
        clipbar.addWidget(self.btn_rowh_plus)
        clipbar.addStretch(1)
        dv.addLayout(clipbar)
        vsplit.addWidget(down)
        vsplit.setSizes([420, 340])
        v.addWidget(vsplit, 1)

        # ---------- 日志 ----------
        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setMaximumHeight(64)
        v.addWidget(self.txt)

    def _pkg_menu(self) -> QMenu:
        m = QMenu(self)
        m.addAction("新建…").triggered.connect(self._new_sheet_dialog)   # ★ 需求1：带比例弹窗
        m.addAction("导入…").triggered.connect(self._import_sheet_dialog)
        m.addAction("导出当前码表…").triggered.connect(self._export_dialog)
        # ★ T02（③ 导出合并）：**菜单里不再有「另存为…」** —— 导出即唯一的落盘入口
        #   （顶栏「导出当前码表」/「① 码表包 → 导出…」都走同一个 `_export_dialog`）。
        self.recent_menu = m.addMenu("打开最近")
        self._fill_recent_menu()
        m.addSeparator()
        m.addAction("关闭").triggered.connect(self._close_sheet)
        return m

    def _fill_recent_menu(self):
        """最近打开 / 导入的 .ercue（配置持久化，最多 RECENT_CAP 条）。"""
        self.recent_menu.clear()
        rec = [p for p in self.cfg.recent() if os.path.isfile(p)]
        if not rec:
            a = self.recent_menu.addAction("（暂无）")
            a.setEnabled(False)
            return
        for p in rec:
            act = self.recent_menu.addAction(os.path.basename(p))
            act.setToolTip(p)
            act.triggered.connect(lambda _c=False, x=p: self._open_recent(x))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("清除最近列表").triggered.connect(self._clear_recent)

    def _clear_recent(self):
        self.cfg.set("recent_files", [])
        self._fill_recent_menu()

    def _open_recent(self, path):
        if not os.path.isfile(path):
            self._log("最近文件已不在：%s" % path)
            self._fill_recent_menu()
            return
        self.import_sheet(path)

    @staticmethod
    def _connect_if(obj, name, slot) -> bool:
        """**有条件接线**：`obj` 上存在名为 `name` 的信号才连（返回是否连上）。

        ★ 为什么需要：需求 3/12/8 的信号（`trimmed` / `context_menu` / `xform_committed`）由
          T04/T06 在各自文件里新增。本批次（window 接线）要能**独立落地、独立回归**，
          不能因为"对方的信号还没加"就崩 —— 所以"有就接、没有就跳过"。
          对方落地后**无需再改本文件**，接线会自动生效。
        """
        sig = getattr(obj, name, None)
        if sig is None:
            log.info("信号尚未提供，暂不接线：%s.%s（等对应批次落地后自动生效）",
                     type(obj).__name__, name)
            return False
        try:
            sig.connect(slot)
            return True
        except Exception:                              # noqa: BLE001
            log.exception("接线失败：%s.%s", type(obj).__name__, name)
            return False

    def _wire(self):
        # 顶栏
        self.cb_sheet.currentIndexChanged.connect(self._on_sheet_combo)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.btn_export.clicked.connect(self._export_dialog)      # ★ 需求 6：右上角独立导出

        # 码表包
        self.pkg_panel.newSheet.connect(self._new_sheet_dialog)   # ★ 需求1：带比例弹窗
        self.pkg_panel.importSheet.connect(self._import_sheet_dialog)
        self.pkg_panel.exportSheet.connect(self._export_dialog)
        self.pkg_panel.deleteSheet.connect(self._close_sheet)
        self.pkg_panel.pickMediaDir.connect(self._pick_media_dir_dialog)
        self.pkg_panel.pickSheet.connect(self._switch_to)

        # 曲目
        self.track_panel.addTrack.connect(self.add_track)
        self.track_panel.importAudio.connect(self._import_track_audio_dialog)
        self.track_panel.delTrack.connect(self.del_track)
        self.track_panel.trackPicked.connect(self._on_track_picked)
        self.track_panel.edited.connect(self._on_track_edited)
        self.track_panel.rescanBpm.connect(self._rescan_bpm)      # ★ BPM「自动识别」按钮

        # 素材
        self.media_panel.addMedia.connect(self._add_media_dialog)
        self.media_panel.pickMediaDir.connect(self._pick_media_dir_dialog)
        self.media_panel.addToTimeline.connect(self._add_media_to_playhead)
        self.media_panel.revealRequested.connect(self._reveal_media)
        self.media_panel.removeRequested.connect(self.remove_media)

        # 属性
        self.props_panel.changed.connect(self._on_props_changed)

        # 传输条
        self.btn_rew.clicked.connect(lambda: self._set_pos(0.0))
        self.btn_play.clicked.connect(self.toggle_play)
        self.chk_loop.toggled.connect(self._on_loop_toggled)
        self.sld.valueChanged.connect(self._on_slider)
        self.sld.sliderReleased.connect(self._flush_audio_seek)   # 松手立即落地待处理 seek

        # 时间轴工具条（★ 需求 5：＋cue / 删除 cue 两按钮已移除，故无对应连线）
        self.btn_add_layer.clicked.connect(self.add_layer)
        self.btn_del_layer.clicked.connect(self.del_layer)

        # 时间轴交互
        self.timeline.picked.connect(self._select)
        self.timeline.moved.connect(self._on_timeline_moved)
        self.timeline.resized.connect(self._on_timeline_resized)
        self.timeline.layer_moved.connect(self._on_timeline_layer_moved)
        self.timeline.clip_dropped.connect(self._on_clip_dropped)
        # ★ T04 冻结接口（本批次可能尚未落地 ⇒ **有就接、没有就跳过**，绝不因此崩）
        self._connect_if(self.timeline, "trimmed", self._on_timeline_trimmed)      # 需求 3
        self._connect_if(self.timeline, "context_menu", self._on_timeline_menu)    # 需求 12
        # ★ 需求 8：预览画布变换「松手」提交（T06 提供该信号；**有就接**）
        self._connect_if(self.preview, "xform_committed", self._on_preview_xform_committed)
        # ★ T02：消费 T04 的「预览点空白 ⇒ 取消选中」（**有就接**，T04 未落地时静默跳过）
        self._connect_if(self.preview, "selection_cleared", self._on_selection_cleared)
        # ★ 需求 11：素材音轨开关 → 时间轴（T04/T07 提供 set_show_clip_audio）
        self.chk_clip_audio.toggled.connect(self._on_show_clip_audio)
        # ★ 第五轮①（#14）：自动吸附开关 → 时间轴（T03 提供 set_snap）
        self.chk_snap.toggled.connect(self._on_snap_toggled)
        # ★ 第五轮⑥（#6）：标尺点击/拖动 ⇒ 挪播放头（T03 提供 seek_requested；**有就接**）
        self._connect_if(self.timeline, "seek_requested", self._on_seek_requested)
        # ★ 第五轮②（#18）：再制产份（T03 提供 duplicate_requested；**有就接**）
        self._connect_if(self.timeline, "duplicate_requested", self._on_duplicate_requested)
        # ★ 第九轮（框选多选）：选中数量 / 整组移动 / 整组再制（**有就接**）
        self._connect_if(self.timeline, "selection_count_changed", self._on_selection_count_changed)
        self._connect_if(self.timeline, "group_moved", self._on_timeline_group_moved)
        self._connect_if(self.timeline, "group_duplicate_requested", self._on_duplicate_group)
        # ★ 需求 2：Delete 键删片段 —— **绑在时间轴上**（WidgetWithChildrenShortcut），
        #   否则在备注/数值框里按 Delete 会误删片段（抢走文本框的删除键）。
        self._sc_del = QShortcut(QKeySequence(QKeySequence.Delete), self.timeline,
                                 context=Qt.WidgetWithChildrenShortcut,
                                 activated=self.del_cue)

        # 快捷键（撤销 / 重做）
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self.redo)

    # ==================================================================
    # session 便捷
    # ==================================================================
    @property
    def session(self) -> SheetSession:
        return self.sessions.get(self.current_key)

    def _sheet(self):
        s = self.session
        return s.sheet if s is not None else None

    def _track(self):
        sh = self._sheet()
        if sh is None or not sh.tracks:
            return None
        i = max(0, min(int(getattr(self, "_ti", 0) or 0), len(sh.tracks) - 1))
        return sh.tracks[i]

    def _layers(self):
        tr = self._track()
        return tr.layers if tr is not None else []

    def _has_media(self) -> bool:
        """当前码表里有没有素材（「＋ cue」需要至少一个素材才有意义）。"""
        s = self.session
        return bool(s is not None and s.sheet is not None and s.sheet.media)

    def _explicit_selected_layer(self):
        """用户**显式**选中的层（= 选中了该层上的某个片段）——**绝不回退**。

        与 `timeline.selected_layer()`（未选中就回退到第一层）严格区分：
        删除层是破坏性操作，必须用"显式选中"，**绝不静默删掉别的层**（P0-3）。
        """
        ly_id = str((self.timeline.sel or ("", ""))[0] or "")
        if not ly_id:
            return None
        return next((l for l in self._layers() if l.lyr_id == ly_id), None)

    # ==================================================================
    # 新建 / 切换 / 关闭（多码表隔离 §3.3）
    # ==================================================================
    def new_sheet(self, title=None, res_w=None, res_h=None) -> str:
        """新建码表。`res_w/res_h` = 画布比例（需求 1）；缺省 1920×1080（16:9）。"""
        self._flush_autosave()                         # ★ T02：关键点强制落盘（新建前）
        identity_scan.clear_hash_cache()               # ★ C3：缓存只属于"当前这张表"
        self._key_seq += 1
        key = "sheet%d" % self._key_seq
        sheet = ercue_io.new_sheet(title or ("新建码表%d" % self._key_seq), "",
                                   res_w if res_w else 1920, res_h if res_h else 1080)
        self.sessions[key] = SheetSession(key, sheet)
        self.current_key = key
        self._ti = 0
        self.last_import_warnings = []
        self._push_history_clean()
        self._log("已新建码表：%s（画布 %d×%d）" % (sheet.title, sheet.res_w, sheet.res_h))
        self._refresh_all()
        self._set_pos(0.0)
        self._ratio_invalidate()                       # ★ 需求 1：把比例喂给预览/导出
        self._autosave_touch(key)                      # ★ T02：新建是关键点 ⇒ 立刻进工作区
        return key

    def _new_sheet_dialog(self):
        """★ 需求 1：弹「新建码表」问名字 + 画布比例（16:9 / 9:16 / 4:3 / 3:4 / 自定义）。

        ⚠ **无头/测试模式绝不弹窗**（`_interactive=False` 的既有约定，见 `__init__:219`）——
        否则 `dlg.exec()` 会**阻塞**无头回归（UI 走查、test_ui_smoke 都靠这条约定不弹框）。
        """
        if not self._interactive or QApplication.instance() is None:
            self.new_sheet()                           # 无头：直接用默认名 + 16:9 新建
            return
        default = "新建码表%d" % (self._key_seq + 1)
        got = dialogs.ask_new_sheet(self, default)
        if not got:
            return
        title, res_w, res_h = got
        self.new_sheet(title, res_w, res_h)

    def _switch_to(self, key):
        if key and key in self.sessions and key != self.current_key:
            self._flush_autosave()                     # ★ T02：切走前先落盘当前码表
            self._stop_play()                          # 换码表先停播（音频不留线程）
            identity_scan.clear_hash_cache()           # ★ C3：缓存只属于"当前这张表"
            self.current_key = key
            self._ti = 0
            self._refresh_all()
            self._preload_audio()                      # 预载这张码表当前曲目的音频
            self._ratio_invalidate()                   # ★ 需求1：切码表 ⇒ 画布比例跟着换
            self._log("已切到码表：%s" % self.session.sheet.title)

    def _on_sheet_combo(self, _idx=0):
        self._sig["sheet"] += 1                    # B1 哨兵计数器
        if self.cb_sheet.signalsBlocked():
            return
        key = self.cb_sheet.currentData()
        if key:
            self._switch_to(key)

    def _close_sheet(self):
        s = self.session
        if s is None:
            return
        # ★ T02：**非阻断**提醒（取代原"保存/丢弃/取消"三选）。⚠ 与"关窗"不同：
        #   这张表会被移出列表、autosave 条目也会一并删除 ⇒ 未导出就真的恢复不了，故文案照实说。
        if not self._warn_unsaved([s], "关闭",
                                  "这张码表将从列表里移除（磁盘上的 .ercue 文件没动）；\n"
                                  "它的改动没有导出，移除后**无法**从自动保存恢复。"):
            self._log("已取消关闭。")
            return
        key = self.current_key
        self._flush_autosave()
        self._stop_play()
        self.audio.stop()
        identity_scan.clear_hash_cache()               # ★ C3：这张表的指纹缓存一并释放
        self.sessions.pop(key, None)
        try:                                           # ★ T02：移出列表 ⇒ 也移出自动保存
            session_store.remove_sheet(key)
        except Exception:                              # noqa: BLE001
            log.exception("移除自动保存条目失败：%s", key)
        self._log("已关闭码表：%s（磁盘文件没动）" % s.sheet.title)
        if self.sessions:
            self.current_key = next(iter(self.sessions))
        else:
            self._key_seq += 1
            key = "sheet%d" % self._key_seq
            self.sessions[key] = SheetSession(key, ercue_io.new_sheet("新建码表", ""))
            self.current_key = key
        self._ti = 0
        self.last_import_warnings = []
        self._flush_autosave()
        self._refresh_all()

    # ==================================================================
    # 导入 / 导出
    # ==================================================================
    def _import_sheet_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        p, _ = QFileDialog.getOpenFileName(self, "导入码表", "",
                                           "码表包 (*.ercue *.json);;所有文件 (*)")
        if p:
            self.import_sheet(p)

    @staticmethod
    def _extract_embedded_media(path):
        """★ B2：若 `.ercue` 是**内嵌素材**的 zip（顶层含 `media/`），解到 `<path>.media` 并返回该目录。

        幂等：目标目录已存在且非空 → 直接返回，不重复解压。
        非 zip / 无 `media/` → 返回 None。
        **解压失败原样抛出**（由调用方 `import_sheet` 明确报错，绝不静默继续）。
        """
        import zipfile
        if not path or not os.path.isfile(path) or not zipfile.is_zipfile(path):
            return None
        dest = str(path) + ".media"
        try:
            with zipfile.ZipFile(path) as z:
                has_media = any(n.startswith(ercue_io.MEDIA_DIR + "/") and not n.endswith("/")
                                for n in z.namelist())
        except zipfile.BadZipFile:
            return None
        if not has_media:
            return None
        if os.path.isdir(dest) and os.listdir(dest):
            return dest                            # 已解过 → 幂等跳过
        ercue_io.extract_media(path, dest)         # 失败会抛，交给调用方报错
        return dest

    def import_sheet(self, path, media_dir=None) -> str:
        # ★ B2：先把内嵌素材解出来（否则换机打开会"全丢失"）。解压失败明确报错，但不阻断导入。
        embed_dir = None
        try:
            embed_dir = self._extract_embedded_media(path)
        except Exception as e:                             # noqa: BLE001
            log.exception("解压内嵌素材失败: %s", path)
            self._log("⚠ 解压内嵌素材失败：%s: %s（素材将显示为丢失，可用「指定素材目录…」重挂）"
                      % (type(e).__name__, e))
        # 非内嵌包仍按旧行为（load 传 None），避免改变原有素材解析路径
        try:
            sheet, info = ercue_io.load(path, media_dir=(media_dir or embed_dir))
        except Exception as e:                         # noqa: BLE001
            log.exception("导入码表失败: %s", path)
            self._log("导入失败：%s" % e)
            return ""
        self._key_seq += 1
        key = "sheet%d" % self._key_seq
        resolved_dir = media_dir or embed_dir or (os.path.dirname(path) or "")
        sess = SheetSession(key, sheet, resolved_dir)
        sess.backing_path = path
        self.sessions[key] = sess
        self.current_key = key
        self._ti = 0
        identity_scan.clear_hash_cache()               # ★ C3：缓存只属于"当前这张表"
        self._remount_media()
        # ★ 兼容提示（规格书 §3.2）：接上 capability_report，**一律不阻断导入**。
        warn, _miss_dir = ercue_io.capability_report(sheet, app_version=APP_VERSION,
                                                     media_dir=sess.dir or None)
        miss = [m.name for m in sheet.media
                if not os.path.isfile(sess.media_paths.get(m.clip_id) or "")]
        warn = [x for x in warn if "素材没找到" not in x]     # 用重挂后的真实映射纠正
        if miss:
            warn.append("有 %d 个素材没找到。" % len(miss))
        self.last_import_warnings = list(warn)
        for x in warn:
            self._log("⚠ " + x)
        self._log("已导入码表：%s（%d 首 / %d 层）"
                  % (sheet.title, len(sheet.tracks),
                     len(sheet.tracks[0].layers) if sheet.tracks else 0))
        self._refresh_all()
        self._set_pos(0.0)
        self._preload_audio()
        self._ratio_invalidate()                       # ★ 需求1：导入的码表带来它自己的画布比例
        self.cfg.add_recent(path)                     # 记住最近文件
        self._fill_recent_menu()
        if miss and self._interactive:                # 缺素材 → 汇总清单弹一次
            self._prompt_missing_media(miss)
        self._autosave_touch(key)                      # ★ T02：导入即入工作区（关键点强制落盘）
        return key

    def _prompt_missing_media(self, missing_names):
        """导入后汇总一次缺素材清单：逐个指定新路径 / 用占位色块先跑（规格书 §15）。"""
        remap = dialogs.ask_missing_media(self, missing_names)
        if not remap:
            self._log("缺素材：先用占位色块跑（缺失片段预览被跳过，编排照常可编辑）。")
            return
        s = self.session
        if s is None:
            return
        fixed = 0
        for m in s.sheet.media:
            p = remap.get(m.name)
            if p and os.path.isfile(p):
                s.media_paths[m.clip_id] = os.path.abspath(p)
                fixed += 1
        self._refresh_all()
        self._rebuild_runtime()
        self._log("已重挂 %d 个素材。" % fixed)

    def _export_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        s = self.session
        if s is None:
            return
        start = s.dir or str(self.cfg.get("last_dir", "") or "")
        default = s.backing_path or os.path.join(
            start, (s.sheet.title or "cuesheet") + ercue_io.EXT)
        p, _ = QFileDialog.getSaveFileName(self, "导出码表", default, "码表包 (*.ercue)")
        if p:
            self.export_sheet(p)

    def pre_export_report(self):
        """导出前检查（**纯逻辑在 model/precheck**，这里只喂数据）。"""
        s = self.session
        if s is None:
            return precheck.check_sheet(None)
        return precheck.check_sheet(s.sheet, s.media_paths)

    def _embed_size_check(self, size_of=None):
        """内嵌素材体积检查：返回 `(over, total_bytes, names)`。

        `over=True` 表示**超过 1 GB 提醒阈值** —— **只提醒、不阻止**：
        由导出流程弹窗询问用户是否仍要内嵌（2026-10-17 用户要求取消 500MB 硬上限）。
        `size_of(path) -> int` 可注入（测试用假体积）。
        """
        s = self.session
        if s is None:
            return False, 0, []
        ok, total, names = precheck.check_embed_size(s.sheet, s.media_paths, size_of=size_of)
        return (not ok), total, names

    # ---------------- 导出：包内缩略图（thumbs/<clip_id>.png） ----------------
    def _export_thumb_items(self):
        """要写进包的素材清单 `[(clip_id, 磁盘路径, has_alpha)]`。

        ★ 只给**码表里真正用到的**素材做（`all_used_clip_ids()`）—— 整库都做会白烧时间。
        路径缺失 / 文件不存在 ⇒ 跳过（该素材没有缩略图，演出端按"没有"处理，**不报错**）。
        """
        s = self.session
        if s is None:
            return []
        used = s.sheet.all_used_clip_ids()
        out = []
        for m in s.sheet.media:
            if m.clip_id not in used:
                continue
            p = s.media_paths.get(m.clip_id) or ""
            if not p or not os.path.isfile(p):
                continue
            out.append((str(m.clip_id), p, bool(m.has_alpha)))
        return out

    def _export_thumb_cache_dir(self, side):
        """导出缩略图的**独立**缓存目录（按尺寸分开）。

        ★ 不能复用 UI 缩略图缓存（那是 320px 且键里没有尺寸）—— 否则把 128px 的写进去，
          素材面板就会显示小图。分开存 ⇒ 既复用又互不污染。
        """
        return os.path.join(self._thumb_dir, "export_%d" % int(side))

    def _make_export_thumbs(self, items, side, progress_cb=None):
        """生成 `{clip_id: PNG 字节}`（**纯计算，可在后台线程跑**）。

        已有缓存文件 ⇒ 直接读，**不重复解码**（第二次导出几乎零成本）。
        `progress_cb(done, total, name)` 用于进度文案。
        """
        cache_dir = self._export_thumb_cache_dir(side)
        out = {}
        total = max(1, len(items))
        for k, (cid, path, has_alpha) in enumerate(items):
            if progress_cb is not None:
                try:
                    progress_cb(k, len(items), os.path.basename(path))
                except Exception:                                # noqa: BLE001
                    pass
            data = None
            cache_path = thumbs.thumb_path(cache_dir, path, ".png")
            try:
                if os.path.isfile(cache_path):
                    with open(cache_path, "rb") as f:
                        data = f.read() or None
            except OSError:
                data = None
            if not data:
                try:
                    data = thumbs.make_thumb(path, max_side=int(side),
                                             has_alpha=has_alpha)
                except Exception:                                # noqa: BLE001
                    log.exception("生成缩略图失败：%s", path)
                    data = None
                if data:
                    try:
                        os.makedirs(cache_dir, exist_ok=True)
                        with open(cache_path, "wb") as f:
                            f.write(data)
                    except OSError:
                        pass                                     # 写不进缓存不影响本次导出
            if data:
                out[cid] = data
        if progress_cb is not None:
            try:
                progress_cb(total, total, "")
            except Exception:                                    # noqa: BLE001
                pass
        return out

    def _run_bg_with_progress(self, fn, title):
        """在**后台线程**跑 `fn(progress_cb)`，期间显示进度条；返回 `fn` 的返回值。

        · ★ 绝不在 GUI 线程解码缩略图（`thumbs` 的硬契约：单个 20~120ms × N 会卡死窗口）；
        · 交互模式：模态进度条 + 泵事件 ⇒ 窗口能重绘、能看进度，且输入被挡住（不会重入导出）；
        · 无头/非交互（测试）：直接同步跑，不弹窗、不阻塞。
        """
        if (not self._interactive) or QApplication.instance() is None:
            return fn(None)
        from PySide6.QtWidgets import QProgressDialog
        box = QProgressDialog(self)
        box.setWindowTitle("导出码表")
        box.setCancelButton(None)                     # 不可取消：中途取消会留下半个包
        box.setWindowModality(Qt.ApplicationModal)
        box.setMinimumDuration(0)
        box.setAutoClose(False)
        box.setAutoReset(False)
        box.setRange(0, 100)
        box.setValue(0)
        box.setLabelText(title)
        box.show()
        state = {"done": 0, "total": 1, "name": "", "result": None, "err": None,
                 "finished": False}
        lock = threading.Lock()

        def _cb(done, total, name):
            with lock:
                state["done"], state["total"], state["name"] = int(done), max(1, int(total)), name

        def _worker():
            try:
                state["result"] = fn(_cb)
            except Exception as e:                               # noqa: BLE001
                log.exception("后台任务失败：%s", title)
                state["err"] = e
            finally:
                state["finished"] = True

        th = threading.Thread(target=_worker, name="ercue-export-thumbs", daemon=True)
        th.start()
        while th.is_alive():
            with lock:
                done, total, name = state["done"], state["total"], state["name"]
            box.setLabelText("%s\n（%d/%d）%s" % (title, done, total, name))
            box.setValue(min(99, int(100.0 * done / max(1, total))))
            QApplication.processEvents()
            time.sleep(0.03)
        th.join()
        box.setValue(100)
        box.close()
        if state["err"] is not None:
            raise state["err"]
        return state["result"]

    def _collect_export_thumbs(self):
        """导出前的缩略图汇总 → `({clip_id: PNG}, 说明文字)`。

        容量保护：256px 总量 > `THUMB_EXPORT_MAX_BYTES`（8MB）⇒ 降到 128px 重算一次
        （说明文字里会写出来）。生成失败**绝不阻断导出**（返回空表 + 日志）。
        """
        items = self._export_thumb_items()
        if not items:
            return {}, ""
        note = ""
        try:
            data = self._run_bg_with_progress(
                lambda cb: self._make_export_thumbs(items, THUMB_EXPORT_SIDE, cb),
                "正在生成码表缩略图（%d 个素材）…" % len(items))
        except Exception as e:                                   # noqa: BLE001
            log.exception("生成缩略图失败（不影响导出）")
            self._log("⚠ 缩略图生成失败（不影响导出）：%s: %s" % (type(e).__name__, e))
            return {}, ""
        total = sum(len(v) for v in (data or {}).values())
        if total > THUMB_EXPORT_MAX_BYTES:
            try:
                data = self._run_bg_with_progress(
                    lambda cb: self._make_export_thumbs(items, THUMB_EXPORT_SIDE_SMALL, cb),
                    "缩略图总量过大，改按 %dpx 重新生成…" % THUMB_EXPORT_SIDE_SMALL)
                note = "（缩略图总量过大，已降到 %dpx）" % THUMB_EXPORT_SIDE_SMALL
                self._log("⚠ 缩略图总量 %.1f MB 超过 %.0f MB ⇒ 已降到 %dpx 重算。"
                          % (total / 1048576.0, THUMB_EXPORT_MAX_BYTES / 1048576.0,
                             THUMB_EXPORT_SIDE_SMALL))
            except Exception as e:                               # noqa: BLE001
                log.exception("缩略图降级重算失败（沿用 256px）")
                self._log("⚠ 缩略图降级失败（沿用原尺寸）：%s: %s" % (type(e).__name__, e))
        return (data or {}), note

    # ==================================================================
    # ★ C4 曲目指纹包（`fp/<fp_id>.bin`）—— 让演出端"歌不在本机也能认出"
    # ==================================================================
    @staticmethod
    def _fixup_audio_path(s, idx, idt, path) -> str:
        """**纠正 `track_audio` 的错绑**（用户 2026-10-18："为什么导出不带指纹"）。

        现场根因：`track_audio` 是**按曲目序号（index）**存的，而序号会随
        "删曲目 / 调顺序 / 重新导入"变化 ⇒ 序号一挪，映射就整体错位（实测三首曲目
        分别指向了错的音频文件），导出时"现算签名 ≠ 码表里的 fp_id" ⇒ 指纹被跳过。

        这里做**最保守的自愈**：只有在
          · 当前路径的 basename ≠ 曲目记录的 `identity.file_name`，且
          · 在已知目录里**确实找到了同名文件**
        时才改用那个文件（并把 `track_audio` 一并纠正）。
        找不到就原样返回 —— 绝不猜、绝不把"另一个版本"当成本曲目。
        """
        try:
            fn = str(getattr(idt, "file_name", "") or "").strip()
            if not fn:
                return path
            if path and os.path.basename(str(path)) == fn:
                return path
            dirs = []
            for v in (getattr(s, "track_audio", None) or {}).values():
                d = os.path.dirname(str(v or ""))
                if d and d not in dirs:
                    dirs.append(d)
            for v in (getattr(s, "media_paths", None) or {}).values():
                d = os.path.dirname(str(v or ""))
                if d and d not in dirs:
                    dirs.append(d)
            if getattr(s, "dir", ""):
                d = str(s.dir)
                if d and d not in dirs:
                    dirs.append(d)
            for d in dirs:
                cand = os.path.join(d, fn)
                if os.path.isfile(cand):
                    try:
                        s.track_audio[int(idx)] = cand
                    except Exception:                          # noqa: BLE001
                        pass
                    log.info("纠正曲目音频绑定：index=%s ⇒ %s", idx, cand)
                    return cand
        except Exception:                                      # noqa: BLE001
            pass
        return path

    def _export_fp_targets(self) -> tuple:
        """本次导出**需要带指纹**的曲目 → `(targets, n_no_id, n_no_audio)`。

        `targets` = `[(曲目 index, fp_id, 音频路径), ...]`：
          · 只取 `identity.fp_id` **非空**的曲目（`fp_id` 为空 ⇒ 包里没有它的成员名，
            写不进去，跳过 —— 这不是错误）；
          · `fp_id` 有、但音频路径为空/文件不在 ⇒ 归入 `n_no_audio`（算不出，不阻断导出）。
        """
        s = self.session
        if s is None or s.sheet is None:
            return [], 0, 0
        targets, n_no_id, n_no_audio = [], 0, 0
        for idx, te in enumerate(s.sheet.tracks or []):
            idt = getattr(te, "identity", None)
            fid = str(getattr(idt, "fp_id", "") or "").strip()
            if not fid:
                n_no_id += 1
                continue                                   # 没有指纹 id ⇒ 无从命名成员，跳过
            path = str(s.track_audio.get(idx) or "")
            path = self._fixup_audio_path(s, idx, idt, path)
            if not path or not os.path.isfile(path):
                n_no_audio += 1
                self._log("⚠ 曲目「%s」有指纹但找不到音频文件 ⇒ 导出时不带它的指纹"
                          % (getattr(idt, "title", "") or getattr(idt, "file_name", "")
                             or ("#%d" % (int(idx) + 1))))
                continue
            targets.append((int(idx), fid, path))
        return targets, n_no_id, n_no_audio

    def _collect_export_fp(self) -> tuple:
        """导出前汇总指纹包 → `({fp_id: 打包字节}, 说明文字)`。

        ★ 为什么要有这一步（用户原话「理论来讲 era 导入就应该要识别到」）：
          `.ercue` 里只写 `identity.fp_id`（**16 个字符的摘要**），演出端拿它认不出音频。
          把**完整哈希序列**（`fp_pack` 格式）带进包，演出端导入后灌进自己的指纹库
          ⇒ 歌不在那台机器上，放歌也能认出来。

        ★ 优先走**身份扫描时的内存缓存**（`identity_scan.HASH_CACHE`）：
          命中 ⇒ 导出时直接 `pack_hashes`（几十 ms），**不重新解整曲**
          （2.7 s/首 × N 会明显拖慢导出）。没扫过的曲目才**后台线程补算 + 进度条**
          （`_run_bg_with_progress`，无头模式同步跑）。

        ★ 失败**绝不阻断导出**：算不出的曲目只是"包里少一个成员"，提示写在日志/状态行。
        """
        targets, n_no_id, n_no_audio = self._export_fp_targets()
        if not targets:
            return {}, "", []

        packs = {}
        todo = []
        reasons = {}          # fp_id → 为什么没算出来（给用户看，也给日志看）
        for idx, fid, path in targets:
            hashes = identity_scan.cached_hashes(path, fid)
            if hashes:
                try:
                    blob = fp_pack.pack_hashes(hashes)
                except Exception as e:                     # noqa: BLE001
                    log.exception("打包指纹失败（走补算）：%s", path)
                    self._log("⚠ 曲目「%s」的指纹打包失败：%s: %s"
                              % (os.path.basename(path), type(e).__name__, e))
                    blob = b""
                if blob:
                    packs[fid] = blob
                    continue
            todo.append((idx, fid, path))

        if todo:
            self._log("有 %d 首曲目的指纹还没算过 ⇒ 导出前补算（后台进行，不影响操作）…"
                      % len(todo))
            try:
                got, why = self._run_bg_with_progress(
                    lambda cb: self._compute_fp_packs(todo, cb),
                    "正在计算 %d 首曲目的音频指纹…" % len(todo))
            except Exception as e:                         # noqa: BLE001
                log.exception("补算曲目指纹失败（不影响导出）")
                self._log("⚠ 补算曲目指纹失败：%s: %s" % (type(e).__name__, e))
                got, why = {}, {t[1]: "%s: %s" % (type(e).__name__, e) for t in todo}
            packs.update(got or {})
            for _idx, _fid, _p in todo:
                if _fid not in (got or {}):
                    reasons[_fid] = ((why or {}).get(_fid) or "未算出（原因不明）")

        n_ok = len(packs)
        n_fail = len(targets) - n_ok
        notes = []
        if n_fail:
            notes.append("%d 首没带指纹" % n_fail)
        if n_no_id:
            notes.append("%d 首没有指纹 id（跳过）" % n_no_id)
        note = ("（%s）" % "，".join(notes)) if notes else ""
        failures = []
        for idx, fid, path in targets:
            if fid in packs:
                continue
            failures.append((os.path.basename(str(path or "")) or ("fp_id=%s" % fid),
                             reasons.get(fid, "未算出（原因不明）")))
        if n_fail:
            self._log("⚠ 有 %d 首曲目没带指纹（演出端仍可按 md5/元信息认歌）。" % n_fail)
        return packs, note, failures

    @staticmethod
    def _compute_fp_packs(targets, progress_cb=None) -> tuple:
        """**后台线程**为 `targets` 逐首解码 + 算哈希 + 打包 → `{fp_id: 字节}`。

        `targets` = `[(index, fp_id, 音频路径), ...]`（见 `_export_fp_targets`）。
        每首：`identity_scan.compute_hashes()`（= 与身份扫描**完全同一条**解码/STFT 路径）
        → 断言签名 == 码表里的 `fp_id`（音频被换过就不写，否则演出端会认错歌）
        → `fp_pack.pack_hashes()`。

        ⚠ 逐首算完**立刻丢掉 hashes**，只留打包字节（打包后约 500 KB/首，比哈希序列小得多）。
        单首失败只记日志，**不抛、不影响其他曲目、不阻断导出**。
        """
        out, why = {}, {}
        total = max(1, len(targets))
        for i, item in enumerate(targets):
            try:
                _idx, fid, path = item
            except (TypeError, ValueError):
                continue
            name = os.path.basename(str(path or ""))
            if progress_cb is not None:
                try:
                    progress_cb(i, total, name)
                except Exception:                          # noqa: BLE001
                    pass
            try:
                hashes, sig = identity_scan.compute_hashes(path)
                if not hashes:
                    # ★ 必须区分"文件读不了/解码失败"与"算法问题"：`compute_hashes`
                    #   自己不抛（返回空），所以这里要再探一次文件是否存在。
                    _why = ("音频文件不存在" if not os.path.isfile(path)
                            else "解码/算哈希失败（详见 ercue.log）")
                    log.warning("补算指纹为空（跳过）：%s ⇒ %s", path, _why)
                    why[fid] = _why
                    continue
                if sig != fid:
                    # 音频文件被换过 / 与码表记录的不是同一份 ⇒ 写进去反而让演出端认错歌
                    log.warning("指纹校验不符（码表 %s ≠ 现算 %s），不写该曲目：%s",
                                fid, sig, path)
                    why[fid] = "音频文件与码表记录不一致（现算 %s ≠ 码表 %s）" % (sig, fid)
                    continue
                blob = fp_pack.pack_hashes(hashes)
                if blob:
                    out[fid] = blob
                    # ★ 顺手回填内存缓存：用户点"重试"就不用再算一遍（8 秒/首）
                    try:
                        identity_scan.HASH_CACHE.put(path, fid, hashes)
                    except Exception:                      # noqa: BLE001
                        pass
            except Exception as e:                         # noqa: BLE001
                log.exception("补算曲目指纹失败：%s", path)
                why[fid] = "%s: %s" % (type(e).__name__, e)
        if progress_cb is not None:
            try:
                progress_cb(total, total, "")
            except Exception:                              # noqa: BLE001
                pass
        return out, why

    def _warn_missing_bpm(self) -> bool:
        """导出前**提示**「有曲目没填 BPM」。返回 True = 继续导出（**永远**）。

        ★★ 2026-10-18 事实纠正：没填 BPM **不影响演出**（两端换算一致、位置取自原曲秒数），
          只在编辑器里没法按小节查看/对齐 ⇒ 这里从「默认取消的确认框」降级为
          **一次性提示**（Information，不提供"取消导出"），绝不挡用户导出。

        规则：
          · 有没填 BPM 的曲目 ⇒ 逐首列出（曲名 — 艺术家）+ 说清"不影响演出、只是没法按小节对齐"；
          · **非阻塞**（用户点掉提示即可），且**永远返回 True**；
          · ⚠ 只有 `_interactive=True` 且存在 QApplication 才弹 ——
            测试/无头模式**绝不阻塞**（项目既有约定，见 `__init__:self._interactive`）。

        Returns:
            恒为 True（保留返回值是为了不破坏既有调用点与测试的语义）。
        """
        bad = tracks_without_bpm(self.session.sheet if self.session else None)
        if not bad:
            return True
        text = bpm_warn_text(bad)
        names = "、".join(t for t, _a in bad)
        # 日志**一律**记（无头/强制导出也留痕）；用「提示」而不是「警告」的语气
        self._log("%d 首曲目没填 BPM（不影响演出，只是没法按小节查看/对齐）：%s"
                  % (len(bad), names))
        if not self._interactive or QApplication.instance() is None:
            return True                             # 无头：只记日志，绝不阻塞
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, "有曲目没填 BPM（可选）", text)
        return True
        return False

    def export_sheet(self, path, force=False, embed=None) -> bool:
        """导出 `.ercue`。`force=True` 跳过弹窗（命令行 / 测试用）。

        流程：① 纯函数检查 → ② **曲目未设 BPM 警告** → ③ 弹窗（缺素材默认焦点在「取消」）
              → ④ 内嵌体积保护 → ⑤ 写包。
        """
        s = self.session
        if s is None:
            return False
        if not str(path).lower().endswith(ercue_io.EXT):
            path += ercue_io.EXT

        # ① 导出前检查（纯函数；缺素材=重要提示，超长/未吸附=警告）
        report = self.pre_export_report()
        for line in report.summary_lines():
            self._log("⚠ " + line)

        # ② 「曲目没填 BPM」的**提示**（不是错误：不影响演出，只是没法按小节对齐）
        #    ⚠ 它**永不阻止导出**（`_warn_missing_bpm()` 恒返回 True）。
        #    `force=True` 是命令行/测试的"我就是要导"语义 ⇒ 连提示都不弹。
        if not force:
            self._warn_missing_bpm()

        # ③ 弹窗（只在交互模式；测试 _interactive=False 时绝不阻塞）
        do_embed = bool(self._embed_pref) if embed is None else bool(embed)
        if self._interactive and not force:
            decision = dialogs.ask_export(self, report, embed_default=do_embed)
            if not decision.proceed:
                self._log("已取消导出。")
                return False
            do_embed = decision.embed

        # ④ 内嵌素材体积**提醒**（超 1 GB ⇒ 弹窗询问，用户可选继续内嵌或改不内嵌）
        #    ⚠ 2026-10-17 用户要求：取消原 500MB 硬上限（旧行为是"超限就自动取消内嵌、不给选择"）
        if do_embed:
            over, total, _names = self._embed_size_check()
            human = precheck.human_mb(total)
            if over:
                if self._interactive and not force:
                    from PySide6.QtWidgets import QMessageBox
                    r = QMessageBox.question(
                        self, "内嵌素材较大（超过 1 GB）",
                        "本次要内嵌的素材共 %s，超过 1 GB。\n\n"
                        "导出的 .ercue 会比较大：导出、拷贝、对方打开都会慢一些。\n"
                        "是否仍然内嵌？\n\n"
                        "（选「No」＝只导出码表不内嵌素材，"
                        "对方机器需要有同一素材库）" % human,
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes)
                    if r != QMessageBox.Yes:
                        do_embed = False
                        self._log("素材共 %s（超过 1 GB）：选择不内嵌。" % human)
                    else:
                        self._log("素材共 %s（超过 1 GB）：确认继续内嵌。" % human)
                else:
                    self._log("⚠ 内嵌素材共 %s（超过 1 GB）。" % human)
        self._embed_pref = bool(do_embed)
        self.cfg.set("embed_media", bool(do_embed))

        # ⑤ 包内缩略图（thumbs/<clip_id>.png）：只给用到的素材做；
        #    **后台线程 + 进度条**（单个 20~120ms × N，绝不能放 GUI 线程）；
        #    失败/没有素材都**不阻断导出**（包照样能导，演出端按"没有缩略图"处理）。
        thumbs_map, thumb_note = {}, ""
        if self.thumbs_enabled:
            thumbs_map, thumb_note = self._collect_export_thumbs()

        # ⑤b 包内曲目指纹（`fp/<fp_id>.bin`）：把**完整哈希序列**带上 ⇒
        #     演出端导入后即使**没有那首音频**也能认出这首歌（用户原话"理论来讲 era 导入
        #     就应该要识别到"）。优先用身份扫描时的内存缓存；没扫过的后台补算 + 进度条；
        #     任何失败都**不阻断导出**（只是少带几个成员）。
        # ★★★ 2026-10-18（用户："为什么导出不带指纹"）：
        #   旧行为是**算不出来就静默少带**，只在角落日志写一句 ⇒ 用户只能看到一个
        #   "已带 1 首（2 首没带指纹）"，既不知道为什么，也没机会补救。
        #   现在：**逐首报告原因 + 重试 / 仍然导出 / 取消** 三选一（默认重试）。
        #   ⚠ 仍然允许"不带指纹导出"——演出端还有 md5/名+艺人/时长几级兜底，
        #     硬拦着用户不让导反而更糟（铁律 9：现场优先）。
        fp_map, fp_note, fp_fail = {}, "", []
        while True:
            fp_map, fp_note, fp_fail = self._collect_export_fp()
            if not fp_fail:
                break
            _detail = "\n".join("· %s —— %s" % (n[:60], r) for n, r in fp_fail[:8])
            self._log("⚠ %d 首曲目没能带上指纹：\n%s" % (len(fp_fail), _detail))
            if not self._interactive:
                break
            from PySide6.QtWidgets import QMessageBox
            _box = QMessageBox(self)
            _box.setIcon(QMessageBox.Warning)
            _box.setWindowTitle("有 %d 首曲目的指纹没算出来" % len(fp_fail))
            _box.setText("下面这些曲目**没能带上指纹**，演出端在没有原音频的机器上"
                         "就认不出它们：\n\n%s" % _detail)
            _box.setInformativeText("「重试」只重算失败的（已成功的不会重算）；"
                                    "「仍然导出」会生成一个**不带这几首指纹**的包。")
            _b_retry = _box.addButton("重试", QMessageBox.AcceptRole)
            _b_skip = _box.addButton("仍然导出（不带指纹）", QMessageBox.DestructiveRole)
            _b_cancel = _box.addButton("取消", QMessageBox.RejectRole)
            _box.setDefaultButton(_b_retry)
            _box.exec()
            if _box.clickedButton() is _b_cancel:
                self._log("已取消导出（指纹没算出来）")
                return False
            if _box.clickedButton() is _b_skip:
                break

        # ⑥ 写包
        try:
            ercue_io.save(s.sheet, path, media_paths=s.media_paths, embed=do_embed,
                          app_version=APP_VERSION,
                          generator={"tool": "EasyRealityCue", "version": APP_VERSION},
                          thumbs=thumbs_map, fp_packs=fp_map)
        except Exception as e:                         # noqa: BLE001
            log.exception("导出失败: %s", path)
            self._log("导出失败：%s: %s" % (type(e).__name__, e))
            return False
        s.backing_path = path
        s.dirty = False
        self.cfg.add_recent(path)
        self._fill_recent_menu()
        n_thumbs = len(thumbs_map)
        thumb_txt = ("已带 %d 张缩略图%s" % (n_thumbs, thumb_note)) if n_thumbs else "无缩略图"
        # ★ 「已带 N 首曲目的指纹」：让用户（和演出端那台机器）**看得见**认歌能力随包走
        n_fp = len(fp_map)
        fp_txt = ("已带 %d 首曲目的指纹%s" % (n_fp, fp_note)) if n_fp else \
            ("没带指纹%s" % fp_note if fp_note else "没带指纹")
        self._log("已导出：%s%s（%s；%s）"
                  % (path, "（素材已内嵌）" if do_embed else "", thumb_txt, fp_txt))
        # ★ 第八轮（用户反馈："导出成功没有提醒"）：日志在角落看不见 ⇒ **明确弹窗反馈**
        if self._interactive and not force:
            from PySide6.QtWidgets import QMessageBox
            _size_txt = ""
            try:
                _sz = int(os.path.getsize(path))
                _size_txt = "（%s）" % precheck.human_mb(_sz) if _sz < (1024 ** 3) \
                    else "（%.2f GB）" % (_sz / float(1024 ** 3))
            except OSError:
                pass
            QMessageBox.information(
                self, "导出成功",
                "码表已导出：\n\n%s\n%s\n\n%s\n%s\n%s" % (
                    path, _size_txt,
                    "素材已内嵌 —— 换台机器也能直接打开。"
                    if do_embed else
                    "未内嵌素材 —— 对方机器需要有同一素材库。",
                    thumb_txt, fp_txt))
        self._refresh_all()
        self._flush_autosave()                         # ★ T02：关键点强制落盘（导出后，dirty=False 也存下来）
        return True

    # ==================================================================
    # 素材
    # ==================================================================
    def _add_media_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        files, _ = QFileDialog.getOpenFileNames(
            self, "添加素材", self.session.dir if self.session else "",
            "视频 / 图片 (*.mp4 *.mov *.webm *.avi *.mkv *.png *.jpg *.jpeg *.bmp);;所有文件 (*)")
        if files:
            self.add_media_paths(files)

    def add_media_paths(self, paths) -> list:
        """把若干素材文件加进当前码表（**只记路径，不拷贝文件**）。

        同名素材已在表中则只补路径、不重复加。返回新增/命中的 clip_id 列表。
        """
        s = self.session
        if s is None:
            return []
        if not s.dir and paths:
            s.dir = os.path.dirname(os.path.abspath(paths[0]))
        self._push_undo()
        ids = []
        for p in paths:
            p = os.path.abspath(str(p))
            name = os.path.basename(p)
            ref = next((m for m in s.sheet.media if m.name == name), None)
            new_ref = ref is None
            if new_ref:
                cid = "c%d" % _next_num("c", [m.clip_id for m in s.sheet.media])
                ref = MediaRef(clip_id=cid, name=name)
                s.sheet.media.append(ref)
            s.media_paths[ref.clip_id] = p
            # ★★ 2026-10-19 审计修复：**没探测过就补探**，不只看 "是否新增"。
            #   旧写法只在新增时探测 ⇒ 这些情况 `has_alpha` 永远停在 False：
            #     · 素材加入时文件不在盘上（probe 早退）
            #     · 从别人的包/旧包导入，包里写的是 false
            #   演出端把 has_alpha 当"天条"（false ⇒ 用不保留 alpha 的解码器 ⇒
            #   透明区变不透明黑 ⇒ 盖死下层 + 淡出末尾黑屏）。
            #   用 `_probed` 标记保证**每个素材只探一次**，不会每次加都重探。
            if new_ref or not getattr(ref, "_probed", False):
                self._probe_media_ref(ref, p)          # ★ B4：探测时长 / alpha 写进 MediaRef
            ids.append(ref.clip_id)
            self._log("已加素材：%s" % name)
        self._mark_dirty()
        self._refresh_all()
        return ids

    @staticmethod
    def _probe_media_ref(ref, path):
        """★ B4：把 `probe.probe()` 的**时长 / alpha** 结论写进 `MediaRef`。

        只在**尚未探测过**时调用（用实例属性 `_probed` 记住，不写进 .ercue）；
        探测失败（`error` 非空 / 抛异常）**容错跳过**，绝不阻断导入
        —— 探测不到的素材照常能编辑，只是 precheck 少一条超长提示。
        """
        if ref is None or not path or not os.path.isfile(path):
            return
        try:
            from ercue.media.probe import probe
            r = probe(path)
        except Exception:                              # noqa: BLE001
            log.exception("素材探测失败: %s", path)
            return
        if r is None or not getattr(r, "ok", False):
            return
        if float(getattr(r, "duration_sec", 0.0) or 0.0) > 0.0:
            ref.duration_sec = float(r.duration_sec)
        ref.has_alpha = bool(r.has_alpha)
        # ★ 记住"已探测过"（实例属性，不写进 .ercue）⇒ 再次加同一素材时不会重复探测。
        try:
            ref._probed = True
        except Exception:                                        # noqa: BLE001
            pass

    def remove_media(self, clip_id):
        s = self.session
        if s is None:
            return
        # ★★ 2026-10-19 审计修复：**删素材前确认 + 一并清理引用**。
        #   旧实现直接删：图层里指向它的片段全部变成"悬空 clip_id"，
        #   演出端会静默跳过（画面凭空消失）。现在有引用就明确问一次，
        #   确认后把那些片段一起删掉（撤销栈可回退）。
        _refs = []
        try:
            for _tr in s.sheet.tracks:
                for _ly in getattr(_tr, "layers", []) or []:
                    for _it in getattr(_ly, "items", []) or []:
                        if str(getattr(_it, "clip_id", "")) == str(clip_id):
                            _refs.append((_ly, _it))
        except Exception:                                        # noqa: BLE001
            _refs = []
        if _refs:
            from PySide6.QtWidgets import QMessageBox
            r = QMessageBox.question(
                self, "删除素材",
                "有 %d 个片段正在使用这个素材。\n\n"
                "继续会把它们一起移除（可以撤销）。" % len(_refs),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self._push_undo()
        for _ly, _it in _refs:
            try:
                _ly.items = [x for x in _ly.items if x is not _it]
            except Exception:                                    # noqa: BLE001
                pass
        s.sheet.media = [m for m in s.sheet.media if m.clip_id != clip_id]
        s.media_paths.pop(clip_id, None)
        self._mark_dirty()
        self._refresh_all()
        self._log("已从码表里去掉素材：%s" % clip_id)

    def _reveal_media(self, clip_id):
        s = self.session
        p = s.media_paths.get(clip_id, "") if s is not None else ""
        if not p or not os.path.isfile(p):
            self._log("这个素材没找到文件，用「指定素材目录…」重挂。")
            return
        try:
            if os.name == "nt":
                os.startfile(os.path.dirname(p))       # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["open", os.path.dirname(p)])
        except Exception:                              # noqa: BLE001
            log.exception("定位素材失败: %s", p)
            self._log(p)

    def _pick_media_dir_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        s = self.session
        if s is None:
            return
        d = QFileDialog.getExistingDirectory(self, "选择素材目录", s.dir or "")
        if d:
            s.dir = d
            self._remount_media()
            self._refresh_all()
            self._log("素材目录已设为：%s" % d)

    def _remount_media(self):
        """按文件名在 session.dir 里重新挂载丢失素材（**递归扫描**，规格书 §1.3 ②）。

        旧实现只扫一层；这里递归到 `SCAN_MAX_DEPTH` 层并跳过系统 / 回收站目录。
        """
        s = self.session
        if s is None or not s.dir:
            return
        need = [m for m in s.sheet.media
                if not os.path.isfile(s.media_paths.get(m.clip_id) or "")]
        if not need:
            return
        index = _scan_media_index(s.dir, SCAN_MAX_DEPTH)
        found = 0
        for m in need:
            cand = index.get(m.name)
            if cand and os.path.isfile(cand):
                s.media_paths[m.clip_id] = cand
                found += 1
        if found:
            self._log("已按文件名重挂 %d 个素材（递归扫描）。" % found)

    def _add_media_to_playhead(self, clip_id):
        self._add_cue_at_playhead(clip_id)

    # ==================================================================
    # 曲目
    # ==================================================================
    def add_track(self, title="新曲目", artist="", duration_sec=0.0, bpm=0.0) -> int:
        s = self.session
        if s is None:
            return -1
        self._push_undo()
        te = ercue_io.new_track(title, artist, duration_sec, bpm)
        te.identity.title = title or "新曲目"
        te.identity.ensure_meta_id()
        if not te.track_key:
            te.track_key = te.identity.meta_id[:12] or "t%d" % (len(s.sheet.tracks) + 1)
        te.composition.layers.append(CompLayer(
            lyr_id="L%d" % _next_num("L", [l.lyr_id for t in s.sheet.tracks for l in t.layers]),
            name="层 1", z=0))
        s.sheet.tracks.append(te)
        self._ti = len(s.sheet.tracks) - 1
        self._mark_dirty()
        self._log("已加一首曲目：%s（默认给了一层，可再「＋ 层」）" % te.identity.title)
        self._refresh_all()
        return self._ti

    def del_track(self):
        s = self.session
        if s is None or not s.sheet.tracks:
            return
        self._push_undo()
        i = max(0, min(int(getattr(self, "_ti", 0) or 0), len(s.sheet.tracks) - 1))
        s.sheet.tracks.pop(i)
        # ★ 2026-10-19 审计修复：`track_audio` 是按**序号**存的 ⇒ 删一首后后面每首的
        #   试听/时长/BPM 识别/指纹全部错位到相邻曲目（注释里早就承认过这个坑）。
        #   这里把该键删掉，并把 > i 的键整体 -1。
        try:
            _ta = dict(getattr(s, "track_audio", {}) or {})
            _ta.pop(i, None)
            s.track_audio = {(k - 1 if k > i else k): v for k, v in _ta.items()}
        except Exception:                                        # noqa: BLE001
            pass
        self._ti = max(0, i - 1)
        self._mark_dirty()
        self._refresh_all()
        self._log("已删除曲目。")

    def _on_track_picked(self, idx):
        s = self.session
        if s is None:
            return
        self._stop_play()                              # 换歌先停播（音频不留线程）
        self._stop_identity_scan()                     # ★ 换歌 ⇒ 丢掉上一首的身份扫描
        self._ti = int(idx)
        self._refresh_all()
        self._rebuild_runtime()
        self._preload_audio()                          # 预载这首歌的音频
        self._autosave_touch(s.key)                    # ★ T02：切曲目是关键点（快照连同"当前曲目下标"）

    def _on_track_edited(self):
        s = self.session
        tr = self._track()
        if s is None or tr is None:
            return
        self._push_undo()
        vals = self.track_panel.values()
        idt = tr.identity
        idt.title = vals["title"]
        idt.artist = vals["artist"]
        idt.duration_sec = vals["duration_sec"]
        idt.bpm = vals["bpm"]
        idt.meta_id = Identity.meta_key(idt.title, idt.artist, idt.duration_sec)
        tr.track_key = idt.meta_id or tr.track_key
        self._mark_dirty()
        self._refresh_all()
        self._log("曲目信息已更新（认歌用「曲名 + 艺术家 + 时长」）。")

    def _import_track_audio_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        p, _ = QFileDialog.getOpenFileName(
            self, "导入音频", self.session.dir if self.session else "",
            "音频 (*.mp3 *.flac *.wav *.m4a *.aac *.ogg *.opus);;所有文件 (*)")
        if p:
            self.import_track_audio(p)

    def import_track_audio(self, path) -> bool:
        """用 mutagen 读标签与时长，填进当前曲目的 identity（只读信息，不拷贝文件）。"""
        tr = self._track()
        if tr is None:
            self._log("先加一首曲目（「＋ 曲目」）再导入音频。")
            return False
        meta = self._read_audio_meta(path)
        self._push_undo()
        idt = tr.identity
        if meta["title"]:
            idt.title = meta["title"]
        if meta["artist"]:
            idt.artist = meta["artist"]
        if meta["duration_sec"]:
            idt.duration_sec = meta["duration_sec"]
        idt.file_name = os.path.basename(path)
        idt.meta_id = Identity.meta_key(idt.title, idt.artist, idt.duration_sec)
        tr.track_key = idt.meta_id or tr.track_key
        s = self.session
        if s is not None:
            i = max(0, min(int(getattr(self, "_ti", 0) or 0), len(s.sheet.tracks) - 1))
            s.track_audio[i] = os.path.abspath(path)
            if not s.dir:
                s.dir = os.path.dirname(os.path.abspath(path))
        self._mark_dirty()
        self._refresh_all()
        self._preload_audio()                          # 导入即预载整首（试听无需等）
        self._log("已导入音频信息：%s（%.1f 秒）" % (idt.title, idt.duration_sec))
        return True

    @staticmethod
    def _read_audio_meta(path) -> dict:
        """mutagen 读标签与时长；失败返回空（绝不抛）。"""
        out = {"title": "", "artist": "", "duration_sec": 0.0}
        try:
            import mutagen
            f = mutagen.File(path, easy=True)
            if f is None:
                return out
            if f.tags:
                t = f.tags.get("title")
                a = f.tags.get("artist")
                out["title"] = str(t[0]) if isinstance(t, list) and t else (str(t) if t else "")
                out["artist"] = str(a[0]) if isinstance(a, list) and a else (str(a) if a else "")
            out["duration_sec"] = float(getattr(f.info, "length", 0.0) or 0.0)
        except Exception:                              # noqa: BLE001
            log.exception("读取音频标签失败: %s", path)
        return out

    # ==================================================================
    # 层 / 片段
    # ==================================================================
    def add_layer(self, name=""):
        tr = self._track()
        if tr is None:
            self._log("先加一首曲目。")
            return None
        self._push_undo()
        z = max([int(l.z) for l in tr.layers], default=-10) + 10
        lid = "L%d" % _next_num("L", [l.lyr_id for t in self.session.sheet.tracks for l in t.layers])
        ly = CompLayer(lyr_id=lid, name=name or ("层 %d" % (len(tr.layers) + 1)), z=z)
        tr.layers.append(ly)
        self._mark_dirty()
        self._log("已加一层：%s" % ly.name)
        self._refresh_all()
        return ly

    def del_layer(self):
        tr = self._track()
        if tr is None:
            self._log("先加一首曲目。")
            return
        # ★ P0-3：删的必须是**用户显式选中的那一层**，没有选中就什么都不删（绝不静默删最上层）。
        ly = self._explicit_selected_layer()
        if ly is None:
            self._log("先在时间轴上点一下要删的那一层（点它上面的片段）再删除。")
            return
        if len(tr.layers) <= 1:
            self._log("至少要留一层。")
            return
        self._push_undo()
        tr.layers.remove(ly)
        self.timeline.set_selected("", "")
        self._mark_dirty()
        self._refresh_all()
        self._log("已删除层：%s" % (ly.name or ly.lyr_id))

    def _add_cue_at_playhead(self, clip_id=None):
        """在播放头位置给当前层加一个片段。

        ⚠ 入口已变（需求 5 移除了「＋ cue」按钮）：现在由**素材面板双击 / 拖到层上**触达。
        """
        ly = self.timeline.selected_layer()
        if ly is None:
            self._log("先加一首曲目并建一层。")
            return None
        # ★ P0-4 + 需求5：没有任何素材时**绝不去造一个空 clip_id 的片段**（那会画成横贯全屏的
        #   「文件丢失」红块）。这里先明确提示先加素材。
        if not self._has_media():
            self._log("这张码表还没有素材：先在「③ 素材」里「＋ 素材…」再加片段。")
            return None
        if clip_id is None:
            clip_id = self.props_panel.cb_clip.currentData() or ""
        if not clip_id:
            clip_id = self.session.sheet.media[0].clip_id
        # 兜底：解析出来仍是空（坏素材）⇒ 不建片段
        if not clip_id:
            self._log("先在「④ 片段属性」里选一个素材再加片段。")
            return None
        return self.add_cue(ly.lyr_id, clip_id, self._pos)

    def add_cue(self, lyr_id, clip_id, at_sec):
        """在当前层 `lyr_id` 的 `at_sec`（秒）处放一个片段。

        ★ 时长 = **素材真实时长**（用户反馈的两条根因一次解决）：
          旧实现把 `end_sec` 留成 `None`（"到本层下一个片段 / 曲终"），而时间轴绘制把
          `end_sec=None` 的片段一直画到**下一个片段的起点或视口右端** ⇒ 一个 3 分钟的
          素材会被"拉成更长时间"，甚至**铺满整条时间轴**（用户反馈 ①/④）。
          现在**只要量得到素材时长**就写 `end_sec = at + 时长`；量不到（探测失败）才留 None。
        """
        s = self.session
        if s is None:
            return None
        ly = next((l for l in self._layers() if l.lyr_id == lyr_id), None)
        if ly is None:
            return None
        self._push_undo()
        iid = "i%d" % _next_num("i", _all_item_ids(s.sheet))
        at = max(0.0, float(at_sec))
        dur = self._clip_duration_sec(clip_id)
        end = (at + dur) if dur > 0.0 else None
        it = CueItem(item_id=iid, clip_id=clip_id, at_sec=at, end_sec=end,
                     trans_in="fade", trans_in_sec=2.0,
                     trans_out="fade", trans_out_sec=2.0).normalized()
        # ★ 需求 4：**GIF 片段绝不设 src_out_sec**（设了就会"到达出点冻结"、循环失效）。
        #   新片段本就不设；这里再兜一道，防止将来有人给它写值。
        if probe.is_gif(s.media_paths.get(clip_id) or ""):
            it.src_out_sec = None                      # 循环由 frames 侧按一轮时长环绕实现
        ly.items.append(it)
        ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self._sync_timeline_data()
        self.timeline.set_selected(ly.lyr_id, it.item_id)
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        return it

    def _clip_duration_sec(self, clip_id) -> float:
        """某个素材放到时间轴上的**默认时长（秒）**；量不到返回 0.0。

        顺序：
          ⓪ ★ **需求 4：图片素材**（`.png/.jpg/...`）没有"文件时长"，其**摆放默认时长**
             取 `probe.IMAGE_DEFAULT_SEC`（=5.0）。⚠ 这是**摆放策略**，**不改** `MediaRef.duration_sec`
             / `probe().duration_sec` 那些**文件属性**（后者对图片仍是 0.0）。
          ① `MediaRef.duration_sec`（新增素材时 `_probe_media_ref` 已写）
          ② 现场用 `_audio_duration_sec()`（mutagen / PyAV 容器时长，**音视频通用**）兜底并回填。
        """
        s = self.session
        if s is None or not clip_id:
            return 0.0
        ref = s.sheet.media_by_id(clip_id) if s.sheet is not None else None
        p = s.media_paths.get(clip_id) or ""
        name = p or str(getattr(ref, "name", "") or "")
        if name and probe.kind_of(name) == "image":
            return float(probe.IMAGE_DEFAULT_SEC)      # ★ 需求 4：图片默认摆 5 秒
        if ref is not None and float(getattr(ref, "duration_sec", 0.0) or 0.0) > 0.0:
            return float(ref.duration_sec)
        if p and os.path.isfile(p):
            d = self._audio_duration_sec(p)          # 容器时长（视频/音频都能读）
            if d > 0.0:
                if ref is not None:
                    ref.duration_sec = float(d)       # 回填，避免反复探测
                return float(d)
        return 0.0

    def _clip_source_dur(self, clip_id) -> float:
        """★ A3/#20：素材**全长**（秒）—— 与 `_clip_duration_sec()` 的区别是**图片返回 0**。

        收敛关系 `end - at == (src_out_eff - src_in) / speed` 里，"源长度未知"就**无法收敛**
        （`reconcile_item_timing` 返回 `'none'`）：图片没有"源长度"概念（其摆放时长是用户自定义
        策略），故这里**图片一律返回 0.0** ⇒ 图片不参与变速收敛（符合设计 §1.A3 边界）。
        视频 / 音频 / GIF：`MediaRef.duration_sec`（新增素材时 `_probe_media_ref` 已回填）
        → 现场 `_audio_duration_sec()` 兜底并回填。**量不到返回 0.0**（此时不收敛）。
        """
        s = self.session
        if s is None or not clip_id:
            return 0.0
        ref = s.sheet.media_by_id(clip_id) if s.sheet is not None else None
        p = s.media_paths.get(clip_id) or ""
        name = p or str(getattr(ref, "name", "") or "")
        if name and probe.kind_of(name) == "image":
            return 0.0                                 # ★ 图片：无"源长度"，不收敛
        if ref is not None and float(getattr(ref, "duration_sec", 0.0) or 0.0) > 0.0:
            return float(ref.duration_sec)
        if p and os.path.isfile(p):
            d = self._audio_duration_sec(p)            # 容器时长（视频/音频都能读）
            if d > 0.0:
                if ref is not None:
                    ref.duration_sec = float(d)        # 回填，避免反复探测
                return float(d)
        return 0.0

    def del_cue(self):
        ly, it = self.timeline.selected_item()
        if ly is None or it is None:
            self._log("先在时间轴上点一个片段。")
            return
        self._push_undo()
        ly.items.remove(it)
        self.timeline.set_selected("", "")
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("已删除片段 %s" % it.item_id)

    # ==================================================================
    # 时间轴信号处理（统一落模型）
    # ==================================================================
    def _select(self, lyr_id, item_id):
        self.timeline.set_selected(lyr_id, item_id)
        self._sync_props_selection()

    @staticmethod
    def _shift_item(it, new_at):
        """把片段平移到 `new_at`，**保持时长**（PR 惯例：拖动的是整块，不是起点）。

        · `end_sec is None`（= "到本层下一个片段 / 曲终"）→ **保持 None**，不凭空造值。
        · `end_sec < at_sec` 之类的坏数据 → 时长钳成 0，**绝不产生负时长**。
        """
        old_at = units.to_float(it.at_sec)
        new_at = max(0.0, units.to_float(new_at))
        dur = None if it.end_sec is None else max(0.0, units.to_float(it.end_sec) - old_at)
        it.at_sec = new_at
        if dur is not None:
            it.end_sec = new_at + dur

    def _on_timeline_moved(self, lyr_id, item_id, at_sec):
        ly, it = self.timeline.selected_item()
        if it is None:
            return
        self._push_undo()
        self._shift_item(it, at_sec)              # ★ 平移保持时长（旧写法会把时长压成 0）
        ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("移动片段到 %s" % mmss(it.at_sec))

    def _on_timeline_resized(self, lyr_id, item_id, at_sec, end_sec):
        ly, it = self.timeline.selected_item()
        if it is None:
            return
        self._push_undo()
        it.at_sec = max(0.0, float(at_sec))
        it.end_sec = None if end_sec is None else max(it.at_sec, float(end_sec))
        ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()

    def _on_timeline_layer_moved(self, old_lyr, item_id, new_lyr, at_sec):
        tr = self._track()
        if tr is None:
            return
        src = next((l for l in tr.layers if l.lyr_id == old_lyr), None)
        dst = next((l for l in tr.layers if l.lyr_id == new_lyr), None)
        if src is None or dst is None or src is dst:
            return
        it = next((x for x in src.items if x.item_id == item_id), None)
        if it is None:
            return
        self._push_undo()
        src.items.remove(it)
        self._shift_item(it, at_sec)              # ★ 跨层同样保持时长
        dst.items.append(it)
        dst.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self._sync_timeline_data()
        self.timeline.set_selected(dst.lyr_id, it.item_id)
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("已把片段移到「%s」" % (dst.name or dst.lyr_id))

    def _on_clip_dropped(self, lyr_id, clip_id, at_sec):
        self.add_cue(lyr_id, clip_id, at_sec)
        self._log("已把素材加到「%s」的 %s" % (lyr_id, mmss(at_sec)))

    # ==================================================================
    # ★ 冻结接口的 window 侧动作（信号由 T04/T06 提供，见 `_connect_if`）
    # ==================================================================
    def _find_item(self, lyr_id, item_id):
        """按 id 找 `(layer, item)`（遍历**全部层**，不依赖当前选中；找不到返回 `(None, None)`）。"""
        for ly in self._layers():
            if lyr_id and ly.lyr_id != lyr_id:
                continue
            for it in ly.items:
                if it.item_id == item_id:
                    return ly, it
        return None, None

    # ---- 需求 3：拖拽剪辑（trim）松手 → 落模型 ----
    def _on_timeline_trimmed(self, lyr_id, item_id, at_sec, end_sec, src_in_sec, src_out_sec):
        """**模型的最终写入在 window**（控件只发信号）：时间轴 + 源内起止一起写。"""
        ly, it = self._find_item(lyr_id, item_id)
        if it is None:
            return
        self._push_undo()
        it.at_sec = max(0.0, float(at_sec))
        it.end_sec = None if end_sec is None else max(it.at_sec, float(end_sec))
        it.src_in_sec = max(0.0, float(src_in_sec))
        it.src_out_sec = None if src_out_sec is None else max(0.0, float(src_out_sec))
        # ★ 需求 4：GIF 不设 src_out（设了就会"到出点冻结"、循环失效）
        if self._is_gif_clip(it.clip_id):
            it.src_out_sec = None
        it.normalized()
        ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("已剪辑片段 %s" % it.item_id)

    def _is_gif_clip(self, clip_id) -> bool:
        s = self.session
        p = (s.media_paths.get(clip_id) or "") if s is not None else ""
        return bool(p) and probe.is_gif(p)

    # ---- 需求 12：片段右键菜单（**菜单在 window 构建**，符合"控件只发信号、窗口落模型"）----
    def _build_item_menu(self, lyr_id, item_id) -> QMenu:
        """构建片段右键菜单（**只构建、不弹出** —— 便于单测直接触发动作）。

        最小集（设计文档裁定）：`属性 / 复制片段 / 重置画面变换 / 定位素材 / 删除片段`。
        """
        ly, it = self._find_item(lyr_id, item_id)
        m = QMenu(self)
        if it is None:                                   # 片段已不在（竞态）⇒ 空菜单，绝不崩
            return m
        m.addAction("属性").triggered.connect(self._focus_props_tab)
        m.addAction("复制片段").triggered.connect(self._copy_selection)   # ★ 第九轮：多选⇒整组
        m.addAction("重置画面变换").triggered.connect(
            lambda: self._reset_item_xform(lyr_id, item_id))
        m.addAction("定位素材").triggered.connect(lambda: self._reveal_media(it.clip_id))
        m.addSeparator()
        m.addAction("删除片段").triggered.connect(self.del_cue)      # 与 Delete 键同一实现
        return m

    def _on_timeline_menu(self, lyr_id, item_id, global_pos):
        ly, it = self._find_item(lyr_id, item_id)
        if it is None:
            return
        self.timeline.set_selected(lyr_id, item_id)
        self._sync_props_selection()
        # ⚠ 无头/测试模式**绝不弹菜单**（`menu.exec()` 会阻塞）。`_build_item_menu()` 仍可单测。
        if not self._interactive or QApplication.instance() is None:
            return
        try:
            self._build_item_menu(lyr_id, item_id).exec(global_pos)
        except Exception:                                # noqa: BLE001
            log.exception("弹出片段菜单失败")

    def _focus_props_tab(self):
        self.tabs.setCurrentWidget(self.props_panel)

    def _copy_item(self, lyr_id, item_id, at=None):
        """★ 需求 12「复制片段」/ 第五轮②（#18）「再制」：**必须走 `CueItem.deep_clone()`**
        （杜绝浅拷贝被共享，需求 10）。

        落点：`at` 给定则用它（**再制** = 鼠标落点）；否则用**播放头**（若播放头正好压在原片段起点，
        则放到原片段**之后**，免完全重叠看不见）。
        """
        s = self.session
        ly, it = self._find_item(lyr_id, item_id)
        if s is None or it is None:
            return None
        self._push_undo()
        new_id = "i%d" % _next_num("i", _all_item_ids(s.sheet))
        dup = it.deep_clone(new_id)
        start = float(it.at_sec)
        dur = 0.0 if it.end_sec is None else max(0.0, float(it.end_sec) - start)
        if at is None:                                    # 未指定落点 ⇒ 播放头（避开原起点）
            at = max(0.0, float(self._pos))
            if abs(at - start) < 0.05:
                at = (start + dur) if dur > 0.0 else (start + 0.1)
        else:                                             # ★ 再制：用给定落点
            at = max(0.0, float(at))
        dup.at_sec = at
        if it.end_sec is not None:
            dup.end_sec = at + dur
        dup.normalized()
        ly.items.append(dup)
        ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self.timeline.set_selected(ly.lyr_id, dup.item_id)
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("已复制片段 %s → %s（"% (it.item_id, dup.item_id) + "同素材同设置，独立对象）")
        return dup

    def _reset_item_xform(self, lyr_id, item_id):
        """需求 12「重置画面变换」：`crop=None`、`xform` 回默认（需求 8 的回退入口）。"""
        ly, it = self._find_item(lyr_id, item_id)
        if it is None:
            return
        self._push_undo()
        it.crop = None
        it.xform = {"scale": 1.0, "x": 0.0, "y": 0.0}
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("已重置片段 %s 的画面变换" % it.item_id)

    # ---- 需求 8：预览画布变换「松手」提交 ----
    def _on_preview_xform_committed(self, lyr_id, item_id, xform, crop):
        ly, it = self._find_item(lyr_id, item_id)
        if it is None:
            return
        self._push_undo()
        it.xform = dict(xform) if isinstance(xform, dict) else {"scale": 1.0, "x": 0.0, "y": 0.0}
        it.crop = (list(crop) if isinstance(crop, (list, tuple)) and len(crop) == 4 else None)
        it.normalized()
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("已更新片段 %s 的画面变换" % it.item_id)

    def _on_selection_cleared(self):
        """★ T02：预览里点空白 ⇒ **取消选中**（信号由 T04 提供；"有就接"，没落地就不触发）。"""
        try:
            self.timeline.set_selected("", "")
            self._sync_props_selection()
        except Exception:                                # noqa: BLE001
            log.exception("预览点空白后取消选中失败")

    def _feed_selection_to_preview(self):
        """把当前选中片段喂给预览（需求 8：无选中 = 只读预览）。预览侧接口**有条件调用**。"""
        setter = getattr(self.preview, "set_selection", None)
        if not callable(setter):
            return
        ly, it = self.timeline.selected_item()
        try:
            setter(ly.lyr_id if ly is not None else "", it.item_id if it is not None else "")
        except Exception:                                # noqa: BLE001
            log.exception("把选中片段喂给预览失败")

    # ---- 需求 11：素材音轨显示开关 ----
    def _on_show_clip_audio(self, on):
        setter = getattr(self.timeline, "set_show_clip_audio", None)
        if callable(setter):
            try:
                setter(bool(on))
            except Exception:                            # noqa: BLE001
                log.exception("切换素材音轨显示失败")
        if on:
            self._ensure_clip_waves()                    # ★ T07：按需起后台分析（不是导入时全量扫）
        self._feed_clip_waves()
        self._log("素材音轨显示：%s" % ("开" if on else "关"))

    # ---- ★ 第五轮①（#14）：自动吸附开关 ----
    def _snap_on(self) -> bool:
        """「自动吸附」开关当前是否打开（没有该控件时视为关）。"""
        chk = getattr(self, "chk_snap", None)
        try:
            return bool(chk is not None and chk.isChecked())
        except Exception:                              # noqa: BLE001
            return False

    def _on_snap_toggled(self, on):
        """开关变化 ⇒ 推给时间轴 + 持久化到 `cfg["snap"]`。"""
        setter = getattr(self.timeline, "set_snap", None)
        if callable(setter):
            try:
                setter(bool(on))
            except Exception:                          # noqa: BLE001
                log.exception("切换自动吸附失败")
        try:
            self.cfg.set("snap", bool(on))              # ★ 与其它工具条开关一致的持久化
        except Exception:                              # noqa: BLE001
            log.exception("保存自动吸附偏好失败")
        self._log("自动吸附：%s" % ("开" if on else "关"))

    # ---- ★ 第五轮⑥（#6）：标尺点击 / 拖动 ⇒ 挪播放头 ----
    def _on_seek_requested(self, sec):
        """标尺点击或拖动 ⇒ 位置走**唯一入口** `_set_pos`。

        `defer_audio=True`：拖动中**不每帧重启音频**（与滑杆拖动同口径的防抖），
        拖动停 100ms 后自动落地（`_seek_timer`），松手不必额外 flush。
        """
        try:
            self._set_pos(max(0.0, float(sec)), defer_audio=True)
        except Exception:                              # noqa: BLE001
            log.exception("标尺 seek 失败")

    # ---- ★ 第五轮②（#18）：再制产份（落点 `at`，**原件绝不动**） ----
    def _on_duplicate_requested(self, lyr_id, item_id, at_sec):
        """在 `at_sec` 处产一份副本（复用 `_copy_item` 的 `deep_clone` 路径）。

        每次产份**各自一次** `_push_undo()`（可逐份撤销）；**不改动原件**（控件侧已保证不发 `moved`）。
        """
        return self._copy_item(lyr_id, item_id, at=float(at_sec))

    # ==================================================================
    # ★ 第九轮（框选多选）—— window 侧落模型
    # ==================================================================
    def _on_selection_count_changed(self, n):
        """选中数量变化（0/1/多）⇒ 刷新属性面板（含多选灰化）。"""
        self._sync_props_selection()

    def _on_timeline_group_moved(self, entries):
        """★ 第九轮：整组一起移动 —— **一次 undo** 落全部；各片段保时长（`_shift_item`）。"""
        s = self.session
        if s is None or not entries:
            return
        self._push_undo()
        touched = []
        for lyr_id, item_id, new_at in entries:
            ly, it = self._find_item(lyr_id, item_id)
            if it is None:
                continue
            self._shift_item(it, float(new_at))
            if ly not in touched:
                touched.append(ly)
        for ly in touched:
            ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("整组移动 %d 个片段" % len(entries))

    def _on_duplicate_group(self, entries):
        """★ 第九轮：整组复制/再制 —— **一次 undo** 产全部；各自 `deep_clone`（独立对象）。"""
        s = self.session
        if s is None or not entries:
            return []
        self._push_undo()
        made = []
        for lyr_id, item_id, new_at in entries:
            ly, it = self._find_item(lyr_id, item_id)
            if ly is None or it is None:
                continue
            new_id = "i%d" % _next_num("i", _all_item_ids(s.sheet))
            dup = it.deep_clone(new_id)
            start = float(it.at_sec)
            dur = 0.0 if it.end_sec is None else max(0.0, float(it.end_sec) - start)
            dup.at_sec = max(0.0, float(new_at))
            if it.end_sec is not None:
                dup.end_sec = dup.at_sec + dur
            dup.normalized()
            ly.items.append(dup)
            ly.items.sort(key=lambda x: (x.at_sec, x.item_id))
            made.append(dup)
        self._sync_props_selection()
        self._mark_dirty()
        self._rebuild_runtime()
        self.timeline.update()
        self._log("整组再制 %d 份" % len(made))
        return made

    def _copy_selection(self):
        """★ 第九轮：右键「复制片段」—— 多选时复制**整组**（各右移 0.5s 便于看见），单选时复制单个。"""
        try:
            ids = list(self.timeline.selection_ids())
        except Exception:                                # noqa: BLE001
            ids = []
        if len(ids) > 1:
            entries = []
            for lid, iid in ids:
                _l, m = self._find_item(lid, iid)
                if m is not None:
                    entries.append((lid, iid, float(m.at_sec) + 0.5))
            return self._on_duplicate_group(entries)
        ly, it = self.timeline.selected_item()
        if ly is not None and it is not None:
            return self._copy_item(ly.lyr_id, it.item_id)
        return None

    def _feed_clip_waves(self):
        """把 session 级素材波形缓存喂给时间轴（T04 提供 `set_clip_waves`；**有条件调用**）。"""
        setter = getattr(self.timeline, "set_clip_waves", None)
        if not callable(setter):
            return
        s = self.session
        waves = dict(getattr(s, "clip_waves", {}) or {}) if s is not None else {}
        try:
            setter(waves)
        except Exception:                                # noqa: BLE001
            log.exception("喂素材波形给时间轴失败")

    # ---- ★ T07（需求 11）：**按需**排队算素材音轨波形（后台，最多算一次） ----
    def _ensure_clip_waves(self):
        """给"当前曲目用到的素材"排队算音轨波形。

        · **按需**：只在"显示素材音轨"开关打开时由调用方触发（见 `_rebuild_runtime`），
          **不在导入时全量扫**；
        · **只算一次**：已在 `session.clip_waves` 里的不再入队；worker 内部还会按 `clip_id` 去重；
        · **磁盘缓存**：`audio/waveform` 的 npz 缓存（键含 路径|mtime|size|版本|列数）
          ⇒ 重复开关/重开软件都不重算；
        · **绝不卡 UI**：全部在 `MediaWaveWorker`（LowPriority 后台线程）里跑。
        """
        s = self.session
        tr = self._track()
        if s is None or tr is None:
            return
        if self._wave_worker is None:
            self._wave_worker = MediaWaveWorker(self)
            self._wave_worker.done.connect(self._on_clip_wave_done)
            self._wave_worker.failed.connect(self._on_clip_wave_failed)
        want = []
        for ly in tr.layers:
            for it in ly.items:
                cid = getattr(it, "clip_id", "")
                if cid and cid not in s.clip_waves and cid not in want:
                    want.append(cid)
        n = 0
        for cid in want:
            p = s.media_paths.get(cid) or ""
            if p and self._wave_worker.request(cid, p):
                n += 1
        if n:
            self._log("后台分析 %d 个素材的音轨波形（不影响播放 / 预览）…" % n)

    def _on_clip_wave_done(self, clip_id, data):
        s = self.session
        if s is None or data is None:
            return
        s.clip_waves[clip_id] = data                     # ★ 落地 session 级缓存
        self._feed_clip_waves()
        self._log("素材音轨波形就绪：%s（%d 列 / %.1f 秒）"
                  % (clip_id, int(getattr(data, "columns", 0)),
                     float(getattr(data, "duration_sec", 0.0))))

    def _on_clip_wave_failed(self, clip_id, reason):
        """**失败要可见**（绝不静默）：无音轨 / 解码失败都记一条日志。"""
        self._log("素材音轨波形跳过：%s（%s）" % (clip_id, reason))

    # ==================================================================
    # 属性面板
    # ==================================================================
    def _sync_props_selection(self):
        ly, it = self.timeline.selected_item()
        if it is None:
            ly = self.timeline.selected_layer()
        self.props_panel.layer = ly
        self.props_panel.item = it
        self.props_panel.set_media_list(self._sheet())
        self.props_panel.refresh()
        # ★ 第九轮（框选多选）：多选（>1）⇒ 属性面板**整体灰化不可编辑**；单选/空 ⇒ 恢复。
        #   只在整个面板上 `setEnabled`（Qt 父禁用天然覆盖子控件），**不碰 props_panel.py**。
        try:
            multi = int(self.timeline.multi_selection_count())
        except Exception:                                # noqa: BLE001
            multi = 0
        want_on = (multi <= 1)
        if self.props_panel.isEnabled() != want_on:       # 仅在真变时切，避免频繁抖动
            self.props_panel.setEnabled(want_on)
        # ★ 第九轮①：多选时给出**可见原因**（面板顶部选择行 `lbl_sel` 文案 + 悬浮提示），
        #   免得用户误以为是 bug。⚠ 不改进度/标签页外的布局尺寸（避免触发布局重排）。
        if not want_on:
            try:
                self.props_panel.lbl_sel.setText(
                    "已选中 %d 个片段 —— 多选状态下不可编辑" % multi)
                self.props_panel.lbl_sel.setToolTip(
                    "多选（%d 个）时片段属性不可编辑；单击任一单个片段或点空白即可恢复" % multi)
            except Exception:                            # noqa: BLE001
                pass
        # ★ P0-2：选中变化会改变按钮可用性 ⇒ 同步刷新按钮启用态。
        self._refresh_buttons()
        # ★ 需求 8：选中变化要喂给预览（无选中 = 预览只读，行为不变）
        self._feed_selection_to_preview()

    def _on_props_changed(self):
        """★ 需求 A3/#20（【Q7.已裁定】触发面 = **单点**）：属性面板提交后，在**此唯一一处**
        做"变速一致性"收敛 —— `props_panel.commit()` 只写字段，**不保证**
        `end - at == (src_out_eff - src_in) / speed`；本方法在 commit 之后用**改动前快照** `prev`
        判定"用户到底改了哪个字段"，再让 `reconcile_item_timing()` 把三者收敛自洽。

        ⚠ 为什么必须在这里补（用户实测复现"只修了一半"）：新拖入的片段 `src_out_sec=None`
          （默认"到素材末尾"），`props_panel.commit()` 里的"变速重算 end"带
          `it.src_out_sec is not None` 守卫 ⇒ **默认路径被跳过**，0.5× 后 `end` 不变
          （实测 `end=7.330`，应 `14.667`）。这里用 `src_dur`（素材**全长**）兜底，
          无出点场景也能收敛（`end = at + (src_dur - src_in) / speed`）。
        ⚠ **GIF** 传 `keep_src_out_unset=True` ⇒ **绝不写 `src_out_sec`**（循环依赖它保持 `None`）。
        ⚠ 平移片段**不得**被误判成"改了时长"：`reconcile` 内部按 `end - at` 比（见 T01），
          这里只需正确抓 `prev`（那是它的判定依据）。
        """
        p = self.props_panel
        if p.item is None and p.layer is None:
            return
        it = p.item
        # ★ 抓"改动前"快照（**必须在 `commit()` 之前**）：reconcile 靠它判定"用户改了哪个"。
        prev = None
        if it is not None:
            prev = {"at_sec": it.at_sec, "end_sec": it.end_sec,
                    "src_in_sec": it.src_in_sec, "src_out_sec": it.src_out_sec,
                    "speed": it.speed}
        self._push_undo()
        if p.commit():
            # ★ A3/#20：commit 之后**单点**收敛（源范围 ↔ end ↔ speed 自洽）
            if it is not None and prev is not None:
                try:
                    # ★ 幻影抑制（UI 精度护栏）：`sp_len` / `sp_at` 是 **2 位小数**控件，
                    #   `commit()` 会把 `end` 写成 `at + round(dur,2)` ⇒ 即便用户没碰"时长"，
                    #   模型也会看到 **<5ms** 的 end 漂移。若此时 `speed` / 入点 / 出点**都没变**
                    #   （= 用户改的是别的字段，或只是平移），那这漂移**不是用户意图** ⇒ 把 end
                    #   还原成"保时长"的 `at + prev_dur`，免得 `reconcile` 误判成"改了时长"，
                    #   从而给 `src_out=None` 的片段**凭空写 src_out**（污染）。
                    dur_now = (None if it.end_sec is None
                               else float(it.end_sec) - float(it.at_sec))
                    dur_prev = (None if prev["end_sec"] is None
                                else float(prev["end_sec"]) - float(prev["at_sec"]))
                    if (_num_eq(it.speed, prev["speed"])
                            and _num_eq(it.src_in_sec, prev["src_in_sec"])
                            and _num_eq(it.src_out_sec, prev["src_out_sec"])
                            and dur_now is not None and dur_prev is not None
                            and abs(dur_now - dur_prev) <= UI_LEN_EPS):
                        it.end_sec = float(it.at_sec) + dur_prev
                    s = self.session
                    path = (s.media_paths.get(getattr(it, "clip_id", "") or "") or ""
                            if s is not None else "")
                    reconcile_item_timing(
                        it, prev,
                        src_dur=self._clip_source_dur(getattr(it, "clip_id", "")),
                        keep_src_out_unset=probe.is_gif(path))
                    it.normalized()
                except Exception:                      # noqa: BLE001
                    log.exception("变速一致性收敛失败（保持 commit 后的原值）")
            self._mark_dirty()
            self._rebuild_runtime()
            self._sync_timeline_data()
            self.timeline.update()
            self._sync_props_selection()      # 归一化（如 end < at）后回填，B2 守卫生效

    # ==================================================================
    # 视图 / 运行时
    # ==================================================================
    def _bpm(self) -> float:
        tr = self._track()
        return float(getattr(tr.identity, "bpm", 0.0) or 0.0) if tr is not None else 0.0

    def timeline_total_sec(self) -> float:
        """★ 时间轴总长（规格书 §6.9 / C3）。

        **必须**来自音频文件的真实时长（`identity.duration_sec`，无效时 probe 实测音频文件）；
        **绝不能读任何播放缓冲的时长**。没有音频才用"最后一个片段终点 + 5 秒"，下限 10 秒。
        """
        tr = self._track()
        dur = 0.0
        if tr is not None:
            dur = float(getattr(tr.identity, "duration_sec", 0.0) or 0.0)
            if dur <= 1.0:
                s = self.session
                idx = max(0, min(int(getattr(self, "_ti", 0) or 0),
                                 max(0, len(s.sheet.tracks) - 1))) if s is not None else 0
                p = (s.track_audio.get(idx) if s is not None else "") or ""
                if p and os.path.isfile(p):
                    dur = self._audio_duration_sec(p)
        if dur > 1.0:
            return dur
        last = 0.0
        for ly in self._layers():
            for it in ly.items:
                end = it.end_sec if it.end_sec is not None else it.at_sec
                last = max(last, float(end))
        return max(last + 5.0, 10.0)

    @staticmethod
    def _audio_duration_sec(path) -> float:
        """实测**音频文件**时长（秒）。

        ⚠ 为什么不用 `media.probe.probe()`：M1 的 `probe()` 面向**视频素材**（要 `streams.video[0]`），
        对纯音频文件会返回"无视频流"，量不到时长。音频时长用 mutagen（与标签同一个工具）读，
        拿不到再用 PyAV 容器时长兜底。**绝不读任何播放缓冲的时长**（规格书 §6.9 真根因）。
        """
        try:
            import mutagen
            f = mutagen.File(path)
            if f is not None and getattr(f, "info", None) is not None:
                d = float(getattr(f.info, "length", 0.0) or 0.0)
                if d > 0.0:
                    return d
        except Exception:                              # noqa: BLE001
            log.exception("mutagen 读音频时长失败: %s", path)
        try:
            import av
            with av.open(str(path), metadata_errors="replace") as c:
                if c.duration:
                    return max(0.0, float(c.duration) / 1_000_000.0)
        except Exception:                              # noqa: BLE001
            log.exception("PyAV 读音频时长失败: %s", path)
        return 0.0

    def _sync_view(self):
        total = self.timeline_total_sec()
        self.timeline.set_bpm(self._bpm())
        self.timeline.set_view(0.0, max(8.0, total))
        self.sld.setRange(0, int(round(total)))
        self.lbl_total.setText("总长 " + mmss(total))

    def _zoom(self, k):
        # ★ P0-5：工具条缩放与 Ctrl+滚轮**用同一套上下限**（`VIEW_SPAN_MIN/MAX`）。
        #   旧写法只走 `set_view()`（只钳下界），「缩小」可无限放大跨度（实测连点 12 次 → 9383s，
        #   标尺标签挤成一团）。
        span = self.timeline.view_span * float(k)
        span = max(theme.VIEW_SPAN_MIN, min(theme.VIEW_SPAN_MAX, span))
        self.timeline.set_view(self.timeline.view0, span)

    def _sync_timeline_data(self):
        """把当前码表 / 曲目 / 素材映射喂给时间轴控件（时间轴只画，不改模型）。"""
        self.timeline.set_sheet(self._sheet())
        self.timeline.set_track(self._track())
        s = self.session
        self.timeline.set_media_paths(s.media_paths if s is not None else {})
        # ★ 真实彩色波形：把当前曲目的音频文件喂给时间轴（换轨即重算；无音频不触发计算）
        self.timeline.set_audio_path(self._audio_path())
        # ★ 第五轮①：把「自动吸附」开关的当前状态推给时间轴控件（新落地时"有就调用"）
        _snap = getattr(self.timeline, "set_snap", None)
        if callable(_snap):
            try:
                _snap(self._snap_on())
            except Exception:                          # noqa: BLE001
                log.exception("同步自动吸附开关失败")

    def _rebuild_runtime(self):
        s = self.session
        if s is None:
            self.preview.set_runtime(None, 0.0)
            self.preview.set_active(False)
            return
        tr = self._track()
        try:
            rt = CueRuntime(s.sheet, tr, media_paths=s.media_paths)
        except Exception:                              # noqa: BLE001
            log.exception("重建运行时失败")
            rt = None
        self.preview.set_runtime(rt, self._bpm())
        # ★ P0-1：只要"装载了码表 **且** 排了片段"就让预览处于 active —— **不再依赖按过播放**。
        #   `_rebuild_runtime()` 只在用户操作 / 换轨时调用（**不在每帧**），故这里置 active
        #   不会踩热路径；置 active 后 `set_pos`（拖播放头）才能触发合成并出画面。
        self.preview.set_active(self._preview_active_wanted())
        # ★ T07：开关打开时按需补素材音轨波形（worker 去重 + 磁盘缓存 ⇒ 可反复调用，开销≈0）
        if self._clip_audio_on():
            self._ensure_clip_waves()

    def _clip_audio_on(self) -> bool:
        """「显示素材音轨」开关当前是否打开（没有该控件时视为关）。"""
        chk = getattr(self, "chk_clip_audio", None)
        try:
            return bool(chk is not None and chk.isChecked())
        except Exception:                              # noqa: BLE001
            return False

    def _preview_active_wanted(self) -> bool:
        """当前是否"该让预览工作"：装载了码表且排了片段。"""
        tr = self._track()
        return bool(tr is not None and any(getattr(ly, "items", None) for ly in tr.layers))

    def _ratio_invalidate(self):
        """★ 需求 1：把当前码表的**画布比例**喂给预览，并让运行时/合成失效重算。

        · 预览侧读比例的**权威来源**是运行时里的 `sheet.res_w/res_h`（T06 在 `preview.py`
          里按它算画布宽高比），故这里**重建运行时**即可生效；
        · 若预览已提供冻结接口 `set_ratio(w, h)`（T06），就再显式喂一次（**有条件调用**，
          本批次可独立落地）。
        """
        sh = self._sheet()
        w, h = (sh.aspect() if sh is not None else (1920, 1080))
        setter = getattr(self.preview, "set_ratio", None)
        if callable(setter):
            try:
                setter(int(w), int(h))
            except Exception:                          # noqa: BLE001
                log.exception("设置预览比例失败")
        if self.session is not None:
            self._rebuild_runtime()

    # ==================================================================
    # 位置：唯一入口
    # ==================================================================
    def _on_slider(self, val):
        self._set_pos(float(val), from_slider=True)

    def _set_pos(self, sec, from_slider=False, from_audio=False, defer_audio=False):
        """★ 位置变化的**唯一入口**：滑杆 / 播放头 / 自动播放 三处都走这里。

        `from_audio=True` 表示这次位置是**音频走带**报上来的（A5b：位置=已交声卡帧号），
        此时**不要**回头再去 seek 音频（否则自激）。用户主动改位置（滑杆 / ⏮）时
        `from_audio=False`，若正在音频驱动播放，则把音频也 seek 过去（拖动=重定位）。
        `defer_audio=True`（★ 第五轮⑥ 标尺拖动）：视觉立即跟随，但**音频走防抖**（不每帧重启）。
        """
        sec = max(0.0, float(sec))
        self._pos = sec
        if not from_audio:
            self._reset_pos_anchor()          # ★ 用户跳转（滑杆/⏮/点标尺）⇒ 平滑锚点作废、重锚
        if not from_slider:
            self.sld.blockSignals(True)
            self.sld.setValue(int(round(sec)))
            self.sld.blockSignals(False)
        self.lbl_pos.setText("%s（%.1f 秒）" % (mmss(sec), sec))
        self.timeline.set_playhead(sec)
        self.preview.set_pos(sec)
        self._update_status()
        # 音频驱动播放时，用户改了位置 → 音频也跟过去。★ 滑杆拖动走**防抖**：
        # 拖动过程中不每帧重启音频（否则 stop/join/重开一连串，拖动会卡、音频会断续）。
        if self._playing and self._audio_backed and not from_audio:
            self._audio_seek(sec, immediate=not (from_slider or defer_audio))

    def _audio_seek(self, sec, immediate):
        """把音频重定位到 `sec`。`immediate=False`（滑杆拖动）走 100ms 防抖。"""
        if immediate:
            self._pending_seek_sec = None
            self._seek_timer.stop()
            self._do_audio_play(sec)
        else:
            self._pending_seek_sec = float(sec)
            self._seek_timer.start()          # 重置式防抖：每次事件都重置计时

    def _flush_audio_seek(self):
        """防抖计时到 / 滑杆松手 / 拖动结束：把待处理的 seek 落地（只起播一次）。"""
        self._seek_timer.stop()
        sec = self._pending_seek_sec
        self._pending_seek_sec = None
        if sec is None:
            return
        if self._playing and self._audio_backed:
            self._do_audio_play(sec)

    def _do_audio_play(self, sec):
        try:
            self.audio.play(float(sec))
        except Exception:                              # noqa: BLE001
            log.exception("音频重定位失败")

    def _transport_advance(self, dt_sec) -> float:
        """**挂钟**走带推进（无音频时的退化路径）。

        ★ 有音频时**绝不走这里** —— 位置以音频"已交声卡帧号/采样率"为准（A5b）。
        """
        return self._pos + max(0.0, float(dt_sec))

    def _reset_pos_anchor(self):
        """清掉音频位置的**平滑锚点**（起播 / 停播 / 用户跳转时调，见 `_smooth_audio_pos`）。"""
        self._pos_anchor = None
        self._pos_smooth = 0.0
        self._pos_anchor_moved = False

    def _smooth_audio_pos(self) -> float:
        """把"音频主时钟"的**块状阶梯位置**平滑成连续位置（**只**用于预览 / 播放头显示）。

        ★ 为什么必须平滑（用户报的"跳帧 / 不连续"的根因）：
          `AudioPlayer.position_sec()` 的语义是 A5b 的"已交声卡帧号 / 采样率"，而音频是
          **按 `BLOCK_SEC = 0.25 s` 的块**交给声卡的（`audio/player.py`）⇒ 该位置是 **0.25 s 的
          阶梯**。UI 以 30 Hz 采样它 ⇒ "同一帧停 7~8 拍 → 跳 0.25 s"。实测（真实声卡）：
          2.6 s 内位置**只变 10 次 ≈ 3.8 次/秒** —— 画面 30fps 在重绘、内容却只 3.8 次/秒地跳，
          肉眼就是**跳帧 / 不连续**（不是性能问题）。
        ⚠ **绝不改 `position_sec()` 本身**：`tests/test_audio.py` 明确要求它"逐帧精确、
          **绝不用挂钟推算**"，本函数只做**消费侧**平滑，不动音频模块的对外契约。
        平滑规则（保守、单调、不放飞）：
          · 锚点 = 本次读到的 `position_sec()`（**数值仍以音频为准**，绝不改写）；
          · ★ **音频没交过帧就绝不外推**（`_pos_anchor_moved=False` 时直接返回音频原值）——
            这正是 A5b 的底线："音频未交帧时位置不该动"，否则就是"挂钟驱动"（旧实现的病，
            `tests/test_ui_smoke.py::test_playback_driven_by_audio` 专门守这条）；
          · 音频**交过帧之后**，两次音频块之间按**墙钟**外推，但**最多外推一个音频块**
            （`BLOCK_SEC`）—— 块若迟到就冻住等下个锚点，**绝不让画面跑到音频前面超过一个块**；
          · 结果**单调不减**（绝不回跳）；位置**回退**（用户 seek / 循环）时立即重锚并放低基准。
        """
        raw = float(self.audio.position_sec() or 0.0)
        now = time.monotonic()
        a = self._pos_anchor
        if a is None or raw > a[0] + 1e-9:
            if a is not None:
                self._pos_anchor_moved = True      # ★ 音频**真的交过帧**（从此才允许插值）
            a = (raw, now)                        # 首次 / 音频块前进 ⇒ 换锚点
            self._pos_anchor = a
        elif raw < a[0] - 1e-6:
            a = (raw, now)                        # 位置回退（seek / 循环）⇒ 重锚并放低单调基准
            self._pos_anchor = a
            self._pos_smooth = raw
        if not self._pos_anchor_moved:
            return raw                             # ★ A5b 底线：未交帧 ⇒ 位置就是音频原值（不动）
        pos = min(a[0] + max(0.0, now - a[1]), a[0] + AUDIO_BLOCK_SEC)
        if pos > self._pos_smooth:
            self._pos_smooth = pos
        return self._pos_smooth

    # ==================================================================
    # 音频（M4）：路径 / 预载 / 起播
    # ==================================================================
    def _track_index(self) -> int:
        s = self.session
        if s is None or not s.sheet.tracks:
            return 0
        return max(0, min(int(getattr(self, "_ti", 0) or 0), len(s.sheet.tracks) - 1))

    def _audio_path(self) -> str:
        """当前曲目对应的音频文件（没导入过则返回空串）。"""
        s = self.session
        if s is None:
            return ""
        return s.track_audio.get(self._track_index(), "") or ""

    def _preload_audio(self):
        """换歌 / 切码表后**后台预载整首**（不阻塞 GUI，P2）。"""
        path = self._audio_path()
        if path and os.path.isfile(path) and not (self.audio.loaded and self.audio.loaded_path == path):
            self.audio.load(path)                      # 异步；同路径在解码时内部幂等
            if self.audio.loading:
                self._start_audio_poll()
        self._ensure_track_identity()                  # ★ 顺便把"认歌身份"（md5 + 指纹）补上
        self._refresh_play_button()

    # ==================================================================
    # ★ 认歌身份：文件 md5 + 音频指纹（自包含 —— 不依赖 AutoVJ）
    # ==================================================================
    def _ensure_track_identity(self):
        """当前曲目若**已导入音频**且身份（md5/指纹/BPM）**还不全** ⇒ 起后台线程去算。

        触发时机 = 「选曲目 / 导入音频 / 切码表」（都汇流到 `_preload_audio`）。
        只在**缺字段**时扫（已有则不重扫）；同一 `(曲目, 路径)` 正在扫则不重复起线程。
        ⚠ 全程后台，**绝不阻塞 GUI**；结果/失败都写进 `self._id_status` 与日志（**禁止静默失败**）。
        ★ BPM = 0 也算"不全" ⇒ 会自动算一次（识别失败时状态行明说"可以留空"）。
        ⚠ 自动扫描**绝不覆盖**已有的 BPM 手填值（见 `_on_id_done`）。
        """
        s = self.session
        tr = self._track()
        if s is None or tr is None:
            return
        idx = self._track_index()
        path = (s.track_audio.get(idx) or "") or ""
        if not path or not os.path.isfile(path):
            self._id_status = ""
            return
        idt = tr.identity
        if idt.file_md5 and idt.fp_id and float(idt.bpm or 0.0) > 0:   # 已算全 ⇒ 无需重扫
            self._id_status = "md5 + 指纹 + BPM 已就绪"
            return
        self._start_identity_scan(idx, path)

    def _start_identity_scan(self, idx, path, force=False):
        """起一次后台身份扫描（md5 → 指纹 → BPM）。

        `force=True` = 用户**明确要求重算**（点「自动识别」）⇒ 忽略"同一目标正在扫"的幂等
        检查，且回执里允许**覆盖**已有 BPM。
        """
        key = "%d|%s" % (int(idx), os.path.abspath(path))
        if not force and self._id_scan is not None and self._id_scan_key == key:
            return                                      # 同一目标正在扫
        self._id_gen += 1                               # 代号自增 ⇒ 旧回执作废
        self._id_scan_key = key
        self._id_status = "扫描中…（文件 md5 → 音频指纹 → BPM）"
        tr0 = self._track()
        dur0 = float(getattr(tr0.identity, "duration_sec", 0.0) or 0.0) if tr0 is not None else 0.0
        # `dur` 给 VDJ 索引的第三级匹配用（时长几乎唯一，能兜住文件名差异大的情况）
        w = IdentityScanWorker(path, int(idx), self, dur=dur0)   # 父对象=窗口 ⇒ 随窗口销毁
        w.staged.connect(self._on_id_staged)
        w.done.connect(self._on_id_done)
        w.finished.connect(lambda: self._on_id_finished(w))
        self._id_scan = w
        self._log("开始识别这首曲目（文件 md5 + 音频指纹 + BPM，后台进行，不影响操作）…")
        w.start()
        self._refresh_track_panel()

    def _rescan_bpm(self):
        """★「自动识别」按钮：对当前曲目**强制重算 BPM**（允许覆盖手填值）。

        与自动扫描是**两套语义**：
          · 自动扫描（选曲目/导入音频触发）⇒ 只在 `bpm <= 0` 时写，绝不覆盖；
          · 手动点「自动识别」          ⇒ 明确要求重算 ⇒ **覆盖**，并在日志里记"原 X → 新 Y"。
        """
        s = self.session
        tr = self._track()
        if s is None or tr is None:
            return
        idx = self._track_index()
        path = (s.track_audio.get(idx) or "") or ""
        if not path or not os.path.isfile(path):
            self._id_status = "BPM 识别失败：这首曲目还没导入音频文件"
            self._log("⚠ BPM 自动识别：这首曲目还没导入音频文件（先「导入音频…」）。")
            self.track_panel.set_bpm_detecting(False)
            self._refresh_track_panel()
            return
        self._bpm_force = True
        self._bpm_force_old = float(tr.identity.bpm or 0.0)
        self.track_panel.set_bpm_detecting(True)
        self._log("手动重算 BPM（曲目：%s）…"
                  % (tr.identity.title or tr.identity.file_name or ("#%d" % (idx + 1))))
        self._start_identity_scan(idx, path, force=True)

    def _on_id_staged(self, text):
        self._id_status = "扫描中… %s" % text
        self._refresh_track_panel()

    def _on_id_finished(self, w):
        if self._id_scan is w:
            self._id_scan = None
        self.track_panel.set_bpm_detecting(False)       # 兜底：线程结束一定恢复按钮

    def _on_id_done(self, idx, md5, fp_sig, bpm, err, info=None):
        """后台回执（在主线程排队投递）：落进 Identity + 刷新 + 明示成功/失败。

        ★ BPM 的两套语义（别写混）：
          · 自动扫描：**只在 `bpm <= 0` 时写入**，绝不覆盖用户手填值；
          · 手动点「自动识别」（`_bpm_force`）：**覆盖**，并在日志里报"原 X → 新 Y"。
          · 识别失败（`bpm == 0`）**绝不静默** —— 状态行明说"没识别出来（可以留空）"。
        ★ BPM 的**来源要写清**（否则用户没法判断该不该信）：
          · `source == "vdj"`   ⇒ `BPM 150.00（来自 VirtualDJ）` —— 演出端用的就是这个值；
          · `source == "audio"` ⇒ `BPM 145.00（音频分析，建议核对）` —— 实测约 8/12 命中，
            首选分不够高 / 次选很接近时，tooltip 里再列出 `备选：160.00 / 80.75`。
        """
        info = dict(info or {})
        s = self.session
        tr = self._track()
        # 过期回执：换了歌 / 换了码表 ⇒ 直接丢（否则会把 A 的 md5 写进 B）
        if self._id_scan_key != "%d|%s" % (idx, os.path.abspath(self._audio_path() or "")):
            return
        if tr is None or self._track_index() != int(idx):
            return
        force = bool(self._bpm_force)
        self._bpm_force = False
        self.track_panel.set_bpm_detecting(False)
        if err:
            self._id_status = "识别失败：%s" % err
            self._log("⚠ 曲目识别失败：%s（可再点一次该曲目重试）" % err)
            self._refresh_track_panel()
            return
        idt = tr.identity
        if md5:
            idt.file_md5 = md5
        if fp_sig:
            idt.fp_id = fp_sig
        # ---- BPM 落库（两套语义，见 docstring）----
        bpm_val = float(bpm or 0.0)
        old = float(getattr(idt, "bpm", 0.0) or 0.0)
        source = str(info.get("source") or "")
        hint = self._bpm_source_text(bpm_val, source, info)
        status = "已识别：md5 + 指纹 就绪"
        if bpm_val > 0 and (force or old <= 0):
            idt.bpm = round(bpm_val, 2)
            status += "，BPM=%.2f%s" % (idt.bpm, self._bpm_source_tag(source))
            if force and old > 0:
                self._log("BPM 重算完成：原 %.2f → 新 %.2f%s"
                          % (old, idt.bpm, self._bpm_source_tag(source)))
            else:
                self._log("已自动识别 BPM：%.2f%s"
                          % (idt.bpm, self._bpm_source_tag(source)))
        elif bpm_val > 0:
            status += "，BPM 保留手填的 %.2f" % old
            self._log("BPM 已是手填值 %.2f：自动识别结果 %.2f%s 未覆盖"
                      "（要重算请点「自动识别」）。"
                      % (old, bpm_val, self._bpm_source_tag(source)))
        else:
            # ★ 中性口径：不填 BPM 也能导出/演出 ⇒ 不是"必须手动填"的错误
            status += "；BPM 没识别出来（可以留空，不影响演出）"
            hint = ""
            self._log("BPM 没识别出来（可以留空，不影响演出；只影响按小节对齐）：%s。"
                      % (idt.title or idt.file_name or ("#%d" % (int(idx) + 1))))
        self._mark_dirty()                              # 身份是码表的一部分（会随 .ercue 保存）
        self._id_status = status
        self.track_panel.set_bpm_hint(hint)
        self._refresh_track_panel()
        self._log("曲目识别完成：md5=%s…  指纹=%s" % ((md5 or "")[:12], (fp_sig or "（无）")))

    @staticmethod
    def _bpm_source_tag(source) -> str:
        """状态行里的来源短标签（`"（来自 VirtualDJ）"` / `"（音频分析，建议核对）"` / `""`）。"""
        if source == "vdj":
            return "（来自 VirtualDJ）"
        if source == "audio":
            return "（音频分析，建议核对）"
        return ""

    def _bpm_source_text(self, bpm_val, source, info) -> str:
        """BPM 输入框 tooltip 里的多行来源说明（含**备选候选**）。

        什么时候列备选（让我们用 `pick_bpm` 自己的裁决边际，而不是拍脑袋的绝对阈值）：
          · 次选分数 ≥ 首选的 70%（`pick_bpm` 就是按 0.70 判"够不够格"的）⇒ 存在半速/双速歧义；
          · 或首选分数本身很低（<1.2）⇒ 峰值很不明显。
        """
        if bpm_val <= 0:
            return ""
        if source == "vdj":
            return "BPM %.2f（来自 VirtualDJ —— 演出端用的就是这个值，最可信）\n" \
                   "（想以音频分析的结果覆盖它，点「自动识别」也可以，但通常没必要）" % bpm_val
        if source != "audio":
            return ""
        line = ("BPM %.2f（音频分析，<b>建议核对</b>：实测约 2/3 命中，可能偏 1~6 BPM；"
                "填错也不影响演出，只影响按小节对齐）" % bpm_val)
        peaks = info.get("peaks") or []
        score = float(info.get("score") or 0.0)
        ambiguous = False
        if len(peaks) >= 2:
            try:
                ambiguous = float(peaks[1][1]) >= 0.70 * float(peaks[0][1])
            except (TypeError, ValueError, IndexError):
                ambiguous = False
        if score and score < 1.2:
            ambiguous = True
        if ambiguous and peaks:
            alt = ["%.2f" % float(b) for b, _s in peaks[:3]]
            line += "\n备选：%s（半速 / 双速歧义，命中不了就手动改）" % " / ".join(alt)
        return line

    def _stop_identity_scan(self, wait_ms=0):
        """丢弃当前扫描（换歌/关窗时调）。

        ⚠ 计算线程**不强杀**：换歌时 `wait_ms=0` ⇒ 只 `requestInterruption()` + 断开回执 +
        丢掉引用（QThread 的父对象是窗口，生命周期有保障），**绝不在切歌时阻塞 GUI**；
        关窗时才给一个有界 `wait`，让线程有机会收尾。结果另有"代号"兜底（`_id_gen` / key）。
        """
        w = self._id_scan
        self._id_scan = None
        self._id_scan_key = ""
        self._id_gen += 1
        self._bpm_force = False                         # 丢弃在途的"手动重算 BPM"语义
        try:
            self.track_panel.set_bpm_detecting(False)   # 按钮回到可点状态（否则会一直"识别中…"）
        except Exception:                               # noqa: BLE001
            pass
        if w is not None:
            try:
                w.requestInterruption()
            except Exception:                           # noqa: BLE001
                pass
            for sig in (w.staged, w.done, w.finished):
                try:
                    sig.disconnect()
                except Exception:                       # noqa: BLE001
                    pass
            if wait_ms:
                try:
                    w.wait(int(wait_ms))
                except Exception:                       # noqa: BLE001
                    pass

    def _start_audio_poll(self):
        """解码中就轮询状态，用于恢复 ▶ 按钮 / 记录日志（不阻塞）。"""
        if not self._audio_poll.isActive():
            self._audio_poll.start()

    def _poll_audio_ready(self):
        if self.audio.loading:
            return
        self._audio_poll.stop()
        if self.audio.loaded:
            self._log("音频已就绪：%s（%.1f 秒）"
                      % (os.path.basename(self.audio.loaded_path or ""), self.audio.duration_sec))
        elif self.audio.loaded_path or self.audio.error:
            self._log("音频解码失败，将用挂钟驱动：%s" % (self.audio.error or "未知原因"))
        self._refresh_play_button()

    def _refresh_play_button(self):
        """▶ 按钮状态：播放中=⏸；解码中=准备中(置灰)；否则=▶。"""
        if self._playing:
            self.btn_play.setText("⏸")
            self.btn_play.setEnabled(True)
        elif self.audio.loading:
            self.btn_play.setText("准备中…")
            self.btn_play.setEnabled(False)
        else:
            self.btn_play.setText("▶")
            self.btn_play.setEnabled(True)

    def _start_audio_at(self, sec) -> bool:
        """从 `sec` 起播音频。**返回是否成功**（失败即退回挂钟，绝不抛到 UI）。"""
        path = self._audio_path()
        if not path or not os.path.isfile(path):
            return False
        if not self.audio.loaded:
            self.audio.load(path)                      # 异步发起（本次不播，就绪后再按 ▶）
            if self.audio.loading:
                self._start_audio_poll()
                self._refresh_play_button()
            return False
        if not self.audio.available:
            return False
        try:
            return bool(self.audio.play(sec))
        except Exception:                              # noqa: BLE001
            log.exception("音频起播失败")
            return False

    def toggle_play(self):
        if self._playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self):
        # ★ 音频还没就绪（后台解码中）：置灰 ▶、显示"准备中"，**本次不播、绝不阻塞 GUI**
        path = self._audio_path()
        if path and os.path.isfile(path) and not self.audio.loaded:
            self._start_audio_at(self._pos)            # 发起异步加载
            if self.audio.loading:
                self._refresh_play_button()
                self._log("音频正在后台解码，就绪后再按 ▶")
                return
        self._playing = True
        self._play_last = time.monotonic()
        self._reset_pos_anchor()                   # ★ 起播 ⇒ 平滑锚点从头开始
        self.preview.set_playing(True)
        self.preview.set_active(True)
        # ★ 优先音频驱动；没有声卡 / 没导入音频 / 打开失败 → 自动退回挂钟（A5b 退化路径）
        self._audio_backed = self._start_audio_at(self._pos)
        self._play_timer.start()
        self._refresh_play_button()
        if self._audio_backed:
            self._log("开始播放（音频驱动：位置=已交声卡帧号/采样率）")
        else:
            self._log("开始播放（挂钟驱动：该曲目没导入音频或本机无可用音频输出）")

    def _stop_play(self):
        self._playing = False
        self._play_timer.stop()
        self._seek_timer.stop()                        # 丢掉未落地的滑杆 seek
        self._pending_seek_sec = None
        self._reset_pos_anchor()                       # ★ 停播 ⇒ 清平滑锚点
        self.preview.set_playing(False)
        try:
            self.audio.pause()                         # 暂停（保留位置），stop() 内部 ≤0.8s 返回
        except Exception:                              # noqa: BLE001
            log.exception("暂停音频失败")
        self._refresh_play_button()

    def _on_loop_toggled(self, on):
        self._loop = bool(on)

    def _play_tick(self):
        if not self._playing:
            return
        total = self.timeline_total_sec()
        if self._audio_backed:
            if self.audio.is_playing():
                # ★ 位置=已交声卡帧号/采样率（A5b）；但它以 0.25s 音频块为步长 ⇒ 走
                #   `_smooth_audio_pos()` 做**消费侧**平滑，否则预览会"跳帧"（见该函数注释）。
                pos = self._smooth_audio_pos()
            else:
                # 音频放完 / 被停 → **画面停在音频实际结束处**，绝不用挂钟继续往前跑
                pos = self.audio.position_sec()
                if self._loop:
                    self._set_pos(0.0)                 # 循环：把音频也重起来（规格书 §6.18）
                    return
                self._stop_play()
                self._set_pos(pos, from_audio=True)
                return
        else:
            now = time.monotonic()
            dt = now - self._play_last
            self._play_last = now
            pos = self._transport_advance(dt)
        if pos >= total:
            if self._loop:
                self._set_pos(0.0)                     # 循环：重定位（音频也会重起）
                return
            self._stop_play()
            self._set_pos(total)
            return
        self._set_pos(pos, from_audio=self._audio_backed)

    # ==================================================================
    # 撤销 / 重做（架构方案 §3.5）
    # ==================================================================
    def _push_undo(self):
        s = self.session
        if s is None:
            return
        s.undo.append(s.snapshot(self._ti))     # ★ B3：快照带上当前曲目下标
        if len(s.undo) > UNDO_CAP:
            s.undo.pop(0)
        s.redo.clear()

    def _push_history_clean(self):
        s = self.session
        if s is not None:
            s.undo.clear()
            s.redo.clear()

    def undo(self):
        s = self.session
        if s is None or not s.undo:
            self._log("没有可撤销的操作。")
            return
        s.redo.append(s.snapshot(self._ti))
        snap = s.undo.pop()
        self._restore_snapshot(s, snap)
        self._log("已撤销。")

    def redo(self):
        s = self.session
        if s is None or not s.redo:
            self._log("没有可重做的操作。")
            return
        s.undo.append(s.snapshot(self._ti))
        snap = s.redo.pop()
        self._restore_snapshot(s, snap)
        self._log("已重做。")

    def _restore_snapshot(self, sess, snap):
        sess.load_snapshot(snap)
        sess.dirty = True
        self.timeline.set_selected("", "")
        self.props_panel.item = None
        self.props_panel.layer = None
        # ★ B3：还原"当前曲目下标"（不再无条件归零），并 clamp 到有效范围（曲目可能被删）
        n = len(sess.sheet.tracks)
        self._ti = max(0, min(int(getattr(sess, "ti", 0) or 0), n - 1)) if n else 0
        self._refresh_all()

    # ==================================================================
    # 刷新（B1：所有重填都 blockSignals）
    # ==================================================================
    def _sheet_label(self, sess: SheetSession) -> str:
        sh = sess.sheet
        present = sum(1 for m in sh.media
                      if os.path.isfile(sess.media_paths.get(m.clip_id) or ""))
        miss = len(sh.media) - present
        return "%s（%d 首 / 缺 %d 个素材）%s" % (
            sh.title or sess.key, len(sh.tracks), miss, "（未导出）" if sess.dirty else "")

    def _refresh_all(self):
        self._refresh_sheet_combo()
        self._refresh_pkg_list()
        self._refresh_track_panel()
        self._refresh_media_panel()
        self._sync_timeline_data()
        self._sync_view()
        self._rebuild_runtime()
        self._sync_props_selection()
        self._refresh_buttons()
        self._refresh_play_button()
        self._refresh_preview_label()
        self._update_status()

    def _preview_label(self) -> str:
        """★ T02（#11）：预览区标签文案 —— 带**当前码表名**。"""
        sh = self._sheet()
        name = str(getattr(sh, "title", "") or "") if sh is not None else ""
        return ("码表预览 —— %s" % name) if name else "码表预览"

    def _refresh_preview_label(self):
        lab = getattr(self, "lbl_preview", None)
        if lab is None:
            return
        try:
            lab.setText(self._preview_label())
        except Exception:                              # noqa: BLE001
            log.exception("更新预览标签失败")

    def _refresh_sheet_combo(self):
        """B1：定时/频繁重填下拉框必须 blockSignals + findData 还原选中项。"""
        self.cb_sheet.blockSignals(True)
        try:
            self.cb_sheet.clear()
            for key, sess in self.sessions.items():
                self.cb_sheet.addItem(self._sheet_label(sess), key)
            j = self.cb_sheet.findData(self.current_key)
            self.cb_sheet.setCurrentIndex(j if j >= 0 else 0)
        finally:
            self.cb_sheet.blockSignals(False)

    def _refresh_pkg_list(self):
        rows = [(k, self._sheet_label(s), s.dirty) for k, s in self.sessions.items()]
        self.pkg_panel.refresh(rows, self.current_key)

    def _refresh_track_panel(self):
        sh = self._sheet()
        labels = []
        if sh is not None:
            for i, tr in enumerate(sh.tracks):
                idt = tr.identity
                nm = idt.title or idt.file_name or ("曲目 %d" % (i + 1))
                if idt.artist:
                    nm = "%s — %s" % (idt.artist, nm)
                if idt.duration_sec:
                    nm += "    %d:%02d" % (int(idt.duration_sec) // 60, int(idt.duration_sec) % 60)
                labels.append(nm)
        self.track_panel.refresh_tracks(labels, int(getattr(self, "_ti", 0) or 0))
        # ★ 身份（md5 / 指纹）与"扫描状态"一并回填（状态由 `_ensure_track_identity` 维护，**绝不静默**）
        tr = self._track()
        self.track_panel.fill_identity(
            tr.identity if tr is not None else None, id_status=self._id_status)

    def _refresh_media_panel(self):
        sh = self._sheet()
        s = self.session
        cache_dir = self._thumb_dir if self.thumbs_enabled else None
        self.media_panel.fill(sh, s.media_paths if s is not None else {}, cache_dir)

    def _refresh_buttons(self):
        """★ P0-2：按当前状态统管**动作按钮**的启用态（不再只管 undo/redo）。

        规则（无一例外）：
          · ＋ 层     —— 需要有曲目。
          · 删除层    —— 需要**显式选中**某一层，且该曲目不止一层（P0-3：绝不静默删别的层）。
          · 放大/缩小 —— 需要有曲目（时间轴才有内容可缩放）。
          · 导出      —— 需要装载了码表（需求 6）。
          · 撤销/重做 —— 需要对应栈非空。
        禁用态意味着点击**不触发任何副作用**（Qt 不会给禁用的按钮发 clicked）。
        ★ 需求 5：「＋ cue / 删除 cue」两按钮已移除 ⇒ 不再有它们的启用态。
          （加片段走素材双击/拖拽；删片段走 **Delete 键 / 右键菜单 → `del_cue()`**）
        """
        s = self.session
        layers = self._layers()
        has_track = self._track() is not None
        exp_ly = self._explicit_selected_layer()
        self.btn_undo.setEnabled(bool(s and s.undo))
        self.btn_redo.setEnabled(bool(s and s.redo))
        self.btn_export.setEnabled(s is not None and self._sheet() is not None)
        self.btn_add_layer.setEnabled(has_track)
        self.btn_del_layer.setEnabled(exp_ly is not None and len(layers) > 1)
        # ★ T02（#7）：已无「放大/缩小」按钮 ⇒ 不再置其启用态（缩放走 Ctrl+滚轮）

    def _update_status(self):
        sh = self._sheet()
        tr = self._track()
        s = self.session
        n_sheets = len(self.sessions)
        cur = (sh.title if sh is not None else "-")
        tr_name = (tr.identity.title if tr is not None else "未选")
        n_layers = len(self._layers())
        suffix = ""
        if self.last_import_warnings:
            suffix = "   ⚠ 兼容提示 %d 条（见日志）" % len(self.last_import_warnings)
        self.lbl_state.setText(
            "码表：%s（共 %d 张）   曲目：%s   层数：%d   位置：%s%s"
            % (cur, n_sheets, tr_name, n_layers, mmss(self._pos), suffix))

    def _mark_dirty(self):
        s = self.session
        if s is not None:
            s.dirty = True
        self._schedule_autosave()                      # ★ T02：唯一挂钩点（所有改动都会走到这）

    # ==================================================================
    # ★ T02（#5）自动保存 —— 独立目录、防抖 + 安全网 + 关键点强制 flush
    #   红线：**绝不写用户的 `.ercue`**（只写 `session_store` 的 autosave 目录）
    # ==================================================================
    def _schedule_autosave(self):
        """把一个 session 标记为"待落盘"并**重启 1.5s 防抖**（编辑期间不落盘，停顿才落）。"""
        if not self._autosave_enabled:
            return
        s = self.session
        if s is None:
            return
        self._autosave_pending.add(s.key)
        try:
            self._autosave_timer.start()
        except Exception:                              # noqa: BLE001
            log.exception("启动自动保存定时器失败")

    def _autosave_touch(self, *keys):
        """★ T02：把这些 key 标记为"待落盘"并**立刻 flush**（关键点用，不等 1.5s 防抖）。

        用于"新建 / 导入 / 切曲目"等**必须马上进工作区**的时刻 —— `_flush_autosave()` 只写
        `_autosave_pending` 里的 session，故这里先把 key 塞进 pending 再 flush。
        **绝不抛**（`_flush_autosave` 自身吞异常）。`_autosave_enabled=False` ⇒ no-op。
        """
        if not self._autosave_enabled:
            return
        for k in keys:
            k = str(k or "")
            if k:
                self._autosave_pending.add(k)
        self._flush_autosave()

    def _flush_autosave(self):
        """把 pending 的 session 快照 + workspace 索引**立刻**落盘（关键点 / 定时器 / 关窗都调）。

        **绝不抛**：任何失败只记一条日志（自动保存失败不该打断用户编辑）。
        """
        if not self._autosave_enabled:
            return
        try:
            self._autosave_timer.stop()
        except Exception:                              # noqa: BLE001
            pass
        pending = sorted(self._autosave_pending)
        self._autosave_pending.clear()
        ok = True
        for key in pending:
            s = self.sessions.get(key)
            if s is None:
                continue
            try:
                # ★ 当前码表要把"当前曲目下标"一并存进快照（切曲目后重开能回到那一首）
                ti = self._ti if key == self.current_key else None
                if not session_store.write_session(key, s.snapshot(ti)):
                    ok = False
            except Exception:                          # noqa: BLE001
                log.exception("自动保存快照失败：%s", key)
                ok = False
        try:
            # ⚠ 只有"确实有码表"时才写索引 —— 否则 `MainWindow(restore_workspace=False,…)` 的
            #   空工作区会把用户已有的 workspace.json 覆盖成空（测试/异常路径都不该有这种副作用）。
            if self.sessions:
                if not session_store.write_workspace(self._autosave_index()):
                    ok = False
        except Exception:                              # noqa: BLE001
            log.exception("自动保存索引失败")
            ok = False
        if pending and not ok:
            self._log("⚠ 自动保存未能落盘（不影响继续编辑；导出仍照常）")

    def _autosave_index(self) -> dict:
        """workspace 索引（有哪些码表 / 当前哪张 / 顺序 / 标题 / 时间）。"""
        return {
            "version": session_store.WORKSPACE_VERSION,
            "current": str(self.current_key or ""),
            "order": [str(k) for k in self.sessions.keys()],
            "titles": {str(k): (s.sheet.title or k) for k, s in self.sessions.items()},
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _restore_workspace(self) -> bool:
        """启动时从 autosave 目录恢复上次工作区（多码表）。

        返回是否真的恢复了内容。**失败/为空一律返回 False**（调用方回落 `new_sheet`），
        **绝不抛** —— 自动保存坏了也绝不能让人打不开软件。
        """
        try:
            if not session_store.available():
                return False
            idx = session_store.read_workspace()
            if not isinstance(idx, dict):
                return False
            order = idx.get("order")
            if not isinstance(order, list) or not order:
                order = list((idx.get("titles") or {}).keys())
            n = 0
            for key in order:
                key = str(key or "")
                if not key:
                    continue
                snap = session_store.read_session(key)
                if not snap:
                    continue
                try:
                    sess = SheetSession(key, CueSheet())
                    sess.load_snapshot(snap)
                except Exception:                      # noqa: BLE001
                    log.exception("恢复码表失败（跳过）：%s", key)
                    continue
                self.sessions[key] = sess
                n += 1
            if n <= 0:
                return False
            cur = str(idx.get("current") or "")
            self.current_key = cur if cur in self.sessions else list(self.sessions.keys())[0]
            # 推进 key 序号，避免与恢复出来的 key 相撞
            used = [int(k[5:]) for k in self.sessions
                    if k.startswith("sheet") and k[5:].isdigit()]
            self._key_seq = max([self._key_seq] + used)
            self._ti = 0
            self.last_import_warnings = []
            self._push_history_clean()
            self._refresh_all()
            self._set_pos(0.0)
            self._ratio_invalidate()
            self._log("已从自动保存恢复 %d 张码表（目录：%s）"
                      % (n, session_store.base_dir()))
            return True
        except Exception:                              # noqa: BLE001
            log.exception("恢复工作区失败（将新建空码表）")
            return False

    # ==================================================================
    # 日志
    # ==================================================================
    def _nudge_row_h(self, factor: float):
        """轨道行高 −/+（`Alt+滚轮` 的**显式等价入口**）。

        ⚠ 第十一轮：用户实测"Alt+滚轮始终无效"（怀疑 Alt 被菜单栏/输入法/驱动吞掉，
        且日志无文件输出无法诊断）⇒ 提供按钮，不再依赖键盘修饰键。
        """
        tl = self.timeline
        tl.row_h = max(theme.ROW_H_MIN, min(theme.ROW_H_MAX, float(tl.row_h) * factor))
        tl._set_voff(tl.v_off)
        tl.update()
        self._log("轨道行高：%.0f px" % tl.row_h)

    def _log(self, msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        self.txt.appendPlainText(line)

    # ==================================================================
    # ★ T02（Q5 裁定）：**非阻断**的"未导出提醒" —— 取代旧「未保存保护」三选弹窗
    # ==================================================================

    def _warn_unsaved(self, sess_list, action_label="退出", note="") -> bool:
        """★ T02（Q5 裁定）：**非阻断**的"有改动未导出"提醒。返回 True = 允许继续（跳过）。

        · **先 flush 自动保存**（R7：必须"先 flush 再提醒"，否则崩溃窗口期可能丢最后一次改动）；
        · 有改动**且未导出** ⇒ 一句提示，**默认按钮 =「跳过并{action_label}」**；「取消」= 不继续；
        · 无改动 / `prompt_export_on_close=False` / 非交互 ⇒ **静默放行**。
        ⇒ 因 autosave 兜底（**关窗**场景改动不会丢），不再强制"保存/丢弃/取消"三选。
        """
        self._flush_autosave()
        if not self._interactive or QApplication.instance() is None:
            return True
        if not bool(self.cfg.get("prompt_export_on_close", True)):
            return True
        dirty = [s for s in (sess_list or []) if s is not None and s.dirty]
        if not dirty:
            return True
        try:
            from PySide6.QtWidgets import QMessageBox
        except Exception:                              # noqa: BLE001
            return True
        names = "、".join((s.sheet.title or s.key) for s in dirty[:8])
        if len(dirty) > 8:
            names += "…"
        box = QMessageBox(self)
        box.setWindowTitle("有改动还没导出")
        box.setIcon(QMessageBox.Information)
        box.setText("这些码表自上次导出后有改动：\n%s" % names)
        box.setInformativeText(note or "改动已**自动保存**，下次打开会自动恢复，不会丢；"
                                       "若想现在就导出，点「取消」再导出。")
        b_skip = box.addButton("跳过并%s" % action_label, QMessageBox.AcceptRole)
        b_cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(b_skip)
        box.setEscapeButton(b_cancel)
        box.exec()
        return box.clickedButton() is not b_cancel

    def _confirm_quit(self) -> bool:
        """★ T02（Q5 裁定）：关窗 = **非阻断**提醒（取代原"保存/丢弃/取消"三选，见 `_warn_unsaved`）。"""
        if not self._warn_unsaved(list(self.sessions.values()), "退出"):
            self._log("已取消退出（可继续导出）。")
            return False
        return True

    def closeEvent(self, e):
        if not self._confirm_quit():                   # ★ B1：取消时 e.ignore()，窗口不关
            e.ignore()
            return
        self.shutdown()
        super().closeEvent(e)

    def shutdown(self):
        """关闭时释放解码器与后台线程、音频（A4/A6/P2）。"""
        # ★ T02（R7）：**先 flush 自动保存**（要在停任何东西之前），保证最后一次改动落盘。
        try:
            self._flush_autosave()
            if getattr(self, "_autosave_timer", None) is not None:
                self._autosave_timer.stop()
            if getattr(self, "_autosave_safety", None) is not None:
                self._autosave_safety.stop()
        except Exception:                              # noqa: BLE001
            log.exception("关闭前自动保存失败")
        try:
            self._seek_timer.stop()
            self._audio_poll.stop()
        except Exception:                              # noqa: BLE001
            log.exception("停止音频定时器失败")
        try:
            self._stop_play()
        except Exception:                              # noqa: BLE001
            log.exception("停止播放失败")
        try:
            self.audio.close()                         # ★ 音频：停线程、丢缓冲、不留线程
        except Exception:                              # noqa: BLE001
            log.exception("关闭音频失败")
        try:
            self.preview.shutdown()
        except Exception:                              # noqa: BLE001
            log.exception("关闭预览失败")
        try:
            self.media_panel.stop_worker()
        except Exception:                              # noqa: BLE001
            log.exception("停止缩略图线程失败")
        try:
            self.timeline.stop_waveform()               # ★ 停后台波形线程（不阻塞退出）
        except Exception:                              # noqa: BLE001
            log.exception("停止波形线程失败")
        try:
            self._stop_identity_scan(wait_ms=2000)      # ★ 停身份扫描线程（有界等待，不挂死退出）
        except Exception:                              # noqa: BLE001
            log.exception("停止身份扫描线程失败")
        try:
            identity_scan.clear_hash_cache()           # ★ C3：关窗释放内存（~3MB/首）
        except Exception:                              # noqa: BLE001
            log.exception("清空指纹哈希缓存失败")
        try:
            if self._wave_worker is not None:           # ★ T07：停素材波形线程（有界等待）
                self._wave_worker.stop()
                self._wave_worker = None
        except Exception:                              # noqa: BLE001
            log.exception("停止素材波形线程失败")
