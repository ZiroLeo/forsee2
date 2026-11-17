import logging
import re

from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from database import (
    create_or_update_user,
    set_referral,
    get_user,
    is_banned,
    record_user_activity,
    log_event,
)

from rich import rich_emoji, rich_bold, build_rich_message
from keyboards import main_menu_rich_message
from utils import rate_limiter
import ui


logger = logging.getLogger(__name__)
router = Router(name="start")


# ============================================================
# Referral Validation
# ============================================================

REFERRAL_PATTERN = re.compile(r"^REF_(\d{5,15})$")


def extract_referrer_id(payload: str, user_id: int):
    """اعتبارسنجی کد رفرال و جلوگیری از رفرال به خود."""

    match = REFERRAL_PATTERN.fullmatch(payload or "")

    if not match:
        return None

    try:
        referrer_id = int(match.group(1))

        if referrer_id == user_id:
            return None

        return referrer_id

    except (ValueError, TypeError):
        return None


# ============================================================
# Main Rich Menu
# ============================================================

def create_main_menu_message(first_name: str = None):
    """
    ساخت پیام اصلی (Rich): ابتدا متن خوش‌آمدگویی، سپس دکمه‌ها.

    متن به‌صورت بلوک پاراگراف ساخته می‌شود (نه html) تا همراه
    دکمه‌ها (blocks) درست نمایش داده شود. نیازی به escape نیست.
    """
    name = (first_name or "دوست عزیز").strip() or "دوست عزیز"

    title = [
        rich_emoji("gram", "👋"),
        " ",
        rich_bold(name),
        " به آی‌من نتورک خوش اومدی! ",
        rich_emoji("shop", "🛒"),
    ]

    body = "از منوی زیر می‌تونی خدمات موردنظرت رو انتخاب کنی:"

    return build_rich_message(
        [title, body],
        main_menu_rich_message(),
    )


async def send_main_menu(message: Message):
    """ارسال منوی اصلی به‌صورت Rich Message با نام کاربر."""

    first_name = (
        message.from_user.first_name
        if message.from_user
        else "دوست عزیز"
    )

    rich_message = create_main_menu_message(first_name)

    return await message.answer_rich(
        rich_message=rich_message
    )


# ============================================================
# User Registration
# ============================================================

def register_user(message: Message):
    """ثبت یا به‌روزرسانی اطلاعات کاربر."""

    user = message.from_user

    if not user:
        return False

    return create_or_update_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name
    )


# ============================================================
# /start With Referral
# ============================================================

@router.message(CommandStart(deep_link=True))
async def cmd_start_with_referral(
    message: Message,
    command: CommandObject
):
    user = message.from_user

    if not user:
        return

    # بررسی مسدود بودن کاربر
    if is_banned(user.id):
        await message.answer(
            "⛔ شما از استفاده از ربات محروم شده‌اید."
        )
        return

    # محدودیت تعداد درخواست
    if not rate_limiter.is_allowed(user.id):
        await message.answer(
            "⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید."
        )
        return

    # استخراج کد رفرال
    referrer_id = extract_referrer_id(
        command.args,
        user.id
    )

    try:
        # ثبت یا به‌روزرسانی کاربر
        is_new = register_user(message)

        # فقط کاربر جدید می‌تواند معرف بگیرد
        if is_new and referrer_id:
            # بررسی وجود معرف
            referrer = get_user(referrer_id)

            if referrer:
                registered = set_referral(
                    referrer_id,
                    user.id,
                )

                if registered:
                    logger.info(
                        "Referral registered: %s -> %s",
                        referrer_id,
                        user.id,
                    )

                    log_event(
                        "INFO",
                        "referral_registered",
                        user.id,
                        f"Direct referrer: {referrer_id}",
                    )
            else:
                logger.warning(
                    "Referral ignored: referrer %s does not exist.",
                    referrer_id,
                )

        # ثبت فعالیت کاربر
        record_user_activity(user.id)

        # ارسال منوی اصلی Rich
        await send_main_menu(message)

        logger.info(
            "Main menu sent to user %s",
            user.id,
        )

    except Exception:
        logger.exception(
            "Failed to process referral start for user %s",
            user.id,
        )

        await message.answer(
            "خطا در راه‌اندازی ربات رخ داد. لطفاً دوباره /start را بزنید."
        )


# ============================================================
# /start Without Referral
# ============================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user

    if not user:
        return

    # بررسی مسدود بودن کاربر
    if is_banned(user.id):
        await message.answer(
            "⛔ شما از استفاده از ربات محروم شده‌اید."
        )
        return

    # محدودیت درخواست
    if not rate_limiter.is_allowed(user.id):
        await message.answer(
            "⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید."
        )
        return

    try:
        # ثبت یا به‌روزرسانی کاربر
        register_user(message)

        # ثبت فعالیت روزانه
        record_user_activity(user.id)

        # ارسال منوی اصلی Rich
        await send_main_menu(message)

        logger.info(
            "Main Rich Menu sent to user %s",
            user.id
        )

    except Exception:
        logger.exception(
            "Failed to process /start for user %s",
            user.id
        )

        await message.answer(
            "خطا در راه‌اندازی ربات رخ داد. لطفاً دوباره /start را بزنید."
        )


# ============================================================
# Return To Main Menu
# ============================================================

@router.callback_query(F.data == "menu:main")
async def callback_main_menu(callback: CallbackQuery):
    user = callback.from_user

    if not user:
        await callback.answer()
        return

    # بررسی مسدود بودن کاربر
    if is_banned(user.id):
        await callback.answer(
            "⛔ شما از استفاده از ربات محروم شده‌اید.",
            show_alert=True
        )
        return

    if not isinstance(callback.message, Message):
        await callback.answer(
            "امکان ارسال منوی اصلی وجود ندارد.",
            show_alert=True
        )
        return

    try:
        # ثبت فعالیت
        record_user_activity(user.id)

        # ساخت مجدد منوی اصلی Rich
        rich_message = create_main_menu_message(
            user.first_name
        )

        # همان پیام (مثلاً صفحه‌ی پشتیبانی) به منوی اصلی ویرایش می‌شود.
        await ui.show_main_menu(callback, rich_message)

        await callback.answer()

    except TelegramBadRequest as error:
        error_text = str(error).lower()

        # اگر پیام از قبل همین منو باشد، خطا محسوب نمی‌شود.
        if "message is not modified" in error_text:
            await callback.answer()
            return

        logger.exception(
            "Telegram rejected main menu send for user %s",
            user.id
        )

        await callback.answer(
            "خطا در بازگشت به منوی اصلی.",
            show_alert=True
        )

    except Exception:
        logger.exception(
            "Failed to return user %s to main menu",
            user.id
        )

        await callback.answer(
            "خطای غیرمنتظره در بازگشت به منو.",
            show_alert=True
        )
