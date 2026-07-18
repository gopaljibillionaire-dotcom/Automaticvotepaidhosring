import os
import sys
import logging

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MultiAccountSystem")

# --- CREDENTIALS ---
API_ID = int(os.getenv("TG_API_ID", "34043431"))
API_HASH = os.getenv("TG_API_HASH", "1b35dae0978194f1088cb6168b70779c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8788369960:AAHoheW7W5tpcRdqxa4Nekx4rxefc_4z3YY")

# HARDCODED SUPER-OWNER IDS
SUPER_OWNER_IDS = [7952327997, 7953147643, 8064493735, 6294187729] 

# DEVELOPER ATTRIBUTIONS
DESIGNER_HANDLE = "Gopalji_choubey"
MANAGER_HANDLE = "BMWM4Z"

# CRYPTO KEY FOR LOCAL DATABASE OBFUSCATION
SECRET_KEY = os.getenv("ENCRYPTION_KEY", "secure_fallback_key_2026")

# AUDIT CHANNEL FOR TELEGRAM LOG EVENTS
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003929609682"))
