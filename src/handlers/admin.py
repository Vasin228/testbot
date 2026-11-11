import html
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from src.config import config
from src.db import db
from src.llm import llm_engine

admin_router = Router()


class AdminStates(StatesGroup):
    waiting_for_system_prompt = State()
    waiting_for_base_url = State()
    waiting_for_api_key = State()
    waiting_for_model = State()


async def check_admin_rights(event: Message | CallbackQuery, bot: Bot) -> bool:
    """Check if the user is a superadmin or chat administrator."""
    user = event.from_user
    if not user:
        return False
    if user.id in config.superadmin_ids:
        return True

    chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
    if chat and chat.type in ("group", "supergroup"):
        try:
            member = await bot.get_chat_member(chat.id, user.id)
            return member.status in ("creator", "administrator")
        except Exception:
            return False
    return False


async def build_admin_dashboard(chat_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate main admin dashboard text and keyboard."""
    settings = await db.get_all_settings()
    model = settings.get("openai_model", config.default_openai_model)
    base_url = settings.get("openai_base_url", config.default_openai_base_url)
    api_key = settings.get("openai_api_key", "")
    masked_key = (api_key[:6] + "..." + api_key[-4:]) if len(api_key) > 10 else ("Установлен" if api_key else "НЕ ЗАДАН")
    system_prompt = settings.get("system_prompt", "")
    short_prompt = (system_prompt[:120] + "...") if len(system_prompt) > 120 else system_prompt
    auto_create = settings.get("allow_auto_create_topics", "1") == "1"

    topics_count = await db.count_topics(chat_id)
    kb_count = await db.count_knowledge(chat_id)

    text = (
        "⚙️ <b>Панель управления VibeRouter Bot</b>\n\n"
        f"🤖 <b>Модель:</b> <code>{html.escape(model)}</code>\n"
        f"🌐 <b>Base URL:</b> <code>{html.escape(base_url)}</code>\n"
        f"🔑 <b>API Key:</b> <code>{html.escape(masked_key)}</code>\n"
        f"🎛 <b>Авто-создание веток:</b> {'✅ ВКЛ' if auto_create else '❌ ВЫКЛ'}\n"
        f"📊 <b>База:</b> {topics_count} веток | {kb_count} материалов в индексе\n\n"
        f"🧠 <b>System Prompt:</b>\n<i>{html.escape(short_prompt)}</i>"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧠 Изменить System Prompt", callback_data="admin:set_prompt"),
                InlineKeyboardButton(text="🤖 Изменить Модель", callback_data="admin:set_model"),
            ],
            [
                InlineKeyboardButton(text="🌐 Изменить Base URL", callback_data="admin:set_base_url"),
                InlineKeyboardButton(text="🔑 Изменить API Key", callback_data="admin:set_api_key"),
            ],
            [
                InlineKeyboardButton(text="🔌 Тест подключения LLM", callback_data="admin:test_llm"),
            ],
            [
                InlineKeyboardButton(text="📂 Управление ветками", callback_data="admin:manage_topics"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:refresh"),
            ],
        ]
    )
    return text, kb


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot, state: FSMContext) -> None:
    """Handle /admin command."""
    if not await check_admin_rights(message, bot):
        await message.reply("⛔️ У вас нет прав доступа к панели управления.")
        return

    await state.clear()
    chat_id = message.chat.id if message.chat.type in ("group", "supergroup") else None
    text, kb = await build_admin_dashboard(chat_id)
    await message.reply(text, parse_mode="HTML", reply_markup=kb)


@admin_router.callback_query(F.data == "admin:refresh")
async def cb_admin_refresh(call: CallbackQuery, bot: Bot) -> None:
    """Refresh admin dashboard."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    chat_id = call.message.chat.id if call.message and call.message.chat.type in ("group", "supergroup") else None
    text, kb = await build_admin_dashboard(chat_id)
    if call.message:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer("Панель обновлена.")


@admin_router.callback_query(F.data == "admin:test_llm")
async def cb_admin_test_llm(call: CallbackQuery, bot: Bot) -> None:
    """Test LLM connection and display response time."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    await call.answer("⏳ Тестирую подключение к LLM...", show_alert=False)
    success, msg = await llm_engine.test_connection()
    if call.message:
        status_icon = "✅" if success else "❌"
        await call.message.reply(
            f"{status_icon} <b>Результат проверки LLM:</b>\n\n<code>{html.escape(msg)}</code>",
            parse_mode="HTML",
        )


@admin_router.callback_query(F.data == "admin:set_prompt")
async def cb_admin_set_prompt(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Prompt user for a new system prompt."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_system_prompt)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]]
    )
    if call.message:
        await call.message.reply(
            "🧠 <b>Введите новый System Prompt для бота:</b>\n"
            "Он будет использоваться при роутинге, составлении карточек и ответах в чате.",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
    await call.answer()


@admin_router.callback_query(F.data == "admin:set_model")
async def cb_admin_set_model(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Prompt user for a new model identifier."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_model)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]]
    )
    if call.message:
        await call.message.reply(
            "🤖 <b>Введите идентификатор модели:</b>\n"
            "Примеры:\n"
            "• <code>gpt-4o-mini</code>\n"
            "• <code>deepseek/deepseek-chat</code>\n"
            "• <code>anthropic/claude-3.5-sonnet</code>\n"
            "• <code>meta-llama/llama-3.1-70b-instruct</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
    await call.answer()


@admin_router.callback_query(F.data == "admin:set_base_url")
async def cb_admin_set_base_url(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Prompt user for a new base URL."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_base_url)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]]
    )
    if call.message:
        await call.message.reply(
            "🌐 <b>Введите новый OpenAI-compatible Base URL:</b>\n"
            "Примеры:\n"
            "• <code>https://api.openai.com/v1</code>\n"
            "• <code>https://openrouter.ai/api/v1</code>\n"
            "• <code>http://host.docker.internal:11434/v1</code> (Ollama)",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
    await call.answer()


@admin_router.callback_query(F.data == "admin:set_api_key")
async def cb_admin_set_api_key(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    """Prompt user for a new API key."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_api_key)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")]]
    )
    if call.message:
        await call.message.reply(
            "🔑 <b>Отправьте новый API ключ:</b>\n"
            "⚠️ <i>Для безопасности бот моментально удалит ваше сообщение с ключом из чата.</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
    await call.answer()


@admin_router.callback_query(F.data == "admin:cancel")
async def cb_admin_cancel(call: CallbackQuery, state: FSMContext) -> None:
    """Cancel any active FSM state."""
    await state.clear()
    if call.message:
        await call.message.edit_text("❌ Ввод отменен.")
    await call.answer("Отменено.")


# FSM Handlers for text input
@admin_router.message(AdminStates.waiting_for_system_prompt)
async def state_receive_prompt(message: Message, state: FSMContext) -> None:
    new_prompt = (message.text or "").strip()
    if not new_prompt:
        await message.reply("Промпт не может быть пустым. Попробуйте еще раз.")
        return
    await db.set_setting("system_prompt", new_prompt)
    await state.clear()
    await message.reply("✅ <b>System Prompt успешно обновлен!</b>", parse_mode="HTML")


@admin_router.message(AdminStates.waiting_for_model)
async def state_receive_model(message: Message, state: FSMContext) -> None:
    new_model = (message.text or "").strip()
    if not new_model:
        await message.reply("Имя модели не может быть пустым.")
        return
    await db.set_setting("openai_model", new_model)
    await state.clear()
    await message.reply(f"✅ <b>Модель изменена на:</b> <code>{html.escape(new_model)}</code>", parse_mode="HTML")


@admin_router.message(AdminStates.waiting_for_base_url)
async def state_receive_base_url(message: Message, state: FSMContext) -> None:
    new_url = (message.text or "").strip()
    if not new_url.startswith("http://") and not new_url.startswith("https://"):
        await message.reply("URL должен начинаться с http:// или https://")
        return
    await db.set_setting("openai_base_url", new_url)
    await state.clear()
    await message.reply(f"✅ <b>Base URL изменен на:</b> <code>{html.escape(new_url)}</code>", parse_mode="HTML")


@admin_router.message(AdminStates.waiting_for_api_key)
async def state_receive_api_key(message: Message, bot: Bot, state: FSMContext) -> None:
    key = (message.text or "").strip()
    # Immediately delete user message containing the secret key
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

    if not key:
        await message.reply("Ключ не может быть пустым.")
        return

    await db.set_setting("openai_api_key", key)
    await state.clear()
    await message.reply("✅ <b>API ключ сохранен и сообщение удалено из истории!</b>", parse_mode="HTML")


# Topic management handlers
@admin_router.callback_query(F.data == "admin:manage_topics")
async def cb_admin_manage_topics(call: CallbackQuery, bot: Bot) -> None:
    """Manage registered topics and toggle auto-creation."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    chat_id = call.message.chat.id if call.message and call.message.chat.type in ("group", "supergroup") else None
    topics = await db.get_topics(chat_id) if chat_id else []
    auto_create_val = await db.get_setting("allow_auto_create_topics") or "1"
    auto_create_bool = auto_create_val == "1"

    toggle_btn_text = f"Авто-создание веток: {'✅ ВКЛ' if auto_create_bool else '❌ ВЫКЛ'}"
    kb_rows = [
        [InlineKeyboardButton(text=toggle_btn_text, callback_data="admin:toggle_auto_create")],
    ]

    # Topic list
    lines = ["📂 <b>Зарегистрированные ветки форума:</b>\n"]
    if topics:
        for t in topics:
            lines.append(f"• <b>{html.escape(t['name'])}</b> (ID: <code>{t['thread_id']}</code>)")
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🗑 Удалить: {t['name'][:25]}",
                    callback_data=f"admin:del_topic:{t['chat_id']}:{t['thread_id']}",
                )
            ])
    else:
        lines.append("<i>В этой группе еще нет зарегистрированных веток. Используйте /setup или /reg.</i>")

    kb_rows.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin:refresh")])

    if call.message:
        await call.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    await call.answer()


@admin_router.callback_query(F.data == "admin:toggle_auto_create")
async def cb_admin_toggle_auto_create(call: CallbackQuery, bot: Bot) -> None:
    """Toggle allow_auto_create_topics setting between 1 and 0."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    current = await db.get_setting("allow_auto_create_topics") or "1"
    new_val = "0" if current == "1" else "1"
    await db.set_setting("allow_auto_create_topics", new_val)
    await cb_admin_manage_topics(call, bot)


@admin_router.callback_query(F.data.startswith("admin:del_topic:"))
async def cb_admin_del_topic(call: CallbackQuery, bot: Bot) -> None:
    """Remove a topic from router registry."""
    if not await check_admin_rights(call, bot):
        await call.answer("⛔️ Доступ запрещен", show_alert=True)
        return

    parts = call.data.split(":")
    if len(parts) == 4:
        chat_id = int(parts[2])
        thread_id = int(parts[3])
        await db.delete_topic(chat_id, thread_id)
        await call.answer("Ветка удалена из роутинга.")
        await cb_admin_manage_topics(call, bot)
