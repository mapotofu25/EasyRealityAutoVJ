# -*- coding: utf-8 -*-
"""一次性把弹窗/标题/状态里的硬编码中文全部包上 T()，并补齐英文词条。

做法：
  1. 只处理**直接作为参数的字面量**（QMessageBox.*/QInputDialog.*/setWindowTitle/setText 后紧跟 "中文"）；
  2. 已经是 `T("...")` / `tr("...")` 的不动（正则要求引号紧跟括号）；
  3. 词典里已存在的键不覆盖；
  4. 带 %s / %.2f 的格式化串同样安全：包完是 `T("…%s…") % (...)`。

用法：venv\\Scripts\\python.exe tools/_i18n_hardcoded.py
"""

import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

FILES = ("ui_main.py", "panels.py", "cue_ui.py", "cue_page.py")

PAT = re.compile(
    r'(QMessageBox\.\w+\([^)]*?|QInputDialog\.\w+\([^)]*?|\.setWindowTitle\(|\.setText\()'
    r'\s*"([^"\n]*[\u4e00-\u9fff][^"\n]*)"')

# 中文 → 英文（人工写，不机翻）
EN = {
    "GPU 解码": "GPU decoding",
    "Kv 主视觉图层已存在": "A KV main-visual layer already exists",
    "NDI 不可用": "NDI unavailable",
    "Spout 不可用": "Spout unavailable",
    "从导入库移除": "Remove from library",
    "从曲库移除": "Remove from track library",
    "从素材库移除": "Remove from media library",
    "全量重扫": "Rescan everything",
    "删除失败": "Delete failed",
    "删除码表": "Delete cue sheet",
    "刷新码表": "Reload cue sheet",
    "发现重复素材": "Duplicate media found",
    "取消": "Cancel",
    "定位文件": "Locate file",
    "导入包曲目": "Import tracks from package",
    "导入失败": "Import failed",
    "导入曲库分析包": "Import library analysis package",
    "导入码表": "Import cue sheet",
    "导入素材": "Import media",
    "导入素材设置": "Import settings",
    "导出全部日志": "Export all logs",
    "局域网：": "LAN: ",
    "应用": "Apply",
    "快捷键冲突": "Hotkey conflict",
    "恢复默认": "Restore defaults",
    "扫描分析": "Analyze",
    "扫描打标": "Scan & tag",
    "新建图层": "New layer",
    "新建曲风": "New genre",
    "新建词条": "New entry",
    "曲目信息": "Track info",
    "曲风 → 画面标签映射": "Genre → visual tag mapping",
    "标签筛选（可多选）": "Tag filter (multi-select)",
    "没有曲目": "No tracks",
    "清除": "Clear",
    "清除分析数据": "Clear analysis data",
    "码表": "Cue sheet",
    "码表曲目": "Cue sheet tracks",
    "确定": "OK",
    "移除": "Remove",
    "素材库目录": "Media library folder",
    "素材文件缺失": "Media file missing",
    "绑到图层": "Bind to layer",
    "缩略图缓存": "Thumbnail cache",
    "自动检测底噪": "Auto-detect noise floor",
    "选择要绑定的曲目": "Pick a track to bind",
    "重命名图层": "Rename layer",
    "重新分析": "Re-analyze",
    "音乐曲库": "Track library",
    "音源设置": "Audio source",
    "预览画面设置": "Preview settings",
    "拍位 %.2f（%.2fs @ %s BPM）": "Beat %.2f (%.2fs @ %s BPM)",
    "拍位 %.2f（%.2fs @ %s）": "Beat %.2f (%.2fs @ %s)",
    "码表：%s ｜ 画布 %dx%d ｜ 曲目：%s ｜ BPM：%s ｜ 片段：%d ｜ 已绑图层：%s":
        "Cue sheet: %s | Canvas %dx%d | Track: %s | BPM: %s | Clips: %d | Bound layer: %s",
    "将删除全部已建立的指纹与自动识别的曲风。\\n音乐文件本身和曲库列表不会被删除。":
        "This clears all fingerprints and auto-detected genres.\\n"
        "Music files and the library list are kept.",
    "Kv 主视觉图层：没有声音时把待机画面带进来、有声音时带出去。\\n":
        "KV main-visual layer: brings in the standby image when silent, takes it out when audio starts.\\n",
    "码表图层：画面由码表接管（片段入点/出场都按码表走）。\\n":
        "Cue layer: the cue sheet drives the picture (clip in/out follow the sheet).\\n",
    "自动图层会随曲风实时替换画面，手动拖入的素材不生效":
        "Auto layers swap visuals by genre in real time; manually dropped media is ignored",
}


def main():
    total_wrap = 0
    missing = set()
    for fn in FILES:
        p = os.path.join(SRC, fn)
        s = io.open(p, encoding="utf-8").read()

        def repl(m):
            nonlocal total_wrap
            head, txt = m.group(1), m.group(2)
            total_wrap += 1
            if txt not in EN:
                missing.add(txt)
            return '%s T("%s")' % (head, txt)

        s2 = PAT.sub(repl, s)
        if s2 != s:
            io.open(p, "w", encoding="utf-8").write(s2)
            print("  %-12s 已包 T()" % fn)

    print("共包 %d 处" % total_wrap)
    if missing:
        print("!! 缺英文：")
        for m in sorted(missing):
            print("   ", m[:60])

    # 写词典（只补缺失的键）
    p = os.path.join(SRC, "i18n_map.py")
    s = io.open(p, encoding="utf-8").read()
    added = 0
    for zh, en in EN.items():
        key = '"%s":' % zh
        if key in s:
            continue
        i = s.rfind("}")
        s = s[:i] + '    "%s":\n        "%s",\n' % (zh, en) + s[i:]
        added += 1
    io.open(p, "w", encoding="utf-8").write(s)
    print("词典新增 %d 条" % added)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
