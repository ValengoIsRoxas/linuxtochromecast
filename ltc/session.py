"""La sesión: une archivo, plan, servidor y Chromecast en un solo estado.

Acá vive el truco central del seek. Cuando hay que pasar por ffmpeg la salida es
un pipe, y un pipe no tiene Range: el stream siempre arranca en 0. Entonces
guardamos `stream_offset` —cuánto vale ese 0 en tiempo real de la película— y
toda posición que mostramos o pedimos se traduce sumando o restando ese offset.
Los subtítulos se sirven desplazados por la misma cantidad, así nunca se
desincronizan.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .caster import Caster, default_text_style
from .compat import DeviceCaps, Plan, plan_for
from .probe import MediaInfo, Stream
from .subtitles import (
    SubTrack, SubtitleError, collect_tracks, pick_default, track_for_file,
)
from .transcode import keyframe_at_or_before


@dataclass
class Session:
    media: MediaInfo
    plan: Plan
    caster: Caster
    caps: DeviceCaps
    tracks: list[SubTrack] = field(default_factory=list)

    base_url: str = ""
    active_sub: int | None = None
    sub_delay: float = 0.0
    sub_scale: float = 1.0
    stream_offset: float = 0.0
    generation: int = 0

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _last_error: str = ""
    _started_at: float = 0.0
    # Lo levanta el panel web al apretar "Detener y salir"; lo mira el bucle de
    # la terminal. Es la única forma de cerrar cuando se lanzó sin terminal,
    # por ejemplo desde el menú del gestor de archivos.
    quit_requested: threading.Event = field(default_factory=threading.Event, repr=False)

    # --- construcción ---

    @classmethod
    def create(
        cls,
        media: MediaInfo,
        caster: Caster,
        *,
        audio_stream: Stream | None = None,
        burn_sub: SubTrack | None = None,
        force: str = "auto",
        surround: bool = False,
        sub_lang: str = "spa",
        sub_choice: int | None = None,
        sub_file: Path | None = None,
        no_subs: bool = False,
        sub_scale: float = 1.0,
        sub_encoding: str | None = None,
    ) -> "Session":
        caps = DeviceCaps.for_model(caster.device.model, caster.device.cast_type)
        tracks = collect_tracks(media, prefer=sub_lang, encoding=sub_encoding)

        burn_stream = burn_sub.stream if burn_sub is not None else None
        plan = plan_for(media, caps, audio_stream=audio_stream, burn_sub=burn_stream,
                        force=force, surround=surround)

        session = cls(media=media, plan=plan, caster=caster, caps=caps,
                      tracks=tracks, sub_scale=sub_scale)

        if not no_subs and burn_sub is None:
            if sub_file is not None:
                session.active_sub = track_for_file(
                    sub_file, tracks, prefer=sub_lang, encoding=sub_encoding).id
            elif sub_choice is not None:
                chosen = next((t for t in tracks if t.id == sub_choice), None)
                if chosen is None:
                    raise ValueError(f"There is no subtitle #{sub_choice}.")
                if chosen.bitmap:
                    raise ValueError(
                        f"Subtitle #{sub_choice} is image-based; use --burn {sub_choice}."
                    )
                session.active_sub = chosen.id
            else:
                default = pick_default(tracks, sub_lang)
                session.active_sub = default.id if default else None

        return session

    # --- URLs ---

    def media_url(self) -> str:
        return f"{self.base_url}/media?g={self.generation}"

    def sub_url(self, track: SubTrack) -> str:
        # La generación y el delay van en la URL a propósito: cambiarlos produce
        # una URL nueva y obliga al receptor a volver a bajar el .vtt en vez de
        # servir el que tenía cacheado.
        return (f"{self.base_url}/sub/{track.id}.vtt"
                f"?g={self.generation}&d={self.sub_delay:.3f}")

    def track_payload(self) -> list[dict]:
        """Las pistas de texto tal como las espera el receptor."""
        return [
            {
                "trackId": t.id,
                "type": "TEXT",
                "subtype": "SUBTITLES",
                "trackContentId": self.sub_url(t),
                "trackContentType": "text/vtt",
                "language": t.lang_short,
                "name": t.label,
            }
            for t in self.usable_tracks()
        ]

    def usable_tracks(self) -> list[SubTrack]:
        return [t for t in self.tracks if not t.bitmap]

    def subtitle_shift(self) -> float:
        """Cuánto hay que correr los cues para que peguen con el stream actual."""
        return self.stream_offset + self.sub_delay

    def estimated_stream_size(self) -> int:
        """Cuántos bytes le prometemos al receptor cuando servimos por pipe.

        El Chromecast no acepta `Transfer-Encoding: chunked` para MP4: rechaza la
        carga con LOAD_FAILED sin reproducir un solo cuadro. Necesita un
        Content-Length, así que hay que darle uno aunque el tamaño real recién se
        sepa cuando ffmpeg termina.

        Conviene pasarse antes que quedarse corto. Si prometemos de más, la
        conexión corta al final, cuando la película ya terminó; si prometemos de
        menos, se corta el final de la película. Como el audio siempre sale igual
        o más chico que el original, el tamaño del archivo de entrada ya es una
        cota alta razonable, y encima le damos margen.
        """
        total = self.media.duration or 0.0
        remaining = max(0.0, total - self.stream_offset) if total else 0.0
        fraction = (remaining / total) if total else 1.0

        base = self.media.size or 0
        if self.media.bitrate and total:
            base = max(base, int(self.media.bitrate / 8 * total))
        if not base:  # sin datos, tiramos alto: 4 Mbps
            base = int(4_000_000 / 8 * (total or 7200))

        return int(base * fraction * 1.15) + 8 * 1024 * 1024

    # --- reproducción ---

    def start(self, position: float = 0.0) -> None:
        """Carga el video en el Chromecast desde `position` (segundos absolutos)."""
        with self._lock:
            self._prepare_subtitles()
            self._load(position)

    def _prepare_subtitles(self) -> None:
        """Fuerza la conversión ahora para fallar acá y no en silencio después."""
        bad: list[SubTrack] = []
        for track in list(self.tracks):
            if track.bitmap:
                continue
            try:
                track.cues(self.media.path)
            except SubtitleError as exc:
                self._last_error = str(exc)
                bad.append(track)
        for track in bad:
            self.tracks.remove(track)
            if self.active_sub == track.id:
                self.active_sub = None

    def _load(self, position: float) -> None:
        live = not self.plan.seekable

        if live:
            # En modo pipe el stream empieza donde arranque ffmpeg. Si copiamos
            # el video, ffmpeg se pega al keyframe anterior, así que preguntamos
            # cuál es para que los subtítulos usen ese mismo cero.
            if position > 0 and not self.plan.reencodes_video:
                position = keyframe_at_or_before(self.media.path, position)
            self.stream_offset = max(0.0, position)
            start_at = None
        else:
            self.stream_offset = 0.0
            start_at = max(0.0, position)

        self.generation += 1

        active = self.active_sub if any(
            t.id == self.active_sub for t in self.usable_tracks()) else None

        self.caster.load(
            self.media_url(),
            self.plan.content_type,
            title=self.media.title,
            duration=self.media.duration,
            tracks=self.track_payload(),
            active_track_id=active,
            live=live,
            current_time=start_at,
            text_style=default_text_style(self.sub_scale),
        )
        self._started_at = time.monotonic()

    # --- posición ---

    def position(self) -> float:
        """Posición absoluta dentro de la película."""
        return min(self.stream_offset + self.caster.position(),
                   self.media.duration or float("inf"))

    def duration(self) -> float:
        return self.media.duration

    # --- comandos ---

    def toggle(self) -> None:
        if self.caster.player_state == "PLAYING":
            self.caster.pause()
        else:
            self.caster.play()

    def play(self) -> None:
        self.caster.play()

    def pause(self) -> None:
        self.caster.pause()

    def seek(self, position: float) -> None:
        """Salta a una posición absoluta, por el camino que corresponda."""
        with self._lock:
            position = max(0.0, position)
            if self.media.duration:
                position = min(position, max(0.0, self.media.duration - 2))

            if self.plan.seekable:
                self.caster.seek(position)
            else:
                # Sin Range no hay seek: reiniciamos ffmpeg desde el offset nuevo.
                self._load(position)

    def skip(self, delta: float) -> None:
        self.seek(self.position() + delta)

    def set_subtitle(self, track_id: int | None) -> None:
        """Cambia de pista. Es instantáneo: no hace falta recargar el video."""
        with self._lock:
            previous = self.active_sub
            if track_id is not None and not any(
                    t.id == track_id for t in self.usable_tracks()):
                raise ValueError(f"There is no subtitle #{track_id}.")

            self.active_sub = track_id
            if track_id is None:
                if previous is not None:
                    self.caster.disable_subtitle(previous)
            else:
                self.caster.enable_subtitle(track_id)

    def cycle_subtitle(self) -> None:
        """Rota entre las pistas disponibles y 'sin subtítulos'."""
        options: list[int | None] = [t.id for t in self.usable_tracks()] + [None]
        if len(options) == 1:
            return
        try:
            idx = options.index(self.active_sub)
        except ValueError:
            idx = -1
        self.set_subtitle(options[(idx + 1) % len(options)])

    def set_delay(self, seconds: float) -> None:
        """Ajusta el desfase de los subtítulos.

        Cambia la URL del .vtt, y eso obliga a recargar el media para que el
        receptor la vuelva a pedir. Mantenemos la posición actual.
        """
        with self._lock:
            self.sub_delay = round(max(-60.0, min(60.0, seconds)), 3)
            here = self.position()
            self._load(here)

    def nudge_delay(self, delta: float) -> None:
        self.set_delay(self.sub_delay + delta)

    def set_volume(self, level: float) -> None:
        self.caster.set_volume(level)

    def set_muted(self, muted: bool) -> None:
        self.caster.set_muted(muted)

    def stop(self) -> None:
        self.caster.stop()

    # --- estado para la UI ---

    def snapshot(self) -> dict:
        state = self.caster.player_state
        return {
            "title": self.media.title,
            "device": self.caster.device.name,
            "state": state,
            "playing": state == "PLAYING",
            "buffering": state == "BUFFERING",
            "position": round(self.position(), 2),
            "duration": round(self.media.duration, 2),
            "volume": round(self.caster.volume, 3),
            "muted": self.caster.muted,
            "mode": self.plan.mode,
            "mode_detail": self.plan.reasons,
            "seekable": self.plan.seekable,
            "active_sub": self.active_sub,
            "sub_delay": self.sub_delay,
            "subs": [
                {"id": t.id, "label": t.label, "lang": t.lang, "source": t.source}
                for t in self.usable_tracks()
            ],
            "error": self._last_error,
        }
