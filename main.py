import os
import json
import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
from typing import Optional, List
from cachetools import TTLCache, cached  # KRX 리스트 등 순수 TTL이 맞는 곳엔 계속 사용
from apscheduler.schedulers.background import BackgroundScheduler

# 기존 quant_core 및 real_estate 모듈 활용
from quant_core import load_price_from_db, fetch_naver_fundamental, calc_quant_metrics, now_kst, load_fundamental_from_db, save_fundamental_to_db
from real_estate import generate_excel_data

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ 경고: Supabase 환경 변수가 설정되지 않았습니다.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

KST = ZoneInfo("Asia/Seoul")

# ==============================================================================
# 🌟 스마트 캐시 (Last-Modified 기반)
# 하루 4번(08:02 / 14:30 / 16:32 / 23:32)만 배치로 데이터가 갱신되는 시스템이므로,
# "10분 지나면 무조건 새로고침" 대신 "DB의 최신 타임스탬프가 바뀌었을 때만 갱신"으로 동작합니다.
# 매 요청마다 가벼운 타임스탬프 조회 1번 + (변경됐을 때만) 전체 재조회.
# ==============================================================================
class SmartCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.last_ts = None  # 마지막으로 확인한 최신 타임스탬프

news_smart_cache = SmartCache()
quant_smart_cache = SmartCache()

# 종목 검색용 KRX 리스트: 배치와 무관하게 거의 안 바뀌므로 기존 24시간 TTL 유지
krx_cache = TTLCache(maxsize=5, ttl=86400)

# 💡 개별 종목 펀더멘털 데이터 캐시 (1시간 유지) - 기존과 동일
funda_cache = TTLCache(maxsize=200, ttl=3600)


def _get_latest_news_ts():
    """market_news 테이블에서 가장 최신 created_at 하나만 가볍게 조회"""
    res = supabase.table("market_news").select("created_at").order("created_at", desc=True).limit(1).execute()
    if res.data:
        return res.data[0]["created_at"]
    return None


def _fetch_all_news(limit: int = 500):
    res = supabase.table("market_news").select("*").order("created_at", desc=True).limit(limit).execute()
    return res.data


def refresh_news_cache(limit: int = 500):
    """뉴스 캐시를 강제로 최신화. 배치 직후 스케줄러가 미리 호출해 예열합니다."""
    if not supabase:
        return
    try:
        latest_ts = _get_latest_news_ts()
        data = _fetch_all_news(limit)
        with news_smart_cache.lock:
            news_smart_cache.data = data
            news_smart_cache.last_ts = latest_ts
        print(f"✅ [뉴스 캐시 예열] {latest_ts}")
    except Exception as e:
        print(f"⚠️ 뉴스 캐시 예열 실패: {e}")


def _get_latest_quant_ts():
    """
    quant_screening_cache 테이블에서 관련 id(1,2,11,12,13)들 중 가장 최근 갱신 시각을 조회.
    ⚠️ 이 테이블에 updated_at(timestamptz) 컬럼이 있어야 합니다.
       없다면: alter table quant_screening_cache add column updated_at timestamptz default now();
       그리고 각 배치 스크립트가 해당 id row를 upsert할 때 updated_at = now() 도 같이 갱신해야 합니다.
    """
    res = (
        supabase.table("quant_screening_cache")
        .select("updated_at")
        .in_("id", [1, 2, 11, 12, 13])
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]["updated_at"]
    return None


def _fetch_all_quant():
    data = {
        "holdings": [],
        "trades": [],
        "history": [],
        "confirmed": [],
        "watchlist": []
    }

    r1 = supabase.table("quant_screening_cache").select("id, results").in_("id", [11, 12, 13]).execute()
    if r1.data:
        for row in r1.data:
            try:
                res_json = json.loads(row["results"])
                if row["id"] == 11: data["holdings"] = res_json
                elif row["id"] == 12: data["trades"] = res_json
                elif row["id"] == 13: data["history"] = res_json
            except: pass

    r2 = supabase.table("quant_screening_cache").select("id, results").in_("id", [1, 2]).execute()
    if r2.data:
        for row in r2.data:
            try:
                res_json = json.loads(row["results"])
                if row["id"] == 1: data["confirmed"] = res_json
                elif row["id"] == 2: data["watchlist"] = res_json
            except: pass

    return data


def refresh_quant_cache():
    """퀀트 대시보드 캐시를 강제로 최신화. 배치 직후 스케줄러가 미리 호출해 예열합니다."""
    if not supabase:
        return
    try:
        latest_ts = _get_latest_quant_ts()
        data = _fetch_all_quant()
        with quant_smart_cache.lock:
            quant_smart_cache.data = data
            quant_smart_cache.last_ts = latest_ts
        print(f"✅ [퀀트 캐시 예열] {latest_ts}")
    except Exception as e:
        print(f"⚠️ 퀀트 캐시 예열 실패: {e}")


# ==============================================================================
# 📰 1. 마켓 뉴스 데스크 API (Last-Modified 기반 스마트 캐시)
# ==============================================================================
@app.get("/api/news")
def get_news(limit: int = 500, refresh: str = "false"):
    if not supabase:
        return {"status": "error", "message": "DB 설정 안됨"}
    try:
        if refresh.lower() == "true":
            refresh_news_cache(limit)
            with news_smart_cache.lock:
                return {"status": "success", "data": news_smart_cache.data, "cached": False}

        latest_ts = _get_latest_news_ts()

        with news_smart_cache.lock:
            if news_smart_cache.data is not None and news_smart_cache.last_ts == latest_ts:
                # DB에 새 데이터가 없음 -> 메모리에 있던 걸 그대로 반환 (무거운 조회 스킵)
                return {"status": "success", "data": news_smart_cache.data, "cached": True}

        # 최신 타임스탬프가 바뀜 -> 그때만 전체 재조회
        data = _fetch_all_news(limit)
        with news_smart_cache.lock:
            news_smart_cache.data = data
            news_smart_cache.last_ts = latest_ts

        return {"status": "success", "data": data, "cached": False}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 📈 2. 퀀트 데스크 전체 탭 데이터 통합 제공 API (Last-Modified 기반 스마트 캐시)
# ==============================================================================
@app.get("/api/quant-dashboard")
def get_quant_dashboard(refresh: str = "false"):
    if not supabase:
        return {"status": "error", "message": "DB 설정 안됨"}
    try:
        if refresh.lower() == "true":
            refresh_quant_cache()
            with quant_smart_cache.lock:
                return {"status": "success", "data": quant_smart_cache.data}

        latest_ts = _get_latest_quant_ts()

        with quant_smart_cache.lock:
            if quant_smart_cache.data is not None and quant_smart_cache.last_ts == latest_ts:
                return {"status": "success", "data": quant_smart_cache.data, "cached": True}

        data = _fetch_all_quant()
        with quant_smart_cache.lock:
            quant_smart_cache.data = data
            quant_smart_cache.last_ts = latest_ts

        return {"status": "success", "data": data, "cached": False}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 🔍 3. 개별 종목 검색창 목록 제공 API (24시간 TTL 캐시 유지 - 배치와 무관하게 안정적)
# ==============================================================================
@app.get("/api/krx-list")
@cached(cache=krx_cache)
def get_krx_list(refresh: str = "false"):
    if refresh.lower() == "true":
        krx_cache.clear()

    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        res = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        if res.data:
            return {"status": "success", "data": json.loads(res.data[0]["results"])}
        return {"status": "success", "data": []}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# ⚡ 4. 실시간 개별 종목 분석 리포트 API (캐시 안 함 - 항상 최신)
# ==============================================================================
@app.get("/api/search/{symbol}")
def search_stock(symbol: str):
    if not supabase: return {"status": "error", "message": "DB 설정 안됨"}
    try:
        df_price = load_price_from_db(supabase, symbol)
        if df_price.empty:
            df_price = fdr.DataReader(symbol, (now_kst() - timedelta(days=300)).strftime('%Y-%m-%d'))

        if df_price.empty:
            return {"status": "error", "message": "차트 데이터를 불러올 수 없습니다."}

        # 1. 차트 데이터 정제 (최근 120일)
        chart_data = []
        df_recent = df_price.tail(120)
        for idx, row in df_recent.iterrows():
            chart_data.append({
                "date": idx.strftime("%Y-%m-%d"),
                "price": int(row['Close'])
            })

        curr_price = int(df_price['Close'].iloc[-1])
        ret_1m = ((curr_price - df_price['Close'].iloc[-21]) / df_price['Close'].iloc[-21] * 100) if len(df_price) >= 21 else 0

        # 💡 2. 펀더멘털 조회 (인메모리 캐시 확인 로직 추가)
        if symbol in funda_cache:
            naver_fund = funda_cache[symbol]
        else:
            naver_fund = fetch_naver_fundamental(symbol)
            if naver_fund:
                funda_cache[symbol] = naver_fund  # 캐시에 저장

        # 💡 [누락 복구] 빈 문자열 덮어쓰기 삭제! 네이버 펀더멘털에서 제대로 추출된 섹터 정보를 사용합니다.
        sector = naver_fund.get("sector", "") if naver_fund else ""

        metrics = calc_quant_metrics(df_price, naver_fund)

        score = 0.0
        gates = {}

        if metrics and metrics.get("ma20", 0) > 0:
            f_growth = bool(metrics["growth_composite"] > 0)
            f_mdd    = bool(metrics["mdd"] >= metrics["dynamic_mdd_limit"])
            f_liq    = bool(metrics["liquidity_20d"] >= 50)
            f_trend  = bool((curr_price > metrics["ma20"]) and (metrics["ma20"] > metrics["ma60"]))
            f_break  = bool(curr_price >= (metrics["high_60d"] * 0.90))
            f_vol    = bool(metrics["vol_5d"] > (metrics["vol_60d"] * 1.5))

            gates = {
                'A': {'name': 'Growth Composite', 'pass': f_growth, 'reason': f"Comp {metrics.get('growth_composite',0):+.1f}%"},
                'B': {'name': 'Dynamic MDD', 'pass': f_mdd, 'reason': f"MDD {metrics.get('mdd',0):.1f}% (Limit: {metrics.get('dynamic_mdd_limit',0):.1f}%)"},
                'C': {'name': 'Liquidity', 'pass': f_liq, 'reason': f"{metrics.get('liquidity_20d',0):,.0f}억"},
                'D': {'name': 'Trend Alignment', 'pass': f_trend, 'reason': "Price > 20MA > 60MA" if f_trend else "추세 미달"},
                'E': {'name': 'Price Breakout', 'pass': f_break, 'reason': f"고점대비 {(curr_price/metrics.get('high_60d',1))*100:.1f}%" if metrics.get('high_60d') else "-"},
                'F': {'name': 'Volume Surge', 'pass': f_vol, 'reason': f"Vol {metrics.get('vol_5d',0)/metrics.get('vol_60d',1):.1f}x 급증" if metrics.get('vol_60d') else "-"}
            }

            pass_count = sum([1 for g in gates.values() if g['pass']])
            mom = ((curr_price - metrics["ma60"]) / metrics["ma60"] * 100) if metrics["ma60"] > 0 else 0
            net_yoy = metrics.get("net_yoy", 0)
            score = float(min(99.9, max(0, (pass_count/6 * 50) + min(25, max(0, net_yoy/5)) + min(25, max(0, mom)))))

        # 3. KOSPI 면 마켓 등 정보 수정
        market = "KOSPI"

        # krx list에서 종목명 등 조회 시도
        r_krx = supabase.table("quant_screening_cache").select("results").eq("id", 99).execute()
        stock_name = symbol
        if r_krx.data:
            krx_list = json.loads(r_krx.data[0]["results"])
            matched = next((item for item in krx_list if item["Symbol"] == symbol), None)
            if matched:
                stock_name = matched.get("Name", symbol)
                # 💡 [추가] 마스터 데이터에 Market 정보가 있다면 KOSDAQ 등으로 맵핑
                market = matched.get("Market", "KOSPI")

        result_data = {
            "symbol": symbol,
            "name": stock_name,
            "current_price": curr_price,
            "ret_1m": ret_1m,
            "score": score,
            "gates": gates,
            "fundamental": naver_fund,
            "chart_data": chart_data,
            "market": market,
            "sector": sector  # 🌟 제대로 할당된 섹터 변수가 응답 JSON에 들어갑니다!
        }

        return {"status": "success", "data": result_data}

    except Exception as e:
        import traceback
        traceback.print_exc()  # 렌더(Render) 로그 확인용
        return {"status": "error", "message": str(e)}


# ==============================================================================
# 🏢 5. 부동산 아파트 실거래가 스캔 API (스트리밍 파싱)
# ==============================================================================
class RealEstateRequest(BaseModel):
    api_key: str
    district_code: str
    district_name: str
    target_dong: str
    start_date: str
    end_date: str
    apt_filters: str

from fastapi.responses import StreamingResponse

@app.post("/api/real-estate")
async def scan_real_estate(req: RealEstateRequest):
    async def event_generator():
        try:
            sd = datetime.strptime(req.start_date, "%Y-%m-%d")
            ed = datetime.strptime(req.end_date, "%Y-%m-%d")
            filters = [f.strip() for f in req.apt_filters.split(',')] if req.apt_filters else []

            gen = generate_excel_data(
                req.api_key, req.district_code, req.district_name,
                req.target_dong, sd, ed, filters
            )

            for event_type, data in gen:
                if event_type == "success":
                    import base64
                    b64_data = base64.b64encode(data["data"]).decode('utf-8')
                    yield f"data: {json.dumps({'status': 'success', 'file_data': b64_data, 'filename': data['filename']})}\n\n"
                    break
                elif event_type == "error":
                    yield f"data: {json.dumps({'status': 'error', 'message': data})}\n\n"
                    break
                else:
                    yield f"data: {json.dumps({'status': event_type, 'message': data})}\n\n"
                await asyncio.sleep(0.01)
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ==============================================================================
# ⏰ 배치 시각 기반 캐시 예열(warm-up) 스케줄러
# 08:02 / 14:30 / 16:32 / 23:32 KST 에 배치가 도니까, 몇 분 뒤(버퍼)에
# 캐시를 미리 한 번 갱신해둡니다. 이렇게 하면 배치 직후 첫 사용자가
# "캐시 미스로 인한 느린 응답"을 겪지 않고, last-modified 체크가 바로 캐시 히트로 잡힙니다.
# (실제 데이터 최신성 보장은 위의 last-modified 체크가 담당하므로,
#  이 스케줄러가 몇 분 늦거나 배치가 조금 밀려도 안전합니다.)
# ==============================================================================
scheduler = BackgroundScheduler(timezone=KST)

BATCH_WARMUP_TIMES = [
    (8, 5),    # 08:02 배치 -> 08:05 예열
    (14, 33),  # 14:30 배치 -> 14:33 예열
    (16, 35),  # 16:32 배치 -> 16:35 예열
    (23, 35),  # 23:32 배치 -> 23:35 예열
]

def _warmup_all():
    refresh_news_cache()
    refresh_quant_cache()

for hour, minute in BATCH_WARMUP_TIMES:
    scheduler.add_job(_warmup_all, "cron", hour=hour, minute=minute)

@app.on_event("startup")
def start_scheduler():
    if supabase:
        # 서버 기동 시에도 한 번 예열
        _warmup_all()
    scheduler.start()

@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
