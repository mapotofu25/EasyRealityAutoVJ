"""音源对话框自测（移除「增益」之后）

覆盖：
  1) 对话框里不再有「增益」控件（滑块/数字框/标签）
  2) 旧配置里残留 gain=2.0 也能正常载入（不崩、不读它）
  3) 「应用」走的是 4 参数签名，配置里不会再写 gain
  4) 单声道勾选框仍然存在且能写入配置
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(ROOT, "src"))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK ]" if cond else "[FAIL]", name, ("  " + extra) if extra else ""))


def main():
    from PySide6.QtWidgets import QApplication, QWidget, QLabel, QSlider, QDoubleSpinBox
    app = QApplication.instance() or QApplication([])

    import panels

    # 不启后台设备扫描线程（离屏测试没必要，且会拖时间）
    panels.AudioSourceDialog.scan_devices = lambda self: None

    class StubCfg:
        def __init__(self, data):
            self.data = data
            self.saved = 0

        def __getitem__(self, k):
            return self.data[k]

        def save(self):
            self.saved += 1

    class StubState:
        def snapshot(self):
            return {}

    class StubAudio:
        def __init__(self):
            self.state = StubState()
            self.started = []

        def start(self, *a, **k):
            self.started.append((a, k))

    class StubMain(QWidget):
        def __init__(self, cfg):
            super().__init__()
            self.cfg = cfg
            self.audio = StubAudio()
            self.calls = []

        def apply_audio_settings(self, t, dev, sr, mono):
            self.calls.append((t, dev, sr, mono))

    print("\n[1] 控件里没有「增益」")
    cfg = StubCfg({"audio": {"source_type": "system", "device_name": "", "mono": True,
                             "sample_rate": 0, "gain": 2.0,      # 故意留一个旧值
                             "device_by_source": {}},
                   "lang": "zh", "theme": "dark"})
    m = StubMain(cfg)
    dlg = panels.AudioSourceDialog(m)
    check("没有 gain 滑块", not hasattr(dlg, "gain"))
    check("没有 gain_spin 数字框", not hasattr(dlg, "gain_spin"))
    check("对话框里没有 QSlider", len([w for w in dlg.findChildren(QSlider)]) == 0,
          "找到 %d 个 QSlider" % len(dlg.findChildren(QSlider)))
    check("对话框里没有 QDoubleSpinBox", len([w for w in dlg.findChildren(QDoubleSpinBox)]) == 0)
    labels = [w.text() for w in dlg.findChildren(QLabel)]
    check("没有「增益」标签", not any("增益" in t for t in labels),
          "标签: %s" % [t for t in labels if t][:8])
    check("旧配置 gain=2.0 能正常载入（不崩）", True)
    check("单声道勾选框仍在", hasattr(dlg, "chk_mono") and dlg.chk_mono.isChecked())

    print("\n[2] 对话框「应用」→ 传给主窗口的参数")
    dlg.chk_mono.setChecked(False)
    dlg.rate.setCurrentIndex(2)          # 48000
    dlg.apply()
    check("apply_audio_settings 被调用一次", len(m.calls) == 1, str(m.calls))
    if m.calls:
        t, dev, sr, mono = m.calls[0]
        check("参数是 4 个（不再有 gain）", True, "t=%s sr=%s mono=%s" % (t, sr, mono))
        check("采样率传对", sr == 48000, "sr=%s" % sr)
        check("单声道传对", mono is False, "mono=%s" % mono)

    print("\n[3] 真实 MainWindow.apply_audio_settings 的行为（用 fake self 直接调）")
    from ui_main import MainWindow

    cfg2 = StubCfg({"audio": {"source_type": "system", "device_name": "", "mono": True,
                              "sample_rate": 0, "gain": 2.0, "device_by_source": {}},
                    "lang": "zh", "theme": "dark"})
    audio2 = StubAudio()

    class FakeSelf:
        pass

    fs = FakeSelf()
    fs.cfg = cfg2
    fs.audio = audio2
    MainWindow.apply_audio_settings(fs, "system", "扬声器 (Realtek)", 48000, False)
    check("配置里写入了 mono=False", cfg2["audio"].get("mono") is False)
    check("配置里写入了采样率", cfg2["audio"].get("sample_rate") == 48000)
    check("配置里写入了设备名", cfg2["audio"].get("device_name") == "扬声器 (Realtek)")
    check("没有动 gain（保留旧值 2.0）", cfg2["audio"].get("gain") == 2.0)
    check("配置已保存", cfg2.saved >= 1)
    check("采集被启动一次", len(audio2.started) == 1, str(audio2.started)[:110])
    if audio2.started:
        args, kwargs = audio2.started[0]
        check("start 收到 mono/sr", kwargs.get("mono") is False and kwargs.get("sr") == 48000,
              str(kwargs))
        check("start 不再收到 gain", "gain" not in kwargs, str(kwargs))

    print("\n" + "=" * 60)
    print("通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   FAIL:", f)
    with open(os.path.join(ROOT, "_audio_dialog_selftest.txt"), "w", encoding="utf-8") as fh:
        fh.write("通过 %d / 失败 %d\n" % (len(PASS), len(FAIL)))
        for f in FAIL:
            fh.write("FAIL: %s\n" % f)


if __name__ == "__main__":
    main()
