import logging
import secrets
import html
import uuid
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest

from dev.alerts import send_alert
from services.notifications import notify_wheel_prize
from services.counter import release_reserved_number

from database import (
    can_spin_wheel,
    try_record_spin,
    void_wheel_spin,
    tehran_day_start_ts,
    count_referrals_since,
    add_balance,
    reserve_config_number,
    create_subscription,
    get_user,
    get_last_wheel_spin,
    log_event,
    record_user_activity,
    create_discount_code,
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
MIN_REFERRALS_REQUIRED = 1      # حداقل رفرال «در همان روز» (به وقت تهران) برای هر چرخش
DISCOUNT_VALID_DAYS = 7         # اعتبار کد تخفیف جایزه (روز)


# ============================================================
# Referral (daily) + spin rollback helpers
# ============================================================

def _referrals_today(user_id: int) -> int:
    """تعداد دعوت‌های امروز کاربر (از ۰۰:۰۰ به وقت تهران)."""
    try:
        return count_referrals_since(user_id, tehran_day_start_ts())
    except Exception:
        logger.exception("Could not count today's referrals. user_id=%s", user_id)
        return 0


def _void_spin(spin_id) -> bool:
    """جایزه تحویل نشد: چرخش برگردانده می‌شود تا کاربر دوباره بتواند بچرخاند."""
    try:
        return bool(void_wheel_spin(spin_id))
    except Exception:
        logger.exception("Could not void wheel spin %s", spin_id)
        return False


_VOIDED = "چرخش شما برگردانده شد و می‌توانید دوباره تلاش کنید."
_NOT_VOIDED = "لطفاً با پشتیبانی تماس بگیرید."


# ============================================================
# Helpers - User Info for Alerts
# ============================================================

def _user_tag(user) -> str:
    """ساخت شناسه‌ی خوانا از کاربر برای هشدارهای دولوپر."""
    try:
        username = f"@{user.username}" if user.username else "بدون یوزرنیم"
        name = user.full_name or "—"
        return f"{user.id} | {username} | {name}"
    except Exception:
        return f"{getattr(user, 'id', '?')}"


# ============================================================
# Inline Keyboards
# ============================================================

def _wheel_emoji_id(key: str) -> str:
    if not PREMIUM_EMOJI_ENABLED:
        raise RuntimeError("Premium emojis must be enabled for the wheel keyboard.")
    if key == "back":
        return "5325598171018051937"
    emoji_id = PREMIUM_EMOJIS.get(key)
    if not emoji_id:
        raise KeyError(f"Premium emoji key is missing: {key}")
    return str(emoji_id)


def wheel_keyboard(can_spin: bool = True) -> InlineKeyboardMarkup:
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
    return InlineKeyboardMarkup(inline_keyboard=[[spin_button], [back_button]])


def wheel_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="مشاهده گردونه", callback_data="menu:wheel", icon_custom_emoji_id=_wheel_emoji_id("dice"), style="primary")],
            [InlineKeyboardButton(text="منوی اصلی", callback_data="menu:main", icon_custom_emoji_id=_wheel_emoji_id("back"), style="danger")],
        ]
    )


def wheel_no_referral_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="رفرال", callback_data="menu:referral", icon_custom_emoji_id=_wheel_emoji_id("referral"), style="primary")],
            [InlineKeyboardButton(text="بازگشت", callback_data="menu:main", icon_custom_emoji_id=_wheel_emoji_id("back"), style="danger")],
        ]
    )


# ============================================================
# Safe Helpers
# ============================================================

async def answer_callback(callback: CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        logger.warning("Could not answer wheel callback.")


async def safe_edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None) -> bool:
    shown = await ui.show(callback, text, keyboard)
    return shown is not None


def safe_log(level: str, event: str, user_id: int, details: str = ""):
    try:
        log_event(level, event, user_id, details)
    except Exception:
        logger.exception("Could not write wheel event log: %s", event)


# ============================================================
# Date Helpers
# ============================================================

def parse_wheel_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw_value = str(value).strip()
        if not raw_value:
            return None
        try:
            timestamp = float(raw_value)
            if timestamp > 0:
                if timestamp > 10_000_000_000:
                    timestamp /= 1000
                parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            else:
                return None
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                logger.error("Unsupported wheel date format: %s", raw_value)
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def get_remaining_time(user_id: int) -> str:
    try:
        last = get_last_wheel_spin(user_id)
        if not last:
            return "۲۴ ساعت"
        try:
            created_at = last["created_at"]
        except (KeyError, TypeError, IndexError):
            created_at = last.get("created_at") if isinstance(last, dict) else None
        if created_at is None:
            return "۲۴ ساعت"
        created_at = parse_wheel_datetime(created_at)
        if created_at is None:
            return "۲۴ ساعت"
        now = datetime.now(timezone.utc)
        elapsed = (now - created_at).total_seconds()
        remaining = max(0, COOLDOWN_HOURS * 3600 - elapsed)
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        if hours == 0:
            return f"{minutes} دقیقه"
        return f"{hours} ساعت و {minutes} دقیقه"
    except Exception:
        logger.exception("Could not calculate wheel cooldown.")
        return "۲۴ ساعت"


# ============================================================
# Wheel Text
# ============================================================

def get_wheel_page_text(can_spin: bool, remaining: str = "", has_referral: bool = True) -> str:
    dot = premium_emoji("blue_dot", "•")

    if not has_referral:
        return (
            f"{premium_emoji('dice', '🎲')} <b>گردونه شانس آی‌من نتورک</b>\n\n"
            f"{premium_emoji('warning', '⚠️')} <b>شرط شرکت در گردونه را ندارید.</b>\n\n"
            "برای چرخاندن گردونه در هر روز، باید <b>همان روز حداقل ۱ نفر</b> را با لینک رفرال خودتان به ربات دعوت کرده باشید "
            "(روز به وقت تهران، از ساعت ۰۰:۰۰).\n"
            "با دعوت یک دوست جدید، گردونه امروز شما فعال می‌شود."
        )

    if can_spin:
        return (
            f"{premium_emoji('dice', '🎲')} <b>گردونه شانس آی‌من نتورک</b>\n\n"
            f"{premium_emoji('gift', '🎁')} <b>جوایز گردونه:</b>\n\n"
            f"{dot} ۱,۰۰۰ تومان موجودی کیف پول\n"
            f"{dot} ۲,۰۰۰ تومان موجودی کیف پول\n"
            f"{dot} کد تخفیف ۱۰ درصدی\n"
            f"{dot} کد تخفیف ۲۰ درصدی\n"
            f"{dot} کانفیگ یک گیگابایتی\n"
            f"{dot} کانفیگ ۲ گیگابایتی\n\n"
            "هر کاربر هر ۲۴ ساعت یک بار می‌تواند گردونه را بچرخاند.\n"
            "برای هر چرخش، همان روز باید حداقل <b>۱ رفرال جدید</b> آورده باشید.\n\n"
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
            await callback.message.answer("لطفاً چند لحظه صبر کنید و دوباره تلاش کنید.")
        return

    try:
        record_user_activity(user.id)

        can_spin = can_spin_wheel(user.id, cooldown_hours=COOLDOWN_HOURS)
        remaining = "" if can_spin else get_remaining_time(user.id)
        has_referral = _referrals_today(user.id) >= MIN_REFERRALS_REQUIRED

        # اگر هنوز در بازهٔ ۲۴ ساعته است، اول زمان باقی‌مانده نشان داده می‌شود
        text = get_wheel_page_text(can_spin, remaining, has_referral or not can_spin)

        if can_spin and not has_referral:
            keyboard = wheel_no_referral_keyboard()
        else:
            keyboard = wheel_keyboard(can_spin)

        await safe_edit(callback, text, keyboard)

    except Exception:
        logger.exception("Failed to display wheel page.")
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
async def spin_wheel(callback: CallbackQuery, paxiva: PaxivaClient):
    user = callback.from_user
    if not user:
        await answer_callback(callback)
        return

    await answer_callback(callback)

    if not rate_limiter.is_allowed(user.id):
        if isinstance(callback.message, Message):
            await callback.message.answer("درخواست‌های شما بیش از حد مجاز است.\nلطفاً کمی صبر کنید.")
        return

    try:
        # محدودیت ۲۴ ساعته (بررسی سریع؛ بررسی نهایی اتمیک در try_record_spin است)
        if not can_spin_wheel(user.id, cooldown_hours=COOLDOWN_HOURS):
            remaining = get_remaining_time(user.id)
            await safe_edit(
                callback,
                f"{premium_emoji('light', '💡')} <b>هنوز زمان چرخش بعدی نرسیده است.</b>\n\n"
                f"زمان باقی‌مانده: <b>{html.escape(remaining)}</b>",
                wheel_keyboard(False),
            )
            return

        # شرط رفرال روزانه (بررسی سریع)
        if _referrals_today(user.id) < MIN_REFERRALS_REQUIRED:
            await safe_edit(
                callback,
                get_wheel_page_text(can_spin=False, has_referral=False),
                wheel_no_referral_keyboard(),
            )
            return

        # انتخاب جایزه
        roll = secrets.randbelow(100)

        if roll < 40:
            prize_type = "nothing"
            prize_text = "پوچ"
            prize_amount = 0
            prize_gb = 0
            prize_discount_percent = 0
        elif roll < 60:
            prize_type = "balance"
            prize_text = "۱,۰۰۰ تومان هدیه"
            prize_amount = 1000
            prize_gb = 0
            prize_discount_percent = 0
        elif roll < 80:
            prize_type = "discount"
            prize_text = "کد تخفیف ۱۰٪"
            prize_amount = 0
            prize_gb = 0
            prize_discount_percent = 10
        elif roll < 90:
            prize_type = "discount"
            prize_text = "کد تخفیف ۲۰٪"
            prize_amount = 0
            prize_gb = 0
            prize_discount_percent = 20
        elif roll < 95:
            prize_type = "balance"
            prize_text = "۲,۰۰۰ تومان هدیه"
            prize_amount = 2000
            prize_gb = 0
            prize_discount_percent = 0
        elif roll < 98:
            prize_type = "config"
            prize_text = "کانفیگ ۱ گیگ رایگان"
            prize_amount = 0
            prize_gb = 1
            prize_discount_percent = 0
        else:
            prize_type = "config"
            prize_text = "کانفیگ ۲ گیگ رایگان"
            prize_amount = 0
            prize_gb = 2
            prize_discount_percent = 0

        # ثبت چرخش
        spin_id, reason = try_record_spin(
            user.id, prize_text, prize_type,
            cooldown_hours=COOLDOWN_HOURS, min_referrals_today=MIN_REFERRALS_REQUIRED,
        )

        if reason == "cooldown":
            await safe_edit(
                callback,
                f"{premium_emoji('light', '💡')} <b>هنوز زمان چرخش بعدی نرسیده است.</b>\n\n"
                f"زمان باقی‌مانده: <b>{html.escape(get_remaining_time(user.id))}</b>",
                wheel_keyboard(False),
            )
            return

        if reason == "no_referral_today":
            await safe_edit(
                callback,
                get_wheel_page_text(can_spin=False, has_referral=False),
                wheel_no_referral_keyboard(),
            )
            return

        if not spin_id:
            send_alert(
                "ERROR",
                f"خطا در ثبت چرخش گردونه\n"
                f"کاربر: {_user_tag(user)}\n"
                f"نتیجه: {prize_text}"
            )
            await safe_edit(
                callback,
                f"{premium_emoji('false', '❌')} <b>ثبت چرخش انجام نشد.</b>\n\nلطفاً دوباره وارد گردونه شوید.",
                wheel_result_keyboard(),
            )
            return

        await safe_edit(
            callback,
            f"{premium_emoji('dice', '🎲')} <b>گردونه چرخید!</b>\n\n"
            "در حال ثبت جایزه شما هستیم...\nلطفاً تا پایان عملیات صبر کنید.",
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
            await safe_edit(callback, text, wheel_result_keyboard())
            safe_log("INFO", "wheel_spin_nothing", user.id, f"spin_id={spin_id}")
            return

        # ----------------------------------------------------
        # Prize: Balance
        # ----------------------------------------------------
        if prize_type == "balance":
            success = add_balance(user.id, prize_amount, "wheel_prize", "جایزه گردونه شانس",
                                  reference_id=f"wheel:{spin_id}")

            if not success:
                voided = _void_spin(spin_id)
                logger.error("Could not add wheel balance prize. user_id=%s", user.id)
                send_alert(
                    "ERROR",
                    f"خطا در واریز جایزه نقدی گردونه\n"
                    f"کاربر: {_user_tag(user)}\n"
                    f"مبلغ: {prize_amount}\n"
                    f"spin_id: {spin_id}"
                )
                await safe_edit(
                    callback,
                    f"{premium_emoji('warning', '⚠️')} <b>خطا در واریز جایزه</b>\n\n"
                    f"واریز جایزه انجام نشد. {_VOIDED if voided else _NOT_VOIDED}",
                    wheel_result_keyboard(),
                )
                safe_log("ERROR", "wheel_balance_failed", user.id, f"spin_id={spin_id}, amount={prize_amount}")
                return

            user_data = get_user(user.id)
            new_balance = user_data.get("balance", 0) if user_data else 0

            text = (
                f"{premium_emoji('gift', '🎁')} <b>تبریک! برنده شدی!</b>\n\n"
                f"جایزه شما: <b>{html.escape(prize_text)}</b>\n\n"
                f"{premium_emoji('wallet', '👛')} <b>موجودی جدید کیف پول:</b>\n"
                f"<b>{new_balance:,} تومان</b>\n\n"
                "امیدواریم دفعه بعد هم برنده شوید!"
            )
            await safe_edit(callback, text, wheel_result_keyboard())
            safe_log("INFO", "wheel_spin_balance", user.id, f"spin_id={spin_id}, amount={prize_amount}")

            try:
                await notify_wheel_prize(bot=callback.bot, user=user, prize=prize_text, prize_type="balance")
            except Exception:
                logger.exception("Wheel balance notification failed")
            return

        # ----------------------------------------------------
        # Prize: Discount Code
        # ----------------------------------------------------
        if prize_type == "discount":
            code_id = None
            try:
                code_str, code_id = "", None
                for _ in range(3):  # در صورت تکراری بودن کد، دوباره تلاش می‌شود
                    code_str = f"WHEEL{uuid.uuid4().hex[:8].upper()}"
                    code_id = create_discount_code(
                        code=code_str,
                        percent=prize_discount_percent,
                        max_uses=1,
                        owner_id=user.id,
                        expires_days=DISCOUNT_VALID_DAYS,
                    )
                    if code_id:
                        break

                if not code_id:
                    voided = _void_spin(spin_id)
                    logger.error("Could not create wheel discount code. user_id=%s", user.id)
                    send_alert(
                        "ERROR",
                        f"خطا در ساخت کد تخفیف گردونه\n"
                        f"کاربر: {_user_tag(user)}\n"
                        f"درصد: {prize_discount_percent}%\n"
                        f"spin_id: {spin_id}"
                    )
                    await safe_edit(
                        callback,
                        f"{premium_emoji('warning', '⚠️')} <b>خطا در ساخت کد تخفیف</b>\n\n"
                        f"کد تخفیف ساخته نشد. {_VOIDED if voided else _NOT_VOIDED}",
                        wheel_result_keyboard(),
                    )
                    return

                text = (
                    f"{premium_emoji('gift', '🎁')} <b>تبریک! برنده کد تخفیف شدی!</b>\n\n"
                    f"جایزه شما: <b>{html.escape(prize_text)}</b>\n\n"
                    f"{premium_emoji('light', '🎟️')} <b>کد تخفیف شما:</b>\n"
                    f"<code>{html.escape(code_str)}</code>\n\n"
                    f"این کد فقط برای <b>یک بار</b> و فقط برای <b>خودتان</b> قابل استفاده است.\n"
                    f"درصد تخفیف: <b>{prize_discount_percent}%</b>\n"
                    f"اعتبار: <b>{DISCOUNT_VALID_DAYS} روز</b>\n\n"
                    "هنگام خرید، در مرحلهٔ «کد تخفیف» این کد را وارد کنید.\n\n"
                    "از سرویس خود لذت ببرید!"
                )
                await safe_edit(callback, text, wheel_result_keyboard())
                safe_log("INFO", "wheel_spin_discount", user.id, f"spin_id={spin_id}, code={code_str}, percent={prize_discount_percent}")

                try:
                    await notify_wheel_prize(bot=callback.bot, user=user, prize=prize_text, prize_type="discount")
                except Exception:
                    logger.exception("Wheel discount notification failed")

            except Exception as error:
                logger.exception("Wheel discount creation failed")
                # اگر کد ساخته شده بود، چرخش برنگردد (کد معتبر است)
                voided = _void_spin(spin_id) if not code_id else False
                send_alert(
                    "ERROR",
                    f"خطای غیرمنتظره در ساخت کد تخفیف گردونه\n"
                    f"کاربر: {_user_tag(user)}\n"
                    f"spin_id: {spin_id}\n"
                    f"error: {str(error)[:300]}"
                )
                await safe_edit(
                    callback,
                    f"{premium_emoji('warning', '⚠️')} <b>خطای غیرمنتظره رخ داد.</b>\n\n"
                    f"ساخت کد تخفیف با مشکل مواجه شد. {_VOIDED if voided else _NOT_VOIDED}",
                    wheel_result_keyboard(),
                )
            return

        # ----------------------------------------------------
        # Prize: Config
        # ----------------------------------------------------
        if prize_type == "config":
            config_number = None
            paxiva_id = None
            client_created = False
            sub_saved = False  # بعد از ثبت اشتراک، هیچ rollbackی مجاز نیست

            try:
                config_number = reserve_config_number(is_test=False)
                if not config_number:
                    raise RuntimeError("Could not reserve config number.")

                config_name = f"iMan-{config_number}"
                idempotency_key = generate_idempotency_key(f"wheel-{user.id}-{spin_id}-{config_number}")

                client_data = await paxiva.create_client(
                    name=config_name,
                    volume_gb=prize_gb,
                    days=CONFIG_DAYS,
                    devices=CONFIG_DEVICES,
                    idempotency_key=idempotency_key,
                )

                if not isinstance(client_data, dict):
                    raise RuntimeError("Invalid response from PAXIVA.")

                paxiva_id = client_data.get("id")
                link = client_data.get("link", "")

                if not paxiva_id:
                    raise RuntimeError("PAXIVA response does not contain client ID.")

                client_created = True

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
                    raise RuntimeError("Could not save subscription in database.")
                sub_saved = True

                text = (
                    f"{premium_emoji('gift', '🎁')} <b>تبریک! برنده کانفیگ رایگان شدی!</b>\n\n"
                    f"جایزه: <b>{html.escape(prize_text)}</b>\n\n"
                    f"نام کانفیگ: <code>{html.escape(config_name)}</code>\n"
                    f"حجم: <b>{prize_gb} گیگابایت</b>\n"
                    f"مدت اعتبار: <b>{CONFIG_DAYS} روز</b>\n"
                    f"تعداد دستگاه: <b>{CONFIG_DEVICES}</b>\n\n"
                )
                if link:
                    text += f"{premium_emoji('safe', '🔐')} <b>لینک اتصال:</b>\n<code>{html.escape(str(link))}</code>\n\n"
                else:
                    text += "لینک اتصال در پاسخ پنل موجود نبود.\nلطفاً از بخش حساب کاربری کانفیگ خود را بررسی کنید.\n\n"
                text += "از سرویس خود لذت ببرید!"

                await safe_edit(callback, text, wheel_result_keyboard())
                safe_log("INFO", "wheel_spin_config", user.id, f"spin_id={spin_id}, sub_id={sub_id}, config={config_name}, gb={prize_gb}")

                try:
                    await notify_wheel_prize(bot=callback.bot, user=user, prize=prize_text, prize_type="config")
                except Exception:
                    logger.exception("Wheel config notification failed")

            except PaxivaError as error:
                logger.exception("PAXIVA wheel config creation failed.")
                send_alert(
                    "ERROR",
                    f"خطای PAXIVA در ساخت کانفیگ گردونه\n"
                    f"کاربر: {_user_tag(user)}\n"
                    f"spin_id: {spin_id}\n"
                    f"prize_gb: {prize_gb}\n"
                    f"error: {str(error)[:300]}"
                )
                if config_number is not None:
                    try:
                        release_reserved_number(config_number, is_test=False)
                    except Exception:
                        logger.exception("Could not release reserved config number.")
                voided = _void_spin(spin_id)
                await safe_edit(
                    callback,
                    f"{premium_emoji('warning', '⚠️')} <b>ساخت کانفیگ با مشکل مواجه شد.</b>\n\n"
                    f"{_VOIDED if voided else _NOT_VOIDED}",
                    wheel_result_keyboard(),
                )
                safe_log("ERROR", "wheel_config_failed", user.id, f"spin_id={spin_id}, error={str(error)}")

            except Exception as error:
                logger.exception("Unexpected error during wheel config creation.")
                send_alert(
                    "ERROR",
                    f"خطای غیرمنتظره در ساخت کانفیگ گردونه\n"
                    f"کاربر: {_user_tag(user)}\n"
                    f"spin_id: {spin_id}\n"
                    f"prize_gb: {prize_gb}\n"
                    f"error: {str(error)[:300]}"
                )
                if sub_saved:
                    # کانفیگ ساخته و ثبت شده؛ فقط نمایش/اعلان خطا داده است. حذف یا برگرداندن چرخش ممنوع.
                    await safe_edit(
                        callback,
                        f"{premium_emoji('gift', '🎁')} <b>کانفیگ جایزه شما ساخته شد.</b>\n\n"
                        "نمایش آن با خطا مواجه شد؛ از بخش «حساب کاربری» کانفیگ خود را ببینید.",
                        wheel_result_keyboard(),
                    )
                    return
                # اگر سرویس در پنل ساخته شده ولی ثبت محلی شکست خورد، سرویس یتیم نماند
                cleaned = not client_created
                if client_created and paxiva_id:
                    try:
                        await paxiva.delete_client(int(paxiva_id), purge=False)
                        cleaned = True
                    except Exception:
                        logger.exception("Could not delete orphan wheel client %s", paxiva_id)
                        send_alert(
                            "CRITICAL",
                            "سرویس یتیم گردونه در پنل ماند (حذف نشد)",
                            user_id=user.id, client_id=paxiva_id, spin_id=spin_id,
                        )
                if config_number is not None and cleaned:
                    try:
                        release_reserved_number(config_number, is_test=False)
                    except Exception:
                        logger.exception("Could not release reserved config number.")
                voided = _void_spin(spin_id) if cleaned else False
                await safe_edit(
                    callback,
                    f"{premium_emoji('warning', '⚠️')} <b>خطای غیرمنتظره رخ داد.</b>\n\n"
                    f"ساخت کانفیگ با مشکل مواجه شد. {_VOIDED if voided else _NOT_VOIDED}",
                    wheel_result_keyboard(),
                )
                safe_log("ERROR", "wheel_unexpected", user.id, f"spin_id={spin_id}, error={str(error)}")
            return

    except Exception:
        logger.exception("Fatal error during wheel spin. user_id=%s", user.id)
        send_alert(
            "ERROR",
            f"خطای مرگبار در گردونه شانس\nکاربر: {_user_tag(user)}"
        )
        await safe_edit(
            callback,
            f"{premium_emoji('warning', '⚠️')} <b>خطایی هنگام چرخاندن گردونه رخ داد.</b>\n\n"
            "لطفاً کمی بعد دوباره تلاش کنید.\nاگر از حساب شما یک چرخش کم شده، با پشتیبانی تماس بگیرید.",
            wheel_result_keyboard(),
        )
