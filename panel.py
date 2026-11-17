
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from config import (
    ADMIN_ID,
    PREMIUM_EMOJIS,
    PREMIUM_EMOJI_ENABLED,
)

from database import (
    log_event,
    get_setting,
)

from utils import rate_limiter
from rich import premium_emoji


logger = logging.getLogger(__name__)

router = Router(name="admin_panel")


# ============================================================
# CONSTANTS
# ============================================================

# شناسه معتبر ایموجی بازگشت؛ مطابق wheel.py
BACK_EMOJI_ID = "5325598171018051937"

# رنگ‌های دکمه‌ها:
# primary = آبی
# success = سبز
# danger  = قرمز


# ============================================================
# PREMIUM EMOJI
# ============================================================

def _admin_emoji_id(key: str) -> str | None:
    """
    دریافت شناسه ایموجی پرمیوم برای دکمه‌های Inline.

    ساختار مشابه wheel.py است.
    """

    if not PREMIUM_EMOJI_ENABLED:
        return None

    if key == "back":
        return BACK_EMOJI_ID

    emoji_id = PREMIUM_EMOJIS.get(key)

    if not emoji_id:
        logger.warning(
            "Admin panel emoji is missing: %s",
            key,
        )
        return None

    return str(emoji_id)


# ============================================================
# INLINE BUTTON BUILDER
# ============================================================

def _button(
    text: str,
    callback_data: str,
    style: str = "primary",
    emoji_key: str | None = None,
) -> InlineKeyboardButton:
    """
    ساخت دکمه Inline با رنگ و Premium Emoji.

    این روش مطابق روش استفاده‌شده در wheel.py است.
    """

    if style not in ("primary", "success", "danger"):
        raise ValueError(
            f"Invalid button style: {style}"
        )

    kwargs = {
        "text": text,
        "callback_data": callback_data,
        "style": style,
    }

    if emoji_key:
        emoji_id = _admin_emoji_id(emoji_key)

        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id

    return InlineKeyboardButton(**kwargs)


# ============================================================
# MAIN ADMIN KEYBOARD
# ============================================================

def admin_main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    منوی اصلی پنل مدیریت.

    تمام دکمه‌ها:
    - دو تایی در هر ردیف
    - دارای Premium Emoji
    - دارای رنگ مشخص
    - دارای callback_data سازگار با Routerهای موجود
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[

            # ------------------------------------------------
            # فروش
            # ------------------------------------------------

            [
                _button(
                    text="بستن فروش",
                    callback_data="admin:sales:close",
                    style="danger",
                    emoji_key="false",
                ),
                _button(
                    text="باز کردن فروش",
                    callback_data="admin:sales:open",
                    style="success",
                    emoji_key="true",
                ),
            ],

            # ------------------------------------------------
            # قیمت و محصولات
            # ------------------------------------------------

            [
                _button(
                    text="مدیریت قیمت‌ها",
                    callback_data="admin:prices:menu",
                    style="primary",
                    emoji_key="stars",
                ),
                _button(
                    text="مدیریت محصولات",
                    callback_data="admin:products:menu",
                    style="success",
                    emoji_key="shop",
                ),
            ],

            # ------------------------------------------------
            # تخفیف و سرویس‌ها
            # ------------------------------------------------

            [
                _button(
                    text="کدهای تخفیف",
                    callback_data="admin:discounts:menu",
                    style="danger",
                    emoji_key="gift",
                ),
                _button(
                    text="مدیریت سرویس‌ها",
                    callback_data="admin:services:menu",
                    style="primary",
                    emoji_key="world",
                ),
            ],

            # ------------------------------------------------
            # کاربران
            # ------------------------------------------------

            [
                _button(
                    text="مدیریت کاربران",
                    callback_data="admin:users:menu",
                    style="success",
                    emoji_key="telegram",
                ),
                _button(
                    text="لیست کاربران",
                    callback_data="admin:users_list:menu",
                    style="danger",
                    emoji_key="blue_heart",
                ),
            ],

            # ------------------------------------------------
            # ارتباطات و بک‌آپ
            # ------------------------------------------------

            [
                _button(
                    text="ارسال پیام",
                    callback_data="admin:broadcast:menu",
                    style="primary",
                    emoji_key="ask",
                ),
                _button(
                    text="بک‌آپ دیتابیس",
                    callback_data="admin:backup:menu",
                    style="success",
                    emoji_key="safe",
                ),
            ],

            # ------------------------------------------------
            # آمار و تست پنل
            # ------------------------------------------------

            [
                _button(
                    text="آمار ربات",
                    callback_data="admin:stats:menu",
                    style="danger",
                    emoji_key="diamond",
                ),
                _button(
                    text="تست اتصال PAXIVA",
                    callback_data="admin:test_panel:menu",
                    style="primary",
                    emoji_key="light",
                ),
            ],

            # ------------------------------------------------
            # لینک‌ها و ادمین رسید
            # ------------------------------------------------

            [
                _button(
                    text="مدیریت لینک‌ها",
                    callback_data="admin:links:menu",
                    style="success",
                    emoji_key="referral",
                ),
                _button(
                    text="ادمین رسید",
                    callback_data="admin:receipt_admin:menu",
                    style="danger",
                    emoji_key="ask",
                ),
            ],
        ]
    )


# ============================================================
# ADMIN MENU TEXT
# ============================================================

def get_admin_menu_text() -> str:
    """
    ساخت متن منوی ادمین.

    وضعیت فروش از دیتابیس خوانده می‌شود.
    """

    try:
        sales_open = str(
            get_setting("sales_open", "1")
        ).strip().lower()

    except Exception:
        logger.exception(
            "Could not read sales status."
        )
        sales_open = "unknown"

    if sales_open in ("1", "true", "yes", "on"):
        sales_status = (
            f"{premium_emoji('true', '🟢')} "
            "<b>فروش فعال است</b>"
        )

    elif sales_open in ("0", "false", "no", "off"):
        sales_status = (
            f"{premium_emoji('false', '🔴')} "
            "<b>فروش بسته است</b>"
        )

    else:
        sales_status = (
            f"{premium_emoji('warning', '⚠️')} "
            "<b>وضعیت فروش نامشخص است</b>"
        )

    return (
        f"{premium_emoji('gram', '👋')} "
        "<b>پنل مدیریت آی‌من نتورک</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{premium_emoji('diamond', '💎')} "
        "<b>خوش آمدید، مدیر!</b>\n\n"
        f"{sales_status}\n\n"
        "از طریق منوی زیر می‌توانید بخش‌های مختلف "
        "ربات را مدیریت کنید.\n\n"
        "<i>برای ورود به هر بخش، دکمه مربوطه را انتخاب کنید.</i>"
    )


# ============================================================
# SAFE LOG
# ============================================================

def safe_log(
    level: str,
    event: str,
    user_id: int,
    details: str = "",
) -> None:
    """
    ثبت رویداد بدون متوقف کردن عملیات اصلی.
    """

    try:
        log_event(
            level,
            event,
            user_id,
            details,
        )

    except Exception:
        logger.exception(
            "Could not write admin panel event: %s",
            event,
        )


# ============================================================
# ACCESS CONTROL
# ============================================================

def is_admin(user_id: int | None) -> bool:
    """
    بررسی دسترسی ادمین اصلی.
    """

    return (
        user_id is not None
        and user_id == ADMIN_ID
    )


async def deny_access(
    callback: CallbackQuery,
) -> None:
    """
    جلوگیری از دسترسی غیرمجاز به پنل.
    """

    user = callback.from_user

    safe_log(
        "SECURITY",
        "unauthorized_admin_callback",
        user.id if user else 0,
        f"data={callback.data}",
    )

    try:
        await callback.answer(
            "⛔ دسترسی غیرمجاز است.",
            show_alert=True,
        )

    except Exception:
        logger.warning(
            "Could not answer unauthorized callback."
        )


# ============================================================
# CALLBACK ANSWER
# ============================================================

async def answer_callback(
    callback: CallbackQuery,
) -> None:
    """
    پاسخ سریع به Callback برای جلوگیری از انقضا.
    """

    try:
        await callback.answer()

    except Exception:
        logger.warning(
            "Could not answer admin callback."
        )


# ============================================================
# SAFE EDIT ADMIN MENU
# ============================================================

async def safe_edit_admin_menu(
    callback: CallbackQuery,
) -> bool:
    """
    نمایش منوی اصلی پنل ادمین.

    ابتدا پیام فعلی ویرایش می‌شود.
    در صورت شکست، پیام جایگزین ارسال می‌شود.
    """

    message = callback.message

    if not isinstance(message, Message):
        return False

    text = get_admin_menu_text()
    keyboard = admin_main_menu_keyboard()

    try:
        await message.edit_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

        return True

    except TelegramBadRequest as error:

        error_text = str(error).lower()

        if "message is not modified" in error_text:
            return True

        logger.warning(
            "Could not edit admin menu: %s",
            error,
        )

    except Exception:
        logger.exception(
            "Unexpected error while editing admin menu."
        )

    # ارسال پیام جایگزین
    try:
        await message.answer(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

        return True

    except Exception:
        logger.exception(
            "Could not send replacement admin menu."
        )

    return False


# ============================================================
# OPEN ADMIN PANEL
# ============================================================

@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    state: FSMContext,
):
    """
    ورود به پنل مدیریت با دستور /admin.

    فقط ADMIN_ID مجاز است.
    """

    user = message.from_user

    if not user:
        return

    # بررسی دسترسی
    if not is_admin(user.id):

        safe_log(
            "SECURITY",
            "unauthorized_admin_access",
            user.id,
            f"username={user.username}, command=/admin",
        )

        await message.answer(
            "⛔ شما اجازه دسترسی به پنل مدیریت را ندارید."
        )

        return

    # محدودیت درخواست
    try:
        if not rate_limiter.is_allowed(user.id):

            await message.answer(
                "⏳ لطفاً چند لحظه صبر کنید و دوباره تلاش کنید."
            )

            return

    except Exception:
        logger.exception(
            "Admin rate limiter failed."
        )

    # پاک‌سازی وضعیت قبلی FSM ادمین
    try:
        await state.clear()

    except Exception:
        logger.exception(
            "Could not clear admin FSM state."
        )

    try:
        await message.answer(
            text=get_admin_menu_text(),
            reply_markup=admin_main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

        safe_log(
            "INFO",
            "admin_panel_opened",
            user.id,
        )

        logger.info(
            "Admin panel opened by user %s.",
            user.id,
        )

    except Exception:

        logger.exception(
            "Failed to open admin panel."
        )

        await message.answer(
            "❌ خطایی هنگام باز کردن پنل مدیریت رخ داد."
        )


# ============================================================
# RETURN TO MAIN ADMIN MENU
# ============================================================

@router.callback_query(F.data == "admin:main")
async def callback_admin_main(
    callback: CallbackQuery,
    state: FSMContext,
):
    """
    بازگشت به منوی اصلی پنل ادمین.

    پیام فعلی را ویرایش می‌کند.
    """

    user = callback.from_user

    # بررسی دسترسی قبل از پاسخ Callback
    if not user or not is_admin(user.id):

        await deny_access(callback)
        return

    # پاسخ سریع به Callback
    await answer_callback(callback)

    # پاک‌سازی وضعیت‌های قبلی پنل
    try:
        await state.clear()

    except Exception:
        logger.exception(
            "Could not clear FSM state on admin menu return."
        )

    success = await safe_edit_admin_menu(callback)

    if not success:

        logger.error(
            "Admin main menu could not be displayed for user %s.",
            user.id,
        )

        try:
            await callback.bot.send_message(
                chat_id=user.id,
                text=get_admin_menu_text(),
                reply_markup=admin_main_menu_keyboard(),
                parse_mode=ParseMode.HTML,
            )

        except Exception:
            logger.exception(
                "Failed to send admin menu fallback."
            )
            return

    safe_log(
        "INFO",
        "admin_main_menu_opened",
        user.id,
    )
