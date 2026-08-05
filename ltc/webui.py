"""Panel de control web. Todo el estado se lee de /api/status.

Es una sola página sin dependencias externas: se abre igual en la compu que en
el celular, así el teléfono queda de control remoto.

La forma es la de un control remoto de verdad —cuerpo angosto, teclas con
relieve, display hundido— porque es exactamente lo que uno tiene en la mano
mientras mira algo, y porque con el teléfono apaisado las teclas caen donde van
los pulgares.
"""

PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="mobile-web-app-capable" content="yes">
<title>Chromecast</title>
<style>
  :root {
    --page:      #0a0c0f;
    --body-hi:   #232830;
    --body-lo:   #14171c;
    --edge:      #0a0c10;
    --rim:       #333941;
    --key-hi:    #333941;
    --key-lo:    #21262d;
    --ink:       #ccd3dc;
    --ink-dim:   #7b8593;
    --screen:    #080a0d;
    --lit:       #e9e2d2;   /* tecla iluminada */
    --lit-ink:   #14171c;
    --led:       #55c46a;
    --led-wait:  #d6a13c;
    --danger:    #a4433a;
    --note:      #d9584c;
    --fill:      #6d7683;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --page:    #d7d9dc;
      --body-hi: #f2f3f5;
      --body-lo: #d9dcdf;
      --edge:    #a9aeb4;
      --rim:     #ffffff;
      --key-hi:  #ffffff;
      --key-lo:  #e2e5e9;
      --ink:     #23272d;
      --ink-dim: #767d87;
      --screen:  #b9c3b4;
      --lit:     #2f3540;
      --lit-ink: #f4f1e8;
      --led:     #2e8b45;
      --danger:  #b04b41;
      --note:    #c2372a;
      --fill:    #6a7079;
    }
  }
  :root[data-theme="dark"] {
    --page:#0a0c0f; --body-hi:#232830; --body-lo:#14171c; --edge:#0a0c10;
    --rim:#333941; --key-hi:#333941; --key-lo:#21262d; --ink:#ccd3dc;
    --ink-dim:#7b8593; --screen:#080a0d; --lit:#e9e2d2; --lit-ink:#14171c;
    --led:#55c46a; --danger:#a4433a; --note:#d9584c; --fill:#6d7683;
  }
  :root[data-theme="light"] {
    --page:#d7d9dc; --body-hi:#f2f3f5; --body-lo:#d9dcdf; --edge:#a9aeb4;
    --rim:#ffffff; --key-hi:#ffffff; --key-lo:#e2e5e9; --ink:#23272d;
    --ink-dim:#767d87; --screen:#b9c3b4; --lit:#2f3540; --lit-ink:#f4f1e8;
    --led:#2e8b45; --danger:#b04b41; --note:#c2372a; --fill:#6a7079;
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--ink);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding: 28px 16px calc(28px + env(safe-area-inset-bottom));
  }

  /* --- cuerpo del control --- */
  .remote {
    width: 100%;
    max-width: 372px;
    background: linear-gradient(180deg, var(--body-hi) 0%, var(--body-lo) 62%);
    border: 1px solid var(--edge);
    border-radius: 30px;
    padding: 20px 20px 24px;
    box-shadow:
      inset 0 1px 0 var(--rim),
      0 24px 60px rgba(0, 0, 0, .55),
      0 2px 6px rgba(0, 0, 0, .4);
  }

  /* --- display hundido --- */
  .screen {
    background: var(--screen);
    border: 1px solid var(--edge);
    border-radius: 14px;
    padding: 13px 15px 15px;
    box-shadow: inset 0 3px 9px rgba(0, 0, 0, .65);
    margin-bottom: 20px;
  }
  .now { display: flex; align-items: baseline; gap: 8px; }
  .title {
    font-size: 1.02rem; font-weight: 600; letter-spacing: -.01em;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
  }
  .where {
    display: flex; align-items: center; gap: 7px;
    font-size: .76rem; color: var(--ink-dim); margin-top: 3px;
  }
  .led {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--ink-dim); flex: none;
  }
  .led.playing { background: var(--led); box-shadow: 0 0 7px var(--led); }
  .led.buffering { background: var(--led-wait); box-shadow: 0 0 7px var(--led-wait); }
  .mode {
    font-size: .62rem; text-transform: uppercase; letter-spacing: .1em;
    color: var(--ink-dim); border: 1px solid currentColor;
    border-radius: 3px; padding: 1px 5px; opacity: .75;
  }
  .times {
    display: flex; justify-content: space-between;
    font-variant-numeric: tabular-nums; font-size: .78rem;
    color: var(--ink-dim); margin-top: 12px;
  }
  #why {
    font-size: .7rem; color: var(--ink-dim); margin-top: 9px;
    padding-top: 9px; border-top: 1px solid rgba(128, 128, 128, .18);
    line-height: 1.4;
  }
  #why:empty { display: none; }

  /* --- barras deslizantes --- */
  input[type=range] {
    -webkit-appearance: none; appearance: none;
    width: 100%; height: 22px; background: transparent; margin: 0;
    --pct: 0;
  }
  /* El relleno lo pinta un degradado cortado en --pct: sin esto la barra se ve
     siempre vacía y parece rota. Firefox tiene ::-moz-range-progress y no lo
     necesita. */
  input[type=range]::-webkit-slider-runnable-track {
    height: 5px; border-radius: 3px;
    background: linear-gradient(90deg,
      var(--fill) 0 calc(var(--pct) * 1%),
      var(--edge) calc(var(--pct) * 1%) 100%);
    box-shadow: inset 0 1px 2px rgba(0, 0, 0, .6);
  }
  input[type=range]::-moz-range-track {
    height: 5px; border-radius: 3px; background: var(--edge);
    box-shadow: inset 0 1px 2px rgba(0, 0, 0, .6);
  }
  input[type=range]::-moz-range-progress {
    height: 5px; border-radius: 3px; background: var(--fill);
  }
  input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 17px; height: 17px; border-radius: 50%; margin-top: -6px;
    background: linear-gradient(180deg, var(--key-hi), var(--key-lo));
    border: 1px solid var(--edge);
    box-shadow: 0 1px 3px rgba(0, 0, 0, .55), inset 0 1px 0 var(--rim);
  }
  input[type=range]::-moz-range-thumb {
    width: 17px; height: 17px; border-radius: 50%;
    background: linear-gradient(180deg, var(--key-hi), var(--key-lo));
    border: 1px solid var(--edge);
    box-shadow: 0 1px 3px rgba(0, 0, 0, .55);
  }
  #seek { margin-top: 2px; }

  /* --- teclas --- */
  button {
    font: inherit; color: var(--ink); cursor: pointer;
    background: linear-gradient(180deg, var(--key-hi) 0%, var(--key-lo) 100%);
    border: 1px solid var(--edge);
    border-radius: 9px;
    padding: 10px 12px;
    box-shadow: inset 0 1px 0 var(--rim), 0 2px 4px rgba(0, 0, 0, .45);
    transition: transform .04s, box-shadow .04s;
  }
  button:active:not(:disabled) {
    transform: translateY(1.5px);
    box-shadow: inset 0 2px 5px rgba(0, 0, 0, .5);
    background: linear-gradient(180deg, var(--key-lo) 0%, var(--key-hi) 100%);
  }
  button:disabled { opacity: .35; cursor: default; }
  button:focus-visible { outline: 2px solid var(--ink-dim); outline-offset: 2px; }
  button.on {
    background: var(--lit); color: var(--lit-ink); border-color: var(--edge);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, .5), 0 0 12px rgba(233, 226, 210, .16);
  }

  /* --- transporte --- */
  .transport {
    display: flex; align-items: center; justify-content: center;
    gap: 9px; margin: 18px 0 4px;
  }
  .transport button {
    font-variant-numeric: tabular-nums; font-size: .82rem;
    padding: 11px 0; flex: 1; min-width: 0;
  }
  #toggle {
    flex: 0 0 62px; height: 62px; border-radius: 50%;
    display: grid; place-items: center; padding: 0;
    background: linear-gradient(180deg, var(--key-hi) 0%, var(--key-lo) 100%);
    box-shadow: inset 0 1px 0 var(--rim), 0 4px 9px rgba(0, 0, 0, .5);
  }
  #toggle svg { width: 24px; height: 24px; fill: var(--ink); }
  #toggle:active { box-shadow: inset 0 3px 8px rgba(0, 0, 0, .55); }

  /* --- secciones --- */
  .band {
    display: flex; align-items: center; gap: 10px;
    margin: 22px 0 11px;
    font-size: .61rem; text-transform: uppercase;
    letter-spacing: .16em; color: var(--ink-dim);
  }
  .band::after {
    content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, rgba(128, 128, 128, .28), transparent);
  }
  .keys { display: flex; flex-wrap: wrap; gap: 8px; }
  .keys button { font-size: .84rem; padding: 9px 13px; }
  .sync { display: flex; gap: 6px; align-items: center; margin-top: 10px; }
  .sync button { flex: 1; font-size: .78rem; padding: 9px 0; min-width: 0; }
  /* El desfase actual es a la vez el botón de volver a cero: una tecla suelta
     rotulada "0" al lado de "+0,1" no se entiende. */
  #delay {
    flex: 0 0 68px; text-align: center; font-variant-numeric: tabular-nums;
    font-size: .78rem; color: var(--ink-dim);
    background: var(--screen); border: 1px solid var(--edge);
    border-radius: 7px; padding: 9px 0;
    box-shadow: inset 0 2px 5px rgba(0, 0, 0, .5);
  }
  #delay:active { transform: none; box-shadow: inset 0 2px 5px rgba(0, 0, 0, .5); }
  .vol { display: flex; align-items: center; gap: 9px; }
  .vol button { flex: 0 0 42px; font-size: .95rem; padding: 9px 0; }
  #volLabel {
    flex: 0 0 46px; text-align: right; font-size: .76rem;
    color: var(--ink-dim); font-variant-numeric: tabular-nums;
  }
  .wide { width: 100%; margin-top: 9px; font-size: .8rem;
          letter-spacing: .04em; text-transform: uppercase; }
  #stop { color: var(--danger); margin-top: 22px; }
  #err {
    display: none; margin-top: 14px; font-size: .78rem;
    color: var(--danger); border: 1px solid currentColor;
    border-radius: 8px; padding: 9px 11px;
  }

  /* --- aviso del celular: sólo tiene sentido en una pantalla grande --- */
  .hint {
    position: fixed; top: 20px; left: 26px; width: 216px;
    color: var(--note); pointer-events: none; z-index: 5;
  }
  .hint svg { display: block; width: 132px; height: 92px; margin-left: 74px; }
  .hint p {
    margin: 2px 0 0; font-size: 1.24rem; line-height: 1.35;
    letter-spacing: .01em;
  }
  @media (max-width: 1180px), (pointer: coarse) { .hint { display: none; } }
  .hint.hidden { display: none; }
</style>
</head>
<body>

<aside class="hint" id="hint">
  <svg viewBox="0 0 132 92" aria-hidden="true">
    <path d="M12 86 C 26 48, 60 16, 112 12" fill="none" stroke="currentColor"
          stroke-width="7" stroke-linecap="round"/>
    <path d="M122 9 L94 4 L104 32 Z" fill="currentColor"/>
  </svg>
  <p>Try using it<br>on your phone</p>
</aside>

<main class="remote">
  <div class="screen">
    <div class="now"><span class="title" id="title">…</span></div>
    <div class="where">
      <span class="led" id="led"></span>
      <span id="device">conectando…</span>
      <span class="mode" id="mode"></span>
    </div>
    <div class="times"><span id="pos">0:00</span><span id="dur">0:00</span></div>
    <input type="range" id="seek" min="0" max="1000" value="0" step="1"
           aria-label="Posición">
    <div id="why"></div>
  </div>

  <div class="transport">
    <button id="back30" aria-label="Atrás 30 segundos">&minus;30</button>
    <button id="back10" aria-label="Atrás 10 segundos">&minus;10</button>
    <button id="toggle" aria-label="Reproducir o pausar"></button>
    <button id="fwd10" aria-label="Adelante 10 segundos">+10</button>
    <button id="fwd30" aria-label="Adelante 30 segundos">+30</button>
  </div>

  <div class="band">Subtítulos</div>
  <div class="keys" id="subs"></div>
  <div class="sync">
    <button data-delay="-0.5">&minus;0,5</button>
    <button data-delay="-0.1">&minus;0,1</button>
    <button id="delay" title="Tocar para volver a cero"
            aria-label="Desfase actual; tocar para volver a cero">0,0s</button>
    <button data-delay="0.1">+0,1</button>
    <button data-delay="0.5">+0,5</button>
  </div>

  <div class="band">Volumen</div>
  <div class="vol">
    <button id="volDown" aria-label="Bajar volumen">&minus;</button>
    <input type="range" id="vol" min="0" max="100" value="50" aria-label="Volumen">
    <button id="volUp" aria-label="Subir volumen">+</button>
    <span id="volLabel">—</span>
  </div>
  <button id="mute" class="wide">Silenciar</button>

  <button id="stop" class="wide">Detener y salir</button>
  <div id="err"></div>
</main>

<script>
const $ = id => document.getElementById(id);
const PLAY  = '<svg viewBox="0 0 24 24"><path d="M8 5.2v13.6L19 12z"/></svg>';
const PAUSE = '<svg viewBox="0 0 24 24"><path d="M7 5h3.4v14H7zm6.6 0H17v14h-3.4z"/></svg>';

let state = null, dragging = false, localPos = 0, localAt = 0;

// WebKit no sabe rellenar el tramo recorrido de un <input type=range>, así que
// le pasamos el porcentaje y lo dibuja el degradado del CSS.
function paint(el) { el.style.setProperty('--pct', el.value / el.max * 100); }
function setRange(el, v) { el.value = v; paint(el); }

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
  catch (e) { $('led').className = 'led'; }
}

function esc(t) {
  return String(t).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
}

function apply(s) {
  if (!s || !s.title) return;   // una respuesta de error no trae estado
  state = s;
  localPos = s.position; localAt = performance.now();

  $('title').textContent = s.title;
  $('device').textContent = s.device;
  $('led').className = 'led' + (s.playing ? ' playing' : s.buffering ? ' buffering' : '');
  $('toggle').innerHTML = s.playing ? PAUSE : PLAY;

  $('mode').textContent = {direct: 'directo', remux: 'reempaquetado',
                           transcode: 'transcodificando'}[s.mode] || s.mode;
  $('why').innerHTML = (s.mode_detail || []).map(esc).join('<br>');

  if (s.error) { $('err').style.display = 'block'; $('err').textContent = s.error; }
  else $('err').style.display = 'none';

  $('dur').textContent = fmt(s.duration);
  $('delay').textContent = (s.sub_delay >= 0 ? '+' : '−') +
    Math.abs(s.sub_delay).toFixed(1).replace('.', ',') + 's';

  const vol = Math.round(s.volume * 100);
  if (document.activeElement !== $('vol')) setRange($('vol'), vol);
  $('volLabel').textContent = s.muted ? 'mudo' : vol + '%';
  $('mute').className = 'wide' + (s.muted ? ' on' : '');

  renderSubs(s);
  tick();
}

let lastSubsKey = '';
function renderSubs(s) {
  const key = JSON.stringify([s.subs, s.active_sub]);
  if (key === lastSubsKey) return;
  lastSubsKey = key;

  const box = $('subs');
  box.innerHTML = '';
  if (!s.subs.length) {
    box.innerHTML = '<span style="color:var(--ink-dim);font-size:.8rem">' +
                    'Este archivo no trae subtítulos de texto.</span>';
    return;
  }
  for (const t of s.subs) {
    const b = document.createElement('button');
    b.textContent = t.label;
    if (s.active_sub === t.id) b.className = 'on';
    b.onclick = () => cmd('sub', t.id);
    box.appendChild(b);
  }
  const off = document.createElement('button');
  off.textContent = 'Ninguno';
  if (s.active_sub === null) off.className = 'on';
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
    setRange($('seek'), Math.round(p / state.duration * 1000));
}

$('toggle').onclick  = () => cmd('toggle');
$('back30').onclick  = () => cmd('skip', -30);
$('back10').onclick  = () => cmd('skip', -10);
$('fwd10').onclick   = () => cmd('skip', 10);
$('fwd30').onclick   = () => cmd('skip', 30);
$('mute').onclick    = () => cmd('mute', !state?.muted);
$('volDown').onclick = () => cmd('volume', Math.max(0, (state?.volume ?? 0) - .05));
$('volUp').onclick   = () => cmd('volume', Math.min(1, (state?.volume ?? 0) + .05));
$('stop').onclick    = async () => {
  if (!confirm('¿Detener la reproducción y cerrar?')) return;
  await cmd('quit');
  document.querySelector('.remote').innerHTML =
    '<div class="screen" style="text-align:center">Listo. Ya podés cerrar esta pestaña.</div>';
  $('hint')?.classList.add('hidden');
};

$('vol').oninput  = e => { paint(e.target); $('volLabel').textContent = e.target.value + '%'; };
$('vol').onchange = e => cmd('volume', e.target.value / 100);

$('seek').oninput = () => {
  dragging = true;
  paint($('seek'));
  if (state?.duration) $('pos').textContent = fmt($('seek').value / 1000 * state.duration);
};
$('seek').onchange = () => {
  dragging = false;
  if (state?.duration) cmd('seek', $('seek').value / 1000 * state.duration);
};

for (const b of document.querySelectorAll('[data-delay]'))
  b.onclick = () => cmd('nudge_delay', parseFloat(b.dataset.delay));
$('delay').onclick = () => cmd('delay', 0);

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const keys = {
    ' ': () => cmd('toggle'), ArrowLeft: () => cmd('skip', -10),
    ArrowRight: () => cmd('skip', 10), ArrowDown: () => cmd('skip', -30),
    ArrowUp: () => cmd('skip', 30), m: () => cmd('mute', !state?.muted),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

// El aviso sólo sirve si estás justamente en la compu; en el teléfono sobra.
if (matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0)
  $('hint').classList.add('hidden');

$('toggle').innerHTML = PLAY;
setInterval(poll, 1000);
setInterval(tick, 250);
poll();
</script>
</body>
</html>
"""
