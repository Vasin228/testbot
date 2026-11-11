import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from src.db import db

logger = logging.getLogger(__name__)


class TopicDiscoveryMiddleware(BaseMiddleware):
    """Passive Forum Topic Discovery Middleware.
    
    Telegram Bot API has no getForumTopics method. This middleware passively
    captures topic creation, edits, and active threads to build a complete
    registry in SQLite automatically.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat and event.chat.type in ("supergroup", "group"):
            chat_id = event.chat.id

            # 1. Direct event: forum topic created
            if event.forum_topic_created:
                thread_id = event.message_thread_id or event.message_id
                name = event.forum_topic_created.name
                await db.add_or_update_topic(
                    chat_id=chat_id,
                    thread_id=thread_id,
                    name=name,
                    description="Создана через событие форума",
                )
                logger.info("Discovered new topic: '%s' (thread_id: %d) in chat %d", name, thread_id, chat_id)

            # 2. Direct event: forum topic edited
            elif event.forum_topic_edited:
                thread_id = event.message_thread_id or event.message_id
                name = event.forum_topic_edited.name
                if name:
                    await db.add_or_update_topic(
                        chat_id=chat_id,
                        thread_id=thread_id,
                        name=name,
                    )
                    logger.info("Updated topic: '%s' (thread_id: %d) in chat %d", name, thread_id, chat_id)

            # 3. Message in topic with reply to topic creation
            elif event.is_topic_message and event.message_thread_id:
                thread_id = event.message_thread_id
                if event.reply_to_message and event.reply_to_message.forum_topic_created:
                    name = event.reply_to_message.forum_topic_created.name
                    await db.add_or_update_topic(
                        chat_id=chat_id,
                        thread_id=thread_id,
                        name=name,
                        description="Обнаружена через первое сообщение ветки",
                    )
                    logger.info("Discovered topic from reply: '%s' (thread_id: %d)", name, thread_id)
                else:
                    # Register thread if not already in topics table
                    existing = await db.get_topic(chat_id=chat_id, thread_id=thread_id)
                    if not existing:
                        await db.add_or_update_topic(
                            chat_id=chat_id,
                            thread_id=thread_id,
                            name=f"Ветка #{thread_id}",
                            description="Авто-обнаружена по сообщению",
                        )
                        logger.info("Discovered untracked thread_id %d in chat %d", thread_id, chat_id)

        return await handler(event, data)
