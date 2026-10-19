# -*- coding: utf-8 -*-
"""★ 导出包内缩略图验证（`thumbs/<clip_id>.png`）。

跑法：
    P:\\ERAVJ\\EasyRealityCue\\venv\\Scripts\\python.exe -u tools\\_thumbs_export_test.py

断言（对应与演出端约定的契约）：
  1. 包内 `thumbs/` 下有 **2 个 png**，文件名 = `cuesheet.json` 里的 `clip_id`；
  2. zip 内路径分隔符**必须是 `/`**（绝不能出现 `\\`）—— Windows 写成 `\\` 演出端解不出来；
  3. PNG 长边 ≤256；color type = 6（RGBA，alpha 通道存在）；
     带 alpha 的素材**真的保留了透明**（有像素 alpha < 255）；
  4. 只给**用到的**素材做（另加一个未使用素材 ⇒ 不得出现在 thumbs/ 里）；
  5. 同一素材第二次导出**走缓存**，耗时明显下降（报数字）；
  6. 20 个素材的总耗时（判据 ≤ 8s，且**在后台线程里**跑，GUI 不卡）。
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
import traceback
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ERCUE_NO_AUTOSAVE"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import numpy as np                                                # noqa: E402

from ercue.media import thumbs as thumbs_mod                      # noqa: E402

RESULTS = []


def _chk(tag, cond, detail=""):
    ok = bool(cond)
    RESULTS.append((tag, ok))
    print("   [%s] %s %s" % ("PASS" if ok else "FAIL", tag, detail))
    return ok


# --------------------------------------------------------------------------
# 素材：一张带真透明的 PNG + 一张不透明 PNG
# --------------------------------------------------------------------------
def _make_png(path, w, h, alpha=False, seed=1):
    """造一张 PNG（alpha=True 时四周留真透明）。复用 `thumbs.encode_png_bgra`。"""
    rng = np.random.default_rng(seed)
    a = np.zeros((h, w, 4), dtype=np.uint8)
    a[..., 0] = (np.linspace(20, 235, w, dtype=np.uint8))[None, :]
    a[..., 1] = (np.linspace(20, 235, h, dtype=np.uint8))[:, None]
    a[..., 2] = 128
    a[..., 3] = 255 if not alpha else 0
    if alpha:                                   # 中间一个不透明的圆（四周保持透明）
        yy, xx = np.mgrid[0:h, 0:w]
        m = ((yy - h / 2.0) ** 2 + (xx - w / 2.0) ** 2) < (min(w, h) * 0.35) ** 2
        a[m, 3] = 255
    a[..., :3] = (a[..., :3].astype(np.int32) + rng.integers(-10, 10, a[..., :3].shape)) \
        .clip(0, 255).astype(np.uint8)
    with open(path, "wb") as f:
        f.write(thumbs_mod.encode_png_bgra(a))
    return path


def png_info(data):
    """解析 PNG：返回 `(w, h, bit_depth, color_type, min_alpha_or_None)`（纯 stdlib）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    pos, w, h, depth, ctype = 8, 0, 0, 0, 0
    idat = b""
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    min_alpha = None
    if ctype == 6 and idat:
        import zlib
        raw = zlib.decompress(idat)
        stride = w * 4
        amin = 255
        for y in range(h):                      # filter 一律是 0（本仓库 PNG 编码器这么写）
            row = raw[y * (stride + 1) + 1: y * (stride + 1) + 1 + stride]
            if len(row) < stride:
                break
            amin = min(amin, int(np.min(np.frombuffer(row, np.uint8)[3::4])))
        min_alpha = amin
    return w, h, depth, ctype, min_alpha


# --------------------------------------------------------------------------
def main() -> int:
    from PySide6.QtWidgets import QApplication
    from ercue.ui.window import MainWindow

    print("=" * 78)
    print("导出包内缩略图验证（thumbs/<clip_id>.png）")
    print("=" * 78)
    app = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="ercue_thumbs_")
    a_png = _make_png(os.path.join(tmp, "alpha_asset.png"), 640, 360, alpha=True, seed=7)
    b_png = _make_png(os.path.join(tmp, "plain_asset.png"), 1920, 1080, alpha=False, seed=9)
    unused = _make_png(os.path.join(tmp, "unused_asset.png"), 320, 180, seed=11)

    win = MainWindow()
    win._interactive = False                 # 无头：不弹导出前检查/进度窗
    win.thumbs_enabled = True
    win._thumb_dir = os.path.join(tmp, "thumb_cache")     # 独立缓存目录，保证"第一次"是冷的
    try:
        win.new_sheet("thumbs 验证")
        ids = win.add_media_paths([a_png, b_png, unused])
        win.add_track("曲目", "测试", 60.0, 128.0)
        ly = win._layers()[0]
        # 只用前两个素材（第三个 unused 不入时间轴 ⇒ 不该有缩略图）
        from ercue.model.cuesheet import CueItem
        ly.items.append(CueItem(item_id="i1", clip_id=ids[0], at_sec=0.0).normalized())
        ly.items.append(CueItem(item_id="i2", clip_id=ids[1], at_sec=2.0).normalized())

        # ①' 交互路径（QProgressDialog + processEvents）单独验一次：
        #     无头测试走的是同步分支，不能因此漏掉真正用户会走的那条路。
        win._interactive = True
        try:
            got = win._run_bg_with_progress(
                lambda cb: ((cb(0, 2, "a"), cb(1, 2, "b"), cb(2, 2, "")) and "bg-done"),
                "正在生成码表缩略图（2 个素材）…")
        finally:
            win._interactive = False
        _chk("交互路径：后台线程 + 进度条能跑通", got == "bg-done", "返回=%r" % (got,))

        pkg = os.path.join(tmp, "with_thumbs.ercue")
        t0 = time.perf_counter()
        ok = win.export_sheet(pkg, force=True, embed=False)
        cost1 = time.perf_counter() - t0
        _chk("导出成功", ok, "%.2fs" % cost1)

        with zipfile.ZipFile(pkg) as z:
            names = z.namelist()
            print("   zip 内部结构：")
            for n in names:
                print("     %-28s %7d B" % (n, z.getinfo(n).file_size))
            json_name = next(n for n in names if n.endswith(".json") and "/" not in n)
            meta = json.loads(z.read(json_name).decode("utf-8"))
            clip_ids = [m["clip_id"] for m in meta["media"]]
            used = {it["clip_id"] for tr in meta["tracks"]
                    for la in tr["composition"]["layers"] for it in la["items"]}
            thumb_names = [n for n in names if n.startswith("thumbs/")]

            # ① 数量 / 名字
            _chk("thumbs/ 下 2 张（只给用到的素材做）", len(thumb_names) == 2,
                 "%r（用到=%s，全部 clip_id=%s）" % (thumb_names, sorted(used), clip_ids))
            want = {"thumbs/%s.png" % c for c in used}
            _chk("缩略图文件名 == clip_id", set(thumb_names) == want,
                 "差集=%r" % (set(thumb_names) ^ want))
            _chk("未使用的素材没有缩略图",
                 all(ids[2] not in n for n in thumb_names), "unused=%s" % ids[2])

            # ② 分隔符必须是 `/`
            backslash = [n for n in names if "\\" in n]
            _chk("zip 内路径分隔符全部是 `/`（无 `\\\\`）", not backslash, "违规=%r" % backslash)

            # ③ PNG 尺寸 / alpha
            bad = []
            for n in thumb_names:
                data = z.read(n)
                w, h, depth, ctype, amin = png_info(data)
                long_edge = max(w, h)
                is_alpha_asset = n == "thumbs/%s.png" % ids[0]
                cond = (long_edge <= 256 and ctype == 6 and depth == 8
                        and (amin is not None) and (amin < 255 if is_alpha_asset else True))
                print("     %-24s %dx%d depth=%d color_type=%d min_alpha=%s %s"
                      % (n, w, h, depth, ctype, amin, "OK" if cond else "BAD"))
                if not cond:
                    bad.append((n, w, h, ctype, amin))
            _chk("PNG 长边 ≤256 / RGBA(color type 6) / alpha 素材真保留透明", not bad, "%r" % (bad,))

            # ④ 包内不该有 FORMAT_VERSION 变化（向后兼容）
            _chk("未改 FORMAT_VERSION（仍是 1）", int(meta["format_version"]) == 1,
                 "format_version=%s" % meta["format_version"])

        # ⑤ 第二次导出走缓存（同一窗口/同一缓存目录）
        pkg2 = os.path.join(tmp, "with_thumbs2.ercue")
        t1 = time.perf_counter()
        ok2 = win.export_sheet(pkg2, force=True, embed=False)
        cost2 = time.perf_counter() - t1
        _chk("第二次导出成功", ok2, "%.2fs" % cost2)
        _chk("第二次导出走缓存（更快）", cost2 < cost1,
             "第一次 %.2fs → 第二次 %.2fs（节省 %.0f%%）"
             % (cost1, cost2, 100.0 * (cost1 - cost2) / max(1e-6, cost1)))

        # ⑥ 「素材 20 个」的实测总耗时（清空缓存，强制全量生成）
        import shutil
        shutil.rmtree(win._thumb_dir, ignore_errors=True)
        many = []
        d2 = os.path.join(tmp, "many")
        os.makedirs(d2, exist_ok=True)
        for k in range(20):
            many.append(_make_png(os.path.join(d2, "asset_%02d.png" % k),
                                  640 + 13 * k, 360 + 7 * k, alpha=(k % 5 == 0), seed=100 + k))
        win.new_sheet("20 个素材")
        ids20 = win.add_media_paths(many)
        win.add_track("曲目20", "测试", 60.0, 128.0)
        ly2 = win._layers()[0]
        for k, cid in enumerate(ids20):
            ly2.items.append(CueItem(item_id="m%d" % k, clip_id=cid,
                                     at_sec=float(k)).normalized())
        pkg20 = os.path.join(tmp, "many.ercue")
        t2 = time.perf_counter()
        ok20 = win.export_sheet(pkg20, force=True, embed=False)
        cost20 = time.perf_counter() - t2
        with zipfile.ZipFile(pkg20) as z:
            nth = len([n for n in z.namelist() if n.startswith("thumbs/")])
        _chk("20 个素材全部出缩略图", ok20 and nth == 20, "n=%d 耗时 %.2fs" % (nth, cost20))
        _chk("20 个素材总耗时 ≤8s", cost20 <= 8.0, "实测 %.2fs" % cost20)
        print("  ⇒ 20 个素材导出总耗时（含缩略图全量生成）：%.2fs（冷缓存）" % cost20)
    except Exception as e:                                       # noqa: BLE001
        print("  ✗ 异常：%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
        RESULTS.append(("脚本未异常完成", False))
    finally:
        try:
            win.shutdown()
        except Exception:                                        # noqa: BLE001
            pass

    print("\n" + "-" * 78)
    passed = sum(1 for _t, ok in RESULTS if ok)
    print("结果：%d 通过 / %d 失败" % (passed, len(RESULTS) - passed))
    for tag, ok in RESULTS:
        if not ok:
            print("   ✗ %s" % tag)
    print("汇总：%s" % ("全部通过 ✓" if passed == len(RESULTS) else "有失败 ✗"))
    print("THUMBS_EXPORT_TEST: %s" % ("OK" if passed == len(RESULTS) else "FAIL"))
    print("=" * 78)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
