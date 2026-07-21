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
TBL_SECTOR  = "stock_sector"   # [추가] 업종 분류 캐시 — 포트폴리오 섹터 분산용

ROLLING_DAYS   = 720   # stock_daily 보관 일수 (721일째 되는 옛날 일봉부터 삭제)
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

# ══════════════════════════════════════════
# [순위 4] 진입(Entry) 개선 — 지속성 / 과열 캡 / 상대강도(RS)
# ══════════════════════════════════════════
# 과열 캡: 고정 %가 아니라 dynamic_mdd_limit과 같은 방식으로 ATR% 기반 동적 계산
#   허용 이격 = ATR% × OVEREXTENSION_ATR_MULT, 단 [FLOOR, CEIL] 사이로 클리핑
#   → 변동성 큰 코스닥 소형 성장주는 넉넉하게, 변동성 낮은 대형주는 빡빡하게 자동 적용
OVEREXTENSION_ATR_MULT  = 4.0
OVEREXTENSION_FLOOR_PCT = 15.0
OVEREXTENSION_CEIL_PCT  = 50.0

# 수급 게이트 지속성: 당일 서지 배수를 1.2배(약함) → 1.5배로 상향, 5일 평균 조건과 밸런스 맞춤
VOL_TODAY_SURGE_MULT    = 1.5

# 돌파 게이트 이중 경로:
#   ① 표준 경로: 오늘+어제 이틀 연속 90% 돌파권 (지속성 확인, 진입가는 하루 늦음)
#   ② 강한 신호 예외: 오늘 거래량이 60일 평균의 이 배수를 넘으면, 1일차라도 즉시 통과
#      → "진짜 강한 돌파는 1일차에 거래량이 터진다"는 지적을 반영해 최고 진입가를 살려둠
STRONG_BREAKOUT_VOL_MULT = 2.0

# RS(상대강도) — 시장별 벤치마크 분리 (KOSPI 종목→KS11, KOSDAQ 종목→KQ11)
RS_LOOKBACK_DAYS       = 60

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

def get_index_return_pct(index_code: str = "KS11", period_days: int = RS_LOOKBACK_DAYS, lookback_days: int = 120) -> float:
    """
    지수(코스피 KS11 / 코스닥 KQ11)의 최근 N일 수익률(%) — 종목별 상대강도(RS) 계산용 벤치마크 값
    [순위4 개정] 벤치마크를 KOSPI 하나로 통일하지 않고 종목 시장에 맞는 지수를 쓸 수 있도록 일반화
    """
    try:
        end = now_kst()
        start = end - timedelta(days=lookback_days)
        idx = fdr.DataReader(index_code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if idx.empty or len(idx) < period_days + 1:
            return 0.0
        close = idx["Close"]
        return float((close.iloc[-1] / close.iloc[-(period_days + 1)] - 1) * 100)
    except Exception:
        return 0.0

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
    sector_col = next((c for c in ["Sector", "업종"] if c in col), None)
    industry_col = next((c for c in ["Industry", "산업"] if c in col), None)

    df = pd.DataFrame({
        "Symbol": raw[sym].astype(str).str.zfill(6), "Name": raw[name].astype(str),
        "Market": market,
        "Marcap": pd.to_numeric(raw[cap], errors="coerce") if cap else 0,
        "Close": pd.to_numeric(raw[close_col], errors="coerce") if close_col else 0,
    })
    if amt_col: df["Amount"] = pd.to_numeric(raw[amt_col], errors="coerce").fillna(0)
    elif vol_col and close_col: df["Amount"] = pd.to_numeric(raw[vol_col], errors="coerce").fillna(0) * pd.to_numeric(raw[close_col], errors="coerce").fillna(0)
    else: df["Amount"] = 0
    # [추가] 업종/산업 — fdr.StockListing이 기본 제공하는 컬럼이라 별도 API 호출 없이 그대로 실음
    df["Sector"] = raw[sector_col].astype(str) if sector_col else ""
    df["Industry"] = raw[industry_col].astype(str) if industry_col else ""
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
# [B-1] 일봉 공백(Gap) 탐지 & 자동 채움 (init)
# ══════════════════════════════════════════
def get_trading_calendar(lookback_days: int = ROLLING_DAYS) -> list:
    """
    실제 개장일 목록(코스피 지수 기준)을 하나만 받아서 모든 종목의 결측일 판정 기준으로 재사용.
    배치 1회 실행당 딱 1번만 호출하면 됨 (종목마다 부르지 않음).
    """
    try:
        end = now_kst()
        start = end - timedelta(days=lookback_days + 10)
        idx = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        if idx.empty: return []
        return [d.strftime("%Y-%m-%d") for d in idx.index]
    except Exception:
        return []

def find_price_gaps(supabase, symbol: str, trading_calendar: list) -> list:
    """DB에 이미 있는 날짜와 실제 개장일 캘린더를 비교해서 비어있는 날짜 목록을 반환"""
    if not trading_calendar: return []
    try:
        res = supabase.table(TBL_DAILY).select("date").eq("symbol", symbol).execute()
        existing = {r["date"] for r in res.data}
    except Exception:
        return []
    today_str = now_kst().strftime("%Y-%m-%d")
    # 오늘자는 배치 STEP2(당일 시세 수집)에서 별도로 채우므로 캘린더에서 제외하고 비교
    return sorted(d for d in trading_calendar if d != today_str and d not in existing)

def _default_history_fetcher(symbol: str, start_date: str, end_date: str) -> list:
    """
    기본 폴백 fetcher (FDR 크롤링). history_fetcher를 안 넘기면 이걸 씀.
    ⚠️ 크롤링 기반이라 응답이 느리거나 막힐 수 있음 — 배치(cron)에서는 KIS API 기반 fetcher를 넘겨서 씀.
    """
    try:
        df = fdr.DataReader(symbol, start_date, end_date)
        if df.empty: return []
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        return [
            {"date": dt.strftime("%Y-%m-%d"), "open": r.get("open"), "high": r.get("high"),
             "low": r.get("low"), "close": r.get("close"), "volume": r.get("volume")}
            for dt, r in df.iterrows()
        ]
    except Exception:
        return []

def fill_price_gaps(supabase, symbol: str, name: str, trading_calendar: list, history_fetcher=None) -> int:
    """
    [init] 결측 구간이 있으면 다시 받아서 stock_daily 공백을 메운다.
    (예: 특정 기간 시세 수집이 누락된 경우 자동 복구용)

    history_fetcher(symbol, start_date, end_date) -> list[{"date","open","high","low","close","volume"}]
      배치(cron)에서는 이미 인증된 KIS API 기반 fetcher를 넘겨서 씀.
      크롤링(FDR) 기반은 응답이 멈추거나 차단될 수 있어 배치에서는 권장하지 않음 → 안 넘기면 폴백으로만 사용.

    반환값: 실제로 채운 행 수. 개별 종목에서 실패해도 예외를 삼키고 0을 반환해 배치 전체가 멈추지 않게 한다.
    """
    missing_dates = find_price_gaps(supabase, symbol, trading_calendar)
    if not missing_dates:
        return 0

    fetcher = history_fetcher or _default_history_fetcher
    try:
        raw_rows = fetcher(symbol, missing_dates[0], missing_dates[-1])
    except Exception:
        return 0
    if not raw_rows:
        return 0

    missing_set = set(missing_dates)
    rows = []
    for r in raw_rows:
        d_str = r.get("date")
        if not d_str or d_str not in missing_set:
            continue
        close_v = r.get("close")
        if close_v is None or (isinstance(close_v, float) and pd.isna(close_v)) or close_v == 0:
            continue
        try:
            rows.append({
                "date": d_str,
                "open": int(r.get("open") or 0),
                "high": int(r.get("high") or 0),
                "low": int(r.get("low") or 0),
                "close": int(close_v),
                "volume": int(r.get("volume") or 0),
            })
        except (ValueError, TypeError):
            continue

    if rows:
        upsert_daily_rows(supabase, symbol, name, rows)
    return len(rows)

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
def calc_quant_metrics(df: pd.DataFrame, fund: dict, benchmark_ret_60d: float = 0.0) -> dict:
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
        return {k: 0 for k in ["growth_composite","mdd","dynamic_mdd_limit","liquidity_20d","ma20","ma60","high_60d","vol_5d","vol_60d","supply_demand","rs_60d","dynamic_overext_limit_pct"]}

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

    # [순위4] 과열(Overextension) 캡을 dynamic_mdd_limit과 같은 방식으로 ATR% 기반 동적 산출
    # 변동성 큰 종목(코스닥 소형 성장주 등)은 20일선 이격 허용폭이 자동으로 넓어지고,
    # 변동성 낮은 대형주는 자동으로 빡빡해짐 (고정 %로 두면 생기는 왜곡을 제거)
    atr_pct = (atr20 / close.iloc[-1]) * 100
    metrics["dynamic_overext_limit_pct"] = max(OVEREXTENSION_FLOOR_PCT, min(OVEREXTENSION_CEIL_PCT, atr_pct * OVEREXTENSION_ATR_MULT))

    # 5. Price Breakout (60일 신고가 근접성)
    metrics["high_60d"] = close.iloc[-60:].max()

    # 6. Volume Surge (거래량 급증)
    metrics["vol_5d"] = vol.iloc[-5:].mean()
    metrics["vol_60d"] = vol.iloc[-60:].mean()

    # 7. Supply / Demand
    metrics["supply_demand"] = (fund.get("foreign_net_buy") or 0) + (fund.get("institute_net_buy") or 0)

    # 8. Relative Strength (RS) — 시장별 벤치마크(KOSPI/KOSDAQ 분리) 대비 60일 수익률 격차. 랭킹 전용 팩터
    if len(close) >= RS_LOOKBACK_DAYS + 1:
        stock_ret = (close.iloc[-1] / close.iloc[-(RS_LOOKBACK_DAYS + 1)] - 1) * 100
        metrics["rs_60d"] = stock_ret - benchmark_ret_60d
    else:
        metrics["rs_60d"] = 0.0

    return metrics

# ══════════════════════════════════════════
# [E] 분리된 스크리닝 엔진 (Survival Filter -> Score Ranking)
# ══════════════════════════════════════════
def run_screening_from_db(supabase, universe_df: pd.DataFrame, log_fn=print, regime: str = "NEUTRAL",
                           kospi_ret_60d: float = 0.0, kosdaq_ret_60d: float = 0.0) -> tuple:
    candidates = []
    watchlist_min = REGIME_WATCHLIST_MIN.get(regime, WATCHLIST_FILTER_MIN)
    log_fn(f"  [레짐] 현재 시장 국면: {regime} (워치리스트 문턱: {watchlist_min}/6) | "
           f"RS 벤치마크 {RS_LOOKBACK_DAYS}일: KOSPI {kospi_ret_60d:+.2f}% / KOSDAQ {kosdaq_ret_60d:+.2f}%")

    # ── Phase 1. Strict Survival & Chase Filters ──
    for _, row in universe_df.iterrows():
        symbol, name = row["Symbol"], row.get("Name", row["Symbol"])
        df = load_price_from_db(supabase, symbol)
        if df.empty or len(df) < 60: continue

        # [순위4] RS 벤치마크를 시장별로 분리 (코스닥 종목을 코스피와 비교하는 왜곡 제거)
        benchmark_ret = kosdaq_ret_60d if str(row.get("Market", "")).upper().startswith("KOSDAQ") else kospi_ret_60d

        fund = load_fundamental_from_db(supabase, symbol) or {}
        metrics = calc_quant_metrics(df, fund, benchmark_ret_60d=benchmark_ret)
        if "ma20" not in metrics or metrics["ma20"] == 0: continue

        curr_price = int(df["Close"].iloc[-1])
        prev_price = int(df["Close"].iloc[-2]) if len(df) >= 2 else curr_price
        today_vol  = df["Volume"].iloc[-1]
        breakout_threshold = metrics["high_60d"] * 0.90

        # 절대 조건 6가지 (강력한 추격매수 로직)
        # [순위4] f_trend: 과열 캡을 ATR% 기반 동적 이격도로 적용 (dynamic_overext_limit_pct)
        f_growth = metrics["growth_composite"] > 0
        f_mdd    = metrics["mdd"] >= metrics["dynamic_mdd_limit"]
        f_liq    = metrics["liquidity_20d"] >= 50
        f_trend  = (curr_price > metrics["ma20"]) and (metrics["ma20"] > metrics["ma60"]) \
                   and (curr_price <= metrics["ma20"] * (1 + metrics["dynamic_overext_limit_pct"] / 100))

        # [순위4] f_break: 이중 경로 — ①표준(2일 연속 돌파권) ②예외(1일차라도 거래량 폭발이면 즉시 인정)
        # "진짜 강한 돌파는 1일차에 거래량이 터진다"는 점을 반영해 최고 진입가를 살려둔다.
        f_break_confirmed = (curr_price >= breakout_threshold) and (prev_price >= breakout_threshold)
        f_break_strong_day1 = (curr_price >= breakout_threshold) and (today_vol > (metrics["vol_60d"] * STRONG_BREAKOUT_VOL_MULT))
        f_break = f_break_confirmed or f_break_strong_day1

        # [순위4] f_vol: 당일 서지 배수 1.2배(약함) → 1.5배로 상향, 5일평균 조건과 밸런스
        f_vol = (metrics["vol_5d"] > (metrics["vol_60d"] * 1.5)) and (today_vol > (metrics["vol_60d"] * VOL_TODAY_SURGE_MULT))

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
            "rs_60d": round(metrics.get("rs_60d", 0.0), 2),
            "filter_details": {
                "Growth Composite": {"pass": f_growth, "reason": f"Comp {metrics['growth_composite']:+.1f}%"},
                "Dynamic MDD": {"pass": f_mdd, "reason": f"MDD {metrics['mdd']:.1f}% (Limit: {metrics['dynamic_mdd_limit']:.1f}%)"},
                "Liquidity": {"pass": f_liq, "reason": f"{metrics['liquidity_20d']:.0f}억"},
                "Trend Alignment": {"pass": f_trend, "reason": f"Price > 20MA > 60MA, 동적 과열캡 {metrics['dynamic_overext_limit_pct']:.1f}% 이내"},
                "Price Breakout": {"pass": f_break, "reason": ("1일차 강한돌파(Vol≥60d×{:.1f})".format(STRONG_BREAKOUT_VOL_MULT) if f_break_strong_day1 else f"고점대비 {(curr_price/metrics['high_60d'])*100:.1f}% (2일 연속 돌파권)")},
                "Volume Surge": {"pass": f_vol, "reason": f"Vol {(metrics['vol_5d']/metrics['vol_60d']):.1f}x 급증 (당일 거래량도 {VOL_TODAY_SURGE_MULT}x 이상)"}
            }
        })

    # ── Phase 2. Scoring & Ranking ──
    if not candidates:
        return [], []

    c_df = pd.DataFrame([c["metrics"] for c in candidates])

    # 상대평가 랭킹으로 단일 팩터 스코어 생성
    # [순위4] RS(상대강도) 팩터 추가 — 가중치 재배분(20/30/30/20 → 15/25/25/15/20, 합계 100 유지)
    s_growth = c_df["growth_composite"].rank(pct=True, na_option='bottom') * 15
    s_break  = (c_df["high_60d"] / c_df["ma20"]).rank(pct=True, ascending=False, na_option='bottom') * 25
    s_vol    = (c_df["vol_5d"] / c_df["vol_60d"]).rank(pct=True, na_option='bottom') * 25
    s_sd     = c_df["supply_demand"].rank(pct=True, na_option='bottom') * 15
    s_rs     = c_df["rs_60d"].rank(pct=True, na_option='bottom') * 20

    factor_score = s_growth + s_break + s_vol + s_sd + s_rs

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

# ══════════════════════════════════════════
# [F] 백테스트 결과 캐시 (id=14) — 프론트 차트용
# ══════════════════════════════════════════
TBL_BACKTEST_CACHE_ID = 14

def save_backtest_result(supabase, result: dict):
    ts = now_kst_str()
    supabase.table(TBL_SCREEN).upsert([
        {"id": TBL_BACKTEST_CACHE_ID, "results": json.dumps(result, ensure_ascii=False, cls=NumpyEncoder), "updated_at": ts}
    ]).execute()

def load_backtest_result(supabase) -> dict:
    try:
        r = supabase.table(TBL_SCREEN).select("*").eq("id", TBL_BACKTEST_CACHE_ID).execute()
        if r.data:
            return json.loads(r.data[0]["results"])
    except: pass
    return {}

# ══════════════════════════════════════════
# [G] 포트폴리오 레벨 리스크 관리
#   — 20년차 퀀트 리뷰에서 지적된 최대 약점(종목 단위 리스크만 있고 계좌 단위가 없음) 보완.
#   여기 있는 값들은 전부 "가상 기준값"이며 실계좌 자동매매를 의미하지 않습니다.
# ══════════════════════════════════════════
VIRTUAL_TOTAL_CAPITAL   = 10_000_000   # 가상 총자본(원) — 모의 포트폴리오/백테스트 계산 기준값
RISK_PER_TRADE_PCT      = 0.01         # 트레이드당 위험률 1% (손절 시 최대 손실 = 총자본의 1%)
MAX_CONCURRENT_HOLDINGS = 10           # 동시 보유 종목 수 상한 (한 바구니에 몰빵 방지)
MAX_POSITION_PCT        = 0.25         # 한 종목에 총자본의 25% 넘게 못 태움 (저변동성 종목 과대편입 방지)
CORR_LOOKBACK_DAYS      = 40           # 상관관계 계산에 쓰는 최근 거래일수
CORR_BLOCK_THRESHOLD    = 0.75         # 이 값 이상이면 "사실상 같은 베팅"으로 보고 신규진입 skip
MAX_HOLDINGS_PER_SECTOR = 3            # 동시보유(10) 중 한 업종에 최대 이만큼만 (섹터 쏠림 방지)

# 체결비용(수수료+세금+슬리피지) — 지금까지 백테스트에 빠져 있던 부분.
# 국내주식 수수료(매수/매도 각 ~0.015%) + 매도 시 증권거래세(~0.18%) + "돌파를 쫓아 사는"
# 전략 특성상 슬리피지를 편도 0.1~0.15%p 추가로 가정한 근사치입니다.
ENTRY_COST_PCT = 0.15   # 매수 체결비용(수수료+슬리피지) — %p
EXIT_COST_PCT  = 0.35   # 매도 체결비용(수수료+거래세+슬리피지) — %p


def calc_position_size(entry_price: float, stop_price: float,
                        total_capital: float = VIRTUAL_TOTAL_CAPITAL,
                        risk_pct: float = RISK_PER_TRADE_PCT,
                        max_position_pct: float = MAX_POSITION_PCT) -> dict:
    """
    리스크 기반(ATR) 포지션 사이징.
    - 1주당 리스크(entry-stop) 기준으로, 이 트레이드가 손절 맞아도 총자본의 risk_pct%만
      잃도록 수량을 정한다 (변동성 큰 종목은 수량이 자동으로 줄고, 변동성 낮은 종목은 늘어남).
    - 다만 변동성이 너무 낮아 수량이 과도하게 커지는 걸 막기 위해, max_position_pct로
      포지션 "금액" 상한도 같이 걸어서 한 종목에 자본이 과도하게 쏠리는 걸 막는다.
    """
    per_share_risk = max(entry_price - stop_price, 0)
    if per_share_risk <= 0 or entry_price <= 0:
        return {"quantity": 0, "position_value": 0, "risk_amount": 0, "capped_by": None}

    risk_budget = total_capital * risk_pct
    qty_by_risk = int(risk_budget // per_share_risk)

    max_position_value = total_capital * max_position_pct
    qty_by_cap = int(max_position_value // entry_price)

    if qty_by_risk <= qty_by_cap:
        quantity, capped_by = qty_by_risk, None
    else:
        quantity, capped_by = qty_by_cap, "max_position_pct"

    quantity = max(quantity, 0)
    position_value = quantity * entry_price
    risk_amount = quantity * per_share_risk
    return {
        "quantity": quantity,
        "position_value": round(position_value),
        "risk_amount": round(risk_amount),
        "capped_by": capped_by,
    }


def is_correlated_with_holdings(supabase, candidate_symbol: str, holding_symbols: list,
                                 lookback: int = CORR_LOOKBACK_DAYS,
                                 threshold: float = CORR_BLOCK_THRESHOLD) -> tuple:
    """
    포트폴리오 집중 리스크 방지용 필터.
    후보 종목이 이미 보유 중인 종목과 최근 일간수익률 상관관계가 threshold 이상이면
    "사실상 같은 베팅"(같은 섹터/테마일 가능성 높음)으로 보고 (True, 유사종목) 반환.
    DB에 섹터 태그가 없어서, 가격 움직임 상관관계로 같은 테마 여부를 대신 판별하는 방식입니다.
    """
    if not holding_symbols:
        return False, None
    df_c = load_price_from_db(supabase, candidate_symbol)
    if df_c.empty or len(df_c) < lookback + 1:
        return False, None
    ret_c = df_c["Close"].pct_change().tail(lookback)

    for h_sym in holding_symbols:
        if h_sym == candidate_symbol:
            continue
        df_h = load_price_from_db(supabase, h_sym)
        if df_h.empty or len(df_h) < lookback + 1:
            continue
        ret_h = df_h["Close"].pct_change().tail(lookback)
        joined = pd.concat([ret_c, ret_h], axis=1, join="inner").dropna()
        if len(joined) < lookback * 0.6:
            continue
        corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
        if corr is not None and corr >= threshold:
            return True, h_sym
    return False, None


# ══════════════════════════════════════════
# [G-1] 업종(섹터) 캐시 — 상관관계 프록시를 "진짜 업종 태그" 기반으로 보강
#   fdr.StockListing()이 기본으로 Sector/Industry를 주기 때문에 별도 API 호출 없이,
#   이미 매일 부르는 load_filtered_universe() 결과에 묻어와서 그대로 저장하면 된다.
# ══════════════════════════════════════════
def save_sector_cache(supabase, universe: pd.DataFrame):
    """universe(load_filtered_universe 결과)에 실려온 Sector/Industry를 stock_sector에 upsert."""
    if "Sector" not in universe.columns:
        return 0
    ts = now_kst_str()
    rows = []
    for _, row in universe.iterrows():
        sector = str(row.get("Sector") or "").strip()
        if not sector or sector.lower() == "nan":
            continue
        rows.append({
            "symbol": row["Symbol"], "name": row.get("Name", row["Symbol"]),
            "sector": sector, "industry": str(row.get("Industry") or "").strip(),
            "updated_at": ts,
        })
    if not rows:
        return 0
    try:
        # 대량 upsert — 한 번에 너무 많이 보내면 실패할 수 있어 500개 단위로 분할
        for i in range(0, len(rows), 500):
            supabase.table(TBL_SECTOR).upsert(rows[i:i+500]).execute()
        return len(rows)
    except Exception:
        return 0


def load_sector_map(supabase) -> dict:
    """{symbol: sector} 딕셔너리로 로드 — 배치 1회 실행당 한 번만 불러서 재사용."""
    try:
        res = supabase.table(TBL_SECTOR).select("symbol,sector").execute()
        return {r["symbol"]: r["sector"] for r in res.data if r.get("sector")}
    except Exception:
        return {}


def is_sector_concentrated(candidate_symbol: str, sector_map: dict, holding_symbols: list,
                            max_per_sector: int = MAX_HOLDINGS_PER_SECTOR) -> tuple:
    """
    후보 종목의 업종이, 이미 보유 중인 종목들 중 같은 업종 개수가 상한(max_per_sector)에
    도달했으면 (True, 업종명)을 반환 — 섹터 단위 쏠림 리스크 방지.
    업종 정보가 없는 종목은 이 필터를 통과시키고 is_correlated_with_holdings()에 판단을 맡긴다
    (섹터 태그 유무와 무관하게 항상 최소한의 분산 체크가 걸리도록 하기 위함).
    """
    cand_sector = sector_map.get(candidate_symbol)
    if not cand_sector:
        return False, None
    same_sector_count = sum(1 for h in holding_symbols if sector_map.get(h) == cand_sector)
    if same_sector_count >= max_per_sector:
        return True, cand_sector
    return False, None
