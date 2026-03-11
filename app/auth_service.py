"""
Authentication service — JWT + Telegram MTProto login flow.
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

from app.config import settings
from app.models import TelegramAccount, User
from app.telegram_client import build_client, decrypt_session, encrypt_session

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Pending clients aguardando verify-code (phone → client)
# Em produção multi-processo, substitua por Redis
_pending_clients: dict[str, object] = {}


# ─── Password ─────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─── JWT ──────────────────────────────────────────────────────────────────────

def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.secret_key, algorithm="HS256")

def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return payload["sub"]  # UUID string
    except (JWTError, KeyError):
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
    client = build_client()
    await client.connect()
    try:
        await client.send_code_request(phone_number)
        _pending_clients[phone_number] = client
        logger.info("Código enviado para %s", phone_number)
        return {"detail": "Code sent successfully"}
    except FloodWaitError as e:
        await client.disconnect()
        raise ValueError(f"Muitas tentativas. Aguarde {e.seconds} segundos.") from e
    except Exception:
        await client.disconnect()
        raise


async def verify_login_code(
    db: AsyncSession,
    user_id: str,
    phone_number: str,
    code: str,
    password: str | None = None,
) -> TelegramAccount:
    client = _pending_clients.get(phone_number)
    if client is None:
        raise ValueError("Nenhum login pendente para este número. Solicite o código primeiro.")

    try:
        await client.sign_in(phone=phone_number, code=code)
    except SessionPasswordNeededError:
        if not password:
            raise ValueError("Autenticação em dois fatores ativa. Informe sua senha 2FA.")
        await client.sign_in(password=password)
    except PhoneCodeInvalidError:
        raise ValueError("Código de verificação inválido.")
    except PhoneCodeExpiredError:
        raise ValueError("Código expirado. Solicite um novo.")
    finally:
        _pending_clients.pop(phone_number, None)

    session_string = client.session.save()
    await client.disconnect()

    encrypted = encrypt_session(session_string)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == user_id,
            TelegramAccount.phone_number == phone_number,
        )
    )
    account = result.scalar_one_or_none()

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
    logger.info("Conta Telegram conectada para user %s", user_id)
    return account


async def get_active_account(db: AsyncSession, user_id: str) -> TelegramAccount | None:
    result = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.user_id == user_id)
        .order_by(TelegramAccount.last_used_at.desc())
    )
    return result.scalars().first()


async def get_decrypted_session(db: AsyncSession, user_id: str) -> str | None:
    account = await get_active_account(db, user_id)
    if account:
        return decrypt_session(account.session_string_encrypted)
    return None
