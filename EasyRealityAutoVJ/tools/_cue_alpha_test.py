# -*- coding: utf-8 -*-
"""**码表素材 alpha 接线**回归（真素材 + 真解码器 + 真绘制路径）。

背景（用户 2026-10-17 报的两件事其实是同一个 bug）：
  「我码表做的多层编排怎么没在 era 实现」
  「我的编排结束后淡出时会先黑屏才切出到其他层」

根因：码表包解压出来的素材**没有本地缩略图缓存**，而 `create_video_player()` 判
  "带不带 alpha"的免费依据就是"缩略图是不是 PNG"。没有缩略图 ⇒ 判 False ⇒ 用 OpenCV
  解码 ⇒ **透明通道丢掉，透明区变成不透明黑**。于是上层（LOGO/PNG）把下层整片盖黑：
    · 看起来"只有一层"⇒ 多层编排像没实现；
    · 淡出末尾整块黑 ⇒ 像"先黑屏才切出"。
  而 `.ercue` 里 `media[].has_alpha` 是编排端探测好的**权威结论**，只差把它接到
  `MediaItem._has_alpha` 上（`engine._cue_alpha` → `_cue_item()`）。

判据（三层，缺一不可）：
  A. 表：`set_cue_sheet()` 建出 `_cue_alpha`（路径 → bool），与包内 `media[]` 一致；
  B. 项：`_cue_item()` 把结论写进 `MediaItem._has_alpha`；
  C. 真解码：LOGO 素材必须得到 **ARGB32 / hasAlpha=True / 透明角 alpha==0**。

运行：`python tools/_cue_alpha_test.py`（exit 0 = 全过）。
带 `--pkg` 可换包；默认用用户的「测试.ercue」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                    # noqa: E402

import cuesheet as CS                                          # noqa: E402
import engine as E                                             # noqa: E402
import media_manager as MM                                     # noqa: E402

PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


def load(pkg):
    """**走真实装载 API**（`cuesheet.load`）—— 包括 `present_media` 的解压/路径换算。
    自己拿 zipfile 拼一份会和生产分叉（上一轮就是夹具失真吃过大亏）。"""
    sheet, info = CS.load(pkg)
    return sheet, dict(info.get("present_media") or {})


class _Lay:
    def __init__(self):
        self.is_cuesheet = True
        self._cue_items = {}
        self._cue_players = {}
        self.cache = {}
        self.cs_states = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=PKG)
    a = ap.parse_args()
    app = QApplication.instance() or QApplication([])

    print("包 =", os.path.basename(a.pkg))
    sheet, paths = load(a.pkg)
    if not paths:
        print("!! 包内素材没有解压到 %s.media/，无法测" % a.pkg)
        return 1

    # ---------------- A. 事实表 ----------------
    print("A. set_cue_sheet 建 alpha 事实表")
    eng = E.AutoVJEngine.__new__(E.AutoVJEngine)
    eng.layers = []
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_rt = None
    eng._cue_media = {}
    eng._cue_alpha = {}
    eng._cue_bound_cache = None
    eng._cue_bad_song_n = 0
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._cue_sid = -1
    eng._cue_jumped = False
    eng._cue_policy = E.CueSeekPolicy(cfg={})
    E.AutoVJEngine.set_cue_sheet(eng, sheet, media_paths=paths,
                                 track=sheet.tracks[0] if sheet.tracks else None)
    tbl = eng._cue_alpha
    want = {paths[m.clip_id]: bool(m.has_alpha) for m in sheet.media if m.clip_id in paths}
    ck("事实表条数 = 包内 media 条数（%d）" % len(want), set(tbl) == set(want),
       "实际 %d 条" % len(tbl))
    ck("每条 has_alpha 与包内一致", tbl == want,
       "包内=%s 实际=%s" % (want, tbl))
    n_alpha = sum(1 for v in tbl.values() if v)
    ck("包里确实有带 alpha 的素材（%d 个）" % n_alpha, n_alpha > 0)

    # ---------------- B. 写进 MediaItem ----------------
    print("B. _cue_item 把结论写进 MediaItem._has_alpha")
    lay = _Lay()
    checked = 0
    for pth, al in tbl.items():
        it = E.AutoVJEngine._cue_item(eng, lay, pth, None)
        if getattr(it, "_has_alpha", None) is not al:
            ck("路径 %s ⇒ _has_alpha=%s" % (os.path.basename(pth), al), False,
               "实际 %r" % (getattr(it, "_has_alpha", None),))
            break
        checked += 1
    ck("全部 %d 个素材都写对了" % checked, checked == len(tbl), "只对了 %d" % checked)

    # ---------------- C. 真解码（最关键）----------------
    print("C. 真解码：带 alpha 的素材必须真的保留透明")
    sample = next((p for p, al in tbl.items() if al and MM.MediaItem(p).kind == "video"), None)
    if sample is None:
        print("  （包里没有带 alpha 的**视频**，跳过 C）")
    else:
        it = E.AutoVJEngine._cue_item(eng, lay, sample, None)
        pl = MM.make_player(it, cue=True)
        ck("选到 alpha 播放器（不是 OpenCV 的 VideoPlayer）",
           type(pl).__name__ != "VideoPlayer", "实际 %s" % type(pl).__name__)
        pl.set_active(True)
        try:
            pl.warm_up()
        except Exception:                                      # noqa: BLE001
            pass
        img = None
        t0 = time.time()
        while time.time() - t0 < 6.0:
            time.sleep(0.05)
            img = pl.current()
            if img is not None and not img.isNull():
                break
        if img is None:
            ck("6 秒内出帧", False)
        else:
            ck("帧格式带 alpha 通道（ARGB32）", img.hasAlphaChannel(),
               "实际 %s" % img.format())
            c = img.pixelColor(3, 3)
            ck("左上角是**透明**（alpha==0）—— 旧实现这里是 0,0,0,255 的不透明黑",
               c.alpha() == 0, "实际 rgba=(%d,%d,%d,%d)" % (c.red(), c.green(), c.blue(), c.alpha()))
        try:
            pl.close()
        except Exception:                                      # noqa: BLE001
            pass

    # ---------------- D. 自愈：alpha 结论变了要重开解码器 ----------------
    print("D. _cue_get_player 自愈（结论变 True ⇒ 重开解码器）")
    src = open(os.path.join(SRC, "engine.py"), encoding="utf-8").read()
    seg = src[src.index("def _cue_get_player"):]
    seg = seg[: seg.index("def set_cue_sheet")]
    ck("_cue_get_player 里读 _alpha_recheck", "_alpha_recheck" in seg)
    ck("重开前先 set_active(False) 再 close（9-30 事故防线）",
       seg.index("set_active(False)") < seg.index("close()"))
    ck("记录建播放器时用的 alpha 结论（_cue_alpha_used）", "_cue_alpha_used" in seg)

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 码表素材的 alpha 已按包内元数据正确落地，多层叠加不再互相盖黑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
