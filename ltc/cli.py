"""Entrada por línea de comandos y controles de teclado en la terminal."""

from __future__ import annotations

import argparse
import os
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
import webbrowser
from pathlib import Path

from . import __version__
from .caster import (
    CastError, Caster, looks_like_host, discover, local_ip_towards,
)
from .probe import ProbeError, probe
from .server import serve
from .session import Session
from .subtitles import collect_tracks

# --- salida con color -----------------------------------------------------------

_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t): return _c(t, "1")
def dim(t): return _c(t, "2")
def cyan(t): return _c(t, "36")
def green(t): return _c(t, "32")
def yellow(t): return _c(t, "33")
def red(t): return _c(t, "31")


def progress(text: str) -> None:
    """Avisa en qué paso estamos.

    En una terminal se sobreescribe con \\r para no ensuciar; fuera de una
    terminal se imprime como línea normal, porque si el programa se cuelga el
    último mensaje visible es la única pista de dónde fue.
    """
    if _TTY:
        print(dim(text), end="\r", flush=True)
    else:
        print(text, flush=True)


def clear_progress() -> None:
    if _TTY:
        print(" " * 46, end="\r", flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"\n{red('✗')} {message}", file=sys.stderr)
    sys.exit(code)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(value: str) -> float:
    """Acepta 90, 1:30 o 1:02:03."""
    value = value.strip().rstrip("s")
    if not value:
        return 0.0
    parts = value.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Tiempo inválido: {value!r}") from exc
    total = 0.0
    for num in nums:
        total = total * 60 + num
    return total


# --- argumentos -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ltc",
        description="Castea un video a tu Chromecast con subtítulos, de una.",
        epilog=(
            "Ejemplos:\n"
            "  ltc pelicula.mkv                  castea con subtítulos en español\n"
            "  ltc serie.mkv -s 2                usa la pista de subtítulos #2\n"
            "  ltc pelicula.mp4 -d \"TV Living\"   elige el dispositivo\n"
            "  ltc pelicula.mkv --list-tracks    muestra las pistas y sale\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("video", nargs="?", help="Archivo de video a transmitir")
    p.add_argument("-d", "--device", metavar="NOMBRE",
                   help="Nombre del Chromecast (alcanza una parte), o su IP")
    p.add_argument("-s", "--sub", metavar="N|ARCHIVO",
                   help="Subtítulo a usar: el número que muestra --list-tracks, "
                        "o directamente un archivo .srt/.ass")
    p.add_argument("--no-subs", action="store_true", help="Arrancar sin subtítulos")
    p.add_argument("--burn", type=int, metavar="N",
                   help="Quemar en el video la pista N (para subtítulos de imagen)")
    p.add_argument("--sub-lang", default="spa", metavar="COD",
                   help="Idioma preferido al elegir subtítulo solo (default: spa)")
    p.add_argument("--sub-scale", type=float, default=1.0, metavar="F",
                   help="Tamaño de los subtítulos, 1.0 = normal")
    p.add_argument("--sub-encoding", metavar="COD",
                   help="Forzar la codificación de los .srt externos (ej: cp1252, utf-8)")
    p.add_argument("--delay", type=float, default=0.0, metavar="SEG",
                   help="Desfase inicial de los subtítulos, en segundos")
    p.add_argument("-a", "--audio", type=int, metavar="N",
                   help="Número de pista de audio")
    p.add_argument("-t", "--start", type=parse_time, default=0.0, metavar="TIEMPO",
                   help="Arrancar en un punto: 90, 1:30 o 1:02:03")
    p.add_argument("-p", "--port", type=int, default=0,
                   help="Puerto del servidor local (default: uno libre)")
    p.add_argument("--transcode", action="store_true",
                   help="Forzar recodificación aunque parezca compatible")
    p.add_argument("--direct", action="store_true",
                   help="Forzar reproducción directa sin pasar por ffmpeg")
    p.add_argument("--surround", action="store_true",
                   help="No bajar el audio multicanal a estéreo")
    p.add_argument("--no-browser", action="store_true",
                   help="No abrir el panel web automáticamente")
    p.add_argument("--list-devices", action="store_true",
                   help="Listar los Chromecast de la red y salir")
    p.add_argument("--list-tracks", action="store_true",
                   help="Listar las pistas del archivo y salir")
    p.add_argument("--timeout", type=float, default=6.0, metavar="SEG",
                   help="Tiempo de búsqueda de dispositivos")
    p.add_argument("-V", "--version", action="version", version=f"ltc {__version__}")
    return p


# --- listados -------------------------------------------------------------------

def cmd_list_devices(timeout: float) -> int:
    print(dim("Buscando Chromecasts…"))
    devices = discover(timeout=timeout)
    if not devices:
        print(yellow("No encontré ninguno. ¿Estás en la misma red Wi-Fi?"))
        return 1
    print()
    for d in devices:
        print(f"  {bold(d.name)}  {dim(d.model)}  {dim(d.host)}")
    print()
    return 0


def cmd_list_tracks(path: str, prefer: str = "spa") -> int:
    media = probe(path)
    print(f"\n{bold(media.path.name)}   "
          f"{dim(fmt_time(media.duration))}  {dim(media.container.split(',')[0])}\n")

    for v in media.video:
        print(f"  {cyan('video')}   {v.codec} {v.width}x{v.height} "
              f"{dim(v.profile)} {dim(f'{v.fps:.0f}fps' if v.fps else '')}")
    for i, a in enumerate(media.audio):
        mark = green(" ←") if a.default else ""
        print(f"  {cyan('audio')}   {bold(f'-a {i}')}  {a.codec} · {a.label()}{mark}")

    tracks = collect_tracks(media, prefer=prefer)
    if not tracks:
        print(f"  {dim('subs    (ninguno)')}")
    for t in tracks:
        flag = red(" · no convertible, usá --burn") if t.bitmap else ""
        origin = "embebido" if t.source == "embedded" else t.path.name
        # Cuando la pista no declara idioma su etiqueta ya es el nombre del
        # archivo, así que no tiene sentido repetirlo.
        detail = t.label if t.label == origin else f"{t.label} · {dim(origin)}"
        print(f"  {cyan('subs')}    {bold(f'-s {t.id}')}  {detail}{flag}")
    print()
    return 0


# --- selección de dispositivo ---------------------------------------------------

def choose_device(name: str | None, timeout: float):
    progress(f"Consultando {name}…" if name and looks_like_host(name)
             else "Buscando Chromecasts…")
    devices = discover(timeout=timeout, name=name)
    clear_progress()

    if not devices:
        if name:
            die(f"No encontré ningún Chromecast que coincida con «{name}». "
                f"Probá con: ltc --list-devices")
        die("No encontré ningún Chromecast en la red.\n"
            "  Verificá que estés en la misma Wi-Fi y que el dispositivo esté prendido.")

    playable = [d for d in devices if d.cast_type != "audio"]
    if not playable:
        die("Sólo encontré dispositivos de audio, que no pueden mostrar video.")

    if len(playable) == 1:
        return playable[0]

    print(f"\n{bold('Encontré varios Chromecast:')}\n")
    for i, d in enumerate(playable, 1):
        print(f"  {bold(str(i))}. {d.name}  {dim(d.model)}")
    print()
    while True:
        try:
            answer = input("¿Cuál? [1] ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            sys.exit(130)
        if answer.isdigit() and 1 <= int(answer) <= len(playable):
            return playable[int(answer) - 1]
        print(yellow("Número inválido."))


# --- controles de teclado -------------------------------------------------------

HELP = ("espacio pausa · ←/→ ±10s · ↓/↑ ±30s · j/k volumen · "
        "s subtítulos · [ ] sincronía · m mudo · q salir")


class Keyboard:
    """Lee teclas sueltas sin Enter, si hay terminal interactiva."""

    def __init__(self) -> None:
        self.enabled = sys.stdin.isatty()
        self._saved = None

    def __enter__(self) -> "Keyboard":
        if self.enabled:
            try:
                self._saved = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except (termios.error, ValueError):
                self.enabled = False
        return self

    def __exit__(self, *exc) -> None:
        self.restore()

    def restore(self) -> None:
        if self._saved is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)
            except (termios.error, ValueError):
                pass
            self._saved = None

    def read(self, timeout: float = 0.25) -> str | None:
        """Devuelve la tecla, o None si no hubo nada en `timeout` segundos."""
        if not self.enabled:
            time.sleep(timeout)
            return None
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return None

        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        # Secuencia de escape: flechas y compañía.
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            return "\x1b"
        seq = sys.stdin.read(2)
        return {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT"}.get(seq, "")


def render_status(session: Session, message: str = "") -> None:
    if not _TTY:
        return
    snap = session.snapshot()
    icon = {"PLAYING": green("▶"), "PAUSED": yellow("❚❚"),
            "BUFFERING": dim("⋯")}.get(snap["state"], dim("·"))

    pos, dur = fmt_time(snap["position"]), fmt_time(snap["duration"])
    bar_width = 24
    frac = snap["position"] / snap["duration"] if snap["duration"] else 0
    filled = max(0, min(bar_width, int(frac * bar_width)))
    bar = cyan("━" * filled) + dim("─" * (bar_width - filled))

    sub = next((t for t in session.usable_tracks() if t.id == session.active_sub), None)
    sub_txt = sub.label.split(" · ")[0] if sub else "off"
    if session.sub_delay:
        sub_txt += f" {session.sub_delay:+.1f}s"

    vol = f"{int(snap['volume'] * 100)}%" if not snap["muted"] else "mudo"
    line = (f"  {icon} {bar} {pos}/{dur}  "
            f"{dim('vol')} {vol}  {dim('subs')} {sub_txt}")
    if message:
        line += f"   {yellow(message)}"

    width = shutil.get_terminal_size((100, 24)).columns
    sys.stdout.write("\r\033[2K" + line[: width + 40])
    sys.stdout.flush()


def control_loop(session: Session) -> None:
    """Bucle principal: dibuja el estado y atiende el teclado."""
    message, message_until = "", 0.0
    saw_playback = False

    def notify(text: str) -> None:
        nonlocal message, message_until
        message, message_until = text, time.monotonic() + 2.0

    with Keyboard() as kb:
        while True:
            if session.quit_requested.is_set():
                print()
                print(dim("  Cerrado desde el panel web."))
                return

            key = kb.read(0.25)

            if message and time.monotonic() > message_until:
                message = ""

            try:
                if key in ("q", "Q", "\x03", "\x04"):
                    break
                elif key == " " or key in ("p", "P"):
                    session.toggle()
                elif key == "LEFT":
                    session.skip(-10)
                elif key == "RIGHT":
                    session.skip(10)
                elif key == "DOWN":
                    session.skip(-30)
                elif key == "UP":
                    session.skip(30)
                elif key in ("k", "K"):
                    session.set_volume(min(1.0, session.caster.volume + 0.05))
                elif key in ("j", "J"):
                    session.set_volume(max(0.0, session.caster.volume - 0.05))
                elif key in ("m", "M"):
                    session.set_muted(not session.caster.muted)
                elif key in ("s", "S"):
                    session.cycle_subtitle()
                    active = next((t for t in session.usable_tracks()
                                   if t.id == session.active_sub), None)
                    notify(f"subtítulos: {active.label if active else 'desactivados'}")
                elif key == "[":
                    session.nudge_delay(-0.1)
                    notify(f"sincronía {session.sub_delay:+.1f}s")
                elif key == "]":
                    session.nudge_delay(0.1)
                    notify(f"sincronía {session.sub_delay:+.1f}s")
                elif key == "0":
                    session.set_delay(0.0)
                    notify("sincronía a cero")
            except Exception as exc:
                notify(f"error: {exc}")

            state = session.caster.player_state
            if state in ("PLAYING", "BUFFERING", "PAUSED"):
                saw_playback = True
            elif saw_playback and state == "IDLE":
                status = session.caster.status
                reason = getattr(status, "idle_reason", None) if status else None
                if reason in ("FINISHED", "CANCELLED", "ERROR"):
                    print()
                    print(dim("  Reproducción terminada." if reason == "FINISHED"
                              else f"  El receptor cortó ({reason})."))
                    return

            render_status(session, message)


# --- programa principal ---------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    media = probe(args.video)

    device = choose_device(args.device, args.timeout)

    progress(f"Conectando a {device.name}…")
    try:
        caster = Caster(device)
    except CastError as exc:
        die(str(exc))
    clear_progress()

    # La pista a quemar se resuelve antes de armar el plan: cambia el pipeline.
    burn_track = None
    if args.burn is not None:
        tracks = collect_tracks(media, prefer=args.sub_lang)
        burn_track = next((t for t in tracks if t.id == args.burn), None)
        if burn_track is None:
            die(f"No existe el subtítulo #{args.burn}. Mirá: ltc «archivo» --list-tracks")

    # -s acepta tanto el número de pista como la ruta a un .srt.
    sub_choice, sub_file = None, None
    if args.sub:
        if args.sub.isdigit():
            sub_choice = int(args.sub)
        else:
            sub_file = Path(args.sub).expanduser()
            if not sub_file.is_file():
                die(f"No existe el archivo de subtítulos: {args.sub}")

    audio_stream = None
    if args.audio is not None:
        if not 0 <= args.audio < len(media.audio):
            die(f"No existe la pista de audio #{args.audio}. "
                f"El archivo tiene {len(media.audio)}.")
        audio_stream = media.audio[args.audio]

    force = "transcode" if args.transcode else "direct" if args.direct else "auto"

    try:
        session = Session.create(
            media, caster,
            audio_stream=audio_stream, burn_sub=burn_track, force=force,
            surround=args.surround, sub_lang=args.sub_lang,
            sub_choice=sub_choice, sub_file=sub_file,
            no_subs=args.no_subs, sub_scale=args.sub_scale,
            sub_encoding=args.sub_encoding,
        )
    except ValueError as exc:
        caster.disconnect()
        die(str(exc))

    session.sub_delay = args.delay

    # El server escucha en todas las interfaces, pero la URL que le pasamos al
    # Chromecast tiene que ser la IP nuestra que él pueda alcanzar.
    http = serve(session, port=args.port)
    ip = local_ip_towards(device.host)
    port = http.server_address[1]
    session.base_url = f"http://{ip}:{port}"

    _print_summary(session, device, session.base_url)

    # SIGTERM tiene que cerrar igual de prolijo que la 'q': el bucle de control
    # ya sabe salir cuando se levanta esta bandera. (SIGINT ya llega como
    # KeyboardInterrupt por el manejador default, incluso en modo cbreak.)
    signal.signal(signal.SIGTERM, lambda *_: session.quit_requested.set())

    try:
        session.start(args.start)
        if not caster.wait_for_session(timeout=25):
            print(yellow("\n  El receptor tardó en responder; sigo esperando…"))

        if not args.no_browser and os.environ.get("DISPLAY", os.environ.get("WAYLAND_DISPLAY")):
            threading.Thread(
                target=lambda: webbrowser.open(session.base_url), daemon=True
            ).start()

        control_loop(session)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n" + dim("  Cerrando…"))
        try:
            session.stop()
            caster.quit_app()
        except Exception:
            pass
        http.shutdown_all()
        http.shutdown()
        caster.disconnect()

    return 0


def _print_summary(session: Session, device, panel_url: str) -> None:
    media, plan = session.media, session.plan
    video = media.video[0]

    print()
    print(f"  {bold(media.path.name)}  {dim(fmt_time(media.duration))}")
    print(f"  {dim('→')} {bold(device.name)} {dim(f'({device.model})')}")
    print()

    mode_label = {"direct": green("directo, sin procesar"),
                  "remux": cyan("reempaquetado (sin recodificar)"),
                  "transcode": yellow("transcodificando")}[plan.mode]
    print(f"  {dim('modo')}   {mode_label}")
    print(f"  {dim('video')}  {video.codec} {video.width}x{video.height}"
          + (f" → {plan.video_codec}" if plan.reencodes_video else ""))
    if plan.audio_stream:
        audio = plan.audio_stream
        extra = " → aac" if plan.audio_codec != "copy" else ""
        print(f"  {dim('audio')}  {audio.codec} · {audio.label()}{extra}")

    for reason in plan.reasons:
        print(f"         {dim(reason)}")

    tracks = session.usable_tracks()
    print()
    if not tracks:
        print(f"  {dim('subs')}   {dim('el archivo no trae subtítulos de texto')}")
    for t in tracks:
        active = green(" ← activo") if t.id == session.active_sub else ""
        print(f"  {dim('subs')}   {bold(f'[{t.id}]')} {t.label}{active}")
    if session.active_sub is None and tracks:
        print(f"         {dim('ninguno activo — apretá s para elegir')}")

    print()
    print(f"  {dim('panel')}  {cyan(panel_url)}")
    print(f"  {dim(HELP)}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.list_devices:
            return cmd_list_devices(args.timeout)
        if not args.video:
            parser.print_help()
            return 1
        if args.list_tracks:
            return cmd_list_tracks(args.video, args.sub_lang)
        return run(args)
    except (ProbeError, CastError) as exc:
        die(str(exc))
    except KeyboardInterrupt:
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
