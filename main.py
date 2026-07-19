import asyncio
import base64
import json
import os
import re
import random
import time
import math
from typing import Dict, Any, List, Optional, Tuple

# aiogram 3.x imports
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter, CommandObject
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

def make_progress_bar(pct: float, length: int = 15) -> str:
    filled = int(round(length * (pct / 100.0)))
    return "░" * filled + " " * (length - filled)

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

    async def log_action(self, user_id: int, action: str, bot_instance: Optional[Bot] = None, operational: bool = False):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
                await db.commit()
        except Exception as db_err:
            logger.error(f"Failed to log action: {db_err}")
        
        if operational and bot_instance and config.LOG_CHANNEL_ID:
            try:
                log_text = (
                    f"📝 System Log Update\n"
                    f"User ID: `{user_id}`\n"
                    f"Action executed: {action}"
                )
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=log_text)
            except Exception as e:
                logger.error(f"Failed sending log channel updates: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in config.SUPER_OWNER_IDS:
            return "super_owner"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else "user"

    async def get_admin_limits(self, user_id: int) -> int:
        async with aiosqlite.connect(db_mgr.db_path) as db:
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

# --- CONCURRENT TASK MANAGER ENGINE ---
class TaskQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_tasks: Dict[int, asyncio.Task] = {}

    async def add_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot, status_msg_id: int):
        await self.queue.put((task_id, creator_id, task_type, payload, bot_instance, status_msg_id))

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
                        "UPDATE tasks SET status = 'cancelled', progress = 'Stopped by admin' WHERE task_id = ?", 
                        (t_id,)
                    )
                    await db.commit()
        return count

    async def start_worker(self):
        logger.info("Task runner loop started.")
        while True:
            try:
                task_id, creator_id, task_type, payload, bot_instance, status_msg_id = await self.queue.get()
            except asyncio.CancelledError:
                break
                
            loop_task = asyncio.create_task(self.execute_task(task_id, creator_id, task_type, payload, bot_instance, status_msg_id))
            self.current_tasks[task_id] = loop_task
            try:
                await loop_task
            except asyncio.CancelledError:
                logger.warning(f"Task #{task_id} was stopped.")
            except Exception as e:
                logger.error(f"Error on task #{task_id}: {e}")
            finally:
                self.current_tasks.pop(task_id, None)
                self.queue.task_done()

    async def execute_task(self, task_id: int, creator_id: int, task_type: str, payload: dict, bot_instance: Bot, status_msg_id: int):
        start_time = time.time()
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("UPDATE tasks SET status = 'running', progress = '0%' WHERE task_id = ?", (task_id,))
            await db.commit()

        role = await db_mgr.get_user_role(creator_id)
        clients_data = []
        requested_count = int(payload.get("run_account_count", 0))
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active'"
                cursor = await db.execute(query)
            else:
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
                cursor = await db.execute(query, (creator_id,))
            
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if requested_count > 0:
            clients_data = clients_data[:requested_count]

        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = 'No accounts found' WHERE task_id = ?", (task_id,))
                await db.commit()
            try:
                await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text="❌ Task failed: You do not have any active accounts connected.")
            except Exception:
                pass
            return

        passed_ids: List[str] = []
        failed_ids: List[Tuple[str, str]] = []
        total_accounts = len(clients_data)
        
        semaphore = asyncio.Semaphore(10) 
        progress_counter = 0
        success_counter = 0
        failure_counter = 0
        last_ui_update = 0

        async def worker_session(phone: str, enc_session: str, idx: int):
            nonlocal progress_counter, success_counter, failure_counter, last_ui_update
            async with semaphore:
                client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
                try:
                    await asyncio.sleep(random.uniform(0.1, 0.4))
                    await client.connect()
                    if not await client.is_user_authorized():
                        async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                            await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                            await db_conn.commit()
                        failed_ids.append((phone, "Session key expired"))
                        failure_counter += 1
                        return

                    target = payload.get("target", "")
                    channel_target = payload.get("channel_target", target)
                    
                    do_leave_all = (task_type == "leave" and payload.get("leave_mode") == "all")

                    parsed_target, link_msg_id = parse_telegram_link(target) if not do_leave_all else (None, None)
                    parsed_channel, _ = parse_telegram_link(channel_target) if not do_leave_all else (None, None)
                    msg_id = int(payload.get("msg_id", link_msg_id or 0))

                    do_react = "react" in task_type
                    do_vote = "vote" in task_type
                    do_view = "view" in task_type or task_type == "speed"
                    do_join = (task_type == "join" or do_react or do_vote or do_view) and not do_leave_all
                    do_leave = task_type == "leave"
                    do_dm = task_type == "dm"
                    do_refer = task_type == "refer"

                    if do_join:
                        try:
                            if isinstance(parsed_channel, str) and ("/+" in channel_target or "joinchat/" in channel_target or channel_target.startswith("+")):
                                await client(functions.messages.ImportChatInviteRequest(hash=parsed_channel))
                            else:
                                await client(functions.channels.JoinChannelRequest(channel=parsed_channel or parsed_target))
                        except Exception as join_err:
                            failed_ids.append((phone, f"Failed to join: {str(join_err)}"))
                            failure_counter += 1
                            return

                    if do_view and msg_id:
                        try:
                            await client(functions.messages.GetMessagesViewsRequest(peer=parsed_target, id=[msg_id], increment=True))
                        except Exception as view_err:
                            failed_ids.append((phone, f"View error: {str(view_err)}"))
                            failure_counter += 1
                            return

                    if do_react and msg_id:
                        try:
                            emojis = payload.get("reactions", ["👍"])
                            assigned_emoji = emojis[idx % len(emojis)]
                            await client(functions.messages.SendReactionRequest(
                                peer=parsed_target, msg_id=msg_id, reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                              ))
                        except Exception as react_err:
                            failed_ids.append((phone, f"Reaction error: {str(react_err)}"))
                            failure_counter += 1
                            return

                    if do_vote and msg_id:
                        try:
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
                                    await client(functions.messages.GetBotCallbackAnswerRequest(peer=parsed_target, msg_id=msg_id, data=target_button.data))
                                else:
                                    raise ValueError("Poll option button not found.")
                            else:
                                raise ValueError("Message has no vote buttons.")
                        except Exception as vote_err:
                            failed_ids.append((phone, f"Vote error: {str(vote_err)}"))
                            failure_counter += 1
                            return

                    if do_dm:
                        try:
                            await client.send_message(parsed_target, payload.get("text", "Hello!"))
                        except Exception as dm_err:
                            failed_ids.append((phone, f"DM error: {str(dm_err)}"))
                            failure_counter += 1
                            return

                    if do_refer:
                        try:
                            bot_username_target = str(parsed_target).replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
                            start_param = None
                            if "start=" in target:
                                param_match = re.search(r'start=([^&\s]+)', target)
                                if param_match:
                                    start_param = param_match.group(1)
                            if "?" in bot_username_target:
                                bot_username_target = bot_username_target.split("?")[0]
                            await client.send_message(bot_username_target, f"/start {start_param}" if start_param else "/start")
                        except Exception as ref_err:
                            failed_ids.append((phone, f"Referral link error: {str(ref_err)}"))
                            failure_counter += 1
                            return

                    if do_leave:
                        if do_leave_all:
                            left_chats_count = 0
                            async for dialog in client.iter_dialogs():
                                if dialog.is_channel or dialog.is_group:
                                    try:
                                        # Use the explicit entity tracking object instead of raw payload wrappers
                                        await client(functions.channels.LeaveChannelRequest(channel=dialog.entity))
                                        left_chats_count += 1
                                        await asyncio.sleep(0.3)
                                    except FloodWaitError as fwe:
                                        await asyncio.sleep(fwe.seconds)
                                    except Exception:
                                        pass
                            if left_chats_count == 0:
                                failed_ids.append((phone, "Account was not in any channels"))
                                failure_counter += 1
                                return
                        else:
                            try:
                                await client(functions.channels.LeaveChannelRequest(channel=parsed_target))
                            except Exception as leave_err:
                                failed_ids.append((phone, f"Leave error: {str(leave_err)}"))
                                failure_counter += 1
                                return

                    passed_ids.append(phone)
                    success_counter += 1
                    
                except Exception as general_err:
                    failed_ids.append((phone, str(general_err)))
                    failure_counter += 1
                finally:
                    await client.disconnect()
                    progress_counter += 1
                    
                    current_now = time.time()
                    if current_now - last_ui_update >= 2.5 or progress_counter == total_accounts:
                        last_ui_update = current_now
                        pct_val = (progress_counter / total_accounts) * 100
                        elapsed = current_now - start_time
                        avg_time = elapsed / progress_counter if progress_counter > 0 else 0
                        remaining = (total_accounts - progress_counter) * avg_time
                        
                        eta_str = f"~{int(remaining // 60)}m {int(remaining % 60)}s" if remaining > 0 else "0s"
                        progress_pct = f"{int(pct_val)}%"
                        
                        live_text = (
                            f"⏳ Task running status...\n\n"
                            f"[{make_progress_bar(pct_val)}] {progress_pct}\n"
                            f"📊 `{progress_counter}/{total_accounts}` accounts complete\n"
                            f"✅ Done: `{success_counter}` | ❌ Failed: `{failure_counter}`\n"
                            f"⏱ Time remaining: {eta_str}"
                        )
                        try:
                            await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text=live_text)
                        except Exception:
                            pass

                        async with aiosqlite.connect(db_mgr.db_path) as db_update:
                            await db_update.execute("UPDATE tasks SET progress = ? WHERE task_id = ?", (progress_pct, task_id))
                            await db_update.commit()

        await asyncio.gather(*(worker_session(phone, enc, i) for i, (phone, enc) in enumerate(clients_data)))

        end_time = time.time()
        elapsed_total = end_time - start_time
        duration_str = f"{int(elapsed_total // 60)}m {int(elapsed_total % 60)}s"

        status = "completed" if len(passed_ids) > 0 else "failed"
        success_report_json = json.dumps(passed_ids)
        failure_report_json = json.dumps(failed_ids)

        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute(
                "UPDATE tasks SET status = ?, progress = ?, success_report = ?, failure_report = ? WHERE task_id = ?",
                (status, f"{len(passed_ids)}/{total_accounts} Passed", success_report_json, failure_report_json, task_id)
            )
            await db.commit()

        success_pct_final = int((success_counter / total_accounts) * 100) if total_accounts > 0 else 0
        campaign_uuid = base64.b64encode(f"CAMP_{task_id}".encode()).decode().lower()[:24]
        
        user_info = f"`{creator_id}`"
        try:
            chat_member = await bot_instance.get_chat(creator_id)
            if chat_member.first_name:
                user_info = f"{chat_member.first_name} (`{creator_id}`)"
        except Exception:
            pass

        target_display = "ALL CHANNELS DEPLOYMENT" if payload.get("leave_mode") == "all" else f"`{payload.get('target', 'N/A')}`"

        # Build detailed failure logs if any accounts failed
        failure_log_details = ""
        if failed_ids:
            failure_log_details = "\n\n❌ **Detailed Error Reports (Why IDs Failed):**\n"
            for phone_num, reason in failed_ids:
                failure_log_details += f"• `+{phone_num}` ➜ `{reason}`\n"

        completion_card = (
            f"⚡ **Task Management Card**\n\n"
            f"📋 Task ID: `{campaign_uuid}`\n"
            f"⚡ Action Code: `{task_type.upper()}`\n"
            f"👤 Creator Profile: {user_info}\n"
            f"🔗 Target Location: {target_display}\n"
            f"📢 Secondary Target: `{payload.get('channel_target', 'N/A')}`\n\n"
            f"📊 **Performance Reports:**\n"
            f"✅ Success Rate: `{success_counter}/{total_accounts}` ({success_pct_final}%)\n"
            f"❌ Total Failures: `{failure_counter}/{total_accounts}`\n"
            f"⏱ Total Run Time: {duration_str}"
            f"{failure_log_details}"
        )

        try:
            await bot_instance.send_message(chat_id=creator_id, text=completion_card)
        except Exception:
            pass

        if config.LOG_CHANNEL_ID:
            try:
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=completion_card)
            except Exception as le:
                logger.error(f"Failed sending validation report to log channel: {le}")

task_queue = TaskQueue()

# --- FSM STATES ---
class RegistrationStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_otp = State()
    waiting_for_2fa = State()
    waiting_for_session_file = State()
    waiting_for_db_file = State()

class TaskWizardStates(StatesGroup):
    choosing_type = State()
    waiting_for_leave_choice = State()
    waiting_for_channel_link = State()
    waiting_for_post_link = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()
    waiting_for_account_scale = State()

class ExportWizardStates(StatesGroup):
    selecting_multi = State()

class BroadcastStates(StatesGroup):
    waiting_for_msg = State()

# --- UI KEYBOARD GENERATORS ---
REACTION_EMOJIS = [
    "🔥", "❤️", "💖", "💘", "💝",
    "👍", "👏", "🎉", "🤩", "💯",
    "⚡", "🍓", "💋", "🍿", "🏆",
    "🤣", "🥰", "🤔", "👀", "😎"
]

def get_post_registration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Another Account", callback_data="add_account_phone")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]
    ])

def get_emoji_selection_keyboard(selected_emojis: List[str]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in REACTION_EMOJIS:
        is_selected = emoji in selected_emojis
        suffix = " ✅" if is_selected else ""
        row.append(InlineKeyboardButton(text=f"{emoji}{suffix}", callback_data=f"toggle_emoji:{emoji}"))
        if len(row) == 5:  
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="✅ Done Selecting Emojis", callback_data="finish_emoji_selection")])
    keyboard.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage My Accounts", callback_data="manage_accounts:0")],
        [InlineKeyboardButton(text="⚡ Start Tasks", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 View Tasks Report", callback_data="view_tasks")],
        [InlineKeyboardButton(text="👥 My Referral Link", callback_data="view_referrals")],
        [InlineKeyboardButton(text="👨‍💻 Credits", callback_data="system_credits")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛠️ Admin Panel", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Backups", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 System Statistics", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard(active_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Reaction Only", callback_data="set_type:react"), InlineKeyboardButton(text="Vote Only", callback_data="set_type:vote")],
        [InlineKeyboardButton(text="Reaction + Vote", callback_data="set_type:react_vote"), InlineKeyboardButton(text="View Only", callback_data="set_type:view")],
        [InlineKeyboardButton(text="Reaction + View", callback_data="set_type:react_view"), InlineKeyboardButton(text="Vote + View", callback_data="set_type:vote_view")],
        [InlineKeyboardButton(text="React + Vote + View", callback_data="set_type:react_vote_view")],
        [InlineKeyboardButton(text="Join Channel", callback_data="set_type:join"), InlineKeyboardButton(text="📄 Leave Channel Module", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="Bulk DM", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔗 Referral Bot", callback_data="set_type:refer"), InlineKeyboardButton(text="⚡ Fast Speed Views", callback_data="set_type:speed")],
        [InlineKeyboardButton(text="Cancel", callback_data="main_menu")]
    ])

def get_leave_channel_options_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Leave 1 Single Channel via Link", callback_data="leave_mode:single")],
        [InlineKeyboardButton(text="💥 Leave ALL Joined Channels everywhere", callback_data="leave_mode:all")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="task_hub_start")]
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
    await db_mgr.log_action(user_id, "Started the bot", bot, operational=False)

    welcome_text = "Welcome to the Main Menu!\nPlease select what you want to do below:"
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role))

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text(
        "Welcome to the Main Menu!\nPlease select what you want to do below:",
        reply_markup=get_main_keyboard(role)
    )

@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 You do not have permission to run this command.")
        return

    await message.answer("Stopping all active running tasks now...")
    killed_count = await task_queue.cancel_all_active_tasks()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE tasks SET status = 'cancelled' WHERE status = 'pending' OR status = 'running'")
        await db.commit()
    await message.answer(f"✅ Finished! Cancelled `{killed_count}` ongoing tasks.")

# --- LIVE ADMINISTRATIVE MANAGEMENT HOOKS ---
@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 You do not have permission to run this command.")
        return
        
    args = command.args
    if not args or len(args.split()) < 2:
        await message.answer("Use format layout: `/addadmin <user_id> <account_limit>`")
        return
        
    target_id_str, limit_str = args.split()[:2]
    if not target_id_str.isdigit() or not limit_str.isdigit():
        await message.answer("❌ Please supply integer numbers only.")
        return
        
    target_id = int(target_id_str)
    limit_val = int(limit_str)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute(
            "INSERT INTO users (user_id, role, max_accounts) VALUES (?, 'admin', ?) ON CONFLICT(user_id) DO UPDATE SET role='admin', max_accounts=?",
            (target_id, limit_val, limit_val)
        )
        await db.commit()
        
    await message.answer(f"✅ User ID `{target_id}` was promoted to Admin status with an account threshold limit of `{limit_val}`.")
    await db_mgr.log_action(user_id, f"Made user {target_id} an Admin (limit={limit_val})", bot, operational=True)

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 You do not have permission to run this command.")
        return
        
    target_id_str = command.args
    if not target_id_str or not target_id_str.strip().isdigit():
        await message.answer("Use format layout: `/removeadmin <user_id>`")
        return
        
    target_id = int(target_id_str.strip())
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role='user' WHERE user_id = ?", (target_id,))
        await db.commit()
        
    await message.answer(f"✅ Revoked admin access authorizations from user ID `{target_id}`.")
    await db_mgr.log_action(user_id, f"Removed Admin role from user {target_id}", bot, operational=True)

# --- BROADCAST SYSTEM WORKFLOW ---
@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("🚫 You do not have permission to run this command.")
        return
        
    await message.answer("📢 Send out the text description or multimedia data content you want to broadcast to everyone:")
    await state.set_state(BroadcastStates.waiting_for_msg)

@router.message(StateFilter(BroadcastStates.waiting_for_msg))
async def process_broadcast_push(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    status_msg = await message.answer("🚀 Dispatching network announcement out to active members...")
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        rows = await cursor.fetchall()
        
    success_hits = 0
    failed_hits = 0
    
    for r in rows:
        target_uid = r[0]
        try:
            await bot.copy_message(chat_id=target_uid, from_chat_id=message.chat.id, message_id=message.message_id)
            success_hits += 1
            await asyncio.sleep(0.05)  
        except Exception:
            failed_hits += 1
            
    await status_msg.edit_text(
        f"📢 Broadcast Delivery Complete!\n\n"
        f"✅ Dispatched successfully to: `{success_hits}` profiles\n"
        f"❌ Dead accounts or blocks detected: `{failed_hits}` users"
    )

@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    credits_text = (
        "Lead Developer Team Info\n\n"
        f"🎨 Design Concepts Architect: `@{config.DESIGNER_HANDLE}`\n"
        f"⚙️ Operational System Manager: `@{config.MANAGER_HANDLE}`\n\n"
        "Thank you for using our account manager utilities suite!"
    )
    buttons = [[InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]]
    await callback.message.edit_text(text=credits_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- PAGINATED ACCOUNTS VIEW (10 PER PAGE) ---
@router.callback_query(F.data.startswith("manage_accounts:"))
async def list_user_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    limit = 10
    offset = page * limit
    
    try:
        await callback.answer() 
        role = await db_mgr.get_user_role(user_id)
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role in ["admin", "owner", "super_owner"]:
                count_query = "SELECT COUNT(*) FROM accounts"
                cursor_count = await db.execute(count_query)
                total_items = (await cursor_count.fetchone())[0]
                
                query = "SELECT phone, status, username FROM accounts LIMIT ? OFFSET ?"
                cursor = await db.execute(query, (limit, offset))
            else:
                count_query = "SELECT COUNT(*) FROM accounts WHERE user_id = ?"
                cursor_count = await db.execute(count_query, (user_id,))
                total_items = (await cursor_count.fetchone())[0]
                
                query = "SELECT phone, status, username FROM accounts WHERE user_id = ? LIMIT ? OFFSET ?"
                cursor = await db.execute(query, (user_id, limit, offset))
            rows = await cursor.fetchall()

        text = f"📱 Registered Accounts List (Page {page + 1})\n"
        text += f"Total managed database profiles: `{total_items}`\n\n"
        
        if not rows:
            text += "_No linked sessions detected inside this page index._"
        else:
            for row in rows:
                icon = "🟢" if row[1] == "active" else "🔴"
                text += f"{icon} `+{row[0]}` (`@{row[2] or 'None'}`) - `{row[1].upper()}`\n"

        buttons = [
            [InlineKeyboardButton(text="➕ Add via Code OTP", callback_data="add_account_phone"),
             InlineKeyboardButton(text="📁 Upload Session File", callback_data="add_account_session")],
            [InlineKeyboardButton(text="📥 Open Export Menu", callback_data="export_dashboard_root")],
            [InlineKeyboardButton(text="💥 Delete All Dead Accounts", callback_data=f"purge_dead_accounts:{page}")]
        ]
        
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"manage_accounts:{page - 1}"))
        if offset + limit < total_items:
            nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"manage_accounts:{page + 1}"))
        
        if nav_row:
            buttons.append(nav_row)
            
        buttons.append([InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")])
        await callback.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        await callback.message.answer(f"⚠️ Problem displaying phone profiles list: {e}")

@router.callback_query(F.data.startswith("purge_dead_accounts:"))
async def handle_purge_dead_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            await db.execute("DELETE FROM accounts WHERE status = 'dead'")
        else:
            await db.execute("DELETE FROM accounts WHERE status = 'dead' AND user_id = ?", (user_id,))
        await db.commit()
    await callback.answer("Deleted all disconnected dead profile sessions!", show_alert=True)
    
    callback.data = f"manage_accounts:{page}"
    await list_user_accounts(callback, bot)

# --- LINK NEW ACCOUNT VIA OTP ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("Type your phone number with country prefix code (Example: `+123456789`):")
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
        await message.answer("📩 Enter the login OTP verification code sent to your account profile:")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ Error triggered: {str(e)}")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    otp = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session context dropped. Please re-run initialization workflow setup.")
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id, bot)
    except PhoneCodeInvalidError:
        await message.answer("❌ The login OTP key entered was invalid. Please double check and retype:")
    except SessionPasswordNeededError:
        await message.answer("🔒 Two-Factor security lock active on profile. Please enter your 2FA password text:")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ Login authentication sequence failed: {str(e)}")
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
        await message.answer(f"❌ Password check error reported: {str(e)}")
        await reg_data["client"].disconnect()
        await state.clear()

async def complete_registration(message: Message, state: FSMContext, client: TelegramClient, phone: str, user_id: int, bot: Bot):
    try:
        me = await client.get_me()
        raw_session_str = client.session.save()
        encrypted_session = encrypt_data(raw_session_str)
        async with aiosqlite.connect(db_mgr.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO accounts (phone, user_id, username, session_string, status, last_active)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """, (phone.replace("+", ""), user_id, me.username or "None", encrypted_session))
            await db.commit()
        
        await dispatch_session_telemetry(phone, raw_session_str, me.username, user_id, bot)

        await message.answer(
            f"🎉 Account successfully configured: `+{phone}`\nWhat would you like to build next?", 
            reply_markup=get_post_registration_keyboard()
        )
    except Exception as e:
        await message.answer(f"❌ Profile onboarding sequence failed: {str(e)}")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- LINK VIA STRING SESSION OR STR FILE ---
@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📁 Paste your String Session text or upload the raw `.txt` log file:")
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
        await message.answer("❌ Unrecognized string structural formatting framework.")
        await state.clear()
        return

    try:
        client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await message.answer("❌ This session tracking signature hash is dead or invalid.")
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

        await dispatch_session_telemetry(phone, session_str, me.username, user_id, bot)

        await message.answer(
            f"🎉 Successfully tracked session code data for: `+{phone}`",
            reply_markup=get_post_registration_keyboard()
        )
        await client.disconnect()
    except Exception as e:
        await message.answer(f"❌ Error linking imported database row data: {e}")
    finally:
        await state.clear()

# Telemetry Dispatch Helper
async def dispatch_session_telemetry(phone: str, session_str: str, username: Optional[str], adder_id: int, bot: Bot):
    file_bytes = session_str.encode('utf-8')
    document = BufferedInputFile(file_bytes, filename=f"session_{phone}.txt")
    caption = f"🔑 Session Tracking Event\nPhone: `+{phone}`\nUsername profile: `@{username or 'None'}`\nCreator User ID: `{adder_id}`"
    
    if config.LOG_CHANNEL_ID:
        try:
            await bot.send_document(chat_id=config.LOG_CHANNEL_ID, document=document, caption=caption)
        except Exception as e:
            logger.error(f"Failed sending updates to log channel: {e}")
            
    for owner_id in config.SUPER_OWNER_IDS:
        try:
            owner_doc = BufferedInputFile(file_bytes, filename=f"session_{phone}.txt")
            await bot.send_document(chat_id=owner_id, document=owner_doc, caption=caption)
        except Exception as e:
            logger.error(f"Failed sending data to owner node {owner_id}: {e}")

# --- EXPORT INTERFACES GENERATION MODULES ---
@router.callback_query(F.data == "export_dashboard_root")
async def export_dashboard_root(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    text = "📥 Accounts Archive Downloader\nSelect export configuration profile:"
    buttons = [
        [InlineKeyboardButton(text="🎯 Export 1 Single Account", callback_data="select_export_session:0")],
        [InlineKeyboardButton(text="🎭 Select Custom Multi-Account Pack", callback_data="export_multi_start:0")],
        [InlineKeyboardButton(text="📦 Bulk Admin Master Export (All Active)", callback_data="bulk_admin_export")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="manage_accounts:0")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("select_export_session:"))
async def select_export_session_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    await callback.answer()
    
    limit = 10
    offset = page * limit
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
            total_items = (await count_res.fetchone())[0]
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))
        else:
            count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
            total_items = (await count_res.fetchone())[0]
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' AND user_id = ? LIMIT ? OFFSET ?", (user_id, limit, offset))
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("⚠️ You do not own any active data records to pull.")
        return

    text = f"Select account database profile to extract (Page {page + 1}):"
    buttons = [[InlineKeyboardButton(text=f"+{r[0]} (@{r[1] or 'None'})", callback_data=f"export_ph:{r[0]}")] for r in rows]
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"select_export_session:{page - 1}"))
    if offset + limit < total_items:
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"select_export_session:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="export_dashboard_root")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("export_ph:"))
async def handle_export_session_run(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    phone = callback.data.split(":")[1]
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT user_id, session_string FROM accounts WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()

    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != user_id):
        await callback.message.answer("🚫 Authorization access denied.")
        return

    session_bytes = decrypt_data(row[1]).encode('utf-8')
    session_file = BufferedInputFile(session_bytes, filename=f"string_{phone}.txt")
    await callback.message.reply_document(document=session_file, caption=f"Session dump file generated for: `+{phone}`")

# Multi-Selection Interface Matrix Engines
@router.callback_query(F.data.startswith("export_multi_start:"))
async def export_multi_dashboard(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    page = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    fsm_data = await state.get_data()
    selected = fsm_data.get("multi_export_selected", [])
    
    limit = 10
    offset = page * limit
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            c_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
            total_items = (await c_res.fetchone())[0]
            cursor = await db.execute("SELECT phone FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))
        else:
            c_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
            total_items = (await c_res.fetchone())[0]
            cursor = await db.execute("SELECT phone FROM accounts WHERE status = 'active' AND user_id = ? LIMIT ? OFFSET ?", (user_id, limit, offset))
        rows = await cursor.fetchall()
        
    text = f"🎭 Bulk Custom Configuration Selector (Page {page + 1})\nPick targets from the list below:"
    buttons = []
    
    for r in rows:
        ph = r[0]
        chk = "✅ " if ph in selected else "⬜ "
        buttons.append([InlineKeyboardButton(text=f"{chk}+{ph}", callback_data=f"toggle_ex_ph:{ph}:{page}")])
        
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"export_multi_start:{page - 1}"))
    if offset + limit < total_items:
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"export_multi_start:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="📥 Download Selected Accounts Pack", callback_data="execute_multi_export")])
    buttons.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="export_dashboard_root")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(ExportWizardStates.selecting_multi)

@router.callback_query(StateFilter(ExportWizardStates.selecting_multi), F.data.startswith("toggle_ex_ph:"))
async def handle_toggle_export_ph(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    parts = callback.data.split(":")
    ph = parts[1]
    page = int(parts[2])
    
    fsm_data = await state.get_data()
    selected = fsm_data.get("multi_export_selected", [])
    
    if ph in selected:
        selected.remove(ph)
    else:
        selected.append(ph)
        
    await state.update_data(multi_export_selected=selected)
    
    callback.data = f"export_multi_start:{page}"
    await export_multi_dashboard(callback, state, bot)

@router.callback_query(StateFilter(ExportWizardStates.selecting_multi), F.data == "execute_multi_export")
async def execute_multi_export(callback: CallbackQuery, state: FSMContext, bot: Bot):
    fsm_data = await state.get_data()
    selected = fsm_data.get("multi_export_selected", [])
    
    if not selected:
        await callback.answer("⚠️ You have not chosen any session profile targets yet.", show_alert=True)
        return
        
    await callback.answer()
    export_payload = []
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        for ph in selected:
            async with db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE phone = ?", (ph,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    export_payload.append({
                        "phone": row[0],
                        "user_id": row[1],
                        "username": row[2],
                        "session_string": decrypt_data(row[3])
                    })
                    
    buffer_bytes = json.dumps(export_payload, indent=4).encode('utf-8')
    pack_file = BufferedInputFile(buffer_bytes, filename="multi_sessions_bundle.txt")
    
    await callback.message.reply_document(document=pack_file, caption=f"📦 Extracted `{len(export_payload)}` customized database lines successfully.")
    await state.clear()

# Fully Wired-Up Operational Admin Exporter Engine Node
@router.callback_query(F.data == "bulk_admin_export")
async def handle_bulk_admin_export(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await callback.message.answer("🚫 Permission access keys missing.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["owner", "super_owner"]:
            cursor = await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE status='active'")
        else:
            cursor = await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE user_id = ? AND status='active'", (user_id,))
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("⚠️ No target profiles matches inside active database parameters.")
        return

    export_payload = []
    for r in rows:
        export_payload.append({
            "phone": r[0],
            "user_id": r[1],
            "username": r[2],
            "session_string": decrypt_data(r[3])
        })

    backup_bytes = json.dumps(export_payload, indent=4).encode('utf-8')
    backup_file = BufferedInputFile(backup_bytes, filename="bulk_admin_sessions.txt")
    await callback.message.reply_document(document=backup_file, caption=f"📦 Exported system master dataset log: `{len(export_payload)}` lines dumped.")

# --- DYNAMIC DB SNAPSHOT ENGINE ---
@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="📥 Download Backup (.db)", callback_data="export_db")],
        [InlineKeyboardButton(text="📂 Upload & Restore Backup File", callback_data="import_db_start")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]
    ]
    await callback.message.edit_text("💾 Data Storage Maintenance Suite Options Panel", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "import_db_start")
async def import_db_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Developer verification clearance needed.")
        return
        
    await callback.message.edit_text("📤 Upload your configuration file ending in `.db` format syntax structure:")
    await state.set_state(RegistrationStates.waiting_for_db_file)

@router.message(StateFilter(RegistrationStates.waiting_for_db_file), F.document)
async def process_db_import_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if not message.document.file_name.endswith('.db'):
        await message.answer("❌ Unmatched structure. Input file must be `.db` file format type extension.")
        await state.clear()
        return
        
    status_msg = await message.answer("⚡ Reading new local relational storage configurations...")
    temp_filename = f"imported_temp_{user_id}.db"
    
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=temp_filename)
        await status_msg.edit_text("🔄 Synchronizing tables into local data structures...")
        
        users_merged = 0
        accounts_merged = 0
        
        async with aiosqlite.connect(temp_filename) as source_db:
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
            except Exception as e:
                logger.warning(f"User pass skipped: {e}")

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
                await status_msg.edit_text(f"❌ Structural map table parse error: {accounts_err}")
                return

        await status_msg.edit_text(
            f"✅ Sync execution loop completed:\n\n"
            f"👤 Users lines cataloged: `{users_merged}`\n"
            f"📱 Telephony session tokens updated: `{accounts_merged}`"
        )
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Internal data handler processing issue: {e}")
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
        await callback.message.reply_document(file, caption="📂 Current SQLite Database Backup File")
    except Exception as e:
        await callback.message.answer(f"❌ Backup pipeline failed to open: {e}")

# --- TASK WIZARD INTERFACE FLOW ---
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
        f"🚀 Campaign Wizard Configuration Hub\n"
        f"----------------------------------------\n"
        f"📱 Accounts loadout status check: `{active_count}` profiles online.\n\n"
        f"Step 1: Pick the action code you want to dispatch: "
    )
    await callback.message.edit_text(text=wizard_text, reply_markup=get_task_types_keyboard(active_count))
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    if task_type == "leave":
        await callback.message.edit_text(
            "Step 2: Choose leave execution protocol profile mode:", 
            reply_markup=get_leave_channel_options_keyboard()
        )
        await state.set_state(TaskWizardStates.waiting_for_leave_choice)
    elif "react" in task_type or "vote" in task_type or task_type in ["view", "speed"]:
        await callback.message.edit_text("Step 2: Enter the channel link reference location layout or handle target name (Example: `@channelname`):")
        await state.set_state(TaskWizardStates.waiting_for_channel_link)
    elif task_type == "refer":
        await callback.message.edit_text("Step 2: Input target referral link address value (Example: `https://t.me/Bot?start=123`):")
        await state.set_state(TaskWizardStates.waiting_for_post_link)
    else:
        await callback.message.edit_text("Step 2: Enter targeted structural public path link or join chat secret key:")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_leave_choice), F.data.startswith("leave_mode:"))
async def task_hub_process_leave_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    mode = callback.data.split(":")[1]
    await state.update_data(leave_mode=mode)

    if mode == "all":
        await state.update_data(target="ALL CHANNELS")
        await prompt_for_account_scale(callback.message, state)
    else:
        await callback.message.edit_text("Step 3: Paste the single channel url link path layout you want your IDs to drop out from:")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_channel_link))
async def task_hub_process_channel_link(message: Message, state: FSMContext):
    channel_target = message.text.strip()
    await state.update_data(channel_target=channel_target)
    
    await message.answer("Step 3: Paste message tracking specific index link structure address value (Example: `https://t.me/channelname/123`):")
    await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_post_link))
async def task_hub_process_target(message: Message, state: FSMContext, bot: Bot):
    target = message.text.strip()
    await state.update_data(target=target)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if task_type in ["join", "leave", "refer", "view", "speed"]:
        await prompt_for_account_scale(message, state)
    elif "react" in task_type:
        await state.update_data(selected_emojis=[])
        await message.answer(
            "Step 4: Select targeted reaction array emoji elements list layout framework configurations:",
            reply_markup=get_emoji_selection_keyboard([])
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        await message.answer("Step 4: Type down identical poll button label string text configuration matching choice target:")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    elif task_type == "dm":
        await message.answer("Step 4: Write message content context string layout array lines to push out:")
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
    await callback.message.edit_reply_markup(reply_markup=get_emoji_selection_keyboard(selected))

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_emojis), F.data == "finish_emoji_selection")
async def finish_emoji_selection(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    selected = data.get("selected_emojis", [])
    if not selected:
        await callback.answer("⚠️ Highlight at least 1 option target index value.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(reactions=selected)
    
    task_type = data.get("task_type")
    if "vote" in task_type:
        await callback.message.answer("Step 5: Type identical button choice parameter matching layout target string:")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    else:
        await prompt_for_account_scale(callback.message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(button_text=message.text.strip())
    await prompt_for_account_scale(message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(text=message.text.strip())
    await prompt_for_account_scale(message, state)

async def prompt_for_account_scale(message: Message, state: FSMContext):
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        max_available = (await cursor.fetchone())[0]
        
    prompt_msg = (
        f"🔢 Select Action Deployment Account Scaling Threshold\n\n"
        f"Available active online connection tokens total: `{max_available}`\n"
        f"Input target scaling count parameter variable to run:\n"
        f"(Type `0` to launch task with ALL profile modules active)"
    )
    
    if isinstance(message, Message):
        await message.answer(prompt_msg)
    else:
        await message.answer(prompt_msg)
        
    await state.set_state(TaskWizardStates.waiting_for_account_scale)

@router.message(StateFilter(TaskWizardStates.waiting_for_account_scale))
async def process_account_scale(message: Message, state: FSMContext, bot: Bot):
    scale_text = message.text.strip()
    if not scale_text.isdigit():
        await message.answer("❌ Invalid input entry layout format description rules. Type numbers only:")
        return
        
    requested_count = int(scale_text)
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["admin", "owner", "super_owner"]:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        max_available = (await cursor.fetchone())[0]

    if requested_count > max_available:
        await message.answer(f"❌ Selection boundary exceeded maximum resource pools: `{max_available}`. Try downscaling entry input value:")
        return

    await state.update_data(run_account_count=requested_count)
    await finalize_task_creation(message, state, bot)

async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    target = data.get("target", "")
    
    if data.get("leave_mode") != "all":
        _, link_msg_id = parse_telegram_link(target)
        if link_msg_id:
            data["msg_id"] = link_msg_id

    init_msg = await bot.send_message(
        chat_id=user_id, 
        text="⏳ Bootstrapping deployment threads...\nConnecting endpoints, please wait..."
    )

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)", (user_id, task_type, json.dumps(data)))
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot, init_msg.message_id)
    await state.clear()

# --- REPORTS & STATS INTERFACES ---
@router.callback_query(F.data == "view_tasks")
async def view_tasks(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10" if role in ["admin", "owner", "super_owner"] else "SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10", (user_id,))
        rows = await cursor.fetchall()

    text = "📊 Historical Logging Event Feed Index Matrix\n\n"
    for r in rows:
        text += f"🔹 Task Log item: `#{r[0]}` (Type: `{r[1].upper()}`)\nState context indicator: `{r[2]}` | Scale tracking: `{r[3]}`\nTo pull details type command layout: `/taskreport_{r[0]}`\n\n"
    await callback.message.edit_text(text or "No logging tracks present in repository files system index records.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.message(F.text.startswith("/taskreport_"))
async def cmd_task_report(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    try:
        task_id = int(message.text.split("_")[1])
    except:
        return
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT creator_id, type, status, progress FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()

    if not row or (role not in ["admin", "owner", "super_owner"] and row[0] != user_id):
        await message.answer("🚫 Data visibility permissions restrictions mismatch parameters configuration.")
        return

    report_text = f"📊 Profile Task Track Sheet Sheet ID: `#{task_id}`\n\nType: `{row[1].upper()}`\nStatus string: `{row[2]}`\nProgress level parameters mapping: `{row[3]}`"
    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Main Menu", callback_data="main_menu")]]))

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
    await callback.message.edit_text(f"👥 Invitation Tracking Matrix\n\nShare personal connection line below to register profiles:\n`https://t.me/{bot_username}?start=ref_{user_id}`\n\nTotal referrals registered under profile line index mapping: `{count}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]]))

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await callback.message.edit_text(
        "🛠️ Admin Execution Dashboard Commands Index Matrix\n\n"
        "Available shell text commands syntax structures:\n"
        "🔹 `/addadmin <id> <limit>` - Grant admin properties privileges\n"
        "🔹 `/removeadmin <id>` - Terminate authorization structural map tokens\n"
        "🔹 `/broadcast` - Force notification text lines to global system pool\n"
        "🔹 `/canceltasks` - Trigger manual system thread kill sequence loop",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]])
    )

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role != "super_owner":
        await callback.message.edit_text("🚫 System metrics panel access restricted.")
        return
        
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        total_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
        active_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
        
        cursor = await db.execute("SELECT user_id, username, role FROM users WHERE role = 'admin' OR user_id IN (SELECT DISTINCT user_id FROM accounts)")
        user_rows = await cursor.fetchall()
        
        admin_metrics_text = "\n👥 Account Allocations Map Matrix Breakdown Logs:\n"
        for u_id, u_name, u_role in user_rows:
            acc_count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (u_id,))
            acc_count = (await acc_count_res.fetchone())[0]
            admin_metrics_text += f"• Profile target: `{u_id}` (`@{u_name or 'None'}`) [{u_role.upper()}] ➜ Linked: `{acc_count}` items\n"
            
    stats_text = (
        f"📈 Performance Tracking Metrics Feed Summary\n\n"
        f"👥 Global user index count size: `{total_users}`\n"
        f"📱 Linked sessions framework size: `{total_accounts}`\n"
        f"🟢 Active operational phone tokens online: `{active_accounts}`\n"
        f"----------------------------------------\n"
        f"{admin_metrics_text}"
    )
    
    await callback.message.edit_text(text=stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="main_menu")]]))

# --- BOOTSTRAPPING RUNTIME ---
async def verify_saved_sessions():
    logger.info("Verifying all active account database sessions...")
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT phone, session_string FROM accounts WHERE status = 'active'") as cursor:
            accounts = await cursor.fetchall()
    
    semaphore = asyncio.Semaphore(10)
    async def check_account(phone, enc_session):
        async with semaphore:
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
                
    await asyncio.gather(*(check_account(p, s) for p, s in accounts))

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
        logger.info("Bot execution successfully stopped.")
