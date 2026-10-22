# -*- coding: utf-8 -*-
r"""判定「用户正在跑的 exe」是不是最新包 —— 逐个模块解 code 对象查函数名。

## 为什么必须这么做（2026-10-08 现场）
用户报「窗口一直在闪 + 渲染非常卡」，我查到自己桌面程序跑的是
`Y:\EasyRealityAutoVJ\EasyRealityAutoVJ.exe`（08:51 构建），**不是**当轮打的包。
只看 exe 的 mtime / 大小不足以说明"里面有没有这轮修复"——
必须解出 PYZ → 取模块 code → 查**本轮新增的函数名**。

## 用法
    venv\Scripts\python.exe tools\_check_which_pkg.py <exe路径> [<exe路径> ...]
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

try:
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
except Exception as e:                                            # noqa: BLE001
    print("需要 PyInstaller（在 venv 里跑）：%s" % e)
    sys.exit(2)

# 每个模块要查的"本轮新标记"（有 = 新包）
PROBE = {
    "lan_audio": [
        ("_no_window_kwargs", "★闪窗修复：arp 不弹控制台"),
        ("CREATE_NO_WINDOW", "★闪窗修复：常量"),
        ("announce_targets", "★P0修复：逐网卡目标（不再建抢包 socket）"),
        ("_isolated_nics", "★P0修复：跳过孤立网卡（复用一次 arp）"),
        ("is_unreachable_src", "拒不可达源地址"),
        ("trivial_nics", "跳过孤立网卡"),
        ("broadcast_targets", "旧的广播目标（应一直在）"),
    ],
    "lan_audio_recv": [
        ("_ann_worker", "宣告线程（旧名）"),
        ("_announce_worker", "宣告线程"),
        ("_ann_targets_ok", "★P0修复：逐网卡目标成功标志"),
        ("_unreach_rejects", "不可达源过滤计数"),
    ],
    "lan_audio_send": [
        ("_PEER_STALE_SEC", "peer 心跳超时"),
        ("peer_timeouts", "peer 超时计数"),
        ("bad_src", "坏源地址计数"),
    ],
    "engine": [
        ("modes_snapshot", "★卡顿修复：无锁只读快照"),
        ("_ndi_send", "NDI 投帧入口"),
    ],
    "audio_engine": [
        ("_empty_streak", "★空转修复：采集空档退避"),
        ("_run_lan", "局域网采集路径"),
    ],
}


def names_of(code):
    out = set()
    out |= set(code.co_names)
    for c in code.co_consts:
        if hasattr(c, "co_code"):
            sub = names_of(c)
            out |= sub
    return out


def probe_one(exe):
    print("=" * 78)
    try:
        sz = os.path.getsize(exe)
        mt = os.path.getmtime(exe)
        import datetime
        mts = datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:                                        # noqa: BLE001
        print("拿不到文件信息：%s（%s）" % (exe, e))
        sz, mts = 0, "?"
    print("EXE = %s" % exe)
    print("      大小 %.2f MB   修改时间 %s" % (sz / 1e6, mts))
    print("=" * 78)
    d = tempfile.mkdtemp(prefix="avj_chk_")
    try:
        try:
            r = CArchiveReader(exe)
            data = r.extract("PYZ.pyz")
        except Exception as e:                                    # noqa: BLE001
            print("  ★ 这个 exe 里没有内嵌 PYZ.pyz（可能不是 PyInstaller 6.x 包）：%s" % e)
            return
        pyz = os.path.join(d, "PYZ.pyz")
        with open(pyz, "wb") as f:
            f.write(data)
        z = ZlibArchiveReader(pyz)
        available = set()
        try:
            available = set(z.toc.keys())
        except Exception:                                         # noqa: BLE001
            try:
                available = set(n for n, _ in z.toc)
            except Exception:                                     # noqa: BLE001
                pass
        for mod, probes in PROBE.items():
            print("")
            if available and mod not in available:
                print("  [%s] ★模块不在包里" % mod)
                continue
            try:
                code = z.extract(mod)
            except Exception as e:                                # noqa: BLE001
                print("  [%s] 解不出：%s" % (mod, e))
                continue
            ns = names_of(code)
            print("  [%s]" % mod)
            for nm, desc in probes:
                mark = "✓ 在  " if nm in ns else "✗ 缺  "
                print("     %s%-22s %s" % (mark, nm, desc))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for exe in sys.argv[1:]:
        try:
            probe_one(exe)
        except Exception as e:                                    # noqa: BLE001
            print("扫描失败：%s -> %s" % (exe, e))
        print("")


if __name__ == "__main__":
    main()
