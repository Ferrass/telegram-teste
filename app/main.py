"""
FastAPI application — Telegram Post Scheduler
"""
import logging
import uuid
from datetime import datetime
from typing import Annotated

import boto3
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_service import (
    authenticate_user,
    create_access_token,
    decode_access_token,
    register_user,
    send_login_code,
    verify_login_code,
)
from app.channel_service import get_user_channels, sync_admin_channels
from app.config import settings
from app.database import get_db, init_db
from app.models import PostStatus, User
from app.post_service import (
    cancel_post,
    get_user_posts,
    schedule_post,
)

logging.basicConfig(level=settings.log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Telegram Post Scheduler",
    description="Agende e analise posts em canais Telegram via MTProto.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()
    logger.info("Banco de dados iniciado")


# ─── Auth dependency ──────────────────────────────────────────────────────────

async def current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido ou expirado")
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário não encontrado")
    return user


# ─── Schemas ──────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class SendCodeRequest(BaseModel):
    phone_number: str

class VerifyCodeRequest(BaseModel):
    phone_number: str
    code: str
    password: str | None = None

class ChannelOut(BaseModel):
    id: str
    channel_id: int
    channel_name: str
    username: str | None
    member_count: int
    model_config = {"from_attributes": True}

class SchedulePostRequest(BaseModel):
    channel_id: str
    message: str
    media_url: str | None = None
    scheduled_time: datetime

class PostOut(BaseModel):
    id: str
    channel_id: str
    message: str
    media_url: str | None
    scheduled_time: datetime
    status: PostStatus
    telegram_message_id: int | None
    views: int
    forwards: int
    created_at: datetime
    model_config = {"from_attributes": True}

class AnalyticsOut(BaseModel):
    post_id: str
    channel_name: str
    message: str
    scheduled_time: datetime
    status: PostStatus
    views: int
    forwards: int
    telegram_message_id: int | None
    model_config = {"from_attributes": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/auth/register", response_model=TokenResponse, status_code=201, tags=["Auth"])
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await register_user(db, body.email, body.password)
        await db.commit()
        return TokenResponse(access_token=create_access_token(user.id))
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="E-mail já cadastrado.")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, form.username, form.password)
    if not user:
        raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")
    return TokenResponse(access_token=create_access_token(user.id))


# ─── Telegram ─────────────────────────────────────────────────────────────────

@app.post("/telegram/send-code", tags=["Telegram"])
async def send_code(body: SendCodeRequest, user: User = Depends(current_user)):
    try:
        return await send_login_code(body.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@app.post("/telegram/verify-code", tags=["Telegram"])
async def verify_code(
    body: VerifyCodeRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        account = await verify_login_code(db, user.id, body.phone_number, body.code, body.password)
        return {"detail": "Conta Telegram conectada", "phone": account.phone_number}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── Channels ─────────────────────────────────────────────────────────────────

@app.get("/telegram/channels", response_model=list[ChannelOut], tags=["Channels"])
async def list_channels(
    sync: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        if sync:
            channels = await sync_admin_channels(db, user.id)
        else:
            channels = await get_user_channels(db, user.id)
        return channels
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── Posts ────────────────────────────────────────────────────────────────────

@app.post("/posts/schedule", response_model=PostOut, status_code=201, tags=["Posts"])
async def create_post(
    body: SchedulePostRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        post = await schedule_post(
            db,
            user_id=user.id,
            channel_id=body.channel_id,
            message=body.message,
            scheduled_time=body.scheduled_time,
            media_url=body.media_url,
        )
        return post
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/posts", response_model=list[PostOut], tags=["Posts"])
async def list_posts(
    status_filter: PostStatus | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    # Filtra SEMPRE pelo user_id do token — nunca expõe dados de outros usuários
    return await get_user_posts(db, user.id, status=status_filter)


@app.delete("/posts/{post_id}", status_code=204, tags=["Posts"])
async def delete_post(
    post_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await cancel_post(db, post_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── Analytics ────────────────────────────────────────────────────────────────

@app.get("/analytics/posts", response_model=list[AnalyticsOut], tags=["Analytics"])
async def analytics(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    # Filtra SEMPRE pelo user_id do token
    posts = await get_user_posts(db, user.id, status=PostStatus.sent)
    return [
        AnalyticsOut(
            post_id=p.id,
            channel_name=p.channel.channel_name,
            message=p.message,
            scheduled_time=p.scheduled_time,
            status=p.status,
            views=p.views,
            forwards=p.forwards,
            telegram_message_id=p.telegram_message_id,
        )
        for p in posts
    ]


# ─── Media Upload ─────────────────────────────────────────────────────────────

@app.post("/media/upload", tags=["Media"])
async def upload_media(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    allowed = ["image/jpeg", "image/png", "image/gif", "video/mp4"]
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Tipo não permitido. Use jpg, png, gif ou mp4.")

    contents = await file.read()

    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Arquivo muito grande. Máximo 50MB.")

    if not settings.r2_endpoint:
        raise HTTPException(status_code=500, detail="Serviço de upload não configurado.")

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        region_name="auto",
    )

    ext = file.filename.split(".")[-1].lower()
    # Usa UUID do usuário como prefixo — garante isolamento por usuário no bucket
    key = f"{user.id}/{uuid.uuid4()}.{ext}"

    s3.put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=contents,
        ContentType=file.content_type,
    )

    url = f"{settings.r2_public_url}/{key}"
    return {"url": url}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
async def health():
    return {"status": "ok"}
