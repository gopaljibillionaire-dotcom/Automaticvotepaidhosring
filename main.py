import asyncio
import logging
import random
import sys
from datetime import datetime
from typing import Optional, List, Dict, Any

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("egg_chicken_bot")

# ==========================================
# DATABASE INITIALIZATION & HELPER FUNCTIONS
# ==========================================

async def init_db():
    async with aiosqlite.connect(settings.DATABASE_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        
        # Users Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance REAL DEFAULT 0.0,
                is_blocked INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Countries Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS countries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                flag TEXT NOT NULL,
                is_enabled INTEGER DEFAULT 1
            )
        """)
        
        # Products Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                type TEXT NOT NULL, -- 'egg' or 'chicken'
                country_id INTEGER NOT NULL,
                price REAL NOT NULL,
                quality TEXT NOT NULL,
                description TEXT DEFAULT '',
                image_file_id TEXT DEFAULT NULL,
                stock INTEGER DEFAULT 1,
                status TEXT DEFAULT 'available', -- 'available', 'disabled', 'sold'
                delivery_type TEXT DEFAULT 'text', -- 'text', 'photo', 'file'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (country_id) REFERENCES countries (id) ON DELETE CASCADE
            )
        """)
        
        # Orders Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id TEXT NOT NULL,
                product_type TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id),
                FOREIGN KEY (product_id) REFERENCES products (product_id)
            )
        """)
        
        # Wallet Transactions Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                transaction_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL, -- 'deposit', 'purchase', 'admin_adjustment'
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id)
            )
        """)
        
        # Seed default countries if empty
        cursor = await db.execute("SELECT COUNT(*) FROM countries")
        count = (await cursor.fetchone())[0]
        if count == 0:
            default_countries = [
                ("US", "America", "🇺🇸", 1),
                ("IN", "India", "🇮🇳", 1),
                ("BD", "Bangladesh", "🇧🇩", 1),
                ("AE", "UAE", "🇦🇪", 1),
                ("GB", "UK", "🇬🇧", 1),
            ]
            await db.executemany(
                "INSERT INTO countries (code, name, flag, is_enabled) VALUES (?, ?, ?, ?)",
                default_countries
            )
        
        await db.commit()

# Corrected get_db: returns connection context manager directly
def get_db():
    return aiosqlite.connect(settings.DATABASE_NAME)

# ==========================================
# FSM STATES
# ==========================================

class AddProductFSM(StatesGroup):
    select_type = State()
    select_country = State()
    enter_id = State()
    enter_price = State()
    select_quality = State()
    enter_stock = State()
    enter_description = State()
    upload_photo = State()
    preview = State()

class AddCountryFSM(StatesGroup):
    enter_name = State()
    enter_flag = State()
    enter_code = State()

class RechargeFSM(StatesGroup):
    enter_amount = State()

class AdminUserActionFSM(StatesGroup):
    adjust_balance = State()
    broadcast_message = State()

# ==========================================
# KEYBOARD BUILDERS (WITH STYLED/COLORED BUTTONS)
# ==========================================

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🥚 BUY EGG", callback_data="buy_cat:egg", style="primary"),
            InlineKeyboardButton(text="🍗 BUY CHICKEN", callback_data="buy_cat:chicken", style="primary")
        ],
        [
            InlineKeyboardButton(text="💳 WALLET RECHARGE", callback_data="wallet_recharge", style="success"),
            InlineKeyboardButton(text="📦 MY ORDERS", callback_data="my_orders", style="primary")
        ],
        [
            InlineKeyboardButton(text="👤 MY PROFILE", callback_data="my_profile"),
            InlineKeyboardButton(text="❓ HELP", callback_data="help")
        ]
    ])

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Add Egg", callback_data="admin:add_product:egg", style="success"),
            InlineKeyboardButton(text="➕ Add Chicken", callback_data="admin:add_product:chicken", style="success")
        ],
        [
            InlineKeyboardButton(text="📦 Manage Eggs", callback_data="admin:manage_prod:egg", style="primary"),
            InlineKeyboardButton(text="🍗 Manage Chicken", callback_data="admin:manage_prod:chicken", style="primary")
        ],
        [
            InlineKeyboardButton(text="🌍 Manage Countries", callback_data="admin:manage_countries", style="primary"),
            InlineKeyboardButton(text="👥 Users", callback_data="admin:users", style="primary")
        ],
        [
            InlineKeyboardButton(text="🛒 Orders", callback_data="admin:orders"),
            InlineKeyboardButton(text="📊 Statistics", callback_data="admin:stats")
        ],
        [
            InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast", style="danger"),
            InlineKeyboardButton(text="🏠 User Main Menu", callback_data="main_menu")
        ]
    ])

def back_home_buttons() -> List[List[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="⬅️ BACK", callback_data="main_menu", style="danger"),
            InlineKeyboardButton(text="🏠 MAIN MENU", callback_data="main_menu", style="primary")
        ]
    ]

# ==========================================
# MIDDLEWARE / CHECKS
# ==========================================

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

async def get_or_create_user(telegram_id: int, username: Optional[str], first_name: Optional[str]) -> dict:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            user = await cursor.fetchone()
            if not user:
                await db.execute(
                    "INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)",
                    (telegram_id, username or "N/A", first_name or "User")
                )
                await db.commit()
                async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as c2:
                    user = await c2.fetchone()
            return dict(user)

# ==========================================
# ROUTERS & HANDLERS
# ==========================================

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if user['is_blocked']:
        await message.answer("❌ Your account is blocked from using this bot.")
        return

    text = (
        f"🥚 **EGG & CHICKEN MARKET**\n\n"
        f"👤 **Name:** {user['first_name']}\n"
        f"🆔 **User ID:** `{user['telegram_id']}`\n"
        f"💰 **Balance:** ${user['balance']:.2f}\n\n"
        f"Welcome to the official market! Select an option below:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Unauthorized access.")
        return
    await message.answer("👨‍💻 **ADMIN PANEL**\nSelect an option to manage the store:", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    text = (
        f"🥚 **EGG & CHICKEN MARKET**\n\n"
        f"👤 **Name:** {user['first_name']}\n"
        f"🆔 **User ID:** `{user['telegram_id']}`\n"
        f"💰 **Balance:** ${user['balance']:.2f}"
    )
    await callback.message.edit_text(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "my_profile")
async def cb_my_profile(callback: CallbackQuery):
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*), SUM(amount) FROM orders WHERE user_id = ?", (user['telegram_id'],)) as c:
            row = await c.fetchone()
            total_orders = row[0] or 0
            total_spent = row[1] or 0.0

    text = (
        f"👤 **MY PROFILE**\n\n"
        f"🆔 **ID:** `{user['telegram_id']}`\n"
        f"👤 **Name:** {user['first_name']}\n"
        f"🏷 **Username:** @{user['username']}\n"
        f"💰 **Balance:** ${user['balance']:.2f}\n"
        f"🛒 **Total Orders:** {total_orders}\n"
        f"💵 **Total Spent:** ${total_spent:.2f}\n"
        f"📅 **Registered:** {user['created_at'][:10]}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=back_home_buttons())
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    text = (
        "❓ **HELP & SUPPORT**\n\n"
        "• To buy Eggs or Chickens, select the category from Main Menu.\n"
        "• Ensure your wallet balance is funded before purchasing.\n"
        "• Stock is delivered instantly upon order confirmation.\n"
        "• Contact support @AdminSupport for payment issues."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=back_home_buttons())
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ==========================================
# BUY FLOW (EGGS & CHICKEN)
# ==========================================

@router.callback_query(F.data.startswith("buy_cat:"))
async def cb_select_category(callback: CallbackQuery):
    p_type = callback.data.split(":")[1] # 'egg' or 'chicken'
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM countries WHERE is_enabled = 1") as cursor:
            countries = await cursor.fetchall()

    if not countries:
        await callback.answer("No countries currently available.", show_alert=True)
        return

    buttons = []
    for c in countries:
        buttons.append([InlineKeyboardButton(
            text=f"{c['flag']} {c['name']}",
            callback_data=f"buy_country:{p_type}:{c['id']}",
            style="primary"
        )])
    buttons.extend(back_home_buttons())
    
    title = "🥚 SELECT EGG COUNTRY" if p_type == "egg" else "🍗 SELECT CHICKEN COUNTRY"
    await callback.message.edit_text(title, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("buy_country:"))
async def cb_list_products(callback: CallbackQuery):
    _, p_type, country_id = callback.data.split(":")
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM countries WHERE id = ?", (country_id,)) as c_cur:
            country = await c_cur.fetchone()
        
        async with db.execute(
            "SELECT * FROM products WHERE type = ? AND country_id = ? AND status = 'available' AND stock > 0",
            (p_type, country_id)
        ) as p_cur:
            products = await p_cur.fetchall()

    if not products:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data=f"buy_cat:{p_type}", style="danger")]])
        await callback.message.edit_text(f"❌ No available {p_type} listings for {country['flag']} {country['name']}.", reply_markup=kb)
        return

    text = f"<b>{country['flag']} {country['name'].upper()} {p_type.upper()} MARKET</b>\n\n"
    buttons = []
    for p in products:
        text += (
            f"🆔 <b>ID:</b> {p['product_id']}\n"
            f"✨ <b>Quality:</b> {p['quality']}\n"
            f"💰 <b>Price:</b> ${p['price']:.2f}\n"
            f"📦 <b>Stock:</b> {p['stock']} available\n"
            f"--------------------------------\n"
        )
        buttons.append([InlineKeyboardButton(
            text=f"BUY {p['product_id']} (${p['price']:.2f})",
            callback_data=f"view_prod:{p['product_id']}",
            style="success"
        )])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"buy_cat:{p_type}", style="danger")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("view_prod:"))
async def cb_view_product(callback: CallbackQuery):
    prod_id = callback.data.split(":")[1]
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.*, c.name as country_name, c.flag as country_flag 
            FROM products p 
            JOIN countries c ON p.country_id = c.id 
            WHERE p.product_id = ?
        """, (prod_id,)) as cursor:
            p = await cursor.fetchone()

    if not p or p['status'] != 'available' or p['stock'] <= 0:
        await callback.answer("❌ This product is out of stock or unavailable.", show_alert=True)
        return

    text = (
        f"<b>Product Details</b>\n\n"
        f"<b>ID:</b> {p['product_id']}\n"
        f"<b>Type:</b> {p['type'].upper()}\n"
        f"<b>Country:</b> {p['country_flag']} {p['country_name']}\n"
        f"<b>Quality:</b> {p['quality']}\n"
        f"<b>Price:</b> ${p['price']:.2f}\n"
        f"<b>Description:</b> {p['description'] or 'N/A'}\n"
    )
    
    buttons = [
        [InlineKeyboardButton(text="🛒 BUY NOW", callback_data=f"confirm_buy:{p['product_id']}", style="success")],
        [InlineKeyboardButton(text="⬅️ BACK", callback_data=f"buy_country:{p['type']}:{p['country_id']}", style="danger")]
    ]
    
    if p['image_file_id']:
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=p['image_file_id'],
            caption=text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@router.callback_query(F.data.startswith("confirm_buy:"))
async def cb_confirm_buy(callback: CallbackQuery):
    prod_id = callback.data.split(":")[1]
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.*, c.name as country_name, c.flag as country_flag 
            FROM products p 
            JOIN countries c ON p.country_id = c.id 
            WHERE p.product_id = ?
        """, (prod_id,)) as cursor:
            p = await cursor.fetchone()

    if not p or p['status'] != 'available' or p['stock'] <= 0:
        await callback.answer("❌ Product no longer available.", show_alert=True)
        return

    if user['balance'] < p['price']:
        text = (
            f"❌ **Insufficient Balance**\n\n"
            f"Required: ${p['price']:.2f}\n"
            f"Your Balance: ${user['balance']:.2f}\n\n"
            f"Please recharge your wallet to proceed."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 RECHARGE", callback_data="wallet_recharge", style="success")],
            [InlineKeyboardButton(text="⬅️ BACK", callback_data=f"view_prod:{prod_id}", style="danger")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        return

    text = (
        f"🛒 **ORDER CONFIRMATION**\n\n"
        f"Product: {p['type'].capitalize()}\n"
        f"Country: {p['country_flag']} {p['country_name']}\n"
        f"Quality: {p['quality']}\n"
        f"Product ID: `{p['product_id']}`\n"
        f"Price: **${p['price']:.2f}**\n\n"
        f"Confirm purchase?"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM", callback_data=f"exec_buy:{prod_id}", style="success")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="main_menu", style="danger")]
    ])
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("exec_buy:"))
async def cb_execute_buy(callback: CallbackQuery):
    prod_id = callback.data.split(":")[1]
    user_id = callback.from_user.id

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            
            async with db.execute("SELECT balance FROM users WHERE telegram_id = ?", (user_id,)) as u_cur:
                user = await u_cur.fetchone()
            
            async with db.execute("SELECT * FROM products WHERE product_id = ? AND status = 'available'", (prod_id,)) as p_cur:
                p = await p_cur.fetchone()

            if not p or p['stock'] <= 0:
                await db.execute("ROLLBACK")
                await callback.answer("❌ Purchase failed: Product is out of stock.", show_alert=True)
                return

            if user['balance'] < p['price']:
                await db.execute("ROLLBACK")
                await callback.answer("❌ Purchase failed: Insufficient balance.", show_alert=True)
                return

            new_balance = user['balance'] - p['price']
            await db.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (new_balance, user_id))

            new_stock = p['stock'] - 1
            new_status = 'sold' if new_stock == 0 else 'available'
            await db.execute("UPDATE products SET stock = ?, status = ? WHERE product_id = ?", (new_stock, new_status, prod_id))

            order_id = f"ORD-{random.randint(100000, 999999)}"
            await db.execute(
                "INSERT INTO orders (order_id, user_id, product_id, product_type, amount, status) VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, user_id, prod_id, p['type'], p['price'], "completed")
            )

            tx_id = f"TX-{random.randint(100000, 999999)}"
            await db.execute(
                "INSERT INTO wallet_transactions (transaction_id, user_id, amount, type, description) VALUES (?, ?, ?, ?, ?)",
                (tx_id, user_id, -p['price'], "purchase", f"Bought product {prod_id}")
            )

            await db.commit()

            text = (
                f"✅ **ORDER SUCCESSFUL**\n\n"
                f"Order ID: `{order_id}`\n"
                f"Product ID: `{prod_id}`\n"
                f"Quality: {p['quality']}\n"
                f"Amount Paid: **${p['price']:.2f}**\n\n"
                f"💰 Remaining Balance: **${new_balance:.2f}**"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 MY ORDERS", callback_data="my_orders", style="primary")],
                [InlineKeyboardButton(text="🏠 MAIN MENU", callback_data="main_menu", style="primary")]
            ])
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

            for admin_id in settings.ADMIN_IDS:
                try:
                    await callback.bot.send_message(
                        admin_id,
                        f"🚨 **NEW SALE**\n\nOrder ID: `{order_id}`\nUser: `{user_id}`\nProduct: `{prod_id}`\nAmount: ${p['price']:.2f}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

        except Exception as e:
            await db.execute("ROLLBACK")
            logger.error(f"Purchase Error: {e}")
            await callback.answer("❌ An error occurred processing your transaction.", show_alert=True)

# ==========================================
# WALLET RECHARGE
# ==========================================

@router.callback_query(F.data == "wallet_recharge")
async def cb_wallet_recharge(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RechargeFSM.enter_amount)
    await callback.message.edit_text(
        "💳 **WALLET RECHARGE**\n\nEnter the amount in USD ($) you wish to add to your wallet:",
        parse_mode="Markdown"
    )

@router.message(RechargeFSM.enter_amount)
async def process_recharge(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Invalid amount. Enter a numeric value greater than 0.")
        return

    await state.clear()
    pay_id = f"PAY-{random.randint(100000, 999999)}"
    
    async with get_db() as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (amount, message.from_user.id))
        await db.execute(
            "INSERT INTO wallet_transactions (transaction_id, user_id, amount, type, description) VALUES (?, ?, ?, ?, ?)",
            (pay_id, message.from_user.id, amount, "deposit", f"Recharged via Payment Gateway API ({pay_id})")
        )
        await db.commit()

    text = (
        f"✅ **PAYMENT VERIFIED & SUCCESSFUL**\n\n"
        f"Transaction ID: `{pay_id}`\n"
        f"Amount Credited: **${amount:.2f}**\n\n"
        f"Your balance has been updated!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=back_home_buttons())
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

# ==========================================
# ORDERS HISTORY
# ==========================================

@router.callback_query(F.data == "my_orders")
async def cb_my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT o.*, p.type, c.flag, c.name as country_name 
            FROM orders o 
            JOIN products p ON o.product_id = p.product_id 
            JOIN countries c ON p.country_id = c.id 
            WHERE o.user_id = ? 
            ORDER BY o.created_at DESC LIMIT 10
        """, (user_id,)) as cursor:
            orders = await cursor.fetchall()

    if not orders:
        kb = InlineKeyboardMarkup(inline_keyboard=back_home_buttons())
        await callback.message.edit_text("📦 You have no order history yet.", reply_markup=kb)
        return

    text = "📦 **MY ORDERS**\n\n"
    for o in orders:
        text += (
            f"🆔 `{o['order_id']}` | {o['flag']} {o['type'].capitalize()}\n"
            f"💰 Amount: ${o['amount']:.2f} | Status: ✅ {o['status']}\n"
            f"📅 Date: {o['created_at'][:16]}\n"
            f"--------------------------------\n"
        )
    
    kb = InlineKeyboardMarkup(inline_keyboard=back_home_buttons())
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ==========================================
# ADMIN: ADD PRODUCT WORKFLOW
# ==========================================

@router.callback_query(F.data.startswith("admin:add_product:"))
async def cb_admin_add_prod(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    p_type = callback.data.split(":")[2]
    await state.update_data(p_type=p_type)

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM countries") as cursor:
            countries = await cursor.fetchall()

    buttons = [[InlineKeyboardButton(text=f"{c['flag']} {c['name']}", callback_data=f"admin_sel_c:{c['id']}", style="primary")] for c in countries]
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="admin_panel", style="danger")])

    await state.set_state(AddProductFSM.select_country)
    await callback.message.edit_text(f"➕ **ADD {p_type.upper()}**\n\nSelect Country:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data.startswith("admin_sel_c:"), StateFilter(AddProductFSM.select_country))
async def cb_admin_sel_country(callback: CallbackQuery, state: FSMContext):
    c_id = int(callback.data.split(":")[1])
    await state.update_data(country_id=c_id)
    await state.set_state(AddProductFSM.enter_id)
    await callback.message.edit_text("Enter Product ID (e.g., EG-IN-10001):")

@router.message(StateFilter(AddProductFSM.enter_id))
async def process_prod_id(message: Message, state: FSMContext):
    prod_id = message.text.strip().upper()
    await state.update_data(prod_id=prod_id)
    await state.set_state(AddProductFSM.enter_price)
    await message.answer("Enter Price in USD (e.g. 5.00):")

@router.message(StateFilter(AddProductFSM.enter_price))
async def process_prod_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
    except ValueError:
        await message.answer("❌ Invalid price. Enter a number.")
        return
    await state.update_data(price=price)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥚 Fresh", callback_data="qual:Fresh", style="success")],
        [InlineKeyboardButton(text="💔 Broken", callback_data="qual:Broken", style="danger")],
    ])
    await state.set_state(AddProductFSM.select_quality)
    await message.answer("Select Quality:", reply_markup=kb)

@router.callback_query(F.data.startswith("qual:"), StateFilter(AddProductFSM.select_quality))
async def cb_select_quality(callback: CallbackQuery, state: FSMContext):
    qual = callback.data.split(":")[1]
    await state.update_data(quality=qual)
    await state.set_state(AddProductFSM.enter_stock)
    await callback.message.edit_text("Enter Stock Quantity (e.g., 10):")

@router.message(StateFilter(AddProductFSM.enter_stock))
async def process_prod_stock(message: Message, state: FSMContext):
    try:
        stock = int(message.text)
    except ValueError:
        await message.answer("❌ Enter a valid integer.")
        return
    await state.update_data(stock=stock)
    await state.set_state(AddProductFSM.enter_description)
    await message.answer("Enter Product Description (or type 'none'):")

@router.message(StateFilter(AddProductFSM.enter_description))
async def process_prod_desc(message: Message, state: FSMContext):
    desc = message.text if message.text.lower() != 'none' else ""
    await state.update_data(description=desc)
    await state.set_state(AddProductFSM.upload_photo)
    await message.answer("Send a Photo for this product, or send 'skip':")

@router.message(StateFilter(AddProductFSM.upload_photo))
async def process_prod_photo(message: Message, state: FSMContext):
    image_id = message.photo[-1].file_id if message.photo else None
    await state.update_data(image_file_id=image_id)

    data = await state.get_data()
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, flag FROM countries WHERE id = ?", (data['country_id'],)) as c:
            country = await c.fetchone()

    preview = (
        f"📋 **EGG PREVIEW**\n\n"
        f"ID: `{data['prod_id']}`\n"
        f"Country: {country['flag']} {country['name']}\n"
        f"Price: ${data['price']:.2f}\n"
        f"Quality: {data['quality']}\n"
        f"Stock: {data['stock']}\n"
        f"Description: {data['description'] or 'N/A'}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ PUBLISH", callback_data="pub_prod", style="success")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="admin_panel", style="danger")]
    ])
    await state.set_state(AddProductFSM.preview)
    await message.answer(preview, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "pub_prod", StateFilter(AddProductFSM.preview))
async def cb_publish_prod(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with get_db() as db:
        try:
            await db.execute("""
                INSERT INTO products (product_id, type, country_id, price, quality, description, image_file_id, stock)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (data['prod_id'], data['p_type'], data['country_id'], data['price'], data['quality'], data['description'], data['image_file_id'], data['stock']))
            await db.commit()
            await callback.message.edit_text("✅ Product successfully published!")
        except Exception as e:
            await callback.message.edit_text(f"❌ Error adding product (ID may already exist): {e}")
    await state.clear()

# ==========================================
# ADMIN: PRODUCT & COUNTRY MANAGEMENT
# ==========================================

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await callback.message.edit_text("👨‍💻 **ADMIN PANEL**", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "admin:manage_countries")
async def cb_manage_countries(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM countries") as c:
            countries = await c.fetchall()

    buttons = []
    for country in countries:
        status_icon = "🟢" if country['is_enabled'] else "🔴"
        style_color = "success" if country['is_enabled'] else "danger"
        buttons.append([InlineKeyboardButton(
            text=f"{status_icon} {country['flag']} {country['name']}",
            callback_data=f"admin_toggle_c:{country['id']}",
            style=style_color
        )])
    
    buttons.append([InlineKeyboardButton(text="➕ ADD COUNTRY", callback_data="admin_add_country", style="primary")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="admin_panel", style="danger")])
    
    await callback.message.edit_text("🌍 **COUNTRY MANAGEMENT**\nClick a country to toggle Enable/Disable status:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data.startswith("admin_toggle_c:"))
async def cb_toggle_country(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    c_id = callback.data.split(":")[1]
    async with get_db() as db:
        await db.execute("UPDATE countries SET is_enabled = NOT is_enabled WHERE id = ?", (c_id,))
        await db.commit()
    await cb_manage_countries(callback)

# ==========================================
# ADMIN: STATS & USERS
# ==========================================

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1:
            total_users = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(*), SUM(amount) FROM orders") as c2:
            row = await c2.fetchone()
            total_orders = row[0] or 0
            total_revenue = row[1] or 0.0

    text = (
        f"📊 **STATISTICS DASHBOARD**\n\n"
        f"👥 **Total Users:** {total_users}\n"
        f"🛒 **Total Orders:** {total_orders}\n"
        f"💰 **Total Revenue:** ${total_revenue:.2f}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin_panel", style="danger")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ==========================================
# APPLICATION ENTRY POINT
# ==========================================

async def main():
    await init_db()
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot execution started.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
