import os
import re
import asyncio
import sqlite3
from datetime import datetime

from pyrogram import Client

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["SESSION_STRING"]
OWNER = os.environ["OWNER_ID"]  # @username или ID — строка

ANICARD_USERNAME = "anicardplaybot"
DB_PATH = "anicard.db"
INTERVAL_SECONDS = 15 * 60


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            clan_name TEXT NOT NULL,
            points INTEGER NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ts ON snapshots(ts)")
    conn.commit()
    conn.close()


def save_snapshot(clans):
    ts = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO snapshots (ts, clan_name, points) VALUES (?, ?, ?)",
        [(ts, name, pts) for name, pts in clans],
    )
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
    cur.execute("SELECT clan_name, points FROM snapshots WHERE ts = ?", (last_ts,))
    rows = cur.fetchall()
    conn.close()
    return dict(rows)


CLAN_LINE_RE = re.compile(r"^\d+\.\s+(.+?)\s+-\s+(\d+)\s+🔹", re.MULTILINE)


def parse_top(text):
    return [(m.group(1).strip(), int(m.group(2))) for m in CLAN_LINE_RE.finditer(text)]


async def fetch_top(app):
    await app.send_message(ANICARD_USERNAME, "/start")
    await asyncio.sleep(3)

    await app.send_message(ANICARD_USERNAME, "🛡 Кланы")
    await asyncio.sleep(3)

    await app.send_message(ANICARD_USERNAME, "🏆 Топ кланов")
    await asyncio.sleep(3)

    top_text_msg = None
    async for msg in app.get_chat_history(ANICARD_USERNAME, limit=15):
        if msg.text and "Топ кланов по очкам" in msg.text:
            top_text_msg = msg
            break
    if not top_text_msg:
        raise RuntimeError("Не найдено сообщение с топом кланов")
    return parse_top(top_text_msg.text)


async def send_report(app, text):
    try:
        await app.send_message(OWNER, text)
    except Exception as e:
        print(f"Не удалось отправить отчёт: {e}")


async def main():
    init_db()
    async with Client(
        name="anicard_userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION,
        in_memory=True,
    ) as app:
        await send_report(app, "🟢 Userbot Anicard запущен")
        while True:
            try:
                clans = await fetch_top(app)
                print(f"[{datetime.utcnow().isoformat()}] Получено {len(clans)} кланов")
                prev = get_last_snapshot()
                deltas = []
                for name, pts in clans:
                    if name in prev:
                        d = pts - prev[name]
                        if d != 0:
                            deltas.append((name, d, pts))
                save_snapshot(clans)
                if deltas:
                    lines = ["📊 Обновление топа:"]
                    for name, d, pts in deltas:
                        if d > 0:
                            lines.append(f"📈 {name}: +{d} ({pts})")
                        else:
                            lines.append(f"📉 {name}: {d} ({pts})")
                    await send_report(app, "\n".join(lines))
            except Exception as e:
                await send_report(app, f"⚠️ Ошибка: {e}")
            await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
