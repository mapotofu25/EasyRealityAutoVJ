# -*- coding: utf-8 -*-
"""任务E：i18n 一致性。"""
import sys
sys.path.insert(0, "src")
import i18n_map as im
import tags_def as td

ZH2EN = None
for attr in ("ZH2EN", "_ZH2EN", "TRANS"):
    if hasattr(im, attr):
        ZH2EN = getattr(im, attr)
        print("命中词表属性:", attr)
        break
print("词表大小 =", len(ZH2EN))

print("① 亮度三词是否注入显示名")
for zh in td.BRIGHTNESS_TAGS:
    print("   %s -> %r" % (zh, ZH2EN.get(zh)))

print("② COVERAGE 铺满/局部 是否注入显示名")
for zh in td.COVERAGE_TAGS:
    print("   %s -> %r" % (zh, ZH2EN.get(zh)))

print("③ DYNAMIC 动态词（对照，已确认注入）")
from tags_def import DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER
for zh in (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER):
    print("   %s -> %r" % (zh, ZH2EN.get(zh)))

print("④ 关键：匹配/数据层是否会被翻译（T() 是否改数据）")
try:
    from i18n import T
except Exception:
    from i18n_map import T
print("   T('暗基调') =", repr(T("暗基调")))
print("   原值断言: td.BRIGHT_DARK == '暗基调' →", td.BRIGHT_DARK == "暗基调")
