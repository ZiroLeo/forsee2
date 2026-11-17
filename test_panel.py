import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from paxiva import PaxivaClient, PaxivaError
from rich import premium_emoji

logger = logging.getLogger(__name__)
router = Router(name="admin_test_panel")


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
        logger.exception("Could not send test_panel message.")


# ============================================================
# TEST PANEL
# ============================================================

@router.callback_query(F.data == "admin:test_panel:menu")
async def test_panel(callback: CallbackQuery, paxiva: PaxivaClient):
    user = callback.from_user
    if not user or user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    await callback.answer("در حال تست اتصال...", show_alert=False)

    try:
        me = await paxiva.get_me()

        credits = me.get("credits", 0)
        pool = me.get("pool", {})
        pool_gb = pool.get("gb", 0) if pool else 0
        counts = me.get("counts", {})
        clients = counts.get("clients", 0)

        text = (
            f"{premium_emoji('true', '✅')} "
            "<b>پنل متصل میباشد</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 <b>شناسه حساب:</b> <code>{me.get('id', '?')}</code>\n"
            f"🏢 <b>برند:</b> {me.get('brand', '?')}\n"
            f"💰 <b>اعتبار (Credits):</b> {credits}\n"
            f"📦 <b>موجودی حجمی (Pool):</b> {pool_gb} GB\n"
            f"👥 <b>تعداد کاربران پنل:</b> {clients}"
        )

        markup = InlineKeyboardMarkup(inline_keyboard=[
            [_button("بازگشت به پنل", "admin:main", "danger", "back")]
        ])

        await _safe_answer(callback.message, text, markup)

    except PaxivaError as e:
        logger.error("Panel test failed: %s", e)

        text = (
            f"{premium_emoji('false', '❌')} "
            "<b>پنل وصل نیست</b>\n\n"
            f"<b>کد خطا:</b> <code>{getattr(e, 'error_code', '?')}</code>\n"
            f"<b>پیام:</b> {getattr(e, 'message', '?')}"
        )

        markup = InlineKeyboardMarkup(inline_keyboard=[
            [_button("بازگشت به پنل", "admin:main", "danger", "back")]
        ])

        await _safe_answer(callback.message, text, markup)

    except Exception as e:
        logger.exception("Panel test unexpected error: %s", e)

        text = (
            f"{premium_emoji('warning', '⚠️')} "
            "<b>پنل وصل نیست</b>\n\n"
            "<b>خطای غیرمنتظره در تست اتصال.</b>"
        )

        markup = InlineKeyboardMarkup(inline_keyboard=[
            [_button("بازگشت به پنل", "admin:main", "danger", "back")]
        ])

        await _safe_answer(callback.message, text, markup)