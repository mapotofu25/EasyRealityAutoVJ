# -*- coding: utf-8 -*-
"""统一标签词表：分类 + 双语。打标(视觉CLIP)、手动编辑、曲风映射 共用这一份。"""

# (分类名, [(中文, 英文), ...])
TAG_CATEGORIES = [
    ("颜色", [
        ("蓝色", "blue"), ("红色", "red"), ("绿色", "green"), ("青色", "cyan"),
        ("黄色", "yellow"),
        ("紫色", "purple"), ("粉色", "pink"), ("白色", "white"), ("黑色", "black"),
        ("灰色", "gray"), ("橙色", "orange"), ("暖色", "warm"), ("冷色", "cool"),
        ("彩色", "colorful"), ("金色", "gold"), ("银色", "silver"),
    ]),
    ("元素特效", [
        ("粒子", "particles"), ("光斑", "bokeh"), ("光效", "light effect"),
        ("光线", "light beam"), ("激光", "laser"), ("霓虹", "neon"),
        ("烟雾", "smoke"), ("火焰", "flames"), ("闪电", "lightning"),
        ("星轨", "star trail"), ("几何", "geometry"), ("线条", "lines"),
        ("网格", "grid"), ("波纹", "ripple"), ("气泡", "bubbles"),
        ("碎片", "debris"), ("辉光", "glow"), ("液态", "liquid"),
        ("水墨", "ink"), ("渐变", "gradient"), ("马赛克", "mosaic"),
    ]),
    ("场景", [
        ("海洋", "ocean"), ("沙滩", "beach"), ("星空", "starry sky"),
        ("夜空", "night sky"), ("天空", "sky"), ("云", "cloud"),
        ("日出", "sunrise"), ("日落", "sunset"), ("森林", "forest"),
        ("山", "mountain"), ("沙漠", "desert"), ("雪", "snow"), ("雨", "rain"),
        ("火", "fire"), ("水", "water"), ("花", "flower"), ("草地", "grass"),
        ("宇宙", "universe"), ("极光", "aurora"), ("月亮", "moon"), ("太阳", "sun"),
        ("城市", "city"), ("夜景", "night scene"), ("舞台", "stage"),
        ("科技", "tech"), ("机械", "machine"), ("未来", "future"),
        ("赛博朋克", "cyberpunk"), ("暗黑", "dark"), ("废墟", "ruins"),
        ("室内", "indoor"), ("工业", "industrial"), ("阳光", "sunshine"),
    ]),
    ("人物生物", [
        ("人物", "person"), ("女性", "woman"), ("男性", "man"), ("舞者", "dancer"),
        ("剪影", "silhouette"), ("动物", "animal"), ("鸟", "bird"), ("眼睛", "eye"),
    ]),
    ("风格", [
        ("抽象", "abstract"), ("纹理", "texture"), ("梦幻", "dreamy"),
        ("恐怖", "horror"), ("可爱", "cute"), ("复古", "retro"),
        ("极简", "minimal"), ("神圣", "sacred"), ("史诗感", "epic"),
        ("粗粝", "gritty"),
    ]),
]

ALL_TAGS = [(zh, en, cat) for cat, items in TAG_CATEGORIES for zh, en in items]
TAG_ZH_LIST = [zh for zh, _, _ in ALL_TAGS]

# 动态标签：由帧差/频闪检测确定性产出（tagger.analyze_media），**不进 TAG_CATEGORIES**。
# 原因：CLIP 是单帧视觉，看不出"动态"；让 CLIP 打动态词会引入噪声。
# 三档动态 + 频闪单列（频闪是"闪"的元凶，缓和段必须精确避开）。
DYNAMIC_LOW = "低动态"
DYNAMIC_MID = "中动态"
DYNAMIC_HIGH = "高动态"
DYNAMIC_FLICKER = "频闪"
DYNAMIC_TAGS = (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH, DYNAMIC_FLICKER)

# 铺满度标签：由 `tagger.coverage_score()`（确定性计算，边缘带内容占比 + 分块广度）
# 产出，**不进 TAG_CATEGORIES** —— 与上面 DYNAMIC_TAGS 同理：铺满度不是 CLIP 单帧视觉
# 能看出来的东西，让 CLIP 去打「铺满/局部」只会引入噪声。仅由打标消费点按阈值落地。
COVERAGE_FULL = "铺满"
COVERAGE_PART = "局部"
COVERAGE_TAGS = (COVERAGE_FULL, COVERAGE_PART)
# 铺满度阈值（**唯一定义处**，杜绝"两处阈值漂移"）：
#   · `COVERAGE_FULL_THRESHOLD`        —— 常规分界；coverage ≥ 该值 → 铺满/背景。
#   · `COVERAGE_FULL_THRESHOLD_DYNAMIC` —— 高动态时的宽松分界（动态画面即便略空也偏向铺满/背景）。
# `auto_roles`（tagger.py）与打标消费点（ui_main.py TagScanThread）**必须**共用这两个常量。
COVERAGE_FULL_THRESHOLD = 0.45
COVERAGE_FULL_THRESHOLD_DYNAMIC = 0.35

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

# 亮度分档：由「全片灰度均值序列的中位数 med」确定性产出（tagger.analyze_dynamic）。
# 与 DYNAMIC_*/COVERAGE_* 同策略：**不进 TAG_CATEGORIES**（非 CLIP 单帧视觉产出）。
#
# ⚠ 命名刻意避开 color_brightness() 的「明亮/昏暗/中等」——那三个词是**单帧 + HSV-V>170**
#    口径，实测有双重缺陷（见下），已废弃。新三档用「暗基调/中亮度/亮基调」明确区分。
#
# 实测分布依据（2026-10-05，真实素材库 162 个素材全片顺序解码实测）：
#   med: min=0  P10=6.3  P25=14.1  中位=26.4  P75=41.4  P90=74.1  max=241
#   阈值落在自然间隙上 → 实测落档 暗 43(26.5%) / 中 74(45.7%) / 亮 45(27.8%)
#   ⚠ 旧 color_brightness 实测只有 3/162 判「明亮」，而全片中位数实测 88/162(54.3%) 可达"亮"
#     —— 旧标签的"明亮=0%"是单帧+阈值双重缺陷造出的人为假象。实测两者判定不一致 85/162。
BRIGHT_DARK = "暗基调"
BRIGHT_MID = "中亮度"
BRIGHT_LIGHT = "亮基调"
BRIGHTNESS_TAGS = (BRIGHT_DARK, BRIGHT_MID, BRIGHT_LIGHT)

BRIGHTNESS_DARK_THRESHOLD = 15.0    # med < 15        → 暗基调
BRIGHTNESS_BRIGHT_THRESHOLD = 40.0  # med >= 40       → 亮基调；15~40 → 中亮度
