import asyncio
import html
import logging
import time
import uuid

from typing import Any, Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED

from database import (
    get_user,
    get_user_subscriptions,
    get_subscription,
    get_subscription_product,
    toggle_auto_renew,
    log_event,

    # APIهای جدید تمدید که باید در database.py اضافه شوند:
    create_renewal_order,
    mark_renewal_processing,
    complete_renewal_order,
    fail_renewal_order,
    get_renewal_order,
)

from paxiva import PaxivaClient, PaxivaError

from services.notifications import notify_manual_renew
from dev.alerts import send_alert, user_context

from rich import premium_emoji
import ui

from utils import (
    rate_limiter,
    format_bytes,
    format_days_remaining,
)

logger = logging.getLogger(__name__)

router = Router(name="renew")


# ============================================================
# SETTINGS
# ============================================================

VOLUME_RENEW_THRESHOLD_GB = 0.2
UNLIMITED_RENEW_THRESHOLD_SECONDS = 3 * 86400

MAX_RENEW_LIST = 7

GIB = 1024 ** 3


# ============================================================
# LOCKS
# ============================================================

# این قفل فقط برای جلوگیری از تداخل در همان پروسه است.
# جلوگیری اصلی از سفارش تکراری باید توسط دیتابیس انجام شود.

_renew_locks: dict[int, asyncio.Lock] = {}


def _get_lock(subscription_id: int) -> asyncio.Lock:
    lock = _renew_locks.get(subscription_id)

    if lock is None:
        lock = asyncio.Lock()
        _renew_locks[subscription_id] = lock

    return lock


# ============================================================
# HELPERS
# ============================================================

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)

        if result != result or result in (float("inf"), float("-inf")):
            return default

        return result

    except (TypeError, ValueError, OverflowError):
        return default


def _safe_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _premium(key: str, fallback: str = "◈") -> str:
    try:
        return premium_emoji(key, fallback)
    except Exception:
        return fallback


def _button(
    text: str,
    callback_data: Optional[str] = None,
    *,
    url: Optional[str] = None,
    style: str = "primary",
    emoji_key: Optional[str] = None,
) -> InlineKeyboardButton:

    if style not in ("primary", "success", "danger"):
        raise ValueError("Invalid button style")

    if callback_data and url:
        raise ValueError("Button cannot have callback_data and url together")

    if not callback_data and not url:
        raise ValueError("Button requires callback_data or url")

    kwargs = {
        "text": text,
        "style": style,
    }

    if callback_data:
        if len(callback_data.encode("utf-8")) > 64:
            raise ValueError("callback_data exceeds Telegram limit")

        kwargs["callback_data"] = callback_data

    if url:
        if not url.startswith(("https://", "http://")):
            raise ValueError("Invalid button URL")

        kwargs["url"] = url

    if PREMIUM_EMOJI_ENABLED and emoji_key:
        emoji_id = PREMIUM_EMOJIS.get(emoji_key)

        if emoji_id:
            kwargs["icon_custom_emoji_id"] = str(emoji_id)

    return InlineKeyboardButton(**kwargs)


def _get_product(subscription_id: int) -> Optional[dict]:
    try:
        product = get_subscription_product(subscription_id)

        if not product:
            return None

        return product

    except Exception:
        logger.exception(
            "Could not load linked product: subscription_id=%s",
            subscription_id,
        )
        return None


def _get_product_price(product: dict) -> int:
    price = _safe_int(product.get("price_toman"))

    if price <= 0:
        raise ValueError("Invalid product price")

    return price


def _get_product_kind(product: dict) -> str:
    kind = product.get("kind")

    if kind not in ("vol", "unl"):
        raise ValueError("Invalid product kind")

    return kind


def _get_product_days(product: dict) -> int:
    kind = _get_product_kind(product)

    if kind == "vol":
        return 0

    days = _safe_int(product.get("days"))

    if days != 30:
        raise ValueError("Unlimited renewal product must be 30 days")

    return days


def _subscription_is_renewable(sub: dict) -> bool:

    if not sub:
        return False

    if sub.get("is_trial"):
        return False

    if sub.get("state") in ("deleted", "paused"):
        return False

    if not sub.get("paxiva_client_id"):
        return False

    if sub.get("kind") not in ("vol", "unl"):
        return False

    return True


# ============================================================
# RENEWAL CONDITIONS
# ============================================================

def _check_normal_renewal(
    sub: dict,
    now: Optional[int] = None,
) -> tuple[bool, str]:

    now = now or int(time.time())

    if not _subscription_is_renewable(sub):
        return False, "این سرویس امکان تمدید ندارد."

    kind = sub.get("kind")

    if kind == "vol":

        volume_gb = _safe_float(sub.get("volume_gb"))
        used_bytes = max(0, _safe_int(sub.get("used_bytes")))

        if volume_gb <= 0:
            return False, "حجم سرویس نامعتبر است."

        total_bytes = int(volume_gb * GIB)

        remaining_bytes = max(
            0,
            total_bytes - used_bytes,
        )

        remaining_gb = remaining_bytes / GIB

        # شرط دقیق: کمتر از ۰.۲ گیگابایت
        if remaining_gb < VOLUME_RENEW_THRESHOLD_GB:
            return True, ""

        return (
            False,
            f"حجم باقی‌مانده: {format_bytes(remaining_bytes)} "
            "(تمدید عادی در کمتر از ۰.۲ گیگابایت باقی‌مانده فعال می‌شود.)"
        )

    if kind == "unl":

        expires_at = sub.get("expires_at")

        if not expires_at:
            return False, "تاریخ انقضای سرویس مشخص نیست."

        remaining_seconds = _safe_int(expires_at) - now

        if remaining_seconds <= UNLIMITED_RENEW_THRESHOLD_SECONDS:
            return True, ""

        return (
            False,
            f"زمان باقی‌مانده: {format_days_remaining(expires_at)} "
            "(تمدید عادی فقط در سه روز آخر امکان‌پذیر است.)"
        )

    return False, "نوع سرویس معتبر نیست."


# ============================================================
# KEYBOARDS
# ============================================================

def renew_list_keyboard(
    subscriptions: list[dict],
) -> InlineKeyboardMarkup:

    rows = []

    for sub in subscriptions[:MAX_RENEW_LIST]:

        sub_id = _safe_int(sub.get("id"))

        if sub_id <= 0:
            continue

        is_auto = bool(sub.get("auto_renew"))

        label = str(sub.get("config_name") or "سرویس")

        if is_auto:
            label += " (تمدید خودکار)"

        rows.append([
            _button(
                label,
                callback_data=f"renew:select:{sub_id}",
                style="success" if is_auto else "primary",
                emoji_key="world",
            )
        ])

    rows.append([
        _button(
            "بازگشت",
            callback_data="menu:main",
            style="danger",
            emoji_key="back",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_type_keyboard(
    subscription_id: int,
    has_auto_renew: bool,
    can_normal_renew: bool,
) -> InlineKeyboardMarkup:

    rows = []

    actions = []

    if can_normal_renew:
        actions.append(
            _button(
                "تمدید عادی",
                callback_data=f"renew:normal:{subscription_id}",
                style="success",
                emoji_key="true",
            )
        )

    if has_auto_renew:
        actions.append(
            _button(
                "غیرفعال کردن تمدید خودکار",
                callback_data=f"renew:auto_off:{subscription_id}",
                style="danger",
                emoji_key="false",
            )
        )
    else:
        actions.append(
            _button(
                "فعال کردن تمدید خودکار",
                callback_data=f"renew:auto:{subscription_id}",
                style="success",
                emoji_key="true",
            )
        )

    if actions:
        rows.append(actions)

    rows.append([
        _button(
            "بازگشت به لیست سرویس‌ها",
            callback_data="renew:list",
            style="danger",
            emoji_key="back",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _renew_result_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "بازگشت به سرویس‌ها",
                    callback_data="renew:list",
                    style="danger",
                    emoji_key="back",
                )
            ],
            [
                _button(
                    "منوی اصلی",
                    callback_data="menu:main",
                    style="primary",
                    emoji_key="world",
                )
            ],
        ]
    )


# ============================================================
# TEXT BUILDERS
# ============================================================

def _renew_list_text() -> str:

    return (
        f"{_premium('world')} "
        "<b>تمدید سرویس‌ها</b>\n\n"
        "سرویسی را که قصد تمدید آن را دارید انتخاب کنید.\n\n"
        f"{_premium('true')} "
        "سرویس‌هایی که دکمه سبز دارند، تمدید خودکارشان فعال است."
    )


def _subscription_text(
    sub: dict,
    product: dict,
    can_normal: bool,
    reason: str,
) -> str:

    config_name = _safe_escape(sub.get("config_name"))

    state = sub.get("state")

    state_names = {
        "active": "فعال",
        "expired": "منقضی شده",
        "depleted": "حجم تمام شده",
        "deleted": "حذف شده",
        "paused": "متوقف",
    }

    state_fa = state_names.get(
        state,
        _safe_escape(state),
    )

    kind = sub.get("kind")

    volume = _safe_float(sub.get("volume_gb"))
    used_bytes = max(0, _safe_int(sub.get("used_bytes")))

    if kind == "vol":

        total_bytes = int(volume * GIB)

        remaining = max(
            0,
            total_bytes - used_bytes,
        )

        service_type = f"{volume:g} گیگابایت"

        usage_text = (
            f"حجم کل: {format_bytes(total_bytes)}\n"
            f"حجم مصرف‌شده: {format_bytes(used_bytes)}\n"
            f"حجم باقی‌مانده: {format_bytes(remaining)}\n"
        )

    else:

        service_type = "نامحدود"

        if volume > 0:
            usage_text = (
                f"سقف مصرف: {volume:g} گیگابایت\n"
                f"حجم مصرف‌شده: {format_bytes(used_bytes)}\n"
            )
        else:
            usage_text = (
                f"حجم مصرف‌شده: {format_bytes(used_bytes)}\n"
            )

    expires_at = sub.get("expires_at")

    expiry_text = (
        format_days_remaining(expires_at)
        if expires_at
        else "بدون محدودیت زمانی"
    )

    try:
        price = _get_product_price(product)
    except ValueError:
        price = 0

    product_name = _safe_escape(
        product.get("name") or product.get("title") or "محصول متصل"
    )

    auto_status = (
        f"{_premium('true')} فعال"
        if sub.get("auto_renew")
        else f"{_premium('false')} غیرفعال"
    )

    text = (
        f"{_premium('telegram')} "
        f"<b>جزئیات سرویس {config_name}</b>\n\n"

        f"وضعیت: <b>{state_fa}</b>\n"
        f"نوع سرویس: <b>{service_type}</b>\n"
        f"زمان باقی‌مانده: <b>{expiry_text}</b>\n\n"

        f"{usage_text}\n"

        f"محصول تمدید: <b>{product_name}</b>\n"
        f"تمدید خودکار: {auto_status}\n"
        f"قیمت تمدید: <b>{price:,} تومان</b>\n"
    )

    if can_normal:
        text += (
            f"\n{_premium('true')} "
            "<b>تمدید عادی برای این سرویس فعال است.</b>"
        )
    else:
        text += (
            f"\n{_premium('warning')} "
            "<b>تمدید عادی در حال حاضر امکان‌پذیر نیست.</b>\n"
            f"{_safe_escape(reason)}"
        )

    return text


# ============================================================
# DISPLAY RENEW LIST
# ============================================================

async def _render_renew_list(callback: CallbackQuery) -> None:

    user = callback.from_user

    subscriptions = get_user_subscriptions(
        user.id,
        only_visible=True,
    )

    subscriptions = subscriptions[:MAX_RENEW_LIST]

    await ui.show(
        callback,
        _renew_list_text(),
        renew_list_keyboard(subscriptions),
    )


# ============================================================
# SHOW RENEW LIST
# ============================================================

@router.callback_query(F.data == "menu:renew")
async def show_renew_list(callback: CallbackQuery):

    user = callback.from_user

    if not user or not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        await _render_renew_list(callback)

    except Exception:
        logger.exception(
            "Could not show renew list: user=%s",
            user.id,
        )

        await callback.answer(
            "خطا در دریافت لیست سرویس‌ها.",
            show_alert=True,
        )
        return

    await callback.answer()


# ============================================================
# SELECT SUBSCRIPTION
# ============================================================

@router.callback_query(F.data.startswith("renew:select:"))
async def select_subscription(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await callback.answer()
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        sub_id = int(callback.data.split(":")[2])

    except (ValueError, IndexError, AttributeError):
        await callback.answer(
            "شناسه سرویس نامعتبر است.",
            show_alert=True,
        )
        return

    sub = get_subscription(sub_id)

    if not sub or int(sub.get("telegram_id") or 0) != user.id:
        await callback.answer(
            "سرویس یافت نشد.",
            show_alert=True,
        )
        return

    if not _subscription_is_renewable(sub):
        await callback.answer(
            "این سرویس امکان تمدید ندارد.",
            show_alert=True,
        )
        return

    product = _get_product(sub_id)

    if not product:
        await callback.answer(
            "محصول تمدید این سرویس مشخص نیست. لطفاً با پشتیبانی تماس بگیرید.",
            show_alert=True,
        )
        return

    if product.get("kind") != sub.get("kind"):
        await callback.answer(
            "نوع محصول تمدید با نوع سرویس مطابقت ندارد.",
            show_alert=True,
        )
        return

    can_normal, reason = _check_normal_renewal(sub)

    text = _subscription_text(
        sub,
        product,
        can_normal,
        reason,
    )

    keyboard = renew_type_keyboard(
        sub_id,
        bool(sub.get("auto_renew")),
        can_normal,
    )

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=keyboard,
        )

    except Exception:
        logger.exception(
            "Failed to show subscription details: sub_id=%s",
            sub_id,
        )

        await callback.answer(
            "خطا در نمایش جزئیات سرویس.",
            show_alert=True,
        )
        return

    await callback.answer()


# ============================================================
# ORDER ERROR HANDLING
# ============================================================

async def _handle_order_creation_error(
    callback: CallbackQuery,
    result: Any,
) -> None:

    if not isinstance(result, dict):
        await callback.answer(
            "پاسخ نامعتبر از سیستم پرداخت دریافت شد.",
            show_alert=True,
        )
        return

    status = result.get("status")

    if status == "insufficient_funds":

        balance = _safe_int(result.get("balance"))
        required = _safe_int(result.get("required"))

        message = "موجودی کیف پول کافی نیست."

        if required:
            message += f"\nمبلغ موردنیاز: {required:,} تومان"

        if balance:
            message += f"\nموجودی فعلی: {balance:,} تومان"

        await callback.answer(
            message,
            show_alert=True,
        )
        return

    if status in ("pending", "processing"):

        await callback.answer(
            "یک درخواست تمدید برای این سرویس در حال پردازش است.",
            show_alert=True,
        )
        return

    await callback.answer(
        "ثبت سفارش تمدید انجام نشد. لطفاً دوباره تلاش کنید.",
        show_alert=True,
    )


# ============================================================
# PAXIVA RENEWAL
# ============================================================

async def _renew_unlimited(
    paxiva: PaxivaClient,
    sub: dict,
    product: dict,
    order: dict,
) -> str:

    paxiva_id = _safe_int(sub.get("paxiva_client_id"))

    if paxiva_id <= 0:
        raise ValueError("Missing PAXIVA client ID")

    days = _get_product_days(product)

    if days <= 0:
        raise ValueError("Invalid unlimited product duration")

    order_id = _safe_int(order.get("id"))

    if order_id <= 0:
        raise ValueError("Invalid renewal order ID")

    # کلید پایدار از شناسه سفارش دیتابیس ساخته می‌شود.
    # Retryهای این سفارش باید دقیقاً همین کلید را استفاده کنند.

    idempotency_key = f"renewal-order-{order_id}"

    await paxiva.renew_volume_client(
        paxiva_id,
        days,
        idempotency_key,
    )

    return f"paxiva:{paxiva_id}:renewal:{order_id}"


async def _renew_volume(
    paxiva: PaxivaClient,
    sub: dict,
    product: dict,
    order: dict,
) -> str:

    paxiva_id = _safe_int(sub.get("paxiva_client_id"))

    if paxiva_id <= 0:
        raise ValueError("Missing PAXIVA client ID")

    purchased_volume = _safe_float(product.get("volume_gb"))

    if purchased_volume <= 0:
        raise ValueError("Invalid volume product")

    order_id = _safe_int(order.get("id"))

    if order_id <= 0:
        raise ValueError("Invalid renewal order ID")

    # ابتدا وضعیت فعلی پنل خوانده می‌شود.
    # حجم جدید به سقف قبلی اضافه می‌شود؛ مصرف کاربر ریست نمی‌شود.

    panel_data = await paxiva.get_client(paxiva_id)

    current_volume = _safe_float(
        panel_data.get("volume_gb"),
        -1,
    )

    if current_volume < 0:
        raise RuntimeError(
            "PAXIVA did not return a valid current volume"
        )

    target_volume = current_volume + purchased_volume

    if target_volume <= current_volume:
        raise RuntimeError("Invalid target volume")

    # این endpoint مقدار نهایی سقف حجم را تنظیم می‌کند.
    # اگر نتیجه درخواست نامشخص باشد، نباید سفارش را خودکار
    # دوباره اجرا کرد؛ چون endpoint فعلی Idempotency-Key ندارد.

    await paxiva.update_client_volume(
        paxiva_id,
        target_volume,
    )

    return (
        f"paxiva:{paxiva_id}:volume:"
        f"order:{order_id}:target:{target_volume:g}"
    )


# ============================================================
# EXECUTE RENEWAL ORDER
# ============================================================

async def _execute_renewal_order(
    callback: CallbackQuery,
    paxiva: PaxivaClient,
    sub: dict,
    product: dict,
    order: dict,
) -> bool:

    user = callback.from_user

    sub_id = _safe_int(sub.get("id"))
    order_id = _safe_int(order.get("id"))

    if not user or sub_id <= 0 or order_id <= 0:
        return False

    kind = _get_product_kind(product)

    # --------------------------------------------------------
    # 1. Claim order
    # --------------------------------------------------------

    try:
        claimed = mark_renewal_processing(order_id)

    except Exception as exc:
        logger.exception(
            "Could not claim renewal order: order_id=%s",
            order_id,
        )

        send_alert(
            "ERROR",
            "خطا در شروع پردازش سفارش تمدید",
            exc=exc,
            user_id=user.id,
            sub_id=sub_id,
            order_id=order_id,
            **user_context(user),
        )

        await callback.message.answer(
            "سفارش ثبت شده، اما شروع پردازش آن با مشکل مواجه شد. "
            "لطفاً دوباره درخواست تمدید ثبت نکنید."
        )

        return False

    if not claimed:

        await callback.message.answer(
            "این سفارش قبلاً وارد مرحله پردازش شده یا تکمیل شده است."
        )

        return False

    # --------------------------------------------------------
    # 2. Perform panel operation
    # --------------------------------------------------------

    phase = "panel"

    try:

        if kind == "unl":
            external_reference = await _renew_unlimited(
                paxiva,
                sub,
                product,
                order,
            )

        elif kind == "vol":
            external_reference = await _renew_volume(
                paxiva,
                sub,
                product,
                order,
            )

        else:
            raise ValueError("Unsupported renewal product kind")

    except PaxivaError as exc:

        logger.exception(
            "PAXIVA rejected renewal: order_id=%s",
            order_id,
        )

        status_code = _safe_int(
            getattr(exc, "status_code", 0)
        )

        # خطای قطعی 4xx یعنی پنل درخواست را رد کرده است.
        # خطاهای دیگر ممکن است نتیجه نامشخص داشته باشند.

        if 400 <= status_code < 500:

            try:
                fail_renewal_order(
                    order_id,
                    str(exc),
                    refund=True,
                )

                log_event(
                    "WARNING",
                    "renewal_order_rejected",
                    user.id,
                    f"order_id={order_id}; sub_id={sub_id}; status={status_code}",
                )

                await callback.message.answer(
                    "پنل درخواست تمدید را نپذیرفت. "
                    "وضعیت سفارش ثبت شد و مبلغ طبق سیستم بازپرداخت بررسی می‌شود."
                )

            except Exception as refund_exc:

                logger.exception(
                    "Renewal failure/refund processing failed: order_id=%s",
                    order_id,
                )

                send_alert(
                    "CRITICAL",
                    "خطا در ثبت شکست یا بازپرداخت سفارش تمدید",
                    exc=refund_exc,
                    user_id=user.id,
                    sub_id=sub_id,
                    order_id=order_id,
                    **user_context(user),
                )

                await callback.message.answer(
                    "تمدید انجام نشد، اما ثبت بازپرداخت با خطا مواجه شد. "
                    "پشتیبانی باید سفارش را بررسی کند."
                )

        else:

            # نتیجه درخواست قطعی نیست.
            # سفارش processing باقی می‌ماند تا reconciliation انجام شود.
            log_event(
                "CRITICAL",
                "renewal_order_uncertain",
                user.id,
                f"order_id={order_id}; sub_id={sub_id}; status={status_code}",
            )

            send_alert(
                "CRITICAL",
                "نتیجه تمدید در پنل نامشخص است",
                exc=exc,
                user_id=user.id,
                sub_id=sub_id,
                order_id=order_id,
                **user_context(user),
            )

            await callback.message.answer(
                "وضعیت عملیات پنل مشخص نیست. "
                "برای جلوگیری از تمدید تکراری، سفارش در حالت بررسی باقی می‌ماند. "
                "لطفاً دوباره تمدید نکنید."
            )

        return False

    except Exception as exc:

        logger.exception(
            "Unexpected panel renewal error: order_id=%s",
            order_id,
        )

        log_event(
            "CRITICAL",
            "renewal_order_unknown_error",
            user.id,
            f"order_id={order_id}; sub_id={sub_id}; phase={phase}",
        )

        send_alert(
            "CRITICAL",
            "خطای نامشخص هنگام اجرای تمدید در پنل",
            exc=exc,
            user_id=user.id,
            sub_id=sub_id,
            order_id=order_id,
            **user_context(user),
        )

        await callback.message.answer(
            "نتیجه عملیات تمدید مشخص نیست. "
            "سفارش برای جلوگیری از پرداخت یا تمدید تکراری محفوظ می‌ماند "
            "تا بررسی انجام شود."
        )

        return False

    # --------------------------------------------------------
    # 3. Complete order in database
    # --------------------------------------------------------

    phase = "database_completion"

    try:

        completed = complete_renewal_order(
            order_id,
            external_reference,
        )

        if not completed:
            raise RuntimeError(
                f"Database did not complete renewal order {order_id}"
            )

    except Exception as exc:

        logger.exception(
            "Panel renewal succeeded but DB completion failed: order_id=%s",
            order_id,
        )

        send_alert(
            "CRITICAL",
            "تمدید پنل موفق بود اما تکمیل سفارش دیتابیس شکست خورد",
            exc=exc,
            user_id=user.id,
            sub_id=sub_id,
            order_id=order_id,
            external_reference=external_reference,
            **user_context(user),
        )

        await callback.message.answer(
            "عملیات پنل انجام شده، اما ثبت نهایی در دیتابیس با مشکل مواجه شده است. "
            "لطفاً دوباره تمدید نکنید؛ سفارش نیازمند بررسی است."
        )

        return False

    # --------------------------------------------------------
    # 4. Reload subscription
    # --------------------------------------------------------

    updated_sub = get_subscription(sub_id) or sub

    # --------------------------------------------------------
    # 5. Purchase statistics
    # --------------------------------------------------------

    try:
        from database import increment_total_purchases

        increment_total_purchases(user.id)

    except Exception:
        logger.exception(
            "Could not update purchase counter: order_id=%s",
            order_id,
        )

    # --------------------------------------------------------
    # 6. Referral commission
    # --------------------------------------------------------

    try:
        from database import add_referral_commission

        add_referral_commission(
            user.id,
            _get_product_price(product),
            sub_id,
            "renewal",
        )

    except Exception as exc:

        logger.exception(
            "Referral commission failed: order_id=%s",
            order_id,
        )

        send_alert(
            "WARNING",
            "خطا در ثبت پورسانت تمدید",
            exc=exc,
            user_id=user.id,
            sub_id=sub_id,
            order_id=order_id,
            **user_context(user),
        )

    # --------------------------------------------------------
    # 7. Log success
    # --------------------------------------------------------

    price = _get_product_price(product)

    days = _get_product_days(product)

    log_event(
        "INFO",
        "renewal_order_completed",
        user.id,
        (
            f"order_id={order_id}; "
            f"sub_id={sub_id}; "
            f"kind={kind}; "
            f"price={price}; "
            f"days={days}"
        ),
    )

    # --------------------------------------------------------
    # 8. Success message
    # --------------------------------------------------------

    config_name = _safe_escape(
        updated_sub.get("config_name")
    )

    if kind == "vol":

        added_volume = _safe_float(
            product.get("volume_gb")
        )

        renewal_detail = (
            f"حجم اضافه‌شده: <b>{added_volume:g} گیگابایت</b>"
        )

    else:

        renewal_detail = (
            f"مدت تمدید: <b>{days} روز</b>"
        )

    success_text = (
        f"{_premium('true')} "
        "<b>تمدید سرویس با موفقیت انجام شد.</b>\n\n"

        f"سرویس: <code>{config_name}</code>\n"
        f"{renewal_detail}\n"
        f"مبلغ پرداخت‌شده: <b>{price:,} تومان</b>\n\n"

        "اطلاعات سفارش با موفقیت ثبت شد."
    )

    try:

        await callback.message.edit_text(
            text=success_text,
            reply_markup=_renew_result_keyboard(),
        )

    except Exception:

        logger.exception(
            "Renewal succeeded but Telegram UI update failed: order_id=%s",
            order_id,
        )

        try:
            await callback.message.answer(
                success_text,
                reply_markup=_renew_result_keyboard(),
            )
        except Exception:
            logger.exception(
                "Could not send renewal success message: order_id=%s",
                order_id,
            )

    # --------------------------------------------------------
    # 9. Channel notification
    # --------------------------------------------------------

    if callback.bot and kind == "unl":

        try:
            await notify_manual_renew(
                bot=callback.bot,
                user=user,
                config_name=updated_sub.get("config_name"),
                days=days,
                price=price,
            )

        except Exception:
            logger.exception(
                "Manual renewal notification failed: order_id=%s",
                order_id,
            )

    logger.info(
        "Renewal order completed: order=%s user=%s sub=%s kind=%s",
        order_id,
        user.id,
        sub_id,
        kind,
    )

    return True


# ============================================================
# NORMAL RENEW
# ============================================================

@router.callback_query(F.data.startswith("renew:normal:"))
async def normal_renew(
    callback: CallbackQuery,
    paxiva: PaxivaClient,
):

    user = callback.from_user

    if not user:
        await callback.answer()
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        sub_id = int(callback.data.split(":")[2])

    except (ValueError, IndexError, AttributeError):
        await callback.answer(
            "شناسه سرویس نامعتبر است.",
            show_alert=True,
        )
        return

    lock = _get_lock(sub_id)

    if lock.locked():
        await callback.answer(
            "درخواست تمدید قبلی هنوز در حال پردازش است.",
            show_alert=True,
        )
        return

    async with lock:

        # ----------------------------------------------------
        # 1. Validate subscription
        # ----------------------------------------------------

        sub = get_subscription(sub_id)

        if not sub or int(sub.get("telegram_id") or 0) != user.id:
            await callback.answer(
                "سرویس یافت نشد.",
                show_alert=True,
            )
            return

        if not _subscription_is_renewable(sub):
            await callback.answer(
                "این سرویس امکان تمدید ندارد.",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 2. Validate linked product
        # ----------------------------------------------------

        product = _get_product(sub_id)

        if not product:
            await callback.answer(
                "محصول تمدید این سرویس مشخص نیست.",
                show_alert=True,
            )
            return

        try:

            if _get_product_kind(product) != sub.get("kind"):
                raise ValueError("Product kind mismatch")

            price = _get_product_price(product)

            _get_product_days(product)

        except ValueError:

            logger.error(
                "Invalid renewal product: sub_id=%s product=%s",
                sub_id,
                product,
            )

            await callback.answer(
                "اطلاعات محصول تمدید معتبر نیست.",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 3. Revalidate renewal condition
        # ----------------------------------------------------

        can_normal, reason = _check_normal_renewal(sub)

        if not can_normal:
            await callback.answer(
                f"شرط تمدید برقرار نیست:\n{reason}",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 4. Validate user
        # ----------------------------------------------------

        db_user = get_user(user.id)

        if not db_user:
            await callback.answer(
                "اطلاعات حساب کاربری پیدا نشد.",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 5. Create atomic renewal order
        # ----------------------------------------------------

        idempotency_key = (
            f"manual-renew-{sub_id}-{uuid.uuid4().hex}"
        )

        try:

            order = create_renewal_order(
                sub_id,
                source="manual",
                trigger_reason="user_manual_request",
                idempotency_key=idempotency_key,
            )

        except Exception as exc:

            logger.exception(
                "Could not create renewal order: user=%s sub=%s",
                user.id,
                sub_id,
            )

            send_alert(
                "ERROR",
                "خطا در ثبت سفارش تمدید دستی",
                exc=exc,
                user_id=user.id,
                sub_id=sub_id,
                **user_context(user),
            )

            await callback.answer(
                "ثبت سفارش تمدید با خطا مواجه شد.",
                show_alert=True,
            )
            return

        if not isinstance(order, dict):
            await callback.answer(
                "پاسخ نامعتبر از سیستم سفارش دریافت شد.",
                show_alert=True,
            )
            return

        if order.get("status") != "pending":

            await _handle_order_creation_error(
                callback,
                order,
            )
            return

        await callback.answer(
            "سفارش تمدید ثبت شد. در حال پردازش..."
        )

        # ----------------------------------------------------
        # 6. Execute order
        # ----------------------------------------------------

        await _execute_renewal_order(
            callback,
            paxiva,
            sub,
            product,
            order,
        )


# ============================================================
# AUTO RENEW ON
# ============================================================

@router.callback_query(F.data.startswith("renew:auto:"))
async def enable_auto_renew(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await callback.answer()
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        sub_id = int(callback.data.split(":")[2])

    except (ValueError, IndexError, AttributeError):
        await callback.answer(
            "شناسه سرویس نامعتبر است.",
            show_alert=True,
        )
        return

    sub = get_subscription(sub_id)

    if not sub or int(sub.get("telegram_id") or 0) != user.id:
        await callback.answer(
            "سرویس یافت نشد.",
            show_alert=True,
        )
        return

    if not _subscription_is_renewable(sub):
        await callback.answer(
            "این سرویس امکان تمدید خودکار ندارد.",
            show_alert=True,
        )
        return

    product = _get_product(sub_id)

    if not product:
        await callback.answer(
            "محصول تمدید این سرویس مشخص نیست.",
            show_alert=True,
        )
        return

    if product.get("kind") != sub.get("kind"):
        await callback.answer(
            "نوع محصول با سرویس مطابقت ندارد.",
            show_alert=True,
        )
        return

    if sub.get("auto_renew"):
        await callback.answer(
            "تمدید خودکار این سرویس از قبل فعال است.",
            show_alert=True,
        )
        return

    try:

        updated = toggle_auto_renew(
            sub_id,
            True,
        )

        if not updated:
            raise RuntimeError(
                "Database rejected auto-renew activation"
            )

        log_event(
            "INFO",
            "auto_renew_enabled",
            user.id,
            f"sub_id={sub_id}",
        )

    except Exception as exc:

        logger.exception(
            "Could not enable auto renewal: sub_id=%s",
            sub_id,
        )

        send_alert(
            "ERROR",
            "خطا در فعال‌سازی تمدید خودکار",
            exc=exc,
            user_id=user.id,
            sub_id=sub_id,
            **user_context(user),
        )

        await callback.answer(
            "فعال‌سازی تمدید خودکار انجام نشد.",
            show_alert=True,
        )
        return

    await callback.answer(
        "تمدید خودکار فعال شد.",
        show_alert=True,
    )

    await _render_renew_list(callback)


# ============================================================
# AUTO RENEW OFF
# ============================================================

@router.callback_query(F.data.startswith("renew:auto_off:"))
async def disable_auto_renew(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await callback.answer()
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "لطفاً کمی صبر کنید.",
            show_alert=True,
        )
        return

    try:
        sub_id = int(callback.data.split(":")[2])

    except (ValueError, IndexError, AttributeError):
        await callback.answer(
            "شناسه سرویس نامعتبر است.",
            show_alert=True,
        )
        return

    sub = get_subscription(sub_id)

    if not sub or int(sub.get("telegram_id") or 0) != user.id:
        await callback.answer(
            "سرویس یافت نشد.",
            show_alert=True,
        )
        return

    if not sub.get("auto_renew"):
        await callback.answer(
            "تمدید خودکار این سرویس از قبل غیرفعال است.",
            show_alert=True,
        )
        return

    try:

        updated = toggle_auto_renew(
            sub_id,
            False,
        )

        if not updated:
            raise RuntimeError(
                "Database rejected auto-renew deactivation"
            )

        log_event(
            "INFO",
            "auto_renew_disabled",
            user.id,
            f"sub_id={sub_id}",
        )

    except Exception as exc:

        logger.exception(
            "Could not disable auto renewal: sub_id=%s",
            sub_id,
        )

        send_alert(
            "ERROR",
            "خطا در غیرفعال‌سازی تمدید خودکار",
            exc=exc,
            user_id=user.id,
            sub_id=sub_id,
            **user_context(user),
        )

        await callback.answer(
            "غیرفعال‌سازی تمدید خودکار انجام نشد.",
            show_alert=True,
        )
        return

    await callback.answer(
        "تمدید خودکار غیرفعال شد.",
        show_alert=True,
    )

    await _render_renew_list(callback)


# ============================================================
# BACK TO RENEW LIST
# ============================================================

@router.callback_query(F.data == "renew:list")
async def back_to_renew_list(callback: CallbackQuery):

    user = callback.from_user

    if not user:
        await callback.answer()
        return

    try:

        await _render_renew_list(callback)

    except Exception:

        logger.exception(
            "Failed to return to renew list: user=%s",
            user.id,
        )

        await callback.answer(
            "خطا در نمایش لیست سرویس‌ها.",
            show_alert=True,
        )
        return

    await callback.answer()
