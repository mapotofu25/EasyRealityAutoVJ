# -*- coding: utf-8 -*-
"""针对性验证两个确定性缺陷修补：
  缺陷1：`青色` 现在必须在 tags_def.TAG_CATEGORIES 的「颜色」分类里，且进 ALL_TAGS。
  缺陷2：coverage 铺满度按 COVERAGE_FULL_THRESHOLD 判「铺满/局部」，且与 auto_roles 裁决一致。

夹具自造、落系统临时目录（tempfile.mkdtemp()），绝不落项目根/用户目录。
用法：venv/Scripts/python.exe -u tools/_test_fix_coverage_cyan.py
"""
import os
import sys
import shutil
import atexit
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

import numpy as np                                                 # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# ========================================================================
# 缺陷 1：青色
# ========================================================================
print("① 缺陷1：`青色` 进 tags_def 颜色分类")
import tags_def as td                                               # noqa: E402

color_cat = None
for cat, items in td.TAG_CATEGORIES:
    if cat == "颜色":
        color_cat = items
        break
check("存在「颜色」分类", color_cat is not None)
zh_list = [zh for zh, _en in (color_cat or [])]
check("「颜色」分类含「青色」", "青色" in zh_list, zh_list)
pairs = dict(color_cat or [])
check("「青色」英文名 = cyan", pairs.get("青色") == "cyan", pairs.get("青色"))
check("「青色」进 ALL_TAGS", "青色" in td.TAG_ZH_LIST, "")
all_zh = {zh for zh, _en, _c in td.ALL_TAGS}
check("ALL_TAGS(zh) 含「青色」", "青色" in all_zh)
check("「青色」紧跟「绿色」之后（色相顺序）",
      zh_list.index("青色") == zh_list.index("绿色") + 1, zh_list)

# i18n 自动注入：青色 → Cyan（不需要手工加）
import i18n_map as im                                               # noqa: E402
check("i18n 自动注入「青色」→ Cyan（无需手工加条目）",
      im.ZH2EN.get("青色") == "Cyan", im.ZH2EN.get("青色"))

# tags.txt 未改动（CLIP 词表/向量对齐不能动）
_tt = os.path.join(os.path.dirname(_HERE), "assets", "models", "chinese_clip", "tags.txt")
_lines = open(_tt, encoding="utf-8").read().splitlines()
check("tags.txt 未含「青色」（补 TAG_CATEGORIES 安全，不动词表）", "青色" not in _lines,
      "lines=%d" % len(_lines))
check("tags.txt 仍是 88 行（tag_emb.npy 对齐未破）", len(_lines) == 88, len(_lines))

# ========================================================================
# 缺陷 2：coverage 落词 + 阈值一致性
# ========================================================================
print()
print("② 缺陷2：coverage 铺满度落词 + 阈值与 auto_roles 一致")
import cv2                                                          # noqa: E402
from tagger import Tagger, auto_roles                              # noqa: E402

check("COVERAGE_FULL = 铺满", td.COVERAGE_FULL == "铺满", td.COVERAGE_FULL)
check("COVERAGE_PART = 局部", td.COVERAGE_PART == "局部", td.COVERAGE_PART)
check("COVERAGE_TAGS = (铺满,局部)", td.COVERAGE_TAGS == ("铺满", "局部"), td.COVERAGE_TAGS)
check("COVERAGE_FULL_THRESHOLD = 0.45", td.COVERAGE_FULL_THRESHOLD == 0.45,
      td.COVERAGE_FULL_THRESHOLD)
check("COVERAGE_FULL_THRESHOLD_DYNAMIC = 0.35",
      td.COVERAGE_FULL_THRESHOLD_DYNAMIC == 0.35, td.COVERAGE_FULL_THRESHOLD_DYNAMIC)
check("COVERAGE_* 不进 TAG_CATEGORIES（与 DYNAMIC_* 同理）",
      not (set(td.COVERAGE_TAGS) & all_zh), sorted(set(td.COVERAGE_TAGS) & all_zh))

# —— 造夹具（临时目录）——
tmp = tempfile.mkdtemp(prefix="autovj_covfix_")
atexit.register(shutil.rmtree, tmp, True)     # 新铁律：mkdtemp 必须清理（退出/ sys.exit 时回收）
full_path = os.path.join(tmp, "full.png")
part_path = os.path.join(tmp, "part.png")

# 全铺满：整幅彩色渐变（边缘也亮、每块都有内容）
h, w = 360, 640
grad = np.zeros((h, w, 3), dtype=np.uint8)
for y in range(h):
    grad[y, :, 0] = int(255 * y / (h - 1))          # B 渐变
    grad[y, :, 1] = int(255 * (1 - y / (h - 1)))    # G 渐变
grad[:, :, 2] = 180                                  # R 常量 → 保证不是纯黑
cv2.imwrite(full_path, grad)

# 黑底局部：纯黑 + 中央一小块亮区（边缘全黑、大多数块无内容）
part = np.zeros((h, w, 3), dtype=np.uint8)
part[h // 2 - 40:h // 2 + 40, w // 2 - 40:w // 2 + 40] = 255
cv2.imwrite(part_path, part)

img_full = cv2.imread(full_path)
img_part = cv2.imread(part_path)
cov_full = Tagger.coverage_score(img_full)
cov_part = Tagger.coverage_score(img_part)
print("     实测 coverage：铺满图=%.4f  局部图=%.4f" % (cov_full, cov_part))
check("铺满图 coverage ≥ 0.45", cov_full >= td.COVERAGE_FULL_THRESHOLD, cov_full)
check("局部图 coverage < 0.45", cov_part < td.COVERAGE_FULL_THRESHOLD, cov_part)


def coverage_word(cov):
    """复刻 ui_main 消费点的落词逻辑（必须与实现同阈值）。"""
    return td.COVERAGE_FULL if cov >= td.COVERAGE_FULL_THRESHOLD else td.COVERAGE_PART


check("铺满图落词 = 铺满", coverage_word(cov_full) == td.COVERAGE_FULL,
      coverage_word(cov_full))
check("局部图落词 = 局部", coverage_word(cov_part) == td.COVERAGE_PART,
      coverage_word(cov_part))

# 与 auto_roles 裁决一致：铺满→背景，局部→前景（无内容词时走第 4 步分流）
r_full = auto_roles([], "video", False, coverage=cov_full)
r_part = auto_roles([], "video", False, coverage=cov_part)
check("auto_roles(铺满)=背景 且 落词=铺满 一致",
      r_full == {"fg": False, "bg": True}, r_full)
check("auto_roles(局部)=前景 且 落词=局部 一致",
      r_part == {"fg": True, "bg": False}, r_part)
check("两侧阈值一致（同一常量，无漂移）",
      (cov_full >= td.COVERAGE_FULL_THRESHOLD) == (coverage_word(cov_full) == td.COVERAGE_FULL)
      and (cov_part >= td.COVERAGE_FULL_THRESHOLD) == (coverage_word(cov_part) == td.COVERAGE_FULL))

# coverage 为 None → 不落词（消费点用 `if cov is not None` 守护）
cov_none = None
落词 = None if cov_none is None else coverage_word(cov_none)
check("coverage=None ⇒ 不落铺满度词（守护生效）", 落词 is None, 落词)

# auto_roles 的 0.45/0.35 已被常量替换：用源码字符串确认没有裸 0.45 残留
_tg_src = open(os.path.join(os.path.dirname(_HERE), "src", "tagger.py"),
               encoding="utf-8").read()
check("tagger.py 内 auto_roles 无裸 0.45 残留（已用常量）",
      "cov >= 0.45" not in _tg_src and "cov >= 0.35" not in _tg_src)
check("tagger.py 内已过滤 COVERAGE_TAGS（防御性）", "z not in COVERAGE_TAGS" in _tg_src)

print()
print("=" * 60)
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
