import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Channel as TgChannel

from app.auth_service import get_decrypted_session
from app.models import Channel
from app.telegram_client import get_client

logger = logging.getLogger(__name__)


async def sync_admin_channels(db: AsyncSession, user_id: int) -> list[Channel]:
    session_string = await get_decrypted_session(db, user_id)
    if not session_string:
        raise ValueError("No connected Telegram account.")

    admin_channels: list[dict] = []

    async with get_client(session_string) as client:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity

            # Aceita Channel e Supergrupo
            if not isinstance(entity, TgChannel):
                continue

            # Tenta verificar admin de várias formas
            is_admin = False

            try:
                # Forma 1: creator direto
                if getattr(entity, 'creator', False):
                    is_admin = True

                # Forma 2: admin_rights no entity
                elif getattr(entity, 'admin_rights', None) is not None:
                    is_admin = True

                # Forma 3: get_permissions
                else:
                    perms = await client.get_permissions(entity)
                    is_admin = getattr(perms, 'is_admin', False) or getattr(perms, 'is_creator', False)

            except Exception as exc:
                logger.debug("Erro ao checar permissão do canal %s: %s", getattr(entity, 'title', entity.id), exc)
                # Se não conseguiu checar, inclui mesmo assim se for creator
                is_admin = getattr(entity, 'creator', False)

            if not is_admin:
                continue

            admin_channels.append({
                "channel_id": entity.id,
                "channel_name": entity.title,
                "username": getattr(entity, "username", None),
                "member_count": getattr(entity, "participants_count", 0) or 0,
            })

            logger.info("Canal admin encontrado: %s (id=%s)", entity.title, entity.id)

    logger.info("Total de canais admin encontrados: %d", len(admin_channels))

    # Upsert no banco
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
        else:
            channel = Channel(user_id=user_id, **ch)
            db.add(channel)
        saved.append(channel)

    await db.flush()
    return saved


async def get_user_channels(db: AsyncSession, user_id: int) -> list[Channel]:
    result = await db.execute(
        select(Channel).where(Channel.user_id == user_id).order_by(Channel.channel_name)
    )
    return list(result.scalars().all())


async def get_channel_or_raise(db: AsyncSession, channel_id: int, user_id: int) -> Channel:
    result = await db.execute(
        select(Channel).where(Channel.id == channel_id, Channel.user_id == user_id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise ValueError(f"Canal {channel_id} não encontrado.")
    return channel