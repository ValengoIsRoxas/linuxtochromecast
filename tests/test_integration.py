"""Prueba el servidor de punta a punta con un Chromecast simulado.

No hace falta hardware: lo que importa es que el HTTP que ve el receptor sea
correcto (Range, CORS, tipos MIME) y que el pipeline de ffmpeg produzca un MP4
que se pueda decodificar.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ltc.caster import Device  # noqa: E402
from ltc.probe import probe  # noqa: E402
from ltc.server import serve  # noqa: E402
from ltc.session import Session  # noqa: E402

MEDIA = Path(sys.argv[1] if len(sys.argv) > 1 else "media")

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  \033[2m{detail}\033[0m" if detail else ""))


class FakeCaster:
    """Se hace pasar por un Chromecast y anota lo que le mandan."""

    def __init__(self, model="Chromecast Ultra"):
        self.device = Device("TV Falsa", model, "127.0.0.1", 8009, "uuid", "cast")
        self.loads: list[dict] = []
        self.enabled: list[int] = []
        self.disabled: list[int] = []
        self.player_state = "PLAYING"
        self.volume = 0.5
        self.muted = False
        self._pos = 0.0

    def load(self, url, content_type, **kw):
        self.loads.append({"url": url, "content_type": content_type, **kw})

    def position(self):
        return self._pos

    def play(self): self.player_state = "PLAYING"
    def pause(self): self.player_state = "PAUSED"
    def stop(self): self.player_state = "IDLE"
    def seek(self, p): self._pos = p
    def set_volume(self, v): self.volume = v
    def set_muted(self, m): self.muted = m
    def enable_subtitle(self, i): self.enabled.append(i)
    def disable_subtitle(self, i): self.disabled.append(i)


def get(url, headers=None, limit=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read(limit) if limit else resp.read()
        return resp.status, dict(resp.headers), body


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())


def build(path: Path, caster: FakeCaster) -> tuple[Session, object, str]:
    session = Session.create(probe(path), caster, sub_lang="spa")
    http = serve(session, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{http.server_address[1]}"
    session.base_url = base
    return session, http, base


# --- direct play (mp4 ya compatible) -------------------------------------------

def test_direct():
    print("\n\033[1mDirect play — MP4 compatible\033[0m")
    caster = FakeCaster()
    session, http, base = build(MEDIA / "compatible.mp4", caster)
    try:
        check("elige modo directo", session.plan.mode == "direct", session.plan.mode)
        check("permite seek nativo", session.plan.seekable)

        size = (MEDIA / "compatible.mp4").stat().st_size

        status, headers, body = get(f"{base}/media")
        check("GET /media responde 200", status == 200)
        check("declara Accept-Ranges", headers.get("Accept-Ranges") == "bytes")
        check("sirve el archivo entero", len(body) == size, f"{len(body)} bytes")

        status, headers, body = get(f"{base}/media", {"Range": "bytes=100-199"})
        check("Range devuelve 206", status == 206, str(status))
        check("Content-Range correcto",
              headers.get("Content-Range") == f"bytes 100-199/{size}",
              headers.get("Content-Range", "—"))
        check("devuelve exactamente 100 bytes", len(body) == 100, f"{len(body)}")

        status, headers, _ = get(f"{base}/media", {"Range": "bytes=100-"})
        check("Range abierto funciona", status == 206)

        session.start(0)
        check("anuncia stream BUFFERED (con seek nativo)",
              caster.loads[-1]["live"] is False)
        check("encuentra el .srt de al lado", len(session.usable_tracks()) == 1,
              f"{len(session.usable_tracks())} pistas")
        check("lo activa solo por ser español", session.active_sub == 1,
              str(session.active_sub))

        # El receptor pide el .vtt por XHR: sin CORS lo descarta sin avisar.
        status, headers, body = get(f"{base}/sub/1.vtt")
        check("subtítulo con CORS",
              headers.get("Access-Control-Allow-Origin") == "*",
              headers.get("Access-Control-Allow-Origin", "FALTA"))
        check("MIME text/vtt", "text/vtt" in headers.get("Content-Type", ""),
              headers.get("Content-Type", "—"))
        check("empieza con WEBVTT", body.startswith(b"WEBVTT"))
        # Lo que importa no es la frase sino que los bytes latin-1 hayan salido
        # como los caracteres correctos, sin mojibake ni reemplazos.
        text = body.decode("utf-8")
        check("los acentos sobrevivieron al latin-1",
              all(ch in text for ch in "éíá¿") and not any(
                  bad in text for bad in ("Ã", "�", "ń")),
              " | ".join(l for l in text.splitlines() if l and "-->" not in l
                         and l != "WEBVTT"))

        status, headers, _ = get(f"{base}/media", {"Range": "bytes=-500"})
        check("Range por sufijo funciona", status == 206)

        status, snap = post(f"{base}/api/cmd", {"cmd": "seek", "value": 4.0})
        check("seek directo no recarga el media", len(caster.loads) == 1
              and abs(caster._pos - 4.0) < 0.01, f"{len(caster.loads)} loads")
    finally:
        http.shutdown_all(); http.shutdown()


# --- transcode (mkv con h264 4:4:4 + ac3) --------------------------------------

def test_transcode():
    print("\n\033[1mTranscodificación — MKV con AC3 y perfil 4:4:4\033[0m")
    caster = FakeCaster()
    session, http, base = build(MEDIA / "pelicula.mkv", caster)
    try:
        check("elige transcodificar", session.plan.mode == "transcode", session.plan.mode)
        check("recodifica el video", session.plan.video_codec == "libx264")
        check("convierte el audio a AAC", session.plan.audio_codec == "aac")
        check("explica por qué", len(session.plan.reasons) >= 2,
              " | ".join(session.plan.reasons))

        session.start(0)
        check("anuncia stream LIVE", caster.loads[-1]["live"] is True)
        check("anuncia las dos pistas de subtítulos",
              len(caster.loads[-1]["tracks"]) == 2)
        check("activa el subtítulo en español",
              caster.loads[-1]["active_track_id"] is not None)

        # El pipe de ffmpeg tiene que producir un MP4 legible.
        _, headers, body = get(f"{base}/media", limit=400_000)
        check("stream sin Range", headers.get("Accept-Ranges") == "none")
        check("llegaron datos", len(body) > 50_000, f"{len(body)} bytes")

        # Probado contra hardware: el receptor rechaza el chunked para MP4 con
        # LOAD_FAILED sin mostrar un solo cuadro. Necesita Content-Length.
        check("promete un Content-Length", headers.get("Content-Length") is not None,
              headers.get("Content-Length", "FALTA"))
        check("no usa Transfer-Encoding: chunked",
              "chunked" not in (headers.get("Transfer-Encoding") or "").lower(),
              headers.get("Transfer-Encoding", "—"))
        check("lo prometido supera al archivo original",
              session.estimated_stream_size() > session.media.size,
              f"{session.estimated_stream_size()/1e6:.1f} MB vs {session.media.size/1e6:.1f} MB")

        tmp = MEDIA / "_out.mp4"
        tmp.write_bytes(body)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", str(tmp)], capture_output=True, timeout=30)
        streams = json.loads(out.stdout or b'{"streams":[]}').get("streams", [])
        codecs = {s["codec_type"]: s["codec_name"] for s in streams}
        check("el MP4 generado es decodificable", bool(streams), str(codecs))
        check("video H.264", codecs.get("video") == "h264", codecs.get("video", "—"))
        check("audio AAC", codecs.get("audio") == "aac", codecs.get("audio", "—"))
        tmp.unlink(missing_ok=True)

        # Seek en modo pipe: reinicia ffmpeg y corre los subtítulos igual.
        before = len(caster.loads)
        session.seek(6.0)
        check("el seek recarga el stream", len(caster.loads) == before + 1)
        check("guarda el offset", abs(session.stream_offset - 6.0) < 2.0,
              f"offset={session.stream_offset:.2f}")

        shift = session.subtitle_shift()
        _, _, body = get(f"{base}/sub/2.vtt?d=0")
        text = body.decode()
        check("los subtítulos se corren con el stream",
              "00:00:02.000" not in text and "WEBVTT" in text,
              f"shift={shift:.2f}s")

        # Delay manual: cambia la URL para invalidar el caché del receptor.
        before = len(caster.loads)
        session.set_delay(1.5)
        check("el delay recarga con la URL nueva", len(caster.loads) == before + 1)
        check("la URL del .vtt lleva el delay",
              "d=1.500" in caster.loads[-1]["tracks"][0]["trackContentId"],
              caster.loads[-1]["tracks"][0]["trackContentId"].split("?")[-1])

        # Cambiar de pista no debe recargar nada.
        before = len(caster.loads)
        session.set_subtitle(1)
        check("cambiar de subtítulo es instantáneo",
              len(caster.loads) == before and caster.enabled[-1] == 1)
        session.set_subtitle(None)
        check("apagar subtítulos usa disable", caster.disabled[-1] == 1)
    finally:
        http.shutdown_all(); http.shutdown()


# --- decisiones de compatibilidad ----------------------------------------------

def test_remux():
    """MKV con códecs ya compatibles: sólo hay que cambiarle el envoltorio."""
    print("\n\033[1mRemux — MKV con h264 + aac\033[0m")
    caster = FakeCaster()
    session, http, base = build(MEDIA / "remuxeable.mkv", caster)
    try:
        check("elige reempaquetar", session.plan.mode == "remux", session.plan.mode)
        check("no recodifica nada",
              session.plan.video_codec == "copy" and session.plan.audio_codec == "copy")
        check("avisa que es por el contenedor",
              any("contenedor" in r for r in session.plan.reasons),
              " | ".join(session.plan.reasons))

        session.start(0)
        _, headers, body = get(f"{base}/media", limit=200_000)
        check("el remux produce datos", len(body) > 20_000, f"{len(body)} bytes")

        tmp = MEDIA / "_remux.mp4"
        tmp.write_bytes(body)
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                              "-show_streams", str(tmp)], capture_output=True, timeout=30)
        codecs = {s["codec_type"]: s["codec_name"]
                  for s in json.loads(out.stdout or b'{"streams":[]}').get("streams", [])}
        check("mantiene los códecs originales",
              codecs.get("video") == "h264" and codecs.get("audio") == "aac", str(codecs))
        tmp.unlink(missing_ok=True)
    finally:
        http.shutdown_all(); http.shutdown()


def test_subtitle_selection():
    """Qué pista se activa sola, que es lo que decide si el usuario ve algo."""
    print("\n\033[1mElección automática de subtítulo\033[0m")
    from ltc.subtitles import SubTrack, pick_default, track_for_file

    def track(tid, lang, forced=False, source="external"):
        return SubTrack(id=tid, label=f"t{tid}", lang=lang, source=source,
                        path=MEDIA / "compatible.es.srt", forced=forced)

    # El caso más común del mundo: "pelicula.mp4" + "pelicula.srt", sin ninguna
    # marca de idioma. Antes se detectaba pero no se activaba.
    solo = [track(1, "und")]
    check("un único .srt sin idioma se activa igual",
          (pick_default(solo, "spa") or SubTrack(0, "", "", "")).id == 1)

    check("elige la pista en el idioma preferido",
          pick_default([track(1, "eng"), track(2, "spa")], "spa").id == 2)
    check("prefiere la normal sobre la forzada",
          pick_default([track(1, "spa", forced=True), track(2, "spa")], "spa").id == 2)
    check("no activa un idioma declarado distinto al preferido",
          pick_default([track(1, "eng")], "spa") is None)
    check("con varias sin idioma no adivina",
          pick_default([track(1, "und"), track(2, "und")], "spa") is None)

    # -s aceptando una ruta en vez de un número.
    tracks = []
    added = track_for_file(MEDIA / "compatible.es.srt", tracks, prefer="spa")
    check("un .srt pasado a mano se agrega como pista",
          len(tracks) == 1 and added.id == 1)
    again = track_for_file(MEDIA / "compatible.es.srt", tracks, prefer="spa")
    check("no se duplica si ya estaba en la lista",
          len(tracks) == 1 and again is added)


def test_multichannel_audio():
    """Audio 5.1: el códec puede estar soportado y aun así no reproducirse.

    Un Chromecast común decodifica AAC pero no multicanal. Mirando sólo el códec
    la película arranca, reproduce unos segundos y muere con
    MEDIA_SRC_NOT_SUPPORTED (104), que es de los errores más confusos que hay.
    """
    print("\n\033[1mAudio multicanal\033[0m")
    from copy import deepcopy
    from ltc.compat import DeviceCaps, plan_for

    base = probe(MEDIA / "compatible.mp4")

    surround = deepcopy(base)
    surround.audio[0].codec = "aac"
    surround.audio[0].channels = 6

    gen1 = DeviceCaps.for_model("Chromecast")
    ultra = DeviceCaps.for_model("Chromecast Ultra")

    plan = plan_for(surround, gen1)
    check("un AAC 5.1 no va directo a un Chromecast común",
          plan.mode == "transcode", plan.mode)
    check("baja a estéreo", plan.downmix and plan.audio_codec == "aac")
    check("pero no recodifica el video", plan.video_codec == "copy",
          plan.video_codec)
    check("explica el motivo real",
          any("canales" in r for r in plan.reasons), " | ".join(plan.reasons))

    check("en un Ultra el 5.1 pasa sin tocar",
          plan_for(surround, ultra).mode == "direct")
    check("--surround respeta la decisión del usuario",
          plan_for(surround, gen1, surround=True).mode == "direct")

    stereo = deepcopy(base)
    stereo.audio[0].channels = 2
    check("el estéreo sigue yendo directo", plan_for(stereo, gen1).mode == "direct")


def test_connection_info():
    """El CastInfo tiene que ir completo o pychromecast se cuelga 30 segundos.

    Si le falta `cast_type`, pychromecast sale a averiguarlo por HTTP con un
    timeout de 30 s antes de intentar conectarse siquiera. Con dos conexiones
    seguidas eso eran 60 s de silencio total antes de mostrar nada.
    """
    print("\n\033[1mDatos de conexión\033[0m")
    from ltc.caster import Device, _cast_info_for, looks_like_host

    info = _cast_info_for(Device("TV", "Chromecast", "10.0.0.5", 8009,
                                 "cab8ff81-4ef1-0470-67d5-6bd0a4aebb9f", "cast"))
    check("el CastInfo lleva cast_type", bool(info.cast_type), repr(info.cast_type))
    check("apunta al host correcto",
          info.host == "10.0.0.5" and any(s.host == "10.0.0.5" for s in info.services))

    # Un uuid inválido no debe romper la conexión.
    roto = _cast_info_for(Device("TV", "Chromecast", "10.0.0.5", 8009, "", "cast"))
    check("un uuid vacío no rompe nada", roto.uuid is not None and bool(roto.cast_type))

    check("reconoce una IP", looks_like_host("192.168.100.13"))
    check("no confunde un nombre con una IP",
          not looks_like_host("Habitación principal") and not looks_like_host("TV.de.casa"))


def test_caps():
    print("\n\033[1mDecisiones según el dispositivo\033[0m")
    from ltc.compat import DeviceCaps, plan_for

    media = probe(MEDIA / "compatible.mp4")
    mkv = probe(MEDIA / "remuxeable.mkv")
    check("un .mkv nunca va directo aunque comparta demuxer con webm",
          plan_for(mkv, DeviceCaps.for_model("Chromecast")).mode == "remux")
    gen1 = DeviceCaps.for_model("Chromecast")
    ultra = DeviceCaps.for_model("Chromecast Ultra")

    check("detecta Ultra como moderno", "hevc" in ultra.video and ultra.max_height == 2160)
    check("gen1 se queda en 1080p", gen1.max_height == 1080 and "hevc" not in gen1.video)
    check("un MP4 h264+aac va directo",
          plan_for(media, gen1).mode == "direct")
    check("--transcode fuerza recodificar",
          plan_for(media, gen1, force="transcode").mode == "transcode")

    audio_dev = DeviceCaps.for_model("Google Nest Mini", "audio")
    check("rechaza dispositivos de sólo audio", audio_dev.audio_only)


if __name__ == "__main__":
    print(f"\033[1mProbando contra {MEDIA}\033[0m")
    test_direct()
    test_remux()
    test_transcode()
    test_subtitle_selection()
    test_multichannel_audio()
    test_connection_info()
    test_caps()

    print(f"\n  \033[32m{len(PASS)} pasaron\033[0m", end="")
    if FAIL:
        print(f", \033[31m{len(FAIL)} fallaron\033[0m:")
        for f in FAIL:
            print(f"    - {f}")
        sys.exit(1)
    print("\n")
