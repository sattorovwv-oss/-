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


class ConfigurationError(ValueError):
    """A fixed, user-facing message which never contains configuration values."""


def clean_input(value):
    value = value.strip()
    # Some Windows SSH terminals paste these wrappers into hidden input.
    if value.startswith("\x1b[200~") and value.endswith("\x1b[201~"):
        value = value[6:-6].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def configure(root, client_id, client_secret):
    client_id, client_secret = clean_input(client_id), clean_input(client_secret)
    root = Path(root)
    target = root / ".env"
    if not (root / "compose.yaml").is_file() or not (root / "main.py").is_file():
        raise ConfigurationError("Папка проекта не найдена.")
    if target.is_symlink() or not target.is_file():
        raise ConfigurationError("Не найден обычный файл /opt/tuman-kargo/.env. Проверьте путь и тип файла.")
    if not re.fullmatch(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
        raise ConfigurationError("Проверьте полный Google Client ID: он заканчивается на .apps.googleusercontent.com.")
    if not client_secret:
        raise ConfigurationError("Client Secret не введён. Скопируйте его из настроек OAuth-клиента и вставьте в скрытый запрос.")
    if client_secret.endswith(".apps.googleusercontent.com"):
        raise ConfigurationError("Вместо Client Secret вставлен Client ID. Нужен отдельный Client Secret из настроек OAuth-клиента.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", client_secret):
        raise ConfigurationError("Формат Client Secret не принят. Вставьте только значение секрета целиком, без названия поля, пробелов и переносов строк.")
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
    print("Нужен отдельный Client Secret из Google Cloud — он часто начинается с GOCSPX-.")
    secret = getpass.getpass("Google Client Secret (ввод скрыт): ")
    try:
        backup = configure(Path(__file__).resolve().parent.parent, client_id, secret)
    except ConfigurationError as error:
        print("Настройка не завершена:", str(error))
        raise SystemExit(1) from None
    except UnicodeError:
        print("Настройка не завершена: файл .env должен быть сохранён в UTF-8. Его содержимое в чат не отправляйте.")
        raise SystemExit(1) from None
    except PermissionError:
        print("Настройка не завершена: нет доступа к .env или папке проекта. Запустите команду от root на сервере.")
        raise SystemExit(1) from None
    except OSError as error:
        # strerror excludes paths and any file content, unlike the full exception.
        print("Файловая ошибка настройки:", error.strerror or "неизвестная ошибка", "(код", error.errno, ")")
        raise SystemExit(1) from None
    print("Настройки Google сохранены. Резервная копия предыдущего .env:", backup.name)
    print("Теперь пересоздайте app: docker compose up -d --build app")


if __name__ == "__main__":
    main()
