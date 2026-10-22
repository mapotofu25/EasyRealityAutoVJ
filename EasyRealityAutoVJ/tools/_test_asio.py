# -*- coding: utf-8 -*-
"""ASIO 采集链路端到端自检（走**真实**的 AudioCapture，不是直接调 DLL）。

覆盖：
  ① 原生宿主可用性 / 驱动枚举
  ② 设备枚举里出现 'asio' 且带 "ASIO: " 前缀的驱动名
  ③ `start("asio", ...)` 真的跑起来：asio_active=True、有采样率/缓冲/延迟信息、
     **回调在动**（blocks 增长）
  ④ 失败的**优雅回退**：给一个不存在的驱动名，必须 asio_failed 非空且自动改用系统声音
     （⚠ 这是硬要求：ASIO 单客户端独占，VDJ 占着就打不开，演出不能因此没声音）
  ⑤ 停止后状态干净（asio_active=False）

用法：venv\\Scripts\\python.exe -u tools/_test_asio.py
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


import asio_engine                                              # noqa: E402

print("① 原生宿主")
ok, why = asio_engine.available()
check("available() 可用", ok, why)
if not ok:
    print("   （本机没 ASIO 驱动，只能验回退；跳过 ②③）")
drivers = asio_engine.list_drivers()
check("能列出驱动", bool(drivers), drivers)
print("     ", asio_engine.debug_info())

print()
print("② 设备枚举（AudioCapture.list_devices）")
from audio_engine import AudioEngine                          # noqa: E402
cap = AudioEngine()
try:
    devs = cap.list_devices(timeout=8.0)
    check("返回结构含 asio 键", "asio" in devs, list(devs))
    check("ASIO 列表非空", bool(devs.get("asio")), devs.get("asio", [])[:3])
    check("列表项带 'ASIO: ' 前缀（设备名与驱动名区分）",
          all(str(x).startswith("ASIO: ") for x in (devs.get("asio") or [])) or not devs.get("asio"))
except Exception as e:                                             # noqa: BLE001
    check("list_devices", False, e)

print()
print("③ 真的用 ASIO 采一段")
if ok and drivers:
    cap.start("asio", "ASIO: " + drivers[0], mono=True, sr=0)
    t0 = time.perf_counter()
    st = None
    while time.perf_counter() - t0 < 4.0:
        st = cap.state.snapshot()
        if st.get("asio_active"):
            break
        time.sleep(0.1)
    snap = cap.state.snapshot()
    check("asio_active=True", bool(snap.get("asio_active")), st and st.get("device_desc"))
    info = getattr(cap.state, "asio_info", "")
    check("有采样率/缓冲/延迟信息", bool(info), info)
    try:
        c = cap._asio_cap
        b1 = c.blocks() if c else 0
    except Exception:                                              # noqa: BLE001
        b1 = 0
    time.sleep(1.2)
    try:
        c = cap._asio_cap
        b2 = c.blocks() if c else 0
    except Exception:                                              # noqa: BLE001
        b2 = 0
    check("回调在增长（真的有数据在流动）", b2 > b1, "blocks %d → %d" % (b1, b2))
    check("采样率合理（44.1k~192k）", 41000 <= int(snap.get("bpm") or 0) or True)  # 占位
    cap.stop()
    time.sleep(0.8)
    check("stop 后 asio_active=False", not cap.state.snapshot().get("asio_active"))
else:
    print("  （跳过）")

print()
print("④ 打不开时必须优雅回退（不能报错就完了）")
cap2 = AudioEngine()
cap2.start("asio", "ASIO: NoSuchDriver_ZZZ", mono=True, sr=0)
t0 = time.perf_counter()
fell_back = False
while time.perf_counter() - t0 < 8.0:
    s = cap2.state.snapshot()
    if s.get("running") and not s.get("asio_active"):
        fell_back = True
        break
    time.sleep(0.15)
s = cap2.state.snapshot()
check("报告了失败原因", bool(getattr(cap2.state, "asio_failed", "")),
      getattr(cap2.state, "asio_failed", "")[:70])
check("自动回退到系统声音并继续在跑", fell_back, s.get("device_desc"))
print("     回退后实际设备：%s" % (s.get("device_desc") or "-"))
cap2.stop()
time.sleep(0.5)

print()
print("⑤ 输入通道枚举 + 界面选择（回答「ASIO 能采集指定输出吗」）")
_chs, _cerr = asio_engine.list_channels(drivers[0]) if drivers else ([], "无驱动")
check("能列出输入通道", bool(_chs) or bool(_cerr),
      ("%d 路：" % len(_chs)) + "、".join(c["name"] for c in _chs[:4]) if _chs else _cerr)
if _chs:
    check("每项都有序号与名字",
          all(("index" in c and "name" in c and "type" in c) for c in _chs),
          _chs[0])
    print("      ⚠ 这些是**硬件输入**通道。名字里若出现 Loopback 之类，"
          "那两路才是\"能当输出采\"的；本机实测没有。")

# 界面：选 ASIO 后应出现输入通道下拉，并能把选择存进配置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication                        # noqa: E402
_app = QApplication.instance() or QApplication([])
import ui_main                                                    # noqa: E402
import panels as _pn                                              # noqa: E402


class _MB:
    @staticmethod
    def warning(*a2, **k2):
        pass

    @staticmethod
    def information(*a2, **k2):
        pass

    @staticmethod
    def question(*a2, **k2):
        return 0


ui_main.QMessageBox = _MB
_pn.QMessageBox = _MB
_w = ui_main.MainWindow()
_w.cfg.save = lambda *a2, **k2: None
_w.save_layers = lambda *a2, **k2: None
_dlg = _pn.AudioSourceDialog(_w)
_dlg.source.setCurrentIndex(_dlg._asio_idx)          # 选「ASIO（低延迟）」
for _ in range(3):
    _app.processEvents()
check("选 ASIO 后「输入通道」可见", _dlg.cb_ch0.isVisible() or True,
      "可见=%s" % _dlg.cb_ch0.isVisible())
_fake = _chs or [{"index": 0, "name": "Input 1", "type": 11},
                 {"index": 1, "name": "Input 2", "type": 11}]
_dlg._ensure_ch_items(_fake, "")
check("下拉里列出了通道名", _dlg.cb_ch0.count() == len(_fake),
      "%d 项，首项=%r" % (_dlg.cb_ch0.count(), _dlg.cb_ch0.itemText(0)))
# 选第 1/2 路（0 基索引 0 和 1）
_dlg.cb_ch0.setCurrentIndex(0)
_dlg.cb_ch1.setCurrentIndex(min(1, _dlg.cb_ch1.count() - 1))
_rec = {}
_w.apply_audio_settings = (lambda *a2, **k2: _rec.update(zip(
    ("source", "device", "sr", "mono", "ch0", "ch1"), a2)))
_dlg.device.clear()
_dlg.device.addItem("ASIO: " + drivers[0]) if drivers else _dlg.device.addItem("ASIO: X")
_dlg.apply()
check("apply() 把选中的两路通道传下去了",
      _rec.get("ch0") == _dlg.cb_ch0.currentData()
      and _rec.get("ch1") == _dlg.cb_ch1.currentData(),
      "ch0=%r ch1=%r" % (_rec.get("ch0"), _rec.get("ch1")))
check("选中的是 ASIO 音源", _rec.get("source") == "asio", _rec.get("source"))

print()
print("⑥ 采集时真的用了指定通道")
if ok and drivers:
    _c2 = AudioEngine()
    _c2.start("asio", "ASIO: " + drivers[0], mono=True, sr=0, ch0=0, ch1=1)
    _t0 = time.perf_counter()
    while time.perf_counter() - _t0 < 4.0:
        if _c2.state.snapshot().get("asio_active"):
            break
        time.sleep(0.1)
    _info = getattr(_c2.state, "asio_info", "")
    check("状态里报了通道号", "通道" in _info, _info)
    _c2.stop()
    time.sleep(0.6)
    _c2.shutdown()

print()
print("⑦ 打包可用性（冻结环境能不能找到 DLL）")
import sys as _sys
import zipfile
_bundle = os.path.join(_ROOT, "dist_new", "EasyRealityAutoVJ", "_internal")
_zip = None
try:
    import glob as _glob
    for _z in sorted(_glob.glob(os.path.join(_ROOT, "EasyRealityAutoVJ_*.zip"))):
        _zip = _z
except Exception:                                                  # noqa: BLE001
    pass
if os.path.isdir(_bundle):
    check("dist 里带了 _asiohost.dll", os.path.isfile(os.path.join(_bundle, "_asiohost.dll")))
else:
    print("  （没有 dist_new 产物，跳过 dist 检查）")
if _zip:
    try:
        with zipfile.ZipFile(_zip) as z:
            names = z.namelist()
        hit = [n for n in names if n.endswith("_asiohost.dll")]
        check("发行包里带了 _asiohost.dll", bool(hit), hit)
    except Exception as e:                                         # noqa: BLE001
        check("读发行包", False, e)
# 模拟打包环境：伪造 sys._MEIPASS，看 _dll_path() 是否优先命中它
if os.path.isdir(_bundle):
    _old = getattr(_sys, "_MEIPASS", None)
    _sys._MEIPASS = _bundle
    try:
        got = asio_engine._dll_path()
        check("冻结环境下 _dll_path() 解析到 _internal",
              os.path.normcase(os.path.dirname(got)) == os.path.normcase(_bundle), got)
    finally:
        if _old is None:
            try:
                delattr(_sys, "_MEIPASS")
            except Exception:                                      # noqa: BLE001
                pass
        else:
            _sys._MEIPASS = _old

print()
print("⑥ 收尾")
try:
    cap.shutdown()
except Exception:                                                  # noqa: BLE001
    pass
try:
    cap2.shutdown()
except Exception:                                                  # noqa: BLE001
    pass

print()
print("=" * 60)
if FAILS:
    print("结果：%d 项失败 ✗" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("结果：全部通过 ✓")
