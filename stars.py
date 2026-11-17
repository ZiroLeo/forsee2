import logging
import uuid

from dev.alerts import send_alert
from aiogram import Router, F
from aiogram.types import (
    Message, PreCheckoutQuery, LabeledPrice, CallbackQuery
)

from config import ADMIN_ID, STARS_TO_TOMAN_RATE
from database import (
    get_user, create_stars_invoice, get_stars_invoice, update_stars_invoice_status,
    register_stars_charge, update_stars_charge_status, create_subscription,
    reserve_config_number, release_config_number, increment_total_purchases,
    add_referral_commission, log_event, hard_delete_subscription,
)
from paxiva import PaxivaClient, PaxivaError
from rich import premium_emoji
from utils import generate_idempotency_key

logger = logging.getLogger(__name__)
router = Router(name="stars_payment")


# ============================================================
# شروع پرداخت با Stars
# ============================================================

@router.callback_query(F.data == "buy:pay:stars")
async def pay_with_stars(callback: CallbackQuery, state, bot):
    user = callback.from_user
    if not user:
        await callback.answer()
        return

    # خواندن اطلاعات سفارش از state (که در buy.py ذخیره شده)
    data = await state.get_data()
    volume_gb = data.get("volume_gb", 0)
    days = data.get("days", 30)
    devices = data.get("devices", 2)
    price_stars = data.get("price_stars", 0)

    if price_stars <= 0:
        await callback.answer("خطا در محاسبه قیمت Stars.", show_alert=True)
        return

    # تولید payload یکتا
    payload = f"stars-{user.id}-{uuid.uuid4().hex[:12]}"

    inv_id = create_stars_invoice(
        payload=payload,
        telegram_id=user.id,
        volume_gb=volume_gb,
        days=days,
        devices=devices,
        amount_stars=price_stars,
    )
    if not inv_id:
        await callback.answer("خطا در ایجاد فاکتور.", show_alert=True)
        return

    try:
        await bot.send_invoice(
            chat_id=user.id,
            title="خرید کانفیگ VPN",
            description=f"حجم: {volume_gb if volume_gb > 0 else 'بی‌نهایت'} گیگ | مدت: {days} روز | دستگاه: {devices}",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="کانفیگ VPN", amount=price_stars)],
            start_parameter="buy_stars",
        )
        await callback.answer("فاکتور ارسال شد. لطفاً پرداخت کنید.")
    except Exception:
        logger.exception("Failed to send stars invoice")
        send_alert("ERROR", f"خطا در ارسال فاکتور Stars\nکاربر: {user.id}\nprice_stars: {price_stars}")
        await callback.answer("خطا در ارسال فاکتور.", show_alert=True)


# ============================================================
# pre_checkout_query
# ============================================================

@router.pre_checkout_query()
async def pre_checkout(pre_checkout: PreCheckoutQuery):
    payload = pre_checkout.invoice_payload
    inv = get_stars_invoice(payload)

    if not inv or inv["status"] != "pending":
        await pre_checkout.answer(ok=False, error_message="فاکتور نامعتبر یا منقضی شده است.")
        return

    if inv["amount_stars"] != pre_checkout.total_amount:
        await pre_checkout.answer(ok=False, error_message="مبلغ فاکتور با انتظار مطابقت ندارد.")
        log_event("SECURITY", "stars_amount_mismatch", inv["telegram_id"], f"payload={payload}")
        send_alert(
            "SECURITY",
            f"عدم تطابق مبلغ Stars\n"
            f"کاربر: {inv['telegram_id']}\n"
            f"payload: {payload}\n"
            f"expected: {inv['amount_stars']}\n"
            f"received: {pre_checkout.total_amount}"
        )
        return

    await pre_checkout.answer(ok=True)


# ============================================================
# successful_payment
# ============================================================

@router.message(F.successful_payment)
async def successful_payment(message: Message, paxiva: PaxivaClient, bot):
    payment = message.successful_payment
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id
    user_id = message.from_user.id

    # ۱. ضد تکرار
    is_new = register_stars_charge(
        charge_id=charge_id,
        telegram_id=user_id,
        payload=payload,
        amount_stars=payment.total_amount,
    )
    if not is_new:
        logger.warning("Duplicate stars charge: %s", charge_id)
        return

    # ۲. خواندن فاکتور
    inv = get_stars_invoice(payload)
    if not inv or inv["status"] != "pending":
        logger.error("Invalid stars invoice: %s", payload)
        await _refund(bot, user_id, charge_id, message)
        return

    update_stars_invoice_status(payload, "paid")

    # ۳. ساخت کانفیگ
    config_number = reserve_config_number(is_test=False)
    if not config_number:
        logger.error("Could not reserve config number for stars purchase")
        send_alert("ERROR", f"رزرو شماره کانفیگ ناموفق (Stars)\nکاربر: {user_id}\npayload: {payload}")
        update_stars_invoice_status(payload, "refunded")
        await _refund(bot, user_id, charge_id, message)
        return

    config_name = f"iMan-{config_number}"
    idem_key = generate_idempotency_key(f"stars-{user_id}-{config_number}")
    subscription_id = None
    client_id = None

    try:
        client_data = await paxiva.create_client(
            name=config_name,
            volume_gb=inv["volume_gb"],
            days=inv["days"],
            devices=inv["devices"],
            idempotency_key=idem_key,
        )
        client_id = client_data.get("id")
        link = client_data.get("link", "")

        if not client_id:
            raise RuntimeError("PAXIVA response has no client ID")

        subscription_id = create_subscription(
            telegram_id=user_id,
            config_number=config_number,
            config_name=config_name,
            kind="vol",
            volume_gb=inv["volume_gb"],
            days=inv["days"],
            devices=inv["devices"],
            price_toman=0,
            price_stars=inv["amount_stars"],
            paxiva_client_id=client_id,
            link=link,
        )

        if not subscription_id:
            send_alert("ERROR", f"ذخیره اشتراک در دیتابیس ناموفق (Stars)\nکاربر: {user_id}\nconfig: {config_name}\npayload: {payload}")
            raise RuntimeError("Could not save subscription")

        increment_total_purchases(user_id)

        # پورسانت Stars: تبدیل به تومان با نرخ STARS_TO_TOMAN_RATE
        try:
            commission_toman = int(inv["amount_stars"] * STARS_TO_TOMAN_RATE * 0.05)
            if commission_toman > 0:
                add_referral_commission(user_id, commission_toman, subscription_id, "stars_purchase")
        except Exception:
            logger.exception("Referral commission failed for stars purchase")
            send_alert("WARNING", f"خطای پورسانت رفرال (Stars)\nکاربر: {user_id}\nsub_id: {subscription_id}\npayload: {payload}")

        update_stars_invoice_status(payload, "delivered")
        update_stars_charge_status(charge_id, "delivered")

        success_text = (
            f"{premium_emoji('true', '✅')} <b>پرداخت با Stars با موفقیت انجام شد!</b>\n\n"
            f"🔹 کانفیگ: <code>{config_name}</code>\n"
            f"📦 حجم: {inv['volume_gb'] if inv['volume_gb'] > 0 else 'بی‌نهایت'} گیگ\n"
            f"📅 مدت: {inv['days']} روز\n"
            f"🔗 لینک:\n<code>{link}</code>"
        )
        await message.answer(success_text, parse_mode="HTML")
        log_event("INFO", "stars_purchase_success", user_id, f"sub_id={subscription_id}, stars={inv['amount_stars']}")

    except PaxivaError as e:
        logger.error("PAXIVA error after stars payment: %s", e)
        send_alert("ERROR", f"خطای PAXIVA در پرداخت Stars\nکاربر: {user_id}\nخطا: {e.message}\npayload: {payload}")
        if client_id:
            try:
                await paxiva.delete_client(client_id, purge=True)
            except Exception:
                logger.exception("Could not delete remote client after stars failure")
                send_alert(
                    "ERROR",
                    f"حذف سرویس از پنل ناموفق (Stars)\n"
                    f"کاربر: {user_id}\n"
                    f"client_id: {client_id}\n"
                    f"payload: {payload}"
                )
        if subscription_id:
            try:
                hard_delete_subscription(subscription_id)
            except Exception:
                logger.exception("Could not rollback subscription after stars failure")
                send_alert(
                    "ERROR",
                    f"پاک‌سازی اشتراک محلی ناموفق (Stars)\n"
                    f"کاربر: {user_id}\n"
                    f"sub_id: {subscription_id}\n"
                    f"payload: {payload}"
                )
        release_config_number(config_number, is_test=False)
        update_stars_invoice_status(payload, "refunded")
        await _refund(bot, user_id, charge_id, message)
        log_event("ERROR", "stars_paxiva_failed", user_id, f"payload={payload}: {e.message}")

    except Exception as e:
        logger.exception("Unexpected error after stars payment")
        if client_id:
            try:
                await paxiva.delete_client(client_id, purge=True)
            except Exception:
                logger.exception("Could not delete remote client after stars failure")
        if subscription_id:
            try:
                hard_delete_subscription(subscription_id)
            except Exception:
                logger.exception("Could not rollback subscription after stars failure")
        release_config_number(config_number, is_test=False)
        update_stars_invoice_status(payload, "refunded")
        await _refund(bot, user_id, charge_id, message)
        log_event("ERROR", "stars_unexpected", user_id, f"payload={payload}: {str(e)}")


# ============================================================
# Refund
# ============================================================

async def _refund(bot, user_id: int, charge_id: str, message: Message = None):
    try:
        ok = await bot.refund_star_payment(
            user_id=user_id,
            telegram_payment_charge_id=charge_id,
        )
        if ok:
            update_stars_charge_status(charge_id, "refunded")
            if message:
                await message.answer(
                    "⚠️ ساخت کانفیگ انجام نشد.\n✅ مبلغ Stars شما بازگشت داده شد."
                )
            log_event("INFO", "stars_refund_ok", user_id, f"charge={charge_id}")
        else:
            logger.error("Refund failed for user %s charge=%s", user_id, charge_id)
            send_alert("ERROR", f"بازگشت Stars ناموفق\nکاربر: {user_id}\ncharge: {charge_id}")
            if message:
                await message.answer(
                    "⚠️ ساخت کانفیگ انجام نشد و بازگشت مبلغ با خطا مواجه شد.\nلطفاً با پشتیبانی تماس بگیرید."
                )
            log_event("ERROR", "stars_refund_failed", user_id, f"charge={charge_id}")
    except Exception:
        logger.exception("Refund exception")
        log_event("ERROR", "stars_refund_exception", user_id, f"charge={charge_id}")
        send_alert(
            "ERROR",
            f"خطای غیرمنتظره در بازگشت Stars\n"
            f"کاربر: {user_id}\n"
            f"charge: {charge_id}"
        )