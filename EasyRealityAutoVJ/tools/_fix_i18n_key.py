# -*- coding: utf-8 -*-
"""补 i18n 词条（新增的 T("退出全屏并按输出分辨率重置窗口大小")）。

按项目铁律：**直接插在 ZH2EN 收尾的 `}` 之前**（不要追加到文件尾再搬，会把 `}` 搬丢）。
另外顺手修掉 beatgrid.py 顶部那句指向 `store_grid` 的注释（写到 cfg 的实际是曲库扫描）。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"

# ---------------- i18n 词条 ----------------
I18N = os.path.join(ROOT, "src", "i18n_map.py")
ANCHOR = '    "已把 cue 从'          # 用不到；下面按收尾 } 定位
NEW_LINE = '    "退出全屏并按输出分辨率重置窗口大小": "Leave fullscreen and reset the window to the output resolution",\n'


def main():
    s = io.open(I18N, encoding="utf-8").read()
    if "退出全屏并按输出分辨率重置窗口大小" in s:
        print("词条已存在，跳过")
    else:
        # ZH2EN 的收尾 `}`：取最后一个单独成行的 `}`
        idx = s.rfind("\n}")
        if idx < 0:
            print("✗ 找不到 ZH2EN 的收尾 }")
            return 1
        s = s[:idx + 1] + NEW_LINE + s[idx + 1:]
        io.open(I18N, "w", encoding="utf-8", newline="\n").write(s)
        print("✓ 已插入词条")

    # ---------------- beatgrid 注释 ----------------
    B = os.path.join(ROOT, "src", "beatgrid.py")
    b = io.open(B, encoding="utf-8").read()
    old = '存储：`cfg["music_meta"][path]["grid"]`（见 `store_grid`）。'
    if old in b:
        b = b.replace(old, '存储：`cfg["music_meta"][path]["grid"]`'
                           '（**由曲库扫描直接写**；本模块的 `store_grid` / `get_grid` '
                           '是等价封装，目前只有 tools 在用）。')
        io.open(B, "w", encoding="utf-8", newline="\n").write(b)
        print("✓ beatgrid 注释已修正")
    else:
        print("· beatgrid 注释未命中，跳过")

    import py_compile
    for f in ("src/i18n_map.py", "src/beatgrid.py"):
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    print("语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
