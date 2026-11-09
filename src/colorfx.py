# -*- coding: utf-8 -*-
"""颜色渲染器（Color Renderer）

用途：一键调色，让同一批素材在不同颜色下看起来不一样，降低对素材数量的依赖、
缓解长时间演出的视觉疲劳。

设计要点（都为现场稳定性服务）：
- **不用乐句/Drop 识别**。颜色只由「实时能量」+「时间自走」+「能量突变」驱动。
- **性能**：颜色在换色时才「编译」成 256 项 3 通道 LUT，每帧只做一次 cv2.LUT 查表
  （实测 0.87ms @720p、1.74ms @1080p；HSV 色相旋转要 10~20ms，直接排除）。
- **Kv 图层绝对不染色**（项目铁律）：Kv 待机层激活时强度直接归 0，连平滑都不走。
- 平滑：强度用「升快降慢」的指数平滑；颜色切换在**参数空间插值**后重建 LUT，
  所以换色是丝滑渐变，不存在硬切。
"""
import math
import time

import numpy as np

from i18n import T, Tf

try:
    import cv2
except Exception:                     # pragma: no cover
    cv2 = None

# 色卡池：名字 / RGB / 归类（冷色用于中低能量，暖色用于高能量）
PALETTES = [
    ("原色",   (255, 255, 255), "none"),
    ("赛博紫", (168, 85, 247),  "warm"),
    ("故障红", (239, 68, 68),   "warm"),
    ("琥珀金", (245, 158, 11),  "warm"),
    ("电光粉", (236, 72, 153),  "warm"),
    ("深海蓝", (37, 99, 235),   "cool"),
    ("霓虹青", (34, 211, 238),  "cool"),
    ("霓虹绿", (74, 222, 128),  "cool"),
]
PALETTE_BY_NAME = {p[0]: p for p in PALETTES}
COOL_POOL = [p[0] for p in PALETTES if p[2] == "cool"]
WARM_POOL = [p[0] for p in PALETTES if p[2] == "warm"]

# 颜色渲染的能量分档：**低于 TIER_LOW 完全不渲染**（用户 2026-09-22 要求 0.4 以下不渲染），
# 中段 0.40~0.65 辅助染色 20~40%，高段 >0.65 强制染色 80~100%。
# 注意与「切换节奏」的能量三档（0.50/0.65）不是同一套阈值：颜色这套按用户要求单独定。
TIER_LOW, TIER_HIGH = 0.40, 0.65


def _clip255(v):
    return int(max(0, min(255, round(v))))


def build_lut(color_rgb, strength, mode="lut"):
    """把「颜色 + 强度」编译成查表数据。

    mode="lut"      → (lut3, None)：每通道增益/提升曲线，保留素材本身的色彩与细节，
                       只是整体往目标颜色「调色」（像相机滤镜 / 调色 LUT）。
    mode="duotone"  → (lut_gray, lut3)：LUT3 直接作用于灰度图，把亮度重映射成
                       「暗部→深色、亮部→亮色」的双色渐变（最像换了一个素材）。
    返回 (lut3, lut_gray)；lut3 形状 (1, 256, 3)，可直接 cv2.LUT 到 BGR 图。
    """
    s = max(0.0, min(1.0, float(strength)))
    r, g, b = [max(0.0, min(1.0, c / 255.0)) for c in color_rgb]
    mean = (r + g + b) / 3.0
    # 通道平衡：目标颜色的偏向（灰/白时全为 0 → 不改变画面）。
    # 注意顺序必须按 **BGR**（画布是 BGR，cv2.LUT 逐通道按图序查表）
    bal = [b - mean, g - mean, r - mean]
    xs = np.arange(256, dtype=np.float32)

    if mode == "duotone":
        # 灰度 LUT：轻微提对比
        lut_g = np.clip(xs * 0.92 + 8.0, 0, 255).astype(np.uint8)
        # 双色渐变：暗部 = 目标色的深色版，亮部 = 目标色提亮混白（拉开明暗，别糊成一片）
        lo = [c * 0.12 * 255.0 for c in (b, g, r)]            # BGR
        hi = [min(255.0, (c * 0.95 + 0.50) * 255.0) for c in (b, g, r)]
        t = (xs / 255.0)[:, None] ** 1.15
        ramp = np.array(lo)[None, :] * (1 - t) + np.array(hi)[None, :] * t
        gray = np.repeat(xs[:, None], 3, axis=1)              # 恒等（灰度图三通道相同）
        lut3 = (gray * (1 - s) + ramp * s)
        return np.clip(lut3, 0, 255).astype(np.uint8).reshape(1, 256, 3), lut_g

    # 调色模式：每通道 gain/lift，再做「中间灰不动」的亮度补偿（防止整体变亮/变暗）
    gains, lifts = [], []
    for c in bal:
        gains.append(1.0 + 1.15 * s * c)
        lifts.append(38.0 * s * c)
    mid = [128.0 * gains[i] + lifts[i] for i in range(3)]
    corr = 128.0 - sum(mid) / 3.0
    lut = []
    for i in range(3):
        lut.append(np.clip(xs * gains[i] + lifts[i] + corr, 0, 255))
    lut3 = np.stack(lut, axis=1).astype(np.uint8).reshape(1, 256, 3)
    return lut3, None


def lerp_rgb(c1, c2, t):
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


class ColorRenderer:
    """颜色渲染器状态机：决定「当前该不该染色、染多浓、染什么色」。

    引擎每帧调用 tick() 拿强度，再 apply() 把 LUT 打到画布上。
    """

    def __init__(self):
        self.clear()

    def clear(self):
        self.s = 0.0                  # 当前平滑后的染色强度 0..1
        self.color = (255.0, 255.0, 255.0)   # 当前颜色（RGB 浮点，换色时插值用）
        self.name = "原色"
        self.pending = None           # 目标颜色（渐变中）
        self.pending_name = ""
        self.palette_i = -1           # 自动模式下池内序号（避免连续同色）
        self.last_pick = ""
        self.t_rotate = 0.0           # 上次换色时间
        self.t_burst = 0.0            # 上次突变刷新时间
        self.e_hist = []              # [(t, E)] 近 0.6s 能量轨迹（突变检测）
        self._lut = None              # 当前 LUT（1,256,3）
        self._lut_gray = None         # 双色调模式的灰度 LUT
        self._lut_key = None          # LUT 对应的 (颜色, 强度, 方式)
        self.last_state = ""
        self.info = {"s": 0.0, "name": "原色", "state": "", "remain": 0.0}

    # -------- 决策 --------
    def tick(self, cfg, snap, *,
             kv_on=0.0, flicker=False, bypass=False, rotate_hint=None,
             advance=True):
        """推进一帧决策，返回当前染色强度 0..1。

        `rotate_hint`：**要不要在这一帧换色**，由调用方判定。
          · `None`（默认）= 不做判定，按 `rotate_sec` 秒数走（旧行为，逐值不变）；
          · `True` / `False` = 用调用方的结论。引擎传的是「按八拍算出来的到期」，
            这样换色点能踩在波形图的八拍头上（用户 2026-10-28 要求）。
        """
        now = time.perf_counter()
        c = cfg or {}
        on = bool(c.get("on", True))
        mode = str(c.get("mode", "lut") or "lut")
        gain = max(0.0, min(1.0, float(c.get("strength", 60) or 0) / 100.0))
        E = max(0.0, min(1.0, float(snap.get("energy", 0.0) or 0.0)))

        # 近 0.6s 能量轨迹（突变检测）
        self.e_hist.append((now, E))
        while self.e_hist and now - self.e_hist[0][0] > 0.6:
            self.e_hist.pop(0)

        auto = bool(c.get("auto", True))
        manual = str(c.get("manual_color", "") or "")
        hold = float(c.get("manual_hold", 10.0) or 0.0)
        t_manual = float(c.get("manual_t", 0.0) or 0.0)
        # ★ 2026-10-23 修（用户报「点了色卡没反应」+「颜色一直是同一个」）：
        #   ① 旧条件写成 `... and auto`：关掉「自动模式」后点色卡**永远无效**，
        #      而界面 tooltip 明明写着"关掉后只有点色卡才染色" —— 两条自相矛盾，属真 bug。
        #   ② `manual_t` 存的是 **perf_counter**（进程内相对时钟）却被落盘，
        #      重启后新进程的 perf_counter 可能**更小** ⇒ `now - t_manual` 是负数
        #      ⇒ 永远 `< hold` ⇒ 一开机就被上一次的颜色锁死（症状："颜色一直不变"）。
        #   现在用墙钟 `time.time()` 判，并要求 0 <= 已过时间 < hold：
        #      旧配置里的 perf_counter 值代入后要么是巨大正数、要么负数，两种都判为"已过期"，
        #      天然完成迁移（不需要改配置版本号）。
        _elapsed = time.time() - t_manual
        manual_active = bool(manual) and (0.0 <= _elapsed < hold)

        # ---- 目标强度 ----
        # 顺序很关键：**手动点色卡优先级最高**（除了总开关/旁路/Kv 三个硬关闭），
        # 频闪休眠管不到手动锁定的颜色——用户点色卡就是要看到那个颜色。
        if not on or bypass or kv_on > 0.01:
            want = 0.0
            self.last_state = (T("已旁路") if bypass else
                               T("待机（Kv）不染色") if kv_on > 0.01 else T("已关闭"))
        elif manual_active:
            # 手动锁定优先：全强度染色，不管频闪；10 秒后自动回到自动模式
            want = max(0.35, gain)
            self.last_state = Tf("手动锁定「{}」（{}s 后回自动）", T(manual),
                                 f"{max(0.0, hold - _elapsed):.0f}")
        elif flicker and auto and str(c.get("flicker_mode", "on")) != "off":
            # 频闪休眠只在自动模式下生效；手动锁定在上一条已经返回，不受影响。
            # flicker_mode=off（用户选了「不休眠」）时这里也不拦——双保险，
            # 免得将来某一侧判断漏了又变成"勾了不休眠还是休眠"。
            want = 0.0
            self.last_state = T("频闪素材，颜色休眠")
        elif not auto:
            want = 0.0
            self.last_state = T("手动模式：请点色卡选色")
        elif E < TIER_LOW:
            want = 0.0
            self.last_state = T("能量过低，颜色渲染已休眠")
        elif E < TIER_HIGH:
            # 2026-10-23：中段从 0.20~0.40 抬到 0.35~0.60。
            # 理由：绝大多数舞曲常态落在 0.45~0.60，旧公式乘上「染色强度 60%」后只剩
            # 0.12~0.18，实测平均像素差仅 7/255（tools/_verify_color_postfx.py），
            # 用户看到的就是"开了也没变"。下限 0.40 以下的休眠保持不变（用户 09-22 明确要求）。
            want = (0.35 + 0.25 * (E - TIER_LOW) / (TIER_HIGH - TIER_LOW)) * gain
            self.last_state = T("辅助染色（中低能量）")
        else:
            want = (0.80 + 0.20 * min(1.0, (E - TIER_HIGH) / 0.35)) * gain
            self.last_state = T("强制染色（高能量）")

        # ★ 2026-11-10（多屏颜色漂移）：`advance=False` 时**只读不写** ——
        #   返回"当前已推进到的那一档强度"，绝不改 `e_hist` / `palette_i` / `self.s` /
        #   `pending` 等内部状态。多屏下主屏（advance=True）推进一次，其余屏复用同一份，
        #   否则同一帧里第 2、3 块屏拿到的颜色和第一块不同，且换色速度按屏数翻倍。
        if not advance:
            # ★★ 2026-11-10（③-3 根因）：这里必须返回**按本屏参数算出来的 `want`**，
            #   而不是全局的 `self.s` —— `self.s` 是主屏那一遍的平滑强度，
            #   主屏 on=False 时它=0 ⇒ 独立屏也被判成 0、整块不调色（QA 量到的 b 组回原色）。
            #   返回 `want` 后：本屏的 on/strength/频闪/手动锁定都按自己那份 cfg 生效，
            #   且仍然**不推进任何内部状态**（换色速度不会随屏数翻倍）。
            #   ⚠ 已知代价：主屏用平滑后的 self.s、独立屏用 want，过渡的头 ~0.2s 内会有几帧差异，
            #     这是刻意的取舍（相位仍共享，绝大多数帧一致）。
            if (not on) or bypass or kv_on > 0.01:
                return 0.0
            return float(want)

        # ---- 目标颜色 ----
        rotate = max(1.0, float(c.get("rotate_sec", 45) or 45))   # 1~60 秒（UI 滑块给的范围）
        if manual_active:
            self._set_color(manual, now)
        elif on and not bypass and want > 0.01:
            pool = WARM_POOL if E >= TIER_HIGH else COOL_POOL
            burst = False
            if bool(c.get("burst", True)):
                lo = min(e for _, e in self.e_hist) if self.e_hist else E
                if E - lo >= 0.18 and E >= TIER_HIGH and now - self.t_burst > 6.0:
                    burst = True
                    self.t_burst = now
            # 换色判定：有 `rotate_hint` 就用调用方的（引擎按八拍算），否则旧行为
            _due = ((now - self.t_rotate) >= rotate) if rotate_hint is None \
                else bool(rotate_hint)
            if self.palette_i < 0 or burst or _due:
                self._pick_from_pool(pool, now)
                if burst:
                    self.last_state += "·突变刷新"

        # ---- 平滑：升快降慢（颜色渐变在参数空间完成，不会硬切）----
        # 三个「立刻归零」的场景：总开关关 / 旁路 / **Kv 待机层激活**（项目铁律：绝不染 Kv，
        # 硬归零而不是慢慢淡出，保证待机画面干净、品牌 Logo 不变色）。
        if (not on) or bypass or kv_on > 0.01:
            self.s = 0.0
        else:
            # 升 0.24 / 降 0.07：升快降慢。0.24 在 60fps 下约 0.15 秒到位
            # （旧值 0.14 要 0.4 秒，加上中段强度本来就低，用户会觉得"没反应"）。
            k = 0.24 if want > self.s else 0.07
            self.s += (want - self.s) * k
            if self.s < 0.004:
                self.s = 0.0
        # 颜色目标插值：步长定成「约 1 秒走完」，轮换时间最短 1 秒时也能变到位
        # （原来 0.06/帧 ≈1.5 秒；轮换设成 1 秒的话颜色会永远停在中途）
        if self.pending is not None:
            self.color = lerp_rgb(self.color, self.pending, 0.09)
            if max(abs(self.color[i] - self.pending[i]) for i in range(3)) < 1.0:
                self.color = self.pending
                self.pending = None
                self.name = self.pending_name
        self.info = {
            "s": self.s, "E": E, "state": self.last_state,
            "name": self.name if self.s > 0.004 else "",
            "remain": max(0.0, rotate - (now - self.t_rotate)) if self.s > 0.004 else 0.0,
        }
        return self.s

    def _pick_from_pool(self, pool, now):
        """从色池里抽一个：随机且不连续抽到同一个颜色"""
        if not pool:
            return
        import random
        n = len(pool)
        cand = [x for x in pool if x != self.last_pick] or list(pool)
        name = cand[random.randrange(len(cand))] if len(cand) > 1 else cand[0]
        self.palette_i = pool.index(name)
        self.last_pick = name
        self._set_color(name, now)

    def _set_color(self, name, now):
        """切到某个色卡：颜色在参数空间平滑插值过去（1~2s），不是硬切"""
        if name == self.pending_name and (self.pending is not None or name == self.name):
            return                      # 已经在目标色（或正朝它渐变），不重复触发
        rgb = tuple(float(v) for v in PALETTE_BY_NAME.get(name, PALETTES[0])[1])
        self.pending_name = name
        self.pending = rgb
        self.t_rotate = now
        if self._lut_key is None or self.name == "原色":
            # 第一次选色（或从原色切过来）：直接到位，避免从白色插值一路发灰
            self.name = name
            self.pending = None
            self.color = rgb

    # -------- 应用 --------
    def apply_to_bgr(self, bgr, mode="lut"):
        """在**已有 BGR 缓冲**上染色，返回新数组；`None` = 本帧不染色。

        ★ 2026-10-28（性能，用户实测 5800X 30%）：从 `apply` 拆出来，让
          「颜色」与「后处理」共用同一次 `_np_view_bgr()` / `write_back_bgr()`。
          原来两段各做一次全画布读回+写回（1280×720 共 3.7 MB），
          实测每帧白搬 1.43 ms ≈ **4.3% 单核**。
        """
        if cv2 is None or self.s <= 0.004:
            return None
        mode = "duotone" if mode == "duotone" else "lut"
        key = (round(self.color[0]), round(self.color[1]), round(self.color[2]),
               round(self.s * 100), mode)
        if self._lut_key != key:
            # 只在「颜色/强度/方式」变化时才重建 LUT（每帧重建也能跑，但没必要）
            self._lut, self._lut_gray = build_lut(self.color, self.s, mode)
            self._lut_key = key
        if mode == "duotone" and self._lut_gray is not None:
            g = cv2.LUT(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), self._lut_gray)
            return cv2.LUT(cv2.cvtColor(g, cv2.COLOR_GRAY2BGR), self._lut)
        return cv2.LUT(bgr, self._lut)

    def apply(self, canvas, mode="lut"):
        """把当前颜色按当前强度打到画布（原地）。返回是否真的动了画面。

        ⚠ 兼容入口：自己读回、自己写回。引擎热路径走 `apply_to_bgr()` 与后处理合并，
          整帧只搬一次；这里保留是因为单测/工具会直接调它。
        """
        if cv2 is None or self.s <= 0.004:
            return False
        from postprocess import _np_view_bgr, write_back_bgr
        bgr = _np_view_bgr(canvas)
        out = self.apply_to_bgr(bgr, mode)
        if out is None:
            return False
        write_back_bgr(canvas, out)
        return True
