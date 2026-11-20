
import html
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# شناسه کانال اعلان‌ها
NOTIFICATION_CHANNEL_ID = -1004332638904


def _escape(value: Any) -> str:
    """Escape امن متن برای Telegram HTML."""
    return html.escape(str(value if value is not None else "-"), quote=True)


def _user_info(user: Any, user_id: Optional[int] = None) -> tuple[str, str]:
    """
    پشتیبانی از aiogram User و دیکشنری اطلاعات کاربر.
    خروجی: (نام نمایشی، شناسه تلگرام)
    """
    if isinstance(user, dict):
        first_name = user.get("first_name") or ""
        last_name = user.get("last_name") or ""
        username = user.get("username")
        telegram_id = user.get("telegram_id") or user.get("id") or user_id

        full_name = f"{first_name} {last_name}".strip()
    else:
        first_name = getattr(user, "first_name", "") or ""
        last_name = getattr(user, "last_name", "") or ""
        username = getattr(user, "username", None)
        telegram_id = getattr(user, "id", None) or user_id

        full_name = (
            getattr(user, "full_name", None)
            or f"{first_name} {last_name}".strip()
        )

    if username:
        display_name = f"@{username}"
    elif full_name:
        display_name = full_name
    else:
        display_name = "کاربر"

    return _escape(display_name), _escape(telegram_id or "-")


async def _send_notification(bot: Any, message: str) -> bool:
    """
    ارسال امن اعلان.
    خطای تلگرام نباید عملیات اصلی بات را مختل کند.
    """
    try:
        if bot is None:
            logger.warning("Notification skipped: bot is None")
            return False

        await bot.send_message(
            chat_id=NOTIFICATION_CHANNEL_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True

    except Exception:
        logger.exception("Could not send channel notification")
        return False


async def notify_purchase(
    bot: Any,
    user: Any,
    config_name: str,
    kind: str,
    volume_gb: float,
    days: int,
    devices: int,
    amount: int,
) -> bool:
    """اعلان خرید موفق سرویس."""

    name, telegram_id = _user_info(user)

    service_type = "نامحدود" if kind == "unl" else "حجمی"
    volume_text = (
        "نامحدود"
        if kind == "unl"
        else f"{volume_gb:g} گیگابایت"
    )

    message = (
        "🛒 <b>خرید موفق سرویس</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: <b>{name}</b>\n"
        f"🆔 شناسه: <code>{telegram_id}</code>\n"
        f"📦 سرویس: <b>{_escape(service_type)}</b>\n"
        f"💾 حجم: <b>{_escape(volume_text)}</b>\n"
        f"⏳ مدت: <b>{int(days)} روز</b>\n"
        f"📱 دستگاه: <b>{int(devices)}</b>\n"
        f"⚙️ نام سرویس: <code>{_escape(config_name)}</code>\n"
        f"💰 مبلغ: <b>{int(amount):,} تومان</b>"
    )

    return await _send_notification(bot, message)


async def notify_wallet_topup(
    bot: Any,
    user: Any,
    amount: int,
) -> bool:
    """اعلان شارژ موفق کیف پول."""

    name, telegram_id = _user_info(user)

    message = (
        "💳 <b>شارژ موفق کیف پول</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: <b>{name}</b>\n"
        f"🆔 شناسه: <code>{telegram_id}</code>\n"
        f"💰 مبلغ شارژ: <b>{int(amount):,} تومان</b>"
    )

    return await _send_notification(bot, message)


async def notify_wheel_prize(
    bot: Any,
    user: Any,
    prize: str,
    prize_type: str,
) -> bool:
    """اعلان دریافت جایزه واقعی گردونه."""

    name, telegram_id = _user_info(user)

    if prize_type == "balance":
        prize_label = "جایزه نقدی"
    elif prize_type == "config":
        prize_label = "کانفیگ رایگان"
    else:
        prize_label = "جایزه"

    message = (
        "🎁 <b>برنده شدن در گردونه شانس</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: <b>{name}</b>\n"
        f"🆔 شناسه: <code>{telegram_id}</code>\n"
        f"🏆 نوع جایزه: <b>{_escape(prize_label)}</b>\n"
        f"🎉 جایزه: <b>{_escape(prize)}</b>"
    )

    return await _send_notification(bot, message)


async def notify_test_account(
    bot: Any,
    user: Any,
    config_name: str,
    volume_gb: float,
    days: int,
    devices: int,
) -> bool:
    """اعلان دریافت موفق اکانت تست."""

    name, telegram_id = _user_info(user)

    message = (
        "🧪 <b>اکانت تست جدید</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: <b>{name}</b>\n"
        f"🆔 شناسه: <code>{telegram_id}</code>\n"
        f"⚙️ نام سرویس: <code>{_escape(config_name)}</code>\n"
        f"💾 حجم: <b>{volume_gb:g} گیگابایت</b>\n"
        f"⏳ مدت: <b>{int(days)} روز</b>\n"
        f"📱 دستگاه: <b>{int(devices)}</b>"
    )

    return await _send_notification(bot, message)


async def notify_manual_renew(
    bot: Any,
    user: Any,
    config_name: str,
    days: int,
    amount: int,
) -> bool:
    """اعلان تمدید دستی موفق."""

    name, telegram_id = _user_info(user)

    message = (
        "🔄 <b>تمدید دستی سرویس</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: <b>{name}</b>\n"
        f"🆔 شناسه: <code>{telegram_id}</code>\n"
        f"⚙️ سرویس: <code>{_escape(config_name)}</code>\n"
        f"⏳ مدت تمدید: <b>{int(days)} روز</b>\n"
        f"💰 مبلغ پرداختی: <b>{int(amount):,} تومان</b>"
    )

    return await _send_notification(bot, message)


async def notify_auto_renew(
    bot: Any,
    user: Any,
    config_name: str,
    days: int,
    amount: int,
) -> bool:
    """اعلان تمدید خودکار موفق."""

    name, telegram_id = _user_info(user)

    message = (
        "⚙️ <b>تمدید خودکار سرویس</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 کاربر: <b>{name}</b>\n"
        f"🆔 شناسه: <code>{telegram_id}</code>\n"
        f"⚙️ سرویس: <code>{_escape(config_name)}</code>\n"
        f"⏳ مدت تمدید: <b>{int(days)} روز</b>\n"
        f"💰 مبلغ پرداختی: <b>{int(amount):,} تومان</b>"
    )

    return await _send_notification(bot, message)
