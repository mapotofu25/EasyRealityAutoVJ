# -*- coding: utf-8 -*-
"""后台 QThread 的“孤儿收尾”工具。

为什么需要：Qt 销毁一个仍在 running 的 QThread 会直接
`QThread: Destroyed while thread is still running`（进程 abort）。
窗口关闭时有些解码/分析线程可能要几百 ms 到几秒才能退出，不能为了等它
把界面卡死。

做法：`orphan_thread(t)` 把还在跑的线程从父对象上摘下来、放进模块级集合
保活，等它 `finished` 后自动移除。**绝不强杀**（QThread 没有安全的终止语义）。
"""
from __future__ import annotations

_ORPHANS = set()


def orphan_thread(thread) -> bool:
    """把仍在运行的 QThread 转为孤儿（保活到 finished）。返回是否接管。

    - 已结束 / None ⇒ False（调用方可以直接丢引用）。
    - 接管后调用方可以把字段置 None，不会触发“Destroyed while running”。
    """
    try:
        if thread is None:
            return False
        if not bool(thread.isRunning()):
            return False
        try:
            thread.setParent(None)
        except Exception:                                        # noqa: BLE001
            pass
        _ORPHANS.add(thread)
        try:
            thread.finished.connect(lambda t=thread: _ORPHANS.discard(t))
        except Exception:                                        # noqa: BLE001
            pass
        return True
    except Exception:                                            # noqa: BLE001
        return False
