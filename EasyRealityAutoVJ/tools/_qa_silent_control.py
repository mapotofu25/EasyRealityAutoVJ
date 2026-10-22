# -*- coding: utf-8 -*-
"""QA 对照脚本（Edward）——在"改动前/改动后"两版源码上跑**同一套行为断言**。

关键点：两版都要能跑，所以只依赖**两版都存在**的公开面：
  · 改动后：`engine._silent_hide_tick(snap)` 推进，读 `engine._silent_now_debounced`
  · 改动前：无该方法 ⇒ 用**当时的行内判据** `silent_sec>0.25 or level<0.03` 逐帧直接算
    （这正是 commit 想消除的闪烁来源），放进同一个"每帧决策"回调里。

于是产出是**同一件事**："这一帧该图层是否被静音隐藏"。对照点：
  · 静音 0.2s 的瞬时间隙 → 改动前：瞬间变 True（闪一下）；改动后：恒 False。

用法：venv/Scripts/python.exe -u tools/_qa_silent_control.py
"""
import os
import sys
import time
import inspect

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine as E  # noqa: E402

HAS_DEBOUNCE = hasattr(E.AutoVJEngine, "_silent_hide_tick")


def make_engine():
    if HAS_DEBOUNCE:
        eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
        eng.layers = []
        eng.running = False
        eng.freeze = False
        eng._silent_debounce_on = False
        eng._silent_since = 0.0
        eng._loud_since = 0.0
        eng._silent_last_t = time.perf_counter()
        eng._silent_now_debounced = False
        return eng
    return E.AutoVJEngine.__new__(E.AutoVJEngine)


def silent_decision(eng, snap):
    """返回"这一帧该图层是否被静音隐藏"——两版语义一致的外部可观测判定。"""
    if HAS_DEBOUNCE:
        eng._silent_hide_tick(snap)
        return eng._silent_now_debounced
    # 改动前的行内判据（commit 描述里的原始表达式）
    return snap["silent_sec"] > 0.25 or snap["level"] < 0.03


def run_glitch_sequence(eng, seq, tick=0.016):
    """真实时间驱动：seq 是 (is_silent, 持续秒) 列表；返回每段末尾的判定 + 全程取值集合。"""
    vals = []
    for is_silent, secs in seq:
        snap = ({"silent_sec": 5.0, "level": 0.5} if is_silent
                else {"silent_sec": 0.0, "level": 0.5})
        t_end = time.perf_counter() + secs
        while time.perf_counter() < t_end:
            vals.append(silent_decision(eng, snap))
            time.sleep(tick)
    return vals


def main():
    tag = "改动后(SILENT_HIDE_DEBOUNCE存在)" if HAS_DEBOUNCE else "改动前(行内判据)"
    print("=== 对照测试：%s ===" % tag)
    ok = True

    # 场景1：稳定有声 0.7s → 瞬时静音 0.2s → 恢复有声 0.3s
    eng = make_engine()
    vals = run_glitch_sequence(eng, [(False, 0.7), (True, 0.2), (False, 0.3)])
    flicker = any(v is True for v in vals)
    print("  瞬时间隙(0.2s)期间出现过 True?  %s   values_set=%s"
          % (flicker, sorted(set(vals))))
    if HAS_DEBOUNCE:
        # 改动后：要求「全程 False」（不闪）
        expect_no_flicker = True
    else:
        # 改动前：预期就是会闪（True 出现）——用来证明"改动前的确有此 bug"
        expect_no_flicker = False
    if flicker == expect_no_flicker:
        ok = False
        print("  => 与预期不符")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
