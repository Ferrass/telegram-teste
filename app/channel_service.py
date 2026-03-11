"""
Channel discovery service.

Uses the user's Telethon session to list dialogs and filters for
channels/supergroups where the user has admin rights.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Channel as TgChannel, ChatAdminRights

from app.auth_service import get_decrypted_session
from app.models import Channel
from app.telegram_client import get_client

logger = logging.getLogger(__name__)


async def sync_admin_channels(db: AsyncSession, user_id: int) -> list[Channel]:
    """
    Fetch all Telegram dialogs for the user, keep only channels/supergroups
    where they are an admin, persist to DB, and return the list.
    """
    session_string = await get_decrypted_session(db, user_id)
    if not session_string:
        raise ValueError("No connected Telegram account. Complete phone verification first.")

    admin_channels: list[dict] = []

    async with get_client(session_string) as client:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, TgChannel):
                continue  # skip DMs and legacy groups

            # Fetch full channel info to check admin rights
            try:
                full = await client.get_entity(entity.id)
                participant = await client.get_permissions(full)
                if not participant.is_admin:
                    continue
            except Exception as exc:
                logger.debug("Skipping channel %s: %s", entity.id, exc)
                continue

            admin_channels.append(
                {
                    "channel_id": entity.id,
                    "channel_name": entity.title,
                    "username": getattr(entity, "username", None),
                    "member_count": getattr(entity, "participants_count", 0) or 0,
                }
            )

    # Upsert channels into DB
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
    logger.info("Synced %d admin channels for user %s", len(saved), user_id)
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
        raise ValueError(f"Channel {channel_id} not found or not owned by user.")
    return channel
