# -*- coding: utf-8 -*-
"""回归：**导入时解压内嵌素材**（`extract_media` 此前从未被调用）。

用**用户那个真实包**（`Y:\\EasyRealityAutoVJ\\测试.ercue`，177MB / 内嵌 4 个素材）实测：
  1. `missing` 从 4 → **0**
  2. `<包路径>.media\\` 出现 **4 个文件**
  3. **幂等**：再导入一次 ⇒ **解压次数 = 0**，文件 mtime 不变
  4. 状态行显示 **素材 4/4**
跑完会留下 `<包>.media/` 目录（这就是修复后的正常产物，不删）。

运行：`python tools/cue_extract_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtGui import QImage                                    # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

import cuesheet                                                     # noqa: E402
import cue_index                                                    # noqa: E402
import cue_ui                                                       # noqa: E402
import engine                                                       # noqa: E402
from cue_seek_policy import CueSeekPolicy                            # noqa: E402

REAL_PKG = r"Y:\EasyRealityAutoVJ\测试.ercue"


class FakeCfg:
    def __init__(self):
        self.data = {"music_library": [], "music_meta": {}, "cue": {},
                     "auto": {"intensity": 1, "transition": None},
                     "mode": {"transition": ""}, "color": {}, "postfx": {}}

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, d=None):
        return self.data.get(k, d)

    def save(self, force=False):
        pass


def new_engine(cfg):
    eng = engine.AutoVJEngine.__new__(engine.AutoVJEngine)
    eng.canvas = QImage(320, 180, QImage.Format_ARGB32)
    eng.cfg = cfg
    eng.layers = []
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


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    if not os.path.isfile(REAL_PKG):
        print("找不到真实包：%s（跳过）" % REAL_PKG)
        return 2

    app = QApplication.instance() or QApplication(sys.argv)

    # ⚠ 这个包里有 **2 首曲目** ⇒ 产品会弹「选择要绑定的曲目」模态框；
    #   无头环境没人点，会一直挂住（第一版就是这么超时的）。
    #   这里桩成"用户选第一首"；文件对话框桩成"取消"。
    class _FDStub:
        @staticmethod
        def getOpenFileNames(*a, **k):
            return ([], "")

        @staticmethod
        def getExistingDirectory(*a, **k):
            return ""

    real_pick = cue_ui.pick_track
    real_fd = cue_ui.QFileDialog
    cue_ui.pick_track = lambda sheet, parent=None, current_title="": (
        sheet.tracks[0] if getattr(sheet, "tracks", None) else None)
    cue_ui.QFileDialog = _FDStub
    cfg = FakeCfg()
    cue_index.cue_cfg(cfg)
    eng = new_engine(cfg)
    lay = engine.Layer("码表层")
    lay.is_cuesheet = True
    eng.layers = [lay]
    owner = type("O", (), {})()
    owner.cfg = cfg
    owner.engine = eng
    owner.layers_panel = None

    dest = REAL_PKG + ".media"
    # ★ 必须从"干净状态"开始，否则第二次跑时解压目录已存在 ⇒ "首次解压"那条会假失败。
    #   （这个目录是本工具自己解出来的产物，删掉不影响原包。）
    import shutil
    shutil.rmtree(dest, ignore_errors=True)
    pkg_size = os.path.getsize(REAL_PKG) / 1048576.0
    print("真实包：%s（%.0f MB）" % (os.path.basename(REAL_PKG), pkg_size))
    print("目标目录：%s" % dest)

    # 解压调用计数（证明幂等）
    calls = {"n": 0}
    real_extract = cuesheet.extract_media

    def _counting_extract(pkg, d):
        calls["n"] += 1
        return real_extract(pkg, d)

    cuesheet.extract_media = _counting_extract
    try:
        dlg = cue_ui.CueSheetDialog(owner)
        t0 = time.perf_counter()
        ok = dlg.import_one(REAL_PKG)
        t_first = time.perf_counter() - t0
        n_sheet_media = len(getattr(dlg.sheet, "media", None) or [])
        print("\n首次导入：%.1fs ｜ 装表=%s ｜ 包内素材 %d 个 ｜ 解析出路径 %d 个 ｜ missing %d"
              % (t_first, ok, n_sheet_media, len(dlg.media_paths), len(dlg.missing)))
        print("  状态行：%s" % dlg.lbl_status.text()[:110])

        # ① missing 4 → 0
        check("missing 归零", len(dlg.missing) == 0, dlg.missing)
        check("4 个素材都有真实路径（media_paths == 4）", len(dlg.media_paths) == 4,
              len(dlg.media_paths))
        check("每条路径都真实存在",
              all(os.path.isfile(p) for p in dlg.media_paths.values()),
              [p for p in dlg.media_paths.values() if not os.path.isfile(p)])
        # ② 目标目录出现 4 个文件
        files = sorted(os.listdir(dest)) if os.path.isdir(dest) else []
        print("  %s 里文件数：%d" % (os.path.basename(dest), len(files)))
        for f in files[:6]:
            print("     · %s" % f)
        check("解压目录里有 4 个文件", len(files) == 4, len(files))
        # ④ 状态行
        check("状态行显示「素材 4/4」", "素材 4/4" in dlg.lbl_status.text(),
              dlg.lbl_status.text()[:120])
        check("首次确实解压了（extract 调用 = 1）", calls["n"] == 1, calls["n"])
        # ★ 这个真实包的曲目 bpm=0.0（上游缺字段）⇒ 必须有可见告警，否则会被当成"码表不跟拍"
        red = dlg.lbl_error.text()
        check("曲目缺 BPM 时有可见告警（避免被误报为不跟拍）",
              "没有 BPM" in red, red[:120])

        # ③ 幂等：再导入一次 ⇒ 不再解压，文件 mtime 不变
        before = {f: os.path.getmtime(os.path.join(dest, f)) for f in files}
        n_before = calls["n"]
        time.sleep(0.05)
        t0 = time.perf_counter()
        ok2 = dlg.import_one(REAL_PKG)
        t_second = time.perf_counter() - t0
        after = {f: os.path.getmtime(os.path.join(dest, f)) for f in files}
        print("\n第二次导入：%.2fs ｜ 装表=%s ｜ extract 调用 +%d"
              % (t_second, ok2, calls["n"] - n_before))
        check("第二次装表也成功", ok2 is True)
        check("第二次**没有解压**（extract 调用 +0）", calls["n"] == n_before,
              calls["n"] - n_before)
        check("文件 mtime 未变（真的没重写）", before == after,
              [(f, before[f], after.get(f)) for f in before if before[f] != after.get(f)])
        check("第二次仍然 missing 归零 + 路径齐", len(dlg.missing) == 0
              and len(dlg.media_paths) == 4, (len(dlg.missing), len(dlg.media_paths)))
        print("  ⇒ 首次 %.1fs / 二次 %.2fs（差 %.1fs 就是省下来的解压时间）"
              % (t_first, t_second, t_first - t_second))
        dlg.close()
    finally:
        cuesheet.extract_media = real_extract
        cue_ui.pick_track = real_pick
        cue_ui.QFileDialog = real_fd

    print("")
    if fails:
        print("EXTRACT_FAIL  %d 项不通过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("EXTRACT_OK  内嵌素材已解压（missing 4→0）/ 4 个文件就位 / 幂等 / 状态行 素材 4/4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
