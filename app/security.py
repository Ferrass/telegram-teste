"""
Módulo central de segurança.

Contém:
- Hashing de senha com Argon2 (mais seguro que bcrypt)
- JWT access + refresh tokens
- Blacklist de tokens invalidados (logout)
- Validação de força de senha
- Validação de número de telefone
- Security headers
"""
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

import phonenumbers
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from jose import JWTError, jwt

from app.config import settings

logger = logging.getLogger(__name__)

# ── Argon2 (superior ao bcrypt contra ataques de GPU) ─────────────────────────
_ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,  # 64MB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

# ── Token blacklist em memória ────────────────────────────────────────────────
# Em produção multi-processo use Redis: SET token_jti EX ttl
_token_blacklist: set[str] = set()


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> str | None:
    """
    Retorna mensagem de erro ou None se a senha for forte.
    Regras: mínimo 8 chars, 1 maiúscula, 1 minúscula, 1 número, 1 especial.
    """
    if len(password) < 8:
        return "Senha deve ter pelo menos 8 caracteres."
    if not re.search(r"[A-Z]", password):
        return "Senha deve ter pelo menos uma letra maiúscula."
    if not re.search(r"[a-z]", password):
        return "Senha deve ter pelo menos uma letra minúscula."
    if not re.search(r"\d", password):
        return "Senha deve ter pelo menos um número."
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return "Senha deve ter pelo menos um caractere especial."
    return None


# ── JWT ───────────────────────────────────────────────────────────────────────

def _make_token(sub: str, kind: Literal["access", "refresh"], expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "kind": kind,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def create_access_token(user_id: str) -> str:
    return _make_token(
        user_id, "access",
        timedelta(minutes=settings.access_token_expire_minutes)
    )


def create_refresh_token(user_id: str) -> str:
    return _make_token(
        user_id, "refresh",
        timedelta(days=settings.refresh_token_expire_days)
    )


def decode_token(token: str, kind: Literal["access", "refresh"] = "access") -> str | None:
    """Decodifica e valida um token JWT. Retorna user_id ou None."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        if payload.get("kind") != kind:
            return None
        if token in _token_blacklist:
            return None
        return payload["sub"]
    except (JWTError, KeyError):
        return None


def revoke_token(token: str) -> None:
    """Adiciona token à blacklist (logout)."""
    _token_blacklist.add(token)
    logger.info("Token revogado")


# ── Phone validation ──────────────────────────────────────────────────────────

def validate_phone(phone: str) -> str:
    """
    Valida e normaliza número de telefone para formato E.164.
    Lança ValueError se inválido.
    """
    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("Número de telefone inválido.")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        raise ValueError("Formato de telefone inválido. Use o formato internacional: +5511999999999")


# ── Security headers ──────────────────────────────────────────────────────────

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": "default-src 'self'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}
