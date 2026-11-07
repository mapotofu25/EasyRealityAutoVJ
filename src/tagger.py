# -*- coding: utf-8 -*-
"""素材"硬指标"检测 —— 动态三档 / 频闪 / 铺满度，以及据此自动分配图层角色。

★★ 2026-10-30 用户「大砍刀」后，本模块**不再有任何视觉模型**：
  删掉了 Chinese-CLIP（`cn_clip_vision.onnx` + `tag_emb.npy` + `tags.txt`，331 MB）、
  内容标签（颜色/元素/场景/人物/风格）、亮度三档（暗基调/中亮度/亮基调）。
  留下的两个检测都是"拿帧直接算"：
    · `Tagger.analyze_dynamic()` —— 顺序解码全片，算相邻帧差中位数 → 三档动态；
      再取 40 点亮度序列算跳变占比 → 频闪。
    · `Tagger.coverage_score()`  —— 取一帧，算边缘带亮度/有内容占比 + 16 分块广度 → 铺满度。
  `auto_roles()` 只用「alpha / 铺满度 / 高动态」判前景还是背景（用户要求保留铺满度，
  它就是自动分配角色的唯一依据）。

⚠ 帧差阈值来自 `tags_def`（唯一来源），调阈值只改那里。
"""
import os

import numpy as np

from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,
                      DYNAMIC_FLICKER, DIFF_MID_THRESHOLD, DIFF_HIGH_THRESHOLD,
                      DIFF_HIGH_RATIO, COVERAGE_FULL_THRESHOLD,
                      COVERAGE_FULL_THRESHOLD_DYNAMIC)


def _cv_imread(path, flags=None):
    """cv2.imread 的**非 ASCII 安全**封装。

    ⚠ OpenCV 在 Windows 上读不了含中文等非 ASCII 字符的路径（实测素材库 5 张图片
      因 `<素材库>/xxx.png` 全部 cv2.imread 返回 None）。
      `imread` 内部走 C 层 narrow 字符 API，Python 层无法修复，须先经
      `media_manager._ascii_safe_path()` 复制到临时 ASCII 副本再读。

    ⚠ **仅图片分支使用**：`cv2.VideoCapture` 自己走 FFmpeg 宽字符通道，实测 157/157
      视频用原始路径全部可读；给视频也套临时副本只会白白多拷一份大文件。
    """
    import cv2
    try:
        from media_manager import _ascii_safe_path
        safe = _ascii_safe_path(path)
    except Exception:                                          # noqa: BLE001
        safe = path
    return cv2.imread(safe, flags) if flags is not None else cv2.imread(safe)


def auto_roles(kind="video", has_alpha=False, coverage=None, dynamic=False):
    """按「alpha / 铺满度 / 高动态」自动建议角色（单一角色：前景或背景二选一）。

    2026-10-30 砍掉内容标签后，判据只剩这三条（顺序即优先级）：
      ① `has_alpha`（带透明通道）⇒ **前景**：透明叠加元素天然是叠加层；
      ② 铺满度 ≥ `COVERAGE_FULL_THRESHOLD`（或高动态且 ≥ 宽松档）⇒ **背景**：
         全屏环境素材，铺满画面；
      ③ 其余（黑底/局部）⇒ **前景**。

    为什么高动态给宽松档：整屏流动的抽象背景（粒子海/光流）边缘常常很暗，
    严格档会把它们误判成前景，铺到画面上就是"一块黑的"。
    """
    roles = {"fg": False, "bg": False}
    cov = 0.5 if coverage is None else float(coverage)
    if has_alpha:
        roles["fg"] = True
        return roles
    if cov >= COVERAGE_FULL_THRESHOLD or (dynamic and cov >= COVERAGE_FULL_THRESHOLD_DYNAMIC):
        roles["bg"] = True
    else:
        roles["fg"] = True
    return roles


class Tagger:
    """素材硬指标检测（纯计算，无模型、无网络）。全部是静态方法，不需要实例。"""

    @staticmethod
    def coverage_score(bgr):
        """0-1 铺满度。旧版只用边缘亮度，暗色调全屏场景（星空/夜森林/暗科幻）边缘虽暗
        但有内容，被严重低估（土星环实测 0.07）→ 误判前景。
        v2 加「分块广度」：边缘带切 16 块，块均值>10 视为有内容——黑底前景素材只有
        局部块有内容（实测 0~4/16），全屏场景几乎每块都有（实测 8~16/16），完美分离。
        取 旧亮度公式 与 分块广度 的较大者。"""
        import cv2
        h, w = bgr.shape[:2]
        if h < 8 or w < 8:
            return 0.5
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        bh, bw = max(1, int(h * 0.15)), max(1, int(w * 0.15))
        bands = [gray[:bh, :], gray[-bh:, :], gray[:, :bw], gray[:, -bw:]]
        edges = np.concatenate([b.ravel() for b in bands])
        edge_bright = float(edges.mean() / 255.0)   # 边缘平均亮度 0-1
        edge_lit = float((edges > 45).mean())       # 边缘"有内容"像素占比 0-1
        cov_bright = float(0.5 * edge_bright + 0.5 * edge_lit)
        # 分块广度：上/下边带沿宽切4段、左/右边带沿高切4段，共16块
        filled = 0
        for band in bands:
            bh2, bw2 = band.shape
            if bw2 >= bh2:   # 横条（上/下）
                segs = [band[:, i * bw2 // 4:(i + 1) * bw2 // 4] for i in range(4)]
            else:            # 竖条（左/右）
                segs = [band[i * bh2 // 4:(i + 1) * bh2 // 4, :] for i in range(4)]
            filled += sum(1 for s in segs if float(s.mean()) > 10)
        spread = filled / 16.0
        return float(max(cov_bright, spread))

    @staticmethod
    def dynamic_score(diff, flicker=False):
        """帧差中位数 → **0~1 连续动态分**（在低/中/高三档之内的细分）。

        ★ 2026-10-30 用户：「能不能给素材也打分，在原来中低高动态的基础上，
          0-1 之间的数，显示在悬浮信息窗口上，roll 素材池时优先挑选贴近的数字」。

        为什么单独要一个分：三档只有 3 个值，同一档里"刚过线"和"快出档"差很多
        （实测低动态 6.2~8.1、中动态 10.5~17.1、高动态 20.8+），换池时只能靠随机，
        同一档内选谁全靠运气。有了连续分，就能按"离目标强度多近"来挑。

        映射（分段线性，锚点就是分档阈值，保证**分数与档位永远自洽**）：
          diff 0            → 0.00
          diff = 中档线(10)  → 0.33
          diff = 高档线(19)  → 0.67
          diff = 高档线×1.8(≈34) → 1.00（再高就封顶；实测库内最高 ~30）
        频闪素材取 `max(score, 0.75)`：它的帧差中位数可能不高（整帧亮暗交替的
        diff 反而不大），但观感是"最炸"的一类 —— 老逻辑也把频闪当高动态用，
        这里保持口径一致，否则换池会把频闪素材从高档池里挤掉。

        :param diff: `analyze_dynamic` 的第三个返回值（帧差中位数）；`<0` = 打不开
        :param flicker: 是否频闪
        :return: 0.0~1.0；`diff < 0`（读不出来）返回 `None`，调用方据此跳过打分
        """
        try:
            d = float(diff)
        except (TypeError, ValueError):
            return None
        if d < 0:
            return None
        lo, hi = float(DIFF_MID_THRESHOLD), float(DIFF_HIGH_THRESHOLD)
        top = hi * 1.8
        if d <= 0.0:
            v = 0.0
        elif d < lo:
            v = 0.333 * (d / max(1e-6, lo))
        elif d < hi:
            v = 0.333 + 0.334 * ((d - lo) / max(1e-6, hi - lo))
        else:
            v = 0.667 + 0.333 * min(1.0, (d - hi) / max(1e-6, top - hi))
        v = max(0.0, min(1.0, v))
        if flicker:
            v = max(v, 0.75)
        return round(v, 3)

    @staticmethod
    def analyze_dynamic(path, kind):
        """动态检测（不跑任何模型，轻量）：返回 `(motion, flicker, diff)`。

        motion ∈ {低动态, 中动态, 高动态}；图片天然低动态；打不开返回 `("", False, -1.0)`。
        供 `DynTagScanThread` 复用。

        算法 v2（顺序读全片，替代旧的 seek 分段采样）：
        - 旧实现用 cv2 的 CAP_PROP_POS_FRAMES seek 做 3 段×8 帧采样，但 VJ 素材几乎全是
          DXV(DXD3) 编码，cv2 对它的 seek 会失效——三段采样采到同一批帧（新视觉82期(35)
          实测三段 ndiffs 完全相同），把「一直在闪/呼吸闪」的素材误判成低动态。
        - 改为顺序读全片（不 seek），一次算出「全程相邻帧差」与「全程每帧亮度」，
          从真实顺序数据里统计，彻底规避 DXV 的 seek 失效。
        - motion：全程相邻帧差的中位数分档（<10 低 / 10~19 中 / ≥19 高），另加
          「高动态帧(>19)占比≥40%」辅助——中位数偏低但近半帧差剧烈的素材仍判高动态
          （如新视觉82期(36) med=17 但 49% 帧差>19）。中位数保证「缓慢移动/几秒才切一下
          画面」的偶发变化仍是低动态。
        - flicker：全程亮度均匀取 40 点，相邻点亮度跳变(>15)占比≥0.35（逐帧亮暗交替/
          快速明暗呼吸，如 001(12)、搬运工(55)）。缓变呼吸闪（如新视觉82期(35)）亮度
          相邻差<15 抓不到，但其帧差中位已达中动态档，靠 motion 规避；不再设低频周期
          检测，避免把「亮度有周期的缓慢循环素材」（如 17.mov、001(2)）误判成频闪。

        ⚠ 2026-10-30 砍掉亮度分档后，这里**不再返回 med**（全片灰度中位数）——
          `brights` 数组只为频闪检测保留。调用方是 3 元组解包。
        """
        import cv2
        if kind == "image":
            # ★★ 2026-10-31（用户：「图片不要打标」）：静态图片没有"动态"可言，
            #   给它打「低动态 / 铺满」既是噪音（用户截图里那两张黑图就顶着
            #   「低动态·局部」），又会让它混进低动态换池。
            #   ⇒ 返回**空 motion** —— `merge_scan_tags` 见到空 motion 会原样返回
            #     现有标签（即一个也不加）；`dynamic_score(-1.0)` 返回 None（不打分）。
            #   ⚠ 调用方 `DynTagScanThread` 仍会单独算 coverage —— 那个只用于
            #     `auto_roles` 判断"前景/背景"，不进标签。
            return "", False, -1.0
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return "", False, -1.0
        # 顺序读全片（不 seek），一次采集：全程相邻帧差 + 全程亮度
        diffs = []    # 全程相邻帧差（灰度 abs diff 均值）
        brights = []  # 全程每帧亮度（只给频闪检测用）
        prev = None
        read = 0
        MAX_FRAMES = 1500   # 顺序读上限（约 50s@30fps）；VJ 循环素材远短于此
        while read < MAX_FRAMES:
            ok, f = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
            brights.append(float(g.mean()))
            if prev is not None:
                diffs.append(float(np.abs(g - prev).mean()))
            prev = g
            read += 1
        cap.release()
        if len(diffs) < 2:
            return "", False, -1.0

        d = np.asarray(diffs, dtype=np.float64)
        diff = float(np.median(d))
        gt19 = float((d > DIFF_HIGH_THRESHOLD).mean())
        if diff >= DIFF_HIGH_THRESHOLD or (diff >= DIFF_MID_THRESHOLD and gt19 >= DIFF_HIGH_RATIO):
            motion = DYNAMIC_HIGH
        elif diff >= DIFF_MID_THRESHOLD:
            motion = DYNAMIC_MID
        else:
            motion = DYNAMIC_LOW

        flicker = False
        if len(brights) >= 20:
            idx = np.linspace(0, len(brights) - 1, 40).astype(int)
            bs = np.asarray([brights[i] for i in idx], dtype=np.float64)
            bd = np.abs(np.diff(bs))
            # 频闪：明显亮度跳变(>15)占比 ≥0.35。真静止素材 1~12 次(占比<0.3)、
            # 明暗呼吸/逐帧闪 19~32 次(占比 0.5+)——0.35 卡在中间。
            flicker = float((bd > 15).mean()) >= 0.35
        # ★★ 2026-10-30（用户「怎么会有低动态又频闪的素材呢」）：
        #   帧差中位数只量"画面内容变化了多少"，**量不到整帧亮暗交替** ——
        #   白闪/黑闪交替的素材，内容其实没动，中位数会很低（实测 001 (12).mov
        #   就是「低动态 + 频闪」，tooltip 里还写着动态分 0.85，自相矛盾）。
        #   而换池那边（`dynamic_score`）早就把频闪当"最炸的一类"（`max(v, 0.75)`），
        #   老逻辑也是按高动态用的 ⇒ **档位必须跟上**，否则标签和分数对不上。
        if flicker:
            motion = DYNAMIC_HIGH
        return motion, flicker, diff
