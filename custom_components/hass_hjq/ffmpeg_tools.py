"""FFmpeg helpers: H.264 transcode for HomeKit and local recording."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil
import time

from homeassistant.core import HomeAssistant

from .stream_broker import TsHttpServer

_LOGGER = logging.getLogger(__name__)

_STARTUP_GRACE_SECONDS = 12.0
_STALL_SECONDS = 8.0


def _ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


class H264Transcoder:
    """Transcode a cloud H.265 URL to a local low-latency H.264 MPEG-TS.

    HomeKit only accepts H.264. One ffmpeg process re-encodes the cloud
    stream and the result is served over HTTP so Home Assistant, HomeKit,
    snapshots, and recording all share it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        camera_id: str,
        *,
        scale: str,
        audio: bool,
        bitrate: str = "1500k",
    ) -> None:
        """Initialize the transcoder."""
        self.hass = hass
        self.camera_id = camera_id
        self.scale = scale
        self.audio = audio
        self.bitrate = bitrate or "1500k"
        self._proc: asyncio.subprocess.Process | None = None
        self._pump: asyncio.Task | None = None
        self._server = TsHttpServer()
        self._source: str | None = None
        self._lock = asyncio.Lock()
        self._started_at = 0.0

    @property
    def stream_url(self) -> str | None:
        """Return the local MPEG-TS URL, once the server is listening."""
        return self._server.url

    @property
    def playlist_path(self) -> str:
        """Backward-compatible name for the local stream URL."""
        return self.stream_url or ""

    @property
    def running(self) -> bool:
        """Return whether ffmpeg is currently running."""
        return self._proc is not None and self._proc.returncode is None

    def health(self) -> str:
        """Return starting, live, or stalled."""
        now = time.monotonic()
        if self._started_at == 0 or not self.running:
            if self._started_at == 0 or now - self._started_at < _STARTUP_GRACE_SECONDS:
                return "starting"
            return "stalled"
        last = self._server.last_data_at
        if last is None:
            if now - self._started_at < _STARTUP_GRACE_SECONDS:
                return "starting"
            return "stalled"
        if now - last > _STALL_SECONDS:
            return "stalled"
        return "live"

    async def prepare(self) -> str:
        """Open the local stream server and return its URL."""
        async with self._lock:
            return await self._server.start()

    async def ensure(self, source: str, *, restart: bool = False) -> str:
        """Start (or keep) transcoding and return the local stream URL."""
        async with self._lock:
            await self._server.start()
            if restart or source != self._source or not self.running:
                await self._restart(source)
            await self._wait_for_bytes()
            assert self.stream_url
            return self.stream_url

    async def restart_if_running(self, source: str) -> None:
        """Restart ffmpeg with a fresh cloud URL when it is already active."""
        async with self._lock:
            self._source = source
            if not self.running:
                return
            await self._server.start()
            await self._restart(source)

    async def stop(self) -> None:
        """Stop ffmpeg and the local server."""
        async with self._lock:
            await self._kill()
            await self._server.stop()

    async def _restart(self, source: str) -> None:
        await self._kill()
        self._server.preroll.clear()
        self._server.total_bytes = 0
        self._server.last_data_at = None
        self._source = source
        self._started_at = time.monotonic()
        cmd = self._build_cmd(source)
        _LOGGER.debug("Starting H.264 transcoder: %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._pump = asyncio.create_task(self._pump_stdout())
        self.hass.async_create_background_task(
            self._watch_stderr(),
            name=f"hass_hjq_ffmpeg_{self.camera_id}",
        )

    def _build_cmd(self, source: str) -> list[str]:
        cmd: list[str] = [
            _ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt+nobuffer",
            "-flags",
            "low_delay",
            "-rw_timeout",
            "15000000",
            "-user_agent",
            "HassHjq/0.2",
            "-i",
            source,
        ]
        if self.scale and self.scale != "source":
            width, _, height = self.scale.partition(":")
            cmd.extend(
                [
                    "-vf",
                    (
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                    ),
                ]
            )
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-profile:v",
                "baseline",
                "-level",
                "3.1",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "15",
                "-keyint_min",
                "15",
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-b:v",
                self.bitrate,
                "-maxrate",
                self.bitrate,
                "-bufsize",
                self.bitrate,
            ]
        )
        if self.audio:
            cmd.extend(["-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "64k"])
        else:
            cmd.append("-an")
        cmd.extend(
            [
                "-f",
                "mpegts",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                "-flush_packets",
                "1",
                "pipe:1",
            ]
        )
        return cmd

    async def _wait_for_bytes(self, timeout: float = 12.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._server.total_bytes > 0 and self.health() == "live":
                return
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError("ffmpeg exited before producing video")
            await asyncio.sleep(0.25)
        if self._server.total_bytes <= 0:
            raise TimeoutError("Timed out waiting for H.264 stream")

    async def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        buf = bytearray()
        last = time.monotonic()
        try:
            while True:
                chunk = await proc.stdout.read(32 * 1024)
                if not chunk:
                    if buf:
                        self._server.publish(bytes(buf))
                    break
                buf.extend(chunk)
                now = time.monotonic()
                if len(buf) >= 16 * 1024 or now - last >= 0.03:
                    self._server.publish(bytes(buf))
                    buf.clear()
                    last = now
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Transcoder pump ended for %s", self.camera_id, exc_info=True)

    async def _watch_stderr(self) -> None:
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
                    _LOGGER.debug("ffmpeg[%s]: %s", self.camera_id, text)
        except Exception:  # noqa: BLE001
            return

    async def _kill(self) -> None:
        pump = self._pump
        self._pump = None
        proc = self._proc
        self._proc = None
        if pump is not None and not pump.done():
            pump.cancel()
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()


class SegmentRecorder:
    """Write rolling MP4 clips from a live source."""

    def __init__(self, hass: HomeAssistant, camera_id: str, camera_name: str) -> None:
        """Initialize the recorder."""
        self.hass = hass
        self.camera_id = camera_id
        self.camera_name = camera_name
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def recording(self) -> bool:
        """Return whether a recording process is running."""
        return self._proc is not None and self._proc.returncode is None

    def output_dir(self) -> Path:
        """Return the directory used for recordings."""
        return Path(recording_location(self.hass)["recording_directory"])

    async def start(
        self, source: str, segment_seconds: int, *, audio: bool = True
    ) -> Path:
        """Start segmented recording. Returns the output directory."""
        if self.recording:
            return self.output_dir()
        out_dir = self.output_dir()
        safe_name = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in self.camera_name
        )
        pattern = str(out_dir / f"{safe_name}_%Y%m%d_%H%M%S.mp4")
        cmd: list[str] = [
            _ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt",
            "-rw_timeout",
            "15000000",
            "-i",
            source,
        ]
        if _is_local_h264(source):
            cmd.extend(["-c:v", "copy"])
            if audio:
                cmd.extend(["-c:a", "copy", "-bsf:a", "aac_adtstoasc"])
            else:
                cmd.append("-an")
        else:
            cmd.extend(
                ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
            )
            if audio:
                cmd.extend(["-c:a", "aac"])
            else:
                cmd.append("-an")
        cmd.extend(
            [
                "-f",
                "segment",
                "-segment_time",
                str(max(30, int(segment_seconds))),
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                pattern,
            ]
        )
        _LOGGER.info("Start recording %s -> %s", self.camera_name, out_dir)
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        return out_dir

    async def stop(self) -> None:
        """Stop recording."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=8)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        _LOGGER.info("Stopped recording %s", self.camera_name)


def recording_location(hass: HomeAssistant) -> dict[str, str]:
    """Return where MP4 files are written, in container and browser terms."""
    directory = Path(hass.config.path("www", "hass_hjq"))
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "recording_directory": str(directory),
        "recording_browser_path": "/local/hass_hjq/",
        "recording_host_hint": (
            "Docker 宿主机上，打开挂载到容器 /config 的那个目录，再进 www/hass_hjq。"
        ),
    }


def recent_recordings(directory: str, limit: int = 5) -> list[str]:
    """Return the newest MP4 file names in a recording directory."""
    path = Path(directory)
    if not path.is_dir():
        return []
    files = sorted(path.glob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
    return [item.name for item in files[:limit]]


def _is_local_h264(source: str) -> bool:
    return (
        source.endswith(".m3u8")
        or source.endswith("/live.ts")
        or "127.0.0.1" in source
    )


def ffmpeg_available() -> bool:
    """Return True if an ffmpeg binary can be found."""
    return shutil.which("ffmpeg") is not None or shutil.which("avconv") is not None


def _bufsize_for(bitrate: str) -> str:
    """Use a 2x VBV buffer from a bitrate like ``1500k``."""
    raw = bitrate.strip().lower()
    if raw.endswith("k") and raw[:-1].isdigit():
        return f"{int(raw[:-1]) * 2}k"
    if raw.endswith("m") and raw[:-1].isdigit():
        return f"{int(raw[:-1]) * 2}M"
    return "3000k"
