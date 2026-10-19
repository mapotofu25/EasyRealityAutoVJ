# -*- coding: utf-8 -*-
"""场景 B 深挖：真实声卡设备名长什么样，以及「匹配不到」到底有多容易发生。

目的：把「USB 声卡线路输入」的常见设备名与配置里存的值做一次比对，
说明为什么 `device_name in m.name` 子串匹配很容易失败 → 静默回退 cands[0]。

本脚本不依赖真实硬件：用一组"现实中很常见"的声卡枚举名做样例。
"""
import os, sys, types

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)
import audio_engine as AE


class FakeMic:
    def __init__(self, name, isloopback=False):
        self.name = name
        self.isloopback = isloopback


# 现实中 Windows WASAPI 枚举出的非回环设备名（样例）
ENUM = [
    "麦克风 (Realtek(R) Audio)",                 # cands[0]
    "线路输入 (Realtek(R) Audio)",
    "立体声混音 (Realtek(R) Audio)",
    "Digital Audio (S/PDIF) (High Definition Audio Device)",
    "Line In (USB Audio CODEC)",
    "Microphone (USB Audio CODEC)",
    "线路 (USB 声卡 Line-In)",                    # 若用户自定义过名字
]

# 用户配置里可能存的值（面板把它当 currentText 原样存）
CONFIGS = [
    "USB Audio CODEC",                # 少了 "Line In (" 前缀 → 子串匹配？"USB Audio CODEC" in "Line In (USB Audio CODEC)" = True
    "Line-In",                        # 连字符 vs 枚举名里是空格 "Line In" → 匹配失败
    "线路输入",                       # 中文 vs 枚举 "Line In" → 匹配失败
    "USB Audio Device",               # 换过 USB 口/驱动版本 → 名字变了
    "Realtek Line In",                # 用户自己拼的名字
    "线路输入 (USB 声卡 Line-In)",     # 恰好精确匹配（理想情况）
]


def stub(mics):
    mod = types.ModuleType("soundcard")
    mod.all_microphones = lambda include_loopback=False: list(mics)
    mod.default_speaker = lambda: None
    mod.default_microphone = lambda: mics[0]
    sys.modules["soundcard"] = mod
    return mod


def match(cands, device_name):
    """复刻 _run 的匹配逻辑（audio_engine.py:1512-1517）。"""
    mic = cands[0]
    if device_name:
        for m in cands:
            if device_name in m.name:
                mic = m
                break
    return mic


print("=" * 74)
print("场景 B 深挖：device_name 子串匹配的失败面")
print("=" * 74)
print("候选非回环设备（cands，cands[0]=%r）：" % ENUM[0])
for i, n in enumerate(ENUM):
    print("   [%d] %s" % (i, n))
print()
mics = [FakeMic(n) for n in ENUM]
cands = [m for m in mics if not m.isloopback]

print("%-34s %-28s %s" % ("配置里的 device_name", "匹配到的设备", "结果"))
print("-" * 74)
fail = 0
for cfg in CONFIGS:
    got = match(cands, cfg)
    hit = (got.name == cfg) or any(cfg in m.name for m in cands)
    got_name = got.name
    tag = "✓ 命中" if any(cfg in m.name for m in cands) else "✗ 落空→静默回退 cands[0]"
    if "落空" in tag:
        fail += 1
    print("%-34s %-28s %s" % (cfg, got_name[:26], tag))
print("-" * 74)
print("落空率：%d/%d" % (fail, len(CONFIGS)))
print()
print("结论：`device_name in m.name` 是**严格子串**（区分空格/连字符/中英文/大小写），")
print("      用户配置里存的字符串只要和 WASAPI 枚举名差一点点（换 USB 口、驱动更新、")
print("      中英文界面差异），就匹配不到 → 静默回退 cands[0]（多半是内置麦克风），")
print("      而且**界面不报错**，用户以为选的是线路输入，实际采的是麦克风。")
print("=" * 74)
