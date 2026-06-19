#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Kevin Griffin
"""
clip_gallery.py — build a sortable/filterable HTML contact sheet.

Reads catalog/manifest.csv (and, if present, order.csv to show the arranged
position and cluster color-coding) and writes catalog/gallery.html: a grid of
thumbnails you can sort by any column, filter by text, and click through to the
actual clip. The modern replacement for the old scenedetect image+html scan.

Usage:
  python3 clip_gallery.py catalog/manifest.csv
  python3 clip_gallery.py catalog/manifest.csv --open   # also open in browser

No third-party deps (standard library only).
"""
import argparse
import csv
import json
import os
import webbrowser


def build_gallery_data(manifest_path):
    """Read manifest.csv (+ order.csv if present) into the gallery row list.

    Reusable by both the CLI and the web tier so the HTML isn't forked. `clip`
    is a file:// URL and `thumb` is the manifest-relative path; the web layer
    rewrites those to served URLs before rendering.
    """
    cat = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, newline="") as fh:
        rows = list(csv.DictReader(fh))

    # merge order.csv if present (adds position + cluster)
    order_path = os.path.join(cat, "order.csv")
    pos_by_clip, cluster_by_clip = {}, {}
    if os.path.exists(order_path):
        with open(order_path, newline="") as fh:
            for r in csv.DictReader(fh):
                key = os.path.abspath(r["clip"])
                pos_by_clip[key] = int(r["position"])
                cluster_by_clip[key] = int(r["cluster"])

    data = []
    for r in rows:
        key = os.path.abspath(r["clip"])
        data.append({
            "name": os.path.basename(r["clip"]),
            "clip": "file://" + key,
            "thumb": r.get("thumb", ""),
            "source": r.get("source", ""),
            "capture_time": r.get("capture_time", ""),
            "duration_s": float(r.get("duration_s") or 0),
            "motion": float(r.get("motion_energy") or 0),
            "luma": float(r.get("mean_luma") or 0),
            "hue": float(r.get("hue_deg") or 0),
            "colorful": float(r.get("colorfulness") or 0),
            "sharp": float(r.get("sharpness") or 0),
            "pos": pos_by_clip.get(key, ""),
            "cluster": cluster_by_clip.get(key, ""),
        })
    return data


def render_from_data(data):
    """Inject a gallery row list into the HTML template -> full page string."""
    return _TEMPLATE.replace("/*DATA*/", json.dumps(data))


def render_gallery(manifest_path):
    """Convenience: manifest path -> rendered HTML string."""
    return render_from_data(build_gallery_data(manifest_path))


def main():
    ap = argparse.ArgumentParser(description="Build an HTML clip gallery.")
    ap.add_argument("manifest", help="path to catalog/manifest.csv")
    ap.add_argument("--open", action="store_true", help="open in browser when done")
    args = ap.parse_args()

    data = build_gallery_data(args.manifest)
    html = render_from_data(data)
    out = os.path.join(os.path.dirname(os.path.abspath(args.manifest)), "gallery.html")
    with open(out, "w") as fh:
        fh.write(html)
    print(f"Wrote {out} ({len(data)} clips)")
    if args.open:
        webbrowser.open("file://" + out)


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Clip gallery</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background:#15171a; color:#e8e8e8; }
  header { position:sticky; top:0; background:#1c1f24; padding:12px 16px;
           border-bottom:1px solid #2c2f36; z-index:5; }
  h1 { font-size:15px; margin:0 0 8px; font-weight:600; }
  .ctl { display:flex; gap:8px; flex-wrap:wrap; align-items:center; font-size:13px; }
  select, input { background:#0f1113; color:#e8e8e8; border:1px solid #333;
                  border-radius:6px; padding:5px 8px; font-size:13px; }
  #grid { display:grid; gap:10px; padding:16px;
          grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); }
  .card { background:#1c1f24; border:1px solid #2c2f36; border-radius:8px;
          overflow:hidden; border-left:4px solid var(--cl,#444); }
  .card img { width:100%; display:block; background:#000; aspect-ratio:4/3;
              object-fit:cover; }
  .meta { padding:7px 9px; font-size:11px; line-height:1.45; }
  .meta .nm { font-weight:600; font-size:11px; word-break:break-all;
              color:#cfe3ff; }
  .bar { height:4px; border-radius:2px; background:#2c2f36; margin-top:4px; }
  .bar > i { display:block; height:100%; border-radius:2px; background:#5b9dff; }
  a { color:inherit; text-decoration:none; }
  .pos { float:right; color:#8a93a0; }
  .muted { color:#8a93a0; }
</style></head>
<body>
<header>
  <h1>Clip gallery — <span id="count"></span> clips</h1>
  <div class="ctl">
    Sort:
    <select id="sort">
      <option value="pos">arranged order</option>
      <option value="motion">motion energy</option>
      <option value="luma">brightness</option>
      <option value="hue">hue</option>
      <option value="colorful">colorfulness</option>
      <option value="sharp">sharpness</option>
      <option value="duration_s">duration</option>
      <option value="capture_time">capture time</option>
      <option value="name">name</option>
    </select>
    <select id="dir"><option value="asc">asc</option><option value="desc">desc</option></select>
    <input id="filter" placeholder="filter by name/source/cluster…" size="22">
    <label class="muted"><input type="checkbox" id="bycluster"> group by cluster</label>
  </div>
</header>
<div id="grid"></div>
<script>
const DATA = /*DATA*/;
const grid = document.getElementById('grid');
const palette = ['#5b9dff','#ff8b5b','#5bd6a0','#d68bff','#ffd45b','#ff5b7a',
                 '#5bd6ff','#a0ff5b','#ff5bd6','#b0b0b0'];
const maxMotion = Math.max(...DATA.map(d=>d.motion), 1e-9);
function clusterColor(c){ return c===''? '#444' : palette[c % palette.length]; }

function render(){
  const key = document.getElementById('sort').value;
  const dir = document.getElementById('dir').value === 'asc' ? 1 : -1;
  const q = document.getElementById('filter').value.toLowerCase();
  const byCluster = document.getElementById('bycluster').checked;
  let rows = DATA.filter(d =>
    !q || d.name.toLowerCase().includes(q) || (d.source||'').toLowerCase().includes(q)
    || String(d.cluster).includes(q));
  rows.sort((a,b)=>{
    if (byCluster && a.cluster !== b.cluster) return (a.cluster>b.cluster?1:-1);
    let x=a[key], y=b[key];
    if (x===''&&y!=='') return 1; if (y===''&&x!=='') return -1;
    if (typeof x === 'string') return x.localeCompare(y)*dir;
    return (x-y)*dir;
  });
  grid.innerHTML = rows.map(d => `
    <div class="card" style="--cl:${clusterColor(d.cluster)}">
      <a href="${d.clip}" title="open clip">
        <img loading="lazy" src="${d.thumb}" alt="">
      </a>
      <div class="meta">
        <span class="pos">${d.pos!==''?'#'+d.pos:''}</span>
        <span class="nm">${d.name}</span><br>
        <span class="muted">${d.capture_time||'—'} · ${d.duration_s.toFixed(1)}s`
        + (d.cluster!==''?` · cl ${d.cluster}`:'') + `</span>
        <div class="bar" title="motion ${d.motion.toFixed(3)}">
          <i style="width:${(d.motion/maxMotion*100).toFixed(0)}%"></i></div>
      </div>
    </div>`).join('');
  document.getElementById('count').textContent = rows.length;
}
['sort','dir','filter','bycluster'].forEach(id=>{
  document.getElementById(id).addEventListener('input', render);
});
// default to motion if no arranged order exists
if (DATA.every(d=>d.pos==='')) document.getElementById('sort').value='motion';
render();
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
