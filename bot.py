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

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не найден!")
    sys.exit(1)

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

DB_PATH = "/tmp/auron.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    premium_until INTEGER DEFAULT 0,
    searches_today INTEGER DEFAULT 0,
    last_date TEXT,
    referrer_id INTEGER DEFAULT 0,
    referals_count INTEGER DEFAULT 0
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS saved_usernames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    created_at INTEGER
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS pending_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    tariff TEXT,
    amount REAL,
    days INTEGER,
    created_at INTEGER,
    status TEXT DEFAULT 'waiting'
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    message TEXT,
    created_at INTEGER,
    is_read INTEGER DEFAULT 0
)
""")

conn.commit()

def escape_md(text):
    special = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in special else c for c in text)

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
    result = []
    for i in range(length):
        result.append(random.choice(string.digits) if i == pos else random.choice(string.ascii_lowercase))
    return ''.join(result)

def set_premium(uid, days):
    now = int(time.time())
    new_until = 2147483647 if days == 0 else now + days * 86400
    c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
    conn.commit()

@bot.message_handler(commands=["start"])
def start(m):
    bot.send_message(m.chat.id, "Бот работает ✅")

@bot.message_handler(func=lambda m: True)
def menu(m):
    bot.send_message(m.chat.id, "Menu")

if __name__ == "__main__":
    while True:
        try:
            bot.polling(none_stop=True)
        except Exception as e:
            print(e)
            time.sleep(5)
