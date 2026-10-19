# -*- coding: utf-8 -*-
"""★ C5 验收：曲目指纹包（`.ercue` 内 `fp/<fp_id>.bin`）—— 格式 / 跨端一致性 / 导出端到端。

**这个功能要解决什么（用户原话）**
    「这首歌在 erc 里做的码表包啊，理论来讲 era 导入就应该要识别到」

  `.ercue` 里原来只有 `identity.fp_id`（整首哈希序列的 sha1 前 16 hex = **16 个字符的摘要**），
  演出端拿它**认不出音频**：必须手里有那首歌、自己重算一遍哈希序列才能比对。歌不在演出端
  的曲库里 ⇒ 永远认不出。现在导出时把**完整哈希序列**写进包（`fp/<fp_id>.bin`），演出端
  导入时灌进自己的指纹库 ⇒ 音频不在那台机器上，放歌也能认出。

**本脚本验什么（6 项，全部必过）**
  1. `pack_hashes` → `unpack_hashes` **逐条相等**（用真实文件的哈希，抽前 2000 条也验全量）
  2. 空列表 ⇒ `b""`；`unpack_hashes(b"")` ⇒ **`[]`**（钉死：是 `[]`，不是 `None`）
  3. 坏数据反例：截断 1 字节 / magic 改错 / version 改成 2 / count 改大 ⇒ 全部 **None，不抛**
  4. **跨端交叉验证**：用演出端解释器（`P:\\ERAVJ\\AutoVJ\\venv\\Scripts\\python.exe`）的
     `fp.fingerprint_audio` 对**同一个文件**再算一遍，断言**前 1000 条 `(hash, offset)` 全等**，
     且**总条数相同**。哈希算法一旦分叉，写进包里的指纹在演出端一条都匹配不上 —— 这是地基。
  5. **导出端到端**：造一个只含 1 首真歌的小包 → `ercue_io.save(..., fp_packs=...)` →
     解开 zip 断言 `fp/<fp_id>.bin` 存在、`unpack_hashes` 条数 > 1000、大小 < 1.5 MB；
     并断言 **`format_version` 没变**、没有 `fp_packs` 时**不写**任何 `fp/` 成员（向后兼容）。
  6. **反向复现**：把 `pack_hashes` 里的 offset 从 `int32` 改成 `int16`（在**临时副本**里改，
     仓库源码一字不动）⇒ 第 3 条那套"长度校验"必须**挂**（真实 `unpack_hashes` 返回 None）。
     证明这条校验真的在起作用，不是摆设。
  7. **格式跨端**（演出端 `AutoVJ/src/cue_fp_pack.py` 就绪后自动启用）：**我们导出的包**
     用演出端的 `unpack_hashes()` / `read_from_package()` 读回来，必须**逐条相等**、
     成员名规则一致。④ 只证明"哈希算法同口径"，这一项才证明"**格式**没分叉"——
     格式写错位，哈希再一致也灌不进演出端的库。
     文件还没落地 ⇒ 打印"跳过"（**绝不假装通过**）。

**零 Qt**：只用 `numpy` / `ercue.model.*` / `ercue.audio.player.AudioTrack`（纯 PyAV 解码，
不 import PySide6）⇒ 可以在无显示环境下跑。

用法：
    venv\\Scripts\\python.exe tools\\_fp_pack_test.py            # 全跑（约 15~25 s）
    venv\\Scripts\\python.exe tools\\_fp_pack_test.py --keep     # 保留临时目录便于复查
退出码：0 = 全过（打印 `FP_PACK_OK`）；1 = 有失败（打印 `FP_PACK_FAIL`）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                # noqa: E402

from ercue.model import ercue_io, fp_core, fp_pack                # noqa: E402
from ercue.model.cuesheet import CompLayer, CueItem, MediaRef     # noqa: E402

# 演出端解释器（只用来跑 `fp.fingerprint_audio`，**绝不改 AutoVJ 仓库里的任何文件**）
AUTOVJ_ROOT = "P:/ERAVJ/AutoVJ"
AUTOVJ_PYTHON = os.path.join(AUTOVJ_ROOT, "venv", "Scripts", "python.exe")
AUTOVJ_SRC = os.path.join(AUTOVJ_ROOT, "src")

# 真实素材（按顺序找；都没有 ⇒ 生成 wav，并在报告里**明说**是生成的）
CANDIDATES = [
    "W:/临时结界相关/2of/tofu/Rolling Contact - Cosmic String.flac",     # 44.1k / 281.6 s
    "W:/CloudMusic/Amateras Records/東方太嗨 -Touhou Toohigh Trance-/"
    "Amateras Records - Pretty World.mp3",                                # 48k
]

CROSS_N = 1000            # 跨端比对前 N 条
SAMPLE_N = 2000           # 第 1 项"抽 2000 条"的抽样量
MAX_PACK_BYTES = int(1.5 * 1024 * 1024)


# ==========================================================================
# 素材
# ==========================================================================
def _make_wav(path, sr=44100, seconds=20.0, seed=20261018):
    """造一段"有明确节拍峰"的 wav（stdlib `wave`，16 位 PCM，**两端都能一致解码**）。"""
    import wave
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    t = np.arange(n, dtype=np.float64) / float(sr)
    left = 0.30 * np.sin(2 * np.pi * 220.0 * t) + 0.20 * np.sin(2 * np.pi * 1320.0 * t)
    right = 0.30 * np.sin(2 * np.pi * 330.0 * t) + 0.20 * np.sin(2 * np.pi * 1760.0 * t)
    click = np.zeros(n, dtype=np.float64)
    for k in range(0, n, max(1, int(sr * 0.5))):
        m = min(n - k, int(sr * 0.05))
        click[k:k + m] += np.exp(-np.linspace(0.0, 6.0, m)) * float(rng.uniform(0.6, 1.0))
    data = np.stack([left + click, right + click], axis=1)
    data += rng.standard_normal(data.shape) * 0.002
    pcm = np.round(np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.reshape(-1).tobytes())
    return path


def pick_material(tmpdir) -> tuple:
    """返回 `(路径, 是否真实文件, 说明)`。真实素材优先，缺了才生成 wav。"""
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p, True, "真实素材"
    out = _make_wav(os.path.join(tmpdir, "gen_44100.wav"))
    return out, False, "生成 wav（真实素材不在本机）"


def hashes_erccueside(path) -> tuple:
    """编排端口径：`AudioTrack`（原生率，纯 PyAV，**不 import PySide6**）→ `(hashes, sr)`。"""
    from ercue.audio.player import AudioTrack
    tr = AudioTrack(path, samplerate=None)
    if not tr.ok:
        raise RuntimeError("AudioTrack 解码失败：%s" % path)
    buf = tr.samples()
    if buf is None or buf.shape[1] == 0:
        raise RuntimeError("解码后没有样本：%s" % path)
    mono = buf.mean(axis=0) if buf.shape[0] > 1 else buf[0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    sr = int(tr.samplerate or fp_core.SAMPLE_RATE)
    return fp_core.fingerprint_audio(mono, sr), sr


# ==========================================================================
# 演出端口径（子进程：AutoVJ 的 `fp.fingerprint_audio` + soundfile 原生率解码）
# ==========================================================================
_AUTOVJ_CODE = r'''
import json, os, sys
autovj_src, path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, autovj_src)
import numpy as np
import soundfile as sf
import fp                                    # AutoVJ/src/fp.py（**同一套 Dejavu 算法**）
try:
    data, sr = sf.read(path, dtype="float32", always_2d=True)
except Exception as e:
    json.dump({"error": "%s: %s" % (type(e).__name__, e), "hashes": [], "sr": 0}, open(out_path, "w"))
    raise SystemExit(3)
if data is None or len(data) == 0:
    payload = {"hashes": [], "sr": int(sr or 0), "count": 0}
else:
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    hs = fp.fingerprint_audio(mono, int(sr))
    payload = {"hashes": [[str(h), int(o)] for h, o in hs], "sr": int(sr), "count": len(hs)}
payload["version"] = getattr(fp, "FP_SCHEMA_VERSION", None)
json.dump(payload, open(out_path, "w"))
'''


def hashes_showside(path, tmpdir) -> tuple:
    """演出端口径（子进程）→ `(hashes, sr, 备注)`。失败抛 RuntimeError（绝不假装通过）。"""
    if not os.path.isfile(AUTOVJ_PYTHON):
        raise RuntimeError("找不到演出端解释器：%s" % AUTOVJ_PYTHON)
    if not os.path.isfile(os.path.join(AUTOVJ_SRC, "fp.py")):
        raise RuntimeError("找不到演出端指纹实现：%s" % os.path.join(AUTOVJ_SRC, "fp.py"))
    code_path = os.path.join(tmpdir, "_showside_fp.py")
    out_path = os.path.join(tmpdir, "_showside_hashes.json")
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(_AUTOVJ_CODE)
    t0 = time.time()
    r = subprocess.run([AUTOVJ_PYTHON, code_path, AUTOVJ_SRC, path, out_path],
                       capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=1800)
    dt = time.time() - t0
    if r.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError("演出端算指纹失败（rc=%s）：%s"
                           % (r.returncode, (r.stderr or "").strip()[-500:]))
    with open(out_path, encoding="utf-8") as f:
        obj = json.load(f)
    if obj.get("error"):
        raise RuntimeError("演出端读文件失败：%s" % obj["error"])
    hashes = [(str(h), int(o)) for h, o in obj.get("hashes") or []]
    return hashes, int(obj.get("sr") or 0), "子进程 %.2fs，fp 版本 %s" % (dt, obj.get("version"))


# ==========================================================================
# 反向复现：把 pack 的 offset 从 int32 改成 int16（**临时副本**，不动仓库源码）
# ==========================================================================
def _mutate_int16(tmpdir) -> tuple:
    """生成"int32→int16"的变体模块并 import 成 `ercue.model._fp_pack_mut16`。

    为什么注册进 `ercue.model` 包名空间：`fp_pack.py` 里是 `from .fp_core import HASH_LEN`
    （相对 import），独立加载会失败；挂在包下才能解析。

    Returns:
        `(mut_module, 源码是否被改动过（应为 False）)`。
    """
    with open(fp_pack.__file__, encoding="utf-8") as f:
        src = f.read()
    mut = src.replace('struct.pack("<i", off_i)', 'struct.pack("<h", off_i)')
    if mut == src:
        raise AssertionError("变异失败：源码里找不到 `struct.pack(\"<i\", off_i)`")
    # ★ 去掉**打包侧**的自检断言，逼着**解包侧**的长度校验来接住（这才叫"反向复现第 3 条"）
    mut = mut.replace(
        "    if len(payload) - HEADER_SIZE != count * RECORD_SIZE:\n"
        "        raise AssertionError(\"指纹包载荷长度与 count 不自洽（打包逻辑有 bug）\")",
        "    if False:  # [mut16] 故意去掉打包侧自检\n"
        "        raise AssertionError(\"mut16\")")
    if "if False:  # [mut16]" not in mut:
        raise AssertionError("变异失败：找不到打包侧自检断言（被打包侧代码改过？）")
    path = os.path.join(tmpdir, "_fp_pack_mut16.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(mut)
    name = "ercue.model._fp_pack_mut16"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod, False


# ==========================================================================
# 六项检查
# ==========================================================================
def check_roundtrip(hashes) -> str:
    """① 往返逐条相等（全量 + 抽 2000 条）。"""
    blob = fp_pack.pack_hashes(hashes)
    assert blob, "非空哈希序列不能打成空字节"
    back = fp_pack.unpack_hashes(blob)
    assert back is not None, "合法数据解包返回了 None"
    assert len(back) == len(hashes), ("条数不一致", len(back), len(hashes))
    assert back == hashes, "全量往返不一致"
    sample = hashes[:SAMPLE_N]
    assert fp_pack.unpack_hashes(fp_pack.pack_hashes(sample)) == sample, \
        "抽 %d 条往返不一致" % SAMPLE_N
    # 头字段逐字节复核（两端契约的锚）
    raw = zlib.decompress(blob)
    assert raw[0:4] == b"ERFP" and raw[4] == 1 and raw[5] == fp_core.HASH_LEN == 20
    assert struct.unpack_from("<H", raw, 6)[0] == 0
    assert struct.unpack_from("<I", raw, 8)[0] == len(hashes)
    assert len(raw) - 12 == len(hashes) * 14, "载荷长度校验式不成立"
    return ("往返 %d 条逐条相等（抽样也过）；magic/version/hash_hex_len/reserved/count/长度 全部符合契约"
            % len(hashes))


def check_empty() -> str:
    """② 空列表 ⇒ b""；b"" ⇒ **[]**（钉死是 []，不是 None）。"""
    assert fp_pack.pack_hashes([]) == b"", "空列表必须返回 b''"
    got = fp_pack.unpack_hashes(b"")
    assert got == [], "unpack_hashes(b'') 必须是 []（**不是 None**），实际 %r" % (got,)
    assert fp_pack.unpack_hashes(None) == [], "unpack_hashes(None) 必须是 []"
    assert fp_pack.pack_hashes(None) == b"", "pack_hashes(None) 必须是 b''"
    return "空列表 ⇒ b''；unpack_hashes(b'') ⇒ []（类型也断言了：list，不是 None）"


def check_bad_data(blob) -> str:
    """③ 坏数据反例 ⇒ 全部 None，**绝不抛**。"""
    raw = zlib.decompress(blob)
    count = struct.unpack_from("<I", raw, 8)[0]
    cases = {}
    cases["截断 1 字节"] = blob[:-1]
    bad = bytearray(raw)
    bad[0:4] = b"XRPF"
    cases["magic 改错"] = zlib.compress(bytes(bad), 6)
    bad = bytearray(raw)
    bad[4] = 2
    cases["version 改成 2"] = zlib.compress(bytes(bad), 6)
    bad = bytearray(raw)
    struct.pack_into("<I", bad, 8, count + 1)
    cases["count 改大"] = zlib.compress(bytes(bad), 6)
    cases["非 zlib 垃圾"] = b"\xde\xad\xbe\xef-not-zlib"
    for label, data in cases.items():
        try:
            got = fp_pack.unpack_hashes(data)
        except Exception as e:                                    # noqa: BLE001
            raise AssertionError("%s 抛异常了（契约是不抛）：%s: %s" % (label, type(e).__name__, e))
        assert got is None, "%s 应返回 None，实际 %r" % (label, (got if got is None else len(got)))
    return "%d 个坏数据反例全部返回 None 且不抛：%s" % (len(cases), "、".join(cases))


def check_cross_end(path, our_hashes, our_sr, tmpdir) -> str:
    """④ 跨端交叉验证：前 1000 条 `(hash, offset)` 必须全等，总条数必须相同。"""
    their_hashes, their_sr, note = hashes_showside(path, tmpdir)
    assert int(their_sr) == int(our_sr), ("两端解码采样率不一致", their_sr, our_sr)
    assert len(their_hashes) == len(our_hashes), \
        ("哈希条数不一致（算法分叉）", len(their_hashes), len(our_hashes))
    n = min(CROSS_N, len(our_hashes))
    assert n > 0, "没有哈希可比对"
    for i in range(n):
        if our_hashes[i] != their_hashes[i]:
            raise AssertionError("第 %d 条不一致：编排端 %r ≠ 演出端 %r"
                                 % (i, our_hashes[i], their_hashes[i]))
    # 逐条全量也比一遍（前 1000 是硬要求，全量顺带兜住"后面才分叉"）
    diff = next((i for i, (a, b) in enumerate(zip(our_hashes, their_hashes)) if a != b), None)
    assert diff is None, "第 %d 条起不一致" % diff
    return ("采样率一致 %d Hz；条数一致 %d；**前 %d 条 (hash, offset) 逐条全等**；全量也全等（%s）"
            % (our_sr, len(our_hashes), n, note))


def check_cross_end_multi(materials, tmpdir, primary, primary_hashes, primary_sr) -> str:
    """④（多素材）：44.1k **和** 48k 都验 —— 48k 走的是 `np.interp` 重采样路径，
    正是历史上最容易分叉的地方（`tools/_fp_sig_parity.py` 就是因为这里出过血案）。"""
    lines = []
    for p in materials:
        if os.path.abspath(p) == os.path.abspath(primary):
            hashes, sr = primary_hashes, primary_sr
        else:
            hashes, sr = hashes_erccueside(p)
        detail = check_cross_end(p, hashes, sr, tmpdir)
        lines.append("%s（%d Hz，%d 条）：%s" % (os.path.basename(p), sr, len(hashes), detail))
    return "\n    ".join(lines)


def _build_pkg(dirpath, name, hashes, sig, path_label) -> tuple:
    """造一个只含 1 首曲目的小包 → `(包路径, 指纹成员字节)`。⑤ / ⑦ 共用。"""
    blob = fp_pack.pack_hashes(hashes)
    sheet = ercue_io.new_sheet("指纹包验收", "unit-test")
    te = ercue_io.new_track(os.path.basename(path_label), "验收", 0.0, 128.0)
    te.identity.fp_id = sig
    te.identity.file_name = os.path.basename(path_label)
    ly = CompLayer(lyr_id="L1", name="背景", z=0, blend="normal", opacity=1.0)
    ly.items.append(CueItem(item_id="i1", clip_id="c1", at_sec=8.0).normalized())
    te.composition.layers.append(ly)
    sheet.tracks.append(te)
    sheet.media.append(MediaRef(clip_id="c1", name="a.mp4"))
    pkg = os.path.join(dirpath, name + ercue_io.EXT)
    ercue_io.save(sheet, pkg, media_paths={}, embed=False, app_version="2026.10.18.01",
                  generator={"tool": "EasyRealityCue", "version": "2026.10.18.01"},
                  thumbs={}, fp_packs={sig: blob})
    return pkg, blob


def check_export_e2e(path, hashes, sig, sr, tmpdir, real) -> str:
    """⑤ 导出端到端：真包里有 `fp/<fp_id>.bin`、条数 > 1000、字节 < 1.5 MB、版本没变。"""
    name = "fp/%s.bin" % sig
    t0 = time.time()
    pkg, _blob = _build_pkg(tmpdir, "fp_e2e", hashes, sig, path)
    dt_with = time.time() - t0
    with zipfile.ZipFile(pkg) as z:
        names = z.namelist()
        assert name in names, ("包里没有 %s" % name, names)
        assert not any("\\" in n for n in names), ("成员名不能含反斜杠", names)
        data = z.read(name)
        meta = json.loads(z.read(ercue_io.JSON_NAME).decode("utf-8"))
    back = fp_pack.unpack_hashes(data)
    assert back == hashes, "包里的指纹解出来与原始哈希序列不一致"
    assert len(back) > 1000, ("条数太少：%d" % len(back))
    assert len(data) < MAX_PACK_BYTES, ("成员过大：%.1f KB" % (len(data) / 1024.0))
    assert int(meta["format_version"]) == int(ercue_io.FORMAT_VERSION), \
        ("FORMAT_VERSION 被改动了", meta["format_version"], ercue_io.FORMAT_VERSION)
    assert meta["tracks"][0]["identity"]["fp_id"] == sig

    # 辅助读接口（排查用）
    assert ercue_io.list_fp_members(pkg) == [name]
    assert ercue_io.read_fp_pack(pkg, sig) == data
    assert ercue_io.fp_member_name(sig) == name

    # 读包照常（老端兼容：多一个未知成员不影响 load）
    sheet2, _info = ercue_io.load(pkg)
    assert len(sheet2.tracks) == 1 and sheet2.tracks[0].identity.fp_id == sig

    # 反向：不带指纹 / fp_id 为空 / 内容为空 ⇒ 包里**不出现** fp/ 成员
    sheet = ercue_io.new_sheet("指纹包验收-无指纹", "unit-test")
    sheet.tracks.append(ercue_io.new_track("空", "验收", 0.0, 128.0))
    pkg2 = os.path.join(tmpdir, "fp_none" + ercue_io.EXT)
    t0 = time.time()
    ercue_io.save(sheet, pkg2, media_paths={}, embed=False, app_version="2026.10.18.01",
                  generator={"tool": "EasyRealityCue", "version": "2026.10.18.01"})
    dt_without = time.time() - t0
    assert ercue_io.list_fp_members(pkg2) == [], "没传 fp_packs 却写出了 fp/ 成员"
    pkg3 = os.path.join(tmpdir, "fp_dirty" + ercue_io.EXT)
    ercue_io.save(sheet, pkg3, media_paths={}, embed=False, app_version="2026.10.18.01",
                  generator={"tool": "EasyRealityCue", "version": "2026.10.18.01"},
                  fp_packs={"": data, "fp2-无此歌": b"", "../evil": data, sig: b""})
    assert ercue_io.list_fp_members(pkg3) == [], \
        ("空 fp_id / 空内容 / 非法 fp_id 都不该写成员", ercue_io.list_fp_members(pkg3))

    return ("包=%.1f KB（成员 %s，%d 条，解码 %d Hz%s）；format_version 仍为 %d；"
            "无 fp_packs 时不写成员；空/非法 fp_id 与空内容一律跳过；"
            "导出耗时 带指纹 %.0f ms / 不带 %.0f ms（+%.0f ms）"
            % (len(data) / 1024.0, name, len(back), sr,
               "，真实素材" if real else "，生成 wav",
               int(ercue_io.FORMAT_VERSION), dt_with * 1000, dt_without * 1000,
               (dt_with - dt_without) * 1000))


# ==========================================================================
# 格式跨端：我们打的包，必须能被**演出端 `cue_fp_pack`** 解开
# ==========================================================================
_AUTOVJ_PACK_CODE = r'''
import json, sys, zipfile
autovj_src, pkg_path, fp_id, out_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, autovj_src)
import cue_fp_pack as cfp            # 演出端那份**独立实现**（只依赖 stdlib）
name = cfp.blob_name(fp_id)
payload = {"name": name, "ok": False, "hashes": [], "member_bytes": 0, "via_api": False}
try:
    with zipfile.ZipFile(pkg_path) as z:
        raw = z.read(name)                       # 我们写进去的原始字节
    payload["member_bytes"] = len(raw)
    hs = cfp.unpack_hashes(raw)                 # ① 演出端的解包器读我们的字节
    if hs is not None:
        payload["hashes"] = [[str(h), int(o)] for h, o in hs]
        payload["ok"] = True
    # ② 演出端实际会调的入口（= 读成员 + 解包一步到位）
    api = cfp.read_from_package(pkg_path, fp_id)
    payload["via_api"] = (api is not None) and \
        ([[str(h), int(o)] for h, o in api] == payload["hashes"])
except Exception as e:
    payload["error"] = "%s: %s" % (type(e).__name__, e)
json.dump(payload, open(out_path, "w"))
'''


def check_pack_cross_format(hashes, sig, tmpdir) -> str:
    """⑦ 格式跨端（★ 地基）：**我们导出的包**用演出端 `cue_fp_pack.read_from_package()`
    读出来，必须与原始哈希序列**逐条相等**，成员名规则也必须一致。

    演出端 `src/cue_fp_pack.py` 是**同一格式的另一份独立实现**（两个仓库互不 import）。
    这一项证明"两端格式没有分叉"——只验哈希算法同口径（④）还不够：格式写错位，
    哈希再一致也灌不进演出端的库。若演出端文件还没落地 ⇒ 返回 `None`（跳过，不算通过）。
    """
    their_path = os.path.join(AUTOVJ_SRC, "cue_fp_pack.py")
    if not os.path.isfile(their_path):
        return None
    if not os.path.isfile(AUTOVJ_PYTHON):
        raise RuntimeError("演出端 cue_fp_pack.py 存在，但解释器缺失：%s" % AUTOVJ_PYTHON)
    pkg, _blob = _build_pkg(tmpdir, "fp_cross_fmt", hashes, sig, "x.flac")
    out_path = os.path.join(tmpdir, "_cross_fmt.json")
    code_path = os.path.join(tmpdir, "_showside_pack.py")
    with open(code_path, "w", encoding="utf-8") as f:
        f.write(_AUTOVJ_PACK_CODE)
    r = subprocess.run([AUTOVJ_PYTHON, code_path, AUTOVJ_SRC, pkg, sig, out_path],
                       capture_output=True, text=True, encoding="utf-8",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=1800)
    if r.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError("演出端读包失败（rc=%s）：%s"
                           % (r.returncode, (r.stderr or "").strip()[-500:]))
    with open(out_path, encoding="utf-8") as f:
        obj = json.load(f)
    if obj.get("error"):
        raise RuntimeError("演出端 cue_fp_pack 报错：%s" % obj["error"])
    assert obj.get("name") == fp_pack.member_name(sig), \
        ("两端成员名规则不一致", obj.get("name"), fp_pack.member_name(sig))
    assert obj.get("ok"), "演出端没能读出我们导出的指纹成员"
    theirs = [(str(h), int(o)) for h, o in obj.get("hashes") or []]
    assert len(theirs) == len(hashes), ("条数不一致", len(theirs), len(hashes))
    assert theirs == hashes, "演出端读出的哈希序列与原始序列不一致（格式分叉）"
    assert obj.get("via_api"), "演出端 `read_from_package()`（读成员+解包）结果与逐字节解包不一致"
    return ("演出端 cue_fp_pack 从**我们导出的包**里读回 %d 条，逐条与原始序列相等；"
            "`unpack_hashes()` 与 `read_from_package()` 两条路径结果一致；"
            "成员名规则一致（%s）；成员 %d 字节"
            % (len(theirs), obj.get("name"), int(obj.get("member_bytes") or 0)))


def check_mutation(tmpdir) -> str:
    """⑥ 反向复现：int32→int16 的变体必须被**解包侧长度校验**抓住。"""
    # 用小 offset 的合成序列（保证 int16 装得下，变异体才"打包成功"）
    small = [("%020x" % (i * 7919 + 12345), i * 3) for i in range(200)]
    mut, _touched = _mutate_int16(tmpdir)
    assert mut.RECORD_SIZE == fp_pack.RECORD_SIZE, "变异体应该只改 offset 宽度，不动 RECORD_SIZE"
    blob_mut = mut.pack_hashes(small)             # 不走打包自检（已去掉）
    assert blob_mut, "变异体打包失败（无法完成反向验证）"
    mut_unpack = mut.unpack_hashes(blob_mut)
    real_unpack = fp_pack.unpack_hashes(blob_mut)
    assert mut_unpack is None, "变异体自己都解不开自己的包？长度校验失效了"
    assert real_unpack is None, \
        "★ 长度校验没抓住 int16 变体（第 3 条那套坏数据检查形同虚设）"
    # 还原确认：仓库源码一字未动，正常数据仍然能往返
    with open(fp_pack.__file__, encoding="utf-8") as f:
        src_now = f.read()
    assert 'struct.pack("<i", off_i)' in src_now, "仓库源码被改动了（必须一字不改）"
    assert fp_pack.unpack_hashes(fp_pack.pack_hashes(small)) == small, "反向验证后正常路径坏了"
    return ("int32→int16 变体（RECORD_SIZE 仍为 14）打出的包被长度校验拒收（None）；"
            "第 3 条那套检查确实在起作用；仓库源码未被改动、正常路径仍往返一致")


# ==========================================================================
# 主流程
# ==========================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="指纹包（fp/<fp_id>.bin）验收")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("指纹包（.ercue 内 fp/<fp_id>.bin）验收")
    print("=" * 78)

    tmpdir = tempfile.mkdtemp(prefix="ercue_fp_pack_")
    path, real, mat_note = pick_material(tmpdir)
    print("素材：%s（%s）" % (path, mat_note))

    results = []
    frozen = {}

    def _run(title, fn):
        t0 = time.time()
        try:
            detail = fn()
            results.append((title, True, detail))
            print("\n[✓] %s  (%.2fs)\n    %s" % (title, time.time() - t0, detail))
        except Exception as e:                                    # noqa: BLE001
            detail = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc())
            results.append((title, False, detail))
            print("\n[✗] %s  (%.2fs)\n%s" % (title, time.time() - t0, detail))

    # 先把真实哈希算出来（后续 ①②③④⑥ 共用，避免重复解码）
    print("\n[..] 解码 + 计算编排端口径哈希…")
    t0 = time.time()
    try:
        hashes, sr = hashes_erccueside(path)
        frozen["hashes"], frozen["sr"] = hashes, sr
        frozen["sig"] = fp_core.signature_from_hashes(hashes)
        print("     条数 %d，采样率 %d Hz，签名 %s，耗时 %.2fs"
              % (len(hashes), sr, frozen["sig"], time.time() - t0))
    except Exception:                                             # noqa: BLE001
        print(traceback.format_exc())
        print("\nFP_PACK_FAIL  （算不出编排端口径哈希，后续检查无法进行）")
        return 1

    blob = fp_pack.pack_hashes(frozen["hashes"])
    print("     打包后 %.1f KB（原始载荷 %.1f KB，压缩率 %.0f%%）"
          % (len(blob) / 1024.0, (12 + 14 * len(hashes)) / 1024.0,
             100.0 * len(blob) / max(1.0, 12 + 14 * len(hashes))))

    _run("① 往返逐条相等", lambda: check_roundtrip(frozen["hashes"]))
    _run("② 空列表 / 空字节流契约", check_empty)
    _run("③ 坏数据反例全部 None（不抛）", lambda: check_bad_data(blob))
    # 44.1k + 48k 都跨端比一遍（48k 走重采样路径，历史上最容易分叉）
    materials = [p for p in CANDIDATES if os.path.isfile(p)] or [path]
    _run("④ 跨端哈希一致性（AutoVJ fp.fingerprint_audio）",
         lambda: check_cross_end_multi(materials, tmpdir, path, frozen["hashes"], frozen["sr"]))
    _run("⑤ 导出端到端（真包 → zip 成员 → 解包）",
         lambda: check_export_e2e(path, frozen["hashes"], frozen["sig"], frozen["sr"],
                                  tmpdir, real))
    _run("⑥ 反向复现（int32→int16 必须被抓住）", lambda: check_mutation(tmpdir))

    # ⑦ 格式跨端（★ 地基之二）：演出端的 `cue_fp_pack.py` 若已落地就**必须**验；
    #   未落地 ⇒ 明示"跳过"（绝不能假装通过）。
    print("\n[..] ⑦ 格式跨端：用演出端 cue_fp_pack 读我们导出的包…")
    t0 = time.time()
    try:
        detail = check_pack_cross_format(frozen["hashes"], frozen["sig"], tmpdir)
        if detail is None:
            print("     ⚠ 跳过：%s 还没落地（演出端同格式实现就绪后重跑本脚本即可）"
                  % os.path.join(AUTOVJ_SRC, "cue_fp_pack.py"))
        else:
            results.append(("⑦ 格式跨端（演出端 cue_fp_pack 解我们的包）", True, detail))
            print("\n[✓] ⑦ 格式跨端（演出端 cue_fp_pack 解我们的包）  (%.2fs)\n    %s"
                  % (time.time() - t0, detail))
    except Exception as e:                                    # noqa: BLE001
        detail = "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc())
        results.append(("⑦ 格式跨端（演出端 cue_fp_pack 解我们的包）", False, detail))
        print("\n[✗] ⑦ 格式跨端  (%.2fs)\n%s" % (time.time() - t0, detail))

    print("\n" + "=" * 78)
    ok_all = all(ok for _t, ok, _d in results)
    for title, ok, _d in results:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", title))
    print("汇总：%d/%d 通过" % (sum(1 for _t, ok, _d in results if ok), len(results)))

    if not args.keep:
        try:
            for fn in os.listdir(tmpdir):
                try:
                    os.remove(os.path.join(tmpdir, fn))
                except OSError:
                    pass
            os.rmdir(tmpdir)
        except OSError:
            pass
    else:
        print("（--keep：临时目录 %s）" % tmpdir)

    print("\n%s" % ("FP_PACK_OK" if ok_all else "FP_PACK_FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
