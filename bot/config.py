# config.py
import os
import logging
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Jira (базовый URL, можно переопределять для каждого пользователя)
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://fk.jira.lanit.ru")

# WebApp
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://google.com")

# Настройки бота
DEFAULT_CHECK_INTERVAL = int(os.getenv("DEFAULT_CHECK_INTERVAL", 5))
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x]

# Database
DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/jira_bot.db")  

# Encryption - ВАЖНО: если задан в .env, используем его, иначе None (автогенерация)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if ENCRYPTION_KEY:
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode()  # Преобразуем строку в bytes для Fernet

# Настройки напоминаний
REMINDER_INTERVAL_MINUTES = int(os.getenv("REMINDER_INTERVAL_MINUTES", "30"))

# Ночной режим (MSK)
NIGHT_MODE_ENABLED = os.getenv("NIGHT_MODE_ENABLED", "true").lower() == "true"
NIGHT_START_HOUR = int(os.getenv("NIGHT_START_HOUR", "22"))  # 22:00 MSK
NIGHT_END_HOUR = int(os.getenv("NIGHT_END_HOUR", "9"))      # 09:00 MSK

# Упоминания
MENTIONS_ENABLED = os.getenv("MENTIONS_ENABLED", "true").lower() == "true"
MENTIONS_CHECK_INTERVAL = int(os.getenv("MENTIONS_CHECK_INTERVAL", "5"))  # минут

# Debug
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# Validation
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env file")
if not JIRA_BASE_URL:
    raise ValueError("JIRA_BASE_URL is not set in .env file")

# Logging configuration
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

logger.info("✅ Configuration loaded for multi-user bot")
logger.info(f"📂 Database path: {DATABASE_PATH}")
logger.info(f"🔑 Encryption key: {'Provided' if ENCRYPTION_KEY else 'Will be auto-generated'}")
logger.info(f"⏰ Reminder interval: {REMINDER_INTERVAL_MINUTES} minutes")
logger.info(f"🌙 Night mode: {'Enabled' if NIGHT_MODE_ENABLED else 'Disabled'} (MSK {NIGHT_START_HOUR:02d}:00 - {NIGHT_END_HOUR:02d}:00)")
logger.info(f"🔔 Mentions tracking: {'Enabled' if MENTIONS_ENABLED else 'Disabled'}")