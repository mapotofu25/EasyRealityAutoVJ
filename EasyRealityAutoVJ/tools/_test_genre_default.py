# -*- coding: utf-8 -*-
"""默认曲风映射自检：138 个曲风**一个都不许空**，且只许用真实存在的画面标签。

用法：venv/Scripts/python.exe -u tools/_test_genre_default.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


import genre_keywords as gk                                     # noqa: E402
import genre_visual_default as gvd                              # noqa: E402
from tags_def import ALL_TAGS                                   # noqa: E402
import match_engine as me                                       # noqa: E402

VALID = {t[0] if isinstance(t, (tuple, list)) else t for t in ALL_TAGS}
ALL_GENRES = [g for _f, names in gk.GENRE_GROUPS for g in (names or [])]
FAMILIES = [f for f, _n in gk.GENRE_GROUPS]

print("① 词表本身")
check("曲风按大类齐全（>=130 个）", len(ALL_GENRES) >= 130, "%d 个 / %d 个大类"
      % (len(ALL_GENRES), len(FAMILIES)))
check("各大类都有底子", all(f in gvd.FAMILY_BASE for f in FAMILIES),
      "缺：" + str([f for f in FAMILIES if f not in gvd.FAMILY_BASE]))

print()
print("② 官方校验函数")
ok, probs = gvd.validate()
check("validate() 通过", ok, probs[:3])

print()
print("③ 覆盖度：一个都不许空")
d = gvd.build_default()
check("每个曲风都有条目", set(ALL_GENRES) <= set(d),
      "缺 " + str(sorted(set(ALL_GENRES) - set(d))[:6]))
empty = [g for g in ALL_GENRES if not (d.get(g) or [])]
check("没有空条目", not empty, empty[:6])

print()
print("④ 只用真实存在的画面标签")
bad = sorted({t for tags in d.values() for t in tags if t not in VALID})
check("标签全部合法", not bad, bad[:6])
dyn = {"低动态", "中动态", "高动态", "频闪"}
check("不含动态标签（那四个由能量驱动）",
      not ({t for tags in d.values() for t in tags} & dyn))

print()
print("⑤ 实际生效表（match_engine）")
m = me.get_genre_visual_map()
check("每个曲风在生效表里都非空",
      all(m.get(g) for g in ALL_GENRES),
      [g for g in ALL_GENRES if not m.get(g)][:6])
# 精调过的宽泛键必须原样保留（不能被默认表覆盖掉）
tuned_ok, tuned_bad = True, []
for g, vis in me.GENRE_TO_VISUAL.items():
    if set(m.get(g) or ()) != set(vis):
        tuned_ok = False
        tuned_bad.append((g, sorted(m.get(g) or ()), sorted(vis)))
check("GENRE_TO_VISUAL 的 33 个精调条目未被改动", tuned_ok, tuned_bad[:3])

print()
print("⑥ 匹配链路仍能命中（大小写/细分词都要行）")
for probe, expect_any in (("hard trance", {"宇宙", "星空", "极光", "星轨", "梦幻"}),
                          ("hardstyle", None),
                          ("techno", None),
                          ("liquid dnb", None)):
    got = me.genre_to_visual([probe])
    ok = bool(got)
    if expect_any:
        ok = ok and bool(got & expect_any)
    check("genre_to_visual(%r) 有结果" % probe, ok, sorted(got)[:5])

print()
print("⑦ 按条覆盖语义（**用户真实处境**：cfg 里已存了 38 条快照，其余 100 个不能空）")
_saved = {g: list(m.get(g) or ()) for g in ALL_GENRES[:38] if m.get(g)}
me.set_custom_visual_map(_saved)
m2 = me.get_genre_visual_map()
check("注入 38 条自定义后，条目数仍是 138", len(m2) >= len(ALL_GENRES),
      "%d 条" % len(m2))
check("其余 100 个曲风**仍然非空**（这是修好的关键）",
      all(m2.get(g) for g in ALL_GENRES), [g for g in ALL_GENRES if not m2.get(g)][:5])
_first = ALL_GENRES[0]
check("自定义的那条按用户的值生效（不再被默认顶掉）",
      m2.get(_first) == set(_saved.get(_first) or ()),
      sorted(m2.get(_first) or ()))
me.set_custom_visual_map({_first: []})
check("显式清空 = 该曲风不映射（空集被尊重，不回落默认）",
      me.get_genre_visual_map().get(_first) == set())
me.set_custom_visual_map({})
check("清空自定义 ⇒ 回到纯默认", me.get_genre_visual_map().get(_first) == m.get(_first))

print()
print("⑧ UI 编辑器**真的打开一次**，看 prefill 是不是满的")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication                        # noqa: E402
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
try:
    import panels as _pn
    _pn.QMessageBox = _MB
    w = ui_main.MainWindow()
    w.cfg.save = lambda *a, **k: None
    dlg = _pn.GenreVisualEditDialog(w)
    filled = sum(1 for g in ALL_GENRES if dlg.map.get(g))
    check("编辑器里非空曲风数 = 全部", filled == len(ALL_GENRES),
          "%d / %d" % (filled, len(ALL_GENRES)))
    del w
except Exception as e:                                             # noqa: BLE001
    check("打开 GenreVisualEditDialog", False, e)

print()
print("⑨ 保存只写「与默认的差异」")
try:
    from match_engine import default_genre_visual_map                # noqa: E402
    dflt = default_genre_visual_map()
    # 模拟保存：map = 默认 + 一条改动
    edited = {g: set(t) for g, t in dflt.items()}
    edited[_first] = {"紫色"}
    out = {}
    for g, tags in edited.items():
        t = sorted(tags)
        base = sorted(dflt.get(g) or ())
        if t != base:
            out[g] = t
    check("只写出改动的那一条（不是 138 条全写）", list(out) == [_first],
          "写出 %d 条" % len(out))
except Exception as e:                                             # noqa: BLE001
    check("差异保存逻辑", False, e)

print()
print("=" * 60)
if FAILS:
    print("结果：%d 项失败 ✗" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("结果：全部通过 ✓")
