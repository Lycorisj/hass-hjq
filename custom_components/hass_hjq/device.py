"""Per-camera runtime: live URL, keep-alive, transcode, recording."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components.persistent_notification import (
    async_create as notify,
    async_dismiss,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUDIO,
    CONF_BITRATE,
    CONF_RECORD_SEGMENT,
    CONF_SCALE,
    CONF_TRANSCODE,
    DEFAULT_AUDIO,
    DEFAULT_BITRATE,
    DEFAULT_RECORD_SEGMENT,
    DEFAULT_SCALE,
    DEFAULT_TRANSCODE,
    KEEPALIVE_INTERVAL,
    OUTPUT_AUDIO_BITRATE,
    OUTPUT_AUDIO_CHANNELS,
    OUTPUT_AUDIO_CODEC,
    OUTPUT_AUDIO_RATE,
    OUTPUT_CONTAINER_LIVE,
    OUTPUT_CONTAINER_RECORD,
    OUTPUT_VIDEO_CODEC,
    OUTPUT_VIDEO_LEVEL,
    OUTPUT_VIDEO_PROFILE,
    STREAM_STALE_SECONDS,
)
from .coordinator import HassHjqCoordinator
from .ffmpeg_tools import (
    H264Transcoder,
    SegmentRecorder,
    ffmpeg_available,
    recent_recordings,
    recording_location,
)
from .hjqapi import HJQApiError, HJQAuthError
from .stream_broker import MotionDetector, recovery_action

_LOGGER = logging.getLogger(__name__)

_MOTION_HOLD_SECONDS = 20.0
_SNAPSHOT_MAX_AGE = 8.0


class CameraWorker:
    """Owns cloud live-address lifecycle and local ffmpeg processes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: HassHjqCoordinator,
        camera: dict[str, Any],
        dev_info: dict[str, Any],
    ) -> None:
        """Initialize the worker."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.camera = camera
        self.dev_info = dev_info
        self.mac_id: str = camera["mac_id"]
        self.name: str = camera["mac_name"]
        self._lock = asyncio.Lock()
        self._cloud_url: str | None = None
        self._cloud_url_at: datetime | None = None
        self._stop = False
        self._keep_task: asyncio.Task | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._stream_status = "starting"
        self._status_since = time.monotonic()
        self._failures = 0
        self._next_recover_at = 0.0
        self._live_since = 0.0
        self._motion_at = 0.0
        self._motion: MotionDetector | None = None
        self._snapshot: bytes | None = None
        self._snapshot_at = 0.0
        self.transcoder = H264Transcoder(
            hass,
            self.mac_id,
            scale=entry.options.get(CONF_SCALE, DEFAULT_SCALE),
            audio=entry.options.get(CONF_AUDIO, DEFAULT_AUDIO),
            bitrate=entry.options.get(CONF_BITRATE, DEFAULT_BITRATE),
        )
        self.recorder = SegmentRecorder(hass, self.mac_id, self.name)

    def listen(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when stream or motion state changes."""
        self._listeners.add(callback)

        def _unsub() -> None:
            self._listeners.discard(callback)

        return _unsub

    def push(self) -> None:
        """Tell entities to refresh their state."""
        for callback in list(self._listeners):
            callback()

    @property
    def stream_status(self) -> str:
        """Return starting, live, or reconnecting."""
        return self._stream_status

    @property
    def motion_is_on(self) -> bool:
        """Return true for a short hold after the last detected motion."""
        return (
            self._motion_at > 0
            and time.monotonic() - self._motion_at < _MOTION_HOLD_SECONDS
        )

    @property
    def transcode(self) -> bool:
        """Return whether H.264 transcoding is enabled and ffmpeg is available."""
        return bool(
            self.entry.options.get(CONF_TRANSCODE, DEFAULT_TRANSCODE)
        ) and ffmpeg_available()

    @property
    def recording(self) -> bool:
        """Return whether local recording is active."""
        return self.recorder.recording

    def recording_info(self) -> dict[str, Any]:
        """Return the folder, browser path, and newest files."""
        info = recording_location(self.hass)
        info["latest_recordings"] = recent_recordings(info["recording_directory"])
        return info

    def output_specs(self) -> dict[str, Any]:
        """Return the configured live/record encode specs for HA and HomeKit."""
        audio = bool(self.entry.options.get(CONF_AUDIO, DEFAULT_AUDIO))
        scale = self.entry.options.get(CONF_SCALE, DEFAULT_SCALE)
        bitrate = self.entry.options.get(CONF_BITRATE, DEFAULT_BITRATE)
        return {
            "transcode_h264": self.transcode,
            "live_container": OUTPUT_CONTAINER_LIVE if self.transcode else "cloud",
            "video_codec": OUTPUT_VIDEO_CODEC if self.transcode else "hevc",
            "video_profile": OUTPUT_VIDEO_PROFILE if self.transcode else "main",
            "video_level": OUTPUT_VIDEO_LEVEL if self.transcode else None,
            "video_resolution": scale,
            "video_bitrate": bitrate if self.transcode else "cloud",
            "audio_enabled": audio,
            "audio_codec": OUTPUT_AUDIO_CODEC if audio else None,
            "audio_rate": OUTPUT_AUDIO_RATE if audio else None,
            "audio_channels": OUTPUT_AUDIO_CHANNELS if audio else None,
            "audio_bitrate": OUTPUT_AUDIO_BITRATE if audio else None,
            "record_container": OUTPUT_CONTAINER_RECORD,
            "record_segment_seconds": int(
                self.entry.options.get(CONF_RECORD_SEGMENT, DEFAULT_RECORD_SEGMENT)
            ),
            "stream_status": self._stream_status,
        }

    @property
    def api(self):
        """Return the shared API client."""
        return self.coordinator.api

    def _meta(self) -> dict[str, Any]:
        latest = self.coordinator.camera_data(self.mac_id)
        if latest:
            self.camera = latest
        return self.camera

    def _set_status(self, status: str) -> None:
        if status == self._stream_status:
            return
        self._stream_status = status
        self._status_since = time.monotonic()
        if status != "live":
            self._snapshot = None
            self._snapshot_at = 0.0
        self.push()

    def _on_motion(self) -> None:
        self._motion_at = time.monotonic()
        self.push()

    async def async_start(self) -> None:
        """Start the keep-alive loop and the local transcoder."""
        self._stop = False
        if self.entry.options.get(CONF_TRANSCODE, DEFAULT_TRANSCODE) and not ffmpeg_available():
            _LOGGER.warning(
                "%s: ffmpeg not found, HomeKit H.264 transcode is disabled",
                self.name,
            )
        if self.transcode:
            try:
                await self.transcoder.prepare()
            except OSError:
                _LOGGER.warning("%s: local stream server did not start", self.name)
        from .homekit_bridge import async_schedule_homekit_bridge

        async_schedule_homekit_bridge(self.hass, self.entry.entry_id)
        if self._keep_task is None or self._keep_task.done():
            self._keep_task = self.hass.async_create_background_task(
                self._keep_loop(),
                name=f"hass_hjq_keep_{self.mac_id}",
            )

    async def async_stop(self) -> None:
        """Stop keep-alive, transcoding and recording."""
        self._stop = True
        if self._keep_task and not self._keep_task.done():
            self._keep_task.cancel()
        if self._motion is not None:
            await self._motion.stop()
        await self.recorder.stop()
        await self.transcoder.stop()
        async_dismiss(self.hass, f"hass_hjq_offline_{self.mac_id}")

    async def async_stream_source(self, *, force: bool = False) -> str | None:
        """Return a playable local H.264 URL, or the cloud URL if transcode is off."""
        if self.transcode and self._stream_status == "reconnecting" and not force:
            return None
        cloud = await self.async_cloud_url(force=force)
        if not cloud:
            return None
        if not self.transcode:
            return cloud
        try:
            return await self.transcoder.ensure(cloud, restart=force)
        except (TimeoutError, OSError, RuntimeError) as err:
            _LOGGER.warning("%s H.264 transcode failed: %s", self.name, err)
            self._set_status("reconnecting")
            return None

    async def async_cloud_url(self, *, force: bool = False) -> str | None:
        """Return a fresh-enough cloud live URL."""
        async with self._lock:
            now = datetime.now()
            stale = (
                self._cloud_url is None
                or self._cloud_url_at is None
                or now - self._cloud_url_at > timedelta(seconds=STREAM_STALE_SECONDS)
            )
            if force or stale:
                await self._refresh_cloud_url()
            return self._cloud_url

    async def async_snapshot(self) -> bytes | None:
        """Return a recent JPEG, or nothing while the stream is down.

        A stale JPEG is what made the Home app keep the frame from the moment
        the phone app took the account.
        """
        if self._stream_status != "live":
            return None
        if self._snapshot and time.monotonic() - self._snapshot_at < _SNAPSHOT_MAX_AGE:
            return self._snapshot
        await self._refresh_snapshot()
        return self._snapshot

    async def async_start_recording(self) -> None:
        """Start rolling MP4 recording."""
        source = await self.async_stream_source()
        if not source:
            raise RuntimeError(f"No live stream for {self.name}")
        segment = int(
            self.entry.options.get(CONF_RECORD_SEGMENT, DEFAULT_RECORD_SEGMENT)
        )
        audio = bool(self.entry.options.get(CONF_AUDIO, DEFAULT_AUDIO))
        directory = await self.recorder.start(source, segment, audio=audio)
        info = self.recording_info()
        notify(
            self.hass,
            (
                f"{self.name} 正在录像。\n"
                f"浏览器打开：{info['recording_browser_path']}\n"
                f"容器内目录：{directory}\n"
                f"{info['recording_host_hint']}"
            ),
            title="摄像头录像位置",
            notification_id=f"hass_hjq_record_{self.mac_id}",
        )
        self.push()

    async def async_stop_recording(self) -> None:
        """Stop rolling MP4 recording."""
        await self.recorder.stop()
        self.push()

    async def _refresh_cloud_url(self) -> None:
        meta = self._meta()
        base_url = meta.get("baseUrl") or ""
        jwt = meta.get("jwtoken") or ""
        if not base_url or not jwt:
            _LOGGER.error("%s missing baseUrl/jwtoken, cannot fetch live address", self.name)
            return
        try:
            payload = await self.api.get_live_addr(base_url, jwt, self.mac_id)
        except (HJQApiError, HJQAuthError) as err:
            _LOGGER.warning("%s getLiveAddress failed: %s", self.name, err)
            return
        url = self.api.pick_stream_url(payload)
        if not url:
            _LOGGER.warning("%s no http(s) live URL in payload: %s", self.name, payload)
            return
        changed = url != self._cloud_url
        self._cloud_url = url
        self._cloud_url_at = datetime.now()
        _LOGGER.info("%s live URL refreshed", self.name)
        if changed and self.transcoder.running:
            await self.transcoder.restart_if_running(url)

    async def _keep_loop(self) -> None:
        await asyncio.sleep(1)
        _LOGGER.info("%s keep-alive started", self.name)
        ticks = 0
        while not self._stop:
            try:
                ticks += 1
                await self._watch_stream(ticks)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s keep-alive iteration failed", self.name)
            await asyncio.sleep(5)

    async def _watch_stream(self, ticks: int) -> None:
        if not self.transcode:
            if self._cloud_url is None:
                async with self._lock:
                    await self._refresh_cloud_url()
            self._set_status("live" if self._cloud_url else "reconnecting")
            return

        health = self.transcoder.health()
        if health == "live":
            if self._stream_status != "live":
                self._set_status("live")
                self._live_since = time.monotonic()
                async_dismiss(self.hass, f"hass_hjq_offline_{self.mac_id}")
                _LOGGER.info("%s stream recovered", self.name)
            elif time.monotonic() - self._live_since > 15:
                self._failures = 0
            await self._ensure_motion()
            if ticks % 4 == 0:
                await self._keepalive()
            if ticks % 2 == 0:
                await self._refresh_snapshot()
            return

        if health == "starting":
            self._set_status("starting")
            if not self.transcoder.running:
                await self.async_stream_source()
            if self.transcoder.running or time.monotonic() - self._status_since < 20:
                return

        now = time.monotonic()
        if now < self._next_recover_at:
            self._set_status("reconnecting")
            return
        self._failures += 1
        self._next_recover_at = now + min(60, 5 * self._failures)
        await self._recover()

    async def _recover(self) -> None:
        action = recovery_action(self._failures)
        self._set_status("reconnecting")
        notify(
            self.hass,
            (
                f"{self.name} 掉线了，多半是手机上的移动爱家抢走了登录。"
                "正在重新登录并拉流，恢复前预览不会停在旧画面。"
            ),
            title="摄像头正在重新连接",
            notification_id=f"hass_hjq_offline_{self.mac_id}",
        )
        _LOGGER.warning("%s stream stalled, recovery=%s", self.name, action)
        if action == "reauth":
            try:
                await self.api.ensure_auth(force=True)
                await self.coordinator.async_request_refresh()
            except (HJQApiError, HJQAuthError) as err:
                _LOGGER.warning("%s re-login failed: %s", self.name, err)
                return
        async with self._lock:
            self._cloud_url = None
            await self._refresh_cloud_url()
        if not self._cloud_url:
            return
        try:
            await self.transcoder.ensure(self._cloud_url, restart=True)
        except (TimeoutError, OSError, RuntimeError) as err:
            _LOGGER.warning("%s restart after stall failed: %s", self.name, err)

    async def _keepalive(self) -> None:
        meta = self._meta()
        if meta.get("baseUrl") and meta.get("jwtoken"):
            kept = await self.api.keep_live_addr(
                meta["baseUrl"], meta["jwtoken"], self.mac_id
            )
            if not kept:
                self._failures = max(self._failures, 1)
                self._next_recover_at = 0

    async def _ensure_motion(self) -> None:
        url = self.transcoder.stream_url
        if not url or self._motion is not None:
            return
        self._motion = MotionDetector(url, self._on_motion)
        self._motion.start()

    async def _refresh_snapshot(self) -> None:
        url = self.transcoder.stream_url
        if not url or self.transcoder.health() != "live":
            return
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-fflags",
            "nobuffer",
            "-i",
            url,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            data, _ = await asyncio.wait_for(proc.communicate(), timeout=4)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return
        if data and data.startswith(b"\xff\xd8"):
            self._snapshot = data
            self._snapshot_at = time.monotonic()
