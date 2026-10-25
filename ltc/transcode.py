"""Construcción y manejo del pipeline de ffmpeg.

Cuando no se puede hacer direct play, ffmpeg reencoda a MP4 fragmentado. En vez
de mandar esa salida derecho al socket, la volcamos a un archivo temporal y
servimos desde ahí.

La diferencia importa: un pipe se consume una sola vez, así que cada
reconexión del receptor obligaba a matar ffmpeg y arrancarlo de nuevo desde el
mismo offset —el receptor recibía otra vez el mismo arranque en lugar de la
continuación, y terminaba quedándose sin datos (MEDIA_SRC_NOT_SUPPORTED, 104)—.
Con el archivo de por medio, una reconexión con `Range` retoma exactamente
donde iba y ffmpeg sigue corriendo sin enterarse.

El seek dentro de la película sí sigue reiniciando ffmpeg desde el offset
pedido: eso es inherente a reencodar, y `Session` lleva la cuenta de cuánto
vale ese cero en tiempo real.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .compat import Plan
from .logutil import get_logger
from .probe import MediaInfo

log = get_logger("transcode")

# Tamaño de bloque al leer del pipe. Suficientemente grande para no hacer
# syscalls de más, suficientemente chico para que el arranque no se demore.
CHUNK = 256 * 1024
# Las líneas `clave=valor` de `-progress`; todo lo demás en stderr son errores.
_PROGRESS_LINE = re.compile(rb"^[a-z_0-9]+=")


def keyframe_at_or_before(path: Path, t: float, window: float = 20.0) -> float:
    """Busca el keyframe más cercano antes de `t`.

    Sólo importa cuando copiamos el video sin recodificar: ahí ffmpeg arranca en
    un keyframe, no exactamente donde le pedimos, y si no lo tenemos en cuenta
    los subtítulos quedan corridos hasta un GOP entero.
    """
    if t <= 0:
        return 0.0
    start = max(0.0, t - window)
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "frame=best_effort_timestamp_time",
        "-read_intervals", f"{start:.3f}%+{t - start + 0.5:.3f}",
        "-of", "csv=p=0", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return t

    times = []
    for line in out.decode("ascii", "ignore").splitlines():
        line = line.strip().rstrip(",")
        try:
            times.append(float(line))
        except ValueError:
            continue

    before = [x for x in times if x <= t + 0.001]
    if before:
        return max(before)
    return min(times) if times else t


@dataclass(frozen=True)
class HWEncoder:
    """Un encoder por hardware que ya probamos y sabemos que anda."""

    name: str            # h264_nvenc, h264_vaapi...
    device: str          # nodo DRM (VAAPI); vacío para NVENC
    label: str           # para mostrarle al usuario


# Se prueba una sola vez por proceso: levantar ffmpeg para preguntar cuesta.
_HW_PROBED = False
_HW: HWEncoder | None = None


def _nvidia_name() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, timeout=10,
        ).stdout.decode("utf-8", "replace").strip().splitlines()
        if out:
            return f"NVENC ({out[0].strip()})"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "NVENC"


def detect_hw_encoder() -> HWEncoder | None:
    """Busca un encoder H.264 por GPU que funcione de verdad.

    Que ffmpeg liste `h264_vaapi` no significa nada: puede no haber GPU, o el
    driver puede fallar al abrir el dispositivo. La única forma confiable es
    encodear unos cuadros de prueba y ver si sale bien.
    """
    global _HW_PROBED, _HW
    if _HW_PROBED:
        return _HW
    _HW_PROBED = True

    if os.environ.get("LTC_NO_HWACCEL"):
        return None
    if not shutil.which("ffmpeg"):
        return None

    available = ""
    try:
        available = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, timeout=15,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None

    # Primero la tarjeta NVIDIA (NVENC): es la GPU de verdad. VAAPI suele ser
    # la gráfica integrada del procesador, que anda pero es mucho más lenta.
    # LTC_HWACCEL=vaapi saltea NVENC; LTC_HWACCEL=nvenc no cae a VAAPI.
    wanted = os.environ.get("LTC_HWACCEL", "").lower()
    if wanted != "vaapi" and "h264_nvenc" in available:
        probe_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=0.4",
            "-vf", "format=yuv420p", "-c:v", "h264_nvenc",
            "-f", "null", "-",
        ]
        try:
            done = subprocess.run(probe_cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            done = None
        if done is not None and done.returncode == 0:
            _HW = HWEncoder("h264_nvenc", "", _nvidia_name())
            log.info("Encoder por hardware: %s", _HW.label)
            return _HW
    if wanted == "nvenc":
        return None

    for node in sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []:
        if "h264_vaapi" not in available:
            break
        probe_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-vaapi_device", str(node),
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=0.4",
            "-vf", "format=nv12|vaapi,hwupload", "-c:v", "h264_vaapi",
            "-f", "null", "-",
        ]
        try:
            done = subprocess.run(probe_cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            _HW = HWEncoder("h264_vaapi", str(node), f"VAAPI ({node.name})")
            log.info("Encoder por hardware: %s", _HW.label)
            return _HW

    return None


def build_command(media: MediaInfo, plan: Plan, offset: float = 0.0,
                  out: str = "pipe:1", hw: HWEncoder | None = None) -> list[str]:
    """Arma la línea de ffmpeg para servir desde `offset`."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]

    use_hw = hw is not None and plan.video_codec not in ("copy", "none")
    nvenc = use_hw and hw.name.endswith("_nvenc")
    if use_hw and hw.device:
        # Tiene que ir antes de -i.
        cmd += ["-vaapi_device", hw.device]
    elif nvenc:
        # Decodifica también en la tarjeta (NVDEC); si no soporta el códec,
        # ffmpeg cae solo al decodificador por CPU. Los cuadros vuelven a
        # memoria normal, así que escala y superpone igual que siempre.
        cmd += ["-hwaccel", "cuda"]

    # -ss antes de -i habilita el seek rápido por índice.
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(media.path)]

    video = media.video[0]
    audio = plan.audio_stream

    # El encoder por GPU necesita los cuadros subidos a memoria de video, y en
    # nv12: la fuente suele ser 10 bits y VAAPI encodea en 8.
    hw_tail = ",format=nv12|vaapi,hwupload" if use_hw and not nvenc else ""
    if nvenc:
        hw_tail = ",format=yuv420p"

    if plan.burn_sub is not None:
        # Los subtítulos de imagen se superponen sobre el video decodificado.
        chain = f"[0:v:{video.type_index}][0:s:{plan.burn_sub.type_index}]overlay"
        if plan.scale_height:
            chain += f",scale=-2:{plan.scale_height}"
        cmd += ["-filter_complex", f"{chain}{hw_tail}[vout]", "-map", "[vout]"]
    else:
        cmd += ["-map", f"0:v:{video.type_index}"]
        vf = []
        if plan.scale_height:
            vf.append(f"scale=-2:{plan.scale_height}")
        if nvenc:
            vf.append("format=yuv420p")
        elif use_hw:
            vf.append("format=nv12|vaapi")
            vf.append("hwupload")
        if vf:
            cmd += ["-vf", ",".join(vf)]

    if audio is not None:
        cmd += ["-map", f"0:a:{audio.type_index}"]

    # Video
    if plan.video_codec == "copy":
        cmd += ["-c:v", "copy"]
    else:
        # Bitrate objetivo en vez de CRF: con CRF el tamaño de salida depende
        # de la complejidad de la escena y no del original, así que no hay
        # forma de prometerle un Content-Length razonable al Chromecast (ver
        # `compat._target_video_bitrate`). Con -maxrate/-bufsize el tamaño
        # queda acotado y predecible.
        bitrate = plan.video_bitrate or 4000
        if use_hw:
            # La GPU va ~25x tiempo real y deja la CPU libre. No acepta
            # -preset/-sc_threshold como libx264, y el pix_fmt lo fija el
            # filtro de arriba.
            cmd += [
                "-c:v", hw.name,
                "-b:v", f"{bitrate}k",
                "-maxrate", f"{bitrate}k",
                "-bufsize", f"{bitrate * 2}k",
                "-profile:v", "high",
                "-level", "4.1" if nvenc else "41",
                "-g", "60",
            ]
            if nvenc:
                # p3 rinde ~25x con la misma calidad (SSIM) que p4; p5 con
                # -tune hq bajaba a ~10x, más lento que la gráfica integrada.
                cmd += ["-preset", "p3", "-rc", "vbr"]
        else:
            cmd += [
                "-c:v", "libx264",
                "-preset", "veryfast",   # tiene que ir más rápido que tiempo real
                "-b:v", f"{bitrate}k",
                "-maxrate", f"{bitrate}k",
                "-bufsize", f"{bitrate * 2}k",
                "-profile:v", "high",
                "-level", "4.1",
                "-pix_fmt", "yuv420p",
                "-g", "60",              # keyframes seguidos = arranque más ágil
                "-sc_threshold", "0",
            ]

    # Audio
    if audio is None:
        cmd += ["-an"]
    elif plan.audio_codec == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        if plan.downmix:
            cmd += ["-ac", "2"]

    # -sn: los subtítulos van aparte, como pistas WebVTT.
    # -dn y -map_chapters -1: los capítulos del MKV se convierten en una pista
    # de texto dentro del MP4 que nadie pidió. El receptor ya demostró ser
    # quisquilloso con lo que hay en el contenedor, así que no le mandamos
    # pistas de más.
    cmd += ["-sn", "-dn", "-map_chapters", "-1"]
    if out == "pipe:1":
        # Por un pipe hay que ir fragmentado: el índice no se puede escribir al
        # final porque no se puede volver atrás.
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]
    else:
        # A un archivo conviene un MP4 común con el índice adelante: queda con
        # tamaño exacto y con seek real, que es todo el punto de encodear antes.
        # -y porque el temporal ya existe (lo creó mkstemp) y si no ffmpeg se
        # planta pidiendo confirmación para sobrescribirlo.
        cmd += ["-movflags", "+faststart", "-y"]
    cmd += ["-f", "mp4", "-max_muxing_queue_size", "2048", out]
    return cmd


class Transcoder:
    """Un ffmpeg volcando su salida a un archivo temporal que se puede releer.

    El archivo es lo que permite que una reconexión del receptor retome por
    donde iba: mientras ffmpeg sigue escribiendo el final, el server puede
    servir cualquier tramo anterior con `Range`.
    """

    def __init__(self, media: MediaInfo, plan: Plan, offset: float = 0.0):
        self.media = media
        self.plan = plan
        self.offset = offset
        self.proc: subprocess.Popen | None = None
        self.path: Path | None = None
        self._stderr = b""
        self._lock = threading.Lock()
        # Cuántos bytes ya están en disco y listos para servir.
        self._size = 0
        self._grew = threading.Condition()
        self._finished = threading.Event()
        # Por qué terminó. Sin esto no hay forma de distinguir "ffmpeg llegó al
        # final del archivo" de "ffmpeg se murió a mitad de camino", que es
        # justo la diferencia entre un final normal y un corte.
        self.returncode: int | None = None
        self.killed = False
        # Segundos ya convertidos desde `offset`, según `-progress` de ffmpeg.
        self._out_time = 0.0

    # --- ciclo de vida ---

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("Couldn't find ffmpeg on your PATH.")
        fd, name = tempfile.mkstemp(prefix="spool-", suffix=".mp4", dir=spool_dir())
        os.close(fd)
        self.path = Path(name)

        cmd = build_command(self.media, self.plan, self.offset,
                            hw=detect_hw_encoder())
        # El avance va por stderr junto con los errores: stdout es el video.
        # `_drain_stderr` los separa.
        cmd = cmd[:1] + ["-progress", "pipe:2", "-nostats"] + cmd[1:]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
        )
        # Drenamos stderr en background: si se llena el buffer, ffmpeg se cuelga.
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._spool, daemon=True).start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                if _PROGRESS_LINE.match(line):
                    if line.startswith(b"out_time_us="):
                        try:
                            seconds = int(line.split(b"=", 1)[1]) / 1_000_000.0
                        except ValueError:  # "N/A" antes del primer cuadro
                            continue
                        with self._lock:
                            self._out_time = max(self._out_time, seconds)
                    continue
                with self._lock:
                    self._stderr = (self._stderr + line)[-4096:]
        except (ValueError, OSError):
            pass

    def _spool(self) -> None:
        """Copia la salida de ffmpeg al archivo, avisando a medida que crece."""
        proc, path = self.proc, self.path
        if proc is None or proc.stdout is None or path is None:
            return
        try:
            with path.open("wb") as fh:
                while True:
                    data = proc.stdout.read(CHUNK)
                    if not data:
                        break
                    fh.write(data)
                    fh.flush()
                    with self._grew:
                        self._size += len(data)
                        self._grew.notify_all()
        except (ValueError, OSError) as exc:
            log.warning("Falló el volcado de ffmpeg a %s: %s", path, exc)
        finally:
            try:
                self.returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.returncode = proc.poll()
            # El stderr se drena en otro hilo; le damos un instante a que
            # termine de juntar las últimas líneas, que son justo las que
            # explican por qué ffmpeg se cayó.
            time.sleep(0.2)
            if self.returncode and not self.killed:
                log.warning(
                    "ffmpeg murió solo con código %s tras volcar %d bytes "
                    "(offset=%.1fs). stderr: %s",
                    self.returncode, self._size, self.offset,
                    self.error or "(vacío)",
                )
            self._finished.set()
            with self._grew:
                self._grew.notify_all()

    # --- lectura ---

    @property
    def size(self) -> int:
        """Bytes disponibles en disco ahora mismo."""
        with self._grew:
            return self._size

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    @property
    def converted(self) -> float:
        """Hasta qué segundo de la película ya está convertido."""
        done_ok = self._finished.is_set() and self.returncode == 0 and not self.killed
        if done_ok and self.media.duration:
            return self.media.duration
        with self._lock:
            return min(self.media.duration or float("inf"), self.offset + self._out_time)

    @property
    def usable(self) -> bool:
        """True si todavía sirve para atender pedidos nuevos.

        Un ffmpeg que ya murió no se puede reusar: si lo devolviéramos igual,
        cada reconexión encontraría un proceso sin datos nuevos y el receptor
        quedaría reintentando contra un stream muerto, sin que nadie vuelva a
        levantar ffmpeg. Sólo se reusa el que sigue produciendo, o el que
        terminó bien de verdad.
        """
        if self.killed:
            return False
        if not self._finished.is_set():
            return True
        return self.returncode == 0

    def wait_for(self, offset: int, timeout: float = 30.0) -> bool:
        """Espera a que haya datos más allá de `offset`.

        Devuelve False si ffmpeg terminó sin llegar hasta ahí: eso significa
        que no va a haber más datos nunca, y el que está sirviendo tiene que
        cortar en vez de quedarse esperando para siempre.
        """
        deadline = time.monotonic() + timeout
        with self._grew:
            while self._size <= offset and not self._finished.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._grew.wait(min(remaining, 1.0))
            return self._size > offset

    def read_from(self, offset: int, count: int) -> bytes:
        """Lee hasta `count` bytes desde `offset` de lo ya volcado."""
        path = self.path
        if path is None:
            return b""
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                return fh.read(count)
        except OSError:
            return b""

    @property
    def error(self) -> str:
        with self._lock:
            return self._stderr.decode("utf-8", "replace").strip()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            # Seguía vivo: lo estamos matando nosotros, no terminó por su cuenta.
            self.killed = True
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if proc is not None:
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
        self._finished.set()
        with self._grew:
            self._grew.notify_all()

    def pad_to(self, target: int) -> None:
        """Rellena con ceros hasta `target` bytes.

        Sólo para el modo `--no-wait`, donde el Content-Length se prometió
        antes de saber el tamaño real. Si la salida termina siendo más chica,
        el receptor se queda esperando bytes que no existen y corta con
        MEDIA_SRC_NOT_SUPPORTED (104) cerca del final; completar la promesa lo
        deja terminar limpio. El relleno va después del último fragmento
        válido, así que no es video: el reproductor ya llegó al final real.
        """
        path = self.path
        if path is None or not self._finished.is_set():
            return
        with self._grew:
            missing = target - self._size
            if missing <= 0:
                return
            try:
                with path.open("r+b") as fh:
                    fh.truncate(target)
            except OSError as exc:
                log.warning("No se pudo rellenar %s hasta %d: %s", path, target, exc)
                return
            self._size = target
            self._grew.notify_all()
        log.info("Relleno de %d bytes para cumplir el Content-Length prometido.", missing)

    def cleanup(self) -> None:
        """Borra el temporal. Aparte de `stop()` porque el que está sirviendo
        puede seguir leyendo un rato después de que ffmpeg terminó."""
        self.stop()
        path, self.path = self.path, None
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


class PrepareError(RuntimeError):
    pass


def spool_dir() -> Path:
    """Dónde van los archivos intermedios.

    A propósito NO en /tmp: en varias distros (Fedora incluida) es tmpfs, o
    sea RAM, y un episodio reencodado pasa el gigabyte.
    """
    d = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "ltc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def encode_whole(media: MediaInfo, plan: Plan,
                 on_progress=None, hw: HWEncoder | None = None,
                 cancel: threading.Event | None = None) -> Path:
    """Reencoda la película entera a un archivo y devuelve su ruta.

    Es el modo por defecto porque resuelve de raíz el problema del
    Content-Length: con el archivo terminado sabemos el tamaño exacto, así que
    no hay nada que estimar y el receptor nunca se queda esperando bytes que no
    existen. De yapa queda seek real, sin reiniciar ffmpeg en cada salto.

    `on_progress` recibe los segundos ya procesados, para poder mostrar avance.
    `cancel` lo corta a mitad de camino (lanza PrepareError): sin eso, cerrar
    ltc dejaba un ffmpeg huérfano encodeando para nadie.
    """
    fd, name = tempfile.mkstemp(prefix="encoded-", suffix=".mp4", dir=spool_dir())
    os.close(fd)
    out = Path(name)

    cmd = build_command(media, plan, 0.0, out=str(out), hw=hw)
    # -progress a stdout da líneas `out_time_us=...` parseables, en vez de
    # tener que adivinar leyendo el stderr con los \r del contador.
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        out.unlink(missing_ok=True)
        raise PrepareError(f"Couldn't start ffmpeg: {exc}") from exc

    try:
        for raw in proc.stdout or ():
            if cancel is not None and cancel.is_set():
                proc.terminate()
                break
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and on_progress is not None:
                try:
                    on_progress(int(line.split("=", 1)[1]) / 1_000_000.0)
                except ValueError:
                    pass
        code = proc.wait()
    finally:
        stderr = b""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read() or b""
            except (OSError, ValueError):
                pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass

    if cancel is not None and cancel.is_set():
        out.unlink(missing_ok=True)
        raise PrepareError("Encoding cancelled.")
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        out.unlink(missing_ok=True)
        raise PrepareError(
            f"ffmpeg failed while encoding (code {code}): "
            f"{detail[-1] if detail else 'no details'}"
        )
    return out
