import asyncio
import json
import re
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
import aiosqlite

from config import (
    API_ID, API_HASH, BOT_TOKEN, SUPER_OWNER_IDS, LOG_CHANNEL_ID,
    logger, encrypt_data, decrypt_data, db_mgr
)
from admin import admin_router, parse_telegram_link

# --- FINITE STATE MACHINE (FSM) FOR CONTEXT HANDLING ---
class AccountStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class TaskStates(StatesGroup):
    waiting_for_target = State()

# --- ANTI-BAN BACKGROUND TASK ENGINE ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance))

    async def start_worker(self):
        logger.info("⚡ Background Task Pipeline Engine Active.")
        while True:
            task_id, creator_id, task_type, payload, bot_instance = await self.queue.get()
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except Exception as e:
                logger.error(f"Execution runtime failure on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        import random
        
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
                await db.execute("UPDATE tasks SET status = 'failed', progress = 'No active accounts connected' WHERE task_id = ?", (task_id,))
                await db.commit()
            return

        passed_ids, failed_ids = [], []
        total_accounts = len(clients_data)

        for index, (phone, raw_session) in enumerate(clients_data):
            client = TelegramClient(StringSession(raw_session), API_ID, API_HASH)
            try:
                # Anti-ban distributed throttling delay
                await asyncio.sleep(random.uniform(1.5, 3.5))
                await client.connect()
                
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                    failed_ids.append((phone, "Session string expired/revoked"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id = parse_telegram_link(target)

                if task_type == "join":
                    if isinstance(parsed_target, str) and ("+" in target or "joinchat/" in target):
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                        
                elif task_type == "leave":
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))
                    
                elif task_type == "view":
                    msg_id = int(payload.get("msg_id", link_msg_id or 0))
                    if msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(peer=parsed_target, id=[msg_id], increment=True))
                    else:
                        raise ValueError("Message ID missing for lookups")

                passed_ids.append(phone)
            except Exception as e:
                failed_ids.append((phone, str(e)))
            finally:
                await client.disconnect()

            # Dynamic real-time calculation update loops
            progress_pct = f"{int(((index + 1) / total_accounts) * 100)}%"
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_pct, task_id))
                await db.commit()

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'completed', success_report = ?, failure_report = ? WHERE task_id = ?",
                             (json.dumps(passed_ids), json.dumps(failed_ids), task_id))
            await db.commit()
            
        await db_mgr.log_action(creator_id, f"Completed Automation Task #{task_id} ({task_type.upper()}). Success: {len(passed_ids)} | Failed: {len(failed_ids)}", bot_instance)

# Global Instance Allocation
task_queue = TaskQueue()
main_router = Router()
main_router.include_router(admin_router)

# --- KEYBOARDS & INTERACTION INTERFACES ---
def get_main_menu_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Add Telegram Account", callback_code="add_acc")],
        [InlineKeyboardButton(text="⚙️ Task Hub (Automation)", callback_code="task_hub")],
        [InlineKeyboardButton(text="📊 Account Statistics", callback_code="acc_stats")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="👑 Server Admin Core", callback_code="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Fix Aiogram inline keyboard typing helper compatibility issues
def InlineKeyboardButton(text: str, callback_code: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_code)

# --- INLINE COMMAND & ACTIONS LOGIC ---
@main_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or f"User_{user_id}"
    
    await db_mgr.create_user_if_not_exists(user_id, username)
    role = await db_mgr.get_user_role(user_id)
    
    welcome_text = (
        f"🤖 **Enterprise Telegram Automation Hub 2026**\n\n"
        f"Hello, {message.from_user.first_name}!\n"
        f"Current Privilege Authorization Level: `{role.upper()}`\n\n"
        f"Use the interactive menu interface parameters down below to drive execution parameters:"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu_keyboard(role))

@main_router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text("🏠 Main Dashboard Controller Environment:", reply_markup=get_main_menu_keyboard(role))

# --- ACCOUNT HANDLING SUB-SYSTEM (TELETHON STRINGS BRIDGE) ---
@main_router.callback_query(F.data == "add_acc")
async def cb_add_account(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AccountStates.waiting_for_phone)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_code="main_menu")]])
    await callback.message.edit_text("📱 Please enter the **Phone Number** with country code (e.g., `+1234567890`):", reply_markup=kb)

@main_router.message(AccountStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await message.answer("❌ Invalid entry format. Include your country code symbol! Example: `+1234567890`")
        return

    await state.update_data(phone=phone)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(phone)
        await state.update_data(phone_code_hash=sent_code.phone_code_hash)
        # Store dynamic session state parameters on heap
        loop = asyncio.get_running_loop()
        state_str = client.session.save()
        await state.update_data(session_str=state_str)
        
        await state.set_state(AccountStates.waiting_for_code)
        await message.answer(f"📩 Login confirmation code sent to `{phone}`.\nPlease respond with your system entry code:")
    except Exception as e:
        logger.error(f"Failed sending validation token: {e}")
        await message.answer(f"❌ Error dispatching payload directly from Telegram infrastructure:\n`{str(e)}`")
        await state.clear()
    finally:
        await client.disconnect()

@main_router.message(AccountStates.waiting_for_code)
async def process_code(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    code = message.text.strip()
    
    client = TelegramClient(StringSession(data['session_str']), API_ID, API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(phone=data['phone'], code=code, phone_code_hash=data['phone_code_hash'])
        # If authorized successfully, save down mapping records
        await save_connected_account(client, data['phone'], message.from_user.id, bot)
        await message.answer("🎉 Account bridge authenticated successfully!", reply_markup=get_main_menu_keyboard(await db_mgr.get_user_role(message.from_user.id)))
        await state.clear()
    except SessionPasswordNeededError:
        await state.set_state(AccountStates.waiting_for_2fa)
        await message.answer("🔐 Two-Step Verification (2FA) is active. Enter Cloud Password:")
    except (PhoneCodeInvalidError, Exception) as error:
        await message.answer(f"❌ Verification failed: `{str(error)}`. Try again or restart.")
    finally:
        await client.disconnect()

@main_router.message(AccountStates.waiting_for_2fa)
async def process_2fa(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    password = message.text.strip()
    
    client = TelegramClient(StringSession(data['session_str']), API_ID, API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(password=password)
        await save_connected_account(client, data['phone'], message.from_user.id, bot)
        await message.answer("🎉 Authenticated successfully with 2FA!", reply_markup=get_main_menu_keyboard(await db_mgr.get_user_role(message.from_user.id)))
        await state.clear()
    except (PasswordHashInvalidError, Exception) as error:
        await message.answer(f"❌ Access denied: `{str(error)}`. Re-verify your master password:")
    finally:
        await client.disconnect()

async def save_connected_account(client: TelegramClient, phone: str, user_id: int, bot: Bot):
    me = await client.get_me()
    username = me.username or f"NoUsername_{me.id}"
    enc_session = encrypt_data(client.session.save())
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status) VALUES (?, ?, ?, ?, 'active')",
            (phone, user_id, username, enc_session)
        )
        await db.commit()
    await db_mgr.log_action(user_id, f"Linked Telegram session profile: {phone} (@{username})", bot)

# --- STATS REPORT HUB ---
@main_router.callback_query(F.data == "acc_stats")
async def cb_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            async with db.execute("SELECT COUNT(*), status FROM accounts GROUP BY status") as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute("SELECT COUNT(*), status FROM accounts WHERE user_id = ? GROUP BY status", (user_id,)) as cursor:
                rows = await cursor.fetchall()
                
    stats_dict = {status: count for count, status in rows}
    active_count = stats_dict.get("active", 0)
    dead_count = stats_dict.get("dead", 0)
    
    stats_msg = (
        f"📊 **System Fleet Performance Metrics**\n\n"
        f"🟢 **Active Authorized Accounts:** `{active_count}`\n"
        f"🔴 **Disconnected/Dead Sessions:** `{dead_count}`\n\n"
        f"Total tracked operations inside current structural schema context."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[[InlineKeyboardButton(text="🏠 Menu", callback_code="main_menu")]][0]])
    await callback.message.edit_text(stats_msg, reply_markup=kb)

# --- CHANNELS / CHATS JOINS & VIEWS AUTOMATION PANEL ---
@main_router.callback_query(F.data == "task_hub")
async def cb_task_hub(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Mass Channel Join", callback_code="task_init:join")],
        [InlineKeyboardButton(text="🏃 Mass Channel Leave", callback_code="task_init:leave")],
        [InlineKeyboardButton(text="👁️ View Counter Booster", callback_code="task_init:view")],
        [InlineKeyboardButton(text="🔙 Back", callback_code="main_menu")]
    ])
    await callback.message.edit_text("⚙️ **Distributed Task Command Desk**\nChoose structural automation pipeline utility below:", reply_markup=kb)

@main_router.callback_query(F.data.startswith("task_init:"))
async def cb_task_init(callback: CallbackQuery, state: FSMContext):
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    await state.set_state(TaskStates.waiting_for_target)
    
    prompt_msg = "🔗 Enter the Target Telegram Link or Public Handle Username:\n(e.g., `https://t.me/example` or `https://t.me/c/12345/67` or `@channel`)"
    if task_type == "view":
        prompt_msg = "🔗 Enter post link configuration:\nFormat: `https://t.me/public_channel/123` or custom target pointer string"
        
    await callback.message.edit_text(prompt_msg)

@main_router.message(TaskStates.waiting_for_target)
async def process_task_target(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    task_type = data['task_type']
    target_link = message.text.strip()
    
    payload = {"target": target_link}
    
    # Run parsing extraction fallback safeguards
    if task_type == "view":
        chan, msg_id = parse_telegram_link(target_link)
        if not msg_id:
            await message.answer("❌ Unresolved sequence parameter metadata. Ensure post target contains numeric identifier pointers.")
            return
        payload["msg_id"] = msg_id

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (creator_id, type, payload, status) VALUES (?, ?, ?, 'pending') RETURNING task_id",
            (message.from_user.id, task_type, json.dumps(payload))
        )
        row = await cursor.fetchone()
        task_id = row[0] if row else None
        await db.commit()

    if not task_id:
        await message.answer("❌ Error provisioning structural background task index.")
        await state.clear()
        return

    # Put task directly into the multi-account non-blocking task queue
    await task_queue.add_task(task_id, message.from_user.id, task_type, payload, bot)
    await message.answer(f"✅ **Task Buffered Successfully!**\n🆔 **Task ID:** `{task_id}`\n📊 **Status:** Enqueued in pipeline pipeline.\n\nUse stats metrics desk to review updates asynchronously.")
    await state.clear()

# --- LONGRUNNING ENGINE THREAD INTERFACE BOOTSTRAPS ---
async def main():
    logger.info("Initializing SQLite schemas...")
    await db_mgr.init()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(main_router)
    
    # Fire up the parallel background task queue loop worker
    worker_task = asyncio.create_task(task_queue.start_worker())
    
    logger.info("Starting long-polling instance updates matrix...")
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Automation environment offline.")
