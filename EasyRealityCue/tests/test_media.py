# -*- coding: utf-8 -*-
"""M1 素材层专项验证（可复跑）。

    P:\\AutoVJ\\venv\\Scripts\\python.exe tests\\test_media.py

覆盖（对照 M1 验收标准）：
  1. 探测：视频 + 图片，duration/fps/尺寸合理
  2. alpha 判定：**只看 pix_fmt 会误判，实解帧才准**（本模块最易写错处，必须钉住）
  3. 缩略图：视频出非空 PNG 且能被 PyAV 重新解析；alpha 素材的 PNG **确实带 alpha**
  4. 退避：坏素材连续 100 次 frame() → 只尝试 1 次打开（A2）
  5. 复用：同路径连续 200 帧 → 底层容器只 open 1 次（A1）
  6. seek 精度：frame() 走关键帧，frame_exact() 精确到目标帧，差异符合预期
  7. 零 Qt：AST 扫 media/*.py 的 import 节点，断言 0 命中 Qt

测试夹具一律落在 `tempfile.mkdtemp()`（**不往项目根目录/用户目录写东西**）。
优先用真实素材库；素材不在也能靠自造夹具全绿。
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile

# 让测试可直接运行（不经安装）
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                            # noqa: E402
import av                                                     # noqa: E402

from ercue.media import probe as probe_mod                    # noqa: E402
from ercue.media import thumbs as thumbs_mod                  # noqa: E402
from ercue.media import frames as frames_mod                  # noqa: E402
from ercue.media.probe import probe, kind_of                  # noqa: E402
from ercue.media.thumbs import make_thumb, resolve_thumb, \
    thumb_path, encode_png_bgra                               # noqa: E402
from ercue.media.frames import CueFrameSource                 # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ercue_m1_test_")

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def _make_video(path, codec="libx264", pix_fmt="yuv420p", w=64, h=48,
                fps=24, nframes=72, alpha_mode=None, red_is_index=False, gop=None):
    """自造测试视频。`red_is_index=True` 时每帧 R 通道 = 帧序号（验证 seek 精度）。

    `gop` 非 None 时强制关键帧间隔（造"大 GOP"视频，供验证 frame() 停在关键帧的行为）。
    """
    container = av.open(path, "w")
    stream = container.add_stream(codec, rate=fps)
    stream.width, stream.height, stream.pix_fmt = w, h, pix_fmt
    if gop is not None:
        try:
            stream.codec_context.gop_size = int(gop)
        except Exception:                                          # noqa: BLE001
            pass
        try:
            stream.options = {"g": str(gop), "keyint_min": str(gop)}
        except Exception:                                          # noqa: BLE001
            pass
    for i in range(int(nframes)):
        arr = np.zeros((h, w, 4), dtype=np.uint8)
        if red_is_index:
            # 灰度 = 帧序号：RGB→YUV→RGB 往返对灰度几乎无损，R 通道均值≈帧序号
            arr[..., 0] = arr[..., 1] = arr[..., 2] = i % 256
        else:
            arr[..., 0] = (i * 7) % 256
            arr[..., 1] = (i * 13) % 256
            arr[..., 2] = 200
        if alpha_mode == "opaque":
            arr[..., 3] = 255
        elif alpha_mode == "vary":
            arr[..., 3] = 255
            arr[h // 4:h // 2, w // 4:w // 2, 3] = 0        # 中间挖一块透明
        for pkt in stream.encode(av.VideoFrame.from_ndarray(arr, format="rgba")):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()
    return path


def _make_png(path, w=100, h=60, alpha=255):
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 0] = 30
    arr[..., 1] = 160
    arr[..., 2] = 90
    arr[..., 3] = alpha
    with open(path, "wb") as f:
        f.write(encode_png_bgra(arr))
    return path


def _fixtures():
    """造齐夹具（视频不含 alpha / 视频 pix_fmt 说 alpha 但实为全不透明 / 真 alpha / 图片）。"""
    f = {}
    f["plain"] = _make_video(os.path.join(_TMP, "plain_x264.mp4"),
                             codec="libx264", pix_fmt="yuv420p",
                             fps=24, nframes=72)
    f["opaque_alpha_fmt"] = _make_video(os.path.join(_TMP, "opaque_alpha_fmt.mov"),
                                        codec="png", pix_fmt="rgba", fps=12,
                                        nframes=12, alpha_mode="opaque")
    f["real_alpha"] = _make_video(os.path.join(_TMP, "real_alpha.mov"),
                                  codec="png", pix_fmt="rgba", fps=12,
                                  nframes=12, alpha_mode="vary")
    f["indexed"] = _make_video(os.path.join(_TMP, "indexed.mp4"),
                               codec="libx264", pix_fmt="yuv420p",
                               fps=24, nframes=72, red_is_index=True)
    # ★ 强制**单个大 GOP**（keyint=250 > 72 帧）→ 整段只有第 0 帧是关键帧，
    #   用来证明旧 frame() 会停在关键帧（= 播放"卡住/减速"）。
    f["biggop"] = _make_video(os.path.join(_TMP, "biggop.mp4"),
                              codec="libx264", pix_fmt="yuv420p",
                              fps=24, nframes=72, red_is_index=True, gop=250)
    f["image"] = _make_png(os.path.join(_TMP, "still.png"), 100, 60)
    f["garbage"] = os.path.join(_TMP, "garbage.mp4")
    with open(f["garbage"], "wb") as fh:
        fh.write(bytes((i * 73 + 11) % 256 for i in range(8192)))
    return f


def _real_media_root():
    """从 %LOCALAPPDATA%\\AutoVJ\\config.json 和常见路径里找素材库。"""
    import json
    cands = []
    cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AutoVJ", "config.json")
    try:
        with open(cfg, "r", encoding="utf-8") as fh:
            txt = fh.read()
        import re
        for m in re.findall(r'"([A-Za-z]:[\\/][^"]+)"', txt):
            p = m.replace("\\\\", "\\").replace("\\", os.sep)
            base = os.path.basename(p.rstrip("\\/"))
            if base in ("素材库", "码表素材", "素材"):
                cands.append(p.rstrip("\\/"))
    except Exception:                                               # noqa: BLE001
        pass
    cands += [r"Y:\EasyRealityAutoVJ\素材库", r"Y:\EasyRealityAutoVJ\码表素材",
              r"P:\AutoVJ\dist\AutoVJ\素材库"]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return None


def _first_real(exts):
    root = _real_media_root()
    if not root:
        return None
    for fn in sorted(os.listdir(root)):
        if fn.lower().endswith(tuple(exts)):
            return os.path.join(root, fn)
    return None


def _real_movs(limit=120):
    root = _real_media_root()
    if not root:
        return []
    out = []
    for fn in sorted(os.listdir(root)):
        if fn.lower().endswith(".mov"):
            out.append(os.path.join(root, fn))
            if len(out) >= limit:
                break
    return out


def _decode_png_bytes(png: bytes):
    """用 PyAV 把 PNG 字节解回来（验证不是垃圾字节）。返回 (h,w,4) RGBA 或 None。"""
    import io
    try:
        c = av.open(io.BytesIO(png), metadata_errors="replace")
        st = c.streams.video[0]
        fr = next(iter(c.decode(video=0)), None)
        arr = fr.to_ndarray(format="rgba")
        c.close()
        return arr
    except Exception:                                               # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 1. 探测
# --------------------------------------------------------------------------
def test_probe_video_and_image(fx):
    r = probe(fx["plain"])
    assert r.ok, r
    assert r.kind == "video", r.kind
    assert abs(r.fps - 24.0) < 0.5, r.fps
    assert abs(r.duration_sec - 3.0) < 0.4, r.duration_sec
    assert (r.width, r.height) == (64, 48), (r.width, r.height)
    assert r.codec == "h264" and r.has_alpha is False, (r.codec, r.has_alpha)

    ri = probe(fx["image"])
    assert ri.ok and ri.kind == "image", ri
    assert (ri.width, ri.height) == (100, 60), (ri.width, ri.height)
    assert ri.duration_sec == 0.0 and ri.fps == 0.0, ri

    rb = probe(fx["garbage"])
    assert not rb.ok and rb.error, rb          # 坏素材返回 error，不抛

    real = _first_real([".mp4", ".mov"])
    note = "真实素材: 无（用自造夹具）"
    if real:
        rr = probe(real)
        assert rr.ok, rr
        assert rr.width > 0 and rr.height > 0 and rr.duration_sec > 0, rr
        note = "真实素材: %s  %dx%d %.1fs %s alpha=%s" % (
            os.path.basename(real), rr.width, rr.height, rr.duration_sec,
            rr.codec, rr.has_alpha)
    return "视频 64x48@24fps 3.0s / 图片 100x60 / 坏素材返回 error | " + note


# --------------------------------------------------------------------------
# 2. alpha 两层判定：只看 pix_fmt 会误判
# --------------------------------------------------------------------------
def test_alpha_two_layer_judgement(fx):
    # 2.1 合成：pix_fmt 说能带 alpha（rgba），实际全 255 → 必须判 False
    ro = probe(fx["opaque_alpha_fmt"])
    assert "rgba" in (ro.pix_fmt or "").lower(), ro.pix_fmt
    assert ro.has_alpha is False, "pix_fmt=rgba 但 alpha 全 255，应判无 alpha（两层判定）"

    # 2.2 合成：真带透明 → True
    ra = probe(fx["real_alpha"])
    assert ra.has_alpha is True, ra
    assert ra.fps == 12.0, ra.fps

    # 2.3 真实 DXV 批量：仅看 pix_fmt 的命中数必须**远大于**实解帧的命中数
    movs = _real_movs(120)
    if not movs:
        return ("合成夹具: pix_fmt=rgba+全255→False ✓ / 真透明→True ✓ | "
                "真实 DXV: 无（用合成夹具钉住误判）")

    marks = probe_mod.ALPHA_PIXFMT_MARKS
    pixfmt_possible = 0
    real_alpha = 0
    for p in movs:
        try:
            c = av.open(p, metadata_errors="replace")
            st = c.streams.video[0]
            pf = (st.pix_fmt or "").lower()
            c.close()
        except Exception:                                           # noqa: BLE001
            continue
        if any(m in pf for m in marks):
            pixfmt_possible += 1
        if probe(p).has_alpha:
            real_alpha += 1
    assert pixfmt_possible > 0, "真实素材里应有 pix_fmt 带 alpha 标记的"
    assert real_alpha > 0, "真实素材里应有真带 alpha 的"
    assert pixfmt_possible > real_alpha, (
        "『只看 pix_fmt 会误判』未成立：pixfmt_possible=%d real_alpha=%d"
        % (pixfmt_possible, real_alpha))
    return ("真实 DXV 抽样 %d 个: 仅看 pix_fmt 会判 alpha=%d，实解帧真带 alpha=%d "
            "→ 误判 %d 个被纠正（Δ=%d）"
            % (len(movs), pixfmt_possible, real_alpha,
               pixfmt_possible - real_alpha, pixfmt_possible - real_alpha))


# --------------------------------------------------------------------------
# 3. 缩略图
# --------------------------------------------------------------------------
def test_thumbnail(fx):
    png = make_thumb(fx["plain"], max_w=320)
    assert isinstance(png, bytes) and len(png) > 100, "视频缩略图应为非空 PNG 字节"
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG 魔数"
    arr = _decode_png_bytes(png)
    assert arr is not None and arr.shape[2] == 4, "缩略图必须能被 PyAV 重新解析"
    assert arr.shape[1] <= 320, arr.shape

    # alpha 素材：PNG 必须**确实带 alpha**
    apng = make_thumb(fx["real_alpha"], max_w=320)
    assert isinstance(apng, bytes) and apng[:8] == b"\x89PNG\r\n\x1a\n"
    aarr = _decode_png_bytes(apng)
    assert aarr is not None and aarr.shape[2] == 4, aarr
    assert int(aarr[..., 3].min()) < 255, "alpha 素材的缩略图应保留透明通道"

    # 不透明素材（pix_fmt 说 alpha）：缩略图仍应正常生成
    opng = make_thumb(fx["opaque_alpha_fmt"], max_w=320)
    assert opng and _decode_png_bytes(opng) is not None

    # 图片
    ipng = make_thumb(fx["image"], max_w=320)
    assert ipng and _decode_png_bytes(ipng) is not None

    # 坏路径
    assert make_thumb(os.path.join(_TMP, "nope.mp4")) is None

    # 缓存：两个扩展名都要试（alpha 真实图是 png、旧实现默认猜 jpg）
    cache_dir = os.path.join(_TMP, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    assert resolve_thumb(fx["plain"], cache_dir) is None
    with open(thumb_path(cache_dir, fx["plain"], ".jpg"), "wb") as fh:
        fh.write(png)                                          # 造一个 .jpg 缓存
    assert resolve_thumb(fx["plain"], cache_dir) is not None, "只 .jpg 存在时也要能命中"
    with open(thumb_path(cache_dir, fx["plain"], ".png"), "wb") as fh:
        fh.write(png)
    got = resolve_thumb(fx["plain"], cache_dir)
    assert got.endswith(".png"), "png 应优先"
    return "视频 PNG %dB 回解 %s / alpha PNG 回解 alpha_min=%d / 缓存双扩展名 OK" % (
        len(png), arr.shape, int(aarr[..., 3].min()))


# --------------------------------------------------------------------------
# 4. 复用（A1）
# --------------------------------------------------------------------------
def test_reuse_single_open(fx):
    src = CueFrameSource()
    p = fx["plain"]
    got = 0
    for i in range(200):
        arr = src.frame(p, (i % 60) / 20.0)
        if arr is not None:
            got += 1
    assert src.open_count_of(p) == 1, (
        "同一路径连续 200 帧，容器应只被 open 1 次，实际 %d" % src.open_count_of(p))
    assert got == 200, "200 次都应取到帧，实际 %d" % got
    assert arr.shape == (48, 64, 4), arr.shape
    assert len(src.cached_paths()) == 1
    src.close()
    assert src.cached_paths() == [], "close() 后必须释放全部解码器（A4）"
    return "200 帧 / 容器 open 次数 = %d / 帧形状 (48,64,4) / close 后缓存清空" % 1


# --------------------------------------------------------------------------
# 5. 退避（A2）
# --------------------------------------------------------------------------
def test_backoff_single_open(fx):
    src = CueFrameSource()
    p = fx["garbage"]                     # 存在但打不开 → 真正走到 av.open 才失败
    for _ in range(100):
        assert src.frame(p, 0.0) is None
    attempts = src.open_count_of(p)
    assert attempts == 1, (
        "坏素材连续 100 次 frame()，只应尝试打开 1 次（退避 2s），实际 %d" % attempts)

    # 不存在路径：连 av.open 都不该发生
    q = os.path.join(_TMP, "__no_such_media__.mp4")
    for _ in range(100):
        assert src.frame(q, 0.0) is None
    assert src.open_count_of(q) == 0, "不存在路径不该真的 open，实际 %d" % src.open_count_of(q)

    src.close()
    return "坏素材 100 次 frame() → open 尝试 = %d（退避 2s）；不存在路径 → open = 0" % attempts


# --------------------------------------------------------------------------
# 6. seek 精度：frame() 关键帧级 vs frame_exact() 精确帧级
# --------------------------------------------------------------------------
def test_seek_precision(fx):
    src = CueFrameSource()
    p = fx["indexed"]                     # 每帧 R 通道 = 帧序号
    fps = 24.0
    target_sec = 2.0
    expect_idx = int(round(target_sec * fps))

    fast = src.frame(p, target_sec)
    exact = src.frame_exact(p, target_sec)
    assert fast is not None and exact is not None

    fast_idx = int(fast[..., 0].mean())   # 近似帧序号（libx264 有损，取均值抗噪）
    exact_idx = int(exact[..., 0].mean())

    # 精确帧应贴近目标帧序号
    assert abs(exact_idx - expect_idx) <= 2, (exact_idx, expect_idx)
    # 关键帧路径落点 ≤ 目标（backward seek），且与精确帧不同（该视频 GOP 覆盖整段）
    assert fast_idx <= exact_idx + 1, (fast_idx, exact_idx)
    assert not np.array_equal(fast, exact), "关键帧帧与精确帧不应相同（内容逐帧变化）"

    # 关键帧对齐时刻：两条路径应一致
    a = src.frame(p, 0.0)
    b = src.frame_exact(p, 0.0)
    assert np.array_equal(a, b), "第 0 秒即关键帧，两条路径应给同一帧"
    src.close()
    return ("目标 %.1fs(帧%d)：frame→帧%d（关键帧级） frame_exact→帧%d（精确） "
            "| 0.0s 两者一致" % (target_sec, expect_idx, fast_idx, exact_idx))


# --------------------------------------------------------------------------
# 6b. 播放取帧 frame_play()：**顺序前进**（用户反馈"预览内开始后被拉长减速了"）
#     · frame_play 连续取帧应得**连续真实帧**（帧序号跟着目标走）；
#     · 逆向：旧 frame() 在同一关键帧区间内**停在关键帧**（不同帧极少）＝画面卡住。
# --------------------------------------------------------------------------
def test_frame_play(fx):
    src = CueFrameSource()
    p = fx["biggop"]                     # 每帧 R 通道 = 帧序号；强制**单个大 GOP**
    fps = 24.0
    targets = [round(0.1 * k, 3) for k in range(20)]          # 0.0 ~ 1.9s

    fast_idx = []
    for t in targets:
        fr = src.frame(p, t)                                  # 旧路径：每次 seek 关键帧
        fast_idx.append(None if fr is None else int(fr[..., 0].mean()))
    play_idx = []
    for t in targets:
        fr = src.frame_play(p, t)                             # 新路径：顺序前进
        play_idx.append(None if fr is None else int(fr[..., 0].mean()))

    def span(seq):
        vals = [v for v in seq if v is not None]
        return (max(vals) - min(vals)) if vals else 0

    adv_fast = span(fast_idx)
    adv_play = span(play_idx)
    # 逆向：旧 frame() 在同一关键帧区间内停在关键帧 → 帧序号几乎不动（= "卡住/减速"）
    assert adv_fast <= 4, "旧 frame() 本应停在关键帧，实际帧序号跨度=%d %s" % (adv_fast, fast_idx)
    # 新路径：连续前进（0 → ≈1.9s×24fps）
    assert adv_play >= 30, "frame_play() 应连续前进，实际帧序号跨度=%d %s" % (adv_play, play_idx)
    # 帧序号 ≈ 目标 × fps（位置真的跟上墙钟，未减速）
    for t, idx in zip(targets, play_idx):
        if idx is None:
            continue
        assert abs(idx - t * fps) <= 3, "t=%.1f 应≈帧%d，实际 %d" % (t, int(t * fps), idx)
    # 同位置重复取帧不跳帧（音频停住时画面必须停住）
    a = src.frame_play(p, 1.0)
    b = src.frame_play(p, 1.0)
    assert a is not None and b is not None and np.array_equal(a, b), "同位置重复取帧不该跳帧"
    src.close()
    return ("frame()帧序号跨度=%d（停在关键帧） frame_play()跨度=%d（连续前进≈目标×fps）"
            % (adv_fast, adv_play))


# --------------------------------------------------------------------------
# 7. 零 Qt：AST 检查 media/*.py
# --------------------------------------------------------------------------
def test_zero_qt():
    media_dir = os.path.join(_SRC, "ercue", "media")
    hits = []
    files = []
    for fn in sorted(os.listdir(media_dir)):
        if not fn.endswith(".py"):
            continue
        fp = os.path.join(media_dir, fn)
        files.append(fn)
        tree = ast.parse(open(fp, "r", encoding="utf-8").read(), filename=fp)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = [mod] + ["%s.%s" % (mod, a.name) for a in node.names]
            for n in names:
                if "PySide6" in n or n == "PyQt5" or n == "PyQt6" or \
                        n.split(".")[0] in ("PySide2",) or "Qt" in n.replace("QTimer", ""):
                    if "PySide" in n or "PyQt" in n or n.startswith("Qt"):
                        hits.append((fn, n))
    assert not hits, "media 层禁止 import Qt，命中: %r" % (hits,)
    return "AST 扫描 media/ 下 %s 共 %d 个 .py，Qt import 命中 0" % (files, len(files))


# --------------------------------------------------------------------------
# 运行器
# --------------------------------------------------------------------------
# 本文件既能 `python tests/test_media.py` 直接跑（见 main），也能被 pytest 收集。
# pytest 下 `fx` 是一个模块级夹具；未安装 pytest 时这段自动跳过，不影响直跑。
try:
    import pytest as _pytest

    @_pytest.fixture(scope="module")
    def fx():
        return _fixtures()

except Exception:                                                   # noqa: BLE001
    _pytest = None


_TESTS = [
    ("探测(视频+图片+坏素材)", test_probe_video_and_image),
    ("alpha 两层判定(只看pix_fmt会误判)", test_alpha_two_layer_judgement),
    ("缩略图(视频/alpha/图片/缓存)", test_thumbnail),
    ("A1 复用(200帧只open一次)", test_reuse_single_open),
    ("A2 退避(100次只尝试打开一次)", test_backoff_single_open),
    ("seek 精度(关键帧 vs 精确)", test_seek_precision),
    ("播放取帧 frame_play(连续前进)", test_frame_play),
    ("零 Qt(AST)", test_zero_qt),
]


def main() -> int:
    print("=" * 70)
    print("EasyRealityCue M1 素材层专项测试")
    print("临时夹具目录: %s" % _TMP)
    print("真实素材库  : %s" % (_real_media_root() or "<未找到>"))
    print("-" * 70)
    fx = _fixtures()
    failed = 0
    for name, fn in _TESTS:
        try:
            note = fn(fx) if fn.__code__.co_argcount else fn()
            print("[PASS] %-38s %s" % (name, note or ""))
        except AssertionError as e:
            failed += 1
            print("[FAIL] %-38s %s" % (name, e))
        except Exception as e:                                      # noqa: BLE001
            failed += 1
            print("[ERROR] %-37s %r" % (name, e))
    print("-" * 70)
    print("结果：%d 通过 / %d 失败" % (len(_TESTS) - failed, failed))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
