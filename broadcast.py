import asyncio
import logging
import time
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database import get_all_user_ids, get_user_by_identifier, is_banned, log_event
from .admin_ui import (
    LINE, GREEN, RED, BLUE, ask, btn, cancel_kb, em, esc, menu, page,
    protect, refresh_panel, remember_panel, reset_flow, title, toast,
)

logger = logging.getLogger(__name__)
router = Router(name="admin_broadcast")
protect(router)  # همهٔ هندلرها فقط برای ادمین اصلی

# فاصله بین ارسال‌ها (ثانیه) و هر چند نفر یک‌بار گزارش پیشرفت ویرایش شود
SEND_DELAY = 0.05
PROGRESS_EVERY = 25

# فقط یک ارسال همگانی هم‌زمان؛ نگه‌داشتن رفرنس تسک‌ها تا garbage collect نشوند
_active: dict = {"running": False, "stop": False}
_tasks: set[asyncio.Task] = set()


class BroadcastStates(StatesGroup):
    waiting_target = State()    # آیدی/یوزرنیم برای ارسال تکی
    waiting_content = State()   # خود پیام (هر نوع: متن/عکس/ویدیو/فایل/ویس/...)
    waiting_link = State()      # لینک پیام کانال
    confirm = State()           # تأیید نهایی ارسال همگانی


# ============================================================
# منو
# ============================================================

def _menu_markup():
    return menu([
        btn("ارسال به یک کاربر", "admin:broadcast:single", emoji="telegram"),
        btn("ارسال به همه", "admin:broadcast:all", emoji="world"),
        btn("ارسال از لینک کانال", "admin:broadcast:link", emoji="light"),
    ], back="admin:main")


def _menu_text() -> str:
    return (
        f"{title('ask', '📨', 'ارسال پیام')}\n{LINE}\n\n"
        "پیام می‌تواند <b>متن، عکس، ویدیو، فایل، ویس</b> و هر نوع دیگر باشد؛\n"
        "قالب‌بندی و ایموجی‌های پریمیوم حفظ می‌شود.\n\n"
        "نوع ارسال را انتخاب کن:"
    )


@router.callback_query(F.data == "admin:broadcast:menu")
async def broadcast_menu(callback: CallbackQuery, state: FSMContext):
    await reset_flow(state)
    shown = await page(callback, _menu_text(), _menu_markup())
    await remember_panel(state, shown)


@router.callback_query(F.data == "admin:broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await reset_flow(state)
    await toast(callback, "لغو شد.")
    shown = await page(callback, _menu_text(), _menu_markup(), answer=False)
    await remember_panel(state, shown)


# ============================================================
# شروع هر حالت
# ============================================================

@router.callback_query(F.data == "admin:broadcast:single")
async def start_single(callback: CallbackQuery, state: FSMContext):
    await reset_flow(state)
    await ask(
        callback, state, BroadcastStates.waiting_target,
        f"{title('telegram', '👤', 'ارسال به یک کاربر')}\n\n"
        "آیدی عددی یا یوزرنیم کاربر را بفرست:",
        "admin:broadcast:cancel", mode="single",
    )


@router.callback_query(F.data == "admin:broadcast:all")
async def start_all(callback: CallbackQuery, state: FSMContext):
    if _active["running"]:
        await toast(callback, "یک ارسال همگانی در حال انجام است.", True)
        return
    await reset_flow(state)
    await ask(
        callback, state, BroadcastStates.waiting_content,
        f"{title('world', '📢', 'ارسال به همه')}\n\n"
        "پیامی را که می‌خواهی برای همهٔ کاربران ارسال شود بفرست.\n"
        "<i>(برای آلبوم چندعکسی فقط اولین پیام ارسال می‌شود.)</i>",
        "admin:broadcast:cancel", mode="all",
    )


@router.callback_query(F.data == "admin:broadcast:link")
async def start_link(callback: CallbackQuery, state: FSMContext):
    if _active["running"]:
        await toast(callback, "یک ارسال همگانی در حال انجام است.", True)
        return
    await reset_flow(state)
    await ask(
        callback, state, BroadcastStates.waiting_link,
        f"{title('light', '🔗', 'ارسال از لینک کانال')}\n\n"
        "لینک پیام کانال را بفرست:\n<code>https://t.me/channel/123</code>\n\n"
        "ربات باید به آن کانال دسترسی داشته باشد.",
        "admin:broadcast:cancel", mode="link",
    )


# ============================================================
# دریافت ورودی‌ها
# ============================================================

@router.message(BroadcastStates.waiting_target)
async def got_target(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ ورودی خالی است.")
        return
    target = get_user_by_identifier(raw)
    if not target:
        await message.answer("❌ کاربر یافت نشد. دوباره بفرست.")
        return
    await state.update_data(target_id=int(target["telegram_id"]))
    await state.set_state(BroadcastStates.waiting_content)
    await refresh_panel(
        bot, state, message,
        f"{title('ask', '✍️', 'پیام را بفرست')}\n\n"
        f"گیرنده: <code>{int(target['telegram_id'])}</code>"
        + (f" (@{esc(target['username'])})" if target.get("username") else ""),
        cancel_kb("admin:broadcast:cancel"),
    )


def _parse_link(link: str) -> Optional[tuple]:
    link = link.strip()
    if not link.startswith("https://t.me/"):
        return None
    parts = [p for p in link.replace("https://t.me/", "").split("?")[0].split("/") if p]
    try:
        if parts[0] == "c":
            return int(f"-100{int(parts[1])}"), int(parts[2])
        return f"@{parts[0]}", int(parts[1])
    except (IndexError, ValueError):
        return None


@router.message(BroadcastStates.waiting_link)
async def got_link(message: Message, state: FSMContext, bot: Bot):
    parsed = _parse_link(message.text or "")
    if not parsed:
        await message.answer("❌ لینک نامعتبر است. نمونه: https://t.me/channel/123")
        return
    chat_id, msg_id = parsed
    # پیش‌نمایش: یک کپی برای خود ادمین؛ هم اعتبار لینک چک می‌شود هم محتوا دیده می‌شود
    try:
        await bot.copy_message(message.chat.id, chat_id, msg_id)
    except Exception as exc:
        await message.answer(f"❌ دسترسی به آن پیام ممکن نیست: {esc(exc)}")
        return
    await state.update_data(src_chat=chat_id, src_msg=msg_id)
    await _show_confirm(bot, state, message)


@router.message(BroadcastStates.waiting_content)
async def got_content(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    mode = data.get("mode")

    if mode == "single":
        target_id = data.get("target_id")
        if not target_id:
            await message.answer("❌ گیرنده مشخص نیست.")
            await reset_flow(state)
            return
        if is_banned(int(target_id)):
            await refresh_panel(bot, state, message,
                f"{title('false', '⛔', 'این کاربر بن است؛ پیام ارسال نشد.')}",
                menu([], back="admin:broadcast:menu"), delete_source=False)
            await reset_flow(state)
            return
        try:
            await bot.copy_message(int(target_id), message.chat.id, message.message_id)
            log_event("INFO", "broadcast_single", message.from_user.id, f"target={target_id}")
            text = title("true", "✅", "پیام ارسال شد.")
        except TelegramForbiddenError:
            text = title("false", "❌", "کاربر ربات را بلاک کرده است.")
        except Exception as exc:
            logger.exception("Broadcast single failed")
            text = f"{title('false', '❌', 'خطا در ارسال')}\n<code>{esc(exc)}</code>"
        await reset_flow(state)
        await refresh_panel(bot, state, message, text,
                            menu([], back="admin:broadcast:menu"), delete_source=False)
        return

    if mode == "all":
        await state.update_data(src_chat=message.chat.id, src_msg=message.message_id)
        await _show_confirm(bot, state, message)
        return

    await message.answer("❌ حالت ارسال نامشخص است.")
    await reset_flow(state)


# ============================================================
# تأیید ارسال همگانی
# ============================================================

async def _show_confirm(bot: Bot, state: FSMContext, source: Message) -> None:
    total = len(get_all_user_ids())
    await state.set_state(BroadcastStates.confirm)
    await refresh_panel(
        bot, state, source,
        f"{title('warning', '⚠️', 'تأیید ارسال همگانی')}\n{LINE}\n\n"
        f"پیام بالا برای <b>{total}</b> کاربر (بدون بن‌شده‌ها) ارسال می‌شود.\n"
        "این کار قابل بازگشت نیست. ادامه می‌دهی؟",
        menu([btn("تأیید و ارسال", "admin:broadcast:go", style=GREEN, emoji="true")],
             back="admin:broadcast:cancel", back_text="انصراف"),
        delete_source=False,
    )


@router.callback_query(F.data == "admin:broadcast:go", BroadcastStates.confirm)
async def confirm_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if _active["running"]:
        await toast(callback, "یک ارسال همگانی در حال انجام است.", True)
        return
    data = await state.get_data()
    src_chat, src_msg = data.get("src_chat"), data.get("src_msg")
    if src_chat is None or src_msg is None:
        await toast(callback, "پیام منبع پیدا نشد؛ از ابتدا شروع کن.", True)
        await reset_flow(state)
        return

    admin_id = callback.from_user.id
    panel = callback.message
    await reset_flow(state)
    await toast(callback, "ارسال شروع شد.")

    _active["running"], _active["stop"] = True, False
    task = asyncio.create_task(_run_broadcast(bot, admin_id, panel, src_chat, src_msg))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(F.data == "admin:broadcast:stop")
async def stop_send(callback: CallbackQuery):
    if _active["running"]:
        _active["stop"] = True
        await toast(callback, "در حال توقف...")
    else:
        await toast(callback, "ارسالی در حال انجام نیست.")


# ============================================================
# ارسال در پس‌زمینه
# ============================================================

def _progress_text(done: int, total: int, ok: int, blocked: int, fail: int, finished: bool, stopped: bool) -> str:
    percent = int(done * 100 / total) if total else 100
    head = (title("true", "✅", "ارسال همگانی تمام شد") if finished and not stopped
            else title("warning", "⏹", "ارسال متوقف شد") if stopped
            else title("light", "📤", "در حال ارسال..."))
    return (
        f"{head}\n{LINE}\n\n"
        f"پیشرفت: <b>{done}/{total}</b> ({percent}%)\n"
        f"{em('true', '✅')} موفق: <b>{ok}</b>\n"
        f"{em('false', '🚫')} بلاک‌کرده: <b>{blocked}</b>\n"
        f"{em('warning', '❌')} ناموفق: <b>{fail}</b>"
    )


async def _edit(panel: Message, text: str, markup=None) -> None:
    try:
        await panel.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    except Exception:
        logger.debug("progress edit failed", exc_info=True)


async def _run_broadcast(bot: Bot, admin_id: int, panel: Message, src_chat, src_msg) -> None:
    ids = get_all_user_ids()
    total = len(ids)
    ok = blocked = fail = done = 0
    stop_kb = menu([btn("توقف ارسال", "admin:broadcast:stop", style=RED, emoji="false")], back=None)
    stopped = False
    last_edit = 0.0
    try:
        await _edit(panel, _progress_text(0, total, 0, 0, 0, False, False), stop_kb)
        for uid in ids:
            if _active["stop"]:
                stopped = True
                break
            for attempt in range(3):
                try:
                    await bot.copy_message(uid, src_chat, src_msg)
                    ok += 1
                    break
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 1)
                    continue  # همان کاربر دوباره
                except TelegramForbiddenError:
                    blocked += 1
                    break
                except Exception:
                    fail += 1
                    break
            else:
                fail += 1  # سه بار flood-wait
            done += 1
            await asyncio.sleep(SEND_DELAY)
            now = time.monotonic()
            if done % PROGRESS_EVERY == 0 and now - last_edit > 2:
                last_edit = now
                await _edit(panel, _progress_text(done, total, ok, blocked, fail, False, False), stop_kb)
    except Exception:
        logger.exception("Broadcast crashed")
    finally:
        _active["running"], _active["stop"] = False, False
        log_event("INFO", "broadcast_all", admin_id,
                  f"ok={ok}, blocked={blocked}, fail={fail}, total={total}, stopped={stopped}")
        await _edit(panel, _progress_text(done, total, ok, blocked, fail, True, stopped),
                    menu([], back="admin:broadcast:menu"))
