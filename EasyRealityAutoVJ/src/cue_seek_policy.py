# -*- coding: utf-8 -*-
"""码表的 **seek 策略**（漂移判定 / 档位 / 落地 / 计速）—— **纯逻辑，零 Qt，零 player 依赖**。

═══ 为什么不让引擎直接判（别把它搬回 `engine.py`）═══
这四类判断（要不要 seek、阈值多少、有没有落地、解码器 speed 取几）全是**纯逻辑**，
但它们决定了"画面贴不贴拍"和"会不会抖动自激"，是最容易写错、又最"看起来能跑"的一段：
  · 用"请求目标"当漂移基准 ⇒ 自己跟自己比 ⇒ 永远不触发（画面不报错，就是不贴拍）；
  · `src_ended` 期间还在校正 ⇒ 解码器 pts 继续涨 ⇒ 每帧都判超阈值 ⇒ 每帧 seek；
  · seek 未落地就重发 ⇒ 长 GOP 素材上自激；
  · `set_speed` 与 `play_sec` 同时计速 ⇒ 实测 2 倍速。
放进 `engine.py` 就只能"开软件、放一首、盯着看"，**没法逐条断言、没法做变异验证**。
放在这里之后：`tools/cue_should_seek_test.py` 用假播放器桩就能把 25 个用例 + 5 个变异全部钉死
（`--impl real` 直接测本模块，不再测"抄来的参考实现"）。

**纪律**
  · 不 import `PySide6` / `engine` / `media_manager`；**不持有 player 对象**（只在调用时把它读出的
    `pos` / `fps` / `speed` 传进来）。⇒ 可以脱离 GUI 跑自检：`python -m cue_seek_policy`。
  · 计速**只有一个**（见 `player_speed`）：本设计走"播放器计速"，`set_speed` 恒为 `ps.speed`，
    **永不出现 1.0**（1.0 只在"每帧注入 seek、由 play_sec 主导"的方案里才需要，那是被禁止的形状）。
  · 阈值的来源是**实测**（`tools/_cue_seek_probe.md`）：seek 成本 ∝ 素材 GOP 长度。
"""
from __future__ import annotations

import time as _time

# ==========================================================================
# 常量（与 docs/码表实装_Step4_任务书草案.md §4.1 一一对应）
# ==========================================================================
DRIFT_SEC = 0.35              # 普通档：漂移超过它才 seek（≈1 帧@30fps 的十几倍，抗抖动）
DRIFT_SEC_CONSERVATIVE = 1.00  # 保守档（HEVC / 长 GOP）：只入场 + 预热，容差放宽
DRIFT_SEC_FREE = 0.35         # free 档（DXV）：**也不逐秒**（裁定 Q2：逐秒 seek 会让时间不连续）
MIN_INTERVAL_SEC = 1.0        # 同一 player 两次 seek 的最小间隔（防连续 seek）
SEEK_TIMEOUT_SEC = 2.0        # 未落地超时 ⇒ 判失败并降级（不再 seek 本片段）
GOP_CONSERVATIVE = 60         # 平均 GOP > 60 帧（≈2s@30fps）⇒ 升保守档
DEFAULT_FPS = 30.0            # 拿不到 fps 时的兜底（只用于换算"2 帧"容差）

# ==========================================================================
# ★★★ 音画对齐机制（四级，2026-10-18 定稿）
# ==========================================================================
# 用户诉求原文：「默认按编排正常播放；出现明显偏差时用**轻微调整播放速度**平滑对齐；
#   只有偏差非常明显且微调纠正不了，才强制切换/跳转；要保证正常播放的连续性。」
#
# ── 对齐判据（唯一误差定义）────────────────────────────────────────────
#     e = (play_sec + LEAD) − player.position()
#   `play_sec`  = 编排在这一刻**应该显示**的素材内秒（由指纹位置推出）
#   `LEAD`      = 前馈补偿（见下），抵消解码管线的固有延迟
#   `position()`= 播放器**实际显示帧**的素材内秒（引擎侧读数，唯一可信来源）
#   e > 0 ⇒ 视频落后于编排；e < 0 ⇒ 视频超前
#   ⚠ **绝不能拿"上次请求的目标"当基准**（自己跟自己比，永远判不出偏差）。
#
# ── 为什么有 LEAD（前馈补偿）─────────────────────────────────────────
#   `position()` 报的是"最后解出的那一帧"，解码管线天生落后 1~2 帧
#   （30fps ⇒ 33~66ms）。这段延迟**不是漂移**，靠反馈越追越糟（会让素材永远快 6%）。
#   前馈一个固定小量（视频略微**早**于音频）主观上最自然：
#   人耳对"画面早 50ms"几乎无感，对"画面晚 150ms"很敏感（声音先到的心理声学效应）。
LEAD_SEC = 0.06
#
# ── 四级调节策略 ─────────────────────────────────────────────────────
#   ①   |e| ≤ DEADBAND(0.05)        → 名义倍速，**一个字节都不改**（正常播放零干扰）
#   ②   DEADBAND < |e| ≤ SOFT(0.30) → **比例变速**：speed = 名义 × (1 + e/CATCHUP)，
#                                      ±RATE_MAX 封顶。纯 set_speed，画面连续不跳帧。
#   ③   SOFT < |e| 且 < SEEK_SOFT    → 倍速**拉满**去追；同时开始"滞留计时"
#   ④   |e| ≥ SEEK_SOFT(0.60)        → 若**持续 SEEK_DWELL(2.0s)** 仍无改善 ⇒ 强制 seek；
#       |e| ≥ SEEK_HARD(1.20)        → 立即 seek（大跳变：DJ 拖动 / 识别重新定位）
#   跳完 SEEK_COOLDOWN(1.2s) 内不再跳（防抖，避免"跳—超调—再跳"的自激）
#
# ── 连续性保证 ───────────────────────────────────────────────────────
#   · 只有 ④ 会改变画面位置；①②③ 全是**改速率**，画面永不跳。
#   · ④ 的软阈值必须"持续 2 秒"才动手 ⇒ 识别抖动/短时卡顿（1~2s）根本触发不了。
#   · `src_ended`（到出点冻结）期间一律不 seek、不调速（持帧是编排意图）。
#   · `off` 档（现场应急）一字节都不改。
RATE_DEADBAND_SEC = 0.05      # ① 死区
RATE_SOFT_SEC = 0.30          # ② 比例段的右端（到这里倍速正好拉满）
RATE_CATCHUP_SEC = 1.5        # 比例段增益：希望 1.5s 追平
RATE_MAX = 0.25               # 倍速上限 ±25%（宁可短暂快慢，也不要 seek 跳帧）
SEEK_SOFT_SEC = 0.60          # ④-软：开始滞留计时
SEEK_DWELL_SEC = 2.0          # ④-软：持续这么久没改善才跳
SEEK_HARD_SEC = 1.20          # ④-硬：立即跳
SEEK_COOLDOWN_SEC = 1.2       # 跳后冷却（防抖）


PROFILE_AUTO = "auto"
PROFILE_CONSERVATIVE = "conservative"
PROFILE_OFF = "off"

MODE_FREE = "free"            # DXV：每帧独立，seek ≈1.85ms
MODE_NORMAL = "normal"        # H.264：seek ≈53~76ms
MODE_CONSERVATIVE = "conservative"   # HEVC/长 GOP：seek ≈192~848ms
MODE_OFF = "off"              # 配置强制关闭（现场应急）


# ==========================================================================
# 档位（全部 O(1) 只读；禁止在此做任何探测/IO）
# ==========================================================================
def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def mode_for(item, profile=PROFILE_AUTO):
    """档位：`free` / `normal` / `conservative` / `off`。

    判据顺序 = 任务书 §4.3 的三道闸门（配置 → codec → GOP 缓存）。
    ⚠ **探测不到编码一律按 `normal`（H.264 档），绝不猜成 `free`/DXV**：
      猜错成 free 会让长 GOP 素材也走"允许频繁校正"，画面持续跳。
    """
    prof = str(profile or PROFILE_AUTO)
    if prof == PROFILE_OFF:
        return MODE_OFF
    if prof == PROFILE_CONSERVATIVE:
        return MODE_CONSERVATIVE
    if item is None:
        return MODE_NORMAL
    gop = getattr(item, "_cue_gop_frames", None)
    if (getattr(item, "_is_dxv", False)
            or str(getattr(item, "_codec", "")) == "dxv"
            or (gop is not None and _f(gop, 1e9) <= 2)):
        return MODE_FREE
    if (str(getattr(item, "_codec", "")) == "hevc"
            or (gop is not None and _f(gop, 0) > GOP_CONSERVATIVE)):
        return MODE_CONSERVATIVE
    return MODE_NORMAL


def drift_limit_for_mode(mode):
    """档位 → 漂移阈值（秒）。"""
    if mode == MODE_CONSERVATIVE:
        return DRIFT_SEC_CONSERVATIVE
    if mode == MODE_FREE:
        return DRIFT_SEC_FREE
    return DRIFT_SEC


def seek_drift_limit(item, profile=PROFILE_AUTO):
    """档位 → 漂移阈值（秒）。档位判定走 `mode_for`（纯函数，可单测）。"""
    return drift_limit_for_mode(mode_for(item, profile))


# ==========================================================================
# 落地判定
# ==========================================================================
def land_tol(fps):
    """落地容差 = 2 帧（默认 30fps ⇒ ≈66ms）。

    ⚠ 这是"判定 seek 有没有落地"的容差，**不是**漂移阈值（漂移阈值见 `seek_drift_limit`）。
      给 2 帧是为了容忍 pts 取整与变帧率素材。
    """
    f = _f(fps, 0.0)
    return 2.0 / (f if f > 1.0 else DEFAULT_FPS)


def landed(pos, target, tol):
    """`pos`（实际显示帧）是否已落到 `target` 附近。`pos is None` ⇒ 未落地（不可信）。"""
    if pos is None or target is None:
        return False
    return abs(_f(pos) - _f(target)) <= _f(tol, 0.0)


# ==========================================================================
# 计速（唯一取值点）
# ==========================================================================
def player_speed(ps):
    """解码器该设的倍速：**恒为 `ps.speed`**（主路径 = 播放器计速）。

    ⛔ **永不返回 1.0**（除非 ps.speed 本来就是 1.0）：
      返回 1.0 意味着"改由 play_sec 主导"，那就必须**每帧注入 seek** —— 被明令禁止的形状
      （长 GOP 素材每次 53~848ms，必然掉帧）。详见任务书 §5.3。
    """
    return max(1e-6, _f(getattr(ps, "speed", 1.0), 1.0))


def sync_target(ps):
    """这一刻**应该显示**的素材内秒（含前馈补偿 `LEAD_SEC`）。

    `src_ended`（到出点冻结）时不加 LEAD —— 冻结就该停在最后一帧，别去追。
    """
    t = _f(getattr(ps, "play_sec", 0.0))
    if getattr(ps, "src_ended", False):
        return t
    return t + LEAD_SEC


def rate_for(ps, pos, mode=MODE_NORMAL):
    """**第 ①②③ 级：按偏差给本帧该设的倍速**（= 名义倍速 × (1 + 修正)）。

    `pos` = 播放器**实际显示帧**的素材内秒（`player.position()`），取不到给 `None`。
    `e = sync_target(ps) - pos`：>0 表示画面落后于编排 ⇒ 加速追。

    返回 `ps.speed`（死区 / 不可信 / 冻结 / off 档）或 `ps.speed × (1 ± ≤RATE_MAX)`。

    ⚠ 与 `player_speed()` 的关系：本函数**包含**它（基准就是它），不是另起一套计速。
      两者都只返回"该设多少"，不记账、不读时钟 ⇒ 纯函数、可单测。
    ⚠ `MODE_OFF`（现场应急关掉定位）⇒ 一字节都不改，返回名义倍速。
    """
    base = player_speed(ps)
    if mode == MODE_OFF or pos is None or getattr(ps, "src_ended", False):
        return base
    if not getattr(ps, "active", True) or getattr(ps, "missing", False):
        return base
    e = sync_target(ps) - _f(pos)
    if abs(e) <= RATE_DEADBAND_SEC:
        return base                      # ① 正常播放：零干扰
    # ② ③ 比例变速（到 RATE_SOFT_SEC 时正好拉满，之后保持满速追）
    adj = max(-RATE_MAX, min(RATE_MAX, e / max(1e-6, RATE_CATCHUP_SEC)))
    return max(1e-6, base * (1.0 + adj))


# ==========================================================================
# 状态
# ==========================================================================
def _trip_breaker(st, now, base=1.0, cap=8.0):
    """把该子层的 seek 熔断一段冷却时间（**可恢复**，不是永久停，铁律 0）。

    退避：1s → 2s → 4s → 8s（封顶）。每次失败翻倍，成功落地即清零（见 `settle`）。
    """
    n = int(st.get("fail_n", 0) or 0) + 1
    st["fail_n"] = n
    st["degraded"] = True
    st["retry_at"] = _f(now) + min(cap, base * (2 ** min(3, n - 1)))
    return st["retry_at"]


def new_state():
    """单个子层的 seek 状态（键名与引擎 `lay._cue_seek_state[lyr_id]` 一致）。"""
    return {"item_id": "", "last_req": -1e9, "inflight": False, "target": 0.0,
            "degraded": False, "from_pos": None,
            # ★ 熔断不是永久：`fail_n` 连续失败次数，`retry_at` 下次允许尝试的时刻
            "fail_n": 0, "retry_at": 0.0,
            # ★ 音画对齐（第④级）用：进入软阈值的时刻 / 上次真 seek 的时刻
            "soft_since": None, "last_seek": -1e9,
            # ★ 硬阈值"连击"计数：单帧毛刺不许触发跳转（见 should_seek 9b）
            "hard_n": 0}


def settle(pos_now, now, st, tol):
    """结算上一次 seek：返回 `"landed"` / `"timeout"` / `None`，并就地更新 `st`。

    ★ 只清 in-flight 的两个条件（任务书 §3.3）：落地（≤2 帧）或超时（>2s）。
    """
    if not st.get("inflight"):
        return None
    target = _f(st.get("target"))
    if landed(pos_now, target, tol):
        st["inflight"] = False
        st["fail_n"] = 0            # ★ 落地成功 ⇒ 熔断计数清零（必须成对）
        return "landed"
    # ★ 越过目标也算落地：播放器是**继续往前走**的，seek 到 target 之后 `position()` 会很快越过
    #   target（cv2 的 POS_MSEC 还会多报 1~2 帧）。只判 `|pos-target| <= 2 帧` 会永远判不出落地
    #   ⇒ 2s 后误判超时 ⇒ `degraded` ⇒ 整段再也不校正（实测：漂移一路涨到 0.44 却只 seek 了 2 次）。
    frm = st.get("from_pos")
    if pos_now is not None and frm is not None:
        if _f(frm) <= target and _f(pos_now) >= target - _f(tol):
            st["inflight"] = False
            st["fail_n"] = 0
            return "landed"
        if _f(frm) > target and _f(pos_now) <= target + _f(tol):
            st["inflight"] = False
            st["fail_n"] = 0
            return "landed"
    if (now - _f(st.get("last_req"), 0.0)) > SEEK_TIMEOUT_SEC:
        st["inflight"] = False
        _trip_breaker(st, now)
        return "timeout"
    return None


# ==========================================================================
# 核心判定（11 条，顺序即语义；纯函数，不碰 player）
# ==========================================================================
def should_seek(ps, pos, now, st, item, profile=PROFILE_AUTO):
    """本帧要不要向播放器投递一次 `seek(ps.play_sec)`。

    参数都是**值**（不是对象）：`pos` = 播放器当前实际显示帧的素材内秒（`position()`），
    取不到给 `None`；`now` = 单调时钟；`st` = `new_state()` 那份状态。

    ⚠ 第 11 条必须用 `pos`（实际值），**不能用 `st["target"]`（请求值）** ——
      用请求值 = 自己跟自己比 ⇒ 永远不触发，而且"看起来能跑"（这是本模块最重要的注释）。
    """
    # 1) 没素材 / 未激活 ⇒ 不 seek
    if getattr(ps, "missing", False) or not getattr(ps, "active", True):
        return False
    # 2) 出点冻结（持帧）⇒ 不 seek。★ 防自激：持帧期间解码器 pts 仍涨，否则每帧都判超阈值
    if getattr(ps, "src_ended", False):
        return False

    # 3) 换片段：**先清掉上一条留下的"降级/目标/节流"**（R11）。
    #    不清的后果：上一条 seek 失败置了 `degraded`，新片段会被它**永久**关掉校正
    #    —— 肉眼表现是"切过去之后再也不跟拍"（不报错、不黑屏，极难发现）。
    #    ⚠ 但 `inflight` **不能清**：那是同一个 player 上还没落地的请求，
    #      清了就会在同一 player 上叠加请求（测试 ㉕ 专门钉这条）。
    item_id = str(getattr(ps, "item_id", "") or "")
    switched = str(st.get("item_id", "")) != item_id
    if switched and not st.get("inflight", False):
        st["degraded"] = False
        st["target"] = 0.0
        st["last_req"] = -1e9              # 入场不受节流限制

    # 4) 熔断中（seek 失败/超时）⇒ 冷却期内不 seek；**到期必须放行一次**
    #    ⚠ 旧写法是"置 degraded 就整段不再 seek" —— 那是标准的"一次失败永久停"
    #      （铁律 0）。现场后果：某次 seek 因解码线程忙而超时 ⇒ 本片段**再也不校正**
    #      ⇒ 位置一路漂到片尾 = 用户说的"越久越对不齐"。
    if st.get("degraded", False):
        if _f(now) < _f(st.get("retry_at"), 0.0):
            return False
        st["degraded"] = False
        st["inflight"] = False
    # 5) 上一次还没落地 ⇒ 不重发。★ 防自激（长 GOP 落地要 664~848ms）
    if st.get("inflight", False):
        return False

    mode = mode_for(item, profile)
    # 6) 换片段 ⇒ 立即 seek（off 档仍然不 seek；节流不适用）
    if switched:
        return mode != MODE_OFF
    # 7) 配置强制关闭
    if mode == MODE_OFF:
        return False
    # 8) 保守档（HEVC/长 GOP）只入场 + 预热，不做运行中校正
    if mode == MODE_CONSERVATIVE:
        return False
    # 9) 位置读数不可信（首帧未就绪 / 格式不报 pts）⇒ 跳过校正（不是错误）
    if pos is None:
        return False
    # 9b) ④-硬：**大跳变不受节流限制** —— 它就是"必须马上落地"的场景。
    #     放在节流之前判：否则刚跳完 1 秒内再来一次大跳变（DJ 连续拖动）会被节流吞掉，
    #     画面就一直卡在错的位置直到下次机会。
    if abs(sync_target(ps) - _f(pos)) >= SEEK_HARD_SEC:
        # ★ 必须**连续 2 帧**都超硬阈值才算数（=33ms，肉眼无感）。
        #   真机抓到过单帧 `dmax=1.31s` 的毛刺（某一层解码线程偶尔停一拍），
        #   单帧就跳会让画面"时不时抽一下"——那正是用户说的"一直跳"。
        st["hard_n"] = int(st.get("hard_n", 0) or 0) + 1
        if st["hard_n"] >= 2:
            st["hard_n"] = 0
            return True
        return False
    st["hard_n"] = 0
    # 10) 节流：同一 player 两次 seek 至少隔 MIN_INTERVAL_SEC
    last_req = _f(st.get("last_req"), -1e9)
    if now - last_req < MIN_INTERVAL_SEC:
        return False
    # 11) 漂移超**硬阈值** ⇒ 校正（用实际位置 pos，不是请求目标 target）
    #     ★ 2026-10-17：原来用 `drift_limit_for_mode()`（0.35s）⇒ 每 1s seek 一次 ⇒ 卡顿。
    #       现在小幅漂移交给 `rate_for()` 用倍速追（画面连续），这里只处理
    #       "追不回来"的大跳变（指纹重新锁定 / 用户拖了播放位置 / 解码器停了很久）。
    ref = pos
    e = sync_target(ps) - _f(ref)
    ae = abs(e)
    # ④-软：明显但"可能追得回来" ⇒ 必须**持续 SEEK_DWELL_SEC 没改善**才动手
    #   这条是"连续性"的核心：识别抖动/短时卡顿（1~2s）根本触发不了 seek。
    if ae >= SEEK_SOFT_SEC:
        if _f(now) - _f(st.get("last_seek"), -1e9) < SEEK_COOLDOWN_SEC:
            return False                  # 刚跳过 ⇒ 冷却，防"跳—超调—再跳"自激
        since = st.get("soft_since")
        if since is None:
            st["soft_since"] = _f(now)    # 开始计时
            return False
        return (_f(now) - _f(since)) >= SEEK_DWELL_SEC
    # 回到死区/比例段 ⇒ 计时清零（下次要重新计满 2 秒）
    st["soft_since"] = None
    return False
    # 12) 否则不动
    return False


def on_seek(ps, now, st, accepted, pos=None):
    """投递之后记账（不管受理与否都要记 `last_req`，否则会 1 帧一个请求）。

    `pos`：投递时的实际位置（给 `settle` 判"是否已越过目标"用；缺省 None = 只按绝对误差判）。
    """
    st["item_id"] = str(getattr(ps, "item_id", "") or "")
    st["target"] = _f(sync_target(ps))
    st["last_seek"] = _f(now)
    st["soft_since"] = None
    st["from_pos"] = None if pos is None else _f(pos)
    st["last_req"] = _f(now)
    if accepted:
        st["inflight"] = True
    else:
        _trip_breaker(st, now)         # 不受理 ⇒ 熔断（带退避，到期自动重试）
    return bool(accepted)


# ==========================================================================
# 薄适配器：给引擎与单测一个"有状态"的门面（**不持有 player**）
# ==========================================================================
class CueSeekPolicy:
    """每张码表一个实例。持有各子层的 `st` 与配置；**不持有 player / 不持有 Qt 对象**。

    引擎侧用法（`_cue_frame` 内，见任务书 §5.2）：
        st = pol.state_for(ps)                    # 或自管 dict
        pol.settle(ps, player, st)                # ② 结算上次 seek
        if pol.should_seek(ps, item, player, st): # ③ 判定
            pol.on_seek(ps, st, player.seek(ps.play_sec))
        player.set_speed(pol.desired_speed(ps))   # ④ 计速（恒 ps.speed）
    """

    def __init__(self, cfg=None, now=None):
        """`now`：单调时钟。**默认用 `time.monotonic`**（真实运行）。

        ⚠ 单测里务必显式传假时钟；**不要把默认值写成常量**——那会让"节流/超时"永远成立：
          引擎不传时钟时 `now - last_req` 恒为 0 ⇒ `MIN_INTERVAL` 永远拦住 ⇒ **seek 只发生一次**，
          漂移会一路涨（这条 bug 真机跑 6 秒才暴露：漂移 0.48s 却不再校正）。
        """
        self.cfg = dict(cfg or {})
        self._now = now or _time.monotonic
        self.state = {}

    # ---- 配置 ----
    @property
    def profile(self):
        return str(self.cfg.get("seek_profile", PROFILE_AUTO) or PROFILE_AUTO)

    # ---- 纯函数的薄包装 ----
    def mode(self, item):
        return mode_for(item, self.profile)

    def drift_limit(self, mode):
        return drift_limit_for_mode(mode)

    def seek_enabled(self, item=None):
        """本片段是否允许 seek（**`off` 档一律不允许**）。

        为什么要单独一个口子：引擎侧"新解码器首次落地"那条 seek 不走 `should_seek`，
        如果它不看档位，`off`（现场应急关闭定位）就会**仍然 seek** ——
        `tools/cue_settings_test.py` 的「off 完全不 seek」就是钉这个。
        """
        return mode_for(item, self.profile) != MODE_OFF

    def land_tol(self, player):
        return land_tol(getattr(player, "fps", 0.0))

    def desired_speed(self, ps, pos=None):
        """本帧该设的倍速（实时变速对齐）。`pos` 不给 ⇒ 名义倍速（旧调用方兼容）。"""
        try:
            mode = mode_for(None, self.profile)
        except Exception:                                         # noqa: BLE001
            mode = MODE_NORMAL
        return rate_for(ps, pos, mode)

    def desired_speed_for(self, ps, item=None, pos=None):
        """同 `desired_speed`，但按**本片段素材**判档位（DXV=free / HEVC=conservative）。

        ★ 2026-10-18（真机联调）：**换片段/落地中一律走名义倍速**。
          换片段那一瞬间 `play_sec` 会跳回新片段的 `src_in`（常常是 0），而播放器还在
          旧位置 ⇒ 误差看起来有"快一个片段的长度"（真机抓到 19.7s）⇒ 若照常按 ②③ 倍速
          去追，画面会在切换瞬间猛快/猛慢一下。这段窗口里真正要做的是**等落地**，
          不是在旧画面上变速。
        """
        try:
            st = self.state_for(ps)
            if st.get("inflight") or st.get("switched_recently"):
                return player_speed(ps)
            if (self._now() - float(st.get("last_seek", -1e9))) < 0.35:
                return player_speed(ps)
        except Exception:                                         # noqa: BLE001
            pass
        return rate_for(ps, pos, mode_for(item, self.profile))

    def seek_target(self, ps):
        """真要 seek 时**落到哪一秒**（= 编排目标 + 前馈补偿）。"""
        return sync_target(ps)

    # ---- 状态 ----
    def state_for(self, ps):
        key = getattr(ps, "lyr_id", "") or ""
        st = self.state.get(key)
        if st is None:
            st = new_state()
            self.state[key] = st
        return st

    def reset(self):
        """切歌/卸表时清状态（配合 `clear_cue_sheet`）。"""
        self.state.clear()

    def _read_pos(self, player):
        """读播放器实际显示位置。**没有 `position()` 的播放器（静态图）⇒ None**。

        ⚠ 2026-10-17：`ImagePlayer` 没有 `position()`，旧写法直接 `player.position()`
          会每帧抛 AttributeError（现场日志刷 `码表 seek 策略异常`）。这里统一兜住。
        """
        fn = getattr(player, "position", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception:                                         # noqa: BLE001
            return None

    # ---- 四步 ----
    def settle(self, ps, player, st):
        pos = self._read_pos(player)
        return settle(pos, self._now(), st, self.land_tol(player))

    def should_seek(self, ps, item, player, st):
        pos = self._read_pos(player)
        return should_seek(ps, pos, self._now(), st, item, self.profile)

    def on_seek(self, ps, st, accepted, pos=None):
        return on_seek(ps, self._now(), st, accepted, pos)


# ==========================================================================
# 自检（零 Qt，可 `python -m cue_seek_policy`）
# ==========================================================================
def selftest():
    """最小自检：档位三档 + 计速恒等 + 漂移用实际位置 + 三条防自激。"""

    class _Item:
        def __init__(self, codec="h264", dxv=False, gop=None):
            self._codec = codec
            self._is_dxv = dxv
            if gop is not None:
                self._cue_gop_frames = gop

    class _PS:
        def __init__(self, **kw):
            self.lyr_id = "L1"
            self.item_id = "i1"
            self.play_sec = 0.0
            self.speed = 1.0
            self.src_ended = False
            self.active = True
            self.missing = False
            for k, v in kw.items():
                setattr(self, k, v)

    # 1) 档位
    assert mode_for(_Item(codec="dxv")) == MODE_FREE
    assert mode_for(_Item(codec="h264")) == MODE_NORMAL
    assert mode_for(_Item(codec="hevc")) == MODE_CONSERVATIVE
    assert mode_for(_Item(codec="", gop=1)) == MODE_FREE
    assert mode_for(_Item(codec="h264", gop=GOP_CONSERVATIVE + 1)) == MODE_CONSERVATIVE
    assert mode_for(None) == MODE_NORMAL          # 未知 ⇒ normal，绝不猜 DXV
    assert mode_for(_Item(codec="dxv"), PROFILE_OFF) == MODE_OFF

    # 2) 阈值
    assert drift_limit_for_mode(MODE_FREE) == DRIFT_SEC_FREE == DRIFT_SEC
    assert drift_limit_for_mode(MODE_NORMAL) == DRIFT_SEC
    assert drift_limit_for_mode(MODE_CONSERVATIVE) == DRIFT_SEC_CONSERVATIVE

    # 3) 落地 / 计速
    assert land_tol(60.0) < land_tol(30.0)
    assert landed(1.0, 1.01, land_tol(30.0)) is True
    assert landed(None, 1.0, 0.07) is False
    assert player_speed(_PS(speed=2.0)) == 2.0
    assert player_speed(_PS(speed=0.0)) > 0.0     # 防御：0 会给到 1e-6，不是 1.0

    # 4) 漂移用实际位置（不是请求目标）—— 本模块最重要的一条
    #    ★ 2026-10-17：硬阈值抬到 SEEK_HARD_SEC(1.2)；0.35~1.2 改由倍速对齐处理。
    #      本断言要钉的是"用 pos 不用 target"，与阈值无关 ⇒ 把漂移拉到硬阈值之上。
    ps, item = _PS(play_sec=10.0), _Item()
    st = new_state()
    st["item_id"] = "i1"
    st["target"] = 10.0                            # 上次请求值 == play_sec
    # ★ 硬阈值现在要求连续 2 帧（抗单帧毛刺）
    assert (should_seek(ps, 8.0, 100.0, st, item)
            or should_seek(ps, 8.0, 100.0, st, item)) is True, "必须用 pos 判漂移"
    assert should_seek(ps, 9.9, 100.0, st, item) is False, "0.1 远小于阈值，不该 seek"
    # ★ 中段漂移（0.35~1.2）不 seek，交给倍速
    assert should_seek(ps, 9.4, 100.0, st, item) is False, "0.6s 漂移应由倍速追，不 seek"

    # 4b) 音画对齐（rate_for）：连续、有界、可退化
    #   ★ 现在误差基准是 `sync_target(ps) = play_sec + LEAD_SEC`（前馈补偿解码管线延迟）。
    _tgt = sync_target(_PS(play_sec=10.0))
    assert abs(_tgt - (10.0 + LEAD_SEC)) < 1e-9, "目标 = 编排 + 前馈补偿"
    assert rate_for(_PS(play_sec=10.0), _tgt) == 1.0, "无偏差 ⇒ 名义倍速（正常播放零干扰）"
    assert rate_for(_PS(play_sec=10.0), _tgt - 0.01) == 1.0, "死区内不纠正"
    _up = rate_for(_PS(play_sec=10.0), _tgt - 0.3)
    assert _up > 1.0 and _up <= 1.0 + RATE_MAX + 1e-9, "落后 ⇒ 加速且钳位（%.4f）" % _up
    _dn = rate_for(_PS(play_sec=10.0), _tgt + 0.3)
    assert _dn < 1.0 and _dn >= 1.0 - RATE_MAX - 1e-9, "超前 ⇒ 减速且钳位（%.4f）" % _dn
    assert rate_for(_PS(play_sec=10.0, speed=2.0), _tgt - 1.0) > 2.0, "倍速要对 ps.speed 乘"
    assert rate_for(_PS(play_sec=10.0), None) == 1.0, "pos 不可信 ⇒ 名义倍速"
    assert rate_for(_PS(play_sec=10.0, src_ended=True), 1.0) == 1.0, "持帧 ⇒ 名义倍速"
    assert rate_for(_PS(play_sec=10.0), _tgt - 1.0, MODE_OFF) == 1.0, "off 档一字节都不改"

    # 5) 三条防自激
    assert should_seek(_PS(play_sec=10.0, src_ended=True), 0.0, 100.0,
                       dict(st), item) is False, "持帧期间不许 seek"
    st2 = new_state()
    st2["item_id"] = "i1"
    st2["inflight"] = True
    assert should_seek(ps, 0.0, 100.0, st2, item) is False, "in-flight 期间不许重发"
    assert should_seek(ps, None, 100.0, dict(st), item) is False, "pos=None 跳过校正"

    # 6) 节流 + 换片段
    st3 = new_state()
    st3["item_id"] = "i1"
    st3["last_req"] = 100.0
    # ★ 偏差 0.10 落在"比例段" ⇒ 靠变速解决，**任何时刻都不 seek**
    _p3 = sync_target(ps) - 0.10
    assert should_seek(ps, _p3, 100.5, st3, item) is False, "比例段不 seek"
    assert should_seek(ps, _p3, 101.5, st3, item) is False, "比例段不 seek（过窗后也一样）"
    # ★ 软阈值 + 节流：0.70 偏差要先过冷却、再滞留满 SEEK_DWELL 才跳
    st7 = new_state()
    st7["item_id"] = "i1"
    st7["last_req"] = 100.0
    _p7 = sync_target(ps) - 0.70
    assert should_seek(ps, _p7, 100.5, st7, item) is False, "节流窗口内不 seek"
    assert should_seek(ps, _p7, 101.5, st7, item) is False, "刚进软阈值：开始计时，不当场跳"
    assert should_seek(ps, _p7, 103.8, st7, item) is True, "滞留满 2 秒 ⇒ seek"
    st4 = new_state()
    st4["item_id"] = "OLD"
    st4["last_req"] = 100.0
    assert should_seek(ps, 9.9, 100.0, st4, item) is True, "换片段不受节流限制"

    # 7) 结算
    st5 = new_state()
    st5["inflight"] = True
    st5["target"] = 5.0
    st5["last_req"] = 0.0
    assert settle(5.01, 0.5, st5, land_tol(30.0)) == "landed"
    st6 = new_state()
    st6["inflight"] = True
    st6["target"] = 5.0
    st6["last_req"] = 0.0
    assert settle(0.0, SEEK_TIMEOUT_SEC + 0.1, st6, land_tol(30.0)) == "timeout"
    assert st6["degraded"] is True

    # 8) 适配器
    pol = CueSeekPolicy({"seek_profile": "off"}, now=lambda: 0.0)
    assert pol.mode(_Item(codec="dxv")) == MODE_OFF
    psx = _PS(play_sec=1.0)
    stx = pol.state_for(psx)
    assert stx is pol.state_for(psx)
    pol.reset()
    assert pol.state_for(psx) is not stx

    print("[cue_seek_policy] 自检通过 ✓  档位=free/normal/conservative/off  "
          "阈值=%.2f/%.2f/%.2f  落地容差(30fps)=%.4fs"
          % (DRIFT_SEC, DRIFT_SEC, DRIFT_SEC_CONSERVATIVE, land_tol(30.0)))
    return True


if __name__ == "__main__":
    selftest()
