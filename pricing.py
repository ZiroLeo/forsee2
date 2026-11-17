"""
pricing.py
----------
محاسبهٔ قیمت سفارش‌ها با قیمت‌های قابل‌تغییر از پنل ادمین (prices.py).

کلیدهای تنظیمات دقیقاً همان کلیدهای prices.py هستند:
هر آیتم دو قیمت مستقل دارد: تومان و استارز. اگر مقداری در دیتابیس نبود،
مقدار پیش‌فرض همین فایل (مطابق prices.py) استفاده می‌شود.

فرمول‌ها:
* حجمی (محدود به زمان):
    حجم × قیمت هر گیگ + (ماه‌های بیش از ۱ماه × ماه اضافه حجمی) + (یوزر بیش از ۲ تا × یوزر اضافه حجمی)
* حجمی بدون محدودیت زمانی (days = 0):
    حجم × قیمت هر گیگ بدون محدودیت + (یوزر بیش از ۲ تا × یوزر اضافه حجمی)
* نامحدود ۱۵۰ گیگ:
    قیمت پایه + (ماه‌های بیش از ۱ماه × ماه اضافه نامحدود) + (یوزر اضافه × یوزر اضافه نامحدود)
* نامحدود بی‌نهایت:
    قیمت پایه + (یوزر اضافه × یوزر اضافه نامحدود)
"""

import logging

from config import DEFAULT_PRICES_TOMAN, DEFAULT_PRICES_STARS, MAX_DEVICES_DEFAULT
from database import get_setting

logger = logging.getLogger(__name__)

# آیتم -> (کلید تومان، کلید استارز، پیش‌فرض تومان، پیش‌فرض استارز)
PRICE_KEYS: dict[str, tuple[str, str, int, int]] = {
    "per_gb":       ("price_per_gb",           "stars_per_gb",           5000,  2),
    "vol_device":   ("per_device_vol_price",   "per_device_vol_stars",   15000, 5),
    "unl_device":   ("per_device_unl_price",   "per_device_unl_stars",   15000, 5),
    "vol_month":    ("per_month_vol_price",    "per_month_vol_stars",    30000, 10),
    "unl_month":    ("per_month_unl_price",    "per_month_unl_stars",    30000, 10),
    "no_expiry_gb": ("price_per_gb_no_expiry", "stars_per_gb_no_expiry", 7000,  3),
}

# قیمت پایهٔ سرویس‌های نامحدود (فعلاً از تنظیمات یا config)
_UNLIMITED_KEYS = {
    "unl150": ("unlimited_150_price", "unlimited_150_stars", "unlimited_150"),
    "unl_inf": ("unlimited_inf_price", "unlimited_inf_stars", "unlimited_inf"),
}


def _read_int(key: str, default: int) -> int:
    try:
        value = int(str(get_setting(key, str(default))).strip())
        return value if value >= 0 else default
    except (TypeError, ValueError):
        logger.warning("Invalid price setting %s", key)
        return default
    except Exception:
        logger.exception("Could not read price setting %s", key)
        return default


def get_price(item: str, currency: str) -> int:
    if item not in PRICE_KEYS or currency not in ("toman", "stars"):
        raise KeyError(f"unknown price {item}/{currency}")
    toman_key, stars_key, default_toman, default_stars = PRICE_KEYS[item]
    if currency == "toman":
        return _read_int(toman_key, default_toman)
    return _read_int(stars_key, default_stars)


def _unlimited_base(key: str, currency: str) -> int:
    toman_key, stars_key, config_key = _UNLIMITED_KEYS[key]
    if currency == "toman":
        return _read_int(toman_key, int(DEFAULT_PRICES_TOMAN.get(config_key, 0)))
    return _read_int(stars_key, int(DEFAULT_PRICES_STARS.get(config_key, 0)))


def _calc(currency: str, key: str, volume: float, days: int, devices: int) -> int:
    extra_devices = max(0, int(devices) - MAX_DEVICES_DEFAULT)
    extra_months = max(0, int(days) // 30 - 1)

    if key in _UNLIMITED_KEYS:
        total = _unlimited_base(key, currency) + extra_devices * get_price("unl_device", currency)
        if key == "unl150":
            total += extra_months * get_price("unl_month", currency)
        return total

    if int(days) <= 0:  # بدون محدودیت زمانی
        return int(volume * get_price("no_expiry_gb", currency)) + extra_devices * get_price("vol_device", currency)

    return (
        int(volume * get_price("per_gb", currency))
        + extra_months * get_price("vol_month", currency)
        + extra_devices * get_price("vol_device", currency)
    )


def order_price_toman(key: str, volume: float, days: int, devices: int) -> int:
    return _calc("toman", key, volume, days, devices)


def order_price_stars(key: str, volume: float, days: int, devices: int) -> int:
    return _calc("stars", key, volume, days, devices)
