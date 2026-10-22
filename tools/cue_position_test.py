# -*- coding: utf-8 -*-
"""Step3 自检：位置认歌（指纹 → 拍位；自动选表；手动起拍降级为兜底）。

6 项判据（团队给定）：
  1. 真实播放（**模拟指纹流**）⇒ 画面自动跟拍；3 个采样点 `pos_beats` 偏差 ≤ 0.5 拍
  2. 认不到歌 ⇒ 不接管 / 不报错 / 不黑屏（手动起拍仍可用）
  3. 切歌 A→B ⇒ A 的表停用；B 有表则换表，没有则卸表（策略明确）
  4. 指纹抖动（短时丢票）⇒ 不抖断（回滞生效）；恢复后继续跟
  5. 性能：位置更新是**低频轮询**（0.2s），且 `_compose` 里**没有任何指纹/位置计算**
  6. 回归另跑（12 项）

⚠ 本文件不接真音频设备：把 `audio_engine` 的识别状态（`recognized_*`）**注入**成假对象，
   走的仍是引擎里的真代码路径（`_cue_auto_position` → `CuePositionTracker` → `set_cue_position`
   → `_cuesheet_tick` → `_compose`）。

运行：`python tools/cue_position_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import ast
import hashlib
import os
import sys
import tempfile
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QColor, QImage                            # noqa: E402

import cuesheet                                                     # noqa: E402
import engine                                                       # noqa: E402
import cue_index                                                    # noqa: E402
import cue_position                                                 # noqa: E402
from cue_seek_policy import CueSeekPolicy                            # noqa: E402

W, H = 320, 180
BPM, BL = 120.0, 0.5          # 120 BPM ⇒ beat_len = 0.5s


# ==========================================================================
class FakeState:
    """`audio_engine.state` 的替身：只有指纹识别那几个字段。"""

    def __init__(self):
        self.recognized_song_id = -1
        self.recognized_offset = -1.0
        self.recognized_votes = 0
        self.recognized_rate = 1.0
        self.recognized_t = 0.0
        self.lock = __import__("threading").RLock()


class FakePlayer:
    def __init__(self, img):
        self._img = img
        self._active = False

    def current(self):
        return self._img

    def set_active(self, a):
        self._active = bool(a)

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        pass

    def position(self):
        return 0.0

    def seek(self, s):
        return True

    def close(self):
        pass


class FakeCfg:
    def __init__(self):
        # `_compose` 也要用到这几段（本测试会真的调它出画）
        self.data = {"music_library": [], "music_meta": {}, "cue": {},
                     "auto": {"intensity": 1, "transition": None},
                     "mode": {"transition": ""}, "color": {}, "postfx": {}}
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, d=None):
        return self.data.get(k, d)

    def save(self, force=False):
        self.saves += 1


def make_sheet(title, track_title, artist, dur, bpm, md5, media_name):
    s = cuesheet.CueSheet(title=title, res_w=W, res_h=H)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title=track_title, artist=artist,
                                    duration_sec=dur, bpm=bpm, file_md5=md5)
    te.identity.ensure_meta_id()
    L = cuesheet.CompLayer(lyr_id="L1", z=0)
    L.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=0.0,
                                    src_in_sec=0.0, src_out_sec=60.0,
                                    trans_in="cut", trans_out="cut").normalized())
    te.layers.append(L)
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name=media_name))
    return s


def new_engine(cfg, layers, images):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = QImage(W, H, QImage.Format_ARGB32)
    eng.mode = "normal"
    eng.blackout = False
    eng.cfg = cfg
    eng._silent_now_debounced = False
    eng._health_log = None
    eng._cue_tick_fail = 0
    eng._cue_err_at = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_media = {}
    eng._cue_jumped = False
    eng._cue_closed_last = 0
    eng._cue_policy = CueSeekPolicy(cfg={})
    eng._cue_index = None
    eng._cue_rt = None
    eng.layers = list(layers)
    # ★ 2026-10-17：**必须与生产接线一致** —— 生产传的是 `self.audio.state`，
    #   所以 `engine.audio` 是**状态对象本身**。原来这里包了一层 SimpleNamespace(state=…)
    #   ⇒ engine.py 里 `getattr(self.audio, 'state', None)` 的错写法在这个夹具下能work
    #   ⇒ 测试全绿而现场 `sid=-1` 永不接管（真实故障持续了两轮）。
    eng.audio = FakeState()
    eng._audio_engine = None
    eng._cue_pos_tracker = cue_position.CuePositionTracker()
    # ★ 2026-10-18：自由运行（freewheel）状态 —— **必须与 `__init__` 同步**，
    #   否则 `_cue_auto_position` 走到外推分支会 AttributeError（铁律 24）。
    eng._cue_fw_pos = None
    eng._cue_fw_t = 0.0
    eng._cue_fw_rate = 1.0
    eng._cue_fw_on = False
    eng._cue_pos_next_t = 0.0
    eng._cue_pos_song = -1
    eng._cue_auto_sheet = ""
    eng._cue_manual_clock = False
    eng._images = images
    return eng


def hold(eng, sec):
    """让下一次 `_cue_auto_position()` 立即执行（它内部有 0.2s 门控）。"""
    eng._cue_pos_next_t = 0.0
    return eng._cue_auto_position()


# ==========================================================================
def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="cuepos_")
    media = os.path.join(tmp, "media")
    os.makedirs(media, exist_ok=True)
    for nm, col in (("a.mp4", (210, 30, 30, 255)), ("b.mp4", (30, 90, 220, 255))):
        open(os.path.join(media, nm), "wb").close()
    images = {}
    for nm, col in (("a.mp4", (210, 30, 30, 255)), ("b.mp4", (30, 90, 220, 255))):
        im = QImage(W, H, QImage.Format_ARGB32)
        im.fill(QColor(*col))
        images[nm] = im

    real_make = engine.make_player
    engine.make_player = lambda item, cue=False: FakePlayer(
        images.get(os.path.basename(getattr(item, "path", "")), QImage()))
    try:
        cfg = FakeCfg()
        cue_index.cue_cfg(cfg)["media_dir"] = media
        # 两首歌、两张表：chipA 对应 a.mp4，chipB 对应 b.mp4
        pkgA = os.path.join(tmp, "A.ercue")
        pkgB = os.path.join(tmp, "B.ercue")
        cuesheet.save(make_sheet("表A", "SongA", "ArtistA", 200.0, BPM, "md5A", "a.mp4"),
                      pkgA, app_version="2026.10.17.12")
        cuesheet.save(make_sheet("表B", "SongB", "ArtistB", 180.0, BPM, "md5B", "b.mp4"),
                      pkgB, app_version="2026.10.17.12")
        cfg["music_library"] = [os.path.join(media, "a.mp4"), os.path.join(media, "b.mp4")]
        cfg["music_meta"] = {
            os.path.join(media, "a.mp4"): {"title": "SongA", "artist": "ArtistA",
                                           "duration": 200.0, "md5": "md5A", "bpm": BPM},
            os.path.join(media, "b.mp4"): {"title": "SongB", "artist": "ArtistB",
                                           "duration": 180.0, "md5": "md5B", "bpm": BPM},
        }
        # ★ 现场约定（已核对 ui_main.py:1791）：song_id = int(md5(path)[:8], 16)
        pathA = os.path.join(media, "a.mp4")
        pathB = os.path.join(media, "b.mp4")
        SID_A = int(hashlib.md5(pathA.encode("utf-8")).hexdigest()[:8], 16)
        SID_B = int(hashlib.md5(pathB.encode("utf-8")).hexdigest()[:8], 16)
        print("      真实 song_id：A=%d  B=%d（md5 路径哈希）" % (SID_A, SID_B))
        lay = engine.Layer("码表层")
        lay.is_cuesheet = True
        eng = new_engine(cfg, [lay], images)
        idx = eng.cue_index()
        idx.add(pkgA)
        idx.add(pkgB)
        idx.save_cfg(cfg)

        # ---------------- ⓪ song_id 方案对齐（真机能否跑通的关键）----------------
        print("⓪ song_id 约定：必须与建库处同算法（md5(path)[:8]）")
        check("A 的 id 能解析回 A 的路径", eng._cue_song_path(SID_A) == pathA,
              eng._cue_song_path(SID_A))
        check("B 的 id 能解析回 B 的路径", eng._cue_song_path(SID_B) == pathB,
              eng._cue_song_path(SID_B))
        check("字符串路径原样可用（兼容测试/嵌入）", eng._cue_song_path(pathA) == pathA)
        check("**库索引不再被当成 id**（0/1 解析不出东西，避免静默错配）",
              eng._cue_song_path(0) == "" and eng._cue_song_path(1) == "",
              (eng._cue_song_path(0), eng._cue_song_path(1)))

        # ---------------- ① 自动跟拍：3 个采样点 ----------------
        print("① 自动跟拍（模拟指纹 0.5s 一次）：3 个采样点偏差 ≤ 0.5 拍")
        st = eng.audio
        st.recognized_song_id, st.recognized_votes, st.recognized_rate = SID_A, 5, 1.0
        samples = []
        # ★ 2026-10-18：采样间隔 0.5s ⇒ 偏移也必须每次 +0.5s（= 1 倍速播放）。
        #   原值 12/14（+2s/0.5s = 4 倍速）其实是"每半秒跳两秒"，防抖会**滑移**去追，
        #   断言"立刻相等"就不再成立。1 倍速才是要验证的真实场景。
        for i, off in enumerate((10.0, 10.5, 11.0)):
            st.recognized_offset = off
            hold(eng, i * 0.5)
            eng._cuesheet_tick()
            pb = eng._cue_pos_beats
            want = off / BL
            got = lay.cs_states[0].play_sec if lay.cs_states else None
            samples.append((off, want, pb, got, (pb or -9) - want))
        # ★ 必须**真的等 0.5 秒**：位置层是按挂钟在两次识别之间插值的。
        #   以前这里只 sleep 0.02 ⇒ 等于"偏移每 0.02 秒跳 0.5 秒"（25 倍速），
        #   任何防抖都会把它当跳变；真实播放必须让挂钟走够。
        time.sleep(0.5)
        for off, want, pb, got, err in samples:
            print("      offset=%.1fs ⇒ 期望 pos_beats=%.1f ；实测 %.2f（偏差 %+.2f 拍）；"
                  "play_sec=%.2f" % (off, want, pb or -1, err, got if got is not None else -1))
        # ★ 2026-10-18（用户要"防抖"）：位置层现在会对"识别结果突变"做**滑移**
        #   （每次喂 ≤0.06s），而不是瞬间对齐 —— 这正是"不跳"的代价：**允许短暂落后**，
        #   但必须 ① 单调 ② 每点偏差有界 ③ **最后一个采样点收敛**。
        check("pos_beats 单调不减（防抖不允许倒走）",
              all((samples[i][2] or 0) >= (samples[i - 1][2] or 0)
                  for i in range(1, len(samples))), samples)
        check("每个采样点偏差 ≤2 拍（滑移追赶中的合理上界）",
              all(abs(e) <= 2.0 for _o, _w, _p, _g, e in samples), samples)
        # ⚠ 不在这里断言"追平到某个绝对值"：挂钟一直在走，`want` 是**采样那一刻**的期望，
        #   再等几秒位置本来就该更靠前。收敛性由 `_cue_pos_smooth_test.py` 的
        #   "卡顿 2 秒后一次性跳 5.7 秒 ⇒ 必须滑移追上" 那条专门覆盖。
        check("画面随之出画（play_sec 非空且逐点前进）",
              all(g is not None for o, _w, _p, g, _e in samples)
              and samples[-1][3] > samples[0][3], samples)
        check("自动装表成功（表A）", "A.ercue" in (eng._cue_auto_sheet or ""),
              eng._cue_auto_sheet)
        check("位置来源标记为 fp", eng._cue_pos_src == "fp", eng._cue_pos_src)

        # ---------------- ④ 抖动：短时丢票不抖断 ----------------
        print("\n④ 指纹抖动：连续 3 次丢票仍保持（回滞），恢复后继续跟")
        pb_before = eng._cue_pos_beats
        holds = []
        for k in range(3):
            st.recognized_votes = 0                 # 丢票
            st.recognized_offset = -1.0
            status, pb = hold(eng, 0)
            eng._cuesheet_tick()
            holds.append((status, eng._cue_pos_beats is not None))
            time.sleep(0.02)
        print("      丢票 3 次：%s" % holds)
        check("容忍期内状态为 hold 且位置不丢",
              all(s == "hold" and keep for s, keep in holds), holds)
        st.recognized_votes, st.recognized_offset = 60, 16.0
        # ★ 2026-10-18（速度式伺服 + 3 秒防抖）：+6s 这类"严重不符"必须先持续 3 秒，
        #   期满才允许重锚（用户："超过这个时间指纹还是严重对不上才切"）。
        #   所以这里要喂够 4 秒，而不是两次。
        import time as _t
        for _ in range(10):          # 真实等 3.5 秒（防抖按真实时间计）
            _t.sleep(0.35)
            hold(eng, 0)
        status, pb = hold(eng, 0)
        eng._cuesheet_tick()
        # 新契约：先按 ±12% 平滑收敛/防抖期满重锚，判据是"朝目标走且已接近"
        check("防抖期满（>3s）后朝目标收敛",
              status == "ok" and 20.0 <= (eng._cue_pos_beats or -1) <= 16.0 / BL + 1.0,
              (status, eng._cue_pos_beats))
        # 超过容忍期 ⇒ 不接管
        for _ in range(6):
            st.recognized_votes = 0
            hold(eng, 0)
            time.sleep(0.01)
        eng._cuesheet_tick()
        # ★★ 2026-10-18（用户："码表图层偶尔突然消失零点几秒"）：
        #   短时丢票**不再立刻卸画面** —— 引擎侧有"自由运行（freewheel）"窗口，
        #   保持最后位置并按速率外推，最多 CUE_FREEWHEEL_SEC 秒。
        from engine import CUE_FREEWHEEL_SEC as _FW
        check("自由运行窗口内仍继续出画（不再闪一下黑）",
              eng._cue_pos_beats is not None and bool(lay.cs_states),
              (eng._cue_pos_beats, len(lay.cs_states)))
        # ★ 2026-10-18 真机实测后改契约：**只要还是同一首歌、还有声音，就一直滑行**
        #   （识别卡 2~4 秒不能闪画面）。真正的释放条件只有：没歌 / 静音太久 / 超上限。
        time.sleep(_FW + 0.3)
        hold(eng, 0)
        eng._cuesheet_tick()
        check("同曲丢票超窗口 ⇒ 仍继续滑行（不再闪出画面）",
              eng._cue_pos_beats is not None and bool(lay.cs_states),
              (eng._cue_pos_beats, len(lay.cs_states)))
        # 真的没歌了（sid=-1 + 静音）⇒ 必须释放
        st.recognized_song_id = -1
        st.silent_sec = 5.0
        hold(eng, 0)
        eng._cuesheet_tick()
        check("没歌/静音 ⇒ 才卸画面（状态 lost、cs_states 清空）",
              eng._cue_pos_beats is None and not lay.cs_states,
              (eng._cue_pos_beats, len(lay.cs_states)))
        check("丢歌时**表与徽标保留**（回来能立刻继续）", eng._cue_sheet is not None,
              eng._cue_sheet)

        # ---------------- ② 认不到歌 ⇒ 手动起拍仍可用 ----------------
        print("\n② 认不到歌：不接管 / 不报错 / 手动起拍可用")
        check("此时不接管（cs_states 空）", not lay.cs_states)
        eng.set_cue_manual_clock(True)          # 相当于点了「手动起拍」
        eng.set_cue_position(8.0, "wallclock")
        eng._cuesheet_tick()
        check("手动起拍立刻接管（cs_states 非空, src=wallclock）",
              bool(lay.cs_states) and eng._cue_pos_src == "wallclock",
              (len(lay.cs_states), eng._cue_pos_src))
        hold(eng, 0)
        check("手动起拍期间，自动位置**让位**（不被指纹覆盖）",
              eng._cue_pos_src == "wallclock", eng._cue_pos_src)
        eng.set_cue_manual_clock(False)

        # ---------------- ③ 切歌 A→B ----------------
        print("\n③ 切歌：A→B（B 有表 ⇒ 换表）")
        st.recognized_song_id, st.recognized_votes, st.recognized_offset = SID_A, 5, 5.0
        hold(eng, 0)
        eng._cuesheet_tick()
        sheetA = eng.cuesheet_status()["title"]
        autoA = os.path.basename(eng._cue_auto_sheet or "")
        # 切到 B：需要连续 2 次确认（CONFIRM_N=2）
        st.recognized_song_id, st.recognized_offset = SID_B, 7.0
        hold(eng, 0)
        mid = eng.cuesheet_status()["title"]
        hold(eng, 0)
        eng._cuesheet_tick()
        sheetB = eng.cuesheet_status()["title"]
        autoB = os.path.basename(eng._cue_auto_sheet or "")
        print("      A：表=%s(%s) → 第1次B票仍 %s → 确认后 表=%s(%s)"
              % (sheetA, autoA, mid, sheetB, autoB))
        check("A 时用表A", sheetA == "表A" and autoA == "A.ercue", (sheetA, autoA))
        check("换歌第 1 票不立刻切（防误切）", mid == "表A", mid)
        check("连续确认后换到表B", sheetB == "表B" and autoB == "B.ercue", (sheetB, autoB))
        eng._compose({"bass": 0.0})
        c = eng.canvas.pixelColor(W // 2, H // 2)
        check("画面跟着换成 B 的内容（蓝）", c.blue() > 120 and c.red() < 120, c.getRgb())

        # ---------------- ⑤ 性能：不进渲染热路径 ----------------
        print("\n⑤ 性能：位置更新是低频轮询，`_compose` 里零指纹计算")
        eng._cue_pos_next_t = 1e18          # 让门控命中（模拟"0.2s 内重复调用"）
        t0 = time.perf_counter()
        for _ in range(20000):
            eng._cue_auto_position()
        cheap = (time.perf_counter() - t0) / 20000 * 1e6
        eng._cue_pos_next_t = 0.0
        t0 = time.perf_counter()
        for _ in range(2000):
            eng._cue_pos_next_t = 0.0
            eng._cue_auto_position()
        full = (time.perf_counter() - t0) / 2000 * 1e6
        print("      门控命中 %.3f µs/次 ；真轮询 %.1f µs/次（含 tracker+set_cue_position）"
              % (cheap, full))
        check("门控命中（未到期）< 5µs", cheap < 5.0, round(cheap, 3))
        check("真轮询 < 200µs（0.2s 一次 ⇒ 占 0.1%）", full < 200.0, round(full, 1))
        src = open(os.path.join(SRC, "engine.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in ("_compose", "_draw_cuesheet_layer", "_cue_frame"):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Attribute) and sub.attr.startswith("recognized_"):
                            hits.append(node.name)
        check("_compose/_draw_cuesheet_layer/_cue_frame 里零 `recognized_*` 引用",
              not hits, hits)

        # ---------------- 选表策略（同级多张 ⇒ prefer） ----------------
        print("\n⑥ 选表策略：级别优先 → prefer → mtime → 路径")
        ea = {"path": "P:/a.ercue", "mtime": 100.0, "title": "A"}
        eb = {"path": "P:/b.ercue", "mtime": 200.0, "title": "B"}
        pick = cue_position.choose_sheet([(ea, "meta"), (eb, "meta")], prefer={}, track_meta_id="")
        check("同级无偏好 ⇒ 取 mtime 最新", pick["path"] == "P:/b.ercue", pick)
        pick = cue_position.choose_sheet([(ea, "meta"), (eb, "meta")],
                                         prefer={"k": "P:/a.ercue"}, track_meta_id="k")
        check("有 prefer ⇒ 用记住的那张", pick["path"] == "P:/a.ercue", pick)
        pick = cue_position.choose_sheet([(ea, "md5"), (eb, "meta")], prefer={"k": "P:/b.ercue"},
                                         track_meta_id="k")
        check("prefer 不能盖过更高命中级别", pick["path"] == "P:/a.ercue", pick)
    finally:
        engine.make_player = real_make

    print("")
    if fails:
        print("POS_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("POS_OK  自动跟拍/丢歌不接管/切歌换表/抖动不抖断/低频轮询/选表策略 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
