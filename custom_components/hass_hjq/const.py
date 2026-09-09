"""Constants for the 移动爱家（原和家亲） integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "hass_hjq"
PLATFORMS = [Platform.CAMERA, Platform.SWITCH]

MANUFACTURER = "移动爱家"

# Options
CONF_TRANSCODE = "transcode_h264"
CONF_SCALE = "video_scale"
CONF_BITRATE = "video_bitrate"
CONF_AUDIO = "support_audio"
CONF_RECORD_SEGMENT = "record_segment_seconds"

DEFAULT_TRANSCODE = True
DEFAULT_SCALE = "1280:720"
DEFAULT_BITRATE = "1500k"
DEFAULT_AUDIO = True
DEFAULT_RECORD_SEGMENT = 300

SCALE_SOURCE = "source"
SCALE_720P = "1280:720"
SCALE_1080P = "1920:1080"
BITRATE_800K = "800k"
BITRATE_1500K = "1500k"
BITRATE_2500K = "2500k"

# Output codec profile used by the local transcoder (HomeKit-friendly).
OUTPUT_VIDEO_CODEC = "h264"
OUTPUT_VIDEO_PROFILE = "constrained_baseline"
OUTPUT_VIDEO_LEVEL = "3.1"
OUTPUT_AUDIO_CODEC = "aac"
OUTPUT_AUDIO_RATE = 16000
OUTPUT_AUDIO_CHANNELS = 1
OUTPUT_AUDIO_BITRATE = "64k"
OUTPUT_CONTAINER_LIVE = "hls"
OUTPUT_CONTAINER_RECORD = "mp4"

KEEPALIVE_INTERVAL = 20
STREAM_STALE_SECONDS = 90
AUTH_RETRY = 1

APP_NAME = "hejiaqin"
APP_VERSION = "6.11.1"
VIDEO_SIGN_SECRET = "r8rw4d1kjwqgqqto9dwsq3ew0ip2np1b"
