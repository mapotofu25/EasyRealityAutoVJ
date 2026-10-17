# -*- coding: utf-8 -*-
"""拍长一致性 + 认错歌护栏（2026-10-17 用户追问后的复盘回归）。

## 这条链的真相（别被"BPM 必须填准"误导）

BPM 在整条链上是**纯单位换算**，两端用同一个常数就完全抵消：

    编排端（模型坐标=秒）  at_beats = at_sec * bpm/60        （导出时算）
    演出端（算位置）       pos_beats = offset_sec / beat_len
    演出端（选片段）       (pos - at_beats) * beat_len = 秒差

⇒ BPM 填错、留 0、甚至一首歌里有多段不同 BPM，**都不会**让画面对不上
  （位置来自指纹算出的**原曲秒数**，不是靠拍钟累加推的）。

真正会坏的只有一件：**算位置用的 beat_len ≠ 选片段用的 beat_len**。
旧实现正是这样：位置走 `_cue_beat_len_for()` 四级回退（会退到曲库网格 149.99），
片段走 `CueRuntime.beat_len`（码表 bpm；0 ⇒ 1.0）⇒ bpm=0 的码表位置被放大 2.5 倍。

本测试钉住三件事：
  ① `_cue_pos_beat_len()` 必须与 `CueRuntime.beat_len` **恒等**；
  ② bpm=0 的码表，第 150 秒必须落在 "at_beats=100" 那个片段（而不是被放大到 375）；
  ③ 认到**另一首歌**时，码表不接管位置（`_cue_accept_pos` 返回 None）。

反向复现：把 ① 改回 `_cue_beat_len_for()`，② 必须挂（本脚本会在 `[FAIL]` 里指出来）。
"""
from __future__ import annotations

import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cuesheet as cs
import cuesheet_runtime as cr

OK, FAIL = [], []


def chk(cond, desc, detail=""):
    (OK if cond else FAIL).append(desc)
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", desc,
                           ("  —— " + str(detail)) if detail else ""))
    return bool(cond)


class FakeCfg:
    """`Config` 是包装对象（**没有 .get()**）⇒ 必须用 `__getitem__` 模拟。"""

    def __init__(self, d):
        self._d = d

    def __getitem__(self, k):
        return self._d[k]


def build(bpm, at_beats_list):
    """造一张单曲目码表：bpm 可为 0（退化态）。"""
    tr = cs.TrackEntry(identity=cs.Identity(title="T", artist="A", duration_sec=300.0,
                                            bpm=float(bpm), file_name="t.flac"))
    items = [cs.CueItem(item_id="i%d" % i, clip_id="c1", at_beats=float(b),
                        trans_in_beats=1.0, trans_out_beats=1.0)
             for i, b in enumerate(at_beats_list)]
    tr.layers = [cs.CompLayer(lyr_id="L1", name="L1", z=0, items=items)]
    sh = cs.CueSheet(title="t")
    sh.tracks = [tr]
    return sh, tr


def fake_engine(sh, tr, meta, lib):
    """最小假引擎：只带被测方法需要的字段。

    ⚠ 不能 `object.__new__(AutoVJEngine)` —— 它是 QObject 子类，Python 会拒绝
      （`TypeError: not safe, use __new__()`）。而真实例化又需要 QApplication。
      所以这里**把要测的方法原样挂到一个普通类上**（函数就是引擎里那一份，不是复制品），
      这样测的仍然是正式代码。
    """
    import engine as eng_mod
    E = type("FakeEngine", (), {
        "_cue_pos_beat_len": eng_mod.AutoVJEngine._cue_pos_beat_len,
        "_cue_bound_song_id": eng_mod.AutoVJEngine._cue_bound_song_id,
        "_cue_lookup_song_id": eng_mod.AutoVJEngine._cue_lookup_song_id,
        "_cue_accept_pos": eng_mod.AutoVJEngine._cue_accept_pos,
        "_cue_bad_song_log": eng_mod.AutoVJEngine._cue_bad_song_log,
        "_cue_beat_len_for": eng_mod.AutoVJEngine._cue_beat_len_for,
        "_cue_song_id_of": staticmethod(eng_mod.AutoVJEngine._cue_song_id_of),
    })
    e = E()
    e.cfg = FakeCfg({"music_meta": meta, "music_library": lib, "cue": {}})
    e._cue_sheet = sh
    e._cue_track = tr
    e._cue_rt = cr.CueRuntime(sh, tr, media_paths={"c1": "/tmp/x.mov"})
    e._cue_bound_cache = None
    e._cue_bad_song_n = 0
    e._cue_bad_song_t = 0.0
    return e


def main():
    LIB = ["W:/lib/t.flac"]
    SID = int(hashlib.md5(LIB[0].encode("utf-8")).hexdigest()[:8], 16)

    # ================= ① 拍长同源 =================
    print("\n===== ① 拍长必须与 CueRuntime.beat_len 同源 =====")
    sh, tr = build(0.0, [100.0, 200.0])
    # 曲库里这首歌是有网格 bpm 的（149.99）—— 旧实现会把它当成拍长
    meta = {LIB[0]: {"fp_id": "fp2-0123456789abcdef", "file_md5": "a" * 32,
                     "grid": {"bpm": 149.99, "dur": 300.0},
                     "title": "T", "artist": "A"}}
    tr.identity.fp_id = "fp2-0123456789abcdef"
    tr.identity.file_md5 = "a" * 32
    tr.identity.file_name = "t.flac"
    e = fake_engine(sh, tr, meta, LIB)

    want = float(e._cue_rt.beat_len)
    got = float(e._cue_pos_beat_len())
    chk(abs(got - want) < 1e-12, "bpm=0 码表：_cue_pos_beat_len == _cue_rt.beat_len",
        "%r vs %r" % (got, want))
    chk(abs(got - 1.0) < 1e-12, "bpm=0 ⇒ 退化 1 拍 1 秒（不是曲库的 0.40）", got)

    # 旧实现（回退到曲库网格）会是多少？—— 只是为了把 bug 量化出来
    bl_old = float(e._cue_beat_len_for(LIB[0]))
    chk(abs(bl_old - 1.0) > 0.1, "（对照）旧口径确实会退到曲库网格 bpm",
        "bl_old=%.4f ⇒ %d 倍缩放" % (bl_old, round(1.0 / bl_old) if bl_old else 0))

    # ================= ② 第 150 秒落在哪个片段 =================
    print("\n===== ② bpm=0 的码表：第 150 秒必须落在 at_beats=100 的片段 =====")
    pos_new = 150.0 / e._cue_pos_beat_len()
    st_new = e._cue_rt.update(pos_new)[0]
    chk(st_new.item_id == "i0", "正确口径下选中 i0（at_beats=100）", st_new.item_id)

    pos_old = 150.0 / bl_old if bl_old > 0 else 150.0
    rt_old = cr.CueRuntime(sh, tr, media_paths={"c1": "/tmp/x.mov"})
    st_old = rt_old.update(pos_old)[0]
    chk(st_old.item_id != "i0", "旧口径下会错选到 i1（证明 bug 真实存在）", st_old.item_id)
    print("      旧口径：pos=%.1f 拍 ⇒ 选中 %s（应为 i0）" % (pos_old, st_old.item_id))

    # 有正常 bpm 时两种口径应当一致（不能把正常码表改坏）
    sh2, tr2 = build(150.0, [100.0, 200.0])
    tr2.identity.fp_id = ""
    tr2.identity.file_md5 = ""
    e2 = fake_engine(sh2, tr2, {LIB[0]: {"grid": {"bpm": 150.0, "dur": 300.0}}}, LIB)
    chk(abs(e2._cue_pos_beat_len() - e2._cue_rt.beat_len) < 1e-12,
        "bpm=150 码表：两者仍恒等", e2._cue_pos_beat_len())
    p = 60.0 * 150.0 / 60.0         # 第 60 秒 → 150 拍（落在 100..200 之间）
    chk(e2._cue_rt.update(p)[0].item_id == "i0", "bpm=150 码表：第 60 秒落在 i0", p)
    p2 = 90.0 * 150.0 / 60.0        # 第 90 秒 → 225 拍（已过 200）
    chk(e2._cue_rt.update(p2)[0].item_id == "i1", "bpm=150 码表：第 90 秒落在 i1", p2)

    # ================= ③ 认错歌护栏 =================
    print("\n===== ③ 认到另一首歌 ⇒ 码表不跟位 =====")
    e3 = fake_engine(*build(150.0, [100.0, 200.0]), meta=meta, lib=LIB)
    e3._cue_track = tr
    e3._cue_track.identity.fp_id = "fp2-0123456789abcdef"
    e3._cue_track.identity.file_md5 = "a" * 32
    e3._cue_track.identity.file_name = "t.flac"
    bound = e3._cue_bound_song_id()
    chk(bound == SID, "反查绑定 song_id（走 fp_id）", "%s vs %s" % (bound, SID))
    chk(e3._cue_accept_pos(SID, 12.5) == 12.5, "同一首歌 ⇒ 照跟")
    chk(e3._cue_accept_pos(SID + 1, 12.5) is None, "另一首歌 ⇒ 本帧不接管（返回 None）")
    chk(e3._cue_bad_song_n > 0, "护栏计数已累加（供限流日志）", e3._cue_bad_song_n)

    # 取不到绑定关系 ⇒ 不启用护栏（绝不能变成"永远不跟"）
    sh4, tr4 = build(150.0, [100.0])
    tr4.identity.fp_id = ""
    tr4.identity.file_md5 = ""
    tr4.identity.file_name = "nonexistent-name.flac"
    e4 = fake_engine(sh4, tr4, {}, LIB)
    chk(e4._cue_bound_song_id() is None, "无 fp_id/md5 且文件名不匹配 ⇒ 绑定为 None")
    chk(e4._cue_accept_pos(SID + 7, 3.0) == 3.0, "绑定未知 ⇒ 不启用护栏（照跟，退回旧行为）")

    # file_md5 / basename 两级也要能反查到
    meta5 = {LIB[0]: {"file_md5": "b" * 32, "grid": {"bpm": 140.0, "dur": 300.0}}}
    sh5, tr5 = build(140.0, [100.0])
    tr5.identity.file_md5 = "b" * 32
    tr5.identity.file_name = "other-name.flac"
    e5 = fake_engine(sh5, tr5, meta5, LIB)
    chk(e5._cue_bound_song_id() == SID, "反查走 file_md5", e5._cue_bound_song_id())
    tr5.identity.file_md5 = ""
    tr5.identity.file_name = "t.flac"
    e6 = fake_engine(sh5, tr5, {}, LIB)
    chk(e6._cue_bound_song_id() == SID, "反查走文件名（老码表兜底）", e6._cue_bound_song_id())

    # 缓存：换表后必须失效（铁律 11）
    e3._cue_track = tr4
    chk(e3._cue_bound_song_id() is None, "换 _cue_track ⇒ 缓存按对象身份自动失效")

    print("\n" + "=" * 74)
    print("通过 %d / 失败 %d" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项：\n  - " + "\n  - ".join(FAIL))
        print("RESULT：CUE_BEATLEN_FAIL")
        return 1
    print("RESULT：CUE_BEATLEN_OK  拍长同源 / 退化态不放大 / 认错歌不跟位 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
