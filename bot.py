#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import time
import random
import string
import asyncio
import asyncpg
import logging
from datetime import date
from collections import defaultdict, deque
from aiohttp import web
from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton,
    LabeledPrice, PreCheckoutQuery
)

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
    BOT_USERNAME = os.getenv("BOT_USERNAME", "Auron_Search_Bot")
    PORT = int(os.getenv("PORT", "8080"))
    DB_SSL = os.getenv("DB_SSL", "true").lower() == "true"
    FREE_LIMIT = 10
    MAX_REF_BONUS = 20
    USDT_WALLET = "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV"

    STARS_PRICES = {"1d":10,"7d":60,"30d":240,"1y":800,"forever":1500}
    STARS_DAYS = {"1d":1,"7d":7,"30d":30,"1y":365,"forever":-1}
    USDT_PRICES = {"1d":0.5,"7d":3,"30d":6,"1y":25,"forever":50}
    USDT_DAYS = {"1d":1,"7d":7,"30d":30,"1y":365,"forever":-1}

config = Config()

if not config.BOT_TOKEN or not config.DATABASE_URL:
    print("❌ Missing BOT_TOKEN or DATABASE_URL")
    sys.exit(1)

print("=" * 50)
print(f"✅ Bot started. Admin: {config.ADMIN_ID}")
print("=" * 50)

# ================= DATABASE =================
db_pool = None

async def db_execute(query, *args):
    async with db_pool.acquire() as conn:
        return await conn.execute(query, *args)

async def db_fetchrow(query, *args):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(query, *args)

async def db_fetchall(query, *args):
    async with db_pool.acquire() as conn:
        return await conn.fetch(query, *args)

async def db_fetchval(query, *args):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(query, *args)

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=2,
        max_size=10,
        ssl="require" if config.DB_SSL else None
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
            CREATE TABLE IF NOT EXISTS saved(
                id SERIAL, user_id BIGINT, username TEXT, created_at BIGINT, UNIQUE(user_id,username)
            );
            CREATE TABLE IF NOT EXISTS payments(
                id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT, plan TEXT, amount FLOAT, txid TEXT, status TEXT DEFAULT 'wait', created_at BIGINT
            );
            CREATE TABLE IF NOT EXISTS temp_usernames(
                user_id BIGINT PRIMARY KEY, username TEXT, created_at BIGINT, expires_at BIGINT
            );
            CREATE TABLE IF NOT EXISTS support_messages(
                id SERIAL PRIMARY KEY, user_id BIGINT, text TEXT, created_at BIGINT, status TEXT DEFAULT 'unread'
            );
        """)
    logger.info("Database ready")

# ================= КЛАВИАТУРЫ =================
def get_main_keyboard(uid=None):
    buttons = [
        [KeyboardButton(text="🔍 Найти юзернейм"), KeyboardButton(text="💎 Купить Premium")],
        [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="🆘 Поддержка")]
    ]
    if uid == config.ADMIN_ID:
        buttons.append([KeyboardButton(text="🔧 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_admin_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💰 Заявки USDT", callback_data="admin_payments")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="📨 Сообщения", callback_data="admin_messages")],
        [InlineKeyboardButton(text="🎁 Выдать Premium", callback_data="admin_premium_start")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="admin_ban_start")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel_back")]
    ])

def premium_request_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🪙 USDT", callback_data="pay_usdt")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

# ================= RATE LIMITER (с очисткой банов) =================
class RateLimiter:
    def __init__(self, max_history=50):
        self.requests = defaultdict(lambda: deque(maxlen=max_history))
        self.bans = {}
    async def check(self, key, limit, window):
        now = time.time()
        if key not in self.requests:
            self.requests[key] = deque(maxlen=50)
        while self.requests[key] and self.requests[key][0] < now - window:
            self.requests[key].popleft()
        if len(self.requests[key]) >= limit:
            wait = int(now - self.requests[key][0]) + 1
            return False, wait
        self.requests[key].append(now)
        return True, 0
    async def is_banned(self, uid):
        if uid in self.bans and self.bans[uid] > time.time():
            return True, int(self.bans[uid] - time.time())
        return False, 0
    async def ban_user(self, uid, duration):
        self.bans[uid] = time.time() + duration
    async def unban_user(self, uid):
        self.bans.pop(uid, None)
    async def cleanup_bans(self):
        now = time.time()
        self.bans = {k:v for k,v in self.bans.items() if v > now}

rate_limiter = RateLimiter()

class AntiSpamMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid = None
        if isinstance(event, Message): uid = event.from_user.id
        elif isinstance(event, CallbackQuery): uid = event.from_user.id
        if uid:
            banned, ttl = await rate_limiter.is_banned(uid)
            if banned:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"🚫 Бан {ttl} сек.", show_alert=True)
                else:
                    await event.answer(f"🚫 Бан {ttl} сек.")
                return
            ok, wait = await rate_limiter.check(f"user:{uid}", 20, 60)
            if not ok:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"⚠️ Ждите {wait} сек.", show_alert=True)
                else:
                    await event.answer(f"⚠️ Ждите {wait} сек.")
                return
        return await handler(event, data)

# ================= ПОМОЩНИКИ =================
async def get_user(uid):
    u = await db_fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    if not u:
        await db_execute("INSERT INTO users(user_id, last_date) VALUES($1,$2)", uid, str(date.today()))
        u = await db_fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    today = str(date.today())
    if u["last_date"] != today:
        await db_execute("UPDATE users SET searches=0,last_date=$1 WHERE user_id=$2", today, uid)
        u = await db_fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    return u

def is_premium(user):
    if user is None:
        return False
    until = user.get("premium_until", 0)
    return until == -1 or until > int(time.time())

def get_limit(user):
    if is_premium(user):
        return -1   # бесконечный лимит
    refs = min(user.get("refs",0), config.MAX_REF_BONUS)
    return config.FREE_LIMIT + refs

async def can_search(uid):
    user = await get_user(uid)
    limit = get_limit(user)
    if limit == -1:
        return True, ""
    if user["searches"] >= limit:
        return False, f"Лимит {user['searches']}/{limit}"
    return True, ""

async def inc_search(uid):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("UPDATE users SET searches=searches+1 WHERE user_id=$1 RETURNING searches", uid)
        return row["searches"] if row else 0

async def set_premium(uid, days):
    if days == 0: until = 0
    elif days == -1: until = -1
    else: until = int(time.time()) + days * 86400
    await db_execute(
        "INSERT INTO users(user_id, premium_until, last_date) VALUES($1, $2, $3) "
        "ON CONFLICT(user_id) DO UPDATE SET premium_until=$2",
        uid, until, str(date.today())
    )

async def username_exists(username):
    return await db_fetchval(
        "SELECT 1 FROM saved WHERE username=$1 UNION SELECT 1 FROM temp_usernames WHERE username=$1 LIMIT 1",
        username
    ) is not None

def gen_username(length, has_digit):
    letters = "abcdefghijklmnopqrstuvwxyz"
    if not has_digit:
        return "".join(random.choice(letters) for _ in range(length))
    chars = [random.choice(letters) for _ in range(length)]
    chars[random.randint(0,length-1)] = str(random.randint(0,9))
    return "".join(chars)

async def generate_unique_username(length, has_digit, max_attempts=20):
    for _ in range(max_attempts):
        name = gen_username(length, has_digit)
        if not await username_exists(name):
            return name
    return gen_username(length, has_digit)

# ================= DISPATCHER =================
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(AntiSpamMiddleware())
dp.callback_query.middleware(AntiSpamMiddleware())

# ================= ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ =================
@dp.message(Command("start"))
async def cmd_start(m: Message):
    args = m.text.split()
    if len(args)>1 and args[1].startswith("ref_"):
        try:
            ref = int(args[1].split("_")[1])
            if ref != m.from_user.id:
                user = await get_user(m.from_user.id)
                if user.get("ref_by",0)==0:
                    async with db_pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute("UPDATE users SET ref_by=$1 WHERE user_id=$2", ref, m.from_user.id)
                            await conn.execute("UPDATE users SET refs=refs+1 WHERE user_id=$1", ref)
        except: pass
    await m.answer(
        "👋 *Auron Search*\n\nПрофессиональный поиск свободных юзернеймов.\n\nВыберите действие:",
        parse_mode="Markdown", reply_markup=get_main_keyboard(m.from_user.id)
    )

@dp.message(F.text == "🔍 Найти юзернейм")
async def find_username(m: Message):
    user = await get_user(m.from_user.id)
    limit = get_limit(user)
    if limit == -1:
        rem = "∞"
    else:
        rem = str(max(0, int(limit) - user["searches"]))
    btns = []
    if is_premium(user):
        btns.append(InlineKeyboardButton(text="5⭐", callback_data="len_5"))
    btns.append(InlineKeyboardButton(text="6", callback_data="len_6"))
    btns.append(InlineKeyboardButton(text="7", callback_data="len_7"))
    kb = InlineKeyboardMarkup(inline_keyboard=[btns, [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]])
    await m.answer(f"📏 *Выберите длину*\n🎫 Осталось: {rem}", parse_mode="Markdown", reply_markup=kb)

@dp.message(F.text == "💎 Купить Premium")
async def buy_premium(m: Message):
    await m.answer(
        "💎 *Premium доступ*\n✅ Безлимит\n✅ Длина 5\n\nВыберите способ оплаты:",
        parse_mode="Markdown", reply_markup=premium_request_inline_kb()
    )

@dp.message(F.text == "👤 Личный кабинет")
async def profile(m: Message):
    u = await get_user(m.from_user.id)
    limit = get_limit(u)
    s = u["searches"]
    if limit == -1:
        txt = f"{s} / ∞"
        rem = "∞"
    else:
        txt = f"{s} / {limit}"
        rem = max(0, int(limit)-s)
    until = u["premium_until"]
    if until == -1:
        prem = "✅ Навсегда"
    elif until > int(time.time()):
        prem = "✅ Активен"
    else:
        prem = "❌ Нет"
    saved = await db_fetchval("SELECT COUNT(*) FROM saved WHERE user_id=$1", m.from_user.id) or 0
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{m.from_user.id}"
    await m.answer(
        f"👤 *Профиль*\n🔎 Использовано: {txt}\n🎫 Осталось: {rem}\n👥 Рефералы: {u['refs']}\n💎 Premium: {prem}\n💾 Сохранено: {saved}\n\n🔗 *Реф. ссылка:* `{link}`\n📋 /my_usernames",
        parse_mode="Markdown", reply_markup=get_main_keyboard(m.from_user.id)
    )

@dp.message(Command("my_usernames"))
async def my_usernames(m: Message):
    rows = await db_fetchall("SELECT username FROM saved WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30", m.from_user.id)
    if not rows:
        await m.answer("📭 Нет сохранённых.", reply_markup=get_main_keyboard(m.from_user.id))
        return
    txt = "💾 *Сохранённые:*\n" + "\n".join(f"• @{r['username']}" for r in rows)
    await m.answer(txt, parse_mode="Markdown", reply_markup=get_main_keyboard(m.from_user.id))

# ================= ПОДДЕРЖКА =================
class SupportState(StatesGroup):
    waiting = State()

@dp.message(F.text == "🆘 Поддержка")
async def support_start(m: Message, state: FSMContext):
    if await state.get_state() is not None:
        await m.answer("Вы уже в режиме поддержки. Отправьте /cancel чтобы выйти.")
        return
    await state.set_state(SupportState.waiting)
    await m.answer("📝 Напишите сообщение администратору.\n/cancel — отмена.", reply_markup=get_main_keyboard(m.from_user.id))

@dp.message(SupportState.waiting)
async def support_msg(m: Message, state: FSMContext):
    if m.text == "/cancel":
        await state.clear()
        await m.answer("❌ Отменено", reply_markup=get_main_keyboard(m.from_user.id))
        return
    await db_execute("INSERT INTO support_messages(user_id,text,created_at,status) VALUES($1,$2,$3,'unread')", m.from_user.id, m.text[:1000], int(time.time()))
    await m.bot.send_message(config.ADMIN_ID, f"📨 *Новое сообщение*\n👤 ID: `{m.from_user.id}`\n💬 {m.text[:500]}", parse_mode="Markdown")
    await m.answer("✅ Отправлено!", reply_markup=get_main_keyboard(m.from_user.id))
    await state.clear()

# ================= ГЕНЕРАЦИЯ =================
@dp.callback_query(F.data == "back_main")
async def back_main(c: CallbackQuery):
    try:
        await c.message.delete()
    except:
        pass
    await c.message.answer("👋 Auron Search", reply_markup=get_main_keyboard(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data == "admin_panel_back")
async def admin_panel_back(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    try:
        await c.message.edit_text("🔧 *Админ панель*", parse_mode="Markdown", reply_markup=get_admin_inline_kb())
    except:
        await c.message.answer("🔧 *Админ панель*", parse_mode="Markdown", reply_markup=get_admin_inline_kb())
    await c.answer()

@dp.callback_query(F.data.startswith("len_"))
async def choose_len(c: CallbackQuery):
    length = int(c.data.split("_")[1])
    if length == 5:
        user = await get_user(c.from_user.id)
        if not is_premium(user):
            await c.answer("❌ Длина 5 только для Premium", show_alert=True)
            return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 Буквы", callback_data=f"mode_{length}_0"),
         InlineKeyboardButton(text="🔢+цифра", callback_data=f"mode_{length}_1")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    try:
        await c.message.edit_text(f"⚙️ *Длина: {length}*\nВыберите режим:", parse_mode="Markdown", reply_markup=kb)
    except:
        await c.message.answer(f"⚙️ *Длина: {length}*\nВыберите режим:", parse_mode="Markdown", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def generate(c: CallbackQuery):
    _, l, d = c.data.split("_",2)
    length, has_digit = int(l), int(d)
    uid = c.from_user.id
    can, reason = await can_search(uid)
    if not can:
        await c.answer(f"❌ {reason}", show_alert=True)
        return
    if length == 5:
        user = await get_user(uid)
        if not is_premium(user):
            await c.answer("❌ Длина 5 только для Premium", show_alert=True)
            return
    username = await generate_unique_username(length, has_digit)
    new_searches = await inc_search(uid)
    await db_execute(
        "INSERT INTO temp_usernames(user_id,username,created_at,expires_at) VALUES($1,$2,$3,$4) ON CONFLICT(user_id) DO UPDATE SET username=$2,expires_at=$4",
        uid, username, int(time.time()), int(time.time())+3600
    )
    user = await get_user(uid)
    limit = get_limit(user)
    if limit == -1:
        rem = ""
    else:
        rem = f"\n🎫 Осталось: {max(0, int(limit)-user['searches'])}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("💾 Сохранить", callback_data="save"),
         InlineKeyboardButton("🔄 Ещё", callback_data=f"mode_{length}_{has_digit}")],
        [InlineKeyboardButton("◀ Назад", callback_data="back_main")]
    ])
    try:
        await c.message.edit_text(f"✅ @{username}{rem}", parse_mode="Markdown", reply_markup=kb)
    except:
        await c.message.answer(f"✅ @{username}{rem}", parse_mode="Markdown", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data == "save")
async def save_username(c: CallbackQuery):
    row = await db_fetchrow("SELECT username FROM temp_usernames WHERE user_id=$1 AND expires_at>$2", c.from_user.id, int(time.time()))
    if not row:
        await c.answer("❌ Нечего сохранять", show_alert=True)
        return
    try:
        await db_execute("INSERT INTO saved(user_id,username,created_at) VALUES($1,$2,$3)", c.from_user.id, row['username'], int(time.time()))
        await c.answer("✅ Сохранено!", show_alert=True)
    except:
        await c.answer("⚠️ Уже сохранён", show_alert=True)

# ================= АДМИН ПАНЕЛЬ =================
start_time = time.time()

@dp.message(F.text == "🔧 Админ панель")
async def admin_panel(m: Message):
    if m.from_user.id != config.ADMIN_ID:
        await m.answer("⛔ Доступ запрещён", reply_markup=get_main_keyboard(m.from_user.id))
        return
    await m.answer("🔧 *Админ панель*", parse_mode="Markdown", reply_markup=get_admin_inline_kb())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    users = await db_fetchval("SELECT COUNT(*) FROM users")
    prem = await db_fetchval("SELECT COUNT(*) FROM users WHERE premium_until=-1 OR premium_until>$1", int(time.time()))
    saved = await db_fetchval("SELECT COUNT(*) FROM saved")
    payments = await db_fetchval("SELECT COUNT(*) FROM payments WHERE status='wait'")
    support = await db_fetchval("SELECT COUNT(*) FROM support_messages WHERE status='unread'")
    uptime = int(time.time() - start_time)
    h,m = uptime//3600, (uptime%3600)//60
    try:
        await c.message.edit_text(
            f"📊 *Статистика*\n👥 {users}\n💎 {prem}\n💾 {saved}\n💰 {payments}\n📨 {support}\n⏱ {h}ч {m}м",
            parse_mode="Markdown", reply_markup=get_admin_inline_kb()
        )
    except:
        await c.message.answer(
            f"📊 *Статистика*\n👥 {users}\n💎 {prem}\n💾 {saved}\n💰 {payments}\n📨 {support}\n⏱ {h}ч {m}м",
            parse_mode="Markdown", reply_markup=get_admin_inline_kb()
        )
    await c.answer()

@dp.callback_query(F.data == "admin_payments")
async def admin_payments(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    rows = await db_fetchall("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
    if not rows:
        try:
            await c.message.edit_text("📭 Нет заявок", reply_markup=get_admin_inline_kb())
        except:
            await c.message.answer("📭 Нет заявок", reply_markup=get_admin_inline_kb())
        await c.answer()
        return
    await show_payment(c, rows, 0)

async def show_payment(c, payments, idx):
    p = payments[idx]
    text = f"💰 *Заявка #{p['id']}*\n👤 `{p['user_id']}`\n💵 {p['amount']} USDT\n📎 TxID: `{p['txid'][:40]}...`\n📅 {time.strftime('%d.%m.%Y %H:%M',time.localtime(p['created_at']))}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{p['id']}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{p['id']}")],
        [InlineKeyboardButton("◀ Назад", callback_data="admin_panel_back")]
    ])
    try:
        await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except:
        await c.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    pid = int(c.data.split("_")[1])
    pay = await db_fetchrow("SELECT user_id,plan FROM payments WHERE id=$1", pid)
    if pay:
        days = config.USDT_DAYS.get(pay["plan"],30)
        await set_premium(pay["user_id"], days)
        await db_execute("UPDATE payments SET status='approved' WHERE id=$1", pid)
        await c.bot.send_message(pay["user_id"], "✅ *Premium активирован!*", parse_mode="Markdown")
        remaining = await db_fetchall("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
        if remaining:
            await show_payment(c, remaining, 0)
        else:
            try:
                await c.message.edit_text("✅ Все заявки обработаны", reply_markup=get_admin_inline_kb())
            except:
                await c.message.answer("✅ Все заявки обработаны", reply_markup=get_admin_inline_kb())
    else:
        await c.answer("❌ Не найдена", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    pid = int(c.data.split("_")[1])
    pay = await db_fetchrow("SELECT user_id FROM payments WHERE id=$1", pid)
    if pay:
        await db_execute("UPDATE payments SET status='rejected' WHERE id=$1", pid)
        await c.bot.send_message(pay["user_id"], "❌ *Заявка отклонена*", parse_mode="Markdown")
        remaining = await db_fetchall("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
        if remaining:
            await show_payment(c, remaining, 0)
        else:
            try:
                await c.message.edit_text("✅ Все заявки обработаны", reply_markup=get_admin_inline_kb())
            except:
                await c.message.answer("✅ Все заявки обработаны", reply_markup=get_admin_inline_kb())
    else:
        await c.answer("❌ Не найдена", show_alert=True)

@dp.callback_query(F.data == "admin_users")
async def admin_users(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    rows = await db_fetchall("SELECT user_id,premium_until,searches,refs FROM users ORDER BY user_id DESC LIMIT 30")
    if not rows:
        try:
            await c.message.edit_text("📭 Нет пользователей", reply_markup=get_admin_inline_kb())
        except:
            await c.message.answer("📭 Нет пользователей", reply_markup=get_admin_inline_kb())
        await c.answer()
        return
    text = "👥 *Последние пользователи:*\n"
    for r in rows:
        prem = "💎" if (r["premium_until"] == -1 or r["premium_until"] > int(time.time())) else "🔹"
        text += f"{prem} `{r['user_id']}` | поисков: {r['searches']} | реф: {r['refs']}\n"
    try:
        await c.message.edit_text(text, parse_mode="Markdown", reply_markup=get_admin_inline_kb())
    except:
        await c.message.answer(text, parse_mode="Markdown", reply_markup=get_admin_inline_kb())
    await c.answer()

@dp.callback_query(F.data == "admin_messages")
async def admin_messages(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    rows = await db_fetchall("SELECT * FROM support_messages WHERE status='unread' ORDER BY created_at DESC")
    if not rows:
        try:
            await c.message.edit_text("📭 Нет новых сообщений", reply_markup=get_admin_inline_kb())
        except:
            await c.message.answer("📭 Нет новых сообщений", reply_markup=get_admin_inline_kb())
        await c.answer()
        return
    await show_message(c, rows, 0)

async def show_message(c, messages, idx):
    m = messages[idx]
    text = f"📨 *Сообщение #{m['id']}*\n👤 `{m['user_id']}`\n📅 {time.strftime('%d.%m.%Y %H:%M',time.localtime(m['created_at']))}\n\n💬 {m['text']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Пометить прочитанным", callback_data=f"mark_read_{m['id']}")],
        [InlineKeyboardButton("◀ Назад", callback_data="admin_panel_back")]
    ])
    try:
        await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except:
        await c.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("mark_read_"))
async def mark_read(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    mid = int(c.data.split("_")[2])
    await db_execute("UPDATE support_messages SET status='read' WHERE id=$1", mid)
    remaining = await db_fetchall("SELECT * FROM support_messages WHERE status='unread' ORDER BY created_at DESC")
    if remaining:
        await show_message(c, remaining, 0)
    else:
        try:
            await c.message.edit_text("✅ Все сообщения прочитаны", reply_markup=get_admin_inline_kb())
        except:
            await c.message.answer("✅ Все сообщения прочитаны", reply_markup=get_admin_inline_kb())
    await c.answer()

# ================= FSM ДЛЯ ВЫДАЧИ PREMIUM И БАНА =================
class AdminPremiumState(StatesGroup):
    waiting_uid = State()
    waiting_days = State()
class AdminBanState(StatesGroup):
    waiting_uid = State()
    waiting_mins = State()

@dp.callback_query(F.data == "admin_premium_start")
async def admin_premium_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    if await state.get_state() is not None:
        await c.answer("⚠️ Сначала завершите текущую операцию", show_alert=True)
        return
    await state.set_state(AdminPremiumState.waiting_uid)
    try:
        await c.message.edit_text("🎁 *Выдача Premium*\nВведите ID пользователя:", parse_mode="Markdown")
    except:
        await c.message.answer("🎁 *Выдача Premium*\nВведите ID пользователя:", parse_mode="Markdown")
    await c.answer()

@dp.message(AdminPremiumState.waiting_uid)
async def admin_premium_uid(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID: return
    try:
        uid = int(m.text.strip())
        await state.update_data(uid=uid)
        await state.set_state(AdminPremiumState.waiting_days)
        await m.answer(f"👤 Пользователь: `{uid}`\nВведите количество дней (-1 = навсегда, 0 = снять):", parse_mode="Markdown")
    except:
        await m.answer("❌ Неверный ID")

@dp.message(AdminPremiumState.waiting_days)
async def admin_premium_days(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID: return
    try:
        days = int(m.text.strip())
        data = await state.get_data()
        uid = data['uid']
        await set_premium(uid, days)
        text = "навсегда" if days == -1 else f"{days} дней" if days > 0 else "снят"
        await m.answer(f"✅ Premium {text} для `{uid}`", parse_mode="Markdown")
        await m.bot.send_message(uid, f"🎉 *Premium {text}!*", parse_mode="Markdown")
        await state.clear()
        await m.answer("🔧 Админ панель", reply_markup=get_admin_inline_kb())
    except:
        await m.answer("❌ Ошибка")

@dp.callback_query(F.data == "admin_ban_start")
async def admin_ban_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != config.ADMIN_ID:
        await c.answer("⛔", show_alert=True)
        return
    if await state.get_state() is not None:
        await c.answer("⚠️ Сначала завершите текущую операцию", show_alert=True)
        return
    await state.set_state(AdminBanState.waiting_uid)
    try:
        await c.message.edit_text("🚫 *Бан пользователя*\nВведите ID пользователя:", parse_mode="Markdown")
    except:
        await c.message.answer("🚫 *Бан пользователя*\nВведите ID пользователя:", parse_mode="Markdown")
    await c.answer()

@dp.message(AdminBanState.waiting_uid)
async def admin_ban_uid(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID: return
    try:
        uid = int(m.text.strip())
        await state.update_data(uid=uid)
        await state.set_state(AdminBanState.waiting_mins)
        await m.answer(f"👤 Пользователь: `{uid}`\nВведите минуты бана (0 = снять):", parse_mode="Markdown")
    except:
        await m.answer("❌ Неверный ID")

@dp.message(AdminBanState.waiting_mins)
async def admin_ban_mins(m: Message, state: FSMContext):
    if m.from_user.id != config.ADMIN_ID: return
    try:
        mins = int(m.text.strip())
        data = await state.get_data()
        uid = data['uid']
        if mins == 0:
            await rate_limiter.unban_user(uid)
            await m.answer(f"✅ Бан снят с `{uid}`", parse_mode="Markdown")
            await m.bot.send_message(uid, "✅ *Бан снят!*", parse_mode="Markdown")
        else:
            await rate_limiter.ban_user(uid, mins*60)
            await m.answer(f"✅ Пользователь `{uid}` забанен на {mins} мин", parse_mode="Markdown")
            await m.bot.send_message(uid, f"🚫 *Бан на {mins} минут*", parse_mode="Markdown")
        await state.clear()
        await m.answer("🔧 Админ панель", reply_markup=get_admin_inline_kb())
    except:
        await m.answer("❌ Ошибка")

# ================= ПЛАТЕЖИ =================
class TxState(StatesGroup):
    waiting = State()

@dp.callback_query(F.data == "pay_stars")
async def stars_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 10★", callback_data="star_1d")],
        [InlineKeyboardButton(text="7 дней — 60★", callback_data="star_7d")],
        [InlineKeyboardButton(text="30 дней — 240★", callback_data="star_30d")],
        [InlineKeyboardButton(text="1 год — 800★", callback_data="star_1y")],
        [InlineKeyboardButton(text="Навсегда — 1500★", callback_data="star_forever")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    try:
        await c.message.edit_text("⭐ *Telegram Stars — выберите тариф:*", parse_mode="Markdown", reply_markup=kb)
    except:
        await c.message.answer("⭐ *Telegram Stars — выберите тариф:*", parse_mode="Markdown", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("star_"))
async def stars_pay(c: CallbackQuery):
    plan = c.data.split("_")[1]
    days = config.STARS_DAYS[plan]
    price = int(config.STARS_PRICES[plan])   # ← исправлено: int
    text = "навсегда" if days == -1 else f"{days} дн."
    try:
        await c.bot.send_invoice(
            chat_id=c.message.chat.id,
            title="Auron Premium",
            description=f"Premium на {text}",
            payload=f"prem_{days}",
            currency="XTR",
            prices=[LabeledPrice(label="Premium", amount=price)],
            start_parameter="auron_premium"
        )
    except Exception as e:
        await c.answer(f"Ошибка: {e}", show_alert=True)
    await c.answer()

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
        await c.message.edit_text(f"🪙 *USDT (TRC20)*\n📤 Кошелёк: `{config.USDT_WALLET}`\n\nВыберите тариф:", parse_mode="Markdown", reply_markup=kb)
    except:
        await c.message.answer(f"🪙 *USDT (TRC20)*\n📤 Кошелёк: `{config.USDT_WALLET}`\n\nВыберите тариф:", parse_mode="Markdown", reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("usdt_"))
async def usdt_request(c: CallbackQuery, state: FSMContext):
    if await state.get_state() is not None:
        await c.answer("⚠️ Сначала завершите текущую операцию", show_alert=True)
        return
    plan = c.data.split("_")[1]
    price = config.USDT_PRICES[plan]
    days = config.USDT_DAYS[plan]
    await state.update_data(plan=plan, days=days, amount=price)
    await state.set_state(TxState.waiting)
    await c.message.answer(
        f"💰 *Оплата USDT (TRC20)*\nСумма: {price} USDT\nТариф: {'навсегда' if days==-1 else f'{days} дн.'}\n\n"
        f"📤 Отправьте на кошелёк: `{config.USDT_WALLET}`\n\n📎 После отправки введите TxID:",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(TxState.waiting)
async def handle_txid(m: Message, state: FSMContext):
    txid = m.text.strip()
    if len(txid) < 30:
        await m.answer("❌ Неверный TxID")
        return
    data = await state.get_data()
    await db_execute(
        "INSERT INTO payments(user_id, method, plan, amount, txid, created_at, status) VALUES($1,'usdt',$2,$3,$4,$5,'wait')",
        m.from_user.id, data["plan"], data["amount"], txid, int(time.time())
    )
    await m.answer("✅ Заявка отправлена! Администратор проверит.", reply_markup=get_main_keyboard(m.from_user.id))
    await state.clear()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def payment_success(m: Message):
    days = int(m.successful_payment.invoice_payload.split("_")[1])
    await set_premium(m.from_user.id, days)
    await m.answer("✅ *Premium активирован!*", parse_mode="Markdown")

# ================= HEALTH CHECK =================
async def health(request):
    return web.Response(text="OK")

async def run_http():
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    while True:
        await asyncio.sleep(3600)

# ================= BACKGROUND =================
async def cleanup():
    while True:
        try:
            await db_execute("DELETE FROM temp_usernames WHERE expires_at < $1", int(time.time()))
            await rate_limiter.cleanup_bans()
        except:
            pass
        await asyncio.sleep(3600)

# ================= MAIN =================
async def main():
    await init_db()
    bot = Bot(token=config.BOT_TOKEN)
    asyncio.create_task(run_http())
    asyncio.create_task(cleanup())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("FATAL:", e)
        sys.exit(1)
