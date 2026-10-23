"""Tests of POST /api/news/ingest (fake Supabase). Run: python -m pytest test_news_ingest_api.py  (self-verification only)"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import news_ingest_api as N

app = FastAPI()
app.include_router(N.router)

TOKEN = "t-news"
H = {"X-News-Token": TOKEN}


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db):
        self.db, self.ins = db, None

    def select(self, cols): self.cols = cols; return self
    def order(self, *a, **k): return self
    def limit(self, n): return self

    def insert(self, row):
        self.ins = row
        return self

    def execute(self):
        if self.ins is not None:
            for c in self.db.missing_cols:
                if c in self.ins:
                    raise Exception(f'column "{c}" of relation "market_news" does not exist')
            self.db.rows.append(dict(self.ins))
            return _Res([self.ins])
        keys = [c.strip() for c in self.cols.split(",")]
        if self.db.no_url_column and "source_url" in keys:
            raise Exception('column market_news.source_url does not exist')
        return _Res([{k: r.get(k) for k in keys} for r in reversed(self.db.rows)])


class FakeDB:
    def __init__(self):
        self.rows, self.no_url_column, self.missing_cols = [], False, []

    def table(self, name):
        assert name == "market_news"
        return _Q(self)


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(N, "_sb", db)
    monkeypatch.setenv("NEWS_INGEST_TOKEN", TOKEN)
    return db, TestClient(app)


def kst_now():
    return datetime.now(timezone.utc).astimezone(N.KST).replace(tzinfo=None)


def item(**kw):
    t = kst_now() - timedelta(hours=2)
    d = dict(category="기업/산업", assets="삼성전자", title="삼성전자 HBM4 공급 확대 발표", bullets=["- 공급 확대", "수율 개선"], sentiment=4, is_major=True,
             published_at=t.strftime("%Y-%m-%d %H:%M"), tags=["반도체", "AI"], source_url="https://www.example.com/a/1?utm_source=x")
    d.update(kw)
    return d


def post(c, *items, headers=H):
    return c.post("/api/news/ingest", json={"items": list(items)}, headers=headers)


def test_auth(env, monkeypatch):
    db, c = env
    assert post(c, item(), headers={}).status_code == 401
    assert post(c, item(), headers={"X-News-Token": "x"}).status_code == 401
    monkeypatch.delenv("NEWS_INGEST_TOKEN")
    assert post(c, item()).status_code == 503
    assert db.rows == []


def test_store_and_idempotent(env):
    db, c = env
    r = post(c, item()).json()
    assert r["inserted"] == 1 and not r["rejected"] and len(db.rows) == 1
    row = db.rows[0]
    assert row["summary"] == "- 공급 확대<br><br>- 수율 개선" and row["tags"] == ["반도체", "AI"] and row["region"] == "기업/산업"
    r = post(c, item()).json()
    assert r["inserted"] == 0 and r["skipped"] == 1 and len(db.rows) == 1, "same batch twice stores once"


def test_same_url_different_title_is_duplicate(env):
    db, c = env
    post(c, item())
    r = post(c, item(title="완전히 다른 제목의 같은 기사 보도", source_url="http://example.com/a/1/")).json()
    assert r["skipped"] == 1 and len(db.rows) == 1


def test_rejects(env):
    db, c = env
    old = (kst_now() - timedelta(hours=60)).strftime("%Y-%m-%d %H:%M")
    fut = (kst_now() + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    r = post(c, item(published_at=old, title="a"), item(published_at=fut, title="b"), item(published_at="어제", title="c"),
             item(source_url="javascript:alert(1)", title="d"), item(source_url="https://u:p@evil.com/x", title="e")).json()
    why = [x["reason"].split(":")[0].split(" (")[0] for x in r["rejected"]]
    assert r["inserted"] == 0 and why == ["stale", "future", "bad published_at", "bad source_url", "bad source_url"], why
    assert db.rows == []


def test_unknown_tags_and_rule_fallback_and_no_url(env):
    db, c = env
    r = post(c, item(tags=["반도체", "없는태그"], source_url=None), item(title="LG에너지솔루션 배터리 수주", tags=["없는태그"], assets="LG에너지솔루션")).json()
    assert r["inserted"] == 2 and r["no_url"] == 1 and r["unknown_tags"] == ["없는태그"] and r["rule_tagged"] == 1
    assert db.rows[0]["tags"] == ["반도체"] and "source_url" not in db.rows[0] and "2차전지" in db.rows[1]["tags"]


def test_validation_limits(env):
    db, c = env
    assert c.post("/api/news/ingest", json={"items": []}, headers=H).status_code == 422
    assert c.post("/api/news/ingest", json={"items": [item(sentiment=9)]}, headers=H).status_code == 422
    assert c.post("/api/news/ingest", json={"items": [item()] * 41}, headers=H).status_code == 422


def test_works_before_source_url_column_exists(env):
    db, c = env
    db.no_url_column = True; db.missing_cols = ["source_url"]
    r = post(c, item()).json()
    assert r["inserted"] == 1 and "source_url" not in db.rows[0]


def test_canon_url():
    assert N.canon_url("https://www.Example.com/a/1/?utm_source=x&b=2#f") == "example.com/a/1?b=2"
    assert N.canon_url("ftp://x.com/a") is None and N.canon_url("not a url") is None


def test_ping(env):
    assert env[1].get("/api/news/ping").json() == {"ok": True}


def test_infer_major():
    assert N.infer_major("미 반도체 급락 여파로 아시아 증시 하락, 코스닥 3.5%·코스피 2.2% 급락", False)
    assert N.infer_major("한국은행 기준금리 연 3.00% 동결", False) and N.infer_major("연준 금리 인상 결정", False)
    assert not N.infer_major("코스피 0.8% 상승 마감", False) and not N.infer_major("미국 10년물 금리 5% 아래로 하락", False)
    assert N.infer_major("아무 제목", True)


def test_major_stored(env):
    db, c = env
    post(c, item(title="코스피 3.5% 급락, 외국인 매도", is_major=False, source_url=None))
    assert db.rows[0]["is_major"] is True


def test_ingested_at_stored_and_optional(env):
    db, c = env
    post(c, item())
    assert db.rows[0]["ingested_at"] and db.rows[0]["created_at"] != db.rows[0]["ingested_at"]
    db.rows.clear(); db.missing_cols = ["ingested_at"]
    r = post(c, item(title="다른 기사 제목 하나")).json()
    assert r["inserted"] == 1 and "ingested_at" not in db.rows[0] and "source_url" in db.rows[0]
    db.rows.clear(); db.missing_cols = ["ingested_at", "source_url"]
    r = post(c, item(title="또 다른 제목 둘째")).json()
    assert r["inserted"] == 1 and "ingested_at" not in db.rows[0] and "source_url" not in db.rows[0]
