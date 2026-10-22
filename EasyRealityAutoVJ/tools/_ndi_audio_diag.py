# -*- coding: utf-8 -*-
"""离线复现：NDI 传输音频「有画面、没声音」的判定实验（**只读，不改任何源码**）。

用 mock/stub 伪造 cyndilib 的 Sender / VideoSendFrame / AudioSendFrame，
**不加载真实 NDI 运行时、不建任何网络连接**，针对 `src/ndi_out.py::NDIOutput`
逐个场景验证 `feed_audio()` 的实际行为。

运行：
    python tools\\_ndi_audio_diag.py
（用项目 venv：P:\\ERAVJ\\AutoVJ\\venv\\Scripts\\python.exe）

输出每个场景的 `_audio_ok / _audio_dropped / 返回值`。
"""

import os
import sys
import types
import time
import importlib
import importlib.util

# ---------------------------------------------------------------------------
# 0. 让 `import ndi_out` 与源码里的 `from cyndilib... import ...` 都能被劫持
# ---------------------------------------------------------------------------
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
SRC = os.path.abspath(SRC)
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# 源码用的是 `from cyndilib.sender import Sender` 这种子模块导入，
# 所以必须先把 cyndilib / cyndilib.sender / cyndilib.video_frame /
# cyndilib.audio_frame 四个模块**整体**塞进 sys.modules 再 import ndi_out。
import importlib.machinery as _mach


class _StubSender(object):
    """伪造 cyndilib.sender.Sender。"""

    def __init__(self, name=None, *a, **k):
        self.name = name
        self.ndi_name = name
        self.opened = False
        self.closed = False
        self.n_conns = 1            # 假装有 1 个接收端连着
        self.video_frame = None
        self.audio_frame = None
        # 计数
        self.open_calls = 0
        self.close_calls = 0
        self.set_video_frame_calls = 0
        self.set_audio_frame_calls = 0
        self.send_video_calls = 0
        self.send_audio_calls = 0
        self._audio_ok = True       # send_audio() 的返回值

    # --- 帧设置 ---
    def set_video_frame(self, vf):
        self.set_video_frame_calls += 1
        if self.opened:
            raise RuntimeError("Cannot add frame while sender is open")
        self.video_frame = vf

    def set_audio_frame(self, af):
        self.set_audio_frame_calls += 1
        if self.opened:
            raise RuntimeError("Cannot add frame while sender is open")
        self.audio_frame = af

    # --- 生命周期 ---
    def open(self):
        self.open_calls += 1
        self.opened = True
        self.closed = False

    def close(self):
        self.close_calls += 1
        self.closed = True
        self.opened = False

    # --- 连接数探针 ---
    def get_num_connections(self, timeout=0.0):
        return self.n_conns

    # --- 发送 ---
    def send_video(self):
        self.send_video_calls += 1

    def send_video_async(self):
        self.send_video_calls += 1

    def send_audio(self):
        self.send_audio_calls += 1
        return self._audio_ok


class _StubVideoFrame(object):
    """伪造 cyndilib.video_frame.VideoSendFrame。"""

    def __init__(self):
        self.w = None
        self.h = None
        self.fourcc = None
        self.frame_rate = None
        self.progressive = None
        self._write_available = True
        self.bytes_written = 0

    def set_resolution(self, w, h):
        self.w, self.h = int(w), int(h)

    def set_fourcc(self, fc):
        self.fourcc = fc

    def set_frame_rate(self, *a, **k):
        # 源码只传 1 个位置参数（set_frame_rate(60)）；若传 2 个应报错（旧坑）
        if len(a) + len(k) != 1:
            raise TypeError("set_frame_rate() takes exactly 1 positional argument")
        self.frame_rate = a[0] if a else list(k.values())[0]

    def set_progressive(self, p):
        self.progressive = p

    def get_write_available(self):
        return self._write_available

    def write_data(self, buf):
        self.bytes_written += len(bytes(buf)) if not hasattr(buf, "nbytes") else buf.nbytes


class _StubAudioFrame(object):
    """伪造 cyndilib.audio_frame.AudioSendFrame。

    ⚠ 关键点：源码 `_fix_audio_metadata(a)` 会去**扫对象内存**找以
    (sample_rate=48000, num_channels=2, FourCC='FLTp') 为特征的帧结构体。
    stub 的 sample_rate 是普通 Python int 属性、不会在 `id(a)+8..+1024` 的
    连续内存里出现 `c_int == 48000` 的布局，所以扫描会 `_log("...未修...")` 但**不崩**。
    本脚本在 load 之后 monkeypatch 掉 `_fix_audio_metadata`，并如实标注。
    """

    def __init__(self):
        self.sample_rate = 48000
        self.num_channels = 2
        self.max_num_samples = None
        self._write_available = True
        self.n_writes = 0
        self.last_shape = None

    def set_max_num_samples(self, n):
        self.max_num_samples = int(n)

    def get_write_available(self):
        return self._write_available

    def write_data(self, planar):
        self.n_writes += 1
        self.last_shape = getattr(planar, "shape", None)


def _install_cyndilib_stubs():
    """把四个 cyndilib 模块整体塞进 sys.modules（必须在 import ndi_out 之前）。"""
    pkg = types.ModuleType("cyndilib")
    pkg.__path__ = []

    mod_sender = types.ModuleType("cyndilib.sender")
    mod_sender.Sender = _StubSender

    mod_video = types.ModuleType("cyndilib.video_frame")
    mod_video.VideoSendFrame = _StubVideoFrame

    mod_audio = types.ModuleType("cyndilib.audio_frame")
    mod_audio.AudioSendFrame = _StubAudioFrame

    pkg.sender = mod_sender
    pkg.video_frame = mod_video
    pkg.audio_frame = mod_audio
    pkg.get_ndi_version = lambda: "6.1.1.0(stub)"

    sys.modules["cyndilib"] = pkg
    sys.modules["cyndilib.sender"] = mod_sender
    sys.modules["cyndilib.video_frame"] = mod_video
    sys.modules["cyndilib.audio_frame"] = mod_audio


def _make_loader(name):
    """造一个 loader，使 import 时真正执行 src/<name>.py 文件。"""
    path = os.path.join(SRC, name + ".py")
    return _mach.SourceFileLoader(name, path)


def _load_source_module(name):
    loader = _make_loader(name)
    spec = _mach.ModuleSpec(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. 装载被测模块（桩必须先就位）
# ---------------------------------------------------------------------------
_install_cyndilib_stubs()
ndi_out = _load_source_module("ndi_out")

# --- 说明：stub 帧对象不含真实结构体布局，_fix_audio_metadata 扫不到东西（无害）。
#     为避免它干扰计数/日志，直接 monkeypatch 成 no-op（不影响被测逻辑）。
_FIX_PATCHED = False
if hasattr(ndi_out, "_fix_audio_metadata"):
    ndi_out._fix_audio_metadata = lambda a, span=1024: 0
    _FIX_PATCHED = True


# ---------------------------------------------------------------------------
# 2. 辅助
# ---------------------------------------------------------------------------
def _pcm(n_samples=4800, ch=2):
    """一段 float32 立体声交错 PCM（值很小，避免任何数值问题）。"""
    import numpy as np
    return np.zeros(n_samples * ch, dtype=np.float32)


def _new_out(name="DiagNDI"):
    # 复用同一个模块里的类（内部按 name 分单例，但这里直接 new 实例，互不干扰）
    return ndi_out.NDIOutput(name)


def _snap(out, tag):
    print("    [%-26s] _audio_ok=%-5d _audio_dropped=%-5d enabled=%-5s "
          "_inited=%-5s sender_open=%s"
          % (tag, out._audio_ok, out._audio_dropped, out.enabled, out._inited,
             (out._sender.opened if out._sender else None)))


def _bypass_warmup(out):
    """把 open() 时刻回拨到很久以前，绕过 _WARMUP_SECS=0.3 静默期（模拟）。
    这是**人为模拟**，真实运行靠 time.sleep(0.35)。"""
    out._opened_at = time.perf_counter() - 10.0


# ===========================================================================
# 场景 A：视频先建立 sender（1920x1080）→ 连续 feed_audio 200 次
# ===========================================================================
def scenario_A():
    print("\n=== 场景 A：先 send_video(1920x1080) 建立，再 feed_audio x200 (sr=48000) ===")
    out = _new_out("DiagA")
    frame = _pcm(1)

    out.send_video(bytes(1920 * 1080 * 4), 1920, 1080)   # 建立 sender
    _bypass_warmup(out)                                   # 模拟已过 0.3s 静默期
    _snap(out, "send_video 之后")

    for _ in range(200):
        out.feed_audio(_pcm(4800), 48000)
    _snap(out, "feed_audio x200 之后")
    print("    >>> 期望 _audio_ok ≈ 200，实际 = %d" % out._audio_ok)
    return out


# ===========================================================================
# 场景 B：只 feed_audio x200（视频线程从未跑过）
# ===========================================================================
def scenario_B():
    print("\n=== 场景 B：只 feed_audio x200（视频从未跑过，无 sender）===")
    out = _new_out("DiagB")
    for _ in range(200):
        out.feed_audio(_pcm(4800), 48000)
    _snap(out, "feed_audio x200 之后")
    print("    >>> 期望 _audio_ok == 0（确认音频依赖视频先建立），实际 = %d"
          % out._audio_ok)
    return out


# ===========================================================================
# 场景 C：建立 sender → feed x50 → 改分辨率 send_video(1280x720) 触发重建
#         → 之后**不再** send_video，继续 feed_audio x200
# ===========================================================================
def scenario_C():
    print("\n=== 场景 C：建立→feed x50→改分辨率触发 close+重建→停发视频→feed x200 ===")
    out = _new_out("DiagC")
    frame = _pcm(1)

    out.send_video(bytes(1920 * 1080 * 4), 1920, 1080)
    _bypass_warmup(out)
    for _ in range(50):
        out.feed_audio(_pcm(4800), 48000)
    ok_after_50 = out._audio_ok
    _snap(out, "feed x50 之后")

    # 改分辨率 → _ensure(w,h) 走 close() + 重建
    out.send_video(bytes(1280 * 720 * 4), 1280, 720)
    _snap(out, "改分辨率 send_video 之后")
    # ⚠ 注意：_ensure 里重建后会重置 _opened_at，这里同样回拨以隔离「静默期」变量
    _bypass_warmup(out)

    # 之后不再调 send_video —— 模拟「视频线程停了 / 第一帧迟迟不来」
    for _ in range(200):
        out.feed_audio(_pcm(4800), 48000)
    _snap(out, "仅 feed x200 之后")
    print("    >>> 改分辨率前 ok=%d；之后只喂音频的 ok 增量 = %d（期望 >0 则证明不受影响）"
          % (ok_after_50, out._audio_ok - ok_after_50))
    return out


# ===========================================================================
# 场景 D：ui_main._ndi_audio_feed 门控 —— eng._ndi is None 时是否静默无操作
# ===========================================================================
def scenario_D():
    print("\n=== 场景 D：模拟 ui_main._ndi_audio_feed 门控（eng._ndi = None）===")

    # 复刻 ui_main._ndi_audio_feed 的**逐字**逻辑（不 import ui_main，避免拉起 PySide6）
    captured = {"called": 0, "prints": []}

    class _Eng(object):
        _ndi = None

    class _FakeCfg(dict):
        pass

    class _FakeMain(object):
        def __init__(self):
            self.engine = _Eng()
            self.cfg = {"output": {"ndi_enabled": True}}   # 用户已勾「NDI 输出」

        # ---- 与 src/ui_main.py:3243 完全一致 ----
        def _ndi_audio_feed(self, pcm, sr):
            ndi = self.engine._ndi
            if ndi is not None and self.cfg["output"].get("ndi_enabled"):
                ndi.feed_audio(pcm, sr)

    m = _FakeMain()

    # 捕获 stdout，验证「静默」（既无返回、也无日志）
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        ret = m._ndi_audio_feed(_pcm(4800), 48000)
    finally:
        sys.stdout = old
    out_text = buf.getvalue()

    print("    返回值 = %r；捕获到的 stdout 文本 = %r" % (ret, out_text))
    print("    >>> _ndi is None ⇒ 静默无操作、无日志：%s"
          % ("是（证实现场‘勾了却没声音、日志也全无’）"
             if ret is None and out_text == "" else "否"))
    return m


# ===========================================================================
# 场景 A2：feed_audio 采用 sr=44100（走重采样分支）验证不会因此丢块
# ===========================================================================
def scenario_A2():
    print("\n=== 场景 A2：先建 sender，再 feed_audio x100 (sr=44100, 走重采样) ===")
    out = _new_out("DiagA2")
    out.send_video(bytes(1920 * 1080 * 4), 1920, 1080)
    _bypass_warmup(out)
    for _ in range(100):
        out.feed_audio(_pcm(4410), 44100)   # 0.1s @44.1k
    _snap(out, "feed x100 (44.1k) 之后")
    print("    >>> _audio_ok=%d  _audio_resampled=%d（期望都接近 100）"
          % (out._audio_ok, out._audio_resampled))
    return out


# ===========================================================================
# 场景 E：额外验证 —— 重建后视频线程**立即**又跑一帧，音频能否恢复
# ===========================================================================
def scenario_E():
    print("\n=== 场景 E：改分辨率重建后，视频线程立刻补一帧 → feed x100 ===")
    out = _new_out("DiagE")
    out.send_video(bytes(1920 * 1080 * 4), 1920, 1080)
    _bypass_warmup(out)
    out.feed_audio(_pcm(4800), 48000)

    out.send_video(bytes(1280 * 720 * 4), 1280, 720)   # 重建
    _bypass_warmup(out)
    out.send_video(bytes(1280 * 720 * 4), 1280, 720)   # 视频线程立刻补一帧
    _bypass_warmup(out)
    base = out._audio_ok
    for _ in range(100):
        out.feed_audio(_pcm(4800), 48000)
    _snap(out, "视频补帧后 feed x100")
    print("    >>> 恢复增量 = %d（期望 ≈100，说明‘视频续上则音频自恢复’）"
          % (out._audio_ok - base))
    return out


# ===========================================================================
# 场景 C2：与 C 相同，但**不**回拨静默期，用真实 time.sleep 跨过 warmup，
#          以证明 C 的“不受影响”不是因为我把 warmup 变量抽掉了
# ===========================================================================
def scenario_C2():
    print("\n=== 场景 C2：C 的严格版（真实 sleep 跨 warmup，不手工回拨）===")
    out = _new_out("DiagC2")
    out.send_video(bytes(1920 * 1080 * 4), 1920, 1080)
    time.sleep(0.35)                                    # 真实跨过 _WARMUP_SECS
    for _ in range(50):
        out.feed_audio(_pcm(4800), 48000)

    out.send_video(bytes(1280 * 720 * 4), 1280, 720)    # 重建（_opened_at 刷新）
    # 立刻喂（此时在静默期，音频应被记 dropped，而不是永久卡死）
    for _ in range(20):
        out.feed_audio(_pcm(4800), 48000)
    _snap(out, "重建后静默期内 feed x20")
    time.sleep(0.35)                                    # 等静默期过去
    base_ok, base_drop = out._audio_ok, out._audio_dropped
    for _ in range(200):
        out.feed_audio(_pcm(4800), 48000)
    _snap(out, "静默期后 feed x200")
    print("    >>> 静默期后 ok 增量=%d（期望 200 ⇒ 音频会自愈，不会永久卡死）"
          % (out._audio_ok - base_ok))
    return out


def _load_fake_config():
    """读取真实 config.json 里的 output 段，用于佐证现场配置。"""
    try:
        import json
        cfg_candidates = [
            os.path.join(os.path.dirname(SRC), "config.json"),
            os.path.join(os.environ.get("APPDATA", ""), "EasyRealityAutoVJ", "config.json"),
        ]
        for c in cfg_candidates:
            if c and os.path.isfile(c):
                with open(c, encoding="utf-8") as f:
                    return c, json.load(f)
    except Exception as e:                                          # noqa: BLE001
        return None, {"_err": str(e)}
    return None, None


def main():
    print("=" * 78)
    print("NDI 传输音频 —— 离线复现实验（stub cyndilib，无网络，无真实 NDI 运行时）")
    print("源码目录: %s" % SRC)
    print("_fix_audio_metadata 已被 monkeypatch 为 no-op: %s" % _FIX_PATCHED)
    print("=" * 78)

    r = {}
    r["A"] = scenario_A()
    r["B"] = scenario_B()
    r["C"] = scenario_C()
    r["C2"] = scenario_C2()
    scenario_D()
    r["A2"] = scenario_A2()
    r["E"] = scenario_E()

    path, cfg = _load_fake_config()
    print("\n--- 现场 config 佐证 ---")
    if cfg and "output" in cfg:
        o = cfg["output"]
        print("config: %s" % path)
        print("  ndi_enabled=%r ndi_audio=%r ndi_name=%r"
              % (o.get("ndi_enabled"), o.get("ndi_audio"), o.get("ndi_name")))
    else:
        print("（未找到 config.json，跳过）")

    print("\n" + "=" * 78)
    print("汇总结论：")
    print("  A (视频先建, 48k)      _audio_ok=%d" % r["A"]._audio_ok)
    print("  B (无视频, 48k)        _audio_ok=%d" % r["B"]._audio_ok)
    print("  C (改分辨率后停视频)   _audio_dropped=%d, _audio_ok=%d"
          % (r["C"]._audio_dropped, r["C"]._audio_ok))
    print("  C2(严格版, 真 sleep)   _audio_dropped=%d, _audio_ok=%d"
          % (r["C2"]._audio_dropped, r["C2"]._audio_ok))
    print("  A2(视频先建, 44.1k)    _audio_ok=%d resampled=%d"
          % (r["A2"]._audio_ok, r["A2"]._audio_resampled))
    print("  E (重建后视频补帧)     _audio_ok=%d" % r["E"]._audio_ok)
    print("=" * 78)


if __name__ == "__main__":
    main()
