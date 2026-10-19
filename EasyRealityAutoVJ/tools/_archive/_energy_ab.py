# -*- coding: utf-8 -*-
"""能量公式 A/B：在若干「已知样本」上比较不同参数，看改法的收益与代价。

背景（2026-09-28）：用户报 `Alcohol Duper -Hardstyle VIP-` 的 3:03~3:29 是劲爆 drop，
但能量只有 0.35~0.48。诊断发现主因是 **kick_e 的映射窗口极窄**：
    kick_e = (kick_rel - 0.48) / 0.30      ⇒ 只有 rel∈[0.48, 0.78] 有输出，
    全曲峰值 50% 的段落直接判 0（不是"不响"，而是"离本曲最猛的那段还差一半"）。

本脚本用**软件本体的同一个分析器**回放多个文件，对比：
    · 用户报的那段（应升）
    · 同曲更猛的段落（应保持高）
    · 已知必须保持低的段落（Black Warrior 钢琴 breakdown / 安静 intro）—— 防回归
用法：venv/Scripts/python.exe -u tools/_energy_ab.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import numpy as np                    # noqa: E402

TOFU = "W:/临时结界相关/2of/tofu"

# (标签, 文件, 起点秒, 终点秒, 期望)  期望: up=应明显升高 / keep=保持 / low=必须保持低
SECTIONS = [
    ("A 3:03~3:29  用户报的 drop", "Rolling Contact - Alcohol Duper -Hardstyle VIP-.flac", 183.0, 208.6, "up"),
    ("B 3:44~4:28  同曲最猛", "Rolling Contact - Alcohol Duper -Hardstyle VIP-.flac", 224.0, 268.0, "keep"),
    ("   同曲 0:00~0:20 安静 intro", "Rolling Contact - Alcohol Duper -Hardstyle VIP-.flac", 2.0, 20.0, "low"),
    ("BW 1:45~2:05 钢琴 breakdown", "Rolling Contact - Black Warrior (2025 Remaster).flac", 105.0, 125.0, "low"),
    ("BW 3:00~3:20 鼓点段(对照)", "Rolling Contact - Black Warrior (2025 Remaster).flac", 180.0, 200.0, "keep"),
    ("CS 1:00~1:30  常规段落", "Rolling Contact - Cosmic String.flac", 60.0, 90.0, "keep"),
]

# 参数集：name -> dict（键名 = audio_engine 里的模块常量）
OLD = {"EN_ABS_W": 0.0, "EN_VOC_MAX": 0.60, "EN_VOC_KNEE": 0.25,
       "EN_KICK_DEAD": 0.48, "EN_KICK_SLOPE": 0.30, "EN_BASS_DEAD": 0.45,
       "EN_BASS_SLOPE": 0.22, "EN_FLUCT_DEAD": 0.45, "EN_FLUCT_SLOPE": 0.22}


def _mix(d):
    """⚠ 基准必须是**代码当前的默认值**，不是 OLD —— 否则漏传的键会被 OLD 覆盖，
    导致不同方案跑出一样的结果（踩过一次：P4 与 P5 结果完全相同就是这个原因）。
    未传的键由 replay() 自动回落到模块当前值。"""
    return dict(d)


PARAMS = [
    # P0 = 本次改动之前的真实行为（把绝对项关掉、人声惩罚调回旧值）
    ("P0 改动前（无绝对项）", OLD),
    # P4 = 只加「绝对响度兜底项」，人声惩罚不动 —— 看这一项单独能拉多少
    ("P4 只加绝对响度", _mix({"EN_ABS_W": 1.0})),
    # P5 = 绝对响度 + 松人声（**当前代码默认值**）
    ("P5 绝对响度+松人声(新默认)", _mix({"EN_ABS_W": 1.0})),
    # P6 = 更激进，作为备选上界
    ("P6 更激进（窗口更宽/人声更松）",
     _mix({"EN_ABS_W": 1.0, "EN_ABS_LO_DB": -15.0, "EN_ABS_HI_DB": -5.0,
           "EN_VOC_MAX": 0.40, "EN_VOC_KNEE": 0.32})),
]
# P4 需要单独把 voc 拉回旧值（P5 用代码默认值）
PARAMS[1] = ("P4 只加绝对响度", _mix({"EN_ABS_W": 1.0, "EN_VOC_MAX": 0.60, "EN_VOC_KNEE": 0.25}))

KEYS = ("EN_KICK_DEAD", "EN_KICK_SLOPE", "EN_BASS_DEAD", "EN_BASS_SLOPE",
        "EN_FLUCT_DEAD", "EN_FLUCT_SLOPE", "EN_VOC_MAX", "EN_VOC_KNEE",
        "EN_ABS_W", "EN_ABS_LO_DB", "EN_ABS_HI_DB")


def replay(ae, path, overrides):
    import soundfile as sf
    from audio_engine import _Analyzer, AudioState, BLOCK
    saved = {k: getattr(ae, k) for k in KEYS}
    try:
        for k in KEYS:
            setattr(ae, k, overrides.get(k, saved[k]))
        real = ae.time

        class FC:
            t = 1e6

            @classmethod
            def perf_counter(cls):
                return cls.t

            @classmethod
            def time(cls):
                return 1.7e9 + cls.t

        data, sr = sf.read(path, always_2d=True, dtype="float32")
        mono = data.mean(axis=1)
        del data
        an, st = _Analyzer(sr), AudioState()
        ae.time = FC
        out = []
        try:
            n = len(mono) // BLOCK
            for i in range(n):
                FC.t += BLOCK / sr
                an.feed(mono[i * BLOCK:(i + 1) * BLOCK].reshape(-1, 1), st)
                out.append((i * BLOCK / sr, float(st.snapshot()["energy"])))
        finally:
            ae.time = real
        return np.array(out)
    finally:
        for k, v in saved.items():
            setattr(ae, k, v)


def main():
    import audio_engine as ae
    print("能量公式 A/B —— 同一分析器、同一文件，只改参数\n")
    cache = {}
    res = {}
    for pname, ov in PARAMS:
        res[pname] = {}
        for label, f, a, b, want in SECTIONS:
            path = os.path.join(TOFU, f)
            curve = cache.get(path)
            if curve is None:
                curve = cache[path] = None      # 占位，下面逐参数重放
            # 每个参数集都要重放（分析器内部状态要重来），但文件只读一次的话更快——
            # 这里为了口径干净仍然重放（速度可接受）
            c = replay(ae, path, ov)
            m = (c[:, 0] >= a) & (c[:, 0] <= b)
            seg = c[m, 1]
            res[pname][label] = (float(seg.mean()), float((seg > 0.65).mean()),
                                 float((seg < 0.50).mean()), want)
        print("  %s 完成" % pname)

    print("\n" + "=" * 108)
    print("%-30s %-22s %7s %8s %8s" % ("段落", "参数集", "平均E", ">0.65占比", "<0.50占比"))
    print("-" * 108)
    for label, f, a, b, want in SECTIONS:
        for pname, _ in PARAMS:
            e, hi, lo, _w = res[pname][label]
            tag = {"up": "应升", "keep": "保持", "low": "必须低"}[want]
            print("%-30s %-22s %7.3f %7.0f%% %7.0f%%   [%s]"
                  % (label[:30], pname, e, hi * 100, lo * 100, tag))
        print("-" * 108)

    print("\n=== 结论速览（A 段升了多少 / 必须低的段落有没有被抬起来）===")
    akey = SECTIONS[0][0]
    lkey = SECTIONS[2][0]
    lkey2 = SECTIONS[3][0]
    base = res[PARAMS[0][0]][akey][0]
    for pname, _ in PARAMS[1:]:
        print("  %-22s A段 %.3f→%.3f (%+.3f)   intro %.3f→%.3f   BW钢琴 %.3f→%.3f"
              % (pname, base, res[pname][akey][0], res[pname][akey][0] - base,
                 res[PARAMS[0][0]][lkey][0], res[pname][lkey][0],
                 res[PARAMS[0][0]][lkey2][0], res[pname][lkey2][0]))
    print("\nOK")


if __name__ == "__main__":
    main()
