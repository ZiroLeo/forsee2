import httpx
import logging
import asyncio
from typing import Optional, Dict, Any
from config import PAXIVA_BASE_URL, PAXIVA_API_KEY, PAXIVA_TIMEOUT

logger = logging.getLogger(__name__)

class PaxivaError(Exception):
    """خطای پایه برای ارتباط با پنل"""
    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(f"[{status_code}] {error_code}: {message}")

class PaxivaClient:
    def __init__(self):
        self.base_url = PAXIVA_BASE_URL.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {PAXIVA_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=httpx.Timeout(PAXIVA_TIMEOUT),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
        )

    async def close(self):
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        idempotency_key: Optional[str] = None,
        retries: int = 3
    ) -> Dict[str, Any]:
        """
        درخواست امن به پنل با مدیریت خطا و Retry
        """
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        last_exception = None
        for attempt in range(retries):
            try:
                response = await self.client.request(
                    method, endpoint, json=json_data, headers=headers
                )

                if response.status_code == 200:
                    return response.json()

                # خطاهای منطقی (بدون Retry)
                if response.status_code in (400, 401, 403, 404, 409, 413, 422):
                    error_data = response.json() if response.content else {}
                    error_code = error_data.get("error", "unknown")
                    message = error_data.get("message", "خطای نامشخص از پنل")
                    logger.warning(f"Paxiva logical error: {response.status_code} {error_code} - {message}")
                    raise PaxivaError(response.status_code, error_code, message)

                # خطاهای موقت (با Retry)
                if response.status_code in (429, 502, 503):
                    retry_after = response.json().get("retry_after", 2) if response.content else 2
                    logger.warning(f"Paxiva temporary error {response.status_code}, retrying after {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue

                response.raise_for_status()

            except httpx.TimeoutException as e:
                logger.warning(f"Paxiva timeout (attempt {attempt+1}/{retries})")
                last_exception = e
                await asyncio.sleep(2 ** attempt)
            except httpx.NetworkError as e:
                logger.warning(f"Paxiva network error (attempt {attempt+1}/{retries}): {e}")
                last_exception = e
                await asyncio.sleep(2 ** attempt)
            except PaxivaError:
                raise
            except Exception as e:
                logger.error(f"Paxiva unexpected error: {e}")
                raise

        raise last_exception or PaxivaError(500, "network_error", "ارتباط با پنل برقرار نشد")

    # ============================================================
    # متدهای اصلی
    # ============================================================

    async def get_me(self) -> Dict[str, Any]:
        """دریافت اطلاعات حساب (تست اتصال)"""
        return await self._request("GET", "/me")

    async def create_client(
        self,
        name: str,
        volume_gb: float,
        days: int,
        devices: int,
        idempotency_key: str,
        kind: str = "vol"
    ) -> Dict[str, Any]:
        """
        ساخت کاربر جدید (حجمی)
        - volume_gb: 0 برای بی‌نهایت
        - days: 0 برای بدون انقضا
        """
        data = {
            "kind": kind,
            "name": name,
            "volume_gb": volume_gb,
            "days": days,
            "devices": devices,
        }
        logger.info(f"Creating Paxiva client: {name} (vol={volume_gb}GB, days={days}, devices={devices})")
        return await self._request("POST", "/clients", json_data=data, idempotency_key=idempotency_key)

    async def get_client(self, client_id: int) -> Dict[str, Any]:
        """دریافت اطلاعات کاربر (برای همگام‌سازی)"""
        return await self._request("GET", f"/clients/{client_id}")

    async def renew_volume_client(self, client_id: int, days: int, idempotency_key: str) -> Dict[str, Any]:
        """
        تمدید کاربر حجمی (فقط افزایش روز - طبق داکیومنت هزینه‌ای ندارد)
        """
        data = {"days": days}
        logger.info(f"Renewing volume client {client_id} for {days} days")
        return await self._request("POST", f"/clients/{client_id}/expiry", json_data=data, idempotency_key=idempotency_key)

    async def reset_client_traffic(self, client_id: int) -> Dict[str, Any]:
        """ریست مصرف حجم (برای پر کردن مجدد حجم پس از تمدید)"""
        logger.info(f"Resetting traffic for client {client_id}")
        return await self._request("POST", f"/clients/{client_id}/reset")

    async def update_client_volume(self, client_id: int, volume_gb: float) -> Dict[str, Any]:
        """تغییر سقف حجم"""
        data = {"volume_gb": volume_gb}
        return await self._request("POST", f"/clients/{client_id}/volume", json_data=data)

    async def update_client_devices(self, client_id: int, devices: int, idempotency_key: str) -> Dict[str, Any]:
        """تغییر تعداد دستگاه"""
        data = {"devices": devices}
        return await self._request("POST", f"/clients/{client_id}/devices", json_data=data, idempotency_key=idempotency_key)

    async def delete_client(self, client_id: int, purge: bool = True) -> Dict[str, Any]:
        """
        حذف کاربر
        - purge=True: حذف فوری برای همیشه (بدون سطل بازیافت)
        """
        url = f"/clients/{client_id}"
        if purge:
            url += "?purge=true"
        logger.warning(f"Deleting client {client_id} (purge={purge})")
        return await self._request("DELETE", url)

    async def create_trial_client(self, name: str, idempotency_key: str) -> Dict[str, Any]:
        """ساخت اکانت تست (حجم و مدت توسط پنل تعیین می‌شود)"""
        data = {
            "trial": True,
            "name": name,
        }
        logger.info(f"Creating trial client: {name}")
        return await self._request("POST", "/clients", json_data=data, idempotency_key=idempotency_key)

    async def list_clients(self, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """لیست کاربران (برای همگام‌سازی دسته‌جمعی)"""
        return await self._request("GET", f"/clients?page={page}&size={size}")