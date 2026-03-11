"""
FastAPI application — Telegram Post Scheduler
"""
import logging
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_service import (
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_decrypted_session,
    register_user,
    send_login_code,
    verify_login_code,
)
from app.channel_service import get_user_channels, sync_admin_channels
from app.config import settings
from app.database import get_db, init_db
from app.models import PostStatus, ScheduledPost, User
from app.post_service import (
    cancel_post,
    get_user_posts,
    schedule_post,
)

logging.basicConfig(level=settings.log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Telegram Post Scheduler",
    description="Schedule and analyse posts to Telegram channels via MTProto.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()
    logger.info("Database initialised")


# ─── Auth dependency ──────────────────────────────────────────────────────────

async def current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = decode_access_token(token)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

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
    password: str | None = None  # 2FA password

class ChannelOut(BaseModel):
    id: int
    channel_id: int
    channel_name: str
    username: str | None
    member_count: int

    model_config = {"from_attributes": True}

class SchedulePostRequest(BaseModel):
    channel_id: int
    message: str
    media_url: str | None = None
    scheduled_time: datetime

class PostOut(BaseModel):
    id: int
    channel_id: int
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
    post_id: int
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

# ─── User Auth ────────────────────────────────────────────────────────────────

@app.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED, tags=["Auth"])
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new platform user."""
    try:
        user = await register_user(db, body.email, body.password)
        token = create_access_token(user.id)
        return TokenResponse(access_token=token)
    except Exception:
        raise HTTPException(status_code=409, detail="Email already registered.")


@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: AsyncSession = Depends(get_db)):
    """Log in with email + password (OAuth2 password flow)."""
    user = await authenticate_user(db, form.username, form.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(user.id))


# ─── Telegram Auth ────────────────────────────────────────────────────────────

@app.post("/telegram/send-code", tags=["Telegram"])
async def send_code(
    body: SendCodeRequest,
    user: User = Depends(current_user),
):
    """Step 1 of Telegram login: request an SMS/app code."""
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
    """Step 2 of Telegram login: submit the verification code."""
    try:
        account = await verify_login_code(db, user.id, body.phone_number, body.code, body.password)
        return {"detail": "Telegram account connected", "phone": account.phone_number}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── Channels ─────────────────────────────────────────────────────────────────

@app.get("/telegram/channels", response_model=list[ChannelOut], tags=["Channels"])
async def list_channels(
    sync: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return channels where the user is admin.
    Pass ?sync=true to refresh from Telegram (slower but always up-to-date).
    """
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
    """Schedule a new post."""
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
    """List all posts for the authenticated user."""
    return await get_user_posts(db, user.id, status=status_filter)


@app.delete("/posts/{post_id}", status_code=204, tags=["Posts"])
async def delete_post(
    post_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a scheduled post (sets status to 'cancelled')."""
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
    """Return sent posts with their latest view/forward metrics."""
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


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
async def health():
    return {"status": "ok"}
