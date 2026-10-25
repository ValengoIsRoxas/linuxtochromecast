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
  /* Text tokens (ink-*, panel-ink-*) all clear 4.5:1 on the surface they sit
     on; --ctl-line clears 3:1 so outlined controls read in a dark room.
     --ink-faint and --border* are decorative only, never used for text.
     The light theme is the dark one's own family: the same cool hue at low
     chroma, with the ochre accent as the single warm note. */
  :root {
    --bg:          #101014;
    --panel:       #191920;
    --panel-alt:   #22222b;
    --ink:         #e9e6df;
    --ink-strong:  #f3f0ea;
    --ink-mid:     #a8a399;
    --ink-dim:     #8f8a82;
    --ink-faint:   #4f4b46;
    --panel-ink:      #a5a1ae;
    --panel-ink-dim:  #908c9a;
    --panel-ink-chip: #c4c0cc;
    --accent:      #a08a5e;
    --led:         #7a9478;
    --led-wait:    #d6a13c;
    --danger:      #c26a5e;
    --border:      #2b2823;
    --border-strong: #3c3934;
    --ctl-line:    #6f6b64;
    --track:       #3a3a45;
    --chip-ink:    #101014;
    --chip-active-bg: #e9e6df;
    --focus:       var(--ink);
    --ease:        cubic-bezier(.22, 1, .36, 1);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #eceff3; --panel: #e1e5eb; --panel-alt: #d3d8e0;
      --ink: #1a1c22; --ink-strong: #121419; --ink-mid: #4d525c; --ink-dim: #585d68;
      --ink-faint: #9aa0ab;
      --panel-ink: #4d525c; --panel-ink-dim: #565b66; --panel-ink-chip: #2f333b;
      --accent: #76591b; --led: #4c7a4c; --led-wait: #b8892e; --danger: #a4433a;
      --border: #cbd0d9; --border-strong: #b9bfca; --ctl-line: #7b818d; --track: #c3c9d3;
      --chip-ink: #eceff3; --chip-active-bg: #1a1c22;
    }
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { min-height: 100%; scrollbar-color: var(--border-strong) transparent; }
  /* overflow-x: clip, not hidden: hidden would turn body into a scroll container */
  body {
    margin: 0; min-height: 100dvh; background: var(--bg); color: var(--ink);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    overflow-x: clip;
  }
  ::selection { background: var(--accent); color: var(--bg); }
  button { font: inherit; color: inherit; background: none; border: none; cursor: pointer; }
  button:active:not(:disabled) { opacity: .6; }
  button:disabled { opacity: .38; cursor: default; }
  button:focus-visible, .bar:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

  /* --- mobile-to-desktop nudge ---
     points straight up at the address bar. Centered because that's where the
     omnibox sits in Brave/Chrome/Edge, and it stays under the wide URL field
     in Firefox too — anchoring it left missed the bar entirely. */
  .hint {
    position: fixed; top: 4px; left: 50%; transform: translateX(-50%);
    width: 240px; text-align: center;
    color: var(--accent); pointer-events: none; z-index: 5;
  }
  .hint svg { display: block; width: 118px; height: 84px; margin: 0 auto; }
  .hint p { margin: 2px 0 0; font-size: 1.12rem; line-height: 1.35; }
  @media (max-width: 1180px), (pointer: coarse) { .hint { display: none; } }

  /* --- layout shell ---
     Phone: the column is a full-height stack. The flexible .spacer pushes the
     position bar, transport, volume and footer to the bottom, directly above
     the subtitle sheet, so every control sits in thumb reach. The bottom
     padding reserves the collapsed sheet's height (--sheet-vis, measured from
     JS). An opened sheet slides over the controls instead of moving them, so
     nothing jumps under the thumb. */
  .app { display: flex; align-items: stretch; min-height: 100dvh; }
  .col-main {
    flex: 1; min-width: 0; align-self: stretch; min-height: 100dvh;
    padding: 34px 22px calc(var(--sheet-vis, 150px) + 20px + env(safe-area-inset-bottom));
    max-width: 640px; margin: 0 auto;
    display: flex; flex-direction: column;
  }
  @media (max-width: 340px) { .col-main { padding-left: 16px; padding-right: 16px; } }
  @media (min-width: 760px) {
    .app { min-height: 100vh; }
    /* the sidebar is a fixed-width rail, but the main column would otherwise
       stretch edge-to-edge on a wide monitor and leave the controls stranded
       in a mostly-empty pane — cap it and center it in the space it has.
       Only the sidebar stretches to full height here; if this column stretched
       too, the flexible spacer would blow the gap between the header and the
       transport controls open to half the screen. */
    .col-main { padding: 44px 40px; max-width: 820px; margin: 0 auto; align-self: flex-start; min-height: 0; }
  }
  /* the phone-hint arrow only shows above 1180px (see .hint below) — give the
     header room so it doesn't sit on top of the title at that width */
  @media (min-width: 1180px) {
    .col-main { padding-top: 176px; }
  }

  /* --- header --- */
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-faint); flex: none; }
  .dot.playing { background: var(--led); }
  .dot.buffering { background: var(--led-wait); }

  /* two lines, then an ellipsis; the full title is also the tooltip */
  .title {
    font: 300 clamp(26px, 8.2vw, 32px)/1.12 system-ui, -apple-system, sans-serif; letter-spacing: -.03em;
    margin: 0; color: var(--ink-strong);
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
    overflow: hidden; overflow-wrap: anywhere; text-wrap: balance;
  }
  @media (min-width: 760px) { .title { font-size: 46px; } }

  .status-row {
    display: flex; align-items: center; gap: 9px; margin-top: 2px; min-width: 0;
    font: 400 13px/1 system-ui, sans-serif; color: var(--ink-dim);
  }
  .status-row .sep { width: 3px; height: 3px; border-radius: 50%; background: var(--border-strong); flex: none; }
  /* which TV this is, quiet at the end of the line; the full name is the tooltip */
  .status-row .device { margin-left: auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @media (min-width: 760px) { .status-row { gap: 12px; } .status-row .device { margin-left: 0; } }

  .mode-btn {
    font: 500 13px/1 system-ui, sans-serif; color: var(--accent); flex: none;
  }
  @media (max-width: 759px), (pointer: coarse) {
    .mode-btn { min-height: 44px; padding: 0 10px; margin: 0 -10px; }
  }
  @media (min-width: 760px) {
    .mode-btn {
      font-size: 12px; letter-spacing: .14em; text-transform: uppercase;
      border: 1px solid var(--ctl-line); border-radius: 3px; padding: 6px 9px;
    }
  }
  .mode-detail {
    margin-top: 10px; font: 12px/1.7 system-ui, sans-serif; color: var(--ink-dim);
    border-left: 1px solid var(--border-strong); padding-left: 12px;
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
    .mode-row .status-row { margin-top: 0; }
    .mode-row .mode-detail { flex: 1; }
  }

  /* phone: the open space above the controls says how long is left and when
     it will end, readable from the couch. Desktop has the mode detail there. */
  .spacer { flex: 1; min-height: 24px; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .mid-num {
    font: 200 clamp(52px, 17vw, 72px)/1 ui-monospace, Menlo, Consolas, monospace;
    letter-spacing: -.04em; color: var(--ink-strong); text-align: center;
  }
  .mid-cap { font: 400 13px/1.4 system-ui, sans-serif; color: var(--ink-dim); margin-top: 10px; text-align: center; }
  .mid { padding: 12px 0; }
  .mid-num:empty, .mid-cap:empty { display: none; }
  /* short phones: scale the readout with the screen height so the controls
     never end up under the collapsed sheet, and drop it on very short ones */
  @media (max-height: 700px) {
    .mid { padding: 0; }
    .mid-num { font-size: clamp(40px, 7dvh, 52px); }
    .mid-cap { margin-top: 6px; line-height: 1.2; }
  }
  @media (max-height: 600px) { .mid { display: none; } }
  /* an error box needs the room more than the clock does */
  @media (max-height: 700px) { .has-err .mid { display: none; } }
  /* very short phones: when playback failed or ltc closed, the position and
     volume rows are moot, and the Try again button must stay above the sheet */
  @media (max-height: 600px) {
    .has-fail .times, .has-fail #seekBar, .has-fail .vol { display: none; }
  }
  @media (min-width: 760px) { .spacer .mid { display: none; } }

  /* --- bars (position, volume) ---
     44px tall hit area around a 4px line; the thumb is always drawn on touch
     screens, since there is no hover to reveal it. */
  .times {
    display: flex; justify-content: space-between;
    font: 13px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-dim);
  }
  .times .pos { color: var(--ink-strong); }
  .bar {
    position: relative; height: 44px; display: flex; align-items: center;
    margin-top: -2px; cursor: pointer; touch-action: none; border-radius: 8px;
  }
  .bar .track { position: absolute; left: 0; right: 0; height: 4px; border-radius: 2px; background: var(--track); }
  .bar .fill { position: absolute; left: 0; height: 4px; border-radius: 2px; background: var(--accent); width: 0%; pointer-events: none; }
  .bar.vol .fill { background: var(--ink-mid); }
  .bar .thumb {
    position: absolute; width: 14px; height: 14px; border-radius: 50%;
    background: var(--ink-strong); left: 0%; margin-left: -7px;
    opacity: 0; transition: opacity .12s;
  }
  .bar:hover .thumb, .bar.dragging .thumb, .bar:focus-visible .thumb { opacity: 1; }
  @media (pointer: coarse) { .bar .thumb { opacity: 1; } }
  .bar[aria-disabled="true"] { opacity: .38; cursor: default; }

  /* --- transport + volume row --- */
  .controls-row { display: flex; align-items: center; flex-wrap: wrap; margin-top: 8px; gap: 4px; }
  /* the four skips share the row with the play circle and may shrink to 44px,
     which is what keeps the row inside a 320px screen */
  .transport { display: flex; align-items: center; justify-content: space-between; gap: 4px; width: 100%; }
  .skip {
    flex: 1 1 0; min-width: 44px; max-width: 60px; height: 52px;
    font: 400 14px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink);
  }
  .skip.far { color: var(--ink-mid); font-size: 13px; }
  #toggle {
    width: 74px; height: 74px; border-radius: 50%; border: 1px solid var(--ctl-line);
    display: grid; place-items: center; flex: none; margin: 0 2px;
  }
  #toggle svg { width: 26px; height: 26px; fill: var(--ink-strong); }
  @media (min-width: 760px) {
    .transport { width: auto; justify-content: flex-start; }
    .skip { flex: none; width: 60px; }
    #toggle { width: 66px; height: 66px; }
    #toggle svg { width: 24px; height: 24px; }
  }

  .vol { display: flex; align-items: center; gap: 12px; margin-top: 8px; flex: 1 1 100%; }
  .vol-label {
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .14em;
    text-transform: uppercase; color: var(--ink-dim); flex: none;
  }
  .vol .bar { flex: 1; margin-top: 0; }
  #mute {
    font: 400 13px/1 system-ui, sans-serif; color: var(--ink-mid);
    border: 1px solid var(--ctl-line); border-radius: 9px; padding: 0 14px; min-height: 44px; flex: none;
  }
  @media (min-width: 760px) {
    .vol { margin-top: 0; margin-left: auto; flex: 0 0 auto; }
    .vol .bar { width: 120px; flex: none; }
  }

  /* --- footer: shortcuts (desktop) + stop ---
     Phone: the footer row dissolves and Stop becomes a small square in the top
     right corner, well away from the thumb, so the one destructive control
     can't be hit by accident. Pressed once it opens into a labelled pill.
     Desktop: it sits at the right of the footer with its label. */
  .col-main { position: relative; }
  .footer-row { display: contents; }
  .shortcuts { display: none; }
  #stop {
    position: absolute; top: 24px; right: 22px; z-index: 3; background: var(--bg);
    width: 44px; height: 44px; padding: 0; display: grid; place-items: center;
    border: 1px solid var(--ctl-line); border-radius: 11px; color: var(--ink-mid);
    font: 400 13px/1 system-ui, sans-serif;
  }
  #stop .stop-ico { width: 18px; height: 18px; fill: currentColor; flex: none; }
  #stop .stop-txt { display: none; white-space: nowrap; }
  #stop.confirm {
    width: auto; padding: 0 16px; display: flex; align-items: center; gap: 10px;
    color: var(--danger); border-color: var(--danger);
  }
  #stop.confirm .stop-txt { display: inline; }
  @media (max-width: 759px) { .title { padding-right: 58px; } }
  @media (min-width: 760px) {
    .footer-row {
      display: flex; align-items: center; gap: 26px;
      margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--border);
    }
    .shortcuts { display: flex; gap: 20px; font: 12px/1 system-ui, sans-serif; color: var(--ink-dim); margin-right: auto; }
    .shortcuts kbd { font-family: ui-monospace, Menlo, Consolas, monospace; color: var(--ink-mid); font-style: normal; }
    #stop {
      position: static; background: none; width: auto; padding: 0 14px; display: flex; gap: 8px;
      border-radius: 9px; color: var(--ink-dim); font-size: 12px;
    }
    #stop .stop-txt { display: inline; }
  }
  #err {
    margin-top: 14px; font-size: 13px; line-height: 1.45; color: var(--danger);
    border: 1px solid currentColor; border-radius: 8px; padding: 9px 11px;
  }
  #err:empty { display: none; }
  #err.note { color: var(--ink-mid); border-color: var(--border-strong); }
  .err-sum { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .err-sum b { font-weight: 500; flex: 1 1 180px; }
  .err-btn {
    border: 1px solid currentColor; border-radius: 9px; padding: 0 16px; min-height: 44px;
    font-size: 13px; color: var(--danger);
  }
  .err-det { margin-top: 6px; font-size: 12px; color: var(--ink-dim); }
  .err-det summary { cursor: pointer; min-height: 44px; display: flex; align-items: center; }
  .err-det p { margin: 0 0 4px; overflow-wrap: anywhere; }

  /* ============================================================ */
  /* subtitle panel — the core feature, so it gets its own surface */
  /* ============================================================ */

  /* mobile: draggable bottom sheet. Collapsed, it still shows the handle row
     (active track + offset) and the five sync keys, so nudging never needs a
     drag; only the track list and the explanation live below the fold. */
  .subs-sheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 10;
    background: var(--panel); border-radius: 22px 22px 0 0;
    padding: 0 22px calc(16px + env(safe-area-inset-bottom)); box-shadow: 0 -14px 34px rgba(0, 0, 0, .35);
    will-change: transform;
  }
  .subs-sheet.settling { transition: transform .26s var(--ease); }
  .sheet-handle {
    position: relative; display: flex; align-items: center; gap: 10px;
    width: 100%; min-height: 52px; padding: 20px 0 6px; text-align: left;
    cursor: grab; touch-action: none;
  }
  .grip {
    position: absolute; top: 9px; left: 50%; margin-left: -17px;
    width: 34px; height: 4px; border-radius: 2px; background: var(--ctl-line);
  }
  .sheet-handle .label { font: 500 13px/1 system-ui, sans-serif; color: var(--panel-ink); flex: none; }
  /* the active track's name; a subtitle filename can be far longer than the
     row, so clip it rather than letting it shove the offset off the edge */
  .sheet-handle .lang {
    font: 400 13px/1.2 system-ui, sans-serif; color: var(--panel-ink-chip); margin-left: auto;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
  }
  .sheet-handle .delay {
    font: 400 22px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-strong); flex: none;
    letter-spacing: -.02em;
  }

  /* every key shares the row proportionally and may shrink; fixed widths made
     the last one overflow the screen edge on a narrow phone */
  .sync-row { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
  .sync-btn {
    flex: 1 1 0; min-width: 0; height: 56px; background: var(--panel-alt); border-radius: 14px;
    color: var(--ink); font: 400 13px/1 ui-monospace, Menlo, Consolas, monospace;
  }
  .sync-reset {
    flex: 1.6 1 0; min-width: 0; height: 56px; color: var(--ink-strong);
    font: 300 clamp(20px, 6.4vw, 28px)/1 ui-monospace, Menlo, Consolas, monospace;
    letter-spacing: -.02em;
  }
  /* the part below the fold: capped so a long track list scrolls inside the
     sheet instead of pushing it taller than a short phone */
  .sheet-more { margin-top: 12px; max-height: min(36dvh, 260px); overflow-y: auto; overscroll-behavior: contain; }
  /* collapsed, the sheet's bottom padding is taller than this block's top
     margin, which would leave a sliver of the hint showing under the keys */
  .sheet-more[inert] { visibility: hidden; transition: visibility 0s linear .26s; }
  .sync-hint { font: 12px/1.5 system-ui, sans-serif; color: var(--panel-ink-dim); margin: 0; text-wrap: balance; }
  .track-list.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .chip {
    display: inline-flex; align-items: center; min-height: 44px; padding: 0 16px;
    font: 400 13px/1 system-ui, sans-serif; border-radius: 22px;
    color: var(--panel-ink-chip); background: var(--panel-alt);
  }
  /* the active track is the inverse fill plus a leading dot, so it reads
     without relying on color alone */
  .chip.on, .row-chip.on { color: var(--chip-ink); background: var(--chip-active-bg); font-weight: 600; }
  .chip.on::before, .row-chip.on::before {
    content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; margin-right: 8px; flex: none;
  }
  .track-empty { font: 13px/1.5 system-ui, sans-serif; color: var(--panel-ink-dim); margin-top: 12px; }

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
    width: 34px; height: 34px; border-radius: 8px; border: 1px solid var(--ctl-line);
    color: var(--panel-ink-chip); font: 400 14px/1 system-ui, sans-serif;
  }
  .rail .chevron { border: none; background: var(--panel-alt); border-radius: 9px; }
  .rail .delay { font: 400 15px/1 ui-monospace, Menlo, Consolas, monospace; color: var(--ink-strong); writing-mode: vertical-rl; }
  .rail .tag { font: 500 13px/1 system-ui, sans-serif; color: var(--panel-ink-dim); writing-mode: vertical-rl; letter-spacing: .06em; }

  .offset-display { text-align: center; margin-top: 30px; }
  .offset-display .num {
    font: 200 62px/1 system-ui, -apple-system, sans-serif; letter-spacing: -.045em;
    color: var(--ink-strong);
  }
  .offset-display .cap {
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .14em; text-transform: uppercase;
    color: var(--panel-ink-dim); margin-top: 12px;
  }
  .sync-row.desktop { margin-top: 26px; gap: 8px; }
  .sync-row.desktop .sync-btn { flex: 1; height: 56px; border-radius: 13px; }
  .subs-col .sync-hint { margin-top: 14px; }
  .track-cap {
    font: 400 12px/1 system-ui, sans-serif; letter-spacing: .14em; text-transform: uppercase;
    color: var(--panel-ink-dim); margin: 26px 0 12px;
  }
  .track-list.rows { display: flex; flex-direction: column; gap: 6px; overflow-y: auto; }
  .row-chip {
    display: flex; align-items: center; min-height: 44px;
    font: 400 13px/1 system-ui, sans-serif; border-radius: 9px; padding: 0 14px; text-align: left;
    color: var(--panel-ink-chip); background: var(--panel-alt);
  }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { transition: none !important; animation: none !important; }
  }
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
    <h1 class="title" id="title">…</h1>

    <div class="mode-row">
      <div class="status-row">
        <span class="dot" id="led"></span>
        <span class="state-word" id="stateWord" role="status">—</span>
        <span class="sep"></span>
        <button class="mode-btn" id="modeBtn" aria-controls="modeDetail" aria-expanded="false"></button>
        <span class="device" id="device"></span>
      </div>
      <div class="mode-detail" id="modeDetail"></div>
    </div>

    <div class="spacer">
      <div class="mid">
        <div class="mid-num" id="midLeft"></div>
        <div class="mid-cap" id="midEnds"></div>
      </div>
    </div>

    <div class="times"><span class="pos" id="pos">0:00</span><span id="dur">0:00</span></div>
    <div class="bar" id="seekBar" role="slider" tabindex="0" aria-label="Position" aria-valuemin="0" aria-valuenow="0">
      <div class="track"></div><div class="fill" id="seekFill"></div><div class="thumb" id="seekThumb"></div>
    </div>

    <div class="controls-row">
      <div class="transport">
        <button class="skip far" id="back30" aria-label="Back 30 seconds">&minus;30</button>
        <button class="skip" id="back10" aria-label="Back 10 seconds">&minus;10</button>
        <button id="toggle" aria-label="Play"></button>
        <button class="skip" id="fwd10" aria-label="Forward 10 seconds">+10</button>
        <button class="skip far" id="fwd30" aria-label="Forward 30 seconds">+30</button>
      </div>
      <div class="vol">
        <span class="vol-label" aria-hidden="true">Vol</span>
        <div class="bar vol" id="volBar" role="slider" tabindex="0" aria-label="Volume" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="track"></div><div class="fill" id="volFill"></div><div class="thumb" id="volThumb"></div></div>
        <button id="mute" aria-pressed="false">Mute</button>
      </div>
    </div>

    <div class="footer-row">
      <div class="shortcuts">
        <span><kbd>space</kbd> play</span>
        <span><kbd>&larr; &rarr;</kbd> 10s</span>
        <span><kbd>&darr; &uarr;</kbd> 30s</span>
        <span><kbd>[ ]</kbd> sync</span>
      </div>
      <button id="stop" aria-label="Stop and quit" title="Stop and quit">
        <svg class="stop-ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
        <span class="stop-txt">Stop and quit</span>
      </button>
    </div>
    <div id="err" role="alert"></div>
  </div>

  <!-- mobile: draggable bottom sheet -->
  <section class="subs-sheet" id="sheet" aria-label="Subtitles">
    <button type="button" class="sheet-handle" id="sheetHandle" aria-expanded="false" aria-controls="sheetMore">
      <span class="grip" aria-hidden="true"></span>
      <span class="label">Subtitles</span>
      <span class="lang" id="sheetLang"></span>
      <span class="delay" id="sheetDelay">0.0s</span>
    </button>
    <div class="sync-row" role="group" aria-label="Subtitle timing">
      <button class="sync-btn" data-nudge="-0.5">&minus;0.5</button>
      <button class="sync-btn" data-nudge="-0.1">&minus;0.1</button>
      <button class="sync-reset" id="sheetReset">0.0s</button>
      <button class="sync-btn" data-nudge="0.1">+0.1</button>
      <button class="sync-btn" data-nudge="0.5">+0.5</button>
    </div>
    <div class="sheet-more" id="sheetMore" inert>
      <p class="sync-hint">Minus shows subtitles earlier, plus later. Tap the number to reset.</p>
      <div class="track-list chips" id="chipsA" role="group" aria-label="Subtitle track"></div>
    </div>
  </section>

  <!-- desktop: static collapsible column -->
  <aside class="subs-col" id="subsCol" aria-label="Subtitles">
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
      <div class="sync-row desktop" role="group" aria-label="Subtitle timing">
        <button class="sync-btn" data-nudge="-0.5">&minus;0.5</button>
        <button class="sync-btn" data-nudge="-0.1">&minus;0.1</button>
        <button class="sync-btn" data-nudge="0.1">+0.1</button>
        <button class="sync-btn" data-nudge="0.5">+0.5</button>
      </div>
      <p class="sync-hint">Minus shows subtitles earlier, plus later.</p>
      <div class="track-cap">Track</div>
      <div class="track-list rows" id="rowsC" role="group" aria-label="Subtitle track"></div>
    </div>
  </aside>
</main>

<script>
const $ = id => document.getElementById(id);
const PLAY  = '<svg viewBox="0 0 24 24"><path d="M8 5.2v13.6L19 12z"/></svg>';
const PAUSE = '<svg viewBox="0 0 24 24"><path d="M7 5h3.4v14H7zm6.6 0H17v14h-3.4z"/></svg>';

let state = null;
let offline = false;
let endedText = '', endedOk = false;   // ltc closed itself and said why
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
// Status text is rewritten on every poll; assigning identical text would make
// a screen reader announce it again each second.
const setText = (el, t) => { if (el.textContent !== t) el.textContent = t; };
const setAttr = (el, k, v) => { if (el.getAttribute(k) !== String(v)) el.setAttribute(k, v); };

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
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error(r.status);
    apply(await r.json());
  } catch (e) {
    offline = true;
    $('led').className = 'dot';
    const word = endedText ? (endedOk ? 'Finished' : 'Stopped') : 'Offline';
    setText($('stateWord'), word);
    if (!endedText) {
      delete $('err').dataset.key;
      setText($('err'), "Can't reach ltc. Is it still running on your computer?");
    }
    refreshDisabled();
    syncErrClass();
  }
}

// Stop and the theme toggle stay usable whatever the connection does.
const ALWAYS_ON = new Set(['stop', 'sheetHandle', 'modeBtn', 'railOpen', 'railClose']);
function refreshDisabled() {
  const off = offline || !!endedText || (state && state.connected === false);
  const noSubs = !state || !state.subs.length;
  for (const b of document.querySelectorAll('.col-main button, .subs-sheet button, .subs-col button')) {
    if (ALWAYS_ON.has(b.id)) continue;
    b.disabled = off || (noSubs && b.matches('[data-nudge], #sheetReset, #colReset'));
  }
  for (const bar of [$('seekBar'), $('volBar')]) setAttr(bar, 'aria-disabled', off ? 'true' : 'false');
  $('stop').disabled = !!endedText;   // it would only talk to a server that is gone
}

let lastSubsKey = '';
function renderSubs(s) {
  const key = JSON.stringify([s.subs, s.active_sub]);
  if (key === lastSubsKey) return;
  lastSubsKey = key;

  // the handle reports the track that is actually on, not the first one in the list
  const active = s.subs.find(t => t.id === s.active_sub);
  const activeLabel = !s.subs.length ? 'No subtitles' : active ? active.label : 'Off';
  $('sheetLang').textContent = activeLabel;
  $('railTag').textContent = 'Subtitles' + (s.subs.length ? ' · ' + activeLabel : '');

  const chips = $('chipsA'), rows = $('rowsC');
  chips.innerHTML = ''; rows.innerHTML = '';

  if (!s.subs.length) {
    const note1 = document.createElement('div');
    note1.className = 'track-empty';
    note1.textContent = 'This file has no text subtitles.';
    chips.appendChild(note1);
    rows.appendChild(note1.cloneNode(true));
    sheetCtl.relayout();
    return;
  }

  const make = (t, cls, on) => {
    const b = document.createElement('button');
    b.className = cls + (on ? ' on' : '');
    b.textContent = t.label;
    b.setAttribute('aria-pressed', on);
    b.onclick = () => cmd('sub', t.id);
    return b;
  };
  for (const t of s.subs) {
    chips.appendChild(make(t, 'chip', s.active_sub === t.id));
    rows.appendChild(make(t, 'row-chip', s.active_sub === t.id));
  }
  const off = s.active_sub === null;
  const offChip = make({label: 'Off'}, 'chip', off);
  offChip.onclick = () => cmd('sub', 'off');
  chips.appendChild(offChip);
  const offRow = make({label: 'Off'}, 'row-chip', off);
  offRow.onclick = () => cmd('sub', 'off');
  rows.appendChild(offRow);
  sheetCtl.relayout();
}

// `s.error_kind` says what failed. "playback": the Chromecast refused the stream
// or stopped with an error, so the box offers Try again (reloads from where it
// was). "subtitles": a track failed to convert and was skipped, the video still
// plays, so the box offers Dismiss, remembered for this tab until a different
// error arrives.
let dismissedErr = '';
try { dismissedErr = sessionStorage.getItem('ltc-dismissed-err') || ''; } catch (e) {}
function syncErrClass() {
  document.body.classList.toggle('has-err', $('err').textContent !== '');
  // a failure that needs an action (Try again) or a closed ltc; plain subtitle notes don't count
  document.body.classList.toggle('has-fail', !!endedText || !!$('err').querySelector('[data-fail]'));
}

function renderErr(s) {
  renderErrBox(s);
  syncErrClass();
}

// ltc is closing on purpose: remember why, so the page can say what to do and
// keep saying it after the server is gone instead of "can't reach ltc".
function noteEnded(s) {
  if (!s.ended) return;
  endedOk = !!s.ended_ok;
  endedText = s.ended + (endedOk ? ' ltc has closed, you can close this tab.'
                                 : ' ltc has closed. Run it again to cast again.');
}

function renderErrBox(s) {
  const e = $('err');
  if (endedText) {
    delete e.dataset.key;
    e.classList.toggle('note', endedOk);
    setText(e, endedText);
    return;
  }
  if (s.connected === false) {
    delete e.dataset.key;
    setText(e, "Lost the connection to the Chromecast. Check that it's on and on the same Wi-Fi.");
    return;
  }
  const playback = s.error_kind === 'playback';
  const raw = s.error && (playback || s.error !== dismissedErr) ? s.error : '';
  const key = raw ? (playback ? 'p|' : 's|') + raw : '';
  if ((e.dataset.key || '') === key) return;
  e.dataset.key = key;
  if (!raw) { e.textContent = ''; return; }
  const summary = playback ? "The Chromecast couldn\'t play this video."
                           : "Some subtitles couldn\'t be loaded and were skipped.";
  e.innerHTML = '<div class="err-sum"' + (playback ? ' data-fail' : '') + '><b>' + summary + '</b>'
    + '<button class="err-btn" id="errAction">' + (playback ? 'Try again' : 'Dismiss') + '</button></div>'
    + '<details class="err-det"><summary>Details</summary><p>' + esc(raw) + '</p></details>';
  $('errAction').onclick = playback
    ? () => cmd('retry')
    : () => {
        dismissedErr = raw;
        try { sessionStorage.setItem('ltc-dismissed-err', raw); } catch (e2) {}
        e.dataset.key = ''; e.textContent = '';
      };
}

let shownPlaying = null;
function apply(s) {
  if (!s || !s.title) return;
  state = s;
  offline = false;
  noteEnded(s);
  localPos = s.position; localAt = performance.now();

  setText($('title'), s.title);
  setAttr($('title'), 'title', s.title);
  setText($('device'), s.device);
  setAttr($('device'), 'title', s.device);
  let stateWord = s.buffering ? 'Buffering' : s.playing ? 'Playing' : 'Paused';
  if (endedText) stateWord = endedOk ? 'Finished' : 'Stopped';
  setText($('stateWord'), stateWord);

  const dotClass = 'dot' + (endedText ? '' : s.playing ? ' playing' : s.buffering ? ' buffering' : '');
  $('led').className = dotClass;
  const showPlaying = s.playing && !endedText;   // a closed ltc isn't playing anything
  if (shownPlaying !== showPlaying) {
    shownPlaying = showPlaying;
    $('toggle').innerHTML = showPlaying ? PAUSE : PLAY;
    $('toggle').setAttribute('aria-label', showPlaying ? 'Pause' : 'Play');
  }

  const modeLabel = {direct: 'Direct', remux: 'Remuxing', transcode: 'Transcoding'}[s.mode] || s.mode;
  setText($('modeBtn'), modeLabel);
  $('modeBtn').setAttribute('aria-expanded', modeOpen);
  $('modeDetail').innerHTML = modeOpen ? (s.mode_detail || []).map(esc).join('<br>') : '';

  renderErr(s);

  setText($('dur'), fmt(s.duration));
  setAttr($('seekBar'), 'aria-valuemax', Math.round(s.duration || 0));

  const delayTxt = fmtDelay(s.sub_delay);
  for (const id of ['sheetDelay', 'sheetReset', 'railDelay', 'colReset'])
    setText($(id), delayTxt);
  for (const id of ['sheetReset', 'colReset'])
    setAttr($(id), 'aria-label', 'Reset subtitle offset, currently ' + delayTxt);

  if (!volDrag) {
    volLocal = s.muted ? 0 : s.volume;
    paintVol();
  }
  setText($('mute'), s.muted ? 'Unmute' : 'Mute');
  setAttr($('mute'), 'aria-pressed', !!s.muted);

  renderSubs(s);
  refreshDisabled();
  tick();
}

function tick() {
  if (!state) return;
  let p = localPos;
  if (state.playing && !endedText) p += (performance.now() - localAt) / 1000;
  p = Math.min(p, state.duration || p);
  setText($('pos'), fmt(p));
  if (state.duration) {
    const left = Math.max(0, state.duration - p);
    setText($('midLeft'), fmt(left));
    setText($('midEnds'), 'left · ' + (endedText ? 'stopped' : state.playing
      ? 'ends ' + new Date(Date.now() + left * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})
      : state.buffering ? 'buffering' : 'paused'));
  } else {
    setText($('midLeft'), ''); setText($('midEnds'), '');
  }
  setAttr($('seekBar'), 'aria-valuenow', Math.round(p));
  setAttr($('seekBar'), 'aria-valuetext', fmt(p) + ' of ' + fmt(state.duration || 0));
  if (!seekDrag && state.duration) { seekLocal = p / state.duration; paintSeek(); }
}

function paintSeek() {
  const pct = Math.max(0, Math.min(1, seekLocal)) * 100 + '%';
  $('seekFill').style.width = pct; $('seekThumb').style.left = pct;
}
function paintVol() {
  const pct = Math.max(0, Math.min(1, volLocal)) * 100 + '%';
  $('volFill').style.width = pct; $('volThumb').style.left = pct;
  setAttr($('volBar'), 'aria-valuenow', Math.round(volLocal * 100));
  setAttr($('volBar'), 'aria-valuetext', volLocal === 0 && state?.muted ? 'Muted' : Math.round(volLocal * 100) + '%');
}

function barDrag(bar, onMove, onCommit) {
  let active = false;
  const ratio = e => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  };
  bar.addEventListener('pointerdown', e => {
    if (bar.getAttribute('aria-disabled') === 'true') return;
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

// The volume slider takes the arrow keys itself. The position slider doesn't
// need to: the global shortcuts below already seek with them.
$('volBar').addEventListener('keydown', e => {
  const step = {ArrowLeft: -.05, ArrowDown: -.05, ArrowRight: .05, ArrowUp: .05}[e.key];
  const jump = {Home: 0, End: 1}[e.key];
  if (step === undefined && jump === undefined) return;
  e.preventDefault(); e.stopPropagation();
  if ($('volBar').getAttribute('aria-disabled') === 'true') return;
  volLocal = Math.max(0, Math.min(1, jump !== undefined ? jump : volLocal + step));
  paintVol(); cmd('volume', volLocal);
});

$('toggle').onclick = () => cmd('toggle');
$('back30').onclick = () => cmd('skip', -30);
$('back10').onclick = () => cmd('skip', -10);
$('fwd10').onclick  = () => cmd('skip', 10);
$('fwd30').onclick  = () => cmd('skip', 30);
$('mute').onclick   = () => cmd('mute', !state?.muted);

for (const b of document.querySelectorAll('[data-nudge]')) {
  const v = parseFloat(b.dataset.nudge);
  b.setAttribute('aria-label', 'Show subtitles ' + Math.abs(v) + ' seconds ' + (v < 0 ? 'earlier' : 'later'));
  b.onclick = () => cmd('nudge_delay', v);
}
$('sheetReset').onclick = () => cmd('delay', 0);
$('colReset').onclick   = () => cmd('delay', 0);

$('modeBtn').onclick = () => {
  modeOpen = !modeOpen;
  $('modeBtn').setAttribute('aria-expanded', modeOpen);
  $('modeDetail').innerHTML = modeOpen && state ? (state.mode_detail || []).map(esc).join('<br>') : '';
};

let stopArmed = false, stopTimer = null;
$('stop').onclick = () => {
  const label = $('stop').querySelector('.stop-txt');
  if (!stopArmed) {
    stopArmed = true;
    label.textContent = 'Tap again to stop and quit';
    $('stop').setAttribute('aria-label', 'Tap again to stop and quit');
    $('stop').classList.add('confirm');
    stopTimer = setTimeout(() => {
      stopArmed = false;
      label.textContent = 'Stop and quit';
      $('stop').setAttribute('aria-label', 'Stop and quit');
      $('stop').classList.remove('confirm');
    }, 3000);
    return;
  }
  clearTimeout(stopTimer);
  cmd('quit');
  document.querySelector('.app').innerHTML =
    '<div class="col-main"><p style="margin-top:40px;color:var(--ink-dim)">Done — you can close this tab.</p></div>';
  const hintEl = $('hint'); if (hintEl) hintEl.remove();
};

/* --- mobile sheet: drag the handle, or tap / press it, to open and collapse ---
   Collapsed, the handle row and the sync keys stay on screen; everything under
   them (hint + track list) is inert so it can't take focus or taps. A tap is
   handled by `click` only, after which the sheet animates: moving the sheet in
   pointerup made the browser aim the follow-up click at whatever slid under
   the finger, which could switch the subtitle track. After a drag, any stray
   click is swallowed for a moment for the same reason.
   Open, the sheet covers the controls rather than moving them; Escape or the
   handle closes it. Its collapsed height is published as --sheet-vis so the
   page above it reserves exactly that much room. */
const sheetCtl = (function () {
  const sheet = $('sheet'), handle = $('sheetHandle'), more = $('sheetMore');
  const sync = sheet.querySelector('.sync-row');
  const root = document.documentElement.style;
  let offset = 0, drag = null, open = false, quietUntil = 0;

  // distance from the bottom of the sync keys to the bottom of the track list
  const maxOffset = () => Math.max(0, more.offsetTop + more.offsetHeight - (sync.offsetTop + sync.offsetHeight));
  const paint = (commit, animate) => {
    sheet.classList.toggle('settling', !!animate);
    sheet.style.transform = 'translateY(' + offset + 'px)';
    if (!commit) return;
    more.inert = !open;
    handle.setAttribute('aria-expanded', open);
    if (sheet.offsetHeight) root.setProperty('--sheet-vis', (sheet.offsetHeight - maxOffset()) + 'px');
  };
  const settle = animate => { offset = open ? 0 : maxOffset(); paint(true, animate); };

  window.addEventListener('resize', () => settle(false));

  handle.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    drag = {y: e.clientY, base: offset, moved: false};
    handle.setPointerCapture(e.pointerId);
    sheet.classList.remove('settling');
  });
  handle.addEventListener('pointermove', e => {
    if (!drag) return;
    const dy = e.clientY - drag.y;
    if (!drag.moved && Math.abs(dy) < 4) return;
    drag.moved = true;
    offset = Math.max(0, Math.min(maxOffset(), drag.base + dy));
    paint(false, false);
  });
  const release = () => {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    if (!moved) return;                         // a tap: the click handler toggles
    open = offset < maxOffset() / 2;
    quietUntil = performance.now() + 400;
    settle(true);
  };
  handle.addEventListener('pointerup', release);
  handle.addEventListener('pointercancel', release);

  handle.addEventListener('click', () => {
    if (performance.now() < quietUntil) return;
    open = !open;
    settle(true);
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !open) return;
    open = false;
    settle(true);
    if (sheet.contains(document.activeElement)) handle.focus();
  });
  sheet.addEventListener('click', e => {
    if (performance.now() < quietUntil && !handle.contains(e.target)) {
      e.preventDefault(); e.stopPropagation();
    }
  }, true);

  return {
    // content height changed (tracks arrived, window resized): re-measure in place
    relayout() { settle(false); },
  };
})();

/* --- desktop column: chevron collapse, no drag --- */
$('railClose').onclick = () => $('subsCol').classList.add('closed');
$('railOpen').onclick  = () => $('subsCol').classList.remove('closed');

// A focused button still leaves the remote shortcuts alone to work (Tab to a
// control and the arrows keep seeking); it only keeps Space and Enter, which
// activate it.
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
  if (t.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;
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
sheetCtl.relayout();
setInterval(poll, 1000);
setInterval(tick, 250);
poll();
</script>
</body>
</html>
"""
