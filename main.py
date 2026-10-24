import asyncio
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from shutil import copyfileobj

import uvicorn
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand

from tuman.bot import build_dispatcher
from tuman.config import Settings
from tuman.db import Store
from tuman.network import EnvironmentSession
from tuman.web import create_app
from tuman.workers import backups, notifications


def prepare_environment_file(project_root=None):
    """Recover .env from a nested ZIP extraction without overwriting user files."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parent
    target = root / ".env"
    if target.exists() or target.is_symlink():
        if not target.is_file():
            raise RuntimeError(".env рядом с main.py должен быть текстовым файлом, а не папкой.")
        return target

    # Only recover from the known double-directory ZIP layout, e.g.
    # Desktop/--main/--main. Do not pick up an unrelated parent's credentials.
    if root.name != root.parent.name:
        return None
    source = root.parent / ".env"
    if not source.exists() and not source.is_symlink():
        return None
    if not source.is_file():
        raise RuntimeError(".env во внешней папке должен быть текстовым файлом. Создайте файл с настройками, а не папку.")

    with source.open("rb") as original:
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another launch may have completed the same recovery.
            if not target.is_file():
                raise RuntimeError(".env рядом с main.py должен быть текстовым файлом.") from None
            return target
        try:
            with os.fdopen(descriptor, "wb") as recovered:
                copyfileobj(original, recovered)
        except BaseException:
            # This launch created the file, so it alone may remove its partial copy.
            target.unlink(missing_ok=True)
            raise
    logging.info("Файл .env скопирован из внешней папки к main.py. Исходный файл сохранён.")
    return target


@contextmanager
def instance_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another instance is already running for this database") from None
    try:
        yield
    finally:
        handle.close()


async def run(settings):
    if not settings.bot_token and not settings.web_only:
        raise RuntimeError("Set BOT_TOKEN securely, or explicitly set WEB_ONLY=1 for local development")
    store = Store(settings.db_path)
    app = create_app(settings, store)
    if not store.one("SELECT id FROM accounts WHERE role='owner' AND enabled=1"):
        raise RuntimeError("Create the owner account first: python -m tuman.cli create-admin --role owner --login owner")
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info", access_log=False, proxy_headers=True, forwarded_allow_ips=settings.proxy_ips)
    server = uvicorn.Server(config)
    if not settings.web_only and not settings.has_public_web_url():
        logging.warning("Сайт пока работает локально: кнопки входа из Telegram появятся после настройки публичного HTTPS-адреса в PUBLIC_URL. Остальные функции бота доступны.")
    bot, tasks = None, []
    try:
        if not settings.web_only:
            bot = Bot(settings.bot_token, session=EnvironmentSession(), default=DefaultBotProperties(parse_mode="HTML"))
            info = await bot.get_me()
            with store.connection(True) as db:
                db.execute("INSERT INTO settings(key,value) VALUES ('bot_username',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (info.username,))
            await bot.set_my_commands([BotCommand(command=c, description=d) for c,d in [("start","Оғоз / Начать"),("my","Борҳо / Мои посылки"),("language","Забон / Язык"),("cancel","Бекор / Отмена"),("website","Ҳисоб / Личный кабинет")]])
        tasks = [asyncio.create_task(server.serve()), asyncio.create_task(backups(store))]
        if bot:
            tasks += [asyncio.create_task(build_dispatcher(store, settings).start_polling(bot, handle_signals=False, close_bot_session=False)), asyncio.create_task(notifications(store, bot))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if not server.started:
            raise RuntimeError("Web server failed to start")
    finally:
        server.should_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prepare_environment_file()
    settings = Settings.from_env()
    with instance_lock(settings.db_path.with_suffix(".lock")):
        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            pass
        except TelegramNetworkError:
            logging.error("Не удалось подключиться к Telegram после повторных попыток. Проверьте интернет и доступ к Telegram, затем повторите запуск. Токен в лог не выводится.")
            raise SystemExit(1) from None
        except TelegramUnauthorizedError:
            logging.error("Telegram отклонил токен бота. Проверьте BOT_TOKEN в .env, не отправляя его в чат.")
            raise SystemExit(1) from None
