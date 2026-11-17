import time
import logging
from typing import Callable, Dict, Any, Awaitable, Optional
from collections import defaultdict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from config import DEV_ID, ADMIN_ID
from dev.alerts import send_alert
from database import is_banned, log_event

logger = logging.getLogger(__name__)

class BehaviorGuardMiddleware(BaseMiddleware):
    """
    سد دفاعی نهایی برای تشخیص نفوذ، اسپم و رفتار عجیب.
    هر مورد مشکوک با آیدی کاربر و توضیح به DEV_ID ارسال می‌شود.
    """

    def __init__(self, rate_window: int = 2, rate_max: int = 5):
        self.rate_window = rate_window
        self.rate_max = rate_max
        # دیکشنری برای ذخیره زمان آخرین پیام‌های هر کاربر (برای تشخیص اسپم)
        self.user_timestamps: Dict[int, list] = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        # ۱. اگر کاربر قبلاً بن شده، نادیده بگیر
        if is_banned(user.id):
            return

        # استخراج متن یا دیتای کال‌بک
        text = ""
        callback_data = ""
        if isinstance(event, Message) and event.text:
            text = event.text
        elif isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            text = callback_data

        # ۲. تشخیص تلاش برای دسترسی غیرمجاز به ادمین (اگر کاربر ادمین نباشد)
        if user.id != ADMIN_ID:
            # اگر کاربر عادی سعی کند دستور /admin یا کال‌بک‌های ادمین را بزند
            if text.startswith("/admin") or callback_data.startswith("admin:"):
                await self._report_and_block(
                    user.id,
                    "تلاش برای دسترسی به پنل ادمین",
                    f"متن/کالبک: {text[:100]}"
                )
                return  # مسدود کردن کامل

        # ۳. تشخیص اسپم (Rate Limit سخت‌گیرانه)
        now = time.time()
        # پاکسازی زمان‌های قدیمی
        self.user_timestamps[user.id] = [
            ts for ts in self.user_timestamps[user.id] if now - ts < self.rate_window
        ]
        if len(self.user_timestamps[user.id]) >= self.rate_max:
            await self._report_and_block(
                user.id,
                "اسپم (ارسال پیام پشت سر هم)",
                f"تعداد پیام در {self.rate_window} ثانیه: {len(self.user_timestamps[user.id])}"
            )
            return
        self.user_timestamps[user.id].append(now)

        # ۴. تشخیص الگوهای خطرناک در متن (تزریق SQL، اسکریپت و...)
        dangerous_patterns = [
            "<script", "javascript:", "onerror=", "union select",
            "drop table", "insert into", "exec(", "eval(", "__import__",
            "os.system", "subprocess", "curl ", "wget ", "rm -rf", "chmod ",
            "../", "/etc/passwd"
        ]
        lower_text = text.lower()
        for pattern in dangerous_patterns:
            if pattern in lower_text:
                await self._report_and_block(
                    user.id,
                    "ورودی خطرناک (احتمال تزریق/اسکریپت)",
                    f"الگو: {pattern} | متن: {text[:100]}"
                )
                return

        # ۵. اگر همه چیز امن بود، اجازه عبور بده
        return await handler(event, data)

    async def _report_and_block(self, user_id: int, reason: str, detail: str):
        """ارسال هشدار به دولوپر و ثبت لاگ امنیتی."""
        alert_text = (
            f"🚨 <b>هشدار امنیتی</b>\n\n"
            f"👤 <b>آیدی کاربر:</b> <code>{user_id}</code>\n"
            f"⚠️ <b>نوع رفتار:</b> {reason}\n"
            f"📝 <b>توضیحات:</b> {detail}\n"
            f"🕒 <b>زمان:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        # ارسال به دولوپر
        send_alert("SECURITY", alert_text)
        # ثبت در لاگ دیتابیس
        log_event("SECURITY", "behavior_guard_blocked", user_id, f"{reason} | {detail}")