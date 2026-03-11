"""
Background worker process.

Runs two independent loops:
  • Scheduler  — fires due posts every SCHEDULER_INTERVAL_SECONDS (default 60s)
  • Metrics    — collects view/forward stats every METRICS_INTERVAL_SECONDS (default 1800s)

Start with:
    python -m app.worker
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.auth_service import get_decrypted_session
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.metrics_service import run_metrics_collection
from app.models import PostStatus, ScheduledPost
from app.post_service import get_pending_posts
from app.telegram_client import get_client

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = asyncio.Event()


# ─── Scheduler loop ───────────────────────────────────────────────────────────

async def send_post(post: ScheduledPost) -> None:
    """Send a single scheduled post via the owner's Telethon session."""
    async with AsyncSessionLocal() as db:
        session_string = await get_decrypted_session(db, post.user_id)
        if not session_string:
            logger.error("No Telegram session for user %s – marking post %s as failed", post.user_id, post.id)
            await db.execute(
                update(ScheduledPost)
                .where(ScheduledPost.id == post.id)
                .values(status=PostStatus.failed)
            )
            await db.commit()
            return

        async with get_client(session_string) as client:
            try:
                entity = await client.get_entity(post.channel.channel_id)

                if post.media_url:
                    sent = await client.send_file(entity, post.media_url, caption=post.message)
                else:
                    sent = await client.send_message(entity, post.message)

                msg_id = sent.id
                await db.execute(
                    update(ScheduledPost)
                    .where(ScheduledPost.id == post.id)
                    .values(
                        status=PostStatus.sent,
                        telegram_message_id=msg_id,
                    )
                )
                await db.commit()
                logger.info("Post %s sent to channel %s (msg_id=%s)", post.id, post.channel.channel_id, msg_id)

            except Exception as exc:
                logger.error("Failed to send post %s: %s", post.id, exc)
                await db.execute(
                    update(ScheduledPost)
                    .where(ScheduledPost.id == post.id)
                    .values(status=PostStatus.failed)
                )
                await db.commit()


async def scheduler_loop() -> None:
    logger.info("Scheduler worker started (interval=%ds)", settings.scheduler_interval_seconds)
    while not _shutdown.is_set():
        try:
            async with AsyncSessionLocal() as db:
                pending = await get_pending_posts(db)

            if pending:
                logger.info("Found %d post(s) to send", len(pending))
                results = await asyncio.gather(*[send_post(p) for p in pending], return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.error("Erro ao enviar post: %s", r, exc_info=r)
        except Exception as exc:
            logger.error("Scheduler error: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                asyncio.shield(_shutdown.wait()),
                timeout=settings.scheduler_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass
        # swallow TimeoutError – it just means we woke up normally


async def metrics_loop() -> None:
    logger.info("Metrics worker started (interval=%ds)", settings.metrics_interval_seconds)
    while not _shutdown.is_set():
        try:
            await run_metrics_collection()
        except Exception as exc:
            logger.error("Metrics collection error: %s", exc)

        await asyncio.wait_for(
            asyncio.shield(_shutdown.wait()),
            timeout=settings.metrics_interval_seconds,
        )


# ─── Graceful shutdown ────────────────────────────────────────────────────────

def _handle_signal(sig: signal.Signals) -> None:
    logger.info("Received %s – shutting down…", sig.name)
    _shutdown.set()


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    import traceback

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    await init_db()

    tasks = [
        asyncio.create_task(scheduler_loop(), name="scheduler"),
        asyncio.create_task(metrics_loop(), name="metrics"),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        if task.exception():
            logger.error(
                "Worker task '%s' falhou:\n%s",
                task.get_name(),
                "".join(traceback.format_exception(task.exception()))
            )

    logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
