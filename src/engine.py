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
import collections
import copy
import hashlib
import math
import os
import queue
import random
import threading
import time
import uuid

import cv2
import numpy as np

from PySide6.QtCore import QObject, QTimer, Signal, Qt, QRectF, Slot, QMetaObject
from PySide6.QtGui import QImage, QPainter, QColor

from media_manager import make_player, _STALL_SECS
from match_engine import match_clips
from beatgrid import GridClock
import sections                     # 能量段落表（曲库预设，见 src/sections.py）
import output_model                 # 多屏输出配置（纯数据模型）
from fp import hint_window_frames
from cue_position import CuePositionTracker, choose_sheet   # 位置层（指纹→拍位，带回滞）
from cue_seek_policy import CueSeekPolicy      # 码表 seek 策略（纯逻辑，见 tools/cue_should_seek_test.py）
from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,
                      DYNAMIC_FLICKER, DYNAMIC_TAGS)


_LOCK_TIMEOUT = 2.0     # GUI 入口等引擎锁的上限（正常等一帧 <50ms，2 秒已极宽容）

# 卡帧看门狗熔断参数（2026-10-01 现场卡死事故后加，见 `_heal_stalled`）：
#   旧实现每个 tick 都可能同步重建解码器（开容器 = I/O + 起线程），一旦判据误判
#   就是"重建风暴"⇒ tick 速率崩塌 ⇒ 卡死。下面三条是硬限流，宁可少救也不能失控。
_HEAL_CLIP_COOLDOWN = 5.0     # 同一 (图层, 素材) 两次自动重建的最小间隔（秒）
_HEAL_MAX_PER_CLIP = 3        # 同一素材累计自动重建次数上限（到顶后放弃，换素材重置）
_HEAL_GLOBAL_INTERVAL = 2.0   # 全局：每这么久最多自动重建 1 次（秒）
# 健康素材的看门狗**检查**节流：冷却闸门只在"重建过"后才闸得住，健康素材从不重建 ⇒
# 旧实现每 tick 都会走到 `stalled()`（QA 实测 30s@20Hz = 600 次）。这里每图层最多每
# `_HEAL_CHECK_INTERVAL` 秒检查一次（20Hz 下 ≈ 2Hz），把热路径代价压下来。它只限检查频率，
# **不是**冷却 —— 换素材时**不需要**重置（重置只会让换素材瞬间多放行一次，无意义）。
_HEAL_CHECK_INTERVAL = 0.5

# 渲染主循环异常日志（`_tick` 的 except）的限流间隔（秒）：首次必记，之后每这么久最多记一次。
#   原实现是 `if _err_log_n <= 5` —— 第 6 次起**永久静默**；而持久异常会让 `_tick_locked`
#   中途退出 ⇒ 预览 / NDI / Spout 静默冻结，且日志里再没有任何线索（FINAL_REPORT P2）。
_ERR_LOG_MIN_INTERVAL = 30.0

# GUI 无锁状态快照里 `next_clips`（每图层「下一个素材预看」）的重算间隔（秒）。
#   `next_clip_for` 内部本就「每 2 秒才换一次预估」（见其注释），所以 0.25s 刷新与每帧刷新
#   **结果完全相同**，却省掉每帧对所有图层跑 `_candidate_indices`（大基数素材库下不便宜）。
_UI_NEXT_CLIP_INTERVAL = 0.25

# 静默冻帧兜底（见 `_heal_silent`）：`_heal_stalled` 看不见的那几类故障
#   （`lay.player` 不在缓存 ⇒ 永不被激活 / `lay.player is None` / 有帧前就 ended）。
_SILENT_GRACE = 1.0          # player 缺失 / 未激活 持续超过这么久才动作（秒）
_SILENT_ACTION_MIN = 2.0     # 同一图层两次"动作"（重建 / 激活）的最小间隔（秒）
_SILENT_ENDED_EVERY = 5.0    # "有帧前就 ended" 这类：每这么久记一行日志（去重、不刷屏）

# 「从未出帧」兜底（见 `_heal_silent` 第 2.5 类）：
#   现场 2026-10-02：可见且当前素材的真解码器**长时间 `_frames==0`**（layer_health.log 里
#   连续 8s `frames=0 age=-`），画面冻住而 `_heal_stalled` 因"从未出帧"提前返回、`_heal_silent`
#   又没有覆盖这一类 ⇒ 无人恢复。这里给它一个兜底重建：宽限后重建解码器，且**限流**
#   （每图层 ≥2s 一次、同素材累计 ≤3 次），到顶只记一行日志、不再重建（避免重建风暴）。
_NOFRAME_GRACE = 2.2        # 可见 + 当前素材 + `_frames==0` 持续这么久 ⇒ 判定"解不出首帧"
# ★★ 大素材宽限（2026-10-29 现场「无帧重建」翻案）：同样的判据对 4K / 大文件太紧。
#   HDD 上冷打开实测 127~543ms（tools/_probe_cv2_vs_av.py：`2020.5.13.mov` 542.6ms、
#   `搬运工 (38).mov` 126.9ms），再叠加首帧解码就很容易越过 2.2s ⇒ 被误判成"解不出
#   首帧" ⇒ 重建解码器 ⇒ **又要把同一份数据从机械盘重新读一遍** ⇒ 越治越卡
#   （现场 ui_stall.log：同一素材 2.2s/2.2s/2.3s 连判三次）。
#   宽限按文件体量给：超过 150MB 的素材给 5.0s。
_NOFRAME_GRACE_BIG = 5.0
_NOFRAME_BIG_BYTES = 150 * 1024 * 1024

# 「多层同时停帧」取证（见 `_stutter_watch`）：
#   用户报"预览和输出都一卡一卡"时，`layer_health.log` 里只能看到某个时刻 age 变大，
#   但**分不清**是"单层素材解不出来"还是"整个渲染/取帧都停了"——这两种的修法完全不同。
#   这里在常态心跳里主动判定：可见且活跃的图层有 ≥2 层的帧龄超过 `_STUTTER_AGE`
#   ⇒ 记一行到 ui_stall.log，附每层 age / frames / 素材名，下次卡了直接看这条。
_STUTTER_AGE = 0.8
_STUTTER_LOG_EVERY = 10.0

# 掉锁后「冻结段位」多久（秒）。见 `_section_tick` 的 2026-10-30 复盘。
_SEC_HOLD_SEC = 12.0
_SILENT_NOFRAME_COOLDOWN = _SILENT_ACTION_MIN   # 同图层两次重建的最小间隔（≥2s/图层）
_SILENT_NOFRAME_MAX = 3     # 同素材累计重建上限（≤3 次/素材，换素材重置）

# 每层健康快照日志（见 `_LayerHealthLog` / `AutoVJEngine._health_tick`）：
#   2026-10-01 用户又实测到"画面卡住但 `ui_stall.log` 47 分钟一条没有"（看门狗完全盲）。
#   看门狗只在"判定卡帧"时才写日志，判据没命中就永远沉默。这里改成**只要在跑就每秒留一行**
#   每层状态——下次用户说"卡了"，直接回看那个时刻的 `layer_health.log` 就能一眼分辨是
#   "播放器没了 / 不在缓存 / 不活跃 / 从未出帧 / age 在涨" 哪一种，不用再猜。
_HEALTH_LOG_INTERVAL = 1.0    # 采样间隔：每秒 1 行（成本极低：只读属性 + 一次 append）
_HEALTH_MAX_LINES = 600       # 环形上限（约 10 分钟）；超出就裁掉最老的
_HEALTH_KEEP_LINES = 300      # 触发裁剪时保留的最新行数（=上限/2，摊薄重写频率，别每秒重写）


# 库内 BPM 与实时 BPM 的「谐波修正」容差。实时值折到 0.5×/1×/2× 后，
# 与库内值相差超过这个比例 ⇒ 判定"库内值不适用当前播放"（变速 / 认错歌 / 拍钟卡死），
# 保持实时值。6% 能盖住 BPM 检测的正常抖动，又能挡住 190 vs 138（差 27%）这种不谐波关系。
LIB_HARM_TOL = 0.06


def content_speed(snap, sync_on=True):
    """素材播放倍速 = 参照 BPM / 128（夹在 0.3~2.5）。

    ★★★ 2026-11-02 现场取证（OBS 22:20–23:10，Funkot 实际 **190 BPM**）：
      实时自相关在现场粒度（block=1323 ⇒ 33.3 帧/秒）下锁到 **125.5**（偶尔 95.5），
      素材倍速只有 0.75~0.98×（应为 1.48×）—— 用户说的"素材速度没跟上来"。
      根因是自相关这个**信号本身**分不出：190 的周期 10.53 帧落在整数格之间，
      而 4/4 的 kick 每 2 拍一次 ⇒ 最强的周期是 95 BPM。详见 `bpm_anchor` 模块头。

    ⇒ 参照值的优先级：
      ① **离线锚点 `bpm_anchor`**（`beatgrid.analyze_bpm_only`，同一段音频稳定给 190.1）
      ② 库内 BPM（只在**谐波对得上**时采信，见 `LIB_HARM_TOL`）
      ③ 实时 `bpm`（兜底，逐值同改前）

    ⚠ 锚点也要过一道合理性检查：与实时值比值落在 [0.4, 2.5] 之外说明两者严重分歧
      （可能是换歌瞬间的旧锚点），此时退回实时值，宁可慢也不让锚点带飞。
    ⚠ 这里**只影响素材播放倍速**，不参与拍钟 / 切素材节奏。

    ★★★ 2026-11-04（用户：「歌扫出来是 180 BPM，演出时加速到 200，那素材播放速度按
      180 还是 200？」）—— **按 200**。`lib_bpm` 是**原曲** BPM，DJ 推到 1.20× 后
      现场听到的是 `lib × r`；素材要对齐的是"耳朵听到的拍速"，所以
      「只能用库内值」这一条分支必须乘实测倍率 `recognized_rate`。

      真机取证（`energy_log` CSV，1.20× 相位 库内 140 / 听感 168）：
        19:11:23–19:11:44 共 **21 秒**  lib=140  bpm=0  bpm_anchor=0  →  spd=**1.094**，
        而同一时刻位置层指纹已经算出 r=1.196 ⇒ 正确值 140×1.196/128 = **1.308**。
        那 21 秒素材慢了 20%（用户报的"素材速度没跟上来"就是这一段）。
        根因：`bpm` 还没收敛 ⇒ 走 lib 分支 ⇒ 只做 `lib/128`，倍率被整体漏掉。

      ⚠⚠ 只改那**一条**分支。另外两条一个数都不许动：
        · **`bpm_anchor` 分支**：锚点是离线对整段音频测出来的**听感值**，本身已含变速
          （1.20× 时锚点给的就是 172.3 而不是 140）⇒ 再乘 r 是**重复计速**。
        · **`live / 128` 兜底**：`bpm` 是实时听感值，同理不许乘。
      ⚠ 倍率的容错与显示层（`ui_main._BPM_RATE_*` / `_rate_text`）**同口径**：
        非数 / None / bool / NaN / 越界 [0.78, 1.25] ⇒ 当 1.0；
        `|r-1| < 1%` ⇒ 当 1.0（死区防抖，和 KVJ「差不到 1% 不改速度」一致）。
        于是 1.0× 正常播放时素材倍速与改前**逐值一致**（回归零变化）。
    """
    d = snap or {}
    try:
        live = float(d.get("bpm") or 0.0)
    except (TypeError, ValueError):
        live = 0.0
    try:
        lib = float(d.get("lib_bpm") or 0.0)
    except (TypeError, ValueError):
        lib = 0.0
    try:
        anchor = float(d.get("bpm_anchor") or 0.0)
    except (TypeError, ValueError):
        anchor = 0.0
    # ★ 2026-11-04：实测变速倍率。容错风格与上面三个 try 块一致（同一位置、同一形态），
    #   另外必须显式挡掉四类"看着像数其实不是倍率"的输入：
    #     · None / 缺键 / 空串 / 非数字字符串  ⇒ 没有倍率信息
    #     · bool（`True` 当 1.0 只是碰巧对，语义上不是倍率）
    #     · NaN（`nan` 是 truthy，`x or 1.0` 挡不住）
    #     · 越界 [0.78, 1.25]（`audio_engine` 发布前已钳过一次，这里是显示/使用层的
    #       二次兜底，防的是读到别的中间态 / 旧快照）
    #   死区 0.01 与 `ui_main._BPM_RATE_DEADBAND` 同值：1.0× 播放时 r 有 ±0.005 量级
    #   的噪声，不过死区素材倍速会跟着 1.093 → 1.099 → 1.088 地抖（现场能看出来）。
    _r_min, _r_max, _r_dead = 0.78, 1.25, 0.01
    rate = 1.0
    try:
        _r = d.get("recognized_rate", 1.0)
        if not isinstance(_r, bool) and isinstance(_r, (int, float)):
            _r = float(_r)
            if _r == _r and _r_min <= _r <= _r_max and abs(_r - 1.0) >= _r_dead:
                rate = _r
    except (TypeError, ValueError):
        rate = 1.0
    if not sync_on:
        return 1.0

    # ① 离线锚点（最可信）
    if anchor > 0.0:
        if live <= 0.0:
            return max(0.3, min(2.5, anchor / 128.0))
        ratio = live / anchor
        if 0.4 <= ratio <= 2.5:
            return max(0.3, min(2.5, anchor / 128.0))
        # 分歧过大 ⇒ 锚点可能是上一首的，退回实时

    if live <= 0.0 and lib <= 0.0:
        return 1.0
    if live <= 0.0:
        # 实时还没测出来 ⇒ 只能用库内值。
        # ★ 2026-11-04：库内值是**原曲** BPM，必须乘实测倍率才是"耳朵听到的拍速"
        #   （1.20× ⇒ 140 × 1.196 / 128 = 1.308，而不再是 1.094）。详见 docstring。
        return max(0.3, min(2.5, lib * rate / 128.0))
    # ② 库内 BPM 只做谐波修正
    if lib > 0.0:
        cands = (live * 0.5, live, live * 2.0)
        best = min(cands, key=lambda x: abs(x - lib))
        if abs(best - lib) / lib <= LIB_HARM_TOL:
            return max(0.3, min(2.5, lib / 128.0))
    # ③ 实时值兜底
    return max(0.3, min(2.5, live / 128.0))


# 库内 BPM 与实时 BPM 的「谐波修正」容差。实时值折到 0.5×/1×/2× 后，
# 与库内值相差超过这个比例 ⇒ 判定"库内值不适用当前播放"（变速 / 认错歌 / 拍钟卡死），
# 保持实时值。6% 能盖住 BPM 检测的正常抖动，又能挡住 190 vs 138（差 27%）这种不谐波关系。
LIB_HARM_TOL = 0.06


def _health_log_path():
    """每层健康快照日志路径：`%LOCALAPPDATA%\\AutoVJ\\layer_health.log`。"""
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "layer_health.log")


_CLIP_LOG_LOCK = threading.Lock()


def _clip_calls_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "clip_calls.log")


def _log_clip_call(lay, item):
    """记录一次真正切到的素材（供统计“哪些素材从没被调用过”）。"""
    try:
        path = str(getattr(item, "path", "") or "")
        if not path:
            return
        line = "[%s] layer=%s pool=%d file=%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), getattr(lay, "name", "?"),
            len(getattr(lay, "clips", None) or []), path)
        with _CLIP_LOG_LOCK:
            with open(_clip_calls_path(), "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:                                          # noqa: BLE001
        pass


class _LayerHealthLog:
    """每层健康快照的**环形**日志文件（最多 `_HEALTH_MAX_LINES` 行，超出裁掉最老的）。

    设计要点（都为了"取证绝不能反过来拖累演出"）：
      · **只在 `self.running` 时**由引擎线程写，每秒 1 行；不做高频 open/close —— 持有句柄，
        进程退出时 `close()`（即便不 close，因为每行都 flush，崩溃也不丢数据）。
      · 达上限后**重写文件只保留最新 `_HEALTH_KEEP_LINES` 行**（摊薄：每 ~300 行才重写一次，
        把文件大小控制在 300~600 行之间，而不是每秒整文件重写）。
      · 任何异常一律吞掉：取证日志绝不能影响演出。
    """

    def __init__(self, path=None, max_lines=_HEALTH_MAX_LINES, keep=_HEALTH_KEEP_LINES):
        self.path = path or _health_log_path()
        self.max_lines = max(10, int(max_lines))
        self.keep = max(5, min(int(keep), self.max_lines))
        self.lines = collections.deque(maxlen=self.max_lines)
        self.fh = None
        self.n = 0          # 文件中当前行数
        self._q = queue.Queue(maxsize=256)
        self._closing = False
        self._thr = None
        self._open()
        if self.fh is not None:
            self._thr = threading.Thread(target=self._writer, name="autovj-health-log",
                                         daemon=True)
            self._thr.start()

    def _open(self):
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            # 读入上次退出留下的内容（只留最新 max_lines 行），供裁剪与 append 续写
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8", errors="replace") as r:
                        for ln in r:
                            self.lines.append(ln.rstrip("\n"))
                            self.n += 1
                except Exception:                                      # noqa: BLE001
                    self.lines.clear()
                    self.n = 0
            if self.n > self.max_lines:
                self._rewrite(self.keep)     # 上次退出时留下的超长文件，启动先裁一次
            self.fh = open(self.path, "a", encoding="utf-8")
        except Exception:                                              # noqa: BLE001
            self.fh = None

    def _rewrite(self, nkeep):
        """把文件重写为最新 `nkeep` 行（用同一句柄 seek+truncate，避免反复 open）。"""
        try:
            keep_lines = list(self.lines)[-nkeep:]
            if self.fh is not None:
                self.fh.seek(0)
                self.fh.truncate()
                for ln in keep_lines:
                    self.fh.write(ln + "\n")
                self.fh.flush()
            else:
                with open(self.path, "w", encoding="utf-8") as f:
                    for ln in keep_lines:
                        f.write(ln + "\n")
            self.n = len(keep_lines)
        except Exception:                                              # noqa: BLE001
            pass

    def write(self, line):
        """渲染线程只入队；真正的 write/flush 在后台线程。

        ★ 2026-11-08（码表演出掉帧）：真机 cProfile 里 6 次
          `TextIOWrapper.write` 累计 **1.29s**（单次可达 200ms+），而它原来跑在
          引擎渲染线程上（每秒一次）⇒ 每次写健康日志都可能直接吃掉好几帧。
          现在渲染线程只 `put_nowait`（微秒级），磁盘再慢也只卡后台线程。
        """
        if self.fh is None or self._closing:
            return
        try:
            self._q.put_nowait(line)
        except queue.Full:
            pass                     # 诊断日志：宁可丢一行，绝不阻塞演出

    def _writer(self):
        while True:
            try:
                line = self._q.get(timeout=0.5)
            except queue.Empty:
                if self._closing:
                    break
                continue
            if line is None:
                break
            try:
                self.fh.write(line + "\n")
                self.fh.flush()      # 后台线程 flush，崩溃也不丢证据；不再影响渲染
                self.lines.append(line)
                self.n += 1
                if self.n >= self.max_lines:
                    self._rewrite(self.keep)
            except Exception:                                          # noqa: BLE001
                pass

    def close(self):
        self._closing = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        try:
            if self._thr is not None:
                self._thr.join(timeout=1.0)
        except Exception:                                              # noqa: BLE001
            pass
        self._thr = None
        try:
            if self.fh is not None:
                self.fh.flush()
                self.fh.close()
        except Exception:                                              # noqa: BLE001
            pass
        self.fh = None


# 输出侧（NDI / Spout）失败取证日志：`%LOCALAPPDATA%\AutoVJ\output_health.log`。
#   2026-10-02（任务#14）：NDI/Spout 旧实现"一次失败就永久停"，导致**接收端永久冻在最后一帧**
#   而本机预览/输出窗口/日志全正常（用户实测四轮"另一台机器冻住"的唯一自洽解释）。改成退避
#   重试后，失败/恢复必须**留下可查证据**——别再只 `print`（打包后控制台无人可见）。
_OUTPUT_BACKOFF_CAP = 30.0      # 退避上限（秒）：1→2→4→8→16→30→30…
_OUTPUT_FAIL_LOG_MIN = 30.0     # 失败日志限流：退避中每这么久最多记一行（别刷屏）


def _output_health_path():
    """输出侧失败/恢复日志路径：`%LOCALAPPDATA%\\AutoVJ\\output_health.log`。"""
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "output_health.log")


def _output_health_write(line):
    """往输出侧取证日志追加一行（带时间戳）。任何异常一律吞掉：取证绝不能反过来影响演出。"""
    p = _output_health_path()
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, line))
    except Exception:                                                  # noqa: BLE001
        pass


def _output_backoff(fails):
    """连续失败 `fails` 次后的退避秒数：1→2→4→8→16→30（上限 `_OUTPUT_BACKOFF_CAP`）。"""
    try:
        n = int(fails)
    except Exception:                                                  # noqa: BLE001
        n = 1
    if n <= 0:
        return 0.0
    return min(_OUTPUT_BACKOFF_CAP, float(2 ** (n - 1)))


#: ---- 慢帧取证：GC 停顿累加（2026-11-10）---------------------------------
# 用 `gc.callbacks` 只累加两个整数 + 两个 float，回调里**不做任何格式化/分配**，
# 开销可忽略；慢帧那行日志读出"自上次慢帧以来"的 gen2/gen1 次数与总停顿。
#: 慢帧取证：routes 逐屏累计（按屏名）。name -> {"spout":ms,"ndi":ms,"bits":ms,"n":n}
_ROUTE_SEG = {}

#: 慢帧取证：颜色/后处理链细分（ms）。由 `_apply_color_postfx` 累加，慢帧日志读取后清零。
_POST_ACC = {"post": 0.0, "mode": "", "s": 0.0,
             "color": 0.0, "write": 0.0, "inplace": 0, "fallback": 0}

_GC_ACC = {"g2_n": 0, "g2_ms": 0.0, "g1_n": 0, "g1_ms": 0.0, "_t0": 0.0}


def _gc_probe_cb(phase, info):
    try:
        if phase == "start":
            _GC_ACC["_t0"] = time.perf_counter()
            return
        dt = (time.perf_counter() - _GC_ACC["_t0"]) * 1000.0
        gen = int(info.get("generation", 0))
        if gen >= 2:
            _GC_ACC["g2_n"] += 1
            _GC_ACC["g2_ms"] += dt
        elif gen == 1:
            _GC_ACC["g1_n"] += 1
            _GC_ACC["g1_ms"] += dt
    except Exception:                                          # noqa: BLE001
        pass


try:
    import gc as _gc_mod
    if _gc_probe_cb not in _gc_mod.callbacks:
        _gc_mod.callbacks.append(_gc_probe_cb)
except Exception:                                              # noqa: BLE001
    pass


def _gc_take():
    """取走并清零累加值（返回 (g2_n, g2_ms, g1_n, g1_ms)）。"""
    try:
        out = (_GC_ACC["g2_n"], _GC_ACC["g2_ms"], _GC_ACC["g1_n"], _GC_ACC["g1_ms"])
        _GC_ACC["g2_n"] = 0
        _GC_ACC["g2_ms"] = 0.0
        _GC_ACC["g1_n"] = 0
        _GC_ACC["g1_ms"] = 0.0
        return out
    except Exception:                                          # noqa: BLE001
        return (0, 0.0, 0, 0.0)


#: 预览缩略图的最长边（V4：以前固定 480×270 + Fast 变换 ⇒ 放大后全是马赛克）
# ★ 2026-11-10 性能：预览卡片只有几百像素宽，1280 会让 1280×720 的帧走
#   `frame.copy()` 全幅拷贝（7 屏 × 3.7MB × 15fps ≈ 388MB/s）。降到 512：
#   预览卡片只有几百像素宽，清晰度仍够，拷贝字节降 >70%。
# ★ 2026-11-11（用户：「操作非常卡」）：py-spy 实测 `_preview_thumb` 的
#   `QImage.scaled` 在 22s 里持有 GIL 2.8s（≈13%），主线程因此排队。
#   512→320 后输出像素只有原来的 39%；缩放改用 Fast（监看缩略图不需要
#   高质量重采样，Smooth 在这条路径上纯烧 GIL），开销再降一个数量级。
PREVIEW_THUMB_MAX = 960
# 单屏 GUI 预览硬上限：1080p 输出保持原生，3000/4K 整合画布不再整幅塞给 GUI
# （2026-11-13 用户：多屏预览卡死不动 —— 原生 3000×900 每帧 copy + GUI 缩放太重）
PREVIEW_GUI_MAX = 1920


def _preview_thumb(frame, max_side=None):
    """预览用缩略图：**只缩不裁**，最长边不超过 `max_side`（默认 PREVIEW_THUMB_MAX）。

    ★ 2026-11-11：`max_side` 由 `engine._preview_res` 决定（预览设置里的 1/2、1/4
      直接在引擎侧缩到最终尺寸），避免"引擎缩到 320 → GUI 再缩到 80"的双重缩放。
    """
    try:
        if frame is None or frame.isNull():
            return frame
        w, h = frame.width(), frame.height()
        if w <= 0 or h <= 0:
            return frame
        _max = max(32, int(max_side or PREVIEW_THUMB_MAX))
        m = max(w, h)
        if m <= _max:
            return frame.copy()          # 本来就不大于上限，无需缩放
        r = _max / float(m)
        return frame.scaled(max(1, int(round(w * r))), max(1, int(round(h * r))),
                            Qt.KeepAspectRatio, Qt.FastTransformation)
    except Exception:                                              # noqa: BLE001
        return frame


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
    # ★ 2026-11-11（用户：有段落时节奏标签还在常规/快切之间跳）：
    #   段落表接管时，HUD/状态栏显示这一项；它**不是** `self.mode` 的合法值，
    #   只用于 `modes_snapshot()` 的展示，避免把 MODE_META 的节奏档位改坏。
    "section": "按段落切换",
    # ★ 2026-10-25：**不是一种节奏**，而是「完全不自动切换」的策略值（`cfg["mode"]["manual"]`）。
    #   它**绝不能进 `MODES`**：`MODES` 同时被 `manual_mode()` / `modes_snapshot()` 当合法值校验，
    #   而且 `MODES` 里的值会落到 `self.mode`，随后被 `MODE_META[self.mode]` 取过渡时长 ——
    #   `MODE_META` 只有三个节奏键，放进去就是 KeyError。
    #   这里只是给状态栏/HUD 一个显示名（`MODE_LABELS.get(m, m)` 兜底也不会崩）。
    "manual": "手动切换",
}
# 「手动切换素材」策略值（存在 `cfg["mode"]["manual"]`，与自动/三档节奏互斥）
SWITCH_MANUAL = "manual"
# 图层混合模式（键为配置文件值；Qt 无原生"减少"，用逐像素实现）
BLEND_MODES = ("normal", "add", "subtract", "screen", "multiply", "lighten", "darken")

# ---- "减少"混合的缓存（见 _apply_subtract 的注释：性能关键）----
# LUT：把「透明度缩放」变成一次查表，避免每帧 float 相乘
_SUB_LUT = {}
# scratch：cv2 的 dst 必须是**连续**内存，而 canvas[:, :, :3] 是 stride=4 的不连续视图，
# 所以要么拷回（慢 10 倍），要么用整幅 4 通道自己当 dst。这里用后者，只需一份临时图。
_SUB_SCRATCH = {}


def _sub_lut(op):
    """透明度 → 256 项 uint8 查找表（缓存；256 字节，代价可忽略）

    ⚠ **必须用 `ceil` + float64，不能用截断**：
    旧实现算的是 `trunc(canvas − tmp*op)`，对整数 canvas 且 `tmp*op >= 0` 有
        `trunc(c − x) == c − ceil(x)`   （负数部分被 clip 到 0，与饱和减等价）
    ⇒ 只要 LUT 取 `ceil`，新实现就与旧实现**逐位一致**（`tools/_subtract_verify.py` 断言最大差 0）。
    用截断会得到每层 ±1、多层累计 ±3 的偏差（视觉不可见，但没必要引入）。
    ⚠ 用 float64 而非 float32：float32 在 `i*op` 接近整数时会因表示误差把 ceil 抬高一档。
    """
    key = round(float(op), 6)
    lut = _SUB_LUT.get(key)
    if lut is None:
        lut = np.clip(np.ceil(np.arange(256, dtype=np.float64) * key), 0, 255).astype(np.uint8)
        _SUB_LUT[key] = lut
    return lut


def _sub_scratch(h, w):
    """按尺寸复用的 (h, w, 4) uint8 暂存区（每帧新建 8 MB 数组是纯浪费）"""
    a = _SUB_SCRATCH.get((h, w))
    if a is None:
        a = np.empty((h, w, 4), np.uint8)
        _SUB_SCRATCH[(h, w)] = a
    return a
BLEND_LABELS = {
    "normal": "常规",
    "add": "添加",
    "subtract": "减少",
    "screen": "屏幕",
    "multiply": "正片叠底",
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


# 自动模式后处理强度 = 「全局强度（感知曲线）」×「能量唤醒系数」
#
# 2026-10-23 修（用户报「后处理好像没效果」）：
#   旧公式 = E × 0.6 × 全局强度。三个乘数**连乘**，任何一个小都会把结果压进死区：
#   全局强度滑到 8% 时，E=0.5 的结果只有 0.024 —— 也就是「2.4% 的特效」，
#   在 255 级画面上平均只改 3 个色阶，肉眼等于没有（实测见
#   tools/_verify_color_postfx.py --user）。
#   现改为两级：
#     · 全局强度走**感知曲线** `g ** 0.55`：滑块低端不再是死区（8% → 0.14、
#       60% → 0.45、100% → 0.60），上限仍是「满滑块 = 全局的六成」，沿用原设计；
#     · 能量只做**唤醒**（0.15 以下休眠、0.70 以上满额）：安静段照旧几乎没有效果，
#       但中段（0.4~0.6，绝大多数舞曲的常态）能到 45%~80% 而不是 10%。
#   ★ 判据：**乘法链条上每一环都不能单独把结果压到看不见**；低端要有可用的分辨率。
AUTO_FX_MAX = 0.6      # 满滑块 + 满唤醒时的强度上限（= 全局强度的六成，旧 AUTO_FX_RATIO）
AUTO_FX_GAMMA = 0.55   # 全局强度的感知曲线指数（<1 = 低端抬高，避免死区）
AUTO_FX_WAKE_LO = 0.15  # 能量低于此值：完全休眠
AUTO_FX_WAKE_HI = 0.70  # 能量高于此值：满额
AUTO_FX_RATIO = AUTO_FX_MAX   # 兼容旧名字（外部脚本/测试引用）


# 「无声音时隐藏本层」（hide_silent）的防抖秒数。
# 需求：静音要**连续**达到该时长才隐藏，恢复有声也要**连续**达到该时长才显示。
# 为什么要防抖：音频线程的 `silent_sec` 是**逐 block 累加/清零**的
# （audio_engine.py: `st.silent_sec = st.silent_sec + block_sec if rms < 1e-4 else 0.0`）
# —— 只要有**任何一个** block 的 rms >= 1e-4，silent_sec 立刻归零。音乐里的鼓点间隙、
# 搓碟、EQ 抽低频都会让它反复归零 ⇒ 图层反复隐藏/显示闪烁。加 0.5s 连续判定即可消除。
SILENT_HIDE_DEBOUNCE = 0.5
# 原始「静音」判据阈值（保持既有语义不变，见 `_silent_hide_tick`）。
_SILENT_SEC_RAW = 0.25
_SILENT_LEVEL_RAW = 0.03


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
        self.lid = "lay_" + uuid.uuid4().hex[:12]   # 多屏输出用的稳定 id（写进配置）
        self.visible = True
        self.opacity = 1.0
        self.blend = "normal"    # 混合模式
        self.speed = 1.0         # 图层播放速度倍率
        self.use_alpha = True    # 保留素材自带 alpha 通道（关闭=透明区域填黑当实底）
        self.lock_motion = False  # 锁定画面：不随能量脉冲缩放、不漂移（logo/字幕用）
        self._alpha_key = None
        self._alpha_img = None
        self._sub_tmp = None     # "减少"混合用的暂存画布（按当前分辨率复用，别每帧新建）
        self.img_scale = 1.0     # 画面缩放（图片/素材通用）
        self.img_x = 0.0         # 水平位移（画布宽度的百分比 -100~100）
        self.img_y = 0.0         # 垂直位移（画布高度的百分比）
        self.pause_silent = False  # 静音时暂停播放（保持当前画面）
        self.hide_silent = False  # 无声音时隐藏本图层内容
        # ★ 2026-11-26（用户）：手动图层专用 —— 勾选后点「下一素材」不切本层。
        #   自动层/Kv/码表层不使用这个字段（Kv 有自己的切换速度设置）。
        self.stop_switch = False
        self.solo = False        # 独奏：只显示本图层
        self.clips = []          # [MediaItem]（手动素材）
        # ★ 2026-11-13（用户：同一素材在不同图层要各自的大小/位置）：
        #   `clips` 里的同一条媒体对象可能同时出现在多个图层；transform 不能再挂在
        #   MediaItem 上（共享），改为**每个图层每个片段一份**：与 `clips` 等长。
        #   `{"scale":1.0,"x":0.0,"y":0.0,"rot":0.0}`；旧配置加载时从 MediaItem 迁移。
        self.clip_xforms = []
        self.is_kv = False       # Kv 主视觉图层（待机层）：无声音时切入、有声音时切出；全局唯一且默认置顶
        self.role = "bg"         # 图层角色：fg 前景 / bg 背景（自动匹配用）
        self.auto_mode = False   # 自动模式：按曲风自动挑素材
        self.auto_clips = []     # [MediaItem]（自动模式挑的素材，独立于手动 clips）
        self._pool_history = []  # 最近进过自动池的路径（跨段落避重，1.2 Beta1）
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
        # ---- 码表（cue）图层：容器层标记与运行时状态（S3 数据面，渲染在 S4/S5）----
        #   `is_cuesheet=True` 的图层是**容器**：自身没有单一 player，画面由内部子层按码表渲染。
        self.is_cuesheet = False
        self.cuesheet_id = ""      # 绑定的码表标识（Step5 由 UI 填；这里只存不解释）
        self.cs_states = []        # [LayerPlayState]，由 S4 的 _cuesheet_tick 每帧填
        self.cs_active = False     # 本帧是否接管（False ⇒ 退回普通图层行为）
        self._cue_players = {}     # 码表**专用**解码器缓存（与 self.cache 隔离，见 _cue_get_player）
        self._cue_items = {}       # 路径 → MediaItem（同一个素材被多个子层复用）
        self._cue_last_frame = {}  # lyr_id → 最后一帧（seek/冻结期间不黑）
        self._cue_sub_tmps = {}    # lyr_id → subtract 用的暂存画布
        self._cue_seek_state = {}  # lyr_id → seek 状态（in-flight / degraded / target）
        self.player = None
        self.prev_player = None
        self.trans_start = 0.0
        self.trans_active = False
        self._trans_prev_item = None   # 过渡中旧素材的 transform（换池后 lay.clips 已变）
        self.trans_ms = None     # 切换时的过渡时长（None=用当前模式的）
        self.last_switch_t = 0.0
        self.recent = []         # 防重复队列
        self.cache = {}          # path -> player（解码器复用缓存，LRU 上限 8）
        # 卡帧看门狗熔断状态（挂在图层上，避免每 tick 构造 dict 做 key；见 `_heal_stalled`）
        self._heal_path = None   # 当前冷却/计数对应的素材 path（换素材即重置）
        self._heal_last_t = 0.0  # 上次自动重建该素材的时刻
        self._heal_count = 0     # 该素材累计自动重建次数
        self._heal_gave_up = False  # 已到上限：换素材前不再自动重建
        self._heal_next_t = 0.0  # 下次允许"检查"（调 stalled()）的时刻（健康素材节流）
        # 静默冻帧兜底状态（见 `_heal_silent`）
        self._silent_none_since = 0.0      # player 缺失的起始时刻
        self._silent_inactive_since = 0.0  # 未激活的起始时刻
        self._silent_last_action_t = 0.0   # 上次动作（重建 / 激活）时刻
        self._silent_ended_last_t = 0.0    # 上次记录"ended 无帧"的时刻
        # 「从未出帧」兜底状态（见 `_heal_silent` 第 2.5 类）
        self._silent_noframe_since = 0.0        # 连续 `_frames==0` 的起始时刻
        self._silent_noframe_path = None        # 计数对应的素材 path（换素材即重置）
        self._silent_noframe_count = 0          # 该素材累计"无帧重建"次数
        self._silent_noframe_last_t = 0.0       # 上次"无帧重建"时刻（图层级冷却）
        self._silent_noframe_capped_log_t = 0.0  # 上次记"重建到顶"日志的时刻
        self._silent_skip_log_t = 0.0           # 上次记"无帧跳过/单素材不可跳"日志的时刻
        # 预热（切素材零延迟）：切换后延迟一会儿把「下一个要用的素材」的容器打开+首帧解出来。
        # 不预热的话切换瞬间新解码器还要 70~690ms 才有首帧（= 用户看到的"卡几帧"）。
        self.pre_idx = -1        # 预选的下一个素材索引
        self.warm_pend = False   # 是否已安排预热
        self.warm_at = 0.0       # 预热执行时刻（错开切换瞬间，避免和正在解码的素材抢 CPU）
        self._presec_path = None # 段落边界前专门预热的素材 path（见 engine._presec_tick）
        # 逐拍交替状态
        self.pair = None         # (idx_a, idx_b)
        self.pair_turn = 0
        self.beats_in_pair = 0
        self.beat_sec = None     # 当前对子所属的段落（换段 ⇒ 换一对）
        self.rand_pool = None
        self.order_dir = 1
        self.order_i = 0

    def close_all(self):
        """关闭本图层所有解码器（图层删除/重建时调用）。

        ★★ 2026-10-19 审计修复：**码表专用解码器必须一起关**。
          码表图层的播放器存在 `_cue_players`（不在 `self.cache` 里，见 `_cue_get_player`）。
          旧实现只关 cache ⇒ 图层被移除后 `_cuesheet_tick` 不再遍历它，
          这些播放器**保持 active 继续满速解码读盘**，直到进程退出才回收
          —— 正是 2026-09-30「12 核跑满 / 磁盘 750MB/s」那起事故的形态。
        """
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
        # ---- 码表侧：先失活（不 decode 了）再 close，最后清引用 ----
        for q in list((getattr(self, "_cue_players", None) or {}).values()):
            try:
                q.set_active(False)
            except Exception:                                     # noqa: BLE001
                pass
            try:
                q.close()
            except Exception:                                     # noqa: BLE001
                pass
        self._cue_players = {}
        self._cue_items = {}
        self._cue_last_frame = {}
        self._cue_sub_tmps = {}
        self.cs_states = []

    # ---------- 每个图层片段独立的 transform（大小/位置/旋转） ----------
    @staticmethod
    def _default_clip_xform(item):
        return {
            "scale": float(getattr(item, "img_scale", 1.0) or 1.0),
            "x": float(getattr(item, "img_x", 0.0) or 0.0),
            "y": float(getattr(item, "img_y", 0.0) or 0.0),
            "rot": float(getattr(item, "img_rot", 0.0) or 0.0),
        }

    def ensure_clip_xforms(self):
        """保证 `clip_xforms` 与 `clips` 等长；旧配置/自动换池时按素材默认值补齐。"""
        if not isinstance(getattr(self, "clip_xforms", None), list):
            self.clip_xforms = []
        n = len(self.clips)
        while len(self.clip_xforms) < n:
            self.clip_xforms.append(self._default_clip_xform(self.clips[len(self.clip_xforms)]))
        if len(self.clip_xforms) > n:
            del self.clip_xforms[n:]

    def clip_xform(self, idx):
        """取第 idx 个片段的 transform（返回内部 dict，只读使用）。"""
        self.ensure_clip_xforms()
        if 0 <= int(idx) < len(self.clip_xforms):
            return self.clip_xforms[int(idx)]
        return {"scale": 1.0, "x": 0.0, "y": 0.0, "rot": 0.0}

    def set_clip_xform(self, idx, xf):
        self.ensure_clip_xforms()
        if not (0 <= int(idx) < len(self.clip_xforms)):
            return
        d = self.clip_xforms[int(idx)]
        try:
            d["scale"] = float(xf.get("scale", d.get("scale", 1.0)) or 1.0)
            d["x"] = float(xf.get("x", d.get("x", 0.0)) or 0.0)
            d["y"] = float(xf.get("y", d.get("y", 0.0)) or 0.0)
            d["rot"] = float(xf.get("rot", d.get("rot", 0.0)) or 0.0)
        except (TypeError, ValueError):
            pass

    def append_clip(self, item, xf=None):
        """加入片段，并复制一份独立 transform（不共享 MediaItem 上的旧值）。"""
        self.ensure_clip_xforms()
        self.clips.append(item)
        self.clip_xforms.append(dict(xf) if isinstance(xf, dict)
                                else self._default_clip_xform(item))

    def remove_clip_at(self, idx):
        self.ensure_clip_xforms()
        if 0 <= int(idx) < len(self.clips):
            self.clips.pop(int(idx))
            self.clip_xforms.pop(int(idx))
            return True
        return False

    def to_dict(self):
        self.ensure_clip_xforms()
        return {"lid": self.lid,
                "name": self.name, "visible": self.visible, "opacity": self.opacity,
                "blend": self.blend, "solo": self.solo,
                "speed": self.speed,
                "use_alpha": self.use_alpha, "lock_motion": self.lock_motion,
                "img_scale": self.img_scale, "img_x": self.img_x, "img_y": self.img_y,
                "fixed": self.fixed, "hide_silent": self.hide_silent,
                "stop_switch": self.stop_switch,
                "role": self.role, "auto_mode": self.auto_mode, "is_kv": self.is_kv,
                # 码表绑定关系要持久化（否则重启后"这个图层挂哪张码表"就丢了）
                "is_cuesheet": bool(getattr(self, "is_cuesheet", False)),
                "cuesheet_id": str(getattr(self, "cuesheet_id", "") or ""),
                "auto_clips": [c.path for c in self.auto_clips],
                "clips": [c.path for c in self.clips],
                "clip_xforms": [dict(x) for x in self.clip_xforms]}


def _crop_src_rect(image, crop):
    """归一化 `crop=[x, y, w, h]`（0~1）→ 源图像素矩形 `QRectF`；非法/None ⇒ `None`。

    取整与钳位**逐字对齐 Cue 端 `render/compositor._crop_src_rect`**（它当初也是照抄演出端）：
      · `cw/ch` 四舍五入且至少 1 像素；`cx/cy` 钳到 `[0, iw-1]/[0, ih-1]`；
      · 越界时收缩到 `iw-cx` / `ih-cy`（不越出图像）；
      · 任何异常 ⇒ `None`（= 不裁），**绝不抛**（这是热路径）。
    ⚠ 定义在**模块级**而不是方法：`_draw_layer` 可能被无 `self` 的测试直接调用
      （`tools/cue_pixel_diff_guard.py` 就是这样做的）。
    """
    if image is None or image.isNull() or not crop:
        return None
    try:
        if len(crop) != 4:
            return None
        fw, fh = image.width(), image.height()
        cw = max(1, int(round(float(crop[2]) * fw)))
        ch = max(1, int(round(float(crop[3]) * fh)))
        cx = max(0, min(fw - 1, int(round(float(crop[0]) * fw))))
        cy = max(0, min(fh - 1, int(round(float(crop[1]) * fh))))
        cw = min(cw, fw - cx)
        ch = min(ch, fh - cy)
        if cw <= 0 or ch <= 0:
            return None
        return QRectF(cx, cy, cw, ch)
    except (TypeError, ValueError, IndexError):
        return None



class _CueItemStub:
    """`MediaItem` 构造失败时的兜底（只为让 `make_player` 能跑；不含任何播放逻辑）。

    正常情况不会用到：真实素材一律走 `MediaItem`。
    """
    def __init__(self, path):
        self.path = str(path or "")
        self.kind = "video"
        self.name = os.path.basename(self.path)
        self._has_alpha = False
        self._is_dxv = False
        self.img_scale = 1.0
        self.img_x = 0.0
        self.img_y = 0.0
        self.img_rot = 0.0
        self._thumb_path = lambda ext=".jpg": self.path + ext

    def all_tags(self):
        return ()


# ==========================================================================
# 码表位置「自由运行」窗口（秒）—— 见 `__init__` 里的说明
# ==========================================================================
CUE_FREEWHEEL_SEC = 2.0       # 常规外推窗口（现在只用于判断"是否需要报告"，不再卸画面）
# ★ 2026-10-18 真机实测：识别偶尔会卡 2~4 秒（低票段），一超 2 秒就卸画面 ⇒
#   用户看到「码表闪出普通图层零点几秒」。而换歌/静音时本来就该卸。
#   ⇒ 只要**还是同一首歌**且**没静音**，就一直滑行（上限 CUE_FREEWHEEL_MAX）；
#     真正释放只由 `sid<0` / 静音>3s / 超上限 触发。
CUE_FREEWHEEL_MAX = 30.0
# ★ 2026-11-13（用户：音乐停十几秒，码表还在演）：静音/无音频超过这个秒数
#   立即停码表（清位置、卸子层播放器）。识别短暂断档（有声）仍走 30s freewheel，
#   但“真的没声音了”不能继续演。
CUE_STOP_SILENT_SEC = 2.0

# 倍率死区（见 `_cue_auto_position` 里的说明）：±0.2% 内按原速处理。
# ★ 2026-11-11：旧值 0.10 把现场真实的 1.018× 按回 1.000，150 秒累积 2.7 秒偏差。
CUE_RATE_DEADBAND = 0.002

# 演出提前量（秒）：补偿指纹识别的固有滞后（用户 2026-10-18 指定 0.5s）
CUE_LEAD_SEC = 0.50



class _CueSubLayer:
    """码表内**单个子层**的轻量替身（**照搬备份 284-317 的完整 `__slots__`**）。

    为什么需要它：`_draw_layer()` 是按"一个 Layer"写的，会读下面这一整串属性。
    码表要在一个容器图层里画多个子层，就必须给每个子层一份**独立**状态，
    不能复用真 Layer（会污染它的播放状态）。

    ⚠ 为什么必须**完整照搬**、不能写"看起来够用"的缩写版：
      `_draw_layer` 里多处是 `getattr(lay, "xxx", 默认)`，缩写版**恰好**因为默认值不崩,
      但那是巧合 —— 一旦某处默认值或语义变了，就会**静默走错分支**（画面错但不报错）。
      多余字段无害，缺字段才是坑。
    ⚠ `speed` 不在这里被 `_draw_layer` 读（计速在播放器的 `set_speed` 上），
      但保留它以与真 Layer 同构，避免以后有人"按 Layer 的属性表"写代码时踩空。
    """
    __slots__ = ("name", "opacity", "is_kv", "lock_motion", "use_alpha", "visible",
                 "prev_player", "cur", "prev_idx", "trans_active", "trans_ms",
                 "last_switch_t", "recent", "cache", "_alpha_key", "_alpha_img", "_sub_tmp",
                 "clips", "lyr_id")

    def __init__(self, lyr_id="", opacity=1.0, item=None, name=""):
        self.lyr_id = lyr_id
        self.name = name
        self.opacity = max(0.0, min(1.0, float(opacity)))
        self.is_kv = False
        self.lock_motion = False
        self.use_alpha = True
        self.visible = True
        self.prev_player = None            # 过渡由 alpha 控制，不用播放器交叉淡化
        self.cur = 0
        self.prev_idx = -1
        self.trans_active = False
        self.trans_ms = None
        self.last_switch_t = 0.0
        self.recent = []
        self.cache = {}
        self._alpha_key = None
        self._alpha_img = None
        self._sub_tmp = None
        # ★ 必须是**真实 MediaItem**：`make_player()` 读 `item.kind` / `item._thumb_path()`，
        #   自造替身会让工厂抛异常（被吞掉 ⇒ 表现为"码表图层一片黑"，很难查）。
        self.clips = [item] if item is not None else []


class AutoVJEngine(QObject):
    # ⚠⚠ **只投递一个 int 序号，帧本身放在 `self._latest_frame`**（2026-09-26 内存雪崩修复）。
    # 旧写法是 `Signal(QImage)`，每帧把一个 8MB 的 1080p 副本投进主线程队列
    # （引擎在独立线程 ⇒ 自动 QueuedConnection）。主线程一旦卡一下（例如切素材那 3 秒），
    # 队列就开始积压：30fps × 8MB = 240MB/秒。实测 **30 秒涨 3.2GB**，
    # 一路涨到 15GB 把整机拖垮（内存耗尽 → 建线程失败 → 界面"无响应"）。
    # 现在队列里堆的只是一个 int，堆多少都无所谓；绘制端自己去取最新帧。
    frame_ready = Signal(int)
    auto_layers_changed = Signal(object)   # [layer_index, ...] 自动图层素材池被重算
    native_display_changed = Signal(bool)  # GPU 合成器接管原生输出窗

    def __init__(self, config, audio_state, media_library, parent=None):
        super().__init__(parent)
        self.cfg = config
        # ★★★ 2026-10-17 复盘（本项目代价最高的一次"接线错位"）：
        #   `self.audio` **不是 AudioEngine，而是 AudioState 本身**（`ui_main.py:2267`
        #   传的是 `self.audio.state`）。历史上本文件多处按 "self.audio 是引擎" 写成了
        #   `getattr(self.audio, "state", None)` ⇒ 恒为 None ⇒ 码表自动跟拍**永远读到 -1**，
        #   而所有单测都自己塞了带 `.state` 的假对象 ⇒ 测试全绿、现场全黑。
        #   铁律：**凡"某字段恒为 -1/0/None"先验证取数对象本身存不存在**（`getattr` 默认值
        #   会把"取错了对象"和"还没数据"伪装成同一件事）。
        self.audio = audio_state
        # 真正的音频引擎（读 `_recognizer_engine` 等内部态用）。由 MainWindow 注入，
        # 未注入时为 None ⇒ 调用方必须能退化成"只读 `self.audio` 的公开状态"。
        self._audio_engine = None
        self.library = media_library
        # ★ 2026-11-09：`safe_decode`（普通视频改走 PyAV）已被用户要求去掉，
        #   普通视频固定走 OpenCV；带 alpha 的素材仍然走 PyAV（见 media_manager.make_player）。
        self.layers = []
        # ---- 码表（cue）引擎级状态（S3 数据面；S4 起由 _cuesheet_tick 消费）----
        self._cue_sheet = None      # 当前码表（cuesheet.CueSheet）
        self._cue_sheet_path = ""   # ★ 2026-11-02：当前码表的**文件路径**（界面按 basename 显示）
        self._cue_track = None      # 认到的曲目（TrackEntry）；None ⇒ 不接管
        self._cue_bound_cache = None    # (track, 主 song_id, frozenset(可接受 id)) 反查缓存
        self._cue_bad_song_n = 0        # 「认到另一首歌」累计次数（限流日志用）
        self._cue_bad_song_t = 0.0
        #   ★ 它同时是 `_cue_beat_len_for()` 的**第一优先**拍长来源（`identity.bpm`）：
        #     `match_track()` 五级链（含 md5/指纹）认出来的，不靠本机文件名猜。
        self._cue_rt = None         # cuesheet_runtime.CueRuntime
        self._cue_media = {}        # clip_id → 磁盘路径
        # ★★★ 2026-10-17：码表素材的 **alpha 事实表**（磁盘路径 → 是否带 alpha）。
        #   根因：码表包解压出来的素材**没有本地缩略图缓存**，而 `create_video_player()`
        #   的免费判据就是"缩略图是 PNG 才算带 alpha"。没有缩略图 ⇒ 判 False ⇒ 用 OpenCV
        #   解码 ⇒ **透明通道被丢掉，透明区变成不透明黑**。现场表现两个：
        #     ① 上层（LOGO/PNG）把下层的 BGA 整片盖成黑 ⇒ 用户说"多层编排没实现"；
        #     ② 淡出结束那一瞬整块黑 ⇒ 用户说"先黑屏才切出到其他层"。
        #   而 `.ercue` 里 `media[].has_alpha` 是**编排端探测好的权威结论**，直接用，零成本。
        self._cue_alpha = {}
        self._cue_pos_beats = None  # 本帧拍位（S3 只初始化，S4 才写）
        # ★★★ 2026-11-06：**无锁快照**（铁律 13：GUI 高频轮询只读无锁快照，绝不去抢 `_tick_lock`）
        #   · `_cue_media_snap`：码表包里的素材路径元组（装表/卸表时整体赋值）
        #   · `_cue_thumb_snap`：包内自带缩略图 `{素材路径: png}`（同上，整体替换）
        #   · `_cue_active_snap`：此刻正在使用的素材路径集合（每帧 tick 整体赋值）
        #   GUI 的图层行（缩略图 + 黄底高亮）只读这两个，不碰 `_cue_sheet` / `cs_states`。
        self._cue_media_snap = ()
        self._cue_thumb_snap = {}
        self._cue_active_snap = frozenset()
        self._cue_pos_src = ""      # 位置来源（fingerprint / deck）
        self._cue_sid = -1          # 当前曲目 sid（换歌要重建 runtime）
        self._cue_jumped = False    # 本帧是否发生跳变重算
        self._cue_closed_last = 0   # 上次 clear 关掉的解码器数（自检/排障用）
        self._cue_index = None      # 码表索引（曲目→码表；**只存摘要**，见 src/cue_index.py）
        # ---- 自动跟拍（Step3）：指纹位置 → 拍位 ----
        self._cue_pos_tracker = CuePositionTracker()
        # ★★★ 2026-10-18（用户："码表图层偶尔突然消失零点几秒"）：
        #   **自由运行（freewheel）** —— 广播 genlock 的标准做法：
        #   位置源短时丢票时，**保持最后位置并继续按速率外推**，画面接着走；
        #   超过 CUE_FREEWHEEL_SEC 才真正释放（卸画面）。
        #   旧行为：`status="lost"` ⇒ `pb=None` ⇒ `_cuesheet_tick` 清空 `cs_states`
        #   ⇒ 整块画面**直接消失**，恢复后再淡入 ⇒ 用户看到"闪一下/黑零点几秒"。
        self._cue_fw_pos = None      # 最后一次可信拍位
        self._cue_fw_t = 0.0         # 该拍位的时刻
        self._cue_fw_rate = 1.0      # 当时的播放速率（外推用）
        self._cue_pos_next_t = 0.0     # 下次轮询时刻（0.2s 一次，**不进 compose**）
        self._cue_pos_song = -1        # 当前自动跟拍的歌（song_id）
        self._cue_fw_on = False        # 本帧是否在自由运行（取证用）
        self._cue_tick_why = ""        # 本帧没接管的原因（取证用）
        # ★ 帧级暗场计时（用户："码表偶尔消失零点几秒"）—— 2s 一行的日志量不出来，靠这个
        self._cue_dark_t0 = None       # 本轮"本该出画却没出"的起始时刻
        self._cue_dark_max = 0.0       # 历史最长连续暗场（秒）
        self._cue_dark_n = 0           # ≥50ms 的暗场次数
        self._cue_auto_sheet = ""      # 自动装上的表（路径）；"" = 没装
        self._cue_manual_clock = False # 手动起拍生效中 ⇒ 自动位置让位（兜底优先）
        _cue_cfg = (self.cfg.get("cue") if isinstance(self.cfg, dict) else None) or {}
        # ★ 所有「该不该 seek / 阈值 / 落地 / 计速」的判断都在这个对象里（见 cue_seek_policy）
        self._cue_policy = CueSeekPolicy(cfg=_cue_cfg)
        self._cue_tick_fail = 0     # _cuesheet_tick 连续失败次数（异常安全用）
        self._cue_err_at = 0.0      # 上次写码表错误日志的时刻（限流用）
        self.running = False
        self.blackout = False
        self.freeze = False
        self.pause_auto = False
        self.locked = False
        self.rng = random.Random()
        self._last_bar_count = 0
        self._last_beat_count = 0
        # 自动匹配（听歌识曲→自动配画面）
        # ★ 2026-10-30 用户「大砍刀」：删掉 `_song_genre_lookup` / `_last_genres` /
        #   `_auto_refresh_t` —— 曲风识别与曲风映射整条链路已移除，自动图层只按
        #   「角色 + 动态档」挑素材（见 `_refresh_auto_layers` / `match_clips`）。
        # 后处理特效：事件触发时间（特效名 -> 上次 Drop 触发时刻）
        self._postfx_trig = {}
        self._frame_i = 0
        # 自动后处理「效果保持」：选中一组效果后保持 N 拍，到点/曲风变化才重挑
        self._fx_hold_pick = None
        self._fx_hold_due = 0.0
        # Spout 输出（懒加载，运行在引擎线程）
        self._latest_frame = None      # 最新合成帧（frame_ready 只投序号，帧放这）
        self._frame_seq = 0            # 帧序号（绘制端用来去重）
        # ---- 多屏输出（2026-11-08）----
        #   `_out_cfg` 是活动配置的深拷贝；None = 还没启用多屏，走旧单路逻辑。
        #   `_screen_canvases` 只保存**独立屏幕**的画布；主屏用 self.canvas。
        #   `_route_thumbs` 是给上方预览工作区的降采样帧（全屏 1080p 不进 GUI）。
        self._out_cfg = None
        self._out_mode = "multi"
        self._screen_canvases = {}
        self._combined_canvas = None
        # ★ U7：按屏 override 的 color/postfx。跑某屏的调色时临时把参数换成它那份，
        #   跑完立刻复位（None = 用全局 self.cfg）。
        self._cp_cfg = None
        self._screen_cp_calls = 0      # 统计"这帧额外跑了几次按屏调色"（性能判据用）
        self._route_hb_t = 0.0         # 路由心跳上次落盘时刻（BUG-A 取证用，10s 一行）
        self._route_thumbs = {}
        # ★ 2026-11-11（用户：多屏预览看起来不到 30 帧）：
        #   QImage.cacheKey() 在图像释放后可能被**复用**，GUI 用 cacheKey 判重会
        #   把新帧误判成旧帧（实测输出页只刷 ~10fps）。这里用单调递增的 rev
        #   明确告诉 GUI「这批缩略图是新的」。
        self._route_thumb_rev = 0
        # ★ 2026-11-11（性能）：输出页多屏缩略图只在**输出页可见**时才需要生成。
        #   以前每 2 个 tick 无条件为 7~8 块屏做 512px 平滑缩放，用户在多屏页之外
        #   （素材库/码表/普通演出台）时纯属白烧 CPU。UI 通过
        #   `set_route_thumb_consumers()` 开关；默认 True 保持工具/旧测试行为。
        self._route_thumb_consumers = False
        # ★ 2026-11-25：主窗口被输出窗完全盖住时由 GUI 置 True ⇒ 跳过缩略图。
        self._gui_covered = False
        self._route_frames = {}        # 发送端用（全分辨率，引用/裁切缓存）
        self._preview_route_id = ""
        self._route_senders = {}       # sid -> {"spout":..., "ndi":..., ...}
        self._route_state = {}         # sid -> 失败退避/日志状态
        self._plan_rev = 0             # 配置变更代数（发送端重建用）
        self._plan_rev_ready = -1      # 已重建到的代数
        self._thumb_tick = 0
        # ★ 2026-11-11（用户：多屏预览看起来不到 30 帧）：缩略图必须跟引擎同帧率。
        #   以前为省 GIL 改成每 3 帧一次 ⇒ 输出页监看只有 10fps，看起来一直掉帧。
        #   现在缩略图已改成 320px + Fast 缩放，每帧一次也能承受；GUI 端还会按
        #   `ui.preview.fps` 再节流（30/15）。
        self._thumb_every = 1
        # 预览分辨率（1.0 / 0.5 / 0.25）：引擎直接缩到最终尺寸，GUI 不再二次缩放
        self._preview_res = 1.0
        # GUI 发布帧率（预览面板 30/15fps；输出窗口/Spout/NDI 不限）
        self._gui_fps = 30.0
        # ★ 2026-11-12：单屏预览分辨率。以前固定 1600，且 `preview.res` 只影响
        #   多屏缩略图、不影响单屏 ⇒ 用户觉得“预览分辨率设置失效”。
        #   现在：无输出窗口时 maxw = **输出画布分辨率** × preview.res；
        #   输出窗口可见时按输出画布原生分辨率（不拿预览设置去降上屏画质）。
        self._gui_max_w = 1920
        self._out_window_live = False
        # ★ 2026-11-16（用户：多屏多路要给每块屏设不同输出显示器）：
        #   每个物理输出窗口拿自己的路由帧。GUI 把"有窗口的屏幕 id"登记到这里，
        #   引擎按 30fps 复制一份最新帧（不能直接给画布引用 —— 下一 tick 会被清空/重画）。
        self._display_route_ids = set()
        self._display_frames = {}
        self._display_t = 0.0
        # ★ 2026-11-25（两块屏都要 30fps）：显示窗口帧不再每 tick `QImage.copy()`
        #   （1080p×2 实测 7~9ms，是 tick 40ms 里最大的一块）。
        #   改成 3 张预分配缓冲轮换 + numpy memcpy（~0.45ms/张，释放 GIL）；
        #   3 张保证 GUI 还拿着上一两张时不会被下一帧覆盖。
        self._display_bufs = {}
        self._display_buf_turn = 0
        # ★ 2026-11-25：GPU 输出视图接管最终调色时，这里存每屏的 256x1 LUT。
        self._gpu_color_offload = False
        self._gpu_scene_mode = False
        # ★ 2026-11-25：只有 UI 确认“所有可见输出窗都是 GLTextureView”时
        #   才允许场景合成；单可见输出仍走 CPU，避免 10fps 和错屏。
        self._gpu_scene_allowed = False
        self._display_scenes = {}
        self._scene_cpu_i = 0
        self._gpu_luts = {}
        self._gpu_lut_img = None
        self._gpu_color_unsupported = False
        self._combined_key = None
        self._spout = None
        self._spout_fail = False       # True=当前处于"失败退避"态（ui_main 置 False ⇒ 立即重试）
        # Spout 退避重试状态（见 `_spout_send` / `_output_fail`）：1→2→4→8→…→30s，成功清零
        self._spout_fails = 0          # 连续失败次数（决定退避长度）
        self._spout_retry_at = 0.0     # 下次允许尝试的时刻（perf_counter）
        self._spout_fail_since = 0.0   # 首次失败时刻（恢复时算中断时长）
        self._spout_log_at = 0.0       # 上次写"失败"日志的时刻（30s 限流）
        # NDI 输出（懒加载，音画同步；缺 Runtime 时静默禁用）
        self._ndi = None
        self._ndi_fail = False         # True=当前处于"失败退避"态（ui_main 置 False ⇒ 立即重试）
        # NDI 退避重试状态（见 `_ndi_send` / `_output_fail`）
        self._ndi_fails = 0
        self._ndi_retry_at = 0.0
        self._ndi_fail_since = 0.0
        self._ndi_log_at = 0.0
        # 行为模式
        self.mode = "normal"
        self.mode_scores = {}
        self._mode_eval_t = 0.0
        self.switch_pos = 0.0
        self._next_due = None   # 已安排的下一次切换目标拍位（缓存：模式切换不重置，倒计时连续）
        # ★ BUG-D 波2：**按屏**排期状态（只有带 override 的屏用；见 `_auto_switch_tick_overrides`）
        self._scr_switch = {}   # {sid: {"pos": float, "due": float|None}}
        # ★ 2026-11-06：这一次的目标是"段落边界"还是"按拍"——HUD 文案据此换词
        #   （有段落表时说「距下一个段落」比「还有 N 拍切换」准确）。
        self._due_kind = None
        self._due_cap = False   # 本次目标是不是"段落太长被 64 拍封顶"的强制切
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
        # ★★ 2026-10-31：song_id -> (sections, dur, grid) —— **导入包**里的能量段落。
        #   本机扫的歌从 `cfg["music_meta"][path]` 取；导入歌没有 path（文件不在本机）
        #   ⇒ 只能按 song_id 查这张表（ui_main 通过 `set_song_sections()` 注入）。
        self._song_sec_lookup = {}
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
        # ---------------- 能量段落（曲库预设，用户 2026-10-28 要求）----------------
        # 见 src/sections.py。三条同时满足才接管：认到歌 / 位置可信 / 该歌有段落表。
        # 任一不满足 ⇒ 下面全部回到空/None，行为与改动前逐字一致。
        self._sec_list = []              # 当前生效的段落表（已 normalize）
        self._sec_idx = -1               # 当前段索引
        self._sec_applied_idx = -1       # 上次**执行过切换**的段索引
        self._sec_energy = None          # 段落目标能量（None = 不接管，用实测）
        self._sec_tier = None            # 段落目标档位
        self._sec_path = ""              # 段落表属于哪首歌（换歌要复位）
        self._sec_seen_path = None       # 上一次见过 path（判断"是否换歌"）
        self._sec_cache = None           # (key, raw_sections, 已 normalize) 缓存
        self._sec_info = {}              # 取证：状态/段索引/目标值
        # 掉锁后「冻结段位」的计时（见 `_section_tick`）：掉锁 ≠ 音乐变了，
        # 立刻退回实测能量会让能量档跳变 ⇒ 素材池重 roll + 强切（用户 2026-10-30 实测）。
        self._sec_lost_t = 0.0
        self._sec_switch_n = 0           # 段落触发的切换次数（取证）
        self._sec_reset_n = 0            # 段落状态被重置的次数（换歌/无表，取证用）
        self._sec_why = ""               # 最近一次切换/重置的原因（取证用）
        # ★ 2026-11-08（素材掉帧）：下一段**提前预热**状态。
        #   段落边界换池时若选中一个全新解码器，首帧要现开容器（大 DXV/机械盘
        #   实测可到 1~2 秒）。这里在边界前 `_PRESEC_LEAD_SEC` 秒，按下一段的
        #   档位预先 `match_clips` + 随机选一个 + `warm_up()`；边界切换时优先
        #   用这个已预热的素材（仍然随机选，不偏向近期用过的素材）。
        self._presec_key = None          # (sid, 下一段索引)；同一段只预热一次
        self._presec_lead = 4.0          # 提前多少秒开始预热
        # Spout/NDI Alpha：开启时画布用 ARGB32（直通 alpha，非预乘），
        # 这样 Spout/NDI 可以原样发 BGRA+A；显示器窗口仍按黑底画。
        self._alpha_out = True      # Alpha 输出固定开启（Spout/NDI 预乘）
        # ★ 2026-11-17（用户：多屏多路里**按屏**开了 alpha 却不透明）：
        #   `_alpha_out` 只是"并集/主画布"的格式开关；每块屏自己的 alpha 存在这里，
        #   由 `_rebuild_output_plan()` 按 `screen["alpha"]` 重算 ⇒ 独立屏/切分屏的
        #   画布格式跟随本屏，不再被别的屏强行拖成 ARGB32 或退回 RGB32。
        self._screen_alpha = {}
        # 任一路由需要 alpha ⇒ 禁用 GPU 合成（GPU 回读会换掉画布格式，见 `_compose_to`）。
        # 与 `_alpha_out`（主画布格式）分开：主屏可以不透明、旁屏透明。
        self._any_alpha = bool(self._alpha_out)
        # ★ 2026-11-26（用户：1080p 输出 17~21 帧、CPU 47%）：**内部分辨率**
        #   与输出分辨率解耦。1080p 输出默认按 2/3（1280×720）合成，再由输出窗
        #   GPU 放大；实测 7 层 + 2 路 Spout 从 14~25 帧回到 28~31 帧。
        #   `perf.render_scale` = 0（自动）/0.5~1.0；环境变量 AUTOVJ_RENDER_SCALE
        #   可临时覆盖（1.0 = 原来的 1080p 全分辨率合成）。
        self._render_scale = self._resolve_render_scale()
        try:
            _ow = int(self.cfg["output"].get("width", 1280) or 1280) * self._render_scale
            _oh = int(self.cfg["output"].get("height", 720) or 720) * self._render_scale
            self.canvas = QImage(max(2, int(round(_ow))), max(2, int(round(_oh))),
                                 self._canvas_format())
        except Exception:                                      # noqa: BLE001
            self.canvas = QImage(1280, 720, self._canvas_format())
        self._clear_canvas(self.canvas)
        # ---- optional GPU composition (QPainter on QOpenGLPaintDevice) ----
        # Deliberately optional: a failed context / unsupported blend set must
        # fall back to the existing CPU path without affecting playback.
        self._gpu_comp = None
        self._gpu_mode = "cpu"
        self._gpu_dead = False
        # ★ 2026-11-25：新的 GPU 纹理合成器（绑定「GPU 解码」开关；失败回退 CPU）
        self._gpu_renderer = None
        self._gpu_render_dead = False
        self._gpu_render_last_ms = 0.0
        self._gpu_render_logged = False
        self._gpu_premul = {}          # QImage.cacheKey -> premultiplied image
        self._gpu_premul_order = []
        self._gpu_premul_max = 24
        # ★ 2026-11-26：完整 GPU 场景合成（引擎线程离屏 FBO，合成后读回）。
        #   主屏 + 独立屏都走它；失败/不支持时整帧回退 CPU。
        self._gpu_stage = None
        self._gpu_native = False
        self._stage_spout_sids = set()
        self._gpu_stage_dead = False
        self._gpu_stage_fail = 0
        self._gpu_stage_frames = 0
        self._gpu_stage_ms = 0.0
        self._gpu_stage_log_t = 0.0
        # ★ 2026-11-26：独立 GPU 渲染进程。引擎只写共享内存 + 投场景描述，
        #   子进程拥有 GL/原生窗口/Spout。默认由 perf.gpu_render_process 或
        #   AUTOVJ_GPU_RENDER=1 开启。
        self._render_client = None          # 主屏客户端（预览来源）
        self._render_clients = {}           # sid -> GpuRenderClient（一屏一进程）
        self._render_clients_cfg = {}       # sid -> (上屏, 显示器号, 输出宽, 输出高)
        self._render_active = False
        self._render_fail = 0
        self._render_log_t = 0.0
        self._render_last_preview_t = 0.0
        self._render_route_frames = {}
        self._gpu_frame_consumers = True   # skip GUI copy when no preview/output
        # ★ 静音待机（见 `_tick_locked`）：运行但长时间无音频时暂停解码/降帧
        self._silent_standby = False
        self._silent_standby_prev = False
        # Kv 过渡期间禁止降帧（见 `_kv_transition_pending`）
        self._kv_anim = False
        self._silent_low_since = 0.0
        self._gpu_last_reason = ""
        self._gpu_active = False
        # adaptive render cadence: never compose faster than the source material
        self._effective_fps = None
        self._fps_next_t = 0.0
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
        # 「无声音时隐藏本层」0.5s 防抖状态（见 `_silent_hide_tick`）。
        # 每帧在 `_tick_locked` 里算**一次**，`hide_silent`（合成）与 `pause_silent`（活跃标记）
        # 两处都读同一个 `self._silent_now_debounced`，保证同帧一致。
        self._silent_debounce_on = False   # 当前对外生效的「静音」状态（防抖后）
        self._silent_since = 0.0           # 连续静音累计（秒）
        self._loud_since = 0.0             # 连续有声累计（秒）
        self._silent_last_t = 0.0          # 上次推进防抖计时的时刻（perf_counter）
        self._silent_now_debounced = False  # 本帧对外暴露的防抖结果（两处判据唯一来源）
        # 卡帧看门狗全局限流：上次自动重建的时刻（见 `_heal_stalled`）
        self._heal_last_global_t = 0.0
        # 静默冻帧兜底日志去重：key=(图层名, 原因) → 上次记录时刻（见 `_heal_silent`）
        self._silent_log_t = {}
        # 多层同时停帧取证：上次记录时刻（见 `_stutter_watch`）
        self._stutter_log_t = 0.0
        # 每层健康快照日志（懒创建；见 `_health_tick` / `_LayerHealthLog`）
        self._health_log = None
        self._health_next_t = 0.0
        # 引擎 tick 间隔 / 单次处理耗时（供 GUI 输出卡顿日志关联；见 `_tick`）
        self._last_tick_t = 0.0
        self._tick_gap_ms = 0.0
        self._tick_ms = 0.0
        self._tick_log_t = 0.0
        self._phase = "init"
        # GPU 可用性状态（-1=未知 / 0=否 / 1=是）；状态变化时在 `_health_tick` 写醒目日志
        self._gpu_state = -1
        # ★ GUI 无锁状态快照（见 `ui_snapshot` / `_tick_locked` 尾部）：
        #   引擎每 tick 把 switch_cd / phrase_cd / grid_view / next_clips 算好放这，
        #   GUI 不再直接调那 4 个 @_locked 方法去抢渲染锁（FINAL_REPORT P1-1）。
        self._ui_snap = {}
        self._ui_next_t = 0.0        # next_clips 上次重算时刻（按 _UI_NEXT_CLIP_INTERVAL 节流）
        self._ui_next_clips = {}     # 缓存的上一次 next_clips（节流期间沿用）

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)

    def _rs_size(self, w, h):
        try:
            s = float(getattr(self, "_render_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            s = 1.0
        return max(2, int(round(float(w) * s))), max(2, int(round(float(h) * s)))

    def _resolve_render_scale(self):
        """内部分辨率倍率：0/缺省 = 自动（1080p 输出用 2/3，其它 1.0）。"""
        v = 0.0
        try:
            v = float(os.environ.get("AUTOVJ_RENDER_SCALE", "0") or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v <= 0.0:
            try:
                v = float((self.cfg["perf"] or {}).get("render_scale", 0) or 0)
            except (TypeError, ValueError, KeyError):
                v = 0.0
        if v <= 0.0:
            try:
                _h = int(self.cfg["output"].get("height", 1080) or 1080)
            except (TypeError, ValueError, KeyError):
                _h = 1080
            v = (2.0 / 3.0) if _h >= 1080 else 1.0
        return max(0.25, min(1.0, float(v)))

    def set_render_scale(self, scale):
        """设置内部分辨率倍率（GUI 线程调用；0 = 自动）。画布在下一次 tick 重建。"""
        try:
            v = float(scale or 0.0)
        except (TypeError, ValueError):
            return
        if v <= 0.0:
            v = self._resolve_render_scale()
        v = max(0.25, min(1.0, v))
        try:
            with self._tick_lock:
                if abs(v - float(getattr(self, "_render_scale", 1.0))) < 1e-4:
                    return
                self._render_scale = v
                self._latest_frame = None
                if self._out_cfg:
                    # 下一次 tick 走 `_rebuild_output_plan()`，按新倍率重建所有画布
                    self._plan_rev_ready = -1
                else:
                    _w = int(round(int(self.cfg["output"].get("width", 1280) or 1280) * v))
                    _h = int(round(int(self.cfg["output"].get("height", 720) or 720) * v))
                    self.canvas = QImage(max(2, _w), max(2, _h),
                                         self._canvas_format())
                    self._clear_canvas(self.canvas)
        except Exception:                                      # noqa: BLE001
            pass

    def _canvas_format(self, on=None):
        """输出画布格式：Alpha 输出用 **ARGB32_Premultiplied**，否则 RGB32。

        ★★★ 2026-11-25（用户：单屏开 NDI+Spout 掉到 20 帧、多屏也 20 帧）：
          以前 Alpha 画布用 `Format_ARGB32`（**直通 alpha**）。Qt 对直通 alpha
          目标的 SourceOver 合成**没有快速路径**（每个像素都要做除法还原）：
          实测 1920×1080、6 个满屏图层、1.04× 能量脉动：
            · Format_ARGB32              15.9 ms/帧  ← 旧
            · Format_ARGB32_Premultiplied 5.7 ms/帧  ← 现
            · Format_RGB32                5.8 ms/帧
          即「开 Alpha」本身要白烧约 **10ms/帧**，再叠加 NDI/Spout 各自一次
          预乘转换 + 拷贝，单屏就从 30fps 掉到 20fps；多屏每块屏都这样，更慢。
          预乘格式同时正好是 NDI / Spout 的 alpha 约定（省掉发送前的
          `convertToFormat(ARGB32_Premultiplied)`），预览/输出窗口按黑底叠加
          显示时观感与直通 alpha 完全一致。

        `on=None` ⇒ 用全局并集 `_alpha_out`（主画布/整合画布）；
        传 True/False ⇒ 按**该屏自己的** alpha 决定（独立屏/切分屏画布）。
        """
        on = self._alpha_out if on is None else bool(on)
        return (QImage.Format_ARGB32_Premultiplied if on
                else QImage.Format_RGB32)

    def _clear_canvas(self, img):
        """按**该画布自己的格式**清画布：透明底 or 黑底。

        ★ 2026-11-11（用户：「操作非常卡」）：`QImage.fill()` 在 PySide 里
        **不释放 GIL**（py-spy：20s 里占 1.6s）。改用 numpy 写同一块像素，
        memcpy 期间释放 GIL，主线程能插进来画界面。RGB32 必须写
        `0xFF000000`（不透明黑），不能写 0 —— 否则 NDI/Spout 收到的 alpha 是 0。
        ★ 2026-11-17：判据从"全局 `_alpha_out`"改为"**这块画布有没有 alpha 通道**"，
           否则某屏 alpha=off（RGB32）而全局并集为真时会被清成 0（本该是不透明黑）。
        """
        _has_a = bool(img.hasAlphaChannel())
        try:
            n = int(img.sizeInBytes()) // 4
            if n > 0:
                arr = np.frombuffer(img.bits(), dtype=np.uint32, count=n)
                arr.fill(0x00000000 if _has_a else 0xFF000000)
                return
        except Exception:                                      # noqa: BLE001
            pass
        img.fill(Qt.transparent if _has_a else Qt.black)

    @staticmethod
    def _blit_1to1(dst, x, y, src):
        """把同格式、同尺寸的 `src` 原样贴到 `dst` 的 (x,y)。

        返回 True=成功；False=调用方退回 QPainter。numpy 的 copy 会释放 GIL，
        比 `QPainter.drawImage()` 更适合大块 1:1 拷贝（单路整合画布的主路径）。
        """
        try:
            x, y = int(x), int(y)
            w, h = int(src.width()), int(src.height())
            if (x < 0 or y < 0 or w <= 0 or h <= 0
                    or x + w > dst.width() or y + h > dst.height()
                    or src.format() != dst.format()):
                return False
            dbpl, sbpl = int(dst.bytesPerLine()), int(src.bytesPerLine())
            d = np.frombuffer(dst.bits(), dtype=np.uint8).reshape(dst.height(), dbpl)
            s = np.frombuffer(src.constBits(), dtype=np.uint8).reshape(h, sbpl)
            d[y:y + h, x * 4:(x + w) * 4] = s[:, :w * 4]
            return True
        except Exception:                                      # noqa: BLE001
            return False

    @_locked
    def set_alpha_output(self, on):
        """运行中切换 Spout/NDI 的 Alpha 输出：立即按当前分辨率重建画布。

        关闭 → RGB32 黑底（原行为）；开启 → ARGB32 透明底。显示器窗口仍然
        黑底显示（物理显示器没有 alpha），只有 Spout/NDI 发送透明通道。
        """
        on = bool(on)
        if on == self._alpha_out:
            return
        with self._tick_lock:
            self._alpha_out = on
            self._any_alpha = bool(on)
            w = max(160, min(7680, int(self.canvas.width())))
            h = max(120, min(4320, int(self.canvas.height())))
            self.canvas = QImage(w, h, self._canvas_format())
            self._clear_canvas(self.canvas)
            self._latest_frame = None
            # ★ 单路 alpha：整合画布 / 各屏画布也必须按新格式重建（尺寸没变时
            #   `_rebuild_output_plan` 靠 format 比较兜底，这里再清空一次双保险）
            self._combined_canvas = None
            self._screen_canvases.clear()
            self._plan_rev = int(getattr(self, "_plan_rev", 0)) + 1
            self._plan_rev_ready = -1

    @_locked
    def set_canvas_size(self, w, h):
        """画布尺寸（输出分辨率）。GUI 线程直接调用，锁内重建，与 worker 的合成串行化。
        之前 GUI 线程直接赋值 engine.canvas 会与 worker 写画布竞态（闪退/尺寸失效根因）。"""
        w = max(160, min(7680, int(w)))
        h = max(120, min(4320, int(h)))
        with self._tick_lock:
            self.canvas = QImage(w, h, self._canvas_format())
            self._clear_canvas(self.canvas)

    # ---- 线程（由 ui_main 把引擎 moveToThread 到独立线程后调用） ----
    def frame_interval_ms(self):
        """主循环间隔（毫秒）—— 读 `cfg["perf"]["render_fps"]`，钳到 1~120fps。

        为什么做成可配：素材大多是 25~30fps，而按 60fps 合成时**有一半左右是把同一帧
        重复合成一遍** —— 在 4 核老机器上是纯浪费（用户目标是把 CPU 让给 VDJ / 直播）。
        若 `perf.fps_follow_source` 为真，`_auto_fps_tick` 会把有效帧率压到素材帧率，
        避免对 30fps 素材做 60fps 的重复合成。
        ★ 2026-11-12：下限从 15 放宽到 1 —— 静音待机用 2fps（`silent_standby_fps`）。
        """
        try:
            fps = int((self.cfg["perf"] or {}).get("render_fps", 60) or 60)
        except Exception:
            fps = 60
        if self._effective_fps:
            fps = min(fps, int(self._effective_fps))
        fps = max(1, min(120, fps))
        return max(1, int(round(1000.0 / fps)))

    @_locked
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

    @_locked
    def set_fps_follow_source(self, on):
        """Toggle adaptive cadence and re-apply the timer immediately."""
        try:
            if hasattr(self.cfg, "set"):
                self.cfg.set("perf", "fps_follow_source", bool(on))
            else:
                self.cfg.data.setdefault("perf", {})["fps_follow_source"] = bool(on)
            if hasattr(self.cfg, "save"):
                self.cfg.save()
        except Exception:
            pass
        self._effective_fps = None
        try:
            QMetaObject.invokeMethod(self, "_apply_render_fps", Qt.QueuedConnection)
        except Exception:
            pass

    @Slot()
    def start(self):
        """在引擎所属线程里启动主循环（QTimer 必须在所属线程启动）"""
        # ★ 2026-11-11（用户：「操作非常卡」）：引擎线程降到 BELOW_NORMAL。
        #   GIL 的排队规则不看优先级，但**操作系统**的 CPU 调度看；引擎是纯后台
        #   产出，GUI 是交互线程，让 GUI 先拿到时间片能明显减少点击迟滞。
        try:
            from PySide6.QtCore import QThread as _QT
            if _QT.currentThread() is self.thread():
                import ctypes as _ct
                _ct.windll.kernel32.SetThreadPriority(
                    _ct.windll.kernel32.GetCurrentThread(), 0)
        except Exception:                                      # noqa: BLE001
            pass
        # Create the optional GPU context here: this slot runs in the engine
        # thread, which is where every QOpenGLContext call must happen.
        try:
            self._init_gpu_backend()
        except Exception:
            pass
        try:
            self._init_render_client()
        except Exception:
            pass
        if self._render_client is None:
            try:
                self._init_gpu_stage()
            except Exception:
                pass
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
            lay.lid = str(ld.get("lid") or lay.lid)
            lay.visible = ld.get("visible", True)
            lay.opacity = float(ld.get("opacity", 1.0))
            lay.blend = ld.get("blend", "normal") or "normal"
            lay.solo = bool(ld.get("solo", False))
            lay.speed = float(ld.get("speed", 1.0) or 1.0)
            # ★ 2026-11-16（用户要求）：UI 里的「静音时暂停播放」已删除，
            #   只保留「无声音时隐藏本层」。旧配置即使还写着 pause_silent=True，
            #   加载时也强制关掉 —— 否则用户看不到开关、层却一直冻着。
            lay.pause_silent = False
            lay.use_alpha = bool(ld.get("use_alpha", True))
            lay.lock_motion = bool(ld.get("lock_motion", False))
            lay.img_scale = float(ld.get("img_scale", 1.0) or 1.0)
            lay.img_x = float(ld.get("img_x", 0.0) or 0.0)
            lay.img_y = float(ld.get("img_y", 0.0) or 0.0)
            lay.fixed = int(ld.get("fixed", -1))
            lay.hide_silent = bool(ld.get("hide_silent", False))
            lay.stop_switch = bool(ld.get("stop_switch", False))
            lay.role = ld.get("role", "bg") or "bg"
            lay.auto_mode = bool(ld.get("auto_mode", False))
            lay.is_kv = bool(ld.get("is_kv", False))
            # ★ 码表绑定关系必须持久化回来（否则重启后徽标/接管关系丢了，用户以为"码表没生效"）
            lay.is_cuesheet = bool(ld.get("is_cuesheet", False))
            lay.cuesheet_id = str(ld.get("cuesheet_id", "") or "")
            lay.auto_clips = []
            for p in ld.get("auto_clips", []):
                m = self.library.get(p)
                if m is None:
                    m = MediaItem(p)
                    self.library[p] = m
                lay.auto_clips.append(m)
            lay.clips = []
            lay.clip_xforms = []
            _xfs = ld.get("clip_xforms") or []
            for _ci, p in enumerate(ld.get("clips", [])):
                m = self.library.get(p)
                if m is None:
                    m = MediaItem(p)
                    self.library[p] = m
                lay.clips.append(m)
                _xf = _xfs[_ci] if _ci < len(_xfs) and isinstance(_xfs[_ci], dict) else None
                lay.clip_xforms.append(dict(_xf) if _xf is not None
                                       else lay._default_clip_xform(m))
            self.layers.append(lay)
        self._switch_layer_to(self.layers[0], 0, instant=True) if self.layers else None

    @_locked
    def push_layers_to_cfg(self):
        self.cfg.data["layers"] = [l.to_dict() for l in self.layers]

    def push_layers_to_cfg_now(self):
        """GUI 线程直接落盘图层列表（**不抢渲染锁**）。

        ★ 2026-11-26：用户加/删图层卡十几秒。`push_layers_to_cfg()` 带
          `@_locked`，引擎忙时 GUI 最多等 2 秒；而图层对象本来由 GUI 线程编辑，
          这里只是把 `engine.layers` 序列化进 `cfg`，不碰渲染状态 ⇒ 不需要锁。
        """
        self.cfg.data["layers"] = [l.to_dict() for l in self.layers]

    # ---------------- 自动匹配（听歌识曲→自动配画面） ----------------
    def set_audio_engine(self, audio_engine):
        """注入**音频引擎**本体（不是它的 state）。ui_main 在构造本对象后立刻调用。

        为什么要单独一个入口（2026-10-17）：`__init__` 的第二个参数 `audio_state` 传进来的是
        `AudioEngine.state`（AudioState），拿不到 `_recognizer_engine.current_song_id` ——
        那是"电荷条确认后**稳定**的歌 id"，而 `state.recognized_song_id` 会被逐拍覆写。
        码表自动跟拍必须两者都能读，所以引擎本体要单独拿到手。
        未注入时一切逻辑退化成"只读 state"，不会抛异常。
        """
        self._audio_engine = audio_engine

    @_locked
    def set_song_grids(self, mapping):
        """注入 song_id → grid 映射（离线八拍网格）。ui_main 在曲库就绪后 / 扫描后调用。

        grid 结构：{bpm, beat_len, anchor, phrase, contrast, src, conf, dur, …}，
        统一约定 **八拍头 = anchor + 8n × beat_len**（beat_len=60/bpm）。
        只有「字段完整 + conf 足够高」的 grid 才会被 ui_main 注入（见 _inject_song_grids）。
        mapping=None / {} 时等价于关闭网格模式（退回现有拍钟逻辑）。"""
        self._song_grid_lookup = mapping or {}
        # 换库 / 重扫 / 改网格：段落是按旧 anchor 吸附的 ⇒ 缓存必须一起作废
        self._sec_cache = None
        # 换库 / 重扫：旧的网格时钟坐标系作废
        self._reset_grid_clock()

    @_locked
    def set_song_sections(self, mapping):
        """注入 `song_id → (raw_sections, dur, grid)`（导入包带的段落表）。

        ★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：`_sections_for_sid()`
          原来**只按路径**查 `cfg["music_meta"]`，而导入的歌文件不在本机、没有 path
          ⇒ 段落表永远取不到 ⇒ 导入曲库没有段落色带、不按段落切素材。
          `mapping=None/{}` 等价于关闭（本机曲库不受影响）。

        ⚠ 与 `set_song_grids` 一样是**整体替换引用**（GIL 下原子），不就地改 dict ——
          引擎线程可能正在读。
        """
        self._song_sec_lookup = mapping or {}
        # 换库/重导入：段落缓存可能指向旧对象，作废
        self._sec_cache = None

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
        self._bar_off_sid = None          # 手动相位属于哪首歌（换歌即作废，见 _beat_pos_grid）
        self._grid_pending = None
        self._grid_seen = None
        self._grid_segkey = None       # 当前段/锚标识（分段/多锚变化时硬重锚，见 _grid_observe）
        self._grid_flip = 0.0          # 手动八拍相位翻转量（0/4 拍），换库/重扫时清掉
        _publish_grid_hint(self, -1.0)

    @staticmethod
    def _grid_segment_of(grid, off):
        """按 offset 取「当前段/锚」的 `(beat_len, anchor, segkey)`。

        · 有 `segments`（分段 BPM）→ `segment_at(segments, off)` 取当前段的 beat_len/anchor；
        · 有 `anchors`（多八拍头，无分段）→ `anchor_for(grid, off)` 取 offset 前最近八拍头；
        · 都没有（老曲目）→ 直接用 `grid.beat_len / grid.anchor`，**segkey=None**（不触发重锚）。
        """
        try:
            import beatgrid as _bg
        except Exception:                                        # noqa: BLE001
            _bg = None
        segments = grid.get("segments") or []
        anchors = grid.get("anchors") or []
        if segments and _bg is not None:
            seg = _bg.segment_at(segments, off)
            bl = float(seg.get("beat_len") or 0.0)
            anchor = float(seg.get("anchor") or 0.0)
            return bl, anchor, ("seg", round(bl, 4), round(anchor, 4))
        if anchors and _bg is not None:
            bl = float(grid.get("beat_len") or 0.0)
            anchor = float(_bg.anchor_for(grid, off))
            return bl, anchor, ("anchor", round(bl, 4), round(anchor, 4))
        bl = float(grid.get("beat_len") or 0.0)
        try:
            anchor = float(grid.get("anchor"))
        except (TypeError, ValueError):
            anchor = None
        return bl, anchor, None

    def _reanchor_across_segment(self, clock, bl, anchor, off, r, ref_t):
        """跨分段 / 跨八拍锚点时的**连续**硬重锚（T05① 跨段 continuity）。

        为什么不能只调 `GridClock.reanchor`：段边界换了坐标系（BPM 与锚点都变），
        `pos0 = (off - anchor) / beat_len` 在边界处**整体跳变**（实测跨段一次 −256 拍）。
        而 `_beat_pos` 返回的拍位 = 网格坐标 + `_grid_coord_delta`，delta 只在
        「网格↔音频」边沿重算 ⇒ 段内跳变会**原样穿到消费方**：
          · `_auto_switch_tick` 的 `pos < self.switch_pos` 把它当成「拍钟被重置」
            ⇒ `switch_pos = pos`、`_next_due = None` ⇒ **倒计时从头数、多切一次素材**；
          · 若新坐标反而比旧的大，同一帧 `pos >= due` ⇒ 边界那一拍立刻白切一次。

        修法（复用「网格↔音频」边沿已有的**坐标续接**思路，不新造机制）：
          ① 重锚前后各取一次 `clock.pos(ref_t)`，把差值（新旧坐标系原点差）叠进
             `_grid_coord_delta` ⇒ 返回拍位在边界处**连续**，于是 `switch_pos` /
             `_next_due` 全部继续有效：**不重置、不多切**。
          ② 对齐相位 `off = (delta + flip) % 8` 随 delta 改变。若不管它，
             `_next_switch_due` 会在同一帧把已排好的目标「重新贴线」，最多可**回退 4 拍**
             ⇒ 又会提前触发一次切换。这里把 `_due_off` 同步成新相位（= 承认相位变了，
             但保留原定目标），令那一次重贴线不执行；下一个间隔照常按新相位排。
             注：`_bar_off_user`（用户按「下一素材」重定义过相位）优先于 delta，此时相位
             与 delta 无关 ⇒ **不**同步 `_due_off`（否则会凭空触发一次重贴线）。
          老曲目（无 segments/anchors）`segkey is None`，本方法**根本不会被调用**
          ⇒ `_grid_coord_delta` / `_due_off` 逐值不变（零回归）。
        """
        try:
            old_raw = clock.pos(ref_t)
        except Exception:                                        # noqa: BLE001
            old_raw = None
        clock.reanchor(bl, anchor, off, r, ref_t)
        try:
            new_raw = clock.pos(ref_t)
        except Exception:                                        # noqa: BLE001
            new_raw = None
        if old_raw is None or new_raw is None:
            return                       # 取不到就不做续接（退化为旧行为，不会更糟）
        shift = float(old_raw) - float(new_raw)
        if abs(shift) <= 1e-9:
            return                       # 坐标系没变（如 anchors 里重复项）：什么都不用做
        self._grid_coord_delta = float(getattr(self, "_grid_coord_delta", 0.0)) + shift
        self._grid_last_pos = new_raw
        if getattr(self, "_bar_off_user", None) is None:
            self._due_off = (self._grid_coord_delta
                             + float(getattr(self, "_grid_flip", 0.0))) % 8.0

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
        off = snap.get("recognized_offset", -1.0)
        if off is None or off < 0:
            return None
        off = float(off)
        # ★ 当前段/锚：支持 grid_manual.segments（分段 BPM）/ anchors（多八拍头）。
        #   老曲目（无 manual）⇒ 单段 = grid.beat_len/anchor、segkey=None（不触发重锚，零回归）。
        bl, anchor, segkey = self._grid_segment_of(grid, off)
        if bl <= 0 or anchor is None:
            return None
        r = snap.get("recognized_rate", 1.0)
        try:
            r = float(r)
        except Exception:
            r = 1.0
        if not (0.5 <= r <= 2.0):
            r = 1.0
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
                # ★ 段/锚切换（跨分段、或多八拍头走到另一个）⇒ **硬重锚**：段边界/锚点是
                #   离线已知的用户产物，不是识别抖动的坐标跳变 ⇒ 不走有限增益/跳变判据。
                #   off 略有抖动也会被下一个可信窗的 update() 校正回来。
                if segkey is not None and segkey != self._grid_segkey:
                    self._reanchor_across_segment(clock, bl, anchor, off, r, ref_t)
                    self._grid_segkey = segkey
                elif trusted:
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
                self._grid_segkey = segkey
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

    @_locked
    def _layer_screen(self, lay):
        """该图层"所属屏"的 screen dict（多屏自建图层后一层只属于一块屏）。

        兜底顺序：屏幕的 `layers` 里显式包含它 → 主屏（共享层/脏数据）。
        `_layer_screen_map` 在每次重建输出计划时失效重建。
        """
        try:
            m = getattr(self, "_layer_screen_map", None)
            if m is None:
                m = {}
                plan = self._out_cfg
                for s in (plan or {}).get("screens") or []:
                    ids = output_model.effective_layers(
                        plan, s, [l.lid for l in self.layers])
                    for lid in ids:
                        # 主屏先遍历 ⇒ 共享层天然落到主屏那套（上面注释里的兜底）
                        m.setdefault(str(lid), s)
                self._layer_screen_map = m
            return m.get(str(getattr(lay, "lid", "")))
        except Exception:                                          # noqa: BLE001
            return None

    def _screen_params_refresh(self):
        """重建「每屏一份生效参数」缓存。**只在参数/计划变化后算一次**，帧内只读。

        用户拍板（2026-11-09）：每块屏都是独立的，Kv 待机与逐拍交替不该互相影响。
        缓存键 = 屏 id；失效条件 = 输出计划版本号 `_plan_rev` 变了（改任何参数、
        增删屏、切模式都会 bump 它）。
        """
        try:
            rev = int(getattr(self, "_plan_rev", 0) or 0)
        except (TypeError, ValueError):
            rev = 0
        if getattr(self, "_params_cache_rev", None) == rev:
            return
        cache = {}
        try:
            data = getattr(self.cfg, "data", None)
            if not isinstance(data, dict):
                data = self.cfg if isinstance(self.cfg, dict) else {}
            for sc in (getattr(self, "_out_cfg", None) or {}).get("screens") or []:
                try:
                    # ★ 2026-11-26：主屏幕 = 全局设置（单屏/多屏共用），
                    #   不再读主屏 params。旧配置里残留的主屏 override 也不会
                    #   在单屏/普通演出台里偷偷生效。
                    if str(sc.get("type") or "") == "main":
                        continue
                    cache[str(sc.get("id") or "")] = output_model.effective_params(data, sc)
                except Exception:                                  # noqa: BLE001
                    continue
        except Exception:                                          # noqa: BLE001
            cache = {}
        self._params_cache = cache
        self._params_cache_rev = rev

    def _params_for(self, lay=None):
        """该图层**所属屏**的生效参数（无计划/无归属 ⇒ 全局面，行为与改动前一致）。

        ⚠ 帧内调用：只读缓存，绝不做深拷贝（`effective_params` 每屏每帧跑一次太重）。
        """
        try:
            self._screen_params_refresh()
            if lay is not None:
                sc = self._layer_screen(lay)
                if sc is not None:
                    got = (getattr(self, "_params_cache", None) or {}).get(
                        str(sc.get("id") or ""))
                    if got:
                        return got
        except Exception:                                          # noqa: BLE001
            pass
        # ★ 2026-11-11（P0：单屏预览黑屏 + 多屏持续卡顿）：
        #   `self.cfg` 是 `Config` 对象，**没有 `.get()`**。这里以前写成
        #   `cfg.get("auto")` ⇒ 每次 Kv 待机层 tick 都抛 AttributeError，
        #   `_tick` 被整个中断（合成/输出都没跑）⇒ 预览黑屏；异常栈以 30fps
        #   反复构造又把 GIL/CPU 拖满 ⇒ 操作卡。这里按 Config/dict 两种形态取值。
        cfg = self.cfg
        try:
            if isinstance(cfg, dict):
                _g = cfg.get
            else:
                def _g(k, d=None):
                    try:
                        return cfg[k]
                    except Exception:                          # noqa: BLE001
                        return d
            return {"auto": _g("auto") or {}, "color": _g("color") or {},
                    "postfx": _g("postfx") or {}, "kv": _g("kv") or {},
                    "beat": _g("beat") or {}}
        except Exception:                                      # noqa: BLE001
            return {"auto": {}, "color": {}, "postfx": {}, "kv": {}, "beat": {}}

    def _auto_for(self, lay=None):
        """该层所属屏的 `auto` 参数（没有 → 全局）。U7 第二步。

        ★ 2026-11-26：主屏幕 = 全局，主屏 params 一律忽略。
        """
        try:
            s = self._layer_screen(lay) if lay is not None else None
            if (s is not None and str(s.get("type") or "") != "main"
                    and (s.get("params") or {}).get("auto")):
                return output_model.effective_params(self.cfg, s)["auto"]
        except Exception:                                          # noqa: BLE001
            pass
        return self.cfg["auto"]

    def _refresh_auto_layers(self, snap, force_tier=None, force=False, instant=True,
                             presec=False):
        """用 `match_clips` 重新挑自动图层的素材池并立即切换。

        ★★ 2026-10-30 用户「大砍刀」：**曲风没了**，所以这里不再有"曲风变化才重算"
          的门控（`_current_genres` / `_last_genres` / `_auto_refresh_t` 一并删除）。
          现在的重算时机只有两个：
            · `force_tier` 给定时（能量跨档 / 段落切换 / 实时档位变化）——按该档重 roll；
            · `force=True`（换库、重扫素材、手动重排角色后由 UI 调用）；
            · 否则（每帧 tick 调进来）**只在某层池子为空时补一次**，绝不每帧重 roll
              （那会让随机序列每帧变化 ⇒ 画面狂切）。
        force_tier: None / "low" / "mid" / "high" —— 池子按动态档过滤/前置。
        instant: True = 立即硬切（旧行为）；False = 有旧画面时走当前过渡（段落/能量
                 跨档用 False，避免用户选了淡入淡出却被 instant 硬切）。"""
        # ★ 段落接管时用目标能量（见 `_energy_for_switch`）
        self._phase = "refresh_pool"
        energy = self._energy_for_switch(snap)   # 注意：0.0 就是 0.0，不能当成 0.5
        changed = []
        for lay in self.layers:
            if not getattr(lay, "auto_mode", False):
                continue
            # ★ 多屏：该层所属屏如果明确设为“手动”，段落/能量换池不能偷偷切它
            try:
                _sc = self._layer_screen(lay)
                if _sc is not None and self._mode_for_screen(_sc) == SWITCH_MANUAL:
                    continue
            except Exception:                                  # noqa: BLE001
                pass
            if force_tier is None and not force and lay.clips:
                continue                      # 池子非空且非强制 ⇒ 保持现状（不 churn）
            # 记住当前正在播的素材：换池时保留画面，等切换点（小节线）再切
            cur_path = lay.clips[lay.cur].path if (0 <= lay.cur < len(lay.clips)) else None
            old_item = (lay.clips[lay.cur]
                        if (0 <= lay.cur < len(lay.clips)) else None)
            _hist = [str(p) for p in (getattr(lay, "_pool_history", None) or []) if p]
            _old = [str(getattr(c, "path", "") or "") for c in (lay.clips or [])]
            _ex = {p for p in (_hist + _old) if p}
            # 换池时按**素材路径**保留用户调过的大小/位置；否则自动层每次
            # 段落/能量重 roll 都会把刚调好的 xform 重置回默认值。
            try:
                _xf_keep = {}
                for _c, _x in zip(lay.clips or [], lay.clip_xforms or []):
                    _p = str(getattr(_c, "path", "") or "")
                    if _p and isinstance(_x, dict):
                        _xf_keep[_p] = dict(_x)
            except Exception:                                  # noqa: BLE001
                _xf_keep = {}
            picked = list(match_clips(energy, self.library, role=lay.role, n=4,
                                      tier=force_tier, exclude_paths=_ex))
            # ★ 2026-11-26：换池时记住刚换下来的当前素材；面板会把它作为
            #   “上一段（已换下）”的缩略图留在自动层素材池末尾。
            lay._prev_pool_clip = old_item
            lay.clips = list(picked)
            lay.clip_xforms = [
                dict(_xf_keep[_p]) if (_p := str(getattr(_m, "path", "") or ""))
                and _p in _xf_keep else lay._default_clip_xform(_m)
                for _m in picked]
            lay.auto_clips = list(picked)
            _h = list(getattr(lay, "_pool_history", None) or [])
            for _c in picked:
                _cp = str(getattr(_c, "path", "") or "")
                if _cp and _cp not in _h:
                    _h.append(_cp)
            lay._pool_history = _h[-64:]
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
            # ★ 2026-11-21：drop 段内被逐拍交替接管的前景层，换池后**不立刻切**，
            #   留给 `_beat_tick` 用新池直接起 A/B（否则同帧两刀，闪一下）。
            if self.beat_owns(lay):
                lay.pair = None
                lay.beat_sec = None
                lay.cur = -1
                lay.prev_idx = -1
                lay.pre_idx = -1
                changed.append(self.layers.index(lay))
                continue
            if force_tier is not None or lay.player is None or lay.cur < 0:
                # ★★★ 2026-11-07（用户：「The Songbird 来回放，素材固定在那几个」）：
                #   这里原来**永远挑新池的第 0 个**（`_switch_layer_to(lay, 0)`）。
                #   段落边界每次都走 `force_tier` ⇒ 每次都切到池里的第 0 个；
                #   而自动层的兜底切换又被 `_apply_section_switch` 跳过（避免同帧两刀），
                #   于是整首歌反复看到的就是同样那一两个素材。
                #   ⇒ 在池里**随机挑一个、且尽量避开当前正在播的**。
                _k = 0
                # ★ 段落边界前 `_presec_tick` 已按下一段档位随机选好并预热一个素材；
                #   这里只要它仍在新池里就直接用它（首帧现成），否则退回纯随机。
                #   只影响“段落边界”这一刀，不对普通按拍切换或缓存素材做任何偏向。
                _presec_path = getattr(lay, "_presec_path", None) if presec else None
                if presec:
                    lay._presec_path = None
                try:
                    if len(picked) > 1:
                        _cand = [i for i, _c in enumerate(picked)
                                 if getattr(_c, "path", None) != cur_path]
                        _pre = -1
                        if _presec_path:
                            for _idx in _cand:
                                if getattr(picked[_idx], "path", None) == _presec_path:
                                    _pre = _idx
                                    break
                        _k = _pre if _pre >= 0 else random.choice(
                            _cand or list(range(len(picked))))
                except Exception:                                # noqa: BLE001
                    _k = 0
                lay.cur = -1
                lay.prev_idx = -1
                lay.pre_idx = -1
                if (not instant) and lay.player is not None:
                    self._switch_layer_to(lay, _k, instant=False)
                    if old_item is not None:
                        lay._trans_prev_item = old_item
                else:
                    self._switch_layer_to(lay, _k, instant=True)
                try:
                    self._schedule_warm(lay, delay=0.8)      # 提前暖下一个候选
                except Exception:                            # noqa: BLE001
                    pass
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

    # ---------------- 能量段落（曲库预设）----------------
    def _sections_for_sid(self, sid):
        """`song_id` → 该歌的段落表（已 normalize，带缓存）。取不到返回 `[]`。

        存储位置 `cfg["music_meta"][path]["sections"]`（与 `grid` 并列，随曲库走）。
        曲长优先取 `meta["dur"]`，退化到 `grid["dur"]`；都没有就只用手填的末段 t1。
        """
        try:
            path = self._cue_song_path(int(sid)) if int(sid) >= 0 else ""
        except (TypeError, ValueError):
            path = ""
        raw, dur, grid = [], 0.0, {}
        if path:
            try:
                rec = (self.cfg["music_meta"] or {}).get(path) or {}
                raw = rec.get("sections") or []
                dur = float(rec.get("dur") or 0.0)
                grid = rec.get("grid") or {}
                if dur <= 0:
                    dur = float(grid.get("dur") or 0.0)
            except Exception:                                 # noqa: BLE001
                raw, dur, grid = [], 0.0, {}
        # ★★ 2026-10-31：本机没有（或没段落）⇒ 查**导入包**里的段落表。
        #   导入歌 `path == ""`（文件不在这台机器），以前这里直接 `return []`。
        if not raw and int(sid) >= 0:
            try:
                imp = self._song_sec_lookup.get(int(sid))
            except Exception:                                 # noqa: BLE001
                imp = None
            if imp:
                try:
                    raw = imp[0] or []
                    dur = float(imp[1] or 0.0) or dur
                    grid = imp[2] or grid
                    if dur <= 0:
                        dur = float((grid or {}).get("dur") or 0.0)
                except Exception:                             # noqa: BLE001
                    raw = []
        # ★ 缓存按 **raw 列表的引用** 失效：详情页保存时替换成新 list ⇒ 这里 `is` 判否
        #   ⇒ 自动重算。这样不需要"保存后记得通知引擎"，少一个易漏的跨模块调用。
        key = path or ("imp", int(sid))
        cached = getattr(self, "_sec_cache", None)
        if cached is not None and cached[0] == key and cached[1] is raw:
            return cached[2]
        # ★ 传 grid ⇒ `normalize` 会把每个 t0 吸附到最近的八拍头
        #   （与 waveform_mark.grid_times 同一组 anchor/beat_len）。
        # ??????????????? snap??? 4 ??????? 8 ??Remix 107.504?
        secs = sections.normalize(raw, dur, None) if raw else []
        self._sec_cache = (key, raw, secs)
        self._sec_path = path
        return secs

    def _energy_for_switch(self, snap):
        """「选池 / 切档 / 候选偏好 / 过渡时长」该用的能量。

        有段落目标就用目标，否则用实测（`snap_energy` 的唯一正确取法）。

        ⚠ **不改写 `snap["energy"]`**：那个值还要给界面显示、给后处理的
          `drive=energy` 用。改成段落值等于给用户看假能量、后处理强度也跟着假。
        """
        tgt = getattr(self, "_sec_energy", None)
        if tgt is not None:
            return float(tgt)
        return snap_energy(snap)

    def _section_log(self, msg):
        """段落取证：写一行 `[S]` 到 `cue_health.log`（失败静默，绝不影响渲染）。

        ★ 2026-11-11（现场：主线程 150~600ms 停顿）：旧实现直接在**引擎线程**上
          `open/append`，超过 800 行还会整文件读+重写。磁盘一忙就持着 GIL 卡住
          GUI/预览几十到几百毫秒。现在复用 `_LayerHealthLog` 的后台写线程：
          渲染线程只 `put_nowait`，裁剪也在后台做。
        """
        try:
            lg = getattr(self, "_cue_section_log", None)
            if lg is None:
                path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                                    "cue_health.log")
                lg = _LayerHealthLog(path, max_lines=800, keep=400)
                self._cue_section_log = lg
            lg.write("[%s] [S] %s" % (time.strftime("%H:%M:%S"), msg))
        except Exception:                                     # noqa: BLE001
            pass

    @staticmethod
    def _build_ups_of(g):
        """兼容单个旧 build_up / 新 build_ups 列表，返回 [{start,beats}]。"""
        if not isinstance(g, dict):
            return []
        v = g.get("build_ups")
        if not v:
            v = g.get("build_up")
        if isinstance(v, dict):
            v = [v]
        if not isinstance(v, (list, tuple)):
            return []
        out = []
        for bu in v:
            if not isinstance(bu, dict):
                continue
            try:
                out.append({"start": max(0.0, float(bu.get("start") or 0.0)),
                            "beats": max(1, int(bu.get("beats") or 32))})
            except (TypeError, ValueError):
                continue
        return out

    def _build_up_at(self, sid, sec):
        """当前秒是否落在该歌曲的任一 Build Up 段；是则返回 {start,end,beats}。"""
        try:
            g = self._song_grid_lookup.get(sid)
            if not isinstance(g, dict) or sec is None:
                return None
            bl = float(g.get("beat_len") or 0.0)
            if bl <= 0:
                return None
            for bu in self._build_ups_of(g):
                start = float(bu["start"])
                beats = int(bu["beats"])
                end = start + beats * bl
                if start <= float(sec) < end:
                    return {"start": start, "end": end, "beats": beats}
        except Exception:                                      # noqa: BLE001
            pass
        return None

    def _section_tick(self, snap):
        """按「曲库预设段落表」推进能量档位（0.2s 级成本，每帧调也不贵）。

        接管条件三条全中：
          ① 指纹认到歌（`CuePositionTracker.song_id >= 0`）；
          ② 位置可信（`pos_sec_at()` 非 None —— 内部已含丢票保持 / 过期不外推）；
          ③ 这首歌在曲库里存了段落表。
        任一不满足 ⇒ `_sec_energy=None`，完全退回「实测能量 + 滞回 + 2s 稳定」。

        ⚠ 不用 `_cue_pos_beat_len()` 换秒：那是**码表**拍长（没装表时退化成 1.0），
          而段落表里存的就是秒。直接要原曲秒，与 BPM / 码表是否装表都无关。
        """
        trk = getattr(self, "_cue_pos_tracker", None)
        sid = -1
        sec = None
        now = time.perf_counter()
        try:
            sid = int(getattr(trk, "song_id", -1))
            pss = getattr(trk, "pos_sec_at", None)
            if callable(pss):
                sec = pss(now)
        except Exception:                                     # noqa: BLE001
            sid, sec = -1, None

        if sid < 0 or sec is None:
            # ★★ 2026-10-30 现场（Dust：实时 BPM 误判 219.5 → BPM 一致性否决 → 掉锁 11 秒）：
            #   旧实现在这里**立刻**把 `_sec_energy/_sec_tier` 清成 None ⇒ 实测能量接管
            #   （当时日志 `E=0.82 tier=mid`，而这一段的段落档位是 high）⇒ 能量档从 high
            #   掉到 mid ⇒ `_energy_tier_switch` 重 roll 素材池 + 立刻切 ⇒ 用户看到的
            #   **「素材强切了一下」**；等位置回来，`_apply_section_switch` 又把档位推回
            #   high ⇒ 再切一次（一掉一认 = 两次强切）。
            #   **掉锁 ≠ 音乐变了**：短暂丢票 / 位置过期期间冻结段位（池、素材、档位都不动）
            #   才符合现场预期；超过 `_SEC_HOLD_SEC` 仍没回来才真退回实测能量。
            _lose_t = float(getattr(self, "_sec_lost_t", 0.0) or 0.0)
            if not _lose_t:
                _lose_t = now
                self._sec_lost_t = now
            if (now - _lose_t) < _SEC_HOLD_SEC and self._sec_tier is not None:
                if getattr(self, "_sec_last_state", None) != "冻结":
                    self._sec_last_state = "冻结"
                    self._section_log("掉锁冻结：保持段位 %.1fs（不退回实测能量，避免强切）"
                                      % (now - _lose_t))
                self._sec_info = {"state": "冻结", "lost": round(now - _lose_t, 2),
                                  "tier": self._sec_tier}
                return
            # 没认到歌 / 位置过期：**不接管**，但**不清段索引**。
            # 为什么不清：短暂丢票（HOLD_N 次内）之后位置会回来，清空会让
            # "丢一帧 → 回落实测能量 → 掉档 → 位置回来再升档"抖一次，现场看得见。
            self._sec_energy = None
            self._sec_tier = None
            self._sec_info = {"state": "无位置"}
            if getattr(self, "_sec_last_state", None) != "无位置":
                self._sec_last_state = "无位置"
                self._section_log("未接管：没认到歌或位置过期（退回实测能量）")
            return

        # ★★★ 2026-11-07（用户：「The Songbird 来回放，素材固定不动」）：
        #   `cue_health.log` 会被环形裁剪，播放结束后的静音期足以把"切换记录"全刷掉
        #   ⇒ 事后查不到。这里加一条**每 20 秒**的心跳，记录段索引 / 累计切换次数 /
        #   距上次切换多久 / 每层当前素材索引 ⇒ 下次一定能看见到底有没有在切。
        try:
            _now_hb = time.perf_counter()
            if _now_hb - float(getattr(self, "_sec_hb_t", 0.0)) >= 20.0:
                self._sec_hb_t = _now_hb
                _cur = ",".join(str(getattr(l, "cur", -1)) for l in (self.layers or [])[:4])
                _since = _now_hb - float(getattr(self, "_sec_last_switch_t", 0.0))
                self._section_log(
                    "心跳 段%d/%s 切换%d次 距上次%.1fs 素材[%s] 运行=%s"
                    % (int(getattr(self, "_sec_idx", -1)) + 1,
                       str(getattr(self, "_sec_energy", None)),
                       int(getattr(self, "_sec_switch_n", 0)), _since, _cur,
                       bool(getattr(self, "running", False))))
        except Exception:                                      # noqa: BLE001
            pass
        secs = self._sections_for_sid(sid)
        if not secs:
            # ★★ 2026-10-28 真机抓到（r35 一首歌切了 15 次）：**绝对不能在这里清
            #   `_sec_applied_idx`**。指纹在两首歌之间抖动时（`sig` 短暂失效 / 误识别成
            #   别的不带段落表的歌），每一抖都会走到这条分支；一清掉索引，认回来时
            #   `i != -1` 又成立 ⇒ 又切一次 ⇒ 变成「每隔几秒重切一次」。
            #   实测 r36 的日志加了 `why=` / `reset=N` 之后定位：正常一首歌 `reset`
            #   只有 1 次（首次绑定），出问题时 `reset` 会涨到 15。
            #   表要清（不能用上一首的表），但**索引要留着**（同一首歌认回来就不该重切）。
            self._sec_list = []
            self._sec_idx = -1
            self._sec_energy = None
            self._sec_tier = None
            self._presec_key = None
            for _lay in self.layers:
                _lay._presec_path = None
            self._sec_reset_n += 1
            self._sec_why = "no-table"
            self._sec_info = {"state": "本曲无段落表"}
            if getattr(self, "_sec_last_state", None) != "本曲无段落表":
                self._sec_last_state = "本曲无段落表"
                self._section_log("未接管：sid=%s 这首歌没存段落表（退回实测能量）" % sid)
            return

        # 换歌 ⇒ 段索引必须重来，否则新歌开场会沿用上一首 drop 的档位
        if self._sec_seen_path != self._sec_path:
            self._sec_seen_path = self._sec_path
            self._sec_idx = -1
            self._sec_applied_idx = -1
            self._presec_key = None
            for _lay in self.layers:
                _lay._presec_path = None
            self._sec_reset_n += 1
            self._sec_why = "song-change"

        self._sec_list = secs
        self._sec_lost_t = 0.0          # 位置回来了 ⇒ 掉锁计时复位（见掉锁冻结分支）
        # ★ 2026-11-08：段落判定**不再提前一拍**（SECTION_ADVANCE_BEATS=0）。
        #   历史 2026-11-06 曾加 1 拍提前量；用户实测要求去掉，段落边界按真实秒数判。
        #   `_adv_beats` 保留为常量入口，未来如需微调只改类常量。
        _adv_beats = float(getattr(self, "SECTION_ADVANCE_BEATS", 0.0))
        # ⚠ `trk` 在**本函数更后面**才赋值（`_trk = self._cue_pos_tracker`），
        #   这里直接用会 NameError ⇒ 整个 `_section_tick` 每帧抛异常 ⇒
        #   段落切换**一次都不执行**（用户 2026-11-07：素材固定不动）。
        #   拍长从 tracker 上取；取不到就不加提前量（绝不因此不切段）。
        _bl = 0.0
        try:
            _trk0 = getattr(self, "_cue_pos_tracker", None)
            _bl = float(getattr(_trk0, "beat_len", 0.0) or 0.0)
        except Exception:                                      # noqa: BLE001
            _bl = 0.0
        _sec_eff = float(sec) + _adv_beats * _bl if _bl > 1e-6 else float(sec)
        # ★ 2026-11-26：Build Up 闪白和段落判定共用同一个“当前原曲秒”。
        #   导入包识别时 recognized_t 有时为 0；没有它时闪白不能直接放弃，
        #   否则段落在、素材在切，但永远不会白。
        try:
            self._sec_last_sec = float(sec)
        except (TypeError, ValueError):
            self._sec_last_sec = -1.0
        i, cur = sections.section_at(secs, _sec_eff)
        self._sec_idx = i
        # ★ 2026-11-25：用户手工标记的 Build Up 段强制按「中动态」处理，
        #   在段开始/结束各切一次池子；其余时间仍按段落表。
        _bu = self._build_up_at(sid, sec)
        _bu_on = _bu is not None
        if _bu_on:
            cur = dict(cur)
            cur["tier"] = "mid"
            cur["name"] = "Build Up"
            cur["t0"] = float(_bu["start"])
            cur["t1"] = float(_bu["end"])
            cur["energy"] = 0.5
        if _bu_on != bool(getattr(self, "_bu_active", False)):
            self._bu_active = _bu_on
            self._sec_applied_idx = -999          # 强制下一行执行一次换池
            self._sec_why = "build-up"
        self._sec_tier = cur["tier"]
        self._sec_energy = cur["energy"]
        self._sec_info = {"state": "ok", "i": i, "n": len(secs),
                          "sec": round(float(sec), 2),
                          "tier": cur["tier"], "e": cur["energy"],
                          "name": cur["name"]}
        # ★ 下一段提前预热（见 `_presec_tick` / `__init__` 注释）：
        #   必须在 `i != _sec_applied_idx` 判段**之前**执行，边界那一帧
        #   `_apply_section_switch` 才能用上已预热的播放器。
        #   Build Up 段强制中动态时不预热下一段（否则会提前切回高动态素材）。
        if not _bu_on:
            try:
                self._presec_tick(sid, secs, i, sec)
            except Exception:                                  # noqa: BLE001
                pass
        # ★★ 只认**段索引变化**（2026-10-28 真机验收修复）：
        #   原来还带了 `or prev_tier != cur["tier"]`，而 `_sec_tier` 会在「位置短暂失效」
        #   分支被清成 None ⇒ 位置一恢复 `None != "mid"` 就再切一次 ⇒ 实测 150 秒内
        #   重复切了 15 次（同一段在 88/91/93 秒反复切，间隔正好是位置层的 STALE 节奏）。
        #   档位由段决定（tier 是段里的字段），同一段不会变档位，所以那个条件本来就多余。
        if i != self._sec_applied_idx:
            # ★★★ 2026-11-25（用户：段落切换时会多插一两拍的其他素材）：
            #   两个来源都会造成「A 播完 → B 只演一两拍 → 又切 C」：
            #   ① 认歌/锁位置晚于段落边界（冷启动、丢票恢复），当前段只剩一两拍，
            #      仍然为它换一次池 ⇒ B 只闪一两拍；
            #   ② 手工 Build Up 跨过段落边界，`i` 一变就换池 ⇒ Build Up 中间多切一刀。
            #   处理：
            #   · 当前段剩余不足一个完整小节（4 拍）且画面已有素材 ⇒ **不切**，
            #     保持当前画面，等下一段边界直接切 C；
            #   · Build Up 段内忽略段落索引变化，只在进入/退出 Build Up 时换一次池。
            _skip_switch = False
            try:
                _t1 = float(cur.get("t1") or 0.0)
                _remain = _t1 - float(sec)
                _has_pic = any(
                    (getattr(_lay, "player", None) is not None
                     and int(getattr(_lay, "cur", -1)) >= 0)
                    for _lay in (self.layers or []))
                _min_hold = float(getattr(self, "SECTION_MIN_HOLD_BEATS", 4.0)) * _bl
                # Build Up **开始**这一刀必须执行（否则 Build Up 的池永远不生效）；
                # Build Up **结束**那一刀才允许被短段保持挡掉。
                _enter_bu = bool(_bu_on and self._sec_applied_idx == -999)
                if (_has_pic and not _enter_bu and _remain > 0.0 and _bl > 1e-6
                        and _remain < _min_hold):
                    _skip_switch = True
                    self._section_log(
                        "段落保持：%s 只剩 %.2f 拍（<%.1f），不切素材，等下一段"
                        % (cur.get("name", "?"), _remain / _bl,
                           float(getattr(self, "SECTION_MIN_HOLD_BEATS", 4.0))))
                elif (_bu_on and self._sec_applied_idx not in (-1, -999)):
                    _skip_switch = True
                    self._section_log(
                        "Build Up 段内跨段落边界（第%d段 %s），不换素材"
                        % (i + 1, cur.get("name", "?")))
            except Exception:                                  # noqa: BLE001
                _skip_switch = False
            self._sec_applied_idx = i
            if _skip_switch:
                self._sec_why = "short-hold"
                return
            if self._sec_why not in ("no-table", "song-change"):
                self._sec_why = "seg"
            # ★ 2026-10-30 用户报「素材切换比听起来慢了一拍」：把**迟到量**落日志
            #   （切的那一刻实际位置 − 段落边界）。>0.2s 是肉眼能看出的"慢"，
            #   换算成拍数（÷ beat_len）就能直接读"慢了几拍"。
            #   `cue_health.log` 的时间戳只到秒，靠它测不出亚秒偏差，所以必须有这个数。
            try:
                _late = float(sec) - float(cur.get("t0", 0.0) or 0.0)
                _bl = float(getattr(trk, "beat_len", 0.0) or 0.0)
                self._sec_late = _late
                self._section_log(
                    "段迟到 %.2fs%s（%s @%.2f）"
                    % (_late, ("=%.2f 拍" % (_late / _bl)) if _bl > 1e-6 else "",
                       cur.get("name", "?"), float(sec)))
            except Exception:                                     # noqa: BLE001
                pass
            self._apply_section_switch(snap, cur)

    def _presec_tick(self, sid, secs, i, sec):
        """段落边界前 `lead` 秒，按**下一段**的档位预热一个素材。

        为什么要做（2026-11-08 现场「素材掉帧」）：
          段落切换会 `_refresh_auto_layers(force_tier=...)` 重建 4 个候选并立即
          选一个切过去。若选中的是全新解码器，首帧要现开容器 + 等 GPU/软解
          （大 DXV、机械盘实测 1~2 秒）；这 1~2 秒里旧素材继续显示/冻结，
          看起来就是「素材掉帧」。提前 4 秒预热，边界那一下新播放器已经有首帧。

        随机性：这里仍然 `random.choice` 选候选，和边界原来的纯随机同分布；
        只是把“选”提前了 4 秒，不会偏向近期用过的素材。
        """
        if not getattr(self, "running", False) or getattr(self, "locked", False):
            return
        try:
            n = len(secs)
            if i < 0 or i + 1 >= n:
                return
            nxt = secs[i + 1] or {}
            t0 = float(nxt.get("t0") or 0.0)
            lead = t0 - float(sec)
            if lead <= 0.0 or lead > float(getattr(self, "_presec_lead", 4.0)):
                return
            key = (int(sid), int(i + 1))
            if key != getattr(self, "_presec_key", None):
                # 换目标段：清掉上一轮（不必关闭播放器，缓存会按 LRU 复用/淘汰）
                for lay in self.layers:
                    try:
                        lay._presec_path = None
                    except Exception:                      # noqa: BLE001
                        pass
                self._presec_key = key
                self._presec_done = set()
                self._presec_warmed = 0
            done = getattr(self, "_presec_done", None)
            if done is None:
                done = set()
                self._presec_done = done

            tier = nxt.get("tier") or "mid"
            energy = float(nxt.get("energy") or 0.0)
            try:
                # ⚠ `seed` 仍是**全局**：这里的 rng 是一个跨图层共用的随机序列，
                #   "每屏一个 seed" 需要每层各自一颗 rng（会改变现有的选片随机序列），
                #   属于下一步；本步只把**逐层决策**（cooldown / energy_map）按屏。
                seed = int(self.cfg["auto"].get("seed") or 0)
            except Exception:                                  # noqa: BLE001
                seed = 0
            rng = random.Random(seed) if seed else self.rng

            # ★ 2026-11-26：**每 tick 只预热一层**。以前一次循环把 4~8 层全部
            #   `warm_up()`（每层 20~60ms），在段落边界前那一帧卡 150~200ms，
            #   直接掉到 15fps。提前 4 秒有 100+ 个 tick，一层一个足够。
            targets = []
            for lay in self.layers:
                if id(lay) in done:
                    continue
                if getattr(lay, "fixed", -1) >= 0 or not getattr(lay, "clips", None):
                    done.add(id(lay))
                    continue
                if getattr(lay, "is_kv", False) or getattr(lay, "is_cuesheet", False):
                    done.add(id(lay))
                    continue
                targets.append(lay)
            if not targets:
                return
            lay = targets[0]
            warmed = 0
            _tw0 = time.perf_counter()
            try:
                if not getattr(lay, "auto_mode", False):
                    # 手动图层也提前预热**下一个**（manual_switch_layer 走 cur+1），
                    # 这样点按钮/快捷键时首帧已经在手。
                    _n = len(lay.clips)
                    if _n >= 2:
                        _ci = (int(getattr(lay, "cur", -1)) + 1) % _n
                        _clip = lay.clips[_ci]
                        _p = self._get_player(lay, _clip)
                        if hasattr(_p, "warm_up"):
                            _p.warm_up()
                        lay._presec_path = getattr(_clip, "path", None)
                        lay.pre_idx = _ci
                        warmed = 1
                else:
                    # 预热必须和 `_refresh_auto_layers` 用**同一套排除集**
                    # （历史 + 当前池），否则预热的是 A、边界选中的是 B。
                    _hist = [str(p) for p in
                             (getattr(lay, "_pool_history", None) or []) if p]
                    _old = [str(getattr(c, "path", "") or "")
                            for c in (lay.clips or [])]
                    _ex = {p for p in (_hist + _old) if p}
                    picked = list(match_clips(energy, self.library, role=lay.role,
                                              n=4, tier=tier,
                                              exclude_paths=_ex))
                    if picked:
                        cur_path = (lay.clips[lay.cur].path
                                    if 0 <= lay.cur < len(lay.clips) else None)
                        cands = [c for c in picked
                                 if getattr(c, "path", None) != cur_path]
                        clip = rng.choice(cands or picked)
                        p = self._get_player(lay, clip)
                        if hasattr(p, "warm_up"):
                            p.warm_up()
                        lay._presec_path = getattr(clip, "path", None)
                        warmed = 1
            except Exception:                              # noqa: BLE001
                lay._presec_path = None
            finally:
                done.add(id(lay))
            _wms = (time.perf_counter() - _tw0) * 1000.0
            if _wms >= 25.0:
                try:
                    import stallwatch as _sw
                    _sw.log_line("[WARM] %.0fms %s"
                                 % (_wms, str(getattr(lay, "name", "?"))[:30]))
                except Exception:                          # noqa: BLE001
                    pass
            if warmed:
                self._presec_warmed = int(getattr(self, "_presec_warmed", 0)) + 1
            remaining = [x for x in self.layers if id(x) not in done
                         and getattr(x, "fixed", -1) < 0
                         and getattr(x, "clips", None)
                         and not getattr(x, "is_kv", False)
                         and not getattr(x, "is_cuesheet", False)]
            if not remaining:
                self._section_log("预热点 下一段%d/%s 提前%.1fs 预热%d层"
                                  % (i + 2, tier, lead,
                                     int(getattr(self, "_presec_warmed", 0))))
        except Exception:                                      # noqa: BLE001
            pass

    def _apply_section_switch(self, snap, cur):
        """段落切换的**立即执行**：换池 + 换素材 + 过渡时长，全都不等拍位排期。

        与 `_energy_tier_switch` 的差别就是这个函数存在的全部理由：
          · 不做「新档位稳定 2.0 秒」的防抖 —— 那是给**实测能量抖动**用的，
            段落边界是预置的确定事件，没有抖动可防（175 BPM 下 2 秒 = 5.8 拍，
            正是用户说的"等 5~8 拍"）；
          · 不等 `_next_switch_due` 的 8/16 拍排期；
          · 过渡时长取段落里写的 `trans_ms`；`0/None` = 跟随全局/模式过渡（不再硬切）。

        不动的图层（与 `_auto_switch_tick` 同口径，别自作主张放宽）：
        固定(`fixed>=0`)、空池、全局锁定、Kv 待机层、码表容器层。
        """
        if not getattr(self, "running", False):
            return                        # 没点「开始」：只记状态，不动画面
        self._phase = "section_switch"
        tier = cur.get("tier") or "mid"
        tms = cur.get("trans_ms", None)
        try:
            _tms_pos = (tms is not None and int(tms) > 0)
        except (TypeError, ValueError):
            _tms_pos = False
        # 段落表里的 trans_ms=0 表示"跟随全局/模式过渡"，不是硬切。
        # 旧实现把 0 写进 lay.trans_ms ⇒ 用户选了淡入淡出也会被强行硬切。
        for _lay in self.layers:
            if getattr(_lay, "is_kv", False) or getattr(_lay, "is_cuesheet", False):
                continue
            _lay.trans_ms = int(tms) if _tms_pos else None
        # ① 重 roll 素材池（★ 2026-10-30 起 mid 也按档位过滤 —— 用户要求中动态也强制换池）
        #    ★ 2026-11-08：自动图层也要走用户选的过渡，不能在这里 instant 硬切。
        _t_ref = time.perf_counter()
        try:
            self._refresh_auto_layers(snap, force_tier=tier, instant=False, presec=True)
        except Exception:                                     # noqa: BLE001
            pass
        _ref_ms = (time.perf_counter() - _t_ref) * 1000.0
        # ② 把档位状态机直接推到目标档，避免下一帧 _energy_tier_switch 又按实测能量改回去
        self._last_energy_tier = tier
        self._tier_since = None
        # ③ 重置切换排期 —— 「不等几拍」的关键一步
        try:
            self.switch_pos = self._beat_pos(snap)
        except Exception:                                     # noqa: BLE001
            pass
        self._next_due = None
        self._due_cap = False        # 新段落 ⇒ 重新算（见 `_next_switch_due`）
        # 段落已经统一切过：按屏 override 的排期也一起清空，避免同一帧再切第二刀
        try:
            self._scr_switch.clear()
        except Exception:                                     # noqa: BLE001
            pass
        # ④ 段落只切**自动图层**；手动图层（auto_mode=False）绝不在段落边界切。
        #   `_refresh_auto_layers(force_tier=…)` 上面已经按新档位换池并切过一次；
        #   这里不再遍历普通图层。用户 2026-11-26：「手动图层只有我手动点下一素材
        #   或快捷键才会切换」。
        _n_sw = 0
        _tsw_ms = 0.0
        self._sec_switch_n = getattr(self, "_sec_switch_n", 0) + 1
        self._sec_last_switch_t = time.perf_counter()
        _sec = self._sec_info.get("sec", -1.0)
        _trk = getattr(self, "_cue_pos_tracker", None)
        self._section_log(
            "段落切换 #%d → %s/%s(E=%.2f) 原曲%.2fs 第%d/%d段 "
            "换池=%s 切层=%d 排期已重置 | sid=%s reset=%d why=%s"
            " | 耗时 refresh=%.1fms switch=%.1fms"
            % (self._sec_switch_n, (cur.get("name") or "-"), tier,
               float(cur.get("energy") or 0.0), float(_sec),
               self._sec_idx + 1, len(self._sec_list or [None]),
               tier, _n_sw,
               getattr(_trk, "song_id", "?"), getattr(self, "_sec_reset_n", 0),
               getattr(self, "_sec_why", "?"), _ref_ms, _tsw_ms))

    def _energy_tier_switch(self, snap):
        """能量跨档即时切换：能量从高(>0.6)跌到低(<0.4)或反向时，立即换素材动态档。

        之前只靠拍位切换点(_next_switch_due 对齐 4 拍小节线)换素材，切换间隔 8~32 拍，
        能量变了要等十几秒才换到对应动态档。这里在能量跨档、新档位稳定 1.5s 后（防抖），
        立即对所有非固定图层切一次，让「高潮→高动态 / 缓和→低动态」跟上能量。"""
        # ★ 2026-10-28：**段落表接管期间整段跳过**。
        #   这条路径的 2 秒稳定期 + 滞回是为「实测能量抖动」设计的；
        #   段落边界是预置的确定事件，由 `_section_tick` 直接执行（不等拍位、不等 2 秒）。
        #   两条同时跑会互相打架：本函数会把段落刚设好的档位按实测能量改回去。
        if getattr(self, "_sec_energy", None) is not None:
            return
        E = snap_energy(snap)   # 0.0 必须当成 0.0（旧写法 `or 0.5` 会让档位卡在中档）
        # 这里**刻意不做静音门控**：档位必须照常评估，否则「能量算成 0」时档位永远停在
        # 上一档，低动态池一辈子 roll 不出来（用户反馈：能量为 0 时不 roll 低动态池）。
        # 本方法只在新档位稳定 2 秒后触发一次（滞回 + 稳定期），所以最多换一次池，不会 churn。
        # 滞回式三段判定（防阈值附近频繁横跳 → 素材池切换"太灵敏"）：
        # ★★ 2026-10-30 用户：「除了高动态低动态 再把中动态也加进来 0.4到0.8」「高动态提到 0.8」
        #   ⇒ 三段边界改成 0.40 / 0.80（和 `sections.TIER_LO/TIER_HI`、`match_clips` 同一套）。
        # ★★ 2026-10-30 晚（`Rolling Contact`）：「中能量改回 0.4 0.75」⇒ 高门槛 0.80 → 0.75。
        # ★★★ 2026-11-06：**改成直接读 `sections` 的常量**（0.72 / 0.35，见 `sections.TIER_HI`
        #   的注释）。之前这里硬编码 0.75/0.40，而 `sections` 那边已经调到 0.72/0.35
        #   ⇒ 库里标成 high 的段（e=0.73）在引擎里被判成 mid，档位和显示对不上。
        #   以后只改 `sections.TIER_*` 一处，这里自动跟随。
        #   滞回带仍是 0.06：进 high 要 >=0.72，退回 mid 要 <0.66；进 low 要 <0.35，退回 mid 要 >0.41。
        _TH, _TL, _HYS = sections.TIER_HI, sections.TIER_LO, 0.06
        cur = self._last_energy_tier
        if cur == "high":
            tier = "mid" if E < _TH - _HYS else "high"
        elif cur == "low":
            tier = "mid" if E > _TL + _HYS else "low"
        else:  # mid
            tier = "high" if E >= _TH else ("low" if E < _TL else "mid")
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
        # ★ 2026-10-30 用户：「中动态 0.4到0.8 稳定了就强制切素材池，和之前一样逻辑」
        #   ⇒ mid 也重 roll（以前只有 low/high 才 roll，中能量段一直在旧池子里挑）。
        self._refresh_auto_layers(snap, force_tier=tier, instant=False)
        # ★ 2026-11-26（用户）：手动图层只手动切 —— 能量跨档也不再动它们。
        #   自动图层已在上面 `_refresh_auto_layers` 里按新档换池并切换。

    # ---------------- 主循环 ----------------
    _err_log_n = 0
    _err_log_t = 0.0        # 上次写 engine_error.log 的时刻（异常日志限流，见 `_tick`）

    def _silent_hide_tick(self, snap):
        """推进「无声音时隐藏本层」的 0.5s 防抖，写入 `self._silent_now_debounced`。

        原始判据保持不变：`silent_sec > 0.25 or level < 0.03`（即"当前这一帧有没有声音"）。
        防抖规则：静音要**连续**累计 ≥ `SILENT_HIDE_DEBOUNCE`(0.5s) 才置为静音；
        恢复有声同样要**连续**累计 ≥ 0.5s 才解除 —— 这样音乐里的瞬时间隙（鼓点间隙、
        搓碟、EQ 抽低频导致某几个 block 的 rms 回到有声）不会让图层闪烁。

        初始值选择：`_silent_debounce_on` 起步为 False，静音状态**同样**要连续累计 ≥ 0.5s
        才置为静音。理由：需求明确要求"静音要**连续**达到阈值才隐藏"——隐藏是破坏性动作
        （画面上该图层整层消失），必须严格连续；而"不隐藏"（显示）是安全态，起步为 False
        不会有副作用。若改成"首个静音帧立即隐藏"，一个单帧噪声毛刺就会隐藏图层 0.5s、
        恢复又要 0.5s ⇒ 反而制造闪烁，违背本需求的初衷。

        ⚠ 冷启动（曲库/音频还没出声、`level≈0`）：引擎启动后会有 0.5s 让 hide_silent 图层
        参与显示，之后才隐藏 —— 这是可接受的（真实演出里引擎在音乐开始前就已经跑起来，
        0.5s 后自然进入静音态）；反过来**不会**造成"该显示的被隐藏"。

        ⚠ 冷却保护：暂停/卡帧后 dt 会巨大（冻结早退期间本函数不推进时间），若不钳位防抖
        会被一个 dt 直接跨过去。这里照抄 `_kv_tick` 的 `dt = max(0.0, min(0.5, dt))`。
        O(1) 纯浮点比较，无锁、无 I/O、无内存分配 —— 满足主线程/tick 热路径约束。
        """
        now = time.perf_counter()
        dt = now - self._silent_last_t
        dt = max(0.0, min(0.5, dt))     # 保护：卡帧/暂停后不累加巨大 dt（照抄 _kv_tick）
        self._silent_last_t = now

        raw_silent = (snap["silent_sec"] > _SILENT_SEC_RAW
                      or snap["level"] < _SILENT_LEVEL_RAW)
        if raw_silent:
            self._silent_since += dt
            self._loud_since = 0.0
            if self._silent_since >= SILENT_HIDE_DEBOUNCE:
                self._silent_debounce_on = True
        else:
            self._loud_since += dt
            self._silent_since = 0.0
            if self._loud_since >= SILENT_HIDE_DEBOUNCE:
                self._silent_debounce_on = False
        self._silent_now_debounced = self._silent_debounce_on

    def _tick(self):
        # 主循环已移入独立线程；与 GUI 线程的入口调用（rebuild/切换等）用锁串行化
        t_start = time.perf_counter()
        gap = (t_start - self._last_tick_t) if self._last_tick_t else 0.0
        self._last_tick_t = t_start
        if gap > 0:
            self._tick_gap_ms = gap * 1000.0
        try:
            with self._tick_lock:
                self._tick_locked()
        except Exception:
            # worker 线程异常在 windowed exe 下不可见：写日志文件便于排查黑屏类问题。
            # ★ 限流而非截断（原 `<= 5` 会在第 6 次后**永久静默**，而持久异常会让
            #   `_tick_locked` 中途退出 ⇒ 预览/NDI/Spout 静默冻结且再无线索）：
            #   首次必记，之后每 `_ERR_LOG_MIN_INTERVAL` 秒最多记一次，并带上累计次数。
            import traceback
            from config import app_base_dir
            AutoVJEngine._err_log_n += 1
            _now = time.time()
            if (AutoVJEngine._err_log_n == 1
                    or (_now - AutoVJEngine._err_log_t) >= _ERR_LOG_MIN_INTERVAL):
                AutoVJEngine._err_log_t = _now
                try:
                    with open(os.path.join(app_base_dir(), "engine_error.log"),
                              "a", encoding="utf-8") as f:
                        f.write("[%s] (累计第 %d 次)\n%s\n"
                                % (time.strftime("%H:%M:%S"),
                                   AutoVJEngine._err_log_n, traceback.format_exc()))
                except Exception:
                    pass
        self._tick_ms = (time.perf_counter() - t_start) * 1000.0
        # ★ 诊断：每秒统计一次"引擎 tick / Spout 发送 / GUI 发布 / 缩略图"的真实速率。
        #   用来区分"引擎本身只有 22fps"还是"引擎 30fps、GUI 只画了 22fps"。
        if os.environ.get("AUTOVJ_RATE_PROBE") == "1":
            try:
                self._rate_ticks = int(getattr(self, "_rate_ticks", 0)) + 1
                _now = time.perf_counter()
                _t0 = float(getattr(self, "_rate_t0", 0.0) or 0.0)
                # ★ 2026-11-25 真机定位：累计本 tick 各阶段耗时，每秒写一行
                #   [SEG] 平均值（平时不开启，零成本）。
                _acc = getattr(self, "_seg_acc", None)
                if _acc is None:
                    _acc = {}
                    self._seg_acc = _acc
                _acc["_tick"] = _acc.get("_tick", 0.0) + float(self._tick_ms)
                _acc["_gap"] = _acc.get("_gap", 0.0) + float(self._tick_gap_ms)
                try:
                    _tr = getattr(self, "_ptrace", None) or []
                    for (_n0, _t0s), (_n1, _t1s) in zip(_tr, _tr[1:]):
                        _acc[_n0] = _acc.get(_n0, 0.0) + (_t1s - _t0s) * 1000.0
                    if _tr:
                        _acc[_tr[-1][0]] = (_acc.get(_tr[-1][0], 0.0)
                                            + (time.perf_counter() - _tr[-1][1]) * 1000.0)
                except Exception:                          # noqa: BLE001
                    pass
                if not _t0:
                    self._rate_t0 = _now
                elif _now - _t0 >= 1.0:
                    import stallwatch
                    stallwatch.log_line(
                        "[RATE] ticks=%d spout=%d gui=%d thumbs=%d"
                        % (self._rate_ticks, int(getattr(self, "_rate_spout", 0)),
                           int(getattr(self, "_rate_gui", 0)),
                           int(getattr(self, "_rate_thumbs", 0))))
                    try:
                        _n = max(1, self._rate_ticks)
                        _parts = ["tick=%.1f gap=%.1f"
                                  % (_acc.get("_tick", 0.0) / _n,
                                     _acc.get("_gap", 0.0) / _n)]
                        for _k in ("section", "compose", "output_plan", "postfx",
                                   "copy", "copy_indep", "copy_comb", "copy_pub",
                                   "pub_frames", "pub_disp", "pub_gui",
                                   "routes", "spout", "ndi", "ui_snap"):
                            if _k in _acc:
                                _parts.append("%s=%.1f" % (_k, _acc[_k] / _n))
                        try:
                            import colorfx as _cfx_m
                            import postprocess as _pp_m
                            _cs = getattr(_cfx_m, "SEG", {}) or {}
                            _ps = getattr(_pp_m, "SEG", {}) or {}
                            _parts.append(
                                "c_view=%.1f c_tick=%.1f c_lut=%.1f"
                                " c_apply=%.1f c_post=%.1f c_write=%.1f"
                                " c_color=%.1f c_wb=%.1f c_in=%d c_fb=%d"
                                % (_ps.get("view", 0.0), _cs.get("tick", 0.0),
                                   _cs.get("lut", 0.0), _cs.get("apply", 0.0),
                                   _POST_ACC.get("post", 0.0), _ps.get("write", 0.0),
                                   _POST_ACC.get("color", 0.0),
                                   _POST_ACC.get("write", 0.0),
                                   int(_POST_ACC.get("inplace", 0)),
                                   int(_POST_ACC.get("fallback", 0))))
                        except Exception:                  # noqa: BLE001
                            pass
                        stallwatch.log_line("[SEG] " + " ".join(_parts))
                    except Exception:                      # noqa: BLE001
                        pass
                    self._rate_ticks = 0
                    self._rate_spout = 0
                    self._rate_gui = 0
                    self._rate_thumbs = 0
                    self._rate_t0 = _now
                    self._seg_acc = {}
                    _POST_ACC["inplace"] = 0
                    _POST_ACC["fallback"] = 0
            except Exception:                                  # noqa: BLE001
                pass
        # 引擎线程的 tick 间隔/处理耗时取证：输出侧 `[OUT]` 日志会带上这两个值。
        # 只有明显异常才写（30fps 下 150ms ≈ 4~5 帧），每 2 秒最多一行，绝不影响演出。
        try:
            if bool(getattr(self, "running", False)) and (
                    self._tick_gap_ms > 100.0 or self._tick_ms > 100.0):
                now = time.perf_counter()
                if now - self._tick_log_t >= 2.0:
                    self._tick_log_t = now
                    import stallwatch
                    _extra = ""
                    try:
                        # 分段耗时（只在本分支组装；正常帧零成本）
                        _tr = getattr(self, "_ptrace", None) or []
                        if len(_tr) >= 2:
                            _segs = {}
                            for (_n0, _t0), (_n1, _t1) in zip(_tr, _tr[1:]):
                                _segs[_n0] = _segs.get(_n0, 0.0) + (_t1 - _t0) * 1000.0
                            _last = (time.perf_counter() - _tr[-1][1]) * 1000.0
                            _segs[_tr[-1][0]] = _segs.get(_tr[-1][0], 0.0) + _last
                            _abbr = {"section": "sec", "compose": "comp",
                                     "output_plan": "plan", "postfx": "post",
                                     "copy": "copy", "routes": "routes",
                                     "spout": "spout", "ndi": "ndi",
                                     "ui_snap": "ui", "tick": "tick"}
                            _order = ("section", "compose", "output_plan", "postfx",
                                      "copy", "routes", "spout", "ndi", "ui_snap")
                            _parts = ["%s=%.0f" % (_abbr.get(_k, _k), _segs[_k])
                                      for _k in _order if _k in _segs]
                            # copy 段细分（单路：indep=独立屏合成 comb=整合 pub=发布）
                            for _k, _ab in (("copy_indep", "ci"), ("copy_comb", "cc"),
                                            ("copy_pub", "cp")):
                                if _k in _segs:
                                    _parts.append("%s=%.0f" % (_ab, _segs[_k]))
                            if _parts:
                                _extra += " | seg: " + " ".join(_parts)
                        # GC：自上次慢帧以来
                        _g2n, _g2ms, _g1n, _g1ms = _gc_take()
                        _extra += " | gc: g2=%d/%.0fms g1=%d/%.0fms" % (
                            _g2n, _g2ms, _g1n, _g1ms)
                        # 颜色/后处理链细分（view/tick/lut/apply/post/write + mode/s）
                        try:
                            import colorfx as _cfx_mod
                            import postprocess as _pp_mod
                            _cs = getattr(_cfx_mod, "SEG", {}) or {}
                            _ps = getattr(_pp_mod, "SEG", {}) or {}
                            _extra += (" | post: view=%.0f tick=%.0f lut=%.0f apply=%.0f"
                                       " post=%.0f write=%.0f"
                                       % (_ps.get("view", 0.0), _cs.get("tick", 0.0),
                                          _cs.get("lut", 0.0), _cs.get("apply", 0.0),
                                          _POST_ACC.get("post", 0.0), _ps.get("write", 0.0)))
                            _extra += (" | c: mode=%s s=%.2f lut_rebuilt=%d bits_copy=%d"
                                       % (_POST_ACC.get("mode") or "?",
                                          float(_POST_ACC.get("s", 0.0)),
                                          1 if _cs.get("lut_n", 0) else 0,
                                          int(_ps.get("bits_copy", 0))))
                            # routes 逐屏（top3 按总耗时），并清空
                            try:
                                _rt = dict(_ROUTE_SEG)
                                _ROUTE_SEG.clear()
                                if _rt:
                                    _tops = sorted(
                                        _rt.items(),
                                        key=lambda kv: -(kv[1]["bits"] + kv[1]["spout"]
                                                         + kv[1]["ndi"]))[:3]
                                    _extra += " | rt: " + " ; ".join(
                                        "%s sp=%.0f nd=%.0f bits=%.0f n=%d dr=%d"
                                        % (_n, _v["spout"], _v["ndi"], _v["bits"], _v["n"],
                                           int(_v.get("dr", 0)))
                                        for _n, _v in _tops)
                            except Exception:                      # noqa: BLE001
                                pass
                            # 清零（下次慢帧重新计）
                            for _k in list(_cs.keys()):
                                _cs[_k] = 0
                            for _k in list(_ps.keys()):
                                _ps[_k] = 0
                            _POST_ACC["post"] = 0.0
                        except Exception:                          # noqa: BLE001
                            pass
                    except Exception:                              # noqa: BLE001
                        pass
                    stallwatch.log_line(
                        "[ENG] tick 间隔 %.0fms / 处理 %.0fms 阶段=%s 图层=%d fps=%s%s"
                        % (self._tick_gap_ms, self._tick_ms,
                           getattr(self, "_phase", "?"), len(self.layers),
                           getattr(self, "_effective_fps", "?"), _extra))
        except Exception:
            pass

    def _phase_set(self, name):
        """记一次阶段切换（同时打点），供慢帧日志拼 `seg:` 分段耗时。

        ★ 2026-11-10：只做 `_phase` 赋值 + 一次 `perf_counter()`，正常帧成本 ~1µs；
          分段字符串**只在 `_tick_ms > 100ms` 时**才组装（见 `_tick_locked` 尾部）。
        """
        self._phase = name
        try:
            tr = getattr(self, "_ptrace", None)
            if tr is None:
                tr = []
                self._ptrace = tr
            tr.append((name, time.perf_counter()))
        except Exception:                                      # noqa: BLE001
            pass

    def _seg_reset(self):
        """每帧开始清零所有慢帧取证累加器 ⇒ 慢帧读到的一定是**本帧**值。"""
        try:
            import colorfx as _cm
            import postprocess as _pm
            for _d in (getattr(_cm, "SEG", None), getattr(_pm, "SEG", None)):
                if isinstance(_d, dict):
                    for _k in list(_d.keys()):
                        _d[_k] = 0
            for _k in list(_POST_ACC.keys()):
                _POST_ACC[_k] = 0 if _k != "mode" else ""
            _ROUTE_SEG.clear()
        except Exception:                                      # noqa: BLE001
            pass

    def _output_layer_ids(self):
        """当前真正参与输出的图层 id 集合。

        单屏：只算主屏那份配置（其他屏的图层不合成、不解码）；
        多屏：所有主屏/独立屏的并集（复制/切分跟随源屏）。
        """
        all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        try:
            plan = getattr(self, "_out_cfg", None)
            if plan:
                ids = set()
                for s in (plan.get("screens") or []):
                    if not isinstance(s, dict):
                        continue
                    if s.get("type") not in ("main", "independent"):
                        continue
                    ids.update(str(x) for x in output_model.effective_layers(
                        plan, s, all_ids))
                # ★ 2026-11-25：被完全遮住且无输出路由的屏，暂停其图层解码。
                hidden = set(getattr(self, "_hidden_sids", None) or ())
                if hidden:
                    for s in (plan.get("screens") or []):
                        if not isinstance(s, dict):
                            continue
                        if str(s.get("id") or "") in hidden:
                            ids.difference_update(
                                str(x) for x in output_model.effective_layers(
                                    plan, s, all_ids))
                return ids
            active = output_model.active_config(self.cfg)
            if active is not None:
                return set(output_model.main_output_layers(active, all_ids))
        except Exception:                                      # noqa: BLE001
            pass
        return set(all_ids)

    def _tick_locked(self):
        self._ptrace = []
        self._seg_reset()
        # ★ 2026-11-25：本 tick 的「输出快照」令牌 —— NDI/Spout 共用同一张
        #   QImage 快照（见 `_get_output_snapshot`），每 tick 失效一次。
        self._out_snap_token = object()
        self._out_snap_cache = None
        try:
            self._hidden_sids = self._hidden_screen_ids()
        except Exception:                                      # noqa: BLE001
            self._hidden_sids = set()
        # ★ 2026-11-25：输出全屏覆盖 GUI 时，引擎是唯一关键路径；把渲染线程从
        #   NORMAL 提到 ABOVE_NORMAL，减少被解码/音频线程抢占造成的 5~10ms 抖动。
        try:
            _want_prio = (1 if (bool(getattr(self, "_gui_covered", False))
                                and str(os.environ.get("AUTOVJ_ENGINE_PRIO", "1")) != "0")
                          else 0)
            if _want_prio != int(getattr(self, "_thread_prio", 0)):
                import ctypes as _ctp
                _ctp.windll.kernel32.SetThreadPriority(
                    _ctp.windll.kernel32.GetCurrentThread(), int(_want_prio))
                self._thread_prio = int(_want_prio)
        except Exception:                                      # noqa: BLE001
            pass
        self._phase_set("tick")
        snap = self.audio.snapshot()
        # ★★★ 2026-11-12（用户：没声音待机仍有 25~35% CPU）：
        #   运行中但长时间无音频时，播放器仍在按源帧率解码、5 屏整合/Spout/GUI
        #   仍按 30fps 跑。这里进入"静音待机"：解码/合成/GUI 全部降到
        #   `perf.silent_standby_fps`（默认 2fps），并且不激活任何播放器。
        #   音频一回来下一 tick 就恢复，画面停在上一次合成帧。
        try:
            _sil = float(snap.get("silent_sec", 0.0) or 0.0)
            _lvl = float(snap.get("level", 0.0) or 0.0)
        except (TypeError, ValueError):
            _sil, _lvl = 0.0, 1.0
        try:
            _perf = self.cfg["perf"] or {}
            _ss_sec = float(_perf.get("silent_standby_sec", 8.0) or 8.0)
            _ss_lvl = float(_perf.get("silent_standby_level", 0.002) or 0.002)
        except (TypeError, ValueError, KeyError):
            _ss_sec, _ss_lvl = 8.0, 0.002
        # `silent_sec` 在采集线程停住/零块时可能不增长（实测卡在 4.3s），
        # 所以引擎侧再用挂钟累计“连续低电平”时长，作为第二判据。
        _now_s = time.perf_counter()
        if _lvl <= max(0.0, _ss_lvl):
            if self._silent_low_since <= 0.0:
                self._silent_low_since = _now_s
        else:
            self._silent_low_since = 0.0
        _low_dur = (_now_s - self._silent_low_since) if self._silent_low_since > 0.0 else 0.0
        self._silent_standby = bool(
            self.running and not self.freeze and not self.blackout
            and not str(snap.get("error", "") or "")
            and max(_sil, _low_dur) >= max(2.0, _ss_sec))
        # ★★ 2026-11-17（用户：Kv 图层淡入时画面掉帧）：
        #   Kv 的淡入/淡出只有 1.5s，而静音待机把引擎降到 2fps ⇒ 整段淡入只有
        #   3 帧，看起来就是"一卡一卡"。这里在 Kv **正在或即将**过渡时禁止进待机
        #   （只在过渡那一两秒保持正常帧率，静音待机的省 CPU 目的不受影响）。
        try:
            self._kv_anim = bool(self._kv_transition_pending())
        except Exception:                                      # noqa: BLE001
            self._kv_anim = False
        if self._kv_anim:
            self._silent_standby = False
        self._auto_fps_tick()
        if self.freeze:
            return  # 冻结：画面完全不变
        # ★ 「无声音时隐藏本层」0.5s 防抖：**必须在本帧任何读取 silent 的代码之前**算一次，
        #   写入 `self._silent_now_debounced`；`hide_silent`（合成 `_compose`）与
        #   `pause_silent`（下方活跃标记）两处都读它，保证同帧判据一致。
        #   冻结时早退（上面）⇒ 不推进防抖计时；解冻后 dt 被 `_silent_hide_tick` 钳到 0.5，
        #   状态不会被一个巨大 dt 瞬间跨过去（与 `_kv_tick` 同策略）。
        self._silent_hide_tick(snap)
        # 注意：输出/预览始终实时渲染（不受开始-停止影响）；停止只停止"自动切换"

        beat_event = snap["beat_count"] != self._last_beat_count
        self._last_bar_count = snap["bar_count"]
        self._last_beat_count = snap["beat_count"]

        # 长静音（>3s，跟音频侧的"换歌"判据一致）后手动重定义的小节头失效：
        # 换歌了，用户手动对齐的相位不再代表新歌的小节线，交回给音频识别的 downbeat。
        if self._bar_off_user is not None and snap.get("silent_sec", 0.0) > 3.0:
            self._bar_off_user = None
            self._next_due = None

        # 静音待机：没有音频时不推进自动换素材/模式/Kv，避免不断打开新播放器
        # （即使它们随后被挂起，开容器+解首帧也会造成 CPU 尖峰）。
        _standby = bool(getattr(self, "_silent_standby", False))
        # 行为模式：每秒评估一次（自动打分或手动强制）
        if self.running and not _standby:
            self._update_mode(snap)

        # 自动匹配：曲风变化时重算自动图层素材池
        if not _standby:
            self._refresh_auto_layers(snap)

        # ★ 2026-10-25「手动切换素材」：**一次性关掉本帧所有自动切换入口**。
        #   为什么用同一个布尔而不是各分支各写一遍：切换入口一共 3 个
        #   （能量跨档 / 按拍位 / 逐拍交替），漏掉任何一个都会变成
        #   "选了手动却还在偷偷换素材"。`pause_auto`（热键 P）语义相同，天然合并。
        #   ⚠ 这里**不能**顺带 gating 下面这些：Kv 待机层（_kv_tick，静音进出）、
        #     码表（_cuesheet_tick，跟拍驱动）、自愈看门狗 —— 它们与"自动换素材"无关。
        _no_auto_switch = self.pause_auto or self.manual_switch_mode()

        # 能量跨档即时切换：能量从高/低跨过阈值（带防抖）时立即换素材动态档，
        # 不等到下一个拍位切换点（否则能量变了十几秒素材动态还换不过来）。
        if self.running and not _no_auto_switch and not _standby and self.mode != "beat":
            self._energy_tier_switch(snap)

        # BPM 速度同步
        # ★★★ 2026-11-02（用户直播：「200+ BPM 的歌，素材速度没跟上来、很慢」）：
        #   **认到歌时优先用库内 BPM**，而不是拍钟那个实时估计值。
        #   为什么：库内 BPM 是离线整曲细扫（`audio_engine` 自己的注释写着
        #   "比实时自相关准一个量级"），而实时值可能**卡在上一首**——
        #   现场取证见 `_bpm_relock_after_veto`：14 分钟里 `bpm=133.5` 一条没变，
        #   200BPM 那首的素材因此跑 `133.5/128 = 1.04×` 而不是 `1.56×`（慢 1.5 倍），
        #   切换间隔也被拖成 10.8s（应 7.2s）。用户看到的"素材速度没跟上来"就是这个。
        #   ⚠ 只在 `lib_bpm > 0`（= 认到歌且库里有 BPM）时换基准；
        #     没认到歌时逐值同改前（仍走 `snap["bpm"]`）。
        speed = content_speed(snap, bool(self.cfg["auto"].get("bpm_speed_sync")))

        # 非逐拍模式的按拍位切换
        if self.running and not _no_auto_switch and not _standby and self.mode != "beat":
            self._auto_switch_tick(snap)

        # 逐拍交替：独立开关（只接管前景层；只在 drop 段生效）
        if self.running and not _no_auto_switch and not _standby:
            self._beat_tick(snap)

        # 预热：把「下一个要切的素材」提前打开容器+解首帧（切到时零延迟，见 warm_up）
        if not getattr(self, "_silent_standby", False):
            self._process_warms(snap)

        # Kv 待机层：静音切入 / 有声切出（独立于所有其它设置，不受 pause_auto 影响）
        # ★ 2026-11-12：**静音待机时也必须继续 tick** —— 它负责“没音乐时把 Kv 图
        #   淡入”。之前跟着 `_standby` 一起跳过，结果 Kv 在音乐停止时永远不显示。
        if self.running and not self.locked:
            self._kv_tick(snap)

        # 视频速度 + 活跃标记（缓存中未显示的解码器挂起省 CPU）
        # ★ 静音判据统一读防抖结果（`_tick_locked` 开头 `_silent_hide_tick` 已算好）：
        #   与 `_compose` 里的 `hide_silent` 同帧同值，避免 hide 和 pause 的时机不一致。
        silent_now = self._silent_now_debounced
        out_ids = self._output_layer_ids()
        for lay in self.layers:
            # ★★ 活跃标记必须按**身份**遍历 `cache ∪ {lay.player, lay.prev_player}`，
            #   不能只遍历 `lay.cache`：`lay.player` 并不保证在缓存里（自动匹配换池、
            #   `_get_player` 的 LRU 驱逐、预热/切换路径都可能让它脱离缓存）。
            #   只遍历缓存 ⇒ 这样的 `lay.player` **永远不被置为活跃 ⇒ 永不解码 ⇒ 画面
            #   冻在最后一帧**；而看门狗又跳过非活跃播放器 ⇒ **零日志的"静默冻帧"**。
            #   这是用户实测"播放中仍卡在某一帧、且 ui_stall.log 一条都没有"的根因。
            # ★★ 不可见图层**不解码**：旧写法 `active` 只看"是不是当前播放器"，与
            #   `visible` 无关 ⇒ 用户关掉的图层仍满速解码。现场 `layer_health.log`
            #   可见：`L7 图层 5 vis=0 … frames=528` 一路涨到 1302。
            #   代价是实打实的帧预算：软解一路 1080p DXV ≈ 12ms/帧、4K ≈ 24ms/帧
            #   （tools/_probe_4k.py 实测），多路并发时 30fps 的 33ms 预算直接爆掉，
            #   而且每路都在从素材盘取数据（Y: 是机械盘，见 tools/_probe_disk.py）。
            #   过渡中的图层仍要解码（淡入需要新画面），所以 `trans_active` 也算可见。
            # ????????? trans_active?_compose_to ?????
            # ????????????????? active ???CPU ????
            if not bool(getattr(lay, "visible", True)):
                try:
                    lay.trans_active = False
                except Exception:
                    pass
            vis_now = (bool(getattr(lay, "visible", True))
                       or bool(getattr(lay, "trans_active", False)))
            # 不在这份输出配置里的层（单屏下属于其他屏幕）既不合成也不解码。
            if str(getattr(lay, "lid", "")) not in out_ids:
                vis_now = False
            # ★★★ 2026-11-12（用户：只显示一张图，CPU 仍 30%+）：
            #   `hide_silent` 的图层在静音时**合成端已经不再画**，但活跃标记只看
            #   `visible` ⇒ 解码器继续满速跑。现场 config 里 L5/L6/L9 都是
            #   `hide_silent=True`，静音时画面只剩 L0 一张图，CPU 却仍是 3 路视频。
            #   合成端不画 ⇒ 解码端也必须挂起。
            try:
                _op_now = float(getattr(lay, "opacity", 1.0) or 0.0)
            except (TypeError, ValueError):
                _op_now = 1.0
            if _op_now <= 0.002:
                vis_now = False
            if bool(getattr(lay, "hide_silent", False)) and silent_now:
                vis_now = False
            seen = set()
            for p in list(lay.cache.values()) + [lay.player, lay.prev_player]:
                if p is None or id(p) in seen:
                    continue
                seen.add(id(p))
                active = ((p is lay.player or p is lay.prev_player) and vis_now
                          and not getattr(self, "blackout", False)
                          and (not getattr(self, "_silent_standby", False)
                               or (getattr(lay, "is_kv", False)
                                   and float(getattr(lay, "kv_on", 0.0) or 0.0) > 0.01)))
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

        # ★ 卡帧看门狗：活跃播放器超过 `_STALL_SECS` 没有新帧 ⇒ 关掉、从缓存摘除、重建。
        #   放在活跃循环**之外**（避免遍历 cache 时改 dict）。
        #   这是"素材解不出来也不会永久冻帧"的兜底，覆盖 R1/R2/R4/R6/R9。
        # ★★ 2026-11-17（用户：静音待机时 39% CPU、电脑卡死）：
        #   待机时上面的活跃循环**故意**把所有解码器挂起（`set_active(False)`），
        #   而看门狗把"不活跃 / 没有新帧"当成故障 ⇒ 每 2~5s 强行激活/重建解码器、
        #   甚至跳到下一个素材、反复读盘（现场 Task Manager：39% CPU / 23MB/s）。
        #   待机期间**直接跳过自愈**，并清掉计时器；音频回来后的第一帧再正常检查
        #   （那时播放器已经重新激活，真有故障仍会被 `_heal_stalled` 抓到）。
        if _standby:
            for lay in self.layers:
                lay._silent_inactive_since = 0.0
                lay._silent_noframe_since = 0.0
        else:
            for lay in self.layers:
                self._heal_stalled(lay)
                # ★ 静默冻帧兜底：`_heal_stalled` 看不见的那几类（player 不在缓存 ⇒ 永不被激活 /
                #   player is None / 有帧前就 ended）在这里恢复并留下日志，避免"画面冻住而日志沉默"。
                #   传 `snap` 供"无帧封顶后跳到下一个素材"复用 `_candidate_indices`。
                self._heal_silent(lay, snap)

        # ★ 每层健康快照（每秒 1 行，只在 running 时）—— 卡了能直接回看那一刻每层状态。
        self._health_tick()

        # 码表：先把「本帧拍位」解释成「每个码表图层该显示什么」（**只算，不画**）。
        # · 没有码表图层时直接返回（实测 0.41µs/帧，见 tools/cue_tick_test.py）；
        # · 内部异常已被吞掉（清空本帧 + 限流记日志），绝不影响下面的合成。
        self._cue_auto_position()      # 自动跟拍：低频轮询（0.2s），不进 _compose
        # ★ 2026-10-28（能量段落）：按曲库预设的段落表推进档位。
        #   必须放在 `_cue_auto_position()` **之后** —— 它要先有可信的原曲秒。
        #   改到前面会让段落判定永远晚 0.2s（用的是上一次的位置）。
        self._phase_set("section")
        self._section_tick(snap)
        self._cuesheet_tick(snap)

        # 合成
        self._phase_set("compose")
        self._split_cache = {}       # ★ 性能：split 裁切按帧缓存，帧首清空
        # ★ 2026-11-26：Build Up 闪白每 tick 只算一次；GPU 渲染进程也要拿到它。
        #   以前只在 CPU 合成末尾画白，GPU 一屏一进程模式下画面由子进程出，
        #   所以“Build Up 段有切换、但不闪白”。
        self._bu_flash_alpha = self._build_up_flash_alpha(snap)
        # ★ 多屏输出：配置变更必须先重建画布/发送端，再合成主屏（主屏分辨率可能变了）
        if self._out_cfg is not None and self._plan_rev != getattr(self, "_plan_rev_ready", -1):
            self._phase_set("output_plan")
            self._rebuild_output_plan()
            self._phase_set("compose")
        # ★★★ 2026-11-26：完整 GPU 场景合成（moderngl 独立上下文 + FBO + 读回）。
        #   旧 PySide GL 版会长时间持 GIL；新后端不依赖 Qt，默认启用，失败自动回退 CPU。
        _gpu_done = False
        _render_done = False
        if getattr(self, "_render_active", False):
            self._phase_set("gpu_render")
            try:
                _render_done = bool(self._render_proc_tick(snap))
                _gpu_done = _render_done
            except Exception as _e:                            # noqa: BLE001
                self._render_note("渲染进程 tick 异常，回退 CPU：%s" % _e)
                self._close_render_client("tick exception")
                _gpu_done = False
                _render_done = False
        if not _gpu_done and self._gpu_compose_now():
            self._phase_set("gpu_scene")
            try:
                _gpu_done = bool(self._gpu_compose_tick(snap))
            except Exception as _e:                            # noqa: BLE001
                self._gpu_stage_note("[GPU] 合成异常，回退 CPU：%s" % _e)
                _gpu_done = False
        if not _gpu_done:
            self._display_scenes = {}
        # ★ BUG-D 波1：主屏合成前先把**主屏**的生效参数挂上（有 override 才挂；
        #   没有 ⇒ None ⇒ 读全局 ⇒ 与改动前逐字等价）。
        self._cp_cfg = None
        if not _gpu_done:
            try:
                if self._out_cfg:
                    _main = output_model.main_screen(self._out_cfg)
                    if (_main and _main.get("params")
                            and str(_main.get("type") or "") != "main"):
                        self._cp_cfg = output_model.effective_params(self.cfg, _main)
            except Exception as _e:                                # noqa: BLE001
                self._cp_cfg = None
                self._section_log("主屏参数读取失败（回落全局）：%s" % _e)
            self._compose(snap)
        # ★★★ 2026-11-11（用户：多屏预览卡 + CPU 30%+）：**复用主屏合成**。
        #   `dj` 这类 follow_main 的从屏，图层集合/尺寸和主屏完全相同，只差
        #   color / 强度参数；旧实现每帧把 10 层 + 码表**再合成一遍**（ci 8~88ms）。
        #   这里在**主屏染色之前**把未染色的主画布复制给它，后面只套它自己的
        #   颜色 LUT（~2ms），省掉整次重合成。
        #   ⚠ 仅允许 params 里只有 color/auto.intensity（postfx 关闭时）；
        #     强度脉冲沿用主屏（差异 <3%，换来一半合成开销）。
        self._reused_screens = set()
        if not _gpu_done:
            try:
                self._copy_main_for_reused_screens(snap)
            except Exception:                                      # noqa: BLE001
                self._reused_screens = set()
        # ★ 2026-11-11（用户：「操作非常卡」）：主合成/多屏合成/缩略图发布都是
        #   单次几毫秒~几十毫秒的 Qt C++ 调用，PySide 在这些调用期间**不释放 GIL**
        #   （py-spy 实测引擎线程占 GIL 54%，主线程只有 7.5%）。每个重阶段后主动
        #   让出一次 GIL，GUI 才有机会处理点击/重绘，而不是等整帧结束。
        #   `time.sleep(0)` = Sleep(0)：只让出时间片，不引入固定延时。
        try:
            if not bool(getattr(self, "_gui_covered", False)):
                time.sleep(0)
        except Exception:                                      # noqa: BLE001
            pass
        # ★ 2026-10-28（性能）：合并成一个入口，**整帧只读回/写回一次**。
        #   原来两段各做一次 `_np_view_bgr` + `write_back_bgr`（1280×720 共 3.7 MB），
        #   实测每帧白搬 1.43 ms ≈ 4.3% 单核（用户报 5800X 30%）。
        #   顺序不变：颜色先、后处理后 —— 辉光/色差等仍吃到调色后的画面。
        # ★ 2026-11-26：段落切换 / 闪白高峰时跳过最贵的后处理链，
        #   避免 合成 + 调色 + postfx 叠加超过 33ms 造成掉帧。
        _pre_phase = str(getattr(self, "_phase", "") or "")
        _heavy_flash = self._bu_flash_alpha >= 160
        _skip_post = (_pre_phase == "section_switch")
        self._phase_set("postfx")
        # ★ 2026-11-25：GPU 输出视图接管调色 ⇒ 引擎只推进颜色状态并生成 LUT，
        #   不再对 1080p 画布做 CPU LUT（实测省 ~8ms/帧）。
        _gpu_color = bool(getattr(self, "_gpu_color_offload", False)
                          and getattr(self, "_gui_covered", False))
        self._gpu_luts = {}
        if _gpu_done:
            # GPU 合成 + LUT 已在 `_gpu_compose_tick` 里完成；这里只叠 Build Up 闪白，
            # 顺序与 CPU 完全一致：合成 → 调色 → 后处理 → 闪白。
            if self._bu_flash_alpha:
                self._paint_build_up_flash(self.canvas, self._bu_flash_alpha)
            self._cp_cfg = None
        else:
            if _heavy_flash:
                self._gpu_lut_img = None
            if _gpu_color:
                if not _heavy_flash:
                    self._apply_color_postfx(snap, lut_only=True)
                    try:
                        _main = output_model.main_screen(self._out_cfg) or {}
                        _msid = str(_main.get("id") or "")
                        if _msid and getattr(self, "_gpu_lut_img", None) is not None:
                            self._gpu_luts[_msid] = self._gpu_lut_img
                    except Exception:                          # noqa: BLE001
                        pass
            else:
                if not _heavy_flash:
                    self._apply_color_postfx(snap, skip_post=_skip_post)
            # ★ 2026-11-24：Build Up 闪白（主屏；多屏在 _compose_independent 后补）
            #   闪白高峰帧已经在上面跳过调色/后处理，这里只叠白。
            if (self._bu_flash_alpha
                    and not (getattr(self, "_gpu_color_offload", False)
                             and getattr(self, "_gui_covered", False))):
                self._paint_build_up_flash(self.canvas, self._bu_flash_alpha)
            self._cp_cfg = None      # ★ BUG-D：主屏这段用完就复位，别漏到别的屏
        self._phase_set("copy")
        if _render_done:
            # 独立渲染进程已经上屏；这里把 480p 预览搬回 GUI，并把成品帧
            # 交给引擎里已验证的 SpoutOut/NDIOutput 发送线程。
            self._publish_render_preview()
            self._phase_set("routes")
            self._send_multi_routes()
        elif self._out_cfg is not None:
            # ---- 多屏输出 ----
            mode = str(self._out_cfg.get("mode") or "multi")
            if mode == "single":
                self._phase_set("copy_indep")
                # ★ BUG-B（2026-11-10）：单路下**必须先合成独立屏**，否则
                #   `_build_combined()` 通过 `_route_frame()` 取到的独立屏画布
                #   只有 `_rebuild_output_plan()` 里的一次 clear ⇒ 预览里永远黑。
                #   顺序不能反：切分屏的源可能就是独立屏。
                #   每屏 color/postfx 已在 `_compose_independent()` 里各套一次（B2）。
                if not _gpu_done:
                    self._compose_independent(snap)
                if _gpu_done or not (getattr(self, "_gpu_color_offload", False)
                                     and getattr(self, "_gui_covered", False)):
                    self._paint_screen_flashes(getattr(self, "_bu_flash_alpha", 0))
                self._phase_set("copy_comb")
                self._build_combined(snap)
                try:
                    time.sleep(0)      # 5 屏拼接后主动让 GIL，避免主线程停顿
                except Exception:                                  # noqa: BLE001
                    pass
            else:
                if not _gpu_done:
                    self._compose_independent(snap)
                if _gpu_done or not (getattr(self, "_gpu_color_offload", False)
                                     and getattr(self, "_gui_covered", False)):
                    self._paint_screen_flashes(getattr(self, "_bu_flash_alpha", 0))
                self._phase_set("copy_comb")
            self._phase_set("copy_pub")
            self._publish_multi_frame()
            try:
                time.sleep(0)
            except Exception:                                  # noqa: BLE001
                pass
            self._phase_set("routes")
            self._send_multi_routes()
            try:
                time.sleep(0)          # 路由发送后让出时间片（Spout/NDI 入队也是 Python 侧拷贝）
            except Exception:                                      # noqa: BLE001
                pass
        else:
            # ---- 旧的单路输出（没有配置时保持原逻辑）----
            # 深拷贝一帧再发射：GUI 只读这份独立数据，worker 不再碰它，彻底消除跨线程抢画布竞态
            # ★ 没有 GUI/输出窗口消费者时（预览关闭且独立输出窗口隐藏），这次整帧拷贝
            #   和跨线程信号是纯浪费；Spout/NDI 直接读 canvas，不需要它。
            if self._gpu_frame_consumers:
                w, h = self.canvas.width(), self.canvas.height()
                frame = QImage(w, h, self.canvas.format())
                # ★ 2026-10-19 性能：**删掉整幅清零**。下面紧接着就对整幅做 copy，
                #   这次 fill(0) 是纯浪费 —— 实测 1080p 3.33ms/帧、4K 18.03ms/帧（同内容 memcpy 仅 0.64ms）。
                #   这里故意不清零，别以为漏了又加回来。
                try:
                    np.copyto(np.frombuffer(frame.bits(), np.uint8, frame.sizeInBytes()),
                              np.frombuffer(self.canvas.constBits(), np.uint8, self.canvas.sizeInBytes()))
                except Exception:
                    frame = self.canvas.copy()
                # ★ 只保留最新帧 + 投递一个轻量序号：队列里再也不会堆 8MB 的图
                self._latest_frame = frame
                self._frame_seq += 1
                self.frame_ready.emit(self._frame_seq)
            # ★ 2026-11-08（用户：显示器输出码表演出仍掉帧）：把 "copy" 拆开记录。
            #   `copy` 同时覆盖帧拷贝 / Spout 入队 / NDI 发送，现场 `阶段=copy` 无法定位；
            #   Spout 发送线程的 sendImage 有可能偶发持 GIL ⇒ 单独标成 spout/ndi 便于取证。
            self._phase_set("spout")
            self._spout_send()
            self._phase_set("ndi")
            self._ndi_send()
        self._phase_set("ui_snap")
        # ★ 算好「GUI 状态快照」放 `self._ui_snap`，供 GUI 无锁读取（`ui_snapshot`）。
        #   见 FINAL_REPORT P1-1：GUI 的 `_update_status`（500ms）原先把 4 个 @_locked 方法全调
        #   一遍 ⇒ 每 500ms 抢一次渲染锁，慢帧时会等满超时、界面一秒一顿。现改为引擎线程算好、
        #   GUI 只读快照。本函数此刻已持 `_tick_lock`（RLock，**可重入**）⇒ 这里再调那几个
        #   @_locked 方法不会死锁；`_beat_pos` 按时间戳重算且幂等（见其注释），额外调用不改行为。
        try:
            # ★★ 2026-11-11（用户：播放音乐多屏输出时界面/预览非常卡）：
            #   `ui_snap` 里的 switch/phrase/grid 计算每帧都做，且现场 stall 日志
            #   978/991 次都停在 `阶段=ui_snap`。HUD 本身 500ms 才刷新一次，
            #   这里降到 10Hz（0.1s）已经足够，渲染线程每帧省下这组计算/锁竞争。
            _now_ui = time.perf_counter()
            if (not self._ui_snap
                    or (_now_ui - float(getattr(self, "_ui_calc_t", 0.0) or 0.0)) >= 0.1):
                _ui = {"switch_cd": self.switch_countdown(snap),
                       # 引擎 tick 实测（供输出侧 [OUT] 卡顿日志关联）
                       "tick_ms": round(float(getattr(self, "_tick_ms", 0.0) or 0.0), 1),
                       "tick_gap_ms": round(float(getattr(self, "_tick_gap_ms", 0.0) or 0.0), 1),
                       # ★ 2026-11-06：告诉 HUD 这次是"段落接管"（用段落措辞）还是"按拍"
                       "switch_kind": getattr(self, "_due_kind", None),
                       # ★ 2026-11-06：当前段落名（设置页「当前节奏」直接显示"跟随段落「X」"）
                       "sec_name": str((getattr(self, "_sec_info", None) or {}).get("name") or ""),
                       "phrase_cd": self.phrase_countdown(snap),
                       "grid_view": self.beat_grid_view(snap),
                       # ★ T05②：当前段/多锚（有分段/多锚才有值，否则 None ⇒ HUD 一字不变）
                       "grid_seg": self.grid_segment_info(snap)}
                # ★★ 2026-11-11：`next_clips` 功能已随「下一个素材」开关一起删除，
                #   不再为 10 个图层每 0.25s 调 `next_clip_for()`（现场 ui_snap 205ms 的元凶）。
                _ui["next_clips"] = {}
                self._ui_calc_t = _now_ui
                self._ui_snap = _ui          # 整体赋值（GIL 下原子）
        except Exception:
            pass
        except Exception:
            pass

    # ⚠⚠ 这里**故意不加 @_locked**：它是 GUI 每帧都要调的取帧入口，
    #   而引擎线程在整帧渲染期间一直握着 _tick_lock ⇒ 加锁等于让 GUI 每帧都
    #   等在锁上（最坏 2 秒一次），屏幕直接卡成幻灯片。
    #   安全性由"只读一个引用"保证：`self._latest_frame = img` 是**原子赋值**，
    #   GUI 拿到的是上一帧的完整对象（引擎之后只会把它换掉、绝不原地改它）。
    #   —— 这是全项目唯一一个允许无锁的跨线程读，别"顺手修"成带锁的。
    def latest_frame(self):
        """取最近一帧合成结果（跨线程只读）。

        QImage 是隐式共享的，`_tick_locked` 每帧都**新建**一个对象再替换引用，
        所以绘制端拿到的是当时的快照，引擎随后替换不会影响它 —— 无需加锁。
        """
        return self._latest_frame

    # ⚠⚠ 这里也**故意不加 @_locked**（与 `latest_frame` / `modes_snapshot` 同类，已在
    #   `tools/_scan_locks.py` 白名单登记）：它是 GUI 高频读取的只读快照，加锁就等于让 GUI
    #   去抢渲染锁（正是本次要修的病）。安全性：`self._ui_snap = _ui` 是**整体引用赋值**
    #   （GIL 下原子），GUI 拿到的要么是上一份、要么是当前份，绝不会读到半成品 —— 引擎之后
    #   只会把它整个换掉、绝不原地改它。
    def ui_snapshot(self):
        """GUI 无锁只读状态快照（每 tick 由引擎线程算好，见 `_tick_locked` 尾部）。

        返回 dict：`{"switch_cd", "phrase_cd", "grid_view", "grid_seg", "next_clips"}`。GUI 用它替代原先
        直接调用 `switch_countdown` / `phrase_countdown` / `beat_grid_view` / `next_clip_for`
        这四个 `@_locked` 方法（那会每 500ms 抢一次渲染锁，慢帧时卡界面）。⚠ 勿"顺手修"成加锁版本。"""
        return self._ui_snap

    def _output_fail(self, ch, now, exc):
        """记录一次输出（NDI/Spout）发送失败：退避重试 + 30s 限流写 `output_health.log`。

        ★ 2026-10-02（任务#14）：旧实现在这里把 `_ndi_fail/_spout_fail` 置成**永久**标志 ⇒
        中途出一次错（接收端重连 / 网络抖动 / 分辨率变化 / Runtime 晚启动）发送端整场不再推帧
        ⇒ **接收端永久冻在最后一帧**，而本机预览/输出窗口/日志全正常。现在改成退避重试：
        失败只推迟到 `_retry_at` 再试，**永不永久放弃**；成功一次由 `_output_ok` 清零。
        `ch`：`"NDI"` / `"Spout"`。退避 1→2→4→8→16→30s（上限 `_OUTPUT_BACKOFF_CAP`）。
        """
        prefix = "_ndi" if ch == "NDI" else "_spout"
        fails = int(getattr(self, prefix + "_fails", 0)) + 1
        setattr(self, prefix + "_fails", fails)
        setattr(self, prefix + "_fail", True)
        if not getattr(self, prefix + "_fail_since", 0.0):
            setattr(self, prefix + "_fail_since", now)
        backoff = _output_backoff(fails)
        setattr(self, prefix + "_retry_at", now + backoff)
        brief = ("%s: %s" % (type(exc).__name__, exc))[:120]
        # 失败日志按 `_OUTPUT_FAIL_LOG_MIN`(=30s) 限流：退避中不刷屏；首次失败必记（log_at 起始 0）
        if now - float(getattr(self, prefix + "_log_at", 0.0) or 0.0) >= _OUTPUT_FAIL_LOG_MIN:
            setattr(self, prefix + "_log_at", now)
            _output_health_write("%s 发送失败（连续第 %d 次，退避 %.0fs 后再试）：%s"
                                 % (ch, fails, backoff, brief))
            print("%s send failed (#%d):" % (ch, fails), exc)

    def _output_ok(self, ch, now):
        """一次成功发送：清零退避/计数；若此前处于失败态，写一行"恢复 + 中断时长"。"""
        prefix = "_ndi" if ch == "NDI" else "_spout"
        fails = int(getattr(self, prefix + "_fails", 0) or 0)
        since = float(getattr(self, prefix + "_fail_since", 0.0) or 0.0)
        setattr(self, prefix + "_fails", 0)
        setattr(self, prefix + "_retry_at", 0.0)
        setattr(self, prefix + "_log_at", 0.0)
        setattr(self, prefix + "_fail", False)
        if fails or since:
            dur = (now - since) if since else 0.0
            setattr(self, prefix + "_fail_since", 0.0)
            _output_health_write("%s 发送恢复（中断 %.1fs）" % (ch, dur))

    def _make_output_snapshot(self, frame):
        """给 NDI / Spout 做**一次**私有快照（之后两路共用，渲染线程不再各拷一遍）。

        · 画布已是 ARGB32_Premultiplied（见 `_canvas_format`）⇒ 直接 copy；
        · 万一还是直通 ARGB32（旧配置/独立路径）⇒ 转成预乘（发送端约定）；
        · RGB32 ⇒ copy。
        ★ 2026-11-25：旧实现是 NDI 一次转换+拷贝、Spout 再一次，1080p 合计
          ~5ms/帧；现在每帧只做一次（~1.4ms），且放在输出之前只做一次。
        """
        if frame is None or frame.isNull():
            return None
        try:
            if frame.format() == QImage.Format_ARGB32_Premultiplied:
                return frame.copy()
            if frame.hasAlphaChannel():
                return frame.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            return frame.copy()
        except Exception:                                      # noqa: BLE001
            return None

    def _get_output_snapshot(self, frame):
        """同一 tick 内 NDI/Spout 共用同一张快照（按帧对象 + tick 令牌缓存）。"""
        tok = getattr(self, "_out_snap_token", None)
        c = getattr(self, "_out_snap_cache", None)
        if c is not None and c[0] is tok and c[1] is frame:
            return c[2]
        img = self._make_output_snapshot(frame)
        self._out_snap_cache = (tok, frame, img)
        return img

    def _spout_send(self, snap=None):
        """Spout 输出：**渲染线程只拷贝入队**，native `sendImage` 在专用线程提交。

        ⚠⚠⚠ 2026-10-29 现场（用户报"预览和输出都一卡一卡，不是帧数低那种卡"）：
          旧实现在本方法里**同步**调 `s.sendImage(buf, w, h, ...)`，而它跑在
          `_tick_locked`（渲染线程）上。实测（tools/_probe_spout.py）：

              720p   中位 1.01ms  p95 1.44ms  **max 14327ms**
              1080p  中位 2.04ms  p95 2.77ms  **max   217ms**

          ⇒ 偶发一次就把整个 tick 停住十几秒：**预览面板和输出窗口同时冻**，而 GUI
          主线程不受影响（所以 `ui_stall.log` 一条都没有、`layer_health.log` 里却是
          好几层 age 一起飙）。用户对照"OBS 的 Spout/NDI 输出不卡"正好印证：问题不在
          Spout 本身，而在同步调用。NDI 那条路 2026-10-08 已经改成同款异步（见
          `ndi_out.send_video`），Spout 这里一直漏着。

          现在：渲染线程只 `bytearray` 拷贝一帧（720p 实测 1.7ms / max 2.8ms）再入队，
          发送线程慢就丢旧帧。发送线程的异常由 `last_error()` 回传，仍走既有退避重试
          （`_output_fail`），绝不永久停。
        """
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
        now = time.perf_counter()
        # ui_main 在用户重新开启 Spout 时会把 `_spout_fail` 复位成 False ⇒ 视为"立即重试"
        if not self._spout_fail:
            self._spout_fails = 0
            self._spout_retry_at = 0.0
        elif now < self._spout_retry_at:
            return                      # 退避窗口内：这一帧不尝试
        try:
            import spout_out
            if self._spout is None:
                self._spout = spout_out.SpoutOut(oc.get("spout_name")
                                                 or "EasyRealityAutoVJ")
            # ★★ 2026-11-25：渲染线程**不再**做「预乘转换 + bytearray 拷贝」。
            #   同一 tick 内 NDI 与 Spout 共用 `_get_output_snapshot()` 的一张
            #   QImage 快照；发送线程直接读它的 bits()。1080p 每帧省 ~3.5ms，
            #   这是单屏开 NDI+Spout 从 20fps 回到 30fps 的关键之一。
            if snap is None:
                snap = self._get_output_snapshot(self.canvas)
            if snap is None or snap.isNull():
                raise RuntimeError("输出快照创建失败")
            if hasattr(self._spout, "send_image"):
                self._spout.send_image(snap)
            else:                                          # 旧 sender（热更新兜底）
                self._spout.send(snap.bits(), snap.width(), snap.height(),
                                 snap.bytesPerLine())
        except Exception as e:                                     # noqa: BLE001
            # ⚠ 失败时**保留已有 `self._spout`**、不丢成 None 重建：源名反复消失/出现更难恢复
            self._output_fail("Spout", now, e)
            return
        # 发送线程里的异常（native 层/GL 上下文）在这里回传，交给同一套退避逻辑
        err = None
        try:
            err = self._spout.last_error()
        except Exception:                                          # noqa: BLE001
            err = None
        if err:
            self._output_fail("Spout", now, RuntimeError(err.strip().splitlines()[-1]
                                                         if err.strip() else err))
            return
        self._output_ok("Spout", now)

    def _ndi_send(self, snap=None):
        """NDI 输出：把画布帧喂给 NDI 发送器（音频由采集线程另行喂）。缺 Runtime 静默禁用；
        失败走**退避重试**（不再永久停，见 `_output_fail`）。"""
        # ★ 2026-09-27 修（用户报 bug：没开始 VJ 时 NDI 也在输出）：
        #   没点「开始」一律**不喂帧**（理由同 `_spout_send`：接收端可能已经在线，未就绪画面会直接上屏）。
        if not self.running:
            return
        oc = self.cfg["output"]
        if not oc.get("ndi_enabled"):
            return
        now = time.perf_counter()
        # ui_main 在用户重新开启 NDI 时会把 `_ndi_fail` 复位成 False ⇒ 视为"立即重试"
        if not self._ndi_fail:
            self._ndi_fails = 0
            self._ndi_retry_at = 0.0
        elif now < self._ndi_retry_at:
            return                      # 退避窗口内：这一帧不尝试
        try:
            from ndi_out import get_ndi
            if self._ndi is None:
                self._ndi = get_ndi(oc.get("ndi_name") or "EasyRealityAutoVJ")
            # ★★ 2026-11-25：与 Spout 共用同一张预乘快照，渲染线程不再
            #   `convertToFormat` + 池拷贝（1080p 每帧省 ~2.5ms）。见
            #   `_make_output_snapshot` 的说明。
            if snap is None:
                snap = self._get_output_snapshot(self.canvas)
            if snap is None or snap.isNull():
                raise RuntimeError("输出快照创建失败")
            w, h = snap.width(), snap.height()
            # ⚠ 必须带**真实分辨率**：发送端的 VideoSendFrame 只能在 open() 之前定死尺寸，
            #   所以 sender 是按第一次发帧的尺寸建立的。旧代码这里不给尺寸 → 直接返回 False
            #   → `_ndi_fail` 被永久置位 → NDI 输出再也不会尝试（静默失效）。
            #   现在 `_ensure()` 失败 = 抛错走退避重试（分辨率变化 / Runtime 晚启动都能自愈）。
            if not self._ndi._ensure(w, h):
                raise RuntimeError("NDI _ensure(%dx%d) 返回 False" % (w, h))
            if hasattr(self._ndi, "send_image"):
                self._ndi.send_image(snap)
            else:                                          # 旧 sender（热更新兜底）
                import numpy as _np
                bgra = _np.frombuffer(snap.bits(), dtype=_np.uint8,
                                      count=snap.bytesPerLine() * h)
                self._ndi.send_video(bgra, w, h)
        except Exception as e:                                     # noqa: BLE001
            # ⚠ 失败时**保留已有 `self._ndi`**（不丢成 None 重建）：NDI 源名在接收端反复
            #   消失/出现会让接收端更难恢复；复用同一 sender，故障消失后接着推同一源。
            self._output_fail("NDI", now, e)
            return
        self._output_ok("NDI", now)

    # ---------------- 行为模式 ----------------
    def modes_snapshot(self):
        """★ 无锁读取「模式状态」——**专供 GUI 高频轮询**（见下方说明）。

        ## ⚠⚠ 为什么不能直接用 `manual_mode()`（2026-10-08 现场事故）

        用户报「**软件非常卡**」。`ui_main._update_status()` 挂在 **500ms 的
        `QTimer`** 上，里面调 `self.engine.manual_mode()`；而 `manual_mode` 是
        `@_locked` ⇒ 主线程每 0.5 秒都要去抢 `_tick_lock`。
        这把锁由引擎线程**在整帧渲染期间持有**（`_tick` → `with self._tick_lock`），
        一旦那一帧慢（例如 `colorfx.apply` 这种逐像素活），主线程就**等到超时**
        （`_LOCK_TIMEOUT=2.0s`）⇒ 界面一秒一顿，看起来就是"卡成幻灯片"。

        ⇒ 状态栏轮询是**只读展示**，**绝不该**为了一个 int/字符串去等渲染锁。
           （与 `latest_frame()` 同一条原则：GUI 每帧路径绝不碰引擎锁。）

        ## 为什么这样是安全的
        本函数只读 `self.cfg` / `self.mode` / `self.mode_scores`：
          - `self.cfg["mode"]` 是个普通 dict，`.get()` 在 GIL 下是**原子**的；
          - `self.mode` 是 str、`self.mode_scores` 是 dict —— 读到的要么是旧值
            要么是新值，**绝不会读到半成品**（引用替换是原子的）。
        最坏情况：界面比渲染线程晚 1 帧（16ms）显示新模式 —— 对状态栏无影响。

        ⚠ 本函数**故意不加 `@_locked`**（与 `latest_frame` 一样属于白名单），
          已在 `tools/_scan_locks.py` 里显式豁免，别"顺手修"成加锁版本。
        """
        manual = None
        try:
            m = self.cfg["mode"].get("manual", "auto")
            if m not in (None, "auto") and m in MODES:
                manual = m
            elif str(m) == SWITCH_MANUAL:
                # 手动切换策略：状态栏/HUD 要显示「手动切换」而不是上一次的节奏
                return {"mode": SWITCH_MANUAL, "manual": SWITCH_MANUAL,
                        "scores": dict(self.mode_scores or {})}
        except Exception:                                        # noqa: BLE001
            manual = None
        # ★ 2026-11-11（用户：段落接管时“时不时跳成常规切/快切”）：只要段落表
        #   正在接管，展示层就固定显示「按段落切换」。实际切换也由 `_section_tick`
        #   独占，不再让常规/快切的拍位排期插刀。
        try:
            if (manual is None and bool(getattr(self, "running", False))
                    and getattr(self, "_sec_energy", None) is not None):
                return {"mode": "section", "manual": manual,
                        "scores": dict(self.mode_scores or {})}
        except Exception:                                        # noqa: BLE001
            pass
        return {
            "mode": manual or self.mode,
            "manual": manual,
            "scores": dict(self.mode_scores or {}),
        }

    @_locked
    def manual_mode(self):
        m = self.cfg["mode"].get("manual", "auto")
        # 已删除「慢切」「静止/氛围」模式：旧配置若存了 slow/ambient，回退到自动，
        # 避免 MODE_META KeyError（旧配置迁移）。
        # ⚠ 「手动切换」(SWITCH_MANUAL) 也走这条 return None：它不是节奏，
        #   self.mode 必须保持 normal/fast/beat 之一（否则 MODE_META 取不到）。
        if m in (None, "auto") or m not in MODES:
            return None
        return m

    def manual_switch_mode(self):
        """是否处于「手动切换素材」策略（完全不自动切换）。

        ⚠ 故意**不加 `@_locked`**：渲染线程每帧都要读它，而它只读 `cfg`（普通 dict，
          `.get()` 在 GIL 下原子）。加锁等于每帧多抢一次渲染锁，本函数是纯读、无副作用。
          也不被 GUI 直接调用 ⇒ `tools/_scan_locks.py` 的白名单不用动。
        """
        try:
            return str(self.cfg["mode"].get("manual", "auto")) == SWITCH_MANUAL
        except Exception:                                        # noqa: BLE001
            return False

    @_locked
    def manual_switch_layer(self, layer_idx):
        """手动切换**某一个图层**的素材（右键菜单「手动切换素材」）：顺序切到下一个。

        与「下一素材」(`next_scene`) 的区别：只动这一层，**不重排全局切换倒计时**，
        所以现场「悄悄换一张图」不会把别层的节奏打乱。

        安全边界（用户 2026-10-25 明确要求"不要影响码表 / Kv / 停止切换"）：
          · 码表图层：`clips` 为空（素材由 `.ercue` 运行时给），天然不动；
            即便将来有 clips，也在下面显式跳过 `is_cuesheet`；
          · Kv 待机层：由静音进出驱动，手动切没有意义 ⇒ 直接拒绝；
          · 全局锁定(`self.locked`)：拒绝（锁定就是要画面不动）；
          · 「停止切换本层素材」的图层：**允许**切（这是用户对这个图层的显式操作），
            但停在哪一格的那个标记 `lay.fixed` 要跟着挪过去 ⇒ 切完仍然是"停住"状态，
            不会被下一次自动切换顶掉，也不会因为 fixed 指向旧索引导致画面与标记不一致。
        """
        try:
            _i = int(layer_idx)
        except (TypeError, ValueError):
            return False
        # ⚠ 必须显式判上下界：`self.layers[-1]` 在 Python 里是合法的**最后一层**，
        #   只写 try/except IndexError 会把 -1 当成有效索引，悄悄切错图层。
        if not (0 <= _i < len(self.layers)):
            return False
        lay = self.layers[_i]
        if not lay.clips or len(lay.clips) < 2 or self.locked:
            return False
        if getattr(lay, "is_kv", False) or getattr(lay, "is_cuesheet", False):
            return False
        cur = int(getattr(lay, "cur", 0) or 0)
        if not (0 <= cur < len(lay.clips)):
            cur = 0
        nxt = (cur + 1) % len(lay.clips)
        if nxt == cur:
            return False
        self._switch_layer_to(lay, nxt)
        if getattr(lay, "fixed", -1) >= 0:
            lay.fixed = nxt
        lay.pre_idx = -1
        lay.warm_pend = False
        return True

    def _update_mode(self, snap):
        now = time.perf_counter()
        if self.manual_switch_mode():
            # 手动切换：节奏档位不再自动打分（否则 `self.mode` 会在后台乱跳，
            # 一旦切回自动，过渡时长/状态栏会先闪一下别的档）。
            return
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
        E = self._energy_for_switch(snap)
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
            # ★ 2026-10-30：手动相位绑在**旧坐标系**上，坐标系一换它就是错值
            #   （切换点会整体偏 delta 那么多拍 —— 用户听到的"差 4 拍"就是这么来的）。
            if getattr(self, "_bar_off_user", None) is not None:
                self._bar_off_user = None
                self._next_due = None
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
        # ★★ 2026-10-30（用户报「好几首歌切素材和八拍线差四拍」）：**换歌就作废手动相位**。
        #   `_bar_off_user`（按「下一素材」时定的相位）原来只在"连续静音 3 秒"后清掉，
        #   而 DJ 接歌混播根本没有 3 秒静音 ⇒ 一首歌里按过的那一下会**跟着后面每一首**
        #   继续生效，后面的歌就整体偏掉（偏多少取决于当时按在第几拍，常听到的就是差 4 拍）。
        #   用户按它的语义本来就是"这一首从现在开始按我的拍走"，换歌必须回到网格给的八拍头。
        if getattr(self, "_bar_off_sid", None) != sid:
            self._bar_off_sid = sid
            if getattr(self, "_bar_off_user", None) is not None:
                self._bar_off_user = None
                self._next_due = None
                self._section_log("换歌 ⇒ 作废手动相位（回到八拍头）sid=%s" % sid)
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

    @_locked
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

    def grid_segment_info(self, snap):
        """HUD 用「当前段 / 当前八角锚」信息（T05②）；**无分段且无多锚时返回 None**。

        返回 `{"kind": "seg"|"anchor", "n": 段号(1基), "bpm": 实际生效BPM, "anchor": 锚点秒}`。
        文案拼装放在 GUI（`ui_main._hud_grid_seg`）里做，引擎不依赖 i18n。

        门控与「距八拍头」**完全一致**（都走 `_grid_state`）：只有网格已锁定、且全局开关
        「节拍网格」打开时才返回值 ⇒ 现场看到的段号就是**此刻真正在用**的那一段，
        不是按播放进度猜的；老曲目（无 manual）恒为 None，HUD 一字不变。

        ⚠ 本方法**不加 `@_locked`**：只被 `_tick_locked`（已持 `_tick_lock`，RLock 可重入）
          调用，结果随 `ui_snapshot` 一起发布给 GUI；GUI 侧绝不直接调它（那会抢渲染锁）。
        """
        try:
            gs = self._grid_state(snap)
        except Exception:                                        # noqa: BLE001
            return None
        if gs is None:
            return None
        grid, bl, anchor, off, _r, _sid = gs
        if bl is None or bl <= 0 or anchor is None:
            return None
        bpm = 60.0 / float(bl)
        segs = grid.get("segments") or []
        if segs:
            idx = 0
            for i, s in enumerate(segs):
                try:
                    t0 = float(s.get("t0") or 0.0)
                except (TypeError, ValueError, AttributeError):
                    t0 = 0.0
                if t0 <= float(off) + 1e-9:
                    idx = i
            return {"kind": "seg", "n": idx + 1, "bpm": bpm, "anchor": float(anchor)}
        if grid.get("anchors"):
            return {"kind": "anchor", "n": 1, "bpm": bpm, "anchor": float(anchor)}
        return None

    @_locked
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

    @_locked
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
    def _mode_for_screen(self, screen=None):
        """该屏生效的"切换节奏"模式；无 override ⇒ 全局 `self.mode`（逐字等价）。

        ★ 2026-11-26：全局/主屏幕选「手动切换」时，必须返回 `SWITCH_MANUAL`。
          以前主屏直接 `return self.mode`（normal/fast/beat）⇒ 段落切换那套
          `_apply_section_switch` 不认手动，仍然把主屏图层切走。
        """
        try:
            _gm = self.manual_switch_mode()
            if not screen:
                return SWITCH_MANUAL if _gm else self.mode
            # ★ 主屏幕 = 全局，主屏 params 一律忽略。
            if str(screen.get("type") or "") == "main":
                return SWITCH_MANUAL if _gm else self.mode
            if not (screen.get("params") or {}).get("mode"):
                return SWITCH_MANUAL if _gm else self.mode
            md = output_model.effective_params(self.cfg, screen).get("mode") or {}
            if bool(md.get("auto", True)):
                return self.mode                    # 自动：仍用引擎实时判定的模式
            man = str(md.get("manual") or "")
            if man == SWITCH_MANUAL:
                return SWITCH_MANUAL
            if man in MODE_META:
                return man
            return SWITCH_MANUAL if _gm else self.mode
        except Exception:                                          # noqa: BLE001
            pass
        try:
            return SWITCH_MANUAL if self.manual_switch_mode() else self.mode
        except Exception:                                          # noqa: BLE001
            return self.mode

    def _mode_for_layer(self, lay):
        """该层所属屏的生效模式（渲染时取过渡时长用）；取不到 ⇒ 全局。"""
        try:
            if lay is not None and getattr(lay, "is_kv", False):
                return self.mode
            return self._mode_for_screen(self._layer_screen(lay) if lay is not None else None)
        except Exception:                                          # noqa: BLE001
            return self.mode

    def _screen_sensitivity(self, screen=None):
        try:
            if screen and (screen.get("params") or {}).get("mode"):
                md = output_model.effective_params(self.cfg, screen).get("mode") or {}
                return int(md.get("sensitivity", 1))
        except Exception:                                          # noqa: BLE001
            pass
        return int(self.cfg["mode"].get("sensitivity", 1))

    def _layers_of_screen(self, screen):
        """该屏自己的层（复用 `effective_layers`）。"""
        plan = self._out_cfg
        if not plan or not screen:
            return []
        try:
            all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
            ids = output_model.effective_layers(plan, screen, all_ids)
            return self._pin_kv_order(self._out_layers_by_ids(ids))
        except Exception:                                          # noqa: BLE001
            return []

    def _auto_switch_tick_overrides(self, snap):
        """★ BUG-D 波2：只给**带 override 的屏**各自排期（旁路，不动主路径）。

        split/copy 没有自己的素材流 ⇒ 跳过。欠账钳位照抄全局那条（防抽搐）。
        """
        plan = self._out_cfg
        if not plan:
            return
        if self.locked:
            return
        ov_sids = self._override_screen_ids()
        # 清理已删除屏的状态（照 `_route_senders` 的写法）
        for sid_ in list(self._scr_switch.keys()):
            if sid_ not in ov_sids:
                self._scr_switch.pop(sid_, None)
        if not ov_sids:
            return
        # ★★ 2026-11-11（用户：段落切换稳不住，时不时跳常规/快切）：
        #   段落表接管期间，**所有屏**都只能由 `_section_tick` 在段落边界切；
        #   带 override 的屏以前仍按自己的 8/16 拍排期切（现场 `dj` 屏日志每 8/16
        #   拍一条），和段落边界交替出现 ⇒ 听感就是节奏一直在变。
        #   这里直接放弃本帧及已排的 due，等段落路径统一切（手动模式仍在上面跳过）。
        if getattr(self, "_sec_energy", None) is not None:
            for _st in self._scr_switch.values():
                try:
                    _st["due"] = None
                except Exception:                              # noqa: BLE001
                    pass
            return
        pos = self._beat_pos(snap)
        for sc in plan.get("screens") or []:
            sid = str(sc.get("id") or "")
            if sid not in ov_sids:
                continue
            mode = self._mode_for_screen(sc)
            if mode == SWITCH_MANUAL:
                continue                                    # 手动：不自动切
            interval = self._current_interval(snap, screen=sc)
            if interval is None:
                continue
            st = self._scr_switch.setdefault(sid, {"pos": 0.0, "due": None})
            if pos < st["pos"]:                             # 拍钟被重置
                st["pos"] = pos
                st["due"] = None
            st["pos"] = pos
            if st["due"] is None:
                st["due"] = float(self._bar_floor(pos, snap)) + float(interval)
            due = st["due"]
            if pos < due:
                continue
            # 欠账钳位：落后超过一个完整间隔 ⇒ 重排，不追
            if pos - due > max(4.0, float(interval)):
                st["due"] = None
                continue
            st["due"] = None                                # 切完重排（下一拍起算）
            # 过渡时长按该屏模式 + 该屏灵敏度
            meta = MODE_META[mode]
            try:
                TR = max(-1.0, min(1.0, snap["energy_trend"]))
            except Exception:                               # noqa: BLE001
                TR = 0.0
            sens = self._screen_sensitivity(sc)
            try:
                E = max(0.0, min(1.0, self._energy_for_switch(snap)))
            except Exception:                               # noqa: BLE001
                E = 0.5
            f = E + TR * 0.15 + (-0.1, 0.0, 0.1)[max(0, min(2, sens))]
            f = max(0.0, min(1.0, f))
            trans_ms = int(meta["trans_ms"] * (1.3 - f * 0.3))
            _n_sw = 0
            for lay in self._layers_of_screen(sc):
                if lay.fixed >= 0 or not lay.clips:
                    continue
                if getattr(lay, "is_kv", False) or getattr(lay, "is_cuesheet", False):
                    continue
                # ★ 2026-11-26：手动图层只手动切
                if not getattr(lay, "auto_mode", False):
                    continue
                if self.beat_owns(lay):
                    continue
                lay.trans_ms = trans_ms
                self._switch_layer_random(lay, snap)
                _n_sw += 1
            # ★ 2026-11-10：override 通道**必须落日志**，否则"两屏节奏不同"无法取证
            #   （QA 量到 B 屏 0 次切换，其实只是没写日志）。格式与全局那条一致，只在**行尾追加**字段
            #   （规则 27：不破坏既有 regex）。节流按屏各自计时，避免一屏把另一屏的日志吃掉。
            try:
                st.setdefault("log_t", 0.0)
                _now2 = time.perf_counter()
                if _n_sw and (_now2 - float(st.get("log_t", 0.0) or 0.0) >= 10.0):
                    st["log_t"] = _now2
                    _w2 = float(self._align_width(snap))
                    _res2 = (pos - float(self._bar_off(snap))) % _w2
                    if _res2 > _w2 / 2.0:
                        _res2 -= _w2
                    _output_health_write(
                        "拍位切换[屏] 栅格=%d拍 残差=%+.2f拍 模式=%s sid=%s 屏=%s 间隔=%.0f拍"
                        % (int(round(_w2)), _res2, mode,
                           snap.get("recognized_song_id", -1),
                           str(sc.get("name") or sid), float(interval)))
            except Exception:                                      # noqa: BLE001
                pass

    def _current_interval(self, snap, screen=None):
        """当前生效的切换间隔（拍）；None 表示不自动切换。
        常规切/快切为固定拍数（16 拍 / 8 拍 = 4 / 2 个 4/4 小节），保证素材始终
        停留整数个小节、切换点踩在乐句结构线上；逐拍交替走独立的 _beat_tick 节奏。
        「素材最小切换间隔」选项已删除：间隔固定，不再被配置里的残留值覆盖。"""
        _mode = self._mode_for_screen(screen)
        if _mode == SWITCH_MANUAL:
            return None
        if _mode == "beat":
            return 1.0
        meta = MODE_META[_mode]
        lo, hi = meta["beats"]
        if lo == hi:
            return float(lo)   # 固定拍数（常规 16 / 快切 8）
        # 兼容仍有区间的模式（未来扩展）：按能量在 lo~hi 间线性插值
        TR = max(-1.0, min(1.0, snap["energy_trend"]))
        sens = self._screen_sensitivity(screen)
        E = max(0.0, min(1.0, self._energy_for_switch(snap)))
        f = E + TR * 0.15 + (-0.1, 0.0, 0.1)[max(0, min(2, sens))]
        f = max(0.0, min(1.0, f))
        return hi - (hi - lo) * f   # f=1(高能)→lo；f=0(低能)→hi

    def beats_to_next_switch(self, snap):
        """距离下一次自动切换还有多少拍（供预览 HUD）。
        基于对齐到小节线的固定目标点，倒计时单调递减不跳动。

        手动切换策略下**永远返回 None**（没有"下一次自动切换"这回事）——
        HUD 会自然省掉「还有 N 拍切换」这一段，而不是卡在某个数字上不动。
        """
        if self.manual_switch_mode():
            return None
        if self.mode == "beat":
            return 1.0
        due = self._next_switch_due(snap)
        if due is None:
            return None
        pos = self._beat_pos(snap)
        return max(0.0, due - pos)

    @_locked
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

    def _snap_to_phrase(self, target, snap, forward=False):
        """把拍位吸附到最近的栅格线（网格模式 = 八拍头，否则 = 4 拍小节线）。

        与 `_next_switch_due` 用**同一组** `_align_width` / `_bar_off` —— 所以
        「素材切换落在哪」和「效果切换 / 段落切换落在哪」必然是**同一条网格**，
        这正是用户 2026-10-28 要求的一致性（不能各算一套）。

        `forward=True`：只许往后吸。用于"到期点"——否则 `round` 可能把到期点吸到
        当前拍位之前，下一帧就立刻判"已到期"，效果会每帧重挑一次。
        """
        try:
            w = float(self._align_width(snap))
            off = float(self._bar_off(snap))
            t = float(target)
        except Exception:                                     # noqa: BLE001
            try:
                return float(target)
            except (TypeError, ValueError):
                return 0.0
        if not (math.isfinite(w) and math.isfinite(off) and math.isfinite(t)) or w <= 0:
            return t
        if forward:
            return off + math.ceil((t - off) / w - 1e-9) * w
        return off + round((t - off) / w) * w

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
    # ★ 2026-11-06：段落接管素材切换后的「最长不换素材」上限（拍）。
    #   段落可能很长（20 个八拍 = 160 拍），不做上限的话现场会出现"十几秒不换画面"。
    #   64 拍 ≈ 4 个八拍乐句，也是常规切（16 拍）的 4 倍，够宽松又不会闷。
    #: 段落模式下的**安全上限**：段落再长也别超过这么多拍不换素材。
    #: ★ 2026-11-11（用户：稳不住按段落切换，时不时跳常规/快切）：旧值 64 拍
    #:   ≈ 22 秒，现场很多 drop/过渡段本身就 25~30 秒 ⇒ 每到 64 拍就被拍位路径
    #:   强切一刀，听感就是“段落中间突然变回常规切”。这里放到 512 拍（≈3.5 分钟），
    #:   只作为脏数据的保险丝，正常段落完全由段落边界决定。
    AUTO_SWITCH_MAX_BEATS = 512.0
    #: **段落切换**提前多少拍触发。
    #: 2026-11-08 用户要求：识别到歌曲后按段落切换有一拍提前量，去掉 ⇒ 0.0。
    #: ⚠ 按拍切换的 `SWITCH_ADVANCE_BEATS` 是另一条路径，不要一起改。
    SECTION_ADVANCE_BEATS = 0.0
    #: 段落剩余不足这么多拍时**不再为它换素材**（避免 A→B 一两拍→C）。
    #: 2026-11-25 用户：段落边界前/锁位置晚时，B 只演一两拍就被 C 切掉。
    #: 4 拍 = 一个完整小节；少于一小节的段落不值得再起一个新素材。
    SECTION_MIN_HOLD_BEATS = 4.0

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
        # ★★★ 2026-11-06（用户：「有段落分析时就用段落分析，没有时再用常规/快切兜底，
        #   不然有时候它会连着切两次素材」）：
        #   问题：段落边界（能量跨档 `_energy_tier_switch`）会切一次，紧接着按拍位的
        #   切换点又在附近 ⇒ 同一秒附近连切两次，画面像抽搐。
        #   改法：**这首歌有段落表时，下一次切换点直接取"下一个段落起点"**，不再按
        #   16/8 拍硬算；没有段落表才退回原来的固定间隔。
        #   ⚠ 代价（用户已知并接受）：段落很长时素材会停留很久 ⇒ 加一道
        #     `AUTO_SWITCH_MAX_BEATS` 上限，超了就按上限强制切一次，避免长时间不动。
        #   ⚠ 段落起点是**秒**，这里换算成拍位增量；只有 bpm / 当前秒都拿得到才用，
        #     否则退回原逻辑（绝不因为缺字段而不切）。
        _secs = getattr(self, "_sec_list", None)
        _sec_beat = None
        if _secs and len(_secs) > 1:
            # ★★★ 2026-11-06（测试 `_test_section_single_switch.py` 抓到的真 bug）：
            #   原来写 `int(getattr(self, "_sec_idx", -1) or -1)` —— **第 0 段会被 or 变成 -1**，
            #   于是"第一段→第二段"这个最关键的边界（intro→drop）根本不走段落排期，
            #   退回 16 拍硬算 ⇒ 拍位路径先切一刀、段落路径再切一刀（用户报的"连切两个素材"）。
            #   `_cur_sec` 同理：`x or -1.0` 会把**歌刚开头的 0.0 秒**变成 -1.0。
            #   ⚠ 铁律：`or 默认值` 不能用来兜"拿不到"——合法的 0/0.0 会被当空值。
            _i_raw = getattr(self, "_sec_idx", -1)
            _i = int(_i_raw) if isinstance(_i_raw, (int, float)) else -1
            try:
                _bpm = float(snap.get("bpm") or 0.0)
                _cur_raw = (getattr(self, "_sec_info", None) or {}).get("sec")
                _cur_sec = float(_cur_raw) if isinstance(_cur_raw, (int, float)) else -1.0
                if _bpm > 0 and 0 <= _i < len(_secs) - 1 and _cur_sec >= 0:
                    _nxt = float(_secs[_i + 1].get("t0") or 0.0)
                    _dsec = _nxt - _cur_sec
                    if 1.0 <= _dsec <= 600.0:            # 合理区间，防脏数据
                        _pos_now = self._beat_pos(snap)
                        _sec_beat = _pos_now + _dsec * _bpm / 60.0
                        # 上限：段落再长也别超过 MAX 拍不换素材
                        _cap = _pos_now + float(self.AUTO_SWITCH_MAX_BEATS)
                        _capped = _sec_beat > _cap
                        if _capped:
                            _sec_beat = _cap
                        # ★ 2026-11-06：记下"这一刀是段落边界还是封顶强制切"。
                        #   段落边界只能由 `_section_tick` 执行（它要换池），拍位路径再切一次
                        #   就是用户报的"连切两个素材"。
                        self._due_cap = bool(_capped)
            except Exception:                                    # noqa: BLE001
                _sec_beat = None
        if self._next_due is None:
            if _sec_beat is not None:
                # 段落模式：目标就是段落起点（不贴 16/8 拍栅格 —— 段落边界本身就在八拍头上）
                self._next_due = _sec_beat
                self._due_off = off
                self._due_kind = "section"
                self._due_cap = bool(getattr(self, "_due_cap", False))
                return self._next_due
            self._due_cap = False
            target = self.switch_pos + iv
            near = off + math.floor((target - off) / w + 0.5) * w
            # 只有明显偏移才拉正（见 ALIGN_MIN_OFFSET）：小偏差原样保留，不打断节奏
            self._next_due = near if abs(near - target) >= self.ALIGN_MIN_OFFSET else target
            self._due_off = off
            self._due_kind = "beat"
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

    def _layers_for_global_schedule(self):
        """归"全局排期"的层。

        ⚠ 没有任何屏带 `mode`/`auto` override 时 **必须返回 `self.layers` 这个对象本身**
          （不是副本、不过滤）—— 这是"主屏逐字等价"的结构性保证。
        """
        try:
            plan = self._out_cfg
            if not plan:
                return self.layers
            ov_sids = self._override_screen_ids()
            if not ov_sids:
                return self.layers                    # ← 零回归的关键一行
            keep = []
            for lay in self.layers:
                sc = None
                try:
                    sc = self._layer_screen(lay)
                except Exception:                                  # noqa: BLE001
                    sc = None
                sid = str((sc or {}).get("id") or "")
                if sid and sid in ov_sids:
                    continue                          # 该层归它自己那块屏排期
                keep.append(lay)
            return keep
        except Exception:                                          # noqa: BLE001
            return self.layers

    def _override_screen_ids(self):
        """带 `mode`/`auto` override 的屏 id 集合（split/copy 不算：它们没有自己的素材流）。"""
        out = set()
        try:
            for sc in (self._out_cfg or {}).get("screens") or []:
                if str(sc.get("type")) not in ("main", "independent"):
                    continue
                p_ = sc.get("params") or {}
                if p_.get("mode") or p_.get("auto"):
                    out.add(str(sc.get("id") or ""))
        except Exception:                                          # noqa: BLE001
            pass
        return out

    def _auto_switch_tick(self, snap):
        """★ BUG-D 波2（旁路设计，主路径零改动）：
        主体逻辑**原样**跑在 `_layers_for_global_schedule()` 选出的层上 ——
        没有任何屏带 override 时它就是 `self.layers`（**同一对象**）⇒ 逐字等价；
        带 override 的屏由 `_auto_switch_tick_overrides()` 在函数末尾单独排期。
        """
        # getattr 兜底：老的单测 Stub 只实现了部分方法，缺这个方法时退回 self.layers
        # （真实引擎上永远走前者；无 override 时它返回的就是 self.layers 本身）
        _sel = getattr(self, "_layers_for_global_schedule", None)
        layers = _sel() if callable(_sel) else self.layers
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
        # ★★★ 2026-11-06（用户：「切换段落的时候会连着切两个素材」）：
        #   段落边界的目标点是由 `_next_switch_due` 算出来的（= 下一段起点），但真正的执行
        #   在 `_section_tick`（同一帧稍后几行）—— 它会**换池**（按新档位挑素材）+ 切层 +
        #   重置排期。原来这里也会照 due 切一刀（旧池），于是同一秒切两次：
        #   先按旧池切一个，紧接着段落那边换池再切一个，用户看到的就是"切一个、半拍后又切一个"。
        #   ⇒ 段落边界（非封顶）**只让 `_section_tick` 切**：这里放弃本次 due 并清排期。
        #   ⚠ 只在"段落表确实接管着"时让路（`_sec_energy is not None`）——否则段落层没认到歌，
        #     让路就会变成"再也不切"。封顶（`_due_cap`，段落太长强制切）照旧在这里切。
        # ⚠ 判"到点"必须和下面真正切换用**同一个口径**（含提前量）：
        #   上一版这里写的是 `pos >= due`，而切换那行是 `pos + 提前量 >= due`
        #   ⇒ 提前量生效的那一拍正好落在"守卫不成立、切换成立"的缝里，
        #   拍位路径先切一刀（旧池）、一拍后段落路径再切一刀 ⇒ 用户报的"连跳两次画面"。
        _reached = pos >= due
        # ★★★ 2026-11-06 21:xx（用户第二次报「两个段落之间多一拍其他画面」）：
        #   日志里每次段落边界前 1 秒都有一条「拍位切换」（残差 +0.42~+1.52 拍），
        #   紧接着才是「段落切换」⇒ 还是同一处两刀。
        #   根因：`_next_switch_due` 只有在 `_dsec` 落在 [1.0, 120.0] 秒时才把目标设成
        #   段落起点（kind=section）；否则退回按拍排期（kind=beat）—— 而那条拍线正好
        #   落在段落边界前 1 秒 ⇒ 上一版的"kind==section 才让路"根本挡不住它。
        #   ⇒ 只要**段落表在接管**（`_sec_energy is not None`），拍位路径一律让路，
        #     只有"段落太长被封顶"（`_due_cap`）那一刀仍在这里切。
        #     这样两段之间绝不会插进第三个画面。
        if (_reached and not getattr(self, "_due_cap", False)
                and getattr(self, "_sec_energy", None) is not None):
            self.switch_pos = pos
            self._next_due = None
            return
        # ★★★ 2026-11-06（用户：「现在所有素材的切换时间都比真实听歌慢了一拍，
        #   给它默认加一拍上去」）：
        #   按拍切换实际落在听到的那一刻之后约一拍（听感与鼠标/画面之间差一个八拍内的拍）。
        #   做法：判断"到了"时给当前拍位**加 `SWITCH_ADVANCE_BEATS` 拍**（默认 1.0），
        #   等价于把切换点整体提前一拍。
        #   ⚠ 只动"按拍排期"这一条路；段落边界（`_section_tick`）是预置事件、
        #     实测迟到 0.01~0.03s，不在这里加，否则会抢到段前。
        if _reached:
            self.switch_pos = due
            self._next_due = None   # 切换后按最新模式/间隔重新安排下一次
            # ★★ 2026-10-30 用户报「好几首歌切素材和八拍线差四拍」：把**每次按拍切换**
            #   的对齐残差记进 `cue_health.log`（每 ≥10 秒一条，避免渲染线程做 I/O 抖动）。
            #   判读：
            #     栅格=8 残差≈0      ⇒ 对齐正常（切在八拍头上）；
            #     栅格=8 残差≈±4     ⇒ 相位差半句（离线 `phrase` 判反 / 存在 grid_flip）；
            #     栅格=4             ⇒ **根本没进网格模式**（没认出歌 / 票数不够没锁定
            #                          / 这首歌没网格）⇒ 切在小节线上，听感正是"差 4 拍"。
            #   三种情况以前只能靠耳朵猜，现在一行字就能分清。
            try:
                _now = time.perf_counter()
                if _now - float(getattr(self, "_bt_log_t", 0.0)) >= 10.0:
                    self._bt_log_t = _now
                    _w = float(self._align_width(snap))
                    _off = float(self._bar_off(snap))
                    _res = (pos - _off) % _w
                    if _res > _w / 2.0:
                        _res -= _w
                    self._section_log(
                        "拍位切换 栅格=%d拍 残差=%+.2f拍 模式=%s sid=%s"
                        % (int(round(_w)), _res, self.mode,
                           snap.get("recognized_song_id", -1)))
            except Exception:                                     # noqa: BLE001
                pass
            meta = MODE_META[self.mode]
            # 过渡时长：区间内微调（高能/快切时更短）
            TR = max(-1.0, min(1.0, snap["energy_trend"]))
            sens = int(self.cfg["mode"].get("sensitivity", 1))
            E = max(0.0, min(1.0, self._energy_for_switch(snap)))
            f = E + TR * 0.15 + (-0.1, 0.0, 0.1)[max(0, min(2, sens))]
            f = max(0.0, min(1.0, f))
            trans_ms = int(meta["trans_ms"] * (1.3 - f * 0.3))  # 区间内微调过渡时长
            for lay in layers:
                if lay.fixed >= 0 or not lay.clips or self.locked:
                    continue
                if getattr(lay, "is_kv", False):
                    continue    # Kv 待机层不参与按拍自动切换
                # ★ 2026-11-26（用户）：手动图层（auto_mode=False）只手动切，
                #   不再跟着自动节奏走。
                if not getattr(lay, "auto_mode", False):
                    continue
                if getattr(lay, "is_cuesheet", False):
                    continue
                if self.beat_owns(lay):
                    continue    # drop 段由逐拍交替接管
                lay.trans_ms = trans_ms
                self._switch_layer_random(lay, snap)
        # ★ BUG-D 波2：带 override 的屏各自排期（旁路；无 override 时本函数直接 return）
        try:
            self._auto_switch_tick_overrides(snap)
        except Exception:                                          # noqa: BLE001
            pass

    def _candidate_indices(self, lay, snap):
        """当前可切换的候选素材索引（不改动任何状态，供切换与"下一个素材"预估共用）"""
        _a = self._auto_for(lay)          # ★ U7：按该层所属屏的 auto 参数
        cooldown = float(_a.get("cooldown_sec", 20))
        now = time.perf_counter()
        idxs = list(range(len(lay.clips)))
        prefer = []
        fallback = None   # 缓和段二级兜底：避开高动态/频闪（用中动态/未标动态兜底）
        if _a.get("energy_map", True):
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
            # ★ 用目标能量判偏好：否则段落选了 drop、候选却还按实测低能量挑低动态素材
            _E_sel = self._energy_for_switch(snap)
            if _E_sel >= 0.80 and hi:
                prefer = hi                    # 高潮段：优先高动态/频闪
            elif _E_sel < 0.40:
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
                # ★ 2026-10-30 用户：「再把中动态也加进来 0.4到0.8」——
                #   中能量段**优先中动态素材**，没有中动态才退回"排除频闪"的全池。
                #   频闪仍只在高潮段（≥0.80）出现。
                prefer = ([i for i in mid if i not in flash]
                          or [i for i in idxs if i not in flash])
        cands = prefer or idxs
        cands = [i for i in cands if i != lay.cur
                 and (lay.clips[i].path not in lay.recent or now - lay.last_switch_t > cooldown)]
        if not cands and fallback:
            cands = [i for i in fallback if i != lay.cur
                     and (lay.clips[i].path not in lay.recent or now - lay.last_switch_t > cooldown)]
        if not cands:
            cands = [i for i in idxs if i != lay.cur]
        return cands

    @_locked
    def next_clip_for(self, lay):
        """预估下一个将播放的素材（供预览区"下一个素材预看"，不消耗随机状态）"""
        if not lay or not lay.clips:
            return None
        if lay.fixed >= 0 and 0 <= lay.fixed < len(lay.clips):
            return lay.clips[lay.fixed]
        if self.beat_owns(lay) and lay.pair and len(lay.clips) >= 2:
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
        """每帧检查一次「到点该预热了」（逐拍交替的对子预热在 _beat_show 里直接做）"""
        if self.locked:
            return
        now = time.perf_counter()
        for lay in self.layers:
            if not lay.warm_pend or now < lay.warm_at:
                continue
            lay.warm_pend = False
            if lay.fixed >= 0 or self.locked or getattr(lay, "is_kv", False):
                continue
            if self.beat_owns(lay):
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

    def _heal_stalled(self, lay):
        """当前播放器超过 `media_manager._STALL_SECS` 没有新帧 ⇒ 重建解码器。

        为什么要这个兜底（用户报「素材突然卡在某一帧，直到下一个切进来才恢复」）：
          播放器对象**还活着**、`current()` 一直返回最后一帧图像，引擎看不出异常，
          于是一直显示那张冻住的图。有了产帧计数 + 时间戳判据（`_DecoderHealth`），
          这里就能主动发现并重建 —— 即使将来还有未知的"解码线程死掉"路径，
          表现也只会是**短暂一卡**，而不再是永久冻帧。

        ⚠ 三处不能误杀：
          ① 图层不可见；
          ② 播放器不在活跃状态（静音暂停 / 预热挂起本来就该不出帧）；
          ③ **从未出过帧**（`_frames == 0`）的对象根本不是"冻在某一帧"。

        ⚠⚠ 2026-10-01 现场卡死事故（本函数失控是根因）：
          旧实现只要 `stalled()` 为真就**每个 tick 都同步重建**（在持有 `_tick_lock`
          的渲染线程里开容器 + 探测 alpha/缩略图 ⇒ 持续 I/O 与线程创建风暴）。而旧判据
          在 `_last_t == 0`（从未出帧）时只要对象创建超过 `_STALL_SECS*4`(=6s) 就返回真 ——
          于是"一直解不出首帧"的素材被**无限重建**：现场日志 90 秒里重建 70+ 次、
          同一个素材反复复现 ⇒ tick 速率崩塌 ⇒ 自动切换停摆、音频/界面饿死 ⇒ 卡死。
          现在两道保险：
            · 判据：只对**出过帧**（`_frames > 0`）的对象判卡帧；从未出帧的交给
              `_ended` + `_get_player` 的健康校验处理；
            · 熔断式硬限流：每 (图层, 素材) 冷却 `_HEAL_CLIP_COOLDOWN` 秒、同素材累计
              `_HEAL_MAX_PER_CLIP` 次后彻底放弃（换素材重置）、全局每
              `_HEAL_GLOBAL_INTERVAL` 秒最多重建 1 次、`running` 为假时绝不自动重建；
            · 健康素材**检查**节流：每图层最多每 `_HEAL_CHECK_INTERVAL` 秒调一次
              `stalled()`（QA 实测：旧实现健康素材每 tick 都调，20Hz 下 30s=600 次）；
            · 廉价健康快路：最近 `_STALL_SECS` 内还产过帧的直接放过（连 `stalled()`
              都不调）⇒ 真实健康素材热路径调用次数 = 0。
        """
        p = lay.player
        if p is None or not hasattr(p, "stalled"):
            return
        # ★ 判据修正：从未出过帧 ⇔ 不是"冻在某帧"。这类对象交给 `_ended` / `_get_player`
        #   健康校验处理，热路径**不**为它做任何事（省掉 stalled() 调用与后续重建 I/O）。
        if getattr(p, "_frames", 0) <= 0:
            return
        # 预览/停止态（未点「开始」）不做自动重建：避免空跑时I/O抖动。
        if not self.running:
            return
        now = time.perf_counter()
        # ★ 廉价健康快路：走到这里必然出过帧（上面已排除 `_frames<=0`），若最近
        #   `_STALL_SECS` 内还产过帧（`_mark_frame()` 会更新 `_last_t`）⇒ 明显健康，
        #   直接放过，**连 stalled() 都不用调**（真实健康素材热路径调用次数 = 0）。
        last = getattr(p, "_last_t", 0.0)
        if last and (now - last) <= _STALL_SECS:
            return
        # ★ 健康素材的检查节流（QA 实测偏差）：冷却闸门只在"重建过"后才闸得住，
        #   而健康素材从不重建 ⇒ `_heal_last_t` 恒为 0 ⇒ 每 tick 都会走到 stalled()。
        #   这里每图层最多每 `_HEAL_CHECK_INTERVAL` 秒检查一次（20Hz ⇒ ≈2Hz）。
        if now < float(getattr(lay, "_heal_next_t", 0.0) or 0.0):
            return
        lay._heal_next_t = now + _HEAL_CHECK_INTERVAL
        if not getattr(lay, "visible", True):
            return
        if not getattr(p, "_active", True):
            return
        if not (0 <= lay.cur < len(lay.clips)):
            return
        clip = lay.clips[lay.cur]
        path = getattr(clip, "path", "")
        # 换素材 ⇒ 重置该图层的冷却与计数（计数语义是"同一素材累计"）。
        # ⚠ 不重置 `_heal_next_t`：它只限检查频率、不是冷却，重置只会让换素材瞬间多放行一次。
        if getattr(lay, "_heal_path", None) != path:
            lay._heal_path = path
            lay._heal_last_t = 0.0
            lay._heal_count = 0
            lay._heal_gave_up = False
        # 已封顶：不再重建、也不再调用任何判据（放弃日志只写一次，见下面置位处）。
        if getattr(lay, "_heal_gave_up", False):
            return
        # —— 硬限流（全部是时间戳/整数比较：无 dict、无 I/O、无锁）——
        if now - getattr(lay, "_heal_last_t", 0.0) < _HEAL_CLIP_COOLDOWN:
            return
        if now - getattr(self, "_heal_last_global_t", 0.0) < _HEAL_GLOBAL_INTERVAL:
            return
        # 到这里才调用判据（相对贵；health() 只在真要重建时才调、且只调一次）。
        try:
            if not p.stalled(_STALL_SECS):
                return
        except Exception:                                      # noqa: BLE001
            return
        # health() 只调一次，同时取 frames/age/reason 供日志诊断（下次现场取证用）：
        # 现场日志能一眼区分"从未出帧"（新判据下不该出现）与"出过帧后卡住"。
        h = {}
        try:
            h = p.health() or {}
        except Exception:                                      # noqa: BLE001
            h = {}
        frames = h.get("frames", getattr(p, "_frames", 0))
        age = h.get("age")
        active_age = h.get("active_age")
        reason = h.get("reason") or ""
        if age is None:
            detail = "frames=%s, 无产帧时间戳" % frames
        else:
            detail = "frames=%s, %.1fs 无新帧" % (frames, age)
        # 距上次"变活跃"的时间：用来识别"刚上场就被砸"的误杀（active_age 很小）
        if active_age is not None:
            detail += ", active=%.1fs" % active_age
        if reason:
            detail += "；" + reason
        # —— 通过全部限流：执行重建，并记账 ——
        lay._heal_last_t = now
        lay._heal_count = getattr(lay, "_heal_count", 0) + 1
        self._heal_last_global_t = now
        try:
            p.close()
        except Exception:                                      # noqa: BLE001
            pass
        try:
            lay.cache.pop(path, None)           # ★ 关键：别把僵尸留在缓存里
        except Exception:                                      # noqa: BLE001
            pass
        try:
            lay.player = self._get_player(lay, clip)
        except Exception:                                      # noqa: BLE001
            lay.player = None
        capped = lay._heal_count >= _HEAL_MAX_PER_CLIP
        try:
            import stallwatch
            stallwatch.log_line("!! 卡帧看门狗：重建解码器 %s（第 %d/%d 次；%s）"
                                % (os.path.basename(path), lay._heal_count,
                                   _HEAL_MAX_PER_CLIP, detail))
        except Exception:                                      # noqa: BLE001
            pass
        if capped:
            # 到顶：只再写一条"放弃"日志，之后本素材不再自动重建（换素材重置计数）。
            lay._heal_gave_up = True
            try:
                import stallwatch
                stallwatch.log_line("!! 卡帧看门狗：放弃重建 %s（累计 %d 次，换素材前不再重建，"
                                    "避免重建风暴卡死）"
                                    % (os.path.basename(path), lay._heal_count))
            except Exception:                                  # noqa: BLE001
                pass

    def _heal_silent(self, lay, snap=None):
        """静默冻帧兜底：处理 `_heal_stalled` 看不见、且**没有任何其它恢复者**的几类故障。

        为什么需要它（2026-10-01 用户实测"播放中仍卡在某一帧、且 ui_stall.log 10 分钟
        一条都没有"）：`_heal_stalled` 顶部两条提前返回把下面两类直接放过去了，而引擎里
        没有别的地方负责恢复它们：
          · `lay.player is None`（自动匹配置换池时池为空被清空，见 `_refresh_auto_layers`）；
          · `lay.player` 存在但**长期不活跃**（不在缓存 ⇒ 活跃标记漏掉它）⇒ 永不解码 ⇒ 冻帧。
        这里只做"恢复 + 取证"，**不重建**已经能自愈的情况，避免回到重建风暴。

        热路径廉价：不需要动作的那一趟只做时间戳/属性比较，绝不调用 `stalled()`/`health()`。
        `snap`：音频快照（供"无帧封顶后跳到下一个素材"复用 `_candidate_indices`）。
        """
        if not self.running:
            return
        if not getattr(lay, "visible", True):
            return
        if not (0 <= lay.cur < len(lay.clips)):
            return
        # ★★ 2026-11-17（用户：切歌时视频输出卡顿掉帧十几秒）：
        #   `hide_silent` 图层在静音时、`opacity=0` 的图层，合成端**故意**不画，
        #   上面的活跃循环也**故意**挂起它们的解码器。但看门狗把"不活跃"当故障，
        #   每 1~2s 又强行激活 ⇒ 下一帧再挂起 ⇒ **反复开/停解码线程 + 读盘**，
        #   切歌/试听那段十几秒就一直抖。这两类"不活跃"是设计行为，直接放过。
        try:
            if (bool(getattr(lay, "hide_silent", False))
                    and bool(getattr(self, "_silent_now_debounced", False))):
                lay._silent_inactive_since = 0.0
                lay._silent_noframe_since = 0.0
                return
            if float(getattr(lay, "opacity", 1.0) or 0.0) <= 0.002:
                lay._silent_inactive_since = 0.0
                lay._silent_noframe_since = 0.0
                return
        except (TypeError, ValueError):
            pass
        now = time.perf_counter()
        p = lay.player

        # ---- 1) 当前没有播放器（自动池被清空 / 重建失败）⇒ 宽限后兜底重建 ----
        if p is None:
            lay._silent_inactive_since = 0.0
            since = getattr(lay, "_silent_none_since", 0.0)
            if not since:
                lay._silent_none_since = now
                return
            if now - since <= _SILENT_GRACE:
                return
            if now - getattr(lay, "_silent_last_action_t", 0.0) < _SILENT_ACTION_MIN:
                return
            clip = lay.clips[lay.cur]
            try:
                lay.player = self._get_player(lay, clip)
            except Exception:                                  # noqa: BLE001
                lay.player = None
            lay._silent_none_since = 0.0
            lay._silent_last_action_t = now
            self._silent_log(lay, "player缺失",
                             "兜底重建 %s（player 缺失超过 %.1fs；此前只有下一个切换点才会重建）"
                             % (os.path.basename(getattr(clip, "path", "")), _SILENT_GRACE))
            return
        lay._silent_none_since = 0.0

        # 只对**真解码器**做静默兜底：图片 / GIF 播放器的 `set_active` 是空操作、也没有
        # `_active`/`_frames`（静态素材本就"不活跃"且永远不会冻帧），若把它们当"未激活"
        # 反复救+记日志，会给图片图层刷出无意义日志。判据同 `_heal_stalled`：有 `stalled`。
        if not hasattr(p, "stalled"):
            lay._silent_inactive_since = 0.0
            return

        # ---- 2) 从未出帧且解码已结束（素材解不出来）：**不重建**（避免风暴），每 5s 记一行 ----
        if getattr(p, "_frames", 0) <= 0 and getattr(p, "_ended", False):
            lay._silent_inactive_since = 0.0
            lay._silent_noframe_since = 0.0
            if now - getattr(lay, "_silent_ended_last_t", 0.0) >= _SILENT_ENDED_EVERY:
                lay._silent_ended_last_t = now
                self._silent_log(lay, "ended无帧",
                                 "从未出帧且解码已结束（%s）—— 该类不重建，仅持续记录"
                                 % os.path.basename(getattr(lay.clips[lay.cur], "path", "")))
            return

        # ---- 2.5) 可见 + 当前素材的真解码器**长时间 `_frames==0`**（解不出首帧）⇒ 兜底重建 ----
        #   现场证据（2026-10-02）：layer_health.log 里可见当前图层连续 8s `frames=0 age=-`、
        #   画布冻住；而 `_heal_stalled` 因"从未出帧"提前返回、上面第 2 类又要求 `_ended`
        #   ⇒ **无人恢复**。这里补一条恢复路径，并**限流**（每图层 ≥`_SILENT_NOFRAME_COOLDOWN`
        #   秒一次、同素材累计 ≤`_SILENT_NOFRAME_MAX` 次；到顶只记一行日志、不再重建）。
        if getattr(p, "_frames", 0) <= 0:
            # `pause_silent` 的"静音挂起"是**有意**不出帧（且不活跃），绝不误重建它
            if getattr(lay, "pause_silent", False) and not getattr(p, "_active", False):
                return
            since = getattr(lay, "_silent_noframe_since", 0.0)
            if not since:
                lay._silent_noframe_since = now
                return
            # ★ 宽限按素材体量自适应：大素材冷打开 + 首帧本来就慢，用 2.2s 会误判
            #   ⇒ 重建 ⇒ 重新读盘 ⇒ 更卡（见 `_NOFRAME_GRACE_BIG` 注释）。
            _gp = getattr(lay.clips[lay.cur], "path", "") if lay.clips else ""
            if getattr(lay, "_silent_grace_path", None) != _gp:
                lay._silent_grace_path = _gp
                try:
                    _big = os.path.getsize(_gp) > _NOFRAME_BIG_BYTES
                except OSError:
                    _big = False
                lay._silent_grace = _NOFRAME_GRACE_BIG if _big else _NOFRAME_GRACE
            if now - since <= float(getattr(lay, "_silent_grace", _NOFRAME_GRACE)):
                return
            clip = lay.clips[lay.cur]
            path = getattr(clip, "path", "")
            # 换素材 ⇒ 重置该素材的"无帧重建"计数（计数语义是"同一素材累计"）
            if getattr(lay, "_silent_noframe_path", None) != path:
                lay._silent_noframe_path = path
                lay._silent_noframe_count = 0
                lay._silent_noframe_last_t = 0.0
            # 图层级冷却（≥ `_SILENT_NOFRAME_COOLDOWN` 秒）—— 防同一图层连续重建风暴
            if now - getattr(lay, "_silent_noframe_last_t", 0.0) < _SILENT_NOFRAME_COOLDOWN:
                return
            # 素材级上限（≤ `_SILENT_NOFRAME_MAX` 次）—— 到顶：不再重建这一帧，
            # ★ 改为**尝试跳到该图层下一个候选素材**（避免整层一直停在这一帧）。
            if getattr(lay, "_silent_noframe_count", 0) >= _SILENT_NOFRAME_MAX:
                switched = self._skip_no_frame_material(lay, now, since, snap)
                if (not switched
                        and now - getattr(lay, "_silent_noframe_capped_log_t", 0.0) >= _SILENT_ENDED_EVERY):
                    lay._silent_noframe_capped_log_t = now
                    self._silent_log(lay, "无帧封顶",
                                     "解不出首帧已 %.1fs、重建 %d 次达上限 —— 暂不再重建"
                                     "（切走 / 换素材后自动重新计数）"
                                     % (now - since, lay._silent_noframe_count))
                return
            # —— 执行重建（与 `_heal_stalled` 同款：先 close、再从缓存摘除、再重取）——
            lay._silent_noframe_last_t = now
            lay._silent_noframe_count = getattr(lay, "_silent_noframe_count", 0) + 1
            try:
                p.close()
            except Exception:                                  # noqa: BLE001
                pass
            try:
                lay.cache.pop(path, None)          # 别把"解不出首帧"的僵尸留在缓存里
            except Exception:                                  # noqa: BLE001
                pass
            try:
                lay.player = self._get_player(lay, clip)
            except Exception:                                  # noqa: BLE001
                lay.player = None
            lay._silent_noframe_since = 0.0
            self._silent_log(lay, "无帧重建",
                             "解不出首帧已 %.1fs，重建解码器 %s（第 %d/%d 次）"
                             % (now - since, os.path.basename(path),
                                lay._silent_noframe_count, _SILENT_NOFRAME_MAX))
            return
        lay._silent_noframe_since = 0.0

        # ---- 3) 有播放器但长期不活跃（活跃标记漏掉了它）⇒ 补进缓存 + 激活，**不重建** ----
        if not getattr(p, "_active", False):
            # `pause_silent` 的"静音挂起"是**有意**不活跃，绝不能强激活它
            # （不可见图层在上面 `visible` 早返回时就已经放过了，这里不用再判）。
            if getattr(lay, "pause_silent", False):
                return
            since = getattr(lay, "_silent_inactive_since", 0.0)
            if not since:
                lay._silent_inactive_since = now
                return
            if now - since <= _SILENT_GRACE:
                return
            if now - getattr(lay, "_silent_last_action_t", 0.0) < _SILENT_ACTION_MIN:
                return
            path = getattr(lay.clips[lay.cur], "path", "")
            if lay.cache.get(path) is not p:
                lay.cache[path] = p          # 补回缓存：让它今后被 LRU/切换正常复用
            try:
                p.set_active(True)
            except Exception:                                  # noqa: BLE001
                pass
            lay._silent_inactive_since = 0.0
            lay._silent_last_action_t = now
            self._silent_log(lay, "未激活", "补进缓存并激活 %s（不活跃超过 %.1fs）"
                             % (os.path.basename(path), _SILENT_GRACE))
            return
        lay._silent_inactive_since = 0.0

    def _skip_no_frame_material(self, lay, now, since, snap):
        """无帧重建封顶后：**自动跳到该图层下一个候选素材**（避免整层一直停在这一帧）。

        返回 True = 已切换；False = 本次没跳（单素材层 / 过渡中 / 无候选 / 未运行）。

        复用引擎既有切换机制（`_candidate_indices` + `_switch_layer_to`），**不自造**。
        安全约束：
          · `running` 为假 ⇒ 绝不动作；
          · 过渡进行中（`trans_active`）⇒ 不跳（下个 tick 再试），**绝不打断过渡**；
          · 单素材层无可跳 ⇒ 只记日志（`_SILENT_ENDED_EVERY` 去重）；
          · 被跳过的坏素材**临时**加入 `lay.recent`（**不永久拉黑** —— 用户可能只是暂时读不到文件）；
          · 用正常过渡切换（非 hard-cut），与自动切换行为一致。
        """
        if not self.running:
            return False
        if getattr(lay, "trans_active", False):
            return False                     # 过渡进行中：不安全，下个 tick 再试
        if len(lay.clips) < 2:
            if now - getattr(lay, "_silent_skip_log_t", 0.0) >= _SILENT_ENDED_EVERY:
                lay._silent_skip_log_t = now
                self._silent_log(lay, "无帧封顶",
                                 "解不出首帧已 %.1fs，但图层仅 1 个素材，无法跳过（仅记录）"
                                 % (now - since))
            return False
        try:
            cands = self._candidate_indices(lay, snap or {"energy": 0.55})
        except Exception:                                      # noqa: BLE001
            cands = []
        cands = [i for i in cands if i != lay.cur]
        if not cands:                        # 兜底：候选被 recent/cooldown 过滤空时，任取一个非当前
            cands = [i for i in range(len(lay.clips)) if i != lay.cur]
        if not cands:
            return False
        bad_path = getattr(lay.clips[lay.cur], "path", "")
        nxt = cands[0]
        try:
            if bad_path and bad_path not in lay.recent:
                lay.recent.append(bad_path)      # 暂时避选（换素材后会自然滑出 recent）
                if len(lay.recent) > max(3, len(lay.clips) // 2):
                    lay.recent.pop(0)
        except Exception:                                      # noqa: BLE001
            pass
        try:
            self._switch_layer_to(lay, nxt)      # 正常过渡切换（`_tick_lock` 可重入）
        except Exception:                                      # noqa: BLE001
            return False
        self._silent_log(lay, "无帧跳过",
                         "解不出首帧已 %.1fs、重建封顶 ⇒ 跳到下一个素材 %s（跳过 %s）"
                         % (now - since,
                            os.path.basename(getattr(lay.clips[nxt], "path", "")),
                            os.path.basename(bad_path)))
        return True

    def _silent_log(self, lay, reason, detail):
        """静默冻帧兜底日志：按 (图层, 原因) 去重（`_SILENT_ACTION_MIN` 秒内只记一次）。"""
        key = (getattr(lay, "name", "?"), reason)
        now = time.perf_counter()
        if now - self._silent_log_t.get(key, 0.0) < _SILENT_ACTION_MIN:
            return
        self._silent_log_t[key] = now
        try:
            import stallwatch
            stallwatch.log_line("!! 静默冻帧兜底：图层「%s」%s：%s" % (key[0], reason, detail))
        except Exception:                                      # noqa: BLE001
            pass

    def _health_tick(self):
        """每秒给 `layer_health.log` 写一行「每层健康快照」。**只在 `self.running` 时**执行。

        为什么做这个：看门狗（`_heal_stalled`）只在"判定卡帧"时才写 `ui_stall.log`，判据没
        命中就永远沉默 —— 用户实测"画面卡住而 47 分钟一条日志都没有"。这里改成**常态留痕**：
        只要在跑，每秒把每层的 player / 缓存 / 活跃 / 帧数 / 帧龄 / ended 落一行，卡了直接回看。

        热路径廉价：一次 `perf_counter` 比较 + 每层几个只读 `getattr`，远低于每帧 `stalled()`。
        """
        if not self.running:
            return
        now = time.perf_counter()
        if now < self._health_next_t:
            return
        self._health_next_t = now + _HEALTH_LOG_INTERVAL
        try:
            log = self._health_log
            if log is None:
                log = _LayerHealthLog()
                self._health_log = log
            try:
                line = self._health_snapshot(now)
            except Exception as _e:                                # noqa: BLE001
                # 以前整行异常被外层吞掉 ⇒ layer_health.log 一直是 0 字节，
                # 现场失去唯一的每秒状态留痕。现在至少写一行错误原因。
                line = "health snapshot error: %s: %s" % (type(_e).__name__, _e)
            log.write("[%s] %s" % (time.strftime("%H:%M:%S"), line))
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self._stutter_watch(now)
        except Exception:                                          # noqa: BLE001
            pass

    def _stutter_watch(self, now):
        """图层停帧取证：单层或同时多层 ⇒ 记一行（判定见 `_STUTTER_AGE` 常量注释）。

        只统计**可见且活跃**的层：不可见图层有意不解码（`_tick_locked`），
        它的帧龄天然会涨，算进来就会天天误报。
        """
        if now - getattr(self, "_stutter_log_t", 0.0) < _STUTTER_LOG_EVERY:
            return
        hits = []
        for lay in self.layers:
            if not (bool(getattr(lay, "visible", True))
                    or bool(getattr(lay, "trans_active", False))):
                continue
            p = getattr(lay, "player", None)
            if p is None or not hasattr(p, "_last_t"):
                continue
            if not getattr(p, "_active", False):
                continue
            last = float(getattr(p, "_last_t", 0.0) or 0.0)
            # ★★ 2026-11-08（日志误导排查）：判据必须与 `_DecoderHealth.stalled()`
            #   一致，用 `max(最后产帧, 本次变活跃)` 作基准。
            #   复用缓存播放器时，`_last_t` 可能停在**上一次被切走**的时刻（例如
            #   16 拍前 = 6.4s），刚重新激活还没出首帧就会被旧写法报成
            #   "age=6.4s 停帧"——日志里那一串 6.4s 大多是这种假阳性，不是真卡了 6.4s。
            active_since = float(getattr(p, "_active_since", 0.0) or 0.0)
            base = last if last > active_since else active_since
            if not base:
                continue
            age = now - base
            if age < _STUTTER_AGE:
                continue
            nm = ""
            try:
                nm = os.path.basename(getattr(lay.clips[lay.cur], "path", ""))
            except Exception:                                      # noqa: BLE001
                nm = ""
            hits.append("「%s」age=%.2fs(active=%.2fs raw=%.2fs) frames=%d %s"
                        % (getattr(lay, "name", "?"), age,
                           (now - active_since) if active_since else -1.0,
                           (now - last) if last else -1.0,
                           int(getattr(p, "_frames", 0) or 0), nm))
        if not hits:
            return
        self._stutter_log_t = now
        try:
            import stallwatch
            label = ("多层同时停帧（%d 层）" % len(hits)) if len(hits) >= 2 else "图层停帧"
            stallwatch.log_line("!! %s：%s" % (label, " | ".join(hits)))
        except Exception:                                          # noqa: BLE001
            pass

    def _gpu_token(self):
        """返回 `gpu=` 用的记号（'1' / '0' / '?'），并在**状态变化**时写一条醒目日志。

        为什么放在快照里：用户报"预览 + 输出 + NDI 三者同时冻住"时，`ui_stall.log` 可能
        一条没有；而 `gpu=0` 能立刻区分"是不是整场退回了软件解码（慢 → 首帧出不来 → 冻帧）"。
        记号：`1`=可用；`0`=工作线程未就绪/已停用（含冷启动中）；`?`=glctx 不可导入。
        变化时附带 `GLWorker.last_message()`，一眼看出原因（冷启动中 / 驱动挂死 / 崩溃…）。
        """
        try:
            import glctx
            wk = glctx.GLWorker.get()
        except Exception:                                          # noqa: BLE001
            tok, msg = "?", ""
        else:
            try:
                ok = bool(wk.available())
            except Exception:                                      # noqa: BLE001
                ok = False
            tok = "1" if ok else "0"
            try:
                msg = "" if ok else (wk.last_message() or "")
            except Exception:                                      # noqa: BLE001
                msg = ""
        if tok != self._gpu_state:
            self._gpu_state = tok
            try:
                import stallwatch
                stallwatch.log_line("!! GPU 解码状态变化：gpu=%s%s"
                                    % (tok, ("（%s）" % msg) if msg else ""))
            except Exception:                                      # noqa: BLE001
                pass
        return tok

    def _health_snapshot(self, now):
        """拼一行「每层健康快照」，形如：

        `gpu=1 | L0 Kv   vis=1 cur=0/1   p=id1234 incache=1 active=1 frames=4821 age=0.03 ended=0`

        只读属性、不打印绝对路径（隐私 + 行长）；层名截断 12 字。
        图片 / GIF 播放器没有 `_active`/`_frames`（"不活跃/0 帧"是正常的），标成 `p=img`
        以免下次回看时把它们误读成"从未出帧的不活跃播放器"。
        行首 `gpu=`（1/0/?）：全局 GPU 可用性，状态变化时另写一条醒目日志（见 `_gpu_token`）。"""
        segs = ["gpu=" + self._gpu_token()]
        for i, lay in enumerate(self.layers):
            name = str(getattr(lay, "name", "?"))[:12]
            vis = 1 if getattr(lay, "visible", True) else 0
            clips = getattr(lay, "clips", None) or []
            nclips = len(clips)
            cur = getattr(lay, "cur", -1)
            in_cache = 0
            if 0 <= cur < nclips:
                try:
                    in_cache = 1 if lay.cache.get(clips[cur].path) is lay.player else 0
                except Exception:                                  # noqa: BLE001
                    in_cache = 0
            p = getattr(lay, "player", None)
            if p is None:
                segs.append("L%d %s vis=%d cur=%d/%d p=none"
                            % (i, name, vis, cur, nclips))
                continue
            if not hasattr(p, "stalled"):     # 图片 / GIF：静态素材，无 _active/_frames
                segs.append("L%d %s vis=%d cur=%d/%d p=img incache=%d"
                            % (i, name, vis, cur, nclips, in_cache))
                continue
            last = float(getattr(p, "_last_t", 0.0) or 0.0)
            active_since = float(getattr(p, "_active_since", 0.0) or 0.0)
            base = last if last > active_since else active_since
            # ★ 复用播放器刚被激活时 `_last_t` 还是上一次用它的时间；直接拿它算
            #   age 会显示 6.4s 这种吓人的假阳性（2026-11-08 排查确认）。
            #   与 `stalled()` / `_stutter_watch` 一致，用 max(last, active_since)。
            # ★ 不可见图层现在**有意**不解码（见 `_tick_locked` 活跃标记），它的
            #   `age` 会一直涨 —— 那是挂起、不是卡帧。不区分的话以后一看日志就会
            #   把"用户关掉的图层"误判成冻帧（本文件已经被这类误读坑过多次）。
            _susp = (not vis) and (not getattr(p, "_active", False))
            age = "-" if _susp else (("%.2f" % (now - base)) if base else "-")
            segs.append("L%d %s vis=%d cur=%d/%d p=id%d incache=%d active=%d frames=%d age=%s ended=%d"
                        % (i, name, vis, cur, nclips, id(p), in_cache,
                           1 if getattr(p, "_active", False) else 0,
                           int(getattr(p, "_frames", 0) or 0), age,
                           1 if getattr(p, "_ended", False) else 0))
        return " | ".join(segs)

    def _get_player(self, lay, clip):
        """取该素材的解码器；缓存命中则复用（切换零开销）。LRU 上限 8。"""
        # ★★ 码表图层（容器层）**必须在这里早返回**，绝不进下面的通用 LRU：
        #   ① 通用 LRU 上限写死 8，且只认 `lay.clips[lay.cur]` / `lay.player` /
        #      `lay.prev_player` 三个"在用"标记 —— 而容器层没有单一 player、`clips` 也是空的
        #      ⇒ 谁都保护不了 ⇒ **会把正在用的 cue 解码器当空闲逐出**（表现为黑屏）；
        #   ② 通用逐出是 `v.close()`，**没有先 `set_active(False)`** —— 那正是
        #      2026-09-30「12 核 / 磁盘 750MB/s」事故的根因之一。
        if getattr(lay, "is_cuesheet", False):
            return self._cue_get_player(lay, clip)
        # 解码缩放上限：默认画布 1.0x（720p 输出不再把 1080p 源整帧搬来搬去）；
        # 有放大/旋转/码表时仍用 1.5x 留余量。见 `_decode_limit`。
        mw, mh = self._decode_limit(lay)
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
            # ★ 健康校验（2026-10-01 加，`_卡帧诊断.md` R2/R6）：
            #   解码线程"真收工"后对象**不会**被置 `_dead`，但缓存里还留着它 ——
            #   于是引擎会把一个**再也出不了帧的僵尸**反复交回来，
            #   画面就永远冻在最后一帧（用户报的「素材突然卡住不动」）。
            #   命中即校验：已放弃（_ended）/ 已关闭（_dead）⇒ 丢掉重开。
            bad = False
            try:
                bad = bool(getattr(p, "_ended", False)) or bool(getattr(p, "_dead", False))
            except Exception:                                  # noqa: BLE001
                bad = False
            if bad:
                lay.cache.pop(clip.path, None)
                try:
                    p.close()
                except Exception:                              # noqa: BLE001
                    pass
                p = None
        if p is not None:
            lay.cache.pop(clip.path)
            lay.cache[clip.path] = p
            if hasattr(p, "set_max_size"):
                p.set_max_size(mw, mh)
            return p
        _tp0 = time.perf_counter()
        p = make_player(clip)
        _open_ms = (time.perf_counter() - _tp0) * 1000.0
        if _open_ms >= 25.0:
            try:
                import stallwatch as _sw
                _sw.log_line("[OPEN] %.0fms %s" %
                             (_open_ms, str(getattr(clip, "path", "?"))[-80:]))
            except Exception:                              # noqa: BLE001
                pass
        if hasattr(p, "set_max_size"):
            p.set_max_size(mw, mh)
        lay.cache[clip.path] = p
        cur_path = lay.clips[lay.cur].path if 0 <= lay.cur < len(lay.clips) else ""
        while len(lay.cache) > 8:
            evicted = False
            for k, v in list(lay.cache.items()):
                if (k == cur_path
                        or k == getattr(lay, "_presec_path", None)
                        or v is lay.player or v is lay.prev_player):
                    continue
                v.close()
                lay.cache.pop(k)
                evicted = True
                break
            if not evicted:
                break
        return p

    # ================= 码表（cue）数据面 =================
    def _cue_get_player(self, lay, clip):
        """码表容器层的**独立**播放器缓存（容量按码表素材数给足；逐出三件套齐全）。

        与普通图层的 `lay.cache` 完全隔离，原因见 `_get_player` 开头的注释。
        逐出顺序固定：**`set_active(False)` → `close()` → 从 dict 删引用**（缺一不可）。
        """
        mw, mh = self._decode_limit(lay)
        cache = getattr(lay, "_cue_players", None)
        if not isinstance(cache, dict):
            cache = {}
            lay._cue_players = cache
        q = cache.get(clip.path)
        if q is not None:
            # ★★ 2026-10-17（铁律 0）：**alpha 结论变了就必须重开解码器**。
            #   为什么需要：`create_video_player()` 在没有缩略图、后台探测也没回填时
            #   会"先按无 alpha 用起来"（OpenCV ⇒ 丢透明 ⇒ 透明区变不透明黑）。
            #   后台 `_probe_alpha_async` 后来回填 `_has_alpha=True` 并置 `_alpha_recheck`，
            #   但 `_cue_get_player` 原来**完全不看这个标记** ⇒ 一旦用错解码器就整场不再纠正
            #   （普通图层的 `_get_player` 有这个逻辑，码表路径漏了）。
            #   逐出顺序仍是三件套：set_active(False) → close() → 从 dict 删引用。
            try:
                _want = getattr(clip, "_has_alpha", None)
                _had = getattr(q, "_cue_alpha_used", None)
                if (getattr(clip, "_alpha_recheck", False) and _want is not None
                        and (_had is None or bool(_want) != bool(_had))):
                    clip._alpha_recheck = False
                    cache.pop(clip.path, None)
                    try:
                        q.set_active(False)
                    except Exception:                             # noqa: BLE001
                        pass
                    try:
                        q.close()
                    except Exception:                             # noqa: BLE001
                        pass
                    q = None
                else:
                    if _had is None and _want is not None:
                        q._cue_alpha_used = bool(_want)
                    if hasattr(q, "set_max_size"):
                        q.set_max_size(mw, mh)
                    return q
            except Exception:                                     # noqa: BLE001
                pass
            if q is not None and hasattr(q, "set_max_size"):
                q.set_max_size(mw, mh)
            if q is not None:
                return q
        # ★ cue=True：码表场景的 DXV 不走 GPU（GPU 播放器没有 position/seek）
        q = make_player(clip, cue=True)
        try:
            q._cue_alpha_used = getattr(clip, "_has_alpha", None)
        except Exception:                                         # noqa: BLE001
            pass
        if hasattr(q, "set_max_size"):
            q.set_max_size(mw, mh)
        cache[clip.path] = q
        # ★★★ 2026-10-17：**新开的解码器一律标"待落地"**。
        #   为什么必须：播放器新建时从 **0 秒**开始，而此刻 `ps.play_sec` 往往已经在片段
        #   中段（例如 LOGO 循环第 6.5 秒）⇒ 不 seek 就永远差 6.5 秒。
        #   旧代码靠 `should_seek` 的"换片段"分支落地，但那个分支只在 `item_id` 变化时触发
        #   —— 解码器被逐出/因 alpha 纠正而重建时 `item_id` 没变 ⇒ **永远不补 seek**
        #   （实测：LOGO 层 `d` 稳定在 6.6 秒，画面与编排差 6 秒）。
        #   ⇒ 由 `_cue_frame` 见到这个标记就立刻 `seek(ps.play_sec)` 并记账。
        try:
            q._cue_need_seek = True
        except Exception:                                         # noqa: BLE001
            pass
        cap = max(32, len(getattr(self, "_cue_media", None) or {}) + 16)
        while len(cache) > cap:
            act = {getattr(st, "clip_path", "") for st in
                   (getattr(lay, "cs_states", None) or [])
                   if getattr(st, "active", False)}
            victim = next((k for k in list(cache) if k not in act), None)
            if victim is None:
                break                       # 全都在用 ⇒ 宁可超一点，也不动活跃的
            r = cache.pop(victim)
            try:
                if hasattr(r, "set_active"):
                    r.set_active(False)     # ★ 先失活
            except Exception:                # noqa: BLE001
                pass
            try:
                r.close()
            except Exception:                # noqa: BLE001
                pass
        return q

    def set_cue_sheet(self, sheet, media_paths=None, track=None, layer=None,
                      sheet_id="", track_title="", track_artist="",
                      track_duration=0.0, sheet_path=""):
        """装载一张码表（**数据面**：本步不参与渲染，渲染在 S4/S5）。

        · `track` 给了就直接用；否则按 (title/artist/duration) 自动认一首；
        · **认不出曲目 ⇒ 只装载码表、不建运行时** ⇒ 码表图层不接管（安全降级）；
        · 幂等：内部先 `clear_cue_sheet(silent=True)`，同一份调两次不会叠两份；
        · `layer` 给了就把它标记成码表容器层（`is_cuesheet=True`）。
        返回 `True` = 已建立运行时（可接管）。
        """
        import cuesheet_runtime as _cr
        self.clear_cue_sheet(silent=True)
        self._cue_sheet = sheet
        # ★ 2026-11-02（用户「码表图层和码表管理里显示的名字不一样」）：
        #   界面要统一按**文件名**显示（`cue_page.sheet_label` 定的口径 — 用户是在文件系统里
        #   管理码表的）。而这里原来只把包内 `title` 带出去 ⇒ 同一个包在码表管理叫
        #   `测试151.ercue`、在图层/对话框叫 `测试11`（title），对不上。
        #   把路径也带出去，界面就能自己取 basename。
        self._cue_sheet_path = str(sheet_path or "")
        self._cue_media = dict(media_paths or {})
        self._rebuild_cue_media_snap(sheet)
        # ★ 用包内 `media[].has_alpha` 建 alpha 事实表（见 `__init__` 的说明）。
        #   键是**解压后的实际路径**，与 `_cue_item()` 拿到的 `ps.clip_path` 同源。
        self._cue_alpha = {}
        if sheet is not None:
            try:
                for _m in (getattr(sheet, "media", None) or []):
                    _pth = self._cue_media.get(getattr(_m, "clip_id", "") or "")
                    if _pth:
                        self._cue_alpha[str(_pth)] = bool(getattr(_m, "has_alpha", False))
            except Exception:                                     # noqa: BLE001
                self._cue_alpha = {}
        if track is None and sheet is not None:
            try:
                track = sheet.track_by_identity(track_title, track_artist,
                                                track_duration)
            except Exception:                                     # noqa: BLE001
                track = None
        self._cue_track = track
        self._cue_bound_cache = None     # ★ 换表/换曲目 ⇒ 绑定关系变了，反查缓存必须失效
        self._cue_bad_song_n = 0
        self._cue_rt = None
        if sheet is not None and track is not None:
            try:
                self._cue_rt = _cr.CueRuntime(sheet, track,
                                              media_paths=self._cue_media)
            except Exception:                                     # noqa: BLE001
                self._cue_rt = None
        if layer is not None:
            layer.is_cuesheet = True
            layer.cuesheet_id = str(sheet_id or getattr(sheet, "title", "") or "")
            if not isinstance(getattr(layer, "_cue_players", None), dict):
                layer._cue_players = {}
        return self._cue_rt is not None

    def clear_cue_sheet(self, silent=False):
        """卸下码表：关掉**所有** cue 解码器、清掉运行时状态。

        · 图层本身的 `is_cuesheet` **保留**（它还是容器层，只是暂时没装表）；
        · 关闭顺序：先 `set_active(False)` 再 `close()`（9-30 事故根因防线）；
        · 返回 True（幂等：没装载时调用也安全）。
        """
        self._cue_sheet = None
        self._cue_sheet_path = ""        # ★ 2026-11-02：卸表 ⇒ 路径一并清（否则界面会显示上一张表的名字）
        self._cue_track = None
        self._cue_bound_cache = None     # ★ 卸表 ⇒ 反查缓存失效（铁律 11：两处独立复位）
        self._cue_bad_song_n = 0
        self._cue_rt = None
        self._cue_media = {}
        # 无锁快照一并复位（铁律 11：新增标志必须有 ≥2 处独立复位 —— 这里是卸表，
        # 另一处是 `__init__`；装表会重新赋值）
        self._cue_media_snap = ()
        self._cue_thumb_snap = {}      # 包内缩略图快照（卸表 ⇒ 一并清空）
        self._cue_active_snap = frozenset()
        self._cue_alpha = {}
        self._cue_pos_beats = None
        self._cue_pos_src = ""
        self._cue_sid = -1
        self._cue_jumped = False
        closed = 0
        for lay in (getattr(self, "layers", None) or []):
            if not getattr(lay, "is_cuesheet", False):
                continue
            lay.cs_states = []
            lay.cs_active = False
            for q in list((getattr(lay, "_cue_players", None) or {}).values()):
                try:
                    if hasattr(q, "set_active"):
                        q.set_active(False)
                except Exception:                                 # noqa: BLE001
                    pass
                try:
                    q.close()
                    closed += 1
                except Exception:                                 # noqa: BLE001
                    pass
            lay._cue_players = {}
            lay._cue_items = {}
            lay._cue_last_frame = {}
            lay._cue_seek_state = {}
            lay._cue_sub_tmps = {}
        self._cue_closed_last = closed
        try:
            if getattr(self, "_cue_policy", None) is not None:
                self._cue_policy.reset()   # 卸表/换歌：清 in-flight / degraded / target
        except Exception:                                     # noqa: BLE001
            pass
        return True

    def cuesheet_status(self):
        """只读状态快照（UI 状态栏 / 自检用；**不参与渲染**，也不改任何状态）。"""
        sheet = getattr(self, "_cue_sheet", None)
        rt = getattr(self, "_cue_rt", None)
        track = getattr(self, "_cue_track", None)
        idn = getattr(track, "identity", None)
        lays = [l for l in (getattr(self, "layers", None) or [])
                if getattr(l, "is_cuesheet", False)]
        # ★ 2026-10-18：把"此刻有几层在画"暴露出去（HUD/日志都用它回答"多层到底有没有生效"）。
        #   容器层一帧里可能同时画多个子层，用户光看画面难判断是"只有一层"还是"多层都在"。
        n_states = n_act = 0
        for l in lays:
            for ps in (getattr(l, "cs_states", None) or []):
                n_states += 1
                if getattr(ps, "active", False) and not getattr(ps, "missing", False):
                    n_act += 1
        return {
            "loaded": sheet is not None,
            "title": str(getattr(sheet, "title", "") or ""),
            # ★ 2026-11-02：包文件名（界面显示名统一用它，见 `set_cue_sheet` 的注释）
            "path": str(getattr(self, "_cue_sheet_path", "") or ""),
            "tracks": len(getattr(sheet, "tracks", None) or []),
            "res": (sheet.aspect() if sheet is not None else (1920, 1080)),
            "track_title": str(getattr(idn, "title", "") or "") if idn else "",
            "bpm": float(getattr(idn, "bpm", 0.0) or 0.0) if idn else 0.0,
            "has_runtime": rt is not None,
            "rt_layers": int(getattr(rt, "n_layers", 0) or 0) if rt is not None else 0,
            "rt_items": int(getattr(rt, "n_items", 0) or 0) if rt is not None else 0,
            "bound_layers": [l.name for l in lays],
            "active_layers": [l.name for l in lays if getattr(l, "cs_active", False)],
            "sub_states": n_states,
            "sub_active": n_act,
            "closed_last": int(getattr(self, "_cue_closed_last", 0) or 0),
        }

    def cue_index(self, refresh=False):
        """取码表索引（惰性建；`refresh=True` 时按 mtime/size 重读变了的包）。

        **启动路径只调 `load_cfg()`**（纯内存、零 IO）⇒ 不加载整张表。
        """
        from cue_index import CueIndex
        if getattr(self, "_cue_index", None) is None:
            self._cue_index = CueIndex()
            self._cue_index.load_cfg(self.cfg)
        if refresh:
            self._cue_index.rescan()
        return self._cue_index

    def cue_index_refresh(self):
        """重读表头 + 落盘（导入/删包后调用）。返回索引。"""
        idx = self.cue_index(refresh=True)
        idx.save_cfg(self.cfg)
        return idx

    def cue_sheets_for_track(self, title="", artist="", duration_sec=0.0, fp_id="",
                             file_name="", file_md5=""):
        """**这首歌能用哪些码表** → `[(摘要, 命中级别)]`（按可靠性排序）。

        ★ 命中判定复用 `CueSheet.match_track()`（五级链），这里只汇总，不重造匹配。
        """
        try:
            return self.cue_index().match(title=title, artist=artist,
                                          duration_sec=duration_sec, fp_id=fp_id,
                                          file_name=file_name, file_md5=file_md5)
        except Exception:                                        # noqa: BLE001
            return []

    # ---------------- 自动跟拍（Step3） ----------------
    def set_cue_manual_clock(self, on):
        """手动起拍开关：True 时**暂停自动位置**（Step3 落地后它是兜底，不是主路径）。"""
        self._cue_manual_clock = bool(on)
        if on:
            self._cue_pos_tracker.reset()
        return self._cue_manual_clock

    @staticmethod
    def _cue_song_id_of(path):
        """路径 → 指纹库 id。**与 `ui_main` 建库处同一算法**：
        `int(hashlib.md5(path.encode("utf-8")).hexdigest()[:8], 16)`。

        ⚠ 这条曾差点写错：我最初按"整数索引查 `music_library`"实现，而现场是**路径哈希**
          ⇒ 真机上会"永远查不到歌"。已按建库处对齐（`ui_main.py:1791`）。
          如果哪天建库算法变了，这里必须同步改（否则认歌链路静默失效）。
        """
        try:
            return int(hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8], 16)
        except Exception:                                        # noqa: BLE001
            return -1

    def _cue_id_map(self):
        """`song_id` → 路径 映射（按 `cfg["music_library"]` 现算；库变了自动重建）。"""
        try:
            lib = tuple(self.cfg["music_library"] or [])
        except Exception:                                        # noqa: BLE001
            lib = ()
        cached = getattr(self, "_cue_id_cache", None)
        if cached is not None and cached[0] == lib:
            return cached[1]
        m = {}
        for p in lib:
            sid = self._cue_song_id_of(p)
            if sid >= 0:
                m[sid] = str(p)
        self._cue_id_cache = (lib, m)
        return m

    def _cue_song_path(self, song_id):
        """`song_id` → 曲子路径（取不到返回 ""）。

        现场约定（已核对 `ui_main.py:1791`）：`song_id = int(md5(path)[:8], 16)`。
        额外兼容"直接给路径字符串"的调用方（便于测试/嵌入）。
        """
        if isinstance(song_id, str) and song_id:
            return song_id
        try:
            return self._cue_id_map().get(int(song_id), "")
        except Exception:                                        # noqa: BLE001
            return ""

    def _cue_lookup_song_id(self, tr):
        """码表里的**一条曲目** → 演出端 `song_id`（= 指纹库 id = `md5(路径)[:8]`）。

        与 `CueSheet.match_track()` 同一套判据，但**方向相反**（从码表反查本机曲库）：
          ⓪ `identity.fp_id` 在 `cfg["cue"]["fp_songs"]` 里 ⇒ 直接用它记的 **合成 id**
             （= 码表包里带的完整指纹已经灌进了指纹库，**本机没有这个文件**也能认）
          ① `identity.fp_id`     == `meta["fp_id"]`      （指纹签名，抗改名/换容器）
          ② `identity.file_md5`  == `meta["file_md5"]`   （同一文件字节）
          ③ basename(`file_name`) == basename(本机路径)   （老码表没有前两个字段时兜底）
        取不到 ⇒ None（调用方据此**不启用**认错歌护栏，退回旧行为）。

        ⚠ ⓪ 级必须在最前：灌库后识别器返回的就是那个合成 id，若这里反查成本机路径哈希，
          `_cue_accept_pos()` 会认为"认到了另一首歌"⇒ 永远不跟位（看着像功能没生效）。
          去重守卫（`cue_fp_pack.ingest_sheet`：本机曲库已有同 fp_id 就不灌）保证
          "同一首歌两份指纹"这种拆票情况不会发生。
        """
        idn = getattr(tr, "identity", None)
        if idn is None:
            return None
        fp_id = str(getattr(idn, "fp_id", "") or "").strip()
        fmd5 = str(getattr(idn, "file_md5", "") or "").strip().lower()
        # ---- ⓪ 码表包里带来的指纹（合成 id；本机没有文件）----
        if fp_id:
            rec = self._cue_fp_songs().get(fp_id)
            if isinstance(rec, dict):
                try:
                    sid = int(rec.get("song_id", -1))
                except (TypeError, ValueError):
                    sid = -1
                if sid >= 0:
                    return sid
        try:
            meta = self.cfg["music_meta"] or {}
            lib = self.cfg["music_library"] or []
        except Exception:                                        # noqa: BLE001
            meta, lib = {}, []
        if fp_id or fmd5:
            for p, m in meta.items():
                if not isinstance(m, dict):
                    continue
                if fp_id and str(m.get("fp_id") or "").strip() == fp_id:
                    return self._cue_song_id_of(p)
                if fmd5 and str(m.get("file_md5") or "").strip().lower() == fmd5:
                    return self._cue_song_id_of(p)
        name = os.path.basename(str(getattr(idn, "file_name", "") or ""))
        if name:
            for p in lib:
                if os.path.basename(str(p)) == name:
                    return self._cue_song_id_of(p)
        return None

    def _cue_pos_beat_len(self):
        """算 `pos_beats` 用的一拍秒数 —— **必须与 `CueRuntime.beat_len` 同源**。

        结构上直接读 `self._cue_rt.beat_len`：它就是 `update()` 消费 `pos_beats` 的那个
        对象，读它就不可能出现"算位置用一种拍长、选片段用另一种"。
        表没装（rt 为 None）⇒ 退化值 `1.0`（= `cuesheet.DEGENERATE_BEAT_LEN`）。

        ⚠ 别改回"从码表 bpm / 曲库网格 bpm 现算"：那正是 2026-10-17 那个
          「bpm=0 的码表位置被放大 2.5 倍」bug 的来源（详见 `_cue_auto_position` 注释）。
        """
        rt = getattr(self, "_cue_rt", None)
        try:
            bl = float(getattr(rt, "beat_len", 0.0) or 0.0)
        except (TypeError, ValueError):
            bl = 0.0
        return bl if bl > 1e-6 else 1.0

    def _cue_bound_ids(self):
        """`(主 id, 可接受 id 集合)`（**带缓存**；`set/clear_cue_sheet` 时清空）。

        为什么要"集合"而不是"单个 id"（用户 2026-10-18 的真实场景）：
          曲库里是**版本 A**（已扫进曲库），码表包却是用**版本 B**做的
          （同一首音乐，只有专辑/封面不同；也可能 A=flac、B=mp3 而 `fp_id` 不同）。
          ⇒ 表里的曲目与"此刻 DJ 放的音频"在演出端是**两个 song_id**：
             · 灌包指纹得到的那一个（合成 id，`fp_song_id(fp_id)`）；
             · 本机曲库里同一首音乐的路径 id。
          只认一个 ⇒ 另一个必被护栏拦掉 ⇒ **不出画**（用户看到"导入了码表却不动"）。
        所以：把"`match_track()` 会认成同一条 track 的"本机 id **全部**放进集合。
        ⚠ 保守：判据与认歌同一套（fp_id → file_md5 → meta_id → 名+艺人+时长≤2s），
          **不**把整库都放进去 —— 别的歌仍然必须被护栏拒掉。
        """
        tr = getattr(self, "_cue_track", None)
        if tr is None:
            return None, frozenset()
        cache = getattr(self, "_cue_bound_cache", None)
        if cache is not None and cache[0] is tr:
            return cache[1], cache[2]
        primary = self._cue_lookup_song_id(tr)
        ids = set()
        if primary is not None:
            try:
                ids.add(int(primary))
            except (TypeError, ValueError):
                pass
        try:
            ids.update(self._cue_peer_song_ids(tr))
        except Exception:                                        # noqa: BLE001
            pass
        # ★ 2026-11-17：导入库的歌不在 `music_meta` 里，但认歌线程会给出它的 sid。
        #   不并进来 ⇒ 护栏把“导入库那首”当成别的歌 ⇒ pb=None ⇒ 码表图层永远没位置
        #   （用户：“导入到 avj 没画面”）。
        try:
            ids.update(self._cue_imported_song_ids(tr))
        except Exception:                                        # noqa: BLE001
            pass
        frozen = frozenset(ids)
        self._cue_bound_cache = (tr, primary, frozen)
        return primary, frozen

    def _cue_bound_song_id(self):
        """当前绑定曲目 → **主** `song_id`（诊断/日志用；护栏请用 `_cue_bound_song_ids()`）。

        "主" = `_cue_lookup_song_id()` 沿 ⓪→①→②→③ 认出来的那个（等同改动前的返回值）。
        同一首歌在本机的**其它等价 id**（同音乐另一个版本）不在这个返回值里，
        它们在 `_cue_bound_song_ids()` 里 —— 护栏要的是集合。
        """
        return self._cue_bound_ids()[0]

    def _cue_bound_song_ids(self):
        """当前绑定曲目在本机**可能对应哪些 song_id**（护栏用的可接受集合）。

        返回 `set`：空集 ⇒ 调用方**不启用护栏**（绝不变成"永远不跟"的新故障）。
        缓存按 track 对象身份（`set/clear_cue_sheet` 会清）。
        """
        return set(self._cue_bound_ids()[1])

    @staticmethod
    def _cue_local_dur(rec):
        """`music_meta` 一条记录里的时长（秒）：`grid.dur` → `duration` → `dur`；没有 ⇒ 0.0。

        ⚠ 曲库扫描把时长写在 `meta["grid"]["dur"]`（不是顶层），只读顶层等于永远拿 0
          ⇒ 第 ④ 级（名+艺人+时长）永远不成立。
        """
        if not isinstance(rec, dict):
            return 0.0
        for v in ((rec.get("grid") or {}).get("dur") if isinstance(rec.get("grid"), dict)
                  else None, rec.get("duration"), rec.get("dur")):
            try:
                f = float(v or 0.0)
            except (TypeError, ValueError):
                continue
            if f > 0.0:
                return f
        return 0.0

    def _cue_peer_song_ids(self, tr):
        """本机曲库里**同一首音乐**的其它 `song_id`（判据与认歌同级，按可靠性从高到低）。

        ★ **0 级（最高优先）：`cfg["cue"]["fp_same"]`** —— 导入码表时用**指纹内容
          重合率**判出来的"码表这首 = 本机这首"（`cue_fp_pack.match_pack_local`）。
          为什么必须放在 `fp_id` 之前（用户 2026-10-30 两次强调）：
            `identity.fp_id` 是 `sha1(全部哈希串联)[:16]`，**一个哈希不同签名就变**，
            只能判"完全同一个 PCM"；而"同一首歌的两个版本，只是专辑不同"在指纹层面
            是**大面积重合、少数哈希不同** ⇒ 签名必不相同、四级链全都判不出来。
            实测：同一文件 100% / 同曲不同混音 11.5% / 无关曲目 8.7~9.1%。
          这一步**只读 cfg**（导入时算好落盘），运行期零重活。

        ① `fp_id` 相等（同一段音频的指纹签名 —— 换容器/改名仍命中）
        ② `file_md5` 相等（同一文件字节）
        ③ `meta_id` 相等（名+艺人+时长取整 2 秒；两边缺失时现算，与 `Identity.meta_key` 同式子）
        ④ 名 + 艺人（归一化，与认歌第 ③ 级同一口径）**且**时长差 ≤2 秒
        **绝不**按"文件名"猜（改名/换目录猜不到，且容易把翻唱认成同一首）。
        取不到 ⇒ 空集（绝不抛）。
        """
        idn = getattr(tr, "identity", None)
        if idn is None:
            return set()
        out = set()
        fp_id = str(getattr(idn, "fp_id", "") or "").strip()
        # ---- 0 级：导入时的指纹内容对账（最优判据，见 docstring）----
        try:
            _same = (self.cfg["cue"] or {}).get("fp_same") or {}
        except Exception:                                        # noqa: BLE001
            _same = {}
        if fp_id and isinstance(_same, dict):
            _p = str(_same.get(fp_id) or "")
            if _p:
                try:
                    _sid = self._cue_song_id_of(_p)
                    if _sid >= 0:
                        out.add(int(_sid))
                except Exception:                                # noqa: BLE001
                    pass
        try:
            meta = self.cfg["music_meta"] or {}
        except Exception:                                        # noqa: BLE001
            meta = {}
        if not isinstance(meta, dict) or not meta:
            return out                       # 曲库为空也要把 0 级结果带回去
        fmd5 = str(getattr(idn, "file_md5", "") or "").strip().lower()
        mid = str(getattr(idn, "meta_id", "") or "").strip()
        title = str(getattr(idn, "title", "") or "")
        artist = str(getattr(idn, "artist", "") or "")
        try:
            dur = float(getattr(idn, "duration_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        try:
            import cuesheet as _cs
            norm = _cs._norm_title_artist
            want_nt, want_na = norm(title), norm(artist)
        except Exception:                                        # noqa: BLE001
            _cs, norm = None, None
            want_nt = want_na = ""
        for p, rec in meta.items():
            if not isinstance(rec, dict):
                continue
            sid = self._cue_song_id_of(p)
            if sid < 0:
                continue
            r_fp = str(rec.get("fp_id") or "").strip()
            if fp_id and r_fp and r_fp == fp_id:
                out.add(sid)
                continue
            r_md5 = str(rec.get("file_md5") or rec.get("md5") or "").strip().lower()
            if fmd5 and r_md5 and r_md5 == fmd5:
                out.add(sid)
                continue
            if not (want_nt or want_na):
                continue
            r_title = str(rec.get("title") or "")
            r_artist = str(rec.get("artist") or "")
            r_dur = self._cue_local_dur(rec)
            # ③ meta_id（两侧任一缺失就现算 —— 与 `cue_index._meta_key` / `Identity.meta_key` 同式子）
            r_mid = str(rec.get("meta_id") or "").strip()
            if _cs is not None:
                if not r_mid and (r_title or r_artist):
                    try:
                        r_mid = _cs.Identity.meta_key(r_title, r_artist, r_dur)
                    except Exception:                            # noqa: BLE001
                        r_mid = ""
                if not mid and (want_nt or want_na):
                    try:
                        mid = _cs.Identity.meta_key(title, artist, dur)
                    except Exception:                            # noqa: BLE001
                        mid = ""
            if mid and r_mid and mid == r_mid:
                out.add(sid)
                continue
            # ④ 名 + 艺人 + 时长差 ≤2 秒（时长必须两侧都有，否则宁可不算 —— 保守）
            if norm is None or dur <= 0.0 or r_dur <= 0.0:
                continue
            if abs(r_dur - dur) > 2.0:
                continue
            try:
                if norm(r_title) == want_nt and norm(r_artist) == want_na:
                    out.add(sid)
            except Exception:                                    # noqa: BLE001
                continue
        return out

    def _cue_imported_song_ids(self, tr):
        """码表曲目在本机**导入库**里对应的 song_id 集合。

        导入库（`imported_meta.db`）是独立 SQLite，不在 `cfg["music_meta"]`；
        但认歌线程会把导入歌的 sid 报上来。码表护栏若不认识这个 sid，就会把
        “正确认出的导入歌”当成别的歌 ⇒ `pb=None` ⇒ 码表图层没画面。
        判据与 `match_track()` 第 ③ 级**完全一致：归一化名 + 艺人**（时长只用于
        多候选排序，不作硬门槛）—— 否则“扩展版/不同母带差 >2 秒”会被 binder 绑上、
        又被 guard 拒掉，现场就是“码表导入了却没画面”。
        """
        out = set()
        try:
            idn = getattr(tr, "identity", None)
            if idn is None:
                return out
            title = str(getattr(idn, "title", "") or "")
            artist = str(getattr(idn, "artist", "") or "")
            try:
                dur = float(getattr(idn, "duration_sec", 0.0) or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            if not (title or artist):
                return out
            import cuesheet as _cs
            norm = _cs._norm_title_artist
            want_nt, want_na = norm(title), norm(artist)
            import imported_lib as _IL
            from config import app_base_dir
            for r in (_IL.display_rows(app_base_dir()) or []):
                if not isinstance(r, dict):
                    continue
                try:
                    sid = int(r.get("sid") or -1)
                except (TypeError, ValueError):
                    sid = -1
                if sid < 0:
                    continue
                try:
                    rdur = float(r.get("dur") or 0.0)
                except (TypeError, ValueError):
                    rdur = 0.0
                try:
                    if (norm(str(r.get("title") or "")) == want_nt
                            and norm(str(r.get("artist") or "")) == want_na):
                        out.add(int(sid))
                except Exception:                                # noqa: BLE001
                    continue
        except Exception:                                        # noqa: BLE001
            pass
        return out

    def _cue_diag(self, sid, votes, off, status, pb, now):
        """`cue_health.log` 取证（2s 一行，环形 400 行）——回答"到底卡在哪一段"。

        为什么要它（2026-10-17）：用户报"识别不到歌"，而识别链的 print 在 windowed exe 里
        **根本看不到**（无控制台）⇒ 只能靠猜。这一行把**输入 + 识别 + 绑定 + 位置**串在一起：

          · `silent` 大 / `buf` 小            ⇒ 音频没进来（设备、路由、静音）
          · `votes=0` 但有能量                  ⇒ 指纹匹配不上（库里没这首 / 指纹不是同一段音频）
          · `sid` 有票 但 `pb=None`            ⇒ 位置层没接管（码表没绑 / 护栏拦了 / 表没装）
          · `pb` 有值 但画面不动                ⇒ 渲染或素材问题（看 layer_health.log）
        """
        try:
            t = float(getattr(self, "_cue_diag_t", 0.0) or 0.0)
            if now - t < 2.0:
                return
            self._cue_diag_t = now
            # ⚠ `self.audio` 就是 AudioState（见 `__init__` 的说明），不能取 `.state`
            st = getattr(self, "audio", None)
            # ⚠ 不能用 `getattr(..., -1.0) or -1.0`：`silent_sec == 0.0`（**有声音**）会被
            #   `or` 变成 -1.0 ⇒ 日志显示"没见过声音"，与事实相反（本行第一版就踩了这个）。
            silent = -1.0
            try:
                _v = getattr(st, "silent_sec", None)
                if _v is not None:
                    silent = float(_v)
            except (TypeError, ValueError):
                pass
            rt = getattr(self, "_cue_rt", None)
            tr = getattr(self, "_cue_track", None)
            idn = getattr(tr, "identity", None) if tr is not None else None
            n_items = int(getattr(rt, "n_items", 0) or 0) if rt is not None else 0
            # ★ 2026-10-18：**子层实况** —— 用户报"多层编排没看见"时，这一行是唯一判据：
            #   `sub=3/2` 表示"这一帧有 3 个子层状态、其中 2 个真的在画"；
            #   `dmax` = 各子层 |play_sec − 播放器实际位置| 的最大值（秒）；
            #   `spd` = 实际下发的倍速范围（能看出实时变速对齐有没有在工作）。
            n_states = n_act = 0
            dmax = -1.0
            spd_lo, spd_hi = 9.9, -9.9
            for _l in (getattr(self, "layers", None) or []):
                if not getattr(_l, "is_cuesheet", False):
                    continue
                _pls = getattr(_l, "_cue_players", None) or {}
                for _ps in (getattr(_l, "cs_states", None) or []):
                    n_states += 1
                    if not (getattr(_ps, "active", False) and not getattr(_ps, "missing", False)):
                        continue
                    n_act += 1
                    _p = _pls.get(getattr(_ps, "clip_path", "") or "")
                    try:
                        _pp = _p.position() if _p is not None else None
                    except Exception:                            # noqa: BLE001
                        _pp = None
                    if _pp is not None:
                        dmax = max(dmax, abs(float(_ps.play_sec) - float(_pp)))
                    try:
                        _sp = float(getattr(_p, "speed", 1.0) or 1.0)
                        spd_lo = min(spd_lo, _sp)
                        spd_hi = max(spd_hi, _sp)
                    except (TypeError, ValueError):
                        pass
            # ★ 2026-10-18（变速真机验证）`r=` = 位置层实测的**音乐播放倍率**（前馈基准）。
            #   有了它才能证明"音乐变速时码表按同一时间基走"，而不是靠 dmax 反推。
            #   ⚠ 本函数里没有 `_f`（那是 `_cue_auto_position` 里的局部函数），这里就地兜住。
            _trk = getattr(self, "_cue_pos_tracker", None)
            try:
                _rv = float(getattr(_trk, "rate", 1.0))
            except (TypeError, ValueError):
                _rv = 1.0
            # ★ 2026-10-29：行尾**追加** `E=`（实测能量）与 `tier=`（当前能量档位）。
            #   为什么要它：用户要"验证能量识别得准不准"，而 VDJ 的 database.xml
            #   **没有能量字段**（只有 Volume 增益），唯一的真值来源就是本软件的实测值 ——
            #   不落盘就完全无法离线核对曲线。
            #   ⚠ 按铁律**一律追加在行尾**，不改既有字段顺序（既有 regex / 解析脚本不破）。
            _ev, _tier = 0.0, "-"
            try:
                _ev = float(getattr(st, "energy", 0.0) or 0.0)
                _tier = str(getattr(self, "_last_energy_tier", "") or "-")
            except (TypeError, ValueError):
                pass
            line = ("[%s] silent=%.1fs sid=%s votes=%d off=%.2f status=%s pb=%s "
                    "| sheet=%d track=%s rt=%d items=%d pos=%s sub=%d/%d dmax=%.2f spd=%s fw=%d why=%s r=%.3f dark=%.2f/%d"
                    " E=%.2f tier=%s"
                    % (time.strftime("%H:%M:%S"), silent, sid, int(votes or 0),
                       float(off if off is not None else -1.0), status,
                       ("%.1f" % pb) if pb is not None else "None",
                       1 if getattr(self, "_cue_sheet", None) is not None else 0,
                       (str(getattr(idn, "title", "") or "?")[:14]) if idn else "None",
                       1 if rt is not None else 0, n_items,
                       str(getattr(self, "_cue_pos_src", "") or "-"),
                       n_states, n_act, dmax,
                       ("%.2f~%.2f" % (spd_lo, spd_hi)) if spd_hi > -9 else "-",
                       1 if getattr(self, "_cue_fw_on", False) else 0,
                       (getattr(self, "_cue_tick_why", "") or "-"),
                       _rv,
                       float(getattr(self, "_cue_dark_max", 0.0) or 0.0),
                       int(getattr(self, "_cue_dark_n", 0) or 0),
                       _ev, _tier))
            path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                                "cue_health.log")
            _d = os.path.dirname(path)
            if _d and not os.path.isdir(_d):
                os.makedirs(_d, exist_ok=True)      # 兜底：目录不在也能写（不在热路径，每 2s 一次）
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # 环形：超过 400 行就砍掉前一半（与 layer_health 同风格，绝不无界增长）
            try:
                if os.path.getsize(path) > 120 * 1024:
                    with open(path, "r", encoding="utf-8") as f:
                        rows = f.readlines()
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(rows[-200:])
            except OSError:
                pass
        except Exception:                                        # noqa: BLE001
            pass

    def _cue_accept_pos(self, sid, pb):
        """认错歌护栏：识别到的歌必须**属于本表这条曲目**，否则本帧不接管（返回 None）。

        背景（用户 2026-10-17 追问「识别错了怎么办」）：指纹会在重复段落 / 同曲不同版本 /
        翻唱上认成另一首歌。照跟的话 `pos_beats` 是**那首歌**的秒数，码表会整段乱跳。

        ★ 2026-10-18 修正：判据从"单个 id 相等"改成"**属于可接受集合**"
          （`_cue_bound_song_ids()`）—— 同一首音乐在本机可能有两个 id（码表包里那版 +
          曲库里那版），只认一个会把其中一版判成"另一首歌"⇒ 不出画。
          集合为空 ⇒ **不启用护栏**（退回"照跟"），绝不让护栏变成"永远不跟"的新故障。
        """
        if pb is None:
            return None
        want = self._cue_bound_song_ids()
        if want and sid >= 0 and sid not in want:
            self._cue_bad_song_log(sid, want)
            return None
        self._cue_bad_song_n = 0
        return pb

    def _cue_bad_song_log(self, sid, want):
        """限流记录「认到的是另一首歌 ⇒ 码表暂不跟位」（10s 一条，不刷屏）。"""
        try:
            n = int(getattr(self, "_cue_bad_song_n", 0) or 0) + 1
            self._cue_bad_song_n = n
            now = time.perf_counter()
            if now - float(getattr(self, "_cue_bad_song_t", 0.0) or 0.0) < 10.0:
                return
            self._cue_bad_song_t = now
            try:
                shown = ",".join(str(int(x)) for x in sorted(want))
            except TypeError:
                shown = str(want)
            self._cue_health("认到另一首歌（song_id=%s，本表绑定 %s）⇒ 码表暂不跟位（累计 %d 次）"
                             % (sid, shown, n))
        except Exception:                                        # noqa: BLE001
            pass

    def _cue_track_query(self, path):
        """由曲子路径拼"认歌查询参数"（有 meta 用 meta，没有就退化到文件名级）。

        ⚠⚠ **三个 md5 不是一回事，别混用**（混用不报错、只让某一级**静默永不命中**）：
          · `meta["song_id"]`  = `int(md5(路径字符串)[:8],16)`（8 hex 整数）
                                 → 指纹库主键 / `recognized_song_id`；**与文件内容无关**
          · `meta["file_md5"]` = **音频文件字节**的 md5（32 hex）→ 码表认歌 ⓪ 级
          · `meta["fp_id"]`    = 指纹签名 `fp2-<16hex>`        → 码表认歌 ① 级
        把 `song_id` 当 `file_md5` / `fp_id` 用不会抛异常，只会让 ⓪/① 两级永不命中，
        认歌就退化成"标题+艺人 / 文件名"这些最容易误判的级别。
        """
        q = {"file_name": os.path.basename(path or "")}
        try:
            meta = (self.cfg["music_meta"] or {}).get(path) or {}
            if meta:
                q["title"] = str(meta.get("title") or "")
                q["artist"] = str(meta.get("artist") or "")
                # ★ 2026-11-18：扫描把时长写在 `grid.dur`，顶层没有 `duration`
                #   ⇒ 原来这里永远 0，`match_track` 第 ②/③ 级全废。
                q["duration_sec"] = self._cue_local_dur(meta)
                # `md5` 是早期键名，保留作旧配置兼容（新扫描只写 `file_md5`）
                q["file_md5"] = str(meta.get("file_md5") or meta.get("md5") or "")
                q["fp_id"] = str(meta.get("fp_id") or "")
        except Exception:                                        # noqa: BLE001
            pass
        return q

    def _cue_fp_songs(self):
        """`cfg["cue"]["fp_songs"]`：码表包里带来的指纹 → **合成 song_id** 的映射。

        由 `cue_fp_pack.ingest_sheet()` 写入（导入码表时后台灌库），形如：
            {"fp2-xxxx": {"song_id": int, "title": str, "artist": str,
                          "duration": float, "sheet": "<包路径>"}}

        ★ 这些歌**本机没有音频文件** —— 这正是整条链的意义：音频从系统声音/调音台进来，
          指纹库里已经有它的哈希 ⇒ 照样能认，码表照样能跟位。
        ⚠ `Config` 是包装对象（**没有 `.get()`**）⇒ 只能 `cfg["cue"]`，且一律兜异常。
        """
        try:
            cur = self.cfg["cue"]
        except Exception:                                        # noqa: BLE001
            return {}
        if not isinstance(cur, dict):
            return {}
        m = cur.get("fp_songs")
        return m if isinstance(m, dict) else {}

    def _cue_imported_meta(self, sid):
        """导入库（`imported_meta.db`）里某个 `song_id` 的歌名/艺人/时长。

        ★ 2026-11-17（用户：放 Can We Believe，dj 屏码表仍不演出）：
          日志是 `pb=111.0 sheet=1 track=None rt=0 why=no_rt` —— 位置早就对了，
          卡在“认曲目”这一步：导入库的歌**没有本机文件路径**，`_cue_track_query()`
          拿不到，而 `_cue_track_query_by_sid()` 原来只查 `cfg["cue"]["fp_songs"]`
          （那是码表包自带的指纹合成 id）。导入库的 sid 不在这张表里 ⇒ 查询为空
          ⇒ `match_track()` 永远返回 None ⇒ `_cue_rt` 永远是 None ⇒ 码表图层
          **一次都没演过**（`items=0`）。
          导入库有 title/artist/dur，正好够 `match_track()` 的第 ③/④ 级。

        `display_rows()` 每次要开 SQLite；按库文件 mtime 缓存一份
        （导入/删除会改 mtime，自动失效）。
        """
        try:
            want = int(sid)
        except (TypeError, ValueError):
            return None
        try:
            import imported_lib as _IL
            from config import app_base_dir
            base = app_base_dir()
            dbp = _IL.meta_db_path(base)
            mtime = os.path.getmtime(dbp) if os.path.isfile(dbp) else 0.0
            cache = getattr(self, "_cue_imp_cache", None)
            if not cache or float(cache[0]) != float(mtime):
                rows = {}
                for r in (_IL.display_rows(base) or []):
                    if not isinstance(r, dict):
                        continue
                    try:
                        rows[int(r.get("sid") or -1)] = r
                    except (TypeError, ValueError):
                        continue
                cache = (mtime, rows)
                self._cue_imp_cache = cache
            r = (cache[1] or {}).get(want)
            if not r:
                return None
            try:
                dur = float(r.get("dur") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            return {
                "title": str(r.get("title") or r.get("file_name") or ""),
                "artist": str(r.get("artist") or ""),
                "duration_sec": dur,
                "file_name": "",
                "fp_id": str(r.get("fp_id") or ""),
                "file_md5": str(r.get("file_md5") or ""),
            }
        except Exception:                                        # noqa: BLE001
            return None

    def _cue_track_query_by_sid(self, sid):
        """**合成 `song_id`**（码表包带来的指纹）→ 认歌查询参数；不是合成 id ⇒ `{}`。

        返回键与 `_cue_track_query(path)` **完全一致**（`fp_id` / `title` / `artist` /
        `duration_sec` / `file_name`），这样 `_cue_auto_bind(**q)` 两条路径走同一段代码，
        不会分叉。`file_name` 恒为 `""`：这类歌本机本来就没有文件，猜文件名只会误配。

        ⚠ 只有 `_cue_song_path(sid)` 返回空（= `cfg["music_library"]` 里没有这个 id）时
          才会被用到；有文件的老路径行为**逐值不变**。
        """
        try:
            want = int(sid)
        except (TypeError, ValueError):
            return {}
        for fp_id, rec in self._cue_fp_songs().items():
            if not isinstance(rec, dict):
                continue
            try:
                if int(rec.get("song_id", -1)) != want:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                dur = float(rec.get("duration") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            return {
                "fp_id": str(fp_id),
                "title": str(rec.get("title") or ""),
                "artist": str(rec.get("artist") or ""),
                "duration_sec": dur,
                "file_name": "",
            }
        # ★ 2026-11-17：导入库的歌（有 sid、没有本机文件、也不在 fp_songs 里）
        #   用 imported_meta.db 的 title/artist/dur 走 match_track ③/④ 级。
        _im = getattr(self, "_cue_imported_meta", None)
        return (_im(want) if callable(_im) else {}) or {}

    def _cue_beat_len_for(self, path, use_track=True):
        """⚠ **诊断用，不要再用它算 `pos_beats`**（2026-10-17 起）。

        算位置必须用 `_cue_pos_beat_len()` —— 它直接读 `self._cue_rt.beat_len`，与
        `CueRuntime.update()` 消费 `pos_beats` 时用的是**同一个对象**，结构上不可能不一致。
        本方法是"四级回退"版本的旧实现：bpm=0 的码表上它会退到曲库网格 bpm，
        而 `CueRuntime.beat_len` 是 1.0 ⇒ 位置被放大 2.5 倍、画面提前一大截。
        保留原因：它把"这条曲子在本机有哪些 bpm 来源"打印出来很有用（排障/单测）。

        ---- 以下为原说明 ----
        该曲子的拍长（秒/拍）。优先码表曲目里的 bpm，其次曲库元数据；都没有 ⇒ 1.0。

        ★ 四级取值（可靠性从高到低）：
          ① `self._cue_track`：装表时 `match_track()` **五级链**（md5 → 指纹 → meta →
             名+艺人 → 文件名）认定出来的那一条，`set_cue_sheet(track=...)` 时记下。
             它**不依赖本机文件名**——码表里写的是对方的文件名，用户改名/换目录后
             basename 猜不到，旧实现就会退回 `music_meta` 的 bpm（**拍长错 ⇒ 画面整体不对拍**）。
             ⚠ 这里**必须是码表自己的 bpm**：`pb`（拍位）要喂给 `rt.update()` 与码表里
               `at_beats` 直接比较，两端必须同一坐标系；用本机网格 bpm（可能 155.03 vs
               155.0，甚至半/双速检测差）会让码表相对自己的 authored 拍位缓慢漂移。
          ② basename 猜测（老路径）：`_cue_track` 为空但表已装（例如只装了表没认曲目）
             时保留，行为与改动前一致。
          ③ `music_meta`：先 `meta["bpm"]`（旧键/手工填的），再 `meta["grid"]["bpm"]`
             —— ★ 实测真实曲库**只有 `grid` 里那份**（`meta["bpm"]` 从来不存在），
             所以只读 `meta["bpm"]` 等于永远拿不到 ⇒ 退化 1.0。
        `use_track=False`：**换歌那一拍**专用 —— 那时 `_cue_track` 还是上一首的
          （装表发生在 `feed()` 之后的 `_cue_auto_bind` 里），用它的 bpm 会按错拍长
          算出拍位（换歌瞬间闪一下 + 一次多余 seek）。
        ⚠ `_cue_track` 的复位点（至少两处，铁律 11）：
          `__init__`（None）、`set_cue_sheet()`（先 clear 再赋值）、`clear_cue_sheet()`（None）
          ⇒ 换歌/卸表不会留下上一首的 track 污染下一首。
        """
        bpm = 0.0
        if use_track:
            try:
                _ct = getattr(self, "_cue_track", None)
                if _ct is not None:
                    bpm = float(getattr(_ct.identity, "bpm", 0.0) or 0.0)
            except Exception:                                    # noqa: BLE001
                bpm = 0.0
        if bpm <= 0.0:
            try:
                if self._cue_sheet is not None:
                    for t in (getattr(self._cue_sheet, "tracks", None) or []):
                        if os.path.basename(str(getattr(t.identity, "file_name", "") or "")) \
                                == os.path.basename(path or ""):
                            bpm = float(getattr(t.identity, "bpm", 0.0) or 0.0)
                            break
            except Exception:                                    # noqa: BLE001
                bpm = 0.0
        if bpm <= 0.0:
            try:
                m = (self.cfg["music_meta"] or {}).get(path) or {}
                bpm = float(m.get("bpm") or 0.0)
                if bpm <= 0.0:
                    g = m.get("grid")
                    if isinstance(g, dict):
                        bpm = float(g.get("bpm") or 0.0)
            except Exception:                                    # noqa: BLE001
                bpm = 0.0
        return (60.0 / bpm) if bpm > 0 else 1.0

    def _cue_match_track_local(self, query):
        """在**当前已装的**码表里按 `query` 重新认曲目 ⇒ `(是否就是当前曲目, 认到的 TrackEntry)`。

        为什么要它：同一张 `.ercue` 里有多首歌时，换歌必须重绑曲目（见 `_cue_auto_bind`）。
        这里只查已加载的 `_cue_sheet`，**不读盘、不解压**。
        """
        tr_cur = getattr(self, "_cue_track", None)
        sheet = getattr(self, "_cue_sheet", None)
        if sheet is None:
            return False, None
        try:
            q = query or {}
            tr, _lvl = sheet.match_track(
                title=str(q.get("title", "") or ""),
                artist=str(q.get("artist", "") or ""),
                duration_sec=float(q.get("duration_sec", 0.0) or 0.0),
                fp_id=str(q.get("fp_id", "") or ""),
                file_name=str(q.get("file_name", "") or ""),
                file_md5=str(q.get("file_md5", "") or ""))
        except Exception:                                        # noqa: BLE001
            return False, None
        if tr is None:
            return False, None
        return (tr is tr_cur), tr

    def _cue_rebind_track(self, tr):
        """**同一张码表内换曲目**：只重建运行时，一个解码器都不关。

        为什么不直接 `set_cue_sheet()` 重装：那条路会 `clear_cue_sheet()` ⇒ 把该图层
        所有码表解码器关掉（下次要重开，70~690ms 首帧黑）——换歌时完全没必要。
        """
        if tr is None:
            return False
        self._cue_track = tr
        self._cue_bound_cache = None
        self._cue_bad_song_n = 0
        self._cue_bad_song_t = 0.0
        self._cue_rt = None
        try:
            import cuesheet_runtime as _cr
            self._cue_rt = _cr.CueRuntime(self._cue_sheet, tr,
                                          media_paths=self._cue_media)
        except Exception as e:                                    # noqa: BLE001
            self._cue_health("换曲目失败（%s）：%s: %s"
                             % (getattr(getattr(tr, "identity", None), "title", "?"),
                                type(e).__name__, e))
            self._cue_rt = None
        # 片段全换了 ⇒ seek 状态（落地/降级/节流）必须清空，否则新片段继承旧状态
        try:
            if getattr(self, "_cue_policy", None) is not None:
                self._cue_policy.reset()
        except Exception:                                         # noqa: BLE001
            pass
        return self._cue_rt is not None

    def _cue_auto_bind(self, path, query):
        """**只跟随已装载的那张码表**（用户 2026-10-30：「灰掉的不会应用到演出的扫描对齐中」）。

        规则（与旧版的差别在第 3 条，这是本次改动的全部意义）：
          1. 已装载表 + 曲目属于它     → 只重建运行时（`_cue_rebind_track`，不关解码器）
          2. 已装载表 + 曲目不属于它   → **停用编排**（`_cue_unbind_track`），**不换表**
          3. **没装载任何表**          → **不自动装**（旧版会遍历全库挑一张装上）
          4. 不再写 `cfg["cue"]["prefer"]`：没有"自动挑表"这一步了
             （`choose_sheet()` 仍供码表页的手动多选使用）

        为什么必须改：旧版第一行是 `hits = self.cue_sheets_for_track(**query)` ——
        **全库遍历所有码表**，命中就 `set_cue_sheet()`。曲库里躺 5 张表的歌时，
        放一首只属于"灰表"的歌，那张表会被自动装上并接管画面 ⇒ 演出中随时可能
        被别的表抢走对齐，现场不可控（用户原话见上）。

        ⚠ 代价（用户已知并接受）：忘记在码表页装载/绑定曲目时，放歌**没有码表编排
          画面**，走普通能量驱动的自动切换。码表页「装载 / 绑定曲目」是唯一表来源。
        """
        if getattr(self, "_cue_sheet", None) is None:
            return False                       # 3. 没表 ⇒ 不自动装（严格隔离）
        _same, _tr = self._cue_match_track_local(query)
        if _same:
            return True                        # 1a. 表与曲目都对 ⇒ 什么都不做
        if _tr is not None:
            return self._cue_rebind_track(_tr)  # 1b. 同一张表内换曲目
        # 2. 这首歌不属于当前表 ⇒ 停用编排，绝不拿上一首的去对这首
        self._cue_unbind_track()
        return False

    def _cue_unbind_track(self):
        """停用当前**曲目**的编排（**不卸表**）：表还挂着，等下一首属于它的歌。

        为什么单独一个方法：`clear_cue_sheet()` 会把该图层的码表解码器全关掉
        （下次重开要 70~690ms 首帧黑），而"这首不在本表里"只是暂时没词，
        表本身还是演出中的那张 ⇒ 只清曲目运行时，不动表与素材。

        清的全是**曲目级**状态，与 `_cue_rebind_track()` 的复位项对齐（铁律 11：
        新增状态必须有 ≥2 处独立复位 —— 这里与 `set/clear_cue_sheet`、`__init__` 三处）。
        """
        try:
            self._cue_track = None
            self._cue_bound_cache = None
            self._cue_rt = None
            self._cue_bad_song_n = 0
            self._cue_bad_song_t = 0.0
        except Exception:                                        # noqa: BLE001
            pass
        try:
            if getattr(self, "_cue_policy", None) is not None:
                self._cue_policy.reset()
        except Exception:                                        # noqa: BLE001
            pass
        return True

    def _cue_auto_position(self):
        """低频轮询指纹识别结果 → 拍位（**0.2s 一次，绝不进 `_compose`**）。

        返回本次的 `(status, pos_beats)`（供自检/日志；正常运行不看返回值）。
        """
        if getattr(self, "_cue_manual_clock", False):
            return ("manual", None)
        now = time.perf_counter()
        if now < float(getattr(self, "_cue_pos_next_t", 0.0) or 0.0):
            return ("skip", None)
        self._cue_pos_next_t = now + 0.2
        # ⚠⚠ 2026-10-17 根因：这里原本是 `getattr(getattr(self, "audio", None), "state", None)`。
        #   但 `self.audio` **就是** AudioState（MainWindow 传的是 `self.audio.state`），它没有
        #   `.state` 属性 ⇒ 恒 None ⇒ 下面每个字段都取到默认值 ⇒ `sid=-1 votes=0 off=-1`
        #   ⇒ 状态永远 `lost` ⇒ **码表图层从导入那一刻起就没演出过**。
        #   取证的铁证：同一秒里 `[R] cur=346225955`（认歌线程确认了）而 `_cue_diag` 写
        #   `sid=-1 votes=0 silent=-1.0s` —— `silent=-1.0` 是"对象不存在"的默认值，
        #   不是"没声音"（真没声音时 `[R]` 会是 silent>0，这里是 0.0）。
        st = getattr(self, "audio", None)

        # ⚠ 不能写 `int(x or -1)`：`song_id == 0` 是**合法**的（第一首），`0 or -1` 会变成 -1
        #   ⇒ 表现为"明明识别到了，却一直当没歌"。这类 bug 只在"歌的 id 恰好是 0"时出现。
        def _i(v, d):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d

        def _f(v, d):
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        sid = _i(getattr(st, "recognized_song_id", -1), -1)
        # ★★ 2026-10-17：`state.recognized_song_id` 会被**逐拍覆写**（换曲判定/异常路径都可能
        #   让它短暂回 -1），而本函数是 0.2s 采样的旁路 ⇒ 采到 -1 就整拍不接管
        #   （现场 `sheet=1 track=None rt=0`，本地已复现）。
        #   电荷条引擎里的 `current_song_id` 才是**确认后稳定**的那个值 ⇒ 作为兜底。
        if sid < 0:
            _eng2 = getattr(self, "_audio_engine", None)
            _cur2 = getattr(_eng2, "current_song_id", None)
            if _cur2 is None:
                _cur2 = getattr(getattr(_eng2, "_recognizer_engine", None),
                                "current_song_id", None)
            if _cur2 is not None:
                try:
                    sid = int(_cur2)
                except (TypeError, ValueError):
                    sid = -1
        off = _f(getattr(st, "recognized_offset", -1.0), -1.0)
        votes = _i(getattr(st, "recognized_votes", 0), 0)
        rate = _f(getattr(st, "recognized_rate", 1.0), 1.0)
        # ★★★ 2026-11-13（用户：音乐停了十几秒，码表还在演）：
        #   freewheel 原本只看 `silent_sec <= 3`，但采集线程在“播放器暂停/没有新 block”
        #   时 `silent_sec` 可能根本不增长（现场日志卡在 0.0~4.3s）⇒ 码表按最后位置
        #   继续演最多 30s。这里用**引擎侧连续低电平**兜底：真的没声音 2s 就停，
        #   不依赖音频线程有没有正常送出 silent 计数。
        _lvl_now = _f(getattr(st, "level", 0.0), 0.0)
        _sil_now = _f(getattr(st, "silent_sec", 0.0), 0.0)
        _low_since = float(getattr(self, "_silent_low_since", 0.0) or 0.0)
        _low_dur = (now - _low_since) if _low_since > 0.0 else 0.0
        _silent_eff = max(_sil_now, _low_dur)
        if _silent_eff >= CUE_STOP_SILENT_SEC:
            self._cue_fw_pos = None
            self._cue_fw_t = 0.0
            self._cue_fw_on = False
            try:
                self._cue_pos_tracker.reset()
            except Exception:                                # noqa: BLE001
                pass
            self._cue_pos_song = -1
            self._cue_diag(-1, 0, off, "lost", None, now)
            self.set_cue_position(None, "fp")
            return ("lost", None)
        # ★★★ 2026-10-18（用户："码表视频还是会慢 跟不上音频"）——**用节拍钟 BPM 校正倍率**。
        #   现场取证（用户自己的 cue_health.log，西行谣）：
        #     · `pb` 74 秒只推进 172.1 拍 = 72.7 秒内容 ⇒ **0.983×，累积慢 1.3 秒**
        #     · 同一段时间 `r=0.970`，而节拍钟全程稳定 `bpm=142.5`（码表 142）
        #   根因：指纹 `off` 是**约 4 秒一格的阶梯**（低票时确认慢），拿它求斜率噪声大且偏低
        #   ⇒ `r` 掉到 0.97 ⇒ 码表既按 0.97 外推、播放器也按 0.97 播 ⇒ **视频比音频慢 1.7%**。
        #   ⚠⚠ 2026-10-18 更深的复盘：**用 BPM 校正这条路是错的**，已停用（原 `_cue_rate_with_bpm` 已删除）。
        #   原因：节拍钟读的是**文件真实 tempo**（143.5），而码表里写的是 142 —— 两者本来就可能
        #   不一致（码表 BPM 是用户填的/估算的）。拿 `bpm / 码表bpm` 当倍率会凭空多出 1%，
        #   真机上实测把位置推快了 1%，累积 3.6 秒。⇒ 倍率只能来自**指纹 offset 自己的斜率**
        #   （它是文件自身时间轴，与码表 BPM 无关）。
        # rate 只用指纹估计（见上）
        # ★★★ 2026-10-18（第四稿，真机反复量出来的结论）：**倍率死区**。
        #   指纹 offset 是 2~4 秒一格的阶梯，1 分钟内估出来的倍率噪声约 **±3%**
        #   —— 和"1% 级的速度偏差"同量级，再精调参数只会让它在 0.94~1.09 之间摆。
        #   ⇒ |r−1| ≤ 5% 一律按**原速**处理（DJ 推子通常就在 0；真 ±10%/±20% 变速远超死区，
        #     照常跟）。这样"稳定播放"时倍率恒为 1.000，不会被噪声带着慢慢漂。
        path = self._cue_song_path(sid)
        trk = self._cue_pos_tracker
        rate = rate if rate > 0 else 1.0
        self._cue_rate_raw = rate               # 取证：未经死区的原始估计
        # ★★★ 2026-11-11（用户：码表演出和音乐差好几秒）：
        #   现场 `r=1.000` 但音乐实际是 142.5/140 = **1.018×**；旧死区 ±10% 把
        #   1.018 硬按回 1.000 ⇒ 码表每秒慢 1.8% ⇒ 150 秒后差 2.7 秒，
        #   正是用户看到的“好几秒严重偏差”。
        #   现在：死区收紧到 ±0.2%，并做慢 EMA（真实变速几秒内跟上，单窗噪声不抖）。
        #   ⚠ 倍率仍只来自指纹 offset 斜率（不用 BPM 比值，避免码表 BPM 填错时反向带偏）。
        _raw = max(0.5, min(2.0, float(rate)))
        try:
            if sid >= 0 and int(sid) != int(getattr(self, "_cue_pos_song", -1) or -1):
                self._cue_rate_ema = 1.0        # 换歌：不继承上一首的倍率
        except (TypeError, ValueError):
            pass
        _prev_rate = float(getattr(self, "_cue_rate_ema", 1.0) or 1.0)
        if not (0.5 <= _prev_rate <= 2.0):
            _prev_rate = 1.0
        _alpha = 0.35 if abs(_raw - _prev_rate) > 0.004 else 0.08
        _ema = _prev_rate + (_raw - _prev_rate) * _alpha
        self._cue_rate_ema = _ema
        trk.rate = 1.0 if abs(_ema - 1.0) <= CUE_RATE_DEADBAND else _ema
        # ★★★ 拍长必须与「真正消费 pos_beats 的那个 runtime」**同一来源**（2026-10-17 复盘）
        #
        # 为什么 BPM 本身不影响对拍：整条链上它是**纯单位换算**，两端用同一个常数就完全抵消。
        #   · 编排端模型内部坐标是**秒**（`CueItem.at_sec` 权威），导出时才用**当前** bpm
        #     换算成文件里的 `at_beats = at_sec * bpm/60`；
        #   · 演出端 `pos_beats = offset_sec / beat_len`（`CuePositionTracker.pos_beats_at`）；
        #   · `CueRuntime.update()` 又用**同一个** `bl` 把 `(pos - at_beats)` 换回秒。
        #   ⇒ 拍位与 at_beats 同时被同一个系数缩放，比较时互相抵消。
        #   所以 BPM 填错 3、留 0、甚至一首歌里有几段不同 BPM，都**不会**让画面对不上
        #   （位置来自指纹算出的**原曲秒数**，不是靠拍钟累加推出来的）。
        #
        # 真正会坏的是**两个 beat_len 不一致**。旧写法用 `_cue_beat_len_for()` 做四级回退
        #   （码表 bpm → 文件名猜 → meta.bpm → grid.bpm），而 `CueRuntime.beat_len` 只用
        #   **码表自己的 bpm**（bpm≤0 ⇒ 1.0）。于是「bpm=0 的码表」上：
        #     位置按 60/149.99 = 0.40 算、片段按 1.0 选 ⇒ 拍位被放大 2.5 倍
        #     ⇒ 排在第 191 秒的片段在第 76 秒就触发（提前 115 秒）。
        #   ⚠ 这与"用户没填 BPM"无关，是本模块**自己两处来源不一致** —— 上一轮把它误判成
        #     "BPM 没填就会不对拍"，还加了个回退把 bug 放大，这里一并纠正。
        #
        # 修法：直接用 `self._cue_rt.beat_len`。它就是下面 `update()` 的那个对象，
        #   结构上不可能不一致；表没装（rt 为 None）时给退化值 1.0（此时也没有片段可选）。
        bl = self._cue_pos_beat_len() if (sid >= 0 and votes > 0) else None
        # ★★ 2026-11-11（用户：码表演出和音乐差好几秒）：`recognized_offset` 对应的
        #   是**识别窗末端**（`state.recognized_t`），而本次轮询的 now 可能已经晚了
        #   几十毫秒到两秒（匹配/BPM 校验/线程调度）。旧写法直接把旧窗口位置锚在
        #   now 上 ⇒ 整条码表滞后“处理耗时”。
        #   这里把这段延迟按倍率补回 offset，再照常用 now 作为挂钟时间喂给 tracker
        #   —— 不碰 tracker 的 STALE 保护，也不会回退到比上次更早的时间。
        #   上限 2s：超过说明不是处理延迟而是识别断档，交给 freewheel 处理。
        _t_rec = _f(getattr(st, "recognized_t", 0.0), 0.0)
        _lat = 0.0
        try:
            if _t_rec > 0.0 and _t_rec <= now:
                _lat = min(2.0, float(now) - float(_t_rec))
        except (TypeError, ValueError):
            _lat = 0.0
        _off_feed = off
        if sid >= 0 and off >= 0.0 and _lat > 0.0:
            try:
                _off_feed = float(off) + _lat * max(0.5, min(2.0, float(trk.rate or 1.0)))
            except (TypeError, ValueError):
                _off_feed = off
        status, pb, song = trk.feed(sid, _off_feed, votes, now, beat_len=bl)
        if status in ("ok", "switch") and song != self._cue_pos_song:
            self._cue_pos_song = song
            q = self._cue_track_query(path) if path else {}
            # ★ 本机没有这个文件（= 码表包里带来的指纹 ⇒ 合成 id，`_cue_song_path` 返回 ""）：
            #   改用 `cfg["cue"]["fp_songs"]` 反查认歌参数，再走**同一个** `_cue_auto_bind()`。
            #   不这么做的话 `q` 恒为空 ⇒ 不会自动选表 ⇒ 用户看到"导入了码表却完全不动"。
            if not q and sid >= 0:
                q = self._cue_track_query_by_sid(sid)
            if q:
                self._cue_auto_bind(path, q)     # 换歌 ⇒ 换表/卸表（策略见 docstring）
                # ★ 装表可能**刚换了拍长**（新表曲目 bpm 与旧表不同；或首次绑定时 rt 原先为
                #   None ⇒ bl 用了退化 1.0）⇒ 必须用**新**的 `rt.beat_len` 重算本帧拍位。
                #   否则这一帧的 pos 按旧拍长算、却喂给新 runtime 消费 ⇒ 换歌那一帧选错片段。
                if bl is not None:
                    nb = self._cue_pos_beat_len()
                    if abs(float(nb) - float(bl)) > 1e-9:
                        bl = nb
                        trk.set_beat_len(nb)
                        pb = trk.pos_beats_at(now)
        # ★★ 认错歌的护栏（用户 2026-10-17 追问「识别错了怎么办」）：
        #   指纹偶尔会认成另一首歌（重复段落 / 翻唱 / 同一首的不同版本）。若照跟，绘出来的
        #   位置是**那首歌**的秒数，码表会整段乱跳。这里要求"识别到的 song_id 必须**属于**
        #   这张码表这条曲目的可接受集合"（`_cue_bound_song_ids()`：包里那版 + 曲库里同音乐
        #   的其它版本）；不属于 ⇒ 本帧不接管（保留上一帧），并限流记日志。
        #   ⚠ 换歌流程不受影响：`_cue_auto_bind` 已在上面把表换成新歌的那张，
        #     `_cue_bound_song_ids()` 随之变成新歌 ⇒ 守卫自动放行。
        #   ⚠ 集合为空（老码表没 fp_id、文件名也对不上）⇒ **不启用守卫**，
        #     退回到"照跟"的旧行为，绝不让护栏变成"永远不跟"的新故障。
        # ★★ 2026-10-17（铁律 0）：**已认出歌但表/曲目仍未绑上** ⇒ 每 2s 重试一次。
        #   一次绑定失败（包刚移动/索引未就绪/瞬时异常）不能永久停 —— 否则用户看到
        #   `sheet=1 track=None rt=0` 一直不动，且没有任何日志。
        if (song >= 0 and self._cue_rt is None and status != "lost"
                and now >= float(getattr(self, "_cue_rebind_t", 0.0) or 0.0)):
            self._cue_rebind_t = now + 2.0
            _q2 = self._cue_track_query(path) if path else self._cue_track_query_by_sid(song)
            if _q2:
                self._cue_auto_bind(path, _q2)
        pb = self._cue_accept_pos(sid, pb)
        # ★★ 自由运行（freewheel）：丢票不立刻卸画面，先按最后速率外推一小段
        _fw = False
        if pb is not None:
            try:
                self._cue_fw_pos = float(pb)
                self._cue_fw_t = now
                self._cue_fw_rate = float(rate) if rate and rate > 0 else 1.0
            except (TypeError, ValueError):
                self._cue_fw_pos = None
        elif (getattr(self, "_cue_fw_pos", None) is not None and self._cue_rt is not None
                and self._cue_track is not None):
            _dt = now - float(self._cue_fw_t or 0.0)
            # ★ 释放条件只有三个（等于"真丢了"）：没歌/换歌、静音太久、超过滑行上限。
            #   只要还是同一首、还有声音 ⇒ **继续滑行**，绝不因为"识别卡了几秒"就卸画面。
            _silent = _f(getattr(st, "silent_sec", None), 0.0)
            _alive = (sid >= 0) and (_silent <= 3.0) and (_dt <= CUE_FREEWHEEL_MAX)
            if _alive:
                _bl = self._cue_pos_beat_len() or 1.0
                pb = float(self._cue_fw_pos) + _dt * float(self._cue_fw_rate or 1.0) / _bl
                _fw = True
            else:
                self._cue_fw_pos = None      # 真丢 ⇒ 允许卸画面
        self._cue_fw_on = _fw
        self._cue_diag(sid, votes, off, status, pb, now)
        if pb is not None:
            self.set_cue_position(pb, "fp")
        else:
            # 真丢了（超过自由运行窗口）⇒ 不接管；**表与徽标保留**（回来能立刻继续）
            self.set_cue_position(None, "fp")
        return (status, pb)

    def cuesheet_wanted(self):
        """是否**有可见的码表图层**在等位置数据（小眼睛关掉 ⇒ 完全不检测）。"""
        for lay in (getattr(self, "layers", None) or []):
            if getattr(lay, "is_cuesheet", False) and getattr(lay, "visible", True):
                return True
        return False

    def _rebuild_cue_media_snap(self, sheet):
        """按码表包重建 `_cue_media_snap` + `_cue_thumb_snap`（装表时调用一次）。

        ⚠ 顺序取**包内 `sheet.media` 的顺序**并按路径去重 —— 同一个素材在多个片段里
          复用是常态，不去重会出现一堆重复格子。
        ⚠ 这里同时清空 `_cue_active_snap`：刚装上表时 nothing is playing yet。
        ★★★ 2026-11-06（用户：「素材缩略图不是已经打包在码表导出包里面了吗」）：
          是 —— `.ercue` 里有 `thumbs/<clip_id>.png`，`cuesheet.load()` 已经幂等解到
          `<包>.thumbs/` 并填好 `sheet.thumbs`。所以这里顺手建一张
          `{素材路径(normcase): 缩略图 png 路径}` 快照，图层行**直接用包内小图**，
          不再去解码视频重新生成（那样又慢、又可能和码表里看到的不是同一帧）。
          老包没有 `thumbs/` ⇒ 快照为空 ⇒ 退回"后台补生成"（行为不变）。
        """
        out, thumbs, seen = [], {}, set()
        media = (getattr(self, "_cue_media", None) or {})
        for m in (getattr(sheet, "media", None) or []):
            cid = str(getattr(m, "clip_id", "") or "")
            p = media.get(cid)
            if not p:
                continue
            k = os.path.normcase(str(p))
            if k in seen:
                continue
            seen.add(k)
            out.append(str(p))
            try:
                tp = str(sheet.thumb_path(cid) or "")
                if tp and os.path.isfile(tp):
                    thumbs[k] = tp
            except Exception:                                     # noqa: BLE001
                pass
        self._cue_media_snap = tuple(out)
        self._cue_thumb_snap = thumbs
        self._cue_active_snap = frozenset()
        return self._cue_media_snap

    def cue_thumb_map(self):
        """码表包内自带的素材缩略图：`{素材路径(normcase): png 绝对路径}`。

        只读一次引用（`_rebuild_cue_media_snap` 整体替换，绝不原地改）⇒ 无需加锁
        （铁律 13）。返回副本，调用方随便用。
        """
        d = getattr(self, "_cue_thumb_snap", None)
        return dict(d) if isinstance(d, dict) else {}

    def cue_media_paths(self):
        """码表图层要显示在行里的素材路径（按**码表包内顺序**去重）。

        ★★★ 2026-11-06（用户：「码表图层在右边把这个码表里面缩略图显示出来」）：
          码表图层的素材来自 `.ercue` 包，**不在** `lay.clips` 里 —— 所以这一行的素材格
          一直是空的。这里返回装表时算好的**无锁快照**（`_cue_media_snap`）。
        ⚠ 只读一次引用赋值，不遍历任何"可能正在变"的结构（铁律 13）。
        """
        return list(getattr(self, "_cue_media_snap", ()) or ())

    def cue_active_media(self):
        """此刻**正在使用**的码表素材路径集合（已 normcase，供格子打黄底）。

        数据源是 `_cuesheet_tick` 每帧整体赋值的 `_cue_active_snap`（frozenset 赋值在 GIL
        下原子），GUI 侧不会看到"半更新"的中间态。
        """
        return set(getattr(self, "_cue_active_snap", frozenset()) or ())

    def _cue_set_active(self, lay, paths):
        """把该层缓存里的播放器按需激活（**两个缓存都必须遍历**）。

        ★★ 这是 2026-09-30「12 核跑满 / 磁盘 750MB/s」事故的**根因防线**：
          码表图层的解码器存在 `lay._cue_players`（**不是** `lay.cache`）。
          只遍历 `lay.cache` ⇒ "一旦激活就再没人关" ⇒ 每碰过一个素材就多一个解码器
          在后台一直解码/一直读盘，越用越多、互相抢 IO。
        """
        for cache in (getattr(lay, "cache", None), getattr(lay, "_cue_players", None)):
            if not isinstance(cache, dict):
                continue
            for pth, pl in list(cache.items()):
                try:
                    pl.set_active(pth in paths)
                except Exception:                                 # noqa: BLE001
                    pass

    def _canvas_screen(self, canvas):
        """从画布对象反查它是哪块屏的（认不出 ⇒ None，调用方按全局处理）。"""
        try:
            if canvas is None or canvas is self.canvas:
                return output_model.main_screen(self._out_cfg) if self._out_cfg else None
            for sid, cv in (getattr(self, "_screen_canvases", None) or {}).items():
                if cv is canvas:
                    return output_model.screen_by_id(self._out_cfg, sid)
        except Exception:                                          # noqa: BLE001
            pass
        return None

    def _cuesheet_on(self, screen=None):
        """码表图层本帧是否在接管画面（1.0 / 0.0）。

        ★ BUG-G（2026-11-10）：多屏下必须**按屏**判断。以前只要**任意一屏**有码表在接管
          就返回 1 ⇒ 所有屏的调色 + 后处理全被跳过（用户"颜色渲染失效"的真因）。
          `screen` 为 None 时保持原行为（全局任意一屏），不影响别处调用。

        用户 2026-09-28：「码表图层和 Kv 图层一样，不受画面效果的影响」——
        码表里是 DJ 做好的成片，再叠调色/特效就不是原片了 ⇒ 接管期间后处理整条跳过。
        """
        _sid = str((screen or {}).get("id") or "") if screen else ""
        for lay in (getattr(self, "layers", None) or []):
            if not (getattr(lay, "is_cuesheet", False) and getattr(lay, "cs_active", False)):
                continue
            if screen is None:
                return 1.0                       # 兼容：不传屏 ⇒ 原来的"任意一屏"语义
            try:
                sc = self._layer_screen(lay)
                if sc is not None and str(sc.get("id") or "") == _sid:
                    return 1.0
            except Exception:                                      # noqa: BLE001
                return 1.0
        return 0.0
        return 0.0

    def set_cue_seek_profile(self, name):
        """设置「定位档位」：`auto` / `conservative` / `off`（立即生效，并落盘）。

        · `conservative`：**只入场 + 预热**，不做运行中漂移校正 ⇒ seek 次数显著减少（抖动更少）；
        · `off`：完全不 seek（现场排障用）。
        """
        n = str(name or "auto")
        if n not in ("auto", "conservative", "off"):
            n = "auto"
        try:
            self.cfg["cue"]["seek_profile"] = n
            self.cfg.save()
        except Exception:                                        # noqa: BLE001
            pass
        pol = getattr(self, "_cue_policy", None)
        if pol is not None:
            try:
                pol.cfg["seek_profile"] = n
            except Exception:                                    # noqa: BLE001
                pass
        return n

    def set_cue_position(self, pos_beats, src="manual"):
        """喂入本帧的**码内拍位**（Step3 的指纹定位会把结果写到这里）。

        `None` ⇒ 表示"没有可靠位置" ⇒ 码表不接管（安全降级，绝不猜）。
        本方法 O(1)：只赋值，不做任何计算。
        """
        self._cue_pos_beats = None if pos_beats is None else float(pos_beats)
        self._cue_pos_src = str(src or "")
        return self._cue_pos_beats

    def _cue_health(self, msg):
        """码表降级/失败写进图层健康日志（滚动保留），**限流**：默认 10s 最多 1 条。"""
        now = time.perf_counter()
        if (now - float(getattr(self, "_cue_err_at", 0.0) or 0.0)) < 10.0:
            return
        self._cue_err_at = now
        try:
            log = self._health_log
            if log is None:
                log = self._health_log = _LayerHealthLog()
            log.write("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
        except Exception:                                         # noqa: BLE001
            pass

    def _cuesheet_tick(self, snap=None):
        """每帧：把「当前拍位」解释成「每个码表图层此刻该显示什么」—— **只算，不画**。

        三个"不接管"条件（任一 ⇒ 清空 `cs_states`，退回普通图层行为）：
          ① 没有装载码表 / 没认到曲目（`_cue_rt is None`）；
          ② 没有可靠拍位（`_cue_pos_beats is None`）—— 宁可不画，也绝不猜位置；
          ③ 没有可见的码表图层（小眼睛关掉 ⇒ 连算都不算）。
        **零副作用**：只调纯逻辑 `CueRuntime.update()`，不建解码器、不 seek、不做 IO（那些是 S6 的事）。
        **异常安全**：内部任何异常都不冒泡（否则一帧崩=全线崩）⇒ 限流记一条 + 本帧清空 +
        下一帧继续尝试（不做永久关闭）。
        """
        lays = [l for l in self.layers if getattr(l, "is_cuesheet", False)]
        if not lays:
            return                                    # ★ 没有码表图层：零成本直接返回
        vis = [l for l in lays if getattr(l, "visible", True)]
        rt = self._cue_rt
        pos = self._cue_pos_beats
        # ★★★ 2026-10-18（用户："识别的延迟问题，给码表层演出加 0.5s 的提前"）
        #   放在这里（`update()` 的唯一入口）而不是位置层：
        #     · 这里的 `rt.beat_len` 一定可用（退化 0 的情况已排除），换算准确；
        #     · 只影响"演出"（选片段 + play_sec + 播放器落点），不污染位置层的误差计算。
        #   要调就改 `CUE_LEAD_SEC`（秒）。
        if pos is not None:
            try:
                _bl1 = float(getattr(rt, "beat_len", 0.0) or 0.0)
                if _bl1 > 1e-6:
                    pos = float(pos) + float(CUE_LEAD_SEC) / _bl1
            except (TypeError, ValueError):
                pass
        # ★ 2026-10-18（真机联调）：记住"本帧为什么没接管"，供 `_cue_diag` 输出。
        #   码表图层**闪出普通图层**只有三条路径：没表 / 没位置 / 没可见层。
        #   不把这条记下来，现场只能看到 `sub=0`，分不清是哪一种。
        if rt is None:
            self._cue_tick_why = "no_rt"
        elif pos is None:
            self._cue_tick_why = ("no_pos_fw" if getattr(self, "_cue_fw_on", False)
                                  else "no_pos")
        elif not vis:
            self._cue_tick_why = "hidden"
        else:
            self._cue_tick_why = ""
        if rt is None or pos is None or not vis:
            for lay in lays:
                if lay.cs_states:
                    lay.cs_states = []
                lay.cs_active = False
                self._cue_set_active(lay, set())   # ★ 不接管 ⇒ 全部失活（别让解码器白烧 CPU）
            self._cue_active_snap = frozenset()    # ★ 快照同步清空（GUI 黄底随之熄灭）
            return
        try:
            # ★ 2026-10-18：把**输入音乐的播放速率**一并传进去 —— 码表把播放器倍速
            #   写成 `it.speed × 它`，于是"位置源"和"播放器"共用同一个时间基，
            #   音乐降速/变速时不会结构性越差越远（详见 `CueRuntime.update` 里的注释）。
            #   取不到就是 1.0（与改动前逐值一致）。
            _r = 1.0
            try:
                # ⚠ 必须 `getattr(self, "_cue_pos_tracker", None)` 两层取：
                #   直接写 `getattr(self._cue_pos_tracker, ...)` 在桩对象上没有该属性时
                #   仍然会抛 AttributeError（外层 getattr 的默认值救不了它）。
                _trk = getattr(self, "_cue_pos_tracker", None)
                _r = float(getattr(_trk, "rate", 1.0) or 1.0)
            except (TypeError, ValueError, AttributeError):
                _r = 1.0
            states = rt.update(pos, rate=_r)
            self._cue_jumped = bool(getattr(rt, "jumped", False))
            self._cue_tick_fail = 0
        except Exception as e:                                    # noqa: BLE001
            # ★ 绝不冒泡：清空本帧 + 限流记一次 + 下帧继续试（不永久关）
            self._cue_tick_fail = int(getattr(self, "_cue_tick_fail", 0) or 0) + 1
            self._cue_jumped = False
            self._cue_health("码表解释失败（连续 %d 次，已跳过本帧）：%s: %s"
                             % (self._cue_tick_fail, type(e).__name__, e))
            for lay in lays:
                lay.cs_states = []
                lay.cs_active = False
            self._cue_active_snap = frozenset()   # ★ 解释失败 ⇒ 快照清空（界面不留假高亮）
            return
        on_any = bool(states)
        # ★ 每帧按"当前片段 + 过渡中片段 + 预加载的下一个"算活跃路径 —— 解码器有界的前提
        paths = {ps.clip_path for ps in states
                 if getattr(ps, "active", False) and getattr(ps, "clip_path", "")
                 and not getattr(ps, "missing", False)}
        # ★ 无锁快照：GUI 图层行只读它来打黄底（见 `cue_active_media`）。
        #   frozenset 整体赋值在 GIL 下原子 ⇒ 界面不会读到"半更新"的集合。
        try:
            self._cue_active_snap = frozenset(os.path.normcase(str(_p)) for _p in paths)
        except Exception:                                         # noqa: BLE001
            self._cue_active_snap = frozenset()
        # ★★★ 2026-10-18（用户："码表偶尔会突然消失零点几秒"）——**帧级暗场计时**。
        #   为什么必须单独做：`_cue_diag` 是 **2s 一行**，抓不到"不出画持续了多久"
        #   （88 个采样里那 1 行 `sub=3/0` 到底是 16ms 还是 400ms，完全看不出来）。
        #   这里逐帧累计：码表本该出画（有 rt、有位置、层可见）但**没有任何子层在画**
        #   的连续时长，记最大值 + ≥50ms 的次数。热路径只做几个浮点运算，无锁、无 IO。
        try:
            _now = time.perf_counter()
            # "该出画却没出画"：表已装上（rt 有）、层可见，但没有任何子层在画。
            # ⚠ 必须包含 `pos is None` 的情形 —— 那正是 `_cuesheet_tick` 提前 return、
            #   把 `cs_states` 清空、码表层整块消失（= 切出普通图层）的路径。
            _dark = bool(rt is not None and vis) and not on_any
            if _dark:
                if getattr(self, "_cue_dark_t0", None) is None:
                    self._cue_dark_t0 = _now
            else:
                _t0d = getattr(self, "_cue_dark_t0", None)
                if _t0d is not None:
                    self._cue_dark_t0 = None
                    _dur = _now - float(_t0d)
                    if _dur > float(getattr(self, "_cue_dark_max", 0.0) or 0.0):
                        self._cue_dark_max = _dur
                    if _dur >= 0.05:          # 50ms 以上才计数（1~2 帧属正常过渡）
                        self._cue_dark_n = int(getattr(self, "_cue_dark_n", 0) or 0) + 1
        except Exception:                                     # noqa: BLE001
            pass
        for lay in lays:
            on = bool(getattr(lay, "visible", True)) and on_any
            lay.cs_states = states if on else []
            lay.cs_active = on
            self._cue_set_active(lay, paths if on else set())

    def _cue_item(self, lay, path, xform=None):
        """按路径取（并缓存）**真实 MediaItem**，并把码表的 `xform` 套上去。

        ⚠ 必须是真实 `MediaItem`：`make_player()` 读 `item.kind` / `item._thumb_path()` /
          `item._has_alpha` / `item._is_dxv`，自造极简替身会让工厂抛异常（被吞掉 ⇒ 静默黑屏）。
        ⚠ 同一个素材被多个子层复用时共用一条缓存（`lay._cue_items`）；`img_*` 是**画之前当场设**的
          （绘制是同步串行的，所以安全）。
        """
        if not isinstance(getattr(lay, "_cue_items", None), dict):
            lay._cue_items = {}
        it = lay._cue_items.get(path)
        if it is None:
            try:
                from media_manager import MediaItem   # 局部导入：与引擎里其它调用点一致
                it = MediaItem(path)
            except Exception:                                     # noqa: BLE001
                it = _CueItemStub(path)
            # ★★★ 2026-10-17：码表包里的 `has_alpha` 用来避免
            #   `create_video_player()` 因"本地没有缩略图"而判 False ⇒ 用 OpenCV
            #   解码 ⇒ 透明区变不透明黑（整层盖住下层 = 多层看起来没生效 + 淡出末尾黑屏）。
            # ★ 2026-11-18：只信 **True**。包里的 `False` 可能只是编排端没探测/老包，
            #   照信会把真 alpha 素材解成黑底（ERCUE 预览却正常）；False/缺省都留给
            #   media_manager 的缩略图/AV 探测去判。
            try:
                _al = getattr(self, "_cue_alpha", None) or {}
                if path in _al and bool(_al[path]):
                    it._has_alpha = True
                    # 明确告诉后面的重开判据：这个结论来自包内元数据，不是"猜的"
                    it._cue_alpha_src = "pkg"
            except Exception:                                     # noqa: BLE001
                pass
            lay._cue_items[path] = it
        xf = xform or {}
        try:
            it.img_scale = float(xf.get("scale", 1.0) or 1.0)
            it.img_x = float(xf.get("x", 0.0) or 0.0)
            it.img_y = float(xf.get("y", 0.0) or 0.0)
            it.img_rot = float(xf.get("rot", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        return it

    def _cue_frame(self, lay, ps):
        """取该子层当前应显示的帧。

        ★★ 三段式（团队裁定，**别改**）：
          ① **取数据**：item / player / `position()`（实际显示帧的素材内秒）；
          ② **问 policy**：`settle()` 结算上次 seek、`should_seek()` 决定是否要 seek、
             `on_seek()` 记账、`desired_speed_for()` 取**实时变速对齐**的倍速 ——
             **判断一行都不在这里**；
          ③ **执行**：`player.seek(play_sec)` / `player.set_speed(want)` / 取帧。
        为什么必须这样：`tools/cue_should_seek_test.py` 用 32 个用例 + 5 个源码级变异
        验的就是 `cue_seek_policy` 那套判断。若在这里另写一遍，两套逻辑迟早分叉。
        """
        try:
            item = self._cue_item(lay, ps.clip_path, ps.xform)
            player = self._get_player(lay, item)
        except Exception:                                         # noqa: BLE001
            return None
        if player is None:
            return None
        try:
            player.set_active(True)      # ⚠ 不激活时 current() 恒 None（历史坑：码表层一片黑）
        except Exception:                                         # noqa: BLE001
            pass

        pol = getattr(self, "_cue_policy", None)
        st = None
        # ★★ 2026-10-17：**静态图/动图播放器没有 `position()`**（`ImagePlayer`），
        #   而 policy 每一步都要读它 ⇒ 旧写法每帧抛 AttributeError ⇒ 被下面的 except 吞掉
        #   ⇒ 那条 `码表 seek 策略异常` 日志刷屏，而且这个图层永远拿不到校正。
        #   静态图本来就"定位无意义"，直接跳过整个 policy 段（不是降级，是不适用）。
        # ★★★ 2026-10-18（真机联调）：**解码器回卷检测**。
        #   素材实际长度常比元数据短一点（例：声明 20.0s，实际 19.85s）⇒ 播到 19.85 时
        #   播放器 EOF 回卷到 0，而 `play_sec` 还在 19.9 ⇒ 误差 ~19.8s ⇒ 控制器猛冲/seek
        #   ⇒ **画面抽一下**（真机 `dmax=19.85` 就是这么来的）。
        #   判据：`pos` 比 `play_sec` 落后"接近整段长度"，且 `play_sec` 已接近素材末尾
        #   ⇒ 认定回卷：冻结在末尾，不再去追。
        _dur = 0.0
        try:
            _dur = float(getattr(ps, "dur", 0.0) or 0.0)
        except (TypeError, ValueError):
            _dur = 0.0
        try:
            _p_now = player.position() if callable(getattr(player, "position", None)) else None
        except Exception:                                     # noqa: BLE001
            _p_now = None
        if (_dur > 0.5 and _p_now is not None
                and ps.play_sec > _dur - 0.6
                and _p_now < ps.play_sec - _dur * 0.4):
            ps.play_sec = max(0.0, _dur - 0.2)
            ps.src_ended = True
        _pos_fn = getattr(player, "position", None)
        if pol is not None and not callable(_pos_fn):
            pol = None
        if pol is not None:
            try:
                st = pol.state_for(ps)
                # ★★ 新开/重建的解码器必须**先落地**到本片段当前时刻（见
                #   `_cue_get_player` 里的 `_cue_need_seek`）。不做的话，被逐出或
                #   因 alpha 纠正重建的播放器会从 0 秒开始播 —— 与编排差好几秒。
                if getattr(player, "_cue_need_seek", False):
                    player._cue_need_seek = False
                    # ★ `off` 档（现场应急关闭定位）**连首次落地都不许** —— 档位判断
                    #   只能问 policy，不许在这里另写一套（`cue_settings_test` 钉这条）。
                    if pol.seek_enabled(item):
                        ok0 = False
                        try:
                            # ★ 首次落地也落到"编排目标 + 前馈补偿"（见 sync_target）
                            ok0 = bool(player.seek(
                                pol.seek_target(ps) if hasattr(pol, "seek_target")
                                else ps.play_sec))
                        except Exception:                      # noqa: BLE001
                            ok0 = False
                        pol.on_seek(ps, st, ok0, None)
                pol.settle(ps, player, st)                     # ②-a 结算上次 seek（落地/超时）
                if pol.should_seek(ps, item, player, st):      # ②-b 判定（全部在 policy 里）
                    ok = False
                    try:
                        # ★ 落到"编排目标 + 前馈补偿"（见 `cue_seek_policy.sync_target`），
                        #   不能落到裸 play_sec —— 那样解码管线延迟会一直体现为"画面晚"。
                        _tgt = (pol.seek_target(ps) if hasattr(pol, "seek_target")
                                else ps.play_sec)
                        ok = bool(player.seek(_tgt))           # ③ 只投递，不等待
                    except Exception:                          # noqa: BLE001
                        ok = False
                    pol.on_seek(ps, st, ok, player.position())  # ②-c 记账（带起点，供落地判方向）
                    if not ok:
                        self._cue_health("码表 seek 失败，本片段降级为自由播放：%s"
                                         % ps.clip_path)
                # ②-d 计速：**实时变速对齐**（小幅漂移用倍速追，不再 seek —— 见
                #   `cue_seek_policy` 顶部"实时变速对齐"一节）。`pos` 取实际显示帧，
                #   没有它就只能给名义倍速（旧行为）。
                try:
                    _pos_now = player.position()
                except Exception:                              # noqa: BLE001
                    _pos_now = None
                want = pol.desired_speed_for(ps, item, _pos_now)
                cur = float(getattr(player, "speed", 1.0) or 1.0)
                # 量化到 0.5% 才下发：漂移是连续量，逐帧微调会让 `set_speed` 每帧都响
                # （虽然只是赋值，但没必要；也避免某些播放器在 set_speed 里重置计时器）。
                if abs(cur - want) > max(0.005, abs(cur) * 0.005) and hasattr(player, "set_speed"):
                    player.set_speed(want)                     # ③ 执行
            except Exception as e:                             # noqa: BLE001
                # policy 自身出问题也不许把渲染线程带走
                self._cue_health("码表 seek 策略异常（已跳过本帧的定位）：%s: %s"
                                 % (type(e).__name__, e))

        if not getattr(player, "_cue_warmed", False):
            try:
                player._cue_warmed = True
                player.warm_up()
            except Exception:                                     # noqa: BLE001
                pass

        cache = getattr(lay, "_cue_last_frame", None)
        if not isinstance(cache, dict):
            cache = {}
            lay._cue_last_frame = cache

        # ★ 到出点：**持最后一帧**（不 seek、不越过、不黑）—— 与 Cue 端 timeline A2 一致
        # ⚠⚠ 2026-10-22：**GIF 例外**。GIF 播放器（`GifPlayer`）是"自走时钟 + 内部循环"，
        #   没有 `position()`；一旦这里因为 `src_ended` 返回缓存帧，就会永远停在
        #   **第一帧**（用户实测："码表里的 GIF 一直卡在第一帧"）。
        #   GIF 的正确行为是：整段循环播放（Cue 端已允许拉长/掐头去尾），不参与"到出点冻结"。
        _is_gif_player = bool(getattr(player, "is_gif", False))
        if getattr(ps, "src_ended", False) and not _is_gif_player:
            last = cache.get(ps.lyr_id)
            if last is not None and not last.isNull():
                return last
        try:
            img = player.current()
        except Exception:                                         # noqa: BLE001
            img = None
        if img is None or img.isNull():
            return cache.get(ps.lyr_id)
        cache[ps.lyr_id] = img            # 只存引用，不拷贝（解码线程每帧产出新 QImage）
        return img

    def _cue_sheet_view(self, W, H):
        """码表画布在输出画布里的**等比 contain 区域** → `(w, h, ox, oy)`。

        ★ 2026-11-18：ERCUE 预览按码表自己的 `res_w/res_h` 做 cover/contain；
          ERAVJ 之前直接用输出画布尺寸 ⇒ 4:3/竖屏/2:1 码表在 16:9 输出上
          位置和裁切都会变。现在把码表子层画到同比例的子画布、居中留黑边，
          输出屏比例与码表一致时（w,h)==(W,H)，行为不变。
        """
        try:
            aw, ah = self._cue_sheet.aspect()
            ar = float(aw) / float(ah) if ah else 0.0
        except Exception:                                        # noqa: BLE001
            ar = 0.0
        if ar <= 0.0:
            return W, H, 0, 0
        out_ar = float(W) / float(H) if H else 0.0
        if abs(ar - out_ar) <= 1e-4:
            return W, H, 0, 0
        if ar < out_ar:
            h2, w2 = H, int(round(H * ar))
        else:
            w2, h2 = W, int(round(W / ar))
        w2 = max(2, min(int(W), int(w2)))
        h2 = max(2, min(int(H), int(h2)))
        return w2, h2, (W - w2) // 2, (H - h2) // 2

    def _draw_cuesheet_layer(self, p, lay, W, H, snap, subtract_jobs):
        """把码表图层的所有激活子层画到画布上（按 z 升序 = 后画的在上）。

        **α² 的来源**：`op = 容器层不透明度 × 片段有效不透明度 × alpha`，再把 `trans_p` 作为
        `prog` 传给 `_draw_layer` ⇒ 有效 α = `opacity × alpha × trans_p` = **α²**（`trans_p=a_in`）。
        ⚠ **不要在这里再乘一次 alpha**（会偏暗），也不要改 `prog` 的定义。
        """
        if not getattr(lay, "cs_active", False):
            return
        base_op = max(0.0, min(1.0, float(getattr(lay, "opacity", 1.0) or 0.0)))
        for ps in list(getattr(lay, "cs_states", None) or []):
            if not ps.active or ps.missing or not ps.clip_path:
                continue
            op = base_op * float(ps.opacity) * float(ps.alpha)
            if op <= 0.002:               # 与普通图层同门槛
                continue
            img = self._cue_frame(lay, ps)
            if img is None or img.isNull():
                continue
            item = self._cue_item(lay, ps.clip_path, ps.xform)
            stub = _CueSubLayer(ps.lyr_id, op, item, name=ps.lyr_id)
            tr = ps.trans if ps.trans in ("fade", "cut", "slide", "zoom", "glitch") else "fade"
            prog = 1.0 if tr == "cut" else max(0.0, min(1.0, float(ps.trans_p)))
            # ★★ 必须置 `trans_active = True`（过渡中），否则 `_draw_layer` 会在
            #   `if tr == "beat_cut" or prog >= 1.0 or (not is_kv and not lay.trans_active)`
            #   处**提前返回**，把 tr/prog 全部忽略 ⇒ 过渡退化成"一次性满不透明度"，
            #   有效 α 就从 α² 变成 α（实测差一倍：64 → 127）。备份实现漏了这一句。
            stub.trans_active = bool(prog < 1.0)
            blend = ps.blend if (ps.blend in BLEND_QT or ps.blend == "subtract") else "normal"
            if blend == "subtract":
                # 复用引擎既有"逐像素减少"机制：先画到暂存图，合成完统一相减
                tmp = (getattr(lay, "_cue_sub_tmps", None) or {}).get(ps.lyr_id)
                if tmp is None or tmp.width() != W or tmp.height() != H:
                    tmp = QImage(W, H, QImage.Format_ARGB32)
                    if not isinstance(getattr(lay, "_cue_sub_tmps", None), dict):
                        lay._cue_sub_tmps = {}
                    lay._cue_sub_tmps[ps.lyr_id] = tmp
                tmp.fill(Qt.transparent)
                tp = QPainter(tmp)
                tp.setRenderHint(QPainter.SmoothPixmapTransform)
                self._draw_layer(tp, stub, img, W, H, prog, tr, 1.0, snap, 0.0, 0.0,
                                 crop=ps.crop,
                                 out_dir=bool(getattr(ps, "trans_is_out", False)))
                tp.end()
                subtract_jobs.append((tmp, op))
            else:
                p.setCompositionMode(
                    BLEND_QT.get(blend, QPainter.CompositionMode_SourceOver))
                self._draw_layer(p, stub, img, W, H, prog, tr, 1.0, snap, 0.0, 0.0,
                                 crop=ps.crop,
                                 out_dir=bool(getattr(ps, "trans_is_out", False)))
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            # ★ 每个码表子层画完让出 GIL：3 个子层叠起来也能挡 GUI 十几毫秒
            try:
                time.sleep(0)
            except Exception:                                  # noqa: BLE001
                pass

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
        old_item = (lay.clips[old_idx]
                    if (0 <= old_idx < len(getattr(lay, "clips", None) or [])) else None)
        lay.prev_player = None if instant else old
        lay.prev_idx = old_idx if not instant else -1
        lay._trans_prev_item = None if instant else old_item
        lay.cur = idx
        _item = lay.clips[idx]
        lay.player = self._get_player(lay, _item)
        lay.trans_start = time.perf_counter()
        lay.trans_active = not instant
        lay.last_switch_t = time.perf_counter()
        _log_clip_call(lay, _item)
        lay.recent.append(_item.path)
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
            if not lay.clips:
                continue
            if getattr(lay, "is_kv", False):
                continue    # Kv 待机层不参与手动切素材
            if not getattr(lay, "auto_mode", False):
                # ★ 2026-11-26（用户）：「停止切换素材」只对手动图层生效。
                #   勾选后，主界面「下一素材」/快捷键跳过这一层（Kv 在上面已跳过，
                #   自动层走另一条分支，码表层没有 clips）。
                if bool(getattr(lay, "stop_switch", False)):
                    lay.switch_pos = pos
                    continue
                # ★ 2026-11-26（用户）：手动图层按"下一个素材"前进；
                #   旧版用 `_switch_layer_random` 会随机跳，而且 `fixed>=0` 会被
                #   整层跳过（菜单里已删「停止切换」，不能让它变成死层）。
                _n = len(lay.clips)
                _cur = int(getattr(lay, "cur", 0) or 0)
                if 0 > _cur or _cur >= _n:
                    _cur = 0
                _nxt = (_cur + 1) % _n
                if _nxt != _cur:
                    self._switch_layer_to(lay, _nxt)
                    if lay.fixed >= 0:
                        lay.fixed = _nxt
                lay.switch_pos = pos
                continue
            if lay.fixed >= 0:
                continue
            self._switch_layer_random(lay, snap)
            lay.switch_pos = pos

    @_locked
    def manual_transition(self):
        """手动切换（Ctrl+Return）：与「下一素材」等价 —— 切一次 + 倒计时从这一拍重新数。

        旧实现把 switch_pos 设成 -999 让下一次切换立刻到期，结果是**下一帧又白切一次**
        （连切两下，第二下把第一下顶掉），净效果其实就是「切一次 + 倒计时重置」。
        现在统一走 next_scene，行为一致且不再多切一刀。"""
        self.next_scene()

    @_locked
    def lock_clip(self):
        self.locked = not self.locked

    # ---------------- 逐拍交替（独立开关：仅前景层 · 仅 drop 段） ----------------
    def _cur_section_row(self):
        try:
            i = int(getattr(self, "_sec_idx", -1))
            secs = getattr(self, "_sec_list", None) or []
            if 0 <= i < len(secs):
                return secs[i]
        except Exception:                                          # noqa: BLE001
            pass
        return None

    def beat_drop_epoch(self):
        """当前是否处于 drop 段；是则返回段落标识（换段 ⇒ 换对子），否则 None。

        判据：曲库能量段落表里该段 `tier == "high"` 或名字含 drop。
        **没有段落表 / 没认出歌 ⇒ 一律 None（不触发）**——实时 drop 只是低频冲击，
        不适合当作持续的“段”。
        """
        row = self._cur_section_row()
        if not row:
            return None
        name = str(row.get("name") or "").lower()
        tier = str(row.get("tier") or "").lower()
        if tier != "high" and "drop" not in name:
            return None
        try:
            t0 = round(float(row.get("t0") or 0.0), 3)
        except (TypeError, ValueError):
            t0 = 0.0
        return (int(getattr(self, "_sec_idx", -1)), t0, name)

    def _beat_cfg_for(self, lay):
        try:
            params = self._params_for(lay) or {}
            return params.get("beat") or self.cfg["beat"]
        except Exception:                                          # noqa: BLE001
            return self.cfg["beat"]

    def beat_owns(self, lay, epoch=None):
        """该图层此刻是否由逐拍交替接管（开关开 + 前景 + drop 段 + 至少 2 个素材）。"""
        # ★ 2026-11-26（用户）：手动切换模式下**只**由手动按钮/快捷键切换，
        #   逐拍交替也不能接管（否则"手动"图层仍会自己跳）。
        try:
            if self.manual_switch_mode():
                return False
        except Exception:                                      # noqa: BLE001
            pass
        # 手动图层（auto_mode=False）同样不参与逐拍交替。
        if not getattr(lay, "auto_mode", False):
            return False
        if epoch is None:
            epoch = self.beat_drop_epoch()
        if epoch is None:
            return False
        if str(getattr(lay, "role", "bg") or "bg") != "fg":
            return False
        if getattr(lay, "is_kv", False) or getattr(lay, "is_cuesheet", False):
            return False
        try:
            if not bool(self._beat_cfg_for(lay).get("enabled", False)):
                return False
        except Exception:                                          # noqa: BLE001
            return False
        return len(getattr(lay, "clips", None) or []) >= 2

    def _beat_tick(self, snap):
        """逐拍交替：只在 drop 段、只接管前景层；对子跟着段落换。"""
        epoch = self.beat_drop_epoch()
        if epoch is None:
            return
        pos = self._beat_pos(snap)
        for lay in self.layers:
            if lay.fixed >= 0 or self.locked:
                continue
            if not self.beat_owns(lay, epoch):
                continue
            bc = self._beat_cfg_for(lay)
            interval = max(0.25, float(bc.get("interval_beats", 1) or 1))
            # ★ 2026-11-21（用户）：一对素材跟着段落走 —— 进新段落才换下一对，
            #   段内一直 A↔B 交替（不再按「每对小节数」换）。
            if lay.pair is None or getattr(lay, "beat_sec", None) != epoch:
                lay.beat_sec = epoch
                lay.pair = self._new_pair(lay, bc, prev_second=None)
                lay.pair_turn = 0
                lay.beat_last = pos
                self._beat_show(lay, 0, bc)
                continue
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
    @_locked
    def kv_layer(self):
        """返回 Kv 主视觉图层（全局唯一），无则 None。"""
        for lay in self.layers:
            if getattr(lay, "is_kv", False):
                return lay
        return None

    def _kv_tick(self, snap):
        """Kv 待机层节奏（**每屏一份**）：
        - 电平低于「静音判定阈值」且持续 > 静音触发延迟 → 切入（待机画面进来）
        - 电平高于阈值且持续 > 声音恢复切出延迟 → 切出（待机画面出去）
        不受视觉行为模式 / 能量映射 / 逐拍 / 曲风匹配 / BPM 变速等任何设置影响。

        ★ 2026-11-09 用户拍板「每一个屏幕都是独立的」：每块屏的 Kv 层各跑一套状态
          （状态本来就存在 layer 上：`kv_on/kv_silent_sec/...`），参数按该层所属屏取
          `effective_params(cfg, screen)["kv"]`。没有 override ⇒ 与全局完全相同 ⇒ 零回归。
        """
        any_kv = False
        for kv in self.layers:
            if not getattr(kv, "is_kv", False):
                continue
            any_kv = True
            self._kv_tick_one(kv, snap)
        if any_kv:
            return

    def _kv_tick_one(self, kv, snap):
        """单块屏的 Kv 待机逻辑（原来的 `_kv_tick` 主体，参数改为按屏取）。"""
        if kv is None or not kv.clips:
            return
        kvp = self._params_for(kv) or {}
        cfg = kvp.get("kv") or self.cfg["kv"]
        thr = float(cfg.get("db_threshold", -50.0))
        silent_delay = max(0.0, float(cfg.get("silent_delay", 2.0)))
        resume_delay = max(0.0, float(cfg.get("resume_delay", 2.5)))
        now = time.perf_counter()
        # 计时基准**按层**一份：各屏各自累积静音/有声时长，互不干扰
        dt = now - getattr(kv, "_kv_last_t", now)
        dt = max(0.0, min(0.5, dt))     # 保护：卡帧/暂停后不累加巨大 dt
        kv._kv_last_t = now

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

    def _kv_transition_pending(self):
        """是否有 Kv 图层正在 / 即将做淡入淡出。

        ★ 2026-11-17（用户：Kv 图层淡入时画面掉帧）：
          静音待机会把帧率降到 `perf.silent_standby_fps`（默认 2fps），而 Kv 的
          `in_dur` 默认 1.5s ⇒ 整个淡入只有 ~3 帧。这里在两种时刻返回 True：
            · 正在过渡（`kv_trans_active` 或 `kv_on` 在 0~1 之间）；
            · 触发条件已经满足、下一帧就会 `_kv_begin`（静音/有声计时到达阈值）。
          只覆盖淡入淡出那一两秒，长静音仍然照常进待机。
        """
        for l in (getattr(self, "layers", None) or []):
            if not getattr(l, "is_kv", False):
                continue
            try:
                on = float(getattr(l, "kv_on", 0.0) or 0.0)
            except (TypeError, ValueError):
                on = 0.0
            if bool(getattr(l, "kv_trans_active", False)) or 0.001 < on < 0.999:
                return True
            try:
                _cfg = (self._params_for(l) or {}).get("kv") or self.cfg["kv"]
            except Exception:                                  # noqa: BLE001
                _cfg = {}
            try:
                _sd = max(0.0, float(_cfg.get("silent_delay", 2.0)))
                _rd = max(0.0, float(_cfg.get("resume_delay", 2.5)))
            except (TypeError, ValueError):
                _sd, _rd = 2.0, 2.5
            try:
                if on < 1.0 and float(getattr(l, "kv_silent_sec", 0.0) or 0.0) >= _sd:
                    return True
                if on > 0.0 and float(getattr(l, "kv_loud_sec", 0.0) or 0.0) >= _rd:
                    return True
            except (TypeError, ValueError):
                pass
        return False

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
    # （`_apply_screen_cp()` 已于 2026-11-10 删除：每屏 color/postfx 合进
    #   `_compose_independent()`，否则单路会套两遍、颜色/特效叠加。）

    def _build_up_flash_alpha(self, snap):
        """Build Up 段内的闪白透明度（0=不闪）。按进入段后的拍数分频。"""
        try:
            # 2026-11-26：库内试听/标记 Build Up 后回到演出台，音频可能已经停了，
            # 但 recognized_offset 还停在 Build Up 区间，旧逻辑会继续闪白。
            # 没有声音/没有在演出时一律不闪，并清掉残留闪白状态。
            if not bool(getattr(self, "running", False)):
                self._bu_flash_until = 0.0
                self._bu_last_step = -1
                return 0
            try:
                _lvl = float(snap.get("level", 0.0) or 0.0)
                _sil = float(snap.get("silent_sec", 0.0) or 0.0)
            except (TypeError, ValueError):
                _lvl, _sil = 1.0, 0.0
            if _sil >= 1.5 or _lvl < 0.002:
                self._bu_flash_until = 0.0
                self._bu_last_step = -1
                return 0
            sid = snap.get("recognized_song_id", -1)
            g = self._song_grid_lookup.get(sid)
            if not isinstance(g, dict):
                self._bu_flash_until = 0.0
                self._bu_last_step = -1
                return 0
            bl = float(g.get("beat_len") or 0.0)
            off = float(snap.get("recognized_offset", -1.0) or -1.0)
            rt = float(snap.get("recognized_t", 0.0) or 0.0)
            rate = float(snap.get("recognized_rate", 1.0) or 1.0)
            if bl <= 0:
                self._bu_flash_until = 0.0
                self._bu_last_step = -1
                return 0
            now = time.perf_counter()
            if rt > 0 and off >= 0:
                pos = off + max(0.0, now - rt) * max(0.1, rate)
            else:
                # 导入包识别路径偶尔没有 recognized_t（=0）；段落判定
                # `_section_tick` 仍然有当前原曲秒，直接用它兜底。
                pos = float(getattr(self, "_sec_last_sec", -1.0) or -1.0)
                if pos < 0:
                    self._bu_flash_until = 0.0
                    self._bu_last_step = -1
                    return 0
            bu = None
            for _b in self._build_ups_of(g):
                _s = float(_b["start"])
                _e = _s + int(_b["beats"]) * bl
                if _s <= pos < _e:
                    bu = _b
                    break
            if bu is None:
                self._bu_flash_until = 0.0
                self._bu_last_step = -1
                self._bu_flash_key = None
                return 0
            start = float(bu["start"])
            beats = int(bu["beats"])
            _key = (round(start, 3), beats)
            if getattr(self, "_bu_flash_key", None) != _key:
                self._bu_flash_key = _key
                self._bu_last_step = -1
            # 前 8 拍不闪；之后 4 拍 → 2 拍 → 1 拍 → 1/2 拍 → 1/4 拍。
            elapsed_beats = (pos - start) / bl
            if elapsed_beats < 8.0:
                self._bu_flash_until = 0.0
                self._bu_last_step = -1
                return 0
            if elapsed_beats < 12.0:
                interval = 4.0
            elif elapsed_beats < 16.0:
                interval = 2.0
            elif elapsed_beats < 20.0:
                interval = 1.0
            elif elapsed_beats < 24.0:
                interval = 0.5
            else:
                # ★ 2026-11-25（用户：1/4 和 1/2 速度差不多、闪得不够快）：
                #   最后 8 拍加密到 **1/4 拍**，并把占空比固定到 ~50%；
                #   否则 1/2 的脉冲本来就占 35%、1/4 只占 50%，30fps 下看着几乎一样。
                interval = 0.25
            # ★ 用「第几个分频格」触发，而不是时间取模：
            #   这样每 2 拍一定触发一次，不会因为识别位置每几秒才刷新一次
            #   而跳过一半的脉冲（用户实测“该两拍一次却像四拍一次”）。
            step = int(elapsed_beats / interval + 1e-9)
            if step != getattr(self, "_bu_last_step", -1):
                self._bu_last_step = step
                period = interval * bl
                # 1/2 与 1/4 都保持 ~50% 占空比，才能明显看出 1/4 更快；
                # 更慢的档位仍封顶 0.12s（不要整拍常亮）。
                pulse = min(0.12, max(0.05, period * 0.5), period * 0.55)
                self._bu_flash_until = now + pulse
            if now < float(getattr(self, "_bu_flash_until", 0.0) or 0.0):
                return 225
        except Exception:                                      # noqa: BLE001
            pass
        return 0

    def _paint_build_up_flash(self, canvas, alpha):
        if not alpha or canvas is None:
            return
        # ★ 2026-11-25 真机（多屏 20 帧的隐藏大头）：Build Up 闪白原来用
        #   `QPainter.fillRect` 整幅叠白，1080p 单块画布实测 4~8ms；多屏还要
        #   对每块屏再刷一次，直接吃掉 8~15ms/帧。
        #   改为 cv2 4 通道原地混合（预乘口径正确）：1080p 实测 **1.6ms/块**，
        #   而且释放 GIL；失败才退回 QPainter。
        try:
            a = max(0.0, min(1.0, float(alpha) / 255.0))
            h, w = int(canvas.height()), int(canvas.width())
            bpl = int(canvas.bytesPerLine())
            if h > 0 and w > 0 and bpl == w * 4:
                arr = np.frombuffer(canvas.bits(), dtype=np.uint8,
                                    count=bpl * h).reshape(h, w, 4)
                white = getattr(self, "_flash_white", None)
                if white is None or white.shape != (h, w, 4):
                    white = np.full((h, w, 4), 255, dtype=np.uint8)
                    self._flash_white = white
                cv2.addWeighted(arr, 1.0 - a, white, a, 0.0, dst=arr)
                return
        except Exception:                                      # noqa: BLE001
            pass
        try:
            p = QPainter(canvas)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.fillRect(canvas.rect(), QColor(255, 255, 255, int(alpha)))
            p.end()
        except Exception:                                      # noqa: BLE001
            pass

    def _paint_screen_flashes(self, alpha):
        if not alpha:
            return
        for cv in (getattr(self, "_screen_canvases", None) or {}).values():
            if cv is not None:
                self._paint_build_up_flash(cv, alpha)

    def _apply_color_postfx(self, snap, canvas=None, advance=True, state_key=None,
                            lut_only=False, skip_post=False):
        """颜色 + 后处理：**共用一次**全画布 BGR 读回 / 写回。

        ★ 2026-10-28（性能）：原来 `_apply_color` 和 `_apply_postfx` 各自
          `_np_view_bgr()` 读回一次、`write_back_bgr()` 写回一次 ⇒ 每帧 2 读 2 写。
          1280×720 单次搬运合计 1.43 ms，白扔一半 ≈ **4.3% 单核**。

        正确性要点（改这里之前先读）：
          * 两段都改成「传入 bgr、返回 bgr、不写回」；`bgr=None` 表示"还没读回"，
            由真正要干活的那一段按需读回 —— 所以**没有效果时依然零成本**。
          * 顺序必须「颜色 → 后处理」，与原来一致（后处理要吃到调色后的画面）。
          * 任何一段抛异常都只跳过自己，且**已改动的 bgr 仍会写回**，
            不会出现"颜色生效了但没上屏"的诡异半帧。
        """
        bgr = None
        # ★★★ 2026-11-10（一级 bug，QA 实测到「独立屏内容跳成主屏画面」）：
        #   `_apply_color()` 原来固定从 `self.canvas` 读回像素 ⇒ 给独立屏调色时变成
        #   「读主屏像素、写独立屏画布」⇒ 那块独立屏被主屏画面整块覆盖。
        #   现在把目标画布传进去（`canvas=tgt`），它读/写的都是**同一块**画布；
        #   主画布（默认）行为与改动前逐字一致，且"没效果就不读回"的性能特性保留。
        tgt = canvas if canvas is not None else self.canvas
        # ★ 2026-11-10（口径闭合）：`seg post` 只覆盖**主画布**这一段
        #   （各独立屏的调色发生在 `copy` 段的 `_compose_independent` 里）。
        #   所以给分屏调用时，把六段的累加值**快照/还原**掉，不计入 ⇒
        #   `seg post ≈ view+tick+lut+apply+post+write` 才能 ±2ms 闭合。
        _seg_snap = None
        if canvas is not None:
            try:
                import colorfx as _cm0
                import postprocess as _pm0
                _seg_snap = (dict(_cm0.SEG), dict(_pm0.SEG),
                             float(_POST_ACC.get("post", 0.0)))
            except Exception:                                  # noqa: BLE001
                _seg_snap = None
        try:
            # 主画布推进共享颜色状态；独立屏只读，避免换色速度按屏数翻倍。
            _adv = bool(advance and (canvas is None or tgt is self.canvas))
            _t_c = time.perf_counter()
            bgr = self._apply_color(snap, bgr=bgr, write=False, canvas=tgt,
                                    advance=_adv, lut_only=lut_only)
            _POST_ACC["color"] = (time.perf_counter() - _t_c) * 1000.0
            if lut_only:
                return bgr
            if bgr is None:
                _POST_ACC["inplace"] = int(_POST_ACC.get("inplace", 0)) + 1
            else:
                _POST_ACC["fallback"] = int(_POST_ACC.get("fallback", 0)) + 1
        except Exception:
            pass
        try:
            if not skip_post:
                _t_p = time.perf_counter()
                bgr = self._apply_postfx(snap, bgr=bgr, write=False, canvas=tgt,
                                         state_key=state_key)
                _POST_ACC["post"] += (time.perf_counter() - _t_p) * 1000.0
        except Exception:
            pass
        try:
            _cfx = self.color_fx
            if _cfx is not None:
                _POST_ACC["s"] = float(getattr(_cfx, "s", 0.0) or 0.0)
                _POST_ACC["mode"] = str(((self._cp_cfg or self.cfg).get("color") or {})
                                        .get("mode", "lut"))
        except Exception:                                          # noqa: BLE001
            pass
        if bgr is None:
            return                      # 两段都没跑（或都没生效）⇒ 一次都不用搬
        try:
            from postprocess import write_back_bgr
            _t_w = time.perf_counter()
            write_back_bgr(tgt, bgr)
            _POST_ACC["write"] = (time.perf_counter() - _t_w) * 1000.0
        except Exception:
            pass                        # 写回失败不影响下一帧（下一帧会重新读回）
        if _seg_snap is not None:
            try:
                import colorfx as _cm1
                import postprocess as _pm1
                _cm1.SEG.clear(); _cm1.SEG.update(_seg_snap[0])
                _pm1.SEG.clear(); _pm1.SEG.update(_seg_snap[1])
                _POST_ACC["post"] = _seg_snap[2]
            except Exception:                                  # noqa: BLE001
                pass

    def _color_rotate_due(self, snap, cfg):
        """「这一帧该不该换颜色」——按**八拍**判定（用户 2026-10-28）。

        原来 `colorfx` 只看 `rotate_sec`（纯秒数），与音乐无关：16 秒在 128 BPM 是
        4 个八拍、在 175 BPM 是 6 个八拍，换色点会落在拍子中间。

        这里把 `rotate_sec` 换算成「几个八拍」：`round(rotate_sec / (8×beat_len))`，至少 1，
        再用八拍序号推进 —— 于是**每次换色都踩在八拍头上**，而"大约多少秒换一次"
        的用户预期仍然成立（BPM 变了间隔跟着变）。

        返回：True=本帧可换色 / False=还不到 / None=没网格（调用方退回旧的按秒行为）。
        """
        try:
            st = self._grid_state(snap)
        except Exception:                                     # noqa: BLE001
            st = None
        if not st:
            return None                      # 没进网格模式：保持旧行为（按秒），零回归
        try:
            bl = float(st[1])
            rot = max(1.0, float(cfg.get("rotate_sec", 45) or 45))
        except (TypeError, ValueError, IndexError):
            return None
        if not math.isfinite(bl) or bl <= 1e-6:
            return None
        try:
            pos = float(self._beat_pos(snap))
            off = float(self._bar_off(snap))
        except Exception:                                     # noqa: BLE001
            return None
        w = sections.PHRASE_BEATS            # 8
        step = bl * w                        # 一个八拍多少秒
        n = max(1, int(round(rot / max(1e-6, step))))
        idx = math.floor((pos - off) / w + 1e-9)
        last = getattr(self, "_color_phrase", None)
        last_pos = float(getattr(self, "_color_phrase_pos", 0.0) or 0.0)
        if last is None or pos < last_pos - 0.5:
            # 首次 / 拍钟重置（换歌）：对齐到当前八拍，但**本帧不换**
            # （否则每次换歌都会立刻闪一下颜色）
            self._color_phrase = idx
            self._color_phrase_pos = pos
            return False
        self._color_phrase_pos = pos
        if idx - int(last) >= n:
            self._color_phrase = idx
            return True
        return False

    def _make_lut_image(self, cfx, cfg, strength):
        """生成给 GPU 输出视图用的 256x1 RGBA LUT（BGR→RGBA 重排）。"""
        try:
            import colorfx
            import numpy as _np
            col = cfx.color_for_cfg(cfg)
            lut3, _ = colorfx.build_lut(col, float(strength), "lut")
            a = _np.asarray(lut3).reshape(256, 3)
            out = _np.empty((1, 256, 4), dtype=_np.uint8)
            out[0, :, 0] = a[:, 2]      # R
            out[0, :, 1] = a[:, 1]      # G
            out[0, :, 2] = a[:, 0]      # B
            out[0, :, 3] = 255
            return QImage(out.data, 256, 1, 256 * 4,
                          QImage.Format_RGBA8888).copy()
        except Exception:                                      # noqa: BLE001
            return None

    def _apply_color(self, snap, bgr=None, write=True, canvas=None, advance=True,
                     lut_only=False):
        """颜色渲染器（一键调色）：合成后、后处理前跑一次。

        **铁律**：Kv 待机层激活时绝不染色（`kv_on>0.01` 时强度直接硬归零，连渐变都不走），
        旁路开启时也立即归零（Bypass 要"响应迅速、无延迟"）。
        频闪素材自动休眠：避免「画面在闪 + 颜色在变」叠加成视觉灾难。

        ★★★ 2026-11-10（一级 bug）：`canvas` 参数**必须**传非主画布时，否则下面读回
          会固定读 `self.canvas` ⇒ 给独立屏调色时变成「读主屏像素、写独立屏画布」，
          那块独立屏会被主屏画面整块覆盖（QA 实测到的"内容在绿和红之间跳"）。
          传 None（默认）时行为与改动前逐字一致（读 `self.canvas`）。
        """
        # ★ 码表接管期间跳整条调色链（与 Kv 同口径：码表里是 DJ 做好的成片，不能再染色）
        #   ★ 按**画布所属的屏**判断（多屏下别的屏有码表不该拖累本屏）
        _tgt = canvas if canvas is not None else self.canvas
        if lut_only:
            self._gpu_lut_img = None
        _scr = self._canvas_screen(_tgt)
        if self._cuesheet_on(_scr):
            return bgr
        cfx = self.color_fx
        try:
            cfg = (self._cp_cfg or self.cfg)["color"]
        except Exception:
            return bgr
        if cfx is None or not cfg:
            return bgr
        kv = self._kv_for_screen(_scr)
        kv_on = float(getattr(kv, "kv_on", 0.0) or 0.0) if kv is not None else 0.0
        flick = self._flicker_hibernate(cfg, _scr)
        # ⚠ advance=False（独立屏）时绝不调用 `_color_rotate_due()`：
        #   它也会改共享的 `_color_phrase` 状态 ⇒ 颜色轮换会按屏数加速。
        try:
            _rot = (self._color_rotate_due(snap, cfg)
                    if advance and bool(cfg.get("auto", True)) else None)
        except Exception:                                     # noqa: BLE001
            _rot = None
        s = cfx.tick(cfg, snap, kv_on=kv_on, flicker=flick,
                     bypass=bool(cfg.get("bypass", False)), rotate_hint=_rot,
                     advance=advance)
        if s > 0.004:
            try:
                _col = None if advance else cfx.color_for_cfg(cfg)
                _mode = cfg.get("mode", "lut")
                if lut_only:
                    # ★ 2026-11-25：GPU 输出视图负责最终调色，这里只推进颜色状态
                    #   并生成 256x1 LUT 纹理，不在 CPU 画布上做 LUT。
                    self._gpu_lut_img = (self._make_lut_image(cfx, cfg, s)
                                         if str(_mode) == "lut" else None)
                    self._gpu_color_unsupported = (str(_mode) != "lut")
                    return bgr
                # ★ 2026-11-11（用户：多屏播放音乐时界面/预览卡）：**原地 LUT 快路径**。
                #   旧链路 = 读回转 BGR（2ms）+ LUT 新数组（3.7ms）+ 写回（7ms），
                #   3000×900 每帧约 13ms；原地 4 通道 LUT 实测 ~1.8ms。
                #   返回 None 表示"已经直接写进画布"，`_apply_color_postfx` 就不再写回。
                if bgr is None and _mode != "duotone":
                    try:
                        from postprocess import _np_view_bgra
                        _v4 = _np_view_bgra(_tgt)
                        if (_v4 is not None
                                and cfx.apply_bgra_inplace(_v4, _mode,
                                                           color=_col, strength=s)):
                            return None
                    except Exception:                          # noqa: BLE001
                        pass
                if bgr is None:
                    from postprocess import _np_view_bgr
                    bgr = _np_view_bgr(_tgt)
                # 从屏共享主屏当前色相，但按**本屏**的强度/手动色卡染色；
                # 传显式 color/strength 不会改 ColorRenderer 的共享状态。
                out = cfx.apply_to_bgr(bgr, _mode,
                                       color=_col, strength=s)
                if out is not None:
                    bgr = out
                    if write:
                        from postprocess import write_back_bgr
                        write_back_bgr(_tgt, bgr)
            except Exception:
                pass   # 调色失败不影响出画面
        return bgr

    def _kv_for_screen(self, screen=None):
        """该屏自己的 Kv 待机层；没有归属信息时退回全局 `kv_layer()`。

        `screen is None` = 旧单路/无输出配置，保持原来的“任意 Kv”语义。
        """
        try:
            if screen is None:
                return self.kv_layer()
            sid = str(screen.get("id") or "")
            for lay in self.layers:
                if not getattr(lay, "is_kv", False):
                    continue
                sc = self._layer_screen(lay)
                if sc is not None and str(sc.get("id") or "") == sid:
                    return lay
            # 该屏没有自己的 Kv：返回 None，绝不能拿别的屏的 Kv 把本屏颜色关掉
            return None
        except Exception:                                      # noqa: BLE001
            return None

    def _flicker_hibernate(self, cfg, screen=None):
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
        path = self._current_flicker_path(screen)
        if not path:
            return False
        if mode == "on":
            return True
        _sid = str((screen or {}).get("id") or "main")
        _key = "%s|%s" % (_sid, path)
        try:
            on = self._flicker_latch.get(_key)
        except Exception:                                      # noqa: BLE001
            self._flicker_latch = {}
            on = None
        if on is None:
            on = random.random() < 0.5
            if len(self._flicker_latch) > 256:
                self._flicker_latch.clear()
            self._flicker_latch[_key] = on
        return bool(on)

    def _current_flicker_path(self, screen=None):
        """当前在播的可见非 Kv 图层里，带「频闪」标签的素材路径（没有则空串）。

        `screen` 非 None 时只看该屏自己的图层（多屏下不能拿别屏的频闪素材
        把本屏颜色一起休眠）。
        """
        try:
            from tags_def import DYNAMIC_FLICKER
        except Exception:
            return ""
        want = None
        if screen is not None:
            try:
                ids = set(output_model.effective_layers(
                    self._out_cfg, screen,
                    [str(getattr(l, "lid", "")) for l in self.layers]))
                want = ids
            except Exception:                                  # noqa: BLE001
                want = None
        for lay in self.layers:
            if getattr(lay, "is_kv", False) or not getattr(lay, "visible", True):
                continue
            if want is not None and str(getattr(lay, "lid", "")) not in want:
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

    def _apply_postfx(self, snap, bgr=None, write=True, canvas=None, state_key=None):
        """对合成后的画布应用后处理特效链。

        触发模式（每个特效独立）：
        - constant 常驻：恒定按手动强度
        - event    事件：Drop 触发后从峰值按 decay 秒衰减回 0
        自动联动总开关 auto=False 时，全部退化为常驻。

        ★ 2026-11-10（多屏）：canvas/state_key 用于独立屏，后处理必须读写
          **该屏自己的画布**与拖影缓冲；`state_key` 由调用方按屏 id 传入。
        """
        tgt = canvas if canvas is not None else self.canvas
        scr = self._canvas_screen(tgt)
        pf_key = state_key or ("main" if tgt is self.canvas else str(id(tgt)))

        def _pf_info(d):
            # 全局 `_postfx_info` 只反映主屏，避免独立屏每帧把主屏 HUD/诊断覆盖
            if pf_key == "main":
                self._postfx_info = d

        # ★ 码表接管期间跳整条后处理链（同 Kv：成片不该被辉光/色差等污染）
        #   ★ 同样按画布所属的屏判断
        if self._cuesheet_on(scr):
            # 拖影缓冲必须一起清：不清的话码表切回来会闪一帧上一场的残影
            self._postfx_reset_trails(pf_key)
            _pf_info({"mode": "", "g": 0.0, "E": 0.0, "fx": {},
                      "state": "码表接管中，已跳过"})
            return bgr
        cfg = (self._cp_cfg or self.cfg)["postfx"]
        # Kv 待机层显示中：画面以 Kv 为主，跳过整条后处理链
        # （Kv 图层要求完全独立，不受「后处理」等任何设置影响）
        kv = self._kv_for_screen(scr)
        if kv is not None and kv.kv_on > 0.01:
            self._postfx_reset_trails(pf_key)
            _pf_info({"mode": "", "g": 0.0, "E": 0.0, "fx": {},
                      "state": "Kv 待机层接管，已跳过"})
            return bgr
        if not cfg or not cfg.get("enabled", True):
            self._postfx_reset_trails(pf_key)
            _pf_info({"mode": "", "g": 0.0, "E": 0.0, "fx": {},
                      "state": "总开关已关闭"})
            return bgr
        self._frame_i = getattr(self, "_frame_i", 0) + 1
        now = time.perf_counter()
        auto_link = bool(cfg.get("auto", True))
        mode = cfg.get("mode", "auto")
        g = max(0.0, min(1.0, float(cfg.get("global_level", 60)) / 100.0))
        # ★ 尺度兜底：老版本/手改过的配置里 global_level 可能是 0~1 小数（写成 0.6），
        #   直接 /100 会变成 0.006 ⇒ 自动模式"配置看着有值、画面完全没效果"。
        #   0.001~1 之间一律按比例换算（0 仍然是 0，代表用户真的关掉了）。
        _rg = float(cfg.get("global_level", 60) or 0.0)
        if 0.0 < _rg < 1.0:
            g = min(1.0, _rg)
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
            # 自动模式：按曲风/能量挑选 2~3 个效果。
            # 强度 = 全局强度（感知曲线 g^0.55，上限六成）× 能量唤醒（0.15 以下 0、0.70 以上满额）。
            # 例：全局 60% + 能量 0.5 → 0.45×0.64 ≈ 29%；全局 8% + 能量 0.5 → 0.14×0.64 ≈ 9%
            # （旧公式是 E×0.6×g，同样条件下只有 3% 和 2.4% —— 那就是"看不出效果"的原因）。
            # level 给 100：让最终强度就等于这条公式，不再被二次压小。
            # 关键：自动挑的效果必须绕过 effects[].on 过滤（默认全 False，否则永远没效果）。
            E_auto = max(0.0, min(1.0, float(snap_energy(snap, 0.0))))
            # 「效果保持」：0 = 实时跟随能量（旧行为）；>0 = 选中一组效果后保持 N 拍，
            # 到点（或曲风变了）才重新挑下一组。
            # 为什么要它：能量在 0.75 / 0.3 这些门槛附近抖动时，exposure / softfocus
            # 会反复进出效果链，画面上就是「效果忽有忽无」；按拍保持后变化才有节奏感。
            hold = max(0, int(cfg.get("auto_hold_beats", 0) or 0))
            if hold <= 0:
                chosen = self._auto_pick_effects(snap)
                self._fx_hold_pick = None
                self._fx_hold_due = 0.0
            else:
                pos = self._beat_pos(snap)
                # 拍位倒退（换歌 / 拍钟重置）= 之前的保持计时失效，必须重挑，
                # 否则 due 会一直大于当前拍位、效果再也不换。
                last = getattr(self, "_fx_hold_last", 0.0)
                back = pos < last - 0.5
                self._fx_hold_last = pos
                need = (self._fx_hold_pick is None or back
                        or pos >= getattr(self, "_fx_hold_due", 0.0))
                if need:
                    self._fx_hold_pick = self._auto_pick_effects(snap)
                    # ★ 2026-10-28：到期点**吸附到八拍头**（用户要求"画面效果里的选项
                    #   也要和波形图八拍对齐"）。原写法 `pos + hold` 会落在任意拍上 ——
                    #   换效果就切在半拍里，跟素材切换不在同一条网格上。
                    self._fx_hold_due = self._snap_to_phrase(pos + hold, snap,
                                                             forward=True)
                chosen = self._fx_hold_pick
            effects_override = {n: {"on": True, "level": 100} for n in chosen}
            # ★ 2026-10-23：不再把三个乘数连乘（见 AUTO_FX_MAX 注释），
            #   全局强度走感知曲线、能量只做唤醒 ⇒ 低滑块/中能量也有可见强度。
            _g_eff = AUTO_FX_MAX * (g ** AUTO_FX_GAMMA)
            _wake_auto = max(0.0, min(1.0, (E_auto - AUTO_FX_WAKE_LO)
                                    / max(0.01, AUTO_FX_WAKE_HI - AUTO_FX_WAKE_LO)))
            _m = _g_eff * _wake_auto
            mods = {n: _m for n in chosen}
            if self._frame_i % 6 == 0:       # 10Hz 足够（GUI 每 500ms 才读一次）
                _pf_info({"mode": "auto", "g": g, "E": E_auto,
                          "fx": {n: _m for n in chosen}, "state": ""})
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
                # ★ 2026-10-30 砍掉曲风后，原 `trig == "genre"`（"曲风命中才生效"）分支删除。
                #   老配置里残留的 "genre" 会落到下面的 `else: m = 1.0` ⇒ 退化为常驻
                #   （用户选过这个触发方式，但曲风已经没有了；常驻至少看得见效果）。
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
            if self._frame_i % 6 == 0:       # 同上：10Hz 回显就够
                _pf_info({"mode": "manual", "g": g, "E": float(E),
                          "fx": {n: float(v) * float(
                              (effects_cfg.get(n) or {}).get("level", 0)) / 100.0
                              for n, v in mods.items()},
                          "state": ""})
        # 拖影：**只在「刚刚停止」那一帧清一次**。
        # ⚠ 旧写法 `reset_trails=not self.running` 在**预览**（从未点开始）时每帧都清 ⇒
        #   拖影在预览里永远为空，用户会认定「拖影这个效果没有用」。
        _was_run = getattr(self, "_postfx_run_prev", None)
        _reset = (_was_run is True) and (not self.running)
        self._postfx_run_prev = bool(self.running)
        try:
            from postprocess import postfx_effects_bgr, write_back_bgr
            _out, _changed = postfx_effects_bgr(
                tgt, bgr, cfg, seed=self._frame_i, mods=mods,
                spectrum=snap.get("spectrum"),
                reset_trails=_reset,
                effects_override=effects_override,
                trail_key=pf_key)
            if _changed and _out is not None:
                bgr = _out
                if write:
                    write_back_bgr(tgt, bgr)
        except Exception:
            pass  # 后处理失败不影响渲染
        return bgr

    def _postfx_reset_trails(self, key=None):
        """清空拖影缓冲（步进被跳过时用，避免下次开启闪现旧残影）。

        ★ 只在缓冲**真的有内容**时才动：码表演出时这条每帧都会被调用，
          无条件清一次等于每帧多一次函数调用 + 属性写（虽然是微秒级，
          但热路径上的"每帧都做"要能省就省）。

        ★ 2026-11-10（多屏）：`key` 只清该屏的拖影；不传 = 清所有屏。
        """
        try:
            import postprocess as _pp
            keys = list(getattr(_pp, "_TRAILS", {}).keys()) if key is None else [key]
            for k in keys:
                if _pp._TRAILS.get(k) is not None:
                    _pp.fx_trails(None, 0.0, reset=True, key=k)
        except Exception:
            pass

    def _auto_pick_effects(self, snap):
        """自动模式：底效 bloom + chromatic；高潮加曝光脉冲，缓拍加柔焦，最多 3 个。

        ★ 2026-10-30 用户「大砍刀」：曲风识别整条链路删除，这里不再按曲风分组挑
          （原 `_AUTO_FX_BY_GENRE` 的 10 组映射一并删除）。底效固定，变化交给能量。
        """
        chosen = ["bloom", "chromatic"]
        E = snap.get("energy", 0.0)
        if E > 0.75 and "exposure" not in chosen:
            chosen.append("exposure")
        elif E < 0.3 and "softfocus" not in chosen:
            chosen.append("softfocus")
        return chosen[:3]


    def _auto_fps_tick(self):
        """Cap composition cadence to the fastest visible video source.

        A 30fps clip does not need a 60Hz QPainter/post-FX pass: every second
        frame is a duplicate image.  This is the single largest *render-side*
        saving on the default 60fps config and costs no visual quality because
        the source has no new frame to show.  GIFs/images still animate via
        effects/transitions at the capped rate.
        """
        # ★★★ 2026-11-12（用户：「软件开着什么都不放也 38% CPU」）：
        #   停止/冻结/黑场时**没有任何演出消费端**（输出窗口已隐藏、Spout/NDI
        #   也被 `running` 门控），却仍按素材 30fps 全速解码 + 30/60fps 合成 +
        #   每帧 GUI 缩放。这里统一降到 idle 帧率：
        #     · 停止：`perf.idle_fps`（默认 15，预览仍然实时，只是 15fps）
        #     · 冻结/黑场：10fps（画面本来就不变，`_set_players_active(False)`
        #       已把播放器挂起）
        #   开始 VJ 后下一 tick 会恢复源帧率，不影响演出。
        # ★★★ 2026-11-12：**静音待机**（运行中但长时间没有音频）。
        #   这不是"停止 VJ"（停止走下面的 idle_fps），而是现场放着歌单但还没放歌：
        #   画面仍要保留最后一帧，但不需要解码/合成/发送。
        #   先于 freeze/stop 判断；音频一回来把解码上限和帧率表重置，避免
        #   恢复后的头几秒还用 2fps 的 EMA 把播放压住。
        if bool(getattr(self, "_silent_standby", False)):
            self._silent_standby_prev = True
            try:
                _sfps = float((self.cfg["perf"] or {}).get(
                    "silent_standby_fps", 2.0) or 2.0)
            except (TypeError, ValueError):
                _sfps = 2.0
            _sfps = max(1.0, min(15.0, _sfps))
            # ★★ 2026-11-17（用户：切歌时视频输出卡顿掉帧十几秒）：
            #   待机的 2fps 会把 **Spout/NDI 输出**也压成 2fps —— 切歌/试听时
            #   中间有十几秒低电平，接收端（OBS/Resolume）看到的就是一卡一卡。
            #   解码器仍然挂起（省 CPU 的大头），但**有输出路由时**把合成/发送
            #   保持在 15fps：画面内容不变，只是重复发同一帧，接收端时间轴是平滑的。
            try:
                _has_out = False
                for _s in (self._out_cfg or {}).get("screens") or []:
                    if _s.get("include", True) is False:
                        continue
                    if _s.get("spout") or _s.get("ndi"):
                        _has_out = True
                        break
                if not _has_out:
                    _oc = self.cfg["output"] or {}
                    _has_out = bool(_oc.get("spout_enabled")
                                    or _oc.get("ndi_enabled"))
            except Exception:                                  # noqa: BLE001
                _has_out = False
            if _has_out:
                _sfps = max(_sfps, 15.0)
            _last = float(getattr(self, "_dec_cap_pub", -1.0) or -1.0)
            if abs(_sfps - _last) >= 0.5:
                self._dec_cap_pub = _sfps
                try:
                    import media_manager as _mm
                    _mm.set_decode_fps_cap(_sfps)
                except Exception:                          # noqa: BLE001
                    pass
            _tgt = int(round(_sfps))
            if self._effective_fps != _tgt:
                self._effective_fps = _tgt
                if self.timer.isActive():
                    self.timer.start(self.frame_interval_ms())
            return
        if bool(getattr(self, "_silent_standby_prev", False)):
            self._silent_standby_prev = False
            self._gap_ema_ms = 0.0
            self._tick_gap_ms = 0.0
            self._dec_cap_pub = 0.0
            # ★ 2026-11-17（Kv 淡入/淡出卡顿）：离开待机必须**立刻**恢复帧率。
            #   旧逻辑只清了 EMA/解码上限，`_effective_fps` 还是 2、`_fps_next_t`
            #   还可能压着最多 2 秒 ⇒ 音乐回来后的 Kv 淡出仍然一卡一卡。
            try:
                self._fps_next_t = 0.0
                self._effective_fps = None
                if self.timer.isActive():
                    self.timer.start(self.frame_interval_ms())
            except Exception:                                  # noqa: BLE001
                pass
            try:
                import media_manager as _mm
                _mm.set_decode_fps_cap(0.0)
            except Exception:                              # noqa: BLE001
                pass

        try:
            _frozen = bool(getattr(self, "freeze", False)
                           or getattr(self, "blackout", False))
            _running = bool(getattr(self, "running", False))
            if _frozen or not _running:
                if _frozen:
                    _idle_fps = 10.0
                else:
                    try:
                        _idle_fps = float((self.cfg["perf"] or {}).get(
                            "idle_fps", 15) or 15)
                    except (TypeError, ValueError):
                        _idle_fps = 15.0
                _idle_fps = max(5.0, min(30.0, _idle_fps))
                _last = float(getattr(self, "_dec_cap_pub", -1.0) or -1.0)
                if abs(_idle_fps - _last) >= 0.5:
                    self._dec_cap_pub = _idle_fps
                    try:
                        import media_manager as _mm
                        _mm.set_decode_fps_cap(_idle_fps)
                    except Exception:                          # noqa: BLE001
                        pass
                _tgt = int(round(_idle_fps))
                if self._effective_fps != _tgt:
                    self._effective_fps = _tgt
                    if self.timer.isActive():
                        self.timer.start(self.frame_interval_ms())
                return
        except Exception:                                      # noqa: BLE001
            pass

        # ★★★ 2026-11-11（现场：多屏预览卡，OBS 正常）：**解码节流**。
        #   引擎实际合成只有 ~10fps 时，解码器仍按素材 30fps 全速出帧 ⇒ 白烧 CPU、
        #   抢 GIL/内存带宽，反过来把引擎和 GUI 拖得更慢（正反馈）。
        #   这里按最近 tick 间隔的 EMA 给解码器设上限 = 实际 fps + 4（12~30），
        #   实际 ≥28fps 时不限制。上限只影响“解码出帧”，不改输出帧率/内容。
        try:
            _now = time.perf_counter()
            _g = float(getattr(self, "_tick_gap_ms", 0.0) or 0.0)
            if _g > 0.0:
                _ema = float(getattr(self, "_gap_ema_ms", 0.0) or 0.0)
                self._gap_ema_ms = _g if _ema <= 0.0 else (_ema * 0.70 + _g * 0.30)
            _ema = float(getattr(self, "_gap_ema_ms", 0.0) or 0.0)
            if bool(getattr(self, "running", False)) and _ema > 0.0:
                _actual = 1000.0 / max(1.0, _ema)
                if _actual >= 28.0:
                    _cap = 0.0                     # 已经跟得上，不限制
                else:
                    _cap = max(12.0, min(30.0, _actual + 4.0))
                _last = float(getattr(self, "_dec_cap_pub", -1.0) or -1.0)
                if _cap <= 0.0 or abs(_cap - _last) >= 2.0:
                    self._dec_cap_pub = _cap
                    try:
                        import media_manager as _mm
                        _mm.set_decode_fps_cap(_cap)
                    except Exception:                          # noqa: BLE001
                        pass
        except Exception:                                      # noqa: BLE001
            pass
        try:
            if not bool((self.cfg["perf"] or {}).get("fps_follow_source", True)):
                # 从 idle 恢复：`_effective_fps=None` 表示用配置里的 render_fps
                if self._effective_fps is not None:
                    self._effective_fps = None
                    if self.timer.isActive():
                        self.timer.start(self.frame_interval_ms())
                return
            now = time.perf_counter()
            if now < float(getattr(self, "_fps_next_t", 0.0) or 0.0):
                return
            self._fps_next_t = now + 2.0
            src_fps = 0.0
            for lay in self.layers:
                if not getattr(lay, "visible", True):
                    continue
                p = getattr(lay, "player", None)
                if p is None:
                    continue
                try:
                    f = float(getattr(p, "fps", 0.0) or 0.0)
                except Exception:
                    f = 0.0
                if 1.0 < f < 240.0:
                    src_fps = max(src_fps, f)
            if src_fps <= 0.0:
                src_fps = 30.0
            target = max(24, int(math.ceil(src_fps - 0.01)))
            try:
                configured = int((self.cfg["perf"] or {}).get("render_fps", 60) or 60)
            except Exception:
                configured = 60
            target = max(15, min(120, min(configured, target)))
            if target != self._effective_fps:
                self._effective_fps = target
                # QTimer must be (re)started from its owning engine thread;
                # _auto_fps_tick is called from _tick_locked, so this is safe.
                if self.timer.isActive():
                    self.timer.start(self.frame_interval_ms())
        except Exception:
            pass

    # ---------------- GPU 合成（可选，失败自动回退 CPU） ----------------
    def _init_gpu_backend(self):
        """Create the GPU composer on the engine thread (idempotent)."""
        if self._gpu_dead or self._gpu_comp is not None:
            return
        try:
            if not bool((self.cfg["perf"] or {}).get("gpu_compose", True)):
                self._gpu_dead = True
                self._gpu_last_reason = "配置已关闭"
                return
        except Exception:
            pass
        try:
            from gpu_compose import GpuCompositor
            self._gpu_comp = GpuCompositor(log=self._gpu_log)
            if not self._gpu_comp.ensure(self.canvas.width(), self.canvas.height()):
                self._gpu_last_reason = self._gpu_comp.last_error
                self._gpu_comp = None
                self._gpu_dead = True
        except Exception as e:                                    # noqa: BLE001
            self._gpu_last_reason = "导入/初始化失败：%s" % e
            self._gpu_comp = None
            self._gpu_dead = True

    def _gpu_log(self, msg):
        try:
            from config import app_base_dir
            p = os.path.join(app_base_dir(), "gpu_compose.log")
            with open(p, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    def _render_enabled(self):
        """默认启用一屏一进程 GPU 输出；AUTOVJ_GPU_RENDER=0 可强制回 CPU。"""
        _env = str(os.environ.get("AUTOVJ_GPU_RENDER", "")).strip()
        if _env == "0":
            return False
        if _env == "1":
            return True
        try:
            _perf = (getattr(self.cfg, "data", self.cfg).get("perf") or {})
            if "gpu_render_process" in _perf:
                return bool(_perf.get("gpu_render_process"))
        except Exception:                                      # noqa: BLE001
            pass
        return True

    def _render_note(self, msg):
        try:
            self._gpu_log("[RENDER] %s" % msg)
        except Exception:                                      # noqa: BLE001
            pass
        try:
            import stallwatch as _sw
            _sw.log_line("[RENDER] %s" % msg)
        except Exception:                                      # noqa: BLE001
            pass

    def _init_render_client(self):
        """启用独立 GPU 渲染模式。

        真正的一屏一进程在 `_render_proc_tick` 第一次拿到该屏任务时启动：
        每屏一个 GLFW 上下文/一个 FBO，读回互不排队，主屏 30fps 才稳得住。
        """
        if self._render_active or not self._render_enabled():
            return
        try:
            self._render_active = True
            self._render_fail = 0
            # 图层按性能设置解码（默认 1080p 输出 → 2/3 = 720p），
            # 渲染进程 FBO 仍按输出 1080p 合成，由 GPU 放大。
            self._render_scale = self._resolve_render_scale()
            self._plan_rev_ready = -1
            self._latest_frame = None
            self._gpu_stage_dead = True
            try:
                _main = output_model.main_screen(self._out_cfg) if self._out_cfg else None
                _disp = int((_main or {}).get("display", -2))
            except Exception:                                  # noqa: BLE001
                _disp = -2
            self._gpu_native = bool(_disp >= 0)
            self._render_note("GPU 一屏一进程模式已启用 native=%s disp=%s"
                              % (self._gpu_native, _disp))
            if self._gpu_native:
                try:
                    self.native_display_changed.emit(True)
                except Exception:                          # noqa: BLE001
                    pass
        except Exception as e:                                 # noqa: BLE001
            self._render_active = False
            self._render_note("初始化异常，保持 CPU：%s" % e)

    def _close_render_client(self, why=""):
        clis = list((getattr(self, "_render_clients", None) or {}).values())
        if getattr(self, "_render_client", None) is not None:
            clis.append(self._render_client)
        was = bool(getattr(self, "_render_active", False))
        self._render_clients = {}
        self._render_clients_cfg = {}
        self._render_client = None
        self._render_active = False
        self._gpu_native = False
        self._gpu_stage_dead = True
        self._plan_rev_ready = -1
        self._latest_frame = None
        self._render_route_frames = {}
        for cli in clis:
            if cli is None:
                continue
            try:
                cli.close()
            except Exception:                                  # noqa: BLE001
                pass
        if why:
            self._render_note("关闭渲染进程：%s" % why)
        if was:
            try:
                self.native_display_changed.emit(False)
            except Exception:                                  # noqa: BLE001
                pass

    def _render_proc_tick(self, snap):
        """把当前场景写进共享内存并投给一屏一进程渲染器。成功返回 True。"""
        if not getattr(self, "_render_active", False):
            return False
        for c in list((getattr(self, "_render_clients", None) or {}).values()):
            if c is not None and not c.alive():
                self._close_render_client("子进程已退出")
                return False
        plan = getattr(self, "_out_cfg", None)
        if not plan:
            return False
        if str(plan.get("mode") or "multi") != "multi":
            self._close_render_client("当前仅支持 multi 模式")
            return False
        try:
            _specs = self._route_specs()
        except Exception:                                      # noqa: BLE001
            _specs = []
        # 非 LUT 调色/后处理依赖 CPU 画布；先保持 CPU 路径，避免"颜色没了"。
        try:
            _col = (self.cfg["color"] or {})
        except Exception:                                      # noqa: BLE001
            _col = {}
        if bool(_col.get("on", False)) and str(_col.get("mode", "lut")) != "lut":
            self._close_render_client("非 LUT 调色暂未接入")
            return False
        t0 = time.perf_counter()
        all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        jobs = []
        try:
            main = output_model.main_screen(plan)
            if main is not None:
                _msid = str(main.get("id") or "")
                try:
                    _md = int(main.get("display", -2))
                except (TypeError, ValueError):
                    _md = -2
                jobs.append({"sid": _msid, "screen": main, "canvas": self.canvas,
                             "layers": self._main_screen_layers(),
                             "main": True, "display": bool(_md >= 0),
                             "display_monitor": _md if _md >= 0 else 0,
                             "alpha": bool(getattr(self, "_alpha_out", False))})
            _hidden = set(getattr(self, "_hidden_sids", None) or ())
            _shown = set(getattr(self, "_display_route_ids", None) or ())
            _sel = str(getattr(self, "_preview_route_id", "") or "")
            _thumbs = bool(getattr(self, "_route_thumb_consumers", False))
            if _sel:
                _shown.add(_sel)
            _net_all = bool(plan.get("ndi_enabled") or plan.get("spout_enabled"))
            for s in plan.get("screens") or []:
                if str(s.get("type") or "") != "independent":
                    continue
                sid = str(s.get("id") or "")
                if not sid or sid in _hidden:
                    continue
                if (not _thumbs and sid not in _shown and not _net_all
                        and not (s.get("ndi") or s.get("spout"))):
                    continue
                cv = self._screen_canvases.get(sid)
                if cv is None:
                    continue
                try:
                    ids = output_model.effective_layers(plan, s, all_ids)
                except Exception:                          # noqa: BLE001
                    self._close_render_client("独立屏图层解析失败")
                    return False
                try:
                    _sd = int(s.get("display", -2))
                except (TypeError, ValueError):
                    _sd = -2
                jobs.append({"sid": sid, "screen": s, "canvas": cv,
                             "layers": self._out_layers_by_ids(ids),
                             "main": False, "display": bool(_sd >= 0),
                             "display_monitor": _sd if _sd >= 0 else 0,
                             "alpha": bool((getattr(self, "_screen_alpha", None)
                                            or {}).get(sid, False))})
            if not jobs:
                return False
            # 路由名（Spout）按当前 plan 实时取
            sp_names = {}
            sp_sids = set()
            nd_sids = set()
            for sid, _kind, _nm, _sp, _nd in _specs:
                if _sp:
                    sp_sids.add(str(sid))
                    sp_names[str(sid)] = str(_nm)
                if _nd:
                    nd_sids.add(str(sid))
            try:
                from gpu_compose_layers import collect as _collect
            except Exception as e:                            # noqa: BLE001
                self._close_render_client("收集器导入失败：%s" % e)
                return False
            screens = []
            for job in jobs:
                s = job["screen"]
                try:
                    self._cp_cfg = (output_model.effective_params(self.cfg, s)
                                    if (s or {}).get("params") else None)
                except Exception:                             # noqa: BLE001
                    self._cp_cfg = None
                cv = job["canvas"]
                # 图层坐标/脉冲仍按 CPU 画布（默认 720p）收集，再整体放大到输出
                # 1080p。这样用户保存的 img_x/img_scale 不会因为内部倍率变化而缩水。
                W, H = int(cv.width()), int(cv.height())
                if W < 16 or H < 16:
                    self._cp_cfg = None
                    self._close_render_client("画布尺寸异常")
                    return False
                try:
                    OW, OH = output_model.effective_size(plan, s)
                except Exception:                             # noqa: BLE001
                    OW, OH = W, H
                OW, OH = int(OW), int(OH)

                try:
                    cue_view = self._cue_sheet_view(W, H)
                except Exception:                             # noqa: BLE001
                    cue_view = None
                desc = _collect(self, job["layers"], snap, W, H,
                                cue_view=cue_view)
                if desc is None:
                    self._cp_cfg = None
                    self._close_render_client("当前场景无法 GPU 收集")
                    return False
                # ★ 2026-11-26：Build Up 闪白交给 GPU 的**最终输出阶段**叠加，
                #   不再塞进 layers（主屏/独立屏的 scene 都统一生效）。
                _fa = int(getattr(self, "_bu_flash_alpha", 0) or 0)
                lut = None
                if bool(_col.get("on", False)) and str(_col.get("mode", "lut")) == "lut":
                    try:
                        self._apply_color(snap, lut_only=True, canvas=cv,
                                          advance=bool(job["main"]))
                        lut = getattr(self, "_gpu_lut_img", None)
                    except Exception:                         # noqa: BLE001
                        lut = None
                self._cp_cfg = None
                sid = job["sid"]
                screens.append({
                    "sid": sid, "w": W, "h": H,
                    "out_w": OW, "out_h": OH,
                    "alpha": bool(job["alpha"]),
                    "flash": int(_fa or 0),
                    # 主屏显示关闭时仍要合成并回传预览，只是不开原生窗。
                    "display": bool(job.get("display")),
                    "preview": bool(job["main"]),
                    "display_monitor": int(job.get("display_monitor") or 0),
                    "title": (("Easy Reality Output - "
                               + str((s or {}).get("name") or "输出"))
                              if job.get("display")
                              else (str((main or {}).get("name") or "Easy Reality Output")
                                    if job["main"] else "")),
                    "spout": str(sp_names.get(sid) or "") if sid in sp_sids else "",
                    "ndi": bool(sid in nd_sids),
                    "layers": desc, "lut": lut,
                    "blackout": bool(getattr(self, "blackout", False))})
            # 一屏一进程：每屏独立队列/GL 上下文/读回，主屏不被 dj 读回拖慢。
            try:
                from gpu_render_client import GpuRenderClient
            except Exception as e:                             # noqa: BLE001
                self._close_render_client("客户端导入失败：%s" % e)
                return False
            used = set()
            main_cli = None
            for sc in screens:
                sid = str(sc.get("sid") or "")
                used.add(sid)
                cli = (getattr(self, "_render_clients", None) or {}).get(sid)
                # 显示拓扑 + 输出分辨率都参与重启判断：GLFW 原生窗/输出 FBO
                # 是在子进程首次 ensure 时按当时尺寸建的，改分辨率后必须重建。
                _want = (bool(sc.get("display")),
                         int(sc.get("display_monitor") or 0),
                         int(sc.get("out_w") or 0),
                         int(sc.get("out_h") or 0))
                if cli is not None and self._render_clients_cfg.get(sid) != _want:
                    # 显示拓扑变了（关→开 / 换显示器 / 开→关）：
                    # 子进程的 GLFW 窗口在第一次 ensure 后不会重建，必须重启。
                    try:
                        cli.close()
                    except Exception:                      # noqa: BLE001
                        pass
                    self._render_clients.pop(sid, None)
                    self._render_clients_cfg.pop(sid, None)
                    cli = None
                if cli is None or not cli.alive():
                    cli = GpuRenderClient(log=self._render_note,
                                          name="autovj_render_%s" % sid[:10])
                    if not cli.start():
                        self._close_render_client("子进程启动失败 %s" % sid)
                        return False
                    self._render_clients[sid] = cli
                    self._render_clients_cfg[sid] = _want
                    self._render_note("启动渲染子进程 sid=%s pid=%s display=%s"
                                      % (sid, getattr(cli._proc, "pid", "?"), _want))
                if not cli.submit([sc]):
                    self._close_render_client("提交失败 %s" % sid)
                    return False
                if sc.get("display") or sc.get("preview"):
                    main_cli = cli
            # 清掉已经不存在的屏
            for sid in list((getattr(self, "_render_clients", None) or {}).keys()):
                if sid not in used:
                    try:
                        self._render_clients.pop(sid).close()
                    except Exception:                          # noqa: BLE001
                        pass
                    self._render_clients_cfg.pop(sid, None)
            if main_cli is not None:
                self._render_client = main_cli
            elif self._render_clients:
                self._render_client = next(iter(self._render_clients.values()))
            self._gpu_stage_frames = int(getattr(self, "_gpu_stage_frames", 0)) + 1
            self._gpu_stage_ms = (time.perf_counter() - t0) * 1000.0
            _now = time.perf_counter()
            if _now - float(getattr(self, "_render_log_t", 0.0) or 0.0) >= 5.0:
                self._render_log_t = _now
                self._render_note("[RENDER] 提交 %d 屏/进程 引擎侧 %.1fms 帧=%d"
                                  % (len(screens), self._gpu_stage_ms,
                                     self._gpu_stage_frames))
            return True
        except Exception as e:                                 # noqa: BLE001
            self._close_render_client("tick 异常：%s" % e)
            return False
        finally:
            self._cp_cfg = None

    def _publish_render_preview(self):
        cli = getattr(self, "_render_client", None)
        try:
            _th = {}
            for _c in list((getattr(self, "_render_clients", None) or {}).values()):
                try:
                    _th.update(_c.poll_thumbs() or {})
                except Exception:                      # noqa: BLE001
                    pass
            if _th:
                self._route_thumbs = _th
                self._route_thumb_rev = int(getattr(self, "_route_thumb_rev", 0)) + 1
        except Exception:                                  # noqa: BLE001
            pass
        if cli is None:
            return False
        try:
            img = cli.poll_preview()
            if img is None or img.isNull():
                return False
            self._latest_frame = img
            self._frame_seq += 1
            self.frame_ready.emit(self._frame_seq)
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def _init_gpu_stage(self):
        """在引擎线程里创建完整 GPU 场景合成器（幂等）。

        ★ 2026-11-26：不再依赖 `perf.gpu_compose`（旧 QPainter-on-GL 实验开关），
          改由 `perf.gpu_decode` 控制 —— 与「GPU 解码」同一个用户开关。
        """
        if self._gpu_stage is not None or self._gpu_stage_dead:
            return
        if not getattr(self, "_out_cfg", None):
            # 输出计划还没下发（start() 早于 set_output_config）⇒ 等第一帧拿到
            # plan 后再建上下文，否则会把原生显示器判断成 -2，永远开 Qt 窗口。
            return
        if os.environ.get("AUTOVJ_GPU_COMPOSE") == "0":
            self._gpu_stage_dead = True
            self._gpu_log("GPU 场景合成被 AUTOVJ_GPU_COMPOSE=0 关闭")
            return
        try:
            if os.environ.get("AUTOVJ_GPU_SCENE") == "0":
                self._gpu_stage_dead = True
                self._gpu_log("GPU 场景合成被 AUTOVJ_GPU_SCENE=0 关闭")
                return
            _cfg = getattr(self.cfg, "data", self.cfg)
            _perf = _cfg.get("perf") or {}
            if not bool(_perf.get("gpu_decode", True)):
                self._gpu_stage_dead = True
                self._gpu_log("GPU 场景合成不可用：GPU 解码已关闭")
                return
            # ★ 2026-11-26：moderngl 后端单测 12.6ms/2 屏，但接上原生显示 +
            #   Spout 后仍被 Python GIL/输出上下文拖到 10fps（见 gpu_compose.log）。
            #   先**默认关闭**，只允许显式 `perf.gpu_compose_mgl=True` 或
            #   `AUTOVJ_GPU_COMPOSE=1` 做实验；正式路径仍是 CPU 合成。
            _mgl_on = bool(_perf.get("gpu_compose_mgl", False)
                           or os.environ.get("AUTOVJ_GPU_COMPOSE") == "1")
            if not _mgl_on:
                self._gpu_stage_dead = True
                self._gpu_log("GPU 场景合成默认关闭（实验开关未开）")
                return
            from gpu_stage_mgl import SceneStage
            stage = SceneStage(log=self._gpu_log)
            # 主屏 display>=0 ⇒ GPU 合成器自己开原生 GLFW 输出窗；Qt 输出窗
            # 会隐藏。这样显示 + Spout 共用同一个 GL 上下文，不再互抢。
            _plan = getattr(self, "_out_cfg", None)
            _main = output_model.main_screen(_plan) if _plan else None
            try:
                _disp = int((_main or {}).get("display", -2))
            except (TypeError, ValueError):
                _disp = -2
            _native = bool(_disp >= 0 and
                           str(os.environ.get("AUTOVJ_GPU_NATIVE", "1")) != "0")
            _title = str((_main or {}).get("name") or "Easy Reality Output")
            self._gpu_log("GPU 场景合成初始化：native=%s disp=%s plan=%s title=%s"
                          % (_native, _disp, bool(_plan), _title))
            if not stage.ensure(self.canvas.width(), self.canvas.height(),
                                native=_native, monitor=max(0, _disp),
                                always_top=True, title=_title):
                self._gpu_stage_dead = True
                self._gpu_log("GPU 场景合成初始化失败：%s" % stage.last_error)
                return
            self._gpu_stage = stage
            self._gpu_native = bool(getattr(stage, "_native", False))
            if self._gpu_native:
                try:
                    self.native_display_changed.emit(True)
                except Exception:                          # noqa: BLE001
                    pass
            # GPU 合成走 1080p 原生，不再需要 CPU 性能模式的 720p 内部缩放。
            # 下一 tick 的 `_rebuild_output_plan()` 会按 1.0 重建画布。
            if float(getattr(self, "_render_scale", 1.0) or 1.0) < 0.999:
                self._render_scale = 1.0
                self._plan_rev_ready = -1
                self._latest_frame = None
            self._gpu_log("GPU 场景合成已接管：1080p 原生合成，NDI/Spout 共用成品帧")
        except Exception as e:                                # noqa: BLE001
            self._gpu_stage_dead = True
            self._gpu_log("GPU 场景合成初始化异常：%s" % e)

    def render_process_active(self):
        return bool(getattr(self, "_render_active", False))

    def gpu_native_display(self):
        """GPU 合成器/独立渲染进程是否已接管原生输出窗（Qt 输出窗必须隐藏）。"""
        if getattr(self, "_render_active", False):
            return bool(getattr(self, "_gpu_native", False))
        return bool(getattr(self, "_gpu_native", False)
                    and self._gpu_stage is not None)

    def _gpu_stage_note(self, msg):
        """GPU 合成诊断：同时写 gpu_compose.log 和 ui_stall.log（限流由调用方控制）。"""
        try:
            self._gpu_log(msg)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            import stallwatch as _sw
            _sw.log_line(msg)
        except Exception:                                     # noqa: BLE001
            pass

    def _gpu_compose_now(self):
        """当前是否尝试 GPU 场景合成。

        ★ 2026-11-26 真机结论：PySide 的 GL 调用（纹理上传 / toImage 回读）
          会长时间持有 GIL，和 GUI 线程互相饿死（实测主线程停顿 700ms+、
          输出掉到 11fps）。因此**默认关闭**，只在显式 `AUTOVJ_GPU_COMPOSE=1`
          时用于实验/对比；正式路径是「CPU 合成 + GPU 显示」。
        """
        if self._gpu_stage_dead:
            return False
        if os.environ.get("AUTOVJ_GPU_COMPOSE") == "0":
            return False
        if self._gpu_stage is None:
            self._init_gpu_stage()
        return self._gpu_stage is not None

    def _gpu_blit(self, dst, src):
        """把 GPU 合成结果贴进现有画布（保持画布对象不变，下游全部复用）。"""
        try:
            if (dst.size() == src.size()
                    and dst.format() == src.format()
                    and int(dst.bytesPerLine()) == int(src.bytesPerLine())):
                np.copyto(np.frombuffer(dst.bits(), np.uint8,
                                        count=dst.sizeInBytes()),
                          np.frombuffer(src.constBits(), np.uint8,
                                        count=src.sizeInBytes()))
                return True
        except Exception:                                     # noqa: BLE001
            pass
        try:
            p = QPainter(dst)
            try:
                p.setCompositionMode(QPainter.CompositionMode_Source)
                p.drawImage(0, 0, src)
            finally:
                p.end()
            return True
        except Exception:                                     # noqa: BLE001
            return False

    def _gpu_compose_tick(self, snap):
        """完整 GPU 场景合成：主屏 + 独立屏。

        成功返回 True（画布已更新，后续发布/NDI/Spout 全部复用）；
        任何一屏无法精确复刻时返回 False，调用方整帧回退 CPU。
        """
        if not self._gpu_compose_now():
            return False
        stage = self._gpu_stage
        if stage is None or not stage.available:
            return False
        t0 = time.perf_counter()
        plan = getattr(self, "_out_cfg", None)
        all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        jobs = []
        try:
            if not plan:
                jobs.append({"sid": "", "screen": None, "canvas": self.canvas,
                             "layers": self._main_screen_layers(),
                             "main": True,
                             "display": self.gpu_native_display(),
                             "spout": False,
                             "alpha": bool(getattr(self, "_alpha_out", False))})
            else:
                main = output_model.main_screen(plan)
                if main is not None:
                    _msid = str(main.get("id") or "")
                    jobs.append({"sid": _msid,
                                 "screen": main, "canvas": self.canvas,
                                 "layers": self._main_screen_layers(),
                                 "main": True,
                                 "display": self.gpu_native_display(),
                                 "spout": _msid in (getattr(self, "_stage_spout_sids", None) or set()),
                                 "alpha": bool(getattr(self, "_alpha_out", False))})
                _hidden = set(getattr(self, "_hidden_sids", None) or ())
                _shown = set(getattr(self, "_display_route_ids", None) or ())
                _sel = str(getattr(self, "_preview_route_id", "") or "")
                _thumbs = bool(getattr(self, "_route_thumb_consumers", False))
                if _sel:
                    _shown.add(_sel)
                _net_all = bool(plan.get("ndi_enabled") or plan.get("spout_enabled"))
                for s in plan.get("screens") or []:
                    if str(s.get("type") or "") != "independent":
                        continue
                    sid = str(s.get("id") or "")
                    if not sid or sid in _hidden:
                        continue
                    # 没人看的独立屏不合成：没有输出窗、不是当前预览路由、
                    # 输出页也没开、且没有 NDI/Spout 接收端。
                    if (not _thumbs and sid not in _shown and not _net_all
                            and not (s.get("ndi") or s.get("spout"))):
                        continue
                    cv = self._screen_canvases.get(sid)
                    if cv is None:
                        continue
                    try:
                        ids = output_model.effective_layers(plan, s, all_ids)
                    except Exception:                         # noqa: BLE001
                        return False
                    jobs.append({
                        "sid": sid, "screen": s, "canvas": cv,
                        "layers": self._out_layers_by_ids(ids),
                        "main": False,
                        "display": False,
                        "spout": sid in (getattr(self, "_stage_spout_sids", None) or set()),
                        "alpha": bool((getattr(self, "_screen_alpha", None)
                                       or {}).get(sid, False))})
            if not jobs:
                return False
            _t_collect = 0.0
            _t_lut = 0.0
            _t_gpu = 0.0
            _t_blit = 0.0
            # 1) 先把每屏描述收集完（任何一屏不支持 ⇒ 整帧回退 CPU，画布不动）
            try:
                from gpu_compose_layers import collect as _collect
            except Exception as e:                            # noqa: BLE001
                self._gpu_stage_dead = True
                self._gpu_stage_note("[GPU] 无法导入收集器，回退 CPU：%s" % e)
                return False
            for job in jobs:
                s = job["screen"]
                _tc0 = time.perf_counter()
                try:
                    self._cp_cfg = (output_model.effective_params(self.cfg, s)
                                    if (s or {}).get("params") else None)
                except Exception:                             # noqa: BLE001
                    self._cp_cfg = None
                cv = job["canvas"]
                W, H = int(cv.width()), int(cv.height())
                if W < 16 or H < 16:
                    self._cp_cfg = None
                    return False
                try:
                    cue_view = self._cue_sheet_view(W, H)
                except Exception:                             # noqa: BLE001
                    cue_view = None
                desc = _collect(self, job["layers"], snap, W, H, cue_view=cue_view)
                if desc is None:
                    self._cp_cfg = None
                    return False
                job["desc"] = desc
                job["gpu_lut"] = False
                lut = None
                _t_collect += (time.perf_counter() - _tc0) * 1000.0
                try:
                    _col = (self._cp_cfg or self.cfg)["color"]
                except Exception:                             # noqa: BLE001
                    _col = {}
                if bool(_col.get("on", False)) and str(
                        _col.get("mode", "lut")) == "lut":
                    _tl0 = time.perf_counter()
                    try:
                        self._apply_color(snap, lut_only=True, canvas=cv,
                                          advance=bool(job["main"]))
                        lut = getattr(self, "_gpu_lut_img", None)
                        job["gpu_lut"] = lut is not None
                    except Exception:                         # noqa: BLE001
                        lut = None
                        job["gpu_lut"] = False
                    _t_lut += (time.perf_counter() - _tl0) * 1000.0
                job["lut"] = lut
                self._cp_cfg = None
            # 2) GPU 逐屏合成（全部成功才开始贴回画布）
            for job in jobs:
                scene = {"w": int(job["canvas"].width()),
                         "h": int(job["canvas"].height()),
                         "sid": job.get("sid") or "",
                         "display": bool(job.get("display")),
                         "spout": bool(job.get("spout")),
                         "layers": job["desc"], "lut": job["lut"],
                         "alpha": bool(job["alpha"]),
                         "blackout": bool(getattr(self, "blackout", False))}
                _tg0 = time.perf_counter()
                img = stage.compose(scene)
                _t_gpu += (time.perf_counter() - _tg0) * 1000.0
                if img is None or img.isNull():
                    self._gpu_stage_fail = int(getattr(self, "_gpu_stage_fail", 0)) + 1
                    if self._gpu_stage_fail >= 3 or not stage.available:
                        self._gpu_stage_dead = True
                        self._gpu_stage_note(
                            "[GPU] 连续失败，本次运行回退 CPU：%s" % stage.last_error)
                    return False
                job["img"] = img
            self._gpu_stage_fail = 0
            # 3) 贴回画布 + 后处理（LUT 已在 GPU 上；非 lut 调色/后处理仍走 CPU）
            for job in jobs:
                _tb0 = time.perf_counter()
                cv = job["canvas"]
                if not self._gpu_blit(cv, job["img"]):
                    return False
                s = job["screen"]
                try:
                    self._cp_cfg = (output_model.effective_params(self.cfg, s)
                                    if (s or {}).get("params") else None)
                except Exception:                             # noqa: BLE001
                    self._cp_cfg = None
                try:
                    if job.get("gpu_lut"):
                        try:
                            self._apply_postfx(
                                snap, canvas=cv,
                                state_key=None if job["main"] else job["sid"])
                        except Exception:                     # noqa: BLE001
                            pass
                    else:
                        try:
                            self._apply_color_postfx(
                                snap, canvas=cv, advance=bool(job["main"]),
                                state_key=None if job["main"] else job["sid"])
                        except Exception:                     # noqa: BLE001
                            pass
                finally:
                    self._cp_cfg = None
                _t_blit += (time.perf_counter() - _tb0) * 1000.0
            self._display_scenes = {}
            self._gpu_luts = {}
            self._gpu_stage_frames = int(getattr(self, "_gpu_stage_frames", 0)) + 1
            self._gpu_stage_ms = (time.perf_counter() - t0) * 1000.0
            _now = time.perf_counter()
            if _now - float(getattr(self, "_gpu_stage_log_t", 0.0) or 0.0) >= 5.0:
                self._gpu_stage_log_t = _now
                try:
                    _st = stage.stats()
                    self._gpu_stage_note(
                        "[GPU] %d 帧 合成=%.1fms 回读=%.1fms 上传=%d 缓存=%d"
                        " 最大=%sx%s 上传MB=%.0f"
                        " | 屏=%d 收集=%.1f lut=%.1f gpu=%.1f 贴回=%.1f"
                        " | 明细 make=%.1f draw=%.1f up=%.1f final=%.1f read=%.1f"
                        " present=%.1f spout=%.1f"
                        % (self._gpu_stage_frames, self._gpu_stage_ms,
                           float(_st.get("readback_ms") or 0.0),
                           int(_st.get("uploads") or 0),
                           len(getattr(stage, "_tex_slots", {}) or {}),
                           (_st.get("max_tex") or (0, 0))[0],
                           (_st.get("max_tex") or (0, 0))[1],
                           float(_st.get("up_bytes") or 0) / (1024.0 * 1024.0),
                           len(jobs), _t_collect, _t_lut, _t_gpu, _t_blit,
                           float((_st.get("parts") or {}).get("make") or 0.0),
                           float((_st.get("parts") or {}).get("draw") or 0.0),
                           float((_st.get("parts") or {}).get("upload") or 0.0),
                           float((_st.get("parts") or {}).get("final") or 0.0),
                           float((_st.get("parts") or {}).get("read") or 0.0),
                           float((_st.get("parts") or {}).get("present") or 0.0),
                           float((_st.get("parts") or {}).get("spout") or 0.0)))
                except Exception:                             # noqa: BLE001
                    pass
            return True
        finally:
            self._cp_cfg = None

    def _gpu_safe_for_scene(self):
        """True when every visible layer can be represented by Qt's GL paint engine.

        Qt's QOpenGLPaintDevice supports SourceOver/Plus.  Other blend modes
        (screen/multiply/lighten/darken) are silently ignored by Qt's GL paint
        engine, and subtract is a project-specific pixel pass -> keep CPU.

        Also keep video layers on the CPU path by default: QPainter's GL paint
        engine re-uploads each new video QImage, and at 720p that costs more
        than CPU QPainter composition (measured 12.4ms vs 7.0ms for 3 layers).
        Static images/GIFs cache their texture and are a clear win.
        """
        safe = {"normal", "add"}
        allow_video = bool((self.cfg["perf"] or {}).get("gpu_compose_video", False))
        for lay in self.layers:
            if not getattr(lay, "visible", True):
                continue
            # Cue containers mix video/image sublayers; keep the CPU compositor
            # for them (same reason as video layers: per-frame texture upload).
            if getattr(lay, "is_cuesheet", False):
                return False
            p = getattr(lay, "player", None)
            if (not allow_video and p is not None
                    and type(p).__name__ in ("VideoPlayer", "AvAlphaPlayer", "GpuDxvPlayer")):
                return False
            if getattr(lay, "is_cuesheet", False):
                for ps in (getattr(lay, "cs_states", None) or []):
                    if not getattr(ps, "active", False):
                        continue
                    if str(getattr(ps, "blend", "normal") or "normal") not in safe:
                        return False
            else:
                if str(getattr(lay, "blend", "normal") or "normal") not in safe:
                    return False
        return True

    def _gpu_prepare_image(self, img):
        """GL paint engine expects premultiplied alpha for ARGB sources.

        Opaque images (RGB32 / no alpha) need no conversion.  Static images are
        cached by cacheKey so the conversion is paid once; video alpha frames
        are converted per frame (still cheaper than CPU-compositing several
        layers, and correctness comes first).
        """
        if img is None or img.isNull():
            return img
        try:
            if not img.hasAlphaChannel():
                return img
            if img.format() == QImage.Format_ARGB32_Premultiplied:
                return img
            key = int(img.cacheKey())
            got = self._gpu_premul.get(key)
            if got is not None:
                return got
            out = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            self._gpu_premul[key] = out
            self._gpu_premul_order.append(key)
            while len(self._gpu_premul_order) > self._gpu_premul_max:
                old = self._gpu_premul_order.pop(0)
                self._gpu_premul.pop(old, None)
            return out
        except Exception:
            return img

    def _gpu_begin(self, W, H):
        if self._gpu_dead or self._gpu_comp is None:
            return None
        try:
            if not self._gpu_comp.ensure(W, H):
                self._gpu_last_reason = self._gpu_comp.last_error
                self._gpu_dead = True
                return None
            p = self._gpu_comp.begin(W, H)
            if p is None:
                self._gpu_last_reason = self._gpu_comp.last_error
                self._gpu_dead = True
                return None
            self._gpu_mode = "gpu"
            return p
        except Exception as e:                                    # noqa: BLE001
            self._gpu_last_reason = str(e)
            self._gpu_dead = True
            self._gpu_mode = "cpu"
            return None

    def _gpu_finish(self, p):
        try:
            img = self._gpu_comp.end(p) if self._gpu_comp is not None else None
        except Exception:
            img = None
        if img is None or img.isNull():
            self._gpu_dead = True
            self._gpu_mode = "cpu"
            return None
        return img

    @_locked
    def set_frame_consumers(self, on):
        """Tell the engine whether the GUI/output window needs a CPU frame copy.

        When preview is off and the independent output window is hidden, the
        full-frame copy + frame_ready signal are pure waste.  Spout/NDI read
        the canvas directly and are unaffected.
        """
        self._gpu_frame_consumers = bool(on)

    # ================= 多屏输出（2026-11-08） =================
    @_locked
    def set_output_config(self, cfg_snapshot):
        """UI 下发当前活动的输出配置（深拷贝）。

        ⚠ 不在这里创建/关闭发送端：配置切换可能发生在 GUI 线程，NDI/Spout 的
          close 最坏要等 1 秒；统一放到引擎线程的 `_rebuild_output_plan()` 里做，
          GUI 只负责保存配置和下发快照。
        """
        try:
            _was_plan = self._out_cfg is not None
            self._out_cfg = copy.deepcopy(cfg_snapshot) if cfg_snapshot else None
        except Exception:
            _was_plan = self._out_cfg is not None
            self._out_cfg = cfg_snapshot
        # ★ 2026-11-26：**绝不在这里 close 发送端**。加/删图层也会调本方法；
        #   NDI/Spout 的 close 最坏要等 1 秒，放在 GUI 线程就是"软件卡十几秒"。
        #   legacy 发送端的清理统一放到引擎线程的 `_rebuild_output_plan()` 开头。
        self._plan_rev = int(getattr(self, "_plan_rev", 0)) + 1
        self._plan_rev_ready = -1
        if self._out_cfg:
            self._out_mode = str(self._out_cfg.get("mode") or "multi")

    def _close_legacy_output(self):
        """关闭 legacy 单路发送端。**绝不关任何被路由表引用的对象**（双保险）。

        ★ BUG-A：`self._spout` 曾被指向路由表里的主屏 sender —— 这里一关，
          主屏那条路由就永久哑火（`_dead=True` 但 send() 仍返回 True）。
        """
        live = []
        for st_ in (getattr(self, "_route_senders", None) or {}).values():
            for key in ("spout", "ndi"):
                obj = (st_ or {}).get(key)
                if obj is not None:
                    live.append(obj)
        for attr in ("_spout", "_ndi"):
            obj = getattr(self, attr, None)
            setattr(self, attr, None)
            if obj is None:
                continue
            if any(obj is x for x in live):
                _output_health_write(
                    "跳过关闭 legacy %s：该对象仍被路由表引用（BUG-A 防线）" % attr)
                continue
            try:
                obj.close()
            except Exception:                                     # noqa: BLE001
                pass

    def latest_route_thumbs(self):
        """无锁只读：{screen_id: QImage 缩略图}，给上方输出预览工作区。"""
        return self._route_thumbs

    def latest_route_thumbs_rev(self):
        """缩略图批次号（每次 `_route_thumbs` 被替换 +1），供 GUI 判新帧。"""
        return int(getattr(self, "_route_thumb_rev", 0))

    def set_preview_res(self, res):
        """预览分辨率（1.0 / 0.5 / 0.25）。只写一个 float，GIL 下原子。"""
        try:
            self._preview_res = max(0.1, min(1.0, float(res)))
        except (TypeError, ValueError):
            self._preview_res = 1.0

    def set_gui_fps(self, fps):
        """GUI 发布帧率上限（预览面板可选 30/15）。

        ★ 2026-11-12：引擎以前固定 60fps 做 `_gui_scaled`，而预览面板默认
          30fps、可选 15fps ⇒ 一半以上的缩放/信号/拷贝纯属白烧。这里跟随
          预览设置；输出窗口/Spout/NDI 不受影响。
        """
        try:
            self._gui_fps = max(5.0, min(60.0, float(fps)))
        except (TypeError, ValueError):
            self._gui_fps = 30.0

    def set_out_window_live(self, on):
        """输出窗口当前是否可见（决定 `_gui_scaled` 能否吃 preview.res）。

        ★ 单屏预览：输出窗口不可见时，预览就是唯一消费者，允许按
          `preview.res` 降低/提高 GUI 帧；输出窗口可见时保持 1920，
          不拿预览设置去降真正上屏的画质。
        """
        self._out_window_live = bool(on)

    def set_display_routes(self, ids):
        """登记当前**有独立输出窗口**的屏幕 id（多路多显示器用）。"""
        try:
            self._display_route_ids = {str(x) for x in (ids or ()) if x}
        except Exception:                                        # noqa: BLE001
            self._display_route_ids = set()
        if not self._display_route_ids:
            self._display_frames = {}
            self._display_bufs = {}

    def display_frames(self):
        """最新一批「每屏一份」的显示帧（QImage 独立副本，GUI 只能读）。"""
        d = getattr(self, "_display_frames", None)
        return d if isinstance(d, dict) else {}

    def set_gpu_color_offload(self, on):
        """GUI 登记：所有输出窗都是 GPU 视图且没有 NDI/Spout/预览消费者时，
        最终调色交给 GPU 视图，引擎跳过 CPU LUT（省 8~10ms）。"""
        self._gpu_color_offload = bool(on)

    def set_gpu_scene_mode(self, on):
        """GUI 登记：输出窗可接收图层场景（GPU 合成）而不是 CPU 合成帧。"""
        self._gpu_scene_mode = bool(on)
        if not on:
            self._display_scenes = {}

    def display_scenes(self):
        d = getattr(self, "_display_scenes", None)
        return d if isinstance(d, dict) else {}

    def _scene_unavailable(self, why):
        if os.environ.get("AUTOVJ_SCENE_DEBUG") == "1":
            try:
                import stallwatch as _sw
                _sw.log_line("[SCENE] unavailable: %s" % why)
            except Exception:                              # noqa: BLE001
                pass
        return False

    def _gpu_scene_available(self):
        """引擎侧自判：两路以上显示器输出、GPU 开、无 NDI/Spout、无 postfx、
        GUI 被完全遮住、颜色模式为 lut（scene shader 支持）时启用 GPU 场景合成。
        UI 的 set_gpu_scene_mode 只是提示，不再作为唯一开关。"""
        try:
            # ★ 2026-11-25：只有“至少两个显示路由”才启用 GPU 场景。
            #   单可见输出必须走 CPU 全量合成（所有图层/叠加/调色），
            #   否则输出窗会拿到每 3 tick 一次的预览帧，现场就是 ~10fps，
            #   且容易把独立屏 scene 错送到主屏。
            if os.environ.get("AUTOVJ_GPU_SCENE") == "0":
                return False
            if os.environ.get("AUTOVJ_FORCE_SCENE") == "1":
                return True
            # 2026-11-26：UI 只有在“至少两个可见输出窗、且全部是 GPU 视图”时
            # 才把 scene_mode 置真。单可见输出走 CPU 全量合成，避免输出窗每 3 tick
            # 才拿到一次帧（现场 10fps）。
            if not bool(getattr(self, "_gpu_scene_mode", False)):
                return False
            # 2026-11-26：不再依赖 UI 的 display_route_ids（多窗口同步时它可能短暂为空，
            # 会把已经能正常工作的 GPU 场景误判成不可用）。直接看输出计划本身。
            # ★ Config 是对象（只有 __getitem__，没有 get）；统一取底层 dict。
            _cfg = getattr(self.cfg, "data", self.cfg)
            if not bool((_cfg.get("perf") or {}).get("gpu_decode", True)):
                return self._scene_unavailable("gpu_decode off")
            plan = getattr(self, "_out_cfg", None)
            if not plan or len(plan.get("screens") or []) < 2:
                return self._scene_unavailable(
                    "plan screens=%s" % (len((plan or {}).get("screens") or [])))
            if str(plan.get("mode") or "multi") == "single":
                return self._scene_unavailable("mode single")
            o = plan or {}
            if o.get("ndi_enabled") or o.get("spout_enabled"):
                return self._scene_unavailable("legacy ndi/spout")
            for sc in o.get("screens") or []:
                if sc.get("ndi") or sc.get("spout"):
                    return self._scene_unavailable("screen ndi/spout")
            if bool((_cfg.get("postfx") or {}).get("enabled", False)):
                return self._scene_unavailable("postfx on")
            col = _cfg.get("color") or {}
            if bool(col.get("on", False)) and str(col.get("mode", "lut")) != "lut":
                return self._scene_unavailable("color mode %s" % col.get("mode"))
            return True
        except Exception as _e:                                  # noqa: BLE001
            return self._scene_unavailable("exc %s" % _e)

    def display_luts(self):
        """GPU 输出视图的每屏 256x1 LUT；无则空 dict。"""
        d = getattr(self, "_gpu_luts", None)
        return d if isinstance(d, dict) else {}

    def _publish_display_frames(self, frames):
        """为多路输出窗口复制各屏最新帧（30fps，避免把引擎画布引用交给 GUI）。

        返回本 tick 真正生成的 `{sid: QImage}`；被节流跳过时返回 None
        （调用方据此判断能否把同一张私有图复用给 GUI 预览，省一次整帧拷贝）。
        """
        ids = getattr(self, "_display_route_ids", None)
        if not ids:
            self._display_frames = {}
            return None
        now = time.perf_counter()
        # ★ 2026-11-25（两块屏）：≥2 路仍按 30fps 发布；60fps 每 tick 复制会让
        #   GUI 持续重绘、引擎反而掉到 20~25（真机 A/B）。单路保持 60fps。
        try:
            _multi60 = str(os.environ.get("AUTOVJ_DISPLAY_60", "")) == "1"
            _fps = 60.0 if (len(ids) <= 1 or _multi60) else 30.0
        except Exception:                                        # noqa: BLE001
            _fps = 30.0
        # ★ 2026-11-25（真机：显示器窗口只有 22 帧，引擎却是 31 帧）：
        #   这里和 `_publish_gui_frame` 是同一个坑 —— 严格的 1/30s 闸门在
        #   引擎 31~32fps（间隔 32.x ms）时会隔帧丢掉一次，实际只发 ~20fps。
        #   留 15% 公差后 30fps 档的每一帧都能出去。
        _gap = (1.0 / _fps) * 0.70
        if now - float(getattr(self, "_display_t", 0.0) or 0.0) < _gap:
            return None
        self._display_t = now
        out = {}
        self._display_buf_turn = int(getattr(self, "_display_buf_turn", 0)) + 1
        _turn = self._display_buf_turn
        for sid in ids:
            f = (frames or {}).get(sid)
            if f is None:
                continue
            try:
                if f.isNull():
                    continue
                key = (int(f.width()), int(f.height()), f.format())
                bufs = self._display_bufs.get(sid)
                if not bufs or bufs[0][2] != key:
                    # 3 张预分配缓冲：GUI 还拿着上一两张时，下一帧不会覆盖它。
                    bufs = []
                    for _k in range(3):
                        _b = QImage(f.size(), f.format())
                        _arr = np.frombuffer(_b.bits(), np.uint8,
                                             count=_b.sizeInBytes())
                        _b._buf = _arr          # 防 GC / 避免每次都 bits() 触发 detach
                        bufs.append((_b, _arr, key))
                    self._display_bufs[sid] = bufs
                buf, arr, _ = bufs[_turn % 3]
                if (int(f.bytesPerLine()) == int(f.width()) * 4
                        and int(buf.bytesPerLine()) == int(buf.width()) * 4):
                    np.copyto(arr, np.frombuffer(
                        f.constBits(), np.uint8, count=f.sizeInBytes()))
                    out[sid] = buf
                else:
                    out[sid] = f.copy()
            except Exception:                                    # noqa: BLE001
                try:
                    out[sid] = f.copy()
                except Exception:                                # noqa: BLE001
                    continue
        self._display_frames = out
        return out

    def set_route_thumb_consumers(self, on):
        """输出页是否可见：不可见时引擎不再生成多屏缩略图（省 7~8 次缩放/帧）。

        ★ 2026-11-11：**故意不加 `@_locked`**。这里只写一个 bool（GIL 下原子），
        而 `showEvent/hideEvent` 在 GUI 线程调用；加锁会在引擎忙时超时丢弃，
        导致用户切回输出页后缩略图一直不更新。引擎每帧读它即可。
        """
        self._route_thumb_consumers = bool(on)

    def set_gui_covered(self, on):
        """主窗口是否已被输出窗完全盖住（GUI 每帧写；GIL 下原子，无需加锁）。

        ★ 2026-11-25 真机：输出全屏盖住主窗时，缩略图/预览根本看不见，但引擎
          仍每帧为每块屏做 960px 缩放（多屏 4~7ms）。盖住时直接停掉缩略图。
        """
        self._gui_covered = bool(on)

    def _hidden_screen_ids(self):
        """被完全遮住、且没有任何输出路由的屏幕 id（可暂停合成/解码）。

        判定：GUI 主窗被输出窗盖住 + 该屏没有输出窗口（`display` 未设置）+
        没有 NDI/Spout 路由。这种屏用户看不到、也没有接收端，没必要每帧合成，
        更没必要让它的解码器占着 GIL（多屏 20 帧的一大来源）。
        主窗一露出来（`_gui_covered=False`）立即恢复全量，预览不会黑。
        """
        if not bool(getattr(self, "_gui_covered", False)):
            return set()
        plan = getattr(self, "_out_cfg", None)
        if not plan:
            return set()
        shown = set(getattr(self, "_display_route_ids", None) or ())
        screens = [s for s in (plan.get("screens") or []) if isinstance(s, dict)]
        for s in screens:
            if s.get("include", True) is False:
                continue
            if s.get("spout") or s.get("ndi"):
                sid = str(s.get("id") or "")
                if sid:
                    shown.add(sid)
        hidden = set()
        for s in screens:
            sid = str(s.get("id") or "")
            if not sid or sid in shown:
                continue
            if str(s.get("type") or "") in ("main", "independent"):
                hidden.add(sid)
        return hidden

    def set_preview_route(self, sid):
        """选择本地输出窗口/主预览要显示哪一路（多路模式）。"""
        self._preview_route_id = str(sid or "")

    def _out_layers_by_ids(self, ids):
        if ids is None:
            return list(self.layers)
        want = set(str(x) for x in ids)
        return [l for l in self.layers if str(getattr(l, "lid", "")) in want]

    def _rebuild_output_plan(self):
        """在引擎线程里按 `_out_cfg` 重建画布/发送端。调用者已持 `_tick_lock`。"""
        plan = self._out_cfg
        if plan:
            try:
                self._close_legacy_output()
            except Exception:                                  # noqa: BLE001
                pass
        self._plan_rev_ready = self._plan_rev
        if not plan:
            self._close_all_route_senders()
            self._spout = None
            self._ndi = None
            self._screen_canvases.clear()
            self._route_frames.clear()
            self._combined_canvas = None
            self._combined_key = None
            return

        # ★ 2026-11-17（用户：多屏多路按屏开 alpha 仍不透明）：先按**每块屏自己的**
        #   `alpha` 标志算出格式，再建画布。以前全用一个全局 `_alpha_out`：
        #   · 任一屏开 alpha ⇒ 所有屏都 ARGB32（多花内存，但没错）；
        #   · 更要命的是反过来——如果全局并集来自别的屏/残留值，本屏拿到的
        #     可能就是 RGB32 ⇒ **本屏的透明通道从画布这一步就没了**。
        #   现在：独立屏/切分屏各用各的格式；主画布（主屏/复制屏/复用主屏的屏共用）
        #   取这几条路由的并集 ⇒ 只有真的用到主画布的屏要 alpha 时它才是 ARGB32。
        _per = {}
        _main_alpha = False
        try:
            _all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        except Exception:                                      # noqa: BLE001
            _all_ids = []
        for _s in (plan.get("screens") or []):
            _sid = str(_s.get("id") or "")
            if not _sid:
                continue
            _on = bool(_s.get("alpha", False))
            _per[_sid] = _on
            _t = str(_s.get("type") or "")
            if _t in ("main", "copy"):
                _main_alpha = _main_alpha or _on
            elif _t == "independent":
                try:
                    if self._independent_reuses_main(plan, _s, _all_ids):
                        _main_alpha = _main_alpha or _on
                except Exception:                              # noqa: BLE001
                    pass
        if str(plan.get("mode") or "multi") == "single":
            # 单路只有一张整合画布：任一屏要 alpha 就必须 ARGB32；
            # 同时尊重遗留全局开关（单路界面上没有"按屏"的概念）。
            _main_alpha = bool(_main_alpha or any(_per.values())
                               or self._alpha_out)
        self._screen_alpha = _per
        self._any_alpha = bool(any(_per.values()) or self._alpha_out)
        # ⚠ 只有配置里**真的存在** `alpha` 键时才让计划说了算；老配置（从来没勾过）
        #   保留遗留全局开关的行为，避免升级后被强制关掉。
        _has_key = any("alpha" in _s for _s in (plan.get("screens") or []))
        if _has_key and bool(_main_alpha) != bool(self._alpha_out):
            self._alpha_out = bool(_main_alpha)
            self._latest_frame = None
        fmt = self._canvas_format()

        main = output_model.main_screen(plan)
        if main is not None:
            mw, mh = output_model.effective_size(plan, main)
            mw, mh = self._rs_size(mw, mh)
            if (self.canvas.width() != mw or self.canvas.height() != mh
                    or self.canvas.format() != fmt):
                self.canvas = QImage(mw, mh, fmt)
                self._clear_canvas(self.canvas)
                self._latest_frame = None
        # 独立屏幕画布（主屏用 self.canvas；copy/split 不单独合成）
        keep = set()
        for s in (plan.get("screens") or []):
            st = s.get("type")
            sid = str(s.get("id") or "")
            if not sid:
                continue
            if st == "independent":
                w, h = self._rs_size(*output_model.effective_size(plan, s))
                # ★ 每屏自己的 alpha：本屏没勾就是 RGB32（实底），勾了才 ARGB32。
                _f = self._canvas_format(self._screen_alpha.get(sid, self._alpha_out))
                cv = self._screen_canvases.get(sid)
                if (cv is None or cv.width() != w or cv.height() != h
                        or cv.format() != _f):
                    cv = QImage(w, h, _f)
                    self._screen_canvases[sid] = cv
                self._clear_canvas(cv)
                keep.add(sid)
            elif st == "split":
                w, h = self._rs_size(*output_model.effective_size(plan, s))
                _f = self._canvas_format(self._screen_alpha.get(sid, self._alpha_out))
                cv = self._screen_canvases.get(sid)
                if (cv is None or cv.width() != w or cv.height() != h
                        or cv.format() != _f):
                    cv = QImage(w, h, _f)
                    self._screen_canvases[sid] = cv
                self._clear_canvas(cv)
                keep.add(sid)
        for sid in list(self._screen_canvases.keys()):
            if sid not in keep:
                self._screen_canvases.pop(sid, None)
        # 整合单路画布
        if str(plan.get("mode") or "multi") == "single":
            # ⚠ 整合画布**不缩放**：布局坐标（screen_rect）是按原始分辨率算的，
            #   缩放画布会让 x/y 越界/错位。各屏画布缩小后在这里按原 slot 放大。
            cw, ch = output_model.combined_size(plan)
            if (self._combined_canvas is None
                    or self._combined_canvas.width() != cw
                    or self._combined_canvas.height() != ch
                    or self._combined_canvas.format() != fmt):
                self._combined_canvas = QImage(cw, ch, fmt)
                self._clear_canvas(self._combined_canvas)
            self._combined_key = (cw, ch)
        else:
            self._combined_canvas = None
            self._combined_key = None
        self._layer_screen_map = None       # ★ U7：屏幕/图层归属可能变了 ⇒ 重算映射
        # 如果启动时输出计划还没下发，GPU 上下文会先建成 standalone；等主屏
        # display>=0 的计划到了，必须重建为原生输出窗（否则 Qt 窗仍在，Spout 也会走旧路）。
        try:
            _pmain = output_model.main_screen(plan) if plan else None
            _pdisp = int((_pmain or {}).get("display", -2))
            if getattr(self, "_render_active", False):
                _want_native = bool(_pdisp >= 0)
                if _want_native != bool(getattr(self, "_gpu_native", False)):
                    self._gpu_native = _want_native
                    try:
                        self.native_display_changed.emit(_want_native)
                    except Exception:                      # noqa: BLE001
                        pass
                self._render_note("输出计划更新：render native=%s disp=%s"
                                  % (_want_native, _pdisp))
            if (_pdisp >= 0 and str(os.environ.get("AUTOVJ_GPU_NATIVE", "1")) != "0"
                    and self._gpu_stage is not None
                    and not self.gpu_native_display()):
                try:
                    self._gpu_stage.close()
                except Exception:                          # noqa: BLE001
                    pass
                self._gpu_stage = None
                self._gpu_stage_dead = False
                self._gpu_native = False
                self._init_gpu_stage()
        except Exception:                                      # noqa: BLE001
            pass
        # ★ 2026-11-26：Spout 交给 GPU 合成器在同一个 GL 上下文里直接发 FBO，
        #   不再走 "QImage → Spout 线程另开 GL 上下文"（那是 1080p 慢帧的来源）。
        _stage_sp = set()
        _maps = {}
        if self._gpu_stage is not None or getattr(self, "_render_active", False):
            try:
                for _sid, _kind, _name, _sp, _nd in self._route_specs():
                    if not _sp:
                        continue
                    if _kind == "combined":
                        # 单路时发送名属于主屏；渲染进程按主屏 sid 合成。
                        _pm = output_model.main_screen(plan) if plan else None
                        _sid = str((_pm or {}).get("id") or _sid)
                    _stage_sp.add(str(_sid))
                    _maps[str(_sid)] = str(_name)
                if self._gpu_stage is not None and hasattr(
                        self._gpu_stage, "set_spout_targets"):
                    self._gpu_stage.set_spout_targets(_maps)
            except Exception as _e:                            # noqa: BLE001
                self._gpu_log("GPU Spout 路由登记失败：%s" % _e)
        self._stage_spout_sids = _stage_sp
        self._sync_route_senders()
        self._route_frames.clear()

    def _route_specs(self):
        """返回当前需要发送/预览的路由：[(route_id, frame_kind, name, spout, ndi)]。"""
        plan = self._out_cfg
        if not plan:
            return []
        mode = str(plan.get("mode") or "multi")
        main = output_model.main_screen(plan)
        if mode == "single":
            if main is None:
                return []
            # ★ V6（用户第 8 条）：单路下发送端名也要跟**屏名**走。
            #   以前读遗留字段 `plan["spout_name"]`（永远是 EasyRealityAutoVJ），
            #   用户把屏改名成 898 之后 Spout/NDI 名却不变 ⇒ 找不到输出。
            try:
                _nm = output_model.screen_output_names(plan).get(str(main.get("id") or ""))
            except Exception:                                      # noqa: BLE001
                _nm = None
            return [("__combined__", "combined",
                     str(_nm or plan.get("spout_name") or "EasyRealityAutoVJ"),
                     bool(main.get("spout", False)), bool(main.get("ndi", False)))]
        names = output_model.screen_output_names(plan)
        out = []
        for s in plan.get("screens") or []:
            sid = str(s.get("id") or "")
            if not sid:
                continue
            # ★ C（2026-11-10）：「参与输出」关掉的屏**不发送**（也不拼进整合画布，
            #   整合画布那边由 `layout_sources()` 过滤）。
            if s.get("include", True) is False:
                continue
            out.append((sid, str(s.get("type") or "independent"),
                        names.get(sid) or s.get("name") or sid,
                        bool(s.get("spout", False)), bool(s.get("ndi", False))))
        return out

    def _sync_route_senders(self):
        """按当前路由表增删 NDI/Spout 发送端；旧发送端先关再建。"""
        plan = self._out_cfg
        specs = self._route_specs()
        want = {sid: (name, sp, nd) for sid, _kind, name, sp, nd in specs}
        # 删除不再需要的路由
        for sid in list(self._route_senders.keys()):
            if sid not in want:
                self._close_route_sender(sid)
        # 创建/更新
        for sid, (name, want_sp, want_nd) in want.items():
            if ((self._gpu_stage is not None
                 or getattr(self, "_render_active", False))
                    and str(sid) in (getattr(self, "_stage_spout_sids", None) or set())):
                want_sp = False
            st = self._route_senders.get(sid)
            if st is None:
                st = {"spout": None, "ndi": None, "spout_name": "", "ndi_name": ""}
                self._route_senders[sid] = st
            if st.get("spout") is not None and st.get("spout_name") != name:
                self._close_route_spout(sid)
            if st.get("ndi") is not None and st.get("ndi_name") != name:
                self._close_route_ndi(sid)
            if want_sp and st.get("spout") is None:
                try:
                    import spout_out
                    st["spout"] = spout_out.SpoutOut(name)
                    st["spout_name"] = name
                except Exception as e:                             # noqa: BLE001
                    _output_health_write("Spout 路由 %s 创建失败：%s" % (name, e))
            if not want_sp and st.get("spout") is not None:
                self._close_route_spout(sid)
            if want_nd and st.get("ndi") is None:
                try:
                    from ndi_out import NDIOutput
                    st["ndi"] = NDIOutput(name)
                    st["ndi_name"] = name
                except Exception as e:                             # noqa: BLE001
                    _output_health_write("NDI 路由 %s 创建失败：%s" % (name, e))
            if not want_nd and st.get("ndi") is not None:
                self._close_route_ndi(sid)
            self._route_state.setdefault(sid, {"spout_fail": 0, "spout_retry": 0.0,
                                               "ndi_fail": 0, "ndi_retry": 0.0,
                                               "log_t": 0.0})
        # ★★ BUG-A 根因（2026-11-10）：这里原来把**主屏路由的发送端对象**
        #   赋给了 legacy 字段 `self._spout` / `self._ndi`。而 `set_output_config()`
        #   在多路下每次都会调 `_close_legacy_output()`（改分辨率/改名/选屏/裁切都会触发）
        #   ⇒ 它把主屏那条 Spout 的 `SpoutOut.close()` 掉了（`_dead=True`、线程退出），
        #   而 `send()` 仍返回 True、`_route_senders[main]["spout"]` 仍非 None
        #   ⇒ **永久哑火且零报错**，OBS 里只剩一个 sender。
        #   修法：多路模式下 legacy 字段**绝不指向路由发送端**，一律为 None
        #   （`_spout_send()` / `_ndi_send()` 只在 `self._out_cfg is None` 的 legacy 分支被调用）。
        self._ndi = None
        self._spout = None

    def _close_route_spout(self, sid):
        st = self._route_senders.get(sid) or {}
        sp = st.get("spout")
        st["spout"] = None
        st["spout_name"] = ""
        if sp is not None:
            try:
                sp.close()
            except Exception:                                     # noqa: BLE001
                pass

    def _close_route_ndi(self, sid):
        st = self._route_senders.get(sid) or {}
        nd = st.get("ndi")
        st["ndi"] = None
        st["ndi_name"] = ""
        if nd is not None:
            try:
                nd.close()
            except Exception:                                     # noqa: BLE001
                pass

    def _close_route_sender(self, sid):
        self._close_route_spout(sid)
        self._close_route_ndi(sid)
        self._route_senders.pop(sid, None)
        self._route_state.pop(sid, None)

    def _close_all_route_senders(self):
        for sid in list(self._route_senders.keys()):
            self._close_route_sender(sid)

    def _build_gpu_scenes(self, snap):
        """构建每屏 GPU 合成场景（图层纹理 + 目标矩形 + 混合 + LUT）。

        只有全部图层都是普通/screen/add、无转场、无码表接管、无按屏独立调色时
        才返回场景；任何一项不满足返回 None，调用方回退 CPU 合成。
        """
        plan = self._out_cfg
        if not plan or str(plan.get("mode") or "multi") != "multi":
            return None
        try:
            main = output_model.main_screen(plan)
            if main is None:
                return None
            all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
            mw, mh = output_model.effective_size(plan, main)
            try:
                self._cp_cfg = (output_model.effective_params(self.cfg, main)
                                if ((main or {}).get("params")
                                    and str((main or {}).get("type") or "") != "main")
                                else None)
            except Exception:                                  # noqa: BLE001
                self._cp_cfg = None
            self._apply_color_postfx(snap, lut_only=True)
            _mlut = getattr(self, "_gpu_lut_img", None)
            _msid = str(main.get("id") or "")
            if _mlut is not None:
                self._gpu_luts[_msid] = _mlut
            _mdesc = self._gpu_simple_layers(
                self._main_screen_layers(), snap, int(mw), int(mh))
            if not _mdesc:
                if os.environ.get("AUTOVJ_SCENE_DEBUG") == "1":
                    try:
                        import stallwatch as _sw
                        _bad = []
                        for _l in self._main_screen_layers():
                            _bad.append("%s vis=%s tr=%s cs=%s/%s blend=%s p=%s"
                                        % (getattr(_l, "name", "?"),
                                           getattr(_l, "visible", None),
                                           getattr(_l, "trans_active", None),
                                           getattr(_l, "is_cuesheet", None),
                                           getattr(_l, "cs_active", None),
                                           getattr(_l, "blend", None),
                                           bool(getattr(_l, "player", None))))
                        _sw.log_line("[SCENE] main fallback: " + " | ".join(_bad))
                    except Exception:                      # noqa: BLE001
                        pass
                return None
            scenes = {_msid: {"w": int(mw), "h": int(mh),
                              "layers": _mdesc, "lut": _mlut}}
            _hidden = set(getattr(self, "_hidden_sids", None) or ())
            for s in plan.get("screens") or []:
                if str(s.get("type") or "") != "independent":
                    continue
                sid = str(s.get("id") or "")
                if not sid or sid in _hidden:
                    continue
                if self._independent_reuses_main(plan, s, all_ids):
                    return None
                _par = (s.get("params") or {})
                _cpar = _par.get("color") or {}
                # 按屏独立调色暂时回退 CPU（场景 LUT 只支持全局主屏调色）
                if _cpar and bool(_cpar.get("on", True)):
                    return None
                try:
                    self._cp_cfg = (output_model.effective_params(self.cfg, s)
                                    if _par else None)
                except Exception:                          # noqa: BLE001
                    self._cp_cfg = None
                w, h = output_model.effective_size(plan, s)
                layers = self._out_layers_by_ids(
                    output_model.effective_layers(plan, s, all_ids))
                desc = self._gpu_simple_layers(layers, snap, int(w), int(h))
                if not desc:
                    return None
                scenes[sid] = {"w": int(w), "h": int(h),
                               "layers": desc, "lut": None}
            self._cp_cfg = None
            if os.environ.get("AUTOVJ_SCENE_DEBUG") == "1":
                try:
                    import stallwatch as _sw
                    _sw.log_line("[SCENE] ok " + " ".join(
                        "%s:%d层" % (k, len(v.get("layers") or []))
                        for k, v in scenes.items()))
                except Exception:                      # noqa: BLE001
                    pass
            return scenes
        except Exception:                                      # noqa: BLE001
            self._cp_cfg = None
            return None

    def _compose_independent(self, snap):
        """合成所有独立屏幕（copy/split 不单独合成）。"""
        plan = self._out_cfg
        if not plan:
            return
        all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        # 每帧重置"按屏调色跑了几次"（原来在 `_apply_screen_cp()` 里重置，那边已删）
        self._screen_cp_calls = 0
        _hidden = set(getattr(self, "_hidden_sids", None) or ())
        _gpu_color = bool(getattr(self, "_gpu_color_offload", False)
                          and getattr(self, "_gui_covered", False))
        _shown = set(getattr(self, "_display_route_ids", None) or ())
        _sel = str(getattr(self, "_preview_route_id", "") or "")
        if _sel:
            _shown.add(_sel)
        _thumbs = bool(getattr(self, "_route_thumb_consumers", False))
        _net_all = bool(plan.get("ndi_enabled") or plan.get("spout_enabled"))
        for s in plan.get("screens") or []:
            if s.get("type") != "independent":
                continue
            sid = str(s.get("id") or "")
            # ★ 2026-11-25：被完全遮住且无输出路由的屏，本帧不合成（保留上一帧）。
            if sid and sid in _hidden:
                continue
            # ★ 2026-11-26：没人看的独立屏不合成（没有输出窗 / 不是当前预览路由 /
            #   输出页没开 / 没有 NDI-Spout 接收端）。和 GPU 路径同一口径。
            if (not _thumbs and sid not in _shown and not _net_all
                    and not (s.get("ndi") or s.get("spout"))):
                continue
            cv = self._screen_canvases.get(sid)
            if cv is None:
                continue
            if self._independent_reuses_main(plan, s, all_ids):
                continue
            if sid in getattr(self, "_reused_screens", set()):
                # 主屏染色前的副本已放进 cv；这里只套本屏自己的颜色（通常 off）
                try:
                    self._cp_cfg = (output_model.effective_params(self.cfg, s)
                                    if (s or {}).get("params") else None)
                except Exception:                          # noqa: BLE001
                    self._cp_cfg = None
                try:
                    self._apply_color_postfx(snap, canvas=cv, advance=False,
                                             state_key=sid, lut_only=_gpu_color)
                    if _gpu_color and getattr(self, "_gpu_lut_img", None) is not None:
                        self._gpu_luts[sid] = self._gpu_lut_img
                    self._screen_cp_calls += 1
                except Exception:                          # noqa: BLE001
                    pass
                self._cp_cfg = None
                continue
            ids = output_model.effective_layers(plan, s, all_ids)
            layers = self._out_layers_by_ids(ids)
            _has_ov = bool((s or {}).get("params"))
            # ★ BUG-D 波1：**合成前**就把该屏参数挂上 —— `_compose_to()` 里的
            #   "振幅强度(intensity)" 要读本屏的 auto 段。
            # ★★ 2026-11-10（一级问题，规则 0）：**读参数失败绝不能跳过合成**。
            #   以前这两句在同一个 try 里 ⇒ `effective_params` 抛异常时 `_compose_to()` 被跳过
            #   ⇒ 带 params 的独立屏**整块黑屏**（用户"一改画面设置这屏就黑了"）。
            #   现在：取参数单独 try，合成**无条件执行**。
            try:
                self._cp_cfg = (output_model.effective_params(self.cfg, s)
                                if _has_ov else None)
            except Exception as _e:                            # noqa: BLE001
                self._cp_cfg = None
                self._section_log("屏参数读取失败（退回全局，合成继续）：%s" % _e)
            self._compose_to(cv, layers, snap, allow_gpu=True)
            # ★ B2 + 2026-11-10（QA 实测「同一配置下两块屏颜色不一致」）：
            #   以前**只有带 params 的屏**才套色 ⇒ 带参数的屏被调了色、不带的屏原样输出
            #   （实测主屏 255,24,28 vs 独立屏 20,200,20）。
            #   现在口径统一：**每块独立屏都用自己的生效参数套一次**（无 override ⇒ 全局，
            #   与主屏在 tick 里那次完全同源）。`_apply_color_postfx` 内部"没效果不读回"，
            #   所以全局没开颜色/后处理时这里仍然零搬运。
            try:
                if not _has_ov:
                    # ⚠ 传 `self.cfg` 即可：`effective_params` 已兼容 Config 对象/普通 dict
                    #   （生产是 Config，单测夹具是 dict，两边都能吃）。
                    self._cp_cfg = output_model.effective_params(self.cfg, s)
                self._apply_color_postfx(snap, canvas=cv, advance=False,
                                         state_key=sid, lut_only=_gpu_color)
                if _gpu_color and getattr(self, "_gpu_lut_img", None) is not None:
                    self._gpu_luts[sid] = self._gpu_lut_img
                self._screen_cp_calls += 1
            except Exception as _e:                            # noqa: BLE001
                self._section_log("屏调色失败（跳过本屏调色）：%s" % _e)
            self._cp_cfg = None

    def _screen_can_reuse_main(self, plan, s, all_ids):
        """从屏能否直接复用主屏**未染色**画布（只差 color/强度）。

        ★ 2026-11-11：`dj` 是 follow_main 的从屏，图层/尺寸与主屏相同，params 只有
          `auto.intensity` + `color`（postfx 全局关闭）。旧实现每帧重合成 10 层+码表
          （现场 ci 8~88ms），是 CPU 30%+ 的主要来源之一。
        """
        try:
            if not s or s.get("type") != "independent" or not s.get("follow_main"):
                return False
            if self._independent_reuses_main(plan, s, all_ids):
                return True                    # 无 params：`_route_frame` 已直接返回主画布
            params = s.get("params") or {}
            if not params:
                return True
            allowed = {"color", "auto", "postfx"}
            if set(str(k) for k in params.keys()) - allowed:
                return False
            auto = params.get("auto") or {}
            if auto and set(str(k) for k in auto.keys()) - {"intensity"}:
                return False
            # postfx 关闭时才允许（从屏的 postfx.mode 只是存储，关闭时无效果）
            try:
                if bool((self.cfg.get("postfx") or {}).get("enabled", False)):
                    return False
            except Exception:                                  # noqa: BLE001
                return False
            mw, mh = output_model.effective_size(plan, output_model.main_screen(plan))
            sw, sh = output_model.effective_size(plan, s)
            if (int(sw), int(sh)) != (int(mw), int(mh)):
                return False
            # ★ 2026-11-18：主/从屏 alpha 设定不同时不能复用同一张画布
            #   （否则 RGB32/ARGB32 格式串用，OBS 端不是透明就是全黑）。
            try:
                _main = output_model.main_screen(plan) or {}
                _ma = bool(self._screen_alpha.get(str(_main.get("id") or ""),
                                                  self._alpha_out))
                _sa = bool(self._screen_alpha.get(str(s.get("id") or ""), self._alpha_out))
                if _ma != _sa:
                    return False
            except Exception:                                  # noqa: BLE001
                return False
            return set(output_model.effective_layers(plan, s, all_ids)) == set(all_ids)
        except Exception:                                      # noqa: BLE001
            return False

    def _copy_main_for_reused_screens(self, snap):
        """主屏**染色前**复制给可复用的从屏；记录 sid，供 `_compose_independent` 跳过。"""
        plan = self._out_cfg
        if not plan:
            return
        if str(plan.get("mode") or "multi") != "single":
            return
        all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        main = output_model.main_screen(plan)
        if main is None:
            return
        mw, mh = output_model.effective_size(plan, main)
        if int(self.canvas.width()) != int(mw) or int(self.canvas.height()) != int(mh):
            return
        for s in plan.get("screens") or []:
            if not self._screen_can_reuse_main(plan, s, all_ids):
                continue
            sid = str(s.get("id") or "")
            if not sid:
                continue
            # 无 params 的屏本来就 `_route_frame` 返回 main（不占画布）；
            # 有 params 的才需要一份独立像素。
            if not (s.get("params") or {}):
                continue
            cv = self.canvas.copy()
            self._screen_canvases[sid] = cv
            self._reused_screens.add(sid)

    def _independent_reuses_main(self, plan, s, all_ids=None):
        """“跟随主屏 + 图层集合与主屏一致 + 分辨率一致”时直接复用主屏画布。"""
        if not s or s.get("type") != "independent" or not s.get("follow_main"):
            return False
        try:
            if s.get("params"):
                return False          # 有按屏颜色/后处理 override ⇒ 不能直接复用主屏原图
            if all_ids is None:
                all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
            main = output_model.main_screen(plan)
            if main is None:
                return False
            mw, mh = output_model.effective_size(plan, main)
            sw, sh = output_model.effective_size(plan, s)
            if (int(sw), int(sh)) != (int(mw), int(mh)):
                return False
            return set(output_model.effective_layers(plan, s, all_ids)) == set(all_ids)
        except Exception:
            return False

    def _build_combined(self, snap):
        """单路模式：把主屏/独立屏按布局拼到整合画布。"""
        plan = self._out_cfg
        cv = self._combined_canvas
        if not plan or cv is None:
            return
        # ★ 2026-11-11：整合画布**只在布局变更那一帧清一次**，不再每帧整幅填充。
        #   3000×900 的 numpy fill 每帧约 1ms；屏幕覆盖区每帧都会被完整重画，
        #   空出来的黑底只会在布局变更时出现，所以按 `_plan_rev_ready` 清一次足够。
        _rev = int(getattr(self, "_plan_rev_ready", -1))
        if int(getattr(self, "_combined_clear_rev", -999)) != _rev:
            self._clear_canvas(cv)
            self._combined_clear_rev = _rev
        # ★ 2026-11-11：1:1 的屏直接 numpy 拷贝（释放 GIL），只有需要缩放或
        #   越界的屏才走 QPainter。顺序保持 layout_sources 原顺序；一旦开了
        #   QPainter，后面的屏全部走 QPainter，避免"直接写"覆盖已画的层。
        p = None
        for _si, s in enumerate(output_model.layout_sources(plan)):
            if _si:
                try:
                    time.sleep(0)      # 每块屏之间让出 GIL（5 屏拼接也能挡 GUI 几十 ms）
                except Exception:                          # noqa: BLE001
                    pass
            sid = str(s.get("id") or "")
            frame = self._route_frame(plan, sid, str(s.get("type") or "independent"))
            if frame is None or frame.isNull():
                continue
            x, y, w, h = output_model.screen_rect(plan, s)
            if (p is None and int(frame.width()) == int(w)
                    and int(frame.height()) == int(h)
                    and self._blit_1to1(cv, x, y, frame)):
                try:
                    time.sleep(0)
                except Exception:                          # noqa: BLE001
                    pass
                continue
            if p is None:
                p = QPainter(cv)
                # ★ alpha 输出：用 Source 直接覆盖（含 alpha），别用默认 SourceOver
                #   把透明底与半透明内容叠出不正确的 alpha；不透明区域仍是 255。
                p.setCompositionMode(QPainter.CompositionMode_Source)
                p.setRenderHint(QPainter.SmoothPixmapTransform)
            if frame.width() == w and frame.height() == h:
                p.drawImage(int(x), int(y), frame)
            else:
                p.drawImage(QRectF(x, y, w, h), frame,
                            QRectF(0, 0, frame.width(), frame.height()))
            try:
                time.sleep(0)
            except Exception:                              # noqa: BLE001
                pass
        if p is not None:
            p.end()

    def _split_frame(self, plan, screen):
        """切分屏幕：把源画布按 UV 裁到复用缓冲。

        ★ 性能：同一块 split 一帧内会被 `_build_combined()` 和 `_publish_multi_frame()`
          各调一次（实测 4 块 split → 8 次/帧）。这里按 sid 缓存本帧结果，帧首清空。
        """
        sid = str(screen.get("id") or "")
        _cache = getattr(self, "_split_cache", None)
        if _cache is None:
            _cache = {}
            self._split_cache = _cache
        if sid in _cache:
            return _cache[sid]
        cv = self._screen_canvases.get(sid)
        _sf = self._canvas_format(self._screen_alpha.get(sid, self._alpha_out))
        if cv is None:
            w, h = output_model.effective_size(plan, screen)
            cv = QImage(w, h, _sf)
            self._screen_canvases[sid] = cv
        src = source_frame = None
        src_screen = output_model.source_screen(plan, screen)
        if src_screen is not None:
            ssid = str(src_screen.get("id") or "")
            if (src_screen.get("type") == "main"
                    or self._independent_reuses_main(plan, src_screen)):
                source_frame = self.canvas
            else:
                source_frame = self._screen_canvases.get(ssid)
        if source_frame is None or source_frame.isNull():
            self._clear_canvas(cv)
            _cache[sid] = cv
            return cv
        u0, v0, u1, v1 = screen.get("uv") or [0, 0, 1, 1]
        sw, sh = source_frame.width(), source_frame.height()
        sx = max(0, min(sw - 1, int(round(sw * float(u0)))))
        sy = max(0, min(sh - 1, int(round(sh * float(v0)))))
        tw = max(1, min(sw - sx, int(round(sw * (float(u1) - float(u0))))))
        th = max(1, min(sh - sy, int(round(sh * (float(v1) - float(v0))))))
        # ⚠ 2026-11-10 真机回归：这里一度"优先用 screen['width'/'height'] 出图"，
        #   结果老配置里被写脏的 `w=47 / h=720`（UV 其实是全幅）直接把切分屏裁成
        #   一条 47 像素宽的竖条。**UV 才是权威**，尺寸只能由 UV 反算；
        #   用户改分辨率走 `_on_screen_setup_changed()` 反算 UV，不在这里覆盖。
        if cv.width() != tw or cv.height() != th or cv.format() != _sf:
            cv = QImage(tw, th, _sf)
            self._screen_canvases[sid] = cv
        p = QPainter(cv)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawImage(0, 0, source_frame, sx, sy, tw, th)
        p.end()
        _cache[sid] = cv
        return cv

    def _route_frame(self, plan, sid, kind):
        if kind == "combined":
            return self._combined_canvas
        s = output_model.screen_by_id(plan, sid)
        if s is None:
            return None
        t = s.get("type")
        if t == "main":
            return self.canvas
        if t == "independent":
            if self._independent_reuses_main(plan, s):
                return self.canvas
            return self._screen_canvases.get(sid)
        if t == "copy":
            return self.canvas
        if t == "split":
            return self._split_frame(plan, s)
        return None

    def _preview_thumb_side(self, frame):
        """按**该屏输出分辨率** × preview.res 决定缩略图边长。

        用户要求「预览用输出屏幕设置的分辨率」：1280×720 的屏就生成 1280 宽的
        缩略图，1080p 屏就 1920 宽；只有整合画布/超宽屏用 1920 硬上限保护内存。
        """
        try:
            w = int(frame.width())
        except Exception:                                      # noqa: BLE001
            w = PREVIEW_THUMB_MAX
        try:
            res = float(getattr(self, "_preview_res", 1.0) or 1.0)
        except (TypeError, ValueError):
            res = 1.0
        res = max(0.1, min(1.0, res))
        side = int(round(max(32, w) * res))
        return max(64, min(PREVIEW_THUMB_MAX, side))

    def _publish_multi_frame(self):
        """生成缩略图 + 选中路由的全分辨率预览帧；返回是否有新帧。"""
        plan = self._out_cfg
        if not plan:
            return False
        self._thumb_tick += 1
        # ★ 2026-11-11：缩略图**每 tick 都生成**（与 OBS/Spout 同帧），不再按屏数降频。
        #   5 屏 320px 约 2~3ms/tick；预览内容必须和输出同速，否则就是"OBS 流畅、预览掉帧"。
        _nscr = len(plan.get("screens") or [])
        _every = max(1, int(getattr(self, "_thumb_every", 1) or 1))
        # ★★★ 2026-11-11（用户：多屏预览比 OBS 掉帧多）：这里曾把 ≥4 屏的缩略图
        #   降到 10fps（每 3 tick 一次）。但 OBS 的 Spout 发的是**每 tick 的全帧**，
        #   于是 OBS 30fps、软件内预览只有 10fps 内容 —— 用户看到的正是"预览掉帧"。
        #   320px 缩略图实测 0.3~0.5ms/屏，5 屏每 tick 约 2~3ms，换来预览与输出同速。
        #   是否生成仍由 `_route_thumb_consumers`（输出页是否可见）控制。
        do_thumbs = (bool(getattr(self, "_route_thumb_consumers", True))
                     and not bool(getattr(self, "_gui_covered", False))
                     and (self._thumb_tick % _every) == 0)
        mode = str(plan.get("mode") or "multi")
        thumbs = {}
        frames = {}
        if mode == "single":
            sid = "__combined__"
            frame = self._combined_canvas
            if frame is not None:
                frames[sid] = frame
                # ★ 2026-11-11：单路下每块屏都有自己的缩略图，整合画布那张
                #   （3000×900 → 320）是最大、最贵的一次缩放，而且预览卡片
                #   只把它当兜底。只有在**没有任何屏幕**时才生成它。
                if do_thumbs and not (plan.get("screens") or []):
                    thumbs[sid] = _preview_thumb(frame, self._preview_thumb_side(frame))
                # ★★ 2026-11-10（用户）：**预览卡片与输出画布解耦**。
                #   Mapping 只负责摆位置 / 定分辨率 / 选哪些屏参与输出；
                #   预览里每块屏必须显示它**自己的完整画面**，与它在整合画布里摆在哪无关。
                #   ⚠ 绝不从这里按 `screen_rect()` 去 `_combined_canvas.copy(...)`：
                #     那会让拖动 layout 改变预览内容，布局超出画布时还会 clamp 截短 ⇒
                #     用户看到"预览画面被裁掉"（上一版就是这个错）。
                #   · 遍历 **plan["screens"] 全部屏**：`include=False` 的屏不进输出，
                #     但预览里仍应能看到它自己的画面。
                for _sc in plan.get("screens") or []:
                    _sid2 = str(_sc.get("id") or "")
                    if not _sid2:
                        continue
                    _f2 = self._route_frame(plan, _sid2,
                                            str(_sc.get("type") or "independent"))
                    if _f2 is None or _f2.isNull():
                        continue
                    frames[_sid2] = _f2
                    if do_thumbs:
                        thumbs[_sid2] = _preview_thumb(_f2, self._preview_thumb_side(_f2))
                        try:
                            time.sleep(0)      # 每块屏缩略图后让出 GIL
                        except Exception:                  # noqa: BLE001
                            pass
            self._route_frames = frames
            if do_thumbs:
                self._route_thumb_rev = int(getattr(self, "_route_thumb_rev", 0)) + 1
                self._rate_thumbs = int(getattr(self, "_rate_thumbs", 0)) + 1
                self._route_thumbs = thumbs
            if (not bool(getattr(self, "_gui_covered", False))
                    and self._gpu_frame_consumers and frame is not None):
                if self._publish_gui_frame(frame):
                    return True
            return False
        # multi
        self._phase_set("pub_frames")
        _hidden = set(getattr(self, "_hidden_sids", None) or ())
        for s in plan.get("screens") or []:
            sid = str(s.get("id") or "")
            if sid and sid in _hidden:
                continue
            frame = self._route_frame(plan, sid, str(s.get("type") or "independent"))
            if frame is not None:
                frames[sid] = frame
                if do_thumbs:
                    thumbs[sid] = _preview_thumb(frame, self._preview_thumb_side(frame))
                    try:
                        time.sleep(0)
                    except Exception:                      # noqa: BLE001
                        pass
        self._route_frames = frames
        self._phase_set("pub_disp")
        _disp = self._publish_display_frames(frames)
        self._phase_set("pub_gui")
        if do_thumbs:
            self._route_thumb_rev = int(getattr(self, "_route_thumb_rev", 0)) + 1
            self._rate_thumbs = int(getattr(self, "_rate_thumbs", 0)) + 1
            self._route_thumbs = thumbs
        sel = str(self._preview_route_id or "")
        if sel not in frames:
            main = output_model.main_screen(plan)
            sel = str((main or {}).get("id") or "")
        frame = frames.get(sel)
        # ★ 2026-11-25：若该屏本 tick 正好有输出窗口的私有副本，直接复用它给
        #   GUI 预览 —— 省掉 `_publish_gui_frame` 里再一次整帧 copy（1080p ~1.4ms）。
        _priv = bool(_disp) and sel in (_disp or {})
        if _priv:
            frame = _disp[sel]
        if (not bool(getattr(self, "_gui_covered", False))
                and self._gpu_frame_consumers and frame is not None):
            if self._publish_gui_frame(frame, private=_priv):
                return True
        return False

    def _publish_gui_frame(self, frame, private=False):
        """把一帧发布给 GUI（主预览 + 独立输出窗口），**默认每 tick 都发（60fps 上限）**。

        `private=True`：调用方保证 `frame` 是**本 tick 的私有副本**（不会被下一帧覆写），
        此时不再为了跨线程安全而再 `copy()` 一次。

        ★ 2026-11-11（用户：多屏播放音乐时界面/预览非常卡）：
          旧实现每帧 `frame.copy()` 后把 3000×900 原图交给 GUI，GUI 线程要在
          `preview_panel._rescale()`、输出窗口 `paintEvent`、输出页卡片里各做一次
          **SmoothTransformation 缩放**（实测 3000×900→1280 一次就 8.6ms），
          一个 frame_ready 能把 GUI 线程占 30~60ms ⇒ 操作卡、预览掉到 6~8fps。
          现在：
            · 引擎线程用 cv2.INTER_AREA 只缩一次到 ≤1600 宽（~3ms，且释放 GIL）；
            · GUI 各消费端拿到的是同一张小图，1:1 或小幅缩放，成本降到 <3ms；
            · GUI 发布限 15fps（输出/Spout 仍按原帧率走，不受影响）。
        """
        now = time.perf_counter()
        try:
            _fps_cap = float(os.environ.get("AUTOVJ_GUI_FPS",
                                            getattr(self, "_gui_fps", 60.0)) or 60.0)
        except (TypeError, ValueError):
            _fps_cap = 60.0
        # ★ 2026-11-25（真机：多屏 GUI 发布只有 18/s，而引擎是 31/s）：
        #   以前用严格的 1/30s 闸门。引擎刚好跑在 31~32fps 时，每帧间隔
        #   32.x ms 会**每隔一帧被闸掉一次**，实际只剩 18~20fps。这里留 15%
        #   公差：30fps 档在 28ms 以上的间隔都放行（60fps 输入仍是约 30/s）。
        _gap = (1.0 / max(1.0, _fps_cap)) * 0.85
        if now - float(getattr(self, "_gui_pub_t", 0.0) or 0.0) < _gap:
            return False
        self._gui_pub_t = now
        self._latest_frame = self._gui_scaled(frame, private=private)
        try:
            time.sleep(0)          # 预缩放后让出 GIL，GUI 可立即重绘
        except Exception:                                      # noqa: BLE001
            pass
        self._frame_seq += 1
        self._rate_gui = int(getattr(self, "_rate_gui", 0)) + 1
        self.frame_ready.emit(self._frame_seq)
        return True

    def _gui_scaled(self, frame, private=False):
        """GUI 预览图：按**输出画布分辨率 × preview.res** 缩一次。

        `private=True` 时，若本来就不需要缩放，直接返回同一张私有图（省一次整帧 copy）。
        """
        try:
            hard = int(getattr(self, "_gui_max_w", PREVIEW_GUI_MAX) or PREVIEW_GUI_MAX)
            hard = max(320, min(PREVIEW_GUI_MAX, hard))
            # ★ 2026-11-26：输出窗口现在吃 `display_frames` 的原生分辨率，
            #   不再和主预览共用 `_latest_frame` ⇒ 预览帧始终按 `preview.res`
            #   缩到最终尺寸（用户设 1/4 就是 480×270），GUI 不再每帧做
            #   1080p 的 Smooth 缩放。这是 GUI 线程最大的省时点。
            try:
                _res = float(getattr(self, "_preview_res", 1.0) or 1.0)
            except (TypeError, ValueError):
                _res = 1.0
            _res = max(0.1, min(1.0, _res))
            maxw = min(hard, max(160, int(round(frame.width() * _res))))
            if frame.width() <= maxw:
                return frame if private else frame.copy()
            tw = maxw
            th = max(2, int(round(frame.height() * tw / float(frame.width()))))
            # ★ 2026-11-11 实测：`cv2.resize(INTER_AREA)` 在 3000×900×4 上要
            #   **18~25ms/帧**（4 通道 AREA 没有 SIMD 优化），是 v18 `cp` 段从
            #   6ms 涨到 50ms 的元凶；QImage.scaled 的 Smooth 只要 **2.5ms**，
            #   而且质量更好。这里改回 QImage，避免在渲染线程上白烧 20ms。
            return frame.scaled(tw, th, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception:                                      # noqa: BLE001
            pass
        return frame.copy()

    def _send_multi_routes(self):
        """把各路由帧提交给 NDI/Spout（不阻塞 GUI）。"""
        if not self.running:
            return
        plan = self._out_cfg
        if not plan:
            return
        mode = str(plan.get("mode") or "multi")
        _render = bool(getattr(self, "_render_active", False))
        if _render:
            for _cli in list((getattr(self, "_render_clients", None) or {}).values()):
                try:
                    for _sid, _img in (_cli.poll_route_frames() or {}).items():
                        self._render_route_frames[str(_sid)] = _img
                except Exception:                              # noqa: BLE001
                    pass
        now = time.perf_counter()
        _snaps = {}
        _main_sid = ""
        if _render:
            try:
                _main_sid = str((output_model.main_screen(plan) or {}).get("id") or "")
            except Exception:                                  # noqa: BLE001
                _main_sid = ""
        for sid, kind, name, want_sp, want_nd in self._route_specs():
            if (not _render and want_sp
                    and str(sid) in (getattr(self, "_stage_spout_sids", None) or set())):
                want_sp = False          # in-process GPU 合成器已直接发 FBO
            if not (want_sp or want_nd):
                continue
            _rs = _ROUTE_SEG.setdefault(str(name or sid),
                                        {"spout": 0.0, "ndi": 0.0, "bits": 0.0, "n": 0})
            _rs["n"] += 1
            _tb = time.perf_counter()
            if _render:
                _fsid = _main_sid if (kind == "combined" and _main_sid) else str(sid)
                frame = self._render_route_frames.get(_fsid)
            else:
                frame = self._route_frame(plan, sid, kind)
            if frame is None or frame.isNull():
                continue
            # ★★ 2026-11-25：每条路由**只做一次**私有快照；同帧的多条路由
            #   （主屏 + 复制屏）共用同一张。Spout/NDI 两个通道也共用。
            #   旧实现：每条路由各自 convertToFormat + 每个通道各自拷贝，
            #   5 屏时 render 线程每帧白搬 ~40MB。
            key = id(frame)
            snap = _snaps.get(key)
            if snap is None:
                snap = self._make_output_snapshot(frame)
                _snaps[key] = snap
            _rs["bits"] += (time.perf_counter() - _tb) * 1000.0
            if snap is None or snap.isNull():
                continue
            frame = snap
            st = self._route_state.setdefault(sid, {})
            if want_sp:
                retry = float(st.get("spout_retry", 0.0) or 0.0)
                if now >= retry:
                    sp = (self._route_senders.get(sid) or {}).get("spout")
                    if sp is None:
                        # 发送端可能在失败后被关掉，重建一次
                        try:
                            import spout_out
                            sp = spout_out.SpoutOut(name)
                            self._route_senders[sid]["spout"] = sp
                            self._route_senders[sid]["spout_name"] = name
                        except Exception as e:                     # noqa: BLE001
                            self._route_fail(sid, "spout", now, e)
                            sp = None
                    if sp is not None:
                        # ★ BUG-A（2026-11-10）：发送端被关掉后必须重建，否则**永久哑火**
                        #   （SpoutOut.close() 只置 _dead，send() 曾经照样返回 True）。
                        _bad = None
                        try:
                            if hasattr(sp, "is_dead") and sp.is_dead():
                                _bad = "sender 已关闭（is_dead）"
                            else:
                                _e = sp.last_error() if hasattr(sp, "last_error") else None
                                if _e:
                                    _bad = _e
                        except Exception:                          # noqa: BLE001
                            pass
                        if _bad:
                            self._route_fail(sid, "spout", now, _bad)
                            sp = None
                    if sp is not None:
                        try:
                            _ts = time.perf_counter()
                            if hasattr(sp, "send_image"):
                                ok = sp.send_image(snap)
                            else:                              # 旧 sender（热更新兜底）
                                ok = sp.send(snap.bits(), snap.width(), snap.height(),
                                             snap.bytesPerLine())
                            self._rate_spout = int(getattr(self, "_rate_spout", 0)) + 1
                            _rs["spout"] += (time.perf_counter() - _ts) * 1000.0
                            _rs["dr"] = int(getattr(sp, "dropped", 0) or 0)
                            if not ok:
                                _e = sp.last_error() if hasattr(sp, "last_error") else None
                                raise RuntimeError(_e or "SpoutOut.send() 返回 False")
                        except Exception as e:                     # noqa: BLE001
                            self._route_fail(sid, "spout", now, e)
            if want_nd:
                retry = float(st.get("ndi_retry", 0.0) or 0.0)
                if now >= retry:
                    nd = (self._route_senders.get(sid) or {}).get("ndi")
                    if nd is None:
                        try:
                            from ndi_out import NDIOutput
                            nd = NDIOutput(name)
                            self._route_senders[sid]["ndi"] = nd
                            self._route_senders[sid]["ndi_name"] = name
                        except Exception as e:                     # noqa: BLE001
                            self._route_fail(sid, "ndi", now, e)
                            nd = None
                    if nd is not None:
                        # ★ BUG-A：NDI 也接上 last_error/is_dead（有就关掉重建）
                        try:
                            _bad = None
                            if hasattr(nd, "is_dead") and nd.is_dead():
                                _bad = "sender 已关闭（is_dead）"
                            else:
                                _e = nd.last_error() if hasattr(nd, "last_error") else None
                                if _e:
                                    _bad = _e
                            if _bad:
                                self._route_fail(sid, "ndi", now, _bad)
                                nd = None
                        except Exception:                          # noqa: BLE001
                            pass
                    if nd is not None:
                        try:
                            if not nd._ensure(snap.width(), snap.height()):
                                raise RuntimeError("NDI _ensure(%dx%d) 返回 False"
                                                   % (snap.width(), snap.height()))
                            _tn = time.perf_counter()
                            if hasattr(nd, "send_image"):
                                nd.send_image(snap)
                            else:                          # 旧 sender（热更新兜底）
                                bpl = snap.bytesPerLine()
                                bgra = np.frombuffer(snap.bits(), dtype=np.uint8,
                                                     count=bpl * snap.height())
                                nd.send_video(bgra, snap.width(), snap.height())
                            _rs["ndi"] += (time.perf_counter() - _tn) * 1000.0
                        except Exception as e:                     # noqa: BLE001
                            self._route_fail(sid, "ndi", now, e)
            try:
                time.sleep(0)          # 每条路由发完让出 GIL（Spout 拷贝可能几毫秒）
            except Exception:                                  # noqa: BLE001
                pass
        self._route_heartbeat(now)

    def _route_heartbeat(self, now):
        """每 10s 往 output_health.log 追加一行路由心跳（BUG-A 取证 / 规则 27）。

        格式（追加式，不破坏既有 regex）：
          `Spout 路由心跳：routes=2 → EasyRealityAutoVJ=sent:1200/err:- | dj=sent:900/err:-`
        字段：路由名、累计 sent、last_error 文本（无则 `-`）。
        """
        if now - float(getattr(self, "_route_hb_t", 0.0) or 0.0) < 10.0:
            return
        self._route_hb_t = now
        try:
            segs = []
            kinds = set()
            for sid, kind, name, want_sp, want_nd in self._route_specs():
                st = (self._route_senders.get(sid) or {})
                for chan, key in (("spout", "spout"), ("ndi", "ndi")):
                    obj = st.get(key)
                    if obj is None:
                        continue
                    kinds.add(chan.upper())
                    sent = 0
                    err = "-"
                    try:
                        sent = int((obj.stats() or {}).get("sent", 0) or 0)
                    except Exception:                              # noqa: BLE001
                        pass
                    try:
                        _e = obj.last_error() if hasattr(obj, "last_error") else None
                        if _e:
                            err = str(_e)[:60]
                    except Exception:                              # noqa: BLE001
                        pass
                    segs.append("%s[%s]=sent:%d/err:%s" % (name, chan, sent, err))
            if segs:
                _output_health_write("%s 路由心跳：routes=%d → %s"
                                     % ("/".join(sorted(kinds)) or "ROUTE",
                                        len(segs), " | ".join(segs)))
            # ★ 2026-11-17（用户：按屏 alpha 仍不透明）：把**实际要发出去的帧**
            #   的 alpha 统计写进同一份取证日志。判据很直接：
            #     · `a=0..255 trans=92.0%` ⇒ 帧里真的有透明像素，发送端没问题，
            #       接收端（OBS Spout2 源）要把 Composite mode 选 Premultiplied Alpha；
            #     · `a=255..255 trans=0.0%`  ⇒ 这块屏的图层把画面铺满了（全屏不透明
            #       素材/BGA），alpha 通道本身没有透明区，怎么设接收端都不会透。
            #   采样 1/16 行 × 1/16 列，1080p 约 8000 点，10s 一次，成本可忽略。
            _abits = []
            _plan = getattr(self, "_out_cfg", None)
            _any_a = any(bool(v) for v in (self._screen_alpha or {}).values())
            for sid, kind, name, _sp, _nd in (self._route_specs() if _any_a else ()):
                fr = self._route_frame(_plan, sid, kind)
                if fr is None or fr.isNull():
                    continue
                if not fr.hasAlphaChannel():
                    _abits.append("%s fmt=RGB32(实底)" % name)
                    continue
                try:
                    _a = np.frombuffer(fr.constBits(), dtype=np.uint8,
                                       count=fr.sizeInBytes())
                    _a = _a.reshape(fr.height(), fr.bytesPerLine())
                    _a = _a[:, :fr.width() * 4].reshape(fr.height(), fr.width(), 4)
                    _s = _a[::16, ::16, 3]
                    _abits.append("%s fmt=ARGB32 a=%d..%d trans=%.1f%%"
                                  % (name, int(_s.min()), int(_s.max()),
                                     float((_s < 250).mean()) * 100.0))
                except Exception:                                  # noqa: BLE001
                    _abits.append("%s fmt=ARGB32 a=?" % name)
            if _abits:
                _output_health_write("Alpha 取证：%s ｜ 接收端需选 Premultiplied Alpha"
                                     "（OBS: Spout2 源 → Composite mode）"
                                     % " | ".join(_abits))
        except Exception:                                          # noqa: BLE001
            pass

    def _route_fail(self, sid, chan, now, exc):
        st = self._route_state.setdefault(sid, {})
        key = "%s_fail" % chan
        n = int(st.get(key, 0) or 0) + 1
        st[key] = n
        st["%s_retry" % chan] = now + min(30.0, 1.0 * (2 ** min(5, n - 1)))
        if now - float(st.get("log_t", 0.0) or 0.0) >= 10.0:
            st["log_t"] = now
            _output_health_write("%s 路由发送失败（第 %d 次，将退避重试）：%s"
                                 % (chan.upper(), n, exc))
        if chan == "spout":
            self._close_route_spout(sid)
        else:
            self._close_route_ndi(sid)

    def _decode_limit(self, lay):
        """Decode at canvas size (1.0x).

        ★ 2026-11-08（用户：显示器输出码表演出仍掉帧）：真机 A/B 实测 1280×720 画布、
          7 图层 + 码表 + Alpha + Spout：
            · 1.5×（旧）：引擎 tick 平均 21ms / p95 38ms，显示器绘制 p95 110ms
            · 1.0×（现）：引擎 tick 平均 8ms / p95 13ms，显示器绘制 p95 39ms
          CPU 总占用基本持平（5.5 核对 5.5 核）：解码线程多做一次 INTER_AREA 缩小，
          换掉渲染线程里每层每帧的 Smooth 缩放，**延迟大幅下降**，而画质由解码器的
          INTER_AREA 保证（不是 QPainter 的快速缩放）。DXV GPU 路径更省：GPU 直接
          缩到目标尺寸，回读带宽也从 1920×1080 降到 1280×720。
          想改回去：环境变量 AUTO_VJ_DECODE_SCALE=1.5（排障用）。
        """
        scale = 1.0
        try:
            _s = float(os.environ.get("AUTO_VJ_DECODE_SCALE", "0") or 0)
            if _s > 0.2:
                scale = _s
        except (TypeError, ValueError):
            pass
        return (max(1, int(self.canvas.width() * scale)),
                max(1, int(self.canvas.height() * scale)))

    def _pin_kv_order(self, layers):
        """KV ????????? UI ????????logo ?? KV ????"""
        kv = [x for x in layers if getattr(x, "is_kv", False)]
        rest = [x for x in layers if not getattr(x, "is_kv", False)]
        pos = {id(x): i for i, x in enumerate(self.layers)}
        rest.sort(key=lambda x: pos.get(id(x), 10 ** 9))
        return kv + rest

    def _main_screen_layers(self):
        """主屏该画哪些层。

        ★ 2026-11-09（用户反馈 #1）：以前 `_compose()` 直接画 `self.layers`（**全部**层）
          ⇒ 挂在 B 屏的码表图层也会被画到主屏上（截图上主屏没有码表图层，画面却是码表内容）。
          现在按输出计划取主屏的 `effective_layers`；没有计划（老单屏配置）时退回全量。
        """
        plan = getattr(self, "_out_cfg", None)
        all_ids = [str(getattr(l, "lid", "")) for l in self.layers]
        if not plan:
            try:
                active = output_model.active_config(self.cfg)
                if active is not None:
                    ids = output_model.main_output_layers(active, all_ids)
                    return self._pin_kv_order(self._out_layers_by_ids(ids))
            except Exception:                                  # noqa: BLE001
                pass
            return self.layers
        try:
            ids = output_model.main_output_layers(plan, all_ids)
            return self._pin_kv_order(self._out_layers_by_ids(ids))
        except Exception:                                      # noqa: BLE001
            return self.layers

    def _compose(self, snap):
        """主屏合成（保留旧入口）：主画布 + **主屏自己的**图层，允许 GPU 合成。"""
        self._compose_to(self.canvas, self._main_screen_layers(), snap, allow_gpu=True)

    def _gpu_simple_layers(self, layers, snap, W, H):
        """收集可交给 GPU 纹理合成的简单图层；有转场/码表/特殊混合就返回 None。"""
        try:
            try:
                _iv = int((self._cp_cfg or self.cfg)["auto"].get("intensity", 1))
            except (TypeError, ValueError):
                _iv = 1
            inten = 0.0 if _iv < 0 else [0.6, 1.0, 1.4, 2.0][max(0, min(3, _iv))]
            pulse = 1.0 + float(snap.get("bass", 0.0) or 0.0) * 0.08 * inten
            silent = bool(getattr(self, "_silent_now_debounced", False))
            solo_on = any(getattr(l, "solo", False) for l in layers)
            out = []
            for lay in layers:
                if not getattr(lay, "visible", True):
                    continue
                if getattr(lay, "is_cuesheet", False):
                    if getattr(lay, "cs_active", False):
                        return None
                    continue
                if getattr(lay, "trans_active", False):
                    return None
                if str(getattr(lay, "blend", "normal") or "normal") not in (
                        "normal", "sourceover", "screen"):
                    return None
                if solo_on and not getattr(lay, "solo", False):
                    continue
                if getattr(lay, "hide_silent", False) and silent:
                    continue
                is_kv = bool(getattr(lay, "is_kv", False))
                if is_kv and float(getattr(lay, "kv_on", 0.0) or 0.0) <= 0.01:
                    continue
                p = getattr(lay, "player", None)
                if p is None:
                    continue
                img = p.current()
                if img is None or img.isNull():
                    img = getattr(lay, "_last_frame", None)
                if img is None or img.isNull():
                    return None
                if not getattr(lay, "use_alpha", True):
                    return None
                idx = lay.cur if 0 <= lay.cur < len(lay.clips) else -1
                item = lay.clips[idx] if idx >= 0 else None
                xf = None
                try:
                    if idx >= 0 and callable(getattr(lay, "clip_xform", None)):
                        xf = lay.clip_xform(idx)
                except Exception:                              # noqa: BLE001
                    xf = None
                if xf:
                    us = float(xf.get("scale", 1.0) or 1.0)
                    ux = float(xf.get("x", 0.0) or 0.0)
                    uy = float(xf.get("y", 0.0) or 0.0)
                    rot = float(xf.get("rot", 0.0) or 0.0)
                elif item is not None:
                    us = float(getattr(item, "img_scale", 1.0) or 1.0)
                    ux = float(getattr(item, "img_x", 0.0) or 0.0)
                    uy = float(getattr(item, "img_y", 0.0) or 0.0)
                    rot = float(getattr(item, "img_rot", 0.0) or 0.0)
                else:
                    us, ux, uy, rot = 1.0, 0.0, 0.0, 0.0
                custom = (abs(us - 1.0) > 1e-3 or abs(ux) > 1e-3
                          or abs(uy) > 1e-3 or abs(rot) > 1e-3)
                iw, ih = img.width(), img.height()
                if iw <= 0 or ih <= 0:
                    return None
                if custom:
                    s = min(W / iw, H / ih) * pulse * us
                else:
                    s = max(W / iw, H / ih) * pulse
                tw, th = iw * s, ih * s
                opacity = float(getattr(lay, "opacity", 1.0) or 1.0)
                if is_kv:
                    opacity *= max(0.0, min(1.0, float(getattr(lay, "kv_on", 1.0) or 1.0)))
                out.append({"img": img, "opacity": opacity,
                            "w": tw, "h": th,
                            "x": W * ux / 100.0, "y": H * uy / 100.0,
                            "rot": rot,
                            "blend": str(getattr(lay, "blend", "normal") or "normal")})
            return out or None
        except Exception:                                      # noqa: BLE001
            return None

    def _compose_gpu_simple(self, canvas, layers, snap):
        """新 GPU 纹理合成；成功写入 canvas 返回 True，失败/不支持返回 False。"""
        # 多屏 Alpha/叠加仍不稳定：关闭 GPU 合成，保留 GPU 解码
        # ★ 2026-11-25 真机复测：用 AUTOVJ_GPU_RENDER=1 打开后，多屏两路
        #   只有 8~19fps 且进程 0xC0000374 堆损坏退出 —— 保持关闭。
        return False
        if bool(getattr(self, "_gpu_render_dead", False)):
            return False
        if str(os.environ.get("QT_QPA_PLATFORM", "")).lower() == "offscreen":
            return False
        try:
            if not bool((self.cfg["perf"] or {}).get("gpu_decode", True)):
                return False
            from gpu_compose_layers import collect as _gpu_collect
            desc = _gpu_collect(self, layers, snap, canvas.width(), canvas.height())
            if desc is None:
                return False
            if self._gpu_renderer is None:
                from gpu_render import GpuRenderer
                from config import app_base_dir
                def _log(msg):
                    try:
                        with open(os.path.join(app_base_dir(), "gpu_render.log"),
                                  "a", encoding="utf-8") as f:
                            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
                    except Exception:                          # noqa: BLE001
                        pass
                self._gpu_renderer = GpuRenderer(_log)
            img = self._gpu_renderer.render(desc, canvas.width(), canvas.height())
            if img is None or img.isNull():
                return False
            try:
                canvas.fill(Qt.transparent if bool(getattr(self, "_any_alpha", False)) else Qt.black)
            except Exception:                                  # noqa: BLE001
                pass
            p = QPainter(canvas)
            try:
                p.setCompositionMode(
                    QPainter.CompositionMode_Source
                    if bool(getattr(self, "_any_alpha", False))
                    else QPainter.CompositionMode_SourceOver)
                p.drawImage(0, 0, img)
            finally:
                p.end()
            try:
                import time as _t
                _t.sleep(0)
            except Exception:
                pass
            self._gpu_render_last_ms = float(getattr(self._gpu_renderer, "_last_ms", 0.0) or 0.0)
            self._gpu_hit = int(getattr(self, "_gpu_hit", 0)) + 1
            if not bool(getattr(self, "_gpu_render_logged", False)):
                self._gpu_render_logged = True
                try:
                    from config import app_base_dir
                    with open(os.path.join(app_base_dir(), "gpu_render.log"),
                              "a", encoding="utf-8") as f:
                        f.write("[%s] GPU 纹理合成已启用 %dx%d（CPU 路径保留）\n"
                                % (time.strftime("%H:%M:%S"), canvas.width(), canvas.height()))
                except Exception:                              # noqa: BLE001
                    pass
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def _compose_to(self, canvas, layers, snap, allow_gpu=False):
        """把指定图层集合合成到指定画布。

        ★ 2026-11-08 多屏输出：独立屏幕用这个入口各自合成；主屏仍走 `_compose()`。
          `allow_gpu=False` 时强制 CPU——多块 QOpenGLPaintDevice 同帧并发会抢驱动，
          独立屏画布量一般不大，CPU 更稳。
        """
        W, H = canvas.width(), canvas.height()
        # ★ 2026-11-25：新 GPU 纹理合成（绑定 GPU 解码开关）；不支持则走下面 CPU。
        if allow_gpu and self._compose_gpu_simple(canvas, layers, snap):
            return
        # ---- optional GPU composition ------------------------------------
        # Only normal/add layers are safe on Qt's GL paint engine (see
        # _gpu_safe_for_scene).  Subtracts and the other blend modes stay CPU.
        gpu_p = None
        if (allow_gpu and not self._gpu_dead and self._gpu_comp is not None
                and not bool((self.cfg["perf"] or {}).get("gpu_decode", True))
                and not self._any_alpha
                and self._gpu_safe_for_scene()):
            gpu_p = self._gpu_begin(W, H)
        if gpu_p is not None:
            p = gpu_p
            p.fillRect(0, 0, W, H, Qt.black)
        else:
            self._clear_canvas(canvas)
            p = QPainter(canvas)
        self._gpu_active = bool(gpu_p)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        now = time.perf_counter()
        # 诊断：AUTOVJ_DRAW_PROBE=1 时按层记录超过阈值的绘制耗时（默认 30ms）。
        # `AUTOVJ_DRAW_PROBE_MS` 可把阈值降到 1~2ms，用来定位"6 层 16ms"是哪几层。
        _probe_ms = 30.0
        try:
            if os.environ.get("AUTOVJ_DRAW_PROBE") == "1":
                _probe_ms = max(0.0, float(
                    os.environ.get("AUTOVJ_DRAW_PROBE_MS", "30") or 30))
        except (TypeError, ValueError):
            _probe_ms = 30.0

        # 能量脉冲缩放（振幅强度：-1 关闭 / 0 低 / 1 中 / 2 高 / 3 极高）
        # 2026-09-24 用户要求「所有选项数值全部上调」：0.4/0.7/1.0/1.4 → 0.6/1.0/1.4/2.0
        # （缩放量 = bass × 0.08 × 该系数 → 满低频时 0% / 4.8% / 8% / 11.2% / 16%）
        # 索引做钳位：旧配置只有 0~2，新版本加了第 4 档也不会 IndexError
        try:
            # ★ BUG-D 波1：振幅强度按**当前屏**取（`_cp_cfg` 由 `_compose_independent` /
            #   主屏合成前设好；没设就是全局 ⇒ 主屏无 override 时与现状逐字等价）。
            _iv = int((self._cp_cfg or self.cfg)["auto"].get("intensity", 1))
        except (TypeError, ValueError):
            _iv = 1
        if _iv < 0:
            inten = 0.0                 # 关闭：pulse 恒为 1.0，完全不缩放画面
        else:
            inten = [0.6, 1.0, 1.4, 2.0][max(0, min(3, _iv))]
        pulse = 1.0 + snap["bass"] * 0.08 * inten
        drift_x = drift_y = 0.0

        # ★ 静音判据统一读防抖结果（见 `_tick_locked` 开头的 `_silent_hide_tick`）：
        #   与 `pause_silent` 活跃标记同帧同值，保证 hide 与 pause 的时机一致。
        silent = self._silent_now_debounced
        solo_on = any(getattr(l, "solo", False) for l in layers)
        subtract_jobs = []  # 需要逐像素"减少"混合的图层

        # 图层列表 index 0 = 最上层 → 从下往上绘制
        for _li, lay in enumerate(reversed(layers)):
            _tl = time.perf_counter()
            # ★★★ 2026-11-11（现场：预览/主线程 150~630ms 停顿）：`_draw_layer`
            #   里的 QPainter 在 PySide 下**整段持 GIL**。实测 10 层连续画，
            #   另一个 Python 线程最长被饿 **57ms**；每层后 `sleep(0)` 后降到 **3.8ms**。
            #   旧实现每 4 层才让一次 ⇒ GUI 仍可能被挡 4 层的时长。现在**每层都让**。
            if _li and not bool(getattr(self, "_gui_covered", False)):
                try:
                    time.sleep(0)
                except Exception:                              # noqa: BLE001
                    pass
            if not lay.visible:
                continue
            # ★★ 码表图层是**容器**：它没有单一 player，画面由内部子层按码表渲染。
            #   必须拦在下面的 `if not lay.player: continue` **之前**，并且**必须 continue** ——
            #   否则会落到 `lay.player.current()`（None ⇒ 抛异常/整帧丢）。
            #   solo / hide_silent 与普通图层同口径（旧实现曾让码表层"盖在最上不受 solo 管"）。
            if getattr(lay, "is_cuesheet", False):
                if solo_on and not getattr(lay, "solo", False):
                    continue
                if getattr(lay, "hide_silent", False) and silent:
                    continue
                _tc = time.perf_counter()
                # ★ 2026-11-18：按码表画布比例 contain 到输出画布（与原预览一致）。
                #   同比例时 ox=oy=0、w/h==W/H，直接画（零额外开销）。
                _cw, _ch, _ox, _oy = self._cue_sheet_view(W, H)
                if (_cw, _ch) == (W, H):
                    self._draw_cuesheet_layer(p, lay, W, H, snap, subtract_jobs)
                else:
                    _sub_jobs = []
                    p.save()
                    try:
                        p.translate(_ox, _oy)
                        p.setClipRect(0, 0, _cw, _ch)
                        self._draw_cuesheet_layer(p, lay, _cw, _ch, snap, _sub_jobs)
                    finally:
                        p.restore()
                    # 减少混合的暂存图是子画布尺寸 ⇒ 先搬进整幅透明图再参与相减
                    for _tmp, _op in _sub_jobs:
                        _full = QImage(W, H, QImage.Format_ARGB32)
                        _full.fill(Qt.transparent)
                        _qp = QPainter(_full)
                        try:
                            _qp.drawImage(_ox, _oy, _tmp)
                        finally:
                            _qp.end()
                        subtract_jobs.append((_full, _op))
                _dc = (time.perf_counter() - _tc) * 1000.0
                if _dc > _probe_ms and os.environ.get("AUTOVJ_DRAW_PROBE") == "1":
                    _nowc = time.perf_counter()
                    _lp = getattr(self, "_layer_probe_t", None)
                    if _lp is None:
                        _lp = {}
                        self._layer_probe_t = _lp
                    _key = ("cuesheet:" + str(getattr(lay, "name", "?")), W, H)
                    if _nowc - float(_lp.get(_key, 0.0) or 0.0) >= 1.0:
                        _lp[_key] = _nowc
                        try:
                            import stallwatch as _sw
                            _sw.log_line("[LAYER] cuesheet=%s %.1fms canvas=%dx%d"
                                         % (getattr(lay, "name", "?"), _dc, W, H))
                        except Exception:                  # noqa: BLE001
                            pass
                continue
            if not lay.player:
                continue
            if solo_on and not getattr(lay, "solo", False):
                continue
            # Kv 待机层：显示强度由 kv_on 控制（无声音=1 完全覆盖，有声音=0 完全切出）
            is_kv = getattr(lay, "is_kv", False)
            if is_kv and lay.kv_on <= 0.01:
                continue
            if lay.hide_silent and silent and not is_kv:
                continue  # 该图层设置了"无声音时隐藏"且当前没有声音
            cur_img = lay.player.current()
            img = cur_img
            if img is None or img.isNull():
                img = lay.prev_player.current() if lay.prev_player else None
            if img is None or img.isNull():
                # 兜底：新素材的首帧还没解出来（打开容器 24~250ms）时继续显示上一帧，
                # 避免硬切/快切下出现「一瞬间黑掉」。只存引用，不拷贝（解码线程每帧都会
                # 产出新的 QImage 对象，所以引用是安全的）。
                img = getattr(lay, "_last_frame", None)
            if img is None or img.isNull():
                continue
            # ★ 2026-11-08：淡入淡出等过渡在**新素材首帧就绪前先冻结计时**。
            #   旧行为：切换瞬间就按墙钟推进 prog，解码器开容器 70~690ms，
            #   等首帧出来时 prog 已到 1.0 ⇒ 直接硬切 —— 用户看到的"卡一下"。
            #   现在：首帧未就绪时把上一帧当当前帧画（prog=1），trans_start 每帧重置；
            #   首帧一到才从 0 开始真正的 fade/slide/zoom。
            hold_trans = False
            if (not is_kv and getattr(lay, "trans_active", False)
                    and (cur_img is None or cur_img.isNull())):
                hold_trans = True
                lay.trans_start = now
            lay._last_frame = img
            if is_kv:
                # Kv 图层：进度=显示强度；过渡方式只用它自己的切入/切出设置，不受其它设置影响
                prog = max(0.0, min(1.0, lay.kv_on))
                tr = lay.kv_trans_kind if lay.kv_trans_kind in ("fade", "cut", "glitch", "zoom") else "fade"
            else:
                prog = 1.0
                if lay.trans_active:
                    if hold_trans:
                        prog = 1.0       # 先完整显示上一帧，等首帧
                    else:
                        dur = (lay.trans_ms
                               or MODE_META[self._mode_for_layer(lay)]["trans_ms"]) / 1000.0
                        dur = max(0.016, dur)
                        prog = min(1.0, (now - lay.trans_start) / dur)
                        if prog >= 1.0:
                            lay.trans_active = False
                            lay.prev_player = None  # 播放器在缓存中复用，不关闭
                            lay._trans_prev_item = None

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
                # ⚠ 临时图**按图层缓存复用**：每帧 `QImage(W,H)` + `fill()` 实测 1.78 ms，
                #   每个"减少"图层每帧白烧一次（3 层 = 5.3 ms/帧）。
                tmp = getattr(lay, "_sub_tmp", None)
                if tmp is None or tmp.width() != W or tmp.height() != H:
                    tmp = QImage(W, H, QImage.Format_ARGB32)
                    lay._sub_tmp = tmp
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
            _dl = (time.perf_counter() - _tl) * 1000.0
            if _dl > _probe_ms and os.environ.get("AUTOVJ_DRAW_PROBE") == "1":
                _nowp = time.perf_counter()
                _lp = getattr(self, "_layer_probe_t", None)
                if _lp is None:
                    _lp = {}
                    self._layer_probe_t = _lp
                _key = (str(getattr(lay, "name", "?")), W, H)
                if _nowp - float(_lp.get(_key, 0.0) or 0.0) >= 1.0:
                    _lp[_key] = _nowp
                    try:
                        import stallwatch as _sw
                        _sw.log_line("[LAYER] %s %.1fms img=%dx%d blend=%s canvas=%dx%d"
                                     % (getattr(lay, "name", "?"), _dl,
                                        img.width(), img.height(), blend, W, H))
                    except Exception:                      # noqa: BLE001
                        pass
            try:
                if not bool(getattr(self, "_gui_covered", False)):
                    time.sleep(0)      # 每层画完让出 GIL（见循环开头说明）
            except Exception:                                  # noqa: BLE001
                pass

        # 黑场
        if self.blackout:
            p.fillRect(0, 0, W, H, Qt.black)

        p.end()

        if gpu_p is not None:
            img = self._gpu_finish(gpu_p)
            if img is not None:
                self.canvas = img
                canvas = img
            else:
                # GPU failed mid-frame: rebuild a CPU canvas for the next tick so
                # the fallback does not inherit a stale frame.
                self.canvas = QImage(W, H, self._canvas_format())
                self._clear_canvas(self.canvas)
                canvas = self.canvas
            self._gpu_active = False

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
        """「减少」混合：画布 RGB 减去图层 RGB（饱和到 0，alpha 不动）。

        ⚠⚠ 性能（2026-09-27 实测，1920x1080，`tools/_subtract_bench.py`）：

            旧写法（astype(int16) 全 4 通道 + float 相乘 + np.clip）
                1 个"减少"图层   88.3 ms/帧      3 层  263.8 ms/帧
            新写法（整幅 4 通道原地 cv2 饱和减 + 缓存 LUT/scratch）
                1 个"减少"图层    2.3 ms/帧      3 层    6.8 ms/帧   → **38×**

        60fps 的单帧预算只有 16.7 ms ⇒ 旧写法一开"减少"就掉到约 4 fps（用户反馈"非常卡"）。
        逐像素与旧实现**完全一致**（最大差 0，见该脚本的对照断言）。

        三个关键点，改之前先读懂：
        1. **cv2 的 dst 必须连续内存**。`canvas[:, :, :3]` 是 stride=4 的**不连续视图**，
           当 dst 会被 cv2 直接拒绝；用 numpy 拷回又因为跨步写入慢到 25 ms/帧（实测）。
           ⇒ 所以改用**整幅 (h,w,4)**：QImage 的 bytesPerLine == w*4 时它是连续的，可以直接当 dst。
        2. **把 operand 的 alpha 置 0** ⇒ 相减后画布 alpha 不变（否则会把透明区域算坏）。
           置 0 后再过 LUT 仍然是 0，顺序安全。
        3. **LUT 与 scratch 一律缓存**，绝不在每帧分配 —— 旧的 float 中间量在 1080p 下
           每层每帧要产生约 50 MB float64 + 16 MB int16，纯粹是内存带宽与 GC 的浪费。
        """
        h, w = canvas.height(), canvas.width()
        try:
            cvv = self._np_view(canvas)          # (h, w, 4) BGRA；连续（bpl == w*4）
        except Exception:
            return
        if cvv.shape[0] != h or cvv.shape[1] != w or not cvv.flags["C_CONTIGUOUS"]:
            return
        scr = _sub_scratch(h, w)
        for tmp, opacity in jobs:
            try:
                np.copyto(scr, self._np_view(tmp))
                scr[:, :, 3] = 0                  # 保护画布 alpha
                if opacity < 0.999:
                    cv2.LUT(scr, _sub_lut(opacity), dst=scr)
                cv2.subtract(cvv, scr, dst=cvv)   # uint8 饱和减（不会回绕）
            except Exception:
                pass  # 单个图层失败就跳过它，不影响整帧

    def _draw_layer(self, p, lay, img, W, H, prog, tr, pulse, snap,
                    drift_x=0.0, drift_y=0.0, crop=None, out_dir=False):
        """把一层画到画布上。

        `crop`：码表（cue）用的**源矩形裁切**，归一化 `[x, y, w, h]`（0~1）；`None` = 不裁。
        ★ 向后兼容铁律：`crop is None` ⇒ 走**与改动前一字不差**的 `drawImage(_tgt, image)`
          ⇒ 普通图层（以及所有不传 crop 的调用点）**逐像素零变化**。
          `crop` 非 None 时用「源矩形重载」绘制（**绝不 `image.copy()`**），
          并把 cover/contain 的基准换成**裁切后的尺寸**（对齐 Cue 端 `compositor._target_src`：
          先裁、再以裁后尺寸算适配，否则裁切区会被放大变形）。
        """
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
        prev = (lay.prev_player.current()
                if (getattr(lay, "trans_active", False) and lay.prev_player) else None)
        if getattr(self, "_gpu_active", False):
            img = self._gpu_prepare_image(img)
            prev = self._gpu_prepare_image(prev)
        now = time.perf_counter()

        # 每个**图层片段实例**独立的大小/位置（存在 Layer.clip_xforms，不再共享 MediaItem）
        # ⚠ `_draw_layer` 也会被码表子层 `_CueSubLayer` 调用；它没有 clip_xform，
        #   必须回退到 item 自身属性，绝不能因此抛异常把整个引擎 tick 打断。
        def _xf_of(l, idx):
            if l is None or idx is None or int(idx) < 0:
                return None
            fn = getattr(l, "clip_xform", None)
            if not callable(fn):
                return None
            try:
                return fn(int(idx))
            except Exception:                                  # noqa: BLE001
                return None

        cur_idx = lay.cur if 0 <= lay.cur < len(lay.clips) else -1
        cur_item = lay.clips[cur_idx] if cur_idx >= 0 else None
        cur_xf = _xf_of(lay, cur_idx)
        prev_idx = -1
        if getattr(lay, "trans_active", False) and 0 <= lay.prev_idx < len(lay.clips):
            prev_idx = lay.prev_idx
        prev_item = lay.clips[prev_idx] if prev_idx >= 0 else None
        if prev_item is None:
            prev_item = getattr(lay, "_trans_prev_item", None)
            if prev_item is not None:
                try:
                    prev_idx = lay.clips.index(prev_item)
                except ValueError:
                    prev_idx = -1
        prev_xf = _xf_of(lay, prev_idx)

        def _t(item, xf=None):
            if item is None:
                return False, 0.0, 0.0, 1.0, 0.0
            if xf:
                us = float(xf.get("scale", 1.0) or 1.0)
                ux = float(xf.get("x", 0.0) or 0.0)
                uy = float(xf.get("y", 0.0) or 0.0)
                rot = float(xf.get("rot", 0.0) or 0.0)
            else:
                us = float(getattr(item, "img_scale", 1.0) or 1.0)
                ux = float(getattr(item, "img_x", 0.0) or 0.0)
                uy = float(getattr(item, "img_y", 0.0) or 0.0)
                rot = float(getattr(item, "img_rot", 0.0) or 0.0)
            custom = (abs(us - 1.0) > 1e-3 or abs(ux) > 1e-3
                      or abs(uy) > 1e-3 or abs(rot) > 1e-3)
            return custom, W * ux / 100.0, H * uy / 100.0, us, rot

        def paint_cover(painter, image, item, xf=None, scale=1.0, dx=0.0, dy=0.0, a=1.0):
            if image is None or image.isNull():
                return
            custom, ux, uy, us, rot = _t(item, xf)
            iw, ih = image.width(), image.height()
            # ★ 裁切（码表 cue 的 crop = [x, y, w, h] 归一化；普通图层 crop=None）
            #   用 drawImage 的「源矩形」重载，**不做每帧 copy**；
            #   裁切后以**裁后尺寸**为 cover/contain 基准（否则裁切区会被放大变形）。
            src = _crop_src_rect(image, crop)
            if src is not None:
                iw, ih = src.width(), src.height()
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
                _tgt = QRectF(-tw / 2.0, -th / 2.0, tw, th)
                if src is None:
                    painter.drawImage(_tgt, image)
                else:
                    painter.drawImage(_tgt, image, src)
            else:
                _tgt = QRectF((W - tw) / 2 + dx + ux, (H - th) / 2 + dy + uy, tw, th)
                # 1:1 快路径：解码尺寸已按画布裁过（见 _decode_limit），
                # 此时 target 恰好等于整幅画布，省掉 QPainter 的缩放/矩形路径。
                if (src is None and abs(_tgt.x()) < 0.01 and abs(_tgt.y()) < 0.01
                        and abs(_tgt.width() - iw) < 0.01
                        and abs(_tgt.height() - ih) < 0.01
                        and iw == W and ih == H):
                    painter.drawImage(0, 0, image)
                elif src is None:
                    painter.drawImage(_tgt, image)
                else:
                    painter.drawImage(_tgt, image, src)
            painter.restore()

        def _own_w(image, item, xf=None, scale=1.0):
            """该素材**实际画出来**的宽度（px）—— 与 `paint_cover` 同一套公式。

            ★★ 2026-11-02（用户：「缩小图滑动时从屏幕最右边进来」）：
              slide 的位移量原来直接用**画布宽 W**。素材被 xform 缩小到 0.28~0.4 之后，
              它实际只有 300~430 px 宽，却要横穿 1920 px ⇒ 看着就是"从屏幕外飞进来"。
              ⇒ 位移量一律按**素材自身宽度**算，小图只在自己那块地方进出。
              ⚠ 旋转不改变宽度，这里不需要分支。
            """
            if image is None or image.isNull() or image.width() <= 0:
                return 0.0
            custom_w, _ux, _uy, us, _rot = _t(item, xf)
            iw, ih = image.width(), image.height()
            src = _crop_src_rect(image, crop)
            if src is not None:
                iw, ih = src.width(), src.height()
            if iw <= 0 or ih <= 0:
                return 0.0
            if custom_w:
                s = min(W / iw, H / ih) * scale * us   # 完整显示 + 用户缩放
            else:
                s = max(W / iw, H / ih) * scale        # 铺满
            return iw * s

        dx = drift_x * (0.5 + (lay.cur % 3) * 0.25)  # 各图层漂移相位不同
        dy = drift_y * (0.5 + (lay.cur % 2) * 0.5)
        if getattr(lay, "lock_motion", False):
            dx = dy = 0.0

        if tr == "beat_cut" or prog >= 1.0 or (not is_kv and not lay.trans_active):
            paint_cover(p, img, cur_item, cur_xf, pulse, dx, dy, alpha)
            return

        if tr == "fade":
            if prev is not None:
                paint_cover(p, prev, prev_item, prev_xf, pulse, dx, dy, alpha)
            paint_cover(p, img, cur_item, cur_xf, pulse, dx, dy, alpha * prog)
        elif tr == "slide":
            # ★★ 2026-11-02（用户：「滑动消失不是从原位置滑，是从屏幕最右边滑」）：
            #   ① 位移量从 `W`（整屏）改成 **d = 素材自己的宽度**；
            #   ② 方向按 `out_dir` 反向 —— 入场 `+d → 0`（从自己右边一格滑到位），
            #      出场 `0 → −d`（继续向左滑出）。原来两者共用 `W*(1-prog)`，
            #      而出场时 prog 是 1→0，于是出场也演成"从右边滑进来"。
            d = _own_w(img, cur_item, cur_xf, pulse)
            sgn = -1.0 if out_dir else 1.0
            if prev is not None:
                paint_cover(p, prev, prev_item, prev_xf, pulse, -sgn * d * prog, dy, alpha)
            paint_cover(p, img, cur_item, cur_xf, pulse, sgn * d * (1.0 - prog), dy, alpha)
        elif tr == "zoom":
            if prev is not None:
                paint_cover(p, prev, prev_item, prev_xf, pulse * (1 + 0.4 * prog), dx, dy, alpha * (1 - prog))
            paint_cover(p, img, cur_item, cur_xf, pulse * (0.7 + 0.3 * prog), dx, dy, alpha)
        elif tr == "glitch":
            import random as _r
            rng = _r.Random(int(now * 1000))
            if prev is not None:
                paint_cover(p, prev, prev_item, prev_xf, pulse, 0, 0, alpha)
            # 随机行错位（用源矩形绘制，避免每行 copy）
            # ★ crop：在**裁切窗口内**按比例切；`src` 为 None 时与原式逐像素一致
            # ★★ 2026-11-02（用户：「缩小图消失时先把它拉成全屏，然后再淡出消失」）：
            #   本分支原来写死 `QRectF(off, y, W, step)` —— 把素材**铺满整个画布**，
            #   完全无视 `xform(scale/x/y/rot)`。被缩到 0.36 的图在 glitch 过渡里
            #   就变成 1080p 满屏，再随 alpha 消失（用户看到的就是这个）。
            #   ⇒ 改成按**该素材自己的目标矩形**切片；默认 xform（cover）时
            #     tx=0 / tw=W / th=H，输出与改动前逐像素一致。
            src = _crop_src_rect(img, crop)
            if src is None:
                sx0, sy0, sw, sh_img = 0.0, 0.0, float(img.width()), float(img.height())
            else:
                sx0, sy0, sw, sh_img = (src.x(), src.y(),
                                        src.width(), src.height())
            custom_g, ux_g, uy_g, us_g, rot_g = _t(cur_item, cur_xf)
            if sw <= 0 or sh_img <= 0 or abs(rot_g) > 1e-3:
                # 旋转状态下做行错位会把图切成斜碎片 ⇒ 退回正常绘制（宁可没特效）
                paint_cover(p, img, cur_item, cur_xf, pulse, dx, dy, alpha)
            else:
                if custom_g:
                    _s = min(W / sw, H / sh_img) * pulse * us_g
                else:
                    _s = max(W / sw, H / sh_img) * pulse
                _tw, _th = sw * _s, sh_img * _s
                _tx = (W - _tw) / 2.0 + ux_g
                _ty = (H - _th) / 2.0 + uy_g
                _th_i = max(1, int(round(_th)))
                step = max(8, _th_i // 24)
                p.save()
                p.setOpacity(alpha)
                yy = 0
                while yy < _th_i:
                    off = rng.randint(-40, 40) if rng.random() < 0.35 else 0
                    sy = sy0 + min(sh_img - 1, int(yy / float(_th_i) * sh_img))
                    sh = max(1, int(step / float(_th_i) * sh_img))
                    p.drawImage(QRectF(_tx + off, _ty + yy, _tw,
                                       min(step, _th_i - yy)),
                                img, QRectF(sx0, sy, sw, sh))
                    yy += step
                p.restore()
        else:
            paint_cover(p, img, cur_item, cur_xf, pulse, dx, dy, alpha)

    def _set_players_active(self, active):
        """冻结/黑场时把已存在的播放器全部挂起，避免解码线程继续满速吃 CPU。

        ★ 2026-11-12（用户：什么都不放还有 38% CPU）：旧 `freeze` 只是让
          `_tick_locked` 提前 return，播放器的 `set_active(False)` 在更后面，
          根本执行不到 ⇒ 画面冻住但解码还在跑。这里显式挂起。
        """
        seen = set()
        for lay in self.layers:
            ps = []
            try:
                ps.extend(list((getattr(lay, "cache", None) or {}).values()))
            except Exception:                                  # noqa: BLE001
                pass
            ps.extend([getattr(lay, "player", None), getattr(lay, "prev_player", None)])
            try:
                ps.extend(list((getattr(lay, "_cue_players", None) or {}).values()))
            except Exception:                                  # noqa: BLE001
                pass
            for p in ps:
                if p is None or id(p) in seen:
                    continue
                seen.add(id(p))
                try:
                    p.set_active(bool(active))
                except Exception:                              # noqa: BLE001
                    pass

    # ---------------- 手动干预 ----------------
    @_locked
    def set_blackout(self, on):
        self.blackout = on
        if on:
            self._set_players_active(False)

    @_locked
    def set_freeze(self, on):
        self.freeze = on
        if on:
            self._set_players_active(False)

    @_locked
    def set_pause_auto(self, on):
        self.pause_auto = on

    @_locked
    def toggle_run(self):
        self.running = not self.running
        if self.running:
            # 静音待机从“开始”重新计时：给 8s 正常预览，别一开就 2fps
            self._silent_low_since = time.perf_counter()
        pos = self._beat_pos(self.audio.snapshot())
        self.switch_pos = pos
        self._next_due = None
        return self.running

    def resume_after_vj_start(self):
        """停止再开始后复位待机/帧率状态；音频采集由 UI 侧重新拉起。"""
        try:
            self._silent_standby = False
            self._silent_standby_prev = False
            self._silent_since = 0.0
            self._silent_low_since = time.perf_counter()
            self._effective_fps = None
            self._fps_next_t = 0.0
            self._last_tick_t = 0.0
            self._tick_gap_ms = 0.0
            self._gap_ema_ms = 0.0
            self._display_scenes = {}
            self._scene_cpu_i = 0
        except Exception:                                      # noqa: BLE001
            pass
        try:
            QMetaObject.invokeMethod(self, "_apply_render_fps", Qt.QueuedConnection)
        except Exception:                                      # noqa: BLE001
            pass

    @_locked
    def close(self):
        # ★ 必须在关图层/停 timer **之前**卸表：否则码表解码器线程/句柄会留到进程退出
        #   （9-30「12 核 / 750MB/s」的形态之一就是"没人关的 cue 解码器"）。
        try:
            self.clear_cue_sheet(silent=True)
        except Exception:                                         # noqa: BLE001
            pass
        self.timer.stop()
        try:
            self._close_render_client("engine close")
        except Exception:                                      # noqa: BLE001
            pass
        # ★ 多屏输出：先关各路由发送端（NDI/Spout 发送线程）
        try:
            self._close_all_route_senders()
        except Exception:                                         # noqa: BLE001
            pass
        for lay in self.layers:
            lay.close_all()
        # 关闭每层健康快照日志句柄（见 `_LayerHealthLog`；进程退出时收尾）
        if self._health_log is not None:
            try:
                self._health_log.close()
            except Exception:
                pass
            self._health_log = None
        if self._spout is not None:
            try:
                self._spout.close()      # 见 spout_out.SpoutOut：sender 归发送线程所有
            except Exception:
                pass
            self._spout = None
        if self._ndi is not None:
            try:
                self._ndi.close()
            except Exception:
                pass
            self._ndi = None
