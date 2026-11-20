import asyncio
import logging
from typing import Optional

from aiogram import Bot, BaseMiddleware, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
)

from config import BOT_TOKEN, ADMIN_ID
from database import init_database, get_setting, get_panels
from pasar_guard import panel_registry
from services.scheduler import start_schedulers
from logging_setup import setup_logging

from rich import initialize_premium_emojis

# ============================================================
# USER ROUTERS
# ============================================================

from handlers import (
    start,
    buy,
    renew,
    account,
    wallet,
    referral,
    wheel,
    test,
    support,
    guide,
)

# ============================================================
# TASK ROUTERS
# ============================================================

# مدیریت مأموریت‌ها از سمت ادمین
from handlers.admin import task as admin_task

# ============================================================
# PAYMENT ROUTERS
# ============================================================

from handlers.payments import stars, card

# ============================================================
# ADMIN ROUTERS
# ============================================================

from handlers.admin import (
    panel,
    sales,
    prices,
    products,
    discounts,
    services,
    users,
    users_list,
    broadcast,
    backup,
    stats,
    links,
    test_panel,
    receipts,
    panels,
)

# ============================================================
# DEVELOPER ROUTER
# ============================================================

from dev import panel as dev_panel

# ============================================================
# GROUP / FORCE SUB ROUTERS
# ============================================================

from handlers.group_guard import router as group_guard_router
from handlers.force_sub import router as force_sub_router

# ============================================================
# MIDDLEWARES
# ============================================================

from middlewares.private_only import PrivateOnlyMiddleware
from middlewares.referral_capture import ReferralCaptureMiddleware
from middlewares.behavior_guard import BehaviorGuardMiddleware
from middlewares.force_sub import ForceSubMiddleware

# ============================================================
# DEVELOPER ALERTS
# ============================================================

from dev.alerts import (
    setup_alerts,
    alert_worker,
    send_alert,
    install_log_handler,
    describe_update,
    flush_alerts,
)


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# GLOBAL RESOURCES
# ============================================================

_scheduler_task: Optional[asyncio.Task] = None
_alert_worker_task: Optional[asyncio.Task] = None


# ============================================================
# DEBUG MIDDLEWARE
# ============================================================

class CallbackDebugMiddleware(BaseMiddleware):

    async def __call__(self, handler, event, data):

        logger.info(
            "[CALLBACK RECEIVED] data=%r user=%s",
            event.data,
            event.from_user.id if event.from_user else None,
        )

        try:

            result = await handler(event, data)

            logger.info(
                "[CALLBACK COMPLETED] data=%r",
                event.data,
            )

            return result

        except Exception:

            logger.warning(
                "[CALLBACK FAILED] data=%r user=%s",
                event.data,
                event.from_user.id if event.from_user else None,
                exc_info=True,
            )

            raise


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

_IGNORED_BAD_REQUEST = (
    "message is not modified",
    "query is too old",
    "query id is invalid",
    "message to edit not found",
)


async def global_error_handler(event: ErrorEvent) -> bool:
    """
    ثبت خطاهای مدیریت‌نشده aiogram همراه با اطلاعات آپدیت و کاربر.
    """

    exc = event.exception

    # خطاهای عادی تلگرام که ارزش هشدار ندارند
    if isinstance(exc, TelegramForbiddenError):

        logger.warning(
            "User blocked the bot or chat is forbidden: %s",
            exc,
        )

        return True

    if isinstance(exc, TelegramBadRequest) and any(
        s in str(exc).lower()
        for s in _IGNORED_BAD_REQUEST
    ):

        logger.info(
            "Ignored harmless Telegram error: %s",
            exc,
        )

        return True

    update = event.update

    context = describe_update(update)

    # استخراج نام کامل کاربر
    for kind in (
        "message",
        "edited_message",
        "callback_query",
        "my_chat_member",
        "inline_query",
    ):

        obj = getattr(update, kind, None)

        if obj is None:
            continue

        user = getattr(obj, "from_user", None)

        if user is not None:

            context["full_name"] = user.full_name

            if user.username:
                context["username"] = f"@{user.username}"

            break

    user_id = context.pop("user_id", None)

    send_alert(
        "ERROR",
        "خطای مدیریت‌نشده هنگام پردازش آپدیت تلگرام",
        exc=event.exception,
        user_id=user_id,
        **context,
    )

    return True


# ============================================================
# BACKGROUND TASK HANDLER
# ============================================================

def _scheduler_task_done(task: asyncio.Task) -> None:
    """ثبت وضعیت پایان Scheduler."""

    if task.cancelled():

        logger.info(
            "Background scheduler task cancelled."
        )

        return

    exception = task.exception()

    if exception:

        logger.error(
            "Background scheduler stopped with an error.",
            exc_info=(
                type(exception),
                exception,
                exception.__traceback__,
            ),
        )

    else:

        logger.info(
            "Background scheduler task finished."
        )


# ============================================================
# STARTUP
# ============================================================

async def _check_panels() -> None:
    """
    بررسی پنل‌های ثبت‌شده در شروع ربات (بدون کرش):
    * هیچ پنل فعالی نیست → هشدار WARNING (ادمین باید پنل اضافه کند)
    * اتصال هر پنل فعال تست می‌شود؛ شکست → هشدار ERROR با نام پنل (بدون آدرس/رمز)
    """
    try:
        panels = await asyncio.to_thread(get_panels, True)
    except Exception as exc:
        logger.exception("Could not read panels from database.")
        send_alert("ERROR", "خواندن لیست پنل‌ها از دیتابیس ناموفق بود", exc=exc)
        return

    if not panels:
        logger.warning("No active PasarGuard panel is registered.")
        send_alert(
            "WARNING",
            "هیچ پنل فعالی ثبت نشده؛ تا ثبت پنل، خرید و ساخت سرویس کار نمی‌کند.",
        )
        return

    async def _probe(panel: dict) -> None:
        panel_id = panel.get("id")
        name = str(panel.get("name") or panel_id)
        try:
            client = await panel_registry.get_client(int(panel_id))
            result = await asyncio.wait_for(client.test_connection(), timeout=30)
        except Exception as exc:
            logger.exception("Startup panel check crashed. panel=%s", name)
            send_alert("ERROR", "تست اتصال پنل هنگام استارت با خطا مواجه شد",
                       exc=exc, panel=name, panel_id=panel_id)
            return
        if isinstance(result, dict) and result.get("success"):
            logger.info("Panel '%s' (#%s) connected.", name, panel_id)
        else:
            message = (result or {}).get("message", "unknown") if isinstance(result, dict) else "unknown"
            logger.error("Panel '%s' (#%s) connection failed: %s", name, panel_id, message)
            send_alert("ERROR", "اتصال به پنل ناموفق است", panel=name, panel_id=panel_id,
                       reason=str(message)[:200])

    await asyncio.gather(*(_probe(p) for p in panels), return_exceptions=True)


async def on_startup(bot: Bot) -> None:
    """
    عملیات لازم قبل از شروع دریافت پیام‌ها.
    """

    global _scheduler_task

    logger.info("===================================")
    logger.info("Bot startup initiated.")
    logger.info("===================================")

    # --------------------------------------------------------
    # 1. DATABASE
    # --------------------------------------------------------

    try:

        init_database()

        logger.info(
            "Database initialized successfully."
        )

    except Exception:

        logger.exception(
            "Database initialization failed."
        )

        raise

    # --------------------------------------------------------
    # 2. CONFIGURATION COUNTERS
    # --------------------------------------------------------

    try:

        from services.counter import sync_counter_with_database

        sync_counter_with_database(is_test=False)
        sync_counter_with_database(is_test=True)

        logger.info(
            "Configuration counters synchronized."
        )

    except Exception:

        logger.exception(
            "Counter synchronization failed."
        )

        raise

    # --------------------------------------------------------
    # 3. DEFAULT SETTINGS
    # --------------------------------------------------------

    try:

        if get_setting("sales_open") is None:

            from database import set_setting

            set_setting("sales_open", "1")

            logger.info(
                "Default sales_open setting created."
            )

    except Exception:

        logger.exception(
            "Default settings initialization failed."
        )

        raise

    # --------------------------------------------------------
    # 4. PASARGUARD PANELS CHECK
    # --------------------------------------------------------
    # پنل‌ها از داخل دیتابیس خوانده می‌شوند و ادمین از ربات اضافه‌شان می‌کند؛
    # نبودن پنل نباید مانع روشن‌شدن ربات شود، فقط باید به دولوپر هشدار برود.

    await _check_panels()

    # --------------------------------------------------------
    # 5. BACKGROUND SCHEDULER
    # --------------------------------------------------------

    try:

        if _scheduler_task is None or _scheduler_task.done():

            _scheduler_task = asyncio.create_task(
                start_schedulers(
                    bot,
                    ADMIN_ID,
                    panel_registry,
                ),
                name="panel-background-scheduler",
            )

            _scheduler_task.add_done_callback(
                _scheduler_task_done
            )

            logger.info(
                "Background scheduler started."
            )

    except Exception:

        logger.exception(
            "Could not start background scheduler."
        )

        raise

    logger.info(
        "Bot startup completed successfully."
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def on_shutdown(bot: Bot) -> None:
    """
    پاک‌سازی منابع هنگام خاموش شدن Dispatcher.
    """

    global _scheduler_task

    logger.info(
        "Bot shutdown initiated."
    )

    if _scheduler_task is not None:

        if not _scheduler_task.done():

            logger.info(
                "Stopping background scheduler..."
            )

            _scheduler_task.cancel()

            await asyncio.gather(
                _scheduler_task,
                return_exceptions=True,
            )

        _scheduler_task = None

    logger.info(
        "Bot shutdown completed."
    )


# ============================================================
# ROUTER REGISTRATION
# ============================================================

def register_routers(dp: Dispatcher) -> None:
    """
    ثبت تمام Routerهای ربات.

    ترتیب ثبت:
    1. Private Only
    2. Group Guard / Force Sub
    3. User Routers
    4. Payment Routers
    5. Admin Routers
    6. Developer Router
    """

    # --------------------------------------------------------
    # 1. PRIVATE ONLY
    # باید اولین Middleware سطح Update باشد.
    # --------------------------------------------------------

    dp.update.outer_middleware(
        PrivateOnlyMiddleware()
    )

    # --------------------------------------------------------
    # 2. GROUP GUARD / FORCE SUB ROUTERS
    # --------------------------------------------------------

    dp.include_router(
        group_guard_router
    )

    dp.include_router(
        force_sub_router
    )

    # --------------------------------------------------------
    # 3. USER ROUTERS
    # --------------------------------------------------------

    dp.include_router(start.router)
    dp.include_router(buy.router)
    dp.include_router(renew.router)
    dp.include_router(account.router)
    dp.include_router(wallet.router)
    dp.include_router(referral.router)
    dp.include_router(wheel.router)
    dp.include_router(test.router)
    dp.include_router(support.router)
    dp.include_router(guide.router)

    # --------------------------------------------------------
    # 4. PAYMENT ROUTERS
    # --------------------------------------------------------

    dp.include_router(card.router)
    dp.include_router(stars.router)

    # --------------------------------------------------------
    # 5. ADMIN ROUTERS
    # --------------------------------------------------------

    dp.include_router(panel.router)
    dp.include_router(sales.router)
    dp.include_router(prices.router)
    dp.include_router(products.router)
    dp.include_router(panels.router)
    dp.include_router(discounts.router)
    dp.include_router(services.router)
    dp.include_router(users.router)
    dp.include_router(users_list.router)
    dp.include_router(broadcast.router)
    dp.include_router(backup.router)
    dp.include_router(stats.router)
    dp.include_router(links.router)
    dp.include_router(test_panel.router)
    dp.include_router(receipts.router)

    # --------------------------------------------------------
    # 5.1. ADMIN TASK ROUTER
    # --------------------------------------------------------

    dp.include_router(
        admin_task.router
    )

    # --------------------------------------------------------
    # 6. DEVELOPER ROUTER
    # --------------------------------------------------------

    dp.include_router(
        dev_panel.router
    )

    logger.info(
        "All routers registered successfully."
    )


# ============================================================
# RESOURCE CLEANUP
# ============================================================

async def close_resources(
    bot: Bot,
) -> None:
    """
    بستن منابع برنامه حتی در صورت بروز خطا.
    """

    global _scheduler_task

    # --------------------------------------------------------
    # 1. STOP SCHEDULER
    # --------------------------------------------------------

    if _scheduler_task is not None:

        if not _scheduler_task.done():

            logger.info(
                "Cancelling scheduler task..."
            )

            _scheduler_task.cancel()

            await asyncio.gather(
                _scheduler_task,
                return_exceptions=True,
            )

        _scheduler_task = None

    # --------------------------------------------------------
    # 2. CLOSE PASARGUARD PANEL CLIENTS
    # --------------------------------------------------------

    try:

        await panel_registry.close_all()

        logger.info(
            "PasarGuard panel clients closed."
        )

    except Exception:

        logger.exception(
            "Error while closing PasarGuard panel clients."
        )

    # --------------------------------------------------------
    # 3. CLOSE TELEGRAM SESSION
    # --------------------------------------------------------

    try:

        await bot.session.close()

        logger.info(
            "Telegram session closed."
        )

    except Exception:

        logger.exception(
            "Error while closing Telegram session."
        )


# ============================================================
# MAIN
# ============================================================

async def main() -> None:

    global _alert_worker_task

    bot: Optional[Bot] = None

    # --------------------------------------------------------
    # 1. LOGGING
    # --------------------------------------------------------

    setup_logging(
        log_level=logging.INFO
    )

    install_log_handler(
        level=logging.ERROR
    )

    logger.info(
        "Initializing bot application..."
    )

    print(
        "[DEBUG 01] Logging initialized",
        flush=True,
    )

    # --------------------------------------------------------
    # 2. TOKEN VALIDATION
    # --------------------------------------------------------

    if not BOT_TOKEN or not isinstance(BOT_TOKEN, str):

        raise RuntimeError(
            "BOT_TOKEN is missing or invalid in config.py"
        )

    if not ADMIN_ID:

        raise RuntimeError(
            "ADMIN_ID is missing in config.py"
        )

    print(
        "[DEBUG 02] Token and Admin validated",
        flush=True,
    )

    # --------------------------------------------------------
    # 3. CREATE BOT
    # --------------------------------------------------------

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    print(
        "[DEBUG 03] Bot instance created",
        flush=True,
    )

    # --------------------------------------------------------
    # 3.1. SETUP DEVELOPER ALERTS
    # --------------------------------------------------------

    setup_alerts(bot)

    _alert_worker_task = asyncio.create_task(
        alert_worker(),
        name="developer-alert-worker",
    )

    try:

        # ----------------------------------------------------
        # 4. INITIALIZE PREMIUM EMOJIS
        # ----------------------------------------------------

        try:

            loaded = await initialize_premium_emojis(bot)

            logger.info(
                "Loaded %s Premium Emojis.",
                loaded,
            )

        except Exception:

            logger.exception(
                "Premium Emoji initialization failed. "
                "Bot will continue without validated emoji cache."
            )

        print(
            "[DEBUG 04] Premium Emoji initialization finished",
            flush=True,
        )

        # ----------------------------------------------------
        # 5. CREATE DISPATCHER
        # ----------------------------------------------------

        dp = Dispatcher()

        # ----------------------------------------------------
        # 5.0. GLOBAL ERROR HANDLER
        # ----------------------------------------------------

        dp.errors.register(
            global_error_handler
        )

        # ----------------------------------------------------
        # 5.0.1. REFERRAL CAPTURE
        # ----------------------------------------------------

        dp.message.outer_middleware(
            ReferralCaptureMiddleware()
        )

        # ----------------------------------------------------
        # 5.1. BEHAVIOR GUARD
        # ----------------------------------------------------

        dp.message.middleware(
            BehaviorGuardMiddleware()
        )

        dp.callback_query.middleware(
            BehaviorGuardMiddleware()
        )

        # ----------------------------------------------------
        # 5.1.1. FORCE SUB MIDDLEWARE
        # ----------------------------------------------------

        dp.message.middleware(
            ForceSubMiddleware()
        )

        dp.callback_query.middleware(
            ForceSubMiddleware()
        )

        # ----------------------------------------------------
        # 5.2. CALLBACK DEBUG MIDDLEWARE
        # ----------------------------------------------------

        dp.callback_query.middleware(
            CallbackDebugMiddleware()
        )

        # ----------------------------------------------------
        # 6. PASARGUARD PANEL REGISTRY
        # ----------------------------------------------------
        # کلاینت هر پنل هنگام اولین استفاده از دیتابیس ساخته و کش می‌شود.
        #
        # ⚠️ موقت: تا وقتی renew/services/stars/test/wheel به panel_registry
        # منتقل نشده‌اند، پارامتر `paxiva` آن‌ها باید در DI وجود داشته باشد
        # (وگرنه aiogram با TypeError هندلر را می‌اندازد). بعد از انتقال
        # آخرین فایل، این خط را حذف کن.
        dp["paxiva"] = panel_registry

        logger.info(
            "PasarGuard panel registry ready."
        )

        print(
            "[DEBUG 05] PasarGuard registry ready",
            flush=True,
        )

        # ----------------------------------------------------
        # 7. REGISTER ROUTERS
        # ----------------------------------------------------

        register_routers(dp)

        print(
            "[DEBUG 06] Routers registered",
            flush=True,
        )

        # ----------------------------------------------------
        # 8. REGISTER STARTUP / SHUTDOWN
        # ----------------------------------------------------

        dp.startup.register(
            on_startup
        )

        dp.shutdown.register(
            on_shutdown
        )

        # ----------------------------------------------------
        # 9. START POLLING
        # ----------------------------------------------------

        print(
            "[DEBUG 07] Starting polling",
            flush=True,
        )

        logger.info(
            "Starting Telegram polling..."
        )

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    except asyncio.CancelledError:

        logger.info(
            "Bot polling cancelled."
        )

        raise

    except Exception as exc:

        logger.exception(
            "Fatal error during bot execution."
        )

        send_alert(
            "CRITICAL",
            "ربات هنگام اجرا از کار افتاد",
            exc=exc,
        )

        raise

    finally:

        logger.info(
            "Closing application resources..."
        )

        # ----------------------------------------------------
        # 10. FLUSH DEVELOPER ALERTS
        # ----------------------------------------------------

        try:

            await flush_alerts(5)

        except Exception:

            logger.exception(
                "Could not flush developer alerts."
            )

        # ----------------------------------------------------
        # 11. STOP ALERT WORKER
        # ----------------------------------------------------

        if _alert_worker_task is not None:

            _alert_worker_task.cancel()

            await asyncio.gather(
                _alert_worker_task,
                return_exceptions=True,
            )

            _alert_worker_task = None

        # ----------------------------------------------------
        # 12. CLOSE RESOURCES
        # ----------------------------------------------------

        if bot is not None:

            await close_resources(bot)

        logger.info(
            "Application resources closed."
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped by keyboard interrupt."
        )

    except SystemExit:

        logger.info(
            "Bot process exited."
        )

    except Exception:

        logger.exception(
            "Bot terminated due to an unexpected error."
        )
