import os
from dataclasses import dataclass

from cryptography.fernet import Fernet
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    admin_token: str
    message_api_key: str
    encryption_key: bytes
    database_path: str


def get_settings() -> Settings:
    load_dotenv()
    admin_token = os.getenv("ADMIN_TOKEN", "")
    message_api_key = os.getenv("MESSAGE_API_KEY", "")
    encryption_key = os.getenv("ENCRYPTION_KEY", "").encode()
    if len(admin_token) < 24 or len(message_api_key) < 24 or admin_token == message_api_key:
        raise RuntimeError("Set distinct, long ADMIN_TOKEN and MESSAGE_API_KEY values. Run `python -m app.bootstrap`.")
    try:
        Fernet(encryption_key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Set a valid ENCRYPTION_KEY. Run `python -m app.bootstrap`.") from exc
    return Settings(admin_token, message_api_key, encryption_key, os.getenv("DATABASE_PATH", "./data/messages.db"))
