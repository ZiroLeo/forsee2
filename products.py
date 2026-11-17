import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import MAX_MONTHS
from database import (
    create_product, list_products, get_product, update_product_prices,
    set_product_active, delete_product, log_event,
)
from pricing import order_price_toman, order_price_stars

from .admin_ui import (
    btn, menu, page, toast, em, esc, money, LINE, protect, ask, refresh_panel,
    reset_flow, parse_int, BLUE, GREEN, RED,
)

logger = logging.getLogger(__name__)
router = Router(name="admin_products")
protect(router)

KIND_TITLE = {"vol": "حجمی", "unl": "نامحدود"}


class ProductStates(StatesGroup):
    waiting_volume = State()
    waiting_months = State()
    waiting_manual_price = State()
    waiting_new_price = State()


def _validity(days: int) -> str:
    return "بدون محدودیت زمانی" if int(days) == 0 else f"{int(days) // 30} ماه"


def _volume(p: dict) -> str:
    return "بی‌نهایت" if float(p["volume_gb"]) <= 0 else f"{float(p['volume_gb']):g} گیگ"


def _product_line(p: dict) -> str:
    flag = "🟢" if p["is_active"] else "🔴"
    return (
        f"{flag} <code>{p['id']}</code> | {_volume(p)} | {_validity(p['days'])} | "
        f"{money(p['price_toman'])} تومان / {money(p['price_stars'])} ⭐"
    )


# ============================================================
# منوی اصلی محصولات
# ============================================================

def _menu_view():
    products = list_products(active_only=False)
    counts = {k: sum(1 for p in products if p["kind"] == k) for k in KIND_TITLE}
    text = (
        f"{em('shop', '🛒')} <b>مدیریت محصولات</b>\n{LINE}\n"
        f"📦 حجمی: <b>{counts['vol']}</b> محصول\n"
        f"♾ نامحدود: <b>{counts['unl']}</b> محصول\n{LINE}\n"
        "یک دسته را انتخاب کن تا محصولاتش را ببینی و مدیریت کنی."
    )
    markup = menu([
        btn(f"حجمی ({counts['vol']})", "admin:products:list:vol", emoji="blue_dot"),
        btn(f"نامحدود ({counts['unl']})", "admin:products:list:unl", emoji="diamond"),
        btn("افزودن محصول", "admin:products:add", style=GREEN, emoji="gift"),
    ])
    return text, markup


@router.callback_query(F.data == "admin:products:menu")
async def products_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = _menu_view()
    await page(callback, text, markup)


def _list_view(kind: str):
    products = [p for p in list_products(active_only=False) if p["kind"] == kind]
    lines = [f"{em('shop', '🛒')} <b>محصولات {KIND_TITLE[kind]}</b>", LINE]
    if not products:
        lines.append("محصولی در این دسته ثبت نشده است.")
    else:
        lines.extend(_product_line(p) for p in products[:30])
        lines += [LINE, "برای مدیریت هر محصول، روی شناسه‌اش بزن."]
    buttons = [btn(f"#{p['id']} | {_volume(p)}", f"admin:products:view:{p['id']}") for p in products[:30]]
    return "\n".join(lines), menu(buttons, back="admin:products:menu")


@router.callback_query(F.data.startswith("admin:products:list:"))
async def products_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kind = callback.data.split(":")[3]
    if kind not in KIND_TITLE:
        await toast(callback, "دستهٔ نامعتبر.", True)
        return
    text, markup = _list_view(kind)
    await page(callback, text, markup)


# ============================================================
# جزئیات یک محصول
# ============================================================

def _view(product_id: int):
    p = get_product(product_id)
    if not p:
        return None
    status = "فعال" if p["is_active"] else "غیرفعال"
    text = (
        f"{em('shop', '🛒')} <b>محصول #{p['id']}</b>\n{LINE}\n"
        f"دسته: <b>{KIND_TITLE.get(p['kind'], p['kind'])}</b>\n"
        f"حجم: <b>{_volume(p)}</b>\n"
        f"مدت: <b>{_validity(p['days'])}</b>\n"
        f"قیمت: <b>{money(p['price_toman'])}</b> تومان\n"
        f"استارز: <b>{money(p['price_stars'])}</b> ⭐\n"
        f"وضعیت: <b>{status}</b>"
    )
    toggle = (btn("غیرفعال کردن", f"admin:products:toggle:{p['id']}", style=RED, emoji="false")
              if p["is_active"] else btn("فعال کردن", f"admin:products:toggle:{p['id']}", style=GREEN, emoji="true"))
    markup = menu(
        [
            btn("قیمت تومانی", f"admin:products:price:{p['id']}:toman", style=BLUE, emoji="wallet"),
            btn("قیمت استارز", f"admin:products:price:{p['id']}:stars", style=GREEN, emoji="stars"),
            toggle,
            btn("حذف محصول", f"admin:products:delask:{p['id']}", style=RED, emoji="warning"),
        ],
        back=f"admin:products:list:{p['kind']}",
    )
    return text, markup


@router.callback_query(F.data.startswith("admin:products:view:"))
async def product_view(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    view = _view(int(callback.data.split(":")[3]))
    if not view:
        await toast(callback, "محصول یافت نشد.", True)
        return
    await page(callback, *view)


@router.callback_query(F.data.startswith("admin:products:toggle:"))
async def product_toggle(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[3])
    p = get_product(product_id)
    if not p:
        await toast(callback, "محصول یافت نشد.", True)
        return
    set_product_active(product_id, not p["is_active"])
    log_event("INFO", "product_toggled", callback.from_user.id, f"id={product_id}")
    await toast(callback, "وضعیت تغییر کرد.")
    await page(callback, *_view(product_id), answer=False)


@router.callback_query(F.data.startswith("admin:products:delask:"))
async def product_delete_ask(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[3])
    p = get_product(product_id)
    if not p:
        await toast(callback, "محصول یافت نشد.", True)
        return
    await page(
        callback,
        f"{em('warning', '⚠️')} <b>محصول #{product_id} حذف شود؟</b>\n{_product_line(p)}\n\nاین کار قابل بازگشت نیست.",
        menu(
            [
                btn("بله، حذف کن", f"admin:products:delete:{product_id}", style=RED, emoji="false"),
                btn("نه، انصراف", f"admin:products:view:{product_id}", style=GREEN, emoji="true"),
            ],
            back=None,
        ),
    )


@router.callback_query(F.data.startswith("admin:products:delete:"))
async def product_delete(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[3])
    p = get_product(product_id)
    kind = p["kind"] if p else "vol"
    delete_product(product_id)
    log_event("INFO", "product_deleted", callback.from_user.id, f"id={product_id}")
    await toast(callback, "محصول حذف شد.")
    text, markup = _list_view(kind)
    await page(callback, text, markup, answer=False)


# ============================================================
# تغییر قیمت یک محصول
# ============================================================

@router.callback_query(F.data.startswith("admin:products:price:"))
async def product_price_ask(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    product_id, cur = int(parts[3]), parts[4]
    p = get_product(product_id)
    if not p or cur not in ("toman", "stars"):
        await toast(callback, "درخواست نامعتبر.", True)
        return
    now = p["price_toman"] if cur == "toman" else p["price_stars"]
    label = "تومان" if cur == "toman" else "استارز"
    await ask(
        callback, state, ProductStates.waiting_new_price,
        f"{em('wallet', '💰')} <b>تغییر قیمت ({label}) محصول #{product_id}</b>\n{LINE}\n"
        f"قیمت فعلی: <b>{money(now)}</b>\nقیمت جدید را فقط به‌صورت عدد بفرست.",
        f"admin:products:view:{product_id}", product_id=product_id, price_cur=cur,
    )


@router.message(ProductStates.waiting_new_price, F.text)
async def product_price_received(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    product_id, cur = data.get("product_id"), data.get("price_cur")
    value = parse_int(message.text, 1, 1_000_000_000 if cur == "toman" else 1_000_000)
    if value is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    update_product_prices(
        product_id,
        price_toman=value if cur == "toman" else None,
        price_stars=value if cur == "stars" else None,
    )
    log_event("INFO", "product_price_changed", message.from_user.id, f"id={product_id} {cur}={value}")
    await reset_flow(state)
    view = _view(product_id)
    await refresh_panel(bot, state, message, f"{em('true', '✅')} <b>قیمت ذخیره شد.</b>\n\n{view[0]}", view[1])


# ============================================================
# افزودن محصول
# ============================================================

@router.callback_query(F.data == "admin:products:add")
async def add_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await page(
        callback,
        f"{em('shop', '🛒')} <b>افزودن محصول</b>\n{LINE}\nنوع محصول را انتخاب کن:",
        menu(
            [
                btn("حجمی", "admin:products:kind:vol", emoji="blue_dot"),
                btn("نامحدود", "admin:products:kind:unl", style=GREEN, emoji="diamond"),
            ],
            back="admin:products:menu",
        ),
    )


@router.callback_query(F.data.startswith("admin:products:kind:"))
async def add_kind(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.split(":")[3]
    if kind not in KIND_TITLE:
        await toast(callback, "نوع نامعتبر.", True)
        return
    await ask(
        callback, state, ProductStates.waiting_volume,
        f"{em('light', '📦')} <b>حجم محصول (گیگابایت)</b>\n{LINE}\n"
        "یک عدد بفرست. برای «بی‌نهایت» عدد <code>0</code> را بفرست.",
        "admin:products:add", kind=kind,
    )


@router.message(ProductStates.waiting_volume, F.text)
async def add_volume(message: Message, state: FSMContext, bot: Bot):
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
    await state.update_data(volume_gb=volume)
    await state.set_state(ProductStates.waiting_months)
    data = await state.get_data()
    extra = "\nبرای «بدون محدودیت زمانی» عدد <code>0</code> را بفرست." if data.get("kind") == "vol" else ""
    await refresh_panel(
        bot, state, message,
        f"{em('light', '📅')} <b>مدت محصول (ماه)</b>\n{LINE}\nعددی بین ۱ تا {MAX_MONTHS} بفرست.{extra}",
        menu([], back="admin:products:add", back_text="انصراف"),
    )


def _default_prices(kind: str, volume: float, days: int):
    key = "vol" if kind == "vol" else ("unl_inf" if volume <= 0 else "unl150")
    return order_price_toman(key, volume, days, 2), order_price_stars(key, volume, days, 2)


def _confirm_text(data: dict) -> str:
    return (
        f"{em('true', '✅')} <b>خلاصهٔ محصول</b>\n{LINE}\n"
        f"دسته: <b>{KIND_TITLE[data['kind']]}</b>\n"
        f"حجم: <b>{'بی‌نهایت' if data['volume_gb'] <= 0 else format(data['volume_gb'], 'g') + ' گیگ'}</b>\n"
        f"مدت: <b>{_validity(data['days'])}</b>\n"
        f"قیمت: <b>{money(data['price_toman'])}</b> تومان / <b>{money(data['price_stars'])}</b> ⭐\n\n"
        "اگر درست است تأیید کن."
    )


def _confirm_markup():
    return menu(
        [
            btn("تأیید و افزودن", "admin:products:confirm", style=GREEN, emoji="true"),
            btn("تغییر قیمت", "admin:products:change_price", style=BLUE, emoji="light"),
        ],
        back="admin:products:menu", back_text="انصراف",
    )


@router.message(ProductStates.waiting_months, F.text)
async def add_months(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    allow_zero = data.get("kind") == "vol"
    months = parse_int(message.text, 0 if allow_zero else 1, MAX_MONTHS)
    if months is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    days = months * 30
    toman, stars = _default_prices(data["kind"], float(data["volume_gb"]), days)
    await state.update_data(days=days, price_toman=toman, price_stars=stars)
    await state.set_state(None)
    data = await state.get_data()
    await refresh_panel(bot, state, message, _confirm_text(data), _confirm_markup())


@router.callback_query(F.data == "admin:products:change_price")
async def add_change_price(callback: CallbackQuery, state: FSMContext):
    await ask(
        callback, state, ProductStates.waiting_manual_price,
        f"{em('light', '💰')} <b>قیمت دلخواه</b>\n{LINE}\n"
        "به شکل <code>تومان/استارز</code> بفرست. مثال: <code>150000/50</code>",
        "admin:products:menu",
    )


@router.message(ProductStates.waiting_manual_price, F.text)
async def add_manual_price(message: Message, state: FSMContext, bot: Bot):
    parts = message.text.replace(" ", "").split("/")
    toman = parse_int(parts[0], 1, 1_000_000_000) if len(parts) == 2 else None
    stars = parse_int(parts[1], 1, 1_000_000) if len(parts) == 2 else None
    if toman is None or stars is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    await state.update_data(price_toman=toman, price_stars=stars)
    await state.set_state(None)
    data = await state.get_data()
    await refresh_panel(bot, state, message, _confirm_text(data), _confirm_markup())


@router.callback_query(F.data == "admin:products:confirm")
async def add_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    required = ("kind", "volume_gb", "days", "price_toman", "price_stars")
    if any(k not in data for k in required):
        await toast(callback, "اطلاعات ناقص است؛ دوباره شروع کن.", True)
        await state.clear()
        return
    try:
        product_id = create_product(
            kind=data["kind"], volume_gb=float(data["volume_gb"]), days=int(data["days"]),
            price_toman=int(data["price_toman"]), price_stars=int(data["price_stars"]),
        )
    except Exception:
        logger.exception("Product creation failed")
        await toast(callback, "خطا در ساخت محصول.", True)
        return
    log_event("INFO", "product_created", callback.from_user.id, f"id={product_id}")
    await state.clear()
    await page(
        callback,
        f"{em('true', '✅')} <b>محصول #{product_id} اضافه شد.</b>",
        menu([btn("مدیریت محصولات", "admin:products:menu", style=GREEN, emoji="shop")]),
    )
