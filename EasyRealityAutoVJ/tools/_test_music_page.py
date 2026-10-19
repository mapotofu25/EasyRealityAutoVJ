# -*- coding: utf-8 -*-
"""曲库整页面板自检：左栏排序 / 筛选是否真的生效。

覆盖：① 第 3 页宿主面板、music_dialog 已指向它（这样扫描进度那 5 处代码不用改）
      ② BPM 区间筛选、③ 时长区间筛选、④ 只看文件缺失、⑤ BPM 升序排序
      ⑥ open_music_library 切到第 3 页
用法：venv/Scripts/python.exe -u tools/_test_music_page.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

app = QApplication([])
import ui_main                                                   # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


class _MB:
    @staticmethod
    def warning(*a, **k):
        pass

    @staticmethod
    def information(*a, **k):
        pass


ui_main.QMessageBox = _MB
w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.show()
app.processEvents()

p = getattr(w, "music_panel", None)
print("① 宿主与接口")
check("第 3 页有曲库面板", p is not None)
check("music_dialog 指向该面板（扫描进度代码零改动）", getattr(w, "music_dialog", None) is p)
check("接口齐全（refresh/progress/lbl/btn_scan/list）",
      all(hasattr(p, a) for a in ("refresh", "progress", "lbl", "btn_scan", "list")))

# ★ 用**自造数据**测：源码跑用的 P:////AutoVJ////config.json 是旧格式（没有 grid 键），
#   拿它测筛选等于测空气。这里直接注入 6 首已知 BPM/时长/缺失状态的曲目。
import tempfile as _tf
import shutil as _shutil
import atexit as _atexit
_d = _tf.mkdtemp(prefix="autovj_musictest_")
_atexit.register(_shutil.rmtree, _d, True)   # 新铁律：mkdtemp 必须清理（退出/ sys.exit 时回收）
_tracks = [
    ("a_128_3min.flac", 128.0, 180.0, True),
    ("b_140_6min.flac", 140.0, 360.0, True),
    ("c_175_8min.flac", 175.0, 480.0, True),
    ("d_nogrid.flac", None, None, True),
    ("e_95_1min.flac", 95.0, 70.0, True),
    ("f_missing_130.flac", 130.0, 200.0, False),   # 文件不存在
]
_meta = {}
_lib = []
for _n, _bpm, _dur, _exists in _tracks:
    _p = os.path.join(_d, _n)
    if _exists:
        open(_p, "wb").close()
    _lib.append(_p)
    _m = {"artist": "测试", "genres": ["Hardstyle"]}
    if _bpm or _dur:
        _m["grid"] = {"bpm": _bpm, "dur": _dur}
    _meta[_p] = _m
w.cfg["music_library"] = _lib
w.cfg["music_meta"] = _meta
lib = list(w.cfg["music_library"] or [])
meta = w.cfg["music_meta"] or {}
p.refresh()
app.processEvents()
print("\n② 默认视图")
check("列表条数 = 曲库总数", p.list.count() == len(lib),
      "list=%d lib=%d" % (p.list.count(), len(lib)))
check("状态行有内容", bool(p.lbl.text()), p.lbl.text())


def gridinfo(path):
    g = (meta.get(path) or {}).get("grid") or {}
    try:
        return (float(g.get("bpm")) if g.get("bpm") else None,
                float(g.get("dur")) if g.get("dur") else None)
    except (TypeError, ValueError):
        return (None, None)


print("\n③ BPM 区间")
# 选「无节拍网格」
idx_none = [i for i, (lab, v) in enumerate(p.BPM_FILTERS) if v == "none"][0]
p.cb_bpm.setCurrentIndex(idx_none)
app.processEvents()
exp = sum(1 for x in lib if gridinfo(x)[0] is None)
check("『无节拍网格』条数正确", p.list.count() == exp, "列表=%d 期望=%d" % (p.list.count(), exp))

# 选 128–150
idx_r = [i for i, (lab, v) in enumerate(p.BPM_FILTERS) if v == (128, 150)][0]
p.cb_bpm.setCurrentIndex(idx_r)
app.processEvents()
exp = sum(1 for x in lib if (gridinfo(x)[0] or -1) >= 128 and (gridinfo(x)[0] or -1) < 150)
check("BPM 128–150 条数正确", p.list.count() == exp, "列表=%d 期望=%d" % (p.list.count(), exp))

print("\n④ 时长区间")
p.cb_bpm.setCurrentIndex(0)
idx_d = [i for i, (lab, v) in enumerate(p.DUR_FILTERS) if v == (300, 420)][0]
p.cb_dur.setCurrentIndex(idx_d)
app.processEvents()
exp = sum(1 for x in lib if (gridinfo(x)[1] or -1) >= 300 and (gridinfo(x)[1] or -1) < 420)
check("时长 5–7 分钟条数正确", p.list.count() == exp, "列表=%d 期望=%d" % (p.list.count(), exp))

print("\n⑤ 只看文件缺失")
p.cb_dur.setCurrentIndex(0)
p.chk_missing.setChecked(True)
app.processEvents()
exp = sum(1 for x in lib if not os.path.isfile(x))
check("只看缺失条数正确", p.list.count() == exp, "列表=%d 期望=%d" % (p.list.count(), exp))
p.chk_missing.setChecked(False)
app.processEvents()

print("\n⑥ 排序（BPM 升序：非空值必须单调不减）")
idx_s = [i for i, (lab, k) in enumerate(p.SORTS) if k == "bpm"][0]
p.cb_sort.setCurrentIndex(idx_s)
app.processEvents()
vals = []
for i in range(p.list.count()):
    path = p.list.item(i).data(Qt.UserRole)
    b = gridinfo(path)[0]
    if b is not None:
        vals.append(b)
check("BPM 升序成立", vals == sorted(vals), "前 6 个=%s" % ["%.1f" % v for v in vals[:6]])

print("\n⑦ 重置筛选 / 打开曲库")
p.reset_filters()
app.processEvents()
check("重置后回到全量 + 原顺序", p.list.count() == len(lib)
      and p.cb_sort.currentIndex() == 0 and not p.chk_missing.isChecked())
w.open_music_library()
check("open_music_library 切到第 3 页", w.pages.currentIndex() == 2)

print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
