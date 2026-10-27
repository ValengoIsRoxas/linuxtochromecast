"""`ltc setup`, `ltc config` y `ltc uninstall`: todo lo que pasa fuera de castear.

  * `setup`      asistente que pregunta lo que se puede querer decidir antes de
                 empezar (carpetas, idioma, Chromecast, firewall) y deja la
                 integración con el gestor de archivos. Se puede repetir.
  * `config`     ver y cambiar la configuración sin editar el TOML.
  * `uninstall`  deshace lo anterior; lo que ocupa espacio se pregunta aparte.

Ninguna de estas cosas pide sudo salvo abrir el puerto del firewall, y eso sólo
si se acepta, y lo hace `sudo` con su propia contraseña, no este programa.
"""

from __future__ import annotations

import argparse
import getpass
import itertools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import config
from .term import bold, cyan, dim, green, red, yellow

PACKAGE = "linuxtochromecast"
VIDEO_MIMES = (
    "video/mp4;video/x-matroska;video/x-msvideo;video/quicktime;video/webm;"
    "video/mpeg;video/x-ms-wmv;video/x-flv;video/3gpp;video/ogg;video/mp2t;video/x-m4v;"
)
PATH_KEYS = {"library.folder", "storage.cache_dir", "storage.converted_dir"}


def ok(text: str) -> None:
    print(f"  {green('✓')} {text}")


def warn(text: str) -> None:
    print(f"  {yellow('!')} {text}")


def fail(text: str, code: int = 1) -> int:
    print(f"\n{red('✗')} {text}", file=sys.stderr)
    return code


# --- preguntas ------------------------------------------------------------------

class Ask:
    """Las preguntas del asistente. Sin terminal (o con --yes) responde el valor por defecto."""

    def __init__(self, assume_yes: bool):
        self.assume_yes = assume_yes or not sys.stdin.isatty()

    def text(self, question: str, default: str = "") -> str:
        if self.assume_yes:
            return default
        shown = f" [{default}]" if default else ""
        try:
            return input(f"  {question}{shown} ").strip() or default
        except EOFError:
            return default

    def yes(self, question: str, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        hint = "Y/n" if default else "y/N"
        while True:
            try:
                answer = input(f"  {question} [{hint}] ").strip().lower()
            except EOFError:
                return default
            if not answer:
                return default
            if answer in ("y", "yes", "s", "si", "sí"):
                return True
            if answer in ("n", "no"):
                return False

    def secret(self, question: str) -> str:
        if self.assume_yes:
            return ""
        try:
            return getpass.getpass(f"  {question} ")
        except EOFError:
            return ""


def section(title: str) -> None:
    print(f"\n{bold(title)}")


# --- utilidades de disco --------------------------------------------------------

def _nearest_existing(path: Path) -> Path:
    path = path.expanduser()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(_nearest_existing(path)).free / 1024 ** 3
    except OSError:
        return 0.0


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def fmt_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return ""


def absolute(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())


def count_videos(folder: Path, limit: int = 5000) -> int:
    from .library import _walk_videos
    return sum(1 for _ in itertools.islice(_walk_videos(folder), limit))


def suggest_cache_limit(directory: Path) -> float:
    """Un cuarto del espacio libre, entre 2 y 40 GB: sobra para varias
    películas y no se come un disco chico."""
    return float(max(2, min(40, int(free_gb(directory) / 4))))


# --- conversiones guardadas: mover de una carpeta a otra ------------------------

def saved_conversions(directory: Path) -> list[Path]:
    try:
        return sorted(directory.glob("*.mp4"))
    except OSError:
        return []


def move_conversions(old: Path, new: Path) -> int:
    """Pasa las conversiones (el .mp4 y su .json) a la carpeta nueva."""
    new.mkdir(parents=True, exist_ok=True)
    moved = 0
    for mp4 in saved_conversions(old):
        for src in (mp4, mp4.with_suffix(".json")):
            if src.exists():
                shutil.move(str(src), str(new / src.name))
        moved += 1
    return moved


def offer_to_move(old: Path, new: Path, ask: Ask) -> None:
    if old.resolve() == new.expanduser().resolve():
        return
    found = saved_conversions(old)
    if not found:
        return
    size = fmt_size(sum(p.stat().st_size for p in found))
    if ask.assume_yes:
        warn(f"{len(found)} saved conversions ({size}) stay in {old}; "
             "move them there by hand or they'll be made again.")
        return
    if ask.yes(f"Move the {len(found)} conversions already saved ({size}) to the new folder?"):
        try:
            move_conversions(old, new)
            ok(f"moved {len(found)} conversions")
        except OSError as exc:
            warn(f"couldn't move them: {exc}")


# --- firewall -------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, (done.stdout + done.stderr).strip()


@dataclass
class Firewall:
    tool: str
    is_open: bool | None        # None: no se pudo saber sin permisos
    commands: list[list[str]]   # lo que habría que correr, con sudo


def check_firewall(port: int) -> Firewall | None:
    """Si hay un firewall activo que pueda bloquear el puerto del panel."""
    if shutil.which("firewall-cmd"):
        code, _ = _run(["firewall-cmd", "--state"])
        if code == 0:
            code, _ = _run(["firewall-cmd", f"--query-port={port}/tcp"])
            return Firewall("firewalld", True if code == 0 else False if code == 1 else None, [
                ["firewall-cmd", "--permanent", f"--add-port={port}/tcp"],
                ["firewall-cmd", "--reload"],
            ])
    if shutil.which("ufw"):
        code, out = _run(["systemctl", "is-active", "ufw"])
        if code == 0 and out.strip() == "active":
            # `ufw status` pide root; `allow` es idempotente, así que no importa.
            return Firewall("ufw", None, [["ufw", "allow", f"{port}/tcp"]])
    return None


def setup_firewall(port: int, ask: Ask) -> None:
    fw = check_firewall(port)
    if fw is None:
        ok("no active firewall found, nothing to open")
        return
    if fw.is_open:
        ok(f"{fw.tool}: port {port} is open")
        return
    state = "is closed" if fw.is_open is False else "may be closed"
    warn(f"{fw.tool}: port {port} {state}. The Chromecast has to reach it to fetch the video.")
    shown = " && ".join("sudo " + " ".join(c) for c in fw.commands)
    if ask.assume_yes:
        print(f"      to open it: {shown}")
        return
    print(f"      {dim(shown)}")
    if not ask.yes("Run that now? (sudo asks for your password)"):
        print(f"      {dim('Skipped; if the panel never loads on the TV, this is the first thing to check.')}")
        return
    for cmd in fw.commands:
        if subprocess.run(["sudo", *cmd]).returncode != 0:
            warn("that didn't work; you can run it yourself later")
            return
    ok(f"port {port} opened")


# --- integración con el gestor de archivos --------------------------------------

def bin_dir() -> Path:
    """Donde está `ltc` (pipx lo deja en ~/.local/bin), para dejar ahí `ltc-open`."""
    override = os.environ.get("LTC_BIN_DIR")
    if override:
        return Path(override).expanduser()
    found = shutil.which("ltc")
    # Sin resolve(): el de pipx es un enlace al entorno, y acá interesa el enlace.
    return Path(found).parent if found else Path.home() / ".local" / "bin"


def _integration_paths(bindir: Path) -> dict[str, Path]:
    share = config.xdg("XDG_DATA_HOME", "~/.local/share")
    return {
        "ltc-open": bindir / "ltc-open",
        "desktop": share / "applications" / "linuxtochromecast.desktop",
        "nautilus": share / "nautilus" / "scripts" / "Send to Chromecast",
        "dolphin": share / "kio" / "servicemenus" / "linuxtochromecast.desktop",
    }


def _resource(name: str) -> str:
    return (resources.files("ltc") / "integration" / name).read_text("utf-8")


def _write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")
    path.chmod(mode)


def install_integration(bindir: Path | None = None) -> list[str]:
    """Deja `ltc-open` y las entradas de los gestores. Devuelve qué quedó."""
    bindir = bindir or bin_dir()
    paths = _integration_paths(bindir)
    done = []

    _write(paths["ltc-open"], _resource("ltc-open"), 0o755)
    _write(paths["desktop"], _resource("linuxtochromecast.desktop"), 0o644)
    if shutil.which("update-desktop-database"):
        _run(["update-desktop-database", str(paths["desktop"].parent)])
    done.append("“Open with → Send to Chromecast” in every file manager")

    if paths["nautilus"].parent.parent.is_dir() or shutil.which("nautilus"):
        _write(paths["nautilus"], (
            "#!/usr/bin/env bash\n"
            "# Generated by ltc setup\n"
            "IFS=$'\\n'\n"
            "for f in $NAUTILUS_SCRIPT_SELECTED_FILE_PATHS; do\n"
            f"    [[ -n \"$f\" ]] && exec \"{paths['ltc-open']}\" \"$f\"\n"
            "done\n"), 0o755)
        done.append("Nautilus: right click → Scripts → Send to Chromecast")

    if shutil.which("dolphin") or paths["dolphin"].parent.parent.is_dir():
        _write(paths["dolphin"], _resource("linuxtochromecast-servicemenu.desktop"), 0o755)
        done.append("Dolphin: right click → Send to Chromecast")

    if shutil.which("thunar"):
        warn("Thunar keeps its actions in its own file: add one by hand in "
             "Edit → Configure custom actions, with the command  ltc-open %f")
    return done


def remove_integration(bindir: Path | None = None) -> int:
    removed = 0
    for path in _integration_paths(bindir or bin_dir()).values():
        if path.exists() or path.is_symlink():
            path.unlink()
            removed += 1
    share = config.xdg("XDG_DATA_HOME", "~/.local/share") / "applications"
    if removed and shutil.which("update-desktop-database") and share.is_dir():
        _run(["update-desktop-database", str(share)])
    return removed


# =============================================================================
# ltc setup
# =============================================================================

def _default_library(cfg: config.Config) -> str:
    if cfg.library_folder:
        return cfg.library_folder
    if shutil.which("xdg-user-dir"):
        code, out = _run(["xdg-user-dir", "VIDEOS"])
        if code == 0 and out and Path(out).is_dir() and Path(out) != Path.home():
            return out
    videos = Path.home() / "Videos"
    return str(videos) if videos.is_dir() else ""


def _ask_library(cfg: config.Config, ask: Ask) -> None:
    section("Your library")
    print(dim("  The folder with your movies and series. Subfolders are scanned too."))
    print(dim("  With one set, plain `ltc` opens it; `ltc some/other/folder` still works."))
    default = _default_library(cfg)
    while True:
        answer = ask.text("Library folder (or 'none'):", default)
        if answer.lower() in ("", "none", "no", "-"):
            config.unset_value(cfg, "library.folder")
            print(f"  {dim('no default library; use `ltc FOLDER` each time')}")
            return
        folder = Path(answer).expanduser()
        if folder.is_dir():
            n = count_videos(folder)
            found = f"{n}{'+' if n >= 5000 else ''} videos" if n else "no videos yet"
            ok(f"{folder}  {dim(found)}")
            config.set_value(cfg, "library.folder", absolute(answer))
            return
        if ask.assume_yes:
            warn(f"{folder} doesn't exist; leaving the library unset")
            return
        if ask.yes(f"{folder} doesn't exist. Use it anyway?", default=False):
            config.set_value(cfg, "library.folder", absolute(answer))
            return
        default = ""


def _ask_storage(cfg: config.Config, ask: Ask) -> None:
    section("Where conversions are kept")
    print(dim("  Files the Chromecast can't play are converted once and saved, so the"))
    print(dim("  next time they start instantly. They are big (1-6 GB each)."))
    old = cfg.converted_path
    print(f"  {dim('default')}  {old}  {dim(f'({free_gb(old):.0f} GB free on that disk)')}")
    if not ask.assume_yes and not ask.yes("Keep conversions there?"):
        while True:
            answer = ask.text("Folder for conversions:", str(old))
            new = Path(answer).expanduser()
            try:
                new.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                warn(f"can't use {new}: {exc.strerror or exc}")
                continue
            config.set_value(cfg, "storage.converted_dir", absolute(answer))
            ok(f"{new}  {dim(f'({free_gb(new):.0f} GB free)')}")
            offer_to_move(old, new, ask)
            break

    if ask.assume_yes and "cache_limit_gb" not in cfg.explicit:
        return
    suggested = cfg.cache_limit_gb if "cache_limit_gb" in cfg.explicit \
        else suggest_cache_limit(cfg.converted_path)
    print(dim("  Oldest ones are deleted when the limit is reached. 0 = don't keep any."))
    while True:
        answer = ask.text("Limit in GB:", f"{suggested:g}")
        try:
            config.set_value(cfg, "storage.cache_limit_gb", answer)
            break
        except config.ConfigError as exc:
            warn(str(exc))
    if cfg.cache_limit_gb == config.default_of(config.field_for("storage.cache_limit_gb")):
        config.unset_value(cfg, "storage.cache_limit_gb")


def _ask_playback(cfg: config.Config, ask: Ask) -> None:
    section("Subtitles")
    print(dim("  Language to pick on its own: spa, eng, por, fre, ger, ita, jpn…"))
    answer = ask.text("Preferred subtitle language:", cfg.sub_lang).lower()
    if answer:
        config.set_value(cfg, "playback.sub_lang", answer)
    if cfg.sub_lang == "spa":
        config.unset_value(cfg, "playback.sub_lang")


def _ask_device(cfg: config.Config, ask: Ask) -> None:
    section("Your Chromecast")
    if ask.assume_yes:
        print(f"  {dim('skipped; ltc will look around each time')}")
        return
    from .caster import CastError, discover
    print(dim("  Looking on the network…"))
    try:
        devices = [d for d in discover(timeout=6.0) if d.cast_type != "audio"]
    except CastError as exc:
        warn(str(exc))
        devices = []
    if not devices:
        warn("found none (same Wi-Fi? device on?). ltc will look again each time; "
             "`ltc config set device.name NAME` pins one later.")
        return
    current = next((i for i, d in enumerate(devices, 1) if d.name == cfg.device), 0)
    for i, d in enumerate(devices, 1):
        print(f"    {bold(str(i))}. {d.name}  {dim(d.model)}")
    print(f"    {bold('0')}. look around and ask each time")
    default = str(current or (1 if len(devices) == 1 else 0))
    while True:
        answer = ask.text("Which one is your TV?", default)
        if answer.isdigit() and int(answer) <= len(devices):
            break
        warn("pick one of the numbers above")
    if int(answer) == 0:
        config.unset_value(cfg, "device.name")
    else:
        config.set_value(cfg, "device.name", devices[int(answer) - 1].name)
        ok(devices[int(answer) - 1].name)


def _ask_network(cfg: config.Config, ask: Ask) -> None:
    section("Network")
    print(dim("  The panel (and your phone remote) lives at a fixed port, so its address"))
    print(dim("  can be saved on the phone."))
    while True:
        answer = ask.text("Port:", str(cfg.port))
        try:
            config.set_value(cfg, "network.port", answer)
            break
        except config.ConfigError as exc:
            warn(str(exc))
    if cfg.port == config.default_of(config.field_for("network.port")):
        config.unset_value(cfg, "network.port")
    setup_firewall(cfg.port, ask)


def _ask_privacy(cfg: config.Config, ask: Ask) -> None:
    section("Posters")
    print(dim("  Library posters come from IMDb's public search, which means the titles of"))
    print(dim("  your files are sent to imdb.com. Without it, you get frames."))
    if ask.yes("Look up posters on IMDb?", cfg.library_posters):
        config.unset_value(cfg, "library.posters")
    else:
        config.set_value(cfg, "library.posters", False)


def _ask_opensubtitles(cfg: config.Config, ask: Ask) -> None:
    section("OpenSubtitles (optional)")
    print(dim("  Lets the panel search and download subtitles. Needs an API key and a free"))
    print(dim("  account from opensubtitles.com; the password is stored in the config file"))
    print(dim("  (readable only by you)."))
    configured = bool(cfg.opensubtitles_api_key)
    if not ask.yes("Set it up now?" if not configured else "Change it?", default=False):
        return
    for key, label, secret in (("opensubtitles.api_key", "API key:", False),
                               ("opensubtitles.username", "Username:", False),
                               ("opensubtitles.password", "Password (hidden):", True)):
        value = ask.secret(label) if secret else ask.text(label, getattr(
            cfg, config.field_for(key).name))
        if value:
            config.set_value(cfg, key, value)


def _ask_integration(ask: Ask, skip: bool) -> None:
    section("File manager")
    if skip:
        print(f"  {dim('skipped')}")
        return
    print(dim("  Adds “Send to Chromecast” to the right-click menu of video files."))
    if not ask.yes("Add it?"):
        return
    for line in install_integration():
        ok(line)


def cmd_setup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ltc setup", description="Guided setup: where things live and how ltc behaves.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Don't ask: keep what is configured and use the defaults for the rest")
    parser.add_argument("--no-integration", action="store_true",
                        help="Don't touch the file manager's right-click menu")
    args = parser.parse_args(argv)

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        return fail(f"{exc}\n  Fix it or delete it ({config.config_path()}) and run `ltc setup` again.")

    ask = Ask(args.yes)
    print(f"\n{bold('ltc setup')}  {dim('Enter accepts the value in [brackets]; run it again any time.')}")
    try:
        _ask_library(cfg, ask)
        _ask_storage(cfg, ask)
        _ask_playback(cfg, ask)
        _ask_device(cfg, ask)
        _ask_network(cfg, ask)
        _ask_privacy(cfg, ask)
        _ask_opensubtitles(cfg, ask)
        _ask_integration(ask, args.no_integration)
    except KeyboardInterrupt:
        print(f"\n\n{yellow('Cancelled;')} the configuration was not changed.")
        return 130

    path = config.save(cfg)
    config.set_active(cfg)
    print()
    ok(f"saved {path}")
    print(f"\n  {bold('Ready.')}")
    if cfg.library_path:
        print(f"    {cyan('ltc')}               open your library ({cfg.library_path})")
    print(f"    {cyan('ltc movie.mkv')}     cast one file")
    print(f"    {cyan('ltc config')}        see or change any of this later\n")
    return 0


# =============================================================================
# ltc config
# =============================================================================

def _shown(f, cfg: config.Config) -> str:
    value = getattr(cfg, f.name)
    if f.metadata["secret"] and value:
        return "(set)"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value).lower() if isinstance(value, bool) else str(value)


def _show(cfg: config.Config) -> None:
    path = config.config_path()
    print(f"\n{dim(str(path) + ('' if path.exists() else '  (not created yet)'))}")
    section_name = None
    for f in config.options():
        key = f.metadata["key"]
        sec = key.split(".")[0]
        if sec != section_name:
            print(f"\n{bold(sec)}")
            section_name = sec
        mark = green("●") if f.name in cfg.explicit else dim("○")
        print(f"  {mark} {key:<28} {_shown(f, cfg)}")
    print(f"\n{dim('● set in the file   ○ default')}")
    print(dim("`ltc config set KEY VALUE` changes one, `ltc config unset KEY` goes back to the "
              "default,\n`ltc config get KEY` prints just the value, `ltc config path` the file."))
    print(dim(f"Conversions go to {cfg.converted_path}\n"))


def cmd_config(argv: list[str]) -> int:
    action = argv[0] if argv else "show"
    rest = argv[1:]
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        return fail(f"{exc}\n  Edit it with: ltc config edit")

    try:
        if action == "show" and not rest:
            _show(cfg)
        elif action == "path" and not rest:
            print(config.config_path())
        elif action == "get" and len(rest) == 1:
            value = getattr(cfg, config.field_for(rest[0]).name)
            print(str(value).lower() if isinstance(value, bool) else value)
        elif action == "set" and len(rest) >= 2:
            return _config_set(cfg, rest[0], " ".join(rest[1:]))
        elif action == "unset" and len(rest) == 1:
            config.unset_value(cfg, rest[0])
            config.save(cfg)
            print(f"{rest[0]} back to the default")
        elif action == "edit" and not rest:
            return _config_edit(cfg)
        else:
            print("Usage: ltc config [show | path | edit | get KEY | set KEY VALUE | unset KEY]",
                  file=sys.stderr)
            return 2
    except config.ConfigError as exc:
        return fail(str(exc))
    return 0


def _config_set(cfg: config.Config, key: str, raw: str) -> int:
    f = config.field_for(key)
    old_converted = cfg.converted_path
    if key in PATH_KEYS:
        raw = absolute(raw)
        if key == "library.folder" and not Path(raw).is_dir():
            warn(f"{raw} doesn't exist (yet)")
    config.set_value(cfg, key, raw)
    config.save(cfg)
    secret = f.metadata["secret"]
    print(f"{key} = {'(set)' if secret else getattr(cfg, f.name)}")
    if key == "storage.converted_dir" or key == "storage.cache_dir":
        offer_to_move(old_converted, cfg.converted_path, Ask(False))
    return 0


def _config_edit(cfg: config.Config) -> int:
    path = config.config_path()
    if not path.exists():
        config.save(cfg)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return subprocess.call([*editor.split(), str(path)])
    if shutil.which("xdg-open"):
        return subprocess.call(["xdg-open", str(path)])
    return fail(f"Set $EDITOR, or open {path} yourself.")


# =============================================================================
# ltc uninstall
# =============================================================================

def _remove_known(directory: Path, names: list[str], patterns: list[str]) -> int:
    """Borra sólo lo que ltc crea dentro de `directory`, y la carpeta si queda vacía."""
    removed = 0
    targets = [directory / n for n in names]
    for pattern in patterns:
        targets += directory.glob(pattern)
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
            removed += 1
        elif t.exists():
            t.unlink(missing_ok=True)
            removed += 1
    try:
        directory.rmdir()
    except OSError:
        pass
    return removed


def cmd_uninstall(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ltc uninstall", description="Remove ltc and what `ltc setup` added.")
    parser.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation")
    parser.add_argument("--purge", action="store_true",
                        help="Also delete the configuration, saved conversions, caches and watch progress")
    parser.add_argument("--keep-data", action="store_true",
                        help="Keep the configuration and saved data without asking")
    args = parser.parse_args(argv)

    try:
        cfg = config.load()
    except config.ConfigError:
        cfg = config.Config()
    ask = Ask(args.yes)

    print(f"\n{bold('ltc uninstall')}")
    if not ask.yes("Remove ltc from this computer?", default=False) and not args.yes:
        print(dim("  Nothing changed."))
        return 0

    removed = remove_integration()
    ok(f"file manager entries removed ({removed} files)")

    converted, cache = cfg.converted_path, cfg.cache_path
    progress = config.data_dir()
    size = sum(dir_size(d) for d in {converted, cache, progress} if d.exists())
    if args.purge:
        purge = True
    elif args.keep_data:
        purge = False
    else:
        purge = ask.yes(f"Also delete the configuration, saved conversions and caches "
                        f"({fmt_size(size)})?", default=False)
    if purge:
        _remove_known(converted, [".tmp"], ["*.mp4", "*.json"])
        _remove_known(cache, ["thumbs", "posters", "converted", "ltc.log",
                              "opensubtitles-downloads.json"], ["ltc.log.*"])
        _remove_known(progress, ["progress.json"], [])
        cfg_dir = config.config_path().parent
        _remove_known(cfg_dir, ["config.toml", "opensubtitles.json"], [])
        ok("configuration and data deleted")
    else:
        print(f"  {dim('kept: ' + str(config.config_path()) + ', ' + str(converted))}")

    return _remove_package()


def _remove_package() -> int:
    """Último paso: quitar el paquete. Por pipx si así se instaló."""
    if "pipx" in Path(sys.prefix).parts and shutil.which("pipx"):
        print(f"\n  {dim('pipx uninstall ' + PACKAGE)}")
        sys.stdout.flush()
        # Este proceso sigue en memoria aunque su entorno desaparezca; no se
        # importa nada más después de esta línea.
        return subprocess.call(["pipx", "uninstall", PACKAGE])
    print(f"\n  Last step, remove the package itself:  {bold('pipx uninstall ' + PACKAGE)}"
          f"\n  {dim('(or: pip uninstall ' + PACKAGE + ')')}")
    return 0
