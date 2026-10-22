# -*- coding: utf-8 -*-
"""把上一步插错位置的词条挪到 ZH2EN 里。

错在哪：我用「文件里最后一个单独成行的 `}`」定位收尾，但 `i18n_map.py` 末尾还有
**第二个字典** `CATEGORY_EN`（标签分类的中英对照），于是词条插进了它 —— 覆盖率工具
立刻报「还是找不到英文」。⇐ 这就是"必须跑 i18n_coverage.py"的价值。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
P = os.path.join(ROOT, "src", "i18n_map.py")
KEY = "退出全屏并按输出分辨率重置窗口大小"
LINE = '    "%s": "Leave fullscreen and reset the window to the output resolution",\n' % KEY
# ZH2EN 的最后一条 + 收尾括号（锚点唯一）
ANCHOR = ('    "在「{}」里新建一个曲风（会出现在纠正曲风里）":\n'
          '        "Add a genre to “{}” (it will also show up in Fix Genre)",\n'
          '}\n')


def main():
    s = io.open(P, encoding="utf-8").read()
    # 1) 从 CATEGORY_EN 里删掉
    if LINE in s:
        s = s.replace(LINE, "", 1)
        print("✓ 已从 CATEGORY_EN 移除")
    # 2) 插到 ZH2EN 收尾之前
    if s.count(ANCHOR) != 1:
        print("✗ ZH2EN 收尾锚点命中 %d 次" % s.count(ANCHOR))
        return 1
    s = s.replace(ANCHOR, ANCHOR[:-2] + LINE + "}\n")
    io.open(P, "w", encoding="utf-8", newline="\n").write(s)

    import importlib
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import i18n_map
    importlib.reload(i18n_map)
    ok = KEY in i18n_map.ZH2EN and KEY not in i18n_map.CATEGORY_EN
    print("ZH2EN 里有（且不在 CATEGORY_EN）：", ok,
          "| ZH2EN 条数 =", len(i18n_map.ZH2EN))
    import py_compile
    py_compile.compile(P, doraise=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
