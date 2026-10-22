# -*- coding: utf-8 -*-
r"""「手动下一素材」与「自动贴小节线」到底怎么互相作用 —— 用真实代码算一遍，不靠嘴说

用真实 AutoVJEngine，喂合成的 snap（beat_count / downbeat_off / bpm 可控），
打印：按下时的拍位 → 重排后的下一次切换拍位 → 实际间隔（拍）→ 相对小节线的相位。

用法：venv\Scripts\python.exe tools\switch_align_report.py
"""
import os
import sys
import math

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

import config as cfgmod  # noqa: E402
from audio_engine import AudioState  # noqa: E402
from engine import AutoVJEngine  # noqa: E402

out = []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def main():
    cfg = cfgmod.Config()
    audio = AudioState()
    eng = AutoVJEngine(cfg, audio, None)
    eng.layers = []          # 空图层：只测倒计时数学

    base = audio.snapshot()

    def snap_at(beat, off=0, bpm=128.0):
        s = dict(base)
        s.update({"bpm": bpm, "beat_active": True, "beat_count": int(beat), "beat_phase": 0.0,
                  "downbeat_off": off, "energy": 0.5, "energy_trend": 0.0})
        return s

    def use(s):
        """让 engine 内部所有 self.audio.snapshot() 都返回这个合成 snap
        （next_scene 内部是自己去取 snapshot 的，不改这里测出来的就是真实设备状态）"""
        audio.snapshot = lambda _s=dict(s): dict(_s)
        return s

    w("=" * 78)
    w("[1] 间隔是「拍」还是「秒」？—— 看 _current_interval")
    w("=" * 78)
    for mode in ("normal", "fast"):
        eng.mode = mode
        iv = eng._current_interval(snap_at(0))
        w("  模式 %-7s → 间隔 %.0f 拍" % (mode, iv))
        for bpm in (90, 128, 174):
            w("      BPM %3d → 一拍 %.3f 秒 → %.0f 拍 = %.1f 秒"
              % (bpm, 60.0 / bpm, iv, iv * 60.0 / bpm))
    w("      BPM 未锁定（回退时钟固定 120BPM）→ 一拍 0.500 秒 → 16 拍 = 8.0 秒")

    w("")
    w("=" * 78)
    w("[2] 手动按下 → 下一次自动切换落在哪（间隔 16 拍的常规切）")
    w("=" * 78)
    w("   现在按下会**就地重定义小节头**（_bar_off_user = 按下拍的相位）")
    w("   %-8s %-9s %-12s %-10s %-12s %s" % ("音频相位", "按下拍位", "下一次切换", "实际间隔",
                                             "用户小节线", "说明"))
    for off in (0, 1):
        for phase in range(4):
            press = 40 + ((phase - off) % 4)     # 让按下时刻在音频网格里的相位 = phase
            eng.mode = "normal"
            eng._next_due = None
            eng.switch_pos = 0
            eng._bar_off_user = None
            s = use(snap_at(press, off=off))
            eng.next_scene()                      # 手动按一下（内含倒计时重置 + 重定义小节头）
            due = eng._next_switch_due(s)
            gap = due - press
            on_user_bar = abs((due - eng._bar_off_user) % 4) < 1e-6
            note = ("正好 %.0f 拍" % gap) if abs(gap - 16) < 1e-6 else ("间隔 %.0f 拍" % gap)
            w("   %-8d %-9d %-12.0f %-10.0f %-12s %s"
              % (off, press, due, gap, "踩" if on_user_bar else "没踩", note))
            w("            （重定义后 _bar_off_user = %s）" % eng._bar_off_user)

    w("")
    w("=" * 78)
    w("[3] 快切（间隔 8 拍）—— 同样是精确 8 拍")
    w("=" * 78)
    for phase in range(4):
        press = 40 + phase
        eng.mode = "fast"
        eng._next_due = None
        eng.switch_pos = 0
        eng._bar_off_user = None
        s = use(snap_at(press, off=0))
        eng.next_scene()
        due = eng._next_switch_due(s)
        w("   按下 %d（音频相位 %d）→ 下一次 %.0f（间隔 %.0f 拍，%s；_bar_off_user=%s）"
          % (press, phase % 4, due, due - press,
             "踩用户小节线" if abs((due - eng._bar_off_user) % 4) < 1e-6 else "没踩", eng._bar_off_user))

    w("")
    w("=" * 78)
    w("[4] 长静音（换歌）后手动相位自动失效")
    w("=" * 78)
    s2 = snap_at(0, off=0)
    s2["silent_sec"] = 5.0
    use(s2)
    eng._bar_off_user = 3
    eng._tick_locked()
    w("   静音 5 秒后 _bar_off_user = %s（None = 已交回音频识别的小节头）" % eng._bar_off_user)

    w("")
    w("=" * 78)
    w("[5] 自动贴小节线：只有明显偏移才介入（门槛 %.1f 拍）" % eng.ALIGN_MIN_OFFSET)
    w("=" * 78)
    for st_pos, label in ((40.0, "正好在小节线上"), (40.3, "偏 0.3 拍"), (40.6, "偏 0.6 拍"),
                        (41.0, "偏 1.0 拍"), (41.5, "偏 1.5 拍"), (42.0, "偏 2.0 拍")):
        eng.mode = "normal"
        eng._bar_off_user = None
        eng._next_due = None
        s = use(snap_at(int(st_pos), off=0))
        eng.switch_pos = st_pos
        due = eng._next_switch_due(s)
        target = st_pos + 16
        nearest = math.floor(target / 4.0 + 0.5) * 4.0   # 与引擎同算法（不用 round，避免 .5 的银行家舍入）
        off_amt = target - nearest
        engaged = abs(due - nearest) < 1e-9
        noop = abs(target - nearest) < 1e-9
        w("   基准 %.1f（%s）→ 目标 %.1f，最近小节线 %.0f，偏移 %+.1f 拍 → 最终 %.1f  【%s】"
          % (st_pos, label, target, nearest, off_amt, due,
             "无需调整（本来就对齐）" if noop else ("介入拉正" if engaged else "不介入（保持原节奏）")))

    w("")
    w("=" * 78)
    w("[6] HUD 倒计时：从「间隔-1」开始数（刚切完 15 / 快切 7）")
    w("=" * 78)
    for mode, iv in (("normal", 16), ("fast", 8)):
        eng.mode = mode
        eng._bar_off_user = None
        vals = []
        for remain in (float(iv), iv - 0.01, iv - 0.5, iv - 1.0, 1.0, 0.3):
            pos = 100.0 - remain
            s = dict(snap_at(int(pos), off=0))
            s["beat_phase"] = pos - int(pos)
            s["beat_count"] = int(pos)
            use(s)
            eng._next_due = 100.0
            vals.append("剩 %.2f 拍→显示 %s" % (remain, eng.switch_countdown(s)))
        w("   模式 %-7s：%s" % (mode, "；".join(vals)))

    w("")
    w("=" * 78)
    w("结论")
    w("=" * 78)
    w("· 间隔单位是「拍」，拍长来自实时 BPM ⇒ 倒计时跟着 BPM 走（16 拍 / 8 拍固定不变）。")
    w("· 按下「下一素材」= 重置倒计时 + 把这一拍定义成小节头 ⇒ 下一次切换精确 16 / 8 拍，")
    w("  而且落在**他自己**的小节线上。换歌（长静音 >3s）后手动相位失效。")
    w("· 自动贴小节线现在只在偏移 ≥ %.1f 拍时介入；小于它的偏移原样保留（恒定、不累积）。"
      % eng.ALIGN_MIN_OFFSET)
    w("· HUD 倒计时从 间隔-1 开始数（常规 15 / 快切 7），数到 0 就是切换点；实际间隔不变。")
    open(r"P:\AutoVJ\_switch_align.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
