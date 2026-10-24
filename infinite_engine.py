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
LADDER_N = 6                  # "폭락장 대비 추가 매수" (the owner's app guide): after each buy order of q shares for `amount`, LOC buys of 1 share at amount/(q+1), amount/(q+2) ... (6 of them); they only fill when the close falls that far
# mode "owner" (default since 2026-10-24, the owner's own rule, verified against the T steps he entered in his app for all 24 fill days of the SOXL cycle): T moves once per DAY by the size of the day's buys measured in units:
# inc = clamp(round-to-0.5(day buy amount / unit), 0, 1) with unit = cash/(N - T) at the START of the day. Two half-size orders (or one full order) = +1, one half-size order = +0.5, crash-prep ladder shares add nothing to T
# (their amount can never lift the day above +1). First buy of a cycle +1. Quarter sell x0.75 unchanged. His third-party app counted every fill row +1 (T 31.375 for what is 18.375 here).
T_MODE_DEFAULT = "owner"
OWNER_ROUND = 0.5                                                  # T granularity of the owner rule


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


def _ladder(amount, qty, n):
    """Extra 1-share LOC buys below the main buys: price = amount / (qty + k), k = 1..n, amount = the full unit and qty = all main shares (official V4.0 tables, first half 539.2/(3+4+k)) (owner's app: unit 1105.21, 9 shares -> 110.52, 100.47, 92.10, 85.02, 78.94, 73.68)."""
    out = []
    for k in range(1, n + 1):
        px = r2(amount / (qty + k))
        if px > 0:
            out.append(dict(side="buy", type="LOC", qty=1, price=px, tag="ladder_x"))
    return out


def plan_orders(st, prev_close, closes5=None):
    """Orders to place before the next US regular session. prev_close = last close (USD). closes5 = last five closes (reverse mode star price)."""
    if st.get("mode") == "reverse":
        return _plan_reverse(st, prev_close, closes5)
    ver, tk, N = st["version"], st["ticker"], st["splits"]
    out = []
    B = min(unit_amount(st), st["cash"])                                 # cash / (N - T) can exceed the cash itself when T is close to N (e.g. right after leaving reverse mode)
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
    ex = st.get("extras") or {}
    n_lad = int(ex.get("ladder_n", LADDER_N)) if ex.get("ladder") else 0                 # ladder only for cycles with the per-cycle ladder switch (extras.ladder) on
    if n_lad > 0:
        main_q = sum(o["qty"] for o in out if o["tag"] in ("avg_buy", "star_buy"))
        lad = _ladder(B, main_q, n_lad) if main_q > 0 else []                              # official V4.0 tables: ladder price = full unit B / (shares of the main orders + k), first half included (B=539.2: 7 shares -> 67.40, 59.91)
        spent = sum(o["qty"] * o["price"] for o in out)
        for o in lad:                                                  # the ladder shares are bought only on a lower close, but never plan more than the cash left
            if spent + o["price"] <= st["cash"] + 1e-6:
                out.append(o)
                spent += o["price"]
    # sells: quarter at the star price (LOC), the rest as a limit order at avg * (1 + target)
    qq = st["qty"] // 4
    if qq > 0:
        out.append(dict(side="sell", type="LOC", qty=qq, price=star, tag="quarter_sell"))
    rest = st["qty"] - qq
    if rest > 0:
        out.append(dict(side="sell", type="LIMIT", qty=rest, price=r2(avg * (1 + TARGET_PCT[ver][tk] / 100.0)), tag="target_sell"))
    return out


def _plan_reverse(st, prev_close, closes5):
    """V4.0 reverse mode (Laor post "2. reverse mode after exhaustion"): day 1 sells 1/20 (40 splits) or 1/10 (20 splits) of the shares (rounded down) at the close (MOC), no buy; afterwards, each day, the same fraction
    of the CURRENT shares as an LOC sell at the star point (the average of the last five closes) and a quarter buy (cash / 4, LOC at star - 0.01). When cash / 4 cannot buy even one share the mid-reverse exhaustion rule
    applies: that day is a MOC sell of the same size and no buy (the post: "if 1 share cannot be bought with the quarter, do the first-day MOC sell")."""
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
    amount = st["cash"] / 4.0
    q = floor_qty(amount, star - 0.01)
    if q <= 0:                                                          # exhausted inside reverse mode: MOC sell like on the first day, no buy
        if q_sell > 0:
            out.append(dict(side="sell", type="MOC", qty=q_sell, price=0.0, tag="reverse_first_sell"))
        return out
    if q_sell > 0:
        out.append(dict(side="sell", type="LOC", qty=q_sell, price=star, tag="reverse_sell"))
    if q > 0:
        out.append(dict(side="buy", type="LOC", qty=q, price=r2(star - 0.01), tag="reverse_buy"))
    return out


def apply_fills(st, fills, close=None, day=None):
    """Update the state with the day's fills [{side, qty, price, tag}] (tag as in plan_orders; '' when unknown) and return the new state (the input is not modified).
    V4.0 T updates (write-up): full unit bought +1, half +0.5, quarter sell T*0.75, limit (target) sell that empties the position ends the cycle; V2.2: T = invested cost / unit (recomputed)."""
    s = dict(st)
    s["extras"] = dict(st.get("extras", {}))
    ver = s["version"]
    rev_before = ver == "V4.0" and st.get("mode") == "reverse"           # fills of a reverse-mode day: T by the reverse rule, not by the normal rules
    limit_reset = False                                                  # a limit (target) sell emptied the position during this call
    owner_day = ver == "V4.0" and day and st.get("mode") != "reverse" and st.get("extras", {}).get("t_mode", T_MODE_DEFAULT) == "owner"
    if owner_day and day not in (s["extras"].get("t_day") or {}):          # the day's unit is fixed by the FIRST call that touches the day (also a sell-only call changes cash and T)
        s["extras"]["t_day"] = dict(s["extras"].get("t_day") or {}, **{day: {"unit": round(unit_amount(st), 4), "amt": 0.0, "inc": 0.0}})
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
                if ver == "V4.0" and not rev_before:                     # any sale that empties the position (limit sell, or the quarter LOC after a partly filled limit sell)
                    limit_reset = True
                    s["T"] = st["T"] * 0.25                              # official V4.0: a limit sell followed by LOC buys is not an end of the cycle, T = T x 0.25 + the step of the buys (+1 or +0.5)
    if ver == "V4.0":
        mode = st.get("extras", {}).get("t_mode", T_MODE_DEFAULT)
        if rev_before:                                                  # post: sell T x (1 - 2/N) (0.95 for 40 splits, 0.9 for 20), buy T + (N - T) x 0.25; a day has one sell order and one buy order
            Nn = s["splits"]
            done_r = dict(s["extras"].get("rev_done") or {})
            did = set(done_r.get(day, [])) if day else set()                  # each factor once per day, even when the fills arrive in several syncs
            if "sell" not in did and any(f["side"] == "sell" and int(f["qty"]) > 0 for f in fills):
                s["T"] = s["T"] * (1.0 - 2.0 / Nn)
                did.add("sell")
            if "buy" not in did and any(f["side"] == "buy" and int(f["qty"]) > 0 for f in fills):
                s["T"] = s["T"] + (Nn - s["T"]) * 0.25
                did.add("buy")
            if day:
                done_r[day] = sorted(did)
                s["extras"]["rev_done"] = {k: done_r[k] for k in sorted(done_r)[-5:]}
        elif mode == "owner":                                            # day-level rule: see the module header
            if fill_tags:
                days = dict(s["extras"].get("t_day") or {})
                prev = days.get(day) if day else None
                unit = float(prev["unit"]) if prev else unit_amount(st)       # the unit of the day's FIRST call (T and cash move during the day)
                amt = (float(prev["amt"]) if prev else 0.0) + sum(a_ for _, a_ in fill_tags)
                target = 1.0 if any(t_ == "first_buy" for t_, _ in fill_tags) else _owner_day_inc(amt, unit)
                done = float(prev["inc"]) if prev else 0.0
                s["T"] = s["T"] + (target - done)
                if day:
                    days[day] = {"unit": round(unit, 4), "amt": round(amt, 2), "inc": target}
                    s["extras"]["t_day"] = {k: days[k] for k in sorted(days)[-5:]}
        elif fill_tags:
            s["T"] = s["T"] + _t_increment(fill_tags, st, mode)
        s["T"] = s["T"] + manual_t
    else:
        unit = s["principal"] / s["splits"]
        s["T"] = round((s["avg"] * s["qty"]) / unit, 2) if s["qty"] > 0 else 0.0
    if ver == "V4.0" and s["qty"] > 0:                                  # exhaust mode (official): starts when T exceeds splits - 1, i.e. less than one unit is left
        if s.get("mode", "normal") == "normal" and s["T"] > s["splits"] - 1:
            s["mode"] = "reverse"
            s["extras"]["reverse_day"] = 0
            s["extras"]["reverse_since"] = day or ""
        elif s.get("mode") == "reverse" and any(f.get("tag") == "reverse_first_sell" and f["side"] == "sell" for f in fills):
            s["extras"]["reverse_day"] = max(1, int(s["extras"].get("reverse_day", 0)))
    if s["qty"] <= 0:                                                   # cycle finished: T resets; with compounding the new principal is the whole cash
        s["T"] = 0.0
        s["mode"] = "normal"
        for k_ in ("t_day", "rev_done", "reverse_day", "reverse_since"):
            s["extras"].pop(k_, None)
        if s.get("compounding", True):
            s["principal"] = s["cash"]
    return s


def _owner_day_inc(amount, unit):
    """Owner rule: the day's buy amount in units, rounded to the nearest 0.5 and capped at 1 (two halves or one full order = +1, one half = +0.5, ladder alone = 0)."""
    if unit <= 0:
        return 0.0
    return min(1.0, max(0.0, math.floor(amount / unit / OWNER_ROUND + 0.5) * OWNER_ROUND))


def _t_increment(fills, st, mode):
    """T added by one day's V4.0 buy fills; fills = [(tag, amount_usd)]."""
    extra = sum(a_ for t_, a_ in fills if t_ == "ladder_x")                # extra shares of the owner's ladder complete the main order: not a row of their own
    fills = [(t_, a_) for t_, a_ in fills if t_ != "ladder_x"]
    if extra and fills:
        fills[-1] = (fills[-1][0], fills[-1][1] + extra)
    elif extra:
        fills = [("star_buy", extra)]
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


def reverse_exit_due(st, close):
    """Post: reverse mode ends once a close is strictly above the average price x (1 + REVERSE_END_PCT) (-15 % TQQQ, -20 % SOXL; e.g. average 40, SOXL close above 32); the first reverse day (the MOC sell) always happens."""
    return st.get("mode") == "reverse" and int(st.get("extras", {}).get("reverse_day", 0)) >= 1 and st.get("qty", 0) > 0 and close is not None and close > st["avg"] * (1 + REVERSE_END_PCT[st["ticker"]] / 100.0)


def leave_reverse(st):
    """Back to normal mode: T is kept as it is (post), the unit is cash / (N - T) again; reverse memory cleared."""
    s = dict(st, mode="normal")
    s["extras"] = {k: v for k, v in (st.get("extras") or {}).items() if k not in ("reverse_day", "reverse_since", "rev_done")}
    return s


def implied_T(version, ticker, splits, avg, quarter_sell_price):
    """Back-solve T from the price of the quarter-sell LOC order the account has outstanding (it sits at the star point): star% = (price/avg - 1)*100, T = N/2 * (1 - star%/base)."""
    sp = (quarter_sell_price / avg - 1.0) * 100.0
    return splits / 2.0 * (1.0 - sp / STAR_BASE[version][ticker])
