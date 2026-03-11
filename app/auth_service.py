"""
Authentication service — JWT-based user auth + Telegram MTProto login flow.

Telegram login is a two-step challenge:
  1. send_code  → Telegram sends an SMS / app notification
  2. verify_code → user submits the code; we persist the StringSession
"""
import logging
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from app.config import settings
from app.models import TelegramAccount, User
from app.telegram_client import build_client, decrypt_session, encrypt_session

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Temporary in-process store for pending Telethon clients (phone → client).
# In production replace with Redis or a persistent store.
_pending_clients: dict[str, object] = {}


# ─── Password helpers ─────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─── JWT helpers ──────────────────────────────────────────────────────────────

def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        return None


# ─── User CRUD ────────────────────────────────────────────────────────────────

async def register_user(db: AsyncSession, email: str, password: str) -> User:
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    await db.flush()
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user and verify_password(password, user.password_hash):
        return user
    return None


# ─── Telegram MTProto login ───────────────────────────────────────────────────

async def send_login_code(phone_number: str) -> dict:
    """
    Start a Telegram login: send verification code to the given phone.
    Keeps the TelegramClient alive in memory until verify_code is called.
    """
    client = build_client()
    await client.connect()

    try:
        await client.send_code_request(phone_number)
        _pending_clients[phone_number] = client
        logger.info("Login code sent to %s", phone_number)
        return {"detail": "Code sent successfully"}
    except FloodWaitError as e:
        await client.disconnect()
        raise ValueError(f"Too many attempts. Wait {e.seconds} seconds.") from e
    except Exception:
        await client.disconnect()
        raise


async def verify_login_code(
    db: AsyncSession,
    user_id: int,
    phone_number: str,
    code: str,
    password: str | None = None,
) -> TelegramAccount:
    """
    Complete Telegram login with the verification code.
    Persists the encrypted StringSession in the DB.
    """
    client = _pending_clients.get(phone_number)
    if client is None:
        raise ValueError("No pending login for this phone number. Call send-code first.")

    try:
        await client.sign_in(phone=phone_number, code=code)
    except SessionPasswordNeededError:
        if not password:
            raise ValueError(
                "Two-factor authentication is enabled. Provide your 2FA password."
            )
        await client.sign_in(password=password)
    except PhoneCodeInvalidError:
        raise ValueError("Invalid verification code.")
    except PhoneCodeExpiredError:
        raise ValueError("Verification code has expired. Request a new one.")
    finally:
        # Always clean up the pending slot
        _pending_clients.pop(phone_number, None)

    session_string = client.session.save()
    await client.disconnect()

    encrypted = encrypt_session(session_string)

    # Upsert – one account per phone number per user
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == user_id,
            TelegramAccount.phone_number == phone_number,
        )
    )
    account = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if account:
        account.session_string_encrypted = encrypted
        account.last_used_at = now
    else:
        account = TelegramAccount(
            user_id=user_id,
            phone_number=phone_number,
            session_string_encrypted=encrypted,
            connected_at=now,
        )
        db.add(account)

    await db.flush()
    logger.info("Telegram account connected for user %s", user_id)
    return account


async def get_active_account(db: AsyncSession, user_id: int) -> TelegramAccount | None:
    result = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.user_id == user_id)
        .order_by(TelegramAccount.last_used_at.desc())
    )
    return result.scalars().first()


async def get_decrypted_session(db: AsyncSession, user_id: int) -> str | None:
    account = await get_active_account(db, user_id)
    if account:
        return decrypt_session(account.session_string_encrypted)
    return None
