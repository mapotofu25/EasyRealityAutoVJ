# -*- coding: utf-8 -*-
"""回归测试：NDI / Spout 发送失败必须**退避重试**、永不永久停（2026-10-02 任务#14）。

背景（用户实测四轮"另一台机器冻住、本机一切正常、日志全空"的唯一自洽解释）：
  旧实现 `_ndi_send` / `_spout_send` 一旦出错就把 `_ndi_fail` / `_spout_fail` 置成**永久**标志
  ⇒ 发送端整场不再推帧 ⇒ NDI/Spout 接收端**永久冻在最后一帧**，而本机预览/输出窗口/日志全正常。
  本项目铁律是"`send_video` 写失败**不得永久关闭输出**"。

本测试**离屏、不真发 NDI / Spout**：给 `engine` 注入假 sender + 可控时钟，断言：
  (a) 连续失败 3 次后成功 ⇒ **第 4 次真的又调用了发送**（attempts==4、sent==1），且成功后
      `_ndi_fails==0`、`_ndi_retry_at==0`；
  (b) 退避序列实测 == 1→2→4→8→16→30→30…（上限 30s）；
  (c) 失败日志按 30s 限流（不刷屏）、恢复时写一行"恢复 + 中断时长"；
  (d) `running=False` / `ndi_enabled=False` 时**一次都不调用 sender**（原有门控没被破坏）；
  (e) `_ensure()` 返回 False 的路径也走重试（不永久停）；
  (f) `get_ndi()` 构造抛异常的路径也走重试（不永久停）；
  (g) Spout 同构：失败退避重试、成功后恢复，且**不丢 `self._spout`**（源名不反复消失）。
退出码 0 = 全部通过；1 = 有断言失败。

用法（离屏）：QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe -u tools/_test_output_retry.py
"""
import os
import sys
import shutil
import atexit
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PySide6.QtWidgets import QApplication                    # noqa: E402

app = QApplication.instance() or QApplication([])

import engine                                                  # noqa: E402

# 保存"真实实现"引用（后面会临时替换 `_output_health_write` 做捕获/重定向）
_REAL_OUTPUT_WRITE = engine._output_health_write
_REAL_OUTPUT_PATH = engine._output_health_path

FAILS = []


def check(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (("   " + str(extra)) if extra else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- 可控时钟
_real_time = engine.time


class _Clock:
    def __init__(self, t0=1000.0):
        self.t = float(t0)

    def perf_counter(self):
        return self.t

    def strftime(self, fmt):          # `_output_health_write` 会用到
        return _real_time.strftime(fmt)


CLOCK = _Clock()
engine.time = CLOCK


# ---------------------------------------------------------------- 假 sender / 假 ndi_out
class FakeNdi:
    """假 NDI 发送器：`_ensure` 每被调一次算一次"发送尝试"。"""

    def __init__(self, fail_until=0, ensure_false_until=0):
        self.attempts = 0
        self.sent = 0
        self.fail_until = fail_until              # 前 N 次 send_video 抛错
        self.ensure_false_until = ensure_false_until

    def _ensure(self, w, h):
        self.attempts += 1
        return self.attempts > self.ensure_false_until

    def send_video(self, buf, w, h):
        if self.attempts <= self.fail_until:
            raise RuntimeError("注入失败 #%d" % self.attempts)
        self.sent += 1


def install_ndi_module(sender=None, raise_times=0):
    """把假 `ndi_out` 塞进 sys.modules（避免真 import ndi_out / 真装 NDI Runtime）。

    `raise_times`：`get_ndi()` 前这么多次抛异常（模拟发送端构造失败）。"""
    mod = types.ModuleType("ndi_out")
    state = {"calls": 0, "sender": sender or FakeNdi()}

    def get_ndi(name):
        state["calls"] += 1
        if state["calls"] <= raise_times:
            raise RuntimeError("注入：get_ndi 构造失败 #%d" % state["calls"])
        return state["sender"]

    mod.get_ndi = get_ndi
    sys.modules["ndi_out"] = mod
    return state


class FakeSpoutSender:
    def __init__(self, fail_until=0):
        self.attempts = 0
        self.sent = 0
        self.fail_until = fail_until

    def sendImage(self, buf, h, w, fmt, a, b):
        self.attempts += 1
        if self.attempts <= self.fail_until:
            raise RuntimeError("注入 Spout 失败 #%d" % self.attempts)
        self.sent += 1


class FakeAudio:
    def snapshot(self):
        return {}


def make_engine(ndi_enabled=True, spout_enabled=True, running=True):
    cfg = {"output": {"ndi_enabled": ndi_enabled, "spout_enabled": spout_enabled,
                      "ndi_name": "Test", "spout_name": "Test"}}
    eng = engine.AutoVJEngine(cfg, FakeAudio(), {})
    eng.set_canvas_size(64, 64)
    eng.running = running
    return eng


# ================================================================
print("(a) 连续失败 3 次后成功 ⇒ 第 4 次真的又发送了；成功后退避/计数归零")
fake = FakeNdi(fail_until=3)
install_ndi_module(sender=fake)
eng = make_engine()
CLOCK.t = 1000.0
eng._ndi_send()                                    # 尝试1 → 抛错 → fails=1, retry_at=1001
check("尝试1失败", fake.attempts == 1 and eng._ndi_fails == 1,
      "attempts=%d fails=%d retry_at=%.1f" % (fake.attempts, eng._ndi_fails, eng._ndi_retry_at))
CLOCK.t = 1000.5
eng._ndi_send()                                    # 退避窗口内：不该尝试
check("退避窗口内不再尝试", fake.attempts == 1, "attempts=%d" % fake.attempts)
CLOCK.t = 1001.0
eng._ndi_send()                                    # 尝试2 → 抛错 → fails=2, retry_at=1003
CLOCK.t = 1003.0
eng._ndi_send()                                    # 尝试3 → 抛错 → fails=3, retry_at=1007
check("前 3 次都失败", fake.attempts == 3 and eng._ndi_fails == 3,
      "attempts=%d fails=%d" % (fake.attempts, eng._ndi_fails))
CLOCK.t = 1007.0
eng._ndi_send()                                    # 尝试4 → 成功
check("★ 第 4 次真的又调用了发送（旧代码此时永久停在 0）", fake.attempts == 4,
      "attempts=%d" % fake.attempts)
check("第 4 次确实发送成功", fake.sent == 1, "sent=%d" % fake.sent)
check("成功后 `_ndi_fails` 归零", eng._ndi_fails == 0, "fails=%d" % eng._ndi_fails)
check("成功后 `_ndi_retry_at` 归零", eng._ndi_retry_at == 0.0, "retry_at=%s" % eng._ndi_retry_at)
check("成功后 `_ndi_fail` 复位为 False", eng._ndi_fail is False, "_ndi_fail=%s" % eng._ndi_fail)

# ================================================================
print("\n(b) 退避序列实测（一直失败）")
fake = FakeNdi(fail_until=10 ** 9)
install_ndi_module(sender=fake)
eng = make_engine()
CLOCK.t = 1000.0
seq = []
for _ in range(7):
    if eng._ndi_retry_at > CLOCK.t:
        CLOCK.t = eng._ndi_retry_at              # 越过退避窗口再试
    eng._ndi_send()
    seq.append(round(eng._ndi_retry_at - CLOCK.t, 3))
print("     退避序列 = %s" % seq)
check("退避序列 == 1→2→4→8→16→30→30", seq == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0], str(seq))

# ================================================================
print("\n(c) 失败日志 30s 限流 + 恢复日志（含中断时长）")
captured = []
engine._output_health_write = lambda line: captured.append((CLOCK.t, line))
fake = FakeNdi(fail_until=10 ** 9)
install_ndi_module(sender=fake)
eng = make_engine()
CLOCK.t = 1000.0
captured.clear()
for _ in range(90):                                # 从 1000s 走到 1090s，每秒 tick 一次
    eng._ndi_send()
    CLOCK.t += 1.0
fails = [(t, l) for t, l in captured if "发送失败" in l]
gaps = [round(fails[i + 1][0] - fails[i][0], 1) for i in range(len(fails) - 1)]
print("     90s 内失败日志 %d 行，时间点=%s，间隔=%s"
      % (len(fails), [t for t, _ in fails], gaps))
check("失败日志按 30s 限流（90s 内 2~4 行）", 2 <= len(fails) <= 4, "行数=%d" % len(fails))
check("相邻失败日志间隔 ≥30s", all(g >= 30.0 for g in gaps), "gaps=%s" % gaps)
check("失败日志含连续次数", any("连续第" in l for _, l in fails), str(fails[:1])[:90])
# ---- 恢复 ----
fake.fail_until = 0                                # 故障消失
captured.clear()
CLOCK.t = eng._ndi_retry_at                        # 到点再试 → 成功
eng._ndi_send()
rec = [l for _, l in captured if "恢复" in l]
check("恢复时写一行「恢复」日志", len(rec) == 1, str(rec)[:90])
check("恢复日志含「中断」时长", bool(rec) and "中断" in rec[0], str(rec)[:90])
print("     恢复行：%s" % (rec[:1] or ["（无）"]))

# ================================================================
print("\n(d) 门控：running=False / ndi_enabled=False ⇒ 一次都不调用 sender")
fake = FakeNdi(fail_until=10 ** 9)
install_ndi_module(sender=fake)
eng = make_engine(running=False)
CLOCK.t = 2000.0
for _ in range(10):
    eng._ndi_send()
    CLOCK.t += 1.0
check("running=False ⇒ sender 0 次尝试", fake.attempts == 0, "attempts=%d" % fake.attempts)
fake2 = FakeNdi()
install_ndi_module(sender=fake2)
eng = make_engine(ndi_enabled=False)
CLOCK.t = 3000.0
for _ in range(10):
    eng._ndi_send()
    CLOCK.t += 1.0
check("ndi_enabled=False ⇒ sender 0 次尝试", fake2.attempts == 0, "attempts=%d" % fake2.attempts)

# ================================================================
print("\n(e) `_ensure()` 返回 False 也走重试（不永久停）")
fake = FakeNdi(ensure_false_until=2)
install_ndi_module(sender=fake)
eng = make_engine()
CLOCK.t = 4000.0
eng._ndi_send()                                    # attempts=1 → ensure False → 失败
check("ensure False ⇒ 记为失败", eng._ndi_fails == 1, "fails=%d" % eng._ndi_fails)
CLOCK.t = eng._ndi_retry_at
eng._ndi_send()                                    # attempts=2 → ensure False → 失败
CLOCK.t = eng._ndi_retry_at
eng._ndi_send()                                    # attempts=3 → ensure True → 发送成功
check("ensure 恢复 ⇒ 第 3 次发送成功（未永久停）", fake.sent == 1, "sent=%d attempts=%d" % (fake.sent, fake.attempts))
check("成功后 fails 归零", eng._ndi_fails == 0, "fails=%d" % eng._ndi_fails)

# ================================================================
print("\n(f) `get_ndi()` 构造抛异常也走重试（不永久停）")
fake = FakeNdi()
st = install_ndi_module(sender=fake, raise_times=2)   # 前两次 get_ndi 抛错
eng = make_engine()
CLOCK.t = 5000.0
eng._ndi_send()                                    # get_ndi 抛错 → 失败
check("构造失败 ⇒ 记为失败、且 `_ndi` 仍为 None", eng._ndi_fails == 1 and eng._ndi is None,
      "fails=%d _ndi=%s" % (eng._ndi_fails, eng._ndi))
CLOCK.t = eng._ndi_retry_at
eng._ndi_send()                                    # get_ndi 再抛错 → 失败
CLOCK.t = eng._ndi_retry_at
eng._ndi_send()                                    # get_ndi 成功 → 构造 + 发送成功
check("构造恢复 ⇒ 最终发送成功（未永久停）", fake.sent == 1 and st["calls"] == 3,
      "sent=%d get_ndi_calls=%d" % (fake.sent, st["calls"]))

# ================================================================
print("\n(g) Spout 同构：失败退避重试 + 恢复，且不丢 `self._spout`")
fake_sp = FakeSpoutSender(fail_until=2)
eng = make_engine()
eng._spout = (fake_sp, 0)                          # 预置假 sender（避免真 import SpoutGL）
CLOCK.t = 6000.0
eng._spout_send()                                  # 尝试1 → 抛错 → fails=1
check("Spout 失败计入退避", eng._spout_fails == 1, "fails=%d" % eng._spout_fails)
check("Spout 失败时**不丢** `self._spout`", eng._spout is not None and eng._spout[0] is fake_sp,
      "_spout=%s" % (eng._spout,))
CLOCK.t = eng._spout_retry_at
eng._spout_send()                                  # 尝试2 → 抛错
CLOCK.t = eng._spout_retry_at
eng._spout_send()                                  # 尝试3 → 成功
check("Spout 第 3 次发送成功（未永久停）", fake_sp.sent == 1, "sent=%d attempts=%d" % (fake_sp.sent, fake_sp.attempts))
check("Spout 成功后 fails 归零", eng._spout_fails == 0, "fails=%d" % eng._spout_fails)
check("Spout 成功同一 sender（源名不反复消失）", eng._spout[0] is fake_sp, "_spout=%s" % (eng._spout,))

# ================================================================
print("\n(h) 取证日志文件路径与真实写入（tempdir，不碰用户目录）")
p = os.path.join(tempfile.mkdtemp(prefix="autovj_out_health_"), "output_health.log")
atexit.register(shutil.rmtree, os.path.dirname(p), True)   # 新铁律：mkdtemp 必须清理
engine._output_health_path = lambda: p
engine._output_health_write = _REAL_OUTPUT_WRITE   # 用真实实现写文件
engine._output_health_write("NDI 发送失败（连续第 1 次，退避 1s 后再试）：RuntimeError: 测试")
engine._output_health_write("NDI 发送恢复（中断 3.2s）")
engine._output_health_path = _REAL_OUTPUT_PATH
engine._output_health_write = _REAL_OUTPUT_WRITE
txt = open(p, encoding="utf-8").read()
check("写到 output_health.log 且有时间戳前缀", "NDI 发送失败" in txt and txt.strip().startswith("["),
      txt.splitlines()[:1])
check("恢复行也落盘", "中断 3.2s" in txt, txt.splitlines()[-1:] if txt else [])

# ================================================================
engine.time = _real_time
sys.modules.pop("ndi_out", None)
print()
print("结果：%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项 ✗ %s" % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)
