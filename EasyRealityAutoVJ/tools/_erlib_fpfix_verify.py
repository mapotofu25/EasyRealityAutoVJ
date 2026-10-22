#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「导入指纹列错位」修复（2026-10-08，P0）。

## Bug 回顾
`iter_fingerprints()` yield 的是 `(song_id, hash, offset)`，而指纹表的列顺序是
`(hash, song_id, offset)`。旧代码直接 `INSERT INTO fingerprints VALUES (?,?,?)`
⇒ SQLite 动态类型**不报错**地两列插反 ⇒ 按 song_id 一条都查不到 ⇒ 识别永远
匹配不上；而 `COUNT(*)` 看起来"导入成功"。

## 判据（每条都能单独抓住这个 bug）
  T1 按 song_id（整数）能查到这批歌的指纹（旧写法 = 0 条）
  T2 库里 `typeof(song_id)<>'integer'` 的行数为 0（旧写法 = 全部脏）
  T3 **端到端**：真实音频算指纹 → `FingerprintDB.match()` 认出该 song_id
     （旧写法必然返回 -1）
  T4 幂等：同一包再导一次，总行数不膨胀（旧写法删不掉脏行 ⇒ 直接翻倍）

## 用法
    python tools/_erlib_fpfix_verify.py             # 修复版（应全过）
    python tools/_erlib_fpfix_verify.py --old       # 反向复现（应失败）

## 反向复现原理
`--old` 把 `erlib.transfer._import_fingerprints` 换成**复刻旧写法**的实现
（只复刻"列顺序"这一处 bug，其余含版本号写入都保留，避免混入别的变量）。
"""
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

PKG = os.path.join(ROOT, "dist_erlib", "我的曲库.eravjlib")

import erlib.transfer as TR                                    # noqa: E402


def _old_import_fingerprints(src, base_dir, sids, on_progress=None):
    """复刻**旧写法**（唯一差异：INSERT 不写列名、不换序）。

    ⚠ 这里保留版本号写入（那是另一个已修的 bug，不要混进来）。
    """
    db = os.path.join(base_dir, "fingerprints.db")
    d = os.path.dirname(db)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30.0, check_same_thread=False)
    n = 0
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:                                      # noqa: BLE001
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS fingerprints ("
                     "hash TEXT NOT NULL, song_id INTEGER NOT NULL,"
                     " offset INTEGER NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON fingerprints(hash)")
        if sids:
            conn.executemany("DELETE FROM fingerprints WHERE song_id=?",
                             [(int(x),) for x in sids])
        try:
            import fp as _fp
            ver = int(getattr(_fp, "FP_SCHEMA_VERSION", 0) or 0)
            if ver:
                cur = conn.execute("PRAGMA user_version").fetchone()[0]
                if int(cur) != ver:
                    conn.execute("PRAGMA user_version=%d" % ver)
        except Exception:                                      # noqa: BLE001
            pass
        for batch in src.iter_fingerprints():
            conn.executemany("INSERT INTO fingerprints VALUES (?,?,?)", batch)
            n += len(batch)
        conn.commit()
    finally:
        conn.close()
    return n


def _counts(db):
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        total = c.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        bad = c.execute("SELECT COUNT(*) FROM fingerprints "
                        "WHERE typeof(song_id) <> 'integer'").fetchone()[0]
        return total, bad
    finally:
        c.close()


def main():
    old = "--old" in sys.argv
    if not os.path.isfile(PKG):
        print("★ 找不到测试包：%s" % PKG)
        return 2
    if old:
        TR._import_fingerprints = _old_import_fingerprints
        print("（已切换为**旧写法**：反向复现模式）")
    else:
        print("（修复版模式）")

    import erlib.format as FMT
    r = FMT.LibReader(PKG)
    sids = [int(s["song_id"]) for s in r.songs()]
    n_fp_expect = r.conn.execute(
        "SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    r.close()

    tmp = tempfile.mkdtemp(prefix="erlib_fpfix_")
    t0 = time.perf_counter()
    st = TR.import_into(PKG, tmp)
    el = time.perf_counter() - t0
    db = os.path.join(tmp, "fingerprints.db")

    fails = []

    # ---------- T1 按 song_id 能查到 ----------
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    n_by_sid = 0
    for s in sids:
        n_by_sid += c.execute(
            "SELECT COUNT(*) FROM fingerprints WHERE song_id=?",
            (s,)).fetchone()[0]
    c.close()
    ok1 = n_by_sid == n_fp_expect == st.get("fingerprints", 0)
    print("\nT1 按 song_id 查指纹：%d 条（包里 %d 条，import_into 报 %d 条）"
          % (n_by_sid, n_fp_expect, st.get("fingerprints", 0)))
    print("   %s" % ("✓ 全部可查" if ok1 else
                     "★ 查不到（列错位！）"))
    if not ok1:
        fails.append("T1")

    # ---------- T2 无错位脏行 ----------
    total, bad = _counts(db)
    ok2 = bad == 0
    print("\nT2 错位脏行（typeof(song_id)<>'integer'）：%d（总行 %d）" % (bad, total))
    print("   %s" % ("✓ 干净" if ok2 else "★ 有错位脏行"))
    if not ok2:
        fails.append("T2")

    # ---------- T3 端到端：真实音频 match ----------
    print("\nT3 端到端匹配（真实音频 → FingerprintDB.match）")
    ok3 = None
    try:
        from erlib.scanner import decode
        import fp as FP
        root = (FMT.LibReader(PKG).meta() or {}).get("root", "")
        r2 = FMT.LibReader(PKG)
        cand = []
        for s in r2.songs():
            cand.append((int(s["song_id"]), s.get("rel_path") or "",
                         s.get("file_name") or ""))
        r2.close()
        got = None
        for sid, rel, fname in cand[:3]:
            p = os.path.join(root, rel) if rel else ""
            if p and os.path.isfile(p):
                got = (sid, fname, p)
                break
        if got is None:
            print("   （跳过：前 3 首的音频文件都不在，无法端到端）")
            ok3 = None
        else:
            sid, fname, p = got
            sig, sr, dur = decode(p)
            db_obj = FP.FingerprintDB(db)
            msid, votes, off = db_obj.match(sig, sr)
            db_obj.close()
            ok3 = (int(msid) == int(sid))
            print("   音频：%s" % fname)
            print("   match 返回：song_id=%s（期望 %s）votes=%s offset=%.2fs"
                  % (msid, sid, votes, off))
            print("   %s" % ("✓ 认出来了" if ok3 else "★ 认不出（指纹对不上）"))
            if not ok3:
                fails.append("T3")
    except Exception as e:                                     # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("   ★ T3 执行异常：%s" % e)
        fails.append("T3")
        ok3 = False

    # ---------- T4 幂等 ----------
    st2 = TR.import_into(PKG, tmp)
    total2, bad2 = _counts(db)
    ok4 = (total2 == total)
    print("\nT4 幂等（再导一次）：总行 %d → %d" % (total, total2))
    print("   %s" % ("✓ 没膨胀" if ok4 else "★ 膨胀了（删不掉旧行）"))
    if not ok4:
        fails.append("T4")

    print("\n用时 %.1f 秒" % el)
    if fails:
        print("结论：★ 判据失败：%s" % "、".join(fails))
        return 1
    print("结论：✅ 全部通过（按 song_id 可查 + 无脏行 + 真实音频匹配成功 + 幂等）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
