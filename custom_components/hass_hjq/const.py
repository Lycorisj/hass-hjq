"""Constants for the 移动爱家（原和家亲） integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "hass_hjq"
PLATFORMS = [Platform.CAMERA, Platform.SWITCH]

MANUFACTURER = "移动爱家"

# Options
CONF_TRANSCODE = "transcode_h264"
CONF_SCALE = "video_scale"
CONF_AUDIO = "support_audio"
CONF_RECORD_SEGMENT = "record_segment_seconds"

DEFAULT_TRANSCODE = True
DEFAULT_SCALE = "1280:720"
DEFAULT_AUDIO = True
DEFAULT_RECORD_SEGMENT = 300

SCALE_SOURCE = "source"
SCALE_720P = "1280:720"
SCALE_1080P = "1920:1080"

KEEPALIVE_INTERVAL = 20
STREAM_STALE_SECONDS = 90
AUTH_RETRY = 1

APP_NAME = "hejiaqin"
APP_VERSION = "6.11.1"
VIDEO_SIGN_SECRET = "r8rw4d1kjwqgqqto9dwsq3ew0ip2np1b"
