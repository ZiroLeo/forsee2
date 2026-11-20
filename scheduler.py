import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from dev.alerts import send_alert
from database import (
    get_connection,
    get_user,
    deduct_balance,
    add_balance,
    update_subscription_state,
    toggle_auto_renew,
    get_setting,
    set_setting,
    log_event,
)
from pasar_guard import (
    PanelRegistry,
    PasarGuardError,
    is_definite_rejection,
    renew_days,
    reset_usage,
)
from utils import generate_idempotency_key, format_bytes
from services.lifecycle import run_lifecycle_cycle
from rich import premium_emoji

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    _TZ = timezone(timedelta(hours=3, minutes=30))

logger = logging.getLogger(__name__)


# ============================================================
# تنظیمات
# ============================================================

AUTO_RENEW_VOL_THRESHOLD_GB = 0.5
AUTO_RENEW_UNL_DAYS = 3

PANEL_REQUEST_DELAY = 0.5

HOURLY_INTERVAL = 3600
DAILY_INTERVAL = 86400

REPORT_HOUR = 23
REPORT_MINUTE = 55

BYTES_PER_GB = 1024 ** 3
NOTIFY_COOLDOWN = 86400  # حداکثر یک پیام «موجودی کافی نیست» در هر ۲۴ ساعت برای هر سرویس

_scheduler_lock = asyncio.Lock()


# ============================================================
# ابزارهای کمکی
# ============================================================

def _premium(key: str, fallback: str) -> str:
    try:
        return premium_emoji(key, fallback)
    except Exception:
        return fallback


def _now_tehran() -> datetime:
    return datetime.now(_TZ)


def _day_start() -> int:
    """شروع «امروز» به وقت تهران (مستقل از ساعت سرور)."""
    n = _now_tehran()
    return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _get_new_expiry(old_expiry: int | None, days: int) -> int:
    """اگر سرویس منقضی شده، تمدید از «الان» حساب می‌شود، نه از تاریخ گذشته."""
    now = int(time.time())
    try:
        base = max(int(old_expiry or 0), now)
    except (TypeError, ValueError):
        base = now
    return base + (days * 86400)


def _disable_auto_renew(sub_id: int, reason: str) -> None:
    """خاموش‌کردن تمدید خودکار تا از کسر مجدد/تکرار بی‌پایان جلوگیری شود."""
    try:
        toggle_auto_renew(sub_id, False)
        log_event("WARNING", "auto_renew_disabled", 0, f"sub_id={sub_id}, reason={reason}")
    except Exception:
        logger.exception("Could not disable auto-renew. sub_id=%s", sub_id)
        send_alert("CRITICAL", "خاموش‌کردن تمدید خودکار ناموفق؛ خطر کسر مجدد!", sub_id=sub_id, reason=reason)


def _notify_allowed(kind: str, sub_id: int) -> bool:
    """throttle: هر نوع پیام برای هر سرویس حداکثر یک‌بار در ۲۴ ساعت."""
    key = f"autorenew_notified:{kind}:{sub_id}"
    try:
        last = float(get_setting(key, "0") or 0)
        if time.time() - last < NOTIFY_COOLDOWN:
            return False
        set_setting(key, str(int(time.time())))
    except Exception:
        logger.exception("Notify throttle failed")
    return True


def _is_definite_panel_rejection(error: PasarGuardError) -> bool:
    # رد قطعی = 4xx (جز 408/425/429) یا circuit-open (درخواست اصلاً ارسال نشده).
    # timeout / قطع شبکه / 5xx / status=None مبهم است و بازگشت وجه ندارد.
    return is_definite_rejection(error)


def _load_auto_renew_subscriptions(kind: str, threshold: int | None = None):
    with get_connection() as conn:
        if kind == "vol":
            rows = conn.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE auto_renew = 1
                  AND kind = 'vol'
                  AND is_trial = 0
                  AND state IN ('active', 'depleted')
                  AND deleted_from_list_at IS NULL
                ORDER BY id ASC
                """
            ).fetchall()
        elif kind == "unl":
            rows = conn.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE auto_renew = 1
                  AND kind = 'unl'
                  AND is_trial = 0
                  AND state IN ('active', 'expired')
                  AND deleted_from_list_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                ORDER BY expires_at ASC, id ASC
                """,
                (threshold,)
            ).fetchall()
        else:
            raise ValueError(f"Unsupported subscription kind: {kind}")
        return [dict(row) for row in rows]


async def _notify_user(bot, telegram_id: int, message: str):
    if not bot:
        return
    try:
        await bot.send_message(telegram_id, message)
    except Exception:
        logger.exception("Failed to notify user. telegram_id=%s", telegram_id)


def _refund_user(telegram_id: int, amount: int, description: str, sub_id: int, idem_key: str = "") -> bool:
    try:
        # reference با پیشوند refund: → بازگشت وجه حداکثر یک‌بار ثبت می‌شود
        ref = f"refund:autorenew:{idem_key or sub_id}"
        if not add_balance(telegram_id, amount, "auto_renew_refund", description, ref):
            raise RuntimeError("refund credit rejected")
        log_event("WARNING", "auto_renew_refund", telegram_id, f"sub_id={sub_id}, amount={amount}")
        return True
    except Exception:
        logger.exception("CRITICAL: Refund failed. telegram_id=%s sub_id=%s", telegram_id, sub_id)
        log_event("CRITICAL", "auto_renew_refund_failed", telegram_id, f"sub_id={sub_id}, amount={amount}")
        send_alert("CRITICAL", "بازگشت وجه تمدید خودکار ناموفق", user_id=telegram_id,
                   sub_id=sub_id, amount=amount)
        return False


# ============================================================
# اجرای تمدید در پنل
# ============================================================

async def _renew_panel_subscription(panel_registry: PanelRegistry, sub: dict, idem_key: str) -> int:
    if not sub.get("panel_id") or not sub.get("config_name"):
        raise ValueError("Subscription has no panel_id/config_name")

    days = int(sub.get("days") or 30)
    if days <= 0:
        raise ValueError("Invalid subscription duration")

    old_expiry = sub.get("expires_at")

    renewed = await renew_days(sub, days, idem_key)

    if sub.get("kind") == "vol":
        try:
            await reset_usage(sub)
        except Exception as exc:
            # روزها در پنل اضافه شده؛ نباید «رد قطعی» تلقی و پول برگردانده شود
            raise RuntimeError("renew applied but traffic reset failed") from exc

    # انقضای واقعی پنل مرجع است؛ فقط اگر در دسترس نبود محاسبهٔ محلی استفاده می‌شود
    try:
        panel_expiry = int((renewed or {}).get("expires_at") or 0)
    except (TypeError, ValueError):
        panel_expiry = 0
    return panel_expiry if panel_expiry > 0 else _get_new_expiry(old_expiry, days)


# ============================================================
# پردازش یک سرویس برای تمدید خودکار
# ============================================================

async def _process_auto_renew(panel_registry: PanelRegistry, sub: dict, bot=None) -> str:
    sub_id = int(sub["id"])
    telegram_id = int(sub["telegram_id"])
    config_name = sub.get("config_name") or f"Subscription-{sub_id}"
    price = int(sub.get("price_toman") or 0)
    days = int(sub.get("days") or 30)

    if not sub.get("panel_id") or not sub.get("config_name"):
        logger.error("Auto-renew skipped: missing panel_id/config_name. sub_id=%s", sub_id)
        return "skipped"

    if price <= 0 or days <= 0:
        logger.error("Auto-renew skipped: invalid price or days. sub_id=%s", sub_id)
        return "skipped"

    if sub.get("kind") not in ("vol", "unl"):
        logger.warning("Auto-renew skipped: unsupported kind. sub_id=%s kind=%s", sub_id, sub.get("kind"))
        return "skipped"

    user = get_user(telegram_id)
    if not user:
        logger.warning("Auto-renew skipped: user not found. sub_id=%s", sub_id)
        return "skipped"

    if int(user.get("balance") or 0) < price:
        logger.info("Auto-renew insufficient balance. sub_id=%s", sub_id)
        if _notify_allowed("insufficient", sub_id):
            await _notify_user(
                bot, telegram_id,
                (
                    f"{_premium('warning', '⚠️')} <b>موجودی کیف پول کافی نیست</b>\n\n"
                    f"سرویس: <code>{config_name}</code>\n"
                    f"مبلغ موردنیاز: <b>{price:,} تومان</b>\n\n"
                    "برای تمدید خودکار، ابتدا کیف پول خود را شارژ کنید."
                )
            )
        return "insufficient"

    deducted = False
    panel_renewed = False
    # کلید پایدار در همان روز/همان تاریخ انقضا → تکرار ناخواسته در پنل دوباره اعمال نمی‌شود
    idem_key = f"autorenew-{sub_id}-{int(sub.get('expires_at') or 0)}-{_day_start()}"

    try:
        deducted = deduct_balance(
            telegram_id, price, "auto_renew",
            f"تمدید خودکار {config_name}", f"autorenew:{idem_key}"
        )
        if not deducted:
            logger.warning("Auto-renew deduction rejected. sub_id=%s", sub_id)
            return "failed"

        new_expiry = await _renew_panel_subscription(panel_registry, sub, idem_key)
        panel_renewed = True

        update_subscription_state(
            sub_id, "active", new_expiry,
            0 if sub.get("kind") == "vol" else sub.get("used_bytes", 0)
        )

        log_event(
            "INFO", "auto_renew_success", telegram_id,
            f"sub_id={sub_id}, kind={sub.get('kind')}, amount={price}, days={days}"
        )
        logger.info("Auto-renew successful. sub_id=%s kind=%s", sub_id, sub.get("kind"))

        await _notify_user(
            bot, telegram_id,
            (
                f"{_premium('true', '✅')} <b>تمدید خودکار با موفقیت انجام شد</b>\n\n"
                f"سرویس: <code>{config_name}</code>\n"
                f"مدت تمدید: <b>{days} روز</b>\n"
                f"مبلغ پرداخت‌شده: <b>{price:,} تومان</b>\n\n"
                "سرویس شما با موفقیت تمدید شد."
            )
        )
        return "renewed"

    except PasarGuardError as error:
        logger.error("PasarGuard error during auto-renew. sub_id=%s status=%s error=%s", sub_id, getattr(error, "status", None), error)
        send_alert("ERROR", "خطای PasarGuard در تمدید خودکار", exc=error, user_id=telegram_id, sub_id=sub_id,
                   status=getattr(error, "status", None))

        if deducted and not panel_renewed:
            if _is_definite_panel_rejection(error):
                refunded = _refund_user(
                    telegram_id, price,
                    f"بازگشت وجه تمدید ناموفق {config_name}", sub_id, idem_key
                )
                _disable_auto_renew(sub_id, "panel_rejected")
                if refunded:
                    await _notify_user(
                        bot, telegram_id,
                        (
                            f"{_premium('warning', '⚠️')} <b>تمدید انجام نشد</b>\n\n"
                            f"سرویس: <code>{config_name}</code>\n"
                            "درخواست توسط پنل رد شد و مبلغ به کیف پول شما بازگشت داده شد.\n"
                            "تمدید خودکار این سرویس خاموش شد؛ پس از بررسی می‌توانید دوباره فعالش کنید."
                        )
                    )
            else:
                logger.critical(
                    "Ambiguous PasarGuard failure. Manual reconciliation required. sub_id=%s idem_key=%s",
                    sub_id, idem_key
                )
                log_event("CRITICAL", "auto_renew_uncertain", telegram_id, f"sub_id={sub_id}, idem_key={idem_key}")
                _disable_auto_renew(sub_id, "uncertain_panel_result")
                send_alert("CRITICAL", "خطای PasarGuard مبهم در تمدید خودکار؛ تمدید خودکار خاموش شد، بررسی دستی لازم است",
                           exc=error, user_id=telegram_id, sub_id=sub_id, idem_key=idem_key, amount=price)
                await _notify_user(
                    bot, telegram_id,
                    (
                        f"{_premium('warning', '⚠️')} <b>وضعیت تمدید نیاز به بررسی دارد</b>\n\n"
                        f"سرویس: <code>{config_name}</code>\n"
                        "درخواست تمدید به پنل ارسال شده، اما نتیجه آن قطعی نیست. "
                        "لطفاً تا بررسی وضعیت، درخواست تمدید دیگری ثبت نکنید."
                    )
                )
                return "uncertain"

        log_event(
            "ERROR", "auto_renew_panel_failed", telegram_id,
            f"sub_id={sub_id}, status={getattr(error, 'status', None)}"
        )
        return "failed"

    except Exception as error:
        logger.exception("Unexpected auto-renew error. sub_id=%s", sub_id)
        send_alert("ERROR", "خطای غیرمنتظره در تمدید خودکار", exc=error, user_id=telegram_id, sub_id=sub_id)

        if deducted:
            if panel_renewed:
                logger.critical(
                    "PANEL RENEWED BUT DATABASE UPDATE FAILED. Manual reconciliation required. sub_id=%s",
                    sub_id
                )
                log_event("CRITICAL", "auto_renew_db_sync_failed", telegram_id, f"sub_id={sub_id}")
                _disable_auto_renew(sub_id, "db_sync_failed")
                send_alert("CRITICAL", "تمدید در پنل انجام شد اما ذخیره در دیتابیس ناموفق؛ تمدید خودکار خاموش شد",
                           exc=error, user_id=telegram_id, sub_id=sub_id, amount=price)
                await _notify_user(
                    bot, telegram_id,
                    (
                        f"{_premium('warning', '⚠️')} <b>تمدید در حال بررسی است</b>\n\n"
                        f"سرویس: <code>{config_name}</code>\n"
                        "عملیات پنل انجام شده، اما ثبت نهایی اطلاعات با مشکل مواجه شده است."
                    )
                )
                return "uncertain"

            logger.critical(
                "Unknown auto-renew outcome. Manual reconciliation required. sub_id=%s idem_key=%s",
                sub_id, idem_key
            )
            log_event("CRITICAL", "auto_renew_unknown_outcome", telegram_id, f"sub_id={sub_id}, idem_key={idem_key}")
            _disable_auto_renew(sub_id, "unknown_outcome")
            send_alert("CRITICAL", "نتیجه تمدید خودکار نامشخص؛ تمدید خودکار خاموش شد", exc=error,
                       user_id=telegram_id, sub_id=sub_id, idem_key=idem_key, amount=price)
            await _notify_user(
                bot, telegram_id,
                (
                    f"{_premium('warning', '⚠️')} <b>وضعیت تمدید نامشخص است</b>\n\n"
                    f"سرویس: <code>{config_name}</code>\n"
                    "برای جلوگیری از ثبت تمدید تکراری، وضعیت درخواست باید بررسی شود."
                )
            )
            return "uncertain"

        log_event(
            "ERROR", "auto_renew_unexpected", telegram_id,
            f"sub_id={sub_id}, error_type={type(error).__name__}"
        )
        return "failed"


# ============================================================
# تمدید خودکار سرویس‌های حجمی
# ============================================================

async def auto_renew_volume(panel_registry: PanelRegistry, bot=None) -> dict:
    stats = {
        "checked": 0, "renewed": 0, "failed": 0,
        "insufficient": 0, "skipped": 0, "uncertain": 0,
    }

    async with _scheduler_lock:
        logger.info("Starting volume auto-renew cycle.")
        subscriptions = _load_auto_renew_subscriptions("vol")

        for sub in subscriptions:
            stats["checked"] += 1
            sub_id = sub["id"]
            volume_gb = float(sub.get("volume_gb") or 0)
            used_bytes = int(sub.get("used_bytes") or 0)

            if volume_gb <= 0:
                stats["skipped"] += 1
                continue

            total_bytes = int(volume_gb * BYTES_PER_GB)
            remaining_bytes = max(0, total_bytes - used_bytes)
            remaining_gb = remaining_bytes / BYTES_PER_GB

            if remaining_gb > AUTO_RENEW_VOL_THRESHOLD_GB:
                continue

            logger.info("Volume threshold reached. sub_id=%s remaining=%.3f GB", sub_id, remaining_gb)
            result = await _process_auto_renew(panel_registry, sub, bot)

            if result in stats:
                stats[result] += 1

            await asyncio.sleep(PANEL_REQUEST_DELAY)

        logger.info("Volume auto-renew completed: %s", stats)

    return stats


# ============================================================
# تمدید خودکار سرویس‌های نامحدود
# ============================================================

async def auto_renew_unlimited(panel_registry: PanelRegistry, bot=None) -> dict:
    stats = {
        "checked": 0, "renewed": 0, "failed": 0,
        "insufficient": 0, "skipped": 0, "uncertain": 0,
    }

    now = int(time.time())
    threshold = now + (AUTO_RENEW_UNL_DAYS * 86400)

    async with _scheduler_lock:
        logger.info("Starting unlimited auto-renew cycle.")
        subscriptions = _load_auto_renew_subscriptions("unl", threshold)

        for sub in subscriptions:
            stats["checked"] += 1
            result = await _process_auto_renew(panel_registry, sub, bot)
            if result in stats:
                stats[result] += 1
            await asyncio.sleep(PANEL_REQUEST_DELAY)

        logger.info("Unlimited auto-renew completed: %s", stats)

    return stats


# ============================================================
# گزارش فروش روزانه
# ============================================================

async def send_daily_report(bot, admin_id: int) -> None:
    today_start = _day_start()

    with get_connection() as conn:
        sold = conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE is_trial = 0 AND created_at >= ?",
            (today_start,)
        ).fetchone()["c"]

        # کسرها منفی ثبت می‌شوند؛ درآمد خالص = −(خریدها + تمدیدها − بازگشت وجه‌ها)
        revenue = -int(conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM wallet_transactions "
            "WHERE type IN ('purchase', 'renewal', 'auto_renew', 'auto_renew_refund', 'refund') "
            "AND created_at >= ?",
            (today_start,)
        ).fetchone()["s"])

        today_str = _now_tehran().strftime("%Y-%m-%d")
        active = conn.execute(
            "SELECT COUNT(DISTINCT telegram_id) AS c FROM user_activity WHERE activity_date = ?",
            (today_str,)
        ).fetchone()["c"]

        new_users = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE joined_at >= ?",
            (today_start,)
        ).fetchone()["c"]

    report_text = (
        f"{_premium('diamond', '📊')} <b>گزارش فروش روزانه</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{_premium('shopping-bag', '🛒')} <b>سرویس‌های فروخته‌شده:</b> {sold}\n"
        f"{_premium('money-bag', '💰')} <b>درآمد امروز:</b> {revenue:,} تومان\n"
        f"{_premium('users', '👥')} <b>کاربران فعال:</b> {active}\n"
        f"{_premium('sparkles', '🆕')} <b>کاربران جدید:</b> {new_users}"
    )
    try:
        await bot.send_message(admin_id, report_text)
        logger.info("Daily report sent. sold=%s revenue=%s active=%s new=%s", sold, revenue, active, new_users)
    except Exception:
        logger.exception("Failed to send daily report.")
        send_alert("ERROR", "ارسال گزارش روزانه به ادمین ناموفق بود.")


async def daily_report_loop(bot, admin_id: int) -> None:
    while True:
        now = _now_tehran()
        target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)

        if target <= now:
            target += timedelta(days=1)

        wait_seconds = max(1, (target - now).total_seconds())
        logger.info("Next daily report in %.1f hours.", wait_seconds / 3600)
        await asyncio.sleep(wait_seconds)

        try:
            await send_daily_report(bot, admin_id)
        except Exception:
            logger.exception("Daily report loop error.")


# ============================================================
# حلقه ساعتی
# ============================================================

async def hourly_loop(panel_registry: PanelRegistry, bot=None) -> None:
    while True:
        try:
            volume_stats = await auto_renew_volume(panel_registry, bot)
            logger.info("Hourly volume stats: %s", volume_stats)
        except Exception:
            logger.exception("Hourly auto-renew volume error.")

        try:
            lifecycle_stats = await run_lifecycle_cycle(panel_registry)
            logger.info("Hourly lifecycle stats: %s", lifecycle_stats)
        except Exception:
            logger.exception("Hourly lifecycle error.")

        await asyncio.sleep(HOURLY_INTERVAL)


# ============================================================
# حلقه روزانه نامحدود
# ============================================================

async def daily_unlimited_loop(panel_registry: PanelRegistry, bot=None) -> None:
    while True:
        try:
            stats = await auto_renew_unlimited(panel_registry, bot)
            logger.info("Daily unlimited stats: %s", stats)
        except Exception:
            logger.exception("Daily unlimited loop error.")

        await asyncio.sleep(DAILY_INTERVAL)


# ============================================================
# شروع همه Schedulerها
# ============================================================

async def start_schedulers(bot, admin_id: int, panel_registry: PanelRegistry) -> None:
    logger.info("Starting all scheduler tasks.")
    tasks = [
        daily_report_loop(bot, admin_id),
        hourly_loop(panel_registry, bot),
        daily_unlimited_loop(panel_registry, bot),
    ]
    # هر حلقه خطاهای خودش را می‌گیرد؛ return_exceptions تا خرابی یکی بقیه را متوقف نکند
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
            logger.error("Scheduler task ended unexpectedly: %r", res)
    for res in results:
        if isinstance(res, asyncio.CancelledError):
            raise res
