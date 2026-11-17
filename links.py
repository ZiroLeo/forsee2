import logging
import re
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import get_setting, set_setting, log_event
from rich import premium_emoji

logger = logging.getLogger(__name__)
router = Router(name="admin_links")


class LinkStates(StatesGroup):
    waiting_support = State()
    waiting_guide = State()


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
        await message.answer(text, reply_markup=markup)
    except Exception:
        logger.exception("Could not send links message.")


def _is_valid_telegram_link(value: str) -> bool:
    """
    بررسی لینک یا آیدی معتبر تلگرام.
    - لینک: https://t.me/username
    - آیدی: @username
    - یوزرنیم بدون @: username
    """
    if not value:
        return False
    value = value.strip()

    # لینک کامل
    if value.startswith("https://t.me/"):
        pattern = r"^https://t\.me/[A-Za-z0-9_]{4,32}(/\d+)?$"
        return bool(re.fullmatch(pattern, value))

    # آیدی با @
    if value.startswith("@"):
        pattern = r"^@[A-Za-z0-9_]{4,32}$"
        return bool(re.fullmatch(pattern, value))

    # یوزرنیم ساده
    pattern = r"^[A-Za-z0-9_]{4,32}$"
    return bool(re.fullmatch(pattern, value))


def _normalize_telegram_link(value: str) -> str:
    """نرمال‌سازی به فرمت استاندارد https://t.me/..."""
    value = value.strip()
    if value.startswith("https://t.me/"):
        return value
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    return f"https://t.me/{value}"


# ============================================================
# MENU
# ============================================================

@router.callback_query(F.data == "admin:links:menu")
async def links_menu(callback: CallbackQuery):
    user = callback.from_user
    if not user or user.id != ADMIN_ID:
        log_event("SECURITY", "unauthorized_links_access", user.id if user else 0)
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    support = get_setting("support_url", "تنظیم نشده")
    guide = get_setting("guide_url", "تنظیم نشده")

    text = (
        f"{premium_emoji('world', '🔗')} "
        "<b>مدیریت لینک‌ها</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{premium_emoji('ask', '💬')} "
        f"<b>لینک پشتیبانی:</b>\n"
        f"<code>{support}</code>\n\n"
        f"{premium_emoji('quest', '📱')} "
        f"<b>لینک آموزش:</b>\n"
        f"<code>{guide}</code>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            _button("تغییر لینک پشتیبانی", "admin:links:support", "primary", "ask"),
        ],
        [
            _button("تغییر لینک آموزش", "admin:links:guide", "primary", "quest"),
        ],
        [
            _button("بازگشت", "admin:main", "danger", "back"),
        ],
    ])

    await _safe_answer(callback.message, text, markup)
    await callback.answer()


# ============================================================
# SUPPORT LINK
# ============================================================

@router.callback_query(F.data == "admin:links:support")
async def edit_support(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    await state.clear()
    await state.set_state(LinkStates.waiting_support)

    text = (
        f"{premium_emoji('ask', '💬')} "
        "<b>لینک جدید پشتیبانی را وارد کنید:</b>\n\n"
        "می‌توانید یکی از این فرمت‌ها را بفرستید:\n"
        "• <code>https://t.me/SupportID</code>\n"
        "• <code>@SupportID</code>\n"
        "• <code>SupportID</code>"
    )

    await _safe_answer(callback.message, text)
    await callback.answer()


@router.message(LinkStates.waiting_support)
async def receive_support(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ ورودی خالی است.")
        return

    if not _is_valid_telegram_link(raw):
        await message.answer(
            "❌ لینک یا آیدی نامعتبر است.\n\n"
            "فرمت‌های مجاز:\n"
            "• <code>https://t.me/SupportID</code>\n"
            "• <code>@SupportID</code>\n"
            "• <code>SupportID</code>"
        )
        return

    normalized = _normalize_telegram_link(raw)
    set_setting("support_url", normalized)
    log_event("INFO", "link_updated", ADMIN_ID, f"support_url={normalized}")

    await state.clear()
    await message.answer(
        f"✅ <b>لینک پشتیبانی به‌روزرسانی شد.</b>\n\n"
        f"<code>{normalized}</code>"
    )


# ============================================================
# GUIDE LINK
# ============================================================

@router.callback_query(F.data == "admin:links:guide")
async def edit_guide(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    await state.clear()
    await state.set_state(LinkStates.waiting_guide)

    text = (
        f"{premium_emoji('quest', '📱')} "
        "<b>لینک جدید آموزش را وارد کنید:</b>\n\n"
        "می‌توانید لینک پست کانال یا لینک آموزش را بفرستید:\n"
        "• <code>https://t.me/channel/123</code>\n"
        "• <code>https://t.me/GuideChannel</code>"
    )

    await _safe_answer(callback.message, text)
    await callback.answer()


@router.message(LinkStates.waiting_guide)
async def receive_guide(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            f"{premium_emoji('false', '❌')} <b>ورودی خالی است.</b>",
            parse_mode="HTML"
        )
        return

    if not _is_valid_telegram_link(raw):
        await message.answer(
            "❌ لینک نامعتبر است.\n\n"
            "فرمت‌های مجاز:\n"
            "• <code>https://t.me/channel/123</code>\n"
            "• <code>https://t.me/GuideChannel</code>"
        )
        return

    normalized = _normalize_telegram_link(raw)
    set_setting("guide_url", normalized)
    log_event("INFO", "link_updated", ADMIN_ID, f"guide_url={normalized}")

    await state.clear()
    await message.answer(
        f"{premium_emoji('true', '✅')} <b>لینک آموزش با موفقیت به‌روزرسانی شد.</b>\n\n"
        f"<code>{normalized}</code>",
        parse_mode="HTML"
    )