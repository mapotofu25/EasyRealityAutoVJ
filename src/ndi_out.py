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
                a.sample_rate = 48000
                a.num_channels = 2
                a.set_max_num_samples(4800)
            except Exception:
                pass
            s.set_audio_frame(a)
            s.open()
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
            self._last_err = str(e)[:160]
            self.enabled = False

    def feed_audio(self, pcm_interleaved, sr):
        # 音频线程拿不到画布尺寸 → `_ensure()` 不带参数：
        # 发送端还没按视频分辨率建立时返回 False（这段音频先不发），建立之后返回 True。
        # NDI 是音画同步输出，视频没起来时音频也没有意义。
        if not self._ensure():
            return
        try:
            with self._lock:
                a = self._audio
                if a.sample_rate != sr:
                    a.set_sample_rate(sr)
                a.write_data(pcm_interleaved)
                self._sender.send_audio()
        except Exception:
            pass

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
