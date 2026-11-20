"""
database.py
-----------
لایهٔ دیتابیس (SQLite) ربات.

اصول امنیتی و پایداری:
* همهٔ کوئری‌ها پارامتریک هستند (بدون ساخت SQL با رشته‌های ورودی کاربر).
* هر اتصال بعد از استفاده بسته می‌شود (نشت اتصال ندارد).
* تغییرات مالی (کیف پول، پرداخت، تخفیف) داخل تراکنش BEGIN IMMEDIATE انجام می‌شود.
* ورودی‌ها قبل از ذخیره اعتبارسنجی و پاک‌سازی می‌شوند؛ جستجوها LIKE-escape دارند.
* log_event هرگز استثنا نمی‌اندازد (خطای لاگ نباید جلوی کار اصلی را بگیرد).
* فایل دیتابیس با دسترسی ۶۰۰ ساخته می‌شود و بک‌آپ با API رسمی SQLite (سازگار با WAL) گرفته می‌شود.
* مهاجرت‌ها فقط افزایشی‌اند (ستون/جدول جدید)؛ هیچ دیتایی پاک یا بازنویسی نمی‌شود.

همهٔ توابع قبلی با همان نام و امضا باقی مانده‌اند؛ توابع جدید در انتهای فایل هستند.
"""

import os
import re
import time
import sqlite3
import logging
import json
from typing import Optional, Dict, List, Any, Tuple

from config import DB_PATH

logger = logging.getLogger(__name__)

# ============================================================
# اتصال امن
# ============================================================

class _SafeConnection(sqlite3.Connection):
    """
    رفتار `with get_connection() as conn` در sqlite3 فقط commit/rollback می‌کند و
    اتصال را باز می‌گذارد. این کلاس بعد از commit/rollback اتصال را می‌بندد.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0, factory=_SafeConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA secure_delete=ON;")
    conn.execute("PRAGMA trusted_schema=OFF;")
    return conn

# ============================================================
# اعتبارسنجی و Sanitize
# ============================================================

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

def sanitize_text(value, max_len: int = 500) -> Optional[str]:
    if value is None:
        return None
    text = _CONTROL_CHARS.sub("", str(value)).strip()
    if len(text) > max_len:
        text = text[:max_len]
    return text or None

def validate_telegram_id(tid) -> int:
    try:
        tid = int(tid)
    except (ValueError, TypeError):
        raise ValueError("Invalid telegram_id")
    if tid <= 0 or tid > 10_000_000_000_000:
        raise ValueError("telegram_id out of range")
    return tid

def validate_amount(amount, max_val: int = 500_000_000) -> int:
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        raise ValueError("Invalid amount")
    if amount <= 0 or amount > max_val:
        raise ValueError("Amount out of range")
    return amount

def now_ts() -> int:
    return int(time.time())

def _like_escape(text: str) -> str:
    """escape کردن % _ \\ برای LIKE ... ESCAPE '\\'."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """ستون را فقط اگر وجود ندارد اضافه می‌کند (نام‌ها ثابت و داخلی‌اند، نه ورودی کاربر)."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

# ============================================================
# ساخت جداول
# ============================================================

def init_database() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                total_purchases INTEGER NOT NULL DEFAULT 0,
                trial_used INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                ban_until INTEGER,
                joined_at INTEGER NOT NULL,
                last_seen_at INTEGER,
                CHECK (balance >= 0),
                CHECK (total_purchases >= 0)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS username_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                changed_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                config_number INTEGER NOT NULL,
                config_name TEXT NOT NULL UNIQUE,
                paxiva_client_id INTEGER,
                kind TEXT NOT NULL DEFAULT 'vol' CHECK (kind IN ('vol', 'unl')),
                volume_gb REAL NOT NULL DEFAULT 0,
                days INTEGER NOT NULL DEFAULT 30,
                devices INTEGER NOT NULL DEFAULT 2,
                price_toman INTEGER NOT NULL DEFAULT 0,
                price_stars INTEGER NOT NULL DEFAULT 0,
                is_trial INTEGER NOT NULL DEFAULT 0,
                auto_renew INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'active',
                expires_at INTEGER,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                link TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_from_list_at INTEGER,
                deleted_from_db_at INTEGER,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                balance_before INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                reference_id TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                photo_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
                reviewed_by INTEGER,
                reviewed_at INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                volume_gb REAL NOT NULL,
                days INTEGER NOT NULL,
                devices INTEGER NOT NULL,
                amount_stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'delivered', 'refunded')),
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars_charges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_id TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                amount_stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'charged' CHECK (status IN ('charged', 'delivered', 'refunded')),
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (referred_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                CHECK (referrer_id != referred_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                source TEXT,
                subscription_id INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (referred_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                percent INTEGER NOT NULL CHECK (percent > 0 AND percent <= 100),
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS discount_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                used_at INTEGER NOT NULL,
                UNIQUE (code_id, telegram_id),
                FOREIGN KEY (code_id) REFERENCES discount_codes(id) ON DELETE CASCADE,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                user_id INTEGER,
                detail TEXT,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                banned_by INTEGER NOT NULL,
                reason TEXT,
                until INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'vol' CHECK (kind IN ('vol', 'unl')),
                volume_gb REAL NOT NULL DEFAULT 0,
                days INTEGER NOT NULL DEFAULT 30,
                price_toman INTEGER NOT NULL,
                price_stars INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                configs_sold INTEGER NOT NULL DEFAULT 0,
                revenue_toman INTEGER NOT NULL DEFAULT 0,
                active_users INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                activity_date TEXT NOT NULL,
                UNIQUE (telegram_id, activity_date),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        # ثبت دائمی ارسال پیام خوش‌آمدگویی پین‌شده برای هر کاربر.
        # telegram_id کلید اصلی است؛ بنابراین برای هر کاربر فقط یک رکورد ممکن است.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_pinned_welcome (
                telegram_id INTEGER PRIMARY KEY,
                sent_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id)
                    REFERENCES users(telegram_id)
                    ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS lucky_wheel_spins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                prize_type TEXT NOT NULL DEFAULT 'nothing',
                config_name TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('toman', 'stars')),
                old_value INTEGER,
                new_value INTEGER NOT NULL,
                changed_by INTEGER,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT,
                created_at INTEGER NOT NULL
            )
        """)

        # ========================================================
        # مهاجرت‌های افزایشی (روی دیتابیس موجود امن است)
        # ========================================================

        _ensure_column(conn, "bans", "unbanned_by", "INTEGER")
        _ensure_column(conn, "bans", "unbanned_at", "INTEGER")
        _ensure_column(conn, "payment_requests", "review_note", "TEXT")

        # ========================================================
        # PasarGuard — پنل‌های پویا (بدون محدودیت تعداد پنل)
        # ========================================================

        conn.execute("""
            CREATE TABLE IF NOT EXISTS panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                group_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)

        _ensure_column(conn, "products", "panel_id", "INTEGER REFERENCES panels(id) ON DELETE SET NULL")
        # Product identity and renewal state are additive migrations.
        _ensure_column(conn, "subscriptions", "product_id", "INTEGER REFERENCES products(id) ON DELETE SET NULL")
        _ensure_column(conn, "subscriptions", "auto_renew_last_attempt_at", "INTEGER")
        _ensure_column(conn, "subscriptions", "auto_renew_last_success_at", "INTEGER")
        _ensure_column(conn, "subscriptions", "auto_renew_last_error", "TEXT")
        _ensure_column(conn, "stars_invoices", "kind", "TEXT NOT NULL DEFAULT 'vol'")
        _ensure_column(conn, "subscriptions", "panel_id", "INTEGER REFERENCES panels(id) ON DELETE SET NULL")
        # اتصال سرویس‌های قدیمی به محصول فقط وقتی انجام می‌شود که تطبیق یکتا باشد.
        # برای محصولات حجمی ستون days در نسخه‌های قدیمی ممکن است 30 باشد؛ در تطبیق نادیده گرفته می‌شود.
        conn.execute("""
            UPDATE subscriptions AS s
            SET product_id = (
                SELECT MIN(p.id)
                FROM products AS p
                WHERE p.is_active = 1
                  AND p.kind = s.kind
                  AND p.volume_gb = s.volume_gb
                  AND (s.kind = 'vol' OR p.days = s.days)
                  AND p.price_toman = s.price_toman
                  AND (s.panel_id IS NULL OR p.panel_id = s.panel_id)
            )
            WHERE s.product_id IS NULL
              AND 1 = (
                SELECT COUNT(*)
                FROM products AS p
                WHERE p.is_active = 1
                  AND p.kind = s.kind
                  AND p.volume_gb = s.volume_gb
                  AND (s.kind = 'vol' OR p.days = s.days)
                  AND p.price_toman = s.price_toman
                  AND (s.panel_id IS NULL OR p.panel_id = s.panel_id)
              )
        """)
        _ensure_column(conn, "stars_invoices", "panel_id", "INTEGER REFERENCES panels(id) ON DELETE SET NULL")
        _ensure_column(conn, "stars_charges", "panel_id", "INTEGER REFERENCES panels(id) ON DELETE SET NULL")
        _ensure_column(conn, "users", "points", "INTEGER NOT NULL DEFAULT 0")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_panels_active ON panels(is_active, id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_panels_name ON panels(name COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_panel ON products(panel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_panel ON subscriptions(panel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_invoice_panel ON stars_invoices(panel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_charge_panel ON stars_charges(panel_id)")

        # تمدیدها: ثبت سفارش، پرداخت اتمیک و جلوگیری از تمدید هم‌زمان تکراری
        conn.execute("""
            CREATE TABLE IF NOT EXISTS renewal_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT UNIQUE,
                subscription_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                panel_id INTEGER,
                source TEXT NOT NULL CHECK (source IN ('manual', 'auto')),
                trigger_reason TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('vol', 'unl')),
                volume_gb REAL NOT NULL DEFAULT 0,
                days INTEGER NOT NULL DEFAULT 0,
                price_toman INTEGER NOT NULL CHECK (price_toman >= 0),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'delivered', 'failed', 'refunded')),
                wallet_transaction_id INTEGER,
                external_reference TEXT,
                last_error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                delivered_at INTEGER,
                refunded_at INTEGER,
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE RESTRICT,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE RESTRICT,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT,
                FOREIGN KEY (panel_id) REFERENCES panels(id) ON DELETE SET NULL,
                FOREIGN KEY (wallet_transaction_id) REFERENCES wallet_transactions(id) ON DELETE SET NULL
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_renewal_active_subscription
            ON renewal_orders(subscription_id)
            WHERE status IN ('pending', 'processing')
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_renewal_status_created ON renewal_orders(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_auto_renew ON subscriptions(auto_renew, state, kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_product ON subscriptions(product_id)")

        # ========================================================
        # امتیازها، گروه‌ها، کلیدواژه‌ها و مأموریت‌های پویا
        # ========================================================

        conn.execute("""
            CREATE TABLE IF NOT EXISTS points_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL CHECK (amount != 0),
                balance_before INTEGER NOT NULL CHECK (balance_before >= 0),
                balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
                type TEXT NOT NULL,
                description TEXT,
                reference_id TEXT,
                related_user_id INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE RESTRICT,
                FOREIGN KEY (related_user_id) REFERENCES users(telegram_id) ON DELETE RESTRICT
            )
        """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_points_idempotent
            ON points_transactions(type, reference_id, telegram_id)
            WHERE reference_id IS NOT NULL
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_points_user_created ON points_transactions(telegram_id, created_at DESC)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                title TEXT,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                points_enabled INTEGER NOT NULL DEFAULT 1 CHECK (points_enabled IN (0, 1)),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_keyword_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                keyword TEXT NOT NULL COLLATE NOCASE,
                points INTEGER NOT NULL CHECK (points > 0),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                cooldown_seconds INTEGER NOT NULL DEFAULT 0 CHECK (cooldown_seconds >= 0),
                created_at INTEGER NOT NULL,
                UNIQUE (group_id, keyword),
                FOREIGN KEY (group_id) REFERENCES activity_groups(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_message_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                trigger_id INTEGER,
                points_awarded INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                UNIQUE (group_id, message_id),
                FOREIGN KEY (group_id) REFERENCES activity_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE RESTRICT,
                FOREIGN KEY (trigger_id) REFERENCES group_keyword_triggers(id) ON DELETE SET NULL
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_group_events_user_time ON group_message_events(telegram_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_group_triggers_active ON group_keyword_triggers(group_id, is_active)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                reward_points INTEGER NOT NULL CHECK (reward_points >= 0),
                duration_seconds INTEGER,
                starts_at INTEGER,
                ends_at INTEGER,
                verification_method TEXT NOT NULL DEFAULT 'manual'
                    CHECK (verification_method IN ('automatic', 'manual', 'hybrid')),
                max_completions INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_by INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                CHECK (duration_seconds IS NULL OR duration_seconds > 0),
                CHECK (max_completions IS NULL OR max_completions > 0)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'assigned'
                    CHECK (status IN ('assigned', 'submitted', 'approved', 'rejected', 'expired')),
                assigned_at INTEGER NOT NULL,
                deadline_at INTEGER,
                submitted_at INTEGER,
                reviewed_at INTEGER,
                reviewed_by INTEGER,
                review_note TEXT,
                evidence_json TEXT,
                rewarded_at INTEGER,
                reward_points INTEGER NOT NULL DEFAULT 0 CHECK (reward_points >= 0),
                UNIQUE (task_id, telegram_id),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE RESTRICT
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_active_window ON tasks(is_active, starts_at, ends_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_tasks_user_status ON user_tasks(telegram_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_tasks_task_status ON user_tasks(task_id, status)")


        # Discount codes
        _ensure_column(conn, "discount_codes", "owner_id", "INTEGER")
        _ensure_column(conn, "discount_codes", "expires_at", "INTEGER")

        # ========================================================
        # Stars invoice — اطلاعات کامل سفارش
        # ========================================================

        _ensure_column(conn, "stars_invoices", "product_id", "INTEGER")
        _ensure_column(conn, "stars_invoices", "price_toman", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "stars_invoices", "discount_id", "INTEGER")
        _ensure_column(conn, "stars_invoices", "discount_code", "TEXT")
        _ensure_column(conn, "stars_invoices", "discount_percent", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "stars_invoices", "discount_amount_toman", "INTEGER NOT NULL DEFAULT 0")

        # اطلاعات پیام فاکتور اصلی خرید در buy.py
        _ensure_column(conn, "stars_invoices", "purchase_chat_id", "INTEGER")
        _ensure_column(conn, "stars_invoices", "purchase_message_id", "INTEGER")

        # وضعیت پردازش فاکتور
        _ensure_column(conn, "stars_invoices", "workflow_status", "TEXT NOT NULL DEFAULT 'pending'")
        _ensure_column(conn, "stars_invoices", "paid_at", "INTEGER")
        _ensure_column(conn, "stars_invoices", "delivered_at", "INTEGER")
        _ensure_column(conn, "stars_invoices", "updated_at", "INTEGER")
        _ensure_column(conn, "stars_invoices", "last_error", "TEXT")
        _ensure_column(conn, "stars_invoices", "subscription_id", "INTEGER")
        _ensure_column(conn, "stars_invoices", "paxiva_client_id", "INTEGER")
        _ensure_column(conn, "stars_invoices", "config_number", "INTEGER")
        _ensure_column(conn, "stars_invoices", "config_name", "TEXT")

        # ========================================================
        # Stars charge — اطلاعات پرداخت واقعی تلگرام
        # ========================================================

        _ensure_column(conn, "stars_charges", "workflow_status", "TEXT NOT NULL DEFAULT 'charged'")
        _ensure_column(conn, "stars_charges", "updated_at", "INTEGER")
        _ensure_column(conn, "stars_charges", "delivered_at", "INTEGER")
        _ensure_column(conn, "stars_charges", "refunded_at", "INTEGER")
        _ensure_column(conn, "stars_charges", "last_error", "TEXT")
        _ensure_column(conn, "stars_charges", "subscription_id", "INTEGER")
        _ensure_column(conn, "stars_charges", "config_number", "INTEGER")
        _ensure_column(conn, "stars_charges", "paxiva_client_id", "INTEGER")
        _ensure_column(conn, "stars_charges", "attempts", "INTEGER NOT NULL DEFAULT 0")

        # انتقال وضعیت رکوردهای قدیمی
        conn.execute("""
            UPDATE stars_invoices
            SET workflow_status = status
            WHERE status IN ('delivered', 'refunded')
              AND workflow_status = 'pending'
        """)

        conn.execute("""
            UPDATE stars_charges
            SET workflow_status = status
            WHERE status IN ('delivered', 'refunded')
              AND workflow_status = 'charged'
        """)

        # ========================================================
        # صف بازپرداخت Stars
        # ========================================================

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars_refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_id TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending',
                        'processing',
                        'succeeded',
                        'failed'
                    )),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                processing_at INTEGER,
                completed_at INTEGER,
                FOREIGN KEY (telegram_id)
                    REFERENCES users(telegram_id)
                    ON DELETE RESTRICT
            )
        """)

        # برای دیتابیس‌هایی که جدول refund از قبل ساخته شده
        _ensure_column(conn, "stars_refunds", "processing_at", "INTEGER")

        # ========================================================
        # ثبت یکتای تحویل Stars
        # ========================================================

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars_fulfillments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                subscription_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id)
                    REFERENCES users(telegram_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (subscription_id)
                    REFERENCES subscriptions(id)
                    ON DELETE RESTRICT
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_price_hist ON price_history(item, currency, id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_created ON payment_requests(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_created ON subscriptions(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(telegram_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_state ON subscriptions(state);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_expires ON subscriptions(expires_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_config_name ON subscriptions(config_name COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_user ON wallet_transactions(telegram_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_status ON payment_requests(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_user ON payment_requests(telegram_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_referrer ON referrals(referrer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_referred ON referrals(referred_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_earn_referrer ON referral_earnings(referrer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_code ON discount_codes(code);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bans_user ON bans(telegram_id, is_active);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_inv_payload ON stars_invoices(payload);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_chg_charge ON stars_charges(charge_id);")

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stars_invoice_status
            ON stars_invoices(workflow_status, updated_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stars_charge_status
            ON stars_charges(workflow_status, updated_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stars_refund_retry
            ON stars_refunds(status, next_retry_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stars_fulfillment_user
            ON stars_fulfillments(telegram_id, created_at)
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_date ON user_activity(activity_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wheel_telegram ON lucky_wheel_spins(telegram_id);")
        # PAY-01: جلوگیری سطح دیتابیس از کسر/بازگشت/پورسانت تکراری برای یک سفارش
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_idempotent ON wallet_transactions(type, reference_id) "
                "WHERE reference_id IS NOT NULL AND (type IN ('purchase', 'referral_commission') OR reference_id LIKE 'refund:%')"
            )
        except sqlite3.Error:
            logger.warning("Could not create uq_wallet_idempotent (duplicate legacy rows?); "
                           "application-level idempotency check stays active", exc_info=True)

    # دسترسی فایل دیتابیس فقط برای صاحب برنامه
    try:
        os.chmod(DB_PATH, 0o600)
    except Exception:
        logger.debug("Could not chmod database file", exc_info=True)


# ============================================================
# تنظیمات
# ============================================================

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    key = str(key)
    if not key or len(key) > 200:
        raise ValueError("Invalid setting key")
    value = str(value)
    if len(value) > 20000:
        raise ValueError("Setting value too long")
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_ts())
        )

# ============================================================
# لاگ
# ============================================================

def log_event(level: str, event_type: str, user_id: Optional[int] = None, detail: Optional[str] = None) -> None:
    """ثبت لاگ. هرگز استثنا نمی‌اندازد."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO logs (level, event_type, user_id, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (sanitize_text(level, 20) or "INFO", sanitize_text(event_type, 100) or "event",
                 user_id, sanitize_text(detail, 1000), now_ts())
            )
    except Exception:
        logger.exception("log_event failed: %s", event_type)

# ============================================================
# کاربران
# ============================================================

def create_or_update_user(telegram_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    username = sanitize_text(username, 64)
    first_name = sanitize_text(first_name, 128)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT username FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if existing:
                if existing["username"] != username and username:
                    conn.execute(
                        "INSERT INTO username_history (telegram_id, username, changed_at) VALUES (?, ?, ?)",
                        (telegram_id, username, now_ts())
                    )
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_seen_at = ? WHERE telegram_id = ?",
                    (username, first_name, now_ts(), telegram_id)
                )
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO users (telegram_id, username, first_name, joined_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, username, first_name, now_ts(), now_ts())
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return dict(row) if row else None

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    username = sanitize_text(username, 64)
    if not username:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username.lstrip("@"),)
        ).fetchone()
        return dict(row) if row else None

def get_user_by_identifier(query: str) -> Optional[Dict[str, Any]]:
    query = sanitize_text(query, 100)
    if not query:
        return None
    raw = query.strip().lstrip("@")
    if re.fullmatch(r"[0-9]{1,15}", raw):
        try:
            return get_user(int(raw))
        except ValueError:
            return None
    return get_user_by_username(raw)

def get_user_registration_date(telegram_id: int) -> str:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT joined_at FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row and row["joined_at"]:
            import datetime
            return datetime.datetime.fromtimestamp(row["joined_at"]).strftime("%Y-%m-%d")
        return "نامشخص"

def increment_total_purchases(telegram_id: int) -> None:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("UPDATE users SET total_purchases = total_purchases + 1 WHERE telegram_id = ?", (telegram_id,))

def mark_trial_used(telegram_id: int) -> None:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("UPDATE users SET trial_used = 1 WHERE telegram_id = ?", (telegram_id,))

def get_user_level(total_purchases: int):
    from config import LEVEL_NAMES
    if total_purchases <= 1:
        return "new", LEVEL_NAMES.get("new", "تازه‌وارد")
    elif total_purchases <= 5:
        return "prime", LEVEL_NAMES.get("prime", "پرایم")
    elif total_purchases <= 10:
        return "royal", LEVEL_NAMES.get("royal", "رویال")
    elif total_purchases <= 20:
        return "diamond", LEVEL_NAMES.get("diamond", "دایموند")
    else:
        return "legendary", LEVEL_NAMES.get("legendary", "لجندری")

# ============================================================
# شمارنده کانفیگ
# ============================================================

def reserve_config_number(is_test: bool = False) -> int:
    key = "test_counter" if is_test else "config_counter"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            counter = int(row["value"]) if row else 0
            if is_test:
                max_row = conn.execute("SELECT COALESCE(MAX(config_number), 0) AS mx FROM subscriptions WHERE is_trial = 1").fetchone()
            else:
                max_row = conn.execute("SELECT COALESCE(MAX(config_number), 0) AS mx FROM subscriptions WHERE is_trial = 0").fetchone()
            next_num = max(counter, int(max_row["mx"])) + 1
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, str(next_num), now_ts())
            )
            conn.commit()
            return next_num
        except Exception:
            conn.rollback()
            raise

def release_config_number(number: int, is_test: bool = False) -> bool:
    key = "test_counter" if is_test else "config_counter"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            counter = int(row["value"]) if row else 0
            used = conn.execute("SELECT 1 FROM subscriptions WHERE config_number = ? AND is_trial = ?", (number, 1 if is_test else 0)).fetchone()
            if counter == number and not used:
                conn.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = ?", (str(number - 1), now_ts(), key))
                conn.commit()
                return True
            conn.rollback()
            return False
        except Exception:
            conn.rollback()
            return False

# ============================================================
# کیف پول
# ============================================================

def get_balance(telegram_id: int) -> int:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return int(row["balance"]) if row else 0

def _is_idempotent_tx(tx_type: str, reference_id: Optional[str]) -> bool:
    """تراکنش‌هایی که برای یک reference_id فقط یک‌بار مجاز هستند (PAY-01).
    خرید، پورسانت و بازگشت وجه با پیشوند refund: . سایر انواع (مثل تمدید) بدون محدودیت‌اند."""
    if not reference_id:
        return False
    return tx_type in ("purchase", "referral_commission", "wheel_prize") or str(reference_id).startswith("refund:")


def change_balance(telegram_id: int, delta: int, tx_type: str, description: str = "", reference_id: Optional[str] = None) -> bool:
    """تغییر اتمیک موجودی.
    اگر تراکنش idempotent باشد و قبلاً ثبت شده باشد: برای واریز (delta>0) True (قبلاً انجام شده،
    دوباره اعمال نمی‌شود) و برای کسر False (جلوگیری از کسر/ساخت تکراری) برمی‌گردد."""
    telegram_id = validate_telegram_id(telegram_id)
    delta = int(delta)
    if delta == 0 or abs(delta) > 500_000_000:
        return False
    if reference_id is not None:
        reference_id = sanitize_text(str(reference_id), 200)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _is_idempotent_tx(tx_type, reference_id):
                dup = conn.execute(
                    "SELECT 1 FROM wallet_transactions WHERE type = ? AND reference_id = ? LIMIT 1",
                    (tx_type, reference_id)
                ).fetchone()
                if dup:
                    conn.rollback()
                    logger.warning("Duplicate wallet tx ignored: type=%s ref=%s user=%s", tx_type, reference_id, telegram_id)
                    return delta > 0
            row = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if not row:
                conn.rollback()
                return False
            before = int(row["balance"])
            after = before + delta
            if after < 0:
                conn.rollback()
                return False
            conn.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (after, telegram_id))
            conn.execute(
                "INSERT INTO wallet_transactions (telegram_id, amount, balance_before, balance_after, type, description, reference_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (telegram_id, delta, before, after, tx_type, sanitize_text(description, 500), reference_id, now_ts())
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def add_balance(telegram_id: int, amount: int, tx_type: str, description: str = "", reference_id: Optional[str] = None) -> bool:
    return change_balance(telegram_id, validate_amount(amount), tx_type, description, reference_id)

def deduct_balance(telegram_id: int, amount: int, tx_type: str, description: str = "", reference_id: Optional[str] = None) -> bool:
    return change_balance(telegram_id, -validate_amount(amount), tx_type, description, reference_id)

def get_wallet_transactions(telegram_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """دریافت تاریخچه تراکنش‌های کیف پول کاربر."""
    telegram_id = validate_telegram_id(telegram_id)
    try:
        limit = max(1, min(int(limit), 100))
    except (ValueError, TypeError):
        limit = 10
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_transactions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

# ============================================================
# درخواست‌های پرداخت
# ============================================================

def create_payment_request(telegram_id: int, amount: int, photo_file_id: str) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    amount = validate_amount(amount)
    photo_file_id = sanitize_text(photo_file_id, 200)
    if not photo_file_id:
        return None
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            pending = conn.execute(
                "SELECT id FROM payment_requests WHERE telegram_id = ? AND status = 'pending'",
                (telegram_id,)
            ).fetchone()
            if pending:
                conn.rollback()
                return None
            cursor = conn.execute(
                "INSERT INTO payment_requests (telegram_id, amount, photo_file_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (telegram_id, amount, photo_file_id, now_ts())
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise

def get_payment_request(payment_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (payment_id,)).fetchone()
        return dict(row) if row else None

def approve_payment(payment_id: int, admin_id: int) -> bool:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            req = conn.execute("SELECT * FROM payment_requests WHERE id = ? AND status = 'pending'", (payment_id,)).fetchone()
            if not req:
                conn.rollback()
                return False
            user = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (req["telegram_id"],)).fetchone()
            if not user:
                conn.rollback()
                return False
            before = int(user["balance"])
            after = before + int(req["amount"])
            conn.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (after, req["telegram_id"]))
            conn.execute(
                "INSERT INTO wallet_transactions (telegram_id, amount, balance_before, balance_after, type, description, created_at) "
                "VALUES (?, ?, ?, ?, 'card_payment', ?, ?)",
                (req["telegram_id"], req["amount"], before, after, f"Payment #{payment_id}", now_ts())
            )
            conn.execute(
                "UPDATE payment_requests SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (admin_id, now_ts(), payment_id)
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def reject_payment(payment_id: int, admin_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE payment_requests SET status = 'rejected', reviewed_by = ?, reviewed_at = ? WHERE id = ? AND status = 'pending'",
            (admin_id, now_ts(), payment_id)
        )
        return cursor.rowcount > 0

# ============================================================
# Stars — Order, Payment and Finalization
# ============================================================

def create_stars_invoice(
    payload: str,
    telegram_id: int,
    volume_gb: float,
    days: int,
    devices: int,
    amount_stars: int,
    product_id: Optional[int] = None,
    price_toman: int = 0,
    discount_id: Optional[int] = None,
    discount_code: Optional[str] = None,
    discount_percent: int = 0,
    discount_amount_toman: int = 0,
    purchase_chat_id: Optional[int] = None,
    purchase_message_id: Optional[int] = None,
) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    payload = sanitize_text(payload, 100)

    try:
        volume_gb = float(volume_gb)
        days = int(days)
        devices = int(devices)
        amount_stars = validate_amount(amount_stars, 5000)
        price_toman = int(price_toman)
        discount_percent = int(discount_percent)
        discount_amount_toman = int(discount_amount_toman)
        product_id = int(product_id) if product_id is not None else None
        discount_id = int(discount_id) if discount_id is not None else None
        purchase_chat_id = int(purchase_chat_id) if purchase_chat_id is not None else None
        purchase_message_id = int(purchase_message_id) if purchase_message_id is not None else None
    except (TypeError, ValueError):
        return None

    if (
        not payload
        or volume_gb < 0
        or volume_gb > 1000
        or not 0 <= days <= 3650
        or not 0 <= devices <= 10
        or price_toman < 0
        or not 0 <= discount_percent <= 100
        or discount_amount_toman < 0
    ):
        return None

    timestamp = now_ts()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            user = conn.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

            if not user:
                conn.rollback()
                return None

            if product_id is not None:
                product = conn.execute(
                    """
                    SELECT id, kind, volume_gb, days, price_toman, price_stars, panel_id
                    FROM products
                    WHERE id = ? AND is_active = 1
                    """,
                    (product_id,),
                ).fetchone()

                if (
                    not product
                    or product["kind"] not in ("vol", "unl")
                    or float(product["volume_gb"]) != volume_gb
                    or (product["kind"] == "unl" and int(product["days"]) != days)
                ):
                    conn.rollback()
                    return None

            cursor = conn.execute(
                """
                INSERT INTO stars_invoices (
                    payload,
                    telegram_id,
                    kind,
                    volume_gb,
                    days,
                    devices,
                    amount_stars,
                    status,
                    workflow_status,
                    created_at,
                    updated_at,
                    product_id,
                    price_toman,
                    discount_id,
                    discount_code,
                    discount_percent,
                    discount_amount_toman,
                    purchase_chat_id,
                    purchase_message_id,
                    panel_id
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    'pending', 'pending',
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    payload,
                    telegram_id,
                    (product["kind"] if product_id is not None else "vol"),
                    volume_gb,
                    days,
                    devices,
                    amount_stars,
                    timestamp,
                    timestamp,
                    product_id,
                    price_toman,
                    discount_id,
                    sanitize_text(discount_code, 50),
                    discount_percent,
                    discount_amount_toman,
                    purchase_chat_id,
                    purchase_message_id,
                    (product["panel_id"] if product_id is not None else None),
                ),
            )

            conn.commit()
            return int(cursor.lastrowid)

        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        except Exception:
            conn.rollback()
            raise


def get_stars_invoice(payload: str) -> Optional[Dict[str, Any]]:
    payload = sanitize_text(payload, 100)
    if not payload:
        return None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM stars_invoices WHERE payload = ?",
            (payload,),
        ).fetchone()

        return dict(row) if row else None


def update_stars_invoice_status(payload: str, status: str) -> bool:
    payload = sanitize_text(payload, 100)

    allowed = {
        "pending",
        "paid",
        "delivering",
        "delivered",
        "refund_pending",
        "refunded",
        "expired",
        "failed",
    }

    if not payload or status not in allowed:
        return False

    timestamp = now_ts()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT workflow_status FROM stars_invoices WHERE payload = ?",
                (payload,),
            ).fetchone()

            if not row:
                conn.rollback()
                return False

            current = str(row["workflow_status"] or "pending")

            if current == "delivered" and status != "delivered":
                conn.rollback()
                return False

            if current == "refunded" and status != "refunded":
                conn.rollback()
                return False

            legacy_status = (
                status if status in ("pending", "paid", "delivered", "refunded")
                else None
            )

            conn.execute(
                """
                UPDATE stars_invoices
                SET workflow_status = ?,
                    status = COALESCE(?, status),
                    updated_at = ?,
                    paid_at = CASE
                        WHEN ? = 'paid' THEN COALESCE(paid_at, ?)
                        ELSE paid_at
                    END,
                    delivered_at = CASE
                        WHEN ? = 'delivered' THEN COALESCE(delivered_at, ?)
                        ELSE delivered_at
                    END
                WHERE payload = ?
                """,
                (
                    status,
                    legacy_status,
                    timestamp,
                    status,
                    timestamp,
                    status,
                    timestamp,
                    payload,
                ),
            )

            conn.commit()
            return True

        except Exception:
            conn.rollback()
            raise


def register_stars_charge(
    charge_id: str,
    telegram_id: int,
    payload: str,
    amount_stars: int,
) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    charge_id = sanitize_text(charge_id, 200)
    payload = sanitize_text(payload, 100)

    try:
        amount_stars = validate_amount(amount_stars, 5000)
    except ValueError:
        return False

    if not charge_id or not payload:
        return False

    timestamp = now_ts()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            invoice = conn.execute(
                """
                SELECT telegram_id, amount_stars, workflow_status
                FROM stars_invoices
                WHERE payload = ?
                """,
                (payload,),
            ).fetchone()

            if (
                not invoice
                or int(invoice["telegram_id"]) != telegram_id
                or int(invoice["amount_stars"]) != amount_stars
                or invoice["workflow_status"] not in ("pending", "paid", "delivering")
            ):
                conn.rollback()
                return False

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO stars_charges (
                    charge_id,
                    telegram_id,
                    payload,
                    amount_stars,
                    status,
                    workflow_status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'charged', 'charged', ?, ?)
                """,
                (
                    charge_id,
                    telegram_id,
                    payload,
                    amount_stars,
                    timestamp,
                    timestamp,
                ),
            )

            if cursor.rowcount != 1:
                conn.rollback()
                return False

            conn.execute(
                """
                UPDATE stars_invoices
                SET workflow_status = 'paid',
                    status = 'paid',
                    paid_at = COALESCE(paid_at, ?),
                    updated_at = ?
                WHERE payload = ?
                  AND workflow_status = 'pending'
                """,
                (timestamp, timestamp, payload),
            )

            conn.commit()
            return True

        except Exception:
            conn.rollback()
            raise


def update_stars_charge_status(charge_id: str, status: str) -> bool:
    charge_id = sanitize_text(charge_id, 200)

    allowed = {
        "charged",
        "delivering",
        "delivered",
        "refund_pending",
        "refunded",
        "failed",
    }

    if not charge_id or status not in allowed:
        return False

    timestamp = now_ts()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT workflow_status FROM stars_charges WHERE charge_id = ?",
                (charge_id,),
            ).fetchone()

            if not row:
                conn.rollback()
                return False

            current = str(row["workflow_status"] or "charged")

            if current == "delivered" and status != "delivered":
                conn.rollback()
                return False

            if current == "refunded" and status != "refunded":
                conn.rollback()
                return False

            legacy_status = (
                status if status in ("charged", "delivered", "refunded")
                else None
            )

            conn.execute(
                """
                UPDATE stars_charges
                SET workflow_status = ?,
                    status = COALESCE(?, status),
                    updated_at = ?,
                    delivered_at = CASE
                        WHEN ? = 'delivered' THEN COALESCE(delivered_at, ?)
                        ELSE delivered_at
                    END,
                    refunded_at = CASE
                        WHEN ? = 'refunded' THEN COALESCE(refunded_at, ?)
                        ELSE refunded_at
                    END
                WHERE charge_id = ?
                """,
                (
                    status,
                    legacy_status,
                    timestamp,
                    status,
                    timestamp,
                    status,
                    timestamp,
                    charge_id,
                ),
            )

            conn.commit()
            return True

        except Exception:
            conn.rollback()
            raise
# ============================================================
# سرویس‌ها
# ============================================================

def create_subscription(
    telegram_id: int, config_number: int, config_name: str, kind: str,
    volume_gb: float, days: int, devices: int,
    price_toman: int, price_stars: int,
    is_trial: int = 0, paxiva_client_id: Optional[int] = None, link: Optional[str] = None,
    panel_id: Optional[int] = None, product_id: Optional[int] = None
) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    kind = str(kind).lower().strip()
    if kind not in ("vol", "unl"):
        raise ValueError("Invalid subscription kind")
    days = 0 if kind == "vol" else max(1, int(days or 30))
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO subscriptions 
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, price_stars, is_trial, paxiva_client_id, panel_id, product_id, link, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, price_stars, is_trial, paxiva_client_id, panel_id, product_id, link, now_ts(), now_ts())
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise

def finalize_purchase(
    telegram_id: int, config_number: int, config_name: str, kind: str,
    volume_gb: float, days: int, devices: int, price_toman: int,
    paxiva_client_id: int, link: str, discount_id: Optional[int] = None,
    panel_id: Optional[int] = None, product_id: Optional[int] = None,
) -> int:
    """FLOW-02: ثبت اشتراک + مصرف کد تخفیف + شمارش خرید + پورسانت رفرال در «یک» تراکنش.
    یا همه انجام می‌شود یا هیچ‌کدام. در صورت ناموجود بودن کد تخفیف ValueError می‌دهد.
    شناسهٔ اشتراک را برمی‌گرداند."""
    telegram_id = validate_telegram_id(telegram_id)
    kind = str(kind).lower().strip()
    if kind not in ("vol", "unl"):
        raise ValueError("Invalid subscription kind")
    days = 0 if kind == "vol" else max(1, int(days or 30))
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            resolved_product_id = int(product_id) if product_id is not None else None
            if resolved_product_id is None:
                matches = conn.execute(
                    """SELECT id FROM products
                       WHERE is_active = 1 AND kind = ? AND volume_gb = ?
                         AND (? = 'vol' OR days = ?)
                         AND price_toman = ? AND (? IS NULL OR panel_id = ?)
                       ORDER BY id DESC LIMIT 2""",
                    (kind, float(volume_gb), kind, days, int(price_toman), panel_id, panel_id),
                ).fetchall()
                if len(matches) == 1:
                    resolved_product_id = int(matches[0]["id"])
            if resolved_product_id is not None:
                product_row = conn.execute(
                    "SELECT id, kind, panel_id FROM products WHERE id = ?",
                    (resolved_product_id,),
                ).fetchone()
                if not product_row or product_row["kind"] != kind:
                    raise ValueError("product_mismatch")
                if panel_id is not None and product_row["panel_id"] != panel_id:
                    raise ValueError("product_panel_mismatch")
            cursor = conn.execute(
                """INSERT INTO subscriptions
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, price_stars, is_trial, paxiva_client_id, panel_id, product_id, link, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, 'active', ?, ?)""",
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, paxiva_client_id, panel_id, resolved_product_id, link, now_ts(), now_ts())
            )
            sub_id = int(cursor.lastrowid)
            if discount_id is not None:
                code = conn.execute("SELECT * FROM discount_codes WHERE id = ? AND is_active = 1", (int(discount_id),)).fetchone()
                if not code or code["used_count"] >= code["max_uses"] or not discount_usable_by(code, telegram_id):
                    raise ValueError("discount_unavailable")
                if conn.execute("SELECT 1 FROM discount_uses WHERE code_id = ? AND telegram_id = ?",
                                (int(discount_id), telegram_id)).fetchone():
                    raise ValueError("discount_unavailable")
                conn.execute("INSERT INTO discount_uses (code_id, telegram_id, used_at) VALUES (?, ?, ?)",
                             (int(discount_id), telegram_id, now_ts()))
                new_count = code["used_count"] + 1
                conn.execute("UPDATE discount_codes SET used_count = ?, is_active = ? WHERE id = ?",
                             (new_count, 0 if new_count >= code["max_uses"] else 1, int(discount_id)))
            conn.execute("UPDATE users SET total_purchases = total_purchases + 1 WHERE telegram_id = ?", (telegram_id,))
            try:
                # شکست پورسانت نباید خرید را خراب کند
                conn.execute("SAVEPOINT ref")
                _apply_referral_commission(conn, telegram_id, price_toman, sub_id, "purchase")
                conn.execute("RELEASE ref")
            except sqlite3.Error:
                conn.execute("ROLLBACK TO ref")
                conn.execute("RELEASE ref")
                logger.exception("Referral commission failed inside finalize_purchase")
            conn.commit()
            return sub_id
        except Exception:
            conn.rollback()
            raise
def finalize_stars_purchase(
    payload: str,
    charge_id: str,
    telegram_id: int,
    config_number: int,
    config_name: str,
    volume_gb: float,
    days: int,
    devices: int,
    price_toman: int,
    price_stars: int,
    paxiva_client_id: int,
    link: str,
) -> int:
    """
    ثبت نهایی خرید Stars در یک تراکنش:

    - اعتبارسنجی فاکتور و پرداخت
    - جلوگیری از تحویل تکراری
    - مصرف اتمیک کد تخفیف
    - ثبت اشتراک با kind='vol'
    - افزایش تعداد خرید
    - ثبت پورسانت رفرال
    - ثبت نهایی پرداخت و فاکتور

    در صورت اجرای مجدد همان پرداخت، شناسه اشتراک قبلی برگردانده می‌شود.
    """

    telegram_id = validate_telegram_id(telegram_id)
    payload = sanitize_text(payload, 100)
    charge_id = sanitize_text(charge_id, 200)
    config_name = sanitize_text(config_name, 100)
    link = sanitize_text(link, 4000)

    try:
        config_number = int(config_number)
        volume_gb = float(volume_gb)
        days = int(days)
        devices = int(devices)
        price_toman = int(price_toman)
        price_stars = validate_amount(price_stars, 5000)
        paxiva_client_id = int(paxiva_client_id)
    except (TypeError, ValueError):
        raise ValueError("invalid_stars_purchase_data")

    if (
        not payload
        or not charge_id
        or not config_name
        or not link
        or config_number <= 0
        or volume_gb < 0
        or not 0 <= devices <= 10
        or not 0 <= days <= 3650
        or price_toman < 0
        or paxiva_client_id <= 0
    ):
        raise ValueError("invalid_stars_purchase_data")

    timestamp = now_ts()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")

        try:
            # ------------------------------------------------
            # 1. بررسی فاکتور
            # ------------------------------------------------

            invoice = conn.execute(
                """
                SELECT *
                FROM stars_invoices
                WHERE payload = ?
                """,
                (payload,),
            ).fetchone()

            if not invoice:
                raise ValueError("stars_invoice_not_found")

            if int(invoice["telegram_id"]) != telegram_id:
                raise ValueError("stars_invoice_user_mismatch")

            if int(invoice["amount_stars"]) != price_stars:
                raise ValueError("stars_amount_mismatch")

            if float(invoice["volume_gb"]) != volume_gb:
                raise ValueError("stars_volume_mismatch")

            if int(invoice["days"]) != days:
                raise ValueError("stars_days_mismatch")

            if int(invoice["devices"]) != devices:
                raise ValueError("stars_devices_mismatch")

            # ------------------------------------------------
            # 2. بررسی پرداخت
            # ------------------------------------------------

            charge = conn.execute(
                """
                SELECT *
                FROM stars_charges
                WHERE charge_id = ?
                """,
                (charge_id,),
            ).fetchone()

            if not charge:
                raise ValueError("stars_charge_not_found")

            if (
                int(charge["telegram_id"]) != telegram_id
                or charge["payload"] != payload
                or int(charge["amount_stars"]) != price_stars
            ):
                raise ValueError("stars_charge_mismatch")

            # ------------------------------------------------
            # 3. بررسی تحویل قبلی
            # ------------------------------------------------

            existing = conn.execute(
                """
                SELECT subscription_id, telegram_id
                FROM stars_fulfillments
                WHERE charge_id = ?
                """,
                (charge_id,),
            ).fetchone()

            if existing:
                if int(existing["telegram_id"]) != telegram_id:
                    raise ValueError("stars_fulfillment_user_mismatch")

                conn.commit()
                return int(existing["subscription_id"])

            if invoice["workflow_status"] not in ("paid", "delivering"):
                raise ValueError("stars_invoice_not_paid")

            if charge["workflow_status"] not in ("charged", "delivering"):
                raise ValueError("stars_charge_not_payable")

            # ------------------------------------------------
            # 4. بررسی کد تخفیف
            # ------------------------------------------------

            discount_id = invoice["discount_id"]

            if discount_id is not None:
                code = conn.execute(
                    """
                    SELECT *
                    FROM discount_codes
                    WHERE id = ?
                      AND is_active = 1
                    """,
                    (int(discount_id),),
                ).fetchone()

                if (
                    not code
                    or int(code["used_count"]) >= int(code["max_uses"])
                    or not discount_usable_by(code, telegram_id)
                ):
                    raise ValueError("discount_unavailable")

                already_used = conn.execute(
                    """
                    SELECT 1
                    FROM discount_uses
                    WHERE code_id = ?
                      AND telegram_id = ?
                    """,
                    (int(discount_id), telegram_id),
                ).fetchone()

                if already_used:
                    raise ValueError("discount_already_used")

                conn.execute(
                    """
                    INSERT INTO discount_uses (
                        code_id,
                        telegram_id,
                        used_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (int(discount_id), telegram_id, timestamp),
                )

                new_count = int(code["used_count"]) + 1

                conn.execute(
                    """
                    UPDATE discount_codes
                    SET used_count = ?,
                        is_active = ?
                    WHERE id = ?
                    """,
                    (
                        new_count,
                        0 if new_count >= int(code["max_uses"]) else 1,
                        int(discount_id),
                    ),
                )

            # ------------------------------------------------
            # 5. ثبت اشتراک
            # ------------------------------------------------

            cursor = conn.execute(
                """
                INSERT INTO subscriptions (
                    telegram_id,
                    config_number,
                    config_name,
                    kind,
                    volume_gb,
                    days,
                    devices,
                    price_toman,
                    price_stars,
                    is_trial,
                    paxiva_client_id,
                    panel_id,
                    product_id,
                    link,
                    state,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    0, ?, ?, ?, ?, 'active', ?, ?
                )
                """,
                (
                    telegram_id,
                    config_number,
                    config_name,
                    (invoice["kind"] or "vol"),
                    volume_gb,
                    (0 if (invoice["kind"] or "vol") == "vol" else days),
                    devices,
                    price_toman,
                    price_stars,
                    paxiva_client_id,
                    invoice["panel_id"],
                    invoice["product_id"],
                    link,
                    timestamp,
                    timestamp,
                ),
            )

            subscription_id = int(cursor.lastrowid)

            # ------------------------------------------------
            # 6. ثبت یکتای تحویل
            # ------------------------------------------------

            conn.execute(
                """
                INSERT INTO stars_fulfillments (
                    charge_id,
                    payload,
                    telegram_id,
                    subscription_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    charge_id,
                    payload,
                    telegram_id,
                    subscription_id,
                    timestamp,
                ),
            )

            # ------------------------------------------------
            # 7. افزایش شمارنده خرید
            # ------------------------------------------------

            conn.execute(
                """
                UPDATE users
                SET total_purchases = total_purchases + 1
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            )

            # ------------------------------------------------
            # 8. پورسانت رفرال
            # ------------------------------------------------

            try:
                conn.execute("SAVEPOINT stars_referral")

                _apply_referral_commission(
                    conn,
                    telegram_id,
                    price_toman,
                    subscription_id,
                    "stars_purchase",
                )

                conn.execute("RELEASE stars_referral")

            except sqlite3.Error:
                conn.execute("ROLLBACK TO stars_referral")
                conn.execute("RELEASE stars_referral")
                logger.exception(
                    "Stars referral commission failed: user=%s",
                    telegram_id,
                )

            # ------------------------------------------------
            # 9. نهایی‌کردن فاکتور
            # ------------------------------------------------

            conn.execute(
                """
                UPDATE stars_invoices
                SET status = 'delivered',
                    workflow_status = 'delivered',
                    subscription_id = ?,
                    paxiva_client_id = ?,
                    panel_id = ?,
                    config_number = ?,
                    config_name = ?,
                    delivered_at = ?,
                    updated_at = ?,
                    last_error = NULL
                WHERE payload = ?
                """,
                (
                    subscription_id,
                    paxiva_client_id,
                    invoice["panel_id"],
                    config_number,
                    config_name,
                    timestamp,
                    timestamp,
                    payload,
                ),
            )

            conn.execute(
                """
                UPDATE stars_charges
                SET status = 'delivered',
                    workflow_status = 'delivered',
                    subscription_id = ?,
                    paxiva_client_id = ?,
                    panel_id = ?,
                    config_number = ?,
                    delivered_at = ?,
                    updated_at = ?,
                    last_error = NULL
                WHERE charge_id = ?
                """,
                (
                    subscription_id,
                    paxiva_client_id,
                    invoice["panel_id"],
                    config_number,
                    timestamp,
                    timestamp,
                    charge_id,
                ),
            )

            conn.commit()
            return subscription_id

        except Exception:
            conn.rollback()
            raise
def get_user_subscriptions(telegram_id: int, only_visible: bool = True) -> List[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        query = "SELECT * FROM subscriptions WHERE telegram_id = ?"
        if only_visible:
            query += " AND deleted_from_list_at IS NULL"
        query += " ORDER BY id DESC"
        rows = conn.execute(query, (telegram_id,)).fetchall()
        return [dict(r) for r in rows]

def get_subscription(subscription_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        return dict(row) if row else None

def update_subscription_state(subscription_id: int, state: str, expires_at: Optional[int] = None, used_bytes: Optional[int] = None) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET state = ?, expires_at = COALESCE(?, expires_at), used_bytes = COALESCE(?, used_bytes), updated_at = ? WHERE id = ?",
            (state, expires_at, used_bytes, now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def update_subscription_volume(subscription_id: int, new_volume: float) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET volume_gb = ?, updated_at = ? WHERE id = ?",
            (new_volume, now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def toggle_auto_renew(subscription_id: int, enabled: bool) -> bool:
    subscription_id = int(subscription_id)
    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sub = conn.execute(
                "SELECT product_id, is_trial, state FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            if not sub:
                conn.rollback()
                return False
            if enabled:
                if int(sub["is_trial"] or 0) or sub["state"] != "active" or not sub["product_id"]:
                    conn.rollback()
                    return False
                product = conn.execute(
                    "SELECT id FROM products WHERE id = ? AND is_active = 1",
                    (int(sub["product_id"]),),
                ).fetchone()
                if not product:
                    conn.rollback()
                    return False
            cursor = conn.execute(
                "UPDATE subscriptions SET auto_renew = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, timestamp, subscription_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise


def set_subscription_product(subscription_id: int, product_id: int) -> bool:
    """اتصال دستی سرویس قدیمی به محصول اصلی پس از اطمینان از نوع و پنل."""
    subscription_id = int(subscription_id)
    product_id = int(product_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sub = conn.execute(
                "SELECT kind, panel_id FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            product = conn.execute(
                "SELECT kind, panel_id FROM products WHERE id = ? AND is_active = 1",
                (product_id,),
            ).fetchone()
            if not sub or not product or sub["kind"] != product["kind"]:
                conn.rollback()
                return False
            if sub["panel_id"] is not None and sub["panel_id"] != product["panel_id"]:
                conn.rollback()
                return False
            cursor = conn.execute(
                "UPDATE subscriptions SET product_id = ?, updated_at = ? WHERE id = ?",
                (product_id, now_ts(), subscription_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise

def delete_subscription_from_list(subscription_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET deleted_from_list_at = ? WHERE id = ?",
            (now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def hard_delete_subscription(subscription_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
        return cursor.rowcount > 0

# ============================================================
# رفرال و پورسانت
# ============================================================

def set_referral(referrer_id: int, referred_id: int) -> bool:
    referrer_id = validate_telegram_id(referrer_id)
    referred_id = validate_telegram_id(referred_id)
    if referrer_id == referred_id:
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
            (referrer_id, referred_id, now_ts())
        )
        return cursor.rowcount > 0

def get_referrer(referred_id: int) -> Optional[int]:
    referred_id = validate_telegram_id(referred_id)
    with get_connection() as conn:
        row = conn.execute("SELECT referrer_id FROM referrals WHERE referred_id = ?", (referred_id,)).fetchone()
        return int(row["referrer_id"]) if row else None

def _apply_referral_commission(conn, referred_id: int, purchase_amount: int, subscription_id: int, source: str) -> bool:
    """پورسانت رفرال داخل تراکنشِ باز (conn). یک پورسانت برای هر (subscription, source)."""
    row = conn.execute("SELECT referrer_id FROM referrals WHERE referred_id = ?", (referred_id,)).fetchone()
    if not row:
        return False
    referrer_id = int(row["referrer_id"])
    commission = int(purchase_amount) * 5 // 100
    if commission <= 0:
        return False
    if conn.execute(
        "SELECT 1 FROM referral_earnings WHERE subscription_id = ? AND source = ? AND referred_id = ? LIMIT 1",
        (subscription_id, source, referred_id)
    ).fetchone():
        return False  # REF-01: پورسانت تکراری
    bal = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (referrer_id,)).fetchone()
    if not bal:
        return False
    before = int(bal["balance"])
    after = before + commission
    conn.execute(
        "INSERT INTO referral_earnings (referrer_id, referred_id, amount, source, subscription_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (referrer_id, referred_id, commission, source, subscription_id, now_ts())
    )
    conn.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (after, referrer_id))
    conn.execute(
        "INSERT INTO wallet_transactions (telegram_id, amount, balance_before, balance_after, type, description, reference_id, created_at) "
        "VALUES (?, ?, ?, ?, 'referral_commission', ?, ?, ?)",
        (referrer_id, commission, before, after, f"پورسانت ۵٪ از خرید {referred_id}", f"{source}:{subscription_id}", now_ts())
    )
    return True
def consume_pending_referral(referred_id: int) -> Optional[int]:
    """
    اگر پیش از تأیید عضویت اجباری، رفرال این کاربر به‌صورت موقت در settings
    ذخیره شده بود (توسط ReferralCaptureMiddleware)، اینجا نهایی می‌شود.
    کلید موقت در هر صورت پاک می‌شود تا دوباره استفاده نشود.
    شناسه معرفی که ثبت شد را برمی‌گرداند، یا None.
    """
    referred_id = validate_telegram_id(referred_id)
    key = f"pending_ref:{referred_id}"
    pending = get_setting(key)
    if not pending:
        return None

    try:
        referrer_id = validate_telegram_id(pending)
    except ValueError:
        set_setting(key, "")
        return None

    set_setting(key, "")  # یک‌بار مصرف؛ فوراً پاک می‌شود

    if referrer_id == referred_id:
        return None
    if get_referrer(referred_id):
        return None  # قبلاً معرف دائمی دارد؛ تغییر نمی‌کند
    if not get_user(referrer_id):
        return None

    return referrer_id if set_referral(referrer_id, referred_id) else None
def add_referral_commission(referred_id: int, purchase_amount: int, subscription_id: int, source: str = "purchase") -> bool:
    referred_id = validate_telegram_id(referred_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ok = _apply_referral_commission(conn, referred_id, purchase_amount, int(subscription_id), source)
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise

def get_referral_stats(telegram_id: int) -> Dict[str, Any]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (telegram_id,)).fetchone()["c"]
        total = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM referral_earnings WHERE referrer_id = ?", (telegram_id,)).fetchone()["s"]
        return {"count": int(count), "total_earnings": int(total)}

def get_user_referrals(referrer_id: int) -> List[Dict[str, Any]]:
    referrer_id = validate_telegram_id(referrer_id)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM referrals WHERE referrer_id = ? ORDER BY id DESC", (referrer_id,)).fetchall()
        return [dict(r) for r in rows]

def get_referral_details(referrer_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    referrer_id = validate_telegram_id(referrer_id)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM referral_earnings WHERE referrer_id = ? ORDER BY id DESC LIMIT ?", (referrer_id, limit)).fetchall()
        return [dict(r) for r in rows]

# ============================================================
# کد تخفیف
# ============================================================

def create_discount_code(code: str, percent: int, max_uses: int,
                         owner_id: Optional[int] = None, expires_days: Optional[int] = None) -> Optional[int]:
    code = sanitize_text(code, 50)
    try:
        percent, max_uses = int(percent), int(max_uses)
    except (TypeError, ValueError):
        return None
    if not code or not (1 <= percent <= 100) or not (1 <= max_uses <= 1_000_000):
        return None
    with get_connection() as conn:
        try:
            expires_at = now_ts() + int(expires_days) * 86400 if expires_days else None
            cursor = conn.execute(
                "INSERT INTO discount_codes (code, percent, max_uses, used_count, is_active, created_at, owner_id, expires_at) "
                "VALUES (?, ?, ?, 0, 1, ?, ?, ?)",
                (code, percent, max_uses, now_ts(), int(owner_id) if owner_id else None, expires_at)
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            conn.rollback()
            return None

def get_discount_code(code: str) -> Optional[Dict[str, Any]]:
    code = sanitize_text(code, 50)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM discount_codes WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
        return dict(row) if row else None

def discount_usable_by(code: Any, telegram_id: int) -> bool:
    """آیا این کد برای این کاربر قابل استفاده است؟ (مالک اختصاصی + تاریخ انقضا)"""
    try:
        owner = code["owner_id"]
        expires = code["expires_at"]
    except (KeyError, IndexError, TypeError):
        return True  # دیتابیس قدیمی بدون این ستون‌ها
    if owner and int(owner) != int(telegram_id):
        return False
    if expires and now_ts() > int(expires):
        return False
    return True

def consume_discount_code(code_id: int, telegram_id: int) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            code = conn.execute("SELECT * FROM discount_codes WHERE id = ? AND is_active = 1", (code_id,)).fetchone()
            if not code or code["used_count"] >= code["max_uses"] or not discount_usable_by(code, telegram_id):
                conn.rollback()
                return False
            used = conn.execute("SELECT 1 FROM discount_uses WHERE code_id = ? AND telegram_id = ?", (code_id, telegram_id)).fetchone()
            if used:
                conn.rollback()
                return False
            conn.execute("INSERT INTO discount_uses (code_id, telegram_id, used_at) VALUES (?, ?, ?)", (code_id, telegram_id, now_ts()))
            new_count = code["used_count"] + 1
            active = 0 if new_count >= code["max_uses"] else 1
            conn.execute("UPDATE discount_codes SET used_count = ?, is_active = ? WHERE id = ?", (new_count, active, code_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

# ============================================================
# بن
# ============================================================

def ban_user(telegram_id: int, admin_id: int, reason: str = "", until: Optional[int] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    admin_id = validate_telegram_id(admin_id)
    if until is not None:
        until = int(until)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE bans SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
            conn.execute(
                "INSERT INTO bans (telegram_id, banned_by, reason, until, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (telegram_id, admin_id, sanitize_text(reason, 500), until, now_ts())
            )
            conn.execute("UPDATE users SET is_banned = 1, ban_until = ? WHERE telegram_id = ?", (until, telegram_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def unban_user(telegram_id: int, admin_id: Optional[int] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE bans SET is_active = 0, unbanned_by = ?, unbanned_at = ? WHERE telegram_id = ? AND is_active = 1",
                (admin_id, now_ts(), telegram_id)
            )
            conn.execute("UPDATE users SET is_banned = 0, ban_until = NULL WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def is_banned(telegram_id: int) -> bool:
    """
    مسدود است اگر is_banned=1 باشد و (بن دائمی باشد یا مهلتش نگذشته باشد).
    (نسخهٔ قبلی بن دائمی را اشتباهاً «غیرمسدود» می‌دانست.)
    """
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_banned, ban_until FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if not row or not row["is_banned"]:
        return False
    until = row["ban_until"]
    if until and int(until) < now_ts():
        unban_user(telegram_id)
        return False
    return True

# ============================================================
# محصولات
# ============================================================

def create_product(kind: str, volume_gb: float, days: int, price_toman: int, price_stars: int, panel_id: Optional[int] = None) -> Optional[int]:
    kind = str(kind).strip().lower()
    volume_gb = float(volume_gb)
    price_toman = int(price_toman)
    price_stars = int(price_stars)
    if kind not in ("vol", "unl") or volume_gb < 0 or price_toman < 0 or price_stars < 0:
        raise ValueError("Invalid product data")
    if panel_id is None:
        raise ValueError("panel_id_required")
    # حجمی تاریخ انقضا ندارد؛ نامحدود یک‌ماهه است.
    days = 0 if kind == "vol" else 30
    panel_id = _validate_panel_id(panel_id)
    with get_connection() as conn:
        panel = conn.execute("SELECT id FROM panels WHERE id = ? AND is_active = 1", (panel_id,)).fetchone()
        if not panel:
            raise ValueError("Panel not found or inactive")
        cursor = conn.execute(
            "INSERT INTO products (kind, volume_gb, days, price_toman, price_stars, panel_id, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (kind, volume_gb, days, price_toman, price_stars, panel_id, now_ts())
        )
        return cursor.lastrowid

def list_products(active_only: bool = True) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM products"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id DESC"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

# ------------------------- گردونه: چرخش اتمیک با شرط رفرال روزانه -------------------------

def tehran_day_start_ts() -> int:
    """زمان شروع «امروز» (۰۰:۰۰) به وقت تهران، به‌صورت timestamp."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Tehran")
    except Exception:
        tz = timezone(timedelta(hours=3, minutes=30))
    now = datetime.now(tz)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

def count_referrals_since(referrer_id: int, since_ts: int) -> int:
    """تعداد دعوت‌های ثبت‌شده از since_ts به بعد (از جدول referrals)."""
    referrer_id = validate_telegram_id(referrer_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND created_at >= ?",
            (referrer_id, int(since_ts))
        ).fetchone()
        return int(row["c"])

def try_record_spin(telegram_id: int, prize: str, prize_type: str,
                    cooldown_hours: int = 24, min_referrals_today: int = 1) -> Tuple[Optional[int], str]:
    """
    در «یک» تراکنش: محدودیت زمانی + شرط رفرال امروز را چک می‌کند و چرخش را ثبت می‌کند.
    (spin_id, "ok") یا (None, "cooldown" | "no_referral_today").
    دو کلیک هم‌زمان نمی‌توانند دو چرخش بگیرند.
    """
    telegram_id = validate_telegram_id(telegram_id)
    prize = sanitize_text(prize, 100) or "?"
    prize_type = sanitize_text(prize_type, 20) or "nothing"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute(
                "SELECT created_at FROM lucky_wheel_spins WHERE telegram_id = ? ORDER BY id DESC LIMIT 1",
                (telegram_id,)
            ).fetchone()
            if last and now_ts() - int(last["created_at"]) < int(cooldown_hours) * 3600:
                conn.rollback()
                return None, "cooldown"
            if min_referrals_today > 0:
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND created_at >= ?",
                    (telegram_id, tehran_day_start_ts())
                ).fetchone()["c"]
                if int(cnt) < int(min_referrals_today):
                    conn.rollback()
                    return None, "no_referral_today"
            cursor = conn.execute(
                "INSERT INTO lucky_wheel_spins (telegram_id, prize, prize_type, config_name, created_at) VALUES (?, ?, ?, NULL, ?)",
                (telegram_id, prize, prize_type, now_ts())
            )
            conn.commit()
            return int(cursor.lastrowid), "ok"
        except Exception:
            conn.rollback()
            raise

def void_wheel_spin(spin_id: int) -> bool:
    """وقتی جایزه تحویل داده نشد، چرخش را برمی‌گرداند تا کاربر دوباره بتواند بچرخاند."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM lucky_wheel_spins WHERE id = ?", (int(spin_id),))
        return cursor.rowcount > 0

# ============================================================
# گزارش و DAU
# ============================================================

def record_user_activity(telegram_id: int) -> None:
    telegram_id = validate_telegram_id(telegram_id)
    today = time.strftime("%Y-%m-%d")
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_activity (telegram_id, activity_date) VALUES (?, ?)",
            (telegram_id, today)
        )

def save_daily_report(report_date: str, configs_sold: int, revenue_toman: int) -> None:
    with get_connection() as conn:
        active = conn.execute("SELECT COUNT(DISTINCT telegram_id) AS c FROM user_activity WHERE activity_date = ?", (report_date,)).fetchone()["c"]
        conn.execute(
            "INSERT OR REPLACE INTO daily_reports (report_date, configs_sold, revenue_toman, active_users, created_at) VALUES (?, ?, ?, ?, ?)",
            (report_date, configs_sold, revenue_toman, active, now_ts())
        )

def _day_start() -> int:
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))

# ============================================================
# آمار
# ============================================================

def get_admin_stats() -> Dict[str, Any]:
    with get_connection() as conn:
        total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        banned = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1").fetchone()["c"]
        total_purchases = conn.execute("SELECT COALESCE(SUM(total_purchases), 0) AS s FROM users").fetchone()["s"]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users").fetchone()["s"]
        total_revenue = conn.execute("SELECT COALESCE(SUM(price_toman), 0) AS s FROM subscriptions").fetchone()["s"]
        total_stars = conn.execute("SELECT COALESCE(SUM(amount_stars), 0) AS s FROM stars_charges WHERE status != 'refunded'").fetchone()["s"]
        return {
            "total_users": int(total_users),
            "banned_users": int(banned),
            "total_purchases": int(total_purchases),
            "total_balance": int(total_balance),
            "total_revenue_toman": int(total_revenue),
            "total_stars": int(total_stars),
            "pending_payments": int(conn.execute(
                "SELECT COUNT(*) AS c FROM payment_requests WHERE status = 'pending'").fetchone()["c"]),
            "new_users_today": int(conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE joined_at >= ?", (_day_start(),)).fetchone()["c"]),
            "sales_today": int(conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE is_trial = 0 AND created_at >= ?",
                (_day_start(),)).fetchone()["c"]),
            "revenue_today_toman": int(conn.execute(
                "SELECT COALESCE(SUM(price_toman), 0) AS s FROM subscriptions WHERE is_trial = 0 AND created_at >= ?",
                (_day_start(),)).fetchone()["s"]),
            "active_subscriptions": int(conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE state = 'active'").fetchone()["c"]),
        }

# ============================================================
# گردونه شانس
# ============================================================

def get_last_wheel_spin(telegram_id: int) -> Optional[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM lucky_wheel_spins WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (telegram_id,)).fetchone()
        return dict(row) if row else None

def can_spin_wheel(telegram_id: int, cooldown_hours: int = 24) -> bool:
    last = get_last_wheel_spin(telegram_id)
    if not last:
        return True
    import datetime
    try:
        created = datetime.datetime.fromtimestamp(last["created_at"])
        elapsed = (datetime.datetime.now() - created).total_seconds()
        return elapsed >= cooldown_hours * 3600
    except Exception:
        return True

def record_wheel_spin(telegram_id: int, prize: str, prize_type: str, config_name: Optional[str] = None) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    prize = sanitize_text(prize, 100) or "?"
    prize_type = sanitize_text(prize_type, 20) or "nothing"
    config_name = sanitize_text(config_name, 100)
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO lucky_wheel_spins (telegram_id, prize, prize_type, config_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, prize, prize_type, config_name, now_ts())
        )
        return cursor.lastrowid
# ============================================================
# ===================  توابع جدید (پنل ادمین)  ===================
# ============================================================

# ------------------------- قیمت‌ها -------------------------

# PRICE-02: توابع تکراری PRICE_ITEMS/set_price/get_price_override حذف شدند؛
# کلیدهای قیمت فقط در pricing.py (PRICE_KEYS) و prices.py تعریف می‌شوند.

def get_price_history(limit: int = 20, item: Optional[str] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        if item:
            rows = conn.execute(
                "SELECT * FROM price_history WHERE item = ? ORDER BY id DESC LIMIT ?", (item, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM price_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------- تنظیمات (ابزار) -------------------------

def delete_setting(key: str) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM settings WHERE key = ?", (str(key),)).rowcount > 0


def get_int_setting(key: str, default: int = 0) -> int:
    try:
        return int(str(get_setting(key, default)).strip())
    except (TypeError, ValueError):
        return int(default)


# ------------------------- ادمین رسید -------------------------

def get_receipt_admin_id(default: int) -> int:
    """شناسهٔ ادمین تاییدکنندهٔ رسیدها؛ اگر تنظیم نشده یا نامعتبر باشد: default."""
    raw = get_setting("receipt_admin_id")
    try:
        value = int(str(raw).strip())
        return value if 0 < value <= 10_000_000_000_000 else int(default)
    except (TypeError, ValueError):
        return int(default)


def set_receipt_admin_id(new_admin_id: int, changed_by: Optional[int] = None) -> bool:
    new_admin_id = validate_telegram_id(new_admin_id)
    set_setting("receipt_admin_id", str(new_admin_id))
    log_admin_action(changed_by or 0, "receipt_admin_set", str(new_admin_id))
    return True


# ------------------------- ثبت اقدامات ادمین -------------------------

def log_admin_action(admin_id: int, action: str, target: Optional[str] = None, detail: Optional[str] = None) -> None:
    """ردپای حسابرسی (audit). هرگز استثنا نمی‌اندازد."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO admin_audit (admin_id, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (int(admin_id or 0), sanitize_text(action, 100) or "action",
                 sanitize_text(target, 200), sanitize_text(detail, 1000), now_ts())
            )
    except Exception:
        logger.exception("log_admin_action failed: %s", action)


def get_admin_actions(limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def purge_old_logs(days: int = 90) -> int:
    """حذف لاگ‌های قدیمی‌تر از N روز (برای کوچک ماندن دیتابیس)."""
    days = max(7, int(days))
    cutoff = now_ts() - days * 86400
    with get_connection() as conn:
        return conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,)).rowcount


# ------------------------- کاربران -------------------------

def search_users(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """جستجو با آیدی عددی (دقیق) یا بخشی از یوزرنیم/نام (LIKE امن)."""
    query = sanitize_text(query, 100)
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    raw = query.lstrip("@")
    results: List[Dict[str, Any]] = []
    if re.fullmatch(r"[0-9]{1,15}", raw):
        try:
            user = get_user(int(raw))
            if user:
                results.append(user)
        except ValueError:
            pass
    pattern = f"%{_like_escape(raw)}%"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE username LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR first_name LIKE ? ESCAPE '\\' COLLATE NOCASE ORDER BY total_purchases DESC LIMIT ?",
            (pattern, pattern, limit)
        ).fetchall()
    seen = {u["telegram_id"] for u in results}
    for row in rows:
        if row["telegram_id"] not in seen:
            results.append(dict(row))
    return results[:limit]


def count_user_subscriptions(telegram_id: int) -> int:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()["c"])


def get_recent_subscriptions(telegram_id: int, limit: int = 7) -> List[Dict[str, Any]]:
    """آخرین سرویس‌های کاربر (شامل حذف‌شده از لیست)."""
    telegram_id = validate_telegram_id(telegram_id)
    limit = max(1, min(int(limit), 50))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_ban_info(telegram_id: int) -> Dict[str, Any]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT reason, until, banned_by, created_at FROM bans "
            "WHERE telegram_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ).fetchone()
        return dict(row) if row else {}


def get_user_profile(telegram_id: int) -> Optional[Dict[str, Any]]:
    """اطلاعات کامل کاربر برای پنل ادمین: کاربر + سطح + تعداد سرویس + وضعیت بن."""
    user = get_user(telegram_id)
    if not user:
        return None
    level_key, level_name = get_user_level(int(user.get("total_purchases") or 0))
    user["level_key"] = level_key
    user["level_name"] = level_name
    user["sub_count"] = count_user_subscriptions(user["telegram_id"])
    user["banned"] = is_banned(user["telegram_id"])
    user["ban_info"] = get_ban_info(user["telegram_id"]) if user["banned"] else {}
    return user


def list_users_page(page: int = 1, page_size: int = 5) -> Tuple[List[Dict[str, Any]], int]:
    """صفحه‌بندی کاربران (بیشترین خرید اول) همراه با تعداد سرویس. خروجی: (کاربران، کل)."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 50))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.telegram_id, u.username, u.first_name, u.balance, u.total_purchases, u.is_banned,
                   (SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id) AS sub_count
            FROM users u
            ORDER BY u.total_purchases DESC, u.telegram_id ASC
            LIMIT ? OFFSET ?
            """,
            (page_size, (page - 1) * page_size)
        ).fetchall()
        total = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
        return [dict(r) for r in rows], total


def get_all_user_ids(include_banned: bool = False) -> List[int]:
    """برای پیام همگانی."""
    with get_connection() as conn:
        if include_banned:
            rows = conn.execute("SELECT telegram_id FROM users").fetchall()
        else:
            rows = conn.execute("SELECT telegram_id FROM users WHERE is_banned = 0").fetchall()
        return [int(r["telegram_id"]) for r in rows]


# ------------------------- رسیدها / پرداخت‌ها -------------------------

def get_pending_payments(limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_payment_stats() -> Dict[str, int]:
    out = {"pending": 0, "approved": 0, "rejected": 0, "approved_amount": 0}
    with get_connection() as conn:
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c, COALESCE(SUM(amount), 0) AS a FROM payment_requests GROUP BY status"
        ).fetchall():
            out[r["status"]] = int(r["c"])
            if r["status"] == "approved":
                out["approved_amount"] = int(r["a"])
    return out


def get_reviewer_stats(admin_id: int) -> Dict[str, int]:
    """تعداد رسیدهای تأیید/رد شده توسط یک ادمین."""
    admin_id = validate_telegram_id(admin_id)
    out = {"approved": 0, "rejected": 0}
    with get_connection() as conn:
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM payment_requests WHERE reviewed_by = ? GROUP BY status", (admin_id,)
        ).fetchall():
            if r["status"] in out:
                out[r["status"]] = int(r["c"])
    return out


# ------------------------- سرویس‌ها -------------------------

def get_subscription_by_config_name(config_name: str) -> Optional[Dict[str, Any]]:
    config_name = sanitize_text(config_name, 100)
    if not config_name:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE config_name = ? COLLATE NOCASE", (config_name,)
        ).fetchone()
        return dict(row) if row else None


def update_subscription_days(subscription_id: int, days: int) -> bool:
    """days=0 یعنی بدون محدودیت زمانی."""
    days = int(days)
    if days < 0 or days > 3650:
        return False
    with get_connection() as conn:
        return conn.execute(
            "UPDATE subscriptions SET days = ?, updated_at = ? WHERE id = ?",
            (days, now_ts(), subscription_id)
        ).rowcount > 0


# ------------------------- کد تخفیف -------------------------

def get_discount_codes(limit: int = 10) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM discount_codes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def set_discount_active(code_id: int, active: bool) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE discount_codes SET is_active = ? WHERE id = ?", (1 if active else 0, code_id)
        ).rowcount > 0


def delete_discount_code(code_id: int) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM discount_codes WHERE id = ?", (code_id,)).rowcount > 0


# ------------------------- محصولات -------------------------

def get_product(product_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None


def update_product_prices(product_id: int, price_toman: Optional[int] = None, price_stars: Optional[int] = None) -> bool:
    sets, params = [], []
    if price_toman is not None:
        if int(price_toman) < 0:
            return False
        sets.append("price_toman = ?")
        params.append(int(price_toman))
    if price_stars is not None:
        if int(price_stars) < 0:
            return False
        sets.append("price_stars = ?")
        params.append(int(price_stars))
    if not sets:
        return False
    params.append(product_id)
    with get_connection() as conn:
        return conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", params).rowcount > 0


def set_product_active(product_id: int, active: bool) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE products SET is_active = ? WHERE id = ?", (1 if active else 0, product_id)
        ).rowcount > 0


def delete_product(product_id: int) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount > 0


# ------------------------- آمار -------------------------

def get_top_services(limit: int = 3) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 20))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, volume_gb, COUNT(*) AS c FROM subscriptions WHERE is_trial = 0 "
            "GROUP BY kind, volume_gb ORDER BY c DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_logs(limit: int = 20, level: Optional[str] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        if level:
            rows = conn.execute(
                "SELECT * FROM logs WHERE level = ? ORDER BY id DESC LIMIT ?", (level, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------- بک‌آپ و سلامت -------------------------

def backup_database(dest_path) -> str:
    """
    بک‌آپ سازگار با WAL با API رسمی SQLite
    (کپی ساده فایل در حالت WAL ممکن است ناقص باشد).
    """
    dest_path = str(dest_path)
    src = get_connection()
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        os.chmod(dest_path, 0o600)
    except Exception:
        pass
    return dest_path


def check_integrity() -> bool:
    with get_connection() as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"

# ============================================================
# پیام خوش‌آمدگویی پین‌شده (یک‌بار برای هر کاربر)
# ============================================================

def has_user_pinned_welcome(telegram_id: int) -> bool:
    """بررسی می‌کند آیا پیام خوش‌آمدگویی این کاربر قبلاً ثبت شده است."""
    telegram_id = validate_telegram_id(telegram_id)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_pinned_welcome WHERE telegram_id = ? LIMIT 1",
            (telegram_id,),
        ).fetchone()

    return row is not None


def mark_user_pinned_welcome(telegram_id: int) -> bool:
    """
    ثبت اتمیک ارسال موفق پیام خوش‌آمدگویی.

    True: رکورد برای اولین بار ثبت شد.
    False: قبلاً ثبت شده بود و رکورد تکراری ساخته نشد.
    خطاهای دیتابیس عمداً به caller منتقل می‌شوند تا موفقیت جعلی ثبت نشود.
    """
    telegram_id = validate_telegram_id(telegram_id)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO user_pinned_welcome
                    (telegram_id, sent_at)
                VALUES (?, ?)
                """,
                (telegram_id, now_ts()),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise



# ============================================================
# PasarGuard — پنل‌های پویا
# ============================================================

def _validate_panel_id(panel_id: int) -> int:
    try:
        value = int(panel_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid panel_id")
    if value <= 0:
        raise ValueError("panel_id must be positive")
    return value


def _validate_panel_fields(name, url, username, password, group_id=None):
    name = sanitize_text(name, 50)
    url = sanitize_text(url, 500)
    username = sanitize_text(username, 128)
    password = str(password) if password is not None else ""
    if not name or not url or not username or not password or len(password) > 500:
        raise ValueError("Invalid panel fields")
    if not (url.startswith("https://") or url.startswith("http://")):
        raise ValueError("Panel URL must start with http:// or https://")
    if any(ord(ch) < 32 for ch in password):
        raise ValueError("Invalid panel password")
    if group_id is not None:
        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid panel group_id")
        if group_id <= 0:
            raise ValueError("Invalid panel group_id")
    return name, url.rstrip("/"), username, password, group_id


def add_panel(name: str, url: str, username: str, password: str,
              group_id: Optional[int] = None) -> int:
    """ثبت پنل PasarGuard پس از تست اتصال توسط لایه handler."""
    name, url, username, password, group_id = _validate_panel_fields(
        name, url, username, password, group_id
    )
    timestamp = now_ts()
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO panels
               (name, url, username, password, group_id, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (name, url, username, password, group_id, timestamp, timestamp),
        )
        return int(cursor.lastrowid)


def get_panel(panel_id: int) -> Optional[Dict[str, Any]]:
    """برای سازگاری با PanelRegistry، پنل را حتی در حالت غیرفعال برمی‌گرداند."""
    panel_id = _validate_panel_id(panel_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM panels WHERE id = ?", (panel_id,)).fetchone()
        return dict(row) if row else None


def get_panels(active_only: bool = True) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = """SELECT id, name, url, username, group_id, is_active,
                          created_at, updated_at FROM panels"""
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id ASC"
        return [dict(row) for row in conn.execute(query).fetchall()]


def update_panel(panel_id: int, name: Optional[str] = None,
                 url: Optional[str] = None, username: Optional[str] = None,
                 password: Optional[str] = None,
                 group_id: Optional[int] = None,
                 is_active: Optional[bool] = None) -> bool:
    panel_id = _validate_panel_id(panel_id)
    current = get_panel(panel_id)
    if not current:
        return False

    updates = {}
    if name is not None:
        value = sanitize_text(name, 50)
        if not value:
            raise ValueError("Invalid panel name")
        updates["name"] = value
    if url is not None:
        value = sanitize_text(url, 500)
        if not value or not (value.startswith("https://") or value.startswith("http://")):
            raise ValueError("Invalid panel URL")
        updates["url"] = value.rstrip("/")
    if username is not None:
        value = sanitize_text(username, 128)
        if not value:
            raise ValueError("Invalid panel username")
        updates["username"] = value
    if password is not None:
        value = str(password)
        if not value or len(value) > 500 or any(ord(ch) < 32 for ch in value):
            raise ValueError("Invalid panel password")
        updates["password"] = value
    if group_id is not None:
        try:
            value = int(group_id)
        except (TypeError, ValueError):
            raise ValueError("Invalid panel group_id")
        if value <= 0:
            raise ValueError("Invalid panel group_id")
        updates["group_id"] = value
    if is_active is not None:
        updates["is_active"] = 1 if bool(is_active) else 0

    if not updates:
        return False
    updates["updated_at"] = now_ts()
    columns = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [panel_id]
    with get_connection() as conn:
        return conn.execute(
            f"UPDATE panels SET {columns} WHERE id = ?", values
        ).rowcount > 0


def delete_panel(panel_id: int) -> bool:
    """پنلِ دارای محصول/اشتراک را غیرفعال می‌کند؛ حذف فیزیکی فقط برای پنل بدون ارجاع."""
    panel_id = _validate_panel_id(panel_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT id FROM panels WHERE id = ?", (panel_id,)).fetchone()
            if not row:
                conn.commit()
                return False
            referenced = conn.execute(
                """SELECT 1 FROM products WHERE panel_id = ?
                   UNION ALL SELECT 1 FROM subscriptions WHERE panel_id = ?
                   UNION ALL SELECT 1 FROM stars_invoices WHERE panel_id = ?
                   UNION ALL SELECT 1 FROM stars_charges WHERE panel_id = ?
                   LIMIT 1""",
                (panel_id, panel_id, panel_id, panel_id),
            ).fetchone()
            if referenced:
                conn.execute(
                    "UPDATE panels SET is_active = 0, updated_at = ? WHERE id = ?",
                    (now_ts(), panel_id),
                )
            else:
                conn.execute("DELETE FROM panels WHERE id = ?", (panel_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


# ============================================================
# امتیازها — دفترکل و انتقال اتمیک
# ============================================================

def get_points_balance(telegram_id: int) -> int:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT points FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return int(row["points"]) if row else 0


def _change_points_conn(conn, telegram_id: int, delta: int, tx_type: str,
                        description: str = "", reference_id: Optional[str] = None,
                        related_user_id: Optional[int] = None) -> int:
    telegram_id = validate_telegram_id(telegram_id)
    if not isinstance(delta, int) or delta == 0 or abs(delta) > 1_000_000_000:
        raise ValueError("Invalid points delta")
    tx_type = sanitize_text(tx_type, 50)
    if not tx_type:
        raise ValueError("Invalid points transaction type")
    reference_id = sanitize_text(reference_id, 200)
    description = sanitize_text(description, 500)
    if related_user_id is not None:
        related_user_id = validate_telegram_id(related_user_id)

    if reference_id and conn.execute(
        "SELECT 1 FROM points_transactions WHERE type = ? AND reference_id = ? AND telegram_id = ?",
        (tx_type, reference_id, telegram_id),
    ).fetchone():
        row = conn.execute("SELECT points FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return int(row["points"]) if row else 0

    user = conn.execute(
        "SELECT points FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if not user:
        raise ValueError("User not found")
    before = int(user["points"])
    after = before + delta
    if after < 0:
        raise ValueError("Insufficient points")

    conn.execute("UPDATE users SET points = ? WHERE telegram_id = ?", (after, telegram_id))
    conn.execute(
        """INSERT INTO points_transactions
           (telegram_id, amount, balance_before, balance_after, type, description,
            reference_id, related_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (telegram_id, delta, before, after, tx_type, description,
         reference_id, related_user_id, now_ts()),
    )
    return after


def change_points(telegram_id: int, delta: int, tx_type: str,
                  description: str = "", reference_id: Optional[str] = None) -> int:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _change_points_conn(
                conn, telegram_id, int(delta), tx_type, description, reference_id
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def add_points(telegram_id: int, amount: int, tx_type: str = "admin_add",
               description: str = "", reference_id: Optional[str] = None) -> int:
    amount = validate_amount(amount, 1_000_000_000)
    return change_points(telegram_id, amount, tx_type, description, reference_id)


def deduct_points(telegram_id: int, amount: int, tx_type: str = "admin_deduct",
                  description: str = "", reference_id: Optional[str] = None) -> int:
    amount = validate_amount(amount, 1_000_000_000)
    return change_points(telegram_id, -amount, tx_type, description, reference_id)


def transfer_points(sender_id: int, receiver_id: int, amount: int,
                    reference_id: Optional[str] = None) -> bool:
    sender_id = validate_telegram_id(sender_id)
    receiver_id = validate_telegram_id(receiver_id)
    amount = validate_amount(amount, 1_000_000_000)
    if sender_id == receiver_id:
        raise ValueError("Cannot transfer points to yourself")
    reference_id = sanitize_text(reference_id, 200)
    if reference_id is None:
        # شناسهٔ مستقل برای جلوگیری از برخورد دو انتقال هم‌زمان/تکراری
        reference_id = f"transfer:{sender_id}:{receiver_id}:{time.time_ns()}"

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute(
                "SELECT 1 FROM points_transactions WHERE type = 'transfer_out' AND reference_id = ? AND telegram_id = ?",
                (reference_id, sender_id),
            ).fetchone():
                conn.commit()
                return False
            _change_points_conn(
                conn, sender_id, -amount, "transfer_out",
                f"انتقال امتیاز به کاربر {receiver_id}", reference_id, receiver_id
            )
            _change_points_conn(
                conn, receiver_id, amount, "transfer_in",
                f"دریافت امتیاز از کاربر {sender_id}", reference_id, sender_id
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def get_points_transactions(telegram_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    limit = max(1, min(int(limit), 100))
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM points_transactions WHERE telegram_id = ?
               ORDER BY id DESC LIMIT ?""",
            (telegram_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


# ============================================================
# گروه‌ها و کلیدواژه‌های امتیازدهی
# ============================================================

def upsert_activity_group(chat_id: int, title: Optional[str] = None,
                          points_enabled: bool = True) -> int:
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid chat_id")
    if chat_id >= 0 or chat_id < -10**16:
        raise ValueError("Invalid Telegram group chat_id")
    title = sanitize_text(title, 200)
    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO activity_groups
               (chat_id, title, is_active, points_enabled, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   title = excluded.title,
                   points_enabled = excluded.points_enabled,
                   is_active = 1,
                   updated_at = excluded.updated_at""",
            (chat_id, title, 1 if points_enabled else 0, timestamp, timestamp),
        )
        row = conn.execute("SELECT id FROM activity_groups WHERE chat_id = ?", (chat_id,)).fetchone()
        return int(row["id"])


def list_activity_groups(active_only: bool = True) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM activity_groups"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id DESC"
        return [dict(row) for row in conn.execute(query).fetchall()]


def set_activity_group_active(group_id: int, active: bool) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE activity_groups SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, now_ts(), int(group_id)),
        ).rowcount > 0


def add_group_keyword(group_id: int, keyword: str, points: int,
                      cooldown_seconds: int = 0) -> int:
    group_id = int(group_id)
    keyword = sanitize_text(keyword, 150)
    points = validate_amount(points, 1_000_000)
    cooldown_seconds = max(0, min(int(cooldown_seconds), 31_536_000))
    if not keyword:
        raise ValueError("Keyword is required")
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO group_keyword_triggers
               (group_id, keyword, points, is_active, cooldown_seconds, created_at)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (group_id, keyword, points, cooldown_seconds, now_ts()),
        )
        return int(cursor.lastrowid)


def list_group_keywords(group_id: int, active_only: bool = False) -> List[Dict[str, Any]]:
    query = "SELECT * FROM group_keyword_triggers WHERE group_id = ?"
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY id"
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, (int(group_id),)).fetchall()]


def set_group_keyword_active(trigger_id: int, active: bool) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE group_keyword_triggers SET is_active = ? WHERE id = ?",
            (1 if active else 0, int(trigger_id)),
        ).rowcount > 0


def award_group_message_points(chat_id: int, telegram_id: int, message_id: int,
                               message_text: str) -> int:
    """یک پیام در هر گروه فقط یک بار امتیاز می‌گیرد؛ cooldown نیز اتمیک بررسی می‌شود."""
    chat_id = int(chat_id)
    telegram_id = validate_telegram_id(telegram_id)
    message_id = int(message_id)
    message_text = sanitize_text(message_text, 4000) or ""
    if message_id <= 0 or not message_text:
        return 0

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            group = conn.execute(
                """SELECT id FROM activity_groups
                   WHERE chat_id = ? AND is_active = 1 AND points_enabled = 1""",
                (chat_id,),
            ).fetchone()
            if not group:
                conn.commit()
                return 0
            group_id = int(group["id"])
            triggers = conn.execute(
                """SELECT * FROM group_keyword_triggers
                   WHERE group_id = ? AND is_active = 1
                   ORDER BY LENGTH(keyword) DESC, id ASC""",
                (group_id,),
            ).fetchall()

            matched = None
            lowered = message_text.casefold()
            for trigger in triggers:
                if str(trigger["keyword"]).casefold() in lowered:
                    matched = trigger
                    break
            if not matched:
                conn.commit()
                return 0

            try:
                conn.execute(
                    """INSERT INTO group_message_events
                       (group_id, telegram_id, message_id, trigger_id, points_awarded, created_at)
                       VALUES (?, ?, ?, ?, 0, ?)""",
                    (group_id, telegram_id, message_id, int(matched["id"]), now_ts()),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return 0

            cooldown = int(matched["cooldown_seconds"])
            if cooldown > 0:
                recent = conn.execute(
                    """SELECT 1 FROM group_message_events
                       WHERE group_id = ? AND telegram_id = ? AND trigger_id = ?
                         AND created_at > ? AND points_awarded > 0
                       LIMIT 1""",
                    (group_id, telegram_id, int(matched["id"]), now_ts() - cooldown),
                ).fetchone()
                if recent:
                    conn.commit()
                    return 0

            points = int(matched["points"])
            ref = f"group-message:{chat_id}:{message_id}"
            _change_points_conn(
                conn, telegram_id, points, "group_keyword",
                f"فعالیت در گروه: {matched['keyword']}", ref
            )
            conn.execute(
                """UPDATE group_message_events SET points_awarded = ?
                   WHERE group_id = ? AND message_id = ?""",
                (points, group_id, message_id),
            )
            conn.commit()
            return points
        except Exception:
            conn.rollback()
            raise


# ============================================================
# مأموریت‌ها — نوع و تنظیمات قابل توسعه
# ============================================================

def create_task(title: str, task_type: str, reward_points: int,
                description: str = "", config: Optional[Dict[str, Any]] = None,
                duration_seconds: Optional[int] = None,
                starts_at: Optional[int] = None, ends_at: Optional[int] = None,
                verification_method: str = "manual",
                max_completions: Optional[int] = None,
                created_by: Optional[int] = None) -> int:
    title = sanitize_text(title, 150)
    description = sanitize_text(description, 2000)
    task_type = sanitize_text(task_type, 64)
    reward_points = int(reward_points)
    if not title or not task_type or reward_points < 0 or reward_points > 1_000_000_000:
        raise ValueError("Invalid task data")
    if verification_method not in ("automatic", "manual", "hybrid"):
        raise ValueError("Invalid verification_method")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("Task config must be a dictionary")
    config_json = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    if len(config_json) > 20_000:
        raise ValueError("Task config too large")
    if duration_seconds is not None:
        duration_seconds = int(duration_seconds)
        if duration_seconds <= 0:
            raise ValueError("Invalid duration_seconds")
    if starts_at is not None:
        starts_at = int(starts_at)
    if ends_at is not None:
        ends_at = int(ends_at)
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ValueError("Task end must be after start")
    if max_completions is not None:
        max_completions = int(max_completions)
        if max_completions <= 0:
            raise ValueError("Invalid max_completions")
    if created_by is not None:
        created_by = validate_telegram_id(created_by)

    timestamp = now_ts()
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO tasks
               (title, description, task_type, config_json, reward_points,
                duration_seconds, starts_at, ends_at, verification_method,
                max_completions, is_active, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (title, description, task_type, config_json, reward_points,
             duration_seconds, starts_at, ends_at, verification_method,
             max_completions, created_by, timestamp, timestamp),
        )
        return int(cursor.lastrowid)


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["config"] = json.loads(result.pop("config_json") or "{}")
        except (TypeError, ValueError):
            result["config"] = {}
        return result


def list_tasks(active_only: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    query = "SELECT * FROM tasks"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY id DESC LIMIT ?"
    with get_connection() as conn:
        rows = conn.execute(query, (limit,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["config"] = json.loads(item.pop("config_json") or "{}")
            except (TypeError, ValueError):
                item["config"] = {}
            output.append(item)
        return output


def update_task(task_id: int, **fields) -> bool:
    allowed = {
        "title", "description", "task_type", "reward_points", "config",
        "duration_seconds", "starts_at", "ends_at", "verification_method",
        "max_completions", "is_active",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unsupported task fields: {', '.join(sorted(unknown))}")
    updates = {}
    for key, value in fields.items():
        if key in ("title", "task_type"):
            value = sanitize_text(value, 150 if key == "title" else 64)
            if not value:
                raise ValueError(f"Invalid {key}")
        elif key == "description":
            value = sanitize_text(value, 2000)
        elif key == "config":
            if not isinstance(value, dict):
                raise ValueError("Task config must be a dictionary")
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(value) > 20_000:
                raise ValueError("Task config too large")
            key = "config_json"
        elif key in ("reward_points", "duration_seconds", "starts_at", "ends_at", "max_completions"):
            if value is not None:
                value = int(value)
            if key == "reward_points" and (value is None or value < 0):
                raise ValueError("Invalid reward_points")
            if key in ("duration_seconds", "max_completions") and value is not None and value <= 0:
                raise ValueError(f"Invalid {key}")
        elif key == "verification_method":
            if value not in ("automatic", "manual", "hybrid"):
                raise ValueError("Invalid verification_method")
        elif key == "is_active":
            value = 1 if bool(value) else 0
        updates[key] = value
    if not updates:
        return False
    updates["updated_at"] = now_ts()
    sql = ", ".join(f"{key} = ?" for key in updates)
    with get_connection() as conn:
        return conn.execute(
            f"UPDATE tasks SET {sql} WHERE id = ?",
            list(updates.values()) + [int(task_id)],
        ).rowcount > 0


def delete_task(task_id: int) -> bool:
    """حذف نرم؛ سابقهٔ انجام مأموریت‌ها و پاداش‌ها حفظ می‌شود."""
    return update_task(task_id, is_active=False)


def assign_task(telegram_id: int, task_id: int) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    task_id = int(task_id)
    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND is_active = 1", (task_id,)
            ).fetchone()
            if not task:
                conn.commit()
                return None
            if task["starts_at"] is not None and int(task["starts_at"]) > timestamp:
                conn.commit()
                return None
            if task["ends_at"] is not None and int(task["ends_at"]) < timestamp:
                conn.commit()
                return None
            existing = conn.execute(
                "SELECT id FROM user_tasks WHERE task_id = ? AND telegram_id = ?",
                (task_id, telegram_id),
            ).fetchone()
            if existing:
                conn.commit()
                return int(existing["id"])
            if task["max_completions"] is not None:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM user_tasks WHERE task_id = ? AND status = 'approved'",
                    (task_id,),
                ).fetchone()["n"]
                if int(count) >= int(task["max_completions"]):
                    conn.commit()
                    return None
            deadline = timestamp + int(task["duration_seconds"]) if task["duration_seconds"] else None
            cursor = conn.execute(
                """INSERT INTO user_tasks
                   (task_id, telegram_id, status, assigned_at, deadline_at, reward_points)
                   VALUES (?, ?, 'assigned', ?, ?, ?)""",
                (task_id, telegram_id, timestamp, deadline, int(task["reward_points"])),
            )
            conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            conn.rollback()
            raise


def submit_task(telegram_id: int, task_id: int, evidence: Optional[Dict[str, Any]] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, dict):
        raise ValueError("Evidence must be a dictionary")
    evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    if len(evidence_json) > 20_000:
        raise ValueError("Evidence too large")
    timestamp = now_ts()
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE user_tasks
               SET status = 'submitted', evidence_json = ?, submitted_at = ?
               WHERE task_id = ? AND telegram_id = ?
                 AND status IN ('assigned', 'rejected')
                 AND (deadline_at IS NULL OR deadline_at >= ?)""",
            (evidence_json, timestamp, int(task_id), telegram_id, timestamp),
        )
        return cursor.rowcount > 0


def review_task(user_task_id: int, admin_id: int, approve: bool,
                note: str = "") -> bool:
    admin_id = validate_telegram_id(admin_id)
    note = sanitize_text(note, 1000)
    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT ut.*, t.reward_points AS task_reward
                   FROM user_tasks ut JOIN tasks t ON t.id = ut.task_id
                   WHERE ut.id = ?""",
                (int(user_task_id),),
            ).fetchone()
            if not row or row["status"] != "submitted":
                conn.commit()
                return False
            if row["deadline_at"] is not None and int(row["deadline_at"]) < timestamp:
                conn.execute(
                    """UPDATE user_tasks SET status = 'expired', reviewed_at = ?,
                       reviewed_by = ?, review_note = ? WHERE id = ?""",
                    (timestamp, admin_id, note or "مهلت مأموریت تمام شده است.", int(user_task_id)),
                )
                conn.commit()
                return False

            if approve:
                reward = int(row["reward_points"])
                if reward > 0 and row["rewarded_at"] is None:
                    _change_points_conn(
                        conn, int(row["telegram_id"]), reward, "task_reward",
                        f"پاداش مأموریت #{int(row['task_id'])}",
                        f"task:{int(user_task_id)}",
                    )
                conn.execute(
                    """UPDATE user_tasks SET status = 'approved', reviewed_at = ?,
                       reviewed_by = ?, review_note = ?, rewarded_at = ?, reward_points = ?
                       WHERE id = ?""",
                    (timestamp, admin_id, note, timestamp if reward > 0 else None,
                     reward, int(user_task_id)),
                )
            else:
                conn.execute(
                    """UPDATE user_tasks SET status = 'rejected', reviewed_at = ?,
                       reviewed_by = ?, review_note = ? WHERE id = ?""",
                    (timestamp, admin_id, note, int(user_task_id)),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def get_user_tasks(telegram_id: int, status: Optional[str] = None,
                   limit: int = 50) -> List[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    limit = max(1, min(int(limit), 200))
    query = "SELECT ut.*, t.title, t.description, t.task_type, t.config_json, t.verification_method FROM user_tasks ut JOIN tasks t ON t.id = ut.task_id WHERE ut.telegram_id = ?"
    params = [telegram_id]
    if status is not None:
        if status not in ("assigned", "submitted", "approved", "rejected", "expired"):
            raise ValueError("Invalid task status")
        query += " AND ut.status = ?"
        params.append(status)
    query += " ORDER BY ut.id DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["config"] = json.loads(item.pop("config_json") or "{}")
            except (TypeError, ValueError):
                item["config"] = {}
            output.append(item)
        return output


def expire_user_tasks() -> int:
    timestamp = now_ts()
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE user_tasks SET status = 'expired'
               WHERE status IN ('assigned', 'submitted')
                 AND deadline_at IS NOT NULL AND deadline_at < ?""",
            (timestamp,),
        )
        return int(cursor.rowcount)


# ============================================================
# پایان APIهای جدید
# ============================================================



def delete_group_keyword(trigger_id: int) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM group_keyword_triggers WHERE id = ?", (int(trigger_id),)
        ).rowcount > 0


def complete_task_automatically(
    telegram_id: int,
    task_id: int,
    verification_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    تکمیل خودکار پس از آن‌که handler شرط بیرونی را واقعاً بررسی کرد
    (مثلاً عضویت کانال با Telegram API). ثبت وضعیت و پرداخت پاداش اتمیک است.
    """
    telegram_id = validate_telegram_id(telegram_id)
    if verification_data is None:
        verification_data = {}
    if not isinstance(verification_data, dict):
        raise ValueError("verification_data must be a dictionary")
    evidence_json = json.dumps(
        verification_data, ensure_ascii=False, separators=(",", ":")
    )
    if len(evidence_json) > 20_000:
        raise ValueError("verification_data too large")

    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND is_active = 1",
                (int(task_id),),
            ).fetchone()
            if not task or task["verification_method"] not in ("automatic", "hybrid"):
                conn.commit()
                return False
            if task["starts_at"] is not None and int(task["starts_at"]) > timestamp:
                conn.commit()
                return False
            if task["ends_at"] is not None and int(task["ends_at"]) < timestamp:
                conn.commit()
                return False

            assignment = conn.execute(
                "SELECT * FROM user_tasks WHERE task_id = ? AND telegram_id = ?",
                (int(task_id), telegram_id),
            ).fetchone()
            if not assignment:
                deadline = timestamp + int(task["duration_seconds"]) if task["duration_seconds"] else None
                cursor = conn.execute(
                    """INSERT INTO user_tasks
                       (task_id, telegram_id, status, assigned_at, deadline_at, reward_points)
                       VALUES (?, ?, 'assigned', ?, ?, ?)""",
                    (int(task_id), telegram_id, timestamp, deadline, int(task["reward_points"])),
                )
                assignment_id = int(cursor.lastrowid)
                assignment = conn.execute(
                    "SELECT * FROM user_tasks WHERE id = ?", (assignment_id,)
                ).fetchone()
            if assignment["status"] == "approved":
                conn.commit()
                return False
            if assignment["status"] not in ("assigned", "submitted"):
                conn.commit()
                return False
            if assignment["deadline_at"] is not None and int(assignment["deadline_at"]) < timestamp:
                conn.execute(
                    "UPDATE user_tasks SET status = 'expired' WHERE id = ?",
                    (int(assignment["id"]),),
                )
                conn.commit()
                return False

            reward = int(assignment["reward_points"])
            if reward > 0:
                _change_points_conn(
                    conn, telegram_id, reward, "task_reward",
                    f"پاداش مأموریت #{int(task_id)}",
                    f"task:{int(assignment['id'])}",
                )
            conn.execute(
                """UPDATE user_tasks
                   SET status = 'approved', evidence_json = ?, submitted_at = ?,
                       reviewed_at = ?, review_note = 'تأیید خودکار',
                       rewarded_at = ?, reward_points = ?
                   WHERE id = ?""",
                (evidence_json, timestamp, timestamp,
                 timestamp if reward > 0 else None, reward, int(assignment["id"])),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def finalize_points_purchase(
    telegram_id: int,
    config_number: int,
    config_name: str,
    kind: str,
    volume_gb: float,
    days: int,
    devices: int,
    points_cost: int,
    panel_id: int,
    link: str,
) -> int:
    """کسر امتیاز و ثبت اشتراک در یک تراکنش؛ شکست هرکدام یعنی rollback کامل."""
    telegram_id = validate_telegram_id(telegram_id)
    points_cost = validate_amount(points_cost, 1_000_000_000)
    panel_id = _validate_panel_id(panel_id)
    config_name = sanitize_text(config_name, 100)
    link = sanitize_text(link, 4000)
    kind = sanitize_text(kind, 10)
    try:
        config_number = int(config_number)
        volume_gb = float(volume_gb)
        days = int(days)
        devices = int(devices)
    except (TypeError, ValueError):
        raise ValueError("Invalid points purchase data")
    if (
        not config_name or not link or kind not in ("vol", "unl")
        or config_number <= 0 or volume_gb < 0 or days < 0
        or not 0 <= devices <= 10
    ):
        raise ValueError("Invalid points purchase data")
    if kind == "vol":
        days = 0
    else:
        days = max(1, days or 30)

    timestamp = now_ts()
    reference = f"points-purchase:{config_name}"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            panel = conn.execute(
                "SELECT id FROM panels WHERE id = ? AND is_active = 1",
                (panel_id,),
            ).fetchone()
            if not panel:
                raise ValueError("Panel not found or inactive")
            existing = conn.execute(
                "SELECT id FROM subscriptions WHERE config_name = ?",
                (config_name,),
            ).fetchone()
            if existing:
                raise ValueError("Config name already exists")

            _change_points_conn(
                conn, telegram_id, -points_cost, "points_purchase",
                f"خرید کانفیگ {config_name}", reference
            )
            cursor = conn.execute(
                """INSERT INTO subscriptions
                   (telegram_id, config_number, config_name, kind, volume_gb, days,
                    devices, price_toman, price_stars, is_trial, panel_id, link,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 'active', ?, ?)""",
                (telegram_id, config_number, config_name, kind, volume_gb, days,
                 devices, panel_id, link, timestamp, timestamp),
            )
            conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            conn.rollback()
            raise



def update_product_panel(product_id: int, panel_id: Optional[int]) -> bool:
    """تغییر پنل محصول؛ مقدار None فقط برای سازگاری/حذف تخصیص مجاز است."""
    product_id = int(product_id)
    if panel_id is not None:
        panel_id = _validate_panel_id(panel_id)
        with get_connection() as conn:
            panel = conn.execute(
                "SELECT id FROM panels WHERE id = ? AND is_active = 1",
                (panel_id,),
            ).fetchone()
            if not panel:
                raise ValueError("Panel not found or inactive")
            return conn.execute(
                "UPDATE products SET panel_id = ? WHERE id = ?",
                (panel_id, product_id),
            ).rowcount > 0
    with get_connection() as conn:
        return conn.execute(
            "UPDATE products SET panel_id = NULL WHERE id = ?", (product_id,)
        ).rowcount > 0


def get_pending_task_submissions(limit: int = 100) -> List[Dict[str, Any]]:
    """فهرست درخواست‌های مأموریت منتظر بررسی برای پنل ادمین."""
    limit = max(1, min(int(limit), 500))
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT ut.*, t.title, t.task_type, t.reward_points AS configured_reward,
                      u.username, u.first_name
               FROM user_tasks ut
               JOIN tasks t ON t.id = ut.task_id
               JOIN users u ON u.telegram_id = ut.telegram_id
               WHERE ut.status = 'submitted'
               ORDER BY ut.submitted_at ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]



# ============================================================
# تمدید سرویس‌ها — تمدید همان محصول با قیمت روز
# ============================================================

_GB_BYTES = 1024 ** 3
_AUTO_RENEW_VOLUME_THRESHOLD_GB = 0.2
_AUTO_RENEW_EXPIRY_WINDOW_SECONDS = 3 * 24 * 60 * 60


def get_auto_renew_candidates(now: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """
    سرویس‌های آماده تمدید خودکار:
    - حجمی: حجم باقی‌مانده <= 0.2GB (بدون بررسی تاریخ انقضا)
    - نامحدود: حداکثر سه روز تا انقضا (یا منقضی‌شده)
    محصول اصلی هر سرویس با قیمت فعلی از جدول products خوانده می‌شود.
    """
    timestamp = int(now if now is not None else now_ts())
    limit = max(1, min(int(limit), 500))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   p.price_toman AS current_price_toman,
                   p.price_stars AS current_price_stars,
                   p.volume_gb AS product_volume_gb,
                   p.days AS product_days,
                   p.kind AS product_kind,
                   p.panel_id AS product_panel_id
            FROM subscriptions s
            JOIN products p ON p.id = s.product_id AND p.is_active = 1
            WHERE s.auto_renew = 1
              AND s.state = 'active'
              AND s.is_trial = 0
              AND (
                    s.auto_renew_last_error IS NULL
                 OR s.auto_renew_last_attempt_at IS NULL
                 OR s.auto_renew_last_attempt_at <= ?
              )
              AND (
                    (s.kind = 'vol'
                     AND ((s.volume_gb * ?) - s.used_bytes) < ?)
                 OR (s.kind = 'unl'
                     AND s.expires_at IS NOT NULL
                     AND s.expires_at <= ?)
              )
              AND NOT EXISTS (
                    SELECT 1 FROM renewal_orders ro
                    WHERE ro.subscription_id = s.id
                      AND ro.status IN ('pending', 'processing')
              )
            ORDER BY s.id ASC
            LIMIT ?
            """,
            (
                timestamp - (6 * 60 * 60),
                _GB_BYTES,
                _AUTO_RENEW_VOLUME_THRESHOLD_GB * _GB_BYTES,
                timestamp + _AUTO_RENEW_EXPIRY_WINDOW_SECONDS,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def get_subscription_product(subscription_id: int) -> Optional[Dict[str, Any]]:
    """محصول اصلی متصل به سرویس، همراه قیمت فعلی."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.*
            FROM subscriptions s
            JOIN products p ON p.id = s.product_id
            WHERE s.id = ?
            """,
            (int(subscription_id),),
        ).fetchone()
        return dict(row) if row else None


def create_renewal_order(
    subscription_id: int,
    source: str = "manual",
    trigger_reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    سفارش تمدید و کسر وجه کیف پول را اتمیک انجام می‌دهد.
    قیمت از products در لحظهٔ تمدید خوانده می‌شود.
    اگر موجودی کافی نباشد، وجهی کسر نمی‌شود و status='insufficient_funds' برمی‌گردد.
    """
    subscription_id = int(subscription_id)
    source = str(source).strip().lower()
    if source not in ("manual", "auto"):
        raise ValueError("invalid_renewal_source")
    trigger_reason = sanitize_text(trigger_reason, 100)
    idempotency_key = sanitize_text(idempotency_key, 200) if idempotency_key else None
    timestamp = now_ts()

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                old = conn.execute(
                    "SELECT * FROM renewal_orders WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if old:
                    conn.commit()
                    return dict(old)

            existing_active = conn.execute(
                """SELECT * FROM renewal_orders
                   WHERE subscription_id = ? AND status IN ('pending', 'processing')
                   ORDER BY id DESC LIMIT 1""",
                (subscription_id,),
            ).fetchone()
            if existing_active:
                conn.commit()
                return dict(existing_active)

            sub = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
            if not sub:
                raise ValueError("subscription_not_found")
            if sub["state"] not in ("active", "expired") or int(sub["is_trial"] or 0):
                raise ValueError("subscription_not_renewable")
            if source == "auto" and sub["state"] != "active":
                raise ValueError("auto_renew_subscription_not_active")
            if int(sub["telegram_id"]) <= 0:
                raise ValueError("invalid_subscription_owner")
            if not sub["product_id"]:
                raise ValueError("subscription_product_not_linked")

            product = conn.execute(
                """SELECT * FROM products
                   WHERE id = ? AND is_active = 1""",
                (int(sub["product_id"]),),
            ).fetchone()
            if not product:
                raise ValueError("renewal_product_unavailable")
            if product["kind"] != sub["kind"]:
                raise ValueError("renewal_product_kind_mismatch")
            if sub["panel_id"] is not None and product["panel_id"] != sub["panel_id"]:
                raise ValueError("renewal_product_panel_mismatch")

            if source == "auto":
                if not int(sub["auto_renew"] or 0):
                    raise ValueError("auto_renew_disabled")
                if sub["kind"] == "vol":
                    remaining_bytes = (float(sub["volume_gb"]) * _GB_BYTES) - int(sub["used_bytes"] or 0)
                    if remaining_bytes >= (_AUTO_RENEW_VOLUME_THRESHOLD_GB * _GB_BYTES):
                        raise ValueError("auto_renew_not_due")
                elif sub["kind"] == "unl":
                    if not sub["expires_at"] or int(sub["expires_at"]) > timestamp + _AUTO_RENEW_EXPIRY_WINDOW_SECONDS:
                        raise ValueError("auto_renew_not_due")

            price = int(product["price_toman"])
            if price <= 0:
                raise ValueError("invalid_renewal_price")

            user = conn.execute(
                "SELECT balance FROM users WHERE telegram_id = ?",
                (int(sub["telegram_id"]),),
            ).fetchone()
            if not user:
                raise ValueError("renewal_user_not_found")
            balance_before = int(user["balance"])
            if balance_before < price:
                conn.execute(
                    """UPDATE subscriptions
                       SET auto_renew_last_attempt_at = ?,
                           auto_renew_last_error = 'insufficient_funds',
                           updated_at = ?
                       WHERE id = ?""",
                    (timestamp, timestamp, subscription_id),
                )
                conn.commit()
                return {
                    "id": None,
                    "subscription_id": subscription_id,
                    "telegram_id": int(sub["telegram_id"]),
                    "status": "insufficient_funds",
                    "price_toman": price,
                    "balance": balance_before,
                }

            # سفارش ابتدا ایجاد می‌شود تا شناسه یکتای آن مرجع تراکنش کیف پول باشد.
            cursor = conn.execute(
                """
                INSERT INTO renewal_orders (
                    idempotency_key, subscription_id, telegram_id, product_id,
                    panel_id, source, trigger_reason, kind, volume_gb, days,
                    price_toman, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    idempotency_key, subscription_id, int(sub["telegram_id"]),
                    int(product["id"]), product["panel_id"], source, trigger_reason,
                    product["kind"], float(product["volume_gb"]), int(product["days"]),
                    price, timestamp, timestamp,
                ),
            )
            order_id = int(cursor.lastrowid)
            balance_after = balance_before - price
            conn.execute(
                "UPDATE users SET balance = ? WHERE telegram_id = ?",
                (balance_after, int(sub["telegram_id"])),
            )
            wallet_cursor = conn.execute(
                """
                INSERT INTO wallet_transactions (
                    telegram_id, amount, balance_before, balance_after,
                    type, description, reference_id, created_at
                )
                VALUES (?, ?, ?, ?, 'renewal', ?, ?, ?)
                """,
                (
                    int(sub["telegram_id"]), -price, balance_before, balance_after,
                    f"تمدید سرویس {sub['config_name']}",
                    f"renewal:{order_id}", timestamp,
                ),
            )
            conn.execute(
                """
                UPDATE renewal_orders
                SET wallet_transaction_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(wallet_cursor.lastrowid), timestamp, order_id),
            )
            conn.execute(
                """
                UPDATE subscriptions
                SET auto_renew_last_attempt_at = ?,
                    auto_renew_last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, subscription_id),
            )
            row = conn.execute(
                "SELECT * FROM renewal_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise


def mark_renewal_processing(order_id: int) -> bool:
    """قفل منطقی سفارش پیش از فراخوانی API پنل."""
    timestamp = now_ts()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE renewal_orders
            SET status = 'processing', attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'processing')
            """,
            (timestamp, int(order_id)),
        )
        return cursor.rowcount > 0


def complete_renewal_order(
    order_id: int,
    external_reference: Optional[str] = None,
) -> bool:
    """
    پس از موفقیت API پنل فراخوانی شود.
    حجمی: سقف حجم خریداری‌شده به سقف قبلی اضافه می‌شود.
    نامحدود: اعتبار به‌صورت ۳۰ روز از max(انقضای فعلی، اکنون) تمدید می‌شود.
    """
    order_id = int(order_id)
    external_reference = sanitize_text(external_reference, 200)
    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = conn.execute(
                "SELECT * FROM renewal_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not order:
                raise ValueError("renewal_order_not_found")
            if order["status"] == "delivered":
                conn.commit()
                return True
            if order["status"] not in ("pending", "processing"):
                raise ValueError("renewal_order_not_payable")

            sub = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (int(order["subscription_id"]),),
            ).fetchone()
            if not sub:
                raise ValueError("renewal_subscription_not_found")

            if order["kind"] == "vol":
                new_volume = float(sub["volume_gb"]) + float(order["volume_gb"])
                conn.execute(
                    """UPDATE subscriptions
                       SET volume_gb = ?, price_toman = ?, product_id = ?,
                           auto_renew_last_success_at = ?,
                           auto_renew_last_error = NULL, updated_at = ?
                       WHERE id = ?""",
                    (
                        new_volume, int(order["price_toman"]), int(order["product_id"]),
                        timestamp, timestamp, int(order["subscription_id"]),
                    ),
                )
            else:
                current_expiry = int(sub["expires_at"] or 0)
                base_expiry = max(current_expiry, timestamp)
                extension_days = max(1, int(order["days"] or 30))
                new_expiry = base_expiry + extension_days * 24 * 60 * 60
                conn.execute(
                    """UPDATE subscriptions
                       SET expires_at = ?, days = ?, price_toman = ?, product_id = ?,
                           auto_renew_last_success_at = ?,
                           auto_renew_last_error = NULL, updated_at = ?
                       WHERE id = ?""",
                    (
                        new_expiry, extension_days, int(order["price_toman"]),
                        int(order["product_id"]), timestamp, timestamp,
                        int(order["subscription_id"]),
                    ),
                )

            conn.execute(
                """
                UPDATE renewal_orders
                SET status = 'delivered', external_reference = ?,
                    delivered_at = ?, updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (external_reference, timestamp, timestamp, order_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def fail_renewal_order(
    order_id: int,
    error: Optional[str] = None,
    refund: bool = True,
) -> bool:
    """ثبت شکست تمدید؛ در صورت نیاز بازگشت وجه به‌صورت اتمیک و idempotent."""
    order_id = int(order_id)
    error = sanitize_text(error, 1000) or "renewal_failed"
    timestamp = now_ts()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = conn.execute(
                "SELECT * FROM renewal_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not order:
                return False
            if order["status"] in ("delivered", "refunded"):
                conn.rollback()
                return False
            if order["status"] == "failed":
                conn.commit()
                return True

            final_status = "failed"
            if refund:
                user = conn.execute(
                    "SELECT balance FROM users WHERE telegram_id = ?",
                    (int(order["telegram_id"]),),
                ).fetchone()
                if user:
                    before = int(user["balance"])
                    amount = int(order["price_toman"])
                    after = before + amount
                    conn.execute(
                        "UPDATE users SET balance = ? WHERE telegram_id = ?",
                        (after, int(order["telegram_id"])),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO wallet_transactions (
                            telegram_id, amount, balance_before, balance_after,
                            type, description, reference_id, created_at
                        )
                        VALUES (?, ?, ?, ?, 'refund', ?, ?, ?)
                        """,
                        (
                            int(order["telegram_id"]), amount, before, after,
                            f"بازگشت وجه تمدید سرویس #{order_id}",
                            f"refund:renewal:{order_id}", timestamp,
                        ),
                    )
                    final_status = "refunded"

            conn.execute(
                """
                UPDATE renewal_orders
                SET status = ?, last_error = ?, refunded_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    final_status, error,
                    timestamp if final_status == "refunded" else None,
                    timestamp, order_id,
                ),
            )
            conn.execute(
                """
                UPDATE subscriptions
                SET auto_renew_last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error, timestamp, int(order["subscription_id"])),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def get_renewal_order(order_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM renewal_orders WHERE id = ?",
            (int(order_id),),
        ).fetchone()
        return dict(row) if row else None


def get_pending_renewal_orders(limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM renewal_orders
               WHERE status IN ('pending', 'processing')
               ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_renewal_history(subscription_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM renewal_orders
               WHERE subscription_id = ?
               ORDER BY id DESC LIMIT ?""",
            (int(subscription_id), limit),
        ).fetchall()
        return [dict(row) for row in rows]
