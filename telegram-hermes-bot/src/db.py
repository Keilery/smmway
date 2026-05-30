"""Хранение истории диалогов в SQLite (чтобы контекст переживал перезапуски)."""
import os

import aiosqlite

from .config import DB_PATH

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages (user_id);"
)


async def init_db() -> None:
    """Создать файл БД и таблицы, если их ещё нет."""
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_TABLE)
        await db.execute(_CREATE_INDEX)
        await db.commit()


async def add_message(user_id: int, role: str, content: str) -> None:
    """Сохранить одно сообщение диалога (role = 'user' или 'assistant')."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await db.commit()


async def get_history(user_id: int, limit: int) -> list[dict]:
    """Вернуть последние `limit` сообщений пользователя в хронологическом порядке."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    rows.reverse()  # из «новые сверху» делаем хронологический порядок
    return [{"role": role, "content": content} for role, content in rows]


async def clear_history(user_id: int) -> None:
    """Удалить всю историю диалога пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await db.commit()
