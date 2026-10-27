# -*- coding: utf-8 -*-
r"""★ 音频试听走带（详情页播放按钮用）—— 整曲解码到内存 + sounddevice 按帧号推进。

移植自 ERCUE `src/ercue/audio/player.py` 的 `AudioPlayer`（同一套位置语义），
但**解码器换成 soundfile**（ERALIB 禁止 PyAV；只有 ERAVJ 侧在 soundfile 失败时
才可选回落到 av，ERALIB 打包不含 av 也不影响主流程）。

## 位置语义（唯一真源）
  `position_sec()` = **已经交给声卡的帧号 / 采样率**，与挂钟无关：
  声卡停 / 放完 ⇒ 位置停。因此详情页的播放头不会「音乐停了还在走」。

## 关键约束（每条都对应真实踩过的坑）
  1. **整曲先解码进内存**，播放循环只做「取下一块 → 交给声卡 → 索引前进」。
     绝不「边解边播的有界队列」（队列满丢块 ⇒ 音乐比挂钟慢，越听越偏）。
  2. 位置只在 `sink.write()` **真正返回之后**前进（绝不是挂钟推算）。
  3. 每次交给声卡的时长 `BLOCK_SEC`=0.1s（4800 帧）：既远大于旧实现踩坑的 1024 帧
     （21ms，会欠载断续），又让 `position_sec()` 的「超前量」≤0.1s（详情页要求
     点播放后位置与播放头偏差 <0.2s）。
  4. sounddevice 的调用**只在专用播放线程**（绝不在 GUI 线程）。
  5. `stop()` 的 join 超时 ≤ 0.8 s（`STOP_TIMEOUT_SEC`），绝不卡界面。
  6. 打开设备在播放线程里做，`play()` **有界等待**其结果：失败时**同步返回 False**
     并写 `error`（UI 据此提示，不抛异常、不静默）。
  7. `load()` 在后台线程解码，不阻塞 GUI；未就绪时 `play()` 返回 False。

## 解码
  `soundfile` 读成 `(frames, channels)` float32；单声道复制成双声道、多声道取前 2。
  采样率：44.1k / 48k 原样播放（声卡直接支持）；其它率用 `np.interp` 重采样到 48k
  （线性插值足够做试听，且不引新依赖）。
  失败 → 可选 av 回落（仅当环境里装了 av，例如 ERAVJ）；再失败 → `error` 记录原因。

## 可测性
  声卡出口是可注入的 sink 工厂（默认 sounddevice `OutputStream`）。测试注入「假 sink」
  即可在没有真实声卡时验证位置语义与 `stop()` 不卡：

      sink.write(block)   # block: (frames, channels) float32，阻塞式
      sink.close()
"""
from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger("autovj.audio_playback")

# 默认播放采样率（仅在需要重采样时作为目标率）
SR = 48000
# 每次交给声卡的时长（秒）。
# ★ 0.1s 的取舍（实测）：块越大越抗欠载，但 `position_sec()` 是按「已交给声卡的帧号」
#   算的，块越大 ⇒ 播头越超前（ERCUE 用 0.25s + 0.5s 预喂，起播位置会领先 0.5s）。
#   详情页要求「点播放后位置与播放头偏差 < 0.2s」，所以这里取 0.1s（4800 帧）：
#   仍远大于旧实现踩坑的 1024 帧（21ms），且位置超前 ≤ 0.1s。
BLOCK_SEC = 0.1
# 起播前先喂这么多（秒），避免开头空转 / 爆音；同样受上一条约束，压到 ≈0.12s。
PREROLL_SEC = 0.12
# stop() 的 join 超时（秒）。★ 绝不卡界面。
STOP_TIMEOUT_SEC = 0.75
# play() 等待「打开结果」的最长时间（秒）：失败能在 OPEN_WAIT_SEC 内同步返回 False。
OPEN_WAIT_SEC = 0.4
# 允许原样播放的采样率（常见输出设备都支持）；其它率重采样到 SR。
_NATIVE_SRS = (44100, 48000)
# 小于这个时长（秒）的音频视为无效
_MIN_DUR_SEC = 0.2


# ==========================================================================
# 解码
# ==========================================================================
def _to_stereo(data) -> np.ndarray:
    """把 `(frames, channels)` float32 归一成双声道 `(frames, 2)`。

    · 单声道 → 复制成双声道；
    · ≥2 声道 → 取前 2 个；
    · 0 声道 / 空数组 → 返回空数组（由调用方判失败）。
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] == 0:
        return np.zeros((0, 2), dtype=np.float32)
    ch = int(arr.shape[1])
    if ch == 1:
        out = np.repeat(arr, 2, axis=1)
    elif ch >= 2:
        out = arr[:, :2]
    else:                                        # pragma: no cover - 上面的分支已覆盖
        out = np.zeros((0, 2), dtype=np.float32)
    return np.ascontiguousarray(out, dtype=np.float32)


def _resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """按列（声道）线性重采样：`(frames, ch) → (new_frames, ch)`。

    试听用途，线性插值足够；不做抗混叠（不引 scipy 依赖）。
    """
    if src_sr == dst_sr or src_sr <= 0 or dst_sr <= 0:
        return x
    n_in = int(x.shape[0])
    n_out = max(1, int(round(n_in * float(dst_sr) / float(src_sr))))
    src_t = np.arange(n_in, dtype=np.float64) / float(src_sr)
    dst_t = np.arange(n_out, dtype=np.float64) / float(dst_sr)
    out = np.empty((n_out, x.shape[1]), dtype=np.float32)
    for c in range(x.shape[1]):
        out[:, c] = np.interp(dst_t, src_t, x[:, c]).astype(np.float32)
    return np.ascontiguousarray(out)


class AudioTrack:
    """一首歌的全部样本（**整曲解码到内存**），`(frames, channels)` float32。

    `ok` 为 False 时 `error` 记录原因；**绝不抛异常到调用方**。
    `samplerate` 恒为**实际播放率**（44.1k / 48k / 重采样后的 48k）。
    """

    def __init__(self, path, target_sr=0):
        self.path = str(path or "")
        self._want_sr = int(target_sr) if target_sr else 0
        self._sr = self._want_sr or SR
        self._buf = np.zeros((0, 2), dtype=np.float32)
        self.error = ""
        self._decode()

    # ---------------- 解码 ----------------
    def _decode(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            self.error = "找不到音频文件"
            return
        if self._decode_soundfile():
            return
        if self._decode_av():
            return
        if not self.error:
            self.error = "无法解码该音频格式"

    def _finish(self, buf: np.ndarray, sr: int) -> bool:
        """做采样率归一 + 有效性检查，落到 `self._buf` / `self._sr`。"""
        sr = int(sr or 0)
        if sr <= 0 or buf.size == 0:
            return False
        if self._want_sr and sr != self._want_sr:
            buf = _resample_linear(buf, sr, self._want_sr)
            sr = self._want_sr
        elif sr not in _NATIVE_SRS:
            # 非常见率（96k/32k…）统一重采样到 48k，避免声卡拒绝
            buf = _resample_linear(buf, sr, SR)
            sr = SR
        if buf.shape[0] <= int(_MIN_DUR_SEC * sr):
            return False
        self._buf = np.ascontiguousarray(buf, dtype=np.float32)
        self._sr = sr
        self.error = ""
        return True

    def _decode_soundfile(self) -> bool:
        """首选 soundfile（ERALIB 打包内含；支持 wav/flac/mp3/ogg）。"""
        try:
            import soundfile as sf
        except Exception as e:                                   # noqa: BLE001
            self.error = "soundfile 不可用：%s: %s" % (type(e).__name__, e)
            return False
        try:
            data, sr = sf.read(self.path, dtype="float32", always_2d=True)
        except Exception as e:                                   # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
            log.info("soundfile 解码失败，尝试回落：%s（%s）", self.path, e)
            return False
        buf = _to_stereo(data)
        if not self._finish(buf, sr):
            self.error = self.error or "音频内容为空"
            return False
        return True

    def _decode_av(self) -> bool:
        """可选回落 av（仅 ERAVJ 环境有；ERALIB 打包不含 av）—— 用于 m4a/aac 等。"""
        try:
            import av
        except Exception:                                        # noqa: BLE001
            return False
        try:
            chunks = []
            with av.open(self.path, metadata_errors="replace") as c:
                sts = [s for s in c.streams if s.type == "audio"]
                if not sts:
                    self.error = "这个文件里没有音频轨"
                    return False
                st = sts[0]
                native = 0
                try:
                    native = int(getattr(st, "rate", 0) or 0)
                except (TypeError, ValueError):
                    native = 0
                rate = self._want_sr or native or SR
                res = av.AudioResampler(format="fltp", layout="stereo", rate=int(rate))
                for frame in c.decode(st):
                    for fr in res.resample(frame):
                        arr = fr.to_ndarray()
                        if arr.ndim == 1:
                            arr = np.stack([arr, arr], axis=0)
                        chunks.append(np.asarray(arr, dtype=np.float32).T)   # (frames,2)
                try:
                    for fr in res.resample(None):
                        arr = fr.to_ndarray()
                        if arr.ndim == 1:
                            arr = np.stack([arr, arr], axis=0)
                        chunks.append(np.asarray(arr, dtype=np.float32).T)
                except Exception:                                # noqa: BLE001
                    pass
            if not chunks:
                self.error = "这个文件里没有可解码的音频"
                return False
            buf = np.ascontiguousarray(np.concatenate(chunks, axis=0), dtype=np.float32)
            if not self._finish(buf, rate):
                self.error = self.error or "音频内容为空"
                return False
            return True
        except Exception as e:                                   # noqa: BLE001
            log.exception("av 解码音频失败: %s", self.path)
            self.error = "%s: %s" % (type(e).__name__, e)
            return False

    # ---------------- 查询 ----------------
    @property
    def ok(self) -> bool:
        return self._buf is not None and self._buf.shape[0] > 0

    @property
    def samplerate(self) -> int:
        return int(self._sr)

    @property
    def channels(self) -> int:
        return int(self._buf.shape[1]) if self._buf is not None else 2

    @property
    def frames(self) -> int:
        return int(self._buf.shape[0]) if self._buf is not None else 0

    @property
    def duration_sec(self) -> float:
        return (self.frames / float(self._sr)) if self._sr else 0.0

    def samples(self):
        """`(frames, channels)` float32 数组；解码失败返回 None。"""
        return self._buf


# ==========================================================================
# 声卡出口
# ==========================================================================
def _default_sink_factory(samplerate, channels):
    """默认 sink：sounddevice 阻塞式 `OutputStream`（**只在播放线程里调用**）。"""
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
    """有状态的试听播放器：`load()` 后台解码，`play(from_sec)` 专用线程喂声卡。

    · `seek(sec)` 可在播放中调用（播放线程每块都重读位置，立即生效）；
    · `pause()` 保留位置；`stop()` 停止并保留位置；`close()` 释放缓冲与事件；
    · 任何声卡异常都**只**体现在 `error` / 返回 False 上，绝不抛到 GUI。
    """

    def __init__(self, samplerate=0, channels=2, block_sec=BLOCK_SEC,
                 sink_factory=None, device_probe=None):
        """
        `samplerate`：0 = 跟随文件原生率（44.1k/48k 原样，其它重采样到 48k）；
                      >0 = 强制重采样到该率。
        `sink_factory(samplerate, channels) -> sink`：声卡出口工厂，默认 sounddevice。
        `device_probe(samplerate) -> bool`：输出设备探测，默认查 sounddevice。
        """
        self._want_sr = int(samplerate) if samplerate else 0
        self._ch = int(channels)
        self._block_sec = max(0.05, float(block_sec))
        self._sink_factory = sink_factory or _default_sink_factory
        self._device_ok = bool((device_probe or _probe_output_device)(
            self._want_sr or SR))
        self.error = ""
        self._open_failed = False

        self._track = None
        self._loaded_path = ""
        self._sr = self._want_sr or SR
        self._i = 0                        # ★ 已交给声卡的帧号（唯一位置真源）
        # ★ seek 序号：seek() 每调一次 +1。播放线程写回 `_i` 前先比对序号，
        #   若期间外部 seek 过就**不覆盖**（否则 seek 会被"正在写的那块"回滚掉）。
        self._seek_seq = 0
        self._lock = threading.Lock()
        self._stop_ev = None               # 当前播放会话的停止事件（每次 play 新建）
        self._th = None
        # 后台解码
        self._load_lock = threading.Lock()
        self._loading = False
        self._load_path = ""
        self._load_th = None
        self._load_token = 0

    # ---------------- 状态 ----------------
    @property
    def available(self) -> bool:
        """本机是否有可用输出设备（没有声卡时为 False，UI 据此禁用播放）。"""
        return bool(self._device_ok)

    @property
    def loaded(self) -> bool:
        return self._track is not None and self._track.ok

    @property
    def loading(self) -> bool:
        with self._load_lock:
            return self._loading

    @property
    def loaded_path(self) -> str:
        return self._loaded_path

    @property
    def duration_sec(self) -> float:
        return self._track.duration_sec if self._track is not None else 0.0

    @property
    def samplerate(self) -> int:
        return int(self._sr)

    def is_playing(self) -> bool:
        with self._lock:
            th, ev = self._th, self._stop_ev
        return th is not None and th.is_alive() and (ev is None or not ev.is_set())

    def position_sec(self) -> float:
        """★ 当前播到第几秒 = 实际交给声卡的帧号 / 采样率（停了就不动）。"""
        with self._lock:
            i = self._i
        total = self._track.frames if self._track is not None else 0
        if total <= 0 or self._sr <= 0:
            return 0.0
        return min(max(0, i), total) / float(self._sr)

    # ---------------- 装载（后台解码，不阻塞调用方） ----------------
    def load(self, path, wait=False) -> bool:
        """发起整曲解码（后台线程）。返回 False 仅当路径**立即**判定无效。"""
        path = str(path or "")
        if self.loaded and self._loaded_path == path:
            return True
        with self._load_lock:
            if self._loading and self._load_path == path:
                return True
        if not path or not os.path.isfile(path):
            self.error = "找不到音频文件"
            return False
        self.stop()
        with self._load_lock:
            self._load_token += 1
            token = self._load_token
            self._loading = True
            self._load_path = path
            self._track = None
            self._loaded_path = ""
            self.error = ""
        th = threading.Thread(target=self._decode_worker, args=(token, path),
                              name="eravj-preview-dec", daemon=True)
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

    def _decode_worker(self, token, path) -> None:
        """后台解码线程（**只做解码，绝不碰 sounddevice**）。"""
        track = AudioTrack(path, target_sr=self._want_sr)
        with self._load_lock:
            if token != self._load_token:
                return                                  # 已被更新的加载 / close 取代
            self._track = track if track.ok else None
            self._loaded_path = track.path if track.ok else ""
            self._load_path = path
            self._loading = False
            self._sr = track.samplerate if track.ok else (self._want_sr or SR)
            self.error = "" if track.ok else (track.error or "解码失败")
        if track.ok:
            with self._lock:
                self._i = 0
            log.info("音频已解码：%s（%.1f 秒 / %d Hz）",
                     track.path, track.duration_sec, track.samplerate)
        else:
            log.warning("音频装载失败：%s（%s）", path, self.error)

    # ---------------- 播放控制 ----------------
    def seek(self, sec) -> float:
        """把播放位置跳到 `sec`（播放中调用立即生效）。返回实际位置（秒）。"""
        try:
            sec = float(sec)
        except (TypeError, ValueError):
            sec = 0.0
        total = self._track.frames if self._track is not None else 0
        if total <= 0 or self._sr <= 0:
            return 0.0
        f = int(max(0.0, sec) * self._sr)
        with self._lock:
            self._i = max(0, min(f, total))
            self._seek_seq += 1
            i = self._i
        return i / float(self._sr)

    def play(self, from_sec=0.0) -> bool:
        """从 `from_sec` 起播。

        返回 False 表示**没起播**（未装载 / 无输出设备 / 打开设备失败），
        `error` 里带原因；调用方据此提示，不抛异常。
        """
        if self._track is None or not self._track.ok:
            self.error = "还没装载音频"
            return False
        if not self._device_ok:
            self.error = self.error or "本机没有可用的音频输出设备"
            return False

        self.stop()                                   # 先停旧会话
        self.seek(from_sec)
        ev = threading.Event()
        opened = threading.Event()
        with self._lock:
            self._stop_ev = ev
            self._open_failed = False
        th = threading.Thread(target=self._run, args=(ev, opened),
                              name="eravj-preview", daemon=True)
        with self._lock:
            self._th = th
        th.start()
        opened.wait(OPEN_WAIT_SEC)
        with self._lock:
            failed = self._open_failed
        return not failed

    def pause(self) -> None:
        """暂停（保留当前位置，可再 `play(position_sec())` 续播）。"""
        self.stop()

    def stop(self) -> None:
        """停止播放线程。★ 必须在 ≤0.8s 内返回（绝不卡界面）。"""
        with self._lock:
            ev, th = self._stop_ev, self._th
            self._th = None
            self._stop_ev = None
        if ev is not None:
            ev.set()
        if th is not None and th.is_alive():
            try:
                th.join(timeout=STOP_TIMEOUT_SEC)
            except Exception:                                    # noqa: BLE001
                log.exception("等待音频线程退出失败")
        # 若 join 超时（声卡卡在 write 里），线程会自己察觉 ev 已置位并释放 stream；
        # 这里**绝不**从别的线程 close 声卡（那才会真出事）。

    def close(self) -> None:
        """释放音频（换歌 / 关详情页 / 退出时调）：停线程、丢缓冲、作废在途解码。"""
        self.stop()
        with self._load_lock:
            self._load_token += 1
            self._track = None
            self._loaded_path = ""
            self._load_path = ""
            self._loading = False
        with self._lock:
            self._i = 0
            self._stop_ev = None

    # ---------------- 播放线程（声卡只在这里碰） ----------------
    def _run(self, ev: threading.Event, opened: threading.Event) -> None:
        track = self._track
        buf = track.samples() if track is not None else None
        total = track.frames if track is not None else 0
        sr = self._sr
        block = max(1, int(round(self._block_sec * sr)))
        preroll = max(block, int(round(PREROLL_SEC * sr)))
        sink = None
        try:
            try:
                sink = self._sink_factory(sr, self._ch)
            except Exception as e:                               # noqa: BLE001
                log.exception("打开音频输出设备失败")
                with self._lock:
                    self._open_failed = True
                    self.error = "打开音频输出设备失败：%s: %s" % (type(e).__name__, e)
                return
            if opened is not None:
                opened.set()                     # 打开成功 → play() 立刻返回 True
            first = True
            while not ev.is_set():
                with self._lock:
                    i = self._i
                    seq = self._seek_seq
                if buf is None or i >= total:
                    break
                n = preroll if first else block
                first = False
                chunk = buf[i:i + n]
                if chunk.shape[0] == 0:
                    break
                sink.write(np.ascontiguousarray(chunk, dtype=np.float32))
                with self._lock:
                    # ★ 只有真交给声卡之后才推进位置（A5b）；
                    #   若这段时间里外部 seek 过（seq 变了），就不覆盖它的新位置。
                    if seq == self._seek_seq:
                        self._i = min(i + chunk.shape[0], total)
        except Exception as e:                                   # noqa: BLE001
            log.exception("音频播放线程异常")
            with self._lock:
                self._open_failed = True
                self.error = "音频播放异常：%s: %s" % (type(e).__name__, e)
        finally:
            if opened is not None and not opened.is_set():
                opened.set()                     # 异常路径也要唤醒 play() 的等待
            if sink is not None:
                try:
                    sink.close()
                except Exception:                                # noqa: BLE001
                    log.exception("关闭音频输出失败")
            ev.set()
