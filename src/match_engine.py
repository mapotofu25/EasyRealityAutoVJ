# -*- coding: utf-8 -*-
"""匹配引擎：曲风 tag × 图层角色 × 素材 tag → 挑素材（能量/颜色兜底）。

曲风标签（英文原词）来自 music_meta / 实时识别，映射到「画面素材标签」（中文，对齐 tags_def.py 词表），
再按图层角色（前景/背景）从素材库挑匹配素材。能量影响选材：高能量偏「高动态」素材，低能量避开。
"""

import random

from tags_def import DYNAMIC_HIGH, DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_FLICKER

# 曲风 → 画面标签（只取 tags_def.py 里真实存在的词，否则匹配不到）
# 注意：素材打标已做相似度校准（压泛化词），所以这里避免用「光效/高动态」这类泛化词，
# 改用更具体的词（光斑/激光/星轨/辉光）；「高动态」是动态特征，由能量驱动，不进内容映射。
# key 必须与 genre_keywords.GENRE_SIMPLIFY 的归并目标一致，否则是永远命不中的死条目
# （曾经 DnB/Chillout/IDM/Breakcore/Garage 五个死条目，2026-09-19 清理合并）。
GENRE_TO_VISUAL = {
    "Electronic": {"城市", "霓虹", "光斑"},
    "Hardcore": {"暗黑", "工业", "粒子", "激光"},
    "Hardstyle": {"暗黑", "粒子", "光线"},
    "Trance": {"宇宙", "星空", "极光", "星轨"},
    "House": {"城市", "夜景", "霓虹"},
    "Techno": {"科技", "机械", "未来", "霓虹"},
    "Drum & Bass": {"城市", "线条", "几何"},
    "Dubstep": {"暗黑", "机械", "霓虹", "几何"},
    "Ambient": {"星空", "云", "宇宙", "极光"},
    "Downtempo": {"天空", "云", "日落", "渐变"},
    "Synthwave": {"赛博朋克", "霓虹", "城市", "日落"},
    "Future Bass": {"辉光", "彩色", "粒子"},
    "Trap": {"暗黑", "霓虹", "城市"},
    "Rock": {"暗黑", "纹理", "线条"},
    "Metal": {"暗黑", "火焰", "废墟", "工业"},
    "Punk": {"纹理", "暗黑"},
    "Pop": {"彩色", "辉光", "人物"},
    "Hip Hop": {"城市", "夜景", "霓虹", "人物"},
    "Classical": {"纹理", "水墨", "云", "森林"},
    "Jazz": {"室内", "暗黑", "金色", "复古"},
    "Folk": {"森林", "草地", "云"},
    "Funk": {"彩色", "复古"},
    "Latin": {"暖色", "人物", "沙滩"},
    "Reggae": {"沙滩", "海洋", "暖色"},
    "Soundtrack": {"云", "天空", "森林", "星空"},
    "Anime": {"彩色", "可爱", "人物"},
    "R&B": {"人物", "夜景", "复古"},
    "Soul": {"人物", "暖色", "复古"},
    "UK Garage": {"城市", "霓虹", "线条"},
    "Breakbeat": {"线条", "几何"},
    "Disco": {"光斑", "彩色", "复古"},
    "Experimental": {"抽象", "几何", "马赛克"},
    "Industrial": {"工业", "机械", "暗黑"},
}

# 曲风别名 → 表 key（小写）。处理 Discogs 原始风格词/细分词，与 GENRE_SIMPLIFY 对齐
GENRE_ALIASES = {
    "dnb": "drum & bass", "drum n bass": "drum & bass",
    "drum and bass": "drum & bass", "jungle": "drum & bass",
    "drumstep": "drum & bass", "neurofunk": "drum & bass",
    "liquid funk": "drum & bass",
    "breakcore": "hardcore", "speedcore": "hardcore", "gabber": "hardcore",
    "happy hardcore": "hardcore", "j-core": "hardcore", "frenchcore": "hardcore",
    "chillout": "downtempo", "chill-out": "downtempo", "trip hop": "downtempo",
    "idm": "experimental", "glitch": "experimental",
    "garage": "uk garage", "2-step garage": "uk garage", "speed garage": "uk garage",
    "future garage": "uk garage", "bassline": "uk garage", "grime": "uk garage",
    "breaks": "breakbeat", "big beat": "breakbeat",
    "rawstyle": "hardstyle", "uptempo hardcore": "hardcore",
    "uk hardcore": "hardcore", "industrial hardcore": "hardcore",
}

FALLBACK_HIGH = {"光斑", "粒子", "激光", "霓虹", "光线"}
FALLBACK_LOW = {"星空", "云", "渐变", "纹理", "天空"}

# 用户自定义映射（UI 编辑器保存后注入）：曲风(英文) -> [画面标签中文]。
# 非空时整体替换默认表（完整快照语义）；空则用默认 GENRE_TO_VISUAL。
_custom_map = {}


def set_custom_visual_map(m):
    """注入用户自定义的曲风→画面标签映射。传空 dict 恢复默认。"""
    global _custom_map
    _custom_map = {g: list(t) for g, t in (m or {}).items()}


def get_genre_visual_map():
    """当前生效映射：用户自定义非空则整体替换默认，否则用默认"""
    if _custom_map:
        return {g: set(t) for g, t in _custom_map.items()}
    return {g: set(v) for g, v in GENRE_TO_VISUAL.items()}


def _norm_genre(gl):
    """曲风词归一：别名表精确命中优先，再做子串命中（'Liquid DnB' 含 'dnb'）"""
    if gl in GENRE_ALIASES:
        return GENRE_ALIASES[gl]
    for k, v in GENRE_ALIASES.items():
        if k in gl:
            return v
    return gl


def genre_to_visual(genre_tags):
    """曲风标签列表（英文原词）→ 目标画面标签集合。
    先过别名归一，再精确匹配优先，否则子串匹配（细分词 "Hard Trance" 命中 "Trance"）。"""
    table = get_genre_visual_map()
    out = set()
    for g in (genre_tags or []):
        gl = _norm_genre(str(g or "").lower())
        hit = False
        for key, vis in table.items():
            if gl == key.lower():
                out |= vis
                hit = True
                break
        if not hit:
            for key, vis in table.items():
                if key.lower() in gl:
                    out |= vis
    return out


def match_clips(genre_tags, energy, library, role="bg", n=6, tier=None):
    """从素材库挑 n 个匹配素材。

    genre_tags: 曲风英文原词标签（如 ['Hardcore','Trance']）
    energy: 0..1 能量（高能量偏动态素材）
    library: {path: MediaItem}
    role: 图层角色 "fg" / "bg"
    tier: None / "low" / "high"——能量跨档时 roll 一批符合动态档的素材池：
          low = 排除高动态+频闪（不足时才回退全池）；high = 高动态/频闪前置，全池随后。
    """
    target = genre_to_visual(genre_tags)
    # 候选：角色匹配且未被排除的素材（排除=logo 等固定素材，不参与自动匹配）
    cands = [m for m in library.values()
             if m.roles.get(role) and not getattr(m, "excluded", False)]
    if not cands:
        return []

    scored = []
    for m in cands:
        tags = set(m.all_tags())
        s = len(tags & target)                      # 曲风 tag 命中数
        if s > 0:
            is_hi = DYNAMIC_HIGH in tags or DYNAMIC_FLICKER in tags
            is_lo = DYNAMIC_LOW in tags
            if energy >= 0.65:
                if is_hi:
                    s += 1.5                        # 高潮段偏好高动态/频闪
                elif is_lo:
                    s -= 1.0                        # 高潮段避开太静的素材
            elif energy < 0.50:
                if is_hi:
                    s -= 3.0                        # 缓和段强避开高动态/频闪（治"还闪"）
                elif is_lo:
                    s += 1.0                        # 缓和段偏好低动态
            scored.append((s, m))

    scored.sort(key=lambda x: -x[0])
    picked = [m for _, m in scored]

    # 兜底 1：无精确命中 → 按能量从该角色挑（颜色/动态启发）
    if not picked:
        fallback = FALLBACK_HIGH if energy >= 0.5 else FALLBACK_LOW
        scored2 = [(len(set(m.all_tags()) & fallback), m) for m in cands]
        scored2.sort(key=lambda x: -x[0])
        best = scored2[0][0] if scored2 else 0
        picked = [m for s, m in scored2 if s > 0] if best > 0 else [m for _, m in scored2]

    # 兜底 2：仍为空 → 该角色下任意素材
    if not picked:
        picked = cands

    # 低能量（<0.5）：动态档优先于曲风匹配。选材顺序：
    #   1) 曲风匹配的低动态（随机）→ 2) 其他低动态（随机，不满足曲风也优先于中动态）
    #   → 3) 中动态兜底（随机，尽量少选）。未标动态/高动态/频闪一律不选。
    if energy < 0.50:
        lo_m = [m for m in cands
                if DYNAMIC_LOW in set(m.all_tags()) and set(m.all_tags()) & target]
        lo_o = [m for m in cands
                if DYNAMIC_LOW in set(m.all_tags()) and not (set(m.all_tags()) & target)]
        if lo_m or lo_o:
            random.shuffle(lo_m)
            random.shuffle(lo_o)
            picked = lo_m + lo_o
        else:
            mid = [m for m in cands if DYNAMIC_MID in set(m.all_tags())]
            random.shuffle(mid)
            picked = mid or picked                 # 无中动态才回退（极端）
    elif tier == "high":
        hi = [m for m in picked
              if {DYNAMIC_HIGH, DYNAMIC_FLICKER} & set(m.all_tags())]
        rest = [m for m in picked
                if not ({DYNAMIC_HIGH, DYNAMIC_FLICKER} & set(m.all_tags()))]
        if hi:
            random.shuffle(hi)                      # 高动态/频闪内部随机前置，其余随后
            picked = hi + rest
        else:
            random.shuffle(picked)
    else:
        # 曲风刷新（无档位，能量 ≥0.5）：也随机，避免每次刷曲风都抽同一个
        random.shuffle(picked)

    # 频闪只在能量高(>=0.65)放行；中低能量一律从池里剔除（用户：低中不要频闪）。
    # 无论曲风刷新还是跨档重 roll，中低能量时池子都不该出现频闪素材。
    if energy < 0.65:
        no_flash = [m for m in picked if DYNAMIC_FLICKER not in set(m.all_tags())]
        picked = no_flash or picked

    return picked[:n]
