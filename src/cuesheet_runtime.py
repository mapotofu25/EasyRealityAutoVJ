# -*- coding: utf-8 -*-
"""码表**运行时解释器**：把 (码表, 曲目, 当前拍位) 翻译成"每一层现在该显示什么"。

★ 本模块**纯逻辑**：不碰 Qt、不碰引擎、不碰 IO（素材路径由调用方注入）。
  这样"什么时候切、切什么、淡入淡出到几成、素材播到第几秒"可以**完全离线测透**，
  再接进引擎 —— 引擎是最危险的地方，不把没测过的逻辑塞进去。
  自检：`python -m cuesheet_runtime`（零 Qt）。

═══ 五条关键规则（设计文档 §4.3 / §④ 定过）═══
1. **位置一律以位置源为权威**；本模块只消费**拍位**，不自己推断位置。
2. **回滞 0.15 拍**：片段边界附近位置抖动时不许来回触发。
3. **大跳变直接重算、不做过渡**：位置倒退或前跳 > 2 小节（8 拍）时，
   说明 DJ 打了 loop / 跳了一段 —— 立刻跟上，慢慢淡入反而更难看。
4. **缺素材只跳过该片段**，绝不让整条链断掉。
5. **变速是 `× speed`（禁止除法）**、**`src_out` 到点冻结最后一帧**、
   **item 级 `blend`/`opacity` 缺失时继承本层** —— 这三条与 Cue 端预览逐值一致，
   任一偏差都会"预览对、现场不对"。

★★ 单位纪律
  拍位（`pos_beats`）是主坐标（与指纹/位置源同一坐标系），
  **素材内起止用秒**（`src_in_sec/src_out_sec`），`speed` 是倍率。
  `beat_len`（一拍的秒数）由**本曲目 identity.bpm** 给出（`cuesheet.beat_len`），
  bpm 缺失/无效 ⇒ 退化 **1 拍 = 1 秒**（与 Cue 端 `units.py` 逐字同口径，绝不猜 BPM）。
"""
from __future__ import annotations

try:                                     # 演出端是扁平 import（src 在 sys.path 上）
    from cuesheet import beat_len as _cue_beat_len, DEGENERATE_BEAT_LEN
except ImportError:                      # pragma: no cover - 以包方式导入时
    from .cuesheet import beat_len as _cue_beat_len, DEGENERATE_BEAT_LEN

HYSTERESIS_BEATS = 0.15      # 边界回滞（拍）
BIG_JUMP_BEATS = 8.0         # > 这么多拍的变化视为"跳转"（2 小节）

_DEFAULT_XFORM = {"scale": 1.0, "x": 0.0, "y": 0.0}


class LayerPlayState:
    """某一层当前的播放状态（合成器直接消费）。"""
    __slots__ = ("lyr_id", "z", "blend", "opacity", "clip_id", "clip_path", "missing",
                 "item_id", "src_in_sec", "src_out_sec", "play_sec", "crop", "xform",
                 "speed", "active", "alpha", "trans", "trans_p", "note", "ended", "dur",
                 "src_ended")

    def __init__(self, lyr_id="", z=0, blend="normal", opacity=1.0):
        self.lyr_id = lyr_id
        self.z = z
        self.blend = blend
        self.opacity = opacity
        self.clip_id = ""
        self.clip_path = ""
        self.missing = False
        self.item_id = ""
        self.src_in_sec = 0.0
        self.src_out_sec = None      # None = 到素材末尾（不冻结）
        self.play_sec = 0.0
        self.crop = None
        self.xform = dict(_DEFAULT_XFORM)      # 恒为 dict（全默认 = cover，见契约①）
        self.speed = 1.0
        self.active = False
        self.alpha = 0.0
        self.trans = "fade"
        self.trans_p = 0.0
        self.note = ""
        self.dur = 0.0               # 素材自身时长（秒）；0 = 未知
        self.ended = False           # 片段区间已结束（end_beats 到期且本层无后继）
        self.src_ended = False       # ★ 素材已放到出点（play_sec 钳在 src_out，冻结该帧）

    def __repr__(self):
        return ("LayerPlayState(%s z=%d %s clip=%s play=%.3f alpha=%.2f%s%s%s)"
                % (self.lyr_id, self.z, self.blend, self.clip_id or "-",
                   self.play_sec, self.alpha, "" if self.active else " 未激活",
                   " 缺素材" if self.missing else "",
                   " 素材到点" if self.src_ended else ""))


def _eff_blend(layer, item):
    """片段**生效**的混合：片段自设优先，否则继承本层（`CompLayer.effective_blend` 同义）。"""
    fn = getattr(layer, "effective_blend", None)
    if callable(fn):
        return fn(item)
    b = str(getattr(item, "blend", "") or "")
    if b:
        return b
    return str(getattr(layer, "blend", "") or "normal")


def _eff_opacity(layer, item):
    """片段**生效**的不透明度：片段自设优先，否则继承本层。"""
    fn = getattr(layer, "effective_opacity", None)
    if callable(fn):
        return fn(item)
    o = getattr(item, "opacity", None)
    if o is not None:
        return min(1.0, max(0.0, float(o)))
    return min(1.0, max(0.0, float(getattr(layer, "opacity", 1.0) or 0.0)))


class CueRuntime:
    """一张码表 + 一首歌 的运行时。**每首歌 new 一个**（换歌即重建）。"""

    def __init__(self, sheet, track, media_paths=None, fps=60.0):
        self.sheet = sheet
        self.track = track
        self.media_paths = dict(media_paths or {})     # clip_id -> 磁盘路径
        self.fps = float(fps)
        # ★ 拍长来自**本曲目**的 bpm（码表时间轴的坐标系）；退化 ⇒ 1 拍 = 1 秒。
        bpm = 0.0
        if track is not None and getattr(track, "identity", None) is not None:
            bpm = getattr(track.identity, "bpm", 0.0)
        self.bpm = float(bpm or 0.0)
        self.beat_len = float(_cue_beat_len(self.bpm)) if self.bpm > 0 \
            else float(DEGENERATE_BEAT_LEN)
        self._lyrs = []                                # [(comp_layer, [items 按 at_beats 排序])]
        if track is not None:
            for ly in track.sorted_layers():           # z 小 → 大（后画在上）
                items = sorted([it for it in ly.items], key=lambda x: (x.at_beats, x.item_id))
                self._lyrs.append((ly, items))
        # ★ 素材时长表（clip_id → 秒）：用来**钳住**播放位置，见 `update()` 里的说明
        self._dur = {}
        for _m in (getattr(sheet, "media", None) or []):
            try:
                _c, _d = (str(getattr(_m, "clip_id", "") or ""),
                          float(getattr(_m, "duration_sec", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
            if _c and _d > 0.0:
                self._dur[_c] = _d
        self._state = {}                               # lyr_id -> 内部状态
        self.jumped = False                            # 本帧是否发生了"跳转重算"
        for ly, _items in self._lyrs:
            self._state[ly.lyr_id or id(ly)] = {"item": None, "alpha": 0.0, "last_pos": None}

    # ------------------------------------------------------------------
    @property
    def n_layers(self):
        return len(self._lyrs)

    @property
    def n_items(self):
        """当前曲目里排好的片段总数（UI 判"这张码表有没有内容"用）。"""
        return sum(len(items) for _ly, items in self._lyrs)

    def set_media_paths(self, media_paths):
        """更新 `clip_id → 路径` 映射（素材目录重挂后调用）。"""
        self.media_paths = dict(media_paths or {})
        return self

    def total_beats(self):
        """最后一层最后一个 item 的结束拍（没有 end 就用 at_beats）。"""
        mx = 0.0
        for _ly, items in self._lyrs:
            for it in items:
                mx = max(mx, it.end_beats if it.end_beats is not None else it.at_beats)
        return mx

    # ------------------------------------------------------------------
    def resolve_beat_len(self, beat_len=None, sheet_beat_len=None):
        """本帧用的一拍秒数：显式传入优先 → 码表 bpm → 退化 1.0（**绝不猜 BPM**）。

        ⚠ 旧实现里 `beat_len=None` 会让 `play_sec` **完全不推进**（画面冻在第 0 秒），
          与 Cue 端"退化 = 1 拍 1 秒"不一致 ⇒ 两端同一张码表行为不同。此处统一为退化 1.0。
        """
        for v in (beat_len, sheet_beat_len, self.beat_len):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0.0:
                return f
        return float(DEGENERATE_BEAT_LEN)

    def _pick_item(self, items, pos):
        """返回 (item, idx, ended)：最后一个 `at_beats <= pos` 且未结束的 item。

        ⚠ 用 `pos + HYSTERESIS` 做上界回滞，避免位置在边界抖动时反复切换。
        """
        cur = None
        for i, it in enumerate(items):
            if it.at_beats <= pos + HYSTERESIS_BEATS:
                cur = (it, i)
            else:
                break
        if cur is None:
            return None, -1, False
        it, i = cur
        if it.end_beats is not None and pos >= it.end_beats:
            return None, i, True            # 已结束（且本层后面没有新 item）
        return it, i, False

    def _alpha_for(self, it, nxt, pos):
        """算这一层当前的显示强度 0~1 + 过渡信息 `(alpha, trans, trans_p)`。

        · 入场：`trans_in == "cut"` 或时长≈0 ⇒ 直接 1.0；否则按拍线性 0→1。
        · 出场：本片段**自己的 `end_beats`** 优先（没有才用下一个 item 的 at_beats）→
          按 `trans_out_beats` 线性收尾；**淡出优先**（正在收尾时用 trans_out 覆盖 trans_in）。
          ★ 与 Cue 端 `timeline._alpha_for` 的 A1 口径一致：**`end` 优先于 `nxt`**，
            否则"前段设了时长 + 与下段之间有空隙"时，现场会在下一段起点才淡出（预览相反）。
        · `trans_p = a_in`（淡入）/ `1-a_out`（淡出）⇒ 合成器 `opacity × alpha × trans_p`
          得到有效 **α²**（契约②）。**这里不要改**，改了会与 Cue 端预览不一致。
        """
        if abs(it.trans_in_beats) < 1e-6 or it.trans_in == "cut":
            a_in = 1.0
        else:
            a_in = (pos - it.at_beats) / float(it.trans_in_beats)
            a_in = min(1.0, max(0.0, a_in))
        a_out = 1.0
        trans = it.trans_in
        trans_p = a_in
        # 淡出窗口的右边界：本片段 end 优先，其次下一个片段的 at（与 Cue 端 A1 同口径）
        fade_out_start = None
        if it.end_beats is not None:
            fade_out_start = it.end_beats
        elif nxt is not None:
            fade_out_start = nxt.at_beats
        if fade_out_start is not None and it.trans_out_beats > 1e-6 and it.trans_out != "cut":
            left = fade_out_start - pos
            if left <= it.trans_out_beats:
                a_out = max(0.0, left / float(it.trans_out_beats))
                if a_out < a_in:            # 淡出优先（正在收尾）
                    trans = it.trans_out
                    # ★★★ 2026-10-17（用户："编排结束后看不见淡出的过渡效果"）：
                    #   `fade` 的 `trans_p` 必须是**剩下的亮度**（a_out），不是"过渡进度"。
                    #   为什么：合成是 `opacity × alpha × trans_p`，而 alpha 本身就是 a_out
                    #     ⇒ 旧写法 trans_p = 1-a_out 时有效 α = **a_out(1-a_out)**：
                    #       淡出**刚开始**（a_out=1）就变 0 ⇒ 画面**瞬间消失**，
                    #       中间又冒出个 0.25 的鬼影 ⇒ 现场看就是"没有淡出，直接切掉"。
                    #   改 a_out 后有效 α = **a_out²**，与淡入的 a_in² 完全对称（契约②）。
                    #   ⚠ slide/zoom/glitch 的 `trans_p` 是**几何进度**（0→1 越走越远），
                    #     必须仍然是 1-a_out；所以只替换 `trans_out == "fade"` 这一种。
                    #   ⚠⚠ 编排端（EasyRealityCue `timeline._alpha_for`）必须同步改，
                    #     否则预览与演出不一致（契约②两端口径）。
                    trans_p = (a_out if it.trans_out == "fade" else 1.0 - a_out)
        return min(a_in, a_out), trans, trans_p

    # ------------------------------------------------------------------
    def update(self, pos_beats, beat_len=None, rate=1.0, sheet_beat_len=None):
        """核心：给定当前拍位，算出每一层该显示什么。

        `pos_beats`: 相对曲首的拍数（**由位置源给，本模块不做任何位置推断**）
        `beat_len` : 一拍秒数；None/≤0 ⇒ 用码表 bpm（再退化 1.0）
        `rate`     : 保留形参（旧调用方兼容；位置已含变速影响，此处不参与换算）
        返回 list[LayerPlayState]（按 z 升序，即绘制顺序）。
        """
        pos = float(pos_beats)
        bl = self.resolve_beat_len(beat_len, sheet_beat_len)
        self.jumped = False
        out = []
        for ly, items in self._lyrs:
            key = ly.lyr_id or id(ly)
            st = self._state.setdefault(key, {"item": None, "alpha": 0.0, "last_pos": None})
            last = st["last_pos"]
            # ★ 规则 3：大跳变 → 直接重算（清回滞状态），不做过渡
            if last is not None and (pos < last - HYSTERESIS_BEATS
                                     or pos > last + BIG_JUMP_BEATS):
                st["item"] = None
                st["alpha"] = 0.0
                self.jumped = True
            st["last_pos"] = pos

            it, idx, ended = self._pick_item(items, pos)
            nxt = items[idx + 1] if (idx >= 0 and idx + 1 < len(items)) else None

            ps = LayerPlayState(ly.lyr_id, ly.z, ly.blend, ly.opacity)
            if it is None:
                ps.alpha = 0.0
                ps.active = False
                ps.ended = bool(ended)
                st["alpha"] = 0.0
            else:
                a, trans, tp = self._alpha_for(it, nxt, pos)
                if self.jumped:                    # 跳变后不做过渡，直接到位
                    a, trans, tp = 1.0, it.trans_in, 1.0
                ps.item_id = it.item_id
                ps.clip_id = it.clip_id
                ps.clip_path = self.media_paths.get(it.clip_id, "")
                ps.missing = not bool(ps.clip_path)
                # ★ 规则 5a：item 级 blend/opacity（缺省继承本层）。**不要**直接读 ly.blend：
                #   那会让"每段素材各自的混合互不影响"失效。
                ps.blend = _eff_blend(ly, it)
                ps.opacity = _eff_opacity(ly, it)
                ps.src_in_sec = float(it.src_in_sec or 0.0)
                # ★ 规则 5：变速 × speed（= 更快）。**禁止改回除法**：
                #   Cue 端 timeline 是 `src_in + elapsed*speed`，演出端播放器
                #   `media_manager` 的 `interval / speed` 也是"倍速"⇒ 两端同向。
                dt_beats = max(0.0, pos - it.at_beats)
                spd = max(1e-6, float(it.speed or 1.0))
                # ★★★ 2026-10-18：**播放器倍速还要乘输入音乐的播放速率**。
                #   为什么（现场实测）：`play_sec` 由 `pos`（指纹算出的原曲秒）推出来，
                #   所以它天然按**音乐的实际速率**前进；而播放器默认只按 `it.speed` 走。
                #   音乐一旦不是 1.0 倍（DJ 降速/keylock/录音设备时钟差），两边就**结构性**
                #   差一个恒定比例 ⇒ 越放越远（实测某一场音乐速率 ≈ 0.861，1 分钟差 8 秒）。
                #   靠 `rate_for()` 的 ±15% 只能勉强兜住 0.85，再慢就没救了。
                #   ⇒ 直接把输入速率乘进播放器倍速，让"位置源"和"播放器"用同一个时间基。
                #   ⚠ `play_sec` 本身**不能**再乘一次（`pos` 里已经含速率）——那是双重计速。
                _r = 1.0
                try:
                    _r = float(rate)
                except (TypeError, ValueError):
                    _r = 1.0
                if not (_r > 0.05):
                    _r = 1.0
                ps.speed = spd * max(0.5, min(2.0, _r))
                raw_play = ps.src_in_sec + dt_beats * bl * spd
                # ★ 规则 5b：到达出点冻结最后一帧（不黑、不越过），直到本片段被替换。
                ps.src_out_sec = (None if it.src_out_sec is None
                                  else float(it.src_out_sec))
                # ★★★ 2026-10-18（真机联调抓到的）：**按素材时长钳位**。
                #   现场 `dmax=19.89s` 的单帧尖峰就是这么来的：LOGO 素材只有 20s，
                #   而这一段的 `play_sec` 走到了 20s 以上 ⇒ 解码器**回卷到 0**
                #   ⇒ `position()` 与 `play_sec` 相关 20 秒 ⇒ 控制器以为"差 20 秒"
                #   ⇒ 猛减速（spd 掉到 0.75）甚至 seek ⇒ **画面一跳**。
                #   正确语义与 `src_out_sec` 一致：**超过素材长度就冻结最后一帧**，
                #   绝不去追一个已经回卷的位置。
                _dur = self._dur.get(str(getattr(it, "clip_id", "") or ""), 0.0)
                _lim = ps.src_out_sec
                if _dur > 0.0 and (_lim is None or _dur < _lim):
                    _lim = _dur
                if _lim is not None and raw_play >= _lim:
                    ps.play_sec = float(_lim)
                    ps.src_ended = True
                    ps.src_out_sec = float(_lim)
                else:
                    ps.play_sec = raw_play
                    ps.src_ended = False
                ps.dur = float(self._dur.get(str(getattr(it, "clip_id", "") or ""), 0.0) or 0.0)
                ps.crop = list(it.crop) if it.crop else None
                # 恒给 dict：全默认 = cover（契约①）；None 会让下游"是否默认"判断分叉
                ps.xform = dict(it.xform) if it.xform else dict(_DEFAULT_XFORM)
                ps.active = True
                ps.alpha = a
                ps.trans = trans
                ps.trans_p = tp
                ps.note = it.note
                ps.ended = bool(it.end_beats is not None and pos >= it.end_beats)
                st["alpha"] = ps.alpha
                st["item"] = it.item_id
            out.append(ps)
        return out

    # ------------------------------------------------------------------
    def active_clips(self, states):
        """给引擎用：{lyr_id: 素材路径}（只含激活且有素材的）。"""
        r = {}
        for ps in states:
            if ps.active and ps.clip_path and not ps.missing:
                r[ps.lyr_id] = ps.clip_path
        return r


# ==========================================================================
# 自检（零 Qt，纯逻辑）
# ==========================================================================
def selftest():
    from cuesheet import (new_sheet, new_track, CompLayer, CueItem, MediaRef,
                          beats_to_sec)

    s = new_sheet("runtime 自检")
    te = new_track("T", "A", 300.0, 128.0)
    bl = 60.0 / 128.0

    L1 = CompLayer(lyr_id="L1", name="背景", z=0)
    L1.items.append(CueItem(item_id="i1", clip_id="c1", at_beats=32.0,
                            trans_in="fade", trans_in_beats=2.0,
                            trans_out="fade", trans_out_beats=2.0).normalized())
    L1.items.append(CueItem(item_id="i2", clip_id="c2", at_beats=64.0,
                            trans_in="cut", trans_out="cut").normalized())
    L2 = CompLayer(lyr_id="L2", name="LOGO", z=10, blend="screen", opacity=0.8)
    L2.items.append(CueItem(item_id="i3", clip_id="c1", at_beats=0.0,
                            trans_in="cut", trans_out="cut").normalized())
    te.layers += [L1, L2]
    s.tracks.append(te)
    for cid in ("c1", "c2"):
        s.media.append(MediaRef(clip_id=cid, name=cid + ".mp4"))

    rt = CueRuntime(s, te, media_paths={"c1": "P:/x/c1.mp4", "c2": "P:/x/c2.mp4"})
    assert rt.n_layers == 2, rt.n_layers
    assert rt.n_items == 3, rt.n_items
    assert abs(rt.beat_len - bl) < 1e-9, rt.beat_len
    assert rt.total_beats() == 64.0, rt.total_beats()

    def walk(runtime, a, b, step=0.1):
        """★ 必须**模拟逐帧单调推进** —— 真实引擎每帧都会调用 update。
        瞬移测试会（正确地）被判成"跳转"，测不到过渡。"""
        last = None
        p = a
        while p <= b + 1e-9:
            last = runtime.update(p, beat_len=bl)
            p += step
        return last

    # --- 第 0 拍：LOGO 层(0 拍起)应激活、背景层(32 拍起)未激活 ---
    st = rt.update(0.0, beat_len=bl)
    d = {x.lyr_id: x for x in st}
    assert d["L2"].active and d["L2"].alpha == 1.0, d["L2"]
    assert not d["L1"].active, d["L1"]
    # --- 走到 31 拍：背景层仍未激活 ---
    assert not {x.lyr_id: x for x in walk(rt, 0.1, 31.0)}["L1"].active

    # --- 到达 32 拍：L1 开始淡入（2 拍），L2 一直满 ---
    st = rt.update(32.0, beat_len=bl)
    d = {x.lyr_id: x for x in st}
    assert d["L1"].active and abs(d["L1"].alpha - 0.0) < 1e-6, d["L1"]
    assert d["L1"].clip_path.endswith("c1.mp4") and d["L1"].play_sec == 0.0, d["L1"]
    assert d["L2"].active and abs(d["L2"].alpha - 1.0) < 1e-6, d["L2"]
    assert d["L2"].blend == "screen" and abs(d["L2"].opacity - 0.8) < 1e-6

    st = walk(rt, 32.1, 33.0)
    d = {x.lyr_id: x for x in st}
    assert abs(d["L1"].alpha - 0.5) < 1e-6, d["L1"]                    # 淡入中点
    assert abs(d["L1"].play_sec - (1.0 * bl)) < 1e-6, d["L1"]          # speed=1 ⇒ ×1

    st = walk(rt, 33.1, 34.5)
    assert abs({x.lyr_id: x for x in st}["L1"].alpha - 1.0) < 1e-6     # 淡入完成

    # --- 快到 i2（64 拍）时 L1 淡出（末段 2 拍处是 0.5）---
    st = walk(rt, 34.6, 63.0)
    d = {x.lyr_id: x for x in st}
    assert d["L1"].trans == "fade" and abs(d["L1"].alpha - 0.5) < 1e-6, d["L1"]

    # --- 到达 64 拍：L1 换到 i2（cut ⇒ 立即满）---
    st = walk(rt, 63.1, 64.0)
    d = {x.lyr_id: x for x in st}
    assert d["L1"].item_id == "i2", d["L1"]
    assert d["L1"].clip_path.endswith("c2.mp4"), d["L1"]
    assert abs(d["L1"].alpha - 1.0) < 1e-6, d["L1"]
    assert abs(d["L1"].play_sec) < 1e-6, d["L1"]      # 刚进 i2，进度≈0（浮点容差）

    # --- ★ 变速方向：speed=2 ⇒ 同样源跨度**播放更快**（play_sec 翻倍），不是慢放 ---
    L1.items[0].speed = 2.0
    rt2 = CueRuntime(s, te, media_paths={"c1": "x", "c2": "y"})
    rt2.update(32.0, beat_len=bl)
    st = rt2.update(34.0, beat_len=bl)
    d = {x.lyr_id: x for x in st}
    assert abs(d["L1"].play_sec - (2.0 * bl * 2.0)) < 1e-6, d["L1"]
    # 时长恒等式：2 拍跨度 / speed=2 ⇒ 源跨度 = 2 拍·bl·2
    assert abs((d["L1"].play_sec - d["L1"].src_in_sec) - 2.0 * bl * 2.0) < 1e-6
    L1.items[0].speed = 1.0

    # --- ★★ 变速方向的决定性用例：src_in=0/src_out=10，speed .5/1/2 ⇒ 时间轴 20/10/5 秒 ---
    #   （若得到 5/10/20 就是用了除法=慢放，现场与预览方向相反。）
    #   时间轴长度**由 runtime 自己的 src_ended 边沿实测**，不是只验公式。
    for spd, want_len in ((0.5, 20.0), (1.0, 10.0), (2.0, 5.0)):
        te_s = new_track("TS", "AS", 300.0, 128.0)
        Ls = CompLayer(lyr_id="LS", name="speed", z=0)
        Ls.items.append(CueItem(item_id="s", clip_id="c1", at_beats=0.0,
                                src_in_sec=0.0, src_out_sec=10.0, speed=spd,
                                trans_in="cut", trans_out="cut").normalized())
        te_s.layers.append(Ls)
        rts = CueRuntime(s, te_s, media_paths={"c1": "x"})
        measured = None
        t = 0.0
        while t <= want_len + 2.0:
            if rts.update(t / bl, beat_len=bl)[0].src_ended:
                measured = t
                break
            t += 0.01
        assert measured is not None and abs(measured - want_len) <= 0.02, \
            ("时间轴长度错（很可能用了除法=慢放）", spd, measured, want_len)

    # --- ★ src_out 冻结：到了出点停住（play_sec 不再前进，src_ended=True）---
    L1.items[0].src_in_sec = 0.0
    L1.items[0].src_out_sec = 5.0
    L1.items[0].speed = 1.0
    rt7 = CueRuntime(s, te, media_paths={"c1": "x", "c2": "y"})
    # 出点对应拍数：32 + 5/bl（+1e-3 拍越过出点，避开浮点边界）
    pos_out = 32.0 + 5.0 / bl + 1e-3
    d = {x.lyr_id: x for x in rt7.update(pos_out, beat_len=bl)}
    assert abs(d["L1"].play_sec - 5.0) < 1e-6, d["L1"]
    assert d["L1"].src_ended is True, d["L1"]
    d = {x.lyr_id: x for x in rt7.update(pos_out + 1.0, beat_len=bl)}
    assert abs(d["L1"].play_sec - 5.0) < 1e-6, d["L1"]      # 冻结（不越过出点）
    assert d["L1"].src_ended is True and d["L1"].active is True
    d = {x.lyr_id: x for x in rt7.update(pos_out - 1.0, beat_len=bl)}
    assert d["L1"].src_ended is False, d["L1"]              # 未到出点不冻结
    L1.items[0].src_out_sec = None

    # --- ★ item 级 blend/opacity 继承：""/None ⇒ 跟随本层；有值 ⇒ 覆盖 ---
    L1.blend, L1.opacity = "screen", 0.6
    L1.items[0].blend, L1.items[0].opacity = "", None
    d = {x.lyr_id: x for x in CueRuntime(s, te, media_paths={"c1": "x"}).update(40.0,
                                                                              beat_len=bl)}
    assert d["L1"].blend == "screen" and abs(d["L1"].opacity - 0.6) < 1e-6, d["L1"]
    L1.items[0].blend, L1.items[0].opacity = "add", 0.25
    d = {x.lyr_id: x for x in CueRuntime(s, te, media_paths={"c1": "x"}).update(40.0,
                                                                              beat_len=bl)}
    assert d["L1"].blend == "add" and abs(d["L1"].opacity - 0.25) < 1e-6, d["L1"]
    L1.items[0].blend, L1.items[0].opacity = "", None
    L1.blend, L1.opacity = "normal", 1.0

    # --- 大跳变（倒退）：标记 jumped、不做过渡 ---
    rt5 = CueRuntime(s, te, media_paths={"c1": "x", "c2": "y"})
    walk(rt5, 0.0, 40.0)
    st = rt5.update(5.0, beat_len=bl)
    assert rt5.jumped, "倒退应标记为跳转"
    assert not {x.lyr_id: x for x in st}["L1"].active, st
    # --- 大跳变（前跳 > 8 拍）：同样重算，alpha 直接到位（不慢慢淡）---
    rt6 = CueRuntime(s, te, media_paths={"c1": "x", "c2": "y"})
    walk(rt6, 0.0, 40.0)
    st = rt6.update(60.0, beat_len=bl)
    assert rt6.jumped and abs({x.lyr_id: x for x in st}["L1"].alpha - 1.0) < 1e-6, st

    # --- 缺素材：只跳过该 item，不崩 ---
    rt3 = CueRuntime(s, te, media_paths={})
    st = rt3.update(40.0, beat_len=bl)
    d = {x.lyr_id: x for x in st}
    assert d["L1"].active and d["L1"].missing and d["L1"].clip_path == "", d["L1"]
    assert rt3.active_clips(st) == {}, rt3.active_clips(st)

    # --- z 顺序：返回顺序必须是 z 升序（引擎按序绘制 ⇒ 大的在上）---
    st = rt.update(70.0, beat_len=bl)
    assert [x.z for x in st] == sorted(x.z for x in st), [x.z for x in st]

    # --- end_beats 到期后该层失活（ended=True 供养"不再画"）---
    L1.items[1].end_beats = 80.0
    rt4 = CueRuntime(s, te, media_paths={"c1": "x", "c2": "y"})
    rt4.update(70.0, beat_len=bl)
    assert {x.lyr_id: x.active for x in rt4.update(79.0, beat_len=bl)}["L1"] is True
    d = {x.lyr_id: x for x in rt4.update(81.0, beat_len=bl)}
    assert d["L1"].active is False and d["L1"].ended is True, d["L1"]
    L1.items[1].end_beats = None

    # --- 退化 bpm（0）⇒ 1 拍 = 1 秒，且 play_sec 照常推进（不冻在第 0 秒）---
    te0 = new_track("T0", "A0", 300.0, 0.0)
    Lz = CompLayer(lyr_id="LZ", name="z", z=0)
    Lz.items.append(CueItem(item_id="z1", clip_id="c1", at_beats=10.0,
                            trans_in="cut", trans_out="cut").normalized())
    te0.layers.append(Lz)
    rtz = CueRuntime(s, te0, media_paths={"c1": "x"})
    assert abs(rtz.beat_len - 1.0) < 1e-12, rtz.beat_len
    d = {x.lyr_id: x for x in rtz.update(14.0)}          # 不传 beat_len
    assert abs(d["LZ"].play_sec - 4.0) < 1e-6, d["LZ"]

    # --- 拍→秒 只走 cuesheet 的换算口径（抽样值与 Cue 端 units 同值）---
    assert abs(beats_to_sec(32.0, 155.03) - 12.384699735535058) < 1e-9

    print("[runtime] 自检通过 ✓  层数=%d 片段=%d 总长=%.1f 拍（%.1fs @128BPM）beat_len=%.5f"
          % (rt.n_layers, rt.n_items, rt.total_beats(), rt.total_beats() * bl, rt.beat_len))
    return True


if __name__ == "__main__":
    selftest()
