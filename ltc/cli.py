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
from . import config
from .cache import ConversionCache
from .caster import (
    CastError, Caster, looks_like_host, discover, local_ip_towards,
)
from .library import Library, ProgressStore
from .libraryserver import serve_library
from .logutil import setup_logging
from .player import PlayOptions, Player
from .posters import Posters
from .probe import ProbeError, probe
from .server import serve
from .session import Session
from .subtitles import collect_tracks
from .term import _TTY, bold, cyan, dim, green, red, yellow
from .transcode import PrepareError, detect_hw_encoder, encode_whole

# --- salida con color -----------------------------------------------------------

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
        raise argparse.ArgumentTypeError(f"Invalid time: {value!r}") from exc
    total = 0.0
    for num in nums:
        total = total * 60 + num
    return total


# --- argumentos -----------------------------------------------------------------

# Subcomandos: se atienden antes del parser, que toma la primera palabra como
# el video. Un archivo que se llame igual se abre con ./config.
COMMANDS = ("config", "setup", "uninstall")


def build_parser(cfg: config.Config | None = None) -> argparse.ArgumentParser:
    """El parser, con los valores por defecto que salen de la configuración."""
    cfg = cfg or config.get()
    p = argparse.ArgumentParser(
        prog="ltc",
        description="Cast a video to your Chromecast with subtitles already working.",
        epilog=(
            "Examples:\n"
            "  ltc movie.mkv                     cast, picking subtitles automatically\n"
            "  ltc show.mkv -s 2                 use subtitle track #2\n"
            "  ltc movie.mp4 -d \"TV Living\"      pick the device\n"
            "  ltc movie.mkv --list-tracks       list the file's tracks and exit\n"
            "  ltc ~/Movies                      browse a folder from your phone\n"
            "  ltc                               browse the folder set in the configuration\n"
            "\n"
            "Other commands:\n"
            "  ltc setup        guided setup: folders, device, firewall, file manager\n"
            "  ltc config       show or change the configuration\n"
            "  ltc uninstall    remove ltc and what it set up\n"
            "\n"
            "Defaults come from the configuration file (ltc config); these options "
            "override it for one run.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("video", nargs="?",
                   help="Video file to cast, or a folder to browse as a library "
                        "(none: the library folder from the configuration)")
    p.add_argument("-d", "--device", metavar="NAME", default=cfg.device or None,
                   help="Chromecast name (a partial match works), or its IP")
    p.add_argument("-s", "--sub", metavar="N|FILE",
                   help="Subtitle to use: the number shown by --list-tracks, "
                        "or a .srt/.ass file directly")
    p.add_argument("--no-subs", action="store_true", help="Start with subtitles off")
    p.add_argument("--burn", type=int, metavar="N",
                   help="Burn track N into the video (for image-based subtitles)")
    p.add_argument("--sub-lang", default=cfg.sub_lang, metavar="CODE",
                   help="Preferred language when auto-selecting a subtitle "
                        f"(default: {cfg.sub_lang})")
    p.add_argument("--sub-scale", type=float, default=cfg.sub_scale, metavar="F",
                   help="Subtitle size, 1.0 = normal")
    p.add_argument("--sub-encoding", metavar="CODE",
                   help="Force the encoding of external .srt files (e.g. cp1252, utf-8)")
    p.add_argument("--delay", type=float, default=0.0, metavar="SECONDS",
                   help="Initial subtitle offset, in seconds")
    p.add_argument("-a", "--audio", type=int, metavar="N",
                   help="Audio track number")
    p.add_argument("-t", "--start", type=parse_time, default=0.0, metavar="TIME",
                   help="Start at a given point: 90, 1:30 or 1:02:03")
    p.add_argument("-p", "--port", type=int, default=None,
                   help=f"Local server port (default: {cfg.port}, or any free "
                        f"one if it's taken; 0 = always a random one)")
    p.add_argument("--transcode", action="store_true",
                   help="Force re-encoding even if the file looks compatible")
    p.add_argument("--direct", action="store_true",
                   help="Force direct playback, bypassing ffmpeg")
    p.add_argument("--surround", action="store_true", default=cfg.surround,
                   help="Don't downmix multichannel audio to stereo")
    p.add_argument("--no-surround", dest="surround", action="store_false",
                   help=argparse.SUPPRESS)
    p.add_argument("--no-wait", action="store_true",
                   help="Start casting immediately instead of re-encoding the "
                        "whole file first (faster to start, no real seeking)")
    p.add_argument("--cache-size", type=float, default=cfg.cache_limit_gb, metavar="GB",
                   help="Keep finished conversions so the same file starts instantly "
                        f"next time, up to this much disk (default: {cfg.cache_limit_gb:g}; "
                        "0 = don't keep them)")
    p.add_argument("--no-hwaccel", dest="hwaccel", action="store_false",
                   help="Re-encode on the CPU even if a GPU could do it")
    p.add_argument("--hwaccel", dest="hwaccel", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-autoplay", dest="autoplay", action="store_false",
                   help="Library mode: don't start the next episode on its own")
    p.add_argument("--autoplay", dest="autoplay", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-posters", dest="posters", action="store_false",
                   help="Library mode: don't look up posters and links on IMDb")
    p.add_argument("--posters", dest="posters", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(hwaccel=cfg.hwaccel, autoplay=cfg.autoplay_next, posters=cfg.library_posters)
    p.add_argument("--no-browser", action="store_true",
                   help="Don't open the web panel automatically")
    p.add_argument("--list-devices", action="store_true",
                   help="List the Chromecasts on the network and exit")
    p.add_argument("--list-tracks", action="store_true",
                   help="List the file's tracks and exit")
    p.add_argument("--timeout", type=float, default=6.0, metavar="SECONDS",
                   help="How long to search for devices")
    p.add_argument("-V", "--version", action="version", version=f"ltc {__version__}")
    return p


# --- listados -------------------------------------------------------------------

def cmd_list_devices(timeout: float) -> int:
    print(dim("Looking for Chromecasts…"))
    devices = discover(timeout=timeout)
    if not devices:
        print(yellow("Found none. Are you on the same Wi-Fi network?"))
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
        print(f"  {dim('subs    (none)')}")
    for t in tracks:
        flag = red(" · not convertible, use --burn") if t.bitmap else ""
        origin = "embedded" if t.source == "embedded" else t.path.name
        # Cuando la pista no declara idioma su etiqueta ya es el nombre del
        # archivo, así que no tiene sentido repetirlo.
        detail = t.label if t.label == origin else f"{t.label} · {dim(origin)}"
        print(f"  {cyan('subs')}    {bold(f'-s {t.id}')}  {detail}{flag}")
    print()
    return 0


# --- selección de dispositivo ---------------------------------------------------

def choose_device(name: str | None, timeout: float):
    progress(f"Querying {name}…" if name and looks_like_host(name)
             else "Looking for Chromecasts…")
    devices = discover(timeout=timeout, name=name)
    clear_progress()

    if not devices:
        if name:
            die(f"Found no Chromecast matching \u00ab{name}\u00bb. "
                f"Try: ltc --list-devices")
        die("Found no Chromecast on the network.\n"
            "  Check that you're on the same Wi-Fi and that the device is on.")

    playable = [d for d in devices if d.cast_type != "audio"]
    if not playable:
        die("Only found audio-only devices, which can't display video.")

    if len(playable) == 1:
        return playable[0]

    print(f"\n{bold('Found several Chromecasts:')}\n")
    for i, d in enumerate(playable, 1):
        print(f"  {bold(str(i))}. {d.name}  {dim(d.model)}")
    print()
    while True:
        try:
            answer = input("Which one? [1] ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            sys.exit(130)
        if answer.isdigit() and 1 <= int(answer) <= len(playable):
            return playable[int(answer) - 1]
        print(yellow("Invalid number."))


# --- controles de teclado -------------------------------------------------------

HELP = ("space pause · ←/→ ±10s · ↓/↑ ±30s · j/k volume · "
        "s subtitles · [ ] sync · m mute · q quit")


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

    vol = f"{int(snap['volume'] * 100)}%" if not snap["muted"] else "muted"
    line = (f"  {icon} {bar} {pos}/{dur}  "
            f"{dim('vol')} {vol}  {dim('subs')} {sub_txt}")
    if message:
        line += f"   {yellow(message)}"

    width = shutil.get_terminal_size((100, 24)).columns
    sys.stdout.write("\r\033[2K" + line[: width + 40])
    sys.stdout.flush()


QUIT_KEYS = ("q", "Q", "\x03", "\x04")


def handle_key(session: Session, key: str | None, notify) -> None:
    """Las teclas de reproducción, compartidas por los dos modos."""
    if key == " " or key in ("p", "P"):
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
        notify(f"subtitles: {active.label if active else 'off'}")
    elif key == "[":
        session.nudge_delay(-0.1)
        notify(f"sync {session.sub_delay:+.1f}s")
    elif key == "]":
        session.nudge_delay(0.1)
        notify(f"sync {session.sub_delay:+.1f}s")
    elif key == "0":
        session.set_delay(0.0)
        notify("sync reset to zero")


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
                print(dim("  Closed from the web panel."))
                return

            key = kb.read(0.25)

            if message and time.monotonic() > message_until:
                message = ""

            if key in QUIT_KEYS:
                break
            try:
                handle_key(session, key, notify)
            except Exception as exc:
                notify(f"error: {exc}")

            if not session.caster.connected:
                # El socket con el receptor se murió sin avisar: sin esto nos
                # quedábamos mudos, sirviendo el pipe HTTP a nadie mientras
                # player_state se congelaba en lo último que vio.
                print()
                print(red("  Lost the connection to the Chromecast."))
                session.announce_end("Lost the connection to the Chromecast.")
                return

            state = session.caster.player_state
            if state in ("PLAYING", "BUFFERING", "PAUSED"):
                saw_playback = True
            elif saw_playback and state == "IDLE":
                status = session.caster.status
                reason = getattr(status, "idle_reason", None) if status else None
                if reason in ("FINISHED", "CANCELLED", "ERROR"):
                    print()
                    finished = reason == "FINISHED"
                    text = "Playback finished." if finished else f"The receiver stopped ({reason})."
                    print(dim(f"  {text}"))
                    session.announce_end(text, ok=finished)
                    return

            render_status(session, message)


# --- programa principal ---------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    setup_logging()
    media = probe(args.video)

    device = choose_device(args.device, args.timeout)

    progress(f"Connecting to {device.name}…")
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
            die(f"There is no subtitle #{args.burn}. See: ltc \u00abfile\u00bb --list-tracks")

    # -s acepta tanto el número de pista como la ruta a un .srt.
    sub_choice, sub_file = None, None
    if args.sub:
        if args.sub.isdigit():
            sub_choice = int(args.sub)
        else:
            sub_file = Path(args.sub).expanduser()
            if not sub_file.is_file():
                die(f"No such subtitle file: {args.sub}")

    audio_stream = None
    if args.audio is not None:
        if not 0 <= args.audio < len(media.audio):
            die(f"There is no audio track #{args.audio}. "
                f"The file has {len(media.audio)}.")
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

    # Reencodar entero antes de castear es lo que hace que el Content-Length
    # sea exacto: con el archivo terminado no hay nada que estimar, así que el
    # receptor nunca se queda esperando bytes que no existen (que es lo que lo
    # hacía cortar cerca del final). Encima queda seek real.
    # Y lo que ya se encodeó una vez queda guardado: volver a ver el mismo
    # capítulo arranca al instante en vez de repetir todo el trabajo.
    cache = ConversionCache(args.cache_size)
    if session.plan.mode == "transcode":
        session.prepared = cache.lookup(media, session.plan)
        if session.prepared is not None:
            print(f"\n  {green('✓')} {dim('using the conversion saved last time')}")
        elif not args.no_wait:
            try:
                encoded = _encode_ahead(media, session.plan, not args.hwaccel)
            except PrepareError as exc:
                caster.disconnect()
                die(str(exc))
            session.prepared = cache.adopt(encoded, media, session.plan)

    # El server escucha en todas las interfaces, pero la URL que le pasamos al
    # Chromecast tiene que ser la IP nuestra que él pueda alcanzar.
    http = _bind(lambda port: serve(session, port=port), args.port)
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
            print(yellow("\n  The receiver is slow to respond; still waiting…"))

        if not args.no_browser and os.environ.get("DISPLAY", os.environ.get("WAYLAND_DISPLAY")):
            threading.Thread(
                target=lambda: webbrowser.open(session.base_url), daemon=True
            ).start()

        control_loop(session)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n" + dim("  Closing…"))
        try:
            session.stop()
            caster.quit_app()
        except Exception:
            pass
        http.shutdown_all()
        http.shutdown()
        caster.disconnect()
        # El reencodado pesa más de un giga: si no quedó en la caché, no lo
        # dejamos tirado.
        if session.prepared is not None and not cache.contains(session.prepared):
            try:
                session.prepared.unlink(missing_ok=True)
            except OSError:
                pass

    return 0


# --- modo biblioteca ------------------------------------------------------------

def run_library(args: argparse.Namespace) -> int:
    """`ltc <carpeta>`: queda corriendo y el panel elige qué ver."""
    setup_logging()
    root = Path(args.video).expanduser().resolve()

    progress(f"Scanning {root}…")
    library = Library.scan(root)
    clear_progress()
    if not len(library):
        die(f"Found no videos in {root}.")

    device = choose_device(args.device, args.timeout)
    progress(f"Connecting to {device.name}…")
    try:
        caster = Caster(device)
    except CastError as exc:
        die(str(exc))
    clear_progress()

    force = "transcode" if args.transcode else "direct" if args.direct else "auto"
    player = Player(caster, library, ProgressStore(), PlayOptions(
        force=force, surround=args.surround, sub_lang=args.sub_lang,
        sub_scale=args.sub_scale, sub_encoding=args.sub_encoding,
        autoplay_next=args.autoplay, hwaccel=args.hwaccel,
    ), cache=ConversionCache(args.cache_size),
       posters=Posters(enabled=args.posters))
    http = _bind(lambda port: serve_library(player, port=port), args.port)
    ip = local_ip_towards(device.host)
    base_url = f"http://{ip}:{http.server_address[1]}"
    player.attach(http, base_url)

    episodes = sum(len(s.episodes) for s in library.shows)
    print()
    print(f"  {bold('Library')}  {dim(str(root))}")
    print(f"  {dim(f'{len(library.movies)} movies · {len(library.shows)} series ({episodes} episodes)')}")
    print(f"  {dim('→')} {bold(device.name)} {dim(f'({device.model})')}")
    hw = detect_hw_encoder() if args.hwaccel else None
    print(f"  {dim('re-encode')}  {hw.label if hw else 'CPU (x264)'}")
    print()
    print(f"  {dim('panel')}  {cyan(base_url)}  {dim('open it on your phone')}")
    print(f"  {dim('log')}    {dim(str(config.get().log_path))}")
    print(f"  {dim(HELP)}")
    print()

    signal.signal(signal.SIGTERM, lambda *_: player.quit_requested.set())

    try:
        if not args.no_browser and os.environ.get("DISPLAY", os.environ.get("WAYLAND_DISPLAY")):
            threading.Thread(target=lambda: webbrowser.open(base_url), daemon=True).start()
        library_loop(player)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n" + dim("  Closing…"))
        try:
            player.close()
            if player.played_anything:
                caster.quit_app()
        except Exception:
            pass
        http.shutdown_all()
        http.shutdown()
        caster.disconnect()

    return 0


def library_loop(player: Player) -> None:
    """Como `control_loop`, pero la sesión va y viene según lo que elijan."""
    message, message_until = "", 0.0

    def notify(text: str) -> None:
        nonlocal message, message_until
        message, message_until = text, time.monotonic() + 2.0

    with Keyboard() as kb:
        while not player.quit_requested.is_set():
            key = kb.read(0.25)
            if message and time.monotonic() > message_until:
                message = ""
            if key in QUIT_KEYS:
                return

            session = player.session
            if session is not None and key:
                try:
                    handle_key(session, key, notify)
                except Exception as exc:
                    notify(f"error: {exc}")

            if not player.caster.connected:
                print()
                print(red("  Lost the connection to the Chromecast."))
                return

            if session is not None:
                render_status(session, message)
            elif _TTY:
                loading, pending = player.loading, player.pending
                progress = player.converter.progress(pending.key) if pending else None
                if loading:
                    line = f"  {dim('⋯')} starting {loading.label}"
                elif pending and progress:
                    done = progress["end"] / progress["duration"] if progress["duration"] else 0
                    line = (f"  {dim('⋯')} converting {pending.item.label} "
                            f"{done * 100:.0f}%  {dim('plays when ready')}")
                else:
                    line = f"  {dim('·')} {dim('waiting for something to play')}"
                if player.error:
                    line += f"   {yellow(player.error)}"
                sys.stdout.write("\r\033[2K" + line)
                sys.stdout.flush()

    print()
    print(dim("  Closed from the web panel."))


def _bind(start_server, port: int | None):
    """Levanta el servidor en el puerto pedido, o en el de siempre.

    Si el de siempre está ocupado (otro `ltc` corriendo, por ejemplo) se usa
    cualquiera libre en vez de morir: la dirección cambia, pero se avisa.
    Un puerto pedido con -p que está ocupado sí es un error.
    """
    preferred = config.get().port
    try:
        return start_server(preferred if port is None else port)
    except OSError as exc:
        if port is not None:
            die(f"Can't use port {port}: {exc.strerror or exc}")
        print(yellow(f"  Port {preferred} is taken; using a random one, "
                     "so the panel address will differ this time."))
        return start_server(0)


def _encode_ahead(media, plan, no_hwaccel: bool):
    """Reencoda la película entera mostrando el avance."""
    hw = None if no_hwaccel else detect_hw_encoder()
    where = hw.label if hw else "CPU"
    total = media.duration or 0.0
    started = time.monotonic()

    print()
    print(f"  {dim('Re-encoding first so playback is exact and seekable')} "
          f"{dim(f'({where})')}")

    def on_progress(done: float) -> None:
        elapsed = time.monotonic() - started
        if not total:
            progress(f"  encoding… {fmt_time(done)}")
            return
        frac = max(0.0, min(1.0, done / total))
        speed = (done / elapsed) if elapsed > 0.5 else 0.0
        eta = ((total - done) / speed) if speed > 0 else 0.0
        bar = int(frac * 24)
        print(f"\r  [{'█' * bar}{'·' * (24 - bar)}] {frac * 100:4.1f}%"
              f"  {speed:.0f}x  ETA {fmt_time(eta)}   ",
              end="", flush=True)

    out = encode_whole(media, plan, on_progress=on_progress, hw=hw)
    took = time.monotonic() - started
    size = out.stat().st_size
    print(f"\r  {green('✓')} encoded in {fmt_time(took)}"
          f" {dim(f'({size / 1e9:.2f} GB, {(total / took) if took else 0:.0f}x realtime)')}"
          f"{' ' * 20}")
    return out


def _print_summary(session: Session, device, panel_url: str) -> None:
    media, plan = session.media, session.plan
    video = media.video[0]

    print()
    print(f"  {bold(media.path.name)}  {dim(fmt_time(media.duration))}")
    print(f"  {dim('→')} {bold(device.name)} {dim(f'({device.model})')}")
    print()

    mode_label = {"direct": green("direct, no processing"),
                  "remux": cyan("remuxed (no re-encoding)"),
                  "transcode": yellow("transcoding")}[plan.mode]
    print(f"  {dim('mode')}   {mode_label}")
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
        print(f"  {dim('subs')}   {dim('this file has no text subtitles')}")
    for t in tracks:
        active = green(" ← active") if t.id == session.active_sub else ""
        print(f"  {dim('subs')}   {bold(f'[{t.id}]')} {t.label}{active}")
    if session.active_sub is None and tracks:
        print(f"         {dim('none active — press s to pick one')}")

    print()
    print(f"  {dim('panel')}  {cyan(panel_url)}")
    print(f"  {dim('log')}    {dim(str(config.get().log_path))}")
    print(f"  {dim(HELP)}")
    print()


def main(argv: list[str] | None = None) -> int:
    # Sin terminal (lanzado desde el gestor de archivos, o con la salida a un
    # archivo) Python junta la salida en bloques: la dirección del panel recién
    # aparecía al cerrar el programa.
    if not _TTY and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    argv = sys.argv[1:] if argv is None else argv

    try:
        if argv and argv[0] in COMMANDS:
            # Se importa acá: el instalador no hace falta para castear.
            from . import installer
            return getattr(installer, f"cmd_{argv[0]}")(argv[1:])
        parser = build_parser()
    except config.ConfigError as exc:
        die(f"{exc}\n  Fix it, or see: ltc config path")
    args = parser.parse_args(argv)

    try:
        if args.list_devices:
            return cmd_list_devices(args.timeout)
        if not args.video:
            folder = config.get().library_path
            if folder is None:
                parser.print_help()
                print(f"\n{dim('Tip: ltc setup picks a library folder, so plain `ltc` opens it.')}")
                return 1
            if not folder.is_dir():
                die(f"The library folder from the configuration doesn't exist: {folder}\n"
                    "  Change it with: ltc config set library.folder PATH")
            args.video = str(folder)
        if Path(args.video).expanduser().is_dir():
            return run_library(args)
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
