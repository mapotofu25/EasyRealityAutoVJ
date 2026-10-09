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
import sqlite3
import time

FORMAT_VERSION = 1          # .eravjlib 自身的结构版本
# 封面边长（像素）。⚠ 这是 erlib **自用**的值，与主程序 `music_cover.COVER_SIZE`(=144)
# **不同** —— 旧注释误称"与主程序一致"（2026-10-10 T7 审计订正）。两边产物都是
# "居中裁方 + 缩到 N×N 的 JPEG"，显示端（列表/面板）还会再缩到 56~72px，
# 故无需一致；真要一致请改这里的值。
COVER_SIZE = 200


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
    " scanned_at REAL)",
    "CREATE TABLE IF NOT EXISTS fingerprints ("
    " song_id INTEGER NOT NULL, hash TEXT NOT NULL, offset INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_fp_hash ON fingerprints(hash)",
    "CREATE INDEX IF NOT EXISTS idx_fp_song ON fingerprints(song_id)",
    "CREATE TABLE IF NOT EXISTS covers ("
    " song_id INTEGER PRIMARY KEY, data BLOB)",
)


# ==========================================================================
# 写入端（DJ 端的 erlib 用）
# ==========================================================================
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
        self.conn = sqlite3.connect(path, check_same_thread=False)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:                                        # noqa: BLE001
            pass
        for stmt in _SCHEMA:
            self.conn.execute(stmt)
        self.conn.commit()
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
                self.conn.execute("DELETE FROM songs")
                self.conn.commit()
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

    def add_song(self, sid, meta_dict, fps=(), cover_bytes=None, commit=True):
        """写一首歌（幂等：同一 song_id 重复写会整体替换）。

        `meta_dict` 允许包含 rel_path/file_name/file_size/duration/artist/title/
        genre_raw/genres/styles/source/level/ai_zh/grid 这些键（缺失按空处理）。
        `genres/styles/ai_zh/grid` 允许直接给 list/dict，这里统一转 JSON 存。
        """
        m = dict(meta_dict or {})

        def _j(v):
            if v is None:
                return ""
            if isinstance(v, (list, dict)):
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            return str(v)

        self.conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
        self.conn.execute("DELETE FROM covers WHERE song_id=?", (sid,))
        self.conn.execute(
            "INSERT OR REPLACE INTO songs VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, m.get("rel_path", ""), m.get("file_name", ""),
             int(m.get("file_size") or 0), float(m.get("duration") or 0.0),
             m.get("artist", ""), m.get("title", ""), m.get("genre_raw", ""),
             _j(m.get("genres")), _j(m.get("styles")), m.get("source", ""),
             m.get("level", ""), _j(m.get("ai_zh")), _j(m.get("grid")),
             len(list(fps or ())), len(cover_bytes or b""),
             time.time()))
        rows = [(sid, h, int(o)) for h, o in (fps or ())]
        if rows:
            self.conn.executemany(
                "INSERT INTO fingerprints VALUES (?,?,?)", rows)
        if cover_bytes:
            self.conn.execute("INSERT OR REPLACE INTO covers VALUES (?,?)",
                              (sid, sqlite3.Binary(cover_bytes)))
        if commit:
            self.conn.commit()

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
        """返回 (ok, 说明)。

        ⚠ 指纹/网格**算法版本**不匹配时必须拒绝导入 —— 旧指纹拿去匹配只会
          永远认不出（还很难查）。这是本格式存在的意义之一。
        """
        m = self.meta()
        fv = m.get("format_version")
        if fv != FORMAT_VERSION:
            return False, ("包格式版本 %r 与本机 %r 不一致，请用新版 erlib 重新导出"
                           % (fv, FORMAT_VERSION))
        bad = []
        cur_fp, cur_grid = _fp_ver(), _grid_ver()
        if m.get("fp_schema_version") != cur_fp:
            bad.append("指纹算法（包 %r / 本机 %r）"
                       % (m.get("fp_schema_version"), cur_fp))
        if m.get("grid_ver") != cur_grid:
            bad.append("节拍网格（包 %r / 本机 %r）"
                       % (m.get("grid_ver"), cur_grid))
        if bad:
            return False, ("算法版本不一致：%s —— 请用新版 erlib 重新导出"
                           % "；".join(bad))
        return True, "版本一致"

    def count(self):
        try:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM songs").fetchone()[0])
        except Exception:                                        # noqa: BLE001
            return 0

    def songs(self):
        """逐首 yield dict（JSON 字段已解回 list/dict）。"""
        cur = self.conn.execute("SELECT * FROM songs")
        cols = [d[0] for d in cur.description]
        for row in cur:
            d = dict(zip(cols, row))
            for k in ("genres", "styles", "ai_zh", "grid"):
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
