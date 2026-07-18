import asyncio
import base64
import json
import os
import re
import random
from typing import Dict, Any, List, Optional, Tuple

# aiogram 3.x imports
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)

# Telethon imports
from telethon import TelegramClient, functions, types as tg_types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    FloodWaitError
)

# SQLite
import aiosqlite

# Import local configurations
import config
from config import logger

# --- CRYPTO HELPERS ---
def _get_crypto_key() -> int:
    return sum(ord(c) for c in config.SECRET_KEY) % 256 or 42

def encrypt_data(data: str) -> str:
    key = _get_crypto_key()
    cipher_bytes = bytes([b ^ key for b in data.encode('utf-8')])
    return base64.b64encode(cipher_bytes).decode('utf-8')

def decrypt_data(encrypted_data: str) -> str:
    key = _get_crypto_key()
    try:
        raw_cipher = base64.b64decode(encrypted_data.encode('utf-8'))
        plain_bytes = bytes([b ^ key for b in raw_cipher])
        return plain_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Decryption failure: {e}")
        return ""

# --- LINK PARSING HELPER ---
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int]]:
    link = link.strip()
    if not link:
        return None, None
        
    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return channel_id, msg_id

    if "+ " in link or "/+" in link or "joinchat/" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        return (hash_match.group(1) if hash_match else link, None)
        
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match:
        return msg_match.group(1), int(msg_match.group(2))
        
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = int(parts[1])
            return target, msg_id
            
    return target, None

# --- DATABASE ENGINE ---
class Database:
    def __init__(self, db_path: str = "bot_core_data.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    role TEXT DEFAULT 'user', 
                    max_accounts INTEGER DEFAULT 5,
                    referred_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY,
                    user_id INTEGER, 
                    username TEXT,
                    session_string TEXT,
                    status TEXT DEFAULT 'active', 
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER,
                    type TEXT, 
                    payload TEXT, 
                    status TEXT DEFAULT 'pending', 
                    progress TEXT DEFAULT '0%',
                    success_report TEXT,
                    failure_report TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()
            logger.info("Database system initialized.")

    async def log_action(self, user_id: int, action: str, bot_instance: Optional[Bot] = None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
                await db.commit()
        except Exception as db_err:
            logger.error(f"Failed to log action to local DB: {db_err}")
        
        if bot_instance and config.LOG_CHANNEL_ID:
            try:
                log_text = (
                    f"📝 **System Audit Log**\n"
                    f"👤 **User ID:** `{user_id}`\n"
                    f"⚡ **Action:** {action}"
                )
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=log_text)
            except Exception as e:
                logger.error(f"Failed sending log channel metric updates: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in config.SUPER_OWNER_IDS:
            return "super_owner"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else "user"

    async def get_admin_limits(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT max_accounts FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 5

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None):
        async with aiosqlite.connect(db_mgr.db_path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                if not await cursor.fetchone():
                    await db.execute(
                        "INSERT INTO users (user_id, username, role, referred_by) VALUES (?, ?, 'user', ?)",
                        (user_id, username, referred_by)
                    )
                    await db.commit()

db_mgr = Database()
registration_sessions: Dict[int, Dict[str, Any]] = {}
bot_username: str = "bot"

# --- ANTI-BAN TASK MANAGER ENGINE ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance))

    def clear_pending_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def cancel_all_active_tasks(self) -> int:
        count = 0
        self.clear_pending_queue()
        active_ids = list(self.current_tasks.keys())
        for t_id in active_ids:
            loop_task = self.current_tasks.get(t_id)
            if loop_task and not loop_task.done():
                loop_task.cancel()
                count += 1
                async with aiosqlite.connect(db_mgr.db_path) as db:
                    await db.execute(
                        "UPDATE tasks SET status = 'cancelled', progress = 'Terminated via Panic Command' WHERE task_id = ?", 
                        (t_id,)
                    )
                    await db.commit()
        return count

    async def start_worker(self):
        logger.info("Anti-Ban Task pipeline processing loop started.")
        while True:
            try:
                task_id, creator_id, task_type, payload, bot_instance = await self.queue.get()
            except asyncio.CancelledError:
                break
                
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except asyncio.CancelledError:
                logger.warning(f"Task #{task_id} execution was forcefully cancelled via panic event loop command.")
            except Exception as e:
                logger.error(f"Execution failure on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot):
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
            await db.commit()

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active'"
                cursor = await db.execute(query)
            else:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
                cursor = await db.execute(query, (creator_id,))
            
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = '0 active bridges available' WHERE task_id = ?", (task_id,))
                await db.commit()
            await db_mgr.log_action(creator_id, f"Failed task #{task_id} (No active accounts available)", bot_instance)
            return

        passed_ids: List[str] = []
        failed_ids: List[Tuple[str, str]] = []
        total_accounts = len(clients_data)

        BATCH_SIZE = 5               
        BASE_COOLDOWN = 15           

        for index, (phone, enc_session) in enumerate(clients_data):
            client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
            try:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await client.connect()
                if not await client.is_user_authorized():
                    async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                        await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                        await db_conn.commit()
                    failed_ids.append((phone, "Unauthorized/Session Expired"))
                    continue

                target = payload.get("target", "")
                parsed_target, link_msg_id = parse_telegram_link(target)
                msg_id = int(payload.get("msg_id", link_msg_id or 0))

                await asyncio.sleep(random.uniform(1.0, 2.5))

                do_react = "react" in task_type
                do_vote = "vote" in task_type
                do_view = "view" in task_type or task_type == "speed"
                do_join = task_type == "join"
                do_leave = task_type == "leave"
                do_dm = task_type == "dm"
                do_refer = task_type == "refer"

                if do_join:
                    if isinstance(parsed_target, str) and ("/+" in target or "joinchat/" in target or target.startswith("+")):
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                        
                if do_leave:
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))

                if do_view:
                    if msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(
                            peer=parsed_target,
                            id=[msg_id],
                            increment=True
                        ))
                    else:
                        await client.get_messages(parsed_target, limit=5)

                if do_react:
                    emojis = payload.get("reactions", ["👍"])
                    assigned_emoji = emojis[index % len(emojis)]
                    await client(functions.messages.SendReactionRequest(
                        peer=parsed_target,
                        msg_id=msg_id,
                        reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                    ))

                if do_vote:
                    button_text = payload.get("button_text", "").strip().lower()
                    msg = await client.get_messages(parsed_target, ids=msg_id)
                    if msg and msg.reply_markup:
                        target_button = None
                        for row in msg.reply_markup.rows:
                            for btn in row.buttons:
                                if button_text in btn.text.strip().lower():
                                    target_button = btn
                                    break
                            if target_button:
                                break
                                
                        if target_button and isinstance(target_button, tg_types.KeyboardButtonCallback):
                            await client(functions.messages.GetBotCallbackAnswerRequest(
                                peer=parsed_target,
                                msg_id=msg_id,
                                data=target_button.data
                            ))
                        else:
                            raise ValueError(f"No callback button containing '{button_text}' was found.")
                    else:
                        raise ValueError("No inline layout keyboard signature found on the message node.")

                if do_dm:
                    message_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, message_text)

                if do_refer:
                    bot_username_target = str(parsed_target) if parsed_target else target
                    bot_username_target = bot_username_target.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
                    start_param = None
                    if "start=" in target:
                        param_match = re.search(r'start=([^&\s]+)', target)
                        if param_match:
                            start_param = param_match.group(1)
                    if "?" in bot_username_target:
                        bot_username_target = bot_username_target.split("?")[0]
                    await client.send_message(bot_username_target, f"/start {start_param}" if start_param else "/start")

                passed_ids.append(phone)
                
            except FloodWaitError as fwe:
                wait_time = fwe.seconds
                logger.warning(f"⚠️ Account +{phone} hit a FloodWait! Action requires {wait_time}s cooldown.")
                failed_ids.append((phone, f"FloodWaitError: Blocked for {wait_time}s"))
                backoff = min(wait_time, 20)
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.warning(f"Bridge +{phone} skipped task #{task_id}: {e}")
                failed_ids.append((phone, str(e)))
            finally:
                await client.disconnect()

            if (index + 1) % BATCH_SIZE == 0 and (index + 1) < total_accounts:
                batch_cooldown = BASE_COOLDOWN + random.randint(3, 8)
                await asyncio.sleep(batch_cooldown)
            else:
                await asyncio.sleep(random.uniform(1.5, 3.5))

            progress_pct = f"{int(((index + 1) / total_accounts) * 100)}%"
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_pct, task_id))
                await db.commit()

        status = "completed" if len(passed_ids) > 0 else "failed"
        success_report_json = json.dumps(passed_ids)
        failure_report_json = json.dumps(failed_ids)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute(
                "UPDATE tasks SET status = ?, progress = ?, success_report = ?, failure_report = ? WHERE task_id = ?",
                (status, f"{len(passed_ids)}/{total_accounts} Passed", success_report_json, failure_report_json, task_id)
            )
            await db.commit()

        await db_mgr.log_action(
            creator_id, 
            f"Finished executing task #{task_id} ({task_type.upper()}). Passed: {len(passed_ids)}, Failed: {len(failed_ids)}", 
            bot_instance
        )

task_queue = TaskQueue()

# --- FSM STATES ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()
    waiting_for_db_file = State()  # New state for database integration

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_target = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()

# --- UI KEYBOARD GENERATORS ---
REACTION_EMOJIS = ["👍", "👎", "🔥", "🎉", "👏", "🥰", "😮", "😢", "😡", "💩", "🤩", "🤔", "👀", "💯", "🤣"]

def get_emoji_selection_keyboard(selected_emojis: List[str], existing_reactions: Optional[List[str]] = None) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in REACTION_EMOJIS:
        is_selected = emoji in selected_emojis
        suffix = " ✅" if is_selected else ""
        if existing_reactions and emoji in existing_reactions:
            suffix += " (Existing)"
        btn_text = f"{emoji}{suffix}"
        row.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_emoji:{emoji}"))
        if len(row) == 3:  
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="✅ Done Select Emojis", callback_data="finish_emoji_selection")])
    keyboard.append([InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage Accounts", callback_data="manage_accounts")],
        [InlineKeyboardButton(text="⚡ Do Tasks", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 Tasks Report", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 My Referral Matrix", callback_data="view_referrals")],
        [InlineKeyboardButton(text="👨‍💻 Developer Attributions", callback_data="system_credits")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Administrative Control Console", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Core Backups", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 System Performance Analytics", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard(active_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="React Only", callback_data="set_type:react"),
            InlineKeyboardButton(text="Vote Only", callback_data="set_type:vote")
        ],
        [
            InlineKeyboardButton(text="React + Vote", callback_data="set_type:react_vote"),
            InlineKeyboardButton(text="View Only", callback_data="set_type:view")
        ],
        [
            InlineKeyboardButton(text="React + View", callback_data="set_type:react_view"),
            InlineKeyboardButton(text="Vote + View", callback_data="set_type:vote_view")
        ],
        [
            InlineKeyboardButton(text="React + Vote + View", callback_data="set_type:react_vote_view")
        ],
        [
            InlineKeyboardButton(text="Join Channel", callback_data="set_type:join"),
            InlineKeyboardButton(text="📄 Leave Channel", callback_data="set_type:leave")
        ],
        [
            InlineKeyboardButton(text="Bulk DM", callback_data="set_type:dm")
        ],
        [
            InlineKeyboardButton(text="🔗 Refer", callback_data="set_type:refer"),
            InlineKeyboardButton(text="⚡ Speed", callback_data="set_type:speed")
        ],
        [
            InlineKeyboardButton(text="Cancel", callback_data="main_menu")
        ]
    ])

# --- ROUTER REGISTER ---
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    referred_by = None
    if len(message.text.split()) > 1:
        ref_payload = message.text.split()[1]
        if ref_payload.startswith("ref_") and ref_payload[4:].isdigit():
            referred_by = int(ref_payload[4:])
            if referred_by == user_id:
                referred_by = None

    await db_mgr.create_user_if_not_exists(user_id, username, referred_by)
    role = await db_mgr.get_user_role(user_id)
    await db_mgr.log_action(user_id, f"Involved command `/start`", bot)

    welcome_text = (
        f"👋 **Main Control Console**\n"
        f"Select an action vector below:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await db_mgr.log_action(callback.from_user.id, "Returned to Main Menu", bot)
    await callback.message.edit_text(
        "👋 **Main Control Console**\nSelect an action vector below:",
        reply_markup=get_main_keyboard(role)
    )

# --- GLOBAL EMERGENCY TASK CANCELLATION COMMAND ---
@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    await message.answer("🛑 **Emergency Panic Instruction Received. Halting pipeline...**")
    killed_count = await task_queue.cancel_all_active_tasks()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE tasks SET status = 'cancelled' WHERE status = 'pending' OR status = 'running'")
        await db.commit()
    await message.answer(f"✅ **Pipeline Terminated!** Killed `{killed_count}` threads.")

# --- DEVELOPER ATTRIBUTIONS ROUTING PANEL ---
@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    credits_text = (
        "👨‍💻 **Core Engineering Team attributions**\n\n"
        f"🎨 **Lead Architect & Designer:** @{config.DESIGNER_HANDLE}\n"
        f"⚙️ **Operations & System Manager:** @{config.MANAGER_HANDLE}\n\n"
        "🛠️ Built safely for modular high-performance execution."
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")]]
    await callback.message.edit_text(text=credits_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- ACCOUNT INFRASTRUCTURE DEPLOYMENT ---
@router.callback_query(F.data == "manage_accounts")
async def list_user_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    try:
        await callback.answer() 
        role = await db_mgr.get_user_role(user_id)
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                cursor = await db.execute("SELECT phone, status, username FROM accounts")
            else:
                cursor = await db.execute("SELECT phone, status, username FROM accounts WHERE user_id = ?", (user_id,))
            rows = await cursor.fetchall()

        text = "📱 **Operational Channels Infrastructure**\n\n"
        if not rows:
            text += "_No automation profiles currently linked._"
        else:
            for row in rows:
                icon = "🟢" if row[1] == "active" else "🔴"
                text += f"{icon} `+{row[0]}` (@{row[2] or 'N/A'}) - **{row[1].upper()}**\n"

        buttons = [
            [InlineKeyboardButton(text="➕ Link via OTP", callback_data="add_account_phone"),
             InlineKeyboardButton(text="📁 Link String / File", callback_data="add_account_session")],
            [InlineKeyboardButton(text="📥 Export Session (.txt)", callback_data="select_export_session"),
             InlineKeyboardButton(text="📦 Bulk Admin Export", callback_data="bulk_admin_export")],
            [InlineKeyboardButton(text="💥 Delete All Dead Logs", callback_data="purge_dead_accounts")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ]
        await callback.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        await callback.message.answer(f"⚠️ **Core UI pipeline error**: {e}")

@router.callback_query(F.data == "purge_dead_accounts")
async def handle_purge_dead_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("DELETE FROM accounts WHERE status = 'dead'")
        else:
            cursor = await db.execute("DELETE FROM accounts WHERE status = 'dead' AND user_id = ?", (user_id,))
        changes = db.total_changes
        await db.commit()
    await callback.answer(f"🔥 Successfully purged {changes} logs!", show_alert=True)
    await list_user_accounts(callback, bot)

# --- LINK NEW ACCOUNT VIA OTP ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📞 Enter phone number with country code (e.g. `+123456789`):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext, bot: Bot):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {"client": client, "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        await message.answer("📩 Input the verification code:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    otp = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id, bot)
    except SessionPasswordNeededError:
        await message.answer("🔒 Enter 2FA Password:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ Failed: {str(e)}")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    password = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await state.clear()
        return
    try:
        await reg_data["client"].sign_in(password=password)
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], user_id, bot)
    except Exception as e:
        await message.answer(f"❌ Auth Error: {str(e)}")
        await reg_data["client"].disconnect()
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int, bot: Bot):
    try:
        me = await client.get_me()
        encrypted_session = encrypt_data(client.session.save())
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", ""), user_id, me.username or "None", encrypted_session))
            await db.commit()
        await message.answer(f"🎉 Verified account `+{phone}` successfully.")
    except Exception as e:
        await message.answer(f"❌ Reg failure: {str(e)}")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- LINK VIA STRING SESSION OR STR FILE ---
@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📁 Upload session text/file:")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    session_str = ""
    if message.document:
        file_info = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        session_str = file_bytes.read().decode('utf-8', errors='ignore').strip()
    elif message.text:
        session_str = message.text.strip()

    if not session_str or len(session_str) < 20:
        await message.answer("❌ Invalid data entry.")
        await state.clear()
        return

    try:
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await message.answer("❌ Session revoked/expired.")
            await client.disconnect()
            await state.clear()
            return
            
        me = await client.get_me()
        phone = me.phone or f"custom_{me.id}"
        encrypted_session = encrypt_data(session_str)
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", ""), user_id, me.username or "None", encrypted_session))
            await db.commit()

        await message.answer(f"🎉 Integrated `+{phone}` successfully.")
        await client.disconnect()
    except Exception as e:
        await message.answer(f"❌ Failure: {e}")
    finally:
        await state.clear()

# --- EXPORT & BACKUPS ENGINE ---
@router.callback_query(F.data == "select_export_session")
async def select_export_session_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("⚠️ No active verified bridges to export.")
        return

    text = "📥 Select active connection profile:"
    buttons = [[InlineKeyboardButton(text=f"+{r[0]}", callback_data=f"export_ph:{r[0]}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="manage_accounts")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("export_ph:"))
async def handle_export_session_run(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    phone = callback.data.split(":")[1]
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT user_id, session_string, username FROM accounts WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()

    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != user_id):
        await callback.message.answer("🚫 Unauthorized/Not found.")
        return

    session_bytes = decrypt_data(row[1]).encode('utf-8')
    session_file = BufferedInputFile(session_bytes, filename=f"string_{phone}.txt")
    await callback.message.reply_document(document=session_file, caption=f"📱 Phone: `+{phone}`")

@router.callback_query(F.data == "bulk_admin_export")
async def handle_bulk_admin_export(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await callback.message.answer("🚫 Access Denied.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("SELECT phone, user_id, username, session_string FROM accounts" if role == "super_owner" else "SELECT phone, user_id, username, session_string FROM accounts WHERE user_id = ?", (user_id,))
        rows = await cursor.fetchall()

    export_payload = [{"phone": r[0], "user_id": r[1], "username": r[2], "session_string": decrypt_data(r[3])} for r in rows]
    backup_file = BufferedInputFile(json.dumps(export_payload, indent=4).encode('utf-8'), filename="accounts_backup.txt")
    await callback.message.reply_document(document=backup_file, caption=f"📦 Exported `{len(export_payload)}` items.")

# --- DYNAMIC DB EXTERNAL IMPORT SYSTEM (.DB INTERFACE) ---
@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="📥 Download Raw DB Image", callback_data="export_db")],
        [InlineKeyboardButton(text="📂 Import External DB (.db)", callback_data="import_db_start")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]
    ]
    await callback.message.edit_text("💾 **Database Core Management Hub**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "import_db_start")
async def import_db_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Only developers/owners can import database frameworks directly.")
        return
        
    await callback.message.edit_text("📤 Please send/upload the target raw structural `.db` file containing user mapping or connection accounts data:")
    await state.set_state(RegistrationStates.waiting_for_db_file)

@router.message(StateFilter(RegistrationStates.waiting_for_db_file), F.document)
async def process_db_import_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    
    if not message.document.file_name.endswith('.db'):
        await message.answer("❌ Invalid structure context. File must have a `.db` format string extension identifier.")
        await state.clear()
        return
        
    status_msg = await message.answer("⚡ Downloading raw database container...")
    temp_filename = f"imported_temp_{user_id}.db"
    
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=temp_filename)
        
        await status_msg.edit_text("🔄 Processing matrix records and merging structures safely...")
        
        users_merged = 0
        accounts_merged = 0
        
        async with aiosqlite.connect(temp_filename) as source_db:
            # 1. Parse and migrate global profiles if existing
            try:
                async with source_db.execute("SELECT user_id, username, role, max_accounts FROM users") as cursor:
                    async for row in cursor:
                        async with aiosqlite.connect(db_mgr.db_path) as current_db:
                            await current_db.execute("""
                                INSERT OR IGNORE INTO users (user_id, username, role, max_accounts)
                                VALUES (?, ?, ?, ?)
                            """, (row[0], row[1], row[2], row[3]))
                            await current_db.commit()
                        users_merged += 1
            except Exception as users_err:
                logger.warning(f"Skipping profiles parsing step: {users_err}")

            # 2. Parse and merge absolute account bridge tokens
            try:
                async with source_db.execute("SELECT phone, user_id, username, session_string, status FROM accounts") as cursor:
                    async for row in cursor:
                        async with aiosqlite.connect(db_mgr.db_path) as current_db:
                            await current_db.execute("""
                                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                            """, (str(row[0]).replace("+", ""), row[1], row[2], row[3], row[4]))
                            await current_db.commit()
                        accounts_merged += 1
            except Exception as accounts_err:
                await status_msg.edit_text(f"❌ Failed to parse data from accounts layout node scheme: {accounts_err}")
                return

        await status_msg.edit_text(
            f"✅ **Database Integration Matrix Complete!**\n\n"
            f"👤 Profile instances loaded: `{users_merged}`\n"
            f"📱 Connected session bridges successfully synchronized: `{accounts_merged}`"
        )
        await db_mgr.log_action(user_id, f"Merged external storage image snapshot successfully. Accounts appended: {accounts_merged}", bot)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Structural schema validation fault: {e}")
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        await state.clear()

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        with open(db_mgr.db_path, "rb") as f:
            file = BufferedInputFile(f.read(), filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 SQLite Snapshot")
    except Exception as e:
        await callback.message.answer(f"❌ Error: {e}")

# --- TASK BUILDER WIZARD INTERFACE ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        active_count = (await cursor.fetchone())[0]

    wizard_text = (
        f"🚀 **Adv Campaign**\n"
        f"----------------------------------------\n"
        f"📱 `{active_count} active account(s) available.`\n\n"
        f"Step 1 — _Choose what your accounts should do:_"
    )
    await callback.message.edit_text(text=wizard_text, reply_markup=get_task_types_keyboard(active_count))
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    if "react" in task_type or "vote" in task_type or task_type in ["view", "speed"]:
        await callback.message.edit_text("🔗 **Provide Post/Message Link:**\nPaste the link pointing to the target message:")
    elif task_type == "refer":
        await callback.message.edit_text("🤖 **Enter Target Bot Link or Username:**\nSend the bot link or referral URL parameters:")
    else:
        await callback.message.edit_text("🔗 **Enter Target Resource:**\nProvide the Username, Public Link, or Private Join Link:")
    await state.set_state(TaskWizardStates.waiting_for_target)

@router.message(StateFilter(TaskWizardStates.waiting_for_target))
async def task_hub_process_target(message: Message, state: FSMContext, bot: Bot):
    target = message.text.strip()
    await state.update_data(target=target)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if task_type in ["join", "leave", "refer", "view", "speed"]:
        await finalize_task_creation(message, state, bot)
    elif "react" in task_type:
        await state.update_data(selected_emojis=[], existing_reactions=[])
        await message.answer(
            "🎭 **Choose Emojis to Distribute:**\nSelect destination reaction expressions below:",
            reply_markup=get_emoji_selection_keyboard([], [])
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        await message.answer("🔘 **Enter Button Text/Emoji:**\nType the exact button text label you want to tap inside the inline layout markup:")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    elif task_type == "dm":
        await message.answer("📝 **Message Body Content:** Enter the text string to dispatch to the target:")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data.startswith("toggle_emoji:"))
async def handle_toggle_emoji(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    emoji = callback.data.split(":")[1]
    data = await state.get_data()
    selected = data.get("selected_emojis", [])
    if emoji in selected:
        selected.remove(emoji)
    else:
        selected.append(emoji)
    await state.update_data(selected_emojis=selected)
    await callback.message.edit_reply_markup(reply_markup=get_emoji_selection_keyboard(selected, []))

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data == "finish_emoji_selection")
async def finish_emoji_selection(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    selected = data.get("selected_emojis", [])
    if not selected:
        await callback.answer("⚠️ Please select at least one emoji!", show_alert=True)
        return
    await callback.answer()
    await state.update_data(reactions=selected)
    await callback.message.delete()
    
    task_type = data.get("task_type")
    if "vote" in task_type:
        await callback.message.answer("🔘 **Enter Button Text/Emoji:**\nType the exact button text label you want to click:")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    else:
        await finalize_task_creation(callback.message, state, bot)

@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(button_text=message.text.strip())
    await finalize_task_creation(message, state, bot)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(text=message.text.strip())
    await finalize_task_creation(message, state, bot)

async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    target = data.get("target", "")
    _, link_msg_id = parse_telegram_link(target)
    if link_msg_id:
        data["msg_id"] = link_msg_id

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)", (user_id, task_type, json.dumps(data)))
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot)
    response_msg = f"🚀 **Task #{task_id} successfully queued!**\n⚙️ **Type:** `{task_type.upper()}`\nUse `/taskreport_{task_id}` to check results."
    
    if isinstance(message, Message):
        await message.answer(response_msg)
    else:
        await message.answer(response_msg)
    await state.clear()

# --- REPORTS, STATS & ADMINISTRATIVE CHANNELS INTERFACES ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10" if role in ["admin", "owner", "super_owner"] else "SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10", (user_id,))
        rows = await cursor.fetchall()

    text = "📊 **Recent Operations Pipeline Log**\n\n"
    for r in rows:
        text += f"🔹 **Task #{r[0]}** ({r[1].upper()})\nStatus: `{r[2]}` | Metrics: `{r[3]}`\n↳ Details: /taskreport_{r[0]}\n\n"
    await callback.message.edit_text(text or "Queue empty.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    try:
        task_id = int(message.text.split("_")[1])
    except:
        return
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress, success_report, failure_report, payload FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != user_id):
        await message.answer("🚫 Access Denied/Not found.")
        return

    report_text = f"📊 **Task #{task_id} Diagnostics**\n⚙️ Type: `{row[1].upper()}`\n🚦 State: `{row[2]}`\n📈 Metrics: `{row[3]}`"
    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")]]))

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
    await callback.message.edit_text(f"👥 **Referral Network Matrix**\n\n🔗 Link:\n`https://t.me/{bot_username}?start=ref_{user_id}`\n\n📈 Nodes: `{count}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await callback.message.edit_text("🛠️ **Administrative Control Console**\nCommands:\n/addadmin <id> <limit>\n/removeadmin <id>\n/canceltasks", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        total_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
        active_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
    await callback.message.edit_text(f"📈 **System Analytics**\n👥 Users: `{total_users}`\n📱 Automation Sessions: `{total_accounts}`\n🟢 Active Bridge Connections: `{active_accounts}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

# --- BOOTSTRAPPING RUNTIME ---
async def verify_saved_sessions():
    logger.info("Running verification pings across system bridges...")
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'") as cursor:
            accounts = await cursor.fetchall()
    for phone, enc_session in accounts:
        try:
            client = TelegramClient(StringSession(decrypt_data(enc_session)), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                    await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_conn.commit()
            await client.disconnect()
        except:
            pass

async def main():
    global bot_username
    await db_mgr.init()
    await verify_saved_sessions()
    if not config.BOT_TOKEN:
        return
    bot = Bot(token=config.BOT_TOKEN)
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    worker_task = asyncio.create_task(task_queue.start_worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process execution halted cleanly.")
