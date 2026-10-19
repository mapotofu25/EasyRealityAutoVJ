# -*- coding: utf-8 -*-
"""补丁：两份使用说明跟着「素材库回演出台 + 第 2 页改曲库·曲风 + 工具栏收按钮」改。"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def edit(path, pairs):
    p = os.path.join(ROOT, path)
    t = io.open(p, encoding="utf-8").read()
    orig = t
    for old, new, cnt in pairs:
        got = t.count(old)
        if got != cnt:
            FAILS.append("%s: 期望 %d 处，实得 %d\n--- %s\n" % (path, cnt, got, old[:160]))
            continue
        t = t.replace(old, new)
    if t != orig:
        io.open(p, "w", encoding="utf-8", newline="").write(t)
        print("  已写入 %s（%+d 字节）" % (path, len(t) - len(orig)))
    else:
        print("  未变化 %s" % path)


edit("使用说明.txt", [
    (
        '  🎬 演出台              现场用：图层 + 预览 + 效果设置 + 输出设置\n'
        '  📁 素材 · 曲库 · 曲风   演出前准备：素材库（整幅宽）+ 音乐曲库 + 曲风映射\n'
        '  🎞 码表编排            编曲用：导入 .ercue 码表、排到拍上、绑定码表图层（试验功能）\n'
        '\n'
        '· 素材库在**第 2 个界面**（不再挤在预览右边）——现场要找素材，点一下页签就过去。\n',
        '  🎬 演出台        现场用：图层 + 预览 + **素材库** + 效果设置 + 输出设置\n'
        '  🎵 曲库 · 曲风    演出前准备：音乐曲库（导入音乐 + 扫描分析曲风）/ 曲风映射\n'
        '  🎞 码表编排      编曲用：导入 .ercue 码表、排到拍上、绑定码表图层（试验功能）\n'
        '\n'
        '· **素材库就在演出台上**（和预览并排）——现场随时取素材、**直接拖进图层**；\n'
        '  双击缩略图 = 立即播放到当前图层（更快）。\n'
        '· 顶部工具条的「🎵 音乐曲库 / 曲风映射」已收进第 2 个页签。\n',
        1),
    (
        '  素材进来后，切到「📁 素材 · 曲库 · 曲风」页签，点素材库上方的「扫描打标」。\n',
        '  素材进来后，点素材库上方的「扫描打标」。\n',
        1),
    (
        '  点顶部「🎵 音乐曲库」，导入你的音乐文件夹，点「扫描分析」。\n',
        '  点第 2 个页签「🎵 曲库 · 曲风」→「🎵 音乐曲库…」，导入音乐文件夹，点「扫描分析」。\n',
        1),
    (
        '素材库的缩略图**不能**直接拖进图层了 —— 素材库在另一个页签上，拖拽要求两个控件\n'
        '同时可见。改用这两种更快的办法：\n'
        '  · **双击**缩略图 = 立即播放到当前图层（推荐，最快）；\n'
        '  · **右键**缩略图 →「添加到图层」/「播放到图层」→ 选目标图层。\n',
        '素材库的缩略图也能直接拖进图层；\n'
        '另外**双击**缩略图 = 立即播放到当前图层（更快，演出中一只手就能用）。\n',
        1),
    (
        '见上手流程第 5 步：点顶部「🎵 音乐曲库」→「导入音乐文件夹」→「扫描分析」。\n',
        '见上手流程第 5 步：第 2 个页签「🎵 曲库 · 曲风」→「🎵 音乐曲库…」\n'
        '→「导入音乐文件夹」→「扫描分析」。\n',
        1),
    (
        '「曲风映射」里可以改「曲风 → 画面标签」的绑定关系：\n',
        '第 2 个页签「🎵 曲库 · 曲风」→「曲风映射…」里可以改「曲风 → 画面标签」的绑定关系：\n',
        1),
])

edit("使用说明_EN.txt", [
    (
        '  🎬 Live                  For the show: layers + preview + effects + output\n'
        '  📁 Media · Music · Genres Prep work: media library (full width) + music\n'
        '                           library + genre mapping\n'
        '  🎞 Cue Sheets            Authoring: import an .ercue sheet, lay it out on\n'
        '                           beats, bind it to a Cue Sheet layer (experimental)\n'
        '\n'
        '· The media library now lives on **page 2** (it no longer shares the row with\n'
        '  the preview). To grab a clip mid-show, just click the tab.\n',
        '  🎬 Live          For the show: layers + preview + **media library** +\n'
        '                   effects + output\n'
        '  🎵 Music · Genres Prep work: music library (import + genre scan) /\n'
        '                   genre mapping\n'
        '  🎞 Cue Sheets    Authoring: import an .ercue sheet, lay it out on\n'
        '                   beats, bind it to a Cue Sheet layer (experimental)\n'
        '\n'
        '· The **media library stays on the Live page** (side by side with the\n'
        '  preview), so you can grab clips and **drag them straight into a layer**\n'
        '  during the show; double-clicking a thumbnail plays it on the current layer.\n'
        '· The toolbar\'s "🎵 Music Library / Genre Mapping" buttons moved to page 2.\n',
        1),
    (
        '  After importing, switch to the "📁 Media · Music · Genres" tab and click\n'
        '  "Scan & Tag" above the library.\n',
        '  After importing, click "Scan & Tag" above the library.\n',
        1),
    (
        '  Click "🎵 Music Library" at the top, import your music folder and click\n',
        '  Click "🎵 Music Library…" on page 2 ("🎵 Music · Genres"), import your music folder and click\n',
        1),
    (
        'Library thumbnails can NO LONGER be dragged into a layer — the library lives on\n'
        'another tab, and Qt requires both widgets to be visible to drag between them.\n'
        'Use these instead (both are faster):\n'
        '  · **Double-click** a thumbnail = play it on the current layer (recommended);\n'
        '  · **Right-click** a thumbnail → "Add to layer" / "Play to layer" → pick a layer.\n',
        'Library thumbnails can also be dragged straight into a layer;\n'
        '**double-clicking** one plays it on the current layer instead (faster - handy mid-show).\n',
        1),
])

if FAILS:
    print("\n!! 未全部应用：")
    for f in FAILS:
        print("---", f)
    sys.exit(1)
print("\n手册补丁应用完毕。")
