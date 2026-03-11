"""
Background worker process.
Suporte a múltiplas contas — usa phone_number do canal para selecionar sessão correta.

Loops:
  • Scheduler  — envia posts a cada SCHEDULER_INTERVAL_SECONDS (default 60s)
  • Metrics    — coleta views/forwards a cada METRICS_INTERVAL_SECONDS (default 1800s)

Start:
    python -m app.worker
"""
import asyncio
import logging
import signal
import traceback

import boto3
from sqlalchemy import update
from telethon.tl.types import PeerChannel

from app.auth_service import get_decrypted_session_by_phone
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


# ── R2 helper ─────────────────────────────────────────────────────────────────

def delete_from_r2(media_url: str) -> None:
    if not settings.r2_endpoint or not media_url:
        return
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key,
            aws_secret_access_key=settings.r2_secret_key,
            region_name="auto",
        )
        key = media_url.split("/")[-1]
        s3.delete_object(Bucket=settings.r2_bucket, Key=key)
        logger.info("Mídia apagada do R2: %s", key)
    except Exception as exc:
        logger.warning("Não foi possível apagar mídia do R2 (%s): %s", media_url, exc)


# ── Send post ─────────────────────────────────────────────────────────────────

async def send_post(post: ScheduledPost) -> None:
    """
    Envia um post agendado.
    Usa o phone_number do canal para selecionar a sessão Telegram correta.
    """
    async with AsyncSessionLocal() as db:
        # Pega o phone do canal — suporte a múltiplas contas
        phone = getattr(post.channel, "phone_number", None)

        session_string = await get_decrypted_session_by_phone(db, post.user_id, phone)
        if not session_string:
            logger.error(
                "Sem sessão Telegram para user=%s phone=%s — post %s marcado como failed",
                post.user_id, phone, post.id
            )
            await db.execute(
                update(ScheduledPost)
                .where(ScheduledPost.id == post.id)
                .values(status=PostStatus.failed)
            )
            await db.commit()
            return

        async with get_client(session_string) as client:
            try:
                # Resolve o canal corretamente
                channel_id = post.channel.channel_id
                if channel_id < 0:
                    channel_id = int(str(abs(channel_id))[3:])

                try:
                    if post.channel.username:
                        entity = await client.get_entity(f"@{post.channel.username}")
                    else:
                        entity = await client.get_entity(PeerChannel(channel_id))
                except Exception:
                    entity = await client.get_entity(post.channel.channel_id)

                # Envia com ou sem mídia
                if post.media_url:
                    sent = await client.send_file(entity, post.media_url, caption=post.message)
                else:
                    sent = await client.send_message(entity, post.message)

                msg_id = sent.id

                await db.execute(
                    update(ScheduledPost)
                    .where(ScheduledPost.id == post.id)
                    .values(status=PostStatus.sent, telegram_message_id=msg_id)
                )
                await db.commit()
                logger.info(
                    "Post %s enviado — canal=%s phone=%s msg_id=%s",
                    post.id, post.channel.channel_name, phone, msg_id
                )

                # Apaga mídia do R2 após envio
                if post.media_url:
                    delete_from_r2(post.media_url)

            except Exception as exc:
                logger.error("Falha ao enviar post %s: %s", post.id, exc, exc_info=True)
                await db.execute(
                    update(ScheduledPost)
                    .where(ScheduledPost.id == post.id)
                    .values(status=PostStatus.failed)
                )
                await db.commit()


# ── Scheduler loop ────────────────────────────────────────────────────────────

async def scheduler_loop() -> None:
    logger.info("Scheduler iniciado (intervalo=%ds)", settings.scheduler_interval_seconds)
    while not _shutdown.is_set():
        try:
            async with AsyncSessionLocal() as db:
                pending = await get_pending_posts(db)

            if pending:
                logger.info("Encontrados %d post(s) para enviar", len(pending))
                results = await asyncio.gather(
                    *[send_post(p) for p in pending],
                    return_exceptions=True
                )
                for r in results:
                    if isinstance(r, Exception):
                        logger.error("Erro ao enviar post: %s", r, exc_info=r)
        except Exception as exc:
            logger.error("Erro no scheduler: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                asyncio.shield(_shutdown.wait()),
                timeout=settings.scheduler_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass


# ── Metrics loop ──────────────────────────────────────────────────────────────

async def metrics_loop() -> None:
    logger.info("Metrics worker iniciado (intervalo=%ds)", settings.metrics_interval_seconds)
    while not _shutdown.is_set():
        try:
            await run_metrics_collection()
        except Exception as exc:
            logger.error("Erro na coleta de métricas: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(
                asyncio.shield(_shutdown.wait()),
                timeout=settings.metrics_interval_seconds,
            )
        except asyncio.TimeoutError:
            pass


# ── Graceful shutdown ─────────────────────────────────────────────────────────

def _handle_signal(sig: signal.Signals) -> None:
    logger.info("Sinal %s recebido — encerrando...", sig.name)
    _shutdown.set()


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
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

    logger.info("Worker encerrado")


if __name__ == "__main__":
    asyncio.run(main())
