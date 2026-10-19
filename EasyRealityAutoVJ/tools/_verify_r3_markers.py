# -*- coding: utf-8 -*-
"""独立校验：本轮（NDI 掉帧）修复是否真的进了这个 PYZ。"""
import sys
import zlib

path = sys.argv[1] if len(sys.argv) > 1 else "build_r4/EasyRealityAutoVJ/PYZ-00.pyz"
blob = open(path, "rb").read()
print("PYZ = %s（%.1f MB）" % (path, len(blob) / 1e6))

NEEDLES = [
    (b"autovj-ndi-video", "NDI 掉帧：视频提交专用线程名"),
    (b"_video_loop", "NDI 掉帧：_video_loop 方法"),
    (b"_vlock", "NDI 掉帧：_vlock 视频锁"),
    (b"_alock", "NDI 掉帧：_alock 音频锁"),
    (b"_vsend_block_ms", "NDI 掉帧：视频提交耗时诊断"),
    (b"_no_window_kwargs", "上一轮：arp 弹窗修复"),
    (b"modes_snapshot", "上一轮：无锁快照"),
    (b"_empty_streak", "上一轮：采集退避"),
]
STALE = [(b"NDI \xe9\x87\x87\xe9\x9b\x86\xe5\xbf\x83\xe8\xb7\xb3", "旧文案 NDI 采集心跳")]

found = {n: 0 for n, _ in NEEDLES}
stale_hits = {n: 0 for n, _ in STALE}
total = 0
n = len(blob)
i = 0
while i < n - 2:
    # zlib 头：0x78 01/9c/da
    if blob[i] == 0x78 and blob[i + 1] in (0x01, 0x9C, 0xDA):
        try:
            d = zlib.decompressobj()
            out = d.decompress(blob[i:])
            if len(out) > 100:
                total += len(out)
                for k in found:
                    if k in out:
                        found[k] += 1
                for k in stale_hits:
                    if k in out:
                        stale_hits[k] += 1
                i += 1
                continue
        except Exception:
            pass
    i += 1

print("解压出源码总量 %.1f MB" % (total / 1e6))
print("\n① 本轮必需标记")
ok = True
for k, desc in NEEDLES:
    hit = found[k] > 0
    print("   %-22s %s   %s" % (k.decode(), "✓" if hit else "✗", desc))
    if not hit and k.startswith((b"autovj", b"_video", b"_vlock", b"_alock", b"_vsend")):
        ok = False
print("\n② 旧包标记（不该出现）")
for k, desc in STALE:
    hit = stale_hits[k] > 0
    print("   %-22s %s   %s" % (desc, "!! 出现了" if hit else "✓ 未出现", ""))
    if hit:
        ok = False
print("\n结论：" + ("✅ 本轮 NDI 修复确实在包里" if ok else "❌ 包内缺本轮修复"))
sys.exit(0 if ok else 2)
