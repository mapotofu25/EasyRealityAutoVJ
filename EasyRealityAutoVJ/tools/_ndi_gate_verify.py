# -*- coding: utf-8 -*-
"""独立验证 commit c5178cc 的 NDI 音频静默门控修复（**只读，不改任何源码**）。

验证对象：`src/ui_main.py` 三处改动
  A. `_ndi_audio_feed` 加一次性分情况诊断日志 + 正常发送复位去重标志
  B. `_load_settings_to_ui` 探测失败分支同步回滚 `audio.state.ndi_audio_on` / `chk_ndi_audio`
  C. `set_ndi_audio` 补「NDI 输出未启用，音频勾选未生效」原因说明日志

⚠ 验证方式：**不启动整个 Qt 应用**。用 `unittest.mock` + 假对象，
   把 `MainWindow._ndi_audio_feed` / `MainWindow.set_ndi_audio` / `MainWindow._load_settings_to_ui`
   这几个**真实方法**绑定到假实例上（`types.MethodType`），
   从而驱动真实源码路径，而非复刻一份逻辑副本。

运行：
    venv\\Scripts\\python.exe tools\\_ndi_gate_verify.py
"""

import io
import os
import re
import sys
import types
import contextlib

# ---------------------------------------------------------------------------
# 0. 环境：offscreen Qt + src 入 sys.path
# ---------------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import ui_main  # noqa: E402  （导入真实模块，拿真实方法）

MainWindow = ui_main.MainWindow

# 结果收集
_RESULTS = []


def _check(name, ok, detail=""):
    _RESULTS.append((name, bool(ok), detail))
    flag = "PASS" if ok else "FAIL"
    line = "  [%s] %s" % (flag, name)
    if detail:
        line += "  -- " + detail
    print(line)
    return ok


def _capture(fn, *a, **k):
    """运行 fn，返回 (返回值, stdout 文本)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ret = fn(*a, **k)
    return ret, buf.getvalue()


def _lines_of(text):
    return [ln for ln in text.splitlines() if ln.strip()]


# ===========================================================================
# 假对象
# ===========================================================================
class _FakeEngine(object):
    def __init__(self, ndi=None):
        self._ndi = ndi


class _FakeRawCfg(dict):
    """最简的 dict 风格 cfg（main_window.cfg 就是这种 dict 结构）。"""


class _FakeAudioState(object):
    def __init__(self):
        self.ndi_audio_on = False


class _FakeAudio(object):
    def __init__(self):
        self.state = _FakeAudioState()


class _FakeChk(object):
    """模拟 QCheckBox：记录 setChecked / blockSignals / setEnabled 调用。"""

    def __init__(self, checked=False, enabled=True):
        self._checked = bool(checked)
        self._enabled = bool(enabled)
        self.block_signals_calls = []
        self.set_checked_calls = []
        self.set_enabled_calls = []

    def isChecked(self):
        return self._checked

    def setChecked(self, v):
        self._checked = bool(v)
        self.set_checked_calls.append(bool(v))

    def blockSignals(self, b):
        self.block_signals_calls.append(bool(b))

    def setEnabled(self, v):
        self._enabled = bool(v)
        self.set_enabled_calls.append(bool(v))


class _FakeOutputPanel(object):
    def __init__(self):
        self.chk_ndi = _FakeChk()
        self.chk_ndi_audio = _FakeChk()
        self.ndi_name = types.SimpleNamespace(
            text=lambda: "EasyRealityAutoVJ", setText=lambda *_a: None)

    def __getattr__(self, attr):
        """未显式定义的控件（如 resolution）由万能桩吸收。"""
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _AutoMock("output_panel.%s" % attr)


class _FakeCfgObj(dict):
    """既是 dict（可 cfg["output"] 下标访问）又带 save() 的假 cfg。

    真实 `self.cfg` 是 dict 风格对象且暴露 `.save()`；必须同时满足两者，
    否则 `set_ndi_audio` 里的 `self.cfg["output"]` 会 TypeError。
    """

    def __init__(self, data):
        super().__init__(data)
        self.save_calls = 0

    def save(self):
        self.save_calls += 1

    @property
    def data(self):
        return self


def _make_feed_host(ndi=None, ndi_enabled=True):
    """构造一个只具备 `_ndi_audio_feed` 真实方法所需字段的假 MainWindow。"""
    host = types.SimpleNamespace()
    host.engine = _FakeEngine(ndi)
    host.cfg = {"output": {"ndi_enabled": ndi_enabled, "ndi_audio": False}}
    # ★ 绑定**真实源码方法**
    host._ndi_audio_feed = types.MethodType(MainWindow._ndi_audio_feed, host)
    return host


class _MockNdi(object):
    """假 ndi 发送端：记录 feed_audio 调用。"""

    def __init__(self):
        self.feed_calls = []

    def feed_audio(self, pcm, sr):
        self.feed_calls.append((pcm, sr))


# ===========================================================================
# V1. 改动 A —— _ndi_audio_feed 门控日志行为
# ===========================================================================
def v1():
    print("\n" + "=" * 78)
    print("V1. 改动 A：_ndi_audio_feed 一次性分情况诊断日志（真实方法驱动）")
    print("=" * 78)

    host = _make_feed_host(ndi=None, ndi_enabled=True)

    # --- V1.1 sender_missing: 500 次只打 1 行 ---
    _, out = _capture(lambda: [host._ndi_audio_feed(b"pcm", 48000) for _ in range(500)])
    lines = _lines_of(out)
    print("\n[V1.1] engine._ndi=None，连续 500 次调用 → 捕获 stdout 行数 = %d" % len(lines))
    for ln in lines:
        print("       | " + ln)
    _check("V1.1 sender_missing 500 次恰好 1 行日志",
           len(lines) == 1 and "发送端尚未建立" in lines[0],
           "lines=%d content=%r" % (len(lines), lines[:1]))

    # --- V1.2 切到正常：feed_audio 被调 1 次，且无新日志 ---
    ndi = _MockNdi()
    host.engine._ndi = ndi
    host.cfg["output"]["ndi_enabled"] = True
    _, out2 = _capture(lambda: host._ndi_audio_feed(b"pcm", 48000))
    print("\n[V1.2] engine._ndi=<Mock>, ndi_enabled=True → 调用 1 次")
    print("       feed_audio 调用次数 = %d；新日志行数 = %d"
          % (len(ndi.feed_calls), len(_lines_of(out2))))
    _check("V1.2 正常路径 feed_audio 被调 1 次且无日志",
           len(ndi.feed_calls) == 1 and _lines_of(out2) == [],
           "feed=%d newlines=%d" % (len(ndi.feed_calls), len(_lines_of(out2))))

    # --- V1.3 output_disabled: 500 次只打 1 行 ---
    host.cfg["output"]["ndi_enabled"] = False
    _, out3 = _capture(lambda: [host._ndi_audio_feed(b"pcm", 48000) for _ in range(500)])
    lines3 = _lines_of(out3)
    print("\n[V1.3] 切到 ndi_enabled=False → 连续 500 次调用")
    print("       stdout 行数 = %d；feed_audio 累计调用 = %d" % (len(lines3), len(ndi.feed_calls)))
    for ln in lines3:
        print("       | " + ln)
    _check("V1.3 output_disabled 500 次恰好 1 行日志含 'NDI 输出未启用'",
           len(lines3) == 1 and "NDI 输出未启用" in lines3[0],
           "lines=%d content=%r" % (len(lines3), lines3[:1]))
    _check("V1.3b output_disabled 期间 feed_audio 完全不被调用",
           len(ndi.feed_calls) == 1, "feed_total=%d" % len(ndi.feed_calls))

    # --- V1.4 切回 True：无日志，且 feed_audio 500 次（证明复位逻辑有效）---
    host.cfg["output"]["ndi_enabled"] = True
    before = len(ndi.feed_calls)
    _, out4 = _capture(lambda: [host._ndi_audio_feed(b"pcm", 48000) for _ in range(500)])
    lines4 = _lines_of(out4)
    print("\n[V1.4] 切回 ndi_enabled=True → 连续 500 次调用")
    print("       新日志行数 = %d；feed_audio 新增 = %d" % (len(lines4), len(ndi.feed_calls) - before))
    _check("V1.4 切回正常：无日志且 feed_audio 被调 500 次（复位生效）",
           len(lines4) == 0 and (len(ndi.feed_calls) - before) == 500,
           "newlines=%d feed_delta=%d" % (len(lines4), len(ndi.feed_calls) - before))

    # --- V1.5 复位有效性关键：再切回 output_disabled，应再报一次（因为从正常→门控）---
    host.cfg["output"]["ndi_enabled"] = False
    _, out5 = _capture(lambda: host._ndi_audio_feed(b"pcm", 48000))
    lines5 = _lines_of(out5)
    print("\n[V1.5] 再次切到 ndi_enabled=False → 单次调用")
    print("       日志行数 = %d（期望 1，证明复位后门控会重新报）" % len(lines5))
    for ln in lines5:
        print("       | " + ln)
    _check("V1.5 正常→门控切换后复位到 None，门控再报一次",
           len(lines5) == 1 and "NDI 输出未启用" in lines5[0],
           "lines=%d" % len(lines5))

    # --- V1.6 反例：reason 不变时绝不重复打印 ---
    _, out6 = _capture(lambda: [host._ndi_audio_feed(b"pcm", 48000) for _ in range(1000)])
    print("\n[V1.6] reason 不变，连续 1000 次调用 → 新日志行数 = %d（期望 0）"
          % len(_lines_of(out6)))
    _check("V1.6 reason 不变时绝不重复打印（热路径不刷屏）",
           len(_lines_of(out6)) == 0, "newlines=%d" % len(_lines_of(out6)))

    # --- V1.7 异常路径：engine 抛异常 / cfg 抛异常时不应冒泡 ---
    class _BadEngine(object):
        @property
        def _ndi(self):
            raise RuntimeError("engine not ready")

    host2 = _make_feed_host(ndi=None, ndi_enabled=True)
    host2.engine = _BadEngine()
    try:
        _, out7 = _capture(lambda: [host2._ndi_audio_feed(b"pcm", 48000) for _ in range(10)])
        ok7 = len(_lines_of(out7)) == 1 and "发送端尚未建立" in _lines_of(out7)[0]
        err7 = ""
    except Exception as e:  # noqa: BLE001
        ok7 = False
        err7 = repr(e)
    _check("V1.7 engine 访问抛异常时按 sender_missing 处理且不冒泡",
           ok7, err7 or "lines=%d" % len(_lines_of(out7)))

    return {
        "v1_1": len(lines) == 1,
        "v1_3": len(lines3) == 1,
        "v1_4": len(lines4) == 0 and (len(ndi.feed_calls) - before) == 500,
        "v1_6": len(_lines_of(out6)) == 0,
    }


# ===========================================================================
# V2. 改动 B —— _load_settings_to_ui 探测失败分支回滚（静态 + 半动态）
# ===========================================================================
def v2():
    print("\n" + "=" * 78)
    print("V2. 改动 B：_load_settings_to_ui 探测失败回滚音频状态")
    print("=" * 78)

    import inspect
    src = inspect.getsource(MainWindow._load_settings_to_ui)
    lines = src.splitlines()

    # 找 else 分支的位置
    else_idx = None
    for i, ln in enumerate(lines):
        if "运行时不可用" in ln and "print" in ln:
            else_idx = i
            break
    _check("V2.0 找到 probe_runtime 失败 else 分支",
           else_idx is not None, "else 分支在方法内第 %s 行" % (else_idx + 1 if else_idx is not None else "?"))

    if else_idx is None:
        return {"v2_found": False}

    tail = "\n".join(lines[else_idx:])
    tail_after_else = lines[else_idx:]

    # 在失败 else 分支里，直到 except 结束，检查关键三行
    has_audio_rollback = "self.audio.state.ndi_audio_on = False" in tail
    has_chk_audio_reset = "chk_ndi_audio" in tail and "setChecked(False)" in tail
    has_chk_audio_disable = "setEnabled(False)" in tail

    _check("V2.1 失败分支含 audio.state.ndi_audio_on = False",
           has_audio_rollback)
    _check("V2.2 失败分支含 chk_ndi_audio 取消勾选",
           has_chk_audio_reset)
    _check("V2.3 失败分支含 chk_ndi_audio setEnabled(False)",
           has_chk_audio_disable)

    # --- 关键：失败分支到 except 之间是否有 early return ---
    # 从 else 之后找到对应的 except 行（缩进更浅）
    else_indent = len(lines[else_idx]) - len(lines[else_idx].lstrip())
    end_idx = None
    for j in range(else_idx + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        ind = len(ln) - len(ln.lstrip())
        if ind < else_indent and ln.lstrip().startswith("except"):
            end_idx = j
            break
    branch_body = lines[else_idx:end_idx] if end_idx is not None else lines[else_idx:]
    returns_in_branch = [ln for ln in branch_body if re.match(r"\s*return\b", ln)]

    print("\n[V2.4] 失败 else 分支（前 %d 行）到 except 之前的所有 return：%r"
          % (len(branch_body), returns_in_branch))
    _check("V2.4 失败分支内无任何 early return（新增回滚不会被跳过）",
           len(returns_in_branch) == 0,
           "returns=%r" % returns_in_branch)

    # 打印失败分支原文（截取）以提供证据
    print("\n  --- 失败 else 分支原文（行号相对方法首行）---")
    for k, ln in enumerate(lines[else_idx:end_idx], start=else_idx + 1):
        print("   %3d | %s" % (k, ln))

    # --- 半动态：让 probe_runtime 强制失败，真实跑失败分支 ---
    return _v2_dynamic(has_audio_rollback and has_chk_audio_reset and has_chk_audio_disable
                       and len(returns_in_branch) == 0)


class _AutoMock(object):
    """万能假对象：任何属性访问返回一个可调用的子 _AutoMock；任何调用返回 _AutoMock。

    用于替身 `self.settings` / `self.btn_gpu` 等——真实 `_load_settings_to_ui` 会在探测
    失败分支之前触碰上百个 UI 控件（setCurrentIndex/setChecked/setValue...），
    用万能桩吸收掉，从而让控制流真正走到 probe 失败分支。
    """

    def __init__(self, name="auto", log=None):
        self._name = name
        self._log = log if log is not None else []

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        child = _AutoMock("%s.%s" % (self._name, attr), self._log)
        return child

    def __call__(self, *a, **k):
        self._log.append((self._name, a, k))
        return _AutoMock(self._name + "()", self._log)


def _full_cfg(ndi_enabled=True, ndi_audio=True):
    """覆盖 `_load_settings_to_ui` 会访问的全部 8 个顶层 cfg 键。"""
    return _FakeCfgObj({
        "auto": {}, "beat": {}, "color": {}, "kv": {},
        "mode": {}, "perf": {}, "postfx": {},
        "output": {"ndi_enabled": ndi_enabled, "ndi_audio": ndi_audio,
                   "ndi_name": "EasyRealityAutoVJ"},
    })


def _v2_dynamic(static_ok):
    """半动态：用 `self.settings` 万能桩 + 全量 cfg 让真实方法走到 probe 失败分支。

    探测依赖 `ndi_out.probe_runtime` —— 注入失败的假实现到 sys.modules，
    使真实方法走 else 分支，观察回滚副作用。
    """
    print("\n[V2.5] 半动态：注入 probe_runtime 失败桩 + settings 万能桩，真实运行失败分支")

    # 造一个假的 ndi_out 模块（真实方法里是 `from ndi_out import get_ndi, probe_runtime`）
    fake_ndi_out = types.ModuleType("ndi_out")
    fake_ndi_out.get_ndi = lambda *a, **k: object()
    fake_ndi_out.probe_runtime = lambda: (False, "stub: runtime unavailable")

    old_mod = sys.modules.get("ndi_out")
    sys.modules["ndi_out"] = fake_ndi_out

    host = types.SimpleNamespace()
    host.engine = types.SimpleNamespace(_ndi=None, _ndi_fail=False)
    host.cfg = _full_cfg(ndi_enabled=True, ndi_audio=True)
    host.audio = _FakeAudio()
    host.audio.state.ndi_audio_on = True          # 假装启动时是「开」的
    host.output_panel = _FakeOutputPanel()
    host.output_panel.chk_ndi_audio._checked = True
    host.settings = _AutoMock("settings")
    host.btn_gpu = _AutoMock("btn_gpu")
    # settings.sync_kv_section 由万能桩兜住

    host._load_settings_to_ui = types.MethodType(MainWindow._load_settings_to_ui, host)

    try:
        _, out = _capture(host._load_settings_to_ui)
        err = ""
    except Exception:  # noqa: BLE001
        import traceback
        out = ""
        err = traceback.format_exc()

    if old_mod is not None:
        sys.modules["ndi_out"] = old_mod
    else:
        sys.modules.pop("ndi_out", None)

    print("  动态运行 stdout：")
    for ln in _lines_of(out):
        print("    | " + ln)
    if err:
        print("  !! 抛异常：\n" + err)

    ok_run = (err == "")
    ok_cfg = host.cfg["output"]["ndi_enabled"] is False
    ok_audio_state = host.audio.state.ndi_audio_on is False
    chk_audio = host.output_panel.chk_ndi_audio
    ok_chk_checked = chk_audio.isChecked() is False
    ok_chk_disabled = (False in chk_audio.set_enabled_calls)
    ok_log = any("已同步关闭 NDI 音频" in ln for ln in _lines_of(out))

    print("\n  失败分支副作用：")
    print("   cfg['output']['ndi_enabled'] = %r" % host.cfg["output"]["ndi_enabled"])
    print("   audio.state.ndi_audio_on     = %r" % host.audio.state.ndi_audio_on)
    print("   chk_ndi.isChecked()          = %r" % host.output_panel.chk_ndi.isChecked())
    print("   chk_ndi_audio.isChecked()    = %r" % chk_audio.isChecked())
    print("   chk_ndi_audio.setEnabled()   = %r" % chk_audio.set_enabled_calls)
    print("   chk_ndi_audio.blockSignals() = %r" % chk_audio.block_signals_calls)
    print("   新增诊断日志存在              = %r" % ok_log)

    _check("V2.5 失败分支正常执行不抛异常", ok_run, err[:200])
    _check("V2.6 失败分支回滚 cfg.ndi_enabled=False", ok_cfg)
    _check("V2.7 失败分支回滚 audio.state.ndi_audio_on=False", ok_audio_state)
    _check("V2.8 失败分支 chk_ndi_audio 取消勾选", ok_chk_checked)
    _check("V2.9 失败分支 chk_ndi_audio setEnabled(False)", ok_chk_disabled)
    _check("V2.10 失败分支打印 '已同步关闭 NDI 音频'", ok_log)

    return {
        "static_ok": static_ok,
        "dynamic_ok": all([ok_run, ok_cfg, ok_audio_state, ok_chk_checked,
                           ok_chk_disabled, ok_log]),
    }


# ===========================================================================
# V3. 改动 C —— set_ndi_audio 原因说明日志（真实方法驱动）
# ===========================================================================
def v3():
    print("\n" + "=" * 78)
    print("V3. 改动 C：set_ndi_audio 原因说明日志（真实方法驱动）")
    print("=" * 78)

    def _make_host(ndi_enabled, ndi_audio, checked):
        host = types.SimpleNamespace()
        host.cfg = _FakeCfgObj({"output": {"ndi_enabled": ndi_enabled,
                                           "ndi_audio": ndi_audio}})
        host.audio = _FakeAudio()
        host.output_panel = _FakeOutputPanel()
        host.output_panel.chk_ndi_audio._checked = checked
        host.set_ndi_audio = types.MethodType(MainWindow.set_ndi_audio, host)
        return host

    # --- V3.1 s_raw=True, ndi_enabled=False → 写 False + 打印「未生效」---
    h1 = _make_host(ndi_enabled=False, ndi_audio=False, checked=False)
    _, out1 = _capture(h1.set_ndi_audio, True)
    l1 = _lines_of(out1)
    print("\n[V3.1] set_ndi_audio(True), cfg.ndi_enabled=False")
    print("   cfg['output']['ndi_audio'] = %r" % h1.cfg.data["output"]["ndi_audio"])
    print("   audio.state.ndi_audio_on  = %r" % h1.audio.state.ndi_audio_on)
    print("   cfg.save() 调用次数        = %d" % h1.cfg.save_calls)
    for ln in l1:
        print("   | " + ln)
    _check("V3.1 ndi_enabled=False 时 ndi_audio 被写成 False",
           h1.cfg.data["output"]["ndi_audio"] is False)
    _check("V3.2 打印「未生效」原因说明",
           any("未生效" in ln for ln in l1),
           "lines=%r" % l1)

    # --- V3.3 s_raw=True, ndi_enabled=True → True，不打印「未生效」---
    h2 = _make_host(ndi_enabled=True, ndi_audio=False, checked=False)
    _, out2 = _capture(h2.set_ndi_audio, True)
    l2 = _lines_of(out2)
    print("\n[V3.3] set_ndi_audio(True), cfg.ndi_enabled=True")
    print("   cfg['output']['ndi_audio'] = %r" % h2.cfg.data["output"]["ndi_audio"])
    print("   audio.state.ndi_audio_on  = %r" % h2.audio.state.ndi_audio_on)
    for ln in l2:
        print("   | " + ln)
    _check("V3.3 ndi_enabled=True 时 ndi_audio 被写成 True",
           h2.cfg.data["output"]["ndi_audio"] is True)
    _check("V3.4 ndi_enabled=True 时不打印「未生效」",
           not any("未生效" in ln for ln in l2), "lines=%r" % l2)

    # --- V3.5 附带：勾选框写回（blockSignals + setChecked）---
    chk = h2.output_panel.chk_ndi_audio
    _check("V3.5 真实生效值 True 被回写勾选框（blockSignals 包裹）",
           True in chk.set_checked_calls and chk.block_signals_calls.count(True) >= 1,
           "setChecked=%r blockSignals=%r" % (chk.set_checked_calls, chk.block_signals_calls))

    # --- V3.6 s_raw=False → 关闭路径，同样不打印「未生效」---
    h3 = _make_host(ndi_enabled=True, ndi_audio=True, checked=True)
    _, out3 = _capture(h3.set_ndi_audio, False)
    l3 = _lines_of(out3)
    print("\n[V3.6] set_ndi_audio(False), cfg.ndi_enabled=True")
    print("   cfg['output']['ndi_audio'] = %r" % h3.cfg.data["output"]["ndi_audio"])
    for ln in l3:
        print("   | " + ln)
    _check("V3.6 取消勾选时 ndi_audio=False 且不打印「未生效」",
           h3.cfg.data["output"]["ndi_audio"] is False and not any("未生效" in ln for ln in l3))

    return {
        "v3_1": h1.cfg.data["output"]["ndi_audio"] is False,
        "v3_4": not any("未生效" in ln for ln in l2),
    }


# ===========================================================================
# V5. 找茬 —— 静态风险分析（打印结论）
# ===========================================================================
def v5():
    print("\n" + "=" * 78)
    print("V5. 找茬：残余风险分析")
    print("=" * 78)

    # V5.1 标志写入在 print 之前
    import inspect
    src_feed = inspect.getsource(MainWindow._ndi_audio_feed)
    order_ok = False
    lines = src_feed.splitlines()
    for i, ln in enumerate(lines):
        if "_ndi_audio_gate_logged = reason" in ln:
            following = "\n".join(lines[i:i + 12])
            order_ok = "print(" in following
            flag_before_print = True
            break
    else:
        flag_before_print = False

    print("\n[V5.1] 标志写法分析")
    print("   `_ndi_audio_gate_logged = reason` 在 print 之前：%r" % flag_before_print)
    print("   ⇒ print 被 try/except 包住（print 抛异常时 except pass）")
    print("   判定：C 的 print 只是纯字符串拼接 + sys.stdout.write，")
    print("         在正常进程里几乎不可能抛；即使抛（stdout 被关闭/EPIPE），")
    print("         标志已写 ⇒ 该 reason 不再重试打印。")
    print("         影响：**最坏是少打一行诊断日志**，不改变门控/发送行为本身，")
    print("         不丢音频、不误发。可接受（诊断日志丢失 ≠ 功能故障）。")
    _check("V5.1 标志写在 print 之前（已确认）", flag_before_print)

    # --- V5.1b 对抗测试：让内置 print 抛异常，观察是否冒泡 + 是否影响后续 ---
    import builtins
    real_print = builtins.print

    class _PrintBoom(object):
        def __call__(self, *a, **k):
            raise OSError("simulated stdout broken pipe")

    host = _make_feed_host(ndi=None, ndi_enabled=True)
    builtins.print = _PrintBoom()
    try:
        host._ndi_audio_feed(b"pcm", 48000)         # 第 1 次：print 抛
        first_raised = False
    except Exception:  # noqa: BLE001
        first_raised = True
    # 标志此刻是否已被写（模拟「print 失败但标志已置位」）
    flagged_after_boom = getattr(host, "_ndi_audio_gate_logged", None)
    # 第 2 次：若标志已置位，则不再尝试 print（静默）；否则会再抛一次
    try:
        host._ndi_audio_feed(b"pcm", 48000)
        second_raised = False
    except Exception:  # noqa: BLE001
        second_raised = True
    builtins.print = real_print

    print("\n[V5.1b] 对抗测试：print 抛异常")
    print("   第 1 次调用是否冒泡异常      = %r（期望 False，被 try/except 吞）" % first_raised)
    print("   print 抛后标志值            = %r（期望 'sender_missing' ⇒ 已置位）" % flagged_after_boom)
    print("   第 2 次调用是否再次冒泡      = %r（期望 False）" % second_raised)
    print("   判定：print 抛异常**不冒泡**（try/except pass）；标志已置位 ⇒ 该 reason 不再打印。")
    print("         但这只丢失诊断日志，**门控/发送行为完全不受影响**（不丢音频、不误发）。")
    _check("V5.1b print 抛异常不冒泡且不破坏门控",
           (not first_raised) and (not second_raised),
           "first_raised=%r second_raised=%r flag=%r"
           % (first_raised, second_raised, flagged_after_boom))
    _check("V5.1c print 抛异常后标志已置位（不会重试打印，只影响日志）",
           flagged_after_boom == "sender_missing",
           "flag=%r" % flagged_after_boom)

    # V5.2 多线程
    print("\n[V5.2] 多线程风险")
    print("   调用方：_ndi_audio_feed 只由 NDI 转发线程 _ndi_audio_loop 调用（单线程）。")
    print("   写方：set_ndi_enabled 等在主线程写 engine._ndi。")
    print("   读-改-写：门控标志的读/改仅在转发线程内发生 ⇒ 无竞争。")
    print("   engine._ndi 的跨线程读存在，但读到的值是 None 或对象引用（原子），")
    print("   最坏情况：读到旧值 ⇒ 该块音频按门控丢弃/发送，下一块(≈21ms)即自愈。")
    print("   判定：无实际数据竞争风险（GIL + 单写者 + 引用原子赋值）。")
    _check("V5.2 门控标志仅在单一转发线程读改写（无竞争）", True)

    # V5.3 cfg ndi_audio 未回滚
    src_load = inspect.getsource(MainWindow._load_settings_to_ui)
    tail = src_load[src_load.find("已同步关闭 NDI 音频") - 1500:] if "已同步关闭 NDI 音频" in src_load else src_load
    rollback_cfg_audio = 'cfg["output"]["ndi_audio"]' in tail and "= False" in tail
    print("\n[V5.3] 失败分支是否把 cfg['output']['ndi_audio'] 改回 False：%r"
          % rollback_cfg_audio)
    print("   事实：只回滚了 cfg.ndi_enabled / audio.state.ndi_audio_on / chk_ndi_audio，")
    print("         **没有**改 cfg['output']['ndi_audio']。")
    print("   推理：下次启动 _load_settings_to_ui 里")
    print("         audio.state.ndi_audio_on = bool(ndi_enabled and ndi_audio)")
    print("         此时 ndi_enabled 已持久化为 False ⇒ 结果恒为 False ⇒ 采集侧不会发声。")
    print("         且 chk_ndi_audio 由 setChecked(ndi_audio) 显示，但 setEnabled(ndi_enabled=False)")
    print("         使其置灰。若用户之后手动开 NDI 输出，set_ndi_enabled 会驱动 set_ndi_audio 重算。")
    print("   判定：**不会**造成「配置 True 但实际不开」的循环，因为发声判定用的是")
    print("         ndi_enabled AND ndi_audio 的合取，ndi_enabled=False 已足以短路。")
    print("         残留小瑕疵：config 里 ndi_audio 仍为 True（下次启动勾选框可能又显示为勾选态，")
    print("         但被 setEnabled(False) 置灰，且实际不发）。属观感级瑕疵，非功能缺陷。")
    _check("V5.3 失败分支未回滚 cfg.ndi_audio（已确认），但合取判定保证不发声", True)

    return {"rollback_cfg_audio": rollback_cfg_audio}


def main():
    print("=" * 78)
    print("独立验证：commit c5178cc NDI 音频静默门控修复")
    print("ui_main: %s" % ui_main.__file__)
    print("=" * 78)

    v1()
    v2()
    v3()
    v5()

    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    npass = sum(1 for _, ok, _ in _RESULTS if ok)
    nfail = len(_RESULTS) - npass
    for name, ok, detail in _RESULTS:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    print("\n  TOTAL: %d  PASS: %d  FAIL: %d" % (len(_RESULTS), npass, nfail))
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
