import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from src.config import config
from src.db import db
from src.handlers import main_router
from src.middlewares import TopicDiscoveryMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("viberouter")


async def setup_bot_commands(bot: Bot) -> None:
    """Register standard commands in Telegram menu."""
    commands = [
        BotCommand(command="setup", description="Инициализация форума и создание веток (Админ)"),
        BotCommand(command="admin", description="Интерактивная панель управления AI и роутером"),
        BotCommand(command="search", description="Умный поиск по базе знаний команды"),
        BotCommand(command="reg", description="Регистрация ветки форума в роутере"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Bot commands successfully registered in Telegram menu.")
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)


async def main() -> None:
    """VibeRouter Bot startup routine."""
    if not config.bot_token:
        logger.error(
            "CRITICAL: BOT_TOKEN is not set in environment or .env file! "
            "Please copy .env.example to .env and configure your bot token."
        )
        sys.exit(1)

    logger.info("Initializing database at %s ...", config.db_path)
    await db.init_db()
    logger.info("Database initialized successfully.")

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Register Passive Topic Discovery Middleware for all incoming messages
    dp.message.middleware(TopicDiscoveryMiddleware())

    # Register all handler routers
    dp.include_router(main_router)

    # Set up commands menu
    await setup_bot_commands(bot)

    bot_info = await bot.get_me()
    logger.info("Logged in as @%s (ID: %d)", bot_info.username, bot_info.id)
    logger.info("Starting long polling mode...")

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        logger.info("Closing bot session...")
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("VibeRouter Bot stopped.")
