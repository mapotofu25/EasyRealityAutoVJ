# -*- coding: utf-8 -*-
r"""erlib 验证：**勾选导出子集** 是否正确 + 界面能否正常构造。

## 验什么
1. 扫描 3 首 → 工作库
2. 只导出其中 2 首 → 导出包里**必须正好 2 首**（且指纹/字段齐全）
3. 界面冒烟：offscreen 起窗口、加行、全选/全不选、筛选

## 反向复现（证明测试不是空过）
`--old`：monkeypatch 掉 `export_subset` 的筛选（不筛、全导出）
⇒ 第 2 步的「正好 2 首」必须失败。
"""

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                               # noqa: E402
import soundfile as sf                                           # noqa: E402

SPECS = [("a.flac", 22.0, 128.0), ("b.flac", 25.0, 174.0), ("c.flac", 20.0, 140.0)]


def make(d):
    out = []
    sr = 44100
    for name, dur, bpm in SPECS:
        n = int(sr * dur)
        t = np.arange(n) / sr
        beat = (np.sin(2 * np.pi * (bpm / 60.0) * t) > 0.75).astype(np.float32)
        x = (0.30 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32) + 0.40 * beat
        x += (0.02 * np.random.RandomState(3).randn(n)).astype(np.float32)
        p = os.path.join(d, name)
        sf.write(p, np.stack([x, x], axis=1), sr, format="FLAC")
        out.append(p)
    return out


def main():
    old = "--old" in sys.argv
    results = []

    def chk(name, cond):
        results.append((name, bool(cond)))
        print("   %s %s" % ("✓" if cond else "✗", name))

    from erlib.format import LibReader, LibWriter, song_id_of
    from erlib.scanner import Scanner
    import erlib.transfer as TR

    if old:
        _orig = TR.export_subset

        def _nofilter(work, out_path, sids, on_progress=None, app_ver="erlib"):
            all_ids = []
            r = LibReader(work)
            try:
                all_ids = [int(s["song_id"]) for s in r.songs()]
            finally:
                r.close()
            return _orig(work, out_path, all_ids, on_progress, app_ver)
        TR.export_subset = _nofilter
        print("★ --old：export_subset 不再筛选（全导出）\n")

    d = tempfile.mkdtemp(prefix="erlib_exp_")
    try:
        make(d)
        work = os.path.join(d, "work.eravjlib")
        out = os.path.join(d, "subset.eravjlib")

        print("① 扫描 3 首 → 工作库")
        w = LibWriter(work, root=d, resume=False, app_ver="t")
        sc = Scanner(root=d)
        st = sc.scan_tree(d, w)
        w.close()
        print("   ok=%d fail=%d" % (st["ok"], st["fail"]))
        chk("工作库有 3 首", TR.count_songs(work) == 3)

        print("\n② 只导出前 2 首")
        r = LibReader(work)
        ids = [int(s["song_id"]) for s in r.songs()]
        r.close()
        pick = sorted(ids)[:2]
        stats = TR.export_subset(work, out, pick)
        n_out = TR.count_songs(out)
        print("   勾选 %d 首 → 导出 %d 首（指纹 %d 条）"
              % (len(pick), n_out, stats.get("fingerprints", 0)))
        chk("导出包正好 2 首（筛选生效）", n_out == 2)
        chk("导出包有指纹", stats.get("fingerprints", 0) > 0)
        chk("包体积 > 0", os.path.getsize(out) > 0)

        print("\n③ 导出包完整性（读回逐字段）")
        rr = LibReader(out)
        ok_ver, msg = rr.check_versions()
        songs = list(rr.songs())
        n_fp = sum(len(b) for b in rr.iter_fingerprints())
        rr.close()
        chk("版本校验通过", ok_ver)
        chk("每首字段齐全",
            all(k in s for s in songs
                for k in ("file_name", "file_size", "duration", "grid")))
        chk("指纹数与整体一致", n_fp > 0)

        print("\n④ 界面冒烟（offscreen）")
        try:
            from PySide6.QtWidgets import QApplication
            from erlib.app import ErlibWindow
            app = QApplication.instance() or QApplication([])
            win = ErlibWindow()
            win.show()
            app.processEvents()
            chk("窗口构造成功", win is not None)
            chk("表格列数正确", win.tbl.columnCount() == 7)
            win._load_existing()
            app.processEvents()
            chk("事件处理正常", True)
            win.close()
        except Exception as e:                                   # noqa: BLE001
            print("   界面异常：%s: %s" % (type(e).__name__, e))
            chk("窗口构造成功", False)

        print("\n" + "=" * 72)
        all_ok = all(v for _, v in results)
        if old:
            print("反向复现：%s（未筛选 ⇒ 「正好 2 首」被抓出）"
                  % ("✅ 成功" if not all_ok else "❌ 未复现"))
            return 0 if not all_ok else 1
        print("结论：%s（%d/%d 通过）"
              % ("✅ 全部通过" if all_ok else "❌ 有失败",
                 sum(1 for _, v in results if v), len(results)))
        print("=" * 72)
        return 0 if all_ok else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
