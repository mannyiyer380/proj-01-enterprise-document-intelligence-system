"""Request authentication and session ID validation."""
from __future__ import annotations

import re
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.services.accounts import LOGIN_COOKIE
from app.config.config import Settings

_bearer = HTTPBearer(auto_error=False)
SESSION_RE = re.compile(r"^[a-f0-9]{32}$")


def require_session_id(session_id: str) -> str:
    if not SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID")
    return session_id


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if settings.auth_mode == "local":
        return "local-user"
    if settings.auth_mode == "sqlite":
        owner = request.app.state.accounts.session_owner(request.cookies.get(LOGIN_COOKIE))
        if owner is None:
            raise HTTPException(status_code=401, detail="Sign in to access the workspace.")
        return owner
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from None
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 200:
        raise HTTPException(status_code=401, detail="Invalid subject")
    return sub
