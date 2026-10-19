# -*- coding: utf-8 -*-
"""基于 VirtualDJ 网格的「八拍开头」二选一核对页。

思路（比从 8 个相位里盲选可靠得多）：
    VDJ 已经给出可靠的 4 拍小节（锚点 + BPM）。八拍 = 两个小节，
    所以「八拍开头」只可能是 **奇数小节** 或 **偶数小节** —— 二选一。

产出：P:\\AutoVJ\\_beat_vdj.html
用法：python tools/_beat_html_vdj.py
"""
import json
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vdj_grid as vg          # noqa: E402

DIR = "W:/临时结界相关/2of/tofu"
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "_beat_vdj.html")
CLIP_DIR = os.path.join(ROOT, "_beat_audio", "vclips")
NPTS = 2600

# (文件名, 用户标注BPM, 区间[(t0,t1,拍数)], VDJ 库匹配关键字)
CASES = [
    ("Rolling Contact - Outburst Oxygen (Sharp Remaster).flac", 128,
     [(0, 30, 64), (60, 91, 64)], "ELECTRO CUTE 7"),
    ("Rolling Contact - The Lost (135 Extended).flac", 135,
     [(58, 85, 64)], r"TRANSFINITE\Rolling"),
    ("Rolling Contact - Crescent Horizon (Rebuild).flac", 145,
     [(80, 106, 64)], "TRANSFINITE 2"),
    ("Rolling Contact - Dust.flac", 128,
     [(15, 29, 32), (75, 105, 64)], "ELECTRO CUTE 8"),
    ("Rolling Contact - Endless & Limitless.flac", 155,
     [(51, 75, 64)], "Look Back"),
]


def make_clip(path, sr, t, pre=0.8, post=3.2):
    t = max(0.0, float(t))
    key = int(round(t * 20))
    fn = "v%07d.wav" % key
    dst = os.path.join(CLIP_DIR, fn)
    if not os.path.exists(dst):
        start = max(0, int((t - pre) * sr))
        n = int((pre + post) * sr)
        data, _ = sf.read(path, start=start, frames=n, dtype="float32",
                          always_2d=True)
        sf.write(dst, data.mean(axis=1), sr, subtype="PCM_16")
    return "_beat_audio/vclips/" + fn


def wave_peaks(path, maxsec, npts=NPTS):
    info = sf.info(path)
    sr = int(info.samplerate)
    frames = min(info.frames, int(maxsec * sr))
    data, _ = sf.read(path, dtype="float32", always_2d=True, frames=frames)
    m = data.mean(axis=1).astype(np.float32)
    del data
    n = len(m)
    seg = max(1, n // npts)
    k = max(1, n // seg)
    mm = m[:k * seg].reshape(k, seg)
    lo, hi = mm.min(axis=1), mm.max(axis=1)
    a = max(1e-6, float(max(abs(lo).max(), abs(hi).max())))
    return (lo / a).tolist(), (hi / a).tolist(), n / float(sr), seg / float(sr)


def pick_vdj(db, kw, fn):
    ex = [e for e in vg.find(db, kw)
          if os.path.basename(e["path"]).lower() == fn.lower()
          and e["anchor"] is not None]
    return ex[0] if ex else None


def build():
    os.makedirs(CLIP_DIR, exist_ok=True)
    db = vg.load_db()
    songs = []
    for fn, ub, segs, kw in CASES:
        path = os.path.join(DIR, fn)
        if not os.path.isfile(path):
            print("  [!] 缺文件 %s" % path)
            continue
        v = pick_vdj(db, kw, fn)
        if v is None:
            print("  [!] 库里没找到已分析的副本: %s" % fn)
            continue
        bl, a = v["beat_len"], v["anchor"]
        sr = int(sf.info(path).samplerate)
        maxsec = max(t1 for _, t1, _ in segs) + 10.0
        lo, hi, dur, dt = wave_peaks(path, maxsec)
        print("  %s  BPM %.2f 锚点 %.3f" % (fn[:46], v["bpm"], a), flush=True)

        segdata = []
        for (t0, t1, n) in segs:
            # 分两类候选：全局拍号 k ≡ 0 (mod 8) 与 k ≡ 4 (mod 8)
            k0 = int(np.floor((t0 - a) / bl)) - 2
            k1 = int(np.ceil((t1 - a) / bl)) + 2
            A, B = [], []
            for k in range(k0, k1 + 1):
                if k < 0:
                    continue
                t = a + k * bl
                if k % 8 == 0:
                    A.append(t)
                elif k % 8 == 4:
                    B.append(t)
            A = [x for x in A if -0.5 <= x <= dur]
            B = [x for x in B if -0.5 <= x <= dur]
            # 小节线（VDJ 视角：每 4 拍一条）
            bars = [a + k * bl for k in range(max(0, k0), k1 + 1) if k % 4 == 0]
            bars = [x for x in bars if -0.5 <= x <= dur]
            A_in = [x for x in A if x >= t0 - 0.6]
            B_in = [x for x in B if x >= t0 - 0.6]
            segdata.append({
                "t0": t0, "t1": t1, "claim": n,
                "A": [float(x) for x in A], "B": [float(x) for x in B],
                "Ain": [float(x) for x in A_in], "Bin": [float(x) for x in B_in],
                "bars": [float(x) for x in bars],
                "clipA": make_clip(path, sr, A_in[0]) if A_in else "",
                "clipB": make_clip(path, sr, B_in[0]) if B_in else "",
                "beats": [float(a + k * bl) for k in range(max(0, k0), k1 + 1)],
            })
        songs.append({
            "file": fn, "user_bpm": ub, "vdj_bpm": v["bpm"], "anchor": a,
            "dur": dur, "dt": dt, "lo": lo, "hi": hi,
            "remix": [[n_, float(p_)] for n_, p_ in sorted(v["remix"], key=lambda x: x[1])],
            "key": v["key"], "segs": segdata,
        })

    html = TEMPLATE.replace("__DATA__", json.dumps(songs, ensure_ascii=False))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s（%.0f KB）" % (OUT, os.path.getsize(OUT) / 1024))
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>八拍开头核对（VirtualDJ 网格版）— AutoVJ</title>
<style>
  :root{--bg:#0f1115;--fg:#e8ecf1;--dim:#8b95a5;--line:#232a35;--acc:#ffb454;
        --ok:#2ecc71;--bad:#e74c3c}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{padding:16px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;
         background:var(--bg);z-index:9}
  h1{margin:0 0 4px;font-size:18px}
  .hint{color:var(--dim);font-size:12.5px;margin:0}
  #songs{padding:14px 22px 90px}
  .song{border:1px solid var(--line);border-radius:10px;padding:13px 15px;margin-bottom:16px;
        background:#141821}
  .song h2{margin:0 0 3px;font-size:15px}
  .meta{color:var(--dim);font-size:12.5px}
  .meta b{color:var(--fg)}
  .wrap{position:relative;background:#0b0e13;border-radius:6px;overflow:hidden;margin:8px 0}
  canvas{display:block;width:100%;height:130px}
  table{border-collapse:collapse;font-size:12.5px;width:100%}
  th,td{padding:3px 8px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:500}
  code{background:#1c222c;padding:1px 5px;border-radius:4px;font-size:12px}
  .cand{display:flex;gap:10px;align-items:center;margin:6px 0;padding:7px 10px;
        border:1px solid var(--line);border-radius:7px}
  .cand.sel{border-color:var(--ok);background:rgba(46,204,113,.09)}
  .cand .tag{font-weight:700;min-width:58px}
  .cand .tag.a{color:#ffb454}.cand .tag.b{color:#6ec1ff}
  button{border:1px solid var(--line);background:#1a1f28;color:var(--fg);
         border-radius:5px;padding:3px 10px;cursor:pointer;font-size:12px}
  button.on{border-color:var(--ok);color:var(--ok)}
  #bar{position:fixed;left:0;right:0;bottom:0;background:#12161e;border-top:1px solid var(--line);
       padding:8px 22px;display:flex;gap:12px;align-items:center;font-size:12.5px}
  #out{flex:1;background:#0b0e13;color:var(--dim);border:1px solid var(--line);
       border-radius:5px;padding:4px 8px;font-size:11.5px;height:28px}
</style>
</head>
<body>
<header>
  <h1>八拍开头核对 · VirtualDJ 网格版</h1>
  <p class="hint">网格用的是 VirtualDJ 已分析好的 BPM + 锚点（橙色细线 = 拍，白色竖线 = 小节线，
    紫色块 = VDJ 标记的段落）。每段只需要二选一：<b>A = 奇数小节起</b> 还是 <b>B = 偶数小节起</b>。
    点「▶」听该点前后约 4 秒，选完点底部「导出」把结果复制给我。</p>
</header>
<div id="songs"></div>
<div id="bar">
  <button id="expo">导出核对结果</button>
  <button id="clr">清空</button>
  <input id="out" readonly placeholder="点「导出」后这里显示 JSON，复制发我">
</div>
<script>
const DATA = __DATA__;
const LSKEY = "autovj_beat_vdj_v1";
let review = {};
try { review = JSON.parse(localStorage.getItem(LSKEY) || "{}"); } catch (e) { review = {}; }
function saveReview(){ try{ localStorage.setItem(LSKEY, JSON.stringify(review)); }catch(e){} }
function fmt(t){ const m = Math.floor(t/60), s = t - m*60; return m + ":" + (s<10?"0":"") + s.toFixed(2); }
let curClip = null;
function playClip(url){
  if (!url) return;
  if (curClip) { try { curClip.pause(); } catch(e){} }
  curClip = new Audio(url); curClip.play().catch(()=>{});
}

const songsEl = document.getElementById("songs");
DATA.forEach((song, si) => {
  const sec = document.createElement("section");
  sec.className = "song";
  let segHtml = "";
  song.segs.forEach((g, gi) => {
    const k = "s" + si + "g" + gi;
    const sel = review[k] || "";
    segHtml += `<div style="margin-top:12px">
      <div class="meta"><b>区间 ${fmt(g.t0)} – ${fmt(g.t1)}</b>（你标注 <b>${g.claim}</b> 拍）
        · A 候选 ${g.A.length} 个 · B 候选 ${g.B.length} 个</div>
      <div class="cand ${sel==='A'?'sel':''}">
        <span class="tag a">A 奇数小节</span>
        <code>${g.Ain.slice(0,4).map(fmt).join("  ") || "—"}</code>
        <button class="pv" data-clip="${g.clipA}">▶ 试听</button>
        <button class="pk" data-k="${k}" data-v="A">就是这个</button>
      </div>
      <div class="cand ${sel==='B'?'sel':''}">
        <span class="tag b">B 偶数小节</span>
        <code>${g.Bin.slice(0,4).map(fmt).join("  ") || "—"}</code>
        <button class="pv" data-clip="${g.clipB}">▶ 试听</button>
        <button class="pk" data-k="${k}" data-v="B">就是这个</button>
      </div>
      <div class="meta"><button class="pk" data-k="${k}" data-v="X">两个都不对 / 听不出来</button>
        ${sel ? ('　已选：<b>' + sel + '</b>') : ''}</div>
    </div>`;
  });
  const remix = song.remix.filter(r => Math.abs(r[1]) > 0.1)
      .map(r => r[0] + " @ " + fmt(r[1])).join("　");
  sec.innerHTML = `
    <h2>${si+1}. ${song.file.replace("Rolling Contact - ","")}</h2>
    <div class="meta">VDJ：BPM <b>${song.vdj_bpm.toFixed(2)}</b> · 锚点 <b>${song.anchor.toFixed(3)}s</b>
      · 调 ${song.key||"—"}　${remix ? "　段落： " + remix : ""}</div>
    <div class="wrap"><canvas width="1600" height="130"></canvas></div>
    ${segHtml}`;
  songsEl.appendChild(sec);

  const cv = sec.querySelector("canvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const xOf = t => t / song.dur * W;
  ctx.clearRect(0,0,W,H);
  song.segs.forEach(g => {
    ctx.fillStyle = "rgba(90,110,140,.10)";
    ctx.fillRect(xOf(g.t0), 0, xOf(g.t1)-xOf(g.t0), H);
  });
  // 段落块
  for (let i = 0; i < song.remix.length - 1; i++) {
    const [n0, p0] = song.remix[i], [n1, p1] = song.remix[i+1];
    if (p0 < 0 || p1 < 0) continue;
    ctx.fillStyle = "rgba(160,110,220,.22)";
    ctx.fillRect(xOf(p0), 0, xOf(p1)-xOf(p0), H);
    ctx.fillStyle = "#c9a6ff"; ctx.font = "11px sans-serif";
    ctx.fillText(n0, xOf(p0)+3, 12);
  }
  // 波形
  const n = song.lo.length;
  ctx.fillStyle = "#3d4d63";
  for (let i=0;i<n;i++){
    const x = xOf(i*song.dt);
    const y0 = H/2 - song.hi[i]*H/2*0.92, y1 = H/2 - song.lo[i]*H/2*0.92;
    ctx.fillRect(x, y0, Math.max(1,W/n), Math.max(1,y1-y0));
  }
  // 拍 / 小节
  song.segs.forEach(g => {
    ctx.fillStyle = "rgba(255,180,84,.35)";
    g.beats.forEach(t => { if(t>=0&&t<=song.dur) ctx.fillRect(xOf(t),0,1,H); });
    ctx.fillStyle = "rgba(255,255,255,.75)";
    g.bars.forEach(t => { if(t>=0&&t<=song.dur) ctx.fillRect(xOf(t)-1,0,2,H); });
  });

  sec.querySelectorAll(".pv").forEach(b => b.addEventListener("click", () =>
    playClip(b.dataset.clip)));
  sec.querySelectorAll(".pk").forEach(b => b.addEventListener("click", () => {
    const k = b.dataset.k, v = b.dataset.v;
    if (review[k] === v) { delete review[k]; } else { review[k] = v; }
    saveReview();
    // 重绘选中态
    const box = b.closest("div").parentNode;
    box.querySelectorAll(".cand").forEach((c, idx) => {
      const want = (idx === 0 ? "A" : "B");
      c.classList.toggle("sel", review[k] === want);
    });
  }));
});

document.getElementById("expo").addEventListener("click", () => {
  const out = [];
  Object.keys(review).sort().forEach(k => {
    const m = k.match(/^s(\d+)g(\d+)$/);
    if (!m) return;
    const si = +m[1], gi = +m[2];
    const song = DATA[si], g = song.segs[gi];
    const v = review[k];
    const list = (v === "A") ? g.A : (v === "B" ? g.B : []);
    out.push({file: song.file, seg: [g.t0, g.t1], claim: g.claim,
              vdj_bpm: song.vdj_bpm, vdj_anchor: song.anchor,
              verdict: v, first_time: list.length ? +list[0].toFixed(3) : null});
  });
  document.getElementById("out").value =
    JSON.stringify({mark: "autovj-beat-vdj-v1", items: out});
  document.getElementById("out").select();
});
document.getElementById("clr").addEventListener("click", () => {
  review = {}; saveReview(); location.reload();
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(build())
