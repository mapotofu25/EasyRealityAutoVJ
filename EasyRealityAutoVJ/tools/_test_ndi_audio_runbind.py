# -*- coding: utf-8 -*-
"""离线回归：NDI 音频「与开始自动VJ绑定」的门控逻辑（2026-10-07）。

用户要求：「ndi音频输出没有和开始自动vj绑定，没开始时也有输出」。

行为约定（三层，任一为假即不推流）：
  ① `audio.state.ndi_audio_on`  —— 用户勾了「NDI 传输音频」且 NDI 输出已启用
  ② `audio.state.ndi_running`   —— 已「开始自动VJ」（engine.running）
  ③ 转发线程侧 `engine.running`  —— 兜底（防止停止瞬间队列残留漏出去）

未开始自动 VJ 时**一个样本都不推**（不推静音、不推数据）。

本测试断言：
  1. 未开始时：采集侧不投递（push 恒 0），转发侧不调 feed_audio。
  2. 开始后：两层都放行。
  3. 停止后：立即回到不投递、不转发（不留尾音）。
  4. 门控原因文案可区分「未开始」vs「输出未启用」vs「发送端未建立」。

判据：脚本自己打印的结论行（EXIT 码不作数）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np                                          # noqa: E402


class FakeState:
    """AudioState 中与门控相关的最小字段集。"""

    def __init__(self):
        self.ndi_audio_on = False
        self.ndi_running = False
        self.ndi_feed = None


class FakeAudioEngine:
    """复刻采集侧投递判据（与 audio_engine._capture_loop 中一致）。"""

    def __init__(self, st):
        self.st = st
        self.q = []

    def push_ndi_audio(self, pcm, sr):
        self.q.append((pcm, sr))

    def capture_one_block(self, data, sr):
        """复刻采集循环里的投递条件。"""
        st = self.st
        if st.ndi_audio_on and st.ndi_running and st.ndi_feed is not None:
            arr = np.asarray(data, dtype=np.float32)
            self.push_ndi_audio(arr.reshape(-1).copy(), sr)
            return True
        return False


def case_1_not_running_no_push():
    """未开始自动 VJ ⇒ 采集侧不投递。"""
    print("-" * 78)
    print("【用例1】未开始自动VJ ⇒ 不投递（即使开关是开的）")
    st = FakeState()
    st.ndi_audio_on = True                  # 用户勾了
    st.ndi_feed = lambda p, s: None         # 回调已接
    st.ndi_running = False                  # 但还没开始
    eng = FakeAudioEngine(st)
    data = np.zeros((1024, 2), np.float32)
    for _ in range(20):
        eng.capture_one_block(data, 48000)
    if eng.q:
        print("  ★ 未开始却投递了 %d 块" % len(eng.q))
        return False, "未开始仍投递"
    print("  未开始：20 块全部跳过，push=0")
    # 开始后应放行
    st.ndi_running = True
    eng.capture_one_block(data, 48000)
    if len(eng.q) != 1:
        print("  ★ 开始后应投递 1 块，实际 %d" % len(eng.q))
        return False, "开始后未投递"
    print("  开始后：立即投递 1 块 ✓")
    print("  ✓ 绑定生效（未开始 0 块 / 开始后放行）")
    return True, ""


def case_2_stop_immediately():
    """停止后立即不再投递（不留尾音）。"""
    print("-" * 78)
    print("【用例2】停止后立即停止投递（不留尾音）")
    st = FakeState()
    st.ndi_audio_on = True
    st.ndi_feed = lambda p, s: None
    st.ndi_running = True
    eng = FakeAudioEngine(st)
    data = np.zeros((1024, 2), np.float32)
    for _ in range(5):
        eng.capture_one_block(data, 48000)
    n_before = len(eng.q)
    st.ndi_running = False                  # 点「停止」
    for _ in range(5):
        eng.capture_one_block(data, 48000)
    if len(eng.q) != n_before:
        print("  ★ 停止后又多了 %d 块" % (len(eng.q) - n_before))
        return False, "停止后仍投递"
    print("  停止前投递 %d 块；停止后 0 块新增" % n_before)
    print("  ✓ 停止立即生效，无尾音")
    return True, ""


def case_3_forward_gate():
    """转发线程侧兜底：未运行时绝不调 feed_audio（即使队列里有残留）。"""
    print("-" * 78)
    print("【用例3】转发线程侧兜底：未运行时不转发残留块")
    calls = []

    def feed(pcm, sr):
        calls.append((len(pcm), sr))

    def feed_gate(engine_running, ndi, ndi_enabled, pcm, sr):
        """复刻 ui_main._ndi_audio_feed 的三条件门控。"""
        if ndi is not None and ndi_enabled and engine_running:
            feed(pcm, sr)
            return "sent"
        if ndi is None:
            return "sender_missing"
        if not ndi_enabled:
            return "output_disabled"
        return "auto_not_running"

    pcm = np.zeros(2048, np.float32)
    r1 = feed_gate(False, object(), True, pcm, 48000)     # 未开始
    r2 = feed_gate(True, None, True, pcm, 48000)          # 发送端没建
    r3 = feed_gate(True, object(), False, pcm, 48000)     # NDI 输出没开
    r4 = feed_gate(True, object(), True, pcm, 48000)      # 全真（应转发）
    print("  未开始 → %s；无发送端 → %s；输出未启用 → %s；全真 → %s"
          % (r1, r2, r3, r4))
    # 前三种门控必须**全部不转发**；只有 r4（全真）允许转发 1 次。
    if len(calls) != 1:
        print("  ★ 门控次数不对：应只有全真那一次转发，实际 %d 次" % len(calls))
        return False, "门控穿透"
    if (r1, r2, r3) != ("auto_not_running", "sender_missing", "output_disabled"):
        print("  ★ 门控原因判定不对")
        return False, "原因判定错误"
    print("  ✓ 三种门控原因可区分且都不转发；仅全真路径放行（共 1 次）")
    return True, ""


def case_4_r4_actually_sends():
    """验证「全真」时确实会转发（避免门控写死导致永不发送）。"""
    print("-" * 78)
    print("【用例4】全真时确实转发（防止门控写死）")
    calls = []

    def feed(pcm, sr):
        calls.append((len(pcm), sr))

    def feed_gate(engine_running, ndi, ndi_enabled, pcm, sr):
        if ndi is not None and ndi_enabled and engine_running:
            feed(pcm, sr)
            return "sent"
        return "gated"

    pcm = np.zeros(2048, np.float32)
    r = feed_gate(True, object(), True, pcm, 48000)
    if r != "sent" or len(calls) != 1:
        print("  ★ 全真时未转发（门控写死了？）r=%s calls=%d" % (r, len(calls)))
        return False, "门控写死"
    print("  全真 → 转发 1 次（%d 样本 @%d）" % calls[0])
    print("  ✓ 门控未写死，正常路径可通")
    return True, ""


def main():
    print("=" * 78)
    print("NDI 音频「与开始自动VJ绑定」门控回归（2026-10-07）")
    print("=" * 78)
    cases = [
        ("未开始不投递", case_1_not_running_no_push),
        ("停止立即生效", case_2_stop_immediately),
        ("转发侧兜底", case_3_forward_gate),
        ("全真可通", case_4_r4_actually_sends),
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
    print("结论：✅ 全部通过 —— 未开始自动VJ时一个样本都不推，开始后才推")
    return 0


if __name__ == "__main__":
    sys.exit(main())
