"""auth_api.py - accounts for the private pages (calendar, infinite buying). Router mounted by main.py: auth_api.init(supabase); app.include_router(auth_api.router).
Table app_users (sql/app_users.sql): username, password_hash "pbkdf2_sha256$iters$salt$hex" (older rows "sha256$salt$hex" are upgraded on the next successful login), name, phone, role user|admin, status pending|approved|rejected.
Flow: POST /signup (a rejected id may re-apply) -> status pending -> the admin approves or rejects in /admin/users -> POST /login returns a signed token (HMAC, 12 h) sent as `Authorization: Bearer <token>`.
Own account: GET/PUT /me (name, phone), POST /me/verify-password, POST /me/password (current password + new one).
The signing key is derived from the server-side Supabase key (AUTH_SECRET overrides it); without any of them a random per-process key is used, so tokens can never be forged with a known default.
Failed logins / password checks are rate limited per client IP AND per username (the client IP comes from the proxy headers, see _client)."""
import hashlib
import hmac
import ipaddress
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
_fails = {}                    # client ip -> failure times
_user_fails = {}               # username (lower case) -> failure times
_signups = {}                  # client ip -> signup times
_lock = threading.Lock()
MAX_FAILS, FAIL_WINDOW, TOKEN_HOURS = 10, 600, 12
USER_MAX_FAILS, USER_FAIL_WINDOW = 8, 900
SIGNUP_MAX, SIGNUP_WINDOW = 5, 3600
RESET_PASSWORD = "1" * 8      # what the admin's reset button sets (the user logs in with it and changes it under "my account")
PBKDF2_ITERS = 600_000        # OWASP 2023 value for PBKDF2-HMAC-SHA256
_MAX_VERIFY_ITERS = 2_000_000 # a tampered row must not make verify_password run for minutes
USER_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PHONE_RE = re.compile(r"^[0-9+\- ]{8,20}$")
_RANDOM_KEY = secrets.token_bytes(32)


def init(supabase):
    global _sb
    _sb = supabase


def hash_password(pw: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            iters = int(parts[1])
            if not 1 <= iters <= _MAX_VERIFY_ITERS:
                return False
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), parts[2].encode("utf-8"), iters)
            return hmac.compare_digest(dk.hex(), parts[3])
        if parts[0] == "sha256" and len(parts) == 3:          # legacy rows: single salted SHA-256
            return hmac.compare_digest(f"sha256${parts[1]}${hashlib.sha256((parts[1] + pw).encode('utf-8')).hexdigest()}", stored)
    except Exception:
        pass
    return False


def needs_rehash(stored: str) -> bool:
    return not str(stored).startswith(f"pbkdf2_sha256${PBKDF2_ITERS}$")


def _key() -> bytes:
    secret = os.environ.get("AUTH_SECRET") or os.environ.get("SUPABASE_KEY")
    if not secret:
        return _RANDOM_KEY
    return hashlib.sha256(b"quant-auth|" + secret.encode("utf-8")).digest()


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
    """Client IP for rate limiting. Never the first X-Forwarded-For value (the caller can write anything there): Cloudflare's CF-Connecting-IP,
    else the LAST hop (the one our own proxy appended), else the socket peer."""
    for raw in (request.headers.get("cf-connecting-ip"), (request.headers.get("x-forwarded-for") or "").split(",")[-1], request.client.host if request.client else ""):
        raw = (raw or "").strip()
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return "?"


def _recent(d: dict, key: str, now: float, window: float) -> list:
    xs = [t for t in d.get(key, []) if now - t < window]
    if xs:
        d[key] = xs
    else:
        d.pop(key, None)
    if len(d) > 5000:                                              # bound the memory an attacker can make us hold
        for k in [k for k, v in d.items() if not v or now - v[-1] >= window]:
            d.pop(k, None)
    return xs


def _check_limits(ip: str, username: str):
    now = time.time()
    with _lock:
        if len(_recent(_fails, ip, now, FAIL_WINDOW)) >= MAX_FAILS or len(_recent(_user_fails, username.lower(), now, USER_FAIL_WINDOW)) >= USER_MAX_FAILS:
            raise HTTPException(429, "too many failed attempts, try later")


def _record_fail(ip: str, username: str):
    now = time.time()
    with _lock:
        _fails.setdefault(ip, []).append(now)
        _user_fails.setdefault(username.lower(), []).append(now)


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
    username: str = Field(max_length=20)
    password: str = Field(min_length=8, max_length=100)
    name: str = Field(min_length=1, max_length=30)
    phone: str = Field(min_length=8, max_length=20)


class LoginIn(BaseModel):
    username: str = Field(max_length=20)
    password: str = Field(max_length=100)


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    phone: str = Field(min_length=8, max_length=20)


class VerifyIn(BaseModel):
    password: str = Field(max_length=100)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=100)
    new_password: str = Field(min_length=8, max_length=100)


def _public(row: dict) -> dict:
    return {"username": row["username"], "name": row["name"], "phone": row.get("phone"), "role": row["role"]}


@router.post("/signup")
def signup(body: SignupIn, request: Request):
    _need_db()
    ip, now = _client(request), time.time()
    with _lock:
        if len(_recent(_signups, ip, now, SIGNUP_WINDOW)) >= SIGNUP_MAX:
            raise HTTPException(429, "too many sign-up requests, try later")
        _signups.setdefault(ip, []).append(now)
    if not USER_RE.match(body.username):
        raise HTTPException(422, "id: 3-20 letters, digits or underscore")
    if not PHONE_RE.match(body.phone.strip()):
        raise HTTPException(422, "bad phone number")
    row = dict(password_hash=hash_password(body.password), name=body.name.strip(), phone=body.phone.strip(), status="pending", decided_at=None)
    old = _get(body.username)
    if old and old["status"] != "rejected":
        raise HTTPException(409, "id already taken")
    if old:      # a rejected applicant may submit again: the row goes back to pending with the new data
        _sb.table("app_users").update(row).eq("username", body.username).execute()
    else:
        _sb.table("app_users").insert(dict(row, username=body.username, role="user")).execute()
    return {"status": "success", "message": "pending approval"}


@router.post("/login")
def login(body: LoginIn, request: Request):
    _need_db()
    ip = _client(request)
    _check_limits(ip, body.username)
    row = _get(body.username) if USER_RE.match(body.username) else None
    if row is None or not verify_password(body.password, row["password_hash"]):
        _record_fail(ip, body.username)
        raise HTTPException(401, "wrong id or password")
    if row["status"] != "approved":
        raise HTTPException(403, "pending approval" if row["status"] == "pending" else "request rejected")
    with _lock:
        _user_fails.pop(body.username.lower(), None)
    if needs_rehash(row["password_hash"]):          # upgrade an old hash while the plain password is at hand
        try:
            _sb.table("app_users").update(dict(password_hash=hash_password(body.password))).eq("username", row["username"]).execute()
        except Exception:
            pass
    return {"status": "success", "token": make_token(row["username"]), "user": {"username": row["username"], "name": row["name"], "role": row["role"]}}


@router.get("/me")
def me(authorization: Optional[str] = Header(None)):
    row = current_user(authorization)
    return {"status": "success", "user": _public(row)}


@router.put("/me")
def update_me(body: ProfileIn, authorization: Optional[str] = Header(None)):
    """Name and phone of the logged-in user (the id and the role cannot be changed here)."""
    row = current_user(authorization)
    name, phone = body.name.strip(), body.phone.strip()
    if not name:
        raise HTTPException(422, "name required")
    if not PHONE_RE.match(phone):
        raise HTTPException(422, "bad phone number")
    _sb.table("app_users").update(dict(name=name, phone=phone)).eq("username", row["username"]).execute()
    return {"status": "success", "user": _public(dict(row, name=name, phone=phone))}


@router.post("/me/verify-password")
def verify_my_password(body: VerifyIn, request: Request, authorization: Optional[str] = Header(None)):
    """True when `password` is the current password. Wrong answers count against the same limits as a failed login, so this cannot be used to guess it."""
    row = current_user(authorization)
    ip = _client(request)
    _check_limits(ip, row["username"])
    ok = verify_password(body.password, row["password_hash"])
    if not ok:
        _record_fail(ip, row["username"])
    return {"status": "success", "ok": ok}


@router.post("/me/password")
def change_my_password(body: PasswordIn, request: Request, authorization: Optional[str] = Header(None)):
    row = current_user(authorization)
    ip = _client(request)
    _check_limits(ip, row["username"])
    if not verify_password(body.current_password, row["password_hash"]):
        _record_fail(ip, row["username"])
        raise HTTPException(401, "current password is wrong")
    pw = body.new_password
    if pw == body.current_password:
        raise HTTPException(422, "the new password must differ from the current one")
    if len(set(pw)) < 3 or pw.lower() == row["username"].lower():
        raise HTTPException(422, "the new password is too simple")
    _sb.table("app_users").update(dict(password_hash=hash_password(pw))).eq("username", row["username"]).execute()
    with _lock:
        _user_fails.pop(row["username"].lower(), None)
    return {"status": "success"}


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


def _target(username: str) -> dict:
    row = _get(username) if USER_RE.match(username) else None
    if row is None:
        raise HTTPException(404, "user not found")
    if row["role"] == "admin":
        raise HTTPException(409, "admin account cannot be changed")
    return row


@router.post("/admin/users/{username}/reset-password")
def admin_reset(username: str, authorization: Optional[str] = Header(None)):
    admin_user(authorization)
    _target(username)
    _sb.table("app_users").update(dict(password_hash=hash_password(RESET_PASSWORD))).eq("username", username).execute()
    return {"status": "success", "username": username}


@router.delete("/admin/users/{username}")
def admin_delete(username: str, authorization: Optional[str] = Header(None)):
    """Withdraw an account. Its infinite-buying cycles are kept but paused with auto orders OFF, so the local executor stops trading them."""
    admin_user(authorization)
    _target(username)
    _sb.table("infinite_cycles").update(dict(auto_order=False, status="paused")).eq("owner", username).neq("status", "done").execute()
    _sb.table("app_users").delete().eq("username", username).execute()
    return {"status": "success", "username": username}
