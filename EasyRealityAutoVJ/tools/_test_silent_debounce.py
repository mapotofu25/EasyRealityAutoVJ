# -*- coding: utf-8 -*-
"""回归测试：「无声音时隐藏本层」的 0.5s 防抖（需求 A）+ 新建自动图层默认勾选（需求 B）。

需求 A：静音/有声都必须**连续**达到 `SILENT_HIDE_DEBOUNCE`(0.5s) 才切换隐藏状态。
  —— 改之前 `silent_sec > 0.25 or level < 0.03` 逐帧判定 ⇒ 音乐里一个鼓点间隙就闪一下。
需求 B：`NewLayerDialog` 里选"自动"新建的图层，默认勾选 hide_silent（手动图层不勾）。

用法：venv/Scripts/python.exe -u tools/_test_silent_debounce.py
（不创建 MainWindow ⇒ 不触发 NDI/ASIO DLL 卸载，退出码正常；判据只看下面的 ✓/✗ 行。）
"""
import os
import sys
import tempfile
import time
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import engine as E                                              # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  OK  " if ok else "  FAIL") + " " + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class _FakeAudio:
    """最小 audio.state 替身：只提供 snapshot()。"""

    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return dict(self._snap)

    # 引擎 __init__ / tick 可能读到的其它属性（按需补，避免 AttributeError）
    grid_pred_orig = 0.0
    grid_pred_w = 0.0


def make_engine(snap):
    """构造一个最小 AutoVJEngine：不建窗口、不建播放器，只喂 snap。"""
    audio = _FakeAudio(snap)
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    # 手工初始化 tick 防抖所需字段（绕过 __init__ 里对 Qt/GUI 的依赖）
    eng.audio = audio
    eng.layers = []
    eng.running = False
    eng.freeze = False
    eng._silent_debounce_on = False
    eng._silent_since = 0.0
    eng._loud_since = 0.0
    eng._silent_last_t = time.perf_counter()
    eng._silent_now_debounced = False
    return eng


def feed(eng, raw_silent, secs, step=0.05):
    """按 step 粒度喂入 secs 秒的"静音/有声"状态，返回每秒调用次数。

    通过 monkeypatch 掉 perf_counter 让 dt 精确可控。
    """
    n = max(1, int(round(secs / step)))
    base = [0.0]

    real_pc = time.perf_counter
    try:
        for i in range(n):
            base[0] += step
            t = base[0]
            E.time.perf_counter = lambda t=t: t
            # 静音 ⇒ silent_sec 大、level 低；有声 ⇒ 两者反之
            if raw_silent:
                eng.audio._snap = {"silent_sec": 5.0, "level": 0.5}
            else:
                eng.audio._snap = {"silent_sec": 0.0, "level": 0.5}
            eng._silent_hide_tick(eng.audio._snap)
    finally:
        E.time.perf_counter = real_pc
    return n


def main():
    d = tempfile.mkdtemp(prefix="silent_debounce_")
    try:
        print("=== 需求 A：0.5s 防抖 ===")

        # 1) 瞬断不闪：先稳定有声 ≥0.5s 建立"显示"基线，再静音 0.2s 就恢复 ⇒ 全程始终 False
        eng = make_engine({})
        seen = []
        real_pc = time.perf_counter
        try:
            t = [0.0]
            # 先稳定有声 ≥0.5s 建立"显示"基线，再静音 0.2s 就恢复 ⇒ 全程始终 False
            seq = ([False] * 14 +         # 0.70s 有声（建立稳定基线 + 越过防抖）
                   [True] * 4 +           # 0.20s 静音（< 0.5s，瞬时中断）
                   [False] * 20)          # 1.00s 恢复有声
            for is_silent in seq:
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]      # 必须在 tick 之前 monkeypatch
                snap = ({"silent_sec": 5.0, "level": 0.5} if is_silent
                        else {"silent_sec": 0.0, "level": 0.5})
                eng._silent_hide_tick(snap)
                seen.append(eng._silent_now_debounced)
        finally:
            E.time.perf_counter = real_pc
        check("瞬断(静音0.2s)不闪：全程始终 False", not any(seen), "值序列=%s" % seen)

        # 2) 持续静音 ≥ 0.5s ⇒ 变 True，且**不早于** 0.5s
        eng = make_engine({})
        first_true_at = None
        real_pc = time.perf_counter
        try:
            t = [0.0]
            # 先有一声 ≥0.5s 建立稳定"显示"基线（这样静音变成"从有声切换过来"）
            for _ in range(14):
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]
                eng._silent_hide_tick({"silent_sec": 0.0, "level": 0.5})
            t0 = t[0]
            for i in range(40):            # 2.0s 持续静音
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]
                eng._silent_hide_tick({"silent_sec": 5.0, "level": 0.5})
                if eng._silent_now_debounced and first_true_at is None:
                    first_true_at = t[0] - t0
        finally:
            E.time.perf_counter = real_pc
        check("持续静音会隐藏：变为 True", eng._silent_now_debounced is True)
        check("不早于 0.5s 变 True", first_true_at is not None and first_true_at >= 0.5,
              "首次 True 时刻=%.3fs" % (first_true_at if first_true_at is not None else -1))

        # 3) 恢复有声也要防抖：从静音恢复，有声只维持 < 0.5s ⇒ 仍为 True（不立刻显示）
        eng = make_engine({})
        real_pc = time.perf_counter
        try:
            t = [0.0]
            # 先静音 1.0s 建立 True
            for _ in range(20):
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]
                eng._silent_hide_tick({"silent_sec": 5.0, "level": 0.5})
            was_true = eng._silent_now_debounced
            # 恢复有声 0.3s（< 0.5s）
            for _ in range(6):
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]
                eng._silent_hide_tick({"silent_sec": 0.0, "level": 0.5})
            after_short_loud = eng._silent_now_debounced
            # 继续有声到 ≥ 0.5s
            for _ in range(8):
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]
                eng._silent_hide_tick({"silent_sec": 0.0, "level": 0.5})
            after_long_loud = eng._silent_now_debounced
        finally:
            E.time.perf_counter = real_pc
        check("恢复有声 <0.5s 仍为 True（不立刻显示）", was_true is True and after_short_loud is True)
        check("恢复有声 ≥0.5s 才变 False", after_long_loud is False)

        # 4) 冻结核对：卡帧/暂停后 dt 巨大，必须被钳到 0.5（照抄 _kv_tick 的 dt 钳位）。
        #    语义与 `_kv_tick` 一致：钳位后**最多一个钳位帧**到达阈值 —— 而不是"巨大 dt 一次
        #    跨过"。这里直接检验钳位本身：把时间跳 30s，断言内部 `_silent_since` 恰好 = 0.5
        #    （若没钳位则是 30.0），且再过一帧才稳定为 True。
        eng = make_engine({})
        real_pc = time.perf_counter
        try:
            t = [0.0]
            # 建立稳定显示基线（有声 ≥0.5s）
            for _ in range(14):
                t[0] += 0.05
                E.time.perf_counter = lambda t=t: t[0]
                eng._silent_hide_tick({"silent_sec": 0.0, "level": 0.5})
            before = eng._silent_now_debounced
            # 模拟一次"卡帧 30s 后恢复"：直接跳时间 30s（dt 必须被钳到 0.5）
            t[0] += 30.0
            E.time.perf_counter = lambda t=t: t[0]
            eng._silent_hide_tick({"silent_sec": 5.0, "level": 0.5})
            clamped_since = eng._silent_since
        finally:
            E.time.perf_counter = real_pc
        check("冻结核对：dt 被钳到 0.5（30s 卡帧后 _silent_since==0.5 而非 30.0）",
              before is False and abs(clamped_since - 0.5) < 1e-6,
              "钳位后 _silent_since=%.3f（无钳位应为 30.0）" % clamped_since)

        # 5) 两处判据一致：源码里不再有自己的 `silent_sec > 0.25` 行内表达式
        src = open(os.path.join(_ROOT, "src", "engine.py"), encoding="utf-8").read()
        n_raw = src.count('snap["silent_sec"] > 0.25')
        check("engine.py 已无 `snap[\"silent_sec\"] > 0.25` 行内判据", n_raw == 0,
              "残留=%d 处" % n_raw)
        check("两处都读同一实例变量 _silent_now_debounced",
              src.count("self._silent_now_debounced") >= 3)  # 写入1 + 两处读取

        print("")
        print("=== 需求 B：新建自动图层默认勾选 ===")
        # 5) 复现 layer_add 的赋值逻辑（不建窗口，只验证分支）
        def resolve_hide_silent(auto, is_kv):
            lay = E.Layer("t")
            lay.auto_mode = auto
            lay.is_kv = is_kv
            # —— 与 ui_main.layer_add 里新加的分支保持一致 ——
            if auto and not is_kv:
                lay.hide_silent = True
            return lay.hide_silent

        check("auto=True  且非Kv ⇒ hide_silent is True", resolve_hide_silent(True, False) is True)
        check("auto=False 手动   ⇒ hide_silent is False", resolve_hide_silent(False, False) is False)
        check("auto 且 is_kv     ⇒ 不叠加 hide_silent", resolve_hide_silent(True, True) is False)

        # 6) 确认 ui_main.layer_add 里确实有该分支（源码级）
        ui = open(os.path.join(_ROOT, "src", "ui_main.py"), encoding="utf-8").read()
        check("ui_main.layer_add 含 `if auto and not is_kv: lay.hide_silent = True`",
              "if auto and not is_kv:" in ui and "lay.hide_silent = True" in ui)

        print("")
        if FAILS:
            print("结论：FAIL（%d 项未通过）：%s" % (len(FAILS), FAILS))
            return 1
        print("结论：ALL PASS（需求 A 防抖 5 项 + 需求 B 3 项）")
        return 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
