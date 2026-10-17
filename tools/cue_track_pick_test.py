# -*- coding: utf-8 -*-
"""**认歌选曲目**自检：导入不弹窗 + 用"正在播放的那首"认 + 同曲多版本护栏放宽。

用户原话（这一轮的两个问题）：
  「如果一首歌，我先在EA中扫描进了曲库，然后这首歌的另外一个版本，就是它的长度啊、
    音乐啊完全都是一样的，就是专辑和封面不一样。我把它在EC里面去做了一个码表包，
    然后导入到EA中，这个时候的逻辑是怎么样的？」
  「为什么有绑定一首的弹窗 我做了这个码表那就说明码表里所有的东西包括音乐和素材我都要用」

判据（6 项，另加一段真实包实测）：
  1. `CueSheetDialog._auto_match()` **会用认歌信息**：正在播放的歌对应表里某一条 ⇒ 返回**那一条**
     （老实现 `sheet.match_track()` 不给参数 ⇒ 永远 `(None, "")`，是死代码）
  2. 多曲目 + 没在放歌 ⇒ **不弹窗**（`pick_track` 零调用）、装表但 `track is None`、提示可见
  3. 单曲目表 ⇒ 仍直接绑定（老行为不能破）
  4. `engine._cue_bound_song_ids()`：同 fp_id / 同 file_md5 / 同名+艺人+时长 / 合成 id 都进集合，
     **另一首歌不进**
  5. `engine._cue_accept_pos()`：集合内任一 id ⇒ 照跟；集合外 ⇒ 拒绝（**反向能力不能丢**）；
     集合为空 ⇒ 不启用护栏
  6. 真窗口：`QDialog` 导入路径不弹（`pick_track` 零调用）+ 状态行截图

**真实包实测**（末尾）：用户那个 `码表素材\\测试.ercue`（2 首 / fp/ 2 个）在"曲库里是同一首
音乐的另一个版本"的模拟下，`_cue_bound_song_ids()` 到底返回什么。

运行：`venv\\Scripts\\python.exe tools/cue_track_pick_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QColor, QImage                            # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

import cuesheet                                                      # noqa: E402
import cue_ui                                                        # noqa: E402
import engine                                                        # noqa: E402
from cue_seek_policy import CueSeekPolicy                            # noqa: E402

W, H = 320, 180
BPM = 120.0
OK, FAIL = [], []

#: 用户那个真实包（2026-10-18 挪进了 `码表素材\\`；老位置也留着兼容）
REAL_PKG = next((p for p in (r"Y:\EasyRealityAutoVJ\码表素材\测试.ercue",
                             r"Y:\EasyRealityAutoVJ\测试.ercue")
                 if os.path.isfile(p)), "")


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                          ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


# ==========================================================================
# 替身
# ==========================================================================
class FakeState:
    """`audio_engine.state` 里 `now_playing_query()` 读的那个字段。"""

    def __init__(self):
        self.recognized_song_id = -1
        self.recognized_offset = -1.0
        self.recognized_votes = 0
        self.recognized_rate = 1.0


class FakeCfg:
    def __init__(self, data=None):
        self.data = json.loads(json.dumps(data or {}))
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, d=None):
        return self.data.get(k, d)

    def save(self, force=False):
        self.saves += 1


class FakePlayer:
    def __init__(self, img):
        self._img = img

    def current(self):
        return self._img

    def set_active(self, a):
        pass

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        pass

    def position(self):
        return 0.0

    def seek(self, sec):
        return True

    def close(self):
        pass


class Owner:
    """`CueSheetDialog` / `load_and_bind` 要的宿主（cfg + engine）。"""

    def __init__(self, cfg, eng):
        self.cfg = cfg
        self.engine = eng
        self.layers_panel = None


class FDStub:
    """文件对话框桩：无头环境没人点，直接"取消"。"""

    @staticmethod
    def getOpenFileNames(*a, **k):
        return ([], "")

    @staticmethod
    def getExistingDirectory(*a, **k):
        return ""


def new_engine(cfg, layers, img):
    """真引擎骨架（`__new__` + 只补 cue 相关字段），方法全是正式代码。"""
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
    eng._cue_bound_cache = None
    eng._cue_bad_song_n = 0
    eng._cue_bad_song_t = 0.0
    eng._cue_pos_song = -1
    eng._cue_auto_sheet = ""
    eng._cue_manual_clock = False
    eng._cue_pos_next_t = 0.0
    eng._cue_sid = -1
    eng.layers = list(layers)
    eng.audio = types.SimpleNamespace(state=FakeState())
    eng._images = {}
    return eng


def song_id(path):
    return int(hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8], 16)


def make_sheet(titles, bpm=BPM, dur=200.0):
    """造一张 n 曲目码表：`titles` = [(title, artist), ...]。"""
    s = cuesheet.CueSheet(title="选曲目自检", res_w=W, res_h=H)
    for k, (tt, ar) in enumerate(titles):
        te = cuesheet.TrackEntry()
        te.identity = cuesheet.Identity(title=tt, artist=ar, duration_sec=dur, bpm=bpm,
                                        file_name="%s.flac" % tt)
        te.identity.ensure_meta_id()
        L = cuesheet.CompLayer(lyr_id="L%d" % k, z=0)
        L.items.append(cuesheet.CueItem(item_id="i%d" % k, clip_id="c1", at_beats=0.0,
                                        src_in_sec=0.0, src_out_sec=8.0,
                                        trans_in="cut", trans_out="cut").normalized())
        te.layers.append(L)
        s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    return s


def save_pkg(sheet, path):
    """写一个最小 `.ercue`（真 `cuesheet.save`）。"""
    cuesheet.save(sheet, path, app_version="2026.10.18.01")
    return path


# ==========================================================================
def main():
    app = QApplication.instance() or QApplication(sys.argv)          # noqa: F841
    tmp = tempfile.mkdtemp(prefix="cuepick_")
    os.makedirs(tmp, exist_ok=True)
    media = os.path.join(tmp, "media")
    os.makedirs(media, exist_ok=True)
    open(os.path.join(media, "a.mp4"), "wb").close()

    # 不碰真指纹库/后台线程（本测试只测"选曲目 + 护栏"，灌库另有 cue_fp_pack_test）
    real_ingest = cue_ui.cue_fp_pack.start_ingest
    cue_ui.cue_fp_pack.start_ingest = lambda *a, **k: None
    real_fd = cue_ui.QFileDialog
    cue_ui.QFileDialog = FDStub
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor(40, 90, 200, 255))
    real_make = engine.make_player
    engine.make_player = lambda item, cue=False: FakePlayer(img)
    try:
        # ---------------------------------------------------------------
        # 曲库：A = 正在播放的那首（有 meta）
        # ---------------------------------------------------------------
        PATH_A = os.path.join(media, "A.flac")
        open(PATH_A, "wb").close()
        SID_A = song_id(PATH_A)
        FP_A = "fp2-aaaaaaaaaaaaaaaa"
        MD5_A = "a" * 32
        cfg = FakeCfg({
            "music_library": [PATH_A],
            "music_meta": {PATH_A: {"title": "SongA", "artist": "ArtistA",
                                    "duration": 200.0, "file_md5": MD5_A,
                                    "fp_id": FP_A, "grid": {"dur": 200.0, "bpm": BPM}}},
            "cue": {"media_dir": media},
        })
        lay = engine.Layer("码表层")
        lay.is_cuesheet = True
        eng = new_engine(cfg, [lay], img)

        # ================= ① _auto_match 会用认歌信息 =================
        print("\n① _auto_match()：正在播放的歌对应表里某一条 ⇒ 返回那一条")
        sheet2 = make_sheet([("SongX", "ArtistX"), ("SongA", "ArtistA")])
        dlg = cue_ui.CueSheetDialog(Owner(cfg, eng))
        eng.audio.state.recognized_song_id = SID_A
        q, how = cue_ui.now_playing_query(eng)
        print("      now_playing_query → %r（%s）" % (q, how))
        chk(bool(q) and q.get("title") == "SongA", "能从引擎拿到「正在播放」的认歌参数", q)
        tr = dlg._auto_match(sheet2)
        got = getattr(getattr(tr, "identity", None), "title", None)
        chk(got == "SongA", "返回的是**正在播放那首**（不是 None、也不是第一条）",
            "%r（第 1 条是 SongX）" % (got,))
        chk(any("命中曲目" in w for w in dlg.warnings), "提示里写了「已按 … 命中曲目」",
            dlg.warnings)
        # 反向：正在播放的歌不在表里 ⇒ 不预设曲目
        PATH_B = os.path.join(media, "B.flac")
        open(PATH_B, "wb").close()
        cfg["music_library"] = [PATH_A, PATH_B]
        cfg["music_meta"][PATH_B] = {"title": "SongZ", "artist": "ArtistZ",
                                     "duration": 111.0, "grid": {"dur": 111.0}}
        eng.audio.state.recognized_song_id = song_id(PATH_B)
        dlg.warnings = []
        chk(dlg._auto_match(make_sheet([("SongX", "ArtistX"), ("SongA", "ArtistA")])) is None,
            "反向：正在播放的歌不在表里 ⇒ 不预设曲目（返回 None）", dlg.warnings)

        # ================= ② 多曲目 + 没在放歌 ⇒ 不弹窗 =================
        print("\n② 多曲目 + 没在放歌 ⇒ **不弹窗**（装表不绑曲目）")
        pkg2 = save_pkg(make_sheet([("SongX", "ArtistX"), ("SongA", "ArtistA")]),
                        os.path.join(tmp, "two.ercue"))
        eng2 = new_engine(cfg, [lay], img)
        eng2.audio.state.recognized_song_id = -1          # 没在放歌
        dlg2 = cue_ui.CueSheetDialog(Owner(cfg, eng2))
        picks = {"n": 0}
        real_pick = cue_ui.pick_track

        def _counting_pick(sheet, parent=None, current_title=""):
            picks["n"] += 1
            return None

        cue_ui.pick_track = _counting_pick
        try:
            ok = dlg2.import_one(pkg2)
        finally:
            cue_ui.pick_track = real_pick
        chk(picks["n"] == 0, "**没有弹「选哪一首」的窗**（pick_track 零调用）", picks["n"])
        chk(dlg2.track is None, "曲目未绑定（track is None）", dlg2.track)
        chk(ok is False, "返回 False（没建运行时 ⇒ 图层先不接管，安全降级）", ok)
        chk(eng2._cue_sheet is not None, "**但表装上了**（引擎里有这张表）",
            eng2._cue_sheet is not None)
        chk("自动选对应的那首" in dlg2.lbl_error.text(),
            "提示「放歌后会自动选对应的那首」可见", dlg2.lbl_error.text()[:120])

        # ================= ③ 单曲目 ⇒ 仍直接绑定 =================
        print("\n③ 单曲目表 ⇒ 仍直接绑定（老行为不能破）")
        pkg1 = save_pkg(make_sheet([("SongA", "ArtistA")]), os.path.join(tmp, "one.ercue"))
        eng3 = new_engine(cfg, [lay], img)
        eng3.audio.state.recognized_song_id = -1
        dlg3 = cue_ui.CueSheetDialog(Owner(cfg, eng3))
        ok1 = dlg3.import_one(pkg1)
        chk(ok1 is True and eng3._cue_rt is not None, "单曲目 ⇒ 建运行时、能接管",
            (ok1, eng3._cue_rt is not None))
        chk(getattr(getattr(dlg3.track, "identity", None), "title", None) == "SongA",
            "绑的就是那一首", getattr(getattr(dlg3.track, "identity", None), "title", None))

        # ================= ④ _cue_bound_song_ids =================
        print("\n④ _cue_bound_song_ids：同一首音乐的其它版本都进集合，别的歌不进")
        PATH_V2 = os.path.join(media, "A_v2.mp3")       # 同一首音乐、另一种编码（fp_id 不同）
        open(PATH_V2, "wb").close()
        SID_V2 = song_id(PATH_V2)
        PATH_OTHER = os.path.join(media, "other.flac")
        open(PATH_OTHER, "wb").close()
        SID_OTHER = song_id(PATH_OTHER)
        PATH_SYN = "<包里带来的指纹>"
        SYNTH = 1234567890
        cfg4 = FakeCfg({
            "music_library": [PATH_A, PATH_V2, PATH_OTHER],
            "music_meta": {
                # ① 同 fp_id（同一段音频：只改了 tag/封面）
                PATH_A: {"title": "SongA", "artist": "ArtistA", "duration": 200.0,
                         "file_md5": MD5_A, "fp_id": FP_A, "grid": {"dur": 200.0}},
                # ② 同 file_md5（fp_id 没写：老曲库）
                PATH_V2: {"title": "SongA (专辑版)", "artist": "ArtistA",
                          "duration": 200.0, "file_md5": MD5_A, "grid": {"dur": 200.0}},
                # ③ 另一首歌
                PATH_OTHER: {"title": "Other", "artist": "Nobody", "duration": 90.0,
                             "file_md5": "b" * 32, "grid": {"dur": 90.0}},
            },
            "cue": {"fp_songs": {"fp2-synth0000000000": {"song_id": SYNTH,
                                                         "title": "SongS",
                                                         "artist": "ArtistS",
                                                         "duration": 200.0,
                                                         "sheet": "x.ercue"}}},
        })
        eng4 = new_engine(cfg4, [lay], img)
        tr_a = cuesheet.TrackEntry(identity=cuesheet.Identity(
            title="SongA", artist="ArtistA", duration_sec=200.0, bpm=BPM,
            file_name="SongA.flac"))
        tr_a.identity.fp_id = FP_A
        tr_a.identity.file_md5 = MD5_A
        tr_a.identity.ensure_meta_id()
        eng4._cue_track = tr_a
        ids = eng4._cue_bound_song_ids()
        print("      同 fp_id / 同 md5 场景 → %r" % (sorted(ids),))
        chk(SID_A in ids, "同 fp_id 的本机歌 ⇒ 进集合", SID_A)
        chk(SID_V2 in ids, "同 file_md5 的本机歌（fp_id 不同）⇒ 进集合", SID_V2)
        chk(SID_OTHER not in ids, "**另一首歌 ⇒ 不进集合**（护栏的判据不能被放宽）", SID_OTHER)
        chk(eng4._cue_bound_song_id() == SID_A, "主 id 仍是「本条 track 认出来的那个」",
            eng4._cue_bound_song_id())
        # ③ 只有"同名+艺人+时长"的（fp_id/md5 都不相同）
        cfg4["music_meta"][PATH_V2].pop("file_md5")
        cfg4["music_meta"][PATH_V2]["fp_id"] = "fp2-different0000"
        eng4._cue_bound_cache = None
        ids2 = eng4._cue_bound_song_ids()
        print("      同名+艺人+时长（fp_id/md5 都不同）→ %r" % (sorted(ids2),))
        chk(SID_V2 in ids2, "只有同名+艺人+时长（差 0 秒）⇒ 也进集合", SID_V2)
        # 时长差 >2 秒 ⇒ 不算同一首
        cfg4["music_meta"][PATH_V2]["grid"]["dur"] = 205.0
        eng4._cue_bound_cache = None
        chk(SID_V2 not in eng4._cue_bound_song_ids(),
            "时长差 5 秒 ⇒ **不算**同一首（第 ④ 级要求 ≤2s）", eng4._cue_bound_song_ids())
        # ④ 合成 id（包里带来的指纹）
        tr_s = cuesheet.TrackEntry(identity=cuesheet.Identity(
            title="SongS", artist="ArtistS", duration_sec=200.0, bpm=BPM))
        tr_s.identity.fp_id = "fp2-synth0000000000"
        eng4._cue_track = tr_s
        eng4._cue_bound_cache = None
        ids3 = eng4._cue_bound_song_ids()
        print("      包内指纹（合成 id）场景 → %r" % (sorted(ids3),))
        chk(SYNTH in ids3, "fp_songs 里的合成 id ⇒ 进集合", SYNTH)
        chk(eng4._cue_bound_song_id() == SYNTH, "主 id = 合成 id（护栏的锚点）",
            eng4._cue_bound_song_id())

        # ================= ⑤ _cue_accept_pos =================
        print("\n⑤ _cue_accept_pos：集合内照跟、集合外拒绝、空集不启用")
        # ④ 里为了验"时长差 >2s 不算同一首"把版本 B 的时长改成了 205 ⇒ 这里改回来
        cfg4["music_meta"][PATH_V2]["grid"]["dur"] = 200.0
        eng4._cue_track = tr_a
        eng4._cue_bound_cache = None
        chk(eng4._cue_accept_pos(SID_A, 12.5) == 12.5, "集合内（主 id）⇒ 照跟")
        chk(eng4._cue_accept_pos(SID_V2, 12.5) == 12.5,
            "集合内（**同一首音乐的另一版**）⇒ 也照跟（这正是本轮要修的）")
        chk(eng4._cue_accept_pos(SID_OTHER, 12.5) is None,
            "**集合外（另一首歌）⇒ 仍拒绝**（反向能力不能丢）", SID_OTHER)
        eng5 = new_engine(cfg4, [lay], img)
        eng5._cue_track = cuesheet.TrackEntry(identity=cuesheet.Identity(
            title="无人认识的歌", artist="Nobody", duration_sec=1.0))   # 什么都对不上
        eng5._cue_bound_cache = None
        chk(eng5._cue_bound_song_ids() == set(), "对不上任何东西 ⇒ 集合为空",
            eng5._cue_bound_song_ids())
        chk(eng5._cue_accept_pos(999999, 3.0) == 3.0,
            "集合为空 ⇒ **不启用护栏**（照跟，绝不变成「永远不跟」）")

        # ================= ⑥ 真窗口：不弹 + 截图 =================
        print("\n⑥ 真 QDialog：导入不弹窗 + 状态行截图")
        dlg6 = cue_ui.CueSheetDialog(Owner(cfg, new_engine(cfg, [lay], img)))
        dlg6.resize(920, 560)
        cue_ui.pick_track = _counting_pick
        picks["n"] = 0
        try:
            ok6 = dlg6.import_one(pkg2)
        finally:
            cue_ui.pick_track = real_pick
        chk(picks["n"] == 0 and ok6 is False, "真对话框导入多曲目包：零弹窗、不接管",
            (picks["n"], ok6))
        os.makedirs(SHOT_DIR, exist_ok=True)
        shot = os.path.join(SHOT_DIR, "cue_track_pick.png")
        ok_s = dlg6.grab().save(shot, "PNG")
        chk(ok_s, "状态行截图已落盘 cue_track_pick.png", shot)
        print("      状态行：%s" % dlg6.lbl_status.text()[:110])

        # ================= ⑦ 真实包：同曲另一版本 的护栏集合 =================
        print("\n⑦ 真实包 `码表素材\\测试.ercue`：曲库里是「同一首音乐的另一个版本」时可接受集合是什么")
        if not REAL_PKG:
            print("      （真实包不在，跳过这段）")
        else:
            sheet_r, _info = cuesheet.load(REAL_PKG, meta_only=True)
            print("      真实包：%s（%.0f MB，%d 首）"
                  % (os.path.basename(REAL_PKG),
                     os.path.getsize(REAL_PKG) / 1048576.0, len(sheet_r.tracks)))
            for te in sheet_r.tracks:
                idn = te.identity
                # 模拟：曲库里是**另一个版本**（fp_id 不同、md5 不同，但名/艺人/时长一致）
                p_v1 = os.path.join(media, "v1_%s.flac" % idn.fp_id[-6:])
                p_v2 = os.path.join(media, "v2_%s.mp3" % idn.fp_id[-6:])
                for p in (p_v1, p_v2):
                    open(p, "wb").close()
                cfgr = FakeCfg({
                    "music_library": [p_v1, p_v2],
                    "music_meta": {
                        # 版本 A：**同一段音频**（fp_id 相同 ⇒ ① 级命中）
                        p_v1: {"title": idn.title, "artist": idn.artist,
                               "duration": float(idn.duration_sec),
                               "fp_id": idn.fp_id, "file_md5": idn.file_md5,
                               "grid": {"dur": float(idn.duration_sec)}},
                        # 版本 B：另一种编码（fp_id/md5 都不同 ⇒ 只能靠 ③/④ 级）
                        p_v2: {"title": idn.title, "artist": idn.artist,
                               "duration": float(idn.duration_sec),
                               "fp_id": "fp2-otherversion00",
                               "file_md5": "c" * 32,
                               "grid": {"dur": float(idn.duration_sec)}},
                    },
                    "cue": {},
                })
                engr = new_engine(cfgr, [lay], img)
                engr._cue_track = te
                ids_r = engr._cue_bound_song_ids()
                print("        %-22s fp_id=%s" % ((idn.title or "?")[:22], idn.fp_id))
                print("          → 可接受 song_id 集合 = %r（主 id=%r）"
                      % (sorted(ids_r), engr._cue_bound_song_id()))
                chk(song_id(p_v1) in ids_r and song_id(p_v2) in ids_r,
                    "「%s」两个版本都在可接受集合里（本机 A + 另一编码 B）" % (idn.title or "?"),
                    sorted(ids_r))
                chk(engr._cue_accept_pos(song_id(p_v2), 5.0) == 5.0,
                    "「%s」放另一个版本时护栏放行（改前会被拦掉）" % (idn.title or "?"))
                chk(engr._cue_accept_pos(12345, 5.0) is None,
                    "「%s」放**别的歌**时仍拦掉（护栏没被放宽成摆设）" % (idn.title or "?"))
    finally:
        engine.make_player = real_make
        cue_ui.cue_fp_pack.start_ingest = real_ingest
        cue_ui.QFileDialog = real_fd

    print("\n" + "=" * 78)
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    shutil.rmtree(tmp, ignore_errors=True)
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("CUE_TRACK_PICK_FAIL")
        return 1
    print("CUE_TRACK_PICK_OK  认歌选曲目 / 导入不弹窗 / 同曲多版本护栏 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
