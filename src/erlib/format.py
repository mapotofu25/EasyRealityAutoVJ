# -*- coding: utf-8 -*-
r"""`.eravjlib`（erlib 曲库分析包）—— **数据格式层**。

DJ 端（`erlib` 小软件）**写**，演出机（ERAVJ）**读**，两边共用这一份定义，
避免"格式漂移"（一边改了字段另一边不知道）。

## 为什么是 SQLite 单文件而不是 zip
- **单文件好传**（U 盘 / 网盘 / 微信都能发）；
- **能增量**：同一首歌重复导入是 `INSERT OR REPLACE`，不产生重复数据；
- **中断可续**：扫描到一半崩了，下次 `LibWriter(resume=True)` 接着写，
  已扫完的歌不会重扫（大曲库要跑一小时，这个很值钱）；
- 导入时**直接读**，不用先解压一遍。

## 表结构
```
meta          key/value —— 格式版本、指纹版本、网格版本、导出时间（导入时校验）
songs         song_id 主键 + 文件名/大小/时长/艺人/曲名/曲风/节拍网格
fingerprints  song_id / hash / offset —— 与主程序 fingerprints.db **同构**
covers        song_id / JPEG 字节
```

## ⚠ song_id 的语义（关键，别想错）
`song_id = int(md5(完整路径)[:8], 16)` —— 来自 **DJ 机器上那个文件的路径**。
演出机上**不存在**这个文件也没关系：它只是个**编号**，用来把
「指纹 → 曲风/封面/节拍网格」串起来。演出机靠**指纹**认出编号，而不是靠路径找文件。
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import time

FORMAT_VERSION = 1          # .eravjlib 自身的结构版本
# 封面边长（像素）。⚠ 这是 erlib **自用**的值，与主程序 `music_cover.COVER_SIZE`(=144)
# **不同** —— 旧注释误称"与主程序一致"（2026-10-10 T7 审计订正）。两边产物都是
# "居中裁方 + 缩到 N×N 的 JPEG"，显示端（列表/面板）还会再缩到 56~72px，
# 故无需一致；真要一致请改这里的值。
COVER_SIZE = 200


def win_sort_key(name):
    """**Windows 资源管理器**式的自然排序键（`Track 2` 排在 `Track 10` 前面）。

    放这里（而不是 scanner）是因为**三处都要用**：
      · `scanner.find_audio` 决定文件夹里的扫描顺序；
      · `_migrate` 给老库回填 `import_seq` 时按它发号；
      · `LibReader.songs()` 读**只读老包**（没有 import_seq 列）时按它排序。
    三处口径必须一致，否则同一个库在不同入口看到的顺序不一样。
    """
    parts = re.split(r"(\d+)", str(name).lower())
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def song_id_of(path):
    """与主程序 `MusicScanThread._one` 完全一致的算法（两处必须一致，否则指纹对不上）。"""
    return int(hashlib.md5(str(path).encode("utf-8")).hexdigest()[:8], 16)


def _fp_ver():
    try:
        import fp as _fp
        return int(getattr(_fp, "FP_SCHEMA_VERSION", -1))
    except Exception:                                            # noqa: BLE001
        return -1


def _grid_ver():
    try:
        import beatgrid as _bg
        return int(getattr(_bg, "GRID_VER", -1))
    except Exception:                                            # noqa: BLE001
        return -1


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta ("
    " key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS songs ("
    " song_id INTEGER PRIMARY KEY,"
    " rel_path TEXT, file_name TEXT, file_size INTEGER, duration REAL,"
    " artist TEXT, title TEXT, genre_raw TEXT,"
    " genres TEXT, styles TEXT, source TEXT, level TEXT, ai_zh TEXT,"
    " grid TEXT, fp_count INTEGER DEFAULT 0, cover_size INTEGER DEFAULT 0,"
    " scanned_at REAL,"
    # ★ 2026-10-27 曲库识别增强：新增 `manual`（用户手工标注：bpm_manual/anchors/segments）。
    #   放**最后**（与 ALTER TABLE ADD COLUMN 追加到末尾一致，见 _migrate）。
    " manual TEXT,"
    # ★★ 2026-10-31（用户「erlib 的扫描要和 eravj 一样」）：能量段落表也随包走。
    #   主程序扫描时会算 `sections`（八拍对齐）/ `sections_raw`（原始边界），
    #   erlib 之前完全没算 ⇒ 导入到演出机后没有段落色带、也不按段落切素材。
    #   ⚠ 只能放**新列**，绝不能塞进 `manual.segments` —— 那是"多 BPM 分段网格"，
    #     语义不同（见 beatgrid.effective_grid）。2026-10-31 踩过。
    " sections TEXT, sections_raw TEXT,"
    # ★★ 2026-10-31（用户「这里还是得要能播放的，只是说导出的文件不要带位置」）：
    #   `src_path` = **本机**音频绝对路径，只服务于"工作库里点详情页试听"。
    #   `transfer.export_subset()` 导出时会**整列丢掉**（meta.root 也清空）⇒
    #   发给演出机的 `.eravjlib` 里没有任何本机位置；而本机照样能播放。
    #   ⚠ 绝不能拿 `rel_path` 兼这个职 —— 它是**跟着包走**的相对路径，
    #     一旦写成 `<曲库目录>\\…` 绝对路径就等于把位置带进导出包了。
    " src_path TEXT,"
    # ★ 2026-11-06：导入顺序（列表按它倒序显示 ⇒ 新导入的在最上面）。
    #   老库由 `_migrate` 按 `scanned_at` 回填。
    " import_seq INTEGER,"
    # ★ 2026-11-26：手动 Build Up（{start,beats}），从 ERLIB 1.3 起随包导出。
    " build_up TEXT)",
    "CREATE TABLE IF NOT EXISTS fingerprints ("
    " song_id INTEGER NOT NULL, hash TEXT NOT NULL, offset INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_fp_hash ON fingerprints(hash)",
    "CREATE INDEX IF NOT EXISTS idx_fp_song ON fingerprints(song_id)",
    "CREATE TABLE IF NOT EXISTS covers ("
    " song_id INTEGER PRIMARY KEY, data BLOB)",
    # ★ 2026-10-27 曲库识别增强：新增 `waveforms` 表（zlib 压缩 JSON BLOB，与 covers 同构）。
    "CREATE TABLE IF NOT EXISTS waveforms ("
    " song_id INTEGER PRIMARY KEY, data BLOB)",
)


def _migrate(conn):
    """幂等迁移：给老库补 `songs.manual` 列（`waveforms` 表由 _SCHEMA 的 CREATE IF NOT EXISTS 兜底）。

    ★ `_SCHEMA` 是 `CREATE TABLE IF NOT EXISTS` —— 对已存在的旧表**不会**加新列，
      老用户升级后必须靠 ALTER 补列。每次打开 LibWriter 都跑一遍（幂等，见 _migrate 调用）。
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(songs)")}
    except Exception:                                            # noqa: BLE001
        return
    # ★ 2026-10-31：改成**通用补列**（以后再加列只往这个表里写一行）。
    # ★★★ 2026-11-06（用户：「Lib 的歌曲列表改成导入顺序，新导入的堆在最上面」）：
    #   `song_id` 是 `md5(路径)[:8]`（不是自增）⇒ 表里**没有插入顺序**。必须单开一列
    #   `import_seq`（写入时递增发号）。⚠ 必须 INTEGER：TEXT 列会按字典序比较，
    #   "10" < "9" 排序就乱了。
    #   老库这里一次性回填：按 `scanned_at` 升序发号（越早扫的号越小 ⇒ 列表里越靠下）
    #   ⇒ 升级后老歌的上下关系仍符合"导入先后"，新导入的自然排到最上面。
    if "import_seq" not in cols:
        try:
            conn.execute("ALTER TABLE songs ADD COLUMN import_seq INTEGER")
            conn.commit()
        except Exception as e:                                   # noqa: BLE001
            try:
                print("[erlib.format] 迁移补列 import_seq 失败（下次打开会重试）：%s" % e)
            except Exception:                                    # noqa: BLE001
                pass
        try:
            # ★ 回填口径：**按文件夹里的自然顺序**发号，而且**倒着发**
            #   （列表是 `import_seq DESC`）⇒ 最终显示 = 资源管理器里的 (01)(02)(03)… 从上到下。
            #   ⚠ 用 `scanned_at` 发号会变成"最后扫到的在最上面"——用户看到的是一堆
            #     (09)(08)(07) 倒着排，正是他说的"看着乱"。
            rows = list(conn.execute(
                "SELECT song_id, COALESCE(NULLIF(rel_path,''), file_name, '')"
                " FROM songs WHERE import_seq IS NULL"))
            rows.sort(key=lambda r: win_sort_key(r[1] or ""), reverse=True)
            base = conn.execute(
                "SELECT COALESCE(MAX(import_seq),0) FROM songs").fetchone()[0] or 0
            for i, (sid, _k) in enumerate(rows, 1):
                conn.execute("UPDATE songs SET import_seq=? WHERE song_id=?",
                             (int(base) + i, sid))
            conn.commit()
        except Exception as e:                                   # noqa: BLE001
            try:
                print("[erlib.format] 回填 import_seq 失败：%s" % e)
            except Exception:                                    # noqa: BLE001
                pass

    for col in ("manual", "sections", "sections_raw", "src_path", "build_up"):
        if col in cols:
            continue
        try:
            conn.execute("ALTER TABLE songs ADD COLUMN %s TEXT" % col)
            conn.commit()
        except Exception as e:                                   # noqa: BLE001
            # 罕见：他处持写锁导致 ALTER 失败，本次吞掉、下次打开重试自愈。
            try:
                print("[erlib.format] 迁移补列 %s 失败（下次打开会重试）：%s"
                      % (col, e))
            except Exception:                                    # noqa: BLE001
                pass


# ==========================================================================
# 写入端（DJ 端的 erlib 用）
# ==========================================================================
def _make_db_writable(path):
    """Clear a stale Windows read-only flag on the DB and its WAL sidecars.

    Why: a .eravjlib copied from a zip/backup/CD often keeps the read-only
    attribute. SQLite then opens it read-only and every UPDATE raises
    "attempt to write a readonly database" (the Build Up save failure).  This
    only touches the writable work/export file we are about to open; LibReader
    (used for user packages) stays strictly read-only.
    """
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        try:
            if os.path.isfile(p) and not (os.stat(p).st_mode & stat.S_IWRITE):
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


class LibWriter:
    """写入 `.eravjlib`。

    ⚠ 默认 `resume=True`：文件已存在就**接着写**（不删库），配合 `existing_ids()`
      跳过已扫完的歌 —— 中断续扫的关键。
    """

    def __init__(self, path, root="", resume=True, app_ver=""):
        self.path = path
        self.root = root
        self.closed = False
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # A stale read-only attribute on the work DB or its -wal/-shm sidecars
        # makes SQLite reject every write. Clear it before opening a writer.
        _make_db_writable(path)
        fresh = not os.path.isfile(path)
        if fresh:
            resume = False
        # ⚠⚠⚠ `check_same_thread=False` **必须**：
        #   `LibWriter` 是在**界面主线程**里创建的（用户点了「开始扫描」），
        #   而真正的写入发生在**扫描线程**（`ScanThread`）里。
        #   sqlite3 默认 `check_same_thread=True` ⇒ 每一首 `add_song` 都会抛
        #     `ProgrammingError: SQLite objects created in a thread can only be
        #      used in that same thread`
        #   而调用方（`Scanner.scan_tree`）把异常吞了 ⇒ **界面只显示"失败 N 首"，
        #   工作库里一首歌都没有**（2026-10-08 现场踩到，用户报"扫描结束不显示出来"）。
        #   ⚠ 写入侧目前是**单线程串行**（`scan_tree` 的 for 循环），
        #     所以关掉线程检查是安全的；将来若改成并行写，**必须自己加锁**。
        #   （主程序的 `fp.FingerprintDB` 早就是 `check_same_thread=False` —— 同一个坑。）
        self.conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        try:
            # ★ 2026-11-16（ERLIB 同步）：两个连接同时写同一个库时，默认 5s busy
            #   超时会直接 "database is locked"；30s + busy_timeout 宁可等也不丢歌。
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:                                        # noqa: BLE001
            pass
        for stmt in _SCHEMA:
            self.conn.execute(stmt)
        _migrate(self.conn)           # 幂等补列（老库加 songs.manual）
        self.conn.commit()
        # ★ 2026-11-06：导入序号发号器（列表按 `import_seq DESC` 显示）。
        #   每次写完都往后加；`add_song` 里**已存在的行保留原号**（重扫不跳到最上面）。
        try:
            self._seq_next = int(self.conn.execute(
                "SELECT COALESCE(MAX(import_seq),0) FROM songs").fetchone()[0] or 0) + 1
        except Exception:                                        # noqa: BLE001
            self._seq_next = 1
        # ★★ 2026-10-10（T7 审计修复 1）：`resume=False` = "要一个干净的包"。
        #   导出时目标文件名可能已存在（用户覆盖同名导出），此时**必须清空三张数据表**，
        #   否则 `CREATE TABLE IF NOT EXISTS` + `INSERT OR REPLACE` 只会覆盖本次真正写到
        #   的 song_id —— 上一次导出残留的歌会留在包里（QA 端到端复现：
        #   导出子集 {3} 后 count=3 而非 1，且 stats 虚报）。
        #   ⚠ 只清 songs/fingerprints/covers，**保留 meta 表**（紧接着会写入本次版本信息）。
        #   ⚠ 与 `resume=True`（工作库续扫）互斥：续扫**绝不**走这里（否则会删掉已扫成果）。
        if not resume:
            try:
                self.conn.execute("DELETE FROM fingerprints")
                self.conn.execute("DELETE FROM covers")
                self.conn.execute("DELETE FROM waveforms")
                self.conn.execute("DELETE FROM songs")
                self.conn.commit()
                self._seq_next = 1           # ★ 清库 ⇒ 发号器归零（否则新包第一首是 300+）
            except Exception:                                    # noqa: BLE001
                pass
        self.created_at = time.time()
        # 版本信息：每次打开都刷新（用的是"当前这份程序"的算法版本）
        self._set("format_version", FORMAT_VERSION)
        self._set("fp_schema_version", _fp_ver())
        self._set("grid_ver", _grid_ver())
        self._set("app_version", app_ver)
        self._set("root", root)
        if resume:
            self._set("updated_at", self.created_at)
        else:
            self._set("created_at", self.created_at)
            self._set("updated_at", self.created_at)
        self.conn.commit()

    def _set(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                          (k, json.dumps(v, ensure_ascii=False)))

    # ---- 扫描端用 ----
    def existing_ids(self):
        """已经写好的 song_id 集合（续扫时用它跳过）。"""
        try:
            return {int(r[0]) for r in
                    self.conn.execute("SELECT song_id FROM songs")}
        except Exception:                                        # noqa: BLE001
            return set()

    def add_song(self, sid, meta_dict, fps=(), cover_bytes=None, commit=True,
                 manual=None, waveform=None):
        """写一首歌（幂等：同一 song_id 重复写会整体替换）。

        `meta_dict` 允许包含 rel_path/file_name/file_size/duration/artist/title/
        genre_raw/genres/styles/source/level/ai_zh/grid/manual/src_path 这些键
        （缺失按空处理）。
        `src_path` = 本机绝对路径（**只给本机试听用**，导出时会被丢弃，见 `_SCHEMA`）。
        `genres/styles/ai_zh/grid/manual` 允许直接给 list/dict，这里统一转 JSON 存。
        `manual`/`waveform` 也可显式传（优先于 `meta_dict` 内的同名键）：
          · `manual`  = grid_manual dict（bpm_manual/anchors/segments）；
          · `waveform` = waveform dict（{v,cps,dur,amp}），落 `waveforms` 表 BLOB。
        """
        m = dict(meta_dict or {})

        def _j(v):
            if v is None:
                return ""
            if isinstance(v, (list, dict)):
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            return str(v)

        manual_val = manual if manual is not None else m.get("manual")
        self.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
        self.conn.execute("DELETE FROM covers WHERE song_id=?", (sid,))
        self.conn.execute("DELETE FROM waveforms WHERE song_id=?", (sid,))
        # ★★★ 2026-11-05：**按 `src_path` 清掉同路径的旧记录**（不同 `song_id`）。
        #   为什么必须做：`song_id_of()` 是 `md5(绝对路径)[:8]`，路径一变 id 就变。
        #   而库里存着 2026-11-01 那批老记录 —— 它们的 `src_path` 早已被更新成现在的
        #   绝对路径，`song_id` 却还是按**老路径**算的旧值 ⇒ 与 `song_id_of(当前路径)`
        #   对不上。于是重扫时 `INSERT OR REPLACE` 认不出是同一首，**新增一行**，
        #   旧行原封不动留着。实测这个库里 9 首歌**每首都有 2 条**（18 行 9 首）。
        #   后果不只是"列表难看"：演出端按 `song_id` 认歌，两条 id 都在库里，
        #   可能认到旧那条（分段是旧算法算的），而且导出 `.eravjlib` 会重复。
        #   ⇒ 写入前先按路径删掉 id 不同的那一行（连同它的指纹/封面/波形）。
        #   ⚠ 只按**非空** `src_path` 精确匹配，不做模糊；`src_path` 为空（导入包里的歌，
        #     只有 rel_path）不走这条，避免误删不同目录的同名文件。
        _sp = (m.get("src_path") or "").strip()
        if _sp:
            try:
                _old = [r[0] for r in self.conn.execute(
                    "SELECT song_id FROM songs WHERE src_path=? AND song_id<>?",
                    (_sp, sid))]
                for _oid in _old:
                    self.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (_oid,))
                    self.conn.execute("DELETE FROM covers WHERE song_id=?", (_oid,))
                    self.conn.execute("DELETE FROM waveforms WHERE song_id=?", (_oid,))
                    self.conn.execute("DELETE FROM songs WHERE song_id=?", (_oid,))
            except Exception:                                    # noqa: BLE001
                pass                                             # 清旧行失败不影响本次写入
        # ★ 导入序号：**已存在的行保留原号**（重扫/重新分析不该把它顶到列表最上面），
        #   新行才发新号（见 `__init__` 的 `_seq_next`）。
        try:
            _row = self.conn.execute(
                "SELECT import_seq FROM songs WHERE song_id=?", (sid,)).fetchone()
        except Exception:                                        # noqa: BLE001
            _row = None
        if _row is not None and _row[0] is not None:
            _seq = int(_row[0])
        else:
            _seq = int(getattr(self, "_seq_next", 1))
            self._seq_next = _seq + 1
        # ★ 显式列名（**绝不裸 INSERT VALUES**）：加 manual 列后列数变了，
        #   裸写法会静默串列（2026-10-08 踩过同款坑）。
        self.conn.execute(
            "INSERT OR REPLACE INTO songs ("
            " song_id, rel_path, file_name, file_size, duration, artist, title,"
            " genre_raw, genres, styles, source, level, ai_zh, grid, fp_count,"
            " cover_size, scanned_at, manual, sections, sections_raw, src_path,"
            " import_seq, build_up)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, m.get("rel_path", ""), m.get("file_name", ""),
             int(m.get("file_size") or 0), float(m.get("duration") or 0.0),
             m.get("artist", ""), m.get("title", ""), m.get("genre_raw", ""),
             _j(m.get("genres")), _j(m.get("styles")), m.get("source", ""),
             m.get("level", ""), _j(m.get("ai_zh")), _j(m.get("grid")),
             len(list(fps or ())), len(cover_bytes or b""),
             time.time(), _j(manual_val), _j(m.get("sections")),
             _j(m.get("sections_raw")), m.get("src_path", ""), _seq,
             _j(m.get("build_up"))))
        rows = [(sid, h, int(o)) for h, o in (fps or ())]
        if rows:
            self.conn.executemany(
                "INSERT INTO fingerprints VALUES (?,?,?)", rows)
        if cover_bytes:
            self.conn.execute("INSERT OR REPLACE INTO covers VALUES (?,?)",
                              (sid, sqlite3.Binary(cover_bytes)))
        if waveform is not None:
            try:
                import waveform as _wf
                blob = _wf.waveform_encode(waveform)
                if blob:
                    self.conn.execute("INSERT OR REPLACE INTO waveforms VALUES (?,?)",
                                      (sid, sqlite3.Binary(blob)))
            except Exception:                                    # noqa: BLE001
                pass
        if commit:
            self.conn.commit()

    def update_sections(self, sid, sections, sections_raw=None):
        """只更新**能量段落**两列（其它字段原样保留）。返回命中行数。

        ★ 2026-10-31（用户「照 ERAVJ 详情页重做」）：详情页新增「按 BPM 重新分段」，
          重算结果要落库；不能用 `add_song` 整体替换（那要求把 grid/指纹/封面全传一遍，
          少传一个就把数据洗掉）。和 `update_manual` 一样走"只改这几列"的路子。

        `sections_raw` 不传时按 `sections` 写（详情页重算时两者都算，正常都传）。
        """
        try:
            def _j(v):
                if v is None:
                    return ""
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            cur = self.conn.execute(
                "UPDATE songs SET sections=?, sections_raw=? WHERE song_id=?",
                (_j(sections),
                 _j(sections if sections_raw is None else sections_raw),
                 int(sid)))
            self.conn.commit()
            return int(cur.rowcount or 0)
        except Exception as e:                                   # noqa: BLE001
            try:
                print("[erlib.format] update_sections 失败：%s" % e)
            except Exception:                                    # noqa: BLE001
                pass
            return 0

    def update_genres(self, sid, r):
        """只更新一首歌的**曲风相关列**（「联网补跑曲风」用）。返回是否命中一行。

        ⚠⚠ **绝不能走 `add_song`**：它会先删指纹/封面再整条重写 ——
          补跑曲风时那些（还有 `grid`）必须**原样保留**，只改曲风那几列。
        ⚠ `artist`/`title` 查不到时**保留原值**（别把扫描阶段读到的元数据抹掉）。
        """
        r = dict(r or {})

        def _j(v):
            if v is None:
                return ""
            if isinstance(v, (list, dict)):
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            return str(v)

        try:
            cur = self.conn.execute(
                "SELECT artist, title FROM songs WHERE song_id=?",
                (int(sid),)).fetchone()
            a0 = (cur[0] or "") if cur else ""
            t0 = (cur[1] or "") if cur else ""
            cur2 = self.conn.execute(
                "UPDATE songs SET artist=?, title=?, genre_raw=?, genres=?, "
                "source=?, level=? WHERE song_id=?",
                (r.get("artist") or a0, r.get("title") or t0,
                 r.get("genre", ""), _j(r.get("genres")),
                 r.get("source", ""), r.get("level", ""), int(sid)))
            return max(0, int(cur2.rowcount))
        except Exception:                                        # noqa: BLE001
            return 0

    def update_manual(self, sid, manual):
        """只更新一首歌的**手工标注列**（详情页保存用）。返回是否命中一行。

        ⚠ 与 `update_genres` 同理，**绝不能走 `add_song`**（会删指纹/封面/波形）；
          这里只改 `songs.manual`，其余列原样保留。
        """
        def _j(v):
            if v is None:
                return ""
            if isinstance(v, (list, dict)):
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            return str(v)

        try:
            cur = self.conn.execute(
                "UPDATE songs SET manual=? WHERE song_id=?",
                (_j(manual), int(sid)))
            self.conn.commit()
            return max(0, int(cur.rowcount))
        except Exception:                                        # noqa: BLE001
            return 0

    def commit(self):
        try:
            self.conn.commit()
        except Exception:                                        # noqa: BLE001
            pass

    def delete_songs(self, sids):
        """从工作库删掉这些歌（**songs + fingerprints + covers 一起删**）。

        返回实际删掉的**歌数**（`songs` 表的命中行数）。
        供界面「删除勾选的歌」用 —— 下次扫描会重扫它们；**已导出的包不受影响**。

        ⚠ 为什么指纹/封面必须一起删：只删 `songs` 会留下"孤儿指纹"
          （占体积、且 `fp_count`/统计对不上）；只删指纹则歌还在列表里。
        """
        sids = [int(x) for x in (sids or ())]
        if not sids:
            return 0
        n = 0
        for sid in sids:
            cur = self.conn.execute(
                "DELETE FROM songs WHERE song_id=?", (sid,))
            n += max(0, int(cur.rowcount))
            self.conn.execute(
                "DELETE FROM fingerprints WHERE song_id=?", (sid,))
            self.conn.execute("DELETE FROM covers WHERE song_id=?", (sid,))
            self.conn.execute("DELETE FROM waveforms WHERE song_id=?", (sid,))
        self.conn.commit()
        return n

    def stats(self):
        try:
            n = self.conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
            f = self.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
            c = self.conn.execute("SELECT COUNT(*) FROM covers").fetchone()[0]
            return {"songs": int(n), "fingerprints": int(f), "covers": int(c)}
        except Exception:                                        # noqa: BLE001
            return {"songs": 0, "fingerprints": 0, "covers": 0}

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._set("updated_at", time.time())
            self.commit()
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:                                        # noqa: BLE001
            pass
        try:
            self.conn.close()
        except Exception:                                        # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


# ==========================================================================
# 读取端（ERAVJ 导入时用）
# ==========================================================================
class LibReader:
    """读 `.eravjlib`。**只读**打开，绝不改动用户的包。"""

    def __init__(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        # 只读 URI：防手滑写坏别人的包
        self.conn = sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3f"),
                                    uri=True)
        self.path = path
        # ★ 2026-11-01：`check_versions()` 的"网格版本过期"结果（只警告，不拒绝）。
        #   形状 `(包里的 grid_ver, 本机 grid_ver)`；一致/还没查过 ⇒ None。
        #   ⚠ 必须在 `__init__` 里先置 None：调用方（`transfer.import_into`、
        #     界面预览）可能在 `check_versions()` 之前就读这个属性，
        #     否则会 AttributeError（比"导入失败"更难查）。
        self.grid_stale = None

    def meta(self):
        out = {}
        try:
            for k, v in self.conn.execute("SELECT key, value FROM meta"):
                try:
                    out[k] = json.loads(v)
                except Exception:                                # noqa: BLE001
                    out[k] = v
        except Exception:                                            # noqa: BLE001
            pass
        return out

    def check_versions(self):
        """返回 (ok, 说明)；同时把"网格过期"记进 `self.grid_stale`（见下）。

        ## 为什么"网格版本"只警告、"指纹版本"仍然拒绝
        ★★ 2026-11-01（用户 17:20 截图）：
          点「导入曲库分析包」弹
            「导入失败：ValueError: 算法版本不一致：节拍网格（包 6 / 本机 7）
              —— 请用新版 erlib 重新导出」
          起因：本轮把 `beatgrid.GRID_VER` 从 6 升到 7（HOP 1024→512，修 200BPM
          被扫成 132.7 的偏差）。旧实现把「指纹版本」和「网格版本」用同一把刀砍
          （任一不一致就整包拒绝）⇒ **一升版本号，用户手上所有旧包全部进不来**。
          这是"升级版本号"的连带影响，包里的数据本身是好的。

        两种版本的严重性完全不同，所以判定必须分开：
          · `fp_schema_version`（指纹）不一致 ⇒ 指纹拿去匹配**永远认不出**
            （功能性错误，且现场极难查：能导入、就是不识别）⇒ **必须继续拒绝**；
          · `grid_ver`（节拍网格）不一致 ⇒ 只是包里的 BPM/anchor/段落可能按旧算法
            算的（BPM 可能偏差几 BPM），歌**照样能放、能认、能切**；用户可以
            重扫、或用新版 ERLIB 重导、或在详情页手改 BPM ⇒ **不该拦死**。

        优先级：`format_version`（拒绝）> `fp_schema_version`（拒绝）> `grid_ver`（只警告）。
        三者同时坏 ⇒ 仍返回 False（拒绝），此时按上面的优先级**先报 format**，
        `self.grid_stale` 不参与判定（可能还停在 None —— 诊断看返回值就够了）。
        """
        m = self.meta()
        fv = m.get("format_version")
        if fv != FORMAT_VERSION:
            return False, ("包格式版本 %r 与本机 %r 不一致，请用新版 erlib 重新导出"
                           % (fv, FORMAT_VERSION))
        cur_fp, cur_grid = _fp_ver(), _grid_ver()
        # ① 网格差异：**只记录不拒绝**（理由见 docstring）。一致 ⇒ None。
        #   ⚠ 包里**根本没写** grid_ver（很老的包）⇒ 也记 None：无法判断就当"不提醒"，
        #     免得文案里出现 "vNone" 吓人（逐首统计同样按"没网格 ⇒ 不计入"处理）。
        pkg_grid = m.get("grid_ver")
        self.grid_stale = (None if (pkg_grid is None or pkg_grid == cur_grid)
                           else (pkg_grid, cur_grid))
        # ② 指纹差异：仍然拒绝（旧指纹拿去匹配永远认不出）
        bad = []
        if m.get("fp_schema_version") != cur_fp:
            bad.append("指纹算法（包 %r / 本机 %r）"
                       % (m.get("fp_schema_version"), cur_fp))
        if bad:
            return False, ("算法版本不一致：%s —— 请用新版 erlib 重新导出"
                           % "；".join(bad))
        # ③ 走到这里说明"能导"。网格过期就带一句提醒（**不抛异常**）。
        if self.grid_stale:
            return True, ("版本一致（节拍网格来自旧算法 v%r，本机 v%r："
                          "BPM 可能偏差，建议重新导出）"
                          % (self.grid_stale[0], self.grid_stale[1]))
        return True, "版本一致"

    def count(self):
        try:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM songs").fetchone()[0])
        except Exception:                                        # noqa: BLE001
            return 0

    def songs(self):
        """逐首 yield dict（JSON 字段已解回 list/dict）。"""
        # ★★★ 2026-11-06（用户：「Lib 的歌曲列表改成导入顺序，新导入的堆在最上面」）：
        #   按 `import_seq` **倒序**（新导入的在上）。`import_seq` 为空的老行排最后
        #   （`import_seq IS NULL` 先排 0/1，NULL 当 1 ⇒ 排在有号的后面）。
        #   ⚠⚠ `LibReader` 是**只读**打开的（绝不改用户的包）⇒ 老的 `.eravjlib`
        #     没有这一列（迁移只在 `LibWriter` 里跑）。这里必须先探测列，缺列就退回
        #     "扫描时间倒序" —— 与迁移回填用的是同一个口径，看到的顺序一致。
        #     不探测的后果是 `SELECT ... ORDER BY import_seq` 直接
        #     `OperationalError: no such column`，界面报"读取工作库失败"。
        try:
            _cols = [d[1] for d in self.conn.execute("PRAGMA table_info(songs)")]
        except Exception:                                        # noqa: BLE001
            _cols = []
        if "import_seq" in _cols:
            cur = self.conn.execute(
                "SELECT * FROM songs ORDER BY (import_seq IS NULL), import_seq DESC")
            cols = [d[0] for d in cur.description]
            rows = cur
        else:
            # 老包（只读，迁移跑不了）：按**文件夹自然顺序**直接排，
            # 与"迁移回填后"的显示一致（否则同一个包在 ERLIB 和导入端看到的顺序不同）。
            cur = self.conn.execute("SELECT * FROM songs")
            cols = [d[0] for d in cur.description]
            rows = list(cur)
            try:
                _fi = cols.index("rel_path") if "rel_path" in cols else -1
                _fn = cols.index("file_name") if "file_name" in cols else -1

                def _k(r):
                    k = (r[_fi] if _fi >= 0 and r[_fi] else "") or (
                        r[_fn] if _fn >= 0 else "")
                    return win_sort_key(k or "")
                rows.sort(key=_k)
            except Exception:                                    # noqa: BLE001
                pass
        for row in rows:
            d = dict(zip(cols, row))
            for k in ("genres", "styles", "ai_zh", "grid", "manual",
                       "sections", "sections_raw", "build_up"):
                v = d.get(k)
                if isinstance(v, str) and v:
                    try:
                        d[k] = json.loads(v)
                    except Exception:                            # noqa: BLE001
                        pass
            yield d

    def iter_fingerprints(self, batch=20000):
        """批量 yield [(song_id, hash, offset), ...]，供导入时流式写库（大包不占内存）。"""
        cur = self.conn.execute(
            "SELECT song_id, hash, offset FROM fingerprints")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield [(int(a), b, int(c)) for a, b, c in rows]

    def fingerprints_of(self, sid):
        """某一首的全部 (hash, offset) —— 导出勾选子集时按歌读取用。"""
        try:
            return [(h, int(o)) for h, o in
                    self.conn.execute(
                        "SELECT hash, offset FROM fingerprints WHERE song_id=?",
                        (int(sid),))]
        except Exception:                                        # noqa: BLE001
            return []

    def cover(self, sid):
        try:
            r = self.conn.execute("SELECT data FROM covers WHERE song_id=?",
                                  (int(sid),)).fetchone()
            return bytes(r[0]) if r else None
        except Exception:                                        # noqa: BLE001
            return None

    def manual_of(self, sid):
        """某一首的手工标注 `grid_manual` dict（没有/损坏返回 None）。"""
        try:
            r = self.conn.execute("SELECT manual FROM songs WHERE song_id=?",
                                  (int(sid),)).fetchone()
            if not r:
                return None
            v = r[0]
            if isinstance(v, str) and v:
                try:
                    v = json.loads(v)
                except Exception:                                # noqa: BLE001
                    return None
            return v if isinstance(v, dict) else None
        except Exception:                                        # noqa: BLE001
            return None

    def waveform(self, sid):
        """某一首的波形 `waveform` dict（{v,cps,dur,amp}；没有/损坏返回 None）。"""
        try:
            r = self.conn.execute("SELECT data FROM waveforms WHERE song_id=?",
                                  (int(sid),)).fetchone()
            if not r:
                return None
            import waveform as _wf
            return _wf.waveform_decode(bytes(r[0]))
        except Exception:                                        # noqa: BLE001
            return None

    def close(self):
        try:
            self.conn.close()
        except Exception:                                        # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False
