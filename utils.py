import re
import time
import logging
from typing import Optional, Dict, Any, Tuple
from collections import defaultdict

from config import (
    MAX_DEVICES_DEFAULT, MAX_MONTHS, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX,
    DEFAULT_PRICES_TOMAN, DEFAULT_PRICES_STARS, LEVEL_NAMES
)

logger = logging.getLogger(__name__)

# ============================================================
# اعتبارسنجی ورودی (Input Validation)
# ============================================================

# الگوی امن برای شناسه کانفیگ (فقط حروف، اعداد و خط تیره)
CONFIG_NAME_PATTERN = re.compile(r"^iMan(Test)?-\d+$", re.IGNORECASE)
# الگوی امن برای کد تخفیف (فقط حروف بزرگ، اعداد و خط تیره/زیرخط)
DISCOUNT_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{3,32}$")
# الگوی امن برای مبلغ (فقط عدد صحیح مثبت)
AMOUNT_PATTERN = re.compile(r"^\d{1,9}$")


def validate_config_name(name: str) -> bool:
    """بررسی اینکه نام کانفیگ با الگوی iMan-N یا iManTest-N مطابقت دارد."""
    if not name:
        return False
    return bool(CONFIG_NAME_PATTERN.match(name.strip()))


def validate_discount_code(code: str) -> bool:
    """بررسی فرمت کد تخفیف (فقط حروف بزرگ، اعداد، _ و -)."""
    if not code:
        return False
    return bool(DISCOUNT_CODE_PATTERN.match(code.strip().upper()))


def validate_amount(amount_str: str, min_val: int = 10_000, max_val: int = 50_000_000) -> Optional[int]:
    """
    اعتبارسنجی مبلغ ورودی کاربر (برای شارژ کیف پول).
    خروجی: مقدار عددی در صورت معتبر بودن، در غیر این صورت None.
    """
    if not amount_str or not AMOUNT_PATTERN.match(amount_str.strip()):
        return None
    try:
        amount = int(amount_str.strip())
    except ValueError:
        return None
    if amount < min_val or amount > max_val:
        return None
    return amount


# ============================================================
# Rate Limiting (ضد اسپم)
# ============================================================

class RateLimiter:
    """
    محدودکننده نرخ درخواست به صورت in-memory.
    برای هر کاربر، زمان آخرین درخواست‌ها را نگه می‌دارد.
    """
    def __init__(self, window: int = RATE_LIMIT_WINDOW, max_requests: int = RATE_LIMIT_MAX):
        self.window = window
        self.max_requests = max_requests
        # دیکشنری: user_id -> list of timestamps
        self.user_requests: Dict[int, list] = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        """بررسی اینکه آیا کاربر مجاز به ارسال درخواست جدید است یا نه."""
        now = time.time()
        # پاک‌سازی درخواست‌های قدیمی‌تر از پنجره زمانی
        self.user_requests[user_id] = [
            ts for ts in self.user_requests[user_id] if now - ts < self.window
        ]
        if len(self.user_requests[user_id]) >= self.max_requests:
            return False
        self.user_requests[user_id].append(now)
        return True

    def cleanup(self):
        """پاک‌سازی دوره‌ای برای جلوگیری از رشد حافظه."""
        now = time.time()
        for uid in list(self.user_requests.keys()):
            self.user_requests[uid] = [
                ts for ts in self.user_requests[uid] if now - ts < self.window
            ]
            if not self.user_requests[uid]:
                del self.user_requests[uid]


# یک نمونه سراسری برای استفاده در هندلرها
rate_limiter = RateLimiter()


# ============================================================
# محاسبات قیمت (سمت سرور - هرگز به کلاینت اعتماد نکن)
# ============================================================

def calculate_price_toman(volume_gb: float, days: int, devices: int, is_unlimited: bool = False, unlimited_price: int = 0) -> int:
    """سازگاری با کد قدیمی؛ قیمت‌ها از pricing.py (تنظیمات ادمین) خوانده می‌شوند."""
    from pricing import order_price_toman
    return order_price_toman("unl150" if is_unlimited else "vol", volume_gb, days, devices)


def calculate_price_stars(volume_gb: float, days: int, devices: int, is_unlimited: bool = False, unlimited_stars: int = 0) -> int:
    from pricing import order_price_stars
    return order_price_stars("unl150" if is_unlimited else "vol", volume_gb, days, devices)


# ============================================================
# سطح‌بندی کاربران
# ============================================================

def get_user_level(total_purchases: int) -> Tuple[str, str]:
    """
    تعیین سطح کاربر بر اساس تعداد کل خریدها.
    خروجی: (کلید سطح، نام سطح)
    """
    if total_purchases <= 1:
        return "new", LEVEL_NAMES.get("new", "تازه‌وارد")
    elif total_purchases <= 5:
        return "prime", LEVEL_NAMES.get("prime", "پرایم")
    elif total_purchases <= 10:
        return "royal", LEVEL_NAMES.get("royal", "رویال")
    elif total_purchases <= 20:
        return "diamond", LEVEL_NAMES.get("diamond", "دایموند")
    else:
        return "legendary", LEVEL_NAMES.get("legendary", "لجندری")


# ============================================================
# ابزارهای کمکی برای زمان و نمایش
# ============================================================

def format_bytes(bytes_val: int) -> str:
    """تبدیل بایت به رشته خوانا (GB/MB)."""
    if bytes_val is None:
        return "نامشخص"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = bytes_val / (1024 ** 2)
    return f"{mb:.2f} MB"


def format_days_remaining(expires_at: Optional[int]) -> str:
    """محاسبه روزهای باقی‌مانده تا انقضا."""
    if not expires_at:
        return "بدون انقضا"
    remaining = expires_at - int(time.time())
    if remaining <= 0:
        return "منقضی شده"
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    if days > 0:
        return f"{days} روز و {hours} ساعت"
    return f"{hours} ساعت"


def generate_idempotency_key(prefix: str = "order") -> str:
    """تولید کلید یکتا برای Idempotency-Key (برای درخواست‌های هزینه‌دار PAXIVA)."""
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:16]}"