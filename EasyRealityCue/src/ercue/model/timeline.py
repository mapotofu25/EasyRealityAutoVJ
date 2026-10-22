# -*- coding: utf-8 -*-
"""**时间轴解释器**：(码表, 曲目, 当前位置秒) → 每一层此刻该显示什么。

对应旧实现 `cuesheet_runtime.py`，但**坐标从"拍"改成"秒"**（架构方案 §3.2 硬约束）。

★ 本模块**纯逻辑、零 Qt**：不碰 Qt、不碰引擎、不碰 IO（素材路径由调用方注入）。
  这样"什么时候切、切什么、淡入淡出到几成"可以完全离线测透，再接进 UI。
  （零 Qt 由 `tests/test_timeline.py` 的 AST 断言守着。）

═══ 硬约束（架构方案 §4-D / §4-E，逐条对照）═══
  D3  返回的 states **必须按 z 升序**（z 大的后画 = 在上）。写成断言。
  D4  `play_sec = src_in_sec + 已播时长 × speed`（变速折算）。
  E1  回滞：边界抖动不许来回触发。旧实现 0.15 拍 ⇒ bpm 有效时等价换算、下限 0.05s；
      bpm 缺失/无效时直接取 0.05s 下限（退化态不猜 BPM）。
  E2  大跳变（> 2 小节 = 8 拍；无 bpm 时 > 2 秒）⇒ **直接把当前片段重算，不做过渡**
      （DJ 打 loop / 跳段时就该立刻跟上，慢慢淡入更难看）。
  E3  缺素材**只跳过该 item**，绝不让整条链断掉（missing 标记交给合成器跳过）。
  E4  测试必须**逐帧单调推进** —— 瞬移测试会（正确地）被判成跳转，测不到过渡。

★★ 单位纪律（最容易翻车处）
  内部唯一坐标 = **秒**。本文件**不出现任何 `beat_len` 的乘除**：需要"拍 → 秒"的
  等价换算一律走 `units.beats_to_sec()`（`units.py` 是唯一换算入口）。

可离线自检：`python -m ercue.model.timeline`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import units as _units

# --------------------------------------------------------------------------
# 常量（秒坐标体系下的取值依据见模块 docstring）
# --------------------------------------------------------------------------
HYSTERESIS_BEATS = 0.15        # E1：旧实现的回滞量（拍）
MIN_HYSTERESIS_SEC = 0.05      # E1：秒坐标下的回滞下限
BIG_JUMP_BEATS = 8.0           # E2：8 拍 = 2 小节
BIG_JUMP_DEGENERATE_SEC = 2.0  # E2：无 bpm 时的大跳变阈值（秒）


def hysteresis_sec(bpm) -> float:
    """E1 回滞（秒）。

    · bpm 有效 → `max(0.05, 0.15 拍的秒数)`（非 120BPM 时随拍长变化）；
    · bpm 缺失/无效 → 直接取 `0.05s` 下限（退化态绝不猜 BPM，见 §3.2）。
    """
    if _units.bpm_is_usable(bpm):
        return max(MIN_HYSTERESIS_SEC, _units.beats_to_sec(HYSTERESIS_BEATS, bpm))
    return MIN_HYSTERESIS_SEC


def big_jump_sec(bpm) -> float:
    """E2 大跳变阈值（秒）。

    · bpm 有效 → 8 拍（2 小节）的秒数；
    · bpm 缺失/无效 → `2.0s`。
    """
    if _units.bpm_is_usable(bpm):
        return _units.beats_to_sec(BIG_JUMP_BEATS, bpm)
    return BIG_JUMP_DEGENERATE_SEC


# ==========================================================================
# 输出数据结构
# ==========================================================================
@dataclass
class LayerDrawState:
    """某一层**此刻**该显示什么（交给 `render/compositor` 画）。

    字段与任务书一致；`active` 为便捷判别（合成器可直接用 `active/missing/alpha` 跳过）。
    """
    lyr_id: str = ""
    z: int = 0                    # 越大越在上
    blend: str = "normal"
    opacity: float = 1.0
    clip_id: str = ""
    item_id: str = ""
    missing: bool = False         # 素材缺失（E3：只跳过本 item）
    src_in_sec: float = 0.0
    play_sec: float = 0.0         # 该素材此刻应播到第几秒（D4）
    crop: Optional[list] = None
    xform: Optional[dict] = None
    speed: float = 1.0
    alpha: float = 0.0            # 含过渡量的最终不透明度系数（合成时只乘一次，D2）
    trans: str = "fade"           # 当前正在进行的过渡类型
    trans_p: float = 0.0          # 过渡进度 0~1
    src_out_sec: Optional[float] = None   # ★ A2：素材出点（None = 到素材末尾）
    src_ended: bool = False       # ★ A2：素材已放到出点（play_sec 已钳在出点，此帧起冻结）
    note: str = ""
    ended: bool = False
    active: bool = False

    def __repr__(self) -> str:
        return ("LayerDrawState(%s z=%d %s clip=%s play=%.3f alpha=%.3f%s%s)"
                % (self.lyr_id, self.z, self.blend, self.clip_id or "-", self.play_sec,
                   self.alpha, "" if self.active else " 未激活",
                   " 缺素材" if self.missing else ""))


# ==========================================================================
# ★ #17「相邻同设置视为一个素材」（run）—— 纯逻辑，零 Qt
# ==========================================================================
ADJ_EPS = 1e-3     # 秒：相邻判定阈值（"紧挨着"的容差）

#: 参与"设置相同"判定的**渲染属性**。
#: ⚠ `item_id`（身份）、`at_sec`/`end_sec`（位置）、`note`（不影响画面）**不算**。
RUN_ATTRS = ("clip_id", "speed", "src_in_sec", "src_out_sec", "crop", "xform",
             "blend", "opacity", "trans_in", "trans_in_sec", "trans_out", "trans_out_sec")


def _as_items(items) -> list:
    try:
        return list(items)
    except TypeError:
        return []


def _run_key(it) -> tuple:
    """把影响渲染的属性归一成**可比较**的键（dict/list 也归一，避免不可哈希 / 顺序敏感）。"""
    vals = []
    for k in RUN_ATTRS:
        v = getattr(it, k, None)
        if isinstance(v, dict):
            v = tuple(sorted((str(kk), repr(vv)) for kk, vv in v.items()))
        elif isinstance(v, (list, tuple)):
            v = tuple(repr(x) for x in v)
        vals.append(repr(v))
    return tuple(vals)


def same_render_settings(a, b) -> bool:
    """两个片段的**渲染设置**是否完全相同（见 `RUN_ATTRS`）。"""
    return _run_key(a) == _run_key(b)


def _sort_key(it):
    return (_units.to_float(it.at_sec), str(getattr(it, "item_id", "")))


def group_runs(items, eps=ADJ_EPS) -> list:
    """把片段按"**同设置 + 紧挨**"归成若干 **run**；返回 `[(i, j), …]`（**闭区间下标**）。

    入参 `items` 内部会**按 `(at_sec, item_id)` 排序**后处理，故返回的下标是**排序后**的下标。

    · **同设置** = `RUN_ATTRS` 全等（`same_render_settings`）；
    · **紧挨**   = `next.at − prev.end_eff ≈ 0`；其中 `prev.end_sec is None` ⇒ **天然相邻**
      （开放结尾的 `end_eff` 就是 `next.at`）；容差 = `eps`；
    · **有缝隙**，或**中间有一片设置不同**（如改了 `crop`/速度/混合）⇒ 在那里**断段**；
    · **单个片段 ⇒ `[(i, i)]`**（run 长度 1 ⇒ 渲染行为与改动前**完全一致**，这是回归底线）。
    """
    src = _as_items(items)
    order = sorted(range(len(src)), key=lambda i: _sort_key(src[i]))
    ordered = [src[i] for i in order]
    runs = []
    if not ordered:
        return runs
    start = 0
    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if not same_render_settings(prev, cur):
            runs.append((start, i - 1))
            start = i
            continue
        if prev.end_sec is None:
            adjacent = True                            # 开放结尾 ⇒ 天然相邻
        else:
            adjacent = abs(_units.to_float(cur.at_sec)
                           - _units.to_float(prev.end_sec)) <= eps
        if not adjacent:
            runs.append((start, i - 1))
            start = i
    runs.append((start, len(ordered) - 1))
    return runs


def run_flags(items, eps=ADJ_EPS) -> dict:
    """`{item_id: (is_run_start, is_run_end)}`。

    E17 渲染语义用它：**段内**片段抑制淡入淡出（`trans='cut'`）、**段首**正常淡入、
    **段尾**正常淡出；单片段 run ⇒ `(True, True)` ⇒ 行为不变。
    """
    src = _as_items(items)
    ordered = [src[i] for i in sorted(range(len(src)), key=lambda i: _sort_key(src[i]))]
    out = {}
    for i, j in group_runs(src, eps):
        for k in range(i, j + 1):
            out[str(getattr(ordered[k], "item_id", ""))] = (k == i, k == j)
    return out


class _LayerPlan:
    """内部：一层 + 已按 `at_sec` 排好序的 items。"""

    __slots__ = ("layer", "items")

    def __init__(self, layer, items):
        self.layer = layer
        self.items = items


# ==========================================================================
# 解释器
# ==========================================================================
class CueRuntime:
    """一张码表 + 一首歌 的运行时。**每首歌 new 一个**（换歌即重建）。"""

    def __init__(self, sheet, track, media_paths=None):
        """
        `sheet`         : `CueSheet`（用于查 media，可传 None）
        `track`         : `TrackEntry`（提供 identity.bpm 与 composition.layers）
        `media_paths`   : `{clip_id: 磁盘路径}`；缺失(或未提供)即视为**素材缺失**（E3）。
        """
        self.sheet = sheet
        self.track = track
        self.media_paths = dict(media_paths or {})
        bpm = 0.0
        if track is not None and getattr(track, "identity", None) is not None:
            bpm = getattr(track.identity, "bpm", 0.0)
        self.bpm = _units.to_float(bpm)
        self.hyst = hysteresis_sec(self.bpm)          # E1
        self.jump = big_jump_sec(self.bpm)            # E2

        self._plans = []                              # [_LayerPlan]，已按 z 升序
        if track is not None:
            for ly in track.sorted_layers():
                items = sorted([it for it in getattr(ly, "items", [])],
                               key=lambda x: (_units.to_float(x.at_sec), x.item_id))
                self._plans.append(_LayerPlan(ly, items))

        self._last_pos = None                          # 上一次 update 的位置（秒）
        self.jumped = False                            # 本帧是否发生"跳变重算"
        # ★ E17（#17）：预计算每个片段的 run 标记（`item_id -> (is_run_start, is_run_end)`）。
        #   纯逻辑、只在构造时算一次；单片段 run 恒为 (True, True) ⇒ 行为与改动前一致。
        self._run_flags = {}
        for plan in self._plans:
            self._run_flags.update(run_flags(plan.items))

    # ------------------------------------------------------------------
    @property
    def n_layers(self) -> int:
        return len(self._plans)

    @property
    def n_items(self) -> int:
        """当前曲目里**排好的片段总数**（零 Qt）。

        ★ 仅供 UI 判"这张编排有没有内容"（例如预览该显示"还没有排好的片段"还是"准备中"）。
        注意**不能用 `total_sec() > 0` 代替**：一条 `at_sec=0` 且 `end_sec=None` 的片段
        `total_sec()` 也是 0，但那显然是有内容的。
        """
        return sum(len(p.items) for p in self._plans)

    def set_media_paths(self, media_paths) -> "CueRuntime":
        """更新 `clip_id → 路径` 映射（素材目录重挂后调用）。"""
        self.media_paths = dict(media_paths or {})
        return self

    def total_sec(self) -> float:
        """编排总长（秒）= 所有层最后一个 item 的 `end_sec`（无则 `at_sec`）的最大值。"""
        mx = 0.0
        for plan in self._plans:
            for it in plan.items:
                end = it.end_sec if it.end_sec is not None else it.at_sec
                mx = max(mx, _units.to_float(end))
        return mx

    # ------------------------------------------------------------------
    def _pick_item(self, items, pos: float):
        """返回 `(item, idx, ended)`。

        取**最后一个** `at_sec <= pos + 回滞` 的 item（E1：上界回滞避免边界抖动反复切）；
        若它已过 `end_sec` → 本层失活并置 `ended=True`；无可用 → `(None, -1, False)`。
        """
        cur = None
        for i, it in enumerate(items):
            if _units.to_float(it.at_sec) <= pos + self.hyst:
                cur = (it, i)
            else:
                break
        if cur is None:
            return None, -1, False
        it, i = cur
        if it.end_sec is not None and pos >= _units.to_float(it.end_sec):
            return None, i, True
        return it, i, False

    def _alpha_for(self, it, nxt, pos: float):
        """算这一层当前的显示强度 `alpha`（0~1）+ `(trans, trans_p)`。

        · 入场：`trans_in == "cut"` 或时长≈0 ⇒ 直接 1.0；否则按时间线性 0→1。
        · 出场：下一个 item 到来前 / 本 item `end_sec` 前，按 `trans_out_sec` 线性收尾；
          **淡出优先**（正在收尾时用 trans_out 覆盖 trans_in）。
        """
        tin = _units.to_float(it.trans_in_sec, 2.0)
        # ★ E17（#17）：run 的**段内**片段抑制淡入/淡出（只段首淡入、段尾淡出）。
        #   单片段 run ⇒ (True, True) ⇒ 下面两个 suppress 都是 False ⇒ **行为不变**。
        is_start, is_end = self._run_flags.get(str(getattr(it, "item_id", "")), (True, True))
        suppress_in, suppress_out = (not is_start), (not is_end)
        if suppress_in or it.trans_in == "cut" or abs(tin) < 1e-6:
            a_in = 1.0
        else:
            a_in = (pos - _units.to_float(it.at_sec)) / tin
            a_in = min(1.0, max(0.0, a_in))
        a_out = 1.0
        trans = "cut" if suppress_in else it.trans_in
        trans_p = a_in
        # ★ A1：淡出窗口的右边界。
        #   本 item **有 end_sec**（面板「时长」/ 拖右边缘都会写它）⇒ 以本 item 的 `end_sec`
        #   为准；**只有开放结尾（end_sec is None）**才退回用下一个 item 的 `at_sec`。
        #   旧写法把 `nxt.at_sec` 放在前面，导致"前段设了时长 + 与下段之间有空隙"时
        #   淡出窗口被算到下一个 item 处 ⇒ 本段到点**硬切**（而不是按时长淡出）。
        fade_start = None
        if it.end_sec is not None:
            fade_start = _units.to_float(it.end_sec)
        elif nxt is not None:
            fade_start = _units.to_float(nxt.at_sec)
        tout = _units.to_float(it.trans_out_sec, 2.0)
        if (not suppress_out) and fade_start is not None and tout > 1e-6 \
                and it.trans_out != "cut":
            left = fade_start - pos
            if left <= tout:
                a_out = max(0.0, left / tout)
                if a_out < a_in:                  # 淡出优先（正在收尾）
                    trans = it.trans_out
                    # ★★★ 2026-10-17：`fade` 的 `trans_p` = **剩下的亮度**（a_out），
                    #   不是"过渡进度"（1-a_out）。合成是 `opacity × alpha × trans_p`，
                    #   而 alpha 本身就是 a_out ⇒ 旧写法有效 α = a_out(1-a_out)：
                    #   淡出**刚开始**就变 0（画面瞬间消失），中间又冒出 0.25 的鬼影
                    #   ⇒ 现场就是"没有淡出，直接切掉"。改成 a_out 后有效 α = a_out²，
                    #   与淡入的 a_in² 对称（契约②）。
                    #   ⚠ slide/zoom/glitch 的 trans_p 是**几何进度**，仍是 1-a_out；
                    #   ⚠⚠ 演出端 `src/cuesheet_runtime.py::_alpha_for` 必须同步（已同步）。
                    trans_p = (a_out if it.trans_out == "fade" else 1.0 - a_out)
        return min(a_in, a_out), trans, trans_p

    # ------------------------------------------------------------------
    def update(self, pos_sec: float) -> list:
        """核心：给定当前**时间轴位置（秒）**，算出每一层该显示什么。

        返回 `list[LayerDrawState]`，**按 z 升序**（= 绘制顺序，D3）。
        """
        pos = _units.to_float(pos_sec)

        # E2：大跳变（倒退超过回滞，或前跳超过阈值）⇒ 本帧重算、不做过渡
        last = self._last_pos
        self.jumped = False
        if last is not None and (pos < last - self.hyst or pos > last + self.jump):
            self.jumped = True
        self._last_pos = pos

        out = []
        for plan in self._plans:
            ly = plan.layer
            # ★ 需求 9（方案 B）：混合/不透明度已**下沉到素材（item）级** —— 这里必须按
            #   "本片段"解析有效值（片段自设优先，否则继承本层）。**不要**直接读 ly.blend，
            #   那会让"每段素材各自的混合互不影响"失效（旧码表靠继承仍逐值不变）。
            it, idx, ended = self._pick_item(plan.items, pos)
            st = LayerDrawState(
                lyr_id=ly.lyr_id, z=int(ly.z),
                blend=(ly.effective_blend(it) if it is not None else ly.blend),
                opacity=(ly.effective_opacity(it) if it is not None else
                         _units.to_float(ly.opacity, 1.0)))
            if it is None:
                st.active = False
                st.alpha = 0.0
                st.ended = bool(ended)
            else:
                nxt = plan.items[idx + 1] if 0 <= idx and idx + 1 < len(plan.items) else None
                a, trans, tp = self._alpha_for(it, nxt, pos)
                if self.jumped:                   # 跳变后不做过渡，直接到位
                    a, tp, trans = 1.0, 1.0, it.trans_in
                clip_id = it.clip_id or ""
                path = self.media_paths.get(clip_id) if clip_id else None
                st.item_id = it.item_id
                st.clip_id = clip_id
                st.missing = not bool(path)       # E3：只标缺失，不断链
                st.src_in_sec = _units.to_float(it.src_in_sec)
                spd = max(1e-6, _units.to_float(it.speed, 1.0))
                st.speed = spd
                # D4：play_sec = 入点 + 已播时长 × speed（变速折算）
                #
                # ⚠⚠ 防回归：**必须乘 speed，禁止改回除法**（team-lead 2026-10-01 裁定）。
                #   `× speed` 与**演出端一致** —— 演出端 speed 就是"倍速"：
                #     · AutoVJ `src/media_manager.py:817`
                #         `wait = float(interval / self.speed - (perf_counter() - t0))`
                #       （四个播放器同此式：speed 越大间隔越短、帧出得越快 ⇒ 2 = 快 2 倍）
                #     · AutoVJ `src/engine.py:359`  注释原文：`self.speed = 1.0  # 图层播放速度倍率`
                #     · AutoVJ `src/audio_engine.py:116` 原文：`与引擎里 speed（输出端 VJ 素材倍速）完全是两回事`
                #     · AutoVJ `src/ui_main.py:4496` 菜单名：`播放速度`
                #   旧运行时 `AutoVJ_码表备份_20260930/src/cuesheet_runtime.py:174` 写的是
                #   `(dt_beats * bl) / spd`（**除法 = 慢放**），与它自己的播放器语义相反 ——
                #   结果是"编辑器预览的变速方向与现场演出相反"，是旧实现的**真实缺陷**。
                #   ⇒ 本项目一律 `× speed`；任何"改回 `/ speed` 与旧实现对齐"的改动都是错的。
                elapsed = max(0.0, pos - _units.to_float(it.at_sec))
                raw_play = st.src_in_sec + elapsed * spd
                # ★ A2：素材出点 `src_out_sec`。不为 None 时把 play_sec **钳到出点**并冻结
                #   最后一帧（`src_ended=True`），直到本 item 自己结束/被下一段替换。
                #   语义依据（旧实现逐行核对，详见 docs/_audit/_fix_a.md）：
                #     · 旧 runtime `cuesheet_runtime.py:174` 只算 `src_in + dt/spd`，**完全忽略 src_out**；
                #     · 旧引擎 `engine.py:2511 _cue_frame` 只取 `player.current()`，素材放到末尾
                #       时播放器保持最后一帧、**不因素材放完而失活**；层是否显示只由 item 的
                #       end_beats/下一段 + 淡入淡出决定。
                #   ⇒ 采用"到出点冻结"：视觉上等价于"素材物理放完后停住"，且尊重用户设定的出点。
                st.src_out_sec = (None if it.src_out_sec is None
                                  else _units.to_float(it.src_out_sec))
                if st.src_out_sec is not None and raw_play >= st.src_out_sec:
                    st.play_sec = st.src_out_sec
                    st.src_ended = True
                else:
                    st.play_sec = raw_play
                    st.src_ended = False
                st.crop = list(it.crop) if it.crop else None
                st.xform = (dict(it.xform) if it.xform
                            else {"scale": 1.0, "x": 0.0, "y": 0.0})
                st.alpha = a
                st.trans = trans
                st.trans_p = tp
                st.note = it.note
                st.ended = False
                st.active = True
            out.append(st)

        out.sort(key=lambda s: s.z)               # D3 契约：z 升序
        assert all(out[i].z <= out[i + 1].z for i in range(len(out) - 1)), \
            "D3 违约：states 必须按 z 升序"
        return out


# ==========================================================================
# 自检（纯逻辑，零 Qt）
# ==========================================================================
def selftest() -> bool:
    from .cuesheet import CueSheet, TrackEntry, Identity, CompLayer, CueItem, MediaRef

    sheet = CueSheet(title="timeline 自检")
    te = TrackEntry(track_key="k", identity=Identity(title="T", artist="A",
                                                     duration_sec=300.0, bpm=120.0))
    te.identity.ensure_meta_id()
    L1 = CompLayer(lyr_id="L1", name="背景", z=0, blend="normal", opacity=1.0)
    L1.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=32.0,
                            trans_in="fade", trans_in_sec=2.0,
                            trans_out="fade", trans_out_sec=2.0).normalized())
    L1.items.append(CueItem(item_id="i2", clip_id="c2", at_sec=64.0,
                            trans_in="cut", trans_out="cut").normalized())
    L2 = CompLayer(lyr_id="L2", name="LOGO", z=10, blend="screen", opacity=0.8)
    L2.items.append(CueItem(item_id="i3", clip_id="c2", at_sec=0.0,
                            trans_in="cut", trans_out="cut").normalized())
    te.composition.layers += [L1, L2]
    sheet.tracks.append(te)
    for cid in ("c1", "c2"):
        sheet.media.append(MediaRef(clip_id=cid, name=cid + ".mp4"))

    rt = CueRuntime(sheet, te, media_paths={"c1": "P:/x/c1.mp4", "c2": "P:/x/c2.mp4"})
    assert rt.n_layers == 2, rt.n_layers
    assert abs(rt.total_sec() - 64.0) < 1e-9, rt.total_sec()

    def walk(runtime, a, b, step=0.02):
        p, last = a, None
        while p <= b + 1e-9:
            last = runtime.update(p)
            p += step
        return last

    # --- 0 秒：LOGO 层满、背景层未激活 ---
    d = {x.lyr_id: x for x in rt.update(0.0)}
    assert d["L2"].active and abs(d["L2"].alpha - 1.0) < 1e-9, d["L2"]
    assert not d["L1"].active, d["L1"]

    # --- 走到 32 秒：L1 开始淡入 ---
    d = {x.lyr_id: x for x in walk(rt, 0.02, 32.0)}
    assert d["L1"].active and abs(d["L1"].alpha) < 1e-6, d["L1"]
    assert abs(d["L1"].play_sec) < 1e-6, d["L1"]

    # --- 33 秒：淡入中点 ---
    d = {x.lyr_id: x for x in walk(rt, 32.02, 33.0)}
    assert abs(d["L1"].alpha - 0.5) < 1e-6, d["L1"]

    # --- 34 秒：淡入完成 ---
    d = {x.lyr_id: x for x in walk(rt, 33.02, 34.0)}
    assert abs(d["L1"].alpha - 1.0) < 1e-6, d["L1"]

    # --- 63 秒：接近 i2，淡出中点 ---
    d = {x.lyr_id: x for x in walk(rt, 34.02, 63.0)}
    assert d["L1"].trans == "fade" and abs(d["L1"].alpha - 0.5) < 1e-6, d["L1"]

    # --- 65 秒：已切到 i2（cut ⇒ 满） ---
    d = {x.lyr_id: x for x in walk(rt, 63.02, 65.0)}
    assert d["L1"].item_id == "i2" and abs(d["L1"].alpha - 1.0) < 1e-6, d["L1"]

    # --- D4 变速折算（× speed）---
    it = CueItem(item_id="a", clip_id="c1", at_sec=10.0, src_in_sec=2.0, speed=2.0)
    L1.items = [it.normalized()]
    rt2 = CueRuntime(sheet, te, media_paths={"c1": "x"})
    assert abs({x.lyr_id: x for x in rt2.update(13.0)}["L1"].play_sec - 8.0) < 1e-9

    # --- D3 z 升序 ---
    L1.items = [CueItem(item_id="i1", clip_id="c1", at_sec=32.0,
                        trans_in="fade", trans_in_sec=2.0,
                        trans_out="fade", trans_out_sec=2.0).normalized(),
                CueItem(item_id="i2", clip_id="c2", at_sec=64.0,
                        trans_in="cut", trans_out="cut").normalized()]
    st = CueRuntime(sheet, te, media_paths={"c1": "x", "c2": "y"}).update(70.0)
    assert [x.z for x in st] == sorted(x.z for x in st), [x.z for x in st]

    # --- E3 缺素材只跳过，不断链（无任何路径 ⇒ 两层都标 missing，但两层 state 都在）---
    rt3 = CueRuntime(sheet, te, media_paths={})
    d = {x.lyr_id: x for x in rt3.update(40.0)}
    assert len(d) == 2, "整条链不许断：两层的 state 都要返回"
    assert d["L1"].active and d["L1"].missing, d["L1"]
    assert d["L2"].active and d["L2"].missing, d["L2"]
    # 只补 L2（c2）的素材 ⇒ 只有 L1（c1）标 missing
    rt4 = CueRuntime(sheet, te, media_paths={"c2": "P:/x/c2.mp4"})
    d = {x.lyr_id: x for x in rt4.update(40.0)}
    assert d["L1"].missing and not d["L2"].missing, (d["L1"], d["L2"])

    print("[timeline] 自检通过 ✓  层数=%d 总长=%.1fs  回滞=%.4fs 大跳变=%.3fs"
          % (rt.n_layers, rt.total_sec(), rt.hyst, rt.jump))
    return True


if __name__ == "__main__":
    selftest()
