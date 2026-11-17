import logging

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import get_setting, record_user_activity
from rich import premium_emoji
from utils import rate_limiter
import ui

logger = logging.getLogger(__name__)
router = Router(name="support")


def support_keyboard(support_url: str) -> InlineKeyboardMarkup:
    """صفحه پشتیبانی با دکمه‌های اینلاین رنگی و Premium Emoji."""

    if not PREMIUM_EMOJI_ENABLED:
        raise RuntimeError("Premium Emoji is disabled in config.py")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ارتباط با پشتیبانی",
                    url=support_url,
                    style="success",
                    icon_custom_emoji_id=PREMIUM_EMOJIS["ask"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="بازگشت",
                    callback_data="menu:main",
                    style="danger",
                    icon_custom_emoji_id=PREMIUM_EMOJIS["back"],
                )
            ],
        ]
    )


@router.callback_query(F.data == "menu:support")
async def show_support(callback: CallbackQuery):
    user = callback.from_user

    if not user or not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    record_user_activity(user.id)

    support_url = get_setting(
        "support_url",
        "https://t.me/YourSupport",
    )

    text = (
        f"{premium_emoji('ask', '◈')} "
        "<b>پشتیبانی آی‌من نتورک</b>\n\n"
        "برای ارتباط با تیم پشتیبانی، "
        "روی دکمه زیر بزنید.\n\n"
        "پاسخگویی در اسرع وقت انجام می‌شود."
    )

    try:
        # همان پیام اصلی به صفحه‌ی پشتیبانی ویرایش می‌شود.
        await ui.show(
            callback,
            text,
            support_keyboard(support_url),
        )

    except Exception:
        logger.exception("Failed to show support menu")

    await callback.answer()
