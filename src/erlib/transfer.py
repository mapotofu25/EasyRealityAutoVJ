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


_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def existing_sidecars(path):
    """`path` 的 SQLite 附属文件里**实际存在**的（`-wal` / `-shm` / `-journal`）。"""
    return [str(path) + s for s in _SQLITE_SIDECARS if os.path.isfile(str(path) + s)]


def purge_sidecars(path):
    """只删 `path` 的附属文件（**不碰** `path` 本身）。返回删掉的基名列表。"""
    gone = []
    for p in existing_sidecars(path):
        try:
            os.remove(p)
            gone.append(os.path.basename(p))
        except OSError:
            pass
    return gone


def purge_sqlite_files(base):
    """删掉 `base` **及其**全部附属文件。返回删掉的基名列表（调试/测试用）。"""
    gone = []
    try:
        if os.path.isfile(str(base)):
            os.remove(str(base))
            gone.append(os.path.basename(str(base)))
    except OSError:
        pass
    gone.extend(purge_sidecars(base))
    return gone


def verify_package(path, expect_songs):
    """导出**收尾自检**（必须在 `LibWriter.close()` 之后调用）。

    三条判据，任一不满足就抛 `RuntimeError` —— 宁可让用户看到"导出失败"，
    也**绝不**交付一个"别人只拷主文件就丢数据"的包：

    1. `path-wal` / `path-shm` 必须**不存在** —— 否则用户复制时漏掉 `-wal`
       就等于丢数据（2026-11-01 微信截图现场：4 KB 主文件 + 12.68 MB `-wal`）；
    2. `PRAGMA journal_mode` 不能是 `wal` —— WAL 是写在**文件头**里的，
       哪怕 checkpoint 成 0 字节，别人一碰它又会把 `-shm`/`-wal` 重新创建出来；
    3. `SELECT COUNT(*) FROM songs` 必须等于**本次应写入的歌数** —— 防止
       "看着导出成功、打开是空的"。

    返回实测信息 `{'journal_mode', 'songs', 'fingerprints', 'size'}`。
    """
    import sqlite3
    side = existing_sidecars(path)
    if side:
        raise RuntimeError(
            "导出收尾自检失败：还留着附属文件 %s（别人只拷主文件会丢数据）"
            % ", ".join(os.path.basename(x) for x in side))
    try:
        conn = sqlite3.connect(
            "file:%s?mode=ro" % str(path).replace("?", "%3f"),
            uri=True, timeout=10.0)
    except Exception as e:                                       # noqa: BLE001
        raise RuntimeError("导出收尾自检失败：临时包打不开（%s）: %s"
                           % (os.path.basename(str(path)), e))
    try:
        try:
            jm = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            n = int(conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0])
            f = int(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
        except Exception as e:                                   # noqa: BLE001
            raise RuntimeError("导出收尾自检失败：临时包读不出数据（%s）" % e)
    finally:
        try:
            conn.close()
        except Exception:                                        # noqa: BLE001
            pass
    if jm == "wal":
        raise RuntimeError(
            "导出收尾自检失败：包还是 WAL 模式（别人打开会重新生成 -wal/-shm）")
    if n != int(expect_songs):
        raise RuntimeError(
            "导出收尾自检失败：包里 %d 首，应为 %d 首" % (n, int(expect_songs)))
    try:
        size = int(os.path.getsize(str(path)))
    except OSError:
        size = 0
    return {"journal_mode": jm, "songs": n, "fingerprints": f, "size": size}


def export_subset(work, out_path, sids, on_progress=None, app_ver="erlib"):
    r"""把工作库 `work` 里 `sids` 这些歌导出成新包 `out_path`（**原子写**）。

    返回 `LibWriter.stats()`：`{'songs': n, 'fingerprints': n, 'covers': n}`。

    ⚠ 只写勾选的歌 —— **没勾的一字节都不进包**（这是"多选导出"的本体）。
    ⚠ 逐首 `commit` 攒 5 首一次，避免每首一次 fsync（大库导出快很多）。

    ## ★★ 2026-11-01（用户「我导出来怎么就是这个 / 没有单独的 eravjlib 文件吗」）
    现场：DJ 导出后目录里是 **三个**文件 —— `我的曲库.eravjlib` 4 KB（空壳）
    \+ `-shm` 32 KB + `-wal` **12.68 MB**。根因不是"收尾没合并"（那步早就修了），
    而是 **`LibWriter` 以 WAL 模式直接建在最终文件名上** ⇒ 导出**过程中**目录里
    就有一个 4 KB 的空壳顶着最终名，数据全在越来越多的 `-wal` 里。
    用户只要在导出没结束时就去看目录 / 把文件发出去，拿到的一定是坏包
    （而且"文件名看起来是对的"，最容易被坑）。

    ## 现在的做法（原子写）
    1. 真正写入 **`<out_path>.tmp`**（同目录、同卷 ⇒ `os.replace` 才是原子的）；
    2. 收尾（`wal_checkpoint(TRUNCATE)` + `journal_mode=DELETE` + `close()`）之后
       先跑 `verify_package()` **自检**；
    3. 自检通过才 `os.replace(tmp, out_path)` —— 最终文件名**要么不存在，
       要么就是一个完整可用的包**，不存在"半成品"这个状态；
    4. 任何异常/中断（含 Ctrl-C）⇒ 删 tmp 及其 `-wal`/`-shm`/`-journal`，
       **最终文件名不受影响**（覆盖导出失败时，旧包原样保留）。
    """
    sids = set(int(x) for x in sids)
    out_path = str(out_path)
    # ★★ 2026-10-19 审计修复：**输出路径不能是工作库本身**。
    #   `resume=False` 的 LibWriter 会先 DELETE 三张表；指向自身 ⇒ 先把正在读的库清空，
    #   导出结果 0 首，而"扫了一小时"的成果已经没了（不可逆）。
    if os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(os.path.abspath(str(work))):
        raise ValueError("导出路径不能是工作库本身，请换一个文件名 / 目录。")
    # 临时名 = 最终名 + `.tmp`（同一目录 ⇒ 同一卷 ⇒ os.replace 原子）
    tmp_path = out_path + ".tmp"
    # 上次导出崩溃/断电可能留下 tmp 三件套 —— 先清掉，别把旧垃圾当本次产物。
    purge_sqlite_files(tmp_path)
    src = LibReader(work)
    n, total = 0, len(sids)
    try:
        # ★★ 2026-10-31（用户「导出的文件不要带位置」）：导出包里**一律不带本机路径**。
        #   ① `meta.root` 写空 —— 它是"扫描这台机器上的曲库目录"，`<曲库目录>\…` 到了
        #      演出机上毫无意义，还会让人以为"文件应该在那儿"；
        #   ② 每首歌的 `songs.src_path`（本机绝对路径）在下面逐行剥掉。
        #   ⚠ 工作库自己**保留**这两样 ⇒ 详情页试听照样能播（需求原话：
        #     "这里还是得要能播放的，只是说导出的文件不要带位置"）。
        #   ★★ 另外（T7 审计修复 1）：`resume=False` ⇒ `LibWriter` 会**清空**三张数据表
        #      （见 format.LibWriter.__init__）。这样覆盖同名导出时，上一次导出的歌
        #      不会残留在新包里（否则用户会看到"没勾的歌也进了包"、stats 也虚报）。
        w = LibWriter(tmp_path, root="", resume=False, app_ver=app_ver)
        try:
            n, total = 0, len(sids)
            # ★ 2026-11-18：`src.songs()` 是 import_seq DESC（新→旧）；新 writer 会
            #   按迭代顺序重新编号。原来直接迭代 ⇒ 导出包再打开变成旧→新（顺序反转）。
            for s in reversed(list(src.songs())):
                sid = int(s["song_id"])
                if sid not in sids:
                    continue
                s = dict(s)
                s.pop("src_path", None)          # ← 本机绝对路径，绝不进包
                w.add_song(sid, s, src.fingerprints_of(sid),
                           src.cover(sid), commit=False,
                           waveform=src.waveform(sid))
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
            # ★★ 2026-10-31（用户「怎么有三个文件」）：光 checkpoint **不够** ——
            #   目录里还是躺着 `-wal`/`-shm` 两个附属文件，用户看到"三个文件"以为
            #   导出了三份曲库。根因：**WAL 模式是写在库文件头里的**，
            #   哪怕 checkpoint 成 0 字节，任何程序（包括 AutoVJ 用 `mode=ro` 打开）
            #   一碰它，SQLite 又会把 `-shm`/`-wal` 创建出来。
            #   ⇒ 交付物必须切回**回滚日志模式**（`journal_mode=DELETE`）：
            #     库里不再有 WAL，`-wal`/`-shm` 永远不会再生，别人只读打开也不用写目录。
            try:
                w.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                w.conn.execute("PRAGMA journal_mode=DELETE")
            except Exception:                                    # noqa: BLE001
                pass
            st = w.stats()
            if on_progress is not None:
                try:
                    on_progress(n, total)
                except Exception:                                # noqa: BLE001
                    pass
        finally:
            w.close()
            # ⚠ 必须在 close() **之后**删：连接还开着时删 WAL 会让 SQLite 状态错乱。
            #   ★ 2026-10-31：库里已经是 `journal_mode=DELETE`（见上），所以这两个
            #   文件**一定是残留**（数据早已并进主文件）⇒ 直接清干净。
            #   ★★ 2026-11-01：清的是 **tmp** 的附属文件；最终名此刻**还没出现**。
            purge_sidecars(tmp_path)

        # ★★ 2026-11-01：**收尾自检** —— 必须在 close() 之后（那时 -wal/-shm 才真的
        #   消失，库也才自包含）。任一判据不满足就抛错，交给下面的 except 清理 tmp。
        verify_package(tmp_path, n)
        if n != total:
            # ⚠ 勾选的歌里有工作库中已经不存在的（界面上的 `missing_sids()` 也是这个
            #   意思）—— 这不是"导出坏了"，所以只提示不阻断。
            try:
                print("[erlib] 勾选 %d 首，实际导出 %d 首（%d 首在工作库里找不到）"
                      % (total, n, total - n))
            except Exception:                                    # noqa: BLE001
                pass

        # ★★ 2026-11-01：**自检通过才改名**（同目录 ⇒ 同一卷 ⇒ os.replace 原子）。
        #   ⇒ 最终文件名只有两种状态：不存在 / 一个完整可用的包。没有中间态。
        os.replace(tmp_path, out_path)
        # 旧版本导出在同一目录留下的 `X.eravjlib-wal` / `-shm` 垃圾（就是用户截图里的
        # "三个文件"）—— 新包是 DELETE 模式、自包含，这些残留只会误导人，顺手清掉。
        gone = purge_sidecars(out_path)
        if gone:
            try:
                print("[erlib] 已清理同目录的旧残留：%s" % ", ".join(gone))
            except Exception:                                    # noqa: BLE001
                pass
        return st
    except BaseException:
        # ★★ 2026-11-01：任何异常/中断（含 KeyboardInterrupt、磁盘满、自检不过）⇒
        #   把半成品 tmp 连同 `-wal`/`-shm`/`-journal` 一起删干净。
        #   **最终文件名绝不出现半成品**；覆盖导出失败时旧包原样保留。
        purge_sqlite_files(tmp_path)
        raise
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
def _import_fingerprints(src, base_dir, sids, on_progress=None, skip=None):
    """把包里的指纹并进主库 `fingerprints.db`。

    ★★ 2026-10-31（用户「导入重复的歌也没提醒」）：新增 `skip` ——
      要**跳过**的 song_id 集合（本机已有的那些，由 `erlib.dupdetect` 判出来）。
      ⚠ 必须在这里过滤，不能只在外层跳过元数据：`iter_fingerprints()` 吐的是
        **整包**的指纹，外层筛歌对它无效 ⇒ 跳过的歌会照样把指纹写进主库，
        指纹库里继续留着两套 id（正是这次要修的现场故障）。
      ⚠ 默认 `None`（=不过滤），保持旧调用方（`tools/_erlib_fpfix_verify.py` 里的
        旧写法复刻版只有 4 个参数）照原样可替换。

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
        #    ★ 2026-10-31：`skip`（本机已有那些）在这一层过滤（见 docstring）。
        skip = skip or ()
        for batch in src.iter_fingerprints():
            rows = [(h, int(s), int(o)) for s, h, o in batch if int(s) not in skip]
            if not rows:
                continue
            conn.executemany(
                "INSERT INTO fingerprints(hash, song_id, offset) VALUES (?,?,?)",
                rows)
            n += len(rows)
            if on_progress is not None and n % 200000 < len(rows):
                try:
                    on_progress(n, 0, "指纹")
                except Exception:                                # noqa: BLE001
                    pass
        conn.commit()
    finally:
        conn.close()
    return n


def import_into(pkg_path, base_dir, on_progress=None, clear_first=False,
                skip_sids=None):
    """把 `.eravjlib` **并进本机**（演出机侧）。返回统计 dict。

    四件事：
    1. **版本校验** —— 指纹算法版本不一致直接拒绝（旧指纹拿去匹配只会永远认不出）；
    2. **元数据** → `<base_dir>/imported_meta.db`（按 song_id 索引，**不碰** `music_meta`）；
    3. **封面** → `<base_dir>/imported_covers/<song_id>.jpg`；
    4. **指纹** → `<base_dir>/fingerprints.db`（并进主库，识别链路无需改动）。

    ⚠ `clear_first=True` 会先清空"之前导入的"（不影响本机自己扫的曲库）。
    ★★ 2026-10-31（用户「导入重复的歌也没提醒」）：`skip_sids` = **本机已有的那些
      song_id**（由 `erlib.dupdetect` 判出，界面上让用户选「跳过这些（推荐）」）。
      这些歌**既不写 `imported_meta.db`、也不写指纹** ——
      否则曲库面板会多出一行 📦（用户看到重复），指纹库里也会有两套编号。
      ⚠ 只跳过"这一次"的写入，**不删**用户之前已经导入的数据（不动既有指纹）。
      `skip_sids=None` ⇒ 与旧行为完全一致（全部导入）。

    ★★ 2026-11-01（用户 17:20「节拍网格（包 6 / 本机 7）导入失败」）：
      **节拍网格版本不一致只警告、不拒绝**，理由：
        · 指纹（`fp_schema_version`）错了 ⇒ 拿去匹配**永远认不出**，属于"能导入但
          不识别"的隐性故障，必须拦；
        · 网格（`grid_ver`）错了 ⇒ 只是包里的 BPM/anchor/段落按旧算法算的（BPM 可能
          偏几 BPM），歌**照样能放、能认、能切**；用户重扫 / 用新版 ERLIB 重导 /
          详情页手改 BPM 都能救 ⇒ 拦死等于"升级一次版本号，所有旧包全废"。
      判据落在 `stats["grid_stale"]`（= **这一包里有几首**来自旧网格算法；0 = 全一致），
      界面据此多提示一行（见 `ui_main._on_erlib_imported`）。**不抛异常**。
    """
    import imported_lib as IL
    from erlib.format import _grid_ver as _grid_ver_now

    src = LibReader(pkg_path)
    try:
        ok, msg = src.check_versions()
        if not ok:
            raise ValueError(msg)
        # 包级网格版本（旧包只在 meta 里写 grid_ver，逐首的 grid JSON 里没有）
        try:
            pkg_grid = src.meta().get("grid_ver")
        except Exception:                                        # noqa: BLE001
            pkg_grid = None
        cur_grid = _grid_ver_now()
        # ★ 2026-11-01：界面要显示"包 v6 / 本机 v7"，但**不能把版本号写死**在文案里
        #   （GRID_VER 再升到 8 时，导 v7 的包会被提示成"（v6）"，误导用户）。
        #   所以把 `check_versions()` 记下的 `(包版本, 本机版本)` 原样带出去。
        #   一致 / 取不到 ⇒ None（界面据此退化文案，绝不出现 "vNone"）。
        grid_versions = getattr(src, "grid_stale", None)
        if clear_first:
            IL.clear(base_dir)
        # ★ 2026-10-31：要跳过的 song_id（容错：参数里混进脏值不该让整个导入失败）
        skip = set()
        for _x in (skip_sids or ()):
            try:
                skip.add(int(_x))
            except (TypeError, ValueError):
                pass
        sids = [int(s["song_id"]) for s in src.songs() if int(s["song_id"]) not in skip]
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
        n_song = n_cover = n_skip = 0
        # ★ 2026-11-01：网格来自旧算法的首数（只警告，见 docstring）
        n_grid_stale = 0
        total = len(sids)
        try:
            for s in src.songs():
                sid = int(s["song_id"])
                # ★ 2026-10-31：本机已有的那些歌**整首跳过**（元数据 + 封面 + 指纹都不写）
                if sid in skip:
                    n_skip += 1
                    # ★ 2026-11-26：跳过重复曲目时，包里的手动 Build Up 不能一起丢。
                    #   按文件名暂存，演出机同名本地曲目仍能生效。
                    try:
                        IL.add_skipped_build_up(conn, s.get("file_name"), s.get("build_up"))
                    except Exception:                            # noqa: BLE001
                        pass
                    continue
                # ★ 2026-11-01：逐首判"网格是否旧算法算的"。
                #   优先取 grid JSON 里的 `grid_ver`（新包逐首都带），缺失时退回
                #   包级 `meta.grid_ver`（旧包只写 meta）；两者都没有 ⇒ 这首没网格，
                #   不计入（不然会把"纯元数据包"全算成过期）。
                _g = s.get("grid")
                _gv = _g.get("grid_ver") if isinstance(_g, dict) else None
                if _gv is None:
                    _gv = pkg_grid
                if _gv is not None and _gv != cur_grid:
                    n_grid_stale += 1
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
                if not cp:
                    # ★ 2026-11-18：新包没封面 ⇒ 清掉旧 sidecar，否则重导入后
                    #   还显示上一版封面。
                    try:
                        _oldc = os.path.join(cd, "%d.jpg" % sid)
                        if os.path.isfile(_oldc):
                            os.remove(_oldc)
                    except OSError:
                        pass
                IL.add_song(conn, sid, s, cp, pack=pack,
                            waveform=src.waveform(sid))
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

        # ② 指纹（★ 2026-10-31：跳过的那些连指纹也不写，见 docstring）
        if skip:
            n_fp = _import_fingerprints(src, base_dir, sids, on_progress, skip=skip)
        else:
            n_fp = _import_fingerprints(src, base_dir, sids, on_progress)
        return {"songs": n_song, "covers": n_cover, "fingerprints": n_fp,
                "skipped": n_skip, "grid_stale": n_grid_stale,
                "grid_versions": grid_versions}
    finally:
        src.close()
