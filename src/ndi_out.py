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
import ctypes
import threading
import time

# ⚠⚠ 日志必须**按 key 去重**，不能全局只打一次（2026-10-01 修）
#   原来用 `_ERR_ONCE = {"shown": False}`：第一条日志之后**所有**日志永久静默。
#   于是「还没有接收端连上」这条无害提示一旦打印过，后面真正的错误
#   （音频发送失败、采样率不对、write_data 卡死……）**全部被吞掉**，
#   直接制造了「有画面、没声音、而且日志里啥也没有」这种最难查的现场。
#   现在：同一条 key 只打一次，不同 key 互不干扰。
_LOGGED = set()
_LOG_LOCK = threading.Lock()


def _log(msg, key=None):
    k = key if key is not None else msg
    if k in _LOGGED:
        return
    with _LOG_LOCK:
        if k in _LOGGED:
            return
        _LOGGED.add(k)
    print("[NDI]", msg)


# NDI 规范：音频固定 48 kHz。⚠ 帧的采样率在 `open()` 那一刻就被**冻结**进每个
# buffer item（`audio_frame_copy()` 只做一次性快照），之后再改母板也不生效 ⇒
# 我们只能固定 48k，并在喂数据时自己重采样（见 feed_audio）。
_AUDIO_SR = 48000


def _mbi_readable(addr, size=8):
    """该地址是否落在已提交、可读的内存页里（探测野指针用）。"""
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p),
                        ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", wintypes.DWORD),
                        ("RegionSize", ctypes.c_size_t),
                        ("State", wintypes.DWORD), ("Protect", wintypes.DWORD),
                        ("Type", wintypes.DWORD)]
        if not addr:
            return False
        mbi = MBI()
        if not k32.VirtualQuery(ctypes.c_void_p(addr), ctypes.byref(mbi),
                                ctypes.sizeof(mbi)):
            return False
        # 0x1000 = MEM_COMMIT；0x01 = PAGE_NOACCESS；0x100 = PAGE_GUARD
        if mbi.State != 0x1000 or (mbi.Protect & 0x01) or (mbi.Protect & 0x100):
            return False
        base = mbi.BaseAddress or 0
        return base <= addr and addr + size <= base + mbi.RegionSize
    except Exception:                                          # noqa: BLE001
        return False


def _fix_audio_metadata(a, span=1024):
    """修 cyndilib 0.1.1 的致命 bug：音频帧的 `p_metadata` 是**野指针**。

    ## 为什么必须修（2026-10-01 实测，见 `_NDI音频诊断.md`）
    cyndilib 的 `ndi_structs.pyx:87 audio_frame_create_default()` 用 `malloc`（**不清零**）
    建 `NDIlib_audio_frame_v3_t`，却漏了 `p_metadata = NULL`
    —— 对照同文件的**视频**版本 `video_frame_create_default()` 是有这一行的。
    `audio_frame_copy()` 也不复制它。
    实测在对象内存里扫出的 3 个音频帧结构体，`p_metadata` **3/3 都是垃圾**
    （读出来是残留的 JSON 文本，典型的"malloc 到别人用过的堆块"）；视频则是 NULL。

    **后果**：只要真有接收端在听，NDI 会把这个指针当 C 字符串读 ⇒
    `0xC0000005` 访问冲突，发送进程 1 秒内必崩（跨进程实验：只发视频 exit=0，
    只发音频 exit=3221225477）。没有接收端时反而不崩，所以很容易误判成"没接收端才会崩"。

    **修法**：在 `set_audio_frame()` 之后、`open()` **之前**，
    把"母板"帧结构体的 `p_metadata` 置 NULL
    （`open()` 时的 `audio_frame_copy()` 会把母板拷进 3 个 buffer item，所以改母板就够）。
    对照实验：置 NULL 后同样的回环测试从"必崩"变成稳定跑满 8 秒、
    接收端收到 42 帧**全部非静音**。

    ⚠ 用结构体特征（sample_rate / 声道数 / FourCC=FLTp）来认帧，而不是硬编码偏移，
      这样 cyndilib 结构变了也不会误写别人内存。
    """
    try:
        base = id(a)
        n = 0
        for off in range(8, span, 8):
            p = ctypes.c_void_p.from_address(base + off).value or 0
            if not _mbi_readable(p, 64):
                continue
            if ctypes.c_int.from_address(p + 0).value != _AUDIO_SR:   # sample_rate
                continue
            if ctypes.c_int.from_address(p + 4).value != 2:           # no_channels
                continue
            # FourCC: 'FLTp' little-endian = 0x70544C46
            if (ctypes.c_int.from_address(p + 24).value & 0xFFFFFFFF) != 0x70544C46:
                continue
            ctypes.c_void_p.from_address(p + 48).value = None         # p_metadata = NULL
            n += 1
        if n:
            _log("已修 cyndilib 音频帧野指针：%d 个帧结构体的 p_metadata 置 NULL"
                 "（不修的话一开声音就 0xC0000005 崩溃）" % n, key="pmeta")
        else:
            _log("⚠ 没找到音频帧结构体，p_metadata 野指针**未修**"
                 "（若此后音频一开就崩，就是这里）", key="pmeta-miss")
        return n
    except Exception as e:                                     # noqa: BLE001
        _log("修 p_metadata 失败：%s（音频可能一开就崩）" % e, key="pmeta-err")
        return 0


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
        self._audio_sr = 48000   # 采集采样率提示（**已不再用于帧设置**：帧固定 48k，见 _AUDIO_SR）
        self._audio_ok = 0       # 成功发出的音频块数（诊断）
        self._audio_dropped = 0  # 因缓冲未就绪丢掉的块数
        self._audio_resampled = 0  # 因采集率≠48k 而重采样的块数（诊断）
        self._audio_last_log = 0.0  # 上次打心跳日志的时刻（每 ~10 秒一行）
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
            # 音频帧：float32 立体声 **固定 48k**（NDI 规范要求；见 _AUDIO_SR 的说明）
            a = AudioSendFrame()
            try:
                a.sample_rate = _AUDIO_SR
                a.num_channels = 2
                a.set_max_num_samples(4800)
            except Exception:
                pass
            s.set_audio_frame(a)
            # ★★ 必须在 open() 之前修掉 cyndilib 的 p_metadata 野指针（见 _fix_audio_metadata）
            _fix_audio_metadata(a)
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
        """记录采集侧采样率（**仅供诊断/日志**）。

        ⚠ 2026-10-01 修正：这里**不再**用来设置音频帧的采样率。
          NDI 规范要求音频固定 48 kHz，而且帧的采样率在 `open()` 那一刻就被冻结
          （`audio_frame_copy()` 是一次性快照，之后改母板不传播）
          ⇒ 帧固定 48k，采集侧非 48k 的数据由 `feed_audio()` 自己重采样。
        """
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
                # ⚠ 开门静默期：这一条是**防段错误**的（实测 open 后立刻 send_audio 会崩）
                if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                    self._audio_dropped += 1
                    return
                ch = int(a.num_channels or 2) or 2
                pcm = _np.asarray(pcm_interleaved, dtype=_np.float32)
                if pcm.ndim == 1:               # 交错一维 → (n, ch)
                    n = (pcm.size // ch) * ch
                    pcm = pcm[:n].reshape(-1, ch)
                elif pcm.shape[0] == ch and pcm.shape[1] != ch:
                    pcm = pcm.T                 # 已经是平面 (ch, n) → 转成 (n, ch) 统一处理
                # ★ 重采样到 48k（NDI 只认 48k，而帧的采样率在 open() 时就被冻结，
                #   之后 `a.sample_rate = sr` 不报错但**完全无效** —— 实测确认）。
                #   不重采样的话：若设备是 44.1k，接收端会按 48k 播 ⇒ 快 8.8%、音调升高。
                if sr and int(sr) != _AUDIO_SR and len(pcm):
                    n_in = int(pcm.shape[0])
                    n_out = int(round(n_in * _AUDIO_SR / float(sr)))
                    if n_in > 1 and n_out > 1:
                        x_in = _np.arange(n_in, dtype=_np.float64)
                        x_out = _np.linspace(0.0, n_in - 1, n_out, dtype=_np.float64)
                        pcm = _np.stack(
                            [_np.interp(x_out, x_in, pcm[:, c].astype(_np.float64))
                             for c in range(pcm.shape[1])], axis=1).astype(_np.float32)
                        self._audio_resampled += 1
                # 交错 (n, ch) → 平面 (ch, n)，并保证 C 连续（cyndilib 直接按缓冲读）
                planar = _np.ascontiguousarray(pcm.T)
                # 与视频同样的坑：缓冲没空出来时 write_data 会抛 —— 这一块直接丢，不关输出
                if hasattr(a, "get_write_available") and not a.get_write_available():
                    self._audio_dropped += 1
                    return
                a.write_data(planar)
                # ⚠ 不再用「连接数门控」（2026-10-01 实测修正）：
                #   原来以为"没有接收端时 send_audio() 会段错误"，实测**不成立**
                #   （无接收端时它只是返回 False，真正会崩的是 p_metadata 野指针，已修）。
                #   而 `get_num_connections()` 在「接收端刚连上、计数还没上报」的窗口返回 0，
                #   门控会把那一段音频**整块丢掉且毫无日志** —— 正是"没有声音"的隐形帮凶。
                #   现在改成：发不出去就丢这一块，并计数 + 周期性打日志。
                ok = True
                try:
                    if hasattr(self._sender, "send_audio"):
                        ok = bool(self._sender.send_audio())
                except Exception as e:                             # noqa: BLE001
                    ok = False
                    self._audio_err = "%s: %s" % (type(e).__name__, str(e)[:120])
                if ok:
                    self._audio_ok += 1
                else:
                    self._audio_dropped += 1
                if self._audio_ok and self._audio_ok == 1:
                    _log("音频已开始发送（%.1f kHz → %.1f kHz / 2ch）"
                         % ((sr or _AUDIO_SR) / 1000.0, _AUDIO_SR / 1000.0), key="audio-start")
                # 每 ~10 秒给一行心跳，现场据此就能判断"到底有没有在发"
                now = time.perf_counter()
                if now - getattr(self, "_audio_last_log", 0.0) >= 10.0:
                    self._audio_last_log = now
                    _log("audio: src=%d -> %d, ok=%d dropped=%d resampled=%d conns=%d err=%s"
                         % (int(sr or 0), _AUDIO_SR, self._audio_ok, self._audio_dropped,
                            self._audio_resampled, self._conns(), self._audio_err or "-"),
                         key="audio-hb-%d" % (self._audio_ok // max(1, self._audio_ok)))
        except Exception as e:                                     # noqa: BLE001
            self._audio_err = "%s: %s" % (type(e).__name__, str(e)[:120])
            if self._audio_err_logged is None:
                self._audio_err_logged = self._audio_err
                _log("!! NDI 音频发送失败（只记一次）：%s" % self._audio_err)
            # ⚠ 已知坑：`write_data` 一旦因「样本数超过 max」失败，cyndilib 的
            #   `buffer_write_item` 会**永久卡住**，之后每次 write_data 都报
            #   `buffer_write_item is not null` ⇒ 音频永久停摆。
            #   出现这类错误就**重建会话**，而不是继续用坏帧。
            if "not null" in self._audio_err or "exceeds maximum" in self._audio_err:
                _log("检测到音频缓冲卡死，重建 NDI 会话以恢复", key="audio-rebuild")
                try:
                    self.close()
                except Exception:                                  # noqa: BLE001
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
