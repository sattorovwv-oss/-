"""Configure Google login on the VPS using only the Python standard library.

Client Secret is read from the terminal without echo. Existing application
settings, bot credentials and passwords are preserved; the previous .env is
backed up privately before an atomic replacement.
"""
import argparse
import getpass
import os
from pathlib import Path
import re
import tempfile
import time


def configure(root, client_id, client_secret):
    root = Path(root)
    target = root / ".env"
    if not (root / "compose.yaml").is_file() or not (root / "main.py").is_file():
        raise ValueError("Папка проекта не найдена.")
    if target.is_symlink() or not target.is_file():
        raise ValueError("Сначала создайте обычный файл .env в папке проекта.")
    if not re.fullmatch(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
        raise ValueError("Проверьте полный Google Client ID: он заканчивается на .apps.googleusercontent.com.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", client_secret):
        raise ValueError("Проверьте Client Secret: вставьте его целиком без пробелов.")
    previous = target.read_bytes()
    lines = previous.decode("utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not re.match(r"\s*(?:export\s+)?GOOGLE_CLIENT_(?:ID|SECRET)\s*=", line)]
    text = "".join(kept)
    if text and not text.endswith(("\n", "\r")):
        text += "\n"
    text += f"GOOGLE_CLIENT_ID='{client_id}'\nGOOGLE_CLIENT_SECRET='{client_secret}'\n"
    backup = root / (".env.before-google-" + str(time.time_ns()))
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(previous)
    descriptor, temporary = tempfile.mkstemp(prefix=".env.google-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default="")
    args = parser.parse_args()
    client_id = args.client_id.strip() or input("Google Client ID: ").strip()
    secret = getpass.getpass("Google Client Secret (ввод скрыт): ").strip()
    try:
        backup = configure(Path(__file__).resolve().parent.parent, client_id, secret)
    except (ValueError, OSError, UnicodeError):
        print("Настройка не завершена. Проверьте Client ID, Client Secret и доступ к .env. Секрет в чат не отправляйте.")
        raise SystemExit(1) from None
    print("Настройки Google сохранены. Резервная копия предыдущего .env:", backup.name)
    print("Теперь пересоздайте app: docker compose up -d --build app")


if __name__ == "__main__":
    main()
