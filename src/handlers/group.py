import html
import logging
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from src.db import db
from src.sandbox import sandbox
from src.llm import llm_engine
from src.handlers.inbox import extract_urls, split_and_reply

logger = logging.getLogger(__name__)
group_router = Router()


@group_router.message(Command("reg"))
async def cmd_reg(message: Message) -> None:
    """Manually register existing forum topic in VibeRouter database."""
    if not message.is_topic_message or not message.message_thread_id:
        await message.reply(
            "⚠️ Команда <code>/reg</code> должна вызываться внутри ветки форума, которую вы хотите зарегистрировать.",
            parse_mode="HTML",
        )
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id

    # Extract topic name from arguments
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        topic_name = parts[1].strip()
    elif message.reply_to_message and message.reply_to_message.forum_topic_created:
        topic_name = message.reply_to_message.forum_topic_created.name
    else:
        await message.reply(
            "ℹ️ Укажите название ветки: <code>/reg Название ветки</code>\n"
            "Или ответьте этой командой на первое сервисное сообщение темы.",
            parse_mode="HTML",
        )
        return

    await db.add_or_update_topic(
        chat_id=chat_id,
        thread_id=thread_id,
        name=topic_name,
        description="Зарегистрирована вручную через /reg",
    )
    await message.reply(
        f"✅ Ветка <b>«{html.escape(topic_name)}»</b> (ID: <code>{thread_id}</code>) успешно зарегистрирована в роутере!",
        parse_mode="HTML",
    )


@group_router.message(Command("search"))
async def cmd_search(message: Message) -> None:
    """Smart knowledge base search with LLM synthesis and links to group posts."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "🔍 <b>Умный поиск по базе знаний</b>\n"
            "Использование: <code>/search ваш запрос</code>\n\n"
            "Пример: <code>/search docker swarm vs kubernetes</code>",
            parse_mode="HTML",
        )
        return

    query = parts[1].strip()
    chat_id = message.chat.id
    status_msg = await message.reply("🔎 <i>Ищу подходящие материалы в базе знаний...</i>", parse_mode="HTML")

    matched_items = await db.search_knowledge(query=query, chat_id=chat_id, limit=8)
    if not matched_items:
        # Fallback to search across all chats if not found in current
        matched_items = await db.search_knowledge(query=query, chat_id=None, limit=8)

    if not matched_items:
        await status_msg.edit_text(
            f"🤷‍♂️ По запросу «<b>{html.escape(query)}</b>» в базе знаний ничего не найдено.\n"
            "Попробуйте изменить формулировку или скинуть полезные ссылки во «Входящие»!",
            parse_mode="HTML",
        )
        return

    await status_msg.edit_text("🧠 <i>AI-архитектор синтезирует ответ с цитатами из базы знаний...</i>", parse_mode="HTML")

    clean_chat_id = str(chat_id).replace("-100", "")
    try:
        synthesis = await llm_engine.search_synthesize(
            query=query,
            matched_items=matched_items,
            clean_chat_id=clean_chat_id,
        )
        response_text = (
            f"🔍 <b>Результаты поиска по запросу «{html.escape(query)}»:</b>\n\n"
            f"{synthesis}"
        )
        await status_msg.delete()
        await split_and_reply(message, response_text, parse_mode="HTML")
    except Exception as e:
        logger.exception("Search synthesis error")
        await status_msg.edit_text(f"❌ Ошибка синтеза ответа: {str(e)}")


@group_router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_topic_mentions_and_replies(message: Message, bot: Bot) -> None:
    """Answer questions when tagged (@bot_username) or replied to in any forum topic."""
    # Ignore commands
    if message.text and message.text.startswith("/"):
        return

    bot_info = await bot.get_me()
    bot_username = (bot_info.username or "").lower()

    text = message.text or message.caption or ""
    is_reply_to_bot = bool(
        message.reply_to_message
        and not message.reply_to_message.forum_topic_created
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    )

    is_mentioned = False
    if f"@{bot_username}" in text.lower():
        is_mentioned = True
    elif message.entities:
        for ent in message.entities:
            if ent.type == "mention":
                mention_text = text[ent.offset : ent.offset + ent.length].lower()
                if mention_text == f"@{bot_username}":
                    is_mentioned = True
                    break

    if not is_reply_to_bot and not is_mentioned:
        return

    # Clean bot mention from prompt text
    cleaned_prompt = text.replace(f"@{bot_info.username}", "").replace(f"@{bot_username}", "").strip()
    if not cleaned_prompt and message.reply_to_message:
        cleaned_prompt = message.reply_to_message.text or "Проанализируй данное сообщение."

    wait_msg = await message.reply("🤔 <i>Формирую ответ...</i>", parse_mode="HTML")

    # If prompt contains URLs, inspect them via Sandbox for fresh context
    urls = extract_urls(message)
    if not urls and message.reply_to_message:
        urls = extract_urls(message.reply_to_message)

    context_text = ""
    if urls:
        target_url = urls[0]
        if "github.com" in target_url:
            sb_data = await sandbox.inspect_github(target_url)
        else:
            sb_data = await sandbox.inspect_url(target_url)
        context_text = sb_data.get("raw_context", "")

    try:
        reply_answer = await llm_engine.general_chat(
            user_question=cleaned_prompt,
            context_text=context_text,
        )
        await wait_msg.delete()
        await split_and_reply(message, reply_answer, parse_mode="HTML")
    except Exception as e:
        logger.exception("Topic mention assistant error")
        await wait_msg.edit_text(f"❌ Не удалось сформировать ответ: {str(e)}")
