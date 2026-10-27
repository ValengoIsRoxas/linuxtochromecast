"""Prueba la configuración y el instalador sin tocar el sistema.

Todo corre contra un HOME de mentira: las variables XDG y LTC_BIN_DIR apuntan a
una carpeta temporal, así que ni el archivo de configuración, ni el menú del
gestor de archivos, ni `ltc-open` del usuario se tocan.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  \033[2m{detail}\033[0m" if detail else ""))


def sandbox(tmp: Path) -> None:
    """Apunta todo lo que ltc mira del sistema a `tmp`."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    os.environ.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "LTC_BIN_DIR": str(home / ".local" / "bin"),
    })
    os.environ.pop("LTC_CONFIG", None)
    for var in ("LTC_OPENSUBTITLES_API_KEY", "LTC_OPENSUBTITLES_USERNAME",
                "LTC_OPENSUBTITLES_PASSWORD"):
        os.environ.pop(var, None)


def run(fn, *args, stdin: str | None = None):
    """Corre `fn` capturando la salida. Devuelve (resultado, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old_in = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
        sys.stdin.isatty = lambda: True        # type: ignore[method-assign]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = fn(*args)
            except SystemExit as exc:
                result = exc.code
    finally:
        sys.stdin = old_in
    return result, out.getvalue(), err.getvalue()


def test_file(tmp: Path) -> None:
    from ltc import config
    print("\n\033[1mArchivo de configuración\033[0m")
    config.set_active(None)

    cfg = config.get()
    check("sin archivo todo es por defecto", cfg.port == 8642 and cfg.sub_lang == "spa"
          and cfg.cache_limit_gb == 40.0 and cfg.hwaccel and not cfg.explicit)
    check("la carpeta de conversiones cuelga de la caché",
          cfg.converted_path == Path(os.environ["XDG_CACHE_HOME"]) / "ltc" / "converted")
    check("y los intermedios van junto a ellas", cfg.spool_path == cfg.converted_path / ".tmp")
    check("sin biblioteca configurada no hay carpeta", cfg.library_path is None)

    config.set_value(cfg, "library.folder", str(tmp / "pelis"))
    config.set_value(cfg, "storage.cache_limit_gb", "12.5")
    config.set_value(cfg, "playback.autoplay_next", "no")
    config.set_value(cfg, "opensubtitles.password", 'pa"ss\\word')
    path = config.save(cfg)
    check("guarda con modo 600 (puede llevar una contraseña)",
          (path.stat().st_mode & 0o777) == 0o600, oct(path.stat().st_mode))

    again = config.load()
    check("lo guardado se lee igual", again.library_folder == str(tmp / "pelis")
          and again.cache_limit_gb == 12.5 and again.autoplay_next is False
          and again.opensubtitles_password == 'pa"ss\\word')
    check("recuerda qué venía escrito y qué no",
          again.explicit == {"library_folder", "cache_limit_gb", "autoplay_next",
                             "opensubtitles_password"}, str(sorted(again.explicit)))
    text = path.read_text()
    check("el resto queda comentado con su valor por defecto",
          "# port = 8642" in text and "# hwaccel = true" in text)

    config.unset_value(again, "storage.cache_limit_gb")
    config.save(again)
    check("unset vuelve al valor por defecto",
          config.load().cache_limit_gb == 40.0 and "cache_limit_gb" not in config.load().explicit)

    def bad(text: str) -> str:
        path.write_text(text)
        try:
            config.load()
        except config.ConfigError as exc:
            return str(exc)
        return ""
    check("un TOML roto dice cuál es el archivo", str(path) in bad("[library\nfolder = 1"))
    check("un tipo equivocado dice cuál es la clave", "network.port" in bad('[network]\nport = "x"'))
    check("un puerto fuera de rango se rechaza", "65535" in bad("[network]\nport = 70000"))
    check("un booleano que no lo es se rechaza", "playback.surround" in bad('[playback]\nsurround = "tal vez"'))
    path.write_text("[network]\nport = 9000\n[nada]\nraro = 1\n")
    check("una clave desconocida se ignora, no rompe", config.load().port == 9000)

    try:
        config.set_value(config.Config(), "no.existe", "1")
        check("una clave que no existe se avisa", False)
    except config.ConfigError as exc:
        check("una clave que no existe se avisa", "no.existe" in str(exc))

    path.unlink()
    config.set_active(None)


def test_wiring(tmp: Path) -> None:
    from ltc import config
    from ltc.cache import ConversionCache
    from ltc.cli import build_parser
    from ltc.library import Thumbnailer
    from ltc.opensubtitles import Credentials, OpenSubtitles
    from ltc.posters import Posters
    print("\n\033[1mLa configuración llega a cada pieza\033[0m")

    cfg = config.Config()
    config.set_value(cfg, "storage.cache_dir", str(tmp / "cache"))
    config.set_value(cfg, "storage.cache_limit_gb", "7")
    config.set_value(cfg, "playback.sub_lang", "eng")
    config.set_value(cfg, "device.name", "TV Living")
    config.set_value(cfg, "performance.hwaccel", "false")
    config.set_value(cfg, "library.posters", "false")
    config.set_value(cfg, "playback.surround", "true")
    config.set_active(cfg)

    cache = ConversionCache()
    check("las conversiones van a la carpeta configurada",
          cache.directory == tmp / "cache" / "converted", str(cache.directory))
    check("con el tope configurado", cache.limit == 7 * 1024 ** 3)
    check("y se puede pisar explícitamente", ConversionCache(1.0, tmp / "x").directory == tmp / "x"
          and ConversionCache(0).enabled is False)
    check("miniaturas y pósters también cuelgan de ahí",
          Thumbnailer().cache_dir == tmp / "cache" / "thumbs"
          and Posters().dir == tmp / "cache" / "posters")

    config.set_value(cfg, "storage.converted_dir", str(tmp / "disco" / "conv"))
    check("la carpeta de conversiones se puede mover aparte de la caché",
          ConversionCache().directory == tmp / "disco" / "conv"
          and Thumbnailer().cache_dir == tmp / "cache" / "thumbs")
    from ltc.transcode import spool_dir
    check("y los intermedios la siguen (mismo disco, rename y no copia)",
          spool_dir() == tmp / "disco" / "conv" / ".tmp" and spool_dir().is_dir())

    args = build_parser().parse_args([])
    check("los defaults del parser salen de la configuración",
          args.sub_lang == "eng" and args.cache_size == 7 and args.device == "TV Living"
          and args.hwaccel is False and args.posters is False and args.surround is True
          and args.autoplay is True, str(vars(args)))
    args = build_parser().parse_args(["--sub-lang", "por", "--cache-size", "0", "-d", "Otra",
                                      "--hwaccel", "--posters", "--no-surround", "--no-autoplay"])
    check("y la línea de comandos los pisa, en los dos sentidos",
          args.sub_lang == "por" and args.cache_size == 0 and args.device == "Otra"
          and args.hwaccel and args.posters and not args.surround and not args.autoplay)

    api = OpenSubtitles(Credentials(api_key="k"))
    check("el registro de subtítulos bajados también va a la caché",
          api.index_path == tmp / "cache" / "opensubtitles-downloads.json")

    config.set_value(cfg, "opensubtitles.api_key", "de-config")
    config.set_value(cfg, "opensubtitles.username", "user-config")
    creds = Credentials.load(tmp / "no-existe.json")
    check("las credenciales salen de config.toml", creds.api_key == "de-config"
          and creds.username == "user-config")
    os.environ["LTC_OPENSUBTITLES_API_KEY"] = "del-entorno"
    check("pero la variable de entorno manda", Credentials.load(tmp / "no-existe.json").api_key == "del-entorno")
    os.environ.pop("LTC_OPENSUBTITLES_API_KEY")
    legacy = tmp / "viejo.json"
    legacy.write_text('{"api_key": "viejo", "password": "pw-viejo"}')
    creds = Credentials.load(legacy)
    check("y el opensubtitles.json de antes sigue sirviendo para lo que config.toml no dice",
          creds.api_key == "de-config" and creds.password == "pw-viejo")
    config.set_active(None)


def test_main(tmp: Path) -> None:
    from ltc import cli, config
    print("\n\033[1m`ltc` sin argumentos\033[0m")

    opened = []
    real = cli.run_library
    cli.run_library = lambda args: opened.append(args.video) or 0
    try:
        cfg = config.Config()
        config.set_active(cfg)
        code, out, _ = run(cli.main, [])
        check("sin biblioteca configurada muestra la ayuda y sugiere ltc setup",
              code == 1 and "usage: ltc" in out and "ltc setup" in out and not opened)

        folder = tmp / "pelis"
        folder.mkdir()
        config.set_value(cfg, "library.folder", str(folder))
        code, _, _ = run(cli.main, [])
        check("con biblioteca configurada la abre", code == 0 and opened == [str(folder)], str(opened))

        config.set_value(cfg, "library.folder", str(tmp / "borrada"))
        code, _, err = run(cli.main, [])
        check("si la carpeta ya no existe lo dice y cómo arreglarlo",
              code == 1 and "doesn't exist" in err and "ltc config set library.folder" in err, err)

        other = tmp / "otra"
        other.mkdir()
        opened.clear()
        run(cli.main, [str(other)])
        check("una carpeta explícita gana sobre la configurada", opened == [str(other)])

        (Path(os.environ["XDG_CONFIG_HOME"]) / "ltc").mkdir(parents=True, exist_ok=True)
        (Path(os.environ["XDG_CONFIG_HOME"]) / "ltc" / "config.toml").write_text("[network\n")
        config.set_active(None)
        code, _, err = run(cli.main, [])
        check("un config.toml roto no es un traceback", code == 1 and "not valid TOML" in err, err)
    finally:
        cli.run_library = real
        (Path(os.environ["XDG_CONFIG_HOME"]) / "ltc" / "config.toml").unlink(missing_ok=True)
        config.set_active(None)


def test_config_command(tmp: Path) -> None:
    from ltc import config, installer
    print("\n\033[1m`ltc config`\033[0m")
    config.set_active(None)
    cmd = installer.cmd_config

    code, out, _ = run(cmd, [])
    check("lista todas las opciones", code == 0 and "network.port" in out and "library.folder" in out)

    code, out, _ = run(cmd, ["set", "network.port", "9100"])
    check("set cambia y lo cuenta", code == 0 and "network.port = 9100" in out)
    check("y queda guardado", config.load().port == 9100)
    code, out, _ = run(cmd, ["get", "network.port"])
    check("get imprime sólo el valor", out.strip() == "9100")
    code, out, _ = run(cmd, ["get", "performance.hwaccel"])
    check("los booleanos salen en minúscula", out.strip() == "true")

    code, _, err = run(cmd, ["set", "network.port", "mucho"])
    check("un valor inválido no se guarda", code == 1 and "network.port" in err and config.load().port == 9100)
    code, _, err = run(cmd, ["set", "no.existe", "1"])
    check("una clave inexistente falla con un mensaje", code == 1 and "no.existe" in err)

    rel = tmp / "rel"
    rel.mkdir()
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        run(cmd, ["set", "library.folder", "rel"])
    finally:
        os.chdir(cwd)
    check("las rutas se guardan absolutas", config.load().library_folder == str(rel.resolve()))

    run(cmd, ["set", "opensubtitles.password", "secreto"])
    code, out, _ = run(cmd, [])
    check("el listado no muestra contraseñas", "secreto" not in out and "(set)" in out)

    run(cmd, ["unset", "network.port"])
    check("unset borra la clave del archivo",
          config.load().port == 8642 and "port =" not in
          "\n".join(l for l in config.config_path().read_text().splitlines() if not l.startswith("#")))
    code, out, _ = run(cmd, ["path"])
    check("path imprime dónde está", out.strip() == str(config.config_path()))
    code, _, err = run(cmd, ["get"])
    check("un uso incorrecto da el uso y código 2", code == 2 and "Usage" in err)

    # Mover la carpeta de conversiones lleva lo ya guardado (si se acepta).
    old = config.get().converted_path
    old.mkdir(parents=True, exist_ok=True)
    (old / "abc.mp4").write_bytes(b"x" * 10)
    (old / "abc.json").write_text("{}")
    new = tmp / "otro-disco"
    config.set_active(None)
    run(cmd, ["set", "storage.converted_dir", str(new)], stdin="y\n")
    check("cambiar la carpeta de conversiones ofrece llevarse lo guardado",
          (new / "abc.mp4").is_file() and (new / "abc.json").is_file()
          and not (old / "abc.mp4").exists())
    config.config_path().unlink()
    config.set_active(None)


def test_integration(tmp: Path) -> None:
    from ltc import installer
    print("\n\033[1mIntegración con el gestor de archivos\033[0m")
    bindir = Path(os.environ["LTC_BIN_DIR"])
    share = Path(os.environ["XDG_DATA_HOME"])

    lines = installer.install_integration()
    opener = bindir / "ltc-open"
    check("deja ltc-open ejecutable en el directorio de binarios",
          opener.is_file() and os.access(opener, os.X_OK))
    check("y la entrada «Abrir con» para todos los gestores",
          "Exec=ltc-open %f" in (share / "applications" / "linuxtochromecast.desktop").read_text())
    check("el paquete trae los tres archivos de integración",
          all(p.is_file() for p in [
              Path(installer.__file__).parent / "integration" / n
              for n in ("ltc-open", "linuxtochromecast.desktop", "linuxtochromecast-servicemenu.desktop")]))
    check("cuenta qué instaló", any("Send to Chromecast" in l for l in lines), str(lines))

    (share / "nautilus" / "scripts").mkdir(parents=True)
    installer.install_integration()
    script = share / "nautilus" / "scripts" / "Send to Chromecast"
    check("con Nautilus presente deja su script, apuntando al ltc-open instalado",
          script.is_file() and str(opener) in script.read_text() and os.access(script, os.X_OK))

    removed = installer.remove_integration()
    check("quitarla borra todo lo que puso", removed >= 3 and not opener.exists()
          and not script.exists()
          and not (share / "applications" / "linuxtochromecast.desktop").exists())
    check("y repetirlo no falla", installer.remove_integration() == 0)


def test_firewall() -> None:
    from ltc import installer
    print("\n\033[1mFirewall\033[0m")
    real_run, real_which = installer._run, installer.shutil.which

    def fake(table):
        def _run(cmd, timeout=8.0):
            return table.get(tuple(cmd), (127, ""))
        return _run

    try:
        installer.shutil.which = lambda name: f"/usr/bin/{name}" if name in ("firewall-cmd", "ufw") else None
        installer._run = fake({("firewall-cmd", "--state"): (0, "running"),
                               ("firewall-cmd", "--query-port=8642/tcp"): (1, "no")})
        fw = installer.check_firewall(8642)
        check("firewalld activo con el puerto cerrado", fw and fw.tool == "firewalld" and fw.is_open is False)
        check("propone abrirlo de forma permanente y recargar",
              fw.commands == [["firewall-cmd", "--permanent", "--add-port=8642/tcp"], ["firewall-cmd", "--reload"]])

        installer._run = fake({("firewall-cmd", "--state"): (0, "running"),
                               ("firewall-cmd", "--query-port=8642/tcp"): (0, "yes")})
        check("con el puerto ya abierto no hace nada", installer.check_firewall(8642).is_open is True)

        installer._run = fake({("firewall-cmd", "--state"): (252, "not running"),
                               ("systemctl", "is-active", "ufw"): (0, "active")})
        fw = installer.check_firewall(8642)
        check("si firewalld está apagado, mira ufw (que sin root no dice su estado)",
              fw and fw.tool == "ufw" and fw.is_open is None and fw.commands == [["ufw", "allow", "8642/tcp"]])

        installer._run = fake({("firewall-cmd", "--state"): (252, ""), ("systemctl", "is-active", "ufw"): (3, "inactive")})
        check("sin firewall activo no propone nada", installer.check_firewall(8642) is None)

        installer.shutil.which = lambda name: None
        check("ni instalado ninguno", installer.check_firewall(8642) is None)

        # Con --yes se informa el comando pero NUNCA se corre sudo.
        ran = []
        real_sub = installer.subprocess.run
        installer.subprocess.run = lambda *a, **k: ran.append(a) or None
        installer.shutil.which = lambda name: f"/usr/bin/{name}" if name == "firewall-cmd" else None
        installer._run = fake({("firewall-cmd", "--state"): (0, "running"),
                               ("firewall-cmd", "--query-port=8642/tcp"): (1, "no")})
        _, out, _ = run(installer.setup_firewall, 8642, installer.Ask(True))
        installer.subprocess.run = real_sub
        check("con --yes muestra el comando pero no ejecuta sudo",
              not ran and "sudo firewall-cmd --permanent --add-port=8642/tcp" in out, out)
    finally:
        installer._run, installer.shutil.which = real_run, real_which


def test_wizard(tmp: Path) -> None:
    from ltc import config, installer
    print("\n\033[1m`ltc setup`\033[0m")
    config.set_active(None)
    pelis, disco = tmp / "mis-pelis", tmp / "disco-grande"
    pelis.mkdir()
    (pelis / "Serie" / "Season 1").mkdir(parents=True)
    (pelis / "Serie" / "Season 1" / "Serie.S01E01.mkv").write_bytes(b"x")
    (pelis / "Peli.2020.mp4").write_bytes(b"x")

    real_fw, real_dev = installer.check_firewall, installer.cmd_setup.__globals__["_ask_device"]
    installer.check_firewall = lambda port: None
    installer.cmd_setup.__globals__["_ask_device"] = lambda cfg, ask: None
    try:
        # library, conversiones (no -> carpeta), límite, idioma, puerto,
        # posters (no), opensubtitles (no), gestor de archivos (no)
        answers = "\n".join([str(pelis), "n", str(disco), "15", "eng", "9000", "n", "n", "n"]) + "\n"
        code, out, _ = run(installer.cmd_setup, [], stdin=answers)
        cfg = config.load()
        check("termina bien", code == 0, out[-300:])
        check("la biblioteca quedó, contando también las subcarpetas",
              cfg.library_folder == str(pelis) and "2 videos" in out, out)
        check("la carpeta de conversiones elegida se creó y quedó",
              cfg.converted_dir == str(disco) and disco.is_dir())
        check("con su tope, idioma y puerto", cfg.cache_limit_gb == 15 and cfg.sub_lang == "eng"
              and cfg.port == 9000)
        check("y rechazó los pósters", cfg.library_posters is False)
        check("sin integración si se dijo que no", not (Path(os.environ["LTC_BIN_DIR"]) / "ltc-open").exists())
        check("lo no tocado queda sin escribir (sigue el valor por defecto)",
              "storage.cache_dir" not in "\n".join(l for l in config.config_path().read_text().splitlines()
                                                   if not l.startswith("#")))
        check("y deja activa la configuración nueva", config.get().library_folder == str(pelis))

        # Repetirlo: Enter en todo conserva lo anterior.
        code, _, _ = run(installer.cmd_setup, [], stdin="\n" * 12)
        again = config.load()
        check("repetir el asistente con Enter conserva lo elegido",
              again.library_folder == str(pelis) and again.converted_dir == str(disco)
              and again.cache_limit_gb == 15 and again.sub_lang == "eng" and again.port == 9000
              and again.library_posters is False, str(again))

        # Una carpeta que no existe no se acepta sin confirmar.
        config.config_path().unlink()
        config.set_active(None)
        answers = "\n".join([str(tmp / "no-existe"), "n", "", "n", "n", "n", "n", "n", "n", "n", "n", "n"]) + "\n"
        run(installer.cmd_setup, [], stdin=answers)
        check("una carpeta inexistente no se guarda si se dice que no",
              config.load().library_folder == "")

        # --yes: no pregunta, no escribe lo que no se tocó.
        config.config_path().unlink(missing_ok=True)
        config.set_active(None)
        code, out, _ = run(installer.cmd_setup, ["--yes", "--no-integration"])
        check("--yes no pregunta y deja la configuración con valores por defecto",
              code == 0 and not config.load().explicit, str(config.load().explicit))
    finally:
        installer.check_firewall = real_fw
        installer.cmd_setup.__globals__["_ask_device"] = real_dev
        config.set_active(None)


def test_uninstall(tmp: Path) -> None:
    from ltc import config, installer
    print("\n\033[1m`ltc uninstall`\033[0m")
    config.set_active(None)
    cfg = config.Config()
    conv = tmp / "mis-conversiones"
    config.set_value(cfg, "storage.converted_dir", str(conv))
    config.save(cfg)
    config.set_active(None)

    conv.mkdir()
    (conv / "a.mp4").write_bytes(b"x")
    (conv / "a.json").write_text("{}")
    (conv / ".tmp").mkdir()
    (conv / ".tmp" / "spool-1.mp4").write_bytes(b"x")
    (conv / "mis-notas.txt").write_text("no es de ltc")
    cache = cfg.cache_path
    (cache / "thumbs").mkdir(parents=True)
    (cache / "thumbs" / "t.jpg").write_bytes(b"x")
    (cache / "ltc.log").write_text("log")
    (cache / "otra-cosa.dat").write_text("ajeno")
    installer.install_integration()

    ran = []
    real_pkg = installer._remove_package
    installer._remove_package = lambda: ran.append(1) or 0
    try:
        code, out, _ = run(installer.cmd_uninstall, ["--yes", "--keep-data"])
        check("--keep-data quita la integración y deja los datos",
              code == 0 and not (Path(os.environ["LTC_BIN_DIR"]) / "ltc-open").exists()
              and (conv / "a.mp4").exists() and config.config_path().exists() and ran)

        code, out, _ = run(installer.cmd_uninstall, ["--yes", "--purge"])
        check("--purge borra conversiones, intermedios, cachés y configuración",
              not (conv / "a.mp4").exists() and not (conv / ".tmp").exists()
              and not (cache / "thumbs").exists() and not (cache / "ltc.log").exists()
              and not config.config_path().exists())
        check("pero no archivos ajenos que estén en esas carpetas",
              (conv / "mis-notas.txt").read_text() == "no es de ltc"
              and (cache / "otra-cosa.dat").exists())

        # Sin --yes y sin terminal: la respuesta segura es no tocar nada.
        installer.install_integration()
        code, out, _ = run(installer.cmd_uninstall, [])
        check("sin confirmación no borra nada",
              (Path(os.environ["LTC_BIN_DIR"]) / "ltc-open").exists() and "Nothing changed" in out)
    finally:
        installer._remove_package = real_pkg
        installer.remove_integration()
        config.set_active(None)


if __name__ == "__main__":
    print("\033[1mProbando configuración e instalador\033[0m")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sandbox(tmp)
        test_file(tmp)
        test_wiring(tmp)
        test_main(tmp)
        test_config_command(tmp)
        test_integration(tmp)
        test_firewall()
        test_wizard(tmp)
        test_uninstall(tmp)

    print(f"\n  \033[32m{len(PASS)} pasaron\033[0m", end="")
    if FAIL:
        print(f", \033[31m{len(FAIL)} fallaron\033[0m:")
        for f in FAIL:
            print(f"    - {f}")
        sys.exit(1)
    print("\n")
