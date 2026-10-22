# -*- coding: utf-8 -*-
"""★ 指纹哈希序列的**二进制打包格式**（`.ercue` 包内 `fp/<fp_id>.bin`）。

**为什么要它（用户原话）**
    「这首歌在 erc 里做的码表包啊，理论来讲 era 导入就应该要识别到」

  现状：`.ercue` 里只存了 `identity.fp_id`（= 整首歌**哈希序列**的 sha1 前 16 hex，
  **16 个字符的摘要**）。演出端只有这个摘要 ⇒ **认不出音频**：它必须手里有那首音频、
  自己重算一遍同口径的哈希序列才能比对。歌不在演出端的曲库里 ⇒ 永远认不出。

  解法：导出时把**完整指纹哈希序列**写进包里；演出端导入时把它灌进自己的指纹库
  ⇒ 哪怕音频不在那台机器上，放歌时照样能认（音频一进来就能算出哈希并对上）。

**格式（★ 两端契约，逐字节实现，不许自创）**
  包内成员名：`fp/<fp_id>.bin`（`fp_id` 形如 `fp2-617615efb8f43911`）
  整个文件 = `zlib.compress(payload, 6)`，`payload` 为：

      偏移 0..3   : magic  b"ERFP"
      偏移 4      : version = 1        (uint8)
      偏移 5      : hash_hex_len       (uint8，= `fp_core.HASH_LEN`，当前 20)
      偏移 6..7   : reserved = 0       (uint16 LE)
      偏移 8..11  : count              (uint32 LE)
      偏移 12..   : count 条记录，每条 = 哈希二进制(HASH_LEN//2 字节) + offset(int32 LE)
                    当前 HASH_LEN=20 ⇒ 每条 14 字节
      校验        ：`len(payload) - 12 == count * 14`

  · 哈希用 **`hashlib` 前 20 hex 的前 10 字节二进制**（80 bit 全保留，**不截断**）；
  · `offset` 就是 `fp_core.fingerprint_audio()` 返回的那个 offset —— **单位 = hop 帧**
    （44.1k / hop=2048 坐标系），不是秒、不是毫秒；
  · 实测体积（真实歌）：49462 条 → 676.2 KB 原始 / **501.6 KB** 压缩；19607 条 → 202.7 KB。

**★ 两份实现，必须同步**
  本文件与**演出端 `AutoVJ/src/cue_fp_pack.py`** 是**同一格式的两份实现**
  （两个仓库各自独立，谁也不能 import 谁）。**改一端必须改另一端**，且两侧的
  `selftest()` 都要跑过；跨端一致性由 `tools/_fp_pack_test.py` 的交叉验证兜底。

**零 Qt 纪律**：本模块只用 stdlib（`zlib`/`struct`），**不得** import PySide6 / numpy。
`tools/selftest.py` 会 AST 扫描 `model/` 下的 Qt 绑定，命中即失败。
"""

from __future__ import annotations

import struct
import zlib

from .fp_core import HASH_LEN

# ==========================================================================
# 常量（★ 两端契约）
# ==========================================================================
MAGIC = b"ERFP"          # 4 字节魔数：EasyReality FingerPrint
VERSION = 1              # 格式版本；**不兼容的布局变更必须 +1**（老端读到新版 ⇒ 返回 None 跳过）
HEADER_SIZE = 12         # 4(magic) + 1(version) + 1(hash_hex_len) + 2(reserved) + 4(count)
RESERVED = 0             # 恒 0（留给将来的标志位；读到非 0 也**不报错**，向前兼容）
HASH_BYTES = HASH_LEN // 2          # 20 hex → 10 字节（**从 fp_core 取，不硬编码**）
RECORD_SIZE = HASH_BYTES + 4        # 哈希 + int32 offset ⇒ 14 字节
COMPRESS_LEVEL = 6       # 与 zip 内其他成员一致的压缩级别

# offset 是 int32（hop 帧）。44.1k / hop=2048 ⇒ 2147483647 帧 ≈ 27.7 小时，
# 远超任何歌曲；越界的记录在打包时**丢弃**（不写坏数据）。
_OFF_MIN = -0x80000000
_OFF_MAX = 0x7FFFFFFF

# 解包时的载荷上限：保护自己不被"压缩炸弹"打爆内存。
# 4,000,000 条 ≈ 56 MB 原始载荷 ≈ 连续 27 小时的音频 —— 正常码表不可能超。
MAX_RECORDS = 4_000_000
MAX_PAYLOAD = HEADER_SIZE + RECORD_SIZE * MAX_RECORDS

# 包内目录（成员名用 `/` 拼，**绝不**用 os.sep；Windows 会写成 `\`，演出端解不出来）
FP_DIR = "fp"
FP_EXT = ".bin"

__all__ = [
    "MAGIC", "VERSION", "HEADER_SIZE", "RECORD_SIZE", "HASH_BYTES", "FP_DIR", "FP_EXT",
    "pack_hashes", "unpack_hashes", "member_name", "is_member", "fit_id_from_member",
    "selftest",
]


# ==========================================================================
# 哈希串 ↔ 10 字节二进制
# ==========================================================================
def _hash_bytes(hex_str):
    """`"0123456789abcdef0123"` → 10 字节；**不合法返回 None**（绝不抛）。

    严格按 `HASH_LEN` 校验长度：`fp_core._hashes()` 产出的 hex 串**恒为 20 字符**，
    长度不对说明数据源有问题（比如别处截断了），宁可丢掉这条也不要写出对不上的指纹。
    """
    if not isinstance(hex_str, str):
        return None
    s = hex_str.strip()
    if len(s) != HASH_LEN:
        return None
    try:
        raw = bytes.fromhex(s)
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == HASH_BYTES else None


def _hash_hex(raw):
    """10 字节 → 20 hex 小写字符串（`fp_core` 侧本来就是小写，比对时逐字一致）。"""
    return raw.hex()


# ==========================================================================
# 打包 / 解包
# ==========================================================================
def pack_hashes(hashes) -> bytes:
    """`[(hash_hex, offset_int), ...]` → **zlib 压缩后的**完整文件字节。

    · **空列表 ⇒ `b""`**（调用方据此跳过写文件 —— 包里就不出现这个成员）；
    · 单条格式不对（hex 长度错 / offset 非整数 / offset 越 int32）⇒ **丢掉那一条**，
      `count` 记实际写入条数（绝不写出自相矛盾的文件）；
    · 全部条目都被丢掉 ⇒ 也返回 `b""`（等价于"没有指纹"）。

    实测：49462 条 → 501.6 KB；耗时约 50 ms（用 `zlib.compress` 一次压完）。

    Args:
        hashes: `fp_core.fingerprint_audio()` 的返回值（`[(hex, offset_frame)]`）。

    Returns:
        完整文件字节（已压缩）；空/无有效条目时 `b""`。
    """
    body = bytearray()
    count = 0
    for item in (hashes or ()):
        try:
            hex_str, off = item[0], item[1]
        except (TypeError, IndexError, KeyError):
            continue                              # 结构不对 ⇒ 跳过（绝不抛）
        raw = _hash_bytes(hex_str)
        if raw is None:
            continue
        try:
            off_i = int(off)
        except (TypeError, ValueError):
            continue
        if off_i < _OFF_MIN or off_i > _OFF_MAX:
            continue
        try:
            body += raw + struct.pack("<i", off_i)
        except struct.error:                      # 理论上不可达（已做范围检查）
            continue
        count += 1
    if count <= 0:
        return b""

    head = bytearray(HEADER_SIZE)
    head[0:4] = MAGIC
    head[4] = int(VERSION) & 0xFF
    head[5] = int(HASH_LEN) & 0xFF
    struct.pack_into("<H", head, 6, RESERVED)
    struct.pack_into("<I", head, 8, count)
    payload = bytes(head) + bytes(body)
    # 自检式断言：布局与 count 必须自洽（写坏文件的代价太高，宁可在这里炸）
    if len(payload) - HEADER_SIZE != count * RECORD_SIZE:
        raise AssertionError("指纹包载荷长度与 count 不自洽（打包逻辑有 bug）")
    return zlib.compress(payload, COMPRESS_LEVEL)


def unpack_hashes(blob):
    """完整文件字节 → `[(hash_hex, offset_int), ...]`。

    **容错契约（绝不抛）**：
      · `b""`（或 `None`）⇒ 返回 **`[]`** —— 空字节流是我们自己"没有指纹"的表示
        （与 `pack_hashes([]) == b""` 互逆）；**注意不是 None**；
      · magic 不对 / 版本不支持 / `hash_hex_len` 不是 `fp_core.HASH_LEN` /
        载荷长度与 count 不自洽 / zlib 解不开 / 超上限 ⇒ 一律返回 **None**；
      · 单条记录内容无所谓"对不对"（哈希就是 80 bit 随机串），不做额外校验。

    Args:
        blob: 文件字节（`bytes` / `bytearray` / `memoryview`）。

    Returns:
        `[(hex, offset), ...]`；坏数据 `None`。
    """
    if blob is None:
        return []
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    elif isinstance(blob, bytearray):
        blob = bytes(blob)
    if not isinstance(blob, bytes):
        return None
    if len(blob) == 0:
        return []                                 # 空 ⇒ 没有指纹（与 pack_hashes([]) 互逆）

    # ---- 解压（带上限，防压缩炸弹）----
    try:
        d = zlib.decompressobj()
        payload = d.decompress(blob, MAX_PAYLOAD + 1)
        if d.unconsumed_tail:                     # 还没读完就到了上限 ⇒ 太大，拒绝
            return None
        payload += d.flush()
        # ★ 必须确认"压缩流确实读到了结尾"。截断 1 字节时 `decompress()` 往往**不抛异常**，
        #   只是 `eof` 为 False（把 Adler-32 校验尾巴切掉了）—— 只靠异常会把截断放过去，
        #   然后在后面的长度校验里靠运气。这里显式拒收。
        if not d.eof:
            return None
    except Exception:                             # noqa: BLE001  (zlib.error / 内存 / 截断)
        return None
    if not isinstance(payload, bytes) or len(payload) < HEADER_SIZE:
        return None
    if len(payload) > MAX_PAYLOAD:
        return None

    # ---- 头校验 ----
    if payload[0:4] != MAGIC:
        return None
    if payload[4] != VERSION:
        return None                               # 版本不符：宁可当"没有指纹"，也别读错位
    if payload[5] != HASH_LEN:
        return None                               # 布局（每条记录长度）不符
    count = struct.unpack_from("<I", payload, 8)[0]
    if count > MAX_RECORDS:
        return None
    if len(payload) - HEADER_SIZE != count * RECORD_SIZE:
        return None                               # ★ 唯一的长度校验：截断/伪造都在这里露出

    # ---- 逐条读出 ----
    out = []
    ap = out.append
    pos = HEADER_SIZE
    try:
        for _ in range(count):
            raw = payload[pos:pos + HASH_BYTES]
            off = struct.unpack_from("<i", payload, pos + HASH_BYTES)[0]
            ap((_hash_hex(raw), int(off)))
            pos += RECORD_SIZE
    except struct.error:                          # 理论上不可达（长度已校验）
        return None
    return out


# ==========================================================================
# 包内成员名（★ 与演出端同一套命名规则）
# ==========================================================================
def member_name(fp_id) -> str:
    """`fp_id` → 包内成员名 `fp/<fp_id>.bin`；**不安全/为空时返回 ""**（调用方跳过）。

    只做**必要的**清洗（拒绝路径分隔符、盘符冒号、`..` 等），其余原样保留 ——
    因为成员名必须能从 `cuesheet.json` 的 `identity.fp_id` **确定性还原**，
    演出端就是按它去找包的。

    ⚠ 分隔符**硬编码 `/`**（绝不用 `os.sep`：Windows 下会写成 `\\`，演出端按 `/` 解不出来）。
    """
    s = str(fp_id or "").strip()
    if not s or s in (".", ".."):
        return ""
    bad = ("/", "\\", ":", "*", "?", '"', "<", ">", "|", "\x00")
    if any(b in s for b in bad):
        return ""
    return "%s/%s%s" % (FP_DIR, s, FP_EXT)


def is_member(name) -> bool:
    """成员名是不是指纹包（`fp/xxx.bin`）。演出端扫包时用它过滤。"""
    s = str(name or "")
    return (s.startswith(FP_DIR + "/") and s.endswith(FP_EXT) and not s.endswith("/"))


def fit_id_from_member(name) -> str:
    """成员名 → `fp_id`（`fp/xxx.bin` → `xxx`）；不是指纹包返回 ""。

    ⚠ 名字里的非法字符原样返回（调用方自己决定要不要用）—— 本函数只做**结构**剥离。
    """
    s = str(name or "")
    if not is_member(s):
        return ""
    stem = s[len(FP_DIR) + 1:]
    if stem.endswith(FP_EXT):
        stem = stem[:-len(FP_EXT)]
    return stem


# ==========================================================================
# 自检（零 Qt、零 numpy、无外部素材）
# ==========================================================================
def selftest() -> bool:
    """往返 / 空列表 / 坏数据反例 —— 全部用**合成哈希**，不碰磁盘。"""
    import random

    rnd = random.Random(20261018)
    hexes = ["%020x" % rnd.getrandbits(80) for _ in range(500)]
    hashes = [(h, i * 7) for i, h in enumerate(hexes)]

    # 1) 往返逐条相等（顺序也必须一致）
    blob = pack_hashes(hashes)
    assert isinstance(blob, bytes) and blob, "非空输入不该产出空字节"
    back = unpack_hashes(blob)
    assert back == hashes, "往返不一致（前 3 条：%r vs %r）" % (back[:3], hashes[:3])

    # 2) 空列表 ⇒ b""；b"" ⇒ []（**钉死是 []，不是 None**）
    assert pack_hashes([]) == b"", "空列表必须返回 b''"
    assert pack_hashes(None) == b"", "None 也必须返回 b''"
    assert unpack_hashes(b"") == [], "空字节流必须返回 []（不是 None）"
    assert unpack_hashes(None) == [], "None 必须返回 []（不是 None）"

    # 3) 头字段逐字节（两端契约的锚）
    raw_payload = zlib.decompress(blob)
    assert raw_payload[0:4] == b"ERFP", "magic 不对"
    assert raw_payload[4] == 1, "version 不对"
    assert raw_payload[5] == HASH_LEN == 20, "hash_hex_len 必须等于 fp_core.HASH_LEN"
    assert struct.unpack_from("<H", raw_payload, 6)[0] == 0, "reserved 必须为 0"
    assert struct.unpack_from("<I", raw_payload, 8)[0] == len(hashes), "count 不对"
    assert len(raw_payload) - 12 == len(hashes) * 14, "载荷长度校验式不成立"
    # 第一条记录：哈希 10 字节 + offset int32 LE
    assert raw_payload[12:22] == bytes.fromhex(hashes[0][0]), "第一条哈希不是裸 10 字节"
    assert struct.unpack_from("<i", raw_payload, 22)[0] == hashes[0][1], "第一条 offset 不对"

    # 4) 坏数据反例：**全部返回 None，绝不抛**
    first_hash_bytes = bytes.fromhex(hashes[0][0])
    bad_magic = bytearray(raw_payload)
    bad_magic[0:4] = b"XRPF"
    bad_ver = bytearray(raw_payload)
    bad_ver[4] = 2
    bad_len = bytearray(raw_payload)
    bad_len[5] = 40
    bad_count = bytearray(raw_payload)
    struct.pack_into("<I", bad_count, 8, len(hashes) + 1)
    bad_cases = {
        "截断 1 字节": blob[:-1],
        "magic 改错": zlib.compress(bytes(bad_magic), 6),
        "version 改成 2": zlib.compress(bytes(bad_ver), 6),
        "hash_hex_len 改成 40": zlib.compress(bytes(bad_len), 6),
        "count 改大": zlib.compress(bytes(bad_count), 6),
        "非 zlib 垃圾": b"\x00\x01\x02not-a-zlib-stream",
        "纯随机": bytes(range(64)),
    }
    for label, bad in bad_cases.items():
        got = unpack_hashes(bad)
        assert got is None, "%s 应返回 None，实际 %r" % (label, (got if got is None else len(got)))

    # 5) 单条脏数据：坏 hex / 越界 offset 被丢掉，count 记实际条数
    mixed = [("zz", 1), (hexes[0], 5), (hexes[1], "x"), (hexes[2], 2 ** 40)]
    got = unpack_hashes(pack_hashes(mixed))
    assert got == [(hexes[0], 5)], "脏条目应被丢弃，只留合法的一条，实际 %r" % (got,)
    assert pack_hashes([("zz", 1)]) == b"", "全部不合法 ⇒ b''"

    # 6) 成员名规则（分隔符必须是 `/`）
    assert member_name("fp2-617615efb8f43911") == "fp/fp2-617615efb8f43911.bin"
    assert member_name("") == "" and member_name("a/b") == "" and member_name("..") == ""
    assert is_member("fp/fp2-1.bin") and not is_member("thumbs/x.png")
    assert fit_id_from_member("fp/fp2-1.bin") == "fp2-1"
    assert fit_id_from_member("thumbs/x.png") == ""
    print("[fp_pack] 自检通过 ✓  往返 %d 条逐条相等；空列表=b''；%d 个坏数据反例全部 None（不抛）"
          % (len(hashes), len(bad_cases)))
    return True


if __name__ == "__main__":
    selftest()
