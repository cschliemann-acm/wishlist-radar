"""Secrets and deployment settings come from environment variables (or .env).

Everyday preferences (sizes, marketplace, check frequency, notifications) live in the
database and are edited on the Settings page instead.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Add it to .env (or the host's environment variables).")
    return value


@dataclass(frozen=True)
class Config:
    # Password for the web interface
    app_password: str = field(default_factory=lambda: _required("APP_PASSWORD"))
    # Bearer token for the iPhone Shortcut (POST /api/ingest)
    ingest_token: str = field(default_factory=lambda: _required("INGEST_TOKEN"))
    # Signs the login cookie. Defaults to something derived from the password.
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))

    # eBay developer app (https://developer.ebay.com/my/keys), production keyset
    ebay_client_id: str = field(default_factory=lambda: os.getenv("EBAY_CLIENT_ID", ""))
    ebay_client_secret: str = field(default_factory=lambda: os.getenv("EBAY_CLIENT_SECRET", ""))

    # Claude: identification of items in posts, and judging listings
    identify_model: str = field(default_factory=lambda: os.getenv("IDENTIFY_MODEL", "claude-opus-5-5"))
    match_model: str = field(default_factory=lambda: os.getenv("MATCH_MODEL", "claude-haiku-5-5"))

    # Speech-to-text for reels: "off", "groq" or "openai" (+ TRANSCRIBE_API_KEY)
    transcribe: str = field(default_factory=lambda: os.getenv("TRANSCRIBE", "off").strip().lower())
    transcribe_api_key: str = field(default_factory=lambda: os.getenv("TRANSCRIBE_API_KEY", "").strip())

    # Mailbox for saved-search alert emails (see emailbox.py)
    alerts_imap_host: str = field(default_factory=lambda: os.getenv("ALERTS_IMAP_HOST", "imap.gmail.com").strip())
    alerts_imap_user: str = field(default_factory=lambda: os.getenv("ALERTS_IMAP_USER", "").strip())
    alerts_imap_password: str = field(default_factory=lambda: os.getenv("ALERTS_IMAP_PASSWORD", "").replace(" ", ""))

    instagram_cookies_file: str = field(default_factory=lambda: os.getenv("INSTAGRAM_COOKIES_FILE", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    # Turn off the background checker (e.g. while developing)
    scheduler_enabled: bool = field(
        default_factory=lambda: os.getenv("SCHEDULER", "on").lower() not in {"off", "0", "false"}
    )

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wishlist.db"


def load_config() -> Config:
    c = Config()
    c.data_dir.mkdir(parents=True, exist_ok=True)
    c.images_dir.mkdir(parents=True, exist_ok=True)
    c.imports_dir.mkdir(parents=True, exist_ok=True)
    if not c.secret_key:
        c = replace(c, secret_key=hashlib.sha256(f"wishlist-radar:{c.app_password}".encode()).hexdigest())
    cookies_b64 = os.getenv("INSTAGRAM_COOKIES_B64", "").strip()
    if cookies_b64 and not c.instagram_cookies_file:
        path = c.data_dir / "instagram_cookies.txt"
        path.write_bytes(base64.b64decode(cookies_b64))
        path.chmod(0o600)
        c = replace(c, instagram_cookies_file=str(path))
    return c
