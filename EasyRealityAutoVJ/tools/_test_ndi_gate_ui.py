# -*- coding: utf-8 -*-
"""NDI 音频开关的**三处一致性**回归（跑真实主窗口，不是手抄副本）。

## 背景（2026-10-01 用户报「NDI 输出还是没有音频」的直接原因）
三道状态必须永远一致，任何一处脱钩都会造成「**有画面、没声音、而且日志里什么都没有**」：
    ① 界面勾选框 `chk_ndi_audio.isChecked()`
    ② 配置 `cfg["output"]["ndi_audio"]`
    ③ 采集线程的布尔量 `audio.state.ndi_audio_on`

旧代码的 bug：`_load_settings_to_ui` 用 `setChecked(ndi_audio)` 回填**没有屏蔽信号**，
触发了 `set_ndi_audio()` 槽；而那个槽里 `s = bool(s) and cfg["ndi_enabled"]` ——
启动时 NDI 输出还没开，于是**把已经存好的 `ndi_audio=True` 改写成 False**，
但勾选框看起来仍是勾选的 ⇒ 用户以为开了，实际采集循环整块跳过，一个字节都不喂给 NDI。

⚠ 本脚本**必须驱动真实 `ui_main.MainWindow`**：早先那版是"照抄三行代码"的副本，
  修好之后它照样报旧结论（测的是副本不是产品），没有回归价值。

用法：venv\\Scripts\\python.exe -u tools/_test_ndi_gate_ui.py
"""
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


from PySide6.QtWidgets import QApplication                        # noqa: E402
app = QApplication([])
import ui_main                                                    # noqa: E402


class _MB:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def information(*a, **k):
        pass

    @staticmethod
    def question(*a, **k):
        return 0


ui_main.QMessageBox = _MB


def triple(w):
    """(勾选框, cfg, 采集侧布尔量) 三元组。"""
    try:
        box = bool(w.output_panel.chk_ndi_audio.isChecked())
    except Exception:                                             # noqa: BLE001
        box = None
    cfg = bool(w.cfg["output"].get("ndi_audio", False))
    try:
        st = bool(w.audio.state.ndi_audio_on)
    except Exception:                                             # noqa: BLE001
        st = None
    return box, cfg, st


def run_case(title, ndi_enabled, ndi_audio):
    print("=" * 68)
    print("场景：%s" % title)
    print("  启动时磁盘配置: ndi_enabled=%r ndi_audio=%r" % (ndi_enabled, ndi_audio))
    w = ui_main.MainWindow()
    w.cfg.save = lambda *a, **k: None
    w.save_cfg = lambda *a, **k: None
    w.save_layers = lambda *a, **k: None
    # 造出"上次的磁盘配置"
    w.cfg["output"]["ndi_enabled"] = bool(ndi_enabled)
    w.cfg["output"]["ndi_audio"] = bool(ndi_audio)
    w._load_settings_to_ui()
    for _ in range(4):
        app.processEvents()
    box, cfg, st = triple(w)
    print("  启动后:            勾选框=%r  cfg=%r  ndi_audio_on=%r" % (box, cfg, st))
    # ⚠ 不变式要说准（这里踩过一次"断言写错反而看不出问题"）：
    #   · 勾选框与 cfg 是**用户偏好** —— 必须一致（脱钩就是用户看到"勾了却没声音"的根源）；
    #   · ndi_audio_on 是**实际生效条件** = 偏好 AND NDI 输出已开
    #     （NDI 输出关着时本来就发不出去，此时它是 False 才是对的）。
    ndi_on = bool(w.cfg["output"].get("ndi_enabled"))
    check("勾选框 == 配置（用户偏好不脱钩）", box == cfg, (box, cfg))
    check("ndi_audio_on == 偏好 AND NDI 已开（实际生效条件）",
          st == (cfg and ndi_on), "st=%r cfg=%r ndi_enabled=%r" % (st, cfg, ndi_on))
    check("启动回填**不能**把已存的 ndi_audio=True 改写成 False",
          cfg == bool(ndi_audio), "cfg=%r 期望=%r" % (cfg, ndi_audio))

    # 用户现场操作：把「NDI 输出」勾上
    w.output_panel.chk_ndi.setChecked(True)
    for _ in range(4):
        app.processEvents()
    box2, cfg2, st2 = triple(w)
    print("  勾上 NDI 输出后:   勾选框=%r  cfg=%r  ndi_audio_on=%r" % (box2, cfg2, st2))
    check("勾上 NDI 输出后：勾选框 == 配置", box2 == cfg2, (box2, cfg2))
    check("此时 NDI 已开 ⇒ 实际生效条件就是偏好本身", st2 == cfg2, (st2, cfg2))
    if ndi_audio:
        check("★ 音频真的会转发（ndi_audio_on=True）", st2 is True, st2)
    else:
        check("没勾音频时不应转发", st2 is False, st2)
    del w


def main():
    run_case("上次开过音频、但当时 NDI 输出是关的（旧代码在这里把配置改坏）", False, True)
    run_case("上次两项都开了", True, True)
    run_case("本机源码版实测配置（NDI 开、音频关）", True, False)
    run_case("全新配置", False, False)

    print()
    print("=" * 68)
    if FAILS:
        print("结果：%d 项失败 ✗" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("结果：全部通过 ✓（三处状态永远一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
