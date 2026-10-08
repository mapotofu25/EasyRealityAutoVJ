# -*- coding: utf-8 -*-
r"""导入曲库（`.eravjlib`）在**演出机本机**的落地存储 —— 元数据 + 封面。

## 为什么单独存一份，不塞进 `cfg["music_meta"]`
`cfg["music_meta"]` 是**按文件路径**索引的，而且：
1. 曲库界面会遍历它把歌列出来 ⇒ 导入的歌**本机没有文件**，会显示成一堆不存在的条目；
2. 曲库扫描用 `p not in meta` 判断"要不要分析" ⇒ 如果按假路径塞进去，逻辑会乱。

⇒ 所以导入的数据**另存一份**、按 **`song_id`** 索引。
识别接管时，只要把它**合并**进引擎那两张查表
（`set_song_grids` / `set_song_genres`）就够了 —— 引擎那边一行都不用改。

## 存放位置（都在 `app_base_dir()` 下）
```
imported_meta.db          song_id → 曲风/节拍网格/文件名…（SQLite）
imported_covers/<sid>.jpg 封面图
fingerprints.db           指纹**直接并进主库**（识别路径不用改，最省事）
```
"""

import json
import os
import sqlite3
import time

DB_NAME = "imported_meta.db"
COVER_DIR = "imported_covers"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS songs ("
    " song_id INTEGER PRIMARY KEY,"
    " file_name TEXT, file_size INTEGER, duration REAL,"
    " artist TEXT, title TEXT, rel_path TEXT,"
    " genres TEXT, styles TEXT, source TEXT, level TEXT, ai_zh TEXT,"
    " grid TEXT, cover_path TEXT, imported_at REAL,"
    # ★ 2026-10-09（用户要求②③）：记录这首歌来自**哪个导入包**（包名）。
    #   `pack=''` = 加这一列之前导入的旧数据 ⇒ 界面显示「早期导入」。
    #   ⚠ 新增列一律放**最后**（与 ALTER TABLE ADD COLUMN 追加到末尾一致），
    #     保证 `add_song` 的显式列名顺序与建表顺序对齐（防"列插反"静默写错）。
    " pack TEXT DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS idx_imp_name ON songs(file_name)",
)


def meta_db_path(base_dir):
    return os.path.join(base_dir, DB_NAME)


def cover_dir(base_dir):
    return os.path.join(base_dir, COVER_DIR)


def _migrate(conn):
    """幂等迁移：把旧库补上后来新增的列。

    ★ 2026-10-09：新增 `songs.pack`（导入包名）。

    为什么迁移必须放这里（`open_meta` 每次打开都跑）：
      · `_SCHEMA` 是 `CREATE TABLE IF NOT EXISTS` —— 对**已存在**的旧表
        **不会**加新列；所以老用户升级后必须靠 ALTER 补列。
      · `open_meta` 是**所有**读写导入库的统一入口（导入 / 展示 / 删除都走它），
        放这里 ⇒ 无论哪条路径先跑，列都已经就位（不会出现"某条路径读到没列的库"）。

    ⚠ 幂等：`PRAGMA table_info` 查到已有该列就**什么都不做** —— 重复调用安全，
      绝不会执行第二次 ALTER（第二次 ALTER 会抛 "duplicate column"）。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(songs)")}
    except Exception:                                            # noqa: BLE001
        return
    if "pack" not in cols:
        try:
            conn.execute("ALTER TABLE songs ADD COLUMN pack TEXT DEFAULT ''")
            conn.commit()
        except Exception as e:                                   # noqa: BLE001
            # 罕见：他处持有写锁（WAL/长事务）导致 ALTER 超时失败。此处只能吞掉异常，
            #   但**当次**会话读 pack 仍会失败（各读取路径各自兜底返回空），
            #   下次 open_meta 会重试并自愈。打一行日志便于排查"导入歌偶发消失"。
            try:
                print("[imported_lib] 迁移补列 pack 失败（下次打开会重试）：%s" % e)
            except Exception:                                    # noqa: BLE001
                pass


def open_meta(base_dir):
    """打开（必要时创建）导入元数据库。调用方负责 close。"""
    p = meta_db_path(base_dir)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:                                            # noqa: BLE001
        pass
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    _migrate(conn)
    return conn


def count(base_dir):
    """本机导入了多少首歌。"""
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return 0
        c = sqlite3.connect(meta_db_path(base_dir), timeout=10.0)
        try:
            return int(c.execute("SELECT COUNT(*) FROM songs").fetchone()[0])
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        return 0


def _j(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


def _p(v):
    """JSON 字段还原成 list/dict；失败就原样返回。"""
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except Exception:                                        # noqa: BLE001
            return v
    return v


def add_song(conn, sid, meta, cover_path="", pack=""):
    """写/覆盖一首导入歌（用于导入过程；调用方自行 commit）。

    `pack` = 这首歌来自哪个导入包（包名，不含 `.eravjlib` 扩展名）；
    留空表示"未知来源"（界面按「早期导入」显示）。

    ⚠⚠ **必须显式写列名**（不要用 `INSERT ... VALUES(?,?,...)` 的裸写法）——
      2026-10-08 踩过大坑：SQLite 动态类型**不会**因"参数个数/顺序对不上列"
      而报错，列错位会**静默写错数据**（表现为"读出来全是空/串位"）。
      加列（pack）后尤其危险 —— 显式列名 + 与 `_SCHEMA` 逐列对齐是唯一保险。
    """
    m = dict(meta or {})
    conn.execute(
        "INSERT OR REPLACE INTO songs ("
        " song_id, file_name, file_size, duration, artist, title, rel_path,"
        " genres, styles, source, level, ai_zh, grid, cover_path, imported_at,"
        " pack) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(sid), m.get("file_name", ""), int(m.get("file_size") or 0),
         float(m.get("duration") or 0.0), m.get("artist", ""),
         m.get("title", ""), m.get("rel_path", ""),
         _j(m.get("genres")), _j(m.get("styles")), m.get("source", ""),
         m.get("level", ""), _j(m.get("ai_zh")), _j(m.get("grid")),
         cover_path or "", time.time(), str(pack or "")))


def lookup_grids(base_dir):
    """`{sid: grid}` —— 给 `engine.set_song_grids()` 用。

    ⚠ 过滤条件必须与 ui_main 里本机曲库那条**完全一致**（同一套门槛），
      否则导入的歌会比本机扫的歌"更容易被网格接管"，出现行为不一致：
        · 只认可信来源 `vdj` / `bt`（local 小节头不可信）
        · `conf >= 0.6` 且 `beat_len ∈ (0.2, 1.5)`
    """
    out = {}
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return out
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            for sid, g, flip in c.execute(
                    "SELECT song_id, grid, '' FROM songs"):
                g = _p(g)
                if not isinstance(g, dict):
                    continue
                if g.get("src") not in ("vdj", "bt"):
                    continue
                try:
                    bl = float(g.get("beat_len") or 0.0)
                    anchor = float(g.get("anchor"))
                    conf = float(g.get("conf") or 0.0)
                except Exception:                                # noqa: BLE001
                    continue
                if conf < 0.6 or not (0.2 < bl < 1.5):
                    continue
                out[int(sid)] = {"bpm": g.get("bpm"), "beat_len": bl,
                                 "anchor": anchor, "conf": conf,
                                 "src": g.get("src"), "dur": g.get("dur"),
                                 "flip": 0.0}
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return out


def lookup_genres(base_dir):
    """`{sid: {"genres":[...], "ai_zh":[...], "artist":..., "title":...}}` ——
    给 `engine.set_song_genres()` 用（指纹识别到歌时取精确曲风）。"""
    out = {}
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return out
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            for sid, gen, aizh, artist, title, name in c.execute(
                    "SELECT song_id, genres, ai_zh, artist, title, file_name "
                    "FROM songs"):
                out[int(sid)] = {
                    "genres": _p(gen) or [],
                    "ai_zh": _p(aizh) or [],
                    "artist": artist or "", "title": title or "",
                    "file_name": name or "",
                }
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return out


def display_rows(base_dir):
    """曲库面板**展示**用：每首导入歌一行（含 bpm / 时长 / 封面文件路径 / 包名）。

    返回 `[{"sid", "file_name", "title", "artist", "genres", "bpm",
             "dur", "cover", "pack"}]`（读不到时返回 `[]`，绝不抛）。
    `pack` = 导入包名（`''` = 早期导入，界面显示「早期导入」）。

    ⚠ 与 `lookup_grids()` 的区别：**这里不做可信度过滤** ——
      展示要如实反映包里的数据（哪怕网格不可信，也要让人看到"有哪些数据"）；
      而 `lookup_grids` 是给引擎"接管拍位"用的，必须过同一套门槛。

    ★★ 2026-10-09 修正（真实缺陷）：本函数**改走 `open_meta()`**，不再直连 ——
      因为它 SELECT 了新列 `pack`，而**老用户升级后、还没点过任何导入**时，
      库结构仍是旧的（15 列、无 pack）⇒ 直连会在 `no such column: pack` 上抛，
      又被外层 `except: pass` 吞掉 ⇒ 返回 `[]` ⇒ **曲库面板里导入的歌"全部消失"**
      （静默、零报错，最难查）。走 `open_meta()` 后内部会先跑 `_migrate()` ⇒ 列必就位。
      ⇒ 本函数**不再是"只读"语义**：首次对旧库调用会顺带完成结构迁移（建表/加列/commit/WAL）。
    """
    out = []
    try:
        # 库文件都不存在就没必要建库（保留这个前置检查：避免"只展示"也把库创建出来）
        if not os.path.isfile(meta_db_path(base_dir)):
            return out
        # ⚠ 必须走 open_meta（内含 _migrate）——直连会在未迁移的旧库上因 pack 列缺失而抛
        c = open_meta(base_dir)
        try:
            for (sid, name, dur, artist, title, gen, grid, pack) in c.execute(
                    "SELECT song_id, file_name, duration, artist, title, "
                    "genres, grid, pack FROM songs ORDER BY song_id"):
                g = _p(grid)
                if not isinstance(g, dict):
                    g = {}
                bpm = g.get("bpm")
                try:
                    bpm = float(bpm) if bpm else None
                except (TypeError, ValueError):
                    bpm = None
                try:
                    du = float(g.get("dur") or dur or 0.0) or None
                except (TypeError, ValueError):
                    du = None
                cp = os.path.join(cover_dir(base_dir), "%d.jpg" % int(sid))
                out.append({
                    "sid": int(sid),
                    "file_name": name or "",
                    "title": title or "",
                    "artist": artist or "",
                    "genres": _p(gen) or [],
                    "bpm": bpm,
                    "dur": du,
                    "cover": cp if os.path.isfile(cp) else "",
                    "pack": pack or "",
                })
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return out


def cover_path_of(base_dir, sid):
    """这首导入歌的封面文件路径（没有则点封面时找不到文件，界面自行兜底）。"""
    p = os.path.join(cover_dir(base_dir), "%d.jpg" % int(sid))
    return p if os.path.isfile(p) else ""


def remove_songs(base_dir, sids):
    """从导入库删掉这些歌（**曲风/封面/指纹一起删**）。返回删掉的歌数。

    为什么要连指纹一起删：只删元数据的话，识别仍然会 match 命中这些歌，
    但查不到 meta ⇒ 表现成"认出来但没名字"，行为不一致。

    ⚠ 删的是主库 `fingerprints.db` 里这些 `song_id` 的行。正常情况导入包的
      sid 与本机自扫的 sid 不会撞（sid = 完整路径的 md5，两台机器路径不同）；
      万一撞了，重新扫描本机那首即可恢复。
    """
    sids = [int(x) for x in (sids or ())]
    if not sids:
        return 0
    n = 0
    # ① 元数据
    try:
        c = open_meta(base_dir)
        try:
            for sid in sids:
                cur = c.execute("DELETE FROM songs WHERE song_id=?", (sid,))
                n += max(0, int(cur.rowcount))
            c.commit()
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    # ② 封面文件
    for sid in sids:
        try:
            p = os.path.join(cover_dir(base_dir), "%d.jpg" % sid)
            if os.path.isfile(p):
                os.remove(p)
        except Exception:                                        # noqa: BLE001
            pass
    # ③ 指纹（主库）
    try:
        db = os.path.join(base_dir, "fingerprints.db")
        c = sqlite3.connect(db, timeout=30.0)
        try:
            c.executemany("DELETE FROM fingerprints WHERE song_id=?",
                          [(s,) for s in sids])
            c.commit()
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return n


def clear(base_dir):
    """清空所有导入歌（元数据 + 封面）。指纹**不删**（它和本机曲库混在一张表里）。"""
    try:
        c = open_meta(base_dir)
        try:
            c.execute("DELETE FROM songs")
            c.commit()
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    try:
        import shutil
        d = cover_dir(base_dir)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:                                            # noqa: BLE001
        pass
