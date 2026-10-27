"""La configuración de ltc: un archivo TOML con valores por defecto sensatos.

    ~/.config/ltc/config.toml

El orden de prioridad es: opción de la línea de comandos, lo que diga este
archivo, el valor por defecto. Un archivo que no existe es lo mismo que uno
vacío, así que ltc funciona sin haber configurado nada.

Cada opción se declara una sola vez, como campo de `Config`, con su clave en
el archivo (`sección.nombre`) y una línea de ayuda. De ahí salen el parser, el
listado de `ltc config` y los comentarios del archivo que se escribe.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from .logutil import get_logger

log = get_logger("config")


def xdg(var: str, fallback: str) -> Path:
    """`$VAR` si está definida, y si no `fallback`; siempre con el ~ expandido."""
    return Path(os.environ.get(var) or fallback).expanduser()


def config_path() -> Path:
    override = os.environ.get("LTC_CONFIG")
    if override:
        return Path(override).expanduser()
    return xdg("XDG_CONFIG_HOME", "~/.config") / "ltc" / "config.toml"


def default_cache_dir() -> Path:
    return xdg("XDG_CACHE_HOME", "~/.cache") / "ltc"


def data_dir() -> Path:
    return xdg("XDG_DATA_HOME", "~/.local/share") / "ltc"


class ConfigError(ValueError):
    pass


def _opt(default, key: str, help: str, secret: bool = False):
    return field(default=default, metadata={"key": key, "help": help, "secret": secret})


@dataclass
class Config:
    # --- biblioteca ---
    library_folder: str = _opt(
        "", "library.folder",
        "Folder with your movies and series (subfolders are scanned too). "
        "With it set, plain `ltc` opens it as a library.")
    library_posters: bool = _opt(
        True, "library.posters",
        "Look up posters on IMDb (sends the titles to imdb.com).")

    # --- almacenamiento ---
    cache_dir: str = _opt(
        "", "storage.cache_dir",
        "Where thumbnails, posters and the log go. Empty = ~/.cache/ltc.")
    converted_dir: str = _opt(
        "", "storage.converted_dir",
        "Where finished conversions are kept. Empty = <cache_dir>/converted.")
    cache_limit_gb: float = _opt(
        40.0, "storage.cache_limit_gb",
        "Disk the conversions may use, in GB; the least recently used go first. "
        "0 = don't keep them.")

    # --- reproducción ---
    sub_lang: str = _opt(
        "spa", "playback.sub_lang",
        "Preferred subtitle language, ISO 639-2 (spa, eng, por, fre, ger, ita…).")
    sub_scale: float = _opt(1.0, "playback.sub_scale", "Subtitle size, 1.0 = normal.")
    surround: bool = _opt(
        False, "playback.surround",
        "Keep multichannel audio instead of downmixing it to stereo.")
    autoplay_next: bool = _opt(
        True, "playback.autoplay_next", "Library mode: start the next episode on its own.")

    # --- dispositivo y red ---
    device: str = _opt(
        "", "device.name",
        "Chromecast to use, by name or IP. Empty = look around and ask if there are several.")
    port: int = _opt(
        8642, "network.port",
        "Port of the panel. Fixed, so the address can be saved on your phone.")

    # --- rendimiento ---
    hwaccel: bool = _opt(
        True, "performance.hwaccel", "Re-encode on the GPU when there is one.")

    # --- OpenSubtitles ---
    opensubtitles_api_key: str = _opt(
        "", "opensubtitles.api_key", "Application API key from opensubtitles.com.")
    opensubtitles_username: str = _opt(
        "", "opensubtitles.username", "opensubtitles.com account (downloads count against it).")
    opensubtitles_password: str = _opt(
        "", "opensubtitles.password", "Its password, stored in plain text: this file is private "
        "to you (mode 600).", secret=True)

    # Qué claves venían escritas en el archivo (el resto son valores por defecto).
    explicit: set[str] = field(default_factory=set, repr=False, compare=False)

    # --- rutas resueltas ---

    @property
    def library_path(self) -> Path | None:
        return Path(self.library_folder).expanduser() if self.library_folder else None

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir).expanduser() if self.cache_dir else default_cache_dir()

    @property
    def converted_path(self) -> Path:
        return Path(self.converted_dir).expanduser() if self.converted_dir \
            else self.cache_path / "converted"

    @property
    def spool_path(self) -> Path:
        """Intermedios del reencodado, junto a las conversiones: el paso final
        a la caché es un rename en el mismo disco y no una copia de un giga.
        (La caché sólo desaloja los `.mp4` de su carpeta, no mira acá.)"""
        return self.converted_path / ".tmp"

    @property
    def log_path(self) -> Path:
        return self.cache_path / "ltc.log"


# --- el registro de opciones, a partir de los campos ---------------------------

def options() -> list[Any]:
    """Los campos que son opciones (todos menos `explicit`)."""
    return [f for f in fields(Config) if "key" in f.metadata]


def _by_key() -> dict[str, Any]:
    return {f.metadata["key"]: f for f in options()}


def default_of(f) -> Any:
    return f.default if f.default is not MISSING else f.default_factory()


def kind_of(f) -> type:
    return type(default_of(f))


def parse_value(f, raw: Any) -> Any:
    """Convierte lo que vino (del TOML o de `ltc config set`) al tipo del campo."""
    key, kind = f.metadata["key"], kind_of(f)
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "yes", "on", "1"):
            return True
        if isinstance(raw, str) and raw.strip().lower() in ("false", "no", "off", "0"):
            return False
        raise ConfigError(f"{key} must be true or false (got {raw!r})")
    if kind in (int, float):
        if isinstance(raw, bool):
            raise ConfigError(f"{key} must be a number (got {raw!r})")
        try:
            value = kind(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be a number (got {raw!r})") from None
        if value < 0:
            raise ConfigError(f"{key} can't be negative")
        if key == "network.port" and value > 65535:
            raise ConfigError("network.port must be between 0 and 65535")
        if key == "playback.sub_scale" and value == 0:
            raise ConfigError("playback.sub_scale can't be 0")
        return value
    if not isinstance(raw, str):
        raise ConfigError(f"{key} must be text (got {raw!r})")
    return raw.strip()


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return Config()
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Can't read {path}: {exc}") from exc

    known, values, explicit = _by_key(), {}, set()
    for section, body in data.items():
        if not isinstance(body, dict):
            raise ConfigError(f"{path}: {section!r} should be a [section]")
        for name, raw in body.items():
            f = known.get(f"{section}.{name}")
            if f is None:
                log.warning("%s: unknown option %s.%s", path, section, name)
                continue
            values[f.name] = parse_value(f, raw)
            explicit.add(f.name)
    return Config(**values, explicit=explicit)


# --- escritura ------------------------------------------------------------------

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render(cfg: Config) -> str:
    """El archivo completo: lo que se fijó, y el resto comentado con su ayuda."""
    lines = [
        "# ltc configuration. `ltc config` shows and changes these; editing by hand is fine",
        "# too, but ltc rewrites the file when it changes a value, so comments are not kept.",
        "# Lines starting with # show the default.",
    ]
    section = None
    for f in options():
        key = f.metadata["key"]
        sec, name = key.split(".", 1)
        if sec != section:
            lines += ["", f"[{sec}]"]
            section = sec
        lines.append(f"# {f.metadata['help']}")
        value = getattr(cfg, f.name)
        if f.name in cfg.explicit:
            lines.append(f"{name} = {_toml_value(value)}")
        else:
            lines.append(f"# {name} = {_toml_value(default_of(f))}")
    return "\n".join(lines) + "\n"


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Se escribe aparte y se renombra: nunca queda un archivo a medias, y el
    # modo 600 (hay una contraseña posible) vale desde el primer byte.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render(cfg))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def set_value(cfg: Config, key: str, raw: Any) -> None:
    f = _by_key().get(key)
    if f is None:
        raise ConfigError(f"Unknown option {key!r}. See: ltc config")
    setattr(cfg, f.name, parse_value(f, raw))
    cfg.explicit.add(f.name)


def unset_value(cfg: Config, key: str) -> None:
    f = _by_key().get(key)
    if f is None:
        raise ConfigError(f"Unknown option {key!r}. See: ltc config")
    setattr(cfg, f.name, default_of(f))
    cfg.explicit.discard(f.name)


def field_for(key: str):
    f = _by_key().get(key)
    if f is None:
        raise ConfigError(f"Unknown option {key!r}. See: ltc config")
    return f


# --- la configuración activa del proceso ---------------------------------------

_active: Config | None = None


def get() -> Config:
    """La configuración de este proceso, leída la primera vez que se pide."""
    global _active
    if _active is None:
        _active = load()
    return _active


def set_active(cfg: Config | None) -> None:
    """Fija (o, con None, olvida) la configuración activa. Para tests y `ltc setup`."""
    global _active
    _active = cfg
