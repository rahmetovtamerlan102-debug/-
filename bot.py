#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import asyncpg
import os
import sys
import time
import random
import string
import logging
import re
from functools import wraps
from html import escape
from datetime import date, datetime
from collections import deque
from aiohttp import web

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# ------------------------- CONFIG -------------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ BOT_TOKEN не задан")
    sys.exit(1)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL не задан")
    sys.exit(1)

PORT = int(os.getenv("PORT", "8080"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@Auron_search")
if not CHANNEL_USERNAME.startswith("@"):
    CHANNEL_USERNAME = "@" + CHANNEL_USERNAME
BOT_USERNAME = os.getenv("BOT_USERNAME", "Auronsearchesbot")
CHECK_SUBSCRIPTION = os.getenv("CHECK_SUBSCRIPTION", "True").lower() == "true"
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
MAX_REF_BONUS = 20
FOREVER_PREMIUM = 9999999999

USDT_WALLET = os.getenv("USDT_WALLET", "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV")
USDT_PRICES = {"7d": 3, "30d": 6, "forever": 25}
USDT_DAYS = {"7d": 7, "30d": 30, "forever": 0}
STARS_PRICES = {"7d": 60, "30d": 240, "forever": 800}
STARS_DAYS = {"7d": 7, "30d": 30, "forever": 0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ------------------------- SAFE DECORATORS -------------------------
def safe_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Handler error in {func.__name__}: {e}", exc_info=True)
            for arg in args:
                if hasattr(arg, 'answer'):
                    try:
                        await arg.answer("⚠️ Произошла ошибка. Попробуйте позже.", show_alert=True)
                    except:
                        pass
                elif hasattr(arg, 'message') and hasattr(arg.message, 'chat'):
                    try:
                        await bot.send_message(arg.message.chat.id, "⚠️ Произошла ошибка. Попробуйте позже.")
                    except:
                        pass
            return
    return wrapper

def sub_required_msg(func):
    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if not CHECK_SUBSCRIPTION:
            return await func(message, *args, **kwargs)
        try:
            member = await bot.get_chat_member(CHANNEL_USERNAME, message.from_user.id)
            if member.status not in ("member", "administrator", "creator"):
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")]
                ])
                await message.answer(f"⚠️ Подпишитесь на канал: {CHANNEL_USERNAME}", reply_markup=markup)
                return
        except Exception as e:
            logger.warning(f"Subscription check failed: {e}")
        return await func(message, *args, **kwargs)
    return wrapper

def sub_required_cb(func):
    @wraps(func)
    async def wrapper(call: types.CallbackQuery, *args, **kwargs):
        if not CHECK_SUBSCRIPTION:
            return await func(call, *args, **kwargs)
        try:
            member = await bot.get_chat_member(CHANNEL_USERNAME, call.from_user.id)
            if member.status not in ("member", "administrator", "creator"):
                await call.answer("❌ Подпишитесь на канал!", show_alert=True)
                return
        except Exception as e:
            logger.warning(f"Subscription check failed: {e}")
        return await func(call, *args, **kwargs)
    return wrapper

# ------------------------- FSM STATES -------------------------
class SupportStates(StatesGroup):
    waiting_for_message = State()

class PaymentStates(StatesGroup):
    waiting_for_txid = State()

# ------------------------- DATABASE -------------------------
class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self.pool = None

    async def init(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=3)  # Исправлено для Render
        async with self.pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                premium_until BIGINT,
                searches_today INT DEFAULT 0,
                last_date DATE,
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_usernames ON saved_usernames(user_id, username);
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                method TEXT,
                tariff TEXT,
                amount FLOAT,
                days INT,
                txid TEXT,
                created_at BIGINT,
                status TEXT DEFAULT 'waiting'
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                message TEXT,
                created_at BIGINT,
                is_read BOOLEAN DEFAULT FALSE
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users_referral_cooldown (
                user_id BIGINT,
                ref_id BIGINT,
                created_at BIGINT
            );
            """)
        logger.info("Database ready")

    async def get_user(self, uid):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT premium_until, searches_today, last_date, referals_count, referrer_id FROM users WHERE user_id=$1",
                uid
            )
            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id, last_date, referals_count, premium_until, referrer_id) VALUES ($1, $2, 0, 0, 0)",
                    uid, date.today()
                )
                return False, 0, date.today(), 0, 0, 0
            premium_until = row["premium_until"] or 0
            searches = row["searches_today"] or 0
            refs = row["referals_count"] or 0
            referrer = row["referrer_id"] or 0
            last = row["last_date"] or date.today()
            is_premium = premium_until == FOREVER_PREMIUM or premium_until > int(time.time())
            today = date.today()
            if last != today:
                searches = 0
                await conn.execute("UPDATE users SET searches_today=0, last_date=$1 WHERE user_id=$2", today, uid)
            return is_premium, searches, last, premium_until, refs, referrer

    async def get_max_searches(self, uid):
        p, _, _, _, refs, _ = await self.get_user(uid)
        return float('inf') if p else FREE_LIMIT + min(refs, MAX_REF_BONUS)

    async def can_search(self, uid):
        p, s, _, _, _, _ = await self.get_user(uid)
        return p or s < await self.get_max_searches(uid)

    async def inc_search(self, uid):
        p, s, _, _, _, _ = await self.get_user(uid)
        if not p:
            async with self.pool.acquire() as conn:
                await conn.execute("UPDATE users SET searches_today = searches_today + 1 WHERE user_id=$1", uid)

    async def get_remaining(self, uid):
        p, s, _, _, _, _ = await self.get_user(uid)
        if p:
            return float('inf')
        return max(0, await self.get_max_searches(uid) - s)

    async def set_premium(self, uid, days):
        async with self.pool.acquire() as conn:
            until = FOREVER_PREMIUM if days == 0 else int(time.time()) + days * 86400
            await conn.execute(
                "INSERT INTO users (user_id, premium_until, last_date) VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id) DO UPDATE SET premium_until = EXCLUDED.premium_until, last_date = EXCLUDED.last_date",
                uid, until, date.today()
            )

    async def add_referral(self, new_uid, ref_id):
        if ref_id == 0 or ref_id == new_uid:
            return False
        async with self.pool.acquire() as conn:
            last_ref = await conn.fetchval(
                "SELECT created_at FROM users_referral_cooldown WHERE user_id=$1 AND ref_id=$2",
                new_uid, ref_id
            )
            if last_ref and int(time.time()) - last_ref < 86400:
                return False
            row = await conn.fetchrow("SELECT referrer_id FROM users WHERE user_id=$1", new_uid)
            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id, last_date, referrer_id, referals_count, premium_until) VALUES ($1, $2, $3, 0, 0)",
                    new_uid, date.today(), ref_id
                )
                await conn.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=$1", ref_id)
                await conn.execute(
                    "INSERT INTO users_referral_cooldown (user_id, ref_id, created_at) VALUES ($1, $2, $3)",
                    new_uid, ref_id, int(time.time())
                )
                return True
            elif row["referrer_id"] == 0:
                await conn.execute("UPDATE users SET referrer_id=$1 WHERE user_id=$2", ref_id, new_uid)
                await conn.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=$1", ref_id)
                await conn.execute(
                    "INSERT INTO users_referral_cooldown (user_id, ref_id, created_at) VALUES ($1, $2, $3)",
                    new_uid, ref_id, int(time.time())
                )
                return True
        return False

    def get_referral_link(self, uid):
        return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

    async def save_username(self, uid, username):
        if not re.match(r"^[a-zA-Z0-9_]{5,32}$", username):
            return False
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO saved_usernames (user_id, username, created_at) VALUES ($1, $2, $3)",
                    uid, username, int(time.time())
                )
                return True
            except asyncpg.UniqueViolationError:
                return False

    async def get_saved_usernames(self, uid):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT username, created_at FROM saved_usernames WHERE user_id=$1 ORDER BY created_at DESC",
                uid
            )

    async def create_payment(self, uid, method, tariff, amount, days, txid=None):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO payments (user_id, method, tariff, amount, days, txid, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
            """, uid, method, tariff, amount, days, txid, int(time.time()))

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
                "SELECT id, user_id, method, tariff, amount, days, txid, created_at FROM payments WHERE status='waiting' ORDER BY created_at DESC"
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

# ------------------------- RATE LIMITER -------------------------
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
        if user_id in self.ban:
            del self.ban[user_id]
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
callback_limiter = RateLimiter(10, 5, 30)
support_limiter = RateLimiter(3, 3600, 3600)

# ------------------------- GENERATOR -------------------------
gen_queue = asyncio.Queue(maxsize=200)

def gen_letters(n):
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(n))

def gen_mixed(n):
    pos = random.randint(0, n-1)
    return ''.join(str(random.randint(0,9)) if i == pos else random.choice(string.ascii_lowercase) for i in range(n))

async def worker(worker_id):
    while True:
        uid, length, mixed, future = await gen_queue.get()
        try:
            username = gen_mixed(length) if mixed else gen_letters(length)
            if not future.done():
                future.set_result(username)
        except Exception as e:
            logger.error(f"Worker {worker_id} error: {e}")
            if not future.done():
                future.set_result(None)
        finally:
            gen_queue.task_done()

async def generate_username(uid, length, mixed=False):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    try:
        await asyncio.wait_for(gen_queue.put((uid, length, mixed, fut)), timeout=2)
    except asyncio.TimeoutError:
        return None
    try:
        return await asyncio.wait_for(fut, timeout=10)
    except asyncio.TimeoutError:
        return None

# ------------------------- HELPERS -------------------------
async def safe_edit(chat_id, msg_id, text, markup=None):
    try:
        await bot.edit_message_text(text, chat_id, msg_id, reply_markup=markup)
    except Exception as e:
        logger.warning(f"Edit failed: {e}")

async def get_profile_text(uid):
    p, s, _, until, refs, _ = await db.get_user(uid)
    remain = await db.get_remaining(uid)
    max_s = await db.get_max_searches(uid)
    remain_str = "∞" if remain == float('inf') else str(remain)
    max_str = "∞" if max_s == float('inf') else str(max_s)
    status = "👑 Premium" if p else "🔷 Обычный"
    if until == FOREVER_PREMIUM:
        expiry = "Навсегда"
    elif p:
        expiry = f"до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}"
    else:
        expiry = "Не активен"
    link = db.get_referral_link(uid)
    return (
        f"╔ 🔷 Статус: {status}\n"
        f"╠ 🔷 Попытки сегодня: {remain_str}\n"
        f"╠ 🔷 Дневной лимит: {max_str}\n"
        f"╠ 🔷 Приглашено друзей: {refs}\n"
        f"╠ 🔷 Реф. ссылка: {link}\n"
        f"╠ 🔷 Premium: {expiry}\n"
        f"╚ 🔷 Сохранённые: /my_usernames"
    )

def get_main_keyboard(uid):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти юзернейм", callback_data="menu_len")],
        [InlineKeyboardButton(text="💎 Купить Premium", callback_data="pay_menu")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support_start")]
    ])
    if uid == ADMIN_ID:
        kb.inline_keyboard.append([InlineKeyboardButton(text="🔧 Админ панель", callback_data="admin_panel")])
    return kb

async def send_main_menu(chat_id, uid):
    await bot.send_message(
        chat_id,
        "🔷 Приветствуем в Auron Search!\n\nПрофессиональный поиск свободных username.\nВыберите действие:",
        reply_markup=get_main_keyboard(uid)
    )

# ------------------------- HANDLERS -------------------------
@dp.message(Command("start"))
@safe_handler
async def cmd_start(message: types.Message):
    if not await global_limiter.check(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Бан на 5 минут.")
        return
    if message.text and len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg.split("_")[1])
                await db.add_referral(message.from_user.id, ref_id)
            except:
                pass
    await send_main_menu(message.chat.id, message.from_user.id)

@dp.message(Command("premium"))
@safe_handler
async def cmd_premium(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Эта команда только для администратора.")
        return
    await db.set_premium(message.from_user.id, 0)
    await message.answer("✅ Premium активирован для вас навсегда!")

@dp.message(Command("my_usernames"))
@sub_required_msg
@safe_handler
async def show_saved(message: types.Message):
    rows = await db.get_saved_usernames(message.from_user.id)
    if not rows:
        await message.answer("📭 Нет сохранённых.")
        return
    text = "💾 Сохранённые:\n\n" + "\n".join(
        f"• @{r['username']} — {datetime.fromtimestamp(r['created_at']).strftime('%d.%m.%Y %H:%M')}" for r in rows[:20]
    )
    await message.answer(text)

@dp.callback_query(F.data == "menu_len")
@sub_required_cb
@safe_handler
async def menu_len(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    rem = await db.get_remaining(call.from_user.id)
    txt = "Выберите длину:" if rem == float('inf') else f"🎫 Осталось {rem} попыток сегодня."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 (Premium)", callback_data="len_5"),
         InlineKeyboardButton(text="6", callback_data="len_6"),
         InlineKeyboardButton(text="7", callback_data="len_7")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "back_main")
@safe_handler
async def back_main(call: types.CallbackQuery):
    await call.message.delete()
    await send_main_menu(call.message.chat.id, call.from_user.id)
    await call.answer()

@dp.callback_query(F.data.startswith("len_"))
@sub_required_cb
@safe_handler
async def len_chosen(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    uid = call.from_user.id
    length = int(call.data.split("_")[1])
    p, _, _, _, _, _ = await db.get_user(uid)
    if length == 5 and not p:
        await call.message.answer("❌ Длина 5 букв доступна только Premium!\n💎 Купите Premium.")
        await call.answer()
        return
    if length == 7:
        if not await db.can_search(uid):
            await call.answer("Лимит исчерпан!", show_alert=True)
            return
        username = await generate_username(uid, 7, False)
        if not username:
            await call.answer("Ошибка генерации", show_alert=True)
            return
        await db.inc_search(uid)
        rem = await db.get_remaining(uid)
        extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{username}"),
             InlineKeyboardButton(text="🔄 Ещё", callback_data="gen_7")],
            [InlineKeyboardButton(text="◀ Назад", callback_data="menu_len")]
        ])
        await safe_edit(call.message.chat.id, call.message.message_id, f"✅ @{username}{extra}", reply_markup=kb)
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 Без цифр", callback_data=f"mode_{length}_0"),
         InlineKeyboardButton(text="🔢 С цифрой", callback_data=f"mode_{length}_1")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="menu_len")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, f"Длина: {length}\nВыберите режим:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("mode_"))
@sub_required_cb
@safe_handler
async def mode_chosen(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    uid = call.from_user.id
    parts = call.data.split("_")
    length = int(parts[1])
    mixed = (parts[2] == "1")
    if not await db.can_search(uid):
        await call.answer("Лимит исчерпан!", show_alert=True)
        return
    username = await generate_username(uid, length, mixed)
    if not username:
        await call.answer("Ошибка генерации", show_alert=True)
        return
    await db.inc_search(uid)
    rem = await db.get_remaining(uid)
    extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{username}"),
         InlineKeyboardButton(text="🔄 Ещё", callback_data=f"mode_{length}_{1 if mixed else 0}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="menu_len")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, f"✅ @{username}{extra}", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "gen_7")
@sub_required_cb
@safe_handler
async def gen7(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    uid = call.from_user.id
    if not await db.can_search(uid):
        await call.answer("Лимит исчерпан!", show_alert=True)
        return
    username = await generate_username(uid, 7, False)
    if not username:
        await call.answer("Ошибка генерации", show_alert=True)
        return
    await db.inc_search(uid)
    rem = await db.get_remaining(uid)
    extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{username}"),
         InlineKeyboardButton(text="🔄 Ещё", callback_data="gen_7")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="menu_len")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, f"✅ @{username}{extra}", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("save_"))
@safe_handler
async def save_username_cb(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    username = call.data[5:]
    if await db.save_username(call.from_user.id, username):
        await call.answer(f"✅ @{username} сохранён!")
    else:
        await call.answer("❌ Недопустимый username или уже сохранён", show_alert=True)

@dp.callback_query(F.data == "profile")
@sub_required_cb
@safe_handler
async def profile(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    text = await get_profile_text(call.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Сохранённые", callback_data="my_usernames")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "my_usernames")
@sub_required_cb
@safe_handler
async def my_usernames(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    rows = await db.get_saved_usernames(call.from_user.id)
    if not rows:
        txt = "📭 Нет сохранённых."
    else:
        txt = "💾 Сохранённые:\n\n" + "\n".join(
            f"• @{r['username']} — {datetime.fromtimestamp(r['created_at']).strftime('%d.%m.%Y %H:%M')}" for r in rows[:20]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ Назад", callback_data="profile")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, txt, reply_markup=kb)
    await call.answer()

# ------------------------- SUPPORT -------------------------
@dp.callback_query(F.data == "support_start")
@sub_required_cb
@safe_handler
async def support_start(call: types.CallbackQuery, state: FSMContext):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    current = await state.get_state()
    if current:
        await call.answer("Вы уже в режиме поддержки.", show_alert=True)
        return
    await safe_edit(call.message.chat.id, call.message.message_id,
                    "📝 Напишите ваше сообщение администратору.\n\n(Команды не поддерживаются)")
    await state.set_state(SupportStates.waiting_for_message)
    await call.answer()

@dp.message(SupportStates.waiting_for_message, F.text)
@safe_handler
async def support_message(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    text = message.text
    if "http://" in text.lower() or "https://" in text.lower():
        await message.answer("❌ Ссылки запрещены.")
        return
    if len(text) > 1000 or len(text.encode()) > 4000:
        await message.answer("⚠️ Сообщение слишком длинное.")
        return
    if not await support_limiter.check(uid):
        await message.answer("⚠️ Слишком много сообщений. Попробуйте через час.")
        await state.clear()
        return
    await db.save_support_message(uid, text)
    safe_name = escape(message.from_user.full_name or "Пользователь")
    await bot.send_message(ADMIN_ID, f"🆘 Новое сообщение!\n👤 {safe_name} (ID: {uid})\n💬 {text[:500]}")
    await message.answer("✅ Сообщение отправлено администратору.")
    await state.clear()

@dp.message(SupportStates.waiting_for_message)
@safe_handler
async def support_non_text(message: types.Message, state: FSMContext):
    await message.answer("❌ Пожалуйста, отправьте текстовое сообщение.")

# ------------------------- PAYMENTS -------------------------
@dp.callback_query(F.data == "pay_menu")
@sub_required_cb
@safe_handler
async def pay_menu(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars"),
         InlineKeyboardButton(text="🪙 USDT (TRC20)", callback_data="pay_usdt")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, "🌟 Способ оплаты Premium:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "pay_stars")
@safe_handler
async def pay_stars(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t, stars in STARS_PRICES.items():
        days = STARS_DAYS[t]
        txt = f"{days} дней" if days > 0 else "Навсегда"
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{txt} – {stars} ★", callback_data=f"star_{t}_{stars}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="pay_menu")])
    await safe_edit(call.message.chat.id, call.message.message_id, "⭐ Выберите тариф:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("star_"))
@safe_handler
async def star_chosen(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    uid = call.from_user.id
    _, tariff, stars = call.data.split("_")
    stars = int(stars)
    days = STARS_DAYS[tariff]
    desc = "навсегда" if days == 0 else f"{days} дней"
    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Auron Premium",
            description=f"Premium на {desc}",
            payload=f"stars_{days}_{stars}_{uid}",
            provider_token=None,  # Исправлено: provider_token=None
            currency="XTR",
            prices=[LabeledPrice(label="Premium", amount=stars)],
            start_parameter="auron_premium"
        )
    except Exception as e:
        logger.error(f"Stars payment error: {e}")
        await call.message.answer("❌ Ошибка при создании платежа. Попробуйте позже.")
    await call.answer()

@dp.callback_query(F.data == "pay_usdt")
@safe_handler
async def pay_usdt(call: types.CallbackQuery):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for t, price in USDT_PRICES.items():
        days = USDT_DAYS[t]
        txt = f"{days} дней" if days > 0 else "Навсегда"
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{txt} – ${price}", callback_data=f"usdt_{t}_{price}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="pay_menu")])
    await safe_edit(call.message.chat.id, call.message.message_id, "🪙 Выберите тариф (USDT TRC20):", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("usdt_"))
@safe_handler
async def usdt_choose(call: types.CallbackQuery, state: FSMContext):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    uid = call.from_user.id
    _, tariff, price = call.data.split("_")
    price = float(price)
    days = USDT_DAYS[tariff]
    await state.update_data(tariff=tariff, amount=price, days=days, method="usdt")
    text = (
        f"💸 Оплата USDT (TRC20)\n\n"
        f"💰 Сумма: {price} USDT\n"
        f"📆 Тариф: {'Навсегда' if days==0 else f'{days} дней'}\n\n"
        f"📤 Отправьте точную сумму на кошелёк:\n"
        f"`{USDT_WALLET}`\n\n"
        f"После отправки нажмите «✅ Я оплатил» и введите TXID транзакции.\n\n"
        f"⚠️ Не округляйте сумму, отправляйте ровно {price} USDT."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="submit_usdt_txid")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="pay_usdt")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "submit_usdt_txid")
@safe_handler
async def submit_usdt_txid(call: types.CallbackQuery, state: FSMContext):
    if not await callback_limiter.check(call.from_user.id):
        await call.answer("⚠️ Слишком много действий. Подождите.", show_alert=True)
        return
    await safe_edit(call.message.chat.id, call.message.message_id,
                    "✍️ Введите TXID транзакции (хеш):")
    await state.set_state(PaymentStates.waiting_for_txid)
    await call.answer()

@dp.message(PaymentStates.waiting_for_txid, F.text)
@safe_handler
async def receive_txid(message: types.Message, state: FSMContext):
    txid = message.text.strip()
    if len(txid) < 20 or not re.match(r'^[a-fA-F0-9]{30,100}$', txid):
        await message.answer("❌ Неверный формат TXID. Попробуйте ещё раз или /cancel")
        return
    data = await state.get_data()
    tariff = data.get("tariff")
    amount = data.get("amount")
    days = data.get("days")
    if not tariff:
        await message.answer("❌ Сессия истекла, начните заново.")
        await state.clear()
        return
    payment_id = await db.create_payment(message.from_user.id, "usdt", tariff, amount, days, txid)
    await bot.send_message(
        ADMIN_ID,
        f"💸 Новая оплата USDT!\n"
        f"👤 Пользователь: {message.from_user.id}\n"
        f"💰 Сумма: {amount} USDT\n"
        f"📆 Тариф: {'Навсегда' if days==0 else f'{days} дней'}\n"
        f"🆔 TXID: {txid}\n"
        f"📝 Заявка #{payment_id}"
    )
    await message.answer(
        "✅ Заявка на оплату принята!\n"
        "Администратор проверит транзакцию и активирует Premium в ближайшее время.\n"
        "Спасибо за ожидание!"
    )
    await state.clear()

@dp.message(PaymentStates.waiting_for_txid)
@safe_handler
async def non_text_txid(message: types.Message, state: FSMContext):
    await message.answer("❌ Отправьте TXID текстом. Это строка из букв и цифр.")

# ------------------------- АДМИН ПАНЕЛЬ -------------------------
@dp.callback_query(F.data == "admin_panel")
@safe_handler
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    pending = await db.get_waiting_payments()
    text = f"🔧 Админ панель\n\nОжидают подтверждения: {len(pending)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Список платежей USDT", callback_data="admin_payments")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_payments")
@safe_handler
async def admin_payments(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    payments = await db.get_waiting_payments()
    if not payments:
        await call.answer("Нет ожидающих платежей", show_alert=True)
        return
    for p in payments:
        text = (
            f"📝 Заявка #{p['id']}\n"
            f"👤 User ID: {p['user_id']}\n"
            f"💰 {p['amount']} USDT\n"
            f"📆 Тариф: {p['days']} дней\n"
            f"🆔 TXID: {p['txid']}\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_payment_{p['id']}"),
             InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{p['id']}")]
        ])
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("confirm_payment_"))
@safe_handler
async def confirm_payment(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    pid = int(call.data.split("_")[-1])
    payment = await db.get_payment(pid)
    if not payment or payment["status"] != "waiting":
        await call.answer("Платёж уже обработан", show_alert=True)
        return
    await db.confirm_payment(pid)
    days = payment["days"]
    await db.set_premium(payment["user_id"], days)
    await bot.send_message(payment["user_id"], f"✅ Premium активирован на {'навсегда' if days==0 else f'{days} дней'}! Спасибо за оплату.")
    await call.answer("✅ Подтверждено, Premium выдан")
    await call.message.delete()

@dp.callback_query(F.data.startswith("reject_payment_"))
@safe_handler
async def reject_payment(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Доступ запрещён", show_alert=True)
        return
    pid = int(call.data.split("_")[-1])
    await db.reject_payment(pid)
    await call.answer("❌ Отклонено")
    await call.message.delete()

# ------------------------- ВЕБ-СЕРВЕР ДЛЯ RENDER -------------------------
async def health_check(request):
    return web.Response(text="OK", status=200)

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"✅ Веб-сервер запущен на порту {PORT}")

async def main():
    await run_web_server()
    await db.init()
    for i in range(3):
        asyncio.create_task(worker(i))
    logger.info("🤖 Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
