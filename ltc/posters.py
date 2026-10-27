"""Portadas de verdad para películas y series, y sus enlaces a IMDb y Letterboxd.

La biblioteca se arma sin internet; esto es lo único de la grilla que sale a
buscar afuera, y se apaga con `--no-posters`. Usa el buscador de sugerencias de
IMDb (el que completa la caja de búsqueda del sitio): no pide key ni cuenta, y
por cada resultado da el id `tt…`, el año, el tipo y el póster. Con el id sale
también el enlace a Letterboxd, que redirige `letterboxd.com/imdb/<id>/` a la
película.

Un póster que el usuario ya tenga en la carpeta gana siempre (lo resuelve
`library.local_artwork` antes de llegar acá). Todo lo encontrado queda en
caché: el índice título → id en `posters/index.json` y las imágenes al lado,
así que cada título se busca una sola vez.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from . import __version__
from .logutil import get_logger

log = get_logger("posters")

SUGGEST = "https://v2.sg.media-imdb.com/suggestion/{first}/{query}.json"
USER_AGENT = f"ltc/{__version__}"
TIMEOUT = 6.0
WIDTH = 480

MOVIE_KINDS = ("movie", "tvMovie", "video")
SHOW_KINDS = ("tvSeries", "tvMiniSeries")

# Un título que no se encontró se vuelve a probar pasado un tiempo: IMDb suma
# películas, y el usuario puede haber renombrado el archivo.
MISS_TTL = 7 * 24 * 3600
# Sin conexión, no tiene sentido que cada tarjeta de la grilla espere su
# timeout: después de un error de red se deja de intentar un rato.
OFFLINE_BACKOFF = 120.0

_CACHE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "ltc"


@dataclass
class Match:
    imdb: str
    title: str
    year: int | None
    image: str  # URL del póster en IMDb; vacía si no tiene

    @property
    def links(self) -> dict:
        return {"imdb": f"https://www.imdb.com/title/{self.imdb}/",
                "letterboxd": f"https://letterboxd.com/imdb/{self.imdb}/"}


def _norm(text: str) -> str:
    """Para comparar títulos: sin acentos, mayúsculas ni puntuación."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]+", "", text.casefold())


def pick(results: list[dict], title: str, year: int | None,
         kinds: tuple[str, ...]) -> dict | None:
    """El resultado que corresponde al archivo, o None si ninguno convence.

    IMDb ya ordena por relevancia, así que el orden pesa; pero un título
    idéntico le gana a uno más popular ("Dark" no es "Dark Matter"), y con el
    año del nombre del archivo se descarta todo lo que no coincide. Los títulos
    en otro idioma ("El secreto de sus ojos") los resuelve IMDb por sus AKA: no
    se parecen al resultado, pero el año y el orden alcanzan.
    """
    want = _norm(title)
    best, best_score = None, float("-inf")
    for pos, result in enumerate(results):
        kind = result.get("qid")
        if kind not in kinds or not str(result.get("id", "")).startswith("tt"):
            continue
        found = result.get("y")
        if year and found and abs(found - year) > 1:
            continue
        name = _norm(result.get("l", ""))
        score = -pos - kinds.index(kind)
        if name == want:
            score += 10
        elif SequenceMatcher(None, name, want).ratio() >= 0.8:
            score += 5
        if year and found:
            score += 6 if found == year else 3
        if score > best_score:
            best, best_score = result, score
    return best


def sized(url: str, width: int = WIDTH) -> str:
    """Las imágenes de IMDb se piden achicadas cambiando el sufijo `._V1_…`."""
    return re.sub(r"\._V1_[^/]*?\.(jpg|png)$", rf"._V1_QL80_UX{width}_.\1", url)


def _http_get(url: str, timeout: float = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class Posters:
    def __init__(self, cache_dir: Path | None = None, enabled: bool = True,
                 fetch=None):
        self.dir = cache_dir or (_CACHE / "posters")
        self.enabled = enabled
        self._fetch = fetch or _http_get
        self._index_path = self.dir / "index.json"
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        # Una grilla de doscientas películas no puede salir toda junta a IMDb.
        self._slots = threading.BoundedSemaphore(3)
        self._offline_until = 0.0
        try:
            self._index: dict[str, dict] = json.loads(self._index_path.read_text("utf-8"))
        except (OSError, ValueError):
            self._index = {}

    # --- búsqueda ---

    def lookup(self, title: str, year: int | None, kind: str) -> Match | None:
        """El título en IMDb, de la caché o preguntando. `kind`: movie | show."""
        if not title:
            return None
        key = f"{kind}:{_norm(title)}:{year or ''}"
        entry = self._cached(key)
        if entry is None and self.enabled:
            with self._key_lock(key):
                entry = self._cached(key)
                if entry is None:
                    entry = self._search(title, year, kind)
                    if entry is not None:
                        self._store(key, entry)
        if not entry or entry.get("miss"):
            return None
        return Match(entry["imdb"], entry.get("title", ""), entry.get("year"),
                     entry.get("image", ""))

    def _cached(self, key: str) -> dict | None:
        with self._lock:
            entry = self._index.get(key)
        if entry and entry.get("miss") and time.time() - entry.get("at", 0) > MISS_TTL:
            return None
        return entry

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    def _search(self, title: str, year: int | None, kind: str) -> dict | None:
        """Una entrada para el índice, o None si no se pudo preguntar."""
        kinds = MOVIE_KINDS if kind == "movie" else SHOW_KINDS
        # Con el año la búsqueda es más precisa ("dune 2021"); si así no aparece,
        # puede que el año del archivo sea el del estreno local.
        queries = [f"{title} {year}", title] if year else [title]
        for query in queries:
            results = self._suggest(query)
            if results is None:
                return None
            found = pick(results, title, year, kinds)
            if found:
                log.info("IMDb: %r → %s (%s)", title, found["id"], found.get("l"))
                return {"imdb": found["id"], "title": found.get("l", ""),
                        "year": found.get("y"),
                        "image": (found.get("i") or {}).get("imageUrl", ""),
                        "at": time.time()}
        log.info("IMDb: nada para %r (%s)", title, year or "sin año")
        return {"miss": True, "at": time.time()}

    def _suggest(self, query: str) -> list[dict] | None:
        if time.monotonic() < self._offline_until:
            return None
        text = query.strip().lower()
        first = next((c for c in _norm(text) if c.isalpha()), "x")
        url = SUGGEST.format(first=first, query=urllib.parse.quote(text))
        try:
            with self._slots:
                body = self._fetch(url)
            return list(json.loads(body).get("d") or [])
        except (OSError, ValueError) as exc:
            log.warning("IMDb no respondió (%s); se reintenta en %.0fs",
                        exc, OFFLINE_BACKOFF)
            self._offline_until = time.monotonic() + OFFLINE_BACKOFF
            return None

    def _store(self, key: str, entry: dict) -> None:
        with self._lock:
            self._index[key] = entry
            payload = json.dumps(self._index, ensure_ascii=False, indent=1)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(payload, "utf-8")
            tmp.replace(self._index_path)
        except OSError as exc:
            log.warning("No se pudo guardar el índice de pósters: %s", exc)

    # --- imágenes ---

    def image(self, title: str, year: int | None, kind: str) -> Path | None:
        """El póster en disco, bajándolo la primera vez."""
        match = self.lookup(title, year, kind)
        if match is None or not match.image:
            return None
        out = self.dir / f"{match.imdb}.jpg"
        if out.is_file():
            return out
        if not self.enabled or time.monotonic() < self._offline_until:
            return None
        with self._key_lock(out.name):
            if out.is_file():
                return out
            try:
                with self._slots:
                    data = self._fetch(sized(match.image))
            except OSError as exc:
                log.warning("No se pudo bajar el póster de %s: %s", match.imdb, exc)
                return None
            if not data.startswith(b"\xff\xd8"):
                log.warning("El póster de %s no es un JPEG", match.imdb)
                return None
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=self.dir)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            Path(tmp).replace(out)
            return out
