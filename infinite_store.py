"""infinite_store.py - cycle bookkeeping shared by the API (infinite_api.py) and the local executor (주식/infinite_executor.py). Needs only a Supabase client; no web framework."""
import re
from datetime import datetime as _dt, timezone as _tz


def now_iso():
    return _dt.now(_tz.utc).isoformat()


def next_key(key: str) -> str:
    m = re.match(r"^(.*?)(?:-c(\d+))?$", key)
    base, n = m.group(1), int(m.group(2) or 1)
    return f"{base}-c{n + 1}"


def close_and_open_next(sb, row, st_after, today=None):
    """A fill emptied the position: close the cycle (status done, realised result) and create the next cycle as a NEW row (same owner; principal re-based by the engine). Returns the new row."""
    today = today or _dt.now(_tz.utc).date().isoformat()
    realized = round(float(st_after["cash"]) - float(row["principal_usd"]), 2)
    sb.table("infinite_cycles").update(dict(status="done", end_date=today, realized_usd=realized, t_value=0, qty=0, avg_price=0, cash_usd=round(float(st_after["cash"]), 2), updated_at=now_iso())).eq("id", row["id"]).execute()
    nxt = dict(cycle_key=next_key(row["cycle_key"]), owner=row.get("owner"), cycle_no=int(row.get("cycle_no") or 1) + 1, prev_cycle_id=row["id"], name=row["name"], version=row["version"],
               ticker=row["ticker"], exch_code=row["exch_code"], market_cls=row["market_cls"], splits=row["splits"], principal_usd=round(float(st_after["principal"]), 2), compounding=row["compounding"], start_date=today,
               mode="normal", status="active", t_value=0, cash_usd=round(float(st_after["cash"]), 2) if row["compounding"] else row["principal_usd"], qty=0, avg_price=0, auto_order=row.get("auto_order", False),
               extras={k: v for k, v in (row.get("extras") or {}).items() if k in ("t_mode", "last_fill_ts", "ladder", "ladder_n")})
    return sb.table("infinite_cycles").insert(nxt).execute().data[0]
