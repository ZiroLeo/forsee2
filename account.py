import html
import logging
from datetime import datetime
from typing import Optional

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import LEVEL_NAMES
from database import (
    get_user,
    get_user_subscriptions,
    get_subscription,
    get_user_level,
    record_user_activity,
    log_event,
)
import ui
from utils import (
    rate_limiter,
    format_bytes,
    format_days_remaining,
)

logger = logging.getLogger(__name__)
router = Router(name="account")


# ============================================================
# PREMIUM EMOJI IDS
# ============================================================

LEVEL_EMOJIS = {
    "new": "5454252967181978413",
    "prime": "5454397690399984810",
    "royal": "5454314922085221913",
    "diamond": "5454360689256730455",
    "legendary": "5451955546225676651",
}

EMOJI = {
    "telegram": "5440389890787281213",
    "wallet": "5445353829304387411",
    "shop": "5211047317309767043",
    "calendar": "5827691921073512783",
    "package": "5280943438991214029",
    "true": "5440547189669516347",
    "false": "5440681540541502133",
    "warning": "5440739793182937488",
    "back": "5325598711018051937",
    "light": "5827691921073512783",
}


# ============================================================
# HELPERS
# ============================================================

def tg_emoji(emoji_id: str, fallback: str = "●") -> str:
    """ساخت Premium Emoji برای متن HTML تلگرام."""
    from rich import safe_emoji_text

    return (
        f'<tg-emoji emoji-id="{emoji_id}">'
        f'{safe_emoji_text("", fallback)}'
        f'</tg-emoji>'
    )


def safe_text(value, default: str = "نامشخص") -> str:
    if value is None:
        value = default

    return html.escape(str(value))


def format_date(value) -> str:
    """پشتیبانی از timestamp و تاریخ متنی."""
    if not value:
        return "نامشخص"

    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value).strftime("%Y-%m-%d")

        if isinstance(value, str):
            value = value.strip()

            if value.isdigit():
                return datetime.fromtimestamp(
                    int(value)
                ).strftime("%Y-%m-%d")

            try:
                return datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).strftime("%Y-%m-%d")
            except ValueError:
                return value[:30]

    except (ValueError, TypeError, OSError, OverflowError):
        logger.warning("Invalid registration date.")

    return "نامشخص"


def get_level_info(total_purchases: int):
    try:
        level_key, level_name = get_user_level(
            max(0, int(total_purchases or 0))
        )
    except Exception:
        logger.exception("Could not calculate user level.")
        level_key, level_name = "new", "تازه‌وارد"

    aliases = {
        "new": "new",
        "beginner": "new",
        "fresh": "new",
        "prime": "prime",
        "royal": "royal",
        "diamond": "diamond",
        "legendary": "legendary",
        "legend": "legendary",
    }

    normalized_key = aliases.get(
        str(level_key).strip().lower(),
        "new",
    )

    emoji_id = LEVEL_EMOJIS[normalized_key]

    display_name = (
        str(level_name).strip()
        if level_name
        else LEVEL_NAMES.get(normalized_key, "تازه‌وارد")
    )

    return (
        normalized_key,
        display_name,
        tg_emoji(emoji_id),
    )


def format_status(state: str) -> tuple[str, str]:
    states = {
        "active": ("فعال", EMOJI["true"]),
        "expired": ("منقضی شده", EMOJI["false"]),
        "depleted": ("حجم تمام شده", EMOJI["warning"]),
        "deleted": ("حذف شده", EMOJI["false"]),
        "paused": ("متوقف شده", EMOJI["warning"]),
    }

    label, emoji_id = states.get(
        str(state or "").lower(),
        ("نامشخص", EMOJI["light"]),
    )

    return label, tg_emoji(emoji_id)


def account_inline_button(
    text: str,
    callback_data: str,
    emoji_id: str,
    style: str = "primary",
) -> InlineKeyboardButton:
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("Callback data is too long.")

    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=emoji_id,
        style=style,
    )


def account_keyboard(subscriptions: list) -> InlineKeyboardMarkup:
    rows = []

    for sub in subscriptions[:7]:
        sub_id = int(sub["id"])
        config_name = str(
            sub.get("config_name") or f"سرویس {sub_id}"
        )[:55]

        rows.append([
            account_inline_button(
                text=config_name,
                callback_data=f"account:sub:{sub_id}",
                emoji_id=EMOJI["telegram"],
                style="primary",
            )
        ])

    if not subscriptions:
        rows.append([
            InlineKeyboardButton(
                text="هنوز سرویسی خریداری نکرده‌اید",
                callback_data="account:noop",
                style="primary",
            )
        ])

    rows.append([
        account_inline_button(
            text="بازگشت به منوی اصلی",
            callback_data="menu:main",
            emoji_id=EMOJI["back"],
            style="danger",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_subscription_detail_keyboard(
    sub_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                account_inline_button(
                    text="بازگشت به حساب کاربری",
                    callback_data="menu:account",
                    emoji_id=EMOJI["back"],
                    style="danger",
                )
            ]
        ]
    )


async def send_new_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """
    نمایش صفحه‌ی حساب در همان پیام (نام قدیمی حفظ شده).

    اگر کیبورد داده نشود، دکمه‌ی بازگشت گذاشته می‌شود تا کاربر
    بعد از یک پیام خطا گیر نکند.
    """

    if reply_markup is None:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="بازگشت به منوی اصلی",
                        callback_data="menu:main",
                        style="danger",
                    )
                ]
            ]
        )

    await ui.show(callback, text, reply_markup)


def safe_log(event: str, user_id: int, detail: str = ""):
    try:
        log_event(
            "SECURITY",
            event,
            user_id,
            detail[:500],
        )
    except Exception:
        logger.exception("Could not write account security log.")


# ============================================================
# ACCOUNT MAIN PAGE
# ============================================================

@router.callback_query(F.data == "menu:account")
async def show_account(callback: CallbackQuery):
    user = callback.from_user

    if not user:
        await callback.answer()
        return

    await callback.answer()

    if not rate_limiter.is_allowed(user.id):
        await send_new_message(
            callback,
            f'{tg_emoji(EMOJI["warning"])} '
            "<b>لطفاً کمی صبر کنید و دوباره تلاش کنید.</b>",
        )
        return

    try:
        record_user_activity(user.id)

        db_user = get_user(user.id)

        if not db_user:
            await send_new_message(
                callback,
                f'{tg_emoji(EMOJI["false"])} '
                "<b>اطلاعات حساب کاربری پیدا نشد.</b>",
            )
            return

        total_purchases = int(
            db_user.get("total_purchases", 0) or 0
        )

        balance = int(
            db_user.get("balance", 0) or 0
        )

        level_key, level_name, level_emoji = get_level_info(
            total_purchases
        )

        joined = format_date(
            db_user.get("joined_at")
        )

        subscriptions = get_user_subscriptions(
            user.id,
            only_visible=True,
        ) or []

        recent_subscriptions = subscriptions[:7]

        text = (
            f'{tg_emoji(EMOJI["telegram"])} '
            "<b>حساب کاربری شما</b>\n"
            "<i>نمای کلی اطلاعات و سرویس‌های حساب</i>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f'{tg_emoji(EMOJI["wallet"])} '
            "<b>اطلاعات حساب</b>\n"
            f"نام: <b>{safe_text(user.first_name)}</b>\n"
            f"شناسه: <code>{user.id}</code>\n"
            f"تاریخ عضویت: <code>{joined}</code>\n\n"

            f'{tg_emoji(EMOJI["shop"])} '
            "<b>وضعیت کاربری</b>\n"
            f"سطح: {level_emoji} <b>{safe_text(level_name)}</b>\n"
            f"تعداد خریدها: <b>{total_purchases:,}</b>\n"
            f"موجودی کیف پول: "
            f"<b>{balance:,} تومان</b>\n\n"

            f'{tg_emoji(EMOJI["package"])} '
            "<b>آخرین سرویس‌های شما</b>\n"
            "<i>برای مشاهده جزئیات، سرویس موردنظر را انتخاب کنید.</i>"
        )

        await send_new_message(
            callback,
            text,
            account_keyboard(recent_subscriptions),
        )

    except Exception:
        logger.exception(
            "Failed to display account for user %s",
            user.id,
        )

        await send_new_message(
            callback,
            f'{tg_emoji(EMOJI["warning"])} '
            "<b>نمایش حساب کاربری با مشکل مواجه شد.</b>\n"
            "لطفاً کمی بعد دوباره تلاش کنید.",
        )


# ============================================================
# ACCOUNT NOOP
# ============================================================

@router.callback_query(F.data == "account:noop")
async def account_noop(callback: CallbackQuery):
    await callback.answer(
        "هنوز سرویسی برای نمایش وجود ندارد.",
        show_alert=True,
    )


# ============================================================
# SUBSCRIPTION DETAILS
# ============================================================

@router.callback_query(F.data.startswith("account:sub:"))
async def show_subscription_detail(callback: CallbackQuery):
    user = callback.from_user

    if not user:
        await callback.answer()
        return

    parts = (callback.data or "").split(":")

    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer(
            "شناسه سرویس نامعتبر است.",
            show_alert=True,
        )
        return

    sub_id = int(parts[2])

    if sub_id <= 0:
        await callback.answer(
            "شناسه سرویس نامعتبر است.",
            show_alert=True,
        )
        return

    await callback.answer()

    if not rate_limiter.is_allowed(user.id):
        await send_new_message(
            callback,
            f'{tg_emoji(EMOJI["warning"])} '
            "<b>لطفاً کمی صبر کنید.</b>",
        )
        return

    try:
        record_user_activity(user.id)

        sub = get_subscription(sub_id)

        if not sub:
            await send_new_message(
                callback,
                f'{tg_emoji(EMOJI["false"])} '
                "<b>این سرویس پیدا نشد یا دیگر در دسترس نیست.</b>",
            )
            return

        if int(sub.get("telegram_id", 0)) != user.id:
            safe_log(
                "unauthorized_sub_access",
                user.id,
                f"sub_id={sub_id}",
            )

            logger.warning(
                "Unauthorized subscription access: "
                "user_id=%s subscription_id=%s",
                user.id,
                sub_id,
            )

            await send_new_message(
                callback,
                f'{tg_emoji(EMOJI["false"])} '
                "<b>دسترسی غیرمجاز است.</b>",
            )
            return

        config_name = safe_text(
            sub.get("config_name")
        )

        state = str(
            sub.get("state") or "unknown"
        ).lower()

        state_name, state_emoji = format_status(state)

        volume_gb = float(
            sub.get("volume_gb") or 0
        )

        used_bytes = max(
            0,
            int(sub.get("used_bytes") or 0),
        )

        if volume_gb > 0:
            volume_bytes = int(
                volume_gb * (1024 ** 3)
            )

            remaining_bytes = max(
                0,
                volume_bytes - used_bytes,
            )

            volume_label = f"{volume_gb:g} گیگابایت"
            remaining_label = format_bytes(
                remaining_bytes
            )

        else:
            volume_label = "نامحدود"
            remaining_label = "نامحدود"

        used_label = format_bytes(used_bytes)

        expires_at = sub.get("expires_at")

        if expires_at:
            try:
                remaining_time = format_days_remaining(
                    expires_at
                )
            except Exception:
                logger.warning(
                    "Could not format expiration for subscription %s",
                    sub_id,
                )
                remaining_time = "نامشخص"
        else:
            remaining_time = "بدون انقضا"

        devices = int(
            sub.get("devices", 2) or 2
        )

        link = sub.get("link")

        if isinstance(link, str) and link.strip():
            link_text = html.escape(link.strip()[:4000])
        else:
            link_text = "نامشخص"

        text = (
            f'{tg_emoji(EMOJI["telegram"])} '
            "<b>جزئیات سرویس</b>\n"
            "<i>اطلاعات اختصاصی کانفیگ شما</i>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"{state_emoji} "
            f"<b>وضعیت سرویس:</b> {safe_text(state_name)}\n\n"

            f'{tg_emoji(EMOJI["package"])} '
            "<b>مشخصات سرویس</b>\n"
            f"نام کانفیگ: <code>{config_name}</code>\n"
            f"حجم کل: <b>{safe_text(volume_label)}</b>\n"
            f"مصرف‌شده: <b>{safe_text(used_label)}</b>\n"
            f"باقی‌مانده: <b>{safe_text(remaining_label)}</b>\n"
            f"دستگاه‌ها: <b>{devices}</b>\n\n"

            f'{tg_emoji(EMOJI["calendar"])} '
            "<b>مدت اعتبار</b>\n"
            f"زمان باقی‌مانده: <b>{safe_text(remaining_time)}</b>\n\n"

            f'{tg_emoji(EMOJI["light"])} '
            "<b>لینک اشتراک</b>\n"
            f"<code>{link_text}</code>\n\n"

            "<i>لینک اشتراک محرمانه است؛ آن را در اختیار دیگران قرار ندهید.</i>"
        )

        await send_new_message(
            callback,
            text,
            account_subscription_detail_keyboard(sub_id),
        )

    except Exception:
        logger.exception(
            "Failed to show subscription details: "
            "user_id=%s subscription_id=%s",
            user.id,
            sub_id,
        )

        await send_new_message(
            callback,
            f'{tg_emoji(EMOJI["warning"])} '
            "<b>دریافت جزئیات سرویس با مشکل مواجه شد.</b>\n"
            "لطفاً دوباره تلاش کنید.",
        )