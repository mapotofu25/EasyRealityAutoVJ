# -*- coding: utf-8 -*-
"""生成「八拍乐句核对报告」HTML。

产出：P:\\AutoVJ\\_beat_report.html
- 每首歌：播放器 + 波形 + 拍位细线 + 八拍边界粗线（可点击跳转）
- 每个八拍边界带 ✓ / ✗ 标注按钮，结果存 localStorage，可一键导出 JSON
  （把导出的内容发回来即可作为算法的监督信号）

用法: python tools/_beat_html.py
"""
import json
import os
import shutil
import sys
import urllib.parse

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import beat_analyze as ba          # noqa: E402

DIR = "W:/临时结界相关/2of/tofu"
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "_beat_report.html")
AUDIO_DIR = os.path.join(ROOT, "_beat_audio")
CLIP_DIR = os.path.join(AUDIO_DIR, "clips")

CASES = [
    ("Rolling Contact - Outburst Oxygen (Sharp Remaster).flac", 128,
     [(0, 30, 64), (60, 91, 64)]),
    ("Rolling Contact - The Lost (135 Extended).flac", 135,
     [(58, 85, 64)]),
    ("Rolling Contact - Crescent Horizon (Rebuild).flac", 145,
     [(80, 106, 64)]),
    ("Rolling Contact - Dust.flac", 128,
     [(15, 29, 32), (75, 105, 64)]),
    ("Rolling Contact - Endless & Limitless.flac", 155,
     [(51, 75, 64)]),
]

NPTS = 2600


def make_clip(path, sr, t, pre=0.8, post=2.8):
    """把 `t` 附近 pre+post 秒切成一个独立小片段（wav）。

    为什么不用整曲 + `audio.currentTime`：本地 http 预览服务通常不支持 Range 请求，
    浏览器无法 seek，设 `currentTime` 会被忽略（表现为「一点就从头发声」）。
    切成独立文件后每个按钮只需 `play()`，不依赖 seek。
    """
    t = max(0.0, float(t))
    key = int(round(t * 20))            # 50ms 粒度，天然去重
    fn = "c%07d.wav" % key
    dst = os.path.join(CLIP_DIR, fn)
    if not os.path.exists(dst):
        start = max(0, int((t - pre) * sr))
        n = int((pre + post) * sr)
        data, _ = sf.read(path, start=start, frames=n, dtype="float32",
                          always_2d=True)
        mono = data.mean(axis=1)
        sf.write(dst, mono, sr, subtype="PCM_16")
    return "_beat_audio/clips/" + fn


def wave_peaks(path, maxsec, npts=NPTS):
    """返回 (min[], max[], dur, dt)，每段 npts 个点。"""
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
    lo = mm.min(axis=1)
    hi = mm.max(axis=1)
    # 归一化到 ±1
    a = max(1e-6, float(max(abs(lo).max(), abs(hi).max())))
    return (lo / a).tolist(), (hi / a).tolist(), n / float(sr), seg / float(sr)


def build():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(CLIP_DIR, exist_ok=True)
    songs = []
    for fn, ub, segs in CASES:
        path = os.path.join(DIR, fn)
        if not os.path.isfile(path):
            print("  [!] 缺文件: %s" % path)
            continue
        maxsec = max(t1 for _, t1, _ in segs) + 10.0
        print("  分析 %s ..." % fn, flush=True)
        r = ba.segment_report(path, seg=segs, user_bpm=ub, maxsec=maxsec,
                              verbose=False)
        lo, hi, dur, dt = wave_peaks(path, maxsec)
        sr = int(sf.info(path).samplerate)
        # 整曲复制到报告同级目录（相对路径 → 本地 http 预览也能播）
        dst = os.path.join(AUDIO_DIR, fn)
        if (not os.path.exists(dst)) or os.path.getsize(dst) != os.path.getsize(path):
            shutil.copy2(path, dst)
        url = "_beat_audio/" + urllib.parse.quote(fn)

        seg_data = []
        for it in r["segs"]:
            period = 60.0 / it["bpm_sel"]
            grid = ba.beat_grid(0.0, it["t1"] + 2.0, period, it["phase"])
            ph_starts = [t for t in it["phrase_starts"] if 0 <= t <= dur]
            # 8 个相位各自的候选起点（区间内前几个），供用户直接点选
            ka = int(np.floor((it["t0"] - it["phase"]) / period))
            kb = int(np.ceil((it["t1"] - it["phase"]) / period))
            cand = {}
            for p in range(8):
                ts = [it["phase"] + k * period for k in range(ka, kb + 1)
                      if (k % 8) == p and it["phase"] + k * period >= 0.0]
                cand[p] = [float(x) for x in ts[:5]]
            # 每个相位第一个起点的独立片段（点一下即播，不依赖 seek）
            cand_clip = {p: (make_clip(path, sr, cand[p][0]) if cand[p] else "")
                         for p in range(8)}
            beat_clips = [make_clip(path, sr, t) for t in ph_starts]
            seg_data.append({
                "t0": it["t0"], "t1": it["t1"], "claim": it["beats_claim"],
                "found": it["n_beats_grid"], "bpm": it["bpm_sel"],
                "phase": it["phase"], "conc": it["phase_conc"],
                "off0": it["t0_off_beats"], "off1": it["t1_off_beats"],
                "align": it.get("beat_align", 0),
                "bh": it.get("beat_vs_half", 0),
                "beats": [float(x) for x in grid],
                "starts": [float(x) for x in ph_starts],
                "scores": it["phrase_scores"],
                "best": it["phrase_best"],
                "cand": cand,
                "cand_clip": cand_clip,
                "beat_clips": beat_clips,
            })
        songs.append({
            "file": fn, "url": url, "user_bpm": ub,
            "dur": dur, "dt": dt, "lo": lo, "hi": hi,
            "cands": r["bpm_peaks"][:5],
            "segs": seg_data,
        })
        print("    OK  %d 个区间" % len(seg_data), flush=True)

    data = json.dumps(songs, ensure_ascii=False)
    html = TEMPLATE.replace("__DATA__", data)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s（%.0f KB）" % (OUT, os.path.getsize(OUT) / 1024))
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>八拍乐句核对报告 — AutoVJ</title>
<style>
  :root{--bg:#0f1115;--fg:#e8ecf1;--dim:#8b95a5;--line:#232a35;--acc:#ffb454;
        --ok:#2ecc71;--bad:#e74c3c;--beat:#4a5a70;--phrase:#ffb454;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{padding:18px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;
         background:var(--bg);z-index:10}
  h1{margin:0 0 6px;font-size:18px}
  .hint{color:var(--dim);font-size:12.5px;margin:0}
  #songs{padding:16px 22px 80px}
  .song{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:18px;
        background:#141821}
  .song h2{margin:0 0 4px;font-size:15px}
  .meta{color:var(--dim);font-size:12.5px;margin-bottom:10px}
  .meta b{color:var(--fg)}
  audio{width:100%;height:34px;margin-bottom:10px}
  .wrap{position:relative;background:#0b0e13;border-radius:6px;overflow:hidden}
  canvas{display:block;width:100%;height:150px;cursor:crosshair}
  .sumbox{display:flex;flex-wrap:wrap;gap:14px;font-size:12.5px;color:var(--dim);
          margin:8px 0 12px}
  .sumbox .k{color:var(--fg);font-weight:600}
  .tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11.5px;
       margin-left:6px}
  .tag.ok{background:rgba(46,204,113,.18);color:var(--ok)}
  .tag.bad{background:rgba(231,76,60,.18);color:var(--bad)}
  table{border-collapse:collapse;font-size:12.5px;width:100%}
  th,td{padding:3px 8px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--dim);font-weight:500}
  code{background:#1c222c;padding:1px 5px;border-radius:4px;font-size:12px}
  .mbtn{display:inline-flex;gap:4px;align-items:center}
  .mbtn button{border:1px solid var(--line);background:#1a1f28;color:var(--dim);
               border-radius:5px;padding:1px 7px;cursor:pointer;font-size:12px}
  .mbtn button.on-ok{border-color:var(--ok);color:var(--ok)}
  .mbtn button.on-bad{border-color:var(--bad);color:var(--bad)}
  table.ph{margin:4px 0 2px}
  table.ph tr.sel{background:rgba(255,180,84,.12)}
  .pick{border:1px solid var(--line);background:#1a1f28;color:var(--dim);
        border-radius:5px;padding:1px 8px;cursor:pointer;font-size:12px}
  .pick.on-ok{border-color:var(--ok);color:var(--ok)}
  .pkst{color:var(--ok);font-size:11.5px;margin-left:5px}
  #bar{position:fixed;left:0;right:0;bottom:0;background:#12161e;border-top:1px solid var(--line);
       padding:8px 22px;display:flex;gap:12px;align-items:center;font-size:12.5px}
  #bar button{border:1px solid var(--line);background:#1a1f28;color:var(--fg);
              border-radius:6px;padding:5px 12px;cursor:pointer}
  #out{flex:1;background:#0b0e13;color:var(--dim);border:1px solid var(--line);
       border-radius:5px;padding:4px 8px;font-size:11.5px;height:28px}
</style>
</head>
<body>
<header>
  <h1>八拍乐句核对报告</h1>
  <p class="hint">橙色粗线 = 算法判定的「八拍开头」；细线 = 拍位。
    每个「▶」按钮播放的是该点前后的 <b>独立音频片段</b>（点了就响，不需要定位）。
    「相位」表里 8 个候选 = 八拍开头可能落在这首歌的 8 个拍位上，逐个听、选出对的那个，
    点「就用这个」；最后用底部「导出」把结果复制发我。</p>
</header>
<div id="songs"></div>
<div id="bar">
  <button id="expo">导出核对结果</button>
  <button id="clr">清空</button>
  <input id="out" readonly placeholder="点「导出」后这里显示 JSON，复制发给我即可">
</div>
<script>
const DATA = __DATA__;
const LSKEY = "autovj_beat_review_v1";
let review = {};
try { review = JSON.parse(localStorage.getItem(LSKEY) || "{}"); } catch (e) { review = {}; }
function saveReview(){ try{ localStorage.setItem(LSKEY, JSON.stringify(review)); }catch(e){} }
function rk(si, gi, ti){ return si + ":" + gi + ":" + ti; }

function fmt(t){
  const m = Math.floor(t/60), s = t - m*60;
  return m + ":" + (s<10?"0":"") + s.toFixed(2);
}

// 片段播放器：每个候选点是一个独立小文件，点一下即播（不依赖 seek）
let curClip = null;
function playClip(url){
  if (!url) return;
  if (curClip) { try { curClip.pause(); } catch(e){} }
  curClip = new Audio(url);
  curClip.play().catch(()=>{});
}

const songsEl = document.getElementById("songs");
DATA.forEach((song, si) => {
  const sec = document.createElement("section");
  sec.className = "song";
  const cand = song.cands.map(c => c[0].toFixed(1) + "(" + c[1].toFixed(1) + ")").join(" · ");
  let segHtml = "";
  song.segs.forEach((g, gi) => {
    const offOk = Math.abs(g.off0) <= 0.35 && Math.abs(g.off1) <= 0.35;
    const diff = g.found - g.claim;
    let phRows = "";
    for (let p = 0; p < 8; p++) {
      const ts = g.cand[p] || [];
      const sel = (p === g.best);
      phRows += `<tr class="${sel?'sel':''}">
        <td><b>${p}</b>${sel?' ★':''}</td>
        <td><code>${ts.slice(0,3).map(fmt).join("   ")}</code></td>
        <td>${g.scores[p].toFixed(3)}</td>
        <td><button class="pv" data-clip="${g.cand_clip[p]||''}">▶ 试听</button></td>
        <td><button class="pick" data-p="${p}">就用这个</button>
            <span class="pkst"></span></td></tr>`;
    }
    segHtml += `<div style="margin-top:12px">
      <div class="meta"><b>区间 ${fmt(g.t0)} – ${fmt(g.t1)}</b>（标注 <b>${g.claim}</b> 拍）
        · 网格 <b>${g.bpm.toFixed(2)}</b> BPM · 检出 <b>${g.found}</b> 拍
        <span class="tag ${diff===0?"ok":(Math.abs(diff)<=2?"":"bad")}">差 ${diff>0?"+":""}${diff}</span>
        · 端点偏移 起 ${g.off0.toFixed(2)} 拍 / 终 ${g.off1.toFixed(2)} 拍
        <span class="tag ${offOk?"ok":"bad"}">${offOk?"贴合":"偏离"}</span>
      </div>
      <div class="meta" style="margin-top:8px"><b>八拍开头应该落在哪个相位？</b>
        逐个点「▶ 试听」（会从候选点前 1 拍开始播），听哪个是乐句开头，
        然后点「就用这个」。★ = 算法当前的选择。</div>
      <table class="ph" data-gi="${gi}"><tr><th style="width:60px">相位</th>
        <th style="width:230px">该相位起点（区间内前 3 个）</th>
        <th style="width:70px">打分</th><th style="width:90px">试听</th>
        <th>确认</th></tr>${phRows}</table>
      <div class="meta" style="margin-top:10px">下面是算法按当前相位列出的完整八拍边界：</div>
      <div class="marks" data-si="${si}" data-gi="${gi}"></div>
    </div>`;
  });
  sec.innerHTML = `
    <h2>${si+1}. ${song.file.replace("Rolling Contact - ","")}</h2>
    <div class="meta">标注 BPM <b>${song.user_bpm}</b> · 算法候选：${cand}
      · 时长 ${fmt(song.dur)}</div>
    <audio controls preload="metadata" src="${song.url}"></audio>
    <div class="wrap"><canvas width="1600" height="150"></canvas></div>
    <div class="sumbox"></div>
    ${segHtml}`;
  songsEl.appendChild(sec);

  const audio = sec.querySelector("audio");
  const cv = sec.querySelector("canvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;

  function xOf(t){ return t / song.dur * W; }

  function draw(playT){
    ctx.clearRect(0,0,W,H);
    // 区间底色
    song.segs.forEach(g => {
      ctx.fillStyle = "rgba(90,110,140,.10)";
      ctx.fillRect(xOf(g.t0), 0, xOf(g.t1)-xOf(g.t0), H);
    });
    // 波形
    const n = song.lo.length;
    ctx.fillStyle = "#3d4d63";
    for (let i=0;i<n;i++){
      const x = xOf(i*song.dt);
      const y0 = H/2 - song.hi[i]*H/2*0.94;
      const y1 = H/2 - song.lo[i]*H/2*0.94;
      ctx.fillRect(x, y0, Math.max(1, W/n), Math.max(1, y1-y0));
    }
    // 拍位（细线）+ 八拍边界（粗线）
    song.segs.forEach(g => {
      ctx.fillStyle = "rgba(120,140,170,.42)";
      g.beats.forEach(t => { if(t>=0&&t<=song.dur){ const x=xOf(t); ctx.fillRect(x,0,1,H);} });
      g.starts.forEach(t => {
        if(t<0||t>song.dur) return;
        const x = xOf(t);
        ctx.fillStyle = "#ffb454";
        ctx.fillRect(x-1, 0, 2.2, H);
        ctx.beginPath(); ctx.arc(x, 12, 5, 0, 6.283); ctx.fill();
      });
      // 标注端点
      [g.t0, g.t1].forEach(t => {
        const x = xOf(t); ctx.fillStyle = "#e74c3c"; ctx.fillRect(x-1,0,2,H);
      });
    });
    // 游标
    if (playT != null){
      const x = xOf(playT);
      ctx.fillStyle = "#2ecc71"; ctx.fillRect(x-1, 0, 2, H);
    }
  }
  draw(null);

  function tick(){ draw(audio.currentTime); requestAnimationFrame(tick); }
  audio.addEventListener("play", () => requestAnimationFrame(tick));
  audio.addEventListener("pause", () => draw(null));

  cv.addEventListener("click", ev => {
    const r = cv.getBoundingClientRect();
    const t = (ev.clientX - r.left) / r.width * song.dur;
    audio.currentTime = Math.max(0, Math.min(song.dur-0.05, t));
  });

  // 八拍边界列表 + 核对按钮
  sec.querySelectorAll(".marks").forEach(box => {
    const gi = +box.dataset.gi;
    const g = song.segs[gi];
    let html = `<table><tr><th style="width:70px">#</th><th style="width:110px">时间</th>
                <th style="width:120px">试听</th><th>核对</th></tr>`;
    g.starts.forEach((t, ti) => {
      html += `<tr><td>${ti+1}</td><td><code>${fmt(t)}</code></td>
        <td><button class="pv" data-clip="${g.beat_clips[ti]||''}">▶ 前 2 拍</button></td>
        <td><span class="mbtn" data-ti="${ti}">
          <button data-v="ok">✓ 对</button><button data-v="bad">✗ 错</button>
          <span class="st"></span></span></td></tr>`;
    });
    html += `</table>`;
    box.innerHTML = html;
    box.querySelectorAll(".pv").forEach(b => b.addEventListener("click", () =>
      playClip(b.dataset.clip)));
    box.querySelectorAll(".mbtn").forEach(mb => {
      const ti = +mb.dataset.ti;
      mb.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
        const v = b.dataset.v;
        const k = rk(si, gi, ti);
        if (review[k] === v) delete review[k]; else review[k] = v;
        saveReview(); paint();
      }));
    });
    function paint(){
      box.querySelectorAll(".mbtn").forEach(mb => {
        const t2 = +mb.dataset.ti;
        const v = review[rk(si, gi, t2)];
        mb.querySelectorAll("button").forEach(b => {
          b.className = (v === b.dataset.v) ? ("on-" + b.dataset.v) : "";
        });
        mb.querySelector(".st").textContent = v ? (v==="ok"?"已确认对":"已标记错") : "";
      });
    }
    paint();
  });

  // ---- 相位候选：试听 + 选它 ----
  function paintPicks(){
    sec.querySelectorAll("table.ph").forEach(tb => {
      const gi = +tb.dataset.gi;
      const cur = review["ph:" + si + ":" + gi];
      tb.querySelectorAll(".pick").forEach(btn => {
        const p = +btn.dataset.p;
        const on = (cur === p);
        btn.className = "pick" + (on ? " on-ok" : "");
        const st = btn.parentNode.querySelector(".pkst");
        if (st) st.textContent = on ? "已选" : "";
      });
    });
  }
  sec.querySelectorAll(".pick").forEach(b => b.addEventListener("click", () => {
    const tb = b.closest("table");
    const gi = +tb.dataset.gi;
    const p = +b.dataset.p;
    const k = "ph:" + si + ":" + gi;
    if (review[k] === p) { delete review[k]; } else { review[k] = p; }
    saveReview(); paintPicks();
  }));
  sec.querySelectorAll("table.ph .pv").forEach(b => b.addEventListener("click", () =>
    playClip(b.dataset.clip)));
  paintPicks();
});

document.getElementById("expo").addEventListener("click", () => {
  const lines = [], picks = [];
  Object.keys(review).sort().forEach(k => {
    if (k.indexOf("ph:") === 0) {
      const parts = k.split(":");
      const si = +parts[1], gi = +parts[2], p = review[k];
      const song = DATA[si], g = song.segs[gi];
      const ts = g.cand[p] || [];
      picks.push({file: song.file, seg: [g.t0, g.t1], algo_phase: g.best,
                  chosen_phase: p, chosen_time: ts.length ? +ts[0].toFixed(3) : null});
    } else {
      const pr = k.split(":").map(Number);
      const song = DATA[pr[0]], g = song.segs[pr[1]];
      lines.push({file: song.file, seg: [g.t0, g.t1], beat_index: pr[2] + 1,
                  time: +g.starts[pr[2]].toFixed(3), verdict: review[k]});
    }
  });
  document.getElementById("out").value =
    JSON.stringify({mark: "autovj-beat-review-v1", phase_picks: picks, items: lines});
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
