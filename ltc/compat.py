"""Qué banca cada Chromecast y cómo llegar desde el archivo hasta ahí.

Tres modos posibles, de menos a más costoso:

  direct    El archivo se sirve tal cual por HTTP con soporte de Range.
            Seek real, cero CPU. Sólo si contenedor y códecs ya son compatibles.
  remux     ffmpeg reempaqueta a MP4 fragmentado sin recomprimir (-c copy).
            Casi cero CPU. Típico de un MKV con h264+aac.
  transcode Hay que recomprimir video y/o audio. Come CPU.

En remux y transcode la salida es un stream por pipe (sin Range), así que el
seek se hace reiniciando ffmpeg desde el offset pedido.
"""

from __future__ import annotations

from dataclasses import dataclass

from .probe import MediaInfo, Stream

# --- Capacidades por dispositivo ------------------------------------------------

BASE_VIDEO = {"h264", "vp8"}
BASE_AUDIO = {"aac", "mp3", "vorbis", "opus", "flac", "pcm_s16le", "pcm_s24le"}

# Ultra / Google TV / Nest Hub Max y sucesores: agregan HEVC, VP9 y 4K.
MODERN_VIDEO = BASE_VIDEO | {"hevc", "vp9", "av1"}

# Extensiones que el receptor default sabe leer directamente. Hay que mirar la
# extensión y no el format_name de ffprobe: ffprobe reporta tanto los .mkv como
# los .webm como "matroska,webm" —comparten demuxer— y un MKV servido crudo es
# justamente lo que el Chromecast no reproduce.
DIRECT_EXTENSIONS = {"mp4", "m4v", "mov", "webm"}

# Familias de demuxer aceptables para cada extensión, como control de sanidad
# por si el archivo tiene la extensión cambiada.
_DEMUXER_FAMILY = {
    "mp4": {"mov", "mp4", "m4a"}, "m4v": {"mov", "mp4", "m4a"},
    "mov": {"mov", "mp4", "m4a"}, "webm": {"webm", "matroska"},
}

MODERN_MARKERS = ("ultra", "google tv", "streamer", "hub max", "shield", "nest hub")


@dataclass
class DeviceCaps:
    name: str = "Chromecast"
    video: frozenset[str] = frozenset(BASE_VIDEO)
    audio: frozenset[str] = frozenset(BASE_AUDIO)
    max_height: int = 1080
    # Los Chromecast comunes decodifican AAC en estéreo, pero no multicanal:
    # el receptor arranca a reproducir y recién ahí falla con
    # MEDIA_SRC_NOT_SUPPORTED (104). Por eso el códec solo no alcanza para
    # decidir; hay que mirar los canales.
    max_audio_channels: int = 2
    audio_only: bool = False

    @classmethod
    def for_model(cls, model: str | None, cast_type: str | None = None) -> "DeviceCaps":
        model = (model or "Chromecast").strip()
        low = model.lower()

        if cast_type == "audio" or any(k in low for k in ("audio", "home mini", "nest mini", "speaker")):
            return cls(name=model, video=frozenset(), audio=frozenset(BASE_AUDIO),
                       max_height=0, audio_only=True)

        if any(marker in low for marker in MODERN_MARKERS):
            return cls(name=model, video=frozenset(MODERN_VIDEO),
                       audio=frozenset(BASE_AUDIO), max_height=2160,
                       max_audio_channels=6)

        return cls(name=model)


# --- Plan de reproducción -------------------------------------------------------

@dataclass
class Plan:
    mode: str                     # "direct" | "remux" | "transcode"
    video_codec: str              # "copy" | "libx264" | "none"
    audio_codec: str              # "copy" | "aac"
    content_type: str             # MIME que se le anuncia al Chromecast
    audio_stream: Stream | None   # pista de audio elegida
    burn_sub: Stream | None = None  # subtítulo bitmap a quemar en el video
    scale_height: int = 0         # 0 = sin reescalar
    downmix: bool = False         # bajar a estéreo
    reasons: list[str] = None     # explicación legible de cada decisión

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []

    @property
    def seekable(self) -> bool:
        """True si el server puede atender Range (seek nativo, sin reiniciar ffmpeg)."""
        return self.mode == "direct"

    @property
    def reencodes_video(self) -> bool:
        return self.video_codec not in ("copy", "none")


def plan_for(
    media: MediaInfo,
    caps: DeviceCaps,
    *,
    audio_stream: Stream | None = None,
    burn_sub: Stream | None = None,
    force: str = "auto",
    surround: bool = False,
) -> Plan:
    """Decide el camino más barato desde `media` hasta algo que `caps` reproduzca."""
    reasons: list[str] = []
    audio = audio_stream or media.default_audio()
    video = media.video[0] if media.video else None

    if caps.audio_only:
        raise ValueError(
            f"'{caps.name}' es un dispositivo de sólo audio y no puede mostrar video."
        )
    if video is None:
        raise ValueError("El archivo no tiene pista de video.")

    # --- video ---
    video_ok = video.codec in caps.video
    if not video_ok:
        reasons.append(f"video {video.codec} no soportado por {caps.name} → recodifico a H.264")
    elif not _pixel_format_ok(video, caps):
        # El formato de píxeles es más confiable que el nombre del perfil, que
        # ffprobe escribe de formas distintas ("High 4:4:4 Predictive", "Hi444PP").
        detail = video.pix_fmt or video.profile or "desconocido"
        video_ok = False
        reasons.append(f"formato de píxeles {detail} no soportado → recodifico a 8 bits 4:2:0")

    scale_height = 0
    if video_ok and caps.max_height and video.height > caps.max_height:
        video_ok = False
        scale_height = caps.max_height
        reasons.append(f"{video.height}p supera el máximo de {caps.name} ({caps.max_height}p) → reescalo")

    if burn_sub is not None:
        video_ok = False
        reasons.append(f"subtítulos '{burn_sub.label()}' son imagen → los quemo en el video")

    # --- audio ---
    downmix = False
    if audio is None:
        reasons.append("el archivo no tiene audio")
        audio_ok = True
    elif audio.codec not in caps.audio:
        audio_ok = False
        reasons.append(f"audio {audio.codec} no soportado → convierto a AAC")
        downmix = audio.channels > caps.max_audio_channels and not surround
        if downmix:
            reasons.append(f"y bajo {audio.channels} canales a estéreo")
    elif audio.channels > caps.max_audio_channels and not surround:
        # El códec sirve pero la cantidad de canales no. Este es el caso que hace
        # que la película arranque y se corte a los pocos segundos.
        audio_ok = False
        downmix = True
        reasons.append(
            f"audio {audio.codec} de {audio.channels} canales: {caps.name} sólo "
            f"reproduce {caps.max_audio_channels} → bajo a estéreo "
            f"(--surround para intentar mantenerlo)"
        )
    else:
        audio_ok = True

    # --- contenedor ---
    container_ok = _container_ok(media)
    multi_audio = audio is not None and len(media.audio) > 1 and audio is not media.audio[0]

    # --- overrides del usuario ---
    if force == "transcode":
        video_ok = audio_ok = False
        reasons.append("forzado por --transcode")
    elif force == "direct":
        if not (video_ok and audio_ok and container_ok):
            reasons.append("⚠ forzado por --direct: puede no reproducirse")
        video_ok = audio_ok = container_ok = True
        multi_audio = False

    # --- resolución final ---
    if video_ok and audio_ok and container_ok and not multi_audio:
        if not reasons:
            reasons.append("contenedor y códecs ya compatibles → sin procesar")
        return Plan("direct", "copy", "copy", _mime_for(media.container),
                    audio, None, 0, False, reasons)

    if video_ok and audio_ok:
        if multi_audio:
            reasons.append(f"pista de audio '{audio.label()}' seleccionada → reempaqueto")
        else:
            reasons.append(f"contenedor {media.container.split(',')[0]} no soportado → reempaqueto a MP4 (sin recodificar)")
        return Plan("remux", "copy", "copy", "video/mp4",
                    audio, None, 0, False, reasons)

    return Plan(
        "transcode",
        "copy" if video_ok else "libx264",
        "copy" if audio_ok else "aac",
        "video/mp4",
        audio,
        burn_sub,
        scale_height,
        downmix,
        reasons,
    )


# Croma y profundidad que sabe decodificar el receptor. Todo lo que no sea 4:2:0
# de 8 bits hay que recodificarlo, aunque el códec en sí esté soportado: los
# rips en 10 bits (típicos del anime) entran por acá.
_SAFE_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12", "nv21", "gbrp", ""}
_HDR_PIX_FMTS = {"yuv420p10le", "yuv420p10be", "p010le"}


def _container_ok(media: MediaInfo) -> bool:
    """True si el archivo se puede mandar tal cual, sin reempaquetar."""
    ext = media.path.suffix.lower().lstrip(".")
    if ext not in DIRECT_EXTENSIONS:
        return False
    family = set(media.container.lower().split(","))
    return bool(family & _DEMUXER_FAMILY[ext])


def _pixel_format_ok(video: Stream, caps: DeviceCaps) -> bool:
    pix = (video.pix_fmt or "").lower()
    if pix in _SAFE_PIX_FMTS:
        return True
    # Los dispositivos modernos sí decodifican 10 bits, pero sólo en HEVC/VP9/AV1.
    if pix in _HDR_PIX_FMTS and caps.max_height > 1080 and video.codec != "h264":
        return True

    # Sin pix_fmt caemos al nombre del perfil, normalizado.
    if not pix:
        profile = (video.profile or "").replace(":", "").replace(" ", "").lower()
        return not any(bad in profile for bad in ("444", "422", "high10", "hi10"))
    return False


def _mime_for(container: str) -> str:
    names = container.split(",")
    if "webm" in names:
        return "video/webm"
    if "mov" in names or "mp4" in names:
        return "video/mp4"
    return "video/mp4"
