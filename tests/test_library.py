"""Prueba el modo biblioteca sin hardware: escaneo, progreso y el servidor.

Arma una carpeta de mentira con copias de los videos de `make_fixtures.sh`
bajo nombres de release reales, y la recorre con un Chromecast simulado que
además reporta su estado (el `Player` lo necesita para saber cuándo terminó
un capítulo).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ltc.cache import ConversionCache, plan_key  # noqa: E402
from ltc.caster import Device  # noqa: E402
from ltc.compat import DeviceCaps, plan_for  # noqa: E402
from ltc.library import (  # noqa: E402
    Library, ProgressStore, Thumbnailer, clean_name, library_json,
    parse_episode, parse_movie, season_of_dir,
)
from ltc.libraryserver import serve_library  # noqa: E402
from ltc.player import PlayOptions, Player  # noqa: E402
from ltc import opensubtitles as osub  # noqa: E402
from ltc.probe import probe  # noqa: E402

MEDIA = Path(sys.argv[1] if len(sys.argv) > 1 else "media")

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  \033[2m{detail}\033[0m" if detail else ""))


class FakeStatus:
    def __init__(self):
        self.content_id = None
        self.player_state = "IDLE"
        self.idle_reason = None


class FakeCaster:
    """Chromecast simulado que también expone `status`, como el de verdad."""

    def __init__(self):
        self.device = Device("TV Falsa", "Chromecast Ultra", "127.0.0.1", 8009, "uuid", "cast")
        self.loads: list[dict] = []
        self.status = FakeStatus()
        self.connected = True
        self.failure = ""
        self.volume = 0.5
        self.muted = False
        self._pos = 0.0

    @property
    def player_state(self):
        return self.status.player_state

    def load(self, url, content_type, **kw):
        self.loads.append({"url": url, "content_type": content_type, **kw})
        self.failure = ""
        self.status = FakeStatus()
        self.status.content_id = url
        self.status.player_state = "PLAYING"
        self._pos = kw.get("current_time") or 0.0

    def finish(self):
        self.status.player_state = "IDLE"
        self.status.idle_reason = "FINISHED"

    def position(self): return self._pos
    def play(self): self.status.player_state = "PLAYING"
    def pause(self): self.status.player_state = "PAUSED"
    def stop(self):
        self.status.player_state = "IDLE"
        self.status.idle_reason = "CANCELLED"
    def seek(self, p): self._pos = p
    def set_volume(self, v): self.volume = v
    def set_muted(self, m): self.muted = m
    def enable_subtitle(self, i): pass
    def disable_subtitle(self, i): pass


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def post(url, payload=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def build_tree(root: Path) -> None:
    """Una biblioteca con las mañas de las que se bajan de verdad."""
    movies = root / "Películas"
    movies.mkdir()
    shutil.copy(MEDIA / "compatible.mp4", movies / "Blade.Runner.2049.2017.1080p.BluRay.x264.mp4")
    shutil.copy(MEDIA / "compatible.es.srt", movies / "Blade.Runner.2049.2017.1080p.BluRay.x264.es.srt")
    shutil.copy(MEDIA / "compatible.mp4", movies / "sample.mp4")
    # h264 4:4:4 + ac3: hay que reencodar, así que pasa por la caché.
    shutil.copy(MEDIA / "pelicula.mkv", movies / "Nueve.Reinas.2000.mkv")

    dark = root / "Series" / "Dark" / "Season 1"
    dark.mkdir(parents=True)
    shutil.copy(MEDIA / "remuxeable.mkv", dark / "Dark.S01E02.Lies.720p.mkv")
    shutil.copy(MEDIA / "compatible.mp4", dark / "Dark.S01E01.Secrets.720p.mp4")
    season2 = root / "Series" / "Dark" / "Temporada 2"
    season2.mkdir()
    shutil.copy(MEDIA / "compatible.mp4", season2 / "01 - Beginnings and Endings.mp4")

    (root / "Series" / "The Office").mkdir()
    shutil.copy(MEDIA / "compatible.mp4", root / "Series" / "The Office" / "the.office.2x01.mp4")

    # Un pack de Blu-ray: la temporada en el nombre de la carpeta, los archivos
    # con la serie abreviada, y una carpeta de extras que no son capítulos.
    tng = root / "Star Trek The Next Generation Season 2 (1080p x265 10bit Joy)"
    (tng / "Extras").mkdir(parents=True)
    shutil.copy(MEDIA / "compatible.mp4", tng / "Star Trek TNG S02E01 The Child.mp4")
    shutil.copy(MEDIA / "compatible.mp4", tng / "Star Trek TNG S02E02 Where Silence Has Lease.mp4")
    shutil.copy(MEDIA / "compatible.mp4", tng / "Extras" / "Star Trek TNG Season 2 Extra 1 - Mission Overview.mp4")
    shutil.copy(MEDIA / "compatible.mp4", movies / "Blade.Runner.2049-trailer.mp4")

    hidden = root / ".trash"
    hidden.mkdir()
    shutil.copy(MEDIA / "compatible.mp4", hidden / "Borrada.2010.mp4")
    (root / "notas.txt").write_text("no es un video")


# --- nombres --------------------------------------------------------------------

def test_names():
    print("\n\033[1mNombres de archivo\033[0m")
    ep = parse_episode("Dark.S01E03.Past.and.Present.1080p.WEB-DL")
    check("SxxEyy", ep is not None and (ep.show, ep.season, ep.episode) == ("Dark", 1, 3),
          str(ep))
    check("título del capítulo sin la calidad", ep is not None and ep.title == "Past and Present",
          ep.title if ep else "—")
    ep = parse_episode("The.Office.US.3x07.HDTV")
    check("NxNN", ep is not None and (ep.show, ep.season, ep.episode) == ("The Office US", 3, 7))
    check("una resolución no es un capítulo", parse_episode("Video 1920x1080") is None)
    check("'Episode 4' suelto es una película", parse_episode("Star Wars Episode 4") is None)
    ep = parse_episode("05 - Breakage", season_hint=2)
    check("número al principio dentro de una temporada",
          ep is not None and (ep.season, ep.episode, ep.title) == (2, 5, "Breakage"))
    check("doble capítulo toma el primero",
          (parse_episode("Show.S02E05E06.Name") or None).episode == 5)

    check("año y calidad fuera del título",
          parse_movie("Blade.Runner.2049.2017.1080p.BluRay") == ("Blade Runner 2049", 2017))
    check("un título que es un año", parse_movie("1917.2019.1080p") == ("1917", 2019))
    check("el año entre paréntesis",
          parse_movie("El Secreto de sus Ojos (2009) [1080p]") == ("El Secreto de sus Ojos", 2009))
    check("con espacios el punto es del título", clean_name("Mr. Nobody") == "Mr. Nobody")
    check("el grupo entre corchetes se va",
          clean_name("[Grupo] Shingeki no Kyojin 720p") == "Shingeki no Kyojin")

    check("carpetas de temporada",
          (season_of_dir("Season 1"), season_of_dir("Temporada 02"), season_of_dir("S3"))
          == (1, 2, 3))
    check("una serie que empieza con S no es temporada", season_of_dir("Supernatural") is None)


# --- escaneo y progreso -----------------------------------------------------------

def test_scan(root: Path, data: Path):
    print("\n\033[1mEscaneo y progreso\033[0m")
    lib = Library.scan(root)

    check("dos películas (sin sample, extras, trailers ni ocultas)",
          [m.label for m in lib.movies] == ["Blade Runner 2049 (2017)", "Nueve Reinas (2000)"],
          str([m.label for m in lib.movies]))
    check("tres series, con el nombre de la carpeta y no el abreviado",
          [s.name for s in lib.shows] == ["Dark", "Star Trek The Next Generation", "The Office"],
          str([s.name for s in lib.shows]))
    check("los extras no son capítulos", len(lib.shows[1].episodes) == 2,
          str([e.code for e in lib.shows[1].episodes]))

    dark = lib.shows[0]
    check("los capítulos en orden", [e.code for e in dark.episodes]
          == ["S01E01", "S01E02", "S02E01"], str([e.code for e in dark.episodes]))
    check("el de la carpeta Temporada 2 se suma a la serie por la carpeta",
          dark.episodes[-1].title == "Beginnings and Endings", dark.episodes[-1].title)
    check("temporadas", dark.seasons == [1, 2])
    check("siguiente capítulo cruza de temporada",
          lib.next_episode(dark.episodes[1]).code == "S02E01")
    check("el último no tiene siguiente", lib.next_episode(dark.episodes[-1]) is None)
    check("los ids no cambian entre escaneos",
          Library.scan(root).shows[0].episodes[0].id == dark.episodes[0].id)

    store = ProgressStore(data / "progress.json", save_every=0)
    movie = lib.movies[0]
    store.update(movie.path, 20.0, 7200.0)
    check("menos de 30 s no se retoma", store.resume_point(movie.path) == 0.0)
    store.update(movie.path, 2400.0, 7200.0)
    check("se retoma donde quedó", store.resume_point(movie.path) == 2400.0)
    store.update(movie.path, 7100.0, 7200.0)
    check("los créditos cuentan como vista", store.get(movie.path)["watched"])
    check("lo visto empieza de cero", store.resume_point(movie.path) == 0.0)
    store.update(movie.path, 2400.0, 7200.0)
    check("se guarda en disco",
          ProgressStore(data / "progress.json").resume_point(movie.path) == 2400.0)

    store.mark_watched(dark.episodes[0].path, 3000.0)
    cont = library_json(lib, store)["continue"]
    check("seguir viendo: la película a medias y el capítulo que sigue",
          {c["id"] for c in cont} == {movie.id, dark.episodes[1].id},
          str([c.get("code") or c["title"] for c in cont]))


def test_thumbnails(root: Path, data: Path):
    print("\n\033[1mMiniaturas\033[0m")
    lib = Library.scan(root)
    thumbs = Thumbnailer(data / "thumbs")
    video = lib.movies[0].path
    t0 = time.monotonic()
    out = thumbs.get(video)
    first = time.monotonic() - t0
    check("saca un cuadro del video", out is not None and out.stat().st_size > 0,
          f"{first:.2f}s")
    check("es un JPEG", out is not None and out.read_bytes()[:2] == b"\xff\xd8")
    t0 = time.monotonic()
    check("la segunda vez sale de la caché", thumbs.get(video) == out
          and time.monotonic() - t0 < 0.05)


# --- servidor y reproducción ------------------------------------------------------

def test_server(root: Path, data: Path):
    print("\n\033[1mServidor del modo biblioteca\033[0m")
    lib = Library.scan(root)
    caster = FakeCaster()
    store = ProgressStore(data / "server-progress.json", save_every=0)
    cache = ConversionCache(directory=data / "converted")
    player = Player(caster, lib, store, PlayOptions(),
                    thumbnailer=Thumbnailer(data / "thumbs"), cache=cache)
    http = serve_library(player, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{http.server_address[1]}"
    player.attach(http, base)
    movie, dark = lib.movies[0], lib.shows[0]

    try:
        status, headers, body = get(base + "/")
        check("sirve la biblioteca en /", status == 200 and b"<title>Library</title>" in body)
        status, _, body = get(base + "/player")
        check("el panel de siempre con la vuelta a la biblioteca",
              status == 200 and b"lib-nav" in body and b"seekBar" in body)

        status, _, body = get(base + "/api/library")
        data_ = json.loads(body)
        check("API de la biblioteca", status == 200 and len(data_["movies"]) == 2
              and len(data_["shows"]) == 3)

        status, _, body = get(f"{base}/api/item/{movie.id}")
        item = json.loads(body)
        check("detalle con duración", status == 200 and 9 < item["duration"] < 11,
              str(item.get("duration")))
        check("encuentra el .srt de al lado y lo elige", item["default_sub"] is not None
              and len(item["subs"]) == 1, str(item["subs"]))
        check("item inexistente da 404", get(base + "/api/item/nada")[0] == 404)

        status, headers, body = get(f"{base}/thumb/{dark.id}")
        check("miniatura de la serie", status == 200
              and headers.get("Content-Type") == "image/jpeg", headers.get("Content-Type", ""))

        check("sin nada elegido no hay /media", get(base + "/media")[0] == 404)
        status, _ = post(base + "/api/cmd", {"cmd": "toggle"})
        check("comandos sin sesión dan 400", status == 400)
        check("estado en reposo", json.loads(get(base + "/api/status")[2])["phase"] == "idle")

        # Retomar: el progreso guardado decide dónde arranca.
        store.update(movie.path, 45.0, 0.0)
        status, snap = post(base + "/api/play", {"id": movie.id})
        check("play responde enseguida, cargando", status == 200
              and snap["phase"] in ("loading", "playing"), snap.get("phase", ""))
        check("arranca", wait_for(lambda: player.session is not None and caster.loads))
        check("retoma desde lo guardado", caster.loads[-1].get("current_time") == 45.0,
              str(caster.loads[-1].get("current_time")))
        check("con el subtítulo elegido", caster.loads[-1].get("active_track_id") == 1)
        snap = json.loads(get(base + "/api/status")[2])
        check("el estado trae el título lindo", snap["phase"] == "playing"
              and snap["title"] == "Blade Runner 2049 (2017)", snap.get("title", ""))
        status, headers, _ = get(base + "/media")
        check("ahora sí hay /media", status == 200)

        # Un capítulo, con los subtítulos apagados, que al terminar sigue solo.
        first = dark.episodes[0]
        post(base + "/api/play", {"id": first.id, "sub": "off", "start": 0})
        check("cambia de video sin reiniciar nada",
              wait_for(lambda: player.item is first and len(caster.loads) == 2))
        check("subtítulos apagados", caster.loads[-1].get("active_track_id") is None)
        snap = json.loads(get(base + "/api/status")[2])
        check("ofrece el siguiente", (snap.get("next") or {}).get("code") == "S01E02")

        time.sleep(1.3)  # que el seguimiento vea la reproducción
        caster.finish()
        check("al terminar pasa al siguiente",
              wait_for(lambda: player.item is dark.episodes[1] and len(caster.loads) == 3),
              str(player.item.code if player.item else None))
        check("y marca el anterior como visto", (store.get(first.path) or {}).get("watched"))
        check("el siguiente arranca de cero", not caster.loads[-1].get("current_time"))
        check("en modo pipe (remux) sin seek nativo", caster.loads[-1]["live"] is True)

        snap = json.loads(get(base + "/api/status")[2])
        check("sin pedir /media todavía no hay conversión", snap.get("conversion") is None)
        get(base + "/media")  # el receptor arranca ffmpeg al pedir el video
        check("informa hasta dónde convirtió", wait_for(lambda: (json.loads(
            get(base + "/api/status")[2]).get("conversion") or {}).get("done")))
        conv = json.loads(get(base + "/api/status")[2])["conversion"]
        check("y llega hasta el final", conv["start"] == 0
              and abs(conv["end"] - player.session.duration()) < 0.2
              and conv["mode"] == "remux", str(conv))
        check("el avance no ensucia los errores de ffmpeg",
              "bitrate=" not in http.transcoder.error, http.transcoder.error[:80])

        post(base + "/api/cmd", {"cmd": "next"})
        check("botón 'siguiente'",
              wait_for(lambda: player.item is dark.episodes[2] and len(caster.loads) == 4))

        # Los videos de prueba duran 10 s: cualquier posición cuenta como
        # "vista", así que acá se mira la posición guardada y no el retomar.
        caster._pos = 4.0
        status, snap = post(base + "/api/cmd", {"cmd": "stop"})
        check("stop vuelve a la biblioteca sin cerrar ltc",
              snap["phase"] == "idle" and not player.quit_requested.is_set())
        check("y guarda dónde quedó",
              (store.get(dark.episodes[2].path) or {}).get("position") == 4.0,
              str(store.get(dark.episodes[2].path)))

        status, snap = post(base + "/api/play", {"id": "nada"})
        check("play de algo que no existe da 400", status == 400)

        # --- conversiones guardadas ---
        nueve = lib.movies[1]
        status, _, body = get(f"{base}/api/item/{nueve.id}")
        detail = json.loads(body)
        check("la ficha sabe que hay que reencodar y que no está guardado",
              detail["mode"] == "transcode" and detail["saved"] is False, str(detail.get("mode")))

        # "Play" sobre algo sin convertir: no castea, convierte y arranca solo.
        loads_before = len(caster.loads)
        status, snap = post(base + "/api/play", {"id": nueve.id, "start": 0, "wait": True})
        check("Play con wait responde enseguida", status == 200)
        check("queda esperando la conversión, sin castear",
              wait_for(lambda: (json.loads(get(base + "/api/status")[2]).get("pending") or {})
                       .get("id") == nueve.id or len(caster.loads) > loads_before, timeout=10)
              and (len(caster.loads) == loads_before or caster.loads[-1]["live"] is False))
        check("cuando termina arranca solo, desde lo guardado y con seek real",
              wait_for(lambda: len(caster.loads) == loads_before + 1, timeout=60)
              and caster.loads[-1]["live"] is False, str(caster.loads[-1].get("live")))
        snap = json.loads(get(base + "/api/status")[2])
        check("y ya no queda nada pendiente", snap["pending"] is None
              and (snap.get("conversion") or {}).get("saved"))
        saved = list((data / "converted").glob("*.mp4"))
        check("queda un MP4 en la caché", len(saved) == 1 and saved[0].stat().st_size > 0)
        info = probe(saved[0]) if saved else None
        check("que es un MP4 con video H.264", info is not None
              and info.video and info.video[0].codec == "h264"
              and "mp4" in info.container, info.container if info else "—")

        post(base + "/api/cmd", {"cmd": "stop"})
        detail = json.loads(get(f"{base}/api/item/{nueve.id}")[2])
        check("después del stop la ficha lo marca guardado", detail["saved"] is True)
        grid = json.loads(get(base + "/api/library")[2])
        marks = {m["id"]: m["saved"] for m in grid["movies"]}
        check("y la grilla también, sin probar cada archivo",
              marks == {nueve.id: True, movie.id: False}, str(marks))
        post(base + "/api/play", {"id": nueve.id, "start": 0, "wait": True})
        check("la segunda vez arranca directo con lo guardado",
              wait_for(lambda: len(caster.loads) == loads_before + 2)
              and caster.loads[-1]["live"] is False)
        status, headers, _ = get(base + "/media")
        check("con tamaño exacto", status == 200
              and int(headers.get("Content-Length", 0)) == saved[0].stat().st_size)
        post(base + "/api/cmd", {"cmd": "stop"})

        post(base + "/api/quit")
        check("/api/quit cierra", player.quit_requested.is_set())
    finally:
        player.close()
        http.shutdown_all()
        http.shutdown()


def test_failure(root: Path, data: Path):
    print("\n\033[1mCorte del receptor y reintento\033[0m")
    lib = Library.scan(root)
    caster = FakeCaster()
    store = ProgressStore(data / "failure-progress.json", save_every=0)
    cache = ConversionCache(directory=data / "converted")
    player = Player(caster, lib, store, PlayOptions(),
                    thumbnailer=Thumbnailer(data / "thumbs"), cache=cache)
    http = serve_library(player, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{http.server_address[1]}"
    player.attach(http, base)
    movie = lib.movies[0]
    try:
        # los videos de prueba duran 10 s
        post(base + "/api/play", {"id": movie.id, "start": 4})
        check("arranca", wait_for(lambda: player.session is not None and caster.loads))
        snap = json.loads(get(base + "/api/status")[2])
        check("sin cortes no hay error de reproducción",
              snap.get("error_kind") != "playback", snap.get("error_kind", ""))

        caster.failure = "The receiver refused the stream (MEDIA_SRC_NOT_SUPPORTED)."
        snap = json.loads(get(base + "/api/status")[2])
        check("el corte llega al panel", snap["error_kind"] == "playback"
              and "refused" in snap["error"], snap.get("error", ""))

        loads_before = len(caster.loads)
        status, snap = post(base + "/api/cmd", {"cmd": "retry"})
        check("reintentar vuelve a cargar", status == 200
              and wait_for(lambda: len(caster.loads) == loads_before + 1))
        check("desde donde se pidió", caster.loads[-1].get("current_time") == 4.0,
              str(caster.loads[-1].get("current_time")))
        snap = json.loads(get(base + "/api/status")[2])
        check("y el error se va con la carga nueva", snap["error_kind"] != "playback"
              and not snap["error"].startswith("The receiver"), snap.get("error", ""))

        # Si ltc se cierra solo, el panel tiene que poder leer por qué.
        session = player.session
        session.announce_end("The receiver stopped (ERROR).", wait=0)
        snap = json.loads(get(base + "/api/status")[2])
        check("el final anunciado llega al panel", snap["ended"] == "The receiver stopped (ERROR)."
              and snap["ended_ok"] is False, snap.get("ended", ""))

        # Con un panel abierto, el cierre espera a que lo lea (y no se demora si no hay).
        session.end_seen.clear()
        session.ended = ""
        session.last_poll = 0.0
        t0 = time.monotonic()
        session.announce_end("Lost the connection to the Chromecast.", wait=3.0)
        check("sin panel abierto no demora el cierre", time.monotonic() - t0 < 0.5)

        session.end_seen.clear()
        session.last_poll = time.monotonic()
        waiter = threading.Thread(
            target=lambda: session.announce_end("Lost the connection to the Chromecast.", wait=3.0))
        t0 = time.monotonic()
        waiter.start()
        time.sleep(0.3)
        session.note_poll()
        waiter.join(2.0)
        check("con panel abierto espera a que lea y sale apenas lo hace",
              not waiter.is_alive() and time.monotonic() - t0 < 1.5, f"{time.monotonic() - t0:.2f}s")
    finally:
        http.shutdown_all()
        http.shutdown()


def test_cache(root: Path, data: Path):
    print("\n\033[1mCaché de conversiones\033[0m")
    lib = Library.scan(root)
    media = probe(lib.movies[1].path)
    caps = DeviceCaps.for_model("Chromecast")
    plan = plan_for(media, caps)
    cache = ConversionCache(limit_gb=1e-6, directory=data / "evict")  # ~1 KB de tope

    other_audio = plan_for(media, caps, surround=True)
    check("la clave depende del plan", plan_key(media, plan) == plan_key(media, plan_for(media, caps))
          and (plan_key(media, plan) != plan_key(media, other_audio) or plan.downmix == other_audio.downmix))

    old = data / "old.mp4"
    old.write_bytes(b"x" * 2048)
    first = cache.adopt(old, media, plan)
    check("adopt mueve el archivo a la caché", first.parent == cache.directory and not old.exists())
    check("lookup lo encuentra", cache.lookup(media, plan) == first)

    copy = data / "copia.mkv"
    shutil.copy(media.path, copy)
    other = probe(copy)
    newer = data / "new.mp4"
    newer.write_bytes(b"y" * 2048)
    second = cache.adopt(newer, other, plan_for(other, caps))
    check("pasado el tope se borra lo más viejo", second.exists() and not first.exists())
    copy.touch()
    check("si el archivo cambia, la conversión vieja no vale",
          cache.lookup(probe(copy), plan_for(other, caps)) is None)

    # Cancelar una conversión en cola: no se hace y no queda rastro.
    from ltc.cache import BackgroundConverter
    conv = BackgroundConverter(ConversionCache(directory=data / "cancel"))
    a = conv.request(media, plan)
    other_media = probe(copy)
    b = conv.request(other_media, plan_for(other_media, caps))
    conv.cancel(b)
    check("una conversión cancelada no informa avance", conv.progress(b) is None)
    check("la otra sigue y termina", wait_for(lambda: (conv.progress(a) or {}).get("done"), 60))
    time.sleep(0.5)
    check("y la cancelada nunca se convirtió",
          len(list((data / "cancel").glob("*.mp4"))) == 1)
    check("pedirla de nuevo la vuelve a encolar", conv.request(other_media, plan_for(other_media, caps)) == b
          and conv.progress(b) is not None)
    conv.close()

    off = ConversionCache(limit_gb=0, directory=data / "off")
    tmp = data / "tmp.mp4"
    tmp.write_bytes(b"z")
    check("con --cache-size 0 no se guarda nada", off.adopt(tmp, media, plan) == tmp
          and off.lookup(media, plan) is None)


class FakeOpenSubtitles(osub.OpenSubtitles):
    """La API de OpenSubtitles simulada: anota los pedidos y responde enlatado."""

    def __init__(self):
        super().__init__(osub.Credentials(api_key="k", username="u", password="p"))
        self.calls: list[tuple] = []

    def _request(self, method, url, body=None, auth=False):
        self.calls.append((method, url, body, auth))
        if url.endswith("/login"):
            return {"token": "jwt", "base_url": "api.opensubtitles.com"}
        if "/subtitles?" in url:
            def row(fid, lang, n, match=False, machine=False):
                return {"attributes": {"language": lang, "release": f"rel-{fid}", "download_count": n,
                                       "moviehash_match": match, "ai_translated": machine,
                                       "files": [{"file_id": fid, "file_name": f"{fid}.srt"}]}}
            return {"data": [row(1, "en", 9000), row(2, "es", 50), row(3, "es", 10, match=True),
                             row(4, "es", 99999, machine=True), {"attributes": {"files": []}}]}
        if url.endswith("/download"):
            return {"link": "https://dl.example/sub.srt", "file_name": "x.srt", "remaining": 4}
        raise AssertionError(url)

    def _fetch(self, link):
        return "1\n00:00:01,000 --> 00:00:02,000\nDesde OpenSubtitles\n".encode("latin-1")


def test_opensubtitles(root: Path, data: Path):
    print("\n\033[1mOpenSubtitles (API simulada)\033[0m")
    blob = data / "hash.bin"
    blob.write_bytes(b"\x00" * 200_000)
    check("hash de un archivo de ceros es su tamaño", osub.movie_hash(blob) == f"{200_000:016x}")
    blob.write_bytes(b"\x01" + b"\x00" * 199_999)
    check("y suma las palabras de las puntas", osub.movie_hash(blob) == f"{200_001:016x}")
    check("idiomas ida y vuelta", osub.api_language("spa") == "es"
          and osub.file_language("es") == "spa" and osub.file_language("pt-br") == "pob")

    lib = Library.scan(root)
    movie = lib.movies[0]
    api = FakeOpenSubtitles()
    results = api.search(movie.path, title=movie.title, year=movie.year, languages=["es", "en"])
    url = api.calls[-1][1]
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    check("busca por hash, título y año", "moviehash" in params
          and params["query"] == [movie.title.lower()] and params["year"] == ["2017"], url)
    keys = [k for k, _ in urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)]
    check("con los parámetros ordenados, como pide la API", keys == sorted(keys))
    check("primero lo que coincide con el archivo, después el idioma, y lo traducido a máquina al final",
          [r["file_id"] for r in results] == [3, 2, 1, 4], str([r["file_id"] for r in results]))

    # Servidor: buscar y bajar, con la descarga del enlace también simulada.
    caster = FakeCaster()
    store = ProgressStore(data / "os-progress.json", save_every=0)
    player = Player(caster, lib, store, PlayOptions(),
                    thumbnailer=Thumbnailer(data / "thumbs"),
                    cache=ConversionCache(directory=data / "os-conv"))
    http = serve_library(player, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{http.server_address[1]}"
    player.attach(http, base)
    http.opensubtitles = api
    try:
        status, _, body = get(f"{base}/api/item/{movie.id}/subtitles")
        found = json.loads(body)
        check("el panel busca", status == 200 and len(found["results"]) == 4
              and found["languages"] == ["es", "en"], str(found.get("languages")))
        status, saved = post(f"{base}/api/item/{movie.id}/subtitles", {"file_id": 3, "language": "es"})
        target = movie.path.with_name(f"{movie.path.stem}.spa.srt")
        check("lo deja al lado del video como .spa.srt",
              status == 200 and saved["file"] == target.name and target.is_file(), str(saved))
        download_call = next(c for c in reversed(api.calls) if c[1].endswith("/download"))
        check("la descarga pide la sesión de la cuenta", download_call[3] is True)
        check("y el login guarda el token", api._login() == "jwt"
              and api.calls[-1][2] == {"username": "u", "password": "p"})
        detail = json.loads(get(f"{base}/api/item/{movie.id}")[2])
        new = [t for t in detail["subs"] if t["file"] == target.name]
        check("y aparece como una pista más", len(new) == 1 and new[0]["lang"] == "spa", str(detail["subs"]))
        status, again = post(f"{base}/api/item/{movie.id}/subtitles", {"file_id": 2, "language": "es"})
        check("una segunda descarga no pisa la primera",
              again["file"] == f"{movie.path.stem}.2.spa.srt", again.get("file", ""))
        srt = "1\n00:00:01,000 --> 00:00:02,000\nArrastrado\n".encode("latin-1")
        def upload(name, body):
            req = urllib.request.Request(
                f"{base}/api/item/{movie.id}/subtitle-file?name={urllib.parse.quote(name)}",
                data=body, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())
        status, up = upload("../../Peli.es.srt", srt)
        check("subir un .srt lo deja al lado del video, sin salirse de la carpeta",
              status == 200 and "/" not in up["file"]
              and (movie.path.parent / up["file"]).is_file(), str(up))
        check("y conserva el idioma del nombre", up["file"].endswith(".spa.srt") if status == 200 else False,
              str(up))
        check("rechaza lo que no es un subtítulo", upload("virus.exe", srt)[0] == 400)
        check("y un archivo que no tiene líneas de subtítulo", upload("vacio.srt", b"hola")[0] == 400)
        for f in movie.path.parent.glob(f"{movie.path.stem}*.spa.srt"):
            f.unlink()
    finally:
        player.close()
        http.shutdown_all()
        http.shutdown()

    sin_key = osub.OpenSubtitles(osub.Credentials())
    try:
        sin_key.search(movie.path, title="x", languages=["es"])
        check("sin API key avisa qué falta", False)
    except osub.OpenSubtitlesError as exc:
        check("sin API key avisa qué falta", "opensubtitles.json" in str(exc), str(exc))


if __name__ == "__main__":
    print(f"\033[1mProbando la biblioteca con {MEDIA}\033[0m")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root, data = tmp / "lib", tmp / "data"
        root.mkdir()
        data.mkdir()
        build_tree(root)
        test_names()
        test_scan(root, data)
        test_thumbnails(root, data)
        test_cache(root, data)
        test_opensubtitles(root, data)
        test_server(root, data)
        test_failure(root, data)

    print(f"\n  \033[32m{len(PASS)} pasaron\033[0m", end="")
    if FAIL:
        print(f", \033[31m{len(FAIL)} fallaron\033[0m:")
        for f in FAIL:
            print(f"    - {f}")
        sys.exit(1)
    print("\n")
