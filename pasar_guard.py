# pasar_guard.py

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from urllib.parse import quote, urlparse

import aiohttp

# =========================================================
# LOGGING
# =========================================================

logger = logging.getLogger(__name__)


# =========================================================
# CONSTANTS
# =========================================================

REQUEST_TIMEOUT = 60
REQUEST_CONNECT_TIMEOUT = 15

BYTES_PER_GB = 1024 ** 3

MAX_RESPONSE_SIZE = 5 * 1024 * 1024

MIN_VOLUME_GB = 0.01
MAX_VOLUME_GB = 100_000

# Circuit breaker سراسری
GLOBAL_CB_FAILURE_THRESHOLD = 15
GLOBAL_CB_RECOVERY_TIME = 120

# Circuit breaker per-panel
CB_FAILURE_THRESHOLD = 5
CB_RECOVERY_TIME = 60

CONFIG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
_LOG_CONTROL_RE = re.compile(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PANEL_NAME_CONTROL_RE = _LOG_CONTROL_RE

_SENSITIVE_WORDS = (
    "authorization", "bearer", "access_token",
    "password", "passwd", "token", "secret",
    "api_key", "apikey", "jwt", "session",
)


# =========================================================
# ERROR
# =========================================================

class PasarGuardError(Exception):
    def __init__(self, message: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class PasarGuardCircuitOpen(PasarGuardError):
    pass


# =========================================================
# PANEL CONFIG (جدید — جایگزین PASARGUARD_1/2/3 در config.py)
# =========================================================

@dataclass
class PanelConfig:
    """
    مشخصات یک پنل PasarGuard که ادمین از داخل پنل ادمین وارد و در دیتابیس
    ذخیره کرده است. تعداد پنل‌ها دیگر ثابت (۱،۲،۳) نیست؛ هرچقدر ادمین
    اضافه کند، همان‌قدر ردیف در دیتابیس و همان‌قدر PanelConfig وجود دارد.
    """
    id: int
    name: str          # اسم دلخواه ادمین، فقط برای نمایش و سازمان‌دهی
    url: str
    username: str
    password: str
    group_id: Optional[int] = None


def _validate_panel_name(name: Any) -> str:
    if not name:
        raise PasarGuardError("Panel name is required.")
    try:
        name = str(name).strip()
    except Exception:
        raise PasarGuardError("Invalid panel name.")
    name = _PANEL_NAME_CONTROL_RE.sub(" ", name)
    if not name or len(name) > 50:
        raise PasarGuardError("Panel name must be 1-50 characters.")
    return name


# =========================================================
# GLOBAL CIRCUIT BREAKER
# =========================================================

class GlobalCircuitBreaker:
    def __init__(self):
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    async def check(self):
        async with self._lock:
            if self._failures >= GLOBAL_CB_FAILURE_THRESHOLD:
                elapsed = time.monotonic() - self._opened_at
                if elapsed < GLOBAL_CB_RECOVERY_TIME:
                    raise PasarGuardCircuitOpen(
                        "All PasarGuard panels temporarily unavailable. "
                        f"Retry in {int(GLOBAL_CB_RECOVERY_TIME - elapsed)}s."
                    )
                self._failures = GLOBAL_CB_FAILURE_THRESHOLD - 1

    async def record_success(self):
        async with self._lock:
            self._failures = 0

    async def record_failure(self):
        async with self._lock:
            self._failures += 1
            if self._failures >= GLOBAL_CB_FAILURE_THRESHOLD:
                self._opened_at = time.monotonic()
                logger.error(
                    "Global circuit breaker opened after %s failures.",
                    self._failures,
                )


_global_cb = GlobalCircuitBreaker()


# =========================================================
# HELPERS
# =========================================================

def _sanitize_for_log(value: Any, max_length: int = 200) -> str:
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return "<unprintable>"
    text = _LOG_CONTROL_RE.sub(" ", text)
    if len(text) > max_length:
        text = text[:max_length] + "..."
    return text


def _validate_username(username: Any) -> str:
    if not username:
        raise PasarGuardError("Username is required.")
    try:
        username = str(username).strip()
    except Exception:
        raise PasarGuardError("Invalid username.")
    if not CONFIG_NAME_RE.match(username):
        raise PasarGuardError(
            "Invalid config name (allowed: 3-64 chars, letters, digits, _ and -)."
        )
    return username


def _validate_volume(volume: Any) -> float:
    try:
        volume = float(volume)
    except (ValueError, TypeError):
        raise PasarGuardError("Invalid volume.")
    if not math.isfinite(volume):
        raise PasarGuardError("Volume is not finite.")
    if volume < MIN_VOLUME_GB or volume > MAX_VOLUME_GB:
        raise PasarGuardError(
            f"Volume must be between {MIN_VOLUME_GB} and {MAX_VOLUME_GB} GB."
        )
    return volume


def _validate_positive_int(
    value,
    field_name: str = "value",
    max_value: int = 1_000_000_000,
) -> int:
    try:
        value = int(value)
    except (ValueError, TypeError):
        raise PasarGuardError(f"Invalid {field_name}.")
    if value <= 0 or value > max_value:
        raise PasarGuardError(f"{field_name} must be between 1 and {max_value}.")
    return value


def _safe_url_join(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    if ".." in path or "\x00" in path:
        raise PasarGuardError("Invalid URL path.")
    return base + path


def _calculate_expire_timestamp(days: int) -> int:
    try:
        days = int(days)
    except (ValueError, TypeError):
        days = 30
    if days <= 0 or days > 3650:
        days = 30
    future = datetime.now(timezone.utc) + timedelta(days=days)
    return int(future.timestamp())


def _default_port_for_scheme(scheme: str) -> int:
    if scheme == "https":
        return 443
    return 80


# =========================================================
# PASARGUARD API
# =========================================================

class PasarGuardAPI:

    def __init__(self, panel: PanelConfig):
        if not isinstance(panel, PanelConfig):
            raise PasarGuardError("PasarGuardAPI requires a PanelConfig instance.")

        self.panel_id = panel.id
        self.panel_name = _validate_panel_name(panel.name)
        # سازگاری با همه‌ی لاگ‌ها و خروجی‌های همین فایل که از self.panel استفاده می‌کنند
        self.panel = self.panel_name

        self.base_url = (panel.url or "").rstrip("/")
        self.username = panel.username
        self.password = panel.password
        group_id = panel.group_id

        if not self.base_url:
            raise PasarGuardError(f"Panel '{self.panel_name}' URL is missing.")

        if not self.username or not self.password:
            raise PasarGuardError(f"Panel '{self.panel_name}' credentials are missing.")

        if not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            raise PasarGuardError(f"Panel '{self.panel_name}' URL must start with http/https.")

        try:
            parsed = urlparse(self.base_url)
            self.base_hostname = (parsed.hostname or "").lower()
            self.base_scheme = parsed.scheme or "https"
            self.base_port = parsed.port
            if self.base_port is None:
                self.base_port = _default_port_for_scheme(self.base_scheme)
        except Exception:
            self.base_hostname = ""
            self.base_scheme = "https"
            self.base_port = None

        if not self.base_hostname:
            raise PasarGuardError(f"Panel '{self.panel_name}' URL is invalid.")

        self.group_id: Optional[int] = None
        if group_id is not None:
            try:
                gid = int(group_id)
                if gid > 0:
                    self.group_id = gid
            except (ValueError, TypeError):
                logger.warning(
                    "Panel '%s': invalid group_id: %s",
                    self.panel_name,
                    _sanitize_for_log(group_id),
                )

        self.access_token: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._login_lock = asyncio.Lock()

        self._cb_failures = 0
        self._cb_opened_at = 0.0

    # ===================================================== 
    # SESSION
    # =====================================================

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(
                total=REQUEST_TIMEOUT,
                connect=REQUEST_CONNECT_TIMEOUT,
            )
            connector = aiohttp.TCPConnector(
                limit=50,
                limit_per_host=20,
                ttl_dns_cache=300,
                ssl=True,
            )
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                pass
        self.session = None
        self.access_token = None

    # =====================================================
    # URL
    # =====================================================

    def _url(self, path: str) -> str:
        return _safe_url_join(self.base_url, path)

    @staticmethod
    def _user_path(username: str) -> str:
        if not username:
            raise PasarGuardError("Username is required.")
        safe = quote(str(username), safe="")
        if not safe:
            raise PasarGuardError("Invalid username for URL.")
        return f"/api/user/{safe}"

    # =====================================================
    # SAFE ERROR TEXT
    # =====================================================

    @staticmethod
    def _safe_error_text(text: Any, max_length: int = 200) -> str:
        if text is None:
            return "Unknown error."
        try:
            value = str(text)
        except Exception:
            return "Unknown error."

        lowered = value.lower()
        for word in _SENSITIVE_WORDS:
            if word in lowered:
                return "PasarGuard returned an error (details hidden)."

        value = _sanitize_for_log(value, max_length=max_length)

        if len(value) > 150 or any(c in value for c in ("<", ">", "{", "}")):
            return "PasarGuard returned an error (details hidden)."

        return value or "Unknown error."

    # =====================================================
    # URL SAFETY
    # =====================================================

    def _is_safe_url(self, url: str) -> bool:
        """
        ✅ FIX: قبول subdomainهای همخانواده‌ی base_hostname.
        """
        if not url:
            return False
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False

            hostname = (parsed.hostname or "").lower()
            if not hostname:
                return False

            base_host = (self.base_hostname or "").lower()
            if not base_host:
                return False

            if hostname == base_host:
                return True

            def _root_domain(h: str) -> str:
                parts = h.split(".")
                if len(parts) < 2:
                    return h
                return ".".join(parts[-2:])

            if _root_domain(hostname) != _root_domain(base_host):
                logger.warning(
                    "Panel %s: rejected URL with different root domain: %s (base=%s)",
                    self.panel,
                    _sanitize_for_log(url, max_length=200),
                    base_host,
                )
                return False

            actual_port = parsed.port
            if actual_port is not None:
                if not (1 <= actual_port <= 65535):
                    return False

            return True
        except Exception:
            return False

    # =====================================================
    # SUBSCRIPTION URL
    # =====================================================

    def normalize_subscription_url(
        self,
        subscription_url: Optional[str],
    ) -> Optional[str]:
        if not subscription_url:
            return None
        try:
            url = str(subscription_url).strip()
        except Exception:
            return None
        if not url:
            return None

        if len(url) > 2000:
            return None

        if _LOG_CONTROL_RE.search(url):
            return None

        if url.startswith("http://") or url.startswith("https://"):
            normalized = url.rstrip("/")
            if not self._is_safe_url(normalized):
                logger.warning(
                    "Panel %s: rejected URL with untrusted domain: %s",
                    self.panel,
                    _sanitize_for_log(normalized),
                )
                return None
            return normalized

        if url.startswith("/"):
            full = self.base_url + url.rstrip("/")
            if self._is_safe_url(full):
                return full
            return None

        if url.startswith("sub/"):
            full = self.base_url + "/" + url.rstrip("/")
            if self._is_safe_url(full):
                return full
            return None

        return None

    # =====================================================
    # CIRCUIT BREAKER
    # =====================================================

    def _check_circuit(self):
        if self._cb_failures >= CB_FAILURE_THRESHOLD:
            elapsed = time.monotonic() - self._cb_opened_at
            if elapsed < CB_RECOVERY_TIME:
                raise PasarGuardCircuitOpen(
                    f"Panel {self.panel} temporarily unavailable. "
                    f"Retry in {int(CB_RECOVERY_TIME - elapsed)}s."
                )
            self._cb_failures = CB_FAILURE_THRESHOLD - 1

    def _record_success(self):
        self._cb_failures = 0

    def _record_failure(self):
        self._cb_failures += 1
        if self._cb_failures >= CB_FAILURE_THRESHOLD:
            self._cb_opened_at = time.monotonic()
            logger.error(
                "Panel %s circuit breaker opened after %s failures.",
                self.panel,
                self._cb_failures,
            )

    # =====================================================
    # LOGIN
    # =====================================================

    async def login(self) -> str:
        async with self._login_lock:
            if self.access_token:
                return self.access_token

            self._check_circuit()
            await _global_cb.check()

            session = await self._get_session()
            url = self._url("/api/admin/token")

            payload = {
                "username": self.username,
                "password": self.password,
                "grant_type": "password",
            }

            try:
                async with session.post(url, data=payload) as response:
                    text = await response.text()

                    if len(text) > MAX_RESPONSE_SIZE:
                        self._record_failure()
                        await _global_cb.record_failure()
                        raise PasarGuardError("Response too large.")

                    if response.status >= 400:
                        self._record_failure()
                        await _global_cb.record_failure()
                        raise PasarGuardError(
                            f"Login failed ({response.status}).",
                            status=response.status,
                        )

                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        self._record_failure()
                        await _global_cb.record_failure()
                        raise PasarGuardError("Login returned invalid JSON.")

                    token = (
                        data.get("access_token")
                        if isinstance(data, dict) else None
                    )

                    if not token:
                        self._record_failure()
                        await _global_cb.record_failure()
                        raise PasarGuardError("Login succeeded but no token.")

                    self.access_token = str(token)
                    self._record_success()
                    await _global_cb.record_success()

                    logger.info("Panel %s login successful.", self.panel)
                    return self.access_token

            except asyncio.TimeoutError:
                self._record_failure()
                await _global_cb.record_failure()
                raise PasarGuardError("Login timed out.")

            except aiohttp.ClientError as e:
                self._record_failure()
                await _global_cb.record_failure()
                raise PasarGuardError(
                    f"Connection error: {self._safe_error_text(e)}"
                )

    # =====================================================
    # REQUEST
    # =====================================================

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        retry: bool = True,
        timeout_retry: bool = True,
    ):
        token = await self.login()
        session = await self._get_session()
        url = self._url(path)

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            async with session.request(
                method, url,
                headers=headers,
                json=json,
                data=data,
                params=params,
            ) as response:
                text = await response.text()

                if len(text) > MAX_RESPONSE_SIZE:
                    raise PasarGuardError("Response too large.")

                if response.status == 401 and retry:
                    logger.warning(
                        "Panel %s token expired, refreshing.",
                        self.panel,
                    )
                    self.access_token = None
                    await self.login()
                    return await self._request(
                        method, path,
                        json=json, data=data, params=params,
                        retry=False,
                        timeout_retry=timeout_retry,
                    )

                if response.status >= 400:
                    if response.status >= 500:
                        self._record_failure()
                        await _global_cb.record_failure()

                    error_detail = ""
                    try:
                        err_data = await response.json(content_type=None)
                        if isinstance(err_data, dict):
                            error_detail = (
                                err_data.get("detail")
                                or err_data.get("message")
                                or err_data.get("error")
                                or ""
                            )
                    except Exception:
                        pass

                    if not error_detail:
                        raw = (text or "").strip()
                        if raw and len(raw) <= 200 and "<" not in raw and ">" not in raw:
                            error_detail = raw

                    if error_detail:
                        error_msg = f"API error {response.status}: {error_detail}"
                    else:
                        error_msg = f"API error {response.status}."

                    logger.error(
                        "Panel %s: %s %s → HTTP %s | detail=%s",
                        self.panel, method, path, response.status,
                        _sanitize_for_log(str(error_detail), max_length=200),
                    )

                    raise PasarGuardError(error_msg, status=response.status)

                self._record_success()
                await _global_cb.record_success()

                if not text.strip():
                    return {}

                try:
                    return await response.json(content_type=None)
                except Exception:
                    return {"raw_text": text}

        except asyncio.TimeoutError:
            self._record_failure()
            await _global_cb.record_failure()

            if timeout_retry:
                logger.warning(
                    "Panel %s: request timed out, retrying once...",
                    self.panel,
                )
                await asyncio.sleep(1.0)
                try:
                    return await self._request(
                        method, path,
                        json=json, data=data, params=params,
                        retry=retry,
                        timeout_retry=False,
                    )
                except Exception:
                    pass

            raise PasarGuardError("Request timed out.")

        except aiohttp.ClientError as e:
            self._record_failure()
            await _global_cb.record_failure()

            if timeout_retry:
                logger.warning(
                    "Panel %s: connection error, retrying once: %s",
                    self.panel,
                    self._safe_error_text(e),
                )
                await asyncio.sleep(1.0)
                try:
                    return await self._request(
                        method, path,
                        json=json, data=data, params=params,
                        retry=retry,
                        timeout_retry=False,
                    )
                except Exception:
                    pass

            raise PasarGuardError(
                f"Connection error: {self._safe_error_text(e)}"
            )

    # =====================================================
    # TEST
    # =====================================================

    async def test_connection(self):
        try:
            await self.login()
            return {
                "success": True,
                "message": f"Panel {self.panel} connected successfully.",
                "data": {},
            }
        except Exception as e:
            logger.exception("Panel %s connection test failed.", self.panel)
            return {
                "success": False,
                "message": self._safe_error_text(e),
                "data": {},
            }

    # =====================================================
    # GET USER
    # =====================================================

    async def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        username = _validate_username(username)
        try:
            return await self._request("GET", self._user_path(username))
        except PasarGuardError as e:
            if e.status == 404:
                return None
            raise

    # =====================================================
    # USER EXISTS
    # =====================================================

    async def user_exists(self, username: str) -> bool:
        try:
            user = await self.get_user(username)
            return user is not None
        except Exception:
            return False

    # =====================================================
    # EXTRACT SUB URL
    # =====================================================

    def _extract_subscription_url(self, data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None

        possible_keys = [
            "subscription_url", "subscription", "subscription_link",
            "sub_url", "sub",
        ]

        for key in possible_keys:
            value = data.get(key)
            if isinstance(value, str):
                normalized = self.normalize_subscription_url(value)
                if normalized:
                    return normalized
            elif isinstance(value, dict):
                for nk in ("url", "subscription_url", "link"):
                    nv = value.get(nk)
                    if isinstance(nv, str):
                        normalized = self.normalize_subscription_url(nv)
                        if normalized:
                            return normalized
        return None

    # =====================================================
    # EXTRACT USAGE
    # =====================================================

    @staticmethod
    def _extract_usage(data: Dict[str, Any]) -> float:
        keys = [
            "used_traffic", "usage", "data_usage", "used",
            "used_bytes", "traffic_used",
        ]
        for key in keys:
            value = data.get(key)
            if value is not None:
                try:
                    n = float(value)
                    if math.isfinite(n) and n >= 0:
                        return n
                except (ValueError, TypeError):
                    pass
        return 0.0

    @staticmethod
    def _extract_status(data: Dict[str, Any]) -> Optional[str]:
        value = data.get("status")
        if value is None:
            return None
        try:
            return str(value).strip().lower()
        except Exception:
            return None

    # =====================================================
    # NORMALIZE
    # =====================================================

    def _normalize_user_response(
        self,
        data: Dict[str, Any],
        *,
        fallback_username: Optional[str] = None,
        subscription_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise PasarGuardError("Invalid user response.")

        username = data.get("username") or fallback_username
        user_id = data.get("id")
        data_limit = data.get("data_limit")
        usage = self._extract_usage(data)

        expire = (
            data.get("expire")
            or data.get("expire_date")
            or data.get("expiration")
        )

        status = self._extract_status(data)
        hwid_limit = data.get("hwid_limit")
        group_ids = data.get("group_ids")

        if subscription_url is None:
            subscription_url = self._extract_subscription_url(data)

        subscription_url = self.normalize_subscription_url(subscription_url)

        remaining_volume = None
        try:
            if data_limit is not None:
                limit = float(data_limit)
                if math.isfinite(limit) and limit > 0:
                    remaining_bytes = max(0, limit - usage)
                    remaining_volume = remaining_bytes / BYTES_PER_GB
                elif limit == 0:
                    remaining_volume = None
        except (ValueError, TypeError):
            remaining_volume = None

        return {
            "success": True,
            "id": user_id,
            "username": username,
            "subscription_url": subscription_url,
            "data_limit": data_limit,
            "used_traffic": usage,
            "usage": usage,
            "remaining_volume": remaining_volume,
            "status": status,
            "expire": expire,
            "hwid_limit": hwid_limit,
            "group_ids": group_ids,
            "panel": self.panel,
            "panel_id": self.panel_id,
            "raw": data,
        }

    # =====================================================
    # CREATE USER
    # =====================================================

    async def create_user(
        self,
        username: str,
        volume_gb: float,
        note: Optional[str] = None,
        group_id: Optional[int] = None,
        hwid_limit: Optional[int] = None,
        expire_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        username = _validate_username(username)
        try:
            unlimited_volume = float(volume_gb) == 0
        except (TypeError, ValueError):
            raise PasarGuardError("Invalid volume.")
        if unlimited_volume:
            data_limit = 0  # در پاسارگارد data_limit=0 یعنی حجم نامحدود
        else:
            volume_gb = _validate_volume(volume_gb)
            data_limit = int(volume_gb * BYTES_PER_GB)
            if data_limit <= 0:
                raise PasarGuardError("Calculated data limit is invalid.")

        try:
            existing = await self.user_exists(username)
            if existing:
                logger.warning(
                    "Panel %s: user %s already exists in panel!",
                    self.panel,
                    _sanitize_for_log(username),
                )
                raise PasarGuardError(
                    f"User {username} already exists in panel.",
                    status=409,
                )
        except PasarGuardError:
            raise
        except Exception as e:
            logger.warning(
                "Panel %s: pre-check failed (continuing): %s",
                self.panel,
                self._safe_error_text(e),
            )

        payload: Dict[str, Any] = {
            "username": username,
            "proxy_settings": {},
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        }

        if expire_days is not None:
            try:
                expire_ts = _calculate_expire_timestamp(expire_days)
                payload["expire"] = expire_ts
            except Exception:
                logger.exception("Could not set expire")

        if hwid_limit is not None:
            try:
                payload["hwid_limit"] = _validate_positive_int(
                    hwid_limit, "hwid_limit", max_value=1000,
                )
            except PasarGuardError:
                pass

        if note:
            try:
                note_str = str(note)[:500]
                note_str = _LOG_CONTROL_RE.sub(" ", note_str)
                if note_str:
                    payload["note"] = note_str
            except Exception:
                pass

        selected_group = group_id if group_id is not None else self.group_id
        if selected_group is not None:
            try:
                payload["group_ids"] = [
                    _validate_positive_int(
                        selected_group, "group_id", max_value=1_000_000,
                    )
                ]
            except PasarGuardError:
                pass

        logger.info(
            "Panel %s: creating user %s (%sGB)",
            self.panel,
            _sanitize_for_log(username),
            volume_gb,
        )

        response = await self._request("POST", "/api/user", json=payload)

        subscription_url = self._extract_subscription_url(response)
        user_data = response

        if not subscription_url:
            try:
                fetched = await self.get_user(username)
                if fetched:
                    user_data = fetched
                    subscription_url = self._extract_subscription_url(fetched)
            except Exception as e:
                logger.warning(
                    "Could not fetch created user: %s",
                    self._safe_error_text(e),
                )

        subscription_url = self.normalize_subscription_url(subscription_url)

        if not subscription_url:
            raise PasarGuardError(
                "User created but PasarGuard did not return a subscription URL."
            )

        result = self._normalize_user_response(
            user_data,
            fallback_username=username,
            subscription_url=subscription_url,
        )

        result["success"] = True
        logger.info(
            "Panel %s: user %s created successfully.",
            self.panel,
            _sanitize_for_log(username),
        )
        return result

    # =====================================================
    # RENEW CONFIG
    # =====================================================

    async def renew_config(
        self,
        username: str,
        additional_volume_gb: float,
    ) -> Dict[str, Any]:
        username = _validate_username(username)
        additional_volume_gb = _validate_volume(additional_volume_gb)

        current = await self.get_user(username)
        if not current:
            raise PasarGuardError("User not found.", status=404)

        current_limit = current.get("data_limit")
        usage = self._extract_usage(current)

        if current_limit is None:
            raise PasarGuardError("Current data_limit is unknown.")

        try:
            current_limit = int(current_limit)
        except (ValueError, TypeError):
            raise PasarGuardError("Invalid data_limit from PasarGuard.")

        if current_limit == 0:
            raise PasarGuardError(
                "This config has unlimited traffic and cannot be renewed."
            )

        additional_bytes = int(additional_volume_gb * BYTES_PER_GB)
        if additional_bytes <= 0:
            raise PasarGuardError("Calculated additional volume is invalid.")

        new_limit = current_limit + additional_bytes

        payload: Dict[str, Any] = {"data_limit": new_limit}

        current_status = self._extract_status(current)
        if current_status in ("limited", "expired") and new_limit > usage:
            payload["status"] = "active"

        logger.info(
            "Panel %s: renewing %s: +%sGB",
            self.panel,
            _sanitize_for_log(username),
            additional_volume_gb,
        )

        response = await self._request(
            "PUT",
            self._user_path(username),
            json=payload,
        )

        try:
            fresh = await self.get_user(username)
        except Exception as e:
            logger.warning(
                "Could not refresh after renew: %s",
                self._safe_error_text(e),
            )
            fresh = None

        if fresh:
            user_data = fresh
        elif isinstance(response, dict) and response:
            user_data = response
        else:
            user_data = dict(current)
            user_data["data_limit"] = new_limit

        subscription_url = self._extract_subscription_url(user_data)
        if not subscription_url:
            subscription_url = self._extract_subscription_url(current)

        subscription_url = self.normalize_subscription_url(subscription_url)

        result = self._normalize_user_response(
            user_data,
            fallback_username=username,
            subscription_url=subscription_url,
        )
        result["success"] = True
        return result

    # =====================================================
    # UPDATE CONFIG VOLUME
    # =====================================================

    async def update_config_volume(
        self,
        username: str,
        change_gb: float,
    ) -> Dict[str, Any]:
        username = _validate_username(username)

        try:
            change_gb = float(change_gb)
        except (ValueError, TypeError):
            raise PasarGuardError("Invalid change amount.")

        if not math.isfinite(change_gb):
            raise PasarGuardError("Change amount is not finite.")

        if change_gb == 0:
            raise PasarGuardError("Change amount cannot be zero.")

        current = await self.get_user(username)
        if not current:
            raise PasarGuardError("User not found.", status=404)

        current_limit = current.get("data_limit")
        usage = self._extract_usage(current)

        if current_limit is None:
            raise PasarGuardError("Current data_limit is unknown.")

        try:
            current_limit = int(current_limit)
        except (ValueError, TypeError):
            raise PasarGuardError("Invalid data_limit from PasarGuard.")

        if current_limit == 0:
            raise PasarGuardError(
                "This config has unlimited traffic and cannot be edited."
            )

        change_bytes = int(change_gb * BYTES_PER_GB)
        new_limit = current_limit + change_bytes

        if new_limit <= 0:
            raise PasarGuardError("New volume would be zero or negative.")

        if new_limit < usage:
            raise PasarGuardError("New volume cannot be less than current usage.")

        payload: Dict[str, Any] = {"data_limit": new_limit}

        current_status = self._extract_status(current)
        if current_status in ("limited", "expired") and new_limit > usage:
            payload["status"] = "active"

        logger.info(
            "Panel %s: editing volume of %s: %s GB -> %s GB",
            self.panel,
            _sanitize_for_log(username),
            current_limit / BYTES_PER_GB,
            new_limit / BYTES_PER_GB,
        )

        response = await self._request(
            "PUT",
            self._user_path(username),
            json=payload,
        )

        try:
            fresh = await self.get_user(username)
        except Exception as e:
            logger.warning(
                "Could not refresh after volume edit: %s",
                self._safe_error_text(e),
            )
            fresh = None

        if fresh:
            user_data = fresh
        elif isinstance(response, dict) and response:
            user_data = response
        else:
            user_data = dict(current)
            user_data["data_limit"] = new_limit

        subscription_url = self._extract_subscription_url(user_data)
        if not subscription_url:
            subscription_url = self._extract_subscription_url(current)

        subscription_url = self.normalize_subscription_url(subscription_url)

        result = self._normalize_user_response(
            user_data,
            fallback_username=username,
            subscription_url=subscription_url,
        )
        result["success"] = True
        result["new_data_limit"] = new_limit
        result["new_volume_gb"] = new_limit / BYTES_PER_GB
        return result

    # =====================================================
    # GET CONFIG INFO
    # =====================================================

    async def get_config_info(self, username: str) -> Dict[str, Any]:
        username = _validate_username(username)
        user = await self.get_user(username)
        if not user:
            raise PasarGuardError("User not found.", status=404)
        return self._normalize_user_response(user, fallback_username=username)

    # =====================================================
    # FULL CONFIG
    # =====================================================

    async def get_full_config(self, username: str) -> Dict[str, Any]:
        username = _validate_username(username)
        user = await self.get_user(username)
        if not user:
            raise PasarGuardError("User not found.", status=404)

        subscription_url = self._extract_subscription_url(user)
        if not subscription_url:
            raise PasarGuardError("Subscription URL not found.")

        if not self._is_safe_url(subscription_url):
            raise PasarGuardError("Subscription URL domain is not trusted.")

        links_url = subscription_url.rstrip("/") + "/links"

        if not self._is_safe_url(links_url):
            raise PasarGuardError("Subscription links URL is not trusted.")

        session = await self._get_session()

        try:
            async with session.get(
                links_url,
                timeout=aiohttp.ClientTimeout(
                    total=REQUEST_TIMEOUT,
                    connect=REQUEST_CONNECT_TIMEOUT,
                ),
            ) as response:
                text = await response.text()

                if len(text) > MAX_RESPONSE_SIZE:
                    raise PasarGuardError("Response too large.")

                if response.status >= 400:
                    raise PasarGuardError(
                        f"Could not retrieve links ({response.status}).",
                        status=response.status,
                    )
        except asyncio.TimeoutError:
            raise PasarGuardError("Links request timed out.")
        except aiohttp.ClientError as e:
            raise PasarGuardError(
                f"Could not retrieve links: {self._safe_error_text(e)}"
            )

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise PasarGuardError("No links returned.")

        config_links = [ln for ln in lines if "://" in ln]
        if not config_links:
            raise PasarGuardError("No configuration links found.")

        return {
            "success": True,
            "username": username,
            "subscription_url": subscription_url,
            "links": config_links,
            "text": "\n".join(config_links),
            "count": len(config_links),
            "panel": self.panel,
            "panel_id": self.panel_id,
        }

    # =====================================================
    # DELETE
    # =====================================================

    async def delete_config(
        self,
        username: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not username and user_id is None:
            raise PasarGuardError("Username or user ID is required.")

        if user_id is not None:
            user_id = _validate_positive_int(user_id, "user_id")
            path = f"/api/user/by-id/{user_id}"
            identifier = str(user_id)
        else:
            username = _validate_username(username)
            path = self._user_path(username)
            identifier = str(username)

        logger.info(
            "Panel %s: deleting user %s",
            self.panel,
            _sanitize_for_log(identifier),
        )

        response = await self._request("DELETE", path)
        return {
            "success": True,
            "identifier": identifier,
            "data": response,
        }


# =========================================================
# PANEL REGISTRY (جدید — جایگزین سینگلتون‌های ثابت ۱/۲/۳)
# =========================================================

class PanelRegistry:
    """
    به‌جای _pasarguard_1/2/3 ثابت، کلاینت هر پنل را بر اساس panel_id از
    دیتابیس می‌سازد و کش می‌کند. تعداد پنل‌ها هرچقدر ادمین اضافه کرده باشد
    همان‌قدر است.

    وابسته به database.get_panel(panel_id) -> dict | None
    """

    def __init__(self):
        self._clients: Dict[int, PasarGuardAPI] = {}
        self._lock = asyncio.Lock()

    async def get_client(self, panel_id: int) -> PasarGuardAPI:
        if panel_id in self._clients:
            return self._clients[panel_id]

        async with self._lock:
            if panel_id in self._clients:
                return self._clients[panel_id]

            from database import get_panel

            row = get_panel(panel_id)
            if not row:
                raise PasarGuardError(f"Panel #{panel_id} not found in database.")

            config = PanelConfig(
                id=row["id"],
                name=row["name"],
                url=row["url"],
                username=row["username"],
                password=row["password"],
                group_id=row.get("group_id"),
            )
            client = PasarGuardAPI(config)
            self._clients[panel_id] = client
            return client

    def invalidate(self, panel_id: int) -> None:
        """بعد از ادیت یا حذف یک پنل در پنل ادمین، کلاینت کش‌شده پاک می‌شود."""
        self._clients.pop(panel_id, None)

    async def close_all(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                logger.exception("Error closing a panel client.")
        self._clients.clear()

    @staticmethod
    async def test_panel_connection(
        url: str,
        username: str,
        password: str,
        group_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """تست یک پنل قبل از ذخیره در دیتابیس (فرم «افزودن پنل جدید»)."""
        temp_config = PanelConfig(
            id=0, name="__test__", url=url, username=username,
            password=password, group_id=group_id,
        )
        client = PasarGuardAPI(temp_config)
        try:
            return await client.test_connection()
        finally:
            await client.close()


panel_registry = PanelRegistry()


# =========================================================
# PUBLIC WRAPPERS
# =========================================================
# نکته‌ی مهم: همه‌ی این‌ها حالا panel_id می‌گیرند (دیگر عدد ثابت ۱/۲/۳ نیست).
# این panel_id در آینده از رکورد محصول در دیتابیس خوانده می‌شود.

async def normalize_subscription_url(url: Optional[str], panel_id: int) -> Optional[str]:
    try:
        client = await panel_registry.get_client(panel_id)
        return client.normalize_subscription_url(url)
    except Exception:
        return None


async def test_connection(panel_id: int) -> Dict[str, Any]:
    try:
        client = await panel_registry.get_client(panel_id)
        return await client.test_connection()
    except Exception as e:
        logger.exception("Panel %s test failed", panel_id)
        return {"success": False, "message": str(e), "data": {}}


def _build_note(telegram_id: int, telegram_username: Optional[str], kind: str) -> str:
    username_part = "N/A"
    if telegram_username:
        try:
            u = str(telegram_username).strip().lstrip("@")
            if TELEGRAM_USERNAME_RE.match(u):
                username_part = f"@{u}"
        except Exception:
            pass
    return f"Telegram ID: {telegram_id} | Username: {username_part} | Type: {kind}"


async def create_volume_config(
    telegram_id: int,
    volume_gb: float,
    config_name: str,
    panel_id: int,
    telegram_username: Optional[str] = None,
) -> Dict[str, Any]:
    """ساخت کانفیگ حجمی روی پنلی که ادمین برای این محصول انتخاب کرده."""
    try:
        telegram_id = int(telegram_id)
    except (ValueError, TypeError):
        raise PasarGuardError("Invalid telegram_id.")
    if telegram_id <= 0:
        raise PasarGuardError("Invalid telegram_id.")

    client = await panel_registry.get_client(panel_id)
    note = _build_note(telegram_id, telegram_username, "volume")

    return await client.create_user(
        username=config_name,
        volume_gb=volume_gb,
        note=note,
        group_id=client.group_id,
    )


async def renew_config(
    username: str,
    additional_volume_gb: float,
    panel_id: int,
) -> Dict[str, Any]:
    client = await panel_registry.get_client(panel_id)
    return await client.renew_config(username, additional_volume_gb)


async def update_config_volume(
    username: str,
    change_gb: float,
    panel_id: int,
) -> Dict[str, Any]:
    client = await panel_registry.get_client(panel_id)
    return await client.update_config_volume(username, change_gb)


async def get_config_info(username: str, panel_id: int) -> Dict[str, Any]:
    client = await panel_registry.get_client(panel_id)
    return await client.get_config_info(username)


async def get_full_config(username: str, panel_id: int) -> Dict[str, Any]:
    client = await panel_registry.get_client(panel_id)
    return await client.get_full_config(username)


async def delete_config(
    panel_id: int,
    username: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    client = await panel_registry.get_client(panel_id)
    return await client.delete_config(username=username, user_id=user_id)


# =========================================================
# SUBSCRIPTION-LEVEL API (لایهٔ سازگاری با کد قدیمی PAXIVA)
# =========================================================
# همهٔ توابع این بخش یک «sub» (ردیف جدول subscriptions شامل panel_id و
# config_name) می‌گیرند، پنل را از panel_registry پیدا می‌کنند و با username
# (= config_name) کار می‌کنند. خروجی get_live/renew_days/... همان شکل دیکشنری
# PAXIVA است تا lifecycle/scheduler/renew/services با کمترین تغییر کار کنند:
#   {"name","link","used_bytes","volume_gb"(0=نامحدود),"expires_at"(ts|None),
#    "state","devices","panel_id"}

USER_RESET_PATH_TEMPLATE = "/api/user/{username}/reset"

_STATE_MAP = {
    "active": "active",
    "on_hold": "active",
    "limited": "depleted",
    "expired": "expired",
    "disabled": "disabled",
}

_IDEM_TTL = 24 * 3600
_IDEM_MAX = 2000
_idem_cache: "Dict[str, tuple]" = {}          # key -> (monotonic_ts, result)
_user_locks: "Dict[str, asyncio.Lock]" = {}   # panel:username -> Lock


def _to_ts(value: Any) -> Optional[int]:
    """expire پاسارگارد (عدد/رشتهٔ عددی/ISO) → timestamp ثانیه؛ None/0 = بدون انقضا."""
    if value in (None, "", 0, "0"):
        return None
    try:
        if isinstance(value, (int, float)):
            n = int(value)
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            n = int(value.strip())
        elif isinstance(value, str):
            text = value.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            n = int(dt.timestamp())
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    if n > 10_000_000_000:  # میلی‌ثانیه
        n //= 1000
    return n if n > 0 else None


def is_not_found(exc: BaseException) -> bool:
    return isinstance(exc, PasarGuardError) and getattr(exc, "status", None) == 404


def is_definite_rejection(exc: BaseException) -> bool:
    """
    رد قطعی = پنل درخواست را قطعاً اعمال نکرده (قابل بازگشت وجه):
    خطای 4xx (جز 408/425/429) یا باز بودن circuit breaker (درخواست اصلاً ارسال نشده).
    timeout/قطع شبکه/5xx = نامطمئن.
    """
    if isinstance(exc, PasarGuardCircuitOpen):
        return True
    if not isinstance(exc, PasarGuardError):
        return False
    status = getattr(exc, "status", None)
    return status is not None and 400 <= int(status) < 500 and int(status) not in (408, 425, 429)


def _sub_ref(sub: Any) -> "tuple[int, str]":
    try:
        data = dict(sub)
    except Exception:
        raise PasarGuardError("Invalid subscription record.")
    panel_id = data.get("panel_id")
    username = data.get("config_name")
    if not panel_id:
        raise PasarGuardError("Subscription has no panel_id.")
    try:
        panel_id = int(panel_id)
    except (TypeError, ValueError):
        raise PasarGuardError("Subscription has an invalid panel_id.")
    return panel_id, _validate_username(username)


def _lock_for(panel_id: int, username: str) -> asyncio.Lock:
    key = f"{panel_id}:{username}"
    lock = _user_locks.get(key)
    if lock is None:
        if len(_user_locks) > 5000:
            for k in [k for k, v in _user_locks.items() if not v.locked()][:2500]:
                _user_locks.pop(k, None)
        lock = _user_locks[key] = asyncio.Lock()
    return lock


def _idem_get(key: Optional[str]):
    if not key:
        return None
    entry = _idem_cache.get(key)
    if entry and time.monotonic() - entry[0] < _IDEM_TTL:
        return entry[1]
    return None


def _idem_put(key: Optional[str], result: Dict[str, Any]) -> None:
    if not key:
        return
    if len(_idem_cache) >= _IDEM_MAX:
        for k in sorted(_idem_cache, key=lambda x: _idem_cache[x][0])[: _IDEM_MAX // 2]:
            _idem_cache.pop(k, None)
    _idem_cache[key] = (time.monotonic(), result)


def to_client_view(client: "PasarGuardAPI", user: Dict[str, Any], panel_id: int) -> Dict[str, Any]:
    """پاسخ خام پاسارگارد → دیکشنری هم‌شکل PAXIVA."""
    try:
        limit = int(user.get("data_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    used = int(client._extract_usage(user))
    expires_at = _to_ts(user.get("expire") or user.get("expire_date") or user.get("expiration"))
    raw_status = client._extract_status(user) or "active"
    state = _STATE_MAP.get(raw_status, "active")
    now = int(time.time())
    if state == "active":
        if limit > 0 and used >= limit:
            state = "depleted"
        elif expires_at is not None and expires_at <= now:
            state = "expired"
    return {
        "name": user.get("username"),
        "link": client._extract_subscription_url(user),
        "used_bytes": max(0, used),
        "volume_gb": (limit / BYTES_PER_GB) if limit > 0 else 0,
        "expires_at": expires_at,
        "state": state,
        "devices": user.get("hwid_limit"),
        "panel_id": panel_id,
        "panel_status": raw_status,
    }


async def _load(sub: Any) -> "tuple[PasarGuardAPI, int, str, Dict[str, Any]]":
    panel_id, username = _sub_ref(sub)
    client = await panel_registry.get_client(panel_id)
    user = await client.get_user(username)
    if not user:
        raise PasarGuardError("User not found.", status=404)
    return client, panel_id, username, user


async def get_live(sub: Any) -> Dict[str, Any]:
    """وضعیت زندهٔ یک اشتراک از پنل (برای lifecycle و نمایش سرویس)."""
    client, panel_id, _username, user = await _load(sub)
    return to_client_view(client, user, panel_id)


async def renew_days(sub: Any, days: int, idem_key: Optional[str] = None) -> Dict[str, Any]:
    """
    افزودن days روز به انقضا: max(انقضای فعلی، الان) + days.
    * پاسارگارد idempotency ندارد؛ کلید در حافظه نگه داشته می‌شود تا تکرار همان
      درخواست (retry) دوباره روز اضافه نکند. (با ری‌استارت ربات پاک می‌شود.)
    * خواندن-تغییر-نوشتن با قفل per-user انجام می‌شود تا دو تمدید هم‌زمان گم نشوند.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise PasarGuardError("Invalid days.", status=400)
    if days <= 0 or days > 3650:
        raise PasarGuardError("Days out of range.", status=400)

    cached = _idem_get(idem_key)
    if cached is not None:
        return cached

    panel_id, username = _sub_ref(sub)
    async with _lock_for(panel_id, username):
        cached = _idem_get(idem_key)
        if cached is not None:
            return cached

        client, panel_id, username, user = await _load(sub)
        now = int(time.time())
        current = _to_ts(user.get("expire") or user.get("expire_date") or user.get("expiration"))
        new_expire = max(current or 0, now) + days * 86400

        payload: Dict[str, Any] = {"expire": new_expire}
        if (client._extract_status(user) or "") == "expired":
            try:
                limit = int(user.get("data_limit") or 0)
            except (TypeError, ValueError):
                limit = 0
            if limit == 0 or client._extract_usage(user) < limit:
                payload["status"] = "active"

        logger.info("Panel %s: renewing %s +%s days", panel_id, _sanitize_for_log(username), days)
        await client._request("PUT", client._user_path(username), json=payload)

        try:
            fresh = await client.get_user(username)
        except Exception:
            fresh = None
        view = to_client_view(client, fresh or {**user, "expire": new_expire}, panel_id)
        if not fresh:
            view["expires_at"] = new_expire
        _idem_put(idem_key, view)
        return view


async def reset_usage(sub: Any) -> Dict[str, Any]:
    """صفر کردن مصرف و فعال‌کردن سرویسِ limited."""
    panel_id, username = _sub_ref(sub)
    async with _lock_for(panel_id, username):
        client, panel_id, username, user = await _load(sub)
        logger.info("Panel %s: resetting usage of %s", panel_id, _sanitize_for_log(username))
        await client._request(
            "POST", USER_RESET_PATH_TEMPLATE.format(username=quote(username, safe=""))
        )
        if (client._extract_status(user) or "") == "limited":
            await client._request("PUT", client._user_path(username), json={"status": "active"})
        fresh = await client.get_user(username)
        return to_client_view(client, fresh or user, panel_id)


async def set_volume(sub: Any, volume_gb: float) -> Dict[str, Any]:
    """تعیین سقف حجم (مقدار مطلق). 0 = نامحدود."""
    try:
        volume_gb = float(volume_gb)
    except (TypeError, ValueError):
        raise PasarGuardError("Invalid volume.", status=400)
    if not math.isfinite(volume_gb) or volume_gb < 0 or volume_gb > MAX_VOLUME_GB:
        raise PasarGuardError("Volume out of range.", status=400)
    new_limit = int(volume_gb * BYTES_PER_GB)
    if volume_gb > 0 and new_limit <= 0:
        raise PasarGuardError("Volume too small.", status=400)

    panel_id, username = _sub_ref(sub)
    async with _lock_for(panel_id, username):
        client, panel_id, username, user = await _load(sub)
        payload: Dict[str, Any] = {"data_limit": new_limit}
        if (client._extract_status(user) or "") == "limited":
            if new_limit == 0 or new_limit > client._extract_usage(user):
                payload["status"] = "active"
        logger.info("Panel %s: set volume of %s to %s GB", panel_id, _sanitize_for_log(username), volume_gb)
        await client._request("PUT", client._user_path(username), json=payload)
        fresh = await client.get_user(username)
        return to_client_view(client, fresh or {**user, "data_limit": new_limit}, panel_id)


async def delete_subscription(sub: Any) -> bool:
    """
    حذف دائمی از پنل (پاسارگارد سطل بازیافت ندارد). نبودن کاربر (404) موفق حساب می‌شود.
    فقط برای حذف توسط ادمین یا rollback خرید ناقص صدا بزن.
    """
    panel_id, username = _sub_ref(sub)
    client = await panel_registry.get_client(panel_id)
    try:
        await client.delete_config(username=username)
    except PasarGuardError as exc:
        if getattr(exc, "status", None) == 404:
            return True
        raise
    return True


async def close_connection():
    await panel_registry.close_all()
