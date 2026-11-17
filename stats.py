import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import (
    get_admin_stats,
    get_top_services,
    get_pending_payments,
    get_payment_stats,
    log_event,
)
from rich import premium_emoji

logger = logging.getLogger(__name__)
router = Router(name="admin_stats")


# ============================================================
# HELPERS
# ============================================================

def _emoji_id(key: str) -> str | None:
    if not PREMIUM_EMOJI_ENABLED:
        return None
    return PREMIUM_EMOJIS.get(key)


def _button(
    text: str,
    callback_data: str,
    style: str = "primary",
    emoji_key: str | None = None,
) -> InlineKeyboardButton:
    kwargs = {"text": text, "callback_data": callback_data, "style": style}
    eid = _emoji_id(emoji_key) if emoji_key else None
    if eid:
        kwargs["icon_custom_emoji_id"] = eid
    return InlineKeyboardButton(**kwargs)


async def _safe_answer(message, text, markup=None):
    try:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        logger.exception("Could not send stats message.")


# ============================================================
# MAIN STATS
# ============================================================

@router.callback_query(F.data == "admin:stats:menu")
async def show_stats(callback: CallbackQuery):
    user = callback.from_user
    if not user or user.id != ADMIN_ID:
        log_event("SECURITY", "unauthorized_stats", user.id if user else 0)
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    try:
        stats = get_admin_stats()
    except Exception:
        logger.exception("Could not load admin stats.")
        await callback.answer("خطا در دریافت آمار.", show_alert=True)
        return

    top_services = []
    try:
        top_services = get_top_services(limit=3)
    except Exception:
        logger.exception("Could not load top services.")

    payment_stats = {}
    try:
        payment_stats = get_payment_stats()
    except Exception:
        logger.exception("Could not load payment stats.")

    top_str = "نامشخص"
    if top_services:
        lines = []
        for item in top_services:
            kind_fa = "حجمی" if item["kind"] == "vol" else "نامحدود"
            vol = item["volume_gb"] if item["volume_gb"] > 0 else "بی‌نهایت"
            lines.append(f"{premium_emoji('blue_dot', '•')} {kind_fa} {vol}G — {item['c']} فروش")
        top_str = "\n".join(lines)

    text = (
        f"{premium_emoji('diamond', '📊')} "
        "<b>آمار کلی ربات</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"{premium_emoji('telegram', '👥')} "
        "<b>کاربران</b>\n"
        f"کل کاربران: <b>{stats['total_users']:,}</b>\n"
        f"کاربران بن‌شده: <b>{stats['banned_users']:,}</b>\n"
        f"کاربران جدید امروز: <b>{stats['new_users_today']:,}</b>\n\n"

        f"{premium_emoji('shop', '🛒')} "
        "<b>فروش</b>\n"
        f"کل خریدها: <b>{stats['total_purchases']:,}</b>\n"
        f"فروش امروز: <b>{stats['sales_today']:,}</b>\n"
        f"سرویس‌های فعال: <b>{stats['active_subscriptions']:,}</b>\n\n"

        f"{premium_emoji('wallet', '💰')} "
        "<b>مالی</b>\n"
        f"کل موجودی کاربران: <b>{stats['total_balance']:,} تومان</b>\n"
        f"کل درآمد (تومان): <b>{stats['total_revenue_toman']:,} تومان</b>\n"
        f"درآمد امروز: <b>{stats['revenue_today_toman']:,} تومان</b>\n"
        f"کل Stars دریافتی: <b>{stats['total_stars']:,}</b>\n\n"

        f"{premium_emoji('light', '📥')} "
        "<b>رسیدهای در انتظار</b>\n"
        f"در انتظار بررسی: <b>{stats.get('pending_payments', 0):,}</b>\n"
        f"تأییدشده: <b>{payment_stats.get('approved', 0):,}</b>\n"
        f"ردشده: <b>{payment_stats.get('rejected', 0):,}</b>\n"
        f"مجموع تأییدشده: <b>{payment_stats.get('approved_amount', 0):,} تومان</b>\n\n"

        f"{premium_emoji('stars', '🏆')} "
        "<b>پرفروش‌ترین سرویس‌ها</b>\n"
        f"{top_str}"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            _button("بازگشت به پنل", "admin:main", "danger", "back"),
        ],
    ])

    await _safe_answer(callback.message, text, markup)
    await callback.answer()