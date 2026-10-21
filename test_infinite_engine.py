"""Offline checks of infinite_engine.py (self-verification only). Fixtures come from the user's own Meritz SOXL history (cycle started 2026-09-22): star prices, quarter / target sells, order prices and quantities.
Run: python -X utf8 test_infinite_engine.py"""
import infinite_engine as E


def st(**kw):
    d = dict(version="V4.0", ticker="SOXL", splits=40, principal=27400.0, cash=0.0, qty=0, avg=0.0, T=0.0, mode="normal", compounding=True, extras={})
    d.update(kw)
    return d


# ---- star percent
assert abs(E.star_pct("V4.0", "SOXL", 40, 1) - 19) < 1e-9 and abs(E.star_pct("V4.0", "SOXL", 20, 2) - 16) < 1e-9
assert abs(E.star_pct("V4.0", "TQQQ", 40, 8) - 9) < 1e-9 and abs(E.star_pct("V4.0", "TQQQ", 20, 4) - 9) < 1e-9
assert abs(E.star_pct("V2.2", "TQQQ", 40, 10) - 5) < 1e-9 and abs(E.star_pct("V2.2", "TQQQ", 20, 5) - 5) < 1e-9, "10 - (T/2)*(40/N)"
assert abs(E.star_pct("V4.0", "SOXL", 40, 20)) < 1e-9 and E.star_pct("V4.0", "SOXL", 40, 23.38) < 0

# ---- implied T from the outstanding quarter-sell order (user history: 2026-09-24 avg 149.10, LOC sell 174.45 -> T = 3; 2026-10-20 avg 128.626, LOC sell 124.28 -> T = 23.38)
assert abs(E.implied_T("V4.0", "SOXL", 40, 149.1, 174.45) - 3.0) < 0.01
assert abs(E.implied_T("V4.0", "SOXL", 40, 128.626, 124.28) - 23.38) < 0.01

# ---- order sheet on 2026-09-25 (qty 12, avg 148.07, T = 5): user placed 2 @ 148.07, 2 @ 170.27 (LOC buys), 3 @ 170.28 LOC sell, 9 @ 177.68 limit sell
o = E.plan_orders(st(qty=12, avg=148.07, T=5.0, cash=27400 - 12 * 148.07), 0)
d = {x["tag"]: x for x in o}
assert (d["avg_buy"]["price"], d["avg_buy"]["qty"], d["avg_buy"]["type"]) == (148.07, 2, "LOC")
assert (d["star_buy"]["price"], d["star_buy"]["qty"]) == (170.27, 2)
assert (d["quarter_sell"]["price"], d["quarter_sell"]["qty"], d["quarter_sell"]["type"]) == (170.28, 3, "LOC")
assert (d["target_sell"]["price"], d["target_sell"]["qty"], d["target_sell"]["type"]) == (177.68, 9, "LIMIT")

# ---- 2026-10-08 (qty 27, avg 150.959, T 9.37): avg order 3, star order 2 @ 166.99, quarter 6, target 21 @ 181.15
o = E.plan_orders(st(qty=27, avg=150.959, T=9.375, cash=27400 - 27 * 150.959 + 96), 0)
d = {x["tag"]: x for x in o}
assert d["avg_buy"]["qty"] == 3 and d["star_buy"]["qty"] == 2 and d["star_buy"]["price"] == 166.99
assert d["quarter_sell"]["qty"] == 6 and d["target_sell"]["qty"] == 21 and d["target_sell"]["price"] == 181.15

# ---- second half: one star-price buy of a full unit; no avg buy
o = E.plan_orders(st(qty=60, avg=136.701, T=21.375, cash=19300.0), 0)
assert [x["tag"] for x in o if x["side"] == "buy"] == ["star_buy"] and [x for x in o if x["tag"] == "quarter_sell"][0]["price"] == 134.82

# ---- first buy of a cycle
o = E.plan_orders(st(cash=27400.0), 150.0)
assert len(o) == 1 and o[0]["tag"] == "first_buy" and o[0]["price"] == 168.0 and o[0]["qty"] == int(27400 / 40 // 150)

# ---- cash guard: nothing is planned beyond the cash
o = E.plan_orders(st(qty=12, avg=148.07, T=5.0, cash=100.0), 0)
assert [x for x in o if x["side"] == "buy"] == []

# ---- V2.2: fixed unit = principal / N, half buys at avg and at star, sells 1/4 at star + rest at +10%
v = dict(version="V2.2", ticker="TQQQ", splits=40, principal=40000.0, cash=36000.0, qty=100, avg=80.0, T=4.0, mode="normal", compounding=True, extras={})
o = {x["tag"]: x for x in E.plan_orders(v, 0)}
assert o["avg_buy"]["qty"] == 6 and o["avg_buy"]["price"] == 80.0            # unit 1000, half 500 / 80 = 6.25 -> round 6
assert abs(o["star_buy"]["price"] - round(80 * (1 + 8 / 100.0) - 0.01, 2)) < 1e-9 and o["quarter_sell"]["qty"] == 25 and o["target_sell"]["price"] == 88.0

# ---- fills: T updates (V4.0)
s0 = st(qty=27, avg=147.667, T=10.5, cash=20000.0)
s1 = E.apply_fills(s0, [dict(side="sell", qty=6, price=163.71, tag="quarter_sell")])
assert abs(s1["T"] - 7.875) < 1e-9 and s1["qty"] == 21 and abs(s1["cash"] - (20000 + 6 * 163.71)) < 1e-6 and abs(s1["avg"] - 147.667) < 1e-9
s2 = E.apply_fills(st(qty=21, avg=147.667, T=7.875, cash=20000.0), [dict(side="buy", qty=2, price=164.27, tag="star_buy")])
assert abs(s2["T"] - 8.375) < 1e-9, "one filled order = +0.5 (app mode)"
both = [dict(side="buy", qty=3, price=142.52, tag="avg_buy"), dict(side="buy", qty=2, price=142.52, tag="star_buy")]
assert abs(E.apply_fills(st(qty=27, avg=150.96, T=9.37, cash=20000.0, extras={"t_mode": "doc"}), both)["T"] - 10.37) < 1e-9, "doc mode: two halves = +1"
obs = st(qty=27, avg=150.96, T=9.37, cash=20000.0, extras={"t_mode": "app"})
assert abs(E.apply_fills(obs, both)["T"] - 11.37) < 1e-9, "app mode: both rows +1 each (owner app list)"
f = E.apply_fills(st(cash=27400.0), [dict(side="buy", qty=4, price=151.95, tag="first_buy")])
assert f["T"] == 1.0 and f["qty"] == 4 and abs(f["avg"] - 151.95) < 1e-9 and abs(f["cash"] - (27400 - 607.8)) < 1e-6

# ---- app mode, second half: a row is +1 when its amount is >= 0.7 of the unit, else +0.5
sh = st(qty=60, avg=136.7, T=21.375, cash=19300.0)               # unit 19300 / 18.625 = 1036.2
assert abs(E.apply_fills(sh, [dict(side="buy", qty=7, price=93.67, tag="ladder")])["T"] - 21.875) < 1e-9        # 655.7 / 1036 = 0.63 -> +0.5 (owner list: 21.375 -> 21.875)
assert abs(E.apply_fills(sh, [dict(side="buy", qty=8, price=98.65, tag="ladder")])["T"] - 22.375) < 1e-9        # 789 / 1036 = 0.76 -> +1
# ---- replay of the owner's first cycle in app mode: T after 2026-10-15 fills = 21.375 (from the outstanding LOC price), after the whole list 24.375 per his app screenshot
# ---- cycle end: target sell empties the position, T resets, compounding re-bases the principal
end = E.apply_fills(st(qty=10, avg=100.0, T=12.0, cash=26000.0, principal=27400.0), [dict(side="sell", qty=10, price=120.0, tag="target_sell")])
assert end["qty"] == 0 and end["T"] == 0.0 and abs(end["cash"] - 27200.0) < 1e-6 and abs(end["principal"] - 27200.0) < 1e-6
simple = E.apply_fills(st(qty=10, avg=100.0, T=12.0, cash=26000.0, principal=27400.0, compounding=False), [dict(side="sell", qty=10, price=120.0, tag="target_sell")])
assert simple["principal"] == 27400.0

# ---- V2.2 T from invested cost
v2 = E.apply_fills(dict(v, qty=0, avg=0.0, cash=40000.0, T=0.0), [dict(side="buy", qty=25, price=80.0, tag="avg_buy")])
assert abs(v2["T"] - 2.0) < 1e-9, "25 x 80 = 2000 / 1000 unit"

# ---- reverse mode day 1: sell 1/20 at the close (40 splits), no buy
r = E.plan_orders(st(qty=200, avg=100.0, T=39.5, cash=100.0, mode="reverse", extras={"reverse_day": 0}), 90.0)
assert r == [dict(side="sell", type="MOC", qty=10, price=0.0, tag="reverse_first_sell")]
print("test_infinite_engine: all checks passed (self-verification only)")
