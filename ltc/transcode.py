"""Construcción y manejo del pipeline de ffmpeg.

Cuando no se puede hacer direct play, ffmpeg escribe MP4 fragmentado a un pipe.
Un pipe no soporta Range, así que el seek se resuelve matando ffmpeg y
arrancándolo de nuevo desde el offset pedido: el stream siempre empieza en 0 y
nosotros llevamos la cuenta de cuánto vale ese 0 en tiempo real de la película.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

from .compat import Plan
from .probe import MediaInfo

# Tamaño de bloque al leer del pipe. Suficientemente grande para no hacer
# syscalls de más, suficientemente chico para que el arranque no se demore.
CHUNK = 256 * 1024


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


def build_command(media: MediaInfo, plan: Plan, offset: float = 0.0) -> list[str]:
    """Arma la línea de ffmpeg para servir desde `offset`."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]

    # -ss antes de -i habilita el seek rápido por índice.
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", str(media.path)]

    video = media.video[0]
    audio = plan.audio_stream

    if plan.burn_sub is not None:
        # Los subtítulos de imagen se superponen sobre el video decodificado.
        chain = f"[0:v:{video.type_index}][0:s:{plan.burn_sub.type_index}]overlay"
        if plan.scale_height:
            chain += f",scale=-2:{plan.scale_height}"
        cmd += ["-filter_complex", f"{chain}[vout]", "-map", "[vout]"]
    else:
        cmd += ["-map", f"0:v:{video.type_index}"]
        if plan.scale_height:
            cmd += ["-vf", f"scale=-2:{plan.scale_height}"]

    if audio is not None:
        cmd += ["-map", f"0:a:{audio.type_index}"]

    # Video
    if plan.video_codec == "copy":
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            "-c:v", "libx264",
            "-preset", "veryfast",     # tiene que ir más rápido que tiempo real
            "-crf", "21",
            "-profile:v", "high",
            "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-g", "60",                # keyframes seguidos = arranque más ágil
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

    cmd += [
        "-sn",  # los subtítulos van aparte, como pistas WebVTT
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-max_muxing_queue_size", "2048",
        "pipe:1",
    ]
    return cmd


class Transcoder:
    """Un proceso de ffmpeg escribiendo a un pipe, con su offset asociado."""

    def __init__(self, media: MediaInfo, plan: Plan, offset: float = 0.0):
        self.media = media
        self.plan = plan
        self.offset = offset
        self.proc: subprocess.Popen | None = None
        self._stderr = b""
        self._lock = threading.Lock()

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("Couldn't find ffmpeg on your PATH.")
        cmd = build_command(self.media, self.plan, self.offset)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
        )
        # Drenamos stderr en background: si se llena el buffer, ffmpeg se cuelga.
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                with self._lock:
                    self._stderr = (self._stderr + line)[-4096:]
        except (ValueError, OSError):
            pass

    def stream(self):
        """Itera los bytes de salida hasta que ffmpeg termina o cortan el cliente."""
        if self.proc is None:
            self.start()
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                data = self.proc.stdout.read(CHUNK)
                if not data:
                    break
                yield data
        finally:
            self.stop()

    @property
    def error(self) -> str:
        with self._lock:
            return self._stderr.decode("utf-8", "replace").strip()

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
