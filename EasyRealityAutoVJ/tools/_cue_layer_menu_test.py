# -*- coding: utf-8 -*-
"""**码表图层右键菜单**回归（用户 2026-10-17：「码表图层的右键菜单应该和其他图层不一样」）。

判据：
  1. 码表图层**不出现**那些对它无效/有害的项 —— 尤其「自动匹配模式」（会重建素材池，
     直接把码表顶掉）和「固定播放本层当前素材」（码表一帧里可能有多个子层）。
  2. 码表图层**必须有**：重命名 / 无声音时隐藏 / Solo / 上移 / 下移 / 卸下码表 / 删除。
  3. 普通图层的菜单**保持原样**（不能被这次改动影响），且**不含**「卸下码表」。
  4. 「卸下码表」走的是 `cue_ui.unbind_layer`（唯一卸下路径），不是直接碰引擎。

做法：把 `ui_main.QMenu` 换成一个"记录型 QMenu"（`exec` 返回 None，什么都不执行），
  于是可以只检查**菜单结构**，不弹窗、不改任何状态。
运行：`python tools/_cue_layer_menu_test.py`（exit 0 = 全过）。
"""
from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu             # noqa: E402

import engine as E                                             # noqa: E402
import ui_main as UM                                           # noqa: E402

FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


class RecMenu(QMenu):
    """记录型 QMenu：收集所有 action 文本/启用态，`exec` 直接返回 None（不执行、不弹窗）。"""

    made = []

    def __init__(self, *a, **kw):
        # 生产里 `QMenu(self)` 的 self 是 MainWindow（QWidget）；本测试的 self 是
        # SimpleNamespace ⇒ 不能当 parent 传，否则 PySide 直接抛 TypeError。
        from PySide6.QtWidgets import QWidget
        if a and not isinstance(a[0], QWidget):
            a = ()
        super().__init__(*a, **kw)
        RecMenu.made.append(self)

    def exec(self, *a, **kw):
        return None

    def texts(self):
        out = []
        for act in self.actions():
            if act.isSeparator():
                continue
            out.append(act.text())
        return out


def menu_of(fn, idx, lay):
    """调一次菜单构造函数，返回它建出来的那个 QMenu 的文本列表。"""
    RecMenu.made.clear()
    holder = types.SimpleNamespace(layers=[lay], engine=types.SimpleNamespace(layers=[lay]))
    holder._cue_layer_menu = types.MethodType(UM.MainWindow._cue_layer_menu, holder)
    holder.layer_menu = types.MethodType(UM.MainWindow.layer_menu, holder)
    holder.layers_panel = types.SimpleNamespace(refresh_header=lambda i: None)
    holder.save_layers = lambda: None
    holder.set_solo = lambda i, v: None
    holder.layer_rename = lambda: None
    holder.layer_del = lambda: None
    holder._cur_layer = 0
    holder.refresh_layer_row = lambda a, b: None
    holder._set_layer_type = lambda i, a: None
    holder._set_layer_role = lambda i, r: None
    holder._copy_layer_pool = lambda i: None
    fn(holder, idx, None)
    if not RecMenu.made:
        return []
    return RecMenu.made[0].texts()


def main():
    app = QApplication.instance() or QApplication([])
    UM.QMenu = RecMenu                      # 只替换 ui_main 里看到的名字

    cue = E.Layer("码表层")
    cue.is_cuesheet = True
    cue.cuesheet_id = "测试"
    normal = E.Layer("普通层")

    print("A. 码表图层菜单")
    cue_txt = menu_of(lambda h, i, g: UM.MainWindow.layer_menu(h, i, g), 0, cue)
    print("     →", cue_txt)
    must_have = ["重命名…", "无声音时隐藏本层", "Solo", "上移", "下移", "删除图层"]
    for s in must_have:
        ck("有「%s」" % s, s in cue_txt)
    ck("有「卸下码表」相关项",
       any("卸下码表" in t for t in cue_txt), "实际 %s" % (cue_txt,))
    forbid = ["固定播放本层当前素材", "自动匹配模式", "图层角色", "播放速度",
              "锁定画面", "保留透明通道", "复制素材池到新图层", "重置本层设置"]
    for s in forbid:
        ck("没有「%s」（对码表无效/有害）" % s, s not in cue_txt)

    print("B. 普通图层菜单（必须保持原样）")
    nor_txt = menu_of(lambda h, i, g: UM.MainWindow.layer_menu(h, i, g), 0, normal)
    print("     →", nor_txt)
    for s in ["重命名…", "固定播放本层当前素材", "自动匹配模式", "图层角色",
              "播放速度", "锁定画面", "保留透明通道", "上移", "下移", "删除图层"]:
        ck("普通层仍有「%s」" % s, s in nor_txt)
    ck("普通层**没有**「卸下码表」",
       not any("卸下码表" in t for t in nor_txt), "实际 %s" % (nor_txt,))

    print("C. 卸下走唯一路径")
    src = open(os.path.join(SRC, "ui_main.py"), encoding="utf-8").read()
    i = src.index("def _cue_layer_menu")
    seg = src[i: src.index("def layer_menu", i)]
    ck("调用了 cue_ui.unbind_layer", "unbind_layer" in seg)
    code = "\n".join(ln.split("#", 1)[0] for ln in seg.splitlines())
    ck("没有直接碰引擎的 clear_cue_sheet", "clear_cue_sheet" not in code)

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 码表图层用的是独立菜单，普通图层菜单未受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
