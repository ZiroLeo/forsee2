export interface InitialAuditIssue {
  code: string;
  title: string;
  severity: "critical" | "high" | "medium" | "low";
  category: "integration" | "payment" | "database" | "security" | "fsm_router" | "cleanup";
  filePath: string;
  relatedFiles: string;
  lineRange: string;
  description: string;
  impact: string;
  brokenCode: string;
  fixedCode: string;
  status: "open" | "in_progress" | "resolved" | "ignored";
  developerNote: string;
}
export interface InitialProjectFile {
  winPath: string;
  relPath: string;
  folder: string;
  linesCount: number;
  healthStatus: "critical" | "warning" | "healthy" | "deprecated";
  roleSummary: string;
  importsList: string;
  callbacksList: string;
  auditSummary: string;
}
export const INITIAL_AUDIT_ISSUES: InitialAuditIssue[] = [
  {
    code: "BUG-01",
    title: "مسدود شدن «ادمین رسید» در BehaviorGuardMiddleware هنگام تأیید یا رد واریزی",
    severity: "critical",
    category: "security",
    filePath: "middlewares/behavior_guard.py",
    relatedFiles: "handlers/admin/receipts.py, handlers/wallet.py, bot.py",
    lineRange: "خطوط ۵۱ تا ۶۰",
    description:
      "در handlers/admin/receipts.py امکانی طراحی شده که ادمین اصلی بتواند یک شخص دیگر را به‌عنوان «ادمین رسید» (receipt_admin_id) تعیین کند تا رسیدهای کارت‌به‌کارت فقط برای او ارسال شوند. دکمه‌های زیر هر رسید دارای callback_data با پیشوند admin:pay:approve:{id} و admin:pay:reject:{id} هستند. اما در middlewares/behavior_guard.py خط ۵۱ شرط if user.id != ADMIN_ID نوشته شده که هر کال‌بکی با پیشوند admin: را بلافاصله مسدود کرده و به DEV_ID هشدار نفوذ می‌دهد!",
    impact:
      "اگر ادمین رسید شخصی غیر از ADMIN_ID باشد، با زدن دکمه «تأیید و شارژ» یا «رد درخواست»، میدلور جلوی اجرای هندلر را می‌گیرد، هیچ رسیدی تأیید نمی‌شود و ادمین رسید به‌عنوان مهاجم گزارش می‌شود.",
    brokenCode: `# middlewares/behavior_guard.py (خطوط ۵۱ تا ۶۰)
if user.id != ADMIN_ID:
    # اگر کاربر عادی سعی کند دستور /admin یا کال‌بک‌های ادمین را بزند
    if text.startswith("/admin") or callback_data.startswith("admin:"):
        await self._report_and_block(
            user.id,
            "تلاش برای دسترسی به پنل ادمین",
            f"متن/کالبک: {text[:100]}"
        )
        return  # مسدود کردن کامل`,
    fixedCode: `# middlewares/behavior_guard.py (اصلاح‌شده با پشتیبانی از ادمین رسید)
from database import is_banned, log_event, get_receipt_admin_id
if user.id != ADMIN_ID:
    receipt_admin_id = get_receipt_admin_id(ADMIN_ID)
    is_receipt_callback = callback_data.startswith(("admin:pay:approve:", "admin:pay:reject:"))
    if not (user.id == receipt_admin_id and is_receipt_callback):
        if text.startswith("/admin") or callback_data.startswith("admin:"):
            await self._report_and_block(
                user.id,
                "تلاش برای دسترسی به پنل ادمین",
                f"متن/کالبک: {text[:100]}"
            )
            return`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-02",
    title: "کرش ۱۰۰٪ بخش «بک‌آپ دیتابیس» به دلیل ۳ خطای هم‌زمان در backup.py",
    severity: "critical",
    category: "integration",
    filePath: "handlers/admin/backup.py",
    relatedFiles: "rich.py, database.py",
    lineRange: "خطوط ۲۶ تا ۴۳",
    description:
      "در فایل handlers/admin/backup.py سه اشکال وجود دارد: ۱) در خط ۴۱ به تابع build_rich_message یک رشته (str) پاس داده شده، در حالی که build_rich_message در rich.py خط ۳۵۹ فقط لیست پاراگراف‌ها (List[Any]) قبول می‌کند و بلافاصله TypeError می‌دهد. ۲) در خط ۲۹ به callback.answer پارامتر parse_mode='HTML' داده شده که در aiogram 3 روی answerCallbackQuery وجود ندارد و باعث TypeError می‌شود. ۳) در خط ۳۹ از shutil.copy2 به جای تابع امن backup_database در database.py (مخصوص حالت WAL) استفاده شده است.",
    impact:
      "کلیک روی دکمه «بک‌آپ دیتابیس» در پنل ادمین همیشه قبل از ارسال فایل در خط ۴۱ کرش می‌کند و پیام «خطا در بک‌آپ» نمایش می‌دهد.",
    brokenCode: `# handlers/admin/backup.py (خطوط ۲۵ تا ۴۹)
    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
            f"{premium_emoji('light', '⏳')} لطفاً کمی صبر کنید.",
            show_alert=True,
            parse_mode="HTML"
        )
        return
...
        shutil.copy2(DB_PATH, backup_path)
        await callback.message.edit_text(rich_message=build_rich_message(
            f"{premium_emoji('safe', '🛡')} **بک‌آپ دیتابیس در حال ارسال...**"
        ))`,
    fixedCode: `# handlers/admin/backup.py (اصلاح‌شده)
from database import log_event, backup_database
    if not rate_limiter.is_allowed(user.id):
        await callback.answer("⏳ لطفاً کمی صبر کنید.", show_alert=True)
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"bot_backup_{timestamp}.db"
        # بک‌آپ امن سازگار با WAL از طریق API رسمی SQLite
        backup_database(backup_path)
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=FSInputFile(backup_path),
            caption=f"{premium_emoji('safe', '🛡')} <b>بک‌آپ دیتابیس</b>\\n🕒 <code>{timestamp}</code>",
            parse_mode="HTML",
        )`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-03",
    title: "وابستگی bot.py به dev/panel.py که قصد حذف آن را دارید",
    severity: "critical",
    category: "cleanup",
    filePath: "bot.py",
    relatedFiles: "dev/panel.py",
    lineRange: "خطوط ۵۳ و ۳۰۳",
    description:
      "در bot.py خط ۵۳ دستور from dev import panel as dev_panel و در خط ۳۰۳ دستور dp.include_router(dev_panel.router) قرار دارد. همچنین در ریپازیتوری گیت‌هاب شما، فایل dev/panel.py اصلاً آپلود نشده (چون هم‌نام با handlers/admin/panel.py بوده است). اگر dev/panel.py را حذف کنید یا استفاده نکنید، بدون ویرایش bot.py ربات در بدو اجرا با ImportError متوقف می‌شود.",
    impact:
      "خطای ImportError: cannot import name 'panel' from 'dev' در لحظه اجرای python bot.py.",
    brokenCode: `# bot.py (خط ۵۳ و خط ۳۰۳)
from dev import panel as dev_panel
...
    # DEVELOPER ROUTER
    dp.include_router(dev_panel.router)`,
    fixedCode: `# bot.py (حذف ایمپورت و روتر dev_panel)
# خط ۵۳ (from dev import panel as dev_panel) و خط ۳۰۳ (dp.include_router(dev_panel.router)) را حذف کنید.
# توجه: dev/alerts.py همچنان برای ارسال هشدارهای امنیتی و خطا به DEV_ID فعال می‌ماند.`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-04",
    title: "تکرار و تداخل کامل handlers/payments/card.py با handlers/wallet.py",
    severity: "critical",
    category: "fsm_router",
    filePath: "handlers/payments/card.py",
    relatedFiles: "handlers/wallet.py, bot.py",
    lineRange: "خطوط ۳۰، ۱۶۶، ۲۰۱ در card.py و ۲۶۶، ۲۷۷ در bot.py",
    description:
      "فرآیند کامل شارژ کارت‌به‌کارت (کال‌بک wallet:charge، دریافت مبلغ، دریافت عکس رسید، و کال‌بک‌های admin:pay:approve: و admin:pay:reject:) به صورت کامل و استاندارد در handlers/wallet.py پیاده‌سازی شده است. اما فایل قدیمی handlers/payments/card.py هم دقیقاً همان کال‌بک‌ها را ثبت کرده و در bot.py خط ۲۷۷ اضافه شده است! علاوه بر این، داخل card.py خطوط ۴۶ و ۷۷ تابع build_rich_message(text) با رشته صدا زده شده که در صورت اجرا کرش می‌کند.",
    impact:
      "به دلیل ثبت زودتر wallet.router در bot.py، فایل card.py کاملاً Shadow شده است و اگر ترتیب روترها عوض شود، شارژ کیف پول به دلیل باگ build_rich_message(text) در card.py از کار می‌افتد.",
    brokenCode: `# bot.py (خطوط ۲۶۶ و ۲۷۷)
dp.include_router(wallet.router)
...
dp.include_router(card.router)  # تکراری و دارای باگ build_rich_message(str)`,
    fixedCode: `# bot.py
# در خط ۳۴ ایمپورت card را حذف کنید:
from handlers.payments import stars
# و در خط ۲۷۷ فقط stars.router را ثبت کنید (شارژ کارت‌به‌کارت در wallet.router کامل است):
dp.include_router(stars.router)`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-05",
    title: "ثبت اشتباه سرویس نامحدود به‌عنوان حجمی + عدم مصرف کد تخفیف + پورسانت ۰.۲۵٪ در خرید با Stars",
    severity: "critical",
    category: "payment",
    filePath: "handlers/payments/stars.py",
    relatedFiles: "handlers/buy.py, database.py",
    lineRange: "خطوط ۳۷ تا ۱۹۵",
    description:
      "در handlers/payments/stars.py سه باگ مهم منطقی وجود دارد: ۱) در خطوط ۱۵۵ و ۱۷۲، نوع سرویس همیشه kind='vol' (حجمی) هاردکد شده است؛ یعنی اگر کاربر سرویس «نامحدود» را با Stars بخرد، در پنل و دیتابیس سرویس حجمی ثبت می‌شود! ۲) اگر کاربر در buy.py کد تخفیف زده باشد، در stars.py تابع consume_discount_code اصلاً صدا زده نمی‌شود و کد تخفیف یک‌بارمصرف قابل استفاده مجدد می‌ماند. ۳) در خط ۱۹۰ مبلغ پورسانت در 0.05 ضرب شده و به add_referral_commission داده می‌شود، در حالی که add_referral_commission در database.py خط ۸۸۱ خودش ورودی را دوباره در 0.05 ضرب می‌کند (پورسانت ۰.۲۵٪ به جای ۵٪)!",
    impact:
      "سرویس‌های نامحدود خریداری‌شده با Stars به‌اشتباه حجمی ثبت می‌شوند، کدهای تخفیف در خرید با Stars نمی‌سوزند و پورسانت معرف ۲۰ برابر کمتر واریز می‌شود.",
    brokenCode: `# handlers/payments/stars.py (خطوط ۱۷۲ و ۱۹۰)
subscription_id = create_subscription(
    telegram_id=user_id,
    config_number=config_number,
    config_name=config_name,
    kind="vol",  # <-- همیشه vol هاردکد شده!
...
)
# پورسانت Stars: دوبار ضرب در 0.05 می‌شود!
commission_toman = int(inv["amount_stars"] * STARS_TO_TOMAN_RATE * 0.05)
if commission_toman > 0:
    add_referral_commission(user_id, commission_toman, subscription_id, "stars_purchase")`,
    fixedCode: `# handlers/payments/stars.py (اصلاح‌شده)
# ۱. پاس دادن kind واقعی سرویس ('unl' یا 'vol') به paxiva.create_client و create_subscription
# ۲. مصرف کد تخفیف در صورت وجود:
if discount_id is not None:
    consume_discount_code(int(discount_id), user_id)
# ۳. ارسال مبلغ کل خرید به تومان (بدون ضرب در 0.05، چون database.py خودش ۵٪ را حساب می‌کند):
purchase_toman_equiv = int(inv["amount_stars"] * STARS_TO_TOMAN_RATE)
if purchase_toman_equiv > 0:
    add_referral_commission(user_id, purchase_toman_equiv, subscription_id, "stars_purchase")`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-06",
    title: "نادیده گرفته شدن قیمت و مدت محصولات پنل ادمین (products.py) در فرآیند خرید (buy.py)",
    severity: "critical",
    category: "integration",
    filePath: "handlers/buy.py",
    relatedFiles: "handlers/admin/products.py, pricing.py, database.py",
    lineRange: "خطوط ۳۵۸ تا ۳۸۰ و ۷۶۵ تا ۷۷۶",
    description:
      "در پنل ادمین (handlers/admin/products.py)، ادمین برای هر محصول فیلدهای days، price_toman و price_stars را تنظیم یا ویرایش می‌کند (مثلاً تغییر قیمت تومانی یا استارز یک محصول خاص). اما در handlers/buy.py خط ۷۷۴ مدت محصول نادیده گرفته شده و همیشه به months=1, days=30 ریست می‌شود، و در خط ۳۷۱ (_build_order_prices) قیمت ذخیره‌شده در جدول products (یعنی product['price_toman'] و product['price_stars']) اصلاً خوانده نمی‌شود و همیشه از فرمول کلی pricing.py محاسبه می‌شود! همچنین اگر محصول حجمی با حجم 0 (بی‌نهایت که در products.py خط ۲۶۱ مجاز است) ساخته شود، در buy.py خط ۳۷۸ قیمت 0 شده و خطای «محاسبه قیمت ناموفق بود» می‌دهد.",
    impact:
      "هر قیمتی که ادمین در بخش «مدیریت محصولات» برای یک محصول تعیین یا ویرایش کند، در فاکتور خرید کاربر هیچ اثری ندارد.",
    brokenCode: `# handlers/buy.py (خطوط ۳۶۸ تا ۳۷۴)
    key = "unl_inf" if product["kind"] == "unl" and volume <= 0 else (
        "unl150" if product["kind"] == "unl" else f"vol_{int(volume)}"
    )
    toman = order_price_toman(key, volume, days, devices)
    stars = order_price_stars(key, volume, days, devices)`,
    fixedCode: `# handlers/buy.py (استفاده از قیمت پایه محصول ثبت‌شده توسط ادمین + هزینه ماه/دستگاه اضافه)
    extra_devices = max(0, devices - MAX_DEVICES_DEFAULT)
    base_days = int(product.get("days") or 30)
    extra_months = max(0, (days // 30) - max(1, base_days // 30)) if days > 0 else 0
    dev_key = "unl_device" if product["kind"] == "unl" else "vol_device"
    mon_key = "unl_month" if product["kind"] == "unl" else "vol_month"
    if int(product.get("price_toman") or 0) > 0 and (days > 0 or base_days == 0):
        toman = int(product["price_toman"]) + extra_months * get_price(mon_key, "toman") + extra_devices * get_price(dev_key, "toman")
        stars = int(product["price_stars"]) + extra_months * get_price(mon_key, "stars") + extra_devices * get_price(dev_key, "stars")
    else:
        key = "unl_inf" if product["kind"] == "unl" and volume <= 0 else ("unl150" if product["kind"] == "unl" else f"vol_{int(volume)}")
        toman = order_price_toman(key, volume, days, devices)
        stars = order_price_stars(key, volume, days, devices)`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-07",
    title: "عدم امکان تمدید سرویس‌های خریداری‌شده با Stars، گردونه یا بدون محدودیت زمانی",
    severity: "high",
    category: "payment",
    filePath: "handlers/renew.py",
    relatedFiles: "services/scheduler.py, handlers/payments/stars.py, pricing.py",
    lineRange: "خطوط ۶۵۳ تا ۶۶۰ و ۶۹۹ تا ۷۰۶ در renew.py و خط ۱۷۱ در scheduler.py",
    description:
      "در handlers/renew.py خط ۶۵۳ و services/scheduler.py خط ۱۷۱، قیمت تمدید مستقیماً از ستون sub['price_toman'] خوانده می‌شود و اگر price <= 0 باشد با خطای «قیمت تمدید نامعتبر است» متوقف می‌شود. اما برای تمام سرویس‌هایی که با Stars خریده شده‌اند (stars.py خط ۱۷۶: price_toman=0) یا از گردونه شانس برنده شده‌اند، مقدار price_toman برابر 0 است! همچنین اگر کاربر در خرید اول از کد تخفیف استفاده کرده باشد، در تمام تمدیدهای بعدی هم با همان قیمت تخفیف‌خورده تمدید می‌شود. علاوه بر این، برای سرویس حجمی بدون محدودیت زمانی (days=0)، در خط ۷۰۱ شرط if days <= 0 مانع تمدید حجم می‌شود.",
    impact:
      "هیچ‌یک از خریداران با Stars یا برندگان گردونه یا خریداران سرویس حجمی بدون محدودیت زمانی نمی‌توانند سرویس خود را تمدید عادی یا خودکار کنند.",
    brokenCode: `# handlers/renew.py (خطوط ۶۵۳ تا ۶۶۰)
price = _safe_int(sub.get("price_toman"))
if price <= 0:
    await callback.answer(
        "قیمت تمدید نامعتبر است.",
        show_alert=True,
    )
    return`,
    fixedCode: `# handlers/renew.py (محاسبه قیمت روز تمدید در صورت صفر بودن price_toman)
from pricing import order_price_toman
price = _safe_int(sub.get("price_toman"))
if price <= 0:
    kind_key = "unl_inf" if sub.get("kind") == "unl" and float(sub.get("volume_gb") or 0) <= 0 else (
        "unl150" if sub.get("kind") == "unl" else "vol"
    )
    price = order_price_toman(
        kind_key,
        float(sub.get("volume_gb") or 0),
        _safe_int(sub.get("days"), 30),
        _safe_int(sub.get("devices"), 2),
    )`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-08",
    title: "منفی شدن درآمد روزانه در گزارش شبانه ادمین (send_daily_report)",
    severity: "high",
    category: "database",
    filePath: "services/scheduler.py",
    relatedFiles: "database.py",
    lineRange: "خطوط ۴۲۷ تا ۴۵۰",
    description:
      "در services/scheduler.py خط ۴۲۸ برای محاسبه درآمد امروز در گزارش روزانه، کوئری SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE type = 'purchase' اجرا می‌شود. اما در database.py خط ۶۳۲ هنگام کسر موجودی (deduct_balance)، مقدار amount با علامت منفی (-amount) ذخیره می‌شود! در نتیجه جمع تراکنش‌ها همیشه یک عدد منفی (مثلاً -350,000 تومان) می‌شود و همچنین درآمد حاصل از تمدیدها (renewal و auto_renew) و ذخیره در جدول daily_reports (تابع save_daily_report) فراموش شده است.",
    impact:
      "هر شب ساعت ۲۳:۵۵ در گزارش ادمین، درآمد روزانه منفی نمایش داده می‌شود و جدول daily_reports همیشه خالی می‌ماند.",
    brokenCode: `# services/scheduler.py (خطوط ۴۲۷ تا ۴۳۰)
revenue = conn.execute(
    "SELECT COALESCE(SUM(amount), 0) AS s FROM wallet_transactions WHERE type = 'purchase' AND created_at >= ?",
    (today_start,)
).fetchone()["s"]`,
    fixedCode: `# services/scheduler.py (استفاده از قدرمطلق تراکنش‌های خرید و تمدید + ذخیره در daily_reports)
from database import save_daily_report
revenue = conn.execute(
    "SELECT COALESCE(SUM(ABS(amount)), 0) AS s FROM wallet_transactions "
    "WHERE type IN ('purchase', 'renewal', 'auto_renew') AND amount < 0 AND created_at >= ?",
    (today_start,)
).fetchone()["s"]
save_daily_report(today_str, int(sold), int(revenue))`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-09",
    title: "ترتیب معکوس در hourly_loop و عدم ثبت پورسانت رفرال در تمدید خودکار",
    severity: "high",
    category: "integration",
    filePath: "services/scheduler.py",
    relatedFiles: "services/lifecycle.py, handlers/renew.py",
    lineRange: "خطوط ۲۱۳ تا ۲۳۴ و ۴۸۲ تا ۴۹۶",
    description:
      "در services/scheduler.py خط ۴۸۲ (hourly_loop)، ابتدا auto_renew_volume اجرا می‌شود و بعد از آن run_lifecycle_cycle اجرا می‌گردد. این یعنی تمدید خودکار حجمی همیشه روی میزان مصرف (used_bytes) قدیمی دیتابیس تصمیم می‌گیرد نه مصرف واقعی که از پنل PAXIVA سینک می‌شود! همچنین در تابع _process_auto_renew (خط ۲۱۳)، برخلاف تمدید عادی در renew.py، توابع increment_total_purchases و add_referral_commission صدا زده نمی‌شوند.",
    impact:
      "تمدید خودکار با ۱ ساعت تأخیر نسبت به مصرف واقعی عمل می‌کند و در تمدیدهای خودکار هیچ پورسانتی به معرف داده نمی‌شود و سطح وفاداری کاربر ارتقا نمی‌یابد.",
    brokenCode: `# services/scheduler.py (خطوط ۴۸۲ تا ۴۹۵)
async def hourly_loop(paxiva: PaxivaClient, bot=None) -> None:
    while True:
        try:
            volume_stats = await auto_renew_volume(paxiva, bot)
            logger.info("Hourly volume stats: %s", volume_stats)
        except Exception:
            logger.exception("Hourly auto-renew volume error.")
        try:
            lifecycle_stats = await run_lifecycle_cycle(paxiva)
            logger.info("Hourly lifecycle stats: %s", lifecycle_stats)`,
    fixedCode: `# services/scheduler.py (ابتدا همگام‌سازی مصرف از پنل، سپس بررسی تمدید خودکار)
async def hourly_loop(paxiva: PaxivaClient, bot=None) -> None:
    while True:
        try:
            lifecycle_stats = await run_lifecycle_cycle(paxiva)
            logger.info("Hourly lifecycle stats: %s", lifecycle_stats)
        except Exception:
            logger.exception("Hourly lifecycle error.")
        try:
            volume_stats = await auto_renew_volume(paxiva, bot)
            logger.info("Hourly volume stats: %s", volume_stats)
        except Exception:
            logger.exception("Hourly auto-renew volume error.")`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-10",
    title: "نمایش «بدون انقضا» برای سرویس‌های زمان‌دار تازه‌خریداری‌شده به دلیل NULL ماندن expires_at",
    severity: "high",
    category: "database",
    filePath: "database.py",
    relatedFiles: "handlers/account.py, handlers/buy.py, handlers/test.py",
    lineRange: "خطوط ۷۸۰ تا ۸۰۲ در database.py و خطوط ۵۱۱ تا ۵۲۵ در account.py",
    description:
      "در تابع create_subscription در database.py (خط ۷۸۰)، هنگام ثبت اشتراک جدید، ستون expires_at در دستور INSERT قرار ندارد و NULL ذخیره می‌شود. در نتیجه تا قبل از اولین سینک ساعتی با پنل، وقتی کاربر وارد «حساب کاربری ← جزئیات سرویس» (account.py خط ۵۲۵) یا ادمین وارد پروفایل کاربر (users.py خط ۶۴) می‌شود، چون expires_at مقدار None دارد، برای سرویس ۳۰ روزه یا اکانت تست ۷ روزه عبارت «بدون انقضا» نمایش داده می‌شود!",
    impact:
      "کاربر بلافاصله پس از خرید سرویس ۱ ماهه یا تست ۷ روزه در جزئیات سرویس عبارت «زمان باقی‌مانده: بدون انقضا» می‌بیند و همچنین تمدید عادی سرویس نامحدود در renew.py خط ۲۸۳ خطای «تاریخ انقضای سرویس مشخص نیست» می‌دهد.",
    brokenCode: `# database.py (خطوط ۷۸۹ تا ۷۹۶)
cursor = conn.execute(
    """INSERT INTO subscriptions 
    (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
     price_toman, price_stars, is_trial, paxiva_client_id, link, state, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
    (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
     price_toman, price_stars, is_trial, paxiva_client_id, link, now_ts(), now_ts())
)`,
    fixedCode: `# database.py (محاسبه اولیه expires_at بر اساس days در زمان ساخت اشتراک)
now = now_ts()
expires_at = (now + int(days) * 86400) if int(days) > 0 else None
cursor = conn.execute(
    """INSERT INTO subscriptions 
    (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
     price_toman, price_stars, is_trial, paxiva_client_id, link, state, expires_at, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
    (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
     price_toman, price_stars, is_trial, paxiva_client_id, link, expires_at, now, now)
)`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-11",
    title: "خطای تبدیل تاریخ انقضای ISO از پنل PAXIVA در renew.py و handlers/admin/services.py",
    severity: "high",
    category: "integration",
    filePath: "handlers/admin/services.py",
    relatedFiles: "handlers/renew.py, services/lifecycle.py",
    lineRange: "خطوط ۱۴۸ تا ۱۷۱ در services.py و خطوط ۸۰۴ تا ۸۱۰ در renew.py",
    description:
      "در services/lifecycle.py تابع _to_ts نوشته شده تا اگر پنل PAXIVA فیلد expires_at را به‌صورت رشته ISO 8601 (مانند '2026-05-01T12:00:00Z') برگرداند، آن را به timestamp عددی تبدیل کند. اما در handlers/admin/services.py خط ۱۵۳ و ۱۷۱ مقدار خام src.get('expires_at') مستقیماً به update_subscription_state و format_days_remaining داده می‌شود که در صورت رشته‌ای بودن با TypeError کرش می‌کند. همچنین در handlers/renew.py خط ۸۰۹، تابع _safe_int روی رشته ISO مقدار 0 برمی‌گرداند و expires_at را در دیتابیس 0 (منقضی‌شده در سال ۱۹۷۰) ثبت می‌کند!",
    impact:
      "کرش صفحه «مدیریت سرویس‌ها» در پنل ادمین و صفر شدن تاریخ انقضا پس از تمدید عادی در صورتی که پنل تاریخ ISO برگرداند.",
    brokenCode: `# handlers/renew.py (خطوط ۸۰۴ تا ۸۱۰)
remote_expiry = panel_data.get("expires_at")
...
if remote_expiry is not None:
    remote_expiry = _safe_int(remote_expiry)  # اگر ISO string باشد 0 برمی‌گرداند!`,
    fixedCode: `# استفاده از مبدل امن _to_ts از services/lifecycle.py در renew.py و services.py
from services.lifecycle import _to_ts
remote_expiry = _to_ts(panel_data.get("expires_at"))
if remote_expiry is None and int(days) > 0:
    old_expiry = _safe_int(sub.get("expires_at"))
    remote_expiry = max(int(time.time()), old_expiry) + days * 86400`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-12",
    title: "نبود گارد ادمین (protect) و کرش HTML روی نام کاربران در services.py و users_list.py",
    severity: "high",
    category: "security",
    filePath: "handlers/admin/users_list.py",
    relatedFiles: "handlers/admin/services.py, handlers/admin/admin_ui.py",
    lineRange: "خطوط ۱۵، ۸۶-۸۷ و ۱۰۷ در users_list.py و خطوط ۲۲ و ۱۶۹ در services.py",
    description:
      "۱) در دو فایل handlers/admin/users_list.py و handlers/admin/services.py برخلاف سایر ماژول‌های ادمین، تابع protect(router) صدا زده نشده و داخل هندلرها نیز شرط user.id == ADMIN_ID وجود ندارد. ۲) در users_list.py خط ۸۷ (name = u.get('first_name')) و در services.py خط ۱۶۹ (owner_name)، نام کاربر بدون html.escape داخل پیام HTML قرار گرفته است؛ اگر نام کاربری در تلگرام دارای کاراکترهای < یا > یا & باشد، تلگرام خطای Bad Request: can't parse entities می‌دهد.",
    impact:
      "کرش صفحه «لیست کاربران» و «مدیریت سرویس‌ها» به محض وجود کاراکترهای < > & در نام یکی از کاربران تلگرام + نبود فیلتر دسترسی مستقیم روی روتر.",
    brokenCode: `# handlers/admin/users_list.py (خطوط ۸۶ تا ۸۸)
username = f"@{u['username']}" if u.get("username") else "بدون یوزرنیم"
name = u.get("first_name") or "—"
# بدون html.escape در پیام با parse_mode="HTML"`,
    fixedCode: `# handlers/admin/users_list.py (افزودن protect و html.escape)
import html
from .admin_ui import protect
protect(router)
...
username = f"@{html.escape(str(u['username']))}" if u.get("username") else "بدون یوزرنیم"
name = html.escape(str(u.get("first_name") or "—"))`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-13",
    title: "باقی ماندن وضعیت FSM پس از کلیک روی «بازگشت به منوی اصلی» در start.py، links.py و broadcast.py",
    severity: "medium",
    category: "fsm_router",
    filePath: "handlers/start.py",
    relatedFiles: "handlers/admin/links.py, handlers/admin/broadcast.py",
    lineRange: "خط ۲۶۵ در start.py، خط ۹۲ در links.py، خط ۵۷ و ۱۸۹ در broadcast.py",
    description:
      "در handlers/start.py خط ۲۶۵ (هندلر menu:main)، پارامتر state: FSMContext دریافت نمی‌شود و await state.clear() صدا زده نمی‌شود. همچنین در handlers/admin/links.py خط ۹۲ (admin:links:menu) و handlers/admin/broadcast.py خطوط ۵۷ و ۱۸۹ نیز state پاک نمی‌شود. اگر کاربر یا ادمین در میانه یک مرحله متنی (مثل انتظار برای مبلغ، رسید، لینک پشتیبانی یا متن همگانی) دکمه بازگشت را بزند، ربات پیام بعدی او را همچنان به‌عنوان ورودی آن مرحله تفسیر می‌کند.",
    impact:
      "گیر کردن کاربر یا ادمین در وضعیت FSM قبلی حتی پس از بازگشت به منوی اصلی.",
    brokenCode: `# handlers/start.py (خطوط ۲۶۴ تا ۲۶۶)
@router.callback_query(F.data == "menu:main")
async def callback_main_menu(callback: CallbackQuery):`,
    fixedCode: `# handlers/start.py (پاک‌سازی FSM هنگام بازگشت به منوی اصلی)
from aiogram.fsm.context import FSMContext
@router.callback_query(F.data == "menu:main")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-14",
    title: "نبود گزینه ویرایش قیمت پایه سرویس‌های نامحدود در پنل ادمین (prices.py) و عدم تطابق کلیدهای database.py",
    severity: "medium",
    category: "integration",
    filePath: "handlers/admin/prices.py",
    relatedFiles: "pricing.py, database.py",
    lineRange: "خطوط ۴۵ تا ۱۰۵ در prices.py و خطوط ۱۱۵۶ تا ۱۲۱۵ در database.py",
    description:
      "در pricing.py خطوط ۳۹ تا ۴۲ کلیدهای unlimited_150_price / unlimited_150_stars و unlimited_inf_price / unlimited_inf_stars برای قیمت پایه سرویس‌های نامحدود خوانده می‌شوند. اما در handlers/admin/prices.py در دیکشنری PRICE_ITEMS فقط ۶ آیتم حجمی/ماهیانه/دستگاه تعریف شده و قیمت پایه نامحدود ۱۵۰ گیگ و نامحدود بی‌نهایت در منوی «تغییر قیمت‌ها» وجود ندارند! همچنین توابع set_price و get_price_override در database.py از فرمت کلید price_{item}_{currency} استفاده می‌کنند که با هیچ‌کدام از کلیدهای pricing.py و prices.py یکی نیست.",
    impact:
      "ادمین از بخش «تغییر قیمت‌ها» نمی‌تواند قیمت پایه سرویس‌های نامحدود را تغییر دهد و جدول price_history همیشه خالی می‌ماند.",
    brokenCode: `# handlers/admin/prices.py (خطوط ۹۶ تا ۱۰۵)
    "no_expiry_gb": {
        "title": "هر گیگ بدون محدودیت زمانی",
        ...
    },
}  # <-- کلیدهای unlimited_150 و unlimited_inf جا افتاده‌اند`,
    fixedCode: `# افزودن به PRICE_ITEMS در handlers/admin/prices.py:
    "unl150": {
        "title": "پایه نامحدود ۱۵۰ گیگ",
        "description": "قیمت پایه سرویس نامحدود ۱۵۰ گیگ",
        "toman_key": "unlimited_150_price",
        "stars_key": "unlimited_150_stars",
        "default_toman": 120000,
        "default_stars": 80,
        "emoji": "blue_fire",
    },
    "unl_inf": {
        "title": "پایه نامحدود بی‌نهایت",
        "description": "قیمت پایه سرویس نامحدود بی‌نهایت",
        "toman_key": "unlimited_inf_price",
        "stars_key": "unlimited_inf_stars",
        "default_toman": 350000,
        "default_stars": 110,
        "emoji": "diamond",
    },`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-15",
    title: "نبود بررسی مالکیت در buy:configs:{client_id} و نبود هندلر برای دکمه buy:noop در buy.py",
    severity: "medium",
    category: "security",
    filePath: "handlers/buy.py",
    relatedFiles: "database.py",
    lineRange: "خط ۲۴۳ و خطوط ۱۰۵۰ تا ۱۰۶۵",
    description:
      "۱) در handlers/buy.py خط ۱۰۵۰ (هندلر buy:configs:{client_id})، بدون بررسی اینکه آیا client_id متعلق به callback.from_user.id در جدول subscriptions هست یا خیر، کانفیگ‌های آن client_id از پنل گرفته و ارسال می‌شوند. ۲) در خط ۲۴۳ وقتی محصولی در دسته وجود ندارد، دکمه‌ای با callback_data='buy:noop' ساخته می‌شود که هیچ هندلری برایش ثبت نشده و کلیک روی آن باعث چرخش بی‌پایان لودینگ تلگرام می‌شود.",
    impact:
      "نشت احتمالی کانفیگ در صورت دستکاری callback_data و بی‌پاسخ ماندن دکمه «محصولی موجود نیست».",
    brokenCode: `# handlers/buy.py (خط ۱۰۵۰)
@router.callback_query(F.data.startswith("buy:configs:"))
async def send_all_subscription_configs(callback: CallbackQuery, paxiva: PaxivaClient):
    await callback.answer()
    try:
        client_id = int(callback.data.rsplit(":", 1)[-1])`,
    fixedCode: `# handlers/buy.py (بررسی مالکیت اشتراک + هندلر buy:noop)
@router.callback_query(F.data == "buy:noop")
async def buy_noop_handler(callback: CallbackQuery):
    await callback.answer("در حال حاضر محصولی در این دسته ثبت نشده است.", show_alert=True)
@router.callback_query(F.data.startswith("buy:configs:"))
async def send_all_subscription_configs(callback: CallbackQuery, paxiva: PaxivaClient):
    user = callback.from_user
    if not user:
        await callback.answer()
        return
    # بررسی مالکیت client_id در جدول subscriptions کاربر
    user_subs = get_user_subscriptions(user.id, only_visible=False)
    if not any(int(s.get("paxiva_client_id") or 0) == client_id for s in user_subs):
        await callback.answer("دسترسی غیرمجاز به این اشتراک.", show_alert=True)
        return`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-16",
    title: "نبود قفل هم‌زمانی (Race Condition)، عدم ثبت config_name و عدم پاک‌سازی کلاینت یتیم در wheel.py",
    severity: "medium",
    category: "integration",
    filePath: "handlers/wheel.py",
    relatedFiles: "database.py, paxiva.py",
    lineRange: "خطوط ۴۴۵ تا ۴۹۶ و ۶۷۲ تا ۶۹۲",
    description:
      "در handlers/wheel.py: ۱) بین بررسی can_spin_wheel(user.id) در خط ۴۴۶ و ثبت record_wheel_spin در خط ۴۹۲ هیچ قفل هم‌زمانی (asyncio.Lock) وجود ندارد و اگر کاربر سریع دوبار روی دکمه «چرخاندن گردونه» بزند، می‌تواند دو بار هم‌زمان گردونه را بچرخاند. ۲) چون record_wheel_spin در خط ۴۹۲ قبل از ساخت کانفیگ صدا زده می‌شود، ستون config_name در جدول lucky_wheel_spins همیشه NULL می‌ماند. ۳) اگر ساخت کلاینت در پنل PAXIVA موفق شود (خط ۶۷۲) اما create_subscription در دیتابیس خطا بدهد، کلاینت ساخته‌شده در پنل حذف نمی‌شود.",
    impact:
      "امکان دبل‌کلیک در گردونه شانس، خالی ماندن نام کانفیگ جایزه در دیتابیس و باقی ماندن اکانت یتیم در پنل در صورت خطای دیتابیس.",
    brokenCode: `# handlers/wheel.py (خطوط ۴۴۶ تا ۴۹۶)
if not can_spin_wheel(user.id, cooldown_hours=COOLDOWN_HOURS):
    ...
spin_id = record_wheel_spin(user.id, prize_text, prize_type)  # بدون Lock و بدون config_name`,
    fixedCode: `# handlers/wheel.py (استفاده از قفل به ازای هر کاربر + حذف کلاینت پنل در صورت شکست دیتابیس)
_wheel_locks: dict[int, asyncio.Lock] = {}
def _get_wheel_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _wheel_locks:
        _wheel_locks[user_id] = asyncio.Lock()
    return _wheel_locks[user_id]
# و در صورت خطا پس از client_created == True:
# await paxiva.delete_client(paxiva_id, purge=True)`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-17",
    title: "نمایش تگ‌های خام HTML در پیام‌های هشدار امنیتی دولوپر (behavior_guard.py و alerts.py)",
    severity: "low",
    category: "integration",
    filePath: "middlewares/behavior_guard.py",
    relatedFiles: "dev/alerts.py, bot.py",
    lineRange: "خطوط ۹۸ تا ۱۰۶ در behavior_guard.py و خط ۴۹ در alerts.py",
    description:
      "در dev/alerts.py خط ۴۹، تابع alert_worker روی متن هشدار html.escape(str(text)) می‌زند و آن را داخل تگ <pre>{safe_text}</pre> قرار می‌دهد. اما در middlewares/behavior_guard.py خطوط ۹۸ تا ۱۰۴، رشته alert_text با تگ‌های HTML مثل <b> و <code> ساخته شده است! در نتیجه در پیام ارسالی به دولوپر، تگ‌های <b> و <code> به‌صورت متنی خام نمایش داده می‌شوند. همچنین در bot.py خط ۴۲۶ پیام تست «تست هشدار...» در هر بار ری‌استارت ربات برای دولوپر ارسال می‌شود.",
    impact:
      "ناخوانا شدن پیام‌های هشدار امنیتی در پی‌وی دولوپر و ارسال پیام تست در هر بار اجرای ربات.",
    brokenCode: `# middlewares/behavior_guard.py (خطوط ۹۸ تا ۱۰۶)
alert_text = (
    f"🚨 <b>هشدار امنیتی</b>\\n\\n"
    f"👤 <b>آیدی کاربر:</b> <code>{user_id}</code>\\n"
...
)
send_alert("SECURITY", alert_text)`,
    fixedCode: `# middlewares/behavior_guard.py (ارسال متن ساده چون alerts.py خودش آن را در <pre> قرار می‌دهد)
alert_text = (
    f"هشدار امنیتی\\n"
    f"آیدی کاربر: {user_id}\\n"
    f"نوع رفتار: {reason}\\n"
    f"توضیحات: {detail}\\n"
    f"زمان: {time.strftime('%Y-%m-%d %H:%M:%S')}"
)
send_alert("SECURITY", alert_text)`,
    status: "open",
    developerNote: "",
  },
  {
    code: "BUG-18",
    title: "توابع کیبورد بلااستفاده در keyboards.py، فایل اضافی middlewares/security.py و ایموجی‌های هاردکدشده",
    severity: "low",
    category: "cleanup",
    filePath: "keyboards.py",
    relatedFiles: "middlewares/security.py, handlers/account.py, handlers/wheel.py",
    lineRange: "خطوط ۲۴۷ تا ۶۵۱ در keyboards.py",
    description:
      "۱) در فایل keyboards.py، به‌جز main_menu_rich_message و test_account_keyboard، تمام توابع دیگر (مثل buy_volume_selection_keyboard، renew_confirm_keyboard، wallet_charge_keyboard و...) مربوط به نسخه قدیمی هستند و کال‌بک‌هایی دارند (مثل buy:vol:10، renew:do:، wallet:paid) که دیگر در هیچ هندلری وجود ندارند، چون هر هندلر کیبورد خودش را داخل فایل خودش تعریف کرده است. ۲) فایل middlewares/security.py اصلاً در bot.py ایمپورت نشده چون منطق آن در behavior_guard.py ادغام شده است. ۳) در account.py خط ۵۶ و wheel.py خط ۶۶ آیدی ایموجی دکمه back به‌صورت متفاوت از config.py هاردکد شده است.",
    impact:
      "سردرگمی هنگام توسعه بعدی در صورت ایمپورت اشتباهی توابع قدیمی از keyboards.py و عدم اعتبارسنجی ایموجی‌های هاردکدشده در زمان استارت.",
    brokenCode: `# handlers/wheel.py (خط ۶۵) و handlers/account.py (خط ۵۶)
if key == "back":
    return "5325598171018051937"  # متفاوت از config.py ("5314444424418192113")`,
    fixedCode: `# خواندن یکپارچه شناسه ایموجی بازگشت از PREMIUM_EMOJIS در config.py:
emoji_id = PREMIUM_EMOJIS.get(key)`,
    status: "open",
    developerNote: "",
  },
];
export const INITIAL_PROJECT_FILES: InitialProjectFile[] = [
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\bot.py",
    relPath: "bot.py",
    folder: "root",
    linesCount: 562,
    healthStatus: "critical",
    roleSummary: "نقطه ورود ربات، مقداردهی اولیه دیتابیس و ایموجی‌ها، ثبت میدلورها، روترها و زمان‌بند",
    importsList: "config, database, paxiva, services.scheduler, services.counter, logging_setup, rich, handlers.*, handlers.payments.*, handlers.admin.*, dev.panel, middlewares.behavior_guard, dev.alerts",
    callbacksList: "Middleware: CallbackDebugMiddleware",
    auditSummary: "دارای وابستگی به dev/panel.py (خط ۵۳ و ۳۰۳)، ثبت هم‌زمان card.router و wallet.router (خط ۲۷۷) و ارسال پیام تست هشدار در هر استارت (خط ۴۲۶).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\config.py",
    relPath: "config.py",
    folder: "root",
    linesCount: 158,
    healthStatus: "healthy",
    roleSummary: "تنظیمات پایه، مسیرهای دیتابیس و بک‌آپ، آیدی ادمین‌ها، قیمت‌های پیش‌فرض و جدول ایموجی‌های پریمیوم",
    importsList: "os, pathlib.Path",
    callbacksList: "—",
    auditSummary: "ساختار تمیز و بدون خطا. فقط فراموش نکنید BOT_TOKEN و PAXIVA_API_KEY را از .env یا مقادیر واقعی پر کنید.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\database.py",
    relPath: "database.py",
    folder: "root",
    linesCount: 1565,
    healthStatus: "warning",
    roleSummary: "لایه دیتابیس SQLite با حالت WAL، تراکنش‌های اتمیک BEGIN IMMEDIATE و توابع CRUD کل سیستم",
    importsList: "os, re, time, sqlite3, logging, config.DB_PATH",
    callbacksList: "—",
    auditSummary: "در create_subscription (خط ۷۸۰) ستون expires_at مقداردهی نمی‌شود؛ همچنین کلیدهای set_price/get_price_override (خط ۱۱۶۶) با pricing.py متفاوت است.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\paxiva.py",
    relPath: "paxiva.py",
    folder: "root",
    linesCount: 173,
    healthStatus: "healthy",
    roleSummary: "کلاینت غیرهمزمان httpx برای ارتباط با API پنل PAXIVA همراه با Retry و Idempotency-Key",
    importsList: "httpx, logging, asyncio, config",
    callbacksList: "—",
    auditSummary: "سالم و استاندارد. پیشنهاد می‌شود متد get_client_configs(client_id) که در buy.py خط ۱۰۶۱ چک می‌شود به کلاس اضافه گردد.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\pricing.py",
    relPath: "pricing.py",
    folder: "root",
    linesCount: 98,
    healthStatus: "healthy",
    roleSummary: "محاسبه فرمولی قیمت سفارش‌های حجمی و نامحدود بر اساس تومان و استارز",
    importsList: "config, database.get_setting",
    callbacksList: "—",
    auditSummary: "منطق فرمول‌ها صحیح است، اما کلیدهای unlimited_150_price و unlimited_inf_price در پنل ادمین (prices.py) قابل ویرایش نیستند.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\rich.py",
    relPath: "rich.py",
    folder: "root",
    linesCount: 392,
    healthStatus: "healthy",
    roleSummary: "مدیریت و کش کردن ایموجی‌های پریمیوم تلگرام و ساخت پیام‌های Rich Message",
    importsList: "aiogram.types.*, config",
    callbacksList: "—",
    auditSummary: "تابع build_rich_message لیست پاراگراف‌ها (List) می‌گیرد، اما در backup.py و card.py به اشتباه با رشته (str) صدا زده شده است.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\ui.py",
    relPath: "ui.py",
    folder: "root",
    linesCount: 132,
    healthStatus: "healthy",
    roleSummary: "ابزار مشترک ویرایش درجا (Edit-in-place) پیام‌ها و بازگشت به منوی اصلی Rich",
    importsList: "aiogram.types, aiogram.exceptions",
    callbacksList: "—",
    auditSummary: "سالم و دارای Fallback ارسال پیام جدید در صورت خطای ویرایش تلگرام.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\utils.py",
    relPath: "utils.py",
    folder: "root",
    linesCount: 165,
    healthStatus: "healthy",
    roleSummary: "اعتبارسنجی ورودی‌ها، RateLimiter در حافظه، سطح‌بندی کاربران و فرمت بایت/زمان",
    importsList: "re, time, config, pricing",
    callbacksList: "—",
    auditSummary: "سالم. فقط تابع format_days_remaining فرض می‌کند ورودی همیشه عدد صحیح (Unix timestamp) است.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\keyboards.py",
    relPath: "keyboards.py",
    folder: "root",
    linesCount: 651,
    healthStatus: "warning",
    roleSummary: "ساخت دکمه‌های منوی اصلی Rich و کیبوردهای اکانت تست (سایر توابع داخل آن قدیمی و بلااستفاده هستند)",
    importsList: "config, rich",
    callbacksList: "menu:buy, menu:renew, menu:account, menu:wallet, menu:wheel, menu:guide, menu:test, menu:referral, menu:support, test:get",
    auditSummary: "فقط main_menu_rich_message و test_account_keyboard استفاده می‌شوند؛ بقیه توابع کیبورد در این فایل قدیمی‌اند و کال‌بک‌های منسوخ دارند.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\logging_setup.py",
    relPath: "logging_setup.py",
    folder: "root",
    linesCount: 190,
    healthStatus: "healthy",
    roleSummary: "پیکربندی لاگ غیرمسدودکننده با QueueHandler/QueueListener و فیلتر سانسور اطلاعات حساس",
    importsList: "logging, queue, re, sys, config",
    callbacksList: "—",
    auditSummary: "سالم و اصولی.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\dev\\alerts.py",
    relPath: "dev/alerts.py",
    folder: "dev",
    linesCount: 62,
    healthStatus: "healthy",
    roleSummary: "صف غیرهمزمان ارسال هشدارهای خطا و امنیتی به پی‌وی دولوپر (DEV_ID)",
    importsList: "asyncio, html, config.DEV_ID",
    callbacksList: "—",
    auditSummary: "سالم. روی متن ورودی html.escape می‌زند، بنابراین فرستنده نباید تگ HTML خام بفرستد.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\dev\\panel.py",
    relPath: "dev/panel.py",
    folder: "dev",
    linesCount: 0,
    healthStatus: "deprecated",
    roleSummary: "پنل دولوپر (طبق اعلام شما دیگر نیازی به آن نیست و در ریپازیتوری هم وجود ندارد)",
    importsList: "—",
    callbacksList: "—",
    auditSummary: "برای حذف کامل، ایمپورت آن در خط ۵۳ و ثبت روتر آن در خط ۳۰۳ فایل bot.py را پاک کنید.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\middlewares\\behavior_guard.py",
    relPath: "middlewares/behavior_guard.py",
    folder: "middlewares",
    linesCount: 107,
    healthStatus: "critical",
    roleSummary: "میدلور امنیتی بررسی مسدودیت، جلوگیری از نفوذ به ادمین، ضد اسپم و تشخیص الگوهای تزریق",
    importsList: "config, dev.alerts, database",
    callbacksList: "رهگیری تمام پیام‌ها و کال‌بک‌ها",
    auditSummary: "باگ بحرانی در خط ۵۱: ادمین رسید (receipt_admin_id) را هنگام کلیک روی admin:pay:approve/reject مسدود می‌کند! همچنین در خط ۹۸ تگ HTML خام به send_alert می‌فرستد.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\middlewares\\security.py",
    relPath: "middlewares/security.py",
    folder: "middlewares",
    linesCount: 67,
    healthStatus: "deprecated",
    roleSummary: "نسخه قدیمی‌تر میدلور امنیتی که در bot.py ثبت نشده و منطقش در behavior_guard.py ادغام شده است",
    importsList: "database, dev.alerts",
    callbacksList: "—",
    auditSummary: "بلااستفاده (در هیچ فایلی ایمپورت نشده است).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\services\\counter.py",
    relPath: "services/counter.py",
    folder: "services",
    linesCount: 138,
    healthStatus: "healthy",
    roleSummary: "مدیریت و همگام‌سازی شمارنده‌های اتمیک کانفیگ‌های عادی (iMan-N) و تست (iManTest-N)",
    importsList: "database",
    callbacksList: "—",
    auditSummary: "سالم و بدون مشکل.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\services\\lifecycle.py",
    relPath: "services/lifecycle.py",
    folder: "services",
    linesCount: 372,
    healthStatus: "healthy",
    roleSummary: "همگام‌سازی وضعیت و مصرف اشتراک‌ها با پنل PAXIVA و پاک‌سازی سرویس‌های منقضی پس از ۷ و ۳۰ روز",
    importsList: "database, paxiva",
    callbacksList: "—",
    auditSummary: "سالم. تابع _to_ts این فایل برای تبدیل تاریخ‌های ISO پنل در renew.py و services.py نیز کاربرد دارد.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\services\\scheduler.py",
    relPath: "services/scheduler.py",
    folder: "services",
    linesCount: 525,
    healthStatus: "critical",
    roleSummary: "زمان‌بند پس‌زمینه: تمدید خودکار حجمی و نامحدود، چرخه حیات و گزارش فروش شبانه",
    importsList: "dev.alerts, database, paxiva, utils, services.lifecycle, rich",
    callbacksList: "—",
    auditSummary: "۳ مشکل مهم: ۱) منفی شدن درآمد در گزارش شبانه (خط ۴۲۸) ۲) اجرای auto_renew_volume قبل از سینک با پنل در hourly_loop (خط ۴۸۵) ۳) عدم ثبت پورسانت رفرال و عدم امکان تمدید سرویس‌های Stars در تمدید خودکار.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\start.py",
    relPath: "handlers/start.py",
    folder: "handlers",
    linesCount: 328,
    healthStatus: "warning",
    roleSummary: "هندلر دستور /start، ثبت لینک دعوت رفرال و بازگشت به منوی اصلی Rich",
    importsList: "database, rich, keyboards, utils, ui",
    callbacksList: "menu:main",
    auditSummary: "در هندلر callback_main_menu (خط ۲۶۵) دستور await state.clear() صدا زده نمی‌شود.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\buy.py",
    relPath: "handlers/buy.py",
    folder: "handlers",
    linesCount: 1101,
    healthStatus: "critical",
    roleSummary: "فرآیند مرحله‌به‌مرحله خرید سرویس حجمی و نامحدود، کد تخفیف و پرداخت از کیف پول",
    importsList: "config, database, paxiva, services.counter, rich, ui, pricing, utils, dev.alerts",
    callbacksList: "menu:buy, buy:type:vol, buy:type:unl, buy:main, buy:prod:*, buy:vol, buy:time:*, buy:time, buy:dev:*, buy:dev, buy:discount:*, buy:discount, buy:pay:wallet, buy:configs:*",
    auditSummary: "قیمت و مدت اختصاصی محصولات در جدول products را نادیده می‌گیرد (خط ۳۷۱ و ۷۷۴)، دکمه buy:noop هندلر ندارد (خط ۲۴۳) و buy:configs:* مالکیت اشتراک را چک نمی‌کند (خط ۱۰۵۰).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\renew.py",
    relPath: "handlers/renew.py",
    folder: "handlers",
    linesCount: 1311,
    healthStatus: "warning",
    roleSummary: "مدیریت تمدید عادی و فعال/غیرفعال‌سازی تمدید خودکار اشتراک‌ها",
    importsList: "dev.alerts, config, database, paxiva, rich, ui, utils",
    callbacksList: "menu:renew, renew:select:*, renew:normal:*, renew:auto:*, renew:auto_off:*, renew:list",
    auditSummary: "سرویس‌های خریداری‌شده با Stars (که price_toman=0 دارند) و سرویس‌های حجمی بدون محدودیت زمانی (days=0) امکان تمدید ندارند (خط ۶۵۳ و ۷۰۱). همچنین _safe_int روی تاریخ ISO مقدار 0 می‌دهد (خط ۸۰۹).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\account.py",
    relPath: "handlers/account.py",
    folder: "handlers",
    linesCount: 584,
    healthStatus: "warning",
    roleSummary: "نمایش پروفایل کاربری، سطح وفاداری و جزئیات سرویس‌های خریداری‌شده",
    importsList: "config, database, ui, utils",
    callbacksList: "menu:account, account:noop, account:sub:*",
    auditSummary: "اگر expires_at در دیتابیس هنوز NULL باشد، حتی برای سرویس ۳۰ روزه عبارت «بدون انقضا» نشان می‌دهد (خط ۵۲۵). همچنین ایموجی back در خط ۵۶ هاردکد شده است.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\wallet.py",
    relPath: "handlers/wallet.py",
    folder: "handlers",
    linesCount: 819,
    healthStatus: "healthy",
    roleSummary: "مدیریت کامل کیف پول، تاریخچه تراکنش‌ها، شارژ کارت‌به‌کارت و تأیید/رد رسید توسط ادمین",
    importsList: "config, database, rich, ui, utils, dev.alerts",
    callbacksList: "menu:wallet, wallet:main, wallet:history, wallet:charge, wallet:cancel, admin:pay:approve:*, admin:pay:reject:*",
    auditSummary: "پیاده‌سازی کامل و استاندارد است؛ فقط به دلیل میدلور behavior_guard.py ادمین رسید فرعی روی کال‌بک‌های admin:pay:* مسدود می‌شود.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\referral.py",
    relPath: "handlers/referral.py",
    folder: "handlers",
    linesCount: 429,
    healthStatus: "healthy",
    roleSummary: "صفحه زیرمجموعه‌گیری، ساخت لینک اختصاصی دعوت و نمایش آمار تفصیلی پورسانت‌ها",
    importsList: "config, database, rich, utils, ui",
    callbacksList: "menu:referral, referral:stats, referral:copy",
    auditSummary: "سالم. در _format_source (خط ۱۳۵) بهتر است کلیدهای 'stars_purchase' و 'auto_renew' هم به دیکشنری ترجمه منابع پورسانت اضافه شوند.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\wheel.py",
    relPath: "handlers/wheel.py",
    folder: "handlers",
    linesCount: 843,
    healthStatus: "warning",
    roleSummary: "گردونه شانس روزانه با جوایز نقدی و کانفیگ رایگان",
    importsList: "dev.alerts, services.counter, database, paxiva, utils, config, rich, ui",
    callbacksList: "menu:wheel, wheel:noop, wheel:spin",
    auditSummary: "نبود قفل هم‌زمانی روی wheel:spin، ثبت نشدن config_name در جدول lucky_wheel_spins و عدم حذف کلاینت پنل در صورت شکست دیتابیس.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\test.py",
    relPath: "handlers/test.py",
    folder: "handlers",
    linesCount: 416,
    healthStatus: "healthy",
    roleSummary: "دریافت اکانت تست رایگان ۱ گیگ ۷ روزه با رزرو اتمیک حق تست (trial_used)",
    importsList: "dev.alerts, ui, database, paxiva, rich, keyboards, utils",
    callbacksList: "menu:test, test:get",
    auditSummary: "سالم و دارای Rollback دقیق در صورت خطای پنل یا دیتابیس.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\guide.py",
    relPath: "handlers/guide.py",
    folder: "handlers",
    linesCount: 156,
    healthStatus: "healthy",
    roleSummary: "نمایش راهنمای دریافت و نصب اپلیکیشن با لینک قابل تنظیم از پنل ادمین",
    importsList: "config, database, rich, utils, ui",
    callbacksList: "menu:guide",
    auditSummary: "سالم و بدون مشکل.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\support.py",
    relPath: "handlers/support.py",
    folder: "handlers",
    linesCount: 85,
    healthStatus: "healthy",
    roleSummary: "نمایش صفحه ارتباط با پشتیبانی با لینک مستقیم تلگرام",
    importsList: "config, database, rich, utils, ui",
    callbacksList: "menu:support",
    auditSummary: "سالم (البته اگر PREMIUM_EMOJI_ENABLED=False شود در خط ۲۴ خطای RuntimeError می‌دهد).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\payments\\card.py",
    relPath: "handlers/payments/card.py",
    folder: "handlers/payments",
    linesCount: 233,
    healthStatus: "deprecated",
    roleSummary: "نسخه قدیمی و تکراری شارژ کارت‌به‌کارت که با handlers/wallet.py تداخل دارد",
    importsList: "config, database, rich, utils",
    callbacksList: "wallet:charge, admin:pay:approve:*, admin:pay:reject:*",
    auditSummary: "کاملاً تکراری با wallet.py و دارای باگ build_rich_message(text). باید از bot.py حذف شود.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\payments\\stars.py",
    relPath: "handlers/payments/stars.py",
    folder: "handlers/payments",
    linesCount: 292,
    healthStatus: "critical",
    roleSummary: "پرداخت و صدور فاکتور با Telegram Stars (XTR) همراه با بازپرداخت خودکار در صورت خطا",
    importsList: "dev.alerts, config, database, paxiva, rich, utils",
    callbacksList: "buy:pay:stars + pre_checkout_query + F.successful_payment",
    auditSummary: "نوع سرویس همیشه kind='vol' ثبت می‌شود (خط ۱۷۲)، کد تخفیف مصرف نمی‌شود و پورسانت رفرال دوبار در 0.05 ضرب می‌شود (خط ۱۹۰).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\admin_ui.py",
    relPath: "handlers/admin/admin_ui.py",
    folder: "handlers/admin",
    linesCount: 330,
    healthStatus: "healthy",
    roleSummary: "چارچوب رابط کاربری پنل ادمین، فیلتر امنیتی AdminOnly، چیدمان دکمه‌ها و مدیریت رفرش پنل",
    importsList: "config, database, rich",
    callbacksList: "Filter: AdminOnly",
    auditSummary: "سالم و خوش‌ساخت.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\panel.py",
    relPath: "handlers/admin/panel.py",
    folder: "handlers/admin",
    linesCount: 630,
    healthStatus: "healthy",
    roleSummary: "منوی اصلی پنل مدیریت (/admin و کال‌بک admin:main)",
    importsList: "config, database, utils, rich",
    callbacksList: "/admin, admin:main",
    auditSummary: "سالم و دارای پاک‌سازی وضعیت FSM هنگام بازگشت به منوی ادمین.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\backup.py",
    relPath: "handlers/admin/backup.py",
    folder: "handlers/admin",
    linesCount: 60,
    healthStatus: "critical",
    roleSummary: "تهیه فایل پشتیبان از دیتابیس SQLite و ارسال به پی‌وی ادمین",
    importsList: "config, database, rich, utils",
    callbacksList: "admin:backup:menu",
    auditSummary: "به دلیل خطای build_rich_message(str) در خط ۴۱ همیشه کرش می‌کند و هیچ وقت فایل بک‌آپ را ارسال نمی‌کند!",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\broadcast.py",
    relPath: "handlers/admin/broadcast.py",
    folder: "handlers/admin",
    linesCount: 327,
    healthStatus: "warning",
    roleSummary: "ارسال پیام تکی و همگانی (متنی یا فوروارد از کانال) به کاربران",
    importsList: "config, database, rich",
    callbacksList: "admin:broadcast:menu, admin:broadcast:single, admin:broadcast:all, admin:broadcast:text, admin:broadcast:link",
    auditSummary: "در broadcast_menu و broadcast_all_menu هنگام بازگشت، state.clear() صدا زده نمی‌شود.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\discounts.py",
    relPath: "handlers/admin/discounts.py",
    folder: "handlers/admin",
    linesCount: 685,
    healthStatus: "healthy",
    roleSummary: "مدیریت و ساخت ۳ مرحله‌ای کدهای تخفیف درصدی با سقف تعداد استفاده",
    importsList: "config, database, rich",
    callbacksList: "admin:discounts:menu, admin:discounts:create, admin:discounts:confirm, admin:discounts:cancel",
    auditSummary: "سالم. (توابع غیرفعال‌سازی/حذف کد تخفیف در database.py وجود دارند که در صورت تمایل می‌توانید دکمه‌شان را هم به این منو اضافه کنید).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\links.py",
    relPath: "handlers/admin/links.py",
    folder: "handlers/admin",
    linesCount: 242,
    healthStatus: "warning",
    roleSummary: "تنظیم و نرمال‌سازی لینک‌های پشتیبانی و کانال آموزش",
    importsList: "config, database, rich",
    callbacksList: "admin:links:menu, admin:links:support, admin:links:guide",
    auditSummary: "در links_menu (خط ۹۲) هنگام کلیک روی دکمه «بازگشت»، وضعیت FSM پاک نمی‌شود (await state.clear() ندارد).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\prices.py",
    relPath: "handlers/admin/prices.py",
    folder: "handlers/admin",
    linesCount: 800,
    healthStatus: "warning",
    roleSummary: "مشاهده و ویرایش قیمت‌های پایه (تومان و استارز) برای مؤلفه‌های سرویس",
    importsList: "config, database, rich",
    callbacksList: "admin:prices:menu, admin:prices:edit:*, admin:prices:currency:*, admin:prices:cancel",
    auditSummary: "گزینه ویرایش قیمت پایه «نامحدود ۱۵۰ گیگ» و «نامحدود بی‌نهایت» در PRICE_ITEMS جا افتاده است.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\products.py",
    relPath: "handlers/admin/products.py",
    folder: "handlers/admin",
    linesCount: 384,
    healthStatus: "warning",
    roleSummary: "افزودن، حذف، فعال/غیرفعال‌سازی و تغییر قیمت محصولات حجمی و نامحدود",
    importsList: "config, database, pricing, .admin_ui",
    callbacksList: "admin:products:menu, admin:products:list:*, admin:products:view:*, admin:products:toggle:*, admin:products:delask:*, admin:products:delete:*, admin:products:price:*, admin:products:add, admin:products:kind:*, admin:products:change_price, admin:products:confirm",
    auditSummary: "خود فایل سالم است، اما قیمت‌ها و مدتی که اینجا ذخیره می‌کنید در handlers/buy.py نادیده گرفته می‌شوند!",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\receipts.py",
    relPath: "handlers/admin/receipts.py",
    folder: "handlers/admin",
    linesCount: 329,
    healthStatus: "healthy",
    roleSummary: "تعیین یا حذف «ادمین رسید»، مشاهده رسیدهای در انتظار و ارسال مجدد آن‌ها",
    importsList: "config, database, .admin_ui",
    callbacksList: "admin:receipt_admin:menu, admin:receipt_admin:set, admin:receipt_admin:confirm:*, admin:receipt_admin:remove, admin:receipt_admin:remove_yes, admin:receipt_admin:test, admin:receipt_admin:pending, admin:receipt_admin:resend",
    auditSummary: "سالم؛ فقط برای اینکه ادمین رسید بتواند دکمه تأیید/رد را بزند، باید باگ BUG-01 در behavior_guard.py اصلاح شود.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\sales.py",
    relPath: "handlers/admin/sales.py",
    folder: "handlers/admin",
    linesCount: 100,
    healthStatus: "healthy",
    roleSummary: "باز و بسته کردن سراسری فروش سرویس در ربات (تنظیم sales_open)",
    importsList: "config, database, rich, utils",
    callbacksList: "admin:sales:close, admin:sales:open",
    auditSummary: "سالم و بدون مشکل.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\services.py",
    relPath: "handlers/admin/services.py",
    folder: "handlers/admin",
    linesCount: 424,
    healthStatus: "critical",
    roleSummary: "جستجوی سرویس با نام کانفیگ یا لینک ساب، مشاهده آنلاین مصرف، افزودن روز، تغییر حجم، ریست مصرف و حذف",
    importsList: "config, database, paxiva, rich, utils",
    callbacksList: "admin:services:menu, admin:services:open:*, admin:services:reset:*, admin:services:days:*, admin:services:volume:*, admin:services:delask:*, admin:services:delete:*",
    auditSummary: "فاقد protect(router) برای کنترل دسترسی ادمین، عدم escape کردن نام مالک (خط ۱۶۹) و عدم تبدیل تاریخ ISO پنل به عدد قبل از format_days_remaining (خط ۱۷۱).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\stats.py",
    relPath: "handlers/admin/stats.py",
    folder: "handlers/admin",
    linesCount: 132,
    healthStatus: "healthy",
    roleSummary: "نمایش آمار کلی کاربران، فروش، درآمد تومانی و استارز، رسیدها و پرفروش‌ترین سرویس‌ها",
    importsList: "config, database, rich",
    callbacksList: "admin:stats:menu",
    auditSummary: "سالم و بدون مشکل.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\test_panel.py",
    relPath: "handlers/admin/test_panel.py",
    folder: "handlers/admin",
    linesCount: 110,
    healthStatus: "healthy",
    roleSummary: "بررسی اتصال ربات به API پنل PAXIVA (متد /me) و نمایش پینگ و وضعیت اکانت پنل",
    importsList: "config, paxiva, rich",
    callbacksList: "admin:test_panel:menu",
    auditSummary: "سالم و بدون مشکل.",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\users.py",
    relPath: "handlers/admin/users.py",
    folder: "handlers/admin",
    linesCount: 245,
    healthStatus: "healthy",
    roleSummary: "جستجوی کاربر با آیدی یا یوزرنیم، مشاهده پروفایل، افزایش/کاهش موجودی و مسدود/رفع مسدودی",
    importsList: "config, database, utils, .admin_ui",
    callbacksList: "admin:users:view:*, admin:users:menu, admin:users:ban:*, admin:users:unban:*, admin:users:bal:*",
    auditSummary: "سالم و دارای escape استاندارد و گارد protect(router).",
  },
  {
    winPath: "C:\\Users\\HAMAHANG\\Desktop\\iMan\\handlers\\admin\\users_list.py",
    relPath: "handlers/admin/users_list.py",
    folder: "handlers/admin",
    linesCount: 194,
    healthStatus: "critical",
    roleSummary: "لیست صفحه‌بندی‌شده تمام کاربران به ترتیب بیشترین خرید همراه با دکمه پروفایل و بن/آن‌بن",
    importsList: "config, database, rich",
    callbacksList: "admin:users_list:menu, admin:users_list:page:*, admin:users_list:ban:*, admin:users_list:unban:*",
    auditSummary: "فاقد protect(router) برای کنترل دسترسی ادمین و عدم escape کردن نام کاربر (first_name) در خط ۸۷ که در صورت وجود < یا > باعث کرش HTML می‌شود.",
  },
];
