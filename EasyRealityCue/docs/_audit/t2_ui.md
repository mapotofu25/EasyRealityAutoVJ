# ui 层审计报告

## 小结
- 审计文件：7 个 / 3,455 行（window.py 1571 / timeline_widget.py 659 / panels.py 464 /
  props_panel.py 303 / dialogs.py 190 / preview.py 165 / theme.py 103）；发现 P0 0 / P1 3 / P2 9 / P3 3
- 最值得关注：**只要关窗（或"关闭码表"）就直接丢光所有未保存编排，且 3 处 session 切换路径里有两处没停播**；
  "内嵌素材"导出后接收方根本读不到素材（`extract_media` 写好了却从没被 UI 调用）——
  这两条正对应用户说的"根本无法正常使用/数据丢"。

> 说明：本报告为**只读审计**，未改动任何 `src/**`。每条发现均给出 `文件:行号` + 真实代码片段。
> 部分"跨层"契约问题（如内嵌素材）根因在 IO 层，但**触发点在 UI**，故按调用方缺失记录在 ui 层。

---

## 发现清单

### [P1] 关闭窗口 / 关闭码表：未保存的编排被静默丢弃，无任何提示
- **位置**：`src/ercue/ui/window.py:1545`（`closeEvent` / `shutdown`）、`src/ercue/ui/window.py:485`（`_close_sheet`）
- **现象**：用户排了半天片段，直接点右上角 × 或菜单「码表 ▾ → 关闭」，程序**不问一句**就退出 /
  移除该码表，所有未导出改动全部丢失。界面上明明有 `dirty` 标记（标题里显示"（未保存）"），
  但关窗时对 `dirty` **完全不检查**。
- **根因**：`closeEvent` 只做资源释放（停定时器 / 停音频 / 停线程），没有任何"脏数据拦截 / 询问保存"逻辑；
  `_close_sheet` 同样直接 `pop` 掉 session。
- **证据**：
  ```python
  # window.py:1545
  def closeEvent(self, e):
      self.shutdown()
      super().closeEvent(e)

  def shutdown(self):
      """关闭时释放解码器与后台线程、音频（A4/A6/P2）。"""
      try:
          self._seek_timer.stop()
          self._audio_poll.stop()
      ...
  ```
  ```python
  # window.py:485
  def _close_sheet(self):
      s = self.session
      if s is None:
          return
      self._stop_play()
      self.audio.stop()
      self.sessions.pop(self.current_key, None)
      self._log("已关闭码表：%s（磁盘文件没动）" % s.sheet.title)
      ...
  ```
- **影响**：**用户级数据丢失**。多码表场景下，"关闭一张码表"甚至没有"未保存"提示就丢弃。
- **建议**：**修**。在 `closeEvent` / `_close_sheet` 里遍历 `self.sessions` 的 `dirty`，
  有脏数据就弹"是否导出/放弃"；`closeEvent` 里用户在弹窗选"取消"时必须 `e.ignore()`。
- **置信度**：高

### [P1] "内嵌素材"导出后接收方打不开：`extract_media` 写好了却从没被调用
- **位置**：`src/ercue/model/ercue_io.py:245`（`load` 里约定的嵌入目录）、`src/ercue/model/ercue_io.py:275`（`extract_media`）、
  调用点应在 `src/ercue/ui/window.py:514`（`import_sheet`）
- **现象**：导出弹窗明确承诺"把素材一起打包进 .ercue（上限 500 MB，**别人换机也能打开**）"，
  但接收方（另一台机器 / 原素材已删）打开这张码表后，**所有片段都显示"文件丢失"**。
- **根因**：`save(embed=True)` 把素材写进 zip 的 `media/`；`load` 里约定的嵌入目录是 `<path>.media`；
  但**没有任何代码调用 `extract_media()` 去把 zip 里的 `media/` 解出来**，UI 导入时也没建这个目录。
  UI 的 `import_sheet` 只把 `sess.dir` 设为 `os.path.dirname(path)` 并靠 `_remount_media()` 扫同目录，
  zip 内部素材永远扫不到。
- **证据**：
  ```python
  # ercue_io.py:245 —— load 约定的嵌入目录
  return raw, path + ".media"
  ...
  # ercue_io.py:266
  md = media_dir if media_dir is not None else (
      embed_dir if embed_dir and os.path.isdir(embed_dir) else None)
  ```
  ```python
  # ercue_io.py:275 —— 提取函数存在，但全库无任何调用
  def extract_media(pkg_path, dest_dir) -> dict:
      """把包内 `media/` 解到 `dest_dir`，返回 `{basename: path}`。"""
      ...
  ```
  （全仓 `grep extract_media` 只命中定义与 `__all__`，**零调用点**。）
- **影响**：内嵌素材这一整条功能**名存实亡**——恰是跨机分发（DJ 把码表给现场机）的核心场景。
- **建议**：**修**。`import_sheet` 中：当 `info["embedded"]` 为真时，调
  `ercue_io.extract_media(path, path + ".media")`，并把 `sess.dir`/`media_paths` 指过去。
- **置信度**：高

### [P1] 撤销 / 重做后当前曲目被强制切回第 1 首
- **位置**：`src/ercue/ui/window.py:1442`（`_restore_snapshot`）
- **现象**：正在编辑第 2、3 首曲目的片段，按一次 Ctrl+Z / 点「撤销」，
  **界面立刻跳回第 1 首曲目**的时间轴，用户丢失编辑上下文（且 `_ti` 也不在撤销快照里）。
- **根因**：`_restore_snapshot` 无条件 `self._ti = 0`。
- **证据**：
  ```python
  # window.py:1442
  def _restore_snapshot(self, sess, snap):
      sess.load_snapshot(snap)
      sess.dirty = True
      self.timeline.set_selected("", "")
      self.props_panel.item = None
      self.props_panel.layer = None
      self._ti = 0                      # ← 无论撤销前在第几首，都归零
      self._refresh_all()
  ```
  （另注：撤销快照 `SheetSession.snapshot()` 只含 `sheet`，不含 `media_paths` / `track_audio`，
  所以"导入音频"后再撤销，`identity` 回滚了但 `track_audio[i]` 不回滚，二者会不一致。）
- **影响**：多曲目编辑时每次撤销都打断上下文；撤销后音频映射与曲目身份可能不同步。
- **建议**：**修**。把 `_ti`（以及 `track_audio`、`media_paths`）纳入 session 快照一起存/取。
- **置信度**：高

### [P2] 新建 / 导入码表时未停播，音频"串台"
- **位置**：`src/ercue/ui/window.py:454`（`new_sheet`）、`src/ercue/ui/window.py:514`（`import_sheet`），
  对照正确写法 `src/ercue/ui/window.py:468`（`_switch_to` 有 `_stop_play()`）
- **现象**：播放中新建 / 导入一张码表，**上一首歌继续在响**（或从 0 重放），
  而时间轴 / 预览已经切到新码表，画面与声音完全对不上。
- **根因**：`_switch_to` / `_close_sheet` / `_on_track_picked` 都先 `_stop_play()`，
  但 `new_sheet` 与 `import_sheet` **漏了**；它们结尾的 `_set_pos(0.0)` 在"播放中且音频驱动"时
  会触发 `_audio_seek`，把**尚未卸载的旧音频**重新起播。
- **证据**：
  ```python
  # window.py:468 _switch_to（正确）
  def _switch_to(self, key):
      if key and key in self.sessions and key != self.current_key:
          self._stop_play()                          # 换码表先停播（音频不留线程）
  ```
  ```python
  # window.py:454 new_sheet（漏停播）
  def new_sheet(self, title=None) -> str:
      self._key_seq += 1
      ...
      self._refresh_all()
      self._set_pos(0.0)      # ← 播放中会 seek 仍在内存里的旧音频
      return key
  ```
- **影响**：换码表音频不停，用户以为软件"乱响"。
- **建议**：**修**。`new_sheet` / `import_sheet` 开头补 `self._stop_play()`（与 `_switch_to` 对齐）。
- **置信度**：高

### [P2] 时间轴：点击空白处**无法取消选择**（取消选择分支不可达，被"平移"抢先）
- **位置**：`src/ercue/ui/timeline_widget.py:559`（`mousePressEvent`）
- **现象**：选中某片段后，想点空白处"取消选中"——没反应；
  属性面板（④）还停在刚才那个片段，`删除 cue` 也仍作用于它。点空白反而进入拖拽平移状态（光标变小手掌）。
- **根因**：左键在"未命中 item"时**先**进入 pan 分支并 `return`，导致下方那段
  `picked.emit("","")`（取消选择）**永远执行不到**——它是死代码。
- **证据**：
  ```python
  # timeline_widget.py:559
  def mousePressEvent(self, e):
      hit = self._hit(e.position())
      if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and hit[1] is None):
          self._pan = (e.position().x(), e.position().y(), self.view0, self.v_off, False)
          self.setCursor(Qt.ClosedHandCursor)
          e.accept()
          return                       # ← 左键点空白（hit[1] is None）从这里就返回了
      if e.button() != Qt.LeftButton:
          return
      ly, it, row, edge = hit
      if ly is None or it is None:
          if ly is not None and hit[3] == "":   # ← 左键永远走不到这里（上面已 return）
              self.picked.emit("", "")
          return
  ```
- **影响**：选择"粘住"放不开，用户误删/误改风险上升。
- **建议**：**修**。区分"空白点击（应取消选择）"与"空白拖动（才平移）"；或至少让
  `picked.emit("","")` 在空白左键按下时可达。
- **置信度**：高

### [P2] "点层名选中该层"未实现（`layer_picked` 死信号）＋「删除层」会默认删掉第一层
- **位置**：`src/ercue/ui/timeline_widget.py:92`（信号声明）、`src/ercue/ui/window.py:907`（`del_layer`）
- **现象**：（a）时间轴左侧层名列点了没反应，无法用它选中某层；
  （b）点「删除层」若当前没选中任何层，**会直接删掉第一层（z 最大的顶层）**，而不是提示"先选一层"。
- **根因**：
  - `layer_picked = Signal(str)` 声明了"点层名 → 选中该层"，但**全仓无一处 `emit`，也无一处 `connect`**（死信号）；
  - `del_layer` 依赖 `timeline.selected_layer()`，而 `selected_layer()` 在无选中时**回退到第一层**，
    于是 `if ly is None` 的"先点一下要删的那一层"提示分支基本永不触发。
- **证据**：
  ```python
  # timeline_widget.py:92（声明后从未 emit / connect）
  layer_picked = Signal(str)                    # 点层名 → 选中该层
  ```
  ```python
  # timeline_widget.py:179 —— 无选中时回退第一层
  def selected_layer(self):
      ly, _it = self.selected_item()
      if ly is not None:
          return ly
      lays = self._layers()
      return lays[0] if lays else None
  ```
  ```python
  # window.py:907
  def del_layer(self):
      tr = self._track()
      ...
      ly = self.timeline.selected_layer()   # ← 无选中时 = 第一层，不是 None
      if ly is None:                        # ← 这个保护基本不会命中
          self._log("先在时间轴上点一下要删的那一层。")
          return
  ```
- **影响**：层选择不可预期；误点「删除层」删错层（虽然可撤销）。
- **建议**：**修**。实现 `layer_picked`（点 GUTTER 区发信号并在 window 里 set_selected 该层），
  并让 `del_layer` 在"无显式选中层"时拒绝删除。
- **置信度**：高

### [P2] 每次刷新都把时间轴视口重置为「从头看全曲」，用户缩放/平移丢失
- **位置**：`src/ercue/ui/window.py:1140`（`_sync_view`）被 `src/ercue/ui/window.py:1462`（`_refresh_all`）调用
- **现象**：放大到某几秒精细排点后，只要动一下曲目信息（改 BPM / 时长 / 曲名）或加删一个素材，
  时间轴**立刻跳回"显示整首"**，之前的缩放和位置全丢。
- **根因**：`_sync_view` 每次都用 `set_view(0.0, max(8.0,total))` 强制复位视口，
  而 `_refresh_all` 会调用它；`_on_track_edited`（曲目字段 `valueChanged` 直连）等高频路径都会走 `_refresh_all`。
- **证据**：
  ```python
  # window.py:1140
  def _sync_view(self):
      total = self.timeline_total_sec()
      bl_bpm = self._bpm()
      self.timeline.set_bpm(bl_bpm)
      self.timeline.set_snap_mode(self._snap_mode())
      self.timeline.set_view(0.0, max(8.0, total))   # ← 每次刷新都复位到 (0, total)
      ...
  ```
  ```python
  # window.py:814 —— 曲目字段一改就 _refresh_all（进而复位视口）
  def _on_track_edited(self):
      ...
      self._refresh_all()
  ```
- **影响**：精细排点被打断，反复重缩放，体验差。
- **建议**：**修**。`_sync_view` 里仅在**切换曲目 / 载入码表**时复位视口，普通刷新保留 `view0/view_span`。
- **置信度**：高

### [P2] 缩略图只能按 clip_id 缓存、跨码表不清空 → 串图 / 新素材永不生成
- **位置**：`src/ercue/ui/panels.py:389`（`_on_thumb`）、`src/ercue/ui/panels.py:419`（`fill`）
- **现象**：切换码表后，②/③ 面板里同名 clip_id（每个码表的素材 id 都从 `c1` 重新编号）**显示的是上一张码表的缩略图**；
  且因为 `clip_id` 已在缓存里，**新码表的真实缩略图永远不会被生成**。
- **根因**：`_thumbs_ready` 以 `clip_id` 为键、**进程级常驻、从不随码表切换清空**；
  `fill()` 又用 `m.clip_id not in self._thumbs_ready` 决定是否入队、用 `_thumbs_ready.get(clip_id)` 取图。
- **证据**：
  ```python
  # panels.py:419
  def fill(self, sheet, media_paths, cache_dir=None):
      ...
      for m in sheet.media:
          ...
          if not ok:
              it.setIcon(red_dot_icon())
          else:
              icon = self._thumbs_ready.get(m.clip_id)   # ← 只按 clip_id 取：
              if icon is not None:                        #    跨码表同名 id 会串图
                  it.setIcon(icon)
          self.lst.addItem(it)
      ...
      for m in sheet.media:
          path = media_paths.get(m.clip_id) or ""
          if path and os.path.isfile(path) and m.clip_id not in self._thumbs_ready:
              self._worker.request(m.clip_id, path)       # ← 缓存命中则不请求 → 新图永不生成
  ```
- **影响**：素材面板给出**错误**的视觉信息，用户据此选错素材。
- **建议**：**修**。缓存键改成 `(sheet 身份/素材绝对路径, clip_id)` 或切换码表时清空 `_thumbs_ready`。
- **置信度**：高

### [P2] 数值框 `valueChanged` 直连：每按一步 = 一条撤销 + 一次全量刷新（撤销栈被灌爆）
- **位置**：`src/ercue/ui/props_panel.py:89` 等（`valueChanged.connect(self._on_edit)`）、
  `src/ercue/ui/panels.py:263`（`sp_dur.valueChanged`）、`src/ercue/ui/panels.py:271`（`sp_bpm.valueChanged`）、
  `src/ercue/ui/window.py:1066`（`_on_props_changed` → `_push_undo`）
- **现象**：在"起点/时长/入点/BPM"等框里用滚轮或键盘连点几下，撤销栈（上限 50）被瞬间灌满，
  **之前的正常撤销步骤被挤掉、再也回不去**；同时每次 `_refresh_all` 还重置视口（见上一条）。
- **根因**：所有 `QDoubleSpinBox.valueChanged` 直连 `_on_edit` → `changed` → 主窗口 `_push_undo()`。
  一次"输入 12.5"会按 1/12/12.5 触发多次；自动重复的滚轮/长按同样每步一次。
- **证据**：
  ```python
  # window.py:1066
  def _on_props_changed(self):
      p = self.props_panel
      if p.item is None and p.layer is None:
          return
      self._push_undo()          # ← 每次 valueChanged 都压一条快照
      if p.commit():
          ...
  ```
  ```python
  # props_panel.py:88
  self.sp_at = _dspin(0, 100000, 2, 0.1, " 秒")
  self.sp_at.valueChanged.connect(self._on_edit)   # ← 每一步都回传
  ```
- **影响**：撤销历史被无意义步骤淹没，用户"撤不回"想要的状态。
- **建议**：**修**。改为 `editingFinished` / `valueChanged` 加防抖合并，或每次编辑会话只压一条快照。
- **置信度**：中高

### [P2] "吸附"下拉被当成片段属性编辑器：改下拉会静默改写当前片段，且不入撤销栈、不置脏
- **位置**：`src/ercue/ui/window.py:1169`（`_on_snap_changed`）
- **现象**：用户只是想把"吸附"从「小节」切到「拍」（一个画布级设置），
  却**顺带把当前选中的那个片段 `snap` 字段改掉**了——不做提示、不能撤销、标题也不显示"未保存"。
- **根因**：`_on_snap_changed` 同时承担两件事：设画布吸附档 + 写回选中 item 的 `snap`。
- **证据**：
  ```python
  # window.py:1169
  def _on_snap_changed(self, _idx=0):
      self.timeline.set_snap_mode(self._snap_mode())
      ly, it = self.timeline.selected_item()
      if it is not None:
          it.snap = self._snap_mode()          # ← 静默改模型，无 _push_undo / 无 _mark_dirty
  ```
- **影响**：导入/导出的"未吸附"检查结果被无意间改动；用户难以理解为何一个下拉改了片段。
- **建议**：**修**。吸附档只控制编辑网格；片段 `snap` 由属性面板显式编辑，并 `_push_undo()`。
- **置信度**：中高

### [P2] 缩略图线程超时未等停 → 退出时可能出现 "QThread destroyed while still running"
- **位置**：`src/ercue/ui/panels.py:460`（`stop_worker`）、`src/ercue/ui/panels.py:103`（`ThumbWorker.run`）
- **现象**：素材很多 / 有耗时 alpha 素材时，关窗瞬间若后台仍在 `make_thumb`，
  `wait(1500)` 超时后 `_worker` 被置 `None`，QThread 对象随后被析构，可能打印告警甚至让进程异常退出。
- **根因**：`stop()` 只置 `_stop` 并清队列，无法中断**正在执行**的 `make_thumb`（单素材可 20~120ms 起，
  alpha 素材更深）；`stop_worker` 在 `wait()` 返回后无条件 `self._worker = None`，未校验线程是否真的结束。
- **证据**：
  ```python
  # panels.py:460
  def stop_worker(self):
      if self._worker is not None:
          self._worker.stop()
          self._worker.wait(1500)      # ← 超时返回后不检查结果
          self._worker = None          # ← 线程可能仍在跑，随后对象被销毁
  ```
- **影响**：退出偶发报错 / 崩溃，用户感知为"关的时候出问题"。
- **建议**：**修**。给 worker 一个"当前任务可打断点"或用 `QThread` 的 `requestInterruption()`；
  `wait()` 失败时不要立即置 `None`（保留引用直到线程结束）。置信度中（依赖时序）。
- **置信度**：中

### [P3] 图层无法重排（改 z）、时间轴不支持多选
- **位置**：`src/ercue/ui/window.py:892`（`add_layer`）、`src/ercue/ui/timeline_widget.py:106`（单一 `self.sel`）
- **现象**：层只能新增/删除，**没有任何改 z 顺序的入口**（画布层叠顺序 = 创建顺序，不可调）；
  时间轴选中是单点 `sel = ("", "")`，无框选/多选，删除/移动只能一个个来。
- **根因**：功能未实现（任务书把"图层重排/多选"列为交互检查项，当前 UI 只有单层增删与单选中）。
- **证据**：
  ```python
  # window.py:898
  z = max([int(l.z) for l in tr.layers], default=-10) + 10   # 只增不减、无上移/下移入口
  ```
- **影响**：复杂编排（层叠加顺序）无法表达；批量编辑效率低。
- **建议**：**暂不修**（可作后续里程碑）。MVP 可接受，但需在界面说明层顺序 = 创建顺序。
- **置信度**：高（属"功能缺口"而非 bug）

### [P3] 预览"激活"只在 shutdown 关闭：播放过一次后 10fps 定时器与解码器常驻
- **位置**：`src/ercue/ui/preview.py:72`（`set_active`）、`src/ercue/ui/window.py:1349`（`_start_play` 只 set_active(True)）
- **现象**：播放一次之后，即便停止播放，预览的 10fps 定时器一直在跑、`CueFrameSource` 持有的解码器不释放
  （`set_active(False)` 仅在 `shutdown()` 调用）。
- **根因**：`_stop_play` 只 `preview.set_playing(False)`，从不 `set_active(False)`。
- **证据**：
  ```python
  # window.py:1359
  def _stop_play(self):
      ...
      self.preview.set_playing(False)      # ← 没有 set_active(False)
      ...
  ```
  ```python
  # preview.py:72
  def set_active(self, on):
      ...
      else:
          self._t.stop()
          try:
              self.src.close()             # 关解码器——但停播时不会被调用
  ```
- **影响**：轻微常驻 CPU / 文件句柄占用；多素材下句柄累积。静止时因 `_tick` 早退 CPU≈0，故不严重。
- **建议**：**暂不修**（或停播时 `set_active(False)`），影响有限。
- **置信度**：中

### [P3] 时间轴 `moved` / `resized` 处理忽略信号参数，改用"当前选中项"
- **位置**：`src/ercue/ui/window.py:1000`（`_on_timeline_moved`）、`src/ercue/ui/window.py:1013`（`_on_timeline_resized`）
- **现象**：槽函数签名收了 `(lyr_id, item_id, ...)` 却不用，改用 `self.timeline.selected_item()`；
  正常流程选中项与参数一致，故一般无碍，但**契约上留了隐患**（若信号与选中态不同步就会改错片段）。
- **根因**：与 `_on_clip_dropped` 直接用参数不同，这里绕了一层。
- **证据**：
  ```python
  # window.py:1000
  def _on_timeline_moved(self, lyr_id, item_id, at_sec):
      ly, it = self.timeline.selected_item()   # ← 忽略传入的 lyr_id/item_id
      if it is None:
          return
  ```
- **影响**：当前无可见故障；属健壮性隐患。
- **建议**：**暂不修**（或改用传入参数精确定位 `(lyr_id, item_id)` 对应的 item）。
- **置信度**：中

---

## 检查过但判定"没问题"的点

1. **单向数据流纪律（B1/B2）**：面板→信号→主窗口落模型，`blockSignals` + `findData`/选中还原在
   `_refresh_sheet_combo` / `PkgPanel.refresh` / `TrackPanel.refresh_tracks` / `PropsPanel.set_media_list`
   都落实到位；`_loading` / `_tloading` 守卫置位在动控件之前。**无自激**。
2. **拖放 mime 两级兜底（B4/B5/B6）**：`ClipList.startDrag` 自造 QDrag，`_clip_from_event` 只从参数取
   （不再误用 `self`），`UserRole` 存 clip_id、路径另存 `UserRole+1`。**一致**。
3. **拖拽平移的时长保持**：`_shift_item` 对 `end_sec=None` 保持 None、对坏数据钳成 0 时长，
   未产生负时长。**正确**（旧实现的"压成 0"缺陷已修）。
4. **导出前检查接线**：`pre_export_report` → `precheck.check_sheet`，`ask_export` 缺素材时默认焦点在「取消」，
   `_embed_size_guard` 超限拒内嵌并提示。逻辑分层干净。
5. **音频位置语义与防抖**：`_set_pos` 唯一入口、`from_audio` 防止 seek 自激、滑杆 100ms 防抖、
   `_stop_play` 保留位置、`shutdown` 停定时器/音频/线程/worker。**线程收尾基本到位**。
6. **坐标换算（像素↔秒）**：`_x` / `_sec` 互为逆运算，`units.snap_grid` 为吸附唯一来源，
   文件内不见 `beat_len` 裸乘除。**正确**。
7. **异常吞没**：扫描 ui 层所有 `except`，均至少 `log.exception`（除 `player.py:122` 的 resampler flush
   与 `thumbs.py` 内部 return None 属模型层），**未发现吞掉用户可见失败**的裸 `except: pass`。
   `import_sheet` / `export_sheet` 失败都会 `self._log("…失败…")` 让用户看到。
