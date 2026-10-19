# -*- coding: utf-8 -*-
r"""低能量段的模式打分体检：什么组合会让「快切」在低能量时胜出

用户反馈「0.2 能量还在快切」。用真实 AutoVJEngine._score_modes 扫一遍
（灵敏度取用户实际值 2 = 高，快切另有 +0.5）。
用法：venv\Scripts\python.exe tools\low_energy_mode_check.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

import config as cfgmod  # noqa: E402
from audio_engine import AudioState  # noqa: E402
from engine import AutoVJEngine  # noqa: E402

out, BAD = [], []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


CASES = [
    # 名字, 瞬态密度(每秒强拍数), 能量趋势, 频谱复杂度, 段落, 灵敏度
    ("典型安静段", 0.5, 0.0, 0.5, "none", 2),
    ("安静但有零星鼓点", 2.0, 0.0, 0.5, "none", 2),
    ("安静的 build（渐强）", 1.0, 0.5, 0.5, "build", 2),
    ("最坏情况：全加成拉满", 4.0, 1.0, 1.0, "build", 2),
]


def main():
    cfg = cfgmod.Config()
    eng = AutoVJEngine(cfg, AudioState(), None)
    cfg["mode"]["sensitivity"] = 2          # 用户实际设置（高）
    eng.layers = []
    snap = dict(AudioState().snapshot())

    w("=" * 84)
    w("低能量段：模式打分（★ = 快切胜出，也就是用户看到的「低能量还在快切」）")
    w("=" * 84)
    w("   %-22s %-6s %-9s %-9s %-8s %s" % ("场景", "能量", "常规切", "快切", "胜出", "低能量自检"))
    for name, od, tr, cx, seg, sens in CASES:
        for E in (0.10, 0.20, 0.30, 0.40, 0.50, 0.70):
            snap.update({"energy": E, "onset_density": od, "energy_trend": tr,
                         "spec_complex": cx, "segment": seg, "bpm_stab": 0.5,
                         "beat_clarity": 0.5, "vocalness": 0.2})
            cfg["mode"]["sensitivity"] = sens
            sc = eng._score_modes(snap)
            nrm, fst = sc["normal"], sc["fast"]
            win = "normal" if nrm >= fst else "fast"
            mark = ""
            if E <= 0.35 and win == "fast":
                mark = "★ 低能量却快切"
                BAD.append("%s E=%.2f" % (name, E))
            w("   %-22s %-6.2f %-9.2f %-9.2f %-8s %s" % (name, E, nrm, fst, win, mark))

    w("")
    w("=" * 84)
    if BAD:
        w("❌ 低能量段仍会判成快切的组合：%d 个" % len(BAD))
        for b in BAD:
            w("     · " + b)
    else:
        w("✅ 低能量段（E≤0.35）在所有组合下都判成常规切")
    open(r"P:\AutoVJ\_low_energy_mode.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
