import itertools
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth_api
import infinite_api
import infinite_engine as E

app = FastAPI()
app.include_router(auth_api.router)
app.include_router(infinite_api.router)
PW = "correct-horse-battery"
H = {"Authorization": "Bearer " + auth_api.make_token("alice")}
H2 = {"Authorization": "Bearer " + auth_api.make_token("bob")}


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.f, self.op, self.payload, self.onc, self.desc = db, name, [], "select", None, None, False

    def select(self, cols="*"): self.op = "select"; return self
    def eq(self, c, v): self.f.append(lambda r: str(r.get(c)) == str(v)); return self
    def order(self, c, desc=False): self.desc = desc; return self
    def limit(self, n): return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def update(self, patch): self.op, self.payload = "update", patch; return self
    def delete(self): self.op = "delete"; return self
    def upsert(self, rows, on_conflict=None): self.op, self.payload, self.onc = "upsert", rows, on_conflict; return self

    def execute(self):
        t = self.db.tables.setdefault(self.name, [])
        if self.op == "insert":
            r = dict(self.payload)
            r.setdefault("id", f"id{next(self.db.seq)}")
            r.setdefault("created_at", f"2026-10-21T00:00:{next(self.db.seq):02d}")
            t.append(r)
            return _Res([r])
        if self.op == "upsert":
            keys = (self.onc or "").split(",")
            for row in self.payload:
                hit = next((x for x in t if all(x.get(k) == row.get(k) for k in keys)), None)
                if hit:
                    hit.update(row)
                else:
                    t.append(dict(row))
            return _Res(self.payload)
        rows = [r for r in t if all(f(r) for f in self.f)]
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
            return _Res(rows)
        if self.op == "delete":
            for r in rows:
                t.remove(r)
            return _Res(rows)
        rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=self.desc)
        return _Res([dict(r) for r in rows])


class FakeDB:
    def __init__(self):
        self.tables, self.seq = {}, itertools.count(10)

    def table(self, name):
        return _Q(self, name)


def cycle(**kw):
    b = dict(cycle_key="soxl-v4", owner="alice", cycle_no=1, prev_cycle_id=None, name="SOXL V4", version="V4.0", ticker="SOXL", exch_code="0066", market_cls="AX",
             splits=40, principal_usd=27567.4, compounding=True, start_date="2026-09-22", end_date=None, mode="normal", status="active", t_value=23.375, cash_usd=17236.33, qty=83, avg_price=125.6306,
             realized_usd=0, prev_close=97.55, closes5=None, extras={}, auto_order=False)
    b.update(kw)
    return b


@pytest.fixture
def env():
    db = FakeDB()
    auth_api._fails.clear()
    auth_api.init(db)
    infinite_api.init(db)
    for u, st in (("alice", "approved"), ("bob", "approved"), ("pat", "pending")):
        db.table("app_users").insert(dict(username=u, password_hash=auth_api.hash_password(PW), name=u, phone="01012345678", role="admin" if u == "alice" else "user", status=st)).execute()
    return db, TestClient(app)


def seed(db, **kw):
    r = _Q(db, "infinite_cycles").insert(cycle(**kw)).execute()
    return r.data[0]


def test_hash_and_token():
    h = auth_api.hash_password("abc12345")
    assert h.startswith("sha256$") and "abc12345" not in h
    assert auth_api.verify_password("abc12345", h) and not auth_api.verify_password("abc12346", h) and not auth_api.verify_password("x", "garbage")
    assert auth_api.hash_password("abc12345") != h, "random salt"
    t = auth_api.make_token("alice", now=1000)
    assert auth_api.parse_token(t, now=1000) == "alice" and auth_api.parse_token(t, now=1000 + 13 * 3600) is None
    assert auth_api.parse_token(t[:-1] + ("0" if t[-1] != "0" else "1"), now=1000) is None and auth_api.parse_token("alice.99999999999.bad") is None


def test_list_only_own_cycles(env):
    db, c = env
    seed(db)
    seed(db, cycle_key="other", owner="bob", name="other")
    assert c.get("/api/infinite/cycles").status_code == 401
    assert c.get("/api/infinite/cycles", headers={"Authorization": "Bearer junk"}).status_code == 401
    data = c.get("/api/infinite/cycles", headers=H).json()["data"]
    assert [d["name"] for d in data] == ["SOXL V4"], "only the caller's cycle"
    assert [d["name"] for d in c.get("/api/infinite/cycles", headers=H2).json()["data"]] == ["other"]
    pending = {"Authorization": "Bearer " + auth_api.make_token("pat")}
    assert c.get("/api/infinite/cycles", headers=pending).status_code == 401, "pending account has no access"


def test_view_fields_and_orders(env):
    db, c = env
    seed(db)
    d = c.get("/api/infinite/cycles", headers=H).json()["data"][0]
    assert abs(d["star_pct"] - (-3.375)) < 1e-6 and d["first_half"] is False and d["target_pct"] == 20.0
    assert d["star_price"] == round(125.6306 * (1 - 0.03375), 2)
    assert d["used_seed_usd"] == round(27567.4 - 17236.33, 2) and d["progress_pct"] == round((27567.4 - 17236.33) / 27567.4 * 100, 1)
    assert {"star_buy", "quarter_sell", "target_sell"} <= {o["tag"] for o in d["orders"]}
    seed(db, cycle_key="done", status="done", qty=0)
    done = [x for x in c.get("/api/infinite/cycles", headers=H).json()["data"] if x["cycle_key"] == "done"][0]
    assert done["orders"] == [], "a finished cycle has no order sheet"


def test_signup_login_approval(env):
    db, c = env
    body = dict(username="carol", password="longenough1", name="Carol", phone="010-1234-5678")
    assert c.post("/api/auth/signup", json=dict(body, username="a b")).status_code == 422
    assert c.post("/api/auth/signup", json=dict(body, password="short")).status_code == 422
    assert c.post("/api/auth/signup", json=body).status_code == 200
    assert c.post("/api/auth/signup", json=body).status_code == 409
    assert c.post("/api/auth/login", json=dict(username="carol", password="longenough1")).status_code == 403, "pending"
    assert c.get("/api/auth/admin/users", headers=H2).status_code == 403 and c.get("/api/auth/admin/users").status_code == 401
    rows = c.get("/api/auth/admin/users", headers=H).json()["data"]
    assert "carol" in [r["username"] for r in rows] and all("password_hash" not in r for r in rows)
    assert c.post("/api/auth/admin/users/carol/decision", json={"decision": "approve"}, headers=H2).status_code == 403
    assert c.post("/api/auth/admin/users/carol/decision", json={"decision": "maybe"}, headers=H).status_code == 422
    assert c.post("/api/auth/admin/users/alice/decision", json={"decision": "reject"}, headers=H).status_code == 409, "admin is protected"
    assert c.post("/api/auth/admin/users/carol/decision", json={"decision": "approve"}, headers=H).status_code == 200
    r = c.post("/api/auth/login", json=dict(username="carol", password="longenough1"))
    assert r.status_code == 200 and r.json()["user"]["role"] == "user"
    tok = {"Authorization": "Bearer " + r.json()["token"]}
    assert c.get("/api/auth/me", headers=tok).json()["user"]["username"] == "carol" and c.get("/api/infinite/cycles", headers=tok).json()["data"] == []
    c.post("/api/auth/admin/users/carol/decision", json={"decision": "reject"}, headers=H)
    assert c.get("/api/auth/me", headers=tok).status_code == 401, "rejection takes effect at once"
    assert c.post("/api/auth/login", json=dict(username="carol", password="longenough1")).status_code == 403


def test_login_rate_limit_and_wrong_password(env):
    db, c = env
    assert c.post("/api/auth/login", json=dict(username="nobody", password="x")).status_code == 401
    for _ in range(auth_api.MAX_FAILS):
        c.post("/api/auth/login", json=dict(username="alice", password="bad"))
    assert c.post("/api/auth/login", json=dict(username="alice", password=PW)).status_code == 429


def test_update_and_delete_own_cycle_only(env):
    db, c = env
    a = seed(db)
    assert c.put(f"/api/infinite/cycles/{a['id']}", json={"t_value": 24.0}).status_code == 401
    assert c.put(f"/api/infinite/cycles/{a['id']}", json={"t_value": 24.0}, headers=H2).status_code == 404, "another user's cycle"
    assert c.put("/api/infinite/cycles/id-nope", json={"t_value": 24.0}, headers=H).status_code == 404
    r = c.put(f"/api/infinite/cycles/{a['id']}", json={"t_value": 24.0, "auto_order": True}, headers=H)
    assert r.status_code == 200 and r.json()["data"]["t_value"] == 24.0 and r.json()["data"]["auto_order"] is True
    assert c.put(f"/api/infinite/cycles/{a['id']}", json={}, headers=H).status_code == 422
    assert c.put(f"/api/infinite/cycles/{a['id']}", json={"mode": "bogus"}, headers=H).status_code == 422
    assert c.delete(f"/api/infinite/cycles/{a['id']}", headers=H2).status_code == 404
    assert c.delete(f"/api/infinite/cycles/{a['id']}", headers=H).status_code == 200
    assert db.tables["infinite_cycles"] == []


def test_manual_trade_applies_engine(env):
    db, c = env
    a = seed(db, t_value=10.5, qty=27, avg_price=147.667, cash_usd=20000)
    r = c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "2026-10-02", "side": "sell", "qty": 6, "price": 163.71, "tag": "quarter_sell"}, headers=H)
    d = r.json()["data"]
    assert abs(d["t_value"] - 7.875) < 1e-9 and d["qty"] == 21 and r.json()["next"] is None
    assert c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "bad", "side": "sell", "qty": 1, "price": 1}, headers=H).status_code == 422
    tr = c.get(f"/api/infinite/cycles/{a['id']}/trades", headers=H).json()["data"]
    assert len(tr) == 1 and tr[0]["t_after"] == 7.875


def test_create_cycle(env):
    db, c = env
    seed(db)
    body = dict(name="TQQQ V2.2", version="V2.2", ticker="TQQQ", splits=30, principal_usd=30000, qty=100, avg_price=80.0, t_value=8.0)
    assert c.post("/api/infinite/cycles", json=body).status_code == 401
    assert c.post("/api/infinite/cycles", json=dict(body, splits=25), headers=H).status_code == 422
    r = c.post("/api/infinite/cycles", json=body, headers=H)
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["cycle_key"] == "tqqq-v22" and d["exch_code"] == "0537" and d["auto_order"] is False and d["cash_usd"] == 22000.0 and d["status"] == "active"
    assert db.tables["infinite_cycles"][1]["owner"] == "alice"
    r2 = c.post("/api/infinite/cycles", json=body, headers=H).json()["data"]
    assert r2["cycle_key"] == "tqqq-v22-2", "auto key is made unique"
    assert c.post("/api/infinite/cycles", json=dict(body, cycle_key="soxl-v4"), headers=H).status_code == 409
    assert len(c.get("/api/infinite/cycles", headers=H).json()["data"]) == 3


def test_manual_half_step(env):
    db, c = env
    a = seed(db, t_value=9.375, qty=27, avg_price=150.96, cash_usd=20000)
    # one order filled: rule engine says +0.5; manual t_step 1 overrides it, 0.5 keeps half
    r = c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "2026-10-09", "side": "buy", "qty": 2, "price": 142.52, "tag": "star_buy", "t_step": 0.5}, headers=H).json()
    assert abs(r["data"]["t_value"] - 9.875) < 1e-9
    r = c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "2026-10-09", "side": "buy", "qty": 3, "price": 142.52, "tag": "avg_buy", "t_step": 1}, headers=H).json()
    assert abs(r["data"]["t_value"] - 10.875) < 1e-9
    assert c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "2026-10-09", "side": "buy", "qty": 3, "price": 142.52, "t_step": 3}, headers=H).status_code == 422


def test_cycle_end_opens_next_cycle(env):
    db, c = env
    a = seed(db, qty=10, avg_price=100.0, t_value=12.0, cash_usd=26000.0, principal_usd=27400.0, cycle_key="soxl-v4")
    r = c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "2026-10-30", "side": "sell", "qty": 10, "price": 120.0, "tag": "target_sell"}, headers=H).json()
    assert r["data"]["status"] == "done" and r["data"]["realized_usd"] == round(27200.0 - 27400.0, 2)
    nxt = r["next"]
    assert nxt["cycle_key"] == "soxl-v4-c2" and nxt["cycle_no"] == 2 and nxt["status"] == "active" and nxt["qty"] == 0 and nxt["t_value"] == 0
    assert abs(nxt["principal_usd"] - 27200.0) < 1e-6 and abs(nxt["cash_usd"] - 27200.0) < 1e-6 and nxt["prev_cycle_id"] == a["id"]
    assert len(db.tables["infinite_cycles"]) == 2 and db.tables["infinite_cycles"][1]["owner"] == "alice", "next cycle keeps the owner"
    assert c.post(f"/api/infinite/cycles/{a['id']}/trades", json={"trade_date": "2026-10-31", "side": "buy", "qty": 1, "price": 1.0}, headers=H).status_code == 409
    assert len(c.get("/api/infinite/cycles", headers=H).json()["data"]) == 2
    assert infinite_api._next_key("soxl-v4-c2") == "soxl-v4-c3"


def test_sync_dedupes_trades(env):
    db, c = env
    a = seed(db)
    body = {"prev_close": 97.55, "closes5": [100, 99, 98, 97, 96, 95], "trades": [{"trade_date": "2026-10-20", "side": "buy", "qty": 8, "price": 97.55, "tag": "ladder", "order_no": "1", "source": "meritz"}]}
    r1 = c.post(f"/api/infinite/cycles/{a['id']}/sync", json=body, headers=H)
    r2 = c.post(f"/api/infinite/cycles/{a['id']}/sync", json=body, headers=H)
    assert r1.status_code == r2.status_code == 200
    assert len(db.tables["infinite_trades"]) == 1
    d = r2.json()["data"]
    assert d["prev_close"] == 97.55 and d["closes5"] == [99, 98, 97, 96, 95]


def test_backsolve(env):
    db, c = env
    seed(db)
    v22 = {"version": "V2.2", "ticker": "TQQQ", "splits": 40, "principal_usd": 40000, "qty": 100, "avg_price": 80}
    assert c.post("/api/infinite/backsolve", json=v22).status_code == 401
    d = c.post("/api/infinite/backsolve", json=v22, headers=H).json()["data"]
    assert d["t_value"] == 8.0 and d["first_half"] is True and abs(d["star_pct"] - 6.0) < 1e-9
    v4 = {"version": "V4.0", "ticker": "SOXL", "splits": 40, "principal_usd": 27400, "qty": 8, "avg_price": 149.1, "quarter_sell_price": 174.45}
    d = c.post("/api/infinite/backsolve", json=v4, headers=H).json()["data"]
    assert abs(d["t_value"] - 3.0) < 0.01 and d["method"].startswith("from the outstanding")
    v4["quarter_sell_price"] = None
    d = c.post("/api/infinite/backsolve", json=v4, headers=H).json()["data"]
    assert d["t_value"] is None and d["t_min"] is not None and d["notes"]
    assert c.post("/api/infinite/backsolve", json=dict(v4, splits=30), headers=H).status_code == 422
