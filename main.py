import os
import re
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta

from pyrogram import Client, filters
from pyrogram.types import Message, BotCommand, MenuButtonCommands
from pyrogram.enums import ParseMode

# ==== Конфиг ====
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["SESSION_STRING"]
OWNER_ID = int(os.environ["OWNER_ID"])
BOT_TOKEN = os.environ["BOT_TOKEN"]
LEADER_CHAT_ID = int(os.environ["LEADER_CHAT_ID"])

ANICARD_USERNAME = "anicardplaybot"
DB_PATH = "anicard.db"
INTERVAL_SECONDS = 15 * 60
MSK = timezone(timedelta(hours=3))

MAX_MESSAGE_LEN = 3800  # запас до лимита Telegram 4096

self_request = False

# цепочка сообщений дня
chain_message_ids = []      # [msg_id_1, msg_id_2, ...]
current_chain_text = ""     # буфер последнего сообщения
current_chain_msg_id = None # id последнего сообщения
last_chain_date = None      # дата цепочки

userbot: Client = None
bot: Client = None


def now_msk() -> datetime:
    return datetime.now(MSK)


def now_msk_str() -> str:
    return now_msk().strftime("%d.%m %H:%M")


def today_msk_date():
    return now_msk().date()


# ==== БД ====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            place INTEGER NOT NULL,
            clan_name TEXT NOT NULL,
            points INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ts ON snapshots(ts)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clan_history (
            clan_name TEXT PRIMARY KEY,
            last_points INTEGER NOT NULL,
            last_seen TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_snapshot(clans):
    ts = now_msk().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO snapshots (ts, place, clan_name, points) VALUES (?, ?, ?, ?)",
        [(ts, p, n, pts) for p, n, pts in clans],
    )
    cur.executemany("""
        INSERT INTO clan_history (clan_name, last_points, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(clan_name) DO UPDATE SET
            last_points=excluded.last_points,
            last_seen=excluded.last_seen
    """, [(n, pts, ts) for _, n, pts in clans])
    conn.commit()
    conn.close()


def get_last_snapshot():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MAX(ts) FROM snapshots")
    last_ts = cur.fetchone()[0]
    if not last_ts:
        conn.close()
        return {}
    cur.execute("SELECT clan_name, place, points FROM snapshots WHERE ts = ?", (last_ts,))
    rows = cur.fetchall()
    conn.close()
    return {name: (place, pts) for name, place, pts in rows}


def get_history():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT clan_name, last_points, last_seen FROM clan_history")
    rows = cur.fetchall()
    conn.close()
    return {name: (pts, seen) for name, pts, seen in rows}


def get_current_top():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MAX(ts) FROM snapshots")
    last_ts = cur.fetchone()[0]
    if not last_ts:
        conn.close()
        return []
    cur.execute(
        "SELECT place, clan_name, points FROM snapshots WHERE ts = ? ORDER BY place",
        (last_ts,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_today_top():
    today_start = now_msk().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT clan_name, points, ts FROM snapshots WHERE ts >= ? ORDER BY ts",
        (today_start,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return []
    first = {}
    last = {}
    for name, pts, ts in rows:
        if name not in first:
            first[name] = pts
        last[name] = pts
    deltas = [(name, last[name] - first[name]) for name in last]
    deltas.sort(key=lambda x: x[1], reverse=True)
    return deltas


def get_clan_history(clan_name: str):
    today_start = now_msk().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT ts, place, points FROM snapshots WHERE clan_name = ? AND ts >= ? ORDER BY ts",
        (clan_name, today_start),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_delta_last_hour():
    one_hour_ago = (now_msk() - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT clan_name, points, ts FROM snapshots WHERE ts >= ? ORDER BY ts",
        (one_hour_ago,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return []
    first = {}
    last = {}
    for name, pts, ts in rows:
        if name not in first:
            first[name] = pts
        last[name] = pts
    deltas = [(name, last[name] - first[name], last[name]) for name in last]
    deltas.sort(key=lambda x: x[1], reverse=True)
    return deltas


# ==== Парсинг ====
CLAN_LINE_RE = re.compile(r"^(\d+)\.\s+(.+?)\s+-\s+(\d+)\s+🔹", re.MULTILINE)


def parse_top(text):
    return [
        (int(m.group(1)), m.group(2).strip(), int(m.group(3)))
        for m in CLAN_LINE_RE.finditer(text)
    ]


# ==== Формат секции ====
def format_snapshot_section(ts, deltas):
    dt = datetime.fromisoformat(ts).astimezone(MSK).strftime("%H:%M")
    lines = [f"📊 <b>[{dt} МСК]</b>"]
    for place, name, kind, d, pts in deltas:
        prefix = f"#{place}"
        if kind == "delta":
            sign = "+" if d > 0 else ""
            lines.append(f"  📈 {prefix} {name}: {sign}{d} ({pts})")
        elif kind == "return":
            sign = "+" if d and d > 0 else ""
            lines.append(f"  🔄 {prefix} {name}: вернулся, {sign}{d} ({pts})")
        else:
            lines.append(f"  🆕 {prefix} {name}: {pts}")
    return "\n".join(lines)


# ==== Цепочка сообщений ====
async def append_to_chain(section: str):
    global current_chain_text, current_chain_msg_id, last_chain_date

    today = today_msk_date()
    if last_chain_date is not None and last_chain_date != today:
        await start_new_chain_day()
    if last_chain_date is None:
        last_chain_date = today

    # первое сообщение дня
    if current_chain_msg_id is None:
        header = f"🗓 <b>История Anicard — {today.strftime('%d.%m.%Y')}</b>\n\n"
        current_chain_text = header + section
        try:
            msg = await bot.send_message(
                LEADER_CHAT_ID,
                current_chain_text,
                parse_mode=ParseMode.HTML,
                protect_content=True,
                disable_notification=True,
            )
            current_chain_msg_id = msg.id
            chain_message_ids.append(msg.id)
            await bot.pin_chat_message(LEADER_CHAT_ID, msg.id, disable_notification=True)
        except Exception as e:
            print(f"Не удалось создать сообщение цепочки: {e}")
        return

    new_text = current_chain_text + "\n\n" + section
    if len(new_text) > MAX_MESSAGE_LEN:
        # создаём новое сообщение и закрепляем
        try:
            msg = await bot.send_message(
                LEADER_CHAT_ID,
                section,
                parse_mode=ParseMode.HTML,
                protect_content=True,
                disable_notification=True,
            )
            current_chain_msg_id = msg.id
            current_chain_text = section
            chain_message_ids.append(msg.id)
            await bot.pin_chat_message(LEADER_CHAT_ID, msg.id, disable_notification=True)
        except Exception as e:
            print(f"Не удалось создать продолжение цепочки: {e}")
    else:
        current_chain_text = new_text
        try:
            await bot.edit_message_text(
                chat_id=LEADER_CHAT_ID,
                message_id=current_chain_msg_id,
                text=current_chain_text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            print(f"Не удалось отредактировать цепочку: {e}")


async def start_new_chain_day():
    """Открепляет все сообщения цепочки. НЕ удаляет их."""
    global chain_message_ids, current_chain_text, current_chain_msg_id, last_chain_date

    for msg_id in chain_message_ids:
        try:
            await bot.unpin_chat_message(LEADER_CHAT_ID, msg_id)
        except Exception as e:
            print(f"Не удалось открепить {msg_id}: {e}")

    chain_message_ids = []
    current_chain_text = ""
    current_chain_msg_id = None
    last_chain_date = today_msk_date()


# ==== Обработка снапшота ====
async def process_snapshot(text, source="плановый"):
    clans = parse_top(text)
    if not clans:
        await userbot.send_message(
            OWNER_ID, f"⚠️ [{now_msk_str()} МСК] Не удалось распарсить топ ({source})"
        )
        return

    print(f"[{now_msk_str()} МСК] Снапшот ({source}): {len(clans)} кланов")

    prev = get_last_snapshot()
    history = get_history()

    deltas = []
    for place, name, pts in clans:
        if name in prev:
            d = pts - prev[name][1]
            if d != 0:
                deltas.append((place, name, "delta", d, pts))
        elif name in history:
            d = pts - history[name][0]
            deltas.append((place, name, "return", d, pts))
        else:
            deltas.append((place, name, "new", None, pts))

    save_snapshot(clans)

    if deltas:
        header = f"📊 Обновление топа [{now_msk_str()} МСК]"
        if source == "ручной":
            header += " 🔄 (ручной)"
        lines = [header + ":"]
        for place, name, kind, d, pts in deltas:
            prefix = f"#{place}"
            if kind == "delta":
                sign = "+" if d > 0 else ""
                lines.append(f"📈 {prefix} {name}: {sign}{d} ({pts})")
            elif kind == "return":
                sign = "+" if d and d > 0 else ""
                lines.append(f"🔄 {prefix} {name}: вернулся, {sign}{d} ({pts})")
            else:
                lines.append(f"🆕 {prefix} {name}: {pts}")
        await userbot.send_message(OWNER_ID, "\n".join(lines))

        section = format_snapshot_section(now_msk().isoformat(), deltas)
        await append_to_chain(section)


async def fetch_top_and_process(source="плановый"):
    global self_request
    self_request = True
    try:
        await userbot.send_message(ANICARD_USERNAME, "🏆 Топ кланов")
    except Exception as e:
        self_request = False
        raise e

    top_text = None
    for _ in range(15):
        await asyncio.sleep(1)
        async for msg in userbot.get_chat_history(ANICARD_USERNAME, limit=10):
            if msg.text and "Топ кланов по очкам" in msg.text:
                top_text = msg.text
                break
        if top_text:
            break

    await asyncio.sleep(10)
    self_request = False

    if not top_text:
        raise RuntimeError("Не найдено сообщение с топом кланов")

    await process_snapshot(top_text, source=source)


async def setup_userbot_listener():
    @userbot.on_message(filters.private & filters.user(ANICARD_USERNAME))
    async def on_anicard_message(client, message: Message):
        if self_request:
            return
        if not message.text:
            return
        if "Топ кланов по очкам" not in message.text:
            return
        try:
            await process_snapshot(message.text, source="ручной")
        except Exception as e:
            await userbot.send_message(
                OWNER_ID, f"⚠️ Ошибка ручного вызова [{now_msk_str()} МСК]: {e}"
            )


# ==== Бот: команды ====
async def setup_bot_handlers():
    def only_our_chat(_, __, message: Message):
        return message.chat.id == LEADER_CHAT_ID

    @bot.on_message(filters.command("top", prefixes="/") & filters.create(only_our_chat))
    async def cmd_top(client, message: Message):
        top = get_current_top()
        if not top:
            await message.reply_text("Пока нет данных.")
            return
        lines = [f"🏆 <b>Топ сезона</b> [{now_msk_str()} МСК]:"]
        for place, name, pts in top[:10]:
            lines.append(f"#{place} {name} — {pts}")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, protect_content=True)

    @bot.on_message(filters.command("delta", prefixes="/") & filters.create(only_our_chat))
    async def cmd_delta(client, message: Message):
        deltas = get_delta_last_hour()
        if not deltas:
            await message.reply_text("За последний час изменений нет.")
            return
        lines = [f"📊 <b>Изменения за час</b> [{now_msk_str()} МСК]:"]
        for name, d, pts in deltas:
            sign = "+" if d > 0 else ""
            lines.append(f"{name}: {sign}{d} ({pts})")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, protect_content=True)

    @bot.on_message(filters.command(["day", "топдня"], prefixes="/") & filters.create(only_our_chat))
    async def cmd_day(client, message: Message):
        today = get_today_top()
        if not today:
            await message.reply_text("Сегодня ещё нет данных.")
            return
        lines = [f"📈 <b>Топ дня</b> [{now_msk_str()} МСК]:"]
        for i, (name, d) in enumerate(today[:15], 1):
            sign = "+" if d >= 0 else ""
            lines.append(f"{i}. {name} — {sign}{d}")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, protect_content=True)

    @bot.on_message(filters.command(["history", "история"], prefixes="/") & filters.create(only_our_chat))
    async def cmd_history(client, message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply_text("Использование: /history <название клана>")
            return
        clan_query = parts[1].strip()
        history_all = get_history()
        matches = [n for n in history_all if clan_query.lower() in n.lower()]
        if not matches:
            await message.reply_text(f"Клан «{clan_query}» не найден в истории.")
            return
        if len(matches) > 1:
            await message.reply_text("Найдено несколько кланов:\n" + "\n".join(matches[:10]))
            return

        clan_name = matches[0]
        rows = get_clan_history(clan_name)
        if not rows:
            await message.reply_text(f"За сегодня данных по «{clan_name}» нет.")
            return

        lines = [f"📜 <b>История «{clan_name}» за сегодня:</b>"]
        first_pts = rows[0][2]
        last_pts = rows[-1][2]
        lines.append(f"Первое значение: {first_pts}")
        lines.append(f"Последнее значение: {last_pts}")
        lines.append(f"Набрано за день: {last_pts - first_pts:+d}")
        lines.append("")
        lines.append("Изменения по снапшотам:")
        prev_pts = None
        for ts, place, pts in rows:
            dt = datetime.fromisoformat(ts).astimezone(MSK).strftime("%H:%M")
            if prev_pts is None:
                lines.append(f"  {dt} — #{place}, {pts}")
            else:
                diff = pts - prev_pts
                if diff != 0:
                    lines.append(f"  {dt} — #{place}, {pts} ({diff:+d})")
            prev_pts = pts

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3900] + "\n…(сообщение обрезано)"
        await message.reply_text(text, parse_mode=ParseMode.HTML, protect_content=True)


# ==== 00:00 МСК ====
async def daily_report_loop():
    while True:
        now = now_msk()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (next_midnight - now).total_seconds()
        print(f"До итогового отчёта: {wait_seconds:.0f} сек")
        await asyncio.sleep(wait_seconds)

        try:
            today = get_today_top()
            top = get_current_top()

            lines = [f"🌙 <b>Итог дня [{now_msk_str()} МСК]</b>"]
            if today:
                lines.append("\n📈 <b>Топ дня:</b>")
                for i, (name, d) in enumerate(today[:15], 1):
                    sign = "+" if d >= 0 else ""
                    lines.append(f"  {i}. {name} — {sign}{d}")
            else:
                lines.append("Нет данных за день.")

            lines.append("\n🏆 <b>Топ сезона:</b>")
            for place, name, pts in top[:10]:
                lines.append(f"  #{place} {name} — {pts}")

            await bot.send_message(
                LEADER_CHAT_ID, "\n".join(lines), parse_mode=ParseMode.HTML, protect_content=True
            )

            await start_new_chain_day()

        except Exception as e:
            await bot.send_message(LEADER_CHAT_ID, f"⚠️ Ошибка итогового отчёта: {e}")


# ==== Запуск ====
async def main():
    global userbot, bot
    init_db()

    userbot = Client(
        name="anicard_userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION,
        in_memory=True,
    )
    bot = Client(
        name="anicard_leader_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

    await userbot.start()
    await bot.start()

    # команды и меню
    await bot.set_bot_commands([
        BotCommand("top", "🏆 Топ кланов сезона"),
        BotCommand("delta", "📊 Изменения за последний час"),
        BotCommand("day", "📈 Топ кланов за сегодня"),
        BotCommand("history", "📜 История конкретного клана"),
    ])
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        print(f"Не удалось установить кнопку меню: {e}")

    await userbot.send_message(OWNER_ID, f"🟢 Userbot Anicard запущен [{now_msk_str()} МСК]")
    await setup_userbot_listener()
    await setup_bot_handlers()

    try:
        await fetch_top_and_process(source="стартовый")
    except Exception as e:
        await userbot.send_message(
            OWNER_ID, f"⚠️ Ошибка стартового парса [{now_msk_str()} МСК]: {e}"
        )

    asyncio.create_task(daily_report_loop())

    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try:
            await fetch_top_and_process(source="плановый")
        except Exception as e:
            await userbot.send_message(OWNER_ID, f"⚠️ Ошибка [{now_msk_str()} МСК]: {e}")


if __name__ == "__main__":
    asyncio.run(main())
