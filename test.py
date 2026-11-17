import html
import logging
from typing import Any, Dict, Optional

from dev.alerts import send_alert

from services.notifications import notify_test_account

from aiogram import Router, F
from aiogram.types import CallbackQuery
import ui

from database import (
    get_user,
    reserve_config_number,
    release_config_number,
    create_subscription,
    log_event,
    record_user_activity,
    get_connection,
)
from paxiva import PaxivaClient, PaxivaError
from rich import premium_emoji
from keyboards import test_account_keyboard
from utils import rate_limiter, generate_idempotency_key

logger = logging.getLogger(__name__)
router = Router(name="test")


def _send_test_alert(
    level: str,
    message: str,
    user,
    exc: Optional[BaseException] = None,
    **context,
) -> None:
    """ارسال هشدار کامل همراه با مشخصات کاربر و traceback."""

    send_alert(
        level,
        message,
        exc=exc,
        user_id=user.id if user else None,
        username=(
            f"@{user.username}"
            if user and user.username
            else None
        ),
        full_name=user.full_name if user else None,
        **context,
    )

# مشخصات اکانت تست
TEST_VOLUME_GB = 0.3
TEST_DAYS = 2
TEST_DEVICES = 2


def _emoji(key: str, fallback: str = "◈") -> str:
    """استفاده از Premium Emoji؛ بدون ایموجی معمولی."""
    try:
        return premium_emoji(key, fallback)
    except Exception:
        logger.warning(
            "Could not render premium emoji: %s",
            key,
            exc_info=True,
        )
        return fallback


def _extract_client_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """پشتیبانی از پاسخ مستقیم و پاسخ‌های دارای data."""
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("data")
    if isinstance(nested, dict):
        return nested
    return payload


def _get_client_id(payload: Dict[str, Any]) -> Optional[int]:
    data = _extract_client_data(payload)
    raw_id = data.get("id", data.get("client_id"))
    try:
        return int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return None


def _get_client_link(payload: Dict[str, Any]) -> Optional[str]:
    data = _extract_client_data(payload)
    for key in ("link", "subscription_url", "subscription_link", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _claim_trial(telegram_id: int) -> bool:
    """
    رزرو اتمیک حق دریافت تست.
    فقط یکی از درخواست‌های هم‌زمان می‌تواند trial_used را از صفر به یک تغییر دهد.
    """
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE users
            SET trial_used = 1
            WHERE telegram_id = ? AND trial_used = 0
            """,
            (telegram_id,),
        )
        return cursor.rowcount == 1


def _restore_trial_claim(telegram_id: int) -> None:
    """آزادسازی حق تست پس از شکست قطعی عملیات."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET trial_used = 0 WHERE telegram_id = ?",
            (telegram_id,),
        )


async def _delete_remote_client(
    paxiva: PaxivaClient,
    client_id: Optional[int],
    user,
) -> bool:
    """پاک‌سازی اکانت پنل در صورت شکست ذخیره‌سازی محلی."""

    if client_id is None:
        return False

    try:
        await paxiva.delete_client(client_id, purge=True)

        logger.info(
            "Removed orphan PAXIVA trial client %s for user %s",
            client_id,
            user.id,
        )
        return True

    except Exception as exc:
        logger.warning(
            "Could not remove orphan PAXIVA trial client %s for user %s",
            client_id,
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "CRITICAL",
            "حذف اکانت تست یتیم از پنل PAXIVA ناموفق بود",
            user,
            exc=exc,
            operation="delete_remote_trial_client",
            client_id=client_id,
            cleanup_status="failed",
        )

        return False


def _page_text(already_used: bool) -> str:
    gift = _emoji("flower_gift")
    false = _emoji("false")
    warning = _emoji("warning")

    if already_used:
        return (
            f"{gift} <b>اکانت تست</b>\n\n"
            f"{false} شما قبلاً اکانت تست خود را دریافت کرده‌اید.\n"
            "هر کاربر فقط یک بار می‌تواند از این امکان استفاده کند."
        )

    return (
        f"{gift} <b>اکانت تست رایگان</b>\n\n"
        f"با دریافت اکانت تست، امکانات زیر در اختیار شما قرار می‌گیرد:\n\n"
        f"حجم: <b>{TEST_VOLUME_GB} گیگابایت</b>\n"
        f"مدت: <b>{TEST_DAYS} روز</b>\n"
        f"دستگاه هم‌زمان: <b>{TEST_DEVICES}</b>\n\n"
        f"{warning} هر کاربر فقط یک بار می‌تواند اکانت تست دریافت کند."
    )


# ============================================================
# صفحه اکانت تست
# ============================================================

@router.callback_query(F.data == "menu:test")
async def show_test_account(callback: CallbackQuery):
    user = callback.from_user
    if not user:
        await callback.answer()
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer("لطفاً کمی صبر کنید.", show_alert=True)
        return

    # پاسخ سریع به Callback تا منقضی نشود.
    await callback.answer()

    try:
        record_user_activity(user.id)
        db_user = get_user(user.id)

        if not db_user:
            if callback.message:
                await callback.message.answer("اطلاعات کاربر پیدا نشد. ابتدا /start را اجرا کنید.")
            return

        already_used = int(db_user.get("trial_used") or 0) == 1
        # پیام جدید با Inline Keyboard؛ پیام اصلی (Rich) ادیت نمی‌شود.
        await ui.show(
            callback,
            _page_text(already_used),
            test_account_keyboard(already_used=already_used),
        )

    except Exception as exc:
        logger.warning(
            "Failed to show test account page for user %s",
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "ERROR",
            "خطا در نمایش صفحه اکانت تست",
            user,
            exc=exc,
            operation="show_test_account",
        )

        if callback.message:
            try:
                await callback.message.answer(
                    "نمایش صفحه اکانت تست با خطا مواجه شد. دوباره تلاش کنید."
                )
            except Exception as send_exc:
                logger.warning(
                    "Could not send test page error message to user %s",
                    user.id,
                    exc_info=True,
                )

                _send_test_alert(
                    "ERROR",
                    "ارسال پیام خطا به کاربر ناموفق بود",
                    user,
                    exc=send_exc,
                    operation="send_test_page_error_message",
                )


# ============================================================
# دریافت اکانت تست
# ============================================================

@router.callback_query(F.data == "test:get")
async def get_test_account(callback: CallbackQuery, paxiva: PaxivaClient):
    user = callback.from_user
    if not user:
        await callback.answer()
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer("لطفاً کمی صبر کنید.", show_alert=True)
        return

    try:
        db_user = get_user(user.id)

        if not db_user:
            await callback.answer(
                "ابتدا /start را اجرا کنید.",
                show_alert=True,
            )
            return

    except Exception as exc:
        logger.warning(
            "Could not retrieve user data before test creation: %s",
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "ERROR",
            "خطا در دریافت اطلاعات کاربر پیش از ساخت اکانت تست",
            user,
            exc=exc,
            operation="get_user_before_test_creation",
        )

        await callback.answer(
            "در حال حاضر امکان دریافت اکانت تست وجود ندارد.",
            show_alert=True,
        )
        return

    # ثبت اتمیک؛ درخواست‌های هم‌زمان فقط یک برنده خواهند داشت.
    try:
        claimed = _claim_trial(user.id)
    except Exception as exc:
        logger.warning(
            "Could not claim trial for user %s",
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "ERROR",
            "خطا در رزرو اتمیک حق دریافت اکانت تست",
            user,
            exc=exc,
            operation="claim_trial",
        )

        await callback.answer(
            "در حال حاضر امکان ثبت درخواست وجود ندارد.",
            show_alert=True,
        )
        return

    if not claimed:
        await callback.answer("شما قبلاً اکانت تست دریافت کرده‌اید.", show_alert=True)
        return

    # از این نقطه Callback را سریع پاسخ می‌دهیم؛ عملیات پنل ممکن است زمان‌بر باشد.
    try:
        await callback.answer("در حال ساخت اکانت تست...")

    except Exception as exc:
        logger.warning(
            "Could not acknowledge test creation callback for user %s",
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "ERROR",
            "پاسخ‌دادن به Callback دریافت اکانت تست ناموفق بود",
            user,
            exc=exc,
            operation="acknowledge_test_creation",
        )

        try:
            _restore_trial_claim(user.id)
        except Exception as restore_exc:
            logger.warning(
                "Could not restore trial claim after callback failure for user %s",
                user.id,
                exc_info=True,
            )

            _send_test_alert(
                "CRITICAL",
                "بازگردانی حق دریافت تست پس از خطای Callback ناموفق بود",
                user,
                exc=restore_exc,
                operation="restore_trial_claim_after_callback_failure",
            )

        return

    config_number = None
    config_name = None
    paxiva_client_id = None
    subscription_created = False
    remote_client_created = False
    keep_trial_claim = False

    try:
        config_number = reserve_config_number(is_test=True)
        config_name = f"iManTest-{config_number}"

        create_idem_key = generate_idempotency_key(
            f"trial-create-{user.id}-{config_number}"
        )

        # ساخت اکانت تست با مشخصات اختصاصی
        create_response = await paxiva.create_client(
            name=config_name,
            volume_gb=TEST_VOLUME_GB,
            days=TEST_DAYS,
            devices=TEST_DEVICES,
            idempotency_key=create_idem_key,
            kind="vol",
        )

        paxiva_client_id = _get_client_id(create_response)

        if paxiva_client_id is None:
            # ممکن است پنل اکانت را ساخته باشد اما پاسخ، شناسه قابل استفاده نداشته باشد.
            # در این حالت حق تست را آزاد نمی‌کنیم تا اکانت تکراری ساخته نشود.
            keep_trial_claim = True
            raise RuntimeError(
                "PAXIVA create_client response did not contain a valid client id"
            )

        remote_client_created = True

        # لینک ممکن است در پاسخ ساخت نباشد؛ در این صورت اطلاعات کلاینت را می‌خوانیم.
        link = _get_client_link(create_response)
        if not link:
            client_response = await paxiva.get_client(paxiva_client_id)
            link = _get_client_link(client_response)

        if not link:
            raise RuntimeError("PAXIVA client response did not contain a subscription link")

        # ثبت اشتراک در دیتابیس
        sub_id = create_subscription(
            telegram_id=user.id,
            config_number=config_number,
            config_name=config_name,
            kind="vol",
            volume_gb=TEST_VOLUME_GB,
            days=TEST_DAYS,
            devices=TEST_DEVICES,
            price_toman=0,
            price_stars=0,
            is_trial=1,
            paxiva_client_id=paxiva_client_id,
            link=link,
        )

        if not sub_id:
            raise RuntimeError("Database did not return a subscription id")

        subscription_created = True

        safe_name = html.escape(config_name)
        safe_link = html.escape(link, quote=True)

        success_text = (
            f"{_emoji('true')} <b>اکانت تست شما آماده شد!</b>\n\n"
            f"نام کاربری: <code>{safe_name}</code>\n"
            f"حجم: <b>{TEST_VOLUME_GB} گیگابایت</b>\n"
            f"مدت اعلام‌شده: <b>{TEST_DAYS} روز</b>\n"
            f"دستگاه هم‌زمان: <b>{TEST_DEVICES}</b>\n\n"
            f"لینک اشتراک:\n<code>{safe_link}</code>\n\n"
            f"{_emoji('safe')} لینک اشتراک خود را در اختیار دیگران قرار ندهید."
        )

        if callback.message:
            await callback.message.edit_text(
                success_text,
                reply_markup=test_account_keyboard(already_used=True),
            )

        log_event(
            "INFO",
            "test_account_created",
            user.id,
            f"sub_id={sub_id}, config={config_name}, paxiva_id={paxiva_client_id}",
        )

        # ارسال اعلان دریافت اکانت تست به کانال
        try:
            await notify_test_account(
                bot=callback.bot,
                user=user,
                config_name=config_name,
                volume_gb=TEST_VOLUME_GB,
                days=TEST_DAYS,
                devices=TEST_DEVICES,
            )

        except Exception as exc:
            logger.warning(
                "Test account channel notification failed for user %s",
                user.id,
                exc_info=True,
            )

            _send_test_alert(
                "ERROR",
                "ارسال اعلان ساخت اکانت تست به کانال ناموفق بود",
                user,
                exc=exc,
                operation="notify_test_account_channel",
                config_name=config_name,
                config_number=config_number,
                paxiva_client_id=paxiva_client_id,
                subscription_id=sub_id,
                subscription_created=subscription_created,
            )

    except PaxivaError as exc:
        logger.warning(
            "PAXIVA trial error for user %s",
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "ERROR",
            "خطای PAXIVA هنگام ساخت اکانت تست",
            user,
            exc=exc,
            operation="create_test_account",
            config_number=config_number,
            config_name=config_name,
            paxiva_client_id=paxiva_client_id,
            remote_client_created=remote_client_created,
            subscription_created=subscription_created,
            status_code=exc.status_code,
            error_code=exc.error_code,
            paxiva_message=exc.message,
        )

        try:
            log_event(
                "ERROR",
                "test_paxiva_failed",
                user.id,
                (
                    f"status={exc.status_code}, "
                    f"code={exc.error_code}, "
                    f"message={exc.message}"
                ),
            )
        except Exception as log_exc:
            logger.warning(
                "Could not save PAXIVA failure event for user %s",
                user.id,
                exc_info=True,
            )

            _send_test_alert(
                "ERROR",
                "ثبت رویداد خطای PAXIVA در دیتابیس ناموفق بود",
                user,
                exc=log_exc,
                operation="log_paxiva_failure",
                config_number=config_number,
                paxiva_client_id=paxiva_client_id,
            )

        if remote_client_created and not subscription_created:
            deleted = await _delete_remote_client(
                paxiva,
                paxiva_client_id,
                user,
            )

            if not deleted:
                keep_trial_claim = True

        if config_number is not None and not subscription_created:
            try:
                release_config_number(config_number, is_test=True)
            except Exception as release_exc:
                logger.warning(
                    "Could not release test config number %s",
                    config_number,
                    exc_info=True,
                )

                _send_test_alert(
                    "ERROR",
                    "آزادسازی شماره اکانت تست در دیتابیس ناموفق بود",
                    user,
                    exc=release_exc,
                    operation="release_test_config_number",
                    config_number=config_number,
                )

        if not keep_trial_claim:
            try:
                _restore_trial_claim(user.id)
            except Exception as restore_exc:
                logger.warning(
                    "Could not restore trial claim for user %s",
                    user.id,
                    exc_info=True,
                )

                _send_test_alert(
                    "CRITICAL",
                    "بازگردانی وضعیت دریافت تست کاربر ناموفق بود",
                    user,
                    exc=restore_exc,
                    operation="restore_trial_claim",
                    config_number=config_number,
                    paxiva_client_id=paxiva_client_id,
                )

        if callback.message:
            try:
                await callback.message.edit_text(
                    "ساخت اکانت تست از سمت پنل با خطا مواجه شد. "
                    "در صورت کسر یا ثبت اکانت، با پشتیبانی تماس بگیرید."
                )
            except Exception as message_exc:
                logger.warning(
                    "Could not notify user %s about PAXIVA error",
                    user.id,
                    exc_info=True,
                )

                _send_test_alert(
                    "ERROR",
                    "ارسال پیام خطای PAXIVA به کاربر ناموفق بود",
                    user,
                    exc=message_exc,
                    operation="notify_user_paxiva_error",
                    config_number=config_number,
                    paxiva_client_id=paxiva_client_id,
                )

    except Exception as exc:
        logger.warning(
            "Unexpected trial creation error for user %s",
            user.id,
            exc_info=True,
        )

        _send_test_alert(
            "CRITICAL",
            "خطای غیرمنتظره هنگام ساخت اکانت تست",
            user,
            exc=exc,
            operation="unexpected_test_creation_error",
            config_number=config_number,
            config_name=config_name,
            paxiva_client_id=paxiva_client_id,
            remote_client_created=remote_client_created,
            subscription_created=subscription_created,
            keep_trial_claim=keep_trial_claim,
        )

        try:
            log_event(
                "ERROR",
                "test_unexpected",
                user.id,
                f"error={str(exc)[:800]}",
            )
        except Exception as log_exc:
            logger.warning(
                "Could not save unexpected test error event for user %s",
                user.id,
                exc_info=True,
            )

            _send_test_alert(
                "ERROR",
                "ثبت رویداد خطای غیرمنتظره در دیتابیس ناموفق بود",
                user,
                exc=log_exc,
                operation="log_unexpected_test_error",
                config_number=config_number,
                paxiva_client_id=paxiva_client_id,
            )

        if remote_client_created and not subscription_created:
            deleted = await _delete_remote_client(
                paxiva,
                paxiva_client_id,
                user,
            )

            if not deleted:
                # اگر حذف اکانت پنل ناموفق باشد، اجازه ساخت تست دوم نمی‌دهیم.
                keep_trial_claim = True

        if config_number is not None and not subscription_created:
            try:
                release_config_number(config_number, is_test=True)
            except Exception as release_exc:
                logger.warning(
                    "Could not release test config number %s",
                    config_number,
                    exc_info=True,
                )

                _send_test_alert(
                    "ERROR",
                    "آزادسازی شماره اکانت تست پس از خطا ناموفق بود",
                    user,
                    exc=release_exc,
                    operation="release_test_config_number",
                    config_number=config_number,
                )

        if not keep_trial_claim:
            try:
                _restore_trial_claim(user.id)
            except Exception as restore_exc:
                logger.warning(
                    "Could not restore trial claim for user %s",
                    user.id,
                    exc_info=True,
                )

                _send_test_alert(
                    "CRITICAL",
                    "بازگردانی وضعیت دریافت تست کاربر پس از خطا ناموفق بود",
                    user,
                    exc=restore_exc,
                    operation="restore_trial_claim",
                    config_number=config_number,
                    paxiva_client_id=paxiva_client_id,
                )

        if callback.message:
            try:
                await callback.message.edit_text(
                    "در ساخت اکانت تست خطایی رخ داد. "
                    "اگر مشکل ادامه داشت، با پشتیبانی تماس بگیرید."
                )
            except Exception as message_exc:
                logger.warning(
                    "Could not notify user %s about unexpected error",
                    user.id,
                    exc_info=True,
                )

                _send_test_alert(
                    "ERROR",
                    "ارسال پیام خطای ساخت اکانت تست به کاربر ناموفق بود",
                    user,
                    exc=message_exc,
                    operation="notify_user_unexpected_test_error",
                    config_number=config_number,
                    paxiva_client_id=paxiva_client_id,
                )

    # در صورت موفقیت، trial_used عمداً روی ۱ باقی می‌ماند.
