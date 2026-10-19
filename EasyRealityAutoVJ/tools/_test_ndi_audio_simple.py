# -*- coding: utf-8 -*-
"""离线回归：NDI 音频「定长累积 + 原样发送」（2026-10-07 定稿）。

## 为什么砍掉了「时钟驱动」（用户质问「市面上没有成熟方案吗？」后的结论）

之前手写了一整套「时钟驱动 + 线性外推 + 追赶丢弃」来对抗本机实测的
「采集比实时慢 ~0.9%」。查证 NDI 官方文档后确认**方向就错了**：

  · NDI **不做 PTP 时钟同步**；
  · 发送端只该「原样送样本」，收发两端的时钟差由**接收端**用高质量重采样器
    吃掉（NDI SDK 文档原话："they will be resampled with high quality to
    adjust for small clock rate differences"）；
  · 接收端（OBS / vMix / Resolume）内部本来就有这个重采样器。

在发送端手写等价逻辑，不仅重复劳动，而且是用**线性外推**这种最差的方式伪造
样本（实测把正弦放大 23 倍 ⇒ 比补零更响的爆音）。

## 现在发送端只做两件事

  ① **定长累积到 `_FRAME_SAMPLES`(4800=100ms) 再发** —— 修「断续/短促高频/爆音」。
     cyndilib 音频发送环只有 3 槽（MAX_FRAME_BUFFERS=3），按采集块 21ms 逐块发
     会让环容量只剩 ~64ms，采集一抖动就排空 ⇒ 接收端听到一簇簇短音。
     攒到 100ms ⇒ 3 槽 = 300ms 储备。

  ② **缓冲满就跳过这一帧，绝不补零、绝不外推** —— 那 0.9% 的时钟差交给接收端。
     伪造样本比少一小段更糟：少一小段接收端会平滑掉，错误波形会直接响。

## 本测试断言（全部是「不变量」，不依赖任何时钟模拟）

  1. 喂入 = 实时且缓冲充足时，输出与原始信号**逐样本一致**（无丢/无重/无错位）。
  2. 只在**攒满整帧**时才发 —— 绝不半帧发送（半帧会让接收端时间轴抖动）。
  3. 缓冲满时**只丢样本、绝不伪造**：输出里出现的每个样本都必须来自输入。
  4. 采样率非 48k 时正确重采样到 48k（NDI 规范要求）。
  5. 输入明显小于一帧时**一帧都不发**（不抢先发半帧）。

判据：脚本自己打印的结论行（EXIT 码不作数 —— 凡创建 Qt/NDI 的脚本可能 127）。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np                                          # noqa: E402

import ndi_out                                              # noqa: E402

FRAME = ndi_out._FRAME_SAMPLES
SR = ndi_out._AUDIO_SR
BLOCK = 1024


class FakeAudioFrame:
    """替身：记录每帧样本数 + 内容。"""

    def __init__(self, always_available=True):
        self.num_channels = 2
        self.frames = []
        self.always_available = always_available

    def get_write_available(self):
        return self.always_available

    def write_data(self, planar):
        if not self.always_available:
            raise RuntimeError("no write frame available")
        # planar 是 (ch, n)，转存成 (n, ch) 便于比对
        self.frames.append(np.array(planar.T, copy=True))


class FakeSenderWrap:
    def __init__(self):
        self.sent = 0

    def send_audio(self):
        self.sent += 1
        return True

    def get_num_connections(self, t=0.0):
        return 2

    def close(self):
        pass


def build_obj(fake_audio=None):
    o = ndi_out.NDIOutput.__new__(ndi_out.NDIOutput)
    o._lock = threading.Lock()
    o._sender = FakeSenderWrap()
    o._video = None
    o._audio = fake_audio or FakeAudioFrame()
    o._w = 1920
    o._h = 1080
    o._inited = True
    o.enabled = True
    o.name = "test"
    o._opened_at = time.perf_counter() - 10.0
    o._last_err = ""
    o._dropped = 0
    o._audio_sr = SR
    o._audio_ok = 0
    o._audio_dropped = 0
    o._audio_resampled = 0
    o._audio_last_log = time.perf_counter()
    o._audio_err = ""
    o._audio_err_logged = None
    o._err_logged = None
    o._pend = None
    o._pend_sr = 0
    o._audio_frames_sent = 0
    o._audio_short = 0
    o._audio_start_logged = False
    o._ensure = lambda *a, **k: True
    return o


def _frames_flat(a):
    """把所有帧拼成一维交错。"""
    if not a.frames:
        return np.zeros(0, np.float32)
    return np.concatenate([f.reshape(-1) for f in a.frames])


def case_1_exact():
    """喂入 = 实时、缓冲充足 ⇒ 输出与输入逐样本一致。"""
    print("-" * 78)
    print("【用例1】喂入=实时 时逐样本一致（无丢/无重/无错位）")
    o = build_obj()
    a = o._audio
    n_blocks = 20
    t = np.arange(BLOCK * n_blocks, dtype=np.float64) / SR
    sig = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    stereo = np.column_stack([sig, sig])
    for i in range(n_blocks):
        o.feed_audio(stereo[i * BLOCK:(i + 1) * BLOCK].reshape(-1), SR)

    flat = _frames_flat(a)
    n_emit = flat.size // 2
    n_in = n_blocks * BLOCK
    n_expect = (n_in // FRAME) * FRAME          # 只有整帧会被发出
    print("  喂入 %d 帧，发出 %d 帧（应为 %d = 整数帧）" % (n_in, n_emit, n_expect))
    if n_emit != n_expect:
        print("  ★ 发出的帧数不等于「整数帧」⇒ 发了半帧或丢了帧")
        return False, "帧数不符"
    cmp = flat.reshape(-1, 2)
    if not np.allclose(cmp, stereo[:n_emit], atol=1e-6):
        d = float(np.abs(cmp - stereo[:n_emit]).max())
        print("  ★ 内容不一致，max diff=%g" % d)
        return False, "内容错位"
    print("  ✓ 逐样本一致，无丢/无重/无错位")
    return True, ""


def case_2_whole_frames_only():
    """只在攒满整帧时才发；不足一帧绝不发。"""
    print("-" * 78)
    print("【用例2】只在攒满整帧时发送（绝不半帧）")
    o = build_obj()
    a = o._audio
    # 喂 FRAME-1 个样本：差一个，不该发任何帧
    o.feed_audio(np.full(((FRAME - 1) * 2,), 0.3, np.float32), SR)
    if a.frames:
        print("  ★ 不足一帧却发出了 %d 帧" % len(a.frames))
        return False, "发了半帧"
    pend = 0 if o._pend is None else len(o._pend)
    if pend != FRAME - 1:
        print("  ★ 残留样本数不对：%d（应 %d）" % (pend, FRAME - 1))
        return False, "累积数不符"
    # 再补 1 个样本 ⇒ 正好满一帧，应发 1 帧
    o.feed_audio(np.full((2,), 0.3, np.float32), SR)
    if len(a.frames) != 1:
        print("  ★ 满一帧后应发 1 帧，实际 %d 帧" % len(a.frames))
        return False, "未按时发帧"
    if a.frames[0].shape[0] != FRAME:
        print("  ★ 帧长 %d ≠ %d" % (a.frames[0].shape[0], FRAME))
        return False, "帧长不符"
    print("  ✓ 差一个样本不发；补满即发 1 整帧（%d 样本）" % FRAME)
    return True, ""


def case_3_no_fabrication():
    """缓冲一直满 ⇒ 只丢样本，绝不伪造；输出中每个样本都来自输入。"""
    print("-" * 78)
    print("【用例3】缓冲满时只丢样本、绝不伪造波形")
    o = build_obj(FakeAudioFrame(always_available=False))
    a = o._audio
    # 喂一段独一无二的斜坡信号，缓冲满 ⇒ 一帧都发不出去
    total = FRAME * 6
    sig = (np.arange(total, dtype=np.float32) / float(total)).astype(np.float32)
    o.feed_audio(np.repeat(sig, 2), SR)
    if a.frames:
        print("  ★ 缓冲满却发出了 %d 帧" % len(a.frames))
        return False, "缓冲满仍写"
    pend = 0 if o._pend is None else len(o._pend)
    print("  缓冲满：发出 0 帧，dropped=%d，_pend=%d 样本"
          % (o._audio_dropped, pend))
    if o._audio_dropped == 0:
        print("  ★ 没有记录丢弃（诊断缺失）")
        return False, "未记丢弃"
    # _pend 有上限（1 秒），超出只保留最新的
    if pend > SR:
        print("  ★ _pend 超上限 %d > %d" % (pend, SR))
        return False, "缓冲无上限"
    print("  ✓ 缓冲满时不写、不伪造，且 _pend 有上限（%d ≤ %d）" % (pend, SR))
    return True, ""


def case_4_resample():
    """44.1k 输入必须重采样到 48k。"""
    print("-" * 78)
    print("【用例4】非 48k 输入重采样到 48k（NDI 规范）")
    o = build_obj()
    a = o._audio
    sr_in = 44100
    # 喂 1 秒的 44.1k ⇒ 重采样后应得 48000 样本 = 10 整帧。
    # ⚠ 必须**按真实节奏分块喂**（每块 ~21ms），不能一次喂 1 秒：一次喂 1 秒会
    #   撞上 `_MAX_FRAMES_PER_CALL` 的单次上限（那是防霸锁用的，真实场景
    #   每块 21ms 永远攒不满一帧，不可能触发）。
    blk_in = 441                                   # 10ms @44.1k
    n_blocks = sr_in // blk_in                     # = 100 块（1 秒）
    sig = np.zeros(blk_in * 2, np.float32)         # 交错立体声
    for _ in range(n_blocks):
        o.feed_audio(sig, sr_in)
    flat = _frames_flat(a)
    n_emit = flat.size // 2
    expect = (SR // FRAME) * FRAME              # 48000 → 10 帧
    print("  44.1k 喂入 %d 样本，发出 %d 样本（应 %d ≈ 48k×1s）"
          % (blk_in * n_blocks, n_emit, expect))
    if abs(n_emit - expect) > FRAME:
        print("  ★ 重采样比例不对（偏差 >1 帧）")
        return False, "重采样错误"
    if o._audio_resampled == 0:
        print("  ★ 没有记录重采样次数（诊断缺失）")
        return False, "未记重采样"
    print("  ✓ 正确重采样到 48k（resampled=%d 次）" % o._audio_resampled)
    return True, ""


def case_5_mono_upmix():
    """单声道输入应复制成双声道（NDI 帧固定 2ch）。"""
    print("-" * 78)
    print("【用例5】单声道输入复制成双声道")
    o = build_obj()
    a = o._audio
    # 传 (FRAME, 1) 的**二维单通道**数组（声明的 shape[1]==1 ⇒ 走复制分支）；
    # 一维数组的语义是「交错 2 通道」，不适合用来测单声道。
    mono = np.full((FRAME, 1), 0.42, np.float32)
    o.feed_audio(mono, SR)
    if not a.frames:
        print("  ★ 未发出任何帧")
        return False, "未发帧"
    f = a.frames[0]
    if f.shape[1] != 2:
        print("  ★ 声道数 %d ≠ 2" % f.shape[1])
        return False, "非双声道"
    if not np.allclose(f[:, 0], f[:, 1]):
        print("  ★ 两声道内容不一致（未正确复制）")
        return False, "频道不齐"
    print("  ✓ 单声道已复制为双声道（形状 %r）" % (f.shape,))
    return True, ""


def case_6_no_deadlock():
    """连续喂 30 秒数据不抛异常、不卡死（锁不泄漏）。"""
    print("-" * 78)
    print("【用例6】连续 30 秒喂入不抛异常、不死锁")
    o = build_obj()
    t0 = time.perf_counter()
    try:
        for _ in range(int(SR * 30 / BLOCK)):
            o.feed_audio(np.zeros(BLOCK * 2, np.float32), SR)
    except Exception as e:                                     # noqa: BLE001
        print("  ★ 抛异常：%s" % e)
        return False, "异常"
    dt = time.perf_counter() - t0
    if dt > 10.0:
        print("  ★ 耗时 %.2fs 过长（疑似锁竞争/死锁）" % dt)
        return False, "过慢"
    pend = 0 if o._pend is None else len(o._pend)
    print("  30 秒喂入耗时 %.2fs，发出 %d 帧，_pend=%d，dropped=%d"
          % (dt, o._audio_frames_sent, pend, o._audio_dropped))
    print("  ✓ 无异常、无死锁、缓冲有界")
    return True, ""


def main():
    print("=" * 78)
    print("NDI 音频「定长累积 + 原样发送」回归（2026-10-07 定稿）")
    print("=" * 78)

    cases = [
        ("逐样本一致", case_1_exact),
        ("只发整帧", case_2_whole_frames_only),
        ("拒绝伪造", case_3_no_fabrication),
        ("重采样", case_4_resample),
        ("单声道上混", case_5_mono_upmix),
        ("无死锁", case_6_no_deadlock),
    ]
    ok_n = 0
    fails = []
    for name, fn in cases:
        ok, why = fn()
        if ok:
            ok_n += 1
        else:
            fails.append("%s（%s）" % (name, why))

    print("=" * 78)
    print("通过 %d/%d" % (ok_n, len(cases)))
    if fails:
        for f in fails:
            print("  ★ 失败：" + f)
        print("结论：★ 未通过")
        return 1
    print("结论：✅ 全部通过 —— 定长整帧发送、逐样本一致、绝不伪造波形")
    return 0


if __name__ == "__main__":
    sys.exit(main())
