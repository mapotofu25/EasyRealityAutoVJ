# -*- coding: utf-8 -*-
"""补 4 条文案 + 修测试（位置现在是秒）。"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- i18n ----
P = os.path.join(ROOT, "src", "i18n_map.py")
E = [
    ("回到开头（0:00）", "Back to the start (0:00)"),
    ("横轴 = 时间（秒）；点选片段后在左边「4 属性」里改",
     "Horizontal axis = time (seconds); select a clip and edit it in \"4 Properties\""),
    ("音频播完了 —— 画面也停在这里（重来点 ⏮ 或 ▶）",
     "Audio finished - the picture stops here too (press ⏮ or ▶ to replay)"),
    ("这个文件里没有可解码的音频", "This file has no decodable audio"),
]
t = io.open(P, encoding="utf-8").read()
ls = t.split("\n")
tgt = None
for i, l in enumerate(ls):
    if l.startswith('        "Order:'):
        for j in range(i + 1, min(i + 24, len(ls))):
            if ls[j].strip() == "}":
                tgt = j
                break
assert tgt, "找不到 ZH2EN 收尾"
add = []
for zh, en in E:
    if ('"' + zh + '":') in t:
        continue
    add.append('    "%s":\n        "%s",' % (zh.replace('"', '\\"'), en.replace('"', '\\"')))
ls[tgt:tgt] = add
io.open(P, "w", encoding="utf-8", newline="").write("\n".join(ls))
print("i18n 补 %d 条" % len(add))

# ---- 测试：位置按秒 ----
P2 = os.path.join(ROOT, "tools", "_cue_pr_ui_test.py")
t2 = io.open(P2, encoding="utf-8").read()
old = ('    dlg._set_pos(10.0)\n'
       '    check("设位置 ⇒ 滑杆跟着走", dlg.sld.value() == 10, dlg.sld.value())\n'
       '    check("设位置 ⇒ 播放头跟着走", dlg.tl.playhead == 10.0, dlg.tl.playhead)\n'
       '    check("设位置 ⇒ 位置文字跟着走", "10.0" in dlg.lbl_pos.text(), dlg.lbl_pos.text())\n')
assert t2.count(old) == 1, t2.count(old)
new = ('    dlg._set_pos(10.0)                     # ★ 位置单位现在是**秒**\n'
       '    _bl = max(0.02, float(dlg._bl or 0.5))\n'
       '    check("设位置(秒) ⇒ 滑杆跟着走", dlg.sld.value() == 10, dlg.sld.value())\n'
       '    check("设位置(秒) ⇒ 播放头按拍换算",\n'
       '          abs(float(dlg.tl.playhead) - 10.0 / _bl) < 0.02, dlg.tl.playhead)\n'
       '    check("设位置(秒) ⇒ 位置文字是 mm:ss", "0:10" in dlg.lbl_pos.text(), dlg.lbl_pos.text())\n')
t2 = t2.replace(old, new)
# 播放推进的期望值也要按秒（原来按拍算）
old2 = ('    bl = float(dlg._bl or 0.5)\n'
        '    dlg._play_last = time.perf_counter() - 0.1     # 0.1s 在 dt 上限(0.25s)以内\n'
        '    dlg._play_tick()\n'
        '    want = 10.0 + 0.1 / bl\n')
if t2.count(old2) == 1:
    t2 = t2.replace(old2,
                    '    bl = float(dlg._bl or 0.5)\n'
                    '    dlg._play_last = time.perf_counter() - 0.1     # 0.1s 在 dt 上限(0.25s)以内\n'
                    '    dlg._play_tick()\n'
                    '    want = 10.0 + 0.1                    # ★ 位置是秒 ⇒ 直接加秒\n')
io.open(P2, "w", encoding="utf-8", newline="").write(t2)
print("测试已按秒更新")
