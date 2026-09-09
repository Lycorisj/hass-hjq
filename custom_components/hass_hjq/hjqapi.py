"""HTTP client for 移动爱家 / 和家亲 camera APIs."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
from typing import Any

import aiohttp

from .const import APP_NAME, APP_VERSION, AUTH_RETRY, VIDEO_SIGN_SECRET

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://base.hjq.komect.com/base/user/passwdLogin"
VIDEO_LOGIN_URL = "https://video.komect.com/user/login/loginByHJQToken"
CAMERA_LIST_URL = "https://video.komect.com/camera/core/api/bind/queryList"

_BASE_URL_KEYS = (
    "baseUrl",
    "base_url",
    "baseURL",
    "dcsUrl",
    "dcs_url",
    "serverUrl",
    "server_url",
)
_JWT_KEYS = (
    "jwtoken",
    "jwToken",
    "jwt",
    "jw_token",
    "AuthorizationJwtoken",
    "authorizationJwtoken",
)
_MAC_KEYS = ("mac_id", "macId", "macid")
_NAME_KEYS = ("mac_name", "macName", "name", "deviceName", "device_name")

TOKEN_EXPIRED_MARKERS = (
    "USER_TOKEN_OUTOFDATE",
    "TOKEN_OUTOFDATE",
    "token out of date",
    "未登录",
    "登录过期",
)


class HJQApiError(Exception):
    """Base error for the HeJiaQin API."""


class HJQAuthError(HJQApiError):
    """Authentication failed or session expired."""


class HJQApi:
    """Async client for HeJiaQin video APIs.

    One instance per config entry. Tokens are refreshed on expiry instead of
    being cached forever (the original singleton + cached auth caused cameras
    to stay unavailable until Home Assistant was restarted).
    """

    def __init__(self, tel: str, pwd: str, session: aiohttp.ClientSession) -> None:
        """Initialize the API client."""
        self.tel = tel
        self.pwd = pwd
        self._session = session
        self.hjq_token: str | None = None
        self.pass_id: str | None = None
        self.auth: str | None = None
        self.device_id = hashlib.md5(f"hass_hjq_{tel}".encode()).hexdigest()[:16]

    def _video_headers(self, jwt: str | None = None) -> dict[str, str]:
        headers = {
            "AppName": APP_NAME,
            "DeviceId": self.device_id,
            "Version": APP_VERSION,
            "DeviceType": "ANDROID",
        }
        if self.auth:
            headers["AuthorizationToken"] = self.auth
        if jwt:
            headers["AuthorizationJwtoken"] = jwt
        return headers

    async def ensure_auth(self, *, force: bool = False) -> str:
        """Return a valid video auth token, logging in again if needed."""
        if force:
            self.auth = None
            self.hjq_token = None
            self.pass_id = None
        if not self.hjq_token or not self.pass_id:
            await self.get_hjqtoken_passid()
        if not self.auth:
            await self.get_video_auth()
        if not self.auth:
            raise HJQAuthError("Unable to obtain video auth token")
        return self.auth

    async def get_hjqtoken_passid(self) -> tuple[str, str]:
        """Log in with phone + password and store session cookies."""
        body = json.dumps(
            {
                "virtualAuthdata": self.get_md5(self.pwd),
                "authType": "10",
                "userAccount": self.tel,
                "authdata": self.get_sha1("fetion.com.cn:" + self.pwd),
            }
        )
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION} (Linux; Android 13; HomeAssistant)",
        }
        async with self._session.post(LOGIN_URL, data=body, headers=headers) as resp:
            text = await resp.text()
            _LOGGER.debug("passwdLogin status=%s body=%s", resp.status, text[:500])
            if resp.status >= 400:
                raise HJQAuthError(f"Login HTTP {resp.status}")
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError as err:
                raise HJQAuthError("Login returned non-JSON") from err
            token = self._cookie_value(resp, "HJQToken") or self._cookie_value(
                resp, "hjqToken"
            )
            if not token and "Set-Cookie" in resp.headers:
                # Fallback for servers that only emit a raw Set-Cookie header.
                try:
                    token = resp.headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
                except (IndexError, AttributeError):
                    token = ""

        data = payload.get("data") or {}
        pass_id = str(data.get("passId") or data.get("pass_id") or "")
        if not token or not pass_id:
            msg = payload.get("msg") or payload.get("message") or "invalid credentials"
            raise HJQAuthError(str(msg))

        self.hjq_token = token
        self.pass_id = pass_id
        _LOGGER.debug("Logged in, pass_id obtained")
        return token, pass_id

    async def get_video_auth(self) -> str:
        """Exchange the HeJiaQin token for a video-platform token."""
        if not self.hjq_token or not self.pass_id:
            await self.get_hjqtoken_passid()

        ts = str(int(time.time() * 1000))
        body = {
            "HJQToken": self.hjq_token or "",
            "nonce": ts + "abcde",
            "passId": self.pass_id or "",
            "time": ts,
            "userId": self.tel,
        }
        body["sign"] = self.get_video_sign(body, VIDEO_LOGIN_URL)
        headers = {
            **self._video_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        # Video login should not send a stale AuthorizationToken.
        headers.pop("AuthorizationToken", None)

        async with self._session.post(
            VIDEO_LOGIN_URL,
            data=urllib.parse.urlencode(body),
            headers=headers,
        ) as resp:
            payload = await self._read_json(resp)
            _LOGGER.debug("loginByHJQToken: %s", payload)

        data = payload.get("data") or {}
        token = data.get("token")
        if not token:
            raise HJQAuthError(payload.get("msg") or "video auth failed")
        self.auth = str(token)
        _LOGGER.info("Video platform login succeeded for %s", self._masked_tel())
        return self.auth

    async def get_camera_info(self) -> list[dict[str, Any]]:
        """Return cameras bound to (or shared with) this account."""
        ts = str(int(time.time() * 1000))
        params = {
            "nonce": ts + "m5kjt",
            "number": "100",
            "page": "1",
            "time": ts,
            "user_id": self.tel,
        }
        payload = await self._signed_get(CAMERA_LIST_URL, params)
        cameras = self._extract_camera_list(payload.get("data"))
        _LOGGER.debug(
            "queryList returned %s camera dicts",
            len(cameras),
        )
        return cameras

    @staticmethod
    def _extract_camera_list(data: Any) -> list[dict[str, Any]]:
        """Pick the camera array out of queryList's variously shaped payloads."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []

        preferred_keys = (
            "bindList",
            "cameras",
            "records",
            "shareList",
            "sharedList",
            "list",
        )
        candidates: list[list[dict[str, Any]]] = []
        for key in preferred_keys:
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                candidates.append(value)
        for value in data.values():
            if (
                isinstance(value, list)
                and value
                and isinstance(value[0], dict)
                and value not in candidates
            ):
                candidates.append(value)

        def _score(items: list[dict[str, Any]]) -> int:
            score = 0
            for item in items:
                if item.get("mac_id") or item.get("macId"):
                    score += 1
                if HJQApi._nested_str(item, _BASE_URL_KEYS):
                    score += 5
                if HJQApi._nested_str(item, _JWT_KEYS):
                    score += 5
            return score

        if candidates:
            candidates.sort(key=_score, reverse=True)
            return [item for item in candidates[0] if isinstance(item, dict)]
        # Some payloads are {macId: {camera fields}, ...}.
        nested = [v for v in data.values() if isinstance(v, dict)]
        return [item for item in nested if item.get("mac_id") or item.get("macId")]

    @staticmethod
    def _nested_str(obj: Any, keys: tuple[str, ...]) -> str:
        """Return the first non-empty string for keys, including nested dicts."""
        if isinstance(obj, dict):
            for key in keys:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in obj.values():
                found = HJQApi._nested_str(value, keys)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = HJQApi._nested_str(item, keys)
                if found:
                    return found
        return ""

    async def get_live_addr(
        self, base_url: str, jwt: str, mac_id: str
    ) -> dict[str, Any] | None:
        """Fetch live-stream descriptors (HLS / FLV / RTMP) for a camera."""
        api = base_url.rstrip("/") + "/dcs/device/getLiveAddress"
        ts = str(int(time.time() * 1000))
        params = {
            "macId": mac_id,
            "nonce": ts + "gs08t",
            "requestTime": ts,
            "time": ts,
        }
        payload = await self._signed_get(api, params, jwt=jwt)
        data = payload.get("data")
        if not data:
            _LOGGER.warning("getLiveAddress returned no data for %s: %s", mac_id, payload)
            return None
        if isinstance(data, str):
            return {"url": data}
        return data

    async def keep_live_addr(self, base_url: str, jwt: str, mac_id: str) -> bool:
        """Ping the cloud so the temporary live URL stays open."""
        api = base_url.rstrip("/") + "/dcs/device/keepOpenLiveAddress"
        ts = str(int(time.time() * 1000))
        params = {
            "macId": mac_id,
            "nonce": ts + "gs08t",
            "time": ts,
        }
        params["sign"] = self.get_video_sign(params, api)
        headers = {
            **self._video_headers(jwt),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with self._session.post(
                api,
                data=urllib.parse.urlencode(params),
                headers=headers,
            ) as resp:
                payload = await self._read_json(resp)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("keepOpenLiveAddress failed for %s: %s", mac_id, err)
            return False
        if self._token_expired(payload):
            await self.ensure_auth(force=True)
            return False
        return True

    async def get_device_info(
        self, base_url: str, jwt: str, mac_id: str
    ) -> dict[str, Any]:
        """Return device metadata; empty dict when the endpoint is missing."""
        api = base_url.rstrip("/") + "/dcs/device/fullInfo"
        ts = str(int(time.time() * 1000))
        params = {
            "macId": mac_id,
            "nonce": ts + "gs08t",
            "time": ts,
        }
        try:
            payload = await self._signed_get(api, params, jwt=jwt)
        except HJQApiError as err:
            _LOGGER.debug("fullInfo unavailable for %s: %s", mac_id, err)
            return {}
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def _signed_get(
        self,
        url: str,
        params: dict[str, str],
        *,
        jwt: str | None = None,
    ) -> dict[str, Any]:
        """GET with app signature and a single re-auth retry on expiry."""
        last_payload: dict[str, Any] = {}
        for attempt in range(AUTH_RETRY + 1):
            await self.ensure_auth(force=attempt > 0)
            query = dict(params)
            query["sign"] = self.get_video_sign(query, url)
            try:
                async with self._session.get(
                    url, params=query, headers=self._video_headers(jwt)
                ) as resp:
                    payload = await self._read_json(resp)
            except (TimeoutError, aiohttp.ClientError) as err:
                raise HJQApiError(str(err)) from err
            last_payload = payload
            if self._token_expired(payload):
                _LOGGER.info("Auth token expired, re-login (attempt %s)", attempt + 1)
                self.auth = None
                continue
            return payload
        raise HJQAuthError(last_payload.get("msg") or "token expired")

    async def _read_json(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        text = await resp.text()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            _LOGGER.debug("Non-JSON response from %s: %s", resp.url, text[:300])
            return {"msg": text, "code": resp.status}
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _cookie_value(resp: aiohttp.ClientResponse, name: str) -> str:
        morsel = resp.cookies.get(name)
        if morsel is None:
            return ""
        return morsel.value if hasattr(morsel, "value") else str(morsel)

    @staticmethod
    def _token_expired(payload: dict[str, Any]) -> bool:
        msg = str(payload.get("msg") or payload.get("message") or "")
        code = str(payload.get("code") or payload.get("status") or "")
        combined = f"{msg} {code}".upper()
        return any(marker.upper() in combined for marker in TOKEN_EXPIRED_MARKERS)

    def _masked_tel(self) -> str:
        tel = self.tel
        if len(tel) >= 7:
            return f"{tel[:3]}****{tel[-4:]}"
        return "***"

    @staticmethod
    def pick_stream_url(data: dict[str, Any] | None) -> str | None:
        """Choose the best playable URL from a getLiveAddress payload.

        HLS is preferred because Home Assistant's stream component and ffmpeg
        both handle it more reliably than HTTP-FLV for long sessions.
        """
        if not data:
            return None

        ranked: list[tuple[int, str]] = []

        def _walk(value: Any, key: str = "") -> None:
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                hint = key.lower()
                if "hls" in hint or value.endswith(".m3u8"):
                    priority = 0
                elif "flv" in hint or value.endswith(".flv"):
                    priority = 1
                elif "rtmp" in hint:
                    priority = 3
                else:
                    priority = 2
                ranked.append((priority, value))
            elif isinstance(value, dict):
                for child_key, child in value.items():
                    _walk(child, str(child_key))
            elif isinstance(value, list):
                for child in value:
                    _walk(child, key)

        _walk(data)
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    @staticmethod
    def get_md5(value: str) -> str:
        """Return hex MD5 of a string."""
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    @staticmethod
    def get_sha1(value: str) -> str:
        """Return hex SHA1 of a string."""
        return hashlib.sha1(value.encode("utf-8")).hexdigest()

    @staticmethod
    def get_video_sign(body: dict[str, str], api: str) -> str:
        """Compute the video API request signature."""
        parts = "".join(k + str(body[k]) for k in sorted(body))
        path = urllib.parse.urlparse(api).path
        return HJQApi.get_md5(parts + path + VIDEO_SIGN_SECRET)
