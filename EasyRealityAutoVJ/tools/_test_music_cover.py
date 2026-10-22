# -*- coding: utf-8 -*-
"""曲库页自检（第二轮）：① 封面提取与缓存 ② 页面真的铺满 ③ 曲风映射入口在右上角。

为什么单独写：第一版只测了「排序/筛选生效」，而这三点是用户第二轮反馈的：
  · 封面没显示
  · 「整个 UI 的显示区域」没占满（实测根因：页布局末尾的 addStretch 在跟面板抢高度）
  · 曲风映射要放右上角

用法：venv/Scripts/python.exe -u tools/_test_music_cover.py
"""
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


import numpy as np                                                # noqa: E402
import cv2                                                        # noqa: E402
import music_cover as mc                                          # noqa: E402

TMP = tempfile.mkdtemp(prefix="autovj_cover_test_")
print("临时目录：%s" % TMP)

# ---------------------------------------------------------------- 造夹具
# 一张 300x200 的假封面（非正方形，用来验证「居中裁方」）
_jpg = os.path.join(TMP, "cover.jpg")
_img = np.zeros((200, 300, 3), dtype=np.uint8)
_img[:, :100] = (40, 40, 220)      # BGR 红
cv2.imencode(".jpg", _img)[1].tofile(_jpg)
_song = os.path.join(TMP, "Some Artist - Some Track.flac")
open(_song, "wb").close()          # 空文件：内嵌封面必然取不到，逼它走「同目录图片」

print()
print("① 封面提取（同目录图片兜底）")
got = mc.extract(_song)
check("提取成功", bool(got), got)
check("缓存文件存在", bool(got) and os.path.isfile(got))
if got:
    im = cv2.imdecode(np.fromfile(got, dtype=np.uint8), cv2.IMREAD_COLOR)
    check("尺寸 = COVER_SIZE 见方", im is not None and im.shape[0] == mc.COVER_SIZE
          and im.shape[1] == mc.COVER_SIZE, None if im is None else im.shape)
check("二次调用命中缓存（不重复解码）", mc.extract(_song) == got)
check("cached() 只查缓存", mc.cached(_song) == got)

print()
print("② 缓存键会随文件变化失效")
import time                                                        # noqa: E402
old_key = mc.cover_path(_song)
time.sleep(1.1)
with open(_song, "ab") as f:
    f.write(b"x" * 32)
os.utime(_song, None)
new_key = mc.cover_path(_song)
check("mtime/size 变了 ⇒ 缓存路径变了", old_key != new_key)

print()
print("③ 无任何封面来源 ⇒ 返回 None（UI 用占位图）")
bare = os.path.join(TMP, "bare")
os.makedirs(bare, exist_ok=True)
_b2 = os.path.join(bare, "no-art.flac")
open(_b2, "wb").close()
check("返回 None", mc.extract(_b2) is None)

print()
print("④ 真实曲库抽样（有就测，没有就跳过）")
real = []
try:
    import json
    for cp in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json"),
               os.path.join(_ROOT, "config.json")):
        if os.path.isfile(cp):
            d = json.load(open(cp, encoding="utf-8"))
            real = [p for p in (d.get("music_library") or []) if os.path.isfile(p)]
            if real:
                break
except Exception:
    real = []
if real:
    t0 = time.perf_counter()
    hit = 0
    for p in real[:20]:
        if mc.extract(p):
            hit += 1
    dt = time.perf_counter() - t0
    check("前 20 首提取命中率 >= 80%", hit >= 16, "%d/20" % hit)
    check("速度可接受（<= 120ms/首）", dt / 20 <= 0.12, "%.0f ms/首" % (dt / 20 * 1000))
else:
    print("  （本机没有可用曲库，跳过）")

# ---------------------------------------------------------------- UI 部分
print()
print("⑤ 页面铺满 + 曲风映射在右上角")
from PySide6.QtWidgets import QApplication, QPushButton            # noqa: E402
app = QApplication.instance() or QApplication([])
import ui_main                                                     # noqa: E402


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
w = ui_main.MainWindow()
w.save_cfg = lambda *a, **k: None
w.save_layers = lambda *a, **k: None
w.cfg.save = lambda *a, **k: None
w.resize(1600, 900)
w.show()
w.switch_page(2)
for _ in range(6):
    app.processEvents()

page = w.pages.currentWidget()
panel = w.music_panel
lst = panel.list

check("曲库面板高度 >= 页面 60%（铺满，不再是一半）",
      panel.height() >= page.height() * 0.60,
      "面板 %d / 页面 %d" % (panel.height(), page.height()))
check("列表高度 >= 页面 60%", lst.height() >= page.height() * 0.60,
      "列表 %d / 页面 %d" % (lst.height(), page.height()))
check("页布局末尾没有多余的 addStretch（否则会跟面板抢高度）",
      page.layout().stretch(page.layout().count() - 1) == 0
      or page.layout().itemAt(page.layout().count() - 1).spacerItem() is None,
      "末项 stretch=%d" % page.layout().stretch(page.layout().count() - 1))

btn = getattr(w, "btn_p2_gvmap", None)
check("存在「曲风映射…」按钮", btn is not None)
if btn is not None and page.width() > 0:
    cx = btn.mapTo(page, btn.rect().center()).x()
    check("按钮在页面右侧（中心 x > 页面 70%）", cx > page.width() * 0.70,
          "x=%d / 宽 %d" % (cx, page.width()))
    check("按钮在上方（中心 y < 页面 20%）",
          btn.mapTo(page, btn.rect().center()).y() < page.height() * 0.20,
          "y=%d" % btn.mapTo(page, btn.rect().center()).y())

print()
print("⑥ 列表带封面图标")
if lst.count():
    with_icon = sum(1 for i in range(lst.count()) if not lst.item(i).icon().isNull())
    check("每一项都有图标（封面或占位图）", with_icon == lst.count(),
          "%d / %d" % (with_icon, lst.count()))
    check("图标尺寸已设成 COVER_ICON", lst.iconSize().width() == 64,
          lst.iconSize().width())
else:
    print("  （曲库为空，跳过）")

print()
import panels as pn                                                # noqa: E402
print("⑦ 右键「封面大小」（用户 2026-10-01 要求）")
from PySide6.QtGui import QAction                                      # noqa: E402
sizes = [px for _l, px in pn.COVER_SIZES]
check("档位至少有 3 档", len(sizes) >= 3, pn.COVER_SIZES)
check("默认档在档位表里", pn.COVER_ICON in sizes, pn.COVER_ICON)
before = lst.iconSize().width()
target = [x for x in sizes if x != before][-1]
panel.set_cover_size(target)
for _ in range(4):
    app.processEvents()
check("set_cover_size 立即改了图标尺寸", panel.list.iconSize().width() == target,
      "before=%d after=%d" % (before, panel.list.iconSize().width()))
check("写进了配置（下次启动记得住）",
      int(w.cfg["ui"].get("music_cover_size", 0) or 0) == target,
      w.cfg["ui"].get("music_cover_size"))
with_icon = sum(1 for i in range(lst.count()) if not lst.item(i).icon().isNull())
check("换尺寸后每行仍有图标", with_icon == lst.count(), "%d/%d" % (with_icon, lst.count()))
w.cfg["ui"]["music_cover_size"] = 9999        # 非法值
check("非法配置值回退默认档", panel._load_cover_size() == pn.COVER_ICON,
      panel._load_cover_size())

print()
print("⑧ 右键菜单里有「封面大小」子菜单且当前档打勾")
menu_holder = {}
_real_menu = pn.QMenu


class _FakeMenu(_real_menu):
    def __init__(self, *a2, **k2):
        super().__init__(*a2, **k2)
        menu_holder["menu"] = self

    def exec(self, *a2, **k2):
        return None            # 不真的弹出来（离屏环境会卡住）


pn.QMenu = _FakeMenu
try:
    if lst.count():
        panel._ctx_menu(lst.visualItemRect(lst.item(0)).center())
finally:
    pn.QMenu = _real_menu
m = menu_holder.get("menu")
sub = None
if m is not None:
    for act in m.actions():
        if act.menu() is not None and act.text() == pn.T("封面大小"):
            sub = act.menu()
            break
check("菜单里有「封面大小」子菜单", sub is not None)
if sub is not None:
    acts = sub.actions()
    check("子菜单档位数与 COVER_SIZES 一致", len(acts) == len(pn.COVER_SIZES),
          "%d 个" % len(acts))
    check("每个都是可勾选项", all(x.isCheckable() for x in acts))
    cur = panel.cover_size
    checked = [x for x in acts if x.isChecked()]
    check("当前档被打勾", len(checked) == 1 and "(%d px)" % cur in checked[0].text(),
          checked[0].text() if checked else "无")

print()
print("⑨ 占位图不重复生成（几百行列表不能每行现画）")
a = pn.cover_placeholder(64)
b = pn.cover_placeholder(64)
check("同一尺寸返回同一个对象", a is b)

try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass

print()
print("=" * 60)
if FAILS:
    print("结果：%d 项失败 ✗" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("结果：全部通过 ✓")
