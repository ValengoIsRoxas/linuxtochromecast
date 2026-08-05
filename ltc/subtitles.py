"""Subtítulos: encontrarlos, normalizarlos a WebVTT y sincronizarlos.

El Chromecast sólo entiende WebVTT servido con CORS. Todo lo demás (SRT en
latin-1, ASS con estilos, pistas embebidas en un MKV) pasa por acá y sale como
WebVTT limpio.

Los cues se guardan en memoria como objetos `Cue`, así desplazarlos en el tiempo
—para el delay manual o para cuando reiniciamos ffmpeg en un seek— es exacto y
no requiere volver a parsear nada.
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .probe import LANG_NAMES, MediaInfo, Stream

SUB_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".smi", ".ssf"}

# Letras propias de cada idioma. Se usan para desempatar entre codificaciones de
# un byte, que es exactamente donde fallan los detectores automáticos.
_LANG_ALPHABET = {
    "spa": "áéíóúüñ¿¡", "por": "áàâãéêíóôõúüç", "cat": "àéèíïóòúüç",
    "glg": "áéíóúüñ", "fra": "àâçéèêëîïôùûüÿœæ", "ita": "àèéìíîòóùú",
    "deu": "äöüß", "nld": "áéíóúëïöü", "swe": "åäö", "nor": "æøå",
    "dan": "æøå", "fin": "äöå", "pol": "ąćęłńóśźż",
    "ces": "áčďéěíňóřšťúůýž", "cze": "áčďéěíňóřšťúůýž",
    "slk": "áäčďéíĺľňóôŕšťúýž", "hun": "áéíóöőúüű",
    "ron": "ăâîșşţț", "tur": "çğıöşü", "eng": "",
}

# Codificaciones latinas de un byte: entre estas hay que desempatar a mano.
_LATIN_SBCS = {
    "cp1252", "cp1250", "cp1254", "cp1257", "iso8859-1", "iso8859-2",
    "iso8859-3", "iso8859-4", "iso8859-9", "iso8859-13", "iso8859-14",
    "iso8859-15", "iso8859-16", "latin_1", "mac_roman", "cp850", "cp437",
}

# Sufijos de idioma típicos: pelicula.es.srt / pelicula.spa.srt / pelicula-Spanish.srt
_LANG_SUFFIX = re.compile(
    r"[.\-_ ]([a-z]{2,3}|spanish|espanol|español|latino|english|ingles|inglés|forced|forzado|sdh)$",
    re.IGNORECASE,
)

_FILENAME_LANG = {
    "spanish": "spa", "espanol": "spa", "español": "spa", "esp": "spa", "es": "spa",
    "latino": "spa", "lat": "spa",
    "english": "eng", "ingles": "eng", "inglés": "eng", "en": "eng",
    "portugues": "por", "português": "por", "pt": "por",
}

_TIMESTAMP = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)

# Llaves de override de ASS: {\an8}, {\pos(...)}, etc.
_ASS_TAGS = re.compile(r"\{[^}]*\}")


class SubtitleError(RuntimeError):
    pass


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class SubTrack:
    """Un subtítulo listo para servir, venga de donde venga."""

    id: int
    label: str
    lang: str
    source: str                 # "external" | "embedded"
    path: Path | None = None    # si es externo
    stream: Stream | None = None  # si es embebido
    forced: bool = False
    bitmap: bool = False        # sólo se puede quemar en el video
    hint_lang: str = "spa"      # idioma esperado, para desambiguar codificación
    encoding: str | None = None  # forzado por el usuario
    _cues: list[Cue] | None = None

    @property
    def lang_short(self) -> str:
        """Código BCP-47 aproximado, que es lo que espera el receptor.

        Sin esto un subtítulo sin idioma se anunciaba como "un" —recortar "und"
        a dos letras—, que no es un código válido.
        """
        base = (self.lang or "").lower()
        if base in ("", "und"):
            base = (self.hint_lang or "spa").lower()
        return {"spa": "es-ES", "eng": "en-US", "por": "pt-BR", "fra": "fr-FR",
                "deu": "de-DE", "ita": "it-IT"}.get(base, base[:2] or "es-ES")

    def cues(self, media_path: Path) -> list[Cue]:
        """Carga (y cachea) los cues de esta pista."""
        if self._cues is None:
            if self.source == "external":
                self._cues = load_file(self.path, self.hint_lang, self.encoding)
            else:
                self._cues = extract_embedded(media_path, self.stream)
        return self._cues

    def vtt(self, media_path: Path, shift: float = 0.0) -> str:
        return render_vtt(self.cues(media_path), shift)


# --- Detección -----------------------------------------------------------------

def _guess_lang(name: str) -> tuple[str, bool]:
    """Devuelve (código de idioma, es_forzado) a partir del nombre de archivo."""
    stem = Path(name).stem
    lang, forced = "und", False
    for _ in range(2):  # permite "pelicula.es.forced.srt"
        m = _LANG_SUFFIX.search(stem)
        if not m:
            break
        token = m.group(1).lower()
        stem = stem[: m.start()]
        if token in ("forced", "forzado"):
            forced = True
        elif token == "sdh":
            continue
        elif lang == "und":
            lang = _FILENAME_LANG.get(token, token)
    return lang, forced


def find_external(video: Path) -> list[Path]:
    """Busca subtítulos al lado del video (y en subcarpetas tipo Subs/)."""
    video = Path(video)
    stem = video.stem.lower()
    found: list[Path] = []

    search_dirs = [video.parent]
    for extra in ("subs", "subtitles", "subtitulos", "subtítulos"):
        d = video.parent / extra
        if d.is_dir():
            search_dirs.append(d)

    for directory in search_dirs:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for item in entries:
            if not item.is_file() or item.suffix.lower() not in SUB_EXTENSIONS:
                continue
            # En la carpeta del video exigimos que el nombre coincida; en Subs/
            # aceptamos cualquier cosa, que es como vienen los torrents.
            if directory == video.parent and not item.stem.lower().startswith(stem):
                continue
            found.append(item)
    return found


def collect_tracks(media: MediaInfo, prefer: str = "spa",
                   encoding: str | None = None) -> list[SubTrack]:
    """Junta subtítulos externos y embebidos en una sola lista numerada.

    `prefer` es el idioma que se asume cuando la pista no declara ninguno; se usa
    para desambiguar la codificación de los archivos externos.
    """
    tracks: list[SubTrack] = []
    next_id = 1

    for stream in media.subtitles:
        tracks.append(SubTrack(
            id=next_id,
            label=stream.label(),
            lang=stream.lang,
            source="embedded",
            stream=stream,
            forced=stream.forced,
            bitmap=stream.is_bitmap_sub,
            hint_lang=stream.lang if stream.lang != "und" else prefer,
        ))
        next_id += 1

    for path in find_external(media.path):
        tracks.append(_external_track(path, next_id, prefer, encoding))
        next_id += 1

    return tracks


def _external_track(path: Path, track_id: int, prefer: str,
                    encoding: str | None) -> SubTrack:
    lang, forced = _guess_lang(path.name)
    if lang == "und":
        # Sin marca de idioma en el nombre, el nombre del archivo dice más que
        # un genérico "Sin idioma".
        label = path.name
    else:
        label = f"{LANG_NAMES.get(lang.lower(), lang.upper())} · file"
    if forced:
        label += " · forced"

    return SubTrack(
        id=track_id,
        label=label,
        lang=lang,
        source="external",
        path=path,
        forced=forced,
        hint_lang=lang if lang != "und" else prefer,
        encoding=encoding,
    )


def track_for_file(path: str | Path, tracks: list[SubTrack], prefer: str = "spa",
                   encoding: str | None = None) -> SubTrack:
    """Devuelve la pista de un archivo puntual, agregándola a la lista si no estaba.

    Sirve para cuando el usuario pasa el .srt a mano, que puede estar en
    cualquier lado y no sólo al lado del video.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise SubtitleError(f"No such subtitle file: {path}")
    resolved = path.resolve()

    for existing in tracks:
        if existing.source == "external" and existing.path \
                and existing.path.resolve() == resolved:
            return existing

    track = _external_track(resolved, max((t.id for t in tracks), default=0) + 1,
                            prefer, encoding)
    tracks.append(track)
    return track


def pick_default(tracks: list[SubTrack], preferred: str = "spa") -> SubTrack | None:
    """Elige la pista que probablemente quiera el usuario: su idioma, no forzada."""
    usable = [t for t in tracks if not t.bitmap]
    if not usable:
        return None
    pref = preferred.lower()
    aliases = {pref} | {k for k, v in _FILENAME_LANG.items() if v == pref}

    def score(t: SubTrack) -> tuple:
        lang_hit = t.lang.lower() in aliases or t.lang.lower()[:2] == pref[:2]
        return (not lang_hit, t.forced, t.source == "embedded", t.id)

    best = min(usable, key=score)
    if best.lang.lower() in aliases or best.lang.lower()[:2] == pref[:2]:
        return best

    # Ninguna declara el idioma preferido. Si hay una sola y ni siquiera dice qué
    # idioma es, la activamos igual: alguien que deja un único .srt al lado de la
    # película lo puso para verlo. El caso típico es "pelicula.mp4" + "pelicula.srt".
    if len(usable) == 1 and usable[0].lang.lower() in ("und", ""):
        return usable[0]

    # Si declara un idioma distinto del preferido, respetamos ese dato y no
    # activamos nada; se cambia con 's' o desde el panel.
    return None


# --- Decodificación y parseo ---------------------------------------------------

def _codec_name(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return name.lower()


def _score_for_lang(text: str, lang: str) -> int:
    """Cuántas letras del alfabeto esperado aparecen en el texto decodificado."""
    alphabet = _LANG_ALPHABET.get(lang.lower()[:3])
    if alphabet is None:
        alphabet = _LANG_ALPHABET["spa"]
    if not alphabet:
        return 0
    wanted = set(alphabet)
    return sum(1 for ch in text.lower() if ch in wanted)


def _decode(data: bytes, lang: str = "spa", encoding: str | None = None) -> str:
    """Decodifica bytes de subtítulo.

    Los .srt no declaran su codificación en ningún lado, así que hay que
    adivinarla. UTF-8 se detecta solo porque es un código autoverificable, pero
    entre las codificaciones de un byte ningún detector estadístico acierta
    siempre: cp1250 y cp1252 difieren en un puñado de bytes y uno de ellos es
    justo el 0xF1, que es `ñ` en cp1252 y `ń` en cp1250. Ahí desempatamos con el
    idioma que ya conocemos de la pista, contando cuántas letras del alfabeto
    esperado produce cada candidata.
    """
    if encoding:
        return data.decode(encoding, "replace")

    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"),
                     (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    guess = None
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best is not None:
            guess = best.encoding
    except Exception:
        pass

    # Si lo detectado no es alfabeto latino (cirílico, griego, CJK), el detector
    # sí es confiable: esas codificaciones tienen firmas de bytes muy distintas.
    if guess is not None and _codec_name(guess) not in _LATIN_SBCS:
        return data.decode(guess, "replace")

    candidates = ["cp1252", "iso-8859-15", "cp1250", "iso-8859-2"]
    if guess:
        candidates.append(guess)

    best_text, best_score = None, -1
    for candidate in candidates:
        try:
            text = data.decode(candidate, "strict")
        except (UnicodeDecodeError, LookupError):
            continue
        score = _score_for_lang(text, lang)
        if score > best_score:  # empate ⇒ gana el primero, que es cp1252
            best_text, best_score = text, score

    if best_text is not None:
        return best_text
    return data.decode("cp1252", "replace")


def _clean(text: str) -> str:
    """Saca lo que el receptor no entiende, deja itálicas y negritas."""
    text = _ASS_TAGS.sub("", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    # Etiquetas de fuente/color: el receptor las ignora y a veces las muestra crudas.
    text = re.sub(r"</?font[^>]*>", "", text, flags=re.IGNORECASE)
    return text.strip()


def parse_timed_text(content: str) -> list[Cue]:
    """Parsea SRT o WebVTT (comparten estructura salvo el separador decimal)."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []

    for block in re.split(r"\n{2,}", content):
        block = block.strip("\n")
        if not block or block.upper().startswith("WEBVTT"):
            continue
        lines = block.split("\n")
        idx = next((i for i, ln in enumerate(lines) if _TIMESTAMP.search(ln)), None)
        if idx is None:
            continue  # bloque NOTE/STYLE o numeración suelta

        m = _TIMESTAMP.search(lines[idx])
        start = _to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        text = _clean("\n".join(lines[idx + 1:]))
        if text and end > start:
            cues.append(Cue(start, end, text))

    cues.sort(key=lambda c: c.start)
    return cues


def _to_seconds(h: str | None, m: str, s: str, frac: str) -> float:
    return (int(h or 0) * 3600 + int(m) * 60 + int(s)
            + int(frac.ljust(3, "0")[:3]) / 1000.0)


def load_file(path: Path, lang: str = "spa", encoding: str | None = None) -> list[Cue]:
    """Carga un subtítulo externo, convirtiéndolo con ffmpeg si hace falta."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SubtitleError(f"Could not read {path.name}: {exc}") from exc

    text = _decode(raw, lang, encoding)

    if path.suffix.lower() in (".srt", ".vtt"):
        cues = parse_timed_text(text)
        if cues:
            return cues
        # Un .srt malformado a veces lo salva ffmpeg; si no, caemos igual abajo.

    # ASS/SSA/SUB y demás los convierte ffmpeg. Le pasamos el texto ya decodificado
    # en UTF-8 por un temporal, así no tiene que adivinar la codificación de nuevo.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=path.suffix,
                                     delete=False) as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        return _ffmpeg_to_cues(["-sub_charenc", "UTF-8", "-i", tmp_path],
                               f"subtitle {path.name}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_embedded(media_path: Path, stream: Stream) -> list[Cue]:
    """Saca una pista de subtítulos de adentro del contenedor."""
    if stream.is_bitmap_sub:
        raise SubtitleError(
            f"Track '{stream.label()}' is image-based ({stream.codec}) and can't be "
            f"converted to text. Use --burn to burn it into the video."
        )
    # Los subtítulos dentro de un contenedor ya vienen en UTF-8 por especificación.
    return _ffmpeg_to_cues(
        ["-i", str(media_path), "-map", f"0:s:{stream.type_index}"],
        f"embedded track '{stream.label()}'",
    )


def _ffmpeg_to_cues(input_args: list[str], what: str) -> list[Cue]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           *input_args, "-f", "webvtt", "pipe:1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except FileNotFoundError as exc:
        raise SubtitleError("Couldn't find ffmpeg to convert the subtitles.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SubtitleError(f"ffmpeg timed out extracting {what}.") from exc

    if not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = err[-1] if err else "no details"
        raise SubtitleError(f"Could not convert {what}: {detail}")

    cues = parse_timed_text(proc.stdout.decode("utf-8", "replace"))
    if not cues:
        raise SubtitleError(f"{what.capitalize()} came out empty after conversion.")
    return cues


# --- Salida --------------------------------------------------------------------

def _fmt(t: float) -> str:
    if t < 0:
        t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def render_vtt(cues: list[Cue], shift: float = 0.0) -> str:
    """Serializa a WebVTT desplazando todo `shift` segundos hacia atrás.

    `shift` es la suma del offset del stream (cuando reiniciamos ffmpeg en un
    seek) y del delay manual que haya puesto el usuario.
    """
    out = ["WEBVTT", ""]
    for cue in cues:
        start, end = cue.start - shift, cue.end - shift
        if end <= 0:
            continue  # ya pasó, antes del inicio del stream
        out.append(f"{_fmt(start)} --> {_fmt(end)}")
        out.append(cue.text)
        out.append("")
    return "\n".join(out)
