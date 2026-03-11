"""
Authentication service — JWT + Telegram MTProto login flow.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.models import TelegramAccount, User
from app.security import (
    hash_password,
    verify_password,
    validate_phone,
    validate_password_strength,
)
from app.telegram_client import build_client, decrypt_session, encrypt_session

logger = logging.getLogger(__name__)

_pending_clients: dict[str, object] = {}


# ── User CRUD ─────────────────────────────────────────────────────────────────

async def register_user(db: AsyncSession, email: str, password: str) -> User:
    error = validate_password_strength(password)
    if error:
        raise ValueError(error)
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    await db.flush()
    logger.info("Novo usuário registrado: %s", email)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        hash_password("dummy_timing_protection")
        logger.warning("Login com e-mail inexistente: %s", email)
        return None
    if not verify_password(password, user.password_hash):
        logger.warning("Senha incorreta para: %s", email)
        return None
    logger.info("Login bem-sucedido: %s", email)
    return user


# ── Telegram MTProto ──────────────────────────────────────────────────────────

async def send_login_code(phone_number: str) -> dict:
    phone_number = validate_phone(phone_number)
    client = build_client()
    await client.connect()
    try:
        await client.send_code_request(phone_number)
        _pending_clients[phone_number] = client
        logger.info("Código Telegram enviado para %s", phone_number)
        return {"detail": "Code sent successfully"}
    except FloodWaitError as e:
        await client.disconnect()
        logger.warning("FloodWait para %s: %ds", phone_number, e.seconds)
        raise ValueError(f"Muitas tentativas. Aguarde {e.seconds} segundos.")
    except Exception as exc:
        await client.disconnect()
        logger.error("Erro ao enviar código: %s", exc)
        raise


async def verify_login_code(
    db: AsyncSession,
    user_id: str,
    phone_number: str,
    code: str,
    password: str | None = None,
) -> TelegramAccount:
    phone_number = validate_phone(phone_number)
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
        logger.warning("Código inválido para %s", phone_number)
        raise ValueError("Código de verificação inválido.")
    except PhoneCodeExpiredError:
        logger.warning("Código expirado para %s", phone_number)
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
    logger.info("Conta Telegram conectada para user %s phone %s", user_id, phone_number)
    return account


async def get_active_account(db: AsyncSession, user_id: str) -> TelegramAccount | None:
    """Retorna a conta mais recentemente usada."""
    result = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.user_id == user_id)
        .order_by(TelegramAccount.last_used_at.desc())
    )
    return result.scalars().first()


async def get_decrypted_session(db: AsyncSession, user_id: str) -> str | None:
    """Sessão da conta mais recente."""
    account = await get_active_account(db, user_id)
    if account:
        return decrypt_session(account.session_string_encrypted)
    return None


async def get_decrypted_session_by_phone(
    db: AsyncSession,
    user_id: str,
    phone_number: str | None = None,
) -> str | None:
    """
    Retorna a sessão descriptografada de uma conta específica pelo telefone.
    Se phone_number não informado, usa a mais recente.
    """
    if phone_number:
        try:
            phone_number = validate_phone(phone_number)
        except ValueError:
            pass
        result = await db.execute(
            select(TelegramAccount).where(
                TelegramAccount.user_id == user_id,
                TelegramAccount.phone_number == phone_number,
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            logger.warning("Sessão não encontrada para phone %s user %s", phone_number, user_id)
            return None
        return decrypt_session(account.session_string_encrypted)

    return await get_decrypted_session(db, user_id)


async def list_telegram_accounts(db: AsyncSession, user_id: str) -> list[TelegramAccount]:
    """Lista todas as contas Telegram do usuário."""
    result = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.user_id == user_id)
        .order_by(TelegramAccount.connected_at.desc())
    )
    return list(result.scalars().all())


async def remove_telegram_account(db: AsyncSession, user_id: str, phone_number: str) -> None:
    """Remove uma conta Telegram do usuário."""
    from sqlalchemy import delete
    try:
        phone_number = validate_phone(phone_number)
    except ValueError:
        pass

    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == user_id,
            TelegramAccount.phone_number == phone_number,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise ValueError("Conta não encontrada.")

    await db.execute(
        delete(TelegramAccount).where(TelegramAccount.id == account.id)
    )
    logger.info("Conta %s removida pelo usuário %s", phone_number, user_id)
