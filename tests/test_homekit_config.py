"""HomeKit Secure Video config keeps pins and pairings."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "hass_hjq"
    / "homekit_config.py"
)
_SPEC = importlib.util.spec_from_file_location("homekit_config_under_test", _PATH)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["homekit_config_under_test"] = _MOD
_SPEC.loader.exec_module(_MOD)


class HomeKitConfigTest(unittest.TestCase):
    def test_pin_is_stable_and_safe(self) -> None:
        first = _MOD.pin_for("9327982000090377")
        self.assertEqual(first, _MOD.pin_for("9327982000090377"))
        self.assertEqual(len(first), 8)
        self.assertNotIn(first, _MOD._INSECURE_PINS)
        self.assertEqual(_MOD.format_pin(first), f"{first[:3]}-{first[3:5]}-{first[5:]}")

    def test_pairings_round_trip(self) -> None:
        camera = _MOD.HomeKitCamera("abc", "客厅", "http://127.0.0.1:9/live.ts")
        text = _MOD.render_config([camera], {})
        self.assertIn("hksv: true", text)
        self.assertIn("motion: api", text)
        self.assertIn("speaker: true", text)
        self.assertIn("http://127.0.0.1:9/live.ts", text)
        saved = _MOD.saved_identities(text)
        saved[camera.stream_id]["pairings"] = ["client_id=abc&public=xyz"]
        again = _MOD.render_config([camera], saved)
        restored = _MOD.saved_identities(again)
        self.assertEqual(restored[camera.stream_id]["pin"], saved[camera.stream_id]["pin"])
        self.assertEqual(
            restored[camera.stream_id]["pairings"],
            ["client_id=abc&public=xyz"],
        )


if __name__ == "__main__":
    unittest.main()
