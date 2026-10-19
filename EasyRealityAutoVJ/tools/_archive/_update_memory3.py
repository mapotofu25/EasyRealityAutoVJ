# -*- coding: utf-8 -*-
"""MEMORY.md：补指纹链路的关键约定 + 实测数字；同时压掉等量旧内容（必须留在注入上限内）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")
t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))

# ---- ① 码表小节：补指纹版本 ----
old = "- ⚠ **指纹认歌 → 自动切曲目：还没接**（`_bind` 传的是手选那首；引擎不读 `recognized_song_id`）。\n  位置只能来自位置源（没位置源 ⇒ 不接管）。"
new = ("- ★★ **指纹认歌已接入**（2026-09-29）：`ChargeBarEngine`（VJVision 同源漏桶状态机）输出\n"
       "  `snap[\"recognized_song_id\"/\"recognized_offset\"/\"recognized_t\"/\"recognized_votes\"/\"recognized_rate\"]`\n"
       "  ⇒ `engine._cuesheet_autoswitch()` 认歌切曲目（**sid 变化本身就是已确认的换歌，码表侧别再自造回滞**）。\n"
       "  ⚠ `recognized_t` 是 **perf_counter**，不是 epoch。\n"
       "  ★ **混音不抖是状态机给的**：`slot_open = cur_bar < BAR_CAP`，槽内歌满格时**不换槽**；\n"
       "  旧歌没票后每拍漏电、约 10 拍（≈5s）才交接 ⇒ \"两首叠在一起\"时画面稳、旧歌走了才换。\n"
       "  ★ 位置：设备为权威；没位置源时 `_cue_pos_from_fingerprint()` 兜底（>=40 票、<=3s 内的结果）。\n"
       "  ★ **曲目匹配四级**（`CueSheet.match_track`）：指纹id → 名+艺人+**时长** → **名+艺人** → 文件名。\n"
       "  ⚠ `Identity.meta_key` 把时长取整进 key ⇒ 两端差 >2s 就命不中，**跨机器靠第 3 级**。")
assert t.count(old) == 1, t.count(old)
t = t.replace(old, new)

# ---- ② 节拍网格：压掉 VDJ 那行细节（REFERENCE 里有）----
old2 = ("- 数据源优先 **VirtualDJ 库**（`W:\\VirtualDJ\\database.xml`）；字段语义/三级匹配/本机 3 个库的坑见 "
        "REFERENCE §五。无 VDJ 才走本地 Beat This。\n")
assert t.count(old2) == 1, t.count(old2)
t = t.replace(old2, "")

# ---- ③ 工作方式：补两条这轮踩的坑 ----
old3 = "- ⚠★ **补丁锚点要够长**"
new3 = ("- ⚠★ **测试夹具自己造，不依赖用户目录**：用户删了自己机器上的 `示例码表.ercue` ⇒ 界面测试全挂。\n"
        "  看 `tools/_cue_demo.py`（现场造自包含 .ercue）。⚠ `cv2.imwrite` 在**中文路径上静默失败** ⇒ 用\n"
        "  `cv2.imencode` + `open(wb)`。\n"
        "- ⚠★ **往 `i18n_map` 加条目：直接插在 `ZH2EN` 收尾 `}` 之前**（别追加到文件尾再搬，会把 `}` 搬丢/复制整块）；\n"
        "  多行中文键写成 `\"...\\n...\"` 转义。\n"
        "- ⚠★ **补丁锚点要够长**")
assert t.count(old3) == 1, t.count(old3)
t = t.replace(old3, new3)

io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
