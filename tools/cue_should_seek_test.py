# -*- coding: utf-8 -*-
"""`_cue_should_seek` / 防自激 / 计速唯一性 —— **单元自测（零 Qt、零依赖、可 CI）**。

为什么先写它（Step4 之前）：
    Step4 里最容易出错、又最"看起来能跑"的，是**漂移校正的判定与自激防护**：
      · 用"请求目标"当漂移基准 ⇒ 自己跟自己比 ⇒ 永远不触发（画面不报错、就是不贴拍）；
      · `src_ended` 期间还在校正 ⇒ 解码器 pts 继续涨 ⇒ 每帧都判超阈值 ⇒ 每帧 seek；
      · seek 还没落地就又发一次 ⇒ 长 GOP 素材上自激；
      · `set_speed` 与 `play_sec` 同时计速 ⇒ 实测 2 倍速。
    这些用**假播放器桩**就能全部钉死，**不需要改播放器、不需要 GUI、不需要渲一帧**。

被测对象（两份来源，自动选择）：
    ① `src/cue_seek_policy.py`（**Step4 应新增的零 Qt 模块**）—— 存在就用它（测真代码）；
    ② 否则用本文件的 `ReferencePolicy`（与任务书 §5.2/§5.4 逐条一致的参考实现）——
       这样本脚本在 Step4 落地前后都能跑。

运行：
    python tools/cue_should_seek_test.py                 # 跑全部用例
    python tools/cue_should_seek_test.py -v              # 逐条打印
    python tools/cue_should_seek_test.py --impl file     # 强制用参考实现
    python tools/cue_should_seek_test.py --impl real     # 强制用 src/cue_seek_policy.py（不存在则失败）
    python tools/cue_should_seek_test.py --mutations     # ★变异验证：故意改坏 5 处，要求每处都被抓

退出码：0 = 全过；1 = 有用例失败；2 = 有变异未被抓到（说明测试本身不够硬）。
"""
from __future__ import annotations

import argparse
import os
import sys

TOL = 1e-9
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))

# ---- 与任务书 §4.1 一致的常量（被判对象可覆盖，测试只读这里做期望值）----
DRIFT_SEC = 0.35
DRIFT_SEC_CONSV = 1.00
MIN_INTERVAL_SEC = 1.0
SEEK_TIMEOUT_SEC = 2.0
FPS = 60.0


# ==========================================================================
# 假播放器桩：只实现被测逻辑需要的最小面
# ==========================================================================
class FakePlayer:
    """最小播放器桩：`position()` / `seek()` / `set_speed()` / `current()`。

    · `advance(dt)`     推进"实际显示帧的位置"（= 正常前向播放）
    · `seek_delay`      模拟长 GOP 的落地延迟（秒）；0 = 立即落地
    · `refuse_seek`     seek 直接不受理（返回 False）
    · `pos_is_none`     position() 返回 None（模拟取不到 pts）
    """

    def __init__(self, pos=0.0, fps=FPS, speed=1.0, seek_delay=0.0, refuse_seek=False,
                 pos_is_none=False, clock=None):
        self._pos = float(pos)
        self.fps = float(fps)
        self.speed = float(speed)
        self.seek_delay = float(seek_delay)
        self.refuse_seek = bool(refuse_seek)
        self.pos_is_none = bool(pos_is_none)
        self.calls = []              # [(t, target)]
        self.speed_calls = []        # [speed]
        self._pending = None         # (target, land_at)
        self._now = clock or (lambda: 0.0)

    # ---- 播放器面 ----
    def current(self):
        return "<img@%.3f>" % self._pos

    def position(self):
        if self.pos_is_none:
            return None
        self._settle()
        return self._pos

    def seek(self, sec):
        self.calls.append((self._now(), float(sec)))
        if self.refuse_seek:
            return False
        if self.seek_delay <= 0.0:
            self._pos = float(sec)
        else:
            self._pending = (float(sec), self._now() + self.seek_delay)
        return True

    def set_speed(self, s):
        self.speed = float(s)
        self.speed_calls.append(float(s))

    def advance(self, dt):
        """正常前向播放：按 speed 前进（模拟"播放器在计速"）。"""
        self._settle()
        if self._pending is None:
            self._pos += float(dt) * self.speed

    # ---- 桩内部 ----
    def _settle(self):
        if self._pending is not None and self._now() >= self._pending[1]:
            self._pos = self._pending[0]
            self._pending = None

    @property
    def inflight(self):
        return self._pending is not None

    def seek_count(self):
        return len(self.calls)


class FakeItem:
    def __init__(self, codec="h264", is_dxv=False, gop=None):
        self._codec = codec
        self._is_dxv = bool(is_dxv)
        if gop is not None:
            self._cue_gop_frames = int(gop)


class FakePS:
    """`LayerPlayState` 的最小替身（只带被测字段）。"""

    def __init__(self, item_id="i1", play_sec=0.0, src_in_sec=0.0, speed=1.0,
                 src_ended=False, active=True, missing=False, clip_path="P:/x.mp4"):
        self.item_id = item_id
        self.play_sec = float(play_sec)
        self.src_in_sec = float(src_in_sec)
        self.speed = float(speed)
        self.src_ended = bool(src_ended)
        self.active = bool(active)
        self.missing = bool(missing)
        self.clip_path = clip_path
        self.lyr_id = "L1"


# ==========================================================================
# 参考实现（与任务书 §5.2 / §5.4 逐条一致）
# ==========================================================================
MUTATIONS = {
    "drift_uses_target": False,   # 变异1：用请求目标当漂移基准（自己跟自己比）
    "skip_src_ended": False,      # 变异2：去掉 src_ended 判断
    "skip_inflight": False,       # 变异3：去掉 in-flight 判断
    "speed_always_one": False,    # 变异4：set_speed 恒 1.0（双重计速）
    "no_throttle": False,         # 变异5：去掉节流（允许连续 seek）
}


class ReferencePolicy:
    """任务书 §5.2/§5.4 的参考实现（Step4 应下沉到 `src/cue_seek_policy.py`）。"""

    def __init__(self, cfg=None, now=None):
        self.cfg = dict(cfg or {})
        self._now = now or (lambda: 0.0)
        self.state = {}

    # ---- 档位（§4.3：全 O(1) 只读；禁止运行时探测）----
    def mode(self, item):
        prof = str(self.cfg.get("seek_profile", "auto") or "auto")
        if prof == "off":
            return "off"
        if prof == "conservative":
            return "conservative"
        gop = getattr(item, "_cue_gop_frames", None) if item is not None else None
        if item is not None and (getattr(item, "_is_dxv", False)
                                 or str(getattr(item, "_codec", "")) == "dxv"
                                 or (gop is not None and gop <= 2)):
            return "free"
        if item is not None and (str(getattr(item, "_codec", "")) == "hevc"
                                 or (gop is not None and gop > 60)):
            return "conservative"
        return "normal"

    def drift_limit(self, mode):
        if mode == "conservative":
            return DRIFT_SEC_CONSV
        if mode == "free":
            # ★ 裁定 Q2：DXV 也不逐秒 ⇒ 与普通档一致
            return DRIFT_SEC
        return DRIFT_SEC

    def land_tol(self, player):
        fps = float(getattr(player, "fps", 0.0) or 0.0)
        return 2.0 / (fps if fps > 1.0 else 30.0)

    # ---- 状态 ----
    def state_for(self, ps):
        st = self.state.get(ps.lyr_id)
        if st is None:
            st = {"item_id": "", "last_req": -1e9, "inflight": False, "target": 0.0,
                  "degraded": False, "inited": False}
            self.state[ps.lyr_id] = st
        return st

    # ---- ② 结算上一次 seek（§3.3 落地判定）----
    def settle(self, ps, player, st):
        if not st["inflight"]:
            return None
        pos = player.position()
        if pos is not None and abs(pos - st["target"]) <= self.land_tol(player):
            st["inflight"] = False
            return "landed"
        if (self._now() - st["last_req"]) > SEEK_TIMEOUT_SEC:
            st["inflight"] = False
            st["degraded"] = True
            return "timeout"
        return None

    # ---- ③ 判定（§5.4 的 11 条，顺序即语义）----
    def should_seek(self, ps, item, player, st):
        if ps.missing or not ps.active:
            return False                                    # 1
        if ps.src_ended and not MUTATIONS["skip_src_ended"]:
            return False                                    # 2 ★防自激
        # 3 R11：换片段先清"降级/目标/节流"，但**不清 in-flight**
        switched = st["item_id"] != ps.item_id
        if switched and not st["inflight"]:
            st["degraded"] = False
            st["target"] = 0.0
            st["last_req"] = -1e9
        if st["degraded"]:
            return False                                    # 4
        if st["inflight"] and not MUTATIONS["skip_inflight"]:
            return False                                    # 5 ★防自激
        mode = self.mode(item)
        if switched:
            return mode != "off"                            # 6 换片段
        if mode == "off":
            return False                                    # 6
        if mode == "conservative":
            return False                                    # 7 保守档只入场
        pos = player.position()
        if pos is None:
            return False                                    # 8
        if (not MUTATIONS["no_throttle"]
                and (self._now() - st["last_req"]) < MIN_INTERVAL_SEC):
            return False                                    # 9 节流
        ref = st["target"] if MUTATIONS["drift_uses_target"] else pos
        limit = self.drift_limit(mode)
        if abs(ps.play_sec - ref) > limit:
            return True                                     # 10
        return False                                        # 11

    # ---- 发请求后记账 ----
    def on_seek(self, ps, st, accepted):
        st["item_id"] = ps.item_id
        st["target"] = ps.play_sec
        st["last_req"] = self._now()
        if accepted:
            st["inflight"] = True
        else:
            st["degraded"] = True
        return accepted

    # ---- ④ 计速（§5.3：主路径 = 播放器计速）----
    def desired_speed(self, ps):
        if MUTATIONS["speed_always_one"]:
            return 1.0
        return float(ps.speed or 1.0)


# ==========================================================================
# 帧循环驱动（把 §5.2 的四步串起来，供"行为类"用例复用）
# ==========================================================================
class Driver:
    """模拟 `_cue_frame` 每帧的一次调用（不含绘制）。"""

    def __init__(self, policy, player, item, clock):
        self.policy = policy
        self.player = player
        self.item = item
        self.clock = clock
        self.log = []          # [(kind, detail)]
        # ★ 桩必须用**同一个时钟**：否则桩的 `_now()` 恒 0 ⇒ seek 永不落地、
        #   记录的时间戳全是 0（第一版就踩了这个坑，用例 ⑬ 误报"间隔 0.000s"）。
        player._now = clock

    def frame(self, ps):
        st = self.policy.state_for(ps)
        p = self.player
        # ② 结算
        res = self.policy.settle(ps, p, st)
        if res:
            self.log.append(("settle", res))
        # ③ 判定 + 发请求
        if self.policy.should_seek(ps, self.item, p, st):
            ok = p.seek(ps.play_sec)
            self.policy.on_seek(ps, st, ok)
            self.log.append(("seek", ps.play_sec))
        # ④ 计速
        want = self.policy.desired_speed(ps)
        if abs(float(getattr(p, "speed", 1.0)) - want) > 1e-6:
            p.set_speed(want)
        return p.position()


# ==========================================================================
# 极小测试框架（不引入 pytest）
# ==========================================================================
class Runner:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.passed = []
        self.failed = []

    def case(self, name):
        def deco(fn):
            self._run(name, fn)
            return fn
        return deco

    def _run(self, name, fn):
        try:
            fn()
        except AssertionError as e:
            self.failed.append((name, str(e) or "assert 失败"))
            print("  [FAIL] %s\n         %s" % (name, str(e) or "assert 失败"))
            return
        except Exception as e:                                   # noqa: BLE001
            self.failed.append((name, "%s: %s" % (type(e).__name__, e)))
            print("  [ERR ] %s\n         %s: %s" % (name, type(e).__name__, e))
            return
        self.passed.append(name)
        if self.verbose:
            print("  [ ok ] %s" % name)


def _assert(cond, msg=""):
    if not cond:
        raise AssertionError(msg)


def _close(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol


class Clock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += float(dt)


def _mk(policy_cls, clock, cfg=None):
    try:
        return policy_cls(cfg=cfg, now=clock)
    except TypeError:
        return policy_cls(cfg, clock)


# ==========================================================================
# 用例
# ==========================================================================
def build_suite(Policy, verbose=False):
    R = Runner(verbose)

    # ---------------- 1. 11 条判定（逐条） ----------------
    def c01_missing_or_inactive():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem()
        for kwargs, why in ((dict(missing=True), "missing"), (dict(active=False), "inactive")):
            ps = FakePS(play_sec=10.0, **kwargs)
            st = pol.state_for(ps); st["item_id"] = ps.item_id   # 非换片段
            _assert(pol.should_seek(ps, it, pl, st) is False,
                    "第1条失败：%s 时不应 seek" % why)
    R.case("① missing / not active ⇒ False")(c01_missing_or_inactive)

    def c02_src_ended():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem()
        ps = FakePS(play_sec=10.0, src_ended=True)
        st = pol.state_for(ps); st["item_id"] = ps.item_id
        _assert(pol.should_seek(ps, it, pl, st) is False,
                "第2条失败：src_ended=True 时必须不 seek（否则每帧 seek 自激）")
    R.case("② src_ended ⇒ False（防自激）")(c02_src_ended)

    def c03_degraded():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem()
        ps = FakePS(play_sec=10.0)
        st = pol.state_for(ps); st["item_id"] = ps.item_id; st["degraded"] = True
        _assert(pol.should_seek(ps, it, pl, st) is False, "第3条失败：degraded 后不应再 seek")
    R.case("③ degraded ⇒ False（降级后不再 seek）")(c03_degraded)

    def c04_inflight():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, seek_delay=0.8); it = FakeItem()
        ps = FakePS(play_sec=10.0)
        st = pol.state_for(ps); st["item_id"] = ps.item_id
        st["inflight"] = True; st["target"] = 10.0; st["last_req"] = clk()
        _assert(pol.should_seek(ps, it, pl, st) is False,
                "第4条失败：in-flight 期间不得再发 seek（长 GOP 上会自激）")
    R.case("④ in-flight ⇒ False（防自激）")(c04_inflight)

    def c05_item_switch():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem()
        ps = FakePS(play_sec=3.0)
        st = pol.state_for(ps); st["item_id"] = "OLD"
        st["last_req"] = clk()          # 即便刚刚 seek 过也要立即换片段
        _assert(pol.should_seek(ps, it, pl, st) is True,
                "第5条失败：换片段应立即 seek（不受节流限制）")
    R.case("⑤ 换片段 ⇒ True（不受节流限制）")(c05_item_switch)

    def c06_off():
        clk = Clock(); pol = _mk(Policy, clk, cfg={"seek_profile": "off"})
        pl = FakePlayer(pos=0.0); it = FakeItem()
        ps = FakePS(play_sec=99.0)
        st = pol.state_for(ps); st["item_id"] = "OLD"
        _assert(pol.should_seek(ps, it, pl, st) is False, "第6条失败：seek_profile=off 时应完全不 seek")
    R.case("⑥ mode=off ⇒ False（配置闸门）")(c06_off)

    def c07_conservative():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem(codec="hevc")
        ps = FakePS(play_sec=99.0)
        st = pol.state_for(ps); st["item_id"] = ps.item_id; st["last_req"] = -1e9
        _assert(pol.should_seek(ps, it, pl, st) is False,
                "第7条失败：保守档（HEVC）只入场，不做运行中校正")
    R.case("⑦ 保守档运行中 ⇒ False（只入场）")(c07_conservative)

    def c08_position_none():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, pos_is_none=True); it = FakeItem()
        ps = FakePS(play_sec=99.0)
        st = pol.state_for(ps); st["item_id"] = ps.item_id; st["last_req"] = -1e9
        _assert(pol.should_seek(ps, it, pl, st) is False,
                "第8条失败：position() 为 None 时应跳过校正（不报错、不 seek）")
    R.case("⑧ position()=None ⇒ False（跳过校正）")(c08_position_none)

    def c09_throttle():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem()
        ps = FakePS(play_sec=50.0)
        st = pol.state_for(ps)
        st["item_id"] = ps.item_id; st["last_req"] = clk()   # 刚发过
        _assert(pol.should_seek(ps, it, pl, st) is False,
                "第9条失败：距上次 < MIN_INTERVAL 时应节流")
        clk.tick(MIN_INTERVAL_SEC + 0.01)
        _assert(pol.should_seek(ps, it, pl, st) is True,
                "第9条失败：超过节流窗口后应恢复判定")
    R.case("⑨ 节流窗口内 ⇒ False；超出 ⇒ 恢复")(c09_throttle)

    def c10_drift_detects_real_position():
        """★ 最重要：漂移必须用 position()（实际显示帧），不是请求目标。"""
        clk = Clock(); pol = _mk(Policy, clk)
        it = FakeItem()
        # 情形A：实际位置落后 1.0s（> 0.35）⇒ 必须 seek
        pl = FakePlayer(pos=9.0)
        ps = FakePS(play_sec=10.0)
        st = pol.state_for(ps)
        st["item_id"] = ps.item_id; st["last_req"] = -1e9
        st["target"] = 10.0        # ← 上次请求目标 = play_sec：用目标值算漂移会得 0
        _assert(pol.should_seek(ps, it, pl, st) is True,
                "第10条失败：|position()-play_sec|=1.0 > 0.35 必须 seek；"
                "若这里返回 False，说明漂移用了『请求目标』（自己跟自己比，永远不触发）")
        # 情形B：实际位置只差 0.1s（< 0.35）⇒ 不 seek
        pl2 = FakePlayer(pos=9.9)
        pol2 = _mk(Policy, clk)
        st2 = pol2.state_for(ps)
        st2["item_id"] = ps.item_id; st2["last_req"] = -1e9
        _assert(pol2.should_seek(ps, it, pl2, st2) is False,
                "第10条失败：|drift|=0.1 < 0.35 不应 seek")
        # 情形C：保守档阈值 1.0s，差 0.9s ⇒ 不 seek（且它本就不做运行中校正）
        pl3 = FakePlayer(pos=9.1)
        pol3 = _mk(Policy, clk)
        it3 = FakeItem(codec="hevc")
        st3 = pol3.state_for(ps)
        st3["item_id"] = ps.item_id; st3["last_req"] = -1e9
        _assert(pol3.should_seek(ps, it3, pl3, st3) is False,
                "第10条失败：保守档不应做运行中校正")
    R.case("⑩ 漂移用 position()（不是请求目标）⇒ True/False 正确")(c10_drift_detects_real_position)

    def c11_default_false():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=10.0); it = FakeItem()
        ps = FakePS(play_sec=10.0)
        st = pol.state_for(ps); st["item_id"] = ps.item_id; st["last_req"] = -1e9
        _assert(pol.should_seek(ps, it, pl, st) is False,
                "第11条失败：漂移未超阈值时应返回 False")
    R.case("⑪ 漂移未超阈值 ⇒ False")(c11_default_false)

    # ---------------- 2. 防自激三连 ----------------
    def c12_src_ended_freeze_no_seek():
        """冻结 10 秒（600 帧）期间 seek 次数必须为 0。"""
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=5.0, speed=2.0); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=5.0, speed=2.0, src_ended=True)
        n = 0
        for _ in range(600):                     # 10s @60fps
            ps.play_sec += 2.0 / FPS              # 码表时间继续走（模拟"素材已冻结但拍在走"）
            drv.frame(ps)
            clk.tick(1.0 / FPS)
            n += 1
        _assert(pl.seek_count() == 0,
                "防自激失败：src_ended 期间 seek 了 %d 次（应为 0）" % pl.seek_count())
        _assert(n == 600, "用例自身错误")
    R.case("⑫ src_ended 冻结 10s ⇒ seek 次数 = 0")(c12_src_ended_freeze_no_seek)

    def c13_inflight_no_resend():
        """慢落地（800ms，模拟长 GOP）：in-flight 期间不得重发。"""
        clk = Clock(); pol = _mk(Policy, clk)
        # 从"素材第 0 秒"跳播到"第 2 秒"：目标与当前位置不同，in-flight 才有意义
        pl = FakePlayer(pos=0.0, seek_delay=0.8); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=2.0)
        # 第 1 帧：换片段 ⇒ 发第一次 seek（in-flight，800ms 后落地到 2.0）
        drv.frame(ps)
        _assert(pl.seek_count() == 1, "换片段应发 1 次 seek，实际 %d" % pl.seek_count())
        _assert(pl.inflight is True, "慢落地时桩应处于 in-flight")
        # 接下来 45 帧（0.75s，仍未落地）且漂移巨大 ⇒ 不得重发
        for _ in range(45):
            clk.tick(1.0 / FPS)
            ps.play_sec += 1.0 / FPS
            drv.frame(ps)
        _assert(pl.seek_count() == 1,
                "防自激失败：in-flight 期间重发了 seek（共 %d 次）" % pl.seek_count())
        # 落地（>0.8s）后：位置到 2.0，之后靠播放器前进；漂移超阈值才允许下一次
        for _ in range(120):
            clk.tick(1.0 / FPS)
            ps.play_sec += 1.0 / FPS
            drv.frame(ps)
            pl.advance(1.0 / FPS)
        _assert(pl.seek_count() >= 1, "落地后应恢复正常判定")
        # 任意两次请求的间隔都必须 ≥ MIN_INTERVAL（换片段除外）
        ts = [c[0] for c in pl.calls]
        for a, b in zip(ts, ts[1:]):
            _assert((b - a) >= MIN_INTERVAL_SEC - 1e-6,
                    "节流失败：两次 seek 间隔 %.3fs < %.1fs" % (b - a, MIN_INTERVAL_SEC))
    R.case("⑬ in-flight 期间不重发；落地后恢复；间隔 ≥ 1s")(c13_inflight_no_resend)

    def c14_position_none_no_crash():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, pos_is_none=True); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=100.0)
        for _ in range(120):
            drv.frame(ps)
            clk.tick(1.0 / FPS)
            ps.play_sec += 1.0 / FPS
        # ⚠ 第 1 次"换片段"的入场 seek 是允许的（§5.4 第 5 条先于第 8 条：
        #   position() 读数不可信不妨碍"入场定位到 src_in"）。
        #   之后一旦进入运行中，position()=None 必须让它**再也不 seek**。
        _assert(pl.seek_count() == 1,
                "position()=None 时除入场外不得 seek（实际 %d 次）" % pl.seek_count())
    R.case("⑭ position()=None 连跑 120 帧 ⇒ 不炸、不 seek")(c14_position_none_no_crash)

    # ---------------- 3. 计速唯一性 ----------------
    def c15_speed_always_ps_speed():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, speed=1.0, seek_delay=0.0); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=0.0, speed=2.0)
        for _ in range(300):                     # 5s @60fps
            ps.play_sec += 2.0 / FPS
            drv.frame(ps)
            pl.advance(1.0 / FPS)                # 播放器按 speed 前进（= 计速在播放器）
            clk.tick(1.0 / FPS)
        _assert(pl.speed_calls, "应至少调用一次 set_speed")
        _assert(all(_close(s, 2.0) for s in pl.speed_calls),
                "计速失败：set_speed 序列出现非 ps.speed 的值 %s（主路径必须恒为 ps.speed）"
                % set(pl.speed_calls))
        _assert(not any(_close(s, 1.0) for s in pl.speed_calls),
                "计速失败：出现了 set_speed(1.0)（那意味着切换到 play_sec 主导，与主路径矛盾）")
        # 位置跟踪：5s 内 play_sec 与 position() 同步（< 0.35）
        _assert(abs(pl.position() - ps.play_sec) <= DRIFT_SEC + 1e-6,
                "跟踪失败：|pos-play|=%.3f 超阈值" % abs(pl.position() - ps.play_sec))
    R.case("⑮ set_speed 恒为 ps.speed、无 1.0、位置跟得上")(c15_speed_always_ps_speed)

    # ---------------- 4. 落地判定 ----------------
    def c16_landed_clears_inflight():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, seek_delay=0.2, clock=clk); it = FakeItem()
        ps = FakePS(play_sec=5.0)
        st = pol.state_for(ps)
        ps_ok = pl.seek(5.0)
        pol.on_seek(ps, st, ps_ok)
        _assert(st["inflight"] is True, "on_seek 后应置 in-flight")
        clk.tick(0.1)
        _assert(pol.settle(ps, pl, st) is None, "未到落地点不应结算")
        clk.tick(0.15)                            # 共 0.25s > 0.2s 落地
        res = pol.settle(ps, pl, st)
        _assert(res == "landed" and st["inflight"] is False,
                "落地判定失败：res=%r inflight=%r" % (res, st["inflight"]))
    R.case("⑯ |pos-target| ≤ 2 帧 ⇒ landed，清 in-flight")(c16_landed_clears_inflight)

    def c17_timeout_degrades_once():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, seek_delay=99.0); it = FakeItem()   # 永不落地
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=5.0)
        drv.frame(ps)                            # 第1次 seek
        base = pl.seek_count()
        for _ in range(int(SEEK_TIMEOUT_SEC * FPS) + 30):
            clk.tick(1.0 / FPS)
            ps.play_sec += 1.0 / FPS
            drv.frame(ps)
        st = pol.state_for(ps)
        _assert(st["degraded"] is True, "超时后应置 degraded")
        _assert(pl.seek_count() == base,
                "降级后不得再 seek（seek 次数 %d → %d）" % (base, pl.seek_count()))
        _assert(any(k == "settle" and v == "timeout" for k, v in drv.log), "应记录一次 timeout")
    R.case("⑰ 超时 ⇒ degraded 且此后不再 seek")(c17_timeout_degrades_once)

    def c18_refuse_degrades():
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, refuse_seek=True); it = FakeItem()
        ps = FakePS(play_sec=0.0)
        st = pol.state_for(ps)
        ok = pl.seek(0.0)
        pol.on_seek(ps, st, ok)
        _assert(ok is False and st["degraded"] is True,
                "seek 不受理 ⇒ 必须立刻 degraded（不能反复重试）")
        _assert(st["inflight"] is False, "不受理时不应置 in-flight")
    R.case("⑱ seek 不受理 ⇒ 立刻降级（不重试）")(c18_refuse_degrades)

    # ---------------- 5. 档位 + 频率上界 ----------------
    def c19_mode_table():
        clk = Clock(); pol = _mk(Policy, clk)
        _assert(pol.mode(FakeItem(codec="dxv", is_dxv=True)) == "free", "dxv 应判 free")
        _assert(pol.mode(FakeItem(codec="h264")) == "normal", "h264 应判 normal")
        _assert(pol.mode(FakeItem(codec="hevc")) == "conservative", "hevc 应判 conservative")
        _assert(pol.mode(FakeItem(codec="", gop=1)) == "free", "GOP=1 应判 free")
        _assert(pol.mode(FakeItem(codec="h264", gop=61)) == "conservative", "GOP>60 应升保守档")
        _assert(pol.mode(FakeItem(codec="h264", gop=30)) == "normal", "GOP=30 应 normal")
        _assert(pol.mode(None) == "normal", "item=None 应回落 normal（不猜 DXV）")
        _assert(_mk(Policy, clk, cfg={"seek_profile": "off"}).mode(FakeItem(codec="dxv",
                                                                          is_dxv=True)) == "off",
                "配置 off 应覆盖一切")
        _assert(_mk(Policy, clk, cfg={"seek_profile": "conservative"}).mode(
            FakeItem(codec="dxv", is_dxv=True)) == "conservative", "配置 conservative 应覆盖一切")
    R.case("⑲ 档位：free/normal/conservative/off + 未知回落 normal")(c19_mode_table)

    def _run_30s(item, ps_kwargs, player_speed_factor=0.97, cfg=None):
        """模拟 30s：码表时间与播放器都按 speed 前进，但播放器略慢（模拟 pacing 抖动）。"""
        clk = Clock(); pol = _mk(Policy, clk, cfg=cfg)
        pl = FakePlayer(pos=0.0, speed=1.0, seek_delay=0.0)
        drv = Driver(pol, pl, item, clk)
        ps = FakePS(**ps_kwargs)
        spd = float(ps.speed)
        dt = 1.0 / FPS
        for _ in range(int(30.0 * FPS)):
            ps.play_sec += dt * spd
            drv.frame(ps)
            pl.advance(dt * spd * player_speed_factor)   # 播放器略慢 ⇒ 漂移缓慢累积
            clk.tick(dt)
        return pl, drv

    def c20_rate_free():
        pl, drv = _run_30s(FakeItem(codec="dxv", is_dxv=True), dict(play_sec=0.0, speed=1.0))
        _assert(pl.seek_count() <= 30,
                "频率上界失败：DXV 30s 内 seek %d 次 > 30（裁定 Q2：不逐秒）" % pl.seek_count())
        _assert(pl.seek_count() >= 1, "DXV 30s 应至少校正过一次（否则阈值/判定写错）")
    R.case("⑳ 频率上界：DXV ≤ 30/30s 且 ≥1")(c20_rate_free)

    def c21_rate_normal():
        pl, drv = _run_30s(FakeItem(codec="h264"), dict(play_sec=0.0, speed=1.0))
        _assert(pl.seek_count() <= 30,
                "频率上界失败：H.264 30s 内 seek %d 次 > 30（MIN_INTERVAL=1.0 应兜住）"
                % pl.seek_count())
    R.case("㉑ 频率上界：H.264 ≤ 30/30s")(c21_rate_normal)

    def c22_rate_conservative():
        pl, drv = _run_30s(FakeItem(codec="hevc"), dict(play_sec=0.0, speed=1.0))
        _assert(pl.seek_count() <= 2,
                "频率上界失败：HEVC 30s 内 seek %d 次 > 2（保守档只入场+预热）"
                % pl.seek_count())
    R.case("㉒ 频率上界：HEVC ≤ 2/30s")(c22_rate_conservative)

    def c26_degraded_not_carry_over():
        """R11：上一条 seek 失败置了 degraded，**换片段后必须重新给机会**。

        不清的后果：新片段被上一条的 `degraded` 永久关掉校正 —— 肉眼表现为
        "切过去之后再也不跟拍"（不报错、不黑屏，极难发现）。
        """
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0); it = FakeItem()
        st = pol.state_for(FakePS(item_id="i1", play_sec=1.0))
        st["item_id"] = "i1"
        st["degraded"] = True
        ps2 = FakePS(item_id="i2", play_sec=50.0)
        got = pol.should_seek(ps2, it, pl, st)
        _assert(got is True,
                "R11 失败：换片段后 degraded 仍残留 ⇒ 新片段永远不 seek")
        _assert(st["degraded"] is False, "R11 失败：degraded 未被清除")
        # 但 in-flight 不能被清（同一个 player 上还没落地的请求）
        st2 = pol.state_for(FakePS(item_id="i3", play_sec=50.0))
        st2["item_id"] = "i2"
        st2["inflight"] = True
        st2["target"] = 40.0
        ps3 = FakePS(item_id="i3", play_sec=60.0)
        _assert(pol.should_seek(ps3, it, pl, st2) is False,
                "R11 失败：换片段时把 in-flight 清掉了（会在同一 player 上叠加请求）")
        _assert(st2["target"] == 40.0, "R11 失败：in-flight 期间不该动 target（否则落地判定失真）")
    R.case("㉖ degraded 不跨片段残留；in-flight/target 不被误清")(c26_degraded_not_carry_over)

    def c24_inflight_beats_throttle():
        """落地慢于节流窗口（1.5s > MIN_INTERVAL）：节流已过期也不得重发。

        ★ 这条专治"in-flight 判断被节流遮住"：
          落地 0.8s < 节流 1.0s 时，in-flight 天然被节流盖住 ⇒ 删掉 in-flight 也测不出来。
        """
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, seek_delay=1.5); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=2.0)
        drv.frame(ps)                            # 入场 seek（落地要 1.5s）
        _assert(pl.seek_count() == 1, "入场应 seek 一次")
        dt = 1.0 / FPS
        for _ in range(int(1.6 * FPS)):           # 跑到 1.6s（跨越节流窗口 1.0s 与落地点 1.5s）
            clk.tick(dt)
            ps.play_sec += dt
            drv.frame(ps)
        ts = [c[0] for c in pl.calls]
        mid = [x for x in ts if MIN_INTERVAL_SEC + 1e-6 < x < 1.5 - 1e-6]
        _assert(not mid,
                "防自激失败：节流已过期但 seek 仍未落地，却又发了 seek（t=%s）" % mid)
    R.case("㉔ 落地 1.5s > 节流窗口 ⇒ 期间仍不重发")(c24_inflight_beats_throttle)

    def c25_switch_while_inflight():
        """换片段时若上一次 seek 未落地：**不重发**（避免请求排队；等落地/超时再说）。"""
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, seek_delay=0.8); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps1 = FakePS(item_id="i1", play_sec=2.0)
        drv.frame(ps1)                            # i1 入场 seek（未落地）
        _assert(pl.seek_count() == 1 and pl.inflight is True, "i1 应发一次且 in-flight")
        clk.tick(0.1)                             # 0.1s 后切到 i2（i1 的 seek 还没落地）
        ps2 = FakePS(item_id="i2", play_sec=10.0)
        drv.frame(ps2)
        _assert(pl.seek_count() == 1,
                "防自激失败：换片段时上一 seek 未落地就重发了（共 %d 次）" % pl.seek_count())
        # 落地后（0.8s）再换回来/继续 ⇒ 允许发
        for _ in range(int(0.9 * FPS)):
            clk.tick(1.0 / FPS)
            drv.frame(ps2)
        _assert(pl.seek_count() >= 2, "落地后应能正常为 i2 发 seek")
    R.case("㉕ 换片段时未落地 ⇒ 不重发；落地后恢复")(c25_switch_while_inflight)

    def c23_speed2_tracking():
        """speed=2 且播放器按 speed 前进 ⇒ 全程不发生 seek（无漂移）。"""
        clk = Clock(); pol = _mk(Policy, clk)
        pl = FakePlayer(pos=0.0, speed=1.0, seek_delay=0.0); it = FakeItem()
        drv = Driver(pol, pl, it, clk)
        ps = FakePS(play_sec=0.0, speed=2.0)
        dt = 1.0 / FPS
        for _ in range(int(10.0 * FPS)):
            ps.play_sec += dt * 2.0
            drv.frame(ps)
            pl.advance(dt * 2.0)
            clk.tick(dt)
        _assert(pl.seek_count() == 1,          # 只有入场那一次
                "speed=2 完美跟随时应只有入场 1 次 seek，实际 %d 次" % pl.seek_count())
        _assert(all(_close(s, 2.0) for s in pl.speed_calls), "speed 必须恒为 2.0")
    R.case("㉓ speed=2 完美跟踪 ⇒ 仅入场 1 次 seek，speed 恒 2.0")(c23_speed2_tracking)

    return R


# ==========================================================================
# 变异验证：故意改坏，测试必须失败
# ==========================================================================
MUTATION_CASES = {
    "drift_uses_target": ("漂移改用『请求目标』（自己跟自己比 ⇒ 永远不触发）",
                          ["⑩ 漂移用 position()（不是请求目标）⇒ True/False 正确",
                           "⑳ 频率上界：DXV ≤ 30/30s 且 ≥1"]),
    "skip_src_ended": ("删掉 src_ended 判断（持帧期间每帧 seek）",
                       ["② src_ended ⇒ False（防自激）",
                        "⑫ src_ended 冻结 10s ⇒ seek 次数 = 0"]),
    "skip_inflight": ("删掉 in-flight 判断（长 GOP 上自激）",
                      ["④ in-flight ⇒ False（防自激）",
                       "㉔ 落地 1.5s > 节流窗口 ⇒ 期间仍不重发",
                       "㉕ 换片段时未落地 ⇒ 不重发；落地后恢复"]),
    "speed_always_one": ("set_speed 恒 1.0（双重计速/主路径被换掉）",
                         ["⑮ set_speed 恒为 ps.speed、无 1.0、位置跟得上",
                          "㉓ speed=2 完美跟踪 ⇒ 仅入场 1 次 seek，speed 恒 2.0"]),
    "no_throttle": ("去掉节流（允许连续 seek）",
                    ["⑨ 节流窗口内 ⇒ False；超出 ⇒ 恢复",
                     "㉑ 频率上界：H.264 ≤ 30/30s"]),
}


#: 源码级变异锚点（对 `src/cue_seek_policy.py` 的精确替换；改不动即报错，不静默跳过）
MUTATION_PATCHES = {
    # 全部是「单行锚点」：只替换判定那一行，不引入换行 ⇒ 不依赖转义
    # （首版用多行锚点，在 shell heredoc 里被吃了两层转义，生成出坏字符串，故改为单行）
    'drift_uses_target': ('    ref = pos',
                '    ref = _f(st.get("target", 0.0))'),
    'skip_src_ended': ('if getattr(ps, "src_ended", False):',
                'if False:  # MUT'),
    'skip_inflight': ('if st.get("inflight", False):',
                'if False:  # MUT'),
    'speed_always_one': ('return max(1e-6, _f(getattr(ps, "speed", 1.0), 1.0))',
                'return 1.0  # MUT'),
    'no_throttle': ('if now - last_req < MIN_INTERVAL_SEC:',
                'if False:  # MUT'),
}


def _import_mutant_policy(Policy, name):
    """把 `Policy` 所在模块的源码**改坏一处**，写成临时模块导入，返回其 Policy 类。

    ★ 为什么要源码级变异而不是"标志位"：标志位只有参考实现认；真实现（`src/cue_seek_policy.py`）
      必须被同样的变异打到，否则 `--impl real --mutations` 会变成"永远绿"的假验证。
    """
    import importlib.util
    import tempfile
    mod = sys.modules.get(getattr(Policy, "__module__", ""), None)
    src_path = getattr(mod, "__file__", "") or ""
    if not src_path or not os.path.isfile(src_path):
        raise RuntimeError("拿不到被测实现的源文件：%r" % src_path)
    text = open(src_path, encoding="utf-8").read()
    old, new = MUTATION_PATCHES[name]
    if old not in text:
        raise RuntimeError("变异锚点未命中（源文件改过？）：%r" % old.strip())
    patched = text.replace(old, new, 1)
    if patched == text:
        raise RuntimeError("变异未生效：%s" % name)
    d = tempfile.mkdtemp(prefix="cuepolicy_mut_")
    path = os.path.join(d, "cue_seek_policy.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    spec = importlib.util.spec_from_file_location("cue_seek_policy__mut_%s" % name, path)
    mut = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mut)
    return getattr(mut, "CueSeekPolicy")


def run_mutations(Policy, verbose=False):
    """逐个变异跑一遍套件，要求**都能被抓到**。返回未抓到的变异名列表。"""
    print("\n=== 变异验证（故意改坏 ⇒ 必须有用例失败）===")
    use_source = Policy is not ReferencePolicy
    print("  变异方式：%s" % ("源码级（改被测模块副本后导入）" if use_source
                              else "标志位（参考实现）"))
    missed = []
    for name, (desc, expect_fail) in MUTATION_CASES.items():
        if use_source:
            try:
                MutPolicy = _import_mutant_policy(Policy, name)
            except Exception as e:                               # noqa: BLE001
                print("  ✗无法变异 %-20s %s（%s）" % (name, desc, e))
                missed.append(name)
                continue
            R = build_suite(MutPolicy, verbose=False)
        else:
            for k in MUTATIONS:
                MUTATIONS[k] = False
            MUTATIONS[name] = True
            R = build_suite(Policy, verbose=False)
        caught = [n for n, _ in R.failed]
        hit = [n for n in caught if any(n.startswith(p.split(" ")[0]) for p in expect_fail)]
        ok = bool(hit)
        print("  %s %-20s %s" % ("✓抓到" if ok else "✗漏掉", name, desc))
        print("        期望失败：%s" % "、".join(expect_fail))
        print("        实际失败：%s（共 %d 条）"
              % ("、".join(caught) if caught else "无", len(caught)))
        if not ok:
            missed.append(name)
    for k in MUTATIONS:
        MUTATIONS[k] = False
    if missed:
        print("\n[!] 以下变异未被抓到 ⇒ 测试不够硬：%s" % "、".join(missed))
    else:
        print("\n全部 %d 个变异都被抓到 ✓（本套件确实能抓错）" % len(MUTATION_CASES))
    return missed


# ==========================================================================
# 入口
# ==========================================================================
def load_policy(impl="auto"):
    """返回 (PolicyClass, 来源说明)。"""
    if impl in ("auto", "real"):
        if SRC not in sys.path:
            sys.path.insert(0, SRC)
        try:
            import cue_seek_policy as real            # Step4 应新增的零 Qt 模块
            cls = getattr(real, "CueSeekPolicy", None)
            if cls is None:
                raise ImportError("cue_seek_policy 里没有 CueSeekPolicy")
            return cls, "src/cue_seek_policy.py（真代码）"
        except Exception as e:                        # noqa: BLE001
            if impl == "real":
                raise SystemExit("--impl real 失败：%s" % e)
            return ReferencePolicy, "本文件内参考实现（Step4 未落地：%s）" % e
    return ReferencePolicy, "本文件内参考实现（--impl file 强制）"


def main(argv=None):
    ap = argparse.ArgumentParser(description="码表漂移校正/防自激/计速 单元自测（零 Qt）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    ap.add_argument("--impl", default="auto", choices=("auto", "file", "real"),
                    help="auto=有真实现就用真实现；file=强制参考实现；real=强制真实现")
    ap.add_argument("--mutations", action="store_true", help="跑变异验证（要求都能被抓到）")
    args = ap.parse_args(argv)

    Policy, origin = load_policy(args.impl)
    print("[cue_should_seek] 被测实现 = %s" % origin)
    print("[cue_should_seek] 常量：drift=%.2fs（保守 %.2fs）/ min_interval=%.1fs / timeout=%.1fs"
          % (DRIFT_SEC, DRIFT_SEC_CONSV, MIN_INTERVAL_SEC, SEEK_TIMEOUT_SEC))

    R = build_suite(Policy, verbose=args.verbose)
    total = len(R.passed) + len(R.failed)
    print("\n用例：%d 通过 / %d 失败（共 %d）" % (len(R.passed), len(R.failed), total))

    missed = run_mutations(Policy, args.verbose) if args.mutations else []
    if R.failed:
        print("\nFAIL 失败用例：%s" % "、".join(n for n, _ in R.failed))
        return 1
    if args.mutations and missed:
        return 2
    print("\nCUE_SHOULD_SEEK_OK  全部用例通过%s"
          % ("；%d 个变异全部被抓到" % len(MUTATION_CASES) if args.mutations else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
