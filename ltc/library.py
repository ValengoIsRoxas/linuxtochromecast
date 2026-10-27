"""La biblioteca: películas y series a partir de una carpeta, sin internet.

Todo sale del nombre de los archivos y de las carpetas, que es lo único que hay
garantizado. No probamos los videos al escanear (ffprobe sobre mil archivos
tarda minutos); eso se hace recién cuando alguien abre uno.

Las convenciones que se reconocen son las que usan los torrents y los
servidores tipo Kodi/Jellyfin:

    Dark/Season 1/Dark.S01E03.1080p.mkv      serie, por SxxEyy
    The.Office.3x07.avi                      serie, por NxNN
    Breaking Bad/Temporada 2/05 - Breakage.mkv  serie, por carpeta de temporada
    Blade.Runner.2049.2017.1080p.mkv         película, año 2017

Las miniaturas también son locales: primero un póster que ya esté en la carpeta
(`poster.jpg`, `folder.jpg`, `<video>.jpg`), y si no, un cuadro del propio video
sacado con ffmpeg y guardado en caché. Los pósters de IMDb para películas y
series son aparte, en `posters`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logutil import get_logger

log = get_logger("library")

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts",
    ".wmv", ".flv", ".mpg", ".mpeg", ".ogv",
}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
POSTER_NAMES = ("poster", "folder", "cover", "show")

_CACHE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "ltc"
_DATA = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "ltc"

# --- nombres --------------------------------------------------------------------

_SXXEYY = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})(?!\d)")
_NXNN = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?!\d)")
# Sólo se usan dentro de una carpeta de temporada: sueltos confundirían
# "Star Wars Episode 4" con un capítulo.
_EP_ONLY = re.compile(
    r"(?i)(?<![a-z0-9])(?:e|ep|episode|episodio|cap|capitulo|capítulo|chapter)"
    r"[ ._-]*(\d{1,3})(?!\d)")
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,3})(?!\d)")
_SEASON_DIR = re.compile(
    r"(?i)^(?:season|temporada|staffel|saison|series|s)[ ._-]*(\d{1,2})(?!\d)")
_EXTRA_EPISODE = re.compile(r"(?i)^[ ._-]*-?[ ._-]*e?\d{1,3}(?!\d)(?![a-z])")
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_QUALITY = re.compile(
    r"(?i)(?<![a-z0-9])(?:2160p|1080p|720p|576p|480p|4k|uhd|hdr10?|bluray|blu-ray|"
    r"bdrip|brrip|bdremux|remux|web-?dl|webrip|hdtv|hdrip|dvdrip|x264|x265|"
    r"h ?264|h ?265|hevc|xvid|10bit|aac|e?ac3|dts|ddp?5 ?1|proper|repack|"
    r"unrated|imax|multi|dual|latino|castellano|subbed|dubbed)(?![a-z0-9])")
_LEADING_GROUP = re.compile(r"^\s*\[[^\]]*\]\s*")
_SAMPLE = re.compile(r"(?i)(?<![a-z])sample(?![a-z])")
# "Star Trek TNG Season 4 (1080p x265)" o "Dark.S01.1080p": la temporada en
# medio del nombre de la carpeta, con la serie adelante. La "s" sola va pegada
# al número para no leer "Ocean's 11" como temporada 11.
_SEASON_IN_NAME = re.compile(
    r"(?i)(?<![a-z0-9])(?:(?:season|temporada|staffel|saison)[ ._-]*|s)\d{1,2}(?!\d)")

# Material extra, que no es ni película ni capítulo. Son los nombres que usan
# Kodi y Jellyfin, que es como vienen armados los packs de Blu-ray.
EXTRA_DIRS = {
    "extras", "extra", "featurettes", "behind the scenes", "deleted scenes",
    "interviews", "scenes", "shorts", "trailers", "bonus", "other",
}
_EXTRA_FILE = re.compile(
    r"(?i)(?:^trailer$|-(?:trailer|featurette|behindthescenes|deleted|interview|scene|short|other)$)")


def clean_name(text: str) -> str:
    """Pasa un nombre de release a algo presentable.

    Los puntos sólo se tratan como espacios cuando el nombre no tiene espacios:
    en "Mr. Robot" el punto es parte del título; en "Mr.Robot.S01" no.
    """
    text = _LEADING_GROUP.sub("", text)
    text = text.replace("_", " ")
    if " " not in text.strip():
        text = text.replace(".", " ")
    cut = _QUALITY.search(text)
    if cut:
        text = text[:cut.start()]
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .-–—_[](){}")


def _key(name: str) -> str:
    """Clave para agrupar: "Dark", "dark" y "Dark (2017)" son la misma serie."""
    name = _YEAR.sub("", name)
    return re.sub(r"[^0-9a-z]+", "", name.casefold())


def season_of_dir(name: str) -> int | None:
    match = _SEASON_DIR.match(name.strip())
    return int(match.group(1)) if match else None


@dataclass
class ParsedEpisode:
    show: str          # vacío si el archivo no lo dice (se usa la carpeta)
    season: int
    episode: int
    title: str


def parse_episode(stem: str, season_hint: int | None = None) -> ParsedEpisode | None:
    """Reconoce un capítulo por el nombre; `season_hint` sale de la carpeta."""
    for pattern in (_SXXEYY, _NXNN):
        match = pattern.search(stem)
        if match:
            rest = _EXTRA_EPISODE.sub("", stem[match.end():], count=1)
            return ParsedEpisode(
                show=clean_name(stem[:match.start()]),
                season=int(match.group(1)),
                episode=int(match.group(2)),
                title=clean_name(rest),
            )

    if season_hint is None:
        return None
    match = _EP_ONLY.search(stem) or _LEADING_NUMBER.search(stem)
    if not match:
        return None
    return ParsedEpisode(
        show=clean_name(stem[:match.start()]),
        season=season_hint,
        episode=int(match.group(1)),
        title=clean_name(stem[match.end():]),
    )


def parse_movie(stem: str) -> tuple[str, int | None]:
    """Título y año. Se usa el último año que tenga título adelante.

    Así "Blade Runner 2049 (2017)" da 2017 y no 2049, y "1917.2019" da "1917".
    """
    for match in reversed(list(_YEAR.finditer(stem))):
        title = clean_name(stem[:match.start()])
        if title:
            return title, int(match.group(1))
    return clean_name(stem) or stem, None


# --- modelo ---------------------------------------------------------------------

def _id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:12]


@dataclass
class Movie:
    id: str
    path: Path
    title: str
    year: int | None = None
    kind: str = "movie"

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Episode:
    id: str
    path: Path
    show_id: str
    show: str
    season: int
    episode: int
    title: str = ""
    kind: str = "episode"

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    @property
    def label(self) -> str:
        return f"{self.show} · {self.code}" + (f" · {self.title}" if self.title else "")


@dataclass
class Show:
    id: str
    name: str
    folder: Path | None
    episodes: list[Episode] = field(default_factory=list)
    kind: str = "show"

    @property
    def seasons(self) -> list[int]:
        return sorted({e.season for e in self.episodes})


Item = Movie | Episode


@dataclass
class Library:
    root: Path
    movies: list[Movie] = field(default_factory=list)
    shows: list[Show] = field(default_factory=list)
    scanned_at: float = 0.0

    def __post_init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._shows: dict[str, Show] = {}
        self._reindex()

    def _reindex(self) -> None:
        self._items = {m.id: m for m in self.movies}
        self._shows = {s.id: s for s in self.shows}
        for show in self.shows:
            for ep in show.episodes:
                self._items[ep.id] = ep

    def item(self, item_id: str) -> Item | None:
        return self._items.get(item_id)

    def show(self, show_id: str) -> Show | None:
        return self._shows.get(show_id)

    def __len__(self) -> int:
        return len(self._items)

    def next_episode(self, item: Item) -> Episode | None:
        if not isinstance(item, Episode):
            return None
        show = self._shows.get(item.show_id)
        if show is None:
            return None
        for i, ep in enumerate(show.episodes):
            if ep.id == item.id:
                return show.episodes[i + 1] if i + 1 < len(show.episodes) else None
        return None

    # --- escaneo ---

    @classmethod
    def scan(cls, root: str | Path) -> "Library":
        root = Path(root).expanduser().resolve()
        movies: list[Movie] = []
        groups: dict[str, list[tuple[Episode, str, Path | None]]] = {}

        for path in _walk_videos(root):
            season_hint = season_of_dir(path.parent.name)
            parsed = parse_episode(path.stem, season_hint)
            if parsed is None:
                title, year = parse_movie(path.stem)
                movies.append(Movie(_id(str(path)), path, title, year))
                continue

            folder = _show_folder(path, root)
            # La carpeta la nombró una persona; el archivo, un grupo de release
            # que abrevia ("Star Trek TNG"). Cuando la carpeta dice claramente
            # que es una temporada de algo, ese algo es el nombre de la serie.
            name = _folder_show_name(path, root)
            if name:
                key = _key(name)
            else:
                name = parsed.show or (clean_name(folder.name) if folder else "") \
                    or clean_name(path.parent.name) or path.stem
                key = _key(name) or _key(path.parent.name) or path.stem
                # Si carpeta y archivo dicen lo mismo, la carpeta escribe mejor
                # ("The Office", no "the office").
                if folder is not None and _key(clean_name(folder.name)) == key:
                    name = clean_name(folder.name)
            ep = Episode(_id(str(path)), path, "", "", parsed.season,
                         parsed.episode, parsed.title)
            groups.setdefault(key, []).append((ep, name, folder))

        shows: list[Show] = []
        for key, entries in groups.items():
            name = _pick_name([n for _, n, _ in entries])
            folders = {f for _, _, f in entries if f is not None}
            show = Show(_id("show:" + key), name,
                        folders.pop() if len(folders) == 1 else None)
            for ep, _, _ in entries:
                ep.show_id, ep.show = show.id, name
                show.episodes.append(ep)
            show.episodes.sort(key=lambda e: (e.season, e.episode, e.path.name))
            shows.append(show)

        movies.sort(key=lambda m: (m.title.casefold(), m.year or 0))
        shows.sort(key=lambda s: s.name.casefold())
        return cls(root, movies, shows, time.time())


def _walk_videos(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # Ocultas, muestras y extras fuera; ordenado para que el resultado no
        # dependa del sistema de archivos.
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and not _SAMPLE.search(d)
                             and d.casefold() not in EXTRA_DIRS)
        for name in sorted(filenames):
            if name.startswith(".") or _SAMPLE.search(name) \
                    or _EXTRA_FILE.search(Path(name).stem):
                continue
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                yield Path(dirpath) / name


def _show_folder(path: Path, root: Path) -> Path | None:
    """La carpeta de la serie: la del archivo, o la de arriba si es "Season N"."""
    folder = path.parent
    if season_of_dir(folder.name) is not None:
        folder = folder.parent
    return folder if folder != root and root in folder.parents else None


def _folder_show_name(path: Path, root: Path) -> str:
    """El nombre de la serie según la carpeta, sólo si la carpeta no deja dudas.

    Vale "Dark/Season 1/…" (la de arriba de una carpeta de temporada) y
    "Star Trek TNG Season 4 (1080p)/…" (lo que va antes de la temporada). Una
    carpeta cualquiera como "Series/" no dice nada y devuelve "".
    """
    parent = path.parent
    if season_of_dir(parent.name) is not None:
        show_dir = parent.parent
        if show_dir != root and root in show_dir.parents:
            return clean_name(show_dir.name)
        return ""
    match = _SEASON_IN_NAME.search(parent.name)
    if match and match.start() > 0:
        return clean_name(parent.name[:match.start()])
    return ""


def _pick_name(names: list[str]) -> str:
    """El nombre más repetido; ante empate, uno con mayúsculas antes que "dark"."""
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return max(counts, key=lambda n: (counts[n], n != n.lower(), -names.index(n)))


# --- imágenes -------------------------------------------------------------------

def _existing_image(directory: Path, stems: tuple[str, ...]) -> Path | None:
    for stem in stems:
        for ext in IMAGE_EXTENSIONS:
            candidate = directory / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
    return None


def local_artwork(library: Library, target: Item | Show) -> Path | None:
    """Un póster o miniatura que el usuario ya tenga en disco, si hay."""
    if isinstance(target, Show):
        return _existing_image(target.folder, POSTER_NAMES) if target.folder else None
    stem = target.path.stem
    found = _existing_image(target.path.parent, (f"{stem}-poster", f"{stem}-thumb", stem))
    if found or not isinstance(target, Movie):
        return found
    # "poster.jpg" en la carpeta sólo vale si la película está sola en ella;
    # en una carpeta con veinte películas sería el póster de cualquiera.
    siblings = [m for m in library.movies if m.path.parent == target.path.parent]
    if len(siblings) == 1:
        return _existing_image(target.path.parent, POSTER_NAMES)
    return None


class Thumbnailer:
    """Saca un cuadro del video con ffmpeg y lo guarda en caché."""

    def __init__(self, cache_dir: Path | None = None, width: int = 480):
        self.cache_dir = cache_dir or (_CACHE / "thumbs")
        self.width = width
        # ffmpeg decodificando en paralelo una grilla entera tira abajo la
        # máquina; dos a la vez alcanza para que la página se vaya llenando.
        self._slots = threading.BoundedSemaphore(2)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._failed: set[str] = set()

    def _cache_path(self, video: Path) -> Path | None:
        try:
            st = video.stat()
        except OSError:
            return None
        key = _id(f"{video}|{st.st_mtime_ns}|{st.st_size}|{self.width}")
        return self.cache_dir / f"{key}.jpg"

    def get(self, video: Path) -> Path | None:
        out = self._cache_path(video)
        if out is None:
            return None
        if out.is_file():
            return out
        key = out.name
        if key in self._failed:
            return None

        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if out.is_file():
                return out
            with self._slots:
                ok = self._extract(video, out)
            if not ok:
                self._failed.add(key)
                return None
            return out

    def _extract(self, video: Path, out: Path) -> bool:
        out.parent.mkdir(parents=True, exist_ok=True)
        duration = _quick_duration(video)
        # Un poco entrado el video: el cuadro 0 suele ser negro o un logo.
        offsets = [min(duration * 0.15, 600.0), 0.0] if duration else [30.0, 0.0]
        fd, tmp_name = tempfile.mkstemp(suffix=".jpg", dir=out.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            for at in offsets:
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                       "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
                       "-vf", f"scale={self.width}:-2", "-q:v", "4", str(tmp)]
                try:
                    subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=30)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log.warning("Miniatura de %s falló: %s", video.name, exc)
                    continue
                if tmp.is_file() and tmp.stat().st_size > 0:
                    tmp.replace(out)
                    return True
            log.info("No se pudo sacar un cuadro de %s", video.name)
            return False
        finally:
            tmp.unlink(missing_ok=True)


def _quick_duration(video: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        return float(result.stdout.strip() or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0.0


# --- progreso -------------------------------------------------------------------

# Menos que esto no vale la pena retomar: es lo que se tarda en darse cuenta de
# que se eligió mal.
RESUME_MIN = 30.0


def counts_as_watched(position: float, duration: float) -> bool:
    """Los créditos no se miran: faltando poco ya cuenta como vista."""
    if not duration:
        return False
    return position >= duration - max(60.0, duration * 0.05)


class ProgressStore:
    """Dónde quedó cada video, en un JSON indexado por ruta absoluta.

    Se indexa por ruta y no por el id de la biblioteca para que sobreviva a
    abrir la misma carpeta desde otro lado (`ltc ~/Videos` y `ltc ~/Videos/Series`).
    """

    def __init__(self, path: Path | None = None, save_every: float = 10.0):
        self.path = path or (_DATA / "progress.json")
        self.save_every = save_every
        self._lock = threading.Lock()
        self._dirty = False
        self._saved_at = 0.0
        self._data: dict[str, dict] = {}
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, video: Path) -> dict | None:
        with self._lock:
            entry = self._data.get(str(video))
            return dict(entry) if entry else None

    def resume_point(self, video: Path) -> float:
        entry = self.get(video)
        if not entry or entry.get("watched"):
            return 0.0
        position = float(entry.get("position") or 0.0)
        return position if position >= RESUME_MIN else 0.0

    def update(self, video: Path, position: float, duration: float) -> None:
        with self._lock:
            entry = self._data.setdefault(str(video), {})
            entry["position"] = round(position, 1)
            if duration:
                entry["duration"] = round(duration, 1)
            entry["watched"] = counts_as_watched(position, duration) or (
                entry.get("watched", False) and position < RESUME_MIN)
            entry["updated"] = time.time()
            self._dirty = True
        if time.monotonic() - self._saved_at >= self.save_every:
            self.flush()

    def mark_watched(self, video: Path, duration: float = 0.0) -> None:
        with self._lock:
            entry = self._data.setdefault(str(video), {})
            entry.update(watched=True, position=0.0, updated=time.time())
            if duration:
                entry["duration"] = round(duration, 1)
            self._dirty = True
        self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = json.dumps(self._data, ensure_ascii=False, indent=1)
            self._dirty = False
        self._saved_at = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(payload, "utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("No se pudo guardar el progreso en %s: %s", self.path, exc)


# --- para la UI -----------------------------------------------------------------

def _progress_json(store: ProgressStore, video: Path) -> dict | None:
    entry = store.get(video)
    if not entry:
        return None
    return {
        "position": entry.get("position", 0.0),
        "duration": entry.get("duration", 0.0),
        "watched": bool(entry.get("watched")),
        "updated": entry.get("updated", 0.0),
    }


def item_json(item: Item, store: ProgressStore, saved: set[str] | None = None) -> dict:
    data = {"id": item.id, "kind": item.kind, "progress": _progress_json(store, item.path)}
    if saved is not None:
        data["saved"] = str(item.path.resolve()) in saved
    if isinstance(item, Movie):
        data.update(title=item.title, year=item.year)
    else:
        data.update(title=item.title, show=item.show, show_id=item.show_id,
                    season=item.season, episode=item.episode, code=item.code)
    return data


def library_json(library: Library, store: ProgressStore,
                 saved: set[str] | None = None) -> dict:
    """Todo lo que pinta la grilla. `saved`: orígenes con conversión guardada."""
    saved = saved if saved is not None else set()
    movies = [item_json(m, store, saved) for m in library.movies]
    shows = []
    for show in library.shows:
        episodes = [item_json(e, store, saved) for e in show.episodes]
        shows.append({
            "id": show.id, "name": show.name, "seasons": show.seasons,
            "episodes": episodes,
            "unwatched": sum(1 for e in episodes
                             if not (e["progress"] or {}).get("watched")),
        })
    return {
        "root": str(library.root),
        "movies": movies,
        "shows": shows,
        "continue": _continue_watching(library, store, saved),
    }


def _continue_watching(library: Library, store: ProgressStore, saved: set[str],
                       limit: int = 12) -> list[dict]:
    """Lo empezado y no terminado, más el capítulo que sigue al último visto."""
    rows: list[tuple[float, dict]] = []
    for movie in library.movies:
        entry = store.get(movie.path)
        if entry and not entry.get("watched") and entry.get("position", 0) >= RESUME_MIN:
            rows.append((entry.get("updated", 0.0), item_json(movie, store, saved)))

    for show in library.shows:
        # Por serie, sólo lo último que se tocó: no tiene sentido ofrecer
        # retomar tres capítulos de la misma serie.
        latest, latest_at = None, 0.0
        for ep in show.episodes:
            entry = store.get(ep.path)
            if entry and entry.get("updated", 0.0) > latest_at:
                latest, latest_at = (ep, entry), entry.get("updated", 0.0)
        if latest is None:
            continue
        ep, entry = latest
        if entry.get("watched"):
            nxt = library.next_episode(ep)
            if nxt is None or (store.get(nxt.path) or {}).get("watched"):
                continue
            ep = nxt
        elif entry.get("position", 0) < RESUME_MIN:
            continue
        rows.append((latest_at, item_json(ep, store, saved)))

    rows.sort(key=lambda r: r[0], reverse=True)
    return [r for _, r in rows[:limit]]
