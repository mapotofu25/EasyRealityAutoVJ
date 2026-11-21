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
    " pack TEXT DEFAULT '',"
    # ★ 2026-10-27 曲库识别增强：手工标注（bpm_manual/anchors/segments），JSON。
    " manual TEXT DEFAULT '',"
    # ★★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：能量段落表随包导入。
    #   没有它 ⇒ 导入的歌没有段落色带、也不按段落切素材（本机扫的歌有，所以"不一样"）。
    #   ⚠ 必须是**独立列**，不能塞 `manual.segments`（那是多 BPM 分段网格）。
    " sections TEXT DEFAULT '', sections_raw TEXT DEFAULT '',"
    # ★ 2026-11-18：身份字段与段落来源标记随包导入（ERLIB format 新增列）。
    " fp_id TEXT DEFAULT '', file_md5 TEXT DEFAULT '',"
    " sections_auto INTEGER DEFAULT 1)",
    "CREATE INDEX IF NOT EXISTS idx_imp_name ON songs(file_name)",
    # ★ 2026-10-27 曲库识别增强：导入歌的波形（zlib 压缩 JSON BLOB）。
    "CREATE TABLE IF NOT EXISTS waveforms ("
    " song_id INTEGER PRIMARY KEY, data BLOB)",
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
    # ★ 2026-10-31：改成**通用补列**（以后再加列只往这个列表里加一项）。
    for _col, _ddl in (("manual", " manual TEXT DEFAULT ''"),
                       ("sections", " sections TEXT DEFAULT ''"),
                       ("sections_raw", " sections_raw TEXT DEFAULT ''"),
                       ("fp_id", " fp_id TEXT DEFAULT ''"),
                       ("file_md5", " file_md5 TEXT DEFAULT ''"),
                       ("sections_auto", " sections_auto INTEGER DEFAULT 1")):
        if _col in cols:
            continue
        try:
            conn.execute("ALTER TABLE songs ADD COLUMN" + _ddl)
            conn.commit()
        except Exception as e:                                   # noqa: BLE001
            try:
                print("[imported_lib] 迁移补列 %s 失败（下次打开会重试）：%s"
                      % (_col, e))
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


def add_song(conn, sid, meta, cover_path="", pack="", manual=None, waveform=None):
    """写/覆盖一首导入歌（用于导入过程；调用方自行 commit）。

    `pack` = 这首歌来自哪个导入包（包名，不含 `.eravjlib` 扩展名）；
    留空表示"未知来源"（界面按「早期导入」显示）。
    `manual`/`waveform` 可显式传（优先于 `meta` 内的同名键）：
      · `manual`  = grid_manual dict（bpm_manual/anchors/segments），落 songs.manual；
      · `waveform` = waveform dict（{v,cps,dur,amp}），落 waveforms 表 BLOB。

    ⚠⚠ **必须显式写列名**（不要用 `INSERT ... VALUES(?,?,...)` 的裸写法）——
      2026-10-08 踩过大坑：SQLite 动态类型**不会**因"参数个数/顺序对不上列"
      而报错，列错位会**静默写错数据**（表现为"读出来全是空/串位"）。
      加列（pack/manual）后尤其危险 —— 显式列名 + 与 `_SCHEMA` 逐列对齐是唯一保险。
    """
    m = dict(meta or {})
    manual_val = manual if manual is not None else m.get("manual")
    conn.execute(
        "INSERT OR REPLACE INTO songs ("
        " song_id, file_name, file_size, duration, artist, title, rel_path,"
        " genres, styles, source, level, ai_zh, grid, cover_path, imported_at,"
        " pack, manual, sections, sections_raw, fp_id, file_md5, sections_auto)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(sid), m.get("file_name", ""), int(m.get("file_size") or 0),
         float(m.get("duration") or 0.0), m.get("artist", ""),
         m.get("title", ""), m.get("rel_path", ""),
         _j(m.get("genres")), _j(m.get("styles")), m.get("source", ""),
         m.get("level", ""), _j(m.get("ai_zh")), _j(m.get("grid")),
         cover_path or "", time.time(), str(pack or ""),
         _j(manual_val), _j(m.get("sections")), _j(m.get("sections_raw")),
         str(m.get("fp_id") or ""), str(m.get("file_md5") or ""),
         int(m.get("sections_auto", 1) or 0)))
    if waveform is not None:
        try:
            import waveform as _wf
            blob = _wf.waveform_encode(waveform)
            if blob:
                conn.execute("INSERT OR REPLACE INTO waveforms VALUES (?,?)",
                             (int(sid), sqlite3.Binary(blob)))
        except Exception:                                        # noqa: BLE001
            pass
    else:
        # ★ 2026-11-18：新包没有波形 ⇒ 删旧 BLOB，避免重导入后仍显示上一版。
        try:
            conn.execute("DELETE FROM waveforms WHERE song_id=?", (int(sid),))
        except Exception:                                        # noqa: BLE001
            pass


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
            for sid, g, manual in c.execute(
                    "SELECT song_id, grid, manual FROM songs"):
                g = _p(g)
                if not isinstance(g, dict):
                    continue
                manual = _p(manual)
                if not isinstance(manual, dict):
                    manual = None
                user_marked = bool(
                    manual and (manual.get("bpm_manual")
                                or manual.get("anchors")
                                or manual.get("segments")))
                if user_marked:
                    # ★ 2026-11-18：ERLIB 的手动 BPM/锚点/分段必须先生效，
                    #   否则 src/conf 门槛会把用户的修正整条丢掉。
                    try:
                        import beatgrid as _bg
                        g = _bg.effective_grid(g, manual, {}) or g
                    except Exception:                        # noqa: BLE001
                        pass
                if not user_marked:
                    if g.get("src") not in ("vdj", "bt"):
                        continue
                try:
                    bl = float(g.get("beat_len") or 0.0)
                    anchor = float(g.get("anchor"))
                    conf = float(g.get("conf") or 0.0)
                except Exception:                                # noqa: BLE001
                    continue
                if (not user_marked) and conf < 0.6:
                    continue
                if not (0.2 < bl < 1.5):
                    continue
                out[int(sid)] = {"bpm": g.get("bpm"), "beat_len": bl,
                                 "anchor": anchor, "conf": conf,
                                 "src": g.get("src"), "dur": g.get("dur"),
                                 "segments": list(g.get("segments") or []),
                                 "anchors": list(g.get("anchors") or []),
                                 "flip": 0.0, "manual": manual}
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ★ 2026-10-30 用户「大砍刀」：`lookup_genres()`（给 engine.set_song_genres 用）随
#   曲风识别一并删除。包里的 genres 列保留不动（老包照常导入，只是不再读）。


def lookup_sections(base_dir):
    """`{sid: (raw_sections, dur, grid)}` —— 给 `engine.set_song_sections()` 用。

    ★★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：本机扫的歌，段落表存在
      `cfg["music_meta"][path]["sections"]`；导入的歌**没有 path**（文件不在这台机器）
      ⇒ `engine._sections_for_sid()` 按路径取，永远取不到 ⇒ 导入歌没有段落色带、
      也不按段落切素材。这里按 `song_id` 补一条取数路径。

    只返回**真的有段落**的歌（空列表直接跳过，别让引擎缓存一堆空表）。
    `dur`/`grid` 一起带出去 —— `sections.normalize()` 要靠它们把 t0 吸附到八拍头。
    """
    out = {}
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return out
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            for sid, secs, dur, grid in c.execute(
                    "SELECT song_id, sections, duration, grid FROM songs"):
                if not secs:
                    continue
                try:
                    raw = json.loads(secs)
                except Exception:                                # noqa: BLE001
                    continue
                if not isinstance(raw, list) or not raw:
                    continue
                g = _p(grid)
                if not isinstance(g, dict):
                    g = {}
                try:
                    d = float(dur or g.get("dur") or 0.0)
                except (TypeError, ValueError):
                    d = 0.0
                out[int(sid)] = (raw, d, g)
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return out


def waveforms_all(base_dir):
    """`{sid: waveform}` —— **全部**导入歌的波形（一次连接、逐条解码）。

    ★★ 2026-10-31（用户「eravj 的导入怎么没有波形」）：曲库面板的「波形」列要
      显示导入歌的包络，而导入歌**本机没有音频文件**（伪路径），拿不到 `.wj`。
      逐首调 `waveform_of()` 会为每首歌开一次 SQLite 连接（百首就是百次）；
      这里一次读全表 —— 反正 BLOB 本来就是整批用的。
    解不开的单条直接跳过（一条坏数据不该让整列都没波形）。
    """
    out = {}
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return out
        import waveform as _wf
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            for sid, data in c.execute("SELECT song_id, data FROM waveforms"):
                try:
                    w = _wf.waveform_decode(bytes(data))
                except Exception:                                # noqa: BLE001
                    continue
                if w and (w.get("amp") or []):
                    out[int(sid)] = w
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        return {}
    return out


def sections_display(base_dir):
    """`{sid: [{"t0","t1","tier",...}, ...]}` —— 曲库面板画段落色带用。

    与 `lookup_sections()` 的区别：那个是给引擎"按段落切素材"的（带 dur/grid 去吸附），
    这里只要**展示**用的原始段落表（tier/区间），不做任何归一化。
    """
    out = {}
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return out
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            for sid, secs in c.execute("SELECT song_id, sections FROM songs"):
                if not secs:
                    continue
                try:
                    raw = json.loads(secs)
                except Exception:                                # noqa: BLE001
                    continue
                if isinstance(raw, list) and raw:
                    out[int(sid)] = [x for x in raw if isinstance(x, dict)]
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        return {}
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
            for (sid, name, dur, artist, title, gen, grid, pack, manual,
                 fp_id, file_md5, sec_auto) in c.execute(
                    "SELECT song_id, file_name, duration, artist, title, "
                    "genres, grid, pack, manual, fp_id, file_md5, sections_auto "
                    "FROM songs ORDER BY song_id"):
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
                manual = _p(manual)
                if not isinstance(manual, dict):
                    manual = None
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
                    "manual": manual,
                    "fp_id": str(fp_id or ""),
                    "file_md5": str(file_md5 or ""),
                    "sections_auto": int(sec_auto if sec_auto is not None else 1),
                })
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        pass
    return out


def song_meta_by_sid(base_dir):
    """`{sid: {"path":"", "title", "artist", "genres": [], "imported": True}}`。

    ★★ 2026-10-31（用户「怎么输出预览不显示我在放那首歌」）：
      导入包的歌**不在 `cfg["music_meta"]` 里**（本机根本没有音频文件，按 `song_id`
      存在 `imported_meta.db`）。主窗口那张 `_song_meta_by_id` 只从本机曲库 + 码表
      构建 ⇒ 导入歌被认出来（`recognized_song_id` 有了）却查不到歌名
      ⇒ 输出预览的 HUD 只有「BPM / 能量」那一行，**没有 🎵 歌名**
      （用户截图：正在放 `(10) [天音] Fly Anima.flac`，HUD 就是不显示它）。

    2026-10-30「大砍刀」把 `lookup_genres` 那一整块删掉时，连 title/artist 的并入
    也一起没了 —— 曲风确实不该恢复，但 **title/artist 是 HUD 要用的**，必须补回来。

    `path` 故意留空：导入歌没有本机路径，任何按路径取文件的地方都不该拿到假路径。
    """
    out = {}
    for r in display_rows(base_dir):
        sid = int(r.get("sid") or 0)
        if not sid:
            continue
        out[sid] = {
            "path": "",
            "title": r.get("title") or r.get("file_name") or "",
            "artist": r.get("artist") or "",
            "genres": [],            # 曲风已随"大砍刀"下线，别在这里复活
            "imported": True,
        }
    return out


def cover_path_of(base_dir, sid):
    """这首导入歌的封面文件路径（没有则点封面时找不到文件，界面自行兜底）。"""
    p = os.path.join(cover_dir(base_dir), "%d.jpg" % int(sid))
    return p if os.path.isfile(p) else ""


def manual_of(base_dir, sid):
    """这首导入歌的手工标注 `grid_manual` dict（没有/损坏返回 None）。"""
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return None
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            r = c.execute("SELECT manual FROM songs WHERE song_id=?",
                          (int(sid),)).fetchone()
            if not r:
                return None
            v = _p(r[0])
            return v if isinstance(v, dict) else None
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        return None


def waveform_of(base_dir, sid):
    """这首导入歌的波形 `waveform` dict（{v,cps,dur,amp}；没有/损坏返回 None）。"""
    try:
        if not os.path.isfile(meta_db_path(base_dir)):
            return None
        c = sqlite3.connect(meta_db_path(base_dir), timeout=30.0)
        try:
            r = c.execute("SELECT data FROM waveforms WHERE song_id=?",
                          (int(sid),)).fetchone()
            if not r:
                return None
            import waveform as _wf
            return _wf.waveform_decode(bytes(r[0]))
        finally:
            c.close()
    except Exception:                                            # noqa: BLE001
        return None


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
                c.execute("DELETE FROM waveforms WHERE song_id=?", (sid,))
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


def purge_sids(base_dir, sids, protect_sids=()):
    r"""删掉这些**导入歌**（元数据 + 波形 + 封面 + **指纹**）。返回统计 dict。

    ★★ 2026-10-31（用户「识别不到」的真正机理，主理人实测）：
      同一段音频如果在 `fingerprints.db` 里有**两套 song_id**（本机一套 + 导入一套，
      只因为路径写法不同），`fp.FingerprintDB.match()` 是按 song_id 分票的 ⇒
      票被精确摊成 50/50（现场实测 6236 : 6236）⇒ 两边都够不到门槛 ⇒
      **这首歌怎么都认不出来**。所以"以后不再导入"只能防新的，已经导进来的必须能清掉。

    ## 安全约束（硬性，别"优化"掉）
    · `sids` 必须是调用方用 `erlib.dupdetect` 判出来的"与本机同名同长"的那些，
      本函数**不再自行判重**（判据只在一个地方，避免两处漂移）；
    · **`protect_sids`（本机自己的 song_id）里的 sid：只删导入元数据，绝不删指纹** ——
      否则删掉的就是本机那首歌的指纹，直接把歌从识别里抹掉；
    · 删前备份 `imported_meta.db`（`*.bak-时间戳`，几百 KB）；
    · **不备份 `fingerprints.db`**（现场 287MB，备份机制会整份复制，C 盘会爆）——
      靠"只按 song_id 精确删 + 打印清单"保证可回溯（重新导入包即可重建）；
    · 只按 **song_id** 删指纹，**绝不**按 hash（hash 会误伤本机那首的同内容指纹）。
    """
    import shutil
    sids = [int(x) for x in (sids or ())]
    protect = set(int(x) for x in (protect_sids or ()))
    out = {"songs": 0, "waveforms": 0, "covers": 0, "fingerprints": 0,
           "backup": "", "protected": []}
    if not sids:
        return out
    mp = meta_db_path(base_dir)
    # ① 备份元数据库（小文件；指纹库不备，见 docstring）
    if os.path.isfile(mp):
        try:
            # ★ 2026-10-31（QA 提的隐患）：原来只到**秒** ⇒ 同一秒内连点两次会覆盖上一份，
            #   把"清理前快照"直接盖成清理后状态（退路没了）。现在带毫秒 + 递增避让，
            #   保证**永不覆盖**已有备份。
            _stamp = (time.strftime("%Y%m%d-%H%M%S")
                      + "-%03d" % int((time.time() % 1) * 1000))   # 带毫秒（末 3 位）
            bak = "%s.bak-%s" % (mp, _stamp)
            _i = 1
            while os.path.exists(bak):
                bak = "%s.bak-%s-%d" % (mp, _stamp, _i)
                _i += 1
            shutil.copy2(mp, bak)
            out["backup"] = bak
            print("[imported_lib] 已备份导入元数据：%s" % os.path.basename(bak))
        except Exception as e:                                   # noqa: BLE001
            # 备份失败 ⇒ **不删**（宁可清不掉，也不能在没有退路的情况下删用户数据）
            print("[imported_lib] 备份失败，已中止清理：%s" % e)
            return out
    # ② 删三张表里的行（逐 sid 显式写条件）
    try:
        c = open_meta(base_dir)
        try:
            for sid in sids:
                try:
                    out["waveforms"] += max(0, int(c.execute(
                        "DELETE FROM waveforms WHERE song_id=?", (sid,)).rowcount))
                    out["songs"] += max(0, int(c.execute(
                        "DELETE FROM songs WHERE song_id=?", (sid,)).rowcount))
                except Exception as e:                           # noqa: BLE001
                    print("[imported_lib] 删除导入歌 %d 失败：%s" % (sid, e))
            c.commit()
        finally:
            c.close()
    except Exception as e:                                       # noqa: BLE001
        # ★ 2026-10-31（QA 提的隐患）：原来只 print 就往下走 ⇒ 步骤④照删指纹，
        #   结果留下"导入行还在、指纹没了"的孤儿行（面板看得到、永远认不出）。
        #   与"备份失败中止"同一策略：**元数据没删干净就绝不继续删指纹**。
        print("[imported_lib] 打开/删除导入元数据失败，已中止清理（未动指纹）：%s" % e)
        return out
    # ③ 封面文件（`imported_covers/<sid>.jpg`）
    for sid in sids:
        try:
            p = os.path.join(cover_dir(base_dir), "%d.jpg" % sid)
            if os.path.isfile(p):
                os.remove(p)
                out["covers"] += 1
        except Exception:                                        # noqa: BLE001
            pass
    # ④ 指纹（主库）：**只按 song_id**，且跳过本机自己的 sid
    try:
        db = os.path.join(base_dir, "fingerprints.db")
        if os.path.isfile(db):
            c = sqlite3.connect(db, timeout=30.0)
            try:
                rows = []
                for sid in sids:
                    if sid in protect:
                        out["protected"].append(sid)
                        continue
                    n = int(c.execute(
                        "SELECT COUNT(*) FROM fingerprints WHERE song_id=?",
                        (sid,)).fetchone()[0])
                    rows.append((sid, n))
                print("[imported_lib] 将删指纹：%s"
                      % ", ".join("sid=%d(%d 条)" % (s, n) for s, n in rows))
                c.executemany("DELETE FROM fingerprints WHERE song_id=?",
                              [(s,) for s, _n in rows])
                c.commit()
                out["fingerprints"] = sum(n for _s, n in rows)
            finally:
                c.close()
    except Exception as e:                                       # noqa: BLE001
        print("[imported_lib] 删除重复指纹失败：%s" % e)
    return out


def clear(base_dir):
    """清空所有导入歌（元数据 + 封面）。指纹**不删**（它和本机曲库混在一张表里）。"""
    try:
        c = open_meta(base_dir)
        try:
            c.execute("DELETE FROM songs")
            c.execute("DELETE FROM waveforms")
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
