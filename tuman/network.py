"""Use standard HTTPS proxy/CA configuration, including cloud-injected bindings.

TLS verification remains enabled. aiogram 3.31's default session ignores proxy
environment variables and explicitly selects certifi; this adapter respects the
machine's trusted CA configuration without extracting any proxy credentials.
"""
import ssl

from aiohttp import ClientSession
from aiogram.client.session.aiohttp import AiohttpSession


class EnvironmentSession(AiohttpSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connector_init["ssl"] = ssl.create_default_context()

    async def create_session(self):
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(connector=self._connector_type(**self._connector_init), trust_env=True)
            self._should_reset_connector = False
        return self._session
