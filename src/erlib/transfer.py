# -*- coding: utf-8 -*-
r"""erlib 包之间的**传输逻辑** —— 与界面解耦，便于自动化验证。

目前：
- `export_subset()` —— 从工作库里挑出勾选的歌，写成最终的 `.eravjlib`

以后（第二步）：
- `import_into()` —— ERAVJ 侧把 `.eravjlib` 并进本机（指纹 → fingerprints.db，
  元数据 → 按 song_id 索引、封面 → 缓存）

## 为什么单独一层
界面里点"导出"只是调一个函数；测试可以直接调同一个函数跑端到端，
不需要起 GUI —— 否则导出这条链就永远测不到（GUI 难自动化）。
"""

import os

from erlib.format import LibReader, LibWriter


def count_songs(path):
    """包里有多少首歌（不全部读出）。"""
    try:
        r = LibReader(path)
        try:
            return r.count()
        finally:
            r.close()
    except Exception:                                            # noqa: BLE001
        return 0


def export_subset(work, out_path, sids, on_progress=None, app_ver="erlib"):
    """把工作库 `work` 里 `sids` 这些歌导出成新包 `out_path`。

    返回 `LibWriter.stats()`：`{'songs': n, 'fingerprints': n, 'covers': n}`。

    ⚠ 只写勾选的歌 —— **没勾的一字节都不进包**（这是"多选导出"的本体）。
    ⚠ 逐首 `commit` 攒 5 首一次，避免每首一次 fsync（大库导出快很多）。
    """
    sids = set(int(x) for x in sids)
    src = LibReader(work)
    try:
        root = (src.meta() or {}).get("root", "")
        w = LibWriter(out_path, root=root, resume=False, app_ver=app_ver)
        try:
            n, total = 0, len(sids)
            for s in src.songs():
                sid = int(s["song_id"])
                if sid not in sids:
                    continue
                w.add_song(sid, s, src.fingerprints_of(sid),
                           src.cover(sid), commit=False)
                n += 1
                if n % 5 == 0:
                    w.commit()
                    if on_progress is not None:
                        try:
                            on_progress(n, total)
                        except Exception:                        # noqa: BLE001
                            pass
            w.commit()
            # ★★ 2026-10-09：把 WAL **合并回主文件**再收工。
            #   否则导出后目录里会留着 `.eravjlib-wal`/`-shm`；用户拷贝给别人时
            #   若只拷主文件、漏了 `-wal`，**WAL 里的数据会一起丢**（导入方看到的是
            #   旧快照）。`TRUNCATE` 把 WAL 清空，让主文件自包含。
            try:
                w.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:                                    # noqa: BLE001
                pass
            st = w.stats()
            if on_progress is not None:
                try:
                    on_progress(n, total)
                except Exception:                                # noqa: BLE001
                    pass
            return st
        finally:
            w.close()
            # ⚠ 必须在 close() **之后**删：连接还开着时删 WAL 会让 SQLite 状态错乱。
            #   有些平台 checkpoint 后仍留 0 字节的占位文件，清掉它们，
            #   让"就拷这一个文件"永远不会踩到。
            for suf in ("-wal", "-shm"):
                try:
                    p = out_path + suf
                    if os.path.isfile(p) and os.path.getsize(p) == 0:
                        os.remove(p)
                except Exception:                                # noqa: BLE001
                    pass
    finally:
        src.close()


def missing_sids(work, sids):
    """勾选的歌里，工作库中**找不到**的那些（界面上可提示"有几首没扫到"）。"""
    sids = set(int(x) for x in sids)
    src = LibReader(work)
    try:
        have = {int(s["song_id"]) for s in src.songs()}
    finally:
        src.close()
    return sorted(sids - have)


# ==========================================================================
# 导入（ERAVJ / 演出机侧）
# ==========================================================================
def _import_fingerprints(src, base_dir, sids, on_progress=None):
    """把包里的指纹并进主库 `fingerprints.db`。

    ## 为什么**并进主库**而不是另开一个库
    识别的代码路径（`fp.FingerprintDB.match`）只查这一个库 ——
    并进去 ⇒ **识别链路一行都不用改**，导入完立刻能认歌。

    ## ⚠ 两个必须做对的地方
    1. **幂等**：导入前先按 song_id 删旧的（重复导入不会让库膨胀）；
    2. **`PRAGMA user_version` 必须写成当前算法版本** ——
       主程序打开库时会检查它，不等于 `FP_SCHEMA_VERSION` 就**清空整张表**重扫。
       不写的话：导入完 → 主程序一打开 → 指纹全没了（而且没有任何提示）。
    """
    import sqlite3
    db = os.path.join(base_dir, "fingerprints.db")
    d = os.path.dirname(db)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30.0, check_same_thread=False)
    n = 0
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:                                        # noqa: BLE001
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS fingerprints ("
                     "hash TEXT NOT NULL, song_id INTEGER NOT NULL,"
                     " offset INTEGER NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON fingerprints(hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_song ON fingerprints(song_id)")
        # ① 幂等：先删这批 song_id 的旧指纹
        if sids:
            conn.executemany("DELETE FROM fingerprints WHERE song_id=?",
                             [(int(x),) for x in sids])
        # ①.5 清掉**历史错位数据**（2026-10-08 那个"列插反"的坑留下的残渣）。
        #      正常写入的 `song_id` 永远是整数 ⇒ `typeof(song_id)<>'integer'`
        #      只会命中错位行，**不会误删任何正常指纹**。这条是幂等的
        #      （修好之后重跑删 0 条），所以每次导入顺手做一次即可。
        try:
            nb = conn.execute(
                "SELECT COUNT(*) FROM fingerprints "
                "WHERE typeof(song_id) <> 'integer'").fetchone()[0]
            if nb:
                conn.execute("DELETE FROM fingerprints "
                             "WHERE typeof(song_id) <> 'integer'")
                print("[erlib] 已清理历史错位指纹 %d 条" % nb)
        except Exception:                                        # noqa: BLE001
            pass
        # ② 版本号对齐（见 docstring）
        try:
            import fp as _fp
            ver = int(getattr(_fp, "FP_SCHEMA_VERSION", 0) or 0)
            if ver:
                cur = conn.execute("PRAGMA user_version").fetchone()[0]
                if int(cur) != ver:
                    conn.execute("PRAGMA user_version=%d" % ver)
        except Exception:                                        # noqa: BLE001
            pass
        # ③ 流式写入（大包不占内存）。
        #    ★★ 必须**显式写列名**，并把 `iter_fingerprints()` 给的
        #       `(song_id, hash, offset)` 换序成表里的 `(hash, song_id, offset)`。
        #    ⚠⚠ 2026-10-08 实测踩到的坑：直接 `INSERT INTO fingerprints VALUES (?,?,?)`
        #       时 SQLite 的动态类型**不报任何错** —— song_id 被塞进 hash 列、
        #       hash 被塞进 song_id 列 ⇒ 按 song_id 一条都查不到 ⇒ 识别永远匹配
        #       不上；而 `COUNT(*)` 看着还"导入成功"了（最迷惑的一类失败）。
        #       判据：`typeof(song_id)` 必须是 integer（见下面的回归测试）。
        for batch in src.iter_fingerprints():
            conn.executemany(
                "INSERT INTO fingerprints(hash, song_id, offset) VALUES (?,?,?)",
                [(h, int(s), int(o)) for s, h, o in batch])
            n += len(batch)
            if on_progress is not None and n % 200000 < len(batch):
                try:
                    on_progress(n, 0, "指纹")
                except Exception:                                # noqa: BLE001
                    pass
        conn.commit()
    finally:
        conn.close()
    return n


def import_into(pkg_path, base_dir, on_progress=None, clear_first=False):
    """把 `.eravjlib` **并进本机**（演出机侧）。返回统计 dict。

    四件事：
    1. **版本校验** —— 指纹/网格算法版本不一致直接拒绝（旧指纹拿去匹配只会永远认不出）；
    2. **元数据** → `<base_dir>/imported_meta.db`（按 song_id 索引，**不碰** `music_meta`）；
    3. **封面** → `<base_dir>/imported_covers/<song_id>.jpg`；
    4. **指纹** → `<base_dir>/fingerprints.db`（并进主库，识别链路无需改动）。

    ⚠ `clear_first=True` 会先清空"之前导入的"（不影响本机自己扫的曲库）。
    """
    import imported_lib as IL
    src = LibReader(pkg_path)
    try:
        ok, msg = src.check_versions()
        if not ok:
            raise ValueError(msg)
        if clear_first:
            IL.clear(base_dir)
        sids = [int(s["song_id"]) for s in src.songs()]
        # ★ 2026-10-09（用户要求②③）：记录**包名**（文件名去扩展名，如「我的曲库」）。
        #   用途：曲库面板按包名筛选 + 行尾显示来源。
        #   ⚠ 重新导入同一个包 ⇒ 同 song_id 走 `add_song` 的 INSERT OR REPLACE，
        #     pack 会被更新成正确值（旧数据"早期导入"由此自然纠正，无需回填）。
        pack = os.path.splitext(os.path.basename(pkg_path))[0]

        # ① 元数据 + 封面
        conn = IL.open_meta(base_dir)
        cd = IL.cover_dir(base_dir)
        try:
            os.makedirs(cd, exist_ok=True)
        except Exception:                                        # noqa: BLE001
            pass
        n_song = n_cover = 0
        total = len(sids)
        try:
            for s in src.songs():
                sid = int(s["song_id"])
                cp = ""
                try:
                    cb = src.cover(sid)
                    if cb:
                        cp = os.path.join(cd, "%d.jpg" % sid)
                        with open(cp, "wb") as f:
                            f.write(cb)
                        n_cover += 1
                except Exception:                                # noqa: BLE001
                    cp = ""
                IL.add_song(conn, sid, s, cp, pack=pack)
                n_song += 1
                if n_song % 50 == 0:
                    conn.commit()
                    if on_progress is not None:
                        try:
                            on_progress(n_song, total, "元数据")
                        except Exception:                        # noqa: BLE001
                            pass
            conn.commit()
        finally:
            conn.close()

        # ② 指纹
        n_fp = _import_fingerprints(src, base_dir, sids, on_progress)
        return {"songs": n_song, "covers": n_cover, "fingerprints": n_fp}
    finally:
        src.close()
