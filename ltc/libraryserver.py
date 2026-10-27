"""El servidor HTTP del modo biblioteca.

Es el mismo servidor de siempre —video, subtítulos, Range, CORS— con dos
diferencias: la sesión puede no existir (nadie eligió nada todavía) o cambiar
en cualquier momento, y suma las rutas de la biblioteca.

    /                  la biblioteca (libraryui.PAGE)
    /player            el control remoto de siempre, con una vuelta a la biblioteca
    /api/library       películas, series y "seguir viendo"
    /api/item/<id>     detalle de un video: pistas de audio y subtítulos
    /thumb/<id>        póster local o cuadro del video
    /poster/<id>       póster local o de IMDb, de una película o serie
    /api/item/<id>/subtitles   GET busca en OpenSubtitles, POST baja uno
    /api/item/<id>/subtitle-file   POST sube un subtítulo (arrastrar y soltar)
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import libraryui, webui
from .compat import DeviceCaps, plan_for
from .library import (
    Episode, Item, Library, Movie, Show, item_json, library_json, local_artwork,
    parse_movie,
)
from .logutil import get_logger
from .opensubtitles import OpenSubtitles, OpenSubtitlesError, api_language
from .player import Player
from .posters import Match
from .probe import ProbeError, probe
from .server import CastHTTPServer, CastRequestHandler
from .subtitles import (
    SUB_EXTENSIONS, SubtitleError, _guess_lang, collect_tracks, load_file, pick_default,
)

log = get_logger("library")

# El panel de siempre, con un enlace de vuelta a la biblioteca. Se inyecta en
# vez de editar webui.PAGE porque en el modo de un solo archivo no hay a dónde
# volver.
_PLAYER_PAGE = webui.PAGE.replace("</body>", libraryui.PLAYER_ADDON + "</body>", 1)

RESCAN_AFTER = 30.0
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024


class LibraryHTTPServer(CastHTTPServer):
    def __init__(self, addr, player: Player):
        self.player = player
        self.session = None
        self.transcoder = None
        self._tlock = threading.Lock()
        self._scan_lock = threading.Lock()
        self.opensubtitles = OpenSubtitles()
        ThreadingHTTPServer.__init__(self, addr, LibraryRequestHandler)

    def library(self, refresh: bool = False) -> Library:
        """La biblioteca, reescaneada si pasó un rato: aparecen descargas nuevas."""
        with self._scan_lock:
            current = self.player.library
            if refresh or time.time() - current.scanned_at > RESCAN_AFTER:
                try:
                    self.player.library = Library.scan(current.root)
                except OSError as exc:
                    log.warning("No se pudo reescanear %s: %s", current.root, exc)
            return self.player.library


class LibraryRequestHandler(CastRequestHandler):
    server: LibraryHTTPServer  # type: ignore[assignment]

    @property
    def player(self) -> Player:
        return self.server.player

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._page(libraryui.PAGE)
        elif path == "/player":
            self._page(_PLAYER_PAGE)
        elif path == "/api/status":
            self._json(self.player.snapshot())
        elif path == "/api/library":
            library = self.server.library(refresh="refresh" in query)
            self._json(library_json(library, self.player.store,
                                    self.player.cache.saved_sources()))
        elif path.startswith("/api/item/") and path.endswith("/subtitles"):
            self._subtitle_search(path.split("/")[3], query)
        elif path.startswith("/api/item/"):
            self._item_detail(path.rsplit("/", 1)[-1])
        elif path.startswith("/thumb/"):
            self._thumbnail(path.rsplit("/", 1)[-1])
        elif path.startswith("/poster/"):
            self._poster(path.rsplit("/", 1)[-1])
        elif path == "/media" or path.startswith("/sub/"):
            # El receptor puede pedir el video viejo justo después de cambiar de
            # sesión; sin sesión no hay nada que darle.
            if self.server.session is None:
                return self._error(404, "Nothing is playing")
            super().do_GET()
        elif path == "/health":
            self._json({"ok": True})
        else:
            self._error(404, "Not found")

    def _page(self, html: str) -> None:
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/item/") and parsed.path.endswith("/subtitle-file"):
            # El cuerpo es el archivo crudo, no JSON.
            return self._subtitle_upload(parsed.path.split("/")[3],
                                         urllib.parse.parse_qs(parsed.query))
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._error(400, "Invalid JSON")

        try:
            if parsed.path == "/api/play":
                self._play(payload)
            elif parsed.path == "/api/cancel":
                self.player.cancel_conversion(str(payload.get("id", "")))
            elif parsed.path.startswith("/api/item/") and parsed.path.endswith("/subtitles"):
                return self._subtitle_download(parsed.path.split("/")[3], payload)
            elif parsed.path == "/api/cmd":
                self._library_command(payload)
            elif parsed.path == "/api/quit":
                self.player.quit_requested.set()
            else:
                return self._error(404, "Not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            return self._error(500, f"{type(exc).__name__}: {exc}")

        self._json(self.player.snapshot())

    def _play(self, payload: dict) -> None:
        start = payload.get("start")
        audio = payload.get("audio")
        self.player.play(
            str(payload.get("id", "")),
            start=None if start is None else float(start),
            sub=payload.get("sub", "auto"),
            audio=None if audio is None else int(audio),
            wait=bool(payload.get("wait", False)),
        )

    def _library_command(self, payload: dict) -> None:
        cmd = payload.get("cmd")
        # En el panel "Stop and quit" cierra el programa; acá sólo corta el
        # video y vuelve a la biblioteca. Para cerrar está /api/quit.
        if cmd in ("stop", "quit"):
            return self.player.stop()
        if cmd == "next":
            return self.player.play_next()
        if self.server.session is None:
            raise ValueError("Nothing is playing.")
        self._run_command(payload)

    # --- detalle y miniaturas ---

    def _item_detail(self, item_id: str) -> None:
        library = self.player.library
        item = library.item(item_id)
        if item is None:
            return self._error(404, "No such video")
        try:
            media = probe(item.path)
        except ProbeError as exc:
            return self._error(500, str(exc))

        lang = self.player.options.sub_lang
        tracks = collect_tracks(media, prefer=lang, encoding=self.player.options.sub_encoding)
        text_tracks = [t for t in tracks if not t.bitmap]
        default = pick_default(text_tracks, lang)
        audio_default = media.default_audio()

        data = item_json(item, self.player.store)
        data.update(
            label=item.label,
            file=item.path.name,
            duration=media.duration,
            resume=self.player.store.resume_point(item.path),
            subs=[{"id": t.id, "label": t.label, "lang": t.lang, "source": t.source,
                   "file": t.path.name if t.path else None}
                  for t in text_tracks],
            opensubtitles={"search": self.server.opensubtitles.can_search,
                           "download": self.server.opensubtitles.can_download},
            # Las de imagen no se pueden mandar como texto; se avisan para que
            # no parezca que el archivo no tiene subtítulos.
            image_subs=sum(1 for t in tracks if t.bitmap),
            default_sub=default.id if default else None,
            audio=[{"index": i, "label": f"{a.codec} · {a.label()}",
                    "default": a is audio_default}
                   for i, a in enumerate(media.audio)],
        )
        match = self._imdb(item)
        data["links"] = match.links if match else None
        nxt = library.next_episode(item)
        data["next"] = item_json(nxt, self.player.store) if nxt else None

        # El mismo plan que armaría Session.create con el audio por defecto,
        # para saber si ya hay una conversión guardada de este video.
        device = self.player.caster.device
        opts = self.player.options
        plan = plan_for(media, DeviceCaps.for_model(device.model, device.cast_type),
                        force=opts.force, surround=opts.surround)
        data["mode"] = plan.mode
        data["saved"] = self.player.cache.lookup(media, plan) is not None
        data["conversion"] = self.player.conversion_for(item.id)
        self._json(data)

    # --- subtítulos arrastrados ---

    def _subtitle_upload(self, item_id: str, query: dict) -> None:
        item = self.player.library.item(item_id)
        if item is None:
            return self._error(404, "No such video")

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._error(400, "The file is empty.")
        if length > MAX_SUBTITLE_BYTES:
            return self._error(413, "That's too big to be a subtitle.")
        content = self.rfile.read(length)

        # Sólo el nombre: lo que mande el navegador no decide dónde se escribe.
        original = Path(query.get("name", ["subtitle.srt"])[0]).name
        ext = Path(original).suffix.lower()
        if ext not in SUB_EXTENSIONS:
            return self._error(400, f"Not a subtitle file ({ext or 'no extension'}). "
                                    f"Use one of: {', '.join(sorted(SUB_EXTENSIONS))}.")

        # Si el nombre dice el idioma ("Peli.es.srt") se conserva; si no, queda
        # como <video>.srt y la pista se llama por su archivo. Adivinar un
        # idioma que no se sabe sería peor que no decir nada.
        lang, forced = _guess_lang(original)
        stem = item.path.stem
        suffix = (f".{lang}" if lang != "und" else "") + (".forced" if forced else "")
        target = item.path.with_name(f"{stem}{suffix}{ext}")
        n = 2
        while target.exists():
            # El contador va antes del idioma: así lo lee find_external.
            target = item.path.with_name(f"{stem}.{n}{suffix}{ext}")
            n += 1

        try:
            target.write_bytes(content)
        except OSError as exc:
            return self._error(500, f"Could not save it next to the video: {exc}")
        # Que lo guardado se lea de verdad: un archivo que no se puede
        # convertir no sirve de pista y mejor enterarse ahora que al darle Play.
        try:
            cues = load_file(target, lang if lang != "und" else self.player.options.sub_lang,
                             self.player.options.sub_encoding)
            if not cues:
                raise SubtitleError("it has no subtitle lines in it")
        except SubtitleError as exc:
            target.unlink(missing_ok=True)
            return self._error(400, f"Couldn't read {original}: {exc}")
        log.info("Subtítulo arrastrado guardado en %s (%d líneas)", target.name, len(cues))
        self._json({"file": target.name, "cues": len(cues)})

    # --- OpenSubtitles ---

    def _subtitle_search(self, item_id: str, query: dict) -> None:
        item = self.player.library.item(item_id)
        if item is None:
            return self._error(404, "No such video")
        langs = query.get("languages", [""])[0]
        if langs:
            languages = [lang for lang in langs.lower().split(",") if lang]
        else:
            # El idioma preferido primero y el inglés de respaldo, que es
            # donde casi siempre hay algo.
            languages = [api_language(self.player.options.sub_lang)]
            if "en" not in languages:
                languages.append("en")
        osub = self.server.opensubtitles
        try:
            if isinstance(item, Episode):
                results = osub.search(item.path, title=item.show, season=item.season,
                                      episode=item.episode, languages=languages)
            else:
                results = osub.search(item.path, title=item.title, year=item.year,
                                      languages=languages)
        except OpenSubtitlesError as exc:
            return self._error(502, str(exc))
        self._json({"languages": languages, "results": results,
                    "can_download": osub.can_download})

    def _subtitle_download(self, item_id: str, payload: dict) -> None:
        item = self.player.library.item(item_id)
        if item is None:
            return self._error(404, "No such video")
        try:
            saved = self.server.opensubtitles.download(
                int(payload.get("file_id", 0)), item.path, str(payload.get("language", "")))
        except (OpenSubtitlesError, ValueError) as exc:
            return self._error(502, str(exc))
        except OSError as exc:
            return self._error(500, f"Could not save it next to the video: {exc}")
        self._json({"file": saved.name})

    def _thumbnail(self, target_id: str) -> None:
        library = self.player.library
        target = library.item(target_id) or library.show(target_id)
        if target is None:
            return self._error(404, "No such video")

        image = local_artwork(library, target)
        if image is None:
            video = target.episodes[0].path if hasattr(target, "episodes") else target.path
            image = self.player.thumbnailer.get(video)
        if image is None:
            return self._error(404, "No thumbnail")

        self._serve_image(image)

    def _poster(self, target_id: str) -> None:
        library = self.player.library
        target = library.item(target_id) or library.show(target_id)
        # Los capítulos no tienen póster propio: en la grilla van por cuadro.
        if target is None or isinstance(target, Episode):
            return self._error(404, "No such movie or series")
        image = local_artwork(library, target)
        if image is None:
            title, year, kind = _imdb_query(target)
            image = self.player.posters.image(title, year, kind)
        if image is None:
            return self._error(404, "No poster")
        self._serve_image(image)

    def _imdb(self, target: Item | Show) -> Match | None:
        return self.player.posters.lookup(*_imdb_query(target))

    def _serve_image(self, image: Path) -> None:
        ctype = {".png": "image/png", ".webp": "image/webp"}.get(
            image.suffix.lower(), "image/jpeg")
        self._serve_file(image, ctype)


def _imdb_query(target: Item | Show) -> tuple[str, int | None, str]:
    """Qué buscar en IMDb: la película, o la serie de un capítulo."""
    if isinstance(target, Movie):
        return target.title, target.year, "movie"
    # El nombre de la serie sale de la carpeta y puede traer el año: "Dark (2017)".
    title, year = parse_movie(target.name if isinstance(target, Show) else target.show)
    return title, year, "show"


def serve_library(player: Player, host: str = "0.0.0.0", port: int = 0) -> LibraryHTTPServer:
    server = LibraryHTTPServer((host, port), player)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3},
                     daemon=True).start()
    return server
