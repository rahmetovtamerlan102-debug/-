#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auron Search Bot - FINAL
- Инлайн админ-панель
- Проверка заявок USDT
- Сообщения поддержки
- Health check для Render
"""

import os
import sys
import time
import random
import string
import asyncio
import asyncpg
from asyncpg.exceptions import UniqueViolationError
import logging
from datetime import date
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
from collections import defaultdict

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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    admin_id: int = int(os.getenv("ADMIN_ID", "8276815852"))
    bot_username: str = os.getenv("BOT_USERNAME", "Auron_Search_Bot")
    environment: str = os.getenv("ENVIRONMENT", "production")
    port: int = int(os.getenv("PORT", "8080"))
    
    free_limit: int = 10
    max_ref_bonus: int = 20
    usdt_wallet: str = "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV"

config = Config()

print("=" * 50)
print("🚀 BOT INITIALIZATION")
print("=" * 50)
print(f"BOT_TOKEN: {'✅' if config.bot_token else '❌ MISSING'}")
print(f"DATABASE_URL: {'✅' if config.database_url else '❌ MISSING'}")
print(f"ADMIN_ID: {config.admin_id}")
print(f"PORT: {config.port}")
print("=" * 50)

if not config.bot_token:
    print("❌ FATAL: BOT_TOKEN not set!")
    sys.exit(1)
if not config.database_url:
    print("❌ FATAL: DATABASE_URL not set!")
    sys.exit(1)

# Тарифы
STARS_PRICES = {"1d": 10, "7d": 60, "30d": 240, "1y": 800, "forever": 1500}
STARS_DAYS = {"1d": 1, "7d": 7, "30d": 30, "1y": 365, "forever": -1}
USDT_PRICES = {"1d": 0.5, "7d": 3, "30d": 6, "1y": 25, "forever": 50}
USDT_DAYS = {"1d": 1, "7d": 7, "30d": 30, "1y": 365, "forever": -1}

# ================= КЛАВИАТУРЫ =================
def get_main_keyboard(uid: int = None) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🔍 Найти юзернейм"), KeyboardButton(text="💎 Купить Premium")],
        [KeyboardButton(text="👤 Личный кабинет"), KeyboardButton(text="🆘 Поддержка")]
    ]
    if uid == config.admin_id:
        buttons.append([KeyboardButton(text="🔧 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_admin_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💰 Заявки USDT", callback_data="admin_payments")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="📨 Сообщения", callback_data="admin_messages")],
        [InlineKeyboardButton(text="🎁 Выдать Premium", callback_data="admin_premium_start")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="admin_ban_start")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

def premium_request_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🪙 USDT (TRC20)", callback_data="pay_usdt")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

# ================= RATE LIMITER =================
class RateLimiter:
    def __init__(self):
        self.requests: Dict[str, list] = defaultdict(list)
        self.bans: Dict[str, float] = {}
    
    async def check(self, key: str, limit: int, window: int) -> Tuple[bool, int]:
        now = time.time()
        window_start = now - window
        self.requests[key] = [t for t in self.requests[key] if t > window_start]
        if len(self.requests[key]) >= limit:
            oldest = min(self.requests[key])
            wait_time = int(window - (now - oldest)) + 1
            return False, wait_time
        self.requests[key].append(now)
        return True, 0
    
    async def is_banned(self, user_id: int) -> Tuple[bool, int]:
        if user_id in self.bans and self.bans[user_id] > time.time():
            return True, int(self.bans[user_id] - time.time())
        return False, 0
    
    async def ban_user(self, user_id: int, duration: int):
        self.bans[user_id] = time.time() + duration
    
    async def unban_user(self, user_id: int):
        self.bans.pop(user_id, None)

# ================= MIDDLEWARE =================
class AntiSpamMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id
        if user_id:
            is_banned, ban_ttl = await app.rate_limiter.is_banned(user_id)
            if is_banned:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"🚫 Бан на {ban_ttl} сек.", show_alert=True)
                else:
                    await event.answer(f"🚫 Бан на {ban_ttl} сек.")
                return
            ok, wait = await app.rate_limiter.check(f"user:{user_id}", 20, 60)
            if not ok:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"⚠️ Подождите {wait} сек.", show_alert=True)
                else:
                    await event.answer(f"⚠️ Подождите {wait} сек.")
                return
        return await handler(event, data)

# ================= COMPONENTS =================
class App:
    def __init__(self):
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self.db: Optional[asyncpg.Pool] = None
        self.rate_limiter: RateLimiter = RateLimiter()
        self.start_time: float = time.time()

app = App()

# ================= DB HELPERS =================
async def db_execute(query: str, *args) -> str:
    if not app.db:
        raise RuntimeError("Database not initialized")
    async with app.db.acquire() as conn:
        return await conn.execute(query, *args)

async def db_fetch(query: str, *args):
    if not app.db:
        raise RuntimeError("Database not initialized")
    async with app.db.acquire() as conn:
        return await conn.fetch(query, *args)

async def db_fetchrow(query: str, *args):
    if not app.db:
        raise RuntimeError("Database not initialized")
    async with app.db.acquire() as conn:
        return await conn.fetchrow(query, *args)

async def db_fetchval(query: str, *args):
    if not app.db:
        raise RuntimeError("Database not initialized")
    async with app.db.acquire() as conn:
        return await conn.fetchval(query, *args)

# ================= USER HELPERS =================
async def get_user(uid: int) -> dict:
    u = await db_fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    if not u:
        await db_execute("INSERT INTO users(user_id, last_date) VALUES($1, $2)", uid, str(date.today()))
        u = await db_fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    if not u:
        return {"premium_until": 0, "searches": 0, "refs": 0, "ref_by": 0}
    today = str(date.today())
    if u["last_date"] != today:
        await db_execute("UPDATE users SET searches=0, last_date=$1 WHERE user_id=$2", today, uid)
        u = await db_fetchrow("SELECT * FROM users WHERE user_id=$1", uid)
    return dict(u)

def is_premium(user: dict) -> bool:
    until = user.get("premium_until", 0)
    return until == -1 or until > int(time.time())

def get_limit(user: dict) -> float:
    if is_premium(user):
        return float("inf")
    refs = max(0, user.get("refs", 0))
    return config.free_limit + min(refs, config.max_ref_bonus)

async def can_search(uid: int) -> Tuple[bool, str]:
    user = await get_user(uid)
    limit = get_limit(user)
    if limit == float("inf"):
        return True, ""
    if user.get("searches", 0) >= limit:
        return False, f"Лимит {user['searches']}/{limit}"
    return True, ""

async def inc_search(uid: int) -> int:
    async with app.db.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE users SET searches = searches + 1 WHERE user_id=$1 RETURNING searches",
            uid
        )
        return row["searches"] if row else 0

async def set_premium(uid: int, days: int):
    if days == 0:
        until = 0
    elif days == -1:
        until = -1
    else:
        until = int(time.time()) + days * 86400
    await db_execute(
        "INSERT INTO users(user_id, premium_until) VALUES($1, $2) ON CONFLICT(user_id) DO UPDATE SET premium_until=$2",
        uid, until
    )

async def add_referral(uid: int, ref_id: int) -> bool:
    if uid == ref_id:
        return False
    user = await get_user(uid)
    if user.get("ref_by", 0) != 0:
        return False
    async with app.db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE users SET ref_by=$1 WHERE user_id=$2", ref_id, uid)
            await conn.execute("UPDATE users SET refs = refs + 1 WHERE user_id=$1", ref_id)
    return True

def gen_username(length: int, has_digit: bool) -> str:
    letters = 'abcdefghijkmnopqrstuvwxyz'
    if not has_digit:
        return "".join(random.choice(letters) for _ in range(length))
    chars = [random.choice(letters) for _ in range(length)]
    chars[random.randint(0, length - 1)] = str(random.randint(0, 9))
    return "".join(chars)

# ================= КЛАВИАТУРЫ ГЕНЕРАЦИИ =================
async def length_inline_kb(uid: int) -> InlineKeyboardMarkup:
    user = await get_user(uid)
    buttons = [6, 7]
    row = [InlineKeyboardButton(text="6", callback_data="len_6"),
           InlineKeyboardButton(text="7", callback_data="len_7")]
    if is_premium(user):
        row.insert(0, InlineKeyboardButton(text="5 (Premium)", callback_data="len_5"))
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="◀ Назад в меню", callback_data="back_main")]
    ])

def mode_inline_kb(length: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 Буквы", callback_data=f"mode_{length}_0"),
         InlineKeyboardButton(text="🔢 Буквы+цифра", callback_data=f"mode_{length}_1")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_length")]
    ])

def result_inline_kb(length: int, has_digit: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить", callback_data="save"),
         InlineKeyboardButton(text="🔄 Ещё", callback_data=f"mode_{length}_{has_digit}")],
        [InlineKeyboardButton(text="◀ Назад в меню", callback_data="back_main")]
    ])

# ================= ОСНОВНЫЕ ХЕНДЛЕРЫ =================
async def cmd_start(m: Message):
    args = m.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1].split("_")[1])
            await add_referral(m.from_user.id, ref_id)
        except:
            pass
    await m.answer(
        "👋 *Приветствуем в Auron Search!*\n\n"
        "Наш бот — профессиональный инструмент для поиска свободных юзернеймов.\n\n"
        "Выберите нужное действие в меню ниже 🎉",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(m.from_user.id)
    )

async def cmd_find_username(m: Message):
    user = await get_user(m.from_user.id)
    limit = get_limit(user)
    searches = user.get("searches", 0)
    remaining_text = "∞" if limit == float("inf") else str(max(0, int(limit) - searches))
    kb = await length_inline_kb(m.from_user.id)
    await m.answer(
        f"📏 *Выберите длину username*\n\n🎫 Осталось попыток: {remaining_text}",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def cmd_premium(m: Message):
    await m.answer(
        "💎 *Premium доступ*\n\n✅ Безлимитная генерация\n✅ Длина 5 символов\n\nВыберите способ оплаты:",
        parse_mode="Markdown",
        reply_markup=premium_request_inline_kb()
    )

async def cmd_profile(m: Message):
    user = await get_user(m.from_user.id)
    limit = get_limit(user)
    searches = user.get("searches", 0)
    searches_text = f"{searches} / ∞" if limit == float("inf") else f"{searches} / {limit}"
    remaining = "∞" if limit == float("inf") else max(0, int(limit) - searches)
    until = user.get('premium_until', 0)
    if until == -1:
        prem_text = "✅ Навсегда"
    elif until > int(time.time()):
        prem_text = f"✅ До {time.strftime('%d.%m.%Y', time.localtime(until))}"
    else:
        prem_text = "❌ Нет"
    link = f"https://t.me/{config.bot_username}?start=ref_{m.from_user.id}"
    saved_count = await db_fetchval("SELECT COUNT(*) FROM saved WHERE user_id=$1", m.from_user.id)
    await m.answer(
        f"👤 *Личный кабинет*\n\n"
        f"🔎 Использовано: {searches_text}\n"
        f"🎫 Осталось: {remaining}\n"
        f"👥 Приглашено: {user.get('refs', 0)}\n"
        f"💎 Premium: {prem_text}\n"
        f"💾 Сохранено: {saved_count or 0} username\n\n"
        f"🔗 *Реферальная ссылка:*\n`{link}`\n\n"
        f"📋 Сохранённые: /my_usernames",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(m.from_user.id)
    )

async def cmd_my_usernames(m: Message):
    rows = await db_fetch("SELECT username FROM saved WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30", m.from_user.id)
    if not rows:
        await m.answer("📭 У вас нет сохранённых username.", reply_markup=get_main_keyboard(m.from_user.id))
        return
    await m.answer(
        "💾 *Ваши сохранённые username:*\n\n" + "\n".join(f"• @{r['username']}" for r in rows),
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(m.from_user.id)
    )

# ================= ПОДДЕРЖКА =================
class SupportState(StatesGroup):
    waiting_for_message = State()

async def cmd_support(m: Message, state: FSMContext):
    await state.set_state(SupportState.waiting_for_message)
    await m.answer(
        "📝 *Напишите ваше сообщение администратору*\n\n"
        "Администратор ответит вам в личные сообщения.\n"
        "/cancel — отмена",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(m.from_user.id)
    )

@dp.message(SupportState.waiting_for_message)
async def handle_support_message(m: Message, state: FSMContext):
    if m.text == "/cancel":
        await state.clear()
        await m.answer("❌ Отменено", reply_markup=get_main_keyboard(m.from_user.id))
        return
    await db_execute(
        "INSERT INTO support_messages(user_id, text, created_at, status) VALUES($1, $2, $3, 'unread')",
        m.from_user.id, m.text[:1000], int(time.time())
    )
    await app.bot.send_message(
        config.admin_id,
        f"📨 *Новое сообщение от пользователя*\n\n"
        f"👤 ID: `{m.from_user.id}`\n"
        f"👤 Username: @{m.from_user.username or 'нет'}\n\n"
        f"💬 Сообщение:\n`{m.text[:500]}`",
        parse_mode="Markdown"
    )
    await m.answer(
        "✅ *Сообщение отправлено администратору!*\n\nОтвет придёт в личные сообщения.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(m.from_user.id)
    )
    await state.clear()

# ================= CALLBACK ХЕНДЛЕРЫ =================
async def cb_back_main(c: CallbackQuery):
    await c.message.delete()
    await c.message.answer(
        "👋 *Auron Search*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(c.from_user.id)
    )
    await c.answer()

async def cb_back_to_length(c: CallbackQuery):
    user = await get_user(c.from_user.id)
    limit = get_limit(user)
    searches = user.get("searches", 0)
    remaining_text = "∞" if limit == float("inf") else str(max(0, int(limit) - searches))
    kb = await length_inline_kb(c.from_user.id)
    await c.message.edit_text(
        f"📏 *Выберите длину username*\n\n🎫 Осталось попыток: {remaining_text}",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await c.answer()

async def cb_choose_len(c: CallbackQuery):
    try:
        length = int(c.data.split("_")[1])
    except:
        return await c.answer("❌ Ошибка", show_alert=True)
    if length == 5:
        user = await get_user(c.from_user.id)
        if not is_premium(user):
            msg = await c.message.answer("❌ Длина 5 доступна только Premium! Купите Premium в разделе 💎")
            await asyncio.sleep(3)
            await msg.delete()
            return
    await c.message.edit_text(
        f"⚙️ *Длина: {length}*\n\nВыберите режим генерации:",
        parse_mode="Markdown",
        reply_markup=mode_inline_kb(length)
    )
    await c.answer()

async def cb_generate(c: CallbackQuery):
    try:
        _, length_str, has_digit_str = c.data.split("_", 2)
        length = int(length_str)
        has_digit = int(has_digit_str)
    except:
        return await c.answer("❌ Ошибка", show_alert=True)
    uid = c.from_user.id
    if length == 5:
        user = await get_user(uid)
        if not is_premium(user):
            msg = await c.message.answer("❌ Длина 5 только для Premium")
            await asyncio.sleep(3)
            await msg.delete()
            return
    can, reason = await can_search(uid)
    if not can:
        return await c.answer(f"❌ {reason}", show_alert=True)
    user = await get_user(uid)
    username = gen_username(length, has_digit)
    new_searches = await inc_search(uid)
    await db_execute(
        "INSERT INTO temp_usernames(user_id, username, created_at, expires_at) VALUES($1, $2, $3, $4) ON CONFLICT(user_id) DO UPDATE SET username=$2, expires_at=$4",
        uid, username, int(time.time()), int(time.time()) + 3600
    )
    limit = get_limit(user)
    remaining = "" if limit == float("inf") else f"\n\n🎫 Осталось: {max(0, int(limit) - new_searches)}"
    await c.message.edit_text(
        f"✅ *Найден юзернейм:*\n\n`@{username}`{remaining}",
        parse_mode="Markdown",
        reply_markup=result_inline_kb(length, has_digit)
    )
    await c.answer()

async def cb_save(c: CallbackQuery):
    row = await db_fetchrow("SELECT username FROM temp_usernames WHERE user_id=$1 AND expires_at > $2", c.from_user.id, int(time.time()))
    if not row:
        return await c.answer("❌ Нечего сохранять", show_alert=True)
    try:
        await db_execute("INSERT INTO saved(user_id, username, created_at) VALUES($1, $2, $3)",
                        c.from_user.id, row['username'], int(time.time()))
        await c.answer("✅ Сохранено!", show_alert=True)
    except UniqueViolationError:
        await c.answer("⚠️ Уже сохранён", show_alert=True)

# ================= АДМИН ПАНЕЛЬ (ИНЛАЙН) =================
async def cmd_admin_panel(m: Message):
    if m.from_user.id != config.admin_id:
        await m.answer("⛔ Доступ запрещён!", reply_markup=get_main_keyboard(m.from_user.id))
        return
    await m.answer(
        "🔧 *Админ панель*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admin_inline_kb()
    )

async def admin_stats(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    users = await db_fetchval("SELECT COUNT(*) FROM users")
    prem = await db_fetchval("SELECT COUNT(*) FROM users WHERE premium_until=-1 OR premium_until>$1", int(time.time()))
    saved = await db_fetchval("SELECT COUNT(*) FROM saved")
    payments = await db_fetchval("SELECT COUNT(*) FROM payments WHERE status='wait'")
    support = await db_fetchval("SELECT COUNT(*) FROM support_messages WHERE status='unread'")
    uptime = int(time.time() - app.start_time)
    hours, minutes = uptime // 3600, (uptime % 3600) // 60
    await c.message.edit_text(
        f"📊 *Статистика*\n\n"
        f"👥 Пользователи: {users}\n"
        f"💎 Premium: {prem}\n"
        f"💾 Сохранений: {saved}\n"
        f"💰 Заявок USDT: {payments}\n"
        f"📨 Сообщений: {support}\n"
        f"⏱ Работает: {hours}ч {minutes}м",
        parse_mode="Markdown",
        reply_markup=get_admin_inline_kb()
    )
    await c.answer()

async def admin_payments(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    rows = await db_fetch("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
    if not rows:
        await c.message.edit_text("📭 Нет ожидающих заявок", reply_markup=get_admin_inline_kb())
        await c.answer()
        return
    # Показываем первую заявку
    await show_payment(c, rows, 0)

async def show_payment(c: CallbackQuery, payments, idx):
    p = payments[idx]
    text = (
        f"💰 *Заявка #{p['id']}*\n"
        f"👤 ID: `{p['user_id']}`\n"
        f"💵 Сумма: {p['amount']} USDT\n"
        f"📎 TxID: `{p['txid'][:40]}...`\n"
        f"📅 {time.strftime('%d.%m.%Y %H:%M', time.localtime(p['created_at']))}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_{p['id']}"),
         InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{p['id']}")],
        [InlineKeyboardButton("◀ Назад в админку", callback_data="admin_panel_back")]
    ])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await c.answer()

async def approve_payment(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    pid = int(c.data.split("_")[1])
    pay = await db_fetchrow("SELECT user_id, plan FROM payments WHERE id=$1", pid)
    if pay:
        days = USDT_DAYS.get(pay["plan"], 30)
        await set_premium(pay["user_id"], days)
        await db_execute("UPDATE payments SET status='approved' WHERE id=$1", pid)
        await app.bot.send_message(pay["user_id"], "✅ *Premium активирован!*", parse_mode="Markdown")
        await c.answer("✅ Подтверждено")
        # показать оставшиеся заявки
        remaining = await db_fetch("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
        if remaining:
            await show_payment(c, remaining, 0)
        else:
            await c.message.edit_text("✅ Все заявки обработаны", reply_markup=get_admin_inline_kb())
    else:
        await c.answer("❌ Заявка не найдена", show_alert=True)

async def reject_payment(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    pid = int(c.data.split("_")[1])
    pay = await db_fetchrow("SELECT user_id FROM payments WHERE id=$1", pid)
    if pay:
        await db_execute("UPDATE payments SET status='rejected' WHERE id=$1", pid)
        await app.bot.send_message(pay["user_id"], "❌ *Заявка отклонена*", parse_mode="Markdown")
        await c.answer("❌ Отклонено")
        remaining = await db_fetch("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
        if remaining:
            await show_payment(c, remaining, 0)
        else:
            await c.message.edit_text("✅ Все заявки обработаны", reply_markup=get_admin_inline_kb())
    else:
        await c.answer("❌ Заявка не найдена", show_alert=True)

async def admin_users(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    rows = await db_fetch("SELECT user_id, premium_until, searches, refs FROM users ORDER BY user_id DESC LIMIT 30")
    if not rows:
        await c.message.edit_text("📭 Нет пользователей", reply_markup=get_admin_inline_kb())
        await c.answer()
        return
    text = "👥 *Последние пользователи:*\n\n"
    for r in rows:
        prem = "💎" if is_premium(dict(r)) else "🔹"
        text += f"{prem} ID: `{r['user_id']}` | Поисков: {r['searches']} | Реф: {r['refs']}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=get_admin_inline_kb())
    await c.answer()

async def admin_messages(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    rows = await db_fetch("SELECT * FROM support_messages WHERE status='unread' ORDER BY created_at DESC")
    if not rows:
        await c.message.edit_text("📭 Нет новых сообщений", reply_markup=get_admin_inline_kb())
        await c.answer()
        return
    await show_message(c, rows, 0)

async def show_message(c: CallbackQuery, messages, idx):
    m = messages[idx]
    text = (
        f"📨 *Сообщение #{m['id']}*\n"
        f"👤 ID: `{m['user_id']}`\n"
        f"📅 {time.strftime('%d.%m.%Y %H:%M', time.localtime(m['created_at']))}\n\n"
        f"💬 {m['text']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Пометить прочитанным", callback_data=f"mark_read_{m['id']}")],
        [InlineKeyboardButton("◀ Назад в админку", callback_data="admin_panel_back")]
    ])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await c.answer()

async def mark_message_read(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    msg_id = int(c.data.split("_")[2])
    await db_execute("UPDATE support_messages SET status='read' WHERE id=$1", msg_id)
    remaining = await db_fetch("SELECT * FROM support_messages WHERE status='unread' ORDER BY created_at DESC")
    if remaining:
        await show_message(c, remaining, 0)
    else:
        await c.message.edit_text("✅ Все сообщения прочитаны", reply_markup=get_admin_inline_kb())
    await c.answer()

async def admin_panel_back(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await c.message.edit_text(
        "🔧 *Админ панель*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admin_inline_kb()
    )
    await c.answer()

# ================= FSM ДЛЯ ВЫДАЧИ PREMIUM И БАНА =================
class AdminPremiumState(StatesGroup):
    waiting_user_id = State()
    waiting_days = State()

class AdminBanState(StatesGroup):
    waiting_user_id = State()
    waiting_minutes = State()

async def admin_premium_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminPremiumState.waiting_user_id)
    await c.message.edit_text(
        "🎁 *Выдача Premium*\n\nВведите **ID пользователя**:",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(AdminPremiumState.waiting_user_id)
async def admin_premium_get_id(m: Message, state: FSMContext):
    if m.from_user.id != config.admin_id:
        return
    try:
        user_id = int(m.text.strip())
        await state.update_data(user_id=user_id)
        await state.set_state(AdminPremiumState.waiting_days)
        await m.answer(
            f"👤 Пользователь: `{user_id}`\n\n"
            "Введите **количество дней**:\n"
            "`-1` — навсегда\n"
            "`0` — снять Premium\n"
            "`30` — 30 дней и т.д.",
            parse_mode="Markdown"
        )
    except:
        await m.answer("❌ Неверный ID! Введите число.")

@dp.message(AdminPremiumState.waiting_days)
async def admin_premium_set_days(m: Message, state: FSMContext):
    if m.from_user.id != config.admin_id:
        return
    try:
        days = int(m.text.strip())
        data = await state.get_data()
        uid = data['user_id']
        await set_premium(uid, days)
        text = "навсегда" if days == -1 else f"{days} дней" if days > 0 else "снят"
        await m.answer(f"✅ Premium {text} для пользователя `{uid}`", parse_mode="Markdown")
        await app.bot.send_message(uid, f"🎉 *Premium {text}!*\n\nБезлимитная генерация и длина 5.", parse_mode="Markdown")
        await state.clear()
        # вернуться в админку
        await m.answer("🔧 Админ панель", reply_markup=get_admin_inline_kb())
    except:
        await m.answer("❌ Ошибка! Введите число.")

async def admin_ban_start(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != config.admin_id:
        await c.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await state.set_state(AdminBanState.waiting_user_id)
    await c.message.edit_text(
        "🚫 *Бан пользователя*\n\nВведите **ID пользователя**:",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(AdminBanState.waiting_user_id)
async def admin_ban_get_id(m: Message, state: FSMContext):
    if m.from_user.id != config.admin_id:
        return
    try:
        user_id = int(m.text.strip())
        await state.update_data(user_id=user_id)
        await state.set_state(AdminBanState.waiting_minutes)
        await m.answer(
            f"👤 Пользователь: `{user_id}`\n\n"
            "Введите **количество минут** бана:\n"
            "`0` — снять бан\n"
            "`5` — 5 минут\n"
            "`1440` — 1 день",
            parse_mode="Markdown"
        )
    except:
        await m.answer("❌ Неверный ID! Введите число.")

@dp.message(AdminBanState.waiting_minutes)
async def admin_ban_set_minutes(m: Message, state: FSMContext):
    if m.from_user.id != config.admin_id:
        return
    try:
        minutes = int(m.text.strip())
        data = await state.get_data()
        uid = data['user_id']
        if minutes == 0:
            await app.rate_limiter.unban_user(uid)
            await m.answer(f"✅ Бан снят с пользователя `{uid}`", parse_mode="Markdown")
            await app.bot.send_message(uid, "✅ *Ваш бан снят!*", parse_mode="Markdown")
        else:
            await app.rate_limiter.ban_user(uid, minutes * 60)
            await m.answer(f"✅ Пользователь `{uid}` забанен на {minutes} мин", parse_mode="Markdown")
            await app.bot.send_message(uid, f"🚫 *Вы забанены на {minutes} минут!*", parse_mode="Markdown")
        await state.clear()
        # вернуться в админку
        await m.answer("🔧 Админ панель", reply_markup=get_admin_inline_kb())
    except:
        await m.answer("❌ Ошибка! Введите число.")

# ================= ПЛАТЕЖИ =================
class TxState(StatesGroup):
    waiting = State()

async def cb_stars_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 10★", callback_data="star_1d")],
        [InlineKeyboardButton(text="7 дней — 60★", callback_data="star_7d")],
        [InlineKeyboardButton(text="30 дней — 240★", callback_data="star_30d")],
        [InlineKeyboardButton(text="1 год — 800★", callback_data="star_1y")],
        [InlineKeyboardButton(text="Навсегда — 1500★", callback_data="star_forever")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await c.message.edit_text("⭐ *Telegram Stars — выберите тариф:*", parse_mode="Markdown", reply_markup=kb)
    await c.answer()

async def cb_stars_pay(c: CallbackQuery):
    plan = c.data.split("_")[1]
    days = STARS_DAYS[plan]
    price = STARS_PRICES[plan]
    text = "навсегда" if days == -1 else f"{days} дн."
    try:
        await app.bot.send_invoice(
            chat_id=c.message.chat.id,
            title="Auron Premium",
            description=f"Premium на {text}",
            payload=f"prem_{days}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Premium", amount=price)],
            start_parameter="auron_premium"
        )
    except Exception as e:
        await c.answer(f"Ошибка: {e}", show_alert=True)
    await c.answer()

async def cb_usdt_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 0.5 USDT", callback_data="usdt_1d")],
        [InlineKeyboardButton(text="7 дней — 3 USDT", callback_data="usdt_7d")],
        [InlineKeyboardButton(text="30 дней — 6 USDT", callback_data="usdt_30d")],
        [InlineKeyboardButton(text="1 год — 25 USDT", callback_data="usdt_1y")],
        [InlineKeyboardButton(text="Навсегда — 50 USDT", callback_data="usdt_forever")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await c.message.edit_text(
        f"🪙 *USDT (TRC20)*\n\n📤 Кошелёк:\n`{config.usdt_wallet}`\n\nВыберите тариф:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await c.answer()

async def cb_usdt_request(c: CallbackQuery, state: FSMContext):
    plan = c.data.split("_")[1]
    price = USDT_PRICES[plan]
    days = USDT_DAYS[plan]
    await state.update_data(plan=plan, days=days, amount=price)
    await state.set_state(TxState.waiting)
    await c.message.answer(
        f"💰 *Оплата USDT (TRC20)*\n\nСумма: {price} USDT\nТариф: {'навсегда' if days == -1 else f'{days} дн.'}\n\n"
        f"📤 Отправьте на кошелёк:\n`{config.usdt_wallet}`\n\n📎 После отправки введите TxID:",
        parse_mode="Markdown"
    )
    await c.answer()

async def handle_txid(m: Message, state: FSMContext):
    txid = m.text.strip()
    if len(txid) < 30:
        await m.answer("❌ Неверный TxID. Попробуйте снова:")
        return
    data = await state.get_data()
    await db_execute(
        "INSERT INTO payments(user_id, method, plan, amount, txid, created_at, status) VALUES($1, 'usdt', $2, $3, $4, $5, 'wait')",
        m.from_user.id, data["plan"], data["amount"], txid, int(time.time())
    )
    await m.answer("✅ Заявка отправлена! Администратор проверит.", reply_markup=get_main_keyboard(m.from_user.id))
    await state.clear()

async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

async def payment_success(m: Message):
    days = int(m.successful_payment.invoice_payload.split("_")[1])
    await set_premium(m.from_user.id, days)
    await m.answer("✅ *Premium активирован!*", parse_mode="Markdown")

# ================= DB INIT =================
async def init_db():
    app.db = await asyncpg.create_pool(
        config.database_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        ssl="require"
    )
    async with app.db.acquire() as conn:
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
                id SERIAL,
                user_id BIGINT,
                username TEXT,
                created_at BIGINT,
                UNIQUE(user_id, username)
            );
            CREATE TABLE IF NOT EXISTS payments(
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                method TEXT,
                plan TEXT,
                amount FLOAT,
                txid TEXT,
                status TEXT DEFAULT 'wait',
                created_at BIGINT
            );
            CREATE TABLE IF NOT EXISTS temp_usernames(
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                created_at BIGINT,
                expires_at BIGINT
            );
            CREATE TABLE IF NOT EXISTS support_messages(
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                text TEXT,
                created_at BIGINT,
                status TEXT DEFAULT 'unread'
            );
        """)
    logger.info("✅ Database connected")

# ================= HEALTH CHECK HTTP =================
async def health(request):
    return web.Response(text="OK")

async def run_http():
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.port)
    await site.start()
    while True:
        await asyncio.sleep(3600)

# ================= BACKGROUND =================
async def cleanup_task():
    while True:
        try:
            await db_execute("DELETE FROM temp_usernames WHERE expires_at < $1", int(time.time()))
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)

# ================= MAIN =================
async def main():
    print("🔵 MAIN FUNCTION STARTED")
    await init_db()

    app.dp = Dispatcher(storage=MemoryStorage())
    app.dp.message.middleware(AntiSpamMiddleware())
    app.dp.callback_query.middleware(AntiSpamMiddleware())

    # Регистрация хендлеров
    app.dp.message.register(cmd_start, Command("start"))
    app.dp.message.register(cmd_find_username, F.text == "🔍 Найти юзернейм")
    app.dp.message.register(cmd_premium, F.text == "💎 Купить Premium")
    app.dp.message.register(cmd_profile, F.text == "👤 Личный кабинет")
    app.dp.message.register(cmd_support, F.text == "🆘 Поддержка")
    app.dp.message.register(cmd_my_usernames, Command("my_usernames"))
    app.dp.message.register(cmd_admin_panel, F.text == "🔧 Админ панель")

    # Админ-колбэки
    app.dp.callback_query.register(admin_stats, F.data == "admin_stats")
    app.dp.callback_query.register(admin_payments, F.data == "admin_payments")
    app.dp.callback_query.register(admin_users, F.data == "admin_users")
    app.dp.callback_query.register(admin_messages, F.data == "admin_messages")
    app.dp.callback_query.register(admin_premium_start, F.data == "admin_premium_start")
    app.dp.callback_query.register(admin_ban_start, F.data == "admin_ban_start")
    app.dp.callback_query.register(admin_panel_back, F.data == "admin_panel_back")
    app.dp.callback_query.register(approve_payment, F.data.startswith("approve_"))
    app.dp.callback_query.register(reject_payment, F.data.startswith("reject_"))
    app.dp.callback_query.register(mark_message_read, F.data.startswith("mark_read_"))
    app.dp.callback_query.register(cb_back_main, F.data == "back_main")
    app.dp.callback_query.register(cb_back_to_length, F.data == "back_to_length")
    app.dp.callback_query.register(cb_choose_len, F.data.startswith("len_"))
    app.dp.callback_query.register(cb_generate, F.data.startswith("mode_"))
    app.dp.callback_query.register(cb_save, F.data == "save")
    app.dp.callback_query.register(cb_stars_menu, F.data == "pay_stars")
    app.dp.callback_query.register(cb_stars_pay, F.data.startswith("star_"))
    app.dp.callback_query.register(cb_usdt_menu, F.data == "pay_usdt")
    app.dp.callback_query.register(cb_usdt_request, F.data.startswith("usdt_"))
    app.dp.callback_query.register(pre_checkout, F.data == "pre_checkout")

    app.dp.message.register(handle_support_message, SupportState.waiting_for_message)
    app.dp.message.register(admin_premium_get_id, AdminPremiumState.waiting_user_id)
    app.dp.message.register(admin_premium_set_days, AdminPremiumState.waiting_days)
    app.dp.message.register(admin_ban_get_id, AdminBanState.waiting_user_id)
    app.dp.message.register(admin_ban_set_minutes, AdminBanState.waiting_minutes)
    app.dp.message.register(handle_txid, TxState.waiting)
    app.dp.message.register(payment_success, F.successful_payment)

    app.bot = Bot(token=config.bot_token)
    asyncio.create_task(cleanup_task())
    asyncio.create_task(run_http())
    logger.info("🤖 BOT IS NOW POLLING...")
    await app.dp.start_polling(app.bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
