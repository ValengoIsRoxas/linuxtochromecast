"""Modo biblioteca: un Chromecast, muchos videos, una sesión a la vez.

En el modo normal `ltc` nace y muere con un archivo. Acá el programa queda
corriendo y el panel elige qué reproducir, así que alguien tiene que crear una
`Session` por cada elección, apuntar el servidor a ella, recordar dónde quedó
cada video y pasar al capítulo siguiente cuando termina uno. Ese alguien es el
`Player`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .cache import BackgroundConverter, ConversionCache, cacheable
from .caster import Caster
from .library import Episode, Item, Library, ProgressStore, Thumbnailer, item_json
from .logutil import get_logger
from .probe import ProbeError, probe
from .session import Session

log = get_logger("player")


@dataclass
class _Pending:
    """Un "Play" que espera a que termine la conversión para arrancar."""
    item: Item
    key: str
    start: float | None
    sub: int | str | None
    audio: int | None


@dataclass
class PlayOptions:
    """Las opciones de la línea de comandos que valen para todos los videos."""
    force: str = "auto"
    surround: bool = False
    sub_lang: str = "spa"
    sub_scale: float = 1.0
    sub_encoding: str | None = None
    autoplay_next: bool = True
    hwaccel: bool = True


class Player:
    def __init__(self, caster: Caster, library: Library, store: ProgressStore,
                 options: PlayOptions | None = None,
                 thumbnailer: Thumbnailer | None = None,
                 cache: ConversionCache | None = None):
        self.caster = caster
        self.library = library
        self.store = store
        self.options = options or PlayOptions()
        self.thumbnailer = thumbnailer or Thumbnailer()
        self.cache = cache or ConversionCache()
        self.converter = BackgroundConverter(self.cache, hwaccel=self.options.hwaccel)
        # Qué conversión en segundo plano le corresponde a cada video, para
        # mostrar su avance aunque ya no se esté reproduciendo.
        self._jobs: dict[str, str] = {}
        self.base_url = ""
        self.http = None  # LibraryHTTPServer; lo engancha `attach`

        self.session: Session | None = None
        self.item: Item | None = None
        self.loading: Item | None = None
        self.pending: _Pending | None = None
        self.error = ""
        self.played_anything = False
        self.quit_requested = threading.Event()

        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._saw_playback = False
        self._closing = threading.Event()
        self._watcher = threading.Thread(target=self._watch, daemon=True)

    def attach(self, http, base_url: str) -> None:
        self.http, self.base_url = http, base_url
        self._watcher.start()

    # --- elegir qué reproducir ---

    def play(self, item_id: str, *, start: float | None = None,
             sub: int | str | None = "auto", audio: int | None = None,
             wait: bool = False) -> None:
        """Arranca un video en segundo plano.

        Probar el archivo y extraer subtítulos embebidos tarda unos segundos;
        hacerlo dentro del pedido HTTP dejaría al panel colgado sin decir nada.
        `start=None` retoma donde había quedado; `sub` es "auto", "off" o el
        número de pista.

        Con `wait`, si hay que reencodar y no está guardado, no castea por el
        pipe: convierte el archivo entero y arranca solo cuando termina. Así
        se ve desde el principio con seek real, en vez de un stream que no se
        puede adelantar.
        """
        item = self.library.item(item_id)
        if item is None:
            raise ValueError("That video is no longer in the library.")
        with self._lock:
            # Elegir otra cosa reemplaza cualquier "Play" que estaba esperando.
            self.loading, self.error, self.pending = item, "", None
        threading.Thread(target=self._start, args=(item, start, sub, audio, wait),
                         daemon=True).start()

    def cancel_conversion(self, item_id: str) -> None:
        with self._lock:
            key = self._jobs.pop(item_id, None)
            if self.pending is not None and self.pending.item.id == item_id:
                self.pending = None
        self.converter.cancel(key)

    def play_next(self) -> None:
        with self._lock:
            nxt = self.library.next_episode(self.item) if self.item else None
            sub = self._sub_preference()
        if nxt is None:
            raise ValueError("There is no next episode.")
        self.play(nxt.id, start=0.0, sub=sub)

    def _sub_preference(self) -> str:
        """Para el capítulo siguiente: mismo idioma, o apagados si estaban apagados."""
        session = self.session
        if session is None:
            return "auto"
        if session.active_sub is None:
            return "off"
        track = next((t for t in session.usable_tracks() if t.id == session.active_sub), None)
        return f"lang:{track.lang}" if track and track.lang != "und" else "auto"

    def _start(self, item: Item, start: float | None, sub, audio: int | None,
               wait: bool = False) -> None:
        # Un solo arranque a la vez: dos toques seguidos en el panel no tienen
        # que terminar con dos LOAD peleándose por el receptor.
        with self._load_lock:
            try:
                session = self._build_session(item, sub, audio)
            except (ProbeError, ValueError, OSError) as exc:
                log.warning("No se pudo abrir %s: %s", item.path, exc)
                with self._lock:
                    if self.loading is item:
                        self.loading, self.error = None, str(exc)
                return

            with self._lock:
                if self.loading is not item:
                    return  # mientras probábamos eligieron otra cosa
                key = self._jobs.get(item.id)
                if wait and session.prepared is None and cacheable(session.plan) and key:
                    # Lo que se esté viendo sigue; esto arranca cuando termine.
                    log.info("Esperando la conversión de %s para arrancar", item.path.name)
                    self.pending = _Pending(item, key, start, sub, audio)
                    self.loading = None
                    return
                self._save_progress()
                self._drop_session()
                if start is None:
                    start = self.store.resume_point(item.path)
                session.base_url = self.base_url
                self.session, self.item = session, item
                if self.http is not None:
                    self.http.session = session
                self._saw_playback = False
                self.played_anything = True
                try:
                    session.start(start)
                except Exception as exc:
                    log.warning("Falló el LOAD de %s: %s", item.path, exc)
                    self.error = f"Could not start playback: {exc}"
                    self._drop_session()
                finally:
                    self.loading = None

    def _build_session(self, item: Item, sub, audio: int | None) -> Session:
        media = probe(item.path)
        opts = self.options
        sub_lang = opts.sub_lang
        sub_choice, no_subs = None, False
        if sub == "off" or sub is None:
            no_subs = True
        elif isinstance(sub, str) and sub.startswith("lang:"):
            sub_lang = sub[5:] or sub_lang
        elif sub != "auto":
            sub_choice = int(sub)

        audio_stream = None
        if audio is not None:
            if not 0 <= audio < len(media.audio):
                raise ValueError(f"There is no audio track #{audio}.")
            audio_stream = media.audio[audio]

        session = Session.create(
            media, self.caster, audio_stream=audio_stream, force=opts.force,
            surround=opts.surround, sub_lang=sub_lang, sub_choice=sub_choice,
            no_subs=no_subs, sub_scale=opts.sub_scale, sub_encoding=opts.sub_encoding,
        )
        # Una conversión guardada se sirve como archivo: arranca al instante y
        # con seek real. Si no la hay, no se espera a encodear entero (desde el
        # sillón nadie quiere mirar una barra de progreso varios minutos): se
        # arranca por el pipe y la conversión completa se hace en segundo
        # plano, para la próxima.
        session.prepared = self.cache.lookup(media, session.plan)
        if session.prepared is None:
            key = self.converter.request(media, session.plan)
            if key is not None:
                self._jobs[item.id] = key
        return session

    def stop(self) -> None:
        """Corta lo que se esté viendo y vuelve a la biblioteca."""
        with self._lock:
            self.loading = None
            self._save_progress()
            if self.session is not None:
                self.session.stop()
            self._drop_session()

    def _drop_session(self) -> None:
        self.session, self.item = None, None
        if self.http is not None:
            self.http.session = None
            self.http.shutdown_all()

    def close(self) -> None:
        self._closing.set()
        self.converter.close()
        self.stop()
        self.store.flush()

    # --- seguimiento ---

    def _save_progress(self) -> None:
        session, item = self.session, self.item
        if session is None or item is None or not self._status_is_ours(session):
            return
        self.store.update(item.path, session.position(), session.duration())
        self.store.flush()

    def _status_is_ours(self, session: Session) -> bool:
        """True si el estado del receptor ya habla del video actual.

        Justo después de un LOAD el receptor todavía reporta el anterior, con su
        posición: sin este control guardábamos "1:40:00" en el capítulo recién
        empezado. La URL lleva la generación, así que tampoco se confunde con
        la carga previa del mismo video (un seek en modo pipe).
        """
        status = self.caster.status
        return bool(status) and getattr(status, "content_id", None) == session.media_url()

    def _watch(self) -> None:
        while not self._closing.wait(1.0):
            try:
                self._tick()
            except Exception as exc:  # el hilo no puede morir por un estado raro
                log.warning("Seguimiento de la reproducción: %s", exc)

    def _check_pending(self) -> None:
        """Arranca el "Play" que esperaba, cuando su conversión termina."""
        with self._lock:
            pending = self.pending
            if pending is None:
                return
            progress = self.converter.progress(pending.key)
            if progress is None:
                # Cancelada, o falló: no hay nada que arrancar.
                failed = self.converter.failed(pending.key)
                if failed:
                    self.error = f"Converting {pending.item.label} failed: {failed}"
                self.pending = None
                return
            if not progress["done"]:
                return
            self.pending = None
        log.info("Conversión lista: arranca %s", pending.item.path.name)
        self.play(pending.item.id, start=pending.start, sub=pending.sub, audio=pending.audio)

    def _tick(self) -> None:
        self._check_pending()
        finished: Item | None = None
        with self._lock:
            session, item = self.session, self.item
            if session is None or item is None or not self._status_is_ours(session):
                return
            state = self.caster.player_state
            if state in ("PLAYING", "BUFFERING", "PAUSED"):
                self._saw_playback = True
                if state != "BUFFERING":
                    self.store.update(item.path, session.position(), session.duration())
                return
            if not (self._saw_playback and state == "IDLE"):
                return
            reason = getattr(self.caster.status, "idle_reason", None)
            if reason == "FINISHED":
                self.store.mark_watched(item.path, session.duration())
                finished = item
                # El receptor sigue diciendo FINISHED hasta que llegue el LOAD
                # del siguiente; sin esto lo encadenábamos una vez por segundo.
                self._saw_playback = False
            elif reason in ("CANCELLED", "ERROR"):
                # Lo cortaron desde el control del teléfono o el receptor se
                # rindió: guardamos dónde estaba y soltamos la sesión.
                self._save_progress()
                if reason == "ERROR":
                    self.error = "The Chromecast stopped with an error."
                self._drop_session()
                return
            else:
                return
            sub = self._sub_preference()
            nxt = self.library.next_episode(finished) if self.options.autoplay_next else None
            if nxt is None:
                self._drop_session()

        if finished is not None and nxt is not None:
            log.info("Terminó %s; sigue %s", finished.path.name, nxt.path.name)
            self.play(nxt.id, start=0.0, sub=sub)

    # --- estado para la UI ---

    def conversion_for(self, item_id: str) -> dict | None:
        """El avance de la conversión en segundo plano de un video, si hay una."""
        return self.converter.progress(self._jobs.get(item_id))

    def _conversion(self, session: Session, item: Item) -> dict | None:
        """Cuánto está convertido el video que se reproduce.

        Lo guardado cuenta entero. Si no, el avance de la conversión completa
        en segundo plano; y para un remux, que no se guarda, lo que lleva el
        pipe por delante de la reproducción. None en direct play, o si ffmpeg
        todavía no arrancó (el receptor lo pone en marcha al pedir /media).
        """
        if self.cache.contains(session.prepared):
            return {"start": 0.0, "end": round(session.duration(), 1),
                    "done": True, "saved": True, "mode": session.plan.mode,
                    "duration": round(session.duration(), 1)}
        background = self.conversion_for(item.id)
        if background is not None:
            return background
        if session.seekable or self.http is None:
            return None
        transcoder = self.http.transcoder
        if transcoder is None or transcoder.media is not session.media:
            return None
        return {
            "start": round(transcoder.offset, 1),
            "end": round(transcoder.converted, 1),
            "done": transcoder.finished and not transcoder.killed,
            "mode": session.plan.mode,
            "duration": round(session.duration(), 1),
        }

    def snapshot(self) -> dict:
        with self._lock:
            session, item, loading = self.session, self.item, self.loading
            data: dict = {
                "library": True,
                "device": self.caster.device.name,
                "connected": self.caster.connected,
                "phase": "loading" if loading else "playing" if session else "idle",
                "loading": item_json(loading, self.store) if loading else None,
                "pending": None if self.pending is None else {
                    **item_json(self.pending.item, self.store),
                    "conversion": self.converter.progress(self.pending.key),
                },
                "library_error": self.error,
            }
            if session is not None and item is not None:
                data.update(session.snapshot())
                if isinstance(item, Episode):
                    data["title"] = f"{item.show} · {item.code}"
                    if item.title:
                        data["title"] += f" · {item.title}"
                else:
                    data["title"] = item.label
                data["item"] = item_json(item, self.store)
                data["conversion"] = self._conversion(session, item)
                nxt = self.library.next_episode(item)
                data["next"] = item_json(nxt, self.store) if nxt else None
            data["conversions"] = {
                item_id: prog for item_id in list(self._jobs)
                if (prog := self.conversion_for(item_id)) is not None
            }
            return data
