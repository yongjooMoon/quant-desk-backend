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
from datetime import date  # 파일 상단 import에 없으면 추가

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
TBL_FUNDA_HISTORY = "stock_fundamental_history"
TBL_FUNDA_QUARTERLY = "stock_fundamental_quarterly"
TBL_TREND = "stock_trend_stats"

QUARTER_REPRT_CODES = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
QUARTERLY_ROUTINE_LOOKBACK = 2   # 매주 확인할 분기 개수 (최신 + 버퍼 1개)

TREND_MIN_BARS = 264  # 200일선 + ma200_3m_ago(63일 전 시점의 200일선) 계산까지 안전하게 되려면
                       # 최소 200+63+1=264행 필요. 210으로는 ma200_trend_score의 두 체크
                       # (1개월/3개월 전 200일선)가 전부 None이 되어 _pass_ratio가 "평가 불가"를
                       # "0점(하락 추세)"으로 잘못 등급 매기는 문제가 있었음. 264 미만 종목은
                       # compute_trend_stats_from_closes가 기존처럼 None을 반환해 이번 배치는
                       # 건너뛰고, 데이터가 쌓이면 자동으로 다음 배치부터 포함된다(신규 로직 아님,
                       # 기존 "데이터 부족 스킵" 경로를 그대로 탐).

ROLLING_DAYS   = 720   # stock_daily 보관 일수 (721일째 되는 옛날 일봉부터 삭제)
FUNDA_TTL_SEC  = 86400 * 90
PREFILTER_MARCAP_억 = 1500
PREFILTER_TVOL_억   = 50

# 확정/워치리스트 필터 기준 (절대 기준)
CONFIRM_FILTER_MIN  = 6     # 6개 강력한 추격매수 조건 ALL PASS
WATCHLIST_FILTER_MIN= 4     # 최소 4개 이상 통과시 관심종목

DART_ACCOUNT_ID_MAP = {
    "revenue":   ["ifrs-full_Revenue", "ifrs_Revenue", "dart_Revenue"],
    "op_profit": ["dart_OperatingIncomeLoss", "ifrs-full_OperatingIncomeLoss"],
    "net_income": ["ifrs-full_ProfitLoss"],  # 지배/비지배 합산 총 당기순이익
    "eps":       ["ifrs-full_BasicEarningsLossPerShare"],
}
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

KOSPI_2ND_BUY_MDD_THRESHOLD_PCT = -20.0  # [추가] 2차 매수 국면 판단 임계값 — 단일 소스

def get_market_regime(lookback_days: int = 400) -> dict:  # [수정] 252거래일 고점 확보 위해 300→400
    """
    코스피 지수 기반 시장 레짐(국면) 판정
    - BULL    : 종가가 120일선 위 + 120일선 자체가 상승 중
    - BEAR    : 종가가 120일선 아래 + 120일선 자체가 하락 중
    - NEUTRAL : 그 외 (박스권 / 전환 구간) → 보수적으로 중립 취급

    [추가] kospi_mdd / is_2nd_buy_regime
      - 52주(252거래일) 고점 대비 코스피 현재가 하락률
      - -20% 이하로 빠졌으면 "2차 매수(불타기/물타기) 국면"으로 판정 — 종목 단위가 아닌
        시장 전체에 적용되는 단일 플래그. 스크리너의 모든 종목 카드가 이 값을 공유한다.
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
        prev_ma120 = float(ma120.iloc[-20])

        above_ma  = curr_close > curr_ma120
        ma_rising = curr_ma120 > prev_ma120

        if above_ma and ma_rising:
            regime = "BULL"
        elif (not above_ma) and (not ma_rising):
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        # [추가] 52주(252거래일) 고점 대비 하락률
        year_window = close.tail(252) if len(close) >= 252 else close
        year_high = float(year_window.max())
        kospi_mdd = round((curr_close - year_high) / year_high * 100, 2)

        return {
            "regime": regime,
            "kospi_close": round(curr_close, 2),
            "ma120": round(curr_ma120, 2),
            "ma120_slope_pct": round((curr_ma120 / prev_ma120 - 1) * 100, 2),
            "kospi_year_high": round(year_high, 2),
            "kospi_mdd": kospi_mdd,
            "is_2nd_buy_regime": bool(kospi_mdd <= KOSPI_2ND_BUY_MDD_THRESHOLD_PCT),
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
    result = {"roe": None, "debt_ratio": None, "op_profit": None, "net_income": None,
              "revenue": None, "eps": None, "period_type": None}  # ← 추가: 어떤 걸로 채웠는지 표시
    for reprt_code in ["11011", "11012"]:
        try:
            res = requests.get(url, params={"crtfc_key": dart_api_key, "corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": "CFS"}, timeout=10).json()
            if res.get("status") != "000": continue
            for item in res.get("list", []):
                acnt = item.get("account_nm", "")
                val_raw = _parse_num(item.get("thstrm_amount", "0"))
                if val_raw is None:
                    continue
                val_억 = val_raw / 1e8
                if "영업이익" in acnt and "영업이익률" not in acnt and result["op_profit"] is None: result["op_profit"] = val_억
                if "당기순이익" in acnt and result["net_income"] is None: result["net_income"] = val_억
                if "매출액" in acnt and result["revenue"] is None: result["revenue"] = val_억
                if "ROE" in acnt and result["roe"] is None: result["roe"] = val_raw
                if "부채비율" in acnt and result["debt_ratio"] is None: result["debt_ratio"] = val_raw
                if "주당순이익" in acnt and "희석" not in acnt and result["eps"] is None:
                    result["eps"] = val_raw
            result["period_type"] = "annual" if reprt_code == "11011" else "half_year"  # ← 추가
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

def load_fundamental_raw(supabase, symbol: str) -> dict | None:
    """
    TTL(90일) 체크 없이 캐시된 펀더멘털을 그대로 반환한다.
    load_fundamental_from_db()는 90일 롤링 TTL로만 신선도를 판단하는데,
    스크리너는 "분기 법정공시기한이 지났는가"라는 더 정확한 기준으로 판단해야 하므로
    여기서는 만료 여부를 따지지 않고 원본 + updated_at만 그대로 돌려준다.
    실제 신선도 판정은 호출부(quant_cron._needs_fundamental_refresh)가 한다.
    """
    try:
        res = supabase.table(TBL_FUNDA).select("*").eq("symbol", symbol).execute()
        if res.data:
            return res.data[0]
    except Exception:
        pass
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
        cur = fetch_dart_financial(c_code, dart_api_key, now_kst().year - 1)
        prev = fetch_dart_financial(c_code, dart_api_key, now_kst().year - 2)

        # [추가] 기간 단위가 다르면(연간 vs 반기) 신뢰 불가 → 통째로 버리고 네이버 폴백에 맡김
        period_mismatch = (
            cur.get("period_type") and prev.get("period_type")
            and cur["period_type"] != prev["period_type"]
        )
        if period_mismatch:
            print(f"  [!] {symbol} DART 기간 불일치 (cur={cur['period_type']}, prev={prev['period_type']}) → 이번 값은 폐기, 네이버로 폴백")
        else:
            data.update({"op_profit_cur": cur.get("op_profit"), "op_profit_prev": prev.get("op_profit"),
                         "net_income_cur": cur.get("net_income"), "net_income_prev": prev.get("net_income"),
                         "revenue_cur": cur.get("revenue"), "revenue_prev": prev.get("revenue"),
                         "roe": cur.get("roe"), "debt_ratio": cur.get("debt_ratio"),
                         "eps_cur": cur.get("eps"), "eps_prev": prev.get("eps")})

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

    # [수정] VCP(변동성 축소) 팩터 계산을 위해 10일, 50일 단/장기 변동성 산출
    atr10 = tr.rolling(10).mean().iloc[-1]
    atr20 = tr.rolling(20).mean().iloc[-1]
    atr50 = tr.rolling(50).mean().iloc[-1]

    metrics["dynamic_mdd_limit"] = max(-40.0, min(-15.0, -((atr20 / close.iloc[-1]) * 300)))  # ATR의 3배수

    # [수정] 단기 변동성(10일)이 장기 변동성(50일) 대비 얼마나 축소되었는가 측정
    metrics["atr_contraction_ratio"] = atr10 / atr50 if atr50 and atr50 > 0 else 1.0

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

def evaluate_gates(metrics: dict, curr_price: float, prev_price: float, today_vol: float) -> dict:
    """6대 매수 관문(성장/MDD/유동성/추세/돌파/수급) 판정 — 이 시스템의 유일한 판정 로직.
    run_screening_from_db, evaluate_entry_gates, quant_backTesting.strategy_filters가 전부 이걸 호출한다."""
    f_growth = bool(metrics["growth_composite"] > 0)
    f_mdd = bool(metrics["mdd"] >= metrics["dynamic_mdd_limit"])
    f_liq = bool(metrics["liquidity_20d"] >= 50)
    f_trend = bool((curr_price > metrics["ma20"]) and (metrics["ma20"] > metrics["ma60"])
                   and (curr_price <= metrics["ma20"] * (1 + metrics["dynamic_overext_limit_pct"] / 100)))

    breakout_threshold = metrics["high_60d"] * 0.90

    # [수정] 단순 고점 근접이 아닌, VCP(변동성 축소) 셋업 확인 (최근 10일 변동성이 50일 대비 90% 이하로 수렴)
    vcp_setup = metrics.get("atr_contraction_ratio", 1.0) <= 0.90

    # 진정한 돌파는 변동성이 축소(vcp_setup)된 상태에서 발생해야 함
    f_break_confirmed = bool(vcp_setup and (curr_price >= breakout_threshold) and (prev_price >= breakout_threshold))
    f_break_strong_day1 = bool(vcp_setup and (curr_price >= breakout_threshold) and (
                today_vol > (metrics["vol_60d"] * STRONG_BREAKOUT_VOL_MULT)))
    f_break = f_break_confirmed or f_break_strong_day1

    f_vol = bool(
        (metrics["vol_5d"] > (metrics["vol_60d"] * 1.5)) and (today_vol > (metrics["vol_60d"] * VOL_TODAY_SURGE_MULT)))

    pass_count = int(sum([f_growth, f_mdd, f_liq, f_trend, f_break, f_vol]))
    high_60d_val = metrics["high_60d"] if metrics["high_60d"] > 0 else 1
    vol_60d_val = metrics["vol_60d"] if metrics["vol_60d"] > 0 else 1

    gates = {
        "growth": {"pass": f_growth, "label": "Growth Composite",
                   "reason": f"Comp {metrics['growth_composite']:+.1f}%"},
        "mdd": {"pass": f_mdd, "label": "Dynamic MDD",
                "reason": f"MDD {metrics['mdd']:.1f}% (Limit: {metrics['dynamic_mdd_limit']:.1f}%)"},
        "liq": {"pass": f_liq, "label": "Liquidity", "reason": f"{metrics['liquidity_20d']:.0f}억"},
        "trend": {"pass": f_trend, "label": "Trend Alignment",
                  "reason": f"Price > 20MA > 60MA, 동적 과열캡 {metrics['dynamic_overext_limit_pct']:.1f}% 이내"},
        "break": {"pass": f_break, "label": "Price Breakout",
                  "reason": (
                      f"VCP수렴({(metrics.get('atr_contraction_ratio', 1) * 100):.0f}%) & 1일차 강한돌파" if f_break_strong_day1
                      else f"VCP수렴({(metrics.get('atr_contraction_ratio', 1) * 100):.0f}%) & 2일연속 돌파권" if f_break_confirmed
                      else "VCP 수렴 미달 또는 고점 이탈")},
        "vol": {"pass": f_vol, "label": "Volume Surge",
                    "reason": f"Vol {(metrics['vol_5d']/vol_60d_val):.1f}x 급증 (당일 거래량도 {VOL_TODAY_SURGE_MULT}x 이상)"},
    }
    return {"pass_count": pass_count, "gates": gates,
            "f_growth": f_growth, "f_mdd": f_mdd, "f_liq": f_liq,
            "f_trend": f_trend, "f_break": f_break, "f_vol": f_vol}
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
        g = evaluate_gates(metrics, curr_price, prev_price, today_vol)
        pass_count = g["pass_count"]
        f_growth, f_mdd, f_liq, f_trend, f_break, f_vol = g["f_growth"], g["f_mdd"], g["f_liq"], g["f_trend"], g[
            "f_break"], g["f_vol"]

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
                "Growth Composite": {"pass": f_growth, "reason": g["gates"]["growth"]["reason"]},
                "Dynamic MDD": {"pass": f_mdd, "reason": g["gates"]["mdd"]["reason"]},
                "Liquidity": {"pass": f_liq, "reason": g["gates"]["liq"]["reason"]},
                "Trend Alignment": {"pass": f_trend, "reason": g["gates"]["trend"]["reason"]},
                "Price Breakout": {"pass": f_break, "reason": g["gates"]["break"]["reason"]},
                "Volume Surge": {"pass": f_vol, "reason": g["gates"]["vol"]["reason"]}
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
# [H] 시장 레짐 캐시 (id=15) — 1차/2차 매수 국면 판정용, API가 매번 fdr을 호출하지 않도록 저장
# ══════════════════════════════════════════
MARKET_REGIME_CACHE_ID = 15

def save_market_regime_cache(supabase, regime_data: dict):
    ts = now_kst_str()
    supabase.table(TBL_SCREEN).upsert([
        {"id": MARKET_REGIME_CACHE_ID, "results": json.dumps(regime_data, ensure_ascii=False, cls=NumpyEncoder), "updated_at": ts}
    ]).execute()

def load_market_regime_cache(supabase) -> dict:
    try:
        r = supabase.table(TBL_SCREEN).select("*").eq("id", MARKET_REGIME_CACHE_ID).execute()
        if r.data:
            return json.loads(r.data[0]["results"])
    except Exception:
        pass
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

def evaluate_exit_signal(entry_price: float, entry_date, df: pd.DataFrame, regime: str) -> dict:
    """
    포지션 청산 판정 — 트레일링/동적손절(ATR) → 추세붕괴(레짐별 문턱) → 모멘텀소진(이익보호모드)
    순으로 검사한다. quant_backTesting.strategy_check_exit()와 quant_cron.process_virtual_
    portfolio() 양쪽이 반드시 이 함수 하나만 호출해야 한다 (evaluate_gates()와 동일한
    "단일 소스" 원칙 — 파라미터를 하나 바꿀 때 두 파일을 동시에 고쳐야 하는 위험을 제거).

    df: entry_date 이후 미래 정보가 섞이면 안 된다.
        - 백테스트: slice_upto(df, t)로 look-ahead 방지된 슬라이스를 넘길 것
        - 라이브: 오늘까지의 전체 df를 그대로 넘기면 됨 (자연히 오늘이 마지막 행이라 안전)
    """
    close = df["Close"]
    vol = df["Volume"]
    curr_price = float(close.iloc[-1])

    atr_mult = REGIME_ATR_MULT.get(regime, 2.5)
    risk_cap = REGIME_RISK_CAP.get(regime, 0.15)
    trend_break_min = REGIME_TREND_BREAK_MIN.get(regime, 2)

    df_held = df[df.index >= entry_date]
    highest_close = float(df_held["Close"].max()) if not df_held.empty else curr_price

    high, low, prev_close = df.get("High", close), df.get("Low", close), close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr20 = tr.rolling(20).mean().iloc[-1]

    ret = ((curr_price - entry_price) / entry_price) * 100
    profit_locking = ret >= PROFIT_LOCK_TRIGGER_PCT
    effective_atr_mult = (atr_mult * PROFIT_LOCK_ATR_MULT_FACTOR) if profit_locking else atr_mult

    initial_risk = min(risk_cap, (atr_mult * atr20) / entry_price) if pd.notna(atr20) else risk_cap
    initial_stop = entry_price * (1 - initial_risk)
    trailing_stop = (highest_close - effective_atr_mult * atr20) if pd.notna(atr20) else initial_stop
    current_stop = max(initial_stop, trailing_stop)

    ma10 = close.iloc[-10:].mean()
    ma20 = close.iloc[-20:].mean()

    # [수정] 50일선 계산 추가 (데이터 부족시 20일선으로 대체)
    ma50 = close.iloc[-50:].mean() if len(close) >= 50 else ma20

    # [수정] 3중 다수결 득점제 폐지 -> 절대 생명선 이탈 기준으로 변경
    # 미너비니 원칙: 약세장(BEAR)에선 20일선 이탈 시 즉시 도망, 강/중립장에선 50일선 이탈 시 절대 청산
    trend_broken = (curr_price < ma20) if regime == "BEAR" else (curr_price < ma50)

    # 기존 코드와의 UI 호환성(에러 방지)을 위해 score 값을 0 또는 3으로 직관적 매핑
    trend_break_score = 3 if trend_broken else 0

    vol_5d, vol_20d = vol.iloc[-5:].mean(), vol.iloc[-20:].mean()
    momentum_exhausted = profit_locking and (vol_5d < vol_20d * VOL_COOLING_RATIO) and (curr_price < ma10)

    should_sell, reason = False, ""
    if curr_price <= current_stop:
        should_sell, reason = True, "트레일링/동적손절 이탈 (ATR)"
    elif trend_broken:
        # [수정] 어떤 선을 깨고 나왔는지 명확히 리포팅
        broken_line = "20일선" if regime == "BEAR" else "50일선"
        should_sell, reason = True, f"추세 생명선({broken_line}) 이탈 (레짐 {regime})"
    elif momentum_exhausted:
        should_sell, reason = True, f"모멘텀 소진 익절 (누적 {ret:+.1f}%)"

    return {
        "should_sell": should_sell, "reason": reason, "return_pct": round(ret, 2),
        "current_stop": current_stop, "trend_break_score": trend_break_score,
        "profit_locking": profit_locking,
    }

def is_correlated_with_holdings_inmemory(price_data: dict, candidate_symbol: str, holding_symbols: list, t,
                                          lookback: int = CORR_LOOKBACK_DAYS,
                                          threshold: float = CORR_BLOCK_THRESHOLD) -> tuple:
    """
    is_correlated_with_holdings()의 백테스트 전용 버전.
    DB 재조회 없이, 이미 메모리에 로딩된 price_data(symbol -> DataFrame)를 t 시점까지
    슬라이스해서 상관관계를 계산한다. 백테스트가 250거래일 x 후보종목 수만큼 이 체크를
    반복해야 하므로, 매번 DB를 때리는 라이브용 함수를 그대로 쓰면 배치가 감당 못 한다.
    판정 기준(threshold, lookback)은 라이브와 완전히 동일한 상수를 공유한다.
    """
    if not holding_symbols:
        return False, None
    df_c = price_data.get(candidate_symbol)
    if df_c is None:
        return False, None
    c_slice = df_c[df_c.index <= t].tail(lookback + 1)
    if len(c_slice) < lookback + 1:
        return False, None
    ret_c = c_slice["Close"].pct_change().dropna()

    for h_sym in holding_symbols:
        if h_sym == candidate_symbol:
            continue
        df_h = price_data.get(h_sym)
        if df_h is None:
            continue
        h_slice = df_h[df_h.index <= t].tail(lookback + 1)
        if len(h_slice) < lookback + 1:
            continue
        ret_h = h_slice["Close"].pct_change().dropna()
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

def evaluate_entry_gates(df: pd.DataFrame, fund: dict, benchmark_ret_60d: float = 0.0) -> dict:
    """단일 종목 실시간 6관문 판정 (UI 검색용) — evaluate_gates()로 위임."""
    if df.empty or len(df) < 60:
        return None
    metrics = calc_quant_metrics(df, fund, benchmark_ret_60d=benchmark_ret_60d)
    if "ma20" not in metrics or metrics["ma20"] == 0:
        return None

    curr_price = int(df["Close"].iloc[-1])
    prev_price = int(df["Close"].iloc[-2]) if len(df) >= 2 else curr_price
    today_vol = df["Volume"].iloc[-1]

    g = evaluate_gates(metrics, curr_price, prev_price, today_vol)
    key_to_letter = {"growth": "A", "mdd": "B", "liq": "C", "trend": "D", "break": "E", "vol": "F"}
    gates = {key_to_letter[k]: {"name": v["label"], "pass": v["pass"], "reason": v["reason"]}
              for k, v in g["gates"].items()}

    return {"pass_count": g["pass_count"], "gates": gates, "metrics": metrics}

def backfill_fundamental_history(symbol: str, name: str, corp_code: str, dart_api_key: str, years: list) -> list:
    """DART API를 호출해서 과거 연도별 재무제표를 point-in-time 스냅샷 형태로 '조회'만 한다.
    DB에 쓰지 않는다 — 저장은 save_fundamental_history()가 담당.

    [수정] ROE/부채비율은 fnlttSinglAcntAll(재무제표 계정과목) 응답에 애초에 존재하지 않는
    필드였음 — 이 사실은 _dart_quarter_report()에서 이미 확인되어 명시돼 있음
    ("이 API에는 애초에 존재하지 않는 필드, 재무비율은 _dart_index_report가 담당").
    그런데 이 함수(backfill_fundamental_history)는 그 리팩터링에서 빠진 채, 여전히
    account_nm에서 "ROE"/"부채비율" 텍스트를 찾고 있어서 항상 None만 채워지고 있었음.
    → 해당 텍스트 스캔을 제거하고, _dart_index_report(fnlttSinglIndx 전용 API) 호출을
      추가해서 실제 값이 채워지도록 수정.
    """
    rows = []
    targets = [("11011", lambda y: f"{y+1}-03-31"), ("11012", lambda y: f"{y}-08-14")]
    for year in years:
        for reprt_code, known_from_fn in targets:
            try:
                res = requests.get(
                    "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                    params={"crtfc_key": dart_api_key, "corp_code": corp_code,
                            "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": "CFS"},
                    timeout=10,
                ).json()
            except Exception:
                continue
            if res.get("status") != "000":
                continue
            snap = {"op_profit": None, "net_income": None, "revenue": None, "roe": None, "debt_ratio": None}
            for item in res.get("list", []):
                acnt, val_raw = item.get("account_nm", ""), _parse_num(item.get("thstrm_amount", "0"))
                val_억 = (val_raw / 1e8) if val_raw is not None else None
                if "영업이익" in acnt and "영업이익률" not in acnt and snap["op_profit"] is None: snap["op_profit"] = val_억
                if "당기순이익" in acnt and snap["net_income"] is None: snap["net_income"] = val_억
                if "매출액" in acnt and snap["revenue"] is None: snap["revenue"] = val_억
                # ← ROE/부채비율 텍스트 스캔 삭제 (이 API엔 해당 필드가 없음, 아래에서 별도 조회)

            # [추가] ROE/부채비율은 재무비율 전용 API(fnlttSinglIndx)에서 조회
            idx = _dart_index_report(corp_code, dart_api_key, year, reprt_code)
            snap["roe"] = idx.get("roe")
            snap["debt_ratio"] = idx.get("debt_ratio")

            rows.append({"symbol": symbol, "name": name, "bsns_year": year, "reprt_code": reprt_code,
                         "known_from": known_from_fn(year), "updated_at": now_kst_str(), **snap})
    return rows

def save_fundamental_history(supabase, rows: list):
    """stock_fundamental_history 테이블에 실제로 저장(UPSERT)하는 함수. 여기서만 DB에 쓴다."""
    if rows:
        supabase.table(TBL_FUNDA_HISTORY).upsert(rows, on_conflict="symbol,bsns_year,reprt_code").execute()

def load_fundamental_history(supabase, symbol: str) -> list:
    """DB에 이미 저장된 스냅샷을 그대로 조회. 이게 비어있어야 DART 호출이 발생한다."""
    try:
        res = supabase.table(TBL_FUNDA_HISTORY).select("*").eq("symbol", symbol).execute()
        return res.data or []
    except Exception:
        return []

def get_fundamental_history_cached(supabase, symbol: str, name: str, corp_code: str,
                                    dart_api_key: str, years: list) -> list:
    """캐시(DB)에 없는 연도만 DART로 채우고 저장한 뒤 반환. 이미 있는 연도는 DART를 다시 안 부른다."""
    history = load_fundamental_history(supabase, symbol)                       # ① DB 조회
    have_years = {h["bsns_year"] for h in history if h.get("reprt_code") == "11011"}
    missing_years = [y for y in years if y not in have_years]
    if missing_years and corp_code:
        new_rows = backfill_fundamental_history(symbol, name, corp_code, dart_api_key, missing_years)  # ② DART 조회
        if new_rows:
            save_fundamental_history(supabase, new_rows)                       # ③ DB 저장 ← 실제 INSERT
            history.extend(new_rows)
    return history

def build_asof_fund_dict(history: list, asof_date) -> dict:
    """asof_date 시점에 이미 공시되어 있었을 연간 실적만으로 cur/prev 매핑.
    ⚠️ 수급(foreign/institute_net_buy)은 과거 재현 불가 → 0으로 중립화."""
    asof_str = asof_date.strftime("%Y-%m-%d") if hasattr(asof_date, "strftime") else str(asof_date)
    annuals = sorted(
        [h for h in history if h.get("reprt_code") == "11011" and h.get("known_from", "9999-99-99") <= asof_str],
        key=lambda h: h["bsns_year"]
    )
    if not annuals:
        return {}
    cur = annuals[-1]
    prev = annuals[-2] if len(annuals) >= 2 else {}
    return {
        "net_income_cur": cur.get("net_income"), "net_income_prev": prev.get("net_income"),
        "op_profit_cur": cur.get("op_profit"), "op_profit_prev": prev.get("op_profit"),
        "revenue_cur": cur.get("revenue"), "revenue_prev": prev.get("revenue"),
        "foreign_net_buy": 0, "institute_net_buy": 0,
    }

def _quarter_deadline(year: int, quarter: int) -> date:
    """분기별 법정공시기한(근사). 4분기(사업보고서)는 마감이 '다음해' 3/31."""
    if quarter == 1: return date(year, 5, 15)
    if quarter == 2: return date(year, 8, 14)
    if quarter == 3: return date(year, 11, 14)
    return date(year + 1, 3, 31)


def latest_confirmed_quarter(asof: date = None) -> tuple:
    """asof 기준 이미 법정기한이 지나서 '존재할 수 있는' 가장 최근 (year, quarter)."""
    asof = asof or now_kst().date()
    y = asof.year
    for q in [4, 3, 2, 1]:
        year = y - 1 if q == 4 else y
        if _quarter_deadline(year, q) <= asof:
            return (year, q)
    return (y - 1, 4)

QUARTERLY_RETRY_AFTER_DAYS = 14  # 핵심 필드가 채워진 분기라도 이 기간이 지나면 한 번 더 재조회
                                  # (DART가 나중에 보완공시로 값을 채우는 경우가 있어, 최초 조회
                                  # 시점의 결측을 영구 확정으로 취급하지 않는다)

def _quarterly_row_needs_retry(row: dict, retry_after_days: int = QUARTERLY_RETRY_AFTER_DAYS) -> bool:
    """
    기존에 저장된 분기 row가 이번 배치에서 재시도 대상인지 판단.
    [수정 이유] 기존 backfill_quarterly_fundamental()은 "이 (연도,분기) row가 DB에 존재하는가"
    만으로 스킵 여부를 정했다. 그런데 _quarter_standalone()은 4개 필드(revenue/op_profit/
    net_income/eps)가 전부 None일 때만 저장을 포기하고, 일부만 채워진 채로도 그대로 upsert된다.
    그 결과 "revenue만 있고 net_income은 없는" row가 한 번 생기면, 존재 여부만 보는 기존
    로직은 그 분기를 영원히 다시 조회하지 않았다 — 이번 배치의 일시적 API 오류였는지,
    실제로 그 분기 데이터가 없는지 구분이 안 된 채로 결측이 고정되는 버그였다.
    - 핵심 필드(revenue_q, net_income_q)가 둘 다 비어있으면 → 완전 결측이므로 항상 재시도
    - 핵심 필드가 있어도 updated_at이 retry_after_days 이상 지났으면 → 한 번 더 재시도
      (DART 보완공시 대응. 그 전에는 매일 재조회하지 않도록 최소한의 TTL을 둠)
    """
    core_missing = row.get("revenue_q") is None and row.get("net_income_q") is None
    if core_missing:
        return True
    return is_expired(row.get("updated_at", ""), retry_after_days * 86400)


def backfill_quarterly_fundamental(supabase, universe_df: pd.DataFrame, dart_api_key: str,
                                    dart_corp_map: dict, lookback_quarters: int = QUARTERLY_ROUTINE_LOOKBACK,
                                    log_fn=print) -> int:
    """
    종목마다: ① DB에 이미 있는 분기 조회 → ② 없거나(결측/오래됨) 재시도 대상인 분기만 DART 호출
    → ③ 있으면 insert, 없으면 넘어감.
    DART 실패/API 소진 시 그냥 pass하고 다음 종목으로 진행 (다음 배치에서 자동 재시도됨).

    [수정] 존재 여부(bsns_year,quarter)만 보던 have 집합을 _quarterly_row_needs_retry() 기반
    판정으로 교체 — 일부 필드만 채워진 채 영구 고정되던 결측을 다음 배치들에서 다시 채울 기회를
    준다.
    """
    latest_y, latest_q = latest_confirmed_quarter()
    target_quarters = []
    y, q = latest_y, latest_q
    for _ in range(lookback_quarters):
        target_quarters.append((y, q))
        q -= 1
        if q == 0:
            q, y = 4, y - 1

    total, filled, skipped, no_corp = len(universe_df), 0, 0, 0
    for i, (_, row) in enumerate(universe_df.iterrows()):
        symbol, name = row["Symbol"], row.get("Name", row["Symbol"])
        corp_code = dart_corp_map.get(symbol)
        if not corp_code:
            no_corp += 1
            continue

        # ① DB 먼저 조회 — 재시도 판단에 필요한 필드까지 함께 select
        try:
            existing = supabase.table(TBL_FUNDA_QUARTERLY) \
                .select("bsns_year,quarter,revenue_q,net_income_q,updated_at") \
                .eq("symbol", symbol).execute()
            complete = {
                (r["bsns_year"], r["quarter"])
                for r in existing.data
                if not _quarterly_row_needs_retry(r)
            }
        except Exception:
            complete = set()

        # ② "완전히 채워져 있고 재시도 기간도 안 지난" 분기만 스킵 대상
        missing = [(yy, qq) for (yy, qq) in target_quarters if (yy, qq) not in complete]
        if not missing:
            skipped += 1
            continue

        # 같은 종목 내에서 연속 분기를 채울 때 cur/prev 요청이 겹치는 걸 줄이기 위한 메모 캐시
        # (예: 2분기 계산의 prev=1분기 요청이 1분기 계산의 cur 요청과 같은 API 호출) —
        # API 호출 절감은 결측을 줄이는 데도 직접 도움이 됨(레이트리밋/타임아웃으로 인한
        # 결측이 실제 원인 중 하나였음).
        report_cache = {}

        rows_to_upsert = []
        for (yy, qq) in missing:
            try:
                vals = _quarter_standalone(corp_code, dart_api_key, yy, qq, report_cache=report_cache)
            except Exception:
                continue  # API 소진/오류 → 이 분기만 pass, 다음 배치에서 재시도
            if not vals:
                continue
            rows_to_upsert.append({
                "symbol": symbol, "name": name, "bsns_year": yy, "quarter": qq,
                "reprt_code": QUARTER_REPRT_CODES[qq],
                "revenue_q": vals.get("revenue"), "op_profit_q": vals.get("op_profit"),
                "net_income_q": vals.get("net_income"), "eps_q": vals.get("eps"),
                "roe": vals.get("roe"), "debt_ratio": vals.get("debt_ratio"),
                "current_ratio": vals.get("current_ratio"), "interest_coverage": vals.get("interest_coverage"),
                "known_from": _quarter_deadline(yy, qq).strftime("%Y-%m-%d"),
                "updated_at": now_kst_str(),
            })

        # ③ 있으면 insert, 없으면 그냥 다음 종목으로
        if rows_to_upsert:
            try:
                supabase.table(TBL_FUNDA_QUARTERLY).upsert(
                    rows_to_upsert, on_conflict="symbol,bsns_year,quarter"
                ).execute()
                filled += len(rows_to_upsert)
            except Exception as e:
                log_fn(f"    [!] {name}({symbol}) 저장 실패: {e}")

        if (i + 1) % 200 == 0 or (i + 1) == total:
            log_fn(f"    [{i+1}/{total}] 처리중... (신규/재시도 {filled}건, 완결스킵 {skipped}종목, corp_code없음 {no_corp}종목)")

    log_fn(f"  [✓] 분기 펀더멘털 백필 완료 — 신규/재시도 {filled}건 / 완결스킵 {skipped}종목 / corp_code없음 {no_corp}종목")
    return filled


def _parse_dart_accounts(item_list: list) -> dict:
    """
    DART fnlttSinglAcntAll의 list 원본을 표준 필드(revenue/op_profit/net_income/eps)로 변환.
    1순위: account_id(XBRL 표준 태그) 매칭 — 라벨 변형에 흔들리지 않음
    2순위: account_nm 텍스트 매칭 — account_id가 없는 구버전 공시 대비 폴백
    3곳(fetch_dart_financial / backfill_fundamental_history / _dart_quarter_report)에
    흩어져 있던 동일 로직을 이 함수 하나로 통합 — 앞으로는 여기만 고치면 됨.
    """
    out = {"revenue": None, "op_profit": None, "net_income": None, "eps": None}

    # 1순위: account_id
    for item in item_list:
        acc_id = item.get("account_id", "")
        val_raw = _parse_num(item.get("thstrm_amount", "0"))
        if val_raw is None or not acc_id:
            continue
        for field, id_candidates in DART_ACCOUNT_ID_MAP.items():
            if out[field] is not None:
                continue
            if acc_id in id_candidates:
                out[field] = val_raw if field == "eps" else val_raw / 1e8

    # 2순위: 텍스트 폴백 (account_id로 못 채운 필드만)
    for item in item_list:
        acnt = item.get("account_nm", "")
        val_raw = _parse_num(item.get("thstrm_amount", "0"))
        if val_raw is None:
            continue
        if out["op_profit"] is None and "영업이익" in acnt and "영업이익률" not in acnt:
            out["op_profit"] = val_raw / 1e8
        if out["net_income"] is None and ("당기순이익" in acnt or "반기순이익" in acnt or "분기순이익" in acnt):
            out["net_income"] = val_raw / 1e8
        if out["revenue"] is None and ("매출액" in acnt or acnt.strip() == "수익(매출액)"):
            out["revenue"] = val_raw / 1e8
        if out["eps"] is None and "주당순이익" in acnt and "희석" not in acnt:
            out["eps"] = val_raw

    return out


def _dart_quarter_report(corp_code: str, dart_api_key: str, year: int, reprt_code: str,
                          report_cache: dict = None) -> dict:
    """
    DART 재무제표(fnlttSinglAcntAll) 조회.
    [수정 1] CFS 실패/빈값 시 OFS(개별재무제표)로 폴백 — 연결재무제표 미제출 기업 데이터 누락 방지
    [수정 2] account_id 우선 매칭으로 교체 (_parse_dart_accounts)
    [수정 3] ROE/부채비율/유동비율/이자보상배율 스캔 로직 완전 제거
             → 이 API에는 애초에 존재하지 않는 필드였음 (재무비율은 _dart_index_report가 담당)
    [수정 4] report_cache: {(corp_code, year, reprt_code): result} 형태의 메모 캐시.
             연속 분기 백필 시 이번 분기의 cur 요청이 다음 분기의 prev 요청과 동일한
             (corp_code, year, reprt_code)를 다시 부르는 중복이 있었음 — 캐시로 API 호출을
             줄여 레이트리밋/타임아웃으로 인한 결측을 완화한다.
    """
    cache_key = (corp_code, year, reprt_code)
    if report_cache is not None and cache_key in report_cache:
        return report_cache[cache_key]

    result = {"revenue": None, "op_profit": None, "net_income": None, "eps": None}
    for fs_div in ("CFS", "OFS"):
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                params={"crtfc_key": dart_api_key, "corp_code": corp_code,
                        "bsns_year": str(year), "reprt_code": reprt_code, "fs_div": fs_div},
                timeout=10,
            ).json()
        except Exception:
            continue
        if res.get("status") != "000" or not res.get("list"):
            continue
        result = _parse_dart_accounts(res["list"])
        if any(v is not None for v in result.values()):
            break

    if report_cache is not None:
        report_cache[cache_key] = result
    return result


def _dart_index_report(corp_code: str, dart_api_key: str, year: int, reprt_code: str,
                        report_cache: dict = None) -> dict:
    """
    ROE/부채비율/유동비율/이자보상배율 조회.
    [확인됨] debt_ratio="부채비율", current_ratio="유동비율" 라벨 매칭 정상 동작 중.
    [수정] roe 라벨이 "자기자본순이익률"이 아니라 그냥 "ROE"임 — 실측 확인 완료.
    [주의] interest_coverage는 이자비용이 미미한 우량기업(삼성전자 등)은
           DART가 아예 계산하지 않고 None으로 옴 — 정상 동작, 코드 문제 아님.
    [추가] report_cache — _dart_quarter_report와 동일한 목적의 메모 캐시(별도 네임스페이스로 저장).
    """
    cache_key = ("idx", corp_code, year, reprt_code)
    if report_cache is not None and cache_key in report_cache:
        return report_cache[cache_key]

    ratios = {"roe": None, "debt_ratio": None, "current_ratio": None, "interest_coverage": None}
    for idx_cl_code in ("M210000", "M220000"):
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/fnlttSinglIndx.json",
                params={"crtfc_key": dart_api_key, "corp_code": corp_code,
                        "bsns_year": str(year), "reprt_code": reprt_code,
                        "idx_cl_code": idx_cl_code},
                timeout=10,
            ).json()
            if res.get("status") != "000":
                continue
            for item in res.get("list", []):
                nm = item.get("idx_nm", "").strip()
                val = _parse_num(item.get("idx_val", ""))
                if val is None:
                    continue
                if nm == "ROE" and ratios["roe"] is None:
                    ratios["roe"] = val
                elif nm == "부채비율" and ratios["debt_ratio"] is None:
                    ratios["debt_ratio"] = val
                elif nm == "유동비율" and ratios["current_ratio"] is None:
                    ratios["current_ratio"] = val
                elif nm in ("이자보상배율", "순이자보상배율") and ratios["interest_coverage"] is None:
                    ratios["interest_coverage"] = val
        except Exception:
            pass

    if report_cache is not None:
        report_cache[cache_key] = ratios
    return ratios


def _quarter_standalone(corp_code: str, dart_api_key: str, year: int, quarter: int,
                         report_cache: dict = None) -> dict:
    """
    분기 단독값 = 해당분기 누적 - 직전분기 누적 (1분기는 그 자체가 단독값).
    [수정] net_income 하나가 None이라고 EPS/revenue/op_profit까지 전부 버리던 게이트를 제거.
           필드별로 독립적으로 살아남게 함 — "EPS만 왜 계속 안 채워지지" 현상의 직접 원인이었음.
           완전히 빈 응답(4개 필드 다 None)일 때만 그 분기를 포기.
    [추가] report_cache를 그대로 하위 호출에 전달 — 같은 (corp_code, year, reprt_code) 조합이
    이번 분기의 cur와 다음 분기의 prev로 중복 조회되는 것을 방지.
    """
    cur = _dart_quarter_report(corp_code, dart_api_key, year, QUARTER_REPRT_CODES[quarter],
                                report_cache=report_cache)
    if not any(v is not None for v in cur.values()):
        return {}  # 진짜 아무 것도 못 가져온 경우만 포기

    idx = _dart_index_report(corp_code, dart_api_key, year, QUARTER_REPRT_CODES[quarter],
                              report_cache=report_cache)
    out = {"roe": idx["roe"], "debt_ratio": idx["debt_ratio"],
           "current_ratio": idx["current_ratio"], "interest_coverage": idx["interest_coverage"]}

    if quarter == 1:
        out.update({k: cur.get(k) for k in ["revenue", "op_profit", "net_income", "eps"]})
        return out

    prev = _dart_quarter_report(corp_code, dart_api_key, year, QUARTER_REPRT_CODES[quarter - 1],
                                 report_cache=report_cache)
    for k in ["revenue", "op_profit", "net_income", "eps"]:
        if cur.get(k) is not None and prev.get(k) is not None:
            out[k] = cur[k] - prev[k]
        else:
            out[k] = None
    return out


def _pass_ratio(checks) -> float:
    valid = [c for c in checks if c is not None]
    if not valid:
        return 0.0
    return round(sum(1 for c in valid if c) / len(valid) * 100, 1)


def compute_trend_stats_from_closes(symbol: str, name: str, closes) -> dict | None:
    """
    [수정됨] 절대 기준(Absolute Criteria) 적용
    부분 점수(_pass_ratio)를 폐지하고, 미너비니의 핵심 템플릿 조건(AND)을
    완벽히 충족할 때만 100점, 하나라도 미달이면 0점 처리.

    [추가] 1차/2차 매수 판정용 플래그
      - is_value_buy : 200일선 근처(+5% 이내)로 눌렸고, 동시에 볼린저 하단(20,2)까지 이탈한 눌림목
      - is_second_buy: 52주 고점 대비 20% 이상 하락 (물타기/불타기 판단용, 종목 자체 기준)
    """
    s = pd.Series(closes).dropna().reset_index(drop=True)
    n = len(s)
    if n < TREND_MIN_BARS:
        return None

    ma50 = s.rolling(50).mean()
    ma150 = s.rolling(150).mean()
    ma200 = s.rolling(200).mean()

    # [추가] 볼린저 밴드 하단 (20일, 2표준편차) — 1차 매수(가치매수) 판정용
    bb_mid20 = s.rolling(20).mean()
    bb_std20 = s.rolling(20).std()
    bb_lower = bb_mid20 - 2 * bb_std20
    bb_lower_now = bb_lower.iloc[-1]

    price = float(s.iloc[-1])
    ma50_now, ma150_now, ma200_now = ma50.iloc[-1], ma150.iloc[-1], ma200.iloc[-1]

    if pd.isna(ma150_now) or pd.isna(ma200_now):
        return None

    def _at(series, back):
        idx = n - 1 - back
        if idx < 0:
            return None
        v = series.iloc[idx]
        return None if pd.isna(v) else v

    ma200_1m_ago = _at(ma200, 21)
    ma200_3m_ago = _at(ma200, 63)
    ma50_2w_ago = _at(ma50, 10)

    lookback = min(n, 252)
    recent = s.iloc[-lookback:]
    w52_high = float(recent.max())
    w52_low = float(recent.min())

    # 1. 정배열 (Trend Alignment): 가격 > 50MA > 150MA > 200MA 완벽 충족
    is_aligned = (
            pd.notna(ma50_now) and
            price > ma50_now and
            ma50_now > ma150_now and
            ma150_now > ma200_now
    )
    trend_alignment_score = 100 if is_aligned else 0

    # 2. 200일선 추세: 1개월 전 및 3개월 전 대비 200일선이 모두 상승 중이어야 함
    is_ma200_trending = (
            ma200_1m_ago is not None and ma200_now > ma200_1m_ago and
            ma200_3m_ago is not None and ma200_now > ma200_3m_ago
    )
    ma200_trend_score = 100 if is_ma200_trending else 0

    # 3. 고점 근접성: 미너비니의 절대 기준인 '52주 고점 대비 25% 이내'를 하드 조건으로 설정
    pct_from_high = (w52_high - price) / w52_high * 100 if w52_high else None
    high_proximity_score = 100 if (pct_from_high is not None and pct_from_high <= 25) else 0

    # 4. 저점 탈출: 신저가 대비 30% 이상 상승
    pct_above_low = (price - w52_low) / w52_low * 100 if w52_low else None
    low_rise_score = 100 if (pct_above_low is not None and pct_above_low >= 30) else 0

    # 5. 50일선 모멘텀: 가격이 50일선 위에 있고, 50일선 자체가 과거(2주전) 대비 상승 중일 것
    is_ma50_trending = (
            pd.notna(ma50_now) and ma50_2w_ago is not None and
            price > ma50_now and
            ma50_now > ma50_2w_ago
    )
    ma50_momentum_score = 100 if is_ma50_trending else 0

    # 6. 모멘텀 팩터 (RS 점수 산출 등 순위 매김용 팩터이므로 기존 로직 유지)
    def _ret(back):
        idx = n - 1 - back
        if idx < 0 or s.iloc[idx] == 0:
            return None
        return (price - s.iloc[idx]) / s.iloc[idx]

    ret_3m, ret_6m, ret_9m, ret_12m = _ret(63), _ret(126), _ret(189), _ret(252)
    weights = [0.4, 0.2, 0.2, 0.2]
    pairs = [(weights[0], ret_3m), (weights[1], ret_6m), (weights[2], ret_9m), (weights[3], ret_12m)]
    valid_pairs = [(w, r) for w, r in pairs if r is not None]
    raw_momentum = (sum(w * r for w, r in valid_pairs) / sum(w for w, _ in valid_pairs)) if valid_pairs else None

    ret_1m = _ret(21)

    # [수정] 1차 매수(눌림목 반등): "하단 이탈 시점"이 아니라 "이탈 후 반등 확인" 시점에 신호
    # 1. 200일선 근처(200일선 위/아래 5% 이내)
    # 2. 전일 종가가 볼린저 하단 아래로 이탈해 있었음 (저점 형성 확인)
    # 3. 오늘 종가가 전일 대비 상승 (반등 확인)
    # 4. 오늘 종가가 볼린저 하단 위로 복귀 (밴드 재진입 확인)
    prev_close = float(s.iloc[-2]) if n >= 2 else None
    prev_bb_lower = bb_lower.iloc[-2] if n >= 2 else None

    is_near_200ma = pd.notna(ma200_now) and price <= ma200_now * 1.05
    prev_below_lower = (
            prev_close is not None and pd.notna(prev_bb_lower) and prev_close < prev_bb_lower
    )
    rebound = prev_close is not None and price > prev_close
    return_inside = pd.notna(bb_lower_now) and price >= bb_lower_now

    is_value_buy = bool(is_near_200ma and prev_below_lower and rebound and return_inside)

    return {
        "symbol": symbol,
        "name": name,
        "current_price": round(price),
        "ma50": round(ma50_now, 1) if pd.notna(ma50_now) else None,
        "ma150": round(ma150_now, 1),
        "ma200": round(ma200_now, 1),
        "week52_high": round(w52_high),
        "week52_low": round(w52_low),
        "pct_from_52w_high": round(pct_from_high, 1) if pct_from_high is not None else None,
        "pct_above_52w_low": round(pct_above_low, 1) if pct_above_low is not None else None,
        "ret_1m": round(ret_1m * 100, 2) if ret_1m is not None else None,
        "trend_alignment_score": trend_alignment_score,
        "ma200_trend_score": ma200_trend_score,
        "high_proximity_score": high_proximity_score,
        "low_rise_score": low_rise_score,
        "ma50_momentum_score": ma50_momentum_score,
        "bb_lower": round(bb_lower_now, 1) if pd.notna(bb_lower_now) else None,
        "is_value_buy": is_value_buy,
        "_raw_momentum": raw_momentum,
    }


def _attach_rs_percentile_and_gates(rows: list) -> list:
    momentum_vals = sorted(r["_raw_momentum"] for r in rows if r["_raw_momentum"] is not None)
    for r in rows:
        v = r.pop("_raw_momentum")
        if v is not None and momentum_vals:
            rank = sum(1 for x in momentum_vals if x <= v) / len(momentum_vals)
            r["rs_score"] = round(max(1, min(99, rank * 99)))
        else:
            r["rs_score"] = None
        axis_scores = [
            r["trend_alignment_score"], r["ma200_trend_score"], r["high_proximity_score"],
            r["low_rise_score"], r["ma50_momentum_score"], r["rs_score"] or 0,
        ]
        r["entry_gate_pass_count"] = sum(1 for s in axis_scores if s is not None and s >= 70)
    return rows


def save_trend_stats_rows(supabase, rows: list) -> int:
    """계산이 끝난 rows를 stock_trend_stats에 upsert만 하는 저장 전용 함수.
    (계산과 저장을 분리해서, 계산 로직은 API fetcher가 뭐든 재사용 가능하게)"""
    if not rows:
        print("  [x] 저장할 트렌드 지표가 없습니다.")
        return 0
    rows = _attach_rs_percentile_and_gates(rows)
    ts = now_kst_str()
    for r in rows:
        r["updated_at"] = ts
    try:
        for i in range(0, len(rows), 500):
            supabase.table(TBL_TREND).upsert(rows[i:i + 500], on_conflict="symbol").execute()
        print(f"  [✓] stock_trend_stats 갱신 완료 ({len(rows)}종목)")
    except Exception as e:
        print(f"  [x] stock_trend_stats 저장 실패: {e}")
        return 0
    return len(rows)
