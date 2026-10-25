"""Buscar y bajar subtítulos de OpenSubtitles.

La API vieja de opensubtitles.org (XML-RPC) se apagó a fines de 2023; lo que
queda es la REST de opensubtitles.com, con la misma base de subtítulos. Pide:

  * una API key de la aplicación, para todo (también para buscar);
  * una cuenta de opensubtitles.com, para bajar: el endpoint de descarga
    exige el token de sesión, y la cuota diaria va por cuenta.

Las dos cosas se leen de `~/.config/ltc/opensubtitles.json`:

    {"api_key": "...", "username": "...", "password": "..."}

o de las variables LTC_OPENSUBTITLES_API_KEY / _USERNAME / _PASSWORD.

Lo bajado se guarda al lado del video como `<video>.<idioma>.srt`, que es
justo lo que `subtitles.find_external` ya encuentra: después de la primera
descarga, el subtítulo es un archivo local más y no se vuelve a pedir nada.

La búsqueda va primero por el hash del archivo (el de OpenSubtitles: tamaño
más la suma de los primeros y últimos 64 KB). Un resultado que coincide por
hash fue subido para esa misma versión del video, así que viene sincronizado.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .logutil import get_logger

log = get_logger("opensubtitles")

API = "https://api.opensubtitles.com/api/v1"
# La API rechaza User-Agents genéricos: tiene que nombrar la aplicación.
USER_AGENT = f"ltc v{__version__}"
# La key de la aplicación ltc. Vacía hasta que el proyecto registre la suya;
# mientras tanto cada uno pone la propia en el archivo de configuración.
DEFAULT_API_KEY = ""
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() \
    / "ltc" / "opensubtitles.json"

# ISO 639-2 (lo que usan ffprobe y los nombres de archivo) ↔ códigos de la API.
_TO_API = {
    "spa": "es", "lat": "es", "eng": "en", "por": "pt-pt", "pob": "pt-br",
    "fre": "fr", "fra": "fr", "ita": "it", "ger": "de", "deu": "de",
    "jpn": "ja", "kor": "ko", "chi": "zh-cn", "zho": "zh-cn", "rus": "ru",
}
_FROM_API = {
    "es": "spa", "en": "eng", "pt-pt": "por", "pt-br": "pob", "fr": "fre",
    "it": "ita", "de": "ger", "ja": "jpn", "ko": "kor", "zh-cn": "chi",
    "zh-tw": "chi", "ru": "rus",
}


class OpenSubtitlesError(RuntimeError):
    pass


def api_language(iso3: str) -> str:
    return _TO_API.get(iso3.lower(), iso3.lower()[:2])


def file_language(api_code: str) -> str:
    """El sufijo para el nombre del archivo, el que `subtitles` reconoce."""
    return _FROM_API.get(api_code.lower(), api_code.lower())


def movie_hash(path: Path) -> str:
    """El hash de OpenSubtitles: tamaño + suma de palabras de 64 bits de las
    puntas del archivo, módulo 2^64."""
    chunk = 64 * 1024
    size = path.stat().st_size
    if size < chunk * 2:
        raise OpenSubtitlesError("File too small to hash.")
    value = size
    with path.open("rb") as fh:
        for offset in (0, size - chunk):
            fh.seek(offset)
            data = fh.read(chunk)
            for (word,) in struct.iter_unpack("<Q", data):
                value = (value + word) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


@dataclass
class Credentials:
    api_key: str = ""
    username: str = ""
    password: str = ""

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Credentials":
        data: dict = {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("No se pudo leer %s: %s", path, exc)
        env = os.environ.get
        return cls(
            api_key=env("LTC_OPENSUBTITLES_API_KEY") or data.get("api_key") or DEFAULT_API_KEY,
            username=env("LTC_OPENSUBTITLES_USERNAME") or data.get("username", ""),
            password=env("LTC_OPENSUBTITLES_PASSWORD") or data.get("password", ""),
        )


class OpenSubtitles:
    def __init__(self, credentials: Credentials | None = None):
        self.credentials = credentials or Credentials.load()
        self._token = ""
        self._base = API
        self._lock = threading.Lock()

    @property
    def can_search(self) -> bool:
        return bool(self.credentials.api_key)

    @property
    def can_download(self) -> bool:
        c = self.credentials
        return bool(c.api_key and c.username and c.password)

    # --- HTTP ---

    def _request(self, method: str, url: str, body: dict | None = None,
                 auth: bool = False) -> dict:
        headers = {
            "Api-Key": self.credentials.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self._login()}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}")
                message = detail.get("message") or detail.get("errors") or exc.reason
            except ValueError:
                message = exc.reason
            if exc.code == 401:
                self._token = ""
                message = f"OpenSubtitles rejected the credentials ({message})"
            elif exc.code == 406:
                message = f"Download limit reached: {message}"
            elif exc.code == 429:
                message = "Too many requests to OpenSubtitles; try again in a moment"
            raise OpenSubtitlesError(str(message)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OpenSubtitlesError(f"Could not reach OpenSubtitles: {exc}") from exc

    def _login(self) -> str:
        with self._lock:
            if self._token:
                return self._token
            c = self.credentials
            if not (c.username and c.password):
                raise OpenSubtitlesError(
                    f"Downloading needs an opensubtitles.com account in {CONFIG_PATH}")
            data = self._request("POST", f"{API}/login",
                                 {"username": c.username, "password": c.password})
            self._token = data.get("token", "")
            # Las cuentas VIP reciben otro host; el resto de los pedidos va ahí.
            if data.get("base_url"):
                self._base = f"https://{data['base_url']}/api/v1"
            if not self._token:
                raise OpenSubtitlesError("OpenSubtitles didn't return a session token.")
            return self._token

    # --- búsqueda y descarga ---

    def search(self, video: Path, *, title: str, year: int | None = None,
               season: int | None = None, episode: int | None = None,
               languages: list[str]) -> list[dict]:
        if not self.can_search:
            raise OpenSubtitlesError(f"No OpenSubtitles API key; see {CONFIG_PATH}")
        params: dict[str, str] = {"languages": ",".join(languages), "query": title.lower()}
        try:
            params["moviehash"] = movie_hash(video)
        except (OSError, OpenSubtitlesError):
            pass
        if season is not None and episode is not None:
            params.update(type="episode", season_number=str(season),
                          episode_number=str(episode))
        elif year:
            params.update(type="movie", year=str(year))
        # La API pide los parámetros ordenados y en minúscula; si no, redirige.
        query = urllib.parse.urlencode(sorted(params.items()))
        data = self._request("GET", f"{self._base}/subtitles?{query}")

        results = []
        for row in data.get("data", []):
            attrs = row.get("attributes", {})
            files = attrs.get("files") or []
            if not files:
                continue
            results.append({
                "file_id": files[0].get("file_id"),
                "language": attrs.get("language", ""),
                "release": attrs.get("release") or files[0].get("file_name") or "",
                "downloads": attrs.get("download_count", 0),
                "hash_match": bool(attrs.get("moviehash_match")),
                "hearing_impaired": bool(attrs.get("hearing_impaired")),
                "machine": bool(attrs.get("ai_translated") or attrs.get("machine_translated")),
            })
        order = {lang: i for i, lang in enumerate(languages)}
        results.sort(key=lambda r: (not r["hash_match"], r["machine"],
                                    order.get(r["language"], 99), -(r["downloads"] or 0)))
        return results[:40]

    def download(self, file_id: int, video: Path, language: str) -> Path:
        """Baja el subtítulo y lo deja al lado del video. Devuelve la ruta."""
        data = self._request("POST", f"{self._base}/download",
                             {"file_id": int(file_id)}, auth=True)
        link = data.get("link")
        if not link:
            raise OpenSubtitlesError(data.get("message") or "OpenSubtitles gave no download link.")
        content = self._fetch(link)
        if not content.strip():
            raise OpenSubtitlesError("OpenSubtitles returned an empty file.")

        ext = Path(data.get("file_name") or "x.srt").suffix.lower() or ".srt"
        target = _free_name(video, file_language(language), ext)
        # Los bytes tal cual: la detección de codificación ya la hace subtitles.
        target.write_bytes(content)
        log.info("Subtítulo de OpenSubtitles guardado en %s (quedan %s descargas hoy)",
                 target.name, data.get("remaining", "?"))
        return target


    def _fetch(self, link: str) -> bytes:
        req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise OpenSubtitlesError(f"Could not download the subtitle: {exc}") from exc


def _free_name(video: Path, lang: str, ext: str) -> Path:
    """`<video>.<lang>.srt`, o `<video>.<lang>.2.srt` si ya existe."""
    base = video.with_name(f"{video.stem}.{lang}{ext}")
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = video.with_name(f"{video.stem}.{n}.{lang}{ext}")
        if not candidate.exists():
            return candidate
        n += 1
