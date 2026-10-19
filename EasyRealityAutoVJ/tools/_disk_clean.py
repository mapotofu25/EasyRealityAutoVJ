# -*- coding: utf-8 -*-
"""清理"确定是我/本项目在 C 盘临时目录里造的垃圾"。

只删**有明确前缀的测试/临时产物**，且逐个先列后删、每批不超过 40 个（沙箱批量删除有额度）。
⚠ 不碰：用户文档、下载、桌面、Program Files、其它软件的缓存。
"""
import os
import shutil
import sys
import tempfile

TMP = tempfile.gettempdir()

# ① 我自己脚本产生的（100% 是测试夹具，不含用户数据）
MINE = ("autovj_", "ercue", "hlreal", "hl_rr", "demo_src", "demochk", "easyreality")
# ② pip 解包残留（pip 自己的临时目录，删掉不影响已装好的环境；列出来交给用户决定）
PIP = ("pip-unpack-", "pip-")

BATCH = 40


def du(path):
    t = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                t += os.path.getsize(os.path.join(dp, f))
            except Exception:                                       # noqa: BLE001
                pass
    return t


def main():
    try:
        entries = sorted(os.listdir(TMP))
    except Exception as e:                                          # noqa: BLE001
        print("读不到 TEMP：", e)
        return 1

    mine, pip = [], []
    for e in entries:
        low = e.lower()
        p = os.path.join(TMP, e)
        if any(low.startswith(x) for x in MINE):
            mine.append((p, e))
        elif any(low.startswith(x) for x in PIP):
            pip.append((p, e))

    print("=" * 72)
    # ★ 按体积从大到小删：每回合额度有限，先把大块头清掉，收益最大
    mine.sort(key=lambda t: -du(t[0]))
    print("① 我的测试夹具残留：%d 个（按体积降序，下面全部删除）" % len(mine))
    total = 0
    for p, e in mine:
        s = du(p)
        total += s
        print("   %-44s %8.1f MB" % (e[:44], s / 1048576))
    print("   小计 %.1f MB" % (total / 1048576))

    done = 0
    for p, e in mine:
        if done >= BATCH:
            print("   （本回合额度 %d 个已用完，剩下的下回合继续）" % BATCH)
            break
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            done += 1
        except Exception as ex:                                     # noqa: BLE001
            print("   !! 删不掉", e, ex)
    left = sum(1 for p, _e in mine if os.path.exists(p))
    print("   已删 %d 个，剩 %d 个" % (done, left))

    print("\n" + "=" * 72)
    ps = sum(du(p) for p, _e in pip)
    print("② pip 解包残留：%d 个 / %.1f MB（**没删** —— 这是 pip 自己的临时目录，"
          "删了只影响以后的下载速度）" % (len(pip), ps / 1048576))
    cache = os.path.join(os.environ.get("LOCALAPPDATA", ""), "pip", "cache")
    if os.path.isdir(cache):
        print("   pip 轮子缓存 %s  %.1f MB（**没删**，同上）"
              % (cache, du(cache) / 1048576))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
