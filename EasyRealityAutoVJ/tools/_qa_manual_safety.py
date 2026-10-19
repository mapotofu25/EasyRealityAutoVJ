# -*- coding: utf-8 -*-
"""任务C：证伪「重扫不碰用户手动标签」。逐行复刻 DynTagScanThread.run() 的标签处理逻辑
（源: src/ui_main.py:1338-1404），作用于真实 MediaItem 对象，断言不变量。"""
import sys, os, shutil, tempfile
import atexit
sys.path.insert(0, "src")
import tags_def as td
from media_manager import MediaItem

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("  " + str(extra) if extra else ""))
    if not cond:
        ok_all = False

from tags_def import (DYNAMIC_TAGS, DYNAMIC_FLICKER, COVERAGE_TAGS,
                      COVERAGE_FULL, COVERAGE_FULL_THRESHOLD,
                      BRIGHT_DARK, BRIGHT_MID, BRIGHT_LIGHT,
                      BRIGHTNESS_DARK_THRESHOLD, BRIGHTNESS_BRIGHT_THRESHOLD)
from tagger import SUBJECT_TAGS

# 造一个假素材（夹具目录在进程退出时回收——新铁律：mkdtemp 必须清理）
tmp = tempfile.mkdtemp(prefix="qa_manual_")
atexit.register(shutil.rmtree, tmp, True)     # True=ignore_errors
p = os.path.join(tmp, "fake.mov")
open(p, "wb").close()
m = MediaItem(p)
m.kind = "video"
# 用户手动标签 + 禁用标签（含禁用「暗基调」）
m.tags = ["人物", "低动态", "昏暗", "明亮", "铺满"]   # 自动标签，含旧亮度词
m.manual_tags = ["我手动加的", "暗基调"]              # 手动（暗基调被手动加了）
m.disabled_tags = ["暗基调", "铺满"]                  # 用户禁掉了暗基调

snap_manual = list(m.manual_tags)
snap_disabled = list(m.disabled_tags)

print("初始:")
print("  tags         =", m.tags)
print("  manual_tags  =", m.manual_tags)
print("  disabled_tags=", m.disabled_tags)
print("  all_tags()   =", m.all_tags())

# ---- 复刻 DynTagScanThread.run() 内部的标签处理（med 提供为 5.0 → 应判暗基调，
#      但暗基调在 disabled 里 ⇒ 不得写入）----
def reprocess(m, motion, flicker, med, cov=None):
    dis = set(m.disabled_tags or [])
    if motion:
        tags = [t for t in m.tags if t not in DYNAMIC_TAGS]
        if motion not in dis:
            tags.append(motion)
        if flicker and DYNAMIC_FLICKER not in dis:
            tags.append(DYNAMIC_FLICKER)
        if med is not None:
            _old_bright = {BRIGHT_DARK, BRIGHT_MID, BRIGHT_LIGHT, "明亮", "昏暗", "中等"}
            tags = [t for t in tags if t not in _old_bright]
            bword = (BRIGHT_DARK if med < BRIGHTNESS_DARK_THRESHOLD
                     else (BRIGHT_LIGHT if med >= BRIGHTNESS_BRIGHT_THRESHOLD else BRIGHT_MID))
            if bword not in dis:
                tags.append(bword)
        m.tags = tags
    if cov is not None:
        tags = [t for t in m.tags if t not in COVERAGE_TAGS]
        word = COVERAGE_FULL if cov >= COVERAGE_FULL_THRESHOLD else "局部"
        if word not in dis:
            tags.append(word)
        m.tags = tags

# med=5 → 暗基调 (被禁用，不得复活)；cov=0.9 → 铺满 (被禁用，不得复活)
reprocess(m, motion="低动态", flicker=False, med=5.0, cov=0.9)

print("重扫后:")
print("  tags         =", m.tags)
print("  manual_tags  =", m.manual_tags)
print("  disabled_tags=", m.disabled_tags)
print("  all_tags()   =", m.all_tags())

check("manual_tags 完全没变", m.manual_tags == snap_manual, m.manual_tags)
check("disabled_tags 完全没变", m.disabled_tags == snap_disabled, m.disabled_tags)
check("被禁的「暗基调」未重新加进 m.tags", BRIGHT_DARK not in m.tags, m.tags)
check("被禁的「铺满」未重新加进 m.tags", "铺满" not in m.tags, m.tags)
check("旧亮度词「昏暗」「明亮」已从 m.tags 剔除",
      "昏暗" not in m.tags and "明亮" not in m.tags, m.tags)
check("「暗基调」仍在 manual_tags（手动保留）", BRIGHT_DARK in m.manual_tags, m.manual_tags)
at = m.all_tags()
check("all_tags() 含手动词「我手动加的」", "我手动加的" in at, at)
check("all_tags() 不含被禁的「暗基调」", BRIGHT_DARK not in at, at)
check("all_tags() 不含被禁的「铺满」", "铺满" not in at, at)

# ---- 对照：med=5 但未禁用 → 应落「暗基调」----
m2 = MediaItem(p); m2.kind = "video"
m2.tags = ["人物"]
reprocess(m2, motion="低动态", flicker=False, med=5.0, cov=None)
check("未禁用的 med=5 → 落「暗基调」（正例）", BRIGHT_DARK in m2.tags, m2.tags)

# ---- 对照：med=100 → 亮基调 ----
m3 = MediaItem(p); m3.kind = "video"
m3.tags = ["人物"]
reprocess(m3, motion="低动态", flicker=False, med=100.0, cov=None)
check("med=100 → 落「亮基调」（正例）", BRIGHT_LIGHT in m3.tags, m3.tags)

# ---- 关键：medi 为 None → 保留原亮度词不动 ----
m4 = MediaItem(p); m4.kind = "video"
m4.tags = ["人物", "低动态", "暗基调"]
reprocess(m4, motion="低动态", flicker=False, med=None, cov=None)
check("med=None → 原亮度词「暗基调」保留不动", "暗基调" in m4.tags, m4.tags)

# ---- motion='' （打不开）→ 保持原标签不动 ----
m5 = MediaItem(p); m5.kind = "video"
m5.tags = ["人物", "昏暗"]
reprocess(m5, motion="", flicker=False, med=None, cov=None)
check("motion='' → 标签完全不动", m5.tags == ["人物", "昏暗"], m5.tags)

print("=" * 50)
print("任务C 结论:", "全部 PASS" if ok_all else "存在 FAIL")
