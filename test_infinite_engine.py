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
d = {x["tag"]: x for x in o if x["tag"] != "ladder_x"}
assert (d["avg_buy"]["price"], d["avg_buy"]["qty"], d["avg_buy"]["type"]) == (148.07, 2, "LOC")
assert (d["star_buy"]["price"], d["star_buy"]["qty"]) == (170.27, 2)
assert (d["quarter_sell"]["price"], d["quarter_sell"]["qty"], d["quarter_sell"]["type"]) == (170.28, 3, "LOC")
assert (d["target_sell"]["price"], d["target_sell"]["qty"], d["target_sell"]["type"]) == (177.68, 9, "LIMIT")

# ---- 2026-10-08 (qty 27, avg 150.959, T 9.37): avg order 3, star order 2 @ 166.99, quarter 6, target 21 @ 181.15
o = E.plan_orders(st(qty=27, avg=150.959, T=9.375, cash=27400 - 27 * 150.959 + 96), 0)
d = {x["tag"]: x for x in o if x["tag"] != "ladder_x"}
assert d["avg_buy"]["qty"] == 3 and d["star_buy"]["qty"] == 2 and d["star_buy"]["price"] == 166.99
assert d["quarter_sell"]["qty"] == 6 and d["target_sell"]["qty"] == 21 and d["target_sell"]["price"] == 181.15

# ---- second half: one star-price buy of a full unit; no avg buy
o = E.plan_orders(st(qty=60, avg=136.701, T=21.375, cash=19300.0), 0)
assert [x["tag"] for x in o if x["side"] == "buy" and x["tag"] != "ladder_x"] == ["star_buy"] and [x for x in o if x["tag"] == "quarter_sell"][0]["price"] == 134.82

# ---- first buy of a cycle
o = E.plan_orders(st(cash=27400.0), 150.0)
assert len(o) == 1 and o[0]["tag"] == "first_buy" and o[0]["price"] == 168.0 and o[0]["qty"] == int(27400 / 40 // 150)

# ---- cash guard: nothing is planned beyond the cash
o = E.plan_orders(st(qty=12, avg=148.07, T=5.0, cash=100.0), 0)
assert [x for x in o if x["side"] == "buy"] == [], "no cash: no buy and no ladder"

# ---- V2.2: fixed unit = principal / N, half buys at avg and at star, sells 1/4 at star + rest at +10%
v = dict(version="V2.2", ticker="TQQQ", splits=40, principal=40000.0, cash=36000.0, qty=100, avg=80.0, T=4.0, mode="normal", compounding=True, extras={})
o = {x["tag"]: x for x in E.plan_orders(v, 0) if x["tag"] != "ladder_x"}
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
sh = st(qty=60, avg=136.7, T=21.375, cash=19300.0, extras={"t_mode": "app"})               # unit 19300 / 18.625 = 1036.2
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
# ---- ladder ("폭락장 대비 추가 매수") as in the owner's app: unit 1105.21, 9 shares at 120.12 -> 6 LOC buys of 1 share at 110.52, 100.47, 92.10, 85.02, 78.94, 73.68
app = st(qty=83, avg=125.6306, T=24.375, cash=17268.93, principal=27600.0)
assert not [x for x in E.plan_orders(app, 97.55) if x["tag"] == "ladder_x"], "ladder is off unless extras.ladder"
app = dict(app, extras={"ladder": True})
lad = [(x["price"], x["qty"]) for x in E.plan_orders(app, 97.55) if x["tag"] == "ladder_x"]
assert lad == [(110.52, 1), (100.47, 1), (92.1, 1), (85.02, 1), (78.94, 1), (73.68, 1)], lad
assert not [x for x in E.plan_orders(dict(app, extras={"ladder_n": 0}), 97.55) if x["tag"] == "ladder_x"], "ladder can be switched off per cycle"
# ladder fills do not count as extra T rows: 9 + 1 + 1 shares in the second half
base = st(qty=83, avg=125.6306, T=24.375, cash=17268.93, principal=27600.0, extras={"t_mode": "app"})
f1 = E.apply_fills(base, [dict(side="buy", qty=9, price=97.0, tag="star_buy"), dict(side="buy", qty=1, price=97.0, tag="ladder_x"), dict(side="buy", qty=1, price=97.0, tag="ladder_x")])
assert abs(f1["T"] - 25.375) < 1e-9 and f1["qty"] == 94, f1["T"]            # one row of (9+2) x 97 = 1067 >= 0.7 x unit -> +1
# ---- owner mode (default, 2026-10-24): T moves once per DAY = clamp(round0.5(day buy amount / unit), 0, 1); ladder shares add nothing on their own
assert E.T_MODE_DEFAULT == "owner"
assert E._owner_day_inc(0.0, 600) == 0.0 and E._owner_day_inc(300, 600) == 0.5 and E._owner_day_inc(0.74 * 600, 600) == 0.5 and E._owner_day_inc(0.75 * 600, 600) == 1.0 and E._owner_day_inc(1500, 600) == 1.0 and E._owner_day_inc(100, 600) == 0.0
fh = st(qty=27, avg=150.96, T=9.37, cash=20000.0)                                  # unit 20000 / 30.63 = 653
assert abs(E.apply_fills(fh, both)["T"] - 10.37) < 1e-9, "first half: avg 3 + star 2 (1.09 unit) = +1"
assert abs(E.apply_fills(fh, [both[0]])["T"] - 9.87) < 1e-9, "first half: one half-size order = +0.5"
assert abs(E.apply_fills(fh, both + [dict(side="buy", qty=2, price=130.0, tag="ladder_x")])["T"] - 10.37) < 1e-9, "ladder shares cannot lift the day above +1"
assert abs(E.apply_fills(fh, [dict(side="buy", qty=1, price=130.0, tag="ladder_x")])["T"] - 9.37) < 1e-9, "ladder-only day adds nothing"
one_full = [dict(side="buy", qty=7, price=93.67, tag="star_buy")]                  # a single full-size order in the first half (656 = 1.0 unit) is +1, as in the owner's app
assert abs(E.apply_fills(st(qty=60, avg=130.0, T=12.375, cash=19000.0), one_full)["T"] - 13.375) < 1e-9
sh2 = st(qty=101, avg=121.0, T=16.375, cash=14514.5, principal=27600.0)           # unit 614
day = [dict(side="buy", qty=9, price=83.56, tag="star_buy")] + [dict(side="buy", qty=1, price=83.56, tag="ladder_x")] * 4
assert abs(E.apply_fills(sh2, day)["T"] - 17.375) < 1e-9, "9 + 4 ladder shares in one day = +1"
assert abs(E.apply_fills(sh2, [dict(side="buy", qty=9, price=82.11, tag="star_buy")])["T"] - 17.375) < 1e-9
# a day whose fills reach us in several syncs counts once: the unit of the first sync is kept, T never exceeds +1 per day
a1 = E.apply_fills(sh2, [dict(side="buy", qty=5, price=83.0, tag="star_buy")], day="2026-10-24")          # 415 / 614 = 0.68 -> +0.5
assert abs(a1["T"] - 16.875) < 1e-9, a1["T"]
a2 = E.apply_fills(a1, [dict(side="buy", qty=4, price=83.0, tag="star_buy"), dict(side="buy", qty=1, price=83.0, tag="ladder_x")], day="2026-10-24")   # cumulative 830 / 614 = 1.35 -> +1 in total
assert abs(a2["T"] - 17.375) < 1e-9, a2["T"]
a3 = E.apply_fills(a2, [dict(side="buy", qty=8, price=80.0, tag="star_buy")], day="2026-10-25")
assert abs(a3["T"] - 18.375) < 1e-9, "the next day counts again"
# first buy split over two calls of one day counts once; a sell-only call does not change the unit of the day; cycle end clears the day memory
fb1 = E.apply_fills(st(cash=27600.0), [dict(side="buy", qty=2, price=151.95, tag="first_buy")], day="2026-09-23")
fb2 = E.apply_fills(fb1, [dict(side="buy", qty=2, price=151.95, tag="first_buy")], day="2026-09-23")
assert abs(fb2["T"] - 1.0) < 1e-9, fb2["T"]
sq = st(qty=60, avg=130.0, T=12.0, cash=19000.0, principal=27600.0)          # unit 19000 / 28 = 678.6
s_first = E.apply_fills(sq, [dict(side="sell", qty=15, price=140.0, tag="quarter_sell")], day="2026-10-06")   # T 12 -> 9, cash 21100: the unit would now be 680.6 -> 21100/31 only if recomputed
s_then = E.apply_fills(s_first, [dict(side="buy", qty=5, price=135.0, tag="star_buy")], day="2026-10-06")
assert s_then["extras"]["t_day"]["2026-10-06"]["unit"] == round(19000.0 / 28.0, 4), s_then["extras"]["t_day"]
done = E.apply_fills(st(qty=10, avg=100.0, T=12.0, cash=26000.0, principal=27400.0), [dict(side="sell", qty=10, price=120.0, tag="target_sell")], day="2026-10-07")
assert "t_day" not in done["extras"] and done["T"] == 0.0
# replay of the owner's T steps entered in his app for 2026-10-21..24 (T 14.375 after 10-20): 15.375, 16.375, 17.375, 18.375
r = st(qty=83 + 8, avg=121.0, T=14.375, cash=16000.0, principal=27600.0)
for d_, rows in [("2026-10-21", [(8, 97.55, "star_buy")]), ("2026-10-22", [(9, 103.24, "star_buy")]), ("2026-10-23", [(9, 82.11, "star_buy")]), ("2026-10-24", [(9, 83.56, "star_buy")] + [(1, 83.56, "ladder_x")] * 4)]:
    r = E.apply_fills(r, [dict(side="buy", qty=q_, price=p_, tag=t_) for q_, p_, t_ in rows], day=d_)
assert abs(r["T"] - 18.375) < 1e-9, r["T"]

# ---- official V4.0 table (Laor cafe post) first half: star LOC 78.11 x3 + average LOC 69.75 x4, ladder 67.40, 59.91 = B / (7 + k) with B = 539.2
fh_lad = E._ladder(539.2, 7, 2)
assert [x["price"] for x in fh_lad] == [67.4, 59.91], fh_lad
# second half official example: star 59.54 x9, ladder 56.85, 51.68, 47.37 = 568.45 / (9 + k) (B is unknown to the cent)
assert [x["price"] for x in E._ladder(568.45, 9, 3)] == [56.85, 51.68, 47.37]
pl = E.plan_orders(dict(st(qty=60, avg=100.0, T=10.0, cash=19000.0, principal=27600.0), extras={"ladder": True}), 98.0)
main_q = sum(o["qty"] for o in pl if o["tag"] in ("avg_buy", "star_buy"))
assert [o["price"] for o in pl if o["tag"] == "ladder_x"][0] == E.r2((19000.0 / 30.0) / (main_q + 1)), "first-half ladder is built from the full unit and all main shares"
# ---- official V4.0: limit sell + LOC buys of the same order day: no cycle end, T = T x 0.25 + step of the buys
lim = st(qty=114, avg=115.63, T=30.0, cash=14514.5, principal=27600.0)
sell = dict(side="sell", qty=114, price=138.76, tag="target_sell")
buys = [dict(side="buy", qty=3, price=100.0, tag="avg_buy"), dict(side="buy", qty=2, price=100.0, tag="star_buy")]       # unit of the order day = 14514.5 / (40 - 30) = 1451.45 (before the sale); 500 / 1451 = 0.34 -> +0.5
res = E.apply_fills(lim, [sell] + buys, day="2026-11-02")
assert res["qty"] == 5 and abs(res["avg"] - 100.0) < 1e-9, res
assert abs(res["T"] - 8.0) < 1e-9, res["T"]                                       # 30 x 0.25 = 7.5, + 0.5 for the half-size buys
assert abs(res["principal"] - 27600.0) < 1e-9 and res.get("mode", "normal") == "normal"
big = [dict(side="buy", qty=7, price=100.0, tag="avg_buy"), dict(side="buy", qty=7, price=100.0, tag="star_buy")]
lim2 = st(qty=114, avg=115.63, T=30.0, cash=14000.0, principal=27600.0)                                              # unit 1400 -> 1400 / 1400 = 1.0 -> +1 (two half-size buys): T = 7.5 + 1
assert abs(E.apply_fills(lim2, [sell] + big, day="2026-11-02")["T"] - 8.5) < 1e-9
alone = E.apply_fills(lim, [sell], day="2026-11-02")
assert alone["qty"] == 0 and alone["T"] == 0.0, "a limit sell with no LOC buys is still the end of the cycle"
# ---- exhaust mode: T above splits - 1 switches the cycle to reverse mode (official); T = 39 itself stays normal
ex0 = st(qty=80, avg=100.0, T=38.0, cash=600.0, principal=27600.0)                                                   # unit 600 / 2 = 300
r39 = E.apply_fills(ex0, [dict(side="buy", qty=3, price=100.0, tag="star_buy")], day="2026-11-03")                  # 300 / 300 = 1 -> T 39.0
assert abs(r39["T"] - 39.0) < 1e-9 and r39.get("mode", "normal") == "normal"
ex1 = st(qty=80, avg=100.0, T=38.5, cash=450.0, principal=27600.0)                                                   # unit 450 / 1.5 = 300
r395 = E.apply_fills(ex1, [dict(side="buy", qty=3, price=100.0, tag="star_buy")], day="2026-11-03")                 # +1 -> 39.5 > 39
assert r395["mode"] == "reverse" and r395["extras"]["reverse_day"] == 0, r395
rv = E.apply_fills(dict(r395, qty=83), [dict(side="sell", qty=4, price=90.0, tag="reverse_first_sell")], day="2026-11-04")
assert rv["extras"]["reverse_day"] == 1 and rv["mode"] == "reverse"
assert E.reverse_exit_due(dict(rv, avg=100.0), 81.0) and not E.reverse_exit_due(dict(rv, avg=100.0), 79.0) and not E.reverse_exit_due(st(qty=5, avg=100.0), 120.0)
# a partly filled limit sell followed by the quarter LOC that empties the position is also "limit sell + LOC buys": T x 0.25, no cycle end when buys follow in the same order day
q_empty = E.apply_fills(st(qty=30, avg=100.0, T=20.0, cash=5000.0, principal=27600.0), [dict(side="sell", qty=30, price=120.0, tag="quarter_sell"), dict(side="buy", qty=5, price=90.0, tag="star_buy")], day="2026-11-05")
assert q_empty["qty"] == 5 and abs(q_empty["T"] - (5.0 + E._owner_day_inc(450.0, 5000.0 / 20.0))) < 1e-9, q_empty["T"]
# cycle end resets the mode and the reverse memory
rv_end = E.apply_fills(dict(rv, qty=4, mode="reverse", extras=dict(rv["extras"], reverse_day=3)), [dict(side="sell", qty=4, price=95.0, tag="reverse_sell")], day="2026-11-06")
assert rv_end["qty"] == 0 and rv_end["mode"] == "normal" and "reverse_day" not in rv_end["extras"]
print("test_infinite_engine: all checks passed (self-verification only)")
