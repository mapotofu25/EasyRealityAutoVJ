"""第七轮探针：**单击**（不双击）拖片段两端改长短 —— 是否真的生效？

场景：
  ① 短片段（完全在视口内）：单击右边缘拉长
  ② 长片段（右端在视口外，`-8` 修的场景）：单击**视口边缘**的把手拖动
  ③ 未选中直接拖（不预先单击选中）
真窗口（offscreen），复用 tests/test_ui_smoke.py 的夹具风格。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

from PySide6.QtCore import QPoint, Qt                     # noqa: E402
from PySide6.QtTest import QTest                          # noqa: E402
from PySide6.QtWidgets import QApplication                # noqa: E402

APP = QApplication.instance() or QApplication([])

from ercue.ui import theme                                # noqa: E402
from ercue.ui.window import MainWindow                    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("✓" if cond else "✗", name, (" -> " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def _mk_media(d, sec=30.0):
    """造一个有长度的假素材（用 ffmpeg? 无 —— 走 wav 假文件 + 时长字段不可控，
    所以这里直接用 '添加素材后手动改片段时长' 的方式造长片段。"""
    p = os.path.join(d, "clip.mp4")
    with open(p, "wb") as f:
        f.write(b"\x00" * 4096)
    return p


def main():
    d = tempfile.mkdtemp(prefix="fix7_probe_")
    w = MainWindow(restore_workspace=False, autosave=False)
    try:
        w.show()
        APP.processEvents()
        w.resize(1200, 700)
        APP.processEvents()
        w.new_sheet("PROBE")
        med = _mk_media(d)
        w.add_media_paths([med])
        APP.processEvents()
        tl = w.timeline
        ly = w._layers()[0]
        clip_id = next(iter(w.session.media_paths.keys()))

        # ---------- 场景①：短片段（完全在视口内）单击拉右边缘 ----------
        w.add_cue(ly.lyr_id, clip_id, 2.0)
        APP.processEvents()
        it = ly.items[0]
        it.end_sec = 6.0                      # 片段 2.0~6.0（短，完全在视口里）
        w._rebuild_runtime()
        tl.set_view(0.0, 30.0)
        APP.processEvents()
        row = tl._layers().index(ly)
        y = int(theme.RULER_H + tl.row_h * row + tl.row_h / 2)
        xr = tl._x(6.0)                       # 右边缘
        print("[① 短片段 · 单击(不双击)拉右边缘] 宽=%d xr=%d y=%d" % (tl.width(), xr, y))
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xr) - 2, y))
        APP.processEvents()
        check("① 进入 resize 而非 move（_resize 已建立）", tl._resize is not None,
              "resize=%s drag=%s trim=%s" % (tl._resize is not None, tl._drag is not None,
                                             tl._trim is not None))
        QTest.mouseMove(tl, QPoint(int(tl._x(10.0)), y))
        APP.processEvents()
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(tl._x(10.0)), y))
        APP.processEvents()
        it2 = ly.items[0]
        check("① 松手后 end 真的变大（≈10）", abs((it2.end_sec or 0) - 10.0) < 1.0,
              "end=%s" % it2.end_sec)

        # ---------- 场景②：长片段（右端在视口外）单击视口边缘把手 ----------
        it2.at_sec = 4.0
        it2.end_sec = 120.0                   # 4~120s，而视口 0~30s ⇒ 右端在视口外
        w._rebuild_runtime()
        tl.set_view(0.0, 30.0)
        APP.processEvents()
        xv = tl.width() - 2                   # 视口右缘（把手被钳到这儿）
        print("[② 长片段 · 单击视口边缘的把手] 宽=%d xv=%d y=%d" % (tl.width(), xv, y))
        QTest.mousePress(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xv), y))
        APP.processEvents()
        check("② 命中视口边缘把手 ⇒ 进 resize（不是 move）", tl._resize is not None,
              "resize=%s drag=%s" % (tl._resize is not None, tl._drag is not None))
        at_before = float(ly.items[0].at_sec)
        QTest.mouseMove(tl, QPoint(int(xv) - 120, y))
        APP.processEvents()
        QTest.mouseRelease(tl, Qt.LeftButton, Qt.NoModifier, QPoint(int(xv) - 120, y))
        APP.processEvents()
        it3 = ly.items[0]
        check("② 松手后 end 真的变小（<120）", (it3.end_sec or 1e9) < 119.0,
              "end=%s（原 120）" % it3.end_sec)
        check("② 原件 at 未被搬走（≈4.0）", abs(float(it3.at_sec) - at_before) < 0.05,
              "at %.3f → %.3f" % (at_before, float(it3.at_sec)))

        print("\n" + "=" * 70)
        if FAILS:
            print("PROBE_FAIL: %d 项未过 -> %s" % (len(FAILS), FAILS))
        else:
            print("PROBE_OK: 单击拖两端（含长片段视口外场景）均生效")
    finally:
        w.shutdown()


if __name__ == "__main__":
    main()
