"""
admin_ui.py
-----------
ابزارهای مشترک پنل ادمین:

* ایموجی پریمیوم روی متن (HTML) و روی دکمه‌ها (icon_custom_emoji_id)
* دکمه‌های اینلاین «دوتا دوتا» با چرخهٔ رنگ آبی ← سبز ← قرمز
* ویرایش در همان پیام (edit-in-place) با fallback به پیام جدید
* «مرجع پنل» برای فرایندهای متنی (FSM): بعد از پیام ادمین، همان پنل ویرایش می‌شود
* گارد دسترسی (ادمین اصلی)
"""

import logging
from html import escape as _html_escape
from typing import Iterable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import log_event
from rich import premium_emoji

logger = logging.getLogger(__name__)

BLUE = "primary"
GREEN = "success"
RED = "danger"
_CYCLE = (BLUE, GREEN, RED)


# ============================================================
# متن
# ============================================================

def esc(value) -> str:
    return _html_escape("" if value is None else str(value), quote=False)


def em(key: str, fallback: str = "⭐") -> str:
    """ایموجی پریمیوم برای متن HTML (فال‌بک: یک ایموجی واقعی)."""
    return premium_emoji(key, fallback)


def title(key: str, fallback: str, text: str) -> str:
    return f"{em(key, fallback)} <b>{esc(text)}</b>"


def money(value: int) -> str:
    return f"{int(value):,}"


def fa_digits(value) -> str:
    return str(value).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


LINE = "━━━━━━━━━━━━━━"


# ============================================================
# دکمه‌ها
# ============================================================

def _icon(emoji_key: Optional[str]) -> Optional[str]:
    if not emoji_key or not PREMIUM_EMOJI_ENABLED or not isinstance(PREMIUM_EMOJIS, dict):
        return None
    value = str(PREMIUM_EMOJIS.get(emoji_key) or "").strip()
    return value or None


def btn(
    text: str,
    callback_data: Optional[str] = None,
    *,
    style: Optional[str] = None,
    emoji: Optional[str] = None,
    url: Optional[str] = None,
) -> InlineKeyboardButton:
    """
    یک دکمه. اگر style ندادید، هنگام چیدمان با menu()/grid() رنگ می‌گیرد.
    """
    kwargs = {"text": text}
    if url:
        kwargs["url"] = url
    else:
        if not callback_data:
            raise ValueError("callback_data is required")
        if len(callback_data.encode("utf-8")) > 64:
            raise ValueError("callback_data exceeds 64 bytes: " + callback_data)
        kwargs["callback_data"] = callback_data
    if style:
        kwargs["style"] = style
    icon = _icon(emoji)
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(**kwargs)


def grid(buttons: Iterable[InlineKeyboardButton], per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    """
    چیدن دکمه‌ها دوتا دوتا. دکمه‌های بدون style به‌ترتیب آبی ← سبز ← قرمز می‌شوند.
    """
    items = []
    cycle_index = 0
    for button in buttons:
        if getattr(button, "style", None) is None:
            button = button.model_copy(update={"style": _CYCLE[cycle_index % 3]})
            cycle_index += 1
        items.append(button)
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def menu(
    buttons: Iterable[InlineKeyboardButton],
    back: Optional[str] = "admin:main",
    back_text: str = "بازگشت",
    per_row: int = 2,
) -> InlineKeyboardMarkup:
    rows = grid(buttons, per_row)
    if back:
        rows.append([btn(back_text, back, style=RED, emoji="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_only(callback_data: str = "admin:main", text: str = "بازگشت") -> InlineKeyboardMarkup:
    return menu([], back=callback_data, back_text=text)


def cancel_kb(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("انصراف", callback_data, style=RED, emoji="false")]])


# ============================================================
# دسترسی
# ============================================================

def is_main_admin(user_id: Optional[int]) -> bool:
    return bool(user_id) and int(user_id) == int(ADMIN_ID)


class AdminOnly(BaseFilter):
    """فقط ادمین اصلی؛ تلاش دیگران لاگ امنیتی می‌شود."""

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        if user and is_main_admin(user.id):
            return True
        if user and isinstance(event, CallbackQuery) and str(event.data or "").startswith("admin:"):
            try:
                log_event("SECURITY", "unauthorized_admin_callback", user.id, str(event.data)[:100])
            except Exception:
                pass
        return False


def protect(router) -> None:
    """فیلتر ادمین را روی همهٔ هندلرهای پیام و کال‌بک یک router اعمال می‌کند."""
    router.message.filter(AdminOnly())
    router.callback_query.filter(AdminOnly())


# ============================================================
# نمایش (ویرایش در همان پیام)
# ============================================================

async def page(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
    *,
    answer: bool = True,
) -> Optional[Message]:
    """همان پیام را ویرایش می‌کند (Rich یا متنی)؛ در صورت رد شدن، پیام جدید می‌فرستد."""
    message = callback.message
    if answer:
        try:
            await callback.answer()
        except Exception:
            pass
    if not isinstance(message, Message):
        return None
    try:
        await message.edit_text(text=text, reply_markup=markup)
        return message
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return message
        logger.warning("admin edit failed, sending new message: %s", error)
    except Exception:
        logger.exception("admin edit crashed")
    try:
        return await message.answer(text, reply_markup=markup)
    except Exception:
        logger.exception("admin send failed")
        return None


async def toast(callback: CallbackQuery, text: str, alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        pass


# ============================================================
# مرجع پنل برای فرایندهای متنی
# ============================================================

async def remember_panel(state: FSMContext, message: Optional[Message]) -> None:
    if isinstance(message, Message):
        await state.update_data(_panel_chat=message.chat.id, _panel_msg=message.message_id)


async def refresh_panel(
    bot: Bot,
    state: FSMContext,
    source: Message,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
    *,
    delete_source: bool = True,
) -> None:
    """
    بعد از پیام متنی ادمین: پیام ادمین پاک می‌شود و «پنل اصلی» ویرایش می‌شود.
    اگر پنل پیدا/ویرایش نشد، پیام جدید ارسال می‌شود.
    """
    data = await state.get_data()
    chat_id, msg_id = data.get("_panel_chat"), data.get("_panel_msg")

    if delete_source:
        try:
            await source.delete()
        except Exception:
            pass

    if chat_id and msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
            logger.warning("panel refresh failed: %s", error)
        except Exception:
            logger.exception("panel refresh crashed")

    try:
        sent = await bot.send_message(
            source.chat.id, text, reply_markup=markup
        )
        await state.update_data(_panel_chat=sent.chat.id, _panel_msg=sent.message_id)
    except Exception:
        logger.exception("panel fallback send failed")


async def ask(
    callback: CallbackQuery,
    state: FSMContext,
    new_state,
    text: str,
    cancel_callback: str,
    **data,
) -> None:
    """وارد حالت دریافت متن می‌شود و پنل را به یک پرسش تبدیل می‌کند."""
    await state.set_state(new_state)
    if data:
        await state.update_data(**data)
    shown = await page(callback, text, cancel_kb(cancel_callback))
    await remember_panel(state, shown)


async def reset_flow(state: FSMContext) -> None:
    """پایان یک فرایند، بدون پاک‌کردن مرجع پنل."""
    data = await state.get_data()
    keep = {k: v for k, v in data.items() if k in ("_panel_chat", "_panel_msg")}
    await state.clear()
    if keep:
        await state.update_data(**keep)


def parse_int(text: Optional[str], minimum: int = 0, maximum: Optional[int] = None) -> Optional[int]:
    """عدد صحیح (ارقام فارسی/عربی، جداکنندهٔ هزارگان) یا None."""
    if text is None:
        return None
    cleaned = fa_digits(text).replace(",", "").replace("٬", "").replace(" ", "").strip()
    if not cleaned.lstrip("-").isdigit():
        return None
    value = int(cleaned)
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return value


# ============================================================
# ابزارهای نمایشی
# ============================================================

def custom_emoji(emoji_id: str, fallback: str = "⭐") -> str:
    """ایموجی پریمیوم با شناسهٔ مستقیم (مثلاً سطح‌ها)."""
    if not PREMIUM_EMOJI_ENABLED or not emoji_id:
        return esc(fallback)
    return f'<tg-emoji emoji-id="{esc(emoji_id)}">{esc(fallback)}</tg-emoji>'


def fmt_date(ts) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "نامشخص"


def bar(percent: float, width: int = 10) -> str:
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100))
    return "▰" * filled + "▱" * (width - filled)
