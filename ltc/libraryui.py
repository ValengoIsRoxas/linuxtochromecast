"""Library page for library mode (`ltc <folder>`), plus the add-on that links
the regular remote-control page back to it.

Same constraints and visual language as webui.PAGE: one self-contained string,
no external requests, light/dark through prefers-color-scheme (it follows the
device). Phone first: browsing happens on the couch, one-handed.
Data comes from /api/library and /api/item/<id>; playback starts with
POST /api/play and then hands over to /player.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="mobile-web-app-capable" content="yes">
<title>Library</title>
<style>
  /* Same palette as the remote (webui.PAGE). Every text token clears 4.5:1 on
     the surface it sits on; here --ink-faint is also used for small text, so it
     is held to the same bar. The light theme follows the device and is the dark
     one's own family: the same cool hue at low chroma, ochre as the one warm note. */
  :root {
    --bg: #101014; --panel: #191920; --panel-alt: #22222b;
    --ink: #e9e6df; --ink-strong: #f3f0ea; --ink-dim: #8f8a82;
    --ink-mid: #a8a399; --ink-faint: #8f8a82;
    --panel-ink: #a5a1ae; --panel-ink-dim: #908c9a; --panel-ink-chip: #c4c0cc;
    --accent: #a08a5e; --led: #7a9478; --led-wait: #d6a13c; --danger: #c26a5e;
    --border: #2b2823; --border-strong: #3c3934; --chip-ink: #101014; --chip-active-bg: #e9e6df;
    --scrim: rgba(8, 8, 10, .72);
    --watched: #e0483e; --convert: #e9e6df; --convert-stripe: #a08a5e;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #eceff3; --panel: #e1e5eb; --panel-alt: #d3d8e0;
      --ink: #1a1c22; --ink-strong: #121419; --ink-dim: #585d68;
      --ink-mid: #4d525c; --ink-faint: #585d68;
      --panel-ink: #4d525c; --panel-ink-dim: #565b66; --panel-ink-chip: #2f333b;
      --accent: #76591b; --led: #4c7a4c; --led-wait: #b8892e; --danger: #a4433a;
      --border: #cbd0d9; --border-strong: #b9bfca; --chip-ink: #eceff3; --chip-active-bg: #1a1c22;
      --scrim: rgba(18, 22, 30, .38);
      --watched: #c8372d; --convert: #f7f8fa; --convert-stripe: #76591b;
    }
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { min-height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    overflow-x: hidden;
  }
  body.locked { overflow: hidden; }
  button { font: inherit; color: inherit; background: none; border: none; cursor: pointer; padding: 0; }
  button:focus-visible, a:focus-visible, input:focus-visible { outline: 2px solid var(--ink-mid); outline-offset: 2px; }
  a { color: inherit; text-decoration: none; }

  .app { max-width: 1180px; margin: 0 auto; padding: 34px 16px 140px; }
  @media (min-width: 760px) { .app { padding: 44px 40px 140px; } }

  /* --- header --- */
  .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--ink-faint); flex: none; }
  .dot.playing { background: var(--led); }
  .dot.buffering { background: var(--led-wait); }
  .head { display: flex; align-items: flex-end; gap: 16px; flex-wrap: wrap; }
  .title {
    font: 300 32px/1.1 system-ui, -apple-system, sans-serif; letter-spacing: -.03em;
    margin: 0; color: var(--ink-strong); flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  @media (min-width: 760px) { .title { font-size: 46px; } }
  .search {
    flex: 1 1 220px; max-width: 320px; font: 400 14px/1 system-ui, sans-serif; color: var(--ink);
    background: transparent; border: none; border-bottom: 1px solid var(--border-strong);
    padding: 8px 0; border-radius: 0;
  }
  .search::placeholder { color: var(--ink-faint); }
  .back {
    display: inline-block; margin-top: 18px;
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .14em; text-transform: uppercase;
    color: var(--ink-mid); padding: 6px 0;
  }
  .back:hover { color: var(--ink-strong); }

  /* --- sections --- */
  .section-cap {
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .18em; text-transform: uppercase;
    color: var(--ink-dim); margin: 38px 0 14px; display: flex; gap: 10px; align-items: baseline;
  }
  .section-cap .count { color: var(--ink-faint); letter-spacing: .08em; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 22px 14px; }
  @media (min-width: 760px) { .grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 28px 20px; } }
  .rail {
    display: grid; grid-auto-flow: column; grid-auto-columns: minmax(210px, 62%);
    gap: 14px; overflow-x: auto; scroll-snap-type: x mandatory; padding-bottom: 6px;
    margin-right: -16px; padding-right: 16px; scrollbar-width: none;
  }
  .rail::-webkit-scrollbar { display: none; }
  .rail .card { scroll-snap-align: start; }
  @media (min-width: 760px) { .rail { grid-auto-columns: 260px; margin-right: 0; padding-right: 0; } }

  .card { display: flex; flex-direction: column; gap: 9px; text-align: left; min-width: 0; }
  .art {
    position: relative; aspect-ratio: 16 / 9; border-radius: 10px; overflow: hidden;
    background: var(--panel); border: 1px solid var(--border);
  }
  .art img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
  .art .initial {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font: 200 34px/1 system-ui, sans-serif; color: var(--ink-faint); letter-spacing: -.02em;
  }
  /* under every poster: red = watched, white with blue stripes = converted.
     Always takes its space (transparent when empty) so titles stay aligned. */
  .cbar { position: relative; height: 4px; border-radius: 2px; overflow: hidden; margin-top: -3px; }
  .cbar.on { background: var(--panel-alt); }
  .art .seen {
    position: absolute; top: 8px; right: 8px; width: 20px; height: 20px; border-radius: 50%;
    background: rgba(10,10,12,.62); color: #e9e6df; display: flex; align-items: center; justify-content: center;
  }
  .card:hover .art, .card:focus-visible .art { border-color: var(--border-strong); }
  .card-title {
    font: 400 14px/1.3 system-ui, sans-serif; color: var(--ink-strong);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .card-meta { font: 400 12px/1.2 system-ui, sans-serif; color: var(--ink-dim); margin-top: -5px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card-meta.converting, .ep-meta.converting { color: var(--convert-stripe); }

  /* --- posters: movies and series --- */
  .grid.posters { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 24px 14px; }
  @media (min-width: 760px) { .grid.posters { grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 30px 20px; } }
  .art.poster { aspect-ratio: 2 / 3; border-radius: 8px; }

  .empty { color: var(--ink-dim); font-size: 14px; margin-top: 40px; max-width: 460px; }
  .empty code { font: 13px ui-monospace, Menlo, Consolas, monospace; color: var(--ink-mid); word-break: break-all; }

  /* --- show view --- */
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip {
    font: 400 13px/1 system-ui, sans-serif; border-radius: 999px; padding: 10px 15px;
    color: var(--panel-ink-chip); background: var(--panel-alt);
  }
  .chip.on { color: var(--chip-ink); background: var(--chip-active-bg); }
  .chip:disabled { opacity: .45; cursor: default; }
  .episodes { display: flex; flex-direction: column; gap: 4px; margin-top: 20px; }
  .ep {
    display: grid; grid-template-columns: 128px 1fr; gap: 14px; align-items: center;
    padding: 8px; margin: 0 -8px; border-radius: 12px; text-align: left;
  }
  .ep:hover, .ep:focus-visible { background: var(--panel); }
  @media (min-width: 760px) { .ep { grid-template-columns: 176px 1fr; } }
  .ep .art { border-radius: 8px; }
  .ep-art { display: flex; flex-direction: column; gap: 6px; }
  .ep-art .cbar { margin-top: 0; }
  .ep-code { font: 12px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-dim); }
  .ep-title { font: 400 15px/1.3 system-ui, sans-serif; color: var(--ink-strong); margin-top: 6px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .ep-meta { display: block; font: 400 12px/1 system-ui, sans-serif; color: var(--ink-dim); margin-top: 7px; }

  /* --- detail sheet --- */
  .scrim {
    position: fixed; inset: 0; background: var(--scrim); z-index: 20;
    display: flex; align-items: flex-end; justify-content: center;
    opacity: 0; pointer-events: none; transition: opacity .16s;
  }
  .scrim.open { opacity: 1; pointer-events: auto; }
  .sheet {
    width: 100%; max-width: 560px; max-height: 92vh; overflow-y: auto;
    background: var(--panel); color: var(--ink); border-radius: 22px 22px 0 0;
    padding: 0 0 calc(24px + env(safe-area-inset-bottom));
    transform: translateY(24px); transition: transform .18s;
  }
  .scrim.open .sheet { transform: none; }
  @media (min-width: 760px) {
    .scrim { align-items: center; }
    .sheet { border-radius: 18px; padding-bottom: 28px; }
  }
  .sheet .art { border-radius: 0; border: none; aspect-ratio: 16 / 8; }
  .sheet .art .seen { display: none; }
  .hero { display: flex; align-items: center; justify-content: center; background: #0d0d0f; }
  .hero .backdrop { filter: blur(22px) brightness(.5) saturate(1.2); transform: scale(1.2); }
  .hero img.cover { position: relative; width: auto; height: 84%; aspect-ratio: 2 / 3; border-radius: 6px;
    box-shadow: 0 10px 28px -10px rgba(0, 0, 0, .7); }
  .links { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .ext {
    display: inline-flex; align-items: center; gap: 7px; padding: 9px 13px; border-radius: 999px;
    font: 500 13px/1 system-ui, sans-serif; color: var(--panel-ink-chip); background: var(--panel-alt);
  }
  .ext:hover { color: var(--ink-strong); }
  .sheet-body { padding: 20px 22px 0; }
  .sheet-close {
    position: absolute; top: 12px; right: 12px; width: 34px; height: 34px; border-radius: 50%;
    background: rgba(10,10,12,.62); color: #e9e6df; display: flex; align-items: center; justify-content: center;
  }
  /* watched (red) over converted (white, blue stripes), like a video player's
     buffered range: conversion runs ahead of playback */
  .watch { margin: 0 0 18px; }
  .watch[hidden] { display: none; }
  .watch-track {
    position: relative; height: 6px; border-radius: 3px; overflow: hidden;
    background: var(--panel-alt);
  }
  .watch-track i, .cbar i { position: absolute; top: 0; bottom: 0; left: 0; }
  .watch-track .conv, .cbar .conv {
    background: repeating-linear-gradient(-45deg, var(--convert) 0 5px, var(--convert-stripe) 5px 8px);
    background-size: 11.3px 11.3px;
  }
  .watch-track .conv.moving, .cbar .conv.moving { animation: stripes .8s linear infinite; }
  @keyframes stripes { to { background-position: 11.3px 0; } }
  @media (prefers-reduced-motion: reduce) { .watch-track .conv.moving, .cbar .conv.moving { animation: none; } }
  .watch-track .seen-bar, .cbar .seen-bar { background: var(--watched); border-radius: 0 3px 3px 0; }
  .watch-track [hidden], .cbar [hidden] { display: none; }
  .watch-cap {
    display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 9px;
    font: 400 12px/1.2 ui-monospace, Menlo, Consolas, monospace; color: var(--panel-ink-dim);
  }
  .watch-cap span { display: inline-flex; align-items: center; gap: 7px; }
  .swatch { width: 14px; height: 6px; border-radius: 2px; flex: none; }
  .swatch.seen { background: var(--watched); }
  .swatch.conv { background: repeating-linear-gradient(-45deg, var(--convert) 0 3px, var(--convert-stripe) 3px 5px); }

  .d-eyebrow { font: 400 11px/1 system-ui, sans-serif; letter-spacing: .18em; text-transform: uppercase; color: var(--panel-ink-dim); }
  .d-title { font: 300 26px/1.15 system-ui, sans-serif; letter-spacing: -.02em; color: var(--ink-strong); margin: 10px 0 0; }
  .d-meta { font: 400 12px/1.5 system-ui, sans-serif; color: var(--panel-ink-dim); margin-top: 8px; word-break: break-word; }
  .d-cap {
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .18em; text-transform: uppercase;
    color: var(--panel-ink-dim); margin: 26px 0 12px;
  }
  .d-note { font: 400 12px/1.4 system-ui, sans-serif; color: var(--panel-ink-dim); margin-top: 10px; }
  .actions { display: flex; flex-direction: column; gap: 10px; margin-top: 28px; }
  .btn {
    height: 52px; border-radius: 13px; font: 500 15px/1 system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center; gap: 10px;
  }
  .btn.primary { background: var(--chip-active-bg); color: var(--chip-ink); }
  .btn.secondary { background: var(--panel-alt); color: var(--panel-ink-chip); }
  .btn.ghost { height: 40px; font-weight: 400; font-size: 13px; color: var(--panel-ink-dim); }
  .dropzone {
    display: block; margin-top: 14px; padding: 14px; text-align: center; cursor: pointer;
    font: 400 13px/1.3 system-ui, sans-serif; color: var(--panel-ink-dim);
    border: 1px dashed var(--border-strong); border-radius: 12px;
  }
  .dropzone:hover { color: var(--panel-ink-chip); }
  .dropzone.err { color: var(--danger); border-color: var(--danger); }
  .sheet.dragging { outline: 2px dashed var(--accent); outline-offset: -8px; }
  .sheet.dragging .dropzone { color: var(--ink-strong); border-color: var(--accent); background: var(--panel-alt); }
  .os-open { margin-top: 10px; justify-content: flex-start; padding: 0 2px; color: var(--accent); }
  .os-box { margin-top: 14px; }
  .os-box code { font: 11px ui-monospace, Menlo, Consolas, monospace; word-break: break-all; }
  .os-list { display: flex; flex-direction: column; gap: 4px; max-height: 300px; overflow-y: auto; }
  .os-row {
    display: grid; grid-template-columns: 46px 1fr; gap: 10px; align-items: center; text-align: left;
    padding: 10px 12px; border-radius: 10px; background: var(--panel-alt); color: var(--panel-ink-chip);
  }
  .os-row:disabled { opacity: .6; cursor: default; }
  .os-lang { font: 500 11px/1 ui-monospace, Menlo, Consolas, monospace; text-transform: uppercase; color: var(--ink-strong); }
  .os-text { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .os-rel { font: 400 13px/1.25 system-ui, sans-serif; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .os-tags { font: 400 11px/1.2 system-ui, sans-serif; color: var(--panel-ink-dim); }
  .os-tags b { font-weight: 500; color: var(--led); }
  .actions .d-note { margin: 2px 2px 0; }
  .conv-note { color: var(--panel-ink-chip); font-size: 13px; margin-bottom: 4px; }
  .btn:disabled { opacity: .5; cursor: default; }
  .d-error { color: var(--danger); font-size: 13px; margin-top: 14px; }
  .d-error:empty { display: none; }

  /* --- now playing bar --- */
  .now {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 10;
    padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
    background: var(--panel); border-top: 1px solid var(--border);
    display: none; align-items: center; gap: 12px;
  }
  .now.show { display: flex; }
  .now .art { width: 72px; flex: none; border-radius: 6px; }
  .now-text { flex: 1; min-width: 0; }
  .now-title { font: 400 14px/1.25 system-ui, sans-serif; color: var(--ink-strong);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .now-state { font: 400 12px/1 system-ui, sans-serif; color: var(--panel-ink-dim); margin-top: 6px;
    display: flex; align-items: center; gap: 7px; }
  .round {
    width: 44px; height: 44px; border-radius: 50%; border: 1px solid var(--border-strong);
    display: flex; align-items: center; justify-content: center; color: var(--ink-strong); flex: none;
  }
  .now-cancel, .now.waiting .round, .now.waiting a.now-open { display: none; }
  .now.waiting .now-cancel { display: block; color: var(--panel-ink-dim); }
  .now-open {
    font: 500 12px/1 system-ui, sans-serif; letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent); padding: 12px 4px 12px 8px; flex: none;
  }
  @media (min-width: 760px) {
    .now { left: 50%; right: auto; bottom: 18px; transform: translateX(-50%); width: min(620px, calc(100% - 80px));
      border-radius: 16px; border: 1px solid var(--border-strong); padding: 10px 14px; }
  }

  /* --- footer --- */
  .foot { display: flex; gap: 22px; margin-top: 56px; flex-wrap: wrap; }
  .foot button { font: 400 11px/1 system-ui, sans-serif; color: var(--ink-faint); letter-spacing: .04em; padding: 6px 0; }
  .foot button:hover { color: var(--ink-mid); }
  .foot .cast { display: flex; align-items: center; gap: 9px; flex-basis: 100%; font: 400 13px/1.2 system-ui, sans-serif; color: var(--ink-dim); }
  .foot .confirm { color: var(--danger); }
  .foot .root { font: 11px/1.4 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-faint); flex-basis: 100%; word-break: break-all; }
  #err { color: var(--danger); font-size: 13px; margin-top: 14px; }
  #err:empty { display: none; }
</style>
</head>
<body>

<main class="app" id="app">
  <div id="view"></div>
  <div id="err"></div>
  <div class="foot">
    <div class="cast"><span class="dot" id="led"></span><span id="device">…</span></div>
    <button id="refresh">Refresh library</button>
    <button id="quit">Quit ltc</button>
    <div class="root" id="root"></div>
  </div>
</main>

<div class="now" id="now">
  <div class="art" id="nowArt"></div>
  <div class="now-text">
    <div class="now-title" id="nowTitle"></div>
    <div class="now-state"><span class="dot" id="nowLed"></span><span id="nowState"></span></div>
  </div>
  <button class="round" id="nowToggle" aria-label="Play or pause"></button>
  <a class="now-open" href="/player">Remote</a>
  <button class="now-open now-cancel" id="nowCancel">Cancel</button>
</div>

<div class="scrim" id="scrim">
  <div class="sheet" id="sheet" role="dialog" aria-modal="true"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const PLAY = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 1.5 L12.5 7 L3 12.5 Z" fill="currentColor"/></svg>';
const PAUSE = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><rect x="2.5" y="1.5" width="3" height="11" fill="currentColor"/><rect x="8.5" y="1.5" width="3" height="11" fill="currentColor"/></svg>';
const CHECK = '<svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.5 L5 9.2 L10 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const EXT = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M3.5 1.5 H8.5 V6.5 M8.5 1.5 L1.5 8.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CLOSE = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 2 L10 10 M10 2 L2 10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

let lib = null, status = null, query = '';
const seasonPick = {};

const esc = t => String(t ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const fmt = s => {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
};
const minutes = s => s >= 3600 ? Math.floor(s / 3600) + ' h ' + Math.round((s % 3600) / 60) + ' min' : Math.max(1, Math.round(s / 60)) + ' min';
const fold = t => String(t).normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

function art(id, label, progress) {
  return '<div class="art">' + artInner(id, label, progress) + '</div>';
}
function artInner(id, label, progress, img) {
  const p = progress || {};
  return '<span class="initial">' + esc((label || '?').trim().charAt(0).toUpperCase()) + '</span>'
    + (img || '<img loading="lazy" alt="" src="/thumb/' + id + '" onerror="this.remove()">')
    + (p.watched ? '<span class="seen" title="Watched">' + CHECK + '</span>' : '');
}
// Movies and series show their poster (local or from IMDb); without one, a
// frame of the video, like everything else.
const noPoster = new Set();
function poster(id, label, progress) {
  const src = noPoster.has(id) ? '/thumb/' + id : '/poster/' + id;
  const img = '<img loading="lazy" alt="" src="' + src + '" onerror="noPosterFor(this, \'' + id + '\')">';
  return '<div class="art poster">' + artInner(id, label, progress, img) + '</div>';
}
function noPosterFor(img, id) {
  if (img.src.includes('/thumb/')) return img.remove();
  noPoster.add(id);
  img.src = '/thumb/' + id;
}

// Filled in by paintBars(), so it can follow playback and conversion live.
const meta = (id, text, cls) => '<span class="' + cls + '" data-meta="' + id + '" data-base="' + esc(text) + '">' + esc(text) + '</span>';
const bar = id => '<div class="cbar" data-bar="' + id + '"><i class="conv" hidden></i><i class="seen-bar"></i></div>';

// --- bars: red = watched, white with blue stripes = converted --------------------
// One model for the cards and the detail sheet. Live numbers for what's
// playing; otherwise a background conversion that may still be running (it
// keeps going after Stop), a saved one, and the saved progress.
let itemsById = {};

function barModel(id, it, knownDur) {
  const p = (it && it.progress) || {};
  if (status && status.phase === 'playing' && status.item && status.item.id === id)
    return {dur: status.duration, pos: status.position, conv: status.conversion, watched: false};
  const running = status && status.conversions && status.conversions[id];
  const dur = knownDur || p.duration || (running && running.duration) || 0;
  const conv = running || (it && it.saved ? {start: 0, end: dur, done: true, saved: true, mode: it.mode} : null);
  return {dur, pos: p.watched ? dur : (p.position || 0), conv, watched: !!p.watched};
}

function fractions(m) {
  const c = m.conv;
  const frac = (x, dur) => dur ? Math.max(0, Math.min(1, (x || 0) / dur)) : 0;
  const dur = m.dur || (c && c.duration) || 0;
  const cdur = (c && c.duration) || dur;
  return {
    seen: m.watched ? 1 : frac(m.pos, dur),
    c0: c && !c.saved ? frac(c.start, cdur) : 0,
    // A saved conversion is whole even when the length isn't known yet
    // (never played, so nothing probed it).
    c1: c ? (c.saved ? 1 : frac(c.end, cdur)) : 0,
    moving: !!(c && !c.done && !c.queued),
  };
}

function paintBar(el, m) {
  const fr = fractions(m);
  const conv = el.querySelector('.conv'), seen = el.querySelector('.seen-bar');
  seen.style.width = (fr.seen * 100).toFixed(2) + '%';
  conv.hidden = !m.conv;
  if (m.conv) {
    conv.style.left = (fr.c0 * 100).toFixed(2) + '%';
    conv.style.width = (Math.max(0, fr.c1 - fr.c0) * 100).toFixed(2) + '%';
    conv.classList.toggle('moving', fr.moving);
  }
  return fr;
}

function convText(c) {
  if (!c) return '';
  if (c.queued) return 'Waiting to convert';
  const pct = c.duration ? Math.floor(c.end / c.duration * 100) : 0;
  return 'Converting ' + pct + '%' + (c.eta ? ' · ' + minutes(c.eta) + ' left' : '');
}

function paintBars() {
  document.querySelectorAll('[data-bar]').forEach(el => {
    const id = el.dataset.bar;
    const m = barModel(id, itemsById[id]);
    const fr = paintBar(el, m);
    el.classList.toggle('on', fr.seen > 0.003 || !!m.conv);
  });
  // The meta line under a card says it too while it converts.
  document.querySelectorAll('[data-meta]').forEach(el => {
    const c = status && status.conversions && status.conversions[el.dataset.meta];
    const live = c && !c.done ? convText(c) : '';
    el.textContent = live || el.dataset.base;
    el.classList.toggle('converting', !!live);
  });
}

function itemMeta(it) {
  const p = it.progress || {};
  if (p.watched) return 'Watched';
  if (p.position && p.duration) return fmt(p.duration - p.position) + ' left';
  return '';
}

// --- views --------------------------------------------------------------------

function route() {
  const h = location.hash.replace(/^#\/?/, '');
  const [kind, id] = h.split('/');
  return {kind: kind || 'home', id};
}

function render() {
  if (!lib) return;
  const r = route();
  const base = r.kind === 'item' ? (sheetBase || {kind: 'home'}) : r;
  if (base.kind === 'show' && lib.shows.some(s => s.id === base.id)) renderShow(base.id);
  else renderHome();
  if (r.kind === 'item') openItem(r.id); else closeSheet();
}
let sheetBase = null;

function renderHome() {
  const v = $('view');
  const q = fold(query.trim());
  const movies = lib.movies.filter(m => !q || fold(m.title).includes(q));
  const shows = lib.shows.filter(s => !q || fold(s.name).includes(q)
    || s.episodes.some(e => fold(e.title || '').includes(q)));

  let html = '<div class="head"><h1 class="title">Library</h1>'
    + '<input class="search" id="search" type="search" placeholder="Search" autocomplete="off" value="' + esc(query) + '"></div>';

  if (!lib.movies.length && !lib.shows.length) {
    html += '<p class="empty">No videos in <code>' + esc(lib.root) + '</code> yet. '
      + 'Drop some files in and use Refresh library below.</p>';
  }

  if (!q && lib.continue.length) {
    html += '<div class="section-cap">Continue watching</div><div class="rail">';
    for (const it of lib.continue) {
      const title = it.kind === 'episode' ? it.show : (it.title + (it.year ? ' (' + it.year + ')' : ''));
      const sub = it.kind === 'episode'
        ? it.code + (it.title ? ' · ' + it.title : '')
        : itemMeta(it);
      html += '<button class="card" data-item="' + it.id + '">' + art(it.id, title, it.progress) + bar(it.id)
        + '<span class="card-title">' + esc(title) + '</span>'
        + meta(it.id, sub || ' ', 'card-meta') + '</button>';
    }
    html += '</div>';
  }

  if (shows.length) {
    html += '<div class="section-cap">Series <span class="count">' + shows.length + '</span></div><div class="grid posters">';
    for (const s of shows) {
      const n = s.episodes.length;
      const meta = (s.seasons.length > 1 ? s.seasons.length + ' seasons · ' : '')
        + n + (n === 1 ? ' episode' : ' episodes')
        + (s.unwatched && s.unwatched < n ? ' · ' + s.unwatched + ' new' : '');
      html += '<a class="card" href="#show/' + s.id + '">' + poster(s.id, s.name, {watched: s.unwatched === 0})
        + '<span class="card-title">' + esc(s.name) + '</span>'
        + '<span class="card-meta">' + esc(meta) + '</span></a>';
    }
    html += '</div>';
  }

  if (movies.length) {
    html += '<div class="section-cap">Movies <span class="count">' + movies.length + '</span></div><div class="grid posters">';
    for (const m of movies) {
      html += '<button class="card" data-item="' + m.id + '">' + poster(m.id, m.title, m.progress) + bar(m.id)
        + '<span class="card-title">' + esc(m.title) + '</span>'
        + meta(m.id, [m.year, itemMeta(m)].filter(Boolean).join(' · ') || ' ', 'card-meta') + '</button>';
    }
    html += '</div>';
  }

  if (q && !movies.length && !shows.length) html += '<p class="empty">Nothing matches that.</p>';

  const hadFocus = document.activeElement && document.activeElement.id === 'search';
  const caret = hadFocus ? document.activeElement.selectionStart : null;
  v.innerHTML = html;
  const input = $('search');
  input.oninput = () => { query = input.value; renderHome(); };
  if (hadFocus) { input.focus(); if (caret !== null) input.setSelectionRange(caret, caret); }
  document.title = 'Library';
  paintBars();
}

function renderShow(id) {
  const show = lib.shows.find(s => s.id === id);
  const season = seasonPick[id] ?? defaultSeason(show);
  const eps = show.episodes.filter(e => e.season === season);

  let html = '<a class="back" href="#">&lsaquo;&nbsp; Library</a>'
    + '<div class="head"><h1 class="title">' + esc(show.name) + '</h1></div>';
  if (show.seasons.length > 1) {
    html += '<div class="section-cap">Season</div><div class="chips">'
      + show.seasons.map(n => '<button class="chip' + (n === season ? ' on' : '') + '" data-season="' + n + '">' + n + '</button>').join('')
      + '</div>';
  } else {
    html += '<div class="section-cap">Season ' + season + '</div>';
  }
  html += '<div class="episodes">';
  for (const e of eps) {
    const label = e.title || ('Episode ' + e.episode);
    html += '<button class="ep" data-item="' + e.id + '"><span class="ep-art">' + art(e.id, label, e.progress) + bar(e.id) + '</span>'
      + '<span><span class="ep-code">E' + String(e.episode).padStart(2, '0') + '</span>'
      + '<span class="ep-title">' + esc(label) + '</span>'
      + meta(e.id, itemMeta(e), 'ep-meta') + '</span></button>';
  }
  html += '</div>';
  $('view').innerHTML = html;
  document.title = show.name;
  paintBars();
}

function defaultSeason(show) {
  // The season of the first unwatched episode: where the viewer actually is.
  const next = show.episodes.find(e => !(e.progress || {}).watched);
  return (next || show.episodes[0]).season;
}

// --- detail sheet ---------------------------------------------------------------

let detail = null, pick = {sub: 'auto', audio: null}, sheetToken = 0;

async function openItem(id) {
  const token = ++sheetToken;
  document.body.classList.add('locked');
  $('scrim').classList.add('open');
  if (detail && detail.id === id) return paintSheet();
  detail = {id, loading: true};
  paintSheet();
  try {
    const r = await fetch('/api/item/' + id);
    const data = await r.json();
    if (token !== sheetToken) return;
    if (!r.ok) throw new Error(data.error || 'Could not read the file.');
    detail = data;
    pick = {sub: data.default_sub === null ? 'off' : data.default_sub, audio: null};
  } catch (e) {
    if (token !== sheetToken) return;
    detail = {id, error: e.message};
  }
  paintSheet();
}

function paintSheet() {
  const d = detail;
  const s = $('sheet');
  if (!d) return;
  const name = d.kind === 'episode' ? d.show : (d.title || '');
  let html = '<div style="position:relative">' + sheetArt(d, name)
    + '<button class="sheet-close" id="sheetClose" aria-label="Close">' + CLOSE + '</button></div>'
    + '<div class="sheet-body">';

  if (d.loading) {
    html += '<div class="d-meta">Reading the file…</div></div>';
    s.innerHTML = html; wireSheet(); return;
  }
  if (d.error && !d.kind) {
    html += '<div class="d-error">' + esc(d.error) + '</div></div>';
    s.innerHTML = html; wireSheet(); return;
  }

  html += '<div class="watch" id="watch" hidden><div class="watch-track">'
    + '<i class="conv" id="wConv" hidden></i><i class="seen-bar" id="wSeen"></i></div>'
    + '<div class="watch-cap" id="wCap"></div></div>';

  if (d.kind === 'episode') {
    html += '<div class="d-eyebrow">' + esc(d.show) + ' · ' + esc(d.code) + '</div>'
      + '<h2 class="d-title">' + esc(d.title || 'Episode ' + d.episode) + '</h2>';
  } else {
    html += '<div class="d-eyebrow">Movie' + (d.year ? ' · ' + d.year : '') + '</div>'
      + '<h2 class="d-title">' + esc(d.title) + '</h2>';
  }
  html += '<div class="d-meta">' + (d.duration ? esc(minutes(d.duration)) + ' · ' : '') + esc(d.file) + '</div>';
  if (d.links) {
    const out = (href, text) => '<a class="ext" href="' + esc(href) + '" target="_blank" rel="noopener">' + text + EXT + '</a>';
    // For an episode, IMDb is the series' page; Letterboxd only has films.
    html += '<div class="links">' + out(d.links.imdb, 'IMDb')
      + (d.kind === 'movie' ? out(d.links.letterboxd, 'Letterboxd') : '') + '</div>';
  }

  html += '<div class="d-cap">Subtitles</div><div class="chips">';
  for (const t of d.subs) {
    html += '<button class="chip' + (pick.sub === t.id ? ' on' : '') + '" data-sub="' + t.id + '">' + esc(t.label) + '</button>';
  }
  html += '<button class="chip' + (pick.sub === 'off' ? ' on' : '') + '" data-sub="off">Off</button></div>';
  if (!d.subs.length) html += '<div class="d-note">This file has no text subtitles.</div>';
  html += dropZone() + osSection(d);
  if (d.image_subs) html += '<div class="d-note">' + d.image_subs + ' image-based track' + (d.image_subs > 1 ? 's' : '')
    + ' left out: those can only be burned in, from the terminal.</div>';

  if (d.audio.length > 1) {
    const current = pick.audio ?? (d.audio.find(a => a.default) || d.audio[0]).index;
    html += '<div class="d-cap">Audio</div><div class="chips">'
      + d.audio.map(a => '<button class="chip' + (a.index === current ? ' on' : '') + '" data-audio="' + a.index + '">' + esc(a.label) + '</button>').join('')
      + '</div>';
  }

  const busy = status && status.phase === 'loading';
  const dis = busy ? ' disabled' : '';
  const conv = convState(d);
  html += '<div class="actions">';
  if (conv === 'converting') {
    // Already converting: it'll start by itself; offer not waiting, or giving up.
    html += '<div class="d-note conv-note" id="convNote"></div>'
      + '<button class="btn secondary" data-play="' + (d.resume ? 'resume' : 'start') + '" data-now="1"' + dis + '>Play now, without seeking until it\'s done</button>'
      + '<button class="btn ghost" data-cancel="' + d.id + '">Cancel conversion</button>';
  } else {
    const wait = conv === 'needed' ? ' data-wait="1"' : '';
    if (d.resume) {
      html += '<button class="btn primary" data-play="resume"' + wait + dis + '>' + PLAY + ' Resume from ' + fmt(d.resume) + '</button>'
        + '<button class="btn secondary" data-play="start"' + wait + dis + '>Play from the beginning</button>';
    } else {
      html += '<button class="btn primary" data-play="start"' + wait + dis + '>' + PLAY + ' Play</button>';
    }
    if (conv === 'needed') {
      html += '<div class="d-note">The Chromecast can\'t play this file as it is, so it gets converted first. '
        + 'You\'ll see the progress on the library; it starts on the TV by itself when it\'s ready.</div>'
        + '<button class="btn ghost" data-play="' + (d.resume ? 'resume' : 'start') + '" data-now="1"' + dis + '>Play right now instead (no seeking)</button>';
    }
  }
  html += '</div><div class="d-error" id="dErr">' + esc(d.playError || '') + '</div></div>';
  sheetConvKey = conv;
  s.innerHTML = html;
  wireSheet();
  paintWatch();
}

// A movie's sheet opens on its poster over a blurred copy of it, not on a frame.
function sheetArt(d, name) {
  const kind = d.kind || (itemsById[d.id] || {}).kind;
  if (kind !== 'movie' || noPoster.has(d.id)) return art(d.id, name, d.progress);
  return '<div class="art hero" id="hero">'
    + '<img class="backdrop" alt="" src="/poster/' + d.id + '" onerror="heroFallback(\'' + d.id + '\')">'
    + '<img class="cover" alt="" src="/poster/' + d.id + '"></div>';
}
function heroFallback(id) {
  noPoster.add(id);
  const hero = $('hero'), it = itemsById[id] || {};
  if (hero) hero.outerHTML = art(id, it.title, it.progress);
}

function paintWatch() {
  const box = $('watch');
  if (!box || !detail || !detail.kind) return;
  const w = barModel(detail.id, detail, detail.duration);
  if (!w.dur || (!w.pos && !w.conv && !w.watched)) { box.hidden = true; return; }
  box.hidden = false;
  paintBar(box.querySelector('.watch-track'), w);

  let cap = w.watched || w.pos >= 1
    ? '<span><i class="swatch seen"></i>'
      + (w.watched ? 'Watched' : fmt(w.pos) + ' of ' + fmt(w.dur) + ' watched') + '</span>'
    : '';
  if (w.conv) {
    const what = w.conv.mode === 'transcode' ? 'converted' : 'remuxed';
    const text = w.conv.saved ? 'conversion saved, starts instantly'
      : w.conv.queued ? 'conversion queued'
      : w.conv.done ? what + ' to the end'
      : what + ' up to ' + fmt(w.conv.end);
    cap += '<span><i class="swatch conv"></i>' + text + '</span>';
  }
  $('wCap').innerHTML = cap;
}

function wireSheet() {
  const close = $('sheetClose');
  if (close) close.onclick = leaveSheet;
}

function leaveSheet() {
  if (sheetBase) location.hash = sheetBase.kind === 'show' ? 'show/' + sheetBase.id : '';
  else location.hash = '';
}

function closeSheet() {
  sheetToken++;
  detail = null;
  os = {id: null};
  drop = {busy: false, message: '', error: false};
  $('scrim').classList.remove('open');
  document.body.classList.remove('locked');
}

// --- drag & drop of subtitle files ------------------------------------------------
// Dropping anywhere on the sheet works; the visible zone is also a button for
// phones, where there is nothing to drag from.
const SUB_EXT = /\.(srt|ass|ssa|vtt|sub|smi|ssf)$/i;
let drop = {busy: false, message: '', error: false};

function dropZone() {
  const text = drop.busy ? 'Adding…' : drop.message || 'Drop a subtitle file here, or tap to choose one';
  return '<label class="dropzone' + (drop.error ? ' err' : '') + '" id="dropzone">'
    + '<input type="file" id="subFile" multiple accept=".srt,.ass,.ssa,.vtt,.sub,.smi,.ssf" hidden>'
    + esc(text) + '</label>';
}

async function addSubtitleFiles(files) {
  const d = detail;
  if (!d || !d.kind || drop.busy) return;
  const list = Array.from(files);
  if (!list.length) return;
  drop = {busy: true, message: '', error: false};
  paintSheet();
  let added = null, failure = '';
  for (const f of list) {
    if (!SUB_EXT.test(f.name)) { failure = f.name + ' isn\'t a subtitle file.'; continue; }
    try {
      const r = await fetch('/api/item/' + d.id + '/subtitle-file?name=' + encodeURIComponent(f.name),
        {method: 'POST', body: f});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Could not add ' + f.name + '.');
      added = data.file;
    } catch (e) { failure = e.message; }
  }
  drop = {busy: false, message: failure || (list.length > 1 ? 'Subtitles added' : 'Subtitle added'), error: !!failure && !added};
  if (added) {
    // Re-read the tracks and select the one that just arrived.
    try {
      const fresh = await (await fetch('/api/item/' + d.id)).json();
      if (detail && detail.id === d.id) {
        detail = fresh;
        const t = fresh.subs.find(t => t.file === added);
        if (t) pick.sub = t.id;
      }
    } catch (e) {}
  }
  if (detail && detail.id === d.id) paintSheet();
}

// Dropping a file on the page would otherwise make the browser open it.
for (const ev of ['dragover', 'drop']) {
  window.addEventListener(ev, e => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) e.preventDefault(); });
}
let dragDepth = 0;
$('sheet').addEventListener('dragenter', e => {
  if (![...(e.dataTransfer.types || [])].includes('Files')) return;
  dragDepth++; $('sheet').classList.add('dragging');
});
$('sheet').addEventListener('dragleave', () => {
  if (--dragDepth <= 0) { dragDepth = 0; $('sheet').classList.remove('dragging'); }
});
$('sheet').addEventListener('drop', e => {
  dragDepth = 0; $('sheet').classList.remove('dragging');
  if (e.dataTransfer && e.dataTransfer.files.length) addSubtitleFiles(e.dataTransfer.files);
});
document.addEventListener('change', e => {
  if (e.target.id === 'subFile') { addSubtitleFiles(e.target.files); e.target.value = ''; }
});

// --- OpenSubtitles -------------------------------------------------------------
// Results are saved next to the video as <video>.<lang>.srt, so after the first
// download it is just another local subtitle file.
let os = {id: null};

function osSection(d) {
  const cfg = d.opensubtitles || {};
  if (os.id !== d.id) {
    return '<button class="btn ghost os-open" data-os="search">Find subtitles on OpenSubtitles</button>';
  }
  let html = '<div class="os-box">';
  if (!cfg.search) {
    html += '<div class="d-note">Searching OpenSubtitles needs an API key. Put it in '
      + '<code>~/.config/ltc/config.toml</code> under <code>[opensubtitles]</code> (<code>api_key</code>, <code>username</code>, <code>password</code>), '
      + 'or run <code>ltc setup</code>. The account, free at opensubtitles.com, is what downloads count against.</div>';
  } else if (os.loading) {
    html += '<div class="d-note">Searching OpenSubtitles…</div>';
  } else if (os.error) {
    html += '<div class="d-error">' + esc(os.error) + '</div>';
  } else if (!os.results.length) {
    html += '<div class="d-note">Nothing found for this one.</div>';
  } else {
    if (!os.canDownload) html += '<div class="d-note">To download, add your opensubtitles.com <code>username</code> and <code>password</code> to <code>[opensubtitles]</code> in <code>~/.config/ltc/config.toml</code>.</div>';
    html += '<div class="os-list">';
    for (const r of os.results) {
      const tags = [r.downloaded ? '<b>already downloaded</b>' : '', r.hash_match ? '<b>matches this file</b>' : '', r.downloads ? r.downloads.toLocaleString() + ' downloads' : '',
        r.hearing_impaired ? 'hearing impaired' : '', r.machine ? 'machine translated' : ''].filter(Boolean).join(' · ');
      const busy = os.downloading === r.file_id;
      html += '<button class="os-row" data-osfile="' + r.file_id + '" data-oslang="' + esc(r.language) + '"'
        + (os.canDownload && !os.downloading ? '' : ' disabled') + '>'
        + '<span class="os-lang">' + esc(r.language) + '</span>'
        + '<span class="os-text"><span class="os-rel">' + esc(busy ? 'Downloading…' : r.release) + '</span>'
        + '<span class="os-tags">' + tags + '</span></span></button>';
    }
    html += '</div>';
  }
  return html + '</div>';
}

async function osSearch(d) {
  os = {id: d.id, loading: true, results: []};
  paintSheet();
  if (!(d.opensubtitles || {}).search) { os.loading = false; return paintSheet(); }
  try {
    const r = await fetch('/api/item/' + d.id + '/subtitles');
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Search failed.');
    os = {id: d.id, results: data.results, canDownload: data.can_download};
  } catch (e) {
    os = {id: d.id, error: e.message, results: []};
  }
  if (detail && detail.id === d.id) paintSheet();
}

async function osDownload(fileId, lang) {
  const d = detail;
  os.downloading = Number(fileId); os.error = '';
  paintSheet();
  try {
    const r = await fetch('/api/item/' + d.id + '/subtitles', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({file_id: Number(fileId), language: lang})});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Download failed.');
    // Re-read the file's tracks: the new .srt is one of them now. Pick it.
    const fresh = await (await fetch('/api/item/' + d.id)).json();
    const added = fresh.subs.find(t => t.file === data.file);
    detail = fresh;
    if (added) pick.sub = added.id;
    os = {id: null};
  } catch (e) {
    os.downloading = null; os.error = e.message;
  }
  paintSheet();
}

// 'saved' or 'none' (nothing to convert), 'needed' (not converted yet), or
// 'converting' (a conversion is running or queued for it).
let sheetConvKey = '';
function convState(d) {
  if (d.mode !== 'transcode' || d.saved) return 'none';
  const running = status && status.conversions && status.conversions[d.id];
  if (running && running.saved) return 'none';
  if (running || (status && status.pending && status.pending.id === d.id)) return 'converting';
  return 'needed';
}

async function play(mode, wait) {
  const d = detail;
  const body = {id: d.id, sub: pick.sub, start: mode === 'start' ? 0 : null, wait: !!wait};
  if (pick.audio !== null) body.audio = pick.audio;
  try {
    const r = await fetch('/api/play', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Could not start playback.');
    if (wait) {
      // Back to the library, where the bar under its poster shows the conversion.
      status = data;
      leaveSheet();
      poll();
    } else {
      location.href = '/player';
    }
  } catch (e) {
    d.playError = e.message;
    paintSheet();
  }
}

// --- clicks (delegated: the views are re-rendered wholesale) --------------------

document.addEventListener('click', e => {
  const el = e.target.closest('[data-item],[data-season],[data-sub],[data-audio],[data-play],[data-cancel],[data-os],[data-osfile]');
  if (!el) {
    if (e.target === $('scrim')) leaveSheet();
    return;
  }
  if (el.dataset.item) {
    const r = route();
    if (r.kind !== 'item') { listScroll = window.scrollY; lastBase = r.kind + '/' + (r.id || ''); }
    sheetBase = r.kind === 'item' ? sheetBase : r;
    location.hash = 'item/' + el.dataset.item;
  } else if (el.dataset.season) {
    const r = route();
    seasonPick[r.id] = Number(el.dataset.season);
    renderShow(r.id);
  } else if (el.dataset.sub) {
    pick.sub = el.dataset.sub === 'off' ? 'off' : Number(el.dataset.sub);
    paintSheet();
  } else if (el.dataset.audio) {
    pick.audio = Number(el.dataset.audio);
    paintSheet();
  } else if (el.dataset.play) {
    play(el.dataset.play, el.dataset.wait === '1' && el.dataset.now !== '1');
  } else if (el.dataset.os) {
    osSearch(detail);
  } else if (el.dataset.osfile) {
    osDownload(el.dataset.osfile, el.dataset.oslang);
  } else if (el.dataset.cancel) {
    fetch('/api/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: el.dataset.cancel})}).then(poll);
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && route().kind === 'item') leaveSheet();
});
let lastBase = 'home/', listScroll = 0;
window.addEventListener('hashchange', () => {
  const r = route();
  if (r.kind === 'item') return render();
  sheetBase = r;
  render();
  // Closing a sheet returns to the same list, where it was: an empty hash makes
  // the browser jump to the top before this runs, so put the scroll back. Only
  // a real change of view (home <-> a show) starts at the top.
  const key = r.kind + '/' + (r.id || '');
  window.scrollTo(0, key === lastBase ? listScroll : 0);
  lastBase = key;
});

// --- data -----------------------------------------------------------------------

async function loadLibrary(refresh) {
  try {
    const r = await fetch('/api/library' + (refresh ? '?refresh=1' : ''));
    lib = await r.json();
    itemsById = {};
    for (const it of lib.movies.concat(lib.continue, ...lib.shows.map(s => s.episodes))) itemsById[it.id] = it;
    $('root').textContent = lib.root;
    $('err').textContent = '';
    const r0 = route();
    if (r0.kind === 'item' && !sheetBase) {
      // Opened straight on an item (a reload): put its show behind it.
      const owner = lib.shows.find(s => s.episodes.some(e => e.id === r0.id));
      sheetBase = owner ? {kind: 'show', id: owner.id} : {kind: 'home'};
    }
    render();
  } catch (e) {
    $('err').textContent = 'Could not reach ltc. Is it still running?';
  }
}

async function poll() {
  try {
    status = await (await fetch('/api/status')).json();
  } catch (e) {
    status = null; $('led').className = 'dot'; return;
  }
  const s = status;
  $('device').textContent = s.device;
  $('led').className = 'dot' + (s.playing ? ' playing' : s.buffering || s.phase === 'loading' ? ' buffering' : '');

  const bar = $('now');
  const waiting = s.phase === 'idle' && s.pending;
  const active = s.phase !== 'idle' || !!waiting;
  bar.classList.toggle('show', active);
  bar.classList.toggle('waiting', !!waiting);
  if (waiting) {
    const it = s.pending;
    if (bar.dataset.key !== it.id) {
      bar.dataset.key = it.id;
      $('nowArt').innerHTML = artInner(it.id, it.kind === 'episode' ? it.show : it.title, null);
    }
    $('nowTitle').textContent = it.kind === 'episode' ? it.show + ' · ' + it.code : it.title;
    $('nowState').textContent = (convText(it.conversion) || 'Converting') + ' · plays when ready';
    $('nowLed').className = 'dot buffering';
    $('nowCancel').dataset.cancel = it.id;
  } else if (active) {
    const it = s.phase === 'loading' ? s.loading : s.item;
    const key = it ? it.id : '';
    if (bar.dataset.key !== key) {
      bar.dataset.key = key;
      $('nowArt').innerHTML = it ? artInner(it.id, it.kind === 'episode' ? it.show : it.title, null) : '';
    }
    $('nowTitle').textContent = s.phase === 'loading'
      ? (it.kind === 'episode' ? it.show + ' · ' + it.code : it.title)
      : s.title;
    $('nowState').textContent = s.phase === 'loading' ? 'Starting on ' + s.device + '…'
      : (s.buffering ? 'Buffering' : s.playing ? 'Playing' : 'Paused') + ' · ' + fmt(s.position) + ' / ' + fmt(s.duration);
    $('nowLed').className = 'dot' + (s.playing ? ' playing' : ' buffering');
    $('nowToggle').innerHTML = s.playing ? PAUSE : PLAY;
    $('nowToggle').disabled = s.phase !== 'playing';
  }
  $('err').textContent = s.connected === false ? 'Lost the connection to the Chromecast.' : (s.library_error || '');
  if (detail && detail.kind && !detail.loading) {
    if (convState(detail) !== sheetConvKey) paintSheet();
    const busy = s.phase === 'loading';
    document.querySelectorAll('[data-play]').forEach(b => b.disabled = busy);
    paintWatch();
    const note = $('convNote');
    if (note) note.textContent = (convText((s.conversions || {})[detail.id]) || 'Converting')
      + '. It starts on the TV by itself when it\'s ready.';
  }
  paintBars();
}

$('nowToggle').onclick = async () => {
  await fetch('/api/cmd', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: 'toggle'})});
  poll();
};
$('refresh').onclick = () => loadLibrary(true);

let quitArmed = false, quitTimer = null;
$('quit').onclick = async () => {
  if (!quitArmed) {
    quitArmed = true; $('quit').textContent = 'Sure? Quit ltc'; $('quit').classList.add('confirm');
    quitTimer = setTimeout(() => { quitArmed = false; $('quit').textContent = 'Quit ltc'; $('quit').classList.remove('confirm'); }, 3000);
    return;
  }
  clearTimeout(quitTimer);
  try { await fetch('/api/quit', {method: 'POST'}); } catch (e) {}
  $('quit').textContent = 'ltc closed'; $('quit').disabled = true;
};

// Coming back from the remote (or waking the phone): progress has moved on.
document.addEventListener('visibilitychange', () => { if (!document.hidden) { loadLibrary(false); poll(); } });
window.addEventListener('pageshow', e => { if (e.persisted) { loadLibrary(false); poll(); } });

loadLibrary(false);
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


# Appended to webui.PAGE when it is served as /player in library mode. Uses the
# page's own `$` and `cmd`. Adds a way back to the library and a "next episode"
# button, turns "Stop and quit" into "Stop" (ltc keeps running), and sends the
# phone back to the library once nothing is playing.
PLAYER_ADDON = r"""
<style>
  .lib-nav { display: flex; justify-content: space-between; align-items: center; margin: -14px 0 10px; }
  .lib-nav a, .lib-nav button {
    display: inline-flex; align-items: center; min-height: 44px;
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .14em; text-transform: uppercase;
    color: var(--ink-mid); padding: 0; text-decoration: none;
  }
  .lib-nav a:hover, .lib-nav button:hover { color: var(--ink-strong); }
  .lib-nav button { color: var(--accent); }
  .lib-nav button[hidden] { display: none; }
</style>
<script>
(function () {
  const nav = document.createElement('div');
  nav.className = 'lib-nav';
  nav.innerHTML = '<a href="/">&lsaquo;&nbsp; Library</a><button id="libNext" hidden>Next episode &rsaquo;</button>';
  document.querySelector('.col-main').prepend(nav);
  $('libNext').onclick = () => cmd('next');

  // A fresh button drops webui's own handler, which would quit ltc. Deep clone:
  // the square icon and its label come along.
  const old = $('stop');
  const stop = old.cloneNode(true);
  old.replaceWith(stop);
  const label = stop.querySelector('.stop-txt');
  label.textContent = 'Stop'; stop.setAttribute('aria-label', 'Stop'); stop.title = 'Stop';
  let armed = false, timer = null;
  stop.onclick = async () => {
    if (!armed) {
      armed = true; label.textContent = 'Tap again to stop';
      stop.setAttribute('aria-label', 'Tap again to stop'); stop.classList.add('confirm');
      timer = setTimeout(() => {
        armed = false; label.textContent = 'Stop'; stop.setAttribute('aria-label', 'Stop'); stop.classList.remove('confirm');
      }, 3000);
      return;
    }
    clearTimeout(timer);
    try { await fetch('/api/cmd', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cmd: 'stop'})}); } catch (e) {}
    location.href = '/';
  };

  let seen = false;
  async function watch() {
    let s;
    try { s = await (await fetch('/api/status')).json(); } catch (e) { return; }
    if (s.phase === 'loading' && s.loading) {
      const it = s.loading;
      $('title').textContent = it.kind === 'episode' ? it.show + ' · ' + it.code : it.title;
      $('stateWord').textContent = 'Starting';
    }
    if (s.phase !== 'idle') seen = true;
    if (s.library_error) $('err').textContent = s.library_error;
    const next = s.next;
    $('libNext').hidden = !next;
    if (next) $('libNext').innerHTML = 'Next: ' + next.code + ' &rsaquo;';
    // Opened with nothing playing, the last episode just ended, or the load
    // failed: the library page is where the error is shown.
    if (s.phase === 'idle') setTimeout(() => {
      if (!document.hidden) location.href = '/';
    }, seen ? 1500 : 0);
  }
  watch();
  setInterval(watch, 1500);
})();
</script>
"""
