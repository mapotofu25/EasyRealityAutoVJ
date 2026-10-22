# media/audio/render 审计报告

> 范围：`src/ercue/media/{thumbs,frames,probe}.py`、`src/ercue/audio/player.py`、`src/ercue/render/compositor.py`
> 参照物：`<项目根>\AutoVJ_码表备份_20260930\src\{cuesheet.py, cuesheet_runtime.py, engine.py}`（演出端旧实现）
> 方式：**只读**代码审计 + 与演出端逐条对照。未修改任何源码。

## 小结

- 审计文件：**5** 个 / **1,609** 行；发现 **P0 0 / P1 5 / P2 6 / P3 5 = 16** 项。
- **最值得关注**：`compositor.py` 的适配基准（contain）与演出端旧实现（默认 cover 铺满裁切）**不是同一套几何**——这是"码表在编辑器里排好、放到现场却对不上画面"的结构性根因，且它连 crop 一起错（预览把裁切区域拉伸放大）。

---

## 发现清单

### [P1] 预览用 contain 适配，演出端默认用 cover（铺满+裁切），两者几何不一致

- **位置**：`src/ercue/render/compositor.py:166` 对照 `<项目根>\AutoVJ_码表备份_20260930\src\engine.py:2800-2802`
- **现象**：非 16:9 素材（或任何分辨率不匹配画布的素材）在**编辑器预览里被完整显示并留黑边**，而**现场演出端会把画面铺满整屏、裁掉溢出部分**。同一个 `xform={"scale":1,"x":0,"y":0}` 的片段，两处看到的构图不同；用户会认为"预览是对的、现场是错的"（或反之）。
- **根因**：`compositor._target_src` **无条件**用 contain（`k = min(W/fw, H/fh)`）；而演出端 `paint_cover` 只在用户改过 scale/x/y（`custom`）时才用 contain，默认走 cover（`s = max(W/iw, H/ih)`）。
- **证据**：
  ```python
  # compositor.py:164-167
  xf = st.xform or {}
  us = float(xf.get("scale", 1.0) or 1.0)
  k = min(w / float(fw), h / float(fh)) * us          # D1 contain + 用户缩放
  tw, th = fw * k, fh * k
  ```
  ```python
  # engine.py:2799-2802（演出端）
  if custom:
      s = min(W / iw, H / ih) * scale * us   # 完整显示 + 用户缩放
  else:
      s = max(W / iw, H / ih) * scale        # 铺满   ← 默认走这里
  ```
  注：`compositor.py:10` 的注释把旧实现描述成"直接铺满 ⇒ 拉伸变形"，但旧实现是 `max(...)`（**等比 cover**，不是 `W/iw × H/ih` 的双向拉伸），注释对旧实现的判断本身有误。
- **影响**：所有非满幅比例素材的构图/裁切现场与预览不一致；"码表对不上画面"。
- **建议**：预览必须复刻演出端分支——用户未改 transform 时用 `max`（cover），改过才用 `min`（contain）；或在数据模型里显式加 `fit` 字段并两端统一。
- **置信度**：高

### [P1] crop 在预览里被"拉伸放大"（用整幅尺寸算适配，再画源矩形）

- **位置**：`src/ercue/render/compositor.py:180-186` 与 `:318` 对照 `src/engine.py:2796`
- **现象**：给一个片段设了裁切后，预览里裁切区域会被拉伸到"整幅图应有的显示框"，**宽高比被破坏**；演出端则是"把裁切区域当作新的源图"再等比适配，不变形。
- **根因**：合成器用**整幅**图尺寸 `img.width()/height()` 计算 `tgt`，再用 `src` 子矩形把裁切区画进这个整幅尺寸的框里 ⇒ 裁切区被放大到整幅框且被该框的宽高比拉伸。演出端先把 `iw, ih = cw, ch`（换成裁切后尺寸）再算适配，故不变形。
- **证据**：
  ```python
  # compositor.py:180-186
  src = QRectF(0.0, 0.0, float(fw), float(fh))
  crop = getattr(st, "crop", None)
  if crop and len(crop) == 4:
      x0, y0, cw, ch = (...)
      src = QRectF(x0 * fw, y0 * fh, max(1.0, cw * fw), max(1.0, ch * fh))
  ```
  ```python
  # compositor.py:318（tgt 用的是 img 的全尺寸，不是裁切后尺寸）
  tgt, src = _target_src(st, img.width(), img.height(), w, h)
  ```
  ```python
  # engine.py:2795-2796（演出端：裁切后以裁切尺寸为基准）
  src = QRectF(cx, cy, cw, ch)
  iw, ih = cw, ch       # cover 的计算基准换成裁切后的尺寸
  ```
- **影响**：任何带 crop 的片段，预览里形状/比例是错的；用户按预览调裁切，现场效果不同。
- **建议**：与 D1 一同修正——裁切后应以 `cw/ch` 为新基准计算 `tgt`。
- **置信度**：高

### [P1] `probe.probe()` 的两层 alpha 判定从未被导入流程调用 ⇒ `MediaRef.has_alpha/duration_sec` 恒为默认值

- **位置**：`src/ercue/media/probe.py:159`（定义）／调用方 `src/ercue/ui/window.py:696`（**没有调用**）／被依赖方 `src/ercue/model/precheck.py:196-197`
- **现象**：加了素材后导出的 `.ercue` 里，每个 `MediaRef` 的 `duration_sec` 恒为 `0.0`、`has_alpha` 恒为 `false`——不管素材是不是带 alpha 的 DXV。
- **根因**：全项目**唯一的素材导入入口** `window.add_media_paths()` 只用 `MediaRef(clip_id=cid, name=name)` 记录路径，**从不调用 `probe()`**；`probe()` 仅被 `tests/`、`tools/smoke_hook.py` 使用。精心实现的两层 alpha 判定（`probe.py:92-141`）实际是**死代码**。
- **证据**：
  ```python
  # window.py:693-697（导入素材，未探测）
  ref = next((m for m in s.sheet.media if m.name == name), None)
  if ref is None:
      cid = "c%d" % _next_num("c", [m.clip_id for m in s.sheet.media])
      ref = MediaRef(clip_id=cid, name=name)      # ← duration_sec/has_alpha 用默认值
      s.sheet.media.append(ref)
  ```
  ```python
  # precheck.py:196-199（依赖 duration_sec，恒 0 ⇒ 校验直接失效）
  mdur = units.to_float(getattr(m, "duration_sec", 0.0)) if m else 0.0
  if end is not None and mdur > 0.0:      # mdur 恒 0 → 永远进不来
      ...
  ```
- **影响**：① 演出端/校验端拿不到真实 `has_alpha`（带 alpha 的 DXV 会被当不透明黑底处理）；② `precheck` 的"片段超出素材时长"校验**形同虚设**；③ 导出清单的时长信息为空，排查困难。
- **建议**：在导入/导入完成时（后台线程）对每个新素材调用 `probe()` 并回填 `MediaRef`；或至少在导出前批量补齐。
- **置信度**：高

### [P1] 音频"打开设备失败/播放异常"后 `_open_failed` 永不复位 ⇒ 本次会话永久无声

- **位置**：`src/ercue/audio/player.py:226`、`:340-347`、`:405`、`:428`
- **现象**：只要有一次打开输出设备失败（设备被独占、采样率不支持上一次瞬时错误），此后**整个会话所有 `play()` 都返回 False**，即使设备随后空出来也无法恢复——用户感觉"这软件放不了声音"。
- **根因**：`_open_failed` 一旦置 `True` 就**再无任何地方复位**；`available` 永久受其钳制，`play()` 直接早退。
- **证据**：
  ```python
  # player.py:224-226
  @property
  def available(self) -> bool:
      return self._device_ok and not self._open_failed
  ```
  ```python
  # player.py:403-408（打开失败置位）
  except Exception as e:
      log.exception("打开音频输出设备失败")
      self._open_failed = True
      self.error = "打开音频输出设备失败：%s: %s" % (type(e).__name__, e)
      ev.set()
      return
  ```
  ```python
  # player.py:345-347（此后永久早退）
  if not self.available:
      self.error = self.error or "本机没有可用的音频输出设备"
      return False
  ```
  全文件搜索 `_open_failed` 只有这两处 `= True`，**无 `= False`**。
- **影响**：瞬态错误被升级为"会话级永久失败"，现场直接没声音（且日志只说设备不可用，误导排查）。
- **建议**：在 `load()`/`play()` 时允许复位（例如新会话开始时清 `_open_failed`），并把 `_open_failed` 与 `_device_ok` 的语义分开；同时把 `_run` 里非"打开"类异常与"打开失败"区分，不要都置 `_open_failed`。
- **置信度**：高

### [P1] 淡入曲线不一致：预览线性 α，演出端 fade 把 `trans_p` 再乘一次（α²）

- **位置**：`src/ercue/render/compositor.py:319`（只乘一次）对照 `src/engine.py:2832-2835`（乘两次）
- **现象**：淡入过程里，预览的透明度是线性上升（`α`），演出端是平方曲线（`α²`）；同一段淡入，两处"看起来的节奏"不同。
- **根因**：演出端运行时 `ps.alpha` 已含淡入量，`_draw_layer` 的 fade 分支又乘了一次 `prog`（`= ps.trans_p = a_in`）⇒ `α·prog = a_in²`。编辑器合成器刻意只乘一次（见 `compositor.py:11-12` 的 D2）。
- **证据**：
  ```python
  # compositor.py:290,319（只乘一次）
  op = _clamp01(getattr(st, "opacity", 1.0)) * _clamp01(getattr(st, "alpha", 1.0))
  ...
  p.setOpacity(op)                       # D2：alpha 只乘一次
  ```
  ```python
  # engine.py:2550  + 2832-2835（演出端：alpha 与 prog 相乘）
  op = base_op * float(ps.opacity) * float(ps.alpha)
  ...
  if tr == "fade":
      if prev is not None:
          paint_cover(p, prev, prev_item, pulse, dx, dy, alpha)
      paint_cover(p, img, cur_item, pulse, dx, dy, alpha * prog, crop)   # ← alpha*prog
  ```
- **影响**：淡入的视觉进度曲线两端不同；虽然编辑器曲线"更正确"，但现场就是放出来的样子，预览因此不能如实反映现场。
- **建议**：明确以哪端为准并统一（推荐修演出端把 fade 的 `prog` 去掉或让编辑器复刻），并写进对齐清单，别让两端各自为政。
- **置信度**：高（代码层），视觉量级中

---

### [P2] `stop()` join 超时后 `is_playing()` 已为 False，但线程可能仍活着 ⇒ 快速 play/stop 可能双开声卡

- **位置**：`src/ercue/audio/player.py:368-381`、`:255-258`、`:349-361`
- **现象**：若 `sink.write()` 阻塞导致 `join(0.75)` 超时，`stop()` 已把 `self._th=None`，`is_playing()` 返回 False；此时再 `play()` 会开**第二个** `OutputStream`，而旧线程仍持有旧 stream —— 设备被重复占用/音频错乱，或直接打开失败（触发上一条 P1）。
- **根因**：`stop()` 在 join **之前**就把 `_th=None`（放弃了对仍在运行线程的追踪）；`play()` 不检查是否还有"僵尸"播放线程。
- **证据**：
  ```python
  # player.py:370-377
  with self._lock:
      ev, th = self._stop, self._th
      self._th = None                 # ← join 之前就清掉
  if ev is not None:
      ev.set()
  if th is not None and th.is_alive():
      th.join(timeout=STOP_TIMEOUT_SEC)
  ```
  ```python
  # player.py:255-258
  def is_playing(self) -> bool:
      with self._lock:
          th, ev = self._th, self._stop
      return th is not None and th.is_alive() and (ev is None or not ev.is_set())
  ```
- **影响**：拖动/连点播放键时可能出现"两个声卡流"、无声或爆音。
- **建议**：保留对播放线程的引用直到确认退出（或用一个"正在退出"标志拦住新的 `play()` 直到旧线程 join 成功）。
- **置信度**：中

### [P2] 预览只走关键帧快速路径 `frame()`，`frame_exact()` 全项目无人调用

- **位置**：`src/ercue/media/frames.py:174`（精确定义）／预览调用 `src/ercue/ui/preview.py:116`／`frames.py:200-205`（快速路径）
- **现象**：预览取帧是"seek 到不晚于 sec 的**最近关键帧**再取该帧"，落点可差**最多一个 GOP**；对长 GOP 的 H.264 `.mov/.mp4`（素材库常见）拖动播放头时，预览画面会明显滞后/不精确。精确取帧的实现存在但**从未被使用**（只在 docstring 里被提到）。
- **根因**：`preview._frame_lookup` 固定调用 `self.src.frame(path, play_sec)`（快速路径），而精确路径 `frame_exact()` 没有任何调用方。
- **证据**：
  ```python
  # preview.py:113-116
  path = self._path_of(clip_id)
  if not path:
      return None
  return self.src.frame(path, play_sec)
  ```
  ```python
  # frames.py:200-205（快速路径：取关键帧）
  if not exact:
      fr = next(iter(src.container.decode(video=0)), None)
      return _frame_to_bgra(fr) if fr is not None else None
  ```
  全项目搜索 `frame_exact`：仅 `frames.py` 自身定义与 `props_panel.py:6` 的注释，**无调用点**。
- **影响**：拖动/定位时"预览落点不精确"，入/出点核对不准；DXV 为帧内编码故无此问题，但 H.264 素材受影响。
- **建议**：拖动结束/入出点定位用 `frame_exact`，滚动预览才用 `frame`（按精度需要分流）。
- **置信度**：高

### [P2] "位置 = 已交声卡帧号" 恒定领先设备输出延迟 ⇒ 画面比听到的声音早

- **位置**：`src/ercue/audio/player.py:260-267`、`:422-425`
- **现象**：`position_sec()` 在 `sink.write()` 返回后前进，而 `write()` 返回只表示数据进了设备缓冲、**并未播放**。因此位置领先"耳朵听到的"约等于设备输出延迟（典型 30~200ms，blocksize=0 时由 PortAudio 决定）。预览画面与滑杆都基于这个位置 ⇒ **画面系统性地早于声音**。
- **根因**：设计以"帧号"为唯一真源（A5b），但未扣除设备输出延迟；`OutputStream` 的 `latency`/`stream.time` 未被使用。
- **证据**：
  ```python
  # player.py:422-425
  data = np.ascontiguousarray(chunk.T)
  sink.write(data)                       # ★ 阻塞式；返回后才前进
  with self._lock:
      self._i = min(i + chunk.shape[1], total)   # A5b：只有真交出去才算
  ```
- **影响**：经久不消的恒定偏移——用户会直接把画面对齐到"看起来对"的提前量，导出到演出端（其自身也有延迟、但可能不同）后偏移量又变，形成"对不上"。
- **建议**：用 `sink.latency`（或 PortAudio 的 `stream.time`）作为常量补偿，把 `position_sec()` 减去输出延迟；或明确记录并抵消该延迟。
- **置信度**：中

### [P2] `precheck` 的"超出素材时长"校验因 `duration_sec == 0` 而失效

- **位置**：`src/ercue/model/precheck.py:196-197`（依赖项来自上面的 probe 未接入）
- **现象**：`beyond_media`（片段比素材本身还长）这条预检**永远不会报警**。
- **根因**：判据 `if end is not None and mdur > 0.0`，而 `mdur` 恒为 0（`MediaRef` 时长从未被探测填充）。
- **证据**：
  ```python
  # precheck.py:194-199
  m = sheet.media_by_id(it.clip_id) if hasattr(sheet, "media_by_id") else None
  mdur = units.to_float(getattr(m, "duration_sec", 0.0)) if m else 0.0
  if end is not None and mdur > 0.0:      # 恒 0 → 分支不可达
      avail = mdur - units.to_float(it.src_in_sec)
      if avail > 0.0 and (end - at) > avail + OVERLONG_TOL_SEC:
  ```
- **影响**：用户排了"素材只有 5s 却让片段放 20s"这类错误，编辑器不给任何提示，现场素材播完卡黑。
- **建议**：与 probe 接入一并修复（回填 `duration_sec`）。
- **置信度**：高

### [P2] glitch 过渡的预览实现与演出端不一致（确定性 vs 随机 / 偏移幅度 / band 宽度）

- **位置**：`src/ercue/render/compositor.py:189-217` 对照 `src/engine.py:2845-2860`
- **现象**：glitch 过渡在两端的"错位图案"完全不同，预览不能作为现场效果的参照。
- **根因**：① 预览用 `sin` 哈希的**确定性**函数（`_glitch_offset`），演出端用 `random.Random(int(now*1000))` 的**随机**；② 预览最大偏移 `_GLITCH_MAX_OFF = 30` 且随 `(1-tp)` 衰减，演出端固定 `randint(-40,40)`；③ 预览 band 宽度用 `tgt.width()`（contain 下 < 画布宽），演出端用整幅 `W`。
- **证据**：
  ```python
  # compositor.py:189-194
  def _glitch_offset(tp: float, n: int) -> float:
      v = (math.sin(tp * 97.0 + n * 12.9898) * 43758.5453) % 1.0
      if v >= 0.35:
          return 0.0
      return (v * 2.0 - 1.0) * _GLITCH_MAX_OFF * (1.0 - tp)
  ```
  ```python
  # engine.py:2846-2859
  rng = _r.Random(int(now * 1000))
  ...
  off = rng.randint(-40, 40) if rng.random() < 0.35 else 0
  p.drawImage(QRectF(off, y, W, step), img, QRectF(0, sy, iw, sh))   # ← 整幅 W
  ```
- **影响**：用户选了 glitch 却无法从预览预判现场观感（可接受，但需知）。
- **建议**：统一随机源与参数，或至少文档标注"glitch 预览为近似"。
- **置信度**：高

### [P2] 预览不做"旧片段"过渡：同层切换时旧素材瞬间消失，无交叉淡化

- **位置**：`src/ercue/render/compositor.py:268-346`（只有 `states`，无 prev 概念）对照 `src/engine.py:2760-2860`（`prev_player` + fade/slide/zoom 都同时画 prev）
- **现象**：同一层从前一片段切到后一片段时，演出端会"旧片段淡出/滑出、同时新片段淡入/滑入"；预览里旧片段在切换瞬间直接消失，新片段从底下/黑底上淡入。
- **根因**：`LayerDrawState` 每层每时刻只描述一个 `clip_id`，合成器没有"过渡中的上一个片段"输入 ⇒ 天然无法做交叉过渡。
- **证据**：
  ```python
  # compositor.py:296（每层只取一个 frame）
  frame = frame_lookup(clip_id, play_sec) if two_args else frame_lookup(clip_id)
  ```
  ```python
  # engine.py:2765 + 2833-2834（演出端会同时画 prev）
  prev_item = lay.clips[lay.prev_idx] if (lay.trans_active and 0 <= lay.prev_idx < len(lay.clips)) else None
  ...
  if prev is not None:
      paint_cover(p, prev, prev_item, pulse, dx, dy, alpha)
  ```
- **影响**：切换瞬间的观感与现场不同；用户按预览微调过渡时长时会"看不出交叉"，调出来的时长判断失真。
- **建议**：给 `LayerDrawState` 或 `compose` 增加"上一片段"状态，复刻 prev 的淡出/位移。
- **置信度**：中

---

### [P3] 缩略图缓存目录无清理、无上限

- **位置**：`src/ercue/media/thumbs.py:46-74`（只有 `cache_key/thumb_path/resolve_thumb`，无清理函数）／写入方 `src/ercue/ui/panels.py:118-125`
- **现象**：缓存按 `md5(path+mtime)` 命名，素材一旦被替换就会产生**新的**缓存文件，旧文件永不被删除；长期使用缓存目录持续增长。
- **证据**：
  ```python
  # thumbs.py:51-56（mtime 进键 ⇒ 每次改素材都生成新文件，旧的不删）
  mtime = int(os.path.getmtime(path))
  ...
  h = hashlib.md5(("%s|%d" % (str(path), mtime)).encode("utf-8")).hexdigest()
  return h[:16]
  ```
  全文件无任何删除/裁剪逻辑。
- **建议**：加一个按目录扫描的清理函数（超过阈值时淘汰最旧/孤儿文件），在启动或关闭时调用。
- **置信度**：高

### [P3] `CueFrameSource._src` 与 `compositor` 的 scratch 缓存都无上限/无淘汰

- **位置**：`src/ercue/media/frames.py:88`、`:132-135`／`src/ercue/render/compositor.py:52-54`、`:98-119`
- **现象**：`_src`（路径→已开容器）只增不减，`release(path)` 除 `close()` 外无调用点；合成器的 `_CANVAS/_SUB_IMG/_SUB_MIN/_OP_LUT` 按 `(w,h)`/op 累积，GUI 缩放导致的中间尺寸也留在字典里。
- **证据**：
  ```python
  # frames.py:132-133
  src = _Source(path, container, stream, kind_of(path) == "image")
  self._src[path] = src            # 无容量上限/无 LRU 淘汰
  ```
  ```python
  # compositor.py:52-54
  _CANVAS = {}      # (w,h) -> QImage(ARGB32)，合成画布
  _SUB_IMG = {}
  _SUB_MIN = {}
  ```
- **影响**：素材多的会话里文件句柄/解码器内存与 scratch 内存缓慢增长。
- **建议**：给 `_src` 加上限并在超限时 `release()` 最久未用者；scratch 缓存按当前尺寸只保留 1 份。
- **置信度**：中

### [P3] 解码失败（非"打不开"）不进退避，每次调用都重试解码

- **位置**：`src/ercue/media/frames.py:110-115`（退避只在 `not os.path.isfile` / `av.open` 失败时记）与 `:193-217`（解码失败返回 None，不动退避）
- **现象**：能打开但**解不出帧**的素材（坏 DXV、无帧容器），预览每帧（10fps）都会重新 seek+decode，CPU 白烧且日志/异常重复。docstring 的 A2 只覆盖"打不开"。
- **证据**：
  ```python
  # frames.py:216-217（解码异常直接 None，不记退避）
  except Exception:
      return None
  ```
  ```python
  # frames.py:128-130（只有 av.open 失败才退避）
  except Exception:
      self._fail[path] = now + RETRY_SEC       # A2：打开失败 → 退避 2 秒
      return None
  ```
- **影响**：个别坏素材会让预览线程持续空转（旧实现"取帧生成器空转"的变体）。
- **建议**：连续 N 次解码失败后对路径做短退避。
- **置信度**：中

### [P3] 整曲解码进内存，长素材内存占用高

- **位置**：`src/ercue/audio/player.py:87-126`（`AudioTrack._decode` 全部 `concatenate` 到内存）
- **现象**：48kHz 立体声 float32 ≈ 384 KB/s；一首 7 分钟曲约 160MB，若导入的是长 DJ set（如 60~120 分钟）则约 1.4~2.8GB。
- **证据**：
  ```python
  # player.py:126
  self._buf = np.ascontiguousarray(np.concatenate(chunks, axis=1))
  ```
- **影响**：长素材可能内存吃紧/换页，进而影响整个软件响应。
- **建议**：对超长素材改为分块保持（当前"整曲进内存"的初衷是避免队列抖动，可折中为"大内存缓存 + 必要时分块"）。
- **置信度**：中

### [P3] DXV 的 YUV 变体拿不到 alpha ⇒ 判为无 alpha（已知坑，非本实现缺陷，但需明确标注）

- **位置**：`src/ercue/media/probe.py:130-141`、`:92-115`
- **现象**：DXV 的 YUV 变体经软解后 alpha 通道全 255（alpha 已丢失），两层判定会得出 `has_alpha=False`，带 alpha 的 YUV-DXV 会被当不透明处理。
- **证据**：
  ```python
  # probe.py:104-107（只在实解帧里真的出现 alpha<255 才判 True）
  arr = fr.to_ndarray(format="rgba")
  if (arr.ndim == 3 and arr.shape[2] >= 4
          and int(arr[:, :, 3].min()) < 255):
      return True
  ```
- **影响**：YUV-DXV 素材的透明区域无法体现（黑底/不透明）。
- **建议**：属上游编解码限制，至少对 `.mov`+DXV 在 UI 上给"该编码可能不含 alpha"的提示，避免用户误判为软件 bug。
- **置信度**：高（代码行为明确）；"用户素材多为这类"：中

---

## ★ 与演出端的能力对齐清单

（演出端 = `AutoVJ_码表备份_20260930/src/engine.py` 的 `_draw_layer/_draw_cuesheet_layer/_compose` + `cuesheet.py` 的能力表）

| 本软件（`compositor.py`）支持 | 演出端（旧实现）支持？ | 影响 |
|---|---|---|
| blend `normal/add/screen/multiply/lighten/darken`（`BLEND_QT`） | **是**，同一张 `BLEND_QT` 表（`engine.py:80-85`） | ✅ 一致 |
| blend `subtract`（numpy 饱和减 `max(0,dst-src)`） | **是**，`engine._apply_subtract`（cv2 饱和减，公式相同） | ✅ 一致（本软件已按勘误修正公式，好） |
| 过渡 `fade` | **是**，但演出端把 `trans_p` 再乘一次（`α×prog`） | ⚠ **曲线不同**（预览线性，现场平方）——见 [P1] |
| 过渡 `cut` | **是** | ✅ 一致（等价于 alpha 直接到位） |
| 过渡 `slide`（右侧滑入） | **是**，方向一致（`off=W*(1-prog)`） | 🔸 方向一致；演出端还会把旧片段左移出，预览不做 |
| 过渡 `zoom`（0.7→1.0） | **是**，cur 缩放 0.7→1.0 | 🔸 缩放**基准不同**（预览 contain / 现场 cover）+ 缺 prev 放大淡出 |
| 过渡 `glitch` | 引擎 `_draw_layer` **有实现**（`engine.py:2845`），但 `cuesheet.py` 把 `trans_glitch` 归入 `KNOWN_FEATURES`（"未支持"） | ⚠ 本软件会把它写进 `requires_features` → 演出端 `capability_report` 提示"**将被忽略**"，但引擎其实能渲染 ⇒ **提示误导**；同时预览实现与引擎不一致（见 [P2]）|
| 过渡 `zoom` 的能力标记 | 同上，`trans_zoom` 同为 `KNOWN_FEATURES`（`features.py:25-27`，`:103-104` 有断言） | ⚠ 同上 |
| 默认适配 **contain**（`k=min(...)`） | 现场**默认 cover**（`max(...)`），仅改过 xform 才 contain | ❌ **不一致**——见 [P1]，头号对齐问题 |
| crop（源矩形，基准=整幅尺寸） | crop 后以**裁切后尺寸**为基准再适配 | ❌ **不一致**——见 [P1] |
| xform `scale/x/y` | **是**（`img_scale/img_x/img_y`，`engine.py:2767-2776`） | ✅ 基本一致 |
| `speed`（`play_sec = src_in + elapsed×speed`） | 演出端 speed=倍速 | ✅ 一致（`timeline.py:250-265` 已按 ×speed） |
| 黑底画布（`fill(black)`） | `canvas.fill(Qt.black)` | ✅ 一致 |

**能力表结论**：`trans_glitch` / `trans_zoom` 属于**演出端"能渲染但声明未支持"**的矛盾项；导出时编辑器把它们写进 `requires_features`，会让演出端错误地提示"将被忽略"。建议要么把这两项从 `KNOWN_FEATURES` 提升为 `SUPPORTED_FEATURES`（两端同步），要么让编辑器在导出前对使用 glitch/zoom 的片段给出明确警告。

---

## 检查过但判定"没问题"的点

- **`metadata_errors="replace"`**：`probe.py:178`、`thumbs.py:238`、`frames.py:126`、`player.py:99` 四处 `av.open` **全都带上**，GBK 元数据 `.mov` 不会因 `UnicodeDecodeError` 被误判为"打不开"。✅
- **blend 集合与公式**：6 种 Qt 原生 + `subtract` numpy 饱和减，公式 `max(0,dst-src)` 与 `engine.py:2701 _apply_subtract` docstring 一致，且注释里拒绝回退到错误近似式。✅
- **`subtract` 逐层独立 scratch**：`compositor.py:328-344` 每个 subtract 层"渲染到同一 scratch 后立即施加"，避免了"多层攒起来只剩最后一层生效"的坑。✅
- **取帧复用/退避**：`frames.py` 对同一路径**只 open 一次**（`_get_source` 命中即复用），坏路径空跑 50 次也不开文件（`selftest` 断言 `open_count_of==0`）；A1/A2 落实到位。✅
- **seek 纪律**：统一 `seek(pts, stream=st, backward=True)`，**无 `any_frame=True`**；时间戳统一 `time.monotonic()`。✅
- **`close()`/`release()`**：合成器预览在 `set_active(False)` 时 `src.close()`；`close()` 后再取帧返回 None。✅
- **`media/`、`audio/` 零 Qt**：`media/*.py`、`audio/player.py` 均只依赖 stdlib + numpy + PyAV，未 import PySide6，符合设计铁律。✅
- **`frame_lookup(clip_id, play_sec)` 双参契约 + 单参兼容**：`_wants_two_args` 用 `inspect` 兼容旧单参回调，且预览对同 `(clip_id, play_sec)` 做 memo 去重。✅
- **异常吞没判定**：`_frame_to_bgra`/`make_thumb`/`decode` 的 `except: return None` 属于"单个坏素材不中断整体"的**有意设计**（符合编排工具容错），但正因如此，**素材永远显示不出来时用户拿不到任何可读原因**——建议至少把失败路径 count 出来供 UI 提示（非缺陷，是体验缺口）。🔸
- **`probe` 时长回退**：流时长 → 容器时长（`/1e6`，AV_TIME_BASE）两级回退，处理正确。✅
