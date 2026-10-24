"""Isolated browser test application; never used by the production entry point."""
import os
from pathlib import Path

from tuman.config import Settings
from tuman.web import create_app
from .conftest import TEST_PASSWORD

db = os.environ.get("TUMAN_E2E_DB")
if not db:
    raise RuntimeError("Browser tests require an explicitly isolated database")
app = create_app(Settings(db_path=Path(db), public_url=f"http://127.0.0.1:{os.environ['TUMAN_E2E_PORT']}", web_only=True, passwords={r:TEST_PASSWORD for r in ("owner","china","tajik")}))
