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
# CONFIGURATION CONSTANTS
# ==========================================
UPI_QR_IMAGE_URL = "https://i.ibb.co/example/your-upi-qr.png"  # 👈 Replace with your actual ImgBB URL
USDT_TRC20_ADDRESS = "TYourTRC20WalletAddressHere"            # 👈 Replace with your TRC20 Address
TON_ADDRESS = "EQYourTonWalletAddressHere"                     # 👈 Replace with your TON Address

# ==========================================
# DATABASE INITIALIZATION
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                seller_id INTEGER NOT NULL, -- 0 for Admin, Telegram ID for Users
                type TEXT NOT NULL, -- 'egg' or 'chicken'
                country_id INTEGER NOT NULL,
                price REAL NOT NULL,
                quality TEXT NOT NULL, -- 'Fresh Egg', 'Broken Egg', 'Fresh Chicken', 'Broken Chicken'
                description TEXT DEFAULT '',
                stock INTEGER DEFAULT 1,
                status TEXT DEFAULT 'available',
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
                amount REAL NOT NULL,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Pending Top-ups
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topups (
                topup_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            ]
            await db.executemany(
                "INSERT INTO countries (code, name, flag, is_enabled) VALUES (?, ?, ?, ?)",
                default_countries
            )
        
        await db.commit()

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

class RechargeFSM(StatesGroup):
    enter_amount = State()
    select_method = State()
    upload_proof = State()

class AddCountryFSM(StatesGroup):
    enter_name = State()
    enter_flag = State()
    enter_code = State()

# ==========================================
# KEYBOARD BUILDERS
# ==========================================

def get_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="🥚 BUY EGGS", callback_data="buy_cat:egg", style="primary"),
            InlineKeyboardButton(text="🍗 BUY CHICKENS", callback_data="buy_cat:chicken", style="primary")
        ],
        [
            InlineKeyboardButton(text="➕ SELL MY ITEM", callback_data="user_sell_item", style="success"),
            InlineKeyboardButton(text="💳 WALLET TOP-UP", callback_data="wallet_topup", style="success")
        ],
        [
            InlineKeyboardButton(text="📦 MY ORDERS", callback_data="my_orders"),
            InlineKeyboardButton(text="👤 PROFILE", callback_data="my_profile")
        ]
    ]
    if user_id in settings.ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="👨‍💻 ADMIN PANEL", callback_data="admin_panel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Add Product", callback_data="admin_add_prod", style="success"),
            InlineKeyboardButton(text="🌍 Add Country", callback_data="admin_add_country", style="success")
        ],
        [
            InlineKeyboardButton(text="📦 Manage Products", callback_data="admin_manage_prods", style="primary"),
            InlineKeyboardButton(text="🌍 Manage Countries", callback_data="admin_manage_countries", style="primary")
        ],
        [
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="danger")
        ]
    ])

def back_home_buttons() -> List[List[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="🏠 MAIN MENU", callback_data="main_menu", style="primary")
        ]
    ]

# ==========================================
# USER & DB HELPERS
# ==========================================

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
    text = (
        f"🥚 **EGG & CHICKEN MARKETPLACE**\n\n"
        f"👤 **User:** {user['first_name']}\n"
        f"🆔 **ID:** `{user['telegram_id']}`\n"
        f"💰 **Balance:** ${user['balance']:.2f}\n\n"
        f"Select an option from below:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(message.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    text = (
        f"🥚 **EGG & CHICKEN MARKETPLACE**\n\n"
        f"👤 **User:** {user['first_name']}\n"
        f"🆔 **ID:** `{user['telegram_id']}`\n"
        f"💰 **Balance:** ${user['balance']:.2f}"
    )
    await callback.message.edit_text(text, reply_markup=get_main_keyboard(callback.from_user.id), parse_mode="Markdown")

# ==========================================
# BUY FLOW (EGGS & CHICKEN SEPARATE)
# ==========================================

@router.callback_query(F.data.startswith("buy_cat:"))
async def cb_select_category(callback: CallbackQuery):
    p_type = callback.data.split(":")[1]  # 'egg' or 'chicken'
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM countries WHERE is_enabled = 1") as cursor:
            countries = await cursor.fetchall()

    if not countries:
        await callback.answer("No countries available right now.", show_alert=True)
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
        await callback.message.edit_text(f"❌ No available {p_type}s for {country['flag']} {country['name']}.", reply_markup=kb)
        return

    text = f"<b>{country['flag']} {country['name'].upper()} {p_type.upper()} MARKET</b>\n\n"
    buttons = []
    for p in products:
        seller_type = "👑 Admin" if p['seller_id'] == 0 else f"👤 User ({p['seller_id']})"
        text += (
            f"🆔 <b>ID:</b> {p['product_id']}\n"
            f"✨ <b>Quality:</b> {p['quality']}\n"
            f"👤 <b>Seller:</b> {seller_type}\n"
            f"💰 <b>Price:</b> ${p['price']:.2f}\n"
            f"📦 <b>Stock:</b> {p['stock']}\n"
            f"--------------------------------\n"
        )
        buttons.append([InlineKeyboardButton(
            text=f"BUY {p['product_id']} (${p['price']:.2f})",
            callback_data=f"exec_buy:{p['product_id']}",
            style="success"
        )])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"buy_cat:{p_type}", style="danger")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

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
                await callback.answer("❌ Product out of stock or unavailable.", show_alert=True)
                return

            if user['balance'] < p['price']:
                await db.execute("ROLLBACK")
                await callback.answer(f"❌ Insufficient balance! Required: ${p['price']:.2f}", show_alert=True)
                return

            # Deduct balance & update stock
            new_balance = user['balance'] - p['price']
            await db.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (new_balance, user_id))

            new_stock = p['stock'] - 1
            new_status = 'sold' if new_stock == 0 else 'available'
            await db.execute("UPDATE products SET stock = ?, status = ? WHERE product_id = ?", (new_stock, new_status, prod_id))

            # If sold by another user, add balance to seller
            if p['seller_id'] != 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (p['price'], p['seller_id']))

            # Order entry
            order_id = f"ORD-{random.randint(100000, 999999)}"
            await db.execute(
                "INSERT INTO orders (order_id, user_id, product_id, amount, status) VALUES (?, ?, ?, ?, ?)",
                (order_id, user_id, prod_id, p['price'], "completed")
            )

            await db.commit()

            text = (
                f"✅ **PURCHASE SUCCESSFUL**\n\n"
                f"Order ID: `{order_id}`\n"
                f"Product ID: `{prod_id}`\n"
                f"Quality: {p['quality']}\n"
                f"Amount Paid: **${p['price']:.2f}**\n\n"
                f"💰 Remaining Balance: **${new_balance:.2f}**"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=back_home_buttons())
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

        except Exception as e:
            await db.execute("ROLLBACK")
            logger.error(f"Purchase Error: {e}")
            await callback.answer("❌ Transaction error.", show_alert=True)

# ==========================================
# WALLET TOP-UP & PROOF SUBMISSION
# ==========================================

@router.callback_query(F.data == "wallet_topup")
async def cb_wallet_topup(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RechargeFSM.enter_amount)
    await callback.message.edit_text("💳 **WALLET TOP-UP**\n\nEnter the amount in USD ($) you wish to add:", parse_mode="Markdown")

@router.message(StateFilter(RechargeFSM.enter_amount))
async def process_topup_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Enter a valid dollar amount.")
        return

    await state.update_data(amount=amount)
    await state.set_state(RechargeFSM.select_method)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇳 Pay with UPI (QR)", callback_data="pay_method:upi", style="primary")],
        [InlineKeyboardButton(text="💎 Pay with Crypto (USDT / TON)", callback_data="pay_method:crypto", style="primary")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu", style="danger")]
    ])
    await message.answer(f"💰 Amount to top-up: **${amount:.2f}**\n\nSelect Payment Method:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("pay_method:"), StateFilter(RechargeFSM.select_method))
async def cb_payment_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[1]
    data = await state.get_data()
    amount = data['amount']
    topup_id = f"TOP-{random.randint(100000, 999999)}"
    await state.update_data(topup_id=topup_id, method=method)

    if method == "upi":
        text = (
            f"🇮🇳 **UPI PAYMENT**\n\n"
            f"Top-Up ID: `{topup_id}`\n"
            f"Amount: **${amount:.2f}**\n\n"
            f"Scan the QR code photo below and pay the required amount.\n"
            f"After paying, click **I Paid** to upload proof."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ I Paid", callback_data=f"i_paid:{topup_id}", style="success")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu", style="danger")]
        ])
        await callback.message.delete()
        await callback.message.answer_photo(photo=UPI_QR_IMAGE_URL, caption=text, reply_markup=kb, parse_mode="Markdown")

    elif method == "crypto":
        text = (
            f"💎 **CRYPTO PAYMENT**\n\n"
            f"Top-Up ID: `{topup_id}`\n"
            f"Amount: **${amount:.2f}**\n\n"
            f"Send exactly **${amount:.2f}** equivalent to one of the addresses:\n\n"
            f"🟢 **USDT (TRC20):**\n`{USDT_TRC20_ADDRESS}`\n\n"
            f"🔵 **TON:**\n`{TON_ADDRESS}`\n\n"
            f"After sending, click **I Paid** below to send screenshot proof."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ I Paid", callback_data=f"i_paid:{topup_id}", style="success")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu", style="danger")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("i_paid:"))
async def cb_i_paid(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RechargeFSM.upload_proof)
    await callback.message.answer("📸 Please upload/send the screenshot photo of your payment transaction proof now:")

@router.message(StateFilter(RechargeFSM.upload_proof), F.photo)
async def process_proof_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_id = message.photo[-1].file_id
    topup_id = data.get('topup_id', f"TOP-{random.randint(100000, 999999)}")
    amount = data.get('amount', 0.0)

    async with get_db() as db:
        await db.execute(
            "INSERT INTO topups (topup_id, user_id, amount, method, status) VALUES (?, ?, ?, ?, ?)",
            (topup_id, message.from_user.id, amount, data.get('method', 'manual'), "pending")
        )
        await db.commit()

    # Alert Admins
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"admin_approve_topup:{topup_id}", style="success"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"admin_reject_topup:{topup_id}", style="danger")
        ]
    ])

    admin_text = (
        f"🚨 **NEW PAYMENT PROOF SUBMITTED**\n\n"
        f"Top-Up ID: `{topup_id}`\n"
        f"User ID: `{message.from_user.id}`\n"
        f"Username: @{message.from_user.username or 'N/A'}\n"
        f"Amount: **${amount:.2f}**"
    )

    for admin_id in settings.ADMIN_IDS:
        try:
            await message.bot.send_photo(admin_id, photo=photo_id, caption=admin_text, reply_markup=admin_kb, parse_mode="Markdown")
        except Exception:
            pass

    await state.clear()
    await message.answer("✅ Your payment proof has been submitted to Admin! Balance will update once verified.", reply_markup=InlineKeyboardMarkup(inline_keyboard=back_home_buttons()))

# ==========================================
# ADMIN TOP-UP APPROVAL
# ==========================================

@router.callback_query(F.data.startswith("admin_approve_topup:"))
async def cb_admin_approve_topup(callback: CallbackQuery):
    if callback.from_user.id not in settings.ADMIN_IDS: return
    topup_id = callback.data.split(":")[1]

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM topups WHERE topup_id = ? AND status = 'pending'", (topup_id,)) as c:
            topup = await c.fetchone()

        if not topup:
            await callback.answer("Top-up already processed or not found.", show_alert=True)
            return

        await db.execute("UPDATE topups SET status = 'approved' WHERE topup_id = ?", (topup_id,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (topup['amount'], topup['user_id']))
        await db.commit()

    try:
        await callback.bot.send_message(
            topup['user_id'],
            f"🎉 **PAYMENT APPROVED!**\n\nYour top-up of **${topup['amount']:.2f}** has been credited to your balance.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ **STATUS: APPROVED BY ADMIN**")

@router.callback_query(F.data.startswith("admin_reject_topup:"))
async def cb_admin_reject_topup(callback: CallbackQuery):
    if callback.from_user.id not in settings.ADMIN_IDS: return
    topup_id = callback.data.split(":")[1]

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM topups WHERE topup_id = ? AND status = 'pending'", (topup_id,)) as c:
            topup = await c.fetchone()

        if topup:
            await db.execute("UPDATE topups SET status = 'rejected' WHERE topup_id = ?", (topup_id,))
            await db.commit()
            try:
                await callback.bot.send_message(topup['user_id'], "❌ Your top-up proof was rejected by admin.")
            except Exception:
                pass

    await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ **STATUS: REJECTED BY ADMIN**")

# ==========================================
# ADD ITEM / PRODUCT WORKFLOW (USER & ADMIN)
# ==========================================

@router.callback_query(F.data.in_({"admin_add_prod", "user_sell_item"}))
async def cb_start_add_item(callback: CallbackQuery, state: FSMContext):
    is_admin = callback.data == "admin_add_prod"
    await state.update_data(seller_id=0 if is_admin else callback.from_user.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥚 Egg", callback_data="prod_type:egg", style="primary")],
        [InlineKeyboardButton(text="🍗 Chicken", callback_data="prod_type:chicken", style="primary")]
    ])
    await state.set_state(AddProductFSM.select_type)
    await callback.message.edit_text("Select Product Type to list:", reply_markup=kb)

@router.callback_query(F.data.startswith("prod_type:"), StateFilter(AddProductFSM.select_type))
async def cb_item_type(callback: CallbackQuery, state: FSMContext):
    p_type = callback.data.split(":")[1]
    await state.update_data(p_type=p_type)

    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM countries WHERE is_enabled = 1") as cursor:
            countries = await cursor.fetchall()

    buttons = [[InlineKeyboardButton(text=f"{c['flag']} {c['name']}", callback_data=f"prod_c:{c['id']}", style="primary")] for c in countries]
    await state.set_state(AddProductFSM.select_country)
    await callback.message.edit_text("Select Country of Origin:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("prod_c:"), StateFilter(AddProductFSM.select_country))
async def cb_item_country(callback: CallbackQuery, state: FSMContext):
    c_id = int(callback.data.split(":")[1])
    await state.update_data(country_id=c_id)
    
    data = await state.get_data()
    p_type = data['p_type']

    if p_type == "egg":
        qual_buttons = [
            [InlineKeyboardButton(text="🥚 Fresh Egg", callback_data="qual:Fresh Egg", style="success")],
            [InlineKeyboardButton(text="💔 Broken Egg", callback_data="qual:Broken Egg", style="danger")]
        ]
    else:
        qual_buttons = [
            [InlineKeyboardButton(text="🍗 Fresh Chicken", callback_data="qual:Fresh Chicken", style="success")],
            [InlineKeyboardButton(text="💔 Broken Chicken", callback_data="qual:Broken Chicken", style="danger")]
        ]

    await state.set_state(AddProductFSM.select_quality)
    await callback.message.edit_text("Select Quality:", reply_markup=InlineKeyboardMarkup(inline_keyboard=qual_buttons))

@router.callback_query(F.data.startswith("qual:"), StateFilter(AddProductFSM.select_quality))
async def cb_item_qual(callback: CallbackQuery, state: FSMContext):
    qual = callback.data.split(":")[1]
    await state.update_data(quality=qual)
    await state.set_state(AddProductFSM.enter_id)
    await callback.message.edit_text("Enter a unique Product ID Code (e.g. EG-US-001):")

@router.message(StateFilter(AddProductFSM.enter_id))
async def process_item_id(message: Message, state: FSMContext):
    prod_id = message.text.strip().upper()
    await state.update_data(prod_id=prod_id)
    await state.set_state(AddProductFSM.enter_price)
    await message.answer("Enter Price per unit in USD ($):")

@router.message(StateFilter(AddProductFSM.enter_price))
async def process_item_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Invalid price number.")
        return
    await state.update_data(price=price)
    await state.set_state(AddProductFSM.enter_stock)
    await message.answer("Enter Stock Quantity available:")

@router.message(StateFilter(AddProductFSM.enter_stock))
async def process_item_stock(message: Message, state: FSMContext):
    try:
        stock = int(message.text)
        if stock <= 0: raise ValueError()
    except ValueError:
        await message.answer("❌ Invalid quantity integer.")
        return

    data = await state.get_data()
    async with get_db() as db:
        try:
            await db.execute("""
                INSERT INTO products (product_id, seller_id, type, country_id, price, quality, stock)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (data['prod_id'], data['seller_id'], data['p_type'], data['country_id'], data['price'], data['quality'], stock))
            await db.commit()
            await message.answer("✅ Product listing created successfully!", reply_markup=InlineKeyboardMarkup(inline_keyboard=back_home_buttons()))
        except Exception as e:
            await message.answer(f"❌ Error adding item (ID might already exist): {e}")
    await state.clear()

# ==========================================
# ADMIN PANEL HANDLERS
# ==========================================

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in settings.ADMIN_IDS: return
    await callback.message.edit_text("👨‍💻 **ADMIN PANEL**", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

# ==========================================
# APPLICATION ENTRY POINT
# ==========================================

async def main():
    await init_db()
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot started successfully.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
