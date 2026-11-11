import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def parse_superadmin_ids(raw_value: str) -> set[int]:
    """Parse comma-separated Telegram IDs into a set of integers."""
    ids: set[int] = set()
    if not raw_value:
        return ids
    for chunk in raw_value.split(","):
        cleaned = chunk.strip()
        if cleaned:
            try:
                ids.add(int(cleaned))
            except ValueError:
                continue
    return ids


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    default_openai_base_url: str
    default_openai_api_key: str
    default_openai_model: str
    superadmin_ids: set[int] = field(default_factory=set)
    db_path: str = "data/bot.db"


def load_config() -> AppConfig:
    """Load configuration from environment variables."""
    token = os.getenv("BOT_TOKEN", "").strip()
    base_url = os.getenv("DEFAULT_OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    api_key = os.getenv("DEFAULT_OPENAI_API_KEY", "").strip()
    model = os.getenv("DEFAULT_OPENAI_MODEL", "gpt-4o-mini").strip()
    superadmins_raw = os.getenv("SUPERADMIN_IDS", "").strip()
    db_path = os.getenv("DB_PATH", "data/bot.db").strip()

    return AppConfig(
        bot_token=token,
        default_openai_base_url=base_url,
        default_openai_api_key=api_key,
        default_openai_model=model,
        superadmin_ids=parse_superadmin_ids(superadmins_raw),
        db_path=db_path,
    )


config = load_config()
