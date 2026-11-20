import html
import logging
import math
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
    get_subscription_product,
)
from dev.alerts import send_alert
from pasar_guard import (
    PanelRegistry,
    PasarGuardCircuitOpen,
    PasarGuardError,
    get_live,
    is_definite_rejection,
    is_not_found,
    renew_days,
    reset_usage as panel_reset_usage,
    set_volume as panel_set_volume,
    delete_subscription as panel_delete_subscription,
)
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
    percent = max(0.0, min(100.0, float(percent)))
    filled = max(0, min(length, int((percent / 100) * length)))
    return "█" * filled + "░" * (length - filled)


def _safe_html(value) -> str:
    """Escape dynamic values before inserting them into Telegram HTML."""
    return html.escape(str(value if value is not None else ""), quote=True)


def _panel_error_notice(error: BaseException) -> str:
    """متن فارسی و امن برای خطای پنل (جزئیات فنی هرگز نمایش داده نمی‌شود)."""
    if isinstance(error, PasarGuardCircuitOpen):
        return "پنل موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید."
    if is_not_found(error):
        return "سرویس در پنل پیدا نشد."
    if is_definite_rejection(error):
        return "پنل درخواست را نپذیرفت."
    return "نتیجهٔ عملیات در پنل نامشخص است؛ با «بروزرسانی» وضعیت را بررسی کنید."


UNKNOWN_RESULT_NOTICE = "نتیجهٔ عملیات نامشخص است؛ با «بروزرسانی» وضعیت را بررسی کنید."


def _report_panel_error(action: str, error: BaseException, admin_id: int, sub_id) -> None:
    """هشدار دولوپر برای خطای عملیات ادمین روی پنل."""
    try:
        send_alert(
            "WARNING" if isinstance(error, PasarGuardError) and is_definite_rejection(error) else "ERROR",
            f"خطا در عملیات ادمین روی پنل ({action})",
            exc=error,
            user_id=admin_id,
            sub_id=sub_id,
            status=getattr(error, "status", None),
        )
    except Exception:
        logger.exception("Could not send panel error alert.")


def _callback_id(callback: CallbackQuery, prefix: str) -> Optional[int]:
    try:
        if not callback.data.startswith(prefix):
            return None
        value = int(callback.data[len(prefix):])
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


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

async def _live(panel_registry: Optional[PanelRegistry], sub: dict) -> tuple[Optional[dict], bool]:
    if not sub.get("panel_id") or not sub.get("config_name"):
        return None, False
    try:
        return await get_live(sub), True
    except PasarGuardError as error:
        logger.warning("panel lookup failed for sub %s: status=%s", sub["id"], getattr(error, "status", None))
    except Exception:
        logger.exception("panel lookup crashed for sub %s", sub["id"])
    return None, False


async def render_service(sub_id: int, panel_registry: Optional[PanelRegistry] = None):
    sub = get_subscription(int(sub_id))
    if not sub:
        return None

    remote, is_live = await _live(panel_registry, sub)
    src = remote or {}

    used_raw = src.get("used_bytes")
    used = max(0, int(used_raw if used_raw is not None else (sub.get("used_bytes") or 0)))

    volume_raw = src.get("volume_gb")
    volume_gb = float(volume_raw if volume_raw is not None else (sub.get("volume_gb") or 0))
    volume_gb = max(0.0, volume_gb)

    expires_at = src.get("expires_at") if remote is not None else sub.get("expires_at")
    state_key = str(src.get("state") or sub.get("state") or "active").lower()

    # Synchronize the latest panel values with the database.
    if remote is not None:
        try:
            update_subscription_state(sub["id"], state_key, expires_at, used)
            if volume_raw is not None:
                update_subscription_volume(sub["id"], volume_gb)
            sub = get_subscription(sub["id"]) or sub
        except Exception:
            logger.exception("subscription sync failed for sub %s", sub["id"])

    if volume_gb > 0:
        total = int(volume_gb * GIB)
        remaining = max(0, total - used)
        percent = min(100.0, used * 100 / total) if total else 0.0
        usage = (
            f"{bar(percent)} <b>{percent:.0f}%</b>\n"
            f"مصرف: <b>{_safe_html(format_bytes(used))}</b> از <b>{volume_gb:g} گیگ</b>\n"
            f"باقی‌مانده: <b>{_safe_html(format_bytes(remaining))}</b>"
        )
    else:
        usage = f"مصرف: <b>{_safe_html(format_bytes(used))}</b> (حجم نامحدود)"

    owner = get_user(sub["telegram_id"]) or {}
    if owner.get("username"):
        owner_name = "@" + _safe_html(owner["username"])
    else:
        owner_name = _safe_html(owner.get("first_name") or "—")

    kind = str(sub.get("kind") or "vol").lower()
    kind_fa = "نامحدود" if kind == "unl" else "حجمی"
    expiry = (
        "بدون محدودیت زمانی"
        if not expires_at
        else f"{_safe_html(format_days_remaining(expires_at))} ({fmt_date(expires_at)})"
    )
    link = src.get("link") or sub.get("link")
    live_note = "" if is_live else (
        f"\n{premium_emoji('warning', '⚠️')} "
        "<i>اتصال به پنل برقرار نشد؛ اطلاعات از دیتابیس نمایش داده می‌شود.</i>"
    )

    auto_renew = bool(sub.get("auto_renew"))
    auto_renew_text = (
        f"{premium_emoji('true', '✅')} روشن"
        if auto_renew else f"{premium_emoji('light', '⏸')} خاموش"
    )

    product = None
    try:
        product = get_subscription_product(sub["id"])
    except Exception:
        logger.exception("could not load linked product for sub %s", sub["id"])

    if product:
        product_name = product.get("name") or product.get("title") or f"#{product['id']}"
        product_text = (
            f"محصول پایه: <b>{_safe_html(product_name)}</b> "
            f"(قیمت فعلی: <b>{int(product.get('price_toman') or 0):,}</b> تومان)"
        )
    else:
        product_text = "محصول پایه: <i>به این سرویس متصل نشده</i>"

    text = (
        f"{premium_emoji('world', '🌍')} <b>سرویس</b> <code>{_safe_html(sub.get('config_name'))}</code>\n"
        "━━━━━━━━━━━━━━\n"
        f"{premium_emoji('telegram', '👤')} مالک: <code>{int(sub['telegram_id'])}</code> ({owner_name})\n"
        f"{premium_emoji('light', '📌')} وضعیت: <b>{_safe_html(STATE_FA.get(state_key, state_key))}</b>\n"
        f"{premium_emoji('shop', '🧩')} نوع: <b>{kind_fa}</b> | دستگاه: <b>{int(sub.get('devices') or 0)}</b>\n"
        f"{premium_emoji('light', '⏳')} انقضا: <b>{expiry}</b>\n"
        f"تمدید خودکار: <b>{auto_renew_text}</b>\n"
        f"{product_text}\n"
        "━━━━━━━━━━━━━━\n"
        f"{usage}\n"
        "━━━━━━━━━━━━━━\n"
        f"{premium_emoji('wallet', '💰')} قیمت خرید: "
        f"<b>{int(sub.get('price_toman') or 0):,}</b> تومان / "
        f"<b>{int(sub.get('price_stars') or 0):,}</b> {premium_emoji('stars', '⭐')}\n"
        f"{premium_emoji('light', '📅')} ساخته‌شده: {fmt_date(sub.get('created_at'))}"
        f"{live_note}"
    )
    if link:
        text += f"\n\n{premium_emoji('safe', '🔗')} <code>{_safe_html(link)}</code>"

    rows = [
        [_button("بروزرسانی", f"admin:services:open:{sub_id}", "primary", "light")],
        [_button("پروفایل مالک", f"admin:users:view:{sub['telegram_id']}", "success", "telegram")],
    ]
    # Vol services have no time limit; only time-based (unl) services can be extended by days.
    if kind == "unl":
        rows.append([_button("افزودن روز", f"admin:services:days:{sub_id}", "primary", "blue_dot")])
    rows.extend([
        [_button("ویرایش حجم", f"admin:services:volume:{sub_id}", "success", "safe")],
        [_button("ریست مصرف", f"admin:services:reset:{sub_id}", "primary", "dice")],
        [_button("حذف سرویس", f"admin:services:delask:{sub_id}", "danger", "warning")],
        [_button("بازگشت", "admin:services:menu", "danger", "back")],
    ])
    return text, _keyboard(rows)


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
async def query_received(message: Message, state: FSMContext, bot: Bot, panel_registry: Optional[PanelRegistry] = None):
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
    view = await render_service(sub["id"], panel_registry)
    try:
        await message.delete()
    except Exception:
        pass
    if view:
        await _safe_answer(message, *view)


@router.callback_query(F.data.startswith("admin:services:open:"))
async def open_service(callback: CallbackQuery, state: FSMContext, panel_registry: Optional[PanelRegistry] = None):
    await state.clear()
    sub_id = _callback_id(callback, "admin:services:open:")
    if sub_id is None:
        await callback.answer("شناسه سرویس نامعتبر است.", show_alert=True)
        return
    view = await render_service(sub_id, panel_registry)
    if not view:
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    await callback.answer()
    await _safe_answer(callback.message, *view)


# ============================================================
# RESET USAGE
# ============================================================

@router.callback_query(F.data.startswith("admin:services:reset:"))
async def reset_usage(callback: CallbackQuery, panel_registry: Optional[PanelRegistry] = None):
    sub_id = int(callback.data.split(":")[3])
    sub = get_subscription(sub_id)
    if not sub or not sub.get("panel_id") or not sub.get("config_name"):
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    try:
        await panel_reset_usage(sub)
        update_subscription_state(sub_id, sub.get("state") or "active", None, 0)
        log_event("INFO", "admin_reset_usage", callback.from_user.id, f"sub_id={sub_id}")
        await callback.answer("مصرف صفر شد.")
    except PasarGuardError as error:
        logger.warning("admin reset usage failed: sub_id=%s status=%s", sub_id, getattr(error, "status", None))
        _report_panel_error("reset_usage", error, callback.from_user.id, sub_id)
        await callback.answer(f"خطا در پنل: {_panel_error_notice(error)}", show_alert=True)
        return
    except Exception as error:
        logger.exception("admin reset usage crashed: sub_id=%s", sub_id)
        _report_panel_error("reset_usage", error, callback.from_user.id, sub_id)
        await callback.answer(f"خطا: {UNKNOWN_RESULT_NOTICE}", show_alert=True)
        return
    view = await render_service(sub_id, panel_registry)
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
async def days_received(message: Message, state: FSMContext, bot: Bot, panel_registry: Optional[PanelRegistry] = None):
    days = parse_int(message.text, 1, 3650)
    if days is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    data = await state.get_data()
    sub = get_subscription(data.get("sub_id"))
    if not sub or not sub.get("panel_id") or not sub.get("config_name"):
        await state.clear()
        await _safe_answer(message, "❌ سرویس یافت نشد.", _keyboard([[_button("بازگشت", "admin:services:menu", "danger", "back")]]))
        return
    if str(sub.get("kind") or "").lower() != "unl":
        await state.clear()
        await _safe_answer(message, "افزودن روز فقط برای سرویس‌های زمان‌دار (نامحدود) قابل انجام است.")
        return
    try:
        await renew_days(
            sub,
            days,
            f"admin-days-{sub['id']}-{int(time.time())}",
        )
        log_event("INFO", "admin_add_days", message.from_user.id, f"sub_id={sub['id']}, days={days}")
        notice = f"{premium_emoji('true', '✅')} <b>{days} روز به سرویس اضافه شد.</b>\n\n"
    except PasarGuardError as error:
        logger.warning("admin add days failed: sub_id=%s status=%s", sub["id"], getattr(error, "status", None))
        _report_panel_error("add_days", error, message.from_user.id, sub["id"])
        notice = f"{premium_emoji('false', '❌')} <b>خطا در پنل:</b> {_panel_error_notice(error)}\n\n"
    except Exception as error:
        logger.exception("admin add days crashed: sub_id=%s", sub["id"])
        _report_panel_error("add_days", error, message.from_user.id, sub["id"])
        notice = f"{premium_emoji('false', '❌')} <b>خطا:</b> {UNKNOWN_RESULT_NOTICE}\n\n"
    await state.clear()
    view = await render_service(sub["id"], panel_registry)
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
        "حجم جدید را به گیگابایت بفرست (۰ تا ۱۰۰۰۰). مقدار صفر فقط برای سرویس نامحدود مجاز است."
    )
    markup = _keyboard([[_button("انصراف", f"admin:services:open:{sub_id}", "danger", "back")]])
    await _safe_answer(callback.message, text, markup)
    await callback.answer()


@router.message(ServiceStates.waiting_volume, F.text)
async def volume_received(message: Message, state: FSMContext, bot: Bot, panel_registry: Optional[PanelRegistry] = None):
    try:
        volume = float(message.text.strip().replace(",", "."))
        if not math.isfinite(volume) or volume < 0 or volume > 10000:
            raise ValueError
    except (ValueError, AttributeError):
        try:
            await message.delete()
        except Exception:
            pass
        return
    data = await state.get_data()
    sub = get_subscription(data.get("sub_id"))
    if not sub or not sub.get("panel_id") or not sub.get("config_name"):
        await state.clear()
        await _safe_answer(message, "❌ سرویس یافت نشد.", _keyboard([[_button("بازگشت", "admin:services:menu", "danger", "back")]]))
        return
    if str(sub.get("kind") or "").lower() == "vol" and volume <= 0:
        await _safe_answer(message, "❌ سرویس حجمی باید حجم بیشتر از صفر داشته باشد.")
        return
    try:
        await panel_set_volume(sub, volume)
        update_subscription_volume(sub["id"], volume)
        log_event("INFO", "admin_edit_volume", message.from_user.id, f"sub_id={sub['id']}, new_vol={volume}")
        notice = f"{premium_emoji('true', '✅')} <b>حجم سرویس به {volume:g} گیگ تغییر کرد.</b>\n\n"
    except PasarGuardError as error:
        logger.warning("admin edit volume failed: sub_id=%s status=%s", sub["id"], getattr(error, "status", None))
        _report_panel_error("edit_volume", error, message.from_user.id, sub["id"])
        notice = f"{premium_emoji('false', '❌')} <b>خطا در پنل:</b> {_panel_error_notice(error)}\n\n"
    except Exception as error:
        logger.exception("admin edit volume crashed: sub_id=%s", sub["id"])
        _report_panel_error("edit_volume", error, message.from_user.id, sub["id"])
        notice = f"{premium_emoji('false', '❌')} <b>خطا:</b> {UNKNOWN_RESULT_NOTICE}\n\n"
    await state.clear()
    view = await render_service(sub["id"], panel_registry)
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
        f"{premium_emoji('warning', '⚠️')} <b>سرویس <code>{_safe_html(sub['config_name'])}</code> حذف شود؟</b>\n"
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
async def delete_service(callback: CallbackQuery, panel_registry: Optional[PanelRegistry] = None):
    sub_id = int(callback.data.split(":")[3])
    sub = get_subscription(sub_id)
    if not sub:
        await callback.answer("سرویس یافت نشد.", show_alert=True)
        return
    had_panel = bool(sub.get("panel_id"))
    try:
        if had_panel:
            # نبودن کاربر در پنل (404) داخل delete_subscription موفق حساب می‌شود.
            await panel_delete_subscription(sub)
    except Exception as error:
        if isinstance(error, PasarGuardError):
            logger.warning("admin delete failed: sub_id=%s status=%s", sub_id, getattr(error, "status", None))
            reason = _panel_error_notice(error)
        else:
            logger.exception("admin delete crashed: sub_id=%s", sub_id)
            reason = UNKNOWN_RESULT_NOTICE
        _report_panel_error("delete", error, callback.from_user.id, sub_id)
        text = f"{premium_emoji('false', '❌')} <b>حذف از پنل ناموفق بود:</b> {reason}\nسرویس در دیتابیس باقی ماند."
        markup = _keyboard([[_button("بازگشت", f"admin:services:open:{sub_id}", "danger", "back")]])
        await _safe_answer(callback.message, text, markup)
        return
    hard_delete_subscription(sub_id)
    log_event("INFO", "admin_service_deleted", callback.from_user.id, f"sub_id={sub_id}; name={sub['config_name']}")
    text = f"{premium_emoji('true', '✅')} <b>سرویس <code>{_safe_html(sub['config_name'])}</code> حذف شد.</b>"
    if not had_panel:
        text += (
            f"\n{premium_emoji('warning', '⚠️')} <i>این سرویس به پنل متصل نبود "
            "(panel_id ندارد)؛ فقط از دیتابیس حذف شد. در صورت وجود در پنل، دستی حذفش کنید.</i>"
        )
    markup = _keyboard([[_button("جستجوی سرویس دیگر", "admin:services:menu", "success", "world")]])
    await _safe_answer(callback.message, text, markup)
