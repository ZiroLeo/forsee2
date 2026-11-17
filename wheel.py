
import logging
import secrets
import html

from dev.alerts import send_alert

from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest

from services.counter import release_reserved_number

from database import (
    can_spin_wheel,
    record_wheel_spin,
    add_balance,
    reserve_config_number,
    create_subscription,
    get_user,
    get_last_wheel_spin,
    log_event,
    record_user_activity,
)

from paxiva import PaxivaClient, PaxivaError
from utils import rate_limiter, generate_idempotency_key
from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from rich import premium_emoji
import ui


logger = logging.getLogger(__name__)
router = Router(name="wheel")


# ============================================================
# Settings
# ============================================================

COOLDOWN_HOURS = 24
CONFIG_DAYS = 30
CONFIG_DEVICES = 2


# ============================================================
# Inline Keyboards
# ============================================================

def _wheel_emoji_id(key: str) -> str:
    """دریافت شناسه ایموجی پریمیوم دکمه‌ها."""

    if not PREMIUM_EMOJI_ENABLED:
        raise RuntimeError(
            "Premium emojis must be enabled for the wheel keyboard."
        )

    if key == "back":
        return "5325598171018051937"

    emoji_id = PREMIUM_EMOJIS.get(key)

    if not emoji_id:
        raise KeyError(
            f"Premium emoji key is missing: {key}"
        )

    return str(emoji_id)


def wheel_keyboard(
    can_spin: bool = True,
) -> InlineKeyboardMarkup:

    if can_spin:
        spin_button = InlineKeyboardButton(
            text="چرخاندن گردونه",
            callback_data="wheel:spin",
            icon_custom_emoji_id=_wheel_emoji_id("dice"),
            style="success",
        )
    else:
        spin_button = InlineKeyboardButton(
            text="هنوز زمان چرخش نرسیده",
            callback_data="wheel:noop",
            icon_custom_emoji_id=_wheel_emoji_id("light"),
            style="primary",
        )

    back_button = InlineKeyboardButton(
        text="بازگشت به منوی اصلی",
        callback_data="menu:main",
        icon_custom_emoji_id=_wheel_emoji_id("back"),
        style="danger",
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [spin_button],
            [back_button],
        ]
    )


def wheel_result_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="مشاهده گردونه",
                    callback_data="menu:wheel",
                    icon_custom_emoji_id=_wheel_emoji_id("dice"),
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="منوی اصلی",
                    callback_data="menu:main",
                    icon_custom_emoji_id=_wheel_emoji_id("back"),
                    style="danger",
                )
            ],
        ]
    )


# ============================================================
# Safe Helpers
# ============================================================

async def answer_callback(callback: CallbackQuery):
    """پاسخ به Callback برای جلوگیری از منقضی شدن Query."""

    try:
        await callback.answer()
    except Exception:
        logger.warning(
            "Could not answer wheel callback."
        )


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    نمایش صفحه‌ی گردونه.

    کلیک از روی پیام اصلی (Rich) -> پیام جدید (پیام اصلی ادیت نمی‌شود).
    کلیک از روی پیام جدید گردونه  -> همان پیام ویرایش می‌شود
    (مثلاً «در حال ثبت جایزه» و بعد نتیجه).
    """

    shown = await ui.show(callback, text, keyboard)

    return shown is not None


def safe_log(
    level: str,
    event: str,
    user_id: int,
    details: str = "",
):
    """ثبت لاگ بدون متوقف کردن عملیات اصلی."""

    try:
        log_event(
            level,
            event,
            user_id,
            details,
        )
    except Exception:
        logger.exception(
            "Could not write wheel event log: %s",
            event,
        )


# ============================================================
# Date Helpers
# ============================================================

def parse_wheel_datetime(value) -> datetime | None:
    """
    تبدیل تاریخ ذخیره‌شده به datetime.

    فرمت‌های پشتیبانی‌شده:
    - Unix Timestamp عددی
    - Unix Timestamp رشته‌ای
    - Unix Timestamp میلی‌ثانیه‌ای
    - ISO 8601
    - datetime object
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        parsed = value

    else:
        raw_value = str(value).strip()

        if not raw_value:
            return None

        # تشخیص Unix Timestamp
        try:
            timestamp = float(raw_value)

            if timestamp > 0:
                # پشتیبانی از میلی‌ثانیه
                if timestamp > 10_000_000_000:
                    timestamp /= 1000

                parsed = datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                )
            else:
                return None

        except ValueError:
            # در صورت عدد نبودن، ISO را بررسی می‌کنیم
            try:
                parsed = datetime.fromisoformat(
                    raw_value.replace("Z", "+00:00")
                )
            except ValueError:
                logger.error(
                    "Unsupported wheel date format: %s",
                    raw_value,
                )
                return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )
    else:
        parsed = parsed.astimezone(
            timezone.utc
        )

    return parsed


def get_remaining_time(user_id: int) -> str:
    """محاسبه زمان باقی‌مانده تا چرخش بعدی."""

    try:
        last = get_last_wheel_spin(user_id)

        if not last:
            return "۲۴ ساعت"

        # پشتیبانی از dict و sqlite3.Row
        try:
            created_at = last["created_at"]
        except (KeyError, TypeError, IndexError):
            created_at = (
                last.get("created_at")
                if isinstance(last, dict)
                else None
            )

        if created_at is None:
            return "۲۴ ساعت"

        created_at = parse_wheel_datetime(
            created_at
        )

        if created_at is None:
            return "۲۴ ساعت"

        now = datetime.now(timezone.utc)

        elapsed = (
            now - created_at
        ).total_seconds()

        remaining = max(
            0,
            COOLDOWN_HOURS * 3600 - elapsed,
        )

        hours = int(remaining // 3600)
        minutes = int(
            (remaining % 3600) // 60
        )

        if hours == 0:
            return f"{minutes} دقیقه"

        return f"{hours} ساعت و {minutes} دقیقه"

    except Exception:
        logger.exception(
            "Could not calculate wheel cooldown."
        )
        return "۲۴ ساعت"


# ============================================================
# Wheel Text
# ============================================================

def get_wheel_page_text(
    can_spin: bool,
    remaining: str = "",
) -> str:
    """متن صفحه اصلی گردونه."""

    dot = premium_emoji("blue_dot", "•")

    if can_spin:
        return (
            f"{premium_emoji('dice', '🎲')} <b>گردونه شانس آی‌من نتورک</b>\n\n"
            f"{premium_emoji('gift', '🎁')} <b>جوایز گردونه:</b>\n\n"
            f"{dot} ۵۰٪ احتمال پوچ\n"
            f"{dot} ۲۰٪ احتمال دریافت ۲,۰۰۰ تومان\n"
            f"{dot} ۲۰٪ احتمال دریافت کانفیگ ۱ گیگ\n"
            f"{dot} ۱۰٪ احتمال دریافت کانفیگ ۲ گیگ\n\n"
            "هر کاربر هر ۲۴ ساعت یک بار می‌تواند گردونه را بچرخاند.\n\n"
            f"{premium_emoji('stars', '⭐')} <b>آماده‌ای شانست رو امتحان کنی؟</b>"
        )

    return (
        f"{premium_emoji('dice', '🎲')} <b>گردونه شانس</b>\n\n"
        "شما در ۲۴ ساعت گذشته از گردونه استفاده کرده‌اید.\n\n"
        f"{premium_emoji('light', '💡')} <b>زمان باقی‌مانده تا چرخش بعدی:</b>\n"
        f"<b>{html.escape(remaining)}</b>\n\n"
        "بعد از پایان این زمان می‌توانید دوباره شانس خود را امتحان کنید."
    )


# ============================================================
# Wheel Page
# ============================================================

@router.callback_query(F.data == "menu:wheel")
async def show_wheel(callback: CallbackQuery):

    await answer_callback(callback)

    user = callback.from_user

    if not user:
        return

    if not rate_limiter.is_allowed(user.id):
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "لطفاً چند لحظه صبر کنید و دوباره تلاش کنید."
            )
        return

    try:
        record_user_activity(user.id)

        can_spin = can_spin_wheel(
            user.id,
            cooldown_hours=COOLDOWN_HOURS,
        )

        remaining = ""

        if not can_spin:
            remaining = get_remaining_time(
                user.id
            )

        text = get_wheel_page_text(
            can_spin=can_spin,
            remaining=remaining,
        )

        await safe_edit(
            callback,
            text,
            wheel_keyboard(can_spin),
        )

    except Exception:
        logger.exception(
            "Failed to display wheel page."
        )

        if isinstance(callback.message, Message):
            await callback.message.answer(
                f"{premium_emoji('warning', '⚠️')} <b>در حال حاضر امکان نمایش گردونه وجود ندارد.</b>\n"
                "لطفاً کمی بعد دوباره تلاش کنید."
            )


# ============================================================
# Wheel - Noop
# ============================================================

@router.callback_query(F.data == "wheel:noop")
async def wheel_noop(callback: CallbackQuery):
    await answer_callback(callback)


# ============================================================
# Spin Wheel
# ============================================================

@router.callback_query(F.data == "wheel:spin")
async def spin_wheel(
    callback: CallbackQuery,
    paxiva: PaxivaClient,
):

    user = callback.from_user

    if not user:
        await answer_callback(callback)
        return

    await answer_callback(callback)

    if not rate_limiter.is_allowed(user.id):
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "درخواست‌های شما بیش از حد مجاز است.\n"
                "لطفاً کمی صبر کنید."
            )
        return

    try:
        # بررسی مجدد محدودیت
        if not can_spin_wheel(
            user.id,
            cooldown_hours=COOLDOWN_HOURS,
        ):
            remaining = get_remaining_time(
                user.id
            )

            await safe_edit(
                callback,
                (
                    f"{premium_emoji('light', '💡')} <b>هنوز زمان چرخش بعدی نرسیده است.</b>\n\n"
                    f"زمان باقی‌مانده: <b>{html.escape(remaining)}</b>"
                ),
                wheel_keyboard(False),
            )
            return

        # انتخاب جایزه
        roll = secrets.randbelow(100)

        if roll < 50:
            prize_type = "nothing"
            prize_text = "پوچ"
            prize_amount = 0
            prize_gb = 0

        elif roll < 70:
            prize_type = "balance"
            prize_text = "۲,۰۰۰ تومان هدیه"
            prize_amount = 2000
            prize_gb = 0

        elif roll < 90:
            prize_type = "config"
            prize_text = "کانفیگ ۱ گیگ رایگان"
            prize_amount = 0
            prize_gb = 1

        else:
            prize_type = "config"
            prize_text = "کانفیگ ۲ گیگ رایگان"
            prize_amount = 0
            prize_gb = 2

        # ثبت چرخش
        spin_id = record_wheel_spin(
            user.id,
            prize_text,
            prize_type,
        )

        if not spin_id:
            send_alert("ERROR", f"خطا در ثبت چرخش گردونه\nکاربر: {user.id}")
            await safe_edit(
                callback,
                (
                    f"{premium_emoji('false', '❌')} <b>ثبت چرخش انجام نشد.</b>\n\n"
                    "لطفاً دوباره وارد گردونه شوید."
                ),
                wheel_result_keyboard(),
            )
            return

        # نمایش وضعیت پردازش
        await safe_edit(
            callback,
            (
                f"{premium_emoji('dice', '🎲')} <b>گردونه چرخید!</b>\n\n"
                "در حال ثبت جایزه شما هستیم...\n"
                "لطفاً تا پایان عملیات صبر کنید."
            ),
            None,
        )

        # ----------------------------------------------------
        # Prize: Nothing
        # ----------------------------------------------------

        if prize_type == "nothing":

            text = (
                f"{premium_emoji('dice', '🎲')} <b>نتیجه گردونه</b>\n\n"
                "متأسفانه این بار جایزه‌ای برنده نشدید!\n\n"
                "شانس خود را ۲۴ ساعت دیگر امتحان کنید."
            )

            await safe_edit(
                callback,
                text,
                wheel_result_keyboard(),
            )

            safe_log(
                "INFO",
                "wheel_spin_nothing",
                user.id,
                f"spin_id={spin_id}",
            )

            return

        # ----------------------------------------------------
        # Prize: Balance
        # ----------------------------------------------------

        if prize_type == "balance":

            success = add_balance(
                user.id,
                prize_amount,
                "wheel_prize",
                "جایزه گردونه شانس",
            )

            if not success:

                logger.error(
                    "Could not add wheel balance prize. user_id=%s",
                    user.id,
                )
                send_alert(
                    "ERROR",
                    f"خطا در واریز جایزه نقدی گردونه\n"
                    f"کاربر: {user.id}\n"
                    f"مبلغ: {prize_amount}\n"
                    f"spin_id: {spin_id}"
                )

                await safe_edit(
                    callback,
                    (
                        f"{premium_emoji('warning', '⚠️')} <b>خطا در واریز جایزه</b>\n\n"
                        "گردونه شما ثبت شده، اما واریز جایزه انجام نشد.\n"
                        "لطفاً با پشتیبانی تماس بگیرید."
                    ),
                    wheel_result_keyboard(),
                )

                safe_log(
                    "ERROR",
                    "wheel_balance_failed",
                    user.id,
                    f"spin_id={spin_id}, amount={prize_amount}",
                )

                return

            user_data = get_user(user.id)

            new_balance = (
                user_data.get("balance", 0)
                if user_data else 0
            )

            text = (
                f"{premium_emoji('gift', '🎁')} <b>تبریک! برنده شدی!</b>\n\n"
                f"جایزه شما: <b>{html.escape(prize_text)}</b>\n\n"
                f"{premium_emoji('wallet', '👛')} <b>موجودی جدید کیف پول:</b>\n"
                f"<b>{new_balance:,} تومان</b>\n\n"
                "امیدواریم دفعه بعد هم برنده شوید!"
            )

            await safe_edit(
                callback,
                text,
                wheel_result_keyboard(),
            )

            safe_log(
                "INFO",
                "wheel_spin_balance",
                user.id,
                f"spin_id={spin_id}, amount={prize_amount}",
            )

            return

        # ----------------------------------------------------
        # Prize: Config
        # ----------------------------------------------------

        if prize_type == "config":

            config_number = None
            client_created = False

            try:
                # رزرو شماره کانفیگ
                config_number = reserve_config_number(
                    is_test=False
                )

                if not config_number:
                    raise RuntimeError(
                        "Could not reserve config number."
                    )

                config_name = f"iMan-{config_number}"

                idempotency_key = generate_idempotency_key(
                    f"wheel-{user.id}-{spin_id}-{config_number}"
                )

                # ساخت کانفیگ در پنل
                client_data = await paxiva.create_client(
                    name=config_name,
                    volume_gb=prize_gb,
                    days=CONFIG_DAYS,
                    devices=CONFIG_DEVICES,
                    idempotency_key=idempotency_key,
                )

                if not isinstance(client_data, dict):
                    raise RuntimeError(
                        "Invalid response from PAXIVA."
                    )

                paxiva_id = client_data.get("id")
                link = client_data.get("link", "")

                if not paxiva_id:
                    raise RuntimeError(
                        "PAXIVA response does not contain client ID."
                    )

                client_created = True

                # ذخیره اشتراک در دیتابیس
                sub_id = create_subscription(
                    telegram_id=user.id,
                    config_number=config_number,
                    config_name=config_name,
                    kind="vol",
                    volume_gb=prize_gb,
                    days=CONFIG_DAYS,
                    devices=CONFIG_DEVICES,
                    price_toman=0,
                    price_stars=0,
                    paxiva_client_id=paxiva_id,
                    link=link,
                )

                if not sub_id:
                    raise RuntimeError(
                        "Could not save subscription in database."
                    )

                text = (
                    f"{premium_emoji('gift', '🎁')} <b>تبریک! برنده کانفیگ رایگان شدی!</b>\n\n"
                    f"جایزه: <b>{html.escape(prize_text)}</b>\n\n"
                    f"نام کانفیگ: <code>{html.escape(config_name)}</code>\n"
                    f"حجم: <b>{prize_gb} گیگابایت</b>\n"
                    f"مدت اعتبار: <b>{CONFIG_DAYS} روز</b>\n"
                    f"تعداد دستگاه: <b>{CONFIG_DEVICES}</b>\n\n"
                )

                if link:
                    text += (
                        f"{premium_emoji('safe', '🔐')} <b>لینک اتصال:</b>\n"
                        f"<code>{html.escape(str(link))}</code>\n\n"
                    )
                else:
                    text += (
                        "لینک اتصال در پاسخ پنل موجود نبود.\n"
                        "لطفاً از بخش حساب کاربری کانفیگ خود را بررسی کنید.\n\n"
                    )

                text += "از سرویس خود لذت ببرید!"

                await safe_edit(
                    callback,
                    text,
                    wheel_result_keyboard(),
                )

                safe_log(
                    "INFO",
                    "wheel_spin_config",
                    user.id,
                    (
                        f"spin_id={spin_id}, "
                        f"sub_id={sub_id}, "
                        f"config={config_name}, "
                        f"gb={prize_gb}"
                    ),
                )

            except PaxivaError as error:

                logger.exception(
                    "PAXIVA wheel config creation failed."
                )

                send_alert(
                    "ERROR",
                    f"خطای PAXIVA در ساخت کانفیگ گردونه\n"
                    f"کاربر: {user.id}\n"
                    f"spin_id: {spin_id}\n"
                    f"prize_gb: {prize_gb}\n"
                    f"error: {str(error)[:300]}"
                )

                if config_number is not None:
                    try:
                        release_reserved_number(
                            config_number,
                            is_test=False,
                        )
                    except Exception:
                        logger.exception(
                            "Could not release reserved config number."
                        )

                await safe_edit(
                    callback,
                    (
                        f"{premium_emoji('warning', '⚠️')} <b>ساخت کانفیگ با مشکل مواجه شد.</b>\n\n"
                        "چرخش شما ثبت شده، اما ساخت جایزه کامل نشد.\n"
                        "لطفاً با پشتیبانی تماس بگیرید."
                    ),
                    wheel_result_keyboard(),
                )

                safe_log(
                    "ERROR",
                    "wheel_config_failed",
                    user.id,
                    f"spin_id={spin_id}, error={str(error)}",
                )

            except Exception as error:

                logger.exception(
                    "Unexpected error during wheel config creation."
                )

                send_alert(
                    "ERROR",
                    f"خطای غیرمنتظره در ساخت کانفیگ گردونه\n"
                    f"کاربر: {user.id}\n"
                    f"spin_id: {spin_id}\n"
                    f"prize_gb: {prize_gb}\n"
                    f"error: {str(error)[:300]}"
                )

                if config_number is not None and not client_created:
                    try:
                        release_reserved_number(
                            config_number,
                            is_test=False,
                        )
                    except Exception:
                        logger.exception(
                            "Could not release reserved config number."
                        )

                await safe_edit(
                    callback,
                    (
                        f"{premium_emoji('warning', '⚠️')} <b>خطای غیرمنتظره رخ داد.</b>\n\n"
                        "چرخش شما ثبت شده، اما ساخت کانفیگ با مشکل مواجه شد.\n"
                        "لطفاً با پشتیبانی تماس بگیرید."
                    ),
                    wheel_result_keyboard(),
                )

                safe_log(
                    "ERROR",
                    "wheel_unexpected",
                    user.id,
                    f"spin_id={spin_id}, error={str(error)}",
                )

            return

    except Exception:

        logger.exception(
            "Fatal error during wheel spin. user_id=%s",
            user.id,
        )

        send_alert(
            "ERROR",
            f"خطای مرگبار در گردونه شانس\n"
            f"کاربر: {user.id}"
        )

        await safe_edit(
            callback,
            (
                f"{premium_emoji('warning', '⚠️')} <b>خطایی هنگام چرخاندن گردونه رخ داد.</b>\n\n"
                "لطفاً کمی بعد دوباره تلاش کنید.\n"
                "اگر از حساب شما یک چرخش کم شده، با پشتیبانی تماس بگیرید."
            ),
            wheel_result_keyboard(),
        )
