"""Local account signup, sign-in, and sign-out."""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response

from app.services.accounts import AccountStore, LOGIN_COOKIE, SESSION_SECONDS

router = APIRouter(prefix="/api/auth", tags=["Local accounts"])
USERNAME = re.compile(r"[a-z0-9_-]{3,32}", re.ASCII)


def get_accounts(request: Request) -> AccountStore:
    if request.app.state.accounts is None:
        raise HTTPException(404, "Local accounts are disabled.")
    return request.app.state.accounts


Accounts = Annotated[AccountStore, Depends(get_accounts)]


def login_response(request: Request, accounts: AccountStore, owner: str) -> Response:
    token = accounts.start_session(owner, request.cookies.get(LOGIN_COOKIE))
    response = Response(status_code=204)
    response.set_cookie(
        LOGIN_COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
        secure=request.url.scheme == "https", samesite="strict",
    )
    return response


@router.post("/signup", status_code=204)
def signup(
    request: Request, accounts: Accounts, username: Annotated[str, Form()], password: Annotated[str, Form()],
) -> Response:
    username = username.strip().lower()
    if not USERNAME.fullmatch(username):
        raise HTTPException(422, "Use 3–32 letters, numbers, underscores or hyphens for your username.")
    if not 15 <= len(password) <= 128:
        raise HTTPException(422, "Use a password between 15 and 128 characters.")
    owner = accounts.signup(username, password)
    if owner is None:
        raise HTTPException(409, "That username is already taken.")
    return login_response(request, accounts, owner)


@router.post("/login", status_code=204)
def login(
    request: Request, accounts: Accounts, username: Annotated[str, Form()], password: Annotated[str, Form()],
) -> Response:
    username = username.strip().lower()
    owner = accounts.verify(username, password) if USERNAME.fullmatch(username) and 15 <= len(password) <= 128 else None
    if owner is None:
        raise HTTPException(401, "Incorrect username or password.")
    return login_response(request, accounts, owner)


@router.post("/logout", status_code=204)
def logout(request: Request, accounts: Accounts) -> Response:
    accounts.end_session(request.cookies.get(LOGIN_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(
        LOGIN_COOKIE, httponly=True, secure=request.url.scheme == "https", samesite="strict",
    )
    return response


@router.get("/session")
def browser_session(request: Request) -> dict:
    accounts: AccountStore | None = request.app.state.accounts
    if accounts is None:
        return {"enabled": False}
    return {"enabled": True, "account_id": accounts.session_owner(request.cookies.get(LOGIN_COOKIE))}
