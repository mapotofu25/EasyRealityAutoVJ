# -*- coding: utf-8 -*-
"""离线 BPM 锚点：把最近 N 秒音频交给离线两级细扫，给素材倍速一个可信的参照。

★★★ 为什么必须有这个（2026-11-02 现场取证，OBS 录像 22:20–23:10）：

  那段是 Funkot，实际 **190 BPM**，实时的自相关估计器在现场粒度下锁到
  **125.5**（偶尔 95.5），素材倍速因此只有 0.75~0.98×（应为 1.48×），
  用户现场感受就是「素材速度没跟上来」。

  根因不是参数没调好，是**信号本身不够**：
    · 现场 `block=1323`（ToDesk 虚拟声卡 441×3）⇒ 33.3 帧/秒；
    · 190 BPM 的周期 = 10.53 帧，落在整数格 10/11 之间，`acn` 被摊薄到 0.158/0.194；
    · 而 4/4 的 kick 只在 1、3 拍响 ⇒ 自相关最强的周期是 **2 拍**（95 BPM），
      实测 kick 分支 score=0.967、全频 2.149，**两条都指向 lag=21（95.2）**。
  ⇒ 任何「谐波/相对强度」判据都救不回来（125.5 的峰 0.428 就是比 190 的 0.2 强）。

  而离线的 `beatgrid.analyze_bpm_only`（全频 onset + 两级细扫 + 网格集中度）
  在同一段音频上稳定给 **190.1**（连续 300 次一次没跳）。
  ⇒ 让它当**锚点**，只用来驱动「素材播放倍速」，**不碰拍钟**。

★ 设计约束（用户 2026-11-02 追问性能/泄漏后定的）：
  · **按需触发**，不是定时轮询：稳态（实时值跟得上）时一次都不跑；
  · 单发：上一轮没跑完就跳过，绝不排队（避免 CPU 紧张时堆积）；
  · 任何异常只记账，**绝不退出线程**（一次失败 ≠ 永久失效）；
  · 采集线程只做一次 `mean` + 环形缓冲写（~几微秒），分析全在本线程；
  · 实测代价：30 秒窗单次 ~76ms、每 20 秒一次 = 0.38% 单核；
    300 次循环 RSS +2MB（无泄漏）；CPU 89% 时耗时涨到 ~100ms、音频线程抖动 4.2ms。
"""

import threading
import time

import numpy as np


class BpmAnchor(threading.Thread):
    """后台线程：按需把最近 `win_sec` 秒音频跑一次离线 BPM，结果写 `state.bpm_anchor`。"""

    def __init__(self, state, sr=44100, win_sec=30.0, tick=0.5,
                 min_gap=6.0, max_gap=45.0, base_dir=None, name="BpmAnchor"):
        super().__init__(name=name, daemon=True)
        self.state = state
        self.sr = int(sr)
        self.win_sec = float(win_sec)
        self.tick = float(tick)
        self.min_gap = float(min_gap)     # 两次分析的最小间隔（防抖）
        # ★ 2026-11-02 真机踩到：第一次触发时缓冲区只攒了 24 秒（不到 30），
        #   而 `analyze_bpm_only` 在 20~24 秒窗口上给 **152**、30 秒给 **190**
        #   —— 窗口不够会把结果算成另一个值，而且之后"实时==锚点"就不再触发，错值会一直挂着。
        #   ⇒ ① 攒够 95% 才分析；② 兜底间隔从 120s 降到 45s（45s×76ms = 0.17% CPU）。
        self.max_gap = float(max_gap)     # 兜底：再稳也每隔这么久校准一次
        self.base_dir = base_dir

        self._cap = max(1, int(self.sr * (self.win_sec + 2.0)))
        self._buf = np.zeros(self._cap, dtype=np.float32)
        self._pos = 0
        self._filled = 0
        self._lk = threading.Lock()       # 只护「写」与「取快照」，持锁时间是微秒级

        self._stop = threading.Event()
        self._last_t = 0.0                # 上次成功分析的墙钟
        self._last_req = 0.0
        self.runs = 0                     # 成功次数（诊断）
        self.last_error = ""              # 最近一次异常（诊断）
        self.last_bpm = 0.0

    # ------------------------------------------------------------------ 采集侧
    def feed(self, chunk):
        """采集线程调用：单声道化后写入环形缓冲。**只做拷贝，不做任何计算**。"""
        try:
            a = np.asarray(chunk, dtype=np.float32)
            if a.ndim > 1:
                a = a.mean(axis=1)        # ★ 左右平均：与建库/指纹同一个口径
            a = a.ravel()
            n = len(a)
            if n <= 0:
                return
            with self._lk:
                if n >= self._cap:
                    self._buf[:] = a[-self._cap:]
                    self._pos = 0
                    self._filled = self._cap
                    return
                p = self._pos
                end = p + n
                if end <= self._cap:
                    self._buf[p:end] = a
                else:
                    k = self._cap - p
                    self._buf[p:] = a[:k]
                    self._buf[:n - k] = a[k:]
                self._pos = end % self._cap
                self._filled = min(self._cap, self._filled + n)
        except Exception as e:                                    # noqa: BLE001
            self.last_error = "feed: %s" % e

    def _snapshot(self, n):
        with self._lk:
            n = int(min(n, self._filled))
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            start = (self._pos - n) % self._cap
            if start + n <= self._cap:
                return self._buf[start:start + n].copy()          # ★ 必须拷贝（跨线程）
            k = self._cap - start
            out = np.empty(n, dtype=np.float32)
            out[:k] = self._buf[start:]
            out[k:] = self._buf[:n - k]
            return out

    # ------------------------------------------------------------------ 触发
    def request(self, now=None):
        """外部请求一次分析（换歌 / 实时值明显不对时由 `_Analyzer` 打标记）。"""
        try:
            self.state.anchor_req = float(now if now is not None else time.time())
        except Exception:                                         # noqa: BLE001
            pass

    def _due(self, now_wall):
        st = self.state
        req = float(getattr(st, "anchor_req", 0.0) or 0.0)
        # ★ 必须接近满窗（95%）：短窗会算出另一个 BPM（实测 20s→152 / 30s→190）
        ready = self._filled >= int(self.win_sec * self.sr * 0.95)
        if not ready:
            return False, ""
        if req > self._last_req:
            if (now_wall - self._last_t) < self.min_gap:
                return False, ""           # 防抖：刚跑过就等一等
            self._last_req = req
            return True, "req"
        if self._last_t <= 0:
            return True, "first"
        if (now_wall - self._last_t) >= self.max_gap:
            return True, "max_gap"
        return False, ""

    # ------------------------------------------------------------------ 分析
    def analyze_once(self):
        """跑一次（同步，供测试直接调用）。返回 bpm 或 0。"""
        import beatgrid as BG

        snap = self._snapshot(int(self.win_sec * self.sr))
        # ★ 同上：窗口不足 90% 一律不算（宁可不给，也不给一个窗口不够的错值）
        if len(snap) < int(self.win_sec * self.sr * 0.9):
            self.last_error = "window_short: %.1fs" % (len(snap) / float(self.sr))
            return 0.0
        m22, _sr = BG.resample(snap, self.sr, BG.SR_TARGET)       # ⚠ 返回元组
        bpm = float(BG.analyze_bpm_only(m22, BG.SR_TARGET) or 0.0)
        now = time.time()
        if not (55.0 <= bpm <= 225.0):
            self.last_error = "out_of_range: %.1f" % bpm
            return 0.0
        try:
            live = float(getattr(self.state, "bpm", 0.0) or 0.0)
        except (TypeError, ValueError):
            live = 0.0
        self.last_bpm = bpm
        self.runs += 1
        self._last_t = now
        try:
            self.state.bpm_anchor = bpm
            self.state.bpm_anchor_t = now
        except Exception as e:                                    # noqa: BLE001
            self.last_error = "publish: %s" % e
        # 取证一行（现场可 grep；失败绝不影响主流程）
        try:
            from energy_log import log_capture_event
            log_capture_event(
                "[BPM] 锚点 %.1f（实时 %.1f，比值 %.2f）第%d次"
                % (bpm, live, (live / bpm) if bpm > 0 else 0.0, self.runs),
                base_dir=self.base_dir)
        except Exception:                                         # noqa: BLE001
            pass
        return bpm

    # ------------------------------------------------------------------ 线程
    def stop(self):
        self._stop.set()

    def run(self):
        # 预热：第一次调用要建 FFT 计划 / 导入路径，别让现场第一次真用付这个钱
        try:
            import beatgrid as BG
            BG.analyze_bpm_only(np.zeros(int(3 * BG.SR_TARGET), dtype=np.float32),
                                BG.SR_TARGET)
        except Exception:                                         # noqa: BLE001
            pass
        while not self._stop.wait(self.tick):
            try:
                good, why = self._due(time.time())
                if not good:
                    continue
                self.analyze_once()
            except Exception as e:                                # noqa: BLE001
                # ★ 绝不能退出：一次失败只是这一轮没更新（铁律 0）
                self.last_error = "%s: %s" % (type(e).__name__, e)
