/*
 * ASIO 低延迟采集 —— 原生宿主 DLL
 * ============================================================================
 * 给 Python 侧（`src/asio_engine.py`，ctypes）用的极简 ASIO 宿主。
 *
 * ## 为什么要自己写
 * 项目原来用 `soundcard` 采音频，但它**只封 WASAPI**，加不了 ASIO；
 * 而 `sounddevice`/PortAudio 的官方预编译 DLL **没有编进 ASIO**
 * （覆盖 2026-10-01 实测：host API 只有 MME / DirectSound / WASAPI / WDM-KS）。
 * 所以要低延迟就只能自己当 ASIO 宿主。
 *
 * ## 依赖
 * 用 Steinberg 官方 ASIO SDK 的 `asio.cpp` / `asiodrivers.cpp` / `asiolist.cpp`
 * （**构建时下载，不进 git** —— SDK 许可禁止再分发其源码，但允许分发编译产物）。
 * 由 `tools/build_asio.py` 用 zig 编译成 `src/_asiohost.dll`。
 *
 * ## 设计要点（每一条都对应一个真实的坑）
 * 1. **回调里绝不做任何可能阻塞的事**：ASIO 的 `bufferSwitch` 跑在驱动的高优先级线程上，
 *    里面 malloc / 加锁 / 打日志都会导致爆音甚至驱动崩。
 *    这里只做「读样本 → 格式转换 → 写环形缓冲」，全是算术和内存写。
 * 2. **环形缓冲一次分配**（`avj_asio_open` 里 malloc 一次，`close` 里释放），
 *    容量取 2 的幂 ⇒ 取模用位与。
 * 3. **单生产者单消费者**：写指针只由回调线程推进，读指针只由 Python 读线程推进，
 *    用 volatile + 单调递增的帧号，不加锁。
 *    缓冲满时**丢最老的**（宁可丢一点音频，也绝不让驱动线程等）。
 * 4. **先建缓冲再 start**（ASIO 规范），关闭顺序相反：stop → disposeBuffers → ASIOExit。
 * 5. 采样类型要按 `ASIOGetChannelInfo().type` 分派（不同驱动/通道可能不同）：
 *    常见 Int32LSB / Int16LSB / Int24LSB / Float32LSB。
 * 6. 只开**两路输入**（第 ch0 / ch1 路）。有些驱动要求同时开输出通道，
 *    所以 `createBuffers` 输入-only 失败时会**自动重试加入两路输出**。
 * 7. 采样率默认**沿用驱动当前值**（ASIO 的采样率是驱动全局的，改了会影响
 *    同一驱动的其它客户端，比如 VirtualDJ）—— 传 sr=0 即是此意。
 */

#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>

#include "asiosys.h"
#include "asio.h"
#include "iasiodrv.h"
#include "asiodrivers.h"

#define AVJ_MAX_DRV     16
#define AVJ_RING_FRAMES (1 << 17)      /* 131072 帧 ≈ 2.7 秒 @48k，立体声 float32 = 1 MB */

/* SDK 在 asio.cpp 里定义了这两个全局：`asioDrivers` 是宿主枚举/装载器，
   `theAsioDriver` 是当前装载的 IASIO*（由 AsioDrivers::loadDriver 填）。
   ⚠ 它们只在 asio.cpp 内部可见，这里必须自己 extern 一份。 */
extern AsioDrivers* asioDrivers;
extern IASIO* theAsioDriver;

/* ---------------------------------------------------------------------------
 * 最小 C++ 运行时垫片
 * ---------------------------------------------------------------------------
 * 为什么需要它（2026-10-01 实测）：
 *   · zig 在本机**编不出** libcxx/libunwind（"sub-compilation of libunwind failed"），
 *     所以 `zig c++` 默认这条路走不通；
 *   · 但 ASIO SDK 的宿主代码只用到 `new`（`new AsioDrivers()`）和几个 C 函数，
 *     并不需要真正的 C++ 标准库。
 *   ⇒ 用 `-nostdinc++ -fno-exceptions -fno-rtti` 编译，并在这里自己给出
 *     `operator new/delete`（直接落在 malloc/free 上）。
 *     好处：**产物不依赖 libstdc++-6.dll / libcxx 等运行时 DLL**，打包干净。
 * --------------------------------------------------------------------------- */
void* operator new(size_t n)
{
    void* p = malloc(n ? n : 1);
    if (!p) abort();
    return p;
}
void* operator new[](size_t n) { return operator new(n); }
void  operator delete(void* p) noexcept { free(p); }
void  operator delete[](void* p) noexcept { free(p); }
void  operator delete(void* p, size_t) noexcept { free(p); }
void  operator delete[](void* p, size_t) noexcept { free(p); }

/* ---------------------------------------------------------------- 状态 */
static float*          g_ring   = NULL;
static LONG            g_cap    = AVJ_RING_FRAMES;   /* 2 的幂 */
static volatile LONG   g_w      = 0;                 /* 已写入的帧数（单调递增） */
static volatile LONG   g_r      = 0;                 /* 已读出的帧数（单调递增） */
static int             g_ch[2]  = {0, 1};            /* 用哪两路输入 */
static ASIOBufferInfo  g_bi[2];
static ASIOBufferInfo  g_bo[2];
static ASIOCallbacks   g_cb;
static long            g_bs     = 0;                 /* 缓冲帧数 */
static double          g_sr     = 48000.0;
static ASIOSampleType  g_st[2]  = {ASIOSTInt32LSB, ASIOSTInt32LSB};
static volatile LONG   g_blocks = 0;                 /* 回调次数 */
static volatile LONG   g_over   = 0;                 /* 溢出丢弃次数 */
static int             g_open   = 0;
static int             g_com    = 0;                 /* 本 DLL 是否做了 CoInitialize */
static long            g_lat_i  = 0, g_lat_o = 0;
static volatile LONG   g_srchg  = 0;
static char            g_err[512] = "";
static int             g_need_out = 0;               /* 是否连输出通道一起开了 */

static void set_err(const char* fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_err, sizeof(g_err), fmt, ap);
    va_end(ap);
}

/* ---------------------------------------------------------------- 样本转换 */
static inline float to_f(const void* p, ASIOSampleType t, long i)
{
    switch (t) {
    case ASIOSTFloat32LSB: return ((const float*)p)[i];
    case ASIOSTFloat64LSB: return (float)((const double*)p)[i];
    case ASIOSTInt16LSB:   return (float)(((const short*)p)[i] * (1.0 / 32768.0));
    case ASIOSTInt16MSB:
        return (float)((short)(((const unsigned char*)p)[i * 2] << 8 |
                               ((const unsigned char*)p)[i * 2 + 1]) * (1.0 / 32768.0));
    case ASIOSTInt24LSB: {
        const unsigned char* b = (const unsigned char*)p + i * 3;
        int v = (b[0] << 8) | (b[1] << 16) | (b[2] << 24);
        return (float)(v * (1.0 / 2147483648.0));
    }
    case ASIOSTInt24MSB: {
        const unsigned char* b = (const unsigned char*)p + i * 3;
        int v = (b[0] << 24) | (b[1] << 16) | (b[2] << 8);
        return (float)(v * (1.0 / 2147483648.0));
    }
    case ASIOSTInt32MSB: {
        const unsigned char* b = (const unsigned char*)p + i * 4;
        int v = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3];
        return (float)(v * (1.0 / 2147483648.0));
    }
    case ASIOSTInt32LSB:
    default:
        return (float)(((const int*)p)[i] * (1.0 / 2147483648.0));
    }
}

/* ---------------------------------------------------------------- 回调核心 */
/* ⚠⚠ 这个函数跑在驱动的高优先级线程上：只准算术 + 内存写。
   不许 malloc / 加锁 / 打印 / 调 Python。 */
static void avj_switch(long index)
{
    if (!g_open || !g_ring) return;

    const long bs = g_bs;
    const LONG m  = g_cap - 1;
    LONG w = g_w;
    LONG r = g_r;

    if (w - r + bs > g_cap) {              /* 空间不够 ⇒ 丢最老的，绝不等 */
        r = w + bs - g_cap;
        g_r = r;
        InterlockedIncrement(&g_over);
    }

    const void* p0 = g_bi[0].buffers[index];
    const void* p1 = g_bi[1].buffers[index];
    const ASIOSampleType t0 = g_st[0];
    const ASIOSampleType t1 = g_st[1];
    float* ring = g_ring;
    LONG i;

    for (i = 0; i < bs; i++) {
        const LONG idx = (w + i) & m;
        ring[idx * 2]     = to_f(p0, t0, i);
        ring[idx * 2 + 1] = to_f(p1, t1, i);
    }
    g_w = w + bs;
    InterlockedIncrement(&g_blocks);

    /* ⚠⚠ 关键：建了输出缓冲就必须在这里告诉驱动「输出已就绪」。
       2026-10-01 实测：只建输入缓冲时 Realtek ASIO **一次回调都不给**
       （open 成功、ASIOStart 成功，但 blocks 恒为 0）⇒ 必须像官方 hostsample 那样
       连输出一起建，并在回调里 ASIOOutputReady()。 */
    if (g_need_out) ASIOOutputReady();
}

static void avj_buffer_switch(long index, ASIOBool /*processNow*/)
{
    avj_switch(index);
}

static ASIOTime* avj_buffer_switch_ti(ASIOTime* params, long index, ASIOBool /*direct*/)
{
    avj_switch(index);
    return params;
}

static void avj_sr_change(ASIOSampleRate sRate)
{
    g_sr = (double)sRate;
    InterlockedIncrement(&g_srchg);
}

static long avj_asio_msg(long selector, long value, void* /*message*/, double* /*opt*/)
{
    /* ⚠ 驱动会通过这里问"你支持哪些能力"。**全返回 0 是不对的**：
       官方 hostsample 至少要对 kAsioEngineVersion 回 2（声明自己支持 ASIO 2.0），
       并对它认识的 selector 回 1。返回 0 会让某些驱动认为宿主能力不足而**不启动回调**
       （2026-10-01 实测：Realtek ASIO 在 open/Start 都成功的情况下 blocks 恒 0，
        补齐这里的应答是排查的一部分）。 */
    static int g_reset_req = 0;
    switch (selector) {
    case kAsioSelectorSupported:
        switch (value) {
        case kAsioResetRequest:
        case kAsioEngineVersion:
        case kAsioResyncRequest:
        case kAsioLatenciesChanged:
        case kAsioSupportsTimeInfo:
        case kAsioSupportsTimeCode:
        case kAsioSupportsInputMonitor:
            return 1;
        default:
            return 0;
        }
    case kAsioEngineVersion:
        return 2;
    case kAsioResetRequest:
        g_reset_req = 1;
        return 1;
    case kAsioResyncRequest:
    case kAsioLatenciesChanged:
    case kAsioSupportsInputMonitor:
        return 1;
    case kAsioSupportsTimeInfo:
    case kAsioSupportsTimeCode:
        return 0;
    default:
        return 0;
    }
}

/* ---------------------------------------------------------------- 枚举 */
extern "C" __declspec(dllexport) int avj_asio_count(void)
{
    if (!asioDrivers) asioDrivers = new AsioDrivers();
    if (!asioDrivers) return 0;
    long n = asioDrivers->asioGetNumDev();
    return (n < 0) ? 0 : (int)n;
}

extern "C" __declspec(dllexport) int avj_asio_name(int idx, char* buf, int cap)
{
    if (!asioDrivers || !buf || cap <= 0) return -1;
    buf[0] = 0;
    if (asioDrivers->asioGetDriverName(idx, buf, cap) != 0) return -2;
    return 0;
}

extern "C" __declspec(dllexport) const char* avj_asio_last_error(void)
{
    return g_err;
}

extern "C" __declspec(dllexport) int avj_asio_is_open(void) { return g_open; }

extern "C" __declspec(dllexport) int avj_asio_cur_rate(void) { return (int)(g_sr + 0.5); }

extern "C" __declspec(dllexport) int avj_asio_buffer_frames(void) { return (int)g_bs; }

extern "C" __declspec(dllexport) int avj_asio_latency_frames(int* in_frames, int* out_frames)
{
    if (in_frames)  *in_frames  = (int)g_lat_i;
    if (out_frames) *out_frames = (int)g_lat_o;
    return 0;
}

extern "C" __declspec(dllexport) int avj_asio_blocks(void) { return (int)g_blocks; }

extern "C" __declspec(dllexport) int avj_asio_overflows(void) { return (int)g_over; }

extern "C" __declspec(dllexport) int avj_asio_rate_changed(void)
{
    return (int)InterlockedExchange(&g_srchg, 0);
}

/* 还没读走多少帧（供 UI 显示"是否在流动"） */
extern "C" __declspec(dllexport) int avj_asio_pending(void)
{
    return (int)(g_w - g_r);
}

/* 一行诊断信息（排查"打开了但没数据"这类问题非常有用） */
extern "C" __declspec(dllexport) int avj_asio_debug(char* buf, int cap)
{
    long nin = 0, nout = 0;
    if (g_open) ASIOGetChannels(&nin, &nout);
    return snprintf(buf, cap,
                    "open=%d in=%ld out=%ld ch=%d,%d type=%d,%d bs=%ld sr=%.0f "
                    "need_out=%d blocks=%d over=%d pending=%d lat_in=%ld",
                    g_open, nin, nout, g_ch[0], g_ch[1], (int)g_st[0], (int)g_st[1],
                    g_bs, g_sr, g_need_out,
                    (int)g_blocks, (int)g_over, (int)(g_w - g_r), g_lat_i);
}

/* ---------------------------------------------------------------- 打开 */
/*
 * 返回 0 = 成功；负数为错误码：
 *   -1 已经打开   -2 找不到该驱动   -3 装载驱动失败（多半被别的程序独占）
 *   -4 ASIOInit 失败   -5 输入通道不足（<2）
 *   -6 采样率不被支持   -7 取缓冲范围失败   -8 建缓冲失败   -9 启动失败
 * out_sr / out_buf 回填实际采样率与缓冲帧数；err 回填可读错误（也可用 avj_asio_last_error）。
 */
extern "C" __declspec(dllexport) int avj_asio_open(const char* drv_name, double want_sr,
                                                   int ch0, int ch1, int want_buf,
                                                   int* out_sr, int* out_buf,
                                                   char* err, int errcap)
{
    char names[AVJ_MAX_DRV][64];
    char* ptrs[AVJ_MAX_DRV];
    int i, n;
    long nin = 0, nout = 0;
    ASIODriverInfo info;
    ASIOError e;

    if (g_open) { set_err("已经打开了"); if (err) snprintf(err, errcap, "%s", g_err); return -1; }
    if (!asioDrivers) asioDrivers = new AsioDrivers();
    if (!asioDrivers) { set_err("无法创建 AsioDrivers"); if (err) snprintf(err, errcap, "%s", g_err); return -3; }

    /* ---------------------------------------------------------------
     * COM：**必须是在 STA（单线程套间）线程上实例化驱动**
     * ---------------------------------------------------------------
     * ⚠⚠ 2026-10-01 实测踩到：
     *   ASIO 驱动注册的是一个 in-proc COM 组件，而且**把自己的 CLSID 当接口 IID 用**
     *   （`CoCreateInstance(clsid, ..., clsid, ...)`，见 SDK 的 asiolist.cpp）。
     *   这种"自引用 IID"没法通过代理/桩(marshaling)跨套间传递 ⇒
     *   从 **MTA** 线程实例化会失败（表现为 `loadDriver` 返回失败）。
     *   本项目的音频工作线程因为 `soundcard` 已经做过 `CoInitializeEx(MTA)`，
     *   所以**在那里直接调 open 一定失败**（一开始就是这么踩的：
     *   主线程（STA）测通了，换成真实采集线程（MTA）就报"装载驱动失败/被独占"）。
     *   ⇒ 两条都要满足：① 这里申请 **APARTMENTTHREADED**；
     *     ② Python 侧（`asio_engine.AsioCapture`）把 open/close 放到**自建 STA 线程**上跑。
     *   ⚠ 只有在**本 DLL 成功初始化**时才在 close 里 CoUninitialize，
     *     否则会把别人的引用计数减掉。
     */
    {
        HRESULT hr = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
        g_com = (hr == S_OK || hr == S_FALSE) ? 1 : 0;
    }

    /* 按名字找序号（驱动名可能有重音/空格，逐字比较） */
    for (i = 0; i < AVJ_MAX_DRV; i++) ptrs[i] = names[i];
    n = (int)asioDrivers->getDriverNames(ptrs, AVJ_MAX_DRV);
    {
        int found = -1;
        for (i = 0; i < n; i++) {
            if (drv_name && strcmp(names[i], drv_name) == 0) { found = i; break; }
        }
        if (found < 0) {
            /* ⚠ 这里**不回显驱动名**：名字来自注册表（ANSI/mbcs），而本文件的字符串常量是
               UTF-8 —— 混在一起会让调用方解码出乱码。名字由 Python 侧拼进去。 */
            set_err("找不到该 ASIO 驱动（本机共注册了 %d 个）。"
                    "请点「重新扫描」刷新设备列表后重选。", n);
            if (err) snprintf(err, errcap, "%s", g_err);
            if (g_com) { CoUninitialize(); g_com = 0; }
            return -2;
        }
    }

    if (!asioDrivers->loadDriver((char*)drv_name)) {
        /* 同样不回显名字（编码原因，见上）。 */
        set_err("装载该 ASIO 驱动失败。最常见原因是它**已经被别的程序独占**"
                "（ASIO 是单客户端 —— VirtualDJ / DAW 开着的时候我们就打不开）。");
        if (err) snprintf(err, errcap, "%s", g_err);
        if (g_com) { CoUninitialize(); g_com = 0; }
        return -3;
    }

    memset(&info, 0, sizeof(info));
    info.sysRef = NULL;
    e = ASIOInit(&info);
    if (e != ASE_OK) {
        set_err("ASIOInit 失败（码 %d）：%s", (int)e, info.errorMessage);
        if (err) snprintf(err, errcap, "%s", g_err);
        ASIOExit();
        if (g_com) { CoUninitialize(); g_com = 0; }
        return -4;
    }

    if (ASIOGetChannels(&nin, &nout) != ASE_OK) { nin = nout = 0; }
    if (nin < 2) {
        set_err("该驱动只有 %ld 路输入（至少要 2 路）", nin);
        if (err) snprintf(err, errcap, "%s", g_err);
        ASIOExit();
        if (g_com) { CoUninitialize(); g_com = 0; }
        return -5;
    }

    /* ---------------------------------------------------------------
     * 采样率：**必须显式设一次**，哪怕就是驱动当前值
     * ---------------------------------------------------------------
     * ⚠⚠ 2026-10-01 实测（Realtek ASIO）：
     *   不调 `ASIOSetSampleRate` 时，`ASIOInit` / `ASIOCreateBuffers` / `ASIOStart`
     *   **全部返回成功**，但驱动**一次回调都不给**（blocks 恒 0、收不到任何样本）。
     *   只要显式设一次（哪怕设成它自己报的 48000），回调立刻正常来
     *   （0.8 秒收 39600 帧 / 75 次回调），而且最小缓冲还从 1024 降到 **528 帧（≈11ms）**。
     *   ⇒ 结论：**永远显式设一次采样率**；want_sr<=0 表示"用当前值"，那就把当前值再设一遍。
     *     其它驱动设成相同值也是合法操作，不会有害。
     */
    {
        ASIOSampleRate cur = 0;
        double want;
        if (ASIOGetSampleRate(&cur) != ASE_OK || cur < 0.5) cur = 48000.0;
        want = (want_sr > 0.5) ? want_sr : (double)cur;
        if (ASIOSetSampleRate(want) != ASE_OK) {
            if (want_sr > 0.5) {                     /* 用户显式指定的采样率不支持 ⇒ 真失败 */
                set_err("驱动不支持 %.0f Hz 采样率", want);
                if (err) snprintf(err, errcap, "%s", g_err);
                ASIOExit();
                if (g_com) { CoUninitialize(); g_com = 0; }
                return -6;
            }
            /* 沿用当前值却也设失败：记一笔，继续（有些驱动不允许重复设置） */
            set_err("警告：ASIOSetSampleRate(%.0f) 失败，继续按当前采样率尝试", want);
        }
        if (ASIOGetSampleRate(&cur) == ASE_OK && cur > 0.5) g_sr = (double)cur;
    }

    {
        long mn = 0, mx = 0, pref = 0, gran = 0;
        long bs;
        if (ASIOGetBufferSize(&mn, &mx, &pref, &gran) != ASE_OK) {
            set_err("取不到缓冲范围");
            if (err) snprintf(err, errcap, "%s", g_err);
            ASIOExit();
            if (g_com) { CoUninitialize(); g_com = 0; }
            return -7;
        }
        /* 低延迟的关键：**尽量取最小缓冲**（用户指定值优先，钳到合法范围并对齐粒度） */
        bs = (want_buf > 0) ? want_buf : mn;
        if (bs < mn) bs = mn;
        if (bs > mx) bs = mx;
        if (gran > 1) bs = mn + ((bs - mn) / gran) * gran;
        g_bs = bs;
    }

    /* 环形缓冲：一次分配 */
    if (!g_ring) {
        g_ring = (float*)malloc((size_t)g_cap * 2 * sizeof(float));
        if (!g_ring) {
            set_err("分配环形缓冲失败（%d 帧）", (int)g_cap);
            if (err) snprintf(err, errcap, "%s", g_err);
            ASIOExit();
            if (g_com) { CoUninitialize(); g_com = 0; }
            return -8;
        }
    }
    g_w = g_r = 0;
    g_blocks = 0; g_over = 0;
    g_ch[0] = (ch0 >= 0 && ch0 < nin) ? ch0 : 0;
    g_ch[1] = (ch1 >= 0 && ch1 < nin) ? ch1 : (g_ch[0] + 1 < nin ? g_ch[0] + 1 : g_ch[0]);

    memset(g_bi, 0, sizeof(g_bi));
    memset(g_bo, 0, sizeof(g_bo));
    g_bi[0].isInput = ASIOTrue; g_bi[0].channelNum = g_ch[0];
    g_bi[1].isInput = ASIOTrue; g_bi[1].channelNum = g_ch[1];
    for (i = 0; i < 2; i++) {
        ASIOChannelInfo ci;
        memset(&ci, 0, sizeof(ci));
        ci.channel = g_ch[i];
        ci.isInput = ASIOTrue;
        if (ASIOGetChannelInfo(&ci) == ASE_OK) g_st[i] = ci.type;
    }

    memset(&g_cb, 0, sizeof(g_cb));
    g_cb.bufferSwitch         = &avj_buffer_switch;
    g_cb.sampleRateDidChange  = &avj_sr_change;
    g_cb.asioMessage          = &avj_asio_msg;
    g_cb.bufferSwitchTimeInfo = &avj_buffer_switch_ti;

    /* ---------------------------------------------------------------
     * 建缓冲：**2 路输入 + 2 路输出**（官方 hostsample 的做法）
     * ---------------------------------------------------------------
     * ⚠⚠ 2026-10-01 实测踩到：只建**输入**缓冲时，Realtek ASIO
     *   `ASIOInit`/`ASIOCreateBuffers`/`ASIOStart` **全部返回成功**，
     *   但回调 `bufferSwitch` **一次都不触发**（blocks 恒 0、一个样本都收不到）。
     *   原因：ASIO 的回调时钟是跟着**输出**走的 —— 纯输入宿主在很多驱动上不成立。
     *   ⇒ 连输出一起建（我们并不往输出写数据，只是让驱动把时钟跑起来），
     *     并在回调里 `ASIOOutputReady()` 告诉驱动"输出已就绪"。
     *   若该驱动没有输出通道、或拒绝混合建缓冲，再退回纯输入。
     */
    {
        int nb = 2;
        ASIOBufferInfo tmp[4];
        memcpy(tmp, g_bi, sizeof(g_bi));
        memset(g_bo, 0, sizeof(g_bo));
        if (nout >= 2) {
            g_bo[0].isInput = ASIOFalse; g_bo[0].channelNum = 0;
            g_bo[1].isInput = ASIOFalse; g_bo[1].channelNum = 1;
            memcpy(tmp + 2, g_bo, sizeof(g_bo));
            nb = 4;
        } else if (nout == 1) {
            g_bo[0].isInput = ASIOFalse; g_bo[0].channelNum = 0;
            memcpy(tmp + 2, g_bo, sizeof(g_bo[0]));
            nb = 3;
        }
        e = ASIOCreateBuffers(tmp, nb, g_bs, &g_cb);
        if (e == ASE_OK) {
            g_bi[0] = tmp[0]; g_bi[1] = tmp[1];
            g_need_out = (nb > 2) ? 1 : 0;
        } else if (nb > 2) {
            /* 驱动不接受「输入 + 输出」混合：退回纯输入 */
            e = ASIOCreateBuffers(g_bi, 2, g_bs, &g_cb);
            nb = 2;
            g_need_out = 0;
        }
        if (e != ASE_OK) {
            set_err("ASIOCreateBuffers 失败（码 %d），缓冲 %ld 帧", (int)e, g_bs);
            if (err) snprintf(err, errcap, "%s", g_err);
            if (g_com) { CoUninitialize(); g_com = 0; }
            ASIOExit();
            return -8;
        }
    }

    if (ASIOGetLatencies(&g_lat_i, &g_lat_o) != ASE_OK) { g_lat_i = g_bs; g_lat_o = 0; }

    e = ASIOStart();
    if (e != ASE_OK) {
        set_err("ASIOStart 失败（码 %d）", (int)e);
        if (err) snprintf(err, errcap, "%s", g_err);
        ASIODisposeBuffers();
        ASIOExit();
        if (g_com) { CoUninitialize(); g_com = 0; }
        return -9;
    }

    g_open = 1;
    g_err[0] = 0;
    if (out_sr)  *out_sr  = (int)(g_sr + 0.5);
    if (out_buf) *out_buf = (int)g_bs;
    return 0;
}

/* ---------------------------------------------------------------- 读 */
/*
 * 拉取最多 max_frames 帧的**交错立体声 float32**（每帧 2 个 float）。
 * 返回实际写入的 float 个数（不是帧数）——调用方按 //2 得到帧数。
 */
extern "C" __declspec(dllexport) int avj_asio_read(float* out, int max_frames)
{
    LONG r, w, avail, n, i;
    const LONG m = g_cap - 1;
    if (!g_open || !g_ring || !out || max_frames <= 0) return 0;

    r = g_r;
    w = g_w;
    avail = w - r;
    if (avail <= 0) return 0;
    if (avail > (LONG)max_frames) avail = (LONG)max_frames;

    for (i = 0; i < avail; i++) {
        const LONG idx = (r + i) & m;
        out[i * 2]     = g_ring[idx * 2];
        out[i * 2 + 1] = g_ring[idx * 2 + 1];
    }
    g_r = r + avail;
    n = avail;
    return (int)(n * 2);
}

/* ---------------------------------------------------------------- 关闭 */
extern "C" __declspec(dllexport) int avj_asio_close(void)
{
    if (!g_open) {
        if (g_ring) { free(g_ring); g_ring = NULL; }
        return 0;
    }
    g_open = 0;                 /* 先置 0：让回调立刻停止写 */
    ASIOStop();
    ASIODisposeBuffers();
    ASIOExit();
    if (asioDrivers) asioDrivers->removeCurrentDriver();
    if (g_com) { CoUninitialize(); g_com = 0; }
    if (g_ring) { free(g_ring); g_ring = NULL; }
    g_bs = 0;
    return 0;
}

/* ---------------------------------------------------------------- 通道枚举 */
/*
 * 列出某驱动可用的**输入通道**（给界面选"用哪两路输入"用）。
 *
 * 为什么需要它（用户问「ASIO 能采集指定输出吗」）：
 *   ASIO **没有 loopback 概念**，抓不到别的程序播出来的声音；它能做的是
 *   **选硬件输入通道**。而有些驱动的输入里**自带 Loopback 通道**
 *   （在通道名里体现，如 "Loopback 1/2"、RME 的 TotalMix 回环），
 *   把名字列给用户看，他才能选中那两路 —— 这是"抓输出"唯一能在 ASIO 侧做到的形式。
 *
 * buf 每行一个通道：`序号\t名字\t类型码\n`（名字是驱动给的 **ANSI** 原文，
 * 调用方按 mbcs 解码；错误信息另有 UTF-8 通道，两者不要混）。
 * 返回 0 成功；负数见上面的错误码表（-2 找不到 / -3 装载失败 / -4 init 失败）。
 */
extern "C" __declspec(dllexport) int avj_asio_channel_info(const char* drv_name,
                                                           int* out_nin, int* out_nout,
                                                           char* buf, int cap)
{
    int i, n, found = -1, off = 0;
    long nin = 0, nout = 0;
    ASIODriverInfo info;
    ASIOError e;
    char names[AVJ_MAX_DRV][64];
    char* ptrs[AVJ_MAX_DRV];
    int com = 0;

    if (buf && cap > 0) buf[0] = 0;
    if (out_nin)  *out_nin = 0;
    if (out_nout) *out_nout = 0;

    if (!asioDrivers) asioDrivers = new AsioDrivers();
    if (!asioDrivers) { set_err("无法创建 AsioDrivers"); return -3; }

    {
        HRESULT hr = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
        com = (hr == S_OK || hr == S_FALSE) ? 1 : 0;
    }

    for (i = 0; i < AVJ_MAX_DRV; i++) ptrs[i] = names[i];
    n = (int)asioDrivers->getDriverNames(ptrs, AVJ_MAX_DRV);
    for (i = 0; i < n; i++) {
        if (drv_name && strcmp(names[i], drv_name) == 0) { found = i; break; }
    }
    if (found < 0) {
        set_err("找不到该 ASIO 驱动（本机共注册了 %d 个）", n);
        if (com) CoUninitialize();
        return -2;
    }

    if (!asioDrivers->loadDriver((char*)drv_name)) {
        set_err("装载该 ASIO 驱动失败（多半被别的程序独占，此时列不出通道名）");
        if (com) CoUninitialize();
        return -3;
    }

    memset(&info, 0, sizeof(info));
    e = ASIOInit(&info);
    if (e != ASE_OK) {
        set_err("ASIOInit 失败（码 %d）：%s", (int)e, info.errorMessage);
        ASIOExit();
        if (asioDrivers) asioDrivers->removeCurrentDriver();
        if (com) CoUninitialize();
        return -4;
    }

    ASIOGetChannels(&nin, &nout);
    if (out_nin)  *out_nin = (int)nin;
    if (out_nout) *out_nout = (int)nout;

    if (buf && cap > 0) {
        for (i = 0; i < nin; i++) {
            ASIOChannelInfo ci;
            int wrote;
            memset(&ci, 0, sizeof(ci));
            ci.channel = i;
            ci.isInput = ASIOTrue;
            ci.name[0] = 0;
            ci.type = ASIOSTInt16LSB;
            ASIOGetChannelInfo(&ci);
            /* ci.name 是定长 char[32]，可能有未初始化尾巴 —— 先确保结尾有 0 */
            ci.name[31] = 0;
            wrote = snprintf(buf + off, (size_t)(cap - off), "%d\t%s\t%d\n",
                             i, ci.name, (int)ci.type);
            if (wrote <= 0 || wrote >= cap - off) break;
            off += wrote;
        }
    }

    ASIOExit();
    asioDrivers->removeCurrentDriver();
    if (com) CoUninitialize();
    if (out_nin && *out_nin <= 0) {
        set_err("该驱动没有输入通道");
        return -5;
    }
    g_err[0] = 0;
    return 0;
}

/* 打开驱动的控制面板（改缓冲大小等）。⚠ 播放中不能开 ⇒ 调用方要先 close。 */
extern "C" __declspec(dllexport) int avj_asio_control_panel(void)
{
    if (!g_open) { set_err("没有打开的驱动"); return -1; }
    return (ASIOControlPanel() == ASE_OK) ? 0 : -2;
}

/* 该驱动目前是否可用（能不能装上）—— 只做「装载 → 卸下」，不 init、不 start。
   ⚠ 用它来判断「是否被别的程序独占」，但要注意：真正打开才算数。 */
extern "C" __declspec(dllexport) int avj_asio_can_load(const char* drv_name)
{
    int ok;
    if (!asioDrivers) asioDrivers = new AsioDrivers();
    if (!asioDrivers) return -1;
    ok = asioDrivers->loadDriver((char*)drv_name) ? 0 : -1;
    if (ok == 0) asioDrivers->removeCurrentDriver();
    return ok;
}
