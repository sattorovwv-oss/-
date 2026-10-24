"""Use standard HTTPS proxy/CA configuration, including cloud-injected bindings.

TLS verification remains enabled. aiogram 3.31's default session ignores proxy
environment variables and explicitly selects certifi; this adapter respects the
machine's trusted CA configuration without extracting any proxy credentials.
"""
import asyncio
import logging
import ssl

from aiohttp import ClientSession
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage, SetMyCommands

logger = logging.getLogger(__name__)


class RetryTelegramNetwork(BaseRequestMiddleware):
    """Retry only API transport failures; never rerun a business handler or polling.

    Telegram sendMessage has no idempotency key. An ambiguous timeout can produce
    a duplicate reply, the same at-least-once limitation as the durable outbox.
    """
    def __init__(self, retries=2, delay=0.25):
        self.retries = retries
        self.delay = delay

    async def __call__(self, make_request, bot, method):
        if not isinstance(method, (SendMessage, GetMe, SetMyCommands, AnswerCallbackQuery)):
            return await make_request(bot, method)
        for attempt in range(self.retries + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError:
                if attempt == self.retries:
                    raise
                # Full exception text can include access links or request URLs.
                logger.warning("Telegram network interruption for %s; retry %s/%s", type(method).__name__, attempt + 1, self.retries)
                await asyncio.sleep(self.delay * (2 ** attempt))


class EnvironmentSession(AiohttpSession):
    def __init__(self, **kwargs):
        kwargs.setdefault("timeout", 20)
        super().__init__(**kwargs)
        self._connector_init["ssl"] = ssl.create_default_context()
        self.middleware.register(RetryTelegramNetwork())

    async def create_session(self):
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(connector=self._connector_type(**self._connector_init), trust_env=True)
            self._should_reset_connector = False
        return self._session
