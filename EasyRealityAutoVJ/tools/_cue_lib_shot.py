# -*- coding: utf-8 -*-
"""**真窗口**验证：曲库面板里的「码表派生行」（青绿 / 来源筛选 / 跟随码表）。

为什么必须真窗口（项目铁律）：`refresh()` 的颜色、下拉选项、筛选行数
都是 Qt 分支 —— 无头跑不到 `setForeground` / `addItem` 这些调用，只能靠真控件取证。

断言：
  ① 码表行前景色 == `#4ec9b0`（青绿；蓝=导入包、红=文件缺失，三者不混）
  ② 「来源」下拉里能选到 `__cue__`（仅码表）和 `cue:<表绝对路径>`（某张表）
  ③ 选「仅码表」后列表里**只剩**码表派生行；选「仅本机」后一个都没有
  ④ 「只看文件缺失」不把码表行算进来（语义不同）
  ⑤ 截图存 `tools/_cueshots/cue_lib_rows.png`

⚠ 夹具全在 `tempfile.mkdtemp()` 里（配置/码表包都不碰真实用户数据）。

运行：`venv\\Scripts\\python.exe tools/_cue_lib_shot.py`
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from PySide6.QtWidgets import QApplication                     # noqa: E402
from PySide6.QtCore import Qt                                   # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)

import cuesheet                                                 # noqa: E402
import cue_index                                                # noqa: E402
import panels                                                   # noqa: E402

OUT_DIR = os.path.join(SRC, "..", "tools", "_cueshots")


class FakeCfg:
    """最小 `Config`（没有 .get()，与项目铁律一致）。"""

    def __init__(self, data):
        self.data = data
        self.saves = 0

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self, force=False):
        self.saves += 1


class FakeMain:
    """曲库面板只用到这些成员（构造函数里 connect 的槽 + `cfg`）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []

    def _noop(self, *a, **k):
        self.calls.append((a, k))
        return None

    import_music_dir = _noop
    import_music_files = _noop
    scan_music = _noop
    show_clear_music_analysis = _noop
    import_erlib_package = _noop
    edit_music_genre = _noop
    rescan_music = _noop
    show_music_info = _noop
    remove_music_many = _noop


def make_sheet(tracks):
    s = cuesheet.CueSheet(title="跨年演出码表", res_w=1920, res_h=1080)
    for k, (tt, ar, du, bpm, fp) in enumerate(tracks):
        te = cuesheet.TrackEntry(track_key="k%d" % k)
        te.identity = cuesheet.Identity(title=tt, artist=ar, duration_sec=du,
                                        bpm=bpm, fp_id=fp or "")
        te.identity.ensure_meta_id()
        te.layers.append(cuesheet.CompLayer(lyr_id="L%d" % k, name="层%d" % k, z=0))
        s.tracks.append(te)
    s.media.append(cuesheet.MediaRef(clip_id="c1", name="a.mp4"))
    return s


def main():
    fails = []

    def check(name, cond, detail=""):
        print("  [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                               "" if cond else "   ← " + str(detail)))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="cueshot_")
    # 表：3 首。第 1 首本机已有（fp_id 命中）⇒ **不该**出现在曲库
    pkg = os.path.join(tmp, "跨年演出.ercue")
    tracks = [
        ("Already Local", "LOCAL", 180.0, 128.0, "fp2-aaaaaaaabbbbbbbb"),
        ("Neon Drift", "Supa7onyz", 165.5, 155.0, "fp2-0123456789abcdef"),
        ("Midnight Run", "Aoi", 200.0, 132.0, "fp2-fedcba9876543210"),
    ]
    cuesheet.save(make_sheet(tracks), pkg, app_version="2026.10.17.12")

    idx = cue_index.CueIndex()
    idx.add(pkg)

    # 本机曲库：一首（文件故意不存在 ⇒ 红色「文件缺失」行，用来对照颜色）
    local_path = os.path.join(tmp, "already_local.mp3")
    data = {
        "music_library": [local_path],
        "music_meta": {local_path: {
            "song_id": 463268676,                       # 路径哈希（不是 md5）
            "fp_id": "fp2-aaaaaaaabbbbbbbb",            # ← 与表里第 1 首相同
            "grid": {"bpm": 128.0, "dur": 180.0},
            "artist": "LOCAL", "title": "Already Local",
            "genres": ["House"], "source": "vdj",
        }},
        "ui": {"music_cover_size": 64},
        "cue": {"sheets": list(idx.entries)},
    }
    cfg = FakeCfg(json.loads(json.dumps(data)))
    main_ = FakeMain(cfg)

    panel = panels.MusicLibraryPanel(main_)
    panel.resize(1200, 640)
    panel.show()
    APP.processEvents()
    panel.refresh()
    APP.processEvents()

    def items():
        return [panel.list.item(i) for i in range(panel.list.count())]

    def cue_items():
        return [it for it in items()
                if str(it.data(Qt.UserRole)).startswith("@cue:")]

    print("=" * 78)
    print("曲库面板：码表派生行（真窗口）")
    print("=" * 78)

    all_items = items()
    cues = cue_items()
    print("  列表共 %d 行；其中码表派生行 %d 行" % (len(all_items), len(cues)))
    for it in all_items:
        print("    · " + it.text())

    # ① 只列本机没覆盖的两首 + 青绿
    check("派生行只列本机未覆盖的 2 首（fp_id 命中的不计）", len(cues) == 2, len(cues))
    tcol = [it.foreground().color().name().lower() for it in cues]
    check("码表行前景色 == #4ec9b0（青绿）",
          bool(tcol) and all(c == panels.CUE_ROW_COLOR.lower() for c in tcol), tcol)
    check("本机缺失文件行仍是红 #e06666（颜色不串味）",
          any(it.foreground().color().name().lower() == "#e06666"
              for it in all_items if not str(it.data(Qt.UserRole)).startswith("@cue:")),
          [it.foreground().color().name() for it in all_items])

    # ② 来源下拉：__cue__ + 具体表
    cb = panel.cb_src
    i_cue = cb.findData("__cue__", Qt.UserRole)
    i_sheet = cb.findData("cue:" + os.path.abspath(pkg), Qt.UserRole)
    check("来源下拉有「仅码表 · 未入库」(__cue__)", i_cue >= 0,
          [cb.itemData(i, Qt.UserRole) for i in range(cb.count())])
    check("来源下拉有该表（cue:<绝对路径>）", i_sheet >= 0,
          [cb.itemData(i, Qt.UserRole) for i in range(cb.count())])

    # ③ 选「仅码表」⇒ 只剩码表行
    cb.setCurrentIndex(i_cue)
    APP.processEvents()
    n_all, n_cue = panel.list.count(), len(cue_items())
    check("选「仅码表」后只剩码表派生行（%d 行全为 @cue:）" % n_all,
          n_all == 2 and n_cue == 2, (n_all, n_cue))
    # 选「仅本机」⇒ 一个码表行都没有
    cb.setCurrentIndex(cb.findData("__local__", Qt.UserRole))
    APP.processEvents()
    check("选「仅本机」后没有码表派生行",
          len(cue_items()) == 0 and panel.list.count() >= 1,
          (panel.list.count(), len(cue_items())))
    # 选「某张表」⇒ 只剩该表的行
    cb.setCurrentIndex(cb.findData("cue:" + os.path.abspath(pkg), Qt.UserRole))
    APP.processEvents()
    check("选「某张表」后只剩该表的 2 行",
          len(cue_items()) == 2 and panel.list.count() == 2,
          (panel.list.count(), len(cue_items())))

    # ④ 「只看文件缺失」不含码表行
    cb.setCurrentIndex(0)                       # 回「全部」
    APP.processEvents()
    panel.chk_missing.setChecked(True)
    APP.processEvents()
    check("「只看文件缺失」不把码表行算进来", len(cue_items()) == 0,
          [it.text() for it in items()])
    panel.chk_missing.setChecked(False)
    APP.processEvents()

    # ⑤ 勾选状态跨刷新保留（码表行 key 稳定）
    target = cue_items()[0]
    target.setCheckState(Qt.CheckState.Checked)
    APP.processEvents()
    panel.refresh()
    APP.processEvents()
    kept = [it for it in cue_items() if it.checkState() == Qt.CheckState.Checked]
    check("码表行勾选在刷新后保留（key 稳定）", len(kept) == 1,
          [it.text() for it in cue_items()])

    # 截图
    os.makedirs(OUT_DIR, exist_ok=True)
    shot = os.path.join(OUT_DIR, "cue_lib_rows.png")
    APP.processEvents()
    ok = panel.grab().save(shot)
    print("\n  截图：%s（%s）" % (shot, "ok" if ok else "FAILED"))
    check("截图写出成功", bool(ok) and os.path.isfile(shot), shot)
    check("截图不是空文件", os.path.isfile(shot) and os.path.getsize(shot) > 5000,
          os.path.getsize(shot) if os.path.isfile(shot) else -1)

    panel.close()
    APP.processEvents()

    print("\n" + "=" * 78)
    if fails:
        print("FAIL：%d 项未通过 → %s" % (len(fails), fails))
        print("=" * 78)
        return 1
    print("OK：真窗口证据齐全（颜色 / 下拉 / 筛选 / 勾选保留 / 截图）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
