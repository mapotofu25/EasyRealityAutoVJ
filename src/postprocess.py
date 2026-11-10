# -*- coding: utf-8 -*-
"""后处理特效链：对合成后的画布做 CPU 特效（纯 numpy + OpenCV）

挂在所有图层合成之后、输出之前。特效强度统一 0~1（0=无效果，直接跳过不耗性能）。
通道约定：BGR 三通道（与 OpenCV 一致），画布是 QImage RGB32，alpha 恒 0xFF 不参与。

第一批（常驻 + 手动强度）：
- chromatic 色差：B/R 通道反向水平错位，产生边缘色散
- bloom     辉光：下采样高斯模糊再加法叠加，柔光
- glitch    故障位移：随机水平条带错位（带时间种子，有动感）
- exposure  曝光脉冲：整体亮度增益
"""
import math
import numpy as np
import time as _time_mod

#: 慢帧取证：颜色/后处理分段累加（ms）。只在慢帧日志里读取并清零。
SEG = {"view": 0.0, "write": 0.0, "bits_copy": 0}

import cv2


# ---- 特效函数：输入 BGR (H,W,3) uint8，强度 0~1，返回新数组 ----

def fx_chromatic(bgr, strength):
    """色差：R 通道右移、B 通道左移（G 不动），强度越大偏移越大

    ⚠ 旧写法 `shift = int(strength * 14)`：`strength < 0.0714`（滑条 1~7）时 shift=0
      ⇒ **整段区间完全无效果**，用户拖到 5、10 会以为"色差坏了"。
      现在只要 strength>0 就至少错 1px（1px 已是可见的紫边）。
    """
    if strength <= 0:
        return bgr
    shift = max(1, int(round(strength * 14)))
    h, w = bgr.shape[:2]
    if shift >= w:
        shift = w - 1
    out = bgr.copy()
    # bgr 通道序 [B, G, R]；R(2) 右移、B(0) 左移
    out[:, shift:, 2] = bgr[:, :w - shift, 2]
    out[:, :w - shift, 0] = bgr[:, shift:, 0]
    return out


def fx_bloom(bgr, strength):
    """辉光：下采样 + 高斯模糊 + 上采样 + 加法叠加"""
    if strength <= 0:
        return bgr
    h, w = bgr.shape[:2]
    sw, sh = max(1, w // 4), max(1, h // 4)
    small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_LINEAR)
    blur = cv2.GaussianBlur(small, (0, 0), 4)
    bloom = cv2.resize(blur, (w, h), interpolation=cv2.INTER_LINEAR)
    # 强度映射到叠加权重：0~1 → 0~1.4（辉光要明显才看得出）
    out = cv2.addWeighted(bgr, 1.0, bloom, strength * 1.4, 0)
    return out


def fx_glitch(bgr, strength, seed):
    """故障位移：随机水平条带错位。seed 变化产生逐帧抖动"""
    if strength <= 0:
        return bgr
    h, w = bgr.shape[:2]
    out = bgr.copy()
    # ★ 2026-10-28（性能）：同 fx_grain，统一用 default_rng（条带数量少，
    #   收益不如 grain 明显，但省掉一次 RandomState 构造）
    rng = np.random.default_rng(seed & 0x7fffffff)
    n_strips = max(1, int(strength * 7))
    max_shift = max(1, int(strength * w * 0.10))
    for _ in range(n_strips):
        y0 = int(rng.integers(0, max(1, h - 2)))
        y1 = int(min(h, y0 + int(rng.integers(4, max(5, h // 8)))))
        dx = int(rng.integers(-max_shift, max_shift + 1))
        if dx != 0 and y1 > y0:
            out[y0:y1] = np.roll(out[y0:y1], dx, axis=1)
    return out


def fx_exposure(bgr, strength):
    """曝光脉冲：整体亮度增益（0=无，1=提亮至约 2.5 倍）
    用 256 项 LUT 查表，避免 float32 转换（原实现 14ms，查表 0.7ms）"""
    if strength <= 0:
        return bgr
    factor = 1.0 + strength * 1.5
    lut = np.clip(np.arange(256, dtype=np.float32) * factor, 0, 255).astype(np.uint8)
    return cv2.LUT(bgr, lut)


def fx_saturation(bgr, strength):
    """饱和度脉冲：向灰度图与彩色图之间插值（f>1 增艳，f<1 褪色）。

    ★ 2026-11-08：旧写法 cvtColor + merge + addWeighted 是三次全画幅 pass；
    这里合成一个 3x3 颜色矩阵，用 cv2.transform 一次算完。逐像素最大差 1
    （定点舍入），1080p 实测 3.95ms → 2.07ms。
    """
    if strength <= 0:
        return bgr
    f = 1.0 + strength * 1.2                       # 1~2.2
    gb, gg, gr = 0.114, 0.587, 0.299               # OpenCV BGR→GRAY 权重
    a = 1.0 - f
    m = np.array([[f + a * gb, a * gg, a * gr],
                  [a * gb, f + a * gg, a * gr],
                  [a * gb, a * gg, f + a * gr]], dtype=np.float32)
    return cv2.transform(bgr, m)


def fx_softfocus(bgr, strength):
    """柔焦：高斯模糊叠加（梦幻感，比辉光更弥散）。

    ★ 2026-11-08：全分辨率 GaussianBlur 是后处理里最贵的一项（1080p s=0.6
    实测 23.2ms/帧）。柔焦本身就是低频效果，先按 sigma 降到 1/2~1/8 分辨率
    做高斯，再上采样；与旧实现逐像素最大差 4、均值 0.25（肉眼无差），
    1080p 实测 23.2ms → 6.4ms。
    """
    if strength <= 0:
        return bgr
    h, w = bgr.shape[:2]
    sigma = 1.0 + strength * 7.0
    f = 2 if sigma <= 3.0 else (4 if sigma <= 8.0 else 8)
    sw, sh = max(1, w // f), max(1, h // f)
    small = cv2.resize(bgr, (sw, sh), interpolation=cv2.INTER_AREA)
    blur = cv2.GaussianBlur(small, (0, 0), sigma / f)
    blur = cv2.resize(blur, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.addWeighted(bgr, 1.0, blur, strength * 0.7, 0)


_TRAIL = {"buf": None}


def fx_trails(bgr, strength, reset=False):
    """拖影：上一帧衰减后与本帧取 max（不叠白）。强度越大残影越久。

    ⚠ `bgr=None` 是**清空拖影缓冲**的约定（引擎在「效果关闭 / 被 Kv 接管」时
      调 `fx_trails(None, 0.0, reset=True)`）。旧实现直接 `bgr.copy()` ⇒
      `AttributeError` ⇒ 被上层 `except Exception: pass` 吞掉 ⇒ **缓冲永远清不掉**，
      下次再开拖影会闪一下上一场的残影（症状很隐蔽，日志里什么都没有）。
    """
    if bgr is None:
        _TRAIL["buf"] = None
        return None
    prev = _TRAIL["buf"]
    if reset or prev is None or prev.shape != bgr.shape:
        _TRAIL["buf"] = bgr.copy()
        return bgr
    decay = 1.0 - max(0.05, min(0.9, strength * 0.75))     # 每帧保留比例
    lut = np.clip(np.arange(256, dtype=np.float32) * decay, 0, 255).astype(np.uint8)
    scaled = cv2.LUT(prev, lut)
    out = cv2.max(bgr, scaled)
    _TRAIL["buf"] = out
    return out


def fx_grain(bgr, strength, seed):
    """颗粒：半分辨率随机噪声（cv2 定点混合），胶片颗粒感"""
    if strength <= 0:
        return bgr
    h, w = bgr.shape[:2]
    # ★ 2026-10-28（性能）：`RandomState.randint` 生成 360×640 要 **1.17 ms**，
    #   `default_rng().integers` 只要 **0.22 ms**（实测，快 5.2 倍）。
    #   两者都是 [0,256) 均匀整数 ⇒ 颗粒观感相同，只是随机序列不同。
    rng = np.random.default_rng(seed & 0x7fffffff)
    noise = rng.integers(0, 256, (max(2, h // 2), max(2, w // 2)), dtype=np.uint8)
    noise = cv2.resize(noise, (w, h), interpolation=cv2.INTER_NEAREST)
    noise3 = cv2.merge([noise, noise, noise])
    k = strength * 0.9
    return cv2.addWeighted(bgr, 1.0, noise3, k, -128.0 * k)


def fx_pixelate(bgr, strength):
    """像素化：马赛克块大小随强度"""
    if strength <= 0:
        return bgr
    h, w = bgr.shape[:2]
    block = max(2, int(2 + strength * 44))
    small = cv2.resize(bgr, (max(1, w // block), max(1, h // block)),
                       interpolation=cv2.INTER_LINEAR)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def fx_spectrum(bgr, strength, spectrum):
    """径向频谱：24 段频谱以圆环放射线形式叠加在画面中心（音乐可视化）"""
    if strength <= 0 or not spectrum or max(spectrum) <= 0.001:
        # 频谱全 0 = 没在采集（audio_engine 切源时会置 [0.0]*24）：
        # 画出来只剩一个 1px 圆环，等于"开了没效果"，直接跳过更诚实也更省。
        return bgr
    h, w = bgr.shape[:2]
    cx, cy = w // 2, h // 2
    n = len(spectrum)
    inner = int(min(h, w) * 0.10)
    max_len = int(min(h, w) * 0.40)
    ov = np.zeros_like(bgr)
    for i in range(n):
        v = min(1.0, float(spectrum[i]))
        ang = 2.0 * math.pi * i / n - math.pi / 2
        r1 = inner + int(max_len * (0.25 + 0.75 * strength) * v)
        x0 = int(cx + inner * math.cos(ang));  y0 = int(cy + inner * math.sin(ang))
        x1 = int(cx + r1 * math.cos(ang));     y1 = int(cy + r1 * math.sin(ang))
        # 高频段偏青、低频段偏橙红，亮度随强度
        t = i / max(1, n - 1)
        color = (int(60 + 195 * t), int(140 + 60 * (1 - abs(t - 0.5) * 2)), int(255 - 195 * t))
        cv2.line(ov, (x0, y0), (x1, y1), color, 2, cv2.LINE_AA)
    cv2.circle(ov, (cx, cy), inner, (120, 90, 40), 1, cv2.LINE_AA)
    return cv2.addWeighted(bgr, 1.0, ov, min(1.0, 0.4 + strength), 0)


def fx_deform(bgr, strength, spectrum, seed):
    """音频反应变形：画面按频谱分段水平错位（低频段大位移，正弦相位随帧流动）"""
    if strength <= 0 or not spectrum or max(spectrum) <= 0.001:
        return bgr
    h, w = bgr.shape[:2]
    n = len(spectrum)
    band = max(2, h // n)
    out = bgr.copy()
    for i in range(n):
        v = min(1.0, float(spectrum[i]))
        y0, y1 = i * band, min(h, (i + 1) * band)
        if y1 <= y0:
            continue
        dx = int(v * strength * w * 0.09 * math.sin(seed * 0.11 + i * 0.9))
        if dx:
            out[y0:y1] = np.roll(bgr[y0:y1], dx, axis=1)
    return out


_FX = {
    "chromatic": fx_chromatic,
    "bloom": fx_bloom,
    "glitch": fx_glitch,
    "exposure": fx_exposure,
    "saturation": fx_saturation,
    "softfocus": fx_softfocus,
    "trails": fx_trails,
    "grain": fx_grain,
    "pixelate": fx_pixelate,
    "spectrum": fx_spectrum,
    "deform": fx_deform,
}


def _np_view_bgr(img):
    """QImage(RGB32/ARGB32) → 连续 BGR (H,W,3) 副本

    ★ 2026-10-19 性能修复：旧写法 `ascontiguousarray(arr[:, :w*4].reshape(h,w,4)[:, :, :3])`
      是**3 通道跨步拷贝**，实测 720p 4.46ms / 1080p 8.86ms（只有 0.62 GB/s）。
      `cv2.cvtColor` 走 SIMD：720p 0.74ms / 1080p 1.14ms（快 6~8 倍），
      且输出**逐像素完全一致**（见 tools/_perf_bgr_view.py 的 array_equal 断言）。
    """
    _t = _time_mod.perf_counter()
    _t = _time_mod.perf_counter()
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    _det_before = False
    try:
        _det_before = bool(img.isDetached())
    except Exception:                                          # noqa: BLE001
        pass
    arr = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    try:
        # bits() 返回非 const ⇒ 若原本被共享引用，Qt 会先整幅 detach（10.8MB 拷贝）
        if _det_before and not bool(img.isDetached()):
            SEG["bits_copy"] = 1
    except Exception:                                          # noqa: BLE001
        pass
    v4 = arr[:, :w * 4].reshape(h, w, 4)
    _out = cv2.cvtColor(v4, cv2.COLOR_BGRA2BGR)
    try:
        SEG["view"] += (_time_mod.perf_counter() - _t) * 1000.0
    except Exception:                                          # noqa: BLE001
        pass
    return _out


def write_back_bgr(img, bgr):
    """把 (h,w,3) BGR 写回 QImage（**保留原 alpha**）。

    ★ 2026-10-19 性能修复：旧写法 `dst[:, :, :3] = bgr` 是跨步赋值，
      实测 720p 3.28ms / 1080p 7.54ms。改成 `cv2.cvtColor(..., dst=v4)`
      （720p 0.84ms / 1080p 2.86ms，快 2.6~3.9 倍），再回填原来的 alpha
      —— alpha 必须保留：码表图层的透明区就靠它，写成 255 会把透明变黑。
      逐像素等价性由 tools/_perf_bgr_view.py 断言。
    """
    _t = _time_mod.perf_counter()
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    dst = np.frombuffer(img.bits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    v4 = dst[:, :w * 4].reshape(h, w, 4)
    alpha = v4[:, :, 3].copy()          # 1 通道，1080p ≈ 2MB，约 0.2ms
    cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA, dst=v4)
    v4[:, :, 3] = alpha
    try:
        SEG["write"] += (_time_mod.perf_counter() - _t) * 1000.0
    except Exception:                                          # noqa: BLE001
        pass


def _level01(e):
    """读单个效果的 level 并归一化到 0~1。

    ⚠ 尺度兜底：UI 滑条写的是 0~100，但老配置/手改可能是 0~1 小数。
      `float(e.get("level", 0)) / 100` 遇上 0.4 会得到 0.004 ⇒ 该特效静默失效
      （"配置里明明开着、画面就是没有"）。0.0001~1 之间按比例解释。
    """
    try:
        v = float(e.get("level", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 < v < 1.0:
        return max(0.0, min(1.0, v))
    return max(0.0, min(1.0, v / 100.0))


def postfx_effects_bgr(canvas, bgr, postfx_cfg, seed=0, mods=None, spectrum=None,
                       reset_trails=False, effects_override=None):
    """在**已有的 BGR 缓冲**上跑后处理特效链。返回 `(bgr, changed)`。

    ★ 2026-10-28（性能，用户实测 5800X 30%）：从 `apply_postfx` 拆出来，目的是让
      「颜色渲染」与「后处理」**共用同一次** `_np_view_bgr()` / `write_back_bgr()`。
      原来两段各做一次全画布读回+写回（1280×720 共 3.7 MB × 2），
      实测每帧白搬 1.43 ms ≈ **4.3% 单核**。

    bgr: 可为 `None` —— 此时本函数**按需**读回画布（没有生效效果就不读，零成本）。
         传数组进来则直接在其上操作，不再读回。
    changed: True = bgr 内容已被改动，调用方**必须**负责写回画布。
    """
    if reset_trails:
        fx_trails(None, 0.0, reset=True)
    if not postfx_cfg or not postfx_cfg.get("enabled", True):
        return bgr, False
    effects = effects_override if effects_override is not None else (postfx_cfg.get("effects") or {})
    mods = mods or {}
    active = [(n, e) for n, e in effects.items()
              if e.get("on") and _level01(e) > 0 and n in _FX]
    if not active:
        return bgr, False
    if bgr is None:
        bgr = _np_view_bgr(canvas)
    for name, e in active:
        strength = _level01(e) * max(0.0, min(1.0, mods.get(name, 1.0)))
        if strength <= 0:
            continue
        fn = _FX[name]
        try:
            if name in ("glitch", "grain"):
                bgr = fn(bgr, strength, seed)
            elif name == "spectrum":
                bgr = fn(bgr, strength, spectrum or [])
            elif name == "deform":
                bgr = fn(bgr, strength, spectrum or [], seed)
            else:
                bgr = fn(bgr, strength)
        except Exception:
            continue
    return bgr, True


def apply_postfx(canvas, postfx_cfg, seed=0, mods=None, spectrum=None, reset_trails=False,
                 effects_override=None):
    """对合成画布应用启用的后处理特效（原地写回 canvas）。

    postfx_cfg: {"enabled": bool, "effects": {name: {"on": bool, "level": 0~100, ...}}}
    mods: {name: 0~1 调制系数}，缺省 = 1.0（常驻）。实际强度 = level/100 * mod。
    spectrum: 24 段频谱 0..1（径向频谱/音频变形用），可 None。
    reset_trails: 歌切/停播时清空拖影缓存，避免残影跨场景。
    effects_override: 自动模式传入的 {name: {"on": True, "level": ..}}，直接替换 effects 列表
        （自动挑的效果不能依赖 config 里 on 开关，否则默认全 off 导致自动模式零效果）。

    ⚠ 本函数是**单段调用**的兼容入口（自己读回、自己写回）。
      引擎热路径走 `postfx_effects_bgr()` 与颜色合并，整帧只搬一次。
    """
    bgr, changed = postfx_effects_bgr(
        canvas, None, postfx_cfg, seed=seed, mods=mods, spectrum=spectrum,
        reset_trails=reset_trails, effects_override=effects_override)
    if changed and bgr is not None:
        write_back_bgr(canvas, bgr)   # 写回画布（跳过 alpha 通道）
