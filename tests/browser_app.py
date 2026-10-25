"""Isolated browser test application; never used by the production entry point."""
import os
from pathlib import Path

from tuman.config import Settings
from tuman.web import create_app
from .conftest import TEST_PASSWORD

db = os.environ.get("TUMAN_E2E_DB")
if not db:
    raise RuntimeError("Browser tests require an explicitly isolated database")
settings = Settings(db_path=Path(db), public_url=f"http://127.0.0.1:{os.environ['TUMAN_E2E_PORT']}", web_only=True, passwords={r:TEST_PASSWORD for r in ("owner","china","tajik")})
if os.environ.get("TUMAN_E2E_GOOGLE") == "1":
    # This module requires an isolated E2E database and is excluded from Docker.
    # Production has no provider-mocking flag or endpoint.
    from tuman import google_auth
    settings.google_client_id = "123456-browser-tests.apps.googleusercontent.com"
    settings.google_client_secret = "GOCSPX-only-for-browser-tests"

    async def fake_google(settings, code, verifier):
        if code not in ("new-client", "existing-client"):
            raise google_auth.GoogleOAuthError("Invalid test code")
        return {"sub": "browser-" + code, "email": code + "@example.com", "email_verified": True, "name": "Browser Google Client"}

    google_auth.exchange_code = fake_google
app = create_app(settings)
