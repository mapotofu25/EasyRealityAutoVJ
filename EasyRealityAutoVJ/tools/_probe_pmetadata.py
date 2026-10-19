# -*- coding: utf-8 -*-
"""直接读进程内存，验证 cyndilib 的 AudioSendFrame 里 frame_ptr.p_metadata 是不是野指针。

原理（全部来自源码，见诊断报告）：
  · `cyndilib/wrapper/ndi_structs.pyx:87`  `audio_frame_create_default()` 用 `mem_alloc`（= malloc，
    `common.pyx:14`，**不清零**）建结构体，并逐个赋值 sample_rate/no_channels/no_samples/
    timecode/FourCC/channel_stride_in_bytes/timestamp/p_data —— **唯独漏了 p_metadata**。
  · 对照 `video_frame_create_default()`（同文件 :36）里有 `p.p_metadata = NULL`。
  · `audio_frame_copy()`（:99）也不复制 p_metadata ⇒ open() 时快照进每个 buffer item 的
    frame_ptr.p_metadata 仍是 malloc 出来的垃圾。

NDIlib_audio_frame_v3_t (x64) 内存布局：
  0  int    sample_rate
  4  int    no_channels
  8  int    no_samples
  16 int64  timecode
  24 int    FourCC
  32 void*  p_data
  40 int    channel_stride_in_bytes
  48 char*  p_metadata      ← 我们要看的
  56 int64  timestamp
共 64 字节。
"""
import ctypes
import sys
import os
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD)]


def readable(addr, size=8):
    if not addr:
        return False
    mbi = MEMORY_BASIC_INFORMATION()
    r = k32.VirtualQuery(ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
    if not r:
        return False
    if mbi.State != MEM_COMMIT:
        return False
    if mbi.Protect & PAGE_NOACCESS or mbi.Protect & PAGE_GUARD:
        return False
    base = mbi.BaseAddress or 0
    end = base + mbi.RegionSize
    return base <= addr and addr + size <= end


def rd_int(addr):
    return ctypes.c_int.from_address(addr).value


def rd_ptr(addr):
    return ctypes.c_void_p.from_address(addr).value or 0


def rd_u64(addr):
    return ctypes.c_uint64.from_address(addr).value


def find_frame_ptrs(obj, predicate, span=320):
    """在对象内存里扫出指向 NDI 帧结构体的指针。"""
    base = id(obj)
    hits = []
    for off in range(0, span, 8):
        cand = rd_ptr(base + off)
        if not readable(cand, 64):
            continue
        try:
            if predicate(cand):
                hits.append((off, cand))
        except OSError:
            continue
    return hits


def dump_audio_frame(ptr):
    return dict(sample_rate=rd_int(ptr + 0),
                no_channels=rd_int(ptr + 4),
                no_samples=rd_int(ptr + 8),
                timecode=rd_u64(ptr + 16),
                fourcc=hex(rd_int(ptr + 24) & 0xFFFFFFFF),
                p_data=hex(rd_ptr(ptr + 32)),
                channel_stride=rd_int(ptr + 40),
                p_metadata="NULL" if rd_ptr(ptr + 48) == 0 else hex(rd_ptr(ptr + 48)),
                p_metadata_raw=rd_ptr(ptr + 48),
                timestamp=rd_u64(ptr + 56))


def main():
    from cyndilib.audio_frame import AudioSendFrame
    from cyndilib.video_frame import VideoSendFrame

    print("=" * 74)
    print("对照实验 A：AudioSendFrame（cyndilib 0.1.1）")
    print("=" * 74)
    a = AudioSendFrame()
    a.sample_rate = 48000
    a.num_channels = 2
    a.set_max_num_samples(4800)

    hits = find_frame_ptrs(a, lambda p: rd_int(p + 0) == 48000 and rd_int(p + 4) == 2)
    print("在 AudioSendFrame 对象（id=0x%x）里扫到 %d 个帧结构体指针" % (id(a), len(hits)))
    bad = 0
    for off, p in hits:
        info = dump_audio_frame(p)
        # 对象内偏移 = 该 frame_ptr 槽位；items[i].frame_ptr 排在 SendFrame_item_s 之后
        print("  frame_ptr @obj+0x%03x -> 0x%x  %r" % (off, p, info))
        # 校验：p_data 是否等于该 item 真实的 p_data？这里只看 metadata 是否 NULL
        if info["p_metadata_raw"] != 0:
            bad += 1
    print()
    print("=> 非 NULL 的 p_metadata 数量: %d / %d" % (bad, len(hits)))
    if hits and bad == len(hits):
        print("=> ★★ 全部为垃圾指针：发送音频时 NDI 会把它当 C 字符串读 → 访问冲突")
    elif bad == 0:
        print("=> 全部为 NULL（恰好 malloc 到清零页）；换一次进程/堆状态就可能变垃圾")

    print()
    print("=" * 74)
    print("对照实验 B：VideoSendFrame（应该 p_metadata=NULL）")
    print("=" * 74)
    v = VideoSendFrame()
    v.set_resolution(640, 360)
    v.set_fourcc(0x41524742)
    v.set_frame_rate(60)
    v.set_progressive(True)
    vh = find_frame_ptrs(v, lambda p: rd_int(p + 0) == 640 and rd_int(p + 4) == 360)
    print("扫到 %d 个视频帧结构体指针" % len(vh))
    for off, p in vh:
        print("  frame_ptr @obj+0x%03x -> 0x%x  p_metadata=%s  xres=%d yres=%d line_stride=%d"
              % (off, p,
                 "NULL" if rd_ptr(p + 56) == 0 else hex(rd_ptr(p + 56)),
                 rd_int(p + 0), rd_int(p + 4), rd_int(p + 48)))
    print()
    print("（视频帧结构体布局：0 xres, 4 yres, 8 FourCC, 12/16 帧率, 20 宽高比, 24 格式,"
          " 32 timecode, 40 p_data, 48 line_stride, 56 p_metadata, 64 timestamp）")


if __name__ == "__main__":
    main()
