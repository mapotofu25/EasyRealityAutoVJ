# -*- coding: utf-8 -*-
"""素材标签词表 —— **只保留"确定性算法算得出来"的那两类**。

★★ 2026-10-30 用户「大砍刀」：
  「砍掉曲风映射，砍掉曲风扫描，砍掉素材标签的扫描，砍掉素材标签扫描的模型，
    砍掉曲风识别的模型…… 砍掉素材明亮地扫描，保留素材动态的扫描」

砍掉的（连同代码、模型、UI 一起）：
  · CLIP 内容词表（颜色 / 元素特效 / 场景 / 人物生物 / 风格）
    + Chinese-CLIP 视觉编码器（assets/models/chinese_clip，331 MB）
  · 亮度三档（暗基调 / 中亮度 / 亮基调，靠全片灰度中位数 med 分档）
  · 自定义词条（cfg["custom_tags"]）
  · 曲风（audio_genre ONNX + discogs 曲风表 + 曲风查询 / 映射 / 纠正）

留下的只有两组，都是"拿帧直接算、零模型"的硬指标：
  · 动态三档 + 频闪   —— `tagger.Tagger.analyze_dynamic()` 相邻帧差 / 亮度跳变
  · 铺满 / 局部       —— `tagger.Tagger.coverage_score()` 边缘带 + 分块广度
（用户 2026-10-30 明确：铺满度**保留**，因为它是自动分配"前景/背景"的唯一依据。）

⚠ 阈值只在本文件定义一次，`tagger` / `ui_main` 全链路共用，杜绝两处漂移。
"""

# ---------------------------------------------------------------- 动态 / 频闪
# 由帧差/亮度跳变确定性产出（`tagger.analyze_dynamic`），**不是**视觉模型打的。
DYNAMIC_LOW = "低动态"
DYNAMIC_MID = "中动态"
DYNAMIC_HIGH = "高动态"
DYNAMIC_FLICKER = "频闪"
DYNAMIC_TAGS = (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER)

# 帧差(diff)分档阈值：全程相邻帧差的「中位数」，0~255。
# v2 改用「顺序读全片」算真实相邻帧差（旧 seek 分段采样对 DXV/DXD3 失效，三段会采到
# 同一批帧，把「一直在闪/呼吸闪」的素材误判成低动态，见新视觉82期(35)）。
# 顺序读实测分布（2026-09-22）：低动态 med 6.2~8.1、中动态 med 10.5~17.1、高动态 med 20.8+。
# 10.0 卡在低动态(≤8.1)与中动态(≥10.5)之间；19.0 卡在中动态(≤17.1)与高动态(≥20.8)之间。
DIFF_MID_THRESHOLD = 10.0   # diff < 10 → 低动态
DIFF_HIGH_THRESHOLD = 19.0  # 10 ≤ diff < 19 → 中动态；diff ≥ 19 → 高动态
# 高动态辅助：中位数偏低(落在中动态区间)但近半帧差>19 的素材仍判高动态。
# 例：新视觉82期(36) med=17.1 但 49% 帧差>19，应保持高动态；纯中动态素材(>19 占比<0.2)不受影响。
DIFF_HIGH_RATIO = 0.40

# ---------------------------------------------------------------- 铺满 / 局部
# 由 `tagger.coverage_score()`（确定性计算，边缘带内容占比 + 分块广度）产出。
COVERAGE_FULL = "铺满"
COVERAGE_PART = "局部"
COVERAGE_TAGS = (COVERAGE_FULL, COVERAGE_PART)
#   · `COVERAGE_FULL_THRESHOLD`          —— 常规分界；coverage ≥ 该值 → 铺满/背景。
#   · `COVERAGE_FULL_THRESHOLD_DYNAMIC`  —— 高动态时的宽松分界（动态画面即便略空也偏向铺满/背景）。
COVERAGE_FULL_THRESHOLD = 0.75
COVERAGE_FULL_THRESHOLD_DYNAMIC = 0.65
