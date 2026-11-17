import asyncio
import html
import logging
import time
from typing import Any, Optional
from services.notifications import notify_manual_renew
from dev.alerts import send_alert, user_context

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED

from database import (
    get_user,
    get_balance,
    deduct_balance,
    add_balance,
    get_user_subscriptions,
    get_subscription,
    update_subscription_state,
    toggle_auto_renew,
    log_event,
    increment_total_purchases,
    add_referral_commission,
)

from paxiva import PaxivaClient, PaxivaError

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

VOLUME_RENEW_THRESHOLD_GB = 0.5
UNLIMITED_RENEW_THRESHOLD_SECONDS = 3 * 86400

MAX_RENEW_LIST = 7
GIB = 1024 ** 3

# جلوگیری از درخواست هم‌زمان در یک پروسه
# هماهنگی با Scheduler در مرحله اصلاح مشترک انجام می‌شود.
_renew_locks: dict[int, asyncio.Lock] = {}


# ============================================================
# LOCK
# ============================================================

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
    except (TypeError, ValueError):
        return default


def _safe_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _premium(key: str, fallback: str = "◈") -> str:
    return premium_emoji(key, fallback)


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


# ============================================================
# RENEW KEYBOARDS
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


# ============================================================
# RENEW CONDITIONS
# ============================================================

def _check_normal_renewal(
    sub: dict,
    now: Optional[int] = None,
) -> tuple[bool, str]:

    now = now or int(time.time())

    kind = sub.get("kind")

    if kind == "vol":

        volume_gb = float(sub.get("volume_gb") or 0)
        used_bytes = _safe_int(sub.get("used_bytes"))

        if volume_gb <= 0:
            return False, "حجم سرویس نامعتبر است."

        total_bytes = int(volume_gb * GIB)

        remaining_bytes = max(
            0,
            total_bytes - used_bytes,
        )

        remaining_gb = remaining_bytes / GIB

        if remaining_gb <= VOLUME_RENEW_THRESHOLD_GB:
            return True, ""

        return (
            False,
            f"حجم باقی‌مانده: {format_bytes(remaining_bytes)} "
            f"(حداکثر مجاز: ۰.۵ گیگابایت)"
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
            f"(تمدید عادی فقط در سه روز آخر)"
        )

    return False, "نوع سرویس معتبر نیست."


def _subscription_is_renewable(sub: dict) -> bool:

    if sub.get("is_trial"):
        return False

    if sub.get("state") in ("deleted", "paused"):
        return False

    if not sub.get("paxiva_client_id"):
        return False

    return True


# ============================================================
# TEXT BUILDERS
# ============================================================

def _renew_list_text() -> str:

    return (
        f"{_premium('world')}"
        " <b>تمدید سرویس‌ها</b>\n\n"
        "سرویسی را که قصد تمدید آن را دارید انتخاب کنید.\n\n"
        f"{_premium('true')} "
        "سرویس‌هایی که دکمه سبز دارند، تمدید خودکارشان فعال است."
    )


def _subscription_text(
    sub: dict,
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

    state_fa = state_names.get(state, _safe_escape(state))

    kind = sub.get("kind")
    volume = float(sub.get("volume_gb") or 0)
    days = _safe_int(sub.get("days"), 30)

    if kind == "vol":

        total_bytes = int(volume * GIB)
        used_bytes = _safe_int(sub.get("used_bytes"))

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
        usage_text = ""

    expires_at = sub.get("expires_at")

    expiry_text = (
        format_days_remaining(expires_at)
        if expires_at
        else "نامشخص"
    )

    price = _safe_int(sub.get("price_toman"))

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
        f"مدت سرویس: <b>{days} روز</b>\n"
        f"زمان باقی‌مانده: <b>{expiry_text}</b>\n\n"

        f"{usage_text}"

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
            f"{_safe_escape(reason)}\n\n"
            "در صورت تمایل می‌توانید تمدید خودکار را فعال کنید."
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

    try:
        # از روی پیام اصلی (Rich) -> پیام جدید؛ از روی پیام Inline -> ویرایش
        await ui.show(
            callback,
            _renew_list_text(),
            renew_list_keyboard(subscriptions),
        )

    except Exception as exc:

        if "message is not modified" not in str(exc).lower():
            logger.exception(
                "Failed to render renew list: %s",
                exc,
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
            "Could not show renew list for user %s",
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

    if not sub or sub.get("telegram_id") != user.id:
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

    can_normal, reason = _check_normal_renewal(sub)

    text = _subscription_text(
        sub,
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
        # 1. Revalidate ownership and subscription
        # ----------------------------------------------------

        sub = get_subscription(sub_id)

        if not sub or sub.get("telegram_id") != user.id:
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
        # 2. Revalidate renewal condition
        # ----------------------------------------------------

        now = int(time.time())

        can_normal, reason = _check_normal_renewal(
            sub,
            now,
        )

        if not can_normal:
            await callback.answer(
                f"شرط تمدید برقرار نیست: {reason}",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 3. Validate price
        # ----------------------------------------------------

        price = _safe_int(sub.get("price_toman"))

        if price <= 0:
            await callback.answer(
                "قیمت تمدید نامعتبر است.",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 4. Validate user and balance
        # ----------------------------------------------------

        db_user = get_user(user.id)

        if not db_user:
            await callback.answer(
                "اطلاعات حساب کاربری پیدا نشد.",
                show_alert=True,
            )
            return

        balance = get_balance(user.id)

        if balance < price:
            await callback.answer(
                f"موجودی کافی نیست.\n"
                f"موجودی فعلی: {balance:,} تومان\n"
                f"مبلغ موردنیاز: {price:,} تومان",
                show_alert=True,
            )
            return

        # ----------------------------------------------------
        # 5. Validate PAXIVA client
        # ----------------------------------------------------

        paxiva_id = sub.get("paxiva_client_id")

        if not paxiva_id:
            await callback.answer(
                "شناسه سرویس در پنل معتبر نیست.",
                show_alert=True,
            )
            return

        days = _safe_int(sub.get("days"), 30)

        if days <= 0:
            await callback.answer(
                "مدت تمدید سرویس نامعتبر است.",
                show_alert=True,
            )
            return

        config_name = str(sub.get("config_name") or "")

        # ----------------------------------------------------
        # 6. Deduct balance
        # ----------------------------------------------------

        try:
            deducted = deduct_balance(
                user.id,
                price,
                "renewal",
                f"تمدید {config_name}",
            )

        except Exception as exc:
            logger.exception(
                "Wallet deduction error: user=%s sub=%s",
                user.id,
                sub_id,
            )

            log_event(
                "ERROR",
                "renew_wallet_deduction_error",
                user.id,
                f"sub_id={sub_id}",
            )

            send_alert(
                "ERROR",
                "خطای کسر کیف پول در تمدید",
                exc=exc,
                user_id=user.id,
                sub_id=sub_id,
                price=price,
                **user_context(user),
            )

            await callback.answer(
                "خطا در برداشت از کیف پول.",
                show_alert=True,
            )
            return

        if not deducted:
            await callback.answer(
                "برداشت از کیف پول انجام نشد.",
                show_alert=True,
            )
            return

        await callback.answer(
            "درخواست تمدید ثبت شد. لطفاً منتظر بمانید."
        )

        # ----------------------------------------------------
        # 7. PAXIVA operation
        # ----------------------------------------------------

        phase = "renew"
        panel_renewed = False
        traffic_reset = False

        # کلید برای Retryهای داخلی همان درخواست.
        # برای Idempotency پایدار بین چند کلیک باید در DB ذخیره شود.
        import uuid

        idem_key = f"renew-{sub_id}-{uuid.uuid4().hex}"

        try:

            # افزایش اعتبار
            await paxiva.renew_volume_client(
                paxiva_id,
                days,
                idem_key,
            )

            panel_renewed = True

            # صفر کردن مصرف
            phase = "reset"

            await paxiva.reset_client_traffic(
                paxiva_id,
            )

            traffic_reset = True

            # ------------------------------------------------
            # 8. Read authoritative state from PAXIVA
            # ------------------------------------------------

            phase = "sync"

            try:
                panel_data = await paxiva.get_client(paxiva_id)

                remote_expiry = panel_data.get("expires_at")
                remote_used = panel_data.get("used_bytes", 0)
                remote_state = panel_data.get("state", "active")

                if remote_expiry is not None:
                    remote_expiry = _safe_int(remote_expiry)

                remote_used = _safe_int(remote_used)

            except Exception:
                # عملیات تمدید و ریست موفق بوده‌اند.
                # در صورت خطای دریافت وضعیت، دیتابیس را با مقدار تقریبی
                # به‌روز می‌کنیم و چرخه Lifecycle بعداً آن را Sync می‌کند.
                logger.exception(
                    "PAXIVA renewal succeeded but state sync failed: sub_id=%s",
                    sub_id,
                )

                old_expiry = _safe_int(sub.get("expires_at"))

                remote_expiry = max(
                    int(time.time()),
                    old_expiry,
                ) + days * 86400

                remote_used = 0
                remote_state = "active"

            # ------------------------------------------------
            # 9. Update local subscription
            # ------------------------------------------------

            updated = update_subscription_state(
                sub_id,
                remote_state or "active",
                remote_expiry,
                remote_used,
            )

            if not updated:
                raise RuntimeError(
                    f"Could not update subscription state after manual renewal: {sub_id}"
                )

            # ------------------------------------------------
            # 10. Purchase statistics and referral
            # ------------------------------------------------

            try:
                increment_total_purchases(user.id)

            except Exception:
                logger.exception(
                    "Could not increment purchase counter: sub_id=%s",
                    sub_id,
                )

            try:
                add_referral_commission(
                    user.id,
                    price,
                    sub_id,
                    "renewal",
                )

            except Exception as exc:
                logger.exception(
                    "Referral commission failed: sub_id=%s",
                    sub_id,
                )
                send_alert(
                    "WARNING",
                    "خطای پورسانت رفرال در تمدید",
                    exc=exc,
                    user_id=user.id,
                    sub_id=sub_id,
                    price=price,
                    **user_context(user),
                )

            log_event(
                "INFO",
                "renew_success",
                user.id,
                f"sub_id={sub_id}; amount={price}; days={days}",
            )

            # ------------------------------------------------
            # 11. Success UI
            # ------------------------------------------------

            success_text = (
                f"{_premium('true')} "
                "<b>تمدید سرویس با موفقیت انجام شد.</b>\n\n"

                f"سرویس: <code>{_safe_escape(config_name)}</code>\n"
                f"اعتبار اضافه‌شده: <b>{days} روز</b>\n"
                f"مبلغ پرداخت‌شده: <b>{price:,} تومان</b>\n"
                f"موجودی باقی‌مانده: <b>{get_balance(user.id):,} تومان</b>\n\n"

                "اطلاعات سرویس با پنل همگام‌سازی شد."
            )

            try:
                await callback.message.edit_text(
                    text=success_text,
                    reply_markup=InlineKeyboardMarkup(
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
                    ),
                )

            except Exception:
                logger.exception(
                    "Renewal succeeded but Telegram UI update failed: sub_id=%s",
                    sub_id,
                )

            logger.info(
                "Renewal completed: user=%s sub=%s amount=%s",
                user.id,
                sub_id,
                price,
            )

            # ارسال اعلان تمدید دستی به کانال
            try:
                await notify_manual_renew(
                    bot=callback.bot,
                    user=user,
                    config_name=config_name,
                    days=days,
                    price=price,
                )
            except Exception:
                logger.exception(
                    "Manual renewal channel notification failed: sub_id=%s",
                    sub_id,
                )

        # ----------------------------------------------------
        # 12. Definite PAXIVA logical rejection
        # ----------------------------------------------------

        except PaxivaError as exc:
            logger.error(f"PAXIVA renew error: {exc}")
            send_alert(
                "ERROR",
                "خطای PAXIVA در تمدید عادی",
                exc=exc,
                user_id=user.id,
                sub_id=sub_id,
                price=price,
                status_code=exc.status_code,
                error_code=exc.error_code,
                paxiva_message=exc.message,
                **user_context(user),
            )

            logger.error(
                "PAXIVA renewal error: sub_id=%s phase=%s status=%s code=%s",
                sub_id,
                phase,
                exc.status_code,
                exc.error_code,
            )

            # اگر تمدید انجام شده ولی Reset شکست خورده،
            # سرویس در وضعیت نیمه‌کاره است و نباید Refund شود.
            if panel_renewed or traffic_reset:

                log_event(
                    "ERROR",
                    "renew_partial_failure",
                    user.id,
                    f"sub_id={sub_id}; phase={phase}; status={exc.status_code}",
                )

                await callback.message.answer(
                    "عملیات تمدید به‌صورت کامل انجام نشده است. "
                    "درخواست برای بررسی ثبت شد؛ لطفاً دوباره تمدید نکنید."
                )

                return

            # خطاهای 4xx منطقی قبل از انجام تمدید:
            # درخواست توسط پنل رد شده و می‌توان مبلغ را برگرداند.
            if 400 <= exc.status_code < 500:

                try:
                    add_balance(
                        user.id,
                        price,
                        "refund",
                        f"بازگشت وجه تمدید ناموفق {config_name}",
                        f"renew:{sub_id}",
                    )

                    log_event(
                        "WARNING",
                        "renew_refunded",
                        user.id,
                        f"sub_id={sub_id}; status={exc.status_code}",
                    )

                    await callback.message.answer(
                        "تمدید توسط پنل پذیرفته نشد. مبلغ به کیف پول شما بازگشت داده شد."
                    )

                except Exception as refund_exc:
                    logger.exception(
                        "CRITICAL: renewal refund failed: user=%s sub=%s",
                        user.id,
                        sub_id,
                    )

                    log_event(
                        "CRITICAL",
                        "renew_refund_failed",
                        user.id,
                        f"sub_id={sub_id}; amount={price}",
                    )

                    send_alert(
                        "CRITICAL",
                        "بازگشت وجه تمدید ناموفق (رد منطقی PAXIVA)",
                        exc=refund_exc,
                        user_id=user.id,
                        sub_id=sub_id,
                        price=price,
                        config_name=config_name,
                        **user_context(user),
                    )

                    await callback.message.answer(
                        "تمدید ناموفق بود و بازگشت وجه نیازمند بررسی پشتیبانی است."
                    )

            else:
                # 5xx یا خطای نامشخص:
                # ممکن است پنل عملیات را انجام داده باشد.
                # Refund خودکار در این حالت خطرناک است.
                log_event(
                    "CRITICAL",
                    "renew_unknown_paxiva_result",
                    user.id,
                    f"sub_id={sub_id}; status={exc.status_code}",
                )

                await callback.message.answer(
                    "وضعیت تمدید از پنل مشخص نیست. "
                    "برای جلوگیری از تمدید تکراری، مبلغ فعلاً محفوظ می‌ماند "
                    "تا وضعیت سرویس بررسی شود."
                )

        # ----------------------------------------------------
        # 13. Unexpected errors
        # ----------------------------------------------------

        except Exception as exc:
            logger.exception(f"Renew unexpected error: {exc}")

            logger.exception(
                "Unexpected renewal error: user=%s sub=%s phase=%s",
                user.id,
                sub_id,
                phase,
            )

            send_alert(
                "ERROR",
                "خطای غیرمنتظره در تمدید",
                exc=exc,
                user_id=user.id,
                sub_id=sub_id,
                phase=phase,
                price=price,
                panel_renewed=panel_renewed,
                traffic_reset=traffic_reset,
                **user_context(user),
            )

            # اگر عملیات پنل شروع شده یا تمدید انجام شده،
            # نتیجه ممکن است نامشخص باشد؛ Refund خودکار ممنوع.
            if panel_renewed or phase in ("renew", "reset", "sync"):

                log_event(
                    "CRITICAL",
                    "renew_unknown_result",
                    user.id,
                    f"sub_id={sub_id}; phase={phase}; error={type(exc).__name__}",
                )

                await callback.message.answer(
                    "نتیجه عملیات تمدید مشخص نیست. "
                    "برای جلوگیری از تمدید یا پرداخت تکراری، "
                    "درخواست به بررسی نیاز دارد."
                )

            else:
                # در این مرحله هنوز درخواست تمدید به پنل ارسال نشده است.
                try:
                    add_balance(
                        user.id,
                        price,
                        "refund",
                        f"بازگشت وجه تمدید ناموفق {config_name}",
                        f"renew:{sub_id}",
                    )

                    log_event(
                        "ERROR",
                        "renew_pre_panel_refund",
                        user.id,
                        f"sub_id={sub_id}",
                    )

                except Exception as refund_exc:
                    logger.exception(
                        "CRITICAL: pre-panel refund failed: sub_id=%s",
                        sub_id,
                    )

                    log_event(
                        "CRITICAL",
                        "renew_pre_panel_refund_failed",
                        user.id,
                        f"sub_id={sub_id}; amount={price}",
                    )

                    send_alert(
                        "CRITICAL",
                        "بازگشت وجه تمدید ناموفق (pre-panel)",
                        exc=refund_exc,
                        user_id=user.id,
                        sub_id=sub_id,
                        price=price,
                        config_name=config_name,
                        **user_context(user),
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

    if not sub or sub.get("telegram_id") != user.id:
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

    if sub.get("auto_renew"):
        await callback.answer(
            "تمدید خودکار این سرویس از قبل فعال است.",
            show_alert=True,
        )
        return

    try:
        toggle_auto_renew(sub_id, True)

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

    if not sub or sub.get("telegram_id") != user.id:
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
        toggle_auto_renew(sub_id, False)

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
