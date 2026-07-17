import os
import base64
import logging
import sys
import aiosqlite

# --- ENVIRONMENT CONFIGURATION ---
API_ID = int(os.getenv("TG_API_ID", "34043431"))
API_HASH = os.getenv("TG_API_HASH", "1b35dae0978194f1088cb6168b70779c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8788369960:AAHoheW7W5tpcRdqxa4Nekx4rxefc_4z3YY")

SUPER_OWNER_IDS = [7952327997, 7953147643, 8064493735] 
SECRET_KEY = os.getenv("ENCRYPTION_KEY", "pydroid_secure_fallback_key_2026")
LOG_CHANNEL_ID = -1003929609682

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MultiAccountSystem")

# --- DATABASE CLASS ---
class Database:
    def __init__(self, db_path: str = "bot_core_data.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, username TEXT, role TEXT DEFAULT 'user', 
                    max_accounts INTEGER DEFAULT 5, referred_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY, user_id INTEGER, username TEXT, session_string TEXT,
                    status TEXT DEFAULT 'active', last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER, type TEXT, payload TEXT, 
                    status TEXT DEFAULT 'pending', progress TEXT DEFAULT '0%', success_report TEXT, failure_report TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def log_action(self, user_id: int, action: str, bot_instance=None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO logs (user_id, action) VALUES (?, ?)", (user_id, action))
                await db.commit()
        except Exception as db_err:
            logger.error(f"Failed to log action: {db_err}")
        
        if bot_instance:
            try:
                log_text = f"📝 **System Audit Log**\n👤 **User ID:** `{user_id}`\n⚡ **Action:** {action}"
                await bot_instance.send_message(chat_id=LOG_CHANNEL_ID, text=log_text)
            except Exception as e:
                logger.error(f"Failed sending log updates: {e}")

    async def get_user_role(self, user_id: int) -> str:
        if user_id in SUPER_OWNER_IDS:
            return "super_owner"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else "user"

    async def create_user_if_not_exists(self, user_id: int, username: str, referred_by: int = None):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
                if not await cursor.fetchone():
                    await db.execute("INSERT INTO users (user_id, username, role, referred_by) VALUES (?, ?, 'user', ?)", (user_id, username, referred_by))
                    await db.commit()

# Create the shared instance right here!
db_mgr = Database()

# --- CRYPTO HELPERS ---
def _get_crypto_key() -> int:
    return sum(ord(c) for c in SECRET_KEY) % 256 or 42

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
