# -*- coding: utf-8 -*-
"""「＋ 曲目」合并进「导入音频…」+ 素材网格尺寸（2026-11-03 02:09/02:10）。

用户两句原话：
  · 「去掉曲目里面那个加曲目那个选项，直接合并到那个导入音频里面。」
  · 「这个素材的图标间隔太大了，图标也太大，这个最小的这一档图标还能再小一点。」

覆盖：
  A. 曲目面板**没有**「＋ 曲目」按钮，只有「导入音频…」「删除曲目」
  B. `import_audio_as_new_track(path)`：**一次点击 = 新建一条曲目 + 读标签/时长**
     （空码表也能开始；已有曲目时再导入 = 再加一首，不是改旧的）
  C. 没有码表/会话时返回 False，不炸
  D. 素材网格三档都变小、格间距收紧（`grid - icon <= 10`），最小档 ≤ 68

跑法：
    set QT_QPA_PLATFORM=offscreen
    <Cue>\\venv\\Scripts\\python.exe tests\\test_track_import_and_grid.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="cuegrid_")
os.environ["LOCALAPPDATA"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from PySide6.QtWidgets import QApplication, QPushButton                # noqa: E402

from ercue.ui.window import MainWindow                                 # noqa: E402

_APP = QApplication.instance() or QApplication([])
_fail = 0


def check(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + str(detail)) if detail else ""))


def pump(ms=200):
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        _APP.processEvents()
        time.sleep(0.005)


def make_wav(path, secs=1.2, sr=44100):
    """造一个能用 mutagen 读出时长的 wav（16bit 单声道）。"""
    n = int(sr * secs)
    with open(path, "wb") as f:
        data = b"\x00\x00" * n
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


def main():
    btn_texts = []
    w = MainWindow()
    w.resize(1500, 950)
    w.move(-20000, -20000)
    w.show()
    pump(500)

    print("=" * 78)
    print("【A】曲目面板按钮：不再有「＋ 曲目」")
    print("=" * 78)
    for b in w.track_panel.findChildren(QPushButton):
        if b.text():
            btn_texts.append(b.text())
    print("  按钮：%s" % " / ".join(btn_texts))
    check(not any("＋ 曲目" in t or "+ 曲目" in t for t in btn_texts),
          "(a1) 没有「＋ 曲目」按钮")
    check(any("导入音频" in t for t in btn_texts), "(a2) 有「导入音频…」")
    check(any("删除曲目" in t for t in btn_texts), "(a3) 有「删除曲目」")

    print()
    print("=" * 78)
    print("【B】导入音频 = 新建一条曲目（空码表也能开始）")
    print("=" * 78)
    wav = os.path.join(_TMP, "测试歌 A.wav")
    make_wav(wav)
    w.new_sheet("导入即建曲目")
    pump(200)
    n0 = len(w._sheet().tracks)
    check(n0 == 0, "(b1) 新码表 0 首曲目", n0)
    ok = w.import_audio_as_new_track(wav)
    pump(300)
    tr = w._sheet().tracks
    check(ok and len(tr) == n0 + 1, "(b2) 导入一次 ⇒ 多了一条曲目", (ok, len(tr)))
    if tr:
        idt = tr[-1].identity
        check(idt.file_name == os.path.basename(wav),
              "(b3) 曲目记住了音频文件名", idt.file_name)
        check(abs(float(idt.duration_sec) - 1.2) < 0.15,
              "(b4) 时长读出来了（≈1.2s）", idt.duration_sec)
        check(bool(idt.title), "(b5) 曲名有值（标签或文件名兜底）", idt.title)
        check(len(tr[-1].composition.layers) >= 1,
              "(b6) 自动给了一层（能直接开始编排）", len(tr[-1].composition.layers))
    # 再导一次 ⇒ 再加一首（不是改旧的）
    wav2 = os.path.join(_TMP, "测试歌 B.wav")
    make_wav(wav2)
    w.import_audio_as_new_track(wav2)
    pump(300)
    check(len(w._sheet().tracks) == 2, "(b7) 第二次导入 ⇒ 2 首（每次都新建）",
          len(w._sheet().tracks))

    print()
    print("=" * 78)
    print("【C】没有会话 ⇒ 返回 False，不抛异常")
    print("=" * 78)
    w2 = MainWindow()
    w2.move(-20000, -20000)
    pump(200)
    try:
        w2.sessions.clear()
        w2.current_key = ""
        got = w2.import_audio_as_new_track(wav)
        check(got is False, "(c1) 没会话 ⇒ False（不崩）", got)
    except Exception as e:                                    # noqa: BLE001
        check(False, "(c1) 没会话 ⇒ False（不崩）", "%s: %s" % (type(e).__name__, e))

    print()
    print("=" * 78)
    print("【D】素材网格：三档都变小、间隔收紧")
    print("=" * 78)
    mp = w.media_panel
    got = {}
    for lvl in (1, 2, 3):
        mp._set_icon_size(lvl)
        _APP.processEvents()
        # ⚠ 必须把**当档**的图标高也存下来：循环结束后 `iconSize()` 已经是最后一档的值，
        #   拿它给第 1/2 档算"是否留得下文件名"必然算错（我自己踩过）。
        got[lvl] = (mp.lst.iconSize().width(), mp.lst.iconSize().height(),
                    mp.lst.gridSize().width(), mp.lst.gridSize().height(),
                    mp.lst.spacing())
    print("     小/中/大 = %s" % got)
    check(got[1][0] <= 68, "(d1) 最小档图标宽 ≤ 68（原来 96）", got[1][0])
    check(got[2][0] <= 96, "(d2) 中档图标宽 ≤ 96（原来 144）", got[2][0])
    check(got[3][0] <= 144, "(d3) 大档图标宽 ≤ 144（原来 208）", got[3][0])
    check(got[1][0] < got[2][0] < got[3][0], "(d4) 三档仍逐级变大",
          [got[i][0] for i in (1, 2, 3)])
    for lvl, (iw, ih, gw, gh, sp) in got.items():
        check(gw - iw <= 10, "(d5) 第 %d 档横向留白 ≤10px（原来 16）" % lvl, gw - iw)
        check(gh - ih >= 14, "(d6) 第 %d 档仍留得下文件名（格高−图标高）" % lvl, gh - ih)
        check(sp <= 2, "(d7) 第 %d 档 spacing ≤2（原来最多 12，和格子留白叠加）" % lvl, sp)

    print()
    print("=" * 78)
    try:
        w.shutdown()
        w2.shutdown()
    except Exception:                                          # noqa: BLE001
        pass
    if _fail:
        print("结果：%d 项失败 ✗" % _fail)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
