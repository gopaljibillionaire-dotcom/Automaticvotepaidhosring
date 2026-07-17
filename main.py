import asyncio
import json
import os
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
import aiosqlite

from config import (
    API_ID, API_HASH, BOT_TOKEN, SUPER_OWNER_IDS, LOG_CHANNEL_ID,
    logger, encrypt_data, decrypt_data
)
from admin import admin_router, parse_telegram_link

# Add database initialization tracking to share safely across files
class Database:
    def __init__(self, db_path: str = "bot_core_data.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, username TEXT, role TEXT DEFAULT 'user', 
                    max_accounts INTEGER DEFAULT 5, referred_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY, user_id INTEGER, username TEXT, session_string TEXT,
                    status TEXT DEFAULT 'active', last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER, type TEXT, payload TEXT, 
                    status TEXT DEFAULT 'pending', progress TEXT DEFAULT '0%', success_report TEXT, failure_report TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def log_action(self, user_id: int, action: str, bot_instance: Bot = None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
                await db.commit()
        except Exception as db_err:
            logger.error(f"Failed to log action: {db_err}")
        
        if bot_instance:
            try:
                log_text = f"📝 **System Audit Log**\n👤 **User ID:** `{user_id}`\n⚡ **Action:** {action}"
                await bot_instance.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
            except Exception as e:
                logger.error(f"Failed sending log updates: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in SUPER_OWNER_IDS:
            return "super_owner"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else "user"

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: int = None):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                if not await cursor.fetchone():
                    await db.execute("INSERT INTO users (user_id, username, role, referred_by) VALUES (?, ?, 'user', ?)", (user_id, username, referred_by))
                    await db.commit()

import config
db_mgr = Database()
config.db_mgr = db_mgr

class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance))

    async def start_worker(self):
        logger.info("Anti-Ban Task pipeline processing loop started.")
        while True:
            task_id, creator_id, task_type, payload, bot_instance = await self.queue.get()
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except Exception as e:
                logger.error(f"Execution failure on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        import random
        from telethon.errors import FloodWaitError

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
            await db.commit()

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                cursor = await db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'")
            else:
                cursor = await db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?", (creator_id,))
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = '0 active bridges' WHERE task_id = ?", (task_id,))
                await db.commit()
            return

        passed_ids, failed_ids = [], []
        total_accounts = len(clients_data)

        for index, (phone, enc_session) in enumerate(clients_data):
            client = TelegramClient(StringSession(enc_session), API_ID, API_HASH)
            try:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await client.connect()
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                    failed_ids.append((phone, "Expired"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id = parse_telegram_link(target)
                msg_id = int(payload.get("msg_id", link_msg_id or 0))

                if task_type == "join":
                    if isinstance(parsed_target, str) and ("+" in target or "joinchat/" in target):
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                elif task_type == "views":
                    if msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(peer=parsed_target, id=[msg_id], increment=True))
                # Additional routes continue contextually...
                passed_ids.append(phone)
            except Exception as e:
                failed_ids.append((phone, str(e)))
            finally:
                await client.disconnect()

            progress_pct = f"{int(((index + 1) / total_accounts) * 100)}%"
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_pct, task_id))
                await db.commit()

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'completed', success_report = ?, failure_report = ? WHERE task_id = ?",
                             (json.dumps(passed_ids), json.dumps(failed_ids), task_id))
            await db.commit()

task_queue = TaskQueue()
router = Router()
router.include_router(admin_router)

# Insert your standard UI key generations, callback filters, and command routines below...
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id
    await db_mgr.create_user_if_not_exists(user_id, message.from_user.username or "Unknown")
    role = await db_mgr.get_user_role(user_id)
    await message.answer(f"👋 Framework Online.\n🛡️ Privilege Level: `{role.upper()}`")

async def main():
    await db_mgr.init()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    worker = asyncio.create_task(task_queue.start_worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()

if __name__ == "__main__":
    asyncio.run(main())
