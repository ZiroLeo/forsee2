"""
ui.py
-----
ابزار مشترک نمایش صفحه‌ها بعد از کلیک روی دکمه‌ها.

قانون پروژه: همه‌چیز در «همان پیام» ویرایش می‌شود.
- منوی اصلی (Rich) با کلیک روی هر دکمه به پیام متنی + Inline Keyboard
  ویرایش می‌شود (تست‌شده: Rich -> text کار می‌کند).
- دکمه‌ی بازگشت همان پیام را دوباره به منوی اصلی (Rich) ویرایش می‌کند.
- اگر تلگرام ویرایش را رد کرد، پیام جدید ارسال می‌شود تا کاربر گیر نکند.
"""

import logging
from typing import Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)


def is_rich_message(message: Message) -> bool:
    """آیا این پیام یک Rich Message (مثل منوی اصلی) است؟"""
    return getattr(message, "rich_message", None) is not None


async def show(
    callback: CallbackQuery,
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
    *,
    new: bool = False,
) -> Optional[Message]:
    """
    نمایش یک صفحه (متن HTML + Inline Keyboard) در همان پیام.

    - new=True        -> مستقیماً پیام جدید.
    - در غیر این صورت -> ویرایش پیام فعلی (چه Rich چه متنی)،
                          و در صورت رد شدن توسط تلگرام پیام جدید.

    خروجی: پیامی که نمایش داده شد (همان پیام یا پیام جدید) یا None.
    """

    message = callback.message

    if not isinstance(message, Message):
        return None

    if new:
        return await _send_new(message, text, markup)

    try:
        await message.edit_text(
            text=text,
            # None یعنی «کیبورد قبلی حذف شود» (مثلاً هنگام پردازش).
            reply_markup=(
                markup
                if markup is not None
                else InlineKeyboardMarkup(inline_keyboard=[])
            ),
        )
        return message

    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return message

        logger.warning("Could not edit message, sending new one: %s", error)

    except Exception:
        logger.exception("Unexpected error while editing message.")

    return await _send_new(message, text, markup)


async def show_main_menu(
    callback: CallbackQuery,
    rich_message,
) -> Optional[Message]:
    """
    برگرداندن همان پیام به منوی اصلی (Rich).

    ویرایش «متن -> Rich» تست‌نشده است؛ اگر تلگرام رد کند،
    دلیل در لاگ ثبت و منوی اصلی به‌صورت پیام جدید ارسال می‌شود.
    پیام قبلی عمداً حذف نمی‌شود (ممکن است لینک اشتراک داشته باشد).
    """

    message = callback.message

    if not isinstance(message, Message):
        return None

    try:
        await message.edit_text(rich_message=rich_message)
        return message

    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return message

        logger.warning(
            "[EDIT] text -> rich main menu failed, sending new: %s",
            error,
        )

    except Exception:
        logger.exception("Unexpected error while editing to main menu.")

    try:
        return await message.answer_rich(rich_message=rich_message)
    except Exception:
        logger.exception("Could not send main menu.")
        return None


async def _send_new(
    message: Message,
    text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> Optional[Message]:
    try:
        return await message.answer(
            text=text,
            reply_markup=markup,
        )
    except Exception:
        logger.exception("Could not send new message.")
        return None
