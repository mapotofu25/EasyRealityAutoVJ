# -*- coding: utf-8 -*-
"""★ 指纹签名**两端同口径**验证（C3）：编排端算出的 `fp_id` 必须与演出端逐字节相同。

**为什么需要这个脚本**
  `identity.fp_id` 是 `.ercue` 跨端认歌的唯一判据：编排端（EasyRealityCue）算一遍写进码表，
  演出端（AutoVJ）对同一首歌算一遍来比对。两端的**指纹算法逐字节相同**
  （`src/ercue/model/fp_core.py` 的 `signature_from_hashes` / `fingerprint_audio`
  与 `AutoVJ/src/fp.py` 同名函数一致），但**喂进去的 PCM 口径**曾经不同：

    演出端：`soundfile.read(p, dtype="float32", always_2d=True)` → **文件原生率**
            （重采样只发生在 `fp.fingerprint_audio()` 内的 `np.interp`）
    编排端：`AudioTrack(path, samplerate=44100)` → PyAV + swresample **强制 44.1k**

  ⇒ 44.1k 素材两边一致，**48k 素材签名分叉**（且完全静默：认歌失败但没有任何报错）。

**本脚本怎么做**
  对同一批文件，各算两套签名并断言相等：
    A「演出端口径」：soundfile 原生率解码 → `np.mean(axis=1)` → `signature_from_hashes(
        fingerprint_audio(mono, sr_native))`
    B「编排端口径」：走**修好后的真实代码路径** `IdentityScanWorker._fp_signature()`
       （直接实例化 worker 调私有方法；QThread 不 `start()` 就不会跑 `run()`，安全）

**反向验证（项目铁律：测试要能反向复现）**
  `--legacy` 把 `ercue.audio.player.AudioTrack` 换成"强制 44.1k"的旧口径垫片，
  再跑同一套断言 ⇒ 48k 素材那条必须 **FAIL**（`_fp_signature()` 是函数内
  `from ... import AudioTrack`，所以在运行期打补丁即等价于改回旧写法）。

用法：
    venv\\Scripts\\python.exe tools\\_fp_sig_parity.py            # 正向：必须 PARITY_OK
    venv\\Scripts\\python.exe tools\\_fp_sig_parity.py --legacy   # 反向：48k 必须 FAIL
退出码：0 = 全部符合预期；1 = 不符合（正向不和 / 反向居然全过 / 素材缺失）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                # noqa: E402

from ercue.model import fp_core                                   # noqa: E402

# 签名格式：`fp2-` + 16 位小写 hex
SIG_RE = re.compile(r"^fp2-[0-9a-f]{16}$")

# 本机可用素材：期望采样率只用于挑选"至少一个 44.1k + 一个 48k"，实际率以解码结果为准。
CANDIDATES = [
    ("<素材库>/2of/tofu/Rolling Contact - Cosmic String.flac", 44100),
    ("W:/CloudMusic/Amateras Records/東方太嗨 -Touhou Toohigh Trance-/"
     "Amateras Records - Pretty World.mp3", 48000),
]

# ---- soundfile 定位 -------------------------------------------------------
# ★ 本仓库（编排端）**故意不依赖 soundfile**（生产代码不许引它）。但"演出端口径"的 A 侧
#   必须用 `soundfile.read` 才能证明两端同口径 ⇒ 本脚本自动找一个**装了 soundfile** 的
#   解释器（优先本进程；其次演出端 AutoVJ 的 venv；再次 `ERCUE_SF_PYTHON` 环境变量）。
#   全程**不修改任何 venv**。
SF_PYTHON_CANDIDATES = [
    "<项目根>/AutoVJ/venv/Scripts/python.exe",
]

# 子进程里跑的 A 侧片段（与进程内实现**同一套算法**，逐行对应）。
_SF_SUBPROCESS_CODE = r"""
import json, os, sys
_here = os.path.abspath(sys.argv[1])
sys.path.insert(0, _here)
import numpy as np
import soundfile as sf
from ercue.model import fp_core
path = sys.argv[2]
data, sr = sf.read(path, dtype="float32", always_2d=True)
if data is None or len(data) == 0:
    print(json.dumps({"sig": "", "sr": int(sr or 0)}))
else:
    mono = np.mean(data, axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    sig = fp_core.signature_from_hashes(fp_core.fingerprint_audio(mono, int(sr)))
    print(json.dumps({"sig": sig, "sr": int(sr)}))
"""


def _has_inline_soundfile():
    """本进程能不能直接 `import soundfile`。"""
    try:
        import soundfile  # noqa: F401
        return True
    except Exception:                                             # noqa: BLE001
        return False


def find_sf_python():
    """找一个**装了 soundfile** 的解释器。

    返回 `None` 表示"本进程就能 import soundfile"；返回路径表示"要用子进程跑 A 侧"；
    返回 `""` 表示"哪儿都没有"（A 侧算不出来 ⇒ 直接算失败，绝不假装验过）。
    """
    if _has_inline_soundfile():
        return None
    cands = []
    env = os.environ.get("ERCUE_SF_PYTHON", "").strip()
    if env:
        cands.append(env)
    cands.extend(SF_PYTHON_CANDIDATES)
    for exe in cands:
        if not exe or not os.path.isfile(exe):
            continue
        try:
            import subprocess
            r = subprocess.run([exe, "-c", "import soundfile, numpy; print(soundfile.__version__)"],
                               capture_output=True, text=True, timeout=120)
        except Exception:                                         # noqa: BLE001
            continue
        if r.returncode == 0:
            return exe
    return ""


# ==========================================================================
# A：演出端口径（AutoVJ `fp.py` 的解码方式）
# ==========================================================================
def signature_showside(path, sf_python=None):
    """演出端口径算签名。返回 `(签名, 原生采样率)`；算不出时签名返回 ""。"""
    if sf_python is None:
        if not _has_inline_soundfile():
            print("  ! 本进程没有 soundfile，且未指定替代解释器 ⇒ A 侧算不出来")
            return "", 0
        return _signature_showside_inline(path)
    if not sf_python:
        print("  ! 找不到装了 soundfile 的解释器（可用环境变量 ERCUE_SF_PYTHON 指定）"
              "⇒ A 侧算不出来")
        return "", 0
    return _signature_showside_subprocess(path, sf_python)


def _signature_showside_inline(path):
    """A 侧（进程内）：`soundfile.read` 原生率 → mono → 逐字节一致的指纹算法。"""
    import soundfile as sf
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception as e:                                        # noqa: BLE001
        print("  ! soundfile 读失败（%s: %s）" % (type(e).__name__, e))
        return "", 0
    if data is None or len(data) == 0:
        return "", int(sr or 0)
    mono = np.mean(data, axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    return fp_core.signature_from_hashes(fp_core.fingerprint_audio(mono, int(sr))), int(sr)


def _signature_showside_subprocess(path, sf_python):
    """A 侧（子进程）：用装了 soundfile 的解释器跑**同一套算法**（本仓库 venv 不引 soundfile）。"""
    import json
    import subprocess
    try:
        r = subprocess.run([sf_python, "-c", _SF_SUBPROCESS_CODE, _SRC, path],
                           capture_output=True, text=True, encoding="utf-8",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=1800)
    except Exception as e:                                        # noqa: BLE001
        print("  ! 子进程算 A 侧失败（%s: %s）" % (type(e).__name__, e))
        return "", 0
    if r.returncode != 0:
        print("  ! 子进程算 A 侧非零退出（%d）：%s" % (r.returncode, (r.stderr or "").strip()[-400:]))
        return "", 0
    try:
        obj = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:                                             # noqa: BLE001
        print("  ! 子进程输出无法解析：%r" % (r.stdout or "")[:200])
        return "", 0
    return str(obj.get("sig") or ""), int(obj.get("sr") or 0)


# ==========================================================================
# B：编排端口径（真实代码路径）
# ==========================================================================
def signature_erccueside(path):
    """编排端口径算签名：`IdentityScanWorker._fp_signature()`（改后的真实路径）。

    QThread **不 start** ⇒ `run()` 不会执行 ⇒ 只调私有方法，不启线程、不需要事件循环。
    """
    from ercue.ui.identity_scan import IdentityScanWorker
    w = IdentityScanWorker(path, 0)
    return w._fp_signature()


# ==========================================================================
# 反向验证垫片：把 AudioTrack 强制成旧口径（44.1k）——等价于改回旧写法
# ==========================================================================
def install_legacy_shim():
    """把 `ercue.audio.player.AudioTrack` 换成"忽略 samplerate、强制 44.1k"的旧口径。"""
    from ercue.audio import player as player_mod
    real_cls = player_mod.AudioTrack

    class _LegacyTrack(real_cls):
        """旧口径：`_fp_signature` 传 `samplerate=None`，这里强制 44100。"""

        def __init__(self, path, samplerate=None, channels=2):
            super().__init__(path, samplerate=fp_core.SAMPLE_RATE, channels=channels)

    player_mod.AudioTrack = _LegacyTrack
    print("  [legacy] 已把 AudioTrack 换成强制 %d Hz 的旧口径" % fp_core.SAMPLE_RATE)
    return real_cls


# ==========================================================================
# 素材：优先真实文件；都不在则**生成 wav**（并在报告里明说）
# ==========================================================================
def _make_wav(path, sr, seconds=12.0, seed=20261015):
    """造一段"有明确节拍峰"的 wav（16 位 PCM，用 stdlib `wave` 写，**不依赖 soundfile**）。

    内容 = 三音持续音 + 每 0.5 s 一次宽带瞬态（保证时间轴有严格局部极大 ⇒ 指纹非空）。
    """
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
    pcm = np.clip(data, -1.0, 1.0)
    pcm = np.round(pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.reshape(-1).tobytes())
    return path


def collect_materials(tmpdir):
    """返回 `[(标签, 路径, 是否真实素材)]`：至少一个 44.1k + 一个 48k。"""
    got = []
    for p, expect in CANDIDATES:
        if os.path.isfile(p):
            got.append(("%dk 真实素材" % (expect // 1000), p, True))
    if len(got) == len(CANDIDATES):
        return got
    # 缺哪个补哪个（生成的 wav 必须与真实文件**区分标注**）
    print("  ! 真实素材不全（找到 %d/%d 个）⇒ 缺失的用**生成 wav** 顶替" %
          (len(got), len(CANDIDATES)))
    have = set(os.path.normcase(os.path.abspath(p)) for _, p, _ in got)
    for p, expect in CANDIDATES:
        if os.path.isfile(p) and os.path.normcase(os.path.abspath(p)) in have:
            continue
        out = os.path.join(tmpdir, "gen_%d.wav" % expect)
        _make_wav(out, expect, seed=20261015 + expect)
        got.append(("%dk 生成素材" % (expect // 1000), out, False))
    return got


# ==========================================================================
# 主流程
# ==========================================================================
def check_one(label, path, expect_parity, sf_python=None):
    """算 A / B 两套签名并比对。返回 `(是否通过, 明细文字)`。"""
    sig_a, sr_a = signature_showside(path, sf_python)
    sig_b = signature_erccueside(path)
    same = bool(sig_a) and bool(sig_b) and (sig_a == sig_b)
    fmt_a = bool(SIG_RE.match(sig_a or ""))
    fmt_b = bool(SIG_RE.match(sig_b or ""))
    ok = fmt_a and fmt_b and (same == expect_parity)
    lines = [
        "  素材：%s" % label,
        "  路径：%s" % path,
        "  A 演出端（soundfile/%d Hz）：%s%s" % (sr_a, sig_a or "<空>",
                                                "" if fmt_a else "   ⚠格式不符"),
        "  B 编排端（IdentityScanWorker._fp_signature）：%s%s" % (
            sig_b or "<空>", "" if fmt_b else "   ⚠格式不符"),
        "  结论：%s（期望 %s）" % ("相同 ✓" if same else "不同 ✗",
                                 "相同" if expect_parity else "不同"),
    ]
    return ok, "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="指纹签名两端同口径验证（C3）")
    ap.add_argument("--legacy", action="store_true",
                    help="反向验证：把 AudioTrack 强制回旧口径（44.1k），48k 素材必须 FAIL")
    ap.add_argument("--keep", action="store_true", help="保留生成的临时 wav（便于人工复查）")
    args = ap.parse_args(argv)

    print("=" * 72)
    print("指纹签名两端同口径验证 %s" % ("【反向验证 legacy 44.1k】" if args.legacy else "【正向】"))
    print("=" * 72)

    tmpdir = tempfile.mkdtemp(prefix="ercue_fp_parity_")
    materials = collect_materials(tmpdir)
    sf_python = find_sf_python()
    if sf_python is None:
        print("A 侧 soundfile：本进程可用")
    elif sf_python:
        print("A 侧 soundfile：本进程没有 ⇒ 用 %s 跑子进程（本仓库 venv 不引 soundfile）" % sf_python)
    else:
        print("A 侧 soundfile：**全部解释器都没有** ⇒ 无法验证（不会假装通过）")
    if args.legacy:
        install_legacy_shim()

    # 正向：全部素材都必须相同；反向：44.1k 仍应相同、非 44.1k 必须不同
    results = []
    all_ok = True
    for label, path, _real in materials:
        expect_parity = True
        if args.legacy:
            _sig_probe, sr_probe = signature_showside(path, sf_python)
            expect_parity = (int(sr_probe) == int(fp_core.SAMPLE_RATE))
        try:
            ok, detail = check_one(label, path, expect_parity, sf_python)
        except Exception as e:                                    # noqa: BLE001
            ok, detail = False, "  素材：%s\n  异常：%s: %s\n%s" % (
                label, type(e).__name__, e, traceback.format_exc())
        results.append(detail)
        all_ok = all_ok and ok
        print("\n" + detail)

    print("\n" + "-" * 72)
    print("素材来源：%s" % ("、".join("%s%s" % (lb, "（真文件）" if real else "（生成 wav）")
                                    for lb, _p, real in materials)))
    if not args.keep:
        for _lb, p, real in materials:
            if not real:
                try:
                    os.remove(p)
                except OSError:
                    pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
    else:
        print("（--keep：临时素材保留在 %s）" % tmpdir)

    if args.legacy:
        # 反向：至少有一条（非 44.1k）必须"不同"，才算反向验证成立
        print("\n反向验证判定：%s" % ("成立 ✓（非 44.1k 素材签名确实分叉）" if all_ok
                                  else "不成立 ✗（旧口径居然也对上了 —— 说明修复没被验证到）"))
    print("\n%s" % ("PARITY_OK" if all_ok else "PARITY_FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
