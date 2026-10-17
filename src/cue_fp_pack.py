# -*- coding: utf-8 -*-
"""码表包里的**完整指纹哈希序列**：读包 → 灌进演出端指纹库（零 Qt）。

════════════════════════════════════════════════════════════════════════
为什么需要它（用户原话）
════════════════════════════════════════════════════════════════════════
    「这首歌在 erc 里做的码表包啊，理论来讲 era 导入就应该要识别到 解决一下」

`.ercue` 里原来只有 `identity.fp_id`（= 整首歌 **哈希序列** 的 sha1 前 16 hex，
**只剩 16 个字符的摘要**）。演出端只有摘要 ⇒ **认不出音频**：它必须手里有那首歌、
自己重算一遍同口径的哈希序列才能比对。歌不在演出端曲库 ⇒ 永远认不出。

现在编排端导出时把**完整指纹哈希序列**写进包里（成员 `fp/<fp_id>.bin`）；
演出端导入码表时把它**灌进自己的指纹库** ⇒ 哪怕音频不在那台机器上，放歌时
（音频从调音台/系统声音进来）照样能认出来，码表就能自动跟位。

════════════════════════════════════════════════════════════════════════
二进制格式（★ **两端契约**，逐字节实现，不许自创）
════════════════════════════════════════════════════════════════════════
包内成员名 `fp/<fp_id>.bin`；整个文件 = `zlib.compress(payload, 6)`，`payload` 为：

    偏移 0..3    magic  b"ERFP"
    偏移 4       version = 1        (uint8)
    偏移 5       hash_hex_len       (uint8，= 20)
    偏移 6..7    reserved = 0       (uint16 LE)
    偏移 8..11   count              (uint32 LE)
    偏移 12..    count 条 × 14 字节：hash 10 字节（20 hex 的前 10 字节）+ offset int32 LE
    校验         `len(payload) - 12 == count * 14`

· 哈希用 **`bytes.hex()` 还原成 20 字符小写 hex** —— 与 `fp.py` 里
  `hashlib.sha1(...).hexdigest()[:20]` **同形**，灌库后 `match()` 的 `hash IN (...)`
  才能对上（用大写/带前缀的写法会静默永不命中）；
· `offset` 单位 = **hop 帧**（44.1k / hop=2048 坐标系，即 `fp.fingerprint_audio()`
  返回的那个 offset），不是秒、不是毫秒。

⚠ **两份实现必须同步**：本文件与编排端 `EasyRealityCue/src/ercue/model/fp_pack.py`
  是同一格式的两份独立实现（两个仓库谁也不能 import 谁）。改一端必须改另一端。

════════════════════════════════════════════════════════════════════════
三条硬约束（别改回去）
════════════════════════════════════════════════════════════════════════
1. **零 Qt / 零 numpy**：只用 stdlib（`zipfile`/`zlib`/`struct`/`sqlite3`/`threading`）
   ⇒ 可无头单测（`tools/cue_fp_pack_test.py`）。
2. **解包绝不抛**：`unpack_hashes()` 对任何坏数据（截断 / 错 magic / 错 version /
   count 与长度不符 / 压缩炸弹）一律返回 `None`；**空字节流返回 `[]`**（"空"与"坏"
   是两个意思，见 `unpack_hashes()` docstring），让调用方"当作没有指纹"继续走。
3. **绝不卡 GUI**：灌 5 万条要 0.3~0.6s ⇒ 一律走 `start_ingest()` 的后台线程；
   `ingest_sheet()` 本身是同步实现（供后台线程与单测直接调用）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import threading
import time
import zipfile
import zlib

# ==========================================================================
# 常量（★ 两端契约）
# ==========================================================================
FP_DIR = "fp"                    # 包内目录（成员名固定用 "/"，**绝不用 os.sep**）
FP_EXT = ".bin"
MAGIC = b"ERFP"                  # 4 字节魔数：EasyReality FingerPrint
VERSION = 1                      # 格式版本（不兼容变更必须 +1）
RESERVED = 0                     # 恒 0
HASH_HEX_LEN = 20                # = fp.HASH_LEN（sha1 前 20 hex）
HASH_BYTES = HASH_HEX_LEN // 2   # 20 hex → 10 字节（80 bit 全保留，不截断）
HEADER_SIZE = 12                 # 4 + 1 + 1 + 2 + 4
RECORD_SIZE = HASH_BYTES + 4     # 10 + int32 = 14
COMPRESS_LEVEL = 6

#: 解包上限（防压缩炸弹）：400 万条 ≈ 56MB 原始载荷 ≈ 连续 27 小时的音频
MAX_RECORDS = 4_000_000
MAX_PAYLOAD = HEADER_SIZE + RECORD_SIZE * MAX_RECORDS

JSON_NAME = "cuesheet.json"      # 与 `cuesheet.JSON_NAME` 一致（这里不 import，保持零依赖）
FP_SONGS_KEY = "fp_songs"        # cfg["cue"]["fp_songs"]：fp_id → {song_id, title, …}
INSERT_CHUNK = 10000             # executemany 分块（控制内存峰值，也让长事务别太粗）
DB_FILE = "fingerprints.db"

# 「合成 song_id」的命名空间前缀。**必须与任何真实曲目 id 的算法无关**：
#   真实 id = int(md5(路径字符串)[:8], 16)（见 `ui_main` 建库处 / `engine._cue_song_id_of`）
#   合成 id = int(md5("fp:" + fp_id)[:8], 16)
# 前缀 `fp:` 保证同一串 fp_id 不可能与某个文件路径算出同一个 md5 ⇒ 两条命名空间不撞。
SONG_ID_SALT = "fp:"

__all__ = [
    "FP_DIR", "FP_EXT", "MAGIC", "VERSION", "HASH_HEX_LEN", "HASH_BYTES",
    "HEADER_SIZE", "RECORD_SIZE", "SONG_ID_SALT",
    "blob_name", "unpack_hashes", "read_from_package", "fp_song_id",
    "ingest_sheet", "drop_sheet", "reconcile_dupes", "default_db_path", "resolve_db",
    "start_ingest", "start_drop", "poll_jobs", "pending_jobs",
]


# ==========================================================================
# 包内成员名
# ==========================================================================
def blob_name(fp_id) -> str:
    """`fp_id` → 包内成员名 `fp/<fp_id>.bin`；**不安全/为空 ⇒ `""`**（调用方跳过）。

    只做**必要**清洗（拒绝路径分隔符 / 盘符冒号 / `..` 之类），其余原样保留 ——
    成员名必须能从 `cuesheet.json` 的 `identity.fp_id` **确定性还原**，
    两端就是靠这个约定互相找到的。

    ⚠ 分隔符硬编码 `/`（`os.sep` 在 Windows 上是 `\\`，写进 zip 演出端按 `/` 找不到）。
    """
    s = str(fp_id or "").strip()
    if not s or s in (".", ".."):
        return ""
    for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|", "\x00"):
        if bad in s:
            return ""
    return "%s/%s%s" % (FP_DIR, s, FP_EXT)


# ==========================================================================
# 解包（★ 绝不抛）
# ==========================================================================
def unpack_hashes(blob):
    """完整文件字节 → `[(hash_hex_str, offset_int), ...]`；**校验失败才 ⇒ `None`**。

    ★ **空字节流 ⇒ 空列表（不是 `None`）；校验失败才返回 `None`。**
      为什么要把"空"和"坏"分开（与编排端 `fp_pack.unpack_hashes` 同一语义）：
        · 空 = **没有指纹**（编排端 `pack_hashes([]) == b""`，与这里互逆，两端一致）；
        · `None` = **数据坏了**（magic/version/hash_hex_len/count/长度/zlib 任一不符）。
      将来真读到空成员时，前者是"这首没导指纹"，后者是"包坏了、该提示用户重导"，
      两者要能区分开 —— 混成一个 `None` 就永远查不出是哪一种。
      ⚠ **历史备注（别以为两边分叉了就是 bug）**：本实现一度对空字节流返回 `None`
        （更严），2026-10-18 按团队裁定与编排端 `fp_pack.unpack_hashes()` 统一成 `[]`。
        两份实现同名同格式，语义必须一致；而且两边**从不生产空成员**
        （编排端 `pack_hashes([]) == b""` 时直接不写这个成员），所以真实数据上
        这条差异永远不会出现 —— 统一它纯粹是为了读代码的人不被误导。

    校验链（任一不符 ⇒ `None`，**绝不抛**）：
      magic / version / hash_hex_len / count(≤上限) / `len(payload)-12 == count*14` /
      zlib 能解开且**压缩流完整**（`decompressobj().eof`）。
      非 bytes 的输入（除 `bytearray`/`memoryview` 外）也返回 `None`。

    :param blob: 文件字节（`bytes` / `bytearray` / `memoryview`）。
    :return: `[(20 字符小写 hex, offset 帧)]`；空字节流 `[]`；坏数据 `None`。
    """
    if blob is None:
        return None
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    elif isinstance(blob, bytearray):
        blob = bytes(blob)
    if not isinstance(blob, bytes):
        return None
    if len(blob) == 0:
        return []                            # ★ 空 ⇒ "没有指纹"（与 pack_hashes([]) 互逆）

    # ---- 解压（带上限，防压缩炸弹）----
    try:
        dec = zlib.decompressobj()
        payload = dec.decompress(blob, MAX_PAYLOAD + 1)
        if dec.unconsumed_tail:              # 还没读完就到了上限 ⇒ 太大，拒绝
            return None
        tail = dec.flush()
        # ★ `eof` 为 False = **压缩流被截断**（结尾的 adler32 都没读完）。
        #   光看解出来的长度是不够的：`.bin` 少一两个字节时载荷仍然完整，
        #   长度校验会放过去 ⇒ 我们会在"包已经损坏"的情况下悄悄灌进去半张指纹表。
        #   宁可当"没有指纹"（用户重导一次包即可），也不要灌坏数据。
        if not dec.eof:
            return None
        payload += tail
    except Exception:                        # noqa: BLE001  (zlib.error / 截断 / 内存)
        return None
    if not isinstance(payload, bytes) or len(payload) < HEADER_SIZE:
        return None
    if len(payload) > MAX_PAYLOAD:
        return None

    # ---- 头校验 ----
    if payload[0:4] != MAGIC:
        return None
    if payload[4] != VERSION:
        return None                          # 版本不符：宁可当"没有指纹"，也别读错位
    if payload[5] != HASH_HEX_LEN:
        return None                          # 每条记录的长度布局不符
    count = struct.unpack_from("<I", payload, 8)[0]
    if count > MAX_RECORDS:
        return None
    if len(payload) - HEADER_SIZE != count * RECORD_SIZE:
        return None                          # ★ 唯一的长度校验：截断/伪造都在这露出

    # ---- 逐条读出 ----
    out = []
    pos = HEADER_SIZE
    try:
        for _ in range(int(count)):
            raw = payload[pos:pos + HASH_BYTES]
            off = struct.unpack_from("<i", payload, pos + HASH_BYTES)[0]
            out.append((raw.hex(), int(off)))    # `bytes.hex()` = 20 字符小写，与 fp.py 同形
            pos += RECORD_SIZE
    except (struct.error, ValueError):        # 理论上不可达（长度已校验）
        return None
    return out


def _read_member(pkg_path, member):
    """从 zip 里读一个成员；成员不存在/包坏了 ⇒ `None`（绝不抛）。"""
    try:
        with zipfile.ZipFile(str(pkg_path)) as z:
            try:
                return z.read(member)
            except KeyError:
                return None
    except Exception:                        # noqa: BLE001  (BadZipFile / OSError / 权限)
        return None


def read_from_package(pkg_path, fp_id):
    """直接从包内读 `fp/<fp_id>.bin` 并解包。

    返回的三种情况要分清（调用方靠它们区分"老包 / 空指纹 / 坏包"）：
      · **成员不存在 ⇒ `None`**（老包的正常情况：这首没导指纹，不是错误）；
      · 成员存在但**是空字节流 ⇒ `[]`**（= 明确写了"没有指纹"）；
      · 成员存在但**数据坏了 ⇒ `None`**（`unpack_hashes` 的容错契约）。
    """
    name = blob_name(fp_id)
    if not name:
        return None
    blob = _read_member(pkg_path, name)
    if blob is None:
        return None
    return unpack_hashes(blob)


# ==========================================================================
# 合成 song_id
# ==========================================================================
def fp_song_id(fp_id) -> int:
    """`fp_id` → 演出端指纹库的**合成主键**（同一串 fp_id 永远同一个数）。

    `int(md5(("fp:" + fp_id).encode("utf-8")).hexdigest()[:8], 16)`

    ⚠ 与真实曲目 id（`int(md5(路径)[:8],16)`）**刻意分处两个命名空间**：
      加盐前缀 `fp:` 让两条算法不可能对同一串输入算出同一个值 ⇒ 灌库不会覆盖、
      也不会被当成"本机曲库里的某一首"。反过来，看 id 就知道这首歌"本机没有文件"。
    """
    return int(hashlib.md5((SONG_ID_SALT + str(fp_id or "")).encode("utf-8"))
               .hexdigest()[:8], 16)


# ==========================================================================
# cfg 访问（Config 是包装对象：**没有 .get()**，只能 `cfg[key]`）
# ==========================================================================
def _cue_cfg(cfg):
    """取（必要时创建）`cfg["cue"]`；失败 ⇒ 返回一个游离 dict（不抛）。"""
    try:
        cur = cfg["cue"]
    except Exception:                                            # noqa: BLE001
        cur = None
    if not isinstance(cur, dict):
        cur = {}
        try:
            cfg["cue"] = cur
        except Exception:                                        # noqa: BLE001
            pass
    return cur


def _fp_songs(cfg):
    """`cfg["cue"]["fp_songs"]`（缺失时建空 dict 并挂回去）。"""
    cur = _cue_cfg(cfg)
    m = cur.get(FP_SONGS_KEY)
    if not isinstance(m, dict):
        m = {}
        cur[FP_SONGS_KEY] = m
    return m


def _fp_songs_ro(cfg):
    """**只读**版 `_fp_songs`：不创建、不写 cfg（启动/对账路径要求零副作用）。

    区别很重要：`reconcile_dupes()` 在**启动**路径上会被调用，若它顺手把
    `cfg["cue"]["fp_songs"] = {}` 写进内存，就等于"每次启动都改一次配置"。
    """
    try:
        cur = cfg["cue"]
    except Exception:                                            # noqa: BLE001
        return {}
    if not isinstance(cur, dict):
        return {}
    m = cur.get(FP_SONGS_KEY)
    return m if isinstance(m, dict) else {}


def _local_fp_ids(cfg):
    """本机曲库已有的 `fp_id` 集合（`cfg["music_meta"]` 的每一项）。"""
    out = set()
    try:
        meta = cfg["music_meta"] or {}
    except Exception:                                            # noqa: BLE001
        meta = {}
    if not isinstance(meta, dict):
        return out
    for _p, rec in meta.items():
        if isinstance(rec, dict):
            fp = str(rec.get("fp_id") or "").strip()
            if fp:
                out.add(fp)
    return out


def _save_cfg(cfg):
    """落盘配置（Config.save() 自带进程内锁与原子替换 ⇒ 后台线程调也安全）。"""
    try:
        cfg.save()
    except Exception:                                            # noqa: BLE001
        pass


# ==========================================================================
# 「包里的曲目」—— 只读 cuesheet.json 的 tracks[].identity，不建模型
# ==========================================================================
def _read_tracks(pkg_path):
    """包里 `tracks[].identity` → `[{"fp_id","title","artist","duration"}]`。

    读不了 / 不是合法 JSON / 没有 tracks ⇒ `None`（调用方据此"这张包没东西可灌"）。
    ★ 刻意**不走 `cuesheet.load()`**：这里只要三个字段，一个 `json.loads` 就够，
      也免得本模块对 `cuesheet` 产生依赖（零依赖更好测）。
    """
    try:
        with zipfile.ZipFile(str(pkg_path)) as z:
            names = z.namelist()
            member = JSON_NAME if JSON_NAME in names else ""
            if not member:
                cands = [n for n in names if n.endswith(".json") and "/" not in n]
                if not cands:
                    return None
                member = cands[0]
            raw = z.read(member).decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return None
    try:
        d = json.loads(raw)
    except Exception:                                            # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    tracks = d.get("tracks")
    if not isinstance(tracks, list):
        return None
    out = []
    for t in tracks:
        if not isinstance(t, dict):
            continue
        idn = t.get("identity")
        if not isinstance(idn, dict):
            continue
        fp_id = str(idn.get("fp_id") or "").strip()
        if not fp_id:
            continue                             # 老包没有 fp_id ⇒ 认歌本来就没有锚点
        try:
            dur = float(idn.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        out.append({
            "fp_id": fp_id,
            "title": str(idn.get("title") or ""),
            "artist": str(idn.get("artist") or ""),
            "duration": dur,
        })
    return out


# ==========================================================================
# 数据库连接
# ==========================================================================
def default_db_path() -> str:
    """指纹库默认位置（与 `ui_main._init_recognizer` 同一处：`app_base_dir()/fingerprints.db`）。"""
    try:
        from config import app_base_dir
        return os.path.join(app_base_dir(), DB_FILE)
    except Exception:                                            # noqa: BLE001
        return ""


def _ensure_schema(conn):
    """建表/建索引（幂等，与 `fp.FingerprintDB.__init__` **同一份 schema**）。

    ⚠ 还要对齐 `PRAGMA user_version`：`fp.FingerprintDB` 在**版本不符时会清空整库**
      （"指纹算法换了 ⇒ 旧指纹不兼容"的保护）。我们自己新建的库 user_version=0，
      若不改成版本号，下次 `FingerprintDB()` 一打开就会把我们刚灌进去的指纹全删掉
      ⇒ 表现为"导入成功，但放歌永远认不出"。**只在库是空的时候改**：
      有数据却还是 0 号的库属于"老算法数据"，交给 `FingerprintDB` 按它自己的策略清。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fingerprints ("
        "hash TEXT NOT NULL, song_id INTEGER NOT NULL, offset INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON fingerprints(hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_song ON fingerprints(song_id)")
    try:
        ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except Exception:                                            # noqa: BLE001
        return
    if ver != 0:
        return
    try:
        n = int(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
    except Exception:                                            # noqa: BLE001
        return
    if n > 0:
        return                                  # 老库有数据：不冒充新版本（交给 fp 自己处理）
    try:
        from fp import FP_SCHEMA_VERSION
        want = int(FP_SCHEMA_VERSION)
    except Exception:                                            # noqa: BLE001
        want = 2                                # 与当前 fp.py 一致的兜底值
    try:
        conn.execute("PRAGMA user_version=%d" % want)
        conn.commit()
    except Exception:                                            # noqa: BLE001
        pass


def _conn_of(db):
    """`FingerprintDB` / `sqlite3.Connection` / `None` → `(conn, 需要我关吗)`。"""
    conn = getattr(db, "conn", None)
    if conn is not None:
        return conn, False
    if isinstance(db, sqlite3.Connection):
        return db, False
    path = default_db_path()
    if not path:
        return None, False
    try:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn)
        return conn, True
    except Exception:                                            # noqa: BLE001
        return None, False


def resolve_db(host, create=True):
    """尽力从演出端对象上取**指纹库**（`fp.FingerprintDB`）；取不到 ⇒ `None`。

    查找顺序（都是"有就用"）：`host._fp_db` → `host.audio._fp_db` →
    `host.engine._fp_db` → `host.engine.audio._fp_db`。

    `create=True` 且确实一个都没有时：在默认路径上开一个 `FingerprintDB`，
    挂到 `host._fp_db` 并**立刻**接给 `audio.set_recognizer()`。

    ★ **这条因果必须留住（否则就是"白灌"）**：
      1. `ui_main._init_recognizer()` 在 `fingerprints.db` 文件不存在时是**直接 return**
         （`if not os.path.exists(db_path): return`）⇒ 全新机器上根本没有识别器；
      2. `audio_engine._recognize_loop()` 在 `self._fp_db is None` 时每轮都 `continue`
         ⇒ 一条指纹都不会被查到；
      3. 于是"导入码表 → 指纹进了库"但识别器从未启用 ⇒ 放歌**永远认不出**，而且
         任何报错都没有（静默失效，最难查）。
      所以：**建库** 与 **set_recognizer(建出来的那个库)** 必须成对出现，
      只做前者等于把指纹灌进了一个没人读的文件。

    ⚠ 本函数会开 sqlite 连接（毫秒级），**请在 GUI 线程调用**；真正的写库交给后台作业。
    """
    objs = []

    def _add(o):
        if o is not None and not any(o is x for x in objs):
            objs.append(o)

    _add(host)
    _add(getattr(host, "audio", None))
    eng = getattr(host, "engine", None)
    _add(eng)
    _add(getattr(eng, "audio", None))
    for o in objs:
        db = getattr(o, "_fp_db", None)
        if db is not None:
            return db
    if not create:
        return None
    db = None
    try:
        from fp import FingerprintDB
        path = default_db_path()
        if not path:
            return None
        db = FingerprintDB(path)
    except Exception:                                            # noqa: BLE001
        return None
    try:
        host._fp_db = db                    # 让后续 `_init_recognizer` 能正常 close/重建
    except Exception:                                            # noqa: BLE001
        pass
    for o in objs:
        fn = getattr(o, "set_recognizer", None)
        if callable(fn):
            try:
                fn(db)                      # audio_engine：启用现场识别
            except Exception:                                    # noqa: BLE001
                pass
            break
    return db


# ==========================================================================
# 灌库 / 清库（同步实现；GUI 请用 start_ingest/start_drop）
# ==========================================================================
def ingest_sheet(cfg, db, pkg_path) -> dict:
    """把包内 `fp/<fp_id>.bin` 全部灌进指纹库，并更新 `cfg["cue"]["fp_songs"]`。

    流程（每张包的每条曲目）：
      1. 读 `cuesheet.json` 的 `tracks[].identity.fp_id`；
      2. **包里没有 `fp/<fp_id>.bin` ⇒ 跳过**（老包 / 该曲目没导指纹，正常情况）；
      3. **去重守卫**：`cfg["music_meta"]` 里已有同 `fp_id` 的曲目 ⇒ **跳过**
         （本机曲库优先：同一段音频在库里存两份指纹，投票会被拆成两半 ⇒ 反而认不出）；
      4. `song_id = fp_song_id(fp_id)`；先 `DELETE FROM fingerprints WHERE song_id=?`
         再批量 `INSERT`（**显式列名** —— 本项目铁律：隐式列顺序改一次就静默写错列）；
      5. 记 `cfg["cue"]["fp_songs"][fp_id] = {"song_id","title","artist","duration","sheet"}`。

    ⚠ **会写库**：5 万条 ≈ 0.3~0.6s ⇒ 必须放后台线程（`start_ingest()`）；
      本函数本身不碰 Qt，可以安全地在工作线程里跑。

    ★ 开头会先调一次 `reconcile_dupes()`：把"本机曲库后来也有了的"合成指纹清掉，
      免得同一首歌在库里留下两份指纹（票数被拆一半 ⇒ 时认时不认）。

    :param cfg:      `Config`（包装对象，没有 `.get()`）。
    :param db:       `fp.FingerprintDB` 或 `sqlite3.Connection`；`None` ⇒ 自己按默认路径开。
    :param pkg_path: `.ercue` 路径。
    :return: `{"sheets": 处理到的包数(0/1), "tracks": 真正写入的曲目数,
               "hashes": 写入的指纹行数, "ms": 耗时毫秒}`
    """
    t0 = time.perf_counter()
    res = {"sheets": 0, "tracks": 0, "hashes": 0, "ms": 0.0}
    pkg = os.path.abspath(str(pkg_path or ""))
    if not pkg or not os.path.isfile(pkg):
        res["ms"] = (time.perf_counter() - t0) * 1000.0
        return res
    tracks = _read_tracks(pkg)
    if tracks is None:
        res["ms"] = (time.perf_counter() - t0) * 1000.0
        return res
    res["sheets"] = 1
    if not tracks:
        res["ms"] = (time.perf_counter() - t0) * 1000.0
        return res

    local_fp = _local_fp_ids(cfg)
    # ★ 先对账一次（撞车的合成指纹清掉）：用户可能"先导过这张包、后来才把歌加进曲库"，
    #   那种残留只靠下面的逐首判据是清不掉的（它只看本包，不看历史）。
    #   幂等且零成本：没有撞车时它不碰数据库、也不写配置。
    try:
        reconcile_dupes(cfg, db)
    except Exception:                                            # noqa: BLE001
        pass
    fp_songs = _fp_songs(cfg)
    conn, own = _conn_of(db)
    if conn is None:
        res["ms"] = (time.perf_counter() - t0) * 1000.0
        return res
    try:
        pending = 0
        for tr in tracks:
            fp_id = tr["fp_id"]
            if fp_id in local_fp:
                continue                     # ★ 去重守卫：本机曲库优先
            hashes = read_from_package(pkg, fp_id)
            if not hashes:
                continue                     # 老包 / 坏包 / 空指纹 ⇒ 跳过
            song_id = fp_song_id(fp_id)
            conn.execute("DELETE FROM fingerprints WHERE song_id=?", (int(song_id),))
            rows = [(h, int(song_id), int(off)) for h, off in hashes]
            for i in range(0, len(rows), INSERT_CHUNK):
                conn.executemany(
                    "INSERT INTO fingerprints (hash, song_id, offset) VALUES (?,?,?)",
                    rows[i:i + INSERT_CHUNK])
            pending += 1
            fp_songs[fp_id] = {
                "song_id": int(song_id),
                "title": tr["title"],
                "artist": tr["artist"],
                "duration": float(tr["duration"]),
                "sheet": pkg,
            }
            res["tracks"] += 1
            res["hashes"] += len(rows)
        if pending:
            conn.commit()                    # 一次提交（别每首歌 fsync 一次）
    except Exception:                        # noqa: BLE001
        try:
            conn.rollback()
        except Exception:                                        # noqa: BLE001
            pass
    finally:
        if own:
            try:
                conn.close()
            except Exception:                                    # noqa: BLE001
                pass
    if res["tracks"]:
        _save_cfg(cfg)
    res["ms"] = (time.perf_counter() - t0) * 1000.0
    return res


def drop_sheet(cfg, db, pkg_path) -> int:
    """把**这张包**灌进去的指纹与映射全部撤掉；返回删掉的指纹行数。

    只按 `cfg["cue"]["fp_songs"]` 里 `sheet == 该包` 的条目动手 ——
    **绝不按"本机曲库"或"目录"去猜**：删错就是删了用户真歌的指纹。

    ★ 为什么必须撤：码表被移除后若指纹还留在库里，放这首"本机没有文件"的歌时
      识别器会一直返回那个合成 id ⇒ 一个不存在的歌占着 `recognized_song_id`。
      映射清掉后引擎反查不到 ⇒ 退回"无歌"，语义正确。
    """
    pkg = os.path.normcase(os.path.abspath(str(pkg_path or "")))
    fp_songs = _fp_songs(cfg)
    if not pkg or not isinstance(fp_songs, dict) or not fp_songs:
        return 0
    victims = []
    for fp_id, rec in list(fp_songs.items()):
        if not isinstance(rec, dict):
            continue
        sheet = os.path.normcase(os.path.abspath(str(rec.get("sheet") or "")))
        if sheet == pkg:
            victims.append((fp_id, rec))
    if not victims:
        return 0
    conn, own = _conn_of(db)
    removed = 0
    try:
        if conn is not None:
            for _fp_id, rec in victims:
                try:
                    sid = int(rec.get("song_id", -1))
                except (TypeError, ValueError):
                    continue
                cur = conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
                try:
                    removed += max(0, int(cur.rowcount or 0))
                except Exception:                                # noqa: BLE001
                    pass
            conn.commit()
    except Exception:                        # noqa: BLE001
        try:
            conn.rollback()
        except Exception:                                        # noqa: BLE001
            pass
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:                                    # noqa: BLE001
                pass
    for fp_id, _rec in victims:
        fp_songs.pop(fp_id, None)
    _save_cfg(cfg)
    return removed


def reconcile_dupes(cfg, db) -> dict:
    """把「本机曲库后来也有了」的码表指纹清掉 → `{"dropped": [fp_id...], "rows": n}`。

    **为什么必须有它（`drop_sheet` 覆盖不到的那一半）**
      `drop_sheet()` 按**包路径**删（用户移除码表时）；本函数按 **fp_id 撞车**删
      （用户先导码表、后把同一首歌加进本机曲库时）。两者互补，都要留着。
      不清的话同一段音频在指纹库里有两份指纹（合成 id + 本机路径 id）⇒ 投票被拆成
      两半 ⇒ 低于 `charge_engine.CONFIRM_VOTES` 就表现为**"有时候认得出、有时候认不出"**，
      这是现场最难查、最浪费时间的一类故障。

    **判据**：`cfg["cue"]["fp_songs"]` 的每个 `fp_id`，若 `cfg["music_meta"]` 里
      某首歌的 `fp_id` 与它相同 ⇒ 该合成 id 的指纹行必须删掉（本机曲库优先）。

    **幂等 + 零副作用**（所以能挂在启动路径上）：
      · 没有 `fp_songs` / 没有本机 fp_id / 没有撞车 ⇒ **0 删除、不碰数据库、不写配置**；
      · 只有真删到东西才 `cfg.save()` 一次（绝不会"每次启动都写一遍配置"）。

    **零重活**：只扫两个 dict（`fp_songs` 的键、`music_meta` 的 fp_id 集合），
      **不解包、不解码音频**。

    **绝不抛**：任何异常都只是"这次没对账成"，返回已完成的部分。

    :param cfg: `Config`（包装对象，**没有 `.get()`**）。
    :param db:  `fp.FingerprintDB` / `sqlite3.Connection` / `None`（⇒ 按默认路径开）。
    :return: `{"dropped": [被清掉的 fp_id...], "rows": 删掉的指纹行数}`
    """
    out = {"dropped": [], "rows": 0}
    try:
        fp_songs = _fp_songs_ro(cfg)
        if not fp_songs:
            return out                          # 没导过带指纹的码表 ⇒ 什么都不用做
        local = _local_fp_ids(cfg)
        if not local:
            return out                          # 本机曲库一首带 fp_id 的都没有 ⇒ 不可能撞车
        victims = [(fid, rec) for fid, rec in list(fp_songs.items())
                   if fid in local and isinstance(rec, dict)]
        if not victims:
            return out                          # ★ 没有撞车 ⇒ 0 删除、**不写盘**
        conn, own = _conn_of(db)
        if conn is None:
            return out                          # 打不开库：不删也不清映射（清了就再也清不掉行）
        try:
            for _fid, rec in victims:
                try:
                    sid = int(rec.get("song_id", -1))
                except (TypeError, ValueError):
                    sid = -1
                if sid < 0:
                    continue                    # 映射坏了：只清映射（下面），没有行可删
                try:
                    cur = conn.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
                    out["rows"] += max(0, int(cur.rowcount or 0))
                except Exception:                                    # noqa: BLE001
                    continue
            try:
                conn.commit()
            except Exception:                                        # noqa: BLE001
                pass
        finally:
            if own:
                try:
                    conn.close()
                except Exception:                                    # noqa: BLE001
                    pass
        # 清映射：即使 song_id 坏了也要清（留着引擎会去找一个用不了的 id）
        for fid, _rec in victims:
            try:
                fp_songs.pop(fid, None)
            except Exception:                                        # noqa: BLE001
                pass
            out["dropped"].append(fid)
        _save_cfg(cfg)
    except Exception:                                            # noqa: BLE001
        pass
    return out


# ==========================================================================
# 后台作业（GUI 只轮询，不阻塞）
# ==========================================================================
class _Job:
    """一次后台作业：`ingest`（灌库）或 `drop`（清库）。

    线程只写 `done/result/error`（属性赋值在 GIL 下原子），GUI 侧用
    `poll_jobs()` 轮询取结果 —— **不用 Qt 信号**，本模块才能保持零 Qt。
    """

    __slots__ = ("kind", "pkg", "cfg", "db", "done", "result", "error",
                 "t_start", "t_end", "_thread")

    def __init__(self, kind, cfg, db, pkg):
        self.kind = str(kind)
        self.cfg = cfg
        self.db = db
        self.pkg = os.path.abspath(str(pkg or ""))
        self.done = False
        self.result = None
        self.error = ""
        self.t_start = time.time()
        self.t_end = 0.0
        self._thread = None

    # ---------------- 执行 ----------------
    def _run(self):
        try:
            if self.kind == "drop":
                self.result = drop_sheet(self.cfg, self.db, self.pkg)
            else:
                self.result = ingest_sheet(self.cfg, self.db, self.pkg)
        except Exception as e:                                   # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.t_end = time.time()
            self.done = True

    def start(self):
        """起一个 **daemon** 线程（退出程序时不会被它拖住）。"""
        th = threading.Thread(target=self._run, name="cue-fp-%s" % self.kind, daemon=True)
        self._thread = th
        th.start()
        return self


#: 作业登记表（`poll_jobs()` 取走已完成项；最多保留 `_KEEP` 条，防长跑堆积）
_JOBS = []
_JOB_LOCK = threading.Lock()
_KEEP = 8


def _spawn(kind, cfg, db, pkg):
    job = _Job(kind, cfg, db, pkg)
    with _JOB_LOCK:
        _JOBS.append(job)
        # 只裁"已完成"的旧项：还在跑的绝不能丢（否则结果没人收）
        done = [j for j in _JOBS if j.done]
        while len(done) > _KEEP:
            try:
                _JOBS.remove(done.pop(0))
            except ValueError:
                break
    return job.start()


def start_ingest(cfg, host, pkg_path):
    """**后台**把一张包的指纹灌进指纹库。返回 `_Job`（失败前置条件时返回 `None`）。

    `host` 只用来找指纹库（主窗 / engine 都行，见 `resolve_db`）。
    """
    pkg = os.path.abspath(str(pkg_path or ""))
    if not pkg or not os.path.isfile(pkg):
        return None
    db = resolve_db(host)
    return _spawn("ingest", cfg, db, pkg)


def start_drop(cfg, host, pkg_path):
    """**后台**撤掉一张包灌进去的指纹。返回 `_Job`（没有可撤的 ⇒ `None`）。"""
    pkg = os.path.abspath(str(pkg_path or ""))
    if not pkg:
        return None
    db = resolve_db(host, create=False)
    return _spawn("drop", cfg, db, pkg)


def poll_jobs():
    """取走**已完成**的作业（列表）。未完成的留在登记表里，下次再问。"""
    out = []
    with _JOB_LOCK:
        for j in list(_JOBS):
            if j.done:
                out.append(j)
                try:
                    _JOBS.remove(j)
                except ValueError:
                    pass
    return out


def pending_jobs() -> int:
    """还在跑的作业数（状态行显示"正在导入指纹…"用）。"""
    with _JOB_LOCK:
        return sum(1 for j in _JOBS if not j.done)
