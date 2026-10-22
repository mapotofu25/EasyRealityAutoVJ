# -*- coding: utf-8 -*-
"""★ `CueFrameSource` —— 编辑器预览的取帧缓存（本节最重要）。

按路径**复用**解码器、支持随机 seek 的取帧缓存。零 Qt，只吐 numpy BGRA 数组。

本模块落实的纪律，每条都对应旧实现真实烧过的代价（架构方案 §4-A）：

  A1 **绝不每帧新建解码器**：同一路径复用容器/解码器实例（`self._src`）。
     旧实现现场事故：容器层 LRU 保护失效 → 每帧新建 + 淘汰 → **CPU 12 核 /
     磁盘 500~750MB/s**（规格书 §6.3）。
     ★ 2026-10-02 扩展为**有界解码器池**（见 A7）——"复用"升级为"**按播放线复用**"，
       但**始终有硬上限**（绝不无限增长）。

  A7 **有界解码器池**（2026-10-02 按用户"预览还是非常卡"另一半原因新增）：
     · 现象：**同一素材切几段、分放多层**（VJ 典型用法）时，各层入点不同却**挤在同一个
       解码器上**，每拍互相 `seek` ⇒ 三层场景实测只有 **15 fps**。
     · 修法：`frame_play/frame/frame_exact` 增加可选 `lane`（**播放线标识**）；
       `_src` 的键从 `path` 变成 **`(path, lane)`** ⇒ **同素材不同入点各持独立解码器**，
       互不打断；**同素材同入点（叠层）落到同一 lane** ⇒ 仍共用（那是同一条播放线）。
     · ★ **严格有界**：`POOL_MAX_PER_PATH`（每路径上限）+ `POOL_MAX_TOTAL`（全局上限），
       超限时 **LRU 逐出并立即 `container.close()`**。实测每个解码器内存：
       **4K ≈ 77 MB / 1080p ≈ 23 MB**（各 +18 个系统句柄），故上限按内存定死，见常量处。
     · `lane=None`（默认）⇒ 键就是 `path` ⇒ **既有调用（缩略图 / 测试）行为完全不变**。

  A2 **失败必须退避**：打开失败记 `self._fail[path] = monotonic() + 2.0`，
     退避期内直接返回 None（规格书 §6.4：失败素材每帧重试一次 → 硬盘狂读）。
     ⚠ **"首帧还没解出来" ≠ "失败"**：打开成功只是暂时拿不到帧时不进退避
     （容器已缓存，不会重复开文件），只有**打不开**才退避。

  A4 **释放**：`release(path)` / `close()` 显式关闭容器，`close()` 后不留任何
     文件句柄（本设计**完全同步、不起任何后台线程**，故关闭即干净）。

  A6 **缩放前置 + 多线程解码**（2026-10-02 按用户"预览还是非常卡"实测新增）：
     · 4K（3840×2160）DXV 素材，预览画布只有几百 px 宽，旧链路却是
       **按原始分辨率解码 → 转全尺寸 BGRA（4K 一帧 ≈ 33MB）→ 交给合成器再缩放**，
       中间数据被放大 ~10 倍；实测 `frame_play` 取帧中位 19ms / P95 37ms ⇒
       超过 33.3ms 的帧预算 ⇒ 掉帧（用户报的"卡"）。
     · **主修**：`frame_play` / `frame` / `frame_exact` 增加 `out_size=(w,h)`（**目标盒**，
       保持宽高比、**绝不放大**），在**解码后立刻**用
       `VideoFrame.reformat(..., interpolation=OUT_INTERP)` 一步**缩放 + 转 BGRA**（libswscale）。
       `out_size=None` ⇒ 原尺寸 ⇒ **既有调用（缩略图等）行为完全不变**。
     · ★ 实测陷阱：`frame.reformat()` **每次调用都新建 swscale 上下文** —— 4K→1440 的 per-call
       写法 p50 16.5ms，**比直接转全尺寸（11.3ms）还慢**！故必须**按尺寸缓存
       `VideoReformatter`**（挂 `_Source.scalers`）——同样的活 p50 7.3ms / P95 10.2ms。
     · **副修**：打开容器后立即（**必须在首次解码前**）设 `stream.thread_type = DECODE_THREAD_TYPE`
       —— DXV 默认 `SLICE`（实测 4K 解码 p50 7.6ms），设 `AUTO`（FRAME|SLICE）后 p50 3.6ms。
       解码是取帧链路最大的一头，这一步把它砍半。

seek 纪律（技术选型 §A2）：
  · 统一 `container.seek(pts, stream=st, backward=True)`（实测 11~36 ms）。
  · ⚠ **禁止 `any_frame=True`** —— 在 mp4 上实测反而慢到 65~819 ms，且落点不精确。
  · 时间戳一律 `time.monotonic()`，**绝不用墙钟**。

★ 三种取帧的**精度差异**（务必看清）：
    `frame(path, sec)`       → 走 **关键帧**快速路径：seek 到不晚于 sec 的最近关键帧，
                               取该关键帧。**落点不精确**（±1 个 GOP），但快。
                               适合"大致就是这一带"的一次性取帧（缩略图）。
                               ⚠ **不适合连续播放**：同一 GOP 内每一帧都是同一张关键帧
                               （画面卡住）。播放请用 `frame_play()`。
    `frame_exact(path, sec)` → 走 **精确**路径：seek 到 sec 之前的关键帧，然后**顺序
                               解码直到时间戳 ≥ sec**，取第一帧 ≥ sec 的帧。精确，但
                               代价是要多解几帧。适合**入点/出点**的精确预览。
    `frame_play(path, sec)`  → ★ **播放**路径：顺序解码**前进**（不 seek），只在回退/
                               大前跳时才 seek；同位置重复取帧复用上一帧。连续播放得到
                               **连续的真实帧**（而不是停在关键帧上）。
    三者对同一秒的差异 == 该秒到其前一个关键帧的距离（通常 0~数帧）。

★ 线程安全（**本节的选择与理由**）：
    UI 侧会从「预览定时器线程」与「后台缩略图线程」两处访问同一个实例。
    本类用**一把 `threading.RLock` 串行化所有取帧操作**（粗粒度）：
      · 好处：实现简单、绝不可能出现"两处同时操作同一容器"导致的解码器状态错乱
        （PyAV 容器**不是**线程安全的）。
      · 代价：不同素材之间也会互相排队。对本项目（单实例、素材几十个、取帧 10~30ms、
        预览 10fps）完全可接受。
    调用契约：**同一实例可从多线程调用，但操作会被串行化**；若调用方另有更高性能要求，
    请自行按路径分片（每路径一个实例）。**绝不要**绕过本类共享底层容器。
"""

from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from .probe import is_gif, kind_of

log = logging.getLogger("ercue.media.frames")

# 打开失败后的退避时长（秒）。规格书 §6.4：旧实现就是 2.0 秒。
RETRY_SEC = 2.0

# ★ 播放取帧（`frame_play`）的阈值/预算：
#   PLAY_FWD_MAX_SEC  —— 目标只比"当前帧"前进 ≤ 这么多秒时，**顺序解码前进**（不 seek）；
#                        超过则重新 seek（避免"跳很远还硬顺序解"白解一大段）。
#   PLAY_REUSE_BACK_SEC —— 目标落在"当前帧"往前这么多秒内时，**复用当前帧**
#                        （同一位置重复取帧不跳帧：音频停住时画面必须停住）。
#   ★ 单次 `frame_play` 的**顺序解码上限**（没有上限时"一次前进接近 1 秒"会顺序解上百帧
#     并逐帧 to_ndarray，实测 1080p DXV **P95 = 211 ms** ⇒ 预览主线程被卡住＝用户报的"卡顿"）。
#     · 前进路径（播放时补帧）：帧数 + 时间双上限，超了就用"最近帧"顶着、下一拍继续追；
#     · 定位路径（回退 / 大跳 / 拖动 scrub）：要**尽量精确**到目标（一次性），只设时间上限。
PLAY_FWD_MAX_SEC = 1.0
PLAY_REUSE_BACK_SEC = 1.0
PLAY_MAX_STEP_FRAMES = 8          # 前进路径：单次顺序解码的帧数上限
PLAY_STEP_BUDGET_SEC = 0.012      # 前进路径：单次顺序解码的时间预算（秒）
PLAY_SEEK_BUDGET_SEC = 0.040      # 定位路径：单次顺序解码的时间预算（秒，防长 GOP 卡顿）

# ★ A6 缩放前置（`out_size`）与多线程解码：
#   OUT_INTERP          —— libswscale 缩放插值。BILINEAR 兼顾质量/速度（4K→1440 p50 7.4ms）；
#                          更高档（BICUBIC/LANCZOS）更贵（p50 10.5/11.8ms）且预览收益很小
#                          （合成器随后还会再缩放一次）。见报告 `_fix_preview_perf2.md` 的画质对比。
#   DECODE_THREAD_TYPE  —— 打开容器后立即设（须在**首次解码前**）。`None`/"" ⇒ 不动（用容器默认）。
#                          DXV 默认 SLICE ⇒ 4K 解码 p50 7.6ms；AUTO（FRAME|SLICE）⇒ p50 3.6ms。
#   DECODE_THREAD_COUNT —— ★ **必须绑定**：`0`/None 会让 ffmpeg 按核数（本机 16）起一大把线程，
#                          顺序解码确实最快（p50 6.8ms），但**每次 seek 都要 flush / 重建线程池**
#                          ⇒ 在"同一素材多层、各层位置不同"（每拍多次 seek）时**反而慢到 62ms/次**！
#                          绑到 2 线程后：顺序解码 p50 10.1ms（仍比不绑快），交替 seek 16.9ms
#                          （远好于不绑的 62ms）。见报告 `_fix_preview_perf2.md` 的调参表。
OUT_INTERP = "BILINEAR"
DECODE_THREAD_TYPE = "AUTO"
DECODE_THREAD_COUNT = 2

# ★ A7 有界解码器池（键 = `(path, lane)`；`lane=None` 时退化为"每路径一个解码器"）：
#   · POOL_MAX_PER_PATH —— **同一素材**最多的并发解码器数。定 3 的理由：
#       目标场景是"同一素材 3 层、各层入点不同"（背景一段 + 前景一段 + 再一段），
#       要 **≥3** 才不互相逐出；每再多一层就 LRU 重开（退化为逐出+重建，绝不无上限新建）。
#       内存：4K ≈ 77 MB/个 ⇒ 3 个 ≈ 230 MB（实测，见报告 `_fix_decoder_pool.md`）。
#   · POOL_MAX_TOTAL   —— **全局**上限（多素材时的安全阀）。6 个 4K 最坏 ≈ 460 MB，
#       对"桌面预览"可接受；超了 LRU 逐出。
#   · POOL_HOT_SEC     —— "热"判据：最近这么多秒内被取过帧的解码器**不许逐出**。
#       ★ 为什么需要它：池满且**所有解码器都在用**时（如"同素材 5 层"），若还按 LRU 硬逐出，
#         就会"每拍逐出→下一拍再重建"——**重建比共用贵得多**（实测 5 层异入点从 12 fps 掉到 4 fps！）。
#         正确做法是**退化为"共用"**（多个 lane 别名到同一个 `_Source`）：画面照样正确，
#         只是回到"一起 seek"的老代价（~12 fps），但**绝不重建、绝不越界**。
#         只有"确实冷掉"（composition 变了、这一层不再取帧）的解码器才关闭、把内存还回去。
POOL_MAX_PER_PATH = 3
POOL_MAX_TOTAL = 6
POOL_HOT_SEC = 0.5


class _Source:
    """一个路径对应的已打开容器（复用单元）。"""
    __slots__ = ("path", "container", "stream", "time_base", "is_image", "static",
                 "static_px", "static_scaled", "static_failed", "scalers",
                 "cur_pts", "cur_frame", "cur_size", "_scale_warned",
                 "loop_dur",                       # ★ 需求4：GIF 的"一轮时长"（秒；非 GIF / 拿不到 = None）
                 "gif_frames", "gif_tried")        # ★ 2026-10-22：GIF 全帧缓存（≤80 帧才缓存）

    def __init__(self, path, container, stream, is_image):
        self.path = path
        self.container = container
        self.stream = stream
        try:
            self.time_base = float(stream.time_base) if stream.time_base else 0.0
        except Exception:                           # noqa: BLE001
            self.time_base = 0.0
        self.is_image = bool(is_image)
        self.static = None                          # 图片/单帧缓存的**原始 VideoFrame**
        self.static_px = None                       # 图片/单帧缓存的**原尺寸** BGRA 数组
        self.static_scaled = {}                     # (w,h) -> 该尺寸的 BGRA 数组（图片按需缩放缓存）
        self.gif_frames = None                       # [(pts, VideoFrame)]（GIF 全帧缓存）
        self.gif_tried = False
        self.static_failed = False                  # 图片解帧失败标记（不反复重解）
        # ★ A6：按目标尺寸缓存的可复用 swscale 缩放器（(w,h) -> VideoReformatter | None）
        self.scalers = {}
        # ★ `frame_play` 的**顺序解码位置**：cur_pts = 最近一帧的 pts（秒），
        #   cur_frame = 该帧的 BGRA，cur_size = 该帧缩放用的目标盒（复用前须尺寸一致）。
        #   任何 seek 都会把它清空（见 `_seek`）。
        self.cur_pts = None
        self.cur_frame = None
        self.cur_size = None
        self._scale_warned = False                  # 缩放失败只记一次日志
        # ★ 需求4（GIF 循环）：仅在 `is_gif(path)` 时于 `_get_source` 里填"一轮时长"；
        #   非 GIF 恒为 None ⇒ 取模环绕分支永不触发（普通视频/图片行为完全不变）。
        self.loop_dur = None



def _new_reformatter():
    """建一个可复用的 swscale 缩放器；失败返回 None（调用方会退化为每次新建）。"""
    try:
        import av
        return av.video.reformatter.VideoReformatter()
    except Exception:                               # noqa: BLE001
        return None


def _fit_size(fw: int, fh: int, out_size):
    """把 `out_size`（目标盒 `(bw,bh)`）落到**保持宽高比、绝不放大**的实际尺寸。

    返回 `(w,h)`；若不需要缩小（源本就 ≤ 目标盒）返回 **None**（= 保原尺寸，不缩放）。
    ★ **必须自己算 fit**：`reformat(width, height)` 会**按给定 w/h 直接拉伸**（不管源比例），
      不先做保持宽高比的换算就会把非 16:9 的素材**拉变形**。
    """
    if not out_size:
        return None
    try:
        bw, bh = int(out_size[0]), int(out_size[1])
    except (TypeError, ValueError, IndexError):
        return None
    if bw <= 0 or bh <= 0 or fw <= 0 or fh <= 0:
        return None
    k = min(bw / float(fw), bh / float(fh))
    if k >= 0.999:                                  # 不需要缩小（甚至更小）→ 保原尺寸、绝不放大
        return None
    w = max(1, int(round(fw * k)))
    h = max(1, int(round(fh * k)))
    if w >= fw and h >= fh:
        return None
    return (w, h)


def _reformat_bgra(frame, size, src=None):
    """把帧**缩放到精确 `size=(w,h)`** 并转连续 BGRA；失败返回 None。

    ★ 缩放器按 `size` 缓存到 `src.scalers`（`frame.reformat()` 每次调用都新建 swscale
      上下文，实测更慢；复用同一 `VideoReformatter` 才快）。
    """
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    sc = None
    if src is not None:
        if size not in src.scalers:
            src.scalers[size] = _new_reformatter()
        sc = src.scalers[size]
    try:
        if sc is not None:
            rf = sc.reformat(frame, width=w, height=h, format="bgra",
                             interpolation=OUT_INTERP)
        else:
            rf = frame.reformat(width=w, height=h, format="bgra",
                                interpolation=OUT_INTERP)
        return np.ascontiguousarray(rf.to_ndarray())
    except Exception:                               # noqa: BLE001
        return None


def _frame_to_bgra(frame, out_size=None, src=None):
    """帧 → 连续 BGRA uint8 数组；失败返回 None（个别 DXV 帧会抛 av.error.OSError）。

    `out_size`（目标盒 `(w,h)`）非空时，在**解码后立即缩放**（保持宽高比、绝不放大）——
    避免"转全尺寸 BGRA（4K ≈ 33MB）再交给合成器缩放"的中间放大（★ A6）。
    `out_size=None` ⇒ **原尺寸**（默认行为，缩略图/其他调用方不受影响）。
    缩放失败时退回原尺寸转换（"最差也有图"，绝不因缩放把整帧弄没）。
    """
    if frame is None:
        return None
    size = _fit_size(int(frame.width), int(frame.height), out_size)
    if size is not None:
        arr = _reformat_bgra(frame, size, src)
        if arr is not None:
            return arr
        log_scale_fail(src)
    try:
        return np.ascontiguousarray(frame.to_ndarray(format="bgra"))
    except Exception:                               # noqa: BLE001
        return None


def log_scale_fail(src) -> None:
    """缩放失败只在**首次**记一条日志（避免坏素材每帧刷屏）。"""
    if src is None or src._scale_warned:
        return
    src._scale_warned = True
    log.warning("取帧缩放到预览尺寸失败，已退回原尺寸（%s）", getattr(src, "path", "?"))


class CueFrameSource:
    """按 **(路径, 播放线)** 复用解码器的取帧缓存（**有界池 + LRU**）。**线程安全（内部 RLock）。**"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._src: dict = {}          # (path, lane) -> _Source     ★ A7：键含 lane
        self._used: dict = {}         # (path, lane) -> 最近使用时刻（LRU 用，monotonic）
        self._fail: dict = {}         # path -> 退避截止的 monotonic 时间（按**路径**退避）
        self._closed = False
        # 观测用计数器：open_attempts[path] = 实际调用 av.open 的次数（含池重建）。
        # A1（复用只 open 一次）/ A2（退避只 open 一次）两条纪律的测试就靠它证明。
        self.open_attempts: dict = {}
        # 观测用：evictions = 因池上限被逐出的解码器次数（只在诊断/验证里看）。
        self.evictions = 0

    # ------------------------------------------------------------------
    # 内部：取/建容器（★ A7 有界池）
    # ------------------------------------------------------------------
    @staticmethod
    def _key(path: str, lane):
        """池键：`(path, lane)`；`lane=None` ⇒ `(path, None)`（等价于"每路径一个解码器"）。"""
        return (path, lane)

    def _close_key(self, key) -> None:
        """关闭并移除池中某个键（**必须持有 `self._lock`**）。

        ★ A7：**别名共用**下同一个 `_Source` 可能被多个键引用 —— 只有**最后一个**引用
          移走时才真正 `container.close()`（否则会把别人正在用的解码器关掉）。
        """
        src = self._src.pop(key, None)
        self._used.pop(key, None)
        if src is None:
            return
        for other in self._src.values():
            if other is src:
                return                                  # 仍被别的 lane 别名引用 ⇒ 不能关
        try:
            src.container.close()
        except Exception:                               # noqa: BLE001
            pass

    def _pool_sources(self, path=None) -> dict:
        """该路径（或全部）下**去重后的** `_Source`：`id(src) -> (src, 最近使用时刻)`。

        ★ 去重很重要：别名共用时 `len(self._src)` 会大于**真实解码器数**，`decoder_count()`
          与池上限都必须按**真实解码器**算。
        """
        out = {}
        for k, s in self._src.items():
            if path is not None and k[0] != path:
                continue
            u = self._used.get(k, 0.0)
            cur = out.get(id(s))
            if cur is None or u > cur[1]:
                out[id(s)] = (s, u)
        return out

    def _make_room(self, path: str, now: float):
        """为新解码器腾位。返回 `(True, None)` = 可新建；`(False, alias_src)` = 池满且都热、**共用**。

        策略（★ A7，全部**必须有界**）：
          1. 该路径未到上限、且全局未到上限 → 直接新建；
          2. 否则挑"**冷**"的（`now - used > POOL_HOT_SEC`）逐出并关闭 —— 把内存真正还回去；
          3. 若**同路径**全都在用（都热）→ **共用**（返回最冷那个源做别名，绝不重建）；
          4. 全局满且都热且是**跨素材** → 只能逐出（**跨素材共用会取错画面，绝不允许**）。
        """
        while True:
            same = self._pool_sources(path)
            if len(same) >= POOL_MAX_PER_PATH:
                cand, allow_alias = same, True
            elif len(self._pool_sources()) >= POOL_MAX_TOTAL:
                cand, allow_alias = self._pool_sources(), False
            else:
                return True, None
            cold = [kv for kv in cand.values() if (now - kv[1]) > POOL_HOT_SEC]
            if cold:
                victim = min(cold, key=lambda kv: kv[1])[0]
                for k in [k for k, s in self._src.items() if s is victim]:
                    self._close_key(k)
                self.evictions += 1
                continue
            if allow_alias:
                return False, min(cand.values(), key=lambda kv: kv[1])[0]
            victim = min(cand.values(), key=lambda kv: kv[1])[0]
            for k in [k for k, s in self._src.items() if s is victim]:
                self._close_key(k)
            self.evictions += 1
            continue

    def _get_source(self, path: str, lane=None):
        """返回该 `(path, lane)` 对应的 `_Source`；不可用返回 None。

        顺序：池中已有 → 命中即复用（**A1/A7**）；退避期内 → 直接 None（**A2**）；
        否则**先腾位（A7 有界；池满且都热则共用）**再尝试打开（失败则记退避）。
        """
        key = self._key(path, lane)
        src = self._src.get(key)
        if src is not None:
            self._used[key] = time.monotonic()       # ★ LRU：每次命中即"刷新鲜度"
            return src

        now = time.monotonic()                       # ★ 一律单调时钟
        until = self._fail.get(path)                 # A2：退避按**路径**（同素材所有 lane 一起退避）
        if until is not None and now < until:
            return None

        if not os.path.isfile(path):
            self._fail[path] = now + RETRY_SEC
            return None

        try:
            import av
        except Exception:                           # noqa: BLE001
            self._fail[path] = now + RETRY_SEC
            return None

        may_new, alias = self._make_room(path, now)  # ★ A7：硬上限；满且都热则共用
        if not may_new:
            self._src[key] = alias                   # 别名：画面正确、只是回到"一起 seek"的代价
            self._used[key] = now
            return alias

        self.open_attempts[path] = self.open_attempts.get(path, 0) + 1
        try:
            # ★ metadata_errors="replace" 不能省（GBK 元数据 .mov）
            container = av.open(path, metadata_errors="replace")
            stream = container.streams.video[0]
        except Exception:                           # noqa: BLE001
            self._fail[path] = now + RETRY_SEC       # A2：打开失败 → 退避 2 秒
            return None

        # ★ A6：开启多线程解码（**必须在首次解码前**设，装到 codec context 上）。
        #   DXV 默认 `SLICE`（实测 4K 解码 p50 7.6ms）→ 设 `AUTO`（FRAME|SLICE）后 p50 3.6ms。
        #   ★ `thread_count` 必须**绑定**（见常量处说明）：按核数自动会在"每拍多次 seek"时
        #      flush/重建线程池、反而慢 4 倍。设不上也不影响取帧（退化为容器默认）。
        if DECODE_THREAD_TYPE:
            try:
                stream.thread_type = DECODE_THREAD_TYPE
                if DECODE_THREAD_COUNT:
                    stream.thread_count = int(DECODE_THREAD_COUNT)
            except Exception:                       # noqa: BLE001
                pass

        src = _Source(path, container, stream, kind_of(path) == "image")
        # ★ 需求4：GIF 记"一轮时长"（取模环绕用）。双来源兜底：stream.duration → container.duration。
        #   拿不到（0）就保持 None ⇒ 不环绕（等于旧行为，不会更糟）。
        if is_gif(path):
            d = 0.0
            try:
                sd = float(stream.duration) if stream.duration else 0.0
                if sd > 0 and src.time_base > 0:
                    d = sd * src.time_base
                elif container.duration:
                    d = float(container.duration) / 1_000_000.0   # av.time_base = 1e6（微秒）
            except Exception:                       # noqa: BLE001
                d = 0.0
            src.loop_dur = d if d > 1e-6 else None
        self._src[key] = src
        self._used[key] = now
        self._fail.pop(path, None)                   # 打开成功 → 清退避记录
        return src

    def _static_frame(self, src: "_Source", out_size=None):
        """图片/单帧素材：只解一次并缓存（seek 对静态图无意义）。

        ★ A6：缓存**原始帧**（`src.static`）+ **原尺寸数组**（`src.static_px`），并按 `out_size`
          缓存缩放结果（`src.static_scaled`）—— 图片逐帧复用，绝不每帧重解 / 重缩。
        """
        frame = src.static
        if frame is None:
            if src.static_failed:
                return None
            try:
                frame = next(iter(src.container.decode(video=0)), None)
            except Exception:                       # noqa: BLE001
                frame = None
            if frame is None:
                src.static_failed = True
                return None
            src.static = frame
            src.static_px = _frame_to_bgra(frame, None, None)     # 原尺寸（out_size=None 时用）
        if not out_size:
            return src.static_px
        key = _fit_size(int(frame.width), int(frame.height), out_size)
        if key is None:
            return src.static_px
        arr = src.static_scaled.get(key)
        if arr is None:                                 # 首次 / 上次失败（**不缓存失败**，可重试）
            arr = _frame_to_bgra(frame, key, src)
            if arr is not None:
                src.static_scaled[key] = arr
        return arr

    def _seek(self, src: "_Source", sec: float):
        """`seek(backward=True)` 到不晚于 sec 的最近关键帧。返回 pts 是否设置成功。

        ★ 任何 seek 都会让 `frame_play` 的顺序解码位置失效（清空 cur_pts/cur_frame/cur_size）——
          这样"另一个调用方（缩略图 / 入出点预览）seek 过"之后，播放路径会重新 seek，
          **绝不会**把陈旧的位置当成"当前帧"。
        """
        if src.time_base <= 0:
            return False
        try:
            src.container.seek(int(float(sec) / src.time_base),
                               stream=src.stream, backward=True)
            src.cur_pts = None
            src.cur_frame = None
            src.cur_size = None
            return True
        except Exception:                           # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_out_size(out_size):
        """把目标盒规范化成 `(int,int)` 或 `None`（便于作为缓存键比较；非法值一律 None）。"""
        if not out_size:
            return None
        try:
            w, h = int(out_size[0]), int(out_size[1])
        except (TypeError, ValueError, IndexError):
            return None
        return (w, h) if (w > 0 and h > 0) else None

    def frame(self, path: str, sec: float, out_size=None, lane=None):
        """取该素材第 `sec` 秒的帧，返回 **BGRA** `np.ndarray`(h,w,4) 或 None。

        **关键帧级精度**（seek 落点 + 取该帧），快，适合预览。精确取帧见
        `frame_exact`。失败/不可用一律返回 None，**绝不抛异常**。

        `out_size=(w,h)`（★ A6，可选）：**目标盒**，解码后立即按此**保持宽高比缩放**
        （绝不放大）；`None`（默认）⇒ 原尺寸。
        `lane`（★ A7，可选）：**播放线标识**；不同 lane 各持独立解码器（有界池）。
        `None`（默认）⇒ 每路径一个解码器（既有行为）。
        """
        return self._get_frame(path, sec, exact=False, out_size=out_size, lane=lane)

    def frame_exact(self, path: str, sec: float, out_size=None, lane=None):
        """取该素材第 `sec` 秒的**精确帧**（返回 BGRA 数组或 None）。

        做法：`seek(backward=True)` 到 sec 之前的关键帧，然后**顺序解码**，取第一个
        时间戳 ≥ `sec` 的帧。用于**入/出点**的精确预览。比 `frame()` 略慢。
        `out_size` / `lane` 语义同 `frame()`。
        """
        return self._get_frame(path, sec, exact=True, out_size=out_size, lane=lane)

    def frame_play(self, path: str, sec: float, out_size=None, lane=None):
        """★ **播放取帧**：优先**顺序解码前进**（复用同一解码器），绝不每帧 seek。

        为什么需要它（用户真实反馈："预览内开始后视频无法正常播放，被拉长减速了"）：
          `frame()` 每次调用都 `seek` 回**不晚于 sec 的关键帧**再取一帧 ⇒ 同一个 GOP
          内的所有时刻都会拿到**同一张关键帧** ⇒ 连续播放时画面"卡住不动、只是偶尔跳一下"，
          看起来就是**被拉长 / 减速**。
          `frame_play()` 改为：目标只比"当前帧"前进 ≤ `PLAY_FWD_MAX_SEC` 时**顺序解码下一帧**，
          于是连续播放得到的是**连续的真实帧**；只在"回退 / 大前跳"时才 seek。
          同一位置（或略回退）重复取帧 → **复用上一帧**，保证音频停住时画面也停住、不跳帧。

        ★ 单次顺序解码**有上限**（`PLAY_MAX_STEP_FRAMES` / `PLAY_STEP_BUDGET_SEC`）：超了就用
          "最近帧"顶着、下一拍继续追 —— 避免"一次前进近 1 秒 → 解上百帧 → 卡预览主线程"。

        ★ `out_size=(w,h)`（★ A6，可选）：**目标盒**，解码后立即按此**保持宽高比缩放**
          （绝不放大）；`None`（默认）⇒ 原尺寸（既有调用行为不变）。预览用它把 4K 帧
          在**解码后立刻**缩到预览尺寸，避免"转全尺寸 BGRA（33MB）→ 合成器再缩放"的浪费
          （实测 4K 取帧中位 19ms → 7ms）。
        ★ `lane`（★ A7，可选）：**播放线标识**。同一素材切多段、分放多层时，各层传入
          **不同的 lane**（预览用图层标识）⇒ 各持**独立解码器**、互不 seek，实测三层
          异入点场景 **15 → 30 fps**。默认 `None` ⇒ 每路径一个解码器（既有行为不变）。
        失败 / 不可用一律返回 None，**绝不抛异常**。
        """
        path = str(path or "")
        if not path or self._closed:
            return None
        out_size = self._norm_out_size(out_size)
        with self._lock:                             # ★ 串行化：不允许并发操作同一容器
            src = self._get_source(path, lane)
            if src is None:
                return None
            if src.is_image:
                return self._static_frame(src, out_size)

            target = float(sec)
            # ★★ 2026-10-22：**GIF 必须走精确取帧**。
            #   实测（W://qq 的真实 GIF）：`frame_play` 的顺序/复用机制对 GIF 会取错帧
            #   （t=0.20 返回的其实是第 0 帧，40 个采样点里 24 个错帧）⇒ 预览"内容一闪一闪"。
            #   原因：GIF 没有关键帧，seek 经常落回起点，而"复用上一帧"的分支会返回陈旧帧。
            #   GIF 体积小、帧数少，精确解码代价可接受，因此这里直接走精确路径。
            if src.loop_dur is not None:
                try:
                    target = float(target) % float(src.loop_dur)
                except Exception:                            # noqa: BLE001
                    pass
                return self._gif_frame(src, target, out_size)

            # ① 同一位置（或往前一点）重复取帧 → 复用上一帧（绝不再解码，避免跳帧）。
            #    ★ 仅当**尺寸一致**才复用：换了 out_size（控件 resize）时旧帧分辨率不符，
            #      必须重新取一帧新的（否则尺寸会一直卡在旧值）。
            if (src.cur_frame is not None and src.cur_pts is not None
                    and src.cur_size == out_size
                    and src.cur_pts - PLAY_REUSE_BACK_SEC <= target <= src.cur_pts + 1e-6):
                return src.cur_frame
            # ② 只比当前帧前进一点点 → 顺序解码前进（不 seek）
            forward = (src.cur_pts is not None
                       and 1e-6 < (target - src.cur_pts) <= PLAY_FWD_MAX_SEC)
            if not forward:
                if not self._seek(src, target):      # 回退 / 大前跳 / 尚未定位 → seek
                    return self._decode_first(src, out_size)   # 退化：拿不到 time_base → 顺序取首帧
                # ★ 定位路径：尽量**精确**到 target（一次性重定位），只加时间预算防长 GOP 卡顿。
                return self._decode_until(src, target, None, PLAY_SEEK_BUDGET_SEC, out_size)
            # ★ 前进路径（播放补帧）：帧数 + 时间双上限，超了用"最近帧"顶着，下一拍继续追 ⇒
            #   画面连续、主线程不卡（绝不因为一次掉帧就顺序解上百帧）。
            return self._decode_until(src, target, PLAY_MAX_STEP_FRAMES,
                                      PLAY_STEP_BUDGET_SEC, out_size)

    def _gif_frame(self, src: "_Source", target: float, out_size=None):
        """GIF 专用：整段解码一次进内存，之后按时间戳直接取（避免每帧重解）。"""
        if not src.gif_tried:
            src.gif_tried = True
            frames = []
            try:
                import av as _av
                with _av.open(src.path) as c:
                    st = c.streams.video[0]
                    tb = float(st.time_base)
                    for fr in c.decode(video=0):
                        frames.append((float(fr.pts or 0) * tb, fr))
                        if len(frames) > 80:               # 超长 GIF 不缓存，退回精确路径
                            break
            except Exception:                              # noqa: BLE001
                frames = []
            if 0 < len(frames) <= 80:
                src.gif_frames = frames
        frames = src.gif_frames
        if not frames:
            return self._exact_locked(src, target, out_size)
        for pts, fr in frames:
            if pts + 1e-6 >= target:
                return _frame_to_bgra(fr, out_size, src)
        return _frame_to_bgra(frames[-1][1], out_size, src)

    def _exact_locked(self, src: "_Source", target: float, out_size=None):
        """已持锁的精确取帧（GIF 专用；等价 `frame_exact` 的核心）。"""
        if not self._seek(src, target):
            return self._decode_first(src, out_size)
        try:
            tb = src.time_base
            eps = 1e-6
            last = None
            for fr in src.container.decode(video=0):
                if fr is None:
                    continue
                if fr.pts is None:
                    src.cur_pts, src.cur_frame, src.cur_size = None, None, None
                    return _frame_to_bgra(fr, out_size, src)
                p = float(fr.pts) * tb
                if p + eps >= target:
                    arr = _frame_to_bgra(fr, out_size, src)
                    src.cur_pts, src.cur_frame, src.cur_size = p, arr, out_size
                    return arr
                last = fr
            # 末帧区间（GIF 常见：末帧显示到一轮结束）
            if last is not None:
                arr = _frame_to_bgra(last, out_size, src)
                src.cur_pts, src.cur_frame, src.cur_size = None, arr, out_size
                return arr
            return None
        except Exception:                                    # noqa: BLE001
            return None

    def _decode_first(self, src: "_Source", out_size=None):
        """退化的取首帧（仅在 seek 不可用时）。"""
        try:
            fr = next(iter(src.container.decode(video=0)), None)
        except Exception:                            # noqa: BLE001
            return None
        src.cur_pts, src.cur_frame, src.cur_size = None, None, None
        return _frame_to_bgra(fr, out_size, src) if fr is not None else None

    def _decode_until(self, src: "_Source", target: float,
                      max_frames=None, budget_sec=None, out_size=None):
        """从解码器**当前位置**顺序解码，返回第一帧 `pts ≥ target` 的 BGRA（或 None）。

        ★ **有界**：`max_frames` / `budget_sec` 任一超限即**提前收工**，返回此刻"最近的帧"
          （而不是 None —— 返回黑帧更难看）。这是为了防"一次前进近 1 秒 → 顺序解上百帧
          → 主线程卡 ~200ms"（实测 1080p DXV 一次前进 1.0s 的 P95 = 211ms）。
          调用方下一拍会从"最近帧"继续顺序前进，几拍内自然追上（画面连续、不跳黑）。
        ★ **只对要返回的那一帧**做 `to_ndarray`（中间帧一律不转，且**一步缩放到 `out_size`**）
          —— 省掉大量无用转换（4K 帧转一次就是 ~33MB，见 ★ A6）。
        顺带把"当前位置"记进 `src.cur_pts/cur_frame/cur_size`，供下次顺序前进 / 复用。
        """
        tb = src.time_base
        t0 = time.monotonic()
        n = 0
        try:
            for fr in src.container.decode(video=0):
                if fr is None:
                    continue
                n += 1
                if fr.pts is None:
                    src.cur_pts, src.cur_frame, src.cur_size = None, None, None
                    return _frame_to_bgra(fr, out_size, src)
                p = float(fr.pts) * tb
                if p + 1e-6 >= target:                       # 到达目标 → 转换并返回
                    arr = _frame_to_bgra(fr, out_size, src)
                    src.cur_pts, src.cur_frame, src.cur_size = p, arr, out_size
                    return arr
                # 未到目标：查预算（帧数 / 时间），超了就用"最近一帧"兜底
                if ((max_frames is not None and n >= max_frames)
                        or (budget_sec is not None
                            and (time.monotonic() - t0) >= budget_sec)):
                    arr = _frame_to_bgra(fr, out_size, src)
                    if arr is not None:
                        src.cur_pts, src.cur_frame, src.cur_size = p, arr, out_size
                    return arr
            src.cur_pts, src.cur_frame, src.cur_size = None, None, None
            return None
        except Exception:                            # noqa: BLE001
            src.cur_pts, src.cur_frame, src.cur_size = None, None, None
            return None

    def _get_frame(self, path: str, sec: float, exact: bool, out_size=None, lane=None):
        path = str(path or "")
        if not path or self._closed:
            return None
        out_size = self._norm_out_size(out_size)
        with self._lock:                             # ★ 串行化：不允许并发操作同一容器
            src = self._get_source(path, lane)
            if src is None:
                return None
            if src.is_image:
                return self._static_frame(src, out_size)

            # ★ 需求4：GIF 环绕 —— 对播放位置按"一轮时长"取模 ⇒ 时间轴多长就循环多久。
            #   ⚠ 只对 `loop_dur` 非 None（= GIF 且成功取到时长）生效；普通视频/图片此分支不触发。
            #   ⚠ 配套约定（`probe.is_gif` 文档）：GIF 片段**不设 `src_out`** ——
            #     否则模型侧"到出点即冻结"会先一步定住，环绕就到不了。
            if src.loop_dur:
                try:
                    sec = float(sec) % src.loop_dur
                except Exception:                   # noqa: BLE001
                    pass

            if not self._seek(src, sec):
                # 拿不到 time_base / seek 失败：退回顺序取首帧（不动退避记录）
                try:
                    return _frame_to_bgra(next(iter(src.container.decode(video=0)), None),
                                          out_size, src)
                except Exception:                   # noqa: BLE001
                    return None

            try:
                if not exact:
                    # 快速路径：关键帧后第一帧
                    fr = next(iter(src.container.decode(video=0)), None)
                    return _frame_to_bgra(fr, out_size, src) if fr is not None else None

                # 精确路径：顺序解到时间戳 ≥ sec
                tb = src.time_base
                target = float(sec)
                eps = 1e-6
                last = None                          # ★ 记住最后一个 pts < target 的帧（GIF 末帧区间用）
                for fr in src.container.decode(video=0):
                    if fr.pts is None:
                        return _frame_to_bgra(fr, out_size, src)
                    if fr.pts * tb + eps >= target:
                        return _frame_to_bgra(fr, out_size, src)
                    last = fr
                # ★ 需求4：GIF 环绕时会撞上"**末帧的显示区间**"——末帧 pts = 一轮时长 − 帧时长，
                #   而该帧要一直显示到"一轮时长"。此时没有"pts ≥ target"的帧可等 ⇒
                #   若不回退就会返回空帧（实测 t=0.25 于 3×100ms 的 GIF 上为 None）。
                #   ⇒ 仅对 GIF（loop_dur 非 None）回退用最后一帧；普通视频行为不变。
                if src.loop_dur is not None and last is not None:
                    return _frame_to_bgra(last, out_size, src)
                return None
            except Exception:                       # noqa: BLE001
                return None

    def release(self, path: str | None = None) -> None:
        """释放解码器：`path=None` 释放全部，否则只释放该路径（**含该路径的所有 lane**）（**A4/A7**）。"""
        with self._lock:
            if path is None:
                keys = list(self._src.keys())
            else:
                keys = [k for k in self._src if k[0] == path]
            for k in keys:
                self._close_key(k)

    def close(self) -> None:
        """全部释放，关闭时必调（**A4/A7**）。close 后再取帧返回 None。

        本类**完全同步、不起后台线程**，故 close() 之后不留后台线程、不留文件句柄。
        """
        self.release(None)
        with self._lock:
            self._closed = True

    # ---- 诊断 ----
    @property
    def open_count(self) -> int:
        """累计实际打开次数（所有路径）。"""
        return sum(self.open_attempts.values())

    def open_count_of(self, path: str) -> int:
        """指定路径实际被 open 的次数（测 A1/A2 用）。"""
        return int(self.open_attempts.get(str(path or ""), 0))

    def decoder_count(self, path: str | None = None) -> int:
        """当前池中**真实解码器**数：`None` = 全部，否则只数该路径（**A7 有界性**的判据）。

        ★ 按**去重的 `_Source`** 计（别名共用时 `len(self._src)` 会偏大）。
        """
        with self._lock:
            return len(self._pool_sources(path))

    def lane_count(self) -> int:
        """当前池中**键（lane）**数：可能大于 `decoder_count()`（别名共用）。"""
        with self._lock:
            return len(self._src)

    def cached_paths(self) -> list:
        """当前持有解码器的**去重路径**列表（诊断/测试用；A1 断言"只 1 个解码器"用它）。"""
        with self._lock:
            seen, out = set(), []
            for k in self._src:
                if k[0] not in seen:
                    seen.add(k[0])
                    out.append(k[0])
            return out

    def cached_keys(self) -> list:
        """当前池中所有键 `(path, lane)`（诊断用）。"""
        with self._lock:
            return list(self._src.keys())


def selftest() -> bool:
    """无素材自检：坏路径退避生效；`out_size` / `lane` 参数不破坏默认行为。"""
    src = CueFrameSource()
    p = "__definitely_not_exist__.mp4"
    for _ in range(50):
        assert src.frame(p, 0.0) is None
    assert src.open_count_of(p) == 0, "坏路径不该真的 open"
    assert src.frame(p, 1.0) is None
    assert src.frame_play(p, 0.0) is None        # 播放路径同样：坏素材绝不抛、不退避
    assert src.frame_play(p, 0.0, out_size=(320, 180)) is None   # ★ A6：带目标盒同样安全
    assert src.frame(p, 0.0, out_size=(320, 180)) is None
    # ★ A7：多个 lane 的坏路径也不该真的 open、也不该建出任何解码器
    for ln in ("L0", "L1", "L2"):
        assert src.frame_play(p, 0.0, lane=ln) is None
    assert src.decoder_count() == 0, "坏路径不该建解码器"
    assert src.open_count_of(p) == 0
    src.close()
    assert src.frame(p, 0.0) is None             # close 后不再工作
    assert src.frame_play(p, 0.0) is None
    assert src.frame_play(p, 0.0, out_size=(320, 180)) is None
    print("[frames] 自检通过 ✓  退避生效（坏路径 0 次 open）；close 后不再取帧；"
          "out_size / lane 安全（池 0 解码器）")
    return True


if __name__ == "__main__":
    selftest()
