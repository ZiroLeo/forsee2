import asyncio
import logging
from typing import Optional

from aiogram import Bot, BaseMiddleware, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BOT_TOKEN, ADMIN_ID
from database import init_database, get_setting
from paxiva import PaxivaClient
from services.scheduler import start_schedulers
from logging_setup import setup_logging

from rich import initialize_premium_emojis

# ============================================================
# ROUTERS
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

from handlers.payments import stars, card

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
)

from dev import panel as dev_panel

from middlewares.behavior_guard import BehaviorGuardMiddleware
from dev.alerts import setup_alerts, alert_worker, send_alert


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# GLOBAL RESOURCES
# ============================================================

_scheduler_task: Optional[asyncio.Task] = None
paxiva_client: Optional[PaxivaClient] = None


# ============================================================
# DEBUG MIDDLEWARE (تشخیصی)
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
            logger.info("[CALLBACK COMPLETED] data=%r", event.data)
            return result
        except Exception:
            logger.exception("[CALLBACK FAILED] data=%r", event.data)
            raise


# ============================================================
# BACKGROUND TASK HANDLER
# ============================================================

def _scheduler_task_done(task: asyncio.Task) -> None:
    """ثبت وضعیت پایان Scheduler."""

    if task.cancelled():
        logger.info("Background scheduler task cancelled.")
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
        logger.info("Background scheduler task finished.")


# ============================================================
# STARTUP
# ============================================================

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
        logger.info("Database initialized successfully.")

    except Exception:
        logger.exception("Database initialization failed.")
        raise

    # --------------------------------------------------------
    # 2. CONFIGURATION COUNTERS
    # --------------------------------------------------------

    try:
        from services.counter import sync_counter_with_database

        sync_counter_with_database(is_test=False)
        sync_counter_with_database(is_test=True)

        logger.info("Configuration counters synchronized.")

    except Exception:
        logger.exception("Counter synchronization failed.")
        raise

    # --------------------------------------------------------
    # 3. DEFAULT SETTINGS
    # --------------------------------------------------------

    try:
        if get_setting("sales_open") is None:
            from database import set_setting

            set_setting("sales_open", "1")

            logger.info("Default sales_open setting created.")

    except Exception:
        logger.exception("Default settings initialization failed.")
        raise

    # --------------------------------------------------------
    # 4. PAXIVA CHECK
    # --------------------------------------------------------

    if paxiva_client is None:
        raise RuntimeError(
            "PaxivaClient was not initialized before startup."
        )

    # --------------------------------------------------------
    # 5. BACKGROUND SCHEDULER
    # --------------------------------------------------------

    try:
        if _scheduler_task is None or _scheduler_task.done():

            _scheduler_task = asyncio.create_task(
                start_schedulers(
                    bot,
                    ADMIN_ID,
                    paxiva_client,
                ),
                name="paxiva-background-scheduler",
            )

            _scheduler_task.add_done_callback(
                _scheduler_task_done
            )

            logger.info("Background scheduler started.")

    except Exception:
        logger.exception("Could not start background scheduler.")
        raise

    logger.info("Bot startup completed successfully.")


# ============================================================
# SHUTDOWN
# ============================================================

async def on_shutdown(bot: Bot) -> None:
    """
    پاک‌سازی منابع هنگام خاموش شدن Dispatcher.
    """

    global _scheduler_task

    logger.info("Bot shutdown initiated.")

    if _scheduler_task is not None:

        if not _scheduler_task.done():

            logger.info("Stopping background scheduler...")

            _scheduler_task.cancel()

            await asyncio.gather(
                _scheduler_task,
                return_exceptions=True,
            )

        _scheduler_task = None

    logger.info("Bot shutdown completed.")


# ============================================================
# ROUTER REGISTRATION
# ============================================================

def register_routers(dp: Dispatcher) -> None:
    """
    ثبت تمام Routerهای ربات.
    ترتیب ثبت حفظ شده است.
    """

    # --------------------------------------------------------
    # USER ROUTERS
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
    # PAYMENT ROUTERS
    # --------------------------------------------------------

    dp.include_router(card.router)
    dp.include_router(stars.router)

    # --------------------------------------------------------
    # ADMIN ROUTERS
    # --------------------------------------------------------

    dp.include_router(panel.router)
    dp.include_router(sales.router)
    dp.include_router(prices.router)
    dp.include_router(products.router)
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
    # DEVELOPER ROUTER
    # --------------------------------------------------------

    dp.include_router(dev_panel.router)

    logger.info("All routers registered successfully.")


# ============================================================
# RESOURCE CLEANUP
# ============================================================

async def close_resources(
    bot: Bot,
    paxiva: Optional[PaxivaClient],
) -> None:
    """
    بستن منابع برنامه حتی در صورت بروز خطا.
    """

    global _scheduler_task

    # --------------------------------------------------------
    # STOP SCHEDULER
    # --------------------------------------------------------

    if _scheduler_task is not None:

        if not _scheduler_task.done():

            logger.info("Cancelling scheduler task...")

            _scheduler_task.cancel()

            await asyncio.gather(
                _scheduler_task,
                return_exceptions=True,
            )

        _scheduler_task = None

    # --------------------------------------------------------
    # CLOSE PAXIVA
    # --------------------------------------------------------

    if paxiva is not None:

        try:
            await paxiva.close()
            logger.info("PaxivaClient closed.")

        except Exception:
            logger.exception("Error while closing PaxivaClient.")

    # --------------------------------------------------------
    # CLOSE TELEGRAM SESSION
    # --------------------------------------------------------

    try:
        await bot.session.close()
        logger.info("Telegram session closed.")

    except Exception:
        logger.exception("Error while closing Telegram session.")


# ============================================================
# MAIN
# ============================================================

async def main() -> None:

    global paxiva_client

    bot: Optional[Bot] = None

    # --------------------------------------------------------
    # 1. LOGGING
    # --------------------------------------------------------

    setup_logging(log_level=logging.INFO)

    logger.info("Initializing bot application...")

    print("[DEBUG 01] Logging initialized", flush=True)

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

    print("[DEBUG 02] Token and Admin validated", flush=True)

    # --------------------------------------------------------
    # 3. CREATE BOT
    # --------------------------------------------------------

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    print("[DEBUG 03] Bot instance created", flush=True)

    # --------------------------------------------------------
    # 3.1. SETUP DEV ALERTS
    # --------------------------------------------------------

    setup_alerts(bot)
    asyncio.create_task(alert_worker())

    # --------------------------------------------------------
    # 3.2. TEST ALERT (موقت - برای تست)
    # --------------------------------------------------------

    send_alert("INFO", "تست هشدار - اگر این پیام را دیدی، سیستم هشدار کار می‌کند.")

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

        print("[DEBUG 04] Premium Emoji initialization finished", flush=True)

        # ----------------------------------------------------
        # 5. CREATE DISPATCHER
        # ----------------------------------------------------

        dp = Dispatcher()

        # ----------------------------------------------------
        # 5.1. REGISTER BEHAVIOR GUARD (اولویت اول)
        # ----------------------------------------------------

        dp.message.middleware(
            BehaviorGuardMiddleware()
        )
        dp.callback_query.middleware(
            BehaviorGuardMiddleware()
        )

        # ----------------------------------------------------
        # 5.2. REGISTER DEBUG MIDDLEWARE
        # ----------------------------------------------------

        dp.callback_query.middleware(
            CallbackDebugMiddleware()
        )

        # ----------------------------------------------------
        # 6. INITIALIZE PAXIVA
        # ----------------------------------------------------

        paxiva_client = PaxivaClient()

        print("[DEBUG 05] Paxiva initialized", flush=True)

        dp["paxiva"] = paxiva_client

        logger.info("PaxivaClient initialized.")

        # ----------------------------------------------------
        # 7. REGISTER ROUTERS
        # ----------------------------------------------------

        register_routers(dp)

        print("[DEBUG 06] Routers registered", flush=True)

        # ----------------------------------------------------
        # 8. REGISTER STARTUP / SHUTDOWN
        # ----------------------------------------------------

        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)

        # ----------------------------------------------------
        # 9. START POLLING
        # ----------------------------------------------------

        print("[DEBUG 07] Starting polling", flush=True)

        logger.info("Starting Telegram polling...")

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    except asyncio.CancelledError:

        logger.info("Bot polling cancelled.")

        raise

    except Exception:

        logger.exception(
            "Fatal error during bot execution."
        )

        raise

    finally:

        logger.info("Closing application resources...")

        await close_resources(
            bot,
            paxiva_client,
        )

        paxiva_client = None

        logger.info("Application resources closed.")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt.")

    except SystemExit:
        logger.info("Bot process exited.")

    except Exception:
        logger.exception(
            "Bot terminated due to an unexpected error."
        )