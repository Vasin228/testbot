import os
import aiosqlite
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator
from src.config import config

DEFAULT_SYSTEM_PROMPT = (
    "Ты — умный AI-архитектор и куратор базы знаний команды разработчиков. "
    "Анализируй скинутые ссылки, репозитории и файлы, объясняй их практическую "
    "пользу без воды и распределяй по тематическим веткам группы."
)


class Database:
    """Async SQLite database manager for VibeRouter Bot."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or config.db_path

    @asynccontextmanager
    async def get_connection(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        dir_name = os.path.dirname(self.db_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    async def init_db(self) -> None:
        """Create tables if they do not exist and initialize default settings."""
        async with self.get_connection() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    chat_id INTEGER,
                    thread_id INTEGER,
                    name TEXT,
                    description TEXT,
                    pin_message_id INTEGER DEFAULT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_base (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    thread_id INTEGER,
                    message_id INTEGER,
                    sender_name TEXT,
                    source_url TEXT,
                    title TEXT,
                    summary TEXT,
                    use_cases TEXT,
                    tags TEXT,
                    raw_context TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Populate default settings if not already present
            defaults = {
                "openai_base_url": config.default_openai_base_url,
                "openai_api_key": config.default_openai_api_key,
                "openai_model": config.default_openai_model,
                "system_prompt": DEFAULT_SYSTEM_PROMPT,
                "inbox_chat_id": "",
                "inbox_thread_id": "",
                "allow_auto_create_topics": "1",
            }

            for key, val in defaults.items():
                await conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, val),
                )
            await conn.commit()

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a setting by key."""
        async with self.get_connection() as conn:
            async with conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return str(row["value"])
                return default

    async def set_setting(self, key: str, value: str) -> None:
        """Set or update a configuration setting."""
        async with self.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await conn.commit()

    async def get_all_settings(self) -> dict[str, str]:
        """Fetch all dynamic settings as a dictionary."""
        async with self.get_connection() as conn:
            async with conn.execute("SELECT key, value FROM settings") as cursor:
                rows = await cursor.fetchall()
                return {row["key"]: row["value"] for row in rows}

    async def add_or_update_topic(
        self,
        chat_id: int,
        thread_id: int,
        name: str,
        description: str = "",
        pin_message_id: int | None = None,
    ) -> None:
        """Register or update forum topic details."""
        async with self.get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO topics (chat_id, thread_id, name, description, pin_message_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    name = excluded.name,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE topics.description END,
                    pin_message_id = COALESCE(excluded.pin_message_id, topics.pin_message_id)
                """,
                (chat_id, thread_id, name, description, pin_message_id),
            )
            await conn.commit()

    async def update_topic_pin(
        self, chat_id: int, thread_id: int, pin_message_id: int
    ) -> None:
        """Update the Auto-Wiki navigator pin message ID for a topic."""
        async with self.get_connection() as conn:
            await conn.execute(
                """
                UPDATE topics SET pin_message_id = ?
                WHERE chat_id = ? AND thread_id = ?
                """,
                (pin_message_id, chat_id, thread_id),
            )
            await conn.commit()

    async def get_topics(self, chat_id: int) -> list[dict[str, Any]]:
        """Retrieve all registered topics for a chat."""
        async with self.get_connection() as conn:
            async with conn.execute(
                "SELECT chat_id, thread_id, name, description, pin_message_id FROM topics WHERE chat_id = ? ORDER BY name ASC",
                (chat_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_topic(self, chat_id: int, thread_id: int) -> dict[str, Any] | None:
        """Retrieve a specific topic by chat_id and thread_id."""
        async with self.get_connection() as conn:
            async with conn.execute(
                "SELECT chat_id, thread_id, name, description, pin_message_id FROM topics WHERE chat_id = ? AND thread_id = ?",
                (chat_id, thread_id),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def delete_topic(self, chat_id: int, thread_id: int) -> None:
        """Remove a topic from routing."""
        async with self.get_connection() as conn:
            await conn.execute(
                "DELETE FROM topics WHERE chat_id = ? AND thread_id = ?",
                (chat_id, thread_id),
            )
            await conn.commit()

    async def find_knowledge_by_url(self, source_url: str) -> dict[str, Any] | None:
        """Anti-Boyan feature: Check if a URL was already posted and indexed."""
        normalized_url = source_url.strip().rstrip("/")
        async with self.get_connection() as conn:
            async with conn.execute(
                """
                SELECT id, chat_id, thread_id, message_id, sender_name, source_url, title, summary, use_cases, tags, created_at
                FROM knowledge_base
                WHERE source_url = ? OR RTRIM(source_url, '/') = ?
                ORDER BY id DESC LIMIT 1
                """,
                (source_url, normalized_url),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def add_knowledge(
        self,
        chat_id: int,
        thread_id: int,
        message_id: int,
        sender_name: str,
        source_url: str,
        title: str,
        summary: str,
        use_cases: str,
        tags: str,
        raw_context: str,
    ) -> int:
        """Insert indexed material into knowledge_base."""
        async with self.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO knowledge_base (
                    chat_id, thread_id, message_id, sender_name, source_url,
                    title, summary, use_cases, tags, raw_context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    thread_id,
                    message_id,
                    sender_name,
                    source_url,
                    title,
                    summary,
                    use_cases,
                    tags,
                    raw_context,
                ),
            )
            await conn.commit()
            return cursor.lastrowid or 0

    async def update_knowledge_message_id(self, item_id: int, message_id: int) -> None:
        """Update knowledge base record with actual Telegram message_id."""
        async with self.get_connection() as conn:
            await conn.execute(
                "UPDATE knowledge_base SET message_id = ? WHERE id = ?",
                (message_id, item_id),
            )
            await conn.commit()

    async def get_knowledge(self, item_id: int) -> dict[str, Any] | None:
        """Get knowledge record by ID for Vibe-Check audit."""
        async with self.get_connection() as conn:
            async with conn.execute(
                """
                SELECT id, chat_id, thread_id, message_id, sender_name, source_url,
                       title, summary, use_cases, tags, raw_context, created_at
                FROM knowledge_base WHERE id = ?
                """,
                (item_id,),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def search_knowledge(
        self, query: str, chat_id: int | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Case-insensitive Unicode search in knowledge_base with recent fallback for LLM semantic matching."""
        sql = """
            SELECT id, chat_id, thread_id, message_id, sender_name, source_url,
                   title, summary, use_cases, tags, created_at
            FROM knowledge_base
        """
        params: list[Any] = []
        if chat_id is not None:
            sql += " WHERE chat_id = ?"
            params.append(chat_id)
        sql += " ORDER BY id DESC LIMIT 100"

        async with self.get_connection() as conn:
            async with conn.execute(sql, params) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]

        cleaned_query = query.strip().lower()
        if not cleaned_query:
            return rows[:limit]

        tokens = [t for t in cleaned_query.split() if len(t) >= 2]
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            haystack = " ".join([
                str(row.get("title") or ""),
                str(row.get("summary") or ""),
                str(row.get("use_cases") or ""),
                str(row.get("tags") or ""),
                str(row.get("source_url") or ""),
            ]).lower()

            score = 0
            if cleaned_query in haystack:
                score += 10
            for token in tokens:
                if token in haystack:
                    score += 3
            if score > 0:
                scored.append((score, row))

        if scored:
            scored.sort(key=lambda x: (x[0], x[1]["id"]), reverse=True)
            return [item for _, item in scored[:limit]]

        # Fallback: return latest items so LLM can match cross-language / semantic queries (e.g. "свич" -> "Switch")
        return rows[:limit]

    async def count_topics(self, chat_id: int | None = None) -> int:
        """Count registered topics."""
        async with self.get_connection() as conn:
            if chat_id is not None:
                async with conn.execute(
                    "SELECT COUNT(*) as cnt FROM topics WHERE chat_id = ?", (chat_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return int(row["cnt"]) if row else 0
            else:
                async with conn.execute("SELECT COUNT(*) as cnt FROM topics") as cursor:
                    row = await cursor.fetchone()
                    return int(row["cnt"]) if row else 0

    async def count_knowledge(self, chat_id: int | None = None) -> int:
        """Count knowledge items."""
        async with self.get_connection() as conn:
            if chat_id is not None:
                async with conn.execute(
                    "SELECT COUNT(*) as cnt FROM knowledge_base WHERE chat_id = ?",
                    (chat_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                    return int(row["cnt"]) if row else 0
            else:
                async with conn.execute(
                    "SELECT COUNT(*) as cnt FROM knowledge_base"
                ) as cursor:
                    row = await cursor.fetchone()
                    return int(row["cnt"]) if row else 0


db = Database()
