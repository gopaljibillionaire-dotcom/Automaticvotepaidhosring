import os
import logging
import aiosqlite
from typing import List
from pydantic_settings import BaseSettings

# Configure Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("egg_chicken_bot")

class Settings(BaseSettings):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "7353822838:AAEUvYQ8pGRGyBtSH9kzKVYiRQ3VgjVOCa4")
    DATABASE_NAME: str = os.getenv("DATABASE_NAME", "egg_chicken_bot.db")
    ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "7952327997").split(",") if x.strip()]
    PAYMENT_API_KEY: str = os.getenv("PAYMENT_API_KEY", "your_payment_api_key")
    PAYMENT_SECRET: str = os.getenv("PAYMENT_SECRET", "your_payment_secret")

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()

# Database Manager class required by admin.py and other modules
class DatabaseManager:
    def __init__(self, db_name: str):
        self.db_name = db_name

    async def get_db(self):
        conn = await aiosqlite.connect(self.db_name)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        return conn

# Export db_mgr for import in admin.py and other files
db_mgr = DatabaseManager(settings.DATABASE_NAME)
