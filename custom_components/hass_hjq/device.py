"""Per-camera runtime: live URL, keep-alive, transcode, recording."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
from .ffmpeg_tools import H264Transcoder, SegmentRecorder, ffmpeg_available
from .hjqapi import HJQApiError, HJQAuthError

_LOGGER = logging.getLogger(__name__)


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
        self.transcoder = H264Transcoder(
            hass,
            self.mac_id,
            scale=entry.options.get(CONF_SCALE, DEFAULT_SCALE),
            audio=entry.options.get(CONF_AUDIO, DEFAULT_AUDIO),
            bitrate=entry.options.get(CONF_BITRATE, DEFAULT_BITRATE),
        )
        self.recorder = SegmentRecorder(hass, self.mac_id, self.name)

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

    async def async_start(self) -> None:
        """Start the keep-alive loop."""
        self._stop = False
        if self.entry.options.get(CONF_TRANSCODE, DEFAULT_TRANSCODE) and not ffmpeg_available():
            _LOGGER.warning(
                "%s: ffmpeg not found, HomeKit H.264 transcode is disabled",
                self.name,
            )
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
        await self.recorder.stop()
        await self.transcoder.stop()

    async def async_stream_source(self, *, force: bool = False) -> str | None:
        """Return a playable URL or local H.264 playlist path."""
        cloud = await self.async_cloud_url(force=force)
        if not cloud:
            return None
        if not self.transcode:
            return cloud
        try:
            return await self.transcoder.ensure(cloud)
        except (TimeoutError, OSError, RuntimeError) as err:
            _LOGGER.warning(
                "%s H.264 transcode failed (%s); falling back to cloud URL",
                self.name,
                err,
            )
            return cloud

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

    async def async_start_recording(self) -> None:
        """Start rolling MP4 recording."""
        source = await self.async_stream_source()
        if not source:
            raise RuntimeError(f"No live stream for {self.name}")
        segment = int(
            self.entry.options.get(CONF_RECORD_SEGMENT, DEFAULT_RECORD_SEGMENT)
        )
        audio = bool(self.entry.options.get(CONF_AUDIO, DEFAULT_AUDIO))
        await self.recorder.start(source, segment, audio=audio)

    async def async_stop_recording(self) -> None:
        """Stop rolling MP4 recording."""
        await self.recorder.stop()

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
        await asyncio.sleep(3)
        _LOGGER.info("%s keep-alive started", self.name)
        session = async_get_clientsession(self.hass)
        ticks = 0
        while not self._stop:
            try:
                meta = self._meta()
                ticks += 1
                if self._cloud_url is None:
                    async with self._lock:
                        await self._refresh_cloud_url()
                elif ticks % 3 == 0 and not await self._url_ok(session, self._cloud_url):
                    _LOGGER.warning("%s live URL invalid, refreshing", self.name)
                    async with self._lock:
                        await self._refresh_cloud_url()
                if meta.get("baseUrl") and meta.get("jwtoken"):
                    await self.api.keep_live_addr(
                        meta["baseUrl"], meta["jwtoken"], self.mac_id
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s keep-alive iteration failed", self.name)
            await asyncio.sleep(KEEPALIVE_INTERVAL)

    async def _url_ok(self, session: aiohttp.ClientSession, url: str) -> bool:
        if not url.startswith("http"):
            return True
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status >= 400:
                    return False
                await resp.content.read(256)
                return True
        except (TimeoutError, aiohttp.ClientError):
            return False
