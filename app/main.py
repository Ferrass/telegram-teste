"""
FastAPI — Telegram Post Scheduler
Suporte a múltiplas contas Telegram por usuário.
"""
import logging
import uuid
from datetime import datetime
from typing import Annotated

import boto3
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_service import (
    authenticate_user,
    get_decrypted_session_by_phone,
    list_telegram_accounts,
    register_user,
    remove_telegram_account,
    send_login_code,
    verify_login_code,
)
from app.channel_service import get_user_channels, sync_admin_channels
from app.config import settings
from app.database import get_db, init_db
from app.models import PostStatus, User
from app.post_service import cancel_post, get_user_posts, schedule_post
from app.security import (
    SECURITY_HEADERS,
    create_access_token,
    create_refresh_token,
    decode_token,
    revoke_token,
    validate_password_strength,
    validate_phone,
)

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="Telegram Post Scheduler",
    version="2.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PUT"],
    allow_headers=["Authorization", "Content-Type", "X-Phone-Number"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()
    logger.info("Banco de dados iniciado")


# ── Dependencies ──────────────────────────────────────────────────────────────

async def current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = decode_token(token, kind="access")
    if not user_id:
        logger.warning("Tentativa de acesso com token inválido")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário não encontrado")
    return user


def get_active_phone(request: Request) -> str | None:
    """Extrai o telefone da conta ativa do header X-Phone-Number."""
    return request.headers.get("X-Phone-Number")


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        error = validate_password_strength(v)
        if error:
            raise ValueError(error)
        return v


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class SendCodeRequest(BaseModel):
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v):
        return validate_phone(v)


class VerifyCodeRequest(BaseModel):
    phone_number: str
    code: str = Field(..., min_length=4, max_length=8)
    password: str | None = None

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v):
        return validate_phone(v)


class TelegramAccountOut(BaseModel):
    id: str
    phone_number: str
    connected_at: datetime
    last_used_at: datetime | None
    model_config = {"from_attributes": True}


class ChannelOut(BaseModel):
    id: str
    channel_id: int
    channel_name: str
    username: str | None
    member_count: int
    phone_number: str | None
    model_config = {"from_attributes": True}


class SchedulePostRequest(BaseModel):
    channel_id: str
    message: str = Field(..., min_length=1, max_length=4096)
    media_url: str | None = Field(None, max_length=2048)
    scheduled_time: datetime

    @field_validator("scheduled_time")
    @classmethod
    def must_be_future(cls, v):
        from datetime import timezone
        now = datetime.now(timezone.utc)
        if v.tzinfo is None:
            raise ValueError("scheduled_time deve incluir timezone (use UTC).")
        if v <= now:
            raise ValueError("scheduled_time deve ser no futuro.")
        return v

    @field_validator("media_url")
    @classmethod
    def validate_media_url(cls, v):
        if v and not v.startswith("https://"):
            raise ValueError("media_url deve ser uma URL HTTPS.")
        return v


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

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/auth/register", response_model=TokenResponse, status_code=201, tags=["Auth"])
@limiter.limit(settings.rate_limit_register)
async def register(request: Request, body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await register_user(db, body.email, body.password)
        await db.commit()
        return TokenResponse(
            access_token=create_access_token(user.id),
            refresh_token=create_refresh_token(user.id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="E-mail já cadastrado.")
    except Exception as e:
        await db.rollback()
        logger.error("Erro no registro: %s", e)
        raise HTTPException(status_code=500, detail="Erro interno.")


@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
@limiter.limit(settings.rate_limit_login)
async def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(db, form.username, form.password)
    if not user:
        raise HTTPException(status_code=401, detail="E-mail ou senha inválidos")
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@app.post("/auth/refresh", response_model=TokenResponse, tags=["Auth"])
async def refresh_token(body: RefreshRequest):
    user_id = decode_token(body.refresh_token, kind="refresh")
    if not user_id:
        raise HTTPException(status_code=401, detail="Refresh token inválido ou expirado.")
    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@app.post("/auth/logout", status_code=204, tags=["Auth"])
async def logout(token: Annotated[str, Depends(oauth2_scheme)], user: User = Depends(current_user)):
    revoke_token(token)
    logger.info("Logout do usuário %s", user.id)


# ── Telegram Accounts ─────────────────────────────────────────────────────────

@app.get("/telegram/accounts", response_model=list[TelegramAccountOut], tags=["Telegram"])
async def list_accounts(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista todas as contas Telegram conectadas do usuário."""
    return await list_telegram_accounts(db, user.id)


@app.delete("/telegram/accounts/{phone_number}", status_code=204, tags=["Telegram"])
async def remove_account(
    phone_number: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove uma conta Telegram. Use o número no formato +5511999999999."""
    try:
        await remove_telegram_account(db, user.id, phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/telegram/send-code", tags=["Telegram"])
@limiter.limit(settings.rate_limit_send_code)
async def send_code(
    request: Request,
    body: SendCodeRequest,
    user: User = Depends(current_user),
):
    try:
        return await send_login_code(body.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc))


@app.post("/telegram/verify-code", tags=["Telegram"])
@limiter.limit("5/minute")
async def verify_code(
    request: Request,
    body: VerifyCodeRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        account = await verify_login_code(db, user.id, body.phone_number, body.code, body.password)
        return {"detail": "Conta Telegram conectada", "phone": account.phone_number}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Channels ──────────────────────────────────────────────────────────────────

@app.get("/telegram/channels", response_model=list[ChannelOut], tags=["Channels"])
async def list_channels(
    sync: bool = False,
    phone: str | None = Depends(get_active_phone),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Lista canais admin do usuário.
    Passe o header X-Phone-Number para filtrar por conta específica.
    Use ?sync=true para atualizar do Telegram em tempo real.
    """
    try:
        if sync:
            channels = await sync_admin_channels(db, user.id, phone=phone)
        else:
            channels = await get_user_channels(db, user.id, phone=phone)
        return channels
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Posts ─────────────────────────────────────────────────────────────────────

@app.post("/posts/schedule", response_model=PostOut, status_code=201, tags=["Posts"])
@limiter.limit(settings.rate_limit_schedule)
async def create_post(
    request: Request,
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


# ── Analytics ─────────────────────────────────────────────────────────────────

@app.get("/analytics/posts", response_model=list[AnalyticsOut], tags=["Analytics"])
async def analytics(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
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


# ── Media Upload ──────────────────────────────────────────────────────────────

@app.post("/media/upload", tags=["Media"])
@limiter.limit("10/minute")
async def upload_media(
    request: Request,
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
    key = f"{user.id}/{uuid.uuid4()}.{ext}"
    s3.put_object(Bucket=settings.r2_bucket, Key=key, Body=contents, ContentType=file.content_type)
    return {"url": f"{settings.r2_public_url}/{key}"}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
async def health():
    return {"status": "ok"}
