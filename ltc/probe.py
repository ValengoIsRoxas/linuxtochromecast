"""Análisis del archivo de entrada con ffprobe."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LANG_NAMES = {
    "spa": "Spanish", "es": "Spanish", "esp": "Spanish", "es-ES": "Spanish",
    "lat": "Spanish (Latin America)", "es-419": "Spanish (Latin America)",
    "eng": "English", "en": "English",
    "por": "Portuguese", "pt": "Portuguese", "pob": "Portuguese (BR)",
    "fra": "French", "fre": "French", "fr": "French",
    "ita": "Italian", "it": "Italian",
    "deu": "German", "ger": "German", "de": "German",
    "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese",
    "rus": "Russian", "ru": "Russian",
    "und": "Unknown language",
}

# Subtítulos que se pueden convertir a texto (WebVTT).
TEXT_SUB_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "subviewer", "microdvd",
}
# Subtítulos que son imágenes: sólo se pueden incrustar quemándolos en el video.
BITMAP_SUB_CODECS = {
    "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "xsub",
}


class ProbeError(RuntimeError):
    pass


@dataclass
class Stream:
    """Una pista dentro del archivo."""

    index: int          # índice absoluto dentro del contenedor
    type_index: int     # índice dentro de su tipo (v:0, a:1, s:2...)
    kind: str           # "video" | "audio" | "subtitle"
    codec: str
    lang: str = "und"
    title: str = ""
    default: bool = False
    forced: bool = False
    # video
    width: int = 0
    height: int = 0
    profile: str = ""
    pix_fmt: str = ""
    fps: float = 0.0
    # audio
    channels: int = 0

    @property
    def lang_name(self) -> str:
        return LANG_NAMES.get(self.lang.lower(), self.lang.upper() if self.lang != "und" else "Unknown language")

    @property
    def is_text_sub(self) -> bool:
        return self.kind == "subtitle" and self.codec in TEXT_SUB_CODECS

    @property
    def is_bitmap_sub(self) -> bool:
        return self.kind == "subtitle" and self.codec in BITMAP_SUB_CODECS

    def label(self) -> str:
        """Nombre legible para mostrar en la UI."""
        bits = []
        if self.title:
            bits.append(self.title)
        else:
            bits.append(self.lang_name)
        if self.forced:
            bits.append("forced")
        if self.kind == "audio" and self.channels:
            bits.append(f"{self.channels}ch")
        if self.kind == "subtitle" and self.is_bitmap_sub:
            bits.append("image")
        return " · ".join(bits)


@dataclass
class MediaInfo:
    path: Path
    duration: float          # segundos, 0 si desconocida
    container: str           # nombre corto del formato, ej "matroska,webm"
    size: int
    bitrate: int
    video: list[Stream] = field(default_factory=list)
    audio: list[Stream] = field(default_factory=list)
    subtitles: list[Stream] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.path.stem

    def default_audio(self) -> Stream | None:
        if not self.audio:
            return None
        for s in self.audio:
            if s.default:
                return s
        return self.audio[0]


def _require(binary: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise ProbeError(
            f"Couldn't find '{binary}'. Install it with:  sudo dnf install ffmpeg-free  "
            f"(or your distro's ffmpeg package)."
        )
    return found


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_fps(raw: str | None) -> float:
    if not raw or "/" not in raw:
        return _as_float(raw)
    num, _, den = raw.partition("/")
    d = _as_float(den)
    return _as_float(num) / d if d else 0.0


def probe(path: str | Path) -> MediaInfo:
    """Corre ffprobe y devuelve la estructura del archivo."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ProbeError(f"No such file: {path}")

    ffprobe = _require("ffprobe")
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60, check=True).stdout
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", "replace").strip() or "error desconocido"
        raise ProbeError(f"ffprobe could not read the file: {msg}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe timed out (file on a slow network share?)") from exc

    data = json.loads(out or b"{}")
    fmt = data.get("format", {})

    info = MediaInfo(
        path=path,
        duration=_as_float(fmt.get("duration")),
        container=fmt.get("format_name", ""),
        size=_as_int(fmt.get("size")),
        bitrate=_as_int(fmt.get("bit_rate")),
    )

    counters = {"video": 0, "audio": 0, "subtitle": 0}
    for raw in data.get("streams", []):
        kind = raw.get("codec_type", "")
        if kind not in counters:
            continue
        # Las carátulas embebidas se anuncian como video; no son pistas reproducibles.
        disp = raw.get("disposition", {}) or {}
        if kind == "video" and disp.get("attached_pic"):
            continue

        tags = {k.lower(): v for k, v in (raw.get("tags") or {}).items()}
        stream = Stream(
            index=_as_int(raw.get("index")),
            type_index=counters[kind],
            kind=kind,
            codec=raw.get("codec_name", "") or "",
            lang=tags.get("language", "und") or "und",
            title=tags.get("title", "") or "",
            default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            width=_as_int(raw.get("width")),
            height=_as_int(raw.get("height")),
            profile=raw.get("profile", "") or "",
            pix_fmt=raw.get("pix_fmt", "") or "",
            fps=_parse_fps(raw.get("avg_frame_rate") or raw.get("r_frame_rate")),
            channels=_as_int(raw.get("channels")),
        )
        counters[kind] += 1
        getattr(info, kind if kind != "subtitle" else "subtitles").append(stream)

    if not info.video and not info.audio:
        raise ProbeError(f"This file has no playable streams: {path.name}")

    return info
