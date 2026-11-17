import logging
import re
import time
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import (
    get_connection, get_subscription, get_subscription_by_config_name,
    update_subscription_volume, update_subscription_state,
    hard_delete_subscription, get_user, log_event,
)
from paxiva import PaxivaClient, PaxivaError
from rich import premium_emoji
from utils import validate_config_name, format_bytes, format_days_remaining

logger = logging.getLogger(__name__)
router = Router(name="admin_services")

GIB = 1024 ** 3
STATE_FA = {
    "active": f"{premium_emoji('true', '✅')} فعال",
    "expired": f"{premium_emoji('light', '⌛')} منقضی",
    "depleted": f"{premium_emoji('warning', '📉')} حجم تمام شده",
    "deleted": f"{premium_emoji('false', '🗑')} حذف شده",
    "paused": f"{premium_emoji('light', '⏸')} متوقف",
    "disabled": f"{premium_emoji('false', '⛔')} غیرفعال",
}


class ServiceStates(StatesGroup):
    waiting_query = State()
    waiting_volume = State()
    waiting_days = State()


# ============================================================
# HELPERS
# ============================================================

def _emoji_id(key: str) -> str | None:
    if not PREMIUM_EMOJI_ENABLED:
        return None
    return PREMIUM_EMOJIS.get(key)


def _button(text: str, callback_data: str, style: str = "primary", emoji_key: str = None) -> InlineKeyboardButton:
    kwargs = {"text": text, "callback_data": callback_data, "style": style}
    eid = _emoji_id(emoji_key) if emoji_key else None
    if eid:
        kwargs["icon_custom_emoji_id"] = eid
    return InlineKeyboardButton(**kwargs)


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_answer(message, text, markup=None):
    try:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        logger.exception("Could not send services message")


def parse_int(value: str, min_val: int, max_val: int) -> Optional[int]:
    try:
        val = int(value.strip())
        if not (min_val <= val <= max_val):
            return None
        return val
    except (ValueError, AttributeError):
        return None


def fmt_date(ts) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts)))
    except Exception:
        return "—"


def bar(percent: float, length: int = 10) -> str:
    filled = int((percent / 100) * length)
    return "█" * filled + "░" * (length - filled)


# ============================================================
# SEARCH
# ============================================================

def _find_by_link(text: str) -> Optional[dict]:
    text = text.strip()
    token = text.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE link = ?", (text,)).fetchone()
        if not row and len(token) >= 8 and re.fullmatch(r"[A-Za-z0-9_\-]+", token):
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE link LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 1",
                (f"%/{token.replace('_', chr(92) + '_')}%",),
            ).fetchone()
        return dict(row) if row else None


def find_subscription(query: str) -> Optional[dict]:
    query = (query or "").strip()
    if not query:
        return None
    if validate_config_name(query):
        return get_subscription_by_config_name(query)
    if query.lower().startswith(("http://", "https://")):
        return _find_by_link(query)
    return None


# ============================================================
# RENDER SERVICE DETAILS
# ============================================================

async def _live(paxiva: PaxivaClient, sub: dict) -> tuple[Optional[dict], bool]:
    if not sub.get("paxiva_client_id"):
        return None, False
    try:
        return await paxiva.get_client(int(sub["paxiva_client_id"])), True
    except PaxivaError as error:
        logger.warning("panel lookup failed for sub %s: %s", sub["id"], getattr(error, "message", error))
    except Exception:
        logger.exception("panel lookup crashed for sub %s", sub["id"])
    return None, False


async def render_service(sub_id: int, paxiva: PaxivaClient):
    sub = get_subscription(sub_id)
    if not sub:
        return None

    remote, is_live = await _live(paxiva, sub)
    src = remote or {}

    used = int(src.get("used_bytes") if src.get("used_bytes") is not None else (sub.get("used_bytes") or 0))
    volume_gb = float(src.get("volume_gb") if src.get("volume_gb") is not None else (sub.get("volume_gb") or 0))
    expires_at = src.get("expires_at") if remote is not None else sub.get("expires_at")
    state_key = src.get("state") or sub.get("state") or "active"

    if remote is not None:
        try:
            update_subscription_state(sub["id"], state_key, expires_at, used)
        except Exception:
            logger.exception("sync failed for sub %s", sub["id"])

    if volume_gb > 0:
        total = int(volume_gb * GIB)
        percent = min(100.0, used * 100 / total) if total else 0
        usage = (
            f"{bar(percent)} <b>{percent:.0f}%</b>\n"
            f"مصرف: <b>{format_bytes(used)}</b> از <b>{volume_gb:g} گیگ</b>\n"
            f"باقی‌مانده: <b>{format_bytes(max(0, total - used))}</b>"
        )
    else:
        usage = f"مصرف: <b>{format_bytes(used)}</b> (حجم نامحدود)"

    owner = get_user(sub["telegram_id"]) or {}
    owner_name = f"@{owner['username']}" if owner.get("username") else (owner.get("first_name") or "—")
    kind_fa = "نامحدود" if sub.get("kind") == "unl" else "حجمی"
    expiry = "بدون محدودیت زمانی" if not expires_at else f"{format_days_remaining(expires_at)} ({fmt_date(expires_at)})"
    link = src.get("link") or sub.get("link")
    live_note = "" if is_live else f"\n{premium_emoji('warning', '⚠️')} <i>اتصال به پنل برقرار نشد؛ اطلاعات از دیتابیس است.</i>"

    text = (
        f"{premium_emoji('world', '🌍')} <b>سرویس</b> <code>{sub['config_name']}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{premium_emoji('telegram', '👤')} مالک: <code>{sub['telegram_id']}</code> ({owner_name})\n"
        f"{premium_emoji('light', '📌')} وضعیت: <b>{STATE_FA.get(state_key, state_key)}</b>\n"
        f"{premium_emoji('shop', '🧩')} نوع: <b>{kind_fa}</b> | دستگاه: <b>{int(sub.get('devices') or 0)}</b>\n"
        f"{premium_emoji('light', '⏳')} انقضا: <b>{expiry}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"{usage}\n"
        "━━━━━━━━━━━━━━\n"
        f"{premium_emoji('wallet', '💰')} قیمت خرید: <b>{sub.get('price_toman') or 0:,}</b> تومان / <b>{sub.get('price_stars') or 0:,}</b> {premium_emoji('stars', '⭐')}\n"
        f"{premium_emoji('light', '📅')} ساخته‌شده: {fmt_date(sub.get('created_at'))}"
        f"{live_note}"
    )
    if link:
        text += f"\n\n{premium_emoji('safe', '🔗')} <code>{link}</code>"

    markup = _keyboard([
        [_button("بروزرسانی", f"admin:services:open:{sub_id}", "primary", "light")],
        [_button("پروفایل مالک", f"admin:users:view:{sub['telegram_id']}", "success", "telegram")],
        [_button("افزودن روز", f"admin:services:days:{sub_id}", "primary", "blue_dot")],
        [_button("ویرایش حجم", f"admin:services:volume:{sub_id}", "success", "safe")],
        [_button("ریست مصرف", f"admin:services:reset:{sub_id}", "primary", "dice")],
        [_button("حذف سرویس", f"admin:services:delask:{sub_id}", "danger", "warning")],
        [_button("بازگشت", "admin:services:menu", "danger", "back")],
    ])
    return text, markup


@router.callback_query(F.data == "admin:services:menu")
async def services_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ServiceStates.waiting_query)
    text = (
        f"{premium_emoji('world', '🌍')} <b>مدیریت سرویس‌ها</b>\n"
        "━━━━━━━━━━━━━━\n"
        "نام کانفیگ یا لینک ساب را بفرست.\n\n"
        "مثال‌ها:\n"
        "<code>iMan-35</code>\n"
        "<code>https://panel.example.com/s/9f2c4e1a…</code>"
    )
    markup = _keyboard([[_button("انصراف", "admin:main", "danger", "back")]])
    await _safe_answer(callback.message, text, markup)
    await callback.answer()


@router.message(ServiceStates.waiting_query, F.text)
async def query_received(message: Message, state: FSMContext, bot: Bot, paxiva: PaxivaClient):
    sub = find_subscription(message.text)
    if not sub:
        try:
            await message.delete()
        except Exception:
            pass
        text = (
            f"{premium_emoji('warning', '⚠️')} <b>سرویسی پیدا نشد.</b>\n"
            "نام کانفیگ (مثل <code>iMan-35</code>) یا لینک کامل ساب را دوباره بفرست."
        )
        markup = _keyboard([[_button("انصراف", "admin:main", "danger", "back")]])
        await _safe_answer(message, text, markup)
        return
    await state.clear()
    view = await render_service(sub["id"], paxiva)
    try:
        await message.delete()
    except Exception:
        pass
    if view:
        await _safe_answer(message, *view)


@router.callback_query(F.data.startswith("admin:services:open:"))
async def open_service(callback: CallbackQuery, state: FSMContext, paxiva: PaxivaClient):
    await state.clear()
    await callback.answer()
    view = await render_service(int(callback.data.split(":")[3]), paxiva)
    if not view:
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    await _safe_answer(callback.message, *view)


# ============================================================
# RESET USAGE
# ============================================================

@router.callback_query(F.data.startswith("admin:services:reset:"))
async def reset_usage(callback: CallbackQuery, paxiva: PaxivaClient):
    sub_id = int(callback.data.split(":")[3])
    sub = get_subscription(sub_id)
    if not sub or not sub.get("paxiva_client_id"):
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    try:
        await paxiva.reset_client_traffic(int(sub["paxiva_client_id"]))
        update_subscription_state(sub_id, sub.get("state") or "active", None, 0)
        log_event("INFO", "admin_reset_usage", callback.from_user.id, f"sub_id={sub_id}")
        await callback.answer("مصرف صفر شد.")
    except PaxivaError as error:
        await callback.answer(f"خطا در پنل: {error.message}", show_alert=True)
        return
    view = await render_service(sub_id, paxiva)
    if view:
        await _safe_answer(callback.message, *view)


# ============================================================
# ADD DAYS / EDIT VOLUME
# ============================================================

@router.callback_query(F.data.startswith("admin:services:days:"))
async def days_ask(callback: CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split(":")[3])
    await state.set_state(ServiceStates.waiting_days)
    await state.update_data(sub_id=sub_id)
    text = (
        f"{premium_emoji('light', '📅')} <b>افزودن روز</b>\n"
        "━━━━━━━━━━━━━━\n"
        "تعداد روزی که به اعتبار سرویس <b>اضافه</b> شود را بفرست (۱ تا ۳۶۵۰)."
    )
    markup = _keyboard([[_button("انصراف", f"admin:services:open:{sub_id}", "danger", "back")]])
    await _safe_answer(callback.message, text, markup)
    await callback.answer()


@router.message(ServiceStates.waiting_days, F.text)
async def days_received(message: Message, state: FSMContext, bot: Bot, paxiva: PaxivaClient):
    days = parse_int(message.text, 1, 3650)
    if days is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    data = await state.get_data()
    sub = get_subscription(data.get("sub_id"))
    if not sub or not sub.get("paxiva_client_id"):
        await state.clear()
        await _safe_answer(message, "❌ سرویس یافت نشد.", _keyboard([[_button("بازگشت", "admin:services:menu", "danger", "back")]]))
        return
    try:
        await paxiva.renew_volume_client(int(sub["paxiva_client_id"]), days, f"admin-days-{sub['id']}-{int(time.time())}")
        log_event("INFO", "admin_add_days", message.from_user.id, f"sub_id={sub['id']}, days={days}")
        notice = f"{premium_emoji('true', '✅')} <b>{days} روز به سرویس اضافه شد.</b>\n\n"
    except PaxivaError as error:
        notice = f"{premium_emoji('false', '❌')} <b>خطا در پنل:</b> {error.message}\n\n"
    await state.clear()
    view = await render_service(sub["id"], paxiva)
    try:
        await message.delete()
    except Exception:
        pass
    if view:
        await _safe_answer(message, notice + view[0], view[1])


@router.callback_query(F.data.startswith("admin:services:volume:"))
async def volume_ask(callback: CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split(":")[3])
    await state.set_state(ServiceStates.waiting_volume)
    await state.update_data(sub_id=sub_id)
    text = (
        f"{premium_emoji('safe', '📦')} <b>ویرایش حجم</b>\n"
        "━━━━━━━━━━━━━━\n"
        "حجم جدید را به گیگابایت بفرست (۰ تا ۱۰۰۰۰)."
    )
    markup = _keyboard([[_button("انصراف", f"admin:services:open:{sub_id}", "danger", "back")]])
    await _safe_answer(callback.message, text, markup)
    await callback.answer()


@router.message(ServiceStates.waiting_volume, F.text)
async def volume_received(message: Message, state: FSMContext, bot: Bot, paxiva: PaxivaClient):
    try:
        volume = float(message.text.strip().replace(",", "."))
        if volume < 0 or volume > 10000:
            raise ValueError
    except (ValueError, AttributeError):
        try:
            await message.delete()
        except Exception:
            pass
        return
    data = await state.get_data()
    sub = get_subscription(data.get("sub_id"))
    if not sub or not sub.get("paxiva_client_id"):
        await state.clear()
        await _safe_answer(message, "❌ سرویس یافت نشد.", _keyboard([[_button("بازگشت", "admin:services:menu", "danger", "back")]]))
        return
    try:
        await paxiva.update_client_volume(int(sub["paxiva_client_id"]), volume)
        update_subscription_volume(sub["id"], volume)
        log_event("INFO", "admin_edit_volume", message.from_user.id, f"sub_id={sub['id']}, new_vol={volume}")
        notice = f"{premium_emoji('true', '✅')} <b>حجم سرویس به {volume:g} گیگ تغییر کرد.</b>\n\n"
    except PaxivaError as error:
        notice = f"{premium_emoji('false', '❌')} <b>خطا در پنل:</b> {error.message}\n\n"
    await state.clear()
    view = await render_service(sub["id"], paxiva)
    try:
        await message.delete()
    except Exception:
        pass
    if view:
        await _safe_answer(message, notice + view[0], view[1])


# ============================================================
# DELETE SERVICE
# ============================================================

@router.callback_query(F.data.startswith("admin:services:delask:"))
async def delete_ask(callback: CallbackQuery):
    sub_id = int(callback.data.split(":")[3])
    sub = get_subscription(sub_id)
    if not sub:
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    text = (
        f"{premium_emoji('warning', '⚠️')} <b>سرویس <code>{sub['config_name']}</code> حذف شود؟</b>\n"
        "━━━━━━━━━━━━━━\n"
        "سرویس از پنل و دیتابیس پاک می‌شود و قابل بازگشت نیست."
    )
    markup = _keyboard([
        [_button("بله، حذف کن", f"admin:services:delete:{sub_id}", "danger", "false")],
        [_button("نه، انصراف", f"admin:services:open:{sub_id}", "success", "true")],
    ])
    await _safe_answer(callback.message, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:services:delete:"))
async def delete_service(callback: CallbackQuery, paxiva: PaxivaClient):
    sub_id = int(callback.data.split(":")[3])
    sub = get_subscription(sub_id)
    if not sub:
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    try:
        if sub.get("paxiva_client_id"):
            await paxiva.delete_client(int(sub["paxiva_client_id"]), purge=True)
    except PaxivaError as error:
        if getattr(error, "status_code", None) != 404:
            text = f"{premium_emoji('false', '❌')} <b>حذف از پنل ناموفق بود:</b> {error.message}\nسرویس در دیتابیس باقی ماند."
            markup = _keyboard([[_button("بازگشت", f"admin:services:open:{sub_id}", "danger", "back")]])
            await _safe_answer(callback.message, text, markup)
            return
    hard_delete_subscription(sub_id)
    log_event("INFO", "admin_service_deleted", callback.from_user.id, f"sub_id={sub_id}; name={sub['config_name']}")
    text = f"{premium_emoji('true', '✅')} <b>سرویس <code>{sub['config_name']}</code> حذف شد.</b>"
    markup = _keyboard([[_button("جستجوی سرویس دیگر", "admin:services:menu", "success", "world")]])
    await _safe_answer(callback.message, text, markup)