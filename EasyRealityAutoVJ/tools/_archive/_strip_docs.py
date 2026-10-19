# -*- coding: utf-8 -*-
"""摘除码表（第 5 步）：把两份《使用说明》里的码表章节删掉，并改掉"三个界面"的措辞。

面向用户的文档必须和代码逐条对得上（项目铁律）：
现在只剩两页界面，图层类型也只有 手动 / 自动 / Kv 三种。
"""
import io
import os
import sys

ROOT = r"P:\AutoVJ"
ZH = os.path.join(ROOT, "使用说明.txt")
EN = os.path.join(ROOT, "使用说明_EN.txt")

ZH_REPS = [
    ("整个软件分成三个界面，共用同一套画面引擎，左上角页签常显：",
     "整个软件分成两个界面，共用同一套画面引擎，左上角页签常显："),
    ("  🎞 码表编排      编曲用：导入 .ercue 码表、排到拍上、绑定码表图层（试验功能）\n", ""),
    ("  在三个界面**都常显**，现场操作不受影响。", "  在两个界面**都常显**，现场操作不受影响。"),
    ("· 右上角「⋯ 更多」里收着：主题、语言、快捷键、关于、码表 / 时间码。",
     "· 右上角「⋯ 更多」里收着：主题、语言、快捷键、关于。"),
    ("  · 码表图层：画面由导入的 **.ercue 码表**决定（一首歌一份编排），见第十七节\n", ""),
    ("  · Kv 主视觉 = 紫金边框      · **码表图层 = 青绿边框**\n"
     "  码表图层那行还会多一个**徽标**：没绑码表时显示橙色的「码表：未绑定」，\n"
     "  绑好了显示码表名（点一下直接进码表编排界面）。它自己的 α / 锁定 / 混合三样\n"
     "  对「容器图层」没有意义，所以不显示 —— 每个片段的大小/位置/混合都在码表里逐片段设。\n",
     "  · Kv 主视觉 = 紫金边框\n"),
]
ZH_SEC_START = "十七、码表 / 时间码（试验功能）"
ZH_SEC_END = "本软件为测试版，功能仍在完善中，遇到问题欢迎反馈。"

EN_REPS = [
    ("The app is split into three pages that share one rendering engine. The tabs",
     "The app is split into two pages that share one rendering engine. The tabs"),
    ("  🎞 Cue Sheets    Authoring: import an .ercue sheet, lay it out on\n"
     "                   beats, bind it to a Cue Sheet layer (experimental)\n", ""),
    ("  Audio / GPU Decode) is **always visible on all three pages**, so nothing",
     "  Audio / GPU Decode) is **always visible on both pages**, so nothing"),
    ('· "⋯ More" (top-right) holds: theme, language, hotkeys, about and\n'
     "  Cue sheet / Timecode.",
     '· "⋯ More" (top-right) holds: theme, language, hotkeys and about.'),
    ("  · Cue Sheet — the picture follows an imported **.ercue** sheet\n"
     "                 (one arrangement per song), see section 17\n", ""),
    ("  · Kv Main Visual = violet border    · **Cue Sheet = teal border**\n"
     "A Cue Sheet row also shows a **badge**: an orange \"Cue sheet: not bound\" when\n"
     "nothing is bound yet, or the sheet's name once it is (click it to jump to the\n"
     "Cue Sheet editor). Its α / lock / blend controls are hidden because they mean\n"
     "nothing for a container layer — size / position / blend are set per clip\n"
     "inside the sheet.\n",
     "  · Kv Main Visual = violet border\n"),
]
EN_SEC_START = "17. Cue Sheets / Timecode (experimental)"
EN_SEC_END = "Beta software — still evolving. Feedback is welcome."


def strip_section(s, start_marker, end_marker, path):
    """删掉「start_marker 所在行之前的那条分隔线」到「end_marker 之前的分隔线」为止。"""
    if s.count(start_marker) != 1 or s.count(end_marker) != 1:
        print("✗ %s 章节边界不唯一" % path)
        return None
    i = s.index(start_marker)
    # 回退到该章节上方的分隔线行首
    j = s.rfind("─", 0, i)
    j = s.rfind("\n", 0, j) + 1
    k = s.index(end_marker)
    k = s.rfind("─", 0, k)                     # 结尾那条分隔线
    k = s.rfind("\n", 0, k) + 1
    if not (j < k):
        print("✗ %s 章节区间异常" % path)
        return None
    print("  %s：删 %d 字符（%d 行）" % (os.path.basename(path), k - j,
                                       s[j:k].count("\n")))
    return s[:j] + s[k:]


def main():
    for path, reps, sec in ((ZH, ZH_REPS, (ZH_SEC_START, ZH_SEC_END)),
                            (EN, EN_REPS, (EN_SEC_START, EN_SEC_END))):
        s = io.open(path, encoding="utf-8").read()
        for a, b in reps:
            if s.count(a) == 0 and (not b or b in s):
                print("  （%s 该处已处理，跳过）" % os.path.basename(path))
                continue
            if s.count(a) != 1:
                print("✗ %s 锚点命中 %d 次：%s" % (os.path.basename(path),
                                                  s.count(a), a.splitlines()[0][:60]))
                return 1
            s = s.replace(a, b)
        if sec[0] not in s:
            print("  （%s 章节已删除，跳过）" % os.path.basename(path))
            s2 = s
        else:
            s2 = strip_section(s, sec[0], sec[1], path)
            if s2 is None:
                return 1
        io.open(path, "w", encoding="utf-8", newline="\n").write(s2)
        left = [l for l in s2.splitlines() if ("码表" in l or ".ercue" in l
                                               or "Cue Sheet" in l or "Cue sheet" in l)]
        print("  %s 残留 %d 行" % (os.path.basename(path), len(left)))
        for l in left[:10]:
            print("     ", l.strip()[:100])
    return 0


if __name__ == "__main__":
    sys.exit(main())
