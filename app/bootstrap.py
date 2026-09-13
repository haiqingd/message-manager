"""Create a local configuration with fresh secrets."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def main() -> None:
    path = Path(".env")
    if path.exists():
        print(f"{path.resolve()} already exists; no secrets were changed.")
        return
    values = {
        "ADMIN_TOKEN": secrets.token_urlsafe(36),
        "MESSAGE_API_KEY": secrets.token_urlsafe(36),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "DATABASE_PATH": "./data/messages.db",
        "HOST": "127.0.0.1",
        "PORT": "8000",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        for key, value in values.items():
            file.write(f"{key}={value}\n")
    print(f"Created {path.resolve()} (mode 0600). Keep its keys private.")
    print("Use ADMIN_TOKEN to sign in to the dashboard and MESSAGE_API_KEY for sending applications.")


if __name__ == "__main__":
    main()
