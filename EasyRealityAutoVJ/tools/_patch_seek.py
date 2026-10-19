# -*- coding: utf-8 -*-
"""一次性补丁：把「远跳一律忽略」换成「候选一致性判定真拖动」。

用法：venv\\Scripts\\python.exe tools/_patch_seek.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "src", "cue_position.py")

NEW_BLOCK = '''            # ══════════════════════════════════════════════════════════════════
            # ★★★ 2026-10-18 第七稿（用户：我手动突然跳到三分钟，视频半天都不跳）
            #   旧写法用「位移 >60 秒一律忽略」来挡野匹配 —— 结果把**真实拖动**也挡死了。
            #   真正的判别不是「跳多远」，而是「这个新位置能不能一直站得住」：
            #     · 野匹配：下一次结果又跳到别处（候选一直在变）⇒ 永远不采信
            #     · 真拖动：之后每次结果都指向同一处 ⇒ 2.5 秒内确认，然后**跳过去**
            #   ⇒ 用「远候选一致性」代替距离硬上限：距离多远都能跟，野匹配照样挡掉。
            # ══════════════════════════════════════════════════════════════════
            _now = float(t_wall)
            _e = abs(_err)
            if _e <= CUE_SERVO_DEAD:
                self._vel = _r                            # 噪声区：一点不抖
            elif _e <= CUE_SERVO_SERIOUS:
                # 中等误差：按比例平滑收敛，钳 ±12%（1 秒误差约 8 秒收干净）
                _step = _err - (CUE_SERVO_DEAD if _err > 0 else -CUE_SERVO_DEAD)
                _v = _r * (1.0 + CUE_SERVO_KP * _step)
                self._vel = max(_r * (1.0 - CUE_SERVO_MID_HI),
                                min(_r * (1.0 + CUE_SERVO_MID_HI), _v))
            else:
                # 远：先看这个「新位置」是不是一直站得住
                _prev = float(getattr(self, "_far_cand", 0.0) or 0.0)
                _pt = float(getattr(self, "_far_t", 0.0) or 0.0)
                _pred_c = (_prev + (_now - _pt) * _r) if _pt > 0.0 else None
                if _pred_c is not None and abs(_new - _pred_c) <= CUE_SEEK_AGREE:
                    _persist = _now - _pt
                else:
                    self._far_cand = _new
                    self._far_t = _now
                    _persist = 0.0
                if _persist >= CUE_SEEK_DEBOUNCE:
                    # 站住了 ⇒ 这就是真位置（拖动/跳转）⇒ **跳过去**，速度归位
                    self._vel = _r
                    self._pos_sec = max(0.0, _new)
                    self._last_off = _new
                    self._t_last = _now
                    self._far_t = 0.0
                    self._bad_t = 0.0
                    self.last_status = "ok"
                    return self.last_status, self.pos_beats_at(t_wall), self.song_id
                # 还没站住：只做 ±5% 微调（看不出来），继续观察
                self._vel = _r * (1.0 + (CUE_SERVO_LIGHT if _err > 0 else -CUE_SERVO_LIGHT))
'''

CONSTS = '''CUE_SERVO_MID_HI = 0.12
# ★ 真实拖动（seek）的判定：新位置**持续一致**这么久就认（与跳多远无关）
CUE_SEEK_DEBOUNCE = 2.5      # 秒
CUE_SEEK_AGREE = 2.0         # 两次候选相差 <= 它 视为同一处
'''

FIELDS = '''        self._bad_t = 0.0           # 连续严重不符的起始时刻（防抖用）
        self._far_cand = 0.0        # 远候选位置（真拖动判定用）
        self._far_t = 0.0           # 远候选首次出现时刻
'''


def main():
    s = io.open(P, encoding="utf-8").read()

    # 1) 删掉重复的常量块（第二份）
    first = s.find("JUMP_ABS_MAX_SEC = 60.0")
    second = s.find("JUMP_ABS_MAX_SEC = 60.0", first + 1)
    if second > 0:
        a = s.rfind("# ★★ 2026-10-18", 0, second)
        b = s.find("CUE_SERVO_MID_HI = 0.12", second)
        b = s.find("\n", b) + 1
        s = s[:a] + s[b:]
        print("去重常量块 ok")

    # 2) 替换决策段
    a = s.find("            if abs(_err) > JUMP_ABS_MAX_SEC:")
    b = s.find("            self._vel = _v\n", a)
    if a < 0 or b < a:
        print("!! 找不到决策段")
        return 1
    s = s[:a] + NEW_BLOCK + s[b:]

    # 3) 常量
    if "CUE_SEEK_DEBOUNCE" not in s:
        s = s.replace("CUE_SERVO_MID_HI = 0.12", CONSTS, 1)

    # 4) 字段
    if "_far_cand" not in s:
        s = s.replace('        self._bad_t = 0.0           # 连续"严重不符"的起始时刻（防抖用）\n',
                      FIELDS, 1)

    io.open(P, "w", encoding="utf-8").write(s)
    print("patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
