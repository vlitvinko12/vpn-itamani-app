import asyncio
import io
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, LabeledPrice,
                           Message, PreCheckoutQuery, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN    = "8992350696:AAGD7yKxDs34M2E0oufSp9KMAHdvsugv-XE"
MINI_APP_URL = "https://vlitvinko12.github.io/vpn-itamani-app/"
SUPPORT      = "https://t.me/vpnitamani"
DB_PATH      = "vpn_bot.db"
ADMIN_IDS    = {7909264638}
CHANNEL_ID   = "@vpnitamani_channel"  # ← замените на username вашего канала
CHANNEL_URL  = "https://t.me/vpnitamani_channel"  # ← ссылка на канал
FLOOD_LIMIT  = 1.0
MAX_WARNINGS = 5

PLANS = {
    "1_month":  ("VPN — 1 месяц",   150,  30),
    "3_months": ("VPN — 3 месяца",  400,  90),
    "6_months": ("VPN — 6 месяцев", 700, 180),
    "1_year":   ("VPN — 1 год",    1100, 365),
}

PLAN_DAYS = {"1_month": 30, "3_months": 90, "6_months": 180, "1_year": 365}

# Крипто-кошельки для оплаты (заполните ваши адреса)
TON_WALLET  = "UQD..."   # ← вставьте TON-адрес
USDT_WALLET = "T..."     # ← вставьте USDT TRC-20 адрес

# Маппинг планов из Mini App → Stars invoice payload
WEBAPP_PLAN_MAP = {
    "sub_month": "1_month",
    "month":     "1_month",
    "3month":    "3_months",
    "6month":    "6_months",
    "year":      "1_year",
    "day":       None,   # нет отдельного плана Stars
    "week":      None,
}
WEBAPP_STARS = {
    "sub_month": 150, "month": 150, "3month": 400,
    "6month": 700, "year": 1100, "day": 50, "week": 100,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── MIDDLEWARES ───────────────────────────────────────────────────────────────
class AntiFloodMiddleware(BaseMiddleware):
    def __init__(self):
        self.last_msg = defaultdict(float)
        self.warnings = defaultdict(int)
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id not in ADMIN_IDS:
            now = time.monotonic()
            if now - self.last_msg[user.id] < FLOOD_LIMIT:
                self.warnings[user.id] += 1
                if self.warnings[user.id] >= MAX_WARNINGS:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE users SET is_banned=1 WHERE tg_id=?", (user.id,))
                        await db.commit()
                    try: await bot.send_message(user.id, "⛔ Заблокированы за спам. Поддержка: " + SUPPORT)
                    except: pass
                return
            self.last_msg[user.id] = now
            self.warnings[user.id] = 0
        return await handler(event, data)


class BanCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id not in ADMIN_IDS:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT is_banned FROM users WHERE tg_id=?", (user.id,)) as c:
                    row = await c.fetchone()
            if row and row[0]: return
        return await handler(event, data)


# ── DATABASE ──────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                join_date TEXT DEFAULT (datetime('now','localtime')),
                referred_by INTEGER, is_banned INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                plan TEXT, amount INTEGER, currency TEXT DEFAULT 'RUB',
                expires_at TEXT,
                date TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER, referred_id INTEGER,
                date TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS promo_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                code TEXT, date TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, discount INTEGER DEFAULT 10,
                max_uses INTEGER DEFAULT 0, uses_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS ref_balance (
                user_id INTEGER PRIMARY KEY,
                stars_earned INTEGER DEFAULT 0,
                stars_paid   INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS vpn_configs (
                user_id INTEGER PRIMARY KEY,
                config  TEXT,
                label   TEXT,
                issued_at TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                subject TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now','localtime')));
            CREATE TABLE IF NOT EXISTS ticket_msgs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                user_id INTEGER,
                text TEXT,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')));
        """)
        for col in ["expires_at"]:
            try: await db.execute(f"ALTER TABLE purchases ADD COLUMN {col} TEXT")
            except: pass
        await db.commit()
    logger.info("DB ready")


async def is_subscribed(user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на канал."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        return True  # если бот не добавлен в канал — пропускаем проверку


def sub_required_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📢 Подписаться на канал", url=CHANNEL_URL)
    b.button(text="✅ Я подписался", callback_data="check_sub")
    b.adjust(1); return b.as_markup()


async def register_user(tg_user, referred_by=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tg_id FROM users WHERE tg_id=?", (tg_user.id,)) as c:
            exists = await c.fetchone()
        if exists:
            await db.execute("UPDATE users SET username=?, first_name=? WHERE tg_id=?",
                             (tg_user.username, tg_user.first_name, tg_user.id))
            await db.commit(); return False
        await db.execute("INSERT INTO users(tg_id,username,first_name,referred_by) VALUES(?,?,?,?)",
                         (tg_user.id, tg_user.username, tg_user.first_name, referred_by))
        if referred_by and referred_by != tg_user.id:
            await db.execute("INSERT INTO referrals(referrer_id,referred_id) VALUES(?,?)", (referred_by, tg_user.id))
        await db.commit()
    return True


async def record_purchase(user_id, plan, amount, currency="RUB", days=0):
    expires_at = None
    if days > 0:
        expires_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO purchases(user_id,plan,amount,currency,expires_at) VALUES(?,?,?,?,?)",
                         (user_id, plan, amount, currency, expires_at))
        await db.commit()
    return expires_at


async def add_ref_bonus(referrer_id, stars):
    bonus = round(stars * 0.15)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO ref_balance(user_id, stars_earned) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET stars_earned=stars_earned+?
        """, (referrer_id, bonus, bonus))
        await db.commit()
    return bonus


async def validate_promo(user_id, code):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT discount,max_uses,uses_count,is_active FROM promo_codes WHERE code=?", (code,)) as c:
            row = await c.fetchone()
        if not row: return 0, "❌ Промокод не найден."
        discount, max_uses, uses_count, is_active = row
        if not is_active: return 0, "❌ Промокод деактивирован."
        if max_uses > 0 and uses_count >= max_uses: return 0, "❌ Промокод исчерпан."
        async with db.execute("SELECT id FROM promo_uses WHERE user_id=? AND code=?", (user_id, code)) as c:
            if await c.fetchone(): return 0, "ℹ️ Вы уже использовали этот промокод."
    return discount, None


async def apply_promo(user_id, code):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO promo_uses(user_id,code) VALUES(?,?)", (user_id, code))
        await db.execute("UPDATE promo_codes SET uses_count=uses_count+1 WHERE code=?", (code,))
        await db.commit()


async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async def one(q, *a):
            async with db.execute(q, a) as c: return (await c.fetchone())[0]
        total_users = await one("SELECT COUNT(*) FROM users")
        new_today   = await one("SELECT COUNT(*) FROM users WHERE date(join_date)=date('now','localtime')")
        total_purch = await one("SELECT COUNT(*) FROM purchases")
        stars_rev   = await one("SELECT COALESCE(SUM(amount),0) FROM purchases WHERE currency='XTR'")
        rub_rev     = await one("SELECT COALESCE(SUM(amount),0) FROM purchases WHERE currency='RUB'")
        purch_today = await one("SELECT COUNT(*) FROM purchases WHERE date(date)=date('now','localtime')")
        total_refs  = await one("SELECT COUNT(*) FROM referrals")
        banned      = await one("SELECT COUNT(*) FROM users WHERE is_banned=1")
        active_subs = await one("SELECT COUNT(DISTINCT user_id) FROM purchases WHERE expires_at > datetime('now','localtime')")
        configs_issued = await one("SELECT COUNT(*) FROM vpn_configs")
        async with db.execute("SELECT plan,COUNT(*),currency FROM purchases GROUP BY plan,currency ORDER BY COUNT(*) DESC") as c:
            plans = await c.fetchall()
        async with db.execute("""SELECT u.first_name,u.username,p.plan,p.amount,p.currency,p.date
            FROM purchases p JOIN users u ON p.user_id=u.tg_id ORDER BY p.date DESC LIMIT 5""") as c:
            recent = await c.fetchall()
        async with db.execute("SELECT code,discount,max_uses,uses_count,is_active FROM promo_codes ORDER BY created_at DESC") as c:
            promos = await c.fetchall()
    return dict(total_users=total_users, new_today=new_today, total_purch=total_purch,
                stars_rev=stars_rev, rub_rev=rub_rev, purch_today=purch_today,
                total_refs=total_refs, banned=banned, active_subs=active_subs,
                configs_issued=configs_issued, plans=plans, recent=recent, promos=promos)


def fmt_stats(s):
    plans_t  = "\n".join(f"  • <b>{p[0]}</b>: {p[1]} шт. ({p[2]})" for p in s["plans"]) or "  — нет"
    recent_t = "\n".join(f"  {i+1}. {r[0] or r[1] or '?'} — {r[2]}, {r[3]} {r[4]} [{r[5][:10]}]"
                          for i,r in enumerate(s["recent"])) or "  — нет"
    promos_t = "\n".join(f"  {'✅' if p[4] else '❌'} <code>{p[0]}</code> -{p[1]}% | {p[3]}/{p[2] or '∞'}"
                          for p in s["promos"]) or "  — нет"
    return (
        f"📊 <b>Панель администратора</b>\n<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>\n\n"
        f"👥 Всего: <b>{s['total_users']}</b> | +{s['new_today']} сегодня | 🟢 Активных: <b>{s['active_subs']}</b>\n"
        f"   Рефов: {s['total_refs']} | Бан: {s['banned']} | 🔑 Конфигов: {s['configs_issued']}\n\n"
        f"💰 Stars: <b>{s['stars_rev']} ⭐</b> | Руб: <b>{s['rub_rev']} ₽</b>\n"
        f"   Покупок: {s['total_purch']} (сегодня: {s['purch_today']})\n\n"
        f"📦 <b>По тарифам:</b>\n{plans_t}\n\n"
        f"🎟 <b>Промокоды:</b>\n{promos_t}\n\n"
        f"🕐 <b>Последние покупки:</b>\n{recent_t}"
    )


async def gen_stats_chart() -> io.BytesIO:
    """Генерирует PNG-график статистики за 14 дней."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT date(date,'localtime') as day, COUNT(*) as cnt
            FROM purchases WHERE date >= date('now','-13 days','localtime')
            GROUP BY day ORDER BY day
        """) as c:
            purch_rows = await c.fetchall()
        async with db.execute("""
            SELECT date(join_date,'localtime') as day, COUNT(*) as cnt
            FROM users WHERE join_date >= datetime('now','-13 days','localtime')
            GROUP BY day ORDER BY day
        """) as c:
            user_rows = await c.fetchall()
        total_stars = (await (await db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM purchases WHERE currency='XTR'")).fetchone())[0]
        total_rub = (await (await db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM purchases WHERE currency='RUB'")).fetchone())[0]
    days_list = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(13, -1, -1)]
    purch_dict = {r[0]: r[1] for r in purch_rows}
    user_dict  = {r[0]: r[1] for r in user_rows}
    purch_vals = [purch_dict.get(d, 0) for d in days_list]
    user_vals  = [user_dict.get(d, 0)  for d in days_list]
    labels     = [d[5:] for d in days_list]
    plt.style.use('dark_background')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7))
    fig.patch.set_facecolor('#0f0f0f')
    for ax in (ax1, ax2):
        ax.set_facecolor('#1a1a1a')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['bottom'].set_color('#333')
        ax.spines['left'].set_color('#333')
        ax.tick_params(colors='#8e8e93', labelsize=8)
    # Purchases bar
    bars = ax1.bar(labels, purch_vals, color='#6366f1', alpha=0.9, width=0.6)
    ax1.set_title('Покупки за 14 дней', color='white', fontsize=13, fontweight='bold', pad=8)
    ax1.set_ylabel('Покупок', color='#8e8e93', fontsize=9)
    ax1.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax1.set_xticks(range(len(labels)))
    ax1.set_xticklabels(labels, rotation=45, ha='right', fontsize=7)
    for bar, val in zip(bars, purch_vals):
        if val > 0:
            ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
                     str(val), ha='center', va='bottom', color='white', fontsize=8, fontweight='bold')
    # Users line
    x = range(len(labels))
    ax2.fill_between(x, user_vals, alpha=0.15, color='#34d399')
    ax2.plot(x, user_vals, color='#34d399', linewidth=2, marker='o', markersize=4)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, rotation=45, ha='right', fontsize=7)
    ax2.set_title('Новые пользователи за 14 дней', color='white', fontsize=13, fontweight='bold', pad=8)
    ax2.set_ylabel('Новых', color='#8e8e93', fontsize=9)
    ax2.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    total_purch = sum(purch_vals)
    total_users = sum(user_vals)
    fig.text(0.5, 0.01,
             f'⭐ {total_stars} Stars   ₽ {total_rub} RUB   Покупок: {total_purch}   Новых юзеров: {total_users}',
             ha='center', color='#8e8e93', fontsize=9)
    plt.tight_layout(rect=[0, 0.04, 1, 1])
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=120, bbox_inches='tight',
                facecolor='#0f0f0f', edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    return buf


# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⭐ Открыть", web_app=WebAppInfo(url=MINI_APP_URL))
    b.button(text="🛟 Поддержка", url=SUPPORT)
    b.adjust(1); return b.as_markup()


def plans_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⭐ 150  — 1 месяц",   callback_data="buy_1_month")
    b.button(text="⭐ 400  — 3 месяца",  callback_data="buy_3_months")
    b.button(text="⭐ 700  — 6 месяцев", callback_data="buy_6_months")
    b.button(text="⭐ 1100 — 1 год",     callback_data="buy_1_year")
    b.button(text="◀️ Назад",            callback_data="back_main")
    b.adjust(1); return b.as_markup()


def renew_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Продлить подписку", callback_data="show_plans")
    b.button(text="🔑 Мой VPN конфиг",   callback_data="my_vpn")
    b.adjust(1); return b.as_markup()


def admin_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить",     callback_data="admin_stats")
    b.button(text="📈 Графики",      callback_data="admin_graph")
    b.button(text="👥 Пользователи", callback_data="admin_users")
    b.button(text="🎟 Промокоды",    callback_data="admin_promos")
    b.button(text="🎫 Тикеты",       callback_data="admin_tickets")
    b.button(text="🚀 Mini App",     web_app=WebAppInfo(url=MINI_APP_URL))
    b.adjust(2, 2, 1, 1); return b.as_markup()


# ── BACKGROUND: НАПОМИНАНИЯ ───────────────────────────────────────────────────
async def reminder_task():
    await asyncio.sleep(60)
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT DISTINCT user_id FROM purchases
                    WHERE expires_at IS NOT NULL
                    AND datetime(expires_at) BETWEEN datetime('now','+2 days 23 hours','localtime')
                                                 AND datetime('now','+3 days 1 hour','localtime')
                """) as c:
                    rows = await c.fetchall()
            for (uid,) in rows:
                try:
                    await bot.send_message(uid,
                        "⚠️ <b>Ваша подписка истекает через 3 дня!</b>\n\n"
                        "Продлите сейчас, чтобы не потерять доступ к VPN.",
                        parse_mode="HTML", reply_markup=renew_kb())
                except: pass
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT DISTINCT user_id FROM purchases
                    WHERE expires_at IS NOT NULL
                    AND date(expires_at) = date('now','localtime')
                """) as c:
                    expired = await c.fetchall()
            for (uid,) in expired:
                try:
                    await bot.send_message(uid,
                        "🔴 <b>Ваша подписка истекает сегодня!</b>\n\nПродлите прямо сейчас:",
                        parse_mode="HTML", reply_markup=renew_kb())
                except: pass
        except Exception as e:
            logger.error(f"Reminder error: {e}")
        await asyncio.sleep(3600)


# ── HANDLERS ──────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split(maxsplit=1)
    referred_by = None
    if len(args) > 1:
        p = args[1].strip()
        if p.startswith("ref_"):
            try:
                referred_by = int(p[4:])
                if referred_by == message.from_user.id: referred_by = None
            except ValueError: pass
        elif p == "admin" and message.from_user.id in ADMIN_IDS:
            s = await get_stats()
            await message.answer(fmt_stats(s), parse_mode="HTML", reply_markup=admin_kb()); return
    is_new = await register_user(message.from_user, referred_by)
    if is_new and referred_by:
        try:
            name = message.from_user.first_name or "Пользователь"
            await bot.send_message(referred_by,
                f"🎉 По вашей ссылке зарегисрировался <b>{name}</b>!\nВы получите <b>+15%</b> с его первой покупки.",
                parse_mode="HTML")
        except: pass
    # Проверка подписки на канал
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "📢 <b>Для доступа к боту нужно подписаться на наш канал!</b>\n\n"
            "Там мы публикуем новости, акции и обновления сервиса.",
            parse_mode="HTML", reply_markup=sub_required_kb()); return

    await message.answer(
        "🌐 <b>Добро пожаловать в VPN Itamani!</b>\n\n"
        "Быстрый, надёжный и безопасный VPN для телефона, ноутбука и любых поездок.\n\n"
        "<b>Что вы получаете:</b>\n"
        "♾ <b>Безлимитный трафик</b> — качайте и смотрите без ограничений\n"
        "📱 <b>До 10 устройств</b> на одной подписке одновременно\n"
        "⚡ <b>Высокая скорость</b> — стриминг 4K, игры, видеозвонки без лагов\n"
        "📶 <b>Работает везде</b> — Wi-Fi, LTE, даже там где обычно всё заблокировано\n"
        "🌍 <b>Серверы EU и USA</b> — российские сервисы работают как обычно\n"
        "🔒 <b>Современное шифрование</b> — ваши данные защищены\n"
        "🛠 <b>Простая настройка</b> — инструкция придёт сразу после оплаты\n"
        "💬 <b>Поддержка 24/7</b> — всегда на связи, ответим быстро\n\n"
        "💳 Оплата через <b>Telegram Stars</b> — мгновенно, без комиссий\n\n"
        "<i>Жмите «Открыть» — и поехали! 🚀</i>",
        parse_mode="HTML", reply_markup=main_kb())


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    s = await get_stats()
    await message.answer(fmt_stats(s), parse_mode="HTML", reply_markup=admin_kb())


@dp.message(Command("addvpn"))
async def cmd_addvpn(message: Message):
    """
    /addvpn USER_ID КОНФИГ_ИЛИ_КЛЮЧ [МЕТКА]
    Выдаёт VPN-конфиг пользователю и сохраняет в БД.
    """
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "📋 <b>Выдача VPN-конфига</b>\n\nИспользование:\n"
            "/addvpn USER_ID КОНФИГ [МЕТКА]\n\n"
            "Примеры:\n"
            "/addvpn 123456789 ss://base64... outline\n"
            "/addvpn 123456789 vpn.config.com/key=abc wireguard",
            parse_mode="HTML"); return
    try: target_id = int(parts[1])
    except: await message.answer("❌ Неверный USER_ID"); return
    config = parts[2]
    label  = parts[3] if len(parts) > 3 else "VPN"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO vpn_configs(user_id, config, label, issued_at)
            VALUES(?,?,?,datetime('now','localtime'))
            ON CONFLICT(user_id) DO UPDATE SET config=?, label=?, issued_at=datetime('now','localtime')
        """, (target_id, config, label, config, label))
        await db.commit()
        async with db.execute("SELECT first_name, username FROM users WHERE tg_id=?", (target_id,)) as c:
            urow = await c.fetchone()
    uname = urow[0] if urow else str(target_id)
    try:
        await bot.send_message(target_id,
            f"🔑 <b>Ваш VPN конфиг готов!</b>\n\n"
            f"<b>Тип:</b> {label}\n\n"
            f"<code>{config}</code>\n\n"
            f"Скопируйте ключ и добавьте в ваше VPN-приложение.\n"
            f"❓ Нужна помощь с настройкой: {SUPPORT}",
            parse_mode="HTML")
        await message.answer(f"✅ Конфиг отправлен пользователю <b>{uname}</b> (ID: {target_id})", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"⚠️ Конфиг сохранён, но не удалось отправить: {e}\nПользователь получит при следующем /myvpn")


@dp.message(Command("myvpn"))
async def cmd_myvpn(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT config, label, issued_at FROM vpn_configs WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
    if not row:
        await message.answer(
            "🔑 <b>VPN конфиг</b>\n\nКонфиг ещё не выдан.\n\n"
            "После оплаты подписки обратитесь в поддержку:\n" + SUPPORT,
            parse_mode="HTML", reply_markup=main_kb()); return
    config, label, issued_at = row
    await message.answer(
        f"🔑 <b>Ваш VPN конфиг</b>\n\n"
        f"<b>Тип:</b> {label}\n"
        f"<b>Выдан:</b> {issued_at[:10]}\n\n"
        f"<code>{config}</code>\n\n"
        f"❓ Поддержка: {SUPPORT}",
        parse_mode="HTML")


@dp.message(Command("extendvpn"))
async def cmd_extendvpn(message: Message):
    """
    /extendvpn USER_ID ПЛАН [КОММЕНТАРИЙ]
    Планы: 1_month / 3_months / 6_months / 1_year
    """
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "📅 <b>Продление подписки</b>\n\nИспользование:\n"
            "/extendvpn USER_ID ПЛАН [КОММЕНТАРИЙ]\n\n"
            "Планы: <code>1_month</code> / <code>3_months</code> / <code>6_months</code> / <code>1_year</code>\n\n"
            "Пример:\n/extendvpn 123456789 3_months оплата переводом",
            parse_mode="HTML"); return
    try: target_id = int(parts[1])
    except: await message.answer("❌ Неверный USER_ID"); return
    plan_key = parts[2].lower().strip()
    if plan_key not in PLAN_DAYS:
        await message.answer(f"❌ Неизвестный план. Доступны: {', '.join(PLAN_DAYS)}"); return
    days = PLAN_DAYS[plan_key]
    comment = parts[3] if len(parts) > 3 else "ручное продление"
    # Если есть активная подписка — продлеваем от неё, иначе от сейчас
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT expires_at FROM purchases
            WHERE user_id=? AND expires_at > datetime('now','localtime')
            ORDER BY expires_at DESC LIMIT 1
        """, (target_id,)) as c:
            active = await c.fetchone()
    if active:
        base = datetime.strptime(active[0], "%Y-%m-%d %H:%M:%S")
    else:
        base = datetime.now()
    expires_at = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    plan_title = PLANS[plan_key][0] if plan_key in PLANS else plan_key
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO purchases(user_id,plan,amount,currency,expires_at,date) VALUES(?,?,0,'MANUAL',?,datetime('now','localtime'))",
            (target_id, f"{plan_title} ({comment})", expires_at))
        await db.commit()
        async with db.execute("SELECT first_name FROM users WHERE tg_id=?", (target_id,)) as c:
            urow = await c.fetchone()
    uname = urow[0] if urow else str(target_id)
    exp_str = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
    try:
        await bot.send_message(target_id,
            f"✅ <b>Подписка продлена!</b>\n\n"
            f"Тариф: <b>{plan_title}</b>\n"
            f"Действует до: <b>{exp_str}</b>\n\n"
            f"Спасибо, что с нами! 🔒",
            parse_mode="HTML", reply_markup=main_kb())
    except: pass
    await message.answer(
        f"✅ <b>{uname}</b> (ID: {target_id})\n"
        f"Тариф: {plan_title} (+{days} дней)\n"
        f"Действует до: <b>{exp_str}</b>",
        parse_mode="HTML")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "📤 <b>Рассылка</b>\n\nИспользование:\n/broadcast Текст сообщения\n\n"
            "Поддерживает HTML: <b>жирный</b>, <i>курсив</i>, <code>моно</code>",
            parse_mode="HTML"); return
    text = parts[1]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tg_id FROM users WHERE is_banned=0") as c:
            users = await c.fetchall()
    total = len(users)
    sent = failed = 0
    status = await message.answer(f"📤 Рассылка запущена: 0/{total}...")
    for i, (uid,) in enumerate(users):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except: failed += 1
        if (i+1) % 25 == 0:
            try: await status.edit_text(f"📤 Рассылка: {i+1}/{total}...")
            except: pass
        await asyncio.sleep(0.04)
    await status.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n✔️ Отправлено: <b>{sent}</b>\n❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML")


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT join_date, referred_by FROM users WHERE tg_id=?", (uid,)) as c:
            user = await c.fetchone()
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)) as c:
            ref_count = (await c.fetchone())[0]
        async with db.execute("""
            SELECT plan, amount, currency, expires_at FROM purchases
            WHERE user_id=? ORDER BY date DESC LIMIT 3
        """, (uid,)) as c:
            purchases = await c.fetchall()
        async with db.execute("SELECT stars_earned, stars_paid FROM ref_balance WHERE user_id=?", (uid,)) as c:
            ref_bal = await c.fetchone()
        async with db.execute("""
            SELECT plan, expires_at FROM purchases
            WHERE user_id=? AND expires_at > datetime('now','localtime')
            ORDER BY expires_at DESC LIMIT 1
        """, (uid,)) as c:
            active = await c.fetchone()
        async with db.execute("SELECT label, issued_at FROM vpn_configs WHERE user_id=?", (uid,)) as c:
            cfg = await c.fetchone()
    join_date = user[0][:10] if user else "—"
    if active:
        exp = datetime.strptime(active[1], "%Y-%m-%d %H:%M:%S")
        days_left = (exp - datetime.now()).days
        sub_text = f"🟢 <b>{active[0]}</b>\n   Истекает: {exp.strftime('%d.%m.%Y')} (через {days_left} дн.)"
    else:
        sub_text = "🔴 Нет активной подпики"
    cfg_text = f"🔑 Конфиг: <b>{cfg[0]}</b> (выдан {cfg[1][:10]})" if cfg else "🔑 Конфиг: не выдан"
    purch_text = "\n".join(
        f"  • {p[0]} — {p[1]} {p[2]}"
        for p in purchases
    ) or "  — нет покупок"
    ref_earned = ref_bal[0] if ref_bal else 0
    ref_paid   = ref_bal[1] if ref_bal else 0
    ref_pend   = ref_earned - ref_paid
    await message.answer(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 Регистрация: {join_date}\n\n"
        f"📡 <b>Подписка:</b>\n{sub_text}\n\n"
        f"{cfg_text}\n\n"
        f"🔗 <b>Рефералы:</b> {ref_count} чел.\n"
        f"⭐ Бонус накоплен: <b>{ref_pend} Stars</b> (всего: {ref_earned})\n\n"
        f"🧾 <b>Последние покупки:</b>\n{purch_text}",
        parse_mode="HTML", reply_markup=main_kb())


@dp.message(Command("addpromo"))
async def cmd_addpromo(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /addpromo КОД [СКИДКА%] [МАКС]\nПример: /addpromo SUMMER 20 100"); return
    code = parts[1].upper()[:20]
    discount = max(1, min(int(parts[2]), 99)) if len(parts) > 2 and parts[2].isdigit() else 10
    max_uses = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    async with aiosqlite.connect(DB_PATH) as db:
        try: await db.execute("INSERT INTO promo_codes(code,discount,max_uses) VALUES(?,?,?)", (code, discount, max_uses))
        except: await db.execute("UPDATE promo_codes SET discount=?,max_uses=?,is_active=1 WHERE code=?", (discount, max_uses, code))
        await db.commit()
    limit = str(max_uses) if max_uses else "∞"
    await message.answer(f"✅ Промокод <code>{code}</code> — -{discount}%, лимит: {limit}", parse_mode="HTML")


@dp.message(Command("promos"))
async def cmd_promos(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT code,discount,max_uses,uses_count,is_active FROM promo_codes ORDER BY created_at DESC") as c:
            rows = await c.fetchall()
    if not rows: await message.answer("Промокодов нет. /addpromo КОД СКИДКА ЛИМИТ"); return
    lines = [f"{'✅' if r[4] else '❌'} <code>{r[0]}</code> -{r[1]}% | {r[3]}/{r[2] or '∞'}" for r in rows]
    await message.answer("🎟 <b>Промокоды:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(Command("delpromo"))
async def cmd_delpromo(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("Использование: /delpromo КОД"); return
    code = parts[1].upper()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE promo_codes SET is_active=0 WHERE code=?", (code,))
        await db.commit()
    await message.answer(f"✅ <code>{code}</code> деактивирован.", parse_mode="HTML")


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    uid = message.from_user.id
    link = f"https://t.me/Vpnitamani_bot?start=ref_{uid}"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)) as c:
            count = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(stars_earned-stars_paid,0) FROM ref_balance WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
    pending = row[0] if row else 0
    await message.answer(
        f"🔗 <b>Ваша реферальная ссылка:</b>\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{count}</b> чел. | Бонус: +15% со Stars-покупок\n"
        f"⭐ Накоплено к выплате: <b>{pending} Stars</b>\n\n"
        f"Для выплаты обратитесь в поддержку: {SUPPORT}",
        parse_mode="HTML")


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("Использование: /ban USER_ID"); return
    try: target = int(parts[1])
    except: await message.answer("Неверный ID"); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=1 WHERE tg_id=?", (target,)); await db.commit()
    await message.answer(f"✅ {target} заблокирован.")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("Использование: /unban USER_ID"); return
    try: target = int(parts[1])
    except: await message.answer("Неверный ID"); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=0 WHERE tg_id=?", (target,)); await db.commit()
    await message.answer(f"✅ {target} разблокирован.")


# ── CALLBACKS ─────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    await callback.answer()
    if await is_subscribed(callback.from_user.id):
        await callback.message.delete()
        await callback.message.answer(
            "✅ <b>Подписка подтверждена!</b>\n\nДобро пожаловать в VPN Itamani! 🔒",
            parse_mode="HTML", reply_markup=main_kb())
    else:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)


@dp.callback_query(F.data == "show_plans")
async def cb_show_plans(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "⭐ <b>Купить VPN за Telegram Stars</b>\n\nОплата мгновенная прямо в Telegram.\n\nВыбери тариф:",
        parse_mode="HTML", reply_markup=plans_kb())


@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy_plan(callback: CallbackQuery):
    await callback.answer()
    key = callback.data[4:]
    if key not in PLANS: return
    title, stars, _ = PLANS[key]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description="Безлимитный VPN, до 10 устройств, серверы EU/USA",
        payload=key, currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)])


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    stars   = message.successful_payment.total_amount
    title, _, days = PLANS.get(payload, (payload, 0, 0))
    expires_at = await record_purchase(message.from_user.id, title, stars, "XTR", days)
    exp_str = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y") if expires_at else "—"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT referred_by FROM users WHERE tg_id=?", (message.from_user.id,)) as c:
            row = await c.fetchone()
        async with db.execute("SELECT config FROM vpn_configs WHERE user_id=?", (message.from_user.id,)) as c:
            cfg_row = await c.fetchone()
    if row and row[0]:
        bonus = await add_ref_bonus(row[0], stars)
        try:
            await bot.send_message(row[0],
                f"💰 Реферал купил <b>{title}</b>!\n"
                f"Вам начислено: <b>+{bonus} ⭐</b>\n"
                f"Баланс: /ref | Выплата: {SUPPORT}",
                parse_mode="HTML")
        except: pass
    for aid in ADMIN_IDS:
        try:
            name = message.from_user.first_name or str(message.from_user.id)
            await bot.send_message(aid,
                f"⭐ <b>Оплата Stars!</b>\n👤 {name} (ID: {message.from_user.id})\n"
                f"Тариф: {title} — {stars} ⭐\nДо: {exp_str}\n\n"
                f"▶️ /addvpn {message.from_user.id} [КЛЮЧ]",
                parse_mode="HTML")
        except: pass
    cfg_text = f"\n\n🔑 Ваш конфиг уже готов — нажмите <b>Мой VPN конфиг</b>." if cfg_row else f"\n\n🔑 Конфиг придёт в ближайшее время от администратора."
    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\nТариф: <b>{title}</b> — {stars} ⭐\n"
        f"Действует до: <b>{exp_str}</b>{cfg_text}",
        parse_mode="HTML", reply_markup=main_kb())


@dp.callback_query(F.data == "my_vpn")
async def cb_my_vpn(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT config, label, issued_at FROM vpn_configs WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
    if not row:
        await callback.message.answer(
            "🔑 <b>VPN конфиг</b>\n\nКонфиг ещё не выдан.\n"
            "После оплаты подписки обратитесь в поддержку:\n" + SUPPORT,
            parse_mode="HTML"); return
    config, label, issued_at = row
    await callback.message.answer(
        f"🔑 <b>Ваш VPN конфиг</b>\n\n"
        f"<b>Тип:</b> {label}\n"
        f"<b>Выдан:</b> {issued_at[:10]}\n\n"
        f"<code>{config}</code>\n\n"
        f"❓ Поддержка: {SUPPORT}",
        parse_mode="HTML")


@dp.callback_query(F.data == "my_profile")
async def cb_my_profile(callback: CallbackQuery):
    await callback.answer()
    await cmd_profile(callback.message)


@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "🌐 <b>Добро пожаловать в VPN Itamani!</b>\n\n"
        "Быстрый, надёжный и безопасный VPN для телефона, ноутбука и любых поездок.\n\n"
        "<b>Что вы получаете:</b>\n"
        "♾ <b>Безлимитный трафик</b> — качайте и смотрите без ограничений\n"
        "📱 <b>До 10 устройств</b> на одной подписке одновременно\n"
        "⚡ <b>Высокая скорость</b> — стриминг 4K, игры, видеозвонки без лагов\n"
        "📶 <b>Работает везде</b> — Wi-Fi, LTE, даже там где обычно всё заблокировано\n"
        "🌍 <b>Серверы EU и USA</b> — российские сервисы работают как обычно\n"
        "🔒 <b>Современное шифрование</b> — ваши данные защищены\n"
        "🛠 <b>Простая настройка</b> — инструкция придёт сразу после оплаты\n"
        "💬 <b>Поддержка 24/7</b> — всегда на связи, ответим быстро\n\n"
        "💳 Оплата через <b>Telegram Stars</b> — мгновенно, без комиссий\n\n"
        "<i>Жмите «Открыть» — и поехали! 🚀</i>",
        parse_mode="HTML", reply_markup=main_kb())


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True); return
    await callback.answer("Обновляю...")
    s = await get_stats()
    try: await callback.message.edit_text(fmt_stats(s), parse_mode="HTML", reply_markup=admin_kb())
    except: await callback.message.answer(fmt_stats(s), parse_mode="HTML", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin_graph")
async def cb_admin_graph(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True); return
    await callback.answer("⏳ Генерирую...")
    try:
        buf = await gen_stats_chart()
        b = InlineKeyboardBuilder()
        b.button(text="◀️ Назад", callback_data="admin_stats")
        await callback.message.answer_photo(
            photo=BufferedInputFile(buf.read(), filename="stats.png"),
            caption="📈 <b>Статистика за 14 дней</b>",
            parse_mode="HTML",
            reply_markup=b.as_markup())
    except Exception as e:
        logger.error(f"Graph error: {e}")
        await callback.message.answer(f"❌ Ошибка генерации графика: {e}")


@dp.callback_query(F.data == "admin_users")
async def cb_admin_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True); return
    await callback.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT tg_id,username,first_name,join_date,is_banned FROM users ORDER BY join_date DESC LIMIT 15") as c:
            users = await c.fetchall()
    lines = [f"<code>{u[0]}</code> {u[2] or '—'} (@{u[1] or '—'}){'🚫' if u[4] else ''}\n  📅 {u[3][:10]}" for u in users]
    b = InlineKeyboardBuilder(); b.button(text="◀️ Назад", callback_data="admin_stats")
    try: await callback.message.edit_text("👥 <b>Последние 15 пользователей:</b>\n\n" + "\n\n".join(lines), parse_mode="HTML", reply_markup=b.as_markup())
    except: await callback.message.answer("👥 <b>Последние 15 пользователей:</b>\n\n" + "\n\n".join(lines), parse_mode="HTML", reply_markup=b.as_markup())


@dp.callback_query(F.data == "admin_promos")
async def cb_admin_promos(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True); return
    await callback.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT code,discount,max_uses,uses_count,is_active FROM promo_codes ORDER BY created_at DESC") as c:
            rows = await c.fetchall()
    if not rows:
        text = "🎟 Промокодов нет.\n\n/addpromo КОД СКИДКА ЛИМИТ"
    else:
        lines = [f"{'✅' if r[4] else '❌'} <code>{r[0]}</code> -{r[1]}% | {r[3]}/{r[2] or '∞'}" for r in rows]
        text = "🎟 <b>Промокоды:</b>\n\n" + "\n".join(lines) + "\n\n/addpromo КОД СКИДКА ЛИМИТ\n/delpromo КОД"
    b = InlineKeyboardBuilder(); b.button(text="◀️ Назад", callback_data="admin_stats")
    try: await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
    except: await callback.message.answer(text, parse_mode="HTML", reply_markup=b.as_markup())


@dp.callback_query(F.data == "my_subs")
async def cb_my_subs(callback: CallbackQuery):
    await callback.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT plan,amount,currency,expires_at,date FROM purchases WHERE user_id=? ORDER BY date DESC LIMIT 5", (callback.from_user.id,)) as c:
            rows = await c.fetchall()
    if rows:
        lines = []
        for r in rows:
            exp = f" (до {r[3][:10]})" if r[3] else ""
            lines.append(f"  • {r[0]} — {r[1]} {r[2]}{exp}")
        text = "📋 <b>Ваши покупки:</b>\n\n" + "\n".join(lines)
    else:
        text = "📋 Покупок пока нет."
    await callback.message.answer(text, parse_mode="HTML", reply_markup=main_kb())


@dp.callback_query(F.data == "my_ref")
async def cb_my_ref(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    link = f"https://t.me/Vpnitamani_bot?start=ref_{uid}"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)) as c:
            count = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(stars_earned-stars_paid,0) FROM ref_balance WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
    pending = row[0] if row else 0
    await callback.message.answer(
        f"🔗 <b>Ваша реферальная ссылка:</b>\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{count}</b> чел. | Бонус: +15% со Stars-покупок\n"
        f"⭐ Накоплено к выплате: <b>{pending} Stars</b>",
        parse_mode="HTML")


@dp.message(F.web_app_data)
async def handle_web_app(message: Message):
    try: data = json.loads(message.web_app_data.data)
    except: return
    action = str(data.get("action", ""))
    if action == "buy_stars":
        # Оплата Stars из Mini App — отправляем инвойс
        plan_key = str(data.get("plan", ""))
        webapp_plan = WEBAPP_PLAN_MAP.get(plan_key)
        stars = WEBAPP_STARS.get(plan_key, 0)
        if webapp_plan and stars:
            title, _, _ = PLANS.get(webapp_plan, (plan_key, stars, 30))
            await bot.send_invoice(
                chat_id=message.from_user.id,
                title=title,
                description="Безлимитный VPN, до 10 устройств, серверы EU/USA",
                payload=webapp_plan, currency="XTR",
                prices=[LabeledPrice(label=title, amount=stars)])
        elif stars:
            # day/week — нет Stars-плана, оформляем вручную
            plan_name = str(data.get("name", plan_key))[:30]
            await record_purchase(message.from_user.id, plan_name, stars, "XTR")
            for aid in ADMIN_IDS:
                try:
                    nm = message.from_user.first_name or str(message.from_user.id)
                    await bot.send_message(aid,
                        f"⭐ <b>Заявка Stars!</b>\n👤 {nm} (ID: {message.from_user.id})\n"
                        f"{plan_name} — {stars} ⭐\n\n▶️ /addvpn {message.from_user.id} [КЛЮЧ]",
                        parse_mode="HTML")
                except: pass
            await message.answer(
                f"✅ <b>Заявка принята!</b>\n{plan_name} — {stars} ⭐\n\n"
                f"Конфиг придёт в ближайшее время. Поддержка: <a href=\"{SUPPORT}\">@vpnitamani</a>",
                parse_mode="HTML", reply_markup=main_kb())
    elif action == "crypto_pay":
        # Оплата криптой из Mini App
        plan_name = str(data.get("name", "VPN"))[:30]
        rub = int(data.get("rub", 0))
        uid = message.from_user.id
        # Примерный курс: 1 USDT ≈ 95 ₽, 1 TON ≈ 350 ₽
        usdt = round(rub / 95, 2)
        ton  = round(rub / 350, 2)
        await record_purchase(uid, plan_name, rub, "CRYPTO")
        for aid in ADMIN_IDS:
            try:
                nm = message.from_user.first_name or str(uid)
                await bot.send_message(aid,
                    f"💎 <b>Заявка крипто!</b>\n👤 {nm} (ID: {uid})\n"
                    f"{plan_name} — {rub} ₽ / {usdt} USDT / {ton} TON\n\n"
                    f"▶️ /extendvpn {uid} 1_month\n▶️ /addvpn {uid} [КЛЮЧ]",
                    parse_mode="HTML")
            except: pass
        await message.answer(
            f"💎 <b>Оплата криптой</b>\n\n"
            f"Тариф: <b>{plan_name}</b>\n\n"
            f"<b>TON:</b> <code>{ton} TON</code>\n"
            f"<code>{TON_WALLET}</code>\n\n"
            f"<b>USDT (TRC-20):</b> <code>{usdt} USDT</code>\n"
            f"<code>{USDT_WALLET}</code>\n\n"
            f"⚠️ В комментарии к переводу укажите ваш ID: <code>{uid}</code>\n\n"
            f"После оплаты отправьте скриншот в поддержку:\n<a href=\"{SUPPORT}\">@vpnitamani</a>",
            parse_mode="HTML", reply_markup=main_kb())
    elif action == "buy":
        plan = str(data.get("plan", ""))[:30]
        try: amount = int(data.get("price", 0))
        except: amount = 0
        if not plan or amount <= 0: return
        await record_purchase(message.from_user.id, plan, amount, "RUB")
        for aid in ADMIN_IDS:
            try:
                name = message.from_user.first_name or str(message.from_user.id)
                await bot.send_message(aid,
                    f"💰 <b>Заявка!</b>\n👤 {name} (ID: {message.from_user.id})\n{plan} — {amount} ₽\n\n"
                    f"▶️ /extendvpn {message.from_user.id} 1_month\n"
                    f"▶️ /addvpn {message.from_user.id} [КЛЮЧ]",
                    parse_mode="HTML")
            except: pass
        await message.answer(
            f"✅ <b>Заявка получена!</b>\nТариф: <b>{plan}</b> — {amount} ₽\n\nДля оплаты: <a href=\"{SUPPORT}\">@vpnitamani</a>",
            parse_mode="HTML", reply_markup=main_kb())
    elif action == "promo":
        code = str(data.get("code", ""))[:20].upper()
        discount, error = await validate_promo(message.from_user.id, code)
        if error: await message.answer(error)
        else:
            await apply_promo(message.from_user.id, code)
            await message.answer(f"🎉 <b>Промокод {code} активирован!</b>\nСкидка: <b>{discount}%</b>", parse_mode="HTML")
    elif action == "admin_stats" and message.from_user.id in ADMIN_IDS:
        s = await get_stats()
        await message.answer(fmt_stats(s), parse_mode="HTML", reply_markup=admin_kb())


@dp.message(Command("ticket"))
async def cmd_ticket(message: Message):
    """Пользователь создаёт тикет: /ticket Текст обращения"""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🎫 <b>Поддержка</b>\n\n"
            "Отправьте своё обращение командой:\n"
            "<code>/ticket Ваш вопрос или проблема</code>\n\n"
            "Или напишите напрямую: " + SUPPORT,
            parse_mode="HTML"); return
    text = parts[1][:1000]
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tickets(user_id, subject) VALUES(?,?)",
            (uid, text[:80]))
        ticket_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO ticket_msgs(ticket_id, user_id, text, is_admin) VALUES(?,?,?,0)",
            (ticket_id, uid, text))
        await db.commit()
    name = message.from_user.first_name or str(uid)
    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    for aid in ADMIN_IDS:
        try:
            b = InlineKeyboardBuilder()
            b.button(text=f"✉️ Ответить #{ticket_id}", callback_data=f"ticket_open_{ticket_id}")
            await bot.send_message(aid,
                f"🎫 <b>Новый тикет #{ticket_id}</b>\n"
                f"👤 {name} ({username}) | ID: <code>{uid}</code>\n\n"
                f"📝 {text}",
                parse_mode="HTML", reply_markup=b.as_markup())
        except: pass
    await message.answer(
        f"✅ <b>Тикет #{ticket_id} создан!</b>\n\n"
        f"Мы ответим в ближайшее время.\n"
        f"Вы получите уведомление прямо здесь.",
        parse_mode="HTML")


@dp.message(Command("tickets"))
async def cmd_tickets(message: Message):
    """Админ: список открытых тикетов"""
    if message.from_user.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT t.id, t.user_id, u.first_name, u.username, t.subject, t.created_at, t.status
            FROM tickets t LEFT JOIN users u ON t.user_id=u.tg_id
            WHERE t.status='open' ORDER BY t.created_at DESC LIMIT 20
        """) as c:
            rows = await c.fetchall()
    if not rows:
        await message.answer("✅ Открытых тикетов нет."); return
    lines = []
    for r in rows:
        nm = r[2] or str(r[1])
        un = f"@{r[3]}" if r[3] else ""
        lines.append(f"🎫 <b>#{r[0]}</b> — {nm} {un}\n   {r[4][:60]}\n   📅 {r[5][:10]}")
    b = InlineKeyboardBuilder(); b.button(text="◀️ Назад", callback_data="admin_stats")
    await message.answer(
        f"🎫 <b>Открытые тикеты ({len(rows)}):</b>\n\n" + "\n\n".join(lines) + "\n\n/reply ID ОТВЕТ — ответить",
        parse_mode="HTML", reply_markup=b.as_markup())


@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    """Админ: /reply TICKET_ID Текст ответа"""
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /reply TICKET_ID Текст\nПример: /reply 5 Проблема решена!"); return
    try: ticket_id = int(parts[1])
    except: await message.answer("❌ Неверный ID тикета"); return
    reply_text = parts[2][:1000]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, subject, status FROM tickets WHERE id=?", (ticket_id,)) as c:
            row = await c.fetchone()
    if not row:
        await message.answer(f"❌ Тикет #{ticket_id} не найден."); return
    user_id, subject, status = row
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ticket_msgs(ticket_id, user_id, text, is_admin) VALUES(?,?,?,1)",
            (ticket_id, message.from_user.id, reply_text))
        await db.execute("UPDATE tickets SET status='answered' WHERE id=?", (ticket_id,))
        await db.commit()
    try:
        b = InlineKeyboardBuilder()
        b.button(text="💬 Уточнить вопрос", callback_data=f"ticket_followup_{ticket_id}")
        await bot.send_message(user_id,
            f"💬 <b>Ответ на ваш тикет #{ticket_id}</b>\n\n"
            f"<i>Ваш вопрос:</i> {subject[:80]}\n\n"
            f"<b>Ответ поддержки:</b>\n{reply_text}",
            parse_mode="HTML", reply_markup=b.as_markup())
        await message.answer(f"✅ Ответ отправлен пользователю (тикет #{ticket_id} → статус: answered)")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить: {e}")


@dp.message(Command("closeticket"))
async def cmd_closeticket(message: Message):
    """Админ: /closeticket ID"""
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split()
    if len(parts) < 2: await message.answer("Использование: /closeticket ID"); return
    try: ticket_id = int(parts[1])
    except: await message.answer("❌ Неверный ID"); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
        await db.commit()
    await message.answer(f"✅ Тикет #{ticket_id} закрыт.")


@dp.callback_query(F.data == "admin_tickets")
async def cb_admin_tickets(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ Нет доступа", show_alert=True); return
    await callback.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT t.id, t.user_id, u.first_name, u.username, t.subject, t.created_at
            FROM tickets t LEFT JOIN users u ON t.user_id=u.tg_id
            WHERE t.status='open' ORDER BY t.created_at DESC LIMIT 15
        """) as c:
            rows = await c.fetchall()
        async with db.execute("SELECT COUNT(*) FROM tickets WHERE status='open'") as c:
            total_open = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM tickets") as c:
            total_all = (await c.fetchone())[0]
    if not rows:
        text = f"🎫 <b>Тикеты</b>\n\nОткрытых: 0 | Всего: {total_all}\n\n✅ Всё обработано!"
    else:
        lines = []
        for r in rows:
            nm = r[2] or str(r[1])
            lines.append(f"<b>#{r[0]}</b> {nm} — {r[4][:50]}")
        text = f"🎫 <b>Тикеты</b> | Открытых: {total_open} | Всего: {total_all}\n\n" + "\n".join(lines) + "\n\n/reply ID ОТВЕТ\n/closeticket ID"
    b = InlineKeyboardBuilder(); b.button(text="◀️ Назад", callback_data="admin_stats")
    try: await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup())
    except: await callback.message.answer(text, parse_mode="HTML", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("ticket_open_"))
async def cb_ticket_open(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True); return
    await callback.answer()
    ticket_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT t.user_id, u.first_name, u.username, t.subject, t.status
            FROM tickets t LEFT JOIN users u ON t.user_id=u.tg_id WHERE t.id=?
        """, (ticket_id,)) as c:
            t = await c.fetchone()
        async with db.execute(
            "SELECT text, is_admin, created_at FROM ticket_msgs WHERE ticket_id=? ORDER BY id",
            (ticket_id,)) as c:
            msgs = await c.fetchall()
    if not t: await callback.message.answer("Тикет не найден."); return
    lines = [f"{'🔵 Поддержка' if m[1] else '👤 Пользователь'} [{m[2][:16]}]:\n{m[0]}" for m in msgs]
    status_icon = {'open':'🔴','answered':'🟡','closed':'⚫'}.get(t[4],'⚪')
    text = (f"🎫 <b>Тикет #{ticket_id}</b> {status_icon}\n"
            f"👤 {t[1] or t[0]} (@{t[2] or '—'}) | ID: <code>{t[0]}</code>\n\n"
            + "\n\n".join(lines))
    b = InlineKeyboardBuilder()
    b.button(text="◀️ К тикетам", callback_data="admin_tickets")
    await callback.message.answer(text[:4000], parse_mode="HTML", reply_markup=b.as_markup())
    await callback.message.answer(f"Ответить: /reply {ticket_id} Текст ответа")


@dp.callback_query(F.data.startswith("ticket_followup_"))
async def cb_ticket_followup(callback: CallbackQuery):
    await callback.answer()
    ticket_id = int(callback.data.split("_")[-1])
    await callback.message.answer(
        f"💬 Чтобы уточнить вопрос по тикету #{ticket_id}, используйте:\n"
        f"<code>/ticket Ваш уточняющий вопрос</code>\n\n"
        f"Или напишите напрямую: {SUPPORT}",
        parse_mode="HTML")



async def set_bot_commands():
    """Устанавливает список команд в меню бота."""
    user_cmds = [
        ("start",    "🏠 Главная"),
        ("status",   "📡 Статус подписки"),
        ("myvpn",    "🔑 Мой VPN конфиг"),
        ("setup",    "📖 Инструкция о настройке"),
        ("trial",    "🎁 Пробный день бесплатно"),
        ("ticket",   "🎫 Написать в поддержку"),
        ("ref",      "🔗 Реферальная программа"),
        ("profile",  "👤 Мой профиль"),
    ]
    admin_cmds = user_cmds + [
        ("admin",        "📊 Панель администратора"),
        ("tickets",      "🎫 Список тикетов"),
        ("broadcast",    "📤 Рассылка"),
        ("addvpn",       "🔑 Выдать VPN"),
        ("extendvpn",    "📅 Продлить подписку"),
        ("finduser",     "🔍 Найти пользователя"),
        ("addpromo",     "🎟 Добавить промокод"),
        ("ban",          "🚫 Заблокировать"),
        ("unban",        "✅ Разблокировать"),
    ]
    from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
    await bot.set_my_commands([BotCommand(command=c, description=d) for c,d in user_cmds],
                               scope=BotCommandScopeDefault())
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c,d in admin_cmds],
                                       scope=BotCommandScopeChat(chat_id=aid))
        except: pass
    logger.info("Bot commands set")


async def daily_digest_task():
    """Ежедневный дайджест для админа в 09:00."""
    await asyncio.sleep(10)
    while True:
        now = datetime.now()
        # Ждём до следующего 09:00
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async def one(q): return (await (await db.execute(q)).fetchone())[0]
                new_users   = await one("SELECT COUNT(*) FROM users WHERE date(join_date)=date('now','localtime')")
                purch_today = await one("SELECT COUNT(*) FROM purchases WHERE date(date)=date('now','localtime')")
                stars_today = await one("SELECT COALESCE(SUM(amount),0) FROM purchases WHERE currency='XTR' AND date(date)=date('now','localtime')")
                rub_today   = await one("SELECT COALESCE(SUM(amount),0) FROM purchases WHERE currency IN ('RUB','CRYPTO') AND date(date)=date('now','localtime')")
                active_subs = await one("SELECT COUNT(DISTINCT user_id) FROM purchases WHERE expires_at > datetime('now','localtime')")
                total_users = await one("SELECT COUNT(*) FROM users")
                open_tickets= await one("SELECT COUNT(*) FROM tickets WHERE status='open'")
            for aid in ADMIN_IDS:
                try:
                    await bot.send_message(aid,
                        f"☀️ <b>Доброе утро! Дайджест за {now.strftime('%d.%m.%Y')}</b>\n\n"
                        f"👥 Новых пользователей: <b>{new_users}</b> | Всего: {total_users}\n"
                        f"💳 Покупок сегодня: <b>{purch_today}</b>\n"
                        f"⭐ Stars: <b>{stars_today}</b> | ₽: <b>{rub_today}</b>\n"
                        f"🟢 Активных подписок: <b>{active_subs}</b>\n"
                        f"🎫 Открытых тикетов: <b>{open_tickets}</b>",
                        parse_mode="HTML", reply_markup=admin_kb())
                except: pass
        except Exception as e:
            logger.error(f"Daily digest error: {e}")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT plan, expires_at FROM purchases
            WHERE user_id=? AND expires_at > datetime('now','localtime')
            ORDER BY expires_at DESC LIMIT 1
        """, (uid,)) as c:
            active = await c.fetchone()
        async with db.execute("SELECT config, label FROM vpn_configs WHERE user_id=?", (uid,)) as c:
            cfg = await c.fetchone()
    if active:
        exp = datetime.strptime(active[1], "%Y-%m-%d %H:%M:%S")
        days_left = (exp - datetime.now()).days
        bar_full  = min(int(days_left / 30 * 10), 10)
        bar = "█" * bar_full + "░" * (10 - bar_full)
        status_text = (
            f"🟢 <b>Подписка активна</b>\n\n"
            f"📦 Тариф: <b>{active[0]}</b>\n"
            f"📅 Истекает: <b>{exp.strftime('%d.%m.%Y')}</b>\n"
            f"⏳ Осталось: <b>{days_left} дн.</b>\n"
            f"[{bar}]\n\n"
        )
    else:
        status_text = "🔴 <b>Подписка не активна</b>\n\nКупите тариф в Mini App:\n\n"
    cfg_text = f"🔑 Конфиг: <b>{cfg[1]}</b> — готов" if cfg else "🔑 Конфиг: ожидает выдачи"
    b = InlineKeyboardBuilder()
    if not active:
        b.button(text="⭐ Купить подписку", web_app=WebAppInfo(url=MINI_APP_URL))
    else:
        b.button(text="🔄 Продлить", callback_data="show_plans")
    b.button(text="🔑 Мой конфиг", callback_data="my_vpn")
    b.adjust(1)
    await message.answer(status_text + cfg_text, parse_mode="HTML", reply_markup=b.as_markup())


@dp.message(Command("setup"))
async def cmd_setup(message: Message):
    b = InlineKeyboardBuilder()
    b.button(text="📱 iOS (iPhone/iPad)",    callback_data="setup_ios")
    b.button(text="🤖 Android",               callback_data="setup_android")
    b.button(text="💻 Windows",               callback_data="setup_windows")
    b.button(text="🍎 macOS",                 callback_data="setup_mac")
    b.adjust(1)
    await message.answer(
        "📖 <b>Настройка VPN Itamani</b>\n\nВыберите вашу платформу:",
        parse_mode="HTML", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("setup_"))
async def cb_setup(callback: CallbackQuery):
    await callback.answer()
    platform = callback.data[6:]
    guides = {
        "ios": (
            "📱 <b>Настройка на iOS (iPhone/iPad)</b>\n\n"
            "<b>Шаг 1.</b> Установите приложение <b>Happ</b> из App Store — работает на всех устройствах\n\n"
            "<b>Шаг 2.</b> Скопируйте ваш ключ командой /myvpn\n\n"
            "<b>Шаг 3.</b> Откройте Happ → нажмите <b>+</b> → <b>Вставить из буфера</b>\n\n"
            "<b>Шаг 4.</b> Нажмите <b>Подключить</b> — готово! 🎉\n\n"
            "❓ Проблемы? <a href=\"https://t.me/vpnitamani\">Поддержка</a>"
        ),
        "android": (
            "🤖 <b>Настройка на Android</b>\n\n"
            "<b>Шаг 1.</b> Установите приложение <b>Happ</b> из Google Play — работает на всех устройствах\n\n"
            "<b>Шаг 2.</b> Скопируйте ваш ключ командой /myvpn\n\n"
            "<b>Шаг 3.</b> Откройте Happ → нажмите <b>+</b> → <b>Вставить из буфера</b>\n\n"
            "<b>Шаг 4.</b> Нажмите ▶️ — подключение активно! 🎉\n\n"
            "❓ Проблемы? <a href=\"https://t.me/vpnitamani\">Поддержка</a>"
        ),
        "windows": (
            "💻 <b>Настройка на Windows</b>\n\n"
            "<b>Шаг 1.</b> Скачайте приложение <b>Happ</b> — работает на всех устройствах, включая Windows\n\n"
            "<b>Шаг 2.</b> Скопируйте ваш ключ командой /myvpn\n\n"
            "<b>Шаг 3.</b> Откройте Happ → нажмите <b>+</b> → <b>Вставить из буфера</b>\n\n"
            "<b>Шаг 4.</b> Нажмите <b>Подключить</b> — VPN работает! 🎉\n\n"
            "❓ Проблемы? <a href=\"https://t.me/vpnitamani\">Поддержка</a>"
        ),
        "mac": (
            "🍎 <b>Настройка на macOS</b>\n\n"
            "<b>Шаг 1.</b> Установите приложение <b>Happ</b> из Mac App Store — работает на всех устройствах\n\n"
            "<b>Шаг 2.</b> Скопируйте ваш ключ командой /myvpn\n\n"
            "<b>Шаг 3.</b> Откройте Happ → нажмите <b>+</b> → <b>Вставить из буфера</b>\n\n"
            "<b>Шаг 4.</b> Включите переключатель — подключено! 🎉\n\n"
            "❓ Проблемы? <a href=\"https://t.me/vpnitamani\">Поддержка</a>"
        ),
    }
    text = guides.get(platform, "❌ Платформа не найдена")
    b = InlineKeyboardBuilder()
    b.button(text="🔑 Получить мой ключ", callback_data="my_vpn")
    b.button(text="◀️ Назад",             callback_data="setup_back")
    b.adjust(1)
    try: await callback.message.edit_text(text, parse_mode="HTML", reply_markup=b.as_markup(), disable_web_page_preview=True)
    except: await callback.message.answer(text, parse_mode="HTML", reply_markup=b.as_markup(), disable_web_page_preview=True)


@dp.callback_query(F.data == "setup_back")
async def cb_setup_back(callback: CallbackQuery):
    await callback.answer()
    b = InlineKeyboardBuilder()
    b.button(text="📱 iOS (iPhone/iPad)",    callback_data="setup_ios")
    b.button(text="🤖 Android",               callback_data="setup_android")
    b.button(text="💻 Windows",               callback_data="setup_windows")
    b.button(text="🍎 macOS",                 callback_data="setup_mac")
    b.adjust(1)
    try: await callback.message.edit_text("📖 <b>Настройка VPN Itamani</b>\n\nВыберите вашу платформу:", parse_mode="HTML", reply_markup=b.as_markup())
    except: await callback.message.answer("📖 <b>Настройка VPN Itamani</b>\n\nВыберите вашу платформу:", parse_mode="HTML", reply_markup=b.as_markup())


@dp.message(Command("finduser"))
async def cmd_finduser(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование:\n/finduser ID или /finduser @username"); return
    query = parts[1].strip().lstrip('@')
    async with aiosqlite.connect(DB_PATH) as db:
        if query.isdigit():
            async with db.execute("SELECT tg_id,username,first_name,join_date,is_banned,referred_by FROM users WHERE tg_id=?", (int(query),)) as c:
                user = await c.fetchone()
        else:
            async with db.execute("SELECT tg_id,username,first_name,join_date,is_banned,referred_by FROM users WHERE username=?", (query,)) as c:
                user = await c.fetchone()
        if not user: await message.answer("❌ Пользователь не найден"); return
        uid, uname, fname, jdate, banned, ref_by = user
        async with db.execute("SELECT plan,amount,currency,expires_at,date FROM purchases WHERE user_id=? ORDER BY date DESC LIMIT 5", (uid,)) as c:
            purchases = await c.fetchall()
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)) as c:
            ref_count = (await c.fetchone())[0]
        async with db.execute("""SELECT plan,expires_at FROM purchases WHERE user_id=? AND expires_at>datetime('now','localtime') ORDER BY expires_at DESC LIMIT 1""", (uid,)) as c:
            active = await c.fetchone()
        async with db.execute("SELECT label FROM vpn_configs WHERE user_id=?", (uid,)) as c:
            cfg = await c.fetchone()
    sub_str = f"🟢 {active[0]} до {active[1][:10]}" if active else "🔴 Нет"
    cfg_str = f"🔑 {cfg[0]}" if cfg else "🔑 Нет конфига"
    purch_str = "\n".join(f"  • {p[0]} — {p[1]} {p[2]}" for p in purchases) or "  — нет"
    ban_icon = "🚫 Заблокирован" if banned else "✅ Активен"
    b = InlineKeyboardBuilder()
    if banned:
        b.button(text="✅ Разблокировать", callback_data=f"fu_unban_{uid}")
    else:
        b.button(text="🚫 Заблокировать", callback_data=f"fu_ban_{uid}")
    b.button(text="📅 +1 месяц",   callback_data=f"fu_extend_{uid}")
    b.button(text="📤 Написать",    callback_data=f"fu_msg_{uid}")
    b.adjust(2, 1)
    await message.answer(
        f"👤 <b>{fname or '—'}</b> (@{uname or '—'})\n"
        f"🆔 <code>{uid}</code> | {ban_icon}\n"
        f"📅 Рег: {jdate[:10]} | Рефов: {ref_count}\n\n"
        f"📡 Подписка: {sub_str}\n"
        f"{cfg_str}\n\n"
        f"🧾 Покупки:\n{purch_str}\n\n"
        f"▶️ /addvpn {uid} [КЛЮЧ]\n"
        f"▶️ /extendvpn {uid} 1_month",
        parse_mode="HTML", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("fu_"))
async def cb_finduser_action(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True); return
    parts = callback.data.split("_")
    action, uid = parts[1], int(parts[2])
    if action == "ban":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned=1 WHERE tg_id=?", (uid,)); await db.commit()
        await callback.answer("✅ Заблокирован")
    elif action == "unban":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned=0 WHERE tg_id=?", (uid,)); await db.commit()
        await callback.answer("✅ Разблокирован")
    elif action == "extend":
        expires_at = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO purchases(user_id,plan,amount,currency,expires_at) VALUES(?,?,0,'MANUAL',?)",
                             (uid, "VPN — 1 месяц (admin)", expires_at)); await db.commit()
        try: await bot.send_message(uid, f"✅ <b>Подписка продлена на 1 месяц!</b>\nДо: {expires_at[:10]}", parse_mode="HTML")
        except: pass
        await callback.answer(f"✅ +30 дней выдано до {expires_at[:10]}")
    elif action == "msg":
        await callback.message.answer(f"Чтобы написать пользователю {uid}:\n/broadcast [ТЕКСТ] (для всех)\nИли используйте /reply для ответа на тикет.")
        await callback.answer()


@dp.message(Command("trial"))
async def cmd_trial(message: Message):
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, использовал ли уже триал
        async with db.execute("SELECT id FROM purchases WHERE user_id=? AND plan LIKE '%trial%'", (uid,)) as c:
            used = await c.fetchone()
        async with db.execute("SELECT plan, expires_at FROM purchases WHERE user_id=? AND expires_at>datetime('now','localtime') ORDER BY expires_at DESC LIMIT 1", (uid,)) as c:
            active = await c.fetchone()
    if active:
        await message.answer(
            f"✅ У вас уже активна подписка <b>{active[0]}</b> до {active[1][:10]}\n\n"
            f"Пробный период доступен только новым пользователям без подписки.",
            parse_mode="HTML"); return
    if used:
        await message.answer(
            "ℹ️ Вы уже использовали пробный период.\n\n"
            "Купите полную подписку в Mini App или воспользуйтесь промокодом.",
            parse_mode="HTML", reply_markup=main_kb()); return
    name = message.from_user.first_name or str(uid)
    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    for aid in ADMIN_IDS:
        try:
            b = InlineKeyboardBuilder()
            b.button(text=f"✅ Выдать триал", callback_data=f"give_trial_{uid}")
            await bot.send_message(aid,
                f"🎁 <b>Запрос пробного периода</b>\n"
                f"👤 {name} ({username}) | ID: <code>{uid}</code>",
                parse_mode="HTML", reply_markup=b.as_markup())
        except: pass
    await message.answer(
        "🎁 <b>Запрос на пробный день отправлен!</b>\n\n"
        "Администратор рассмотрит и выдаст доступ в течение нескольких часов.\n"
        "Вы получите уведомление.",
        parse_mode="HTML")


@dp.callback_query(F.data.startswith("give_trial_"))
async def cb_give_trial(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔", show_alert=True); return
    await callback.answer("Выдаю...")
    uid = int(callback.data.split("_")[-1])
    expires_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO purchases(user_id,plan,amount,currency,expires_at) VALUES(?,?,0,'TRIAL',?)",
                         (uid, "VPN — trial 1 день", expires_at))
        await db.commit()
    try:
        await bot.send_message(uid,
            f"🎁 <b>Пробный день активирован!</b>\n\n"
            f"Ваш пробный доступ действует 24 часа — до {expires_at[:10]}.\n\n"
            f"🔑 Для получения конфига: /myvpn\n"
            f"📖 Инструкция по настройке: /setup",
            parse_mode="HTML", reply_markup=main_kb())
    except: pass
    await callback.message.answer(f"✅ Триал выдан пользователю {uid} на 24 часа.")



# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    await init_db()
    dp.update.middleware(AntiFloodMiddleware())
    dp.update.middleware(BanCheckMiddleware())
    asyncio.create_task(reminder_task())
    asyncio.create_task(daily_digest_task())
    await set_bot_commands()
    logger.info("Bot is running...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "pre_checkout_query"])

if __name__ == "__main__":
    asyncio.run(main())
