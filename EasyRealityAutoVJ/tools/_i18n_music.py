# -*- coding: utf-8 -*-
"""补曲库整页面板的 13 条词条。

⚠ 用**括号配对**从 `ZH2EN = {` 找它的收尾 `}`：`i18n_map.py` 里有两个字典
（ZH2EN 在前、CATEGORY_EN 在后），用"文件最后一个 `}`"必然插错（已踩两次）。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
I18N = os.path.join(ROOT, "src", "i18n_map.py")

ROWS = [
    ('    "排序 / 筛选": "Sort / Filter",\n', "排序 / 筛选"),
    ('    "排序": "Sort",\n', "排序"),
    ('    "BPM（按节拍网格）": "BPM (from beat grid)",\n', "BPM（按节拍网格）"),
    ('    "时长": "Duration",\n', "时长"),
    ('    "只看文件缺失": "Missing files only",\n', "只看文件缺失"),
    ('    "换盘 / 改名之后找不到文件的曲目（修复清单）":\n'
     '        "Tracks that can no longer be found after moving drives / renaming '
     '(repair list)",\n', "换盘 / 改名之后找不到文件的曲目（修复清单）"),
    ('    "重置筛选": "Reset filters",\n', "重置筛选"),
    ('    "BPM / 时长来自曲库扫描时建的节拍网格；「无节拍网格」的曲目"\n'
     '    "八拍乐句对齐用不了，是需要处理的清单。":\n'
     '        "BPM / duration come from the beat grid built during the library scan; "\n'
     '        "tracks without a grid cannot use 8-beat phrase alignment — that is your "\n'
     '        "to-do list.",\n',
     "BPM / 时长来自曲库扫描时建的节拍网格；「无节拍网格」的曲目八拍乐句对齐用不了，是需要处理的清单。"),
    ('    "未分析": "Not analysed",\n', "未分析"),
    ('    "无网格": "No grid",\n', "无网格"),
    ('    "文件缺失": "File missing",\n', "文件缺失"),
    ('    "共 {} 首，已分析 {} 首（当前显示 {} 首）":\n'
     '        "{} tracks, {} analysed (showing {})",\n',
     "共 {} 首，已分析 {} 首（当前显示 {} 首）"),
    ('    "筛选后 {} / {} 首": "Filtered: {} / {}",\n', "筛选后 {} / {} 首"),
]


def zh2en_close(s):
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
    raise ValueError("没找到 ZH2EN 收尾")


def main():
    s = io.open(I18N, encoding="utf-8").read()
    end = zh2en_close(s)
    head = s[:end]
    added = 0
    for row, key in ROWS:
        if ('"%s"' % key) in head:
            print("· 已有，跳过：", key[:22])
            continue
        s = s[:end] + row + s[end:]
        end += len(row)
        head = s[:end]
        added += 1
    io.open(I18N, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ 插入 %d 条" % added)

    sys.path.insert(0, os.path.join(ROOT, "src"))
    import i18n_map
    miss = [k for _r, k in ROWS if k not in i18n_map.ZH2EN]
    print("ZH2EN 条数 =", len(i18n_map.ZH2EN), "| 仍缺 =", miss or "无")
    import py_compile
    py_compile.compile(I18N, doraise=True)
    print("语法 OK")
    return 0 if not miss else 1


if __name__ == "__main__":
    sys.exit(main())
