"""Local MPEG-TS broker and session-recovery decisions."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "hass_hjq"
    / "stream_broker.py"
)
_SPEC = importlib.util.spec_from_file_location("stream_broker_under_test", _PATH)
assert _SPEC and _SPEC.loader
_BROKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BROKER)
TsHttpServer = _BROKER.TsHttpServer
recovery_action = _BROKER.recovery_action


class RecoveryActionTest(unittest.TestCase):
    def test_first_failure_refreshes_url(self) -> None:
        self.assertEqual(recovery_action(1), "refresh_url")

    def test_later_failures_reauth(self) -> None:
        self.assertEqual(recovery_action(2), "reauth")
        self.assertEqual(recovery_action(5), "reauth")


class TsHttpServerTest(unittest.TestCase):
    def test_client_receives_published_bytes(self) -> None:
        async def scenario() -> None:
            server = TsHttpServer()
            url = await server.start()
            try:
                server.publish(b"\x47" * 188)
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(b"GET /live.ts HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                await writer.drain()
                data = await asyncio.wait_for(reader.read(4096), timeout=2)
                self.assertIn(b"video/mp2t", data)
                self.assertIn(b"\x47" * 188, data)
                server.publish(b"\x47more")
                more = await asyncio.wait_for(reader.read(64), timeout=2)
                self.assertIn(b"\x47more", more)
                writer.close()
                await writer.wait_closed()
                self.assertTrue(url.endswith("/live.ts"))
            finally:
                await server.stop()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
