# -*- coding: utf-8 -*-
"""M4 · 音频走带测试（`ercue.audio.player`）。

跑法：
    P:\\AutoVJ\\venv\\Scripts\\python.exe  P:\\EasyRealityCue\\tests\\test_audio.py

覆盖（对应任务书验收标准 + 架构方案 §4-A5/A6 逐条）：
  1. **解码**：自造 wav / mp3 → `samplerate == 48000`（重采样统一）、`channels == 2`、
     `duration_sec` 正确、`samples().shape == (channels, frames)`、dtype float32。
  2. **位置语义（A5b）**：用**可注入的假 sink** 证明 `position_sec()` 来自
     "已交给声卡的帧号 / 采样率" —— 喂进去 N 帧后 ≈ N/samplerate，**不喂就绝不前进**
     （挂钟在走、位置不动，正是旧实现"音乐停了画面还在走"的解药）。
  3. **块大小（A5c）**：`BLOCK_SEC >= 0.25`。
  4. **`stop()` 不卡（A6）**：即使声卡出口的 `write()` 卡住，`stop()` 也在 ≤0.8s 内返回。
  5. **无设备不崩**：`available=False` 时 `load/play/pause/stop/position/is_playing/close`
     全部可安全调用、`play()` 返回 False、不抛异常。
  6. **设备打开失败优雅降级**：运行中打开声卡失败 → `available` 变 False、`is_playing()` 变
     False、进程不崩（窗口据此退回挂钟）。
  7. **坏文件不炸**：不存在 / 无音频轨的文件 → `load()` 返回 False 且记 `error`。

测试夹具全部落**系统临时目录**（`tempfile.mkdtemp()`），不写项目目录、不写用户目录。
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import traceback
import wave

# 本测试**故意**触发"坏文件 / 打开声卡失败"等预期失败路径，这些路径会 `log.exception`。
# 挂一个 NullHandler 吸收它们（否则回溯会污染绿灯输出）；这些路径都已被断言覆盖。
_lg = logging.getLogger("ercue.audio.player")
_lg.propagate = False
if not _lg.handlers:
    _lg.addHandler(logging.NullHandler())

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                             # noqa: E402
import av                                                      # noqa: E402

from ercue.audio import player as ap                           # noqa: E402
from ercue.audio.player import AudioPlayer, AudioTrack         # noqa: E402

TMP = tempfile.mkdtemp(prefix="ercue_audio_")


# ==========================================================================
# 测试夹具：系统临时目录里造 wav / mp3（不写项目目录）
# ==========================================================================
def _make_wav(path, sec=1.5, sr=44100, ch=2):
    n = int(sec * sr)
    t = np.arange(n, dtype=np.float64) / sr
    sig = 0.3 * np.sin(2 * np.pi * 440.0 * t)
    data = np.empty((n, ch), np.int16)
    for c in range(ch):
        data[:, c] = (sig * 32767).astype(np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(ch)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(data.tobytes())
    return path


def _make_mp3(path, sec=0.8, sr=22050):
    c = av.open(path, "w")
    st = c.add_stream("libmp3lame", rate=sr)
    st.layout = "mono"
    n = int(sec * sr)
    t = np.arange(n, dtype=np.float32) / sr
    sig = (0.25 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32).reshape(1, -1)
    fr = av.AudioFrame.from_ndarray(sig, format="flt", layout="mono")
    fr.sample_rate = sr
    for pk in st.encode(fr):
        c.mux(pk)
    for pk in st.encode():
        c.mux(pk)
    c.close()
    return path


def _fixtures():
    return {
        "wav": _make_wav(os.path.join(TMP, "song.wav"), sec=1.5, sr=44100, ch=2),
        "mp3": _make_mp3(os.path.join(TMP, "song.mp3"), sec=0.8, sr=22050),
        "long": _make_wav(os.path.join(TMP, "long.wav"), sec=120.0, sr=44100, ch=1),
        "bad": os.path.join(TMP, "__no_such_audio__.flac"),
    }


def _wait_until(pred, timeout=3.0, interval=0.005):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


# ==========================================================================
# 假 sink（可注入）—— 没有真实声卡也能测位置语义 / stop 不卡
# ==========================================================================
class GateSink:
    """每写一块必须先拿一个 permit（信号量）才返回；帧号只在返回后计入。

    ⇒ 精确模拟"位置 = 已交给声卡的帧号"：**不喂就不前进**。
    """

    def __init__(self):
        self.gate = threading.Semaphore(0)
        self.frames = 0
        self.closed = False
        self.blocks = 0

    def write(self, block):
        if not self.gate.acquire(timeout=5.0):
            raise TimeoutError("test gate timeout")
        self.frames += int(block.shape[0])
        self.blocks += 1

    def close(self):
        self.closed = True


class StuckSink:
    """`write()` 一直阻塞（模拟卡死的声卡出口）—— 用来验证 `stop()` 不卡界面。"""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()

    def write(self, block):
        self.entered.set()
        self.release.wait(timeout=3.0)

    def close(self):
        pass


# ==========================================================================
# 1) 解码
# ==========================================================================
def test_decode(fx):
    print("    文件                              采样率  声道  时长(s)   samples.shape   dtype")
    for key, want_dur in (("wav", 1.5), ("mp3", 0.8)):
        tr = AudioTrack(fx[key])
        assert tr.ok, "解码 %s 失败：%s" % (key, tr.error)
        s = tr.samples()
        print("    %-30s %6d %5d %8.3f   %-14s %s"
              % (os.path.basename(fx[key]), tr.samplerate, tr.channels,
                 tr.duration_sec, str(s.shape), s.dtype))
        assert tr.samplerate == 48000, "应统一重采样到 48000，实际 %d" % tr.samplerate
        assert tr.channels == 2, "应统一成立体声，实际 %d" % tr.channels
        assert s.dtype == np.float32, s.dtype
        assert s.shape[0] == tr.channels
        assert s.shape[1] == tr.frames
        assert abs(tr.duration_sec - want_dur) < 0.05, \
            "%s 时长应 ≈ %.2fs，实际 %.3f" % (key, want_dur, tr.duration_sec)
        # 时长 = 帧数 / 采样率（自洽）
        assert abs(tr.duration_sec - tr.frames / tr.samplerate) < 1e-9
    print("  ✓ 解码：wav/mp3 均统一为 48kHz 立体声 float32，时长与 shape 正确")


def test_bad_file_is_graceful(fx):
    tr = AudioTrack(fx["bad"])
    assert tr.ok is False and tr.error, "坏文件应 ok=False 且有 error"
    assert tr.samples() is None
    assert tr.duration_sec == 0.0
    p = AudioPlayer(device_probe=lambda sr: True)
    assert p.load(fx["bad"]) is False, "装载坏文件应返回 False"
    assert p.available is True and p.loaded is False
    print("  ✓ 坏文件不炸：ok=False / error=%r，load() 返回 False" % (tr.error,))


def test_load_is_async(fx):
    """P2：`load()` 必须**后台解码**，发起即返回（绝不阻塞 GUI 线程）。"""
    # 先量一遍"整曲同步解码"要多久，作为对照
    t0 = time.monotonic()
    AudioTrack(fx["long"])
    decode_dt = time.monotonic() - t0
    p = AudioPlayer(device_probe=lambda sr: True)
    t0 = time.monotonic()
    accepted = p.load(fx["long"])
    load_dt = time.monotonic() - t0
    assert accepted is True
    print("    120s wav：整曲同步解码 %.3fs，load() 发起仅用 %.4fs" % (decode_dt, load_dt))
    assert load_dt < 0.2, "load() 必须非阻塞（发起即返回），实际 %.3fs" % load_dt
    assert load_dt < decode_dt, "load() 应比整曲解码快得多（证明解码在后台）"
    # 解码中：play() 必须返回 False（未就绪），不崩
    if p.loading:
        assert p.play(0.0) is False, "解码未就绪时 play() 应返回 False"
    assert p.wait_loaded(timeout=15.0), "后台解码最终应就绪"
    assert p.loaded and not p.loading
    assert 115.0 < p.duration_sec < 125.0, p.duration_sec
    print("    解码完成：loaded=%s duration=%.1fs" % (p.loaded, p.duration_sec))
    p.close()
    print("  ✓ 后台解码：load() 发起即返回（%.4fs << 解码 %.3fs），未就绪 play()=False" % (load_dt, decode_dt))


# ==========================================================================
# 2) 位置语义（A5b）：位置 = 已交声卡帧号 / 采样率
# ==========================================================================
def test_position_is_frames_delivered(fx):
    sink = GateSink()
    p = AudioPlayer(sink_factory=lambda sr, ch: sink, device_probe=lambda sr: True)
    assert p.load(fx["wav"]) is True
    assert p.wait_loaded(3.0), "音频应解码就绪"
    assert p.is_playing() is False

    assert p.play(0.0) is True
    assert _wait_until(lambda: p.is_playing()), "起播后线程应存活"
    # 还没喂任何一块 → 位置必须停在 0（挂钟即使过去也不动）
    time.sleep(0.12)
    assert sink.blocks == 0, "测试前置：sink 还没收到块"
    assert p.position_sec() == 0.0, "未喂帧时位置必须为 0（绝不用挂钟推算）"

    sr = p.samplerate
    # 喂第 1 块
    sink.gate.release()
    assert _wait_until(lambda: sink.blocks == 1), "应收到第 1 块"
    pos1 = p.position_sec()
    assert abs(pos1 - sink.frames / sr) < 1e-9, \
        "位置必须 = 已交声卡帧号/采样率：pos=%.6f 期望=%.6f" % (pos1, sink.frames / sr)
    assert pos1 >= ap.BLOCK_SEC - 1e-6, "首块应 ≥ 0.25s（A5c），实际 %.3fs" % pos1
    print("    喂 1 块：sink.frames=%d → position=%.4fs = %d/%d"
          % (sink.frames, pos1, sink.frames, sr))

    # ★ 不喂就不前进：等一会儿，位置一分不动（证明与挂钟无关）
    n_before = sink.blocks
    time.sleep(0.2)
    assert sink.blocks == n_before, "没喂 permit，sink 不该再收块"
    assert p.position_sec() == pos1, "没喂帧时位置不该前进"

    # 再喂一块 → 前进
    sink.gate.release()
    assert _wait_until(lambda: sink.blocks == 2), "应收到第 2 块"
    pos2 = p.position_sec()
    assert abs(pos2 - sink.frames / sr) < 1e-9
    assert pos2 > pos1, "喂了帧就该前进"
    print("    喂 2 块：sink.frames=%d → position=%.4fs（前进 %.4fs）"
          % (sink.frames, pos2, pos2 - pos1))

    # 跳到任意位置起播：position 立即反映"待喂的起点"（第 0 块还没交出去时仍是新起点）
    p.stop()
    assert p.play(0.5) is True
    assert _wait_until(lambda: p.is_playing())
    assert abs(p.position_sec() - 0.5) < 1e-9, "从 0.5s 起播，位置应为 0.5s"
    p.stop()
    assert p.is_playing() is False
    print("  ✓ 位置语义：position=已交声卡帧号/采样率；不喂不前进、喂了才前进、可按起点定位")


def test_block_ge_0p25s():
    assert ap.BLOCK_SEC >= 0.25, "块大小必须 ≥ 0.25s（A5c），实际 %.3f" % ap.BLOCK_SEC
    print("  ✓ 块大小：BLOCK_SEC=%.2fs ≥ 0.25s（旧实现 1024 帧≈21ms 是坑）" % ap.BLOCK_SEC)


# ==========================================================================
# 3) stop() 不卡（A6）：声卡卡住也 ≤0.8s 返回
# ==========================================================================
def test_stop_is_not_blocking(fx):
    sink = StuckSink()
    p = AudioPlayer(sink_factory=lambda sr, ch: sink, device_probe=lambda sr: True)
    assert p.load(fx["wav"]) is True
    assert p.wait_loaded(3.0), "音频应解码就绪"
    assert p.play(0.0) is True
    assert _wait_until(lambda: sink.entered.is_set(), timeout=1.0), "应已进入 sink.write（卡住）"
    t0 = time.monotonic()
    p.stop()
    dt = time.monotonic() - t0
    print("    sink.write 卡住时 stop() 用时 %.4fs（预算 ≤0.8s，join 超时 %.2fs）"
          % (dt, ap.STOP_TIMEOUT_SEC))
    assert dt <= 0.8, "stop() 必须在 0.8s 内返回，实际 %.3fs" % dt
    assert p.is_playing() is False
    sink.release.set()                       # 放行卡住的线程，收尾
    time.sleep(0.1)
    # 无设备时 stop() 也应立即返回
    p2 = AudioPlayer(device_probe=lambda sr: False)
    t0 = time.monotonic()
    p2.stop()
    assert time.monotonic() - t0 < 0.1, "未播放时 stop() 应立即返回"
    print("  ✓ stop() 不卡：卡死的声卡下 %.4fs 返回；未播放时立即返回" % dt)


# ==========================================================================
# 4) 无设备不崩：available=False 时所有方法可安全调用
# ==========================================================================
def test_no_device_graceful(fx):
    p = AudioPlayer(device_probe=lambda sr: False)
    assert p.available is False
    # 装载与设备无关（解码不需要声卡）
    assert p.load(fx["wav"]) is True
    assert p.wait_loaded(3.0), "音频应解码就绪（无设备也能解码）"
    assert p.available is False
    assert p.play(0.0) is False, "无设备时 play() 必须返回 False（窗口据此退回挂钟）"
    assert p.is_playing() is False
    assert p.position_sec() == 0.0
    assert p.duration_sec > 0.0                       # 已装载 → 时长与设备无关
    # 其余方法全部可安全调用、不抛
    p.pause()
    p.stop()
    p.close()
    # load/close 后再调一遍也不炸
    assert p.load(fx["mp3"]) is True
    p.close()
    assert p.loaded is False
    assert p.duration_sec == 0.0                       # close 后已释放
    print("  ✓ 无设备优雅降级：available=False，play()=False，全方法可安全调用、不崩")


# ==========================================================================
# 5) 设备打开失败 → 优雅降级 + 可恢复（C1）
# ==========================================================================
def test_device_open_failure(fx):
    def boom(sr, ch):
        raise RuntimeError("模拟打开声卡失败")

    p = AudioPlayer(sink_factory=boom, device_probe=lambda sr: True)
    assert p.load(fx["wav"]) is True
    assert p.wait_loaded(3.0), "音频应解码就绪"
    assert p.available is True                       # 探测说有设备
    # ★ C1 修复：打开失败时 play() **同步返回 False**（而不是旧实现先返回 True、再异步静音）。
    #   打开仍发生在专用线程里，play() 只**有界等待**其结果。
    assert p.play(0.0) is False, "打开失败时 play() 应同步返回 False（C1）"
    assert p.available is False, "打开失败后 available 应变 False（进入退避冷却）"
    assert p.is_playing() is False
    assert "打开音频输出设备失败" in p.error
    # ★ C1：一次失败**不得永久静音** —— 退避冷却期一过 available 应自动恢复可重试。
    p._next_open_ts = 0.0
    assert p.available is True, "退避冷却过后 available 应自动恢复（不再永久不可用）"
    # 失败后一切照常安全
    p.stop()
    p.close()
    print("  ✓ 设备打开失败：play()→False、available→False(可恢复)、is_playing→False、"
          "不崩（error=%r）" % p.error)


# ==========================================================================
# 6) 装载失败 / 未装载时的 play 安全性
# ==========================================================================
def test_play_without_load():
    p = AudioPlayer(device_probe=lambda sr: True)
    assert p.play(0.0) is False, "未装载时 play() 应返回 False"
    assert p.position_sec() == 0.0
    assert p.is_playing() is False
    p.close()
    print("  ✓ 未装载 play() 返回 False，不崩")


# ==========================================================================
def main() -> int:
    fx = _fixtures()
    dev = ap._probe_output_device()
    print("=" * 72)
    print("M4 · 音频走带测试  (python %s)" % sys.version.split()[0])
    print("夹具目录: %s" % TMP)
    print("本机输出设备探测: available=%s  sounddevice=%s"
          % (dev, getattr(__import__("sounddevice"), "__version__", "?")))
    print("=" * 72)
    tests = [
        ("解码 wav/mp3", test_decode, True),
        ("坏文件不炸", test_bad_file_is_graceful, True),
        ("解码后台化（load 非阻塞）", test_load_is_async, True),
        ("位置语义=已交声卡帧号", test_position_is_frames_delivered, True),
        ("块大小 ≥0.25s", test_block_ge_0p25s, False),
        ("stop() 不卡 ≤0.8s", test_stop_is_not_blocking, True),
        ("无设备不崩", test_no_device_graceful, True),
        ("设备打开失败降级", test_device_open_failure, True),
        ("未装载 play 安全", test_play_without_load, False),
    ]
    ok_all = True
    for name, fn, needs_fx in tests:
        print("\n--- %s ---" % name)
        try:
            fn(fx) if needs_fx else fn()
        except Exception as e:                                      # noqa: BLE001
            ok_all = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 72)
    print("汇总：%s" % ("全部通过 ✓" if ok_all else "有失败 ✗"))
    print("=" * 72)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
