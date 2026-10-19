# -*- coding: utf-8 -*-
"""图层顺序规则（用户 2026-10-17）：

  · **码表图层新建时置顶——但必须在 KV 之下**（"除 KV 以外的最上层"）
  · **KV 图层永远强制置顶**（新建/复制/上移/删除/读档 都不能把它挤下去）

为什么必须单独测：KV 是"待机层"（无声音切入、有声音切出）。它一旦被别的图层盖住，
就等于**失效**，而界面上看不出任何异常（图层还在，只是被压住了）——这类"看不见的功能失效"
只能靠断言钉住。

被测的是**正式代码里那两个方法**（`MainWindow._layer_insert_index` / `_enforce_kv_top`），
不是复制品；用 `type(...)` 把未绑定函数挂到假宿主上，避开真实例化 Qt 主窗口。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

OK, FAIL = [], []


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                           ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


class L:
    """最小图层替身（只需要 is_kv / 名字 / 身份）。"""

    def __init__(self, name, is_kv=False):
        self.name = name
        self.is_kv = is_kv

    def __repr__(self):
        return "<%s%s>" % (self.name, "*KV" if self.is_kv else "")


def host(layers, cur=0):
    """把 MainWindow 的两个方法原样挂到假宿主上（测的仍是正式代码）。"""
    import ui_main
    H = type("FakeHost", (), {
        "_layer_insert_index": staticmethod(ui_main.MainWindow._layer_insert_index),
        "_enforce_kv_top": ui_main.MainWindow._enforce_kv_top,
    })
    h = H()
    h.engine = type("E", (), {"layers": layers})()
    h._cur_layer = cur
    return h


def names(layers):
    return [getattr(l, "name", "?") for l in layers]


def insert_and_enforce(is_kv, is_cue, layers, cur=0):
    """按 `layer_add` 的真实顺序：先算插入位置 → 插 → 强制 KV 置顶。"""
    h = host(layers, cur)
    new = L("新建", is_kv=is_kv)
    at = h._layer_insert_index(is_kv, is_cue, len(layers))
    if at is None:
        layers.append(new)
    else:
        layers.insert(at, new)
    h._enforce_kv_top()
    return h, new


def main():
    # ================= ① 插入位置规则 =================
    print("\n===== ① 新建时的插入位置 =====")
    chk(host([])._layer_insert_index(True, False, 5) == 0, "KV 图层 ⇒ 插到 0")
    chk(host([])._layer_insert_index(False, True, 5) == 0, "码表图层 ⇒ 插到 0（随后被 KV 顶到 1）")
    chk(host([])._layer_insert_index(False, False, 5) is None, "普通图层 ⇒ None（追加到末尾）")

    # ================= ② 码表图层：KV 之下、其它之上 =================
    print("\n===== ② 码表图层新建 ⇒ 除 KV 以外的最上层 =====")
    ls = [L("KV", True), L("图层A"), L("图层B")]
    h, new = insert_and_enforce(False, True, ls)
    chk(names(ls) == ["KV", "新建", "图层A", "图层B"], "顺序 = KV, 码表, A, B", names(ls))
    chk(ls[0].is_kv and ls[1] is new, "KV 仍在 0，新码表层在 1", names(ls))
    chk(h._cur_layer == 1, "选中项跟到新图层（index 1）", h._cur_layer)

    # 没有 KV 时，码表层就是最上层
    ls2 = [L("图层A"), L("图层B")]
    h2, new2 = insert_and_enforce(False, True, ls2)
    chk(names(ls2) == ["新建", "图层A", "图层B"], "无 KV ⇒ 码表层在 0", names(ls2))

    # 普通图层仍在最底（老行为不变）
    ls3 = [L("KV", True), L("图层A")]
    h3, new3 = insert_and_enforce(False, False, ls3)
    chk(names(ls3) == ["KV", "图层A", "新建"], "普通图层 ⇒ 仍追加到最底", names(ls3))

    # ================= ③ KV 永远置顶 =================
    print("\n===== ③ 任何操作后 KV 都必须回到 0 =====")
    # ③-a 读档顺序错乱：KV 在中间
    ls = [L("图层A"), L("图层B"), L("KV", True), L("图层C")]
    h = host(ls, cur=0)
    h._enforce_kv_top()
    chk(names(ls) == ["KV", "图层A", "图层B", "图层C"], "读档把 KV 放中间 ⇒ 纠正到 0", names(ls))
    chk(h._cur_layer == 1, "选中项按对象身份跟随（A 从 0 → 1）", h._cur_layer)

    # ③-b 上移/拖动交换把某层顶到 KV 之上
    ls = [L("KV", True), L("图层A")]
    h = host(ls, cur=1)
    ls[0], ls[1] = ls[1], ls[0]          # 模拟 refresh_layer_row 的交换
    h._enforce_kv_top()
    chk(names(ls) == ["KV", "图层A"], "交换后 KV 回 0（表现为 KV 不能被越过）", names(ls))

    # ③-c 复制图层插在 0（模拟 _copy_layer_pool 插到 KV 上面）
    ls = [L("KV", True), L("图层A")]
    h = host(ls, cur=1)
    ls.insert(0, L("副本"))
    h._enforce_kv_top()
    chk(names(ls) == ["KV", "副本", "图层A"], "复制插到 0 也被纠正", names(ls))

    # ③-d 删除 KV 前面的层（KV 自然来到 0，不该被移动）
    ls = [L("图层A"), L("KV", True)]
    h = host(ls, cur=1)
    ls.pop(0)
    before = id(ls[0])
    h._enforce_kv_top()
    chk(names(ls) == ["KV"] and id(ls[0]) == before, "KV 已在 0 ⇒ 不动（幂等）", names(ls))

    # ③-e 没有 KV
    ls = [L("A"), L("B")]
    h = host(ls, cur=1)
    h._enforce_kv_top()
    chk(names(ls) == ["A", "B"] and h._cur_layer == 1, "无 KV ⇒ 完全不动", names(ls))

    # ③-f 幂等：连续调用 3 次，顺序不变
    ls = [L("A"), L("KV", True), L("B")]
    h = host(ls)
    for _ in range(3):
        h._enforce_kv_top()
    chk(names(ls) == ["KV", "A", "B"], "连续 3 次 ⇒ 顺序稳定", names(ls))

    # ================= ④ KV 唯一性前提 =================
    print("\n===== ④ 多个 is_kv（脏数据）时只保证第一个到顶 =====")
    ls = [L("A"), L("KV1", True), L("B"), L("KV2", True)]
    h = host(ls)
    h._enforce_kv_top()
    chk(names(ls)[0] == "KV1", "第一个 KV 被提到 0（其余保持相对顺序）", names(ls))

    print("\n" + "=" * 74)
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("RESULT：CUE_LAYER_ORDER_FAIL")
        return 1
    print("RESULT：CUE_LAYER_ORDER_OK  码表图层置顶（KV 之下）/ KV 永远置顶 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
