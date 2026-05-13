import os
import logging
import asyncpg
from flask import Flask
import threading
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# ========== НАСТРОЙКИ ==========
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# ========== ВЕБ-СЕРВЕР ДЛЯ CRON-JOB.ORG ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Бот работает!", 200

@app.route('/health')
def health():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ========== РАБОТА С БАЗОЙ ДАННЫХ ==========
async def init_db():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        await conn.close()
        logger.info("✅ База данных подключена")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
        return False

async def add_user(user_id, username, first_name):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute('''
            INSERT INTO users (user_id, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
        ''', user_id, username, first_name)
        await conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления: {e}")
        return False

async def get_users_count():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        count = await conn.fetchval('SELECT COUNT(*) FROM users')
        await conn.close()
        return count
    except:
        return 0

async def notify_admin(text):
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except:
            pass

# ========== КОМАНДЫ БОТА ==========
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    user = message.from_user
    await add_user(user.id, user.username, user.first_name)
    count = await get_users_count()
    
    await message.reply(
        f"✅ Привет, {user.first_name}!\n\n"
        f"Я работаю 24/7 бесплатно\n"
        f"База данных: PostgreSQL ✅\n"
        f"👥 Пользователей: {count}\n\n"
        f"Команды:\n/stats — статистика (только админ)"
    )

@dp.message_handler(commands=['stats'])
async def stats_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Доступ только для админа")
        return
    
    count = await get_users_count()
    await message.reply(
        f"📊 Статистика бота:\n"
        f"👥 Пользователей: {count}\n"
        f"✅ Бот активен"
    )

@dp.message_handler()
async def echo(message: types.Message):
    await message.answer(f"Ты написал: {message.text}")

# ========== ЗАПУСК ==========
async def on_startup(dp):
    await init_db()
    await notify_admin("🚀 Бот запущен и готов к работе!")
    logger.info("Бот запущен")

async def on_shutdown(dp):
    await notify_admin("🛑 Бот остановлен")
    await bot.close()

if __name__ == '__main__':
    # Запускаем веб-сервер в отдельном потоке
    threading.Thread(target=run_web, daemon=True).start()
    
    # Запускаем бота
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
