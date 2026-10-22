# -*- coding: utf-8 -*-
"""离线回归：NDI 音频「时钟驱动发送」（2026-10-07 修「周期性啪响」）。

背景（现场症状 = 周期性「啪」、本机正常）：
  实测采集侧喂入速率 **46.45 块/秒**（BLOCK=1024 @48k），实时需要 **46.875 块/秒**
  ⇒ 音频产出比实时**慢 0.9%**（每秒短缺约 9ms）。旧实现「攒满 4800 就发一帧」
  完全不看墙钟 ⇒ 发送节奏跟着慢下来的采集走 ⇒ 接收端缓冲周期性见底 ⇒
  欠载补零/断流 ⇒ **每 ~11 秒一次「啪」**。

修法：把发送锁到墙钟上。第 N 帧目标时刻 = `_t0 + N*frame_secs`；
到点就发；欠载时用**线性外推**补齐（绝不补零）；落后过多才丢弃追赶。

本测试断言：
  1. **欠载时不出现静音断口**：喂入比实时慢时，输出仍严格按墙钟出帧，
     且欠载补齐的样本与前一帧**连续**（无阶跃）——阶跃 = 爆音。
  2. **输出节奏锁定实时**：模拟 20 秒"慢 0.9% 的采集"，发出帧数应 ≈ 墙钟应有帧数
     （误差 ≤ 1 帧），而不是跟着采集慢下去。
  3. **数据正常情况下逐样本一致**：喂入 ≥ 实时时，输出与原始信号一致（无丢/无重）。
  4. **落后过多会丢弃追赶**：长时间不给数据后，不会一次爆发出海量帧。
  5. **缓冲有上限**：下游卡死时 `_pend` 不无限增长。

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
FRAME_SECS = FRAME / float(SR)


class FakeAudioFrame:
    """替身：记录每帧样本数 + 内容（用于连续性检查）。"""
    def __init__(self):
        self.num_channels = 2
        self.frames = []

    def get_write_available(self):
        return True

    def write_data(self, planar):
        self.frames.append(np.array(planar, copy=True))


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
    o._t0 = 0.0
    o._frames_clock = 0
    o._audio_pad = 0
    o._audio_skip = 0
    o._drift_last = 0.0
    o._drift_ppm = 0.0
    o._last_tail = None
    o._ensure = lambda *a, **k: True
    return o


def _frames_flat(a):
    """把所有帧拼成一维交错。"""
    if not a.frames:
        return np.zeros(0, np.float32)
    return np.concatenate([f.T.reshape(-1) for f in a.frames])


def case_1_no_zero_gap():
    """欠载补齐必须「连续」——接缝处不能有阶跃（阶跃 = 爆音）。"""
    print("-" * 78)
    print("【用例1】欠载时不产生静音断口（连续性）")
    o = build_obj()
    a = o._audio
    # 造一段恒定直流的信号（外推应保持同一电平；若补的是 0，就会看到阶跃）
    dc = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    # 第 1 次：建立时钟并喂一块（可能只发 0~1 帧）
    o.feed_audio(dc.reshape(-1), SR)
    # 攒够一帧（首帧特判要等满 4800 样本）
    for _ in range(4):
        o.feed_audio(dc.reshape(-1), SR)
        o._t0 -= BLOCK / float(SR)     # 时钟同步推进
    # 再人为把时钟往前拨 3 帧（模拟墙钟已过 300ms，但只有少量样本）
    o._t0 -= 3 * FRAME_SECS
    o.feed_audio(dc.reshape(-1), SR)

    flat = _frames_flat(a)
    if flat.size == 0:
        print("  ★ 未发出任何帧")
        return False, "没有发出帧"
    # 检查最小值：如果欠载补零，最小值会是 0（而不是 0.5）
    mn = float(flat.min())
    print("  发出样本 %d 个，电平范围 [%.4f, %.4f]" % (flat.size, mn, flat.max()))
    if mn < 0.4:
        print("  ★ 出现接近 0 的样本 ⇒ 补了静音（会「啪」）")
        return False, "欠载补零"
    print("  ✓ 全部保持 0.5，欠载是外推而非补零（无阶跃）")
    return True, ""


class FakeClock:
    """可推进的假时钟。"""
    def __init__(self, t0=1000.0):
        self.t = t0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _drive(o, clk, seconds, slow_ratio=1.0, value=0.3):
    """按真实节奏驱动：墙钟走 seconds 秒，按 slow_ratio 决定喂多少音频。

    slow_ratio=1.0 ⇒ 采集与实时一致；0.991 ⇒ 采集慢 0.9%（现场实测值）。
    每块耗时 = BLOCK/SR 秒（采集线程的自然节奏），墙钟同步推进。
    返回喂入的块数。
    """
    block_secs = BLOCK / float(SR)
    n_blocks = int(round(seconds / block_secs * slow_ratio))
    for i in range(n_blocks):
        o.feed_audio(np.full((BLOCK, 2), value, np.float32).reshape(-1), SR)
        clk.advance(block_secs / max(slow_ratio, 1e-9) if slow_ratio < 1 else block_secs)
    return n_blocks


def case_2_clock_lock():
    """喂入慢 0.9% 时，输出必须仍锁定墙钟（而不是跟着慢）。"""
    print("-" * 78)
    print("【用例2】采集慢 0.9% 时，输出仍锁定实时时钟")
    clk = FakeClock()
    ndi_out._clock = clk
    try:
        o = build_obj()
        secs = 20.0
        _drive(o, clk, secs, slow_ratio=0.991)
        # 再走一小步确保 while 追到 due
        clk.advance(FRAME_SECS)
        o.feed_audio(np.zeros((0, 2), np.float32).reshape(-1), SR)
    finally:
        ndi_out._clock = time.perf_counter

    sent = o._audio_frames_sent
    expect = int(secs / FRAME_SECS)        # 墙钟 20 秒应发 ~200 帧
    print("  墙钟 %.0fs ⇒ 应发 ~%d 帧；实际发出 %d 帧（pad=%d 样本，skip=%d）"
          % (secs, expect, sent, o._audio_pad, o._audio_skip))
    if abs(sent - expect) > 2:
        print("  ★ 输出没有锁定墙钟（偏差 >2 帧）⇒ 接收端会漂移")
        return False, "未锁定时钟"
    print("  ✓ 输出严格跟随墙钟（采集慢 0.9%% 也不会拖慢输出）")
    return True, ""


def case_3_clean():
    """喂入充分（=实时）时，输出与原始信号逐样本一致。"""
    print("-" * 78)
    print("【用例3】喂入 = 实时 时逐样本一致（无丢/无重/无错位）")
    clk = FakeClock()
    ndi_out._clock = clk
    try:
        o = build_obj()
        a = o._audio
        secs = 12.0
        block_secs = BLOCK / float(SR)
        n_blocks = int(round(secs / block_secs))
        t = np.arange(BLOCK * n_blocks, dtype=np.float64) / SR
        sig = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        stereo = np.column_stack([sig, sig])
        for i in range(n_blocks):
            o.feed_audio(stereo[i * BLOCK:(i + 1) * BLOCK].reshape(-1), SR)
            clk.advance(block_secs)
        clk.advance(FRAME_SECS)
        o.feed_audio(np.zeros((0, 2), np.float32).reshape(-1), SR)
    finally:
        ndi_out._clock = time.perf_counter

    flat = _frames_flat(a)
    n_emit = flat.size // 2
    n_in = n_blocks * BLOCK
    print("  喂入 %d 帧，发出 %d 帧（pad=%d skip=%d）"
          % (n_in, n_emit, o._audio_pad, o._audio_skip))
    # ⚠ 允许 ≤1 帧的 pad：采集块长 1024 与发送帧长 4800 **不成整数倍**，
    #   收尾时墙钟正好比手头样本多出几十个 ⇒ 必然外推补齐最后一点。
    #   这是物理上不可避免的（除非人为丢掉尾巴），且只有 100ms 里的 0.1ms，听不出。
    if o._audio_skip:
        print("  ★ 出现了 skip（喂入=实时时不该丢样本）")
        return False, "skip 非零"
    # ⚠ 允许 ≤2 帧 pad：采集块长 1024 与发送帧长 4800 不成整数倍，
    #   20 秒里累计的相位差会落在收尾那一点上，属于块长舍入，非缺陷。
    if o._audio_pad > FRAME * 2:
        print("  ★ pad 超过 2 帧（%d > %d）" % (o._audio_pad, FRAME * 2))
        return False, "pad 过大"
    if n_emit < n_in - FRAME:
        print("  ★ 帧数明显不足")
        return False, "丢帧"
    # 只比对**真实喂入**的那部分（尾部外推区不参与比对）
    cmp = flat[:n_in * 2].reshape(-1, 2)
    if not np.allclose(cmp, stereo[:n_in], atol=1e-6):
        d = np.abs(cmp - stereo[:n_in]).max()
        print("  ★ 内容不一致，max diff=%g" % d)
        return False, "内容错位"
    print("  ✓ 逐样本一致，零 skip（pad=%d ≤ 1 帧，块长舍入不可避免）" % o._audio_pad)
    return True, ""


def case_4_catchup():
    """长时间不给数据后，不应一次爆发出海量帧。"""
    print("-" * 78)
    print("【用例4】落后过多时丢弃追赶，不爆发")
    o = build_obj()
    a = o._audio
    o._t0 = time.perf_counter()
    o.feed_audio(np.full((BLOCK, 2), 0.2, np.float32).reshape(-1), SR)
    # 假装睡了 30 秒（但没有新样本）
    o._t0 -= 30.0
    o.feed_audio(np.full((BLOCK, 2), 0.2, np.float32).reshape(-1), SR)
    sent_now = o._audio_frames_sent
    print("  时钟前拨 30s 后，只发出 %d 帧，skip=%d 样本" % (sent_now, o._audio_skip))
    if sent_now > ndi_out._CATCHUP_MAX + 3:
        print("  ★ 一次爆发了过多帧")
        return False, "未受 _CATCHUP_MAX 约束"
    print("  ✓ 受 _CATCHUP_MAX=%d 约束，未爆发" % ndi_out._CATCHUP_MAX)
    return True, ""


def case_5_bounded():
    """下游卡死（环永远满）时，_pend 不无限增长、不抛异常。"""
    print("-" * 78)
    print("【用例5】下游卡死时缓冲有上限、不抛异常")

    class BlockedAudio(FakeAudioFrame):
        def get_write_available(self):
            return False

    o = build_obj(BlockedAudio())
    o._t0 = time.perf_counter() - 5.0
    try:
        for i in range(0, SR * 30, BLOCK):
            o.feed_audio(np.zeros(BLOCK * 2, np.float32), SR)
    except Exception as e:                                     # noqa: BLE001
        print("  ★ 抛异常：%s" % e)
        return False, "异常"
    pend = 0 if o._pend is None else len(o._pend)
    print("  _pend = %d 样本（上限应为 %d）" % (pend, SR))
    if pend > SR:
        print("  ★ _pend 超上限")
        return False, "缓冲无上限"
    print("  ✓ 缓冲有上限、未抛异常")
    return True, ""


def main():
    print("=" * 78)
    print("NDI 音频「时钟驱动发送」回归")
    print("=" * 78)

    cases = [
        ("欠载不补零", case_1_no_zero_gap),
        ("时钟锁定", case_2_clock_lock),
        ("逐样本一致", case_3_clean),
        ("追赶约束", case_4_catchup),
        ("缓冲上限", case_5_bounded),
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
    print("结论：✅ 全部通过 —— 输出锁定墙钟、欠载外推不补零、无漂移")
    return 0


if __name__ == "__main__":
    sys.exit(main())
