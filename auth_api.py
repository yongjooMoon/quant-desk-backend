"""auth_api.py - accounts for the private pages (calendar, infinite buying). Router mounted by main.py: auth_api.init(supabase); app.include_router(auth_api.router).
Table app_users (sql/app_users.sql): username, password_hash "sha256$salt$hex", name, phone, role user|admin, status pending|approved|rejected.
Flow: POST /signup -> status pending -> the admin approves or rejects in /admin/users -> POST /login returns a signed token (HMAC, 12 h) sent as `Authorization: Bearer <token>`.
The signing key is derived from the server-side Supabase key, so no new environment variable is needed. Failed logins are rate limited per client IP."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime as _dt, timezone as _tz
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/auth")
_sb = None
_fails = {}
_lock = threading.Lock()
MAX_FAILS, FAIL_WINDOW, TOKEN_HOURS = 10, 600, 12
USER_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


def init(supabase):
    global _sb
    _sb = supabase


def hash_password(pw: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(8)
    return f"sha256${salt}${hashlib.sha256((salt + pw).encode('utf-8')).hexdigest()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, _h = stored.split("$")
        return hmac.compare_digest(hash_password(pw, salt), stored)
    except Exception:
        return False


def _key() -> bytes:
    return hashlib.sha256(b"quant-auth|" + os.environ.get("SUPABASE_KEY", "dev-key").encode("utf-8")).digest()


def make_token(username: str, hours: int = TOKEN_HOURS, now: Optional[float] = None) -> str:
    body = f"{username}.{int((now or time.time()) + hours * 3600)}"
    return f"{body}.{hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()}"


def parse_token(token: str, now: Optional[float] = None) -> Optional[str]:
    try:
        user, exp, sig = token.rsplit(".", 2)
        good = hmac.new(_key(), f"{user}.{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and (now or time.time()) < int(exp):
            return user
    except Exception:
        pass
    return None


def _client(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()


def _need_db():
    if _sb is None:
        raise HTTPException(503, "DB not configured")


def _get(username: str):
    return (_sb.table("app_users").select("*").eq("username", username).execute().data or [None])[0]


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    """Dependency: the approved user row behind the Bearer token (status is re-checked on every call, so a rejection takes effect at once)."""
    _need_db()
    tok = (authorization or "").removeprefix("Bearer ").strip()
    user = parse_token(tok) if tok else None
    row = _get(user) if user else None
    if row is None or row.get("status") != "approved":
        raise HTTPException(401, "login required")
    return row


def admin_user(authorization: Optional[str] = Header(None)) -> dict:
    row = current_user(authorization)
    if row.get("role") != "admin":
        raise HTTPException(403, "admin only")
    return row


class SignupIn(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=100)
    name: str = Field(min_length=1, max_length=30)
    phone: str = Field(min_length=9, max_length=20)


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/signup")
def signup(body: SignupIn):
    _need_db()
    if not USER_RE.match(body.username):
        raise HTTPException(422, "id: 3-20 letters, digits or underscore")
    if not re.match(r"^[0-9+\- ]{9,20}$", body.phone):
        raise HTTPException(422, "bad phone number")
    if _get(body.username):
        raise HTTPException(409, "id already taken")
    _sb.table("app_users").insert(dict(username=body.username, password_hash=hash_password(body.password), name=body.name.strip(), phone=body.phone.strip(), role="user", status="pending")).execute()
    return {"status": "success", "message": "pending approval"}


@router.post("/login")
def login(body: LoginIn, request: Request):
    _need_db()
    ip, now = _client(request), time.time()
    with _lock:
        xs = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = xs
        if len(xs) >= MAX_FAILS:
            raise HTTPException(429, "too many failed attempts, try later")
    row = _get(body.username) if USER_RE.match(body.username) else None
    if row is None or not verify_password(body.password, row["password_hash"]):
        with _lock:
            _fails[ip].append(now)
        raise HTTPException(401, "wrong id or password")
    if row["status"] != "approved":
        raise HTTPException(403, "pending approval" if row["status"] == "pending" else "request rejected")
    return {"status": "success", "token": make_token(row["username"]), "user": {"username": row["username"], "name": row["name"], "role": row["role"]}}


@router.get("/me")
def me(authorization: Optional[str] = Header(None)):
    row = current_user(authorization)
    return {"status": "success", "user": {"username": row["username"], "name": row["name"], "role": row["role"]}}


@router.get("/admin/users")
def admin_list(authorization: Optional[str] = Header(None)):
    admin_user(authorization)
    rows = _sb.table("app_users").select("username,name,phone,role,status,created_at,decided_at").order("created_at", desc=True).execute().data or []
    return {"status": "success", "data": [{k: v for k, v in r.items() if k != "password_hash"} for r in rows]}


@router.post("/admin/users/{username}/decision")
def admin_decide(username: str, body: dict, authorization: Optional[str] = Header(None)):
    admin_user(authorization)
    dec = {"approve": "approved", "reject": "rejected"}.get(str(body.get("decision")))
    row = _get(username) if USER_RE.match(username) else None
    if dec is None or row is None:
        raise HTTPException(422, "decision approve|reject and an existing user are required")
    if row["role"] == "admin":
        raise HTTPException(409, "admin account cannot be changed")
    _sb.table("app_users").update(dict(status=dec, decided_at=_dt.now(_tz.utc).isoformat())).eq("username", username).execute()
    return {"status": "success", "username": username, "new_status": dec}
