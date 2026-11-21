# -*- coding: utf-8 -*-
r"""打「工具集」1.0 压缩包 —— ERLIB / 音频发送端 / NDI自检 / 音源诊断 合成一个 zip。

包内结构：
    工具集/
      EasyReality_音频发送端.exe
      ERLIB曲库分析.exe
      NDI接收自检.exe
      NDI局域网测试说明.txt
      NDI防火墙放行.bat
      音源诊断.exe
      音源排查说明.txt
      使用说明_工具集.txt

用法：venv\Scripts\python.exe -u tools/make_tools_zip.py
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.dirname(ROOT)
LEGACY = r"P:\ERAVJ"
OUT_DIR = os.path.join(ROOT, "发行版")
OUT = os.path.join(OUT_DIR, "EasyReality_Tools_1.0.zip")
TOP = "工具集"

README_TXT = """Easy Reality — 工具集 1.0（2026.11.21）
================================================================
主程序 EasyRealityAutoVJ 和码表编辑器 EasyRealityCue 有各自独立的包，
这个包里是配套工具。四个软件怎么配合：

    ERLIB曲库分析  ->  扫曲库、导出 .eravjlib（认歌用）
    EasyRealityCue  ->  拿曲库包逐首编码表 .ercue（在另一个包里）
    EasyRealityAutoVJ（另一个包）-> 加载码表 + 素材，演出时按拍切
    音频发送端      ->  B 机没有歌文件时，把 A 机的声音实时送过去

--------------------------------------------------------------
1) ERLIB曲库分析.exe          跑在 DJ 机
   选文件夹 -> 开始扫描 -> 勾选 -> 导出 .eravjlib。
   扫出来的东西：封面 / 音频指纹 / 节拍网格 / BPM / 能量段落。
   导出时**不带音频文件**，演出机拿这个包就能认出正在放的是哪首。

2) EasyReality_音频发送端.exe  跑在放歌机（A 机）
   选输入设备 -> 填目标端口（默认 47811）-> 开始。
   演出机那边把音源切成「局域网音频」就能收到。
   两台机器要在同一个局域网，Windows 防火墙要放行 UDP 47811。

3) NDI接收自检.exe             任一台机器，排查用
   确认本机能不能收到 NDI 画面，连上后存一张截图。
   配套：NDI局域网测试说明.txt、NDI防火墙放行.bat（右键以管理员运行）

4) 音源诊断.exe                任一台机器，排查用
   把当前系统声音/麦克风的电平打出来，判断「到底有没有声音进来」。
   配套：音源排查说明.txt

--------------------------------------------------------------
常见问题
- 认不出歌：先确认演出机导入的曲库包是同一台 DJ 机扫的；再看曲库面板的「来源」列。
- 联不上 A 机：先 ping 通，再确认防火墙放行；自动发现失败时直接填 A 机 IP。
- 有画面没声音 / 有声音没画面：分别用 NDI接收自检 和 音源诊断 定位是哪一段断了。

反馈：QQ 1793170435    测试群：ERAVJ测试 543643838
项目：https://github.com/mapotofu25/EasyRealityAutoVJ
"""

# (源路径, 包内名字)
ITEMS = [
    (os.path.join(LEGACY, "EasyReality_音频发送端", "dist_sender",
                  "EasyReality_音频发送端.exe"),
     "EasyReality_音频发送端.exe"),
    (os.path.join(WORKSPACE, "ERLIB曲库分析_单文件_2026.11.20_8线程.exe"),
     "ERLIB曲库分析.exe"),
    (os.path.join(LEGACY, "AutoVJ", "NDI局域网测试", "NDI接收自检.exe"),
     "NDI接收自检.exe"),
    (os.path.join(LEGACY, "AutoVJ", "NDI局域网测试", "NDI局域网测试说明.txt"),
     "NDI局域网测试说明.txt"),
    (os.path.join(LEGACY, "AutoVJ", "NDI局域网测试", "NDI防火墙放行.bat"),
     "NDI防火墙放行.bat"),
    (os.path.join(LEGACY, "AutoVJ", "音源排查", "音源诊断.exe"),
     "音源诊断.exe"),
    (os.path.join(LEGACY, "音源诊断", "音源排查说明.txt"),
     "音源排查说明.txt"),
]


def main():
    miss = [p for p, _n in ITEMS if not os.path.isfile(p)]
    if miss:
        print("缺文件：")
        for m in miss:
            print("   ", m)
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.isfile(OUT):
        os.remove(OUT)
    total = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for src, name in ITEMS:
            z.write(src, "%s/%s" % (TOP, name))
            total += os.path.getsize(src)
            print("  + %-40s %8.1f MB" % (name, os.path.getsize(src) / 1e6))
        z.writestr("%s/使用说明_工具集.txt" % TOP, README_TXT.encode("utf-8"))
        print("  + %-40s %8.1f KB" % ("使用说明_工具集.txt",
                                      len(README_TXT.encode("utf-8")) / 1e3))
    print("\n完成：%s（%d 项 / 原始 %.1f MB / 压缩后 %.1f MB）"
          % (OUT, len(ITEMS) + 1, total / 1e6, os.path.getsize(OUT) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
