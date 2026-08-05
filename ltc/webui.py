"""Panel de control web. Todo el estado se lee de /api/status.

Es una sola página sin dependencias externas: se abre igual en la compu que en
el celular, así el teléfono queda de control remoto.
"""

PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Chromecast</title>
<style>
  :root {
    --bg: #0e1116; --card: #171b22; --line: #262c37;
    --fg: #e7ecf3; --dim: #8b97a8; --accent: #4c9aff; --accent-dim: #2b5fa8;
    --ok: #3fb950; --warn: #d29922;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f6f9; --card: #ffffff; --line: #dfe4ec;
      --fg: #161c24; --dim: #5d6b7f; --accent: #1c6ee0; --accent-dim: #a9c8f5;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: max(16px, env(safe-area-inset-top)) 16px 32px;
    display: flex; justify-content: center;
  }
  .wrap { width: 100%; max-width: 620px; }
  h1 { font-size: 1.25rem; margin: 0 0 2px; overflow-wrap: anywhere; }
  .sub { color: var(--dim); font-size: .875rem; margin-bottom: 18px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 18px; margin-bottom: 14px;
  }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .between { justify-content: space-between; }
  h2 {
    font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
    color: var(--dim); margin: 0 0 12px; font-weight: 600;
  }
  button {
    font: inherit; color: var(--fg); background: transparent;
    border: 1px solid var(--line); border-radius: 9px;
    padding: 9px 14px; cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: .4; cursor: default; }
  button.on { background: var(--accent); border-color: var(--accent); color: #fff; }
  .big {
    width: 62px; height: 46px; font-size: 1.1rem;
    background: var(--accent); border-color: var(--accent); color: #fff;
  }
  .transport { justify-content: center; gap: 8px; margin-top: 6px; }
  .times { display: flex; justify-content: space-between; font-variant-numeric: tabular-nums;
           font-size: .8rem; color: var(--dim); margin-top: 6px; }
  input[type=range] { width: 100%; accent-color: var(--accent); height: 26px; }
  .pills { display: flex; flex-wrap: wrap; gap: 8px; }
  .pills button { flex: 0 1 auto; font-size: .875rem; padding: 8px 12px; }
  .badge {
    font-size: .7rem; padding: 3px 8px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--dim);
  }
  .badge.live { border-color: var(--warn); color: var(--warn); }
  .why { font-size: .78rem; color: var(--dim); margin-top: 10px; line-height: 1.45; }
  .why li { margin: 2px 0; }
  .why ul { margin: 0; padding-left: 18px; }
  .delay { font-variant-numeric: tabular-nums; min-width: 76px; text-align: center; }
  .err {
    background: #3a1d1d; border-color: #6b2b2b; color: #ffb4b4;
    font-size: .82rem; display: none;
  }
  @media (prefers-color-scheme: light) {
    .err { background: #fdeaea; border-color: #f3b8b8; color: #8c2020; }
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); }
  .dot.playing { background: var(--ok); }
  .dot.buffering { background: var(--warn); }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title">…</h1>
  <div class="sub row">
    <span class="dot" id="dot"></span>
    <span id="device">conectando…</span>
    <span class="badge" id="mode"></span>
  </div>

  <div class="card err" id="err"></div>

  <div class="card">
    <input type="range" id="seek" min="0" max="1000" value="0" step="1">
    <div class="times"><span id="pos">0:00</span><span id="dur">0:00</span></div>
    <div class="row transport">
      <button id="back30" title="-30s">« 30</button>
      <button id="back10" title="-10s">‹ 10</button>
      <button id="toggle" class="big">▶</button>
      <button id="fwd10" title="+10s">10 ›</button>
      <button id="fwd30" title="+30s">30 »</button>
    </div>
  </div>

  <div class="card">
    <h2>Subtítulos</h2>
    <div class="pills" id="subs"></div>
    <div class="row between" style="margin-top:14px">
      <span style="color:var(--dim);font-size:.82rem">Sincronía</span>
      <div class="row" style="gap:6px">
        <button data-delay="-0.5">−0,5s</button>
        <button data-delay="-0.1">−0,1s</button>
        <span class="delay" id="delay">0,0s</span>
        <button data-delay="0.1">+0,1s</button>
        <button data-delay="0.5">+0,5s</button>
        <button id="resetDelay" title="Volver a cero">↺</button>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Volumen</h2>
    <div class="row">
      <button id="mute" style="min-width:52px">🔊</button>
      <input type="range" id="vol" min="0" max="100" value="50" style="flex:1">
      <span id="volLabel" style="color:var(--dim);min-width:44px;text-align:right">—</span>
    </div>
    <div class="why" id="why"></div>
    <div class="row" style="margin-top:14px">
      <button id="stop">Detener y salir</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let state = null;
let dragging = false;
let localPos = 0, localAt = 0;   // interpolación entre polls, para que no salte

const fmt = s => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
};

async function cmd(c, value) {
  try {
    const r = await fetch('/api/cmd', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cmd: c, value})
    });
    apply(await r.json());
  } catch (e) { /* el server puede estar reiniciando el stream */ }
}

async function poll() {
  try { apply(await (await fetch('/api/status')).json()); }
  catch (e) { $('dot').className = 'dot'; }
}

function apply(s) {
  if (!s || !s.title) return;   // una respuesta de error no trae estado
  state = s;
  localPos = s.position; localAt = performance.now();

  $('title').textContent = s.title;
  $('device').textContent = s.device;
  $('dot').className = 'dot' + (s.playing ? ' playing' : s.buffering ? ' buffering' : '');
  $('toggle').textContent = s.playing ? '❚❚' : '▶';

  const modeName = {direct: 'directo', remux: 'reempaquetado', transcode: 'transcodificando'}[s.mode] || s.mode;
  $('mode').textContent = modeName;
  $('mode').className = 'badge' + (s.seekable ? '' : ' live');

  $('why').innerHTML = (s.mode_detail || []).length
    ? '<ul>' + s.mode_detail.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>' : '';

  if (s.error) { $('err').style.display = 'block'; $('err').textContent = s.error; }
  else $('err').style.display = 'none';

  $('dur').textContent = fmt(s.duration);
  $('delay').textContent = (s.sub_delay >= 0 ? '+' : '−') +
    Math.abs(s.sub_delay).toFixed(1).replace('.', ',') + 's';

  const vol = Math.round(s.volume * 100);
  if (document.activeElement !== $('vol')) $('vol').value = vol;
  $('volLabel').textContent = s.muted ? 'mudo' : vol + '%';
  $('mute').textContent = s.muted ? '🔇' : '🔊';
  $('mute').className = s.muted ? 'on' : '';

  renderSubs(s);
  tick();
}

function esc(t) {
  return String(t).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
}

let lastSubsKey = '';
function renderSubs(s) {
  const key = JSON.stringify([s.subs, s.active_sub]);
  if (key === lastSubsKey) return;
  lastSubsKey = key;

  const box = $('subs');
  box.innerHTML = '';
  if (!s.subs.length) {
    box.innerHTML = '<span style="color:var(--dim);font-size:.85rem">' +
                    'Este archivo no trae subtítulos de texto.</span>';
    return;
  }
  for (const t of s.subs) {
    const b = document.createElement('button');
    b.textContent = t.label;
    b.className = s.active_sub === t.id ? 'on' : '';
    b.onclick = () => cmd('sub', t.id);
    box.appendChild(b);
  }
  const off = document.createElement('button');
  off.textContent = 'Sin subtítulos';
  off.className = s.active_sub === null ? 'on' : '';
  off.onclick = () => cmd('sub', 'off');
  box.appendChild(off);
}

function tick() {
  if (!state) return;
  let p = localPos;
  if (state.playing) p += (performance.now() - localAt) / 1000;
  p = Math.min(p, state.duration || p);
  $('pos').textContent = fmt(p);
  if (!dragging && state.duration)
    $('seek').value = Math.round(p / state.duration * 1000);
}

$('toggle').onclick = () => cmd('toggle');
$('back30').onclick = () => cmd('skip', -30);
$('back10').onclick = () => cmd('skip', -10);
$('fwd10').onclick = () => cmd('skip', 10);
$('fwd30').onclick = () => cmd('skip', 30);
$('stop').onclick = async () => {
  if (!confirm('¿Detener la reproducción y cerrar?')) return;
  await cmd('quit');
  document.body.innerHTML =
    '<div class="wrap"><div class="card" style="text-align:center">' +
    'Listo. Ya podés cerrar esta pestaña.</div></div>';
};
$('mute').onclick = () => cmd('mute', !state?.muted);

$('vol').oninput = e => { $('volLabel').textContent = e.target.value + '%'; };
$('vol').onchange = e => cmd('volume', e.target.value / 100);

$('seek').oninput = () => {
  dragging = true;
  if (state?.duration) $('pos').textContent = fmt($('seek').value / 1000 * state.duration);
};
$('seek').onchange = () => {
  dragging = false;
  if (state?.duration) cmd('seek', $('seek').value / 1000 * state.duration);
};

for (const b of document.querySelectorAll('[data-delay]'))
  b.onclick = () => cmd('nudge_delay', parseFloat(b.dataset.delay));
$('resetDelay').onclick = () => cmd('delay', 0);

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const keys = {
    ' ': () => cmd('toggle'), ArrowLeft: () => cmd('skip', -10),
    ArrowRight: () => cmd('skip', 10), ArrowDown: () => cmd('skip', -30),
    ArrowUp: () => cmd('skip', 30), m: () => cmd('mute', !state?.muted),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

setInterval(poll, 1000);
setInterval(tick, 250);
poll();
</script>
</body>
</html>
"""
