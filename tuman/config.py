from dataclasses import dataclass, field
from pathlib import Path
from ipaddress import ip_address
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    db_path: Path = ROOT / "var/tuman.db"
    public_url: str = "http://localhost:8000"
    production: bool = False
    bot_token: str = ""
    web_only: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    proxy_ips: str = "127.0.0.1"
    passwords: dict = field(default_factory=dict, repr=False)
    telegram_ids: dict = field(default_factory=dict)
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)

    @property
    def google_enabled(self):
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def google_callback_url(self):
        return self.public_url.rstrip("/") + "/auth/google/callback"

    def has_public_web_url(self):
        """Check URL suitability for Telegram buttons, without a DNS/network call."""
        try:
            parsed = urlsplit(self.public_url)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            if any(char.isspace() or ord(char) < 32 for char in self.public_url):
                return False
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                return False
            # Accessing port validates malformed/out-of-range explicit ports.
            parsed.port
            if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
                return False
            if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example")):
                return False
            try:
                return ip_address(hostname).is_global
            except ValueError:
                return "." in hostname
        except ValueError:
            return False

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        path = Path(os.getenv("DB_PATH", "var/tuman.db"))
        s = cls(
            db_path=path if path.is_absolute() else ROOT / path,
            public_url=os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            production=os.getenv("APP_ENV") == "production",
            bot_token=os.getenv("BOT_TOKEN", ""),
            web_only=os.getenv("WEB_ONLY") == "1",
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
            proxy_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            passwords={r: os.getenv(r.upper()+"_PASSWORD", "") for r in ("owner", "china", "tajik")},
            telegram_ids={r: [int(x.strip()) for x in os.getenv(r.upper()+"_TELEGRAM_IDS", "").split(",") if x.strip()] for r in ("owner", "china", "tajik")},
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
        )
        u = urlsplit(s.public_url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError("PUBLIC_URL must be an absolute origin, without credentials, path or query")
        if s.production and u.scheme != "https":
            raise ValueError("Production requires an HTTPS PUBLIC_URL")
        if s.production and s.web_only:
            raise ValueError("WEB_ONLY is for development, not production")
        return s
