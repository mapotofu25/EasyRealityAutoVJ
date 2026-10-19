# -*- coding: utf-8 -*-
r"""erlib 端到端验证：**造夹具 → 扫描 → 导出 .eravjlib → 读回校验**。

## 为什么自造夹具
项目铁律：测试夹具自己造、落系统临时目录（`tempfile.mkdtemp()`），
**不依赖用户目录**（否则测试随用户曲库变化而随机失败）。

## 反向复现（证明测试不是空过）
`--old` 模式 monkeypatch 掉指纹写入（模拟"只存元数据、不存指纹"的半成品）
⇒ 「指纹必须 > 0」这条校验**必须失败**。
"""

import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                               # noqa: E402
import soundfile as sf                                           # noqa: E402

# ⚠ 夹具必须有**足够长度**：4 秒的音频算不出指纹（频谱峰值不够 ⇒ fps=0，
#   第一版就是因此误判成"扫描失败"）。真实歌曲 3~5 分钟，这里取 20~30 秒折中。
SPECS = [("a_tone.flac", 25.0, 128.0),
         ("b_sweep.flac", 30.0, 174.0),
         ("c_noise.flac", 22.0, 140.0)]


def make_fixtures(d):
    """造 3 个带明显节拍的测试音频（有节奏才可能被算出 BPM）。"""
    out = []
    sr = 44100
    for name, dur, bpm in SPECS:
        n = int(sr * dur)
        t = np.arange(n) / sr
        beat = (np.sin(2 * np.pi * (bpm / 60.0) * t) > 0.75).astype(np.float32)
        x = (0.30 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32) + 0.40 * beat
        x += (0.02 * np.random.RandomState(7).randn(n)).astype(np.float32)
        p = os.path.join(d, name)
        sf.write(p, np.stack([x, x], axis=1), sr, format="FLAC")
        out.append(p)
    return out


def main():
    old = "--old" in sys.argv
    print("=" * 74)
    print("erlib 端到端验证" + ("   ★ --old（应失败）" if old else ""))
    print("=" * 74)

    from erlib.format import LibReader, LibWriter
    from erlib.scanner import Scanner, find_audio

    if old:
        import erlib.format as _F
        _orig = _F.LibWriter.add_song

        def _patched(self, sid, meta, fps=(), cover_bytes=None, commit=True):
            return _orig(self, sid, meta, (), cover_bytes, commit)   # 丢掉指纹
        _F.LibWriter.add_song = _patched
        print("★ 已启用 --old：add_song 不写指纹\n")

    d = tempfile.mkdtemp(prefix="erlib_e2e_")
    try:
        files = make_fixtures(d)
        print("① 夹具：%d 个音频（%s）"
              % (len(files), ", ".join(os.path.basename(f) for f in files)))
        found = find_audio(d)
        print("   find_audio 找到：%d 个" % len(found))

        pkg = os.path.join(d, "test.eravjlib")
        print("\n② 扫描 + 写入 %s" % os.path.basename(pkg))
        w = LibWriter(pkg, root=d, resume=False, app_ver="e2e-test")
        sc = Scanner(root=d)
        st = sc.scan_tree(d, w)
        w.close()
        print("   ok=%d fail=%d skip=%d  用时 %.1fs"
              % (st["ok"], st["fail"], st["skip"], st.get("elapsed", 0)))
        print("   分段耗时：解码 %.2fs / 指纹 %.2fs / 曲风 %.2fs / 网格 %.2fs / 封面 %.2fs"
              % (st["t_decode"], st["t_fp"], st["t_meta"], st["t_grid"],
                 st["t_cover"]))
        print("   包体积：%.2f MB" % (os.path.getsize(pkg) / 1e6))

        print("\n③ 读回校验")
        r = LibReader(pkg)
        ok_ver, msg = r.check_versions()
        n = r.count()
        songs = list(r.songs())
        n_fp = sum(len(b) for b in r.iter_fingerprints())
        print("   版本检查：%s（%s）" % ("✓" if ok_ver else "✗", msg))
        print("   songs=%d  fingerprints=%d" % (n, n_fp))
        fields_ok = True
        for s in songs:
            need = ("file_name", "file_size", "duration", "rel_path", "grid")
            miss = [k for k in need if k not in s]
            if miss:
                fields_ok = False
                print("   ★ %s 缺字段：%s" % (s.get("file_name"), miss))
        if songs:
            s0 = songs[0]
            print("   样例：%s  %.1fs  genres=%r  grid.bpm=%r"
                  % (s0["file_name"], s0["duration"], s0.get("genres"),
                     (s0.get("grid") or {}).get("bpm")))
        r.close()

        print("\n" + "=" * 74)
        checks = {
            "版本检查通过": ok_ver,
            "songs 数 = 3": n == len(SPECS),
            "指纹数 > 0": n_fp > 0,
            "每首字段齐全": fields_ok,
        }
        for k, v in checks.items():
            print("   %s %s" % ("✓" if v else "✗", k))
        all_ok = all(checks.values())
        if old:
            print("\n反向复现：%s（指纹为 0 ⇒ 这正是要抓的半成品）"
                  % ("✅ 成功" if not all_ok else "❌ 未复现"))
            sys.exit(0 if not all_ok else 1)
        print("\n结论：%s" % ("✅ 端到端通过" if all_ok else "❌ 有校验未过"))
        print("=" * 74)
        sys.exit(0 if all_ok else 1)
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
