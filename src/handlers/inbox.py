import html
import re
import logging
from typing import Any
from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from src.db import db
from src.sandbox import sandbox
from src.llm import llm_engine
from src.handlers.admin import check_admin_rights

logger = logging.getLogger(__name__)
inbox_router = Router()


def extract_urls(message: Message) -> list[str]:
    """Extract all URLs from message text or caption and entities."""
    urls: list[str] = []
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []

    for entity in entities:
        if entity.type == "url":
            extracted = text[entity.offset : entity.offset + entity.length]
            if extracted and extracted not in urls:
                urls.append(extracted)
        elif entity.type == "text_link" and entity.url:
            if entity.url not in urls:
                urls.append(entity.url)

    # Regex fallback
    if not urls and text:
        found = re.findall(r"https?://[^\s<>\"']+", text)
        for u in found:
            clean = u.rstrip(".,;:!?)")
            if clean and clean not in urls:
                urls.append(clean)
    return urls


def format_post_link(chat_id: int, thread_id: int, message_id: int) -> str:
    """Generate universal Telegram deep-link for a supergroup topic message."""
    clean_id = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{clean_id}/{thread_id}/{message_id}"


async def _safe_reply_chunk(message: Message, chunk: str, parse_mode: str | None) -> None:
    """Send message chunk with automatic fallback to plain text on HTML parse error."""
    try:
        await message.reply(chunk, parse_mode=parse_mode)
    except TelegramBadRequest:
        await message.reply(chunk, parse_mode=None)


async def split_and_reply(message: Message, text: str, parse_mode: str = "HTML") -> None:
    """Send text split into safe chunks (< 4000 characters) to avoid Telegram limit."""
    chunk_size = 3800
    if len(text) <= chunk_size:
        await _safe_reply_chunk(message, text, parse_mode)
        return

    lines = text.split("\n")
    current_chunk = ""
    for line in lines:
        if len(current_chunk) + len(line) + 1 > chunk_size:
            if current_chunk:
                await _safe_reply_chunk(message, current_chunk, parse_mode)
                current_chunk = ""
        current_chunk += (line + "\n")

    if current_chunk.strip():
        await _safe_reply_chunk(message, current_chunk, parse_mode)


class IsInboxMessageFilter(BaseFilter):
    """Filter matching only non-command messages with URLs or files inside the configured Inbox thread."""

    async def __call__(self, message: Message) -> bool:
        if not message.chat or message.chat.type not in ("group", "supergroup"):
            return False
        if message.text and message.text.startswith("/"):
            return False

        inbox_chat_id = await db.get_setting("inbox_chat_id")
        inbox_thread_id = await db.get_setting("inbox_thread_id")
        if not inbox_chat_id or not inbox_thread_id:
            return False

        if (
            str(message.chat.id) != inbox_chat_id
            or message.message_thread_id != int(inbox_thread_id)
        ):
            return False

        return bool(extract_urls(message) or message.document)


@inbox_router.message(Command("setup"))
async def cmd_setup(message: Message, bot: Bot) -> None:
    """Initialize group forum structure: Inbox topic, starter topics, and pinned guide."""
    if not await check_admin_rights(message, bot):
        await message.reply("⛔️ Команда доступна только администраторам группы.")
        return

    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        await message.reply("⚠️ Данная команда должна вызываться в супергруппе с включенными темами (Topics/Forum).")
        return

    status_msg = await message.reply("🛠 Начинаю инициализацию форума и веток...")
    chat_id = chat.id

    try:
        # 1. Create or register Inbox topic
        inbox_thread_id_setting = await db.get_setting("inbox_thread_id")
        inbox_chat_id_setting = await db.get_setting("inbox_chat_id")

        inbox_thread_id: int
        if (
            inbox_chat_id_setting == str(chat_id)
            and inbox_thread_id_setting
            and inbox_thread_id_setting.isdigit()
        ):
            inbox_thread_id = int(inbox_thread_id_setting)
        else:
            inbox_topic = await bot.create_forum_topic(chat_id=chat_id, name="📥 Входящие")
            inbox_thread_id = inbox_topic.message_thread_id
            await db.set_setting("inbox_chat_id", str(chat_id))
            await db.set_setting("inbox_thread_id", str(inbox_thread_id))
            await db.add_or_update_topic(
                chat_id=chat_id,
                thread_id=inbox_thread_id,
                name="📥 Входящие",
                description="Входной шлюз для анализа и распределения материалов",
            )

        # 2. Check and create default starter topics if database is empty
        starter_topics = [
            ("🤖 AI & LLM", "Нейросети, агенты, модели и промпт-инжиниринг"),
            ("🛠 DevTools & Libraries", "Инструменты разработки, библиотеки и фреймворки"),
            ("📚 Статьи и Гайды", "Технические статьи, туториалы и обзоры"),
        ]

        existing_topics = await db.get_topics(chat_id)
        # Exclude inbox topic from count
        real_topics = [t for t in existing_topics if t["thread_id"] != inbox_thread_id]

        created_starter_names: list[str] = []
        if not real_topics:
            for s_name, s_desc in starter_topics:
                try:
                    topic = await bot.create_forum_topic(chat_id=chat_id, name=s_name)
                    await db.add_or_update_topic(
                        chat_id=chat_id,
                        thread_id=topic.message_thread_id,
                        name=s_name,
                        description=s_desc,
                    )
                    created_starter_names.append(s_name)
                except Exception as e:
                    logger.warning("Could not create starter topic %s: %s", s_name, e)

        # 3. Post pinned instruction into Inbox topic
        guide_text = (
            "📥 <b>Добро пожаловать во «Входящие»!</b>\n\n"
            "Скидывайте сюда любые ссылки, GitHub-репозитории, статьи, архивы и файлы.\n"
            "Я сам выйду в интернет, изучу содержимое и разложу по полочкам в нужные ветки!\n\n"
            "✨ <b>Возможности:</b>\n"
            "• 🛡 <i>Анти-Боян:</i> предупрежу, если ссылку уже скидывали\n"
            "• 🕵️‍♂️ <i>Изолированная песочница:</i> разбор сайтов, GitHub API, ZIP и PDF\n"
            "• 📌 <i>Auto-Wiki:</i> закрепленный список материалов в каждой ветке\n"
            "• 🔍 <i>Vibe-Check:</i> глубокий архитектурный аудит по кнопке"
        )

        inbox_post = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=inbox_thread_id,
            text=guide_text,
            parse_mode="HTML",
        )
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=inbox_post.message_id)
        except Exception:
            pass

        summary_msg = (
            "✅ <b>Инициализация VibeRouter успешно завершена!</b>\n\n"
            f"📥 <b>Ветка «Входящие»:</b> ID <code>{inbox_thread_id}</code>\n"
        )
        if created_starter_names:
            summary_msg += f"📂 <b>Созданы стартовые темы:</b> {', '.join(created_starter_names)}\n"
        summary_msg += "\nТеперь любые ссылки и файлы, отправленные во «Входящие», будут автоматически анализироваться и распределяться!"

        await status_msg.edit_text(summary_msg, parse_mode="HTML")

    except TelegramBadRequest as e:
        await status_msg.edit_text(
            f"❌ Ошибка Telegram API при настройке: <code>{html.escape(str(e))}</code>\n"
            "Убедитесь, что у бота есть права администратора (Управление темами, Закрепление сообщений).",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Setup failed")
        await status_msg.edit_text(f"❌ Ошибка настройки: {str(e)}")


async def handle_auto_wiki_pin(
    bot: Bot,
    chat_id: int,
    target_thread_id: int,
    item_title: str,
    item_summary: str,
    post_link: str,
) -> None:
    """Killer Feature: Maintain Auto-Wiki navigator pinned message in target topic."""
    try:
        topic_info = await db.get_topic(chat_id, target_thread_id)
        pin_message_id = topic_info.get("pin_message_id") if topic_info else None

        new_entry_line = f"• <a href=\"{post_link}\">{html.escape(item_title)}</a> — {html.escape(item_summary[:90])}"

        if not pin_message_id:
            # Create new pinned navigator message
            wiki_text = (
                "📌 <b>Навигатор ветки (Auto-Wiki)</b>\n\n"
                "Здесь автоматически собираются все полезные материалы ветки:\n"
                f"{new_entry_line}"
            )
            sent_pin = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=target_thread_id,
                text=wiki_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_pin.message_id)
            await db.update_topic_pin(chat_id, target_thread_id, sent_pin.message_id)
        else:
            # Edit existing pinned message
            # Fetch existing message by editing or recreating if deleted
            try:
                # We append to the message. First construct updated text
                # We can request existing text by attempting edit or maintaining simple update
                # Since Telegram doesn't have getMessage API without MTProto, we read previous from topic
                # To be resilient, we store navigator entries or prepend cleanly
                wiki_header = "📌 <b>Навигатор ветки (Auto-Wiki)</b>\n\nЗдесь автоматически собираются все полезные материалы ветки:\n"
                
                # Fetch recent items for this topic from knowledge_base
                recent_items = await db.search_knowledge("", chat_id=chat_id, limit=20)
                topic_items = [it for it in recent_items if it.get("thread_id") == target_thread_id][:15]

                clean_chat_id = str(chat_id).replace("-100", "")
                entries: list[str] = []
                for it in topic_items:
                    link = f"https://t.me/c/{clean_chat_id}/{target_thread_id}/{it['message_id']}"
                    entries.append(f"• <a href=\"{link}\">{html.escape(it['title'])}</a> — {html.escape((it.get('summary') or '')[:80])}")

                if not entries:
                    entries.append(new_entry_line)

                updated_text = wiki_header + "\n".join(entries)
                if len(updated_text) > 3900:
                    updated_text = updated_text[:3850] + "\n• <i>...и другие материалы выше в ленте</i>"

                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pin_message_id,
                    text=updated_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest as e:
                if "message to edit not found" in str(e).lower():
                    # Message was deleted manually; recreate it
                    wiki_text = (
                        "📌 <b>Навигатор ветки (Auto-Wiki)</b>\n\n"
                        "Здесь автоматически собираются все полезные материалы ветки:\n"
                        f"{new_entry_line}"
                    )
                    sent_pin = await bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=target_thread_id,
                        text=wiki_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    await bot.pin_chat_message(chat_id=chat_id, message_id=sent_pin.message_id)
                    await db.update_topic_pin(chat_id, target_thread_id, sent_pin.message_id)
    except Exception as e:
        logger.warning("Auto-Wiki pin update notice: %s", e)


@inbox_router.message(IsInboxMessageFilter())
async def process_inbox_messages(message: Message, bot: Bot) -> None:
    """Pipeline for messages in the Inbox topic."""
    chat_id = message.chat.id
    inbox_thread_id = message.message_thread_id or 0

    urls = extract_urls(message)
    has_document = bool(message.document)

    if not urls and not has_document:
        return

    sender_name = message.from_user.full_name if message.from_user else "Аноним"
    user_mention = f"@{message.from_user.username}" if message.from_user and message.from_user.username else sender_name

    # Step 1: Anti-Boyan check for URLs
    primary_url = urls[0] if urls else ""
    if primary_url:
        duplicate = await db.find_knowledge_by_url(primary_url)
        if duplicate:
            dup_chat_id = duplicate["chat_id"]
            dup_thread_id = duplicate["thread_id"]
            dup_msg_id = duplicate["message_id"]
            dup_link = format_post_link(dup_chat_id, dup_thread_id, dup_msg_id)
            await message.reply(
                f"⚠️ <b>Этот материал уже скидывали ранее!</b>\n\n"
                f"📌 <b>{html.escape(duplicate.get('title', 'Ранее опубликованный материал'))}</b>\n"
                f"👤 Автор первой публикации: {html.escape(duplicate.get('sender_name', 'Коллега'))}\n"
                f"👉 <a href=\"{dup_link}\">Перейти к карточке и обсуждению</a>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

    # Step 2: Sandbox inspection
    status_msg = await message.reply("🕵️‍♂️ <i>Песочница запущена: изучаю содержимое...</i>", parse_mode="HTML")

    sandbox_data: dict[str, Any]
    if primary_url:
        if "github.com" in primary_url:
            await status_msg.edit_text("🐙 <i>Песочница: опрашиваю GitHub API и анализирую дерево репозитория...</i>", parse_mode="HTML")
            sandbox_data = await sandbox.inspect_github(primary_url)
        else:
            await status_msg.edit_text("🌐 <i>Песочница: загружаю и анализирую веб-страницу...</i>", parse_mode="HTML")
            sandbox_data = await sandbox.inspect_url(primary_url)
    elif has_document and message.document:
        await status_msg.edit_text(f"📦 <i>Песочница: загружаю и изолированно парсю файл {html.escape(message.document.file_name or '')}...</i>", parse_mode="HTML")
        sandbox_data = await sandbox.inspect_telegram_file(
            bot=bot,
            file_id=message.document.file_id,
            file_name=message.document.file_name or "file.bin",
        )
    else:
        await status_msg.edit_text("❌ Не удалось извлечь ссылку или файл.")
        return

    # Step 3: LLM Routing
    await status_msg.edit_text("🧠 <i>AI-архитектор анализирует материал и определяет ветку...</i>", parse_mode="HTML")
    existing_topics = await db.get_topics(chat_id)
    # Filter out inbox topic itself
    selectable_topics = [t for t in existing_topics if t["thread_id"] != int(inbox_thread_id)]
    allow_new = (await db.get_setting("allow_auto_create_topics") or "1") == "1"

    user_note = message.caption if has_document else (message.text or "")
    routed = await llm_engine.analyze_and_route(
        user_text=user_note,
        sandbox_data=sandbox_data,
        existing_topics=selectable_topics,
        allow_new_topics=allow_new,
    )

    action = routed.get("action", "route_existing")
    target_thread_id = routed.get("target_thread_id")
    target_topic_name = ""

    # Step 4: Handle new topic creation or existing topic
    if action == "create_new" and allow_new:
        new_name = (routed.get("new_topic_name") or "💡 Новая тема")[:40]
        new_desc = routed.get("new_topic_description", "")
        try:
            new_forum_topic = await bot.create_forum_topic(chat_id=chat_id, name=new_name)
            target_thread_id = new_forum_topic.message_thread_id
            target_topic_name = new_name
            await db.add_or_update_topic(
                chat_id=chat_id,
                thread_id=target_thread_id,
                name=new_name,
                description=new_desc,
            )
        except Exception as e:
            logger.warning("Could not auto-create topic '%s': %s", new_name, e)
            if selectable_topics:
                target_thread_id = selectable_topics[0]["thread_id"]
                target_topic_name = selectable_topics[0]["name"]
            else:
                target_thread_id = int(inbox_thread_id)
                target_topic_name = "Входящие"
    else:
        # Fallback to selectable topics if target_thread_id is missing
        if not target_thread_id and selectable_topics:
            target_thread_id = selectable_topics[0]["thread_id"]
            target_topic_name = selectable_topics[0]["name"]
        else:
            # Find name
            for t in existing_topics:
                if t["thread_id"] == target_thread_id:
                    target_topic_name = t["name"]
                    break
            if not target_topic_name:
                target_topic_name = f"Ветка #{target_thread_id}"

    # Step 5: Publish card into target topic
    item_title = routed.get("title") or sandbox_data.get("title") or "Без названия"
    item_tldr = routed.get("tldr") or "Описание отсутствует."
    use_cases = routed.get("use_cases") or []
    tech_stack = routed.get("tech_stack") or []

    use_cases_formatted = "\n".join(f"• {html.escape(uc)}" for uc in use_cases) if use_cases else "• Для повседневных задач команды"
    tags_formatted = " ".join(t if t.startswith("#") else f"#{t}" for t in tech_stack) if tech_stack else "#dev"

    card_header = f"<b>{html.escape(item_title)}</b>"
    if primary_url:
        card_header += f"\n🔗 <a href=\"{primary_url}\">{html.escape(primary_url)}</a>"

    card_text = (
        f"{card_header}\n\n"
        f"💡 <b>Что это (TL;DR):</b>\n{html.escape(item_tldr)}\n\n"
        f"🎯 <b>Зачем это нужно:</b>\n{use_cases_formatted}\n\n"
        f"🏷 <b>Стек / Теги:</b> {html.escape(tags_formatted)}\n"
        f"👤 <b>Прислал:</b> {html.escape(user_mention)}"
    )

    # Save to knowledge base first to acquire item_id for interactive callback buttons
    item_id = await db.add_knowledge(
        chat_id=chat_id,
        thread_id=target_thread_id,
        message_id=0,  # placeholder, updated right after send
        sender_name=sender_name,
        source_url=primary_url,
        title=item_title,
        summary=item_tldr,
        use_cases="\n".join(use_cases),
        tags=tags_formatted,
        raw_context=sandbox_data.get("raw_context", ""),
    )

    card_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Vibe-Check / Аудит", callback_data=f"vibecheck:{item_id}:audit"),
                InlineKeyboardButton(text="🚀 Как внедрить (Quickstart)", callback_data=f"vibecheck:{item_id}:quickstart"),
            ]
        ]
    )

    sent_card = await bot.send_message(
        chat_id=chat_id,
        message_thread_id=target_thread_id,
        text=card_text,
        parse_mode="HTML",
        disable_web_page_preview=False,
        reply_markup=card_kb,
    )

    # Update knowledge_base record with actual message_id
    await db.update_knowledge_message_id(item_id, sent_card.message_id)

    post_link = format_post_link(chat_id, target_thread_id, sent_card.message_id)

    # Step 6: Update Auto-Wiki pinned navigator in target topic
    await handle_auto_wiki_pin(
        bot=bot,
        chat_id=chat_id,
        target_thread_id=target_thread_id,
        item_title=item_title,
        item_summary=item_tldr,
        post_link=post_link,
    )

    # Step 7: Update Inbox status message with direct post link
    inbox_report = (
        "✅ <b>Разобрано и распределено!</b>\n\n"
        f"📂 <b>Ветка:</b> {html.escape(target_topic_name)}\n"
        f"📌 <b>Заголовок:</b> {html.escape(item_title)}\n"
        f"👉 <a href=\"{post_link}\">Перейти к карточке материала</a>"
    )
    await status_msg.edit_text(inbox_report, parse_mode="HTML", disable_web_page_preview=True)


@inbox_router.callback_query(F.data.startswith("vibecheck:"))
async def cb_vibe_check(call: CallbackQuery, bot: Bot) -> None:
    """Handle interactive Vibe-Check audit and quickstart buttons."""
    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer("Неверные параметры запроса", show_alert=True)
        return

    item_id = int(parts[1])
    mode = parts[2]  # 'audit' or 'quickstart'

    item_record = await db.get_knowledge(item_id)
    if not item_record:
        await call.answer("Материал не найден в базе знаний.", show_alert=True)
        return

    mode_label = "архитектурный аудит" if mode == "audit" else "руководство по быстрому внедрению"
    await call.answer(f"⏳ Готовлю {mode_label}...")

    # Notify in thread that audit is generating
    if call.message:
        wait_msg = await call.message.reply(
            f"🧠 <i>AI-архитектор формирует {mode_label}...</i>", parse_mode="HTML"
        )
        try:
            report_text = await llm_engine.vibe_check_audit(item_record, mode)
            await wait_msg.delete()
            header = "🔍 <b>Vibe-Check: Архитектурный аудит</b>\n\n" if mode == "audit" else "🚀 <b>Quickstart: Инструкция по внедрению</b>\n\n"
            full_response = header + report_text
            await split_and_reply(call.message, full_response, parse_mode="HTML")
        except Exception as e:
            logger.exception("Vibe-check audit error")
            await wait_msg.edit_text(f"❌ Ошибка генерации отчета: {str(e)}")
