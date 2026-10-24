import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from .bot import ticket_keyboard

logger = logging.getLogger(__name__)


async def deliver_once(store, bot):
    """Single consumer. DB outbox provides at-least-once delivery, not exactly-once."""
    for message in await asyncio.to_thread(store.due_messages):
        text = await asyncio.to_thread(store.prepare_message, message)
        if text is None:
            continue
        markup = None
        if message["event_key"].startswith("ticket_notice:"):
            markup = ticket_keyboard(int(message["event_key"].split(":")[1]))
        try:
            await bot.send_message(message["chat_id"], text, parse_mode="HTML", reply_markup=markup)
        except TelegramRetryAfter as exc:
            await asyncio.to_thread(store.finish_message, message["id"], "TelegramRetryAfter", False, exc.retry_after+1)
            # Telegram rate limits commonly apply to the whole bot. Stop this batch.
            break
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            await asyncio.to_thread(store.finish_message, message["id"], type(exc).__name__, True)
        except Exception as exc:
            # Never put a request URL, Telegram token, or full transport exception in the DB/log.
            await asyncio.to_thread(store.finish_message, message["id"], type(exc).__name__)
        else:
            await asyncio.to_thread(store.finish_message, message["id"])
        await asyncio.sleep(0.05)


async def notifications(store, bot):
    while True:
        try:
            await deliver_once(store, bot)
        except Exception as exc:
            logger.error("Notification worker failed: %s", type(exc).__name__)
        await asyncio.sleep(0.5)


async def backups(store):
    while True:
        try:
            await asyncio.to_thread(store.backup)
        except Exception as exc:
            logger.error("Backup failed: %s", type(exc).__name__)
        await asyncio.sleep(6*3600)
