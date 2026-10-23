"""news_ingest_api.py - POST /api/news/ingest: a scheduled Cowork task sends collected articles as JSON; the server validates, de-duplicates and stores them in market_news.
Auth: header X-News-Token == env NEWS_INGEST_TOKEN (hmac compare; 503 when unset). Mounted by main.py: news_ingest_api.init(supabase); app.include_router(news_ingest_api.router).
Rules (same as the old Doc sync): published_at in KST, rejected when older than 36 h or more than 3 h ahead; tags only from the fixed list (news_tags.py); duplicates by title /
Dice / shared words (news_parse.is_duplicate) or by the same canonical source URL. Calling twice with the same items is safe: the second call stores nothing.
"""
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlsplit, parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel, Field

from news_parse import KST, is_duplicate, to_kst
from news_tags import normalize_category, normalize_tags, rule_tags

router = APIRouter(prefix="/api/news")
_sb = None
MAX_BYTES = 1_000_000
MAX_AGE_H, MAX_FUTURE_H = 36, 3
_INDEX = r"(코스피|코스닥|나스닥|S&P|다우|닛케이|항셍|상하이종합|필라델피아 반도체|반도체지수|DAX|유로스톡스)"
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_CENTRAL = re.compile(r"(기준금리|금리|FOMC|연준|금통위|한국은행|ECB|BOJ|일본은행|영란은행).{0,18}(동결|인하|인상|결정|유지)|(동결|인하|인상).{0,10}(기준금리|FOMC|금통위)")


def infer_major(title, is_major):
    """Backstop for the model's Is Major flag (it often misses these): an index named in the title with a move of 2% or more, or a central-bank rate decision.
    Only upgrades False -> True; the model's True is kept."""
    if is_major:
        return True
    if re.search(_INDEX, title) and any(float(x) >= 2.0 for x in _PCT.findall(title)):
        return True
    return bool(_CENTRAL.search(title))


_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|ref$|cmpid$)", re.I)


def init(supabase):
    global _sb
    _sb = supabase


class NewsItem(BaseModel):
    category: str = Field(max_length=40)
    assets: str = Field("", max_length=200)
    title: str = Field(min_length=1, max_length=200)
    bullets: List[str] = Field(min_length=1, max_length=6)
    sentiment: int = Field(ge=1, le=5)
    is_major: bool = False
    published_at: str = Field(max_length=16)          # "YYYY-MM-DD HH:MM", KST
    tags: List[str] = Field(default_factory=list, max_length=12)
    source_url: Optional[str] = Field(None, max_length=500)


class NewsBatch(BaseModel):
    items: List[NewsItem] = Field(min_length=1, max_length=40)


def canon_url(u):
    """http(s) URL without scheme / www / fragment / tracking params / trailing slash, lower-cased host; None when not a plain public http(s) URL."""
    try:
        p = urlsplit((u or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    host = p.hostname.lower().removeprefix("www.")
    q = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)))
    return f"{host}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def _ts(v):
    return datetime.fromisoformat(str(v)[:19])


def _recent():
    """Last 600 stored rows as (title, category, created_at, summary) + the set of their canonical URLs. Works before the source_url column exists."""
    cols = "title,region,created_at,summary,source_url"
    try:
        data = _sb.table("market_news").select(cols).order("created_at", desc=True).limit(600).execute().data or []
    except Exception as e:
        if "source_url" not in str(e):
            raise
        data = _sb.table("market_news").select(cols.replace(",source_url", "")).order("created_at", desc=True).limit(600).execute().data or []
    rec = [(x["title"], normalize_category(x.get("region")), _ts(x["created_at"]), x.get("summary") or "") for x in data if x.get("created_at")]
    urls = {canon_url(x.get("source_url")) for x in data if x.get("source_url")} - {None}
    return rec, urls


OPTIONAL_COLS = ("source_url", "ingested_at")        # columns added later (sql/news_tags.sql): stored without them until the SQL has been run


def _insert(row):
    """Insert; when the database says an optional column does not exist yet, drop that column and retry."""
    for _ in range(len(OPTIONAL_COLS) + 1):
        try:
            _sb.table("market_news").insert(row).execute()
            return
        except Exception as e:
            miss = [c for c in OPTIONAL_COLS if c in row and c in str(e)]
            if not miss:
                raise
            for c in miss:
                row.pop(c)
    raise RuntimeError("insert failed")


@router.get("/ping")
def ping():
    """Wake-up call for the collector (the free Render instance sleeps)."""
    return {"ok": True}


@router.post("/ingest")
def ingest(body: NewsBatch, request: Request, x_news_token: Optional[str] = Header(None)):
    expected = os.environ.get("NEWS_INGEST_TOKEN")
    if not expected:
        raise HTTPException(503, "news ingest disabled")
    if not x_news_token or not hmac.compare_digest(x_news_token.encode(), expected.encode()):
        raise HTTPException(401, "invalid token")
    if _sb is None:
        raise HTTPException(503, "DB not configured")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BYTES:
        raise HTTPException(413, "payload too large")
    now = datetime.now(timezone.utc).astimezone(KST).replace(tzinfo=None)
    try:
        recent, urls = _recent()
    except Exception as e:
        print(f"[news ingest] cannot read stored rows: {type(e).__name__}")
        raise HTTPException(503, "stored news unreadable")      # storing without the duplicate check would double rows
    out = dict(inserted=0, skipped=0, no_url=0, rule_tagged=0, rejected=[], unknown_tags=[])
    for it in body.items:
        def rej(why):
            out["rejected"].append({"title": it.title[:60], "reason": why})
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", it.published_at.strip())
        if not m:
            rej("bad published_at (need YYYY-MM-DD HH:MM KST)"); continue
        try:
            created = to_kst(m.group(1), m.group(2))
        except ValueError:
            rej("bad published_at"); continue
        if created < now - timedelta(hours=MAX_AGE_H):
            rej(f"stale: {created:%Y-%m-%d %H:%M}"); continue
        if created > now + timedelta(hours=MAX_FUTURE_H):
            rej(f"future: {created:%Y-%m-%d %H:%M}"); continue
        url = None
        if it.source_url:
            url = canon_url(it.source_url)
            if url is None:
                rej("bad source_url"); continue
        else:
            out["no_url"] += 1
        bullets = [re.sub(r"^[-\s]+", "", b).strip() for b in it.bullets]
        bullets = [b for b in bullets if b]
        if not bullets:
            rej("empty summary"); continue
        tags, unknown = normalize_tags(",".join(it.tags))
        out["unknown_tags"] += unknown
        from_rules = not tags
        if from_rules:
            tags = rule_tags(it.title, " ".join(bullets), it.assets)
        category = normalize_category(it.category, tags)
        summary = "<br><br>".join("- " + b for b in bullets)
        if (url and url in urls) or is_duplicate(it.title, category, created, recent, summary=summary):
            out["skipped"] += 1; continue
        row = {"region": category, "sector_asset": it.assets.strip(), "title": it.title.strip(), "summary": summary, "sentiment_score": it.sentiment,
               "is_major": infer_major(it.title, it.is_major), "created_at": created.isoformat(), "tags": tags,
               "ingested_at": datetime.now(timezone.utc).isoformat()}      # when the server stored it; created_at is the publication time (KST wall clock)
        if it.source_url:
            row["source_url"] = it.source_url.strip()
        try:
            _insert(row)
        except Exception as e:
            print(f"[news ingest] insert failed: {type(e).__name__}")
            rej("store failed"); continue
        recent.append((it.title, category, created, summary))
        if url:
            urls.add(url)
        out["inserted"] += 1
        out["rule_tagged"] += int(from_rules)
    out["unknown_tags"] = sorted(set(out["unknown_tags"]))
    return {"status": "success", **out}
