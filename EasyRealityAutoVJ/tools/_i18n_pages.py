# -*- coding: utf-8 -*-
"""补 3 条 i18n 词条（新增的「素材库」全屏页文案）。按铁律插在 ZH2EN 收尾 `}` 之前。"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
I18N = os.path.join(ROOT, "src", "i18n_map.py")

ROWS = [
    ('    "\U0001f5c2 素材库": "\U0001f5c2 Library",\n'),
    ('    "全屏管理素材：搜索 / 筛选 / 导入 / 打标签（演出台那份原样不动）":\n'
     '        "Manage clips full-screen: search / filter / import / tag '
     '(the Live-page copy stays untouched)",\n'),
    ('    "这里是全屏版素材库，和「🎬 演出台」上那份是同一批素材（那份保持原样）。\\n'
     '搜索 / 筛选 / 导入 / 右键打标签都在这里做；双击缩略图 = 播放到当前图层。\\n'
     '⚠ 「拖进图层」只能在演出台做（跨页面拖拽系统不允许）。":\n'
     '        "This is the full-screen copy of the media library (same clips as the '
     '🎬 Live page, which stays untouched).\\n'
     'Search / filter / import / right-click to tag here; double-click a thumbnail to play it '
     'on the current layer.\\n'
     '⚠ Dragging a clip into a layer only works on the Live page (cross-page drag is not '
     'supported by Qt).",\n'),
]


def main():
    s = io.open(I18N, encoding="utf-8").read()
    added = 0
    for row in ROWS:
        key = row.strip().split('":')[0].strip().strip('"')
        # 用"整行是否存在"判断更稳：直接看这条 row 的前 12 个字符是否出现过
        probe = row.strip().split('":')[0][:14]
        if probe in s:
            print("· 已有，跳过：", probe[:26])
            continue
        idx = s.rfind("\n}")
        if idx < 0:
            print("✗ 找不到 ZH2EN 收尾")
            return 1
        s = s[:idx + 1] + row + s[idx + 1:]
        added += 1
    io.open(I18N, "w", encoding="utf-8", newline="\n").write(s)
    print("✓ 插入 %d 条词条" % added)
    import py_compile
    py_compile.compile(I18N, doraise=True)
    # 确认能正确 import 且键都在
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import i18n_map
    for k in ("\U0001f5c2 素材库",
              "全屏管理素材：搜索 / 筛选 / 导入 / 打标签（演出台那份原样不动）"):
        print("   ZH2EN 含 %r -> %s" % (k[:14], k in i18n_map.ZH2EN))
    long_k = [x for x in i18n_map.ZH2EN if x.startswith("这里是全屏版素材库")]
    print("   长描述键存在 ->", bool(long_k), "| 含真实换行 ->",
          ("\\n" in long_k[0]) if long_k else None)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
