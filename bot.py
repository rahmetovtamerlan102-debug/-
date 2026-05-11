#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import asyncpg
import os
import signal
import time
import random
import string
import logging
from datetime import date, datetime
from collections import deque, OrderedDict
from aiohttp import web, ClientSession

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery

# ================= КОНФИГ =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@Auron_search")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Auronsearchesbot")
CHECK_SUBSCRIPTION = os.getenv("CHECK_SUBSCRIPTION", "True").lower() == "true"
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
PREMIUM_MULTIPLIER = 5

USDT_WALLET = "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV"
USDT_PRICES = {"7d": 3, "30d": 6, "forever": 25}
USDT_DAYS = {"7d": 7, "30d": 30, "forever": 0}

STARS_PRICES = {"7d": 60, "30d": 240, "forever": 800}
STARS_DAYS = {"7d": 7, "30d": 30, "forever": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.AsyncTeleBot(TOKEN)

# ================= БАЗА ДАННЫХ =================
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    async def init(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)
        async with self.pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                premium_until BIGINT,
                searches_today INT DEFAULT 0,
                last_date TEXT,
                referrer_id BIGINT DEFAULT 0,
                referals_count INT DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_users_last_date ON users(last_date);
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_usernames (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                created_at BIGINT
            );
            CREATE INDEX IF NOT EXISTS idx_saved_user_id ON saved_usernames(user_id);
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                method TEXT,
                tariff TEXT,
                amount FLOAT,
                days INT,
                created_at BIGINT,
                status TEXT DEFAULT 'waiting'
            );
            CREATE INDEX idx_payments_status ON payments(status);
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                message TEXT,
                created_at BIGINT,
                is_read BOOLEAN DEFAULT FALSE
            );
            CREATE INDEX idx_support_is_read ON support_messages(is_read);
            """)
        logger.info("DB ready")

    async def get_user(self, uid):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT premium_until, searches_today, last_date, referals_count FROM users WHERE user_id=$1", uid
            )
            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id, last_date, referals_count, premium_until) VALUES ($1, $2, 0, NULL)",
                    uid, str(date.today())
                )
                return False, 0, str(date.today()), None, 0
            premium_until, searches, last, refs = row
            is_premium = premium_until is None or (premium_until > int(time.time()))
            today = str(date.today())
            if last != today:
                searches = 0
                await conn.execute("UPDATE users SET searches_today=0, last_date=$1 WHERE user_id=$2", today, uid)
            return is_premium, searches, last, premium_until, refs

    async def get_max_searches(self, uid):
        p, _, _, _, refs = await self.get_user(uid)
        base = FREE_LIMIT + refs
        return base * PREMIUM_MULTIPLIER if p else base

    async def can_search(self, uid):
        p, s, _, _, _ = await self.get_user(uid)
        return p or s < await self.get_max_searches(uid)

    async def inc_search(self, uid):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE users SET searches_today = searches_today + 1 WHERE user_id=$1", uid)

    async def get_remaining(self, uid):
        p, s, _, _, _ = await self.get_user(uid)
        if p:
            return float('inf')
        remain = await self.get_max_searches(uid) - s
        return max(0, remain)

    async def set_premium(self, uid, days):
        async with self.pool.acquire() as conn:
            until = None if days == 0 else int(time.time()) + days * 86400
            await conn.execute("""
                INSERT INTO users (user_id, premium_until, last_date)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    premium_until = EXCLUDED.premium_until,
                    last_date = EXCLUDED.last_date
            """, uid, until, str(date.today()))

    async def add_referral(self, new_uid, ref_id):
        if ref_id == 0 or ref_id == new_uid:
            return False
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, last_date) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                new_uid, str(date.today())
            )
            row = await conn.fetchrow("SELECT referrer_id FROM users WHERE user_id=$1", new_uid)
            if row and row[0] == 0:
                await conn.execute("UPDATE users SET referrer_id=$1 WHERE user_id=$2", ref_id, new_uid)
                await conn.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=$1", ref_id)
                return True
        return False

    def get_referral_link(self, uid):
        return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

    async def save_username(self, uid, username):
        if not (5 <= len(username) <= 32) or username.isdigit() or not all(c in string.ascii_letters + string.digits + '_' for c in username):
            return False
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO saved_usernames (user_id, username, created_at) VALUES ($1, $2, $3)",
                uid, username, int(time.time())
            )
        return True

    async def get_saved_usernames(self, uid):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT username, created_at FROM saved_usernames WHERE user_id=$1 ORDER BY created_at DESC",
                uid
            )

    async def create_payment(self, uid, method, tariff, amount, days):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO payments (user_id, method, tariff, amount, days, created_at)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
            """, uid, method, tariff, amount, days, int(time.time()))

    async def get_payment(self, pid):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM payments WHERE id=$1", pid)

    async def confirm_payment(self, pid):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status='confirmed' WHERE id=$1", pid)

    async def reject_payment(self, pid):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status='cancelled' WHERE id=$1", pid)

    async def get_waiting_payments(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, user_id, method, tariff, amount, days, created_at FROM payments WHERE status='waiting' ORDER BY created_at DESC"
            )

    async def save_support_message(self, uid, text):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO support_messages (user_id, message, created_at) VALUES ($1, $2, $3)",
                uid, text[:1000], int(time.time())
            )

    async def get_unread_support_messages(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, user_id, message, created_at FROM support_messages WHERE is_read=FALSE ORDER BY created_at DESC"
            )

    async def mark_support_read(self, msg_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE support_messages SET is_read=TRUE WHERE id=$1", msg_id)

db = Database(DATABASE_URL)

# ================= ГЛОБАЛЬНАЯ HTTP СЕССИЯ =================
http_session = None

async def get_http_session():
    global http_session
    if http_session is None:
        http_session = ClientSession()
    return http_session

# ================= ЗАЩИТА ОТ ФЛУДА / RATE LIMIT =================
class RateLimiter:
    def __init__(self, max_actions, period, ban_seconds=300):
        self.max_actions = max_actions
        self.period = period
        self.ban_seconds = ban_seconds
        self.records = {}
        self.ban = {}

    async def check(self, user_id):
        now = time.time()
        if user_id in self.ban and self.ban[user_id] > now:
            return False
        if user_id not in self.records:
            self.records[user_id] = deque()
        q = self.records[user_id]
        while q and q[0] < now - self.period:
            q.popleft()
        if len(q) >= self.max_actions:
            self.ban[user_id] = now + self.ban_seconds
            return False
        q.append(now)
        return True

global_limiter = RateLimiter(30, 60, 300)
generate_semaphore = asyncio.Semaphore(5)

# ================= ОЧЕРЕДЬ ГЕНЕРАЦИИ =================
gen_queue = asyncio.Queue(maxsize=100)

# Кэш с очисткой (простая, без периодической чистки, TTL управляет)
_username_cache = {}          # key -> (free, timestamp)
_sub_cache = {}               # user_id -> (subscribed, timestamp)

async def worker():
    while True:
        task = await gen_queue.get()
        uid, length, mixed, future = task
        username = None
        for _ in range(20):
            if mixed:
                username = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))
            else:
                username = ''.join(random.choice(string.ascii_lowercase) for _ in range(length))
            if await is_username_free_fast(username):
                break
            else:
                username = None
            await asyncio.sleep(0.1)
        future.set_result(username)
        gen_queue.task_done()

asyncio.create_task(worker())

async def is_username_free_fast(username):
    now = time.time()
    if username in _username_cache:
        free, ts = _username_cache[username]
        if now - ts < 300:   # TTL 5 минут
            return free
    try:
        session = await get_http_session()
        async with session.head(f"https://t.me/{username}", timeout=5) as resp:
            free = (resp.status == 404)
    except:
        free = False
    _username_cache[username] = (free, now)
    return free

async def generate_username(uid, length, mixed=False):
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    await gen_queue.put((uid, length, mixed, fut))
    return await fut

async def is_subscribed(uid):
    if not CHECK_SUBSCRIPTION:
        return True
    now = time.time()
    if uid in _sub_cache and now - _sub_cache[uid][1] < 600:
        return _sub_cache[uid][0]
    try:
        # CHANNEL_USERNAME должен начинаться с @
        member = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        res = member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Sub check failed for {uid}: {e}")
        res = False
    _sub_cache[uid] = (res, now)
    return res

def sub_required(func):
    async def wrapper(message):
        if not await is_subscribed(message.from_user.id):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
            await bot.send_message(message.chat.id, f"⚠️ Подпишитесь на канал: {CHANNEL_USERNAME}", reply_markup=markup)
            return
        return await func(message)
    return wrapper

def sub_required_cb(func):
    async def wrapper(call):
        if not await is_subscribed(call.from_user.id):
            await bot.answer_callback_query(call.id, "❌ Подпишитесь на канал!", show_alert=True)
            return
        return await func(call)
    return wrapper

async def safe_edit(chat_id, msg_id, text, markup=None):
    try:
        await bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Edit error: {e}")

async def get_profile_text(uid):
    p, s, _, until, refs = await db.get_user(uid)
    remain = await db.get_remaining(uid)
    remain_str = f"{remain}" if remain != float('inf') else "∞"
    status = "👑 Premium" if p else "🔷 Обычный"
    if until is None:
        expiry = "Навсегда"
    elif p:
        expiry = f"до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}"
    else:
        expiry = "Не активен"
    link = db.get_referral_link(uid)
    max_s = await db.get_max_searches(uid)
    return (
        f"╔ 🔷 Статус: {status}\n"
        f"╠ 🔷 Попытки сегодня: {remain_str}\n"
        f"╠ 🔷 Дневной лимит: {'∞' if p else max_s}\n"
        f"╠ 🔷 Приглашено друзей: {refs}\n"
        f"╠ 🔷 Реф. ссылка: {link}\n"
        f"╠ 🔷 Premium: {expiry}\n"
        f"╚ 🔷 Сохранённые: /my_usernames"
    )

# ================= ХЕНДЛЕРЫ =================
@bot.message_handler(commands=['start'])
async def start_cmd(message):
    if not await global_limiter.check(message.from_user.id):
        await bot.send_message(message.chat.id, "⚠️ Слишком много запросов. Бан на 5 минут.")
        return
    if len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg.split("_")[1])
                await db.add_referral(message.from_user.id, ref_id)
            except:
                pass
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔍 Найти юзернейм", callback_data="menu_len"),
        InlineKeyboardButton("💎 Купить Premium", callback_data="pay_menu"),
        InlineKeyboardButton("👤 Личный кабинет", callback_data="profile"),
        InlineKeyboardButton("🆘 Поддержка", callback_data="support_start")
    )
    if message.from_user.id == ADMIN_ID:
        kb.add(InlineKeyboardButton("🔧 Админ панель", callback_data="admin_panel"))
    await bot.send_message(
        message.chat.id,
        "🔷 Приветствуем в Auron Search!\n\nПрофессиональный поиск свободных username.\nВыберите действие:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: True)
@sub_required_cb
async def handle_callback(call):
    uid = call.from_user.id
    data = call.data

    if not await global_limiter.check(uid):
        await bot.answer_callback_query(call.id, "⚠️ Слишком много запросов. Бан на 5 минут.", show_alert=True)
        return

    if data == "menu_len":
        rem = await db.get_remaining(uid)
        txt = "Выберите длину:" if rem == float('inf') else f"🎫 Осталось {rem} попыток сегодня."
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("5 (Premium)", callback_data="len_5"),
            InlineKeyboardButton("6", callback_data="len_6"),
            InlineKeyboardButton("7", callback_data="len_7")
        )
        kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_main"))
        await safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "back_main":
        await start_cmd(call.message)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("len_"):
        l = int(data.split("_")[1])
        p, _, _, _, _ = await db.get_user(uid)
        if l == 5 and not p:
            await bot.answer_callback_query(call.id)
            await bot.send_message(call.message.chat.id, "❌ Длина 5 букв доступна только Premium!\n💎 Купите Premium.")
            return
        if l == 7:
            if not await db.can_search(uid):
                await bot.answer_callback_query(call.id, "Лимит исчерпан!", show_alert=True)
                return
            username = await generate_username(uid, 7, mixed=False)
            if not username:
                await bot.answer_callback_query(call.id, "Не удалось найти свободный username", show_alert=True)
                return
            await db.inc_search(uid)
            rem = await db.get_remaining(uid)
            extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
            kb = InlineKeyboardMarkup()
            kb.row(
                InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
                InlineKeyboardButton("🔄 Ещё", callback_data="gen_7")
            )
            kb.row(InlineKeyboardButton("◀ Назад", callback_data="menu_len"))
            await safe_edit(call.message.chat.id, call.message.message_id, f"✅ @{username}{extra}", reply_markup=kb)
            await bot.answer_callback_query(call.id)
            return
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("🔤 Без цифр", callback_data=f"mode_{l}_0"),
            InlineKeyboardButton("🔢 С цифрой", callback_data=f"mode_{l}_1")
        )
        kb.row(InlineKeyboardButton("◀ Назад", callback_data="menu_len"))
        await safe_edit(call.message.chat.id, call.message.message_id, f"Длина: {l}\nВыберите режим:", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("mode_"):
        parts = data.split("_")
        l = int(parts[1])
        mixed = (parts[2] == "1")
        if not await db.can_search(uid):
            await bot.answer_callback_query(call.id, "Лимит исчерпан!", show_alert=True)
            return
        username = await generate_username(uid, l, mixed=mixed)
        if not username:
            await bot.answer_callback_query(call.id, "Не удалось найти свободный username", show_alert=True)
            return
        await db.inc_search(uid)
        rem = await db.get_remaining(uid)
        extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
            InlineKeyboardButton("🔄 Ещё", callback_data=f"mode_{l}_{mixed}")
        )
        kb.row(InlineKeyboardButton("◀ Назад", callback_data="menu_len"))
        await safe_edit(call.message.chat.id, call.message.message_id, f"✅ @{username}{extra}", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "gen_7":
        if not await db.can_search(uid):
            await bot.answer_callback_query(call.id, "Лимит исчерпан!", show_alert=True)
            return
        username = await generate_username(uid, 7, mixed=False)
        if not username:
            await bot.answer_callback_query(call.id, "Не удалось найти свободный username", show_alert=True)
            return
        await db.inc_search(uid)
        rem = await db.get_remaining(uid)
        extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
            InlineKeyboardButton("🔄 Ещё", callback_data="gen_7")
        )
        kb.row(InlineKeyboardButton("◀ Назад", callback_data="menu_len"))
        await safe_edit(call.message.chat.id, call.message.message_id, f"✅ @{username}{extra}", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("save_"):
        username = data[5:]
        if await db.save_username(uid, username):
            await bot.answer_callback_query(call.id, f"✅ @{username} сохранён!")
        else:
            await bot.answer_callback_query(call.id, "❌ Недопустимый username (5-32, a-z0-9_)", show_alert=True)
        return

    if data == "profile":
        text = await get_profile_text(uid)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("📋 Сохранённые", callback_data="my_usernames"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_main"))
        await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "my_usernames":
        rows = await db.get_saved_usernames(uid)
        if not rows:
            txt = "📭 Нет сохранённых."
        else:
            txt = "💾 Сохранённые:\n\n" + "\n".join(f"• @{r['username']} — {datetime.fromtimestamp(r['created_at']).strftime('%d.%m.%Y %H:%M')}" for r in rows[:20])
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="profile"))
        await safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "support_start":
        await safe_edit(call.message.chat.id, call.message.message_id,
                        "📝 Напишите ваше сообщение администратору.\n\nЯ перешлю его.\n(Команды не отправляются)")
        support_await[uid] = call.message.chat.id
        await bot.answer_callback_query(call.id)
        return

    if data == "pay_menu":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars"),
            InlineKeyboardButton("🪙 USDT (TRC20)", callback_data="pay_usdt")
        )
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_main"))
        await safe_edit(call.message.chat.id, call.message.message_id, "🌟 Способ оплаты Premium:", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "pay_stars":
        kb = InlineKeyboardMarkup(row_width=2)
        for t, stars in STARS_PRICES.items():
            days = STARS_DAYS[t]
            txt = f"{days} дней" if days > 0 else "Навсегда"
            kb.add(InlineKeyboardButton(f"{txt} – {stars} ★", callback_data=f"star_{t}_{stars}"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="pay_menu"))
        await safe_edit(call.message.chat.id, call.message.message_id, "⭐ Выберите тариф:", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("star_"):
        _, tariff, stars = data.split("_")
        stars = int(stars)
        days = STARS_DAYS[tariff]
        desc = "навсегда" if days == 0 else f"{days} дней"
        try:
            await bot.send_invoice(
                chat_id=call.message.chat.id,
                title="Auron Premium",
                description=f"Premium на {desc}",
                invoice_payload=f"stars_{days}_{stars}_{uid}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Premium", amount=stars)],
                start_parameter="auron_premium"
            )
        except Exception as e:
            logger.error(f"Stars invoice error: {e}")
            await bot.answer_callback_query(call.id, "Ошибка создания счёта", show_alert=True)
        else:
            await bot.answer_callback_query(call.id)
        return

    if data == "pay_usdt":
        kb = InlineKeyboardMarkup(row_width=2)
        for t, price in USDT_PRICES.items():
            days = USDT_DAYS[t]
            txt = f"{days} дней" if days > 0 else "Навсегда"
            kb.add(InlineKeyboardButton(f"{txt} – {price} USDT", callback_data=f"usdt_{t}"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="pay_menu"))
        await safe_edit(call.message.chat.id, call.message.message_id, "🪙 Выберите тариф USDT:", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("usdt_") and data.count("_") == 1:
        tariff = data.split("_")[1]
        if tariff not in USDT_PRICES:
            await bot.answer_callback_query(call.id, "Неизвестный тариф", show_alert=True)
            return
        if not await global_limiter.check(uid):
            await bot.answer_callback_query(call.id, "⚠️ Создавать заявку можно не чаще 1 раза в 10 минут.", show_alert=True)
            return
        amount = USDT_PRICES[tariff]
        days = USDT_DAYS[tariff]
        pid = await db.create_payment(uid, "usdt", tariff, amount, days)
        text = (f"💸 Счёт создан!\nСумма: {amount} USDT\nТариф: {days if days>0 else 'навсегда'} дней\n\n"
                f"Оплата:\n1. Переведите {amount} USDT (TRC20) на кошелёк: {USDT_WALLET}\n"
                f"2. Нажмите «✅ Я оплатил»\n3. Админ проверит платёж.\n\nID заявки: {pid}")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_usdt_{pid}"),
               InlineKeyboardButton("◀ Назад", callback_data="pay_menu"))
        await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("confirm_usdt_"):
        pid = int(data.split("_")[2])
        payment = await db.get_payment(pid)
        if not payment or payment['status'] != 'waiting':
            await bot.answer_callback_query(call.id, "❌ Заявка не найдена или уже обработана", show_alert=True)
            return
        admin_text = (f"🆕 ОПЛАТА USDT\n"
                      f"Пользователь: {payment['user_id']}\n"
                      f"Тариф: {payment['days'] if payment['days']>0 else 'навсегда'} дней\n"
                      f"Сумма: {payment['amount']} USDT\n"
                      f"ID заявки: {pid}")
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_approve_usdt_{pid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_usdt_{pid}")
        )
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
        await bot.answer_callback_query(call.id, "✅ Заявка отправлена администратору.")
        await safe_edit(call.message.chat.id, call.message.message_id, "✅ Заявка отправлена администратору. Ожидайте.")
        return

    if data == "admin_panel":
        if uid != ADMIN_ID:
            await bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("📩 Сообщения поддержки", callback_data="admin_support"),
            InlineKeyboardButton("💸 USDT заявки", callback_data="admin_usdt_list")
        )
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_main"))
        await safe_edit(call.message.chat.id, call.message.message_id, "🔧 Админ панель", reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "admin_support":
        if uid != ADMIN_ID:
            return
        msgs = await db.get_unread_support_messages()
        if not msgs:
            await safe_edit(call.message.chat.id, call.message.message_id, "📭 Нет новых сообщений.")
            await bot.answer_callback_query(call.id)
            return
        msg = msgs[0]
        text = (f"📩 От {msg['user_id']}\n🕒 {datetime.fromtimestamp(msg['created_at']).strftime('%d.%m.%Y %H:%M')}\n\n{msg['message'][:500]}")
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Прочитано", callback_data=f"read_support_{msg['id']}"),
            InlineKeyboardButton("➡️ Далее", callback_data="admin_support_next")
        )
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
        await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "admin_support_next":
        if uid != ADMIN_ID:
            return
        msgs = await db.get_unread_support_messages()
        if not msgs:
            await safe_edit(call.message.chat.id, call.message.message_id, "📭 Нет новых сообщений.")
            await bot.answer_callback_query(call.id)
            return
        msg = msgs[0]
        text = (f"📩 От {msg['user_id']}\n🕒 {datetime.fromtimestamp(msg['created_at']).strftime('%d.%m.%Y %H:%M')}\n\n{msg['message'][:500]}")
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Прочитано", callback_data=f"read_support_{msg['id']}"),
            InlineKeyboardButton("➡️ Далее", callback_data="admin_support_next")
        )
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
        await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("read_support_"):
        if uid != ADMIN_ID:
            return
        msg_id = int(data.split("_")[2])
        await db.mark_support_read(msg_id)
        await bot.answer_callback_query(call.id, "✅ Помечено прочитанным")
        msgs = await db.get_unread_support_messages()
        if not msgs:
            await safe_edit(call.message.chat.id, call.message.message_id, "📭 Нет новых сообщений.")
        else:
            msg = msgs[0]
            text = (f"📩 От {msg['user_id']}\n🕒 {datetime.fromtimestamp(msg['created_at']).strftime('%d.%m.%Y %H:%M')}\n\n{msg['message'][:500]}")
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("✅ Прочитано", callback_data=f"read_support_{msg['id']}"),
                InlineKeyboardButton("➡️ Далее", callback_data="admin_support_next")
            )
            kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
            await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        return

    if data == "admin_usdt_list":
        if uid != ADMIN_ID:
            return
        payments = await db.get_waiting_payments()
        if not payments:
            await safe_edit(call.message.chat.id, call.message.message_id, "💰 Нет ожидающих заявок USDT.")
            await bot.answer_callback_query(call.id)
            return
        p = payments[0]
        text = (f"💰 Заявка #{p['id']}\n👤 {p['user_id']}\n"
                f"📅 {datetime.fromtimestamp(p['created_at']).strftime('%d.%m.%Y %H:%M')}\n"
                f"💵 {p['amount']} USDT\n📆 Тариф: {p['days'] if p['days']>0 else 'навсегда'}")
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_approve_usdt_{p['id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_usdt_{p['id']}")
        )
        if len(payments) > 1:
            kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
        await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data == "admin_usdt_next":
        if uid != ADMIN_ID:
            return
        payments = await db.get_waiting_payments()
        if len(payments) <= 1:
            await safe_edit(call.message.chat.id, call.message.message_id, "💰 Нет других заявок.")
            await bot.answer_callback_query(call.id)
            return
        p = payments[1]
        text = (f"💰 Заявка #{p['id']}\n👤 {p['user_id']}\n"
                f"📅 {datetime.fromtimestamp(p['created_at']).strftime('%d.%m.%Y %H:%M')}\n"
                f"💵 {p['amount']} USDT\n📆 Тариф: {p['days'] if p['days']>0 else 'навсегда'}")
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_approve_usdt_{p['id']}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_usdt_{p['id']}")
        )
        if len(payments) > 2:
            kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
        await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        await bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_approve_usdt_"):
        if uid != ADMIN_ID:
            return
        pid = int(data.split("_")[3])
        payment = await db.get_payment(pid)
        if not payment or payment['status'] != 'waiting':
            await bot.answer_callback_query(call.id, "Заявка уже обработана")
            return
        await db.set_premium(payment['user_id'], payment['days'])
        await db.confirm_payment(pid)
        await bot.send_message(payment['user_id'], f"🎉 Premium активирован! {'Навсегда' if payment['days']==0 else f'На {payment['days']} дней.'}")
        await bot.answer_callback_query(call.id, "✅ Premium выдан")
        # Обновить текущее сообщение
        payments = await db.get_waiting_payments()
        if not payments:
            await safe_edit(call.message.chat.id, call.message.message_id, "💰 Нет ожидающих заявок.")
        else:
            p = payments[0]
            text = (f"💰 Заявка #{p['id']}\n👤 {p['user_id']}\n"
                    f"📅 {datetime.fromtimestamp(p['created_at']).strftime('%d.%m.%Y %H:%M')}\n"
                    f"💵 {p['amount']} USDT\n📆 Тариф: {p['days'] if p['days']>0 else 'навсегда'}")
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_approve_usdt_{p['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_usdt_{p['id']}")
            )
            if len(payments) > 1:
                kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
            kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
            await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        return

    if data.startswith("admin_reject_usdt_"):
        if uid != ADMIN_ID:
            return
        pid = int(data.split("_")[3])
        payment = await db.get_payment(pid)
        if not payment or payment['status'] != 'waiting':
            await bot.answer_callback_query(call.id, "Заявка уже обработана")
            return
        await db.reject_payment(pid)
        await bot.send_message(payment['user_id'], "❌ Ваша заявка на оплату USDT отклонена.")
        await bot.answer_callback_query(call.id, "❌ Заявка отклонена")
        payments = await db.get_waiting_payments()
        if not payments:
            await safe_edit(call.message.chat.id, call.message.message_id, "💰 Нет ожидающих заявок.")
        else:
            p = payments[0]
            text = (f"💰 Заявка #{p['id']}\n👤 {p['user_id']}\n"
                    f"📅 {datetime.fromtimestamp(p['created_at']).strftime('%d.%m.%Y %H:%M')}\n"
                    f"💵 {p['amount']} USDT\n📆 Тариф: {p['days'] if p['days']>0 else 'навсегда'}")
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_approve_usdt_{p['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_usdt_{p['id']}")
            )
            if len(payments) > 1:
                kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
            kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_panel"))
            await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        return

    await bot.answer_callback_query(call.id, "Неизвестная команда")

# ================= ПОДДЕРЖКА: ПОЛУЧЕНИЕ СООБЩЕНИЯ ОТ ПОЛЬЗОВАТЕЛЯ =================
support_await = {}

@bot.message_handler(func=lambda m: True)
async def handle_support_message(message):
    uid = message.from_user.id
    if uid in support_await:
        chat_id = support_await.pop(uid)
        if message.text.startswith('/'):
            return
        await db.save_support_message(uid, message.text)
        await bot.send_message(ADMIN_ID, f"🆘 Новое сообщение!\n👤 {message.from_user.first_name} (ID: {uid})\n💬 {message.text[:500]}")
        await bot.send_message(chat_id, "✅ Сообщение отправлено администратору.")
    # остальные сообщения игнорируются

# ================= ПЛАТЕЖИ STARS =================
@bot.pre_checkout_query_handler(func=lambda q: True)
async def pre_checkout_hook(query):
    try:
        await bot.answer_pre_checkout_query(query.id, ok=True)
    except Exception as e:
        logger.error(f"Pre-checkout error: {e}")
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="Ошибка платежа")

@bot.message_handler(content_types=['successful_payment'])
async def on_successful_payment(message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split("_")
    if len(parts) >= 3 and parts[0] == "stars":
        days = int(parts[1])
        await db.set_premium(message.from_user.id, days)
        desc = "навсегда" if days == 0 else f"{days} дней"
        await bot.send_message(message.chat.id, f"✅ Premium активирован на {desc}!")
        await start_cmd(message)

# ================= АДМИН-КОМАНДЫ =================
@bot.message_handler(commands=['premium'])
async def give_premium(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, "/premium <user_id> [дни] (0 = навсегда)")
        return
    try:
        uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 0
        await db.set_premium(uid, days)
        await bot.reply_to(message, f"✅ Premium выдан {uid} на {'навсегда' if days==0 else f'{days} дней'}.")
    except Exception as e:
        await bot.reply_to(message, f"Ошибка: {e}")

@bot.message_handler(commands=['unpremium'])
async def remove_premium(message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, "/unpremium <user_id>")
        return
    try:
        uid = int(args[1])
        await db.set_premium(uid, 0)
        await bot.reply_to(message, f"✅ Premium снят с {uid}.")
    except Exception as e:
        await bot.reply_to(message, f"Ошибка: {e}")

# ================= ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECK =================
async def health(request):
    return web.Response(text="OK")
web_app = web.Application()
web_app.router.add_get('/', health)
runner = web.AppRunner(web_app)

# ================= SHUTDOWN =================
async def shutdown():
    logger.info("Shutting down gracefully...")
    if runner:
        await runner.cleanup()
    global http_session
    if http_session:
        await http_session.close()
    if db.pool:
        await db.pool.close()
    # В AsyncTeleBot нет close_session, просто завершаем
    loop = asyncio.get_event_loop()
    loop.stop()

def signal_handler():
    asyncio.get_event_loop().create_task(shutdown())

# ================= ЗАПУСК =================
async def main():
    await db.init()
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("Bot started")
    await bot.infinity_polling(timeout=30, allowed_updates=["message", "callback_query", "pre_checkout_query"])

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
