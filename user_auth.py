"""
Authentication helpers for the HM Data Extractor API.
Users live in Supabase Auth; the API trusts only tokens that Supabase itself confirms.
"""

import asyncio
import re
import time

from fastapi import HTTPException, Request

import supabase_db

_TOKEN_TTL = 300
_token_cache: dict[str, tuple[float, dict]] = {}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 8

_signup_hits: dict[str, list[float]] = {}
_SIGNUP_LIMIT, _SIGNUP_WINDOW = 10, 3600


def _token_from(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # EventSource and plain downloads cannot send headers
    return request.query_params.get("access_token", "")


async def current_user(request: Request) -> dict:
    token = _token_from(request)
    if not token:
        raise HTTPException(401, "Please sign in")

    hit = _token_cache.get(token)
    if hit and hit[0] > time.time():
        return hit[1]

    try:
        resp = await asyncio.to_thread(supabase_db.get_client().auth.get_user, token)
        user = resp.user if resp else None
    except Exception:
        user = None
    if not user:
        raise HTTPException(401, "Session expired - please sign in again")

    info = {
        "id": user.id,
        "email": user.email or "",
        "name": (user.user_metadata or {}).get("full_name", ""),
    }
    if len(_token_cache) > 500:
        now = time.time()
        for k in [k for k, (exp, _) in _token_cache.items() if exp <= now]:
            del _token_cache[k]
    _token_cache[token] = (time.time() + _TOKEN_TTL, info)
    return info


def check_signup_rate(ip: str) -> None:
    now = time.time()
    hits = [t for t in _signup_hits.get(ip, []) if now - t < _SIGNUP_WINDOW]
    if len(hits) >= _SIGNUP_LIMIT:
        raise HTTPException(429, "Too many sign-ups from this network. Try again later.")
    hits.append(now)
    _signup_hits[ip] = hits


def validate_signup(name: str, email: str, password: str) -> None:
    """Validate signup fields — the frontend creates the account via Supabase directly."""
    name, email = name.strip(), email.strip().lower()
    if len(name) < 2:
        raise HTTPException(400, "Please enter your full name")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters")
