# -*- coding: utf-8 -*-
"""记录：两个金框 + 素材卡住不动，两个问题的根因与修复。"""
import io
import os

MEM = r"C:\Users\Administrator\WorkBuddy\自动VJ软件\.workbuddy\memory"
LOG = os.path.join(MEM, "2026-10-01.md")

TXT = """

## 九、两个新反馈：「不在逐拍为什么有两个框」+「素材会突然卡住不动」

### ① 两个金框 —— **我上一轮修高亮时埋的**
`refresh_playing` 改按路径比对时，我顺手把老代码里的 `trans_active` 条件去掉了
（老写法是 `if lay.trans_active and 0 <= lay.prev_idx < …`），改成无条件把
`prev_idx` 也算进"正在使用"。而**引擎过渡结束后不会把 `prev_idx` 重置成 -1**
（`grep "prev_idx = -1"` 只有 4 处：初始化 / 换池 / 硬切），所以**永远有两个金框**。
⇒ 修：只在 `lay.trans_active` 为真时才把 `prev_idx` 算上。
回归测试 `tools/_test_hl_prev.py`（非过渡 1 个框、过渡中 2 个框、prev=-1 时 1 个）。

### ② 素材卡住不动、直到下一次切进来才恢复 —— `_loop` 一次失败就**永久死亡**
`_loop` 原来：
- `self.c, self.st = self._open()` 抛异常 ⇒ **直接 `return`**（播放器死掉）；
- 取帧生成器一收工（`StopIteration`）⇒ **直接 `break`**（线程结束）。
两条路都不会再产生新帧，而引擎画的是 `player.current()` 的**旧 `_img`**
⇒ 画面**停在最后一帧**，直到下一次切素材新建播放器才恢复 —— 与用户描述逐字吻合。
（我上一轮加的"空转 30 趟放弃"会让这个情况**更容易命中**，但根因是原来的"一次失败即死"。）
⇒ 修：打开失败 / 取帧收工 ⇒ **退避后重开容器再试**（最多 `_REOPEN_MAX=6` 轮），
只有连续失败 6 次才真收工；出过帧就把计数清零（好素材永远不会被误放弃）；
期间一律 sleep 退避，**绝不忙等**。
回归测试 `tools/_test_freeze_heal.py`（打开失败 2 次仍能出帧；一直失败则退避收工+记日志）。

### 教训
**改一处逻辑前，先看清它依赖的"状态约定"**：`prev_idx` 的语义是"过渡期间的上一个"，
它**不会**在过渡结束后自动清空 —— 我按"它就是上一个"来用，就错了一整轮。
"""

io.open(LOG, "a", encoding="utf-8").write(TXT)
print("已写入", LOG)
