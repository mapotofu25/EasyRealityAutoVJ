# -*- coding: utf-8 -*-
"""QA 独立验证（Edward）—— 用**真实** time.sleep 驱动，不 mock perf_counter。

对照 `tools/_test_silent_debounce.py`（engineer 自测，monkeypatch 掉 perf_counter）：
本脚本刻意**不**替换 perf_counter，让 dt 由真实时钟决定，从外部可观测行为证伪/证实。

用法：venv/Scripts/python.exe -u tools/_qa_silent_realtime.py
（不建 MainWindow ⇒ 不触发 NDI/ASIO DLL 卸载。判据只看下面的 OK/FAIL 行。）
"""
import os
import sys
import time
import tempfile
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine as E  # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  OK  " if ok else "  FAIL") + " " + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def make_engine():
    """最小 AutoVJEngine：不建 Qt 窗口、不建播放器，只手工初始化防抖字段。"""
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    eng.audio = None
    eng.layers = []
    eng.running = False
    eng.freeze = False
    # —— 与 commit 3edc4e9 engine.py:573-580 __init__ 里新增的 5 个字段一致 ——
    eng._silent_debounce_on = False
    eng._silent_since = 0.0
    eng._loud_since = 0.0
    eng._silent_last_t = time.perf_counter()
    eng._silent_now_debounced = False
    return eng


def snap_silent():
    return {"silent_sec": 5.0, "level": 0.5}


def snap_loud():
    return {"silent_sec": 0.0, "level": 0.5}


def drive(eng, snap, secs, tick=0.016):
    """真实时间：每 tick 秒调一次 _silent_hide_tick，共 secs 秒。真实 sleep。

    返回本段内 `_silent_now_debounced` 被观测到的取值序列（去重连续重复）。
    """
    seen = []
    t_end = time.perf_counter() + secs
    while time.perf_counter() < t_end:
        eng._silent_hide_tick(snap)
        v = eng._silent_now_debounced
        if not seen or seen[-1] != v:
            seen.append(v)
        time.sleep(tick)
    return seen


def drive_until_state(eng, snap, want, timeout=2.0, tick=0.005):
    """持续喂 snap，直到 `_silent_now_debounced is want`；返回耗时（秒）或 None。

    真实时间。为避免"上一段 drive 的尾部 dt 被算进本段"，先对齐 `_silent_last_t`
    到 t0，再开始计时 —— 否则首个 tick 的 dt 会把段前空档算进累积，导致测到 0.485s 这类
    偏小值（是测量工件，非源码问题）。
    """
    t0 = time.perf_counter()
    eng._silent_last_t = t0        # 对齐：本段计时从 t0 起
    while time.perf_counter() - t0 < timeout:
        eng._silent_hide_tick(snap)
        if eng._silent_now_debounced is want:
            return time.perf_counter() - t0
        time.sleep(tick)
    return None


def main():
    d = tempfile.mkdtemp(prefix="qa_silent_rt_")
    try:
        print("=== 需求 A：真实时间驱动 _silent_hide_tick ===")

        # ---- 1) 静音 0.2s 后恢复有声 ⇒ 全程 False（改之前这里会变 True = 闪烁） ----
        eng = make_engine()
        seq = []
        seq += drive(eng, snap_loud(), 0.70)     # 先建立有声基线
        seq += drive(eng, snap_silent(), 0.20)   # 静音仅 0.2s（< 0.5s）
        seq += drive(eng, snap_loud(), 0.30)     # 恢复
        check("R1 静音0.2s不闪：全程 _silent_now_debounced 恒为 False",
              all(v is False for v in seq), "观测序列=%s" % seq)

        # ---- 2) 单帧毛刺：静音只维持 1 个 tick(~16ms) ⇒ 不隐藏 ----
        eng = make_engine()
        drive(eng, snap_loud(), 0.70)            # 基线
        eng._silent_hide_tick(snap_silent())     # 仅一帧
        time.sleep(0.016)
        glitch_val = eng._silent_now_debounced
        drive(eng, snap_loud(), 0.30)
        check("R3a 单帧毛刺(1 tick)不隐藏", glitch_val is False, "毛刺后=%s" % glitch_val)

        # ---- 3) 抖动序列：静音/有声交替，每段 0.4s(<0.5s)，10 轮 ⇒ 状态全程不变 ----
        eng = make_engine()
        drive(eng, snap_loud(), 0.60)            # 稳定基线 False
        flip = None
        for _ in range(10):
            s1 = drive(eng, snap_silent(), 0.40)
            s2 = drive(eng, snap_loud(), 0.40)
            if any(v is not False for v in s1 + s2):
                flip = (s1, s2)
                break
        check("R3b 抖动(每段0.4s×10轮)全程不翻转", flip is None,
              ("翻转于 %s" % (flip,)) if flip else "")

        # ---- 4) 刚好 0.5s：翻转不早于 0.5s（边界 >= 语义） ----
        eng = make_engine()
        drive(eng, snap_loud(), 0.60)            # 基线 False
        flipped_at = drive_until_state(eng, snap_silent(), True, timeout=1.5, tick=0.005)
        check("R4 持续静音：翻转发生且不早于 0.5s",
              flipped_at is not None and flipped_at >= 0.5 - 0.005,
              "翻转耗时=%.3fs" % (flipped_at if flipped_at is not None else -1))

        # ---- 5) 长静音 30s 后恢复 ⇒ 恢复仍需新的 0.5s（不被 dt 瞬间跨过） ----
        #     真实时间里不能真睡 30s；用一次真实间隔 + 一次人为长停顿两段验证：
        #     a) 静音到稳定 True；
        #     b) 恢复有声 0.2s 仍为 True（<0.5s 不解除）；
        #     c) 继续到 >=0.5s 才 False —— 说明解除由**连续累积**决定。
        eng = make_engine()
        drive(eng, snap_loud(), 0.60)
        drive(eng, snap_silent(), 0.80)          # 稳定静音 True
        was_true = eng._silent_now_debounced
        loud_a = eng._loud_since
        drive(eng, snap_loud(), 0.20)            # 恢复仅 0.2s
        still_true = eng._silent_now_debounced
        loud_after_short = eng._loud_since
        # 现在要从 True 翻到 False；对齐测量起点后继续喂有声
        eng._silent_last_t = time.perf_counter()
        flip2 = drive_until_state(eng, snap_loud(), False, timeout=1.0, tick=0.005)
        final = eng._silent_now_debounced
        total2 = None if flip2 is None else 0.20 + flip2
        # 容差 0.03s 吸收 tick 粒度/循环开销（tick=16ms，测量在下一个 tick 边界）
        check("R3d 长静音后恢复仍需连续≥0.5s 才显示",
              was_true is True and still_true is True and final is False
              and total2 is not None and total2 >= 0.5 - 0.03,
              "静音后=%s 0.2s有声时=%s(loud_since=%.3f) 累计有声到显示=%s"
              % (was_true, still_true, loud_after_short,
                 None if total2 is None else "%.3fs" % total2))

        # ---- 6) freeze 语义：freeze=True 时 _tick_locked 早退 ⇒ 防抖不推进 ----
        #     _tick_locked 需要 audio.snapshot()，用一个最小替身。
        class _FakeAudio:
            def snapshot(self):
                return snap_silent()

        eng = make_engine()
        eng.audio = _FakeAudio()
        # 手工再补几处 _tick_locked 会碰到的属性会被早退挡掉（freeze=True 时 return 在最前）
        eng._silent_last_t = time.perf_counter()
        # 冻结点：记录 freeze 前状态
        eng.freeze = True
        before = (eng._silent_since, eng._silent_now_debounced)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.80:   # freeze 期间反复 tick
            try:
                eng._tick_locked()
            except Exception as ex:              # freeze 早退若被绕过会抛错，暴露问题
                print("     (freeze 期间 _tick_locked 抛异常: %r)" % (ex,))
                break
            time.sleep(0.02)
        after = (eng._silent_since, eng._silent_now_debounced)
        check("R5 freeze=True 时防抖不推进（_silent_since 与 _silent_now 不变）",
              before == after, "before=%s after=%s" % (before, after))

        # ---- 7) level < 0.03 支路也走防抖（不是只测 silent_sec 那条） ----
        eng = make_engine()
        drive(eng, snap_loud(), 0.60)            # 基线
        low_level = {"silent_sec": 0.0, "level": 0.001}   # silent_sec 小、level 低
        drive(eng, low_level, 0.20)              # 低电平仅 0.2s
        low_short = eng._silent_now_debounced
        flip3 = drive_until_state(eng, low_level, True, timeout=1.0, tick=0.005)
        # 累计静音时长 = 先前的 0.2s + flip3（flip3 已从对齐点起算）
        total3 = None if flip3 is None else 0.20 + flip3
        check("R6 level<0.03 支路：0.2s 不隐藏、连续≥0.5s 才隐藏",
              low_short is False and total3 is not None and total3 >= 0.5 - 0.03,
              "0.2s时=%s 累计静音到隐藏=%s"
              % (low_short, None if total3 is None else "%.3fs" % total3))

        print("")
        print("结论：" + ("ALL PASS" if not FAILS else "FAIL %d 项：%s" % (len(FAILS), FAILS)))
        return 0 if not FAILS else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
