"""
Channel discovery service.
Suporta múltiplas contas via phone_number opcional.
Salva e filtra canais pelo número de telefone da conta.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Channel as TgChannel

from app.auth_service import get_decrypted_session_by_phone
from app.models import Channel
from app.telegram_client import get_client

logger = logging.getLogger(__name__)


async def sync_admin_channels(
    db: AsyncSession,
    user_id: str,
    phone: str | None = None,
) -> list[Channel]:
    """
    Sincroniza canais admin do Telegram.
    Salva o phone_number junto ao canal para controle por conta.
    """
    session_string = await get_decrypted_session_by_phone(db, user_id, phone)
    if not session_string:
        raise ValueError("Conta Telegram não encontrada. Conecte a conta primeiro.")

    admin_channels: list[dict] = []

    async with get_client(session_string) as client:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, TgChannel):
                continue

            is_admin = False
            try:
                if getattr(entity, 'creator', False):
                    is_admin = True
                elif getattr(entity, 'admin_rights', None) is not None:
                    is_admin = True
                else:
                    perms = await client.get_permissions(entity)
                    is_admin = getattr(perms, 'is_admin', False) or getattr(perms, 'is_creator', False)
            except Exception as exc:
                logger.debug("Erro ao checar permissão do canal %s: %s", getattr(entity, 'title', entity.id), exc)
                is_admin = getattr(entity, 'creator', False)

            if not is_admin:
                continue

            admin_channels.append({
                "channel_id": entity.id,
                "channel_name": entity.title,
                "username": getattr(entity, "username", None),
                "member_count": getattr(entity, "participants_count", 0) or 0,
                "phone_number": phone,  # vincula o canal ao número da conta
            })
            logger.info("Canal admin encontrado: %s (id=%s phone=%s)", entity.title, entity.id, phone)

    logger.info("Total: %d canais para user=%s phone=%s", len(admin_channels), user_id, phone)

    saved: list[Channel] = []
    for ch in admin_channels:
        result = await db.execute(
            select(Channel).where(
                Channel.user_id == user_id,
                Channel.channel_id == ch["channel_id"],
            )
        )
        channel = result.scalar_one_or_none()
        if channel:
            channel.channel_name = ch["channel_name"]
            channel.username = ch["username"]
            channel.member_count = ch["member_count"]
            channel.phone_number = ch["phone_number"]
        else:
            channel = Channel(user_id=user_id, **ch)
            db.add(channel)
        saved.append(channel)

    await db.flush()
    return saved


async def get_user_channels(
    db: AsyncSession,
    user_id: str,
    phone: str | None = None,
) -> list[Channel]:
    """
    Lista canais do usuário.
    Se phone informado, retorna só os canais daquele número.
    Se não informado, retorna todos os canais do usuário.
    """
    query = select(Channel).where(Channel.user_id == user_id)

    if phone:
        query = query.where(Channel.phone_number == phone)

    query = query.order_by(Channel.channel_name)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_channel_or_raise(db: AsyncSession, channel_id: str, user_id: str) -> Channel:
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id, Channel.user_id == user_id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise ValueError("Canal não encontrado ou sem permissão.")
    return channel
