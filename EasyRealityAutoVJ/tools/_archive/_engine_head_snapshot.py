# -*- coding: utf-8 -*-
"""自动 VJ 决策引擎 + 多图层合成渲染

视觉行为模式（四种）：
- slow    慢切：每 8~14 拍切一次，长过渡。
- normal  常规切：每 8~16 拍切一次，中等过渡。
- fast    快切：每 4~8 拍切一次，短过渡。
- beat    逐拍交替：每拍 ABAB 交替（保留一个换一个、随机池防重复）。

自动模式：综合低频能量/瞬态密度/BPM 稳定性/节拍清晰度/频谱复杂度/
段落标签/能量趋势/人声占比 每秒打分，选最高分模式；手动模式可强制固定。
"""
import math
import os
import random
import threading
import time

import numpy as np

from PySide6.QtCore import QObject, QTimer, Signal, Qt, QRectF, Slot, QMetaObject
from PySide6.QtGui import QImage, QPainter, QColor

from media_manager import make_player
from match_engine import match_clips
from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,
                      DYNAMIC_FLICKER, DYNAMIC_TAGS)


_LOCK_TIMEOUT = 2.0     # GUI 入口等引擎锁的上限（正常等一帧 <50ms，2 秒已极宽容）


def _locked(fn):
    """GUI 线程入口调用与 worker 线程主循环串行化（引擎已 moveToThread）。

    ⚠⚠ **必须带超时**（2026-09-26 现场事故，用户原话「无响应了就一直卡死，直到重新打开」）：
    这把锁由引擎线程在**整帧渲染期间**持有（`_tick` → `with self._tick_lock: _tick_locked()`）。
    一旦引擎那一帧卡住不返回，锁就**永不释放**；而这里装饰的全是 GUI 按钮路径
    （`toggle_run` / `next_scene` / `manual_transition` / `close` …），主线程一碰就
    **永久等待** —— 表现正是 Windows 的「未响应」而且**永远不恢复**。
    带超时之后：最坏情况只是"这一次操作没生效"（日志里会记），**界面继续能操作、
    还能正常关窗口**，不用强制结束进程。
    """
    def wrapper(self, *a, **kw):
        lock = self._tick_lock
        got = False
        try:
            got = lock.acquire(timeout=_LOCK_TIMEOUT)
        except Exception:                                        # noqa: BLE001
            got = False
        if not got:
            try:
                import stallwatch
                stallwatch.log_line(
                    "★ 引擎锁获取超时（%.1f 秒未拿到）：%s 这次没执行 —— "
                    "说明引擎那一帧卡住了（界面仍可操作，不用强杀进程）"
                    % (_LOCK_TIMEOUT, fn.__name__))
            except Exception:                                    # noqa: BLE001
                pass
            return None
        try:
            return fn(self, *a, **kw)
        finally:
            try:
                lock.release()
            except Exception:                                    # noqa: BLE001
                pass
    try:
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
    except Exception:                                            # noqa: BLE001
        pass
    return wrapper

# 混合模式 → Qt 合成模式（"减少"用逐像素，见 _apply_subtract）
BLEND_QT = {
    "normal": QPainter.CompositionMode_SourceOver,
    "add": QPainter.CompositionMode_Plus,
    "screen": QPainter.CompositionMode_Screen,
    "multiply": QPainter.CompositionMode_Multiply,
    "lighten": QPainter.CompositionMode_Lighten,
    "darken": QPainter.CompositionMode_Darken,
}

MODES = ("normal", "fast", "beat")
MODE_LABELS = {
    "normal": "常规切",
    "fast": "快切",
    "beat": "逐拍交替",
}
# 图层混合模式（键为配置文件值；Qt 无原生"减少"，用逐像素实现）
BLEND_MODES = ("normal", "add", "subtract", "screen", "multiply", "lighten", "darken")
BLEND_LABELS = {
    "normal": "常规",
    "add": "添加",
    "subtract": "减少",
    "screen": "屏幕",
    "multiply": "多层",
    "lighten": "变亮",
    "darken": "变暗",
}
# 每模式：切换间隔（拍，固定值）、过渡时长。
# 拍数为固定值（不再随能量在区间内插值）：素材切换时机对齐 4 拍小节线，
# 常规切 16 拍 = 4 个 4/4 小节，快切 8 拍 = 2 个 4/4 小节（用户要求踩在乐句结构点上）。
MODE_META = {
    "normal":  {"beats": (16, 16), "trans_ms": 600},
    "fast":    {"beats": (8, 8), "trans_ms": 250},
    "beat":    {"beats": (1, 1), "trans_ms": 30},
}


# 自动模式后处理强度 = 能量 × （全局强度 × AUTO_FX_RATIO）
# 0.6 =「取全局强度的六成」，是全局面板的从属系数，不是固定的绝对强度值。
AUTO_FX_RATIO = 0.6


def snap_energy(snap, default=0.5):
    """从 snapshot 取能量值（**唯一正确取法**）。

    绝不要写成 `snap.get("energy", 0.5) or 0.5`：能量算成 0.0 时 `0.0 or 0.5` = 0.5，
    会把「完全没能量」当成「中等能量」——能量跨档就永远掉不到低档、低动态池永不 roll
    （用户实测：持续 5 秒 energy=0，档位只掉到 mid 就卡住）。
    只有「键不存在 / 值为 None」才用默认值。
    """
    v = snap.get("energy", None)
    return float(default) if v is None else float(v)


class Layer:
    _uid = 0

    def __init__(self, name):
        Layer._uid += 1
        self.name = name
        self.visible = True
        self.opacity = 1.0
        self.blend = "normal"    # 混合模式
        self.speed = 1.0         # 图层播放速度倍率
        self.use_alpha = True    # 保留素材自带 alpha 通道（关闭=透明区域填黑当实底）
        self.lock_motion = False  # 锁定画面：不随能量脉冲缩放、不漂移（logo/字幕用）
        self._alpha_key = None
        self._alpha_img = None
        self.img_scale = 1.0     # 画面缩放（图片/素材通用）
        self.img_x = 0.0         # 水平位移（画布宽度的百分比 -100~100）
        self.img_y = 0.0         # 垂直位移（画布高度的百分比）
        self.pause_silent = False  # 静音时暂停播放（保持当前画面）
        self.hide_silent = False  # 无声音时隐藏本图层内容
        self.solo = False        # 独奏：只显示本图层
        self.clips = []          # [MediaItem]（手动素材）
        self.is_kv = False       # Kv 主视觉图层（待机层）：无声音时切入、有声音时切出；全局唯一且默认置顶
        self.role = "bg"         # 图层角色：fg 前景 / bg 背景（自动匹配用）
        self.auto_mode = False   # 自动模式：按曲风自动挑素材
        self.auto_clips = []     # [MediaItem]（自动模式挑的素材，独立于手动 clips）
        self.fixed = -1          # -1 自动；>=0 固定播放的素材索引
        self.next_est = None     # 预估的下一个素材（供预览区"下一个素材预看"）
        # 运行时
        self.cur = -1
        self.prev_idx = -1       # 过渡中的上一个素材索引（供高亮）
        # Kv 待机层运行时状态（仅在 is_kv 图层上使用）
        self.kv_on = 0.0         # 当前显示强度 0~1（0=完全切出，1=完全覆盖）
        self.kv_silent_sec = 0.0  # 连续静音累计（按 Kv 阈值判定）
        self.kv_loud_sec = 0.0    # 连续有声累计
        self.kv_trans_active = False
        self.kv_trans_start = 0.0
        self.kv_trans_dur = 0.0
        self.kv_trans_from = 0.0
        self.kv_trans_to = 0.0
        self.kv_trans_kind = "fade"   # fade / cut / glitch / zoom
        self.kv_switch_t = 0.0    # 素材轮换计时（0=未开始；见 cfg["kv"]["switch_sec"]）
        self.player = None
        self.prev_player = None
        self.trans_start = 0.0
        self.trans_active = False
        self.trans_ms = None     # 切换时的过渡时长（None=用当前模式的）
        self.last_switch_t = 0.0
        self.recent = []         # 防重复队列
        self.cache = {}          # path -> player（解码器复用缓存，LRU 上限 8）
        # 预热（切素材零延迟）：切换后延迟一会儿把「下一个要用的素材」的容器打开+首帧解出来。
        # 不预热的话切换瞬间新解码器还要 70~690ms 才有首帧（= 用户看到的"卡几帧"）。
        self.pre_idx = -1        # 预选的下一个素材索引
        self.warm_pend = False   # 是否已安排预热
        self.warm_at = 0.0       # 预热执行时刻（错开切换瞬间，避免和正在解码的素材抢 CPU）
        # 逐拍交替状态
        self.pair = None         # (idx_a, idx_b)
        self.pair_turn = 0
        self.beats_in_pair = 0
        self.rand_pool = None
        self.order_dir = 1
        self.order_i = 0

    def close_all(self):
        """关闭本图层所有解码器（图层删除/重建时调用）"""
        if self.player:
            self.player.close()
        if self.prev_player:
            self.prev_player.close()
        for p in self.cache.values():
            if p is not self.player and p is not self.prev_player:
                p.close()
        self.cache.clear()
        self.player = None
        self.prev_player = None

    def to_dict(self):
        return {"name": self.name, "visible": self.visible, "opacity": self.opacity,
                "blend": self.blend, "solo": self.solo,
                "speed": self.speed, "pause_silent": self.pause_silent,
                "use_alpha": self.use_alpha, "lock_motion": self.lock_motion,
                "img_scale": self.img_scale, "img_x": self.img_x, "img_y": self.img_y,
                "fixed": self.fixed, "hide_silent": self.hide_silent,
                "role": self.role, "auto_mode": self.auto_mode, "is_kv": self.is_kv,
                "auto_clips": [c.path for c in self.auto_clips],
                "clips": [c.path for c in self.clips]}


class AutoVJEngine(QObject):
    # ⚠⚠ **只投递一个 int 序号，帧本身放在 `self._latest_frame`**（2026-09-26 内存雪崩修复）。
    # 旧写法是 `Signal(QImage)`，每帧把一个 8MB 的 1080p 副本投进主线程队列
    # （引擎在独立线程 ⇒ 自动 QueuedConnection）。主线程一旦卡一下（例如切素材那 3 秒），
    # 队列就开始积压：30fps × 8MB = 240MB/秒。实测 **30 秒涨 3.2GB**，
    # 一路涨到 15GB 把整机拖垮（内存耗尽 → 建线程失败 → 界面"无响应"）。
    # 现在队列里堆的只是一个 int，堆多少都无所谓；绘制端自己去取最新帧。
    frame_ready = Signal(int)
    auto_layers_changed = Signal(object)   # [layer_index, ...] 自动图层素材池被重算

    def __init__(self, config, audio_state, media_library, parent=None):
        super().__init__(parent)
        self.cfg = config
        self.audio = audio_state
        self.library = media_library
        self.layers = []
        self.running = False
        self.blackout = False
        self.freeze = False
        self.pause_auto = False
        self.locked = False
        self.rng = random.Random()
        self._last_bar_count = 0
        self._last_beat_count = 0
        # 自动匹配（听歌识曲→自动配画面）
        self._song_genre_lookup = {}   # song_id -> {"genres":[...], ...}（指纹识别歌的精确曲风）
        self._last_genres = None       # 上次生效的曲风元组（用于曲风变化检测）
        self._auto_refresh_t = 0.0     # 上次自动匹配重算时间
        # 后处理特效：事件触发时间（特效名 -> 上次 Drop 触发时刻）
        self._postfx_trig = {}
        self._frame_i = 0
        # 自动后处理「效果保持」：选中一组效果后保持 N 拍，到点/曲风变化才重挑
        self._fx_hold_pick = None
        self._fx_hold_due = 0.0
        self._fx_hold_genres = None
        # Spout 输出（懒加载，运行在引擎线程）
        self._latest_frame = None      # 最新合成帧（frame_ready 只投序号，帧放这）
        self._frame_seq = 0            # 帧序号（绘制端用来去重）
        self._spout = None
        self._spout_fail = False
        # NDI 输出（懒加载，音画同步；缺 Runtime 时静默禁用）
        self._ndi = None
        self._ndi_fail = False
        # 行为模式
        self.mode = "normal"
        self.mode_scores = {}
        self._mode_eval_t = 0.0
        self.switch_pos = 0.0
        self._next_due = None   # 已安排的下一次切换目标拍位（缓存：模式切换不重置，倒计时连续）
        self._due_off = 0.0     # 排 _next_due 时用的小节头相位（相位变了要重新贴线）
        self._bar_off_user = None   # 用户按「下一素材」手动重定义的小节头相位（None=用音频识别的）
        self._pos_ref = None    # 上次切换时的连续拍位（非逐拍模式）
        # 能量跨档即时切换：能量从高(>0.6)跌到低(<0.4)或反向时，不等拍位立即换素材动态档
        self._last_energy_tier = "mid"   # high / mid / low
        self._tier_since = None          # 新档位首次出现时刻（防抖）
        self.canvas = QImage(1280, 720, QImage.Format_RGB32)
        self.canvas.fill(Qt.black)
        # 颜色渲染器（一键调色）。状态机在 colorfx.ColorRenderer 里，每帧 tick 一次；
        # 它只在「颜色/强度」变化时重建 LUT，每帧只做一次查表（~1.7ms @1080p）。
        try:
            from colorfx import ColorRenderer
            self.color_fx = ColorRenderer()
        except Exception:
            self.color_fx = None
        self._flicker_latch = {}   # 「随机休眠」模式下当前素材的休眠决定（换素材才重掷）
        # 引擎主循环移入独立线程后，用锁串行化 GUI 线程的少量入口调用
        self._tick_lock = threading.RLock()

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)

    def set_canvas_size(self, w, h):
        """画布尺寸（输出分辨率）。GUI 线程直接调用，锁内重建，与 worker 的合成串行化。
        之前 GUI 线程直接赋值 engine.canvas 会与 worker 写画布竞态（闪退/尺寸失效根因）。"""
        w = max(160, min(7680, int(w)))
        h = max(120, min(4320, int(h)))
        with self._tick_lock:
            self.canvas = QImage(w, h, QImage.Format_RGB32)
            self.canvas.fill(Qt.black)

    # ---- 线程（由 ui_main 把引擎 moveToThread 到独立线程后调用） ----
    def frame_interval_ms(self):
        """主循环间隔（毫秒）—— 读 `cfg["perf"]["render_fps"]`，钳到 15~120fps。

        为什么做成可配：素材大多是 25~30fps，而按 60fps 合成时**有一半左右是把同一帧
        重复合成一遍** —— 在 4 核老机器上是纯浪费（用户目标是把 CPU 让给 VDJ / 直播）。
        """
        try:
            fps = int((self.cfg["perf"] or {}).get("render_fps", 60) or 60)
        except Exception:
            fps = 60
        fps = max(15, min(120, fps))
        return max(1, int(round(1000.0 / fps)))

    def set_render_fps(self, fps):
        """运行时改渲染帧率（立即生效，不用重启）。

        ⚠⚠ **必须投递到引擎线程去改**：QTimer 只能在它所属线程里操作，
        从主线程（UI 回调）直接 `timer.setInterval()` 会让 Qt 先停表再按新间隔重启，
        而重启发生在错误的线程 → **定时器被永久停掉**（主循环死掉、画面全黑、CPU 近乎为零）。
        实测复现：tools/eco_diag.py 里 `timer.isActive()` 从 True 变成 False。
        """
        try:
            if hasattr(self.cfg, "set"):
                self.cfg.set("perf", "render_fps", int(fps))
            else:
                self.cfg.data.setdefault("perf", {})["render_fps"] = int(fps)
        except Exception:
            pass
        try:
            QMetaObject.invokeMethod(self, "_apply_render_fps", Qt.QueuedConnection)
        except Exception:
            pass

    @Slot()
    def _apply_render_fps(self):
        """在**引擎线程**里按新间隔（重新）启动主循环。

        直接用 `timer.start(ms)`：定时器已在跑时它会先停再按新间隔启动，
        正好同时覆盖「改间隔」和「万一被停掉了要恢复」两种情况。
        """
        try:
            self.timer.start(self.frame_interval_ms())
        except Exception:
            pass

    @Slot()
    def start(self):
        """在引擎所属线程里启动主循环（QTimer 必须在所属线程启动）"""
        if not self.timer.isActive():
            self.timer.start(self.frame_interval_ms())

    @Slot()
    def stop(self):
        self.timer.stop()

    # ---------------- 图层管理 ----------------
    @_locked
    def rebuild_from_cfg(self):
        from media_manager import MediaItem
        for old in self.layers:
            old.close_all()
        self.layers = []
        for ld in self.cfg["layers"]:
            lay = Layer(ld.get("name", "Layer"))
            lay.visible = ld.get("visible", True)
            lay.opacity = float(ld.get("opacity", 1.0))
            lay.blend = ld.get("blend", "normal") or "normal"
            lay.solo = bool(ld.get("solo", False))
            lay.speed = float(ld.get("speed", 1.0) or 1.0)
            lay.pause_silent = bool(ld.get("pause_silent", False))
            lay.use_alpha = bool(ld.get("use_alpha", True))
            lay.lock_motion = bool(ld.get("lock_motion", False))
            lay.img_scale = float(ld.get("img_scale", 1.0) or 1.0)
            lay.img_x = float(ld.get("img_x", 0.0) or 0.0)
            lay.img_y = float(ld.get("img_y", 0.0) or 0.0)
            lay.fixed = int(ld.get("fixed", -1))
            lay.hide_silent = bool(ld.get("hide_silent", False))
            lay.role = ld.get("role", "bg") or "bg"
            lay.auto_mode = bool(ld.get("auto_mode", False))
            lay.is_kv = bool(ld.get("is_kv", False))
            lay.auto_clips = []
            for p in ld.get("auto_clips", []):
                m = self.library.get(p)
                if m is None:
                    m = MediaItem(p)
                    self.library[p] = m
                lay.auto_clips.append(m)
            lay.clips = []
            for p in ld.get("clips", []):
                m = self.library.get(p)
                if m is None:
                    m = MediaItem(p)
                    self.library[p] = m
                lay.clips.append(m)
            self.layers.append(lay)
        self._switch_layer_to(self.layers[0], 0, instant=True) if self.layers else None

    @_locked
    def push_layers_to_cfg(self):
        self.cfg.data["layers"] = [l.to_dict() for l in self.layers]

    # ---------------- 自动匹配（听歌识曲→自动配画面） ----------------
    @_locked
    def set_song_genres(self, lookup):
        """注入 song_id → {"genres":[...], ...} 映射（指纹识别到歌时取精确曲风）。ui_main 在曲库就绪后调用。"""
        self._song_genre_lookup = lookup or {}

    def _current_genres(self, snap):
        """当前生效曲风（英文原词）：指纹识别歌的精确曲风优先，兜底实时本地AI曲风"""
        sid = snap.get("recognized_song_id", -1)
        if sid is not None and sid >= 0 and self._song_genre_lookup:
            meta = self._song_genre_lookup.get(sid)
            if meta:
                g = meta.get("genres") or []
                if g:
                    return g
        return list(snap.get("genre_tags") or [])

    @_locked
    def _refresh_auto_layers(self, snap, force_tier=None):
        """曲风变化时（或能量跨档 force_tier 时）用 match_clips 重新挑素材池并立即切换。

        force_tier: None=常规（曲风变化触发，带门控）；"low"/"high"=能量跨档重 roll，
        跳过曲风门控，池子按动态档过滤/前置（match_clips 的 tier 参数）。"""
        genres = self._current_genres(snap)
        key = tuple(sorted(set(g.lower() for g in genres if g)))
        now = time.perf_counter()
        if force_tier is None:
            if not key:
                self._last_genres = key      # 无曲风（静音/未识别）：不重算，保持现状
                return
            if key == self._last_genres or now - self._auto_refresh_t < 3.0:
                return
        self._last_genres = key
        self._auto_refresh_t = now
        energy = snap_energy(snap)   # 注意：0.0 就是 0.0，不能当成 0.5（见 snap_energy 注释）
        changed = []
        for lay in self.layers:
            if not getattr(lay, "auto_mode", False):
                continue
            # 记住当前正在播的素材：曲风变化换池时保留画面，等切换点（小节线）再切
            cur_path = lay.clips[lay.cur].path if (0 <= lay.cur < len(lay.clips)) else None
            picked = list(match_clips(genres, energy, self.library, role=lay.role, n=8,
                                      tier=force_tier))
            lay.clips = list(picked)
            lay.auto_clips = list(picked)
            lay.fixed = -1
            lay.recent = []
            lay.pair = None
            lay.pair_turn = 0
            lay.rand_pool = None
            lay.order_i = 0
            lay.order_dir = 1
            if not picked:
                # 素材库已没有该角色的素材：清空画面（不再保留已删除素材的残留）
                if lay.player:
                    lay.player.close()
                    lay.player = None
                lay.prev_player = None
                lay.cur = -1
                lay.prev_idx = -1
                changed.append(self.layers.index(lay))
                continue
            # 切换时机：
            # - 能量跨档（force_tier 非空）：立即切（响应速度优先，用户明确要求）
            # - 曲风变化但当前还没画面：先放一个（避免黑屏）
            # - 纯曲风变化且正在播：只换池、保留当前画面，等下一个切换点（小节线）再切 → 对齐
            if force_tier is not None or lay.player is None or lay.cur < 0:
                lay.cur = -1
                lay.prev_idx = -1
                self._switch_layer_to(lay, 0, instant=True)
            else:
                # 当前素材仍在新池 → 保持索引（画面不断）；已不在新池 → -1（画面暂留旧 player，
                # 由 _auto_switch_tick 的下一个小节线切换点换成新池素材）
                keep = -1
                if cur_path is not None:
                    for i, c in enumerate(picked):
                        if c.path == cur_path:
                            keep = i
                            break
                lay.cur = keep
                lay.prev_idx = -1
            changed.append(self.layers.index(lay))
        if changed:
            self.auto_layers_changed.emit(changed)

    def _energy_tier_switch(self, snap):
        """能量跨档即时切换：能量从高(>0.6)跌到低(<0.4)或反向时，立即换素材动态档。

        之前只靠拍位切换点(_next_switch_due 对齐 4 拍小节线)换素材，切换间隔 8~32 拍，
        能量变了要等十几秒才换到对应动态档。这里在能量跨档、新档位稳定 1.5s 后（防抖），
        立即对所有非固定图层切一次，让「高潮→高动态 / 缓和→低动态」跟上能量。"""
        E = snap_energy(snap)   # 0.0 必须当成 0.0（旧写法 `or 0.5` 会让档位卡在中档）
        # 这里**刻意不做静音门控**：档位必须照常评估，否则「能量算成 0」时档位永远停在
        # 上一档，低动态池一辈子 roll 不出来（用户反馈：能量为 0 时不 roll 低动态池）。
        # 本方法只在新档位稳定 2 秒后触发一次（滞回 + 稳定期），所以最多换一次池，不会 churn。
        # 滞回式三段判定（防阈值附近频繁横跳 → 素材池切换"太灵敏"）：
        # 三段划分 0~0.5 低 / 0.5~0.65 中 / 0.65+ 高，但进出阈值不对称，留出滞回带：
        #   进 high 要 >=0.70，退回 mid 要 <0.58；进 low 要 <0.45，退回 mid 要 >0.55。
        # 能量在 0.5/0.65 附近小幅波动时不会反复跨档（之前纯三段一刀切会来回切池子）。
        cur = self._last_energy_tier
        if cur == "high":
            tier = "mid" if E < 0.58 else "high"
        elif cur == "low":
            tier = "mid" if E > 0.55 else "low"
        else:  # mid
            tier = "high" if E >= 0.70 else ("low" if E < 0.45 else "mid")
        if tier == self._last_energy_tier:
            self._tier_since = None
            return
        now = time.perf_counter()
        if self._tier_since is None:
            self._tier_since = now
            return
        if now - self._tier_since < 2.0:   # 新档位稳定 2.0s 才确认（配合滞回，降低跨档频率）
            return
        self._last_energy_tier = tier
        self._tier_since = None
        # 立即切换：重置切换基准点
        self.switch_pos = self._beat_pos(snap)
        self._next_due = None
        # 自动图层：跨档即重新 roll 一批符合新动态档的素材池（match_clips tier 过滤 +
        # 立即切换），而不是在旧池子里挑——否则池里大部分不符合新档，几个素材来回切
        if tier in ("low", "high"):
            self._refresh_auto_layers(snap, force_tier=tier)
        # 手动图层：在现有池里按动态档挑一个
        for lay in self.layers:
            if lay.fixed >= 0 or not lay.clips or self.locked:
                continue
            if getattr(lay, "auto_mode", False):
                continue    # 自动图层上面已重 roll + instant 切
            if getattr(lay, "is_kv", False):
                continue    # Kv 待机层不受能量档位影响
            self._switch_layer_random(lay, snap)

    # ---------------- 主循环 ----------------
    _err_log_n = 0

    def _tick(self):
        # 主循环已移入独立线程；与 GUI 线程的入口调用（rebuild/切换等）用锁串行化
        try:
            with self._tick_lock:
                self._tick_locked()
        except Exception:
            # worker 线程异常在 windowed exe 下不可见：写日志文件便于排查黑屏类问题
            import traceback
            from config import app_base_dir
            AutoVJEngine._err_log_n += 1
            if AutoVJEngine._err_log_n <= 5:
                try:
                    with open(os.path.join(app_base_dir(), "engine_error.log"),
                              "a", encoding="utf-8") as f:
                        f.write("[%s]\n%s\n" % (time.strftime("%H:%M:%S"),
                                                traceback.format_exc()))
                except Exception:
                    pass

    def _tick_locked(self):
        snap = self.audio.snapshot()
        if self.freeze:
            return  # 冻结：画面完全不变
        # 注意：输出/预览始终实时渲染（不受开始-停止影响）；停止只停止"自动切换"

        beat_event = snap["beat_count"] != self._last_beat_count
        self._last_bar_count = snap["bar_count"]
        self._last_beat_count = snap["beat_count"]

        # 长静音（>3s，跟音频侧的"换歌"判据一致）后手动重定义的小节头失效：
        # 换歌了，用户手动对齐的相位不再代表新歌的小节线，交回给音频识别的 downbeat。
        if self._bar_off_user is not None and snap.get("silent_sec", 0.0) > 3.0:
            self._bar_off_user = None
            self._next_due = None

        # 行为模式：每秒评估一次（自动打分或手动强制）
        if self.running:
            self._update_mode(snap)

        # 自动匹配：曲风变化时重算自动图层素材池
        self._refresh_auto_layers(snap)

        # 能量跨档即时切换：能量从高/低跨过阈值（带防抖）时立即换素材动态档，
        # 不等到下一个拍位切换点（否则能量变了十几秒素材动态还换不过来）。
        if self.running and not self.pause_auto and self.mode != "beat":
            self._energy_tier_switch(snap)

        # BPM 速度同步
        if self.cfg["auto"].get("bpm_speed_sync") and snap["bpm"] > 0:
            speed = max(0.3, min(2.5, snap["bpm"] / 128.0))
        else:
            speed = 1.0

        # 非逐拍模式的按拍位切换
        if self.running and not self.pause_auto and self.mode != "beat":
            self._auto_switch_tick(snap)

        # 逐拍交替（仅当模式为 beat；相位驱动，每帧节流在内部完成）
        if self.running and not self.pause_auto and self.mode == "beat":
            self._beat_tick(snap)

        # 预热：把「下一个要切的素材」提前打开容器+解首帧（切到时零延迟，见 warm_up）
        self._process_warms(snap)

        # Kv 待机层：静音切入 / 有声切出（独立于所有其它设置，不受 pause_auto 影响）
        if self.running and not self.locked:
            self._kv_tick(snap)

        # 视频速度 + 活跃标记（缓存中未显示的解码器挂起省 CPU）
        silent_now = snap["silent_sec"] > 0.25 or snap["level"] < 0.03
        for lay in self.layers:
            for p in lay.cache.values():
                active = p is lay.player or p is lay.prev_player
                # 静音时暂停：保持当前画面不动（解码线程挂起，最后一帧仍在显示）
                # 仅在 VJ 运行中生效；停止时预览要保持实时，不能挂起
                if active and lay.pause_silent and silent_now and p is lay.player and self.running:
                    active = False
                try:
                    p.set_active(active)
                except Exception:
                    pass
                # Kv 待机层不受 BPM 速度同步影响（保持原速播放）
                lsp = 1.0 if getattr(lay, "is_kv", False) else speed
                p.set_speed(max(0.1, min(4.0, lsp * float(getattr(lay, "speed", 1.0) or 1.0))))

        # 合成
        self._compose(snap)
        # 颜色渲染（一键调色）：放在后处理之前，让辉光/色差等特效吃到调色后的画面
        self._apply_color(snap)
        # 后处理特效链（合成后、输出前）
        self._apply_postfx(snap)
        # 深拷贝一帧再发射：GUI 只读这份独立数据，worker 不再碰它，彻底消除跨线程抢画布竞态
        w, h = self.canvas.width(), self.canvas.height()
        frame = QImage(w, h, QImage.Format_RGB32)
        frame.fill(0)
        try:
            np.copyto(np.frombuffer(frame.bits(), np.uint8, frame.sizeInBytes()),
                      np.frombuffer(self.canvas.constBits(), np.uint8, self.canvas.sizeInBytes()))
        except Exception:
            frame = self.canvas.copy()
        # ★ 只保留最新帧 + 投递一个轻量序号：队列里再也不会堆 8MB 的图
        self._latest_frame = frame
        self._frame_seq += 1
        self.frame_ready.emit(self._frame_seq)
        self._spout_send()
        self._ndi_send()

    def latest_frame(self):
        """取最近一帧合成结果（跨线程只读）。

        QImage 是隐式共享的，`_tick_locked` 每帧都**新建**一个对象再替换引用，
        所以绘制端拿到的是当时的快照，引擎随后替换不会影响它 —— 无需加锁。
        """
        return self._latest_frame

    def _spout_send(self):
        """Spout 输出：CPU 共享内存路径把画布帧发给本机其他程序（OBS/Resolume 等）。
        懒加载 + 失败静默禁用（未装 SpoutGL 时输出功能不受影响）。"""
        oc = self.cfg["output"]
        if not oc.get("spout_enabled"):
            return
        if self._spout_fail:
            return
        try:
            if self._spout is None:
                from SpoutGL import SpoutSender
                from SpoutGL.enums import GL_BGRA_EXT
                s = SpoutSender()
                s.setSenderName(oc.get("spout_name") or "EasyRealityAutoVJ")
                s.createOpenGL()
                self._spout = (s, GL_BGRA_EXT)
            import numpy as _np
            s, glfmt = self._spout
            w, h = self.canvas.width(), self.canvas.height()
            bpl = self.canvas.bytesPerLine()
            buf = _np.frombuffer(self.canvas.bits(), dtype=_np.uint8, count=bpl * h)
            s.sendImage(buf, h, w, glfmt, True, 0)
        except Exception as e:
            self._spout_fail = True
            print("Spout send failed:", e)

    def _ndi_send(self):
        """NDI 输出：把画布帧喂给 NDI 发送器（音频由采集线程另行喂）。缺 Runtime 静默禁用。"""
        oc = self.cfg["output"]
        if not oc.get("ndi_enabled"):
            return
        if self._ndi_fail:
            return
        try:
            from ndi_out import get_ndi
            import numpy as _np
            w, h = self.canvas.width(), self.canvas.height()
            if self._ndi is None:
                self._ndi = get_ndi(oc.get("ndi_name") or "EasyRealityAutoVJ")
                # ⚠ 必须带**真实分辨率**：发送端的 VideoSendFrame 只能在 open() 之前定死尺寸，
                #   所以 sender 是按第一次发帧的尺寸建立的。旧代码这里不给尺寸 → 直接返回 False
                #   → `_ndi_fail` 被永久置位 → NDI 输出再也不会尝试（静默失效）。
                if not self._ndi._ensure(w, h):
                    self._ndi_fail = True
                    return
            bpl = self.canvas.bytesPerLine()
            bgra = _np.frombuffer(self.canvas.bits(), dtype=_np.uint8, count=bpl * h)
            # 传给 NDI 的是 BGRA 连续缓冲（画布 RGB32 = BGRA 内存序）
            self._ndi.send_video(bgra, w, h)
        except Exception as e:
            self._ndi_fail = True
            print("NDI send failed:", e)

    # ---------------- 行为模式 ----------------
    def manual_mode(self):
        m = self.cfg["mode"].get("manual", "auto")
        # 已删除「慢切」「静止/氛围」模式：旧配置若存了 slow/ambient，回退到自动，
        # 避免 MODE_META KeyError（旧配置迁移）。
        if m in (None, "auto") or m not in MODES:
            return None
        return m

    def _update_mode(self, snap):
        now = time.perf_counter()
        manual = self.manual_mode()
        if manual:
            self.mode = manual
            return
        if now - self._mode_eval_t < 1.0:
            return
        self._mode_eval_t = now
        scores = self._score_modes(snap)
        scores.pop("beat", None)   # 自动模式不选「逐拍交替」（逐拍只能手动选择）
        self.mode_scores = scores
        best = max(scores, key=scores.get)
        # 迟滞：新模式需明显高于当前才切换，避免频繁跳模式
        if best != self.mode and scores[best] > scores.get(self.mode, -9) + 0.25:
            self.mode = best
            # 模式切换不重置切换基准点：已安排的下一次切换目标（_next_due）保持不变，
            # 新模式的节奏只影响「切换之后」的下一次间隔——倒计时连续，不从新模式的
            # 满间隔重新数（用户反馈：快切数到 2 突然变慢切又跳到 30，体验割裂）。

    def _score_modes(self, snap):
        E = snap["energy"]
        OD = min(1.0, snap["onset_density"] / 4.0)          # 瞬态密度归一（每秒强拍数，drop≈4）
        BS = snap["bpm_stab"]                                # BPM 稳定性
        BC = snap["beat_clarity"]                            # 节拍清晰度
        CX = snap["spec_complex"]                            # 频谱复杂度
        TR = max(-1.0, min(1.0, snap["energy_trend"]))       # 能量趋势
        VC = snap["vocalness"]                               # 人声占比
        seg = snap["segment"]

        def seg_w(label, w):
            return w if seg == label else 0.0

        sens = int(self.cfg["mode"].get("sensitivity", 1))
        speed_bonus = (-0.5, 0.0, 0.5)[max(0, min(2, sens))]  # 灵敏度→偏向快模式

        scores = {
            # 已删除「慢切」：安静/尾奏段直接归到常规切（16 拍，4 小节，本身就慢）
            "normal":  1.4 + 0.8 * (1 - abs(E - 0.5) * 2) + (1 - CX) * 0.3
                       + seg_w("peak", 0.3) + seg_w("quiet", 1.0) + seg_w("outro", 0.5),
            "fast":    E * 1.6 + OD * 1.0 + max(TR, 0) * 1.2 + CX * 0.3
                       + max(0.0, E - 0.7) * 1.5
                       + seg_w("build", 1.6) + speed_bonus,
            "beat":    BC * 1.4 + BS * 1.2 + OD * 0.8 + (0.6 if E > 0.55 else 0)
                       + seg_w("drop", 2.5) + speed_bonus,
        }
        # 低能量段：**常规切优先、快切按能量比例压制**。
        # 先加一个小偏置（中低能量过渡用），再对快切做**乘性**压制 —— 关键在乘性：
        # 快切在低能量时能靠 build(+1.6) + 灵敏度「高」(+0.5) + 高瞬态密度(+1.0) +
        # 能量趋势(+1.2) 叠到 4.5+，任何常数减项都压不住（用户 2026-09-24 反馈
        # 「0.2 能量还在快切」，实测正是"安静的 build"和"全加成拉满"两种组合）。
        # k 从 E=0.35 的 0 线性升到 E=0.50 的 1：E≤0.35 快切得分直接归零（低能量只用常规切），
        # E≥0.50 完全不受影响（build/drop 该快切照样快切）。
        # 门槛 0.35 是用户 2026-09-24 定的（原为 0.30，他看了实测表后要求再抬高一档）。
        low_bias = max(0.0, 0.55 - E)
        scores["normal"] += low_bias * 1.4
        scores["fast"] -= low_bias * 1.2
        k_fast = max(0.0, min(1.0, (E - 0.35) / 0.15))
        if k_fast < 1.0:
            scores["fast"] *= k_fast
            scores["normal"] += (1.0 - k_fast) * 0.9
        return scores

    def _beat_pos(self, snap):
        """连续拍位（小数）：beat_count + 拍内相位。
        鼓点/BPM 检测不可用（bpm=0 或长时间无鼓点）时，按真实时间以 120BPM 推算拍位，
        保证自动切换不会因为"拍钟不走"而永远不切换。"""
        if snap.get("bpm", 0) > 0 and snap.get("beat_active"):
            pos = snap["beat_count"] + snap.get("beat_phase", 0.0)
            self._pos_ref = (time.perf_counter(), pos)
            return pos
        now = time.perf_counter()
        ref = getattr(self, "_pos_ref", None)
        if ref is None or now - ref[0] > 8.0:
            # 长时间无鼓点：以「当前推算拍位」为新基准继续走（保持连续）。
            # 旧实现这里写成 (now, ref[1])——取的是旧基准，导致每 8 秒拍位突然跳回旧值，
            # 触发 _auto_switch_tick 的 pos<switch_pos 重置 → 切换间隔翻倍甚至切不动
            # （BPM 未锁定、走这条 120BPM 回退时钟时最明显：快切 8 拍实际变成 16 拍）。
            cur = (ref[1] + (now - ref[0]) * (120.0 / 60.0)) if ref else 0.0
            self._pos_ref = (now, cur)
            ref = self._pos_ref
        return ref[1] + (now - ref[0]) * (120.0 / 60.0)   # 回退时钟：120 BPM

    def _beat_recent(self, snap):
        """最近是否检测到鼓点活动"""
        return bool(snap.get("beat_active"))

    # ---------------- 自动切换（非逐拍模式） ----------------
    def _current_interval(self, snap):
        """当前生效的切换间隔（拍）；None 表示不自动切换。
        常规切/快切为固定拍数（16 拍 / 8 拍 = 4 / 2 个 4/4 小节），保证素材始终
        停留整数个小节、切换点踩在乐句结构线上；逐拍交替走独立的 _beat_tick 节奏。
        「素材最小切换间隔」选项已删除：间隔固定，不再被配置里的残留值覆盖。"""
        if self.mode == "beat":
            return 1.0
        meta = MODE_META[self.mode]
        lo, hi = meta["beats"]
        if lo == hi:
            return float(lo)   # 固定拍数（常规 16 / 快切 8）
        # 兼容仍有区间的模式（未来扩展）：按能量在 lo~hi 间线性插值
        TR = max(-1.0, min(1.0, snap["energy_trend"]))
        sens = int(self.cfg["mode"].get("sensitivity", 1))
        E = max(0.0, min(1.0, snap["energy"]))
        f = E + TR * 0.15 + (-0.1, 0.0, 0.1)[max(0, min(2, sens))]
        f = max(0.0, min(1.0, f))
        return hi - (hi - lo) * f   # f=1(高能)→lo；f=0(低能)→hi

    def beats_to_next_switch(self, snap):
        """距离下一次自动切换还有多少拍（供预览 HUD）。
        基于对齐到小节线的固定目标点，倒计时单调递减不跳动。"""
        if self.mode == "beat":
            return 1.0
        due = self._next_switch_due(snap)
        if due is None:
            return None
        pos = self._beat_pos(snap)
        return max(0.0, due - pos)

    def switch_countdown(self, snap):
        """HUD 用的倒计时整数（拍）：从「间隔-1」开始数，数到 0 就是下一个切换点。

        用户 2026-09-24 要求：刚切完/刚按下时显示 15 拍（快切 7 拍），而不是 16 / 8。
        **实际间隔不变**（仍是完整 16 / 8 拍、照旧踩小节线），只是这个显示按"还剩几拍"来数。
        用 `ceil(remain)-1` 而不是四舍五入：四舍五入在刚切完时会显示成 16 / 8。
        没有自动切换（逐拍模式/未开始）时返回 None。"""
        remain = self.beats_to_next_switch(snap)
        if remain is None:
            return None
        return max(0, int(math.ceil(float(remain)) - 1))

    def _bar_off(self, snap):
        """当前生效的「小节头相位」（0~3）：手动重定义优先，否则用音频识别出来的。

        「下一素材」按下时会把相位就地重定义成他按的那一拍（见 next_scene）——
        用户 2026-09-24 选的方案：**按下即第 1 拍**，之后所有切换都跟着他的拍走，
        这样既不会和自动贴小节线打架，间隔也是精确的 16 / 8 拍。
        换歌（长静音）后自动失效，回到音频识别的相位。"""
        u = self._bar_off_user
        if u is not None:
            return float(u)
        return float(snap.get("downbeat_off", 0) or 0)

    def _bar_floor(self, pos, snap):
        """拍位所在小节的起始拍位（按小节头相位对齐，floor 到当前小节）"""
        off = self._bar_off(snap)
        return off + math.floor((pos - off) / 4.0) * 4.0

    # 自动贴小节线的介入门槛（拍）：目标点离最近的小节线**不足这么多拍**时不动它。
    # 用户 2026-09-24 要求「只有明显偏移才介入」：以前任何偏移都会把切换点拉正，
    # 亚拍级的小偏差（拍钟抖动、帧粒度、手动按下的相位）也被强行拽一下 → 节奏被打断。
    # 现在只有 ≥1 拍（= 一个小节的 1/4）的错位才拉正；小偏差保持不变（它是**恒定**的、
    # 不会累积漂移，因为下一轮的目标始终是「上一轮目标 + 固定间隔」）。
    ALIGN_MIN_OFFSET = 1.0

    def _next_switch_due(self, snap):
        """下一次切换的目标拍位：**就近对齐到 4/4 小节线**（每 4 拍，按小节头相位起算）。
        目标 = 当前基准 + 固定间隔（常规 16 / 快切 8 拍，都是 4 的倍数），再四舍五入到
        最近的小节线——对不齐时最多加/减 1~2 拍把它凑到整小节上，这样素材切换始终踩在
        音乐的整小节（1-2-3-4 | 2-2-3-4 …）上，而不会切在拍子中间。
        （旧版用 ceil 向上取整：目标差 1 拍到小节线时会多等 3 拍才切，听感上就是"没对齐"。）

        小节线相位由音频侧的 downbeat（小节头）识别给出（`_bar_off()`，手动重定义优先）。
        相位识别出来/变化时，把已排好的切换点重新贴到新的小节线上（偏移 ≤2 拍，听不出跳动）。

        缓存 _next_due：模式切换不重置、能量波动不重算，倒计时稳定连续；
        只在切换发生/拍钟重置/静音/进入无间隔模式时清空重算。"""
        iv = self._current_interval(snap)
        if iv is None:
            self._next_due = None
            return None
        off = self._bar_off(snap)
        if self._next_due is None:
            target = self.switch_pos + iv
            near = off + math.floor((target - off) / 4.0 + 0.5) * 4.0
            # 只有明显偏移才拉正（见 ALIGN_MIN_OFFSET）：小偏差原样保留，不打断节奏
            self._next_due = near if abs(near - target) >= self.ALIGN_MIN_OFFSET else target
            self._due_off = off
        elif off != getattr(self, "_due_off", off):
            # 小节头相位变了：只有挪动幅度够明显才把已排好的切换点重新贴线，
            # 否则保持原目标（细碎的相位更新不再动切换点）
            near = off + round((self._next_due - off) / 4.0) * 4.0
            if abs(near - self._next_due) >= self.ALIGN_MIN_OFFSET:
                self._next_due = near
            self._due_off = off
        if self._next_due <= self.switch_pos:
            self._next_due += 4.0   # 对齐后必须前进，避免原地打转
        return self._next_due

    def _auto_switch_tick(self, snap):
        pos = self._beat_pos(snap)
        if pos < self.switch_pos:  # 拍钟被重置（重启采集/换曲归零）
            self.switch_pos = pos
            self._next_due = None
        # 【已取消】低能量（E<0.10）不切换素材的门控。
        # 用户反馈：能量突然掉到 0 之后，画面会**冻结在同一片素材上**（通常还是掉下来之前
        # 那片高动态素材），看起来就是「卡在高动态、永远不切低动态」。原因是门控直接
        # `return`，拍位切换全停，而能量跨档那次只切一次，之后整段安静期画面不再变。
        # 现在低能量照常按拍位切换：节奏自然变慢（_current_interval 在低能量取 hi 端 =
        # 常规 16 拍 / 快切 8 拍），选材由 _candidate_indices 按低能量偏好挑低动态素材。
        # ⚠ 因此下面「欠账钳位」必须保留：没有它，静音期积压的切换点在能量恢复瞬间会被
        #   逐帧追赶（实测 30s→14 次/秒、120s→50 次/秒的抽搐）。

        due = self._next_switch_due(snap)
        if due is None:
            return
        # 兜底钳位：目标点落后当前拍位超过一个完整间隔（=欠下了不止一次切换）时，
        # 直接重新排，不做"每帧追一点"的追赶——那条路径就是抽搐的来源。
        # 正常情况下 due 最多落后 2 拍（就近对齐 + 帧粒度），不会碰到这个阈值。
        if pos - due > max(4.0, self._current_interval(snap) or 4.0):
            self.switch_pos = pos
            self._next_due = None
            return
        if pos >= due:
            self.switch_pos = due
            self._next_due = None   # 切换后按最新模式/间隔重新安排下一次
            meta = MODE_META[self.mode]
            # 过渡时长：区间内微调（高能/快切时更短）
            TR = max(-1.0, min(1.0, snap["energy_trend"]))
            sens = int(self.cfg["mode"].get("sensitivity", 1))
            E = max(0.0, min(1.0, snap["energy"]))
            f = E + TR * 0.15 + (-0.1, 0.0, 0.1)[max(0, min(2, sens))]
            f = max(0.0, min(1.0, f))
            trans_ms = int(meta["trans_ms"] * (1.3 - f * 0.3))  # 区间内微调过渡时长
            for lay in self.layers:
                if lay.fixed >= 0 or not lay.clips or self.locked:
                    continue
                if getattr(lay, "is_kv", False):
                    continue    # Kv 待机层不参与按拍自动切换
                lay.trans_ms = trans_ms
                self._switch_layer_random(lay, snap)

    def _candidate_indices(self, lay, snap):
        """当前可切换的候选素材索引（不改动任何状态，供切换与"下一个素材"预估共用）"""
        cooldown = float(self.cfg["auto"].get("cooldown_sec", 20))
        now = time.perf_counter()
        idxs = list(range(len(lay.clips)))
        prefer = []
        fallback = None   # 缓和段二级兜底：避开高动态/频闪（用中动态/未标动态兜底）
        if self.cfg["auto"].get("energy_map", True):
            hi, lo, mid = [], [], []
            flash = set()
            for i, c in enumerate(lay.clips):
                tags = set(c.all_tags())
                if DYNAMIC_FLICKER in tags:
                    flash.add(i)
                if DYNAMIC_HIGH in tags or DYNAMIC_FLICKER in tags:
                    hi.append(i)
                elif DYNAMIC_LOW in tags:
                    lo.append(i)
                elif DYNAMIC_MID in tags:
                    mid.append(i)
                # 未标动态：不分类（低能量不选）
            if snap["energy"] >= 0.65 and hi:
                prefer = hi                    # 高潮段：优先高动态/频闪
            elif snap["energy"] < 0.50:
                # 缓和段：只低动态；低动态被 cooldown/recent 过滤完→中动态兜底（少选）；
                # 无低/中动态→避开高动态/频闪（未标动态做最后手段）。绝不选未标/高动态/频闪优先。
                if lo:
                    prefer = lo
                    fallback = mid
                elif mid:
                    prefer = mid
                    fallback = None
                else:
                    avoid = set(hi)
                    prefer = [i for i in idxs if i not in avoid]
                    fallback = None
            else:
                # 中能量（0.5~0.65）：排除频闪（高动态仍可参与），频闪只在高潮段出现
                prefer = [i for i in idxs if i not in flash]
        cands = prefer or idxs
        cands = [i for i in cands if i != lay.cur
                 and (lay.clips[i].path not in lay.recent or now - lay.last_switch_t > cooldown)]
        if not cands and fallback:
            cands = [i for i in fallback if i != lay.cur
                     and (lay.clips[i].path not in lay.recent or now - lay.last_switch_t > cooldown)]
        if not cands:
            cands = [i for i in idxs if i != lay.cur]
        return cands

    def next_clip_for(self, lay):
        """预估下一个将播放的素材（供预览区"下一个素材预看"，不消耗随机状态）"""
        if not lay or not lay.clips:
            return None
        if lay.fixed >= 0 and 0 <= lay.fixed < len(lay.clips):
            return lay.clips[lay.fixed]
        if self.mode == "beat" and lay.pair and len(lay.clips) >= 2:
            nxt = lay.pair[lay.pair_turn ^ 1]
            if 0 <= nxt < len(lay.clips):
                return lay.clips[nxt]
        cands = self._candidate_indices(lay, self.audio.snapshot())
        if not cands:
            return None
        # 稳定选择：每 2 秒换一次预估，避免预览小窗闪烁
        pick = cands[int(time.perf_counter() / 2.0) % len(cands)]
        return lay.clips[pick]

    def _switch_layer_random(self, lay, snap):
        idxs = list(range(len(lay.clips)))
        if not idxs:
            return
        cands = self._candidate_indices(lay, snap)
        if not cands:
            return
        # 优先用「已经预热好的那个」——预热过的解码器首帧在手，切过去零延迟；
        # 若它已经不在候选里（能量跨档/曲风重算换池/被 cooldown 排除）则重新摇。
        pick = lay.pre_idx if lay.pre_idx in cands else -1
        if pick < 0:
            seed = int(self.cfg["auto"].get("seed") or 0)
            rng = random.Random(seed) if seed else self.rng
            pick = rng.choice(cands)
        self._switch_layer_to(lay, pick)
        lay.pre_idx = -1
        self._schedule_warm(lay)     # 切换后安排预热下一个（真正执行在 _process_warms）

    def _schedule_warm(self, lay, delay=1.5):
        """安排预热（delay 秒后执行）。延时是为了错开刚切换那一下的解码高峰。"""
        if lay.fixed >= 0 or self.locked or not lay.clips or \
                getattr(lay, "is_kv", False) or len(lay.clips) < 2:
            lay.pre_idx = -1
            lay.warm_pend = False
            return
        lay.warm_at = time.perf_counter() + delay
        lay.warm_pend = True

    def _do_warm(self, lay, idx):
        """真正预热：取出（或新建）该素材的解码器并让它解出首帧。
        全程在解码线程里做，渲染线程只花创建对象的时间（0.2~0.5ms）。"""
        try:
            if 0 <= idx < len(lay.clips):
                p = self._get_player(lay, lay.clips[idx])
                if hasattr(p, "warm_up"):
                    p.warm_up()
        except Exception:
            pass

    def _process_warms(self, snap):
        """每帧检查一次「到点该预热了」（只在拍位切换模式下用；逐拍交替在 _beat_show 里直接预热）"""
        if self.mode == "beat" or self.locked:
            return
        now = time.perf_counter()
        for lay in self.layers:
            if not lay.warm_pend or now < lay.warm_at:
                continue
            lay.warm_pend = False
            if lay.fixed >= 0 or self.locked or getattr(lay, "is_kv", False):
                continue
            cands = self._candidate_indices(lay, snap)
            if not cands:
                continue
            # 与 _switch_layer_random 用同一条随机源（固定 seed 时也要保持可复现）
            seed = int(self.cfg["auto"].get("seed") or 0)
            rng = random.Random(seed) if seed else self.rng
            pick = rng.choice(cands)
            lay.pre_idx = pick
            self._do_warm(lay, pick)

    def _get_player(self, lay, clip):
        """取该素材的解码器；缓存命中则复用（切换零开销）。LRU 上限 8。"""
        # 解码缩放上限：画布 ×1.5（留 img_scale 放大余量），4K/5K 素材在解码线程缩到该尺寸，
        # 避免每帧对超大帧做 cvtColor+copy+QPainter 缩放（画面持续卡、FPS 掉的主因）。
        mw = max(1, int(self.canvas.width() * 1.5))
        mh = max(1, int(self.canvas.height() * 1.5))
        # 缩略图缺失时 alpha 是**后台**探测的：探测回来发现确实带 alpha → 丢掉当时按
        # 「无 alpha」建的解码器（OpenCV 会丢透明通道），下次取自然重开成 PyAV。
        if getattr(clip, "_alpha_recheck", False):
            stale = lay.cache.get(clip.path)
            if stale is None or (stale is not lay.player and stale is not lay.prev_player):
                clip._alpha_recheck = False
                lay.cache.pop(clip.path, None)
                if stale is not None:
                    try:
                        stale.close()
                    except Exception:
                        pass
            # 正在播的话这次不动它，标记留着，下次取该素材时再换（避免切换中把画面掐掉）
        p = lay.cache.get(clip.path)
        if p is not None:
            lay.cache.pop(clip.path)
            lay.cache[clip.path] = p
            if hasattr(p, "set_max_size"):
                p.set_max_size(mw, mh)
            return p
        p = make_player(clip)
        if hasattr(p, "set_max_size"):
            p.set_max_size(mw, mh)
        lay.cache[clip.path] = p
        cur_path = lay.clips[lay.cur].path if 0 <= lay.cur < len(lay.clips) else ""
        while len(lay.cache) > 8:
            evicted = False
            for k, v in list(lay.cache.items()):
                if k == cur_path or v is lay.player or v is lay.prev_player:
                    continue
                v.close()
                lay.cache.pop(k)
                evicted = True
                break
            if not evicted:
                break
        return p

    def _preload_clips(self, lay, clips=None, max_preload=3):
        """已废弃：预加载机制曾导致 decoder 线程堆积、CPU 20%→60%（详见 memory）。
        现在**惰性打开**：切换时才创建解码器，而且构造不再阻塞（容器在解码线程里打开），
        所以无需预加载。保留空实现供可能的旧引用调用。"""
        return

    @_locked
    def _switch_layer_to(self, lay, idx, instant=False):
        if idx < 0 or idx >= len(lay.clips):
            return
        old = lay.player
        old_idx = lay.cur
        lay.prev_player = None if instant else old
        lay.prev_idx = old_idx if not instant else -1
        lay.cur = idx
        lay.player = self._get_player(lay, lay.clips[idx])
        lay.trans_start = time.perf_counter()
        lay.trans_active = not instant
        lay.last_switch_t = time.perf_counter()
        lay.recent.append(lay.clips[idx].path)
        if len(lay.recent) > max(3, len(lay.clips) // 2):
            lay.recent.pop(0)

    @_locked
    def next_scene(self):
        """手动下一素材（快捷键/按钮）——**按下即小节头** + 重置切换倒计时（手动对齐）。

        用户 2026-09-24 选定的方案（在"终点优先踩小节线"与"绝对精确拍数"之间选了这个）：
        按下这一刻同时做两件事 ——
          1) 把引擎的切换基准挪到当前整拍、清掉已排好的目标拍（下一次切换重新数满
             常规 16 / 快切 8 拍）；
          2) **把这个相位记成"小节头"**（`_bar_off_user`），于是 16 / 8 拍之后的目标点
             正好就是他自己定义的小节线 —— 既不会和自动贴线打架，间隔也是精确的。
        长静音（换歌）后这个手动相位自动失效，回到音频识别的小节头。

        （旧实现只设了 `lay.switch_pos`，引擎用的是 `self.switch_pos`，所以按了键倒计时
        根本没重置 —— 这是 2026-09-24 一起修掉的。）"""
        snap = self.audio.snapshot()
        pos = self._beat_pos(snap)
        beat = math.floor(pos)               # 对齐到整拍，避免基准落在拍中间
        self.switch_pos = beat
        self._bar_off_user = beat % 4        # ★ 按下即小节头（0~3 相位，网格 = 相位 + 4k）
        self._next_due = None                # 强制重排下一次切换
        self._due_off = self._bar_off(snap)
        for lay in self.layers:
            if lay.fixed >= 0 or not lay.clips:
                continue
            if getattr(lay, "is_kv", False):
                continue    # Kv 待机层不参与手动切素材
            self._switch_layer_random(lay, snap)
            lay.switch_pos = pos

    @_locked
    def manual_transition(self):
        """手动切换（Ctrl+Return）：与「下一素材」等价 —— 切一次 + 倒计时从这一拍重新数。

        旧实现把 switch_pos 设成 -999 让下一次切换立刻到期，结果是**下一帧又白切一次**
        （连切两下，第二下把第一下顶掉），净效果其实就是「切一次 + 倒计时重置」。
        现在统一走 next_scene，行为一致且不再多切一刀。"""
        self.next_scene()


    def manual_transition(self):
        self.switch_pos = -999  # 强制下一拍重切
        self._next_due = None
        self.next_scene()

    def lock_clip(self):
        self.locked = not self.locked

    # ---------------- 逐拍交替（模式为 beat 时） ----------------
    def _beat_tick(self, snap):
        """逐拍交替（按拍位相位驱动，支持 1/4 拍粒度的交替间隔）。
        不受「素材最小切换间隔」影响（独立节奏）。"""
        bc = self.cfg["beat"]
        interval = max(0.25, float(bc.get("interval_beats", 1) or 1))
        bars = max(1, int(bc.get("bars_per_pair", 2)))
        pos = self._beat_pos(snap)
        for lay in self.layers:
            if lay.fixed >= 0 or len(lay.clips) < 2 or self.locked:
                continue
            if getattr(lay, "is_kv", False):
                continue    # Kv 待机层不参与逐拍交替
            if lay.pair is None:
                lay.pair = self._new_pair(lay, bc, prev_second=None)
                lay.pair_turn = 0
                # 对子起点贴到小节线：之后每 bars 小节换一对，换对时机自然踩在整小节上
                lay.pair_start = self._bar_floor(pos, snap)
                lay.beat_last = pos
                self._beat_show(lay, 0, bc)
                continue
            # 每对小节数：一对素材整体使用 bars 小节后换新一对
            if pos - getattr(lay, "pair_start", pos) >= bars * 4:
                prev_second = lay.pair[1]
                lay.pair = self._new_pair(lay, bc, prev_second)
                lay.pair_turn = 0
                lay.pair_start = self._bar_floor(pos, snap)
                lay.beat_last = pos
                self._beat_show(lay, 0, bc)
                continue
            # 交替间隔：拍位每推进 interval 拍，A↔B 交替一次（默认 1 拍）
            if pos - getattr(lay, "beat_last", pos) >= interval:
                lay.beat_last = pos
                lay.pair_turn ^= 1
                self._beat_show(lay, lay.pair_turn, bc)

    def _new_pair(self, lay, bc, prev_second):
        n = len(lay.clips)
        mode = bc.get("pick_mode", "seq")
        end = bc.get("end_action", "loop")

        if mode == "seq":
            if prev_second is None:
                a = lay.order_i % n
                b = (a + 1) % n
                lay.order_i = (b + 1) % n
                return (a, b)
            a = prev_second
            ni = (prev_second + lay.order_dir) % n
            if ni == prev_second:
                ni = (prev_second + 1) % n
            if ni == 0 and end == "reverse":
                lay.order_dir = -1
                ni = (prev_second - 1) % n
            if end == "stop" and ni == 0:
                return lay.pair or (a, (a + 1) % n)
            b = ni
            lay.order_i = (ni + 1) % n
            return (a, b)

        # 随机模式：维护未使用素材池
        seed = int(self.cfg["auto"].get("seed") or 0)
        rng = random.Random(seed) if seed else self.rng
        if prev_second is None:
            lay.rand_pool = list(range(n))
            rng.shuffle(lay.rand_pool)
            a = lay.rand_pool.pop()
            b = lay.rand_pool.pop()
            pair = (a, b)
        else:
            a = prev_second
            pool = lay.rand_pool
            if not pool:  # 池空：重置，排除当前保留素材
                pool = [i for i in range(n) if i != a]
                rng.shuffle(pool)
                lay.rand_pool = pool
            cands = [i for i in pool if i != a]
            if not cands:
                cands = [i for i in range(n) if i != a]
            b = rng.choice(cands)
            if b in lay.rand_pool:
                lay.rand_pool.remove(b)
            pair = (a, b)
        return pair

    def _beat_show(self, lay, turn, bc):
        idx = lay.pair[turn] if lay.pair else 0
        if lay.cur == idx and lay.player:
            return
        clip = lay.clips[idx] if 0 <= idx < len(lay.clips) else None
        if clip is None:
            return
        lay.prev_player = lay.player
        lay.prev_idx = lay.cur
        lay.cur = idx
        lay.player = self._get_player(lay, clip)
        lay.trans_start = time.perf_counter()
        lay.trans_active = True
        lay.trans_ms = MODE_META["beat"]["trans_ms"]
        lay.last_switch_t = time.perf_counter()
        # 逐拍交替间隔可能只有 1 拍（≈0.5s），所以立刻预热对子里的另一个素材，
        # 否则每次交替都要等 70~690ms 才有首帧（用户说的"卡几帧"）。
        other = lay.pair[1 - turn] if lay.pair else -1
        if 0 <= other < len(lay.clips):
            lay.pre_idx = other
            lay.warm_pend = False
            self._do_warm(lay, other)

    # ---------------- Kv 主视觉图层（待机层） ----------------
    def kv_layer(self):
        """返回 Kv 主视觉图层（全局唯一），无则 None。"""
        for lay in self.layers:
            if getattr(lay, "is_kv", False):
                return lay
        return None

    def _kv_tick(self, snap):
        """Kv 待机层节奏（完全独立于其它设置）：
        - 电平低于「静音判定阈值」且持续 > 静音触发延迟 → 切入（待机画面进来）
        - 电平高于阈值且持续 > 声音恢复切出延迟 → 切出（待机画面出去）
        不受视觉行为模式 / 能量映射 / 逐拍 / 曲风匹配 / BPM 变速等任何设置影响，
        过渡方式与时长也只用 Kv 自己的配置。"""
        kv = self.kv_layer()
        if kv is None or not kv.clips:
            return
        cfg = self.cfg["kv"]
        thr = float(cfg.get("db_threshold", -50.0))
        silent_delay = max(0.0, float(cfg.get("silent_delay", 2.0)))
        resume_delay = max(0.0, float(cfg.get("resume_delay", 2.5)))
        now = time.perf_counter()
        dt = now - getattr(self, "_kv_last_t", now)
        dt = max(0.0, min(0.5, dt))     # 保护：卡帧/暂停后不累加巨大 dt
        self._kv_last_t = now

        lv = float(snap.get("lv_db", -80.0))
        if lv < thr:
            kv.kv_silent_sec += dt
            kv.kv_loud_sec = 0.0
        else:
            kv.kv_loud_sec += dt
            kv.kv_silent_sec = 0.0

        # 推进已有过渡
        if kv.kv_trans_active:
            dur = max(0.001, kv.kv_trans_dur)
            p = (now - kv.kv_trans_start) / dur
            if p >= 1.0:
                kv.kv_on = kv.kv_trans_to
                kv.kv_trans_active = False
            else:
                kv.kv_on = kv.kv_trans_from + (kv.kv_trans_to - kv.kv_trans_from) * p

        # 触发新过渡（不在过渡中时才触发）
        if not kv.kv_trans_active:
            if kv.kv_silent_sec >= silent_delay and kv.kv_on < 1.0:
                self._kv_begin(kv, 1, cfg.get("in_trans", "fade"), cfg.get("in_dur", 1.5))
            elif kv.kv_loud_sec >= resume_delay and kv.kv_on > 0.0:
                self._kv_begin(kv, 0, cfg.get("out_trans", "fade"), cfg.get("out_dur", 1.5))

        # 素材轮换：Kv 显示期间每隔 switch_sec 秒换下一个素材（0=不轮换，保持当前素材）
        sw = max(0.0, float(cfg.get("switch_sec", 0.0)))
        if sw > 0.0 and kv.kv_on > 0.01 and len(kv.clips) > 1:
            if kv.kv_switch_t <= 0.0:
                kv.kv_switch_t = now          # 切入后重新开始计时
            elif now - kv.kv_switch_t >= sw:
                kv.kv_switch_t = now
                kv.cur = ((kv.cur + 1) if 0 <= kv.cur < len(kv.clips) else 1) % len(kv.clips)
                self._kv_show(kv)
        else:
            kv.kv_switch_t = 0.0

        if kv.kv_on > 0.01:
            self._kv_show(kv)

    def _kv_begin(self, kv, to_state, kind, dur):
        """开始 Kv 切入(to_state=1)/切出(to_state=0) 过渡。cut 视为瞬切。"""
        kind = kind or "fade"
        if kind == "cut":
            kv.kv_on = float(to_state)
            kv.kv_trans_active = False
            kv.kv_trans_kind = "cut"
            kv.kv_trans_dur = 0.0
        else:
            kv.kv_trans_from = float(kv.kv_on)
            kv.kv_trans_to = float(to_state)
            kv.kv_trans_kind = kind
            kv.kv_trans_dur = max(0.05, float(dur))
            kv.kv_trans_start = time.perf_counter()
            kv.kv_trans_active = True
        if to_state == 1:
            self._kv_show(kv)

    def _kv_show(self, kv):
        """确保 Kv 图层有素材在播（固定索引优先，其次 cur，否则 clips[0]）。"""
        if not kv.clips:
            return
        if 0 <= kv.fixed < len(kv.clips):
            idx = kv.fixed
        else:
            idx = kv.cur if 0 <= kv.cur < len(kv.clips) else 0
        clip = kv.clips[idx]
        kv.cur = idx
        kv.player = self._get_player(kv, clip)

    # ---------------- 合成渲染 ----------------
    def _apply_color(self, snap):
        """颜色渲染器（一键调色）：合成后、后处理前跑一次。

        **铁律**：Kv 待机层激活时绝不染色（`kv_on>0.01` 时强度直接硬归零，连渐变都不走），
        旁路开启时也立即归零（Bypass 要"响应迅速、无延迟"）。
        频闪素材自动休眠：避免「画面在闪 + 颜色在变」叠加成视觉灾难。
        """
        cfx = self.color_fx
        try:
            cfg = self.cfg["color"]
        except Exception:
            return
        if cfx is None or not cfg:
            return
        kv = self.kv_layer()
        kv_on = float(getattr(kv, "kv_on", 0.0) or 0.0) if kv is not None else 0.0
        flick = self._flicker_hibernate(cfg)
        s = cfx.tick(cfg, snap, kv_on=kv_on, flicker=flick,
                     bypass=bool(cfg.get("bypass", False)))
        if s > 0.004:
            try:
                cfx.apply(self.canvas, cfg.get("mode", "lut"))
            except Exception:
                pass   # 调色失败不影响出画面

    def _flicker_hibernate(self, cfg):
        """频闪素材要不要让颜色休眠：开 / 关 / 随机。

        - on     遇频闪就休眠（防"画面在闪 + 颜色在变"叠加）
        - off    不休眠，照常染色
        - random 每次换到新素材时随机决定一次，**整段素材播放期间保持不变**
                 （每帧随机的话画面会疯闪，等于制造第二个频闪源）
        """
        mode = cfg.get("flicker_mode")
        if mode is None:                      # 老配置只有 skip_flicker
            mode = "on" if cfg.get("skip_flicker", True) else "off"
        if mode == "off":
            return False
        path = self._current_flicker_path()
        if not path:
            return False
        if mode == "on":
            return True
        if self._flicker_latch.get("path") != path:
            self._flicker_latch = {"path": path, "on": random.random() < 0.5}
        return bool(self._flicker_latch.get("on"))

    def _current_flicker_path(self):
        """当前在播的可见非 Kv 图层里，带「频闪」标签的素材路径（没有则空串）"""
        try:
            from tags_def import DYNAMIC_FLICKER
        except Exception:
            return ""
        for lay in self.layers:
            if getattr(lay, "is_kv", False) or not getattr(lay, "visible", True):
                continue
            try:
                i = lay.cur
                if i is not None and 0 <= i < len(lay.clips):
                    c = lay.clips[i]
                    if DYNAMIC_FLICKER in c.all_tags():
                        return c.path
            except Exception:
                continue
        return ""

    def _apply_postfx(self, snap):
        """对合成后的画布应用后处理特效链。

        触发模式（每个特效独立）：
        - constant 常驻：恒定按手动强度
        - genre    曲风：当前曲风命中绑定曲风才生效
        - event    事件：Drop 触发后从峰值按 decay 秒衰减回 0
        自动联动总开关 auto=False 时，全部退化为常驻。
        """
        cfg = self.cfg["postfx"]
        # Kv 待机层显示中：画面以 Kv 为主，跳过整条后处理链
        # （Kv 图层要求完全独立，不受「后处理」等任何设置影响）
        kv = self.kv_layer()
        if kv is not None and kv.kv_on > 0.01:
            try:
                from postprocess import fx_trails
                fx_trails(None, 0.0, reset=True)
            except Exception:
                pass
            return
        if not cfg or not cfg.get("enabled", True):
            try:
                from postprocess import fx_trails
                fx_trails(None, 0.0, reset=True)
            except Exception:
                pass
            return
        self._frame_i = getattr(self, "_frame_i", 0) + 1
        now = time.perf_counter()
        auto_link = bool(cfg.get("auto", True))
        mode = cfg.get("mode", "auto")
        g = max(0.0, min(1.0, float(cfg.get("global_level", 60)) / 100.0))
        genres_now = [x.lower() for x in self._current_genres(snap)] if auto_link else []
        # 全局唤醒系数：能量映射 0~1（0.2 以下归零=缓拍段休眠，0.65 以上满额=高潮活跃）。
        E = snap.get("energy", 0.0)
        wake = max(0.0, min(1.0, (E - 0.2) / 0.45))
        # 音频驱动带（能量连续驱动）
        band_val = {
            "bass": min(1.0, float(snap.get("bass", 0.0))),
            "mid": min(1.0, float(snap.get("mid", 0.0))),
            "high": min(1.0, float(snap.get("high", 0.0))),
            "energy": min(1.0, E),
        }
        effects_cfg = cfg.get("effects") or {}

        if mode != "manual":
            # 自动模式：按曲风/能量挑选 2~3 个效果，最终强度 = **能量 × 全局强度的 60%**。
            # 注意这里的 0.6 是「取全局强度的六成」，不是固定的绝对强度：
            #   全局强度 100% + 能量 1.0 → 60%；全局强度 60% + 能量 1.0 → 36%；
            #   全局强度 60% + 能量 0.5 → 18%。
            # （原来是「固定 45 × 唤醒系数 × 全局」——死值 45 不跟能量走，观感偏死板；
            #   现在强度完全由能量连续决定，安静段≈没有效果、高潮段≈全局的六成。）
            # level 给 100：让最终强度就等于这条公式，不再被 45/100 二次压小。
            # 关键：自动挑的效果必须绕过 effects[].on 过滤（默认全 False，否则永远没效果）。
            E_auto = max(0.0, min(1.0, float(snap_energy(snap, 0.0))))
            # 「效果保持」：0 = 实时跟随能量（旧行为）；>0 = 选中一组效果后保持 N 拍，
            # 到点（或曲风变了）才重新挑下一组。
            # 为什么要它：能量在 0.75 / 0.3 这些门槛附近抖动时，exposure / softfocus
            # 会反复进出效果链，画面上就是「效果忽有忽无」；按拍保持后变化才有节奏感。
            hold = max(0, int(cfg.get("auto_hold_beats", 0) or 0))
            if hold <= 0:
                chosen = self._auto_pick_effects(snap, genres_now)
                self._fx_hold_pick = None
                self._fx_hold_due = 0.0
                self._fx_hold_genres = None
            else:
                pos = self._beat_pos(snap)
                # 拍位倒退（换歌 / 拍钟重置）= 之前的保持计时失效，必须重挑，
                # 否则 due 会一直大于当前拍位、效果再也不换。
                last = getattr(self, "_fx_hold_last", 0.0)
                back = pos < last - 0.5
                self._fx_hold_last = pos
                gkey = tuple(genres_now or ())
                need = (self._fx_hold_pick is None or back
                        or pos >= getattr(self, "_fx_hold_due", 0.0)
                        or getattr(self, "_fx_hold_genres", None) != gkey)
                if need:
                    self._fx_hold_pick = self._auto_pick_effects(snap, genres_now)
                    self._fx_hold_due = pos + hold
                    self._fx_hold_genres = gkey
                chosen = self._fx_hold_pick
            effects_override = {n: {"on": True, "level": 100} for n in chosen}
            mods = {n: E_auto * AUTO_FX_RATIO * g for n in chosen}
        else:
            effects_override = None
            mods = {}
            active = []
            for name, e in effects_cfg.items():
                if not e.get("on"):
                    continue
                trig = e.get("trigger", "constant")
                if not auto_link or trig == "constant":
                    m = 1.0
                elif trig == "genre":
                    bound = [x.strip().lower() for x in (e.get("genres") or "").split(",") if x.strip()]
                    m = 1.0 if (bound and genres_now and any(
                        b in c or c in b for b in bound for c in genres_now)) else 0.0
                    m *= wake
                elif trig == "event":
                    if snap.get("drop"):
                        self._postfx_trig[name] = now
                    t0 = self._postfx_trig.get(name, 0.0)
                    decay = max(0.2, float(e.get("decay", 1.5) or 1.5))
                    env = max(0.0, 1.0 - (now - t0) / decay) if t0 else 0.0
                    m = env * wake
                else:
                    m = 1.0
                # 音频驱动：绑定频段强度连续缩放（留 20% 底）
                drive = (e.get("drive") or "off")
                if drive in band_val:
                    m *= (0.2 + 0.8 * band_val[drive])
                # **手动模式完全独立：不乘全局强度**（用户要求）。
                # 手动模式的强度 = 该效果自己的滑块数值 × 它自己的触发/驱动系数，
                # 所以「全局强度」只在自动模式生效，不会再暗地里缩放手动效果。
                mods[name] = m
                active.append((name, e))
        try:
            from postprocess import apply_postfx
            apply_postfx(self.canvas, cfg, seed=self._frame_i, mods=mods,
                         spectrum=snap.get("spectrum"),
                         reset_trails=not self.running,
                         effects_override=effects_override)
        except Exception:
            pass  # 后处理失败不影响渲染

    _AUTO_FX_BY_GENRE = [
        (("hardcore", "frenchcore", "gabber", "uptempo"), ["chromatic", "glitch"]),
        (("trance",), ["bloom", "trails"]),
        (("house", "disco", "funk", "garage"), ["saturation", "bloom"]),
        (("techno", "industrial"), ["deform", "chromatic"]),
        (("drum & bass", "dnb", "dubstep", "breakbeat", "breakcore"), ["glitch", "deform"]),
        (("ambient", "downtempo", "chillout"), ["softfocus", "trails"]),
        (("synthwave",), ["chromatic", "bloom"]),
        (("hardstyle", "rawstyle"), ["chromatic", "deform"]),
        (("hip hop", "trap"), ["glitch", "grain"]),
        (("pop", "anime", "future bass"), ["saturation", "softfocus"]),
    ]

    def _auto_pick_effects(self, snap, genres_now):
        """自动模式：按曲风挑 2 个基础效果，高潮加曝光脉冲，缓拍加柔焦，最多 3 个"""
        chosen = []
        for keys, fx in self._AUTO_FX_BY_GENRE:
            if any(k in gn or gn in k for gn in genres_now for k in keys):
                chosen = list(fx)
                break
        if not chosen:
            chosen = ["bloom", "chromatic"]
        E = snap.get("energy", 0.0)
        if E > 0.75 and "exposure" not in chosen:
            chosen.append("exposure")
        elif E < 0.3 and "softfocus" not in chosen:
            chosen.append("softfocus")
        return chosen[:3]

    def _compose(self, snap):
        canvas = self.canvas
        canvas.fill(Qt.black)
        p = QPainter(canvas)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        W, H = canvas.width(), canvas.height()
        now = time.perf_counter()

        # 能量脉冲缩放（振幅强度：0 低 / 1 中 / 2 高 / 3 极高）
        # 2026-09-24 用户要求「所有选项数值全部上调」：0.4/0.7/1.0/1.4 → 0.6/1.0/1.4/2.0
        # （缩放量 = bass × 0.08 × 该系数 → 满低频时 4.8% / 8% / 11.2% / 16%）
        # 索引做钳位：旧配置只有 0~2，新版本加了第 4 档也不会 IndexError
        inten = [0.6, 1.0, 1.4, 2.0][max(0, min(3, int(self.cfg["auto"]["intensity"])))]
        pulse = 1.0 + snap["bass"] * 0.08 * inten
        drift_x = drift_y = 0.0

        silent = snap["silent_sec"] > 0.25 or snap["level"] < 0.03
        solo_on = any(getattr(l, "solo", False) for l in self.layers)
        subtract_jobs = []  # 需要逐像素"减少"混合的图层

        # 图层列表 index 0 = 最上层 → 从下往上绘制
        for lay in reversed(self.layers):
            if not lay.visible or not lay.player:
                continue
            if solo_on and not getattr(lay, "solo", False):
                continue
            # Kv 待机层：显示强度由 kv_on 控制（无声音=1 完全覆盖，有声音=0 完全切出）
            is_kv = getattr(lay, "is_kv", False)
            if is_kv and lay.kv_on <= 0.01:
                continue
            if lay.hide_silent and silent and not is_kv:
                continue  # 该图层设置了"无声音时隐藏"且当前没有声音
            img = lay.player.current()
            if img is None or img.isNull():
                img = lay.prev_player.current() if lay.prev_player else None
            if img is None or img.isNull():
                # 兜底：新素材的首帧还没解出来（打开容器 24~250ms）时继续显示上一帧，
                # 避免硬切/快切下出现「一瞬间黑掉」。只存引用，不拷贝（解码线程每帧都会
                # 产出新的 QImage 对象，所以引用是安全的）。
                img = getattr(lay, "_last_frame", None)
            if img is None or img.isNull():
                continue
            lay._last_frame = img
            if is_kv:
                # Kv 图层：进度=显示强度；过渡方式只用它自己的切入/切出设置，不受其它设置影响
                prog = max(0.0, min(1.0, lay.kv_on))
                tr = lay.kv_trans_kind if lay.kv_trans_kind in ("fade", "cut", "glitch", "zoom") else "fade"
            else:
                prog = 1.0
                if lay.trans_active:
                    dur = (lay.trans_ms or MODE_META[self.mode]["trans_ms"]) / 1000.0
                    dur = max(0.016, dur)
                    prog = min(1.0, (now - lay.trans_start) / dur)
                    if prog >= 1.0:
                        lay.trans_active = False
                        lay.prev_player = None  # 播放器在缓存中复用，不关闭

                # 过渡方式：用户覆盖 > 旧版保存的设置 > 模式默认（逐拍=硬切，其余=淡入）
                ov_tr = (self.cfg["mode"].get("transition") or "").strip()
                if not ov_tr:
                    legacy = self.cfg["auto"].get("transition")
                    if isinstance(legacy, str) and legacy in ("fade", "cut", "slide", "zoom", "glitch"):
                        ov_tr = legacy
                if ov_tr in ("fade", "cut", "slide", "zoom", "glitch"):
                    tr = ov_tr
                elif self.mode == "beat":
                    tr = "beat_cut"
                else:
                    tr = "fade"

            blend = getattr(lay, "blend", "normal") or "normal"
            if blend == "subtract":
                # Qt 无原生"减少"：先渲染到临时图，p.end() 后逐像素相减
                tmp = QImage(W, H, QImage.Format_ARGB32)
                tmp.fill(Qt.transparent)
                tp = QPainter(tmp)
                tp.setRenderHint(QPainter.SmoothPixmapTransform)
                self._draw_layer(tp, lay, img, W, H, prog, tr, pulse, snap, drift_x, drift_y)
                tp.end()
                subtract_jobs.append((tmp, lay.opacity))
            else:
                p.setCompositionMode(BLEND_QT.get(blend, QPainter.CompositionMode_SourceOver))
                self._draw_layer(p, lay, img, W, H, prog, tr, pulse, snap, drift_x, drift_y)
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)

        # 黑场
        if self.blackout:
            p.fillRect(0, 0, W, H, Qt.black)

        p.end()

        # "减少"混合：画布 RGB 减去图层 RGB（逐像素，仅在该图层使用"减少"时执行）
        if subtract_jobs:
            self._apply_subtract(canvas, subtract_jobs)

    @staticmethod
    def _np_view(img):
        """QImage(RGB32/ARGB32) → 读写 numpy 视图 (h, w, 4)，通道序 BGRA"""
        w, h = img.width(), img.height()
        bpl = img.bytesPerLine()
        arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
        return arr[:, :w * 4].reshape(h, w, 4)

    def _apply_subtract(self, canvas, jobs):
        for tmp, opacity in jobs:
            try:
                c = self._np_view(canvas).astype(np.int16)
                t = self._np_view(tmp).astype(np.int16)
                rgb = np.clip(c[:, :, :3] - (t[:, :, :3] * opacity), 0, 255)
                c[:, :, :3] = rgb
                self._np_view(canvas)[:, :, :3] = c[:, :, :3].astype(np.uint8)
            except Exception:
                pass  # 逐像素失败时跳过该图层，不影响整帧

    def _draw_layer(self, p, lay, img, W, H, prog, tr, pulse, snap, drift_x=0.0, drift_y=0.0):
        alpha = lay.opacity
        is_kv = getattr(lay, "is_kv", False)
        # 锁定画面：该图层不随能量脉冲缩放、不随氛围漂移（logo/字幕钉在原地）
        # Kv 待机层同样不做能量脉冲缩放（显示强度完全由 kv_on/prog 控制）
        if getattr(lay, "lock_motion", False) or is_kv:
            pulse = 1.0
        # 关闭"保留素材自带 alpha"时：把透明区域填黑，作为实底画面（带缓存，避免每帧转换）
        if not getattr(lay, "use_alpha", True) and img is not None and img.hasAlphaChannel():
            key = img.cacheKey()
            if lay._alpha_key != key or lay._alpha_img is None:
                flat = QImage(img.width(), img.height(), QImage.Format_RGB32)
                flat.fill(Qt.black)
                fp = QPainter(flat)
                fp.drawImage(0, 0, img)
                fp.end()
                lay._alpha_key, lay._alpha_img = key, flat
            img = lay._alpha_img
        prev = lay.prev_player.current() if lay.prev_player else None
        now = time.perf_counter()

        # 每个素材独立的大小/位置（存在 MediaItem 上，而非整个图层）
        cur_item = lay.clips[lay.cur] if 0 <= lay.cur < len(lay.clips) else None
        prev_item = lay.clips[lay.prev_idx] if (lay.trans_active and 0 <= lay.prev_idx < len(lay.clips)) else None

        def _t(item):
            if item is None:
                return False, 0.0, 0.0, 1.0, 0.0
            us = float(getattr(item, "img_scale", 1.0) or 1.0)
            ux = float(getattr(item, "img_x", 0.0) or 0.0)
            uy = float(getattr(item, "img_y", 0.0) or 0.0)
            rot = float(getattr(item, "img_rot", 0.0) or 0.0)
            custom = (abs(us - 1.0) > 1e-3 or abs(ux) > 1e-3
                      or abs(uy) > 1e-3 or abs(rot) > 1e-3)
            return custom, W * ux / 100.0, H * uy / 100.0, us, rot

        def paint_cover(painter, image, item, scale=1.0, dx=0.0, dy=0.0, a=1.0):
            if image is None or image.isNull():
                return
            custom, ux, uy, us, rot = _t(item)
            iw, ih = image.width(), image.height()
            if custom:
                s = min(W / iw, H / ih) * scale * us   # 完整显示 + 用户缩放
            else:
                s = max(W / iw, H / ih) * scale        # 铺满
            tw, th = iw * s, ih * s
            painter.save()
            painter.setOpacity(max(0.0, min(1.0, a)))
            if abs(rot) > 1e-3:
                # 旋转：平移到画面中心（+用户位移），绕中心旋转后居中绘制
                painter.translate(W / 2.0 + dx + ux, H / 2.0 + dy + uy)
                painter.rotate(rot)
                painter.drawImage(QRectF(-tw / 2.0, -th / 2.0, tw, th), image)
            else:
                painter.drawImage(QRectF((W - tw) / 2 + dx + ux, (H - th) / 2 + dy + uy, tw, th), image)
            painter.restore()

        dx = drift_x * (0.5 + (lay.cur % 3) * 0.25)  # 各图层漂移相位不同
        dy = drift_y * (0.5 + (lay.cur % 2) * 0.5)
        if getattr(lay, "lock_motion", False):
            dx = dy = 0.0

        if tr == "beat_cut" or prog >= 1.0 or (not is_kv and not lay.trans_active):
            paint_cover(p, img, cur_item, pulse, dx, dy, alpha)
            return

        if tr == "fade":
            if prev is not None:
                paint_cover(p, prev, prev_item, pulse, dx, dy, alpha)
            paint_cover(p, img, cur_item, pulse, dx, dy, alpha * prog)
        elif tr == "slide":
            off = W * (1 - prog)
            if prev is not None:
                paint_cover(p, prev, prev_item, pulse, -W * prog, dy, alpha)   # 旧画面左移出
            paint_cover(p, img, cur_item, pulse, off, dy, alpha)              # 新画面右移入
        elif tr == "zoom":
            if prev is not None:
                paint_cover(p, prev, prev_item, pulse * (1 + 0.4 * prog), dx, dy, alpha * (1 - prog))
            paint_cover(p, img, cur_item, pulse * (0.7 + 0.3 * prog), dx, dy, alpha)
        elif tr == "glitch":
            import random as _r
            rng = _r.Random(int(now * 1000))
            if prev is not None:
                paint_cover(p, prev, prev_item, pulse, 0, 0, alpha)
            # 随机行错位（用源矩形绘制，避免每行 copy）
            p.save()
            p.setOpacity(alpha)
            step = max(8, H // 24)
            iw, ih = img.width(), img.height()
            for y in range(0, H, step):
                off = rng.randint(-40, 40) if rng.random() < 0.35 else 0
                sy = min(ih - 1, int(y / H * ih))
                sh = max(1, int(step / H * ih))
                p.drawImage(QRectF(off, y, W, step), img, QRectF(0, sy, iw, sh))
            p.restore()
        else:
            paint_cover(p, img, cur_item, pulse, dx, dy, alpha)

    # ---------------- 手动干预 ----------------
    def set_blackout(self, on):
        self.blackout = on

    def set_freeze(self, on):
        self.freeze = on

    def set_pause_auto(self, on):
        self.pause_auto = on

    @_locked
    def toggle_run(self):
        self.running = not self.running
        pos = self._beat_pos(self.audio.snapshot())
        self.switch_pos = pos
        self._next_due = None
        return self.running

    @_locked
    def close(self):
        self.timer.stop()
        for lay in self.layers:
            lay.close_all()
        if self._spout is not None:
            try:
                self._spout[0].releaseSender()
            except Exception:
                pass
            self._spout = None
        if self._ndi is not None:
            try:
                self._ndi.close()
            except Exception:
                pass
            self._ndi = None
