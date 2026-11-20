import asyncio
import html
import json
import logging
from typing import Any, Optional
from urllib.parse import urlparse

from services.notifications import notify_purchase

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import (
    add_balance, set_setting, finalize_purchase,
    deduct_balance, get_balance, get_discount_code,
    get_setting, hard_delete_subscription, log_event,
    list_products, get_product, discount_usable_by, get_user_subscriptions,
)
from pasar_guard import PasarGuardError, panel_registry
from pasar_guard import BYTES_PER_GB
from services.counter import make_config_name, release_reserved_number, reserve_new_config_number
from rich import premium_emoji
import ui
from utils import (
    generate_idempotency_key,
    rate_limiter, validate_discount_code,
)

# ✅ افزودن هشدار به دولوپر
from dev.alerts import send_alert

logger = logging.getLogger(__name__)
router = Router(name="buy")

SERVICE_VOLUME = "vol"
SERVICE_UNLIMITED = "unl"

# تمام سرویس‌های فروشگاه بدون انقضا و بدون محدودیت دستگاه ساخته می‌شوند.
SERVICE_DAYS = 0
SERVICE_DEVICES = 0

_user_purchase_locks: dict[int, asyncio.Lock] = {}
_pending_purchase_users: set[int] = set()


class BuyStates(StatesGroup):
    waiting_discount_code = State()


def _user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_purchase_locks:
        _user_purchase_locks[user_id] = asyncio.Lock()
    return _user_purchase_locks[user_id]


def _pending_setting_key(user_id: int) -> str:
    return f"purchase_pending:{int(user_id)}"


def _persist_pending_purchase(user_id: int, order_key: str, status: str) -> None:
    payload = {"order_key": order_key, "status": status}
    set_setting(_pending_setting_key(user_id), json.dumps(payload, ensure_ascii=False))


def _clear_pending_purchase(user_id: int) -> None:
    set_setting(_pending_setting_key(user_id), "")
    _pending_purchase_users.discard(user_id)


def _safe_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _safe_log(level: str, event: str, user_id: int, detail: str = "") -> None:
    try:
        log_event(level, event, user_id, detail[:1000])
    except Exception:
        logger.exception("Could not write purchase event log: %s", event)


def _emoji_id(key: Optional[str]) -> Optional[str]:
    if not PREMIUM_EMOJI_ENABLED or not key:
        return None
    value = PREMIUM_EMOJIS.get(key)
    return str(value).strip() if value else None


def _button(text: str, callback_data: str, emoji_key: Optional[str] = None) -> InlineKeyboardButton:
    if not callback_data:
        raise ValueError("callback_data is required")
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("Callback data exceeds Telegram's 64-byte limit")

    emoji_id = _emoji_id(emoji_key)
    if not text and not emoji_id:
        raise ValueError("Button must have text or emoji")

    kwargs: dict[str, Any] = {
        "text": text if text else "\u200b",  # کاراکتر نامرئی برای متن خالی
        "callback_data": callback_data,
        "style": "primary",
    }
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**kwargs)

def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _valid_subscription_link(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _valid_config_uri(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 3900:
        return False
    return urlparse(value.strip()).scheme.lower() in {
        "vless", "vmess", "trojan", "ss", "ssr", "tuic", "hysteria",
        "hysteria2", "wireguard", "http", "https",
    }


# ============================================================
# PRODUCTS FROM DATABASE
# ============================================================

def _get_products_by_kind(kind: str) -> list[dict]:
    """دریافت محصولات فعال از دیتابیس بر اساس نوع."""
    try:
        return [p for p in list_products(active_only=True) if p["kind"] == kind]
    except Exception:
        logger.exception("Could not load products from database")
        return []


def _valid_product_for_service(product: dict, service_type: str) -> bool:
    if service_type == SERVICE_VOLUME:
        return product["kind"] == "vol"
    if service_type == SERVICE_UNLIMITED:
        return product["kind"] == "unl"
    return False


# ============================================================
# MESSAGE EDIT
# ============================================================

async def _edit_purchase(
    *, bot, state: FSMContext, text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    source_message: Optional[Message] = None,
) -> bool:
    data = await state.get_data()
    chat_id, message_id = data.get("purchase_chat_id"), data.get("purchase_message_id")
    if not chat_id or not message_id:
        logger.error("Purchase message identifiers are missing")
        return False
    if source_message and (
        source_message.chat.id != int(chat_id) or source_message.message_id != int(message_id)
    ):
        logger.warning("Ignored stale purchase message callback: %s", source_message.message_id)
        return False
    try:
        await bot.edit_message_text(
            chat_id=int(chat_id), message_id=int(message_id),
            text=text, reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        logger.exception("Could not edit active purchase message")
        return False
    except Exception:
        logger.exception("Unexpected purchase message edit error")
        return False


async def _current_purchase(callback: CallbackQuery, state: FSMContext) -> bool:
    data = await state.get_data()
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("پیام خرید در دسترس نیست.", show_alert=True)
        return False
    if data.get("purchase_chat_id") != message.chat.id or data.get("purchase_message_id") != message.message_id:
        await callback.answer("این دکمه مربوط به یک خرید قدیمی است. از منوی خرید دوباره وارد شو.", show_alert=True)
        return False
    return True


async def _edit_callback(callback: CallbackQuery, state: FSMContext, text: str, markup=None) -> bool:
    if not await _current_purchase(callback, state):
        return False
    return await _edit_purchase(
        bot=callback.bot, state=state, text=text, reply_markup=markup,
        source_message=callback.message if isinstance(callback.message, Message) else None,
    )


async def _edit_user_message(message: Message, state: FSMContext, text: str, markup=None) -> bool:
    return await _edit_purchase(bot=message.bot, state=state, text=text, reply_markup=markup)


# ============================================================
# KEYBOARDS
# ============================================================

def buy_type_selection_keyboard() -> InlineKeyboardMarkup:
    return _keyboard([
        [_button("سرویس حجمی", "buy:type:vol", "shop"), _button("سرویس نامحدود", "buy:type:unl", "world")],
        [_button("بازگشت به منوی اصلی", "menu:main", "telegram")],
    ])


def buy_volume_selection_keyboard(service_type: str) -> InlineKeyboardMarkup:
    products = _get_products_by_kind(service_type)
    rows: list[list[InlineKeyboardButton]] = []

    if not products:
        rows.append([_button("محصولی موجود نیست", "buy:noop", "warning")])
    else:
        # دو دکمه در هر ردیف برای خوانایی بهتر
        for i in range(0, len(products), 2):
            row = []
            for p in products[i:i + 2]:
                if p["kind"] == "unl":
                    label = (
                        f"نامحدود {float(p['volume_gb']):g} گیگ"
                        if float(p["volume_gb"]) > 0
                        else "نامحدود بی‌نهایت"
                    )
                else:
                    label = f"{float(p['volume_gb']):g} گیگ"
                row.append(_button(label, f"buy:prod:{p['id']}", "blue_dot" if p["kind"] == "vol" else "diamond"))
            rows.append(row)

    rows.append([_button("بازگشت", "buy:main", "back")])
    return _keyboard(rows)


def buy_discount_keyboard() -> InlineKeyboardMarkup:
    return _keyboard([
        [_button("وارد کردن کد تخفیف", "buy:discount:enter", "gift")],
        [_button("بدون کد تخفیف ادامه می‌دهم", "buy:discount:skip", "true")],
        [_button("بازگشت", "buy:vol", "back")],
    ])


def buy_payment_keyboard(price_toman: int, price_stars: int) -> InlineKeyboardMarkup:
    rows = [[_button(f"پرداخت با کیف پول | {price_toman:,} تومان", "buy:pay:wallet", "wallet")]]
    if int(price_stars or 0) > 0:
        rows.append([_button(f"پرداخت با Stars | {price_stars}", "buy:pay:stars", "stars")])
    rows.append([_button("بازگشت", "buy:discount", "back")])
    return _keyboard(rows)


def purchase_success_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return _keyboard([
        [_button("دریافت کامل کانفیگ‌ها", f"buy:configs:{subscription_id}", "world")],
        [_button("بازگشت به منوی اصلی", "menu:main", "back")],
    ])


def purchase_retry_keyboard(price_toman: int) -> InlineKeyboardMarkup:
    return _keyboard([
        [_button(f"تلاش دوباره برای پرداخت | {price_toman:,} تومان", "buy:pay:wallet", "wallet")],
        [_button("بازگشت به فاکتور", "buy:discount", "back")],
    ])


# ============================================================
# PRICING
# ============================================================

def _valid_purchase_data(data: dict[str, Any]) -> bool:
    """اعتبارسنجی سفارش بر اساس محصول؛ مدت و تعداد دستگاه در فروشگاه نداریم."""
    try:
        product_id = int(data["product_id"])
        service_type = str(data["service_type"])
        volume = float(data["volume_gb"])
    except (KeyError, TypeError, ValueError):
        return False

    if service_type not in {SERVICE_VOLUME, SERVICE_UNLIMITED}:
        return False
    if volume < 0:
        return False

    product = get_product(product_id)
    if not product or not product.get("is_active"):
        return False
    if product.get("kind") != service_type:
        return False
    if float(product.get("volume_gb") or 0) != volume:
        return False

    try:
        panel_id = int(product.get("panel_id") or 0)
    except (TypeError, ValueError):
        return False
    if panel_id <= 0:
        return False

    return True


def _user_error_text(exc: Exception, default: str = "اطلاعات سفارش معتبر نیست.") -> str:
    """فقط پیام‌های فارسیِ خودِ ربات به کاربر نشان داده می‌شود؛ متن‌های فنی انگلیسی هرگز."""
    text = str(exc or "")
    if any("\u0600" <= ch <= "\u06ff" for ch in text):
        return text[:300]
    return default


def _discount_for_order(data: dict[str, Any]) -> tuple[int, Optional[int]]:
    if not data.get("discount_id") or not data.get("discount_code"):
        return 0, None
    try:
        code = get_discount_code(str(data["discount_code"]))
        if not code or int(code.get("id", 0)) != int(data["discount_id"]):
            return 0, None
        if not code.get("is_active") or int(code.get("used_count", 0)) >= int(code.get("max_uses", 0)):
            return 0, None
        if not discount_usable_by(code, int(data.get("user_id", 0))):
            return 0, None  # کد اختصاصی دیگران یا منقضی‌شده
        percent = int(code.get("percent", 0))
        return (percent, int(data["discount_id"])) if 1 <= percent <= 100 else (0, None)
    except Exception:
        logger.exception("Could not revalidate discount")
        return 0, None


def _validate_order_discount(data: dict[str, Any]) -> tuple[int, Optional[int]]:
    """
    اعتبارسنجی سخت‌گیرانه کد تخفیف پیش از نهایی‌سازی سفارش.
    در صورت نامعتبر شدن تخفیف قبلی، اجازه ادامه خرید داده نمی‌شود.
    """

    discount_id = data.get("discount_id")
    discount_code = data.get("discount_code")

    try:
        stored_percent = int(data.get("discount_percent") or 0)
    except (TypeError, ValueError):
        raise ValueError("اطلاعات درصد تخفیف سفارش معتبر نیست.")

    # سفارش بدون کد تخفیف
    if not discount_id and not discount_code:
        if stored_percent != 0:
            raise ValueError("اطلاعات تخفیف سفارش ناسازگار است.")
        return 0, None

    # اطلاعات ناقص کد تخفیف
    if not discount_id or not discount_code:
        raise ValueError("اطلاعات کد تخفیف ناقص است.")

    try:
        code = get_discount_code(str(discount_code).strip())
    except Exception as exc:
        logger.exception("Final discount lookup failed")
        raise ValueError("بررسی کد تخفیف ناموفق بود.") from exc

    if not code:
        raise ValueError("کد تخفیف دیگر وجود ندارد.")

    try:
        current_id = int(code.get("id", 0))
        expected_id = int(discount_id)
        used_count = int(code.get("used_count", 0))
        max_uses = int(code.get("max_uses", 0))
        current_percent = int(code.get("percent", 0))
    except (TypeError, ValueError):
        raise ValueError("اطلاعات کد تخفیف معتبر نیست.")

    if current_id != expected_id:
        raise ValueError("کد تخفیف سفارش تغییر کرده است.")

    if not code.get("is_active"):
        raise ValueError("کد تخفیف غیرفعال شده است.")

    if max_uses <= 0 or used_count >= max_uses:
        raise ValueError("ظرفیت استفاده از کد تخفیف تکمیل شده است.")

    if not discount_usable_by(code, int(data.get("user_id", 0))):
        raise ValueError("کد تخفیف برای شما معتبر نیست یا منقضی شده است.")

    if not 1 <= current_percent <= 100:
        raise ValueError("درصد کد تخفیف معتبر نیست.")

    if current_percent != stored_percent:
        raise ValueError("درصد کد تخفیف تغییر کرده است.")

    return current_percent, expected_id


def _build_order_prices(data: dict[str, Any]) -> tuple[int, int, int, Optional[int]]:
    """قیمت هر سفارش مستقیماً از قیمت ثبت‌شده روی محصول خوانده می‌شود.
    هیچ هزینه‌ای برای ماه اضافه یا دستگاه اضافه وجود ندارد."""
    if not _valid_purchase_data(data):
        raise ValueError("Invalid purchase data")

    product = get_product(int(data["product_id"]))
    if not product:
        raise ValueError("Product not found")

    try:
        toman = int(product.get("price_toman") or 0)
        stars = int(product.get("price_stars") or 0)
    except (TypeError, ValueError):
        raise ValueError("Product price is invalid")

    if toman <= 0:
        raise ValueError("Product toman price is not configured")

    discount_percent, discount_id = _discount_for_order(data)
    toman = toman * (100 - discount_percent) // 100
    if stars > 0:
        stars = max(1, -(-stars * (100 - discount_percent) // 100))

    if toman <= 0:
        raise ValueError("Purchase price must be greater than zero")

    return toman, stars, discount_percent, discount_id


def _service_title(data: dict[str, Any]) -> str:
    product_id = data.get("product_id")
    product = get_product(int(product_id)) if product_id else None
    if not product:
        return "سرویس نامشخص"
    volume = float(product.get("volume_gb") or 0)
    if product["kind"] == "unl":
        return "نامحدود بی‌نهایت" if volume <= 0 else f"نامحدود {volume:g} گیگ"
    return f"{volume:g} گیگ حجمی"


# ============================================================
# RENDER STEPS
# ============================================================

async def _show_discount_step(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not _valid_purchase_data(data):
        await _edit_callback(callback, state, "اطلاعات سفارش معتبر نیست. از ابتدا شروع کن.", buy_type_selection_keyboard())
        return
    await _edit_callback(callback, state,
        f"{premium_emoji('gift', '●')} <b>کد تخفیف داری؟</b>\n\nاگر کد تخفیف داری وارد کن؛ در غیر این صورت ادامه بده.",
        buy_discount_keyboard())


async def _show_invoice(*, bot, state: FSMContext, source_message: Optional[Message] = None) -> None:
    data = await state.get_data()
    if not _valid_purchase_data(data):
        await _edit_purchase(
            bot=bot,
            state=state,
            text=f"{premium_emoji('warning', '●')} <b>اطلاعات سفارش معتبر نیست.</b>\nخرید را از ابتدا شروع کن.",
            reply_markup=buy_type_selection_keyboard(),
            source_message=source_message,
        )
        return

    try:
        price_toman, price_stars, discount, _discount_id = _build_order_prices(data)
    except Exception:
        logger.exception("Could not calculate purchase invoice")
        send_alert(
            "ERROR",
            f"محاسبه قیمت ناموفق\nuser_id: {data.get('user_id')}\nproduct_id: {data.get('product_id')}",
        )
        await _edit_purchase(
            bot=bot,
            state=state,
            text=f"{premium_emoji('warning', '●')} <b>محاسبه قیمت ناموفق بود.</b> با پشتیبانی تماس بگیر.",
            reply_markup=buy_discount_keyboard(),
            source_message=source_message,
        )
        return

    # stars.py قیمت‌های ذخیره‌شده در state را برای پرداخت تلگرام می‌خواند.
    await state.update_data(price_toman=price_toman, price_stars=price_stars)

    try:
        balance = get_balance(int(data["user_id"]))
    except Exception:
        logger.exception("Could not read balance")
        balance = 0

    discount_line = (
        f"{premium_emoji('gift', '🏷')} <b>تخفیف:</b> {discount}%\n"
        if discount else ""
    )
    text = (
        f"{premium_emoji('blue_fire', '💰')} <b>فاکتور نهایی خرید</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{premium_emoji('shop', '🛒')} <b>نوع سرویس:</b> {_safe_text(_service_title(data))}\n"
        f"{premium_emoji('world', '🌐')} <b>مدت اعتبار:</b> بدون محدودیت زمانی\n"
        f"{premium_emoji('telegram', '👥')} <b>تعداد کاربران و دستگاه‌ها:</b> نامحدود\n"
        f"{discount_line}\n"
        "━━━━━━━━━━━━━━\n"
        f"{premium_emoji('blue_heart', '💵')} <b>مبلغ تومانی:</b> <b>{price_toman:,} تومان</b>\n"
        + (f"{premium_emoji('stars', '⭐')} <b>مبلغ Stars:</b> <b>{price_stars}</b>\n" if price_stars > 0 else "")
        + f"{premium_emoji('diamond', '💎')} <b>موجودی کیف پول:</b> {int(balance):,} تومان\n\n"
        "روش پرداخت را انتخاب کن."
    )

    await _edit_purchase(
        bot=bot,
        state=state,
        text=text,
        reply_markup=buy_payment_keyboard(price_toman, price_stars),
        source_message=source_message,
    )


# ============================================================
# PASARGUARD + WALLET
# ============================================================

async def _delete_remote(panel_id: int, config_name: str) -> bool:
    """حذف کانفیگ از همان پنلی که برای محصول انتخاب شده بود."""
    try:
        client = await panel_registry.get_client(int(panel_id))
        await client.delete_config(username=config_name)
        return True
    except PasarGuardError as exc:
        if getattr(exc, "status", None) == 404:
            return True
        logger.exception("Could not delete remote config %s from panel %s", config_name, panel_id)
        return False
    except Exception:
        logger.exception("Could not delete remote config %s from panel %s", config_name, panel_id)
        return False


def _refund(user_id: int, amount: int, order_key: str, description: str) -> bool:
    if amount <= 0:
        return True
    try:
        return bool(add_balance(user_id, amount, "refund", description, reference_id=f"refund:{order_key}"))
    except Exception:
        logger.exception("Refund failed for user %s", user_id)
        return False


async def _create_remote_config(
    *, user_id: int, username: Optional[str], config_name: str,
    product: dict, panel_id: int,
) -> dict[str, Any]:
    """ساخت کانفیگ با payload رسمی PasarGuard و پنل متصل به محصول.

    create_user فعلی در pasar_guard.py حجم صفر را رد می‌کند و expire=0 را
    صریح نمی‌فرستد؛ بنابراین از registry و لایه درخواست همان API استفاده می‌کنیم.
    """
    client = await panel_registry.get_client(int(panel_id))
    volume_gb = float(product.get("volume_gb") or 0)
    if volume_gb < 0:
        raise PasarGuardError("Invalid product volume.")

    data_limit = 0 if volume_gb == 0 else int(volume_gb * BYTES_PER_GB)
    note_username = str(username).strip().lstrip("@") if username else "N/A"
    note = f"Telegram ID: {int(user_id)} | Username: {note_username} | Type: {product.get('kind')}"

    payload: dict[str, Any] = {
        "username": config_name,
        "proxy_settings": {},
        "data_limit": data_limit,
        "expire": 0,
        "data_limit_reset_strategy": "no_reset",
        "hwid_limit": None,
        "status": "active",
        "note": note[:500],
    }
    if client.group_id is not None:
        payload["group_ids"] = [int(client.group_id)]

    try:
        response = await client._request("POST", "/api/user", json=payload)
    except Exception as exc:
        # درخواست ارسال شده اما پاسخ قطعی دریافت نشده است.
        try:
            setattr(exc, "_pasarguard_request_sent", True)
        except Exception:
            pass
        raise

    # از این نقطه به بعد می‌دانیم درخواست به پنل رسیده و پاسخ برگشته است.
    try:
        if not isinstance(response, dict):
            response = {}

        user_data = response
        try:
            remote_user = await client.get_user(config_name)
        except Exception:
            logger.exception("Could not fetch created PasarGuard user %s", config_name)
            remote_user = None

        if remote_user:
            user_data = remote_user

        subscription_url = client._extract_subscription_url(user_data)
        if not subscription_url:
            subscription_url = client._extract_subscription_url(response)
        subscription_url = client.normalize_subscription_url(subscription_url)

        if not subscription_url:
            raise PasarGuardError("User was created but subscription URL is missing.")

        normalized = client._normalize_user_response(
            user_data,
            fallback_username=config_name,
            subscription_url=subscription_url,
        )
        normalized["subscription_url"] = subscription_url
        return normalized
    except Exception as exc:
        try:
            setattr(exc, "_pasarguard_response_received", True)
        except Exception:
            pass
        raise


async def _provision_purchase(*, user_id: int, data: dict[str, Any], telegram_username: Optional[str] = None) -> tuple[str, Any]:
    if not _valid_purchase_data(data):
        return "failed", "اطلاعات سفارش معتبر نیست."

    try:
        discount_data = {**data, "user_id": user_id}
        discount_percent, discount_id = _validate_order_discount(discount_data)
        price_toman, _price_stars, current_discount, current_discount_id = _build_order_prices(discount_data)
        if current_discount_id != discount_id or current_discount != discount_percent:
            raise ValueError("کد تخفیف سفارش تغییر کرده است.")
    except ValueError as exc:
        logger.warning("Purchase discount validation rejected: user=%s reason=%s", user_id, exc)
        return "failed", _user_error_text(exc)
    except Exception:
        logger.exception("Final purchase price validation failed")
        return "failed", "بررسی اعتبار تخفیف یا محاسبه مبلغ سفارش ناموفق بود."

    order_key = str(data.get("idempotency_key") or "")
    if not order_key:
        return "failed", "شناسه سفارش معتبر نیست. خرید را از ابتدا شروع کن."

    product_id = int(data["product_id"])
    product = get_product(product_id)
    if not product or not product.get("is_active"):
        return "failed", "محصول دیگر در دسترس نیست."

    try:
        panel_id = int(product.get("panel_id") or 0)
    except (TypeError, ValueError):
        panel_id = 0
    if panel_id <= 0:
        return "failed", "برای این محصول پنل معتبری انتخاب نشده است. با پشتیبانی تماس بگیر."

    volume = float(product.get("volume_gb") or 0)
    kind = str(product.get("kind") or "")
    days = SERVICE_DAYS
    devices = SERVICE_DEVICES
    number = config_name = client_id = subscription_id = None
    debited = False
    create_response_received = False
    completed = False
    link = None

    try:
        debited = deduct_balance(
            user_id,
            price_toman,
            "purchase",
            f"خرید سرویس {volume:g}GB بدون انقضا و محدودیت کاربر",
            reference_id=order_key,
        )
    except Exception as exc:
        logger.exception("Wallet deduction failed")
        send_alert("CRITICAL", "خطای کسر کیف پول پیش از ساخت سرویس", exc=exc,
                   user_id=user_id, amount=price_toman, order=order_key,
                   product_id=product_id, panel_id=panel_id)
        return "failed", "کسر موجودی انجام نشد. دوباره تلاش کن."

    if not debited:
        return "failed", "موجودی کیف پول کافی نیست یا کسر موجودی انجام نشد."

    try:
        number = reserve_new_config_number(is_test=False)
        if not number:
            raise RuntimeError("Could not reserve config number")
        config_name = make_config_name(number, is_test=False)
        client_data = await _create_remote_config(
            user_id=user_id,
            username=telegram_username,
            config_name=config_name,
            product=product,
            panel_id=panel_id,
        )
        create_response_received = True

        if not isinstance(client_data, dict):
            raise RuntimeError("Invalid PasarGuard response")

        try:
            client_id = int(client_data.get("id") or 0) or None
        except (TypeError, ValueError):
            client_id = None

        link = client_data.get("subscription_url")
        if not _valid_subscription_link(link):
            raise PasarGuardError("PasarGuard returned an invalid subscription URL.")

        try:
            subscription_id = finalize_purchase(
                telegram_id=user_id,
                config_number=int(number),
                config_name=config_name,
                kind=kind,
                volume_gb=volume,
                days=days,
                devices=devices,
                price_toman=price_toman,
                # ستون قدیمی paxiva_client_id حفظ شده؛ در این پروژه شناسه کاربر پنل را نگه می‌دارد.
                paxiva_client_id=int(client_id or 0),
                link=str(link),
                discount_id=int(discount_id) if discount_id is not None else None,
                panel_id=panel_id,
            )
        except ValueError as exc:
            send_alert("WARNING", "کد تخفیف هم‌زمان مصرف شد؛ سفارش باید rollback شود",
                       exc=exc, user_id=user_id, discount_id=discount_id,
                       order=order_key, config=config_name, panel_id=panel_id)
            raise RuntimeError("Discount code became unavailable or was already used") from exc
        except Exception as exc:
            send_alert("CRITICAL", "ثبت اشتراک در دیتابیس ناموفق؛ سرویس در پنل ساخته شده است",
                       exc=exc, user_id=user_id, order=order_key, config=config_name,
                       amount=price_toman, product_id=product_id, panel_id=panel_id)
            raise

        completed = True
        _safe_log("INFO", "purchase_success", user_id,
                  f"order={order_key}; subscription={subscription_id}; config={config_name}; panel={panel_id}")
        return "success", {
            "config_name": config_name,
            "link": str(link),
            "client_id": int(client_id or 0),
            "subscription_id": int(subscription_id),
            "price_toman": price_toman,
            "kind": kind,
            "volume_gb": volume,
            "days": days,
            "devices": devices,
            "panel_id": panel_id,
        }

    except Exception as exc:
        logger.exception("Purchase provisioning failed for user %s", user_id)

        if completed and subscription_id:
            send_alert("WARNING", "خطا پس از ثبت موفق خرید؛ سرویس حفظ شد", exc=exc,
                       user_id=user_id, order=order_key, subscription_id=subscription_id,
                       config=config_name, panel_id=panel_id)
            return "success", {
                "config_name": config_name,
                "link": str(link or ""),
                "client_id": int(client_id or 0),
                "subscription_id": int(subscription_id),
                "price_toman": price_toman,
                "kind": kind,
                "volume_gb": volume,
                "days": days,
                "devices": devices,
                "panel_id": panel_id,
            }

        status = getattr(exc, "status", None)
        definitive_rejection = isinstance(exc, PasarGuardError) and status is not None and 400 <= int(status) < 500
        request_sent = bool(getattr(exc, "_pasarguard_request_sent", False))
        response_received = create_response_received or bool(
            getattr(exc, "_pasarguard_response_received", False)
        )

        if request_sent and not response_received and not definitive_rejection:
            _safe_log("CRITICAL", "purchase_ambiguous", user_id,
                      f"order={order_key}; config={config_name}; panel={panel_id}; no panel response")
            send_alert("CRITICAL", "خرید مبهم: پاسخ پنل دریافت نشد؛ ممکن است سرویس ساخته شده باشد و وجه کسر شده است",
                       exc=exc, user_id=user_id, order=order_key, config=config_name,
                       amount=price_toman, product_id=product_id, panel_id=panel_id)
            return "pending", (
                "ارتباط با پنل هنگام ساخت سرویس قطع شد و نتیجه قطعی نیست. ممکن است سرویس ساخته شده باشد؛ "
                "برای جلوگیری از خرید تکراری، بازپرداخت خودکار انجام نمی‌شود. با پشتیبانی تماس بگیر."
            )

        if response_received and config_name and not await _delete_remote(panel_id, config_name):
            _safe_log("CRITICAL", "purchase_orphan_config", user_id,
                      f"order={order_key}; config={config_name}; panel={panel_id}")
            send_alert("CRITICAL", "سرویس ساخته‌شده در پنل حذف نشد؛ بازپرداخت انجام نشد",
                       exc=exc, user_id=user_id, order=order_key, config=config_name,
                       panel_id=panel_id, amount=price_toman)
            return "pending", "سرویس در پنل ساخته شده اما حذف آن تأیید نشد؛ سفارش نیازمند بررسی پشتیبانی است."

        if subscription_id is not None:
            try:
                if not hard_delete_subscription(int(subscription_id)):
                    send_alert("ERROR", "حذف اشتراک محلی هنگام rollback تأیید نشد",
                               user_id=user_id, order=order_key, subscription_id=subscription_id)
                    return "pending", "سرویس حذف شد اما پاک‌سازی دیتابیس تأیید نشد. با پشتیبانی تماس بگیر."
            except Exception as rb_exc:
                logger.exception("Local subscription rollback failed")
                send_alert("ERROR", "rollback اشتراک محلی با خطا مواجه شد", exc=rb_exc,
                           user_id=user_id, order=order_key, subscription_id=subscription_id)
                return "pending", "پاک‌سازی سفارش کامل نشد و نیازمند بررسی پشتیبانی است."

        if number is not None:
            release_reserved_number(int(number), is_test=False)

        if debited and not _refund(user_id, price_toman, order_key,
                                   f"بازگشت وجه سفارش {config_name or order_key}"):
            _safe_log("CRITICAL", "purchase_refund_failed", user_id,
                      f"order={order_key}; amount={price_toman}")
            send_alert("CRITICAL", "بازگشت وجه ناموفق: کاربر پول داده و سرویس نگرفته",
                       exc=exc, user_id=user_id, order=order_key, amount=price_toman,
                       refund_reference=f"refund:{order_key}", config=config_name)
            return "pending", "خرید ناموفق بود اما بازگشت وجه تأیید نشد. فوراً با پشتیبانی تماس بگیر."

        _safe_log("ERROR", "purchase_failed", user_id,
                  f"order={order_key}; error={type(exc).__name__}; panel={panel_id}")
        if isinstance(exc, PasarGuardError):
            send_alert("ERROR", "پنل PasarGuard ساخت سرویس را رد کرد", exc=exc,
                       user_id=user_id, order=order_key, panel_id=panel_id,
                       product_id=product_id, volume_gb=volume, refunded="yes")
            return "failed", "پنل ساخت سرویس را رد کرد؛ مبلغ به کیف پولت بازگشت داده شد."
        return "failed", "ساخت سرویس کامل نشد؛ مبلغ به کیف پولت بازگشت داده شد."


# ============================================================
# START / SERVICE SELECTION
# ============================================================

@router.callback_query(F.data == "menu:buy")
async def start_buy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = callback.from_user
    if not user:
        return
    if not rate_limiter.is_allowed(user.id):
        await callback.message.answer("لطفاً کمی صبر کن و دوباره تلاش کن.")
        return
    persisted_pending = get_setting(_pending_setting_key(user.id), "")
    if user.id in _pending_purchase_users or persisted_pending:
        await callback.message.answer(
            f"{premium_emoji('warning', '●')} <b>یک سفارش قبلی شما نیازمند بررسی است.</b>\n\n"
            "برای جلوگیری از خرید تکراری، تا تعیین وضعیت سفارش قبلی امکان خرید جدید وجود ندارد. با پشتیبانی تماس بگیر."
        )
        return
    if get_setting("sales_open", "1") == "0":
        await callback.message.answer(
            f"{premium_emoji('warning', '●')} <b>فروش موقتاً بسته است.</b>\n\nلطفاً اطلاعیه‌های کانال را دنبال کن."
        )
        return

    await state.clear()
    text = f"{premium_emoji('shop', '●')} <b>خرید سرویس FORSEE2</b>\n\nنوع سرویس موردنظرت را انتخاب کن."
    purchase_message = await ui.show(callback, text, buy_type_selection_keyboard())
    if purchase_message is None:
        return
    await state.update_data(
        user_id=user.id, purchase_chat_id=purchase_message.chat.id,
        purchase_message_id=purchase_message.message_id,
        idempotency_key=generate_idempotency_key(f"buy-{user.id}"),
        purchase_processing=False, purchase_pending=False,
    )


@router.callback_query(F.data == "buy:noop")
async def buy_noop(callback: CallbackQuery):
    await callback.answer("در حال حاضر محصولی برای این بخش موجود نیست.", show_alert=True)


@router.callback_query(F.data == "buy:type:vol")
async def select_volume_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    await state.update_data(
        service_type=SERVICE_VOLUME,
        product_id=None,
        volume_gb=None,
        panel_id=None,
        days=SERVICE_DAYS,
        devices=SERVICE_DEVICES,
        discount_id=None,
        discount_code=None,
        discount_percent=0,
        purchase_processing=False,
        purchase_pending=False,
    )
    await _edit_callback(
        callback,
        state,
        f"{premium_emoji('shop', '●')} <b>انتخاب سرویس حجمی</b>\n\nمحصول موردنظرت را انتخاب کن.",
        buy_volume_selection_keyboard(SERVICE_VOLUME),
    )


@router.callback_query(F.data == "buy:type:unl")
async def select_unlimited_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    await state.update_data(
        service_type=SERVICE_UNLIMITED,
        product_id=None,
        volume_gb=None,
        panel_id=None,
        days=SERVICE_DAYS,
        devices=SERVICE_DEVICES,
        discount_id=None,
        discount_code=None,
        discount_percent=0,
        purchase_processing=False,
        purchase_pending=False,
    )
    await _edit_callback(
        callback,
        state,
        f"{premium_emoji('world', '●')} <b>انتخاب سرویس نامحدود</b>\n\nمحصول موردنظرت را انتخاب کن.",
        buy_volume_selection_keyboard(SERVICE_UNLIMITED),
    )


@router.callback_query(F.data == "buy:main")
async def back_to_buy_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    await state.update_data(
        service_type=None,
        product_id=None,
        volume_gb=None,
        panel_id=None,
        days=SERVICE_DAYS,
        devices=SERVICE_DEVICES,
        discount_id=None,
        discount_code=None,
        discount_percent=0,
    )
    await _edit_callback(
        callback,
        state,
        f"{premium_emoji('shop', '●')} <b>نوع سرویس را انتخاب کن.</b>",
        buy_type_selection_keyboard(),
    )


@router.callback_query(F.data.startswith("buy:prod:"))
async def select_product(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return

    data = await state.get_data()
    service_type = data.get("service_type")
    try:
        product_id = int(callback.data.rsplit(":", 1)[-1])
    except (ValueError, TypeError):
        await _edit_callback(callback, state, "شناسه محصول نامعتبر است.", buy_type_selection_keyboard())
        return

    product = get_product(product_id)
    if not product or not product.get("is_active"):
        await _edit_callback(callback, state, "این محصول در دسترس نیست.", buy_type_selection_keyboard())
        return
    if not _valid_product_for_service(product, service_type):
        await _edit_callback(callback, state, "این محصول برای نوع سرویس انتخاب‌شده معتبر نیست.", buy_type_selection_keyboard())
        return

    try:
        panel_id = int(product.get("panel_id") or 0)
    except (TypeError, ValueError):
        panel_id = 0
    if panel_id <= 0:
        await _edit_callback(
            callback,
            state,
            f"{premium_emoji('warning', '●')} <b>این محصول هنوز به پنل متصل نشده است.</b>\nبا پشتیبانی تماس بگیر.",
            buy_volume_selection_keyboard(service_type),
        )
        return

    volume = float(product.get("volume_gb") or 0)
    await state.update_data(
        product_id=product_id,
        volume_gb=volume,
        volume_key=str(product_id),
        panel_id=panel_id,
        days=SERVICE_DAYS,
        devices=SERVICE_DEVICES,
        discount_id=None,
        discount_code=None,
        discount_percent=0,
    )
    await _show_discount_step(callback, state)


@router.callback_query(F.data == "buy:vol")
async def back_to_volume(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    data = await state.get_data()
    service_type = data.get("service_type")
    if service_type not in {SERVICE_VOLUME, SERVICE_UNLIMITED}:
        await _edit_callback(callback, state, "ابتدا نوع سرویس را انتخاب کن.", buy_type_selection_keyboard())
        return
    await _edit_callback(
        callback,
        state,
        f"{premium_emoji('shop', '●')} <b>انتخاب محصول</b>",
        buy_volume_selection_keyboard(service_type),
    )


@router.callback_query(F.data == "buy:discount:enter")
async def enter_discount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    if not _valid_purchase_data(await state.get_data()):
        await _edit_callback(callback, state, "اطلاعات سفارش معتبر نیست. از ابتدا شروع کن.", buy_type_selection_keyboard())
        return
    await state.set_state(BuyStates.waiting_discount_code)
    await _edit_callback(callback, state,
        f"{premium_emoji('gift', '●')} <b>وارد کردن کد تخفیف</b>\n\nکد تخفیف را در یک پیام ارسال کن.\nبرای لغو /cancel را بفرست.",
        _keyboard([[_button("بازگشت", "buy:discount", "back")]]))


@router.message(BuyStates.waiting_discount_code, F.text != "/cancel")
async def process_discount_code(message: Message, state: FSMContext):
    user = message.from_user
    if not user:
        return
    data = await state.get_data()
    if int(data.get("user_id", 0)) != user.id:
        return
    if not rate_limiter.is_allowed(user.id):
        await _edit_user_message(message, state, "لطفاً کمی صبر کن و دوباره تلاش کن.",
            _keyboard([[_button("بازگشت", "buy:discount", "back")]]))
        return
    code_text = (message.text or "").strip().upper()
    if not validate_discount_code(code_text):
        await _edit_user_message(message, state,
            f"{premium_emoji('warning', '●')} <b>فرمت کد تخفیف معتبر نیست.</b>\nفقط حروف انگلیسی، عدد، خط تیره و زیرخط مجاز است.",
            _keyboard([[_button("بازگشت", "buy:discount", "back")]]))
        return
    try:
        code = get_discount_code(code_text)
    except Exception:
        logger.exception("Discount lookup failed")
        await _edit_user_message(message, state, "بررسی کد تخفیف ناموفق بود. دوباره تلاش کن.")
        return
    if (not code or not code.get("is_active") or int(code.get("used_count", 0)) >= int(code.get("max_uses", 0))
            or not discount_usable_by(code, user.id)):
        await _edit_user_message(message, state,
            f"{premium_emoji('warning', '●')} <b>کد تخفیف نامعتبر، غیرفعال یا تکمیل‌شده است.</b> دوباره ارسال کن.",
            _keyboard([[_button("بازگشت", "buy:discount", "back")]]))
        return
    await state.update_data(discount_code=code_text, discount_id=int(code["id"]), discount_percent=int(code["percent"]))
    await state.set_state(None)
    await _show_invoice(bot=message.bot, state=state)


@router.callback_query(F.data == "buy:discount:skip")
async def skip_discount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    await state.update_data(discount_code=None, discount_id=None, discount_percent=0)
    await _show_invoice(bot=callback.bot, state=state,
        source_message=callback.message if isinstance(callback.message, Message) else None)


@router.callback_query(F.data == "buy:discount")
async def back_to_discount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _current_purchase(callback, state):
        return
    await state.set_state(None)
    await _show_discount_step(callback, state)


@router.callback_query(F.data == "buy:pay:wallet")
async def pay_with_wallet(callback: CallbackQuery, state: FSMContext):
    user = callback.from_user
    if not user:
        await callback.answer()
        return
    if not await _current_purchase(callback, state):
        return
    lock = _user_lock(user.id)
    if lock.locked():
        await callback.answer("سفارش شما در حال پردازش است؛ کمی صبر کن.", show_alert=True)
        return
    await callback.answer()

    async with lock:
        data = await state.get_data()
        if data.get("purchase_pending") or user.id in _pending_purchase_users:
            await _edit_callback(callback, state,
                f"{premium_emoji('warning', '●')} <b>این سفارش نیازمند بررسی است.</b>\nامکان پرداخت مجدد وجود ندارد؛ با پشتیبانی تماس بگیر.")
            return
        if data.get("purchase_processing"):
            return
        if not _valid_purchase_data(data):
            await _edit_callback(callback, state, "اطلاعات سفارش نامعتبر است. از ابتدا شروع کن.", buy_type_selection_keyboard())
            await state.clear()
            return
        if data.get("discount_id") or data.get("discount_code"):
            try:
                _validate_order_discount({**data, "user_id": user.id})
            except ValueError as exc:
                await state.update_data(discount_id=None, discount_code=None, discount_percent=0)
                data = await state.get_data()
                try:
                    fresh_price = _build_order_prices({**data, "user_id": user.id})[0]
                except Exception:
                    fresh_price = 0
                await _edit_callback(callback, state,
                    f"{premium_emoji('warning', '●')} <b>{_safe_text(_user_error_text(exc))}</b>\n\n"
                    "کد تخفیف از سفارش حذف شد. فاکتور را دوباره بگیر.",
                    purchase_retry_keyboard(fresh_price))
                return
        try:
            price_toman, _stars, _discount, _discount_id = _build_order_prices({**data, "user_id": user.id})
            balance = get_balance(user.id)
        except Exception as exc:
            logger.exception("Could not validate payment")
            send_alert("ERROR", "خطای اعتبارسنجی پرداخت قبل از کسر", exc=exc, user_id=user.id,
                       product_id=data.get("product_id"), order=data.get("idempotency_key"))
            await _edit_callback(callback, state, "بررسی مبلغ یا موجودی ناموفق بود. دوباره فاکتور بگیر.")
            return
        if balance < price_toman:
            await _edit_callback(callback, state,
                f"{premium_emoji('wallet', '●')} <b>موجودی کیف پول کافی نیست.</b>\n\n"
                f"موجودی فعلی: <b>{int(balance):,} تومان</b>\nمبلغ سفارش: <b>{price_toman:,} تومان</b>\n\n"
                "کیف پول را شارژ کن و دوباره تلاش کن.", purchase_retry_keyboard(price_toman))
            return

        await state.update_data(purchase_processing=True)
        await _edit_callback(callback, state,
            f"{premium_emoji('light', '●')} <b>در حال ثبت سفارش...</b>\n\nدر حال کسر موجودی و ساخت سرویس در پنل هستیم. لطفاً صبر کن.")

        order_key = str(data.get("idempotency_key") or "")
        try:
            _persist_pending_purchase(user.id, order_key, "processing")
        except Exception as exc:
            logger.exception("Could not persist purchase lock before payment")
            send_alert("ERROR", "ذخیرهٔ قفل خرید در دیتابیس ناموفق (دیتابیس قفل/خراب؟)", exc=exc,
                       user_id=user.id, order=order_key)
            await _edit_callback(callback, state,
                "ثبت امن سفارش در دیتابیس ناموفق بود؛ برای جلوگیری از کسر وجه، پرداخت انجام نشد.")
            await state.update_data(purchase_processing=False)
            return

        status, result = await _provision_purchase(user_id=user.id, data=data, telegram_username=user.username)
        if status == "success":
            result = result if isinstance(result, dict) else {}
            try:
                _clear_pending_purchase(user.id)
            except Exception:
                logger.exception("Could not clear persistent purchase marker after success")
            await state.clear()
            success_text = (
                f"{premium_emoji('true', '●')} <b>خرید سرویس با موفقیت انجام شد.</b>\n\n"
                f"<b>نام کانفیگ:</b>\n<code>{_safe_text(result.get('config_name'))}</code>\n\n"
                f"<b>لینک اشتراک:</b>\n<code>{_safe_text(result.get('link'))}</code>\n\n"
                f"{premium_emoji('safe', '●')} لینک اشتراک محرمانه است؛ آن را در اختیار دیگران قرار نده."
            )
            success_kb = purchase_success_keyboard(int(result.get("subscription_id", 0)))
            try:
                await callback.message.edit_text(success_text, reply_markup=success_kb)
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.exception("Could not display purchase success")
                    # سرویس تحویل داده شده؛ اگر ویرایش پیام شکست خورد، لینک به‌صورت پیام جدید برود
                    try:
                        await callback.message.answer(success_text, reply_markup=success_kb)
                    except Exception:
                        send_alert("CRITICAL", "نمایش لینک سرویس به کاربر ناموفق (خرید موفق بود)",
                                   exc=exc, user_id=user.id, username=user.username,
                                   config=result.get("config_name"))

            # ارسال اعلان خرید موفق به کانال
            try:
                await notify_purchase(
                    bot=callback.bot,
                    user=user,
                    config_name=result.get("config_name"),
                    kind=result.get("kind"),
                    volume_gb=result.get("volume_gb", 0),
                    days=result.get("days", 0),
                    devices=result.get("devices", 0),
                    amount=result.get("price_toman", 0),
                )
            except Exception as exc:
                logger.exception("Purchase channel notification failed")
                send_alert("WARNING", "ارسال اعلان خرید به کانال ناموفق (خرید موفق بود)", exc=exc,
                           user_id=user.id, config=result.get("config_name"))

            return
        if status == "pending":
            send_alert("ERROR", f"سفارش در وضعیت «نیازمند بررسی»؛ خریدهای جدید این کاربر مسدود شد: {result}",
                       user_id=user.id, order=order_key, product_id=data.get("product_id"))
            _pending_purchase_users.add(user.id)
            try:
                _persist_pending_purchase(user.id, order_key, "pending_review")
            except Exception:
                logger.exception("Could not persist pending-review marker")
            await state.update_data(purchase_processing=False, purchase_pending=True)
            await _edit_callback(callback, state,
                f"{premium_emoji('warning', '●')} <b>سفارش نیازمند بررسی است.</b>\n\n{_safe_text(result)}")
            return
        try:
            _clear_pending_purchase(user.id)
        except Exception:
            logger.exception("Could not clear purchase marker after definitive failure")
        stale = {}
        if data.get("discount_id") and _discount_for_order({**data, "user_id": user.id})[1] is None:
            stale = dict(discount_id=None, discount_code=None, discount_percent=0)
            price_toman = _build_order_prices({**data, **stale, "user_id": user.id})[0] if _valid_purchase_data(data) else price_toman
        await state.update_data(purchase_processing=False, purchase_pending=False,
            idempotency_key=generate_idempotency_key(f"buy-{user.id}"), **stale)
        await _edit_callback(callback, state,
            f"{premium_emoji('warning', '●')} <b>{_safe_text(result)}</b>\n\nدر صورت کسر وجه، مبلغ به کیف پولت بازگردانده شده است.",
            purchase_retry_keyboard(price_toman))


@router.callback_query(F.data.startswith("buy:configs:"))
async def send_all_subscription_configs(callback: CallbackQuery):
    await callback.answer()
    owner_id = callback.from_user.id if callback.from_user else 0

    try:
        subscription_id = int(callback.data.rsplit(":", 1)[-1])
        if subscription_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await callback.message.answer("شناسه اشتراک معتبر نیست.")
        return

    try:
        owned_subscriptions = get_user_subscriptions(owner_id, only_visible=False)
        subscription = next(
            (item for item in owned_subscriptions if int(item.get("id") or 0) == subscription_id),
            None,
        )
    except Exception:
        logger.exception("Subscription ownership lookup failed")
        subscription = None

    if not subscription:
        send_alert(
            "SECURITY",
            "تلاش برای دریافت کانفیگ اشتراک دیگران یا اشتراک ناموجود",
            user_id=owner_id,
            username=callback.from_user.username if callback.from_user else None,
            subscription_id=subscription_id,
        )
        await callback.message.answer("این اشتراک متعلق به شما نیست یا در دسترس نیست.")
        return

    try:
        panel_id = int(subscription.get("panel_id") or 0)
        config_name = str(subscription.get("config_name") or "").strip()
        if panel_id <= 0 or not config_name:
            raise ValueError("Subscription panel data is incomplete")

        from pasar_guard import get_full_config
        response = await get_full_config(config_name, panel_id)
        links = response.get("links", []) if isinstance(response, dict) else []
        if not isinstance(links, list) or not links:
            await callback.message.answer(
                f"{premium_emoji('warning', '●')} فعلاً کانفیگی از پنل دریافت نشد. کمی بعد دوباره تلاش کن."
            )
            return

        sent = 0
        for index, value in enumerate(links, start=1):
            link = str(value or "").strip()
            if not _valid_config_uri(link):
                logger.warning(
                    "Invalid config URI returned for subscription %s, item %s",
                    subscription_id,
                    index,
                )
                continue
            await callback.message.answer(
                f"{premium_emoji('world', '●')} <b>کانفیگ {index}</b>\n\n<code>{_safe_text(link)}</code>"
            )
            sent += 1

        if sent:
            await callback.message.answer(
                f"{premium_emoji('true', '●')} <b>{sent} کانفیگ با موفقیت ارسال شد.</b>\nهر کانفیگ را جداگانه کپی کن."
            )
        else:
            await callback.message.answer("هیچ لینک کانفیگ معتبری در پاسخ پنل نبود.")

    except Exception as exc:
        logger.exception("Could not retrieve configs for subscription %s", subscription_id)
        send_alert(
            "ERROR",
            "دریافت کانفیگ‌ها از پنل ناموفق",
            exc=exc,
            user_id=owner_id,
            username=callback.from_user.username if callback.from_user else None,
            subscription_id=subscription_id,
            panel_id=subscription.get("panel_id"),
            config=subscription.get("config_name"),
        )
        await callback.message.answer(
            f"{premium_emoji('warning', '●')} دریافت کانفیگ‌ها از پنل ناموفق بود. کمی بعد دوباره تلاش کن."
        )


@router.message(BuyStates.waiting_discount_code, F.text == "/cancel")
async def cancel_buy(message: Message, state: FSMContext):
    data = await state.get_data()
    if int(data.get("user_id", 0)) != (message.from_user.id if message.from_user else 0):
        return
    await _edit_user_message(message, state,
        f"{premium_emoji('warning', '●')} <b>ورود کد تخفیف لغو شد.</b>\nبرای شروع دوباره از منوی خرید وارد شو.")
    await state.clear()
