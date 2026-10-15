"""go2rtc config for the HomeKit Secure Video accessories."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

try:
    import yaml
except ImportError:  # pragma: no cover - Home Assistant always has PyYAML
    yaml = None

API_PORT = 11984
RTSP_PORT = 18554
WEBRTC_PORT = 18555
SRTP_PORT = 18443

_INSECURE_PINS = {
    "00000000",
    "11111111",
    "22222222",
    "33333333",
    "44444444",
    "55555555",
    "66666666",
    "77777777",
    "88888888",
    "99999999",
    "12345678",
    "87654321",
}


@dataclass(frozen=True)
class HomeKitCamera:
    """One camera exported to Apple Home."""

    mac_id: str
    name: str
    source_url: str

    @property
    def stream_id(self) -> str:
        return stream_id(self.mac_id)


def stream_id(mac_id: str) -> str:
    """Return a stable go2rtc stream name."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in mac_id)
    return f"hjq_{safe}"


def pin_for(mac_id: str) -> str:
    """Return a stable 8-digit pairing PIN."""
    number = int.from_bytes(hashlib.sha256(mac_id.encode()).digest()[:4], "big")
    for offset in range(32):
        pin = f"{((number + offset) % 90000000) + 10000000:08d}"
        if pin not in _INSECURE_PINS:
            return pin
    return "27041991"


def format_pin(pin: str) -> str:
    """Format a PIN the way the Home app expects it."""
    digits = pin.replace("-", "")
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def saved_identities(text: str) -> dict[str, dict]:
    """Read pins and pairings already written by a previous run."""
    if not text.strip() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    homekit = data.get("homekit") if isinstance(data, dict) else None
    if not isinstance(homekit, dict):
        return {}
    saved: dict[str, dict] = {}
    for key, value in homekit.items():
        if not isinstance(value, dict):
            continue
        pairings = value.get("pairings") or []
        if not isinstance(pairings, list):
            pairings = []
        saved[str(key)] = {
            "pin": str(value.get("pin") or "").replace("-", ""),
            "pairings": [str(item) for item in pairings],
        }
    return saved


def render_config(cameras: list[HomeKitCamera], saved: dict[str, dict]) -> str:
    """Render go2rtc YAML. Pairings already saved are copied back in."""
    lines = [
        "# Written by hass_hjq. Pairings in this file are kept across restarts.",
        "api:",
        f'  listen: ":{API_PORT}"',
        "rtsp:",
        f'  listen: ":{RTSP_PORT}"',
        "webrtc:",
        f'  listen: ":{WEBRTC_PORT}"',
        "srtp:",
        f'  listen: ":{SRTP_PORT}"',
        "log:",
        "  level: info",
        "streams:",
    ]
    blocks: list[tuple[HomeKitCamera, str, list[str]]] = []
    for camera in cameras:
        previous = saved.get(camera.stream_id) or {}
        pin = previous.get("pin") or pin_for(camera.mac_id)
        pairings = list(previous.get("pairings") or [])
        blocks.append((camera, pin, pairings))
        lines.append(f"  {camera.stream_id}:")
        lines.append(f"    - {_yaml_str(camera.source_url)}")
        lines.append(
            f"    - {_yaml_str(f'ffmpeg:{camera.stream_id}#audio=opus')}"
        )
    lines.append("homekit:")
    for camera, pin, pairings in blocks:
        lines.append(f"  {camera.stream_id}:")
        lines.append(f"    pin: {_yaml_str(pin)}")
        lines.append(f"    name: {_yaml_str(camera.name)}")
        lines.append("    hksv: true")
        lines.append("    motion: api")
        lines.append("    speaker: true")
        if pairings:
            lines.append("    pairings:")
            for pairing in pairings:
                lines.append(f"      - {_yaml_str(pairing)}")
    lines.append("")
    return "\n".join(lines)


def _yaml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
