# -*- coding: utf-8 -*-
"""回归测试：音频源对话框 `AudioSourceDialog` 不得因 Qt 信号重入而无限递归（栈溢出）。

背景（2026-10-01 阻断项）：`scan_devices()` 里程序化 `device.clear()/addItem()` 会**同步**
发 `currentIndexChanged` → 回 `_on_device_changed` → 音源==asio 时又调 `scan_devices()`
→ 无限递归 ⇒ Windows 栈溢出 0xC00000FD（切到 ASIO 或点「重新扫描」即触发）。

本测试**只构造对话框本身**（用最小假 main，不建 MainWindow、不跑主窗口业务），
切到 ASIO 后**主动**高频重入 `_on_device_changed` + `scan_devices`，断言：
  (a) 全程无 `RecursionError`、无异常；
  (b) 重入标志 `_scanning` 中间曾为 `True`（证明守卫真的在起作用）；
  (c) 收尾后 `_scanning` 必须回到 `False`（绝不留下会把后续扫描全挡死的僵标志）。

用法（离屏）：
  QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_test_audio_dialog_reentry.py

⚠ 判定**以断言结论为准**：本测试不构造 MainWindow，故不存在"收尾 exit 127"那种
  原生卸载 abort；若你的环境里出现 127，那是进程退出阶段的 native 行为，不算失败。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication, QWidget                 # noqa: E402
_app = QApplication.instance() or QApplication([])
import panels as _pn                                                # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class _FakeState:
    """最小音频状态：`snapshot()` 返回空 dict 即可（调用方全用 `.get(k, 默认)`）。"""

    def snapshot(self):
        return {}


class _FakeAudio:
    def __init__(self):
        self.state = _FakeState()

    def list_devices(self):
        return {"system": ["扬声器 (Fake)"], "mic": [], "asio": ["ASIO: Fake Driver"]}


class _FakeMain(QWidget):
    """只提供对话框需要的 `cfg` / `audio`，不建真实主窗口、不跑任何播放业务。"""

    def __init__(self):
        super().__init__()
        self.cfg = {"audio": {}}
        self.audio = _FakeAudio()


def _pump(secs=0.2):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        _app.processEvents()
        time.sleep(0.005)


def _wait_idle(dlg, timeout=5.0):
    """等到扫描结束且重入标志复位。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        _app.processEvents()
        sc = getattr(dlg, "_scanning", False)
        th = getattr(dlg, "_scanner", None)
        if (not sc) and (th is None or not th.isRunning()):
            return True
        time.sleep(0.005)
    return False


print("构造 AudioSourceDialog（最小假 main，不建 MainWindow）")
main = _FakeMain()
try:
    dlg = _pn.AudioSourceDialog(main)
except Exception as e:                                              # noqa: BLE001
    print("  ✗ 构造失败：%s: %s" % (type(e).__name__, e))
    sys.exit(1)

# __init__ 末尾本身就会 scan 一次；先让它跑完
_settled0 = _wait_idle(dlg, 6.0)
check("初始状态：_scanning 复位为 False", getattr(dlg, "_scanning", None) is False,
      "scanning=%s settled=%s" % (getattr(dlg, "_scanning", None), _settled0))

print("\n锤击：切到 ASIO 后连续 50×( _on_device_changed + scan_devices )")
asio_idx = getattr(dlg, "_asio_idx", 3)
dlg.source.setCurrentIndex(asio_idx)          # 触发 _on_source_changed → _fill_devices + scan_devices
check("已切到 ASIO 音源", dlg._src_key() == "asio",
      "key=%s idx=%s" % (dlg._src_key(), dlg.source.currentIndex()))

errors = []
seen_true = False
for i in range(50):
    try:
        dlg._on_device_changed(0)
        if getattr(dlg, "_scanning", False):
            seen_true = True
        dlg.scan_devices()
        if getattr(dlg, "_scanning", False):
            seen_true = True
    except RecursionError as e:
        errors.append("RecursionError@iter%d: %s" % (i, e))
        break
    except Exception as e:                                          # noqa: BLE001
        errors.append("%s@iter%d: %s" % (type(e).__name__, i, e))
        break

check("50× 重入无 RecursionError / 无异常", not errors, str(errors[:1]))
check("重入标志曾是 True（守卫确实在起作用，否则就是每帧新起扫描）",
      seen_true, "seen_true=%s" % seen_true)

print("\n收尾：等待线程结束，标志必须复位为 False")
_settled = _wait_idle(dlg, 6.0)
check("收尾后 _scanning 复位为 False（不留僵标志）",
      (getattr(dlg, "_scanning", None) is False) and _settled,
      "scanning=%s settled=%s" % (getattr(dlg, "_scanning", None), _settled))

# 收尾清理（不影响判定）
try:
    dlg.close()
    main.close()
    _pump(0.1)
except Exception:                                                   # noqa: BLE001
    pass

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
