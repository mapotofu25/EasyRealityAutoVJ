# -*- coding: utf-8 -*-
"""S3 一次性补丁：`Layer` 字段 + `_CueSubLayer` + `set/clear_cue_sheet` + `_get_player` 早返回。

用脚本而不是手改：这次要动 6 处、且要保证"只碰数据面"。跑完即删。
"""
import io
import os

NL = chr(10)
P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "src", "engine.py")
text = open(P, encoding="utf-8").read()
orig = text
edits = []


def sub(old, new, tag):
    global text
    assert text.count(old) == 1, "锚点命中 %d 次：%s" % (text.count(old), tag)
    text = text.replace(old, new, 1)
    edits.append(tag)


# ---------------------------------------------------------------- ① Layer 字段
sub(
    "        self.kv_switch_t = 0.0    # 素材轮换计时（0=未开始；见 cfg[\"kv\"][\"switch_sec\"]）"
    + NL + "        self.player = None",
    "        self.kv_switch_t = 0.0    # 素材轮换计时（0=未开始；见 cfg[\"kv\"][\"switch_sec\"]）"
    + NL
    + "        # ---- 码表（cue）图层：容器层标记与运行时状态（S3 数据面，渲染在 S4/S5）----"
    + NL + "        #   `is_cuesheet=True` 的图层是**容器**：自身没有单一 player，画面由内部子层按码表渲染。"
    + NL + "        self.is_cuesheet = False"
    + NL + "        self.cuesheet_id = \"\"      # 绑定的码表标识（Step5 由 UI 填；这里只存不解释）"
    + NL + "        self.cs_states = []        # [LayerPlayState]，由 S4 的 _cuesheet_tick 每帧填"
    + NL + "        self.cs_active = False     # 本帧是否接管（False ⇒ 退回普通图层行为）"
    + NL + "        self._cue_players = {}     # 码表**专用**解码器缓存（与 self.cache 隔离，见 _cue_get_player）"
    + NL + "        self._cue_items = {}       # 路径 → MediaItem（同一个素材被多个子层复用）"
    + NL + "        self._cue_last_frame = {}  # lyr_id → 最后一帧（seek/冻结期间不黑）"
    + NL + "        self._cue_sub_tmps = {}    # lyr_id → subtract 用的暂存画布"
    + NL + "        self._cue_seek_state = {}  # lyr_id → seek 状态（in-flight / degraded / target）"
    + NL + "        self.player = None",
    "Layer.__init__ 加 9 个 cue 字段",
)

# ---------------------------------------------------------------- ② to_dict
sub(
    '                "role": self.role, "auto_mode": self.auto_mode, "is_kv": self.is_kv,',
    '                "role": self.role, "auto_mode": self.auto_mode, "is_kv": self.is_kv,'
    + NL
    + '                # 码表绑定关系要持久化（否则重启后"这个图层挂哪张码表"就丢了）'
    + NL
    + '                "is_cuesheet": bool(getattr(self, "is_cuesheet", False)),'
    + NL
    + '                "cuesheet_id": str(getattr(self, "cuesheet_id", "") or ""),',
    "Layer.to_dict 带 is_cuesheet / cuesheet_id",
)

# ---------------------------------------------------------------- ③ _CueSubLayer
sub(
    "class AutoVJEngine(QObject):",
    '''class _CueSubLayer:
    """码表内**单个子层**的轻量替身（**照搬备份 284-317 的完整 `__slots__`**）。

    为什么需要它：`_draw_layer()` 是按"一个 Layer"写的，会读下面这一整串属性。
    码表要在一个容器图层里画多个子层，就必须给每个子层一份**独立**状态，
    不能复用真 Layer（会污染它的播放状态）。

    ⚠ 为什么必须**完整照搬**、不能写"看起来够用"的缩写版：
      `_draw_layer` 里多处是 `getattr(lay, "xxx", 默认)`，缩写版**恰好**因为默认值不崩,
      但那是巧合 —— 一旦某处默认值或语义变了，就会**静默走错分支**（画面错但不报错）。
      多余字段无害，缺字段才是坑。
    ⚠ `speed` 不在这里被 `_draw_layer` 读（计速在播放器的 `set_speed` 上），
      但保留它以与真 Layer 同构，避免以后有人"按 Layer 的属性表"写代码时踩空。
    """
    __slots__ = ("name", "opacity", "is_kv", "lock_motion", "use_alpha", "visible",
                 "prev_player", "cur", "prev_idx", "trans_active", "trans_ms",
                 "last_switch_t", "recent", "cache", "_alpha_key", "_alpha_img", "_sub_tmp",
                 "clips", "lyr_id")

    def __init__(self, lyr_id="", opacity=1.0, item=None, name=""):
        self.lyr_id = lyr_id
        self.name = name
        self.opacity = max(0.0, min(1.0, float(opacity)))
        self.is_kv = False
        self.lock_motion = False
        self.use_alpha = True
        self.visible = True
        self.prev_player = None            # 过渡由 alpha 控制，不用播放器交叉淡化
        self.cur = 0
        self.prev_idx = -1
        self.trans_active = False
        self.trans_ms = None
        self.last_switch_t = 0.0
        self.recent = []
        self.cache = {}
        self._alpha_key = None
        self._alpha_img = None
        self._sub_tmp = None
        # ★ 必须是**真实 MediaItem**：`make_player()` 读 `item.kind` / `item._thumb_path()`，
        #   自造替身会让工厂抛异常（被吞掉 ⇒ 表现为"码表图层一片黑"，很难查）。
        self.clips = [item] if item is not None else []


class AutoVJEngine(QObject):''',
    "模块级 _CueSubLayer（完整 __slots__）",
)

# ---------------------------------------------------------------- ④ __init__ 状态
sub(
    "        self.cfg = config" + NL + "        self.audio = audio_state" + NL
    + "        self.library = media_library" + NL + "        self.layers = []",
    "        self.cfg = config" + NL + "        self.audio = audio_state" + NL
    + "        self.library = media_library" + NL + "        self.layers = []" + NL
    + "        # ---- 码表（cue）引擎级状态（S3 数据面；S4 起由 _cuesheet_tick 消费）----"
    + NL + "        self._cue_sheet = None      # 当前码表（cuesheet.CueSheet）"
    + NL + "        self._cue_track = None      # 认到的曲目（TrackEntry）；None ⇒ 不接管"
    + NL + "        self._cue_rt = None         # cuesheet_runtime.CueRuntime"
    + NL + "        self._cue_media = {}        # clip_id → 磁盘路径"
    + NL + "        self._cue_pos_beats = None  # 本帧拍位（S3 只初始化，S4 才写）"
    + NL + "        self._cue_pos_src = \"\"      # 位置来源（fingerprint / deck）"
    + NL + "        self._cue_sid = -1          # 当前曲目 sid（换歌要重建 runtime）"
    + NL + "        self._cue_jumped = False    # 本帧是否发生跳变重算"
    + NL + "        self._cue_closed_last = 0   # 上次 clear 关掉的解码器数（自检/排障用）",
    "AutoVJEngine.__init__ 加 cue 状态",
)

# ---------------------------------------------------------------- ⑤ _get_player 早返回
sub(
    '''    def _get_player(self, lay, clip):
        """取该素材的解码器；缓存命中则复用（切换零开销）。LRU 上限 8。"""''',
    '''    def _get_player(self, lay, clip):
        """取该素材的解码器；缓存命中则复用（切换零开销）。LRU 上限 8。"""
        # ★★ 码表图层（容器层）**必须在这里早返回**，绝不进下面的通用 LRU：
        #   ① 通用 LRU 上限写死 8，且只认 `lay.clips[lay.cur]` / `lay.player` /
        #      `lay.prev_player` 三个"在用"标记 —— 而容器层没有单一 player、`clips` 也是空的
        #      ⇒ 谁都保护不了 ⇒ **会把正在用的 cue 解码器当空闲逐出**（表现为黑屏）；
        #   ② 通用逐出是 `v.close()`，**没有先 `set_active(False)`** —— 那正是
        #      2026-09-30「12 核 / 磁盘 750MB/s」事故的根因之一。
        if getattr(lay, "is_cuesheet", False):
            return self._cue_get_player(lay, clip)''',
    "_get_player cue 早返回",
)

# ---------------------------------------------------------------- ⑥ cue 数据面方法
sub(
    "    def _preload_clips(self, lay, clips=None, max_preload=3):",
    '''    # ================= 码表（cue）数据面 =================
    def _cue_get_player(self, lay, clip):
        """码表容器层的**独立**播放器缓存（容量按码表素材数给足；逐出三件套齐全）。

        与普通图层的 `lay.cache` 完全隔离，原因见 `_get_player` 开头的注释。
        逐出顺序固定：**`set_active(False)` → `close()` → 从 dict 删引用**（缺一不可）。
        """
        mw = max(1, int(self.canvas.width() * 1.5))
        mh = max(1, int(self.canvas.height() * 1.5))
        cache = getattr(lay, "_cue_players", None)
        if not isinstance(cache, dict):
            cache = {}
            lay._cue_players = cache
        q = cache.get(clip.path)
        if q is not None:
            if hasattr(q, "set_max_size"):
                q.set_max_size(mw, mh)
            return q
        # ★ cue=True：码表场景的 DXV 不走 GPU（GPU 播放器没有 position/seek）
        q = make_player(clip, cue=True)
        if hasattr(q, "set_max_size"):
            q.set_max_size(mw, mh)
        cache[clip.path] = q
        cap = max(32, len(getattr(self, "_cue_media", None) or {}) + 16)
        while len(cache) > cap:
            act = {getattr(st, "clip_path", "") for st in
                   (getattr(lay, "cs_states", None) or [])
                   if getattr(st, "active", False)}
            victim = next((k for k in list(cache) if k not in act), None)
            if victim is None:
                break                       # 全都在用 ⇒ 宁可超一点，也不动活跃的
            r = cache.pop(victim)
            try:
                if hasattr(r, "set_active"):
                    r.set_active(False)     # ★ 先失活
            except Exception:                # noqa: BLE001
                pass
            try:
                r.close()
            except Exception:                # noqa: BLE001
                pass
        return q

    def set_cue_sheet(self, sheet, media_paths=None, track=None, layer=None,
                      sheet_id="", track_title="", track_artist="",
                      track_duration=0.0):
        """装载一张码表（**数据面**：本步不参与渲染，渲染在 S4/S5）。

        · `track` 给了就直接用；否则按 (title/artist/duration) 自动认一首；
        · **认不出曲目 ⇒ 只装载码表、不建运行时** ⇒ 码表图层不接管（安全降级）；
        · 幂等：内部先 `clear_cue_sheet(silent=True)`，同一份调两次不会叠两份；
        · `layer` 给了就把它标记成码表容器层（`is_cuesheet=True`）。
        返回 `True` = 已建立运行时（可接管）。
        """
        import cuesheet_runtime as _cr
        self.clear_cue_sheet(silent=True)
        self._cue_sheet = sheet
        self._cue_media = dict(media_paths or {})
        if track is None and sheet is not None:
            try:
                track = sheet.track_by_identity(track_title, track_artist,
                                                track_duration)
            except Exception:                                     # noqa: BLE001
                track = None
        self._cue_track = track
        self._cue_rt = None
        if sheet is not None and track is not None:
            try:
                self._cue_rt = _cr.CueRuntime(sheet, track,
                                              media_paths=self._cue_media)
            except Exception:                                     # noqa: BLE001
                self._cue_rt = None
        if layer is not None:
            layer.is_cuesheet = True
            layer.cuesheet_id = str(sheet_id or getattr(sheet, "title", "") or "")
            if not isinstance(getattr(layer, "_cue_players", None), dict):
                layer._cue_players = {}
        return self._cue_rt is not None

    def clear_cue_sheet(self, silent=False):
        """卸下码表：关掉**所有** cue 解码器、清掉运行时状态。

        · 图层本身的 `is_cuesheet` **保留**（它还是容器层，只是暂时没装表）；
        · 关闭顺序：先 `set_active(False)` 再 `close()`（9-30 事故根因防线）；
        · 返回 True（幂等：没装载时调用也安全）。
        """
        self._cue_sheet = None
        self._cue_track = None
        self._cue_rt = None
        self._cue_media = {}
        self._cue_pos_beats = None
        self._cue_pos_src = ""
        self._cue_sid = -1
        self._cue_jumped = False
        closed = 0
        for lay in (getattr(self, "layers", None) or []):
            if not getattr(lay, "is_cuesheet", False):
                continue
            lay.cs_states = []
            lay.cs_active = False
            for q in list((getattr(lay, "_cue_players", None) or {}).values()):
                try:
                    if hasattr(q, "set_active"):
                        q.set_active(False)
                except Exception:                                 # noqa: BLE001
                    pass
                try:
                    q.close()
                    closed += 1
                except Exception:                                 # noqa: BLE001
                    pass
            lay._cue_players = {}
            lay._cue_items = {}
            lay._cue_last_frame = {}
            lay._cue_seek_state = {}
            lay._cue_sub_tmps = {}
        self._cue_closed_last = closed
        return True

    def cuesheet_status(self):
        """只读状态快照（UI 状态栏 / 自检用；**不参与渲染**，也不改任何状态）。"""
        sheet = getattr(self, "_cue_sheet", None)
        rt = getattr(self, "_cue_rt", None)
        track = getattr(self, "_cue_track", None)
        idn = getattr(track, "identity", None)
        lays = [l for l in (getattr(self, "layers", None) or [])
                if getattr(l, "is_cuesheet", False)]
        return {
            "loaded": sheet is not None,
            "title": str(getattr(sheet, "title", "") or ""),
            "tracks": len(getattr(sheet, "tracks", None) or []),
            "res": (sheet.aspect() if sheet is not None else (1920, 1080)),
            "track_title": str(getattr(idn, "title", "") or "") if idn else "",
            "bpm": float(getattr(idn, "bpm", 0.0) or 0.0) if idn else 0.0,
            "has_runtime": rt is not None,
            "rt_layers": int(getattr(rt, "n_layers", 0) or 0) if rt is not None else 0,
            "rt_items": int(getattr(rt, "n_items", 0) or 0) if rt is not None else 0,
            "bound_layers": [l.name for l in lays],
            "active_layers": [l.name for l in lays if getattr(l, "cs_active", False)],
            "closed_last": int(getattr(self, "_cue_closed_last", 0) or 0),
        }

    def cuesheet_wanted(self):
        """是否**有可见的码表图层**在等位置数据（小眼睛关掉 ⇒ 完全不检测）。"""
        for lay in (getattr(self, "layers", None) or []):
            if getattr(lay, "is_cuesheet", False) and getattr(lay, "visible", True):
                return True
        return False

    def _preload_clips(self, lay, clips=None, max_preload=3):''',
    "cue 数据面方法（_cue_get_player / set / clear / status / wanted）",
)

open(P, "w", encoding="utf-8").write(text)
print("engine.py 已改：%d 处" % len(edits))
for e in edits:
    print("  ·", e)
print("字节数 %d → %d" % (len(orig), len(text)))
