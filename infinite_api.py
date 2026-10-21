"""infinite_api.py - REST API of the infinite-buying manager (Laor V2.2 / V4.0), cycle based. Router mounted by main.py: infinite_api.init(supabase); app.include_router(infinite_api.router).
Model (owner decision 2026-10-21): every cycle is a row of infinite_cycles with its own cycle_key and password (stored only as a scrypt hash); a finished cycle is closed (status done) and the next cycle is created
as a NEW row that inherits the password hash. There is no global admin token.
Auth: header X-Infinite-Password. GET /cycles returns every cycle whose password matches it; the other endpoints work on one cycle id and check that cycle's hash. Failed attempts are rate limited per client IP.
Rule engine: infinite_engine.py (pure functions). Tables: sql/infinite_buy.sql. Cycles are created by hand with POST /cycles (needs an existing cycle's password; the new cycle shares it), by 주식/infinite_setup.py, or by the automatic next-cycle step.
"""
import os
import re
import threading
import time
from datetime import date as _date, datetime as _dt, timezone as _tz
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

import infinite_engine as E
import infinite_store as store

router = APIRouter(prefix="/api/infinite")
_sb = None
_fails = {}
_fails_lock = threading.Lock()
MAX_FAILS, FAIL_WINDOW = 20, 600


def _now():
    return _dt.now(_tz.utc).isoformat()


def init(supabase):
    global _sb
    _sb = supabase


from infinite_auth import hash_password, verify_password  # noqa: E402  (re-exported)


def _client(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()


def _check_rate(ip: str):
    now = time.time()
    with _fails_lock:
        xs = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = xs
        if len(xs) >= MAX_FAILS:
            raise HTTPException(429, "too many failed attempts, try later")


def _fail(ip: str):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())
    raise HTTPException(401, "invalid password")


def _need_db():
    if _sb is None:
        raise HTTPException(503, "DB not configured")


def _cycle_for(cycle_id: str, password: Optional[str], request: Request):
    """The cycle row when the password matches its hash; 404 for an unknown id (same status as a wrong password after the check so ids are not probed)."""
    _need_db()
    ip = _client(request)
    _check_rate(ip)
    if not password:
        _fail(ip)
    r = _sb.table("infinite_cycles").select("*").eq("id", cycle_id).execute()
    row = (r.data or [None])[0]
    if row is None or not verify_password(password, row["password_hash"]):
        _fail(ip)
    return row


VERSIONS = {"V2.2": (10, 20, 30, 40), "V4.0": (20, 40)}
TICKERS = {"TQQQ": ("0537", "OQ"), "SOXL": ("0066", "AX")}


class CycleIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    version: str
    ticker: str
    splits: int
    principal_usd: float = Field(gt=0)
    compounding: bool = True
    start_date: Optional[str] = None
    t_value: float = Field(0, ge=0)
    cash_usd: Optional[float] = None
    qty: int = Field(0, ge=0)
    avg_price: float = Field(0, ge=0)
    cycle_key: Optional[str] = Field(None, max_length=40)
    extras: dict = {}


class CyclePatch(BaseModel):
    name: Optional[str] = None
    principal_usd: Optional[float] = None
    compounding: Optional[bool] = None
    start_date: Optional[str] = None
    t_value: Optional[float] = None
    cash_usd: Optional[float] = None
    qty: Optional[int] = None
    avg_price: Optional[float] = None
    mode: Optional[str] = None
    status: Optional[str] = None
    auto_order: Optional[bool] = None
    extras: Optional[dict] = None
    prev_close: Optional[float] = None
    closes5: Optional[List[float]] = None


class TradeIn(BaseModel):
    trade_date: str
    side: str
    qty: int = Field(gt=0)
    price: float = Field(gt=0)
    tag: Optional[str] = ""
    order_no: Optional[str] = None
    source: str = "manual"
    t_step: Optional[float] = Field(None, ge=0, le=2)     # manual T step of this fill (V4.0): 0.5 for a half buy, 1 for a full one; empty = rule engine decides


class SyncIn(BaseModel):
    prev_close: Optional[float] = None
    closes5: Optional[List[float]] = None
    trades: List[TradeIn] = []
    state: Optional[CyclePatch] = None


class BacksolveIn(BaseModel):
    version: str
    ticker: str
    splits: int
    principal_usd: float = Field(gt=0)
    qty: int = Field(ge=0)
    avg_price: float = Field(ge=0)
    cash_usd: Optional[float] = None
    quarter_sell_price: Optional[float] = None       # price of the outstanding quarter-sell LOC order (reveals T exactly)
    invested_usd: Optional[float] = None


def _validate(version, ticker, splits):
    if version not in VERSIONS:
        raise HTTPException(422, "unknown version")
    if ticker not in TICKERS:
        raise HTTPException(422, "ticker must be TQQQ or SOXL")
    if splits not in VERSIONS[version]:
        raise HTTPException(422, f"splits for {version}: {VERSIONS[version]}")


def _state(row):
    return dict(version=row["version"], ticker=row["ticker"], splits=int(row["splits"]), principal=float(row["principal_usd"]), cash=float(row["cash_usd"]),
                qty=int(row["qty"]), avg=float(row["avg_price"]), T=float(row["t_value"]), mode=row.get("mode", "normal"), compounding=bool(row.get("compounding", True)), extras=row.get("extras") or {})


def _view(row):
    out = {k: v for k, v in row.items() if k != "password_hash"}
    st = _state(row)
    sp = E.star_pct(st["version"], st["ticker"], st["splits"], st["T"])
    out["star_pct"] = round(sp, 3)
    out["star_price"] = round(st["avg"] * (1 + sp / 100.0), 2) if st["qty"] > 0 else None
    out["unit_usd"] = round(E.unit_amount(st), 2)
    out["first_half"] = E.is_first_half(st)
    out["invested_usd"] = round(st["avg"] * st["qty"], 2)
    out["used_seed_usd"] = round(st["principal"] - st["cash"], 2)                       # "사용한 시드": buys minus sell proceeds of this cycle
    out["progress_pct"] = round((st["principal"] - st["cash"]) / st["principal"] * 100.0, 1) if st["principal"] > 0 else 0.0     # "진행도" of the owner's app = used seed / seed
    out["target_pct"] = E.TARGET_PCT[st["version"]][st["ticker"]]
    pc = row.get("prev_close")
    live = row.get("status") == "active"
    out["orders"] = E.plan_orders(st, float(pc) if pc else (st["avg"] or 0.0), row.get("closes5")) if (live and (pc or st["qty"] > 0)) else []
    return out


def _next_key(key: str) -> str:
    return store.next_key(key)


def _close_and_open_next(row, st_after):
    return store.close_and_open_next(_sb, row, st_after)


@router.get("/cycles")
def list_cycles(request: Request, x_infinite_password: Optional[str] = Header(None)):
    """All cycles whose password equals the one sent (newest first)."""
    _need_db()
    ip = _client(request)
    _check_rate(ip)
    if not x_infinite_password:
        _fail(ip)
    rows = _sb.table("infinite_cycles").select("*").order("created_at", desc=True).execute().data or []
    mine = [r for r in rows if verify_password(x_infinite_password, r["password_hash"])]
    if not mine:
        _fail(ip)
    return {"status": "success", "data": [_view(r) for r in mine]}


@router.post("/cycles")
def create_cycle(body: CycleIn, request: Request, x_infinite_password: Optional[str] = Header(None)):
    """New cycle by hand (another account / another strategy line). Needs the password of an existing cycle; the new cycle shares that password. auto_order starts OFF."""
    _need_db()
    ip = _client(request)
    _check_rate(ip)
    if not x_infinite_password:
        _fail(ip)
    rows = _sb.table("infinite_cycles").select("cycle_key,password_hash").execute().data or []
    mine = [r for r in rows if verify_password(x_infinite_password, r["password_hash"])]
    if not mine:
        _fail(ip)
    _validate(body.version, body.ticker, body.splits)
    if body.start_date:
        try:
            _date.fromisoformat(body.start_date)
        except ValueError:
            raise HTTPException(422, "bad start_date")
    taken = {r["cycle_key"] for r in rows}
    key = body.cycle_key or f"{body.ticker.lower()}-{body.version.lower().replace('.', '')}"
    if body.cycle_key and key in taken:
        raise HTTPException(409, "cycle key already exists")
    base, n = key, 1
    while key in taken:
        n += 1
        key = f"{base}-{n}"
    exch, mk = TICKERS[body.ticker]
    cash = body.cash_usd if body.cash_usd is not None else max(body.principal_usd - body.qty * body.avg_price, 0.0)
    row = dict(cycle_key=key, password_hash=mine[0]["password_hash"], cycle_no=1, name=body.name, version=body.version, ticker=body.ticker, exch_code=exch, market_cls=mk, splits=body.splits,
               principal_usd=body.principal_usd, compounding=body.compounding, start_date=body.start_date, mode="normal", status="active", t_value=body.t_value, cash_usd=round(cash, 2), qty=body.qty,
               avg_price=body.avg_price, auto_order=False, extras=dict(body.extras or {}, t_mode="app"))
    r = _sb.table("infinite_cycles").insert(row).execute()
    return {"status": "success", "data": _view(r.data[0])}


@router.put("/cycles/{cycle_id}")
def update_cycle(cycle_id: str, body: CyclePatch, request: Request, x_infinite_password: Optional[str] = Header(None)):
    _cycle_for(cycle_id, x_infinite_password, request)
    patch = {k: v for k, v in body.dict().items() if v is not None}
    if "mode" in patch and patch["mode"] not in ("normal", "reverse", "quarter_stop"):
        raise HTTPException(422, "bad mode")
    if "status" in patch and patch["status"] not in ("active", "paused", "done"):
        raise HTTPException(422, "bad status")
    if not patch:
        raise HTTPException(422, "nothing to update")
    patch["updated_at"] = _now()
    r = _sb.table("infinite_cycles").update(patch).eq("id", cycle_id).execute()
    return {"status": "success", "data": _view(r.data[0])}


@router.post("/cycles/{cycle_id}/password")
def change_password(cycle_id: str, body: dict, request: Request, x_infinite_password: Optional[str] = Header(None)):
    """Set a new password on this cycle (and every cycle that shared the old one)."""
    row = _cycle_for(cycle_id, x_infinite_password, request)
    new = str(body.get("new_password") or "")
    if len(new) < 8:
        raise HTTPException(422, "new password needs at least 8 characters")
    allr = _sb.table("infinite_cycles").select("id,password_hash").execute().data or []
    h = hash_password(new)
    n = 0
    for r in allr:
        if r["password_hash"] == row["password_hash"]:
            _sb.table("infinite_cycles").update({"password_hash": h, "updated_at": _now()}).eq("id", r["id"]).execute()
            n += 1
    return {"status": "success", "updated": n}


@router.delete("/cycles/{cycle_id}")
def delete_cycle(cycle_id: str, request: Request, x_infinite_password: Optional[str] = Header(None)):
    _cycle_for(cycle_id, x_infinite_password, request)
    _sb.table("infinite_cycles").delete().eq("id", cycle_id).execute()
    return {"status": "success"}


@router.get("/cycles/{cycle_id}/trades")
def list_trades(cycle_id: str, request: Request, limit: int = Query(200, ge=1, le=1000), x_infinite_password: Optional[str] = Header(None)):
    _cycle_for(cycle_id, x_infinite_password, request)
    r = _sb.table("infinite_trades").select("*").eq("cycle_id", cycle_id).order("trade_date", desc=True).order("id", desc=True).limit(limit).execute()
    return {"status": "success", "data": r.data or []}


@router.post("/cycles/{cycle_id}/trades")
def add_trade(cycle_id: str, body: TradeIn, request: Request, x_infinite_password: Optional[str] = Header(None)):
    """Manual fill: recorded and applied with the rule engine. A fill that empties the position closes the cycle and opens the next one (returned as `next`)."""
    row = _cycle_for(cycle_id, x_infinite_password, request)
    if body.side not in ("buy", "sell"):
        raise HTTPException(422, "bad side")
    try:
        _date.fromisoformat(body.trade_date)
    except ValueError:
        raise HTTPException(422, "bad trade_date")
    if row.get("status") == "done":
        raise HTTPException(409, "cycle is done")
    st = E.apply_fills(_state(row), [dict(side=body.side, qty=body.qty, price=body.price, tag=body.tag or "", t_step=body.t_step)])
    _sb.table("infinite_trades").insert(dict(cycle_id=cycle_id, trade_date=body.trade_date, side=body.side, qty=body.qty, price=body.price, tag=body.tag, order_no=body.order_no,
                                             t_after=st["T"], source=body.source)).execute()
    if st["qty"] <= 0 and int(row["qty"]) > 0:
        nxt = _close_and_open_next(row, st)
        done = _sb.table("infinite_cycles").select("*").eq("id", cycle_id).execute().data[0]
        return {"status": "success", "data": _view(done), "next": _view(nxt)}
    upd = dict(t_value=st["T"], cash_usd=st["cash"], qty=st["qty"], avg_price=st["avg"], principal_usd=st["principal"], extras=st["extras"], updated_at=_now())
    r = _sb.table("infinite_cycles").update(upd).eq("id", cycle_id).execute()
    return {"status": "success", "data": _view(r.data[0]), "next": None}


@router.post("/cycles/{cycle_id}/sync")
def sync_cycle(cycle_id: str, body: SyncIn, request: Request, x_infinite_password: Optional[str] = Header(None)):
    """Local executor: store the last close(s), the day's fills (de-duplicated by the unique key) and optionally the authoritative state."""
    _cycle_for(cycle_id, x_infinite_password, request)
    patch = {}
    if body.prev_close:
        patch["prev_close"] = body.prev_close
    if body.closes5:
        patch["closes5"] = body.closes5[-5:]
    if body.state:
        patch.update({k: v for k, v in body.state.dict().items() if v is not None})
    if patch:
        patch["updated_at"] = _now()
        _sb.table("infinite_cycles").update(patch).eq("id", cycle_id).execute()
    rows = [dict(cycle_id=cycle_id, trade_date=t.trade_date, side=t.side, qty=t.qty, price=t.price, tag=t.tag, order_no=t.order_no, source=t.source) for t in body.trades]
    if rows:
        _sb.table("infinite_trades").upsert(rows, on_conflict="cycle_id,order_no,trade_date,side,qty,price").execute()
    r = _sb.table("infinite_cycles").select("*").eq("id", cycle_id).execute()
    return {"status": "success", "data": _view(r.data[0]) if r.data else None, "trades": len(rows)}


@router.post("/backsolve")
def backsolve(body: BacksolveIn, request: Request, x_infinite_password: Optional[str] = Header(None)):
    """Where is the account now? V2.2: exact from quantity and average price (T = invested / unit). V4.0: exact from the price of the outstanding quarter-sell LOC order, otherwise a range only.
    Needs the password of any existing cycle (the calculation itself is stateless)."""
    _need_db()
    ip = _client(request)
    _check_rate(ip)
    if not x_infinite_password:
        _fail(ip)
    rows = _sb.table("infinite_cycles").select("password_hash").execute().data or []
    if not any(verify_password(x_infinite_password, r["password_hash"]) for r in rows):
        _fail(ip)
    _validate(body.version, body.ticker, body.splits)
    invested = body.invested_usd if body.invested_usd is not None else body.qty * body.avg_price
    cash = body.cash_usd if body.cash_usd is not None else max(body.principal_usd - invested, 0.0)
    unit = body.principal_usd / body.splits
    out = dict(invested_usd=round(invested, 2), cash_usd=round(cash, 2), unit_usd=round(unit, 2), method=None, t_value=None, t_min=None, t_max=None, notes=[])
    if body.version == "V2.2":
        out.update(method="V2.2 definition: T = invested / unit", t_value=round(invested / unit, 2))
    elif body.quarter_sell_price and body.avg_price > 0:
        t = E.implied_T(body.version, body.ticker, body.splits, body.avg_price, body.quarter_sell_price)
        out.update(method="from the outstanding quarter-sell LOC price (star point)", t_value=round(t, 3))
    else:
        out.update(method="range only", t_min=round(invested / unit * 0.5, 2), t_max=round(invested / unit * 2.0, 2))
        out["notes"].append("V4.0 T depends on the buy/sell path; give the price of the outstanding quarter-sell order (or T) for an exact value.")
    t = out["t_value"]
    if t is not None:
        sp = E.star_pct(body.version, body.ticker, body.splits, t)
        out.update(star_pct=round(sp, 3), star_price=round(body.avg_price * (1 + sp / 100.0), 2) if body.avg_price else None, first_half=bool(t < body.splits / 2.0))
    return {"status": "success", "data": out}
