"""
Metrics collection service.

Fetches live view/forward counts from Telegram for every sent post and
stores a snapshot in post_metrics_history.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.functions.channels import GetMessagesRequest

from app.auth_service import get_decrypted_session
from app.database import AsyncSessionLocal
from app.models import PostMetricsHistory, ScheduledPost
from app.post_service import get_sent_posts
from app.telegram_client import get_client

logger = logging.getLogger(__name__)


async def collect_post_metrics(db: AsyncSession, post: ScheduledPost) -> tuple[int, int]:
    """
    Retrieve views and forwards for a single sent post via Telethon.
    Returns (views, forwards).
    """
    session_string = await get_decrypted_session(db, post.user_id)
    if not session_string:
        logger.warning("No session for user %s – skipping metrics", post.user_id)
        return post.views, post.forwards

    async with get_client(session_string) as client:
        channel = post.channel
        try:
            entity = await client.get_entity(channel.channel_id)
            messages = await client(
                GetMessagesRequest(
                    channel=entity,
                    id=[post.telegram_message_id],
                )
            )
            if not messages.messages:
                return post.views, post.forwards

            msg = messages.messages[0]
            views = getattr(msg, "views", 0) or 0
            forwards = getattr(msg, "forwards", 0) or 0
            return views, forwards
        except Exception as exc:
            logger.error("Failed to fetch metrics for post %s: %s", post.id, exc)
            return post.views, post.forwards


async def run_metrics_collection() -> None:
    """
    Called by the metrics worker every N minutes.
    Iterates over all sent posts, collects fresh metrics, saves snapshots.
    """
    async with AsyncSessionLocal() as db:
        posts = await get_sent_posts(db)
        logger.info("Collecting metrics for %d posts", len(posts))

        for post in posts:
            try:
                views, forwards = await collect_post_metrics(db, post)

                # Update running totals on the post itself
                post.views = views
                post.forwards = forwards

                # Insert a history snapshot
                snapshot = PostMetricsHistory(
                    post_id=post.id,
                    views=views,
                    forwards=forwards,
                    captured_at=datetime.now(timezone.utc),
                )
                db.add(snapshot)
            except Exception as exc:
                logger.error("Error processing metrics for post %s: %s", post.id, exc)

        await db.commit()
        logger.info("Metrics collection complete")
