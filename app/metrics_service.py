"""
Metrics collection service.
Usa o phone_number do canal para selecionar a sessão correta — suporte a múltiplas contas.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.types import PeerChannel

from app.auth_service import get_decrypted_session_by_phone
from app.database import AsyncSessionLocal
from app.models import PostMetricsHistory, ScheduledPost
from app.post_service import get_sent_posts
from app.telegram_client import get_client

logger = logging.getLogger(__name__)


async def collect_post_metrics(db: AsyncSession, post: ScheduledPost) -> tuple[int, int]:
    """
    Busca views e forwards de um post enviado.
    Usa a sessão do número vinculado ao canal do post.
    """
    # Usa o phone_number do canal para pegar a sessão correta
    phone = getattr(post.channel, "phone_number", None)
    session_string = await get_decrypted_session_by_phone(db, post.user_id, phone)

    if not session_string:
        logger.warning(
            "Sem sessão para user=%s phone=%s — pulando métricas do post %s",
            post.user_id, phone, post.id
        )
        return post.views, post.forwards

    async with get_client(session_string) as client:
        channel = post.channel
        try:
            # Resolve o canal corretamente
            channel_id = channel.channel_id
            if channel_id < 0:
                channel_id = int(str(abs(channel_id))[3:])

            try:
                if channel.username:
                    entity = await client.get_entity(f"@{channel.username}")
                else:
                    entity = await client.get_entity(PeerChannel(channel_id))
            except Exception:
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
            logger.info("Métricas post %s: views=%d forwards=%d", post.id, views, forwards)
            return views, forwards

        except Exception as exc:
            logger.error("Erro ao buscar métricas do post %s: %s", post.id, exc)
            return post.views, post.forwards


async def run_metrics_collection() -> None:
    """Executa coleta de métricas para todos os posts enviados."""
    async with AsyncSessionLocal() as db:
        posts = await get_sent_posts(db)
        logger.info("Coletando métricas de %d posts", len(posts))

        for post in posts:
            try:
                views, forwards = await collect_post_metrics(db, post)
                post.views = views
                post.forwards = forwards

                snapshot = PostMetricsHistory(
                    post_id=post.id,
                    views=views,
                    forwards=forwards,
                    captured_at=datetime.now(timezone.utc),
                )
                db.add(snapshot)
            except Exception as exc:
                logger.error("Erro ao processar métricas do post %s: %s", post.id, exc)

        await db.commit()
        logger.info("Coleta de métricas concluída")
