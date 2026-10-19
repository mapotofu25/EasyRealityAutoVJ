# -*- coding: utf-8 -*-
"""再压 MEMORY.md 回到注入上限内。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")
t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))

PAIRS = [
    # 指纹块压紧
    ("- ★★ **指纹认歌已接入**（2026-09-29）：`ChargeBarEngine`（VJVision 同源漏桶状态机）输出\n"
     "  `snap[\"recognized_song_id\"/\"recognized_offset\"/\"recognized_t\"/\"recognized_votes\"/\"recognized_rate\"]`\n"
     "  ⇒ `engine._cuesheet_autoswitch()` 认歌切曲目（**sid 变化本身就是已确认的换歌，码表侧别再自造回滞**）。\n"
     "  ⚠ `recognized_t` 是 **perf_counter**，不是 epoch。\n"
     "  ★ **混音不抖是状态机给的**：`slot_open = cur_bar < BAR_CAP`，槽内歌满格时**不换槽**；\n"
     "  旧歌没票后每拍漏电、约 10 拍（≈5s）才交接 ⇒ \"两首叠在一起\"时画面稳、旧歌走了才换。\n"
     "  ★ 位置：设备为权威；没位置源时 `_cue_pos_from_fingerprint()` 兜底（>=40 票、<=3s 内的结果）。\n"
     "  ★ **曲目匹配四级**（`CueSheet.match_track`）：指纹id → 名+艺人+**时长** → **名+艺人** → 文件名。\n"
     "  ⚠ `Identity.meta_key` 把时长取整进 key ⇒ 两端差 >2s 就命不中，**跨机器靠第 3 级**。",
     "- ★★ **指纹认歌已接入**（2026-09-29）：`ChargeBarEngine`（VJVision 同源漏桶机）输出 `snap[\"recognized_*\"]`\n"
     "  ⇒ `engine._cuesheet_autoswitch()` 认歌切曲目（**sid 变化=已确认的换歌，码表侧别自造回滞**）；\n"
     "  没位置源时 `_cue_pos_from_fingerprint()` 兜底（>=40 票、<=3s）。⚠ `recognized_t` 是 **perf_counter**。\n"
     "  ★ **混音不抖是状态机给的**：槽内歌满格时不换槽；旧歌没票后漏电约 10 拍(≈5s)才交接。\n"
     "  ★ **曲目匹配四级**（`CueSheet.match_track`）：指纹id → 名+艺人+**时长** → **名+艺人** → 文件名\n"
     "  （`meta_key` 把时长取整进 key ⇒ 差 >2s 命不中，**跨机器靠第 3 级**）。"),
    # Beat This 块压紧
    ("- ★★ **本地小节识别 = `src/phrase_bt.py`（Beat This! / CPJKU，代码与权重均 MIT 含商用）**：自转\n"
     "  `small0_fixed.onnx`（9.9 MB，`onnxruntime` 已在包里 ⇒ 零新依赖、不装 torch），**小节相位 21/21 判对**，\n"
     "  **默认窗口 60s（≈1.9 秒/首），拍数不足自动延到 180s**。定锚点三铁律 / 前处理 / 模型来源见 REFERENCE §五。\n"
     "  ⚠ **BT 只到小节级（4 拍），8 拍乐句头仍要自己判**。",
     "- ★★ **本地小节识别 = `src/phrase_bt.py`（Beat This! / CPJKU，MIT 含商用）**：自转 `small0_fixed.onnx`\n"
     "  （9.9 MB，`onnxruntime` 已在包里 ⇒ 零新依赖），**小节相位 21/21 判对**，默认窗口 60s（≈1.9 秒/首），\n"
     "  拍数不足自动延到 180s。定锚点三铁律 / 前处理 / 模型来源见 REFERENCE §五。\n"
     "  ⚠ **BT 只到小节级（4 拍），8 拍乐句头仍要自己判**。"),
]

for a, b in PAIRS:
    assert t.count(a) == 1, (t.count(a), a[:40])
    t = t.replace(a, b)
io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
