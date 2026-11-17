import time
import logging
from collections import deque
from typing import Callable, Dict, Any, Awaitable, Deque, FrozenSet, Tuple

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from config import DEV_ID, ADMIN_ID
from dev.alerts import send_alert
from database import is_banned, log_event, get_receipt_admin_id

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# تنظیمات
# ------------------------------------------------------------

# کال‌بک‌هایی که ادمین رسید هم می‌تواند بزند (تأیید/رد رسید)
RECEIPT_CALLBACK_PREFIX = "admin:pay:"

# الگوهای واقعاً خطرناک. (الگوهای عمومی مثل "../" و "curl " حذف شدند؛
# در پیام‌های عادی کاربر هم پیش می‌آمدند و هیچ سطح حمله‌ای در بات ندارند.)
DANGEROUS_PATTERNS: Tuple[str, ...] = (
    "<script", "javascript:", "onerror=", "union select",
    "drop table", "insert into", "exec(", "eval(", "__import__",
    "os.system", "subprocess", "rm -rf", "/etc/passwd",
)

TRUSTED_CACHE_TTL = 10        # ثانیه
CLEANUP_INTERVAL = 300        # ثانیه؛ پاکسازی کاربران غیرفعال از حافظه
REPORT_THROTTLE = 60          # ثانیه؛ حداکثر یک هشدار به‌ازای هر (کاربر، دلیل)
MAX_TRACKED_USERS = 50_000    # سقف سخت برای جلوگیری از رشد حافظه


class BehaviorGuardMiddleware(BaseMiddleware):
    """
    سد دفاعی برای تشخیص اسپم، دسترسی غیرمجاز و ورودی خطرناک.

    * ادمین اصلی، ادمین رسید و دولوپر «معتبر» هستند و از محدودیت نرخ و
      فیلتر متن معاف‌اند (ولی بن‌شدن برای بقیه همچنان اعمال می‌شود).
    * ادمین رسید فقط به کال‌بک‌های `admin:pay:` دسترسی دارد، نه پنل مدیریت.
    * هیچ درخواستی بی‌صدا حذف نمی‌شود: کاربر برای کال‌بک‌ها پاسخ می‌گیرد.
    * وضعیت نرخ بین همهٔ نمونه‌ها (پیام و کال‌بک) مشترک است.
    """

    # وضعیت مشترک بین نمونه‌ها: bot.py دو نمونه می‌سازد (message و callback)
    _timestamps: Dict[int, Deque[float]] = {}
    _last_cleanup: float = 0.0
    _last_report: Dict[Tuple[int, str], float] = {}
    _trusted_cache: FrozenSet[int] = frozenset()
    _trusted_expiry: float = 0.0

    def __init__(self, rate_window: int = 2, rate_max: int = 5):
        self.rate_window = rate_window
        self.rate_max = rate_max

    # --------------------------------------------------------
    # کاربران معتبر (با کش کوتاه‌مدت تا هر درخواست کوئری نزند)
    # --------------------------------------------------------

    @classmethod
    def _trusted_ids(cls) -> FrozenSet[int]:
        now = time.monotonic()
        if now < cls._trusted_expiry:
            return cls._trusted_cache
        ids = {int(ADMIN_ID)}
        if DEV_ID:
            ids.add(int(DEV_ID))
        try:
            ids.add(int(get_receipt_admin_id(ADMIN_ID)))
        except Exception:
            logger.exception("Could not read receipt admin id")
        cls._trusted_cache = frozenset(ids)
        cls._trusted_expiry = now + TRUSTED_CACHE_TTL
        return cls._trusted_cache

    @classmethod
    def invalidate_trusted_cache(cls) -> None:
        """بعد از تغییر ادمین رسید صدا زده شود تا فوراً اعمال شود."""
        cls._trusted_expiry = 0.0

    # --------------------------------------------------------
    # نرخ درخواست
    # --------------------------------------------------------

    @classmethod
    def _cleanup(cls, now: float, window: float) -> None:
        if now - cls._last_cleanup < CLEANUP_INTERVAL and len(cls._timestamps) < MAX_TRACKED_USERS:
            return
        cls._last_cleanup = now
        for uid in [u for u, q in cls._timestamps.items() if not q or now - q[-1] > window]:
            cls._timestamps.pop(uid, None)
        for key in [k for k, t in cls._last_report.items() if now - t > REPORT_THROTTLE]:
            cls._last_report.pop(key, None)

    def _is_spamming(self, user_id: int) -> bool:
        now = time.time()
        self._cleanup(now, self.rate_window)
        queue = self._timestamps.setdefault(user_id, deque())
        while queue and now - queue[0] >= self.rate_window:
            queue.popleft()
        if len(queue) >= self.rate_max:
            return True
        queue.append(now)
        return False

    # --------------------------------------------------------
    # پاسخ به کاربر (بدون حذف بی‌صدا)
    # --------------------------------------------------------

    @staticmethod
    async def _notify(event: TelegramObject, text: str) -> None:
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=False)
        except Exception:
            logger.debug("Could not answer blocked callback", exc_info=True)

    # --------------------------------------------------------
    # اصلی
    # --------------------------------------------------------

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        trusted = self._trusted_ids()
        is_trusted = user.id in trusted

        # ۱. کاربر مسدود
        if user.id not in (ADMIN_ID, DEV_ID) and is_banned(user.id):
            await self._notify(event, "⛔ شما از استفاده از ربات محروم شده‌اید.")
            return

        text = ""
        callback_data = ""
        if isinstance(event, Message) and event.text:
            text = event.text
        elif isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            text = callback_data

        # ۲. دسترسی به بخش ادمین
        is_admin_area = text.startswith("/admin") or callback_data.startswith("admin:")
        if is_admin_area and user.id != ADMIN_ID:
            receipt_ok = (
                user.id in trusted
                and callback_data.startswith(RECEIPT_CALLBACK_PREFIX)
            )
            if not receipt_ok:
                await self._report_and_block(
                    user.id, "تلاش برای دسترسی به پنل ادمین", f"متن/کالبک: {text[:100]}"
                )
                await self._notify(event, "⛔ دسترسی غیرمجاز.")
                return

        # ۳. کاربران معتبر از محدودیت نرخ و فیلتر متن معاف‌اند
        if is_trusted:
            return await handler(event, data)

        # ۴. محدودیت نرخ
        if self._is_spamming(user.id):
            await self._report_and_block(
                user.id, "اسپم (ارسال پیام پشت سر هم)",
                f"بیش از {self.rate_max} درخواست در {self.rate_window} ثانیه",
            )
            await self._notify(event, "⏳ لطفاً کمی آهسته‌تر.")
            return

        # ۵. الگوهای خطرناک
        lower_text = text.lower()
        for pattern in DANGEROUS_PATTERNS:
            if pattern in lower_text:
                await self._report_and_block(
                    user.id, "ورودی خطرناک (احتمال تزریق/اسکریپت)",
                    f"الگو: {pattern} | متن: {text[:100]}",
                )
                await self._notify(event, "⚠️ ورودی نامعتبر.")
                return

        return await handler(event, data)

    # --------------------------------------------------------
    # گزارش (با throttle تا دولوپر اسپم نشود)
    # --------------------------------------------------------

    async def _report_and_block(self, user_id: int, reason: str, detail: str):
        now = time.time()
        key = (user_id, reason)
        if now - self._last_report.get(key, 0.0) < REPORT_THROTTLE:
            return
        self._last_report[key] = now

        alert_text = (
            f"🚨 <b>هشدار امنیتی</b>\n\n"
            f"👤 <b>آیدی کاربر:</b> <code>{user_id}</code>\n"
            f"⚠️ <b>نوع رفتار:</b> {reason}\n"
            f"📝 <b>توضیحات:</b> {detail}\n"
            f"🕒 <b>زمان:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_alert("SECURITY", alert_text)
        log_event("SECURITY", "behavior_guard_blocked", user_id, f"{reason} | {detail}")
