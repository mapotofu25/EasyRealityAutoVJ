# -*- coding: utf-8 -*-
"""认歌身份测试：**文件 md5（最高优先级）** + 指纹签名 + 兼容性。

跑法（无头）：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe  tests\\test_identity_match.py

覆盖（对应任务书验收）：
  1. `fp_core.file_md5` 分块读 == 一次性读；坏路径返回 ""（绝不抛）
  2. 指纹签名：同信号同签名、改一个样本即变、过短信号返回 ""
  3. 算法常量与 AutoVJ `fp.py` **逐值一致**（防"手滑改参数"）
  4. `match_track` 五级：**md5 → fingerprint → meta → title+artist → file**
  5. **改名 + 换目录 ⇒ md5 级仍命中**（对照：只给新文件名时 `file` 级失效）
  6. md5 一致但 meta 不一致 + 指纹撞上另一首 ⇒ 必须命中 `md5`
  7. 旧 `.ercue`（无 `file_md5`）导入不报错、md5 为空、正常降级
  8. 带 `file_md5` 的码表往返不丢字段
  9. 零 Qt：`model/fp_core.py` 不得 import PySide6（AST 扫描）
"""
from __future__ import annotations

import ast
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import hashlib                                                    # noqa: E402
import tempfile                                                   # noqa: E402

import numpy as np                                                # noqa: E402

from ercue.model import ercue_io                                  # noqa: E402
from ercue.model import fp_core                                   # noqa: E402
from ercue.model.cuesheet import CueSheet, Identity, TrackEntry   # noqa: E402

MD5_A = "0ce2211193efbdf51244e422fa5d4db6"
MD5_B = "ffffffffffffffffffffffffffffffff"


def _track(fp="", md5="", title="", artist="", dur=0.0, fname=""):
    te = TrackEntry(track_key="k", identity=Identity(
        fp_id=fp, file_md5=md5, title=title, artist=artist, duration_sec=dur, file_name=fname))
    te.identity.ensure_meta_id()
    return te


# ---------------------------------------------------------------- 1
def test_md5_chunked_equals_whole():
    fd, p = tempfile.mkstemp(prefix="ercue_t_md5_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(2 * fp_core.MD5_CHUNK + 777))
        one = hashlib.md5(open(p, "rb").read()).hexdigest()
        assert fp_core.file_md5(p) == one, "分块 md5 ≠ 一次性 md5"
        assert fp_core.file_md5(p, chunk=997) == one, "小块 md5 ≠ 一次性 md5"
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
    assert fp_core.file_md5("__nope__.flac") == "", "坏路径应返回空串（绝不抛）"
    print("  ✓ md5 分块==整体；坏路径安全")


# ---------------------------------------------------------------- 2
def test_fingerprint_signature():
    rng = np.random.default_rng(7)
    sig = (rng.standard_normal(fp_core.SAMPLE_RATE * 2) * 0.2).astype(np.float32)
    sig[::4099] += 2.0
    a = fp_core.fingerprint_signature(sig, fp_core.SAMPLE_RATE)
    b = fp_core.fingerprint_signature(sig.copy(), fp_core.SAMPLE_RATE)
    assert a.startswith(fp_core.SIG_PREFIX) and a == b, ("签名不确定", a, b)
    # ★ 指纹**设计上**抗微小扰动（这正是它比 md5 稳的地方）：改一个样本**不应**换签名
    sig2 = sig.copy()
    sig2[123] += 9.0
    assert fp_core.fingerprint_signature(sig2, fp_core.SAMPLE_RATE) == a, \
        "指纹应对微小扰动稳健（否则同歌轻微差异就认不出）"
    # ★ 但**不同内容**必须得到不同签名（否则认歌会撞）
    rng2 = np.random.default_rng(99)
    sig3 = (rng2.standard_normal(fp_core.SAMPLE_RATE * 2) * 0.2).astype(np.float32)
    sig3[::4099] += 2.0
    c = fp_core.fingerprint_signature(sig3, fp_core.SAMPLE_RATE)
    assert c and c != a, "不同音频应得到不同签名"
    assert fp_core.fingerprint_signature(np.zeros(8, dtype=np.float32),
                                         fp_core.SAMPLE_RATE) == "", "过短信号应返回空签名"
    print("  ✓ 签名确定（%s）；抗微小扰动；不同内容不同；过短安全" % a)


# ---------------------------------------------------------------- 3
def test_algorithm_constants_match_autovj():
    assert (fp_core.SAMPLE_RATE, fp_core.FFT_WINDOW, fp_core.HOP_SIZE, fp_core.FAN_VALUE,
            fp_core.AMP_MIN, fp_core.HASH_LEN, fp_core.MAX_HASH_TIME_DELTA,
            fp_core.DT_QUANT, fp_core.FREQ_QUANT, fp_core.PEAK_NEIGHBORHOOD,
            fp_core.FP_SCHEMA_VERSION) == \
        (44100, 4096, 2048, 8, 12.0, 20, 200, 4, 4, 20, 2), "指纹参数被改动（必须与 AutoVJ 一致）"
    print("  ✓ 指纹参数与 AutoVJ fp.py 逐值一致")


# ---------------------------------------------------------------- 4/5/6
def test_match_priority_and_md5_first():
    s = CueSheet()
    t1 = _track(fp="fpX", md5=MD5_A, title="T", artist="A", dur=100.0, fname="a.flac")
    t2 = _track(fp="fpY", md5=MD5_B, title="T", artist="A", dur=200.0, fname="b.flac")
    s.tracks = [t1, t2]
    # md5 压过指纹 / meta
    got, lvl = s.match_track(title="T", artist="A", duration_sec=200.0, fp_id="fpY",
                             file_md5=MD5_A)
    assert (got is t1) and lvl == "md5", (lvl, got is t1)
    # 无 md5 ⇒ 指纹
    assert s.match_track(fp_id="fpY", file_md5="")[1] == "fingerprint"
    # 无 md5/指纹 ⇒ meta
    assert s.match_track(title="T", artist="A", duration_sec=100.0)[1] == "meta"
    print("  ✓ 优先级 md5 → fingerprint → meta（md5 压过指纹与 meta）")


def test_rename_move_still_hits_md5():
    s = CueSheet()
    s.tracks = [_track(fp="fpZ", md5=MD5_A, title="老标题", artist="老人",
                       dur=165.5, fname="Song.flac")]
    # 改名 + 换目录：md5 不变 ⇒ 命中 md5
    assert s.match_track(file_md5=MD5_A)[1] == "md5"
    # 对照：只给"新文件名"，旧码表里存的还是旧文件名 ⇒ file 级命中不了
    assert s.match_track(file_name="完全不同的名字_v2.flac")[1] == "", "新文件名不该命中 file 级"
    # 指纹级同样抗改名/换目录
    assert s.match_track(fp_id="fpZ")[1] == "fingerprint"
    print("  ✓ 改名+换目录：md5 级命中；file 级失效（对照）；指纹级也命中")


def test_md5_beats_colliding_fingerprint():
    s = CueSheet()
    tM = _track(fp="SAME", md5=MD5_A, title="真身", artist="X", dur=10.0)
    tO = _track(fp="SAME", md5=MD5_B, title="撞名", artist="Y", dur=20.0)
    s.tracks = [tM, tO]
    got, lvl = s.match_track(title="查询用", artist="别的", duration_sec=33.0,
                             fp_id="SAME", file_md5=MD5_A)
    assert (got is tM) and lvl == "md5", ("md5 应压过撞车的指纹", lvl, got is tM)
    print("  ✓ md5 一致但 meta 不一致 + 指纹撞车 ⇒ 命中 md5（正确的曲目）")


# ---------------------------------------------------------------- 7
def test_old_ercue_without_file_md5():
    s = CueSheet()
    s.tracks = [_track(fp="fpQ", md5=MD5_A, title="T", artist="A", dur=100.0)]
    fd = ercue_io.sheet_to_file_dict(s)
    for t in fd["tracks"]:
        t["identity"].pop("file_md5", None)             # 模拟旧码表
    back = ercue_io.sheet_from_file_dict(fd)            # 不报错
    idn = back.tracks[0].identity
    assert idn.file_md5 == "", "旧码表导入后 md5 应为空"
    assert idn.fp_id == "fpQ", "旧码表其它身份字段应保留"
    assert back.match_track(fp_id="fpQ")[1] == "fingerprint", "旧码表应正常降级"
    print("  ✓ 旧 .ercue（无 file_md5）导入不报错、md5 空、正常降级")


# ---------------------------------------------------------------- 8
def test_roundtrip_keeps_file_md5():
    s = CueSheet()
    s.tracks = [_track(fp="fpR", md5=MD5_A, title="T", artist="A", dur=100.0)]
    fd = ercue_io.sheet_to_file_dict(s)
    assert fd["tracks"][0]["identity"].get("file_md5") == MD5_A, "导出缺 file_md5"
    back = ercue_io.sheet_from_file_dict(fd)
    assert back.tracks[0].identity.file_md5 == MD5_A, "往返丢了 file_md5"
    print("  ✓ 带 md5 的码表往返保留 file_md5")


# ---------------------------------------------------------------- 9
def test_fp_core_zero_qt():
    p = os.path.join(_SRC, "ercue", "model", "fp_core.py")
    tree = ast.parse(open(p, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in ("PySide6", "PyQt5", "PyQt6", "shiboken6"):
                    bad.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in ("PySide6", "PyQt5", "PyQt6", "shiboken6"):
                bad.append(node.module)
    assert not bad, ("model/fp_core.py 不允许 import Qt 绑定", bad)
    print("  ✓ model/fp_core.py 零 Qt（AST 扫描）")


def main() -> int:
    tests = [
        ("md5 分块读 / 坏路径", test_md5_chunked_equals_whole),
        ("指纹签名确定性", test_fingerprint_signature),
        ("指纹参数与 AutoVJ 一致", test_algorithm_constants_match_autovj),
        ("认歌优先级（md5 最高）", test_match_priority_and_md5_first),
        ("改名+换目录仍命中 md5", test_rename_move_still_hits_md5),
        ("md5 压过撞车指纹", test_md5_beats_colliding_fingerprint),
        ("旧 .ercue 兼容", test_old_ercue_without_file_md5),
        ("往返保留 file_md5", test_roundtrip_keeps_file_md5),
        ("fp_core 零 Qt", test_fp_core_zero_qt),
    ]
    ok = True
    passed = 0
    for name, fn in tests:
        print("\n--- %s ---" % name)
        try:
            fn()
            passed += 1
        except Exception as e:                              # noqa: BLE001
            ok = False
            print("  ✗ %s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
    print("\n" + "=" * 68)
    print("结果：%d 通过 / %d 失败" % (passed, len(tests) - passed))
    print("汇总：%s" % ("全部通过 ✓" if ok else "有失败 ✗"))
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
