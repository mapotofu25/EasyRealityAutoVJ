# -*- coding: utf-8 -*-
"""摘除码表（第 4 步）：i18n_map.py 词条 + config.py 配置段 + 删除码表源文件。

i18n 用的是"按行区间整段删"（885~1222 全是码表专用），删完**必须**跑
tools/i18n_coverage.py 复核：若还有别处在用某个键，会被报成"缺译"，那时再补回来。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"

# ---------------------------------------------------------------- i18n_map
I18N = os.path.join(ROOT, "src", "i18n_map.py")

I18N_REPS = [
    ('    # ==================== 三界面（页签条）====================',
     '    # ==================== 两界面（页签条）===================='),
    ('    "🎞 码表编排": "🎞 Cue Sheets",\n', ''),
    ('    "编曲：把 .ercue 码表对到拍上，绑定码表图层": "Authoring: lay out an .ercue sheet on beats and bind it to a Cue Sheet layer",\n',
     ''),
]
CUE_BLOCK_FIRST = '    "0 拍": "0 beats",'
CUE_BLOCK_LAST = '    "已把 cue 从「{}」移到「{}」的 {}": "Cue moved to a different layer at {}",'

# ---------------------------------------------------------------- config
CFG = os.path.join(ROOT, "src", "config.py")
CFG_OLD = """    "cue": {
        "cue_dir": "",               # 码表包（.ercue）放哪：导入/导出/新建都默认用它
        "track_audio": {},           # 曲目 key -> 音频文件路径（「导入音频…」记下来，试听用）                          # 码表 / 时间码（cue sheet，见 _码表功能设计.md）
        "vdj_osc_port": 8000,         # VirtualDJ 的 oscPort（我们把订阅发到这里）
        "cue_dirs": [],               # 找码表素材时额外搜索的目录
        "zoom_beats": 16,             # 时间轴视图一屏显示多少拍（界面用）
    },
"""

# ---------------------------------------------------------------- 删除的文件
DEL_FILES = [
    "src/cue_editor.py", "src/cue_preview.py", "src/cuesheet.py",
    "src/cuesheet_runtime.py", "src/position_source.py",
    "_码表功能设计.md",
]


def main():
    # --- i18n ---
    s = io.open(I18N, encoding="utf-8").read()
    for a, b in I18N_REPS:
        if s.count(a) != 1:
            print("✗ i18n 锚点命中 %d 次：%s" % (s.count(a), a[:60]))
            return 1
        s = s.replace(a, b)
    if s.count(CUE_BLOCK_FIRST) != 1 or s.count(CUE_BLOCK_LAST) != 1:
        print("✗ i18n 区间边界不唯一")
        return 1
    lines = s.splitlines(keepends=True)
    ia = s[:s.index(CUE_BLOCK_FIRST)].count("\n")
    ib = s[:s.index(CUE_BLOCK_LAST)].count("\n")
    print("i18n 删除行 %d..%d（共 %d 行）" % (ia + 1, ib + 1, ib - ia + 1))
    lines2 = lines[:ia] + lines[ib + 1:]
    s2 = "".join(lines2)
    io.open(I18N, "w", encoding="utf-8", newline="\n").write(s2)
    print("i18n_map.py：%d → %d 行" % (len(lines), len(lines2)))

    # --- config ---
    c = io.open(CFG, encoding="utf-8").read()
    if c.count(CFG_OLD) != 1:
        print("✗ config cue 段锚点命中 %d 次" % c.count(CFG_OLD))
        return 1
    io.open(CFG, "w", encoding="utf-8", newline="\n").write(c.replace(CFG_OLD, ""))

    # --- 删文件 ---
    for f in DEL_FILES:
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            os.remove(p)
            print("已删除", f)
        else:
            print("（不存在，跳过）", f)

    # 语法检查
    import py_compile
    for f in ("src/i18n_map.py", "src/config.py"):
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
    print("i18n_map.py / config.py 语法 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
