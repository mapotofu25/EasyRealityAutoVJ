# -*- coding: utf-8 -*-
"""审计后的 3 处零风险修正（audio_engine 取证日志 ×2、engine 注释 ×1）。"""

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AE = os.path.join(ROOT, "src", "audio_engine.py")
EN = os.path.join(ROOT, "src", "engine.py")


def patch_audio():
    s = io.open(AE, encoding="utf-8").read()

    # ① hit 分母用「实际查询的条数」，不是全量 n_hash（否则完美命中也会显示 400/1700）
    old1 = """                    if n_hash and fp_db is not None:
                        _uni = list({h for h, _o in _hs})[:400]"""
    new1 = """                    if n_hash and fp_db is not None:
                        _uni = list({h for h, _o in _hs})[:400]
                        _n_uni = len(_uni)"""
    assert old1 in s, "① 锚点没找到"
    s = s.replace(old1, new1, 1)

    old2 = """            except Exception:                                    # noqa: BLE001
                hit, n_hash = -1, -1
            line = ("[%s] [R] db=%d"""
    new2 = """            except Exception:                                    # noqa: BLE001
                hit, n_hash, _n_uni = -1, -1, -1
            line = ("[%s] [R] db=%d"""
    assert old2 in s, "② 锚点没找到"
    s = s.replace(old2, new2, 1)

    # 初始化 _n_uni（异常前未赋值时也要能用）
    old3 = """                    _hs = _fa(_seg, self._fp_sr)
                    n_hash = len(_hs)"""
    new3 = """                    _hs = _fa(_seg, self._fp_sr)
                    n_hash = len(_hs)
                    _n_uni = 0"""
    assert old3 in s, "③ 锚点没找到"
    s = s.replace(old3, new3, 1)

    # ② 行尾追加 sid / off（数据就在 _last_match 里，之前没打）
    old4 = '''"hit=%d/%d seg=%.3f/%d sig=%d/%.3f raw=%d/hint=%d adv=%d cur=%d cand=%d bar=%d hits=%d"'''
    new4 = '''"hit=%d/%d seg=%.3f/%d sig=%d/%.3f raw=%d/hint=%d adv=%d cur=%d cand=%d bar=%d hits=%d"
                    " sid=%d off=%.2f"'''
    assert old4 in s, "④ 锚点没找到"
    s = s.replace(old4, new4, 1)

    old5 = """                       *[int(x) for x in (getattr(self, "_eng_state", (0, -1, 0, 0)) or (0, -1, 0, 0))]))"""
    new5 = """                       *[int(x) for x in (getattr(self, "_eng_state", (0, -1, 0, 0)) or (0, -1, 0, 0))],
                       int((getattr(self, "_last_match", (0, 0, 0.0, 0)) or (0, 0, 0.0, 0))[0]),
                       float((getattr(self, "_last_match", (0, 0, 0.0, 0)) or (0, 0, 0.0, 0))[2]))"""
    assert old5 in s, "⑤ 锚点没找到"
    s = s.replace(old5, new5, 1)

    # ③ hit 分母改用 _n_uni
    old6 = """                       hit, n_hash, seg_rms, seg_n,"""
    new6 = """                       hit, _n_uni, seg_rms, seg_n,"""
    assert old6 in s, "⑥ 锚点没找到"
    s = s.replace(old6, new6, 1)

    io.open(AE, "w", encoding="utf-8").write(s)
    print("audio_engine.py 已改 6 处")


def patch_engine():
    s = io.open(EN, encoding="utf-8").read()
    old = "# 倍率死区（见 `_cue_auto_position` 里的说明）：±5% 内按原速处理"
    new = "# 倍率死区（见 `_cue_auto_position` 里的说明）：±10% 内按原速处理（CUE_RATE_DEADBAND=0.10）"
    if old in s:
        s = s.replace(old, new, 1)
        print("engine.py 注释已改")
    else:
        print("engine.py 注释锚点没找到（可能已被改过）")
    io.open(EN, "w", encoding="utf-8").write(s)


if __name__ == "__main__":
    patch_audio()
    patch_engine()
    sys.exit(0)
