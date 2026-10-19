# -*- coding: utf-8 -*-
"""构建版本号 = 日期 + 当日序号（单一来源：exe 内 / 压缩包名 / 说明书头 共用）。

规则（沿用参考项目 AutoVJ 的约定）：
    每生成一次新文件，当日序号 +1；当天第 1 次生成 → 01。
    版本号  2026.10.01.01
    压缩包  EasyRealityCue_2026.10.01.01.zip

实现：
    · 计数器落盘在项目根 build_seq.json（{"2026-10-01": 1}），跨天自动从 01 重新开始。
    · 打包前由 build_exe.py 调 `bump()` 递增，并把结果写成 src/ercue/_build_ver.py
      （含 BUILD_VER / BUILD_DATE / BUILD_SEQ）；该文件在 .gitignore 里，不提交。
    · make_zip.py 只调 `read_build_info()` 读同一个号，保证「包名 == 本次构建版本」。

命令行：
    python tools/build_version.py show     # 只看当前（不递增）
    python tools/build_version.py next     # 递增一次并写 _build_ver.py（打包脚本自己会调）
    python tools/build_version.py set 5    # 手动把当日序号设为 5（改错时用）
    python tools/build_version.py stamp    # 把当前版本号写进 使用说明.txt 头部
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(SRC, "ercue")
SEQ_FILE = os.path.join(ROOT, "build_seq.json")
BUILD_INFO = os.path.join(PKG, "_build_ver.py")

DEV_VER = "dev"          # 源码直接运行（没打包过）时的版本号


# ---------------------------------------------------------------- 基础
def today_key():
    return time.strftime("%Y-%m-%d")


def _load():
    try:
        with open(SEQ_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d):
    try:
        with open(SEQ_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:                                    # noqa: BLE001
        print("!! 计数器写入失败:", e)


def version_str(date_key, seq):
    y, m, d = date_key.split("-")
    return "%s.%s.%s.%02d" % (y, m, d, seq)


def zip_name(date_key, seq):
    """压缩包名：EasyRealityCue_<版本>.zip"""
    return "EasyRealityCue_%s.zip" % version_str(date_key, seq)


def version_from_zip_name(name):
    """从压缩包名反解版本号（校验用）。非法返回 ""。"""
    base = os.path.basename(name)
    if base.startswith("EasyRealityCue_") and base.endswith(".zip"):
        return base[len("EasyRealityCue_"):-len(".zip")]
    return ""


# ---------------------------------------------------------------- 读写
def read_build_info():
    """读「当前构建的版本」= src/ercue/_build_ver.py（没有则退回计数器 / 开发版）。"""
    if os.path.exists(BUILD_INFO):
        ns = {}
        try:
            with open(BUILD_INFO, encoding="utf-8") as f:
                exec(compile(f.read(), BUILD_INFO, "exec"), ns)
            date_key = ns.get("BUILD_DATE") or today_key()
            seq = int(ns.get("BUILD_SEQ") or 0)
            if seq > 0:
                return date_key, seq, ns.get("BUILD_VER") or version_str(date_key, seq)
        except Exception:                                    # noqa: BLE001
            pass
    d = _load()
    date_key = today_key()
    seq = int(d.get(date_key) or 0)
    if seq > 0:
        return date_key, seq, version_str(date_key, seq)
    return date_key, 0, DEV_VER


def bump():
    """当日序号 +1，写 src/ercue/_build_ver.py，返回 (date_key, seq, ver)。"""
    d = _load()
    date_key = today_key()
    seq = int(d.get(date_key) or 0) + 1
    d = {date_key: seq}          # 只保留当天，避免文件无限膨胀
    _save(d)
    write_build_info(date_key, seq)
    return date_key, seq, version_str(date_key, seq)


def write_build_info(date_key, seq):
    ver = version_str(date_key, seq)
    os.makedirs(PKG, exist_ok=True)
    with open(BUILD_INFO, "w", encoding="utf-8") as f:
        f.write(
            "# -*- coding: utf-8 -*-\n"
            "# 由 tools/build_version.py 自动生成，请勿手改（每次打包会覆盖）。\n"
            "# 版本号规则：年份.月.日.当日第几次生成（两位数）。\n"
            "# 本文件已加入 .gitignore，不进公开仓库。\n"
            'BUILD_DATE = "%s"\n'
            "BUILD_SEQ = %d\n"
            'BUILD_VER = "%s"\n'
            'ZIP_NAME = "%s"\n' % (date_key, seq, ver, zip_name(date_key, seq))
        )
    stamp_docs(ver)
    return ver


# ---------------------------------------------------------------- 说明书版本号
# 说明书随压缩包一起发，头部版本号必须和 exe 内一致；由打包流程在递增版本号时同步改写。
# 只认前 8 行里的「版本 …」行，正文一律不动。
DOC_HEAD = os.path.join(ROOT, "使用说明.txt")


def stamp_docs(ver):
    import re
    if not os.path.exists(DOC_HEAD):
        return
    pat = re.compile(r"^版本\s+[0-9]")
    try:
        with open(DOC_HEAD, "r", encoding="utf-8", newline="") as f:
            lines = f.read().splitlines(keepends=True)
    except Exception as e:                                    # noqa: BLE001
        print("!! 说明书读取失败 %s: %s" % (os.path.basename(DOC_HEAD), e))
        return
    for i in range(min(8, len(lines))):
        body = lines[i].rstrip("\r\n")
        if pat.match(body.strip()):
            lines[i] = ("版本 %s" % ver) + lines[i][len(body):]
            try:
                with open(DOC_HEAD, "w", encoding="utf-8", newline="") as f:
                    f.write("".join(lines))
                print("   说明书版本号 -> %s" % ver)
            except Exception as e:                            # noqa: BLE001
                print("!! 说明书写入失败: %s" % e)
            return


def set_seq(n):
    date_key = today_key()
    _save({date_key: int(n)})
    write_build_info(date_key, int(n))
    return date_key, int(n), version_str(date_key, int(n))


if __name__ == "__main__":
    import sys
    arg = (sys.argv[1] if len(sys.argv) > 1 else "show").lower()
    if arg == "next":
        k, s, v = bump()
        print("版本 %s（%s 第 %d 次生成）" % (v, k, s))
    elif arg == "set" and len(sys.argv) > 2:
        k, s, v = set_seq(int(sys.argv[2]))
        print("已设为 %s（%s 第 %d 次）" % (v, k, s))
    elif arg == "stamp":
        k, s, v = read_build_info()
        print("把当前版本 %s 写进说明书…" % v)
        stamp_docs(v)
    else:
        k, s, v = read_build_info()
        print("当前版本 %s（%s%s）" % (v, k, "" if s else " 未打包过"))
        print("压缩包名 %s" % (zip_name(k, s) if s else "——"))
