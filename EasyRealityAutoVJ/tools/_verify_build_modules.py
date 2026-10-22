# -*- coding: utf-8 -*-
r"""确认打好的包里「本轮修复」真的进去了（打包后唯一可靠的判据）。

## 为什么需要它
- 看 exe 的**修改时间**会骗人（COLLECT 失败时旧目录还在，时间没变）；
- 看**日志** `Build complete!` 也会骗人（PySide6 缺失那轮照样通过）；
- 唯一硬判据 = **在产物里找到"本轮新加的代码"**。

## 做法
`build\EasyRealityAutoVJ\PYZ-00.pyz` 是 ZlibArchive：头部 `PYZ\0` + 8 字节 +
TOC（marshal 编码的 (name, typecode, offset, length) 列表）+ 各模块 zlib 压缩体。
本脚本**不做二进制格式硬解**（PyInstaller 版本间 TOC 编码有变动，硬解易错），
改为两步：
  1. **模块名存在性**：在 PYZ 原始字节里找模块名（TOC 明文，可靠）；
  2. **新代码存在性**：用 zlib **全文扫描**出所有可解压流，在解出的字节里找
     本轮新加的函数名 / 常量名（如 `_no_window_kwargs` / `modes_snapshot` /
     `_empty_streak`）—— 这些是**本轮才有的**，出现即证明是新包。

用法：python tools/_verify_build_modules.py [PYZ路径]
默认：build/EasyRealityAutoVJ/PYZ-00.pyz
"""
import os
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, "build", "EasyRealityAutoVJ", "PYZ-00.pyz")

# ---- 必须存在的模块 ----
WANT_MODULES = [
    "lan_audio", "lan_audio_recv", "lan_audio_send", "wasapi_capture",
    "engine", "audio_engine", "ui_main", "colorfx", "i18n_map",
]

# ---- 本轮新加的代码标记（打包里出现 = 是新包）----
WANT_MARKERS = [
    (b"_no_window_kwargs", "弹窗修复：arp 不再弹控制台"),
    (b"CREATE_NO_WINDOW", "弹窗修复：常量"),
    (b"modes_snapshot", "卡顿修复：无锁只读快照"),
    (b"_empty_streak", "空转修复：采集空档退避"),
    # ★ 2026-10-17 本轮（码表图层不演出 / HUD 无歌名）
    (b"set_audio_engine", "接线修复：引擎本体注入（码表跟拍读 current_song_id）"),
    (b"_merge_cue_song_meta", "HUD 修复：码表曲目并入歌名映射"),
    (b"_cue_pos_beat_len", "对拍修复：算位置与选片段同一拍长来源"),
    (b"_audio_engine", "接线修复：_audio_engine 属性已初始化"),
    # ★ 2026-10-17 第三轮（码表多层 / 淡出黑屏 / 卡顿 / 右键菜单）
    (b"_cue_alpha", "多层修复：码表素材 alpha 事实表"),
    (b"rate_for", "卡顿修复：实时变速对齐"),
    (b"desired_speed_for", "卡顿修复：按素材档位取倍速"),
    (b"_cue_layer_menu", "菜单：码表图层独立右键菜单"),
    (b"_cue_alpha_used", "自愈：alpha 结论变了重开解码器"),
    # ★ 2026-10-17 第四轮（同包多曲目 / 淡出曲线 / 解码器重建不落地）
    (b"_cue_rebind_track", "换歌：同包内重绑曲目（不关解码器）"),
    (b"_cue_match_track_local", "换歌：在已装表里重认曲目"),
    (b"_cue_need_seek", "落地：重建的解码器首次必须 seek 到 play_sec"),
    (b"seek_enabled", "off 档连首次落地也不许 seek"),
    # ★ 2026-10-18 第五轮（越久越对不齐 / 熔断可恢复 / 子层可见）
    (b"_Pacer", "速率修复：解码线程绝对时间表"),
    (b"pacer", "速率修复：三处循环都走节拍器（局部变量名落进 co_varnames）"),
    (b"_trip_breaker", "熔断修复：seek 失败退避后可恢复"),
    (b"sub_active", "可见性：码表子层活跃数（HUD/日志）"),
]

# ---- 旧包独有的标记（**不该**再出现）----
STALE_MARKERS = [
    (b"NDI \xe9\x87\x87\xe9\x9b\x86\xe5\xbf\x83\xe8\xb7\xb3", "旧文案 NDI 采集心跳"),
]


def decompress_all(blob, limit=4000):
    """暴力扫出所有可解压的 zlib 流（PYZ 里每个模块一个流）。"""
    out = []
    n = len(blob)
    i = 0
    found = 0
    while i < n and found < limit:
        # zlib 头：0x78 (CMF)，第二字节满足 (CMF*256+FLG)%31==0
        if blob[i] == 0x78 and i + 1 < n:
            flg = blob[i + 1]
            if (blob[i] * 256 + flg) % 31 == 0:
                try:
                    d = zlib.decompressobj()
                    dec = d.decompress(blob[i:i + 4000000])
                    if dec:
                        out.append(dec)
                        found += 1
                        # 跳过这段已消费的输入
                        consumed = len(blob[i:i + 4000000]) - len(d.unused_data)
                        i += max(consumed, 1)
                        continue
                except Exception:
                    pass
        i += 1
    return out, found


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not os.path.isfile(path):
        print("找不到 PYZ：%s" % path)
        sys.exit(2)
    blob = open(path, "rb").read()
    print("PYZ = %s（%.1f MB）" % (path, len(blob) / 1e6))

    ok = True

    print("")
    print("① 关键模块（TOC 明文里找名字）")
    for m in WANT_MODULES:
        n = blob.count(m.encode())
        good = n > 0
        print("   %-18s %s" % (m, ("在 (%d)" % n) if good else "★缺失★"))
        if not good:
            ok = False

    print("")
    print("② 解压所有模块流，在源码里找本轮新标记")
    streams, cnt = decompress_all(blob)
    joined = b"\x00".join(streams)
    print("   解出模块流 %d 个（共 %.1f MB 源码）"
          % (cnt, len(joined) / 1e6))
    for needle, desc in WANT_MARKERS:
        good = needle in joined
        print("   %-22s %s   %s"
              % (needle.decode("latin-1"), "✓" if good else "★未找到★", desc))
        if not good:
            ok = False

    print("")
    print("③ 旧包标记（不该出现）")
    for needle, desc in STALE_MARKERS:
        bad = needle in joined
        print("   %-28s %s   %s"
              % (desc, "★出现了（旧包！）" if bad else "✓ 未出现", ""))
        if bad:
            ok = False

    print("")
    if ok:
        print("结论：✅ 本轮修复代码确实在包里，且不含旧包标记")
        sys.exit(0)
    print("结论：❌ 校验未通过（见上）")
    sys.exit(1)


if __name__ == "__main__":
    main()
