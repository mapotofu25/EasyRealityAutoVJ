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
import os
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
# ⚠⚠ 写盘**必须用另一把锁**（2026-10-07 自检抓到的死锁）：
#   `threading.Lock` **不可重入**。若 `_health_write` 复用 `_LOG_LOCK`，
#   而 `_log` 已经持有它 → 第一次打印就**自锁死**，整个 NDI 线程卡住
#   （比"没日志"恶劣得多：会直接冻住输出）。两把锁各管各的。
_FILE_LOCK = threading.Lock()


def _health_path():
    """NDI 取证日志路径：`%LOCALAPPDATA%\\AutoVJ\\output_health.log`。

    ⚠⚠ 2026-10-07 修（现场"另一台电脑收不到声音"查不到原因的本质）：
    本模块原先把 100% 的诊断**只走 `print()`** —— 而打包后是 `--windowed`，
    **控制台根本不存在，stdout 被丢弃**。于是「音频有没有在发、发失败没有、
    门控把哪一段拦下了」在现场**一个字都看不到**，只能靠猜。
    而 `engine._output_health_write` 早就把视频侧失败写进了这个文件 ——
    NDI 音频侧却从来没写进去过。这里补上，让两条链共用同一份取证日志。
    """
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ",
                        "output_health.log")


def _health_write(line):
    """把一行诊断追加到 `output_health.log`（带时间戳）。

    任何异常一律吞掉：取证绝不能反过来影响演出（与 engine 侧同口径）。
    ⚠ 本函数会被**多线程**调用（视频引擎线程 / 采集转发线程），用独立的
      `_FILE_LOCK` 串行化写入（**不能复用 `_LOG_LOCK`**，否则与 `_log` 构成自锁）。
    """
    try:
        p = _health_path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with _FILE_LOCK:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("[%s] NDI %s\n" % (stamp, line))
    except Exception:                                          # noqa: BLE001
        pass


def _log(msg, key=None):
    """NDI 诊断：**同时**打控制台与 `output_health.log`（按 key 去重）。

    ⚠⚠ 必须落盘：打包后 `--windowed` 没有控制台，只 `print` 等于没记（见 `_health_path`）。
    去重仍在 `print`/写盘**之前**判定，保证同一条 key 只出现一次；不同 key 互不干扰。
    """
    k = key if key is not None else msg
    if k in _LOGGED:
        return
    with _LOG_LOCK:
        if k in _LOGGED:
            return
        _LOGGED.add(k)
    print("[NDI]", msg)
    _health_write(msg)


# NDI 规范：音频固定 48 kHz。⚠ 帧的采样率在 `open()` 那一刻就被**冻结**进每个
# buffer item（`audio_frame_copy()` 只做一次性快照），之后再改母板也不生效 ⇒
# 我们只能固定 48k，并在喂数据时自己重采样（见 feed_audio）。
_AUDIO_SR = 48000

# ★★ 2026-10-07：每一「帧」发出的样本数（= AudioSendFrame 的 max_num_samples）。
#   100ms @48k。**必须与 _ensure() 里 `a.set_max_num_samples(_FRAME_SAMPLES)` 一致。**
#   定长发送是修「声音断续/短促高频/爆音」的关键：cyndilib 的音频发送环只有 3 个槽
#   （MAX_FRAME_BUFFERS=3），若按采集块（1024 帧 ≈21ms）逐块发，环总容量仅 ~64ms，
#   采集循环一抖动就排空/溢出 ⇒ 接收端听到一簇簇短音。攒满 100ms 再发，
#   3 槽 = 300ms 储备，抖动被完全吸收。
_FRAME_SAMPLES = 4800

# ★★ 2026-10-07 定稿（砍掉时钟驱动后的全部发送参数，就这一个）。
#   `feed_audio` 一次调用最多发几帧 —— 只为防止极端积压时长时间霸占锁。
#   正常每块（1024 样本 ≈21ms）远攒不满一帧，恒为 0 或 1。
_MAX_FRAMES_PER_CALL = 4


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


class _BytePool:
    """定长 bytes 缓冲池（只用于**视频帧拷贝**，见 `NDIOutput.send_video`）。

    为什么需要：渲染线程每帧要把画布拷贝成独立缓冲（画布会被立刻覆写），
    1920×1080×4 = 8.3 MB。若每帧都 `bytes(...)` 新分配，等于每 16ms 分配+释放
    8 MB ⇒ 内存分配器压力大、易碎片化。

    做法：预放 `n` 个 `bytearray`，`acquire(size)` 取一个大小够用的（不够就补分配），
    提交线程用完调 `release(buf)` 归还。**池空时不等待、直接新分配** ——
    绝不能因为"没块可用"而让渲染线程卡住或丢帧（铁律 #0）。

    ⚠ 只支持单一大小的帧（我们同一会话里分辨率固定；换分辨率时会本对象重建）。
      不同大小的块分开存，不会互相污染。
    """

    def __init__(self, n=2):
        self._free = []          # 空闲 bytearray 列表
        self._cap = 0            # 已分配块的大小（首个 acquire 决定）
        self._n = int(n)
        self._lk = threading.Lock()

    def prealloc(self, size):
        """按 `size` 预分配 `n` 个块（`_ensure()` 成功后调用，避免首帧临时分配大对象）。

        预分配能显著降低"第一帧拷贝"的分配尖峰（实测首帧可达 10~25ms）。
        """
        try:
            size = int(size)
            if size <= 0:
                return
            with self._lk:
                if self._cap == size and len(self._free) >= self._n:
                    return
                self._cap = size
                self._free = [bytearray(size) for _ in range(self._n)]
        except Exception:                                          # noqa: BLE001
            pass

    def acquire(self, size):
        with self._lk:
            if self._free:
                b = self._free.pop()
                if len(b) == size:
                    return b
            # 池空 / 大小变了 ⇒ 新分配
            self._cap = size
            return bytearray(size)

    def release(self, buf):
        if buf is None:
            return
        try:
            with self._lk:
                if len(buf) == self._cap and len(self._free) < self._n:
                    self._free.append(buf)
        except Exception:                                          # noqa: BLE001
            pass


class NDIOutput:
    # ⚠⚠ `open()` 之后的**静默期**：cyndilib 实测在这个窗口内提交帧会**段错误**
    #   （native crash，Python 的 try/except 抓不住）。现场路径正好踩：
    #   视频线程刚建好 sender，音频线程 ~20ms 后就 feed_audio ⇒ 立刻崩/丢。
    _WARMUP_SECS = 0.3

    def __init__(self, name="EasyRealityAutoVJ"):
        self.name = name
        # ★★ 2026-10-08：**视频与音频各用一把锁**（原来是共用 `self._lock`）。
        #   原因见 `_vlock/_alock` 定义处（用户报「开了 NDI 音频之后画面掉帧」）。
        self._vlock = threading.Lock()   # 只保护视频帧提交（渲染线程）
        self._alock = threading.Lock()   # 只保护音频帧提交（NDI 音频转发线程）
        # ★★ 2026-10-08（第二轮，真凶）：**NDI SDK 内部把「视频提交」和「音频提交」
        #   串行化了** —— 实测（`tools/_ndi_chain_verify.py`）当音频线程在跑时，
        #   `send_video_async()` 的 native 调用会**等待 ~14ms**（音频不跑时 0.02ms），
        #   长尾 17.9ms，而且**与我们自己的锁无关**（换单锁也一样慢）。
        #   ⇒ 只要视频提交留在**渲染线程**上，开音频就必然拖慢整帧（预览/输出一起掉帧）。
        #   修法（与项目铁律「可能无限阻塞驱动的调用绝不持有别人需要的锁」同源）：
        #   **视频帧改成"入队"，由本类**自己的一条专用线程**去提交** ——
        #   渲染线程只做一次 O(1) 入队，永不接触 SDK。
        self._vq = None                  # 待提交的 (bgra_bytes, seq)（只留最新一帧）
        self._vcv = threading.Condition()
        self._vstop = False
        self._vthread = None
        self._vsend_ok = 0               # 真正提交成功的视频帧数（诊断）
        self._vsend_block_ms = 0.0       # 上次提交耗时（诊断：现场看是否被音频拖住）
        # ★★ 帧拷贝缓冲池（2026-10-08 第三轮，修"接收端闪黑屏"）：
        #   渲染线程入队前必须**拷贝**画布帧（画布每帧被覆写，见 `send_video` docstring）。
        #   直接 `bytes(bgra)` 每帧要新分配 + 释放 8.3 MB ⇒ 高频分配抖动（GC/内存碎片）。
        #   池里放 2 块轮换：渲染线程取块拷贝 → 提交线程用完归还。
        #   ⚠ 2 块够用：正常提交远快于 1/60s；真遇到阻塞时取不到块就退化成临时分配
        #     （**绝不因池空而丢帧或阻塞渲染线程** —— 这与铁律 #0 同源）。
        self._vbuf_pool = _BytePool(2)
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
        # ---- ★★ 2026-10-07 定长累积发送（修「声音断续/短促高频/爆音」）----
        #   根因：cyndilib 的音频发送环 **只有 3 个槽**（MAX_FRAME_BUFFERS=3，见
        #   send_frame_status.pxd），而采集侧每块只有 BLOCK=1024 帧 ≈21ms
        #   ⇒ 环总容量仅 ~64ms。采集循环任一次抖动（能量/拍钟/指纹分析、GC、调度）
        #   超过 64ms，环就被排空；缓过来后又连灌数块 ⇒ 环满、后面的块被
        #   `get_write_available()` 拦下直接丢（现场 `dropped` 持续增长即此）。
        #   更致命的是**节奏被拉得忽快忽慢**：接收端按每块自带的时间戳回放，
        #   块间隔被拖大 ⇒ 听感就是「一簇短音 → 停顿 → 一簇短音」，
        #   且音调被拉高（短促高频）、块边界爆音。
        #   修法：在发送端**自己攒够一整帧**（默认 100ms = 4800 样本 @48k，
        #   正好等于 `set_max_num_samples(4800)`）再 `write_data + send_audio`。
        #   这样发送节奏恒定、每块长度恒定，3 槽环 = 300ms 储备，抖动完全被吸收。
        self._pend = None        # 累积中的 (N, 2) float32 未发送样本
        self._pend_sr = 0        # 累积缓冲对应的采样率（变了就丢弃重来）
        self._audio_frames_sent = 0   # 真正发出的「整帧」数（诊断）
        self._audio_short = 0         # 退出时残留的不足一帧样本数（诊断）
        self._audio_start_logged = False   # 「已开始发送」只写一次（见 feed_audio）
        self._audio_call_ms = 0.0          # 上次 feed_audio 单次总耗时（诊断，含锁外 send）
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
                a.set_max_num_samples(_FRAME_SAMPLES)
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
            # ★ 预热帧拷贝池：按真实分辨率预分配 2 块，避免首帧临时分配 8MB 大对象
            #   （实测首帧分配尖峰可达 10~25ms；预热后稳定 ~1ms）。
            try:
                self._vbuf_pool.prealloc(int(w) * int(h) * 4)
            except Exception:                                      # noqa: BLE001
                pass
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
        """★ 渲染线程调用：**拷贝一帧后入队**（绝不接触 SDK），提交在 `_video_loop`。

        ⚠⚠ 2026-10-08 改（第二轮，真凶定位）：原实现在本方法里直接
        `write_data + send_video_async`。实测当音频在发时，`send_video_async()`
        的 native 调用会等 **~14ms**（NDI SDK 内部把音视频提交串行化了，与我们
        自己的锁无关）。而本方法跑在**渲染线程**上（`engine._tick_locked` →
        `_ndi_send`），那 14ms 直接把整帧节奏顶掉 ⇒ **预览和输出同时肉眼可见掉帧**。
        ⇒ 改成"入队 + 专用线程提交"：渲染线程永不接触 SDK。

        ⚠⚠⚠ 2026-10-08 修（第三轮，**接收端闪黑屏** —— 第二轮改造引入的回归）：
          第二轮把入队写成了**只存 `bgra` 引用**（`self._vq = (bgra, w, h)`）。
          但 `engine._ndi_send` 传进来的 `bgra` 是
          `np.frombuffer(self.canvas.bits(), ...)` —— 它是 **QImage 画布的零拷贝
          视图**，而 `self.canvas` 每帧都被 `_compose()` **重画（先清黑底再合成）**。
          旧代码是**同步** `write_data`：拷进 NDI buffer 后才返回，安全。
          新代码是**异步**：只塞了个引用就返回，渲染线程下一帧立刻覆写同一块内存
          ⇒ `_video_loop` 几毫秒后 `write_data(bgra)` 拿到的已经是"**清完还没画**"
          的中间态（全黑）或下一帧的错乱内容 ⇒ **接收端周期性闪黑**。
          （佐证：`engine._tick_locked` 给 GUI 投帧时专门做了**深拷贝**并注释
           "消除跨线程抢画布竞态" —— 同一个坑，我们这边漏了。）

          ⇒ 修法：**在渲染线程里立刻拷贝成独立 bytes**（`bytes(bgra)`）。
            1920×1080×4 = 8.3 MB，实测 memcpy ≈ 0.6ms，**远小于** 14ms 的阻塞，
            且只是顺序开销、不阻塞任何锁 ⇒ 帧节奏不受影响。
            ⚠ 绝不能再改回"存引用"，除非调用方传的是它自己的私有 buffer。
        """
        if not self._ensure(w, h):
            return
        try:
            need = int(w) * int(h) * 4
            if len(bgra) != need:
                self._last_err = "帧数据 %d 字节 != 期望 %d" % (len(bgra), need)
                return
            # ★★ 必须拷贝：`bgra` 通常是调用方画布的**活动视图**，异步提交时会被覆写。
            #
            # ⚠⚠⚠ 2026-10-08 修（本轮「OBS 收不到视频」的根因，视频一帧都发不出去）：
            #   ① **`bytearray[:] = numpy数组` 必抛 `TypeError`**
            #      （实测四种 numpy 形态全失败：只读 / 可写 / `frombuffer` 视图 / slice 视图。
            #        CPython 的 bytearray 切片赋值**不接受 numpy 数组**，必须显式转 memoryview。）
            #   ② 旧兜底写的是 `snap = bytes(bgra)` —— 这会把 `snap` 变成**不可变**对象；
            #      而 `_video_loop` 里 `v.write_data(snap)` 的形参是 Cython `uint8_t[:]`
            #      （**要求可写 buffer**）⇒ 必抛 `BufferError: Object is not writable.`
            #      ⇒ `_video_loop` 每帧都进 except、一帧都发不出去。
            #      症状：**源能被搜到、音频正常、就是没画面**（音频走另一条路径，不受影响）。
            #      实测对照：`write_data(bytes)` 必抛；`write_data(bytearray)` / `write_data(memoryview)` 正常。
            #   复现工具：`tools/_ndi_err_probe.py`（抓完整堆栈，日志里只有 str(e) 看不出真凶）。
            try:
                snap = self._vbuf_pool.acquire(len(bgra))
                snap[:] = memoryview(bgra)      # ★ 必须显式 memoryview（见上）
            except Exception:                                      # noqa: BLE001
                snap = bytearray(bgra)          # ★ 必须 bytearray —— 绝不能落到 bytes
            # ★ 只留最新一帧：队列里不堆 8MB 大图（与引擎 `_latest_frame` 同一约定）
            # ⚠ 覆盖旧帧时必须把旧块**归还缓冲池**：旧块的归属是「待 `_video_loop` 消费并 release」，
            #   若它还没被消费就被这里覆盖，则永远不会走到 `_video_loop` 的 release ⇒ 池逐渐变空、
            #   退化为每帧新分配 8MB（正是该池要避免的）。这里在 `_vcv` 临界区内**原子接管**旧块
            #   （`_video_loop` 的「取走并置 None」也在同一把锁内 ⇒ 不可能两边都拿到、重复归还）。
            _old = None
            with self._vcv:
                _old = self._vq
                self._vq = (snap, int(w), int(h))
                self._vcv.notify()
            if _old is not None:
                self._vbuf_pool.release(_old[0])
            self._ensure_vthread()
        except Exception as e:                                     # noqa: BLE001
            self._last_err = str(e)[:160]

    def _ensure_vthread(self):
        """确保视频提交线程在跑（首次入队时惰性启动）。"""
        t = self._vthread
        if t is not None and t.is_alive():
            return
        self._vstop = False
        self._vthread = threading.Thread(
            target=self._video_loop, daemon=True, name="autovj-ndi-video")
        self._vthread.start()

    def _video_loop(self):
        """视频提交线程：从队列取最新一帧 → `write_data + send_video_async`。

        ⚠ 这里是**唯一**接触视频帧 SDK 调用的地方；它慢（被音频挤到 ~14ms）
          也只影响"这一帧晚一点出去"，**绝不阻塞渲染线程**。

        ⚠⚠ 队列里的帧是**独立拷贝**（见 `send_video`）：本线程慢慢提交期间，
          渲染线程可以放心继续画它的画布，不会互相踩内存（这是修"闪黑屏"的关键）。
        """
        while not self._vstop:
            item = None
            try:
                with self._vcv:
                    if self._vq is None:
                        self._vcv.wait(0.2)
                    item = self._vq
                    self._vq = None
            except Exception:                                      # noqa: BLE001
                time.sleep(0.1)
                continue
            if item is None:
                continue
            snap, w, h = item
            t0 = time.perf_counter()
            try:
                with self._vlock:
                    # ⚠ 开门静默期：这段时间内提交帧会抛（视频）/崩（音频），一律丢掉
                    if time.perf_counter() - self._opened_at < self._WARMUP_SECS:
                        self._dropped += 1
                        continue
                    v = self._video
                    if v is None:
                        continue
                    # ⚠⚠ 必须先查「可写」再写：buffer 还没空出来时 `write_data` 会抛
                    #   （cyndilib `RuntimeError: no write frame available`）。
                    #   正确的做法是**丢掉这一帧**，既不等待、也不关输出。
                    if hasattr(v, "get_write_available") and not v.get_write_available():
                        self._dropped += 1
                        continue
                    v.write_data(snap)
                    if hasattr(self._sender, "send_video_async"):
                        self._sender.send_video_async()
                    else:
                        self._sender.send_video()
                self._vsend_ok += 1
            except Exception as e:                                 # noqa: BLE001
                # ⚠⚠ **绝不能**在这里 `enabled = False`（2026-10-01 实测踩到）：
                #   没有接收端连接时写视频帧会抛 `Object is not writable`，老代码就
                #   永久关掉整个 NDI 输出 ⇒ 接收端**后面再接上来也永远没画面/没声音**。
                #   正确做法：丢掉这一帧、记一次日志，保持 enabled，下一帧继续尝试。
                self._last_err = str(e)[:160]
                self._dropped += 1
                if self._audio_err_logged is None and not self._err_logged:
                    self._err_logged = self._last_err
                    _log("!! NDI 发送视频失败（只记一次，后续继续尝试）：%s" % self._last_err)
            finally:
                self._vsend_block_ms = (time.perf_counter() - t0) * 1000.0
                # ★ 归还缓冲池（本轮不再用这帧数据；write_data 已把内容拷进 SDK buffer）
                self._vbuf_pool.release(snap)

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
            _do_send = False
            _t_entry = time.perf_counter()   # ★ 本次调用起点（统计总耗时，见函数末尾）
            with self._alock:
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
                if ch < 2:
                    # 单声道兜底：复制成双声道（NDI 帧固定 2ch，写单声道会报
                    # "number of channels must match"）
                    pcm = _np.repeat(pcm, 2, axis=1)
                    ch = 2
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
                # ★★ 定长累积（见 __init__ 字段注释）：攒够 _FRAME_SAMPLES 才发一块。
                #   未满则留在 self._pend，绝不半帧发送 —— 半帧会让接收端时间轴抖动。
                if sr and int(sr) != self._pend_sr:
                    # 采样率变了（换设备）⇒ 旧累积作废，避免拼出错误时长
                    self._pend = None
                    self._pend_sr = int(sr) if sr else _AUDIO_SR
                new = pcm if pcm.shape[1] == 2 else _np.repeat(pcm, 2, axis=1)
                if self._pend is None or len(self._pend) == 0:
                    buf = new
                else:
                    buf = _np.concatenate([self._pend, new], axis=0)
                # ★★ 2026-10-07 定稿：回归 **NDI 官方立场** —— 发送端原样送样本。
                #
                #   为什么砍掉之前那套「时钟驱动 + 线性外推 + 追赶丢弃」（用户质问
                #   「市面上没有成熟方案吗？」后重新查证的结论）：
                #
                #   **NDI 官方明确不做 PTP 时钟同步。** 发送端只该「原样送样本」，
                #   收发两端那点时钟差（本机实测采集比实时慢 ~0.9%）由**接收端**
                #   用高质量重采样器吃掉 —— NDI SDK 文档原话：
                #       "they will be resampled with high quality to adjust for
                #        small clock rate differences"
                #   接收端（OBS / vMix / Resolume）内部都有这个重采样器；我们
                #   在发送端手写一个，等于把接收端的活塞进发送端，而且是用线性
                #   外推这种最差的方式（实测把正弦放大 23 倍 ⇒ 更响的爆音）。
                #
                #   现在只做两件事，都有明确依据：
                #     ① **定长累积到 _FRAME_SAMPLES 再发** —— 修「断续/短促高频/爆音」
                #        （cyndilib 音频发送环只有 3 槽，按 21ms 小块发会让环容量只剩
                #         ~64ms，采集一抖动就排空；攒到 100ms ⇒ 3 槽 = 300ms 储备）。
                #     ② **缓冲满就跳过这一帧，绝不补零、绝不外推、绝不伪造样本**
                #        —— 那 0.9% 的时钟差交给接收端；伪造出来的样本是**错误波形**，
                #        比少一小段更糟（少一小段接收端会平滑掉，错误波形会直接响）。
                #
                #   ⚠ 本函数现在**没有任何跨调用的时钟状态**（无 _t0 / _frames_clock /
                #     _last_tail / _extrapolate）—— 状态越少，现场越不会出玄学。
                sent_any = False
                # ★ 本函数会**先写环、出锁后再 send_audio**（见循环内说明）。
                #   这个标志记录「本次是否已 write_data 了至少一帧，需要在锁外提交」。
                _do_send = False
                # 一次调用里最多发多少帧：正常 1 帧（每块 21ms，攒满 100ms 才够），
                # 上限只是防止极端积压时长时间霸占锁。
                # ⚠ 由于 send_audio 已移到锁外，这里最多只会 write 一帧
                #   （循环第二轮起 `get_write_available()` 会因上一帧未提交而返回 False）。
                for _ in range(_MAX_FRAMES_PER_CALL):
                    if len(buf) < _FRAME_SAMPLES:
                        break
                    chunk = buf[:_FRAME_SAMPLES]
                    buf = buf[_FRAME_SAMPLES:]
                    # 交错 (n, ch) → 平面 (ch, n)，并保证 C 连续（cyndilib 直接按缓冲读）
                    planar = _np.ascontiguousarray(chunk.T)
                    # ★ 缓冲没空出来（接收端慢/抖动）⇒ **跳过这一帧**，样本塞回 buf 头部
                    #   等下一轮。不补零、不造样本，宁可晚 20ms 也不出错误波形。
                    if hasattr(a, "get_write_available") and not a.get_write_available():
                        buf = _np.concatenate([chunk, buf], axis=0)
                        self._audio_dropped += 1
                        break
                    try:
                        a.write_data(planar)
                    except Exception as _we:
                        buf = _np.concatenate([chunk, buf], axis=0)
                        self._audio_dropped += 1
                        self._audio_err = "%s: %s" % (type(_we).__name__, str(_we)[:120])
                        break
                    # ★★ `send_audio()` **必须在锁外**调用（2026-10-08 修「开音频就掉帧」）。
                    #   实测（`tools/_ndi_sendaudio_block.py`）：没有接收端排空时，
                    #   `send_audio()` 的 native 调用**每次阻塞 ~94ms**（59/60 次 > 20ms）。
                    #   旧代码在 `with self._alock` 里调它 ⇒ 渲染线程的 `send_video()`
                    #   撞上同一把锁就要干等 94ms ⇒ 一帧 16.7ms 的节奏直接崩掉 ⇒
                    #   **预览和输出同时肉眼可见掉帧**（用户报的现象）。
                    #   这里只**记下待提交标志**，真正的 `send_audio()` 在锁外做（见下方）。
                    _do_send = True
                # 剩余的不足一帧，留到下次
                self._pend = buf if len(buf) else None
                # 累积缓冲兜底上限：万一下游一直发不出去（连不上接收端），
                # 不能让 _pend 无限增长 ⇒ 超过 1 秒就只留最新 1 秒。
                if self._pend is not None and len(self._pend) > _AUDIO_SR:
                    self._pend = self._pend[-_AUDIO_SR:]
                    self._audio_short += 1
                # ⚠ 不再用「连接数门控」（2026-10-01 实测修正）：
                #   原来以为"没有接收端时 send_audio() 会段错误"，实测**不成立**
                #   （无接收端时它只是返回 False，真正会崩的是 p_metadata 野指针，已修）。
                #   而 `get_num_connections()` 在「接收端刚连上、计数还没上报」的窗口返回 0，
                #   门控会把那一段音频**整块丢掉且毫无日志** —— 正是"没有声音"的隐形帮凶。
                #   现在改成：发不出去就丢这一帧，并计数 + 周期性打日志（见上面的发送循环）。
                # ★★ 2026-10-07 修「已开始发送」反复刷屏：原判据 `_audio_ok == 1`
                #   看似"只打一次"，实则只要中间出现过写失败（_audio_ok 不变但
                #   下面会走到 else 分支... 更准确地说：`_audio_ok` 只增不减，
                #   所以 ==1 确实是瞬态）—— 但现场日志里它**每 20 秒重复一次**，
                #   说明 `_audio_ok` 被重置过（重建会话/开关切换）。
                #   改用**一次性布尔标志**：整个会话生命周期里只写一行，
                #   不依赖任何计数的瞬态值。
                if not self._audio_start_logged:
                    self._audio_start_logged = True
                    _log("音频已开始发送（%.1f kHz → %.1f kHz / 2ch）"
                         % ((sr or _AUDIO_SR) / 1000.0, _AUDIO_SR / 1000.0), key="audio-start")
                # 每 ~10 秒给一行心跳，现场据此就能判断"到底有没有在发"
                now = time.perf_counter()
                if now - getattr(self, "_audio_last_log", 0.0) >= 10.0:
                    self._audio_last_log = now
                    # ⚠⚠ 2026-10-07 修：原 key 是 `"audio-hb-%d" % (self._audio_ok // max(1, self._audio_ok))`
                    #   —— 当 `_audio_ok >= 1` 时它恒等于 `audio_ok // audio_ok = 1`
                    #   ⇒ key 永远是 `audio-hb-1` ⇒ **心跳这辈子只写一次**，
                    #   之后再想从日志看"音频还在不在发"就完全看不到。
                    #   现改成**按 60 秒时间桶**做 key：每分钟一行，既不刷屏又能看出走势。
                    bucket = int(now) // 60
                    # ★ 诊断量只剩 pend（当前积压样本）。**持续单调增长**说明发送跟不上
                    #   （接收端慢 / 环满），是「破音」现场唯一需要看的量；
                    #   正常应在 0 ~ 1 帧之间小幅波动。
                    _log("audio: src=%d -> %d, ok=%d dropped=%d resampled=%d conns=%d "
                         "pend=%d call_ms=%.1f err=%s"
                         % (int(sr or 0), _AUDIO_SR, self._audio_ok, self._audio_dropped,
                            self._audio_resampled, self._conns(),
                            0 if self._pend is None else len(self._pend),
                            getattr(self, "_audio_call_ms", 0.0),
                            self._audio_err or "-"),
                         key="audio-hb-%d" % bucket)
            # ★★ 真正的提交：**已经在锁外**了（见上面 `_do_send` 说明）。
            #
            #   这一步可能阻塞 ~94ms（无接收端排空时），但它**不再持有任何锁**
            #   ⇒ 渲染线程的 `send_video()` 完全不受影响，帧节奏保持不变。
            #
            #   ⚠ 为什么可以在锁外调用：`send_audio()` 只从 cyndilib 的音频发送环
            #     取已写好的帧推给 SDK，**不触碰视频帧对象**，也不读我们的任何状态；
            #     `write_data`（在锁内）已经完成了对环的写入。所以这里出去执行是安全的。
            #   ⚠ 万一它真的抛异常，走下面的 `except` 统一处理（含"环卡死 → 重建会话"）。
            if _do_send:
                ok1 = True
                try:
                    s_ = self._sender
                    if s_ is not None and hasattr(s_, "send_audio"):
                        ok1 = bool(s_.send_audio())
                except Exception as e:                             # noqa: BLE001
                    ok1 = False
                    self._audio_err = "%s: %s" % (type(e).__name__, str(e)[:120])
                if ok1:
                    self._audio_ok += 1
                    self._audio_frames_sent += 1
                else:
                    self._audio_dropped += 1
            # ★ 在**锁外**记录"音频侧单次总耗时"，供心跳诊断（现场可看 send_audio 是否卡）。
            try:
                self._audio_call_ms = (time.perf_counter() - _t_entry) * 1000.0
            except Exception:                                      # noqa: BLE001
                pass
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
        # ★ 先停视频提交线程（它可能正卡在 SDK 里，用超时 join 兜底）
        self._vstop = True
        try:
            with self._vcv:
                self._vq = None
                self._vcv.notify_all()
        except Exception:                                          # noqa: BLE001
            pass
        t = self._vthread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=1.0)
            except Exception:                                      # noqa: BLE001
                pass
        self._vthread = None
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
        # ★ 累积缓冲一并清空：重开输出时若有残留样本，会拼进新会话的第一帧
        #   ⇒ 开头多出几十毫秒的旧声音（且采样率可能已变）。
        self._pend = None


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
