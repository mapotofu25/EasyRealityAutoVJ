# -*- coding: utf-8 -*-
"""曲风 → 画面标签 的**完整默认映射**（覆盖 `genre_keywords.GENRE_GROUPS` 里的全部曲风）。

## 为什么要有这个文件
用户反馈：「曲风映射里**有很多是空的**」。
原因：原来只有 `match_engine.GENRE_TO_VISUAL`，覆盖 **33 个**宽泛曲风；
而编辑器（`GenreVisualEditDialog`）按 `genre_keywords.GENRE_GROUPS` 列出 **138 个**曲风，
两者差集里的曲风打开就是一片空白 —— 用户不知道该勾什么，等于这个功能对他是残缺的。

## 做法
**按大类给底子 + 按曲风做区别**：
  · `FAMILY_BASE`：15 个大类各一套基础标签（同大类的曲风天然共享气质）；
  · `GENRE_EXTRA`：个别曲风的差异化标签；
  · `build_default()`：`大类底子 ∪ 曲风区别`，覆盖 **每一个** 曲风，一个都不留空。

## 硬约束
**只用 `tags_def.py` 里真实存在的画面标签**（否则匹配阶段永远命不中，是死条目）。
`validate()` 会检查这件事，配套测试 `tools/_test_genre_default.py` 每次都跑。

⚠ 注意：这里**不写**「低动态 / 中动态 / 高动态 / 频闪」——
那四个是**动态特征**，由能量驱动（见 `tags_def` 的 DYNAMIC_*），不属于内容映射。
"""

# ============================================================================
# 一、大类底子（每个大类一套）
# ============================================================================
FAMILY_BASE = {
    "硬核系":       ["暗黑", "工业", "粒子", "激光", "光线", "碎片"],
    "Hardstyle系":  ["暗黑", "粒子", "光线", "辉光", "霓虹"],
    "浩室系":       ["城市", "夜景", "霓虹", "舞台", "舞者", "彩色"],
    "Trance系":     ["宇宙", "星空", "极光", "星轨", "梦幻"],
    "Techno系":     ["科技", "机械", "未来", "霓虹", "网格"],
    "贝斯/回响系":  ["暗黑", "机械", "霓虹", "几何", "碎片"],
    "Drum & Bass系": ["城市", "线条", "几何", "网格", "霓虹"],
    "陷阱系":       ["暗黑", "霓虹", "城市", "烟雾", "碎片"],
    "车库/碎拍系":  ["城市", "霓虹", "线条", "复古", "几何"],
    "氛围/慢节奏":  ["星空", "云", "宇宙", "极光", "渐变", "梦幻"],
    "电子其他":     ["霓虹", "未来", "科技", "复古", "彩色"],
    "嘻哈/R&B/灵魂": ["城市", "夜景", "人物", "复古", "金色", "剪影"],
    "流行/摇滚":    ["彩色", "辉光", "人物", "舞台"],
    "金属系":       ["暗黑", "火焰", "废墟", "工业", "恐怖", "粗粝"],
    "爵士/古典/世界": ["室内", "金色", "复古", "纹理"],
    # 兜底（万一 genre_keywords 里出现未归类的新曲风，走这个，绝不空着）
    "其他":         ["抽象", "几何", "纹理"],
}

FALLBACK_TAGS = ["抽象", "几何", "纹理"]

# ============================================================================
# 二、单曲风的差异化标签（只写「跟大类底子不一样」的部分）
# ============================================================================
GENRE_EXTRA = {
    # ---- 硬核系 ----
    "Hard Dance":           ["舞者", "闪电"],
    "Puzzycore":            ["可爱", "粉色", "碎片"],
    "Uptempo Hardcore":     ["闪电", "碎片", "白色"],
    "UK Hardcore":          ["辉光", "彩色", "舞者"],
    "Happy Hardcore":       ["彩色", "可爱", "辉光", "粉色"],
    "Industrial Hardcore":  ["工业", "机械", "废墟"],
    "Frenchcore":           ["闪电", "碎片", "红色"],
    "Gabber":               ["工业", "黑色", "粗粝"],

    # ---- Hardstyle系 ----
    "Hardstyle":            ["史诗感", "激光"],
    "Rawstyle":             ["工业", "碎片", "黑色"],
    "Euphoric Hardstyle":   ["辉光", "史诗感", "梦幻", "紫色"],

    # ---- 浩室系 ----
    "Deep House":           ["夜景", "梦幻", "极简", "紫色"],
    "Tech House":           ["科技", "网格", "舞台"],
    "Progressive House":    ["史诗感", "渐变", "日出"],
    "Acid House":           ["液态", "绿色", "光斑"],
    "Tropical House":       ["沙滩", "海洋", "花", "暖色"],
    "French House":         ["复古", "金色", "舞者"],
    "Disco House":          ["光斑", "金色", "舞者", "复古"],
    "Hard House":           ["工业", "激光", "红色"],
    "Bass House":           ["机械", "霓虹", "网格"],
    "Future House":         ["未来", "辉光", "科技"],
    "Electro House":        ["科技", "霓虹", "几何"],
    "Melodic House":        ["梦幻", "渐变", "星空"],
    "Afro House":           ["暖色", "人物", "沙漠"],
    "Big Room House":       ["舞台", "激光", "史诗感"],
    "Melbourne Bounce":     ["舞台", "彩色", "舞者"],
    "Amapiano":             ["暖色", "夜景", "人物"],
    "Dutch House":          ["舞台", "激光", "橙色"],

    # ---- Trance系 ----
    "Hard Trance":          ["激光", "星轨", "机械"],
    "Psytrance":            ["几何", "液态", "紫色", "眼睛"],
    "Uplifting Trance":     ["日出", "梦幻", "神圣", "辉光"],
    "Progressive Trance":   ["渐变", "星空", "梦幻"],
    "Tech Trance":          ["科技", "网格", "星轨"],

    # ---- Techno系 ----
    "Hard Techno":          ["工业", "激光", "粗粝"],
    "Acid Techno":          ["液态", "绿色", "光斑"],
    "Melodic Techno":       ["梦幻", "渐变", "夜景"],
    "Industrial Techno":    ["工业", "废墟", "机械", "烟雾"],
    "Minimal Techno":       ["极简", "几何", "白色"],

    # ---- 贝斯/回响系 ----
    "Brostep":              ["机械", "碎片", "激光"],
    "Riddim":               ["网格", "液态", "绿色"],
    "Future Riddim":        ["液态", "紫色", "网格"],
    "Deathstep":            ["恐怖", "暗黑", "火焰"],
    "Melodic Dubstep":      ["梦幻", "紫色", "辉光"],
    "Colour Bass":          ["彩色", "辉光", "液态"],
    "Bass Music":           ["机械", "暗黑", "几何"],
    "Kawaii Bass":          ["可爱", "粉色", "辉光"],
    "Future Bass":          ["辉光", "彩色", "粒子"],
    "Midtempo Bass":        ["机械", "霓虹", "网格"],
    "Moombahcore":          ["舞台", "霓虹", "几何"],

    # ---- Drum & Bass系 ----
    "Liquid DnB":           ["水", "蓝色", "波纹", "梦幻"],
    "Dancefloor DnB":       ["霓虹", "舞者", "光斑"],
    "Jump Up DnB":          ["几何", "彩色", "网格"],
    "Neurofunk":            ["机械", "液态", "科技", "绿色"],
    "Jungle":               ["森林", "绿色", "纹理"],
    "Drumstep":             ["机械", "碎片", "霓虹"],
    "Breakcore":            ["马赛克", "碎片", "彩色", "抽象"],

    # ---- 陷阱系 ----
    "Hard Trap":            ["暗黑", "激光", "火焰"],
    "Hybrid Trap":          ["机械", "烟雾", "紫色"],
    "Glitch Hop":           ["马赛克", "几何", "碎片"],
    "Moombahton":           ["沙滩", "暖色", "舞者"],

    # ---- 车库/碎拍系 ----
    "2-Step Garage":        ["夜景", "蓝色", "线条"],
    "Speed Garage":         ["线条", "霓虹", "网格"],
    "Future Garage":        ["梦幻", "蓝色", "雨"],
    "Bassline":             ["机械", "霓虹", "网格"],
    "Breakbeat":            ["几何", "线条", "碎片"],
    "Big Beat":             ["舞台", "光斑", "复古"],
    "Nu-Disco":             ["复古", "金色", "光斑"],
    "Electro Swing":        ["复古", "金色", "舞者", "室内"],

    # ---- 氛围/慢节奏 ----
    "Chillout":             ["云", "日落", "海洋", "渐变"],
    "Downtempo":            ["天空", "云", "日落", "渐变"],
    "IDM":                  ["抽象", "几何", "网格"],
    "Glitch":               ["马赛克", "碎片", "抽象"],
    "Experimental":         ["抽象", "几何", "马赛克"],

    # ---- 电子其他 ----
    "Electro":              ["科技", "霓虹", "几何"],
    "Synthwave":            ["赛博朋克", "霓虹", "城市", "日落"],
    "Disco":                ["光斑", "金色", "舞者", "复古"],
    "Complextro":           ["碎片", "几何", "彩色"],

    # ---- 嘻哈/R&B/灵魂 ----
    "Lo-Fi Hip Hop":        ["室内", "复古", "梦幻", "极简"],
    "Instrumental Hip Hop": ["城市", "夜景", "纹理"],
    "Experimental Hip Hop": ["抽象", "碎片", "城市"],
    "Phonk":                ["暗黑", "复古", "剪影", "紫色"],
    "Drift Phonk":          ["夜景", "雨", "剪影", "暗黑"],
    "Alternative R&B":      ["夜景", "紫色", "梦幻"],
    "Contemporary R&B":     ["人物", "夜景", "金色"],
    "New Jack Swing":       ["复古", "彩色", "舞者"],
    "Neo Soul":             ["金色", "暖色", "人物", "室内"],
    "Gospel":               ["神圣", "金色", "人物"],
    "Funk":                 ["彩色", "复古", "舞者", "金色"],

    # ---- 流行/摇滚 ----
    "J-Pop":                ["彩色", "可爱", "城市"],
    "K-Pop":                ["彩色", "舞者", "霓虹"],
    "City Pop":             ["城市", "夜景", "复古", "霓虹"],
    "Dance Pop":            ["舞者", "彩色", "光斑"],
    "Indie Pop":            ["复古", "梦幻", "花", "暖色"],
    "Vocaloid":             ["可爱", "彩色", "辉光", "蓝色"],
    "Anime":                ["可爱", "彩色", "人物", "花"],
    "Rock":                 ["暗黑", "纹理", "线条", "火焰"],
    "Alternative Rock":     ["纹理", "暗黑", "抽象"],
    "Pop Rock":             ["彩色", "舞台", "人物"],
    "Punk":                 ["粗粝", "纹理", "暗黑", "马赛克"],

    # ---- 金属系 ----
    "Metalcore":            ["火焰", "碎片", "粗粝"],
    "Deathcore":            ["恐怖", "火焰", "暗黑", "碎片"],
    "Death Metal":          ["恐怖", "暗黑", "火焰", "纹理"],
    "Black Metal":          ["暗黑", "雪", "森林", "恐怖"],
    "Nu Metal":             ["工业", "机械", "碎片", "城市"],
    "Industrial Metal":     ["工业", "机械", "废墟", "网格"],
    "Progressive Metal":    ["史诗感", "几何", "抽象"],

    # ---- 爵士/古典/世界 ----
    "Jazz Fusion":          ["金色", "抽象", "几何", "室内"],
    "Bossa Nova":           ["沙滩", "暖色", "海洋", "花"],
    "Bebop":                ["室内", "复古", "金色"],
    "Swing Jazz":           ["复古", "舞者", "金色", "室内"],
    "Classical":            ["纹理", "水墨", "云", "室内"],
    "Baroque":              ["神圣", "金色", "纹理", "室内"],
    "Romantic Classical":   ["梦幻", "花", "暖色", "纹理"],
    "Opera":                ["神圣", "史诗感", "舞台", "金色"],
    "Modern Classical":     ["极简", "抽象", "纹理", "白色"],
    "Soundtrack":           ["云", "天空", "森林", "史诗感"],
    "Latin":                ["暖色", "人物", "舞者", "沙滩"],
    "Reggae":               ["沙滩", "海洋", "暖色", "绿色"],
    "Country":              ["草地", "阳光", "暖色", "山"],
    "Folk":                 ["森林", "草地", "云", "暖色"],
    "Blues":                ["暗黑", "暖色", "室内", "复古"],
    "Singer-Songwriter":    ["室内", "暖色", "人物", "极简"],
}


# ============================================================================
# 三、构建
# ============================================================================
def build_default():
    """返回 {曲风名: [画面标签, ...]}，覆盖 `GENRE_GROUPS` 里的每一个曲风。

    同一大类里的曲风共享 `FAMILY_BASE`，再叠加 `GENRE_EXTRA` 的区别 ——
    所以**任何**曲风打开编辑器都不是空白。
    """
    out = {}
    try:
        from genre_keywords import GENRE_GROUPS
    except Exception:                                          # noqa: BLE001
        GENRE_GROUPS = []
    if not GENRE_GROUPS:
        # 退化路径：直接拿 GENRE_EXTRA + 一份宽泛底子，至少不为空
        for g, extra in GENRE_EXTRA.items():
            out[g] = sorted(set(FALLBACK_TAGS) | set(extra))
        return out

    for family, names in GENRE_GROUPS:
        base = FAMILY_BASE.get(family) or FALLBACK_TAGS
        for g in (names or []):
            if not g:
                continue
            tags = set(base) | set(GENRE_EXTRA.get(g) or ())
            out[g] = sorted(tags)
    return out


def validate():
    """检查：① 用的标签都在 `tags_def` 里真实存在 ② 没有任何曲风是空的。

    返回 (ok, 问题列表)。配套测试 `tools/_test_genre_default.py` 调用它。
    """
    problems = []
    try:
        from tags_def import ALL_TAGS
        valid = {t[0] if isinstance(t, (tuple, list)) else t for t in ALL_TAGS}
    except Exception as e:                                     # noqa: BLE001
        return False, ["无法导入 tags_def：%s" % e]

    checked = set()
    for src in (FAMILY_BASE, GENRE_EXTRA):
        for k, tags in src.items():
            for t in tags:
                checked.add(t)
                if t not in valid:
                    problems.append("未知画面标签 %r（出现在 %r）" % (t, k))
    for t in FALLBACK_TAGS:
        if t not in valid:
            problems.append("未知画面标签 %r（FALLBACK_TAGS）" % t)

    m = build_default()
    if not m:
        problems.append("build_default() 返回空表")
    empty = [g for g, t in m.items() if not t]
    if empty:
        problems.append("以下曲风的标签为空：%s" % empty)

    try:
        from genre_keywords import GENRE_GROUPS
        allg = {g for _f, names in GENRE_GROUPS for g in (names or [])}
        missing = sorted(allg - set(m))
        if missing:
            problems.append("以下曲风没有被覆盖：%s" % missing)
    except Exception:                                          # noqa: BLE001
        pass

    return (not problems), problems


if __name__ == "__main__":
    ok, probs = validate()
    d = build_default()
    print("曲风数：%d" % len(d))
    for g in list(d)[:10]:
        print("  %-22s %s" % (g, " ".join(d[g])))
    print("...")
    print("校验：%s" % ("通过 ✓" if ok else "有问题 ✗"))
    for p in probs:
        print("  !! " + p)
