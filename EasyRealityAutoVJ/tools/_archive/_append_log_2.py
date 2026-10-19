# -*- coding: utf-8 -*-
"""追加今日日志（用 Write+执行，避免 shell 吃掉正文里的反引号）。"""
import io
import os

P = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                 "WorkBuddy", "自动VJ软件", ".workbuddy", "memory", "2026-09-28.md")

ADD = '''

---

## ★★ 10:30~ 用户 6 条追问/要求（新建码表 → 图层优先级 → 指纹 → 播放 → 时间轴操作）

### ① 「怎么没有新建码表的选项啊」
确实没有 —— 之前只有「导入…」，手上没有 .ercue 的人根本进不来。补了两样：
- 顶栏 **「新建…」**：`QInputDialog` 起名 → `cuesheet.new_sheet()` + 1 首曲目 + 1 个「背景」层，
  **不落盘**（`path=""`，列表标「（未保存）」），导出时才写文件；重名自动加序号；建完自动停在「① 曲目」页。
- ② 标签页 **「＋ 素材…」**：⚠ 以前**根本没有把素材加进码表的入口**（`cuesheet.media` 只能来自导入的包），
  所以就算新建了空码表也没素材可选。现在选视频/图片直接加进 `sheet.media`（同名复用、不拷贝文件），
  时长用 PyAV 读容器 duration（不解码）。
- `_import_from` 的登记尾巴抽成 **`_register_sheet()`**（新建/导入共用）；新增 `_sheet_label()` /
  `_refresh_sheet_labels()`；导出后记住路径 ⇒ 标签里的「（未保存）」消失。
- ② 标签页顶部加了「顺序：新建/导入 → 加素材 → 绑歌 → 排片段 → 导出」的上手提示。

### ② 「现在的码表图层可以按照图层的优先级显示逻辑吗」
- **可以**（查了代码确认）：`_compose` 是 `for lay in reversed(self.layers)`，index 0 最后画 = 最上层；
  码表图层在这个循环**内部**处理（在 `not lay.player` 之前拦截）⇒ **与普通图层同一套优先级** ✓
- 时间轴里层序也是 `reversed(sorted_layers())`（z 大的在上面）⇒ 与主界面一致 ✓
- ⚠ **但发现 Solo 没接上**：旧代码在 solo 判断**之前**就 `continue` 了 ⇒ 别的图层按 S 时，
  码表图层照样盖在最上面（图层头上那个 S 是给用户点的，必须真管用）。**已修**。

### ③ 「现在的码表可以匹配指纹吗」
**没有。** 查清楚了，诚实结论（三处缺口）：
1. `_bind()` 传的是**界面上手选的那一首**（`track=self._track()`），不是认出来的；
2. 引擎里 **没有任何"认出新歌 ⇒ 换曲目"的路径** —— `_cuesheet_tick` 只吃 `_deck_state`（位置源），
   `snap["recognized_song_id"]` 完全不参与码表路径；
3. 界面里「指纹 id」是**只读且永远空**（曲库扫描没往曲目里写 fp_id；fp 库用的 id 是它自己的内部编号）。
⇒ 现状：**单曲目码表能用（手选哪首就是哪首）；多曲目不会自动切**。
★ **已把界面/手册里"自动认歌切曲目"的说法改成与现状一致**（不能把没接上的功能写进用户文档）。
★ 顺手让「切曲目立刻生效」成真：`_pick_track` 现在会 `_rebuild_runtime()`
（以前只换时间轴 ⇒ "选了另一首但画面还是上一首"，很容易误会成坏了）。

### ④⑤⑥ 「要有一个播放」「时间轴要能拖动」「按 PR 的逻辑复刻滚轮」
- **播放条**：`⏮ ｜ ▶/⏸ ｜ ☐循环 ｜ 位置滑杆`。▶ = 按曲目 BPM **实时**推进（33ms 定时器，
  `dt` 上限 0.25s 防卡顿跳远），预览 + 播放头 + 时间轴一起走；点 ▶ 会**自动把「模拟」位置源起起来**；
  切走页面 / 停位置源 / 关界面都自动停播。位置统一走 `_set_pos(b)`（四处同步）。
- **时间轴 PR 式操作**：滚轮=上下滚动；Shift+滚轮=左右平移；**Ctrl+滚轮=以鼠标所在拍位为锚缩放**；
  Alt+滚轮=行高；**中键拖动 / 空白处左键拖动=平移**。图层多时右侧有细滚动指示；
  行高与偏移都参与 `_hit`（滚完点选依然准）。
- ⚠ **没做音频试听**：`PySide6.QtMultimedia` 在这个 venv 里**没有后端插件**
  （只有 QtMultimedia.pyd 0.5MB，`plugins/` 下没有 multimedia 目录）⇒ 装了也放不出声。
  要"边听边对"得另找通路（PyAV 解码 + soundcard 播放，有 COM/线程风险），未做。

### 发布
- 版本 **2026.09.28.12**，包 `EasyRealityAutoVJ_测试版092812.zip`；冒烟 40 秒通过；
  13 个测试文件全量回归全绿；i18n 611 处 / 856 词条全覆盖。
- ⚠ `dist/` **仍然被占**（第 3 次改名/删除失败），这轮产物继续落 `dist_new/`。

### ⚠ 这轮补丁踩的坑（记下来别再犯）
1. **`adds` 的插入文本里不要自己再写一遍锚点行**（我写了，结果生成了两个连续 `def`，IndentationError）。
2. **`adds`/`pairs` 的锚点太短会匹配到错误位置**：`self._drag = None` 在
   `mouseReleaseEvent` 里也出现（8 空格缩进），状态被插进了方法中间 ⇒ 属性不存在。
   **锚点要么够长，要么用行号切片**；改完**必跑一次真测试**（编译能过、跑起来才炸）。
3. 测试里构造 `QWheelEvent` / `QMouseEvent` 直接调 `tl.wheelEvent(ev)` / `tl.mousePressEvent(ev)`
   最省事；**Qt 约定"往上滚 angleDelta 为正"** —— 断言方向别写反。
'''

t = io.open(P, encoding="utf-8").read()
if "10:30~ 用户 6 条追问" not in t:
    io.open(P, "w", encoding="utf-8", newline="").write(t.rstrip() + ADD)
    print("已追加，共 %d 字节" % len(io.open(P, encoding="utf-8").read().encode("utf-8")))
else:
    print("已存在，跳过")
