import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import set_setting, get_setting, log_event
from rich import premium_emoji
from utils import rate_limiter

logger = logging.getLogger(__name__)
router = Router(name="admin_sales")


# ============================================================
# HELPERS
# ============================================================

def _emoji_id(key: str) -> str | None:
    if not PREMIUM_EMOJI_ENABLED:
        return None
    return PREMIUM_EMOJIS.get(key)


def _button(text: str, callback_data: str, style: str = "primary", emoji_key: str = None) -> InlineKeyboardButton:
    kwargs = {"text": text, "callback_data": callback_data, "style": style}
    eid = _emoji_id(emoji_key) if emoji_key else None
    if eid:
        kwargs["icon_custom_emoji_id"] = eid
    return InlineKeyboardButton(**kwargs)


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_answer(message, text, markup=None):
    try:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        logger.exception("Could not send sales message.")


# ============================================================
# CLOSE SALES
# ============================================================

@router.callback_query(F.data == "admin:sales:close")
async def close_sales(callback: CallbackQuery):
    user = callback.from_user
    if not user or user.id != ADMIN_ID:
        log_event("SECURITY", "unauthorized_sales_access", user.id if user else 0, "close_sales")
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    set_setting("sales_open", "0")
    log_event("INFO", "sales_closed", user.id)

    text = (
        f"{premium_emoji('false', '❌')} "
        "<b>فروش با موفقیت بسته شد.</b>\n\n"
        "از این پس کاربران نمی‌توانند سرویس جدید خریداری کنند."
    )

    markup = _keyboard([
        [_button("باز کردن فروش", "admin:sales:open", "success", "true")],
        [_button("بازگشت به پنل", "admin:main", "danger", "back")],
    ])

    await _safe_answer(callback.message, text, markup)
    await callback.answer()


# ============================================================
# OPEN SALES
# ============================================================

@router.callback_query(F.data == "admin:sales:open")
async def open_sales(callback: CallbackQuery):
    user = callback.from_user
    if not user or user.id != ADMIN_ID:
        log_event("SECURITY", "unauthorized_sales_access", user.id if user else 0, "open_sales")
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    set_setting("sales_open", "1")
    log_event("INFO", "sales_opened", user.id)

    text = (
        f"{premium_emoji('true', '✅')} "
        "<b>فروش با موفقیت باز شد.</b>\n\n"
        "از این پس کاربران می‌توانند سرویس جدید خریداری کنند."
    )

    markup = _keyboard([
        [_button("بستن فروش", "admin:sales:close", "danger", "false")],
        [_button("بازگشت به پنل", "admin:main", "danger", "back")],
    ])

    await _safe_answer(callback.message, text, markup)
    await callback.answer()