#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auron Search Bot - FINAL VERSION
- CV паттерн для красивых username (согласная+гласная)
- Длина 5 (Premium), 6, 7
- Выбор режима: только буквы (CV паттерн) ИЛИ буквы + 1 цифра
"""

import os
import sys
import re
import time
import random
import string
import asyncio
import asyncpg
import logging
from html import escape
from datetime import date
from collections import deque, OrderedDict
from typing import Optional, Dict, List, Tuple, Any
from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
)

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
    BOT_USERNAME = os.getenv("BOT_USERNAME", "Auron_Search_Bot")
    FREE_LIMIT = 10
    MAX_REF_BONUS = 20
    USDT_WALLET = "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV"
    
    USDT_PRICES = {
        "1d": 0.5,
        "7d": 3,
        "30d": 6,
        "1y": 25,
        "forever": 50
    }
    
    USDT_DAYS = {
        "1d": 1,
        "7d": 7,
        "30d": 30,
        "1y": 365,
        "forever": -1
    }

config = Config()

if not config.BOT_TOKEN:
    logger.error("❌ BOT_TOKEN required")
    sys.exit(1)
if not config.DATABASE_URL:
    logger.error("❌ DATABASE_URL required")
    sys.exit(1)

logger.info("=" * 50)
logger.info(f"🤖 Auron Search Bot starting")
logger.info(f"👑 Admin: {config.ADMIN_ID}")
logger.info("=" * 50)

# ================= CV PATTERN =================
C = "bcdfghjklmnpqrstvwxyz"
V = "aeiou"

def gen_cv_pattern(length: int) -> str:
    """Генерация username по CV паттерну (согласная-гласная)"""
    name = []
    for i in range(length):
        if i % 2 == 0:
            name.append(random.choice(C))
        else:
            name.append(random.choice(V))
    return ''.join(name)

def gen_cv_with_digit(length: int) -> str:
    """CV паттерн + 1 цифра в случайной позиции"""
    name = gen_cv_pattern(length)
    pos = random.randint(0, length - 1)
    name_list = list(name)
    name_list[pos] = str(random.randint(0, 9))
    return ''.join(name_list)

# ================= DATABASE =================
db_pool = None
bot_instance = None

taken_usernames_cache = set()
cache_last_update = 0
CACHE_TTL = 60

user_generation_locks = {}

def get_user_lock(uid: int) -> asyncio.Lock:
    if uid not in user_generation_locks:
        user_generation_locks[uid] = asyncio.Lock()
    return user_generation_locks[uid]

async def refresh_taken_cache():
    global taken_usernames_cache, cache_last_update
    now = time.time()
    if now - cache_last_update > CACHE_TTL:
        rows = await db_query("SELECT username FROM saved UNION SELECT username FROM temp_usernames", 'all')
        taken_usernames_cache = {row['username'] for row in rows} if rows else set()
        cache_last_update = now
        logger.info(f"Cache refreshed: {len(taken_usernames_cache)} taken usernames")

def is_username_taken_cache(username: str) -> bool:
    return username in taken_usernames_cache

async def mark_username_taken(username: str):
    taken_usernames_cache.add(username)

async def db_query(query: str, fetch: str = None, *args) -> Any:
    if not db_pool:
        return None if fetch else False
    for attempt in range(3):
        try:
            async with db_pool.acquire() as conn:
                if fetch == 'one':
                    row = await conn.fetchrow(query, *args)
                    return dict(row) if row else None
                elif fetch == 'all':
                    rows = await conn.fetch(query, *args)
                    return [dict(row) for row in rows] if rows else []
                elif fetch == 'val':
                    return await conn.fetchval(query, *args)
                else:
                    return await conn.execute(query, *args)
        except Exception as e:
            logger.error(f"DB attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return None if fetch else False

async def init_db():
    global db_pool
    for attempt in range(5):
        try:
            db_pool = await asyncpg.create_pool(
                config.DATABASE_URL,
                min_size=2,
                max_size=5,
                command_timeout=30
            )
            async with db_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS users(
                        user_id BIGINT PRIMARY KEY,
                        premium_until BIGINT DEFAULT 0,
                        searches INT DEFAULT 0,
                        last_date TEXT,
                        refs INT DEFAULT 0,
                        ref_by BIGINT DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS banned_users(
                        user_id BIGINT PRIMARY KEY,
                        banned_until BIGINT DEFAULT 0,
                        reason TEXT
                    );
                    CREATE TABLE IF NOT EXISTS saved(
                        id SERIAL, user_id BIGINT, username TEXT, created_at BIGINT,
                        UNIQUE(user_id, username), UNIQUE(username)
                    );
                    CREATE TABLE IF NOT EXISTS payments(
                        id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT,
                        plan TEXT, amount FLOAT, txid TEXT, status TEXT DEFAULT 'wait',
                        created_at BIGINT
                    );
                    CREATE TABLE IF NOT EXISTS support_messages(
                        id SERIAL PRIMARY KEY, user_id BIGINT, text TEXT,
                        created_at BIGINT, status TEXT DEFAULT 'unread'
                    );
                    CREATE TABLE IF NOT EXISTS temp_usernames(
                        id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT,
                        created_at BIGINT, expires_at BIGINT, UNIQUE(username)
                    );
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_premium ON users(premium_until)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_banned_users ON banned_users(user_id, banned_until)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_username ON saved(username)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_temp_expires ON temp_usernames(expires_at)")
            logger.info("✅ Database connected")
            await refresh_taken_cache()
            return True
        except Exception as e:
            logger.error(f"DB attempt {attempt+1}/5 failed: {e}")
            if attempt < 4:
                await asyncio.sleep(2 ** attempt)
    return False

# ================= RATE LIMITER =================
class RateLimiter:
    def __init__(self, max_size=5000, ttl=3600):
        self._limits = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._lock = asyncio.Lock()
    
    async def check(self, key: str, limit: int, window: int) -> bool:
        async with self._lock:
            now = time.time()
            if key in self._limits:
                queue = self._limits[key]
                if queue and queue[0] < now - self._ttl:
                    del self._limits[key]
                    return True
            
            if key not in self._limits:
                self._limits[key] = deque(maxlen=limit)
                self._limits.move_to_end(key)
            else:
                self._limits.move_to_end(key)
            
            queue = self._limits[key]
            while queue and queue[0] < now - window:
                queue.popleft()
            
            if len(queue) >= limit:
                return False
            queue.append(now)
            
            if len(self._limits) > self._max_size:
                self._limits.popitem(last=False)
            return True

rate_limiter = RateLimiter()

# ================= BAN SYSTEM =================
async def is_user_banned(user_id: int) -> Tuple[bool, Optional[int]]:
    result = await db_query("SELECT banned_until FROM banned_users WHERE user_id = $1 AND banned_until > $2", 'val', user_id, int(time.time()))
    return (True, result) if result else (False, None)

async def ban_user(user_id: int, minutes: int, reason: str = "Нарушение правил"):
    banned_until = int(time.time()) + minutes * 60
    await db_query("INSERT INTO banned_users(user_id, banned_until, reason) VALUES($1, $2, $3) ON CONFLICT(user_id) DO UPDATE SET banned_until=$2, reason=$3", None, user_id, banned_until, reason)

async def unban_user(user_id: int):
    await db_query("DELETE FROM banned_users WHERE user_id = $1", None, user_id)

# ================= MIDDLEWARE =================
class AntiSpamMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid = None
        if isinstance(event, Message):
            uid = event.from_user.id
        elif isinstance(event, CallbackQuery):
            uid = event.from_user.id
        
        if uid:
            banned, until = await is_user_banned(uid)
            if banned:
                mins_left = (until - int(time.time())) // 60 + 1
                msg = f"🚫 Вы забанены на {mins_left} мин."
                if isinstance(event, CallbackQuery):
                    await event.answer(msg, show_alert=True)
                else:
                    await event.answer(msg)
                return
            
            if not await rate_limiter.check(f"user:{uid}", 20, 60):
                if isinstance(event, CallbackQuery):
                    await event.answer("⚠️ Подождите!", show_alert=True)
                else:
                    await event.answer("⚠️ Подождите!")
                return
        return await handler(event, data)

# ================= ГЕНЕРАЦИЯ USERNAME =================
async def generate_unique_username(uid: int, length: int, has_digit: bool, max_attempts=50) -> Optional[str]:
    """Генерация уникального username с CV паттерном"""
    await refresh_taken_cache()
    
    for attempt in range(max_attempts):
        if has_digit:
            name = gen_cv_with_digit(length)
        else:
            name = gen_cv_pattern(length)
        
        if is_username_taken_cache(name):
            continue
        
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM saved WHERE username=$1 FOR UPDATE",
                    name
                )
                if not exists:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM temp_usernames WHERE username=$1 FOR UPDATE",
                        name
                    )
                
                if exists:
                    await mark_username_taken(name)
                    continue
                
                await conn.execute(
                    "INSERT INTO temp_usernames(user_id, username, created_at, expires_at) VALUES($1, $2, $3, $4)",
                    uid, name, int(time.time()), int(time.time()) + 3600
                )
                await mark_username_taken(name)
                return name
    
    return None

# ================= USER HELPERS =================
async def get_user(uid: int) -> dict:
    user = await db_query("SELECT * FROM users WHERE user_id=$1", 'one', uid)
    if not user:
        await db_query("INSERT INTO users(user_id, last_date) VALUES($1, $2)", None, uid, str(date.today()))
        user = await db_query("SELECT * FROM users WHERE user_id=$1", 'one', uid)
    
    if not user:
        return {"premium_until": 0, "searches": 0, "refs": 0, "ref_by": 0}
    
    today = str(date.today())
    if user.get("last_date") != today:
        await db_query("UPDATE users SET searches=0, last_date=$1 WHERE user_id=$2", None, today, uid)
        user = await db_query("SELECT * FROM users WHERE user_id=$1", 'one', uid)
    
    return user

def is_premium(user: dict) -> bool:
    if not user:
        return False
    until = user.get("premium_until", 0)
    return until == -1 or until > int(time.time())

def get_user_limit(user: dict) -> int:
    if is_premium(user):
        return -1
    refs = min(user.get("refs", 0), config.MAX_REF_BONUS)
    return config.FREE_LIMIT + refs

async def increment_searches(uid: int) -> int:
    return await db_query(
        "UPDATE users SET searches = searches + 1 WHERE user_id = $1 RETURNING searches",
        'val', uid
    ) or 0

async def set_premium(uid: int, days: int):
    if days == -1:
        until = -1
    elif days == 0:
        until = 0
    else:
        until = int(time.time()) + days * 86400
    await db_query(
        "INSERT INTO users(user_id, premium_until, last_date) VALUES($1, $2, $3) ON CONFLICT(user_id) DO UPDATE SET premium_until=$2",
        None, uid, until, str(date.today())
    )

async def add_referral(uid: int, ref_id: int):
    if uid == ref_id:
        return
    user = await get_user(uid)
    if user and user.get("ref_by", 0) == 0:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE users SET ref_by=$1 WHERE user_id=$2", ref_id, uid)
                await conn.execute("UPDATE users SET refs = refs + 1 WHERE user_id=$1", ref_id)

async def cleanup_job():
    while True:
        try:
            await db_query("DELETE FROM temp_usernames WHERE expires_at < $1", None, int(time.time()))
            await refresh_taken_cache()
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)

async def keep_alive():
    while True:
        logger.info("🟢 Bot is alive")
        await asyncio.sleep(300)

# ================= KEYBOARDS =================
def main_kb(uid=None):
    buttons = [
        [KeyboardButton(text="🔍 Найти юзернейм"), KeyboardButton(text="💎 Купить Premium")],
        [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="🆘 Поддержка")]
    ]
    if uid == config.ADMIN_ID:
        buttons.append([KeyboardButton(text="🔧 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def admin_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💰 Заявки USDT", callback_data="admin_payments")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="📨 Сообщения", callback_data="admin_messages")],
        [InlineKeyboardButton(text="🎁 Выдать Premium", callback_data="admin_premium")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="admin_ban")],
    ])

def premium_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 USDT (TRC20)", callback_data="pay_usdt")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

def length_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 (Premium)", callback_data="len_5"),
         InlineKeyboardButton(text="6", callback_data="len_6"),
         InlineKeyboardButton(text="7", callback_data="len_7")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

def mode_inline_kb(length: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 Буквы", callback_data=f"mode_{length}_0"),
         InlineKeyboardButton(text="🔢 Буквы+цифра", callback_data=f"mode_{length}_1")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_length")]
    ])

def result_inline_kb(length: int, has_digit: int, username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{username}"),
         InlineKeyboardButton(text="🔄 Ещё", callback_data=f"mode_{length}_{has_digit}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

# ================= FSM STATES =================
class AdminPremiumState(StatesGroup):
    waiting_uid = State()
    waiting_days = State()

class AdminBanState(StatesGroup):
    waiting_uid = State()
    waiting_mins = State()

class SupportState(StatesGroup):
    waiting = State()

class TxState(StatesGroup):
    waiting = State()

# ================= HANDLERS =================
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(AntiSpamMiddleware())
dp.callback_query.middleware(AntiSpamMiddleware())

# ----- User Handlers -----
@dp.message(Command("start"))
async def cmd_start(m: Message):
    args = m.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref = int(args[1].split("_")[1])
            await add_referral(m.from_user.id, ref)
        except:
            pass
    await m.answer(
        "👋 <b>Auron Search Bot</b>\n\n"
        "🔍 Генератор свободных username\n\n"
        "👇 Выберите действие в меню ниже",
        reply_markup=main_kb(m.from_user.id)
    )

@dp.message(F.text == "🔍 Найти юзернейм")
async def find_username(m: Message):
    user = await get_user(m.from_user.id)
    limit = get_user_limit(user)
    rem = "∞" if limit == -1 else max(0, limit - user.get("searches", 0))
    await m.answer(f"📏 Выберите длину username\n\n🎫 Осталось попыток: {rem}", reply_markup=length_inline_kb())

@dp.message(F.text == "💎 Купить Premium")
async def buy_premium(m: Message):
    await m.answer(
        "💎 Premium доступ\n\n"
        "✅ Безлимитная генерация\n"
        "✅ Длина 5 символов\n\n"
        "💰 Способ оплаты: USDT (TRC20)\n\n"
        "Выберите способ оплаты:",
        reply_markup=premium_inline_kb()
    )

@dp.message(F.text == "👤 Личный кабинет")
async def profile(m: Message):
    u = await get_user(m.from_user.id)
    limit = get_user_limit(u)
    s = u.get("searches", 0)
    txt = f"{s} / ∞" if limit == -1 else f"{s} / {limit}"
    rem = "∞" if limit == -1 else max(0, limit - s)
    until = u.get("premium_until", 0)
    if until == -1:
        prem = "✅ Навсегда"
    elif until > int(time.time()):
        prem = f"✅ До {time.strftime('%d.%m.%Y', time.localtime(until))}"
    else:
        prem = "❌ Нет"
    saved = await db_query("SELECT COUNT(*) FROM saved WHERE user_id=$1", 'val', m.from_user.id) or 0
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{m.from_user.id}"
    await m.answer(
        f"👤 Личный кабинет\n\n"
        f"🔎 Использовано: {txt}\n"
        f"🎫 Осталось: {rem}\n"
        f"👥 Приглашено: {u.get('refs', 0)}\n"
        f"💎 Premium: {prem}\n"
        f"💾 Сохранено: {saved}\n\n"
        f"🔗 Реферальная ссылка:\n<code>{link}</code>",
        reply_markup=main_kb(m.from_user.id)
    )

@dp.message(Command("my_usernames"))
async def my_usernames(m: Message):
    rows = await db_query("SELECT username FROM saved WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30", 'all', m.from_user.id)
    if not rows:
        await m.answer("📭 Нет сохранённых.", reply_markup=main_kb(m.from_user.id))
        return
    txt = "💾 Сохранённые:\n\n" + "\n".join(f"• <code>{escape(r['username'])}</code>" for r in rows)
    await m.answer(txt, reply_markup=main_kb(m.from_user.id))

@dp.message(F.text == "🆘 Поддержка")
async def support_start(m: Message, state: FSMContext):
    if await state.get_state():
        await m.answer("⚠️ Вы уже в режиме поддержки. /cancel чтобы выйти.")
        return
    if not await rate_limiter.check(f"support:{m.from_user.id}", 1, 60):
        await m.answer("⚠️ Можно отправлять раз в минуту.")
        return
    await state.set_state(SupportState.waiting)
    await m.answer(
        "📞 Служба поддержки\n\n"
        "📝 Напишите ваше сообщение администратору.\n\n"
        "🚫 /cancel - отмена",
        reply_markup=main_kb(m.from_user.id)
    )

@dp.message(SupportState.waiting)
async def support_msg(m: Message, state: FSMContext):
    if m.text == "/cancel":
        await state.clear()
        await m.answer("❌ Отменено", reply_markup=main_kb(m.from_user.id))
        return
    if not m.text:
        await m.answer("❌ Отправьте текст")
        return
    if len(m.text) > 2000:
        await m.answer("❌ Слишком длинное сообщение")
        return
    await db_query(
        "INSERT INTO support_messages(user_id, text, created_at, status) VALUES($1, $2, $3, 'unread')",
        None, m.from_user.id, m.text[:1000], int(time.time())
    )
    await bot_instance.send_message(config.ADMIN_ID, f"📨 Новое сообщение от {m.from_user.id}\n\n{m.text[:500]}")
    await m.answer("✅ Сообщение отправлено!", reply_markup=main_kb(m.from_user.id))
    await state.clear()

@dp.message(Command("cancel"))
async def cancel_handler(m: Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await m.answer("❌ Отменено", reply_markup=main_kb(m.from_user.id))

# ----- Generation Callbacks -----
@dp.callback_query(F.data == "back_main")
async def back_main(c: CallbackQuery):
    try:
        await c.message.delete()
    except:
        pass
    await c.message.answer("👋 Auron Search", reply_markup=main_kb(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data == "back_to_length")
async def back_to_length(c: CallbackQuery):
    await c.message.edit_text("📏 Выберите длину username", reply_markup=length_inline_kb())
    await c.answer()

@dp.callback_query(F.data.startswith("len_"))
async def choose_len(c: CallbackQuery):
    length = int(c.data.split("_")[1])
    
    if length == 5:
        user = await get_user(c.from_user.id)
        if not is_premium(user):
            await c.answer("❌ Длина 5 только для Premium!", show_alert=True)
            return
    
    await c.message.edit_text(f"⚙️ Длина: {length}\n\nВыберите режим генерации:", reply_markup=mode_inline_kb(length))
    await c.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def generate(c: CallbackQuery):
    async with get_user_lock(c.from_user.id):
        _, l, d = c.data.split("_")
        length = int(l)
        has_digit = int(d)
        uid = c.from_user.id
        
        await c.message.edit_text(f"🔄 Генерация {length}-символьного username...")
        
        user = await get_user(uid)
        limit = get_user_limit(user)
        
        if limit != -1 and user.get("searches", 0) >= limit:
            await c.message.edit_text(f"❌ Лимит {user.get('searches', 0)}/{limit}. Завтра обнулится.")
            return
        
        if length == 5 and not is_premium(user):
            await c.message.edit_text("❌ Длина 5 только для Premium!", reply_markup=length_inline_kb())
            return
        
        username = await generate_unique_username(uid, length, has_digit)
        if not username:
            await c.message.edit_text("❌ Не удалось найти свободный username. Попробуйте другую длину или режим.", reply_markup=length_inline_kb())
            return
        
        new_searches = await increment_searches(uid)
        rem = "" if limit == -1 else f"\n\n🎫 Осталось: {max(0, limit - new_searches)}"
        
        mode_text = "буквы" if has_digit == 0 else "буквы + цифра"
        
        await c.message.edit_text(
            f"✅ Найден свободный username:\n\n<code>@{username}</code>\n\n"
            f"📏 Длина: {length}\n🎯 Режим: {mode_text}{rem}",
            reply_markup=result_inline_kb(length, has_digit, username)
        )
        await c.answer()

@dp.callback_query(F.data.startswith("save_"))
async def save_username(c: CallbackQuery):
    username = c.data.split("_", 1)[1]
    
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval("SELECT 1 FROM saved WHERE username=$1 FOR UPDATE", username)
            if exists:
                await c.answer("⚠️ Этот username уже сохранён", show_alert=True)
                return
            
            await conn.execute(
                "INSERT INTO saved(user_id, username, created_at) VALUES($1, $2, $3)",
                c.from_user.id, username, int(time.time())
            )
            await mark_username_taken(username)
            await c.answer("✅ Сохранено!", show_alert=True)

# ----- Admin Panel -----
@dp.message(F.text == "🔧 Админ панель")
async def admin_panel(m: Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ Доступ запрещён", reply_markup=main_kb(m.from_user.id))
        return
    await m.answer("🔧 Админ панель", reply_markup=admin_inline_kb())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    users = await db_query("SELECT COUNT(*) FROM users", 'val') or 0
    prem = await db_query("SELECT COUNT(*) FROM users WHERE premium_until=-1 OR premium_until>$1", 'val', int(time.time())) or 0
    saved = await db_query("SELECT COUNT(*) FROM saved", 'val') or 0
    payments = await db_query("SELECT COUNT(*) FROM payments WHERE status='wait'", 'val') or 0
    support = await db_query("SELECT COUNT(*) FROM support_messages WHERE status='unread'", 'val') or 0
    await c.message.edit_text(
        f"📊 Статистика\n\n"
        f"👥 Пользователи: {users}\n"
        f"💎 Премиум: {prem}\n"
        f"💾 Уникальных username: {saved}\n"
        f"💰 Заявок USDT: {payments}\n"
        f"📨 Сообщений: {support}",
        reply_markup=admin_inline_kb()
    )
    await c.answer()

@dp.callback_query(F.data == "admin_payments")
async def admin_payments(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    rows = await db_query("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC", 'all')
    if not rows:
        await c.message.edit_text("📭 Нет заявок", reply_markup=admin_inline_kb())
        await c.answer()
        return
    await show_payment(c, rows, 0)

async def show_payment(c, payments, idx):
    p = payments[idx]
    text = f"💰 Заявка #{p['id']}\n👤 {p['user_id']}\n💵 {p['amount']} USDT\n📎 TxID: <code>{p['txid'][:40]}...</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve_{p['id']}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{p['id']}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_payments")]
    ])
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except:
        await c.message.answer(text, reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    pid = int(c.data.split("_")[1])
    pay = await db_query("SELECT user_id, plan FROM payments WHERE id=$1", 'one', pid)
    if pay:
        days = config.USDT_DAYS.get(pay["plan"], 30)
        await set_premium(pay["user_id"], days)
        await db_query("UPDATE payments SET status='approved' WHERE id=$1", None, pid)
        await bot_instance.send_message(pay["user_id"], "✅ Premium активирован!")
        await c.answer("✅")
        remaining = await db_query("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC", 'all')
        if remaining:
            await show_payment(c, remaining, 0)
        else:
            await c.message.edit_text("✅ Все заявки обработаны", reply_markup=admin_inline_kb())
    else:
        await c.answer("❌ Заявка не найдена", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    pid = int(c.data.split("_")[1])
    pay = await db_query("SELECT user_id FROM payments WHERE id=$1", 'one', pid)
    if pay:
        await db_query("UPDATE payments SET status='rejected' WHERE id=$1", None, pid)
        await bot_instance.send_message(pay["user_id"], "❌ Заявка отклонена")
        await c.answer("❌")
        remaining = await db_query("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC", 'all')
        if remaining:
            await show_payment(c, remaining, 0)
        else:
            await c.message.edit_text("✅ Все заявки обработаны", reply_markup=admin_inline_kb())
    else:
        await c.answer("❌ Заявка не найдена", show_alert=True)

@dp.callback_query(F.data == "admin_users")
async def admin_users(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    rows = await db_query("SELECT user_id, premium_until, searches, refs FROM users ORDER BY user_id DESC LIMIT 30", 'all')
    if not rows:
        await c.message.edit_text("📭 Нет пользователей", reply_markup=admin_inline_kb())
        await c.answer()
        return
    text = "👥 Последние пользователи:\n\n"
    for r in rows:
        prem = "💎" if (r.get("premium_until") == -1 or r.get("premium_until", 0) > int(time.time())) else "🔹"
        text += f"{prem} ID: <code>{r['user_id']}</code> | Поисков: {r.get('searches', 0)} | Рефералов: {r.get('refs', 0)}\n"
    await c.message.edit_text(text, reply_markup=admin_inline_kb())
    await c.answer()

@dp.callback_query(F.data == "admin_messages")
async def admin_messages(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    rows = await db_query("SELECT * FROM support_messages WHERE status='unread' ORDER BY created_at DESC", 'all')
    if not rows:
        await c.message.edit_text("📭 Нет новых сообщений", reply_markup=admin_inline_kb())
        await c.answer()
        return
    await show_message(c, rows, 0)

async def show_message(c, messages, idx):
    m = messages[idx]
    text = f"📨 Сообщение #{m['id']}\n👤 {m['user_id']}\n📅 {time.strftime('%d.%m.%Y %H:%M', time.localtime(m['created_at']))}\n\n{m['text']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Пометить прочитанным", callback_data=f"mark_read_{m['id']}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_messages")]
    ])
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except:
        await c.message.answer(text, reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("mark_read_"))
async def mark_message_read(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    mid = int(c.data.split("_")[2])
    await db_query("UPDATE support_messages SET status='read' WHERE id=$1", None, mid)
    remaining = await db_query("SELECT * FROM support_messages WHERE status='unread' ORDER BY created_at DESC", 'all')
    if remaining:
        await show_message(c, remaining, 0)
    else:
        await c.message.edit_text("✅ Все сообщения прочитаны", reply_markup=admin_inline_kb())
    await c.answer()

@dp.callback_query(F.data == "admin_premium")
async def admin_premium_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    await state.clear()
    await state.set_state(AdminPremiumState.waiting_uid)
    await c.message.edit_text("🎁 Выдача Premium\n\nВведите ID пользователя:")
    await c.answer()

@dp.message(AdminPremiumState.waiting_uid)
async def admin_premium_get_uid(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID:
        return
    try:
        uid = int(m.text.strip())
        await state.update_data(uid=uid)
        await state.set_state(AdminPremiumState.waiting_days)
        await m.answer("📅 Введите количество дней:\n-1 = навсегда\n0 = снять\n30 = 30 дней")
    except:
        await m.answer("❌ Ошибка! Введите число")

@dp.message(AdminPremiumState.waiting_days)
async def admin_premium_set_days(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID:
        return
    try:
        days = int(m.text.strip())
        data = await state.get_data()
        uid = data['uid']
        await set_premium(uid, days)
        await m.answer(f"✅ Premium выдан {uid}")
        await state.clear()
        await m.answer("🔧 Админ панель", reply_markup=admin_inline_kb())
    except:
        await m.answer("❌ Ошибка! Введите число")

@dp.callback_query(F.data == "admin_ban")
async def admin_ban_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔")
        return
    await state.clear()
    await state.set_state(AdminBanState.waiting_uid)
    await c.message.edit_text("🚫 Бан пользователя\n\nВведите ID пользователя:")
    await c.answer()

@dp.message(AdminBanState.waiting_uid)
async def admin_ban_get_uid(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID:
        return
    try:
        uid = int(m.text.strip())
        await state.update_data(uid=uid)
        await state.set_state(AdminBanState.waiting_mins)
        await m.answer("⏱ Введите минуты бана:\n0 = снять\n5 = 5 минут\n60 = 1 час")
    except:
        await m.answer("❌ Ошибка! Введите число")

@dp.message(AdminBanState.waiting_mins)
async def admin_ban_set_mins(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID:
        return
    try:
        mins = int(m.text.strip())
        data = await state.get_data()
        uid = data['uid']
        if mins == 0:
            await unban_user(uid)
            await m.answer(f"✅ Бан снят с {uid}")
        else:
            await ban_user(uid, mins)
            await m.answer(f"✅ Пользователь {uid} забанен на {mins} минут")
        await state.clear()
        await m.answer("🔧 Админ панель", reply_markup=admin_inline_kb())
    except:
        await m.answer("❌ Ошибка! Введите число")

# ----- USDT Payment -----
@dp.callback_query(F.data == "pay_usdt")
async def usdt_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 0.5 USDT", callback_data="usdt_1d")],
        [InlineKeyboardButton(text="7 дней — 3 USDT", callback_data="usdt_7d")],
        [InlineKeyboardButton(text="30 дней — 6 USDT", callback_data="usdt_30d")],
        [InlineKeyboardButton(text="1 год — 25 USDT", callback_data="usdt_1y")],
        [InlineKeyboardButton(text="Навсегда — 50 USDT", callback_data="usdt_forever")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    try:
        await c.message.edit_text(f"🪙 USDT (TRC20)\n\n📤 Кошелёк: <code>{config.USDT_WALLET}</code>\n\nВыберите тариф:", reply_markup=kb)
    except:
        await c.message.answer(f"🪙 USDT (TRC20)\n\n📤 Кошелёк: <code>{config.USDT_WALLET}</code>\n\nВыберите тариф:", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("usdt_"))
async def usdt_request(c: CallbackQuery, state: FSMContext):
    if await state.get_state():
        await c.answer("⚠️ Сначала завершите текущую операцию", show_alert=True)
        return
    plan = c.data.split("_")[1]
    price = config.USDT_PRICES[plan]
    days = config.USDT_DAYS[plan]
    await state.update_data(plan=plan, days=days, amount=price)
    await state.set_state(TxState.waiting)
    await c.message.answer(
        f"💰 Оплата USDT (TRC20)\n\n"
        f"Сумма: {price} USDT\n"
        f"Тариф: {'навсегда' if days == -1 else f'{days} дн.'}\n\n"
        f"📤 Отправьте на кошелёк: <code>{config.USDT_WALLET}</code>\n\n"
        f"📎 После отправки введите TxID:"
    )
    await c.answer()

@dp.message(TxState.waiting)
async def handle_txid(m: Message, state: FSMContext):
    if not m.text:
        await m.answer("❌ Отправьте текст с TxID")
        return
    txid = m.text.strip()
    if len(txid) < 30:
        await m.answer("❌ Неверный формат TxID")
        return
    data = await state.get_data()
    await db_query(
        "INSERT INTO payments(user_id, method, plan, amount, txid, created_at, status) VALUES($1, 'usdt', $2, $3, $4, $5, 'wait')",
        None, m.from_user.id, data["plan"], data["amount"], txid, int(time.time())
    )
    await m.answer("✅ Заявка отправлена! Администратор проверит.", reply_markup=main_kb(m.from_user.id))
    await state.clear()

# ================= MAIN =================
async def run_bot():
    global bot_instance, db_pool
    while True:
        try:
            print("🟢 Starting Auron Search Bot...")
            
            if not await init_db():
                print("❌ Database connection failed, retrying...")
                await asyncio.sleep(10)
                continue
            
            bot_instance = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            
            asyncio.create_task(cleanup_job())
            asyncio.create_task(keep_alive())
            
            print("🟢 Bot is running...")
            await dp.start_polling(bot_instance)
            
        except Exception as e:
            print(f"💥 BOT CRASHED: {e}")
            logger.error(f"Bot crashed: {e}", exc_info=True)
            
            if bot_instance:
                await bot_instance.session.close()
            if db_pool:
                await db_pool.close()
                db_pool = None
            
            print("🔄 Restarting in 10 seconds...")
            await asyncio.sleep(10)

async def main():
    try:
        await run_bot()
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
