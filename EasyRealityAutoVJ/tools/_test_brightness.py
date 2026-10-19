# -*- coding: utf-8 -*-
"""针对性验证「全片亮度分档」+ 本轮 4 项补丁：
  1) BRIGHTNESS_* 常量齐备、**不进 TAG_CATEGORIES**、阈值唯一定义处；
  2) analyze_dynamic 返 4 元组、med=全片灰度中位数；造图断言四档（含"局部高光不误导"核心正确性）；
  3) 落词逻辑（复刻 ui_main TagScanThread）与 tagger 一致；
  4) 旧「明亮/昏暗/中等」已废除（color_brightness 不再产出）。

本轮补丁追加验证（QA 独立验证发现）：
  ⑦ 图片**非 ASCII 路径**修复：tagger 两处图片分支走 _ascii_safe_path（造中文路径图 → med 正常）；
  ⑧ TagEditDialog 有**亮度三档互斥按钮**（内省 cat_btns/_bright_btns）；
  ⑨ COVERAGE_FULL/PART 已注入 i18n 显示名（Z2EN.get("铺满") 非 None）；
  ⑩ merge_scan_tags **纯函数逐条等价**（含"重扫不碰 manual/disabled"重证）。

夹具自造、落 tempfile.mkdtemp()，绝不落项目根/用户目录。
用法：venv/Scripts/python.exe -u tools/_test_brightness.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))

import numpy as np                                                 # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


import tags_def as td                                               # noqa: E402
import cv2                                                          # noqa: E402
from tagger import Tagger                                           # noqa: E402

# ========================================================================
# ① 常量
# ========================================================================
print("① 亮度常量与策略")
check("BRIGHT_DARK = 暗基调", td.BRIGHT_DARK == "暗基调", td.BRIGHT_DARK)
check("BRIGHT_MID = 中亮度", td.BRIGHT_MID == "中亮度", td.BRIGHT_MID)
check("BRIGHT_LIGHT = 亮基调", td.BRIGHT_LIGHT == "亮基调", td.BRIGHT_LIGHT)
check("BRIGHTNESS_TAGS = (暗基调,中亮度,亮基调)",
      td.BRIGHTNESS_TAGS == ("暗基调", "中亮度", "亮基调"), td.BRIGHTNESS_TAGS)
check("BRIGHTNESS_DARK_THRESHOLD = 15.0", td.BRIGHTNESS_DARK_THRESHOLD == 15.0,
      td.BRIGHTNESS_DARK_THRESHOLD)
check("BRIGHTNESS_BRIGHT_THRESHOLD = 40.0", td.BRIGHTNESS_BRIGHT_THRESHOLD == 40.0,
      td.BRIGHTNESS_BRIGHT_THRESHOLD)
_all_zh = {zh for zh, _e, _c in td.ALL_TAGS}
check("BRIGHTNESS_TAGS 不进 TAG_CATEGORIES",
      not (set(td.BRIGHTNESS_TAGS) & _all_zh), sorted(set(td.BRIGHTNESS_TAGS) & _all_zh))

# 阈值唯一定义处：src 里不出现裸 15.0/40.0 用作亮度分界
import io                                                           # noqa: E402
_src_root = os.path.join(os.path.dirname(_HERE), "src")
_bare = []
for fn in ("tagger.py", "ui_main.py"):
    txt = io.open(os.path.join(_src_root, fn), encoding="utf-8").read()
    for needle in ("< 15.0", ">= 40.0", "< 15)", ">= 40)"):
        if needle in txt:
            _bare.append((fn, needle))
check("tagger.py/ui_main.py 无裸 15.0/40.0 亮度分界（用常量）", not _bare, _bare)

# ========================================================================
# ② 造图 + analyze_dynamic med
# ========================================================================
print()
print("② 造图断言（tempfile.mkdtemp）")
tmp = tempfile.mkdtemp(prefix="autovj_bright_")
# 新铁律：mkdtemp 必须清理。本脚本是线性脚本，用 atexit 保证任意退出路径都回收。
import atexit as _atexit                                          # noqa: E402
import shutil as _shutil                                          # noqa: E402
_atexit.register(lambda: _shutil.rmtree(tmp, ignore_errors=True))
H, W = 512, 512

black = np.zeros((H, W, 3), dtype=np.uint8)
white = np.full((H, W, 3), 255, dtype=np.uint8)
# 暗底 + 中心小亮块（约 2.8% 面积）：局部高光不该把整片拉亮 → 核心正确性
dark_spot = np.zeros((H, W, 3), dtype=np.uint8)
_side = int((W * H * 0.028) ** 0.5)
_c = (W // 2, H // 2)
dark_spot[_c[1] - _side // 2:_c[1] + _side // 2, _c[0] - _side // 2:_c[0] + _side // 2] = 255
# 暗底 + 半屏亮
half = np.zeros((H, W, 3), dtype=np.uint8)
half[:, :W // 2] = 255

cases = [("black.png", black, 0.0, td.BRIGHT_DARK),
         ("white.png", white, 255.0, td.BRIGHT_LIGHT),
         ("darkspot.png", dark_spot, None, td.BRIGHT_DARK),     # med ~7
         ("half.png", half, None, td.BRIGHT_LIGHT)]             # med ~127

paths = {}
for name, arr, _e, _w in cases:
    p = os.path.join(tmp, name)
    cv2.imwrite(p, arr)
    paths[name] = p


def grade(med):
    return (td.BRIGHT_DARK if med < td.BRIGHTNESS_DARK_THRESHOLD
            else (td.BRIGHT_LIGHT if med >= td.BRIGHTNESS_BRIGHT_THRESHOLD
                  else td.BRIGHT_MID))


for name, _arr, expect_med, expect_grade in cases:
    _m, _f, _d, med = Tagger.analyze_dynamic(paths[name], "image")
    g = grade(med) if med is not None else None
    extra = "med=%.4f 档=%s" % (med, g)
    if expect_med is not None:
        # 全黑/全白：灰度均值即期望
        check("%s med≈%.0f" % (name, expect_med), med is not None
              and abs(med - expect_med) < 1.0, extra)
    check("%s → %s" % (name, expect_grade), g == expect_grade, extra if expect_med is None else extra)
    # med 必为 None 或 float，绝不为 -1 之类的哨兵进入落词
    check("%s med 为 None 或数值" % name, med is None or isinstance(med, float), repr(med))

# 局部高光核心正确性：darkspot 的 med 必须仍 < 15（不因中心亮块被拉亮）
_, _, _, med_ds = Tagger.analyze_dynamic(paths["darkspot.png"], "image")
check("★ 局部高光不误导（darkspot med < 15）", med_ds is not None
      and med_ds < td.BRIGHTNESS_DARK_THRESHOLD, "med=%.3f" % med_ds)

# ========================================================================
# ③ 分析一致性：analyze_dynamic(图片) 与直接算灰度均值一致
# ========================================================================
print()
print("③ analyze_dynamic 图片分支 med 与灰度均值一致")
for name in ("black.png", "white.png"):
    img = cv2.imread(paths[name])
    direct = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    _, _, _, med = Tagger.analyze_dynamic(paths[name], "image")
    check("%s 图片 med == 灰度均值" % name, med is not None and abs(med - direct) < 1e-3,
          "med=%.4f mean=%.4f" % (med, direct))

# ========================================================================
# ④ 旧 bright 废除
# ========================================================================
print()
print("④ 旧「明亮/昏暗/中等」已废除")
cb_full = Tagger.color_brightness(np.full((64, 64, 3), 255, dtype=np.uint8))
cb_dark = Tagger.color_brightness(np.zeros((64, 64, 3), dtype=np.uint8))
check("color_brightness 不再产出「明亮」", cb_full.get("bright") is None, cb_full)
check("color_brightness 不再产出「昏暗」", cb_dark.get("bright") is None, cb_dark)
check("color_brightness 仍保留 colors 键", "colors" in cb_full, cb_full.get("colors"))

# ========================================================================
# ⑤ 防御性过滤：analyze_media 返回 brightness 字段 + 过滤 BRIGHTNESS_TAGS
# ========================================================================
print()
print("⑤ analyze_media 字段 + CLIP 防御过滤")
_tg_src = io.open(os.path.join(_src_root, "tagger.py"), encoding="utf-8").read()
check("analyze_media 返回 brightness 字段", '"brightness": med' in _tg_src)
check("analyze_media 过滤 BRIGHTNESS_TAGS", "z not in BRIGHTNESS_TAGS" in _tg_src)
check("analyze_media 不再 append bright（保留键恒 None）", '"bright": cb["bright"]' in _tg_src)
# 不实例化 Tagger（需 onnx 运行时）；用源码断言解包 4 元组。
check("analyze_media 解包 4 元组", "motion, flicker, _diff, med = self.analyze_dynamic" in _tg_src)

# ========================================================================
# ⑥ ui_main 落词/重扫一致性
# ========================================================================
print()
print("⑥ ui_main 落词/重扫一致")
_ui_src = io.open(os.path.join(_src_root, "ui_main.py"), encoding="utf-8").read()
check("落词用 BRIGHTNESS_DARK_THRESHOLD", "brightness < BRIGHTNESS_DARK_THRESHOLD" in _ui_src)
check("落词用 BRIGHTNESS_BRIGHT_THRESHOLD", "brightness >= BRIGHTNESS_BRIGHT_THRESHOLD" in _ui_src)
check("TagScanThread 已删除 append(r[\"bright\"])", 'tags.append(r["bright"])' not in _ui_src)
check("DynTagScanThread 解包 4 元组",
      "motion, flicker, diff, med = Tagger.analyze_dynamic" in _ui_src)
check("merge_scan_tags 剔除旧亮度词（含明亮/昏暗/中等）",
      '"明亮", "昏暗", "中等"' in _ui_src)
check("dyn_tag_ver 触发阈值已升到 6", 'self.cfg["dyn_tag_ver"] or 0) < 6' in _ui_src)
check("dyn_tag_ver 写回值已升到 6", 'self.main.cfg["dyn_tag_ver"] = 6' in _ui_src)

# ========================================================================
# ⑦ 图片非 ASCII 路径修复（本轮必修 1）
# ========================================================================
print()
print("⑦ 图片非 ASCII 路径修复")
check("tagger 定义 _cv_imread 封装", "def _cv_imread(" in _tg_src)
check("analyze_dynamic 图片分支走 _cv_imread",
      "img = _cv_imread(path, cv2.IMREAD_UNCHANGED)" in _tg_src)
# 视频分支**不得**改：仍应直接用原始 path 开 VideoCapture（不套临时副本）
check("视频分支仍用原始 path（cv2.VideoCapture(path)）",
      "cap = cv2.VideoCapture(path)" in _tg_src)
check("视频分支未引入 _cv_imread", "_cv_imread" not in _tg_src.split("cap = cv2.VideoCapture(path)")[1].split("\n\n")[0])

# 造一张**中文目录 + 中文文件名**的图，验证 med 正常（不再是 None）
# ⚠ 夹具必须用 imencode + 字节写：cv2.imwrite 自身也读不了非 ASCII 路径（同样限制），
#   用它造图会"文件压根没写出来"，从而误判我们的修复无效。
_zh_dir = os.path.join(tmp, "素材库", "夜景")
os.makedirs(_zh_dir, exist_ok=True)
_zh_img = os.path.join(_zh_dir, "亮图.png")
_ok_enc, _buf = cv2.imencode(".png", white)
with open(_zh_img, "wb") as _f:
    _f.write(_buf.tobytes())
check("★ 前置：中文路径夹具确已落盘", os.path.exists(_zh_img), _zh_img)
# 前置自检：原始路径的确含非 ASCII（否则本测试无效）
try:
    _zh_img.encode("ascii")
    check("★ 前置：构造路径确含非 ASCII", False, _zh_img)
except UnicodeEncodeError:
    check("★ 前置：构造路径确含非 ASCII", True, "...")
_, _, _, med_zh = Tagger.analyze_dynamic(_zh_img, "image")
check("★ 中文路径白图 med≈255（修复前=NULL）", med_zh is not None and abs(med_zh - 255.0) < 1.0,
      "med=%r" % med_zh)
check("★ 中文路径白图 → 亮基调", med_zh is not None and grade(med_zh) == td.BRIGHT_LIGHT,
      "档=%s" % (grade(med_zh) if med_zh is not None else None))
# analyze_dynamic 图片分支返回 med 非 None 即可推出 analyze_media 不会再 return None
# （analyze_media 图片分支同一 _cv_imread 封装，源码断言见上）。

# ========================================================================
# ⑧ 亮度三档互斥按钮（本轮缺口 2）—— 内省，不实际创建窗口
# ========================================================================
print()
print("⑧ TagEditDialog 亮度三档互斥按钮（内省）")
import ast                                                          # noqa: E402
_panels_src = io.open(os.path.join(_src_root, "panels.py"), encoding="utf-8").read()
_panels_tree = ast.parse(_panels_src)
_ted = None
for node in ast.walk(_panels_tree):
    if isinstance(node, ast.ClassDef) and node.name == "TagEditDialog":
        _ted = node
check("TagEditDialog 类存在", _ted is not None)
if _ted is not None:
    _methods = {n.name for n in _ted.body if isinstance(n, ast.FunctionDef)}
    check("含 _on_bright 互斥处理", "_on_bright" in _methods, sorted(_methods))
    check("__init__ 建 self._bright_btns", "self._bright_btns" in _panels_src)
    # 三档常量都进了亮度按钮行
    check("亮度行含 BRIGHT_DARK/MID/LIGHT 三档",
          "(BRIGHT_DARK, BRIGHT_MID, BRIGHT_LIGHT)" in _panels_src)
    check("panels 顶部已 import BRIGHT_*",
          "BRIGHT_DARK, BRIGHT_MID, BRIGHT_LIGHT" in _panels_src.split("class TagEditDialog")[0])
    # _on_bright 必须是**互斥**（遍历其它两档 setChecked(False)）
    _on_bright_src = ""
    for n in ast.walk(_ted):
        if isinstance(n, ast.FunctionDef) and n.name == "_on_bright":
            _on_bright_src = ast.get_source_segment(_panels_src, n) or ""
    check("_on_bright 互斥（setChecked(False) 其它档）",
          "setChecked(False)" in _on_bright_src and "BRIGHT_DARK" in _on_bright_src)
    # 亮度词不进「额外」行（避免重复显示）
    check("_sync_extra 排除亮度词",
          "BRIGHT_DARK, BRIGHT_MID, BRIGHT_LIGHT" in _panels_src.split("def _sync_extra")[1].split("def ")[0])

# ========================================================================
# ⑨ COVERAGE_* i18n 显示名注入（本轮缺口 3）
# ========================================================================
print()
print("⑨ COVERAGE_* i18n 显示名注入")
import i18n_map                                                     # noqa: E402
_cov_full = i18n_map.ZH2EN.get(td.COVERAGE_FULL)
_cov_part = i18n_map.ZH2EN.get(td.COVERAGE_PART)
check("铺满 有显示名（非 None / 非中文原值）",
      _cov_full is not None and _cov_full != td.COVERAGE_FULL, repr(_cov_full))
check("局部 有显示名（非 None / 非中文原值）",
      _cov_part is not None and _cov_part != td.COVERAGE_PART, repr(_cov_part))
check("铺满 → Full Frame", _cov_full == "Full Frame", repr(_cov_full))
check("局部 → Partial", _cov_part == "Partial", repr(_cov_part))
# 数据层不被动：中文原值仍是标签本身（显示名只是 Z2EN 映射）
check("数据层中文原值未被改（COVERAGE_FULL == 铺满）", td.COVERAGE_FULL == "铺满")

# ========================================================================
# ⑩ merge_scan_tags 纯函数逐条等价（本轮可维护性 4）
# ========================================================================
print()
print("⑩ merge_scan_tags 纯函数等价 + 重证「不碰 manual/disabled」")
import ui_main as um                                                # noqa: E402
check("merge_scan_tags 为模块级函数", callable(getattr(um, "merge_scan_tags", None)))

MST = getattr(um, "merge_scan_tags", None)

if callable(MST):
    # 题面断言：tags=['人物','低动态','昏暗','明亮','铺满'] manual=['我手动加的','暗基调']
    #           disabled=['暗基调','铺满']  fed med=5.0 / cov=0.9
    # 期望：['人物','低动态']（铺满被 disabled 不复活；旧亮度词全剔；med=5→暗基调但暗基调被 disabled 不复活）
    _tags = ["人物", "低动态", "昏暗", "明亮", "铺满"]
    _manual = ["我手动加的", "暗基调"]
    _disabled = ["暗基调", "铺满"]
    out = MST(_tags, _manual, _disabled,
              motion=td.DYNAMIC_LOW, flicker=False, coverage=0.9, brightness=5.0)
    check("★ 题面断言合并结果 == ['人物','低动态']", out == ["人物", "低动态"], out)
    check("★ 返回值不含 manual 词", "我手动加的" not in out and "暗基调" not in out, out)
    check("★ 返回值不含 disabled 词（铺满不复活）", "铺满" not in out, out)
    # 入参未被就地修改（纯函数）
    check("★ 入参 existing_tags 未被就地改", _tags == ["人物", "低动态", "昏暗", "明亮", "铺满"], _tags)
    check("★ 入参 manual_tags 未被改", _manual == ["我手动加的", "暗基调"], _manual)
    check("★ 入参 disabled_tags 未被改", _disabled == ["暗基调", "铺满"], _disabled)

    # 边界 1：motion=""（打不开）→ 完全不变
    _b1 = ["人物", "低动态", "铺满", "暗基调"]
    out1 = MST(_b1, [], [], motion="", flicker=False, coverage=0.9, brightness=5.0)
    check("★ 边界 motion='' → 标签完全不变", out1 == _b1, out1)
    check("★ 边界 motion='' 不就地改", _b1 == ["人物", "低动态", "铺满", "暗基调"], _b1)

    # 边界 2：brightness=None → 保留旧亮度词不动
    _b2 = ["人物", "中动态", "暗基调"]
    out2 = MST(_b2, [], [], motion=td.DYNAMIC_HIGH, flicker=False,
               coverage=None, brightness=None)
    check("★ 边界 brightness=None → 旧亮度词保留", "暗基调" in out2, out2)
    check("★ brightness=None → 动态词已换新", td.DYNAMIC_HIGH in out2 and "中动态" not in out2, out2)

    # 边界 3：coverage=None → 保留旧铺满度词不动
    _b3 = ["人物", "低动态", "铺满"]
    out3 = MST(_b3, [], [], motion=td.DYNAMIC_LOW, flicker=False,
               coverage=None, brightness=100.0)
    check("★ 边界 coverage=None → 旧铺满度词保留", "铺满" in out3, out3)

    # 边界 4：disabled 命中动态词 → 不复活
    out4 = MST(["人事", "低动态"], [], [td.DYNAMIC_HIGH],
               motion=td.DYNAMIC_HIGH, flicker=False, coverage=None, brightness=None)
    check("★ 边界 disabled 命中动态词 → 不复活", td.DYNAMIC_HIGH not in out4, out4)

    # 边界 5：亮度三档落词边界（15 / 40）
    check("med=14.9 → 暗基调",
          td.BRIGHT_DARK in MST([], [], [], motion=td.DYNAMIC_LOW, flicker=False,
                                coverage=None, brightness=14.9))
    check("med=15.0 → 中亮度",
          td.BRIGHT_MID in MST([], [], [], motion=td.DYNAMIC_LOW, flicker=False,
                               coverage=None, brightness=15.0))
    check("med=39.9 → 中亮度",
          td.BRIGHT_MID in MST([], [], [], motion=td.DYNAMIC_LOW, flicker=False,
                               coverage=None, brightness=39.9))
    check("med=40.0 → 亮基调",
          td.BRIGHT_LIGHT in MST([], [], [], motion=td.DYNAMIC_LOW, flicker=False,
                                 coverage=None, brightness=40.0))

    # 铺满度阈值落词
    check("cov=0.45 → 铺满",
          td.COVERAGE_FULL in MST([], [], [], motion=td.DYNAMIC_LOW, flicker=False,
                                  coverage=0.45, brightness=None))
    check("cov=0.44 → 局部",
          td.COVERAGE_PART in MST([], [], [], motion=td.DYNAMIC_LOW, flicker=False,
                                  coverage=0.44, brightness=None))

# 源码断言：DynTagScanThread.run 确实调用 merge_scan_tags（不再内联重复逻辑）
check("DynTagScanThread.run 调用 merge_scan_tags",
      "merge_scan_tags(m.tags, m.manual_tags, m.disabled_tags," in _ui_src)
# 内联追加动态词已从 DynTagScanThread.run 抽出 → 该 run 方法体内不应再出现 tags.append(motion)
# （merge_scan_tags 纯函数内允许出现，故只检查 run 方法体）。
_ui_tree = ast.parse(_ui_src)
_run_src = ""
for _node in ast.walk(_ui_tree):
    if isinstance(_node, ast.ClassDef) and _node.name == "DynTagScanThread":
        for _m in _node.body:
            if isinstance(_m, ast.FunctionDef) and _m.name == "run":
                _run_src = ast.get_source_segment(_ui_src, _m) or ""
check("DynTagScanThread.run 不再内联追加动态词（tags.append(motion)）",
      "tags.append(motion)" not in _run_src)

print()
print("=" * 60)
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
