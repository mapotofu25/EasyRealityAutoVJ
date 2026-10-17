# -*- coding: utf-8 -*-
"""**端到端**：本机没有音频文件的歌，靠码表包里带的指纹也能自动装表 + 跟位。

用户原话（这条链要解决的问题）：
    「这首歌在 erc 里做的码表包啊，理论来讲 era 导入就应该要识别到 解决一下」

`tools/cue_fp_pack_test.py` 证的是「包里的指纹能认音频」+「几个 engine 方法各自正确」；
本文件把**整条链**串起来跑一遍（全走正式代码，只有播放器与音频状态是假的）：

    真 `.ercue`（`cuesheet.save` 写、再手工塞进 `fp/<fp_id>.bin`）
      → `cue_fp_pack.ingest_sheet()` 灌进真指纹库 + 写 `cfg["cue"]["fp_songs"]`
      → 真 `AutoVJEngine`（`__new__` 骨架，同 `cue_position_test.py`）
      → 模拟识别结果 `recognized_song_id = 合成 id`
      → `_cue_auto_position()` ⇒ **自动装表**（`_cue_auto_sheet`）+ **跟位**（`pos_beats` / `play_sec`）

判据：
  1. `_cue_song_path(合成 id) == ""`（本机**确实**没有这个文件 —— 前提成立）
  2. 包里的指纹让**自动装表**发生（`_cue_auto_sheet` == 这张表）
  3. 3 个采样点 `pos_beats == offset/beat_len`（±0.5 拍）、`play_sec ≈ offset`（±0.5s）
  4. **认错歌护栏**：认到另一个 song_id ⇒ 本帧不接管（`pb is None`），恢复后继续跟
  5. **反向复现**：把 `cfg["cue"]["fp_songs"]` 拿掉（等价于"没导入过这张包"）⇒
     同样输入**不会**自动装表 —— 证明 ⓪ 级（包里带来的指纹）真的在起作用

运行：`venv\\Scripts\\python.exe tools/cue_fp_synth_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import types
import zipfile
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import numpy as np                                                   # noqa: E402
from PySide6.QtGui import QColor, QImage                             # noqa: E402
from PySide6.QtWidgets import QApplication                           # noqa: E402

import cue_fp_pack                                                   # noqa: E402
import cue_index                                                     # noqa: E402
import cue_position                                                  # noqa: E402
import cuesheet                                                      # noqa: E402
import engine                                                        # noqa: E402
import fp as fpvj                                                    # noqa: E402
from cue_seek_policy import CueSeekPolicy                            # noqa: E402

W = H = 180
BPM = 120.0
BEAT_LEN = 60.0 / BPM           # 0.5 秒/拍 ⇒ pos_beats = offset / 0.5
OK, FAIL = [], []


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                          ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


# ==========================================================================
# 最小替身（播放器 / 音频状态 / 配置）
# ==========================================================================
class FakeState:
    """`audio_engine.state` 里引擎会读的那几个识别字段。"""

    def __init__(self):
        self.recognized_song_id = -1
        self.recognized_offset = -1.0
        self.recognized_votes = 0
        self.recognized_rate = 1.0
        self.recognized_t = 0.0


class FakePlayer:
    """不碰解码器的播放器（与 `cue_position_test.py` 同款）。"""

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

    def seek(self, s):
        return True

    def close(self):
        pass


class FakeCfg:
    def __init__(self):
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


def new_engine(cfg, layer, img):
    """真引擎骨架（`__new__` + 只补 `_cue_auto_position` 用到的那几个字段）。"""
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
    eng.layers = [layer]
    eng.audio = types.SimpleNamespace(state=FakeState())
    eng._cue_pos_tracker = cue_position.CuePositionTracker()
    eng._cue_pos_next_t = 0.0
    eng._cue_pos_song = -1
    eng._cue_auto_sheet = ""
    eng._cue_manual_clock = False
    eng._images = {}
    return eng


def step(eng, layer, sid, off):
    """模拟"识别到 sid、偏移 off"这一拍，返回 (status, pos_beats, play_sec)。"""
    st = eng.audio.state
    st.recognized_song_id = sid
    st.recognized_offset = float(off)
    st.recognized_votes = 5
    st.recognized_rate = 1.0
    eng._cue_pos_next_t = 0.0                 # 绕过 0.2s 门控
    status, pb = eng._cue_auto_position()
    eng._cuesheet_tick()                      # 出画（cs_states[].play_sec）
    sec = layer.cs_states[0].play_sec if getattr(layer, "cs_states", None) else None
    return status, pb, sec


def build_pkg(pkg, fp_id, hashes, media_name="a.mp4"):
    """真 `cuesheet.save()` 写包，再按契约手工塞进 `fp/<fp_id>.bin`（不经 cue_fp_pack）。"""
    s = cuesheet.CueSheet(title="合成表", res_w=W, res_h=H)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title="SongX", artist="ArtistX", duration_sec=40.0,
                                    bpm=BPM, file_name="a.flac")
    te.identity.fp_id = fp_id
    te.identity.ensure_meta_id()
    lay = cuesheet.CompLayer(lyr_id="L1", z=0)
    lay.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=0.0,
                                      src_in_sec=0.0, src_out_sec=60.0,
                                      trans_in="cut", trans_out="cut").normalized())
    te.layers.append(lay)
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name=media_name))
    cuesheet.save(s, pkg, app_version="2026.10.18.01")
    body = b"".join(bytes.fromhex(h) + struct.pack("<i", int(o)) for h, o in hashes)
    head = b"ERFP" + bytes([1, 20]) + struct.pack("<H", 0) + struct.pack("<I", len(hashes))
    with zipfile.ZipFile(pkg, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("fp/%s.bin" % fp_id, zlib.compress(head + body, 6))
    return pkg


def main():
    app = QApplication.instance() or QApplication(sys.argv)          # noqa: F841
    tmp = tempfile.mkdtemp(prefix="cuefpsynth_")
    media = os.path.join(tmp, "media")
    os.makedirs(media, exist_ok=True)
    open(os.path.join(media, "a.mp4"), "wb").close()

    # ---- 「音频」：合成一段（正弦+噪声），算真指纹（生产口径，不是假数据）----
    rnd = np.random.default_rng(3)
    sr = 44100
    sig = (rnd.standard_normal(sr * 40) * 0.05).astype(np.float32)
    t3 = np.arange(int(sr * 3)) / sr
    sig[int(sr * 3):int(sr * 6)] += (0.4 * np.sin(2 * np.pi * 440.0 * t3)).astype(np.float32)
    hashes = fpvj.fingerprint_audio(sig, sr)
    fp_id = fpvj.signature_from_hashes(hashes)
    synth = cue_fp_pack.fp_song_id(fp_id)
    print("合成素材：%d 条哈希 / fp_id=%s / 合成 song_id=%d" % (len(hashes), fp_id, synth))
    if len(hashes) < 1000:
        chk(False, "合成素材哈希太少，测不出东西", len(hashes))
        return 1

    pkg = build_pkg(os.path.join(tmp, "S.ercue"), fp_id, hashes)
    cfg = FakeCfg()
    cue_index.cue_cfg(cfg)["media_dir"] = media
    db = fpvj.FingerprintDB(os.path.join(tmp, "fp.db"))

    # ================= ① 灌库 =================
    print("\n① 包里的指纹灌进指纹库")
    r = cue_fp_pack.ingest_sheet(cfg, db, pkg)
    print("      ingest_sheet → %r" % (r,))
    chk(int(r.get("tracks", 0)) == 1 and int(r.get("hashes", 0)) == len(hashes),
        "1 首入库 / 全部条数入库", r)
    chk((cfg["cue"].get("fp_songs") or {}).get(fp_id, {}).get("song_id") == synth,
        "cfg[\"cue\"][\"fp_songs\"] 有这条映射")

    # ================= ② 前提：本机真的没有这个文件 =================
    print("\n② 前提：这个 song_id 在本机曲库里查不到路径")
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor(200, 40, 40, 255))
    engine.make_player = lambda item, cue=False: FakePlayer(img)
    layer = engine.Layer("码表层")
    layer.is_cuesheet = True
    eng = new_engine(cfg, layer, img)
    idx = eng.cue_index()
    idx.add(pkg)
    idx.save_cfg(cfg)
    chk(eng._cue_song_path(synth) == "", "_cue_song_path(合成 id) == \"\"（没有文件）",
        repr(eng._cue_song_path(synth)))
    q = eng._cue_track_query_by_sid(synth)
    chk(q.get("fp_id") == fp_id and q.get("file_name") == "",
        "_cue_track_query_by_sid 能反查出认歌参数（file_name 为空）", q)

    # ================= ③ 自动装表 + 跟位 =================
    print("\n③ 模拟识别到合成 id ⇒ 自动装表 + 跟位")
    samples = []
    for i, off in enumerate((10.0, 12.0, 14.0)):
        status, pb, sec = step(eng, layer, synth, off)
        samples.append((off, status, pb, sec))
        print("      offset=%.1fs → status=%s pos_beats=%r play_sec=%r"
              % (off, status, pb, sec))
        time.sleep(0.02)
    chk(eng._cue_sheet is not None and "S.ercue" in (eng._cue_auto_sheet or ""),
        "自动装上了这张表（_cue_auto_sheet）", eng._cue_auto_sheet)
    chk(eng._cue_bound_song_id() == synth, "绑定曲目 == 合成 id（护栏的判据）",
        eng._cue_bound_song_id())
    chk(all(pb is not None and abs(pb - off / BEAT_LEN) <= 0.5
            for off, _s, pb, _sec in samples),
        "3 个采样点 pos_beats == offset/beat_len（±0.5 拍）", samples)
    chk(all(sec is not None and abs(sec - off) <= 0.5 for off, _s, _pb, sec in samples),
        "画面随之出画 play_sec ≈ offset（±0.5s）", samples)
    chk(eng._cue_pos_src == "fp", "位置来源标记 = fp", eng._cue_pos_src)

    # ================= ④ 认错歌护栏 =================
    print("\n④ 认到另一首歌 ⇒ 本帧不接管；恢复后继续跟")
    status, pb, _sec = step(eng, layer, synth + 1, 20.0)
    chk(pb is None, "认到别的 song_id ⇒ pos_beats 不接管（pb is None）",
        "status=%s pb=%r" % (status, pb))
    status, pb, sec = step(eng, layer, synth, 22.0)
    chk(pb is not None and abs(pb - 22.0 / BEAT_LEN) <= 0.5,
        "回到本表那首 ⇒ 立刻恢复跟位", "pb=%r" % (pb,))
    chk(sec is not None and abs(sec - 22.0) <= 0.5, "恢复后画面也对", sec)

    # ================= ⑤ 反向复现：没有 fp_songs 就不会装表 =================
    print("\n⑤ 反向复现：拿掉 cfg[\"cue\"][\"fp_songs\"]（= 从没导入过这张包）")
    cfg2 = FakeCfg()
    cue_index.cue_cfg(cfg2)["media_dir"] = media
    layer2 = engine.Layer("码表层2")
    layer2.is_cuesheet = True
    eng2 = new_engine(cfg2, layer2, img)
    idx2 = eng2.cue_index()
    idx2.add(pkg)
    idx2.save_cfg(cfg2)
    chk(eng2._cue_track_query_by_sid(synth) == {},
        "反查为空（引擎不知道这个合成 id）")
    status, pb, sec = step(eng2, layer2, synth, 10.0)
    print("      status=%s pos_beats=%r play_sec=%r 自动装表=%r"
          % (status, pb, sec, eng2._cue_auto_sheet))
    chk(eng2._cue_sheet is None and not (eng2._cue_auto_sheet or ""),
        "**不会**自动装表（对照：证明 ⓪ 级真的在起作用）", eng2._cue_auto_sheet)
    chk(sec is None, "没有表 ⇒ 不出画（play_sec is None）", sec)

    db.close()
    print("\n" + "=" * 78)
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("CUE_FP_SYNTH_FAIL")
        return 1
    print("CUE_FP_SYNTH_OK  包内指纹 → 指纹库 → 自动装表 → 跟位（本机无文件）全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
