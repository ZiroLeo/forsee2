
import logging
import html
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from aiogram.exceptions import TelegramBadRequest

from config import BOT_USERNAME, PREMIUM_EMOJIS
from database import (
    get_referral_stats,
    get_user_referrals,
    get_referral_details,
    record_user_activity,
    log_event,
    get_user,
)
from rich import premium_emoji
from utils import rate_limiter
import ui

logger = logging.getLogger(__name__)
router = Router(name="referral")


# ============================================================
# تنظیمات
# ============================================================

COMMISSION_PERCENT = 5
MAX_REFERRALS_DISPLAY = 10
MAX_EARNINGS_DISPLAY = 10


# ============================================================
# ابزارهای کمکی
# ============================================================

def _emoji(key: str, fallback: str = "●") -> str:
    try:
        return premium_emoji(key, fallback)
    except Exception:
        logger.exception("Premium emoji error: %s", key)
        return fallback


def _button(
    text: str,
    callback_data: str,
    emoji_key: str,
    style: str = "primary",
) -> InlineKeyboardButton:
    """
    ساخت دکمه Inline با Premium Emoji و رنگ مشخص.
    """

    emoji_id = PREMIUM_EMOJIS.get(emoji_key)

    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=emoji_id,
        style=style,
    )


def _referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "کپی لینک دعوت",
                    "referral:copy",
                    "telegram",
                    "success",
                ),
                _button(
                    "آمار تفصیلی",
                    "referral:stats",
                    "light",
                    "primary",
                ),
            ],
            [
                _button(
                    "بازگشت به منوی اصلی",
                    "menu:main",
                    "back",
                    "danger",
                ),
            ],
        ]
    )


def _referral_stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "بازگشت به رفرال",
                    "menu:referral",
                    "back",
                    "danger",
                ),
            ],
        ]
    )


def _make_referral_link(user_id: int) -> str:
    username = BOT_USERNAME.strip().lstrip("@")
    return f"https://t.me/{username}?start=REF_{user_id}"


def _format_date(value) -> str:
    if value is None:
        return "نامشخص"

    try:
        timestamp = int(value)
        return datetime.fromtimestamp(timestamp).strftime(
            "%Y/%m/%d"
        )
    except (TypeError, ValueError, OSError):
        return str(value)


def _format_source(source: str) -> str:
    sources = {
        "purchase": "خرید سرویس",
        "renew": "تمدید سرویس",
        "renewal": "تمدید سرویس",
    }

    return sources.get(source, "تراکنش")


async def _answer_callback(
    callback: CallbackQuery,
    text: str = None,
    show_alert: bool = False,
):
    try:
        await callback.answer(
            text=text,
            show_alert=show_alert,
        )
    except Exception:
        logger.warning("Could not answer callback.")


async def _edit_or_send(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
):
    # از روی پیام اصلی (Rich) -> پیام جدید؛ از روی پیام Inline -> ویرایش.
    await ui.show(callback, text, keyboard)


def _valid_callback_user(callback: CallbackQuery) -> bool:
    return bool(callback.from_user)


# ============================================================
# صفحه اصلی رفرال
# ============================================================

@router.callback_query(F.data == "menu:referral")
async def show_referral(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await _answer_callback(callback)
        return

    if not rate_limiter.is_allowed(user.id):
        await _answer_callback(
            callback,
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        db_user = get_user(user.id)

        if not db_user:
            await _answer_callback(
                callback,
                "حساب کاربری شما پیدا نشد.",
                show_alert=True,
            )
            return

        record_user_activity(user.id)

        stats = get_referral_stats(user.id) or {}

        referral_count = int(stats.get("count", 0))
        total_earnings = int(
            stats.get("total_earnings", 0)
        )

        referral_link = _make_referral_link(user.id)

        text = (
            f"{_emoji('referral', '●')} "
            "<b>سیستم دعوت و درآمد آی‌من نتورک</b>\n\n"

            "با دعوت دوستان خود، از خرید و تمدید "
            f"آن‌ها <b>{COMMISSION_PERCENT}٪ پورسانت</b> "
            "دریافت کنید.\n\n"

            f"{_emoji('light', '●')} "
            "<b>آمار حساب شما</b>\n"
            f"تعداد دعوت‌شدگان: "
            f"<b>{referral_count:,}</b> نفر\n"
            f"مجموع پورسانت دریافتی: "
            f"<b>{total_earnings:,}</b> تومان\n\n"

            "<b>نحوه محاسبه پورسانت</b>\n"
            f"از هر خرید یا تمدید مستقیم، "
            f"<b>{COMMISSION_PERCENT}٪</b> به کیف پول شما اضافه می‌شود.\n\n"

            "<b>لینک اختصاصی شما</b>\n"
            f"<code>{html.escape(referral_link)}</code>\n\n"

            "لینک را برای دوستانتان ارسال کنید."
        )

        await _answer_callback(callback)

        await _edit_or_send(
            callback,
            text,
            _referral_keyboard(),
        )

    except Exception:
        logger.exception(
            "Failed to show referral page for user %s",
            user.id,
        )

        await _answer_callback(
            callback,
            "خطا در دریافت اطلاعات رفرال.",
            show_alert=True,
        )


# ============================================================
# آمار تفصیلی
# ============================================================

@router.callback_query(F.data == "referral:stats")
async def show_referral_stats(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await _answer_callback(callback)
        return

    if not rate_limiter.is_allowed(user.id):
        await _answer_callback(
            callback,
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        record_user_activity(user.id)

        referrals = get_user_referrals(user.id) or []

        earnings = get_referral_details(
            user.id,
            limit=20,
        ) or []

        lines = [
            f"{_emoji('light', '●')} "
            "<b>آمار تفصیلی رفرال</b>\n"
        ]

        lines.append(
            f"<b>دعوت‌شدگان ({len(referrals):,} نفر)</b>"
        )

        if referrals:
            for referral in referrals[:MAX_REFERRALS_DISPLAY]:
                referred_id = html.escape(
                    str(referral.get("referred_id", "نامشخص"))
                )

                created_at = _format_date(
                    referral.get("created_at")
                )

                lines.append(
                    f"• <code>{referred_id}</code>"
                    f" — {created_at}"
                )

            remaining = (
                len(referrals) - MAX_REFERRALS_DISPLAY
            )

            if remaining > 0:
                lines.append(
                    f"و {remaining:,} دعوت‌شده دیگر"
                )
        else:
            lines.append("هنوز کسی با لینک شما وارد نشده است.")

        lines.append("\n<b>آخرین پورسانت‌ها</b>")

        if earnings:
            for earning in earnings[:MAX_EARNINGS_DISPLAY]:

                amount = int(earning.get("amount", 0))
                source = _format_source(
                    earning.get("source", "")
                )
                created_at = _format_date(
                    earning.get("created_at")
                )

                lines.append(
                    f"• <b>{amount:,}</b> تومان"
                    f" | {source}"
                    f" | {created_at}"
                )
        else:
            lines.append(
                "هنوز پورسانتی برای حساب شما ثبت نشده است."
            )

        text = "\n".join(lines)

        await _answer_callback(callback)

        await _edit_or_send(
            callback,
            text,
            _referral_stats_keyboard(),
        )

    except Exception:
        logger.exception(
            "Failed to show referral stats for user %s",
            user.id,
        )

        await _answer_callback(
            callback,
            "خطا در دریافت آمار رفرال.",
            show_alert=True,
        )


# ============================================================
# ارسال لینک اختصاصی
# ============================================================

@router.callback_query(F.data == "referral:copy")
async def copy_referral_link(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await _answer_callback(callback)
        return

    if not rate_limiter.is_allowed(user.id):
        await _answer_callback(
            callback,
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        if not get_user(user.id):
            await _answer_callback(
                callback,
                "حساب کاربری شما پیدا نشد.",
                show_alert=True,
            )
            return

        link = _make_referral_link(user.id)

        await _answer_callback(
            callback,
            "لینک اختصاصی شما ارسال شد.",
        )

        await callback.message.answer(
            (
                f"{_emoji('referral', '●')} "
                "<b>لینک اختصاصی دعوت شما</b>\n\n"
                f"<code>{html.escape(link)}</code>\n\n"
                "برای کپی کردن، روی لینک بالا لمس کنید."
            )
        )

    except Exception:
        logger.exception(
            "Failed to send referral link for user %s",
            user.id,
        )

        await _answer_callback(
            callback,
            "ارسال لینک با خطا مواجه شد.",
            show_alert=True,
        )
