"""infinite_engine.py - pure rule engine for Laor's infinite buying method (V2.2 and V4.0) on leveraged ETFs (TQQQ, SOXL). No I/O, no broker calls.

Rules implemented from the public write-ups (quantstack.app V4.0 normal / reverse, pbdfinance V2.2 / V3.0); the original cafe posts are members-only and NOT verified.
Every place where the write-ups are silent or where the user's own account history disagrees is a named parameter below (T_RULE, FIRST_BUY_PREMIUM, ...) so it can be calibrated without touching the logic.

Account state (dict, JSON friendly): version "V2.2" | "V4.0", ticker, splits N, principal (USD, the capital of this cycle), cash (USD left, "잔금"), qty (int shares), avg (average price, USD), T (float), mode "normal" | "reverse",
compounding (bool), extras (dict: reverse-mode memory, realised profit of the cycle, ...).
Orders returned by plan_orders(): dicts {side "buy"|"sell", type "LOC"|"LIMIT"|"MOC", qty, price, tag}. Prices are rounded to cents; LOC/LIMIT buy prices already include the -0.01 star offset.
"""
import math

STAR_BASE = {"V4.0": {"TQQQ": 15.0, "SOXL": 20.0}, "V2.2": {"TQQQ": 10.0, "SOXL": 10.0}}      # star percent at T = 0 (V2.2 for SOXL is NOT documented in the write-ups: default 10, editable)
TARGET_PCT = {"V4.0": {"TQQQ": 15.0, "SOXL": 20.0}, "V2.2": {"TQQQ": 10.0, "SOXL": 10.0}}    # full (limit) sell target above the average price, percent
REVERSE_END_PCT = {"TQQQ": -15.0, "SOXL": -20.0}                                           # V4.0 reverse mode ends when the close is above avg * (1 + pct)
FIRST_BUY_PREMIUM = 0.12            # first buy of a cycle: LOC limit = previous close * (1 + premium) so that it fills at the close (write-up: 10-15 percent above)
T_RULE = {"half": 0.5, "full": 1.0, "quarter_sell_mult": 0.75}                              # V4.0 normal mode (write-up)
# T update of the buy fills of one day, V4.0. mode "app" (default) = the owner's app, read from its fill list (screenshots 2026-10-21): every filled buy order is one row. First half (T < N/2): when two or more buy orders
# filled that day each row is +1, when exactly one filled that row is +0.5; the cycle's first buy +1; a quarter sell multiplies T by 0.75. Second half: a row is +1 when its amount is at least APP_FULL_RATIO of the one-unit
# amount, else +0.5 (calibrated on three observations only: 0.63 -> +0.5, 0.71 -> +1, 0.74 -> +1.5 unexplained). mode "doc" = the write-up (each half-size order +0.5, a full unit +1).
APP_FULL_RATIO = 0.7
T_MODE_DEFAULT = "app"


def r2(x):
    return round(float(x) + 1e-9, 2)


def floor_qty(amount, price):
    return int(math.floor(amount / price + 1e-9)) if price and price > 0 else 0


def round_qty(amount, price):
    """Round half up (the user's own sheet rounds the average-price order this way; calibrated on 30 of the user's orders, see calibrate notes in the module docstring)."""
    return int(math.floor(amount / price + 0.5)) if price and price > 0 else 0


QTY_FN = {"avg_buy": round_qty, "star_buy": floor_qty, "first_buy": floor_qty}      # quantity of one buy order from its amount; the user's account history matched 26 of 30 orders with round for avg and floor for star


def star_pct(version, ticker, splits, T):
    """Star percent: base * (1 - 2T/N). V4.0 SOXL 40 splits -> 20 - T, TQQQ 40 -> 15 - 0.75T, TQQQ 20 -> 15 - 1.5T; V2.2 40 splits -> 10 - T/2 (general 10 - (T/2)*(40/N))."""
    return STAR_BASE[version][ticker] * (1.0 - 2.0 * T / splits)


def unit_amount(st):
    """One buy unit (1회 매수금) in USD. V4.0: cash / (N - T), changes daily. V2.2: principal / N (fixed within a cycle; with compounding the principal already contains earlier profit)."""
    N, T = st["splits"], st["T"]
    if st["version"] == "V4.0":
        return st["cash"] / max(N - T, 1e-9)
    return st["principal"] / N


def is_first_half(st):
    return st["T"] < st["splits"] / 2.0


def plan_orders(st, prev_close, closes5=None):
    """Orders to place before the next US regular session. prev_close = last close (USD). closes5 = last five closes (reverse mode star price)."""
    if st.get("mode") == "reverse":
        return _plan_reverse(st, prev_close, closes5)
    ver, tk, N = st["version"], st["ticker"], st["splits"]
    out = []
    B = unit_amount(st)
    if st["qty"] <= 0:                                                  # new cycle: one LOC buy of a full unit, limit well above the last close
        px = r2(prev_close * (1 + FIRST_BUY_PREMIUM))
        q = floor_qty(B, prev_close)
        if q > 0:
            out.append(dict(side="buy", type="LOC", qty=q, price=px, tag="first_buy"))
        return out
    avg = st["avg"]
    sp = star_pct(ver, tk, N, st["T"])
    star = r2(avg * (1 + sp / 100.0))
    buy_star = r2(star - 0.01)
    if is_first_half(st):
        h = B / 2.0
        q_avg, q_star = QTY_FN["avg_buy"](h, avg), QTY_FN["star_buy"](h, buy_star)
        if q_avg > 0:
            out.append(dict(side="buy", type="LOC", qty=q_avg, price=r2(avg), tag="avg_buy"))
        if q_star > 0:
            out.append(dict(side="buy", type="LOC", qty=q_star, price=buy_star, tag="star_buy"))
    else:
        q = floor_qty(B, buy_star)
        if q > 0:
            out.append(dict(side="buy", type="LOC", qty=q, price=buy_star, tag="star_buy"))
    # cash guard: never plan more than the cash left
    spent = 0.0
    kept = []
    for o in out:
        cost = o["qty"] * o["price"]
        if spent + cost <= st["cash"] + 1e-6:
            kept.append(o)
            spent += cost
    out = kept
    # sells: quarter at the star price (LOC), the rest as a limit order at avg * (1 + target)
    qq = st["qty"] // 4
    if qq > 0:
        out.append(dict(side="sell", type="LOC", qty=qq, price=star, tag="quarter_sell"))
    rest = st["qty"] - qq
    if rest > 0:
        out.append(dict(side="sell", type="LIMIT", qty=rest, price=r2(avg * (1 + TARGET_PCT[ver][tk] / 100.0)), tag="target_sell"))
    return out


def _plan_reverse(st, prev_close, closes5):
    """V4.0 reverse mode (write-up): day 1 sells 1/20 (40 splits) or 1/10 (20 splits) of the shares at the close (MOC), no buy; afterwards sells the same fraction of the current shares if the close is above the
    5-day-average star price and buys with cash/4 (LOC) below the star price."""
    N = st["splits"]
    frac = 20 if N == 40 else 10
    ex = st.get("extras", {})
    out = []
    q_sell = st["qty"] // frac
    if ex.get("reverse_day", 0) == 0:
        if q_sell > 0:
            out.append(dict(side="sell", type="MOC", qty=q_sell, price=0.0, tag="reverse_first_sell"))
        return out
    star = r2(sum(closes5) / len(closes5)) if closes5 else r2(prev_close)
    if q_sell > 0:
        out.append(dict(side="sell", type="LOC", qty=q_sell, price=star, tag="reverse_sell"))
    amount = st["cash"] / 4.0
    q = floor_qty(amount, star - 0.01)
    if q > 0:
        out.append(dict(side="buy", type="LOC", qty=q, price=r2(star - 0.01), tag="reverse_buy"))
    return out


def apply_fills(st, fills, close=None):
    """Update the state with the day's fills [{side, qty, price, tag}] (tag as in plan_orders; '' when unknown) and return the new state (the input is not modified).
    V4.0 T updates (write-up): full unit bought +1, half +0.5, quarter sell T*0.75, limit (target) sell that empties the position ends the cycle; V2.2: T = invested cost / unit (recomputed)."""
    s = dict(st)
    s["extras"] = dict(st.get("extras", {}))
    ver = s["version"]
    bought_amt = 0.0
    fill_tags = []
    manual_t = 0.0
    for f in fills:
        q, p = int(f["qty"]), float(f["price"])
        if q <= 0:
            continue
        if f["side"] == "buy":
            cost = s["avg"] * s["qty"] + q * p
            s["qty"] += q
            s["avg"] = cost / s["qty"]
            s["cash"] -= q * p
            bought_amt += q * p
            tag = f.get("tag", "")
            if ver == "V4.0":
                if f.get("t_step") is not None:                  # manual override of this fill's T step (e.g. +0.5 when only one of the two orders filled)
                    manual_t += float(f["t_step"])
                else:
                    fill_tags.append((tag, q * p))
        else:
            sold_cost = s["avg"] * q
            s["qty"] -= q
            s["cash"] += q * p
            s["extras"]["cycle_realized"] = s["extras"].get("cycle_realized", 0.0) + q * p - sold_cost
            if f.get("tag") == "quarter_sell" and ver == "V4.0":
                s["T"] = s["T"] * T_RULE["quarter_sell_mult"]
            if s["qty"] <= 0:
                s["qty"], s["avg"] = 0, 0.0
    if ver == "V4.0":
        if fill_tags:
            s["T"] = s["T"] + _t_increment(fill_tags, st, st.get("extras", {}).get("t_mode", T_MODE_DEFAULT))
        s["T"] = s["T"] + manual_t
    else:
        unit = s["principal"] / s["splits"]
        s["T"] = round((s["avg"] * s["qty"]) / unit, 2) if s["qty"] > 0 else 0.0
    if s["qty"] <= 0:                                                   # cycle finished: T resets; with compounding the new principal is the whole cash
        s["T"] = 0.0
        if s.get("compounding", True):
            s["principal"] = s["cash"]
    return s


def _t_increment(fills, st, mode):
    """T added by one day's V4.0 buy fills; fills = [(tag, amount_usd)]."""
    tags = [t for t, _ in fills]
    if "first_buy" in tags:
        return T_RULE["full"]
    first_half = is_first_half(st)
    if mode == "app":
        if first_half:
            return float(len(fills)) if len(fills) >= 2 else T_RULE["half"]
        unit = unit_amount(st)
        return sum(T_RULE["full"] if (unit > 0 and amt >= APP_FULL_RATIO * unit) else T_RULE["half"] for _, amt in fills)
    inc = 0.0                                                         # doc: every half-size order = 0.5 in the first half, a full-size order = 1 in the second half
    for t in tags:
        inc += T_RULE["half"] if (first_half or t not in ("star_buy", "avg_buy")) else T_RULE["full"]
    return inc


def implied_T(version, ticker, splits, avg, quarter_sell_price):
    """Back-solve T from the price of the quarter-sell LOC order the account has outstanding (it sits at the star point): star% = (price/avg - 1)*100, T = N/2 * (1 - star%/base)."""
    sp = (quarter_sell_price / avg - 1.0) * 100.0
    return splits / 2.0 * (1.0 - sp / STAR_BASE[version][ticker])
