# -*- coding: utf-8 -*-
"""S3 数据面自检：`Layer` 字段 / `_CueSubLayer` / `set_cue_sheet` / `clear_cue_sheet`。

**不开窗口、不建真解码器**（`make_player` 换成记录器）⇒ 可以在 CI 里跑。
覆盖团队给的 5 项判据：
  1. 装载 → 卸载 → 再装载 不泄漏（解码器**创建数 == 关闭数**，缓存归零）
  2. `set_cue_sheet` 幂等（同一份调两次不叠两份）
  3. `clear_cue_sheet` 后 `_cue_players` 清空、`lay.player is None`
  4. `_cue_players` 的所有引用点**只在 cue 分支/数据面函数里**（AST 静态断言）
  5. 绘制守卫不受影响 —— 由 `tools/cue_pixel_diff_guard.py` 单独跑（本文件只做静态确认
     `_draw_layer / _compose` 里没有 `_cue_players` 引用）

运行：`python tools/cue_sheet_mount_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QImage                     # noqa: E402

import engine                                        # noqa: E402
import cuesheet                                      # noqa: E402

ENGINE_PY = os.path.join(SRC, "engine.py")

# `_cue_players` 允许出现的函数（其余位置出现即为"越界引用"）
ALLOWED_CUE_PLAYERS_FUNCS = {"__init__", "_cue_get_player", "set_cue_sheet",
                             "clear_cue_sheet", "cuesheet_status"}
# 明确**不许**出现的函数（热路径 / 渲染 / 心跳）
FORBIDDEN_CUE_PLAYERS_FUNCS = {"_get_player", "_compose", "_draw_layer",
                               "_cuesheet_tick", "_preload_clips"}


class FakePlayer:
    """只记"被创建/被关/被激活"的假播放器。"""
    created = []
    closed = 0
    active_calls = []

    def __init__(self, path):
        self.path = path
        self.closed_flag = False
        FakePlayer.created.append(self)

    def set_max_size(self, w, h):
        self.max_size = (w, h)

    def set_active(self, a):
        FakePlayer.active_calls.append((self.path, bool(a)))

    def close(self):
        if not self.closed_flag:
            self.closed_flag = True
            FakePlayer.closed += 1


def _fake_make_player(item, cue=False):
    p = FakePlayer(getattr(item, "path", "?"))
    p.cue_flag = bool(cue)
    return p


def _build_sheet():
    s = cuesheet.new_sheet("S3 自检码表", res_w=1440, res_h=1080)
    te = cuesheet.new_track("T", "A", 300.0, 128.0)
    te.identity.bpm = 128.0
    L1 = cuesheet.CompLayer(lyr_id="L1", name="背景", z=0, blend="screen", opacity=0.9)
    L1.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=8.0,
                                     src_out_sec=4.0,
                                     trans_in="fade", trans_out="fade").normalized())
    L1.items.append(cuesheet.CueItem(item_id="i2", clip_id="c2", at_beats=24.0,
                                     trans_in="cut", trans_out="cut").normalized())
    L2 = cuesheet.CompLayer(lyr_id="L2", name="LOGO", z=10, blend="normal", opacity=0.85)
    L2.items.append(cuesheet.CueItem(item_id="i3", clip_id="c1", at_beats=0.0,
                                     trans_in="cut", trans_out="cut").normalized())
    te.layers += [L1, L2]
    s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    s.media.append(cuesheet.MediaRef(clip_id="c2", name="b.mp4"))
    return s, te


def _new_engine():
    """绕过 `__init__`（它要 audio/threads）：只装数据面用到的东西。"""
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.layers = []
    eng.canvas = QImage(1920, 1080, QImage.Format_ARGB32)
    return eng


class _Item:
    """`_cue_get_player` 只读 `clip.path`。"""

    def __init__(self, path):
        self.path = path


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    # 用假工厂（**不建真解码器**）
    real_make = engine.make_player
    engine.make_player = _fake_make_player
    try:
        sheet, track = _build_sheet()
        eng = _new_engine()
        lay = engine.Layer("码表层")
        lay2 = engine.Layer("普通层")
        eng.layers = [lay, lay2]

        print("① 装载（数据面）")
        ok = eng.set_cue_sheet(sheet, media_paths={"c1": "P:/x/a.mp4",
                                                   "c2": "P:/x/b.mp4"},
                               track=track, layer=lay, sheet_id="S3TEST")
        st = eng.cuesheet_status()
        check("set_cue_sheet 返回 True（建立了运行时）", ok is True, ok)
        check("status.loaded / title", st["loaded"] and st["title"] == "S3 自检码表", st)
        check("status.res == (1440,1080)（画布比例贯通）", st["res"] == (1440, 1080), st["res"])
        check("status.rt_items == 3", st["rt_items"] == 3, st["rt_items"])
        check("status.rt_layers == 2", st["rt_layers"] == 2, st["rt_layers"])
        check("status.track_title == 'T' / bpm == 128", st["track_title"] == "T"
              and abs(st["bpm"] - 128.0) < 1e-9, st)
        check("码表层已标记 is_cuesheet / cuesheet_id",
              lay.is_cuesheet and lay.cuesheet_id == "S3TEST",
              (lay.is_cuesheet, lay.cuesheet_id))
        check("普通层不受影响", lay2.is_cuesheet is False and lay2._cue_players == {})

        print("\n② _cue_get_player：独立缓存 + cue=True 传给工厂")
        p1 = eng._get_player(lay, _Item("P:/x/a.mp4"))
        p2 = eng._get_player(lay, _Item("P:/x/a.mp4"))
        check("同一素材命中缓存（同一个对象）", p1 is p2, (id(p1), id(p2)))
        check("落进 lay._cue_players 而不是 lay.cache",
              "P:/x/a.mp4" in lay._cue_players and "P:/x/a.mp4" not in lay.cache,
              (list(lay._cue_players), list(lay.cache)))
        check("工厂收到 cue=True（DXV 才会走软解）", getattr(p1, "cue_flag", None) is True,
              getattr(p1, "cue_flag", None))
        p3 = eng._get_player(lay, _Item("P:/x/b.mp4"))
        check("不同素材各一份", p3 is not p1 and len(lay._cue_players) == 2,
              len(lay._cue_players))

        print("\n③ 幂等：同一份再 set 一次")
        rt1 = eng._cue_rt
        ok2 = eng.set_cue_sheet(sheet, media_paths={"c1": "P:/x/a.mp4",
                                                    "c2": "P:/x/b.mp4"},
                                track=track, layer=lay, sheet_id="S3TEST")
        st2 = eng.cuesheet_status()
        check("第二次也返回 True", ok2 is True)
        check("_cue_rt 被重建（不是叠一份）", eng._cue_rt is not None and eng._cue_rt is not rt1)
        check("码表图层仍然只有 1 个（没叠层）",
              len([l for l in eng.layers if l.is_cuesheet]) == 1,
              len([l for l in eng.layers if l.is_cuesheet]))
        check("重装时旧解码器已清空（_cue_players == {}）", lay._cue_players == {},
              lay._cue_players)
        check("rt_items 没变成 6", st2["rt_items"] == 3, st2["rt_items"])

        print("\n④ clear_cue_sheet：清空 + 关解码器（先失活再关）")
        lay._cue_players["P:/x/c.mp4"] = _fake_make_player(_Item("P:/x/c.mp4"))
        before_closed = FakePlayer.closed
        n_cache = len(lay._cue_players)
        eng.clear_cue_sheet()
        st3 = eng.cuesheet_status()
        check("_cue_players 清空", lay._cue_players == {}, lay._cue_players)
        check("lay.player is None", lay.player is None, lay.player)
        check("cs_states 清空 / cs_active False",
              lay.cs_states == [] and lay.cs_active is False,
              (lay.cs_states, lay.cs_active))
        check("_cue_items/_cue_last_frame/_cue_seek_state/_cue_sub_tmps 都清空",
              all(getattr(lay, k) == {} for k in ("_cue_items", "_cue_last_frame",
                                                  "_cue_seek_state", "_cue_sub_tmps")),
              [getattr(lay, k) for k in ("_cue_items", "_cue_last_frame",
                                         "_cue_seek_state", "_cue_sub_tmps")])
        check("解码器全部 close()", FakePlayer.closed - before_closed == n_cache,
              "关 %d / 应关 %d" % (FakePlayer.closed - before_closed, n_cache))
        check("关之前先 set_active(False)",
              all(a is False for pth, a in FakePlayer.active_calls
                  if pth == "P:/x/c.mp4"),
              FakePlayer.active_calls)
        check("status 归零", (st3["loaded"] is False and st3["has_runtime"] is False
                             and st3["rt_items"] == 0), st3)
        check("图层仍是码表容器（is_cuesheet 保留）", lay.is_cuesheet is True)
        check("clear 幂等（再调一次不炸）", eng.clear_cue_sheet() is True)

        print("\n⑤ 三轮 装载→卸载：无泄漏（创建数 == 关闭数）")
        FakePlayer.created.clear()
        FakePlayer.closed = 0
        for i in range(3):
            eng.set_cue_sheet(sheet, media_paths={"c1": "P:/x/a.mp4",
                                                   "c2": "P:/x/b.mp4"},
                              track=track, layer=lay, sheet_id="S3TEST")
            for pth in ("P:/x/a.mp4", "P:/x/b.mp4"):
                eng._get_player(lay, _Item(pth))
            eng.clear_cue_sheet()
            check("第 %d 轮后缓存归零" % (i + 1), lay._cue_players == {}, lay._cue_players)
        check("创建 %d == 关闭 %d" % (len(FakePlayer.created), FakePlayer.closed),
              len(FakePlayer.created) == FakePlayer.closed and FakePlayer.closed > 0,
              (len(FakePlayer.created), FakePlayer.closed))
        check("没有仍然存活的假解码器",
              all(p.closed_flag for p in FakePlayer.created),
              [p.path for p in FakePlayer.created if not p.closed_flag])

        print("\n⑥ `cuesheet_wanted()`：只有可见的码表图层才算等位置")
        check("可见 ⇒ True", eng.cuesheet_wanted() is True)
        lay.visible = False
        check("不可见 ⇒ False", eng.cuesheet_wanted() is False)
        lay.visible = True
    finally:
        engine.make_player = real_make

    print("\n⑦ 静态断言：`_cue_players` 的引用点只在 cue 数据面（AST）")
    src = open(ENGINE_PY, encoding="utf-8").read()
    tree = ast.parse(src)
    hits = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "_cue_players":
                    hits.setdefault(node.name, []).append(sub.lineno)
    print("      引用点：%s" % {k: sorted(v) for k, v in sorted(hits.items())})
    bad = [n for n in hits if n in FORBIDDEN_CUE_PLAYERS_FUNCS]
    check("热路径/渲染函数里没有 _cue_players 引用（%s）"
          % "/".join(sorted(FORBIDDEN_CUE_PLAYERS_FUNCS)), not bad, bad)
    unexpected = [n for n in hits if n not in ALLOWED_CUE_PLAYERS_FUNCS]
    check("所有引用点都在允许的函数集合内", not unexpected, unexpected)

    print("")
    if fails:
        print("MOUNT_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("MOUNT_OK  S3 数据面 6 组判据全过（幂等 / 清空 / 解码器计数 / 静态越界检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
