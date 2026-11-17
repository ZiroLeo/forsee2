import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, LEVEL_EMOJIS
from database import (
    get_user_by_identifier, search_users, get_user_profile, get_recent_subscriptions,
    get_referral_stats, ban_user, unban_user, add_balance, deduct_balance,
    get_user_registration_date, log_admin_action, log_event,
)
from utils import format_days_remaining

from .admin_ui import (
    btn, menu, page, toast, em, esc, money, LINE, protect, ask, refresh_panel,
    reset_flow, parse_int, fmt_date, custom_emoji, BLUE, GREEN, RED,
)

logger = logging.getLogger(__name__)
router = Router(name="admin_users")
protect(router)

LEVEL_FALLBACK = {"new": "🌱", "prime": "🥈", "royal": "👑", "diamond": "💎", "legendary": "🏆"}
STATE_ICON = {"active": "🟢", "expired": "⌛", "depleted": "📉", "deleted": "🗑", "paused": "⏸"}


class UserStates(StatesGroup):
    waiting_query = State()
    waiting_balance = State()


# ============================================================
# صفحهٔ پروفایل
# ============================================================

def render_profile(telegram_id: int):
    profile = get_user_profile(telegram_id)
    if not profile:
        return None

    level_key = profile["level_key"]
    level = f"{custom_emoji(LEVEL_EMOJIS.get(level_key, ''), LEVEL_FALLBACK.get(level_key, '⭐'))} <b>{esc(profile['level_name'])}</b>"
    username = f"@{esc(profile['username'])}" if profile.get("username") else "ندارد"
    name = esc(profile.get("first_name") or "—")
    referral = get_referral_stats(telegram_id)

    if profile["banned"]:
        info = profile.get("ban_info") or {}
        status = f"{em('false', '🚫')} <b>مسدود</b>"
        if info.get("reason"):
            status += f" — {esc(info['reason'])}"
        if info.get("until"):
            status += f"\n     تا {fmt_date(info['until'])}"
    else:
        status = f"{em('true', '✅')} <b>فعال</b>"

    subs = get_recent_subscriptions(telegram_id, 7)
    if subs:
        sub_lines = []
        for s in subs:
            kind = "♾" if s.get("kind") == "unl" else f"{float(s.get('volume_gb') or 0):g}G"
            expiry = "بدون انقضا" if not s.get("expires_at") else format_days_remaining(s["expires_at"])
            sub_lines.append(
                f"{STATE_ICON.get(s.get('state'), '▫️')} <code>{esc(s['config_name'])}</code> | {kind} | {expiry}"
            )
        subs_text = "\n".join(sub_lines)
    else:
        subs_text = "هنوز سرویسی نخریده است."

    text = (
        f"{em('telegram', '👤')} <b>پروفایل کاربر</b>\n{LINE}\n"
        f"🆔 آیدی: <code>{telegram_id}</code>\n"
        f"👤 یوزرنیم: {username}   |   نام: {name}\n"
        f"📅 عضویت: {esc(get_user_registration_date(telegram_id))}\n"
        f"{LINE}\n"
        f"💰 موجودی: <b>{money(profile.get('balance') or 0)}</b> تومان\n"
        f"🛒 تعداد خرید: <b>{int(profile.get('total_purchases') or 0)}</b>   |   سرویس‌ها: <b>{profile['sub_count']}</b>\n"
        f"🏆 سطح: {level}\n"
        f"🤝 دعوت‌ها: <b>{referral['count']}</b>   |   درآمد رفرال: <b>{money(referral['total_earnings'])}</b>\n"
        f"📌 وضعیت: {status}\n"
        f"{LINE}\n"
        f"{em('light', '📦')} <b>۷ سرویس آخر</b>\n{subs_text}"
    )

    ban_button = (
        btn("رفع مسدودی", f"admin:users:unban:{telegram_id}", style=GREEN, emoji="true")
        if profile["banned"]
        else btn("مسدود کردن", f"admin:users:ban:{telegram_id}", style=RED, emoji="false")
    )
    buttons = [
        btn("افزایش موجودی", f"admin:users:bal:add:{telegram_id}", style=GREEN, emoji="wallet"),
        btn("کاهش موجودی", f"admin:users:bal:sub:{telegram_id}", style=RED, emoji="warning"),
        ban_button,
        btn("بروزرسانی", f"admin:users:view:{telegram_id}", style=BLUE, emoji="light"),
    ]
    for s in subs[:7]:
        buttons.append(btn(s["config_name"], f"admin:services:open:{s['id']}", style=BLUE))
    return text, menu(buttons, back="admin:users:menu", back_text="جستجوی کاربر دیگر")


@router.callback_query(F.data.startswith("admin:users:view:"))
async def view_user(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    view = render_profile(int(callback.data.split(":")[3]))
    if not view:
        await toast(callback, "کاربر یافت نشد.", True)
        return
    await page(callback, *view)


# ============================================================
# جستجو
# ============================================================

@router.callback_query(F.data == "admin:users:menu")
async def users_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await ask(
        callback, state, UserStates.waiting_query,
        f"{em('telegram', '👤')} <b>مدیریت کاربران</b>\n{LINE}\n"
        "آیدی عددی یا یوزرنیم کاربر را بفرست.\n\nمثال: <code>123456789</code> یا <code>@username</code>",
        "admin:main",
    )


@router.message(UserStates.waiting_query, F.text)
async def query_received(message: Message, state: FSMContext, bot: Bot):
    query = (message.text or "").strip()
    user = get_user_by_identifier(query)
    candidates = [user] if user else search_users(query, 6)

    if not candidates:
        try:
            await message.delete()
        except Exception:
            pass
        await refresh_panel(
            bot, state, message,
            f"{em('warning', '⚠️')} <b>کاربری پیدا نشد.</b>\nآیدی یا یوزرنیم را دوباره بفرست.",
            menu([], back="admin:main", back_text="انصراف"),
            delete_source=False,
        )
        return

    await reset_flow(state)

    if len(candidates) == 1:
        view = render_profile(candidates[0]["telegram_id"])
        await refresh_panel(bot, state, message, *view)
        return

    buttons = [
        btn(f"{c.get('username') or c.get('first_name') or c['telegram_id']} | {c['telegram_id']}",
            f"admin:users:view:{c['telegram_id']}")
        for c in candidates
    ]
    await refresh_panel(
        bot, state, message,
        f"{em('telegram', '👤')} <b>چند کاربر پیدا شد</b>\n{LINE}\nیکی را انتخاب کن:",
        menu(buttons, back="admin:users:menu", back_text="جستجوی دوباره"),
    )


# ============================================================
# مسدود / رفع مسدودی (با رندر دوبارهٔ پروفایل)
# ============================================================

@router.callback_query(F.data.startswith("admin:users:ban:"))
async def ban(callback: CallbackQuery):
    target = int(callback.data.split(":")[3])
    if target == ADMIN_ID:
        await toast(callback, "مسدود کردن ادمین اصلی ممکن نیست.", True)
        return
    if not get_user_profile(target):
        await toast(callback, "کاربر یافت نشد.", True)
        return
    ban_user(target, callback.from_user.id, "مسدود توسط ادمین")
    log_admin_action(callback.from_user.id, "user_ban", str(target))
    log_event("INFO", "admin_user_banned", callback.from_user.id, f"target={target}")
    await toast(callback, "کاربر مسدود شد.")
    await page(callback, *render_profile(target), answer=False)


@router.callback_query(F.data.startswith("admin:users:unban:"))
async def unban(callback: CallbackQuery):
    target = int(callback.data.split(":")[3])
    unban_user(target, callback.from_user.id)
    log_admin_action(callback.from_user.id, "user_unban", str(target))
    log_event("INFO", "admin_user_unbanned", callback.from_user.id, f"target={target}")
    await toast(callback, "مسدودی برداشته شد.")
    view = render_profile(target)
    if view:
        await page(callback, *view, answer=False)


# ============================================================
# افزایش / کاهش موجودی
# ============================================================

@router.callback_query(F.data.startswith("admin:users:bal:"))
async def balance_ask(callback: CallbackQuery, state: FSMContext):
    _, _, _, mode, target = callback.data.split(":")
    if mode not in ("add", "sub") or not get_user_profile(int(target)):
        await toast(callback, "درخواست نامعتبر.", True)
        return
    label = "افزایش" if mode == "add" else "کاهش"
    await ask(
        callback, state, UserStates.waiting_balance,
        f"{em('wallet', '💰')} <b>{label} موجودی</b> — <code>{target}</code>\n{LINE}\n"
        "مبلغ را به تومان و فقط به‌صورت عدد بفرست.",
        f"admin:users:view:{target}", bal_mode=mode, bal_target=int(target),
    )


@router.message(UserStates.waiting_balance, F.text)
async def balance_received(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    mode, target = data.get("bal_mode"), data.get("bal_target")
    amount = parse_int(message.text, 1, 500_000_000)
    if amount is None or not target:
        try:
            await message.delete()
        except Exception:
            pass
        return
    admin_id = message.from_user.id
    try:
        if mode == "add":
            ok = add_balance(target, amount, "admin_adjust", f"افزایش توسط ادمین {admin_id}")
        else:
            ok = deduct_balance(target, amount, "admin_adjust", f"کاهش توسط ادمین {admin_id}")
    except Exception:
        logger.exception("admin balance change failed")
        ok = False
    if ok:
        log_admin_action(admin_id, f"balance_{mode}", str(target), f"amount={amount}")
    notice = (
        f"{em('true', '✅')} <b>{'افزایش' if mode == 'add' else 'کاهش'} موجودی انجام شد:</b> {money(amount)} تومان\n\n"
        if ok else f"{em('false', '❌')} <b>انجام نشد</b> (موجودی کافی نیست یا کاربر پیدا نشد).\n\n"
    )
    await reset_flow(state)
    view = render_profile(target)
    await refresh_panel(bot, state, message, notice + view[0], view[1])
