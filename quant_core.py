"""
quant_core.py
─────────────────────────────────────────────
정통 퀀트 추격매수(Chase Momentum) 아키텍처

[핵심 변경]
1. 억지로 10개 채우기 완전 폐지: 절대 조건 미달 시 0개 반환
2. 진짜 추격매수 조건 도입 (Trend, Breakout, Volume Surge)
3. 스코어(Ranking)는 생존 종목들의 상대평가용으로만 사용
4. 펀더멘털(Fundamental) 스크래퍼 강화 및 빈값 통과 버그(>=0) 차단
5. [수정] 현실적 진입가(Entry Price) 적용: 과거 이동평균(5MA)이 아닌 종가 돌파 시점(curr_price) 사용
6. [추가] UI 리포트용 지표 전체(PER, PBR, ROA, 영업이익률, 순이익률, 배당수익률, 시총 등) 완벽 수집 및 NULL 방지
"""

import json, re, time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import FinanceDataReader as fdr

# ──────────────────────────────────────────
# 공통 유틸
# ──────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.utcnow() + timedelta(hours=9)

def now_kst_str() -> str:
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")

def is_expired(ts_str: str, threshold_sec: int) -> bool:
    if not ts_str: return True
    try:
        clean = ts_str.replace("T", " ").split(".")[0].split("+")[0]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        return (now_kst() - dt).total_seconds() >= threshold_sec
    except:
        return True

# 테이블 상수
TBL_DAILY   = "stock_daily"
TBL_FUNDA   = "stock_fundamental"
TBL_SCREEN  = "quant_screening_cache"
TBL_WATCH   = "quant_watchlist_cache"

ROLLING_DAYS   = 756
FUNDA_TTL_SEC  = 86400 * 90
PREFILTER_MARCAP_억 = 1500
PREFILTER_TVOL_억   = 50

# 확정/워치리스트 필터 기준 (절대 기준)
CONFIRM_FILTER_MIN  = 6     # 6개 강력한 추격매수 조건 ALL PASS
WATCHLIST_FILTER_MIN= 4     # 최소 4개 이상 통과시 관심종목

# ══════════════════════════════════════════
# [규모 1] 시장 레짐(국면) 파라미터
# ══════════════════════════════════════════
# 레짐별 ATR 트레일링 배수 — 강세장은 넉넉히 태우고, 약세장은 바짝 조임
REGIME_ATR_MULT = {"BULL": 3.0, "NEUTRAL": 2.5, "BEAR": 1.5}
# 레짐별 초기 손절 상한(%) — 약세장은 최대 손실폭 자체를 줄임
REGIME_RISK_CAP = {"BULL": 0.15, "NEUTRAL": 0.15, "BEAR": 0.10}
# 레짐별 워치리스트 진입 문턱 — 약세장엔 더 깐깐하게
REGIME_WATCHLIST_MIN = {"BULL": WATCHLIST_FILTER_MIN, "NEUTRAL": WATCHLIST_FILTER_MIN, "BEAR": WATCHLIST_FILTER_MIN + 1}

# ══════════════════════════════════════════
# [순위 2] 이익 보호(Profit Protection) 파라미터 — 고정 40% 익절 폐지
# ══════════════════════════════════════════
PROFIT_LOCK_TRIGGER_PCT     = 15.0  # 수익률이 이 값을 넘으면 "이익 보호 모드" 진입
PROFIT_LOCK_ATR_MULT_FACTOR = 0.6   # 보호 모드에서 트레일링 ATR 배수를 좁히는 비율 (더 타이트하게 추종)
VOL_COOLING_RATIO           = 0.8   # 최근 5일 거래량이 20일 평균의 이 비율 밑으로 식으면 "모멘텀 소진" 후보

# ══════════════════════════════════════════
# [순위 3] 추세 붕괴(Trend Breakdown) 조기화 — 3중 AND(만장일치) 완화
# ══════════════════════════════════════════
# 추세붕괴 3개 하위신호(가격<20일선 / 10일선<20일선 / 20일선 하락전환) 중
# 몇 개 이상 충족돼야 "추세붕괴"로 판정할지를 레짐별로 다르게 적용
# BULL/NEUTRAL: 2/3 (다수결 - 기존 3/3 만장일치보다 빠르지만 하루짜리 노이즈엔 안 흔들림)
# BEAR: 1/3 (약세장에선 의심 신호 하나만 떠도 즉시 컷)
REGIME_TREND_BREAK_MIN = {"BULL": 2, "NEUTRAL": 2, "BEAR": 1}

def get_market_regime(lookback_days: int = 300) -> dict:
    """
    코스피 지수 기반 시장 레짐(국면) 판정
    - BULL    : 종가가 120일선 위 + 120일선 자체가 상승 중
    - BEAR    : 종가가 120일선 아래 + 120일선 자체가 하락 중
    - NEUTRAL : 그 외 (박스권 / 전환 구간) → 보수적으로 중립 취급
    """
    try:
        end = now_kst()
        start = end - timedelta(days=lookback_days)
        kospi = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if kospi.empty or len(kospi) < 130:
            return {"regime": "NEUTRAL", "reason": "데이터 부족"}

        close = kospi["Close"]
        ma120 = close.rolling(120).mean()
        curr_close = float(close.iloc[-1])
        curr_ma120 = float(ma120.iloc[-1])
        prev_ma120 = float(ma120.iloc[-20])  # 약 1개월 전 120일선과 비교해 기울기 판정

        above_ma  = curr_close > curr_ma120
        ma_rising = curr_ma120 > prev_ma120

        if above_ma and ma_rising:
            regime = "BULL"
        elif (not above_ma) and (not ma_rising):
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        return {
            "regime": regime,
            "kospi_close": round(curr_close, 2),
            "ma120": round(curr_ma120, 2),
            "ma120_slope_pct": round((curr_ma120 / prev_ma120 - 1) * 100, 2),
        }
    except Exception as e:
        return {"regime": "NEUTRAL", "reason": f"오류: {e}"}

# ══════════════════════════════════════════
# [A] 유니버스 사전 필터링 & [B] 일봉 DB (유지)
# ══════════════════════════════════════════
def _normalize_listing(raw: pd.DataFrame, market: str) -> pd.DataFrame:
    col = raw.columns.tolist()
    sym = next((c for c in ["Symbol", "Code", "Ticker"] if c in col), None)
    name = next((c for c in ["Name", "종목명"] if c in col), None)
    cap = next((c for c in ["Marcap", "시가총액"] if c in col), None)
    close_col = next((c for c in ["Close", "종가"] if c in col), None)
    vol_col = next((c for c in ["Volume", "거래량"] if c in col), None)
    amt_col = next((c for c in ["Amount", "거래대금"] if c in col), None)

    df = pd.DataFrame({
        "Symbol": raw[sym].astype(str).str.zfill(6), "Name": raw[name].astype(str),
        "Market": market,
        "Marcap": pd.to_numeric(raw[cap], errors="coerce") if cap else 0,
        "Close": pd.to_numeric(raw[close_col], errors="coerce") if close_col else 0,
    })
    if amt_col: df["Amount"] = pd.to_numeric(raw[amt_col], errors="coerce").fillna(0)
    elif vol_col and close_col: df["Amount"] = pd.to_numeric(raw[vol_col], errors="coerce").fillna(0) * pd.to_numeric(raw[close_col], errors="coerce").fillna(0)
    else: df["Amount"] = 0
    return df

def load_filtered_universe(marcap_min_억: int = PREFILTER_MARCAP_억, tvol_min_억: int = PREFILTER_TVOL_억) -> pd.DataFrame:
    kospi = _normalize_listing(fdr.StockListing("KOSPI"), "KOSPI")
    kosdaq = _normalize_listing(fdr.StockListing("KOSDAQ"), "KOSDAQ")
    raw_df = pd.concat([kospi, kosdaq], ignore_index=True)
    raw_df = raw_df[raw_df["Symbol"].str.len() == 6].dropna(subset=["Symbol","Name"])

    exclude_kw = ["ETF","ETN","스팩","리츠","REIT","인프라","선박"]
    mask_name = raw_df["Name"].str.contains("|".join(exclude_kw), na=False)
    mask_code = raw_df["Symbol"].str[-1] != "0"
    common = raw_df[~mask_name & ~mask_code].copy()

    cap_filtered = common[common["Marcap"] >= (marcap_min_억 * 1e8)].copy()
    cap_filtered["TradingVol억"] = cap_filtered["Amount"] / 1e8 if "Amount" in cap_filtered.columns else 0
    final = cap_filtered[cap_filtered["TradingVol억"] >= tvol_min_억].copy().reset_index(drop=True)
    return final

def load_price_from_db(supabase, symbol: str) -> pd.DataFrame:
    try:
        res = supabase.table(TBL_DAILY).select("date,open,high,low,close,volume").eq("symbol", symbol).order("date", desc=False).execute()
        if not res.data: return pd.DataFrame()
        df = pd.DataFrame(res.data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index().rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
        for col in ["Open","High","Low","Close","Volume"]:
            if col in df.columns: df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["Close"])
    except: return pd.DataFrame()

def upsert_daily_rows(supabase, symbol: str, name: str, rows: list):
    if rows: supabase.table(TBL_DAILY).upsert([{**r, "symbol": symbol, "name": name} for r in rows], on_conflict="symbol,date").execute()

def trim_old_rows(supabase, symbol: str):
    try:
        res = supabase.table(TBL_DAILY).select("date").eq("symbol", symbol).order("date", desc=False).execute()
        dates = [r["date"] for r in res.data]
        if len(dates) > ROLLING_DAYS: supabase.table(TBL_DAILY).delete().eq("symbol", symbol).lte("date", dates[len(dates) - ROLLING_DAYS - 1]).execute()
    except: pass

# ══════════════════════════════════════════
# [C] 펀더멘털 스크래핑 (업그레이드 적용)
# ══════════════════════════════════════════
def _parse_num(txt) -> float | None:
    if not txt: return None
    try:
        clean = re.sub(r"[^\d.\-]", "", str(txt).replace(",", "").strip())
        return float(clean) if clean and clean != "-" else None
    except: return None

def fetch_dart_financial(corp_code: str, dart_api_key: str, year: int = None) -> dict:
    if year is None: year = now_kst().year - 1
    url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
    result = {"roe": None, "debt_ratio": None, "op_profit": None, "net_income": None, "revenue": None, "eps": None}
    for reprt_code in ["11011", "11012"]:
        try:
            res = requests.get(url, params={"crtfc_key": dart_api_key, "corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": "CFS"}, timeout=10).json()
            if res.get("status") != "000": continue
            for item in res.get("list", []):
                acnt, val_raw = item.get("account_nm", ""), _parse_num(item.get("thstrm_amount", "0"))
                val_억 = val_raw / 1e8
                if "영업이익" in acnt and "영업이익률" not in acnt and result["op_profit"] is None: result["op_profit"] = val_억
                if "당기순이익" in acnt and result["net_income"] is None: result["net_income"] = val_억
                if "매출액" in acnt and result["revenue"] is None: result["revenue"] = val_억
                if "ROE" in acnt and result["roe"] is None: result["roe"] = val_raw
                if "부채비율" in acnt and result["debt_ratio"] is None: result["debt_ratio"] = val_raw
            break
        except: pass
    return result

def fetch_naver_fundamental(symbol: str) -> dict:
    """네이버 펀더멘털 종합 수집 (누락 방지) - UI 리포트용 모든 필드 포함"""
    fund = {
        "roe": None, "debt_ratio": None,
        "op_profit_cur": None, "op_profit_prev": None,
        "net_income_cur": None, "net_income_prev": None,
        "revenue_cur": None, "revenue_prev": None,
        "op_margin": None, "net_margin": None, "roa": None,
        "per": None, "pbr": None, "eps_cur": None, "eps_prev": None,
        "dividend_yield": None, "marcap_억": None,
        "sector": None
    }
    try:
        url = f"https://finance.naver.com/item/main.naver?code={symbol}"
        res = requests.get(url, headers={'User-agent': 'Mozilla/5.0'}, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')

        # 💡 [추가] 섹터(업종) 추출
        upjong_elem = soup.select_one("a[href*='type=upjong']")
        if upjong_elem:
            fund['sector'] = upjong_elem.text.strip()

        # 시가총액 (marcap_억) 추출
        marcap_elem = soup.select_one("#_market_sum")
        if marcap_elem:
            txt = marcap_elem.text.strip().replace(',', '').replace('\t', '').replace('\n', '')
            if '조' in txt:
                parts = txt.split('조')
                jo = int(re.sub(r'\D', '', parts[0])) if parts[0] else 0
                eok_str = re.sub(r'\D', '', parts[1]) if len(parts)>1 else ''
                eok = int(eok_str) if eok_str else 0
                fund['marcap_억'] = jo * 10000 + eok
            else:
                eok_str = re.sub(r'\D', '', txt)
                fund['marcap_억'] = int(eok_str) if eok_str else 0

        table = soup.select_one("div.cop_analysis table")
        if table:
            for tr in table.select("tbody tr"):
                th = tr.select_one("th")
                if not th: continue
                label = th.text.strip()
                tds = tr.select("td")

                vals = []
                for td in tds:
                    clean = re.sub(r"[^\d.\-]", "", td.text)
                    if clean and clean != '-': vals.append(float(clean))
                    else: vals.append(None)

                valid_vals = [v for v in vals if v is not None]
                recent_val = valid_vals[-1] if valid_vals else None
                prev_val = valid_vals[-2] if len(valid_vals) > 1 else None

                if "매출액" in label:
                    fund['revenue_cur'] = recent_val
                    fund['revenue_prev'] = prev_val
                elif "영업이익" in label and "영업이익률" not in label:
                    fund['op_profit_cur'] = recent_val
                    fund['op_profit_prev'] = prev_val
                elif "당기순이익" in label or ("순이익" in label and "순이익률" not in label):
                    fund['net_income_cur'] = recent_val
                    fund['net_income_prev'] = prev_val
                elif "영업이익률" in label: fund['op_margin'] = recent_val
                elif "순이익률" in label: fund['net_margin'] = recent_val
                elif "ROE" in label: fund['roe'] = recent_val
                elif "ROA" in label: fund['roa'] = recent_val
                elif "부채비율" in label: fund['debt_ratio'] = recent_val
                elif "PER" in label: fund['per'] = recent_val
                elif "PBR" in label: fund['pbr'] = recent_val
                elif "EPS" in label:
                    fund['eps_cur'] = recent_val
                    fund['eps_prev'] = prev_val
                elif "배당수익률" in label or "시가배당률" in label:
                    fund['dividend_yield'] = recent_val

    except: pass
    return fund

def load_fundamental_from_db(supabase, symbol: str) -> dict | None:
    try:
        res = supabase.table(TBL_FUNDA).select("*").eq("symbol", symbol).execute()
        if res.data and not is_expired(res.data[0].get("updated_at",""), FUNDA_TTL_SEC): return res.data[0]
    except: pass
    return None

def save_fundamental_to_db(supabase, symbol: str, name: str, data: dict):
    payload = {
        "symbol": symbol, "name": name,
        "roe": data.get("roe"), "debt_ratio": data.get("debt_ratio"),
        "op_profit_cur": data.get("op_profit_cur"), "op_profit_prev": data.get("op_profit_prev"),
        "net_income_cur": data.get("net_income_cur"), "net_income_prev": data.get("net_income_prev"),
        "revenue_cur": data.get("revenue_cur"), "revenue_prev": data.get("revenue_prev"),
        # [수정] 수급이 0일 때 업데이트가 무시되어 NULL이 박히는 현상을 방지하고자 기본값 0을 세팅
        "foreign_net_buy": data.get("foreign_net_buy", 0),
        "institute_net_buy": data.get("institute_net_buy", 0),
        "op_margin": data.get("op_margin"),
        "net_margin": data.get("net_margin"),
        "roa": data.get("roa"),
        "per": data.get("per"),
        "pbr": data.get("pbr"),
        "eps_cur": data.get("eps_cur"),
        "eps_prev": data.get("eps_prev"),
        "dividend_yield": data.get("dividend_yield"),
        "marcap_억": data.get("marcap_억"),
        "sector": data.get("sector"),
        "updated_at": now_kst_str(),
    }
    try:
        supabase.table(TBL_FUNDA).upsert(payload).execute()
    except Exception as e:
        print(f"  [!] DB 저장 오류 (신규 컬럼 누락 시 무시): {e}")

def get_fundamental(supabase, symbol: str, name: str, dart_api_key: str = "", dart_corp_map: dict = None) -> dict:
    cached = load_fundamental_from_db(supabase, symbol)
    if cached: return cached
    data = {}
    if dart_api_key and dart_corp_map and dart_corp_map.get(symbol):
        c_code = dart_corp_map.get(symbol)
        cur, prev = fetch_dart_financial(c_code, dart_api_key, now_kst().year - 1), fetch_dart_financial(c_code, dart_api_key, now_kst().year - 2)
        data.update({"op_profit_cur": cur.get("op_profit"), "op_profit_prev": prev.get("op_profit"),
                     "net_income_cur": cur.get("net_income"), "net_income_prev": prev.get("net_income"),
                     "revenue_cur": cur.get("revenue"), "revenue_prev": prev.get("revenue"),
                     "roe": cur.get("roe"), "debt_ratio": cur.get("debt_ratio")})

    naver = fetch_naver_fundamental(symbol)
    for k, v in naver.items():
        if data.get(k) is None and v is not None: data[k] = v
    time.sleep(0.3)
    save_fundamental_to_db(supabase, symbol, name, data)
    return data

# ══════════════════════════════════════════
# [D] 퀀트 지표 (Strict Chase Momentum)
# ══════════════════════════════════════════
def calc_quant_metrics(df: pd.DataFrame, fund: dict) -> dict:
    """엄격한 돌파/추격매수(Chase Momentum) 지표 산출"""
    metrics = {}
    close = df["Close"]
    vol = df["Volume"]

    # 1. Growth Composite
    def safe_yoy(c, p):
        if c is None or p is None or p == 0: return 0.0
        return (c - p) / abs(p) * 100

    metrics["net_yoy"] = safe_yoy(fund.get("net_income_cur"), fund.get("net_income_prev"))
    op_yoy = safe_yoy(fund.get("op_profit_cur"), fund.get("op_profit_prev"))
    rev_yoy = safe_yoy(fund.get("revenue_cur"), fund.get("revenue_prev"))
    metrics["growth_composite"] = (metrics["net_yoy"] * 0.5) + (op_yoy * 0.3) + (rev_yoy * 0.2)

    if len(df) < 60:
        return {k: 0 for k in ["growth_composite","mdd","dynamic_mdd_limit","liquidity_20d","ma20","ma60","high_60d","vol_5d","vol_60d","supply_demand"]}

    # 2. Dynamic MDD (ATR 기반 생존 방어선)
    roll_max = close.tail(60).cummax()
    metrics["mdd"] = ((close.tail(60) - roll_max) / roll_max * 100).min()

    high, low, prev_close = df.get("High", close), df.get("Low", close), close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr20 = tr.rolling(20).mean().iloc[-1]
    metrics["dynamic_mdd_limit"] = max(-40.0, min(-15.0, -((atr20 / close.iloc[-1]) * 300))) # ATR의 3배수

    # 3. Liquidity
    metrics["liquidity_20d"] = (close * vol).iloc[-20:].mean() / 1e8

    # 4. Trend Alignment (정배열 추세)
    metrics["ma20"] = close.iloc[-20:].mean()
    metrics["ma60"] = close.iloc[-60:].mean()

    # 5. Price Breakout (60일 신고가 근접성)
    metrics["high_60d"] = close.iloc[-60:].max()

    # 6. Volume Surge (거래량 급증)
    metrics["vol_5d"] = vol.iloc[-5:].mean()
    metrics["vol_60d"] = vol.iloc[-60:].mean()

    # 7. Supply / Demand
    metrics["supply_demand"] = (fund.get("foreign_net_buy") or 0) + (fund.get("institute_net_buy") or 0)

    return metrics

# ══════════════════════════════════════════
# [E] 분리된 스크리닝 엔진 (Survival Filter -> Score Ranking)
# ══════════════════════════════════════════
def run_screening_from_db(supabase, universe_df: pd.DataFrame, log_fn=print, regime: str = "NEUTRAL") -> tuple:
    candidates = []
    watchlist_min = REGIME_WATCHLIST_MIN.get(regime, WATCHLIST_FILTER_MIN)
    log_fn(f"  [레짐] 현재 시장 국면: {regime} (워치리스트 문턱: {watchlist_min}/6)")

    # ── Phase 1. Strict Survival & Chase Filters ──
    for _, row in universe_df.iterrows():
        symbol, name = row["Symbol"], row.get("Name", row["Symbol"])
        df = load_price_from_db(supabase, symbol)
        if df.empty or len(df) < 60: continue

        fund = load_fundamental_from_db(supabase, symbol) or {}
        metrics = calc_quant_metrics(df, fund)
        if "ma20" not in metrics or metrics["ma20"] == 0: continue

        curr_price = int(df["Close"].iloc[-1])

        # 절대 조건 6가지 (강력한 추격매수 로직)
        f_growth = metrics["growth_composite"] > 0
        f_mdd    = metrics["mdd"] >= metrics["dynamic_mdd_limit"]
        f_liq    = metrics["liquidity_20d"] >= 50
        f_trend  = (curr_price > metrics["ma20"]) and (metrics["ma20"] > metrics["ma60"])
        f_break  = curr_price >= (metrics["high_60d"] * 0.90)
        f_vol    = metrics["vol_5d"] > (metrics["vol_60d"] * 1.5)

        pass_count = sum([f_growth, f_mdd, f_liq, f_trend, f_break, f_vol])

        if pass_count < watchlist_min:
            continue

        entry_price = curr_price

        # 성장률 YoY 및 영업이익률 UI 보고용 사전 계산
        c_net = fund.get('net_income_cur')
        p_net = fund.get('net_income_prev')
        net_yoy = ((c_net - p_net) / abs(p_net) * 100) if c_net is not None and p_net is not None and p_net != 0 else None

        c_rev = fund.get('revenue_cur')
        p_rev = fund.get('revenue_prev')
        rev_yoy = ((c_rev - p_rev) / abs(p_rev) * 100) if c_rev is not None and p_rev is not None and p_rev != 0 else None

        c_op = fund.get('op_profit_cur')
        op_margin = fund.get('op_margin')
        if op_margin is None and c_op is not None and c_rev:
            op_margin = (c_op / c_rev) * 100

        # UI에서 N/A 방지를 위해 캐시 JSON 내부에 모든 펀더멘털 데이터를 꽉꽉 채워 넣습니다!
        candidates.append({
            "symbol": symbol, "name": name, "market": row.get("Market", "-"),
            "sector": fund.get("sector"),
            "marcap_억": fund.get("marcap_억", round(row.get("Marcap", 0) / 1e8, 0)),
            "current_price": curr_price, "entry_price": entry_price,
            "ret_1m": round(float((curr_price - df["Close"].iloc[-21]) / df["Close"].iloc[-21] * 100) if len(df) >= 21 else 0.0, 2),
            "metrics": metrics, "pass_count": pass_count,
            "roe": fund.get("roe"),
            "debt_ratio": fund.get("debt_ratio"),
            "op_margin": op_margin,
            "net_margin": fund.get("net_margin"),
            "roa": fund.get("roa"),
            "per": fund.get("per"),
            "pbr": fund.get("pbr"),
            "eps_cur": fund.get("eps_cur"),
            "eps_prev": fund.get("eps_prev"),
            "dividend_yield": fund.get("dividend_yield"),
            "revenue_cur": fund.get("revenue_cur"),
            "op_profit_cur": fund.get("op_profit_cur"),
            "net_income_cur": fund.get("net_income_cur"),
            "revenue_yoy": rev_yoy,
            "net_income_yoy": net_yoy,
            "filter_details": {
                "Growth Composite": {"pass": f_growth, "reason": f"Comp {metrics['growth_composite']:+.1f}%"},
                "Dynamic MDD": {"pass": f_mdd, "reason": f"MDD {metrics['mdd']:.1f}% (Limit: {metrics['dynamic_mdd_limit']:.1f}%)"},
                "Liquidity": {"pass": f_liq, "reason": f"{metrics['liquidity_20d']:.0f}억"},
                "Trend Alignment": {"pass": f_trend, "reason": f"Price > 20MA > 60MA"},
                "Price Breakout": {"pass": f_break, "reason": f"고점대비 {(curr_price/metrics['high_60d'])*100:.1f}%"},
                "Volume Surge": {"pass": f_vol, "reason": f"Vol {(metrics['vol_5d']/metrics['vol_60d']):.1f}x 급증"}
            }
        })

    # ── Phase 2. Scoring & Ranking ──
    if not candidates:
        return [], []

    c_df = pd.DataFrame([c["metrics"] for c in candidates])

    # 상대평가 랭킹으로 단일 팩터 스코어 생성
    s_growth = c_df["growth_composite"].rank(pct=True, na_option='bottom') * 20
    s_break  = (c_df["high_60d"] / c_df["ma20"]).rank(pct=True, ascending=False, na_option='bottom') * 30
    s_vol    = (c_df["vol_5d"] / c_df["vol_60d"]).rank(pct=True, na_option='bottom') * 30
    s_sd     = c_df["supply_demand"].rank(pct=True, na_option='bottom') * 20

    factor_score = s_growth + s_break + s_vol + s_sd

    confirmed, watchlist = [], []
    for i, c in enumerate(candidates):
        c["factor_score"] = round(factor_score.iloc[i], 2)
        if c.get("net_income_yoy") is None:
            c["net_income_yoy"] = round(c["metrics"]["net_yoy"], 2)
        c["momentum_score"] = round(((c["current_price"] - c["metrics"]["ma60"]) / c["metrics"]["ma60"]) * 100, 2)
        c["screened_at"] = now_kst_str()
        c["total_pass"] = c["pass_count"]

        if c["pass_count"] >= CONFIRM_FILTER_MIN:
            confirmed.append(c)
        elif c["pass_count"] >= watchlist_min:
            watchlist.append(c)

    confirmed.sort(key=lambda x: x["factor_score"], reverse=True)
    watchlist.sort(key=lambda x: x["factor_score"], reverse=True)

    return confirmed, watchlist

class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return float(obj)
        if isinstance(obj, (np.bool_,)): return bool(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super().default(obj)

def save_screening_result(supabase, confirmed: list, watchlist: list):
    ts = now_kst_str()
    supabase.table(TBL_SCREEN).upsert([
        {"id": 1, "results": json.dumps(confirmed, ensure_ascii=False, cls=NumpyEncoder), "updated_at": ts},
        {"id": 2, "results": json.dumps(watchlist, ensure_ascii=False, cls=NumpyEncoder), "updated_at": ts}
    ]).execute()

def load_screening_result(supabase) -> tuple:
    c, w, ts = [], [], ""
    try:
        r1 = supabase.table(TBL_SCREEN).select("*").eq("id",1).execute()
        if r1.data: c, ts = json.loads(r1.data[0]["results"]), r1.data[0].get("updated_at","")
        r2 = supabase.table(TBL_SCREEN).select("*").eq("id",2).execute()
        if r2.data: w = json.loads(r2.data[0]["results"])
    except: pass
    return c, w, ts
