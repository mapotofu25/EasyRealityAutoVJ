# -*- coding: utf-8 -*-
r"""erlib **导入端**端到端验证（第二步）。

## 验什么
夹具 → 扫描 → 导出 `.eravjlib` → **导入到临时 base_dir** → 逐项校验：
- `imported_meta.db` 有歌、字段齐
- 封面文件真的落盘
- 指纹进了 `fingerprints.db`
- **★ 用主程序自己的 `fp.FingerprintDB` 打开一遍**（这是最真实的复现）：
  它会检查 `PRAGMA user_version`，不等于 `FP_SCHEMA_VERSION` 就**清空整张表** ——
  导入时漏写版本号的话，用户一开主程序，刚导入的指纹就全没了（且毫无提示）。
- `imported_lib.lookup_grids()` / `lookup_genres()` 能给出引擎要的结构

## 反向复现
`--old`：导入后**把 user_version 改回 0**（模拟"忘了写版本号"）
⇒ 用主程序打开后指纹数应为 0 ⇒ 判据必须失败。
"""

import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                               # noqa: E402
import soundfile as sf                                           # noqa: E402

SPECS = [("a.flac", 20.0, 128.0), ("b.flac", 22.0, 174.0)]


def make(d):
    sr = 44100
    for name, dur, bpm in SPECS:
        n = int(sr * dur)
        t = np.arange(n) / sr
        beat = (np.sin(2 * np.pi * (bpm / 60.0) * t) > 0.75).astype(np.float32)
        x = (0.30 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32) + 0.40 * beat
        x += (0.02 * np.random.RandomState(5).randn(n)).astype(np.float32)
        sf.write(os.path.join(d, name), np.stack([x, x], axis=1), sr,
                 format="FLAC")


def main():
    old = "--old" in sys.argv
    res = []

    def chk(name, cond, extra=""):
        res.append((name, bool(cond)))
        print("   %s %s%s" % ("✓" if cond else "✗", name,
                              ("  " + extra) if extra else ""))

    from erlib.format import LibWriter
    from erlib.scanner import Scanner
    from erlib.transfer import export_subset, import_into
    import imported_lib as IL
    import fp as FP

    work_dir = tempfile.mkdtemp(prefix="erlib_imp_")
    base = tempfile.mkdtemp(prefix="erlib_base_")     # 模拟演出机的 app_base_dir
    try:
        # ---- 造包 ----
        src_dir = os.path.join(work_dir, "src")
        os.makedirs(src_dir)
        make(src_dir)
        work = os.path.join(work_dir, "work.eravjlib")
        w = LibWriter(work, root=src_dir, resume=False, app_ver="t")
        Scanner(root=src_dir).scan_tree(src_dir, w)
        w.close()
        pkg = os.path.join(work_dir, "pkg.eravjlib")
        st_ex = export_subset(work, pkg, _sids(work))
        print("① 造包完成：%d 首 / %d 条指纹 / %.2f MB"
              % (st_ex.get("songs", 0), st_ex.get("fingerprints", 0),
                 os.path.getsize(pkg) / 1e6))

        # ---- 导入 ----
        print("\n② 导入到临时 base_dir")
        st = import_into(pkg, base)
        print("   songs=%d covers=%d fingerprints=%d"
              % (st["songs"], st["covers"], st["fingerprints"]))
        chk("元数据入库", st["songs"] == len(SPECS))
        chk("指纹入库", st["fingerprints"] > 0)

        # ---- 存储校验 ----
        print("\n③ 落地校验")
        chk("imported_meta.db 存在", os.path.isfile(IL.meta_db_path(base)))
        chk("imported_lib.count() 对得上", IL.count(base) == len(SPECS))
        cdir = IL.cover_dir(base)
        n_cov = len([f for f in os.listdir(cdir)]) if os.path.isdir(cdir) else 0
        print("   封面文件：%d 个（夹具没有内嵌图，0 也正常）" % n_cov)
        grids = IL.lookup_grids(base)
        gen = IL.lookup_genres(base)
        chk("lookup_genres 有数据", len(gen) == len(SPECS))
        print("   lookup_grids：%d 条（只收 src∈vdj/bt 且 conf≥0.6，夹具是自算的，少也正常）"
              % len(grids))
        if gen:
            k = list(gen)[0]
            print("   样例 genres：%r" % (gen[k].get("genres"),))

        # ---- ★ 用主程序自己的库类打开（最真实的复现）----
        print("\n④ ★ 用主程序 fp.FingerprintDB 打开（它会校验 user_version）")
        fpdb = os.path.join(base, "fingerprints.db")
        n_before = _count_fp(fpdb)
        db = FP.FingerprintDB(fpdb)
        try:
            n_after = db.conn.execute(
                "SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        finally:
            try:
                db.conn.close()
            except Exception:                                    # noqa: BLE001
                pass
        print("   打开前 %d 条 → 打开后 %d 条" % (n_before, n_after))
        chk("指纹没被主程序清空", n_after > 0 and n_after == n_before)

        if old:
            # 模拟"忘了写 user_version"：改成 0，再用主程序打开
            c = sqlite3.connect(fpdb)
            c.execute("PRAGMA user_version=0")
            c.commit()
            c.close()
            db2 = FP.FingerprintDB(fpdb)
            try:
                n0 = db2.conn.execute(
                    "SELECT COUNT(*) FROM fingerprints").fetchone()[0]
            finally:
                try:
                    db2.conn.close()
                except Exception:                                # noqa: BLE001
                    pass
            print("   反向：user_version=0 时主程序打开后剩 %d 条" % n0)
            chk("此处应失败（指纹被清空）", n0 > 0)

        print("\n" + "=" * 72)
        allok = all(v for _, v in res)
        if old:
            print("反向复现：%s（漏写版本号 ⇒ 指纹被主程序清空）"
                  % ("✅ 成功" if not allok else "❌ 未复现"))
            return 0 if not allok else 1
        print("结论：%s（%d/%d）"
              % ("✅ 全部通过" if allok else "❌ 有失败",
                 sum(1 for _, v in res if v), len(res)))
        print("=" * 72)
        return 0 if allok else 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(base, ignore_errors=True)


def _sids(work):
    from erlib.format import LibReader
    r = LibReader(work)
    try:
        return [int(s["song_id"]) for s in r.songs()]
    finally:
        r.close()


def _count_fp(path):
    try:
        c = sqlite3.connect(path)
        try:
            return int(c.execute(
                "SELECT COUNT(*) FROM fingerprints").fetchone()[0])
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        return 0


if __name__ == "__main__":
    sys.exit(main())
