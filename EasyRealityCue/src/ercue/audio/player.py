# -*- coding: utf-8 -*-
"""★ 音频走带（预览试听）：整曲解码到内存 + 声卡按帧号推进。

本模块是 M4 的核心，落实架构方案 §4-A5 / §4-A6 的每一条 —— 每条都对应旧实现
（`P:\\AutoVJ_码表备份_20260930\\src\\cue_preview.py` 的 `CueAudio`）真实烧过的代价：

  A5a **先把整首歌解码进内存**（48 kHz 立体声 float32）。播放循环**只做**
      「把下一块交给声卡 → 索引前进」，**绝不用"边解边播的有界队列"**。
      旧写法「解码线程 → 有界队列 → 声卡」队列满丢块、空插静音 ⇒ 音乐比挂钟慢
      （听感被拉长、和画面越走越偏）。
  A5b **位置 = 已交给声卡的帧号 / 采样率**（`_i` 只在 `sink.write()` 真正返回后前进）。
      **绝不用挂钟推算** —— 否则音乐停了画面还在走。
  A5c 块大小 **≥ 0.25 秒**（`BLOCK_SEC`）。旧实现用 1024 帧（≈21 ms），单次开销一旦
      超过 21 ms 就供不上，声卡欠载 ⇒ 音乐变慢断续。
  A6  sounddevice 的调用**只在专用线程**（绝不在 GUI 线程）；`stop()` `join` 超时
      **≤0.8 s**（`STOP_TIMEOUT_SEC`），**绝不卡界面**；欠载时由 sounddevice 内部
      **短等**（我们从不丢块）。

另外：播放前把 `PREROLL_SEC` 那一段先喂进去（对应旧实现的 `PREROLL_SEC=0.5`），
起播不空转、不爆音。

═══ 找不到声卡 / 设备打开失败时的**优雅降级 + 可恢复** ═══
  · 构造时探测本机有无输出设备 → 无则 `available=False`；
  · `play()` 在**未装载 / 无设备 / 打开设备失败**时**返回 False**（绝不抛到 UI）；
  · ★★ C1 修复：**打开设备失败不再让整个会话永久无声** ——
      ① 每次显式 `play()` 都**复位**"上次打开失败"标记并**重新尝试**打开设备
         （旧实现 `_open_failed` 一旦置位永不复位 ⇒ 一次失败即整会话静音、只能重启软件）；
      ② 连续失败按 **1→2→4s 退避**（`_next_open_ts`；冷却期内不再尝试，绝不每帧重试），
         打开成功即清零；**首败后的第一次重试仍放行** —— 让"条件已恢复"
         （设备已插回 / 被占用已解除）的用户一次 ▶ 就能立刻恢复；
      ③ 打开仍在**专用线程**里发生（A6），`play()` 只在 `OPEN_WAIT_SEC` 内**有界等待**
         其结果：失败则**同步返回 False 并写 `error`**，成功则立即返回 True；
      ④ 运行中（已起播后）出错 → 置 `available=False` 并进入退避，`is_playing()` 变
         False，窗口据此退回挂钟驱动 —— **不报错、不崩**。
  · `available` 不再"永久 False"：退避冷却期一过即自动恢复可重试（供 UI 重新调用 `play()`）。
  ⇒ 编辑器在没有声卡的机器上照样能用；也能从"设备临时被占用 / 热插拔"中自行恢复。

═══ 解码在后台线程（P2）═══
  `load()` **不阻塞调用方**：它发起一个专用解码线程（`ercue-audio-dec`），立刻返回。
  解码进度用 `loading` 查询，`loaded` 表示就绪，`wait_loaded(timeout)` 可阻塞到完成（测试用）。
  ⚠ 解码线程**只做解码，绝不碰 sounddevice**（与播放线程分开，符合 A6）；
    在途解码被新加载 / `close()` 取代时，靠 `_load_token` 作废结果，不污染状态。
  未就绪时 `play()` 返回 False（窗口据此把 ▶ 置灰/显示"准备中"，绝不阻塞 GUI）。

═══ 可测性 ═══
  `AudioPlayer` 的声卡出口是一个**可注入的 sink 工厂**（默认开 sounddevice 的
  `OutputStream`）。测试用一个"假 sink"就能在**没有真实声卡**时验证位置语义与
  `stop()` 不卡：

      sink.write(block)   # block: (frames, channels) float32，阻塞式
      sink.close()

  位置只会在 `write()` 返回之后前进 ⇒ 喂进去 N 帧后 `position_sec() ≈ N / samplerate`，
  不喂就绝不前进。
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("ercue.audio.player")

# 播放采样率（常见输出设备都吃 48k）
SR = 48000
# 每次交给声卡的时长（秒）。★ 必须 ≥ 0.25s（A5c）—— 旧实现的 1024 帧（21ms）就是坑。
BLOCK_SEC = 0.25
# 起播前先喂这么多（秒），避免开头空转 / 爆音
PREROLL_SEC = 0.5
# stop() 的 join 超时（秒）。★ 绝不卡界面（A6）。
# 取 0.75s 而非 0.8s：join 返回后还有一点点收尾开销，留出余量才能保证**总耗时 ≤0.8s**。
STOP_TIMEOUT_SEC = 0.75

# ---- C1：打开声卡失败后的**退避**与**结果等待**----
# 连续失败第 N 次后的退避（秒）阶梯：1 → 2 → 4（封顶 OPEN_RETRY_MAX_SEC），成功即清零。
OPEN_RETRY_BASE_SEC = 1.0
OPEN_RETRY_MAX_SEC = 4.0
# play() 等待"打开结果"的最长时间（秒）：让 play() 能在**打开失败时同步返回 False**，
# 又不必在"打开成功"的正常路径上多等（成功/失败通常都在几十 ms 内出结果）。
# 只有"设备打开卡住"这种极端情形才会真的等满这个时间（之后乐观返回 True，沿用非阻塞语义）。
OPEN_WAIT_SEC = 0.4


def _open_backoff_sec(fail_count: int) -> float:
    """连续失败第 `fail_count` 次后应退避的秒数（C1）。

    · `fail_count <= 0` → 0（无失败，不退避）；
    · 1 → 1s、2 → 2s、3 → 4s、≥4 → 4s（`OPEN_RETRY_MAX_SEC` 封顶）。
    """
    if fail_count <= 0:
        return 0.0
    return min(OPEN_RETRY_MAX_SEC, OPEN_RETRY_BASE_SEC * (2 ** (fail_count - 1)))


def _normalize_want_sr(samplerate):
    """把 `AudioTrack(samplerate=...)` 归一成"目标采样率"（**0 = 跟随源原生率**）。

    · `None` / 非正数 ⇒ 0（不重采样，保持源的原生率，供**身份扫描**用）；
    · 正数 ⇒ 该整数（播放路径固定 48000）。
    """
    try:
        v = int(samplerate) if samplerate is not None else 0
    except (TypeError, ValueError):
        return 0
    return v if v > 0 else 0


# ==========================================================================
# 解码：整首进内存（纯逻辑，可脱离声卡测试）
# ==========================================================================
class AudioTrack:
    """一首歌的全部音频样本（**整首先解码到内存**，A5a）。

    `samples()` 返回 `(channels, frames)` 的 **float32** 数组（值域 [-1, 1]）；
    解码失败时 `ok` 为 False、`error` 记下原因，**绝不抛异常**。

    ★★ 采样率口径（`samplerate` 参数）—— 两个**互不混用**的用途：
      · `samplerate=SR`（48000，**默认 / 播放路径**）：强制重采样成 48k 立体声 fltp。
        预览试听走这条（A5a/A5c 的既有约定：整曲 48k 进内存、块 ≥0.25s），**逐字节不变**。
      · `samplerate=None`（**只给身份扫描用**）：保持**源文件的原生采样率**（取音频流的
        `st.rate`，不再由 swresample 改率），让 `fp_core.fingerprint_audio()` 自己按
        AutoVJ/演出端同款 `np.interp` 做重采样 ⇒ 两端喂给指纹算法的 PCM **同源同率**，
        48k 素材的 `fp_id` 才不会与演出端分叉。
        ⚠ 这条路径**只算指纹**，不用于播放（播放固定 48k）。
      · 属性 `samplerate` 恒返回**实际使用的率**（`None` 模式下即源原生率；解码失败且
        尚未读到流信息时为 0，调用方用 `sr or 默认值` 兜底）。
    """

    def __init__(self, path, samplerate=SR, channels=2):
        self.path = str(path or "")
        # `None`（或非正数）= 保持源原生率；否则强制重采样到该率。0 表示"尚未确定"。
        self._want_sr = _normalize_want_sr(samplerate)
        self._sr = self._want_sr
        self._ch = int(channels)
        self._buf = None                 # (channels, frames) float32
        self.error = ""
        self._decode()

    # ---------------- 解码 ----------------
    def _decode(self) -> None:
        import os
        if not self.path or not os.path.isfile(self.path):
            self.error = "找不到音频文件"
            return
        try:
            import av
        except Exception as e:                                   # noqa: BLE001
            self.error = "PyAV 不可用：%s: %s" % (type(e).__name__, e)
            return
        try:
            # ★ metadata_errors="replace" 不能省（GBK 元数据会让 av.open 抛 UnicodeDecodeError）
            with av.open(self.path, metadata_errors="replace") as c:
                sts = [s for s in c.streams if s.type == "audio"]
                if not sts:
                    self.error = "这个文件里没有音频轨"
                    return
                st = sts[0]
                # ★ 目标采样率：`samplerate=None` ⇒ 取源流的原生率（身份扫描口径，与演出端一致）；
                #   否则用调用方指定的率（播放路径 48k）。`st.rate` 缺失时退回 `SR`。
                native = 0
                try:
                    native = int(getattr(st, "rate", 0) or 0)
                except (TypeError, ValueError):
                    native = 0
                rate = self._want_sr or native or SR
                self._sr = int(rate)
                # 统一成 fltp（planar float32）/ 立体声；`rate` 与源相同则不改率（等同直通）。
                res = av.AudioResampler(format="fltp", layout="stereo", rate=self._sr)
                chunks = []
                for frame in c.decode(st):
                    for fr in res.resample(frame):
                        arr = fr.to_ndarray()          # fltp stereo → (2, N) float32
                        if arr.ndim == 1:
                            arr = np.stack([arr, arr], axis=0)
                        chunks.append(np.asarray(arr, dtype=np.float32))
                # flush 重采样器里残留的样本
                try:
                    for fr in res.resample(None):
                        arr = fr.to_ndarray()
                        if arr.ndim == 1:
                            arr = np.stack([arr, arr], axis=0)
                        chunks.append(np.asarray(arr, dtype=np.float32))
                except Exception:                                # noqa: BLE001
                    pass
                if not chunks:
                    self.error = "这个文件里没有可解码的音频"
                    return
                self._buf = np.ascontiguousarray(np.concatenate(chunks, axis=1))
        except Exception as e:                                   # noqa: BLE001
            log.exception("解码音频失败: %s", self.path)
            self.error = "%s: %s" % (type(e).__name__, e)
            self._buf = None

    # ---------------- 查询 ----------------
    @property
    def ok(self) -> bool:
        return self._buf is not None and self._buf.shape[1] > 0

    @property
    def samplerate(self) -> int:
        return self._sr

    @property
    def channels(self) -> int:
        return int(self._buf.shape[0]) if self._buf is not None else self._ch

    @property
    def frames(self) -> int:
        return int(self._buf.shape[1]) if self._buf is not None else 0

    @property
    def duration_sec(self) -> float:
        return (self.frames / float(self._sr)) if self._sr else 0.0

    def samples(self):
        """`(channels, frames)` float32 数组；解码失败返回 None。"""
        return self._buf


# ==========================================================================
# 声卡出口：默认实现 + 探测
# ==========================================================================
def _default_sink_factory(samplerate, channels):
    """默认 sink：sounddevice 的阻塞式 `OutputStream`（**只在播放线程里调用**）。"""
    import sounddevice as sd
    stream = sd.OutputStream(samplerate=int(samplerate), channels=int(channels),
                             dtype="float32", blocksize=0)
    stream.start()
    return stream


def _probe_output_device(samplerate=SR) -> bool:
    """探测本机是否有可用的**输出**设备（查不到就 False，绝不抛）。"""
    try:
        import sounddevice as sd
    except Exception:                                            # noqa: BLE001
        return False
    try:
        dev = sd.query_devices(kind="output")
        return bool(dev) and int(dev.get("max_output_channels", 0) or 0) > 0
    except Exception:                                            # noqa: BLE001
        return False


# ==========================================================================
# 播放器
# ==========================================================================
class AudioPlayer:
    """有状态的音频播放器：解码到内存（`load`）+ 专用线程喂声卡（`play`）。

    ★ 位置语义（A5b）：`position_sec()` = **已交给声卡的帧号 / 采样率**，
      与挂钟无关 —— 音乐停 / 放完 ⇒ 位置停 ⇒ 画面跟着停。
    """

    def __init__(self, samplerate=SR, channels=2, block_sec=BLOCK_SEC,
                 sink_factory=None, device_probe=None):
        """
        `sink_factory(samplerate, channels) -> sink`：声卡出口工厂，默认开 sounddevice。
            测试可注入"假 sink"（只需有 `write(block)` 与 `close()`）。
        `device_probe(samplerate) -> bool`：输出设备探测，默认查 sounddevice。
        """
        self._sr = int(samplerate)
        self._ch = int(channels)
        self._block = max(1, int(round(float(block_sec) * self._sr)))
        self._preroll = max(self._block, int(round(PREROLL_SEC * self._sr)))
        self._sink_factory = sink_factory or _default_sink_factory
        self._device_ok = bool((device_probe or _probe_output_device)(self._sr))
        # ★ C1：`_open_failed` 只表示"**上次**打开失败"（供 available 判断），
        #   它**不是**永久的锁 —— play() 会复位它并重试；连续失败由 `_open_fail_count`
        #   + `_next_open_ts` 做时间退避。
        self._open_failed = False
        self._open_fail_count = 0          # 连续打开失败次数（成功即清零）
        self._next_open_ts = 0.0           # 下一次允许尝试打开的最早时刻（monotonic）
        self._opened = threading.Event()   # 本次 play 会话"打开结果已定"的信号
        self._open_result = None           # 本次 play 会话打开结果："ok" / "fail" / None
        self.error = ""

        self._track = None
        self._loaded_path = ""
        self._i = 0                       # ★ 已交给声卡的帧号（唯一位置真源）
        self._lock = threading.Lock()
        self._stop = None                 # 当前播放会话的停止事件（每次 play 新建）
        self._th = None
        # 后台解码（P2：整曲解码不再阻塞 GUI 线程）
        self._load_lock = threading.Lock()
        self._loading = False
        self._load_path = ""
        self._load_th = None
        self._load_token = 0              # 每次新加载/close 递增，旧解码线程的结果作废

    # ---------------- 状态 ----------------
    @property
    def available(self) -> bool:
        """本机是否有可用输出设备，且**不处于打开失败的退避冷却期**。

        ★ C1 修复：打开失败后**不再"永久不可用"** —— 退避冷却期（1→2→4s）一过即自动
          恢复为 True，因此一次打开失败不会让整个会话静音；冷却期内返回 False 以
          **抑制高频重试**（UI 据此暂时退回挂钟驱动）。
        """
        if not self._device_ok:
            return False
        if not self._open_failed:
            return True
        return time.monotonic() >= self._next_open_ts

    @property
    def loaded(self) -> bool:
        return self._track is not None and self._track.ok

    @property
    def loading(self) -> bool:
        """是否正在**后台解码**（GUI 据此把 ▶ 置灰/显示"准备中"）。"""
        with self._load_lock:
            return self._loading

    @property
    def loading_path(self) -> str:
        with self._load_lock:
            return self._load_path

    @property
    def loaded_path(self) -> str:
        return self._loaded_path

    @property
    def duration_sec(self) -> float:
        return self._track.duration_sec if self._track is not None else 0.0

    @property
    def samplerate(self) -> int:
        return self._sr

    def is_playing(self) -> bool:
        with self._lock:
            th, ev = self._th, self._stop
        return th is not None and th.is_alive() and (ev is None or not ev.is_set())

    def position_sec(self) -> float:
        """★ 当前播到第几秒 = **实际交给声卡的帧号 / 采样率**（停了就不动）。"""
        with self._lock:
            i = self._i
        total = self._track.frames if self._track is not None else 0
        if total <= 0:
            return 0.0
        return min(max(0, i), total) / float(self._sr)

    # ---------------- 装载（后台解码，绝不阻塞调用方） ----------------
    def load(self, path, wait=False) -> bool:
        """发起**整曲解码**（后台线程，不阻塞调用方）。

        返回 True 表示"已发起加载 / 已经就绪"；False 表示**立即**判定路径无效
        （空路径 / 文件不存在），无需解码。解码完成与否用 `loading` / `loaded` 查询，
        `wait=True` 时阻塞到解码结束（测试 / 需要确定性的场合用）。
        """
        import os
        path = str(path or "")
        if self.loaded and self._loaded_path == path:
            return True                                    # 已经是这首歌且就绪
        with self._load_lock:
            if self._loading and self._load_path == path:
                return True                                # 同路径已在解码 → 幂等
        if not path or not os.path.isfile(path):
            self._set_load_result(None, "", path, "找不到音频文件")
            return False
        self.stop()                                        # 停旧播放线程
        with self._load_lock:
            self._load_token += 1
            token = self._load_token
            self._loading = True
            self._load_path = path
            self._track = None                             # 解码期间不可播
            self._loaded_path = ""
            self.error = ""
        th = threading.Thread(target=self._decode_worker, args=(token, path),
                              name="ercue-audio-dec", daemon=True)
        self._load_th = th
        th.start()
        if wait:
            return self.wait_loaded()
        return True

    def wait_loaded(self, timeout=None) -> bool:
        """阻塞到当前解码结束（`timeout` 秒后无论完成与否都返回）。返回是否就绪。"""
        with self._load_lock:
            th = self._load_th
        if th is not None and th.is_alive():
            th.join(timeout=timeout)
        return self.loaded and not self.loading

    def _set_load_result(self, track, loaded_path, load_path, error):
        with self._load_lock:
            self._track = track
            self._loaded_path = loaded_path
            self._load_path = load_path
            self._loading = False
            self.error = error or ""

    def _decode_worker(self, token, path):
        """后台解码线程（**只做解码，绝不碰 sounddevice**，与播放线程分开）。"""
        track = AudioTrack(path, samplerate=self._sr, channels=self._ch)
        with self._load_lock:
            if token != self._load_token:
                return                                     # 已被更新的加载请求 / close 取代
            self._track = track if track.ok else None
            self._loaded_path = track.path if track.ok else ""
            self._load_path = path
            self._loading = False
            if track.ok:
                self.error = ""
            else:
                self.error = track.error or "解码失败"
                log.warning("音频装载失败：%s（%s）", path, self.error)
                return
        log.info("音频已解码：%s（%.1f 秒 / %d Hz / %d 声道）",
                 track.path, track.duration_sec, track.samplerate, track.channels)

    # ---------------- 播放控制 ----------------
    def play(self, from_sec: float) -> bool:
        """从 `from_sec` 起播。返回 False 表示"**没起播**"（未装载 / 无设备 / 打开设备失败）
        —— 调用方据此退回挂钟。

        ★★ C1 修复：**打开设备失败不再永久静音**。
          ① 每次显式 `play()` 都复位"上次打开失败"标记并重新尝试打开设备；
          ② 连续失败（≥2 次）后按 1→2→4s 退避，冷却期内直接返回 False、**不再尝试**
             （绝不每帧重试）；**首败后的第一次**重试仍放行，便于条件恢复后一次 ▶ 即恢复；
          ③ 打开在专用线程（A6）；此处**有界等待**其结果，故打开失败时能**同步返回
             False** 并带上 `error`（而不是像旧实现那样先返回 True、再异步静音）。
        """
        if self._track is None or not self._track.ok:
            self.error = "还没装载音频"
            return False
        if not self._device_ok:
            self.error = self.error or "本机没有可用的音频输出设备"
            return False

        now = time.monotonic()
        with self._lock:
            fails = self._open_fail_count
            next_ts = self._next_open_ts
        # ② 退避：连续失败 ≥2 次后，冷却期内**不再尝试**（避免高频重试拖住界面/线程）。
        if fails >= 2 and now < next_ts:
            self.error = "音频输出设备打开失败，%.1f 秒内不再重试" % (next_ts - now)
            return False

        # ① 显式 play() 一律复位"永久失败"标记，本次真正尝试打开（绝不再永久拒绝）。
        with self._lock:
            self._open_failed = False

        self.stop()                       # 先停旧的（旧会话事件的 _stop 保持置位，旧线程会退出）

        total = self._track.frames
        f = max(0, int(float(from_sec) * self._sr))
        with self._lock:
            self._i = min(f, total)        # 位置跳到 from_sec
        ev = threading.Event()
        opened = threading.Event()
        with self._lock:
            self._stop = ev
            self._opened = opened
            self._open_result = None       # 本次会话结果未定
        th = threading.Thread(target=self._run, args=(ev, opened), name="ercue-audio", daemon=True)
        with self._lock:
            self._th = th
        th.start()
        # ③ 有界等待"打开结果"：失败 → 立刻返回 False（带 error）；成功 → 立刻返回 True；
        #    打开卡住（超时未定）→ 乐观返回 True（沿用旧的"非阻塞"语义，不至于久等）。
        opened.wait(OPEN_WAIT_SEC)
        with self._lock:
            res = self._open_result
        return res != "fail"

    def pause(self) -> None:
        """暂停（保留当前位置，可再 `play(position_sec())` 续播）。"""
        self.stop()

    def stop(self) -> None:
        """停止播放线程。★ 必须在 ≤0.8s 内返回（A6，绝不卡界面）。"""
        with self._lock:
            ev, th = self._stop, self._th
            self._th = None
        if ev is not None:
            ev.set()
        if th is not None and th.is_alive():
            try:
                th.join(timeout=STOP_TIMEOUT_SEC)
            except Exception:                                    # noqa: BLE001
                log.exception("等待音频线程退出失败")
        # 注：若 join 超时（声卡卡在 write 里），线程仍会自行察觉 ev 已置位并释放自己的 stream；
        #     这里**绝不**从别的线程去 close 声卡（那才是真会出事）。

    def close(self) -> None:
        """释放音频（换歌 / 关窗 / `shutdown()` 时调）：停线程、丢缓冲、作废在途解码。"""
        self.stop()
        with self._load_lock:
            self._load_token += 1          # 作废在途解码结果（不让它回填已关闭的播放器）
            self._track = None
            self._loaded_path = ""
            self._load_path = ""
            self._loading = False
        with self._lock:
            self._i = 0
            self._stop = None

    # ---------------- 打开结果的状态变更（只在持锁时改，且只在"本会话仍有效"时改） ----------------
    def _is_current_session(self, ev) -> bool:
        """`ev` 是否仍是当前会话（未被更新的 `play()` / `close()` 取代）。

        ★ 防串味：旧播放线程在被新会话取代后，其迟到回调**不得**污染新会话的状态
          （与解码侧 `_load_token` 同一思路）。
        """
        return ev is None or self._stop is ev

    def _on_open_ok(self, ev=None) -> None:
        """打开成功：清零退避、标记可用、登记本次会话结果为 ok。"""
        with self._lock:
            if not self._is_current_session(ev):
                return
            self._open_fail_count = 0
            self._next_open_ts = 0.0
            self._open_failed = False
            self._open_result = "ok"

    def _on_open_failed(self, exc, prefix="打开音频输出设备失败", ev=None) -> None:
        """打开/播放失败：累加连续失败、登记退避、置 `available=False`、记 `error`。"""
        now = time.monotonic()
        with self._lock:
            if not self._is_current_session(ev):
                return
            self._open_fail_count += 1
            self._next_open_ts = now + _open_backoff_sec(self._open_fail_count)
            self._open_failed = True
            self._open_result = "fail"
            self.error = "%s：%s: %s" % (prefix, type(exc).__name__, exc)

    # ---------------- 播放线程（A6：sounddevice 只在这里碰） ----------------
    def _run(self, ev: threading.Event, opened: threading.Event = None) -> None:
        buf = self._track.samples() if self._track is not None else None
        total = self._track.frames if self._track is not None else 0
        sink = None
        try:
            try:
                sink = self._sink_factory(self._sr, self._ch)
            except Exception as e:                               # noqa: BLE001
                log.exception("打开音频输出设备失败")
                self._on_open_failed(e, "打开音频输出设备失败", ev)
                return
            # 打开成功：先登记结果再发信号（play() 据此即刻返回 True，无需等满超时）
            self._on_open_ok(ev)
            if opened is not None:
                opened.set()
            try:
                first = True
                while not ev.is_set():
                    with self._lock:
                        i = self._i
                    if buf is None or i >= total:
                        break
                    # 首块用 preroll（≥0.25s），避免起播空转（PREROLL_SEC）
                    n = self._preroll if first else self._block
                    first = False
                    chunk = buf[:, i:i + n]
                    if chunk.shape[1] == 0:
                        break
                    data = np.ascontiguousarray(chunk.T)         # (frames, channels)
                    sink.write(data)                             # ★ 阻塞式；返回后才前进
                    with self._lock:
                        self._i = min(i + chunk.shape[1], total)  # A5b：只有真交出去才算
            except Exception as e:                               # noqa: BLE001
                log.exception("音频播放线程异常")
                self._on_open_failed(e, "音频播放异常", ev)
        finally:
            # 保证 play() 的等待一定会被唤醒（异常/超时路径也不例外）
            if opened is not None and not opened.is_set():
                opened.set()
            if sink is not None:
                try:
                    sink.close()
                except Exception:                                # noqa: BLE001
                    log.exception("关闭音频输出失败")
            ev.set()
