#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import fcntl
import sqlite3
import random
import string
import logging
import requests
from datetime import date, datetime
from threading import Lock, Thread
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery

# ================= КОНФИГ =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ BOT_TOKEN не найден!")
    sys.exit(1)

ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@Auron_search")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Auronsearchesbot")
CHECK_SUBSCRIPTION = os.getenv("CHECK_SUBSCRIPTION", "True").lower() == "true"
USDT_WALLET = os.getenv("USDT_WALLET", "#IV50644032")

USDT_PRICES = {"1d":0.6, "3d":1.6, "7d":2.8, "30d":6.0, "365d":24.0, "forever":40.0}
USDT_DAYS = {"1d":1, "3d":3, "7d":7, "30d":30, "365d":365, "forever":0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= БОТ =================
bot = telebot.TeleBot(TOKEN)

# ================= HTTP-СЕРВЕР ДЛЯ RENDER =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()

Thread(target=run_http_server, daemon=True).start()
logger.info("✅ HTTP-сервер запущен на порту 8080")

# ================= ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА =================
PID_FILE = "/tmp/bot.pid"
fp = open(PID_FILE, 'w')
try:
    fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except:
    print("❌ Бот уже запущен. Выход.")
    sys.exit(1)
fp.write(str(os.getpid()))
fp.flush()

# ================= УДАЛЕНИЕ ВЕБХУКА =================
try:
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
    logger.info("✅ Вебхук удалён")
except Exception as e:
    logger.warning(f"Ошибка удаления вебхука: {e}")

# ================= БЕЗОПАСНЫЙ SQL-ВРАППЕР =================
DB_PATH = "/tmp/auron.db"

@contextmanager
def safe_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"DB error: {e}")
        raise
    finally:
        if conn:
            conn.close()

def init_db():
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            premium_until INTEGER DEFAULT 0,
            searches_today INTEGER DEFAULT 0,
            last_date TEXT,
            referrer_id INTEGER DEFAULT 0,
            referals_count INTEGER DEFAULT 0
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS saved_usernames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            created_at INTEGER
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            tariff TEXT,
            amount REAL,
            days INTEGER,
            created_at INTEGER,
            status TEXT DEFAULT 'waiting'
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            created_at INTEGER,
            is_read INTEGER DEFAULT 0
        )""")
    logger.info("✅ БД инициализирована (WAL)")

init_db()

# ================= ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ =================
def get_user(uid):
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("SELECT premium_until, searches_today, last_date, referals_count FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        if not row:
            c.execute("INSERT INTO users (user_id, last_date, referals_count, premium_until) VALUES (?, ?, 0, 0)",
                      (uid, str(date.today())))
            return False, 0, str(date.today()), 0, 0
        premium_until, searches, last, refs = row
        is_premium = premium_until > int(time.time())
        today = str(date.today())
        if last != today:
            searches = 0
            c.execute("UPDATE users SET searches_today=0, last_date=? WHERE user_id=?", (today, uid))
        return is_premium, searches, last, premium_until, refs

def get_max_searches(uid):
    _, _, _, _, refs = get_user(uid)
    return FREE_LIMIT + refs

def can_search(uid):
    p, s, _, _, _ = get_user(uid)
    return p or s < get_max_searches(uid)

def inc_search(uid):
    p, s, _, _, _ = get_user(uid)
    if not p:
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET searches_today=? WHERE user_id=?", (s+1, uid))

def get_remaining(uid):
    p, s, _, _, _ = get_user(uid)
    return float('inf') if p else get_max_searches(uid) - s

def set_premium(uid, days):
    with safe_db() as conn:
        c = conn.cursor()
        until = 2147483647 if days == 0 else int(time.time()) + days * 86400
        c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))

def add_referral(new_uid, ref_id):
    if ref_id == 0 or ref_id == new_uid:
        return False
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("SELECT referrer_id FROM users WHERE user_id=?", (new_uid,))
        row = c.fetchone()
        if row and row[0] == 0:
            c.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (ref_id, new_uid))
            c.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=?", (ref_id,))
            return True
        return False

def get_referral_link(uid):
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

def save_username(uid, username):
    if not username or not username.isalnum():
        return False
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO saved_usernames (user_id, username, created_at) VALUES (?, ?, ?)",
                  (uid, username, int(time.time())))
        return True

def get_saved_usernames(uid):
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("SELECT username, created_at FROM saved_usernames WHERE user_id=? ORDER BY created_at DESC", (uid,))
        return c.fetchall()

def save_support_message(uid, text):
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO support_messages (user_id, message, created_at, is_read) VALUES (?, ?, ?, 0)",
                  (uid, text, int(time.time())))

def get_unread_support_messages():
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, user_id, message, created_at FROM support_messages WHERE is_read=0 ORDER BY created_at DESC")
        return c.fetchall()

def mark_support_read(msg_id):
    with safe_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE support_messages SET is_read=1 WHERE id=?", (msg_id,))

def gen_letters(n):
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(n))

def gen_mixed(n):
    pos = random.randint(0, n-1)
    return ''.join(str(random.randint(0,9)) if i==pos else random.choice(string.ascii_lowercase) for i in range(n))

# ================= ПРОВЕРКА ПОДПИСКИ (безопасно) =================
def is_subscribed(uid):
    if not CHECK_SUBSCRIPTION:
        return True
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, uid)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Ошибка проверки подписки для {uid}: {e}")
        return False

def sub_required_msg(func):
    def wrapper(m):
        if not is_subscribed(m.from_user.id):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
            bot.send_message(m.chat.id, f"⚠️ Подпишитесь на канал: {CHANNEL_USERNAME}", reply_markup=markup)
            return
        return func(m)
    return wrapper

def sub_required_cb(func):
    def wrapper(call):
        if not is_subscribed(call.from_user.id):
            bot.answer_callback_query(call.id, "❌ Подпишитесь на канал!", show_alert=True)
            return
        return func(call)
    return wrapper

# ================= КЛАВИАТУРЫ =================
def start_kb(uid):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти юзернейм", "💎 Купить Premium")
    kb.row("👤 Личный кабинет", "🆘 Поддержка")
    if uid == ADMIN_ID:
        kb.row("🔧 Админ панель")
    return kb

def admin_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📩 Сообщения поддержки", callback_data="admin_support"),
        InlineKeyboardButton("💸 USDT заявки", callback_data="admin_usdt")
    )
    return kb

def len_kb(uid):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("5 (Premium)", callback_data="len_5"),
        InlineKeyboardButton("6", callback_data="len_6"),
        InlineKeyboardButton("7", callback_data="len_7")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_start"))
    return kb

def mode_kb(l):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🔤 Без цифр", callback_data=f"mode_{l}_letters"),
        InlineKeyboardButton("🔢 С цифрой", callback_data=f"mode_{l}_mixed")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_len"))
    return kb

def res_kb(l, mode, username):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
        InlineKeyboardButton("🔄 Ещё", callback_data=f"mode_{l}_{mode}")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data=f"back_mode_{l}"))
    return kb

def res7_kb(username):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
        InlineKeyboardButton("🔄 Ещё", callback_data="gen_7")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_len"))
    return kb

def pay_method_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⭐ Stars", callback_data="pay_stars"),
        InlineKeyboardButton("🪙 USDT", callback_data="pay_usdt")
    )
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_start"))
    return kb

def stars_tariffs_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("1 день – 30 ★", callback_data="star_1_30"),
        InlineKeyboardButton("3 дня – 80 ★", callback_data="star_3_80"),
        InlineKeyboardButton("7 дней – 140 ★", callback_data="star_7_140"),
        InlineKeyboardButton("30 дней – 300 ★", callback_data="star_30_300"),
        InlineKeyboardButton("365 дней – 1200 ★", callback_data="star_365_1200"),
        InlineKeyboardButton("Навсегда – 2000 ★", callback_data="star_0_2000")
    )
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_pay_method"))
    return kb

def usdt_tariffs_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    for t, price in USDT_PRICES.items():
        days = USDT_DAYS[t]
        txt = f"{days if days>0 else 'Навсегда'} – {price} USDT"
        kb.add(InlineKeyboardButton(txt, callback_data=f"usdt_{t}"))
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_pay_method"))
    return kb

def profile_text(uid):
    p, s, _, until, refs = get_user(uid)
    remain = get_remaining(uid)
    remain_str = f"{remain}" if remain != float('inf') else "∞ (Premium)"
    status = "👑 Premium" if p else "🔷 Обычный"
    if p and until != 2147483647:
        expiry = f"до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}"
    elif p:
        expiry = "Навсегда"
    else:
        expiry = "Не активен"
    link = get_referral_link(uid)
    return (
        f"╔ 🔷 Статус: {status}\n"
        f"╠ 🔷 Попытки сегодня: {remain_str}\n"
        f"╠ 🔷 Приглашено друзей: {refs}\n"
        f"╠ 🔷 Реф. ссылка: {link}\n"
        f"╠ 🔷 Premium: {expiry}\n"
        f"╚ 🔷 Сохранённые: /my_usernames\n\n"
        f"💡 Базовый лимит {FREE_LIMIT} попыток/день + {refs} за рефералов."
    )

# ================= ХЕНДЛЕРЫ =================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    try:
        if " " in m.text:
            arg = m.text.split()[1]
            if arg.startswith("ref_"):
                ref_id = int(arg.split("_")[1])
                add_referral(m.from_user.id, ref_id)
    except Exception as e:
        logger.error(f"Ошибка парсинга реферала: {e}")

    bot.send_message(m.chat.id,
        "🔷 Приветствуем в Auron Search!\n\n"
        "Профессиональный поиск свободных username.\n"
        "Выберите действие 💙",
        reply_markup=start_kb(m.from_user.id))

@bot.message_handler(commands=["my_usernames"])
@sub_required_msg
def show_saved(m):
    uid = m.from_user.id
    rows = get_saved_usernames(uid)
    if not rows:
        bot.send_message(m.chat.id, "📭 Нет сохранённых.")
        return
    text = "💾 Сохранённые:\n\n"
    for un, ts in rows[:20]:
        text += f"• @{un} — {datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')}\n"
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "🔍 Найти юзернейм")
@sub_required_msg
def menu_len(m):
    uid = m.from_user.id
    rem = get_remaining(uid)
    if rem == float('inf'):
        txt = "Выберите длину:"
    else:
        txt = f"🎫 Осталось {rem} попыток сегодня.\n\nВыберите длину:"
    bot.send_message(m.chat.id, txt, reply_markup=len_kb(uid))

@bot.message_handler(func=lambda m: m.text == "💎 Купить Premium")
@sub_required_msg
def payment_start(m):
    bot.send_message(m.chat.id, "🌟 Способ оплаты:", reply_markup=pay_method_kb())

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
@sub_required_msg
def profile_cmd(m):
    bot.send_message(m.chat.id, profile_text(m.from_user.id))

@bot.message_handler(func=lambda m: m.text == "🆘 Поддержка")
@sub_required_msg
def support_cmd(m):
    bot.send_message(m.chat.id,
        "📝 Как обратиться в поддержку?\n\n"
        "Просто напишите ваше сообщение ниже.\n"
        "Я перешлю его администратору.\n\n"
        "📌 Пример:\n"
        "• Проблема с оплатой\n"
        "• Не пришли бонусы за реферала\n"
        "• Вопрос по работе")
    waiting_for_support[m.from_user.id] = True

@bot.message_handler(func=lambda m: m.text == "🔧 Админ панель")
def admin_panel(m):
    if m.from_user.id != ADMIN_ID:
        return
    bot.send_message(m.chat.id, "🔧 Админ панель", reply_markup=admin_kb())

waiting_for_support = {}
@bot.message_handler(func=lambda m: waiting_for_support.get(m.from_user.id, False))
def forward_support(m):
    uid = m.from_user.id
    save_support_message(uid, m.text)
    bot.send_message(ADMIN_ID,
        f"🆘 Новое сообщение!\n👤 {m.from_user.first_name} (ID: {uid})\n💬 {m.text}")
    bot.send_message(m.chat.id, "✅ Отправлено администратору! Ответ придёт сюда.")
    waiting_for_support[uid] = False

@bot.message_handler(commands=["reply"])
def reply_support(m):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        parts = m.text.split(maxsplit=2)
        uid = int(parts[1])
        text = parts[2]
        bot.send_message(uid, f"💬 Ответ поддержки:\n\n{text}")
        bot.reply_to(m, f"✅ Ответ отправлен {uid}")
    except:
        bot.reply_to(m, "❌ /reply <user_id> <текст>")

# ================= CALLBACK =================
@bot.callback_query_handler(func=lambda c: True)
@sub_required_cb
def handle_cb(call):
    uid = call.from_user.id
    data = call.data

    # Сохранение username
    if data.startswith("save_"):
        un = data[5:]
        if save_username(uid, un):
            bot.answer_callback_query(call.id, f"✅ @{un} сохранён!")
        else:
            bot.answer_callback_query(call.id, "❌ Недопустимое имя", show_alert=True)
        return

    # Навигация
    if data == "back_start":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        cmd_start(call.message)
        bot.answer_callback_query(call.id)
        return
    if data == "back_len":
        rem = get_remaining(uid)
        txt = "Выберите длину:" if rem == float('inf') else f"🎫 Осталось {rem} попыток.\n\nВыберите длину:"
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=len_kb(uid))
        bot.answer_callback_query(call.id)
        return
    if data.startswith("back_mode_"):
        l = int(data.split("_")[2])
        bot.edit_message_text(f"Выбрана длина: {l}\nВыберите режим:", call.message.chat.id, call.message.message_id, reply_markup=mode_kb(l))
        bot.answer_callback_query(call.id)
        return
    if data == "back_pay_method":
        bot.edit_message_text("🌟 Способ оплаты:", call.message.chat.id, call.message.message_id, reply_markup=pay_method_kb())
        bot.answer_callback_query(call.id)
        return

    # Выбор длины
    if data.startswith("len_"):
        l = int(data.split("_")[1])
        p, _, _, _, _ = get_user(uid)
        if l == 5 and not p:
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "❌ Длина 5 букв доступна только Premium!\n💎 Купите Premium.")
            return
        if l == 7:
            if not can_search(uid):
                bot.answer_callback_query(call.id, f"❌ Лимит исчерпан! Осталось: {get_remaining(uid)}.", show_alert=True)
                return
            un = gen_letters(7)
            inc_search(uid)
            rem = get_remaining(uid)
            extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
            bot.edit_message_text(f"✅ @{un}{extra}", call.message.chat.id, call.message.message_id, reply_markup=res7_kb(un))
            bot.answer_callback_query(call.id)
            return
        bot.edit_message_text(f"Длина: {l}\nВыберите режим:", call.message.chat.id, call.message.message_id, reply_markup=mode_kb(l))
        bot.answer_callback_query(call.id)
        return

    # Режим генерации
    if data.startswith("mode_"):
        _, l, mode = data.split("_")
        l = int(l)
        if not can_search(uid):
            bot.answer_callback_query(call.id, f"❌ Лимит исчерпан! Осталось: {get_remaining(uid)}.", show_alert=True)
            return
        un = gen_letters(l) if mode == "letters" else gen_mixed(l)
        inc_search(uid)
        rem = get_remaining(uid)
        extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
        bot.edit_message_text(f"✅ @{un}{extra}", call.message.chat.id, call.message.message_id, reply_markup=res_kb(l, mode, un))
        bot.answer_callback_query(call.id)
        return
    if data == "gen_7":
        if not can_search(uid):
            bot.answer_callback_query(call.id, f"❌ Лимит исчерпан! Осталось: {get_remaining(uid)}.", show_alert=True)
            return
        un = gen_letters(7)
        inc_search(uid)
        rem = get_remaining(uid)
        extra = "" if rem == float('inf') else f"\n\n🎫 Осталось: {rem}"
        bot.edit_message_text(f"✅ @{un}{extra}", call.message.chat.id, call.message.message_id, reply_markup=res7_kb(un))
        bot.answer_callback_query(call.id)
        return

    # ---------- АДМИН-ПАНЕЛЬ ----------
    if data == "admin_support":
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        msgs = get_unread_support_messages()
        if not msgs:
            bot.edit_message_text("📭 Нет новых сообщений.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
            bot.answer_callback_query(call.id)
            return
        msg_id, user_id, text, ts = msgs[0]
        date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        preview = text[:200] + "…" if len(text) > 200 else text
        txt = f"📩 От {user_id}\n🕒 {date_str}\n\n{preview}"
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Прочитано", callback_data=f"support_read_{msg_id}"),
            InlineKeyboardButton("➡️ Далее", callback_data="admin_support_next")
        )
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_support_next":
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        # Отмечаем текущее как прочитанное (упрощённо: просто показываем следующее)
        msgs = get_unread_support_messages()
        if not msgs:
            bot.edit_message_text("📭 Нет новых сообщений.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
            bot.answer_callback_query(call.id)
            return
        msg_id, user_id, text, ts = msgs[0]
        date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        preview = text[:200] + "…" if len(text) > 200 else text
        txt = f"📩 От {user_id}\n🕒 {date_str}\n\n{preview}"
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Прочитано", callback_data=f"support_read_{msg_id}"),
            InlineKeyboardButton("➡️ Далее", callback_data="admin_support_next")
        )
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("support_read_"):
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        msg_id = int(data.split("_")[2])
        mark_support_read(msg_id)
        bot.answer_callback_query(call.id, "✅ Помечено прочитанным")
        msgs = get_unread_support_messages()
        if not msgs:
            bot.edit_message_text("📭 Нет новых сообщений.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
        else:
            msg_id, user_id, text, ts = msgs[0]
            date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
            preview = text[:200] + "…" if len(text) > 200 else text
            txt = f"📩 От {user_id}\n🕒 {date_str}\n\n{preview}"
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("✅ Прочитано", callback_data=f"support_read_{msg_id}"),
                InlineKeyboardButton("➡️ Далее", callback_data="admin_support_next")
            )
            kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    # ---------- USDT ЗАЯВКИ ----------
    if data == "admin_usdt":
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, amount, days, created_at FROM pending_payments WHERE status='waiting' ORDER BY created_at DESC")
            rows = c.fetchall()
        if not rows:
            bot.edit_message_text("💰 Нет ожидающих заявок USDT.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
            bot.answer_callback_query(call.id)
            return
        pid, uid_p, amt, days, ts = rows[0]
        date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        txt = f"💰 Заявка #{pid}\n👤 {uid_p}\n📅 {date_str}\n💵 {amt} USDT\n📆 Тариф: {days if days>0 else 'навсегда'}"
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"usdt_approve_{pid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"usdt_reject_{pid}")
        )
        if len(rows) > 1:
            kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_usdt_next":
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, amount, days, created_at FROM pending_payments WHERE status='waiting' ORDER BY created_at DESC")
            rows = c.fetchall()
        if len(rows) <= 1:
            bot.edit_message_text("💰 Нет других заявок.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
            bot.answer_callback_query(call.id)
            return
        pid, uid_p, amt, days, ts = rows[1]
        date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
        txt = f"💰 Заявка #{pid}\n👤 {uid_p}\n📅 {date_str}\n💵 {amt} USDT\n📆 Тариф: {days if days>0 else 'навсегда'}"
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"usdt_approve_{pid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"usdt_reject_{pid}")
        )
        if len(rows) > 2:
            kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
        kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("usdt_approve_"):
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        pid = int(data.split("_")[2])
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, days FROM pending_payments WHERE id=? AND status='waiting'", (pid,))
            row = c.fetchone()
            if not row:
                bot.answer_callback_query(call.id, "❌ Заявка уже обработана")
                return
            uid_p, days = row
            set_premium(uid_p, days)
            c.execute("UPDATE pending_payments SET status='confirmed' WHERE id=?", (pid,))
        bot.send_message(uid_p, f"🎉 Premium активирован! {'Навсегда' if days==0 else f'На {days} дней.'}")
        bot.answer_callback_query(call.id, "✅ Premium выдан")
        # Обновить отображение
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, amount, days, created_at FROM pending_payments WHERE status='waiting' ORDER BY created_at DESC")
            rows = c.fetchall()
        if not rows:
            bot.edit_message_text("💰 Нет ожидающих заявок USDT.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
        else:
            pid, uid_p, amt, days, ts = rows[0]
            date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
            txt = f"💰 Заявка #{pid}\n👤 {uid_p}\n📅 {date_str}\n💵 {amt} USDT\n📆 Тариф: {days if days>0 else 'навсегда'}"
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"usdt_approve_{pid}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"usdt_reject_{pid}")
            )
            if len(rows) > 1:
                kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
            kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    if data.startswith("usdt_reject_"):
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        pid = int(data.split("_")[2])
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM pending_payments WHERE id=? AND status='waiting'", (pid,))
            row = c.fetchone()
            if not row:
                bot.answer_callback_query(call.id, "❌ Заявка уже обработана")
                return
            c.execute("UPDATE pending_payments SET status='cancelled' WHERE id=?", (pid,))
        bot.send_message(row[0], "❌ Ваша заявка на оплату USDT отклонена.")
        bot.answer_callback_query(call.id, "❌ Заявка отклонена")
        # Обновить отображение
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, amount, days, created_at FROM pending_payments WHERE status='waiting' ORDER BY created_at DESC")
            rows = c.fetchall()
        if not rows:
            bot.edit_message_text("💰 Нет ожидающих заявок USDT.", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
        else:
            pid, uid_p, amt, days, ts = rows[0]
            date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
            txt = f"💰 Заявка #{pid}\n👤 {uid_p}\n📅 {date_str}\n💵 {amt} USDT\n📆 Тариф: {days if days>0 else 'навсегда'}"
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"usdt_approve_{pid}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"usdt_reject_{pid}")
            )
            if len(rows) > 1:
                kb.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
            kb.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back"))
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)
        return

    if data == "admin_back":
        if uid != ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔ Нет прав")
            return
        bot.edit_message_text("🔧 Админ панель", call.message.chat.id, call.message.message_id, reply_markup=admin_kb())
        bot.answer_callback_query(call.id)
        return

    # ---------- ОПЛАТА ----------
    if data == "pay_stars":
        bot.edit_message_text("⭐ Выберите тариф:", call.message.chat.id, call.message.message_id, reply_markup=stars_tariffs_kb())
        bot.answer_callback_query(call.id)
        return
    if data == "pay_usdt":
        bot.edit_message_text("🪙 Выберите тариф:", call.message.chat.id, call.message.message_id, reply_markup=usdt_tariffs_kb())
        bot.answer_callback_query(call.id)
        return

    if data.startswith("star_"):
        _, days, stars = data.split("_")
        days = int(days)
        stars = int(stars)
        desc = "навсегда" if days == 0 else f"{days} дней"
        try:
            bot.send_invoice(call.message.chat.id,
                title="Auron Premium",
                description=f"Premium на {desc}",
                invoice_payload=f"stars_{days}_{stars}_{uid}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label="Premium", amount=stars)],
                start_parameter="auron_premium")
        except Exception as e:
            logger.error(f"Invoice error: {e}")
            bot.answer_callback_query(call.id, "Ошибка создания счёта", show_alert=True)
        else:
            bot.answer_callback_query(call.id)
        return

    if data.startswith("usdt_"):
        tariff = data.split("_")[1]
        amount = USDT_PRICES[tariff]
        days = USDT_DAYS[tariff]
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO pending_payments (user_id, tariff, amount, days, created_at) VALUES (?, ?, ?, ?, ?)",
                      (uid, tariff, amount, days, int(time.time())))
            pid = c.lastrowid
        text = (f"💸 Счёт создан!\nСумма: {amount} USDT\n"
                f"Тариф: {days if days>0 else 'навсегда'} дней\n\n"
                f"Оплата:\n1. Переведите {amount} USDT (TRC20) на кошелёк: {USDT_WALLET}\n"
                f"2. Нажмите «✅ Я оплатил»\n3. Админ проверит платёж.")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Я оплатил", callback_data=f"usdt_confirm_{pid}"),
               InlineKeyboardButton("◀ Назад", callback_data="back_pay_method"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("usdt_confirm_"):
        pid = int(data.split("_")[2])
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, tariff, amount, days FROM pending_payments WHERE id=? AND status='waiting'", (pid,))
            row = c.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "❌ Заявка не найдена", show_alert=True)
            return
        admin_text = (f"🆕 ОПЛАТА USDT\nПользователь: {row[0]}\n"
                      f"Тариф: {row[3] if row[3]!=0 else 'навсегда'} дней\nСумма: {row[2]} USDT\nID: {pid}")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("✅ Подтвердить", callback_data=f"usdt_approve_{pid}"),
               InlineKeyboardButton("❌ Отклонить", callback_data=f"usdt_reject_{pid}"))
        bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
        bot.answer_callback_query(call.id, "✅ Заявка отправлена админу.")
        bot.edit_message_text("✅ Заявка отправлена администратору. Ожидайте.", call.message.chat.id, call.message.message_id)
        return

    bot.answer_callback_query(call.id, "Неизвестная команда")

# ================= ПЛАТЕЖИ STARS (PRE-CHECKOUT) =================
@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(q):
    try:
        bot.answer_pre_checkout_query(q.id, ok=True)
    except Exception as e:
        logger.error(f"Pre-checkout error: {e}")
        bot.answer_pre_checkout_query(q.id, ok=False, error_message="Ошибка платежа, попробуйте позже")

@bot.message_handler(content_types=['successful_payment'])
def on_successful_payment(m):
    payload = m.successful_payment.invoice_payload
    parts = payload.split("_")
    if len(parts) >= 3 and parts[0] == "stars":
        days = int(parts[1])
        set_premium(m.from_user.id, days)
        desc = "навсегда" if days == 0 else f"{days} дней"
        bot.send_message(m.chat.id, f"✅ Premium активирован на {desc}!", reply_markup=start_kb(m.from_user.id))

# ================= АДМИН КОМАНДЫ =================
@bot.message_handler(commands=["premium"])
def give_premium(m):
    if m.from_user.id != ADMIN_ID:
        return
    args = m.text.split()
    if len(args) < 2:
        bot.reply_to(m, "/premium <user_id> [дни] (0=навсегда)")
        return
    try:
        uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 0
        set_premium(uid, days)
        bot.reply_to(m, f"✅ Premium выдан {uid} на {'навсегда' if days==0 else f'{days} дней'}.")
    except:
        bot.reply_to(m, "Ошибка")

@bot.message_handler(commands=["unpremium"])
def remove_premium(m):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(m.text.split()[1])
        with safe_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET premium_until=0 WHERE user_id=?", (uid,))
        bot.reply_to(m, f"✅ Premium снят с {uid}.")
    except:
        bot.reply_to(m, "Ошибка")

# ================= ЗАПУСК =================
if __name__ == "__main__":
    logger.info("🚀 Бот запущен")
    backoff = 1
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=30)
            backoff = 1
        except Exception as e:
            logger.error(f"Ошибка polling: {e}. Повтор через {backoff} сек.")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
