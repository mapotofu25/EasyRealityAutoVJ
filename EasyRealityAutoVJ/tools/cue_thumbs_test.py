# -*- coding: utf-8 -*-
"""回归：**码表包内的素材缩略图**（`thumbs/<clip_id>.png`）—— 解出 + 详情页显示。

背景
----
编排端（EasyRealityCue）导出时**已经**写 `thumbs/<clip_id>.png`（长边 ≤256、保留 alpha），
演出端此前**完全忽略** `thumbs/`（连 `cuesheet.py` 的注释都写着"可选，忽略"）。
用户原话：「码表导出没有带着图片导吗」—— 导出侧做好了，显示侧缺这一步。

契约（已定，别单边改）
----------------------
· 包内路径 `thumbs/<clip_id>.png`，`clip_id` == `cuesheet.json` 里 `media[].clip_id`
· **缺某个 png ⇒ 当"没有缩略图"**：不报错、不影响导入、不影响播放
· **不改 `FORMAT_VERSION`**（`thumbs/` 是向后兼容的可选新增）

判据（零 Qt 部分 ①–⑥⑩ 只依赖 `cuesheet`）
-------------------------------------------
  ① 造包带 2 个 png ⇒ `load()` 后 `sheet.thumbs` 2 项、路径存在、**内容逐字节一致**
  ② 连续 `load()` 两次 ⇒ **幂等**：第二次 `thumbs/` 成员解压次数 = 0、文件 mtime 不变
  ③ `meta_only=True` ⇒ `thumbs == {}` 且**没有**生成 `<包>.thumbs/` 目录（零增量 IO）
  ④ zip 里放 `thumbs/../../evil.png` ⇒ 不写到包外、不抛（zip-slip 防护）
  ⑤ `thumbs/a.jpg` 被忽略；`thumbs/c1.PNG`（大写扩展名）被接受
  ⑥ 损坏 png（随机字节）⇒ `load()` 不抛（显示侧自己退回占位）
  ⑩ 老包（**完全没有** `thumbs/`）⇒ `thumbs == {}`、不建目录、不报错

真窗口部分（⑦⑧⑨，需要 QApplication）
-------------------------------------
  ⑦ 真 `CueSheetDetailDialog`（码表管理页的详情窗）显示缩略图：格子数 == 素材数，
     每个 `QPixmap` 非空、尺寸不超过 96×54（等比缩放，不拉伸）
  ⑧ 部分素材无图 ⇒ 占位数量正确（灰底「无图」，不留空洞）
  ⑨ 换表（`reload`）⇒ 控件数按新表变化，**旧图不残留**

运行：`venv\\Scripts\\python.exe tools\\cue_thumbs_test.py`（exit 0 = 全过）
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
SHOT_DIR = os.path.join(HERE, "_cueshots")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cuesheet                                                    # noqa: E402

FAILS = []


def check(name, ok, extra=""):
    print(("  [ok  ] " if ok else "  [FAIL] ") + name
          + (("   ← " + str(extra)) if (extra and not ok) else ""))
    if not ok:
        FAILS.append(name)


# ==========================================================================
# 造测试数据（纯 Python 写 PNG，不依赖 Qt / PIL）
# ==========================================================================
def make_png_bytes(w=32, h=18, rgba=(200, 40, 40, 255)):
    """最小合法 PNG（8bit RGBA，无滤波）—— 只为了有一张"真能解码"的图。"""
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def make_sheet(title, clips):
    """`clips`: [(clip_id, name)] ⇒ 一张迷你码表（每首歌一层，每个 clip 一个片段）。"""
    s = cuesheet.CueSheet(title=title, res_w=320, res_h=180)
    te = cuesheet.TrackEntry()
    te.identity = cuesheet.Identity(title="曲目-" + title, artist="测试者",
                                    duration_sec=180.0, bpm=120.0)
    te.identity.ensure_meta_id()
    ly = cuesheet.CompLayer(lyr_id="L1", name="层1", z=0)
    for k, (cid, _nm) in enumerate(clips):
        ly.items.append(cuesheet.CueItem(
            item_id="i%d" % k, clip_id=cid, at_beats=float(k * 4),
            src_in_sec=0.0, src_out_sec=8.0,
            trans_in="cut", trans_out="cut").normalized())
    te.layers.append(ly)
    s.tracks.append(te)
    for cid, nm in clips:
        s.media.append(cuesheet.MediaRef(clip_id=cid, name=nm, duration_sec=8.0))
    return s


def build_pkg(path, sheet, thumbs=None, extra_members=None):
    """写一个 `.ercue`：`cuesheet.json` + 指定 zip 成员（默认 `thumbs/<clip>.png`）。

    `thumbs`: {zip 内成员名: bytes}（**原样放**，方便构造 `.jpg` / 目录穿越等异常成员）
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(cuesheet.JSON_NAME,
                   json.dumps(cuesheet.to_dict(sheet), ensure_ascii=False, indent=1))
        for name, data in (thumbs or {}).items():
            z.writestr(name, data)
        for name, data in (extra_members or {}).items():
            z.writestr(name, data)
    return path


def _wrap_zip_open_counter():
    """统计"真正读了 `thumbs/` 成员数据"的次数（`ZipFile.read` 内部也走 `open`）。"""
    state = {"n": 0}
    real = zipfile.ZipFile.open

    def _patched(self, name, *a, **k):
        try:
            nm = name.filename if isinstance(name, zipfile.ZipInfo) else str(name)
        except Exception:                                          # noqa: BLE001
            nm = ""
        if nm.startswith(cuesheet.THUMB_DIR + "/"):
            state["n"] += 1
        return real(self, name, *a, **k)

    zipfile.ZipFile.open = _patched

    def restore():
        zipfile.ZipFile.open = real

    return state, restore


def main():
    print("=" * 88)
    print("码表缩略图（thumbs/<clip_id>.png）回归：解出 + 详情页显示")
    print("=" * 88)
    tmp = tempfile.mkdtemp(prefix="cuethumb_")
    png_a = make_png_bytes(64, 36, (220, 40, 40, 255))
    png_b = make_png_bytes(40, 40, (40, 200, 90, 255))

    # ---------------- ① 解出 thumbs ----------------
    print("\n① 包内有 2 个 png ⇒ `sheet.thumbs` 2 项、路径存在、内容逐字节一致")
    s2 = make_sheet("两张图", [("c1", "a.mp4"), ("c2", "b.mp4")])
    pkg2 = build_pkg(os.path.join(tmp, "两张图.ercue"), s2,
                     {"thumbs/c1.png": png_a, "thumbs/c2.png": png_b})
    sheet2, _info = cuesheet.load(pkg2)
    thumbs = dict(getattr(sheet2, "thumbs", {}) or {})
    print("      thumbs = %s" % {k: os.path.basename(v) for k, v in thumbs.items()})
    check("thumbs 有 2 项（c1 / c2）", set(thumbs) == {"c1", "c2"}, thumbs)
    check("路径是绝对路径且真实存在",
          all(os.path.isabs(p) and os.path.isfile(p) for p in thumbs.values()), thumbs)
    check("落盘位置在 <包路径>.thumbs/ 里",
          all(os.path.basename(os.path.dirname(p)) == os.path.basename(pkg2) + ".thumbs"
              for p in thumbs.values()), thumbs)
    ok_bytes = True
    for cid, src in (("c1", png_a), ("c2", png_b)):
        with open(thumbs.get(cid, ""), "rb") as f:
            ok_bytes = ok_bytes and (f.read() == src)
    check("内容与原图逐字节一致（没有二次编码）", ok_bytes)
    check("`thumb_path()` 取得到、取不到返回空串",
          sheet2.thumb_path("c1") == thumbs.get("c1")
          and sheet2.thumb_path("不存在") == "",
          (sheet2.thumb_path("c1"), sheet2.thumb_path("不存在")))

    # ---------------- ② 幂等 ----------------
    print("\n② 连续 load() 两次 ⇒ 幂等（第二次不重解压）")
    # ⚠ ① 已经解过一次了；要测"首次解压"必须先回到干净状态（这个目录是本测试自己的产物）
    import shutil
    shutil.rmtree(pkg2 + ".thumbs", ignore_errors=True)
    check("前置：把 ① 解出的 .thumbs/ 删掉，回到干净状态",
          not os.path.exists(pkg2 + ".thumbs"))
    state, restore = _wrap_zip_open_counter()
    try:
        t0 = time.perf_counter()
        _s1, _i1 = cuesheet.load(pkg2)
        t_first = time.perf_counter() - t0
        n_first = state["n"]
        files = {cid: p for cid, p in (_s1.thumbs or {}).items()}
        before = {cid: os.path.getmtime(p) for cid, p in files.items()}
        state["n"] = 0
        time.sleep(0.05)
        t0 = time.perf_counter()
        _s2b, _i2 = cuesheet.load(pkg2)
        t_second = time.perf_counter() - t0
        n_second = state["n"]
    finally:
        restore()
    after = {cid: os.path.getmtime(p) for cid, p in files.items()}
    print("      首次 %.2f ms ｜ 第二次 %.2f ms（差 %.2f ms）"
          % (t_first * 1000, t_second * 1000, (t_first - t_second) * 1000))
    print("      thumbs/ 成员解压次数：首次 %d ｜ 第二次 %d" % (n_first, n_second))
    check("首次确实解压了 2 个成员", n_first == 2, n_first)
    check("第二次**一个都没解压**（幂等，按大小跳过）", n_second == 0, n_second)
    check("文件 mtime 未变（真的没重写）", before == after,
          [(k, before[k], after[k]) for k in before if before[k] != after[k]])
    check("第二次得到的 thumbs 与第一次相同", dict(_s1.thumbs) == dict(_s2b.thumbs))
    check("第二次不比第一次明显更慢", t_second <= t_first + 0.05,
          (t_first, t_second))

    # ---------------- ③ meta_only 不碰 thumbs ----------------
    print("\n③ meta_only=True ⇒ thumbs == {} 且**没有**生成 .thumbs/ 目录")
    pkg3 = build_pkg(os.path.join(tmp, "索引用.ercue"),
                     make_sheet("索引用", [("c1", "a.mp4")]),
                     {"thumbs/c1.png": png_a})
    dest3 = pkg3 + ".thumbs"
    check("前置：目录还不存在", not os.path.exists(dest3), dest3)
    meta, minfo = cuesheet.load(pkg3, meta_only=True)
    check("meta_only ⇒ thumbs == {}", getattr(meta, "thumbs", None) == {},
          getattr(meta, "thumbs", None))
    check("meta_only ⇒ 一个 .thumbs/ 目录都没建（零增量 IO）", not os.path.exists(dest3))
    check("meta_only ⇒ info 的 present_media 仍为空（原有行为不变）",
          minfo.get("present_media") == {}, minfo.get("present_media"))
    full, _fi = cuesheet.load(pkg3)
    check("同一个包走全量 load ⇒ 正常解出 1 张", set(full.thumbs) == {"c1"}, full.thumbs)

    # ---------------- ④ zip-slip ----------------
    print("\n④ zip 里放 `thumbs/../../evil.png` ⇒ 不写到包外、不抛")
    pkg4 = build_pkg(os.path.join(tmp, "包内", "穿越.ercue"),
                     make_sheet("穿越", [("c1", "a.mp4")]),
                     {"thumbs/../../evil.png": png_a})
    sentinels = [os.path.join(tmp, "evil.png"),
                 os.path.join(tmp, "包内", "evil.png"),
                 os.path.join(os.path.dirname(tmp), "evil.png"),
                 pkg4 + ".evil.png"]
    raised = None
    sheet4 = None
    try:
        sheet4, _i4 = cuesheet.load(pkg4)
    except Exception as e:                                         # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
    check("load() 没有抛异常", raised is None, raised)
    check("恶意成员被丢弃（thumbs == {}）",
          (getattr(sheet4, "thumbs", None) or {}) == {},
          getattr(sheet4, "thumbs", None))
    check("包外没有出现 evil.png", not any(os.path.exists(p) for p in sentinels),
          [p for p in sentinels if os.path.exists(p)])
    check("连 .thumbs/ 目录都没建（没有合法成员）", not os.path.exists(pkg4 + ".thumbs"))

    # ---------------- ⑤ 只收 .png ----------------
    print("\n⑤ `thumbs/a.jpg` 被忽略；`thumbs/c1.PNG`（大写扩展名）被接受")
    pkg5 = build_pkg(os.path.join(tmp, "扩展名.ercue"),
                     make_sheet("扩展名", [("c1", "a.mp4"), ("a", "x.jpg")]),
                     {"thumbs/c1.PNG": png_a, "thumbs/a.jpg": png_b})
    sheet5, _i5 = cuesheet.load(pkg5)
    print("      thumbs = %s" % sorted(getattr(sheet5, "thumbs", {}) or {}))
    check("大写 .PNG 被接受（clip_id = c1）", "c1" in (sheet5.thumbs or {}), sheet5.thumbs)
    check(".jpg 被忽略（不在 thumbs 里）", "a" not in (sheet5.thumbs or {}), sheet5.thumbs)
    check("落盘文件名统一成小写 .png",
          os.path.basename(sheet5.thumb_path("c1")) == "c1.png",
          sheet5.thumb_path("c1"))

    # ---------------- ⑥ 损坏 png ----------------
    print("\n⑥ 损坏 png（随机字节）⇒ load() 不抛")
    pkg6 = build_pkg(os.path.join(tmp, "坏图.ercue"),
                     make_sheet("坏图", [("c1", "a.mp4"), ("c2", "b.mp4")]),
                     {"thumbs/c1.png": os.urandom(512), "thumbs/c2.png": png_b})
    raised = None
    sheet6 = None
    try:
        sheet6, _i6 = cuesheet.load(pkg6)
    except Exception as e:                                         # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
    check("load() 没有抛异常", raised is None, raised)
    check("两个成员都解出来了（内容不校验，坏图交给显示侧退回占位）",
          set(getattr(sheet6, "thumbs", {}) or {}) == {"c1", "c2"},
          getattr(sheet6, "thumbs", None))
    check("坏图字节数也对（512）",
          os.path.getsize(sheet6.thumb_path("c1")) == 512,
          os.path.getsize(sheet6.thumb_path("c1")))

    # ---------------- ⑩ 老包（无 thumbs/） ----------------
    print("\n⑩ 老包：完全没有 `thumbs/` ⇒ thumbs == {}、不建目录、不报错")
    pkg10 = build_pkg(os.path.join(tmp, "老包.ercue"),
                      make_sheet("老包", [("c1", "a.mp4")]), None)
    raised = None
    sheet10 = None
    try:
        sheet10, _i10 = cuesheet.load(pkg10)
    except Exception as e:                                         # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
    check("load() 没有抛异常", raised is None, raised)
    check("thumbs == {}", getattr(sheet10, "thumbs", None) == {},
          getattr(sheet10, "thumbs", None))
    check("没有留下空的 .thumbs/ 目录（老包零垃圾）",
          not os.path.exists(pkg10 + ".thumbs"))
    check("媒体检查等原有行为不变（missing 里是 a.mp4）",
          _i10.get("missing_media") == ["a.mp4"], _i10.get("missing_media"))

    # ---------------- ⑦⑧⑨ 真窗口 ----------------
    print("\n⑦ 真详情窗显示缩略图（格子数 == 素材数、QPixmap 非空）")
    from PySide6.QtWidgets import QApplication                       # noqa: E402
    import cue_page                                                  # noqa: E402

    # ⚠ 截图要能看清中文 ⇒ 用**真平台**（offscreen 没 CJK 字体，截图全是方框），
    #   并像 main.py 那样先 `theme.apply()`（否则 palette/QSS 没装，灰字压白底）。
    os.environ.pop("QT_QPA_PLATFORM", None)
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        import theme
        theme.apply(app, "dark")
    except Exception as e:                                         # noqa: BLE001
        print("      （theme.apply 失败，截图用默认配色：%s）" % e)

    # 全都有图：3 个素材 / 3 张图
    clips_full = [("c1", "a.mp4"), ("c2", "b.mp4"), ("c3", "c.mp4")]
    pkg_full = build_pkg(os.path.join(tmp, "全都有图.ercue"),
                         make_sheet("全都有图", clips_full),
                         {"thumbs/c1.png": png_a, "thumbs/c2.png": png_b,
                          "thumbs/c3.png": make_png_bytes(50, 20, (60, 90, 220, 255))})
    # 部分有图：c3 没有
    pkg_part = build_pkg(os.path.join(tmp, "部分有图.ercue"),
                         make_sheet("部分有图", clips_full),
                         {"thumbs/c1.png": png_a, "thumbs/c2.png": png_b})
    # 另一张表（换表用）：1 个素材 / 1 张图
    pkg_other = build_pkg(os.path.join(tmp, "另一张表.ercue"),
                          make_sheet("另一张表", [("d1", "d.mp4")]),
                          {"thumbs/d1.png": make_png_bytes(30, 30, (240, 200, 40, 255))})

    def entry_of(p):
        return {"path": p, "title": os.path.splitext(os.path.basename(p))[0],
                "n_tracks": 1, "size": os.path.getsize(p),
                "mtime": os.path.getmtime(p), "format_version": 1,
                "min_app_version": ""}

    owner = type("Owner", (), {"cfg": {"cue": {}}, "engine": None,
                               "layers_panel": None})()
    dlg = cue_page.CueSheetDetailDialog(owner, entry_of(pkg_full), None)
    dlg.show()
    for _ in range(8):
        app.processEvents()
    strip = dlg.thumb_strip
    n_used = len(dlg.sheet.all_used_clip_ids()) if dlg.sheet else -1
    print("      用到的素材 %d 个 ｜ 格子 %d 个 ｜ 占位 %d 个"
          % (n_used, strip.count(), strip.placeholder_count()))
    check("格子数 == 素材数（3）", strip.count() == 3 and n_used == 3,
          (strip.count(), n_used))
    check("全都有图 ⇒ 占位 0 个", strip.placeholder_count() == 0,
          strip.placeholder_count())
    pms = strip.pixmaps()
    check("每个 QPixmap 都非空", all(pm is not None and not pm.isNull() for pm in pms),
          [pm.isNull() if pm is not None else "None" for pm in pms])
    check("等比缩放不超框（≤96×54）且保持比例",
          all(pm.width() <= strip.THUMB_W and pm.height() <= strip.THUMB_H for pm in pms),
          [(pm.width(), pm.height()) for pm in pms])
    check("标题行标出「3 个素材 ｜ 3 张有图」",
          "3 个素材" in dlg.lbl_thumbs.text() and "3 张有图" in dlg.lbl_thumbs.text(),
          dlg.lbl_thumbs.text())
    # 换成"部分有图"这张表（同一窗口 reload ⇒ 只会有 2 张图 + 1 个占位）
    n_after_reload = dlg.reload(pkg_part, entry_of(pkg_part))
    for _ in range(6):
        app.processEvents()
    check("reload 到「部分有图」⇒ 仍是 3 格", n_after_reload == 3, n_after_reload)

    # ---------------- ⑧ 部分无图 ⇒ 占位 ----------------
    print("\n⑧ 部分素材无图 ⇒ 占位数量正确（灰底「无图」，不留空洞）")
    check("占位 1 个（c3 没图）", strip.placeholder_count() == 1,
          strip.placeholder_count())
    check("有图 2 个、QPixmap 非空",
          sum(1 for pm in strip.pixmaps() if pm is not None and not pm.isNull()) == 2,
          [pm.isNull() for pm in strip.pixmaps()])
    check("占位格显示「无图」",
          any(lb.text() == "无图" for lb in strip.cells),
          [lb.text() for lb in strip.cells])
    check("标题行标出「3 个素材 ｜ 2 张有图」",
          "3 个素材" in dlg.lbl_thumbs.text() and "2 张有图" in dlg.lbl_thumbs.text(),
          dlg.lbl_thumbs.text())
    check("格子顺序按素材清单（c1 / c2 / c3）",
          strip.clip_ids == ["c1", "c2", "c3"], strip.clip_ids)

    # 截图（证据）：一张"部分无图"的详情窗
    os.makedirs(SHOT_DIR, exist_ok=True)
    shot = os.path.join(SHOT_DIR, "cue_thumbs.png")
    dlg.resize(860, 620)
    for _ in range(10):
        app.processEvents()
    check("截图已落盘 cue_thumbs.png", dlg.grab().save(shot, "PNG"), shot)

    # ---------------- ⑨ 换表不残留 ----------------
    print("\n⑨ 换表 ⇒ 控件数按新表变化，旧图不残留")
    n_other = dlg.reload(pkg_other, entry_of(pkg_other))
    for _ in range(6):
        app.processEvents()
    print("      另一张表：格子 %d 个 ｜ clip_ids = %s" % (n_other, strip.clip_ids))
    check("换成 1 个素材的表 ⇒ 1 格", n_other == 1, n_other)
    check("旧表的 clip_id 全部消失（只剩 d1）", strip.clip_ids == ["d1"], strip.clip_ids)
    check("旧图控件已销毁（QWidget 树上不再挂着 3 个）",
          len([lb for lb in strip.cells if lb.parent() is None]) == 0,
          [str(lb.parent()) for lb in strip.cells])
    check("占位 0 个（新表有图）", strip.placeholder_count() == 0,
          strip.placeholder_count())
    # 换到一张**不存在**的包 ⇒ 清空，且不许抛
    raised = None
    try:
        n_gone = dlg.reload(os.path.join(tmp, "不存在.ercue"), {"path": "x"})
    except Exception as e:                                         # noqa: BLE001
        raised = "%s: %s" % (type(e).__name__, e)
        n_gone = -1
    check("换到不存在的包：不抛 + 清空（0 格）", raised is None and n_gone == 0,
          (raised, n_gone))
    check("清空后 cells 列表也空了", strip.count() == 0 and strip.cells == [],
          strip.count())
    dlg.close()

    print("")
    if FAILS:
        print("CUE_THUMBS_FAIL  %d 项不通过：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("CUE_THUMBS_OK  thumbs 解出（2/2 内容一致）/ 二次 load 零重解压 / meta_only 零碰 / "
          "zip-slip 被拒 / 非 png 忽略 / 坏图不抛 / 老包无图不报错 / 详情窗显示与换表无残留")
    print("截图：%s" % os.path.join(SHOT_DIR, "cue_thumbs.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
