# -*- coding: utf-8 -*-
"""再精简 MEMORY.md（回到注入上限以内）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "MEMORY.md")

# ---- 节拍网格：把 Beat This 的细节压成两行（细节在 REFERENCE §五 / 日志）----
OLD_BEAT = """- ⚠★★ **八拍相位才是难点，BPM 本就算得准**：现有「冷启动」离线方法判小节相位 **9/9 首全错** ⇒ 无 VDJ 数据时不可信。0/4 判反靠手动「八拍相位翻转」修（人工兜底，**不能当主方案**）。
- ★★ **本地小节识别 = `src/phrase_bt.py`（Beat This! / CPJKU，代码与权重均 MIT 含商用）**：自转
  `small0_fixed.onnx`（9.9 MB，`onnxruntime` 已在包里 ⇒ 零新依赖、不装 torch），**小节相位 21/21 判对**。
  **默认窗口 60s（1.91 秒/首），拍数不足自动延到 180s**；前处理与模型来源见 REFERENCE §五。
  - **定锚点三铁律**：① 不能用 BT 全局 BPM 展开小节序号（0.2~0.8% 误差会累积）；② 不能拿 downbeat
    直线拟合（会多检/漏检）；③ 正解 = **bar 抗离群 LS（`_fit_bar` 3 轮）+ 相位中位数折叠**。
  - ⚠ **BT 只到小节级（4 拍），8 拍乐句头仍要自己判**（低频重音/换和弦）。
- ★★ **引擎拍钟跟不上变速**：`beat_interval` 每块被 `60/bpm_smooth` 重设 + 锁定后只接受 ±8 BPM ⇒ **>±5~6% 冻结**；要跟变速须**另立"网格驱动"路径**（`engine._beat_pos()`），不是调参。
- ★★ **指纹认歌的变速容忍度**：**开 keylock ⇒ ±10% 仍能认出**；**关 keylock ⇒ 彻底失效**。⚠ 变速下 `recognized_offset` 应为 `off + 4.5*r`。
- ⚠ `phrase_scores()` **必须传全局拍号 `ks`**（否则整体平移）。"""
NEW_BEAT = """- ⚠★★ **八拍相位才是难点，BPM 本就算得准**：现有「冷启动」离线方法判小节相位 **9/9 首全错** ⇒ 无 VDJ 数据时不可信。0/4 判反靠手动「八拍相位翻转」修（人工兜底，**不能当主方案**）。
- ★★ **本地小节识别 = `src/phrase_bt.py`（Beat This! / CPJKU，代码与权重均 MIT 含商用）**：自转
  `small0_fixed.onnx`（9.9 MB，`onnxruntime` 已在包里 ⇒ 零新依赖、不装 torch），**小节相位 21/21 判对**，
  **默认窗口 60s（≈1.9 秒/首），拍数不足自动延到 180s**。定锚点三铁律 / 前处理 / 模型来源见 REFERENCE §五。
  ⚠ **BT 只到小节级（4 拍），8 拍乐句头仍要自己判**。
- ★★ **引擎拍钟跟不上变速**：`beat_interval` 每块被 `60/bpm_smooth` 重设 + 锁定后只接受 ±8 BPM ⇒ **>±5~6% 冻结**；要跟变速须**另立"网格驱动"路径**（`engine._beat_pos()`），不是调参。
- ★★ **指纹认歌的变速容忍度**：**开 keylock ⇒ ±10% 仍能认出**；**关 keylock ⇒ 彻底失效**（`recognized_offset` 应为 `off + 4.5*r`）。⚠ `phrase_scores()` 必须传全局拍号 `ks`。"""

# ---- 铁律：压 6/7/8 的措辞（保留全部关键信息）----
OLD_IRON = """6. ★ **跨线程投帧必须「轻量信号 + 单份最新帧」**，绝不把整幅图塞进信号参数（每帧 8MB 入队 → 30 秒涨 3.2GB）。参照 `engine.frame_ready = Signal(int)` + `latest_frame()`。
7. ★★ **`engine._tick_lock` = 引擎↔GUI 串行化总锁，GUI 侧必须带超时**：引擎整帧渲染期间持有它，**9 个 GUI 入口**（`toggle_run`/`next_scene`/`manual_transition`/`_switch_layer_to`/`rebuild_from_cfg`/`push_layers_to_cfg`/`set_song_genres`/`_refresh_auto_layers`/`close`）也要拿它 ⇒ 引擎卡一帧连「停止/关闭」都永久等待。已改 `acquire(timeout=2.0)`，失败记日志放弃。**不要改回 `with self._tick_lock:`**。"""
NEW_IRON = """6. ★ **跨线程投帧必须「轻量信号 + 单份最新帧」**，绝不把整幅图塞进信号参数（每帧 8MB 入队 → 30 秒涨 3.2GB）。参照 `engine.frame_ready = Signal(int)` + `latest_frame()`。
7. ★★ **`engine._tick_lock` = 引擎↔GUI 总锁，GUI 侧必须带超时**：引擎整帧渲染期间持有它，而 **9 个 GUI 入口**也要拿它 ⇒ 引擎卡一帧连「停止/关闭」都永久等待。已改 `acquire(timeout=2.0)`，失败记日志放弃。**不要改回 `with self._tick_lock:`**。"""

# ---- 能量与节奏：把重复的「写文档要核实」并进工作方式那条 ----
OLD_EN = """- ⚠ **写 README / 使用说明必须逐条对照代码核实**（**界面文案权威清单 = `i18n_map.py`**），
  不能凭旧文档或记忆推断（曾一次写错 5 处）。
- 其余细项（滞回值 / 拍位 PLL / 手动「下一素材」/ 对齐阈值 / HUD 计数 / 振幅档 / 废弃残留）见 REFERENCE §五。"""
NEW_EN = """- ⚠ **写 README / 使用说明必须逐条对照代码核实**（**界面文案权威清单 = `i18n_map.py`**），
  不能凭旧文档或记忆推断（曾一次写错 5 处）。其余细项见 REFERENCE §五。"""

t = io.open(P, encoding="utf-8").read()
before = len(t.encode("utf-8"))
for a, b in ((OLD_BEAT, NEW_BEAT), (OLD_IRON, NEW_IRON), (OLD_EN, NEW_EN)):
    assert t.count(a) == 1, (t.count(a), a[:40])
    t = t.replace(a, b)
io.open(P, "w", encoding="utf-8", newline="").write(t)
print("MEMORY.md  %d -> %d 字节" % (before, len(t.encode("utf-8"))))
