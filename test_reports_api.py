import json
import pytest
from fastapi.testclient import TestClient

import main

TOKEN = "t-secret"
H = {"X-Report-Token": TOKEN}


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db):
        self.db, self.cols, self.f, self.desc, self.lim, self.up = db, None, [], False, None, None

    def select(self, cols):
        self.cols = cols
        self.db.selects.append(cols)
        return self

    def eq(self, c, v): self.f.append(lambda r: r[c] == v); return self
    def gte(self, c, v): self.f.append(lambda r: r[c] >= v); return self
    def lte(self, c, v): self.f.append(lambda r: r[c] <= v); return self

    def order(self, c, desc=False):
        self.desc = desc
        return self

    def limit(self, n):
        self.lim = n
        return self

    def upsert(self, row):
        self.up = row
        return self

    def execute(self):
        if self.up:
            self.db.rows[self.up["id"]] = dict(self.up)
            return _Res([self.up])
        rows = sorted((r for r in self.db.rows.values() if all(f(r) for f in self.f)),
                      key=lambda r: r["id"], reverse=self.desc)[: self.lim]
        keys = [c.strip() for c in self.cols.split(",")]
        return _Res([{k: r.get(k) for k in keys} for r in rows])


class FakeDB:
    def __init__(self):
        self.rows, self.selects = {}, []

    def table(self, name):
        assert name == "quant_screening_cache"
        return _Q(self)


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(main, "supabase", db)
    monkeypatch.setenv("REPORT_INGEST_TOKEN", TOKEN)
    main.reports_list_cache.clear()
    return db, TestClient(main.app)


def body(**kw):
    b = {"category": "morning", "report_date": "2026-10-20", "title": "T", "html": "<p>hi</p>"}
    b.update(kw)
    return b


def test_auth(env, monkeypatch):
    _, c = env
    assert c.post("/api/reports", json=body()).status_code == 401
    assert c.post("/api/reports", json=body(), headers={"X-Report-Token": "x"}).status_code == 401
    monkeypatch.delenv("REPORT_INGEST_TOKEN")
    assert c.post("/api/reports", json=body(), headers=H).status_code == 503
    monkeypatch.setenv("REPORT_INGEST_TOKEN", "")
    assert c.post("/api/reports", json=body(), headers=H).status_code == 503


def test_post_and_repost_replaces(env):
    db, c = env
    r = c.post("/api/reports", json=body(), headers=H)
    assert r.status_code == 200 and r.json()["id"] == 120261020 and r.json()["bytes"] == 9
    c.post("/api/reports", json=body(title="T2"), headers=H)
    assert len(db.rows) == 1
    assert json.loads(db.rows[120261020]["results"])["title"] == "T2"


@pytest.mark.parametrize("kw,code", [
    ({"category": "Bad"}, 422), ({"category": "nope"}, 422),
    ({"report_date": "2026-13-01"}, 422), ({"report_date": "2099-01-01"}, 422),
    ({"title": ""}, 422), ({"html": ""}, 422), ({"html": "a" * 3_000_001}, 413),
])
def test_rejects(env, kw, code):
    _, c = env
    assert c.post("/api/reports", json=body(**kw), headers=H).status_code == code


def test_list_get(env):
    db, c = env
    for d in ("2026-10-18", "2026-10-20", "2026-10-19"):
        c.post("/api/reports", json=body(report_date=d, title=d), headers=H)
    db.rows[5] = {"id": 5, "results": "{}", "updated_at": None}  # unrelated row
    db.selects.clear()
    r = c.get("/api/reports?category=morning&limit=2").json()
    assert [x["report_date"] for x in r["data"]] == ["2026-10-20", "2026-10-19"]
    assert db.selects and all("results" not in s for s in db.selects)
    g = c.get("/api/reports/120261019").json()
    assert g["title"] == "2026-10-19" and g["html"] == "<p>hi</p>" and g["category"] == "morning"
    assert c.get("/api/reports/120261001").status_code == 404
    assert c.get("/api/reports/5").status_code == 404
    assert c.get("/api/reports?limit=0").status_code == 422
