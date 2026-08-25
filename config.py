import os
from typing import List
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "7353822838:AAEUvYQ8pGRGyBtSH9kzKVYiRQ3VgjVOCa4")
    DATABASE_NAME: str = os.getenv("DATABASE_NAME", "market_bot.db")
    ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "7952327997").split(",") if x.strip()]
    PAYMENT_API_KEY: str = os.getenv("PAYMENT_API_KEY", "mock_key")
    PAYMENT_SECRET: str = os.getenv("PAYMENT_SECRET", "mock_secret")
    WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))

    class Config:
        env_file = ".env"

settings = Settings()
