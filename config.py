import os
import base64
import logging
import sys

# --- ENVIRONMENT CONFIGURATION ---
API_ID = int(os.getenv("TG_API_ID", "34043431"))
API_HASH = os.getenv("TG_API_HASH", "1b35dae0978194f1088cb6168b70779c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8788369960:AAHoheW7W5tpcRdqxa4Nekx4rxefc_4z3YY")

# Hardcoded Matrix
SUPER_OWNER_IDS = [7952327997, 7953147643, 8064493735] 
SECRET_KEY = os.getenv("ENCRYPTION_KEY", "pydroid_secure_fallback_key_2026")
LOG_CHANNEL_ID = -1003929609682

# --- LOGGING SETUPS ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MultiAccountSystem")

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
