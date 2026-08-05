"""Web control panel. State comes from /api/status, commands go to /api/cmd.

Single self-contained page: opens on the machine running ltc, and on any
phone on the same LAN, where it's the primary remote control. Used
one-handed, in the dark, in short bursts — so subtitle sync (the reason
this program exists over just using VLC) gets more visual weight than a
typical secondary control would.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="mobile-web-app-capable" content="yes">
<title>Chromecast</title>
<style>
  :root {
    --bg:          #101014;
    --panel:       #191920;
    --panel-alt:   #22222b;
    --ink:         #e9e6df;
    --ink-strong:  #f3f0ea;
    --ink-dim:     #6b6660;
    --ink-mid:     #8d877e;
    --ink-faint:   #4f4b46;
    --panel-ink:      #8f8b98;
    --panel-ink-dim:  #6f6b78;
    --panel-ink-chip: #b6b2bd;
    --accent:      #a08a5e;
    --led:         #7a9478;
    --led-wait:    #d6a13c;
    --danger:      #c26a5e;
    --border:      #2b2823;
    --border-strong: #3c3934;
    --chip-ink:    #101014;
    --chip-active-bg: #e9e6df;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f2ee; --panel: #e9e6e0; --panel-alt: #dcd8d1;
      --ink: #1b1c1e; --ink-strong: #16171a; --ink-dim: #75797e;
      --ink-mid: #8b8781; --ink-faint: #9d9891;
      --panel-ink: #6d6a65; --panel-ink-dim: #8b877e; --panel-ink-chip: #4a4d51;
      --accent: #8a6a25; --led: #4c7a4c; --led-wait: #b8892e; --danger: #a4433a;
      --border: #d8d4cd; --border-strong: #cdc8c0; --chip-ink: #16171a; --chip-active-bg: #e9e6df;
    }
  }
  :root[data-theme="dark"] {
    --bg: #101014; --panel: #191920; --panel-alt: #22222b;
    --ink: #e9e6df; --ink-strong: #f3f0ea; --ink-dim: #6b6660;
    --ink-mid: #8d877e; --ink-faint: #4f4b46;
    --panel-ink: #8f8b98; --panel-ink-dim: #6f6b78; --panel-ink-chip: #b6b2bd;
    --accent: #a08a5e; --led: #7a9478; --led-wait: #d6a13c; --danger: #c26a5e;
    --border: #2b2823; --border-strong: #3c3934; --chip-ink: #101014; --chip-active-bg: #e9e6df;
  }
  :root[data-theme="light"] {
    --bg: #f4f2ee; --panel: #e9e6e0; --panel-alt: #dcd8d1;
    --ink: #1b1c1e; --ink-strong: #16171a; --ink-dim: #75797e;
    --ink-mid: #8b8781; --ink-faint: #9d9891;
    --panel-ink: #6d6a65; --panel-ink-dim: #8b877e; --panel-ink-chip: #4a4d51;
    --accent: #8a6a25; --led: #4c7a4c; --led-wait: #b8892e; --danger: #a4433a;
    --border: #d8d4cd; --border-strong: #cdc8c0; --chip-ink: #16171a; --chip-active-bg: #e9e6df;
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    overflow-x: hidden;
  }
  button { font: inherit; color: inherit; background: none; border: none; cursor: pointer; }
  button:focus-visible { outline: 2px solid var(--ink-mid); outline-offset: 2px; }

  /* --- theme toggle: lives in the footer, not pinned to a corner, so it can't
     collide with the subtitle column's collapse chevron --- */
  #themeToggle {
    font: 400 11px/1 system-ui, sans-serif; color: var(--ink-faint);
    border: 1px solid var(--border); border-radius: 7px; padding: 6px 10px;
  }

  /* --- mobile-to-desktop nudge ---
     points straight up at the address bar. Centered because that's where the
     omnibox sits in Brave/Chrome/Edge, and it stays under the wide URL field
     in Firefox too — anchoring it left missed the bar entirely. */
  .hint {
    position: fixed; top: 4px; left: 50%; transform: translateX(-50%);
    width: 240px; text-align: center;
    color: var(--danger); pointer-events: none; z-index: 5;
  }
  .hint svg { display: block; width: 118px; height: 84px; margin: 0 auto; }
  .hint p { margin: 2px 0 0; font-size: 1.12rem; line-height: 1.35; }
  @media (max-width: 1180px), (pointer: coarse) { .hint { display: none; } }

  /* --- layout shell --- */
  .app { display: flex; align-items: stretch; min-height: 100vh; }
  .col-main {
    flex: 1; min-width: 0;
    padding: 34px 22px 120px;
    max-width: 640px; margin: 0 auto;
    display: flex; flex-direction: column;
    /* only the sidebar stretches to full height; if this column stretched too,
       the flexible spacer would blow the gap between the header and the
       transport controls open to half the screen */
    align-self: flex-start;
  }
  @media (min-width: 760px) {
    .app { min-height: 100vh; }
    /* the sidebar is a fixed-width rail, but the main column would otherwise
       stretch edge-to-edge on a wide monitor and leave the controls stranded
       in a mostly-empty pane — cap it and center it in the space it has */
    .col-main { padding: 44px 40px; max-width: 820px; margin: 0 auto; }
  }
  /* the phone-hint arrow only shows above 1180px (see .hint below) — give the
     header room so it doesn't sit on top of the title at that width */
  @media (min-width: 1180px) {
    .col-main { padding-top: 176px; }
  }

  /* --- header --- */
  .eyebrow-row { display: flex; align-items: center; gap: 10px; }
  .eyebrow {
    font: 400 11px/1 system-ui, sans-serif; letter-spacing: .18em;
    text-transform: uppercase; color: var(--ink-dim);
  }
  .dot { width: 5px; height: 5px; border-radius: 50%; background: var(--ink-faint); flex: none; }
  .dot.playing { background: var(--led); }
  .dot.buffering { background: var(--led-wait); }
  .eyebrow-desktop { display: none; }
  @media (min-width: 760px) {
    .eyebrow-mobile { display: none; }
    .eyebrow-desktop { display: flex; align-items: center; gap: 10px; }
  }

  .title {
    font: 300 32px/1.1 system-ui, -apple-system, sans-serif; letter-spacing: -.03em;
    margin: 16px 0 0; color: var(--ink-strong);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  @media (min-width: 760px) { .title { font-size: 46px; margin-top: 22px; } }

  .status-row {
    display: flex; align-items: center; gap: 9px; margin-top: 14px;
    font: 400 12px/1 system-ui, sans-serif; color: var(--ink-dim);
  }
  .status-row .sep { width: 3px; height: 3px; border-radius: 50%; background: var(--border-strong); }
  @media (min-width: 760px) { .status-row .state-word, .status-row .sep, .status-row .dot { display: none; } }

  .mode-btn {
    font: 500 12px/1 system-ui, sans-serif; color: var(--accent);
  }
  @media (min-width: 760px) {
    .mode-btn {
      font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
      border: 1px solid var(--border-strong); border-radius: 3px; padding: 6px 9px;
    }
  }
  .mode-detail {
    margin-top: 12px; font: 11px/1.7 system-ui, sans-serif; color: var(--ink-dim);
    border-left: 1px solid var(--border); padding-left: 12px;
  }
  .mode-detail:empty { display: none; }
  @media (min-width: 760px) {
    .mode-detail {
      border: none; padding-left: 0; margin: 4px 0 0;
      font-family: ui-monospace, Menlo, Consolas, monospace; line-height: 1.8;
    }
  }
  .mode-row { display: flex; flex-direction: column; }
  @media (min-width: 760px) {
    .mode-row { flex-direction: row; align-items: flex-start; gap: 22px; margin-top: 20px; }
    .mode-row .mode-detail { flex: 1; }
  }

  .spacer { flex: 1; min-height: 24px; }

  /* --- bars (position, volume) --- */
  .times {
    display: flex; justify-content: space-between; margin-top: 30px;
    font: 13px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-dim);
  }
  .times .pos { color: var(--ink-strong); }
  .bar {
    position: relative; height: 20px; display: flex; align-items: center;
    margin-top: 9px; cursor: pointer; touch-action: none;
  }
  .bar .track { position: absolute; left: 0; right: 0; height: 2px; background: var(--panel-alt); }
  .bar .fill { position: absolute; left: 0; height: 2px; background: var(--accent); width: 0%; pointer-events: none; }
  .bar.vol .fill { background: var(--ink-dim); }
  .bar .thumb {
    position: absolute; width: 11px; height: 11px; border-radius: 50%;
    background: var(--ink-strong); left: 0%; margin-left: -5.5px;
    opacity: 0; transition: opacity .12s;
  }
  .bar:hover .thumb, .bar.dragging .thumb { opacity: 1; }

  /* --- transport + volume row --- */
  .controls-row { display: flex; align-items: center; flex-wrap: wrap; margin-top: 26px; gap: 4px; }
  .transport { display: flex; align-items: center; gap: 4px; }
  .skip {
    width: 52px; height: 52px; font: 400 14px/1 ui-monospace, Menlo, Consolas, monospace;
    color: var(--ink);
  }
  .skip.far { color: var(--ink-mid); font-size: 13px; }
  #toggle {
    width: 74px; height: 74px; border-radius: 50%; border: 1px solid var(--border-strong);
    display: grid; place-items: center; flex: none; margin: 0 2px;
  }
  #toggle svg { width: 26px; height: 26px; fill: var(--ink-strong); }
  @media (min-width: 760px) {
    .skip { width: 60px; }
    #toggle { width: 66px; height: 66px; }
    #toggle svg { width: 24px; height: 24px; }
  }

  .vol { display: flex; align-items: center; gap: 12px; margin-top: 22px; flex: 1 1 100%; }
  .vol-label {
    font: 400 11px/1 system-ui, sans-serif; letter-spacing: .14em;
    text-transform: uppercase; color: var(--ink-dim); flex: none;
  }
  .vol .bar { flex: 1; margin-top: 0; }
  #mute {
    font: 400 12px/1 system-ui, sans-serif; color: var(--ink-mid);
    border: 1px solid var(--border); border-radius: 9px; padding: 9px 12px; flex: none;
  }
  @media (min-width: 760px) {
    .vol { margin-top: 0; margin-left: auto; flex: 0 0 auto; }
    .vol .bar { width: 120px; flex: none; }
  }

  /* --- footer: shortcuts (desktop) + stop + theme --- */
  .footer-row {
    display: flex; align-items: center; gap: 16px; margin-top: 22px; padding-top: 4px;
  }
  .shortcuts { display: none; }
  @media (min-width: 760px) {
    .footer-row { margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--border); gap: 26px; }
    .shortcuts { display: flex; gap: 20px; font: 11px/1 system-ui, sans-serif; color: var(--ink-faint); }
    .shortcuts kbd { font-family: ui-monospace, Menlo, Consolas, monospace; color: var(--ink-dim); font-style: normal; }
  }
  /* same shape as #themeToggle: a bare text label next to a bordered pill read
     as two different kinds of control sitting on mismatched baselines */
  #stop {
    font: 400 11px/1 system-ui, sans-serif; color: var(--ink-faint);
    border: 1px solid var(--border); border-radius: 7px; padding: 6px 10px;
    margin-right: auto;
  }
  /* desktop puts the shortcut legend on the left, so stop moves over to the right */
  @media (min-width: 760px) {
    .shortcuts { margin-right: auto; }
    #stop { margin-right: 0; order: 3; }
    #themeToggle { order: 2; }
  }
  #stop.confirm { color: var(--danger); }
  #err {
    margin-top: 14px; font-size: .78rem; color: var(--danger);
    border: 1px solid currentColor; border-radius: 8px; padding: 9px 11px;
  }
  #err:empty { display: none; }

  /* ============================================================ */
  /* subtitle panel — the core feature, so it gets its own surface */
  /* ============================================================ */

  /* mobile: draggable bottom sheet */
  .subs-sheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 10;
    background: var(--panel); border-radius: 22px 22px 0 0;
    padding: 12px 22px 24px; box-shadow: 0 -14px 34px rgba(0, 0, 0, .35);
    touch-action: none; will-change: transform;
  }
  .sheet-handle { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; cursor: grab; }
  .grip { width: 34px; height: 4px; border-radius: 2px; background: var(--border-strong); flex: none; }
  .sheet-handle .label { font: 500 13px/1 system-ui, sans-serif; color: var(--panel-ink); flex: none; }
  /* a subtitle filename can be far longer than the row — clip it rather than
     letting it shove the offset off the edge */
  .sheet-handle .lang {
    font: 400 12px/1.2 system-ui, sans-serif; color: var(--panel-ink-dim);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
  }
  .sheet-handle .delay {
    font: 400 15px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-strong); flex: none;
  }

  /* every key shares the row proportionally and may shrink; fixed widths made
     the last one overflow the screen edge on a narrow phone */
  .sync-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .sync-btn {
    flex: 1 1 0; min-width: 0; height: 56px; background: var(--panel-alt); border-radius: 14px;
    color: var(--ink); font: 400 13px/1 ui-monospace, Menlo, Consolas, monospace;
  }
  .sync-reset {
    flex: 1.6 1 0; min-width: 0; height: 56px; color: var(--ink-strong);
    font: 300 clamp(20px, 6.4vw, 28px)/1 ui-monospace, Menlo, Consolas, monospace;
    letter-spacing: -.02em;
  }
  .track-list.chips { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }
  .chip {
    font: 400 13px/1 system-ui, sans-serif; border-radius: 20px; padding: 10px 14px;
    color: var(--panel-ink-chip); background: var(--panel-alt);
  }
  .chip.on { color: var(--chip-ink); background: var(--chip-active-bg); }
  .track-empty { font: 12px/1.5 system-ui, sans-serif; color: var(--panel-ink-dim); margin-top: 12px; }

  /* desktop: static collapsible column, no drag */
  .subs-col { display: none; }
  @media (min-width: 760px) {
    .subs-sheet { display: none; }
    .subs-col {
      display: flex; flex-direction: column; flex: 0 0 320px;
      background: var(--panel); padding: 36px 26px 28px; overflow: hidden;
      transition: flex-basis .16s ease;
      /* the page can be taller than the viewport, and a panel that stops
         partway down reads as a rendering glitch — run it the whole way */
      min-height: 100vh;
    }
    .subs-col.closed { flex-basis: 78px; }
    .subs-col .open-content { display: flex; flex-direction: column; flex: 1; min-height: 0; }
    .subs-col.closed .open-content { display: none; }
    .subs-col .rail { display: none; }
    .subs-col.closed .rail {
      display: flex; flex-direction: column; align-items: center; gap: 24px; margin: 0 auto;
    }
  }
  .subs-head { display: flex; align-items: center; gap: 10px; }
  .subs-head .label { font: 500 15px/1 system-ui, sans-serif; color: var(--panel-ink); flex: 1; }
  .chevron {
    width: 30px; height: 30px; border-radius: 8px; border: 1px solid var(--border);
    color: var(--panel-ink-dim); font: 400 14px/1 system-ui, sans-serif;
  }
  .rail .chevron { border: none; background: var(--panel-alt); width: 34px; height: 34px; border-radius: 9px; color: var(--panel-ink-chip); }
  .rail .delay { font: 400 15px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-strong); writing-mode: vertical-rl; }
  .rail .tag { font: 500 13px/1 system-ui, sans-serif; color: var(--panel-ink-dim); writing-mode: vertical-rl; letter-spacing: .06em; }

  .offset-display { text-align: center; margin-top: 30px; }
  .offset-display .num {
    font: 200 62px/1 system-ui, -apple-system, sans-serif; letter-spacing: -.045em;
    color: var(--ink-strong);
  }
  .offset-display .cap {
    font: 400 10px/1 system-ui, sans-serif; letter-spacing: .18em; text-transform: uppercase;
    color: var(--panel-ink-dim); margin-top: 12px;
  }
  .sync-row.desktop { margin-top: 26px; gap: 8px; }
  .sync-row.desktop .sync-btn { flex: 1; height: 56px; border-radius: 13px; }
  .track-cap {
    font: 400 10px/1 system-ui, sans-serif; letter-spacing: .18em; text-transform: uppercase;
    color: var(--panel-ink-dim); margin: 30px 0 12px;
  }
  .track-list.rows { display: flex; flex-direction: column; gap: 6px; overflow-y: auto; }
  .row-chip {
    font: 400 13px/1 system-ui, sans-serif; border-radius: 9px; padding: 12px 14px; text-align: left;
    color: var(--panel-ink-chip); background: var(--panel-alt);
  }
  .row-chip.on { color: var(--chip-ink); background: var(--chip-active-bg); }
</style>
</head>
<body>

<aside class="hint" id="hint">
  <svg viewBox="0 0 118 84" aria-hidden="true">
    <path d="M30 80 C 33 52, 42 26, 56 12" fill="none" stroke="currentColor" stroke-width="7" stroke-linecap="round"/>
    <path d="M60 2 L40 16 L66 27 Z" fill="currentColor"/>
  </svg>
  <p>Try using it<br>on your phone</p>
</aside>

<main class="app">
  <div class="col-main">
    <div class="eyebrow-row eyebrow-mobile">
      <span class="eyebrow" id="device">…</span>
    </div>
    <div class="eyebrow-row eyebrow-desktop">
      <span class="dot" id="led2"></span>
      <span class="eyebrow" id="deviceState">…</span>
    </div>

    <h1 class="title" id="title">…</h1>

    <div class="mode-row">
      <div class="status-row">
        <span class="dot" id="led"></span>
        <span class="state-word" id="stateWord">—</span>
        <span class="sep"></span>
        <button class="mode-btn" id="modeBtn"></button>
      </div>
      <div class="mode-detail" id="modeDetail"></div>
    </div>

    <div class="spacer"></div>

    <div class="times"><span class="pos" id="pos">0:00</span><span id="dur">0:00</span></div>
    <div class="bar" id="seekBar">
      <div class="track"></div><div class="fill" id="seekFill"></div><div class="thumb" id="seekThumb"></div>
    </div>

    <div class="controls-row">
      <div class="transport">
        <button class="skip far" id="back30">&minus;30</button>
        <button class="skip" id="back10">&minus;10</button>
        <button id="toggle"></button>
        <button class="skip" id="fwd10">+10</button>
        <button class="skip far" id="fwd30">+30</button>
      </div>
      <div class="vol">
        <span class="vol-label">Vol</span>
        <div class="bar vol" id="volBar"><div class="track"></div><div class="fill" id="volFill"></div><div class="thumb" id="volThumb"></div></div>
        <button id="mute">Mute</button>
      </div>
    </div>

    <div class="footer-row">
      <div class="shortcuts">
        <span><kbd>space</kbd> play</span>
        <span><kbd>&larr; &rarr;</kbd> 10s</span>
        <span><kbd>&darr; &uarr;</kbd> 30s</span>
        <span><kbd>[ ]</kbd> sync</span>
      </div>
      <button id="stop">Stop and quit</button>
      <button id="themeToggle"></button>
    </div>
    <div id="err"></div>
  </div>

  <!-- mobile: draggable bottom sheet -->
  <div class="subs-sheet" id="sheet">
    <div class="sheet-handle" id="sheetHandle">
      <div class="grip"></div>
      <span class="label">Subtitles</span>
      <span style="flex:1"></span>
      <span class="lang" id="sheetLang"></span>
      <span class="delay" id="sheetDelay">0.0s</span>
    </div>
    <div class="sync-row">
      <button class="sync-btn" data-nudge="-0.5">&minus;.5</button>
      <button class="sync-btn" data-nudge="-0.1">&minus;.1</button>
      <button class="sync-reset" id="sheetReset">0.0s</button>
      <button class="sync-btn" data-nudge="0.1">+.1</button>
      <button class="sync-btn" data-nudge="0.5">+.5</button>
    </div>
    <div class="track-list chips" id="chipsA"></div>
  </div>

  <!-- desktop: static collapsible column -->
  <aside class="subs-col" id="subsCol">
    <div class="rail">
      <button class="chevron" id="railOpen" aria-label="Open subtitles">&lsaquo;</button>
      <span class="delay" id="railDelay">0.0s</span>
      <span class="tag" id="railTag">Subtitles</span>
    </div>
    <div class="open-content">
      <div class="subs-head">
        <span class="label">Subtitles</span>
        <button class="chevron" id="railClose" aria-label="Collapse subtitles">&rsaquo;</button>
      </div>
      <div class="offset-display">
        <button class="num" id="colReset">0.0s</button>
        <div class="cap">Offset &middot; click to reset</div>
      </div>
      <div class="sync-row desktop">
        <button class="sync-btn" data-nudge="-0.5">&minus;0.5</button>
        <button class="sync-btn" data-nudge="-0.1">&minus;0.1</button>
        <button class="sync-btn" data-nudge="0.1">+0.1</button>
        <button class="sync-btn" data-nudge="0.5">+0.5</button>
      </div>
      <div class="track-cap">Track</div>
      <div class="track-list rows" id="rowsC"></div>
    </div>
  </aside>
</main>

<script>
const $ = id => document.getElementById(id);
const PLAY  = '<svg viewBox="0 0 24 24"><path d="M8 5.2v13.6L19 12z"/></svg>';
const PAUSE = '<svg viewBox="0 0 24 24"><path d="M7 5h3.4v14H7zm6.6 0H17v14h-3.4z"/></svg>';

let state = null;
let seekDrag = false, seekLocal = 0;
let volDrag = false, volLocal = 0;
let localPos = 0, localAt = 0;
let modeOpen = false;

const fmt = s => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
};
const fmtDelay = d => (d < 0 ? '−' : '+') + Math.abs(d).toFixed(1) + 's';
const esc = t => String(t).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));

async function cmd(c, value) {
  try {
    const r = await fetch('/api/cmd', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cmd: c, value})
    });
    apply(await r.json());
  } catch (e) { /* mid-restart of the stream; next poll recovers */ }
}

async function poll() {
  try { apply(await (await fetch('/api/status')).json()); }
  catch (e) { $('led').className = 'dot'; $('led2').className = 'dot'; }
}

let lastSubsKey = '';
function renderSubs(s) {
  const key = JSON.stringify([s.subs, s.active_sub]);
  if (key === lastSubsKey) return;
  lastSubsKey = key;

  const first = s.subs[0];
  $('sheetLang').textContent = first ? first.label : 'None';
  $('railTag').textContent = 'Subtitles' + (first ? ' · ' + first.label : '');

  const chips = $('chipsA'), rows = $('rowsC');
  chips.innerHTML = ''; rows.innerHTML = '';

  if (!s.subs.length) {
    const note1 = document.createElement('div');
    note1.className = 'track-empty';
    note1.textContent = 'This file has no text subtitles.';
    chips.appendChild(note1);
    rows.appendChild(note1.cloneNode(true));
    sheetCtl.settle();
    return;
  }

  const make = (t, cls, on) => {
    const b = document.createElement('button');
    b.className = cls + (on ? ' on' : '');
    b.textContent = t.label;
    b.onclick = () => cmd('sub', t.id);
    return b;
  };
  for (const t of s.subs) {
    chips.appendChild(make(t, 'chip', s.active_sub === t.id));
    rows.appendChild(make(t, 'row-chip', s.active_sub === t.id));
  }
  const offChip = document.createElement('button');
  offChip.className = 'chip' + (s.active_sub === null ? ' on' : '');
  offChip.textContent = 'Off';
  offChip.onclick = () => cmd('sub', 'off');
  chips.appendChild(offChip);
  const offRow = offChip.cloneNode(true);
  offRow.onclick = () => cmd('sub', 'off');
  rows.appendChild(offRow);
  sheetCtl.settle();
}

function apply(s) {
  if (!s || !s.title) return;
  state = s;
  localPos = s.position; localAt = performance.now();

  $('title').textContent = s.title;
  $('device').textContent = s.device;
  const stateWord = s.buffering ? 'Buffering' : s.playing ? 'Playing' : 'Paused';
  $('stateWord').textContent = stateWord;
  $('deviceState').textContent = stateWord + ' on ' + s.device;

  const dotClass = 'dot' + (s.playing ? ' playing' : s.buffering ? ' buffering' : '');
  $('led').className = dotClass;
  $('led2').className = dotClass;
  $('toggle').innerHTML = s.playing ? PAUSE : PLAY;

  const modeLabel = {direct: 'Direct', remux: 'Remuxing', transcode: 'Transcoding'}[s.mode] || s.mode;
  $('modeBtn').textContent = modeLabel;
  $('modeDetail').innerHTML = modeOpen ? (s.mode_detail || []).map(esc).join('<br>') : '';

  $('err').textContent = s.error || '';

  $('dur').textContent = fmt(s.duration);

  const delayTxt = fmtDelay(s.sub_delay);
  for (const id of ['sheetDelay', 'sheetReset', 'railDelay', 'colReset'])
    $(id).textContent = delayTxt;

  if (!volDrag) {
    volLocal = s.muted ? 0 : s.volume;
    paintVol();
  }
  $('mute').textContent = s.muted ? 'Unmute' : 'Mute';

  renderSubs(s);
  tick();
}

function tick() {
  if (!state) return;
  let p = localPos;
  if (state.playing) p += (performance.now() - localAt) / 1000;
  p = Math.min(p, state.duration || p);
  $('pos').textContent = fmt(p);
  if (!seekDrag && state.duration) { seekLocal = p / state.duration; paintSeek(); }
}

function paintSeek() {
  const pct = Math.max(0, Math.min(1, seekLocal)) * 100 + '%';
  $('seekFill').style.width = pct; $('seekThumb').style.left = pct;
}
function paintVol() {
  const pct = Math.max(0, Math.min(1, volLocal)) * 100 + '%';
  $('volFill').style.width = pct; $('volThumb').style.left = pct;
}

function barDrag(bar, onMove, onCommit) {
  let active = false;
  const ratio = e => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  };
  bar.addEventListener('pointerdown', e => {
    active = true; bar.classList.add('dragging');
    bar.setPointerCapture(e.pointerId);
    onMove(ratio(e));
  });
  bar.addEventListener('pointermove', e => { if (active) onMove(ratio(e)); });
  bar.addEventListener('pointerup', e => {
    if (!active) return;
    active = false; bar.classList.remove('dragging');
    onCommit(ratio(e));
  });
}

barDrag($('seekBar'),
  r => { seekDrag = true; seekLocal = r; paintSeek(); $('pos').textContent = fmt(r * (state?.duration || 0)); },
  r => { seekDrag = false; cmd('seek', r * (state?.duration || 0)); });

barDrag($('volBar'),
  r => { volDrag = true; volLocal = r; paintVol(); },
  r => { volDrag = false; cmd('volume', r); });

$('toggle').onclick = () => cmd('toggle');
$('back30').onclick = () => cmd('skip', -30);
$('back10').onclick = () => cmd('skip', -10);
$('fwd10').onclick  = () => cmd('skip', 10);
$('fwd30').onclick  = () => cmd('skip', 30);
$('mute').onclick   = () => cmd('mute', !state?.muted);

for (const b of document.querySelectorAll('[data-nudge]'))
  b.onclick = () => cmd('nudge_delay', parseFloat(b.dataset.nudge));
$('sheetReset').onclick = () => cmd('delay', 0);
$('colReset').onclick   = () => cmd('delay', 0);

$('modeBtn').onclick = () => {
  modeOpen = !modeOpen;
  $('modeDetail').innerHTML = modeOpen && state ? (state.mode_detail || []).map(esc).join('<br>') : '';
};

let stopArmed = false, stopTimer = null;
$('stop').onclick = () => {
  if (!stopArmed) {
    stopArmed = true;
    $('stop').textContent = 'Sure? Stop and quit';
    $('stop').classList.add('confirm');
    stopTimer = setTimeout(() => {
      stopArmed = false; $('stop').textContent = 'Stop and quit'; $('stop').classList.remove('confirm');
    }, 3000);
    return;
  }
  clearTimeout(stopTimer);
  cmd('quit');
  document.querySelector('.app').innerHTML =
    '<div class="col-main"><p style="margin-top:40px;color:var(--ink-dim)">Done — you can close this tab.</p></div>';
  const hintEl = $('hint'); if (hintEl) hintEl.remove();
};

/* --- mobile sheet: drag to open/collapse, tap the handle to toggle ---
   scrollHeight isn't known until the track chips are populated by the first
   /api/status response, so the initial collapse has to wait for that rather
   than running at parse time against an empty sheet. */
const sheetCtl = (function () {
  const PEEK = 62;
  const sheet = $('sheet'), handle = $('sheetHandle');
  let offset = 0, drag = null, touched = false;

  const maxOffset = () => Math.max(0, sheet.scrollHeight - PEEK);
  const paint = () => { sheet.style.transform = 'translateY(' + offset + 'px)'; };

  window.addEventListener('resize', () => { offset = Math.min(offset, maxOffset()); paint(); });

  handle.addEventListener('pointerdown', e => {
    if (e.target.closest('button')) return;
    drag = { y: e.clientY, base: offset, moved: false };
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener('pointermove', e => {
    if (!drag) return;
    const dy = e.clientY - drag.y;
    if (Math.abs(dy) > 3) drag.moved = true;
    offset = Math.max(0, Math.min(maxOffset(), drag.base + dy));
    paint();
  });
  handle.addEventListener('pointerup', () => {
    if (!drag) return;
    touched = true;
    const M = maxOffset();
    const open = drag.moved ? offset < M / 2 : drag.base > M / 2;
    drag = null;
    offset = open ? 0 : M;
    paint();
  });

  return {
    settle() { if (!touched) { offset = maxOffset(); paint(); } },
  };
})();

/* --- desktop column: chevron collapse, no drag --- */
$('railClose').onclick = () => $('subsCol').classList.add('closed');
$('railOpen').onclick  = () => $('subsCol').classList.remove('closed');

/* --- theme: system default, with a manual override kept in localStorage --- */
(function () {
  const KEY = 'ltc-theme';
  const applyTheme = t => {
    if (t) document.documentElement.setAttribute('data-theme', t);
    else document.documentElement.removeAttribute('data-theme');
    const dark = t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    $('themeToggle').textContent = dark ? 'Light' : 'Dark';
  };
  const saved = localStorage.getItem(KEY);
  applyTheme(saved);
  $('themeToggle').onclick = () => {
    const current = document.documentElement.getAttribute('data-theme');
    const dark = current ? current === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = dark ? 'light' : 'dark';
    localStorage.setItem(KEY, next);
    applyTheme(next);
  };
})();

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON') return;
  const keys = {
    ' ': () => cmd('toggle'), ArrowLeft: () => cmd('skip', -10),
    ArrowRight: () => cmd('skip', 10), ArrowDown: () => cmd('skip', -30),
    ArrowUp: () => cmd('skip', 30),
    '[': () => cmd('nudge_delay', -0.1), ']': () => cmd('nudge_delay', 0.1),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

if (matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0) {
  const hintEl = $('hint'); if (hintEl) hintEl.remove();
}

modeOpen = matchMedia('(min-width: 760px)').matches;
$('toggle').innerHTML = PLAY;
setInterval(poll, 1000);
setInterval(tick, 250);
poll();
</script>
</body>
</html>
"""
