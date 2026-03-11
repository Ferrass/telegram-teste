"""
Reusable Telethon client service.

Each user has their own MTProto session stored (encrypted) in the database.
We load the StringSession at runtime and build a TelegramClient on demand.
"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Encryption helpers ───────────────────────────────────────────────────────

_fernet = Fernet(settings.encryption_key.encode() if not settings.encryption_key.startswith("b'") else eval(settings.encryption_key))


def encrypt_session(session_string: str) -> str:
    """Encrypt a Telethon StringSession before DB storage."""
    return _fernet.encrypt(session_string.encode()).decode()


def decrypt_session(encrypted: str) -> str:
    """Decrypt a session string retrieved from the DB."""
    return _fernet.decrypt(encrypted.encode()).decode()


# ─── Client factory ───────────────────────────────────────────────────────────

def build_client(session_string: str | None = None) -> TelegramClient:
    """
    Build a TelegramClient.

    Pass *session_string* (plain, already decrypted) to resume an existing
    session, or omit it to create an anonymous in-memory session.
    """
    session = StringSession(session_string) if session_string else StringSession()
    client = TelegramClient(
        session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        connection_retries=3,
        retry_delay=1,
        auto_reconnect=True,
    )
    return client


@asynccontextmanager
async def get_client(session_string: str | None = None) -> AsyncGenerator[TelegramClient, None]:
    """
    Async context manager that connects and disconnects a TelegramClient.

    Usage::

        async with get_client(session_string=plain_session) as client:
            me = await client.get_me()
    """
    client = build_client(session_string)
    try:
        await client.connect()
        yield client
    finally:
        if client.is_connected():
            await client.disconnect()
