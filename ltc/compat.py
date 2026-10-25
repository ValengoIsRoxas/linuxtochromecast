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
    video_bitrate: int = 0        # kbps objetivo cuando reencodamos (0 = copy)
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
            f"'{caps.name}' is an audio-only device and can't display video."
        )
    if video is None:
        raise ValueError("This file has no video stream.")

    # --- video ---
    video_ok = video.codec in caps.video
    if not video_ok:
        reasons.append(f"video {video.codec} not supported by {caps.name} → re-encoding to H.264")
    elif not _pixel_format_ok(video, caps):
        # El formato de píxeles es más confiable que el nombre del perfil, que
        # ffprobe escribe de formas distintas ("High 4:4:4 Predictive", "Hi444PP").
        detail = video.pix_fmt or video.profile or "desconocido"
        video_ok = False
        reasons.append(f"pixel format {detail} not supported → re-encoding to 8-bit 4:2:0")

    scale_height = 0
    if video_ok and caps.max_height and video.height > caps.max_height:
        video_ok = False
        scale_height = caps.max_height
        reasons.append(f"{video.height}p exceeds the maximum for {caps.name} ({caps.max_height}p) → rescaling")

    if burn_sub is not None:
        video_ok = False
        reasons.append(f"subtitles '{burn_sub.label()}' are image-based → burning them into the video")

    # --- audio ---
    downmix = False
    if audio is None:
        reasons.append("this file has no audio")
        audio_ok = True
    elif audio.codec not in caps.audio:
        audio_ok = False
        reasons.append(f"audio {audio.codec} not supported → converting to AAC")
        downmix = audio.channels > caps.max_audio_channels and not surround
        if downmix:
            reasons.append(f"and downmixing {audio.channels} channels to stereo")
    elif audio.channels > caps.max_audio_channels and not surround:
        # El códec sirve pero la cantidad de canales no. Este es el caso que hace
        # que la película arranque y se corte a los pocos segundos.
        audio_ok = False
        downmix = True
        reasons.append(
            f"{audio.channels}-channel {audio.codec} audio: {caps.name} only plays "
            f"{caps.max_audio_channels} → downmixing to stereo "
            f"(--surround to try keeping it)"
        )
    else:
        audio_ok = True

    # --- contenedor ---
    container_ok = _container_ok(media)
    multi_audio = audio is not None and len(media.audio) > 1 and audio is not media.audio[0]

    # --- overrides del usuario ---
    if force == "transcode":
        video_ok = audio_ok = False
        reasons.append("forced by --transcode")
    elif force == "direct":
        if not (video_ok and audio_ok and container_ok):
            reasons.append("⚠ forced by --direct: may fail to play")
        video_ok = audio_ok = container_ok = True
        multi_audio = False

    # --- resolución final ---
    if video_ok and audio_ok and container_ok and not multi_audio:
        if not reasons:
            reasons.append("container and codecs already compatible → no processing")
        return Plan("direct", "copy", "copy", _mime_for(media.container),
                    audio, None, 0, False, reasons=reasons)

    if video_ok and audio_ok:
        if multi_audio:
            reasons.append(f"audio track '{audio.label()}' selected → remuxing")
        else:
            reasons.append(f"container {media.container.split(',')[0]} not supported → remuxing to MP4 (no re-encoding)")
        return Plan("remux", "copy", "copy", "video/mp4",
                    audio, None, 0, False, reasons=reasons)

    return Plan(
        "transcode",
        "copy" if video_ok else "libx264",
        "copy" if audio_ok else "aac",
        "video/mp4",
        audio,
        burn_sub,
        scale_height,
        downmix,
        video_bitrate=0 if video_ok else _target_video_bitrate(scale_height or video.height),
        reasons=reasons,
    )


def _target_video_bitrate(height: int) -> int:
    """kbps objetivo para el H.264 de salida, según la altura efectiva.

    No hay que adivinar a partir del bitrate del original: HEVC comprime
    bastante mejor que H.264 a igual calidad, así que un H.264 CRF de calidad
    pareja pesa más —a veces bastante más— que el HEVC que reemplaza. Con un
    bitrate objetivo en vez de CRF, el tamaño de salida queda acotado y
    predecible, que es lo que necesita `estimated_stream_size` para no
    quedarse corto y cortar la película antes del final real.
    """
    if height >= 1080:
        return 6000
    if height >= 720:
        return 3500
    if height >= 480:
        return 2000
    return 1200


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
