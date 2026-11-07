# -*- coding: utf-8 -*-
"""④ 片段属性：新增字段与三项联动（2026-11-03）。

用户原话：「这个片段属性的东西怎么这么少啊」。
本文件盯住**新加的东西真的能用**，以及过程中修掉的两个真 bug：

  A. 字段齐不齐：结束 / 旋转 / 只读信息（素材时长、透明通道、素材文件、所在层、段落、ID）
  B. 起点 ↔ 时长 ↔ 结束 **三者联动**（改任一个，另外两个立刻自洽）
  C. `commit()` **不能抹掉 `rot`**（旧写法整块重建 `xform`，一改大小旋转就归零）
  D. 混合 / 不透明度**必须登记进 `_fields`**（旧版没登记 ⇒ `_reflow` 不重排 ⇒ 被备注盖住）
  E. 只读信息内容正确（含"素材文件缺失"这个现场最需要的判断）

跑法：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe tests\\test_props_fields.py
    ... --reverse   # 用改前的写法跑 ⇒ 必须变红
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication                            # noqa: E402

from ercue.model.cuesheet import CompLayer, CueItem, MediaRef           # noqa: E402
from ercue.model.cuesheet import CueSheet                              # noqa: E402
from ercue.ui.props_panel import PropsPanel                            # noqa: E402

REVERSE = "--reverse" in sys.argv
_APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if REVERSE and name.startswith(("(a4)", "(a5)", "(c1)", "(c2)", "(d1)")):
        cond = not cond          # 这几组是"修复后才成立"的，反向必须反过来才说明测对了
        detail = ("←反向：" + str(detail)) if detail else "←反向"
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def make_panel():
    p = PropsPanel()
    p.resize(520, 560)
    ly = CompLayer(lyr_id="L1", name="层 1", z=0)
    it = CueItem(item_id="i1", clip_id="c1", at_sec=10.0, end_sec=22.0,
                 src_in_sec=1.0, src_out_sec=None, speed=1.0,
                 xform={"scale": 1.0, "x": 0.0, "y": 0.0},
                 trans_in="fade", trans_in_sec=2.0, trans_out="fade", trans_out_sec=2.0)
    ly.items.append(it)
    sheet = CueSheet(media=[MediaRef(clip_id="c1", name="a.mp4",
                                     duration_sec=123.6, has_alpha=True)])
    p.set_media_list(sheet, {"c1": os.path.abspath(__file__)})   # 这个文件一定存在
    return p, ly, it


def main():
    print("=" * 78)
    print("【A】字段齐不齐（用户：「东西怎么这么少」）")
    print("=" * 78)
    p, ly, it = make_panel()
    labels = [lb.text() for lb, _w, _g in p._fields]
    print("  字段（%d 个）：%s" % (len(labels), " / ".join(x or "·" for x in labels)))
    check(len(labels) >= 25, "(a1) 字段数 ≥ 25（补完前是 17）", len(labels))
    for nm in ("结束", "旋转", "素材时长", "透明通道", "素材文件", "所在层", "段落", "片段 ID"):
        check(nm in labels, "(a2) 有「%s」" % nm)
    check("本素材混合" in labels and "本素材不透明度" in labels,
          "(a3) 混合 / 不透明度仍在")
    # D：这两项必须在 `_fields` 里（否则 _reflow 不重排 ⇒ 被备注盖住）
    in_fields = {id(lb) for lb, _w, _g in p._fields}
    check(id(p.lbl_blend) in in_fields and id(p.lbl_op) in in_fields,
          "(d1) 混合/不透明度**已登记**进 `_fields`（旧版没登记 ⇒ 被备注盖住）")
    # 行号不能撞车（撞车 = 互相覆盖）
    rows = sorted(p._grid.getItemPosition(p._grid.indexOf(lb))[0]
                  for lb, _w, _g in p._fields)
    check(len(rows) == len(set(rows)) or getattr(p, "_ncol_now", 1) > 1,
          "(d2) 单列时每个字段都占独立行（不重叠）", rows[:6])

    print()
    print("=" * 78)
    print("【B】起点 ↔ 时长 ↔ 结束 三者联动")
    print("=" * 78)
    p.set_selection(ly, it)
    check(abs(p.sp_at.value() - 10.0) < 1e-6 and abs(p.sp_len.value() - 12.0) < 1e-6
          and abs(p.sp_end.value() - 22.0) < 1e-6,
          "(b1) 回填：起点 10 / 时长 12 / 结束 22",
          (p.sp_at.value(), p.sp_len.value(), p.sp_end.value()))
    p.sp_len.setValue(30.0)                      # 改时长
    check(abs(p.sp_end.value() - 40.0) < 1e-6,
          "(b2) 改时长 12→30 ⇒ 结束自动变 40", p.sp_end.value())
    p.sp_end.setValue(60.0)                      # 改结束
    check(abs(p.sp_len.value() - 50.0) < 1e-6,
          "(b3) 改结束 40→60 ⇒ 时长自动变 50", p.sp_len.value())
    p.sp_at.setValue(20.0)                       # 改起点（时长不变，结束跟着走）
    check(abs(p.sp_len.value() - 50.0) < 1e-6 and abs(p.sp_end.value() - 70.0) < 1e-6,
          "(b4) 改起点 10→20 ⇒ 时长仍 50、结束变 70",
          (p.sp_len.value(), p.sp_end.value()))
    p.sp_end.setValue(20.0)                      # 结束 = 起点 ⇒ 时长 0（到下一段）
    check(abs(p.sp_len.value()) < 1e-6, "(b5) 结束 ≤ 起点 ⇒ 时长 0（= 到下一段）",
          p.sp_len.value())
    p.commit()
    check(it.end_sec is None, "(b6) 时长 0 ⇒ 存 end_sec=None（到下一段）", it.end_sec)

    print()
    print("=" * 78)
    print("【C】commit 必须保住 rot（旧写法整块重建 xform ⇒ 旋转被清零）")
    print("=" * 78)
    it.xform = {"scale": 1.5, "x": 5.0, "y": -3.0, "rot": 37.0}
    p.set_selection(ly, it)
    check(abs(p.sp_rot.value() - 37.0) < 1e-6, "(c1) 回填旋转 37°", p.sp_rot.value())
    p.sp_scale.setValue(2.0)                     # 只改大小
    p.commit()
    check(abs(float(it.xform.get("rot") or 0.0) - 37.0) < 1e-6,
          "(c2) 改大小后 rot 仍是 37°（不被抹掉）", it.xform)
    check(abs(float(it.xform.get("scale")) - 2.0) < 1e-6,
          "(c3) 大小确实写进去了 2.0", it.xform.get("scale"))
    p.sp_rot.setValue(0.0)                       # 归零 ⇒ 不落键（与旧文件一致）
    p.commit()
    check("rot" not in it.xform, "(c4) 旋转回 0 ⇒ 不写 rot 键（包体干净）", it.xform)

    print()
    print("=" * 78)
    print("【E】只读信息（现场判断「这段为什么没画面」就靠它）")
    print("=" * 78)
    p.set_selection(ly, it)
    check("123" in p.le_med_dur.text(), "(e1) 素材时长写出来了", p.le_med_dur.text())
    check("有" in p.le_med_alpha.text(), "(e2) 透明通道=有", p.le_med_alpha.text())
    check("已找到" in p.le_med_file.text(), "(e3) 素材文件在 ⇒ 已找到", p.le_med_file.text())
    check("L1" in p.le_layer.text() or "层 1" in p.le_layer.text(),
          "(e4) 所在层带 z", p.le_layer.text())
    check("独立一段" in p.le_run.text(), "(e5) 单段 ⇒ 独立一段", p.le_run.text())
    check(p.le_item_id.text() == "i1", "(e6) 片段 ID", p.le_item_id.text())

    # 缺失路径
    p.set_media_list(CueSheet(media=[MediaRef(clip_id="c1", name="a.mp4",
                                              duration_sec=1.0, has_alpha=False)]),
                     {"c1": os.path.join(_HERE, "不存在的素材.mp4")})
    p.set_selection(ly, it)
    check("缺失" in p.le_med_file.text(), "(e7) 文件不存在 ⇒ 明确写「缺失」",
          p.le_med_file.text())
    check("无" in p.le_med_alpha.text(), "(e8) 无 alpha ⇒ 无", p.le_med_alpha.text())

    # 相邻同设置 ⇒ 段内（⚠ B 段 commit 把 it 的 end 写成了 None、C 段改了 xform，
    #   这里先把它恢复成"紧挨上一段"的样子，否则设置/时间不一致 ⇒ **应该**断段）
    it.at_sec, it.end_sec = 10.0, 22.0
    it.src_in_sec = 0.0          # B 段 commit 把入点写成了 1.0；run 判定含 src_in ⇒ 也要对齐
    it.xform = {"scale": 1.0, "x": 0.0, "y": 0.0}
    it2 = CueItem(item_id="i2", clip_id="c1", at_sec=22.0, end_sec=30.0,
                  src_in_sec=0.0, src_out_sec=None, speed=1.0,
                  xform={"scale": 1.0, "x": 0.0, "y": 0.0},
                  trans_in="fade", trans_in_sec=2.0, trans_out="fade", trans_out_sec=2.0)
    ly.items.append(it2)
    p.set_selection(ly, it2)
    check("段尾" in p.le_run.text() or "段内" in p.le_run.text(),
          "(e9) 与上一段同设置 ⇒ 归到同一段（段尾/段内）", p.le_run.text())

    print()
    print("=" * 78)
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
