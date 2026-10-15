"""Second HomeKit bridge: iCloud recording, faces, zones, motion, intercom.

Home Assistant's own HomeKit Bridge cannot do HomeKit Secure Video. This
starts the bundled go2rtc build that can, and feeds it the local H.264
stream. Face recognition and activity zones are the Home app controls that
appear after the accessory is paired and recording is turned on.
"""

from __future__ import annotations

import asyncio
from asyncio.subprocess import Process
import logging
from pathlib import Path
import platform

import aiohttp

from homeassistant.components.persistent_notification import async_create as notify
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_HOMEKIT_SECURE, DOMAIN
from .homekit_config import (
    API_PORT,
    SRTP_PORT,
    WEBRTC_PORT,
    HomeKitCamera,
    format_pin,
    render_config,
    saved_identities,
)

_LOGGER = logging.getLogger(__name__)

_bridges: dict[str, HomeKitSecureBridge] = {}
_pending: dict[str, asyncio.Task] = {}


def bundled_go2rtc() -> Path | None:
    """Return the go2rtc binary for this machine, if one was shipped."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64"):
        arch = "amd64"
    else:
        return None
    if system not in ("linux", "darwin"):
        return None
    path = Path(__file__).resolve().parent / "bin" / f"go2rtc_{system}_{arch}"
    if path.is_file():
        return path
    return None


def async_schedule_homekit_bridge(hass: HomeAssistant, entry_id: str) -> None:
    """Start the bridge once cameras have opened their local streams."""
    previous = _pending.get(entry_id)
    if previous is not None and not previous.done():
        previous.cancel()

    async def _later() -> None:
        try:
            await asyncio.sleep(2)
            await async_start_homekit_bridge(hass, entry_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HomeKit Secure Video bridge failed to start")

    _pending[entry_id] = hass.async_create_task(_later())


async def async_start_homekit_bridge(hass: HomeAssistant, entry_id: str) -> None:
    """Start or refresh the bridge for one config entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or not entry.options.get(CONF_HOMEKIT_SECURE, True):
        await async_stop_homekit_bridge(hass, entry_id)
        return
    stored = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    workers = stored.get("workers") or {}
    cameras: list[HomeKitCamera] = []
    for worker in workers.values():
        url = worker.transcoder.stream_url
        if worker.transcode and url:
            cameras.append(HomeKitCamera(worker.mac_id, worker.name, url))
    if not cameras:
        _LOGGER.info("HomeKit Secure Video bridge waiting for a transcoded stream")
        return
    bridge = _bridges.get(entry_id)
    if bridge is None:
        bridge = HomeKitSecureBridge(hass, entry_id)
        _bridges[entry_id] = bridge
    await bridge.async_apply(cameras)


async def async_stop_homekit_bridge(hass: HomeAssistant, entry_id: str) -> None:
    """Stop the bridge started for a config entry."""
    pending = _pending.pop(entry_id, None)
    if pending is not None and not pending.done():
        pending.cancel()
    bridge = _bridges.pop(entry_id, None)
    if bridge is not None:
        await bridge.async_stop()


class HomeKitSecureBridge:
    """One go2rtc process exporting every camera on this account."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._proc: Process | None = None
        self._motion: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._signature: tuple[tuple[str, str, str], ...] = ()
        self._sent: dict[str, bool] = {}
        self._config_path = Path(hass.config.path("hass_hjq_homekit.yaml"))

    async def async_apply(self, cameras: list[HomeKitCamera]) -> None:
        """Rewrite config and start go2rtc when the camera set changes."""
        signature = tuple(
            (camera.stream_id, camera.name, camera.source_url) for camera in cameras
        )
        if signature == self._signature and self._running:
            return
        self._signature = signature
        binary = bundled_go2rtc()
        if binary is None:
            notify(
                self.hass,
                "没有找到适用于这台机器的 HomeKit 组件，iCloud 录像配件没有启动。",
                title="家庭 App 录像未启动",
                notification_id=f"hass_hjq_homekit_{self.entry_id}",
            )
            return
        try:
            binary.chmod(0o755)
        except OSError:
            pass
        previous = ""
        if self._config_path.is_file():
            previous = self._config_path.read_text(encoding="utf-8")
        text = render_config(cameras, saved_identities(previous))
        self._config_path.write_text(text, encoding="utf-8")
        try:
            await self._restart(binary)
        except OSError as err:
            _LOGGER.error("HomeKit bridge did not start: %s", err)
            notify(
                self.hass,
                f"家庭 App 录像配件没有启动：{err}",
                title="家庭 App 录像未启动",
                notification_id=f"hass_hjq_homekit_{self.entry_id}",
            )
            return
        self._announce(cameras, saved_identities(text))
        if self._motion is None or self._motion.done():
            self._motion = self.hass.async_create_task(self._motion_loop())

    async def async_stop(self) -> None:
        """Stop go2rtc and the motion forwarder."""
        if self._motion is not None and not self._motion.done():
            self._motion.cancel()
        self._motion = None
        await self._kill()

    @property
    def _running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _announce(self, cameras: list[HomeKitCamera], saved: dict[str, dict]) -> None:
        lines = []
        for camera in cameras:
            pin = (saved.get(camera.stream_id) or {}).get("pin") or ""
            lines.append(f"{camera.name}    配对码 {format_pin(pin)}")
        notify(
            self.hass,
            "\n".join(
                [
                    "用下面的配对码把摄像头加到家庭 App（这是带 iCloud 录像的配件，不是 Home Assistant 原来的 HomeKit Bridge）：",
                    *lines,
                    "",
                    "添加后点开摄像头，打开「录制」。之后可以在家庭 App 里看 iCloud 录像、人脸，以及画活动区域。对讲按钮会一起出现。",
                    f"家庭 App 要能发现配件，HA 需要 host 网络，或放行 TCP {API_PORT}、UDP/TCP {SRTP_PORT} 和 {WEBRTC_PORT}。",
                ]
            ),
            title="家庭 App 配对码",
            notification_id=f"hass_hjq_homekit_{self.entry_id}",
        )

    async def _restart(self, binary: Path) -> None:
        await self._kill()
        _LOGGER.info("Starting HomeKit Secure Video bridge: %s", binary.name)
        self._proc = await asyncio.create_subprocess_exec(
            str(binary),
            "-c",
            str(self._config_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").strip()
                if text:
                    _LOGGER.info("go2rtc: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return

    async def _kill(self) -> None:
        if self._stderr is not None and not self._stderr.done():
            self._stderr.cancel()
        self._stderr = None
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def _motion_loop(self) -> None:
        while True:
            try:
                await self._push_motion()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.debug("HomeKit motion forward failed", exc_info=True)
            await asyncio.sleep(2)

    async def _push_motion(self) -> None:
        stored = self.hass.data.get(DOMAIN, {}).get(self.entry_id) or {}
        workers = stored.get("workers") or {}
        session = async_get_clientsession(self.hass)
        for worker in workers.values():
            stream = HomeKitCamera(worker.mac_id, worker.name, "").stream_id
            active = bool(worker.motion_is_on)
            if self._sent.get(stream) is active:
                continue
            method = "POST" if active else "DELETE"
            try:
                async with session.request(
                    method,
                    f"http://127.0.0.1:{API_PORT}/api/homekit/motion",
                    params={"id": stream},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status >= 300:
                        continue
            except (TimeoutError, OSError):
                return
            self._sent[stream] = active
