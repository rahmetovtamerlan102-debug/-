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

# ------------------------- DATABASE -------------------------
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
                last_date DATE,
                referrer_id BIGINT DEFAULT 0,
                referals_count INT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS saved_usernames (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                created_at BIGINT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_usernames ON saved_usernames(user_id, username);
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
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                message TEXT,
                created_at BIGINT,
                is_read BOOLEAN DEFAULT FALSE
            );
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
            row = await conn.fetchrow("SELECT referrer_id FROM users WHERE user_id=$1", new_uid)
            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id, last_date, referrer_id, referals_count, premium_until) VALUES ($1, $2, $3, 0, 0)",
                    new_uid, date.today(), ref_id
                )
                await conn.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=$1", ref_id)
                return True
            elif row["referrer_id"] == 0:
                await conn.execute("UPDATE users SET referrer_id=$1 WHERE user_id=$2", ref_id, new_uid)
                await conn.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=$1", ref_id)
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

    async def save_support_message(self, uid, text):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO support_messages (user_id, message, created_at) VALUES ($1, $2, $3)",
                uid, text[:1000], int(time.time())
            )

db = Database(DATABASE_URL)

# ------------------------- ГЕНЕРАТОР -------------------------
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

# ------------------------- ХЕНДЛЕРЫ (сокращённо) -------------------------
# Здесь вставьте все ваши хендлеры команд и callback'ов
# ... (весь ваш код с @dp.message, @dp.callback_query и т.д.)

# Для краткости покажу минимальные:
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Бот работает! Команды: /start, /help")

# ------------------------- ВЕБ-СЕРВЕР ДЛЯ RENDER -------------------------
async def health_check(request):
    """Эндпоинт для UptimeRobot"""
    return web.Response(text="OK", status=200)

async def run_bot():
    """Запуск бота и веб-сервера"""
    # Инициализация БД
    await db.init()
    
    # Запуск воркеров
    for i in range(3):
        asyncio.create_task(worker(i))
    
    # Настройка веб-сервера
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    # Запуск веб-сервера
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Web server started on port {PORT}")
    
    # Запуск бота в polling режиме
    logger.info("🤖 Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(run_bot())
