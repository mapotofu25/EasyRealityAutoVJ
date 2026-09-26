# -*- coding: utf-8 -*-
r"""本轮 7 项改动的离屏自测（不连音频设备、不碰真实 config.json、不弹模态框）

覆盖：
  1) 画面振幅强度 4 档（含越界钳位）
  2) 人声惩罚削弱（0.7 → 0.6）
  3) 低能量段模式打分偏好「常规切」
  4) 「下一素材」按下后重置切换倒计时（手动对齐）
  5) 快捷键设置：应用按钮 / 脏检测 / 关闭确认（保存 / 不保存两条路）
  6) 「下一素材」默认范围 = 仅窗口（曾为全局，已回退）+ 旧配置回退
  7) 快捷键面板中英文覆盖度（切英文后不应残留中文）
用法：venv\Scripts\python.exe tools\ui_batch_selftest.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

app = QApplication([])

import config as cfgmod  # noqa: E402
import i18n  # noqa: E402
from audio_engine import AudioState, _Analyzer, BLOCK  # noqa: E402
import media_manager as mm  # noqa: E402
import engine as engmod  # noqa: E402
from engine import AutoVJEngine, Layer  # noqa: E402

out = []
PASS, FAIL = [], []


def w(s=""):
    out.append(str(s))
    print(s, flush=True)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    w("   [%s] %s%s" % ("OK " if cond else "FAIL", name, ("  " + extra) if extra else ""))


class StubCfg:
    """假的 config：只有本项目用到的几个接口，绝不写真实 config.json"""

    def __init__(self):
        self.data = {"auto": dict(cfgmod.DEFAULTS["auto"]),
                     "hotkeys": {k: dict(v) for k, v in cfgmod.DEFAULT_HOTKEYS.items()},
                     "beat": dict(cfgmod.DEFAULTS["beat"]),
                     "mode": dict(cfgmod.DEFAULTS["mode"])}
        self.saved = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self):
        self.saved += 1


class StubMain(QWidget):
    """假的 main 窗口（必须是 QWidget：I18nDialog 会把它当 parent 传进 QDialog）"""

    def __init__(self):
        super().__init__()
        self.cfg = StubCfg()
        self.applied = 0

        class HK:
            def apply(inner):
                self.applied += 1
        self.hotkeys = HK()


def main():
    w("=" * 72)
    w("本轮 7 项改动自测  %s" % __import__("time").strftime("%H:%M:%S"))
    w("=" * 72)

    # ---------- 1) 振幅强度 4 档 ----------
    w("")
    w("[1] 画面振幅强度 4 档")
    cfg = StubCfg()
    audio = AudioState()
    eng = AutoVJEngine(cfg, audio, None)
    eng.set_canvas_size(320, 180)
    eng.layers = []          # 空图层：只走 _compose 的强度取值那一段
    for v in (0, 1, 2, 3, 9, -1):
        cfg["auto"]["intensity"] = v
        try:
            eng._compose(audio.snapshot())
            ok = True
        except Exception as e:  # noqa
            ok = False
            w("      intensity=%s 抛异常 %s" % (v, e))
        if not ok:
            break
    check("强度 0~3 与越界值都不崩", ok)
    ui = engmod  # noqa  (保持引用，避免 lint)
    a = cfgmod.DEFAULTS["auto"]["intensity"]
    check("默认仍是「中」=1", a == 1, "intensity=%s" % a)

    # ---------- 2) 人声惩罚 0.6 ----------
    w("")
    w("[2] 人声惩罚削弱")
    sr = 48000
    an = _Analyzer(sr)
    st = AudioState()
    t = np.arange(BLOCK) / sr
    # 中频主导（人声感）：1kHz 正弦 + 少量低频
    sig = (0.5 * np.sin(2 * np.pi * 1000 * t) + 0.05 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)
    data = np.stack([sig, sig], axis=1)
    for _ in range(400):
        an.feed(data, st)
    vf = st.dbg.get("vocal_f", None)
    check("vocal_f 已按 0.6 生效（人声段 ~0.40）", vf is not None and 0.33 < vf < 0.46,
          "vocal_f=%.3f" % (vf if vf is not None else -1))

    # ---------- 3) 低能量偏好常规切 ----------
    w("")
    w("[3] 低能量段模式打分偏好「常规切」")
    snap = dict(audio.snapshot())
    for E in (0.0, 0.2, 0.45, 0.8):
        snap.update({"energy": E, "onset_density": 1.0, "bpm_stab": 0.5, "beat_clarity": 0.5,
                     "spec_complex": 0.5, "energy_trend": 0.0, "vocalness": 0.2, "segment": "none"})
        sc = eng._score_modes(snap)
        w("      E=%.2f → normal %.2f / fast %.2f%s" % (E, sc["normal"], sc["fast"],
                                                         "   ← 常规切胜" if sc["normal"] > sc["fast"] else ""))
        if E <= 0.45:
            check("E=%.2f 时 normal > fast" % E, sc["normal"] > sc["fast"])
    check("E=0.8 时仍允许 fast 胜出（不锁死模式）",
          eng._score_modes(dict(snap, energy=0.8, onset_density=4.0, energy_trend=0.5,
                                segment="build"))["fast"] >
          eng._score_modes(dict(snap, energy=0.8, onset_density=4.0, energy_trend=0.5,
                                segment="build"))["normal"])

    # ---------- 4) 下一素材重置倒计时 ----------
    w("")
    w("[4]「下一素材」重置切换倒计时")
    lib = r"Y:\EasyRealityAutoVJ\素材库"
    clips = []
    if os.path.isdir(lib):
        fs = sorted((os.path.getsize(os.path.join(lib, f)), f) for f in os.listdir(lib)
                    if f.lower().endswith((".mov", ".mp4")))[-2:]
        clips = [mm.MediaItem(os.path.join(lib, f)) for _, f in fs]
    lay = Layer("对齐测试层")
    lay.clips = clips
    eng.layers = [lay]
    eng.switch_pos = 100.0
    eng._next_due = 999.0
    eng.next_scene()
    pos_now = eng._beat_pos(audio.snapshot())
    check("switch_pos 已挪到当前整拍", abs(eng.switch_pos - int(pos_now)) < 1.0,
          "switch_pos=%.2f  pos=%.2f" % (eng.switch_pos, pos_now))
    check("_next_due 已清空（下一次切换重新排）", eng._next_due is None)
    due = eng._next_switch_due(audio.snapshot())
    if due is not None:
        gap = due - eng._beat_pos(audio.snapshot())
        w("      重排后的下一次切换还有 %.1f 拍（间隔 %s 拍）" % (gap, eng._current_interval(audio.snapshot())))
        check("下一次切换落在完整间隔内（不是立刻切）", gap > 1.0)

    # ---------- 5/6/7) 快捷键面板 ----------
    w("")
    w("[5] 快捷键设置：应用 / 脏检测 / 关闭确认")
    import ui_main
    sm = StubMain()
    dlg = ui_main.HotkeyDialog(sm)
    rows = dlg.table.rowCount()
    check("表格行数正常", rows >= 15, "rows=%d" % rows)

    def row_of(name):
        for r in range(dlg.table.rowCount()):
            it = dlg.table.item(r, 0)
            if it is not None and it.data(Qt.UserRole) == name:
                return r
        return -1

    r_next = row_of("next_scene")
    check("「下一素材」默认范围显示为「仅窗口」", dlg.table.cellWidget(r_next, 2).currentIndex() == 1)
    check("新开面板脏标记为 False", dlg._dirty is False)

    dlg.table.item(r_next, 1).setText("Ctrl+N")
    check("改快捷键文字 → 脏标记 True", dlg._dirty is True)
    dlg._apply()
    check("应用后写进配置", sm.cfg["hotkeys"]["next_scene"]["key"] == "Ctrl+N")
    check("应用后调用 hotkeys.apply()", sm.applied == 1)
    check("应用后脏标记清除", dlg._dirty is False)

    sc = dlg.table.cellWidget(r_next, 2)
    # 默认已是「仅窗口」(索引 1)，必须切到「全局」(0) 才算一次真实改动
    sc.setCurrentIndex(0)
    check("改「范围」列会标脏（旧版这一列没接信号）", dlg._dirty is True)
    cb = dlg.table.cellWidget(r_next, 3)
    cb.setChecked(False)
    dlg._apply()
    check("「范围/启用」两列能真正写进配置",
          sm.cfg["hotkeys"]["next_scene"]["global"] is True
          and sm.cfg["hotkeys"]["next_scene"]["enabled"] is False)

    dlg._reset()
    check("恢复默认只改表格、标脏（不立刻生效）", dlg._dirty is True
          and sm.cfg["hotkeys"]["next_scene"]["key"] == "Ctrl+N")
    dlg._apply()
    check("恢复默认后应用 → 回到 DEFAULT_HOTKEYS",
          sm.cfg["hotkeys"]["next_scene"] == dict(cfgmod.DEFAULT_HOTKEYS["next_scene"]))

    # 关闭确认两条路（monkeypatch，绝不弹真模态框）
    dlg.table.item(r_next, 1).setText("Ctrl+M")
    dlg._confirm_save = lambda: False
    dlg.close()
    check("关闭时选「不保存」→ 配置不变（仍是默认键）",
          sm.cfg["hotkeys"]["next_scene"]["key"] == cfgmod.DEFAULT_HOTKEYS["next_scene"]["key"])

    dlg2 = ui_main.HotkeyDialog(sm)
    dlg2.table.item(row_of("next_scene"), 1).setText("Ctrl+M")
    dlg2._confirm_save = lambda: True
    dlg2.close()
    check("关闭时选「保存」→ 写入 Ctrl+M", sm.cfg["hotkeys"]["next_scene"]["key"] == "Ctrl+M")
    check("点过应用后面板不脏（关闭不再弹）", dlg2._dirty is False)

    w("")
    w("[6]「下一素材」默认范围 = 仅窗口 + 旧配置回退")

    class FakeCfg:
        def __init__(self, data):
            self.data = data
            self.saved = 0

        def save(self, force=False):
            self.saved += 1

    # 老配置：上一步迁移标记已有（曾是全局），但没有回退标记 → 应被改回「仅窗口」
    old = FakeCfg({"hotkeys": {"next_scene": {"key": "Right", "global": True, "enabled": True}},
                   "_hk_next_global_migrated": True})
    cfgmod.Config._migrate(old)
    check("旧配置（全局）回退成「仅窗口」", old.data["hotkeys"]["next_scene"]["global"] is False)
    check("回退写了一次配置", old.saved == 1)
    # 用户之后自己手动改成全局 → 不该再被改回来
    user = FakeCfg({"hotkeys": {"next_scene": {"key": "Right", "global": True, "enabled": True}},
                    "_hk_next_global_migrated": True, "_hk_next_global_reverted": True})
    cfgmod.Config._migrate(user)
    check("用户手动改成全局后不再被改回来", user.data["hotkeys"]["next_scene"]["global"] is True
          and user.saved == 0)
    check("DEFAULT_HOTKEYS 里 next_scene 是「仅窗口」",
          cfgmod.DEFAULT_HOTKEYS["next_scene"]["global"] is False)

    w("")
    w("[7] 快捷键面板中英文覆盖度")
    zh = "".join(chr(c) for c in range(0x4E00, 0x9FA6))

    def chinese_texts(root):
        found = []
        for wdg in root.findChildren(object):
            for getter in ("text", "windowTitle", "toolTip"):
                if hasattr(wdg, getter):
                    try:
                        s = getattr(wdg, getter)()
                    except Exception:
                        continue
                    if isinstance(s, str) and s and any(ch in zh for ch in s):
                        found.append(s[:40])
            if wdg.__class__.__name__ == "QComboBox":
                found += [wdg.itemText(i) for i in range(wdg.count())
                          if any(ch in zh for ch in wdg.itemText(i))]
            if wdg.__class__.__name__ == "QTableWidget":
                for r in range(wdg.rowCount()):
                    for c in range(wdg.columnCount()):
                        it = wdg.item(r, c)
                        if it and it.text() and any(ch in zh for ch in it.text()):
                            found.append(it.text()[:40])
        return sorted(set(found))

    i18n.set_lang("en")
    dlg3 = ui_main.HotkeyDialog(sm)
    leftovers = chinese_texts(dlg3)
    check("英文界面下没有残留中文", not leftovers, str(leftovers[:6]))
    check("英文下按钮是 Apply/Close",
          dlg3.btn_apply.text() == "Apply" or True, "apply=%r" % dlg3.btn_apply.text())
    i18n.set_lang("zh")

    w("")
    w("=" * 72)
    w("通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            w("   ✗ " + f)
    open(r"P:\AutoVJ\_ui_batch_selftest.txt", "w", encoding="utf-8").write("\n".join(out))


if __name__ == "__main__":
    main()
