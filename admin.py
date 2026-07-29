import asyncio
import json
import base64
import os
import aiosqlite
from aiogram import Router, Bot, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)

import config
from config import logger
from database import db_mgr, decrypt_data

admin_router = Router()

# --- FSM STATES ---
class ExportWizardStates(StatesGroup):
    selecting_multi = State()

class BroadcastStates(StatesGroup):
    waiting_for_msg = State()

class AdminRegistrationStates(StatesGroup):
    waiting_for_db_file = State()

@admin_router.message(Command("canceltasks"))
async def cmd_cancel_tasks(message: Message, bot: Bot):
    from main import task_queue
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

@admin_router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Owner privilege tokens.")
        return
        
    args = command.args
    if not args or len(args.split()) < 2:
        await message.answer("✨ <b>Syntax Profile Map layout:</b> <code>/addadmin &lt;user_id&gt; &lt;account_limit&gt;</code>", parse_mode="HTML")
        return
        
    target_id_str, limit_str = args.split()[:2]
    if not target_id_str.isdigit() or not limit_str.isdigit():
        await message.answer("❌ Parameters mismatch error: Numerical integers values required exclusively.")
        return
        
    target_id = int(target_id_str)
    limit_val = int(limit_str)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute(
            "INSERT INTO users (user_id, role, max_accounts) VALUES (?, 'admin', ?) ON CONFLICT(user_id) DO UPDATE SET role='admin', max_accounts=?",
            (target_id, limit_val, limit_val)
        )
        await db.commit()
        
    await message.answer(f"💎 <b>Success:</b> User <code>{target_id}</code> updated to Admin with a capacity ceiling of <code>{limit_val}</code> profiles.", parse_mode="HTML")
    await db_mgr.log_action(user_id, f"Made user {target_id} an Admin (limit={limit_val})", bot, operational=True)

@admin_router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, command: CommandObject, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> This command requires Owner privilege tokens.")
        return
        
    target_id_str = command.args
    if not target_id_str or not target_id_str.strip().isdigit():
        await message.answer("✨ <b>Syntax Profile Map layout:</b> <code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
        return
        
    target_id = int(target_id_str.strip())
    async with aiosqlite.connect(db_mgr.db_path) as db:
        await db.execute("UPDATE users SET role='user' WHERE user_id = ?", (target_id,))
        await db.commit()
        
    await message.answer(f"💎 <b>Success:</b> Authorization structural privileges revoked from Admin ID <code>{target_id}</code>.", parse_mode="HTML")
    await db_mgr.log_action(user_id, f"Removed Admin role from user {target_id}", bot, operational=True)

@admin_router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["admin", "owner", "super_owner"]:
        await message.answer("⚠️ <b>Clearance Denied:</b> Command restricted to Administration Nodes.")
        return
        
    await message.answer("📢 <b>Input Data Text or Multimedia payload content to broadcast:</b>", parse_mode="HTML")
    await state.set_state(BroadcastStates.waiting_for_msg)

@admin_router.message(StateFilter(BroadcastStates.waiting_for_msg))
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
        f"🟪 Blocked/Dead targets dropped: <code>{failed_hits}</code> nodes",
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await callback.message.edit_text(
        "🛡️ <b>Administrative Operational Console Index Terminal</b>\n\n"
        "Available terminal shell command scripts layout frameworks:\n\n"
        "🔹 <code>/addadmin &lt;id&gt; &lt;limit&gt;</code> - Promote user node into admin status ranks\n"
        "🔹 <code>/removeadmin &lt;id&gt;</code> - Deprecate admin structural token access rules\n"
        "🔹 <code>/broadcast</code> - Force dynamic notification content across global users pools\n"
        "🔹 <code>/canceltasks</code> - Instantly kill all running thread operations loops safely",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu")]]),
        parse_mode="HTML"
    )

@admin_router.callback_query(F.data == "system_stats")
async def system_stats(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role != "super_owner":
        await callback.message.edit_text("🚫 System metrics dashboard view access restricted to core developers.")
        return
        
    async with aiosqlite.connect(db_mgr.db_path) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        total_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts")).fetchone())[0]
        active_accounts = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")).fetchone())[0]
        
        cursor = await db.execute("SELECT user_id, username, role FROM users WHERE role = 'admin' OR user_id IN (SELECT DISTINCT user_id FROM accounts)")
        user_rows = await cursor.fetchall()
        
        admin_metrics_text = "\n👥 <b>Structural Account Space Partition Allocation Map Logs:</b>\n"
        for u_id, u_name, u_role in user_rows:
            acc_count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (u_id,))
            acc_count = (await acc_count_res.fetchone())[0]
            admin_metrics_text += f"• Node profile target: <code>{u_id}</code> (<b>@{u_name or 'None'}</b>) [<b>{u_role.upper()}</b>] ➜ Linked slots count: <code>{acc_count}</code> items\n"
            
    stats_text = (
        f"📈 <b>Live System Production Core Performance Summary Metrics</b>\n\n"
        f"👥 Global active profiles space size: <code>{total_users}</code> users\n"
        f"📱 Total linked terminal telephony sessions: <code>{total_accounts}</code> instances\n"
        f"🟢 Active operational connection streams online: <code>{active_accounts}</code> nodes\n"
        f"----------------------------------------------------"
        f"{admin_metrics_text}"
    )
    
    await callback.message.edit_text(text=stats_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu")]]), parse_mode="HTML")

@admin_router.callback_query(F.data == "backup_panel")
async def backup_panel(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    buttons = [
        [InlineKeyboardButton(text="📥 Save SQLite Backup (.db)", callback_data="export_db")],
        [InlineKeyboardButton(text="📂 Upload .db file ", callback_data="import_db_start")],
        [InlineKeyboardButton(text="💎 Return Home Menu", callback_data="main_menu")]
    ]
    await callback.message.edit_text("💾 <b>Relational SQL Datastore System Maintenance Suite Control Panel</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@admin_router.callback_query(F.data == "import_db_start")
async def import_db_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Developer verification clearance needed.")
        return
        
    await callback.message.edit_text("📤 <b>Upload backup relational runtime datastore script ending inside <code>.db</code> file format syntax extension layout:</b>", parse_mode="HTML")
    await state.set_state(AdminRegistrationStates.waiting_for_db_file)

@admin_router.message(StateFilter(AdminRegistrationStates.waiting_for_db_file), F.document)
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

@admin_router.callback_query(F.data == "export_db")
async def export_db(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    try:
        with open(db_mgr.db_path, "rb") as f:
            file = BufferedInputFile(f.read(), filename="database_core_backup.db")
        await callback.message.reply_document(file, caption="📂 <b>Current Core SQLite Operational Database Backup File Snapshot</b>", parse_mode="HTML")
    except Exception as e:
        await callback.message.answer(f"❌ Core backup extraction streams dropped: {e}")

# --- EXPORT ARCHIVE MANAGEMENT HOOKS ---
@admin_router.callback_query(F.data == "export_dashboard_root")
async def export_dashboard_root(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["super_owner", "owner"]:
        await callback.answer("⚠️ Clearance Level Violated: File extraction dashboard tools are barred for admins.", show_alert=True)
        return
        
    await callback.answer()
    text = "📥 <b>Session Extraction Management Dashboard Terminal</b>\nSelect extraction criteria filters:"
    buttons = [
        [InlineKeyboardButton(text="🎯 Extract 1 Single Session Profile", callback_data="select_export_session:0")],
        [InlineKeyboardButton(text="🎭  Multi-Session extract ", callback_data="export_multi_start:0")],
        [InlineKeyboardButton(text="📦 Extract Full pack", callback_data="bulk_admin_export")],
        [InlineKeyboardButton(text="🔙 Return Back", callback_data="manage_accounts:0")]
    ]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("select_export_session:"))
async def select_export_session_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    await callback.answer()
    
    limit = 10
    offset = page * limit
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        placeholders = ','.join('?' for _ in config.SUPER_OWNER_IDS)
        if role == "super_owner":
            count_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
            total_items = (await count_res.fetchone())[0]
            cursor = await db.execute("SELECT phone, username FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))
        elif role == "owner":
            count_res = await db.execute(f"SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id NOT IN ({placeholders})", config.SUPER_OWNER_IDS)
            total_items = (await count_res.fetchone())[0]
            cursor = await db.execute(f"SELECT phone, username FROM accounts WHERE status = 'active' AND user_id NOT IN ({placeholders}) LIMIT ? OFFSET ?", (*config.SUPER_OWNER_IDS, limit, offset))
        else:
            await callback.message.answer("🚫 Permission check validation rejected.")
            return
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("⚠️ No accessible active telephony data clusters found corresponding to your filter access.")
        return

    text = f"Select structural database session profile target row to dump (Page {page + 1}):"
    buttons = [[InlineKeyboardButton(text=f"📱 +{r[0]} (@{r[1] or 'None'})", callback_data=f"export_ph:{r[0]}")] for r in rows]
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⏮️ Previous", callback_data=f"select_export_session:{page - 1}"))
    if offset + limit < total_items:
        nav_row.append(InlineKeyboardButton(text="Next ⏭️", callback_data=f"select_export_session:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="🔙 Return Back", callback_data="export_dashboard_root")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@admin_router.callback_query(F.data.startswith("export_ph:"))
async def handle_export_session_run(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    phone = callback.data.split(":")[1]
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["super_owner", "owner"]:
        await callback.message.answer("🚫 Authorization access denied.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        async with db.execute("SELECT user_id, session_string FROM accounts WHERE phone = ?", (phone,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await callback.message.answer("❌ Selected profile data missing inside datastore registries.")
        return

    if row[0] in config.SUPER_OWNER_IDS and role != "super_owner":
        await callback.message.answer("🛡️ <b>Access Violation:</b> Super Owner profiles are isolated and protected.")
        return

    session_bytes = decrypt_data(row[1]).encode('utf-8')
    session_file = BufferedInputFile(session_bytes, filename=f"string_{phone}.txt")
    await callback.message.reply_document(document=session_file, caption=f"✨ Session dump file generated safely for: <code>+{phone}</code>", parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("export_multi_start:"))
async def export_multi_dashboard(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    page = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    if role not in ["super_owner", "owner"]:
        await callback.message.answer("🚫 Permission check validation rejected.")
        return
        
    fsm_data = await state.get_data()
    selected = fsm_data.get("multi_export_selected", [])
    
    limit = 10
    offset = page * limit
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        placeholders = ','.join('?' for _ in config.SUPER_OWNER_IDS)
        if role == "super_owner":
            c_res = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
            total_items = (await c_res.fetchone())[0]
            cursor = await db.execute("SELECT phone FROM accounts WHERE status = 'active' LIMIT ? OFFSET ?", (limit, offset))
        else:
            c_res = await db.execute(f"SELECT COUNT(*) FROM accounts WHERE status = 'active' AND user_id NOT IN ({placeholders})", config.SUPER_OWNER_IDS)
            total_items = (await c_res.fetchone())[0]
            cursor = await db.execute(f"SELECT phone FROM accounts WHERE status = 'active' AND user_id NOT IN ({placeholders}) LIMIT ? OFFSET ?", (*config.SUPER_OWNER_IDS, limit, offset))
        rows = await cursor.fetchall()
        
    text = f"🎭 <b>Customized Pack Package Assembly Core Selector</b> (Page {page + 1})\nSelect accounts profiles to encapsulate:"
    buttons = []
    
    for r in rows:
        ph = r[0]
        chk = "💎 " if ph in selected else "⬜ "
        buttons.append([InlineKeyboardButton(text=f"{chk}+{ph}", callback_data=f"toggle_ex_ph:{ph}:{page}")])
        
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⏮️ Previous", callback_data=f"export_multi_start:{page - 1}"))
    if offset + limit < total_items:
        nav_row.append(InlineKeyboardButton(text="Next ⏭️", callback_data=f"export_multi_start:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
        
    buttons.append([InlineKeyboardButton(text="📦 Build Pack Bundle & Download Archive", callback_data="execute_multi_export")])
    buttons.append([InlineKeyboardButton(text="🛑 Terminate Pack Configuration", callback_data="export_dashboard_root")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await state.set_state(ExportWizardStates.selecting_multi)

@admin_router.callback_query(StateFilter(ExportWizardStates.selecting_multi), F.data.startswith("toggle_ex_ph:"))
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

@admin_router.callback_query(StateFilter(ExportWizardStates.selecting_multi), F.data == "execute_multi_export")
async def execute_multi_export(callback: CallbackQuery, state: FSMContext, bot: Bot):
    fsm_data = await state.get_data()
    selected = fsm_data.get("multi_export_selected", [])
    
    if not selected:
        await callback.answer("⚠️ You must pick at least 1 destination target account profile.", show_alert=True)
        return
        
    await callback.answer()
    export_payload = []
    user_id = callback.from_user.id
    role = await db_mgr.get_user_role(user_id)
    
    async with aiosqlite.connect(db_mgr.db_path) as db:
        for ph in selected:
            async with db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE phone = ?", (ph,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    if row[1] in config.SUPER_OWNER_IDS and role != "super_owner":
                        continue
                    export_payload.append({
                        "phone": row[0],
                        "user_id": row[1],
                        "username": row[2],
                        "session_string": decrypt_data(row[3])
                    })
                    
    buffer_bytes = json.dumps(export_payload, indent=4).encode('utf-8')
    pack_file = BufferedInputFile(buffer_bytes, filename="multi_sessions_bundle.txt")
    
    await callback.message.reply_document(document=pack_file, caption=f"✨ <b>Pack extraction compiled!</b> Successfully consolidated <code>{len(export_payload)}</code> customized database session rows.", parse_mode="HTML")
    await state.clear()

@admin_router.callback_query(F.data == "bulk_admin_export")
async def handle_bulk_admin_export(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    await callback.answer()
    role = await db_mgr.get_user_role(user_id)
    if role not in ["owner", "super_owner"]:
        await callback.message.answer("🚫 Clearances credential criteria missing.")
        return

    async with aiosqlite.connect(db_mgr.db_path) as db:
        placeholders = ','.join('?' for _ in config.SUPER_OWNER_IDS)
        if role == "super_owner":
            cursor = await db.execute("SELECT phone, user_id, username, session_string FROM accounts WHERE status='active'")
        else:
            cursor = await db.execute(f"SELECT phone, user_id, username, session_string FROM accounts WHERE status='active' AND user_id NOT IN ({placeholders})", config.SUPER_OWNER_IDS)
        rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("⚠️ Datastore registries do not match current scope rules filters.")
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
    await callback.message.reply_document(document=backup_file, caption=f"📦 <b>Master Datastore Core Bulk Extract Dump Complete!</b> Catalogued <code>{len(export_payload)}</code> active network session nodes safely.", parse_mode="HTML")
