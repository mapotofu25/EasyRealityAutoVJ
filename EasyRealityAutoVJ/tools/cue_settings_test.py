# -*- coding: utf-8 -*-
"""Step7 自检：配置（定位档位 + 清空最近）与 i18n。

判据（团队给定）：
  1. **改语言后对话框文案跟着变**（真窗口截图 `tools/_cueshots/step7_en.png`）
  2. **`seek_profile` 改动能生效**：设 `conservative` ⇒ **seek 次数明显更少**（数次数）

运行：`python tools/cue_settings_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QImage                                    # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

import cuesheet                                                     # noqa: E402
import cue_index                                                    # noqa: E402
import cue_position                                                 # noqa: E402
import cue_ui                                                       # noqa: E402
import engine                                                       # noqa: E402
import i18n                                                         # noqa: E402
from cue_seek_policy import CueSeekPolicy                            # noqa: E402

W, H, BPM, BL = 320, 180, 120.0, 0.5


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


class SlowPlayer:
    """位置**故意跟不上**（每秒只前进 0.2s）⇒ 漂移持续增长，用来数 seek 次数。"""

    def __init__(self):
        self._pos = 0.0
        self.seeks = []
        self.speed = 1.0
        self._active = False

    def current(self):
        return None

    def set_active(self, a):
        self._active = bool(a)

    def warm_up(self):
        pass

    def set_max_size(self, w, h):
        pass

    def set_speed(self, s):
        self.speed = float(s)

    def position(self):
        return self._pos

    def seek(self, sec):
        self.seeks.append(float(sec))
        self._pos = float(sec)
        return True

    def close(self):
        pass

    def crawl(self, dt):
        self._pos += 0.2 * dt


def new_engine(cfg, lay):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    # ⚠ `_cue_get_player` 要读 `self.canvas`（解码缩放上限），缺了会抛异常被 `_cue_frame` 吞掉
    #   ⇒ 表现为"一次 seek 都没有"（第一版就踩了，加了 canvas 才对）。
    eng.canvas = QImage(W, H, QImage.Format_ARGB32)
    eng.cfg = cfg
    eng.layers = [lay]
    eng._cue_index = None
    eng._cue_policy = CueSeekPolicy(cfg={})
    eng._cue_sheet = None
    eng._cue_track = None
    eng._cue_media = {}
    eng._cue_rt = None
    eng._cue_pos_beats = None
    eng._cue_pos_src = ""
    eng._health_log = None
    eng._cue_err_at = 0.0
    eng._cue_tick_fail = 0
    return eng


def run_seeks(profile, seconds=12.0, fps=30.0):
    """按 `profile` 跑一段"位置领先、播放器落后"的模拟，返回 seek 次数。"""
    tmp = tempfile.mkdtemp(prefix="cueprof_")
    media = os.path.join(tmp, "media")
    os.makedirs(media, exist_ok=True)
    open(os.path.join(media, "a.mp4"), "wb").close()
    sheet = cuesheet.CueSheet(title="档位表", res_w=W, res_h=H)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title="S", artist="A", duration_sec=200.0, bpm=BPM)
    te.identity.ensure_meta_id()
    L = cuesheet.CompLayer(lyr_id="L1", z=0)
    L.items.append(cuesheet.CueItem(item_id="i1", clip_id="c1", at_beats=0.0,
                                    src_in_sec=0.0, src_out_sec=120.0,
                                    trans_in="cut", trans_out="cut").normalized())
    te.layers.append(L)
    sheet.tracks.append(te)
    sheet.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))

    cfg = FakeCfg()
    cue_index.cue_cfg(cfg)["seek_profile"] = profile
    lay = engine.Layer("码表层")
    lay.is_cuesheet = True
    eng = new_engine(cfg, lay)
    pl = SlowPlayer()
    eng._cue_policy.cfg["seek_profile"] = profile
    # ★ 用**模拟时钟**喂 policy：否则 12s 的模拟会在一瞬间跑完，而 policy 的节流用的是
    #   `time.monotonic`（真实 1s）⇒ 只有入场那次 seek（第一版就是这么误判的）。
    sim = [0.0]
    eng._cue_policy._now = lambda: sim[0]
    eng.set_cue_sheet(sheet, media_paths={"c1": os.path.join(media, "a.mp4")},
                      track=te, layer=lay, sheet_id="t.ercue")
    real_make = engine.make_player
    engine.make_player = lambda item, cue=False: pl
    try:
        dt = 1.0 / fps
        n = int(seconds * fps)
        t = 0.0
        for _ in range(n):
            t += dt
            sim[0] = t
            eng.set_cue_position(t / BL, "test")     # 码表按真实时间前进
            eng._cuesheet_tick()
            ps = lay.cs_states[0] if lay.cs_states else None
            if ps is not None:
                eng._cue_frame(lay, ps)              # 走真定位路径（policy 决定是否 seek）
            pl.crawl(dt)
            time.sleep(0)
    finally:
        engine.make_player = real_make
    return len(pl.seeks), pl.seeks[:6]


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    os.environ.pop("QT_QPA_PLATFORM", None)      # 真实平台（中文/英文都要能渲染）
    app = QApplication.instance() or QApplication(sys.argv)

    # ---------------- ② seek_profile 生效（先测，不依赖语言） ----------------
    print("② 定位档位：conservative 的 seek 次数应明显少于 auto")
    n_auto, s_auto = run_seeks("auto", seconds=12.0)
    n_cons, s_cons = run_seeks("conservative", seconds=12.0)
    n_off, s_off = run_seeks("off", seconds=12.0)
    print("      auto         = %d 次（前几个 %s）" % (n_auto, [round(x, 1) for x in s_auto]))
    print("      conservative = %d 次（前几个 %s）" % (n_cons, [round(x, 1) for x in s_cons]))
    print("      off          = %d 次" % n_off)
    check("conservative 的 seek 次数 ≤ 2（只入场/预热）", n_cons <= 2, n_cons)
    check("auto 明显更多（auto > conservative）", n_auto > n_cons, (n_auto, n_cons))
    check("off 完全不 seek", n_off == 0, n_off)

    # ---------------- ① i18n：切语言后文案跟着变 ----------------
    print("\n① i18n：切到英文后对话框文案跟着变")
    cfg = FakeCfg()
    cue_index.cue_cfg(cfg)
    lay = engine.Layer("码表层")
    lay.is_cuesheet = True
    eng = new_engine(cfg, lay)
    owner = type("O", (), {})()
    owner.cfg = cfg
    owner.engine = eng
    owner.layers_panel = None

    i18n.set_lang("zh")
    dlg_zh = cue_ui.CueSheetDialog(owner)
    zh_texts = (dlg_zh.windowTitle(), dlg_zh.btn_import.text(), dlg_zh.btn_media.text(),
                dlg_zh.btn_clock.text())
    print("      中文：%s" % (zh_texts,))
    check("中文模式是中文", "码表" in zh_texts[0] and "导入" in zh_texts[1], zh_texts)

    i18n.set_lang("en")
    dlg_en = cue_ui.CueSheetDialog(owner)
    en_texts = (dlg_en.windowTitle(), dlg_en.btn_import.text(), dlg_en.btn_media.text(),
                dlg_en.btn_clock.text())
    print("      英文：%s" % (en_texts,))
    check("英文模式对话框标题已翻译", "Cue Sheet" in en_texts[0], en_texts[0])
    check("导入按钮已翻译", en_texts[1].startswith("Import"), en_texts[1])
    check("素材目录按钮已翻译", "media folder" in en_texts[2], en_texts[2])
    check("手动起拍按钮已翻译", "Manual beat" in en_texts[3], en_texts[3])
    # 高级小节（定位档位）也要翻译
    check("定位档位下拉项已翻译",
          dlg_en.cmb_profile.itemText(0).startswith("Auto"),
          [dlg_en.cmb_profile.itemText(i) for i in range(3)])

    os.makedirs(SHOT_DIR, exist_ok=True)
    shot = os.path.join(SHOT_DIR, "step7_en.png")
    dlg_en.resize(720, 320)
    dlg_en.show()
    for _ in range(6):
        app.processEvents()
    ok = dlg_en.grab().save(shot, "PNG")
    check("英文截图已落盘 step7_en.png", ok, shot)

    # ---------------- 配置落地 ----------------
    print("\n③ 配置落地：档位/清空最近都写进 cfg 并生效")
    dlg_en.cmb_profile.setCurrentIndex(1)          # conservative
    app.processEvents()
    check("cfg 里 seek_profile = conservative",
          (cue_index.cue_cfg(cfg) or {}).get("seek_profile") == "conservative",
          (cue_index.cue_cfg(cfg) or {}).get("seek_profile"))
    check("policy 立即生效（无需重启）",
          str(eng._cue_policy.cfg.get("seek_profile")) == "conservative",
          eng._cue_policy.cfg)
    cue_index.cue_cfg(cfg)["recent"] = ["P:/x/a.ercue", "P:/x/b.ercue"]
    dlg_en._load_recent()
    dlg_en.on_clear_recent()
    check("清空最近 ⇒ cfg.recent 为空", (cue_index.cue_cfg(cfg) or {}).get("recent") == [],
          (cue_index.cue_cfg(cfg) or {}).get("recent"))

    dlg_zh.close()
    dlg_en.close()
    i18n.set_lang("zh")

    print("")
    if fails:
        print("SETTINGS_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("SETTINGS_OK  档位生效（数次数）/ i18n 跟随 / 配置落地 全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
