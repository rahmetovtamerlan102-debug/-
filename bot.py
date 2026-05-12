#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auron Search Bot - FINAL PRODUCTION
Запускается на VPS с webhook и PostgreSQL
"""

import os
import re
import time
import random
import string
import asyncio
import asyncpg
from asyncpg.exceptions import UniqueViolationError
import json
import logging
from datetime import date
from aiohttp import web
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, Update
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
    webhook_url: str = os.getenv("WEBHOOK_URL", "")
    webhook_path: str = os.getenv("WEBHOOK_PATH", "/webhook")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    admin_id: int = int(os.getenv("ADMIN_ID", "8276815852"))
    bot_username: str = os.getenv("BOT_USERNAME", "AuronSearchBot")
    environment: str = os.getenv("ENVIRONMENT", "production")
    port: int = int(os.getenv("PORT", "8080"))
    
    free_limit: int = 10
    max_ref_bonus: int = 20
    usdt_wallet: str = "TQ1hHPveZ737G5i1ZxHN2sfpV9PSdx5nfV"

config = Config()

# Проверка переменных (мягкая, чтобы polling тоже работал)
if config.bot_token:
    logger.info("✅ BOT_TOKEN loaded")
else:
    logger.error("❌ BOT_TOKEN not set")

# Тарифы
STARS_PRICES = {"1d": 10, "7d": 60, "30d": 240, "1y": 800, "forever": 1500}
STARS_DAYS = {"1d": 1, "7d": 7, "30d": 30, "1y": 365, "forever": -1}
USDT_PRICES = {"1d": 0.5, "7d": 3, "30d": 6, "1y": 25, "forever": 50}
USDT_DAYS = {"1d": 1, "7d": 7, "30d": 30, "1y": 365, "forever": -1}

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
    async def __call__(self, handler, event: any, data: dict):
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
            
            ok, wait = await app.rate_limiter.check(f"user:{user_id}", 10, 60)
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
    async with app.db.acquire() as conn:
        return await conn.execute(query, *args)

async def db_fetch(query: str, *args):
    async with app.db.acquire() as conn:
        return await conn.fetch(query, *args)

async def db_fetchrow(query: str, *args):
    async with app.db.acquire() as conn:
        return await conn.fetchrow(query, *args)

async def db_fetchval(query: str, *args):
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

# ================= KEYBOARDS =================
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Генерация", callback_data="gen")],
        [InlineKeyboardButton(text="💎 Premium", callback_data="premium")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ])

async def length_kb(uid: int) -> InlineKeyboardMarkup:
    user = await get_user(uid)
    if is_premium(user):
        btns = [
            InlineKeyboardButton(text="5⭐", callback_data="len_5"),
            InlineKeyboardButton(text="6", callback_data="len_6"),
            InlineKeyboardButton(text="7", callback_data="len_7")
        ]
    else:
        btns = [
            InlineKeyboardButton(text="6", callback_data="len_6"),
            InlineKeyboardButton(text="7", callback_data="len_7")
        ]
    return InlineKeyboardMarkup(inline_keyboard=[
        btns,
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])

def mode_kb(length: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 Буквы", callback_data=f"mode_{length}_0"),
         InlineKeyboardButton(text="🔢 С цифрой", callback_data=f"mode_{length}_1")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="gen")]
    ])

def result_kb(length: int, has_digit: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить", callback_data="save"),
         InlineKeyboardButton(text="🔄 Ещё", callback_data=f"mode_{length}_{has_digit}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="gen")]
    ])

# ================= HANDLERS =================
async def cmd_start(m: Message):
    args = m.text.split()
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1].split("_")[1])
            await add_referral(m.from_user.id, ref_id)
        except:
            pass
    
    await m.answer(
        "👋 *Auron Search*\n\n"
        "Генератор свободных username.\n\n"
        f"🔓 Бесплатно: {config.free_limit} генераций/день\n"
        f"👥 За рефералов: +{config.max_ref_bonus} генераций\n"
        f"💎 Premium: безлимит + длина 5\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

async def cmd_my_usernames(m: Message):
    rows = await db_fetch("SELECT username FROM saved WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30", m.from_user.id)
    if not rows:
        await m.answer("📭 Нет сохранённых username.")
        return
    await m.answer("💾 Сохранённые:\n\n" + "\n".join(f"• @{r['username']}" for r in rows))

async def cb_back_main(c: CallbackQuery):
    await c.message.edit_text(
        "👋 *Auron Search*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    await c.answer()

async def cb_gen_menu(c: CallbackQuery):
    kb = await length_kb(c.from_user.id)
    await c.message.edit_text("📏 Выберите длину username:", reply_markup=kb)
    await c.answer()

async def cb_choose_len(c: CallbackQuery):
    try:
        length = int(c.data.split("_")[1])
    except:
        return await c.answer("❌ Ошибка", show_alert=True)
    await c.message.edit_text(f"⚙️ Длина: {length}\n\nВыберите режим:", reply_markup=mode_kb(length))
    await c.answer()

async def cb_generate(c: CallbackQuery):
    try:
        _, length_str, has_digit_str = c.data.split("_", 2)
        length = int(length_str)
        has_digit = int(has_digit_str)
    except:
        return await c.answer("❌ Ошибка", show_alert=True)
    
    uid = c.from_user.id
    
    can, reason = await can_search(uid)
    if not can:
        return await c.answer(f"❌ {reason}", show_alert=True)
    
    user = await get_user(uid)
    if length == 5 and not is_premium(user):
        return await c.answer("❌ Длина 5 только для Premium", show_alert=True)
    
    username = gen_username(length, has_digit)
    new_searches = await inc_search(uid)
    
    await db_execute(
        "INSERT INTO temp_usernames(user_id, username, created_at, expires_at) VALUES($1, $2, $3, $4) ON CONFLICT(user_id) DO UPDATE SET username=$2, expires_at=$4",
        uid, username, int(time.time()), int(time.time()) + 3600
    )
    
    limit = get_limit(user)
    if limit == float("inf"):
        remaining = ""
    else:
        remaining = f"\n\n🎫 Осталось: {max(0, int(limit) - new_searches)}"
    
    await c.message.edit_text(f"✅ @{username}{remaining}", reply_markup=result_kb(length, has_digit))
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

async def cb_profile(c: CallbackQuery):
    user = await get_user(c.from_user.id)
    limit = get_limit(user)
    searches = user.get("searches", 0)
    
    if limit == float("inf"):
        searches_text = f"{searches} / ∞"
        remaining = "∞"
    else:
        searches_text = f"{searches} / {limit}"
        remaining = max(0, int(limit) - searches)
    
    until = user.get('premium_until', 0)
    if until == -1:
        prem_text = "✅ Навсегда"
    elif until > int(time.time()):
        prem_text = f"✅ До {time.strftime('%d.%m.%Y', time.localtime(until))}"
    else:
        prem_text = "❌ Нет"
    
    link = f"https://t.me/{config.bot_username}?start=ref_{c.from_user.id}"
    
    await c.message.edit_text(
        f"👤 *Профиль*\n\n"
        f"🔎 Использовано: {searches_text}\n"
        f"🎫 Осталось: {remaining}\n"
        f"👥 Рефералы: {user.get('refs', 0)}\n"
        f"💎 Premium: {prem_text}\n\n"
        f"🔗 *Реф. ссылка:*\n`{link}`\n\n"
        f"📋 Сохранённые: /my_usernames",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )
    await c.answer()

async def cb_premium_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🪙 USDT (TRC20)", callback_data="pay_usdt")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_main")]
    ])
    await c.message.edit_text(
        "💎 *Premium доступ*\n\n"
        "✅ Безлимитная генерация\n"
        "✅ Длина 5 символов\n"
        "✅ Приоритетная поддержка\n\n"
        "Выберите способ оплаты:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await c.answer()

# ================= STARS PAYMENT =================
class TxState(StatesGroup):
    waiting = State()

async def cb_stars_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 день — 10★", callback_data="star_1d")],
        [InlineKeyboardButton(text="7 дней — 60★", callback_data="star_7d")],
        [InlineKeyboardButton(text="30 дней — 240★", callback_data="star_30d")],
        [InlineKeyboardButton(text="1 год — 800★", callback_data="star_1y")],
        [InlineKeyboardButton(text="Навсегда — 1500★", callback_data="star_forever")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="premium")]
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
        [InlineKeyboardButton(text="◀ Назад", callback_data="premium")]
    ])
    await c.message.edit_text(
        f"🪙 *USDT (TRC20)*\n\n"
        f"📤 Кошелёк:\n`{config.usdt_wallet}`\n\n"
        f"Выберите тариф:",
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
        f"💰 *Оплата USDT (TRC20)*\n\n"
        f"Сумма: {price} USDT\n"
        f"Тариф: {'навсегда' if days == -1 else f'{days} дн.'}\n\n"
        f"📤 Отправьте на кошелёк:\n`{config.usdt_wallet}`\n\n"
        f"📎 После отправки введите TxID:",
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
    
    await m.answer("✅ Заявка отправлена! Администратор проверит её.")
    await state.clear()

# ================= ADMIN COMMANDS =================
async def cmd_stats(m: Message):
    if m.from_user.id != config.admin_id:
        return
    users = await db_fetchval("SELECT COUNT(*) FROM users")
    prem = await db_fetchval("SELECT COUNT(*) FROM users WHERE premium_until=-1 OR premium_until>$1", int(time.time()))
    saved = await db_fetchval("SELECT COUNT(*) FROM saved")
    uptime = int(time.time() - app.start_time)
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60
    
    await m.answer(
        f"📊 *Статистика*\n\n"
        f"👥 Пользователи: {users}\n"
        f"💎 Premium: {prem}\n"
        f"💾 Сохранений: {saved}\n"
        f"⏱ Работает: {hours}ч {minutes}м",
        parse_mode="Markdown"
    )

async def cmd_payments(m: Message):
    if m.from_user.id != config.admin_id:
        return
    rows = await db_fetch("SELECT * FROM payments WHERE status='wait' ORDER BY created_at DESC")
    if not rows:
        await m.answer("📭 Нет ожидающих заявок")
        return
    for p in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("✅ Подтвердить", callback_data=f"app_{p['id']}"),
             InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{p['id']}")]
        ])
        await m.answer(
            f"💰 Заявка #{p['id']}\n👤 {p['user_id']}\n💵 {p['amount']} USDT\n📎 TxID: `{p['txid'][:40]}...`",
            parse_mode="Markdown",
            reply_markup=kb
        )

async def cb_approve(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        return
    pid = int(c.data.split("_")[1])
    pay = await db_fetchrow("SELECT user_id, plan FROM payments WHERE id=$1", pid)
    if pay:
        await set_premium(pay["user_id"], USDT_DAYS[pay["plan"]])
        await db_execute("UPDATE payments SET status='approved' WHERE id=$1", pid)
        await app.bot.send_message(pay["user_id"], "✅ *Premium активирован!*", parse_mode="Markdown")
        await c.answer("✅ Подтверждено")
        await c.message.delete()

async def cb_reject(c: CallbackQuery):
    if c.from_user.id != config.admin_id:
        return
    pid = int(c.data.split("_")[1])
    pay = await db_fetchrow("SELECT user_id FROM payments WHERE id=$1", pid)
    if pay:
        await db_execute("UPDATE payments SET status='rejected' WHERE id=$1", pid)
        await app.bot.send_message(pay["user_id"], "❌ *Заявка отклонена*", parse_mode="Markdown")
        await c.answer("❌ Отклонено")
        await c.message.delete()

async def cmd_premium_give(m: Message):
    if m.from_user.id != config.admin_id:
        return
    args = m.text.split()
    if len(args) < 2:
        await m.answer("/premium <user_id> [days] (-1=forever, 0=remove)")
        return
    try:
        uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30
        await set_premium(uid, days)
        await m.answer(f"✅ Premium выдан {uid} на {'навсегда' if days == -1 else f'{days} дн.'}")
    except:
        await m.answer("❌ Ошибка")

async def cmd_ban(m: Message):
    if m.from_user.id != config.admin_id:
        return
    args = m.text.split()
    if len(args) < 2:
        await m.answer("/ban <user_id> [minutes]")
        return
    try:
        uid = int(args[1])
        minutes = int(args[2]) if len(args) > 2 else 5
        await app.rate_limiter.ban_user(uid, minutes * 60)
        await m.answer(f"✅ Пользователь {uid} забанен на {minutes} минут")
    except:
        await m.answer("❌ Ошибка")

async def cmd_unban(m: Message):
    if m.from_user.id != config.admin_id:
        return
    args = m.text.split()
    if len(args) < 2:
        await m.answer("/unban <user_id>")
        return
    try:
        uid = int(args[1])
        await app.rate_limiter.unban_user(uid)
        await m.answer(f"✅ Пользователь {uid} разбанен")
    except:
        await m.answer("❌ Ошибка")

# ================= PAYMENT CALLBACKS =================
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

async def payment_success(m: Message):
    days = int(m.successful_payment.invoice_payload.split("_")[1])
    await set_premium(m.from_user.id, days)
    await m.answer("✅ *Premium активирован!*", parse_mode="Markdown")

# ================= REGISTER HANDLERS =================
def register_handlers():
    dp = app.dp
    
    # Message handlers
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_my_usernames, Command("my_usernames"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_payments, Command("payments"))
    dp.message.register(cmd_premium_give, Command("premium"))
    dp.message.register(cmd_ban, Command("ban"))
    dp.message.register(cmd_unban, Command("unban"))
    
    # Callback handlers
    dp.callback_query.register(cb_back_main, F.data == "back_main")
    dp.callback_query.register(cb_gen_menu, F.data == "gen")
    dp.callback_query.register(cb_choose_len, F.data.startswith("len_"))
    dp.callback_query.register(cb_generate, F.data.startswith("mode_"))
    dp.callback_query.register(cb_save, F.data == "save")
    dp.callback_query.register(cb_profile, F.data == "profile")
    dp.callback_query.register(cb_premium_menu, F.data == "premium")
    dp.callback_query.register(cb_stars_menu, F.data == "pay_stars")
    dp.callback_query.register(cb_stars_pay, F.data.startswith("star_"))
    dp.callback_query.register(cb_usdt_menu, F.data == "pay_usdt")
    dp.callback_query.register(cb_usdt_request, F.data.startswith("usdt_"))
    dp.callback_query.register(cb_approve, F.data.startswith("app_"))
    dp.callback_query.register(cb_reject, F.data.startswith("rej_"))
    
    # FSM
    dp.message.register(handle_txid, StateFilter(TxState.waiting))
    
    # Payments
    dp.pre_checkout_query.register(pre_checkout)
    dp.message.register(payment_success, F.successful_payment)

# ================= DB INIT =================
async def init_db():
    app.db = await asyncpg.create_pool(
        config.database_url,
        min_size=2,
        max_size=10,
        command_timeout=30
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
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS saved(
                id SERIAL,
                user_id BIGINT,
                username TEXT,
                created_at BIGINT,
                UNIQUE(user_id, username)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments(
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                method TEXT,
                plan TEXT,
                amount FLOAT,
                txid TEXT,
                status TEXT DEFAULT 'wait',
                created_at BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS temp_usernames(
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                created_at BIGINT,
                expires_at BIGINT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_premium ON users(premium_until)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_temp_expires ON temp_usernames(expires_at)")
    
    logger.info("✅ Database ready")

# ================= BACKGROUND TASKS =================
async def cleanup_task():
    while True:
        try:
            await db_execute("DELETE FROM temp_usernames WHERE expires_at < $1", int(time.time()))
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)

# ================= WEBHOOK =================
async def webhook_handler(request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != config.webhook_secret:
        return web.Response(status=403)
    
    if not app.bot or not app.dp:
        return web.Response(status=503)
    
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await app.dp.feed_update(app.bot, update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

async def health_check(request):
    status = {"status": "ok", "timestamp": time.time()}
    if not app.db or app.db.is_closing():
        status["status"] = "db_error"
        return web.json_response(status, status=503)
    if not app.bot:
        status["status"] = "bot_not_ready"
        return web.json_response(status, status=503)
    return web.json_response(status)

# ================= STARTUP =================
async def on_startup(app_web):
    logger.info("🚀 Starting Auron Search Bot...")
    app.start_time = time.time()
    
    if not config.database_url:
        logger.error("❌ DATABASE_URL not set!")
        return
    
    await init_db()
    
    app.dp = Dispatcher(storage=MemoryStorage())
    app.dp.message.middleware(AntiSpamMiddleware())
    app.dp.callback_query.middleware(AntiSpamMiddleware())
    register_handlers()
    
    app.bot = Bot(token=config.bot_token)
    asyncio.create_task(cleanup_task())
    
    if config.webhook_url and config.webhook_secret:
        webhook_url = f"{config.webhook_url.rstrip('/')}{config.webhook_path}"
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_webhook(
            url=webhook_url,
            secret_token=config.webhook_secret,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "pre_checkout_query"]
        )
        logger.info(f"✅ Webhook set: {webhook_url}")
    else:
        logger.warning("⚠️ Webhook not configured, try polling...")
        await app.bot.delete_webhook()
        asyncio.create_task(app.dp.start_polling(app.bot))
    
    logger.info(f"✅ Bot started! Admin: {config.admin_id}")

async def on_shutdown(app_web):
    logger.info("🛑 Shutting down...")
    if app.bot:
        await app.bot.delete_webhook()
        await app.bot.session.close()
    if app.db:
        await app.db.close()
    logger.info("✅ Stopped")

# ================= MAIN =================
web_app = web.Application()
web_app.router.add_get("/health", health_check)
web_app.router.add_post(config.webhook_path, webhook_handler)
web_app.on_startup.append(on_startup)
web_app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    if config.webhook_url:
        web.run_app(web_app, port=config.port)
    else:
        # Режим polling для локального тестирования
        async def poll():
            app.dp = Dispatcher(storage=MemoryStorage())
            app.dp.message.middleware(AntiSpamMiddleware())
            app.dp.callback_query.middleware(AntiSpamMiddleware())
            register_handlers()
            app.bot = Bot(token=config.bot_token)
            await app.bot.delete_webhook()
            await app.dp.start_polling(app.bot)
        
        asyncio.run(poll())
