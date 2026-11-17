import logging
import shutil
from pathlib import Path
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, FSInputFile

from config import ADMIN_ID, DB_PATH, BACKUP_DIR
from database import log_event
from rich import build_rich_message, premium_emoji
from utils import rate_limiter

logger = logging.getLogger(__name__)
router = Router(name="admin_backup")


@router.callback_query(F.data == "admin:backup:menu")
async def take_backup(callback: CallbackQuery, bot):
    user = callback.from_user
    if not user or user.id != ADMIN_ID:
        log_event("SECURITY", "unauthorized_backup", user.id if user else 0)
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
    f"{premium_emoji('light', '⏳')} لطفاً کمی صبر کنید.",
    show_alert=True,
    parse_mode="HTML"
)
        return

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"bot_backup_{timestamp}.db"

        # کپی فیزیکی امن فایل دیتابیس
        shutil.copy2(DB_PATH, backup_path)

        await callback.message.edit_text(rich_message=build_rich_message(
            f"{premium_emoji('safe', '🛡')} **بک‌آپ دیتابیس در حال ارسال...**"
        ))
        
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=FSInputFile(backup_path),
            caption=f"📦 بک‌آپ دیتابیس\n🕒 {timestamp}"
        )
        
        # پاک کردن فایل بک‌آپ پس از ارسال (برای جلوگیری از پر شدن دیسک)
        backup_path.unlink(missing_ok=True)
        
        log_event("INFO", "database_backup", user.id, f"file={backup_path.name}")
        await callback.answer("بک‌آپ با موفقیت ارسال شد.")
        
    except Exception as e:
        logger.exception(f"Backup failed: {e}")
        await callback.answer("خطا در بک‌آپ.", show_alert=True)
        log_event("ERROR", "backup_failed", user.id, str(e))