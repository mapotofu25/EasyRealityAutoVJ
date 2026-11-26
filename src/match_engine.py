# -*- coding: utf-8 -*-
"""素材匹配引擎：**图层角色 × 动态档 × 能量** → 挑素材。

★★ 2026-10-30 用户「大砍刀」后，本模块**没有曲风了**：
  删掉了曲风→画面标签映射表（33 个精调条目 + 138 个兜底条目）、别名归一、
  以及"按曲风 tag 命中打分"的主选材通道 —— 曲风识别模型（audio_genre）、
  discogs 曲风表、曲风映射编辑器一并删除。

现在选材只剩三条硬判据（全部来自确定性检测，见 `tags_def`）：
  ① **角色**：素材在某图层角色下有勾选（`m.roles[role]`），且未被"排除自动匹配"；
  ② **动态档**：低 / 中 / 高（+ 频闪），按当前能量或段落档位过滤/前置；
  ③ **能量**：<0.40 只要低动态（绝不闪）、0.40~0.75 中动态优先、≥0.75 高动态/频闪优先。
候选用完再逐级兜底（同角色任意素材），绝不返回空表让图层黑掉。
"""

import random

import sections
from tags_def import DYNAMIC_HIGH, DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_FLICKER

# ★★★ 2026-11-06：**直接引用 `sections` 的常量**，不再各存一份。
#   之前这里硬编码 0.40/0.75、`sections` 已经调到 0.35/0.72 ⇒ 库里标成 high 的段
#   （e=0.73）在选材这边按"中动态"处理，档位和显示对不上。
TIER_LO = sections.TIER_LO      # 与 `sections.TIER_LO`、`engine._energy_tier_switch` 同一套
TIER_HI = sections.TIER_HI      # 与 `sections.TIER_HI` 一致


def _tags(m):
    try:
        ts = set(m.all_tags())
    except Exception:                                          # noqa: BLE001
        return set()
    # ★★ 2026-10-30（用户「怎么会有低动态又频闪的素材呢」）：
    #   频闪是**整帧亮暗交替**，帧差中位数反而很低 ⇒ 老扫描会给它同时打上
    #   「低动态 + 频闪」（库里现有 25 个素材），tooltip 里还写着动态分 0.85，
    #   自相矛盾。换池（`_is_hi`）和打分（`score_of`）一直都把频闪当高动态用
    #   ⇒ **读标签时统一归一化**：有频闪就去掉低/中动态、补高动态。
    #   这样老库不用重扫也不会再显示出矛盾的组合。
    if DYNAMIC_FLICKER in ts:
        ts.discard(DYNAMIC_LOW)
        ts.discard(DYNAMIC_MID)
        ts.add(DYNAMIC_HIGH)
    return ts


def _is_hi(ts):
    return bool({DYNAMIC_HIGH, DYNAMIC_FLICKER} & ts)


#: 每档的"目标动态分"（`tagger.dynamic_score` 的刻度：<0.33 低 / 0.33~0.67 中 / >0.67 高）
_TIER_ANCHOR = {"low": 0.20, "mid": 0.55, "high": 0.85}


def score_of(m):
    """素材的**动态分**（0~1）；没打过分时按三档给一个代理值。

    ★ 老素材库只存了三档（`clip_tags`），没有帧差 ⇒ 没有精确分。这时不能让它们
      一律按 0.5 排（会把整批老素材挤到中间、看着像"分数功能没生效"）。
      按档位中点取代理值：低 0.20 / 中 0.55 / 高或频闪 0.85。
      跑一次「扫描素材」就会写真实的 `clip_score`，代理值自动让位。
    """
    s = getattr(m, "score", None)
    if isinstance(s, (int, float)):
        return max(0.0, min(1.0, float(s)))
    tg = _tags(m)
    if DYNAMIC_HIGH in tg or DYNAMIC_FLICKER in tg:
        return 0.85
    if DYNAMIC_MID in tg:
        return 0.55
    if DYNAMIC_LOW in tg:
        return 0.20
    return 0.5


def _by_score(group, target, jitter=0.25):
    """组内按「动态分离目标多近」排序，带随机抖动。

    ★ 2026-10-30 用户：「给素材也打分…roll 素材池时优先挑选贴近的数字」。
      只在这个**档位组内部**调整顺序 —— 档位（低/中/高）仍然一票否决，
      分数只决定"同一档里先挑谁"。
    抖动 `jitter` 是必须的：没有它，同一批里分数最接近的几首会被**每次都选中**，
    换池就变成"永远那几个"。0.25 相当于"差 0.25 分以内的素材视为并列，随机"。
    没打分的素材按 0.5（中性）参与，不会因为缺数据被系统性排到最后。
    """
    if not group:
        return group

    return sorted(group, key=lambda m: abs(score_of(m) - target)
                  + random.random() * jitter)


def match_clips(energy, library, role="bg", n=8, tier=None, exclude_paths=None):
    """从素材库挑素材（按动态档 + 能量，不再看曲风）。

    :param n: 最多返回几个；``None``/``<=0`` = 返回该档全部候选。
    :param exclude_paths: 刚进过池/刚播过的素材路径；优先避开，数量不够时才回填。
    """
    all_cands = [m for m in library.values()
                 if getattr(m, "roles", {}).get(role)
                 and not getattr(m, "excluded", False)]
    if not all_cands:
        return []
    _ex = {str(p) for p in (exclude_paths or ()) if p}
    cands = [m for m in all_cands if str(getattr(m, "path", "")) not in _ex]
    excluded = [m for m in all_cands if str(getattr(m, "path", "")) in _ex]
    if not cands:                     # 全被排除：只能回填，绝不返回空池
        cands, excluded = excluded, []

    eff = tier if tier in ("low", "mid", "high") else (
        "high" if energy >= TIER_HI else ("low" if energy < TIER_LO else "mid"))

    # ★ 2026-10-30：目标动态分 = 档位锚点与实测能量的加权（能量越接近哪档就越往那档靠）
    _anchor = _TIER_ANCHOR.get(eff, 0.55)
    try:
        _e = max(0.0, min(1.0, float(energy)))
    except (TypeError, ValueError):
        _e = 0.5
    target = 0.6 * _anchor + 0.4 * _e

    if eff == "high":
        hi = [m for m in cands if _is_hi(_tags(m))]
        rest = [m for m in cands if m not in hi]
        picked = _by_score(hi, target) + _by_score(rest, target)
    elif eff == "low":
        # 缓和段：低动态优先 → 中动态兜底 → 全池（**绝不主动选高动态/频闪**）
        lo = [m for m in cands if DYNAMIC_LOW in _tags(m)]
        mid = [m for m in cands if DYNAMIC_MID in _tags(m)]
        picked = _by_score(lo, target) + _by_score(mid, target)
        if not picked:
            picked = list(cands)
    else:
        # 中段：中动态优先 → 低动态随后 → 未标兜底；高动态/频闪不进中档池
        mid = [m for m in cands if DYNAMIC_MID in _tags(m)]
        lo = [m for m in cands if DYNAMIC_LOW in _tags(m)]
        rest = [m for m in cands if not _is_hi(_tags(m))
                and DYNAMIC_MID not in _tags(m) and DYNAMIC_LOW not in _tags(m)]
        picked = (_by_score(mid, target) + _by_score(lo, target)
                  + _by_score(rest, target))
        if not picked:
            picked = list(cands)

    # 频闪只在能量高(≥0.75)放行；中低能量一律从池里剔除（用户：低中不要频闪）。
    if energy < TIER_HI:
        no_flash = [m for m in picked if DYNAMIC_FLICKER not in _tags(m)]
        picked = no_flash or picked

    if n is None or int(n) <= 0:
        return picked
    lim = int(n)
    out = picked[:lim]
    if len(out) < lim:
        for m in excluded:
            if len(out) >= lim:
                break
            if m not in out:
                out.append(m)
    return out
