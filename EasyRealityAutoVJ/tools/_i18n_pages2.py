# -*- coding: utf-8 -*-
"""把词条挪到 **ZH2EN** 里（上次又插进了文件末尾的 CATEGORY_EN）。

教训：`i18n_map.py` 里有**两个**字典（ZH2EN 在前、CATEGORY_EN 在后），
用"文件里最后一个 `}`"定位必然插错。正确做法：**从 `ZH2EN = {` 开始做括号配对**找它的收尾。
"""
import io
import os
import re
import sys

ROOT = r"P:\AutoVJ"
I18N = os.path.join(ROOT, "src", "i18n_map.py")

ROWS = [
    '    "\U0001f5c2 素材库": "\U0001f5c2 Library",\n',
    '    "全屏管理素材：搜索 / 筛选 / 导入 / 打标签（演出台那份原样不动）":\n'
    '        "Manage clips full-screen: search / filter / import / tag '
    '(the Live-page copy stays untouched)",\n',
    '    "这里是全屏版素材库，和「🎬 演出台」上那份是同一批素材（那份保持原样）。\\n'
    '搜索 / 筛选 / 导入 / 右键打标签都在这里做；双击缩略图 = 播放到当前图层。\\n'
    '⚠ 「拖进图层」只能在演出台做（跨页面拖拽系统不允许）。":\n'
    '        "This is the full-screen copy of the media library (same clips as the '
    '🎬 Live page, which stays untouched).\\n'
    'Search / filter / import / right-click to tag here; double-click a thumbnail to play it '
    'on the current layer.\\n'
    '⚠ Dragging a clip into a layer only works on the Live page (cross-page drag is not '
    'supported by Qt).",\n',
]


def zh2en_close_index(s):
    """返回 ZH2EN 字典**收尾 `}`** 的下标（括号配对，跳过字符串里的括号）。"""
    i = s.index("ZH2EN = {")
    j = s.index("{", i)
    depth = 0
    in_str = False
    quote = ""
    esc = False
    while j < len(s):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in "\"'":
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    raise ValueError("没找到 ZH2EN 的收尾" + "}")


def main():
    s = io.open(I18N, encoding="utf-8").read()
    # ① 先把上次插错（CATEGORY_EN 里）的 3 行删掉
    removed = 0
    for row in ROWS:
        if s.count(row) == 1:
            s = s.replace(row, "", 1)
            removed += 1
    print("从错误位置移除 %d 行" % removed)

    # ② 插到 ZH2EN 收尾之前
    end = zh2en_close_index(s)
    added = 0
    for row in ROWS:
        probe = row.strip().split('":')[0][:14]
        if probe in s[:end]:
            print("· ZH2EN 里已有，跳过")
            continue
        s = s[:end] + row + s[end:]
        end += len(row)
        added += 1
    io.open(I18N, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ 插入 ZH2EN %d 条" % added)

    sys.path.insert(0, os.path.join(ROOT, "src"))
    import i18n_map
    ok = ("\U0001f5c2 素材库" in i18n_map.ZH2EN
          and "全屏管理素材：搜索 / 筛选 / 导入 / 打标签（演出台那份原样不动）"
          in i18n_map.ZH2EN
          and any(k.startswith("这里是全屏版素材库") for k in i18n_map.ZH2EN))
    print("ZH2EN 条数 =", len(i18n_map.ZH2EN), "| 3 条都在 ->", ok)
    import py_compile
    py_compile.compile(I18N, doraise=True)
    print("语法 OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
