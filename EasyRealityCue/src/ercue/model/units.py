# -*- coding: utf-8 -*-
"""**单位换算的唯一入口**（架构方案 §3.2 硬约束）。

铁律（违反即违规，代码评审逐条对照）：
  · 内部唯一坐标 = **秒**。除本文件与 `ercue_io.py` 外，任何地方都不准出现
    `beat_len` 的乘除。
  · 拍（beats）只是「吸附网格」与「标尺刻度」的**派生物**。
  · 文件格式 `.ercue` 里 `at_beats` 是主坐标、`at_sec` 是冗余；换算只在
    序列化边界发生一次（见 `ercue_io.sheet_to_file_dict / sheet_from_file_dict`，模型内部
    只有秒坐标，`to_state_dict/from_state_dict` 是**秒**形态，不碰拍）。

★ bpm 退化行为（**明确且一致**）
------------------------------------------------------------
`bpm <= 0`（或非数值）时，视为「**1 拍 = 1 秒**」（`DEGENERATE_BEAT_LEN = 1.0`），
**不抛异常、不猜 BPM**。调用方若需要知道当前是否处于退化态，用 `bpm_is_usable(bpm)`
显式判断 —— 设计上 UI 应据此**禁用拍吸附档位**（架构方案 §3.2）。
"""

from __future__ import annotations

# 退化态：1 拍 = 1 秒
DEGENERATE_BEAT_LEN = 1.0
_MIN_BPM = 1e-6

# 吸附档位 → 步长（单位：拍）。唯一数值来源（架构方案 §4-C2：绝不在别处硬编码 4.0）。
SNAP_STEP_BEATS = {
    "off": 0.0,      # 不吸附
    "beat": 1.0,     # 1 拍
    "bar": 4.0,      # 1 小节 = 4 拍
    "phrase": 8.0,   # 1 乐句 = 8 拍
}


def to_float(v, default: float = 0.0) -> float:
    """宽松转 float：任何转换失败都回落 `default`（**绝不抛**，导入路径要容错）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def bpm_is_usable(bpm) -> bool:
    """bpm 是否为可用的有效值（> 0 且可转 float）。False 时一切换算走退化态。"""
    return to_float(bpm) > _MIN_BPM


def beat_len(bpm) -> float:
    """一拍的秒数 = 60 / bpm。退化态返回 `DEGENERATE_BEAT_LEN`（1.0）。"""
    b = to_float(bpm)
    if b > _MIN_BPM:
        return 60.0 / b
    return DEGENERATE_BEAT_LEN


def sec_to_beats(sec, bpm) -> float:
    """秒 → 拍。退化态下 1 拍 = 1 秒（即原样返回）。"""
    return to_float(sec) / beat_len(bpm)


def beats_to_sec(beats, bpm) -> float:
    """拍 → 秒。退化态下 1 拍 = 1 秒（即原样返回）。"""
    return to_float(beats) * beat_len(bpm)


def snap_grid(mode, bpm) -> float:
    """吸附档位 → 步长（**秒**）。`off` 返回 0.0（表示不吸附）。

    这是吸附档位数值的唯一来源，其它任何地方都不得硬编码步长。
    """
    step_beats = SNAP_STEP_BEATS.get(mode, 0.0)
    if step_beats <= 0.0:
        return 0.0
    return step_beats * beat_len(bpm)


def format_timecode(sec) -> str:
    """秒 → `mm:ss.mmm`。负值按 0 处理。"""
    s = to_float(sec)
    neg = s < 0.0
    if neg:
        s = 0.0
    total_ms = int(round(s * 1000.0))
    m, rem_ms = divmod(total_ms, 60_000)
    ss, ms = divmod(rem_ms, 1000)
    return "%02d:%02d.%03d" % (m, ss, ms)


# ==========================================================================
# 自检
# ==========================================================================
def selftest() -> bool:
    """单位换算自检（往返 + 退化态 + 吸附 + 时间码）。"""
    # 1) 往返一致（有效 bpm）
    for bpm in (120.0, 155.03, 128.0, 99.9):
        for sec in (0.0, 0.5, 12.384, 372.4, 1000.0):
            b = sec_to_beats(sec, bpm)
            back = beats_to_sec(b, bpm)
            assert abs(back - sec) < 1e-9, (bpm, sec, back)

    # 2) beat_len 数值
    assert abs(beat_len(120.0) - 0.5) < 1e-12
    assert abs(beat_len(155.03) - 60.0 / 155.03) < 1e-12
    # 文件样例：32 拍 × beat_len(155.03) ≈ 12.3846997（文件里的 12.384 是旧实现用
    # 截断过的 beat_len 算出的冗余值，见规格书 §2.3 注）
    assert abs(beats_to_sec(32.0, 155.03) - 12.384699735535058) < 1e-9

    # 3) 退化态：1 拍 = 1 秒，且调用方可判知
    for bad in (0, 0.0, -1.0, None, "xx", ""):
        assert not bpm_is_usable(bad), bad
        assert beat_len(bad) == DEGENERATE_BEAT_LEN
        assert sec_to_beats(3.0, bad) == 3.0
        assert beats_to_sec(3.0, bad) == 3.0
    assert bpm_is_usable(1e-9) is False   # 低于阈值仍判退化
    assert bpm_is_usable(1e-4) is True    # 高于阈值视为有效

    # 4) 吸附步长（唯一来源）
    assert snap_grid("off", 120.0) == 0.0
    assert abs(snap_grid("beat", 120.0) - 0.5) < 1e-12
    assert abs(snap_grid("bar", 120.0) - 2.0) < 1e-12       # 4 拍 = 2s
    assert abs(snap_grid("phrase", 120.0) - 4.0) < 1e-12    # 8 拍 = 4s
    assert snap_grid("beat", 0) == 1.0                       # 退化：1 拍 = 1s
    assert snap_grid("bar", 0) == 4.0
    assert snap_grid("phrase", 0) == 8.0
    assert snap_grid("nonsense", 120.0) == 0.0

    # 5) 时间码
    assert format_timecode(0.0) == "00:00.000"
    assert format_timecode(12.384) == "00:12.384"
    assert format_timecode(372.4) == "06:12.400"
    assert format_timecode(-5.0) == "00:00.000"
    assert format_timecode(61.007) == "01:01.007"
    assert format_timecode(59.9995) in ("00:59.999", "01:00.000")  # 边界四舍五入，二者之一

    print("[units] 自检通过 ✓  beat_len(120)=%.4f  snap: bar@120=%.3fs  "
          "退化(1拍=1秒)=%s" % (beat_len(120.0), snap_grid("bar", 120.0),
                               bpm_is_usable(0) is False))
    return True


if __name__ == "__main__":
    selftest()
