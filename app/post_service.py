"""
Post scheduling service — CRUD for ScheduledPost records.
Suporta múltiplas contas Telegram via channel.phone_number.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.channel_service import get_channel_or_raise
from app.models import PostStatus, ScheduledPost

logger = logging.getLogger(__name__)


async def schedule_post(
    db: AsyncSession,
    user_id: str,
    channel_id: str,
    message: str,
    scheduled_time: datetime,
    media_url: str | None = None,
) -> ScheduledPost:
    # Valida ownership — canal deve pertencer ao usuário
    channel = await get_channel_or_raise(db, channel_id, user_id)

    if scheduled_time <= datetime.now(timezone.utc):
        raise ValueError("scheduled_time deve ser no futuro.")

    post = ScheduledPost(
        user_id=user_id,
        channel_id=channel.id,
        message=message,
        media_url=media_url,
        scheduled_time=scheduled_time,
        status=PostStatus.scheduled,
    )
    db.add(post)
    await db.flush()
    logger.info(
        "Post %s agendado para user=%s canal=%s phone=%s em %s",
        post.id, user_id, channel.channel_name, channel.phone_number, scheduled_time
    )
    return post


async def get_user_posts(
    db: AsyncSession,
    user_id: str,
    status: PostStatus | None = None,
    phone_number: str | None = None,
) -> list[ScheduledPost]:
    """
    Lista posts do usuário.
    Se phone_number informado, filtra apenas posts dos canais daquele número.
    """
    q = (
        select(ScheduledPost)
        .where(ScheduledPost.user_id == user_id)
        .options(selectinload(ScheduledPost.channel))
    )

    if status:
        q = q.where(ScheduledPost.status == status)

    if phone_number:
        from app.models import Channel
        q = q.join(Channel, ScheduledPost.channel_id == Channel.id)\
             .where(Channel.phone_number == phone_number)

    q = q.order_by(ScheduledPost.scheduled_time)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_post_or_raise(db: AsyncSession, post_id: str, user_id: str) -> ScheduledPost:
    result = await db.execute(
        select(ScheduledPost)
        .where(
            ScheduledPost.id == post_id,
            ScheduledPost.user_id == user_id,
        )
        .options(selectinload(ScheduledPost.channel))
    )
    post = result.scalar_one_or_none()
    if not post:
        raise ValueError(f"Post {post_id} não encontrado.")
    return post


async def cancel_post(db: AsyncSession, post_id: str, user_id: str) -> ScheduledPost:
    post = await get_post_or_raise(db, post_id, user_id)
    if post.status != PostStatus.scheduled:
        raise ValueError(f"Não é possível cancelar um post com status '{post.status}'.")
    post.status = PostStatus.cancelled
    await db.flush()
    return post


async def get_pending_posts(db: AsyncSession) -> list[ScheduledPost]:
    """Posts prontos para envio — usado pelo worker scheduler."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScheduledPost)
        .where(
            ScheduledPost.status == PostStatus.scheduled,
            ScheduledPost.scheduled_time <= now,
        )
        .options(selectinload(ScheduledPost.channel))
        .order_by(ScheduledPost.scheduled_time)
    )
    return list(result.scalars().all())


async def get_sent_posts(db: AsyncSession) -> list[ScheduledPost]:
    """Posts enviados com ID do Telegram — usado pelo worker de métricas."""
    result = await db.execute(
        select(ScheduledPost)
        .where(
            ScheduledPost.status == PostStatus.sent,
            ScheduledPost.telegram_message_id.is_not(None),
        )
        .options(selectinload(ScheduledPost.channel))
    )
    return list(result.scalars().all())
