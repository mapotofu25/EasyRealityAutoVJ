# -*- coding: utf-8 -*-
"""**同包多曲目换歌重绑**回归（用户 2026-10-17："Nimbus 能识别但是 era 里没有码表编排的画面"）。

现场日志铁证：
    sid=216601071 votes=8 off=31.03 status=ok **pb=None** | sheet=1 **track=揽月** rt=1 items=10
—— 放的是 Nimbus（216601071），引擎却一直用**揽月**的编排（10 个片段），
于是"认错歌护栏"发现 sid 不属于揽月 ⇒ 位置被拦成 None ⇒ **一幅画面都不出**。

根因：`_cue_auto_bind()` 里
    `if want and want == self._cue_auto_sheet: return True`
只比**包路径**。一张 `.ercue` 里有多首歌时，换歌永远不会重绑曲目。

判据：
  A. 同一张包、**换曲目** ⇒ `_cue_track` 必须换、`_cue_rt` 必须按新曲目重建（items 数对得上）；
  B. 同一张包、**同一曲目**再叫一次 ⇒ 必须**原对象不动**（不重建、不抖）；
  C. 重绑**不许关解码器**（走 `_cue_rebind_track`，不是 `set_cue_sheet` 全量重装）。

运行：`python tools/_cue_multi_track_test.py`
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import engine as E                                             # noqa: E402

REAL_CFG = r"C:/Users/Administrator/AppData/Local/AutoVJ/config.json"
PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


class FakeCfg(dict):
    def save(self):
        pass


def main():
    if not os.path.isfile(PKG):
        print("!! 找不到码表包：%s" % PKG)
        return 1
    import cuesheet as CS
    sheet, info = CS.load(PKG)
    if len(sheet.tracks) < 2:
        print("!! 包里只有 %d 首曲目，测不了换歌" % len(sheet.tracks))
        return 1
    t0, t1 = sheet.tracks[0], sheet.tracks[1]
    n0 = sum(len(ly.items) for ly in t0.layers)
    n1 = sum(len(ly.items) for ly in t1.layers)
    print("包 = %s   曲目：%s(%d 片段) / %s(%d 片段)"
          % (os.path.basename(PKG), t0.identity.title, n0, t1.identity.title, n1))

    cfg = FakeCfg(json.load(open(REAL_CFG, encoding="utf-8")))
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    eng.cfg = cfg
    eng.layers = []
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_rt = None
    eng._cue_media = {}
    eng._cue_alpha = {}
    eng._cue_bound_cache = None
    eng._cue_bad_song_n = 0
    eng._cue_bad_song_t = 0.0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sid = -1
    eng._cue_jumped = False
    eng._cue_auto_sheet = ""
    eng._cue_policy = E.CueSeekPolicy(cfg={})
    eng._cue_health = lambda msg: print("    [health]", msg)

    print("A. 先装 track0，再用 track1 的 query 自动装表")
    E.AutoVJEngine.set_cue_sheet(eng, sheet, media_paths=info.get("present_media") or {},
                                 track=t0)
    # 用**索引里的真实路径**（生产就是这么来的；大小写/分隔符必须一致，
    # 否则 `_cue_auto_bind` 会判成"另一张表"而走全量重装）
    hits = E.AutoVJEngine.cue_sheets_for_track(eng, title="Nimbus Spark 1999",
                                               artist="Supa7onyz", duration_sec=165.5)
    AUTO = str((hits[0][0] if hits else {}).get("path")
               or os.path.normpath(PKG).replace("/", "\\"))
    eng._cue_auto_sheet = AUTO
    ck("初始绑定 = %s" % t0.identity.title, eng._cue_track is t0)
    ck("初始 items = %d" % n0, getattr(eng._cue_rt, "n_items", -1) == n0,
       "实际 %r" % (getattr(eng._cue_rt, "n_items", None),))

    q1 = {"title": t1.identity.title, "artist": t1.identity.artist,
          "duration_sec": t1.identity.duration_sec, "fp_id": t1.identity.fp_id,
          "file_name": t1.identity.file_name, "file_md5": t1.identity.file_md5}
    rt0 = eng._cue_rt
    ok = E.AutoVJEngine._cue_auto_bind(eng, "", q1)
    ck("换歌返回 True", ok is True)
    ck("曲目已切到 %s" % t1.identity.title, eng._cue_track is t1,
       "实际 %r" % (getattr(getattr(eng._cue_track, "identity", None), "title", None),))
    ck("运行时按新曲目重建（items = %d）" % n1,
       getattr(eng._cue_rt, "n_items", -1) == n1,
       "实际 %r" % (getattr(eng._cue_rt, "n_items", None),))
    ck("运行时是新对象（不是旧的那份）", eng._cue_rt is not rt0)
    ck("包路径不变（没有重装整包）", eng._cue_auto_sheet == AUTO,
       "期望 %r 实际 %r" % (AUTO, eng._cue_auto_sheet))
    ck("alpha 事实表没被清空", bool(eng._cue_alpha))

    print("B. 同一曲目再叫一次 ⇒ 必须原对象不动")
    rt1 = eng._cue_rt
    ok2 = E.AutoVJEngine._cue_auto_bind(eng, "", q1)
    ck("仍然返回 True", ok2 is True)
    ck("运行时对象没被重建（不抖）", eng._cue_rt is rt1)

    print("C. 重绑不许关解码器")
    src = open(os.path.join(SRC, "engine.py"), encoding="utf-8").read()
    i = src.index("def _cue_rebind_track")
    seg = src[i: src.index("def _cue_auto_bind", i)]
    code = "\n".join(ln.split("#", 1)[0] for ln in seg.splitlines())
    # ⚠ 只看**真实调用**：docstring 里为解释原因会提到这两个方法名（正文说明不算调用）
    ck("_cue_rebind_track 不调 self.clear_cue_sheet",
       "self.clear_cue_sheet(" not in code)
    ck("_cue_rebind_track 不调 self.set_cue_sheet",
       "self.set_cue_sheet(" not in code)
    ck("重建了 CueRuntime", "CueRuntime" in code)
    ck("清了 seek 状态（片段全换了）", "_cue_policy" in code and "reset" in code)

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 同一张码表里换歌会重绑曲目，画面不再被护栏拦掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
