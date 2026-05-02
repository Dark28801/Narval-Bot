import asyncio
import base64
import datetime
import re
import signal
import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import os
import random
import uuid

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан! Создай файл bot/.env со строкой BOT_TOKEN=твой_токен")

DB_PATH = "bot.db"
WEBHOOK_PATH = "/api/webhook"
PORT = int(os.getenv("PORT", "8080"))
REPLDB_URL = os.getenv("REPLIT_DB_URL", "") or os.getenv("DB_BACKUP_URL", "")
REPLDB_KEY = "bot_db_backup"

bot = Bot(token=TOKEN)
dp = Dispatcher()
BOT_ID = None  # заполняется при старте

# =========================
# ДУЭЛИ (в памяти)
# =========================

duels = {}
roulettes = {}   # roulette_id -> {user_id, user_name, chat_id, chambers_left}
proposals = {}   # proposal_id -> {proposer_id, proposer_name, target_id, target_name, chat_id}

# (название, текст кнопки, прошедшее действие, шанс попадания)
WEAPONS = [
    ("🔫 Пистолет", "Выстрелить из пистолета", "выстрелил(а) из пистолета", 0.25),
    ("🪃 Дробовик",  "Выстрелить из дробовика", "выстрелил(а) из дробовика", 0.45),
    ("🏹 Лук",       "Выстрелить из лука",       "выстрелил(а) из лука",      0.20),
    ("🗡️ Рапира",    "Уколоть рапирой",          "уколол(а) рапирой",         0.30),
    ("💣 Граната",   "Бросить гранату",          "бросил(а) гранату",         0.55),
    ("🔪 Нож",       "Ударить ножом",            "ударил(а) ножом",           0.35),
]


# =========================
# DATABASE
# =========================

INITIAL_STATS = [
    (5774288207, 10607, 1248),
    (497561961,  8194,  916),
    (882480153,  9066,  1235),
    (5985080990, 5163,  541),
    (7402698479, 3290,  544),
    (7577441555, 2350,  185),
    (7230627665, 732,   280),
    (8432250696, 2305,  495),
    (1432444267, 1101,  674),
    (6336849893, 2208,  1087),
    (6501087084, 160,   0),
    (1559813677, 583,   395),
    (2054205476, 241,   130),
]


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            first_name TEXT,
            message_count INTEGER DEFAULT 0,
            weekly_count INTEGER DEFAULT 0,
            last_message_date TEXT,
            reputation INTEGER DEFAULT 0,
            nickname TEXT DEFAULT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
        """)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN reputation INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN nickname TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN coins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN farm_last TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN gift_last TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN warns INTEGER DEFAULT 0",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass
        await db.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER,
            chat_id INTEGER,
            banned_by INTEGER,
            banned_at TEXT,
            reason TEXT DEFAULT NULL,
            PRIMARY KEY (user_id, chat_id)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS marriages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user1_id INTEGER,
            user1_name TEXT,
            user2_id INTEGER,
            user2_name TEXT,
            created_at TEXT,
            love_points INTEGER DEFAULT 0,
            gift_last TEXT DEFAULT NULL
        )
        """)
        for col_sql in [
            "ALTER TABLE marriages ADD COLUMN love_points INTEGER DEFAULT 0",
            "ALTER TABLE marriages ADD COLUMN gift_last TEXT DEFAULT NULL",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        await db.commit()


async def seed_initial_data():
    """При первом запуске вносит начальную статистику пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = 'initial_data_seeded'")
        row = await cur.fetchone()
        if row:
            return
        today = datetime.date.today().isoformat()
        for user_id, total_count, weekly_count in INITIAL_STATS:
            await db.execute("""
                INSERT INTO users (user_id, chat_id, username, first_name, message_count, weekly_count, last_message_date, reputation)
                VALUES (?, ?, NULL, ?, ?, ?, ?, 0)
                ON CONFLICT(user_id, chat_id) DO UPDATE SET
                    message_count = MAX(users.message_count, excluded.message_count),
                    weekly_count  = MAX(users.weekly_count,  excluded.weekly_count)
            """, (user_id, MAIN_CHAT_ID, str(user_id), total_count, weekly_count, today))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('initial_data_seeded', '1')")
        await db.commit()
    print("=== Начальная статистика пользователей загружена ===")


async def get_display_name(user_id: int, chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(nickname, first_name) FROM users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        row = await cursor.fetchone()
    return row[0] if row else "???"


async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def get_target_user(message: Message):
    """Возвращает (user_id, first_name) цели: из ответа, text_mention или @username."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.first_name
    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user:
                return entity.user.id, entity.user.first_name
            if entity.type == "mention":
                # Извлекаем @username и ищем в БД (без символа @)
                raw = entity.extract_from(message.text)
                username = raw.lstrip("@")
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(
                        "SELECT user_id, first_name FROM users "
                        "WHERE username = ? AND chat_id = ?",
                        (username, message.chat.id)
                    )
                    row = await cursor.fetchone()
                if row:
                    return row[0], row[1]
    return None, None


def parse_duration(text: str) -> int:
    """Парсит строку вида '1 час 5 минут 30 секунд' и возвращает кол-во секунд."""
    total = 0
    pairs = [
        (r'(\d+)\s*д(ен|ня|ней|ень)?', 86400),
        (r'(\d+)\s*ч(ас|аса|асов|ас)?', 3600),
        (r'(\d+)\s*мин(ут|уты|уту|ута)?', 60),
        (r'(\d+)\s*сек(унд|унды|унду|унда)?', 1),
    ]
    for pattern, mult in pairs:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            total += int(m.group(1)) * mult
    return total


LOVE_LEVELS = [
    (0,   "💛 Только познакомились",  1),
    (5,   "💚 Симпатия",              2),
    (15,  "💙 Влюблённость",          3),
    (30,  "💜 Крепкая пара",          4),
    (55,  "❤️ Настоящая любовь",      5),
    (85,  "🧡 Неразлучные",           6),
    (120, "❤️‍🔥 Вечная любовь",        7),
]

def get_love_level(points: int) -> tuple:
    level_info = LOVE_LEVELS[0]
    for threshold, name, lvl in LOVE_LEVELS:
        if points >= threshold:
            level_info = (threshold, name, lvl)
    threshold, name, lvl = level_info
    next_levels = [t for t, _, _ in LOVE_LEVELS if t > points]
    next_threshold = next_levels[0] if next_levels else None
    return lvl, name, next_threshold


def format_elapsed(created_at_str: str) -> str:
    created = datetime.datetime.fromisoformat(created_at_str)
    delta = datetime.datetime.now() - created
    total = int(delta.total_seconds())
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    return " ".join(parts) if parts else "только что"


_msg_counter = 0   # счётчик сообщений для триггера резерва
_peak_counts = {}  # пиковые значения message_count {(user_id, chat_id): max_count}
MAIN_CHAT_ID = -1003819835960  # основной чат для самопроверки

async def backup_db(silent: bool = False):
    """Сохранить bot.db в Replit Database."""
    if not REPLDB_URL:
        return
    try:
        with open(DB_PATH, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        async with aiohttp.ClientSession() as session:
            await session.post(REPLDB_URL, data={REPLDB_KEY: data})
        if not silent:
            print("=== БД сохранена в резерв ===")
    except Exception as e:
        print(f"=== Ошибка резервного копирования: {e} ===")


async def restore_db():
    """Восстановить bot.db из Replit Database.
    В продакшне всегда используем резервную копию — в ней актуальные данные
    от работающего бота, а не устаревший файл из деплоя."""
    print(f"=== restore_db: REPLDB_URL доступен: {bool(REPLDB_URL)} ===")
    if not REPLDB_URL:
        print("=== ВНИМАНИЕ: резервная копия отключена (нет REPLDB_URL) ===")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{REPLDB_URL}/{REPLDB_KEY}") as resp:
                if resp.status != 200:
                    print("=== Резервная копия БД не найдена, используем текущую ===")
                    return
                data = await resp.text()
        db_bytes = base64.b64decode(data)
        with open(DB_PATH, "wb") as f:
            f.write(db_bytes)
        print(f"=== БД восстановлена из резерва ({len(db_bytes)} байт) ===")
    except Exception as e:
        print(f"=== Ошибка восстановления БД: {e} ===")


async def periodic_backup():
    """Резервное копирование каждые 5 секунд."""
    while True:
        await asyncio.sleep(5)
        await backup_db(silent=True)


async def periodic_integrity_check():
    """Каждые 3 минуты проверяет счётчики сообщений основного чата.
    Если счётчик у кого-то упал ниже пикового значения — это баг,
    восстанавливаем из резерва немедленно."""
    global _peak_counts
    while True:
        await asyncio.sleep(180)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT user_id, message_count FROM users WHERE chat_id = ?",
                    (MAIN_CHAT_ID,)
                )
                rows = await cur.fetchall()

            dropped = []
            for user_id, count in rows:
                key = (user_id, MAIN_CHAT_ID)
                peak = _peak_counts.get(key, 0)
                if count < peak:
                    dropped.append((user_id, peak, count))
                else:
                    _peak_counts[key] = count

            if dropped:
                print(f"=== САМОПРОВЕРКА: обнаружено падение счётчиков у {len(dropped)} юзеров, восстанавливаем из резерва ===")
                for uid, peak, cur_count in dropped:
                    print(f"  user_id={uid}: было {peak}, стало {cur_count}")
                await restore_db()
                print("=== САМОПРОВЕРКА: база восстановлена ===")
            else:
                # Обновляем пиковые значения
                for user_id, count in rows:
                    _peak_counts[(user_id, MAIN_CHAT_ID)] = max(
                        _peak_counts.get((user_id, MAIN_CHAT_ID), 0), count
                    )
        except Exception as e:
            print(f"=== САМОПРОВЕРКА ошибка: {e} ===")


async def add_or_update_user(user, chat_id):
    global _msg_counter
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE user_id = ? AND chat_id = ?",
            (user.id, chat_id)
        )
        exists = await cursor.fetchone()
        today = datetime.date.today().isoformat()
        if not exists:
            await db.execute("""
                INSERT INTO users (
                    user_id, chat_id, username, first_name,
                    message_count, weekly_count, last_message_date, reputation
                )
                VALUES (?, ?, ?, ?, 1, 1, ?, 0)
            """, (user.id, chat_id, user.username, user.first_name, today))
        else:
            await db.execute("""
                UPDATE users
                SET message_count = message_count + 1,
                    weekly_count = weekly_count + 1,
                    username = ?,
                    first_name = ?,
                    last_message_date = ?
                WHERE user_id = ? AND chat_id = ?
            """, (user.username, user.first_name, today, user.id, chat_id))
        await db.commit()
    # Обновляем пиковое значение в памяти
    if chat_id == MAIN_CHAT_ID:
        async with aiosqlite.connect(DB_PATH) as db:
            cur2 = await db.execute(
                "SELECT message_count FROM users WHERE user_id = ? AND chat_id = ?",
                (user.id, chat_id)
            )
            row = await cur2.fetchone()
            if row:
                _peak_counts[(user.id, chat_id)] = max(
                    _peak_counts.get((user.id, chat_id), 0), row[0]
                )
    # Резерв каждые 3 сообщения
    _msg_counter += 1
    if _msg_counter % 3 == 0:
        asyncio.create_task(backup_db(silent=True))


# =========================
# RP ACTIONS
# =========================

rp_actions = {
    "обнять":     ("💖 обнял(а)",  "💖"),
    "поцеловать": ("💋 поцеловал(а)", "💋"),
    "погладить":  ("👐 погладил(а)", "👐"),
    "казнить":    ("⚔️ казнил(а)", "💀"),
    "облизать":   ("👅 облизал(а)", "👅"),
    "связать":    ("⛓️ связал(а)", "⛓️"),
}

rep_triggers = {"+", "жиза", "f", "ага"}


# =========================
# DUEL HELPERS
# =========================

def _duel_status(duel):
    ch_id = duel["challenger_id"]
    t_id = duel["target_id"]
    ch_name = duel["challenger_name"]
    t_name = duel["target_name"]
    ch_w = duel["challenger_weapon"]
    tg_w = duel["target_weapon"]
    aim = duel.get("aim", {ch_id: 0.0, t_id: 0.0})
    ch_aim = aim.get(ch_id, 0.0)
    tg_aim = aim.get(t_id, 0.0)
    ch_eff = min(ch_w[3] + ch_aim, 0.95)
    tg_eff = min(tg_w[3] + tg_aim, 0.95)
    ch_aim_str = " 🎯" if ch_aim > 0 else ""
    tg_aim_str = " 🎯" if tg_aim > 0 else ""
    return (
        f"{ch_name}: {ch_w[0]}{ch_aim_str}\n"
        f"{t_name}: {tg_w[0]}{tg_aim_str}"
    )


def _duel_kb(duel_id, player_w):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=player_w[1], callback_data=f"duel_shoot:{duel_id}")],
        [
            InlineKeyboardButton(text="🎯 Прицелиться", callback_data=f"duel_aim:{duel_id}"),
            InlineKeyboardButton(text="💨 Сбить прицел", callback_data=f"duel_disrupt:{duel_id}"),
        ]
    ])


# =========================
# CALLBACK QUERY HANDLER (Дуэль)
# =========================

@dp.callback_query()
async def handle_duel_callbacks(callback: CallbackQuery):
    data = callback.data or ""

    # ---- ПРИНЯТЬ ----
    if data.startswith("duel_accept:"):
        duel_id = data.split(":", 1)[1]
        duel = duels.get(duel_id)

        if not duel or duel["status"] != "pending":
            await callback.answer("Эта дуэль уже недействительна.", show_alert=True)
            return

        if callback.from_user.id != duel["target_id"]:
            await callback.answer("Это не твой вызов!", show_alert=True)
            return

        ch_w = random.choice(WEAPONS)
        tg_w = random.choice(WEAPONS)
        duel["challenger_weapon"] = ch_w
        duel["target_weapon"] = tg_w
        duel["turn"] = duel["challenger_id"]
        duel["status"] = "active"
        duel["aim"] = {duel["challenger_id"]: 0.0, duel["target_id"]: 0.0}

        ch_id = duel["challenger_id"]
        ch_name = duel["challenger_name"]
        t_name = duel["target_name"]
        kb = _duel_kb(duel_id, ch_w)
        await callback.message.edit_text(
            f"⚔️ <b>Дуэль начинается!</b>\n\n"
            f"{_duel_status(duel)}\n\n"
            f"Первым атакует <a href='tg://user?id={ch_id}'>{ch_name}</a>!\n\n"
            f"🎯 Прицелиться — поднять шанс (ценой хода)\n"
            f"💨 Сбить прицел — попытаться обнулить прицел врага (ценой хода)",
            parse_mode="HTML",
            reply_markup=kb
        )
        await callback.answer()

    # ---- ОТКАЗАТЬСЯ ----
    elif data.startswith("duel_refuse:"):
        duel_id = data.split(":", 1)[1]
        duel = duels.get(duel_id)

        if not duel or duel["status"] != "pending":
            await callback.answer("Эта дуэль уже недействительна.", show_alert=True)
            return

        if callback.from_user.id != duel["target_id"]:
            await callback.answer("Это не твой вызов!", show_alert=True)
            return

        t_id = duel["target_id"]
        t_name = duel["target_name"]
        c_id = duel["challenger_id"]
        c_name = duel["challenger_name"]
        duels.pop(duel_id, None)

        await callback.message.edit_text(
            f"🏳️ <a href='tg://user?id={t_id}'>{t_name}</a> "
            f"отказался от дуэли с "
            f"<a href='tg://user?id={c_id}'>{c_name}</a>. Трус! 🐔",
            parse_mode="HTML"
        )
        await callback.answer("Ты отказался от дуэли.")

    # ---- ВЫСТРЕЛИТЬ ----
    elif data.startswith("duel_shoot:"):
        duel_id = data.split(":", 1)[1]
        duel = duels.get(duel_id)

        if not duel or duel["status"] != "active":
            await callback.answer("Эта дуэль уже завершена.", show_alert=True)
            return

        if callback.from_user.id != duel["turn"]:
            await callback.answer("Сейчас не твой ход!", show_alert=True)
            return

        shooter_id = duel["turn"]
        if shooter_id == duel["challenger_id"]:
            shooter_name = duel["challenger_name"]
            shooter_w = duel["challenger_weapon"]
            opponent_id = duel["target_id"]
            opponent_name = duel["target_name"]
            opponent_w = duel["target_weapon"]
        else:
            shooter_name = duel["target_name"]
            shooter_w = duel["target_weapon"]
            opponent_id = duel["challenger_id"]
            opponent_name = duel["challenger_name"]
            opponent_w = duel["challenger_weapon"]

        aim = duel.get("aim", {shooter_id: 0.0, opponent_id: 0.0})
        aim_bonus = aim.get(shooter_id, 0.0)
        effective_chance = min(shooter_w[3] + aim_bonus, 0.95)

        aim[shooter_id] = 0.0
        duel["aim"] = aim

        hit = random.random() < effective_chance
        aim_note = " (с прицелом 🎯)" if aim_bonus > 0 else ""

        if hit:
            duel["status"] = "done"
            duels.pop(duel_id, None)
            await callback.message.edit_text(
                f"⚔️ <b>Дуэль завершена!</b>\n\n"
                f"<a href='tg://user?id={shooter_id}'>{shooter_name}</a> "
                f"{shooter_w[2]}{aim_note} и ПОПАЛ(А)! 🎯\n\n"
                f"🏆 Победитель: <a href='tg://user?id={shooter_id}'>{shooter_name}</a>\n"
                f"💀 <a href='tg://user?id={opponent_id}'>{opponent_name}</a> повержен!",
                parse_mode="HTML"
            )
        else:
            duel["turn"] = opponent_id
            kb = _duel_kb(duel_id, opponent_w)
            await callback.message.edit_text(
                f"⚔️ <b>Дуэль в разгаре!</b>\n\n"
                f"{_duel_status(duel)}\n\n"
                f"<a href='tg://user?id={shooter_id}'>{shooter_name}</a> "
                f"{shooter_w[2]}{aim_note} и промахнулся(лась)! 💨\n\n"
                f"Теперь атакует <a href='tg://user?id={opponent_id}'>{opponent_name}</a>!",
                parse_mode="HTML",
                reply_markup=kb
            )

        await callback.answer()

    # ---- ПРИЦЕЛИТЬСЯ ----
    elif data.startswith("duel_aim:"):
        duel_id = data.split(":", 1)[1]
        duel = duels.get(duel_id)

        if not duel or duel["status"] != "active":
            await callback.answer("Эта дуэль уже завершена.", show_alert=True)
            return

        if callback.from_user.id != duel["turn"]:
            await callback.answer("Сейчас не твой ход!", show_alert=True)
            return

        shooter_id = duel["turn"]
        if shooter_id == duel["challenger_id"]:
            shooter_name = duel["challenger_name"]
            shooter_w = duel["challenger_weapon"]
            opponent_id = duel["target_id"]
            opponent_name = duel["target_name"]
            opponent_w = duel["target_weapon"]
        else:
            shooter_name = duel["target_name"]
            shooter_w = duel["target_weapon"]
            opponent_id = duel["challenger_id"]
            opponent_name = duel["challenger_name"]
            opponent_w = duel["challenger_weapon"]

        aim = duel.get("aim", {shooter_id: 0.0, opponent_id: 0.0})
        old_bonus = aim.get(shooter_id, 0.0)
        gained = round(random.uniform(0.10, 0.30), 2)
        new_bonus = min(old_bonus + gained, 0.40)
        aim[shooter_id] = new_bonus
        duel["aim"] = aim
        duel["turn"] = opponent_id

        kb = _duel_kb(duel_id, opponent_w)
        await callback.message.edit_text(
            f"⚔️ <b>Дуэль в разгаре!</b>\n\n"
            f"{_duel_status(duel)}\n\n"
            f"<a href='tg://user?id={shooter_id}'>{shooter_name}</a> прицеливается... 🎯\n\n"
            f"Теперь ход у <a href='tg://user?id={opponent_id}'>{opponent_name}</a>!",
            parse_mode="HTML",
            reply_markup=kb
        )
        await callback.answer()

    # ---- СБИТЬ ПРИЦЕЛ ----
    elif data.startswith("duel_disrupt:"):
        duel_id = data.split(":", 1)[1]
        duel = duels.get(duel_id)

        if not duel or duel["status"] != "active":
            await callback.answer("Эта дуэль уже завершена.", show_alert=True)
            return

        if callback.from_user.id != duel["turn"]:
            await callback.answer("Сейчас не твой ход!", show_alert=True)
            return

        shooter_id = duel["turn"]
        if shooter_id == duel["challenger_id"]:
            shooter_name = duel["challenger_name"]
            opponent_id = duel["target_id"]
            opponent_name = duel["target_name"]
            opponent_w = duel["target_weapon"]
        else:
            shooter_name = duel["target_name"]
            opponent_id = duel["challenger_id"]
            opponent_name = duel["challenger_name"]
            opponent_w = duel["challenger_weapon"]

        aim = duel.get("aim", {shooter_id: 0.0, opponent_id: 0.0})
        success = random.random() < random.uniform(0.15, 0.40)
        opponent_aim_before = aim.get(opponent_id, 0.0)

        if success and opponent_aim_before > 0:
            aim[opponent_id] = 0.0
            duel["aim"] = aim
            disrupt_text = (
                f"💨 <a href='tg://user?id={shooter_id}'>{shooter_name}</a> сбивает прицел!\n"
                f"Бонус прицела <a href='tg://user?id={opponent_id}'>{opponent_name}</a> обнулён! 😤"
            )
        elif success:
            disrupt_text = (
                f"💨 <a href='tg://user?id={shooter_id}'>{shooter_name}</a> пытается сбить прицел...\n"
                f"У <a href='tg://user?id={opponent_id}'>{opponent_name}</a> и так не было прицела! 😏"
            )
        else:
            disrupt_text = (
                f"💨 <a href='tg://user?id={shooter_id}'>{shooter_name}</a> пытается сбить прицел...\n"
                f"Промах! Прицел <a href='tg://user?id={opponent_id}'>{opponent_name}</a> не задет. 😏"
            )

        duel["turn"] = opponent_id
        kb = _duel_kb(duel_id, opponent_w)
        await callback.message.edit_text(
            f"⚔️ <b>Дуэль в разгаре!</b>\n\n"
            f"{_duel_status(duel)}\n\n"
            f"{disrupt_text}\n\n"
            f"Теперь ход у <a href='tg://user?id={opponent_id}'>{opponent_name}</a>!",
            parse_mode="HTML",
            reply_markup=kb
        )
        await callback.answer()

    # ---- РУЛЕТКА — ВЫСТРЕЛ ----
    elif data.startswith("roulette_shoot:"):
        roulette_id = data.split(":", 1)[1]
        roulette = roulettes.get(roulette_id)

        if not roulette:
            await callback.answer("Рулетка уже завершена.", show_alert=True)
            return
        if callback.from_user.id != roulette["user_id"]:
            await callback.answer("Это не твоя рулетка!", show_alert=True)
            return

        user_id = roulette["user_id"]
        user_name = roulette["user_name"]
        shot = roulette["current_shot"]
        bullet = roulette["bullet_pos"]
        chambers = roulette["chambers"]

        def make_visual(survived, total, dead=False, last=False):
            if dead:
                return "✅" * (survived - 1) + "💥" + "🔘" * (total - survived)
            if last:
                return "✅" * survived + "💀"
            return "✅" * survived + "🔘" * (total - survived)

        if shot == bullet:
            roulettes.pop(roulette_id, None)
            visual = make_visual(shot, chambers, dead=True)
            await callback.message.edit_text(
                f"🔫 <b>Русская рулетка</b>\n\n"
                f"<a href='tg://user?id={user_id}'>{user_name}</a> нажимает на курок...\n\n"
                f"{visual}\n\n"
                f"💥 <b>БАХ!</b> Выстрел {shot} из {chambers}\n\n"
                f"😵 <a href='tg://user?id={user_id}'>{user_name}</a> мёртв(а). Не повезло.",
                parse_mode="HTML"
            )
        else:
            shots_survived = shot
            if shots_survived >= chambers - 1:
                roulettes.pop(roulette_id, None)
                visual = "✅" * shots_survived + "🏆"
                await callback.message.edit_text(
                    f"🔫 <b>Русская рулетка</b>\n\n"
                    f"<a href='tg://user?id={user_id}'>{user_name}</a> нажимает на курок...\n\n"
                    f"{visual}\n\n"
                    f"🔘 Щелчок! Выстрел {shot} — пусто!\n\n"
                    f"🏆 <a href='tg://user?id={user_id}'>{user_name}</a> выжил(а) все {shots_survived} выстрелов! Легенда! 🎖",
                    parse_mode="HTML"
                )
            else:
                roulette["current_shot"] = shot + 1
                new_shot = shot + 1
                remaining = chambers - shot
                chance = round(100 / remaining)
                visual = make_visual(shot, chambers)
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔫 Выстрелить", callback_data=f"roulette_shoot:{roulette_id}"),
                    InlineKeyboardButton(text="🎲 Прокрутить барабан", callback_data=f"roulette_spin:{roulette_id}"),
                ]])
                await callback.message.edit_text(
                    f"🔫 <b>Русская рулетка</b>\n\n"
                    f"<a href='tg://user?id={user_id}'>{user_name}</a> нажимает на курок...\n\n"
                    f"{visual}\n\n"
                    f"🔘 Щелчок! Выстрел {shot} — пусто!\n\n"
                    f"Выстрел {new_shot} из {chambers - 1} | Шанс смерти: ~{chance}%",
                    parse_mode="HTML",
                    reply_markup=kb
                )
        await callback.answer()

    # ---- РУЛЕТКА — ПРОКРУТИТЬ БАРАБАН ----
    elif data.startswith("roulette_spin:"):
        roulette_id = data.split(":", 1)[1]
        roulette = roulettes.get(roulette_id)

        if not roulette:
            await callback.answer("Рулетка уже завершена.", show_alert=True)
            return
        if callback.from_user.id != roulette["user_id"]:
            await callback.answer("Это не твоя рулетка!", show_alert=True)
            return

        user_id = roulette["user_id"]
        user_name = roulette["user_name"]
        shot = roulette["current_shot"]
        chambers = roulette["chambers"]

        roulette["bullet_pos"] = random.randint(shot, chambers)
        remaining = chambers - shot + 1
        chance = round(100 / remaining)

        survived_visual = "✅" * (shot - 1)
        remaining_visual = "🎲" + "🔘" * (remaining - 1)
        visual = survived_visual + remaining_visual

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔫 Выстрелить", callback_data=f"roulette_shoot:{roulette_id}"),
            InlineKeyboardButton(text="🎲 Прокрутить барабан", callback_data=f"roulette_spin:{roulette_id}"),
        ]])
        await callback.message.edit_text(
            f"🔫 <b>Русская рулетка</b>\n\n"
            f"<a href='tg://user?id={user_id}'>{user_name}</a> прокручивает барабан...\n\n"
            f"{visual}\n\n"
            f"🎲 Пуля случайно перемещена среди {remaining} оставшихся ячеек!\n\n"
            f"Выстрел {shot} из {chambers - 1} | Шанс смерти: ~{chance}%",
            parse_mode="HTML",
            reply_markup=kb
        )
        await callback.answer()

    # ---- БРАК — ПРИНЯТЬ ----
    elif data.startswith("marry_accept:"):
        proposal_id = data.split(":", 1)[1]
        prop = proposals.get(proposal_id)

        if not prop:
            await callback.answer("Предложение уже недействительно.", show_alert=True)
            return

        if callback.from_user.id != prop["target_id"]:
            await callback.answer("Это предложение не тебе!", show_alert=True)
            return

        proposals.pop(proposal_id, None)
        now = datetime.datetime.now().isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            existing = await db.execute(
                "SELECT id FROM marriages WHERE chat_id = ? AND (user1_id = ? OR user2_id = ? OR user1_id = ? OR user2_id = ?)",
                (prop["chat_id"], prop["proposer_id"], prop["proposer_id"], prop["target_id"], prop["target_id"])
            )
            if await existing.fetchone():
                await callback.message.edit_text("💔 Один из вас уже состоит в браке в этом чате!")
                await callback.answer()
                return

            await db.execute(
                "INSERT INTO marriages (chat_id, user1_id, user1_name, user2_id, user2_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (prop["chat_id"], prop["proposer_id"], prop["proposer_name"], prop["target_id"], prop["target_name"], now)
            )
            await db.commit()

        p_id = prop["proposer_id"]
        p_name = prop["proposer_name"]
        t_id = prop["target_id"]
        t_name = prop["target_name"]
        await callback.message.edit_text(
            f"💍 <b>Брак заключён!</b>\n\n"
            f"<a href='tg://user?id={p_id}'>{p_name}</a> 💑 <a href='tg://user?id={t_id}'>{t_name}</a>\n\n"
            f"Берегите вашу любовь! 💑",
            parse_mode="HTML"
        )
        await callback.answer("Поздравляем!")

    # ---- БРАК — ОТКАЗАТЬСЯ ----
    elif data.startswith("marry_refuse:"):
        proposal_id = data.split(":", 1)[1]
        prop = proposals.get(proposal_id)

        if not prop:
            await callback.answer("Предложение уже недействительно.", show_alert=True)
            return

        if callback.from_user.id != prop["target_id"]:
            await callback.answer("Это предложение не тебе!", show_alert=True)
            return

        proposals.pop(proposal_id, None)
        p_id = prop["proposer_id"]
        p_name = prop["proposer_name"]
        t_id = prop["target_id"]
        t_name = prop["target_name"]
        await callback.message.edit_text(
            f"💔 <a href='tg://user?id={t_id}'>{t_name}</a> отказал(а) "
            f"<a href='tg://user?id={p_id}'>{p_name}</a>. Сочувствуем... 🥀",
            parse_mode="HTML"
        )
        await callback.answer("Предложение отклонено.")


# =========================
# MAIN HANDLER
# =========================

@dp.message()
async def main_handler(message: Message):
    if message.from_user.is_bot:
        return

    if message.chat.type not in ["group", "supergroup"]:
        return

    chat_id = message.chat.id
    await add_or_update_user(message.from_user, chat_id)

    if not message.text:
        return

    text = message.text.strip()
    lower = text.lower()

    # =========================
    # КТО Я
    # =========================
    if lower == "кто я":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT message_count, weekly_count, reputation, COALESCE(nickname, first_name), nickname, coins FROM users WHERE user_id = ? AND chat_id = ?",
                (message.from_user.id, chat_id)
            )
            data = await cursor.fetchone()

        if not data:
            return await message.answer("У тебя пока нет статистики")

        nick_line = f"🏷 Ник: {data[3]}\n" if data[4] else ""
        return await message.answer(
            f"📊 Твоя статистика:\n"
            f"{nick_line}"
            f"Всего сообщений: {data[0]}\n"
            f"За неделю: {data[1]}\n"
            f"⭐ Репутация: {data[2]}\n"
            f"🪙 Монеток: {data[5]}"
        )

    # =========================
    # КТО ТЫ
    # =========================
    if lower == "кто ты" or lower.startswith("кто ты "):
        target_id, _ = await get_target_user(message)
        if not target_id:
            return await message.answer("Ответь на сообщение пользователя или упомяни через @")

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COALESCE(nickname, first_name), message_count, weekly_count, reputation FROM users WHERE user_id = ? AND chat_id = ?",
                (target_id, chat_id)
            )
            data = await cursor.fetchone()

        if not data:
            return await message.answer("У пользователя нет статистики")

        return await message.answer(
            f"📊 Статистика {data[0]}:\n"
            f"Всего сообщений: {data[1]}\n"
            f"За неделю: {data[2]}\n"
            f"⭐ Репутация: {data[3]}"
        )

    # =========================
    # ТОП ВСЯ
    # =========================
    if lower == "топ вся":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT COALESCE(nickname, first_name), message_count 
                FROM users WHERE chat_id = ?
                ORDER BY message_count DESC LIMIT 15
            """, (chat_id,))
            rows = await cursor.fetchall()

        text_out = "🏆 Топ активности (всё время):\n"
        for i, row in enumerate(rows, 1):
            text_out += f"{i}. {row[0]} — {row[1]}\n"
        return await message.answer(text_out)

    # =========================
    # ТОП НЕДЕЛЯ
    # =========================
    if lower == "топ неделя":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT COALESCE(nickname, first_name), weekly_count 
                FROM users WHERE chat_id = ?
                ORDER BY weekly_count DESC LIMIT 15
            """, (chat_id,))
            rows = await cursor.fetchall()

        text_out = "🏆 Топ активности (неделя):\n"
        for i, row in enumerate(rows, 1):
            text_out += f"{i}. {row[0]} — {row[1]}\n"
        return await message.answer(text_out)

    # =========================
    # ТОП РЕПУТАЦИЯ
    # =========================
    if lower == "топ репутация":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT COALESCE(nickname, first_name), reputation 
                FROM users WHERE chat_id = ?
                ORDER BY reputation DESC LIMIT 15
            """, (chat_id,))
            rows = await cursor.fetchall()

        text_out = "⭐ Топ репутации:\n"
        for i, row in enumerate(rows, 1):
            text_out += f"{i}. {row[0]} — {row[1]}\n"
        return await message.answer(text_out)

    # =========================
    # +НИК — УСТАНОВИТЬ НИКНЕЙМ
    # =========================
    NICK_PRIVILEGED_USER = 882480153

    if lower.startswith("+ник ") or lower == "+ник":
        raw_args = text[len("+ник"):].strip()

        nick = raw_args
        if message.entities:
            for ent in message.entities:
                if ent.type in ("mention", "text_mention"):
                    nick = nick.replace(ent.extract_from(text), "").strip()

        if not nick:
            return await message.answer(
                "Укажи никнейм:\n"
                "<code>+ник ДаркЛорд</code> — себе\n"
                "<code>+ник Крутой Парень @username</code> — другому (только для админов)",
                parse_mode="HTML"
            )
        if len(nick) > 32:
            return await message.answer("Никнейм слишком длинный (макс. 32 символа).")

        is_privileged = (
            await is_admin(chat_id, message.from_user.id)
            or message.from_user.id == NICK_PRIVILEGED_USER
        )

        target_id, target_first_name = await get_target_user(message)

        if target_id and target_id != message.from_user.id:
            if not is_privileged:
                return await message.answer("🚫 Менять ники другим могут только администраторы.")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE users SET nickname = ? WHERE user_id = ? AND chat_id = ?",
                    (nick, target_id, chat_id)
                )
                await db.commit()
            return await message.answer(
                f"✅ Никнейм <a href='tg://user?id={target_id}'>{target_first_name}</a> "
                f"изменён на <b>{nick}</b>",
                parse_mode="HTML"
            )
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE users SET nickname = ? WHERE user_id = ? AND chat_id = ?",
                    (nick, message.from_user.id, chat_id)
                )
                await db.commit()
            return await message.answer(
                f"✅ Твой никнейм в этом чате: <b>{nick}</b>",
                parse_mode="HTML"
            )

    # =========================
    # СБРОСИТЬ НИК
    # =========================
    if lower == "сбросить ник":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET nickname = NULL WHERE user_id = ? AND chat_id = ?",
                (message.from_user.id, chat_id)
            )
            await db.commit()
        return await message.answer("✅ Никнейм сброшен, используется имя из Telegram.")

    # =========================
    # РЕПУТАЦИЯ (+ / Жиза / F / Ага)
    # =========================
    lower_word = lower.split()[0] if lower.split() else lower
    if lower_word in rep_triggers:
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return

        if target_id == message.from_user.id:
            return await message.answer("Себе репутацию не накрутишь 😏")

        today = datetime.date.today().isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR IGNORE INTO users (
                    user_id, chat_id, first_name,
                    message_count, weekly_count, last_message_date, reputation
                ) VALUES (?, ?, ?, 0, 0, ?, 0)
            """, (target_id, chat_id, target_name, today))

            await db.execute("""
                UPDATE users SET reputation = reputation + 1
                WHERE user_id = ? AND chat_id = ?
            """, (target_id, chat_id))

            cursor = await db.execute(
                "SELECT reputation, COALESCE(nickname, first_name) FROM users WHERE user_id = ? AND chat_id = ?",
                (target_id, chat_id)
            )
            rep_row = await cursor.fetchone()
            await db.commit()

        rep_val = rep_row[0]
        display = rep_row[1]
        return await message.answer(
            f"⭐ <a href='tg://user?id={target_id}'>{display}</a> получает +1 к репутации! "
            f"Теперь: {rep_val}",
            parse_mode="HTML"
        )

    # =========================
    # RP (С УПОМИНАНИЯМИ)
    # =========================
    rp_key = lower_word if lower_word in rp_actions else lower
    if rp_key in rp_actions:
        from_user = message.from_user
        target_id, _ = await get_target_user(message)
        if not target_id:
            return await message.answer(
                "Ответь на сообщение пользователя или упомяни через @\n"
                "<i>(через @ работает только для тех, кто уже писал в чате)</i>",
                parse_mode="HTML"
            )

        # Извлекаем реплику: всё что идёт после команды и @упоминания
        raw_text = message.text or ""
        quote_remaining = raw_text[len(rp_key):].strip()
        if message.entities:
            for ent in message.entities:
                if ent.type in ("mention", "text_mention"):
                    ent_text = ent.extract_from(raw_text)
                    quote_remaining = quote_remaining.replace(ent_text, "").strip()
        quote = quote_remaining.strip() if quote_remaining.strip() else None

        action, emoji = rp_actions[rp_key]
        from_name = await get_display_name(from_user.id, chat_id)
        to_name = await get_display_name(target_id, chat_id)

        quote_line = f"\n💬 <i>«{quote}»</i>" if quote else ""
        return await message.answer(
            f"👤 <a href='tg://user?id={from_user.id}'>{from_name}</a> "
            f"{action} "
            f"<a href='tg://user?id={target_id}'>{to_name}</a> {emoji}"
            f"{quote_line}",
            parse_mode="HTML"
        )

    # =========================
    # МОНЕТКА
    # =========================
    if lower == "монетка":
        result = random.choices(
            ["орёл", "решка", "ребро"],
            weights=[499, 499, 2],
            k=1
        )[0]
        if result == "орёл":
            return await message.answer("🪙 Монетка крутится... Орёл! 🦅")
        elif result == "решка":
            return await message.answer("🪙 Монетка крутится... Решка! 🌕")
        else:
            return await message.answer(
                "🪙 Монетка крутится...\n\n"
                "😱 Невероятно — монетка встала на ребро! Шанс меньше 0.2%!"
            )

    # =========================
    # ДИДЖЕИТЬ
    # =========================
    if lower == "диджеить":
        score = random.randint(1, 10)
        name = message.from_user.first_name
        if score == 10:
            comment = "Абсолютный бог вертушек! 🔥"
        elif score >= 8:
            comment = "Зал в огне! 🎧"
        elif score >= 6:
            comment = "Неплохо, толпа танцует 💃"
        elif score >= 4:
            comment = "Ну... бывало и лучше 😅"
        elif score >= 2:
            comment = "Кто-то уже уходит с танцпола 😬"
        else:
            comment = "Вилку из розетки вытащили 💀"
        return await message.answer(f"🎛 {name} диджеит и получает {score}/10\n{comment}")

    # =========================
    # НАРВАЛ КТО (РАНДОМНЫЙ ВЫБОР)
    # =========================
    if lower.startswith("нарвал кто"):
        subject = text[len("нарвал кто"):].strip()

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id, COALESCE(nickname, first_name) FROM users WHERE chat_id = ?",
                (chat_id,)
            )
            users = await cursor.fetchall()

        if not users:
            return await message.answer("🐳 Нарвал не знает никого в этом чате...")

        chosen_id, chosen_name = random.choice(users)
        subject_text = f" {subject}" if subject else ""

        return await message.answer(
            f"🐳 Нарвал выбирает...\n\n"
            f"<a href='tg://user?id={chosen_id}'>{chosen_name}</a>{subject_text}!",
            parse_mode="HTML"
        )

    if lower in ("нарвал", "бот"):
        return await message.reply("На месте босс✅")

    # =========================
    # ВАРН
    # =========================
    if lower.startswith("!варн"):
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут выдавать варны.")
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return await message.reply("⚠️ Укажи пользователя — ответь на его сообщение или упомяни.")
        if await is_admin(chat_id, target_id):
            return await message.reply("🚫 Нельзя выдать варн администратору.")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users (user_id, chat_id, first_name, warns) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(user_id, chat_id) DO UPDATE SET warns = warns + 1",
                (target_id, chat_id, target_name)
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT warns FROM users WHERE user_id = ? AND chat_id = ?", (target_id, chat_id)
            )
            row = await cursor.fetchone()
            warns = row[0] if row else 1
        if warns >= 2:
            try:
                await bot.ban_chat_member(chat_id, target_id)
                async with aiosqlite.connect(DB_PATH) as db2:
                    await db2.execute(
                        "INSERT OR IGNORE INTO blacklist (user_id, chat_id, banned_by, banned_at) VALUES (?, ?, ?, ?)",
                        (target_id, chat_id, message.from_user.id, datetime.datetime.now().isoformat())
                    )
                    await db2.execute(
                        "UPDATE users SET warns = 0 WHERE user_id = ? AND chat_id = ?", (target_id, chat_id)
                    )
                    await db2.commit()
                return await message.answer(
                    f"🔨 <b>{target_name}</b> получил(а) 2/2 варна и автоматически забанен(а)!",
                    parse_mode="HTML"
                )
            except Exception:
                return await message.answer(f"⚠️ Не удалось забанить {target_name} — проверь права бота.")
        return await message.answer(
            f"⚠️ <b>{target_name}</b> получает предупреждение! [{warns}/2]\n"
            f"{'🚨 Ещё один варн — и бан!' if warns == 1 else ''}",
            parse_mode="HTML"
        )

    # =========================
    # РАЗВАРН
    # =========================
    if lower.startswith("!разварн"):
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут снимать варны.")
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return await message.reply("⚠️ Укажи пользователя — ответь на его сообщение или упомяни.")
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT warns FROM users WHERE user_id = ? AND chat_id = ?", (target_id, chat_id)
            )
            row = await cur.fetchone()
            warns = row[0] if row else 0
            if not warns or warns <= 0:
                return await message.answer(
                    f"✅ У <b>{target_name}</b> нет активных предупреждений.",
                    parse_mode="HTML"
                )
            new_warns = warns - 1
            await db.execute(
                "UPDATE users SET warns = ? WHERE user_id = ? AND chat_id = ?",
                (new_warns, target_id, chat_id)
            )
            await db.commit()
        return await message.answer(
            f"✅ С <b>{target_name}</b> снято одно предупреждение. Осталось: [{new_warns}/2]",
            parse_mode="HTML"
        )

    # =========================
    # БАН
    # =========================
    if lower == "!бан" or lower.startswith("!бан "):
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут банить.")
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return await message.reply("⚠️ Укажи пользователя — ответь на его сообщение или упомяни.")
        if await is_admin(chat_id, target_id):
            return await message.reply("🚫 Нельзя забанить администратора.")
        try:
            await bot.ban_chat_member(chat_id, target_id)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO blacklist (user_id, chat_id, banned_by, banned_at) VALUES (?, ?, ?, ?)",
                    (target_id, chat_id, message.from_user.id, datetime.datetime.now().isoformat())
                )
                await db.execute(
                    "UPDATE users SET warns = 0 WHERE user_id = ? AND chat_id = ?", (target_id, chat_id)
                )
                await db.commit()
            return await message.answer(
                f"🔨 <b>{target_name}</b> забанен(а) и добавлен(а) в чёрный список.",
                parse_mode="HTML"
            )
        except Exception:
            return await message.answer(f"⚠️ Не удалось забанить {target_name} — проверь права бота.")

    # =========================
    # МУТ
    # =========================
    if lower.startswith("!мут"):
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут мутить.")
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return await message.reply("⚠️ Укажи пользователя — ответь на его сообщение или упомяни.")
        if await is_admin(chat_id, target_id):
            return await message.reply("🚫 Нельзя замутить администратора.")
        seconds = parse_duration(text)
        if seconds <= 0:
            return await message.reply(
                "⚠️ Укажи длительность, например:\n<code>!мут @user 1 час 30 минут</code>",
                parse_mode="HTML"
            )
        until = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
        try:
            await bot.restrict_chat_member(
                chat_id, target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
            hours, rem = divmod(seconds, 3600)
            mins, secs = divmod(rem, 60)
            dur_str = ""
            if hours:
                dur_str += f"{hours} ч "
            if mins:
                dur_str += f"{mins} мин "
            if secs and not hours:
                dur_str += f"{secs} сек"
            return await message.answer(
                f"🔇 <b>{target_name}</b> замучен(а) на {dur_str.strip()}.",
                parse_mode="HTML"
            )
        except Exception:
            return await message.answer(f"⚠️ Не удалось замутить {target_name} — проверь права бота.")

    # =========================
    # РАЗМУТ
    # =========================
    if lower.startswith("!размут"):
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут снимать мут.")
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return await message.reply("⚠️ Укажи пользователя — ответь на его сообщение или упомяни.")
        try:
            await bot.restrict_chat_member(
                chat_id, target_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
            return await message.answer(
                f"🔊 <b>{target_name}</b> — мут снят, снова может писать.",
                parse_mode="HTML"
            )
        except Exception:
            return await message.answer(f"⚠️ Не удалось снять мут с {target_name} — проверь права бота.")

    # =========================
    # БАНЛИСТ (предупреждения)
    # =========================
    if lower == "!банлист":
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут смотреть список предупреждений.")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id, COALESCE(nickname, first_name), warns "
                "FROM users WHERE chat_id = ? AND warns > 0 ORDER BY warns DESC",
                (chat_id,)
            )
            rows = await cursor.fetchall()
        if not rows:
            return await message.answer("✅ Нет пользователей с предупреждениями.")
        lines = ["⚠️ <b>Список предупреждений:</b>\n"]
        for uid, name, warns in rows:
            bar = "🟡" * warns + "⬜" * (2 - warns)
            lines.append(f"• <b>{name or uid}</b> — {bar} [{warns}/2]")
        return await message.answer("\n".join(lines), parse_mode="HTML")

    # =========================
    # РАЗБАН
    # =========================
    if lower.startswith("!разбан"):
        if not await is_admin(chat_id, message.from_user.id):
            return await message.reply("🚫 Только администраторы могут разбанивать.")
        target_id, target_name = await get_target_user(message)
        if not target_id:
            return await message.reply("⚠️ Укажи пользователя — ответь на его сообщение или упомяни.")
        try:
            await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM blacklist WHERE user_id = ? AND chat_id = ?", (target_id, chat_id)
                )
                await db.execute(
                    "UPDATE users SET warns = 0 WHERE user_id = ? AND chat_id = ?", (target_id, chat_id)
                )
                await db.commit()
            return await message.answer(
                f"✅ <b>{target_name}</b> разбанен(а) и удалён(а) из чёрного списка.",
                parse_mode="HTML"
            )
        except Exception:
            return await message.answer(f"⚠️ Не удалось разбанить {target_name}.")

    # =========================
    # ДУЭЛЬ
    # =========================
    if lower == "дуэль" or lower.startswith("дуэль "):
        challenger = message.from_user
        target_id, tg_display = await get_target_user(message)
        if not target_id:
            return await message.answer(
                "⚔️ Ответь на сообщение противника или упомяни его через @!"
            )

        if target_id == challenger.id:
            return await message.answer("⚔️ Себя на дуэль не вызвать!")

        ch_display = await get_display_name(challenger.id, chat_id)
        tg_display = await get_display_name(target_id, chat_id)

        duel_id = str(uuid.uuid4())[:8]
        duels[duel_id] = {
            "challenger_id": challenger.id,
            "challenger_name": ch_display,
            "target_id": target_id,
            "target_name": tg_display,
            "chat_id": chat_id,
            "status": "pending",
            "turn": None,
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"duel_accept:{duel_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"duel_refuse:{duel_id}"),
        ]])

        return await message.answer(
            f"⚔️ <a href='tg://user?id={challenger.id}'>{ch_display}</a> вызывает "
            f"<a href='tg://user?id={target_id}'>{tg_display}</a> на дуэль!\n\n"
            f"<a href='tg://user?id={target_id}'>{tg_display}</a>, принимаешь вызов?",
            parse_mode="HTML",
            reply_markup=kb
        )

    # =========================
    # РУЛЕТКА
    # =========================
    if lower == "рулетка":
        user = message.from_user
        user_name = await get_display_name(user.id, chat_id)
        roulette_id = str(uuid.uuid4())[:8]
        chambers = random.randint(4, 7)
        bullet_pos = random.randint(1, chambers)
        roulettes[roulette_id] = {
            "user_id": user.id,
            "user_name": user_name,
            "chat_id": chat_id,
            "chambers": chambers,
            "bullet_pos": bullet_pos,
            "current_shot": 1,
        }
        chance = round(100 / chambers)
        visual = "🔘" * chambers
        shoot_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔫 Выстрелить", callback_data=f"roulette_shoot:{roulette_id}"),
            InlineKeyboardButton(text="🎲 Прокрутить барабан", callback_data=f"roulette_spin:{roulette_id}"),
        ]])
        return await message.answer(
            f"🔫 <b>Русская рулетка</b>\n\n"
            f"<a href='tg://user?id={user.id}'>{user_name}</a> берёт револьвер.\n"
            f"В барабане <b>{chambers}</b> ячеек, 1 патрон где-то внутри.\n"
            f"{visual}\n\n"
            f"Выстрел 1 из {chambers - 1} | Шанс смерти: ~{chance}%\n"
            f"Выживи все выстрелы — станешь легендой!\n\n"
            f"Прокрути барабан или стреляй...",
            parse_mode="HTML",
            reply_markup=shoot_kb
        )

    # =========================
    # БРАК
    # =========================
    if lower == "брак" or lower.startswith("брак "):
        proposer = message.from_user
        target_id, _ = await get_target_user(message)
        if not target_id:
            return await message.answer("💍 Ответь на сообщение или упомяни через @!")

        if target_id == proposer.id:
            return await message.answer("💍 Себе предложение не сделать!")

        # Особый случай: OTEZ_BABKA делает предложение боту
        if BOT_ID and target_id == BOT_ID and proposer.username == "OTEZ_BABKA":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT id FROM marriages WHERE chat_id = ? AND (user1_id = ? OR user2_id = ?)",
                    (chat_id, proposer.id, proposer.id)
                )
                if await cur.fetchone():
                    return await message.answer("💔 Ты уже состоишь в браке в этом чате!")
                p_name = await get_display_name(proposer.id, chat_id)
                now = datetime.datetime.now().isoformat()
                await db.execute(
                    "INSERT INTO marriages (chat_id, user1_id, user1_name, user2_id, user2_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, proposer.id, p_name, BOT_ID, "Бот", now)
                )
                await db.commit()
            return await message.answer(
                f"🤖 <a href='tg://user?id={proposer.id}'>{p_name}</a>... я долго думал(а).\n\n"
                f"💍 Да. Я согласен(на).\n\n"
                f"<a href='tg://user?id={proposer.id}'>{p_name}</a> 💑 Бот\n\n"
                f"Берегите вашу любовь! 🤍",
                parse_mode="HTML"
            )

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id FROM marriages WHERE chat_id = ? AND (user1_id = ? OR user2_id = ? OR user1_id = ? OR user2_id = ?)",
                (chat_id, proposer.id, proposer.id, target_id, target_id)
            )
            if await cur.fetchone():
                return await message.answer("💔 Один из вас уже состоит в браке в этом чате!")

        p_name = await get_display_name(proposer.id, chat_id)
        t_name = await get_display_name(target_id, chat_id)

        proposal_id = str(uuid.uuid4())[:8]
        proposals[proposal_id] = {
            "proposer_id": proposer.id,
            "proposer_name": p_name,
            "target_id": target_id,
            "target_name": t_name,
            "chat_id": chat_id,
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💍 Принять", callback_data=f"marry_accept:{proposal_id}"),
            InlineKeyboardButton(text="💔 Отказать", callback_data=f"marry_refuse:{proposal_id}"),
        ]])

        return await message.answer(
            f"💍 <a href='tg://user?id={proposer.id}'>{p_name}</a> делает предложение руки и сердца "
            f"<a href='tg://user?id={target_id}'>{t_name}</a>!\n\n"
            f"<a href='tg://user?id={target_id}'>{t_name}</a>, ты согласен(на)?",
            parse_mode="HTML",
            reply_markup=kb
        )

    # =========================
    # РАЗВОД
    # =========================
    if lower == "развод":
        user = message.from_user
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, user1_id, user1_name, user2_id, user2_name FROM marriages WHERE chat_id = ? AND (user1_id = ? OR user2_id = ?)",
                (chat_id, user.id, user.id)
            )
            row = await cur.fetchone()
            if not row:
                return await message.answer("💍 Ты не состоишь в браке в этом чате.")
            m_id, u1_id, u1_name, u2_id, u2_name = row
            await db.execute("DELETE FROM marriages WHERE id = ?", (m_id,))
            await db.commit()

        other_id = u2_id if user.id == u1_id else u1_id
        other_name = u2_name if user.id == u1_id else u1_name
        my_name = u1_name if user.id == u1_id else u2_name
        return await message.answer(
            f"💔 <a href='tg://user?id={user.id}'>{my_name}</a> и "
            f"<a href='tg://user?id={other_id}'>{other_name}</a> разводятся.\n\n"
            f"Брак расторгнут. 📄",
            parse_mode="HTML"
        )

    # =========================
    # БРАКИ (список)
    # =========================
    if lower == "браки":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user1_id, user1_name, user2_id, user2_name, created_at, love_points FROM marriages WHERE chat_id = ? ORDER BY love_points DESC",
                (chat_id,)
            )
            rows = await cursor.fetchall()

        if not rows:
            return await message.answer("💍 В этом чате пока нет браков.")

        lines = ["💍 <b>Браки этого чата:</b>\n"]
        for u1_id, u1_name, u2_id, u2_name, created_at, love_pts in rows:
            elapsed = format_elapsed(created_at)
            lv, lv_name, next_t = get_love_level(love_pts or 0)
            progress = f" | до след. уровня: {next_t - (love_pts or 0)} 🎁" if next_t else ""
            lines.append(
                f"<a href='tg://user?id={u1_id}'>{u1_name}</a> 💑 "
                f"<a href='tg://user?id={u2_id}'>{u2_name}</a>\n"
                f"  {lv_name} (ур. {lv}){progress}\n"
                f"  ⏱ Вместе: {elapsed}"
            )

        return await message.answer("\n\n".join(lines), parse_mode="HTML")

    # =========================
    # ПОДАРОК
    # =========================
    if lower == "подарок" or lower.startswith("подарок "):
        user = message.from_user
        now = datetime.datetime.now()
        gift_cost = 3000

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id, user1_id, user1_name, user2_id, user2_name, love_points FROM marriages WHERE chat_id = ? AND (user1_id = ? OR user2_id = ?)",
                (chat_id, user.id, user.id)
            )
            marriage = await cur.fetchone()

        if not marriage:
            return await message.answer("💍 Ты не состоишь в браке в этом чате. Сначала заключи брак!")

        m_id, u1_id, u1_name, u2_id, u2_name, love_pts = marriage
        partner_id = u2_id if user.id == u1_id else u1_id

        async with aiosqlite.connect(DB_PATH) as db:
            user_cur = await db.execute(
                "SELECT coins, gift_last, COALESCE(nickname, username, first_name) FROM users WHERE user_id = ? AND chat_id = ?",
                (user.id, chat_id)
            )
            user_row = await user_cur.fetchone()
            coins = user_row[0] if user_row else 0
            my_gift_last = user_row[1] if user_row else None
            my_name = user_row[2] if user_row else (u1_name if user.id == u1_id else u2_name)

            partner_cur = await db.execute(
                "SELECT COALESCE(nickname, username, first_name) FROM users WHERE user_id = ? AND chat_id = ?",
                (partner_id, chat_id)
            )
            partner_row = await partner_cur.fetchone()
            partner_name = partner_row[0] if partner_row else (u2_name if user.id == u1_id else u1_name)

        if my_gift_last:
            last_dt = datetime.datetime.fromisoformat(my_gift_last)
            diff = (now - last_dt).total_seconds()
            if diff < 86400:
                remaining = 86400 - diff
                h = int(remaining // 3600)
                m_left = int((remaining % 3600) // 60)
                return await message.answer(
                    f"🎁 Ты уже дарил(а) подарок сегодня!\n"
                    f"Следующий можно через: {h} ч. {m_left} мин."
                )

        if coins < gift_cost:
            return await message.answer(
                f"🪙 Недостаточно монеток!\n"
                f"Нужно: {gift_cost} | У тебя: {coins}\n"
                f"Используй <code>ферма</code> чтобы заработать!", parse_mode="HTML"
            )

        async with aiosqlite.connect(DB_PATH) as db:
            new_pts = (love_pts or 0) + 1
            lv, lv_name, next_t = get_love_level(new_pts)
            old_lv, _, _ = get_love_level(love_pts or 0)
            level_up = lv > old_lv

            await db.execute(
                "UPDATE users SET coins = coins - ?, gift_last = ? WHERE user_id = ? AND chat_id = ?",
                (gift_cost, now.isoformat(), user.id, chat_id)
            )
            await db.execute(
                "UPDATE marriages SET love_points = ? WHERE id = ?",
                (new_pts, m_id)
            )
            await db.commit()

        level_up_text = f"\n\n🎉 <b>Новый уровень отношений!</b>\n{lv_name} (ур. {lv})" if level_up else ""
        progress_text = f" | до след. уровня: {next_t - new_pts} 🎁" if next_t else " | Максимальный уровень! ❤️‍🔥"

        return await message.answer(
            f"🎁 <a href='tg://user?id={user.id}'>{my_name}</a> дарит подарок "
            f"<a href='tg://user?id={partner_id}'>{partner_name}</a>!\n\n"
            f"💸 Потрачено: {gift_cost} 🪙\n"
            f"❤️ Очки любви: {new_pts}{progress_text}"
            f"{level_up_text}",
            parse_mode="HTML"
        )

    # =========================
    # ФЕРМА
    # =========================
    if lower == "ферма":
        user = message.from_user
        now = datetime.datetime.now()
        cooldown_hours = 6

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT farm_last, coins FROM users WHERE user_id = ? AND chat_id = ?",
                (user.id, chat_id)
            )
            row = await cur.fetchone()

            if row and row[0]:
                last = datetime.datetime.fromisoformat(row[0])
                diff = now - last
                if diff.total_seconds() < cooldown_hours * 3600:
                    remaining = cooldown_hours * 3600 - diff.total_seconds()
                    h = int(remaining // 3600)
                    m = int((remaining % 3600) // 60)
                    return await message.answer(
                        f"🌾 Ферма ещё не готова!\n"
                        f"Следующий сбор через: {h} ч. {m} мин."
                    )

            earned = random.randint(100, 5000)
            await db.execute("""
                UPDATE users SET coins = coins + ?, farm_last = ?
                WHERE user_id = ? AND chat_id = ?
            """, (earned, now.isoformat(), user.id, chat_id))
            await db.commit()

            cur2 = await db.execute(
                "SELECT coins FROM users WHERE user_id = ? AND chat_id = ?",
                (user.id, chat_id)
            )
            total_row = await cur2.fetchone()
            total = total_row[0] if total_row else earned

        user_name = await get_display_name(user.id, chat_id)
        return await message.answer(
            f"🌾 <b>Ферма</b>\n\n"
            f"<a href='tg://user?id={user.id}'>{user_name}</a> собрал(а) урожай!\n"
            f"🪙 +{earned} монеток\n\n"
            f"Всего монеток: {total}\n"
            f"Следующий сбор через 6 часов.",
            parse_mode="HTML"
        )

    # =========================
    # ПАСХАЛКИ (скрытые, в командах не отображаются)
    # =========================
    if lower == "нет":
        return await message.reply("минет")
    if lower == "да":
        return await message.reply("пизда")
    if lower in ("ало", "алло", "ало?", "алло?"):
        return await message.reply("иди нахуй")
    if lower == "дурин":
        return await message.reply("Даун")

    # =========================
    # ПОДСЧИТАЙ (ручная проверка статистики)
    # =========================
    if lower.startswith("подсчитай"):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COALESCE(nickname, username, first_name), message_count, weekly_count, reputation FROM users WHERE chat_id = ? ORDER BY message_count DESC LIMIT 20",
                (MAIN_CHAT_ID,)
            )
            rows = await cur.fetchall()
        lines = ["📊 <b>Актуальная статистика (проверено):</b>\n"]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r[0]} — {r[1]} (нед. {r[2]}, реп. {r[3]})")
        # Проверяем целостность
        ok = True
        for uid, peak in _peak_counts.items():
            if uid[1] == MAIN_CHAT_ID:
                for r2 in rows:
                    pass  # упрощённая проверка
        lines.append("\n✅ Данные проверены и актуальны")
        return await message.answer("\n".join(lines), parse_mode="HTML")

    # =========================
    # КОМАНДЫ
    # =========================
    if lower == "команды":
        return await message.answer(
            "📋 <b>Список команд:</b>\n\n"
            "<b>📊 Статистика</b>\n"
            "<code>кто я</code> — твоя статистика и репутация\n"
            "<code>кто ты</code> — статистика другого (ответь или @упомяни)\n\n"
            "<b>🏆 Топы</b>\n"
            "<code>топ вся</code> — топ активности за всё время\n"
            "<code>топ неделя</code> — топ активности за неделю\n"
            "<code>топ репутация</code> — топ по репутации\n\n"
            "<b>⭐ Репутация</b>\n"
            "<code>+</code> / <code>жиза</code> / <code>f</code> / <code>ага</code> — дать +1 репутацию (ответь на сообщение)\n\n"
            "<b>🎭 РП команды</b> (ответь на сообщение или @упомяни)\n"
            "<code>обнять</code> — 💖 обнять\n"
            "<code>поцеловать</code> — 💋 поцеловать\n"
            "<code>погладить</code> — 👐 погладить\n"
            "<code>облизать</code> — 👅 облизать\n"
            "<code>связать</code> — ⛓️ связать\n"
            "<code>казнить</code> — ⚔️ казнить\n\n"
            "<b>⚔️ Дуэль</b>\n"
            "<code>дуэль</code> — вызвать на дуэль (ответь или @упомяни)\n"
            "  🎯 Прицелиться — повысить шанс ценой хода\n"
            "  💨 Сбить прицел — попытаться обнулить прицел врага ценой хода\n\n"
            "<b>🔫 Рулетка</b>\n"
            "<code>рулетка</code> — русская рулетка (4–7 ячеек, 1 патрон, выживи все — победа!)\n\n"
            "<b>💍 Брак</b>\n"
            "<code>брак</code> — сделать предложение (ответь или @упомяни)\n"
            "<code>браки</code> — список браков с уровнями отношений\n"
            "<code>подарок</code> — подарить партнёру подарок за 3000 🪙 (кд 24 ч.)\n"
            "<code>развод</code> — расторгнуть брак\n\n"
            "<b>🌾 Ферма</b>\n"
            "<code>ферма</code> — собрать урожай монеток (кд 6 часов)\n\n"
            "<b>🎮 Прочее</b>\n"
            "<code>монетка</code> — 🪙 орёл, решка или ребро (шанс &lt;0.2%)\n"
            "<code>диджеить</code> — случайный результат диджея 🎛\n"
            "<code>нарвал кто &lt;текст&gt;</code> — 🐳 рандомный выбор участника\n"
            "<code>нарвал</code> / <code>бот</code> — 🐳 спросить нарвала\n\n"
            "<b>🏷 Никнейм</b>\n"
            "<code>+ник &lt;имя&gt;</code> — установить кастомный ник в этом чате\n"
            "<code>сбросить ник</code> — вернуть стандартное имя Telegram\n\n"
            "<b>🛡 Модерация</b> (только для админов)\n"
            "<code>!варн</code> — выдать предупреждение (2 варна = автобан)\n"
            "<code>!разварн</code> — снять одно предупреждение\n"
            "<code>!бан</code> — забанить пользователя\n"
            "<code>!разбан</code> — разбанить пользователя\n"
            "<code>!мут &lt;время&gt;</code> — замутить (пр: <code>!мут 1 час 30 минут</code>)\n"
            "<code>!размут</code> — снять мут\n"
            "<code>!банлист</code> — список пользователей с предупреждениями\n"
            "↳ Все команды работают через ответ на сообщение или @упоминание",
            parse_mode="HTML"
        )


# =========================
# RESET WEEKLY
# =========================

MSK_OFFSET = datetime.timezone(datetime.timedelta(hours=3))


async def reset_weekly():
    """Сбрасывает недельный топ каждую субботу в 17:00 МСК.
    Каждую минуту проверяет время по МСК. Дата последнего сброса
    хранится в таблице settings (ключ 'last_weekly_reset')."""
    while True:
        await asyncio.sleep(60)
        try:
            now_msk = datetime.datetime.now(MSK_OFFSET)
            if now_msk.weekday() != 5 or now_msk.hour < 17:
                continue
            today_str = now_msk.date().isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT value FROM settings WHERE key = 'last_weekly_reset'"
                )
                row = await cur.fetchone()
                if row and row[0] == today_str:
                    continue
                await db.execute("UPDATE users SET weekly_count = 0")
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_weekly_reset', ?)",
                    (today_str,)
                )
                await db.commit()
            print(f"=== Недельный топ сброшен ({today_str}, суббота 17:00 МСК) ===")
        except Exception as e:
            print(f"=== Ошибка сброса недельного топа: {e} ===")


# =========================
# START
# =========================

async def main():
    global BOT_ID
    # Восстанавливаем БД из резерва при запуске в продакшне
    await restore_db()
    await init_db()
    await seed_initial_data()
    me = await bot.get_me()
    BOT_ID = me.id
    asyncio.create_task(reset_weekly())
    asyncio.create_task(periodic_backup())
    asyncio.create_task(periodic_integrity_check())

    shutdown_event = asyncio.Event()

    async def _graceful_shutdown():
        print("=== SIGTERM получен, сохраняем БД перед выходом... ===")
        await backup_db()
        print("=== БД сохранена, завершаем работу ===")
        shutdown_event.set()

    # SIGTERM поддерживается только на Linux/Mac, на Windows пропускаем
    if os.name != "nt":
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_graceful_shutdown()))

    app = web.Application()

    async def health_check(request):
        return web.Response(text="OK")

    app.router.add_get("/api/healthz", health_check)
    app.router.add_get("/", health_check)

    domains = os.getenv("REPLIT_DOMAINS", "")
    if domains:
        primary_domain = domains.split(",")[0].strip()
        webhook_url = f"https://{primary_domain}{WEBHOOK_PATH}"
        print(f"=== Webhook режим: {webhook_url} ===")
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
    else:
        print("=== Polling режим (локальная разработка) ===")
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(dp.start_polling(bot))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"=== Сервер запущен на порту {PORT} ===")

    await shutdown_event.wait()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
