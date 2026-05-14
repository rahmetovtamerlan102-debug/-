import os
import time
import asyncio
import random
import asyncpg

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

FREE_LIMIT = 10

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher(storage=MemoryStorage())

db: asyncpg.Pool = None


# ================= DB =================

async def init_db():
    global db
    db = await asyncpg.create_pool(DATABASE_URL)

    async with db.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id BIGINT PRIMARY KEY,
            searches INT DEFAULT 0,
            last_day TEXT
        );

        CREATE TABLE IF NOT EXISTS saved(
            username TEXT PRIMARY KEY
        );
        """)


# ================= USER =================

async def get_user(uid: int):
    async with db.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

        if not user:
            await conn.execute(
                "INSERT INTO users(user_id, searches, last_day) VALUES($1,0,$2)",
                uid, str(time.strftime("%Y-%m-%d"))
            )
            return {"searches": 0, "last_day": str(time.strftime("%Y-%m-%d"))}

        # reset daily
        today = str(time.strftime("%Y-%m-%d"))
        if user["last_day"] != today:
            await conn.execute(
                "UPDATE users SET searches=0, last_day=$1 WHERE user_id=$2",
                today, uid
            )
            user["searches"] = 0

        return dict(user)


async def inc(uid: int):
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE users SET searches = searches + 1 WHERE user_id=$1",
            uid
        )


# ================= GENERATOR =================

SYL = [
    "ka", "lo", "mi", "ne", "ra", "vo", "ze",
    "ta", "lu", "xi", "qo", "re", "sa"
]

def gen_name(length: int):
    name = ""
    while len(name) < length:
        name += random.choice(SYL)
    return name[:length]


# ================= CHECK =================

async def is_free(username: str):
    try:
        chat = await bot.get_chat(f"@{username}")
        return False
    except:
        return True


# ================= FIND =================

async def find_name(length: int):
    for _ in range(200):
        name = gen_name(length)

        async with db.acquire() as conn:
            taken = await conn.fetchval(
                "SELECT 1 FROM saved WHERE username=$1",
                name
            )

        if taken:
            continue

        if await is_free(name):
            async with db.acquire() as conn:
                await conn.execute(
                    "INSERT INTO saved(username) VALUES($1)",
                    name
                )
            return name

    return None


# ================= KEYBOARDS =================

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти", callback_data="find")],
    ])


def kb_result(name, length):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💾 @{name}", callback_data=f"save_{name}")],
        [InlineKeyboardButton(text="🔁 Ещё", callback_data=f"more_{length}")]
    ])


# ================= HANDLERS =================

@dp.message(F.text == "/start")
async def start(m: Message):
    await m.answer("👋 Бот запущен\n\nНажми кнопку:", reply_markup=kb_main())


@dp.callback_query(F.data == "find")
async def find(c: CallbackQuery):
    user = await get_user(c.from_user.id)

    if user["searches"] >= FREE_LIMIT and c.from_user.id != ADMIN_ID:
        await c.message.answer("❌ Лимит исчерпан")
        return

    await c.message.edit_text("🔎 Поиск...")

    name = await find_name(6)

    if not name:
        await c.message.edit_text("❌ Не найдено")
        return

    await inc(c.from_user.id)

    await c.message.edit_text(
        f"✅ Найдено:\n\n@{name}",
        reply_markup=kb_result(name, 6)
    )


@dp.callback_query(F.data.startswith("more_"))
async def more(c: CallbackQuery):
    length = int(c.data.split("_")[1])

    await c.message.edit_text("🔎 Поиск...")

    name = await find_name(length)

    if not name:
        await c.message.edit_text("❌ Не найдено")
        return

    await inc(c.from_user.id)

    await c.message.edit_text(
        f"✅ Найдено:\n\n@{name}",
        reply_markup=kb_result(name, length)
    )


@dp.callback_query(F.data.startswith("save_"))
async def save(c: CallbackQuery):
    await c.answer("Сохранено ✅", show_alert=True)


# ================= MAIN =================

async def main():
    await init_db()
    print("BOT STARTED")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
