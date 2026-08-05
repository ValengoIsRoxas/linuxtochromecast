"""Descubrimiento y control del Chromecast.

pychromecast trae `play_media`, pero sólo sabe mandar un subtítulo por vez.
Nosotros armamos el mensaje LOAD a mano para poder anunciar todas las pistas
juntas y que cambiar de idioma después sea instantáneo (EDIT_TRACKS_INFO) en
lugar de tener que recargar el video.
"""

from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass

import pychromecast
from pychromecast.controllers.media import MediaStatus
from pychromecast.dial import get_device_info
from pychromecast.models import CastInfo, HostServiceInfo

DEFAULT_MEDIA_RECEIVER = "CC1AD845"


class CastError(RuntimeError):
    pass


@dataclass
class Device:
    """Un Chromecast, como datos sueltos.

    A propósito no guarda el objeto Chromecast que devuelve el descubrimiento:
    ese objeto necesita que la instancia de zeroconf siga viva para resolver el
    host al conectarse, y el navegador mDNS se cierra apenas termina la búsqueda.
    Con la IP alcanza para conectarse después, sin depender de nada.
    """

    name: str
    model: str
    host: str
    port: int
    uuid: str
    cast_type: str
    manufacturer: str = "Google Inc."

    def __str__(self) -> str:
        return f"{self.name} ({self.model}) — {self.host}"


def _cast_info_for(device: Device) -> CastInfo:
    """Arma el CastInfo completo que necesita pychromecast para conectarse.

    Es importante que lleve `cast_type`: si falta, pychromecast sale a pedirlo
    por HTTP al dispositivo con un timeout de 30 segundos antes de siquiera
    intentar conectarse. Como ya lo sabemos, se lo damos hecho.
    """
    try:
        device_uuid = uuid.UUID(device.uuid)
    except (ValueError, AttributeError, TypeError):
        device_uuid = uuid.uuid4()

    return CastInfo(
        services={HostServiceInfo(device.host, device.port)},
        uuid=device_uuid,
        model_name=device.model,
        friendly_name=device.name,
        host=device.host,
        port=device.port,
        cast_type=device.cast_type or "cast",
        manufacturer=device.manufacturer,
    )


def looks_like_host(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def discover(timeout: float = 6.0, name: str | None = None) -> list[Device]:
    """Busca Chromecasts en la red local vía mDNS.

    Si `name` es una dirección IP, se saltea el descubrimiento y se conecta
    directo — útil cuando mDNS no pasa por la red (VLANs, Wi-Fi de invitados).
    """
    if name and looks_like_host(name):
        return [device_from_host(name)]

    try:
        casts, browser = pychromecast.get_chromecasts(timeout=timeout)
    except Exception as exc:  # zeroconf puede fallar si no hay red
        raise CastError(f"Falló el descubrimiento en la red: {exc}") from exc

    devices = []
    try:
        for cast in casts:
            info = cast.cast_info
            devices.append(Device(
                name=info.friendly_name or "Chromecast",
                model=info.model_name or "Chromecast",
                host=info.host,
                port=info.port,
                uuid=str(info.uuid),
                cast_type=info.cast_type or "cast",
                manufacturer=info.manufacturer or "Google Inc.",
            ))
    finally:
        try:
            pychromecast.discovery.stop_discovery(browser)
        except Exception:
            pass

    if name:
        needle = name.lower()
        exact = [d for d in devices if d.name.lower() == needle]
        devices = exact or [d for d in devices if needle in d.name.lower()]

    devices.sort(key=lambda d: d.name.lower())
    return devices


def device_from_host(host: str, port: int = 8009, timeout: float = 10.0) -> Device:
    """Consulta los datos de un dispositivo por IP.

    Usa el endpoint HTTP de información (unos milisegundos) en vez de abrir una
    conexión de casteo. Así queda una sola conexión en toda la sesión, la que
    abre Caster.
    """
    try:
        status = get_device_info(host, timeout=timeout)
    except Exception as exc:
        raise CastError(f"No pude consultar {host}:{port} — {exc}") from exc

    if status is None:
        raise CastError(
            f"No hay ningún Chromecast respondiendo en {host}. "
            f"Verificá la IP con: ltc --list-devices"
        )

    return Device(
        name=status.friendly_name or host,
        model=status.model_name or "Chromecast",
        host=host,
        port=port,
        uuid=str(status.uuid) if status.uuid else "",
        cast_type=status.cast_type or "cast",
        manufacturer=status.manufacturer or "Google Inc.",
    )


def local_ip_towards(host: str) -> str:
    """IP de esta máquina en la interfaz que llega al Chromecast.

    No sirve 127.0.0.1: el Chromecast tiene que poder alcanzar nuestro server.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 8009))
        return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        sock.close()


class Caster:
    """Conexión activa a un Chromecast."""

    def __init__(self, device: Device, timeout: float = 15.0):
        self.device = device
        try:
            # Conexión directa por IP: no necesita que zeroconf siga corriendo.
            self.cast = pychromecast.get_chromecast_from_cast_info(
                _cast_info_for(device), None, timeout=timeout
            )
            self.cast.wait(timeout=timeout)
        except Exception as exc:
            raise CastError(
                f"No pude conectarme a «{device.name}» ({device.host}): {exc}"
            ) from exc
        self.mc = self.cast.media_controller

    # --- carga ---

    def load(
        self,
        url: str,
        content_type: str,
        *,
        title: str,
        duration: float = 0.0,
        tracks: list[dict] | None = None,
        active_track_id: int | None = None,
        live: bool = False,
        current_time: float | None = None,
        text_style: dict | None = None,
    ) -> None:
        """Manda un LOAD con todas las pistas de subtítulos anunciadas."""
        media: dict = {
            "contentId": url,
            "contentType": content_type,
            "streamType": "LIVE" if live else "BUFFERED",
            "metadata": {"metadataType": 0, "title": title},
        }
        if duration and not live:
            media["duration"] = duration
        if tracks:
            media["tracks"] = tracks
            media["textTrackStyle"] = text_style or default_text_style()

        msg: dict = {"type": "LOAD", "media": media, "autoplay": True, "customData": {}}
        if current_time is not None:
            msg["currentTime"] = current_time
        if active_track_id is not None:
            msg["activeTrackIds"] = [active_track_id]

        self.mc.send_message(msg, inc_session_id=True)

    # --- control ---

    def play(self) -> None:
        self.mc.play()

    def pause(self) -> None:
        self.mc.pause()

    def stop(self) -> None:
        try:
            self.mc.stop()
        except Exception:
            pass

    def seek(self, position: float) -> None:
        self.mc.seek(max(0.0, position))

    def set_volume(self, level: float) -> None:
        self.cast.set_volume(max(0.0, min(1.0, level)))

    def set_muted(self, muted: bool) -> None:
        self.cast.set_volume_muted(bool(muted))

    def enable_subtitle(self, track_id: int) -> None:
        try:
            self.mc.enable_subtitle(track_id)
        except Exception:
            pass

    def disable_subtitle(self, track_id: int) -> None:
        try:
            self.mc.disable_subtitle(track_id)
        except Exception:
            pass

    def quit_app(self) -> None:
        try:
            self.cast.quit_app()
        except Exception:
            pass

    def disconnect(self) -> None:
        try:
            self.cast.disconnect(timeout=5)
        except Exception:
            pass

    # --- estado ---

    @property
    def status(self) -> MediaStatus:
        return self.mc.status

    @property
    def volume(self) -> float:
        status = self.cast.status
        return float(status.volume_level) if status else 0.0

    @property
    def muted(self) -> bool:
        status = self.cast.status
        return bool(status.volume_muted) if status else False

    def position(self) -> float:
        """Posición dentro del stream actual (no de la película)."""
        status = self.mc.status
        if status is None:
            return 0.0
        try:
            return float(status.adjusted_current_time or 0.0)
        except (TypeError, ValueError):
            return float(status.current_time or 0.0)

    @property
    def player_state(self) -> str:
        status = self.mc.status
        return (status.player_state if status else None) or "UNKNOWN"

    def wait_for_session(self, timeout: float = 20.0) -> bool:
        """Espera a que el receptor confirme que arrancó a cargar."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.mc.status
            if status and status.media_session_id and status.player_state in (
                "PLAYING", "BUFFERING", "PAUSED"
            ):
                return True
            time.sleep(0.25)
        return False


def default_text_style(scale: float = 1.0) -> dict:
    """Subtítulos legibles: blanco con contorno negro, sin caja de fondo."""
    return {
        "foregroundColor": "#FFFFFFFF",
        "backgroundColor": "#00000000",
        "edgeType": "OUTLINE",
        "edgeColor": "#000000FF",
        "fontScale": scale,
        "fontGenericFamily": "SANS_SERIF",
    }
