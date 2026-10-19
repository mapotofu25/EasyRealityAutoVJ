# -*- coding: utf-8 -*-
"""QA(严过关) A5b：反事实取证 —— 现场签名（缓存播放器闲置 ~6.3s 后被激活）
在**旧判据**下会误杀、在**新判据**下不会。"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import media_manager as mm                                          # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def perf_counter(self):
        return self.t


CLK = Clock()
_orig = mm.time
mm.time = CLK
try:
    class P(mm._DecoderHealth):
        def __init__(self):
            self._active = False
            self._init_health()

    p = P()
    p._frames = 483
    p._last_t = 1000.0             # 最后一帧在 t=1000
    CLK.t = 1006.3                 # 闲置 6.3s（现场 frames=483, 31.2s / 6.4s 同签名）
    p._set_active(True)            # 刚被激活 → _active_since=1006.3
    last = p._last_t
    active_since = p._active_since
    old_base = last                                                     # 旧判据：只认 _last_t
    new_base = max(last, active_since)                                  # 新判据
    old_stalled = (CLK.t - old_base) > 1.5
    new_stalled = (CLK.t - new_base) > 1.5
    print("闲置 6.3s 后激活瞬间：")
    print("  旧判据 base=%.1f -> stalled=%s （误杀）" % (old_base, old_stalled))
    print("  新判据 base=%.1f -> stalled=%s （放过）" % (new_base, new_stalled))
    print("  真实 stalled()=%s" % p.stalled(1.5))
    ok = old_stalled and (not new_stalled) and (p.stalled(1.5) is False)
    print("结果：%s" % ("A5b 通过（修复精确命中现场签名）" if ok else "A5b 失败"))
    sys.exit(0 if ok else 1)
finally:
    mm.time = _orig
