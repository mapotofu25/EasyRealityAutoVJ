# -*- coding: utf-8 -*-
"""构建版本号 = 日期 + 当日序号（单一来源，exe 内显示与压缩包命名共用）。

规则（用户 2026-09-23 定）：
    每生成一次新文件，当日序号 +1；当天第 5 次生成 → 05。
    版本号  2026.09.23.05
    压缩包  EasyRealityAutoVJ_测试版092305.zip

实现：
    - 计数器落盘在项目根的 build_seq.json（{"2026-09-23": 5}），跨天自动从 01 重新开始。
    - 打包前由 build_exe.py 调 `bump()` 递增，并把结果写成 src/_build_ver.py
      （version.py 读它 → 打包进 exe 的就是这个版本号）。
    - 压缩包脚本只调 `read_build_info()` 读同一个号，保证「包」与「exe 内显示」永远一致。

命令行：
    python tools/build_version.py show     # 只看当前（不递增）
    python tools/build_version.py next     # 递增一次并写 _build_ver.py（打包脚本自己会调）
    python tools/build_version.py set 5    # 手动把当日序号设为 5（改错时用）
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SEQ_FILE = os.path.join(ROOT, "build_seq.json")
BUILD_INFO = os.path.join(SRC, "_build_ver.py")

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
    except Exception as e:
        print("!! 计数器写入失败:", e)


def version_str(date_key, seq):
    y, m, d = date_key.split("-")
    return "%s.%s.%s.%02d" % (y, m, d, seq)


def zip_name(date_key, seq):
    y, m, d = date_key.split("-")
    return "EasyRealityAutoVJ_测试版%s%s%02d.zip" % (m, d, seq)


# ---------------------------------------------------------------- 读写
def read_build_info():
    """读「当前 exe 的版本」= src/_build_ver.py（没有则退回计数器/开发版）。"""
    if os.path.exists(BUILD_INFO):
        ns = {}
        try:
            with open(BUILD_INFO, encoding="utf-8") as f:
                exec(compile(f.read(), BUILD_INFO, "exec"), ns)
            date_key = ns.get("BUILD_DATE") or today_key()
            seq = int(ns.get("BUILD_SEQ") or 0)
            if seq > 0:
                return date_key, seq, ns.get("BUILD_VER") or version_str(date_key, seq)
        except Exception:
            pass
    d = _load()
    date_key = today_key()
    seq = int(d.get(date_key) or 0)
    if seq > 0:
        return date_key, seq, version_str(date_key, seq)
    return date_key, 0, DEV_VER


def bump():
    """当日序号 +1，写 src/_build_ver.py，返回 (date_key, seq, ver)。"""
    d = _load()
    date_key = today_key()
    seq = int(d.get(date_key) or 0) + 1
    d = {date_key: seq}          # 只保留当天，避免文件无限膨胀
    _save(d)
    write_build_info(date_key, seq)
    return date_key, seq, version_str(date_key, seq)


def write_build_info(date_key, seq):
    ver = version_str(date_key, seq)
    with open(BUILD_INFO, "w", encoding="utf-8") as f:
        f.write(
            "# -*- coding: utf-8 -*-\n"
            "# 由 tools/build_version.py 自动生成，请勿手改（每次打包会覆盖）。\n"
            "# 版本号规则：年份.月.日.当日第几次生成（两位数）。\n"
            'BUILD_DATE = "%s"\n'
            "BUILD_SEQ = %d\n"
            'BUILD_VER = "%s"\n'
            'ZIP_NAME = "%s"\n' % (date_key, seq, ver, zip_name(date_key, seq))
        )
    return ver


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
    else:
        k, s, v = read_build_info()
        print("当前版本 %s（%s%s）" % (v, k, "" if s else " 未打包过"))
        print("压缩包名 EasyRealityAutoVJ_测试版%s" % (zip_name(k, s)[len("EasyRealityAutoVJ_测试版"):] if s else "——"))
