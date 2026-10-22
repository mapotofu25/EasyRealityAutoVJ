# -*- coding: utf-8 -*-
"""扫出：engine.py 里**没有 @_locked** 的公开方法，以及它们在 GUI 侧被谁调用。

判据：GUI（ui_main.py / panels.py / output_window.py）直接调用的引擎方法，
如果会读/写引擎状态（快照、滑块、素材、图层），就必须进同一把锁。
"""
import io
import os
import re
import sys

# 项目根：按脚本自身位置推导（项目已从 P:\AutoVJ 迁移到 P:\ERAVJ\AutoVJ）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = io.open(os.path.join(ROOT, "src", "engine.py"), encoding="utf-8").read().splitlines()

# 收集 engine 类内的方法名 + 是否有 @_locked
meths = []
cur_cls = ""
pend_deco = []
for i, l in enumerate(_src):
    if re.match(r"^class ", l):
        cur_cls = l.split()[1].split("(")[0]
        pend_deco = []
        continue
    s = l.strip()
    if s.startswith("@"):
        pend_deco.append(s)
        continue
    m = re.match(r"^    def (\w+)\(", l)
    if m and cur_cls == "AutoVJEngine":
        meths.append((m.group(1), i + 1, any("@_locked" in d for d in pend_deco)))
    pend_deco = []

no_lock = {n: ln for n, ln, lk in meths if not lk}
print("引擎方法 %d 个，其中无 @_locked %d 个" % (len(meths), len(no_lock)))

# ★★ 白名单：**故意不加锁**的无锁只读访问器（GUI 高频轮询用）。
#   加锁反而有害 —— 主线程每帧/每 0.5s 去等引擎渲染锁 ⇒ 界面卡成幻灯片。
#   原则（与 `latest_frame` 注释一致）：只读、GIL 原子、绝不碰渲染中间态。
__LOCKFREE_OK = {
    "latest_frame",      # 绘制端自取最新帧（信号只投序号，见 engine.py 帧队列说明）
    "modes_snapshot",    # 500ms 状态栏轮询模式（2026-10-08 加，修"软件非常卡"）
    # ★ 2026-10-10 加（修 `_update_status` 抢渲染锁的**残留 4 处**，见
    #   `_audit_20261010/FINAL_REPORT.md` P1-1）：
    #   引擎在 `_tick_locked` 里算好整个 dict 后**整体赋值**（GIL 下原子），
    #   GUI 只读、绝不碰渲染中间态 —— 与上面两个同理。
    #   值来源：switch_countdown / phrase_countdown / beat_grid_view / next_clip_for
    #   （前 3 个会推进网格时钟 ⇒ 由**渲染线程**统一推进，比原来 GUI 每 500ms 推一次更正确）。
    "ui_snapshot",
    # ★ 2026-10-17 加：`cuesheet_status` 本身**就没加锁**（纯读 `_cue_sheet` / `_cue_track` /
    #   `layers` 的**属性快照**，每次新建一个 dict 返回）。GUI 在 `refresh_header`
    #   (`panels.py:3062`) 里调它 —— 那条路上若真去抢 `_tick_lock`（2s 超时），
    #   就等于把铁律 13 那个「软件非常卡」原样再犯一次。**必须保持无锁**。
    #   安全性：局部变量各取一次（对象引用读取在 GIL 下原子）；最坏情况是拿到
    #   "换表进行中"的瞬时混合快照 —— 用途是状态标签，可接受。
    #   ⚠ 反例：任何**会改状态 / 读渲染中间态**的方法不许往这里加。
    "cuesheet_status",
    # ★ 2026-10-17 加：`set_cue_sheet` **不是只读访问器**，但 GUI 侧就是它的唯一调用方
    #   （`cue_ui` / `cue_page` / 新增的 `ui_main._restore_cue_sheets` 都在 GUI 线程装表）。
    #   为什么无锁是安全的：它内部**第一步**就 `clear_cue_sheet()`（把 `_cue_rt` 置 None），
    #   然后才写 `_cue_sheet` → 重建 runtime → 写 `_cue_rt`。tick 线程在中间任何时刻看到的
    #   要么是"没有表"（直接不接管），要么是"新表 + 新 runtime"，**不会出现表与 runtime 错配**。
    #   ⚠ 它若真去抢 `_tick_lock`（2s 超时），装表时就会冻界面 —— 恰恰是铁律 13 禁止的。
    "set_cue_sheet",
    # ★ 2026-10-17 加：`set_audio_engine` 只做一件事 —— `self._audio_engine = audio_engine`
    #   （**引用赋值**，GIL 下原子）。调用方是 `ui_main.py:2272`，**构造期一次**，
    #   早于引擎线程 `start()`（同函数 2275 行）⇒ 不存在与渲染线程并发读写的窗口。
    #   ⚠ 它**不读不写**任何渲染/码表状态；若将来它要在运行中改内容，必须回到加锁。
    #   ⚠ 为什么不能写成 `@_locked`：`_tick_lock` 在 `__init__` 里才创建，而本方法
    #     与 `set_song_genres` 同属"注入引用"族，加锁只会在启动路径上多一次等锁风险。
    "set_audio_engine",
}

# GUI 侧调用点
GUI = ["ui_main.py", "panels.py", "output_window.py", "main.py"]
hits = []
for f in GUI:
    p = os.path.join(ROOT, "src", f)
    if not os.path.isfile(p):
        continue
    for i, l in enumerate(io.open(p, encoding="utf-8").read().splitlines()):
        for m in re.finditer(r"(?:engine|self\.engine)\.(\w+)\(", l):
            name = m.group(1)
            if name in no_lock and name not in __LOCKFREE_OK:
                hits.append((f, i + 1, name, no_lock[name], l.strip()[:100]))

print("\nGUI 直接调用、但引擎侧**没有加锁**的方法（按名字汇总）：")
print("（白名单内故意无锁：%s）" % ", ".join(sorted(__LOCKFREE_OK)))
seen = {}
for f, ln, name, dl, txt in hits:
    seen.setdefault(name, []).append((f, ln, dl, txt))
for name, v in sorted(seen.items()):
    print("\n  %s  （引擎定义在第 %d 行，无 @_locked；GUI 调用 %d 处）" % (name, v[0][2], len(v)))
    for f, ln, dl, txt in v[:6]:
        print("     %s:%d  %s" % (f, ln, txt))
hit_names = set(seen.keys())
whitelisted_only = hit_names <= __LOCKFREE_OK
print("")
if not seen:
    print("结论：✅ 所有被 GUI 调用的引擎方法都已加 @_locked（白名单外无遗漏）")
else:
    print("结论：⚠ 上列方法被 GUI 调用但未加锁 —— 需确认是否应进锁，"
          "或加入本脚本顶部的白名单（并写清「为什么无锁是安全的」）")
