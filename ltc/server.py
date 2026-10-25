"""Servidor HTTP local: le sirve el video y los subtítulos al Chromecast.

Dos detalles que no son opcionales:

  * CORS. El receptor default baja los .vtt por XHR y sin
    `Access-Control-Allow-Origin` los descarta en silencio. Es la causa número
    uno de "casteo bien pero no aparecen los subtítulos".
  * Range. En direct play hay que soportar peticiones parciales o no hay seek
    ni arranque rápido.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .logutil import get_logger
from .session import Session
from .subtitles import SubtitleError
from .transcode import Transcoder
from .webui import PAGE

CHUNK = 256 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
log = get_logger("server")


class CastHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # El Chromecast corta la conexión de golpe al cambiar de stream; no queremos
    # que eso mate el server.
    protocol_version = "HTTP/1.1"

    def __init__(self, addr, session: Session):
        self.session = session
        self.transcoder: Transcoder | None = None
        self._tlock = threading.Lock()
        super().__init__(addr, CastRequestHandler)

    def transcoder_for(self, offset: float) -> Transcoder:
        """Devuelve el ffmpeg que corresponde a `offset`, reusándolo si ya está.

        Reusarlo es el punto: una reconexión del receptor pide el mismo offset
        y antes eso mataba al proceso que venía bien para arrancar otro de
        cero, con lo cual el receptor recibía otra vez el arranque en vez de la
        continuación. Sólo arrancamos uno nuevo cuando el offset cambió de
        verdad (un seek).
        """
        with self._tlock:
            current = self.transcoder
            if current is not None and abs(current.offset - offset) < 0.001 \
                    and current.path is not None and current.usable:
                return current
            if current is not None:
                current.cleanup()
            t = Transcoder(self.session.media, self.session.plan, offset)
            t.start()
            self.transcoder = t
            return t

    def shutdown_all(self) -> None:
        with self._tlock:
            if self.transcoder is not None:
                self.transcoder.cleanup()
                self.transcoder = None

    def handle_error(self, request, client_address) -> None:
        # Desconexiones del receptor son normales; no ensuciamos la terminal.
        pass


class CastRequestHandler(BaseHTTPRequestHandler):
    server: CastHTTPServer  # type: ignore[assignment]
    server_version = "linuxtochromecast"

    def log_message(self, fmt, *args):  # silencio
        pass

    # --- helpers ---

    @property
    def session(self) -> Session:
        return self.server.session

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self._write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"),
                   "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _write(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            return False

    # --- verbos ---

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8",
                       {"Cache-Control": "no-store"})
        elif path == "/media":
            self._serve_media()
        elif path.startswith("/sub/"):
            self._serve_subtitle(path, query)
        elif path == "/api/status":
            self.session.note_poll()
            self._json(self.session.snapshot())
        elif path == "/health":
            self._json({"ok": True})
        else:
            self._error(404, "No encontrado")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/cmd":
            return self._error(404, "No encontrado")

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._error(400, "Invalid JSON")

        try:
            self._run_command(payload)
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"{type(exc).__name__}: {exc}")

        self._json(self.session.snapshot())

    # --- comandos ---

    def _run_command(self, payload: dict) -> None:
        session = self.session
        cmd = payload.get("cmd")
        value = payload.get("value")

        if cmd == "toggle":
            session.toggle()
        elif cmd == "play":
            session.play()
        elif cmd == "pause":
            session.pause()
        elif cmd == "stop":
            session.stop()
        elif cmd == "retry":
            session.retry()
        elif cmd == "quit":
            session.stop()
            session.quit_requested.set()
        elif cmd == "seek":
            session.seek(float(value))
        elif cmd == "skip":
            session.skip(float(value))
        elif cmd == "volume":
            session.set_volume(float(value))
        elif cmd == "mute":
            session.set_muted(bool(value))
        elif cmd == "sub":
            session.set_subtitle(None if value in (None, "", "off") else int(value))
        elif cmd == "delay":
            session.set_delay(float(value))
        elif cmd == "nudge_delay":
            session.nudge_delay(float(value))
        else:
            raise ValueError(f"Unknown command: {cmd!r}")

    # --- subtítulos ---

    def _serve_subtitle(self, path: str, query: dict) -> None:
        try:
            track_id = int(Path(path).stem)
        except ValueError:
            return self._error(400, "Invalid subtitle id")

        track = next((t for t in self.session.tracks if t.id == track_id), None)
        if track is None:
            return self._error(404, "No such subtitle")

        # El delay viaja en la URL para que cambiarlo invalide el caché del
        # receptor; el offset del stream lo sabe la sesión.
        try:
            delay = float(query.get("d", ["0"])[0])
        except (TypeError, ValueError):
            delay = self.session.sub_delay

        shift = self.session.stream_offset + delay
        try:
            body = track.vtt(self.session.media.path, shift).encode("utf-8")
        except SubtitleError as exc:
            return self._error(500, str(exc))

        self._send(200, body, "text/vtt; charset=utf-8", {"Cache-Control": "no-store"})

    # --- video ---

    def _serve_media(self) -> None:
        prepared = self.session.prepared
        if prepared is not None:
            # Ya reencodado entero: se sirve como cualquier archivo, con el
            # tamaño exacto y Range de verdad.
            self._serve_file(prepared, "video/mp4")
        elif self.session.plan.seekable:
            self._serve_file()
        else:
            self._serve_pipe()

    def _serve_file(self, path: Path | None = None, ctype: str | None = None) -> None:
        path = path or self.session.media.path
        try:
            size = path.stat().st_size
        except OSError as exc:
            return self._error(500, f"Could not open the file: {exc}")

        ctype = ctype or self.session.plan.content_type
        start, end = 0, size - 1
        partial = False

        match = _RANGE.match(self.headers.get("Range", "") or "")
        if match:
            raw_start, raw_end = match.group(1), match.group(2)
            if raw_start:
                start = int(raw_start)
                if raw_end:
                    end = min(int(raw_end), size - 1)
            elif raw_end:
                start = max(0, size - int(raw_end))  # sufijo: bytes=-N
            partial = True

        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._cors()
        self.end_headers()

        if self.command == "HEAD":
            return

        remaining = length
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    data = fh.read(min(CHUNK, remaining))
                    if not data:
                        break
                    if not self._write(data):
                        return
                    remaining -= len(data)
        except OSError:
            return

    def _serve_pipe(self) -> None:
        """Sirve el MP4 que ffmpeg va escribiendo a un temporal.

        Acepta `Range`, y esa es la parte que importa: si el receptor corta y
        vuelve pidiendo desde el byte N, retomamos desde ahí en vez de matar
        ffmpeg y reencodar de cero el mismo tramo. Sin esto, cada reconexión
        reiniciaba el stream y el receptor terminaba sin datos, cortando la
        reproducción con MEDIA_SRC_NOT_SUPPORTED (104).

        El Content-Length sigue siendo estimado —el tamaño real recién se sabe
        cuando ffmpeg termina— y no se usa `Transfer-Encoding: chunked`: probado
        contra el hardware, el receptor lo rechaza para MP4 con LOAD_FAILED
        antes de mostrar un solo cuadro.
        """
        promised = self.session.estimated_stream_size()

        start = 0
        match = _RANGE.match(self.headers.get("Range", "") or "")
        if match and match.group(1):
            start = int(match.group(1))
        partial = bool(match)

        if start >= promised:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{promised}")
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", self.session.plan.content_type)
        self.send_header("Content-Length", str(promised - start))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range",
                             f"bytes {start}-{promised - 1}/{promised}")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        # Este socket queda dedicado al stream hasta que termine.
        self.close_connection = True

        if self.command == "HEAD":
            return

        transcoder = self.server.transcoder_for(self.session.stream_offset)
        pos = start
        client_gone = False
        starved = False
        try:
            while pos < promised:
                if not transcoder.wait_for(pos):
                    if transcoder.finished and not transcoder.killed \
                            and not transcoder.returncode:
                        # ffmpeg terminó bien pero la salida quedó más chica
                        # que lo prometido. Si dejamos el Content-Length sin
                        # cumplir, el receptor espera bytes que no existen y
                        # corta con 104 cerca del final: completamos.
                        transcoder.pad_to(promised)
                        if transcoder.wait_for(pos, timeout=5):
                            continue
                    # No va a haber más datos: cortamos en vez de colgarnos.
                    starved = not transcoder.finished or transcoder.killed
                    break
                block = transcoder.read_from(pos, min(CHUNK, promised - pos))
                if not block:
                    # Sin bytes pese a que `wait_for` dijo que había: el
                    # temporal desapareció (un seek lo reemplazó). Cortamos:
                    # insistir acá sería un bucle infinito.
                    starved = True
                    break
                if not self._write(block):
                    client_gone = True
                    return
                pos += len(block)
        finally:
            if pos < promised and not client_gone:
                if transcoder.killed:
                    cause = "lo matamos desde acá (cambió el offset)"
                elif transcoder.returncode:
                    cause = f"ffmpeg salió con código {transcoder.returncode}"
                elif starved:
                    cause = "se cortó el volcado a disco"
                else:
                    cause = "ffmpeg llegó al final de su salida"
                stderr = transcoder.error
                log.warning(
                    "El stream terminó en %d/%d bytes prometidos (offset=%.1fs, "
                    "%s): %s.%s",
                    pos, promised, self.session.stream_offset,
                    self.session.media.path.name, cause,
                    f" ffmpeg stderr: {stderr}" if stderr else "",
                )


def serve(session: Session, host: str = "0.0.0.0", port: int = 0) -> CastHTTPServer:
    """Levanta el server en un hilo de fondo y devuelve la instancia."""
    server = CastHTTPServer((host, port), session)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True).start()
    return server
