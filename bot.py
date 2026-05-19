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
from aiohttp import web, ClientSession

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

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
BOT_USERNAME = os.getenv("BOT_USERNAME", "Auronsearchesbot")
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
MAX_REF_BONUS = 20
FOREVER_PREMIUM = 9999999999

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

    async def create_payment(self, uid, method, tariff, amount, days, txid=None):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO payments (user_id, method, tariff, amount, days, txid, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
            """, uid, method, tariff, amount, days, txid, int(time.time()))

    async def confirm_payment(self, pid):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE payments SET status='confirmed' WHERE id=$1", pid)

    async def get_waiting_payments(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, user_id, method, tariff, amount, days, txid, created_at FROM payments WHERE status='waiting' ORDER BY created_at DESC"
            )

db = Database(DATABASE_URL)

# ------------------------- ГЕНЕРАТОР ЮЗЕРНЕЙМОВ -------------------------
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

# ------------------------- ВЕБ-СЕРВЕР ДЛЯ SELF-PING -------------------------
async def health_check(request):
    return web.Response(text="Bot is running", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

async def self_pinger():
    """Каждые 4 минуты стучится к себе, чтобы Render не усыпил"""
    url = f"http://localhost:{PORT}/"
    while True:
        await asyncio.sleep(240)
        try:
            async with ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("🏓 Self-ping отправлен")
                    else:
                        logger.warning(f"Self-ping ответ {resp.status}")
        except Exception as e:
            logger.error(f"Self-ping ошибка: {e}")

# ------------------------- КЛАВИАТУРЫ -------------------------
def get_main_keyboard(uid):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 НАЙТИ ЮЗЕРНЕЙМ", callback_data="menu_len")],
        [InlineKeyboardButton(text="💎 КУПИТЬ PREMIUM", callback_data="pay_menu")],
        [InlineKeyboardButton(text="👤 ЛИЧНЫЙ КАБИНЕТ", callback_data="profile")]
    ])
    if uid == ADMIN_ID:
        kb.inline_keyboard.append([InlineKeyboardButton(text="🔧 АДМИН ПАНЕЛЬ", callback_data="admin_panel")])
    return kb

async def send_main_menu(chat_id, uid):
    await bot.send_message(
        chat_id,
        "🔷 *AURON SEARCH*\n\nПрофессиональный поиск свободных username.\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(uid)
    )

# ------------------------- ОБРАБОТЧИКИ -------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.text and len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg.split("_")[1])
                await db.add_referral(message.from_user.id, ref_id)
            except:
                pass
    await send_main_menu(message.chat.id, message.from_user.id)

@dp.message(Command("my_usernames"))
async def show_saved(message: types.Message):
    rows = await db.get_saved_usernames(message.from_user.id)
    if not rows:
        await message.answer("📭 Нет сохранённых username.")
        return
    text = "💾 *Сохранённые username:*\n\n" + "\n".join(
        f"• @{r['username']} — {datetime.fromtimestamp(r['created_at']).strftime('%d.%m.%Y %H:%M')}" for r in rows[:20]
    )
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "menu_len")
async def menu_len(call: types.CallbackQuery):
    rem = await db.get_remaining(call.from_user.id)
    txt = "🔍 *Выберите длину username:*" if rem == float('inf') else f"🎫 *Осталось {rem} попыток сегодня.*\n\n🔍 Выберите длину:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 (ТОЛЬКО PREMIUM)", callback_data="len_5"),
         InlineKeyboardButton(text="6", callback_data="len_6"),
         InlineKeyboardButton(text="7", callback_data="len_7")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
    ])
    await call.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "back_main")
async def back_main(call: types.CallbackQuery):
    await call.message.delete()
    await send_main_menu(call.message.chat.id, call.from_user.id)
    await call.answer()

@dp.callback_query(F.data.startswith("len_"))
async def len_chosen(call: types.CallbackQuery):
    uid = call.from_user.id
    length = int(call.data.split("_")[1])
    p, _, _, _, _, _ = await db.get_user(uid)
    if length == 5 and not p:
        await call.answer("❌ Длина 5 букв доступна только Premium!", show_alert=True)
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
            [InlineKeyboardButton(text="💾 СОХРАНИТЬ", callback_data=f"save_{username}"),
             InlineKeyboardButton(text="🔄 ЕЩЁ", callback_data="gen_7")],
            [InlineKeyboardButton(text="◀ НАЗАД", callback_data="menu_len")]
        ])
        await call.message.edit_text(f"✅ @{username}{extra}", parse_mode="Markdown", reply_markup=kb)
        await call.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 БЕЗ ЦИФР", callback_data=f"mode_{length}_0"),
         InlineKeyboardButton(text="🔢 С ЦИФРОЙ", callback_data=f"mode_{length}_1")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="menu_len")]
    ])
    await call.message.edit_text(f"🔍 *Длина: {length}*\nВыберите режим:", parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def mode_chosen(call: types.CallbackQuery):
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
        [InlineKeyboardButton(text="💾 СОХРАНИТЬ", callback_data=f"save_{username}"),
         InlineKeyboardButton(text="🔄 ЕЩЁ", callback_data=f"mode_{length}_{1 if mixed else 0}")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="menu_len")]
    ])
    await call.message.edit_text(f"✅ @{username}{extra}", parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "gen_7")
async def gen7(call: types.CallbackQuery):
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
        [InlineKeyboardButton(text="💾 СОХРАНИТЬ", callback_data=f"save_{username}"),
         InlineKeyboardButton(text="🔄 ЕЩЁ", callback_data="gen_7")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="menu_len")]
    ])
    await call.message.edit_text(f"✅ @{username}{extra}", parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("save_"))
async def save_username_cb(call: types.CallbackQuery):
    username = call.data[5:]
    if await db.save_username(call.from_user.id, username):
        await call.answer(f"✅ @{username} сохранён!")
    else:
        await call.answer("❌ Недопустимый username или уже сохранён", show_alert=True)

@dp.callback_query(F.data == "profile")
async def profile(call: types.CallbackQuery):
    uid = call.from_user.id
    p, s, _, until, refs, _ = await db.get_user(uid)
    remain = await db.get_remaining(uid)
    max_s = await db.get_max_searches(uid)
    remain_str = "∞" if remain == float('inf') else str(remain)
    max_str = "∞" if max_s == float('inf') else str(max_s)
    status = "👑 PREMIUM" if p else "🔷 ОБЫЧНЫЙ"
    if until == FOREVER_PREMIUM:
        expiry = "Навсегда"
    elif p:
        expiry = f"до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}"
    else:
        expiry = "Не активен"
    link = db.get_referral_link(uid)
    text = (
        f"👤 *ЛИЧНЫЙ КАБИНЕТ*\n\n"
        f"┌ 🔷 Статус: {status}\n"
        f"├ 🔷 Попытки сегодня: {remain_str}\n"
        f"├ 🔷 Дневной лимит: {max_str}\n"
        f"├ 🔷 Приглашено друзей: {refs}\n"
        f"├ 🔷 Реферальная ссылка: [тык]({link})\n"
        f"├ 🔷 Premium: {expiry}\n"
        f"└ 🔷 Сохранённые: /my_usernames"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 СОХРАНЁННЫЕ", callback_data="my_usernames")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
    ])
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
    await call.answer()

@dp.callback_query(F.data == "my_usernames")
async def my_usernames(call: types.CallbackQuery):
    rows = await db.get_saved_usernames(call.from_user.id)
    if not rows:
        txt = "📭 *Нет сохранённых username.*"
    else:
        txt = "💾 *Сохранённые username:*\n\n" + "\n".join(
            f"• @{r['username']} — {datetime.fromtimestamp(r['created_at']).strftime('%d.%m.%Y %H:%M')}" for r in rows[:20]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="profile")]
    ])
    await call.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
    await call.answer()

# ------------------------- ПЛАТЕЖИ -------------------------
@dp.callback_query(F.data == "pay_menu")
async def pay_menu(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ TELEGRAM STARS", callback_data="pay_stars")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
    ])
    await call.message.edit_text("🌟 *Способ оплаты Premium:*", parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "pay_stars")
async def pay_stars(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 ДНЕЙ – 60 ★", callback_data="star_7d_60")],
        [InlineKeyboardButton(text="30 ДНЕЙ – 240 ★", callback_data="star_30d_240")],
        [InlineKeyboardButton(text="НАВСЕГДА – 800 ★", callback_data="star_forever_800")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="pay_menu")]
    ])
    await call.message.edit_text("⭐ *Выберите тариф Premium:*", parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("star_"))
async def star_chosen(call: types.CallbackQuery):
    uid = call.from_user.id
    _, tariff, stars = call.data.split("_")
    stars = int(stars)
    if tariff == "7d":
        days = 7
        desc = "7 дней"
    elif tariff == "30d":
        days = 30
        desc = "30 дней"
    else:
        days = 0
        desc = "навсегда"
    await bot.send_invoice(
        chat_id=call.message.chat.id,
        title="AURON PREMIUM",
        description=f"Premium доступ на {desc}",
        payload=f"premium_{days}_{uid}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=stars)],
        start_parameter="auron_premium"
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    uid = message.from_user.id
    payload = message.successful_payment.invoice_payload
    try:
        _, days_str, _ = payload.split("_")
        days = int(days_str)
    except:
        days = 0
    await db.set_premium(uid, days)
    days_text = "навсегда" if days == 0 else f"{days} дней"
    await message.answer(f"✅ *Premium успешно активирован!*\n\nДоступ: {days_text}\nСпасибо за покупку! 🎉", parse_mode="Markdown")

# ------------------------- АДМИН ПАНЕЛЬ -------------------------
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещён", show_alert=True)
        return
    payments = await db.get_waiting_payments()
    text = f"🔧 *АДМИН ПАНЕЛЬ*\n\n💰 *Ожидают подтверждения:* {len(payments)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 ПОДТВЕРДИТЬ ОПЛАТЫ", callback_data="admin_verify")],
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
    ])
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_verify")
async def admin_verify(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещён", show_alert=True)
        return
    payments = await db.get_waiting_payments()
    if not payments:
        await call.answer("Нет ожидающих оплат", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for p in payments:
        days = p["days"]
        days_text = "навсегда" if days == 0 else f"{days} дн"
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"✅ {p['user_id']} – {days_text}", callback_data=f"confirm_{p['id']}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")])
    await call.message.edit_text("💰 *Выберите оплату для подтверждения:*", parse_mode="Markdown", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_payment(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещён", show_alert=True)
        return
    pid = int(call.data.split("_")[1])
    payment = await db.get_payment(pid)
    if not payment:
        await call.answer("Оплата не найдена", show_alert=True)
        return
    await db.confirm_payment(pid)
    await db.set_premium(payment["user_id"], payment["days"])
    await bot.send_message(payment["user_id"], "✅ *Ваша оплата подтверждена! Premium активирован.*", parse_mode="Markdown")
    await call.answer("✅ Оплата подтверждена!", show_alert=True)
    await admin_verify(call)

# ------------------------- ЗАПУСК -------------------------
async def main():
    # Запускаем веб-сервер для self-ping
    asyncio.create_task(start_web_server())
    asyncio.create_task(self_pinger())
    
    # Запускаем воркеры генерации
    for i in range(10):
        asyncio.create_task(worker(i))
    
    await db.init()
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
