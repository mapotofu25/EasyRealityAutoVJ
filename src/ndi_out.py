# -*- coding: utf-8 -*-
"""NDI 输出（音画同步）。cyndilib 封装。

⚠ **NDI 运行时随软件自带，用户不需要安装任何东西**：
`cyndilib` 的 wheel 里带有完整的 `Processing.NDI.Lib.x64.dll`（28.8 MB），
PyInstaller 会把它一起打进包（实测本机 `Program Files` 下从未安装过 NDI，
而 `cyndilib.get_ndi_version()` 仍能报出 6.1.1.0 —— 读的就是自带的这一份）。
所以只有**接收端**（另一台电脑 / OBS 的 NDI 插件）需要它自己装 NDI Runtime。
⚠ 注意：接收端只装**免费的 NDI Runtime 单独安装包**即可，**不必**装完整的 NDI Tools
（那是一整套软件，体积大得多）。

Runtime 未安装时：import 成功但 Sender.open() 会失败 → 静默禁用，不影响其余功能。
视频从引擎线程喂帧，音频从采集线程喂 PCM（内部加锁）。
"""
import threading
import time

_ERR_ONCE = {"shown": False}


def _log(msg):
    if not _ERR_ONCE["shown"]:
        _ERR_ONCE["shown"] = True
        print("[NDI]", msg)


def probe_runtime():
    """探测 NDI 运行时能否加载。返回 (ok: bool, 说明: str)。

    ⚠⚠ **这是 UI 专用的「勾选时检查」，绝不能用 `_ensure()` 代替。**
    `_ensure(w, h)` 需要**真实分辨率** —— 发送端的 VideoSendFrame 尺寸只能在 `open()`
    之前定死，所以不给尺寸时它**按设计返回 False**（等第一帧来了才知道尺寸）。
    UI 在勾选那一刻还没有画布尺寸；若拿 `_ensure()` 当探测，就会永远误报
    「NDI 不可用」并把用户的勾选自动取消掉 —— 2026-09-25 正是这个原因让 092502
    的 NDI 输出完全打不开（引擎侧其实已经修好了）。
    """
    try:
        import cyndilib
        return True, str(cyndilib.get_ndi_version())
    except Exception as e:                                       # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:140])


class NDIOutput:
    # ⚠⚠ `open()` 之后的**静默期**：cyndilib 实测在这个窗口内提交帧会**段错误**
    #   （native crash，Python 的 try/except 抓不住）。现场路径正好踩：
    #   视频线程刚建好 sender，音频线程 ~20ms 后就 feed_audio ⇒ 立刻崩/丢。
    _WARMUP_SECS = 0.3

    def __init__(self, name="EasyRealityAutoVJ"):
        self.name = name
        self._lock = threading.Lock()
        self._sender = None
        self._video = None
        self._audio = None
        self._w = None                      # 当前 sender 是按哪个分辨率建的
        self._h = None
        self.enabled = False
        self._inited = False
        self._last_err = ""
        self._dropped = 0        # 因缓冲未就绪而丢掉的帧数（诊断用，正常应接近 0）
        # ---- 音频（2026-10-01 修：原来三个错叠在一起，NDI 音频从来没响过）----
        self._audio_sr = 48000   # 采集采样率提示：帧必须在 open() 之前定好采样率
        self._audio_ok = 0       # 成功发出的音频块数（诊断）
        self._audio_dropped = 0  # 因缓冲未就绪丢掉的块数
        self._audio_err = ""     # 最近一次发送错误（诊断）
        self._audio_err_logged = None   # 只把**第一条**错误写日志，避免刷屏
        self._err_logged = None         # 视频侧同样只记第一条
        self._opened_at = 0.0           # sender 的 open() 时刻（算静默期用）

    def _ensure(self, w=None, h=None):
        """确保发送端按 (w, h) 就绪；成功返回 True。

        ⚠⚠ 这里有两个 cyndilib/NDI 的**硬约束**，踩过一次惨痛的静默失效：

        1. `Sender.set_video_frame()` **必须在 `open()` 之前**调用 ——
           open 之后再设会抛 `Cannot add frame while sender is open`。
        2. VideoSendFrame 一旦 attach 到 sender，**分辨率与 fourcc 都不能再改** ——
           改就抛 `Cannot alter frame`。

        所以分辨率必须**在 open 之前定死**。旧代码把 frame 硬编码成 1280x720 就 attach、
        然后 `send_video` 想改成实际的 1920x1080 → 抛 `Cannot alter frame` → 被本函数的
        `except` 吞掉 → `enabled = False` → **此后每一帧都被静默丢弃**，
        现象是「NDI 开关是开的、源也能被搜到，但接收端一个字都收不到」。

        结论：**按第一帧的真实尺寸建立 sender**；分辨率变了就整个重建。
        音频线程调用时不给 w/h（返回 False），等视频线程先按真实分辨率建起来。
        """
        if self._inited and self.enabled and (w is None or (self._w == w and self._h == h)):
            return True
        if w is None or h is None:
            return False
        if self._inited:
            self.close()                    # 分辨率变了 → 重建（frame 不能改）
        self._inited = True
        try:
            from cyndilib.sender import Sender
            from cyndilib.video_frame import VideoSendFrame
            from cyndilib.audio_frame import AudioSendFrame
            s = Sender(self.name)
            # ⚠ cyndilib 的 Sender **没有 set_ndi_name() 方法**（只有 name / ndi_name 属性）。
            # 2026-09-24 实测报 `'Sender' object has no attribute 'set_ndi_name'`，
            # 而这个异常被本函数的 except 吃掉 → NDI 输出**一直静默不可用**。
            # 名字在 Sender(name) 里已经设过，这里只是显式再赋一次属性（失败也无所谓）。
            try:
                s.ndi_name = self.name
            except Exception:
                pass
            # 视频帧：BGRA（QImage RGB32 内存序一致）—— 尺寸必须在 open() 之前定死
            v = VideoSendFrame()
            v.set_resolution(int(w), int(h))
            v.set_fourcc(0x41524742)  # 'BGRA' little-endian
            # ⚠ 第二个坑：set_frame_rate 的签名是 (value: Fraction)，**只能传 1 个位置参数**。
            # 旧代码写 set_frame_rate(60, 1) → TypeError → 同样被吞掉，NDI 开不起来。
            v.set_frame_rate(60)
            v.set_progressive(True)
            s.set_video_frame(v)
            # 音频帧：float32 立体声 48k
            a = AudioSendFrame()
            try:
                # ⚠ 采样率必须**在 open() 之前**定好，所以用采集侧提前告知的值
                #   （`set_audio_rate()`；没告知就按 48k）。立体声、每帧最多 4800 样本。
                a.sample_rate = int(self._audio_sr or 48000)
                a.num_channels = 2
                a.set_max_num_samples(4800)
            except Exception:
                pass
            s.set_audio_frame(a)
            s.open()
            self._opened_at = time.perf_counter()   # ★ 开始算静默期（见 _WARMUP_SECS）
            self._sender, self._video, self._audio = s, v, a
            self._w, self._h = int(w), int(h)
            self.enabled = True
            self._last_err = ""
            print("[NDI] 已开启输出: %s  %dx%d" % (self.name, w, h))
        except Exception as e:
            self._last_err = str(e)[:160]
            _log("NDI 运行时加载失败（运行时随包自带，通常是被杀毒隔离或缺文件）：%s" % self._last_err)
            self.enabled = False
        return self.enabled

    def _conns(self):
        """当前有几个接收端连着（0 = 没人连，**绝不能**调 send_*）。

        ⚠⚠ 2026-10-01 实测：没有接收端时 `send_audio()` 会**段错误**（让整个软件崩掉），
          `send_video()` 会抛 `Object is not writable`。所以一律先看连接数，0 就直接丢帧。
        """
        try:
            return int(self._sender.get_num_connections(0.0))
        except Exception:                                          # noqa: BLE001
            return 1          # 探针本身失败时按"有人连"处理，保持老行为（不误伤正常输出）

    def send_video(self, bgra, w, h):
        if not self._ensure(w, h):
            return
        try:
            with self._lock:
                need = int(w) * int(h) * 4
                if len(bgra) != need:
                    # 画布若带行填充（bytesPerLine > w*4）长度会对不上 —— 直接放弃这一帧，
                    # 不要抛给外面把整个输出关掉
                    self._last_err = "帧数据 %d 字节 != 期望 %d" % (len(bgra), need)
                    return
                # ⚠⚠ 必须先查「可写」再写：buffer 还没空出来时 `write_data` 会**抛异常**
                #   （cyndilib 抛 `RuntimeError: no write frame available`），而下面的
                #   `except` 会把整个 NDI 输出**永久关掉** → enabled=False → 此后每帧静默丢弃。
                #   接收端稍慢、网络抖一下就会中招 —— 又是一个"静默失效"。
                #   正确做法：这一帧**直接丢掉**，既不等待、也不关输出。
                # ⚠ 开门静默期：这段时间内提交帧会抛（视频）/崩（音频），一律丢掉
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._dropped += 1
                    return
                # ⚠ 视频**不能**做连接数门控：NDI 是"接收端收到数据后连接才建立"，
                #   不发视频 ⇒ 接收端永远连不上（连接数恒 0）⇒ 音视频一起废。
                #   这里只靠 warmup 守卫 + "写失败只丢帧不关输出"就够安全了。
                v = self._video
                if hasattr(v, "get_write_available") and not v.get_write_available():
                    self._dropped += 1
                    return
                v.write_data(bgra)
                if hasattr(self._sender, "send_video_async"):
                    self._sender.send_video_async()
                else:
                    self._sender.send_video()
        except Exception as e:
            # ⚠⚠ **绝不能**在这里 `enabled = False`（2026-10-01 实测踩到）：
            #   没有接收端连接时写视频帧会抛 `Object is not writable`，老代码就永久关掉
            #   整个 NDI 输出 ⇒ 接收端**后面再接上来也永远没画面/没声音**。
            #   正确做法：丢掉这一帧、记一次日志，保持 enabled，下一帧继续尝试（接收端
            #   一旦连上就自然恢复）。
            self._last_err = str(e)[:160]
            self._dropped += 1
            if self._audio_err_logged is None and not self._err_logged:
                self._err_logged = self._last_err
                _log("!! NDI 发送视频失败（只记一次，后续继续尝试）：%s" % self._last_err)

    def set_audio_rate(self, sr):
        """采集侧提前告知采样率：音频帧的采样率**只能在 open() 之前设**。"""
        try:
            self._audio_sr = int(sr)
        except Exception:                                          # noqa: BLE001
            pass

    def feed_audio(self, pcm_interleaved, sr):
        # 音频线程拿不到画布尺寸 → `_ensure()` 不带参数：
        # 发送端还没按视频分辨率建立时返回 False（这段音频先不发），建立之后返回 True。
        # NDI 是音画同步输出，视频没起来时音频也没有意义。
        #
        # ⚠⚠ 2026-10-01 修（用户报「NDI 输出还是没有音频」，实测三个错叠在一起）：
        #   1. `AudioSendFrame` **没有 `set_sample_rate()` 方法**（只有 `sample_rate` 属性）
        #      ⇒ 老代码 `a.set_sample_rate(sr)` 直接 AttributeError；
        #   2. `write_data` 要的是 **(声道数, 样本数) 的平面(planar) 二维数组**
        #      （实测 `shape=(2,4800)`、`channel_stride=19200`；传一维会报
        #      `Buffer has wrong number of dimensions (expected 2, got 1)`，
        #      传 (n, ch) 交错二维会报 `number of channels must match`）；
        #   3. 上面两个异常都被 `except Exception: pass` 吞掉 ⇒ **一个字节都发不出去、
        #      而且完全没有日志**。三者叠加 = NDI 音频从来就没响过。
        if not self._ensure():
            return
        try:
            import numpy as _np
        except Exception:                                          # noqa: BLE001
            return
        try:
            with self._lock:
                a = self._audio
                if a is None:
                    return
                sr = int(sr or 0)
                if sr > 0 and int(a.sample_rate) != sr:
                    a.sample_rate = sr          # ★ 属性赋值（没有 set_sample_rate 方法）
                # ⚠ 开门静默期：这一条是**防段错误**的（实测 open 后立刻 send_audio 会崩）
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._audio_dropped += 1
                    return
                # ⚠⚠ 没有任何接收端连上时，`send_audio()` 会**段错误**（实测）。
                #   没人听就没必要发 —— 直接丢块，绝不碰 native 调用。
                if self._conns() <= 0:
                    self._audio_dropped += 1
                    if self._audio_dropped == 1:
                        _log("[NDI] 音频暂时不发：还没有接收端连上（连上后自动开始）")
                    return
                ch = int(a.num_channels or 2) or 2
                pcm = _np.asarray(pcm_interleaved, dtype=_np.float32)
                if pcm.ndim == 1:               # 交错一维 → (n, ch)
                    n = (pcm.size // ch) * ch
                    pcm = pcm[:n].reshape(-1, ch)
                elif pcm.shape[0] == ch and pcm.shape[1] != ch:
                    pcm = pcm.T                 # 已经是平面 (ch, n) → 转成 (n, ch) 统一处理
                # 交错 (n, ch) → 平面 (ch, n)，并保证 C 连续（cyndilib 直接按缓冲读）
                planar = _np.ascontiguousarray(pcm.T)
                # 与视频同样的坑：缓冲没空出来时 write_data 会抛 —— 这一块直接丢，不关输出
                if hasattr(a, "get_write_available") and not a.get_write_available():
                    self._audio_dropped += 1
                    return
                a.write_data(planar)
                self._sender.send_audio()
                self._audio_ok += 1
                if self._audio_ok == 1:
                    _log("[NDI] 音频已开始发送（%.1f kHz / 2ch）" % (sr / 1000.0))
        except Exception as e:                                     # noqa: BLE001
            self._audio_err = "%s: %s" % (type(e).__name__, str(e)[:120])
            if self._audio_err_logged is None:
                self._audio_err_logged = self._audio_err
                _log("!! NDI 音频发送失败（只记一次）：%s" % self._audio_err)

    def close(self):
        try:
            if self._sender is not None:
                self._sender.close()
        except Exception:
            pass
        self._sender = None
        self._video = None
        self._audio = None
        self._w = None
        self._h = None
        self._opened_at = 0.0
        self._inited = False        # ★ 必须重置：否则换分辨率后无法重建（见 _ensure 注释）
        self.enabled = False


# 全局单例（引擎线程 + 采集线程共享）
_inst = None
_inst_lock = threading.Lock()


def get_ndi(name):
    global _inst
    with _inst_lock:
        if _inst is None:
            _inst = NDIOutput(name)
        elif _inst.name != name:
            _inst.close()
            _inst = NDIOutput(name)
        return _inst
