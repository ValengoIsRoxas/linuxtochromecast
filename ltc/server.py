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

from .session import Session
from .subtitles import SubtitleError
from .transcode import Transcoder
from .webui import PAGE

CHUNK = 256 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


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

    def new_transcoder(self) -> Transcoder:
        """Arranca un ffmpeg nuevo y descarta el anterior, si quedaba alguno."""
        session = self.session
        with self._tlock:
            if self.transcoder is not None:
                self.transcoder.stop()
            t = Transcoder(session.media, session.plan, session.stream_offset)
            t.start()
            self.transcoder = t
            return t

    def shutdown_all(self) -> None:
        with self._tlock:
            if self.transcoder is not None:
                self.transcoder.stop()
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
            return self._error(400, "JSON inválido")

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
            raise ValueError(f"Comando desconocido: {cmd!r}")

    # --- subtítulos ---

    def _serve_subtitle(self, path: str, query: dict) -> None:
        try:
            track_id = int(Path(path).stem)
        except ValueError:
            return self._error(400, "Id de subtítulo inválido")

        track = next((t for t in self.session.tracks if t.id == track_id), None)
        if track is None:
            return self._error(404, "Ese subtítulo no existe")

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
        if self.session.plan.seekable:
            self._serve_file()
        else:
            self._serve_pipe()

    def _serve_file(self) -> None:
        path = self.session.media.path
        try:
            size = path.stat().st_size
        except OSError as exc:
            return self._error(500, f"No pude abrir el archivo: {exc}")

        ctype = self.session.plan.content_type
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
        """Stream generado por ffmpeg al vuelo.

        Va con un Content-Length estimado y sin `Transfer-Encoding: chunked`:
        probado contra el hardware, el receptor rechaza el chunked para MP4 con
        LOAD_FAILED antes de mostrar un solo cuadro. Ver
        `Session.estimated_stream_size` para de dónde sale el número.
        """
        promised = self.session.estimated_stream_size()

        self.send_response(200)
        self.send_header("Content-Type", self.session.plan.content_type)
        self.send_header("Content-Length", str(promised))
        self.send_header("Accept-Ranges", "none")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        # Este socket queda dedicado al stream hasta que termine.
        self.close_connection = True

        if self.command == "HEAD":
            return

        transcoder = self.server.new_transcoder()
        sent = 0
        try:
            for block in transcoder.stream():
                # No pasarse de lo prometido: el cliente deja de leer ahí igual,
                # y así el cuerpo de la respuesta queda bien formado.
                if sent + len(block) > promised:
                    block = block[: promised - sent]
                if block and not self._write(block):
                    return
                sent += len(block)
                if sent >= promised:
                    break
        finally:
            transcoder.stop()


def serve(session: Session, host: str = "0.0.0.0", port: int = 0) -> CastHTTPServer:
    """Levanta el server en un hilo de fondo y devuelve la instancia."""
    server = CastHTTPServer((host, port), session)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True).start()
    return server
