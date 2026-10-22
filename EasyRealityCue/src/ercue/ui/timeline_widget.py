# -*- coding: utf-8 -*-
"""★ 自绘时间轴控件 `CueTimeline`（本项目最难的一块）。

横轴 = **秒**（标尺显示 `mm:ss`），纵轴 = 层（上面的层 z 更大、优先显示），
底部叠加一条音频轨。片段画成色块，可点选 / 拖动 / 拉边 / 跨层 / 拖放素材。

═══ 逐条落实的纪律（架构方案 §4 / 规格书 §5）═══
  · 单位 = 秒：控件内部一切坐标都是秒；`units.py` 是拍↔秒唯一入口，
    本文件**不出现任何 beat_len 乘除**。
    （拍/小节/乐句的**标尺刻度**只在 `_paint_ruler()` 里经 units 换算。放素材/拖动/拉边
    一律按**真实坐标**落点；**吸附是可选项** —— 第五轮 #14 起加「自动吸附」开关（默认关），
    开启后**只吸正在拖的那一个**，见模块级纯函数 `snap_at()`。）
  · B3 禁止裸 except 静默吞异常：所有 except 至少 `log.exception`。
  · B4 `@staticmethod` 里绝不用 `self`（`_clip_from_event` 的旧血案，见规格书 §6.1）。
  · B5 拖放 mime：`_ClipList` 在**视图 startDrag() 里自己造 QDrag**（在 panels.py），
    本控件 `_clip_from_event` 做**两级兜底**（mime → 拖拽源控件 currentItem）。
  · B6 素材 id 与路径分开存 role（本控件只从 mime 读 clip_id）。
  · C5 标尺自适应阈值集中在模块级纯函数 `ruler_plan()` 里（数字 ≥34px、竖线 ≥2px、
    ●≥7px、◆≥7px、拍刻度 ≥6px），便于自测。
  · 松手落点用**最后鼠标位置**（不是事件位置）—— 跨层拖动时事件位置可能不在 item 上。
  · 音频轨画**真实彩色波形**（数据来自零 Qt 的 `ercue.audio.waveform`）：本控件只负责
    「后台线程里算一次 → 主线程里按当前视口重采样着画」。**绝不占用 GUI 线程算波形**，
    也**绝不自己实现 DSP**（数据层已算好：每列的幅度 + RGB 三频段配色）。

═══ ★ T04（需求 3 / 11 / 12）在本文件新增的东西，全部在**交互层 / 独立绘制路径**里 ═══
  · 需求 3 拖拽剪辑：双击进**剪辑态**（`_edit_mode`）、拖左右把手 = trim 开头/末尾（`_trim`
    本地预览 → 松手发 `trimmed`）；trim 公式是模块级**纯函数** `compute_trim()`。
  · 需求 12 右键菜单：`contextMenuEvent` 命中片段 ⇒ 发 `context_menu`（菜单由 window 构建）。
  · 需求 11 素材音轨：`set_show_clip_audio` / `set_clip_waves` + **独立**的
    `_paint_clip_audio()`（画在片段块内）。⚠ 歌曲轨的 `_paint_waveform` / `_paint_ruler` /
    `wave_sample` **一行不改**（用户明确要求"只改交互层"）。

═══ ★ 24 条 T03（#6/#9/#10/#13/#14/#18）—— 同样只在**交互层 / 独立新绘制**里加 ═══
  · #6  `seek_requested`：左键点**标尺** ⇒ 发秒（**不进入平移**，修掉"点标尺误平移"）。
  · #9  光标：`_update_cursor()` —— 剪辑态命中左右把手 ⇒ `SizeHorCursor`（进入 trim 拖动亦然）。
  · #10 点空白 ⇒ `picked("","")` 取消选择（★ 再制态下**不**取消，见 R6）。
  · #13 空白右键 ⇒ `context_menu("","",gpos)`（供 window 弹"粘贴"）+ `sec_at_global()`。
  · #14 自动吸附：模块级纯函数 `snap_at()` + `set_snap()` / `snap_enabled()` / `_snap_targets()`；
        **只吸被拖的那个**（绝不写别的片段）+ `_paint_snap_guide()` 竖导引线。默认关。
  · #18 再制状态机（★ 第六轮改成 CDR 式，见下）：`_dup_mode` + `duplicate_requested`。
  · ★ 第六轮 #18 · **CDR 式再制**（用户实测语义，权威）：
      左键按在片段上 → 拖动 → **按右键** ⇒ 进入「复制模式」（原件**锁定在按下位置**、
      光标 `DragCopyCursor`、跟手的是"**副本预演**"）；松开右键（无动作）→ **松开左键** ⇒
      **提交**：原件留在按下位置 + 副本落在"松开左键的位置"，**此刻才发** `duplicate_requested`；
      提交后**自动退出**复制模式（不必再按一次右键）。
      中途取消：`Esc` 或再按一次右键 ⇒ 取消复制（不发副本、原件回原位）。
      保留旧语义：**没有"拖动中"的普通右键**（片段已选中、未在拖动）⇒ 仍**立即复制一份**（一次性）。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from ercue.model import units as _units
from . import theme

log = logging.getLogger("ercue.ui.timeline")

# 私有拖放 mime（「3 素材」列表 → 时间轴某层）
CLIP_MIME = "application/x-ercue-clip"

# ★ 2026-10-11（用户反馈「波形在时间轴的占比加大一点」）：
#   波形半高 = `min(half, amp × half × _WAVE_GAIN)`。
#   `1.0` = 原始映射（amp 均值约 0.49 ⇒ 平均只到半高，观感偏小）；`>1` 更饱满。
#   超出 half 的部分**钳到顶**（等于峰值削平）—— 观感更像"满格的波形条"。
_WAVE_GAIN = 1.45


def mmss(sec) -> str:
    """秒 → `mm:ss`（超过 1 小时显示 `h:mm:ss`）。负值按 0。"""
    s = max(0, int(round(float(_units.to_float(sec)))))
    if s >= 3600:
        return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)
    return "%d:%02d" % (s // 60, s % 60)


def ruler_plan(pxs: float):
    """★ 标尺自适应（**纯函数**，规格书 §5.5 / 架构方案 §4-C5）。

    `pxs` = 每秒像素 = `(W-GUTTER)/span_sec`。

    返回 `(step_sec, line_sec, show_label, show_bar_mark, show_phrase_mark, show_beat_tick)`：
      · `step_sec`   数字标签的数据步长（秒）：从 `(1,2,5,10,15,30,60,120,300,600)`
                     取第一个满足 `cand × pxs ≥ 34` 的（`mm:ss` 排得下）。
      · `line_sec`   长竖线每几秒一条：取第一个满足 `cand × pxs ≥ 2.0` 的（否则整屏条纹）。
      · `show_label`      = `step_sec × pxs ≥ 34`
      · `show_bar_mark`   = `pxs × 2.0 ≥ 7`
      · `show_phrase_mark`= `pxs × 4.0 ≥ 7`
      · `show_beat_tick`  = `pxs ≥ 6`

    缩小时逐级抽稀，**放不下就不画**（用户当年明确要求"缩小时上面的排数不要全部显示"）。
    """
    try:
        pxs = float(pxs)
    except (TypeError, ValueError):
        pxs = 0.0
    step_sec = 1
    for cand in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
        step_sec = cand
        if cand * pxs >= theme.RULER_LABEL_MIN_PX:
            break
    line_sec = 1
    for cand in (1, 2, 5, 10, 15, 30, 60, 300, 600):
        line_sec = cand
        if cand * pxs >= theme.RULER_LINE_MIN_PX:
            break
    return (step_sec, line_sec,
            step_sec * pxs >= theme.RULER_LABEL_MIN_PX,
            pxs * 2.0 >= theme.RULER_BAR_MIN_PX,
            pxs * 4.0 >= theme.RULER_PHRASE_MIN_PX,
            pxs >= theme.RULER_BEAT_MIN_PX)


# ★ 需求 3（拖拽剪辑）：trim 把手的视觉宽度（像素）。命中仍复用 `theme.EDGE_TOL`。
TRIM_HANDLE_W = 6.0

# ★ #14（自动吸附）：吸附像素阈值（默认关；只吸附"正在拖的那一个"）。
SNAP_TOL_PX = 8.0

# ★ 第九轮（框选多选）：空白处左键"立即移动=框选 / 长按后移动=平移"的判定阈值。
#   `BOX_PAN_MS` = 长按毫秒阈值（≥ 它才开始平移）；`BOX_START_PX` = 认定"开始移动"的像素死区。
BOX_PAN_MS = 250.0
BOX_START_PX = 3.0
# 橡皮筋框定的最小尺寸（像素）——小于它视作"误触/未移动"，不产生多选。
BOX_MIN_PX = 4.0


def snap_at(at, dur, targets, px_per_sec, tol_px=SNAP_TOL_PX):
    """★ #14 自动吸附（**纯函数**）：把被拖片段的**左端或右端**吸到最近的 target。

    参数
    ----
    at          : 拟落左端（秒）
    dur         : 片段时长（秒）
    targets     : 候选吸附点（秒，已排序；一般含 0 / 播放头 / 其它片段的 at 与 end）
    px_per_sec  : 每秒像素（把 `tol_px` 像素阈值换算成秒）
    tol_px      : 阈值（像素）

    返回 `(at', snapped_target_sec|None)`：`at'` = 吸附后的左端；`None` 表示未吸附。
    只改**传入的 `at`**，绝不触碰任何其它片段（满足"只动被拖的那个"）。
    """
    def _f(v, dflt=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return dflt

    at = _f(at)
    dur = max(0.0, _f(dur))
    pps = _f(px_per_sec)
    if pps <= 0.0:
        return (at, None)
    tol_sec = max(0.0, _f(tol_px, SNAP_TOL_PX)) / pps
    best_at, best_target, best_d = at, None, None
    for t in (targets or ()):
        t = _f(t)
        dl = abs(at - t)                       # 左端吸到 t
        if dl <= tol_sec and (best_d is None or dl < best_d):
            best_d, best_at, best_target = dl, t, t
        dr = abs((at + dur) - t)               # 右端吸到 t
        if dr <= tol_sec and (best_d is None or dr < best_d):
            best_d, best_at, best_target = dr, t - dur, t
    return (best_at, best_target)


def compute_trim(edge, at, end, src_in, src_out, speed, target_sec, src_dur, loopable=False):
    """★ trim 公式（**纯函数**，需求 3 / 设计文档 §需求3）。

    「trim **同时**改『源内起止 + 时间轴起止』（由 `speed` 锁定），**不 ripple**；素材边界**钳位**」。

    参数
    ----
    edge      : `"l"`（trim 开头）| `"r"`（trim 末尾）
    at / end  : 当前时间轴起止（秒）；`end` 可为 None（= 开放结尾）
    src_in/out: 当前源内起止（秒）；`src_out` 可为 None
    speed     : 变速（>0；非法回落 1.0）
    target_sec: 拖动落点（秒）—— 左把手 = 新 `at'`；右把手 = 新 `end'`
    src_dur   : 素材**源时长** S（秒）；`<=0` 表示**未知**
    loopable  : `True` = 该素材可循环（GIF）⇒ **允许把片段拉长**（超过源时长也不钳，
                源出点保持不变，多出来的长度由运行时循环填充）

    返回
    ----
    `(at', end', src_in', src_out')`：
      · `at'` / `src_in'` **恒为 float**（模型 `src_in_sec` 不接受 None）；
      · `end'` / `src_out'` **可为 None**（开放结尾 / 源出点未知）。

    规则（逐字照设计文档 §需求3）
    ----
    · trim 开头（`Δ = at' - at`）：
        - `src_in' = clamp(src_in + Δ·speed, 0, S)`；
        - 若触到**素材头**（`src_in + Δ·speed < 0`）⇒ `src_in' = 0`、`at' = at - src_in/speed`（不能再往左）；
        - `end` / `src_out` **不变**。
    · trim 末尾（`len' = max(0, end' - at)`）：
        - `src_out' = src_in + len'·speed`；
        - 若 `src_out' > S` ⇒ `src_out' = S`、`end' = at + (S - src_in)/speed`（**钳住，不产生冻结尾帧**）；
        - 若 `S` **未知** ⇒ 保持 `end'`、`src_out'` 置 `None`。
    """
    def _f(v, dflt=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return dflt

    at = _f(at)
    src_in = _f(src_in)
    end = None if end is None else _f(end)
    src_out = None if src_out is None else _f(src_out)
    speed = _f(speed, 1.0)
    if speed <= 0.0:
        speed = 1.0
    target = _f(target_sec, at)
    src_dur = _f(src_dur)
    if src_dur < 0.0:
        src_dur = 0.0

    if edge == "l":
        at_new = max(0.0, target)
        if end is not None:
            at_new = min(at_new, end)                 # 起点不得越过终点
        raw_src_in = src_in + (at_new - at) * speed
        if raw_src_in < 0.0:
            # 触到素材头（已到素材开头）⇒ 钳住：不能再往左
            at_new = max(0.0, at - src_in / speed)
            return (at_new, end, 0.0, src_out)
        src_in_new = raw_src_in
        if src_dur > 0.0 and src_in_new > src_dur:
            # 源入点越过素材尾（异常数据）⇒ 钳到素材尾，时间轴起点随之
            src_in_new = src_dur
            at_new = at + (src_dur - src_in) / speed
            if end is not None:
                at_new = min(at_new, end)
        return (at_new, end, src_in_new, src_out)

    # edge == "r"：trim 末尾
    end_new = max(at, target)
    length = max(0.0, end_new - at)
    src_out_new = src_in + length * speed
    if src_dur > 0.0:
        if loopable:
            # ★ 2026-10-22：**GIF 允许拉长** —— 超出源时长时不钳终点；
            #   源出点固定在 min(原 src_out, S)；多出来的时间轴长度由运行时循环填充。
            base_out = src_dur if src_out is None else min(src_out, src_dur)
            base_out = max(base_out, src_in)
            if src_out_new <= base_out + 1e-9:
                # 往回缩：正常按长度算 src_out
                return (at, at + max(0.0, src_out_new - src_in) / speed, src_in, src_out_new)
            # 拉长：终点跟手，源出点不动
            return (at, end_new, src_in, base_out)
        if src_out_new > src_dur:
            # 素材不够长 ⇒ 钳到素材尾（不产生"冻结尾帧"）
            src_out_new = src_dur
            end_new = at + (src_dur - src_in) / speed
        return (at, end_new, src_in, src_out_new)
    # S 未知：无法判定是否越界 ⇒ 只改时间轴终点，**不动源出点**
    return (at, end_new, src_in, None)


def _waveform_mod():
    """惰性取波形**数据层**模块 `ercue.audio.waveform`（零 Qt，只吐 numpy 数组）。

    惰性 import 的好处：启动时不必拖进 numpy；且**每次取的都是同一个模块对象**，
    于是自测/离线可 `monkeypatch ercue.audio.waveform.load_or_compute` 造慢速/失败分支。
    """
    from ercue.audio import waveform
    return waveform


class WaveformWorker(QThread):
    """★ 后台算真实波形（秒级计算**绝不在 GUI 线程做**）。

    照 `panels.py` 的 `ThumbWorker` 套路：`QThread` + 回主线程的信号 + `stop()` 中断。
    不同点：整轨波形只需**最终一条**，故用**单槽（latest-wins）**而非队列 ——
    换轨时后来的请求直接顶掉先前的；旧计算完成时若已被顶掉，其结果**一律作废不回写**。
    """

    done = Signal(str, object)          # (audio_path, WaveformData|None)
    progressed = Signal(str, float)     # (audio_path, 进度 0..1)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending = ""              # 待算的最新音频路径（"" = 无）
        self._seq = 0                   # 每次请求自增；用来判"我这条还算不算数"
        self._stop = False

    def request(self, audio_path):
        """登记一条请求（latest-wins）：旧的在途计算会被自动作废。"""
        with self._lock:
            self._pending = str(audio_path or "")
            self._seq += 1

    def stop(self):
        """请求停止：置停止位并丢掉待处理请求（在途计算会在返回后自行退出）。"""
        with self._lock:
            self._stop = True
            self._pending = ""

    def _is_current(self, seq) -> bool:
        """我这条（`seq`）此刻还是"最新且未停止"的请求吗？"""
        with self._lock:
            return (not self._stop) and self._seq == seq

    def run(self):                                          # noqa: D401
        wf = _waveform_mod()
        while True:
            with self._lock:
                if self._stop:
                    return
                job = self._pending
                self._pending = ""
                seq = self._seq
            if not job:
                self.msleep(40)
                continue

            def _prog(frac, _job=job, _seq=seq):
                # 过期请求不再报进度（避免旧轨进度闪回新轨）
                if self._is_current(_seq):
                    self.progressed.emit(_job, float(frac))

            try:
                data = wf.load_or_compute(job, progress=_prog)
            except Exception:                              # noqa: BLE001
                log.exception("计算波形失败: %s", job)
                data = None
            if self._stop:
                return
            if self._is_current(seq):                      # 未被顶掉才回主线程
                self.done.emit(job, data)


class CueTimeline(QWidget):
    """PR 式时间轴（横轴=秒）。**只负责"画"与"交互"，不改模型** ——
    所有改动以信号发出去由主窗口落到数据模型（单一数据流，避免控件与模型双写）。"""

    picked = Signal(str, str)                     # 选中 (lyr_id, item_id)；item_id="" 表示空白
    moved = Signal(str, str, float)               # 拖动结束 → (lyr_id, item_id, 新起点秒)
    resized = Signal(str, str, float, object)     # 拉边结束 → (lyr_id, item_id, 起点秒, 终点秒|None)
    layer_picked = Signal(str)                    # 点层名 → 选中该层
    layer_moved = Signal(str, str, str, float)    # 跨层 → (旧层, item_id, 新层, 新起点秒)
    clip_dropped = Signal(str, str, float)        # 素材拖到层上 → (lyr_id, clip_id, 落点秒)
    # ★ 需求 3（拖拽剪辑）：trim 松手 → (lyr_id, item_id, at, end|None, src_in, src_out|None)
    #   签名照设计文档 §3.3 **冻结**（window 侧 `_on_timeline_trimmed` 已按此接线）。
    trimmed = Signal(str, str, float, object, float, object)
    # ★ 需求 12（片段右键）：右键命中片段 → (lyr_id, item_id, global_pos)。
    #   菜单**由 window 构建**（控件只发信号、窗口落模型）。
    #   ★ 24 条 #13：**空白也发**（item_id=""，供"粘贴"）。
    context_menu = Signal(str, str, object)
    # ★ 24 条 #6：点时间刻度（标尺）→ (秒)。window 接 `_set_pos`。
    seek_requested = Signal(float)
    # ★ 24 条 #18：再制产份 → (lyr_id, item_id, 落点秒)。"进入"与"拖动产份"共用。
    duplicate_requested = Signal(str, str, float)
    # ★ 第九轮（框选多选）：
    #   · 选中数量变化（0=空 / 1=单选 / >1=多选）→ window 据此禁/启用属性面板。
    selection_count_changed = Signal(int)
    #   · 整组一起移动 → object = list[(lyr_id, item_id, new_at_sec)]（一次 undo 落全部）。
    group_moved = Signal(object)
    #   · 整组复制/再制 → object = list[(lyr_id, item_id, new_at_sec)]（一次 undo 产全部）。
    group_duplicate_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sheet = None
        self.track = None
        self.media_paths = {}                  # clip_id → 磁盘路径（判「素材丢失」用）
        self.bpm = 0.0
        self.view0 = 0.0                       # 视口起点（秒）
        self.view_span = 64.0                  # 视口跨度（秒）
        self.playhead = None                   # 当前位置（秒）；None = 不画
        self.sel = ("", "")
        # ★ 第九轮（框选多选）：并行多选集合（**单选时清空**）。`self.sel` 仍是单选语义、结构不变。
        #   多选态：`len(sel_multi) > 1` ⇒ 拖动/复制/再制走"整组分支"，window 侧属性面板灰化。
        self.sel_multi = set()
        self._blank = None                     # 空白左键按下：{x0,y0,t0,mode:None|"box"|"pan"}
        self._box = None                       # 橡皮筋框：{x0,y0,x1,y1}（widget 像素）
        self.row_h = float(theme.ROW_H)
        self.v_off = 0.0
        self._pan = None                       # (x0, y0, view0, v_off, moved)
        self._drag = None                      # 拖动片段
        self._resize = None                    # 拉边
        self._drop_row = -1
        self._preview_dx = 0.0
        self._preview_len = None               # 拉边预览：新的 (at, end)
        self._last_mx = None
        self._missing_paint_count = 0          # 本帧「丢失」分支绘制次数（哨兵用）
        # ★ 需求 3（双击进剪辑态）：`_edit_mode` = 处于剪辑态的 CueItem（同一时刻至多一个）；
        #   `_trim` = 拖动把手时的本地预览状态（松手才发 `trimmed`）。
        self._edit_mode = None
        self._trim = None
        # ★ 24 条 #14（自动吸附）：默认关；`_snap_guide` = 当前吸附到的目标秒（画竖向导引线）。
        self._snap_on = False
        self._snap_guide = None
        # ★ 24 条 #18（再制状态机）：True = 处在"再制态"（左键拖=产份、右键=退出）。
        self._dup_mode = False
        # ★ 第五轮 #⑥：标尺**按下并拖动** ⇒ 连续发 `seek_requested`（拖动中节流）。
        self._ruler_drag = False
        self._last_seek_emit = 0.0                     # 上次发 seek 的单调时刻（秒）
        # ★ 需求 11（素材音轨）：显示开关（默认关）+ `{clip_id: WaveformData}`（由 window 喂入）。
        #   ⚠ **拿不到数据就不画**（不崩、不卡）；绘制是**独立新路径**，绝不碰歌曲轨波形。
        self._show_clip_audio = False
        self._clip_wav = {}
        # ★ 真实彩色波形状态（数据源 = ercue.audio.waveform；算在后台线程）
        self._wav = None                       # WaveformData | None（当前音频的波形）
        self._wav_path = ""                    # 当前请求/已就绪的音频路径（判「换轨」）
        self._wav_state = "none"               # "none"（无/失败）| "computing" | "ready"
        self._wav_progress = 0.0               # 0..1（计算中的进度，画细进度条用）
        self._wav_worker = None                # WaveformWorker（用到才建）
        self.setMinimumHeight(theme.RULER_H + int(theme.ROW_H) * 2 + 8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        # ★ 需求 3：剪辑态要用 Esc 退出 ⇒ 需要能拿到键盘焦点（点进控件即聚焦）。
        self.setFocusPolicy(Qt.ClickFocus)

    # ==================================================================
    # 对外设置
    # ==================================================================
    def set_sheet(self, sheet):
        if sheet is not self.sheet:
            self._dup_mode = False                     # ★ #18：换码表 ⇒ 退出再制态
            if self.sel_multi:                         # ★ 第九轮：换码表 ⇒ 清多选
                self.sel_multi.clear()
                self.selection_count_changed.emit(1 if self.sel[1] else 0)
        self.sheet = sheet
        self.update()

    def set_media_paths(self, media_paths):
        """更新 `clip_id → 路径` 映射（判素材是否丢失；**不影响选中**）。"""
        self.media_paths = dict(media_paths or {})
        self.update()

    # ------------------------------------------------------------------
    # ★ 需求 11 / 3 —— 冻结对外接口（window 侧一律「有就调用」⇒ 落地即闭环）
    # ------------------------------------------------------------------
    def set_show_clip_audio(self, on):
        """★ 需求 11：素材音轨波形显示开关（默认关）。**只切换绘制，不触发任何计算**。"""
        self._show_clip_audio = bool(on)
        self.update()

    def set_clip_waves(self, waves):
        """★ 需求 11：接收 `{clip_id: WaveformData}`（由 window `_feed_clip_waves()` 喂入）。

        ⚠ 拿不到 / 传 `None` ⇒ 清空，**不崩、不卡**（绘制端见 `_paint_clip_audio`：
        该 clip 无数据时直接不画）。
        """
        self._clip_wav = dict(waves) if isinstance(waves, dict) else {}
        self.update()

    def edit_mode_item(self):
        """（供自测 / 状态）返回当前处于**剪辑态**的 `CueItem`；无则 None。"""
        return self._edit_mode

    def clip_audio_status(self):
        """（供自测 / 状态）返回 `(show_clip_audio, n_waves)`。"""
        return (bool(self._show_clip_audio), len(self._clip_wav))

    # ------------------------------------------------------------------
    # ★ 24 条 #14（自动吸附）/ #13（粘贴落点）/ #18（再制）—— 对外接口
    # ------------------------------------------------------------------
    def set_snap(self, on):
        """★ #14：自动吸附开关（默认关）。**只影响拖动落点**，不写模型。"""
        self._snap_on = bool(on)
        if not self._snap_on:
            self._snap_guide = None
        self.update()

    def snap_enabled(self) -> bool:
        """★ #14：当前是否开了自动吸附。"""
        return bool(self._snap_on)

    def duplicate_mode(self) -> bool:
        """★ #18：是否处在"再制态"。"""
        return bool(self._dup_mode)

    def sec_at_global(self, global_pos) -> float:
        """★ #13：全局坐标 → 时间轴秒（供 window 定"鼠标位置"粘贴落点）。

        越界/非法一律钳到 ≥0（拿不到就返回 0.0，绝不抛）。
        """
        try:
            p = self.mapFromGlobal(global_pos)
            return max(0.0, self._sec(p.x()))
        except Exception:                                  # noqa: BLE001
            log.exception("全局坐标换算时间轴秒失败")
            return 0.0

    def _snap_targets(self, exclude=None):
        """★ #14：吸附候选（秒，已排序）= `0` + 播放头 + **其它**片段的 at/end。

        `exclude` = 正在拖的那个片段（**排除在外** ⇒ 只吸到别人、不吸到自己）。
        """
        ts = [0.0]
        if self.playhead is not None:
            ts.append(_units.to_float(self.playhead))
        for ly in self._layers():
            for it in ly.items:
                if it is exclude:
                    continue
                ts.append(_units.to_float(it.at_sec))
                ts.append(self._item_end_px_sec(ly, it))
        out = []
        for t in ts:
            try:
                out.append(round(float(t), 6))
            except (TypeError, ValueError):
                continue
        return sorted(set(out))

    def _update_cursor(self, pos):
        """★ #9：按当前 hover 位置决定光标。

        剪辑态命中左右把手 ⇒ `SizeHorCursor`；再制态 ⇒ `DragCopyCursor`；其余 ⇒ 箭头。
        """
        try:
            if self._dup_mode:
                self.setCursor(Qt.DragCopyCursor)
                return
            _ly, it, _row, edge = self._hit(pos)
            if self._edit_mode is not None and it is self._edit_mode and edge in ("l", "r"):
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        except Exception:                                  # noqa: BLE001
            log.exception("更新光标失败")

    # ------------------------------------------------------------------
    # ★ 真实彩色波形接线（数据源 = ercue.audio.waveform，零 Qt 数据层）
    # ------------------------------------------------------------------
    def set_audio_path(self, audio_path):
        """告诉时间轴「当前曲目的音频文件路径」，据此接上**真实彩色波形**。

        · 路径没变 ⇒ 直接返回（不重算，也不打断正在算的）。
        · 无音频 / 文件不存在（如**纯图片素材**）⇒ 退化态，**绝不触发计算**。
        · 命中缓存 ⇒ **同步**读入（毫秒级，可接受，无需线程）。
        · 未命中（要算数秒）⇒ 丢**后台线程**（绝不占用 GUI 线程）。
        """
        ap = str(audio_path or "")
        if ap == self._wav_path:
            return
        self._wav_path = ap
        self._wav = None
        self._wav_progress = 0.0
        # 换轨（含"换到没有音频的轨"）：先把在途的旧请求顶掉
        # （latest-wins ⇒ 旧计算完成后作废、不回写当前轨道）
        if self._wav_worker is not None:
            self._wav_worker.request("")
        if not ap or not os.path.isfile(ap):
            self._wav_state = "none"
            self.update()
            return
        cached = self._load_wave_cache(ap)
        if cached is not None:
            self._wav = cached
            self._wav_state = "ready"
            self.update()
            return
        self._wav_state = "computing"
        self._ensure_wave_worker().request(ap)
        self.update()

    @staticmethod
    def _load_wave_cache(audio_path):
        """同步读波形缓存（**命中即返回，从不触发计算**）；拿不到返回 None。

        用数据层的私有读接口 `_load_cache` 只为「只读缓存、绝不顺带重算」——
        若该接口缺席（数据层演进）则安全退化为 None ⇒ 走后台线程，绝不阻塞主线程。
        """
        try:
            wf = _waveform_mod()
            loader = getattr(wf, "_load_cache", None)
            if loader is None:
                return None
            return loader(wf.cache_path_for(audio_path))
        except Exception:                                  # noqa: BLE001
            log.exception("读波形缓存失败: %s", audio_path)
            return None

    def _ensure_wave_worker(self):
        """惰性建后台波形线程（与主窗口同生命周期）。"""
        if self._wav_worker is None:
            self._wav_worker = WaveformWorker(self)
            self._wav_worker.done.connect(self._on_wave_done)
            self._wav_worker.progressed.connect(self._on_wave_progress)
            self._wav_worker.start()
        return self._wav_worker

    def _on_wave_done(self, audio_path, data):
        """后台算完 →（主线程）落地。★ 过期结果丢弃，绝不回写当前轨道。"""
        if str(audio_path or "") != self._wav_path:
            return
        self._wav = data
        self._wav_state = "ready" if data is not None else "none"
        self._wav_progress = 1.0 if data is not None else 0.0
        self.update()

    def _on_wave_progress(self, audio_path, frac):
        if str(audio_path or "") != self._wav_path:
            return
        try:
            self._wav_progress = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            self._wav_progress = 0.0
        self.update()

    def stop_waveform(self):
        """关窗口时停后台波形线程（**不阻塞退出**）。"""
        w = self._wav_worker
        self._wav_worker = None
        if w is None:
            return
        try:
            w.stop()
            if not w.wait(1500):                           # 在途计算最多等 1.5s
                log.warning("波形后台线程 1.5s 未结束（计算中，完成后会自行退出）")
        except Exception:                                  # noqa: BLE001
            log.exception("停止波形线程失败")

    def wave_status(self):
        """（供自测/状态）返回 `(state, has_data, progress, path)`。"""
        return (self._wav_state, self._wav is not None,
                float(self._wav_progress), self._wav_path)

    def wave_sample(self, x):
        """★ 当前视口下像素 `x` 处应画的 `(amp, (r, g, b))`；无真实波形返回 None。

        数据层是 **200 列/秒**（列 `i` 覆盖时长 `i * duration_sec / columns`）；这里把像素
        换算成「浮点列号」，再对相邻两列**线性插值** ⇒ 缩放 / 平移**只重采样、不重算**。
        视口落在数据范围之外（音频比视口短）⇒ 返回 None，绘制端据此**只画可见列**。
        仅供 `_paint_waveform` 与自测共用（自测据此断言"画的是真数据"）。
        """
        data = self._wav
        if data is None:
            return None
        try:
            n = int(data.columns)
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return None
        dur = float(getattr(data, "duration_sec", 0.0) or 0.0)
        if dur <= 0.0:
            dur = max(1e-6, self.view_span)
        colf = self._sec(x) / (dur / float(n))
        if colf < 0.0 or colf > float(n - 1) + 1e-6:
            return None
        i0 = int(math.floor(colf))
        if i0 > n - 1:
            i0 = n - 1
        i1 = min(n - 1, i0 + 1)
        fr = colf - i0
        try:
            a = float(data.amp[i0]) * (1.0 - fr) + float(data.amp[i1]) * fr
            r = float(data.r[i0]) * (1.0 - fr) + float(data.r[i1]) * fr
            g = float(data.g[i0]) * (1.0 - fr) + float(data.g[i1]) * fr
            b = float(data.b[i0]) * (1.0 - fr) + float(data.b[i1]) * fr
        except (IndexError, TypeError, ValueError):
            return None
        return (a, (r, g, b))

    def is_clip_missing(self, clip_id) -> bool:
        """该 clip 此刻是否「素材丢失」（空 clip_id 或路径不存在 / 不是文件）。"""
        p = self.media_paths.get(clip_id) if clip_id else None
        return not (p and os.path.isfile(p))

    def set_track(self, track):
        # 只有**换了一首歌**才清空选中；同一首歌的频繁刷新保留选中（避免每次刷新都掉选）
        if track is not self.track:
            self.sel = ("", "")
            self.sel_multi.clear()                     # ★ 第九轮：换曲目 ⇒ 清多选
            self._dup_mode = False                     # ★ #18：换曲目 ⇒ 退出再制态
        self.track = track
        self._clamp_voff()
        self.update()

    def set_bpm(self, bpm):
        self.bpm = _units.to_float(bpm)
        self.update()

    def set_view(self, sec0, span_sec):
        self.view0 = max(0.0, _units.to_float(sec0))
        self.view_span = max(theme.VIEW_SPAN_MIN, _units.to_float(span_sec))
        self.update()

    def set_playhead(self, sec):
        self.playhead = None if sec is None else _units.to_float(sec)
        self.update()

    def set_selected(self, lyr_id, item_id):
        self.sel = (lyr_id or "", item_id or "")
        self.sel_multi.clear()                     # ★ 第九轮：单选 API ⇒ 清多选
        self.selection_count_changed.emit(1 if self.sel[1] else 0)
        self.update()

    # ------------------------------------------------------------------
    # ★ 第九轮（框选多选）—— 对外只读接口
    # ------------------------------------------------------------------
    def multi_selection(self):
        """返回多选集合（`set[(lyr_id, item_id)]` 的拷贝；单选/空时为**空集**）。"""
        return set(self.sel_multi)

    def selection_ids(self):
        """当前"选中"片段 id 列表：多选时=多选集；否则=单选 `sel`（干净、稳定的顺序）。"""
        if self.sel_multi:
            return sorted(self.sel_multi)
        return [self.sel] if self.sel[1] else []

    def multi_selection_count(self) -> int:
        return len(self.sel_multi) if self.sel_multi else (1 if self.sel[1] else 0)

    def _find(self, lyr_id, item_id):
        """按 id 找 `(layer, item)`（找不到返回 `(None, None)`）。"""
        for ly in self._layers():
            if ly.lyr_id != lyr_id:
                continue
            for it in ly.items:
                if it.item_id == item_id:
                    return ly, it
        return None, None

    def _start_group_drag(self, ly, it, row, pos):
        """★ 第九轮：整组拖动起点 —— 记录**全部多选成员**及其起始 `at`（保相对位置不变）。

        `_drag["item"]` 仍是**锚点**（被按下的那个），沿用原有"位移/吸附/落点"算法；
        新增 `members` 让 `_paint_clip` 对所有成员做**同一位移**预演。
        """
        members = []
        for lid, iid in self.sel_multi:
            l2, m2 = self._find(lid, iid)
            if m2 is not None:
                members.append((l2, m2, _units.to_float(m2.at_sec)))
        if not members:                                    # 兜底：至少含锚点
            members = [(ly, it, _units.to_float(it.at_sec))]
        self._drag = {"lyr_id": ly.lyr_id, "item": it, "row": row,
                      "dx": self._x(_units.to_float(it.at_sec)) - pos.x(),
                      "dur0": 0.0, "target": None, "moved": False,
                      "members": members,
                      "anchor_at0": _units.to_float(it.at_sec)}
        self._drop_row = row
        self.update()

    def _box_hit_ids(self, box):
        """★ 第九轮：框选命中 —— 与框**相交**的片段（跨层全算）。返回稳定顺序的 id 列表。

        与 `_paint_clip` 用**同一套钳到视口的 rect**，保证"框到即选中"。
        """
        if box is None:
            return []
        bx0 = min(box["x0"], box["x1"])
        bx1 = max(box["x0"], box["x1"])
        by0 = min(box["y0"], box["y1"])
        by1 = max(box["y0"], box["y1"])
        if (bx1 - bx0) < BOX_MIN_PX and (by1 - by0) < BOX_MIN_PX:
            return []                                      # 太小视作误触
        W = self.width()
        out = []
        for i, ly in enumerate(self._layers()):
            row = self._row_rect(i)
            top = row.top() + 5
            bot = row.bottom() - 5
            if bot < by0 or top > by1:
                continue
            for it in ly.items:
                x0 = self._x(it.at_sec)
                x1 = self._x(self._item_end_px_sec(ly, it))
                if x1 < theme.GUTTER or x0 > W:
                    continue
                ix0 = max(theme.GUTTER, x0)
                ix1 = min(W, x1)
                if ix1 < bx0 or ix0 > bx1:                 # 横向不相交
                    continue
                out.append((ly.lyr_id, it.item_id))
        return sorted(out)

    def _apply_multi_selection(self, ids):
        """★ 第九轮：结算框选 —— 写 `sel_multi`（>1 才算多选）+ 锚点 `sel`，并通知 window。

        · 0 个 ⇒ 清空（发 `picked("","")` 清面板）；
        · 1 个 ⇒ 视作单选（`sel_multi` 空、`sel`=它）；
        · >1  ⇒ 多选（`sel_multi` 非空、`sel`=第一个作锚点；**不发 `picked`**，
                 避免 window 回设 `set_selected` 把 `sel_multi` 清掉）。
        """
        self.sel_multi = set(ids) if len(ids) > 1 else set()
        if ids:
            self.sel = ids[0]
        else:
            self.sel = ("", "")
            if not self._dup_mode:
                self.picked.emit("", "")
        self.selection_count_changed.emit(
            len(self.sel_multi) if self.sel_multi else (1 if self.sel[1] else 0))
        self.update()

    def _paint_box(self, p: QPainter):
        """★ 第九轮：框选橡皮筋（虚线框 + 淡填充，**纯 UI**）。"""
        if self._box is None:
            return
        bx0 = min(self._box["x0"], self._box["x1"])
        bx1 = max(self._box["x0"], self._box["x1"])
        by0 = min(self._box["y0"], self._box["y1"])
        by1 = max(self._box["y0"], self._box["y1"])
        r = QRectF(bx0, by0, max(1.0, bx1 - bx0), max(1.0, by1 - by0))
        col = QColor(theme.c("accent2"))
        fill = QColor(col)
        fill.setAlpha(38)
        p.fillRect(r, fill)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(col, 1, Qt.DashLine))
        p.drawRect(r)

    def selected_item(self):
        """返回当前选中 `(layer, item)`；未选中返回 `(None, None)`。"""
        if self.track is None or not self.sel[1]:
            return None, None
        for ly in self.track.layers:
            if ly.lyr_id != self.sel[0]:
                continue
            for it in ly.items:
                if it.item_id == self.sel[1]:
                    return ly, it
        return None, None

    def selected_layer(self):
        ly, _it = self.selected_item()
        if ly is not None:
            return ly
        lays = self._layers()
        return lays[0] if lays else None

    # ==================================================================
    # 坐标换算（一律秒）
    # ==================================================================
    def _w(self):
        return max(1, self.width() - theme.GUTTER)

    def _x(self, sec):
        return theme.GUTTER + (_units.to_float(sec) - self.view0) / max(1e-9, self.view_span) * self._w()

    def _sec(self, x):
        return self.view0 + (_units.to_float(x) - theme.GUTTER) / self._w() * self.view_span

    def _layers(self):
        """上面的层画在上面（z 大的在上）⇒ 行序 = z 降序。"""
        if self.track is None:
            return []
        return list(reversed(self.track.sorted_layers()))

    def _row_count(self):
        return len(self._layers()) + 1        # +1 = 音频轨

    def _row_rect(self, i):
        return QRectF(0, theme.RULER_H + i * self.row_h - self.v_off,
                      self.width(), self.row_h)

    def _content_h(self):
        return theme.RULER_H + self._row_count() * self.row_h

    def _max_voff(self):
        return max(0.0, self._content_h() - self.height())

    def _clamp_voff(self):
        self.v_off = max(0.0, min(self.v_off, self._max_voff()))

    def _set_voff(self, v):
        self.v_off = max(0.0, min(float(v), self._max_voff()))
        self.update()

    def _pan_sec(self, dsec):
        self.view0 = max(0.0, self.view0 + float(dsec))
        self.update()

    def _item_end_px_sec(self, ly, it):
        """某片段的终点（秒）：`end_sec`，无则取"本层下一个 item 起点或视口右端"。"""
        end = it.end_sec
        if end is None:
            nxt = [x.at_sec for x in ly.items if _units.to_float(x.at_sec) > _units.to_float(it.at_sec)]
            end = min(nxt) if nxt else (self.view0 + self.view_span)
        return max(_units.to_float(end), _units.to_float(it.at_sec))

    def _clip_is_gif(self, it):
        """该片段的素材是否 GIF（GIF 允许拉长后循环）。"""
        try:
            m = self.sheet.media_by_id(it.clip_id) if self.sheet is not None else None
            name = str(getattr(m, "name", "") or "")
            return name.lower().endswith(".gif")
        except Exception:                                  # noqa: BLE001
            return False

    def _clip_src_dur(self, it):
        """★ 需求 3：素材**源时长** S（秒）。拿不到返回 `0.0`（= 未知 ⇒ trim 末尾不设 `src_out`）。"""
        if self.sheet is None or it is None:
            return 0.0
        try:
            m = self.sheet.media_by_id(it.clip_id)
        except Exception:                                  # noqa: BLE001
            log.exception("取素材时长失败: %s", getattr(it, "clip_id", ""))
            return 0.0
        if m is None:
            return 0.0
        try:
            d = float(getattr(m, "duration_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return d if d > 0.0 else 0.0

    # ==================================================================
    # 滚轮语义（规格书 §5.1）
    # ==================================================================
    def wheelEvent(self, e):
        """滚轮=上下滚动；Shift=左右平移；Ctrl=以鼠标为锚缩放；Alt=轨道行高。"""
        d = e.angleDelta()
        dy, dx = d.y(), d.x()
        mods = e.modifiers()
        # ★ 第九轮（用户反馈"Alt+滚轮还是没实现"）：**兜底读全局键盘状态**
        #   某些平台/环境 `QWheelEvent.modifiers()` 拿不到 Alt（典型：Windows 上 Alt 被菜单栏抢占，
        #   或合成/远程注入的滚轮事件丢了修饰位）。这里补一次 `QApplication.keyboardModifiers()`。
        try:
            _gm = QApplication.keyboardModifiers()
            if _gm & Qt.AltModifier:
                mods = mods | Qt.AltModifier
        except Exception:                                  # noqa: BLE001
            pass
        if dx and not (mods & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier)):
            self._pan_sec(-dx / 120.0 * max(1.0, self.view_span) * 0.08)
            e.accept()
            return
        if not dy:
            return
        n = dy / 120.0
        if mods & Qt.ControlModifier:
            # 以**鼠标所在秒位**为锚缩放（PR 行为）：缩放时手不用重新找位置
            anchor = self._sec(e.position().x())
            frac = (anchor - self.view0) / max(1e-9, self.view_span)
            span = max(theme.VIEW_SPAN_MIN,
                       min(theme.VIEW_SPAN_MAX, self.view_span * (0.82 ** n)))
            self.view_span = span
            self.view0 = max(0.0, anchor - frac * span)
        elif mods & Qt.ShiftModifier:
            self._pan_sec(-n * max(1.0, self.view_span) * 0.08)
        elif mods & Qt.AltModifier:
            _h0 = self.row_h
            self.row_h = max(theme.ROW_H_MIN, min(theme.ROW_H_MAX, self.row_h * (1.08 ** n)))
            self._set_voff(self.v_off)
            # ★ 第九轮：留痕（用户/排查时可用它确认"Alt 到底有没有被收到"）
            log.info("Alt+滚轮：row_h %.1f → %.1f (mods=%s)", _h0, self.row_h, mods)
        else:
            self._set_voff(self.v_off - n * 54.0)
        e.accept()
        self.update()

    # ==================================================================
    # 拖放：素材 → 某层（B5/B6）
    # ==================================================================
    @staticmethod
    def _clip_from_event(e):
        """从拖放事件取 clip_id（**两级兜底**）。

        ⚠ 规格书 §6.1：旧实现这个函数是 `@staticmethod` 却在里面用了 `self`
        ⇒ `NameError` 被裸 `except` 吞掉 ⇒ mime 分支等于不存在 ⇒ 素材"拖不进层"。
        这里**只从参数取**，绝不碰 `self`（B4）。
        """
        if e is None:
            return ""
        try:
            md = e.mimeData()
            if md is not None and md.hasFormat(CLIP_MIME):
                cid = bytes(md.data(CLIP_MIME)).decode("utf-8", "replace")
                if cid:
                    return cid
        except Exception:                                  # noqa: BLE001
            log.exception("读取拖放 mime 失败")
        try:
            src = e.source()
            it = (src.currentItem() if (src is not None and hasattr(src, "currentItem")) else None)
            if it is not None:
                return str(it.data(Qt.UserRole) or "")
        except Exception:                                  # noqa: BLE001
            log.exception("从拖拽源控件兜底取 clip_id 失败")
        return ""

    def dragEnterEvent(self, e):
        if self._clip_from_event(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._clip_from_event(e):
            e.acceptProposedAction()

    def dropEvent(self, e):
        cid = self._clip_from_event(e)
        if not cid:
            return
        lays = self._layers()
        if not lays:
            return
        _ly, _it, row, _edge = self._hit(e.position())
        if row < 0 or row >= len(lays):
            row = 0
        e.acceptProposedAction()
        self.clip_dropped.emit(lays[row].lyr_id, cid, max(0.0, self._sec(e.position().x())))

    # ==================================================================
    # 绘制
    # ==================================================================
    def paintEvent(self, _e):
        self._missing_paint_count = 0          # 每帧重置（哨兵统计本帧）
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, False)
            p.fillRect(self.rect(), QColor(theme.c("bg")))
            self._paint_ruler(p)
            self._paint_rows(p)
            self._paint_snap_guide(p)
            self._paint_box(p)                     # ★ 第九轮：框选橡皮筋（画在片段之上）
            self._paint_playhead(p)
            self._paint_scrollbar(p)
        finally:
            p.end()

    def _paint_snap_guide(self, p: QPainter):
        """★ #14：吸附导引线 —— 拖动吸附到某目标时的一条竖虚线（纯视觉反馈）。"""
        if self._snap_guide is None:
            return
        x = self._x(self._snap_guide)
        if theme.GUTTER <= x <= self.width():
            p.setPen(QPen(QColor(theme.c("accent2")), 1, Qt.DashLine))
            p.drawLine(QPointF(x, theme.RULER_H), QPointF(x, self.height()))

    def _paint_ruler(self, p: QPainter):
        W = self.width()
        grid = QColor(theme.c("grid"))
        fg = QColor(theme.c("fg"))
        p.fillRect(QRectF(0, 0, W, theme.RULER_H), QColor(theme.c("panel")))

        pxs = self._w() / max(1e-9, self.view_span)        # 每秒像素
        sec0 = self.view0
        sec1 = self.view0 + self.view_span
        step_sec, line_sec, show_label, show_bar, show_phrase, show_beat = ruler_plan(pxs)
        p.setFont(QFont("", 8))

        s = int(math.floor(sec0))
        while s <= sec1 + 1:
            if s >= 0:
                x = self._x(s)
                if x >= theme.GUTTER - 1:
                    if s % line_sec == 0:
                        p.setPen(QPen(grid, 1))
                        p.drawLine(QPointF(x, theme.RULER_H - 8), QPointF(x, self.height()))
                    elif show_beat:
                        p.setPen(QPen(grid, 1, Qt.DotLine))
                        p.drawLine(QPointF(x, theme.RULER_H - 4), QPointF(x, theme.RULER_H))
                    if show_label and s % step_sec == 0:
                        p.setPen(fg)
                        p.drawText(int(x) + 2, 12, mmss(s))
            s += 1

        # 拍 / 小节 / 乐句线（BPM 可用时才叠加；退化态 1 拍 = 1 秒，与整秒重合，无意义）
        if _units.bpm_is_usable(self.bpm) and pxs >= theme.RULER_LINE_MIN_PX:
            bl = _units.beat_len(self.bpm)
            k0 = int(math.floor(sec0 / bl))
            k1 = int(math.ceil(sec1 / bl))
            for k in range(k0, k1 + 1):
                if k % 4:
                    continue
                x = self._x(_units.beats_to_sec(k, self.bpm))
                if x < theme.GUTTER - 1:
                    continue
                if k % 8 == 0 and show_phrase:
                    p.setPen(QColor(theme.c("mark_phrase")))
                    p.drawLine(QPointF(x, theme.RULER_H - 8), QPointF(x, theme.RULER_H))
                    p.drawText(int(x) - 3, 22, "\u25c6")
                elif show_bar:
                    p.setPen(QPen(grid.darker(120), 1))
                    p.drawLine(QPointF(x, theme.RULER_H - 6), QPointF(x, theme.RULER_H))
                    p.setPen(QColor(theme.c("mark_bar")))
                    p.drawText(int(x) - 2, 22, "\u25cf")
        p.setPen(QPen(grid, 1))
        p.drawLine(0, theme.RULER_H, W, theme.RULER_H)

    def _paint_rows(self, p: QPainter):
        W = self.width()
        grid = QColor(theme.c("grid"))
        fg = QColor(theme.c("fg"))
        dim = QColor(theme.c("fg_dim"))
        lays = self._layers()
        p.setClipRect(QRectF(0, theme.RULER_H, W, max(0, self.height() - theme.RULER_H)))
        try:
            for i, ly in enumerate(lays):
                r = self._row_rect(i)
                if r.bottom() < theme.RULER_H or r.top() > self.height():
                    continue
                col = QColor(theme.TRACK_COLORS[i % len(theme.TRACK_COLORS)])
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(theme.GUTTER, r.top()), QPointF(W, r.top()))
                p.setPen(fg)
                p.setFont(QFont("", 8))
                p.drawText(QRectF(2, r.top(), theme.GUTTER - 6, r.height()),
                           Qt.AlignVCenter | Qt.AlignLeft, (ly.name or ly.lyr_id)[:11])
                for it in ly.items:
                    self._paint_clip(p, ly, it, r, col)
            # 音频轨（最底一行）
            a_row = len(lays)
            ar = self._row_rect(a_row)
            if ar.bottom() >= theme.RULER_H and ar.top() <= self.height():
                p.fillRect(QRectF(theme.GUTTER, ar.top() + 2, W - theme.GUTTER, ar.height() - 4),
                           QColor(theme.c("audio")))
                p.setPen(QPen(QColor(theme.c("accent2")), 1))
                p.drawLine(QPointF(theme.GUTTER, ar.top()), QPointF(W, ar.top()))
                p.setPen(dim)
                p.setFont(QFont("", 8))
                p.drawText(QRectF(2, ar.top(), theme.GUTTER - 6, ar.height()),
                           Qt.AlignVCenter | Qt.AlignLeft, "音频")
                self._paint_waveform(p, ar)
        finally:
            p.setClipping(False)

    def _paint_waveform(self, p: QPainter, r: QRectF):
        """★ 音频轨里画**真实彩色波形**（数据来自零 Qt 的 `ercue.audio.waveform`）。

        · 列 → 像素：按当前视口做**像素↔列线性插值**（缩放只重采样，不重算）；
        · 高度：以轨道**中线**为中心上下对称，`amp` 映射到轨高（留内边距）；
        · 颜色：每列 `(r, g, b)`（低频红 / 中频绿 / 高频蓝）→ `QColor.fromRgbF`
          （**不交换通道**；仅按幅度等比压暗亮度，保持色相）；
        · 只画**视口内可见的列**：遍历当前宽度的像素即可，长歌也很快。

        无真实数据时**退化**：计算中画一条细进度，否则留空 —— **绝不假装有波形、绝不崩**。
        """
        cy = r.center().y()
        half = max(2.0, (r.height() - 2) / 2.0)     # 内边距 6→2：更贴近轨道上下沿
        if self._wav is None:
            if self._wav_state == "computing":
                self._paint_wave_progress(p, r, cy)
            return
        p.setRenderHint(QPainter.Antialiasing, False)
        W = self.width()
        x = theme.GUTTER
        while x < W:
            s = self.wave_sample(x)
            if s is not None:
                amp, (cr, cg, cb) = s
                a = 0.0 if amp < 0.0 else (1.0 if amp > 1.0 else amp)
                h = max(1.0, min(half, a * half * _WAVE_GAIN))
                k = 0.55 + 0.45 * a          # 亮度随幅度等比缩放（弱段仍见、且不偏色）
                col = QColor.fromRgbF(min(1.0, cr * k), min(1.0, cg * k), min(1.0, cb * k))
                p.setPen(QPen(col, 1))
                p.drawLine(QPointF(x, cy - h), QPointF(x, cy + h))
            x += 1

    def _paint_wave_progress(self, p: QPainter, r: QRectF, cy: float):
        """计算中的退化显示：一条淡中线 + 底部细进度条 + 百分比。"""
        dim = QColor(theme.c("fg_dim"))
        dim.setAlpha(90)
        W = self.width()
        p.setPen(QPen(dim, 1))
        p.drawLine(QPointF(theme.GUTTER, cy), QPointF(W, cy))
        frac = max(0.0, min(1.0, float(self._wav_progress)))
        bh = 3.0
        span = max(0.0, W - theme.GUTTER)
        p.fillRect(QRectF(theme.GUTTER, r.bottom() - bh - 2.0, span * frac, bh),
                   QColor(theme.c("accent2")))
        if span > 120:
            p.setPen(QColor(theme.c("fg_dim")))
            p.setFont(QFont("", 7))
            p.drawText(QRectF(theme.GUTTER + 4, r.top() + 1, span - 8, 12),
                       Qt.AlignVCenter | Qt.AlignLeft, "波形计算中 %d%%" % int(round(frac * 100)))

    def _paint_clip(self, p: QPainter, ly, it, row_rect: QRectF, col: QColor):
        W = self.width()
        at = _units.to_float(it.at_sec)
        src_in = _units.to_float(it.src_in_sec)
        spd = _units.to_float(it.speed) or 1.0
        end = self._item_end_px_sec(ly, it)
        x0 = self._x(at)
        x1 = self._x(max(end, at + 0.5))
        # 预览期间的**有效**时间轴起点 / 源入点：块几何与素材波形**共用**同一套映射
        # ⇒ 拖动/拉边/裁剪时块与波形**一起走**（BugFix Bug2：波形不跟手）。
        eff_at, eff_src_in = at, src_in
        # ★ 第六轮（CDR 再制）：复制模式下跟手的**不是原件**，而是"副本预演"
        #   ⇒ 原件画在**按下位置**（不位移），另画一个随光标走的半透明虚线块。
        ghost_dx = 0.0
        is_drag_target = False
        if self._drag is not None:
            _members = self._drag.get("members")
            if _members is not None:                       # ★ 第九轮：整组拖动 —— 全成员同位移
                is_drag_target = any(m is it for (_l, m, _a) in _members)
            else:
                is_drag_target = (self._drag.get("item") is it)
        if is_drag_target:
            if self._dup_mode:
                ghost_dx = float(self._preview_dx)
            else:
                x0 += self._preview_dx
                x1 += self._preview_dx
                eff_at = at + self._preview_dx / max(1e-9, self._w() / max(1e-9, self.view_span))
        if self._resize is not None and self._resize.get("item") is it:
            pa, pe = self._resize.get("preview", (at, end))
            x0 = self._x(pa)
            x1 = self._x(max(pe, pa + 0.5))
            eff_at = _units.to_float(pa)
        if self._trim is not None and self._trim.get("item") is it:
            # ★ 需求 3：trim 拖动时的**本地预览**几何（松手前只改画、不改模型）
            pv = self._trim.get("preview")
            pa, pe = pv[:2] if pv is not None else (at, end)     # ★ BugFix Bug1：按下未移动前 preview=None
            if pe is None:
                pe = end
            x0 = self._x(pa)
            x1 = self._x(max(pe, pa + 0.5))
            eff_at = _units.to_float(pa)
            if pv is not None and len(pv) >= 4:
                eff_src_in = _units.to_float(pv[2])
        if x1 < theme.GUTTER or x0 > W:
            if ghost_dx:                                   # ★ 原件在视口外 ⇒ 仍画副本预演
                self._paint_dup_ghost(p, x0 + ghost_dx, x1 + ghost_dx, row_rect, col)
            return
        rect = QRectF(max(theme.GUTTER, x0), row_rect.top() + 5,
                      max(3.0, min(W, x1) - max(theme.GUTTER, x0)),
                      max(6.0, self.row_h - 12))
        sel = ((self.sel[0] == ly.lyr_id and self.sel[1] == it.item_id)
               or ((ly.lyr_id, it.item_id) in self.sel_multi))     # ★ 第九轮：多选全部高亮
        if self.is_clip_missing(it.clip_id):
            # ★ 规格书 §15：片段**仍占位**（仍可拖拽移动 / 删除 / 跨层），
            #   只换画法 —— 红色半透明覆盖 + 白色大字「文件丢失」。
            self._missing_paint_count += 1
            p.setBrush(col)
            p.setPen(QPen(QColor("#ffffff") if sel else col.darker(150), 2 if sel else 1))
            p.drawRoundedRect(rect, 3, 3)
            p.fillRect(rect, QColor(200, 40, 40, 150))       # 红色半透明覆盖
            name = ""
            if self.sheet is not None:
                m = self.sheet.media_by_id(it.clip_id)
                name = (m.name if m is not None else "") or ""
            p.setPen(QColor("#ffffff"))
            f = QFont("", 9 if rect.width() >= 64 else 7)
            f.setBold(True)
            p.setFont(f)
            p.drawText(rect, Qt.AlignCenter, "文件丢失")
            if name and rect.width() > 96:
                p.setFont(QFont("", 7))
                p.drawText(rect.adjusted(4, 0, -4, -2),
                           Qt.AlignBottom | Qt.AlignHCenter, name)
            self._paint_clip_edit(p, it, rect)               # ★ 需求 3：剪辑态高亮 + 把手
            if ghost_dx:                                     # ★ 第六轮：副本预演（丢失样式按正常块画）
                self._paint_dup_ghost(p, x0 + ghost_dx, x1 + ghost_dx, row_rect, col)
            return
        p.setBrush(col.lighter(140) if sel else col)
        p.setPen(QPen(QColor("#ffffff") if sel else col.darker(150), 2 if sel else 1))
        p.drawRoundedRect(rect, 3, 3)
        # ★ 需求 11：素材音轨波形（**独立新绘制路径**，画在片段块内；受开关控制）
        if self._show_clip_audio:
            self._paint_clip_audio(p, ly, it, rect, eff_at, eff_src_in, spd)
        name = ""
        if self.sheet is not None:
            m = self.sheet.media_by_id(it.clip_id)
            name = (m.name if m is not None else it.clip_id) or ""
        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("", 8))
        p.drawText(rect.adjusted(4, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft,
                   name[:max(1, int(rect.width() / 7))])
        # ★ 需求 3：剪辑态高亮 + 左右把手（**少量**绘制，画在最上层）
        self._paint_clip_edit(p, it, rect)
        # ★ 第六轮（CDR）：副本预演 —— 画在原件之上（跟手移动，松手才真正产份）
        if ghost_dx:
            self._paint_dup_ghost(p, x0 + ghost_dx, x1 + ghost_dx, row_rect, col)

    def _paint_dup_ghost(self, p: QPainter, gx0: float, gx1: float,
                         row_rect: QRectF, col: QColor):
        """★ 第六轮（CDR 再制）：绘制**副本预演** —— 跟随光标的半透明虚线块。

        复制模式下原件留在"按下位置"不动，本预演随光标走；松开左键时才真正产份。
        视口外不画（无副作用）。
        """
        W = self.width()
        if gx1 < theme.GUTTER or gx0 > W:
            return
        grect = QRectF(max(theme.GUTTER, gx0), row_rect.top() + 5,
                       max(3.0, min(W, gx1) - max(theme.GUTTER, gx0)),
                       max(6.0, self.row_h - 12))
        fill = QColor(col)
        fill.setAlpha(110)
        p.setBrush(fill)
        pen = QColor(theme.c("accent"))
        pen.setAlpha(235)
        p.setPen(QPen(pen, 2, Qt.DashLine))
        p.drawRoundedRect(grect, 3, 3)
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("", 7))
        p.drawText(grect.adjusted(4, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft, "副本")

    def _paint_clip_edit(self, p: QPainter, it, rect: QRectF):
        """★ 需求 3：剪辑态视觉 —— 强调色高亮边框 + 左右两个 trim 把手。

        只在 `self._edit_mode is it` 时绘制（同一时刻至多一个片段处于剪辑态）。
        """
        if self._edit_mode is None or self._edit_mode is not it:
            return
        accent = QColor(theme.c("accent"))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(accent, 2))
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        hw = TRIM_HANDLE_W
        p.setPen(Qt.NoPen)
        p.setBrush(accent)
        p.drawRect(QRectF(rect.left(), rect.top(), hw, rect.height()))
        p.drawRect(QRectF(rect.right() - hw, rect.top(), hw, rect.height()))
        p.setBrush(Qt.NoBrush)

    def _paint_clip_audio(self, p: QPainter, ly, it, rect: QRectF,
                          eff_at=None, eff_src_in=None, spd=None):
        """★ 需求 11：在**片段块内**画该素材自己的音轨波形（**独立新路径**）。

        · 像素 ↔ 源秒：`src_sec = src_in + (时间轴秒 - 片段起点) × speed`；
        · 列号 = `源秒 × (columns / duration_sec)`（与歌曲轨**同一套读法/插值**）；
        · **只画视口内可见列**；数据缺失 / 非法 ⇒ **直接不画**（不崩、不卡）。

        ★ BugFix Bug2：`eff_at` / `eff_src_in` 是**预览期间的有效起点/源入点**（由
          `_paint_clip` 与块几何一起算好传入）—— 这样拖动/裁剪时波形**跟着块走**，
          而不是停在模型里的原 `at_sec`。

        ⚠ 本方法**完全独立**于歌曲轨 `_paint_waveform` / `wave_sample`，二者互不影响。
        """
        wf = self._clip_wav.get(it.clip_id) if self._clip_wav else None
        if wf is None:
            return
        try:
            n = int(wf.columns)
        except (TypeError, ValueError):
            return
        dur = float(getattr(wf, "duration_sec", 0.0) or 0.0)
        if n <= 0 or dur <= 0.0:
            return
        cols_per_sec = float(n) / dur
        at = _units.to_float(it.at_sec) if eff_at is None else float(eff_at)
        src_in = _units.to_float(it.src_in_sec) if eff_src_in is None else float(eff_src_in)
        if spd is None:
            spd = _units.to_float(it.speed) or 1.0
        x_lo = max(theme.GUTTER, int(rect.left()))
        x_hi = min(self.width(), int(rect.right()))
        if x_hi <= x_lo:
            return
        cy = rect.center().y()
        half = max(1.0, (rect.height() - 3) / 2.0)
        x = x_lo
        while x <= x_hi:
            src_sec = src_in + max(0.0, self._sec(x) - at) * spd
            s = self._wave_sample_of(wf, src_sec, cols_per_sec)
            if s is not None:
                amp, (cr, cg, cb) = s
                a = 0.0 if amp < 0.0 else (1.0 if amp > 1.0 else amp)
                h = max(1.0, min(half, a * half * _WAVE_GAIN))
                k = 0.55 + 0.45 * a          # 亮度随幅度等比缩放（与歌曲轨同一映射）
                col = QColor.fromRgbF(min(1.0, cr * k), min(1.0, cg * k), min(1.0, cb * k))
                p.setPen(QPen(col, 1))
                p.drawLine(QPointF(x, cy - h), QPointF(x, cy + h))
            x += 1

    @staticmethod
    def _wave_sample_of(wf, src_sec, cols_per_sec):
        """★ 需求 11：按**源秒**取 `(amp, (r, g, b))`（相邻列线性插值）；越界返回 None。

        **独立新函数**（绝不改歌曲轨的 `wave_sample`）；自测可据此断言"画的是真数据"。
        """
        try:
            n = int(wf.columns)
        except (TypeError, ValueError):
            return None
        if n <= 0 or cols_per_sec <= 0.0:
            return None
        colf = float(src_sec) * float(cols_per_sec)
        if colf < 0.0 or colf > float(n - 1) + 1e-6:
            return None
        i0 = int(math.floor(colf))
        if i0 > n - 1:
            i0 = n - 1
        i1 = min(n - 1, i0 + 1)
        fr = colf - i0
        try:
            a = float(wf.amp[i0]) * (1.0 - fr) + float(wf.amp[i1]) * fr
            r = float(wf.r[i0]) * (1.0 - fr) + float(wf.r[i1]) * fr
            g = float(wf.g[i0]) * (1.0 - fr) + float(wf.g[i1]) * fr
            b = float(wf.b[i0]) * (1.0 - fr) + float(wf.b[i1]) * fr
        except (IndexError, TypeError, ValueError):
            return None
        return (a, (r, g, b))

    def _paint_playhead(self, p: QPainter):
        if self.playhead is None:
            return
        x = self._x(self.playhead)
        if theme.GUTTER <= x <= self.width():
            p.setPen(QPen(QColor(theme.c("playhead")), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))

    def _paint_scrollbar(self, p: QPainter):
        if self._max_voff() <= 0:
            return
        track_h = max(1.0, self.height() - theme.RULER_H)
        thumb_h = max(18.0, track_h * track_h / max(1.0, self._content_h()))
        y = theme.RULER_H + (track_h - thumb_h) * (self.v_off / self._max_voff())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#555555"))
        p.drawRoundedRect(QRectF(self.width() - 6, y, 4, thumb_h), 2, 2)
        p.setBrush(Qt.NoBrush)

    # ==================================================================
    # 命中测试（规格书 §5.4）
    # ==================================================================
    def _hit(self, pos):
        """返回 `(layer, item, row, edge)`；未命中 item 时 item=None、edge=""；无命中 row=-1。

        · `pos.y() < RULER_H` → 标尺区不算命中。
        · 行号越界 / 音频行 → 无命中（音频行不算层）。
        · 片段命中含**边缘容差** `theme.EDGE_TOL` 像素（拉边改时长用）：
          edge = "l" / "r" / ""。
        · ★ 第六轮（③ 根因修复）：命中用**钳到视口的坐标**（与绘制一致）—— 见下方注释。
        """
        if pos.y() < theme.RULER_H:
            return None, None, -1, ""
        i = int((pos.y() - theme.RULER_H + self.v_off) // self.row_h)
        if i < 0:
            return None, None, -1, ""
        lays = self._layers()
        if not (0 <= i < len(lays)):
            return None, None, -1, ""
        ly = lays[i]
        W = self.width()
        for it in ly.items:
            x0 = self._x(it.at_sec)
            x1 = self._x(self._item_end_px_sec(ly, it))
            if x1 < theme.GUTTER or x0 > W:
                continue            # 完全在视口外（未绘制）⇒ 不参与命中，避免"幽灵命中"
            # ★ 第六轮（③ 根因修复）：块与把手绘制时**钳到视口**（`_paint_clip` 用
            #   `max(GUTTER,x0)`/`min(W,x1)` 定 rect）⇒ 命中与"边缘"判定必须用**同一套钳位坐标**。
            #   旧实现用**未钳位**的真实边缘：当片段某端在视口外（长片段极常见），画出来的
            #   把手在视口边、却命不中 ⇒ 抓把手落到 `edge=""` ⇒ 变成"移动整块"，
            #   用户观感就是"拖左右两边改长短没效果"。
            x0c = max(theme.GUTTER, x0)
            x1c = min(W, x1)
            if x0c - theme.EDGE_TOL <= pos.x() <= x1c + theme.EDGE_TOL:
                edge = ""
                if abs(pos.x() - x0c) <= theme.EDGE_TOL:
                    edge = "l"
                elif abs(pos.x() - x1c) <= theme.EDGE_TOL:
                    edge = "r"
                return ly, it, i, edge
        return ly, None, i, ""

    # ==================================================================
    # 鼠标交互（规格书 §5.2；需求 3 剪辑态 / 需求 12 右键）
    # ==================================================================
    def mouseDoubleClickEvent(self, e):
        """★ 需求 3：双击片段 ⇒ 进入**剪辑态**（高亮 + 左右把手）；双击空白 ⇒ 退出剪辑态。

        ★ #18：双击 ⇒ 退出"再制态"。
        """
        if e.button() != Qt.LeftButton:
            return
        self._dup_mode = False                         # ★ #18：双击 ⇒ 回普通态
        ly, it = self._hit(e.position())[:2]
        if ly is None or it is None:
            if self._edit_mode is not None:
                self._edit_mode = None
                self._trim = None
                self.update()
            return
        self.sel = (ly.lyr_id, it.item_id)
        self.sel_multi.clear()                       # ★ 第九轮：双击片段 ⇒ 收敛为单选
        self.picked.emit(ly.lyr_id, it.item_id)
        self._edit_mode = it                 # 双击（另一）片段 ⇒ 剪辑态切到它；同一时刻仅一个
        self._trim = None
        self.update()
        e.accept()

    def _cancel_gesture(self):
        """取消**在途的左键手势**（拖动 / trim / 拉边 + 吸附导引线），**绝不改模型**。

        返回 `(item, lyr_id)` = 被取消的那个片段（无手势则 `(None, "")`）。
        ★ 第五轮修复③：右键若发生在"拖动中"，必须把 pending 的 `_drag` 清掉 ——
        否则随后松开左键时 `mouseReleaseEvent` 会发 `moved` ⇒ **原件被挪到落点**（用户实测 bug）。
        """
        it, ly_id = None, ""
        for g in (self._drag, self._trim, self._resize):
            if g is not None:
                it = g.get("item")
                ly_id = g.get("lyr_id", "")
                break
        if self._drag is not None or self._trim is not None or self._resize is not None:
            self._drag = None
            self._trim = None
            self._resize = None
            self._preview_dx = 0.0
            self._snap_guide = None
            self.update()
        # ★ 第九轮：框选在途 ⇒ 一并取消（Esc / 右键）
        if self._blank is not None or self._box is not None:
            self._blank = None
            self._box = None
            self.update()
        return (it, ly_id)

    def contextMenuEvent(self, e):
        """★ 需求 12 / 24 条 #13 + #18（★ 第六轮 CDR 式再制）+ 第五轮③：右键分流。

        ★ 第六轮（用户实测语义，权威）：
          · **拖动中**按右键（尚未进复制模式）⇒ **进入复制模式**：原件锁定在按下位置、
            光标 `DragCopyCursor`、跟手的是"副本预演"。⚠ 此刻**不产份、不弹菜单**。
          · **已在复制模式** ⇒ 再按右键 = **取消复制**（不发副本、原件回原位）。
          · **没有"拖动中"的普通右键**（片段已选中、未在拖动）⇒ **立即复制一份**（旧语义保留，
            **一次性**，不进入会一直留着的复制模式 ⇒ 满足"再制后自动取消"）。
          · 命中片段但**未选中** ⇒ 照旧发 `context_menu(lyr, item, gpos)`（菜单由 window 构建）。
          · **空白**（#13）⇒ 发 `context_menu("", "", gpos)`（供 window 弹"粘贴"）。
        """
        gpos = e.globalPos()
        # ① 拖动中按右键（尚未进复制模式）⇒ 进入复制模式（**不产份、不弹菜单**）
        if self._drag is not None and not self._dup_mode:
            self._dup_mode = True
            self.setCursor(Qt.DragCopyCursor)
            self.update()
            e.accept()
            return
        # ② 已在复制模式 ⇒ 再按右键 = 取消复制（不发副本、原件回原位）
        if self._dup_mode:
            self._cancel_gesture()                     # 清掉副本预演（原件回到按下位置）
            self._dup_mode = False
            self.setCursor(Qt.ArrowCursor)
            self.update()
            e.accept()
            return
        # ③ 其余：先取消在途手势（trim / resize；拖动中已在 ①/② 处理），再按原语义分流
        g_item, g_lyr = self._cancel_gesture()         # ★ 第五轮③
        if g_item is not None:                         # trim/resize 在途 ⇒ 目标 = 那个片段
            it, lyr_id = g_item, g_lyr
        else:
            _ly, it = self._hit(e.pos())[:2]
            lyr_id = (_ly.lyr_id if _ly is not None else "")
        if it is not None:
            # ★ 第九轮：多选态下右键"多选成员" ⇒ **整组**按"锚点对齐到点击处"复制一份
            if len(self.sel_multi) > 1 and (lyr_id, it.item_id) in self.sel_multi:
                delta = max(0.0, self._sec(e.pos().x())) - _units.to_float(it.at_sec)
                entries = []
                for lid, iid in sorted(self.sel_multi):
                    _l2, m2 = self._find(lid, iid)
                    if m2 is not None:
                        entries.append((lid, iid, max(0.0, _units.to_float(m2.at_sec) + delta)))
                if entries:
                    self.group_duplicate_requested.emit(entries)
                    e.accept()
                    return
            if self.sel == (lyr_id, it.item_id):
                # ★ 旧语义保留：右键"已选中片段"且**未在拖动** ⇒ 立即复制一份
                #   （一次性；不进复制模式 ⇒ 无需再手动取消）
                self.duplicate_requested.emit(lyr_id, it.item_id,
                                              max(0.0, self._sec(e.pos().x())))
                e.accept()
                return
            self.sel = (lyr_id, it.item_id)
            self.sel_multi.clear()                 # ★ 第九轮：右键未选中的片段 ⇒ 收敛为单选
            self.picked.emit(lyr_id, it.item_id)
            self.update()
            self.context_menu.emit(lyr_id, it.item_id, gpos)
            e.accept()
            return
        # 空白：照旧发菜单（item=""），供 window 弹"粘贴"
        self.context_menu.emit("", "", gpos)
        e.accept()

    def keyPressEvent(self, e):
        """★ 需求 3 / #18：`Esc` 退出剪辑态 + 再制态（其余键交回基类）。"""
        if e.key() == Qt.Key_Escape:
            changed = False
            if self._dup_mode:
                # ★ 第六轮：取消复制 —— 清掉副本预演（原件回到按下位置），不发副本
                self._cancel_gesture()
                self._dup_mode = False
                changed = True
            if self._box is not None or self._blank is not None:
                # ★ 第九轮：Esc 取消在途框选
                self._blank = None
                self._box = None
                changed = True
            if self.sel_multi:
                # ★ 第九轮②：Esc 清多选 ⇒ 回到单选（锚点）⇒ 面板恢复可编辑
                self.sel_multi.clear()
                self.selection_count_changed.emit(1 if self.sel[1] else 0)
                changed = True
            if self._edit_mode is not None:
                self._edit_mode = None
                self._trim = None
                changed = True
            if changed:
                self.setCursor(Qt.ArrowCursor)
                self.update()
                e.accept()
                return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        # ★ 24 条 #6 + 第五轮⑥：左键在**标尺**上 ⇒ 定位（发 `seek_requested`）；**按下并拖动** ⇒
        #   连续 seek（拖动中节流 ~30ms、松手定稿）。⚠ **不进入平移**（标尺拖动 ≠ 平移）。
        if e.button() == Qt.LeftButton and e.position().y() < theme.RULER_H:
            self._ruler_drag = True
            self._last_seek_emit = time.monotonic()
            self.seek_requested.emit(max(0.0, self._sec(e.position().x())))
            e.accept()
            return
        # ★ 第九轮：中键 ⇒ **立即**平移（原样）；左键在空白 ⇒ **延迟判定**（见 mouseMoveEvent）：
        #   · 按下后"立即移动"（< BOX_PAN_MS）⇒ 框选（橡皮筋）；
        #   · 按下"静止 ≥ BOX_PAN_MS 后再移动" ⇒ 平移（沿用原 `_pan`）。
        hit = self._hit(e.position())
        if e.button() == Qt.MiddleButton:
            self._pan = (e.position().x(), e.position().y(), self.view0, self.v_off, False)
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        if e.button() == Qt.LeftButton and hit[1] is None:
            # ★ 需求 3：左键单击空白 ⇒ **退出剪辑态**（立即，保持原手感）
            if self._edit_mode is not None:
                self._edit_mode = None
                self._trim = None
            # 记下按下点 + 时刻；**先不清选中、先不平移**（等"移动开始那一刻"再定 box/pan）。
            self._blank = {"x0": e.position().x(), "y0": e.position().y(),
                           "t0": time.monotonic(), "mode": None}
            self._box = None
            self.update()
            e.accept()
            return
        if e.button() != Qt.LeftButton:
            return
        ly, it, row, edge = hit
        if ly is None or it is None:
            if ly is not None and hit[3] == "":
                self.picked.emit("", "")
            return
        key = (ly.lyr_id, it.item_id)
        # ★ 第九轮：按在"多选成员"上 ⇒ **保持多选、整组一起拖**（不塌回单选、不发 picked）
        if len(self.sel_multi) > 1 and key in self.sel_multi:
            self.sel = key                                  # 锚点（直接赋值，保留 sel_multi）
            self._start_group_drag(ly, it, row, e.position())
            e.accept()
            return
        # 单选：清多选（若有）
        if self.sel_multi:
            self.sel_multi.clear()
            self.selection_count_changed.emit(1 if it.item_id else 0)
        self.sel = key
        self.picked.emit(ly.lyr_id, it.item_id)
        # ★ 需求 3：剪辑态 + 命中把手 ⇒ trim 拖动（只本地预览；松手才发 `trimmed`）
        #   ⚠ 第七轮回退：**非剪辑态**命中边缘 ⇒ 走下方既有 `_resize` 路径（"单击拖两端"的既有实现），
        #     不能在这里抢占（否则 `_resize` 变死代码、且 window 侧 `resized` 消费断链）。
        if self._edit_mode is not None and self._edit_mode is it and edge in ("l", "r"):
            at0 = _units.to_float(it.at_sec)
            end0 = self._item_end_px_sec(ly, it)
            self._trim = {"lyr_id": ly.lyr_id, "item": it, "row": row, "edge": edge,
                          "at0": at0, "end0": end0,
                          "src_in0": _units.to_float(it.src_in_sec),
                          "src_out0": it.src_out_sec,
                          "src_dur": self._clip_src_dur(it),
                          "loopable": self._clip_is_gif(it),
                          # ★ A1 加固：初值给"当前几何（at0,end0,src_in0,src_out0）"而非 None
                          #   （与拖动期 `compute_trim` 产出的 4 元组**同格式**，双保险）
                          "preview": (at0, end0, _units.to_float(it.src_in_sec), it.src_out_sec),
                          "moved": False}
            self._drop_row = row
            self.setCursor(Qt.SizeHorCursor)           # ★ #9：进入 trim 拖动 = 左右箭头
            self.update()
            return
        # ★ 第七轮（2026-10-17 用户明确要求）：**单击不能拖两端** ——
        #   只有"双击进剪辑态"才能改长短（见上方 `_trim` 分支，`_edit_mode` 前置）。
        #   非剪辑态一律按"移动整块"处理（忽略边缘命中）。
        #   ⚠ 历史：曾支持"单击拖边缘 ⇒ `_resize`"，用户实测后要求取消（防止误拖改变长短）。
        #      `_resize` 状态 / `resized` 信号与其 window 侧消费**保留未删**（只是不再进入）。
        dur0 = max(0.0, self._item_end_px_sec(ly, it) - _units.to_float(it.at_sec))
        self._drag = {"lyr_id": ly.lyr_id, "item": it, "row": row,
                      "dx": self._x(it.at_sec) - e.position().x(),
                      "dur0": dur0, "target": None, "moved": False}
        self._drop_row = row
        self.update()

    def mouseMoveEvent(self, e):
        self._last_mx = e.position().x()
        if self._ruler_drag:
            # ★ 第五轮⑥：标尺拖动 ⇒ 连续 seek（节流 ~30ms，松手定稿）
            now = time.monotonic()
            if now - self._last_seek_emit >= 0.030:
                self._last_seek_emit = now
                self.seek_requested.emit(max(0.0, self._sec(e.position().x())))
            return
        # ★ 第九轮：空白左键手势 —— 移动开始时按"按下至今的时长"锁定 box / pan（锁定后不再回摆）
        if self._blank is not None:
            bp = self._blank
            if bp["mode"] == "box":
                self._box["x1"] = e.position().x()
                self._box["y1"] = e.position().y()
                self._box["moved"] = True
                self.update()
                return
            if bp["mode"] is None:
                dx = e.position().x() - bp["x0"]
                dy = e.position().y() - bp["y0"]
                if math.hypot(dx, dy) < BOX_START_PX:
                    return                                 # 还没真正开始移动
                if (time.monotonic() - bp["t0"]) * 1000.0 < BOX_PAN_MS:
                    bp["mode"] = "box"                     # 立即移动 ⇒ 框选
                    self._box = {"x0": bp["x0"], "y0": bp["y0"],
                                 "x1": e.position().x(), "y1": e.position().y(),
                                 "moved": False}
                    self.setCursor(Qt.CrossCursor)
                    self.update()
                    return
                bp["mode"] = "pan"                         # 长按后再移动 ⇒ 平移
                self._pan = (bp["x0"], bp["y0"], self.view0, self.v_off, True)
                self._blank = None
                self.setCursor(Qt.ClosedHandCursor)
                # 落到下方 `_pan` 分支处理本次位移
        if self._pan is not None:
            x0, y0, v0, vo0, _m = self._pan
            dx = e.position().x() - x0
            dy = e.position().y() - y0
            pxps = max(1e-9, self._w() / max(1e-9, self.view_span))
            self.view0 = max(0.0, v0 - dx / pxps)     # 往右拖 = 时间往前
            self.v_off = max(0.0, min(vo0 - dy, self._max_voff()))
            self._pan = (x0, y0, v0, vo0, True)
            self.update()
            return
        if self._resize is not None:
            rs = self._resize
            it = rs["item"]
            sec = max(0.0, self._sec(e.position().x()))
            if rs["edge"] == "l":
                at = max(0.0, min(sec, rs["end0"] - 0.05))
                rs["preview"] = (at, rs["end0"] if it.end_sec is not None else None)
                rs["moved"] = abs(at - rs["at0"]) > 1e-3
            else:
                end = max(rs["at0"] + 0.05, sec)
                rs["preview"] = (rs["at0"], end)
                rs["moved"] = abs(end - rs["end0"]) > 1e-3
            self.update()
            return
        if self._trim is not None:
            # ★ 需求 3：拖动把手 —— **只本地预览**（松手才入栈一次，见 mouseReleaseEvent）
            ts = self._trim
            it = ts["item"]
            self.setCursor(Qt.SizeHorCursor)           # ★ #9：trim 拖动期间保持左右箭头
            target = max(0.0, self._sec(e.position().x()))
            at, end, src_in, src_out = compute_trim(
                ts["edge"], ts["at0"], ts["end0"], ts["src_in0"], ts["src_out0"],
                it.speed, target, ts["src_dur"], ts.get("loopable", False))
            ts["preview"] = (at, end, src_in, src_out)
            if ts["edge"] == "l":
                ts["moved"] = abs(at - ts["at0"]) > 1e-3
            else:
                ts["moved"] = abs(end - ts["end0"]) > 1e-3
            self.update()
            return
        if self._drag is None:
            self._update_cursor(e.position())          # ★ #9：非拖动时按 hover 换光标
            return
        it = self._drag["item"]
        new_x = e.position().x() + self._drag["dx"]
        target = max(0.0, self._sec(new_x))
        # ★ #14：自动吸附（只改**被拖的这个**的落点；绝不写其它片段）
        self._snap_guide = None
        if self._snap_on:
            pxps = max(1e-9, self._w() / max(1e-9, self.view_span))
            target, guide = snap_at(target, self._drag.get("dur0", 0.0),
                                    self._snap_targets(exclude=it), pxps)
            target = max(0.0, float(target))
            self._snap_guide = guide
        self._drag["target"] = float(target)
        self._preview_dx = self._x(target) - self._x(it.at_sec)
        if abs(self._preview_dx) > 1.0:
            self._drag["moved"] = True
        _ly2, _it2, row2, _edge2 = self._hit(e.position())
        if 0 <= row2 < len(self._layers()) and row2 != self._drop_row:
            self._drop_row = row2
            self._drag["moved"] = True
        self.update()

    def mouseReleaseEvent(self, e):
        if self._ruler_drag:
            # ★ 第五轮⑥：标尺拖动松手 ⇒ 最后一次 seek **定稿**
            self._ruler_drag = False
            self.seek_requested.emit(max(0.0, self._sec(e.position().x())))
            self.update()
            return
        # ★ 第九轮：空白左键松手 —— 框选结算 / 未移动=取消选择（平移分支在上面已转为 `_pan`）
        if self._blank is not None and e.button() == Qt.LeftButton:
            bp = self._blank
            self._blank = None
            if bp["mode"] == "box":
                box = self._box
                self._box = None
                self.setCursor(Qt.ArrowCursor)
                self._apply_multi_selection(self._box_hit_ids(box))
                e.accept()
                return
            # mode is None ⇒ 没移动：点空白 = **取消选择**（保留原行为，含清多选）
            if self._edit_mode is not None:
                self._edit_mode = None
                self._trim = None
            changed = False
            if self.sel_multi:
                self.sel_multi.clear()
                changed = True
            if not self._dup_mode and self.sel != ("", ""):
                self.sel = ("", "")
                self.picked.emit("", "")
                changed = True
            if changed:
                self.selection_count_changed.emit(0)
            self.update()
            e.accept()
            return
        if self._pan is not None:
            self._pan = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            return
        # ★ 第六轮（CDR）：**右键松开本身无动作**（复制的提交在"松开左键"那一刻，见下）。
        #   `_ruler_drag` / `_pan` 已在上面处理（它们可由左/中键发起），到这里的只可能是
        #   `_resize` / `_trim` / `_drag`（均由**左键按下**发起）⇒ 非左键一律忽略。
        if e.button() != Qt.LeftButton:
            return
        if self._resize is not None:
            rs = self._resize
            self._resize = None
            self.setCursor(Qt.ArrowCursor)                 # ★ #9 复位
            if rs.get("moved"):
                pa, pe = rs.get("preview", (rs["at0"], rs["end0"]))
                self.resized.emit(rs["lyr_id"], rs["item"].item_id, float(pa),
                                  None if pe is None else float(pe))
            self.update()
            return
        if self._trim is not None:
            # ★ 需求 3：松手**一次**发 `trimmed`（签名冻结，见设计文档 §3.3）
            ts = self._trim
            self._trim = None
            self.setCursor(Qt.ArrowCursor)                 # ★ #9 复位
            if ts.get("moved") and ts.get("preview") is not None:
                at, end, src_in, src_out = ts["preview"]
                self.trimmed.emit(ts["lyr_id"], ts["item"].item_id,
                                  float(at),
                                  None if end is None else float(end),
                                  float(src_in),
                                  None if src_out is None else float(src_out))
            self.update()
            return
        d = self._drag
        self._drag = None
        self._preview_dx = 0.0
        self._snap_guide = None                            # ★ #14：清吸附导引线
        # ★ 第六轮（CDR）：左键松手 = **提交**。
        #   · 复制模式（拖动中按过右键）⇒ 产一份副本（**原件留在按下位置**），**不发 moved**；
        #   · 提交后**自动退出复制模式**（无需再按一次右键取消）。
        was_dup = bool(self._dup_mode)
        self._dup_mode = False
        self.setCursor(Qt.ArrowCursor)                     # ★ #9/#18：光标回箭头
        if not d:
            self.update()
            return
        # ★ 第九轮：整组拖动/再制提交（members 存在 ⇒ 多选分支）
        members = d.get("members")
        if members is not None:
            anchor = d["item"]
            target = d.get("target")
            if target is None:
                mx = self._last_mx if self._last_mx is not None else self._x(float(anchor.at_sec))
                target = max(0.0, self._sec(mx + d.get("dx", 0.0)))
            delta = float(target) - float(d.get("anchor_at0", float(anchor.at_sec)))
            if d.get("moved") or was_dup:
                entries = [(lyr.lyr_id, m.item_id, max(0.0, float(at0) + delta))
                           for (lyr, m, at0) in members]
                if was_dup:
                    self.group_duplicate_requested.emit(entries)
                else:
                    self.group_moved.emit(entries)
            else:
                # ★ 多选成员上"只按不动" ⇒ 视作单击该成员 ⇒ **收敛为单选**
                self.sel_multi.clear()
                self.sel = (d.get("lyr_id", ""), anchor.item_id)
                self.selection_count_changed.emit(1)
            self.update()
            return
        it = d["item"]
        # ★ 落点：优先用拖动中算好的（含 **#14 吸附**）`target`；否则按"以拖动距离为准"算。
        nb = d.get("target")
        if nb is None:
            mx = self._last_mx if self._last_mx is not None else self._x(it.at_sec)
            new_x = mx + d.get("dx", 0.0)
            nb = max(0.0, self._sec(new_x))
        nb = max(0.0, float(nb))
        # ★ 复制模式提交：副本落"**松开左键的位置**"，原件**不动**（不发 `moved`）
        if was_dup:
            self.duplicate_requested.emit(d["lyr_id"], it.item_id, float(nb))
            self.update()
            return
        if not d.get("moved"):
            self.update()
            return
        rows = self._layers()
        tgt = self._drop_row
        if 0 <= tgt < len(rows) and rows[tgt].lyr_id != d["lyr_id"]:
            self.layer_moved.emit(d["lyr_id"], it.item_id, rows[tgt].lyr_id, float(nb))
            self.update()
            return
        self.moved.emit(d["lyr_id"], it.item_id, float(nb))
        self.update()
