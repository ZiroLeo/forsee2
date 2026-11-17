import re
import logging
from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from database import log_event, is_banned
from dev.alerts import send_alert

logger = logging.getLogger(__name__)

# الگوهای خطرناک (تزریق SQL، اسکریپت، دستور شل)
DANGEROUS_PATTERNS = [
    r"<script", r"javascript:", r"onerror=", r"onload=",
    r"union\s+select", r"drop\s+table", r"insert\s+into",
    r"exec\s*\(", r"eval\s*\(", r"__import__",
    r"os\.system", r"subprocess", r"curl\s+",
    r"wget\s+", r"rm\s+-rf", r"chmod\s+",
    r"\.\./", r"etc/passwd",
]


class SecurityMiddleware(BaseMiddleware):
    """بررسی امنیتی پیام‌ها و کال‌بک‌ها قبل از رسیدن به هندلرها."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        if is_banned(user.id):
            return

        text = ""
        if isinstance(event, Message) and event.text:
            text = event.text
        elif isinstance(event, CallbackQuery):
            text = event.data or ""

        if text:
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    # ثبت لاگ امنیتی در دیتابیس
                    log_event(
                        "SECURITY",
                        "dangerous_input",
                        user.id,
                        f"pattern={pattern}; text={text[:300]}",
                    )

                    # ارسال هشدار به دولوپر
                    send_alert(
                        "SECURITY",
                        f"کاربر: {user.id}\n"
                        f"متن: {text[:300]}\n"
                        f"الگو: {pattern}"
                    )

                    # مسدود کردن (بدون پاسخ به کاربر)
                    return

        return await handler(event, data)