import asyncio
import logging
import time

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup

from config import ADMIN_ID
from database import (
    get_receipt_admin_id, set_receipt_admin_id, get_user, get_user_by_identifier,
    is_banned, get_pending_payments, get_payment_stats, get_reviewer_stats,
    log_admin_action, log_event,
)

from .admin_ui import (
    btn, menu, page, toast, em, esc, money, LINE, protect, ask, refresh_panel,
    reset_flow, BLUE, GREEN, RED,
)

logger = logging.getLogger(__name__)
router = Router(name="admin_receipts")
protect(router)

MAX_RESEND = 20


class ReceiptAdminStates(StatesGroup):
    waiting_user = State()


# ============================================================
# ابزارها
# ============================================================

def current_receipt_admin() -> int:
    """شناسهٔ فعلی ادمین رسید (اگر تنظیم نشده باشد: ادمین اصلی)."""
    return get_receipt_admin_id(ADMIN_ID)


def _person(telegram_id: int) -> str:
    user = get_user(telegram_id) or {}
    name = esc(user.get("first_name") or "—")
    username = f" (@{esc(user['username'])})" if user.get("username") else ""
    return f"<code>{telegram_id}</code> — {name}{username}"


def _age(ts: int) -> str:
    seconds = max(0, int(time.time()) - int(ts))
    if seconds < 3600:
        return f"{seconds // 60} دقیقه پیش"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت پیش"
    return f"{seconds // 86400} روز پیش"


def _receipt_keyboard(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        btn("تأیید پرداخت", f"admin:pay:approve:{req_id}", style=GREEN, emoji="true"),
        btn("رد پرداخت", f"admin:pay:reject:{req_id}", style=RED, emoji="false"),
    ]])


def _receipt_caption(req: dict) -> str:
    return (
        f"{em('stars', '⭐')} <b>درخواست شارژ در انتظار</b>\n\n"
        f"👤 کاربر: <code>{req['telegram_id']}</code>\n"
        f"💰 مبلغ: <b>{money(req['amount'])} تومان</b>\n"
        f"🆔 شماره درخواست: <code>{req['id']}</code>\n"
        f"🕒 ثبت: {_age(req['created_at'])}\n\n"
        "وضعیت: <b>در انتظار بررسی</b>"
    )


# ============================================================
# منوی اصلی
# ============================================================

def _menu_view():
    reviewer = current_receipt_admin()
    delegated = reviewer != ADMIN_ID
    stats = get_payment_stats()

    if delegated:
        who = f"{em('true', '✅')} <b>ادمین رسید:</b> {_person(reviewer)}"
        rs = get_reviewer_stats(reviewer)
        mine = f"\n📊 بررسی‌شده توسط او: ✅ {rs['approved']} تأیید | ❌ {rs['rejected']} رد"
        note = "رسیدها فقط برای او ارسال می‌شوند و برای ادمین اصلی نمی‌آیند."
    else:
        who = f"{em('light', 'ℹ️')} <b>ادمین رسید:</b> تعیین نشده (رسیدها برای ادمین اصلی می‌آید)"
        mine = ""
        note = "با تعیین ادمین رسید، رسیدهای واریزی فقط برای او ارسال می‌شود."

    text = (
        f"{em('light', '🧾')} <b>مدیریت ادمین رسید</b>\n{LINE}\n"
        f"{who}{mine}\n{LINE}\n"
        f"⏳ در انتظار بررسی: <b>{stats['pending']}</b>\n"
        f"✅ تأییدشده: <b>{stats['approved']}</b> ({money(stats['approved_amount'])} تومان)\n"
        f"❌ ردشده: <b>{stats['rejected']}</b>\n{LINE}\n{note}"
    )

    buttons = [btn("تعیین ادمین رسید" if not delegated else "تغییر ادمین رسید",
                   "admin:receipt_admin:set", style=BLUE, emoji="telegram")]
    if delegated:
        buttons.append(btn("حذف ادمین رسید", "admin:receipt_admin:remove", style=RED, emoji="false"))
    buttons.append(btn(f"رسیدهای در انتظار ({stats['pending']})", "admin:receipt_admin:pending", style=GREEN, emoji="wallet"))
    if delegated:
        buttons.append(btn("پیام تست", "admin:receipt_admin:test", style=BLUE, emoji="ask"))
    return text, menu(buttons)


@router.callback_query(F.data == "admin:receipt_admin:menu")
async def receipt_admin_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = _menu_view()
    await page(callback, text, markup)


# ============================================================
# تعیین ادمین رسید
# ============================================================

@router.callback_query(F.data == "admin:receipt_admin:set")
async def set_start(callback: CallbackQuery, state: FSMContext):
    await ask(
        callback, state, ReceiptAdminStates.waiting_user,
        f"{em('telegram', '👤')} <b>تعیین ادمین رسید</b>\n{LINE}\n"
        "آیدی عددی یا یوزرنیم شخص را بفرست.\n"
        "او باید قبلاً ربات را استارت کرده باشد.",
        "admin:receipt_admin:menu",
    )


@router.message(ReceiptAdminStates.waiting_user, F.text)
async def set_received(message: Message, state: FSMContext, bot: Bot):
    target = get_user_by_identifier(message.text.strip())

    problem = None
    if not target:
        problem = "کاربری پیدا نشد. او باید قبلاً ربات را استارت کرده باشد."
    elif target["telegram_id"] == ADMIN_ID:
        problem = "این شخص ادمین اصلی است. برای برگشت از «حذف ادمین رسید» استفاده کن."
    elif is_banned(target["telegram_id"]):
        problem = "این کاربر مسدود است."
    elif target["telegram_id"] == current_receipt_admin():
        problem = "این شخص همین حالا ادمین رسید است."

    if problem:
        try:
            await message.delete()
        except Exception:
            pass
        await refresh_panel(
            bot, state, message,
            f"{em('warning', '⚠️')} <b>{problem}</b>\nدوباره آیدی یا یوزرنیم بفرست.",
            menu([], back="admin:receipt_admin:menu", back_text="انصراف"),
            delete_source=False,
        )
        return

    tid = target["telegram_id"]
    await reset_flow(state)
    await state.update_data(new_receipt_admin=tid)
    await refresh_panel(
        bot, state, message,
        f"{em('warning', '⚠️')} <b>تأیید نهایی</b>\n{LINE}\n"
        f"{_person(tid)} به‌عنوان <b>ادمین رسید</b> تعیین شود؟\n\n"
        "• از این لحظه رسیدهای واریزی فقط برای او ارسال می‌شود.\n"
        "• او فقط تأیید/رد رسید را می‌تواند انجام دهد؛ به پنل مدیریت دسترسی ندارد.\n"
        "• ادمین اصلی دیگر پیام رسید دریافت نمی‌کند.",
        menu(
            [
                btn("تأیید و تعیین", f"admin:receipt_admin:confirm:{tid}", style=GREEN, emoji="true"),
                btn("انصراف", "admin:receipt_admin:menu", style=RED, emoji="false"),
            ],
            back=None,
        ),
    )


@router.callback_query(F.data.startswith("admin:receipt_admin:confirm:"))
async def set_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    tid = int(callback.data.split(":")[3])
    data = await state.get_data()
    # فقط شخصی که در همین فرایند انتخاب شده تأیید می‌شود
    if data.get("new_receipt_admin") != tid or not get_user(tid) or tid == ADMIN_ID:
        await toast(callback, "درخواست منقضی شده؛ دوباره انتخاب کن.", True)
        await state.clear()
        return

    # ابتدا مطمئن می‌شویم ربات می‌تواند به او پیام بدهد؛ وگرنه رسیدها گم می‌شوند.
    try:
        await bot.send_message(
            tid,
            f"{em('true', '✅')} <b>شما به‌عنوان ادمین رسید تعیین شدید.</b>\n\n"
            "از این پس رسیدهای واریزی برای شما ارسال می‌شود. "
            "با دکمه‌های «تأیید» یا «رد» زیر هر رسید تصمیم بگیرید.",
        )
    except Exception:
        logger.warning("Cannot message new receipt admin %s", tid, exc_info=True)
        await toast(callback, "ارسال پیام به این شخص ممکن نشد (احتمالاً ربات را بلاک کرده). تغییری اعمال نشد.", True)
        return

    previous = current_receipt_admin()
    set_receipt_admin_id(tid, callback.from_user.id)
    log_admin_action(callback.from_user.id, "receipt_admin_set", str(tid), f"previous={previous}")
    log_event("INFO", "receipt_admin_changed", callback.from_user.id, f"new={tid}; previous={previous}")

    if previous not in (ADMIN_ID, tid):
        try:
            await bot.send_message(previous, "ℹ️ شما دیگر ادمین رسید نیستید.")
        except Exception:
            pass

    await state.clear()
    await toast(callback, "ادمین رسید تعیین شد.")
    text, markup = _menu_view()
    pending = get_payment_stats()["pending"]
    extra = (f"\n\n{em('warning', '⚠️')} <b>{pending}</b> رسید در انتظار هنوز نزد ادمین قبلی است؛ "
             "برای انتقال از «رسیدهای در انتظار» استفاده کن.") if pending else ""
    await page(callback, f"{em('true', '✅')} <b>ادمین رسید تعیین شد.</b>{extra}\n\n{text}", markup, answer=False)


# ============================================================
# حذف ادمین رسید
# ============================================================

@router.callback_query(F.data == "admin:receipt_admin:remove")
async def remove_ask(callback: CallbackQuery):
    reviewer = current_receipt_admin()
    if reviewer == ADMIN_ID:
        await toast(callback, "ادمین رسیدی تعیین نشده است.", True)
        return
    await page(
        callback,
        f"{em('warning', '⚠️')} <b>ادمین رسید حذف شود؟</b>\n{LINE}\n{_person(reviewer)}\n\n"
        "رسیدها از این لحظه دوباره برای <b>تو (ادمین اصلی)</b> ارسال می‌شود.",
        menu(
            [
                btn("بله، حذف کن", "admin:receipt_admin:remove_yes", style=RED, emoji="false"),
                btn("نه، انصراف", "admin:receipt_admin:menu", style=GREEN, emoji="true"),
            ],
            back=None,
        ),
    )


@router.callback_query(F.data == "admin:receipt_admin:remove_yes")
async def remove_confirm(callback: CallbackQuery, bot: Bot):
    previous = current_receipt_admin()
    if previous == ADMIN_ID:
        await toast(callback, "ادمین رسیدی تعیین نشده است.", True)
        return
    set_receipt_admin_id(ADMIN_ID, callback.from_user.id)
    log_admin_action(callback.from_user.id, "receipt_admin_removed", str(previous))
    log_event("INFO", "receipt_admin_removed", callback.from_user.id, f"removed={previous}")
    try:
        await bot.send_message(previous, "ℹ️ شما دیگر ادمین رسید نیستید.")
    except Exception:
        pass
    pending = get_payment_stats()["pending"]
    await toast(callback, "ادمین رسید حذف شد؛ رسیدها برای تو می‌آید.")
    text, markup = _menu_view()
    extra = (f"\n\n{em('warning', '⚠️')} <b>{pending}</b> رسید در انتظار داری؛ برای دریافتشان "
             "از «رسیدهای در انتظار» استفاده کن.") if pending else ""
    await page(callback, f"{em('true', '✅')} <b>ادمین رسید حذف شد.</b>{extra}\n\n{text}", markup, answer=False)


# ============================================================
# پیام تست
# ============================================================

@router.callback_query(F.data == "admin:receipt_admin:test")
async def send_test(callback: CallbackQuery, bot: Bot):
    reviewer = current_receipt_admin()
    try:
        await bot.send_message(reviewer, f"{em('light', '🧪')} <b>پیام تست</b>\nاگر این پیام را می‌بینی، رسیدها به دستت می‌رسد.")
        await toast(callback, "پیام تست ارسال شد.", True)
    except Exception:
        logger.warning("Test message to receipt admin failed", exc_info=True)
        await toast(callback, "ارسال ناموفق بود؛ شاید ربات را بلاک کرده یا استارت نکرده است.", True)


# ============================================================
# رسیدهای در انتظار + ارسال مجدد
# ============================================================

@router.callback_query(F.data == "admin:receipt_admin:pending")
async def pending_list(callback: CallbackQuery):
    pending = get_pending_payments(10)
    reviewer = current_receipt_admin()
    lines = [f"{em('wallet', '🧾')} <b>رسیدهای در انتظار</b>", LINE]
    if not pending:
        lines.append("رسید در انتظاری وجود ندارد. ✅")
    for req in pending:
        lines.append(f"🆔 <code>{req['id']}</code> | 👤 <code>{req['telegram_id']}</code> | "
                     f"{money(req['amount'])} تومان | {_age(req['created_at'])}")
    buttons = []
    if pending:
        target = "ادمین رسید" if reviewer != ADMIN_ID else "ادمین اصلی"
        lines += [LINE, f"با دکمهٔ زیر، همهٔ این رسیدها دوباره برای {target} ارسال می‌شود."]
        buttons.append(btn(f"ارسال مجدد برای {target}", "admin:receipt_admin:resend", style=GREEN, emoji="telegram"))
    await page(callback, "\n".join(lines), menu(buttons, back="admin:receipt_admin:menu"))


@router.callback_query(F.data == "admin:receipt_admin:resend")
async def resend_pending(callback: CallbackQuery, bot: Bot):
    reviewer = current_receipt_admin()
    pending = get_pending_payments(MAX_RESEND)
    if not pending:
        await toast(callback, "رسید در انتظاری نیست.", True)
        return
    await toast(callback, "در حال ارسال...")
    sent = failed = 0
    for req in pending:
        try:
            await bot.send_photo(
                chat_id=reviewer, photo=req["photo_file_id"],
                caption=_receipt_caption(req), reply_markup=_receipt_keyboard(req["id"]),
            )
            sent += 1
        except Exception:
            logger.warning("Resend failed for request %s", req["id"], exc_info=True)
            failed += 1
        await asyncio.sleep(0.3)
    log_admin_action(callback.from_user.id, "receipts_resent", str(reviewer), f"sent={sent}; failed={failed}")
    note = f"{em('true', '✅')} <b>{sent}</b> رسید ارسال شد." + (f"\n❌ {failed} مورد ناموفق بود." if failed else "")
    await page(callback, note, menu([btn("بازگشت به مدیریت رسید", "admin:receipt_admin:menu", style=GREEN)], back=None), answer=False)