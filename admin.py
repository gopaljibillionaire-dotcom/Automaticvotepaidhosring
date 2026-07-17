import re
from typing import Tuple, Any, Optional
from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import aiosqlite
from config import db_mgr, logger

admin_router = Router()

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

@admin_router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, bot: Bot):
    role = await db_mgr.get_user_role(message.from_user.id)
    if role not in ["owner", "super_owner"]:
        await message.answer("🚫 Access Denied: Insufficient authorization profiles.")
        return

    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.answer("ℹ️ **Usage:** `/addadmin <target_user_id> <max_account_limit>`")
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
    await message.answer(f"✅ Success! User ID `{target_id}` is registered as an Admin.")

@admin_router.message(Command("removeadmin"))
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
    await message.answer(f"✅ User ID `{target_id}` has been demoted.")
