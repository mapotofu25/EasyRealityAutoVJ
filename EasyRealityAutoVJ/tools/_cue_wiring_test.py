# -*- coding: utf-8 -*-
"""**接线校验**：引擎拿到的 `self.audio` 必须是 `AudioState` 本体，不是 `AudioEngine`。

背景（2026-10-17，本项目代价最高的一次"假绿"）：
  `ui_main.py` 里 `AutoVJEngine(self.cfg, self.audio.state, self.library)` —— 第二个参数是
  **状态对象**。而 `engine.py` 里有多处按"`self.audio` 是音频引擎"写成
  `getattr(getattr(self, "audio", None), "state", None)` ⇒ **恒 None**
  ⇒ `_cue_auto_position()` 每个字段都取默认值（`sid=-1 votes=0 off=-1.0 silent=-1.0`）
  ⇒ 状态永远 `lost` ⇒ **码表图层从导入那一刻起就没演出过**。
  现场铁证：同一秒里 `[R] cur=346225955`（认歌线程已确认）而 `_cue_diag` 写 `sid=-1`；
  且 `silent=-1.0` 是 `getattr` 的**默认值**，不是"没声音"（真没声音时 `[R]` 会是 silent>0）。

  为什么全量测试没抓到：`_e2e_recog_bind.py` 里写的是 `eng.audio = ae`（**完整 AudioEngine**，
  它自己有 `.state`）—— 夹具与真实接线不一致 ⇒ 测试全绿、现场全黑。
  **新的铁律：凡是"某字段恒为默认值"，先验证「取数对象本身存不存在」，再怀疑逻辑。**

本测试做三件事（A/B 是动态、C 是静态）：
  A. **真构造**：用**生产的签名** `AutoVJEngine(cfg, AudioState(), lib)` 造引擎，
     断言 `engine.audio` 就是那个 state 对象、且它**没有** `.state` 属性。
  B. **真调用**：把 sid/votes/off 写进 state，跑真 `_cue_auto_position()`，
     用替身 `_cue_diag` 截获实参 —— 断言它**读到了**这些值。
     （把两处改回 `.state` 写法，这里必然 `sid=-1` ⇒ 测试会红，可反向复现。）
  C. **静态**：`engine.py` 不得再出现对 `self.audio` 取 `.state` 的写法；
     `ui_main.py` 必须仍是 `AutoVJEngine(cfg, self.audio.state, lib)` 且调用 `set_audio_engine`。
"""
from __future__ import annotations
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

FAIL = []


def ck(name, cond, detail=""):
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s  %s" % (name, detail))
        FAIL.append(name)


class FakeCfg(dict):
    """最小可用的 Config 替身：只实现引擎 `__init__` 会碰到的接口。"""

    def save(self):
        pass


def make_engine():
    import engine as E
    import audio_engine as AE

    cfg = FakeCfg({
        "cue": {},
        "output": {},
        "ui": {},
        "music_meta": {},
        "layers": [],
        "global": {},
    })
    st = AE.AudioState()
    st.running = True
    eng = E.AutoVJEngine(cfg, st, [])
    return eng, st, E, AE


def main():
    # ---------------- A. 真构造 ----------------
    print("A. 生产签名构造（AutoVJEngine(cfg, AudioState(), lib)）")
    try:
        eng, st, E, AE = make_engine()
        ok = True
    except Exception as e:                                       # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("  [FAIL] 构造失败：%s" % e)
        FAIL.append("A.构造")
        eng = st = None
        ok = False
    if ok:
        ck("engine.audio 就是传入的 AudioState",
           getattr(eng, "audio", None) is st, repr(type(getattr(eng, "audio", None))))
        ck("AudioState **没有** .state 属性（旧写法必然取到 None）",
           not hasattr(st, "state"))
        ck("engine._audio_engine 已初始化（未注入时为 None）",
           hasattr(eng, "_audio_engine") and eng._audio_engine is None)

    # ---------------- B. 真调用 ----------------
    print("B. 真调用 _cue_auto_position（喂 sid/votes/off 进 state，截获 _cue_diag 实参）")
    if ok:
        cap = {}
        orig = eng._cue_diag

        def spy(sid, votes, off, status, pb, now):
            cap["sid"] = sid
            cap["votes"] = votes
            cap["off"] = off
            cap["status"] = status
            return orig(sid, votes, off, status, pb, now)

        eng._cue_diag = spy
        st.recognized_song_id = 424242
        st.recognized_votes = 30
        st.recognized_offset = 100.0
        eng._cue_pos_next_t = 0.0
        try:
            eng._cue_auto_position()
            err = None
        except Exception as e:                                   # noqa: BLE001
            import traceback
            traceback.print_exc()
            err = e
        ck("_cue_auto_position 未抛异常", err is None, repr(err))
        ck("它读到的 sid == 424242（旧写法这里必为 -1）",
           cap.get("sid") == 424242, "实际 sid=%r" % (cap.get("sid"),))
        ck("它读到的 votes == 30", cap.get("votes") == 30, "实际 votes=%r" % (cap.get("votes"),))
        ck("它读到的 off == 100.0", cap.get("off") == 100.0, "实际 off=%r" % (cap.get("off"),))

        # B2. sid 被逐拍覆写成 -1 时，必须能从音频引擎的 `current_song_id` 兜底读回
        class _FakeCharge:
            current_song_id = 777001
            tentative_song_id = -1
            cand_bar = 0
            cand_hits = 0
            rms = 0.0

        class _FakeAE:
            state = st
            _recognizer_engine = _FakeCharge()

        eng._audio_engine = _FakeAE()
        cap.clear()
        st.recognized_song_id = -1
        st.recognized_votes = 0
        st.recognized_offset = -1.0
        eng._cue_pos_next_t = 0.0
        try:
            eng._cue_auto_position()
        except Exception as e:                                   # noqa: BLE001
            print("  [FAIL] 兜底路径抛异常：%r" % (e,))
            FAIL.append("B2.兜底")
        ck("state 为 -1 时兜底读到 current_song_id=777001",
           cap.get("sid") == 777001, "实际 sid=%r" % (cap.get("sid"),))
        ck("兜底后 votes/off 仍来自 state（不伪造）",
           cap.get("votes") == 0 and cap.get("off") == -1.0,
           "votes=%r off=%r" % (cap.get("votes"), cap.get("off")))

    # ---------------- D. HUD 歌名映射 ----------------
    print("D. _merge_cue_song_meta：码表包曲目（合成 id）能进 HUD 查表")
    if ok:
        import types
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])
        import ui_main as UM
        fk = types.SimpleNamespace()
        fk.cfg = FakeCfg({"cue": {"fp_songs": {
            "fp2-aaaa": {"song_id": 111, "title": "揽月", "artist": "Dreamira",
                         "duration": 200.0, "sheet": "x.ercue"},
            "fp2-bbbb": {"song_id": 222, "title": "另一首", "artist": "",
                         "duration": 180.0, "sheet": "x.ercue"},
        }}})
        fk._song_meta_by_id = {}
        n = UM.MainWindow._merge_cue_song_meta(fk)
        ck("合并了 2 首", n == 2, "实际 %r" % (n,))
        ck("111 → 揽月", (fk._song_meta_by_id.get(111) or {}).get("title") == "揽月")
        ck("带 cue 标记（HUD 据此显示艺人而不是曲风）",
           bool((fk._song_meta_by_id.get(111) or {}).get("cue")))
        # 幂等 + 本机优先
        fk._song_meta_by_id[111]["title"] = "本机曲库版本"
        n2 = UM.MainWindow._merge_cue_song_meta(fk)
        ck("重复调用幂等（第二次 0 新增）", n2 == 0, "实际 %r" % (n2,))
        ck("本机曲库同名 id **不覆盖**",
           fk._song_meta_by_id[111]["title"] == "本机曲库版本")

    # ---------------- C. 静态 ----------------
    print("C. 静态扫描（防回归）")
    eng_src = io.open(os.path.join(SRC, "engine.py"), encoding="utf-8").read()
    # ⚠ 只看**代码行**：本文件的注释里保留了错误写法当反面教材（会被裸正则误判）
    code_lines = [ln.split("#", 1)[0] for ln in eng_src.splitlines()
                  if not ln.lstrip().startswith("#")]
    bad = re.findall(r'getattr\(\s*getattr\(self,\s*"audio"[^)]*\)\s*,\s*"state"',
                     "\n".join(code_lines))
    ck("engine.py 无 `getattr(self.audio, 'state')` 式取法", not bad, "命中 %d 处" % len(bad))

    ui_src = io.open(os.path.join(SRC, "ui_main.py"), encoding="utf-8").read()
    ck("ui_main.py 仍用生产签名 AutoVJEngine(cfg, self.audio.state, lib)",
       "AutoVJEngine(self.cfg, self.audio.state" in ui_src)
    ck("ui_main.py 调用了 set_audio_engine（引擎本体注入）",
       "set_audio_engine(self.audio)" in ui_src)
    ck("ui_main.py 有 _merge_cue_song_meta（码表歌名映射）",
       "def _merge_cue_song_meta" in ui_src)

    print()
    if FAIL:
        print("结论：FAIL —— %d 项未通过：%s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("结论：PASS —— 接线一致，码表自动跟拍能读到识别结果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
