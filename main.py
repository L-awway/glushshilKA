import os
import re
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta

from pyrogram import Client, filters
from pyrogram.types import Message, BotCommand, MenuButtonCommands, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

# ==== Конфиг ====
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["SESSION_STRING"]
OWNER_ID = int(os.environ["OWNER_ID"])
BOT_TOKEN = os.environ["BOT_TOKEN"]
LEADER_CHAT_ID = int(os.environ["LEADER_CHAT_ID"])
NEWS_CHANNEL_ID = int(os.environ["NEWS_CHANNEL_ID"])

ANICARD_USERNAME = "anicardplaybot"
DB_PATH = "anicard.db"
INTERVAL_SECONDS = 15 * 60
MSK = timezone(timedelta(hours=3))

MAX_MESSAGE_LEN = 3800

self_request = False
chain_message_ids = []
current_chain_sections = []
current_chain_msg_id = None
last_chain_date = None
pending_raids = {}

season_start_date = datetime.now(MSK).date()

userbot: Client = None
bot: Client = None

def now_msk() -> datetime:
    return datetime.now(MSK)

def now_msk_str() -> str:
    return now_msk().strftime("%d.%m %H:%M")

def today_msk_date():
    return now_msk().date()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, place INTEGER NOT NULL, clan_name TEXT NOT NULL, points INTEGER NOT NULL)""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ts ON snapshots(ts)")
    cur.execute("""CREATE TABLE IF NOT EXISTS clan_history (clan_name TEXT PRIMARY KEY, last_points INTEGER NOT NULL, last_seen TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS raids (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, clan_name TEXT NOT NULL, before_pts INTEGER NOT NULL, during_pts INTEGER NOT NULL, after_pts INTEGER NOT NULL)""")
    conn.commit()
    conn.close()

def save_snapshot(clans):
    ts = now_msk().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany("INSERT INTO snapshots (ts, place, clan_name, points) VALUES (?, ?, ?, ?)", [(ts, p, n, pts) for p, n, pts in clans])
    cur.executemany("""
        INSERT INTO clan_history (clan_name, last_points, last_seen) VALUES (?, ?, ?)
        ON CONFLICT(clan_name) DO UPDATE SET last_points=excluded.last_points, last_seen=excluded.last_seen
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

def get_snapshot_at_time(ts_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT clan_name, points FROM snapshots WHERE ts = ?", (ts_str,))
    rows = cur.fetchall()
    conn.close()
    return dict(rows)

def get_ts_before(limit_ts, minutes=10):
    target = datetime.fromisoformat(limit_ts) - timedelta(minutes=minutes)
    target_str = target.isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT ts FROM snapshots 
        WHERE ts < ? 
        ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) ASC 
        LIMIT 1
    """, (limit_ts, target_str))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

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
    cur.execute("SELECT place, clan_name, points FROM snapshots WHERE ts = ? ORDER BY place", (last_ts,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_season_top():
    if now_msk().day == 5:
        top = get_current_top()
        return top

    season_start_iso = f"{season_start_date.isoformat()}T00:00:00"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT ts FROM snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (season_start_iso,))
    first_row = cur.fetchone()
    if not first_row:
        conn.close()
        return []
    first_ts = first_row[0]
    cur.execute("SELECT clan_name, points FROM snapshots WHERE ts = ?", (first_ts,))
    start_pts = dict(cur.fetchall())
    cur.execute("SELECT place, clan_name, points FROM snapshots WHERE ts = (SELECT MAX(ts) FROM snapshots) ORDER BY place")
    top_end = cur.fetchall()
    conn.close()

    season_top = []
    for place, name, pts in top_end:
        start = start_pts.get(name, 0)
        diff = pts - start
        if diff < 0: diff = pts
        season_top.append((place, name, diff))
        
    season_top.sort(key=lambda x: x[2], reverse=True)
    return [(i+1, name, pts) for i, (_, name, pts) in enumerate(season_top)]

def get_today_top():
    if now_msk().day == 5:
        top = get_current_top()
        return [(name, pts) for place, name, pts in top]

    today_start = now_msk().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT ts FROM snapshots WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (today_start,))
    first_row = cur.fetchone()
    if not first_row:
        conn.close()
        return []
    cur.execute("SELECT clan_name, points FROM snapshots WHERE ts = ?", (first_row[0],))
    start_pts = dict(cur.fetchall())
    cur.execute("SELECT clan_name, points FROM snapshots WHERE ts = (SELECT MAX(ts) FROM snapshots WHERE ts >= ?)", (today_start,))
    end_pts = dict(cur.fetchall())
    conn.close()

    deltas = []
    for name, current in end_pts.items():
        start = start_pts.get(name, 0)
        diff = current - start
        if diff < 0: diff = current
        deltas.append((name, diff))
        
    deltas.sort(key=lambda x: x[1], reverse=True)
    return deltas

def get_clan_history(clan_name: str):
    today_start = now_msk().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT ts, place, points FROM snapshots WHERE clan_name = ? AND ts >= ? ORDER BY ts", (clan_name, today_start))
    rows = cur.fetchall()
    conn.close()
    return rows

CLAN_LINE_RE = re.compile(r"^(\d+)\.\s+(.+?)\s+-\s+(\d+)\s+🔹", re.MULTILINE)

def parse_top(text):
    return [(int(m.group(1)), m.group(2).strip(), int(m.group(3))) for m in CLAN_LINE_RE.finditer(text)]

def get_analytics_day(date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    start_dt = f"{date_str}T00:00:00"
    end_dt = f"{date_str}T23:59:59"
    
    is_season_start = (date_str.endswith("-05"))
    
    cur.execute("""
        SELECT place, clan_name, points 
        FROM snapshots 
        WHERE ts = (SELECT MAX(ts) FROM snapshots WHERE ts >= ? AND ts <= ?) 
        ORDER BY place
    """, (start_dt, end_dt))
    top_end = cur.fetchall()
    
    cur.execute("""
        SELECT clan_name, points 
        FROM snapshots 
        WHERE ts = (SELECT MIN(ts) FROM snapshots WHERE ts >= ? AND ts <= ?)
    """, (start_dt, end_dt))
    top_start = dict(cur.fetchall())
    
    cur.execute("SELECT ts, clan_name, before_pts, during_pts, after_pts FROM raids WHERE ts >= ? AND ts <= ? ORDER BY ts", (start_dt, end_dt))
    raids = cur.fetchall()
    conn.close()

    lines = [f"📊 <b>Аналитика за {date_str}</b>\n", "<b>Топ-10 на конец дня (ср. за день):</b>"]
    
    for place, name, pts in top_end:
        start_pts = top_start.get(name, 0)
        
        if is_season_start:
            day_delta = pts
        else:
            day_delta = pts - start_pts
            if day_delta < 0:
                day_delta = 0
            
        avg = day_delta / 35 if day_delta > 0 else 0
        lines.append(f"#{place} {name} — {pts} ({day_delta:+d} | ср: {avg:.1f})")

    lines.append("\n⚔️ <b>Рейды (скачки > 40):</b>")
    if not raids: 
        lines.append("Не зафиксировано.")
    else:
        for ts, name, b, d, a in raids:
            time_str = datetime.fromisoformat(ts).astimezone(MSK).strftime("%H:%M")
            lines.append(f"[{time_str}] ({b}; {d}; {a}) — {name}")
            
    return "\n".join(lines)[:4000]

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

def format_header():
    lines = []
    top = get_season_top()
    if top:
        lines.append(f"🏆 <b>Топ сезона (с {season_start_date.strftime('%d.%m')}):</b>")
        for place, name, pts in top[:10]: lines.append(f"  #{place} {name} — {pts}")
    today = get_today_top()
    if today:
        lines.append("\n📈 <b>Топ дня (с 00:00):</b>")
        for i, (name, d) in enumerate(today[:10], 1):
            sign = "+" if d >= 0 else ""
            lines.append(f"  {i}. {name} — {sign}{d}")
    return "\n".join(lines)

def build_chain_text(date_str, sections, is_continuation=False):
    suffix = " (продолжение)" if is_continuation else ""
    header_date = f"🗓 <b>История Anicard — {date_str}{suffix}</b>"
    return f"{header_date}\n\n{format_header()}\n\n" + "\n\n".join(sections)

async def append_to_chain(section: str):
    global current_chain_sections, current_chain_msg_id, last_chain_date
    today = today_msk_date()
    if last_chain_date is not None and last_chain_date != today: await start_new_chain_day()
    if last_chain_date is None: last_chain_date = today
    date_str = today.strftime("%d.%m.%Y")

    if current_chain_msg_id is None:
        current_chain_sections = [section]
        text = build_chain_text(date_str, current_chain_sections)
        
        try:
            msg = await bot.send_message(LEADER_CHAT_ID, text, parse_mode=ParseMode.HTML, protect_content=True, disable_notification=True)
            current_chain_msg_id = msg.id
            chain_message_ids.append(msg.id)
            await bot.pin_chat_message(LEADER_CHAT_ID, msg.id, disable_notification=True)
        except Exception as e:
            print(f"Ошибка чата: {e}")

        try:
            await bot.send_message(NEWS_CHANNEL_ID, text, parse_mode=ParseMode.HTML, protect_content=True, disable_notification=True)
        except Exception as e:
            try:
                await userbot.send_message(OWNER_ID, f"⚠️ Ошибка отправки в КАНАЛ: {e}")
            except: pass
        return

    candidate_sections = current_chain_sections + [section]
    candidate_text = build_chain_text(date_str, candidate_sections)

    if len(candidate_text) > MAX_MESSAGE_LEN:
        current_chain_sections = [section]
        text = build_chain_text(date_str, current_chain_sections, is_continuation=True)
        
        try:
            msg = await bot.send_message(LEADER_CHAT_ID, text, parse_mode=ParseMode.HTML, protect_content=True, disable_notification=True)
            current_chain_msg_id = msg.id
            chain_message_ids.append(msg.id)
            await bot.pin_chat_message(LEADER_CHAT_ID, msg.id, disable_notification=True)
        except Exception as e:
            print(f"Ошибка чата: {e}")

        try:
            await bot.send_message(NEWS_CHANNEL_ID, text, parse_mode=ParseMode.HTML, protect_content=True, disable_notification=True)
        except Exception as e:
            try:
                await userbot.send_message(OWNER_ID, f"⚠️ Ошибка канала: {e}")
            except: pass
    else:
        current_chain_sections = candidate_sections
        try:
            await bot.edit_message_text(chat_id=LEADER_CHAT_ID, message_id=current_chain_msg_id, text=candidate_text, parse_mode=ParseMode.HTML)
        except Exception: pass

async def start_new_chain_day():
    global chain_message_ids, current_chain_sections, current_chain_msg_id, last_chain_date
    for msg_id in chain_message_ids:
        try: await bot.unpin_chat_message(LEADER_CHAT_ID, msg_id)
        except Exception: pass
    chain_message_ids = []
    current_chain_sections = []
    current_chain_msg_id = None
    last_chain_date = today_msk_date()

async def process_snapshot(text, source="плановый"):
    global pending_raids
    clans = parse_top(text)
    if not clans: return

    prev = get_last_snapshot()
    history = get_history()
    deltas = []
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for clan in list(pending_raids.keys()):
        raid_info = pending_raids[clan]
        curr_pts = next((pts for _, n, pts in clans if n == clan), None)
        
        if curr_pts is not None:
            after_delta = curr_pts - raid_info["pts_at_raid"]
            cur.execute("UPDATE raids SET after_pts = ? WHERE id = ?", (after_delta, raid_info["id"]))
        else:
            cur.execute("UPDATE raids SET after_pts = 0 WHERE id = ?", (raid_info["id"],))
        del pending_raids[clan]

    for place, name, pts in clans:
        if name in prev:
            d = pts - prev[name][1]
            if d != 0:
                deltas.append((place, name, "delta", d, pts))
                
                if d >= 40:
                    ts_prev = get_ts_before(now_msk().isoformat(), minutes=15)
                    before = 0
                    if ts_prev:
                        prev_snap = get_snapshot_at_time(ts_prev)
                        if name in prev_snap and name in prev:
                            before = prev[name][1] - prev_snap[name]
                            if before < -1000: before = 0

                    cur.execute(
                        "INSERT INTO raids (ts, clan_name, before_pts, during_pts, after_pts) VALUES (?, ?, ?, ?, ?)",
                        (now_msk().isoformat(), name, before, d, 0)
                    )
                    raid_id = cur.lastrowid
                    pending_raids[name] = {
                        "id": raid_id,
                        "pts_at_raid": pts
                    }
        elif name in history:
            d = pts - history[name][0]
            deltas.append((place, name, "return", d, pts))
        else:
            deltas.append((place, name, "new", None, pts))

    conn.commit()
    conn.close()

    save_snapshot(clans)

    if deltas:
        header = f"📊 Обновление топа [{now_msk_str()} МСК]"
        if source == "ручной": header += " 🔄 (ручной)"
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
    try: await userbot.send_message(ANICARD_USERNAME, "🏆 Топ кланов")
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
        if top_text: break

    await asyncio.sleep(10)
    self_request = False
    if top_text: await process_snapshot(top_text, source=source)

async def setup_userbot_listener():
    @userbot.on_message(filters.private & filters.user(ANICARD_USERNAME))
    async def on_anicard_message(client, message: Message):
        if self_request: return
        if message.text and "Топ кланов по очкам" in message.text:
            await process_snapshot(message.text, source="ручной")

def get_report_keyboard(current_date_str):
    current_dt = datetime.strptime(current_date_str, "%Y-%m-%d").date()
    today = today_msk_date()
    buttons = []
    row1 = [InlineKeyboardButton("◀️ Раньше", callback_data=f"rep_shift_{(current_dt - timedelta(days=1)).isoformat()}")]
    if current_dt != today: row1.append(InlineKeyboardButton("📅 Сегодня", callback_data=f"rep_set_{today.isoformat()}"))
    if current_dt < today: row1.append(InlineKeyboardButton("Позже ▶️", callback_data=f"rep_shift_{(current_dt + timedelta(days=1)).isoformat()}"))
    buttons.append(row1)
    row2 = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        text = f"[{d.day:02d}.{d.month:02d}]" if d == current_dt else f"{d.day:02d}.{d.month:02d}"
        row2.append(InlineKeyboardButton(text, callback_data=f"rep_set_{d.isoformat()}"))
    buttons.append(row2)
    return InlineKeyboardMarkup(buttons)

async def setup_bot_handlers():
    def only_our_chat(_, __, message: Message): return message.chat.id == LEADER_CHAT_ID

    @bot.on_message(filters.command(["help", "info", "инструкция"], prefixes="/") & filters.create(only_our_chat))
    async def cmd_help(client, message: Message):
        text = (
            "📖 <b>Инструкция по боту</b>\n\n"
            "<b>Что за числа в рейдах?</b>\n"
            "Формат: <code>[Время] (До; Рейд; После) — Клан</code>\n"
            "• <b>До</b> — сколько очков клан набрал за 10-15 минут <i>до</i> этого рейда (разогрев).\n"
            "• <b>Рейд</b> — сколько очков набрали за эти 10 минут (основной скачок).\n"
            "• <b>После</b> — сколько очков добрали за следующие 10 минут (продолжение).\n\n"
            "Пример: <code>[11:03] (0; 47; 70)</code> означает, что в 11:03 они влупили 47 очков (а до этого было 0), и еще 70 очков набрали в следующие 10 минут.\n\n"
            "⚔️ <b>Команды:</b>\n"
            "/report — показать аналитику и рейды за сегодня (можно ввести дату: <code>/report 15.10</code>).\n"
            "/top — текущий топ сезона.\n"
            "/day — топ за сегодня.\n"
            "/history &lt;клан&gt; — история конкретного клана за сегодня.\n"
            "/check — <b>Срочный опрос!</b> Заставить бота прямо сейчас проверить Anicard и выдать свежие изменения (не дожидаясь 10 минут)."
        )
        await message.reply_text(text, parse_mode=ParseMode.HTML)

    @bot.on_message(filters.command(["check", "проверить", "срочно"], prefixes="/") & filters.create(only_our_chat))
    async def cmd_check(client, message: Message):
        wait_msg = await message.reply_text("⏳ Запрашиваю данные у Anicard...")
        try:
            await fetch_top_and_process(source="ручной")
            await wait_msg.edit_text("✅ Данные обновлены! Свежие изменения отправлены в чат и в личку.")
        except Exception as e:
            await wait_msg.edit_text(f"❌ Ошибка при ручном запросе: {e}")

    @bot.on_message(filters.command(["report", "day", "топдня"], prefixes="/") & filters.create(only_our_chat))
    async def cmd_report(client, message: Message):
        parts = message.text.split()
        if len(parts) > 1:
            date_input = parts[1].strip()
            try:
                if "." in date_input and len(date_input.split(".")[0]) <= 2:
                    d, m = date_input.split(".")
                    date_str = f"{now_msk().year}-{int(m):02d}-{int(d):02d}"
                else:
                    date_str = date_input
                    datetime.fromisoformat(date_str)
            except Exception:
                await message.reply_text("❌ Неверный формат даты. Используй: /report 15.10")
                return
        else:
            date_str = today_msk_date().isoformat()

        text = get_analytics_day(date_str)
        keyboard = get_report_keyboard(date_str)
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @bot.on_callback_query(filters.create(lambda _, __, q: q.message.chat.id == LEADER_CHAT_ID))
    async def on_report_button(client, callback_query):
        data = callback_query.data
        if data.startswith("rep_set_"): date_str = data.replace("rep_set_", "")
        elif data.startswith("rep_shift_"): date_str = data.replace("rep_shift_", "")
        else:
            await callback_query.answer()
            return

        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if target_date > today_msk_date():
            target_date = today_msk_date()
            date_str = target_date.isoformat()

        text = get_analytics_day(date_str)
        keyboard = get_report_keyboard(date_str)
        try:
            await callback_query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception: pass
        await callback_query.answer()

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
            await message.reply_text(f"Клан «{clan_query}» не найден.")
            return
        if len(matches) > 1:
            await message.reply_text("Найдено несколько:\n" + "\n".join(matches[:10]))
            return

        clan_name = matches[0]
        rows = get_clan_history(clan_name)
        if not rows:
            await message.reply_text(f"За сегодня данных по «{clan_name}» нет.")
            return

        lines = [f"📜 <b>История «{clan_name}» за сегодня:</b>"]
        first_pts, last_pts = rows[0][2], rows[-1][2]
        lines.append(f"Первое: {first_pts} | Последнее: {last_pts} | Набрано: {last_pts - first_pts:+d}\n")
        prev_pts = None
        for ts, place, pts in rows:
            dt = datetime.fromisoformat(ts).astimezone(MSK).strftime("%H:%M")
            diff = pts - prev_pts if prev_pts is not None else 0
            if prev_pts is None or diff != 0:
                lines.append(f"  {dt} — #{place}, {pts}" + (f" ({diff:+d})" if diff else ""))
            prev_pts = pts

        text = "\n".join(lines)
        if len(text) > 4000: text = text[:3900] + "\n…(обрезано)"
        await message.reply_text(text, parse_mode=ParseMode.HTML, protect_content=True)

async def daily_report_loop():
    while True:
        now = now_msk()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_midnight - now).total_seconds())
        try:
            today, top = get_today_top(), get_current_top()
            lines = [f"🌙 <b>Итог дня [{now_msk_str()} МСК]</b>"]
            if today:
                lines.append("\n📈 <b>Топ дня:</b>")
                for i, (name, d) in enumerate(today[:15], 1):
                    lines.append(f"  {i}. {name} — {'+' if d >= 0 else ''}{d}")
            lines.append("\n🏆 <b>Топ сезона:</b>")
            for place, name, pts in top[:10]: lines.append(f"  #{place} {name} — {pts}")
            await bot.send_message(LEADER_CHAT_ID, "\n".join(lines), parse_mode=ParseMode.HTML)
            await start_new_chain_day()
        except Exception: pass

async def main():
    global userbot, bot
    init_db()
    userbot = Client("anicard_userbot", api_id=API_ID, api_hash=API_HASH, session_string=SESSION, in_memory=True)
    bot = Client("anicard_leader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

    await userbot.start()
    await bot.start()
    
    await bot.set_bot_commands([
        BotCommand("top", "🏆 Топ кланов сезона"),
        BotCommand("report", "📈 Аналитика и рейды (дата: /report 15.10)"),
        BotCommand("check", "🔄 Срочно обновить данные (ручной опрос)"),
        BotCommand("help", "📖 Инструкция по боту"),
        BotCommand("history", "📜 История конкретного клана"),
    ])
    try: await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception: pass

    await userbot.send_message(OWNER_ID, f"🟢 Бот запущен [{now_msk_str()} МСК]")
    await setup_userbot_listener()
    await setup_bot_handlers()

    try: await fetch_top_and_process(source="стартовый")
    except Exception: pass

    asyncio.create_task(daily_report_loop())
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        try: await fetch_top_and_process(source="плановый")
        except Exception: pass

if __name__ == "__main__":
    asyncio.run(main())
