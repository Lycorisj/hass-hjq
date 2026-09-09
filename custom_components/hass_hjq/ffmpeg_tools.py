"""FFmpeg helpers: H.264 transcode for HomeKit and local recording."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


class H264Transcoder:
    """Transcode a cloud H.265/FLV/HLS URL to a local H.264 HLS playlist.

    HomeKit only accepts H.264. The cloud cameras typically emit H.265, so a
    local ffmpeg process re-encodes on demand. The same playlist is used by
    Home Assistant live view and ``camera.record``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        camera_id: str,
        *,
        scale: str,
        audio: bool,
    ) -> None:
        """Initialize the transcoder."""
        self.hass = hass
        self.camera_id = camera_id
        self.scale = scale
        self.audio = audio
        self._proc: asyncio.subprocess.Process | None = None
        self._output_dir = Path("/tmp") / "hass_hjq" / camera_id  # noqa: S108
        self._playlist = self._output_dir / "index.m3u8"
        self._source: str | None = None
        self._lock = asyncio.Lock()

    @property
    def playlist_path(self) -> str:
        """Return the HLS playlist path."""
        return str(self._playlist)

    @property
    def running(self) -> bool:
        """Return whether ffmpeg is currently running."""
        return self._proc is not None and self._proc.returncode is None

    async def ensure(self, source: str) -> str:
        """Start (or keep) transcoding and return the playlist path."""
        async with self._lock:
            if source != self._source or not self.running:
                await self._restart(source)
            await self._wait_for_playlist()
            return self.playlist_path

    async def restart_if_running(self, source: str) -> None:
        """Restart ffmpeg with a fresh cloud URL when it is already active."""
        async with self._lock:
            self._source = source
            if not self.running:
                return
            await self._restart(source)

    async def stop(self) -> None:
        """Stop ffmpeg immediately."""
        async with self._lock:
            await self._kill()

    async def _restart(self, source: str) -> None:
        await self._kill()
        self._output_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self._output_dir.glob("*"):
            leftover.unlink(missing_ok=True)
        self._source = source
        cmd = self._build_cmd(source)
        _LOGGER.debug("Starting H.264 transcoder: %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
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
                "30",
                "-keyint_min",
                "30",
                "-bf",
                "0",
                "-b:v",
                "1500k",
                "-maxrate",
                "1500k",
                "-bufsize",
                "3000k",
            ]
        )
        if self.audio:
            cmd.extend(["-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "64k"])
        else:
            cmd.append("-an")
        cmd.extend(
            [
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "5",
                "-hls_flags",
                "delete_segments+append_list+independent_segments",
                "-hls_allow_cache",
                "0",
                self.playlist_path,
            ]
        )
        return cmd

    async def _wait_for_playlist(self, timeout: float = 12.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._playlist.exists() and self._playlist.stat().st_size > 0:
                return
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError("ffmpeg exited before producing a playlist")
            await asyncio.sleep(0.25)
        raise TimeoutError("Timed out waiting for H.264 playlist")

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
        path = Path(self.hass.config.path("www", "hass_hjq"))
        path.mkdir(parents=True, exist_ok=True)
        return path

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
        if source.endswith(".m3u8"):
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


def ffmpeg_available() -> bool:
    """Return True if an ffmpeg binary can be found."""
    return shutil.which("ffmpeg") is not None or shutil.which("avconv") is not None
