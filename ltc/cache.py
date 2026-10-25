"""Conversiones guardadas: lo que ya se reencodó una vez no se vuelve a encodear.

Un capítulo en HEVC 10 bits tarda minutos en pasar a H.264, y volver a verlo
(o retomarlo otro día) repetía todo el trabajo. Acá se guarda el MP4 terminado,
indexado por el archivo de origen y por el plan de conversión: si cambia el
archivo, el audio elegido o lo que el Chromecast soporta, la clave cambia y no
se sirve una conversión que no corresponde.

El archivo guardado es el mismo que produce `encode_whole` (MP4 común con el
índice adelante), así que además de arrancar al instante tiene seek real.

Sólo se guardan reencodes (`transcode`). Un remux se rehace en segundos y
guardarlo ocuparía tanto como el original.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .compat import Plan
from .logutil import get_logger
from .probe import MediaInfo
from .transcode import PrepareError, detect_hw_encoder, encode_whole, spool_dir

log = get_logger("cache")

DEFAULT_LIMIT_GB = 40.0
# Subirlo invalida todo lo guardado: para cuando cambie cómo se arma el MP4.
FORMAT_VERSION = 1


def cacheable(plan: Plan) -> bool:
    return plan.mode == "transcode"


def plan_key(media: MediaInfo, plan: Plan) -> str:
    try:
        st = media.path.stat()
        stamp = (st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = (media.size, 0)
    parts = (
        FORMAT_VERSION, str(media.path.resolve()), *stamp,
        plan.mode, plan.video_codec, plan.audio_codec,
        plan.audio_stream.index if plan.audio_stream else -1,
        plan.burn_sub.index if plan.burn_sub else -1,
        plan.scale_height, plan.downmix, plan.video_bitrate,
    )
    return hashlib.sha1(repr(parts).encode("utf-8", "surrogateescape")).hexdigest()[:20]


class ConversionCache:
    def __init__(self, limit_gb: float = DEFAULT_LIMIT_GB, directory: Path | None = None):
        self.directory = directory or (spool_dir() / "converted")
        self.limit = int(max(0.0, limit_gb) * 1024 ** 3)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def path_for(self, media: MediaInfo, plan: Plan) -> Path:
        return self.directory / f"{plan_key(media, plan)}.mp4"

    def contains(self, path: Path | None) -> bool:
        return path is not None and path.parent == self.directory

    def lookup(self, media: MediaInfo, plan: Plan) -> Path | None:
        if not self.enabled or not cacheable(plan):
            return None
        path = self.path_for(media, plan)
        try:
            # La fecha de modificación es el "último uso" para el desalojo.
            os.utime(path)
        except OSError:
            return None
        return path

    def adopt(self, encoded: Path, media: MediaInfo, plan: Plan,
              keep: set[Path] | None = None) -> Path:
        """Mueve una conversión recién terminada a la caché y devuelve su ruta.

        Con la caché apagada devuelve el mismo archivo: quien llama lo borra al
        terminar, como siempre.
        """
        if not self.enabled or not cacheable(plan):
            return encoded
        target = self.path_for(media, plan)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                # Normalmente los dos cuelgan de ~/.cache/ltc: un rename
                # instantáneo, no una copia de un giga.
                os.replace(encoded, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # Otro sistema de archivos (XDG_CACHE_HOME en otro disco):
                # copia a un temporal al lado y rename, para que nunca se vea
                # un MP4 a medio copiar con el nombre definitivo.
                partial = target.with_suffix(".part")
                shutil.copyfile(encoded, partial)
                os.replace(partial, target)
                encoded.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("No se pudo guardar la conversión de %s: %s", media.path.name, exc)
            return encoded
        log.info("Conversión guardada: %s → %s", media.path.name, target.name)
        self._write_sidecar(target, media.path)
        self.evict(keep=(keep or set()) | {target})
        return target

    # --- qué archivos tienen conversión, sin probarlos ---
    #
    # La clave sale del plan, y el plan necesita ffprobe: para pintar una
    # grilla de cien películas no se puede probar cada una. Al lado de cada
    # MP4 se anota de qué archivo salió, y con eso alcanza.

    def _write_sidecar(self, target: Path, source: Path) -> None:
        try:
            st = source.stat()
            target.with_suffix(".json").write_text(json.dumps({
                "source": str(source.resolve()),
                "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            }), "utf-8")
        except OSError as exc:
            log.warning("No se pudo anotar el origen de %s: %s", target.name, exc)

    def saved_sources(self) -> set[str]:
        """Rutas de origen con una conversión guardada y todavía vigente."""
        if not self.enabled:
            return set()
        found: set[str] = set()
        for sidecar in self.directory.glob("*.json"):
            try:
                info = json.loads(sidecar.read_text("utf-8"))
                if not sidecar.with_suffix(".mp4").is_file():
                    continue
                st = Path(info["source"]).stat()
            except (OSError, ValueError, KeyError):
                continue
            if (st.st_size, st.st_mtime_ns) == (info.get("size"), info.get("mtime_ns")):
                found.add(info["source"])
        return found

    def evict(self, keep: set[Path] = frozenset()) -> None:
        """Borra lo que hace más tiempo que no se usa hasta entrar en el tope."""
        with self._lock:
            try:
                files = [(p, p.stat()) for p in self.directory.glob("*.mp4")]
            except OSError:
                return
            total = sum(st.st_size for _, st in files)
            for path, st in sorted(files, key=lambda f: f[1].st_mtime):
                if total <= self.limit:
                    break
                if path in keep:
                    continue
                try:
                    path.unlink()
                    path.with_suffix(".json").unlink(missing_ok=True)
                    total -= st.st_size
                    log.info("Caché llena: borrada la conversión %s", path.name)
                except OSError:
                    pass


# --- conversión en segundo plano (modo biblioteca) --------------------------------

@dataclass
class _Job:
    key: str
    media: MediaInfo
    plan: Plan
    done_seconds: float = 0.0
    started: bool = False
    finished: bool = False
    failed: str = ""
    result: Path | None = None
    started_at: float = 0.0
    cancel: threading.Event = field(default_factory=threading.Event)

    def eta(self) -> float | None:
        """Segundos que faltan, a la velocidad que viene; None si es temprano."""
        elapsed = time.monotonic() - self.started_at
        if not self.started or elapsed < 3 or self.done_seconds <= 0:
            return None
        rate = self.done_seconds / elapsed
        return max(0.0, (self.media.duration - self.done_seconds) / rate)


class BackgroundConverter:
    """Convierte archivos enteros a la caché, de a uno, en un hilo propio.

    Corre aparte del pipe que alimenta la reproducción: el pipe arranca a
    mostrar enseguida (y desde donde haga falta), y esto deja guardado el
    archivo completo para la próxima, aunque se corte antes o se empiece por
    la mitad.
    """

    def __init__(self, cache: ConversionCache, hwaccel: bool = True):
        self.cache = cache
        self.hwaccel = hwaccel
        self._jobs: dict[str, _Job] = {}
        self._queue: list[str] = []
        self._cond = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request(self, media: MediaInfo, plan: Plan) -> str | None:
        """Encola la conversión si hace falta. Devuelve la clave, o None."""
        if not self.cache.enabled or not cacheable(plan):
            return None
        key = plan_key(media, plan)
        with self._cond:
            job = self._jobs.get(key)
            if job is not None and not job.failed and not job.cancel.is_set():
                return key
            if self.cache.lookup(media, plan) is not None:
                return None
            job = _Job(key, media, plan)
            self._jobs[key] = job
            self._queue.append(key)
            self._cond.notify()
        return key

    def progress(self, key: str | None) -> dict | None:
        with self._cond:
            job = self._jobs.get(key) if key else None
            if job is None or job.failed or job.cancel.is_set():
                return None
            eta = job.eta()
            return {
                "start": 0.0,
                "end": round(job.media.duration if job.finished else job.done_seconds, 1),
                "done": job.finished,
                "queued": not job.started,
                "saved": job.finished,
                "mode": job.plan.mode,
                "duration": round(job.media.duration, 1),
                "eta": None if eta is None or job.finished else round(eta),
            }

    def failed(self, key: str | None) -> str:
        with self._cond:
            job = self._jobs.get(key) if key else None
            return job.failed if job is not None and not job.cancel.is_set() else ""

    def cancel(self, key: str | None) -> None:
        """Corta una conversión (en curso o en cola). Lo hecho hasta ahí se tira."""
        with self._cond:
            job = self._jobs.get(key) if key else None
            if job is None or job.finished:
                return
            job.cancel.set()
            if key in self._queue:
                self._queue.remove(key)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            for job in self._jobs.values():
                job.cancel.set()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                job = self._jobs[self._queue.pop(0)]
                job.started = True
                job.started_at = time.monotonic()
            self._convert(job)

    def _convert(self, job: _Job) -> None:
        def on_progress(seconds: float) -> None:
            job.done_seconds = seconds

        hw = detect_hw_encoder() if self.hwaccel else None
        started = time.monotonic()
        try:
            encoded = encode_whole(job.media, job.plan, on_progress=on_progress,
                                   hw=hw, cancel=job.cancel)
            job.result = self.cache.adopt(encoded, job.media, job.plan)
            if not self.cache.contains(job.result):
                # No se pudo guardar: acá nadie lo va a servir ni a borrar.
                job.result.unlink(missing_ok=True)
                raise OSError("could not keep the converted file")
        except (PrepareError, OSError) as exc:
            if not job.cancel.is_set():
                log.warning("Falló la conversión en segundo plano de %s: %s",
                            job.media.path.name, exc)
            job.failed = str(exc)
            return
        job.finished = True
        log.info("Conversión en segundo plano de %s lista en %.0f s",
                 job.media.path.name, time.monotonic() - started)
