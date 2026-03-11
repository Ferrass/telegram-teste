"""
Post scheduling service — CRUD for ScheduledPost records.
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
    user_id: int,
    channel_id: int,
    message: str,
    scheduled_time: datetime,
    media_url: str | None = None,
) -> ScheduledPost:
    # Validate ownership
    channel = await get_channel_or_raise(db, channel_id, user_id)

    if scheduled_time <= datetime.now(timezone.utc):
        raise ValueError("scheduled_time must be in the future.")

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
    logger.info("Scheduled post %s for user %s at %s", post.id, user_id, scheduled_time)
    return post


async def get_user_posts(
    db: AsyncSession,
    user_id: int,
    status: PostStatus | None = None,
) -> list[ScheduledPost]:
    q = select(ScheduledPost).where(ScheduledPost.user_id == user_id)
    if status:
        q = q.where(ScheduledPost.status == status)
    q = q.options(selectinload(ScheduledPost.channel)).order_by(ScheduledPost.scheduled_time)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_post_or_raise(db: AsyncSession, post_id: int, user_id: int) -> ScheduledPost:
    result = await db.execute(
        select(ScheduledPost).where(
            ScheduledPost.id == post_id,
            ScheduledPost.user_id == user_id,
        )
    )
    post = result.scalar_one_or_none()
    if not post:
        raise ValueError(f"Post {post_id} not found.")
    return post


async def cancel_post(db: AsyncSession, post_id: int, user_id: int) -> ScheduledPost:
    post = await get_post_or_raise(db, post_id, user_id)
    if post.status != PostStatus.scheduled:
        raise ValueError(f"Cannot cancel a post with status '{post.status}'.")
    post.status = PostStatus.cancelled
    await db.flush()
    return post


async def get_pending_posts(db: AsyncSession) -> list[ScheduledPost]:
    """Used by the scheduler worker to find posts due for delivery."""
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
    """Used by the metrics worker."""
    result = await db.execute(
        select(ScheduledPost)
        .where(
            ScheduledPost.status == PostStatus.sent,
            ScheduledPost.telegram_message_id.is_not(None),
        )
        .options(selectinload(ScheduledPost.channel))
    )
    return list(result.scalars().all())
