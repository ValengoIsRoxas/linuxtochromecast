"""Prueba contra un Chromecast de verdad.

Verifica lo único que un simulador no puede: que el receptor acepte el mensaje
LOAD con varias pistas, que efectivamente baje el .vtt desde nuestro servidor, y
que prender/apagar subtítulos en caliente funcione.

Registra cada petición que llega al servidor junto con la IP de origen, así que
"el Chromecast bajó los subtítulos" se comprueba, no se asume.

    python tests/test_live.py CARPETA_DE_MEDIOS [nombre-del-dispositivo]
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ltc import server as server_mod  # noqa: E402
from ltc.caster import Caster, discover  # noqa: E402
from ltc.probe import probe  # noqa: E402
from ltc.server import serve  # noqa: E402
from ltc.session import Session  # noqa: E402

MEDIA = Path(sys.argv[1] if len(sys.argv) > 1 else "media")
WANTED = sys.argv[2] if len(sys.argv) > 2 else None

PASS, FAIL = [], []
REQUESTS: Counter = Counter()
CLIENTS: dict[str, set] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  \033[2m{detail}\033[0m" if detail else ""), flush=True)


def instrument() -> None:
    """Anota cada GET con su ruta y quién lo pidió."""
    original = server_mod.CastRequestHandler.do_GET

    def spy(self):
        route = self.path.split("?")[0]
        key = "/sub" if route.startswith("/sub/") else route
        REQUESTS[key] += 1
        CLIENTS.setdefault(key, set()).add(self.client_address[0])
        return original(self)

    server_mod.CastRequestHandler.do_GET = spy


def wait_for(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def run_case(path: Path, caster: Caster, label: str, expect_mode: str) -> None:
    print(f"\n\033[1m{label}\033[0m", flush=True)
    REQUESTS.clear()
    CLIENTS.clear()

    session = Session.create(probe(path), caster, sub_lang="spa")
    check(f"elige modo «{expect_mode}»", session.plan.mode == expect_mode,
          session.plan.mode)

    http = serve(session, port=0)
    from ltc.caster import local_ip_towards
    ip = local_ip_towards(caster.device.host)
    session.base_url = f"http://{ip}:{http.server_address[1]}"
    print(f"  \033[2mservidor en {session.base_url}\033[0m", flush=True)

    try:
        session.start(0)
        check("el receptor acepta el LOAD",
              caster.wait_for_session(timeout=30),
              f"estado: {caster.player_state}")

        check("el Chromecast pide el video",
              wait_for(lambda: REQUESTS["/media"] > 0, 20),
              f"{REQUESTS['/media']} peticiones desde {CLIENTS.get('/media', set())}")

        # Lo importante: el receptor baja el .vtt solo si la pista está activa y
        # si el CORS está bien. Si falla, se ve el video sin subtítulos.
        check("el Chromecast baja los subtítulos",
              wait_for(lambda: REQUESTS["/sub"] > 0, 20),
              f"{REQUESTS['/sub']} peticiones desde {CLIENTS.get('/sub', set())}")

        check("las pide desde su propia IP",
              caster.device.host in CLIENTS.get("/sub", set()),
              f"esperaba {caster.device.host}, vi {CLIENTS.get('/sub', set())}")

        check("llega a reproducir",
              wait_for(lambda: caster.player_state == "PLAYING", 25),
              caster.player_state)

        time.sleep(3)
        pos = session.position()
        check("la posición avanza", pos > 0.3, f"{pos:.1f}s")

        # Cambio de pista en caliente: no debe cortar la reproducción.
        tracks = session.usable_tracks()
        if len(tracks) > 1:
            other = next(t for t in tracks if t.id != session.active_sub)
            before = REQUESTS["/sub"]
            session.set_subtitle(other.id)
            check("cambiar de subtítulo pide la otra pista",
                  wait_for(lambda: REQUESTS["/sub"] > before, 12),
                  f"{REQUESTS['/sub']} peticiones totales")
            time.sleep(1)
            check("sigue reproduciendo tras el cambio",
                  caster.player_state in ("PLAYING", "BUFFERING"),
                  caster.player_state)

        session.set_subtitle(None)
        time.sleep(1)
        check("apagar subtítulos no corta el video",
              caster.player_state in ("PLAYING", "BUFFERING"),
              caster.player_state)

        # Seek: en modo directo es nativo, en pipe reinicia ffmpeg.
        target = min(5.0, max(1.0, session.duration() - 4))
        session.seek(target)
        check("el seek reposiciona",
              wait_for(lambda: caster.player_state in ("PLAYING", "BUFFERING"), 20)
              and session.position() >= target - 2.5,
              f"pedí {target:.1f}s, quedó en {session.position():.1f}s")
    finally:
        try:
            session.stop()
        except Exception:
            pass
        http.shutdown_all()
        http.shutdown()


def main() -> int:
    instrument()

    print("\033[1mBuscando el Chromecast…\033[0m", flush=True)
    devices = discover(timeout=8, name=WANTED)
    devices = [d for d in devices if d.cast_type != "audio"]
    if not devices:
        print("\033[31mNo encontré ningún Chromecast con video.\033[0m")
        return 1

    device = devices[0]
    print(f"  \033[32m✓\033[0m {device}", flush=True)

    caster = Caster(device)
    try:
        run_case(MEDIA / "compatible.mp4", caster,
                 "Direct play + subtítulo externo en latin-1", "direct")
        run_case(MEDIA / "pelicula.mkv", caster,
                 "Transcodificación + dos pistas de subtítulos", "transcode")
    finally:
        print("\n\033[2m  Cerrando la app del Chromecast…\033[0m", flush=True)
        caster.quit_app()
        time.sleep(1)
        caster.disconnect()

    print(f"\n  \033[32m{len(PASS)} pasaron\033[0m", end="")
    if FAIL:
        print(f", \033[31m{len(FAIL)} fallaron\033[0m:")
        for f in FAIL:
            print(f"    - {f}")
        return 1
    print("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
