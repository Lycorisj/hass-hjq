"""Local low-latency MPEG-TS broadcast for HA and HomeKit.

HomeKit reads whatever ``stream_source()`` returns. A short HLS playlist
makes that reader stall or step one frame every few seconds. One ffmpeg
process writes MPEG-TS here; every viewer (live, snapshot, record, motion)
connects to the same local URL.
"""

from __future__ import annotations

import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)

_PREROLL_BYTES = 256 * 1024


def recovery_action(consecutive_failures: int) -> str:
    """First drop refreshes the cloud URL; further drops log in again."""
    if consecutive_failures <= 1:
        return "refresh_url"
    return "reauth"


class TsHttpServer:
    """Serve one live MPEG-TS to any number of local HTTP clients."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.clients: set[asyncio.Queue[bytes | None]] = set()
        self.preroll = bytearray()
        self.total_bytes = 0
        self.last_data_at: float | None = None

    @property
    def url(self) -> str | None:
        if not self.port:
            return None
        return f"http://127.0.0.1:{self.port}/live.ts"

    async def start(self) -> str:
        if self._server is not None and self.url:
            return self.url
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        assert self.url
        return self.url

    async def stop(self) -> None:
        for queue in list(self.clients):
            queue.put_nowait(None)
        self.clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.port = 0

    def publish(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        self.last_data_at = time.monotonic()
        self.preroll.extend(chunk)
        if len(self.preroll) > _PREROLL_BYTES:
            del self.preroll[: len(self.preroll) - _PREROLL_BYTES]
        dead: list[asyncio.Queue[bytes | None]] = []
        for queue in self.clients:
            if queue.full():
                dead.append(queue)
                continue
            queue.put_nowait(chunk)
        for queue in dead:
            self.clients.discard(queue)
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if not line or line in (b"\r\n", b"\n"):
                    break
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: video/mp2t\r\n"
                b"Cache-Control: no-cache, no-store\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            self.clients.add(queue)
            if self.preroll:
                writer.write(bytes(self.preroll))
            await writer.drain()
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                parts = [chunk]
                while True:
                    try:
                        nxt = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:
                        chunk = None
                        break
                    parts.append(nxt)
                writer.write(b"".join(parts))
                await writer.drain()
                if chunk is None:
                    break
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError, RuntimeError):
            return
        finally:
            self.clients.discard(queue)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                return


class MotionDetector:
    """Raise motion from the local H.264 stream, a few times a second."""

    def __init__(self, url: str, on_motion) -> None:
        self.url = url
        self._on_motion = on_motion
        self._stop = False
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def start(self) -> None:
        self._stop = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        await self._kill()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                return

    async def _run(self) -> None:
        while not self._stop:
            try:
                await self._once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Motion detector restarting", exc_info=True)
            await asyncio.sleep(2)

    async def _once(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-probesize",
            "32768",
            "-analyzeduration",
            "0",
            "-fflags",
            "nobuffer",
            "-i",
            self.url,
            "-an",
            "-vf",
            "fps=2,select='gt(scene,0.05)',metadata=print",
            "-f",
            "null",
            "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        assert proc.stderr is not None
        while not self._stop:
            line = await proc.stderr.readline()
            if not line:
                break
            if b"scene_score" in line:
                self._on_motion()
        await self._kill()

    async def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except TimeoutError:
            proc.kill()
            await proc.wait()
