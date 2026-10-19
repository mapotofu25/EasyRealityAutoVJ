# -*- coding: utf-8 -*-
"""预览画布（节目监视器）。

按播放头位置，用 `timeline.CueRuntime` 解出"此刻每层该显示什么"，
再用 `render.compositor.compose()` 合成成一幅 `QImage` 画到控件上。

═══ 纪律（架构方案 §4-A）═══
  A1/A4 取帧一律走 `media.frames.CueFrameSource`（复用解码器 + 失败退避），
        关闭时 `close()` 释放（本画布 **`shutdown()`** 时调用；`set_active(False)` 只停
        重合成、**不**释放解码器，见该方法注释）。
  A3 **只在"位置变了(dirty)"或"正在播放(playing)"时才重合成**；播放按 **30fps** 目标、用
        **单次触发**（处理完再挂下一拍，绝不重入/堆积）；静止时**不再挂下一拍** ⇒ CPU≈0
        （旧实现静止烧十几核）。
    A6 **取帧按预览尺寸缩放**（`out_size`）：4K 素材不再"转全尺寸 BGRA（33MB）再让合成器缩放"，
        而是在**解码后立刻**缩到 `画布 × OUT_HEADROOM`（见 `_out_size`）。实测 4K 取帧
        中位 19ms → 7ms。
    A7 **每层一条播放线（lane）**：合成器只按 `(clip_id, play_sec)` 取帧，看不出"哪一层"。
        本画布在 `_compose` 里按 `states` 造一张 **`(clip_id, play_sec) → lane(=图层标识)`** 表
        （见 `_build_lane_map`），`_frame_lookup` 据此把 **lane** 交给 `CueFrameSource` ——
        于是"**同一素材切多段、分放多层**"时各层**各持独立解码器**，不再挤在一个解码器上
        互相 seek（实测三层异入点场景 **15 → 30 fps**）。

本画布**不 import 演出引擎**：它自己解帧、自己合成，与演出输出完全无关
（规格书 §0 的区分）。

═══ 本轮（24 条）新增 ═══
  · **#16 PS 式裁切把手**：选中片段 ⇒ **双击预览**进"裁切模式"（画 8 个把手在**当前显示图像矩形**
    上）；拖把手改 `CueItem.crop`（源归一化 `[x,y,w,h]`）。像素↔源归一化的映射**冻结**（拖拽开始
    时用 `compositor._target_src` 取一次 `(tgt, src)`，拖拽期**不重算** ⇒ 无"裁切改适配基准→基准
    再改裁切"的反馈环）。拖拽期间**只改内存补丁 `_override`**（绝不 `set_runtime()`），**松手才
    提交**（复用 `xform_committed`，由 window 落模型 + 一次入撤销栈）。
  · **#10 点空白取消选择**：选中片段时，点"比例框外 / 框内但不在显示图像上" ⇒ 发新信号
    `selection_cleared`（由 window 收敛到 `_sync_props_selection`）。未选中 = 只读，不发。
  · **#16 像素尺寸**：`media.probe.video_size(path)`（零 Qt）给 `fw,fh`；**带缓存**（换运行时时清）。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ercue.media import probe
from ercue.media.frames import CueFrameSource
from ercue.render import compositor
from . import theme

log = logging.getLogger("ercue.ui.preview")

# ★ P0-1：黑屏时**分清原因**，别再让用户看到"没装载码表"却其实已经装了。
HINT_NOSHEET = "还没有装载码表"
HINT_EMPTY = "还没有排好的片段（把素材拖到时间轴，或双击素材在播放头加片段）"
HINT_BUSY = "预览准备中…"
# 兼容旧引用（旧代码/工具里可能只认 HINT）
HINT = HINT_EMPTY


class _XfShim:
    """只为把 `xform` dict 喂给 `compositor._xform_values(st)` 用（它按 `st.xform` 取参）。

    ★ 目的：**不重复实现"是否改过变换"的判据** —— 一律复用合成器的 `_xform_values/_is_custom`。
    """
    __slots__ = ("xform",)

    def __init__(self, xf):
        self.xform = xf or {}


# ---- ★ 需求 #16：PS 式裁切把手常量（纯几何，与合成器无关）----
CROP_MIN = 0.02            # 源归一化最小裁切边长（把手互顶保护）
HANDLE_PX = 8.0            # 把手命中半径（widget 像素）
HANDLE_CURSORS = {
    "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
    "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
    "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
    "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
}


class _GeomShim:
    """把 `xform`/`crop` 喂给 `compositor._target_src` 用（它按 `st.xform`/`st.crop` 取参）。

    ★ 复用合成器的几何（**绝不重复实现** `_target_src`）；`trans_p=1.0` 抹掉 slide/zoom 的
      空间偏移 ⇒ "显示图像矩形"只由 xform+crop 决定（裁切把手要的是一个**稳定基准**）。
    """
    __slots__ = ("xform", "crop", "trans", "trans_p")

    def __init__(self, xform, crop):
        self.xform = xform or {}
        self.crop = crop
        self.trans = "cut"
        self.trans_p = 1.0


def _clampf(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def crop_apply(crop0, handle, dx_norm, dy_norm, mn=CROP_MIN):
    """★ #16 **纯函数**：给定起始 `crop0=[x,y,w,h]` 与把手位移（**源归一化增量**），算新 crop。

    `handle` ∈ {tl,tr,bl,br,l,r,t,b}：左把手动 x/w、右把手动 w、上/下动 y/h、角把手两轴联动。
    `mn` = 最小边长守卫；结果**钳在 0..1** 且边长 `≥ mn`。**不碰模型/不碰 Qt**（可离线单测）。
    """
    l, t, w, h = (float(crop0[0]), float(crop0[1]), float(crop0[2]), float(crop0[3]))
    r, b = l + w, t + h
    if "l" in handle:
        l = _clampf(l + dx_norm, 0.0, r - mn)
    if "r" in handle:
        r = _clampf(r + dx_norm, l + mn, 1.0)
    if "t" in handle:
        t = _clampf(t + dy_norm, 0.0, b - mn)
    if "b" in handle:
        b = _clampf(b + dy_norm, t + mn, 1.0)
    return [l, t, r - l, b - t]


class PreviewCanvas(QWidget):
    """节目监视器画布。播放 30fps；只在 dirty/playing 时重合成（静止不挂下一拍）。

    ★ T06（需求 1 显示侧 + 需求 8 一期）：本画布还负责
      · **按码表比例**出画面（`set_ratio` / `runtime.sheet.aspect()`）—— 几何契约定不变，
        只是"基准框"从写死的 9/16 变成码表比例（合成器本来就吃传入的 `size`）；
      · **PR 式拖/拉变换**（一期=位移+缩放）：拖动画面改 `xform.x/y`、滚轮改 `xform.scale`；
        拖拽期间只在**内存**里打补丁（`_override`，不碰模型、不重建运行时），
        **松手才发 `xform_committed`** ⇒ 由 window 落模型 + **一次入撤销栈**。

    ★ T04（#16 裁切把手 + #10 点空白取消选择）：见上"本轮新增"段。
      交互只在**已选中片段**时可用；未选中 ⇒ 预览只读（双击/拖动/滚轮/点空白一律不响应）。
      退出裁切：再次双击 / `Esc` / 点框外 / 选中变化 / 换码表（换码表会清选中 ⇒ 自然退出）。
    """

    # ★ 冻结接口（T03 已在 window 侧"有就接"）：松手/滚轮防抖后提交变换
    xform_committed = Signal(str, str, dict, object)      # (lyr_id, item_id, xform, crop|None)
    # ★ T04（#10）：预览上点空白 ⇒ 请求 window 取消选择（"有就接"，未接不报错）
    selection_cleared = Signal()

    FPS = 30
    # ★ A6：取帧目标盒 = 预览画布 × OUT_HEADROOM（保持宽高比；`CueFrameSource` 绝不放大）。
    #   为什么 1.5×：合成器随后还要按 cover/contain + crop/xform **再缩放一次**，若取帧就缩到
    #   "正好画布尺寸"，被裁切/放大的图层就是**从已缩小的帧上再放大** ⇒ 糊。留 1.5× 余量，
    #   常见 crop / 轻微缩放仍清晰，同时内存/带宽仍比 4K 小 ~7×。
    OUT_HEADROOM = 1.5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runtime = None
        self.bpm = 0.0
        self.pos = 0.0                 # 当前位置（秒）
        self.playing = False
        self._img = None
        self._dirty = True
        self._err = ""
        self._active = False
        self._lane_of = {}             # ★ A7：(clip_id, round(play_sec,3)) -> lane（每拍由 _compose 重建）
        self.src = CueFrameSource()
        self._interval_ms = max(1, int(1000 / self.FPS))   # ★ 标称帧间隔（自校正用，不被 start() 改写）
        self._t = QTimer(self)
        self._t.setSingleShot(True)           # ★ 单次触发：处理完再挂下一拍，绝不重入/堆积
        # ★ 帧率修复：QTimer 默认 `Qt.CoarseTimer`，在 Windows 上会被量化到系统精度
        #   （15.625ms）⇒ 自校正用的 20~33ms 延迟会被抬到 ~31/47ms，重合成节奏不稳。
        #   改 `Qt.PreciseTimer` 让每一拍真正落在标称间隔上（实测 33ms Coarse=21.0Hz → Precise=30.0Hz）。
        self._t.setTimerType(Qt.PreciseTimer)
        self._t.setInterval(self._interval_ms)
        self._t.timeout.connect(self._tick)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # ★ T06：画布比例（需求 1 显示侧）。默认 16:9；`set_ratio()` 或运行时里的 sheet 会覆盖它。
        self._ratio = (1920, 1080)
        # ★ T06：PR 式变换（需求 8 一期）
        self._sel = ("", "")           # 选中片段 (lyr_id, item_id)；**未选中 = 只读预览**
        self._override = None          # 拖拽期间的**内存补丁** (lyr, item, xform, crop)
        self._drag = None              # 拖动状态
        # ★ T04：PS 式裁切把手（#16）+ 点空白取消选择（#10）
        self._crop_mode = False        # 是否处于裁切模式（选中片段 + 双击预览进入）
        self._crop_drag = None         # 裁切把手拖动状态（含**冻结**的映射：p0/handle/crop0/tgt）
        self._src_size = {}            # clip_id -> (w,h)|None（`probe.video_size` 缓存）
        self._hint_until = 0.0         # 首次切到 contain 时的提示显示截止时刻（monotonic）
        self._hint_t = QTimer(self)    # 提示到点后重绘一次（把提示擦掉）
        self._hint_t.setSingleShot(True)
        self._hint_t.timeout.connect(self.update)
        self._wheel_t = QTimer(self)   # 滚轮防抖：停止滚 250ms 才提交（**一次入栈**）
        self._wheel_t.setSingleShot(True)
        self._wheel_t.setInterval(250)
        self._wheel_t.timeout.connect(self._commit_override)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.WheelFocus)

    # ------------------------------------------------------------------
    def set_runtime(self, rt, bpm=None):
        # ★ A7：换了曲目/素材映射 ⇒ 先关掉池里全部解码器（不泄漏句柄；下次取帧按需懒开）。
        #   `set_runtime` 只在"用户操作/换轨"时调用（**不在每拍**），故这里释放代价可忽略。
        try:
            self.src.release(None)
        except Exception:                              # noqa: BLE001
            log.exception("换曲目时释放预览解码器失败")
        self.runtime = rt
        self._img = None                     # ★ P0-1：换了运行时 ⇒ 旧帧作废，绝不显示上一张码表的画面
        # ★ T06：运行时带着它那张码表 ⇒ **以码表比例为准**（换码表/导入/新建都自动跟随）。
        #   拿不到就用上一次 `set_ratio()` 的值，再没有就 16:9。
        try:
            if rt is not None and getattr(rt, "sheet", None) is not None:
                rw, rh = rt.sheet.aspect()
                if rw > 0 and rh > 0:
                    self._ratio = (int(rw), int(rh))
        except Exception:                              # noqa: BLE001
            log.exception("读取码表比例失败（沿用上一次的比例）")
        if bpm is not None:
            self.bpm = float(bpm or 0.0)
        self._lane_of = {}
        self._override = None                # 换运行时 ⇒ 丢弃未提交的拖拽补丁
        self._crop_drag = None               # ★ T04：换运行时 ⇒ 放弃进行中的裁切拖动
        self._src_size = {}                  # ★ T04：换运行时可换素材路径 ⇒ 尺寸缓存作废
        self._dirty = True
        self.update()

    # ------------------------------------------------------------------
    # ★ T06 冻结接口：比例 / 选中 / 变换提交
    # ------------------------------------------------------------------
    def set_ratio(self, res_w, res_h):
        """设置画布比例（需求 1）。`set_runtime` 会以运行时的码表比例覆盖它。"""
        try:
            w, h = int(res_w), int(res_h)
        except (TypeError, ValueError):
            return
        if w <= 0 or h <= 0 or (w, h) == self._ratio:
            return
        self._ratio = (w, h)
        self._dirty = True
        self._arm()
        self.update()

    def ratio(self) -> tuple:
        """当前画布比例 `(res_w, res_h)`（非法回落 16:9）——供测试/诊断。"""
        w, h = self._ratio
        return (w, h) if (w > 0 and h > 0) else (1920, 1080)

    def set_selection(self, lyr_id, item_id):
        """告诉画布"当前选中哪个片段"（需求 8）：**只有选中时**才允许拖拽变换。"""
        sel = (str(lyr_id or ""), str(item_id or ""))
        if sel == self._sel:
            return
        self._sel = sel
        self._override = None                         # 换选中 ⇒ 丢弃旧补丁
        self._drag = None
        self._crop_mode = False                       # ★ T04：换选中 ⇒ 退出裁切模式
        self._crop_drag = None
        self.update()

    def set_pos(self, sec):
        s = float(sec or 0.0)
        if abs(s - self.pos) > 1e-6:
            self.pos = s
            self._dirty = True
            self._arm()                    # ★ 位置变了 → 挂一拍去重合成（播放中本就连续 tick）
        self.update()

    def set_playing(self, on):
        self.playing = bool(on)
        if self.playing:
            self._arm()                    # ★ 起播 → 开始连续重合成

    def set_active(self, on):
        """可见时开 30fps 连续重合成；不可见时**只停定时器**（不关解码器）。

        ⚠ **必须可逆**：`set_active(False)` 只停重合成定时器、保留解码器；`_tick` 在
        `not _dirty and not playing` 时会自动空闲，故不会空转。**绝不能在这里调
        `src.close()`** —— `CueFrameSource.close()` 会永久置 `_closed=True`，之后
        所有取帧恒返回 None（见 `media/frames.py`），而「新建码表」会先 `_rebuild_runtime`
        再让用户加片段，一旦在这里 close 掉，预览就永久黑屏了。
        真正释放解码器只在 :meth:`shutdown`（窗口关闭）时做。
        """
        self._active = bool(on)
        if on:
            self._dirty = True
            self._arm()
            self._tick()
        else:
            self._t.stop()

    def _arm(self, delay_ms=None):
        """挂**一次**定时触发（单次模式）：只有可见、且当前没有已挂的触发时才挂。

        `delay_ms` 省略时用标称间隔；`_tick` 会传入「标称间隔 − 本拍耗时」做**自校正**，
        使播放时的周期稳定在标称值（而不是"间隔 + 本拍耗时"叠加导致只有一半帧率）。
        ⚠ 标称间隔存 `self._interval_ms`，**不读 `self._t.interval()`** —— 后者会被
        `QTimer.start(msec)` 改写成自定义延迟值。
        """
        if self._active and not self._t.isActive():
            d = self._interval_ms if delay_ms is None else delay_ms
            self._t.start(max(1, int(d)))

    def shutdown(self):
        """窗口关闭时调用：停重合成并**真正释放解码器**（此后再取帧返回 None）。"""
        self.set_active(False)
        try:
            self.src.close()
        except Exception:                                  # noqa: BLE001
            log.exception("关闭预览解码器失败")

    def frame_source(self) -> CueFrameSource:
        return self.src

    def has_content(self) -> bool:
        """当前运行时里是否**排了片段**（决定黑屏该说"还没有排好的片段"还是"准备中"）。"""
        rt = self.runtime
        if rt is None:
            return False
        try:
            return int(getattr(rt, "n_items", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    def _hint_text(self) -> str:
        """黑屏提示：分清 ①没装载 ②装了但没排片段 ③有片段但预览没激活/还没合成。"""
        if self.runtime is None:
            return HINT_NOSHEET
        if not self.has_content():
            return HINT_EMPTY
        return HINT_BUSY

    # ------------------------------------------------------------------
    def _tick(self):
        # ★ 只有"位置变了"或"正在播放"才重新合成（静止时 CPU≈0，且不再挂下一拍）
        if not self._dirty and not self.playing:
            return
        t0 = time.monotonic()
        self._dirty = False
        self._compose()
        self.update()
        # ★ 单次触发 + **自校正**再挂下一拍：周期稳定在标称帧间隔；慢帧时自动拉长、
        #   绝不堆积/重入（旧固定周期定时器在慢帧后会连发补拍）。
        if self._active and self.playing:
            used_ms = (time.monotonic() - t0) * 1000.0
            self._arm(self._interval_ms - used_ms)

    def _canvas_size(self):
        """★ T06（需求 1）：画布尺寸 = **码表比例框** contain 进控件（保持宽高比、居中）。

        旧实现写死 `W × 9/16`；现在比例来自 `ratio()`（`set_ratio` 或运行时里的 sheet）。
        ⚠ 合成器本来就按传入的 `size=(W,H)` 算 cover/contain ⇒ **几何契约不变**，
          只是"基准框"跟着码表比例走（竖屏 9:16 时基准框就是竖的）。
        """
        return self._box_size()

    def _box_size(self):
        """在控件矩形内放下"码表比例框"（contain）：返回 `(w, h)`（像素，≥1）。"""
        rw, rh = self.ratio()
        avail_w = max(64, self.width() or 640)
        avail_h = max(64, self.height() or 360)
        k = min(avail_w / float(rw), avail_h / float(rh))
        return (max(1, int(round(rw * k))), max(1, int(round(rh * k))))

    def _box_rect(self) -> QRectF:
        """比例框在控件内的实际矩形（居中）。"""
        r = self.rect()
        w, h = self._box_size()
        return QRectF(r.x() + (r.width() - w) / 2.0, r.y() + (r.height() - h) / 2.0, w, h)

    def _out_size(self):
        """★ A6 取帧目标盒 `(w,h)` = 画布尺寸 × `OUT_HEADROOM`（见类常量处说明）。

        `CueFrameSource` 收到后**保持宽高比、绝不放大**（小素材/图片不受影响）；
        同一尺寸的 swscale 缩放器会被缓存，故这里每帧新建元组无额外开销。
        """
        W, H = self._canvas_size()
        return (max(1, int(round(W * self.OUT_HEADROOM))),
                max(1, int(round(H * self.OUT_HEADROOM))))

    def _frame_lookup(self, clip_id, play_sec):
        """为某一层解析它此刻该取的帧：`(clip_id, play_sec) -> BGRA | None`。

        ★ M4：合成器契约改成**双参数**（`frame_lookup(clip_id, play_sec)`）。
          旧实现只按 `clip_id` 取帧 ⇒ 同一素材同时出现在两层、且 `play_sec` 不同时，
          两层会拿到同一帧（后者覆盖前者）。现在每层按自己的 `play_sec` 取帧。

        ★ 播放修复：一律走 `frame_play()`（**顺序解码前进**），绝不用 `frame()` ——
          `frame()` 每次 seek 回关键帧，同一 GOP 内所有时刻都返回同一张关键帧，
          连续播放时画面"卡住 / 减速"（用户真实反馈）。

        ★ A6：带上 `out_size`（预览尺寸），让 4K 素材**在解码后立刻缩小**，避免中间整帧浪费。
        ★ A7：带上 `lane`（由 `_build_lane_map` 按图层定），让"同一素材多层不同入点"
          **各持独立解码器**、互不 seek。
        """
        path = self._path_of(clip_id)
        if not path:
            return None
        key = (clip_id, round(float(play_sec or 0.0), 3))
        lane = self._lane_of.get(key)
        return self.src.frame_play(path, play_sec, out_size=self._out_size(), lane=lane)

    @staticmethod
    def _build_lane_map(states):
        """★ A7：`(clip_id, round(play_sec,3)) -> lane`（lane = 图层标识：`lyr_id`）。

        为什么这样定键：
          · 合成器只按 `(clip_id, play_sec)` 取帧，**拿不到图层**；而 `_compose` 手里有
            `states`（含 `lyr_id`），故在这里把"取帧键 → 图层"的映射造出来交给取帧回调。
          · **同素材、不同入点** ⇒ `play_sec` 不同 ⇒ 不同键 ⇒ 不同 lane ⇒ **各持解码器**（本任务目标）。
          · **同素材、同入点（叠层）** ⇒ 同一个键 ⇒ 同一个 lane ⇒ **仍共用解码器**
            （那本来就是同一条播放线；共用反而更省）。
          · ★ 与合成器**逐字对齐**：合成器会把 `play_sec` 钳到 `src_out_sec`（A2），
            故这里**同时登记"原始键"与"钳位后的键"**，保证回调一定命中。
        """
        m = {}
        for i, st in enumerate(states or []):
            if st is None or not getattr(st, "active", True) or getattr(st, "missing", False):
                continue                                   # 合成器会跳过 ⇒ 不必登记
            lane = (getattr(st, "lyr_id", "") or ("z%d" % getattr(st, "z", i))
                    or ("i%d" % i))
            cid = getattr(st, "clip_id", "")
            try:
                ps = float(getattr(st, "play_sec", 0.0) or 0.0)
            except (TypeError, ValueError):
                ps = 0.0
            m[(cid, round(ps, 3))] = lane
            so = getattr(st, "src_out_sec", None)
            if so is not None:
                try:
                    m[(cid, round(min(ps, float(so)), 3))] = lane
                except (TypeError, ValueError):
                    pass
        return m

    def _path_of(self, clip_id):
        rt = self.runtime
        if rt is None:
            return None
        return rt.media_paths.get(clip_id)

    def _compose(self):
        W, H = self._canvas_size()
        self._img = None
        if self.runtime is None:
            return
        try:
            states = self.runtime.update(self.pos)
            self._apply_override(states)                   # ★ T06：拖拽补丁（只改这一帧的输出）
            self._lane_of = self._build_lane_map(states)   # ★ A7：先定"每层一条播放线"
            # 同一帧内对同一 (clip_id, play_sec) 只解一次（避免多花解码）；不同 play_sec 各解各的。
            memo = {}

            def lookup(clip_id, play_sec=0.0):
                key = (clip_id, round(float(play_sec), 3))
                if key in memo:
                    return memo[key]
                fr = self._frame_lookup(clip_id, play_sec)
                memo[key] = fr
                return fr

            self._img = compositor.compose((W, H), states, lookup)
            self._err = ""
        except Exception as e:                              # noqa: BLE001
            self._err = "%s: %s" % (type(e).__name__, e)
            log.exception("预览合成失败")
            self._img = None

    # ------------------------------------------------------------------
    # ★ T06：PR 式拖/拉变换（一期 = 位移 + 缩放；**只在选中片段时可用**）
    # ------------------------------------------------------------------
    def _selected_item(self):
        """按当前选中 id 在运行时里找 `(layer, item)`；找不到返回 `(None, None)`。"""
        lyr_id, item_id = self._sel
        rt = self.runtime
        if rt is None or not item_id or getattr(rt, "sheet", None) is None:
            return None, None
        try:
            for t in rt.sheet.tracks:
                for ly in t.composition.layers:
                    if lyr_id and ly.lyr_id != lyr_id:
                        continue
                    for it in ly.items:
                        if it.item_id == item_id:
                            return ly, it
        except Exception:                                  # noqa: BLE001
            log.exception("解析选中片段失败")
        return None, None

    @staticmethod
    def _xf_is_custom(xf) -> bool:
        """该 xform 是否"改过"（⇒ 契约上从 cover 切到 contain）。

        ⚠ **复用合成器的判据**（`compositor._is_custom/_xform_values`），**绝不在此重复实现几何契约**，
          否则两端会漂。
        """
        return bool(compositor._is_custom(*compositor._xform_values(_XfShim(xf))))

    def _eff_xform(self):
        """当前该显示的 `(xform, crop)`：**拖拽补丁优先**，否则选中片段自己的值。"""
        patch = self._override
        if patch is not None:
            crop = patch[3]
            return dict(patch[2]), (list(crop) if isinstance(crop, (list, tuple)) else None)
        _ly, it = self._selected_item()
        if it is None:
            return {"scale": 1.0, "x": 0.0, "y": 0.0}, None
        xf = getattr(it, "xform", None)
        crop = getattr(it, "crop", None)
        return (dict(xf) if isinstance(xf, dict) else {"scale": 1.0, "x": 0.0, "y": 0.0},
                list(crop) if isinstance(crop, (list, tuple)) and len(crop) == 4 else None)

    def _apply_override(self, states):
        """把拖拽补丁打到本帧的 `LayerDrawState` 上（**只改输出，不碰模型/不入栈**）。"""
        patch = self._override
        if not patch:
            return
        lyr_id, item_id, xf, crop = patch
        for st in states or []:
            if (getattr(st, "lyr_id", "") == lyr_id
                    and getattr(st, "item_id", "") == item_id):
                st.xform = dict(xf)
                st.crop = (list(crop) if isinstance(crop, (list, tuple)) and len(crop) == 4
                           else None)
                return

    def _set_override(self, xf, crop, *, hint_check=True):
        """写入拖拽补丁 → 请求一次**轻量**重合成（单次触发定时器天然把重绘限制在 ≤30fps）。

        ⚠ 拖拽期间**绝不** `set_runtime()`（那会 `CueFrameSource.release(None)` 释放全部解码器）。
          位置没变 ⇒ `frame_play` 走复用路径（0 解码），所以每拍的代价只有"合成+绘制"。
        """
        ly, it = self._selected_item()
        if it is None:
            return
        if hint_check and self._xf_is_custom(xf) and not self._xf_is_custom(
                getattr(it, "xform", None)):
            self._show_contain_hint()                     # ★ 首次 默认→有变换（cover→contain）给提示
        self._override = (ly.lyr_id, it.item_id, dict(xf), crop)
        self._dirty = True
        self._arm()                                       # 单次触发；已在挂就不重复挂（天然节流）
        self.update()

    def _show_contain_hint(self):

        self._hint_until = time.monotonic() + 3.0
        self._hint_t.start(3200)                          # 到点后重绘一次把提示擦掉
        self.update()

    def _commit_override(self):
        """**松手（或滚轮停止防抖后）才提交**：发信号 ⇒ window 落模型 + **一次入撤销栈**。"""
        patch = self._override
        self._override = None
        if not patch:
            return
        lyr_id, item_id, xf, crop = patch
        try:
            self.xform_committed.emit(lyr_id, item_id, dict(xf), crop)
        except Exception:                                  # noqa: BLE001
            log.exception("提交画面变换失败")

    # ==================================================================
    # ★ T04（#16）：PS 式裁切把手 —— 几何 / 命中 / 绘制 / 进入退出
    # ==================================================================
    def _source_size(self, it):
        """选中片段素材的像素尺寸 `(fw,fh)`；探测不到返回 `None`（**带缓存**）。

        ★ 复用 `media.probe.video_size`（零 Qt 的临时容器探测）。**不重复实现**尺寸读取。
        """
        cid = getattr(it, "clip_id", "")
        if cid in self._src_size:
            return self._src_size[cid]
        wh = None
        path = self._path_of(cid)
        if path:
            try:
                wh = probe.video_size(path)
            except Exception:                              # noqa: BLE001
                log.exception("探测素材像素尺寸失败: %s", path)
        self._src_size[cid] = wh
        return wh

    @staticmethod
    def _crop_or_full(crop):
        """归一化 crop 归一化：合法 4 元 → `list`；否则 `[0,0,1,1]`（= 不裁）。"""
        if isinstance(crop, (list, tuple)) and len(crop) == 4:
            return [float(crop[0]), float(crop[1]), float(crop[2]), float(crop[3])]
        return [0.0, 0.0, 1.0, 1.0]

    def _crop_frame(self, crop=None, xf=None):
        """算"当前显示图像矩形"的几何：返回 `(wgt, tgt, src, fw, fh)` 或 `None`。

        · `wgt` = **widget 坐标**的显示图像矩形（= `box` 左上 + 画布内 `tgt` 偏移；画布 1:1 映射到 `box`）。
        · `tgt/src` = 合成器 `_target_src` 的原始返回值（画布坐标 / 源像素），**几何零重复**。
        `crop`/`xf` 省略时取"当前**该显示**的值"（`_override` 优先，否则选中片段自身）——
        故拖动中把手始终贴合实际画面。拿不到素材尺寸 ⇒ `None`（如素材缺失）⇒ 不画把手、不响应裁切。
        """
        _ly, it = self._selected_item()
        if it is None:
            return None
        exf, ecrop = self._eff_xform()
        if xf is None:
            xf = exf
        if crop is None:
            crop = ecrop
        crop = self._crop_or_full(crop)
        wh = self._source_size(it)
        if not wh or int(wh[0]) <= 0 or int(wh[1]) <= 0:
            return None
        fw, fh = int(wh[0]), int(wh[1])
        W, H = self._canvas_size()
        xf = dict(xf) if isinstance(xf, dict) else {"scale": 1.0, "x": 0.0, "y": 0.0}
        tgt, src = compositor._target_src(_GeomShim(xf, crop), fw, fh, int(W), int(H))
        box = self._box_rect()
        wgt = QRectF(box.x() + tgt.x(), box.y() + tgt.y(), tgt.width(), tgt.height())
        return wgt, tgt, src, fw, fh

    def _image_widget_rect(self):
        """当前显示图像在 widget 上的矩形（`None` = 算不出 ⇒ 调用方按"整框都是图像"处理）。"""
        geo = self._crop_frame()
        return None if geo is None else geo[0]

    @staticmethod
    def _handle_points(wgt):
        """8 个把手中心点（4 角 + 4 边），顺序固定（便于测试）。"""
        l, t, r, b = wgt.left(), wgt.top(), wgt.right(), wgt.bottom()
        cx, cy = (l + r) / 2.0, (t + b) / 2.0
        return [(l, t), (cx, t), (r, t), (l, cy), (r, cy), (l, b), (cx, b), (r, b)]

    def _crop_hit(self, pos):
        """命中测试：返回 `(把手名|None, 该时刻的冻结 tgt|None)`。"""
        geo = self._crop_frame()
        if geo is None:
            return None, None
        wgt, tgt = geo[0], geo[1]
        spots = {"tl": (wgt.left(), wgt.top()), "tr": (wgt.right(), wgt.top()),
                 "bl": (wgt.left(), wgt.bottom()), "br": (wgt.right(), wgt.bottom()),
                 "l": (wgt.left(), (wgt.top() + wgt.bottom()) / 2.0),
                 "r": (wgt.right(), (wgt.top() + wgt.bottom()) / 2.0),
                 "t": ((wgt.left() + wgt.right()) / 2.0, wgt.top()),
                 "b": ((wgt.left() + wgt.right()) / 2.0, wgt.bottom())}
        for name, (hx, hy) in spots.items():
            if abs(pos.x() - hx) <= HANDLE_PX and abs(pos.y() - hy) <= HANDLE_PX:
                return name, tgt
        return None, tgt

    def _selection_blank_hit(self, pos, box) -> bool:
        """是否点在"空白"（#10）：比例框外，或框内但不在**显示图像**上（contain 黑边）。"""
        if not box.contains(pos):
            return True
        wgt = self._image_widget_rect()
        if wgt is not None and not wgt.contains(pos):
            return True                                    # 框内非图像区（letterbox）= 空白
        return False

    def _crop_drag_move(self, pos):
        """裁切把手拖动：**冻结映射**把像素增量换成源归一化增量 → 只写 `_override`。"""
        cd = self._crop_drag
        tgt, c0 = cd["tgt"], cd["crop0"]
        dxn = (pos.x() - cd["p0"].x()) / max(1.0, tgt.width()) * c0[2]
        dyn = (pos.y() - cd["p0"].y()) / max(1.0, tgt.height()) * c0[3]
        crop = crop_apply(c0, cd["handle"], dxn, dyn)
        cd["moved"] = cd["moved"] or any(
            abs(crop[i] - c0[i]) > 1e-9 for i in range(4))
        _ly, it = self._selected_item()
        xf = getattr(it, "xform", None)
        xf = dict(xf) if isinstance(xf, dict) else {"scale": 1.0, "x": 0.0, "y": 0.0}
        self._set_override(xf, crop, hint_check=False)      # xform 不变，只改 crop

    def _paint_crop_frame(self, p: QPainter) -> None:
        """在显示图像矩形上画 PS 式裁切框：框外压暗 + 边框 + 三分线 + 8 把手。"""
        geo = self._crop_frame()
        if geo is None:
            return
        wgt = geo[0]
        box = self._box_rect()
        p.save()
        try:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 100))
            for rc in self._dim_rects(box, wgt):
                p.drawRect(rc)                              # 框外压暗（PS 观感）
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(theme.c("accent")), 2))
            p.drawRect(wgt)
            p.setPen(QPen(QColor(255, 255, 255, 80), 1))
            for i in (1, 2):                                # 三分参考线
                x = wgt.left() + wgt.width() * i / 3.0
                y = wgt.top() + wgt.height() * i / 3.0
                p.drawLine(QPointF(x, wgt.top()), QPointF(x, wgt.bottom()))
                p.drawLine(QPointF(wgt.left(), y), QPointF(wgt.right(), y))
            p.setPen(QPen(QColor(0, 0, 0, 170), 1))
            p.setBrush(QColor(theme.c("accent")))
            for hx, hy in self._handle_points(wgt):
                p.drawRect(QRectF(hx - 4.0, hy - 4.0, 8.0, 8.0))
        finally:
            p.restore()

    @staticmethod
    def _dim_rects(box, wgt):
        """`box` 减去 `wgt`（交集外）的 4 条边框矩形，用于压暗（无交集则整框）。"""
        inter = box.intersected(wgt)
        if inter.isEmpty():
            return [QRectF(box)]
        out = []
        if inter.top() > box.top():
            out.append(QRectF(box.left(), box.top(), box.width(),
                              inter.top() - box.top()))
        if inter.bottom() < box.bottom():
            out.append(QRectF(box.left(), inter.bottom(), box.width(),
                              box.bottom() - inter.bottom()))
        if inter.left() > box.left():
            out.append(QRectF(box.left(), inter.top(), inter.left() - box.left(),
                              inter.height()))
        if inter.right() < box.right():
            out.append(QRectF(inter.right(), inter.top(), box.right() - inter.right(),
                              inter.height()))
        return out

    def _update_cursor(self, pos) -> None:
        """按悬停位置设置光标（裁切把手 / 显示图像 / 空白 / 只读）。"""
        if self._drag is not None or self._crop_drag is not None:
            return                                          # 拖动中由按下/松开处设置
        _ly, it = self._selected_item()
        if it is None:
            self.unsetCursor()                              # 未选中 = 只读预览
            return
        if self._crop_mode:
            hnd, _tgt = self._crop_hit(pos)
            if hnd:
                self.setCursor(HANDLE_CURSORS.get(hnd, Qt.ArrowCursor))
                return
        if self._selection_blank_hit(pos, self._box_rect()):
            self.setCursor(Qt.ArrowCursor)
            return
        self.setCursor(Qt.OpenHandCursor)

    # ---- 鼠标 / 滚轮 ----
    def mousePressEvent(self, e):
        self._drag = None
        self._crop_drag = None
        if e.button() != Qt.LeftButton:
            return
        _ly, it = self._selected_item()
        if it is None:
            return                                    # 未选中 = 只读预览（不进入任何拖拽/取消）
        pos = e.position()
        box = self._box_rect()

        # ① 裁切模式：优先命中 8 把手（冻结映射：p0 / handle / crop0 / 当时的 tgt）
        if self._crop_mode:
            hnd, tgt0 = self._crop_hit(pos)
            if hnd is not None:
                _xf, crop = self._eff_xform()
                self._crop_drag = {"p0": pos, "handle": hnd,
                                   "crop0": self._crop_or_full(crop),
                                   "tgt": tgt0, "moved": False}
                self.setCursor(HANDLE_CURSORS.get(hnd, Qt.ArrowCursor))
                e.accept()
                return

        # ② 点空白（比例框外 / 框内非图像）⇒ #10 取消选择（并退出裁切模式）
        if self._selection_blank_hit(pos, box):
            self._crop_mode = False
            self.selection_cleared.emit()
            e.accept()
            return

        # ③ 命中显示图像 ⇒ 拖位移（需求 8 一期）
        xf, crop = self._eff_xform()
        self._drag = {"p0": pos, "w": box.width(), "h": box.height(),
                      "x0": float(xf.get("x", 0.0) or 0.0),
                      "y0": float(xf.get("y", 0.0) or 0.0),
                      "scale": float(xf.get("scale", 1.0) or 1.0),
                      "crop": crop, "moved": False}
        self.setCursor(Qt.ClosedHandCursor)
        e.accept()

    def mouseMoveEvent(self, e):
        # ★ T04：裁切把手拖动优先
        if self._crop_drag is not None:
            self._crop_drag_move(e.position())
            e.accept()
            return
        d = self._drag
        if d is None:
            self._update_cursor(e.position())         # 悬停：把手/图像/空白 光标
            return
        # 位移单位 = **画布百分比**（与 `props_panel` 的 %、`xform.x/y` 的约定一致）
        dx = (e.position().x() - d["p0"].x()) / max(1.0, d["w"]) * 100.0
        dy = (e.position().y() - d["p0"].y()) / max(1.0, d["h"]) * 100.0
        if abs(dx) > 0.05 or abs(dy) > 0.05:
            d["moved"] = True
        self._set_override({"scale": d["scale"], "x": d["x0"] + dx, "y": d["y0"] + dy},
                           d["crop"])
        e.accept()

    def mouseReleaseEvent(self, e):
        # ★ T04：裁切把手松手（同样"松手一次入栈"）
        cd = self._crop_drag
        self._crop_drag = None
        if cd is not None:
            if cd.get("moved"):
                self._commit_override()
            else:
                self._override = None
                self.update()
            self._update_cursor(e.position())
            e.accept()
            return
        d = self._drag
        self._drag = None
        if d is None:
            return
        if d.get("moved"):
            self._commit_override()                   # ★ 松手一次入栈
        else:
            self._override = None
            self.update()
        self._update_cursor(e.position())
        e.accept()

    def mouseDoubleClickEvent(self, e):
        """★ T04（#16）：选中片段时**双击**进入/退出裁切模式。"""
        if e.button() != Qt.LeftButton:
            return
        _ly, it = self._selected_item()
        if it is None:
            return                                     # 未选中 = 只读，不能进裁切
        self._crop_mode = not self._crop_mode
        self._crop_drag = None
        self._override = None
        self.update()
        e.accept()

    def keyPressEvent(self, e):
        """★ T04：`Esc` 退出裁切模式。"""
        if e.key() == Qt.Key_Escape and self._crop_mode:
            self._crop_mode = False
            self._crop_drag = None
            self._override = None
            self.update()
            e.accept()
            return
        super().keyPressEvent(e)

    def leaveEvent(self, e):
        if self._drag is None and self._crop_drag is None:
            self.unsetCursor()
        super().leaveEvent(e)

    def wheelEvent(self, e):
        """滚轮 = 缩放（需求 8 一期）。停止滚动 250ms 后**防抖提交一次**（一次入栈）。"""
        _ly, it = self._selected_item()
        if it is None:
            return                                     # 未选中 = 只读，不吃滚轮
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        xf, crop = self._eff_xform()
        scale = float(xf.get("scale", 1.0) or 1.0) * (1.12 ** steps)
        scale = min(10.0, max(0.1, scale))
        self._set_override({"scale": scale, "x": float(xf.get("x", 0.0) or 0.0),
                            "y": float(xf.get("y", 0.0) or 0.0)}, crop)
        self._wheel_t.start()                          # 防抖：滚完再提交
        e.accept()

    # ------------------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        try:
            r = self.rect()
            p.fillRect(r, QColor("#101010"))
            box = self._box_rect()
            img = self._img
            # 比例框描边（比例切换一眼可见；也便于截图对比）
            p.setPen(QPen(QColor(theme.c("border")), 1))
            p.drawRect(box)
            if img is None or img.isNull():
                p.setPen(QColor(theme.c("fg_dim")))
                p.drawText(box, Qt.AlignCenter, self._hint_text())
                return
            p.drawImage(box, img)
            self._paint_overlay(p, box)
        finally:
            p.end()

    def _paint_overlay(self, p: QPainter, box: QRectF) -> None:
        """选中片段时画"当前适配模式"角标；首次切 contain 时给一句提示（需求 8）。"""

        _ly, it = self._selected_item()
        if it is None:
            return                                     # 未选中 = 只读预览：不画任何交互提示
        xf, _crop = self._eff_xform()
        contain = self._xf_is_custom(xf)
        txt = "完整显示(contain)·已含变换" if contain else "铺满(cover)"
        f = QFont(self.font())
        f.setPointSize(max(8, f.pointSize() - 1))
        p.setFont(f)
        bx = QRectF(box.x() + 6, box.y() + 6, 190, 20)
        p.fillRect(bx, QColor(0, 0, 0, 140))
        p.setPen(QColor(theme.c("accent") if contain else theme.c("fg_dim")))
        p.drawText(bx.adjusted(6, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, txt)
        if time.monotonic() < self._hint_until:
            p.setPen(QColor(theme.c("accent2")))
            p.drawText(QRectF(box.x(), box.bottom() - 24, box.width(), 20),
                       Qt.AlignCenter, "已切换到「完整显示」：不再铺满裁切（想回铺满就重置变换）")
        # ★ T04（#16）：裁切模式 ⇒ 画 PS 式裁切框 + 8 把手（并给一句操作提示）
        if self._crop_mode:
            self._paint_crop_frame(p)
            p.setPen(QColor(theme.c("accent2")))
            p.drawText(QRectF(box.x(), box.y() + 28, box.width(), 20), Qt.AlignCenter,
                       "裁切模式：拖动四角/四边把手裁切；Esc 或双击退出")
