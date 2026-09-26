# -*- coding: utf-8 -*-
"""Chinese-CLIP 本地视觉打标模块（离线，无网络上传）。

运行时只用「视觉编码器 ONNX + 预计算标签向量表」，
文本编码器只在 tools/precompute_tag_emb.py 里离线用过一次。
"""
import os
import sys
import threading

import numpy as np

from tags_def import (DYNAMIC_LOW, DYNAMIC_MID, DYNAMIC_HIGH,
                      DYNAMIC_FLICKER, DYNAMIC_TAGS,
                      DIFF_MID_THRESHOLD, DIFF_HIGH_THRESHOLD,
                      DIFF_HIGH_RATIO)


# 内容 tag → 图层角色（两角色制，v2）
# 前景=可叠加元素/主体（粒子、光效、线条、人物…）；背景=铺满环境（海洋、城市、星空…）
FG_TAGS = {"粒子", "光斑", "光效", "光线", "激光", "霓虹", "烟雾", "火焰", "闪电",
           "星轨", "几何", "线条", "网格", "波纹", "气泡", "碎片", "辉光", "液态",
           "水墨", "渐变", "马赛克", "剪影", "眼睛", "人物", "女性", "男性", "舞者",
           "动物", "鸟", "抽象"}
BG_TAGS = {"海洋", "沙滩", "星空", "夜空", "天空", "云", "日出", "日落", "森林",
           "山", "沙漠", "雪", "雨", "火", "水", "花", "草地", "宇宙", "极光",
           "月亮", "太阳", "城市", "夜景", "舞台", "废墟", "室内", "工业", "阳光"}
# 中性词：既可铺满当背景、也可局部当前景，需按「铺满度+动态」二次分流
NEUTRAL_TAGS = {"科技", "机械", "未来", "赛博朋克", "暗黑"}
# 明确主体词：画面主体是局部实体（人物/动物等）→ 无论铺满度一律前景。
# 与其他 FG 词（烟雾/水墨/粒子等"氛围词"）区分——氛围词既可全屏也可局部，交给铺满度裁决。
SUBJECT_TAGS = {"人物", "女性", "男性", "舞者", "剪影", "眼睛", "动物", "鸟"}


def auto_roles(tags, kind="video", has_alpha=False, coverage=None, dynamic=False, scored=None):
    """按内容 tag 自动建议角色（单一角色：前景或背景二选一，不产生双角色）。
    scored: [(tag, score)] 带相似度的内容标签（推荐传入）——按最强命中定单一主角色，
    分差极小/存疑时用铺满度裁决；否则任何前景词+背景词并存会把所有素材都标成双角色。
    coverage: 0-1 铺满度（边缘带内容占比，越高越铺满）；dynamic: 是否高动态。
    v3 裁决原则（治「全屏氛围场景被误判前景」）：
    ① alpha → 前景（透明叠加元素）；② 明确主体词（人物/舞者等）→ 前景；
    ③ 其余以铺满度为主裁决——烟雾/水墨/霓虹这类"氛围词"既可全屏也可局部，
    CLIP 分数分不出铺满与否，铺满(coverage≥0.45)一律背景。"""
    ts = set(tags or [])
    roles = {"fg": False, "bg": False}
    cov = 0.5 if coverage is None else coverage

    # 0) alpha → 透明叠加元素，必前景
    if has_alpha:
        roles["fg"] = True
        return roles

    # 0.5) 明确主体词：黑底局部 → 前景（人物特写/抠像素材）；
    #      画面铺满时不特判（人物剪影看星系这类"人在环境中"是背景），交给下面的铺满度裁决
    if (ts & SUBJECT_TAGS) and cov < 0.45:
        roles["fg"] = True
        return roles

    # 1) 有带分数的标签 → 按最强命中 + 铺满度定角色
    if scored:
        cat_best = {}
        for z, s in scored:
            if z in FG_TAGS:
                c = "fg"
            elif z in BG_TAGS:
                c = "bg"
            elif z in NEUTRAL_TAGS:
                c = "neutral"
            else:
                continue
            if c not in cat_best or s > cat_best[c]:
                cat_best[c] = s
        if "fg" in cat_best or "bg" in cat_best:
            cands = [(c, s) for c, s in cat_best.items() if c in ("fg", "bg")]
            if len(cands) == 2:
                fb, bs = cands
                if abs(fb[1] - bs[1]) <= 0.008:
                    roles["bg" if cov >= 0.45 else "fg"] = True   # 分差极小 → 铺满度裁决
                elif cov >= 0.45:
                    roles["bg"] = True   # 铺满 → 背景（氛围词分数差不再一票定前景）
                else:
                    roles[max(cands, key=lambda x: x[1])[0]] = True
            else:
                c = cands[0][0]
                if c == "fg" and cov >= 0.45:
                    roles["bg"] = True   # 只有模糊前景词命中但画面铺满 → 背景（水墨/烟雾全屏）
                else:
                    roles[c] = True
            return roles
        if "neutral" in cat_best:
            # 只有中性词 → 按铺满度/动态分流
            if cov >= 0.45 or (dynamic and cov >= 0.35):
                roles["bg"] = True
            else:
                roles["fg"] = True
            return roles

    # 2) 无分数时的简化逻辑（对齐 scored 分支的裁决原则）
    hit_fg = bool(ts & FG_TAGS)
    hit_bg = bool(ts & BG_TAGS)
    if hit_fg or hit_bg:
        if hit_bg:
            roles["bg"] = True          # 有明确环境词 → 背景
        elif cov >= 0.45:
            roles["bg"] = True          # 只有氛围词但画面铺满 → 背景
        else:
            roles["fg"] = True          # 氛围词 + 黑底局部 → 前景

    # 3) 只有中性词命中（无明确词）→ 按铺满度 + 动态分流
    if not any(roles.values()) and (ts & NEUTRAL_TAGS):
        # 铺满 → 背景；局部 → 前景（高动态且铺满更偏向整屏流动环境）
        if cov >= 0.45 or (dynamic and cov >= 0.35):
            roles["bg"] = True
        else:
            roles["fg"] = True

    # 4) 什么都没命中 → 铺满度分流，再退回透明度兜底
    if not any(roles.values()):
        if cov >= 0.45:
            roles["bg"] = True
        else:
            roles["fg"] = True
    return roles


def _model_dir():
    # exe 模式：PyInstaller 打包后资源在 sys._MEIPASS（_internal 目录）
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "models", "chinese_clip")


_MODEL_DIR = _model_dir()

_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

# 色相(hue, 0-180) → 颜色词
_HUE_COLORS = [
    ((0, 10), "红色"), ((10, 22), "橙色"), ((22, 38), "黄色"),
    ((38, 70), "绿色"), ((70, 100), "青色"), ((100, 125), "蓝色"),
    ((125, 145), "紫色"), ((145, 158), "粉色"), ((158, 180), "红色"),
]


class Tagger:
    """Chinese-CLIP 视觉打标（单例，懒加载）"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        import onnxruntime as ort
        # ⚠ 限制线程数 + **关掉池线程自旋**（同 audio_genre.py 的理由，见那里注释）。
        # 这个 session 只在**导入/重扫素材**时用（不在演出路径上），所以给 4 线程 ——
        # 在"导入速度"与"不抢 CPU"之间折中（单次 embed 约 77ms@16线程 / 94ms@4线程）。
        # 想改回去：AUTO_VJ_ORT_SPIN=1（恢复自旋）、AUTO_VJ_ORT_THREADS_CLIP=N。
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.environ.get("AUTO_VJ_ORT_THREADS_CLIP", "4"))
        opts.inter_op_num_threads = 1
        if os.environ.get("AUTO_VJ_ORT_SPIN", "0") != "1":
            try:
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
                opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
            except Exception:
                pass
        self.session = ort.InferenceSession(
            os.path.join(_MODEL_DIR, "cn_clip_vision.onnx"),
            sess_options=opts, providers=["CPUExecutionProvider"])
        self.tag_emb = np.load(os.path.join(_MODEL_DIR, "tag_emb.npy")).astype(np.float32)
        self.tags = []
        with open(os.path.join(_MODEL_DIR, "tags.txt"), encoding="utf-8") as f:
            for line in f:
                z, e = line.strip().split("|")
                self.tags.append((z, e))
        # 相似度校准：实测（2026-09-18，用户 VJ 抽象光效素材 8 个样本 alpha 扫描）
        # 原始相似度排序本来就多样且准确（霓虹/激光/烟雾/几何/粒子各有区分），
        # 任何强度的先验减法都会让"稀有词"（宇宙/赛博朋克）霸榜——alpha=0.35 起全面失真。
        # 结论：88 标签词表上不存在"泛化词霸榜"问题，不校准。
        self._calib_alpha = 0.0

    # ---------- 视觉 ----------
    def _embed(self, bgr):
        import cv2
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (224, 224), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
        rgb = (rgb - _MEAN) / _STD
        x = np.transpose(rgb, (2, 0, 1))[None].astype(np.float32)
        emb = self.session.run(None, {"pixel_values": x})[0]
        return (emb / np.linalg.norm(emb, axis=1, keepdims=True))[0]

    def _rank(self, raw_sims, top):
        """排序：原始相似度降序（不校准，实测校准会让稀有词霸榜）+ 相对下限过滤"""
        order = np.argsort(-raw_sims)
        raw_top = float(raw_sims[order[0]])
        out = []
        for i in order:
            if float(raw_sims[i]) < raw_top - 0.12:   # 与最强标签相似度差太多的不要
                break
            out.append((self.tags[i][0], float(raw_sims[i])))
            if len(out) >= top:
                break
        return out

    def tag_frame(self, bgr, top=8):
        """对单帧返回 [(中文tag, 相似度)]，按校准后的相似度降序"""
        emb = self._embed(bgr)
        return self._rank(emb @ self.tag_emb.T, top)

    def tag_image(self, path, top=8):
        import cv2
        img = cv2.imread(path)
        if img is None:
            return []
        return self.tag_frame(img, top)

    def tag_video(self, path, n=5, top=8):
        """抽 n 帧取平均相似度（校准后），返回 [(tag, score)]"""
        import cv2
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return []
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100
        frames = []
        for frac in (0.15, 0.3, 0.45, 0.6, 0.8)[:n]:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * frac))
            ok, f = cap.read()
            if ok:
                frames.append(f)
        cap.release()
        if not frames:
            return []
        sims = np.zeros(len(self.tags), dtype=np.float32)
        for f in frames:
            emb = self._embed(f)
            sims += emb @ self.tag_emb.T
        sims /= len(frames)
        return self._rank(sims, top)

    # ---------- 颜色 / 亮度启发（确定性） ----------
    @staticmethod
    def color_brightness(bgr):
        """返回 {'colors':[颜色词...], 'bright':'明亮/昏暗/中等'}"""
        import cv2
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0].astype(np.float32)
        s = hsv[:, :, 1].astype(np.float32)
        v = hsv[:, :, 2].astype(np.float32)
        sat_mask = s > 40
        colors = []
        if sat_mask.sum() > len(sat_mask) * 0.05:
            hist, _ = np.histogram(h[sat_mask], bins=18, range=(0, 180))
            top_bin = int(np.argmax(hist))
            hue_c = (top_bin * 10 + 5) % 180
            for (lo, hi), name in _HUE_COLORS:
                if lo <= hue_c <= hi or (lo == 0 and hue_c >= 175):
                    colors.append(name)
                    break
            if hist.max() > 0 and len(colors) == 0:
                colors.append("彩色")
        else:
            mean_v = v.mean()
            colors.append("白色" if mean_v > 180 else ("黑色" if mean_v < 70 else "灰色"))
        bright = "明亮" if v.mean() > 170 else ("昏暗" if v.mean() < 90 else "中等")
        return {"colors": colors, "bright": bright}

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
    def analyze_dynamic(path, kind):
        """仅动态检测（不跑 CLIP，轻量）：返回 (motion, flicker, diff)。

        motion ∈ {低动态, 中动态, 高动态}；图片天然低动态；打不开返回 ('', False, -1)。
        供 analyze_media 复用，也供存量素材的「动态标签快速重扫」单独调用。

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
        """
        import cv2
        if kind == "image":
            return DYNAMIC_LOW, False, 0.0
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return "", False, -1.0
        # 顺序读全片（不 seek），一次采集：全程相邻帧差 + 全程亮度
        diffs = []    # 全程相邻帧差（灰度 abs diff 均值）
        brights = []  # 全程每帧亮度
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
        return motion, flicker, diff

    def analyze_media(self, path, kind, top=8):
        """统一入口：图片/视频 → {'tags':[(zh,score)], 'colors':[], 'bright':'', 'motion', 'flicker'}"""
        import cv2
        motion, flicker, _diff = self.analyze_dynamic(path, kind)
        if kind == "image":
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                img = cv2.imread(path)
            if img is None:
                return None
            has_alpha = img.ndim == 3 and img.shape[2] == 4
            if has_alpha:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            tags = self.tag_frame(img, top)
            frame = img
        else:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                return None
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.7))
            ok2, frame = cap.read()
            cap.release()
            tags = self.tag_video(path, top=top)
            if frame is None:
                frame = np.zeros((224, 224, 3), dtype=np.uint8)
        cb = self.color_brightness(frame)
        alpha = bool(locals().get("has_alpha", False))
        coverage = self.coverage_score(frame) if frame is not None else None
        dynamic = motion in (DYNAMIC_MID, DYNAMIC_HIGH)
        # 过滤 CLIP 误打的动态词：动态标签只应由帧差/频闪检测产出（DYNAMIC_TAGS 不进 CLIP 词表，
        # 但「高动态」历史上在 tags.txt 里，CLIP 单帧可能误打，这里统一剔除，避免与三档词冲突）
        tags = [(z, s) for z, s in tags if z not in DYNAMIC_TAGS]
        return {"tags": tags, "colors": cb["colors"], "bright": cb["bright"],
                "alpha": alpha, "motion": motion, "flicker": flicker,
                "coverage": coverage, "dynamic": dynamic}


def get_tagger():
    if Tagger._instance is None:
        with Tagger._lock:
            if Tagger._instance is None:
                Tagger._instance = Tagger()
    return Tagger._instance
