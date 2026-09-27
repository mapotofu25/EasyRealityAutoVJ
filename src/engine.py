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
from beatgrid import GridClock
from fp import hint_window_frames
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


def _publish_grid_hint(obj, orig_sec, w_frames=None):
    """把「预测的识别窗起点原曲秒数」发布给识别线程（作为 fp.match 的 hint_delta）。

    模块级函数（**不是**方法）：老的测试 dummy 只 bind 指定的几个方法，若写成
    `self._set_grid_hint(...)` 会让它们 AttributeError。这里用 getattr 容错：
    · obj 无 `audio`（离线 dummy）⇒ 静默 no-op，行为与改动前逐值一致；
    · <0 表示无提示（清除）。
    `w_frames`：hint 窗口半宽（hop 帧）。None ⇒ 不更新（识别侧用缺省 HINT_W）。引擎按
    当前歌 grid 的 beat_len 用 `fp.hint_window_frames()` 换算后传入，使窗口**按拍数**
    定义、跨 BPM 一致（固定帧窗在 90~200BPM 间对应的拍数差 2 倍以上）。
    **不**去持 audio 的锁（只写一个 float 属性，CPython 下原子）：本函数在引擎
    每帧路径（`_tick_lock` 内）运行，绝不能与音频线程的锁互相牵扯。"""
    aud = getattr(obj, "audio", None)
    if aud is not None:
        try:
            aud.grid_pred_orig = float(orig_sec)
            if w_frames is not None:
                aud.grid_pred_w = float(w_frames)
        except Exception:                                                 # noqa: BLE001
            pass


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
        # 排 _next_due 时用的**栅格宽度**（4=小节线 / 8=八拍乐句）。用于识别「栅格变粗」
        # （4→8，未锁定→锁定交接）——此时必须把重贴限制在「绝不提前」，见 _next_switch_due。
        self._due_w = None
        # 用户按「下一素材」手动重定义的相位：存**绝对拍位**（_bar_off 再按当前栅格宽度取模）。
        # 存绝对值而不是相位，是为了在「4 拍小节」与「8 拍乐句（网格模式）」之间切换宽度时不丢对齐。
        self._bar_off_user = None
        self._pos_ref = None    # 上次切换时的连续拍位（非逐拍模式）
        # ---- 离线节拍网格（八拍乐句）驱动 ----
        self._song_grid_lookup = {}   # song_id -> grid（ui_main 在曲库就绪后注入）
        self._grid_clock = GridClock()   # 网格拍位时钟（连续/单调/防抖）
        # 上一帧处于网格模式的 song_id（None=当前不在网格模式）。
        # 用于检测「网格 → 音频拍钟」的切换边沿，做坐标续接（见 _beat_pos）。
        self._grid_active_sid = None
        # 网格坐标 − 音频拍钟坐标 的一次性平移量：两种坐标原点/速率都可能不同，在**任一切换
        # 边沿**（网格→音频 或 音频→网格）都重算一次，使返回的绝对拍位在开关切换处
        # **连续**（避免坐标原点不同导致的 ±数百拍跳变）。关闭 / 从未用过网格时恒为 0.0
        # → 原有逻辑逐值不变（零回归）。
        self._grid_coord_delta = 0.0
        self._grid_last_pos = 0.0      # 最近一次网格模式返回的**网格原始坐标**拍位
        self._grid_last_out = None     # 最近一次 _beat_pos 返回的最终拍位（切换时算 delta 用）
        # 首次锁定用的「多窗一致性」暂存：(sid, off, ref_t)，见 _grid_observe。
        self._grid_pending = None
        # 手动「八拍相位翻转」量（0 或 4 拍）：离线算的 phrase（0/4 二选一）判错时，用
        # `flip_phrase_now()` 翻转半个乐句。只影响**对齐相位 / 预告**，**不影响拍位本身**
        # （见 _bar_off / phrase_countdown）。0 = 与离线结果一致（默认，零回归）。
        self._grid_flip = 0.0
        # 最近一次**已处理**的识别窗 (sid, ref_t)：识别结果每 0.5s 才更新一次，但引擎每帧都会
        # 调 _beat_pos_grid。用它保证「每个识别窗只处理一次」（否则同一窗每帧都覆盖
        # _grid_pending，两窗 offset 增量被压成 ~0，一致性永不成立 → 永不上锁）。
        self._grid_seen = None
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

    @_locked
    def set_song_grids(self, mapping):
        """注入 song_id → grid 映射（离线八拍网格）。ui_main 在曲库就绪后 / 扫描后调用。

        grid 结构：{bpm, beat_len, anchor, phrase, contrast, src, conf, dur, …}，
        统一约定 **八拍头 = anchor + 8n × beat_len**（beat_len=60/bpm）。
        只有「字段完整 + conf 足够高」的 grid 才会被 ui_main 注入（见 _inject_song_grids）。
        mapping=None / {} 时等价于关闭网格模式（退回现有拍钟逻辑）。"""
        self._song_grid_lookup = mapping or {}
        # 换库 / 重扫：旧的网格时钟坐标系作废
        self._reset_grid_clock()

    def _reset_grid_clock(self):
        """作废局部网格时钟（换库/重扫/关闭网格时调用）。"""
        try:
            self._grid_clock.reset()
        except Exception:
            pass
        self._grid_active_sid = None
        self._grid_coord_delta = 0.0
        self._grid_last_pos = 0.0
        self._grid_last_out = None
        self._grid_pending = None
        self._grid_seen = None
        self._grid_flip = 0.0          # 手动八拍相位翻转量（0/4 拍），换库/重扫时清掉
        _publish_grid_hint(self, -1.0)

    # 指纹对齐票数门槛：低于此值视为「歧义 / 弱匹配」，offset 不可信 → 不做相位校正。
    # 依据（tools/_qa_votes_dist.py 实测：10 首真 FLAC、159 个 4.5s 查询窗；QA 第二轮 21 首
    #   × 前 200s、hop=1s、4116 个 4.5s 窗复测）：
    #   正确命中票数 min=6 p25=27 中位=48~56 p75=113~189 max=423；
    #   命错歌票数 = 0~3；命中同歌但**位置错**（重复段落歧义）票数最高达 172。
    #   ★ 第二轮实测：votes ≥ 180 的窗仅 10.2%（p50=56 / p75=113 / p90=181），
    #     最长连续低于 180 达 196s ⇒ 门槛 180 太严，重同步机会过少（时钟长期自由跑）。
    #   现降到 **120**（≈p75 档）：把重同步覆盖率从 ~10% 提到明显更高，同时用
    #   「多窗一致性 + 连续同向跳转判据」挡住坏样本（见 _beat_pos_grid 与 GridClock），
    #   误接受率仍为 0。弱窗只做速率更新（见 update_rate），不校正相位，无损。
    #   设计取舍：**宁可自由跑，也不让坏坐标把整条时钟拽飞**（重复段落会硬重锚到错坐标）。
    GRID_MIN_VOTES = 120
    # 强匹配（"确认"）门槛：**硬重锚**（坐标跳变）的默认要求。实测坏样本（重复段落歧义）
    # 票数最高 172 < 180 ⇒ 用 180 可以把所有已知坏样本挡在「单次巨跳」之外；而弱窗
    # （120~179）仍可做**有界相位微调**并计入「连续同向」计数，重同步覆盖率不受影响。
    GRID_STRONG_VOTES = 180
    # 首次锁定的「多窗一致性」容差：连续两个可信窗 offset 增量 / 挂钟增量 的比值需落在此区间
    # （= 输入速率 r 的合理范围）。区间比真实 r[0.8,1.25] 略宽，兼顾 keylock + 识别误差。
    GRID_CONSIST_LO = 0.6
    GRID_CONSIST_HI = 1.45
    # ★ 首次锁定「多窗一致性」判据允许的**最大窗间隔 dt**（秒）：仅当
    #   「上个暂存可信锚」与「本窗」的挂钟间隔 ≤ 它时，才允许用两窗 offset 增量判一致并锁定。
    #   超时（> 它）视为锚太旧/识别中断/seek ⇒ 丢弃旧锚、以本窗**重新暂存**，绝不用很旧的锚锁。
    #
    #   取值依据（本轮实测，220 例 = 20 首 × 11 个起点，口径见 tools/_gridfix5_accept.py）：
    #     6.0（旧，本常量引入前的字面量）：总误锁 171s、最长误锁 54.0s、>5s 5 例。
    #       唯一长误锁来自 `Can We Believe That`(t=0)=54.0s：t=0 暂存锚、t=6.0 因一个残差窗
    #       “一致”（dt=6.0 正好卡在旧上限）而锁定，锁到偏 −1.82s 的坐标（锚太旧）。
    #     4.0（现取值）：总误锁 48s、最长 32.0s、>5s 3 例、CWB/Lost 均 0、覆盖仍 220/220。
    #     3.0 与 4.0 指标完全相同 ⇒ 3~4 是一块**平台**（非尖峰），取更保守的 4.0（离 2.5 的反弹更远）。
    #   ⚠ 为什么不能太小（别顺手调小！）：
    #     · 2.5 / 2.0：总误锁反弹到 208s、>5s 升到 8 例 —— 上限过紧会把“正常 0.5s 识别窗、
    #       偶发一次 1~2s 抖动延迟”的合法暂存锚也判超时丢弃 ⇒ 反复重暂存 ⇒ 反而锁不住正确坐标。
    #     · 1.5 / 1.0：**彻底不锁定**（覆盖 0/220）—— 功能直接失效（多数正常窗间隔都会超时）。
    #   ⇒ 上限必须留出「正常识别窗（0.5s）+ 少量抖动/一两次丢窗」的余量，4.0 是实测的稳妥点。
    GRID_CONSIST_MAX_DT = 4.0

    def _grid_observe(self, snap):
        """每帧观测识别结果并推进「网格锁定判定」。可用时返回
        (grid, beat_len, anchor, off_orig, r, sid)，不可用（关闭 / 未识别 / 无网格 /
        offset 缺失）时返回 None。

        ★ 语义：只要返回非 None，本方法**已经把本帧的新识别窗喂进 `_grid_clock`**
          （已锁定 → update / 低票 update_rate；未锁定 → 「连续两个可信窗一致」锁定判据
          + 暂存 `_grid_pending`）。调用方据 `clock.sid == sid` 判是否**已锁定**
          （见 `_grid_state`）。
        ★ **未锁定阶段不发布 hint**（H2 保留）：本方法只喂时钟、**不写** `grid_pred_orig`；
          未锁定帧由 `_beat_pos` 的非网格分支统一发布 -1（matcher 走全局众数、开环），
          这样首个锚点的对错在单窗内无法判断时不会被自己的临时 hint 锁死
          （在离线重放里实测：给临时 hint 会把错误锚点"自证"，Demon 78s→5s）。
        ★ 关闭 / 没网格 / 没认出歌时**立即返回 None**（不碰时钟）——这就是「关闭 / 没网格 /
          没认出歌 → 零回归」的保证。"""
        try:
            if not self.cfg["auto"].get("beat_grid", True):
                return None
        except Exception:
            return None
        sid = snap.get("recognized_song_id", -1)
        if sid is None or sid < 0:
            return None
        grid = self._song_grid_lookup.get(sid)
        if not grid:
            return None
        bl = grid.get("beat_len") or 0.0
        if bl <= 0:
            return None
        anchor = grid.get("anchor")
        if anchor is None:
            return None
        off = snap.get("recognized_offset", -1.0)
        if off is None or off < 0:
            return None
        r = snap.get("recognized_rate", 1.0)
        try:
            r = float(r)
        except Exception:
            r = 1.0
        if not (0.5 <= r <= 2.0):
            r = 1.0
        bl = float(bl)
        anchor = float(anchor)
        off = float(off)
        # 手动八拍相位翻转（0/4 拍）：每帧从注入表读，所以 `flip_phrase_now()` 说改就改，
        # **不用重建网格、不丢锁定**。未设置时恒为 0（零回归）。
        try:
            self._grid_flip = float(grid.get("flip") or 0.0) % 8.0
        except Exception:
            self._grid_flip = 0.0
        # ---------------- 喂时钟 / 锁定判定 ----------------
        ref_t = snap.get("recognized_t", 0.0)
        try:
            ref_t = float(ref_t)
        except Exception:
            ref_t = 0.0
        if ref_t <= 0.0:
            # 时间戳缺失的防御：沿用时钟已记录的参考时刻；**不要**用 perf_counter（会给
            # 每一帧造出"新参考"，让同一窗被反复处理）。时钟尚未记录时用 0（只处理一次）。
            rt = self._grid_clock.ref_time()
            ref_t = rt if rt > 0.0 else 0.0
        # 票数是否可信：缺失字段当可信（保持既有行为）；有字段则按门槛判。
        votes = snap.get("recognized_votes", None)
        if votes is None:
            trusted = True
            strong = True
        else:
            try:
                _iv = int(votes)
                trusted = _iv >= self.GRID_MIN_VOTES
                strong = _iv >= getattr(self, "GRID_STRONG_VOTES", 180)
            except Exception:
                trusted = True
                strong = True
        clock = self._grid_clock
        # ★ 每个识别窗**只处理一次**：识别结果每 0.5s 才更新（recognized_t 在这段时间内不变），
        #   而引擎每帧都会走到这里。用显式「最近已处理窗」(sid, ref_t) 去重，只在真正的新窗
        #   （换歌或 ref_t 前进）处理一次；否则同一窗每帧覆盖 `_grid_pending`，两窗 offset
        #   增量被压成 ~0 ⇒ 一致性永不成立 ⇒ 永不上锁。
        seen = getattr(self, "_grid_seen", None)
        is_new_win = (seen is None) or (seen[0] != sid) or (ref_t > seen[1] + 1e-6)
        if is_new_win:
            self._grid_seen = (sid, ref_t)
        if clock.sid == sid:
            if is_new_win:
                if trusted:
                    clock.update(sid, bl, anchor, off, r, ref_t, strong=strong)
                else:
                    # 低票数窗：只刷新速率、不做相位校正（阻塞项 A）
                    clock.update_rate(bl, r)
        elif is_new_win and trusted:
            # 尚未锁定坐标系：只有「本窗可信 + 与上个可信窗一致」才锁定（阻塞项 B）。
            # 多窗一致性：本窗与上个暂存可信窗的 offset 增量，需与挂钟增量一致
            # （= 原曲时间轴推进速率 ≈ r ∈ [LO,HI]），否则视为歧义/坏值暂不锁定。
            p = getattr(self, "_grid_pending", None)
            have = (p is not None and p[0] == sid)
            dt = (ref_t - p[2]) if have else 0.0
            consistent = False
            max_dt = getattr(self, "GRID_CONSIST_MAX_DT", 4.0)
            if have and 0.05 < dt <= max_dt:
                d_off = off - p[1]
                if d_off > 0.0:
                    rate = d_off / dt
                    consistent = (getattr(self, "GRID_CONSIST_LO", 0.6)
                                  <= rate
                                  <= getattr(self, "GRID_CONSIST_HI", 1.45))
            if have and consistent:
                clock.update(sid, bl, anchor, off, r, ref_t, strong=strong)  # 正式锁定
            elif (not have) or dt > max_dt:
                # 首个可信窗 / 距上个可信窗很久（识别超时、seek）→ 重新定位暂存锚
                self._grid_pending = (sid, off, ref_t)
        return grid, bl, anchor, off, r, sid

    def _grid_state(self, snap):
        """网格模式所需信息；**只有已锁定**且（开关开 + 认出的歌有网格）时返回
        (grid, beat_len, anchor, off, r, sid)，否则 None。

        ★ **未锁定阶段彻底不进入网格模式**（本轮定案）：本方法在未锁定时返回 None ⇒
          `_beat_pos`/`_bar_off`/`_next_switch_due`/`phrase_countdown` 全走**原有拍钟**
          （逐值等价）。理由是「未锁定期的绝对位置根本不需要准」—— 那段时间只是在等锁定，
          用原有拍钟拿到的**正确的 4 拍节奏**比网格给出的**错误绝对位置**对现场更有价值。
          这与产品既定目标一致：「前几秒只保证 4 拍；攒够几小节锁定八拍相位，锁定后才预告」。
          锁定那一刻起，`_beat_pos` 从原拍钟切到网格，并由 `_grid_coord_delta`（坐标续接，
          **任一边沿**都重算）保证返回拍位**连续不跳**（复用已有机制，未新造轮子）。
        ★ 本方法每帧都经 `_grid_observe` 喂一次识别结果（推进锁定判定）；**未锁定不发布
          hint**（H2，见 `_grid_observe`）。
        ★ 关闭 / 没网格 / 没认出歌时立即返回 None（`_grid_observe` 早退，不碰时钟）——零回归。"""
        info = self._grid_observe(snap)
        if info is None:
            return None
        if self._grid_clock.sid != info[5]:
            return None          # 尚未锁定坐标系：不进入网格模式（走原有拍钟）
        return info

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
        # ★ 2026-09-27 修（用户报：**没开始 VJ 时 NDI 也在输出**；Spout 同理，同一个坑）：
        #   没点「开始」一律**不喂帧** —— 与「输出设置」既有门控语义一致
        #   （显示输出窗口 / 重置窗口大小 / 输出显示器本来就要先开始）。
        #   为什么必须这样：演出前 OBS / 投影 / NDI 接收端很可能**已经在线**，
        #   未就绪的画面一推出去就直接上屏（彩排还没开始就把画面送出去了）。
        #   发送端本身**不拆**（接收端仍能发现这个源），只是不喂帧。
        if not self.running:
            return
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
        # ★ 2026-09-27 修（用户报 bug：没开始 VJ 时 NDI 也在输出）：
        #   没点「开始」一律**不喂帧**（理由同 `_spout_send`：接收端可能已经在线，未就绪画面会直接上屏）。
        if not self.running:
            return
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
        保证自动切换不会因为"拍钟不走"而永远不切换。

        ★ 网格模式（全局开关开 + 认出的歌有网格 + 识别已确认）优先：拍位改由离线八拍网格
          推算（连续/单调/防抖，见 `_beat_pos_grid`）。**否则完全走下面的原有逻辑（零回归）**。

        ★ 播出中途开关「节拍网格」：网格坐标与音频拍钟坐标**原点/速率都可能不同**，直接切会让
          返回拍位整体跳变。这里在**任一切换边沿**（网格→音频 或 音频→网格）都重算一次
          `_grid_coord_delta`（= 上一帧返回拍位 − 本帧原生坐标），叠加后返回拍位在切换处
          **连续、不跳拍**（旧实现只在【网格→音频】记 delta，【音频→网格】重入会跳 ~2 拍
          且随切换次数线性累积）。从未开过网格时 delta 恒为 0 → 原有逻辑逐值不变（零回归）。"""
        gs = self._grid_state(snap)
        in_grid = gs is not None
        was_grid = getattr(self, "_grid_active_sid", None) is not None
        if in_grid:
            raw = self._beat_pos_grid(snap, gs)
            self._grid_active_sid = gs[5]     # 记录本帧处于网格模式（song_id）
        else:
            # ---- 非网格模式：原有拍钟逻辑（零回归）。先在**原生坐标**算 raw ----
            if snap.get("bpm", 0) > 0 and snap.get("beat_active"):
                raw = snap["beat_count"] + snap.get("beat_phase", 0.0)
                self._pos_ref = (time.perf_counter(), raw)
            else:
                now = time.perf_counter()
                ref = getattr(self, "_pos_ref", None)
                if ref is None or now - ref[0] > 8.0:
                    # 长时间无鼓点：*必须*以「当前推算拍位」为新基准继续走（保持连续），
                    # 而不是沿用旧基准 ref[1]：否则每 8 秒拍位会突然跳回旧值，触发
                    # _auto_switch_tick 的 pos<switch_pos 重置 → 切换间隔翻倍甚至切不动
                    # （BPM 未锁定、走这条 120BPM 回退时钟时最明显：快切 8 拍实际变成 16 拍）。
                    cur = (ref[1] + (now - ref[0]) * (120.0 / 60.0)) if ref else 0.0
                    self._pos_ref = (now, cur)
                    ref = self._pos_ref
                raw = ref[1] + (now - ref[0]) * (120.0 / 60.0)   # 回退时钟：120 BPM
            self._grid_active_sid = None
            # 退出网格模式 ⇒ 清除匹配 hint（识别退回全局众数，与旧行为一致）
            _publish_grid_hint(self, -1.0)
        # 坐标续接：仅在**切换边沿**（网格↔音频）重算平移量，使返回拍位连续（不跳拍）。
        if in_grid != was_grid:
            prev_out = getattr(self, "_grid_last_out", None)
            if prev_out is not None:
                self._grid_coord_delta = prev_out - raw
        delta = getattr(self, "_grid_coord_delta", 0.0)
        out = raw + delta if delta else raw
        self._grid_last_out = out
        return out

    def _beat_pos_grid(self, snap, gs):
        """网格模式的连续拍位（**仅在已锁定时由 `_beat_pos` 调用**；见 `_grid_state`）。

        网格坐标 = (原曲位置 - anchor) / beat_len ⇒ 八拍头落在 8 的整数倍。
        GridClock 内部保证**单调不减**（一次识别抖动绝不跳拍）。

        ★ 喂时钟的工作已移到 `_grid_observe`（每帧一次、每识别窗去重；已锁定做
          update / 低票 update_rate；未锁定做多窗一致性锁定判定）。本方法**只取当前拍位
          并发布连续性 hint**，不再含"未锁定自由跑锚"分支：
          · 未锁定的拍位不再经此（走原有拍钟，见 `_grid_state`）——那段时间的绝对位置
            不需要准，用原有 4 拍拍钟即可（产品目标："前几秒只保证 4 拍"）。
          · 于是 `_pos_ref`/`_grid_last_pos` 只在锁定后更新，退出网格时坐标续接
            （`_beat_pos` 里的 `_grid_coord_delta`）仍连续。

        ★ 票数门槛与首次锁定判定的说明见 `_grid_observe`（GRID_MIN_VOTES / GRID_CONSIST_*）。

        ★ hint 发布：网格坐标 pos 在挂钟 now 时刻对应原曲位置 = anchor + pos·beat_len
          （= 识别窗**末端**，与 audio_engine 的 recognized_offset = off + 4.5·r 同轴）；
          窗口**起点**再回退 4.5·r 秒（r=输入速率，4.5s=识别窗长，见 audio_engine）。
          已锁定（clock.sid==sid）时才发布，保证 hint 属于当前歌的坐标系。
          R3：时钟刚发生 >jump_max（>32 拍）的硬重锚时，`hint_hold()`>0 —— 有界暂停发布
          （开环复核），避免"连续 2 个强错窗重锚到错坐标后 hint 又把它锁住"。"""
        _grid, bl, anchor, off, r, sid = gs
        now = time.perf_counter()
        clock = self._grid_clock
        pos = clock.pos(now)
        # 记录连续拍位：网格模式关掉后回退时钟可从这里接续，避免突然跳变
        self._pos_ref = (now, pos)
        self._grid_last_pos = pos
        if clock.hint_hold() > 0:
            _publish_grid_hint(self, -1.0)
        else:
            _publish_grid_hint(self, anchor + pos * bl - 4.5 * r,
                               hint_window_frames(bl))
        return pos

    def phrase_countdown(self, snap):
        """网格模式下「距下一个八拍头还有几拍 / 几秒」；非网格模式返回 None。

        秒的换算必须用 r：原曲 1 拍 = beat_len 秒，播放快 r 倍 ⇒ 1 拍 = beat_len/r 秒。"""
        gs = self._grid_state(snap)
        if gs is None:
            return None
        _grid, bl, _anchor, _off, r, _sid = gs
        self._beat_pos(snap)      # 先推进网格时钟（保证本帧已吸收最新识别参考）
        beats = max(0.0, self._grid_clock.beats_to_phrase(time.perf_counter()))
        # 手动相位翻转：时钟坐标里的八拍头在 `≡0 (mod 8)`，翻转后真正的乐句头在 `≡4 (mod 8)`，
        # 所以"还有几拍到八拍头"要跟着挪半句。flip=0 时与旧行为逐值一致（零回归）。
        flip = float(getattr(self, "_grid_flip", 0.0)) % 8.0
        if flip:
            pos = (8.0 - beats) % 8.0                 # 当前在组内的位置 [0, 8)
            nxt = (flip - pos) % 8.0
            beats = nxt if nxt > 1e-6 else 8.0
        sec = beats * bl / max(1e-6, r)
        return beats, sec

    def flip_phrase_now(self, snap):
        """把**当前识别到的这首歌**的八拍相位翻转半个乐句（+4 拍），**就地生效**。

        用途：离线算出来的 `phrase`（0/4 二选一）判错时，用户一听就知道"差半个乐句/差 4 拍"，
        这里一键翻过来。做法只改注入表里那首歌的 `flip`（`_grid_observe` 每帧读它）
        ⇒ **拍位不变、锁定不丢**，只有"哪条线算乐句头"整体挪 4 拍（`_bar_off` 跟着走，
        所以切换对齐、HUD 倒计时、预览网格线**同时**生效）。

        返回 `(True, 新相位)`；不适用时返回 `(False, 原因)`（`no_song` / `no_grid`）。
        持久化由调用方写进 `cfg["music_meta"][path]["grid_flip"]`（引擎不碰配置）。
        """
        sid = snap.get("recognized_song_id", -1)
        if sid is None or sid < 0:
            return False, "no_song"
        g = self._song_grid_lookup.get(sid)
        if not isinstance(g, dict):
            return False, "no_grid"
        cur = float(g.get("flip") or 0.0) % 8.0
        new = 0.0 if cur >= 2.0 else 4.0
        g["flip"] = new
        self._grid_flip = new
        return True, new

    def beat_grid_view(self, snap):
        """预览「节拍网格线」浮层用的极简数据：返回 (组内位置, 组宽) 或 None。

        - **组宽 w**：网格模式 8（八拍乐句），否则 4（4/4 小节）—— 与 `_align_width` 同一来源。
        - **组内位置**：当前拍在 `[0, w)` 中的位置，**0 = 正好踩在乐句头/小节头上**。
          界面以它为左端往右画 2 组，于是"哪条线是乐句头、现在离它多远"一眼可见。
        - ★ 相位与切换对齐**共用** `_align_width/_bar_off`（含手动「下一素材」重定义、含网格坐标
          续接 delta）⇒ HUD 上画出来的那条线**就是切换实际踩的那条线**，不会画出一条好看但
          与实际不符的网格（早先 `_bar_off` 漏掉 `_grid_coord_delta` 时，显示与实际就差了半个乐句）。
        取不到拍位（未开始 / 无拍钟）时返回 None。
        """
        try:
            p = self._beat_pos(snap)
        except Exception:
            return None
        if p is None:
            return None
        w = int(self._align_width(snap) or 0)
        if w <= 0:
            return None
        try:
            off = float(self._bar_off(snap))
        except Exception:
            off = 0.0
        idx = int(round(int(math.floor(float(p))) - off)) % w
        return idx, w

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

    def _align_width(self, snap):
        """切换点对齐的栅格宽度（拍）：网格模式=8（八拍乐句头），否则=4（4/4 小节线，现状）。"""
        return 8.0 if self._grid_state(snap) is not None else 4.0

    def _bar_off(self, snap):
        """当前生效的「对齐相位」（模 `_align_width`）：手动重定义优先，否则用音频/网格给的。

        「下一素材」按下时会把相位就地重定义（见 next_scene）——用户 2026-09-24 选的方案：
        **按下即第 1 拍**，之后所有切换都跟着他的拍走，间隔也是精确的 16 / 8 拍。
        换歌（长静音）后自动失效，回到音频识别的相位。

        ★ `_bar_off_user` 存的是**绝对拍位**，这里按当前栅格宽度取模：
          非网格模式 w=4 → `beat % 4`，与旧实现（存 `beat % 4` 再原样返回）**逐值一致**；
          网格模式 w=8 → `beat % 8`（对齐到八拍头）。"""
        w = self._align_width(snap)
        u = self._bar_off_user
        if u is not None:
            return float(u) % w
        if w == 8.0:
            # 网格模式：对齐到网格锚点（八拍头 = anchor + 8n·beat_len ⇒ **网格坐标** 8n）。
            # ⚠ `_beat_pos` 返回的是「网格坐标 + `_grid_coord_delta`」（delta = 坐标续接平移量，
            #   见 `_beat_pos`）。网格坐标 8n 在本坐标系（out）里落在 `out ≡ delta (mod 8)`，
            #   所以对齐相位必须返回 `delta % 8`，**不能**返回 0：返回 0 会把切换对齐到
            #   `out ≡ 0`，离真实八拍头相差 `delta % 8` 拍 —— 实测 anchor≠0 时偏差可达 ±4 拍
            #   （tools/_gridfix5_handoff.py：anchor=2.0s(=5 拍) 现状偏 3.04 拍 → 修正后 0.62 拍；
            #   anchor=1.6s(=4 拍) 现状偏 -3.96 拍 → 修正后 0.62 拍）。
            #   网格模式恒为锁定态（w==8 ⇔ `_grid_state` 非 None），delta 此时有效。
            return (float(getattr(self, "_grid_coord_delta", 0.0))
                    + float(getattr(self, "_grid_flip", 0.0))) % w
        return float(snap.get("downbeat_off", 0) or 0)

    def _bar_floor(self, pos, snap):
        """拍位所在小节的起始拍位（按小节头相位对齐，floor 到当前小节）
        注：始终按 4 拍一小节 floor —— 网格坐标里的"拍"就是真实拍，bar=4 拍在任何模式下都对。"""
        off = self._bar_off(snap)
        return off + math.floor((pos - off) / 4.0) * 4.0

    # 自动贴小节线的介入门槛（拍）：目标点离最近的小节线**不足这么多拍**时不动它。
    # 用户 2026-09-24 要求「只有明显偏移才介入」：以前任何偏移都会把切换点拉正，
    # 亚拍级的小偏差（拍钟抖动、帧粒度、手动按下的相位）也被强行拽一下 → 节奏被打断。
    # 现在只有 ≥1 拍（= 一个小节的 1/4）的错位才拉正；小偏差保持不变（它是**恒定**的、
    # 不会累积漂移，因为下一轮的目标始终是「上一轮目标 + 固定间隔」）。
    ALIGN_MIN_OFFSET = 1.0

    def _next_switch_due(self, snap):
        """下一次切换的目标拍位：**就近对齐到栅格线**（网格模式=八拍头，否则=4 拍小节线）。
        目标 = 当前基准 + 固定间隔（常规 16 / 快切 8 拍，都是 8 的倍数，网格下自然落在八拍头），
        再四舍五入到最近的栅格线——对不齐时最多加/减几拍把它凑到栅格上，这样素材切换始终踩在
        音乐的整乐句（网格模式）或整小节（否则）上，而不会切在拍子中间。
        （旧版用 ceil 向上取整：目标差 1 拍到小节线时会多等 3 拍才切，听感上就是"没对齐"。）

        对齐相位由音频侧 downbeat（小节头）或离线网格给出（`_bar_off()`，手动重定义优先）。
        相位识别出来/变化时，把已排好的切换点重新贴到新的栅格上（偏移 ≤半个栅格，听不出跳动）。

        ★ 「栅格变粗」（4→8）的特例（未锁定→锁定交接，本轮修）：
          从原拍钟（w=4）切到网格（w=8）那一帧，若照旧「就近贴线」，可能把已排好的目标
          重贴到当前 pos **之前**（实测 −2.4~−3.4 拍），于是锁定帧立刻触发一次
          「该切没到、纯 4 拍不会切」的提前/额外切换（240 组合扫描旧实现 9 例、其中 6 例
          lock→首切 = 0.000s）。修法：变粗时**只许往后贴**——
            · 先取离原目标最近的八拍头；若它会**明显提前**（≥ ALIGN_MIN_OFFSET 拍）则改取
              「≥ 原目标」的最近八拍头（绝不允许提前；0.04 拍级的边界残差不触发跳句）；
            · 若结果仍落在当前 pos 上/之前，再取 pos 之后的最近八拍头。
          这样锁定帧的切换**一定晚于**当时拍位，且相对原排程最多推迟一个八拍乐句（≤8 拍）。
          w 不变（含关闭/无网格/未锁定）时**不走此分支**，与旧实现逐值一致（零回归）。

        缓存 _next_due：模式切换不重置、能量波动不重算，倒计时稳定连续；
        只在切换发生/拍钟重置/静音/进入无间隔模式时清空重算。"""
        iv = self._current_interval(snap)
        if iv is None:
            self._next_due = None
            return None
        w = self._align_width(snap)
        off = self._bar_off(snap)
        if self._next_due is None:
            target = self.switch_pos + iv
            near = off + math.floor((target - off) / w + 0.5) * w
            # 只有明显偏移才拉正（见 ALIGN_MIN_OFFSET）：小偏差原样保留，不打断节奏
            self._next_due = near if abs(near - target) >= self.ALIGN_MIN_OFFSET else target
            self._due_off = off
        elif (getattr(self, "_due_w", None) is not None) and w > self._due_w:
            # ★ 栅格变粗（4→8，锁定交接）：只许往后贴，绝不允许提前。
            pos = getattr(self, "_grid_last_out", None)
            if pos is None:
                pos = self.switch_pos
            t0 = self._next_due
            near = off + round((t0 - off) / w) * w
            if abs(near - t0) < self.ALIGN_MIN_OFFSET:
                # 就近贴线的位移微不足道（含 0.0x 拍级的边界残差）⇒ 保持原目标，
                # 与旧实现一致；避免残差把目标跳整整一个乐句。
                near = t0
            elif near < t0:
                # 就近贴线会明显**提前** ⇒ 改取「≥ 原目标」的最近八拍头（最多推迟一个乐句）
                near = off + math.ceil((t0 - off) / w) * w
            if near <= pos + 1e-9:
                # 仍落在当前拍位上/之前（会立刻切）⇒ 取 pos 之后的最近八拍头
                near = off + math.ceil((pos - off) / w) * w
                while near <= pos + 1e-9:
                    near += w
            self._next_due = near
            self._due_off = off
        elif off != getattr(self, "_due_off", off):
            # 对齐相位变了：只有挪动幅度够明显才把已排好的切换点重新贴线，
            # 否则保持原目标（细碎的相位更新不再动切换点）
            near = off + round((self._next_due - off) / w) * w
            if abs(near - self._next_due) >= self.ALIGN_MIN_OFFSET:
                self._next_due = near
            self._due_off = off
        if self._next_due <= self.switch_pos:
            self._next_due += w   # 对齐后必须前进，避免原地打转
        self._due_w = w
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
        # ★ 按下即栅格头：存**绝对拍位**（_bar_off 再按当前宽度取模）
        #   —— 非网格（w=4）等价于旧的 `beat % 4`；网格模式（w=8）即"按下即八拍头"。
        self._bar_off_user = float(beat)
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
