#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import telebot
import sqlite3
import random
import string
import time
import logging
from datetime import date, datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery

# ================= ПРОВЕРКА ТОКЕНА =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не найден!")
    sys.exit(1)

# ================= КОНФИГУРАЦИЯ =================
ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@Auron_search")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Auronsearchesbot")
CHECK_SUBSCRIPTION = os.getenv("CHECK_SUBSCRIPTION", "True").lower() == "true"
USDT_WALLET = os.getenv("USDT_WALLET", "#IV50644032")

USDT_PRICES = {"1d":0.6, "3d":1.6, "7d":2.8, "30d":6.0, "365d":24.0, "forever":40.0}
USDT_DAYS = {"1d":1, "3d":3, "7d":7, "30d":30, "365d":365, "forever":0}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)

# ================= БАЗА =================
DB_PATH = "/tmp/auron.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)

# FIX SQLITE STABILITY
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")

c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    premium_until INTEGER DEFAULT 0,
    searches_today INTEGER DEFAULT 0,
    last_date TEXT,
    referrer_id INTEGER DEFAULT 0,
    referals_count INTEGER DEFAULT 0
)""")

c.execute("""CREATE TABLE IF NOT EXISTS saved_usernames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    created_at INTEGER
)""")

c.execute("""CREATE TABLE IF NOT EXISTS pending_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    tariff TEXT,
    amount REAL,
    days INTEGER,
    created_at INTEGER,
    status TEXT DEFAULT 'waiting'
)""")

c.execute("""CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    message TEXT,
    created_at INTEGER,
    is_read INTEGER DEFAULT 0
)""")

conn.commit()

# ================= HELPERS =================
def escape_md(text):
    return text

def is_subscribed(user_id):
    if not CHECK_SUBSCRIPTION:
        return True
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def gen_letters(length):
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(length))

def gen_mixed(length):
    pos = random.randint(0, length - 1)
    return ''.join(random.choice(string.digits if i == pos else string.ascii_lowercase) for i in range(length))

def get_user(uid):
    c.execute("SELECT premium_until, searches_today, last_date, referals_count FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, last_date) VALUES (?, ?)", (uid, str(date.today())))
        conn.commit()
        return False, 0, str(date.today()), 0, 0
    return row[0] > int(time.time()), row[1], row[2], row[0], row[3]

def can_search(uid):
    is_premium, searches, _, _, _ = get_user(uid)
    return is_premium or searches < FREE_LIMIT

def increment_search(uid):
    is_premium, searches, _, _, _ = get_user(uid)
    if not is_premium:
        c.execute("UPDATE users SET searches_today=? WHERE user_id=?", (searches+1, uid))
        conn.commit()

def set_premium(uid, days):
    now = int(time.time())
    until = 2147483647 if days == 0 else now + days*86400
    c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
    conn.commit()

# ================= START =================
@bot.message_handler(commands=["start"])
def start(m):
    bot.send_message(m.chat.id, "🔷 Бот работает")

# ================= LENGTH =================
@bot.message_handler(func=lambda m: m.text == "🔍 Найти юзернейм")
def find(m):
    bot.send_message(m.chat.id, "Выбери длину")

# ================= CALLBACK =================
@bot.callback_query_handler(func=lambda c: True)
def cb(call):
    uid = call.from_user.id
    data = call.data

    # FIX SAVE
    if data.startswith("save_"):
        bot.answer_callback_query(call.id, "Сохранено")
        return

    # LENGTH 7
    if data == "gen_7":
        if not can_search(uid):
            bot.answer_callback_query(call.id, "Лимит")
            return
        username = gen_letters(7)
        increment_search(uid)
        bot.edit_message_text(f"@{username}", call.message.chat.id, call.message.message_id)

    # FIX USDT APPROVE (ВАЖНО)
    if data.startswith("admin_usdt_approve_"):
        pid = int(data.split("_")[3])
        c.execute("SELECT user_id, days FROM pending_payments WHERE id=?", (pid,))
        row = c.fetchone()
        if row:
            set_premium(row[0], row[1])
            bot.send_message(row[0], "Premium активирован")
        bot.answer_callback_query(call.id)

    # FIX USDT REJECT
    if data.startswith("admin_usdt_reject_"):
        pid = int(data.split("_")[3])
        c.execute("UPDATE pending_payments SET status='cancelled' WHERE id=?", (pid,))
        conn.commit()
        bot.answer_callback_query(call.id, "Отклонено")

# ================= STARS =================
@bot.pre_checkout_query_handler(func=lambda q: True)
def pre(q):
    bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=["successful_payment"])
def success(m):
    payload = m.successful_payment.invoice_payload
    if payload.startswith("stars"):
        _, days, stars, uid = payload.split("_")
        set_premium(m.from_user.id, int(days))
        bot.send_message(m.chat.id, "Premium активирован")

# ================= REPLY FIX =================
@bot.message_handler(commands=["reply"])
def reply(m):
    parts = m.text.split(" ", 2)
    if len(parts) < 3:
        bot.reply_to(m, "Использование: /reply id текст")
        return
    uid = int(parts[1])
    bot.send_message(uid, parts[2])

# ================= RUN =================
if __name__ == "__main__":
    print("Bot started")
    bot.polling(none_stop=True)
