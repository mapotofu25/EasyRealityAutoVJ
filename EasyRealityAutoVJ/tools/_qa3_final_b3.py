# -*- coding: utf-8 -*-
"""QA(严过关) —— B3：两处「未完成」文案（中/英）+ 音源下拉索引映射未破坏。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication, QWidget                     # noqa: E402
_app = QApplication.instance() or QApplication([])

import i18n as _i18n                                                    # noqa: E402
import panels as _pn                                                    # noqa: E402
import ui_main as _um                                                   # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  PASS " if ok else "  FAIL ") + name + (("   | " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


def _pump(secs=0.3):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        _app.processEvents()
        time.sleep(0.005)


# ================= ① AudioSourceDialog =================
class _FakeState:
    def snapshot(self):
        return {}


class _FakeAudio:
    def __init__(self):
        self.state = _FakeState()

    def list_devices(self):
        return {"system": ["扬声器 (Fake)"], "mic": [], "asio": ["ASIO: Fake Driver"]}


class _FakeMain(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = {"audio": {}}
        self.audio = _FakeAudio()


_i18n.set_lang("zh")
main = _FakeMain()
dlg = _pn.AudioSourceDialog(main)
t0 = time.perf_counter()
while time.perf_counter() - t0 < 6.0:
    _app.processEvents()
    th = getattr(dlg, "_scanner", None)
    if not getattr(dlg, "_scanning", False) and (th is None or not th.isRunning()):
        break
    time.sleep(0.01)

zh3 = dlg.source.itemText(3)
print("    zh itemText(3) = %r" % zh3)
print("    zh itemText(0..3) = %s" % [dlg.source.itemText(i) for i in range(dlg.source.count())])
check("中文：source.itemText(3) 含「未完成」", "未完成" in zh3, zh3)

idxs = []
for i in range(4):
    dlg.source.setCurrentIndex(i)
    idxs.append(dlg._src_key())
check("索引映射：0..3 -> system/mic/mic/asio", idxs == ["system", "mic", "mic", "asio"], idxs)
dlg.source.setCurrentIndex(3)
check("索引 3 仍 _src_key()=='asio'（文案改动没破坏映射）", dlg._src_key() == "asio",
      "idx=%d key=%s" % (dlg.source.currentIndex(), dlg._src_key()))

# 中文先记录原文，再切英文
_i18n.retranslate(dlg)
_i18n.set_lang("en")
_i18n.retranslate(dlg)
en3 = dlg.source.itemText(3)
print("    en itemText(3) = %r" % en3)
check("英文：source.itemText(3) 取到英文译文（含 not finished）",
      ("not finished" in en3.lower()) and ("未完成" not in en3), en3)
check("英文下索引 3 仍 _src_key()=='asio'", dlg._src_key() == "asio", dlg._src_key())
_i18n.set_lang("zh")
_i18n.retranslate(dlg)
check("切回中文：source.itemText(3) 恢复含「未完成」", "未完成" in dlg.source.itemText(3),
      dlg.source.itemText(3))

try:
    dlg.close()
    main.close()
except Exception:                                                       # noqa: BLE001
    pass


# ================= ② OutputPanel.chk_ndi_audio =================
class _OPMain(QWidget):
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


opm = _OPMain()
panel = _um.OutputPanel(opm)
zh_txt = panel.chk_ndi_audio.text()
print("    zh chk_ndi_audio.text() = %r" % zh_txt)
check("中文：chk_ndi_audio.text() 含「未完成」", "未完成" in zh_txt, zh_txt)

_i18n.set_lang("zh")
_i18n.retranslate(panel)
_i18n.set_lang("en")
_i18n.retranslate(panel)
en_txt = panel.chk_ndi_audio.text()
print("    en chk_ndi_audio.text() = %r" % en_txt)
check("英文：chk_ndi_audio.text() 取到英文译文（含 not finished）",
      ("not finished" in en_txt.lower()) and ("未完成" not in en_txt), en_txt)

_i18n.set_lang("zh")
_i18n.retranslate(panel)
check("切回中文：chk_ndi_audio.text() 恢复含「未完成」", "未完成" in panel.chk_ndi_audio.text(),
      panel.chk_ndi_audio.text())

try:
    panel.close()
    opm.close()
    _pump(0.1)
except Exception:                                                       # noqa: BLE001
    pass

print()
print("结果：%s" % ("B3 通过" if not FAILS else "B3 失败 %s" % FAILS))
sys.exit(1 if FAILS else 0)
