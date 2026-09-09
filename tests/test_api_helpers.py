"""Tests for stream URL selection and request signing (no Home Assistant runtime)."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_PATH = ROOT / "custom_components" / "hass_hjq"


def _stub_homeassistant() -> None:
    if "homeassistant.const" in sys.modules:
        return
    homeassistant = types.ModuleType("homeassistant")
    const_mod = types.ModuleType("homeassistant.const")

    class _Platform:
        CAMERA = "camera"
        SWITCH = "switch"

    const_mod.Platform = _Platform
    homeassistant.const = const_mod
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.const"] = const_mod
    sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))


def _load_module(fullname: str, path: Path):
    spec = importlib.util.spec_from_file_location(fullname, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


def _stub_homeassistant_core() -> None:
    _stub_homeassistant()
    if "homeassistant.core" in sys.modules:
        return
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.core"] = core


_stub_homeassistant_core()
pkg = types.ModuleType("hass_hjq")
pkg.__path__ = [str(PKG_PATH)]
pkg.__package__ = "hass_hjq"
sys.modules["hass_hjq"] = pkg
_load_module("hass_hjq.const", PKG_PATH / "const.py")
hjqapi = _load_module("hass_hjq.hjqapi", PKG_PATH / "hjqapi.py")
ffmpeg_tools = _load_module("hass_hjq.ffmpeg_tools", PKG_PATH / "ffmpeg_tools.py")
HJQApi = hjqapi.HJQApi


class PickStreamUrlTest(unittest.TestCase):
    def test_prefers_hls_over_flv(self) -> None:
        url = HJQApi.pick_stream_url(
            {
                "flv": "https://cdn.example/live.flv",
                "hls": "https://cdn.example/live.m3u8",
                "rtmp": "rtmp://cdn.example/live",
            }
        )
        self.assertEqual(url, "https://cdn.example/live.m3u8")

    def test_nested_payload(self) -> None:
        url = HJQApi.pick_stream_url(
            {"data": {"liveAddress": {"flvUrl": "https://cdn.example/a.flv"}}}
        )
        self.assertEqual(url, "https://cdn.example/a.flv")

    def test_empty(self) -> None:
        self.assertIsNone(HJQApi.pick_stream_url(None))
        self.assertIsNone(HJQApi.pick_stream_url({}))


class SignTest(unittest.TestCase):
    def test_sign_is_stable(self) -> None:
        body = {"macId": "abc", "nonce": "1", "time": "2"}
        first = HJQApi.get_video_sign(
            body, "https://example.com/dcs/device/getLiveAddress"
        )
        second = HJQApi.get_video_sign(
            body, "https://example.com/dcs/device/getLiveAddress"
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)


class BufsizeTest(unittest.TestCase):
    def test_bufsize_is_twice_bitrate(self) -> None:
        self.assertEqual(ffmpeg_tools._bufsize_for("800k"), "1600k")
        self.assertEqual(ffmpeg_tools._bufsize_for("1500k"), "3000k")
        self.assertEqual(ffmpeg_tools._bufsize_for("2500k"), "5000k")
        self.assertEqual(ffmpeg_tools._bufsize_for("2m"), "4M")
        self.assertEqual(ffmpeg_tools._bufsize_for("weird"), "3000k")


if __name__ == "__main__":
    unittest.main()
