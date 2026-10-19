# -*- coding: utf-8 -*-
"""**码表素材查找顺序**回归（用户 2026-10-18："多层编排还是没在 era 里看见"）。

现场铁证：HUD 显示 `🎬 码表 3层 · 1在播`，画面只有 BGA。
日志 `sub=3/1` —— 3 个子层都在，但**只有 1 个有素材路径**。

根因：`cuesheet.load(pkg, media_dir=…)` 里
    md = media_dir if media_dir is not None else embed_dir      # ← 替换，不是追加
而 `cfg["cue"]["media_dir"]` 被设成了**包所在目录**（`…/码表素材`）。
那一层里恰好躺着两个 **BGA 的散装副本**（c1/c2），
但 LOGO(c5)/PNG(c4) 只存在于 `<包>.media/` 里
⇒ `check_media` 只找到 2 个 ⇒ 另外两层的 `clip_path` 为空 ⇒ `missing=True` ⇒ **一帧都不画**。

判据：
  A. 生产调用 `load(pkg, media_dir=<包所在目录>)` 必须解析出**全部**素材；
  B. 每个命中路径都必须落在 `<包>.media/` 内（**包内优先**，作者那份才算数）；
  C. 真的缺文件时仍报 missing（不能因为多搜几个目录就把缺失掩盖掉）；
  D. 反向复现：只搜 media_dir 单目录时必然是缺的（证明这条测试钉得住）。

运行：`python tools/_cue_media_dir_test.py`
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cuesheet as CS                                          # noqa: E402

PKG = r"Y:/EasyRealityAutoVJ/码表素材/测试.ercue"
FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [ok  ] %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


def main():
    if not os.path.isfile(PKG):
        print("!! 找不到码表包：%s" % PKG)
        return 1
    pkg_dir = os.path.dirname(os.path.abspath(PKG))
    print("包 =", os.path.basename(PKG))
    print("media_dir（现场配置值）= ", pkg_dir)

    sheet, info = CS.load(PKG, media_dir=pkg_dir)
    pm = info.get("present_media") or {}
    miss = info.get("missing_media") or []

    print("A. 生产调用必须解析出全部素材")
    ck("解析数 == 包内 media 数（%d）" % len(sheet.media), len(pm) == len(sheet.media),
       "实际 %d 个" % len(pm))
    ck("missing 为空", not miss, "缺失：%s" % (miss,))

    print("B. 包内有同名文件时**必须**用包内那份（包内优先）")
    embed = PKG + ".media"
    # ⚠ 不能要求"全部都落在包内"：包里**没带**的素材（后来才加进工程的）本来就该回落到
    #   `media_dir`/包所在目录 —— 那是正确行为，不是 bug。真正的判据是：
    #   凡 `<包>.media/` 里**存在同名文件**的，命中路径必须指向包内那份。
    bad = []
    for cid, path in pm.items():
        name = os.path.basename(str(path))
        in_embed = os.path.isfile(os.path.join(embed, name))
        if in_embed and not os.path.abspath(path).startswith(os.path.abspath(embed)):
            bad.append((cid, name))
    ck("包内有的都优先用包内那份", not bad, "跑到包外的：%s" % (bad,))
    _n_in = sum(1 for p2 in pm.values()
                if os.path.isfile(os.path.join(embed, os.path.basename(str(p2)))))
    print("       包内命中 %d / %d（其余回落到 media_dir，正常）" % (_n_in, len(pm)))

    print("C. 真缺的仍要报 missing")
    import copy
    s2 = copy.deepcopy(sheet)
    s2.media.append(CS.MediaRef(clip_id="cz", name="绝对不存在_zzz.mp4"))
    miss3, pres3 = CS.check_media(s2, [embed, pkg_dir])
    ck("不存在的文件仍判 missing", "绝对不存在_zzz.mp4" in miss3, str(miss3))
    ck("存在的仍然命中（不被缺失带跑）", len(pres3) == len(pm), "实际 %d" % len(pres3))

    print("D. 反向复现：只搜 media_dir 单目录 ⇒ 必然缺")
    miss_old, pres_old = CS.check_media(sheet, pkg_dir)
    print("       旧口径：present=%d missing=%d" % (len(pres_old), len(miss_old)))
    ck("旧口径下确实缺（这正是现场 sub=3/1 的来源）", len(pres_old) < len(sheet.media),
       "present=%d/%d" % (len(pres_old), len(sheet.media)))

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), "、".join(FAIL)))
        return 1
    print("结论：PASS —— 包内素材优先于 media_dir，多层的每一层都能拿到自己的素材。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
